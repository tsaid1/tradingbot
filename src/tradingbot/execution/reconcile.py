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

from tradingbot.execution.broker import (
    Broker,
    BrokerError,
    ConditionDirection,
    OrderSide,
    OrderStatus,
    OrderType,
    QuoteQuality,
    reference_quote,
)
from tradingbot.execution.store import (
    PROTECTIVE_ROLES,
    UNRESOLVED_STATES,
    ExecutionStore,
    Role,
)
from tradingbot.models import Instrument, PositionSide, StrictModel


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
    # The working stop is not a protective stop: wrong type, wrong side, or (when a quote is
    # available) not below the bid for a long / above the ask for a short.
    PROTECTIVE_STOP_INVALID = "PROTECTIVE_STOP_INVALID"
    # The working stop's price differs from the ledger's confirmed stop for the idea (review F3).
    STOP_PRICE_MISMATCH = "STOP_PRICE_MISMATCH"
    # Fills of an intent an operator settled as NOT_AT_BROKER or CANCELLED (beyond what it had
    # when settled), or an open SETTLED_INTENT_CONTRADICTED exception: quantity no broker evidence
    # or verified settlement explains. Never explained away (review F1).
    SETTLED_INTENT_CONTRADICTED = "SETTLED_INTENT_CONTRADICTED"


# Never explained away: while any of these is present the report is not clean.
SAFETY_KINDS = frozenset(
    {
        DiscrepancyKind.POSITION_WITHOUT_PROTECTIVE_STOP,
        DiscrepancyKind.PROTECTIVE_STOP_INVALID,
        DiscrepancyKind.STOP_PRICE_MISMATCH,
        DiscrepancyKind.WORKING_ENTRY_ON_CLOSED_IDEA,
        DiscrepancyKind.PARTIAL_GROUP,
        DiscrepancyKind.SETTLED_INTENT_CONTRADICTED,
    }
)


class Discrepancy(StrictModel):
    kind: DiscrepancyKind
    symbol: str | None = None
    # The instrument's identity (ContractRef.contract_key) where the discrepancy is per contract.
    contract_key: str | None = None
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


def _reference_stop_problem(stop, p, quote) -> tuple[DiscrepancyKind | None, str]:
    """A REFERENCE_TRIGGER stop: a conditional exit in the idea's contract, held on the ledger's
    reference series, in the protective direction, at the ledger's level (multi-asset mapping)."""
    long = p.position_side == PositionSide.LONG
    c = stop.condition
    if c is None:
        return DiscrepancyKind.PROTECTIVE_STOP_INVALID, "reference stop carries no price condition"
    if stop.side != (OrderSide.SELL if long else OrderSide.BUY):
        return DiscrepancyKind.PROTECTIVE_STOP_INVALID, (
            f"stop side {stop.side.value} would add to a {p.position_side.value} position"
        )
    if p.contract_key and stop.contract.contract_key != p.contract_key:
        return DiscrepancyKind.PROTECTIVE_STOP_INVALID, "conditional exit is in another contract"
    if c.contract.contract_key != p.reference_key:
        return DiscrepancyKind.PROTECTIVE_STOP_INVALID, (
            f"condition is on {c.contract.contract_key}, not the reference {p.reference_key}"
        )
    want = ConditionDirection.BELOW if long else ConditionDirection.ABOVE
    if c.direction != want:
        return DiscrepancyKind.PROTECTIVE_STOP_INVALID, f"condition direction {c.direction.value}"
    ledger = Decimal(p.stop_price) if p.stop_price else None
    if ledger is None or c.trigger_price != ledger:
        return DiscrepancyKind.STOP_PRICE_MISMATCH, (
            f"condition trigger {c.trigger_price} differs from the ledger's stop {ledger}"
        )
    if quote is not None and quote.quality != QuoteQuality.UNAVAILABLE:
        last = quote.last
        if last is None and quote.bid is not None and quote.ask is not None:
            last = (quote.bid + quote.ask) / 2
        if last is not None and not (last > ledger if long else last < ledger):
            return DiscrepancyKind.PROTECTIVE_STOP_INVALID, (
                f"reference {last} is already through the stop condition {ledger}"
            )
    return None, ""


