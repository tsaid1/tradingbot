"""Offline, broker-neutral instrument-mapping diagnostics. NON-AUTHORITATIVE.

Inspects explicit synthetic mapping records and reports what is missing, ambiguous, mismatched,
invalid or unsupported about the relationship between a quoted source price series (an index, a
spot level, a continuous futures series, a listed security...) and the instrument a record says
it maps to. Every number, contract field and symbol is whatever the record author typed; nothing
is looked up, inferred or defaulted.

What this module deliberately does NOT do:

* no catalog, broker, network or credential access;
* no price conversion, roll choice, source-to-contract selection, sizing, order construction,
  eligibility decision or authorization of any kind;
* no change to the deterministic gateway (``validator.py``) or to any versioned trading contract.

Findings are diagnostics for a human reviewer. An INFO finding is never trading permission; the
gateway still applies every provenance, freshness, quote and policy rule it applies today, and
options and futures remain unsupported by the current paper adapter whatever their metadata says.
"""
from __future__ import annotations

import json
import re
from datetime import date
from decimal import ROUND_HALF_EVEN, Context, Decimal, InvalidOperation, localcontext
from enum import Enum
from pathlib import Path
from typing import Literal

from pydantic import Field

from tradingbot.event_models import PriceBasis
from tradingbot.models import AssetClass, Instrument, StrictModel
from tradingbot.validator import SYMBOL_RE

RULES_VERSION = 2
AUTHORITY = "NON_AUTHORITATIVE"
NOTICE = (
    "Non-authoritative offline diagnostic over synthetic mapping records. It reports metadata gaps, "
    "ambiguities, mismatches and invalid economics only. It performs no price conversion, roll choice, "
    "source-to-contract selection, sizing, order construction or authorization. INFO findings are not "
    "trading permission; the deterministic gateway is unchanged and still decides every action."
)

CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
# Numeric inputs are bounded BEFORE any arithmetic so that one extreme record becomes a finding instead
# of an exception that would abort the whole batch: at most NUMERIC_MAX_DIGITS significant digits and a
# magnitude from 1e-12 up to but excluding 1e16. No market quote, multiplier, tick or strike lies outside.
NUMERIC_MAX_DIGITS = 28
NUMERIC_MIN_ADJUSTED_EXPONENT = -12
NUMERIC_MAX_ADJUSTED_EXPONENT = 15
NUMERIC_RANGE_TEXT = (f"at most {NUMERIC_MAX_DIGITS} significant digits and a magnitude from "
                      f"1e{NUMERIC_MIN_ADJUSTED_EXPONENT} up to but excluding 1e{NUMERIC_MAX_ADJUSTED_EXPONENT + 1}")
# Exact for every product and remainder of two in-range numbers (a product has at most 56 coefficient
# digits; a remainder's integer quotient stays below 1e28). Applied only through localcontext(), so the
# ambient decimal context of the host process is never modified.
ARITHMETIC_CONTEXT = Context(prec=64, rounding=ROUND_HALF_EVEN)
CONTRACT_MONTH_RE = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")
# Root, delivery-month letter and one- or two-digit year, e.g. ABCZ6 / ABCZ26. A widely used
# convention, not a venue catalog: codes that do not match are reported as unrecognised, not rejected.
FUTURES_CODE_RE = re.compile(r"^([A-Z]{1,4})([FGHJKMNQUVXZ])(\d{1,2})$")
MONTH_LETTERS = "FGHJKMNQUVXZ"  # January..December
DERIVATIVES = frozenset({AssetClass.OPTION, AssetClass.FUTURE})
CASH_INSTRUMENTS = frozenset({AssetClass.EQUITY, AssetClass.ETF})
# Undated instruments (multi-asset mapping): spot commodities, CFDs and reference indexes carry no
# expiry, strike or underlying; an index is a reference series only and is never orderable.
UNDATED_INSTRUMENTS = frozenset({AssetClass.COMMODITY, AssetClass.CFD, AssetClass.INDEX})


# ----------------------------------------------------------------------------- input records

class SeriesKind(str, Enum):
    """What the record says the quoted source level refers to."""
    TRADED_SECURITY = "TRADED_SECURITY"        # a listed stock/ETF quote
    DATED_CONTRACT = "DATED_CONTRACT"          # a specific expiring contract quote
    INDEX = "INDEX"                            # a cash index level; not orderable
    SPOT = "SPOT"                              # a spot/cash commodity or FX level; not orderable
    CONTINUOUS_FUTURES = "CONTINUOUS_FUTURES"  # a rolled front-month series; identifies no contract
    CFD = "CFD"                                # a dealer contract-for-difference quote
    UNKNOWN = "UNKNOWN"


