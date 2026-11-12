"""``IbkrPaperBroker``: the execution core's ``Broker`` protocol on a ``PaperOrderSession``.

SDK-free itself: it talks to the session through plain records. Error mapping:

* nothing was sent (guard refusal, a failed read before sending, a session not READY): a definite
  ``BrokerError`` (the coordinator records SUBMIT_ERROR and never resends);
* anything else once a submission was recorded (no acknowledgement, a send failure, a partial
  bracket, the session poisoned or disconnected mid-request): ``AckLost`` (here ``PartialBracket``
  for the bracket case). It never retries; the coordinator looks the order up by ``orderRef``.

Whether anything was sent is decided from the durable submission counter, not from the exception.
"""
from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from decimal import ROUND_CEILING, Decimal
from typing import Any

from tradingbot.execution.broker import (
    AckLost,
    BrokerError,
    BrokerOrder,
    BrokerPosition,
    BrokerQuote,
    ContractRef,
    Fill,
    OrderIntent,
    OrderNotFound,
    OrderSide,
    OrderStatus,
    OrderType,
    QuoteQuality,
    TimeInForce,
)
from tradingbot.execution.coordinator import snap_to_tick
from tradingbot.ibkr.errors import IbkrError
from tradingbot.ibkr_orders.records import (
    AmendRejected,
    ExecRecord,
    OrderNotWorking,
    OrderOutcomeUnknown,
    OrderRefused,
    OrderSnapshot,
    OrderSpec,
)
from tradingbot.ibkr_orders.records import PartialBracket as _SessionPartialBracket
from tradingbot.ibkr_orders.timefmt import parse_broker_time
from tradingbot.models import AssetClass, Instrument


class ProtectionLost(BrokerError):
    """POSITION OPEN, PROTECTION ABSENT: an exit was refused or failed (definitely not sent)
    after this binding had cancelled the protective stop for it, and no working stop covers the
    held position. The execution core records it as a submit error; its next ``sync`` raises
    UNPROTECTED_POSITION (``IbkrPaperBroker.protection_lost`` holds the message)."""


class PartialBracket(AckLost):
    """Part of a bracket was accepted and part was not. The parent's cancel has been requested;
    the coordinator must treat the entry as of unknown outcome (look it up), never as success."""


def _contract_ref(**fields: Any) -> ContractRef:
    """Forward compatible: optional fields the ContractRef model does not (yet) define are
    dropped, so a later additive change to the protocol needs no change here."""
    known = ContractRef.model_fields
    return ContractRef(**{k: v for k, v in fields.items() if k in known and v is not None})


def _int_qty(value: Decimal, what: str) -> int:
    if value != value.to_integral_value():
        raise BrokerError(f"{what}: fractional quantity {value} is not supported")
    return int(value)


