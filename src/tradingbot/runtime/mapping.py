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
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import Enum
from itertools import pairwise
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    Field,
    PrivateAttr,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    ValidationError,
    field_validator,
    model_validator,
)

from tradingbot.event_models import ParsedPost, Timing, TradeAction
from tradingbot.execution.broker import ContractRef
from tradingbot.models import (
    Action,
    AssetClass,
    BasisTreatment,
    EntryRule,
    Instrument,
    MarketTerms,
    PositionSide,
    StrictModel,
)
from tradingbot.runtime.entry_text import (
    ascii_shadow,
    entry_text_placeholder,
    entry_text_problem,
    has_non_ascii_word,
    restore,
)
from tradingbot.runtime.instruction_terms import (
    ACTION_SEMANTIC_WORDS,
    ADD_WORDS,
    ADDITIVE_PHRASES,
    ADDITIVE_WORDS,
    INSTRUMENT_NOUNS,
    MARKET_NOUNS,
    MENTION_ACTION_PHRASES,
    MENTION_ACTION_WORDS,
    MONTH_WORD_RE,
    PRESENT_MARKERS,
    SIDE_WORDS,
    AnchorLevelKind,
    InstructionTerms,
    anchor_not_current,
    check_phrase,
    clause_kind,
    clause_numbers,
    code_shaped,
    context_problem,
    extract_terms,
    first_word_mention,
    foreign_words,
    has_phrase,
    instruction_word_in,
    leftover_tokens,
    level_kind_of_words,
    level_places,
    marked_numbers,
    post_multi_leg,
    price_like_level,
    protective_sentence_problem,
    protective_unknown_words,
    quote_clause_words,
    relative_contract_words,
    sentences,
    stop_values,
    understood_words,
    unknown_tokens,
    unknown_words,
    unresolved_market_mentions,
    words_with_parts,
)
from tradingbot.validator import (
    ANCHOR_MAX_ADVERSE_PCT_CEILING,
    ANCHOR_MAX_FAVOURABLE_PCT_CEILING,
    ANCHOR_MAX_QUOTE_AGE_CEILING_SECONDS,
    ANCHOR_MAX_SIGNAL_AGE_CEILING_SECONDS,
    ANCHOR_MAX_STOP_DISTANCE_PCT_CEILING,
    business_days_until,
)

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
# entry_rule = REFERENCE_AS_COLLAR: why a stated market level cannot be this action's entry anchor.
ENTRY_ANCHOR_SEVERAL_LEVELS = "ENTRY_ANCHOR_SEVERAL_LEVELS"
ENTRY_ANCHOR_SECOND_PRICE = "ENTRY_ANCHOR_SECOND_PRICE"
ENTRY_ANCHOR_UNREADABLE = "ENTRY_ANCHOR_UNREADABLE"
ENTRY_ANCHOR_NOT_IN_POST = "ENTRY_ANCHOR_NOT_IN_POST"
ENTRY_ANCHOR_SOURCE_AMBIGUOUS = "ENTRY_ANCHOR_SOURCE_AMBIGUOUS"
ENTRY_ANCHOR_INSTRUMENT_AMBIGUOUS = "ENTRY_ANCHOR_INSTRUMENT_AMBIGUOUS"
ENTRY_ANCHOR_UNEXPLAINED_PRICE = "ENTRY_ANCHOR_UNEXPLAINED_PRICE"
# The level carries no present-market marker, or its own clause says it is something else (a past
# or future level, a chart or order level, a condition, a range, a comparison): not an anchor.
ENTRY_ANCHOR_NOT_CURRENT = "ENTRY_ANCHOR_NOT_CURRENT"
# A separate level line or attached quote has a word that is not this family's name, alias, a
# present-market marker or a unit: whose level it is cannot be told.
ENTRY_ANCHOR_INSTRUMENT_UNCLEAR = "ENTRY_ANCHOR_INSTRUMENT_UNCLEAR"
# A word in the anchor's context window is not on the allow-list: what the number is cannot be told.
ENTRY_ANCHOR_CONTEXT_UNCLEAR = "ENTRY_ANCHOR_CONTEXT_UNCLEAR"
# anchor_scope = SINGLE_MARKET_POST: why the post as a whole does not attribute the level.
ENTRY_ANCHOR_POST_SEVERAL_LEVELS = "ENTRY_ANCHOR_POST_SEVERAL_LEVELS"  # (a)
ENTRY_ANCHOR_POST_SEVERAL_MARKETS = "ENTRY_ANCHOR_POST_SEVERAL_MARKETS"  # (b)
ENTRY_ANCHOR_POST_SEVERAL_ACTIONS = "ENTRY_ANCHOR_POST_SEVERAL_ACTIONS"  # (c)
ENTRY_ANCHOR_REFERENCE_UNNAMED = "ENTRY_ANCHOR_REFERENCE_UNNAMED"  # (d)
# (b), owner's safeguard of 12 November 2026: the post mentions a market the bot cannot resolve
# (an unknown ticker-shaped token, a generic market noun, an instrument phrase or a proper noun that
# is not this family's). Detail: the mention kinds only (MENTION:<MarketMention>).
ENTRY_ANCHOR_POST_UNRESOLVED_MARKET = "ENTRY_ANCHOR_POST_UNRESOLVED_MARKET"
# The candidate level is not an explicit contemporaneous market quotation (every scope). Detail:
# LEVEL_KIND:<AnchorLevelKind> and what decided it (OWN_CLAUSE, CONTEXT_WORD:<w>, CONTEXT_NUMBER,
# CONTEXT_UNKNOWN).
ENTRY_ANCHOR_LEVEL_NOT_MARKET_QUOTE = "ENTRY_ANCHOR_LEVEL_NOT_MARKET_QUOTE"
# Owner, 12 November 2026 (DECIDED 7). (b) a sentence outside this action's own clause carries an
# instruction word: the post may hold a second actionable instruction (RULE:UNATTRIBUTED_ACTION_
# SENTENCE). D3: the anchored entry's protective stop is not uniquely this action's (detail
# TWO_STOP_VALUES, STOP_DIFFERS_FROM_ACTION, STOP_SHARED_WITH_ANOTHER_ACTION). D5: a short entry
# in a long-only family.
ENTRY_ANCHOR_POST_UNATTRIBUTED_ACTION = "ENTRY_ANCHOR_POST_UNATTRIBUTED_ACTION"
PROTECTIVE_STOP_NOT_UNIQUE = "PROTECTIVE_STOP_NOT_UNIQUE"
MAPPING_LONG_ONLY = "MAPPING_LONG_ONLY"
# Runtime modes in which a family may carry entry_rule = REFERENCE_AS_COLLAR: an allow-list, so a
# future mode never inherits the rule silently. Both modes simulate every order and fill: "mock"
# on an invented market, "shadow" at read-only real quotes (no order capability).
ANCHOR_RULE_MODES = ("mock", "shadow")
# Interpreter flags meaning it did not resolve one instrument.
_UNRESOLVED_INSTRUMENT_FLAGS = ("ASSET_UNRESOLVED", "MIXED_SERIES_AND_TICKER",
                                "MULTIPLE_TICKERS_IN_CLAUSE")
# level_contract_rule = FRONT_MONTH: why the front contract cannot be used for an unnamed month.
FRONT_MONTH_IN_ROLL_WINDOW = "FRONT_MONTH_IN_ROLL_WINDOW"
FRONT_MONTH_AMBIGUOUS = "FRONT_MONTH_AMBIGUOUS"
_CASHTAG_RE = re.compile(r"(?<![\w$])\$([A-Za-z][A-Za-z0-9.]{0,9})(?![\w])")


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


class VocabEvidence:
    """PRIVATE: the words and sentences that made an entry-rule hold a vocabulary hold, for the
    owner's local review log only (``[interpreter] review_log_path``). Never journaled, logged,
    alerted or shown on a page other than the owner's review page: it is carried on
    ``MappingDecision`` as a pydantic private attribute, which no serialisation reads, and its
    ``repr`` shows counts only. ``tokens``: (word, kind); ``sentences``: (role, sentence)."""

    __slots__ = ("sentences", "tokens")

    def __init__(self, tokens=(), sentences=()):
        self.tokens: tuple[tuple[str, str], ...] = tuple(dict.fromkeys(tokens))
        self.sentences: tuple[tuple[str, str], ...] = tuple(dict.fromkeys(sentences))

    def __repr__(self) -> str:
        return f"VocabEvidence(<private: {len(self.tokens)} token(s)>)"

    __str__ = __repr__

    def __eq__(self, other) -> bool:
        return isinstance(other, VocabEvidence) and (self.tokens, self.sentences) == (
            other.tokens, other.sentences)

    def __hash__(self) -> int:
        return hash((self.tokens, self.sentences))


@dataclass
class _EvidenceSink:
    """Collects private evidence while one action is decided (only when the policy collects)."""
    tokens: list[tuple[str, str]] = field(default_factory=list)
    sentences: list[tuple[str, str]] = field(default_factory=list)

    def add(self, kind: str, words, role: str, *texts: str) -> None:
        self.tokens += [(w, kind) for w in words]
        self.sentences += [(role, t) for t in texts if t]

    def frozen(self) -> VocabEvidence | None:
        return VocabEvidence(self.tokens, self.sentences) if self.tokens else None


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
    # entry_rule = REFERENCE_AS_COLLAR: the stated market level accepted as this action's entry
    # anchor (a decimal string), handed to the gateway on the MappedQuote. None otherwise.
    entry_anchor: str | None = None
    # How a futures month was chosen: NAMED (the clause), IDEA (a follow-up's own contract),
    # NEAREST (EXPLICIT_OR_NEAREST) or FRONT_MONTH (level_contract_rule). None for other families.
    month_source: Literal["NAMED", "IDEA", "NEAREST", "FRONT_MONTH"] | None = None
    # How the accepted anchor was attributed to this action (additive): OWN_SENTENCE (the
    # instruction's own sentence), AFTER_OWN_INSTRUCTION (ACTION_BLOCK: a level sentence right
    # after the action's own clause), AFTER_ALLOW_LISTED_SENTENCE (a level sentence after an
    # entirely allow-listed one, such as "Stop 48.90."), FIRST_SENTENCE (nothing before it).
    anchor_attribution: str | None = None
    # entry_rule = REFERENCE_AS_COLLAR (additive): what the candidate level was classified as
    # (AnchorLevelKind value). Only MARKET_QUOTE anchors; set on a held anchor once a candidate
    # level was read, None when no level was classified.
    anchor_level_kind: str | None = None
    # PRIVATE (owner's review tool): the words and sentences behind a vocabulary hold. A pydantic
    # private attribute: never in model_dump, model_dump_json, repr, the schema or the journal.
    # Set only when the policy collects evidence (``MappingPolicy.collect_vocab_evidence``).
    _vocab_evidence: VocabEvidence | None = PrivateAttr(default=None)

    @property
    def vocab_evidence(self) -> VocabEvidence | None:
        return self._vocab_evidence


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


