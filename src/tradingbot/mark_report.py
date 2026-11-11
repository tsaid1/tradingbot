"""MARK-1: a read-only exposure and mark report for fictional open positions. NON_AUTHORITATIVE.

``build_mark_report(snapshot, marks, policy)`` values each open position in an existing
``operations_view.OperationsSnapshot`` against quotes the caller supplies as invented marks. It is
a pure function: it reads no ledger, writes nothing, reads no wall clock and changes no input.

* **Marks are a caller assertion.** Every supplied quote is labelled ``SUPPLIED_INVENTED``. That is
  the caller's statement of input scope, not verified provenance. Freshness is judged at the
  snapshot's own timezone-aware ``as_of`` with the existing ``operations_view.freshness`` and
  ``policy.max_quote_age_seconds``, including its five-second future tolerance. A missing mark is
  ``UNKNOWN`` and a malformed one ``INVALID``. Only a ``FRESH`` mark values anything.
* **Exposure convention.** Units times the executable side of the mark (bid for a long position, ask
  for a short one), in the instrument's own price units. Signed exposure is positive for long and
  negative for short; gross exposure is its magnitude. Only equities and ETFs are valued. No
  currency, multiplier, cross-symbol or cross-engine total is computed.
* **Average cost, only where provable.** Basis comes from the position's own fills, joined by
  ``(engine, idea_id)`` and validated for identity, direction, status, quantity and lifecycle. Entries
  (OPEN, ADD) set the average; exits (REDUCE, CLOSE) remove units at that average. Amendments
  (``SIMULATED_AMENDED`` MODIFY rows) are not fills. Fill order in a snapshot is not authenticated
  execution chronology, so a basis that would depend on it (entries at different prices together
  with an exit) is ``UNKNOWN``, never guessed. An incomplete reconciliation or any discrepancy for
  the engine also makes basis and P&L ``UNKNOWN``.
* **Recorded levels are not protection.** Stop and target distances are informational: no level is
  monitored and no protection is active. The snapshot's protection status is carried unchanged.

Arithmetic uses one local Decimal context (34 significant digits, half-even rounding, traps on) and
never touches the global context. Only the average-cost division can round. Nothing here is advice,
a trigger, an alert, an order or trading permission. See docs/MOCK_MARK_REPORT.md.
"""
from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping
from datetime import datetime
from decimal import (
    ROUND_HALF_EVEN,
    Context,
    Decimal,
    DivisionByZero,
    InvalidOperation,
    Overflow,
)
from typing import Literal

from tradingbot.models import Action, AssetClass, MockPolicy, PositionSide, Quote, StrictModel
from tradingbot.operations_view import Engine, FillRow, OperationsSnapshot, freshness

__all__ = [
    "EXPOSURE_CONVENTION",
    "MARK_SCOPE",
    "MarkReport",
    "PositionMark",
    "build_mark_report",
]

MARK_SCOPE = "SUPPLIED_INVENTED"
EXPOSURE_CONVENTION = (
    "units x executable side of the mark (bid for LONG, ask for SHORT), in the instrument's own price "
    "units; signed exposure is positive for LONG and negative for SHORT, gross exposure is its magnitude; "
    "no currency, multiplier or cross-symbol total"
)
LEVEL_NOTE = "recorded level, not monitored; no protection is active"
VALUED_ASSET_CLASSES = frozenset({AssetClass.EQUITY.value, AssetClass.ETF.value})
MAX_UNITS = 10**9
MAX_PRICE_DIGITS = 20
MAX_PRICE_EXPONENT = 12

_CONTEXT = Context(prec=34, rounding=ROUND_HALF_EVEN, Emax=999, Emin=-999,
                   traps=[InvalidOperation, DivisionByZero, Overflow])
_PRICE_TEXT = re.compile(r"[0-9]{1,15}(\.[0-9]{1,10})?")
_ENTRY_SIDE = {PositionSide.LONG.value: "BUY", PositionSide.SHORT.value: "SELL"}
_EXIT_SIDE = {PositionSide.LONG.value: "SELL", PositionSide.SHORT.value: "BUY"}
_ENTRIES = (Action.OPEN.value, Action.ADD.value)
_EXITS = (Action.REDUCE.value, Action.CLOSE.value)