ORDERABLE_KINDS = frozenset({SeriesKind.TRADED_SECURITY, SeriesKind.DATED_CONTRACT})
NON_ORDERABLE_KINDS = frozenset({SeriesKind.INDEX, SeriesKind.SPOT, SeriesKind.CONTINUOUS_FUTURES, SeriesKind.CFD})


class SourceSeries(StrictModel):
    """The publisher-side price series exactly as the record describes it."""
    symbol: str | None = Field(default=None, max_length=40)
    kind: SeriesKind = SeriesKind.UNKNOWN
    currency: str | None = None
    unit: str | None = None          # free label, e.g. USD_PER_SHARE, INDEX_POINT, USD_PER_BARREL
    quoted_level: str | None = None  # decimal string; never converted


class ContractSpec(StrictModel):
    """Record-supplied contract economics. Nothing here is looked up or defaulted; a synthetic
    record states whatever it states and the diagnostic only checks it for gaps and contradictions."""
    currency: str | None = None
    unit: str | None = None
    multiplier: str | None = None
    tick_size: str | None = None
    tick_value: str | None = None
    settlement: Literal["CASH", "PHYSICAL"] | None = None
    settlement_time: Literal["AM", "PM"] | None = None
    exercise_style: Literal["AMERICAN", "EUROPEAN"] | None = None
    contract_month: str | None = None  # YYYY-MM


class MappingRecord(StrictModel):
    """One explicit, synthetic mapping input. ``synthetic`` must be stated: unmarked input is refused."""
    record_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    synthetic: Literal[True]
    description: str = ""
    as_of: str | None = None  # ISO date used only for expiry comparison; wall-clock is never read
    source: SourceSeries | None = None
    instrument: Instrument | None = None
    price_basis: PriceBasis = PriceBasis.UNKNOWN
    contract: ContractSpec | None = None


# ----------------------------------------------------------------------------- findings and report

class FindingKind(str, Enum):
    INFO = "INFO"                # informational control; not permission
    MISSING = "MISSING"          # required metadata absent
    AMBIGUOUS = "AMBIGUOUS"      # supplied identifiers admit more than one reading
    MISMATCH = "MISMATCH"        # supplied fields contradict each other
    INVALID = "INVALID"          # numeric or format error in supplied economics
    UNSUPPORTED = "UNSUPPORTED"  # the current runtime cannot act on this class whatever the metadata


# Most pressing first. A record's headline is its most severe finding kind.
HEADLINE_ORDER = (FindingKind.INVALID, FindingKind.MISMATCH, FindingKind.MISSING, FindingKind.AMBIGUOUS,
                  FindingKind.UNSUPPORTED, FindingKind.INFO)


class Identity(str, Enum):
    DIRECT = "DIRECT"                              # source label is the instrument label
    DERIVATIVE_OF_SOURCE = "DERIVATIVE_OF_SOURCE"  # source label is the derivative's stated underlying
    DIFFERENT = "DIFFERENT"                        # labels differ; no alias resolution is attempted
    UNDETERMINED = "UNDETERMINED"                  # a side of the comparison is missing


class RuntimeScope(str, Enum):
    EQUITY_ETF_GATEWAY_CHECKS_STILL_APPLY = "EQUITY_ETF_GATEWAY_CHECKS_STILL_APPLY"
    ASSET_CLASS_UNSUPPORTED_IN_CURRENT_RUNTIME = "ASSET_CLASS_UNSUPPORTED_IN_CURRENT_RUNTIME"
    UNDETERMINED = "UNDETERMINED"


class Finding(StrictModel):
    code: str
    kind: FindingKind
    field: str | None = None
    detail: str


class RecordDiagnostic(StrictModel):
    authority: Literal["NON_AUTHORITATIVE"] = AUTHORITY
    record_id: str
    asset_class: AssetClass | None = None
    identity: Identity
    runtime_scope: RuntimeScope
    headline: FindingKind
    counts: dict[str, int]
    findings: list[Finding]


class MappingDiagnosticReport(StrictModel):
    report_kind: Literal["INSTRUMENT_MAPPING_DIAGNOSTIC"] = "INSTRUMENT_MAPPING_DIAGNOSTIC"
    authority: Literal["NON_AUTHORITATIVE"] = AUTHORITY
    rules_version: int = RULES_VERSION
    notice: str = NOTICE
    record_count: int
    totals: dict[str, int]
    headlines: dict[str, int]
    records: list[RecordDiagnostic]


# ----------------------------------------------------------------------------- helpers