_MOVE = r"^(?:\d+(?:\.\d+)?)%?$"


class EntryAnchorRules(StrictModel):
    """The limits of entry_rule = REFERENCE_AS_COLLAR. All three are required: there are no
    defaults. A move is a decimal in the anchor's own price units ("2.5") or a percentage of the
    anchor ("0.5%"). Each has a hard ceiling in code (validator.ANCHOR_MAX_*_CEILING*), which only
    a code change and its review can raise."""
    # How far the live price may have moved from the anchor in the direction that makes the entry
    # worse (up for a long, down for a short): beyond it the bot would be chasing.
    max_adverse_move: str = Field(pattern=_MOVE)
    # How far it may have moved the other way: beyond it the signal may be stale or misread.
    max_favourable_move: str = Field(pattern=_MOVE)
    # The oldest source post the rule may act on, at the decision.
    max_signal_age_seconds: StrictInt = Field(gt=0)
    # A family whose anchor is compared in a reference series and that has no [basis_shift] (a
    # REFERENCE_TRIGGER without a backstop): the largest absolute difference allowed between the
    # live execution and reference quotes at the decision, in execution prices, and the oldest
    # each quote (and their skew) may be. Otherwise [basis_shift]'s cap is the one used.
    max_abs_basis: Decimal | None = Field(default=None, gt=0, allow_inf_nan=False)
    max_basis_age_seconds: StrictFloat | StrictInt | None = Field(default=None, gt=0, le=60,
                                                                  allow_inf_nan=False)
    # How much textual certainty an anchor needs (the owner's choice; no default). Both
    # allow-list the marked number's whole sentence. SENTENCE_STRICT: a standalone level sentence
    # also needs the sentence before it entirely allow-listed. ACTION_BLOCK: it is also accepted
    # when the sentence immediately before it is this action's own clause as the interpreter
    # delimited it (docs/MULTI_ASSET_MAPPING.md, "Anchor scope").
    # SINGLE_MARKET_POST: the level's own sentence as SENTENCE_STRICT; attribution by the whole
    # post instead of a neighbouring sentence (one marked level, one market, one current entry).
    anchor_scope: Literal["SENTENCE_STRICT", "ACTION_BLOCK", "SINGLE_MARKET_POST"]
    # The oldest each side of a live streaming quote (bid and ask, by their own update times) may
    # be at an anchored decision. Required, no default; ceiling validator.ANCHOR_MAX_QUOTE_AGE_*.
    max_quote_age_seconds: StrictFloat | StrictInt = Field(gt=0, allow_inf_nan=False)
    # Required, and "PAPER" is its only value: the rule is for simulation and the paper account,
    # never a funded account. The file states the limitation; no value could mean anything else.
    environment: Literal["PAPER"]
    # An owner decision, default false (shipped false). Off: a level written on its own
    # ("88.42 now.") never anchors. On: it may, only after the action's own instruction sentence
    # or its own stop-and-target sentence, when every own instruction sentence passes the
    # whole-sentence allow-list (docs/MULTI_ASSET_MAPPING.md, "What the level is").
    standalone_level_sentence: StrictBool = False
    @model_validator(mode="before")
    @classmethod
    def _renamed(cls, data):
        if isinstance(data, dict) and "max_stop_distance" in data:
            raise ValueError("max_stop_distance is a family-level key (it governs every entry of "
                             "the family): write it beside entry_rule, not in [entry_anchor]")
        if isinstance(data, dict) and "level_after_own_protective_levels" in data:
            raise ValueError("level_after_own_protective_levels was renamed to "
                             "standalone_level_sentence (an elliptical level may anchor only "
                             "with it, default false)")
        return data

    @field_validator("max_adverse_move", "max_favourable_move")
    @classmethod
    def _positive(cls, v):
        if Decimal(v.rstrip("%")) <= 0:
            raise ValueError(f"move limit {v!r} must be positive")
        return v

    @model_validator(mode="after")
    def _ceilings(self):
        for name, v, ceiling in (
            ("max_adverse_move", self.max_adverse_move, ANCHOR_MAX_ADVERSE_PCT_CEILING),
            ("max_favourable_move", self.max_favourable_move, ANCHOR_MAX_FAVOURABLE_PCT_CEILING),
        ):
            if v.endswith("%") and Decimal(v[:-1]) > ceiling:
                raise ValueError(f"{name} {v!r} is above its ceiling of {ceiling}% (a code "
                                 "constant)")
        if self.max_signal_age_seconds > ANCHOR_MAX_SIGNAL_AGE_CEILING_SECONDS:
            raise ValueError(f"max_signal_age_seconds {self.max_signal_age_seconds} is above its "
                             f"ceiling of {ANCHOR_MAX_SIGNAL_AGE_CEILING_SECONDS} s (a code "
                             "constant)")
        if self.max_quote_age_seconds > ANCHOR_MAX_QUOTE_AGE_CEILING_SECONDS:
            raise ValueError(f"max_quote_age_seconds {self.max_quote_age_seconds:g} is above its "
                             f"ceiling of {ANCHOR_MAX_QUOTE_AGE_CEILING_SECONDS} s (a code "
                             "constant)")
        if (self.max_abs_basis is None) != (self.max_basis_age_seconds is None):
            raise ValueError("the anchor's basis cap states max_abs_basis and "
                             "max_basis_age_seconds together")
        return self

    def absolute_ceiling_problem(self, max_price: Decimal) -> str | None:
        """An absolute move above its ceiling percentage of the highest entry price this family's
        sizing can ever approve (max_notional / (units_per_signal x multiplier))."""
        for name, v, ceiling in (
            ("max_adverse_move", self.max_adverse_move, ANCHOR_MAX_ADVERSE_PCT_CEILING),
            ("max_favourable_move", self.max_favourable_move, ANCHOR_MAX_FAVOURABLE_PCT_CEILING),
        ):
            if not v.endswith("%") and Decimal(v) > max_price * ceiling / 100:
                return (f"{name} {v!r} is above its ceiling of {ceiling}% of the highest price "
                        f"the sizing can buy ({max_price.normalize():f})")
        return None


class AnchorLexicon(StrictModel):
    """The entry-anchor part of the private lexicon extension (its ``entry_anchor`` section):
    present-market markers beyond the public list, and names of a family beyond its mapping
    entry's ``aliases``. Publisher-specific phrasing lives only there. A marker or alias may never
    contain a disqualifying word, so neither can turn a past, chart or conditional level into an
    anchor."""
    present_markers: list[str] = Field(default_factory=list)
    family_aliases: dict[str, list[str]] = Field(default_factory=dict)
    # A publisher's own plain instruction phrasing ("adding exposure"): words an anchor's
    # sentence may also hold. Never a disqualifying word.
    context_words: list[str] = Field(default_factory=list)
    # SINGLE_MARKET_POST: capitalised words of a publisher's that are not markets (a person, a
    # place), so check (b) does not hold on them. One word of letters each; never a disqualifying
    # word, a generic market noun or an instrument noun.
    non_market_words: list[str] = Field(default_factory=list)

    @field_validator("context_words")
    @classmethod
    def _context(cls, v):
        return [check_phrase(m, "context word", 4) for m in v]

    @field_validator("non_market_words")
    @classmethod
    def _non_market(cls, v):
        out = []
        for w in v:
            p = check_phrase(w, "non-market word", 1)
            if p in MARKET_NOUNS or p in INSTRUMENT_NOUNS:
                raise ValueError(f"non-market word {w!r} is a market or instrument noun")
            out.append(p)
        return out

    @field_validator("present_markers")
    @classmethod
    def _markers(cls, v):
        return [check_phrase(m, "present-market marker", 4) for m in v]

    @field_validator("family_aliases")
    @classmethod
    def _aliases(cls, v):
        return {f: [_alias(a) for a in names] for f, names in v.items()}