MarkStatus = Literal["FRESH", "STALE", "FUTURE", "UNKNOWN", "INVALID"]
Status = Literal["KNOWN", "UNKNOWN", "UNSUPPORTED"]


class PositionMark(StrictModel):
    """One open position's valuation. ``None`` values are unknown, never zero."""

    engine: Engine
    idea_id: str
    symbol: str
    asset_class: str
    side: str
    units: int
    mark_scope: Literal["SUPPLIED_INVENTED"] = MARK_SCOPE
    mark_status: MarkStatus
    mark_reason: str
    mark_observed_at: datetime | None = None
    executable_mark: Decimal | None = None
    exposure_status: Status
    exposure_reasons: tuple[str, ...]
    signed_exposure: Decimal | None = None
    gross_exposure: Decimal | None = None
    basis_status: Status
    basis_reasons: tuple[str, ...]
    average_cost: Decimal | None = None
    unrealized_pnl: Decimal | None = None
    stop_price: str | None = None
    stop_distance: Decimal | None = None
    target_price: str | None = None
    target_distance: Decimal | None = None
    level_reasons: tuple[str, ...]
    level_note: str = LEVEL_NOTE
    protection: str
    protection_note: str


class MarkReport(StrictModel):
    """Fixed fields, per-position values and counts only. No monetary total of any kind."""

    authority: Literal["NON_AUTHORITATIVE"] = "NON_AUTHORITATIVE"
    trading_permission: Literal[False] = False
    mark_scope: Literal["SUPPLIED_INVENTED"] = MARK_SCOPE
    exposure_convention: str = EXPOSURE_CONVENTION
    as_of: datetime
    as_of_aware: bool
    reconciliation_complete: bool
    status: Literal["COMPLETE", "INCOMPLETE"]
    positions: tuple[PositionMark, ...]
    counts: dict[str, int]


# ----------------------------------------------------------------------------- parsing


def _bounded(value: Decimal) -> bool:
    if not value.is_finite():
        return False
    _sign, digits, exponent = value.as_tuple()
    return len(digits) <= MAX_PRICE_DIGITS and abs(int(exponent)) <= MAX_PRICE_EXPONENT


def _price_text(text: object) -> Decimal | None:
    """A recorded price or level: plain positive decimal text, bounded. Anything else is None."""
    if type(text) is not str or not _PRICE_TEXT.fullmatch(text):
        return None
    value = Decimal(text)
    return value if value > 0 and _bounded(value) else None


def _mark(quote: object, symbol: str, as_of: datetime, aware: bool,
          policy: MockPolicy) -> tuple[MarkStatus, str, Quote | None]:
    if not aware:
        return "UNKNOWN", "AS_OF_NOT_TIMEZONE_AWARE", None
    if quote is None:
        return "UNKNOWN", "MARK_MISSING", None
    if type(quote) is not Quote:
        return "INVALID", "MARK_NOT_A_QUOTE", None
    if quote.symbol != symbol:
        return "INVALID", "MARK_SYMBOL_MISMATCH", None
    bid, ask, observed = quote.bid, quote.ask, quote.observed_at
    if type(bid) is not Decimal or type(ask) is not Decimal or not _bounded(bid) or not _bounded(ask):
        return "INVALID", "MARK_PRICE_MALFORMED", None
    if bid <= 0 or ask <= 0 or ask < bid:
        return "INVALID", "MARK_BID_ASK_INVALID", None
    if not isinstance(observed, datetime) or observed.tzinfo is None or observed.utcoffset() is None:
        return "INVALID", "MARK_TIME_NOT_TIMEZONE_AWARE", None
    state = freshness((as_of - observed).total_seconds(), policy.max_quote_age_seconds)
    if state != "FRESH":
        return state, f"MARK_{state}", None
    return "FRESH", "MARK_FRESH", quote


# ----------------------------------------------------------------------------- basis