def _stop_problem(stop, p, quote) -> tuple[DiscrepancyKind | None, str]:
    """Type, side and price against the ledger's confirmed stop, then the market side. The average
    entry is deliberately not used: break-even and profit-locking stops are legitimate (F3)."""
    long = p.position_side == PositionSide.LONG
    want_side = OrderSide.SELL if long else OrderSide.BUY
    if stop.order_type != OrderType.STP:
        return DiscrepancyKind.PROTECTIVE_STOP_INVALID, (
            f"protective order is {stop.order_type.value}, not a stop"
        )
    if stop.side != want_side:
        return DiscrepancyKind.PROTECTIVE_STOP_INVALID, (
            f"stop side {stop.side.value} would add to a {p.position_side.value} position"
        )
    ledger = Decimal(p.stop_price) if p.stop_price else None
    if ledger is None or stop.stop_price is None or stop.stop_price != ledger:
        return DiscrepancyKind.STOP_PRICE_MISMATCH, (
            f"working stop {stop.stop_price} differs from the ledger's stop {ledger}"
        )
    if quote is not None and quote.quality != QuoteQuality.UNAVAILABLE:
        if long and quote.bid is not None and stop.stop_price >= quote.bid:
            return DiscrepancyKind.PROTECTIVE_STOP_INVALID, (
                f"sell stop {stop.stop_price} is not below the bid {quote.bid}"
            )
        if not long and quote.ask is not None and stop.stop_price <= quote.ask:
            return DiscrepancyKind.PROTECTIVE_STOP_INVALID, (
                f"buy stop {stop.stop_price} is not above the ask {quote.ask}"
            )
    return None, ""


def _ledger_key(broker: Broker, p) -> str:
    """The idea's contract key; a row written before round 2 is resolved by symbol."""
    if p.contract_key:
        return p.contract_key
    try:
        return broker.resolve(Instrument(asset_class=p.asset_class, symbol=p.symbol)).contract_key
    except BrokerError:
        return f"SYMBOL:{p.symbol}"


def _quote(broker: Broker, contract):
    try:
        return broker.quote(contract)
    except BrokerError:
        return None


SETTLED_WITHOUT_FILL = ("OPERATOR_SETTLED_NOT_AT_BROKER", "OPERATOR_SETTLED_CANCELLED")


def _declared_filled(store: ExecutionStore, order_ref: str) -> int:
    for e in reversed(store.journal(order_ref)):
        if e.event == "OPERATOR_SETTLEMENT":
            d = e.detail
            return int(d.get("declared_filled", d.get("before", {}).get("intent", {})
                             .get("filled", 0)) or 0)
    return 0


