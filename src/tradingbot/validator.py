"""Deterministic policy and preflight: the ONLY authorization gateway. No AI decision is trusted here.

Two engines call in, and both receive identical decisions for identical inputs
(tests/test_gateway_equivalence.py proves it):

* The Sprint 1 pipeline (storage.Journal.commit) uses the compatibility wrapper ``validate()``,
  which lifts a SignalCandidate/SourceEvent into the Sprint 1.1 action schema and translates the
  canonical result into the legacy Decision vocabulary (EXECUTE_MOCK / REVIEW / BLOCK / NO_TRADE).
* The Sprint 1.1 replay coordinator (event_replay.PaperEventReplay) calls ``gate_source()`` for
  post-level provenance and ``gate_action()`` per action.

Sizing comes from MockPolicy through ``fill_quantity()``; no LLM-produced field carries a
quantity, venue or account. Engines apply fictional fills only after an APPROVE.
"""
from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from tradingbot.event_models import PriceBasis, ReplaySource, Timing, TradeAction
from tradingbot.models import (
    Action,
    AssetClass,
    Decision,
    ExecutionPlan,
    GateDecision,
    LegQuote,
    MappedQuote,
    MarketTerms,
    MockPolicy,
    Position,
    PositionSide,
    Quote,
    SignalCandidate,
    SourceEvent,
)

SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.]{0,9}$")
ALL_ACTIONS = frozenset({Action.OPEN, Action.ADD, Action.REDUCE, Action.CLOSE, Action.MODIFY})
# The Sprint 1 journal can only fill opens, closes and reductions; ADD/MODIFY stay under review there.
SPRINT1_ACTIONS = frozenset({Action.OPEN, Action.CLOSE, Action.REDUCE})

# Canonical reason codes -> the Sprint 1 names the legacy pipeline, dashboard and tests expect.
# Only codes whose spelling differs are listed; everything else passes through unchanged.
LEGACY_REASON_CODES = {
    "CONDITIONAL_TRIGGER_NOT_IMPLEMENTED": "CONDITIONAL_SIGNAL_UNSUPPORTED",
    "NOT_EXPLICITLY_AUTHORITATIVE": "NO_EXPLICIT_VERIFIABLE_INSTRUCTION",
    "SOURCE_AMBIGUITIES": "AMBIGUOUS_SOURCE",
    "ORDERABLE_INSTRUMENT_MISSING": "MISSING_INSTRUMENT",
    "ASSET_CLASS_PAPER_ADAPTER_UNSUPPORTED": "MOCK_EXECUTION_EQUITIES_ONLY",
    "ACTION_UNSUPPORTED": "ACTION_UNSUPPORTED_IN_SPRINT_1",
    "SOURCE_TIMEZONE_MISSING": "SOURCE_TIMESTAMP_MISSING_TIMEZONE",
    "SOURCE_STALE_OR_FUTURE": "STALE_OR_FUTURE_SOURCE",
    "QUOTE_STALE_OR_FUTURE": "STALE_OR_FUTURE_QUOTE",
    "OPEN_ENTRY_PRICE_MISSING": "NO_EXPLICIT_ENTRY_REFERENCE",
    "ENTRY_PRICE_COLLAR": "PRICE_COLLAR_BREACH",
    "STOP_DIRECTION_CONTRADICTION": "STOP_SIDE_CONTRADICTION",
    "TARGET_DIRECTION_CONTRADICTION": "TARGET_SIDE_CONTRADICTION",
    "NOTIONAL_LIMIT": "MOCK_NOTIONAL_CAP",
    "REFERENCED_ACTIVE_IDEA_NOT_FOUND": "REFERENCED_POSITION_NOT_FOUND",
    "IDEA_INSTRUMENT_MISMATCH": "POSITION_INSTRUMENT_MISMATCH",
    "REDUCTION_FRACTION_UNDEFINED": "INVALID_REDUCE_FRACTION",
    "NON_INTEGER_REDUCTION": "NON_INTEGER_REDUCTION_IN_MOCK",
    "PAPER_OPEN": "PREFLIGHT_OK_MOCK_ONLY",
    "PAPER_REDUCE_ONLY": "REDUCE_ONLY_MOCK_OK",
}
LEGACY_STATUS = {
    "APPROVE": "EXECUTE_MOCK", "HOLD": "REVIEW", "BLOCK": "BLOCK", "NO_TRADE": "NO_TRADE",
    "RECONCILE": "REVIEW",  # unreachable from SignalCandidate (no timing field); mapped defensively
}


def _num(value: str | None) -> Decimal | None:
    """Positive, finite decimal or None. Never raises on model-produced text."""
    if value is None:
        return None
    try:
        result = Decimal(value)
    except (InvalidOperation, ValueError, TypeError):
        return None
    return result if result.is_finite() and result > 0 else None


def _gate(status: str, *reasons: str) -> GateDecision:
    return GateDecision(status=status, reasons=list(reasons))


# ----------------------------------------------------------------------------- post-level provenance

def gate_source(source: ReplaySource, now: datetime, *, is_edit: bool = False) -> GateDecision | None:
    """Provenance rules that apply to every action of a post. None means the post may be evaluated.

    `is_edit` is supplied by the coordinator from its ledger (another revision of the same post is
    already on record). Revised posts are held for review rather than silently altering positions.
    """
    if is_edit or source.revision != 1:
        return _gate("HOLD", "SOURCE_EDIT_REQUIRES_REVIEW")
    if not source.verified_author:
        return _gate("BLOCK", "SOURCE_NOT_AUTHENTICATED")
    if source.is_repost:
        return _gate("NO_TRADE", "REPOST_NOT_AUTHOR_INSTRUCTION")
    if not source.complete_text:
        return _gate("HOLD", "SOURCE_TEXT_TRUNCATED")
    if source.published_at.tzinfo is None or source.received_at.tzinfo is None or now.tzinfo is None:
        return _gate("BLOCK", "SOURCE_TIMEZONE_MISSING")
    if source.received_at < source.published_at:
        return _gate("BLOCK", "SOURCE_RECEIPT_BEFORE_PUBLICATION")
    return None


# ----------------------------------------------------------------------------- freshness

def source_is_fresh(published_at: datetime, now: datetime, policy: MockPolicy) -> bool:
    """The one source-freshness rule: published no more than ``max_signal_age_seconds`` ago and not
    more than 5 s in the future. ``gate_action`` applies it; the execution core re-applies it before
    re-sending an entry whose first submission outcome was unknown."""
    if published_at.tzinfo is None or now.tzinfo is None:
        return False
    age = (now - published_at).total_seconds()
    return -5 <= age <= policy.max_signal_age_seconds


# ----------------------------------------------------------------------------- per-action preflight