def _basis(side: str, symbol: str, units: int, fills: list[FillRow],
           duplicate_orders: set[str]) -> tuple[Decimal | None, tuple[str, ...]]:
    """Average cost from one position's own fills, or ``None`` with the reasons it is not provable."""
    reasons: list[str] = []
    entries: list[tuple[int, Decimal]] = []
    exit_units = 0
    opens = 0
    for f in fills:
        if f.order_id in duplicate_orders:
            reasons.append("DUPLICATE_ORDER_ID")
            continue
        if f.symbol != symbol:
            reasons.append("FILL_SYMBOL_MISMATCH")
            continue
        if f.action == Action.MODIFY.value:
            if not (f.status == "SIMULATED_AMENDED" and f.quantity == 0 and f.price is None and f.side == "NONE"):
                reasons.append("AMENDMENT_RECORD_INVALID")
            continue  # a valid amendment is not a fill
        if f.action not in _ENTRIES + _EXITS or f.status != "SIMULATED_FILLED":
            reasons.append("FILL_RECORD_INVALID")
            continue
        if type(f.quantity) is not int or not 0 < f.quantity <= MAX_UNITS:
            reasons.append("FILL_QUANTITY_INVALID")
            continue
        expected_side = _ENTRY_SIDE[side] if f.action in _ENTRIES else _EXIT_SIDE[side]
        if f.side != expected_side:
            reasons.append("FILL_DIRECTION_MISMATCH")
            continue
        if f.action in _EXITS:
            if f.action == Action.CLOSE.value:
                reasons.append("CLOSE_FILL_ON_OPEN_POSITION")
            exit_units += f.quantity
            continue
        price = _price_text(f.price)
        if price is None:
            reasons.append("FILL_PRICE_INVALID")
            continue
        opens += f.action == Action.OPEN.value
        entries.append((f.quantity, price))
    if opens != 1:
        reasons.append("OPEN_FILL_MISSING" if opens == 0 else "REPEATED_OPEN_FILL")
    entry_units = sum(q for q, _ in entries)
    if not reasons and entry_units - exit_units != units:
        reasons.append("FILL_QUANTITY_DISAGREES_WITH_POSITION")
    if not reasons and exit_units and len({p for _, p in entries}) > 1:
        # With an exit among entries at different prices, average cost depends on whether any entry
        # followed an exit, and the snapshot does not authenticate fill order.
        reasons.append("ORDER_DEPENDENT_BASIS_UNPROVEN")
    if reasons:
        return None, tuple(dict.fromkeys(reasons))
    total_cost = Decimal(0)
    for quantity, price in entries:
        total_cost = _CONTEXT.add(total_cost, _CONTEXT.multiply(price, Decimal(quantity)))
    return _CONTEXT.divide(total_cost, Decimal(entry_units)), ()


# ----------------------------------------------------------------------------- report