def _alias(a: str) -> str:
    p = check_phrase(a, "alias", 3)
    marker_words = {w for m in PRESENT_MARKERS for w in m.split()}
    if set(p.split()) & marker_words:
        raise ValueError(f"alias {a!r} contains a present-market marker word")
    return p


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
    # same_series only: a micro and its standard contract, the two roots (review N1).
    equivalent_roots: list[str] = Field(default_factory=list)
    reference: SeriesSpec | None = None
    execution: ExecutionSpec | None = None
    contract_selection: ContractSelection | None = None
    contracts: list[ContractMonth] = Field(default_factory=list)
    sizing: Sizing | None = None
    session: Session | None = None
    roll: Roll | None = None
    basis_shift: BasisShiftRules | None = None
    reference_trigger: ReferenceTriggerRules | None = None
    # How an OPEN or ADD gets its entry price. EXPLICIT_ENTRY_ONLY (the default, today's rule):
    # only explicit execution language sets it. REFERENCE_AS_COLLAR: a current instruction stating
    # exactly one market level enters at the live marketable price, the level bounding it as an
    # anchor ([entry_anchor] limits, required). Part of what the owner approves.
    entry_rule: EntryRule = "EXPLICIT_ENTRY_ONLY"
    entry_anchor: EntryAnchorRules | None = None
    # DIRECT futures quoted in a futures series (their own, or same_series), EXPLICIT_ONLY: what a
    # level stated without a contract month refers to.
    # EXPLICIT_MONTH_ONLY (the default): nothing; the action HOLDs. FRONT_MONTH: the front contract
    # of this entry's own list (the first listed contract still at least
    # min_business_days_to_roll business days before its roll date), executed in that month.
    level_contract_rule: Literal["EXPLICIT_MONTH_ONLY", "FRONT_MONTH"] = "EXPLICIT_MONTH_ONLY"
    # Plain-word names of this family (one to three words, for example "crude oil"). With the
    # entry rule on, a separate level line may name only this family: its code, its series
    # symbols, these aliases (and the private lexicon's).
    aliases: list[str] = Field(default_factory=list)
    # Owner, 12 November 2026 (D5): an OPEN or ADD on the short side HOLDs at the mapping stage
    # (MAPPING_LONG_ONLY), for explicit and anchored entries alike. Default false.
    long_only: StrictBool = False
    # Owner, 12 November 2026: the farthest the protective stop of ANY entry of this family
    # (explicit or anchored, OPEN or ADD) may be, as a percentage, from the entry (the explicit
    # price, or the stated reference of an anchored entry) and from the limit actually sent.
    # Strict "N%", code ceiling validator.ANCHOR_MAX_STOP_DISTANCE_PCT_CEILING; required with
    # entry_rule = REFERENCE_AS_COLLAR, optional otherwise (absent: today's behaviour). An outer
    # rejection boundary, never a stop the bot sets; it does not bound the loss in a gap.
    max_stop_distance: StrictStr | None = Field(default=None, pattern=r"^\d+(?:\.\d+)?%$")

    @field_validator("aliases")
    @classmethod
    def _aliases(cls, v):
        return [_alias(a) for a in v]

    @model_validator(mode="after")
    def _consistent(self):
        def bad(msg: str):
            raise ValueError(f"family {self.family}: {msg}")

        collar = self.entry_rule == "REFERENCE_AS_COLLAR"
        if collar != (self.entry_anchor is not None):
            bad("entry_rule = REFERENCE_AS_COLLAR needs its [entry_anchor] limits "
                "(max_adverse_move, max_favourable_move, max_signal_age_seconds; no defaults), "
                "and [entry_anchor] is used only by it")
        if self.max_stop_distance is not None:
            stop_limit = Decimal(self.max_stop_distance[:-1])
            if not stop_limit.is_finite() or stop_limit <= 0 \
                    or stop_limit > ANCHOR_MAX_STOP_DISTANCE_PCT_CEILING:
                bad(f"max_stop_distance {self.max_stop_distance!r} must be above 0 and at most "
                    f"its ceiling of {ANCHOR_MAX_STOP_DISTANCE_PCT_CEILING}% (a code constant)")
        if collar and self.max_stop_distance is None:
            bad("entry_rule = REFERENCE_AS_COLLAR needs max_stop_distance (a family-level key)")
        if collar and (self.basis == "UNSUPPORTED" or self.family.endswith(".OPT")):
            bad("entry_rule = REFERENCE_AS_COLLAR applies to a non-option traded family only")

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
        if legacy and collar:
            bad("entry_rule = REFERENCE_AS_COLLAR is decided on the mapped path: state sizing "
                "and session for this entry")
        if collar:
            self._check_anchor_rule(bad)
        if self.same_series or self.equivalent_roots:
            self._check_same_series(bad)
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
        if self.level_contract_rule == "FRONT_MONTH" and not (
                self.basis == "DIRECT" and x.sec_type == "FUT" and ref.sec_type == "FUT"
                and self.contract_selection.rule == "EXPLICIT_ONLY"):
            bad("level_contract_rule = FRONT_MONTH is only for a DIRECT futures entry whose levels "
                "are a futures series (its own, or same_series), selected EXPLICIT_ONLY")
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

    def _check_anchor_rule(self, bad) -> None:
        """The anchor's absolute limits within their ceilings, and the basis cap (review F4): an
        anchor compared in a reference series bounds the execution price only through the live
        basis, so the entry must state its cap ([basis_shift]'s, or the anchor's own when there is
        no [basis_shift])."""
        rules, x, s = self.entry_anchor, self.execution, self.sizing
        problem = rules.absolute_ceiling_problem(
            s.max_notional / (s.units_per_signal * x.multiplier))
        if problem:
            bad(problem)
        if self.basis in ("BASIS_SHIFT", "REFERENCE_TRIGGER"):
            if self.basis_shift is None and rules.max_abs_basis is None:
                bad("entry_rule = REFERENCE_AS_COLLAR on a reference-series family needs a basis "
                    "cap: [basis_shift], or entry_anchor max_abs_basis and max_basis_age_seconds")
            if self.basis_shift is not None and rules.max_abs_basis is not None:
                bad("the basis cap of this family is [basis_shift]'s: entry_anchor states no "
                    "second one")
        elif rules.max_abs_basis is not None:
            bad("an entry_anchor basis cap is only for a reference-series family")

    def _check_same_series(self, bad) -> None:
        """same_series joins two futures only (review N1): both FUT, one currency, one root or
        the two roots of an ``equivalent_roots`` pair (a micro and its standard contract), and a
        selection that uses the month the instruction names, so a level quoted for one month is
        never applied to another. An index, a spot commodity, a CFD, a stock or an ETF on either
        side refuses: a stock or ETF quoted in its own price needs no flag."""
        x, ref = self.execution, self.reference
        rule = "same_series joins two futures of one underlying only"
        if not self.same_series:
            bad(f"{rule}: equivalent_roots is used only with same_series = true")
        if ref.sec_type != "FUT" or x.sec_type != "FUT":
            bad(f"{rule}, not {ref.sec_type} to {x.sec_type}")
        if ref.currency != x.currency:
            bad(f"{rule}, in one currency ({ref.currency} != {x.currency})")
        if ref.symbol != x.symbol and sorted(self.equivalent_roots) != sorted(
                {ref.symbol, x.symbol}):
            bad(f"{rule}: roots {ref.symbol} and {x.symbol} differ and are not listed together "
                "in equivalent_roots")
        if self.contract_selection is None or self.contract_selection.rule != "EXPLICIT_ONLY":
            bad(f"{rule}, with contract_selection EXPLICIT_ONLY (the level's month is the "
                "execution contract's month)")

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
            entry_rule=self.entry_rule,
            anchor_max_adverse_move=self.entry_anchor.max_adverse_move if self.entry_anchor
            else None,
            anchor_max_favourable_move=self.entry_anchor.max_favourable_move if self.entry_anchor
            else None,
            anchor_max_signal_age_seconds=self.entry_anchor.max_signal_age_seconds
            if self.entry_anchor else None,
            anchor_max_abs_basis=self.entry_anchor.max_abs_basis if self.entry_anchor else None,
            anchor_max_basis_age_seconds=self.entry_anchor.max_basis_age_seconds
            if self.entry_anchor else None,
            max_stop_distance=self.max_stop_distance,
            anchor_max_quote_age_seconds=self.entry_anchor.max_quote_age_seconds
            if self.entry_anchor else None,
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
        post_text: str | None = None,
        anchor: Decimal | None = None,
        source: str | None = None,
    ) -> tuple[Instrument | None, list[str]]:
        """The execution instrument for this action, or the reasons it cannot be selected.
        ``anchor`` is the stated level accepted as the entry anchor (REFERENCE_AS_COLLAR): the
        leftover-token rule counts it as read, and nothing else. ``source`` is the line or quote
        it was taken from when that is not the clause: it passes the same contract checks."""
        x = self.execution
        if x.sec_type == "STK":
            exchange = None if x.exchange == "SMART" else x.exchange  # v1: SMART is the default
            return Instrument(asset_class=x.asset, symbol=x.symbol, exchange=exchange), []
        if x.sec_type in ("CMDTY", "CFD"):
            return Instrument(asset_class=x.asset, symbol=x.symbol, exchange=x.exchange), []
        if x.sec_type == "FUT":
            return self._select_future(terms, today, held, action, anchor, source)
        return self._select_option(asset, terms, held, action, post_text)

    def _roots(self) -> set[str]:
        """The symbols a contract code of this family may start with."""
        roots = {self.execution.symbol, self.family, *self.equivalent_roots}
        if self.reference is not None and self.reference.sec_type == "FUT":
            roots.add(self.reference.symbol)
        return roots

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
                   if code_shaped(w, x.symbol) and w not in codes]
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

    def _select_future(self, terms, today, held, action=None, anchor=None, source=None):
        x = self.execution
        new_exposure = action is None or action.action in (Action.OPEN, Action.ADD)
        # Review F2 (entry anchor): a contract described rather than named ("back month", "next
        # contract", "roll") is the publisher's choice, not the bot's: HOLD, never the front.
        line = extract_terms(source) if source and source != terms.clause else None
        for t in (terms, line) if new_exposure else ():
            relative = relative_contract_words(t) if t is not None else []
            if relative:
                return None, [CONTRACT_MONTH_UNRECOGNISED,
                              *(f"RELATIVE_CONTRACT:{w}" for w in relative)]
        named, problems = self._named_months(terms, today)
        if problems:
            return None, problems
        line_named = None
        if line is not None:
            # Review F3: the line the anchor is taken from passes the clause's own checks, and a
            # month it names must be the order's month (checked below).
            line_named, problems = self._named_months(line, today)
            if problems:
                return None, [*problems, "IN_LEVEL_LINE"]
        if held is None and action is not None:
            # Default HOLD (review F2, confirmation): a clause that still names anything after its
            # recognised months and the action's own levels has named something not understood.
            # An accepted entry anchor is one of the action's levels; nothing else is added.
            levels, fractions = _consumed(action)
            if anchor is not None:
                levels = levels | {anchor}
            leftover = leftover_tokens(terms, levels, fractions, self._roots())
            if line is not None:  # its numbers are checked by unexplained_prices
                leftover += [t for t in leftover_tokens(line, levels, fractions, self._roots())
                             if not any(ch.isdigit() for ch in t)]
            if leftover:
                return None, [CONTRACT_MONTH_UNRECOGNISED,
                              *(f"UNREAD_TOKEN:{t}" for t in leftover)]
        if held is not None:  # a follow-up acts on the idea's own contract
            month = (held.expiry or "")[:6]
            if named and named != {month}:
                return None, [IDEA_CONTRACT_MISMATCH, f"IDEA_MONTH:{month}"]
        elif named:
            month = named.pop()
        elif self.contract_selection.rule == "EXPLICIT_ONLY":
            if self.level_contract_rule != "FRONT_MONTH":
                return None, [CONTRACT_SELECTION_AMBIGUOUS, "CONTRACT_MONTH_NOT_STATED"]
            month, problems = self.front_month(today)
            if problems:
                return None, problems
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
        if line_named and line_named != {month}:
            return None, [CONTRACT_MONTH_UNRECOGNISED, f"LEVEL_LINE_MONTH:{min(line_named)}",
                          f"ORDER_MONTH:{month}"]
        return Instrument(
            asset_class=AssetClass.FUTURE, symbol=x.symbol, exchange=x.exchange,
            futures_contract=month,
        ), []

    def front_month(self, today: date) -> tuple[str | None, list[str]]:
        """level_contract_rule = FRONT_MONTH: the contract a level stated without a month refers
        to. The list must be unambiguous (months listed in order, roll dates strictly increasing
        with them). The front is the first listed contract whose roll date has not been reached.
        If it is fewer than ``min_business_days_to_roll`` business days from that date, the
        publisher may be quoting either it or the next month: HOLD, never the next one."""
        listed = list(self.contracts)
        months = [c.month for c in listed]
        dates = [c.roll_date for c in listed]
        if months != sorted(months) or any(b <= a for a, b in pairwise(dates)):
            return None, [FRONT_MONTH_AMBIGUOUS, "CONTRACT_LIST_NOT_IN_ROLL_ORDER"]
        holidays = self.roll.holidays
        live = [c for c in listed if business_days_until(today, c.roll_date, holidays) > 0]
        if not live:
            return None, [FRONT_MONTH_AMBIGUOUS, "NO_LISTED_CONTRACT_BEFORE_ITS_ROLL_DATE"]
        front = live[0]
        need = self.contract_selection.min_business_days_to_roll
        if business_days_until(today, front.roll_date, holidays) < need:
            return None, [FRONT_MONTH_IN_ROLL_WINDOW, f"FRONT:{front.month}",
                          f"ROLL_DATE:{front.roll_date}"]
        return front.month, []

    def entry_anchor_for(
        self,
        action: TradeAction,
        asset: str,
        terms: InstructionTerms,
        levels: list[tuple[str, str]] | None,
        others: list[tuple[str | None, str | None, set[Decimal]]] = (),
        post_text: str | None = None,
        lexicon: AnchorLexicon | None = None,
        attribution: list[str] | None = None,
        post: dict | None = None,
        kind_out: list[AnchorLevelKind] | None = None,
        evidence: _EvidenceSink | None = None,
    ) -> tuple[Decimal | None, str | None, list[str]]:
        """entry_rule = REFERENCE_AS_COLLAR: the stated market level that may anchor this action's
        entry, the text it is written in, or why none may. ``levels`` are the interpreter's price
        mentions for this action, as (role, value): its classification of the post
        (``PriceMention``), never the action's entry field. Applies only to a CURRENT OPEN or ADD
        with no explicit entry price on an entry with the rule on; for anything else
        (None, None, []) and the gateway decides as today.

        A level is an anchor only if the interpreter tagged exactly one distinct value REFERENCE for
        this action and no other price (a trigger, an exit) beside its stop and target, and the value
        is written in exactly one place: the action's own clause; else exactly one other clause of
        the post (a commentary line quoting the market); else exactly one line of the post that is
        no action's clause (a level line such as "41.20 now."). The interpreter attaches such lines
        to the instruction they follow. No action of the post for another instrument may share the
        action's clause or state the same value, and neither the clause nor the source line may name
        another cashtag. The last check, that no other number is left in either, runs after the
        contract is selected (``unexplained_prices``).

        Default HOLD (review F1, F7): the value is an anchor only with positive evidence that it is
        the market now, a present-market marker attached to it, and nothing in its own clause
        making it something else (``anchor_not_current``: ENTRY_ANCHOR_NOT_CURRENT). A separate
        line or quote may name only this family (ENTRY_ANCHOR_INSTRUMENT_UNCLEAR).

        Every candidate level is classified (``kind_out``: one AnchorLevelKind) and only a
        MARKET_QUOTE anchors (``_level_kind``; ENTRY_ANCHOR_LEVEL_NOT_MARKET_QUOTE otherwise). The
        checks before it feed the classification: a refusal by them records the kind their words
        show (a stop word STOP_OR_TARGET, a time word HISTORICAL, anything else AMBIGUOUS).

        ``evidence`` (the owner's local review tool only) receives the words and sentences of a
        vocabulary hold; it changes nothing in the decision."""
        kind_out = kind_out if kind_out is not None else []

        def held(problems: list[str], kind: AnchorLevelKind | None = AnchorLevelKind.AMBIGUOUS,
                 words=()):
            kind_out[:] = [level_kind_of_words(words) or kind] if kind else []
            return None, None, problems

        if not (self.entry_rule == "REFERENCE_AS_COLLAR"
                and action.action in (Action.OPEN, Action.ADD)
                and action.timing == Timing.CURRENT and action.entry_reference is None):
            return None, None, []
        refs, second = set(), []
        for role, value in levels or ():
            if role in ("STOP", "TARGET"):
                continue
            if role != "REFERENCE":
                second.append(f"PRICE_ROLE:{role}")
                continue
            v = _level(value)
            if v is None:
                return None, None, [ENTRY_ANCHOR_UNREADABLE]
            refs.add(v)
        if not refs:
            return None, None, []  # no stated level: the gateway holds for the missing entry
        if len(refs) > 1:
            return None, None, [ENTRY_ANCHOR_SEVERAL_LEVELS, f"LEVELS:{len(refs)}"]
        if second:
            return None, None, [ENTRY_ANCHOR_SECOND_PRICE, *second]
        anchor = refs.pop()
        for other_asset, other_clause, other_values in others:
            if other_asset is not None and other_asset != asset and (
                    (other_clause and other_clause == terms.clause) or anchor in other_values):
                return None, None, [ENTRY_ANCHOR_INSTRUMENT_AMBIGUOUS,
                                    "SHARED_WITH_ANOTHER_INSTRUMENT"]
        if anchor in clause_numbers(terms.clause):
            source = terms.clause
        else:
            clauses = {c for _, c, _ in others if c and anchor in clause_numbers(c)}
            lines = [] if clauses else [
                line for line in _post_lines(post_text)
                if anchor in clause_numbers(line)
                and not any(c and (c.strip() in line or line in c) for _, c, _ in others)]
            found = sorted(clauses) or lines
            if not found:
                return None, None, [ENTRY_ANCHOR_NOT_IN_POST]
            if len(found) > 1:
                return None, None, [ENTRY_ANCHOR_SOURCE_AMBIGUOUS, f"PLACES:{len(found)}"]
            source = found[0]
        names = {asset, self.family.removesuffix(".OPT")}
        names |= {s.symbol for s in (self.execution, self.reference) if s is not None}
        for text in {terms.clause, source}:
            if {t.upper().rstrip(".") for t in _CASHTAG_RE.findall(text)} - names:
                return None, None, [ENTRY_ANCHOR_INSTRUMENT_AMBIGUOUS, "OTHER_CASHTAG"]
        lexicon = lexicon or AnchorLexicon()
        markers = (*PRESENT_MARKERS, *lexicon.present_markers)
        why = anchor_not_current(source, anchor, markers)
        bad_words = [w.split(":", 1)[1] for w in why if w.startswith("NOT_CURRENT_WORD:")]
        if "NO_PRESENT_MARKER" in why:  # no positive evidence at all
            return held([ENTRY_ANCHOR_NOT_CURRENT, *why], words=bad_words)
        own = self.anchor_names(asset, lexicon)
        if self.entry_anchor.anchor_scope == "SINGLE_MARKET_POST":
            problems = self._single_market_problems(action, asset, anchor, source, post_text,
                                                    markers, post or {}, lexicon,
                                                    own_clause=terms.clause, evidence=evidence)
            if problems:
                return held(problems, None)  # the post, not the level, is in doubt
        attributed: list[str] = []
        unknown: list[str] = []
        problems = self._sentence_problems(action, anchor, source, terms, levels, post_text,
                                           markers, own, lexicon, unknown_out=unknown,
                                           attribution=attributed, evidence=evidence)
        if problems:
            return held(problems, words=unknown)
        if why:  # the disqualifying words: a second, redundant check
            return held([ENTRY_ANCHOR_NOT_CURRENT, *why], words=bad_words)
        kind, detail = self._level_kind(anchor, source, post_text, own, markers, action=action,
                                        levels=levels, terms=terms, lexicon=lexicon,
                                        attribution=attributed, evidence=evidence)
        if kind != AnchorLevelKind.MARKET_QUOTE:
            return held([ENTRY_ANCHOR_LEVEL_NOT_MARKET_QUOTE, f"LEVEL_KIND:{kind.value}", detail],
                        kind)
        kind_out[:] = [AnchorLevelKind.MARKET_QUOTE]
        # Another marked level elsewhere in the post that is no other action's: which is meant?
        consumed, _ = _consumed(action)
        consumed |= {v for role, raw in levels or () if role in ("STOP", "TARGET")
                     and (v := _level(raw)) is not None}
        texts = [terms.clause, source, *(
            line for line in _post_lines(post_text)
            if not any(c and (c.strip() in line or line in c) for _, c, _ in others))]
        marked = set().union(*(marked_numbers(x, markers) for x in texts)) - consumed - {anchor}
        if marked:
            return held([ENTRY_ANCHOR_SEVERAL_LEVELS, "MARKED_ELSEWHERE"], None)
        if source != terms.clause:
            allowed = set(own) | set(markers) | {"at"}
            foreign = foreign_words(source, anchor, allowed, self._month_spans(source))
            if foreign:
                if evidence is not None:
                    evidence.add("LEVEL_LINE_WORD", foreign, "LEVEL_LINE", source)
                return held([ENTRY_ANCHOR_INSTRUMENT_UNCLEAR, f"OTHER_WORDS:{len(foreign)}"])
        if attribution is not None:
            attribution += attributed
        return anchor, source, []

    def _level_kind(self, anchor: Decimal, source: str, post_text: str | None, own: list[str],
                    markers, *, action: TradeAction | None = None, levels=None,
                    terms: InstructionTerms | None = None, lexicon: AnchorLexicon | None = None,
                    attribution: list[str] | None = None,
                    evidence: _EvidenceSink | None = None) -> tuple[AnchorLevelKind, str]:
        """The owner's ambiguity safeguard: what the candidate level is, as one closed kind. Only
        an explicit contemporaneous market quotation (MARKET_QUOTE) may anchor.

        A. The number's own clause (between punctuation, as ``anchor_not_current`` delimits it)
           holds only the number, its attached marker, the quote connectors, a currency or unit
           and a name of this family; a holder, position or trade word reads as an entry price or
           a cost basis (ENTRY_OR_COST_BASIS), a pronoun, a quotation or approximation mark or any
           other word as AMBIGUOUS.
        B. Understood neighbours (final review round: paths removed, not patterns added). An
           understood neighbour of the level's sentence is the post's boundary, one of this
           action's own instruction sentences (an own clause sentence naming this family or
           carrying the action's side or add word), or a sentence stating only the action's own
           stop and target on their protective and profit sides. R1, every anchored level: the
           sentence AFTER the level's sentence is understood (a protective-levels sentence counts
           whatever the key), else FOLLOWING_NOT_UNDERSTOOD. An elliptical level (its clause does
           not name this family and its sentence is not the instruction) never anchors unless
           ``standalone_level_sentence`` is on (STANDALONE_LEVEL_NOT_ENABLED); then every own
           instruction sentence must pass the whole-sentence allow-list a level's own sentence
           passes (INSTRUCTION_NOT_UNDERSTOOD), and R2: the earlier clauses
           of its sentence, else the sentence BEFORE, are the action's own instruction sentence or,
           only with ``standalone_level_sentence``, its own protective levels, else
           NEIGHBOUR_NOT_UNDERSTOOD (also at the start of the post). The level- and cost-word
           deny-list (``context_problem``) remains a second check that can only add a hold.
        A sentence that cannot be located in the post has no known context: AMBIGUOUS.
        ``evidence`` (the owner's local review tool only) receives the words of a neighbour or
        instruction sentence that only vocabulary keeps from being understood; it decides
        nothing."""
        lexicon = lexicon or AnchorLexicon()
        allowed = [*markers, *own]

        def neighbour_evidence(level_sentence: str, neighbour: str, question: bool,
                               require_key: bool) -> None:
            """Only when the private context words alone could make the neighbour a
            protective-levels sentence: otherwise no vocabulary would resolve it."""
            if evidence is None or question:
                return
            words = protective_unknown_words(neighbour, lexicon.context_words)
            if not words:
                return
            widened = AnchorLexicon.model_construct(
                present_markers=lexicon.present_markers, family_aliases=lexicon.family_aliases,
                non_market_words=lexicon.non_market_words,
                context_words=[*lexicon.context_words, *words])
            if self._own_protective_levels(neighbour, question, anchor, action, widened,
                                           require_key=require_key):
                evidence.add("NEIGHBOUR_WORD", words, "NEIGHBOUR", neighbour)
                evidence.add("NEIGHBOUR_WORD", (), "LEVEL", level_sentence)
        own_block = {s for s, _ in sentences(terms.clause)} if terms is not None else set()
        instruction = list(SIDE_WORDS.get(getattr(getattr(action, "position_side", None),
                                                  "value", ""), ()))
        if action is not None and action.action == Action.ADD:
            instruction += ADD_WORDS

        def instruction_sentence(text: str) -> bool:
            """One of this action's own clause sentences that is the instruction itself: it names
            this family or carries the action's own side or add word (final review round: a
            holder or pronoun line folded into the clause is not the instruction)."""
            if text not in own_block:
                return False
            words = {w.lower().strip("()[]{}.,;:!?\"'") for w in text.split()}
            return self._names_family(text, own) or bool(words & set(instruction))

        whole_q = sentences(post_text or source)
        whole = [s for s, _ in whole_q]
        places = level_places(source, anchor)
        if not places:
            return AnchorLevelKind.AMBIGUOUS, "NOT_FOUND"
        for sentence, clause, earlier in places:
            kind = clause_kind(quote_clause_words(clause, allowed, self._month_spans(clause)))
            if kind is not None:
                return kind, "OWN_CLAUSE"
            at = next((i for i, w in enumerate(whole) if w == sentence), None)
            if at is None:
                at = next((i for i, w in enumerate(whole) if sentence in w), None)
                if at is None:
                    return AnchorLevelKind.AMBIGUOUS, "CONTEXT_UNKNOWN"
                # The source is a part of a longer post sentence: its earlier clauses count too.
                earlier = [*next((e for s, c, e in level_places(whole[at], anchor)
                                  if c.strip() == clause.strip()), []), *earlier]
                sentence = whole[at]
            # R1, every anchored level: the sentence after the level's sentence is understood (the
            # post's end, the action's own instruction sentence, or its own protective levels,
            # whatever the key).
            if at + 1 < len(whole):
                after, question = whole_q[at + 1]
                if not (instruction_sentence(after) or self._own_protective_levels(
                        after, question, anchor, action, lexicon, require_key=False)):
                    neighbour_evidence(sentence, after, question, require_key=False)
                    return AnchorLevelKind.AMBIGUOUS, "FOLLOWING_NOT_UNDERSTOOD"
            if self._names_family(clause, own):
                continue  # an explicit quotation
            if instruction_sentence(sentence):
                # The level inside the instruction itself; the deny-list still reads the clauses
                # before it ("We are long $X, stop 40.10, 41.20 now." holds as before).
                problem = context_problem(earlier)
                if problem:
                    return problem
                continue
            # Fallback (review F1): an elliptical level never anchors unless the owner enables
            # standalone_level_sentence; then (c) every own instruction sentence must pass the
            # whole-sentence allow-list a level's own sentence must pass, so the level is
            # accepted only where it would have been accepted inside the instruction sentence.
            if not (self.entry_anchor and self.entry_anchor.standalone_level_sentence):
                return AnchorLevelKind.AMBIGUOUS, "STANDALONE_LEVEL_NOT_ENABLED"
            allowed_all, _, consumed, fractions = _allow_list(action, levels, markers, own,
                                                              lexicon)
            own_instructions = [(t, q) for t, q in whole_q if instruction_sentence(t)]
            if not own_instructions or any(
                    self._whole_sentence_problem(t, q, allowed_all, consumed | {anchor}, fractions)
                    for t, q in own_instructions):
                if evidence is not None:
                    for t, q in own_instructions:
                        words: list[str] = []
                        self._whole_sentence_problem(t, q, allowed_all, consumed | {anchor},
                                                     fractions, words)
                        evidence.add("UNKNOWN_WORD", words, "INSTRUCTION", t)
                    evidence.add("UNKNOWN_WORD", (), "LEVEL", sentence)
                return AnchorLevelKind.AMBIGUOUS, "INSTRUCTION_NOT_UNDERSTOOD"
            # R2, an elliptical level: its preceding neighbour (the earlier clauses of its
            # sentence, else the sentence before) is the action's own instruction sentence or,
            # only with standalone_level_sentence, its own protective levels. Nothing
            # else: the generic allow-listed neighbour no longer counts.
            if earlier:
                neighbour, question = " ".join(earlier), False
            elif at > 0:
                neighbour, question = whole_q[at - 1]
            else:
                return AnchorLevelKind.AMBIGUOUS, "NEIGHBOUR_NOT_UNDERSTOOD"
            if self._own_protective_levels(neighbour, question, anchor, action, lexicon):
                if attribution is not None and "AFTER_OWN_PROTECTIVE_LEVELS" not in attribution:
                    attribution.append("AFTER_OWN_PROTECTIVE_LEVELS")
                continue
            if earlier or not instruction_sentence(neighbour):
                neighbour_evidence(sentence, neighbour, question, require_key=True)
                return AnchorLevelKind.AMBIGUOUS, "NEIGHBOUR_NOT_UNDERSTOOD"
            # The deny-list, a second check that can only add a hold.
            problem = context_problem([neighbour])
            if problem:
                return problem
        return AnchorLevelKind.MARKET_QUOTE, ""

    def _own_protective_levels(self, sentence: str, question: bool, anchor: Decimal,
                               action: TradeAction | None, lexicon: AnchorLexicon, *,
                               require_key: bool = True) -> bool:
        """``standalone_level_sentence`` (default false): ``sentence`` states only
        this action's own stop (and target): every word on the public protective-levels list or
        the private context words, every number exactly the action's own stop or target, the stop
        on the protective side of the level (below it for a long), a target on the profit side,
        no time, condition or cost-basis word, no month, not a question."""
        if (require_key and not (self.entry_anchor
                                 and self.entry_anchor.standalone_level_sentence)) \
                or action is None or question:
            return False
        stop, target = _level(action.stop_price), _level(action.target_price)
        if stop is None:
            return False
        long = getattr(action.position_side, "value", "") == "LONG"
        if (stop >= anchor) if long else (stop <= anchor):
            return False
        if target is not None and ((target <= anchor) if long else (target >= anchor)):
            return False
        own_values = {stop} | ({target} if target is not None else set())
        return protective_sentence_problem(sentence, own_values, lexicon.context_words) is None

    def _names_family(self, clause: str, own: list[str]) -> bool:
        """Does the clause name this family: one of its names or aliases, or a contract code of
        one of its roots (the contract check then decides the month)?"""
        low = clause.lower().replace("$", " ")
        if any(re.search(rf"(?<![\w]){re.escape(n)}(?![\w])", low) for n in own):
            return True
        roots = {r.upper() for r in self._roots()} if self.execution.sec_type == "FUT" else set()
        return any(code_shaped(t.strip("()[]{}.,;:!?\"'").lstrip("$").upper(), r)
                   for t in clause.split() for r in roots)

    def _single_market_problems(self, action, asset, anchor, source, post_text, markers,
                                post, lexicon: AnchorLexicon | None = None,
                                own_clause: str | None = None,
                                evidence: _EvidenceSink | None = None) -> list[str]:
        """anchor_scope = SINGLE_MARKET_POST: the post as a whole attributes the level. (c) this is
        the post's only action besides commentary, and no action is conditional, retrospective or
        uncertain; (b) every action is this family's, no instrument is unresolved, and the post
        names no other family (its codes and symbols, case as written; its aliases from the
        mapping file or the private lexicon, any case) and no cashtag but this family's; (a)
        exactly one sentence of the post carries a marked number; (d) for a reference-series
        family the level's sentence does not name the execution instrument."""
        actions = post.get("actions")
        if actions is None:
            return [ENTRY_ANCHOR_POST_SEVERAL_ACTIONS, "POST_ACTIONS_UNKNOWN"]
        for b in actions:
            if b.action_id == action.action_id:
                continue
            if b.timing in (Timing.CONDITIONAL, Timing.HISTORICAL, Timing.UNCERTAIN) \
                    or b.action != Action.NO_TRADE:
                return [ENTRY_ANCHOR_POST_SEVERAL_ACTIONS, f"OTHER:{b.action.value}"]
        for b in actions:
            if any(f in a for a in b.ambiguities for f in _UNRESOLVED_INSTRUMENT_FLAGS):
                return [ENTRY_ANCHOR_POST_SEVERAL_MARKETS, "INSTRUMENT_UNRESOLVED"]
            if b.source_market_symbol is not None and b.source_market_symbol != asset:
                return [ENTRY_ANCHOR_POST_SEVERAL_MARKETS, "ACTION_FOR_ANOTHER_FAMILY"]
        text = post_text or source
        mine = {asset, self.family.removesuffix(".OPT"), self.execution.symbol,
                *self.equivalent_roots, *([self.reference.symbol] if self.reference else [])}
        if {c.upper().rstrip(".") for c in _CASHTAG_RE.findall(text)} - mine:
            return [ENTRY_ANCHOR_POST_SEVERAL_MARKETS, "OTHER_CASHTAG"]
        mine_lower = {m.lower() for m in mine} | set(self.aliases)
        for family, (codes, aliases) in (post.get("vocabulary") or {}).items():
            if family.removesuffix(".OPT") == self.family.removesuffix(".OPT"):
                continue
            # Any case, with or without a cashtag sign or surrounding punctuation, whole tokens
            # only (final review): "qof", "(QOF)" and "$qof" name the family, "qoffee" does not.
            for name in {c.lower() for c in codes} | set(aliases):
                if name in mine_lower:
                    continue
                if re.search(rf"(?<!\w){re.escape(name)}(?!\w)", text, re.IGNORECASE):
                    return [ENTRY_ANCHOR_POST_SEVERAL_MARKETS, "OTHER_FAMILY_NAMED"]
        # The owner's decision (DECIDED 7): a competing market holds the post only when the post
        # "may contain multiple actionable instruments".
        problem = self._competing_instrument(text, asset, mine, mine_lower, markers,
                                             lexicon or AnchorLexicon(), own_clause,
                                             evidence)
        if problem:
            return problem
        marked = [s for s, _ in sentences(text) if marked_numbers(s, markers)]
        if len(marked) != 1:
            return [ENTRY_ANCHOR_POST_SEVERAL_LEVELS, f"MARKED_SENTENCES:{len(marked)}"]
        if self.basis in ("BASIS_SHIFT", "REFERENCE_TRIGGER") and self.reference is not None:
            execution = {self.execution.symbol, *self.equivalent_roots} - {self.reference.symbol}
            for s, _ in sentences(source):
                if anchor in clause_numbers(s) and any(
                        re.search(rf"(?<![\w]){re.escape(x)}(?!\w)", s.replace("$", " "))
                        for x in execution):
                    return [ENTRY_ANCHOR_REFERENCE_UNNAMED, "EXECUTION_SYMBOL"]
        return []

    def _competing_instrument(self, text, asset, mine, mine_lower, markers, lexicon,
                              own_clause, evidence: _EvidenceSink | None = None) -> list[str]:
        """(a) a mention the bot cannot tie to this family (``unresolved_market_mentions``, and a
        sentence-initial unknown capitalised word) holds only when its own sentence gives it a
        trading context: an instruction, stop or target word (``ACTION_SEMANTIC_WORDS``), a
        price-like level (``price_like_level``) or, with the mention only, a past form, close or
        acquisition verb (``MENTION_ACTION_WORDS``). (d) the sentence directly after an own
        instruction sentence holds a mention and an additive word ("Qorvex too."). (b) a sentence
        outside this action's own clause sentences that holds an instruction word (hyphen and
        slash parts included) holds, whatever it names. Plain commentary holds none of them.
        ``evidence`` (the owner's local review tool only) receives the mention words and their
        sentence of a MENTION_WITH_CONTEXT hold, and nothing for (d) or (b), which no vocabulary
        decision can or should remove; it changes nothing in the result."""
        own_sentences = {s for s, _ in sentences(own_clause or "")}
        own = set(self.anchor_names(asset, lexicon)) | mine_lower
        understood = understood_words([*lexicon.present_markers, *lexicon.context_words])
        codes = {m.upper() for m in mine}
        non_market = set(lexicon.non_market_words)
        texts = [s for s, _ in sentences(text)]
        for k, sentence in enumerate(texts):
            words: list | None = [] if evidence is not None else None
            kinds = set(unresolved_market_mentions(
                sentence, own, codes, self._roots(), understood=understood,
                non_market=non_market, futures=self.execution.sec_type == "FUT",
                found_out=words))
            initial = first_word_mention(sentence, own, understood, non_market, codes,
                                         self._roots(), found_out=words)
            mention = kinds or initial is not None
            context = (instruction_word_in(sentence, ACTION_SEMANTIC_WORDS)
                       or price_like_level(sentence, markers)
                       or instruction_word_in(sentence, MENTION_ACTION_WORDS)
                       or has_phrase(sentence, MENTION_ACTION_PHRASES))
            if mention and context:
                if evidence is not None:
                    for word, kind in words or ():
                        evidence.add(kind.value, [word], "MENTION", sentence)
                found = sorted({*kinds, *([initial] if initial else [])}, key=lambda m: m.value)
                return [ENTRY_ANCHOR_POST_UNRESOLVED_MARKET, "RULE:MENTION_WITH_CONTEXT",
                        *(f"MENTION:{m.value}" for m in found)]
            if mention and k > 0 and texts[k - 1] in own_sentences and (
                    words_with_parts(sentence) & ADDITIVE_WORDS
                    or has_phrase(sentence, ADDITIVE_PHRASES)):
                return [ENTRY_ANCHOR_POST_UNRESOLVED_MARKET, "RULE:ADDITIVE_ELLIPSIS"]
        for sentence in texts:
            if sentence not in own_sentences and instruction_word_in(sentence):
                return [ENTRY_ANCHOR_POST_UNATTRIBUTED_ACTION, "RULE:UNATTRIBUTED_ACTION_SENTENCE"]
        return []

    def _month_spans(self, text: str) -> list[tuple[int, int]]:
        """A future's contract months in ``text``: left to the contract check."""
        if self.execution.sec_type != "FUT":
            return []
        return [*extract_terms(text).month_spans,
                *(m.span() for m in MONTH_WORD_RE.finditer(text))]

    def _whole_sentence_problem(self, sentence: str, question: bool, allowed, consumed,
                                fractions, unknown_out: list[str] | None = None) -> list[str]:
        """The whole-sentence allow-list a level's own sentence must pass (and, with
        ``standalone_level_sentence``, every own instruction sentence): not a question, every
        alphabetic token allow-listed, no number but the action's own levels and months."""
        if question:
            return [ENTRY_ANCHOR_CONTEXT_UNCLEAR, "QUESTION"]
        unknown = unknown_tokens(sentence, allowed, self._month_spans(sentence))
        if unknown:
            if unknown_out is not None:  # a local analysis tool's hook; never logged
                unknown_out += unknown
            return [ENTRY_ANCHOR_CONTEXT_UNCLEAR, f"WORDS:{len(unknown)}"]
        extra = [x for x in leftover_tokens(extract_terms(sentence), consumed, fractions,
                                            self._roots())
                 if any(ch.isdigit() for ch in x)
                 and not (x.startswith("~") and clause_numbers(x[1:]) <= consumed
                          and clause_numbers(x[1:]))]
        if extra:
            return [ENTRY_ANCHOR_CONTEXT_UNCLEAR, "SECOND_NUMBER"]
        return []

    def _sentence_problems(self, action, anchor, source, terms, levels, post_text, markers, own,
                           lexicon, unknown_out: list[str] | None = None,
                           attribution: list[str] | None = None,
                           evidence: _EvidenceSink | None = None) -> list[str]:
        """The final rule (last review pass): the scope of the marked number is its whole
        sentence. Every alphabetic token in it must be on the allow-list (markers, connectors,
        function words, units, this family's names, the action's own side, verb and level words,
        the private lexicon's context words); it may not be a question; and it may hold no other
        number than the action's own levels and contract months. A level sentence without the
        action's own instruction words also needs the sentence before it to be entirely on the
        allow-list: otherwise whose level it is cannot be told."""
        allowed, instruction, consumed, fractions = _allow_list(action, levels, markers, own,
                                                               lexicon)
        consumed.add(anchor)
        found = [(s, q) for s, q in sentences(source) if anchor in clause_numbers(s)]
        if not found:
            return [ENTRY_ANCHOR_NOT_IN_POST]
        whole = sentences(post_text) if post_text else sentences(source)
        for sentence, question in found:
            words: list[str] = []
            problem = self._whole_sentence_problem(sentence, question, allowed, consumed,
                                                   fractions, words)
            if unknown_out is not None:
                unknown_out += words
            if problem:
                if evidence is not None:
                    evidence.add("UNKNOWN_WORD", words, "LEVEL", sentence)
                return problem
            if unknown_words(sentence, instruction) == unknown_words(sentence, []):
                # A level sentence: the one before it must not name anything else.
                texts = [s for s, _ in whole]
                at = texts.index(sentence) if sentence in texts else -1
                before = texts[at - 1] if at > 0 else None
                own_block = {s for s, _ in sentences(terms.clause)}
                if self.entry_anchor.anchor_scope == "SINGLE_MARKET_POST":
                    case = "SINGLE_MARKET_POST"  # attributed by the whole post, checked before
                elif before is None:
                    case = "FIRST_SENTENCE"
                elif (self.entry_anchor.anchor_scope == "ACTION_BLOCK" and before in own_block):
                    # Final review: every sentence the interpreter folded into the action's clause
                    # passes the same whole-sentence allow-list as the level's sentence.
                    for s in own_block:
                        other = unknown_tokens(s, allowed, self._month_spans(s))
                        if other:
                            if unknown_out is not None:
                                unknown_out += other
                            if evidence is not None:
                                evidence.add("UNKNOWN_WORD", other, "INSTRUCTION", s)
                            return [ENTRY_ANCHOR_CONTEXT_UNCLEAR, "OWN_CLAUSE"]
                    # The interpreter delimited this action's clause; the level sentence follows it
                    # with nothing in between. Which sentence its stop or target came from is not
                    # exposed (PriceMention has no sentence index), so those are attributed only
                    # as allow-listed sentences, below.
                    case = "AFTER_OWN_INSTRUCTION"
                else:
                    other = unknown_tokens(before, allowed, self._month_spans(before))
                    if other:
                        if unknown_out is not None:
                            unknown_out += other
                        if evidence is not None:
                            evidence.add("UNKNOWN_WORD", other, "PRECEDING", before, sentence)
                        return [ENTRY_ANCHOR_INSTRUMENT_UNCLEAR, "PRECEDING_SENTENCE"]
                    case = "AFTER_ALLOW_LISTED_SENTENCE"
            else:
                case = "OWN_SENTENCE"
            if attribution is not None and case not in attribution:
                attribution.append(case)
        return []

    def anchor_names(self, asset: str, lexicon: AnchorLexicon | None = None) -> list[str]:
        """Lower-case names an anchor may be attributed to: the family, its reference series and
        aliases, and for a family compared in its execution series (DIRECT) the execution
        instrument's symbols too."""
        names = {asset, self.family.removesuffix(".OPT")}
        if self.reference is not None:
            names.add(self.reference.symbol)
        if self.basis not in ("BASIS_SHIFT", "REFERENCE_TRIGGER"):
            names |= {self.execution.symbol, *self.equivalent_roots}
        out = {n.lower() for n in names} | set(self.aliases)
        out |= set((lexicon or AnchorLexicon()).family_aliases.get(self.family, ()))
        return sorted(out)

    def unexplained_prices(self, action: TradeAction, terms: InstructionTerms, anchor: Decimal,
                           levels: list[tuple[str, str]] | None,
                           source: str | None = None) -> list[str]:
        """Numbers left in an anchored clause, and in the line its anchor is written in, once their
        months, an index's own name, the anchor, the stop and target (the action's, or the ones the
        interpreter tagged as such) and a reduction percentage are read. Any one HOLDs: a second
        unexplained price is where a misread would be most dangerous."""
        consumed, fractions = _consumed(action)
        consumed = consumed | {anchor}
        for role, value in levels or ():
            v = _level(value) if role in ("STOP", "TARGET") else None
            if v is not None:
                consumed.add(v)
        out = []
        for t in (terms, *([extract_terms(source)] if source and source != terms.clause else [])):
            out += [x for x in leftover_tokens(t, consumed, fractions, self._roots())
                    if any(ch.isdigit() for ch in x)
                    and not (x.startswith("~") and clause_numbers(x[1:]) <= consumed
                             and clause_numbers(x[1:]))]  # "~41.20": about the anchor itself
        return out

    def _select_option(self, asset, terms, held, action=None, post_text=None):
        x = self.execution
        if held is None or (action is not None and action.action == Action.ADD):
            # New exposure: the whole post, not only this clause, must be one leg.
            post = post_multi_leg(post_text)
            if post:
                return None, [OPTION_STRATEGY_NOT_SINGLE_LEG, *post]
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


