"""The source-series-to-broker-contract mapping policy.

A publisher quotes levels in some series: an ETF's own price, an index, a spot rate, a futures
contract. An order can only be placed in a broker contract. This module decides, from an explicit
data file, which source assets may reach the broker at all, and as which contract.

Version 2 (``version = 2``, ``FamilyMapping``; see docs/MULTI_ASSET_MAPPING.md) maps each source
asset family to a reference series and an execution instrument with exactly one price-basis
treatment (DIRECT, BASIS_SHIFT, REFERENCE_TRIGGER, UNSUPPORTED), a contract selection rule,
sizing, session, roll and option rules, and an owner approval. Nothing executes unless its entry
is approved: an unapproved entry is marked MAPPING_NOT_APPROVED, an unmapped family
UNSUPPORTED_INSTRUMENT_MAPPING, and both are HELD by the one gateway. Loading fails closed.

Version 1 rules (``version = 1`` files, ``MappingEntry``), kept unchanged:
  * Each entry names the source asset, the broker instrument and the price-basis relationship.
  * Only basis ``DIRECT`` (the publisher's levels are the traded instrument's own price) may
    execute. A ``DIRECT`` entry must name the same symbol on both sides: the gateway checks that
    the quoted level belongs to the orderable contract, and this policy never renames one into
    another.
  * Anything unmapped, or mapped with any other basis (index, spot or futures levels that are not
    the traded instrument's price), is marked ``UNSUPPORTED_INSTRUMENT_MAPPING``. The marker is
    added as an ambiguity, so the one gateway (``validator.gate_action``) HOLDs the action; it
    never reaches the broker. No index or spot level is ever treated as interchangeable with an
    ETF or futures price.
  * The policy only ever restricts. It cannot approve anything, and it never changes a price basis,
    a price, a side or a timing.
"""

from __future__ import annotations

import re
import tomllib
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, StrictBool, ValidationError, field_validator, model_validator

from tradingbot.event_models import ParsedPost, TradeAction
from tradingbot.execution.broker import ContractRef
from tradingbot.models import (
    Action,
    AssetClass,
    BasisTreatment,
    Instrument,
    MarketTerms,
    StrictModel,
)
from tradingbot.runtime.instruction_terms import InstructionTerms, extract_terms
from tradingbot.validator import business_days_until

UNSUPPORTED_INSTRUMENT_MAPPING = "UNSUPPORTED_INSTRUMENT_MAPPING"
MAPPING_NOT_APPROVED = "MAPPING_NOT_APPROVED"
CONTRACT_SELECTION_AMBIGUOUS = "CONTRACT_SELECTION_AMBIGUOUS"
OPTION_TERMS_INCOMPLETE = "OPTION_TERMS_INCOMPLETE"
IDEA_CONTRACT_MISMATCH = "IDEA_CONTRACT_MISMATCH"
# The clause has a month-like or contract-code token that does not resolve to exactly one listed
# contract of the entry (review F2): never replaced by the nearest month.
CONTRACT_MONTH_UNRECOGNISED = "CONTRACT_MONTH_UNRECOGNISED"
# An option instruction with more than one strike, expiry or right, or a strategy word (review F3).
OPTION_STRATEGY_NOT_SINGLE_LEG = "OPTION_STRATEGY_NOT_SINGLE_LEG"


class BasisRelation(str, Enum):
    DIRECT = "DIRECT"  # levels are quoted in the traded instrument's own price
    INDEX_LEVEL = "INDEX_LEVEL"  # levels are index points (e.g. an equity index), not the ETF price
    SPOT_LEVEL = "SPOT_LEVEL"  # levels are a spot rate or commodity price
    FUTURES_LEVEL = "FUTURES_LEVEL"  # levels are a futures contract's price
    PROXY = "PROXY"  # a related instrument whose price differs (ratio, tracking error, roll)
    UNKNOWN = "UNKNOWN"


class MappingEntry(StrictModel):
    source_asset: str = Field(min_length=1, max_length=40)
    broker_symbol: str = Field(min_length=1, max_length=40)
    broker_asset_class: AssetClass
    broker_exchange: str | None = None
    basis: BasisRelation
    note: str = ""

    @model_validator(mode="after")
    def _direct_is_same_symbol(self):
        if self.basis == BasisRelation.DIRECT and self.source_asset != self.broker_symbol:
            raise ValueError(
                f"DIRECT entry {self.source_asset!r} -> {self.broker_symbol!r}: a direct basis "
                "means the publisher quotes the traded instrument itself, so the symbols must match"
            )
        return self

    @property
    def executable(self) -> bool:
        return self.basis == BasisRelation.DIRECT

    def instrument(self) -> Instrument:
        return Instrument(
            asset_class=self.broker_asset_class,
            symbol=self.broker_symbol,
            exchange=self.broker_exchange,
        )