def _decimal(value: str | None) -> Decimal | None:
    """Finite decimal or None. Sign is checked by the caller; malformed text is None."""
    if value is None:
        return None
    try:
        result = Decimal(value)
    except (InvalidOperation, ValueError, TypeError):
        return None
    return result if result.is_finite() else None


def _positive(value: str | None) -> tuple[Decimal | None, str | None]:
    """(number, None) for a positive finite decimal inside the supported range; otherwise (None, why).

    The range check happens before any arithmetic, so an extreme but finite value is reported as INVALID
    for its own field and can never raise out of the rule set.
    """
    result = _decimal(value)
    if result is None or result <= 0:
        return None, "not a positive finite decimal"
    if len(result.as_tuple().digits) > NUMERIC_MAX_DIGITS or \
            not NUMERIC_MIN_ADJUSTED_EXPONENT <= result.adjusted() <= NUMERIC_MAX_ADJUSTED_EXPONENT:
        return None, f"outside the supported numeric range ({NUMERIC_RANGE_TEXT})"
    return result, None


def _iso_date(value: str | None) -> date | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(value)
    except (ValueError, TypeError):
        return None


def _label(value: str | None) -> str | None:
    """Comparison form of a symbol label: trimmed, upper-cased, leading cashtag removed.
    This is the only normalisation performed; no alias table exists."""
    if value is None:
        return None
    text = value.strip().upper().removeprefix("$")
    return text or None


def _blank(value: object) -> bool:
    return type(value) is str and not value.strip()


def _without_blanks(model):
    """Copy of a record component in which whitespace-only optional strings are None, plus the field names
    affected. Diagnostic-local: the shared Instrument contract itself is not changed and still accepts them."""
    if model is None:
        return None, []
    fields = type(model).model_fields
    names = [name for name, value in model if _blank(value) and not fields[name].is_required()]
    if not names:
        return model, []
    return model.model_copy(update={name: None for name in names}), names


def _normalise(record: MappingRecord) -> tuple[MappingRecord, list[str]]:
    """Blank strings are 'not stated'. Returns the normalised record and the dotted paths that were blank."""
    source, source_names = _without_blanks(record.source)
    instrument, instrument_names = _without_blanks(record.instrument)
    contract, contract_names = _without_blanks(record.contract)
    blanks = ([f"source.{n}" for n in source_names] + [f"instrument.{n}" for n in instrument_names]
              + [f"contract.{n}" for n in contract_names])
    update: dict = {"source": source, "instrument": instrument, "contract": contract}
    if _blank(record.as_of):
        update["as_of"] = None
        blanks.append("as_of")
    return record.model_copy(update=update), blanks


class _Collector:
    def __init__(self) -> None:
        self.findings: list[Finding] = []

    def add(self, code: str, kind: FindingKind, detail: str, field: str | None = None) -> None:
        self.findings.append(Finding(code=code, kind=kind, field=field, detail=detail))

    def has(self, code: str) -> bool:
        return any(f.code == code for f in self.findings)


# ----------------------------------------------------------------------------- rules

