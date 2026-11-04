"""Deterministic policy and preflight. No AI decisions are trusted here."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import re

from tradingbot.models import (
    Action, AssetClass, Decision, MockPolicy, Position, PositionSide,
    Quote, SignalCandidate, SourceEvent,
)

SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.]{0,9}$")


def _num(value: str | None) -> Decimal | None:
    if value is None:
        return None
    try:
        result = Decimal(value)
    except (InvalidOperation, ValueError):
        return None
    return result if result.is_finite() and result > 0 else None


def validate(
    source: SourceEvent,
    signal: SignalCandidate,
    quote: Quote | None,
    policy: MockPolicy,
    position: Position | None = None,
    *,
    now: datetime | None = None,
) -> Decision:
    now = now or datetime.now(timezone.utc)
    if signal.action == Action.NO_TRADE:
        return Decision(status="NO_TRADE", reasons=[signal.reason or "NOT_ACTIONABLE"])
    if not signal.explicit_instruction or not signal.evidence:
        return Decision(status="REVIEW", reasons=["NO_EXPLICIT_VERIFIABLE_INSTRUCTION"])
    if signal.conditional:
        return Decision(status="REVIEW", reasons=["CONDITIONAL_SIGNAL_UNSUPPORTED"])
    if signal.ambiguities:
        return Decision(status="REVIEW", reasons=["AMBIGUOUS_SOURCE", *signal.ambiguities])
    if signal.instrument is None:
        return Decision(status="REVIEW", reasons=["MISSING_INSTRUMENT"])
    inst = signal.instrument
    if inst.asset_class not in (AssetClass.EQUITY, AssetClass.ETF):
        return Decision(status="REVIEW", reasons=["MOCK_EXECUTION_EQUITIES_ONLY"])
    if not SYMBOL_RE.fullmatch(inst.symbol):
        return Decision(status="BLOCK", reasons=["INVALID_SYMBOL"])
    if signal.action not in (Action.OPEN, Action.CLOSE, Action.REDUCE):
        return Decision(status="REVIEW", reasons=["ACTION_UNSUPPORTED_IN_SPRINT_1"])
    # Require genuinely UTC/timezone-aware source and quote timestamps.
    if source.published_at.tzinfo is None:
        return Decision(status="BLOCK", reasons=["SOURCE_TIMESTAMP_MISSING_TIMEZONE"])
    age = (now - source.published_at).total_seconds()
    if age < -5 or age > policy.max_signal_age_seconds:
        return Decision(status="BLOCK", reasons=["STALE_OR_FUTURE_SOURCE"])
    if quote is None or quote.observed_at.tzinfo is None:
        return Decision(status="BLOCK", reasons=["QUOTE_UNAVAILABLE"])
    age = (now - quote.observed_at).total_seconds()
    if age < -5 or age > policy.max_quote_age_seconds:
        return Decision(status="BLOCK", reasons=["STALE_OR_FUTURE_QUOTE"])
    if quote.symbol.upper() != inst.symbol:
        return Decision(status="BLOCK", reasons=["QUOTE_INSTRUMENT_MISMATCH"])
    if quote.bid <= 0 or quote.ask <= 0 or quote.ask < quote.bid:
        return Decision(status="BLOCK", reasons=["INVALID_BID_ASK"])
    spread_pct = (quote.ask - quote.bid) / ((quote.bid + quote.ask) / 2) * 100
    if spread_pct > policy.max_spread_pct:
        return Decision(status="BLOCK", reasons=["SPREAD_TOO_WIDE"])
    if signal.action == Action.OPEN:
        if signal.idea_reference:
            return Decision(status="REVIEW", reasons=["OPEN_SHOULD_NOT_REFERENCE_EXISTING_IDEA"])
        if signal.side is None:
            return Decision(status="REVIEW", reasons=["DIRECTION_MISSING"])
        ref = _num(signal.entry_reference)
        if ref is None:
            return Decision(status="REVIEW", reasons=["NO_EXPLICIT_ENTRY_REFERENCE"])
        price = quote.ask if signal.side == PositionSide.LONG else quote.bid
        deviation_pct = abs(price - ref) / ref * 100
        if deviation_pct > policy.max_price_deviation_pct:
            return Decision(status="BLOCK", reasons=["PRICE_COLLAR_BREACH"])
        stop = _num(signal.stop_price) if signal.stop_price is not None else None
        target = _num(signal.target_price) if signal.target_price is not None else None
        if signal.stop_price is not None and stop is None:
            return Decision(status="BLOCK", reasons=["INVALID_STOP_PRICE"])
        if signal.target_price is not None and target is None:
            return Decision(status="BLOCK", reasons=["INVALID_TARGET_PRICE"])
        if stop is not None and (
            (signal.side == PositionSide.LONG and stop >= ref)
            or (signal.side == PositionSide.SHORT and stop <= ref)
        ):
            return Decision(status="BLOCK", reasons=["STOP_SIDE_CONTRADICTION"])
        if target is not None and (
            (signal.side == PositionSide.LONG and target <= ref)
            or (signal.side == PositionSide.SHORT and target >= ref)
        ):
            return Decision(status="BLOCK", reasons=["TARGET_SIDE_CONTRADICTION"])
        if price * policy.quantity > policy.max_notional_usd:
            return Decision(status="BLOCK", reasons=["MOCK_NOTIONAL_CAP"])
        return Decision(status="EXECUTE_MOCK", reasons=["PREFLIGHT_OK_MOCK_ONLY"])
    if not signal.idea_reference or position is None:
        return Decision(status="REVIEW", reasons=["REFERENCED_POSITION_NOT_FOUND"])
    if position.symbol != inst.symbol or position.asset_class != inst.asset_class:
        return Decision(status="BLOCK", reasons=["POSITION_INSTRUMENT_MISMATCH"])
    if signal.action == Action.REDUCE:
        fraction = _num(signal.close_fraction)
        if fraction is None or fraction > 1:
            return Decision(status="REVIEW", reasons=["INVALID_REDUCE_FRACTION"])
        units = Decimal(position.quantity) * fraction
        if units != units.to_integral_value() or units < 1:
            return Decision(status="REVIEW", reasons=["NON_INTEGER_REDUCTION_IN_MOCK"])
    return Decision(status="EXECUTE_MOCK", reasons=["REDUCE_ONLY_MOCK_OK"])