def gate_action(
    action: TradeAction,
    published_at: datetime,
    quote: Quote | None,
    policy: MockPolicy,
    position: Position | None,
    *,
    now: datetime,
    supported_actions: frozenset[Action] = ALL_ACTIONS,
) -> GateDecision:
    """Decide one action. `position` is the ledger's active position for `action.idea_reference`
    (None when there is none). `supported_actions` is engine capability, not risk policy."""
    a = action
    # 1. Temporal intent: only a current instruction can authorize anything.
    if a.timing == Timing.HISTORICAL:
        return _gate("RECONCILE", "HISTORICAL_REPORT_NO_ORDER")
    if a.timing == Timing.CONDITIONAL:
        return _gate("HOLD", "CONDITIONAL_TRIGGER_NOT_IMPLEMENTED")
    if a.timing == Timing.COMMENTARY or a.action == Action.NO_TRADE:
        return _gate("NO_TRADE", "COMMENTARY_OR_SUMMARY")
    if a.timing != Timing.CURRENT:
        return _gate("HOLD", "TEMPORAL_INTENT_UNCERTAIN")
    # 2. Authority of the extracted instruction.
    if not a.explicit_instruction or not a.evidence:
        return _gate("HOLD", "NOT_EXPLICITLY_AUTHORITATIVE")
    if a.ambiguities:
        return _gate("HOLD", "SOURCE_AMBIGUITIES", *a.ambiguities)
    # 3. Instrument identity: the quoted level must belong to the orderable contract.
    if a.instrument is None:
        return _gate("HOLD", "ORDERABLE_INSTRUMENT_MISSING")
    inst = a.instrument
    if isinstance(quote, MappedQuote):
        # An owner-approved mapping (runtime/mapping.py, version 2) relates the publisher's series
        # to the execution contract; every price is checked in the series it belongs to.
        return _gate_mapped(a, published_at, quote, policy, position, now, supported_actions)
    if a.price_basis != PriceBasis.TRADED_CONTRACT:
        return _gate("HOLD", "SOURCE_TO_BROKER_PRICE_BASIS_UNRESOLVED")
    if not a.source_market_symbol:
        return _gate("HOLD", "SOURCE_PRICE_INSTRUMENT_IDENTITY_UNVERIFIED")
    if a.source_market_symbol != inst.symbol:
        return _gate("HOLD", "SOURCE_TO_BROKER_MAPPING_UNAPPROVED")
    if inst.asset_class not in (AssetClass.EQUITY, AssetClass.ETF):
        return _gate("HOLD", "ASSET_CLASS_PAPER_ADAPTER_UNSUPPORTED")
    if not SYMBOL_RE.fullmatch(inst.symbol):
        return _gate("BLOCK", "INVALID_SYMBOL")
    if policy.allowed_symbols is not None and inst.symbol not in policy.allowed_symbols:
        return _gate("BLOCK", "INSTRUMENT_NOT_ALLOWLISTED")
    if a.action not in supported_actions:
        return _gate("HOLD", "ACTION_UNSUPPORTED")
    # 4. Freshness of source and quote; quote integrity.
    if published_at.tzinfo is None:
        return _gate("BLOCK", "SOURCE_TIMEZONE_MISSING")
    if not source_is_fresh(published_at, now, policy):
        return _gate("BLOCK", "SOURCE_STALE_OR_FUTURE")
    if quote is None or quote.observed_at.tzinfo is None:
        return _gate("BLOCK", "QUOTE_UNAVAILABLE")
    if quote.symbol.upper() != inst.symbol:
        return _gate("BLOCK", "QUOTE_INSTRUMENT_MISMATCH")
    quote_age = (now - quote.observed_at).total_seconds()
    if quote_age < -5 or quote_age > policy.max_quote_age_seconds:
        return _gate("BLOCK", "QUOTE_STALE_OR_FUTURE")
    if quote.bid <= 0 or quote.ask <= 0 or quote.ask < quote.bid:
        return _gate("BLOCK", "INVALID_BID_ASK")
    spread_pct = (quote.ask - quote.bid) / ((quote.bid + quote.ask) / 2) * 100
    if spread_pct > policy.max_spread_pct:
        return _gate("BLOCK", "SPREAD_TOO_WIDE")
    if a.action == Action.OPEN:
        return _gate_open(a, quote, policy)
    return _gate_follow_up(a, quote, policy, position)


def _gate_open(a: TradeAction, quote: Quote, policy: MockPolicy) -> GateDecision:
    if a.idea_reference is not None:
        return _gate("HOLD", "OPEN_SHOULD_NOT_REFERENCE_EXISTING_IDEA")
    if a.position_side is None:
        return _gate("HOLD", "DIRECTION_MISSING")
    long = a.position_side == PositionSide.LONG
    ref = _num(a.entry_reference)
    if ref is None:
        return _gate("HOLD", "OPEN_ENTRY_PRICE_MISSING")
    price = quote.ask if long else quote.bid
    if abs(price - ref) / ref * 100 > policy.max_price_deviation_pct:
        return _gate("BLOCK", "ENTRY_PRICE_COLLAR")
    stop = _num(a.stop_price)
    target = _num(a.target_price)
    if a.stop_price is not None and stop is None:
        return _gate("BLOCK", "INVALID_STOP_PRICE")
    if a.target_price is not None and target is None:
        return _gate("BLOCK", "INVALID_TARGET_PRICE")
    if policy.require_stop_on_open and stop is None:
        return _gate("HOLD", "PROTECTIVE_STOP_MISSING")
    if stop is not None and (stop >= ref if long else stop <= ref):
        return _gate("BLOCK", "STOP_DIRECTION_CONTRADICTION")
    if stop is not None and (stop >= quote.bid if long else stop <= quote.ask):
        return _gate("BLOCK", "STOP_ALREADY_CROSSED_AT_ENTRY")
    if target is not None and (target <= ref if long else target >= ref):
        return _gate("BLOCK", "TARGET_DIRECTION_CONTRADICTION")
    if price * policy.quantity > policy.max_notional_usd:
        return _gate("BLOCK", "NOTIONAL_LIMIT")
    return _gate("APPROVE", "PAPER_OPEN")


def _gate_follow_up(a: TradeAction, quote: Quote, policy: MockPolicy, position: Position | None) -> GateDecision:
    inst = a.instrument
    assert inst is not None
    if not a.idea_reference or position is None or position.idea_id != a.idea_reference or position.quantity <= 0:
        return _gate("HOLD", "REFERENCED_ACTIVE_IDEA_NOT_FOUND")
    if position.symbol != inst.symbol or position.asset_class != inst.asset_class:
        return _gate("BLOCK", "IDEA_INSTRUMENT_MISMATCH")
    if a.position_side is not None and a.position_side != position.side:
        return _gate("BLOCK", "IDEA_SIDE_MISMATCH")
    long = position.side == PositionSide.LONG
    if a.action in (Action.CLOSE, Action.REDUCE):
        if a.action == Action.REDUCE:
            fraction = _num(a.close_fraction)
            if fraction is None or fraction > 1:
                return _gate("HOLD", "REDUCTION_FRACTION_UNDEFINED")
            units = Decimal(position.quantity) * fraction
            if units < 1 or units != units.to_integral_value():
                return _gate("HOLD", "NON_INTEGER_REDUCTION")
        return _gate("APPROVE", "PAPER_REDUCE_ONLY")
    if a.action == Action.ADD:
        prior_stop = _num(position.stop_price)
        if prior_stop is not None and (prior_stop >= quote.bid if long else prior_stop <= quote.ask):
            return _gate("HOLD", "EXISTING_STOP_ALREADY_CROSSED")
        ref = _num(a.entry_reference)
        if ref is None:
            return _gate("HOLD", "ADD_ENTRY_REFERENCE_MISSING")
        price = quote.ask if long else quote.bid
        if abs(price - ref) / ref * 100 > policy.max_price_deviation_pct:
            return _gate("BLOCK", "ENTRY_PRICE_COLLAR")
        if price * (position.quantity + policy.quantity_per_add) > policy.max_notional_usd:
            return _gate("BLOCK", "NOTIONAL_LIMIT")
        return _gate("APPROVE", "PAPER_ADD")
    if a.action == Action.MODIFY:
        if a.stop_price is None and a.target_price is None:
            return _gate("HOLD", "NO_FIELDS_TO_MODIFY")
        stop = _num(a.stop_price)
        target = _num(a.target_price)
        if a.stop_price is not None and stop is None:
            return _gate("BLOCK", "INVALID_STOP_PRICE")
        if a.target_price is not None and target is None:
            return _gate("BLOCK", "INVALID_TARGET_PRICE")
        if stop is not None:
            old_stop = _num(position.stop_price)
            if old_stop is not None and (stop < old_stop if long else stop > old_stop):
                return _gate("HOLD", "STOP_WIDENING_REQUIRES_POLICY_REVIEW")
            if stop >= quote.bid if long else stop <= quote.ask:
                return _gate("BLOCK", "STOP_ON_WRONG_SIDE_OF_MARKET")
        if target is not None and (target <= quote.bid if long else target >= quote.ask):
            return _gate("BLOCK", "TARGET_ON_WRONG_SIDE_OF_MARKET")
        return _gate("APPROVE", "PAPER_PROTECTION_AMENDMENT")
    return _gate("HOLD", "UNKNOWN_ACTION")