_LINE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|\n+")


def _stop_not_unique(action, levels, others, text, trading_values) -> list[str]:
    """The family stop rule (owner, 12 November 2026): an entry's protective stop must be
    uniquely this action's; the bot never selects among competing stops. Checked on the
    interpreter's facts (the values tagged STOP for the action, the action's own stop, the values
    other trading actions of the post state) and on the text (every number in a clause with a
    stop word anywhere in the post, ``stop_values``). A repeated identical value is not a second
    stop. An ADD is checked on the text only: its stop is the idea's, checked at the gateway."""
    tagged = {v for role, raw in levels or () if role == "STOP" and (v := _level(raw)) is not None}
    own_stop = _level(action.stop_price) if action.stop_price is not None else None
    if len(tagged) > 1:
        return [PROTECTIVE_STOP_NOT_UNIQUE, "TWO_STOP_VALUES"]
    if own_stop is not None and tagged and own_stop not in tagged:
        return [PROTECTIVE_STOP_NOT_UNIQUE, "STOP_DIFFERS_FROM_ACTION"]
    stop_value = own_stop if own_stop is not None else next(iter(tagged), None)
    written = stop_values(text or "")
    if len(written) > 1:
        return [PROTECTIVE_STOP_NOT_UNIQUE, "SEVERAL_STOP_VALUES"]
    if written and stop_value is not None and written != {stop_value}:
        return [PROTECTIVE_STOP_NOT_UNIQUE, "STOP_TEXT_DIFFERS"]
    pool = trading_values if trading_values is not None else [v for _, _, v in others]
    if stop_value is not None and any(stop_value in vals for vals in pool):
        return [PROTECTIVE_STOP_NOT_UNIQUE, "STOP_SHARED_WITH_ANOTHER_ACTION"]
    return []