def _identity_rules(r: MappingRecord, c: _Collector) -> Identity:
    inst, src = r.instrument, r.source
    if inst is None:
        c.add("INSTRUMENT_MISSING", FindingKind.MISSING, "No traded instrument is stated; nothing to map to.",
              "instrument")
    inst_label = _label(inst.symbol) if inst is not None else None
    if inst is not None and inst_label is None:
        c.add("INSTRUMENT_SYMBOL_BLANK", FindingKind.MISSING, "The instrument symbol is blank.", "instrument.symbol")
    src_label = _label(src.symbol) if src is not None else None
    if src_label is None:
        c.add("SOURCE_SERIES_MISSING", FindingKind.MISSING,
              "No source price series is stated; the quoted level cannot be tied to anything.", "source.symbol")
    if r.price_basis == PriceBasis.UNKNOWN:
        c.add("PRICE_BASIS_UNKNOWN", FindingKind.AMBIGUOUS,
              "The record does not say whether the quoted level is the traded contract's price or a source-"
              "series level; no basis is assumed.", "price_basis")
    if src is not None and src.kind == SeriesKind.UNKNOWN:
        c.add("SOURCE_SERIES_KIND_UNKNOWN", FindingKind.AMBIGUOUS,
              "The source series is not classified (index, spot, continuous, listed security...); its "
              "orderability cannot be assessed.", "source.kind")
    if inst is None or inst_label is None or src is None or src_label is None:
        return Identity.UNDETERMINED

    underlying = _label(inst.underlying)
    if src_label == inst_label:
        identity = Identity.DIRECT
    elif inst.asset_class in DERIVATIVES and underlying is not None and src_label == underlying:
        identity = Identity.DERIVATIVE_OF_SOURCE
    else:
        identity = Identity.DIFFERENT

    # Kind versus stated basis.
    if identity == Identity.DIRECT and src.kind in ORDERABLE_KINDS and r.price_basis == PriceBasis.SOURCE_SERIES:
        c.add("SOURCE_KIND_BASIS_CONFLICT", FindingKind.MISMATCH,
              f"The source is the instrument's own {src.kind.value} quote, yet price_basis says SOURCE_SERIES.",
              "price_basis")
    if src.kind in NON_ORDERABLE_KINDS and r.price_basis == PriceBasis.TRADED_CONTRACT:
        c.add("PRICE_BASIS_CLAIM_CONFLICT", FindingKind.MISMATCH,
              f"price_basis claims TRADED_CONTRACT but the source series kind is {src.kind.value}, which "
              "cannot be the price of an orderable contract.", "price_basis")
    if r.price_basis == PriceBasis.TRADED_CONTRACT and identity != Identity.DIRECT \
            and not c.has("PRICE_BASIS_CLAIM_CONFLICT"):
        c.add("PRICE_BASIS_CLAIM_CONFLICT", FindingKind.MISMATCH,
              f"price_basis claims TRADED_CONTRACT but the source label {src_label} is not the instrument "
              f"label {inst_label}.", "price_basis")
    if r.price_basis == PriceBasis.SOURCE_SERIES:
        c.add("PRICE_BASIS_NOT_TRADED_CONTRACT", FindingKind.MISMATCH,
              "The quoted level is a source-series level, not the instrument's own price. No conversion is "
              "performed; the gateway holds such actions as SOURCE_TO_BROKER_PRICE_BASIS_UNRESOLVED.",
              "price_basis")

    # Label relationships.
    if identity == Identity.DIRECT:
        expected_kind = (SeriesKind.TRADED_SECURITY if inst.asset_class in CASH_INSTRUMENTS
                         else SeriesKind.DATED_CONTRACT)
        if src.kind in NON_ORDERABLE_KINDS:
            c.add("SOURCE_KIND_NOT_ORDERABLE", FindingKind.MISMATCH,
                  f"Same label on both sides, but the source series kind is {src.kind.value}; such a series "
                  "cannot be ordered as an instrument.", "source.kind")
        elif src.kind in ORDERABLE_KINDS and src.kind != expected_kind:
            c.add("SOURCE_KIND_ASSET_CLASS_CONFLICT", FindingKind.MISMATCH,
                  f"Same label on both sides, but source kind {src.kind.value} does not describe a "
                  f"{inst.asset_class.value} (expected {expected_kind.value}).", "source.kind")
        elif r.price_basis == PriceBasis.TRADED_CONTRACT and src.kind in ORDERABLE_KINDS:
            c.add("DIRECT_IDENTITY_CONTROL", FindingKind.INFO,
                  f"Source label {src_label} equals instrument label {inst_label} and the quoted level is "
                  "stated as the traded contract's own price. Informational control only: this is not trading "
                  "permission and the gateway still applies every provenance, freshness, quote and policy rule.")
    elif identity == Identity.DERIVATIVE_OF_SOURCE:
        c.add("SOURCE_IS_STATED_UNDERLYING", FindingKind.INFO,
              f"The record states {src_label} as the underlying of {inst_label}. The contract's price is not "
              "the underlying level; no basis, carry, premium, roll or contract selection is derived.")
        if src.kind == SeriesKind.CONTINUOUS_FUTURES and inst.asset_class == AssetClass.FUTURE:
            c.add("CONTINUOUS_SERIES_ROLL_UNRESOLVED", FindingKind.AMBIGUOUS,
                  "A continuous front-month series does not identify a dated contract; no roll choice is made.",
                  "source.kind")
    else:  # DIFFERENT
        if inst.asset_class in CASH_INSTRUMENTS:
            if src.kind in NON_ORDERABLE_KINDS or src.kind == SeriesKind.UNKNOWN:
                c.add("SOURCE_SERIES_PROXY_MISMATCH", FindingKind.MISMATCH,
                      f"The quoted {src.kind.value} series {src_label} is not the {inst.asset_class.value} "
                      f"{inst_label}; using one as a proxy for the other is a mapping decision this diagnostic "
                      "does not make.", "source.symbol")
            else:
                c.add("SOURCE_INSTRUMENT_SYMBOL_MISMATCH", FindingKind.MISMATCH,
                      f"The quoted security {src_label} is a different security from {inst_label}.",
                      "source.symbol")
        else:
            if underlying is None:
                c.add("SOURCE_UNDERLYING_UNCOMPARABLE", FindingKind.AMBIGUOUS,
                      f"Source label {src_label} differs from instrument label {inst_label} and no underlying is "
                      "stated, so the relationship cannot be assessed.", "instrument.underlying")
            else:
                c.add("SOURCE_UNDERLYING_LABEL_MISMATCH", FindingKind.AMBIGUOUS,
                      f"Source label {src_label} differs from the stated underlying {underlying}; the "
                      "diagnostic does not resolve aliases, so the relationship is unverified.",
                      "instrument.underlying")
            if src.kind == SeriesKind.CONTINUOUS_FUTURES and inst.asset_class == AssetClass.FUTURE:
                c.add("CONTINUOUS_SERIES_ROLL_UNRESOLVED", FindingKind.AMBIGUOUS,
                      "A continuous front-month series does not identify a dated contract; no roll choice is "
                      "made.", "source.kind")
    return identity