# ----------------------------------------------------------------------------- mapped families
#
# Reached only with a ``MappedQuote``: the runtime built it from an owner-approved mapping entry,
# the selected execution contract and the broker's quotes of both legs. A stock or ETF quoted in
# its own price never gets one, so its path above is unchanged. Treatments:
#   DIRECT             levels are the execution instrument's own prices.
#   BASIS_SHIFT        levels are converted with the basis (execution mid - reference mid) seen at
#                      this decision from two live, fresh, near-simultaneous quotes, within a cap.
#   REFERENCE_TRIGGER  the entry is priced from the execution quote; stop and target stay in
#                      reference levels and are held as broker-side conditions on the reference.
#   UNSUPPORTED        always HOLD.
# A reference level is never used as an execution price.

ROLL_WINDOW = "CONTRACT_IN_ROLL_WINDOW"
OUTSIDE_SESSION = "OUTSIDE_PERMITTED_SESSION"
MAPPING_NOT_APPROVED = "MAPPING_NOT_APPROVED"
UNSUPPORTED_MAPPING = "UNSUPPORTED_INSTRUMENT_MAPPING"
# An amendment kept a working protective level rather than loosen it (the plan records which).
PROTECTIVE_LEVEL_NOT_LOOSENED = "PROTECTIVE_LEVEL_NOT_LOOSENED"


def business_days_until(today: date, target: date, holidays=()) -> int:
    """Business days d with today < d <= target (weekends and ``holidays`` excluded); negative
    when ``target`` has passed."""
    if target < today:
        return -business_days_until(target, today, holidays)
    n, d, off = 0, today, set(holidays)
    while d < target:
        d += timedelta(days=1)
        if d.weekday() < 5 and d not in off:
            n += 1
    return n


def in_roll_window(t: MarketTerms, now: datetime) -> bool:
    """Inside the last ``roll_business_days`` business days before the contract's roll date (or
    past it): no new exposure may be taken in it."""
    if t.roll_date is None or t.roll_business_days is None:
        return False
    today = now.astimezone(ZoneInfo(t.session_timezone)).date()
    return business_days_until(today, t.roll_date, t.holidays) <= t.roll_business_days


def in_windows(windows, days, tz: str, now: datetime) -> bool:
    local = now.astimezone(ZoneInfo(tz))
    minute = local.hour * 60 + local.minute
    return local.weekday() in days and any(a <= minute < b for a, b in windows)


def _mid(leg: LegQuote) -> Decimal | None:
    if leg.bid is not None and leg.ask is not None and leg.bid > 0 and leg.ask >= leg.bid:
        return (leg.bid + leg.ask) / 2
    return leg.last if leg.last is not None and leg.last > 0 else None


# A reference series that cannot be shown to be updating now (review F5): its value is never used.
REFERENCE_NOT_FRESH = "REFERENCE_QUOTE_NOT_FRESH"
_NOT_FRESH = frozenset({"STALE", "UPDATE_TIME_UNKNOWN", "CROSSED", "OUTSIDE_HOURS"})


def _leg_problem(leg: LegQuote | None, t: MarketTerms, now: datetime, max_age: float,
                 *, need_touch: bool = True, reference: bool = False) -> str | None:
    """Why a quote cannot be used, or None. A ``reference`` leg is judged by the series' own
    last-update time (never by when the quote was received), must not be crossed, and must fall
    inside the series' declared hours when the entry declares them."""
    if leg is None or leg.quality == "UNAVAILABLE":
        return "UNAVAILABLE"
    if leg.quality not in t.accepted_qualities:
        return f"QUALITY_{leg.quality}"
    age = (now - leg.observed_at).total_seconds()
    if age < -5 or age > max_age:
        return "STALE"
    if reference:
        if leg.updated_at is None:
            return "UPDATE_TIME_UNKNOWN"
        updated = (now - leg.updated_at).total_seconds()
        if updated < -5 or updated > max_age:
            return "STALE"
        if leg.bid is not None and leg.ask is not None and leg.bid > leg.ask:
            return "CROSSED"
        if t.reference_windows and not in_windows(t.reference_windows, t.session_days,
                                                  t.session_timezone, now):
            return "OUTSIDE_HOURS"
    if need_touch and (leg.bid is None or leg.ask is None or leg.bid <= 0 or leg.ask < leg.bid):
        return "INVALID_BID_ASK"
    if _mid(leg) is None:
        return "NO_PRICE"
    return None


def _leg_reasons(name: str, problem: str) -> list[str]:
    reason = f"{name}_QUOTE_{problem}"
    return [REFERENCE_NOT_FRESH, reason] if name == "REFERENCE" and problem in _NOT_FRESH \
        else [reason]


def _touch(leg: LegQuote, buy: bool) -> Decimal:
    """The price a buy (ask) or sell (bid) trades against; a reference series that reports only a
    last price uses that."""
    side = leg.ask if buy else leg.bid
    return side if side is not None and side > 0 else (leg.last or Decimal(0))


def _basis(q: MappedQuote, now: datetime, cap: Decimal | None = None,
           max_age: float | None = None) -> tuple[ExecutionPlan | None, list[str]]:
    """The basis observed now from two live, fresh, near-simultaneous quotes, within its cap
    (the terms' basis-shift cap unless ``cap`` and ``max_age`` are given)."""
    t = q.terms
    if cap is None:
        cap, max_age = t.max_abs_basis, t.max_basis_age_seconds
    age = max_age or 0
    execution = LegQuote(contract_key=t.execution_key, bid=q.bid, ask=q.ask,
                         observed_at=q.observed_at, quality=q.quality)
    for name, leg in (("EXECUTION", execution), ("REFERENCE", q.reference)):
        problem = _leg_problem(leg, t, now, age, need_touch=(name == "EXECUTION"),
                               reference=(name == "REFERENCE"))
        if problem:
            return None, ["BASIS_QUOTE_UNUSABLE", *_leg_reasons(name, problem)]
    # The two prices must describe the same moment: the reference at its own update time.
    skew = abs((execution.observed_at - q.reference.updated_at).total_seconds())
    if skew > age:
        return None, ["BASIS_QUOTE_UNUSABLE", "QUOTES_NOT_SIMULTANEOUS"]
    em, rm = _mid(execution), _mid(q.reference)
    basis = em - rm
    if cap is None or abs(basis) > cap:
        return None, ["BASIS_EXCEEDS_CAP", f"BASIS:{basis}"]
    plan = ExecutionPlan(family=t.family, basis=t.basis, execution_key=t.execution_key,
                         basis_value=basis, basis_execution_mid=em, basis_reference_mid=rm,
                         basis_observed_at=min(execution.observed_at, q.reference.updated_at))
    return plan, []


