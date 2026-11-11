"""Read-only reconciliation of the follower ledger against the broker.

It reads the execution store and the broker's positions, open orders and executions, and returns
typed discrepancies. It never submits, amends, cancels or writes anything: resolving a discrepancy
is the operator's decision (or a later, separately reviewed automation).

A discrepancy is *explained* when an open exception record already covers it (for example an
``UNPROTECTED_POSITION`` the coordinator raised itself); anything else is unexplained. A safety
discrepancy (``SAFETY_KINDS``: a position without valid confirmed protection, a working entry of a
closed idea, a half-executed group) keeps the report unclean even when explained (review S1).
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import Enum

from pydantic import Field

from tradingbot.execution.broker import Broker, BrokerError, OrderSide, OrderStatus, OrderType
from tradingbot.execution.store import UNRESOLVED_STATES, ExecutionStore, Role
from tradingbot.models import PositionSide, StrictModel


class DiscrepancyKind(str, Enum):
    UNEXPLAINED_BROKER_POSITION = "UNEXPLAINED_BROKER_POSITION"
    LEDGER_POSITION_MISSING_AT_BROKER = "LEDGER_POSITION_MISSING_AT_BROKER"
    QUANTITY_MISMATCH = "QUANTITY_MISMATCH"
    POSITION_WITHOUT_PROTECTIVE_STOP = "POSITION_WITHOUT_PROTECTIVE_STOP"
    ORPHAN_WORKING_ORDER = "ORPHAN_WORKING_ORDER"
    INTENT_STUCK_UNRESOLVED = "INTENT_STUCK_UNRESOLVED"
    UNAPPLIED_EXECUTION = "UNAPPLIED_EXECUTION"
    BROKER_UNREADABLE = "BROKER_UNREADABLE"
    # A working entry or add of an idea that is closed or being closed: it would reopen it.
    WORKING_ENTRY_ON_CLOSED_IDEA = "WORKING_ENTRY_ON_CLOSED_IDEA"
    # An approved atomic group executed only in part (an open PARTIAL_GROUP exception): never
    # explained away, it stays unclean until a human resolves the exception.
    PARTIAL_GROUP = "PARTIAL_GROUP"
    # The working stop is not a sell-stop below the average entry (long) / a buy-stop above it
    # (short): wrong side, wrong type, or a price on the wrong side of the entry.
    PROTECTIVE_STOP_INVALID = "PROTECTIVE_STOP_INVALID"


# Never explained away: while any of these is present the report is not clean.
SAFETY_KINDS = frozenset(
    {
        DiscrepancyKind.POSITION_WITHOUT_PROTECTIVE_STOP,
        DiscrepancyKind.PROTECTIVE_STOP_INVALID,
        DiscrepancyKind.WORKING_ENTRY_ON_CLOSED_IDEA,
        DiscrepancyKind.PARTIAL_GROUP,
    }
)


class Discrepancy(StrictModel):
    kind: DiscrepancyKind
    symbol: str | None = None
    idea_id: str | None = None
    order_ref: str | None = None
    detail: str = ""
    explained_by: int | None = None  # id of the open exception record that covers it


class ReconciliationReport(StrictModel):
    checked_at: datetime
    account: str
    ledger_ideas_open: int = 0
    broker_positions: int = 0
    broker_open_orders: int = 0
    discrepancies: list[Discrepancy] = Field(default_factory=list)

    @property
    def unexplained(self) -> list[Discrepancy]:
        return [d for d in self.discrepancies if d.explained_by is None]

    @property
    def blocking(self) -> list[Discrepancy]:
        """Everything that keeps the report unclean: unexplained, or a safety discrepancy."""
        return [
            d for d in self.discrepancies if d.explained_by is None or d.kind in SAFETY_KINDS
        ]

    @property
    def clean(self) -> bool:
        return not self.blocking


def _stop_problem(stop, p) -> str:
    long = p.position_side == PositionSide.LONG
    want_side = OrderSide.SELL if long else OrderSide.BUY
    if stop.order_type != OrderType.STP:
        return f"protective order is {stop.order_type.value}, not a stop"
    if stop.side != want_side:
        return f"stop side {stop.side.value} would add to a {p.position_side.value} position"
    if p.avg_price and stop.stop_price is not None:
        avg = Decimal(p.avg_price)
        if (long and stop.stop_price >= avg) or (not long and stop.stop_price <= avg):
            return (
                f"stop {stop.stop_price} is on the wrong side of the average entry {avg} for a "
                f"{p.position_side.value} position"
            )
    return ""


def reconcile(
    store: ExecutionStore, broker: Broker, *, now: datetime, stuck_after_seconds: int = 60
) -> ReconciliationReport:
    account = broker.account_id()
    try:
        broker_positions = broker.positions()
        open_orders = broker.open_orders()
        executions = broker.executions()
    except BrokerError as exc:
        return ReconciliationReport(
            checked_at=now,
            account=account,
            discrepancies=[Discrepancy(kind=DiscrepancyKind.BROKER_UNREADABLE, detail=str(exc))],
        )
    ledger = store.positions()
    rows = {r.order_ref: r for r in store.orders()}
    intents = {i.order_ref: i for i in store.intents()}
    open_exceptions = store.exceptions(open_only=True)
    out: list[Discrepancy] = []

    def covering(kind: str, idea_id: str | None) -> int | None:
        return next(
            (e.id for e in open_exceptions if e.kind == kind and e.idea_id == idea_id), None
        )

    # 1. Net quantity per symbol: ledger (signed by idea side) versus broker.
    ledger_net: dict[str, int] = {}
    for p in ledger:
        if p.quantity > 0:
            sign = 1 if p.position_side == PositionSide.LONG else -1
            ledger_net[p.symbol] = ledger_net.get(p.symbol, 0) + sign * p.quantity
    broker_net: dict[str, int] = {}
    for bp in broker_positions:
        broker_net[bp.contract.symbol] = broker_net.get(bp.contract.symbol, 0) + bp.quantity
    for symbol in sorted(set(ledger_net) | set(broker_net)):
        ours, theirs = ledger_net.get(symbol, 0), broker_net.get(symbol, 0)
        if ours == theirs:
            continue
        if ours == 0:
            kind, detail = DiscrepancyKind.UNEXPLAINED_BROKER_POSITION, f"broker holds {theirs}"
        elif theirs == 0:
            kind, detail = DiscrepancyKind.LEDGER_POSITION_MISSING_AT_BROKER, f"ledger holds {ours}"
        else:
            kind, detail = DiscrepancyKind.QUANTITY_MISMATCH, f"ledger {ours}, broker {theirs}"
        out.append(Discrepancy(kind=kind, symbol=symbol, detail=detail))

    # 2. Every open idea with a stop level needs a working stop covering exactly its quantity less
    #    what is committed to exits that may be live (review R1).
    open_by_ref = {o.order_ref: o for o in open_orders}
    cover = {
        p.idea_id: max(p.quantity - store.committed_exit_quantity(p.idea_id), 0) for p in ledger
    }
    for p in ledger:
        if p.quantity <= 0 or p.stop_price is None:
            continue
        if cover[p.idea_id] == 0:
            # Everything held is committed to working exits: no stop may work beside them, so the
            # position has no stop until those exits fill.
            out.append(
                Discrepancy(
                    kind=DiscrepancyKind.POSITION_WITHOUT_PROTECTIVE_STOP,
                    symbol=p.symbol,
                    idea_id=p.idea_id,
                    order_ref=p.stop_ref,
                    detail=f"ledger {p.quantity}, fully committed to working exits; no stop",
                )
            )
            continue
        stop = open_by_ref.get(p.stop_ref) if p.stop_ref else None
        if stop is not None and stop.working:
            problem = _stop_problem(stop, p)
            if problem:
                out.append(
                    Discrepancy(
                        kind=DiscrepancyKind.PROTECTIVE_STOP_INVALID,
                        symbol=p.symbol,
                        idea_id=p.idea_id,
                        order_ref=p.stop_ref,
                        detail=problem,
                    )
                )
        if (
            stop is None
            or not stop.working
            or stop.status == OrderStatus.PENDING_CANCEL
            or stop.remaining != cover[p.idea_id]
        ):
            covered = stop.remaining if stop is not None else 0
            out.append(
                Discrepancy(
                    kind=DiscrepancyKind.POSITION_WITHOUT_PROTECTIVE_STOP,
                    symbol=p.symbol,
                    idea_id=p.idea_id,
                    order_ref=p.stop_ref,
                    detail=f"ledger {p.quantity}, working stop covers {covered}",
                    explained_by=covering("UNPROTECTED_POSITION", p.idea_id),
                )
            )

    # 3. Working orders the ledger cannot account for.
    positions = {p.idea_id: p for p in ledger}
    for o in open_orders:
        row = rows.get(o.order_ref)
        if row is None:
            out.append(
                Discrepancy(
                    kind=DiscrepancyKind.ORPHAN_WORKING_ORDER,
                    symbol=o.contract.symbol,
                    order_ref=o.order_ref,
                    detail="working order not placed by this ledger",
                )
            )
            continue
        pos = positions.get(row.idea_id)
        if row.role in (Role.STOP, Role.TARGET):
            flat = pos is None or (pos.quantity == 0 and pos.status != "PENDING")
            if flat:
                out.append(
                    Discrepancy(
                        kind=DiscrepancyKind.ORPHAN_WORKING_ORDER,
                        symbol=o.contract.symbol,
                        idea_id=row.idea_id,
                        order_ref=o.order_ref,
                        detail="protective order working while the follower idea is flat",
                    )
                )
            elif pos.quantity and o.remaining > cover[pos.idea_id]:
                out.append(
                    Discrepancy(
                        kind=DiscrepancyKind.ORPHAN_WORKING_ORDER,
                        symbol=o.contract.symbol,
                        idea_id=row.idea_id,
                        order_ref=o.order_ref,
                        detail=(
                            f"protective order oversized: {o.remaining} > {cover[pos.idea_id]} "
                            f"(held {pos.quantity}, less working exits)"
                        ),
                    )
                )
        if row.role == Role.ENTRY and pos is not None:
            closing = any(
                i.idea_id == row.idea_id
                and i.action.value == "CLOSE"
                and i.state.value in ("RECORDED", "UNRESOLVED", "WORKING")
                for i in intents.values()
            )
            if pos.status in ("CLOSED", "REVERSED") or closing:
                out.append(
                    Discrepancy(
                        kind=DiscrepancyKind.WORKING_ENTRY_ON_CLOSED_IDEA,
                        symbol=o.contract.symbol,
                        idea_id=row.idea_id,
                        order_ref=o.order_ref,
                        detail=f"entry/add working while the idea is {pos.status}"
                        + (" and being closed" if closing else ""),
                    )
                )
        intent = intents.get(row.intent_ref)
        if intent is not None and intent.state.value in ("HALTED", "REFUSED", "REJECTED"):
            out.append(
                Discrepancy(
                    kind=DiscrepancyKind.ORPHAN_WORKING_ORDER,
                    symbol=o.contract.symbol,
                    idea_id=row.idea_id,
                    order_ref=o.order_ref,
                    detail=f"working although its intent is {intent.state.value}",
                )
            )

    # 4. Executions on our refs that the ledger has not applied.
    for f in executions:
        if f.order_ref in rows and not store.has_fill(f.exec_id):
            out.append(
                Discrepancy(
                    kind=DiscrepancyKind.UNAPPLIED_EXECUTION,
                    symbol=f.contract.symbol,
                    idea_id=rows[f.order_ref].idea_id,
                    order_ref=f.order_ref,
                    detail=f"{f.exec_id}: {f.side.value} {f.quantity} @ {f.price}",
                )
            )

    # 5. Intents whose outcome is still unknown.
    for i in intents.values():
        if (
            i.state in UNRESOLVED_STATES
            and (now - i.updated_at).total_seconds() >= stuck_after_seconds
        ):
            out.append(
                Discrepancy(
                    kind=DiscrepancyKind.INTENT_STUCK_UNRESOLVED,
                    symbol=i.symbol,
                    idea_id=i.idea_id,
                    order_ref=i.order_ref,
                    detail=f"{i.state.value} since {i.updated_at.isoformat()}",
                )
            )

    # 6. Atomic groups that executed only in part.
    for e in open_exceptions:
        if e.kind == DiscrepancyKind.PARTIAL_GROUP.value:
            out.append(
                Discrepancy(
                    kind=DiscrepancyKind.PARTIAL_GROUP,
                    idea_id=e.idea_id,
                    order_ref=e.order_ref,
                    detail=e.detail,
                )
            )

    return ReconciliationReport(
        checked_at=now,
        account=account,
        ledger_ideas_open=sum(1 for p in ledger if p.quantity > 0),
        broker_positions=len(broker_positions),
        broker_open_orders=len(open_orders),
        discrepancies=out,
    )