def _unit_currency_rules(r: MappingRecord, c: _Collector) -> None:
    src, spec = r.source, r.contract
    for path, code in (("source.currency", src.currency if src else None),
                       ("contract.currency", spec.currency if spec else None)):
        if code is not None and not CURRENCY_RE.fullmatch(code):
            c.add("CURRENCY_CODE_INVALID", FindingKind.INVALID,
                  f"{path} '{code}' is not a three-letter upper-case currency code.", path)
    if src is not None:
        if src.currency is None:
            c.add("SOURCE_CURRENCY_MISSING", FindingKind.MISSING, "The source series states no currency.",
                  "source.currency")
        if src.unit is None:
            c.add("SOURCE_UNIT_MISSING", FindingKind.MISSING,
                  "The source series states no unit (per share, index point, per barrel...).", "source.unit")
    if spec is None:
        if r.instrument is not None:
            c.add("CONTRACT_SPEC_MISSING", FindingKind.MISSING,
                  "No contract economics (currency, unit, multiplier, tick) are stated for the instrument.",
                  "contract")
        return
    if spec.currency is None:
        c.add("TRADED_CURRENCY_MISSING", FindingKind.MISSING, "The instrument states no trading currency.",
              "contract.currency")
    if spec.unit is None:
        c.add("TRADED_UNIT_MISSING", FindingKind.MISSING, "The instrument states no price unit.",
              "contract.unit")
    if src is not None:
        if src.currency and spec.currency and src.currency != spec.currency:
            c.add("CURRENCY_MISMATCH", FindingKind.MISMATCH,
                  f"Source currency {src.currency} differs from instrument currency {spec.currency}; no FX "
                  "conversion is performed.", "contract.currency")
        if src.unit and spec.unit and src.unit.strip().upper() != spec.unit.strip().upper():
            c.add("UNIT_MISMATCH", FindingKind.MISMATCH,
                  f"Source unit {src.unit} differs from instrument unit {spec.unit}; no unit conversion is "
                  "performed.", "contract.unit")


def _economics_rules(r: MappingRecord, c: _Collector) -> None:
    inst, spec, src = r.instrument, r.contract, r.source
    cls = inst.asset_class if inst is not None else None
    level = None
    if src is not None and src.quoted_level is not None:
        level, problem = _positive(src.quoted_level)
        if problem:
            c.add("SOURCE_LEVEL_INVALID", FindingKind.INVALID,
                  f"Quoted level '{src.quoted_level}' is {problem}.", "source.quoted_level")
    if spec is None:
        return
    multiplier = tick = tick_value = None
    if spec.multiplier is not None:
        multiplier, problem = _positive(spec.multiplier)
        if problem:
            c.add("MULTIPLIER_INVALID", FindingKind.INVALID,
                  f"Multiplier '{spec.multiplier}' is {problem}.", "contract.multiplier")
    elif cls in DERIVATIVES:
        c.add("MULTIPLIER_MISSING", FindingKind.MISSING,
              "A derivative without a stated multiplier has undefined notional per contract.",
              "contract.multiplier")
    if spec.tick_size is not None:
        tick, problem = _positive(spec.tick_size)
        if problem:
            c.add("TICK_SIZE_INVALID", FindingKind.INVALID,
                  f"Tick size '{spec.tick_size}' is {problem}.", "contract.tick_size")
    elif cls == AssetClass.FUTURE:
        c.add("TICK_SIZE_MISSING", FindingKind.MISSING, "The future states no minimum price increment.",
              "contract.tick_size")
    if spec.tick_value is not None:
        tick_value, problem = _positive(spec.tick_value)
        if problem:
            c.add("TICK_VALUE_INVALID", FindingKind.INVALID,
                  f"Tick value '{spec.tick_value}' is {problem}.", "contract.tick_value")
    # Every operand passed the range check, so both operations below are exact in this context and cannot
    # signal; the context is local to this block and the host process's decimal settings are untouched.
    with localcontext(ARITHMETIC_CONTEXT):
        if tick_value is not None and multiplier is not None and tick is not None:
            expected = multiplier * tick
            if tick_value != expected:
                c.add("TICK_VALUE_INCONSISTENT", FindingKind.MISMATCH,
                      f"Stated tick value {tick_value} is not multiplier {multiplier} x tick size {tick} = "
                      f"{expected}.", "contract.tick_value")
        if level is not None and tick is not None and r.price_basis == PriceBasis.TRADED_CONTRACT \
                and level % tick != 0:
            c.add("QUOTED_LEVEL_OFF_TICK_GRID", FindingKind.AMBIGUOUS,
                  f"Quoted level {level} is not a multiple of the stated tick size {tick}, which is unusual for a "
                  "level said to be the contract's own price.", "source.quoted_level")