def _snap(price: Decimal, tick: Decimal, rounding: str) -> Decimal:
    return (price / tick).to_integral_value(rounding) * tick


def _shift_protection(level: Decimal, basis: Decimal, tick: Decimal, long: bool, kind: str):
    """A converted stop moves to the tick nearer the market side that protects the position (never
    looser); a converted target to the tick nearer the entry (never further)."""
    raw = level + basis
    tighter = ROUND_CEILING if long else ROUND_FLOOR
    nearer = ROUND_FLOOR if long else ROUND_CEILING
    return _snap(raw, tick, tighter if kind == "stop" else nearer)


def _gate_mapped(a: TradeAction, published_at: datetime, q: MappedQuote, policy: MockPolicy,
                 position: Position | None, now: datetime,
                 supported_actions: frozenset[Action]) -> GateDecision:
    t = q.terms
    inst = a.instrument
    assert inst is not None
    if not t.approved:
        return _gate("HOLD", MAPPING_NOT_APPROVED, f"FAMILY:{t.family}")
    if t.exits_only and a.action in (Action.OPEN, Action.ADD):
        # The idea's recorded terms let it be reduced, closed and protected; approval governs new
        # exposure and has been withdrawn.
        return _gate("HOLD", MAPPING_NOT_APPROVED, f"FAMILY:{t.family}", "EXITS_ONLY")
    if t.basis == "UNSUPPORTED":
        return _gate("HOLD", UNSUPPORTED_MAPPING, f"FAMILY:{t.family}")
    if t.problems:
        return _gate("HOLD", *t.problems)
    if a.source_market_symbol != t.family.removesuffix(".OPT"):
        return _gate("HOLD", "SOURCE_TO_BROKER_MAPPING_UNAPPROVED")
    if a.price_basis == PriceBasis.UNKNOWN or (
            t.basis == "DIRECT" and a.price_basis != PriceBasis.TRADED_CONTRACT
            and not (t.same_series and a.price_basis == PriceBasis.SOURCE_SERIES)):
        # DIRECT means the levels are the traded contract's own prices: levels the interpreter
        # marked as another series are refused, exactly as on the stock path, unless the owner
        # declared that series and the execution price one series (same_series = true). Every
        # check below is then made in the execution series.
        return _gate("HOLD", "SOURCE_TO_BROKER_PRICE_BASIS_UNRESOLVED")
    if inst.quote_key != t.execution_key or inst.symbol != t.execution_symbol:
        return _gate("BLOCK", "QUOTE_INSTRUMENT_MISMATCH")
    if not SYMBOL_RE.fullmatch(inst.symbol):
        return _gate("BLOCK", "INVALID_SYMBOL")
    if policy.allowed_symbols is not None and inst.symbol not in policy.allowed_symbols:
        return _gate("BLOCK", "INSTRUMENT_NOT_ALLOWLISTED")
    if a.action not in supported_actions:
        return _gate("HOLD", "ACTION_UNSUPPORTED")
    if published_at.tzinfo is None:
        return _gate("BLOCK", "SOURCE_TIMEZONE_MISSING")
    if not source_is_fresh(published_at, now, policy):
        return _gate("BLOCK", "SOURCE_STALE_OR_FUTURE")
    if (position is not None and position.contract_key and t.execution_contract_key
            and position.contract_key != t.execution_contract_key):
        return _gate("BLOCK", "IDEA_CONTRACT_MISMATCH")
    trades_now = a.action in (Action.OPEN, Action.ADD, Action.REDUCE, Action.CLOSE)
    if trades_now and not in_windows(t.permitted_windows, t.session_days, t.session_timezone, now):
        return _gate("HOLD", OUTSIDE_SESSION)
    if a.action in (Action.OPEN, Action.ADD) and in_roll_window(t, now):
        return _gate("HOLD", ROLL_WINDOW, f"ROLL_DATE:{t.roll_date}")
    # The execution instrument's own quote.
    execution = LegQuote(contract_key=t.execution_key, bid=q.bid, ask=q.ask,
                         observed_at=q.observed_at, quality=q.quality)
    problem = _leg_problem(execution, t, now, policy.max_quote_age_seconds)
    if problem:
        legacy = {"UNAVAILABLE": "QUOTE_UNAVAILABLE", "STALE": "QUOTE_STALE_OR_FUTURE",
                  "INVALID_BID_ASK": "INVALID_BID_ASK", "NO_PRICE": "QUOTE_UNAVAILABLE"}
        return _gate("BLOCK", legacy.get(problem, f"QUOTE_{problem}"))
    spread_pct = (q.ask - q.bid) / ((q.bid + q.ask) / 2) * 100
    if spread_pct > policy.max_spread_pct:
        return _gate("BLOCK", "SPREAD_TOO_WIDE")
    if a.action == Action.OPEN:
        return _mapped_open(a, q, policy, now, published_at)
    return _mapped_follow_up(a, q, policy, position, now, published_at)


def _reference_leg(q: MappedQuote, policy: MockPolicy, now: datetime) -> GateDecision | None:
    problem = _leg_problem(q.reference, q.terms, now, policy.max_quote_age_seconds,
                           need_touch=False, reference=True)
    return _gate("HOLD", *_leg_reasons("REFERENCE", problem)) if problem else None


def _mapped_levels(a: TradeAction) -> tuple[Decimal | None, Decimal | None, GateDecision | None]:
    stop, target = _num(a.stop_price), _num(a.target_price)
    if a.stop_price is not None and stop is None:
        return None, None, _gate("BLOCK", "INVALID_STOP_PRICE")
    if a.target_price is not None and target is None:
        return None, None, _gate("BLOCK", "INVALID_TARGET_PRICE")
    return stop, target, None


def _off_tick(t: MarketTerms, **prices) -> GateDecision | None:
    bad = [f"{k.upper()}_PRICE_OFF_TICK" for k, p in prices.items()
           if p is not None and p % t.min_tick != 0]
    return _gate("BLOCK", *bad) if bad else None


