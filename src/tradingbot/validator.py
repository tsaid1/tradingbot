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
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from tradingbot.event_models import PriceBasis, ReplaySource, Timing, TradeAction
from tradingbot.models import (
    Action, AssetClass, Decision, GateDecision, MockPolicy, Position, PositionSide,
    Quote, SignalCandidate, SourceEvent,
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


# ----------------------------------------------------------------------------- sizing

def fill_quantity(action: Action, policy: MockPolicy, position: Position | None,
                  close_fraction: str | None = None) -> int:
    """Deterministic fictional fill size. Only policy and ledger state decide; never model output.
    Call after an APPROVE; raises if the inputs could not have been approved."""
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
    now = now or datetime.now(timezone.utc)
    if signal.action == Action.NO_TRADE:
        return Decision(status="NO_TRADE", reasons=[signal.reason or "NOT_ACTIONABLE"])
    gate = gate_action(as_trade_action(signal), source.published_at, quote, policy, position,
                       now=now, supported_actions=SPRINT1_ACTIONS)
    return to_legacy_decision(gate)