def _allow_list(action, levels, markers, own, lexicon):
    """The whole-sentence allow-list of an anchor's sentence (and of the sentence before a level
    sentence): markers, this family's names, the action's own side, verb and level words and the
    private context words; with the action's instruction words, its consumed levels and its
    reduction fractions."""
    instruction = list(SIDE_WORDS.get(getattr(action.position_side, "value", ""), ()))
    if action.action == Action.ADD:
        instruction += ADD_WORDS
    consumed, fractions = _consumed(action)
    for role, raw in levels or ():
        v = _level(raw) if role in ("STOP", "TARGET") else None
        if v is not None:
            consumed.add(v)
            instruction += ["stop", "stops"] if role == "STOP" else ["target", "targets"]
    if action.stop_price is not None:
        instruction += ["stop", "stops"]
    if action.target_price is not None:
        instruction += ["target", "targets"]
    allowed = [*markers, *own, *instruction, *lexicon.context_words]
    return allowed, instruction, consumed, fractions


# The holds a word in the private lexicon can resolve (the owner's review tool): only these ever
# carry private evidence or are proposed for review. An unattributed action sentence, an additive
# ellipsis, a stop refusal or long-only never is: no vocabulary decision may remove them.
_VOCABULARY_DETAILS = {
    ENTRY_ANCHOR_CONTEXT_UNCLEAR: None,  # any detail: WORDS:<n>, OWN_CLAUSE
    ENTRY_ANCHOR_INSTRUMENT_UNCLEAR: {"PRECEDING_SENTENCE"},  # or OTHER_WORDS:<n>
    ENTRY_ANCHOR_LEVEL_NOT_MARKET_QUOTE: {"FOLLOWING_NOT_UNDERSTOOD", "INSTRUCTION_NOT_UNDERSTOOD",
                                          "NEIGHBOUR_NOT_UNDERSTOOD"},
    ENTRY_ANCHOR_POST_UNRESOLVED_MARKET: {"RULE:MENTION_WITH_CONTEXT"},
}