# entry_rule = REFERENCE_AS_COLLAR (docs/MULTI_ASSET_MAPPING.md, "Entry rule").
REFERENCE_AS_COLLAR = "REFERENCE_AS_COLLAR"
ANCHOR_NOT_STATED = "ENTRY_ANCHOR_NOT_STATED"
ANCHOR_RULE_INCOMPLETE = "ENTRY_ANCHOR_RULE_INCOMPLETE"
ANCHOR_SIGNAL_TOO_OLD = "ENTRY_ANCHOR_SIGNAL_TOO_OLD"
ANCHOR_ADVERSE_MOVE = "ENTRY_ANCHOR_ADVERSE_MOVE"  # the market ran away: do not chase
ANCHOR_FAVOURABLE_MOVE = "ENTRY_ANCHOR_FAVOURABLE_MOVE"  # moved too far the other way: stale?
ANCHOR_LIMIT_ABOVE_CEILING = "ENTRY_ANCHOR_LIMIT_ABOVE_CEILING"
ANCHOR_BASIS_CAP_MISSING = "ENTRY_ANCHOR_BASIS_CAP_MISSING"
# The live streaming quote an anchored entry is decided on (owner, 12 November 2026): the side
# the order trades against is executable only on a LIVE quote with both sides present, positive,
# on tick and not locked or crossed, and each side updated no longer ago than the rule allows.
ANCHOR_QUOTE_NOT_LIVE = "ENTRY_ANCHOR_QUOTE_NOT_LIVE"  # delayed or frozen: never eligible
ANCHOR_QUOTE_SIDE_MISSING = "ENTRY_ANCHOR_QUOTE_SIDE_MISSING"
ANCHOR_QUOTE_OFF_TICK = "ENTRY_ANCHOR_QUOTE_OFF_TICK"
ANCHOR_QUOTE_LOCKED_OR_CROSSED = "ENTRY_ANCHOR_QUOTE_LOCKED_OR_CROSSED"
ANCHOR_QUOTE_SIDE_TIME_UNKNOWN = "ENTRY_ANCHOR_QUOTE_SIDE_TIME_UNKNOWN"
ANCHOR_QUOTE_SIDE_STALE = "ENTRY_ANCHOR_QUOTE_SIDE_STALE"
ANCHOR_QUOTE_SIDE_FUTURE = "ENTRY_ANCHOR_QUOTE_SIDE_FUTURE"
# Hard ceilings of the entry rule's limits (review F6). Constants on purpose: raising one is a code
# change and its review, never a configuration value. The mapping loader refuses limits above them
# (an absolute move: above the ceiling percentage of the highest price the family's sizing can
# buy), and the gateway refuses terms above them as a percentage of the anchor, or wider than the
# policy's own entry collar (max_price_deviation_pct).
ANCHOR_MAX_ADVERSE_PCT_CEILING = Decimal(1)
ANCHOR_MAX_FAVOURABLE_PCT_CEILING = Decimal(2)
ANCHOR_MAX_SIGNAL_AGE_CEILING_SECONDS = 120
# The oldest each side of the live streaming quote may be at an anchored decision (owner, 12
# November 2026: per-side freshness; a delayed quote is never eligible).
ANCHOR_MAX_QUOTE_AGE_CEILING_SECONDS = 5


def anchor_quote_problem(bid: Decimal | None, ask: Decimal | None, bid_at: datetime | None,
                         ask_at: datetime | None, quality, now: datetime, max_age: float,
                         tick: Decimal | None = None) -> list[str]:
    """Why a quote cannot carry an anchored entry, or []: quality LIVE (a delayed or frozen quote
    is never eligible, whatever the accepted qualities); bid and ask present, positive and (when
    ``tick`` is given) on tick; bid strictly below ask; each side's own update time known, no
    older than ``max_age`` and not more than 5 s in the future. A broker that cannot report
    per-side times can never anchor. The gateway and the execution core's pre-submit re-check
    both apply it."""
    quality = getattr(quality, "value", quality)
    if quality != "LIVE":
        return [ANCHOR_QUOTE_NOT_LIVE, f"QUALITY:{quality}"]
    if bid is None or ask is None or bid <= 0 or ask <= 0:
        return [ANCHOR_QUOTE_SIDE_MISSING]
    if tick is not None and (bid % tick or ask % tick):
        return [ANCHOR_QUOTE_OFF_TICK]
    if bid >= ask:
        return [ANCHOR_QUOTE_LOCKED_OR_CROSSED]
    for side, at in (("BID", bid_at), ("ASK", ask_at)):
        if at is None or at.tzinfo is None:
            return [ANCHOR_QUOTE_SIDE_TIME_UNKNOWN, f"SIDE:{side}"]
        age = (now - at).total_seconds()
        if age < -5:
            return [ANCHOR_QUOTE_SIDE_FUTURE, f"SIDE:{side}"]
        if age > max_age:
            return [ANCHOR_QUOTE_SIDE_STALE, f"SIDE:{side}", f"MAX_AGE:{max_age:g}"]
    return []


def move_limit(spec: str | None, anchor: Decimal) -> Decimal | None:
    """A move limit in the anchor's own price units: "2.5" as stated, "0.5%" of the anchor."""
    if not spec:
        return None
    try:
        if spec.endswith("%"):
            return anchor * Decimal(spec[:-1]) / 100
        return Decimal(spec)
    except (InvalidOperation, ValueError):
        return None


def _entry_anchor(a: TradeAction, q: MappedQuote, policy: MockPolicy, now: datetime,
                  published_at: datetime, long: bool) -> tuple[ExecutionPlan | None,
                                                               GateDecision | None]:
    """The anchored entry of a CURRENT OPEN or ADD without an explicit entry price, on an entry
    with REFERENCE_AS_COLLAR. Returns ``(None, None)`` when the rule does not apply (the caller
    then holds for the missing entry exactly as before), a refusal, or the plan fields: the
    anchor, the live price it was compared with in the anchor's own series and the entry limit.

    The anchor is compared in its own series: the live reference quote for BASIS_SHIFT and
    REFERENCE_TRIGGER, the execution quote for DIRECT (a same-series future included). Its side
    is the one the entry trades against (ask for a long, bid for a short). The execution quote has
    passed the gateway's quote checks already; the reference leg must be live and fresh here."""
    t = q.terms
    if t.entry_rule != REFERENCE_AS_COLLAR or a.entry_reference is not None \
            or a.timing != Timing.CURRENT or a.action not in (Action.OPEN, Action.ADD):
        return None, None
    anchor = q.entry_anchors.get(a.action_id)
    if anchor is None or not anchor.is_finite() or anchor <= 0:
        return None, None
    adverse = move_limit(t.anchor_max_adverse_move, anchor)
    favourable = move_limit(t.anchor_max_favourable_move, anchor)
    if adverse is None or favourable is None or not t.anchor_max_signal_age_seconds \
            or not t.anchor_max_quote_age_seconds:
        return None, _gate("HOLD", ANCHOR_RULE_INCOMPLETE)
    collar = policy.max_price_deviation_pct
    for name, limit, ceiling in (("ADVERSE", adverse, ANCHOR_MAX_ADVERSE_PCT_CEILING),
                                 ("FAVOURABLE", favourable, ANCHOR_MAX_FAVOURABLE_PCT_CEILING)):
        if limit <= 0 or limit > anchor * min(ceiling, collar) / 100:
            return None, _gate("HOLD", ANCHOR_LIMIT_ABOVE_CEILING, f"{name}:{limit}")
    if t.anchor_max_signal_age_seconds > ANCHOR_MAX_SIGNAL_AGE_CEILING_SECONDS:
        return None, _gate("HOLD", ANCHOR_LIMIT_ABOVE_CEILING,
                           f"SIGNAL_AGE:{t.anchor_max_signal_age_seconds:g}")
    if t.anchor_max_quote_age_seconds > ANCHOR_MAX_QUOTE_AGE_CEILING_SECONDS:
        return None, _gate("HOLD", ANCHOR_LIMIT_ABOVE_CEILING,
                           f"QUOTE_AGE:{t.anchor_max_quote_age_seconds:g}")
    if (now - published_at).total_seconds() > t.anchor_max_signal_age_seconds:
        return None, _gate("HOLD", ANCHOR_SIGNAL_TOO_OLD,
                           f"MAX_AGE:{t.anchor_max_signal_age_seconds:g}")
    # Live streaming quotes only, each side fresh (owner, 12 November 2026).
    problem = anchor_quote_problem(q.bid, q.ask, q.bid_at, q.ask_at, q.quality, now,
                                   t.anchor_max_quote_age_seconds, t.min_tick)
    if problem:
        return None, _gate("HOLD", *problem, "LEG:EXECUTION")
    if t.basis == "REFERENCE_TRIGGER" and not in_windows(
            t.reference_windows, t.session_days, t.session_timezone, now):
        return None, _gate("HOLD", "REFERENCE_SERIES_NOT_UPDATING")
    if t.basis in ("BASIS_SHIFT", "REFERENCE_TRIGGER"):
        held = _reference_leg(q, policy, now)
        if held:
            return None, held
        r = q.reference
        problem = anchor_quote_problem(r.bid, r.ask, r.bid_at, r.ask_at, r.quality, now,
                                       t.anchor_max_quote_age_seconds)
        if problem:
            return None, _gate("HOLD", *problem, "LEG:REFERENCE")
        # Review F4: the anchor bounds the reference price only. The execution price is bounded
        # by the live basis, within the family's cap, or the entry does not happen.
        cap, age = ((t.max_abs_basis, t.max_basis_age_seconds) if t.max_abs_basis is not None
                    else (t.anchor_max_abs_basis, t.anchor_max_basis_age_seconds))
        if cap is None or not age:
            return None, _gate("HOLD", ANCHOR_BASIS_CAP_MISSING)
        _, reasons = _basis(q, now, cap, age)
        if reasons:
            return None, _gate("HOLD", *reasons)
        live, series = _touch(q.reference, long), "REFERENCE"
    else:
        live, series = (q.ask if long else q.bid), "EXECUTION"
    worse = (live - anchor) if long else (anchor - live)
    if worse > adverse:
        return None, _gate("HOLD", ANCHOR_ADVERSE_MOVE, f"MOVE:{worse}>{adverse}")
    if -worse > favourable:
        return None, _gate("HOLD", ANCHOR_FAVOURABLE_MOVE, f"MOVE:{-worse}>{favourable}")
    # A limit at the marketable side of the live execution quote, never past it.
    limit = _snap(q.ask, t.min_tick, ROUND_FLOOR) if long else _snap(q.bid, t.min_tick,
                                                                     ROUND_CEILING)
    if limit <= 0:
        return None, _gate("BLOCK", "INVALID_BID_ASK")
    return ExecutionPlan(family=t.family, basis=t.basis, execution_key=t.execution_key,
                         entry_rule=REFERENCE_AS_COLLAR, entry_anchor=anchor,
                         anchor_series=series, anchor_live=live, entry_limit=limit,
                         anchor_max_quote_age_seconds=t.anchor_max_quote_age_seconds), None