class IbkrPaperBroker:
    """``exit_collar_pct`` is the execution core's ``MockPolicy.max_price_deviation_pct``: the
    core prices a close as a marketable SELL limit at the bid less this collar, snapped up to the
    tick, and the exit dry check prices its probe the same way. Without it (a binding used only
    for reads) no protective stop can be cancelled or shrunk while a position is held."""

    def __init__(self, session: Any, *, exit_collar_pct: Decimal | None = None) -> None:
        self._s = session
        self._exit_collar_pct = exit_collar_pct
        self._asset_class: dict[int, AssetClass] = {}
        self._stops_cancelled: set[str] = set()  # protective stops this binding cancelled
        self.protection_lost = ""  # set when an exit failed after its stop was cancelled

    # ------------------------------------------------------------------ helpers
    def _contract_for(self, con_id: int, symbol: str, sec_type: str, currency: str,
                      exchange: str = "SMART", primary: str | None = None) -> ContractRef:
        pinned = self._s.contract
        extra: dict[str, Any] = {}
        if con_id == pinned.con_id:
            # the experiment's one contract is a stock (caps): multiplier 1, whole shares; no
            # expiry, strike or right. Min tick and local symbol come from its contract details.
            primary = primary or pinned.primary_exchange or None
            extra = {"broker_contract_id": pinned.con_id, "multiplier": Decimal(1),
                     "min_tick": Decimal(str(pinned.min_tick)), "size_increment": 1,
                     "local_symbol": pinned.local_symbol or None}
        return _contract_ref(
            symbol=symbol, asset_class=self._asset_class.get(con_id, AssetClass.EQUITY),
            sec_type=sec_type or "STK", exchange=exchange or "SMART", currency=currency or "USD",
            primary_exchange=primary or None, con_id=con_id or None, **extra)

    def _order(self, s: OrderSnapshot) -> BrokerOrder:
        try:
            order_type = OrderType(s.order_type)
            tif = TimeInForce(s.tif)
            side = OrderSide(s.action)
        except ValueError:
            # a working order this protocol cannot express (e.g. another client's TRAIL or IOC):
            # refusing to read is safer than hiding it from exit netting
            raise BrokerError(f"unmappable order {s.order_id}: {s.order_type}/{s.tif}/{s.action}"
                              ) from None
        status, reason = s.status, s.reason
        if status == OrderStatus.INACTIVE:
            # At IBKR Inactive is not final (held, or rejected): never handed to the core as
            # INACTIVE, which it records as rejected. UNKNOWN keeps it unresolved, unprotected.
            status = OrderStatus.UNKNOWN
            reason = "IBKR_INACTIVE: held or rejected, not final" + (f"; {reason}" if reason
                                                                       else "")
        return BrokerOrder(
            broker_order_id=str(s.order_id if s.own else f"perm:{s.perm_id}"),
            order_ref=s.order_ref or f"UNREF:{s.perm_id or s.order_id}",
            parent_ref=s.parent_ref,
            contract=self._contract_for(s.con_id, s.symbol, s.sec_type, s.currency),
            side=side, quantity=_int_qty(s.total_quantity, "order"), order_type=order_type,
            limit_price=s.limit_price if order_type == OrderType.LMT else None,
            stop_price=s.stop_price if order_type == OrderType.STP else None,
            tif=tif, status=status, filled_quantity=_int_qty(s.filled, "filled"),
            avg_fill_price=s.avg_fill_price, reason=reason, updated_at=s.updated_at,
        )

    def _find_snap(self, order_ref: str) -> OrderSnapshot:
        """The newest order of this client carrying ``order_ref`` that is not terminal."""
        try:
            snaps = self._s.open_orders()
        except IbkrError as exc:
            raise BrokerError(f"open orders unreadable: {exc}") from exc
        mine = [s for s in snaps if s.own and s.order_ref == order_ref]
        if not mine:
            raise OrderNotFound(f"no working order with ref {order_ref}")
        return max(mine, key=lambda s: s.order_id)

    def _find(self, order_ref: str) -> int:
        return self._find_snap(order_ref).order_id

    def _dry_check_exit(self, snap: OrderSnapshot) -> bool:
        """For a protective SELL stop: refuse to cancel or shrink it when the exit it makes room
        for would be refused by the guard. True when the order is such a stop."""
        if snap.action != "SELL" or snap.order_type != "STP":
            return False
        try:
            self._s.dry_check_exit(snap.order_id, limit_price=self.exit_limit())
        except OrderRefused as exc:
            raise BrokerError(f"EXIT_WOULD_BE_REFUSED: {exc}; the protective stop "
                              f"{snap.order_ref} was NOT cancelled or resized") from exc
        except IbkrError as exc:
            raise BrokerError(f"exit dry check failed: {exc}; the protective stop "
                              f"{snap.order_ref} was NOT cancelled or resized") from exc
        return True

    def exit_limit(self) -> Decimal | None:
        """The closing SELL limit the execution core would send now (``_exit_limit``): the bid
        of a fresh quote less the collar, snapped UP to the min tick. None when it cannot be
        priced (no collar, no quote, no positive bid)."""
        if self._exit_collar_pct is None:
            return None
        pinned = self._s.contract
        try:
            q = self.quote(self._contract_for(pinned.con_id, pinned.symbol, pinned.sec_type,
                                              pinned.currency))
        except BrokerError:
            return None
        if q.quality == QuoteQuality.UNAVAILABLE or q.bid is None or q.bid <= 0:
            return None
        band = self._exit_collar_pct / Decimal(100)
        return snap_to_tick(q.bid * (1 - band), Decimal(str(pinned.min_tick)), ROUND_CEILING)

    def _exit_failed(self, order: OrderIntent, children: Sequence[OrderIntent],
                     exc: Exception) -> BrokerError:
        """A definite failure (nothing sent). For an exit after a stop was cancelled for it,
        report POSITION OPEN, PROTECTION ABSENT unambiguously."""
        if order.side != OrderSide.SELL or children or not self._stops_cancelled:
            return BrokerError(str(exc))
        try:
            held = sum((p.quantity for p in self._s.positions()
                        if p.con_id == self._s.contract.con_id), Decimal(0))
            stops = [s for s in self._s.open_orders()
                     if s.own and s.action == "SELL" and s.order_type == "STP"]
        except IbkrError as read:
            held, stops = None, []
            exc = Exception(f"{exc}; and the position/orders are unreadable: {read}")
        if held is not None and (held <= 0 or stops):
            return BrokerError(str(exc))
        self.protection_lost = (
            f"POSITION_OPEN_PROTECTION_ABSENT: {held if held is not None else 'unknown'} "
            f"{self._s.contract.symbol} held and no working protective stop (cancelled for this "
            f"exit); the exit {order.order_ref} was not sent: {exc}")
        return ProtectionLost(self.protection_lost)

    def _sent_since(self, before: int | None) -> bool:
        after = self._s.submissions_used
        return before is None or after is None or after > before

    # ------------------------------------------------------------------ Broker protocol
    def supports_price_conditions(self) -> bool:
        """Main's optional ``ConditionalOrders`` capability: never. The core then sends no
        reference-triggered order here, and ``submit`` refuses one anyway."""
        return False

    def account_id(self) -> str:
        return self._s.account_id

    def resolve(self, instrument: Instrument) -> ContractRef:
        pinned = self._s.contract
        if instrument.symbol != pinned.symbol or instrument.asset_class not in (
                AssetClass.EQUITY, AssetClass.ETF):
            raise BrokerError(f"{instrument.symbol} is not the experiment's contract")
        self._asset_class[pinned.con_id] = instrument.asset_class
        return self._contract_for(pinned.con_id, pinned.symbol, pinned.sec_type,
                                  pinned.currency, pinned.exchange or "SMART",
                                  pinned.primary_exchange)

    def quote(self, contract: ContractRef) -> BrokerQuote:
        """The session's stream-first quote with its own quality label (never upgraded)."""
        pinned = self._s.contract
        if contract.con_id not in (None, pinned.con_id) or contract.symbol != pinned.symbol:
            raise BrokerError("quotes are available for the experiment's contract only")
        try:
            report = self._s.quote(pinned)
        except IbkrError as exc:
            raise BrokerError(f"quote failed: {exc}") from exc
        attempts = list(report.attempts)
        best = next((a for a in reversed(attempts) if a.two_sided_valid), attempts[-1])
        quality = QuoteQuality(best.quality.value)

        def dec(v: float | None) -> Decimal | None:
            return None if v is None else Decimal(str(v))

        return BrokerQuote(contract=contract, bid=dec(best.bid), ask=dec(best.ask),
                           last=dec(best.last), received_at=datetime.fromisoformat(
                               best.received_at), quality=quality)

    def submit(self, order: OrderIntent, children: Sequence[OrderIntent] = ()) -> list[BrokerOrder]:
        if order.parent_ref is not None or any(c.parent_ref != order.order_ref for c in children):
            raise BrokerError("children must name the entry as parent; the entry has none")
        if any(getattr(o, "condition", None) is not None for o in (order, *children)):
            # main's optional PriceCondition: never sent unconditionally (nothing was sent)
            raise BrokerError("PRICE_CONDITION_NOT_SUPPORTED: this binding places no "
                              "conditional order")
        pinned = self._s.contract
        for o in (order, *children):
            if o.contract.con_id not in (None, pinned.con_id) or o.contract.symbol != \
                    pinned.symbol:
                raise BrokerError("order is not for the experiment's contract")
        specs = [OrderSpec(order_ref=o.order_ref, action=o.side.value, quantity=o.quantity,
                           order_type=o.order_type.value, limit_price=o.limit_price,
                           stop_price=o.stop_price, tif=o.tif.value)
                 for o in (order, *children)]
        before = self._s.submissions_used
        try:
            snaps = self._s.place(specs)
        except OrderRefused as exc:
            if self._sent_since(before):
                raise PartialBracket(f"refused mid-bracket: {exc}") from exc
            raise self._exit_failed(order, children, exc) from exc
        except _SessionPartialBracket as exc:
            raise PartialBracket(str(exc)) from exc
        except OrderOutcomeUnknown as exc:
            raise AckLost(str(exc)) from exc
        except IbkrError as exc:
            if self._sent_since(before):
                raise AckLost(f"outcome unknown after sending: {exc}") from exc
            raise self._exit_failed(order, children, exc) from exc
        if order.side == OrderSide.SELL and not children:
            self._stops_cancelled.clear()  # the exit is at the broker: it is the protection now
        return [self._order(s) for s in snaps]

    def modify(self, order_ref: str, *, limit_price: Decimal | None = None,
               stop_price: Decimal | None = None, quantity: int | None = None) -> BrokerOrder:
        snap = self._find_snap(order_ref)
        order_id = snap.order_id
        if quantity is not None and quantity < snap.total_quantity:
            self._dry_check_exit(snap)  # a protective stop shrunk for an exit
        before = self._s.submissions_used
        try:
            snap = self._s.modify(order_id, limit_price=limit_price, stop_price=stop_price,
                                  quantity=quantity)
        except OrderNotWorking as exc:
            raise OrderNotFound(str(exc)) from exc
        except (OrderRefused, AmendRejected) as exc:
            if isinstance(exc, OrderRefused) and self._sent_since(before):
                raise AckLost(str(exc)) from exc
            raise BrokerError(str(exc)) from exc
        except OrderOutcomeUnknown as exc:
            raise AckLost(str(exc)) from exc
        except IbkrError as exc:
            if self._sent_since(before):
                raise AckLost(f"amendment outcome unknown: {exc}") from exc
            raise BrokerError(str(exc)) from exc
        return self._order(snap)

    def cancel(self, order_ref: str) -> BrokerOrder:
        snap = self._find_snap(order_ref)
        order_id = snap.order_id
        stop = self._dry_check_exit(snap)  # never remove protection for a refused exit
        try:
            out = self._order(self._s.cancel(order_id))
            if stop:
                self._stops_cancelled.add(order_ref)
            return out
        except OrderNotWorking as exc:
            raise OrderNotFound(str(exc)) from exc
        except IbkrError as exc:
            raise BrokerError(f"cancel failed: {exc}") from exc

    def open_orders(self) -> list[BrokerOrder]:
        try:
            snaps = self._s.open_orders()
        except IbkrError as exc:
            raise BrokerError(f"open orders unreadable: {exc}") from exc
        return [self._order(s) for s in snaps]

    def executions(self, since: datetime | None = None) -> list[Fill]:
        try:
            recs: list[ExecRecord] = self._s.executions()
        except IbkrError as exc:
            raise BrokerError(f"executions unreadable: {exc}") from exc
        out = []
        for r in recs:
            try:
                side = OrderSide(r.side)
            except ValueError:
                raise BrokerError(f"execution {r.exec_id}: unknown side {r.side!r}") from None
            at = parse_broker_time(r.time) or r.received_at
            if since is not None and at < since:
                continue
            out.append(Fill(
                exec_id=r.exec_id, broker_order_id=str(r.order_id), order_ref=r.order_ref,
                contract=self._contract_for(r.con_id, r.symbol, r.sec_type, r.currency),
                side=side, quantity=_int_qty(r.shares, "execution"), price=r.price,
                executed_at=at))
        return out

    def completed_orders(self, since: datetime | None = None) -> list[BrokerOrder]:
        try:
            snaps = self._s.completed_orders()
        except IbkrError as exc:
            raise BrokerError(f"completed orders unreadable: {exc}") from exc
        out = []
        for s in snaps:
            if since is not None and s.updated_at < since:
                continue
            if s.status not in (OrderStatus.FILLED, OrderStatus.CANCELLED,
                                OrderStatus.API_CANCELLED, OrderStatus.REJECTED,
                                OrderStatus.INACTIVE):
                s = s.__class__(**{**s.__dict__, "status": OrderStatus.UNKNOWN})
            out.append(self._order(s))
        return out

    def positions(self) -> list[BrokerPosition]:
        try:
            rows = self._s.positions()
        except IbkrError as exc:
            raise BrokerError(f"positions unreadable: {exc}") from exc
        out = []
        for r in rows:
            contract = self._contract_for(r.con_id, r.symbol, r.sec_type, r.currency,
                                          r.exchange or "SMART")
            # IBKR's avgCost is multiplied by the contract multiplier; BrokerPosition.avg_cost is
            # per unit BEFORE the multiplier, so divide (1 for this experiment's stock).
            avg = (Decimal(str(r.avg_cost)) / contract.multiplier) if r.avg_cost else None
            out.append(BrokerPosition(account=self._s.account_id, contract=contract,
                                      quantity=_int_qty(r.quantity, "position"), avg_cost=avg))
        return out