def _contract_rules(r: MappingRecord, c: _Collector) -> None:
    inst, spec = r.instrument, r.contract
    if inst is None:
        return
    cls = inst.asset_class
    as_of = _iso_date(r.as_of)
    if r.as_of is not None and as_of is None:
        c.add("AS_OF_DATE_INVALID", FindingKind.INVALID, f"as_of '{r.as_of}' is not an ISO date.", "as_of")

    if cls in CASH_INSTRUMENTS:
        if inst.symbol.strip() and not SYMBOL_RE.fullmatch(inst.symbol):
            c.add("SYMBOL_FORMAT_INVALID", FindingKind.INVALID,
                  f"Symbol '{inst.symbol}' does not match the gateway symbol pattern (the gateway blocks it as "
                  "INVALID_SYMBOL).", "instrument.symbol")
        elif "." in inst.symbol:
            c.add("SYMBOL_SUFFIX_CONVENTION_AMBIGUOUS", FindingKind.AMBIGUOUS,
                  f"Symbol '{inst.symbol}' carries a share-class or suffix separator whose spelling differs "
                  "between venues and vendors.", "instrument.symbol")
        if inst.exchange is None:
            c.add("LISTING_VENUE_UNSPECIFIED", FindingKind.AMBIGUOUS,
                  "A bare ticker without a listing venue can denote different securities on different "
                  "markets; no venue is chosen.", "instrument.exchange")
        extras = [name for name in ("expiry", "strike", "option_right", "futures_contract")
                  if getattr(inst, name) is not None]
        if extras:
            c.add("DERIVATIVE_FIELDS_ON_CASH_INSTRUMENT", FindingKind.MISMATCH,
                  f"{cls.value} carries derivative fields {', '.join(extras)}; the identifiers contradict the "
                  "asset class.", "instrument")
        return

    if cls in UNDATED_INSTRUMENTS:
        if inst.exchange is None:
            c.add("LISTING_VENUE_UNSPECIFIED", FindingKind.AMBIGUOUS,
                  "The venue is part of the instrument's identity and is not stated.",
                  "instrument.exchange")
        if cls == AssetClass.INDEX:
            c.add("INDEX_NOT_ORDERABLE", FindingKind.UNSUPPORTED,
                  "An index is a reference series: it can be quoted and used in a broker-side "
                  "condition, never ordered.", "instrument.asset_class")
        return

    if inst.underlying is None:
        c.add("UNDERLYING_MISSING", FindingKind.MISSING, f"{cls.value} states no underlying.",
              "instrument.underlying")
    if inst.exchange is None:
        kind = FindingKind.MISSING if cls == AssetClass.FUTURE else FindingKind.AMBIGUOUS
        c.add("FUTURES_VENUE_MISSING" if cls == AssetClass.FUTURE else "LISTING_VENUE_UNSPECIFIED", kind,
              "The venue is part of a derivative's identity and is not stated.", "instrument.exchange")
    expiry = _iso_date(inst.expiry)
    if inst.expiry is None:
        c.add("OPTION_EXPIRY_MISSING" if cls == AssetClass.OPTION else "FUTURES_EXPIRY_MISSING",
              FindingKind.MISSING, f"{cls.value} states no expiry / last trade date.", "instrument.expiry")
    elif expiry is None:
        c.add("EXPIRY_FORMAT_INVALID", FindingKind.INVALID, f"Expiry '{inst.expiry}' is not an ISO date.",
              "instrument.expiry")
    elif as_of is not None and expiry < as_of:
        c.add("CONTRACT_EXPIRED_AT_AS_OF", FindingKind.INVALID,
              f"Expiry {expiry.isoformat()} is before the record's as_of date {as_of.isoformat()}.",
              "instrument.expiry")

    if cls == AssetClass.OPTION:
        if inst.strike is None:
            c.add("OPTION_STRIKE_MISSING", FindingKind.MISSING, "The option states no strike.", "instrument.strike")
        else:
            _, problem = _positive(inst.strike)
            if problem:
                c.add("OPTION_STRIKE_INVALID", FindingKind.INVALID, f"Strike '{inst.strike}' is {problem}.",
                      "instrument.strike")
        if inst.option_right is None:
            c.add("OPTION_RIGHT_MISSING", FindingKind.MISSING, "The option states neither CALL nor PUT.",
                  "instrument.option_right")
        if spec is not None:
            if spec.exercise_style is None:
                c.add("OPTION_EXERCISE_STYLE_MISSING", FindingKind.MISSING,
                      "Exercise style (AMERICAN/EUROPEAN) is not stated.", "contract.exercise_style")
            if spec.settlement is None:
                c.add("OPTION_SETTLEMENT_MISSING", FindingKind.MISSING,
                      "Settlement (CASH/PHYSICAL) is not stated.", "contract.settlement")
            if spec.settlement != "PHYSICAL" and spec.settlement_time is None:
                c.add("OPTION_SETTLEMENT_TIME_UNSPECIFIED", FindingKind.AMBIGUOUS,
                      "Cash-settled option series with AM and PM settlement are distinct listings; the record "
                      "does not say which.", "contract.settlement_time")
        return

    # FUTURE
    if inst.strike is not None or inst.option_right is not None:
        c.add("OPTION_FIELDS_ON_FUTURE", FindingKind.MISMATCH,
              "A future carries strike/option_right; the identifiers contradict the asset class.", "instrument")
    month = _contract_month(spec.contract_month if spec else None, c)
    if inst.futures_contract is None:
        c.add("FUTURES_CONTRACT_CODE_MISSING", FindingKind.MISSING,
              "No dated contract code is stated; a product root alone does not identify a contract.",
              "instrument.futures_contract")
    else:
        _futures_code_rules(inst.futures_contract, month, c)
    if spec is not None:
        if spec.contract_month is None:
            c.add("FUTURES_CONTRACT_MONTH_MISSING", FindingKind.MISSING, "No contract month is stated.",
                  "contract.contract_month")
        if spec.settlement is None:
            c.add("FUTURES_SETTLEMENT_MISSING", FindingKind.MISSING,
                  "Settlement (CASH/PHYSICAL) is not stated.", "contract.settlement")