class MappingDecision(StrictModel):
    action_id: str
    # NOT_APPROVED: a version-2 entry exists and is not approved. UNRESOLVED: the entry is approved
    # but the instruction does not select one contract (month, option terms, idea's contract).
    status: Literal["SUPPORTED", "UNSUPPORTED", "NOT_APPROVED", "UNRESOLVED", "NOT_APPLICABLE"]
    source_asset: str | None = None
    broker_symbol: str | None = None
    basis: str | None = None
    reason: str = ""
    # Version 2: the family key (``QSTK.OPT`` for an option family) and whether the gateway must
    # receive a ``MappedQuote`` (False for a stock or ETF quoted in its own price, as in v1).
    family: str | None = None
    mapped: bool = False
    # The family's entry is no longer approved (or no longer present) and this exit or protective
    # amendment uses the terms recorded on its idea when it was opened.
    under_revoked_mapping: bool = False


class MappingFile(StrictModel):
    version: Literal[1] = 1
    mapping: list[MappingEntry] = Field(default_factory=list)


# ============================================================================ version 2

SEC_TYPES = {
    "STK": None,  # EQUITY or ETF: stated by ``asset_class``
    "FUT": AssetClass.FUTURE,
    "OPT": AssetClass.OPTION,
    "CMDTY": AssetClass.COMMODITY,
    "CFD": AssetClass.CFD,
    "IND": AssetClass.INDEX,
}
SYMBOL = r"^[A-Z][A-Z0-9.]{0,9}$"
WINDOW_RE = re.compile(r"^(\d{2}):(\d{2})-(\d{2}):(\d{2})$")
DAYS = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")


def parse_window(text: str) -> tuple[int, int]:
    m = WINDOW_RE.match(text)
    if not m:
        raise ValueError(f"session window {text!r} is not HH:MM-HH:MM")
    h1, m1, h2, m2 = (int(g) for g in m.groups())
    start, end = h1 * 60 + m1, h2 * 60 + m2
    if not (0 <= start < end <= 24 * 60) or m1 > 59 or m2 > 59:
        raise ValueError(f"session window {text!r} must run forward within one day")
    return start, end


class SeriesSpec(StrictModel):
    """A broker series: the reference the publisher's levels are quoted in, or the execution
    instrument. Its identity lets the runtime request a quote of it."""
    sec_type: Literal["STK", "FUT", "OPT", "CMDTY", "CFD", "IND"]
    symbol: str = Field(pattern=SYMBOL)
    exchange: str = Field(min_length=1, max_length=20)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    asset_class: Literal["EQUITY", "ETF"] | None = None  # STK only

    @model_validator(mode="after")
    def _stock_class(self):
        if (self.sec_type == "STK") != (self.asset_class is not None):
            raise ValueError("asset_class (EQUITY or ETF) is stated for STK and only for STK")
        return self

    @property
    def asset(self) -> AssetClass:
        return AssetClass(self.asset_class) if self.asset_class else SEC_TYPES[self.sec_type]

    def same_series(self, other: SeriesSpec) -> bool:
        return (self.sec_type, self.symbol, self.exchange) == (
            other.sec_type, other.symbol, other.exchange)


class ExecutionSpec(SeriesSpec):
    sec_type: Literal["STK", "FUT", "OPT", "CMDTY", "CFD"]
    # Expected contract rules: the broker's resolved contract must agree, otherwise HOLD.
    multiplier: Decimal = Field(default=Decimal(1), gt=0)
    min_tick: Decimal = Field(default=Decimal("0.01"), gt=0)
    size_increment: int = Field(default=1, ge=1)


class ContractMonth(StrictModel):
    month: str = Field(pattern=r"^20\d{2}(0[1-9]|1[0-2])$")  # YYYYMM
    # The configured date the roll policy counts back from (for example the broker's close-out
    # or the first notice day); never inferred.
    roll_date: date


class ContractSelection(StrictModel):
    # EXPLICIT_ONLY: the instruction must name the month. EXPLICIT_OR_NEAREST: a named month, else
    # the nearest listed month with at least ``min_business_days_to_roll`` before its roll date.
    rule: Literal["EXPLICIT_ONLY", "EXPLICIT_OR_NEAREST"]
    min_business_days_to_roll: int = Field(ge=1)