def _settlement_discrepancies(store, intents, open_exceptions, broker_net, ledger_net):
    out = []
    for e in open_exceptions:
        if e.kind == DiscrepancyKind.SETTLED_INTENT_CONTRADICTED.value:
            i = intents.get(e.order_ref or "")
            out.append(Discrepancy(
                kind=DiscrepancyKind.SETTLED_INTENT_CONTRADICTED,
                symbol=i.symbol if i else None, contract_key=i.contract_key if i else None,
                idea_id=e.idea_id, order_ref=e.order_ref, detail=e.detail[:300],
            ))
    reported = {d.order_ref for d in out}
    for i in intents.values():
        if i.order_ref in reported or "SETTLED_INTENT_CONTRADICTED" in i.reasons:
            continue
        if not any(r in SETTLED_WITHOUT_FILL for r in i.reasons):
            continue
        excess = store.filled_quantity(i.order_ref) - _declared_filled(store, i.order_ref)
        if excess <= 0:
            continue
        key = i.contract_key
        out.append(Discrepancy(
            kind=DiscrepancyKind.SETTLED_INTENT_CONTRADICTED, symbol=i.symbol, contract_key=key,
            idea_id=i.idea_id, order_ref=i.order_ref,
            detail=(f"{excess} filled beyond the operator's declaration; broker "
                    f"{broker_net.get(key, 0) if key else '?'}, explained by evidence "
                    f"{ledger_net.get(key, 0) - excess if key else '?'}"),
        ))
    return out


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

    # 1. Net quantity per contract (never per symbol: two months of a future, or a stock and a
    #    future sharing a symbol, are different instruments): ledger (signed) versus broker.
    ledger_net: dict[str, int] = {}
    symbols: dict[str, str] = {}
    for p in ledger:
        if p.quantity > 0:
            key = _ledger_key(broker, p)
            sign = 1 if p.position_side == PositionSide.LONG else -1
            ledger_net[key] = ledger_net.get(key, 0) + sign * p.quantity
            symbols[key] = p.symbol
    broker_net: dict[str, int] = {}
    for bp in broker_positions:
        key = bp.contract.contract_key
        broker_net[key] = broker_net.get(key, 0) + bp.quantity
        symbols[key] = bp.contract.symbol
    for key in sorted(set(ledger_net) | set(broker_net)):
        ours, theirs = ledger_net.get(key, 0), broker_net.get(key, 0)
        if ours == theirs:
            continue
        if ours == 0:
            kind, detail = DiscrepancyKind.UNEXPLAINED_BROKER_POSITION, f"broker holds {theirs}"
        elif theirs == 0:
            kind, detail = DiscrepancyKind.LEDGER_POSITION_MISSING_AT_BROKER, f"ledger holds {ours}"
        else:
            kind, detail = DiscrepancyKind.QUANTITY_MISMATCH, f"ledger {ours}, broker {theirs}"
        out.append(Discrepancy(kind=kind, symbol=symbols[key], contract_key=key, detail=detail))

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
                    contract_key=p.contract_key,
                    idea_id=p.idea_id,
                    order_ref=p.stop_ref,
                    detail=f"ledger {p.quantity}, fully committed to working exits; no stop",
                )
            )
            continue
        stop = open_by_ref.get(p.stop_ref) if p.stop_ref else None
        if stop is not None and stop.working and p.protection_series == "REFERENCE":
            ref_quote = (
                reference_quote(broker, stop.condition.contract, now) if stop.condition else None
            )
            kind, problem = _reference_stop_problem(stop, p, ref_quote)
        elif stop is not None and stop.working:
            kind, problem = _stop_problem(stop, p, _quote(broker, stop.contract))
        else:
            kind = None
        if kind is not None:
            out.append(
                Discrepancy(
                    kind=kind,
                    symbol=p.symbol,
                    contract_key=p.contract_key,
                    idea_id=p.idea_id,
                    order_ref=p.stop_ref,
                    detail=problem,
                )
            )
        if p.backstop_ref:
            b = open_by_ref.get(p.backstop_ref)
            ok = (
                b is not None and b.working and b.status != OrderStatus.PENDING_CANCEL
                and b.order_type == OrderType.STP and b.remaining == cover[p.idea_id]
                and b.stop_price == Decimal(p.backstop_price or "0")
            )
            if not ok:
                out.append(
                    Discrepancy(
                        kind=DiscrepancyKind.POSITION_WITHOUT_PROTECTIVE_STOP,
                        symbol=p.symbol,
                        contract_key=p.contract_key,
                        idea_id=p.idea_id,
                        order_ref=p.backstop_ref,
                        detail=(
                            f"required backstop STP {p.backstop_price} x{cover[p.idea_id]} not "
                            f"working ({b.status.value if b else 'absent'})"
                        ),
                        explained_by=covering("UNPROTECTED_POSITION", p.idea_id),
                    )
                )
        # A PreSubmitted stop counts only once its parent's own fills are applied (never inferred).
        unproven = (
            stop is not None
            and stop.status == OrderStatus.PRE_SUBMITTED
            and store.filled_quantity(stop.parent_ref or p.stop_ref.rsplit(".", 1)[0]) == 0
        )
        if (
            stop is None
            or not stop.working
            or unproven
            or stop.status == OrderStatus.PENDING_CANCEL
            or stop.remaining != cover[p.idea_id]
        ):
            covered = stop.remaining if stop is not None else 0
            out.append(
                Discrepancy(
                    kind=DiscrepancyKind.POSITION_WITHOUT_PROTECTIVE_STOP,
                    symbol=p.symbol,
                    contract_key=p.contract_key,
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
                    contract_key=o.contract.contract_key,
                    order_ref=o.order_ref,
                    detail="working order not placed by this ledger",
                )
            )
            continue
        pos = positions.get(row.idea_id)
        if row.role in PROTECTIVE_ROLES:
            flat = pos is None or (pos.quantity == 0 and pos.status != "PENDING")
            if flat:
                out.append(
                    Discrepancy(
                        kind=DiscrepancyKind.ORPHAN_WORKING_ORDER,
                        symbol=o.contract.symbol,
                        contract_key=o.contract.contract_key,
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
                        contract_key=o.contract.contract_key,
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
                        contract_key=o.contract.contract_key,
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
                    contract_key=o.contract.contract_key,
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

    # 7. Operator settlements the broker contradicts (review F1): the broker position may hold
    #    quantity that only fills of an intent declared never filled account for.
    out.extend(_settlement_discrepancies(store, intents, open_exceptions, broker_net, ledger_net))

    return ReconciliationReport(
        checked_at=now,
        account=account,
        ledger_ideas_open=sum(1 for p in ledger if p.quantity > 0),
        broker_positions=len(broker_positions),
        broker_open_orders=len(open_orders),
        discrepancies=out,
    )