def _contract_month(text: str | None, c: _Collector) -> tuple[int, int] | None:
    if text is None:
        return None
    match = CONTRACT_MONTH_RE.fullmatch(text)
    if match is None:
        c.add("FUTURES_CONTRACT_MONTH_FORMAT_INVALID", FindingKind.INVALID,
              f"Contract month '{text}' is not YYYY-MM.", "contract.contract_month")
        return None
    return int(match.group(1)), int(match.group(2))


def _futures_code_rules(code: str, month: tuple[int, int] | None, c: _Collector) -> None:
    match = FUTURES_CODE_RE.fullmatch(code.strip().upper())
    if match is None:
        c.add("FUTURES_CONTRACT_CODE_FORMAT_UNRECOGNIZED", FindingKind.AMBIGUOUS,
              f"Contract code '{code}' is not in root+month-letter+year form; its month and year cannot be "
              "read and no venue-specific format is assumed.", "instrument.futures_contract")
        return
    _root, letter, year_digits = match.groups()
    if len(year_digits) == 1:
        c.add("FUTURES_YEAR_DIGIT_AMBIGUOUS", FindingKind.AMBIGUOUS,
              f"Contract code '{code}' uses a single year digit, which repeats every decade.",
              "instrument.futures_contract")
    if month is None:
        return
    year, month_number = month
    code_month = MONTH_LETTERS.index(letter) + 1
    year_matches = (year % 10 if len(year_digits) == 1 else year % 100) == int(year_digits)
    if code_month != month_number or not year_matches:
        c.add("FUTURES_CONTRACT_MONTH_CONFLICT", FindingKind.MISMATCH,
              f"Contract code '{code}' reads as month {code_month:02d}/year ...{year_digits} but contract_month "
              f"says {year:04d}-{month_number:02d}.", "instrument.futures_contract")