def vocabulary_hold(code: str, detail) -> bool:
    """Is a hold with this public reason code and detail one a lexicon word can resolve?"""
    if code not in _VOCABULARY_DETAILS:
        return False
    wanted = _VOCABULARY_DETAILS[code]
    detail = [str(d) for d in detail or ()]
    if wanted is None:
        return True
    if code == ENTRY_ANCHOR_INSTRUMENT_UNCLEAR and any(d.startswith("OTHER_WORDS:")
                                                       for d in detail):
        return True
    if code == ENTRY_ANCHOR_POST_UNRESOLVED_MARKET and not any(d.startswith("RULE:")
                                                               for d in detail):
        return True  # a line logged before the rule detail existed
    return bool(wanted & set(detail))


def _post_lines(text: str | None) -> list[str]:
    """The post's lines and sentences, where a level line ("41.20 now.") stands on its own."""
    return [p.strip() for p in _LINE_SPLIT_RE.split(text or "") if p and p.strip()]


def _level(value) -> Decimal | None:
    try:
        v = Decimal(str(value).replace(",", "").lstrip("$"))
    except ArithmeticError:
        return None
    return v if v.is_finite() and v > 0 else None


def _consumed(action: TradeAction) -> tuple[set[Decimal], set[Decimal]]:
    """The numbers the interpreter already assigned to this action: its levels, and its
    reduction fraction."""
    levels, fractions = set(), set()
    for value, into in ((action.entry_reference, levels), (action.stop_price, levels),
                        (action.target_price, levels), (action.close_fraction, fractions)):
        try:
            if value is not None:
                into.add(Decimal(value))
        except ArithmeticError:
            continue
    return levels, fractions