_ANCHOR_FIELDS = {"entry_rule", "entry_anchor", "anchor_series", "anchor_live", "entry_limit",
                  "anchor_max_quote_age_seconds"}


def _mapped_open(a: TradeAction, q: MappedQuote, policy: MockPolicy, now: datetime,
                 published_at: datetime) -> GateDecision:
    t = q.terms
    if a.idea_reference is not None:
        return _gate("HOLD", "OPEN_SHOULD_NOT_REFERENCE_EXISTING_IDEA")
    if a.position_side is None:
        return _gate("HOLD", "DIRECTION_MISSING")
    long = a.position_side == PositionSide.LONG
    if not long and not t.allow_short:
        return _gate("HOLD", "SHORT_NOT_PERMITTED_FOR_FAMILY")
    ref = _num(a.entry_reference)
    anchored = None
    if ref is None:
        anchored, refused = _entry_anchor(a, q, policy, now, published_at, long)
        if refused:
            return refused
        if anchored is None:
            extra = [ANCHOR_NOT_STATED] if t.entry_rule == REFERENCE_AS_COLLAR else []
            return _gate("HOLD", "OPEN_ENTRY_PRICE_MISSING", *extra)
        # The publisher's own series: direction checks compare the stop and target with the
        # anchor; the collar is the anchor's own limits, already applied.
        ref = anchored.entry_anchor
    stop, target, bad = _mapped_levels(a)
    if bad:
        return bad
    if (policy.require_stop_on_open or anchored) and stop is None:
        # An anchored entry always carries a protective stop, whatever the policy says.
        return _gate("HOLD", "PROTECTIVE_STOP_MISSING")
    # Direction checks in the publisher's own series (both sides of each comparison are there).
    if stop is not None and (stop >= ref if long else stop <= ref):
        return _gate("BLOCK", "STOP_DIRECTION_CONTRADICTION")
    if target is not None and (target <= ref if long else target >= ref):
        return _gate("BLOCK", "TARGET_DIRECTION_CONTRADICTION")
    band = policy.max_price_deviation_pct
    exec_touch = q.ask if long else q.bid
    plan = ExecutionPlan(family=t.family, basis=t.basis, execution_key=t.execution_key,
                         quantity=t.units_per_signal, entry_reference=ref,
                         publisher_stop=stop, publisher_target=target)
    if t.basis == "DIRECT":
        bad = _off_tick(t, stop=stop, target=target)
        if bad:
            return bad
        if not anchored and abs(exec_touch - ref) / ref * 100 > band:
            return _gate("BLOCK", "ENTRY_PRICE_COLLAR")
        if stop is not None and (stop >= q.bid if long else stop <= q.ask):
            return _gate("BLOCK", "STOP_ALREADY_CROSSED_AT_ENTRY")
        plan = plan.model_copy(update={"execution_reference": ref, "stop": stop, "target": target})
    elif t.basis == "BASIS_SHIFT":
        shifted, reasons = _basis(q, now)
        if shifted is None:
            return _gate("HOLD", *reasons)
        b = shifted.basis_value
        entry = ref + b
        stop_x = _shift_protection(stop, b, t.min_tick, long, "stop") if stop is not None else None
        target_x = (_shift_protection(target, b, t.min_tick, long, "target")
                    if target is not None else None)
        if not anchored and abs(exec_touch - entry) / entry * 100 > band:
            return _gate("BLOCK", "ENTRY_PRICE_COLLAR")
        if stop_x is not None and (stop_x >= q.bid if long else stop_x <= q.ask):
            return _gate("BLOCK", "STOP_ALREADY_CROSSED_AT_ENTRY")
        plan = plan.model_copy(update={
            **shifted.model_dump(include={"basis_value", "basis_execution_mid",
                                          "basis_reference_mid", "basis_observed_at"}),
            "execution_reference": entry, "stop": stop_x, "target": target_x})
    else:  # REFERENCE_TRIGGER
        if not in_windows(t.reference_windows, t.session_days, t.session_timezone, now):
            return _gate("HOLD", "REFERENCE_SERIES_NOT_UPDATING")
        held = _reference_leg(q, policy, now)
        if held:
            return held
        ref_touch = _touch(q.reference, long)
        if not anchored and abs(ref_touch - ref) / ref * 100 > band:
            return _gate("BLOCK", "ENTRY_PRICE_COLLAR")
        ref_bid, ref_ask = _touch(q.reference, False), _touch(q.reference, True)
        if stop is not None and (stop >= ref_bid if long else stop <= ref_ask):
            return _gate("BLOCK", "STOP_ALREADY_CROSSED_AT_ENTRY")
        plan = plan.model_copy(update={
            "execution_reference": exec_touch, "stop": stop, "target": target,
            "protection_series": "REFERENCE", "reference_contract_key": t.reference_contract_key,
            "reference_contract": t.reference_contract})
        if t.require_backstop:
            if stop is None:
                return _gate("HOLD", "BACKSTOP_NEEDS_A_STOP")
            shifted, reasons = _basis(q, now)
            if shifted is None:
                return _gate("HOLD", "BACKSTOP_UNPRICEABLE", *reasons)
            backstop = _shift_protection(stop, shifted.basis_value, t.min_tick, long, "stop")
            if backstop >= q.bid if long else backstop <= q.ask:
                return _gate("BLOCK", "BACKSTOP_ALREADY_CROSSED_AT_ENTRY")
            plan = plan.model_copy(update={
                **shifted.model_dump(include={"basis_value", "basis_execution_mid",
                                              "basis_reference_mid", "basis_observed_at"}),
                "backstop_stop": backstop})
    if anchored:
        # The entry is the live marketable limit; the publisher's level is recorded as the anchor.
        plan = plan.model_copy(update={
            **anchored.model_dump(include=_ANCHOR_FIELDS), "entry_reference": None,
            "execution_reference": anchored.entry_limit})
    qty = t.units_per_signal
    if qty > t.max_units or qty % t.size_increment:
        return _gate("BLOCK", "UNIT_CAP_OR_INCREMENT")
    notional = plan.execution_reference * qty * t.multiplier
    if notional > min(t.max_notional, policy.max_notional_usd):
        return _gate("BLOCK", "NOTIONAL_LIMIT")
    return _gate_plan(plan, "PAPER_OPEN", f"MAPPING_{t.basis}",
                      *([REFERENCE_AS_COLLAR] if anchored else []))