def _runtime_rules(r: MappingRecord, c: _Collector) -> RuntimeScope:
    inst = r.instrument
    if inst is None:
        return RuntimeScope.UNDETERMINED
    if inst.asset_class in DERIVATIVES | UNDATED_INSTRUMENTS:
        c.add("RUNTIME_ASSET_CLASS_UNSUPPORTED", FindingKind.UNSUPPORTED,
              f"{inst.asset_class.value} cannot be acted on by the current paper adapter: the gateway holds it as "
              "ASSET_CLASS_PAPER_ADAPTER_UNSUPPORTED however complete the contract metadata is.",
              "instrument.asset_class")
        return RuntimeScope.ASSET_CLASS_UNSUPPORTED_IN_CURRENT_RUNTIME
    return RuntimeScope.EQUITY_ETF_GATEWAY_CHECKS_STILL_APPLY


# ----------------------------------------------------------------------------- entry points

def diagnose_record(record: MappingRecord) -> RecordDiagnostic:
    """Evaluate every rule against one record. Pure: same record, same findings, same order.

    Whitespace-only strings are treated as not stated (and said so) before the rules run, and every
    numeric field is range-checked before arithmetic, so a single record can neither hide a gap nor
    abort the batch."""
    record, blanks = _normalise(record)
    c = _Collector()
    if blanks:
        c.add("BLANK_FIELDS_TREATED_AS_ABSENT", FindingKind.INFO,
              f"Whitespace-only values were treated as not stated: {', '.join(blanks)}.")
    identity = _identity_rules(record, c)
    _unit_currency_rules(record, c)
    _economics_rules(record, c)
    _contract_rules(record, c)
    scope = _runtime_rules(record, c)
    counts = {kind.value: 0 for kind in FindingKind}
    for finding in c.findings:
        counts[finding.kind.value] += 1
    headline = next((kind for kind in HEADLINE_ORDER if counts[kind.value]), FindingKind.INFO)
    return RecordDiagnostic(
        record_id=record.record_id,
        asset_class=record.instrument.asset_class if record.instrument else None,
        identity=identity, runtime_scope=scope, headline=headline, counts=counts, findings=c.findings,
    )


def diagnose_records(records: list[MappingRecord]) -> MappingDiagnosticReport:
    diagnostics = [diagnose_record(r) for r in records]
    totals = {kind.value: 0 for kind in FindingKind}
    headlines = {kind.value: 0 for kind in FindingKind}
    for d in diagnostics:
        headlines[d.headline.value] += 1
        for kind, n in d.counts.items():
            totals[kind] += n
    return MappingDiagnosticReport(record_count=len(diagnostics), totals=totals, headlines=headlines,
                                   records=diagnostics)


def load_records(path: Path) -> list[MappingRecord]:
    """Read a JSON list of records, or an object with a ``records`` list. Unmarked input is refused
    by validation (``synthetic`` must be true). Raises ValueError on malformed input."""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot read mapping records from {path}: {exc}") from exc
    if isinstance(payload, dict):
        payload = payload.get("records")
    if not isinstance(payload, list):
        raise ValueError(f"{path}: expected a JSON list of records or an object with a 'records' list")  # noqa: TRY004
    try:
        return [MappingRecord.model_validate(item) for item in payload]
    except Exception as exc:  # pydantic.ValidationError, kept generic so the CLI can report it
        raise ValueError(f"{path}: invalid mapping record: {exc}") from exc


def render_json(report: MappingDiagnosticReport) -> str:
    return json.dumps(report.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"


def render_text(report: MappingDiagnosticReport, *, summary: bool = False) -> str:
    """Human-readable report. ``summary`` gives one line per record (headline, identity, runtime, codes)."""
    lines = [f"INSTRUMENT MAPPING DIAGNOSTIC (rules v{report.rules_version}) - {report.authority}",
             report.notice,
             f"records={report.record_count}  findings: " + "  ".join(
                 f"{kind.value}={report.totals[kind.value]}" for kind in FindingKind),
             ""]
    for d in report.records:
        cls = d.asset_class.value if d.asset_class else "-"
        head = (f"[{d.record_id}] {cls}  identity={d.identity.value}  headline={d.headline.value}  "
                f"runtime={d.runtime_scope.value}")
        if summary:
            lines.append(head + "  " + " ".join(f"{f.kind.value}:{f.code}" for f in d.findings))
            continue
        lines.append(head)
        for f in d.findings:
            where = f"  ({f.field})" if f.field else ""
            lines.append(f"  {f.kind.value:<11} {f.code}{where}")
            lines.append(f"              {f.detail}")
        lines.append("")
    if summary:
        lines.append("")
    lines.append(f"{report.authority}: diagnostics only; no mapping, price, size or order decision was made.")
    return "\n".join(lines) + "\n"