def _named(entry: FamilyMapping, terms: InstructionTerms, today: date) -> bool:
    named, problems = entry._named_months(terms, today)
    return bool(named) and not problems


class MappingFileV2(StrictModel):
    version: Literal[2]
    # A what-if analysis file (an offline replay harness's invented entries, never an owner's
    # approval). ``MappingPolicy.from_toml`` refuses any file that carries this key unless its caller
    # passes ``allow_what_if=True``, which only the historical replay seam does.
    what_if: str | None = Field(default=None, min_length=1, max_length=200)
    mapping: list[FamilyMapping] = Field(default_factory=list)


class WhatIfMappingRefused(ValueError):
    """A what-if mapping file was offered where only an owner's mapping may be used."""


# ============================================================================ the policy


class MappingPolicy:
    def __init__(self, entries: list[MappingEntry | FamilyMapping], *, what_if: str | None = None):
        seen: dict[str, MappingEntry | FamilyMapping] = {}
        for e in entries:
            if e.source_asset in seen:
                raise ValueError(f"duplicate mapping for source asset {e.source_asset!r}")
            seen[e.source_asset] = e
        self.entries = seen
        # Set only for a what-if analysis file loaded with allow_what_if=True.
        self.what_if = what_if
        # The private lexicon extension's entry-anchor section (markers, aliases); empty unless
        # the runtime loads one.
        self.anchor_lexicon = AnchorLexicon()
        # The owner's review log (``[interpreter] review_log_path``): when True, a vocabulary
        # hold carries its private words and sentences on the decision (``vocab_evidence``, never
        # serialised). Off by default; it never changes a decision.
        self.collect_vocab_evidence = False

    def vocabulary(self) -> dict[str, tuple[set[str], set[str]]]:
        """Every family the mapping file and the private lexicon know: its codes and series
        symbols (matched as written) and its aliases (lower case, any case)."""
        out: dict[str, tuple[set[str], set[str]]] = {}
        for family, e in self.entries.items():
            codes = {family.removesuffix(".OPT")}
            if isinstance(e, FamilyMapping):
                codes |= {s.symbol for s in (e.reference, e.execution) if s is not None}
                codes |= set(e.equivalent_roots)
                aliases = set(e.aliases)
            else:
                codes.add(e.broker_symbol)
                aliases = set()
            out[family] = (codes, aliases)
        for family, names in self.anchor_lexicon.family_aliases.items():
            codes, aliases = out.get(family, (set(), set()))
            out[family] = (codes, aliases | set(names))
        return out

    def check_policy(self, policy) -> None:
        """Refuse a gateway policy under which an entry rule here could open without a stop."""
        collar = sorted(f for f, e in self.entries.items()
                        if getattr(e, "entry_rule", None) == "REFERENCE_AS_COLLAR")
        if collar and not policy.require_stop_on_open:
            raise ValueError(f"entry_rule = REFERENCE_AS_COLLAR ({', '.join(collar)}) needs "
                             "policy.require_stop_on_open = true: an anchored entry always "
                             "carries a protective stop")

    def check_mode(self, mode: str) -> None:
        """Refuse a runtime mode in which the entry rule may not run (an allow-list,
        ``ANCHOR_RULE_MODES``: the two simulated modes, and a future mode never inherits the rule
        silently)."""
        collar = sorted(f for f, e in self.entries.items()
                        if getattr(e, "entry_rule", None) == "REFERENCE_AS_COLLAR")
        if collar and mode not in ANCHOR_RULE_MODES:
            raise ValueError(f"entry_rule = REFERENCE_AS_COLLAR ({', '.join(collar)}) is paper "
                             f"and simulation only: runtime mode {mode!r} is not in "
                             f"{ANCHOR_RULE_MODES}")

    @classmethod
    def from_toml(cls, path: str | Path, *, allow_what_if: bool = False) -> MappingPolicy:
        """Load a mapping file. A file with a top-level ``what_if`` key is an analysis aid, not an
        owner's mapping: it is refused (``WhatIfMappingRefused``) unless ``allow_what_if``."""
        p = Path(path)
        try:
            raw = tomllib.loads(p.read_text(encoding="utf-8"))
            if "what_if" in raw and not allow_what_if:
                raise WhatIfMappingRefused(
                    f"instrument mapping file {p} is a what-if analysis mapping (what_if is set): "
                    "it is never an owner's approval and only an offline replay harness may load it"
                )
            if raw.get("version") == 2:
                parsed = MappingFileV2.model_validate(raw)
                return cls(parsed.mapping, what_if=parsed.what_if)
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

    def resolve(self, action: TradeAction, **kw) -> tuple[MappingDecision, Instrument | None]:
        """``_resolve``, then the entry rule's check of text that is not plain ASCII
        (``tradingbot.runtime.entry_text``), for an OPEN or ADD in a family with an entry rule
        (entry_rule = REFERENCE_AS_COLLAR or a family stop distance). It only ever turns a
        SUPPORTED decision into a hold: an invisible character or a number outside ASCII holds; a
        word holding a non-ASCII letter is decided once more as an unknown ASCII word (the shadow
        text) and the stricter decision wins. Pure ASCII text is decided exactly as before."""
        d, instrument = self._resolve(action, **kw)
        if d.status != "SUPPORTED" or action.action not in (Action.OPEN, Action.ADD):
            return d, instrument
        entry = self.entries.get(d.family) if d.family else None
        if not isinstance(entry, FamilyMapping) or not (
            entry.entry_rule == "REFERENCE_AS_COLLAR" or getattr(entry, "max_stop_distance", None)
        ):
            return d, instrument
        post_text, clause = kw.get("post_text"), kw.get("clause")
        others = list(kw.get("others") or ())
        texts = [post_text, clause, *(c for _, c, _ in others)]
        problem = entry_text_problem(*texts)
        out = d.model_dump(include={"action_id", "source_asset", "family", "broker_symbol",
                                    "basis", "anchor_level_kind"})
        if problem:
            return MappingDecision(status="UNRESOLVED", reason=problem, **out), None
        if not has_non_ascii_word(*texts):
            return d, instrument
        originals: dict[str, str] = {}

        def shadow_all(t):
            if not t:
                return t
            sh, orig = ascii_shadow(t)
            if not orig:
                return t
            mapping = {}
            for ph, word in orig.items():
                key = next((k for k, w in originals.items() if w == word), None)
                if key is None:
                    key = entry_text_placeholder(len(originals), ph[0].isupper())
                    originals[key] = word
                mapping[ph] = key
            return re.sub(r"[Qq]zu[a-z]{3}x", lambda m: mapping.get(m.group(0), m.group(0)), sh)

        kw2 = dict(kw)
        kw2["post_text"], kw2["clause"] = shadow_all(post_text), shadow_all(clause)
        kw2["others"] = [(a, shadow_all(c), v) for a, c, v in others]
        d2, _ = self._resolve(action, **kw2)
        if d2.status == "SUPPORTED":
            return d, instrument
        held = MappingDecision(status=d2.status, reason=restore(d2.reason, originals),
                               **d2.model_dump(include={"action_id", "source_asset", "family",
                                                        "broker_symbol", "basis",
                                                        "anchor_level_kind"}))
        ev = d2.vocab_evidence
        if ev is not None:
            held._vocab_evidence = VocabEvidence(
                [(restore(w, originals), k) for w, k in ev.tokens],
                [(r, restore(t, originals)) for r, t in ev.sentences])
        return held, None

    def _resolve(
        self,
        action: TradeAction,
        *,
        clause: str | None = None,
        now: datetime | None = None,
        held: dict[str, ContractRef] | None = None,
        recorded: dict[str, FamilyMapping] | None = None,
        post_text: str | None = None,
        levels: list[tuple[str, str]] | None = None,
        others: list[tuple[str | None, str | None, set[Decimal]]] = (),
        post_actions: list[TradeAction] | None = None,
        trading_values: list[set[Decimal]] | None = None,
    ) -> tuple[MappingDecision, Instrument | None]:
        """One action's mapping decision and, when SUPPORTED, its execution instrument.
        ``clause`` is the instruction text the action came from (contract terms are read from it);
        ``held`` maps follower idea ids to their contracts, so a follow-up acts on its own;
        ``recorded`` maps them to the entry each was opened under. Approval governs new exposure
        only: a REDUCE, CLOSE or MODIFY on an open idea whose family is no longer approved (or no
        longer has an entry) uses its recorded entry, marked exits-only. ``levels`` (the
        interpreter's price mentions for this action, as (role, value)) and ``others`` (every
        other action of the post: asset, clause, stated values) are read only by an entry with
        entry_rule = REFERENCE_AS_COLLAR (``entry_anchor_for``)."""
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
        if getattr(entry, "long_only", False) and action.action in (Action.OPEN, Action.ADD) \
                and action.position_side == PositionSide.SHORT:
            return MappingDecision(status="UNRESOLVED", reason=MAPPING_LONG_ONLY, **out), None
        if not revoked and getattr(entry, "max_stop_distance", None) \
                and action.action in (Action.OPEN, Action.ADD):
            problem = _stop_not_unique(action, levels, others, post_text or clause,
                                       trading_values)
            if problem:
                return MappingDecision(status="UNRESOLVED", reason=",".join(problem), **out), None
        if action.instrument is not None and action.instrument.symbol not in (
            asset, entry.execution.symbol
        ):
            return MappingDecision(
                status="UNSUPPORTED", reason="INTERPRETED_INSTRUMENT_DIFFERS_FROM_MAPPING", **out
            ), None
        today = (now or datetime.now(UTC)).astimezone(
            ZoneInfo(entry.session.timezone if entry.session else "UTC")
        ).date()
        attribution: list[str] = []
        kind: list[AnchorLevelKind] = []
        sink = _EvidenceSink() if self.collect_vocab_evidence else None
        anchor, source, problems = (None, None, []) if revoked else entry.entry_anchor_for(
            action, asset, terms, levels, others, post_text, self.anchor_lexicon, attribution,
            {"actions": post_actions, "vocabulary": self.vocabulary(),
             "trading_values": trading_values}, kind_out=kind, evidence=sink)
        if kind:
            out["anchor_level_kind"] = kind[0].value
        if problems:
            d = MappingDecision(status="UNRESOLVED", reason=",".join(problems), **out)
            if sink is not None and vocabulary_hold(problems[0], problems[1:]):
                d._vocab_evidence = sink.frozen()
            return d, None
        instrument, problems = entry.select(action, asset, terms, today, held_contract, post_text,
                                            anchor, source)
        if instrument is None:
            return MappingDecision(
                status="UNRESOLVED", reason=",".join(problems), **out
            ), None
        if anchor is not None:
            extra = entry.unexplained_prices(action, terms, anchor, levels, source)
            if extra:
                return MappingDecision(status="UNRESOLVED", reason=",".join(
                    [ENTRY_ANCHOR_UNEXPLAINED_PRICE,
                     *(f"UNREAD_TOKEN:{t.replace(',', '')}" for t in extra)]),
                    **out), None
        month_source = None
        if instrument.asset_class == AssetClass.FUTURE:
            if held_contract is not None:
                month_source = "IDEA"
            elif _named(entry, terms, today):
                month_source = "NAMED"
            else:
                month_source = ("FRONT_MONTH" if entry.level_contract_rule == "FRONT_MONTH"
                                else "NEAREST")
        return MappingDecision(status="SUPPORTED", mapped=not entry.legacy_equivalent,
                               under_revoked_mapping=revoked,
                               entry_anchor=str(anchor) if anchor is not None else None,
                               anchor_attribution=(",".join(attribution)
                                                   if anchor is not None and attribution
                                                   else None),
                               month_source=month_source, **out), instrument

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
        post_text: str | None = None,
        levels: dict[str, list[tuple[str, str]]] | None = None,
    ) -> tuple[ParsedPost, list[MappingDecision]]:
        """Return the post with every action that may not execute marked for HOLD, and one
        decision per action. A supported action gets its execution instrument. ``clauses`` maps
        action ids to their instruction text (contract months and option terms are read there).
        ``levels`` maps action ids to the interpreter's price mentions, as (role, value): only an
        entry with entry_rule = REFERENCE_AS_COLLAR reads them (its entry anchor)."""
        actions: list[TradeAction] = []
        decisions: list[MappingDecision] = []
        clauses, levels = clauses or {}, levels or {}
        stated = {a.action_id: {v for _, raw in levels.get(a.action_id, ())
                                if (v := _level(raw)) is not None} for a in parsed.actions}
        for a in parsed.actions:
            others = [(b.source_market_symbol, clauses.get(b.action_id), stated[b.action_id])
                      for b in parsed.actions if b.action_id != a.action_id]
            d, instrument = self.resolve(
                a, clause=clauses.get(a.action_id), now=now, held=held, recorded=recorded,
                post_text=post_text, levels=levels.get(a.action_id), others=others,
                post_actions=list(parsed.actions),
                trading_values=[stated[b.action_id] for b in parsed.actions
                                if b.action_id != a.action_id and b.action != Action.NO_TRADE],
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