def build_mark_report(snapshot: OperationsSnapshot, marks: Mapping[str, Quote],
                      policy: MockPolicy) -> MarkReport:
    """Value each open position in ``snapshot`` against ``marks`` (symbol -> supplied invented quote)."""
    if type(snapshot) is not OperationsSnapshot:
        raise TypeError("snapshot must be an OperationsSnapshot")
    if not isinstance(marks, Mapping):
        raise TypeError("marks must be a mapping of symbol to Quote")
    if type(policy) is not MockPolicy:
        raise TypeError("policy must be a MockPolicy")
    as_of = snapshot.as_of
    aware = as_of.tzinfo is not None and as_of.utcoffset() is not None
    open_positions = snapshot.open_positions
    keys = Counter((p.engine, p.idea_id) for p in snapshot.positions)
    order_ids = Counter((f.engine, f.order_id) for f in snapshot.fills)
    discrepant_engines = {d.engine for d in snapshot.discrepancies}
    serious_engines = {d.engine for d in snapshot.discrepancies if d.severity == "SERIOUS"}
    named = {(d.engine, d.subject) for d in snapshot.discrepancies}
    rows: list[PositionMark] = []
    for p in open_positions:
        key = (p.engine, p.idea_id)
        duplicate_orders = {oid for (engine, oid), n in order_ids.items() if engine == p.engine and n > 1}
        mark_status, mark_reason, quote = _mark(marks.get(p.symbol), p.symbol, as_of, aware, policy)

        identity: list[str] = []
        if keys[key] > 1:
            identity.append("DUPLICATE_POSITION")
        if p.side not in _ENTRY_SIDE:
            identity.append("POSITION_SIDE_INVALID")
        if type(p.quantity) is not int or not 0 < p.quantity <= MAX_UNITS:
            identity.append("POSITION_UNITS_INVALID")
        if key in named or p.engine in serious_engines or "view" in serious_engines:
            identity.append("DISCREPANCY_FOR_POSITION")
        supported = p.asset_class in VALUED_ASSET_CLASSES

        executable = None
        if quote is not None and p.side in _ENTRY_SIDE:
            executable = quote.bid if p.side == PositionSide.LONG.value else quote.ask
        exposure_reasons = list(identity)
        if not supported:
            exposure_status: Status = "UNSUPPORTED"
            exposure_reasons.append("ASSET_CLASS_NOT_VALUED")
        elif executable is None:
            exposure_status = "UNKNOWN"
            exposure_reasons.append(mark_reason)
        elif exposure_reasons:
            exposure_status = "UNKNOWN"
        else:
            exposure_status = "KNOWN"
        signed = gross = None
        if exposure_status == "KNOWN":
            gross = _CONTEXT.multiply(executable, Decimal(p.quantity))
            signed = gross if p.side == PositionSide.LONG.value else _CONTEXT.minus(gross)

        basis_reasons = list(identity)
        if not snapshot.reconciliation_complete:
            basis_reasons.append("RECONCILIATION_INCOMPLETE")
        if p.engine in discrepant_engines or "view" in discrepant_engines:
            basis_reasons.append("ENGINE_DISCREPANCY_PRESENT")
        average = None
        if not supported:
            basis_status: Status = "UNSUPPORTED"
            basis_reasons.append("ASSET_CLASS_NOT_VALUED")
        elif basis_reasons:
            basis_status = "UNKNOWN"
        else:
            fills = [f for f in snapshot.fills if (f.engine, f.idea_id) == key]
            average, fill_reasons = _basis(p.side, p.symbol, p.quantity, fills, duplicate_orders)
            basis_status = "KNOWN" if average is not None else "UNKNOWN"
            basis_reasons.extend(fill_reasons)
        pnl = None
        if basis_status == "KNOWN" and exposure_status == "KNOWN":
            units = Decimal(p.quantity)
            difference = (_CONTEXT.subtract(executable, average) if p.side == PositionSide.LONG.value
                          else _CONTEXT.subtract(average, executable))
            pnl = _CONTEXT.multiply(difference, units)

        level_reasons: list[str] = []
        distances: list[Decimal | None] = []
        for name, text in (("STOP", p.stop_price), ("TARGET", p.target_price)):
            if text is None:
                distances.append(None)
                level_reasons.append(f"{name}_NOT_RECORDED")
                continue
            level = _price_text(text)
            if level is None:
                distances.append(None)
                level_reasons.append(f"{name}_LEVEL_INVALID")
            elif exposure_status != "KNOWN":
                distances.append(None)
                level_reasons.append(f"{name}_DISTANCE_UNKNOWN")
            else:
                distances.append(_CONTEXT.subtract(level, executable))

        rows.append(PositionMark(
            engine=p.engine, idea_id=p.idea_id, symbol=p.symbol, asset_class=p.asset_class, side=p.side,
            units=p.quantity, mark_status=mark_status, mark_reason=mark_reason,
            mark_observed_at=quote.observed_at if quote is not None else None,
            executable_mark=executable if exposure_status == "KNOWN" else None,
            exposure_status=exposure_status, exposure_reasons=tuple(dict.fromkeys(exposure_reasons)),
            signed_exposure=signed, gross_exposure=gross,
            basis_status=basis_status, basis_reasons=tuple(dict.fromkeys(basis_reasons)),
            average_cost=average, unrealized_pnl=pnl,
            stop_price=p.stop_price, stop_distance=distances[0],
            target_price=p.target_price, target_distance=distances[1],
            level_reasons=tuple(level_reasons), protection=p.protection, protection_note=p.protection_note,
        ))
    counts = {
        "open_positions": len(rows),
        "exposure_known": sum(r.exposure_status == "KNOWN" for r in rows),
        "basis_known": sum(r.basis_status == "KNOWN" for r in rows),
        "pnl_known": sum(r.unrealized_pnl is not None for r in rows),
        "marks_fresh": sum(r.mark_status == "FRESH" for r in rows),
        "marks_invalid": sum(r.mark_status == "INVALID" for r in rows),
    }
    complete = snapshot.reconciliation_complete and aware
    return MarkReport(as_of=as_of, as_of_aware=aware, reconciliation_complete=snapshot.reconciliation_complete,
                      status="COMPLETE" if complete else "INCOMPLETE", positions=tuple(rows), counts=counts)
