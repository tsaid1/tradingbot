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
from decimal import Decimal
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
    def __init__(self, session: Any) -> None:
        self._s = session
        self._asset_class: dict[int, AssetClass] = {}

    # ------------------------------------------------------------------ helpers
    def _contract_for(self, con_id: int, symbol: str, sec_type: str, currency: str,
                      exchange: str = "SMART", primary: str | None = None) -> ContractRef:
        pinned = self._s.contract
        extra: dict[str, Any] = {}
        if con_id == pinned.con_id:
            primary = primary or pinned.primary_exchange or None
            extra = {"broker_contract_id": pinned.con_id, "multiplier": 1,
                     "min_tick": Decimal(str(pinned.min_tick)), "size_increment": 1}
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

    def _find(self, order_ref: str) -> int:
        """The newest order id of this client carrying ``order_ref`` that is not terminal."""
        try:
            snaps = self._s.open_orders()
        except IbkrError as exc:
            raise BrokerError(f"open orders unreadable: {exc}") from exc
        ids = [s.order_id for s in snaps if s.own and s.order_ref == order_ref]
        if not ids:
            raise OrderNotFound(f"no working order with ref {order_ref}")
        return max(ids)

    def _sent_since(self, before: int | None) -> bool:
        after = self._s.submissions_used
        return before is None or after is None or after > before

    # ------------------------------------------------------------------ Broker protocol
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
            raise BrokerError(str(exc)) from exc
        except _SessionPartialBracket as exc:
            raise PartialBracket(str(exc)) from exc
        except OrderOutcomeUnknown as exc:
            raise AckLost(str(exc)) from exc
        except IbkrError as exc:
            if self._sent_since(before):
                raise AckLost(f"outcome unknown after sending: {exc}") from exc
            raise BrokerError(str(exc)) from exc
        return [self._order(s) for s in snaps]

    def modify(self, order_ref: str, *, limit_price: Decimal | None = None,
               stop_price: Decimal | None = None, quantity: int | None = None) -> BrokerOrder:
        order_id = self._find(order_ref)
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
        order_id = self._find(order_ref)
        try:
            return self._order(self._s.cancel(order_id))
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
        return [BrokerPosition(
            account=self._s.account_id,
            contract=self._contract_for(r.con_id, r.symbol, r.sec_type, r.currency,
                                        r.exchange or "SMART"),
            quantity=_int_qty(r.quantity, "position"),
            avg_cost=Decimal(str(r.avg_cost)) if r.avg_cost else None) for r in rows]