def _gate_plan(plan: ExecutionPlan, *reasons: str) -> GateDecision:
    return GateDecision(status="APPROVE", reasons=list(reasons), plan=plan)


def _mapped_follow_up(a: TradeAction, q: MappedQuote, policy: MockPolicy,
                      position: Position | None, now: datetime,
                      published_at: datetime | None = None) -> GateDecision:
    t = q.terms
    inst = a.instrument
    assert inst is not None
    if not a.idea_reference or position is None or position.idea_id != a.idea_reference \
            or position.quantity <= 0:
        return _gate("HOLD", "REFERENCED_ACTIVE_IDEA_NOT_FOUND")
    if position.symbol != inst.symbol or position.asset_class != inst.asset_class:
        return _gate("BLOCK", "IDEA_INSTRUMENT_MISMATCH")
    if a.position_side is not None and a.position_side != position.side:
        return _gate("BLOCK", "IDEA_SIDE_MISMATCH")
    long = position.side == PositionSide.LONG
    reference_series = t.basis == "REFERENCE_TRIGGER"
    base = ExecutionPlan(family=t.family, basis=t.basis, execution_key=t.execution_key,
                         protection_series="REFERENCE" if reference_series else "EXECUTION",
                         reference_contract_key=t.reference_contract_key,
                         reference_contract=t.reference_contract)
    if a.action in (Action.CLOSE, Action.REDUCE):
        if a.action == Action.REDUCE:
            fraction = _num(a.close_fraction)
            if fraction is None or fraction > 1:
                return _gate("HOLD", "REDUCTION_FRACTION_UNDEFINED")
            units = Decimal(position.quantity) * fraction
            if units < 1 or units != units.to_integral_value():
                return _gate("HOLD", "NON_INTEGER_REDUCTION")
            if int(units) % t.size_increment:
                return _gate("HOLD", "REDUCTION_OFF_SIZE_INCREMENT")
        return _gate_plan(base, "PAPER_REDUCE_ONLY", f"MAPPING_{t.basis}")
    # The series the position's protection lives in, and the market it is compared against.
    if reference_series and a.action == Action.ADD and not in_windows(
            t.reference_windows, t.session_days, t.session_timezone, now):
        # New exposure while the reference cannot trigger its exits, exactly as for an OPEN.
        return _gate("HOLD", "REFERENCE_SERIES_NOT_UPDATING")
    if reference_series:
        held = _reference_leg(q, policy, now)
        if held:
            return held
        bid, ask = _touch(q.reference, False), _touch(q.reference, True)
    else:
        bid, ask = q.bid, q.ask
    if a.action == Action.ADD:
        prior_stop = _num(position.stop_price)
        if prior_stop is not None and (prior_stop >= bid if long else prior_stop <= ask):
            return _gate("HOLD", "EXISTING_STOP_ALREADY_CROSSED")
        ref = _num(a.entry_reference)
        exec_touch = q.ask if long else q.bid
        if ref is None:
            anchored, refused = (None, None) if published_at is None else _entry_anchor(
                a, q, policy, now, published_at, long)
            if refused:
                return refused
            if anchored is None:
                extra = [ANCHOR_NOT_STATED] if t.entry_rule == REFERENCE_AS_COLLAR else []
                return _gate("HOLD", "ADD_ENTRY_REFERENCE_MISSING", *extra)
            if prior_stop is None:  # an anchored add never adds to an unprotected idea
                return _gate("HOLD", "PROTECTIVE_STOP_MISSING")
            entry = anchored.entry_limit
            if t.basis == "BASIS_SHIFT":  # both legs live and near-simultaneous, recorded
                shifted, reasons = _basis(q, now)
                if shifted is None:
                    return _gate("HOLD", *reasons)
                base = base.model_copy(update=shifted.model_dump(
                    include={"basis_value", "basis_execution_mid", "basis_reference_mid",
                             "basis_observed_at"}))
            qty = t.units_per_signal
            if position.quantity + qty > t.max_units:
                return _gate("BLOCK", "UNIT_CAP")
            if entry * (position.quantity + qty) * t.multiplier > min(t.max_notional,
                                                                       policy.max_notional_usd):
                return _gate("BLOCK", "NOTIONAL_LIMIT")
            plan = base.model_copy(update={**anchored.model_dump(include=_ANCHOR_FIELDS),
                                           "quantity": qty, "execution_reference": entry})
            return _gate_plan(plan, "PAPER_ADD", f"MAPPING_{t.basis}", REFERENCE_AS_COLLAR)
        if t.basis == "DIRECT":
            entry = ref
        elif t.basis == "BASIS_SHIFT":
            shifted, reasons = _basis(q, now)
            if shifted is None:
                return _gate("HOLD", *reasons)
            entry = ref + shifted.basis_value
            base = base.model_copy(update=shifted.model_dump(
                include={"basis_value", "basis_execution_mid", "basis_reference_mid",
                         "basis_observed_at"}))
        else:
            entry = exec_touch
            if abs((ask if long else bid) - ref) / ref * 100 > policy.max_price_deviation_pct:
                return _gate("BLOCK", "ENTRY_PRICE_COLLAR")
        if abs(exec_touch - entry) / entry * 100 > policy.max_price_deviation_pct:
            return _gate("BLOCK", "ENTRY_PRICE_COLLAR")
        qty = t.units_per_signal
        if position.quantity + qty > t.max_units:
            return _gate("BLOCK", "UNIT_CAP")
        if entry * (position.quantity + qty) * t.multiplier > min(t.max_notional,
                                                                   policy.max_notional_usd):
            return _gate("BLOCK", "NOTIONAL_LIMIT")
        plan = base.model_copy(update={"quantity": qty, "entry_reference": ref,
                                       "execution_reference": entry})
        return _gate_plan(plan, "PAPER_ADD", f"MAPPING_{t.basis}")
    if a.action == Action.MODIFY:
        if a.stop_price is None and a.target_price is None:
            return _gate("HOLD", "NO_FIELDS_TO_MODIFY")
        stop, target, bad = _mapped_levels(a)
        if bad:
            return bad
        plan = base.model_copy(update={"publisher_stop": stop, "publisher_target": target})

        def looser(new: Decimal, old: Decimal) -> bool:
            return new < old if long else new > old

        # The publisher's own previous stop: the ledger's stop for DIRECT and REFERENCE_TRIGGER
        # (it is in the publisher's series), the recorded publisher stop for BASIS_SHIFT.
        old_publisher = _num(position.publisher_stop if t.basis == "BASIS_SHIFT"
                             else position.stop_price)
        if stop is not None and old_publisher is not None and looser(stop, old_publisher):
            return _gate("HOLD", "STOP_WIDENING_REQUIRES_POLICY_REVIEW")
        # A tightening by the publisher never moves a broker-side protective level the loosening
        # way (the basis may have moved since it was set): the tighter level is kept, recorded on
        # the plan and raised as an exception by the execution core.
        kept: list[str] = []
        if t.basis == "BASIS_SHIFT" or (reference_series and t.require_backstop
                                        and stop is not None):
            shifted, reasons = _basis(q, now)
            if shifted is None:
                return _gate("HOLD", *reasons)
            b = shifted.basis_value
            plan = plan.model_copy(update=shifted.model_dump(
                include={"basis_value", "basis_execution_mid", "basis_reference_mid",
                         "basis_observed_at"}))
            if t.basis == "BASIS_SHIFT":
                stop = _shift_protection(stop, b, t.min_tick, long, "stop") if stop else None
                target = (_shift_protection(target, b, t.min_tick, long, "target")
                          if target else None)
                working = _num(position.stop_price)
                if stop is not None and working is not None and looser(stop, working):
                    if old_publisher is None:  # the publisher's direction is unknown
                        return _gate("HOLD", "STOP_WIDENING_REQUIRES_POLICY_REVIEW")
                    kept.append(f"STOP: computed {stop} looser than working {working}; "
                                f"kept {working}")
                    stop = working
            else:
                backstop = _shift_protection(stop, b, t.min_tick, long, "stop")
                working = _num(position.backstop_price)
                if working is not None and looser(backstop, working):
                    kept.append(f"BACKSTOP: computed {backstop} looser than working {working}; "
                                f"kept {working}")
                    backstop = working
                if backstop >= q.bid if long else backstop <= q.ask:
                    return _gate("BLOCK", "BACKSTOP_ON_WRONG_SIDE_OF_MARKET")
                plan = plan.model_copy(update={"backstop_stop": backstop})
        elif t.basis == "DIRECT":
            bad = _off_tick(t, stop=stop, target=target)
            if bad:
                return bad
        if stop is not None:
            old_stop = _num(position.stop_price)
            if old_stop is not None and (stop < old_stop if long else stop > old_stop):
                return _gate("HOLD", "STOP_WIDENING_REQUIRES_POLICY_REVIEW")
            if stop >= bid if long else stop <= ask:
                return _gate("BLOCK", "STOP_ON_WRONG_SIDE_OF_MARKET")
        if target is not None and (target <= bid if long else target >= ask):
            return _gate("BLOCK", "TARGET_ON_WRONG_SIDE_OF_MARKET")
        plan = plan.model_copy(update={"stop": stop, "target": target,
                                       "protective_levels_kept": kept})
        return _gate_plan(plan, "PAPER_PROTECTION_AMENDMENT", f"MAPPING_{t.basis}",
                          *([PROTECTIVE_LEVEL_NOT_LOOSENED] if kept else []))
    return _gate("HOLD", "UNKNOWN_ACTION")