class Sizing(StrictModel):
    units_per_signal: int = Field(ge=1)  # contracts (or shares, units) per OPEN / ADD
    max_units: int = Field(ge=1)  # per idea
    max_notional: Decimal = Field(gt=0)  # per idea: price x units x multiplier, USD

    @model_validator(mode="after")
    def _fits(self):
        if self.units_per_signal > self.max_units:
            raise ValueError("units_per_signal exceeds max_units")
        return self


class Session(StrictModel):
    policy: Literal["REGULAR_ONLY", "EXTENDED"]
    timezone: str
    days: list[Literal["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]] = Field(min_length=1)
    regular: list[str] = Field(min_length=1)
    extended: list[str] = Field(default_factory=list)

    @field_validator("timezone")
    @classmethod
    def _tz(cls, v):
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError(f"unknown timezone {v!r}") from None
        return v

    @model_validator(mode="after")
    def _windows(self):
        for w in (*self.regular, *self.extended):
            parse_window(w)
        if self.policy == "EXTENDED" and not self.extended:
            raise ValueError("an EXTENDED session policy needs extended windows")
        if self.policy == "REGULAR_ONLY" and self.extended:
            raise ValueError("extended windows are listed but the policy is REGULAR_ONLY")
        return self

    def permitted(self) -> list[tuple[int, int]]:
        return [parse_window(w) for w in (*self.regular, *self.extended)]

    def day_numbers(self) -> list[int]:
        return [DAYS.index(d) for d in self.days]


class Roll(StrictModel):
    # Flatten (or roll by a new, approved instruction) this many business days before the
    # contract's roll date. Only FLATTEN exists: the service flags the position; it never sends an
    # order the gateway did not approve from a publisher action.
    business_days_before: int = Field(ge=1)
    action: Literal["FLATTEN"]
    holidays: list[date] = Field(default_factory=list)


class BasisShiftRules(StrictModel):
    max_abs_basis: Decimal = Field(gt=0)  # execution prices
    max_age_seconds: float = Field(gt=0, le=60)  # each quote's age and their skew
    # Execution price per reference price. The basis is a difference, so the two series must be
    # on the same price scale: exactly 1 is the only supported value (a ratio, such as an ETF
    # against its index, is UNSUPPORTED for now). Required, so the owner states it.
    scale_ratio: Decimal

    @field_validator("scale_ratio")
    @classmethod
    def _same_scale(cls, v):
        if v != 1:
            raise ValueError(f"scale_ratio {v}: only series on the same price scale (scale_ratio = "
                             "1) can be BASIS_SHIFT; any other ratio is UNSUPPORTED")
        return v


class ReferenceTriggerRules(StrictModel):
    released_order_type: Literal["MKT"]
    # When the reference series updates (its own session), in the session's timezone and days.
    # Outside these windows a condition on it cannot trigger.
    reference_hours: list[str] = Field(min_length=1)
    # Also place a BASIS_SHIFT stop in the execution instrument (needs [mapping.basis_shift]).
    require_backstop: bool

    @model_validator(mode="after")
    def _hours(self):
        for w in self.reference_hours:
            parse_window(w)
        return self


class FamilyMapping(StrictModel):
    """One source asset family (version 2). See docs/MULTI_ASSET_MAPPING.md."""
    family: str = Field(pattern=r"^[A-Z][A-Z0-9]{0,9}(\.OPT)?$")
    basis: BasisTreatment
    # The TOML boolean true only: "yes", 1 or "true" refuse to load (review F11). An approval also
    # names its approver and a timezone-aware time that is not in the future.
    approved: StrictBool = False
    approved_by: str | None = None
    approved_at: datetime | None = Field(default=None, strict=True)
    note: str = ""
    # DIRECT only: the reference and execution identities differ but are one price series (for
    # example one ETF listed on two venues). Needs a stated reason.
    same_series: StrictBool = False
    same_series_reason: str = ""
    reference: SeriesSpec | None = None
    execution: ExecutionSpec | None = None
    contract_selection: ContractSelection | None = None
    contracts: list[ContractMonth] = Field(default_factory=list)
    sizing: Sizing | None = None
    session: Session | None = None
    roll: Roll | None = None
    basis_shift: BasisShiftRules | None = None
    reference_trigger: ReferenceTriggerRules | None = None

    @model_validator(mode="after")
    def _consistent(self):
        def bad(msg: str):
            raise ValueError(f"family {self.family}: {msg}")

        if self.approved and (not (self.approved_by or "").strip() or self.approved_at is None):
            bad("an approved entry states approved_by and approved_at")
        if self.approved_at is not None and (
            self.approved_at.tzinfo is None or self.approved_at > datetime.now(UTC)
        ):
            bad("approved_at must be a past date-time with its UTC offset")
        if not self.approved and (self.approved_by or self.approved_at):
            bad("approved_by / approved_at are set but approved = false")
        if self.basis == "UNSUPPORTED":
            if self.approved:
                bad("an UNSUPPORTED entry cannot be approved")
            return self
        if self.reference is None or self.execution is None:
            bad("reference and execution are required")
        x, ref = self.execution, self.reference
        legacy = self.legacy_equivalent
        if not legacy and (self.sizing is None or self.session is None):
            bad("sizing and session are required (only a stock quoted in its own price, with "
                "neither, keeps the version-1 behaviour)")
        if legacy and (self.session or self.roll or self.contracts):
            bad("a stock entry without sizing takes no session, roll or contracts")
        if self.approved and x.currency != "USD":
            bad("caps are in USD and no FX conversion exists: only USD execution may be approved")
        if self.same_series and self.basis != "DIRECT":
            bad("same_series is only for DIRECT")
        if self.same_series and not self.same_series_reason.strip():
            bad("same_series = true needs a same_series_reason")
        # A stock or ETF is never priced from an index, a spot rate or a CFD: their scales differ
        # (review F10). Only REFERENCE_TRIGGER without a backstop keeps its levels in the
        # reference series and never converts them.
        cross_scale = x.sec_type == "STK" and ref.sec_type in ("IND", "CMDTY", "CFD")
        if cross_scale and (self.basis in ("DIRECT", "BASIS_SHIFT") or self.basis_shift):
            bad(f"a {ref.sec_type} reference and a STK execution are not on one price scale")
        if self.basis == "DIRECT" and not self.same_series:
            if not ref.same_series(x):
                bad("DIRECT means the levels are the execution instrument's own prices: the "
                    "reference must be the execution series (or declare same_series = true "
                    "with a same_series_reason)")
            if x.sec_type == "STK" and self.family != x.symbol:
                bad(f"DIRECT family {self.family} is not the same price series as the stock "
                    f"{x.symbol} it executes (declare same_series = true with a reason if it is)")
        if self.basis in ("BASIS_SHIFT", "REFERENCE_TRIGGER") and ref.same_series(x):
            bad(f"{self.basis} needs a reference series other than the execution instrument")
        if ref.sec_type == "FUT" and self.basis != "DIRECT":
            bad("a futures reference is supported only as DIRECT (its month is the execution's)")
        if self.basis != "REFERENCE_TRIGGER" and (
            (self.basis == "BASIS_SHIFT") != (self.basis_shift is not None)
        ):
            bad("[basis_shift] is required for BASIS_SHIFT and only used by it or a backstop")
        if self.basis == "REFERENCE_TRIGGER":
            if self.reference_trigger is None:
                bad("[reference_trigger] is required for REFERENCE_TRIGGER")
            if self.reference_trigger.require_backstop != (self.basis_shift is not None):
                bad("require_backstop needs [basis_shift], and [basis_shift] needs a backstop")
            if self.session is None:
                bad("REFERENCE_TRIGGER needs a session (its reference hours use its timezone)")
        elif self.reference_trigger is not None:
            bad("[reference_trigger] is only used by REFERENCE_TRIGGER")
        if x.sec_type == "FUT":
            if self.contract_selection is None or self.roll is None or not self.contracts:
                bad("a futures entry needs contract_selection, roll and contracts")
            months = [c.month for c in self.contracts]
            if len(set(months)) != len(months):
                bad("a contract month is listed twice")
            if self.contract_selection.min_business_days_to_roll <= self.roll.business_days_before:
                bad("the selection rule would pick a contract already inside its roll window")
        elif self.contract_selection or self.contracts or self.roll:
            bad("contract_selection, contracts and roll apply to futures only")
        if x.sec_type == "OPT":
            if not self.family.endswith(".OPT"):
                bad("an option family is named <UNDERLYING>.OPT")
            if self.basis != "DIRECT":
                bad("an option is executed only on its own premium (DIRECT)")
            if x.symbol != self.family.removesuffix(".OPT"):
                bad("the option's symbol is its underlying, the family's asset")
        elif self.family.endswith(".OPT"):
            bad("a .OPT family executes an option (sec_type OPT)")
        return self

    # ---------------------------------------------------------------- views

    @property
    def legacy_equivalent(self) -> bool:
        """A stock or ETF quoted in its own price with no sizing or session: exactly the v1
        DIRECT behaviour (MockPolicy sizing, the gateway's stock path, a plain quote)."""
        x = self.execution
        return (
            self.basis == "DIRECT" and x is not None and x.sec_type == "STK"
            and self.family == x.symbol and self.sizing is None
        )

    # Version-1 attribute names, so existing callers read a v2 entry unchanged.
    @property
    def source_asset(self) -> str:
        return self.family

    @property
    def broker_symbol(self) -> str | None:
        return self.execution.symbol if self.execution else None

    @property
    def broker_asset_class(self) -> AssetClass | None:
        return self.execution.asset if self.execution else None

    @property
    def executable(self) -> bool:
        return self.approved and self.basis != "UNSUPPORTED"

    @property
    def status_label(self) -> str:
        if self.basis == "UNSUPPORTED":
            return "UNSUPPORTED"
        return "APPROVED" if self.approved else "NOT APPROVED"

    def execution_label(self) -> str:
        x = self.execution
        if x is None:
            return "-"
        return f"{x.symbol} {x.sec_type} {x.exchange} x{format(x.multiplier.normalize(), 'f')}"

    def reference_instrument(self) -> Instrument | None:
        r = self.reference
        if r is None:
            return None
        return Instrument(asset_class=r.asset, symbol=r.symbol, exchange=r.exchange)

    def terms(
        self,
        instrument: Instrument,
        contract: ContractRef | None,
        reference: ContractRef | None,
        problems: list[str] = (),
        *,
        exits_only: bool = False,
    ) -> MarketTerms:
        """The terms the gateway decides with, for one selected execution instrument."""
        x = self.execution
        out = list(problems)
        roll_date = None
        if contract is not None:
            for name, want, have in (
                ("MULTIPLIER", x.multiplier, contract.multiplier),
                ("MIN_TICK", x.min_tick, contract.min_tick),
                ("SIZE_INCREMENT", x.size_increment, contract.size_increment),
            ):
                if want != have:
                    out.append(f"CONTRACT_SPEC_MISMATCH:{name}:{want}!={have}")
            if contract.symbol != x.symbol or contract.asset_class != x.asset:
                out.append("CONTRACT_SPEC_MISMATCH:IDENTITY")
        else:
            out.append("CONTRACT_UNRESOLVED")
        if x.sec_type == "FUT" and instrument.futures_contract:
            roll_date = next(
                (c.roll_date for c in self.contracts if c.month == instrument.futures_contract),
                None,
            )
        if self.basis in ("BASIS_SHIFT", "REFERENCE_TRIGGER") and reference is None:
            out.append("REFERENCE_SERIES_UNRESOLVED")
        s = self.session
        sizing = self.sizing
        rt = self.reference_trigger
        return MarketTerms(
            family=self.family,
            approved=self.approved,
            basis=self.basis,
            execution_symbol=x.symbol,
            execution_key=instrument.quote_key,
            execution_contract_key=contract.contract_key if contract else None,
            asset_class=x.asset,
            multiplier=contract.multiplier if contract else x.multiplier,
            min_tick=contract.min_tick if contract else x.min_tick,
            size_increment=contract.size_increment if contract else x.size_increment,
            units_per_signal=sizing.units_per_signal,
            max_units=sizing.max_units,
            max_notional=sizing.max_notional,
            allow_short=x.sec_type != "OPT",
            session_timezone=s.timezone,
            session_days=s.day_numbers(),
            permitted_windows=(
                [parse_window(w) for w in s.regular] if s.policy == "REGULAR_ONLY"
                else s.permitted()
            ),
            roll_date=roll_date,
            roll_business_days=self.roll.business_days_before if self.roll else None,
            holidays=list(self.roll.holidays) if self.roll else [],
            reference_contract_key=reference.contract_key if reference else None,
            reference_contract=reference.model_dump_json() if reference else None,
            reference_windows=[parse_window(w) for w in rt.reference_hours] if rt else [],
            max_abs_basis=self.basis_shift.max_abs_basis if self.basis_shift else None,
            max_basis_age_seconds=self.basis_shift.max_age_seconds if self.basis_shift else None,
            require_backstop=bool(rt and rt.require_backstop),
            exits_only=exits_only,
            same_series=self.same_series,
            problems=out,
        )

    # ---------------------------------------------------------------- selection

    def select(
        self,
        action: TradeAction,
        asset: str,
        terms: InstructionTerms,
        today: date,
        held: ContractRef | None,
    ) -> tuple[Instrument | None, list[str]]:
        """The execution instrument for this action, or the reasons it cannot be selected."""
        x = self.execution
        if x.sec_type == "STK":
            exchange = None if x.exchange == "SMART" else x.exchange  # v1: SMART is the default
            return Instrument(asset_class=x.asset, symbol=x.symbol, exchange=exchange), []
        if x.sec_type in ("CMDTY", "CFD"):
            return Instrument(asset_class=x.asset, symbol=x.symbol, exchange=x.exchange), []
        if x.sec_type == "FUT":
            return self._select_future(terms, today, held)
        return self._select_option(asset, terms, held)

    def _named_months(self, terms: InstructionTerms, today: date):
        """The contract months the clause names, or the reasons it names something that does not
        resolve to a listed contract of this entry. Anything month-like that is not read exactly
        HOLDs (CONTRACT_MONTH_UNRECOGNISED): it is never replaced by the nearest month."""
        x = self.execution
        unread = [*terms.unrecognised_months]
        named = set(terms.months) | {f"{d.year}{d.month:02d}" for d in terms.expiries}
        for token, root, month, digits in terms.month_codes:
            if root and root != x.symbol:
                unread.append(token)
                continue
            if len(digits) == 2:
                year = 2000 + int(digits)
            else:  # one digit: the first year from this one that ends in it
                year = next(y for y in range(today.year, today.year + 10) if y % 10 == int(digits))
            named.add(f"{year}{month:02d}")
        codes = {t for t, _, _, _ in terms.month_codes}
        unread += [w for w in terms.upper_words
                   if w.startswith(x.symbol) and w != x.symbol and w not in codes]
        if unread:
            return None, [CONTRACT_MONTH_UNRECOGNISED, *(f"MONTH_TOKEN:{t}" for t in unread)]
        for n in terms.bare_months:
            matches = {c.month for c in self.contracts if int(c.month[4:]) == n}
            if len(matches) != 1:
                return None, [CONTRACT_MONTH_UNRECOGNISED, f"MONTH_{n:02d}_MATCHES_{len(matches)}"]
            named |= matches
        listed = {c.month for c in self.contracts}
        if len(named) > 1:
            return None, [CONTRACT_MONTH_UNRECOGNISED, "SEVERAL_MONTHS_NAMED"]
        if named and not named <= listed:
            return None, [CONTRACT_MONTH_UNRECOGNISED, f"MONTH_NOT_LISTED:{min(named)}"]
        return named, []

    def _select_future(self, terms, today, held):
        x = self.execution
        named, problems = self._named_months(terms, today)
        if problems:
            return None, problems
        if held is not None:  # a follow-up acts on the idea's own contract
            month = (held.expiry or "")[:6]
            if named and named != {month}:
                return None, [IDEA_CONTRACT_MISMATCH, f"IDEA_MONTH:{month}"]
        elif named:
            month = named.pop()
        elif self.contract_selection.rule == "EXPLICIT_ONLY":
            return None, [CONTRACT_SELECTION_AMBIGUOUS, "CONTRACT_MONTH_NOT_STATED"]
        else:
            need = self.contract_selection.min_business_days_to_roll
            holidays = self.roll.holidays
            eligible = sorted(
                c.month for c in self.contracts
                if business_days_until(today, c.roll_date, holidays) >= need
            )
            if not eligible:
                return None, [CONTRACT_SELECTION_AMBIGUOUS, "NO_LISTED_MONTH_OUTSIDE_ROLL_WINDOW"]
            month = eligible[0]
        return Instrument(
            asset_class=AssetClass.FUTURE, symbol=x.symbol, exchange=x.exchange,
            futures_contract=month,
        ), []

    def _select_option(self, asset, terms, held):
        x = self.execution
        if terms.multi_leg:  # only one underlying, expiry, strike and right is a single leg
            return None, [OPTION_STRATEGY_NOT_SINGLE_LEG, *terms.multi_leg]
        if held is not None:
            right = {"C": "CALL", "P": "PUT"}.get(held.right or "")
            expiry = f"{held.expiry[:4]}-{held.expiry[4:6]}-{held.expiry[6:8]}" if held.expiry \
                else None
            stated = (
                (terms.expiries and [d.isoformat() for d in terms.expiries] != [expiry])
                or (terms.strikes and terms.strikes != [held.strike])
                or (terms.rights and terms.rights != [right])
            )
            if stated or held.strike is None or expiry is None:
                return None, [IDEA_CONTRACT_MISMATCH]
            strike = format(held.strike.normalize(), "f")
            return Instrument(asset_class=AssetClass.OPTION, symbol=x.symbol, underlying=asset,
                              expiry=expiry, strike=strike, option_right=right,
                              exchange=x.exchange), []
        missing = [
            name for name, values in (
                ("UNDERLYING", [asset] if asset == x.symbol else []),
                ("EXPIRY", terms.expiries), ("STRIKE", terms.strikes), ("RIGHT", terms.rights),
            ) if len(values) != 1
        ]
        if missing:
            return None, [OPTION_TERMS_INCOMPLETE, *(f"OPTION_{m}_NOT_EXPLICIT" for m in missing)]
        return Instrument(
            asset_class=AssetClass.OPTION, symbol=x.symbol, underlying=asset,
            expiry=terms.expiries[0].isoformat(), strike=format(terms.strikes[0].normalize(), "f"),
            option_right=terms.rights[0], exchange=x.exchange,
        ), []


class MappingFileV2(StrictModel):
    version: Literal[2]
    mapping: list[FamilyMapping] = Field(default_factory=list)


# ============================================================================ the policy


class MappingPolicy:
    def __init__(self, entries: list[MappingEntry | FamilyMapping]):
        seen: dict[str, MappingEntry | FamilyMapping] = {}
        for e in entries:
            if e.source_asset in seen:
                raise ValueError(f"duplicate mapping for source asset {e.source_asset!r}")
            seen[e.source_asset] = e
        self.entries = seen

    @classmethod
    def from_toml(cls, path: str | Path) -> MappingPolicy:
        p = Path(path)
        try:
            raw = tomllib.loads(p.read_text(encoding="utf-8"))
            if raw.get("version") == 2:
                return cls(MappingFileV2.model_validate(raw).mapping)
            return cls(MappingFile.model_validate(raw).mapping)
        except FileNotFoundError:
            raise ValueError(f"instrument mapping file not found: {p}") from None
        except (tomllib.TOMLDecodeError, ValidationError) as exc:
            raise ValueError(f"invalid instrument mapping file {p}: {exc}") from None

    @property
    def executable_assets(self) -> list[str]:
        return sorted(a for a, e in self.entries.items() if e.executable)

    def entry_for_contract(self, contract: ContractRef) -> FamilyMapping | None:
        """The version-2 entry whose execution instrument this contract is (roll monitoring)."""
        for e in self.entries.values():
            x = getattr(e, "execution", None)
            if isinstance(e, FamilyMapping) and x is not None and x.symbol == contract.symbol \
                    and x.asset == contract.asset_class:
                return e
        return None

    def decide(self, action: TradeAction, **kwargs) -> MappingDecision:
        return self.resolve(action, **kwargs)[0]

    def resolve(
        self,
        action: TradeAction,
        *,
        clause: str | None = None,
        now: datetime | None = None,
        held: dict[str, ContractRef] | None = None,
        recorded: dict[str, FamilyMapping] | None = None,
    ) -> tuple[MappingDecision, Instrument | None]:
        """One action's mapping decision and, when SUPPORTED, its execution instrument.
        ``clause`` is the instruction text the action came from (contract terms are read from it);
        ``held`` maps follower idea ids to their contracts, so a follow-up acts on its own;
        ``recorded`` maps them to the entry each was opened under. Approval governs new exposure
        only: a REDUCE, CLOSE or MODIFY on an open idea whose family is no longer approved (or no
        longer has an entry) uses its recorded entry, marked exits-only."""
        if action.action == Action.NO_TRADE:
            return MappingDecision(action_id=action.action_id, status="NOT_APPLICABLE"), None
        asset = action.source_market_symbol or (
            action.instrument.symbol if action.instrument is not None else None
        )
        if asset is None:
            return MappingDecision(
                action_id=action.action_id, status="UNSUPPORTED", reason="SOURCE_ASSET_UNRESOLVED"
            ), None
        terms = extract_terms(clause)
        follow_up = action.action in (Action.ADD, Action.REDUCE, Action.CLOSE, Action.MODIFY)
        idea = action.idea_reference if follow_up else None
        held_contract = (held or {}).get(idea) if idea else None
        idea_entry = (recorded or {}).get(idea) if idea else None
        if not follow_up:
            # Option routing applies only to an instruction that opens a position: one that names
            # options (even without complete terms) looks up the option family and never the stock.
            family = f"{asset}.OPT" if terms.option_language else asset
        elif idea_entry is not None and idea_entry.family.removesuffix(".OPT") == asset:
            family = idea_entry.family  # a follow-up binds to its idea's own family
        elif held_contract is not None and held_contract.asset_class == AssetClass.OPTION:
            family = f"{asset}.OPT"
        elif held_contract is not None or asset in self.entries or not terms.option_language:
            family = asset  # "call it a day" in a stock exit is not an option instruction
        else:
            # No idea on record and no stock entry: the option family names why it HOLDs (the
            # gateway then finds no open idea either way).
            family = f"{asset}.OPT"
        entry = self.entries.get(family)
        revoked = False
        if idea_entry is not None and action.action != Action.ADD \
                and idea_entry.family == family and (entry is None or not entry.executable):
            entry, revoked = idea_entry, True
        if entry is None:
            return MappingDecision(
                action_id=action.action_id,
                status="UNSUPPORTED",
                source_asset=asset,
                family=family,
                reason="SOURCE_ASSET_NOT_MAPPED",
            ), None
        if isinstance(entry, MappingEntry):
            d = self._decide_v1(action, asset, entry)
            return d, entry.instrument() if d.status == "SUPPORTED" else None
        out = {
            "action_id": action.action_id, "source_asset": asset, "family": family,
            "broker_symbol": entry.broker_symbol, "basis": entry.basis,
        }
        if entry.basis == "UNSUPPORTED":
            return MappingDecision(status="UNSUPPORTED", reason="BASIS_UNSUPPORTED", **out), None
        if not entry.approved:
            return MappingDecision(status="NOT_APPROVED", reason=MAPPING_NOT_APPROVED, **out), None
        if action.instrument is not None and action.instrument.symbol not in (
            asset, entry.execution.symbol
        ):
            return MappingDecision(
                status="UNSUPPORTED", reason="INTERPRETED_INSTRUMENT_DIFFERS_FROM_MAPPING", **out
            ), None
        today = (now or datetime.now(UTC)).astimezone(
            ZoneInfo(entry.session.timezone if entry.session else "UTC")
        ).date()
        instrument, problems = entry.select(action, asset, terms, today, held_contract)
        if instrument is None:
            return MappingDecision(
                status="UNRESOLVED", reason=",".join(problems), **out
            ), None
        return MappingDecision(status="SUPPORTED", mapped=not entry.legacy_equivalent,
                               under_revoked_mapping=revoked, **out), instrument

    def _decide_v1(self, action: TradeAction, asset: str, entry: MappingEntry) -> MappingDecision:
        if not entry.executable:
            return MappingDecision(
                action_id=action.action_id,
                status="UNSUPPORTED",
                source_asset=asset,
                broker_symbol=entry.broker_symbol,
                basis=entry.basis.value,
                reason=f"BASIS_{entry.basis.value}_NOT_EXECUTABLE",
            )
        if action.instrument is not None and action.instrument.symbol != entry.broker_symbol:
            return MappingDecision(
                action_id=action.action_id,
                status="UNSUPPORTED",
                source_asset=asset,
                broker_symbol=entry.broker_symbol,
                basis=entry.basis.value,
                reason="INTERPRETED_INSTRUMENT_DIFFERS_FROM_MAPPING",
            )
        return MappingDecision(
            action_id=action.action_id,
            status="SUPPORTED",
            source_asset=asset,
            broker_symbol=entry.broker_symbol,
            basis=entry.basis.value,
        )

    def apply(
        self,
        parsed: ParsedPost,
        *,
        clauses: dict[str, str] | None = None,
        now: datetime | None = None,
        held: dict[str, ContractRef] | None = None,
        recorded: dict[str, FamilyMapping] | None = None,
    ) -> tuple[ParsedPost, list[MappingDecision]]:
        """Return the post with every action that may not execute marked for HOLD, and one
        decision per action. A supported action gets its execution instrument. ``clauses`` maps
        action ids to their instruction text (contract months and option terms are read there)."""
        actions: list[TradeAction] = []
        decisions: list[MappingDecision] = []
        for a in parsed.actions:
            d, instrument = self.resolve(
                a, clause=(clauses or {}).get(a.action_id), now=now, held=held, recorded=recorded
            )
            decisions.append(d)
            marks: list[str] = []
            if d.status == "UNSUPPORTED":
                marks = [UNSUPPORTED_INSTRUMENT_MAPPING, f"MAPPING_{d.reason}"]
            elif d.status == "NOT_APPROVED":
                marks = [MAPPING_NOT_APPROVED, f"MAPPING_FAMILY:{d.family}"]
            elif d.status == "UNRESOLVED":
                marks = d.reason.split(",")
            if marks:
                a = a.model_copy(
                    update={
                        "ambiguities": [
                            *a.ambiguities,
                            *(m for m in marks if m not in a.ambiguities),
                        ]
                    }
                )
            elif d.status == "SUPPORTED":
                a = a.model_copy(update={"instrument": instrument})
            actions.append(a)
        return ParsedPost(actions=actions, summary=parsed.summary), decisions