# ----------------------------------------------------------------------------- sizing

def fill_quantity(action: Action, policy: MockPolicy, position: Position | None,
                  close_fraction: str | None = None, *, per_signal: int | None = None) -> int:
    """Deterministic fictional fill size. Only policy and ledger state decide; never model output.
    Call after an APPROVE; raises if the inputs could not have been approved. ``per_signal`` is
    the units per signal of an approved mapping entry (multi-asset mapping, from the gateway's
    plan); without it the policy sizes exactly as before."""
    if action in (Action.OPEN, Action.ADD) and per_signal is not None:
        return per_signal
    if action == Action.OPEN:
        return policy.quantity
    if action == Action.ADD:
        return policy.quantity_per_add
    if action == Action.MODIFY:
        return 0
    if position is None:
        raise ValueError("No active position for a reduce-only action")
    if action == Action.CLOSE:
        return position.quantity
    if action == Action.REDUCE:
        fraction = _num(close_fraction)
        if fraction is None or fraction > 1:
            raise ValueError("Undefined reduction fraction")
        units = Decimal(position.quantity) * fraction
        if units < 1 or units != units.to_integral_value():
            raise ValueError("Non-integer reduction")
        return int(units)
    raise ValueError(f"Unsized action {action}")


# ----------------------------------------------------------------------------- Sprint 1 compatibility

def as_trade_action(signal: SignalCandidate, action_id: str = "legacy") -> TradeAction:
    """Lift a Sprint 1 candidate into the Sprint 1.1 action schema.

    The Sprint 1 grammars quote the traded symbol directly ("BUY ABC @ 100"), so the price
    basis is the traded contract and the source market symbol is the instrument itself.
    A conditional candidate becomes CONDITIONAL timing; everything else is CURRENT.
    """
    inst = signal.instrument
    return TradeAction(
        action_id=action_id, action=signal.action,
        timing=Timing.CONDITIONAL if signal.conditional else Timing.CURRENT,
        instrument=inst, position_side=signal.side, idea_reference=signal.idea_reference,
        price_basis=PriceBasis.TRADED_CONTRACT if inst is not None else PriceBasis.UNKNOWN,
        source_market_symbol=inst.symbol if inst is not None else None,
        entry_reference=signal.entry_reference, stop_price=signal.stop_price,
        target_price=signal.target_price, close_fraction=signal.close_fraction,
        explicit_instruction=signal.explicit_instruction, evidence=list(signal.evidence),
        ambiguities=list(signal.ambiguities), reason=signal.reason,
    )


def to_legacy_decision(gate: GateDecision) -> Decision:
    return Decision(status=LEGACY_STATUS[gate.status],
                    reasons=[LEGACY_REASON_CODES.get(r, r) for r in gate.reasons])


def validate(
    source: SourceEvent,
    signal: SignalCandidate,
    quote: Quote | None,
    policy: MockPolicy,
    position: Position | None = None,
    *,
    now: datetime | None = None,
) -> Decision:
    """Sprint 1 entry point, unchanged in signature. Delegates every rule to ``gate_action``.

    A manual-demo SourceEvent carries no provenance metadata, so no post-level gate applies here;
    the Sprint 1.1 adapter path supplies that through ``gate_source``.
    """
    now = now or datetime.now(UTC)
    if signal.action == Action.NO_TRADE:
        return Decision(status="NO_TRADE", reasons=[signal.reason or "NOT_ACTIONABLE"])
    gate = gate_action(as_trade_action(signal), source.published_at, quote, policy, position,
                       now=now, supported_actions=SPRINT1_ACTIONS)
    return to_legacy_decision(gate)
