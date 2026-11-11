"""Order side of the scripted fake TWS. Requires the real ``ibapi`` (imported after importorskip).

Every answer is a protobuf frame built from the SDK's own message classes and decoded by the REAL
``EReader``/``Decoder`` path into the session's ``EWrapper``. Test-only synthetic data.

Modelled: bracket transmission (an order with ``transmit`` explicitly False is held until a
transmitting order of its bracket arrives; an ABSENT transmit field is treated as True, the
pessimistic reading), acknowledgements (openOrder + orderStatus), full and partial fills with
executions and commission reports, rejection by error code, cancel confirmation (error 202 plus
Cancelled), cancel of an unknown or filled order (10147 / 161), amendments under the same id, a lost
acknowledgement, duplicate and stale status callbacks, completed orders, callbacks for a foreign
account, and Read-Only API ON (every order answered with error 321).
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from _ibkr_fake_tws import _REQUESTS, PAPER, FakeConnection, FakeTws, error_frame, frame
from ibapi.message import IN, OUT
from ibapi.protobuf.AllOpenOrdersRequest_pb2 import AllOpenOrdersRequest
from ibapi.protobuf.CancelOrderRequest_pb2 import CancelOrderRequest
from ibapi.protobuf.CommissionAndFeesReport_pb2 import CommissionAndFeesReport
from ibapi.protobuf.CompletedOrder_pb2 import CompletedOrder
from ibapi.protobuf.CompletedOrdersEnd_pb2 import CompletedOrdersEnd
from ibapi.protobuf.CompletedOrdersRequest_pb2 import CompletedOrdersRequest
from ibapi.protobuf.Contract_pb2 import Contract as ContractProto
from ibapi.protobuf.ContractData_pb2 import ContractData
from ibapi.protobuf.ContractDataEnd_pb2 import ContractDataEnd
from ibapi.protobuf.ContractDetails_pb2 import ContractDetails as ContractDetailsProto
from ibapi.protobuf.Execution_pb2 import Execution
from ibapi.protobuf.ExecutionDetails_pb2 import ExecutionDetails
from ibapi.protobuf.ExecutionDetailsEnd_pb2 import ExecutionDetailsEnd
from ibapi.protobuf.ExecutionRequest_pb2 import ExecutionRequest
from ibapi.protobuf.IdsRequest_pb2 import IdsRequest
from ibapi.protobuf.MarketDataType_pb2 import MarketDataType
from ibapi.protobuf.NextValidId_pb2 import NextValidId
from ibapi.protobuf.OpenOrder_pb2 import OpenOrder
from ibapi.protobuf.OpenOrdersEnd_pb2 import OpenOrdersEnd
from ibapi.protobuf.Order_pb2 import Order as OrderProto
from ibapi.protobuf.OrderState_pb2 import OrderState
from ibapi.protobuf.OrderStatus_pb2 import OrderStatus
from ibapi.protobuf.PlaceOrderRequest_pb2 import PlaceOrderRequest
from ibapi.protobuf.TickPrice_pb2 import TickPrice
from ibapi.protobuf.TickString_pb2 import TickString

TLT_CON_ID = 15547841
ET = ZoneInfo("US/Eastern")
# "Inactive" is NOT final at this broker: an inactive order may be rejected, or merely held and
# later become active. The fake keeps it open (listed, cancellable) until a final state.
TERMINAL = {"Filled", "Cancelled", "ApiCancelled"}
READ_ONLY_REJECTION = (321, ("Error validating request.-'bW' : cause - The API interface is "
                            "currently in Read-Only mode."))

ORDER_REQUESTS = {
    **_REQUESTS,
    OUT.PLACE_ORDER: ("PLACE_ORDER", PlaceOrderRequest),
    OUT.CANCEL_ORDER: ("CANCEL_ORDER", CancelOrderRequest),
    OUT.REQ_ALL_OPEN_ORDERS: ("REQ_ALL_OPEN_ORDERS", AllOpenOrdersRequest),
    OUT.REQ_EXECUTIONS: ("REQ_EXECUTIONS", ExecutionRequest),
    OUT.REQ_COMPLETED_ORDERS: ("REQ_COMPLETED_ORDERS", CompletedOrdersRequest),
    OUT.REQ_IDS: ("REQ_IDS", IdsRequest),
}


def core_ref(order_ref: str) -> str:
    """The execution core's reference inside an experiment-prefixed orderRef."""
    return order_ref.split(":", 1)[1] if ":" in order_ref else order_ref


def all_day_hours(now: datetime | None = None) -> str:
    """Liquid hours covering yesterday, today and tomorrow (US/Eastern), current IBKR format."""
    today = (now or datetime.now(UTC)).astimezone(ET).date()
    segs = []
    for d in (today - timedelta(days=1), today, today + timedelta(days=1)):
        nxt = d + timedelta(days=1)
        segs.append(f"{d:%Y%m%d}:0000-{nxt:%Y%m%d}:0000")
    return ";".join(segs)


@dataclass
class FakeOrder:
    order_id: int
    client_id: int
    perm_id: int
    contract: ContractProto
    order: OrderProto
    transmit_present: bool
    held: bool = True
    status: str = ""
    filled: Decimal = Decimal(0)
    avg: float = 0.0
    rejected: str = ""
    completed_at: str = ""
    hidden: bool = False  # accepted, but not yet in any callback or lookup (broker lag)
    history: list[str] = field(default_factory=list)

    @property
    def total(self) -> Decimal:
        return Decimal(self.order.totalQuantity)

    @property
    def remaining(self) -> Decimal:
        return max(self.total - self.filled, Decimal(0))

    @property
    def working(self) -> bool:
        return (not self.held and self.status not in TERMINAL and not self.rejected
                and not self.hidden)


class FakeOrderTws(FakeTws):
    request_types = ORDER_REQUESTS

    def __init__(self) -> None:
        super().__init__()
        self.api_settings = {"readOnlyApi": False, "socketPort": 7497, "allowLocalhostOnly": True}
        self.positions = []
        self.next_order_id = 100
        self.client_id: int | None = None
        self.orders: dict[int, FakeOrder] = {}
        self.execs: list[tuple[ContractProto, Execution]] = []
        self.order_account = PAPER
        self.liquid_hours = all_day_hours()
        self.bid, self.ask, self.last = 88.10, 88.12, 88.11
        self.market_data_type = 1
        # --- knobs ---
        self.ack_lost = False  # PLACE_ORDER creates the order but sends no callback at all
        self.reject_refs: dict[str, tuple[int, str]] = {}  # orderRef -> error
        self.reject_all: tuple[int, str] | None = None  # e.g. READ_ONLY_REJECTION
        self.duplicate_callbacks = False  # every status / execution is sent twice
        self.stale_after_fill = False  # after Filled, an older "Submitted" arrives
        self.foreign_open_order = False  # open-order replies include another account's order
        self.foreign_completed = False
        self.modify_error: tuple[int, str] | None = None
        self.invisible = False  # new orders are accepted but stay hidden (lagging views)
        self.positions_lag = False  # fills do not (yet) move the position read
        self._lagged: list[tuple[ContractProto, Decimal]] = []
        self._perm = itertools.count(900001)
        self._exec_n = itertools.count(1)
        self.on.update({
            "START_API": FakeOrderTws.start_api,
            "REQ_CONTRACT_DATA": FakeOrderTws.contract_handler,
            "REQ_MKT_DATA": FakeOrderTws.quote_handler,
            "PLACE_ORDER": FakeOrderTws.place,
            "CANCEL_ORDER": FakeOrderTws.cancel,
            "REQ_ALL_OPEN_ORDERS": FakeOrderTws.all_open_orders,
            "REQ_EXECUTIONS": FakeOrderTws.executions_handler,
            "REQ_COMPLETED_ORDERS": FakeOrderTws.completed_handler,
            "REQ_IDS": lambda s, c, r: c.push(frame(IN.NEXT_VALID_ID,
                                                    NextValidId(orderId=s.next_order_id))),
        })

    # ------------------------------------------------------------------ helpers
    @property
    def conn(self) -> FakeConnection:
        return self.connections[-1]

    def push(self, raw: bytes) -> None:
        self.conn.push(raw)

    def wire_names(self) -> list[str]:
        return self.names()

    def count(self, name: str) -> int:
        return self.names().count(name)

    @staticmethod
    def start_api(s: FakeOrderTws, conn: FakeConnection, req) -> None:
        s.client_id = req.clientId
        s.send_next_valid_id = False
        FakeTws.start_api(s, conn, req)
        conn.push(frame(IN.NEXT_VALID_ID, NextValidId(orderId=s.next_order_id)))

    @staticmethod
    def contract_handler(s: FakeOrderTws, conn: FakeConnection, req) -> None:
        c = ContractProto(conId=TLT_CON_ID, symbol="TLT", secType="STK", exchange="SMART",
                          primaryExch="NASDAQ", currency="USD", localSymbol="TLT",
                          tradingClass="NMS")
        d = ContractDetailsProto(minTick="0.01", timeZoneId="US/Eastern",
                                 longName="ISHARES 20+ YEAR", tradingHours=s.liquid_hours,
                                 liquidHours=s.liquid_hours)
        conn.push(frame(IN.CONTRACT_DATA, ContractData(reqId=req.reqId, contract=c,
                                                       contractDetails=d)))
        conn.push(frame(IN.CONTRACT_DATA_END, ContractDataEnd(reqId=req.reqId)))

    @staticmethod
    def quote_handler(s: FakeOrderTws, conn: FakeConnection, req) -> None:
        rid = req.reqId
        live = s.market_data_type == 1
        conn.push(frame(IN.MARKET_DATA_TYPE, MarketDataType(reqId=rid,
                                                            marketDataType=s.market_data_type)))
        for tick, price in ((1 if live else 66, s.bid), (2 if live else 67, s.ask),
                            (4 if live else 68, s.last)):
            conn.push(frame(IN.TICK_PRICE, TickPrice(reqId=rid, tickType=tick, price=price,
                                                     size="100", attrMask=0)))
        conn.push(frame(IN.TICK_STRING, TickString(
            reqId=rid, tickType=45 if live else 88,
            value=str(int(datetime.now(UTC).timestamp())))))

    def _contract_of(self, fo: FakeOrder) -> ContractProto:
        return fo.contract

    def _order_of(self, fo: FakeOrder, account: str | None = None) -> OrderProto:
        o = OrderProto()
        o.CopyFrom(fo.order)
        o.orderId = fo.order_id
        o.clientId = fo.client_id
        o.permId = fo.perm_id
        o.account = account or self.order_account
        return o

    def _status(self, fo: FakeOrder, status: str | None = None, filled: Decimal | None = None,
                ) -> bytes:
        filled = fo.filled if filled is None else filled
        return frame(IN.ORDER_STATUS, OrderStatus(
            orderId=fo.order_id, status=status or fo.status, filled=str(filled),
            remaining=str(max(fo.total - filled, Decimal(0))), avgFillPrice=fo.avg,
            permId=fo.perm_id, parentId=fo.order.parentId, lastFillPrice=fo.avg,
            clientId=fo.client_id, whyHeld="", mktCapPrice=0.0))

    def _open(self, fo: FakeOrder, account: str | None = None) -> bytes:
        return frame(IN.OPEN_ORDER, OpenOrder(orderId=fo.order_id, contract=fo.contract,
                                              order=self._order_of(fo, account),
                                              orderState=OrderState(status=fo.status)))

    def announce(self, fo: FakeOrder) -> None:
        reps = 2 if self.duplicate_callbacks else 1
        for _ in range(reps):
            self.push(self._open(fo))
            self.push(self._status(fo))

    def _set(self, fo: FakeOrder, status: str) -> None:
        fo.status = status
        fo.history.append(status)
        if status in TERMINAL and not fo.completed_at:
            fo.completed_at = datetime.now(UTC).astimezone(ET).strftime("%Y%m%d %H:%M:%S") + \
                " US/Eastern"

    def children(self, parent_id: int) -> list[FakeOrder]:
        return [o for o in self.orders.values() if o.order.parentId == parent_id]

    # ------------------------------------------------------------------ order handlers
    @staticmethod
    def place(s: FakeOrderTws, conn: FakeConnection, req) -> None:
        oid, o = req.orderId, req.order
        if s.reject_all is not None:
            code, msg = s.reject_all
            conn.push(error_frame(oid, code, msg))
            return
        existing = s.orders.get(oid)
        if existing is not None:  # an amendment under the same id
            if s.modify_error is not None:
                conn.push(error_frame(oid, *s.modify_error))
                return
            if existing.status in TERMINAL:
                conn.push(error_frame(oid, 104, "Cannot modify a filled order."))
                return
            for name in ("totalQuantity", "lmtPrice", "auxPrice"):
                if o.HasField(name):
                    setattr(existing.order, name, getattr(o, name))
            if not s.ack_lost:
                s.announce(existing)
            return
        transmit = o.transmit if o.HasField("transmit") else True  # absent: pessimistic
        fo = FakeOrder(order_id=oid, client_id=s.client_id or 0, perm_id=next(s._perm),
                       contract=req.contract, order=o, transmit_present=o.HasField("transmit"))
        fo.hidden = s.invisible
        s.orders[oid] = fo
        s.next_order_id = max(s.next_order_id, oid + 1)
        if not transmit:
            return  # held, untransmitted: nothing is live yet
        parent = s.orders.get(o.parentId) if o.parentId else None
        group = [parent] if parent is not None and parent.held else []
        root = parent.order_id if parent is not None else oid
        group += [c for c in s.children(root) if c.held]
        if fo not in group:
            group.append(fo)
        for g in group:
            g.held = False
            is_child = bool(g.order.parentId)
            parent_open = is_child and s.orders[g.order.parentId].status != "Filled"
            s._set(g, "PreSubmitted" if parent_open else "Submitted")
            if core_ref(g.order.orderRef) in s.reject_refs:
                code, msg = s.reject_refs[core_ref(g.order.orderRef)]
                g.rejected = f"{code} {msg}"
                s._set(g, "Cancelled")
                if not s.ack_lost:
                    conn.push(error_frame(g.order_id, code, msg))
                continue
            if not s.ack_lost and not g.hidden:
                s.announce(g)

    @staticmethod
    def cancel(s: FakeOrderTws, conn: FakeConnection, req) -> None:
        fo = s.orders.get(req.orderId)
        if fo is None:
            conn.push(error_frame(req.orderId, 10147,
                                  f"OrderId {req.orderId} that needs to be cancelled is not found."))
            return
        if fo.status in TERMINAL or fo.rejected:
            conn.push(error_frame(req.orderId, 161, "Cancel attempted when order is not in a "
                                                    "cancellable state."))
            return
        for target in [fo, *[c for c in s.children(fo.order_id) if c.status not in TERMINAL]]:
            target.hidden = False
            s._set(target, "Cancelled")
            conn.push(error_frame(target.order_id, 202, "Order Canceled - reason:"))
            conn.push(s._status(target))

    def fill(self, order_id: int, qty: str | int = 1, price: float | None = None) -> str:
        fo = self.orders[order_id]
        q = Decimal(str(qty))
        px = price if price is not None else (fo.order.lmtPrice or fo.order.auxPrice)
        fo.avg = float((Decimal(str(fo.avg)) * fo.filled + Decimal(str(px)) * q)
                       / (fo.filled + q))
        fo.filled += q
        exec_id = f"0000e1a2.{next(self._exec_n):08x}.01.01"
        when = datetime.now(UTC).astimezone(ET).strftime("%Y%m%d %H:%M:%S") + " US/Eastern"
        ex = Execution(orderId=fo.order_id, clientId=fo.client_id, execId=exec_id, time=when,
                       acctNumber=self.order_account, exchange="ISLAND",
                       side="BOT" if fo.order.action == "BUY" else "SLD", shares=str(q),
                       price=px, permId=fo.perm_id, cumQty=str(fo.filled), avgPrice=fo.avg,
                       orderRef=fo.order.orderRef)
        self.execs.append((fo.contract, ex))
        self._set(fo, "Filled" if fo.remaining == 0 else "Submitted")
        sign = 1 if fo.order.action == "BUY" else -1
        self._move_position(fo.contract, sign * q)
        reps = 2 if self.duplicate_callbacks else 1
        for _ in range(reps):
            self.push(frame(IN.EXECUTION_DATA, ExecutionDetails(reqId=-1, contract=fo.contract,
                                                                execution=ex)))
            self.push(frame(IN.COMMISSION_AND_FEES_REPORT, CommissionAndFeesReport(
                execId=exec_id, commissionAndFees=1.0, currency="USD")))
            self.push(self._status(fo))
        if self.stale_after_fill and fo.status == "Filled":
            self.push(self._status(fo, "Submitted", Decimal(0)))
        if fo.status == "Filled":
            if not fo.order.parentId:  # an entry filled: its children become active
                for c in self.children(fo.order_id):
                    if c.working:
                        self._set(c, "Submitted")
                        self.push(self._status(c))
            else:  # a child filled: its OCA siblings are cancelled
                for c in self.children(fo.order.parentId):
                    if c is not fo and c.working:
                        self._set(c, "Cancelled")
                        self.push(self._status(c))
        return exec_id

    def set_status(self, order_id: int, status: str) -> None:
        """Push a broker-originated status change (e.g. Inactive, then Submitted again)."""
        fo = self.orders[order_id]
        self._set(fo, status)
        self.push(self._open(fo))
        self.push(self._status(fo))

    def catch_up_positions(self) -> None:
        self.positions_lag = False
        for contract, delta in self._lagged:
            self._move_position(contract, delta)
        self._lagged.clear()

    def _move_position(self, contract: ContractProto, delta: Decimal) -> None:
        if self.positions_lag:
            self._lagged.append((contract, delta))
            return
        for i, (acct, sym, con, qty) in enumerate(self.positions):
            if con == contract.conId and acct == PAPER:
                self.positions[i] = (acct, sym, con, str(Decimal(qty) + delta))
                return
        self.positions.append((PAPER, contract.symbol, contract.conId, str(delta)))

    @staticmethod
    def all_open_orders(s: FakeOrderTws, conn: FakeConnection, req) -> None:
        for fo in s.orders.values():
            if fo.working:
                conn.push(s._open(fo))
                conn.push(s._status(fo))
        if s.foreign_open_order:
            foreign = FakeOrder(order_id=7, client_id=99, perm_id=555, contract=ContractProto(
                conId=TLT_CON_ID, symbol="TLT", secType="STK", currency="USD", exchange="SMART"),
                order=OrderProto(action="SELL", totalQuantity="1", orderType="LMT", lmtPrice=90.0,
                                 tif="DAY"), transmit_present=True, held=False, status="Submitted")
            conn.push(s._open(foreign, account="DU9990002"))
        conn.push(frame(IN.OPEN_ORDER_END, OpenOrdersEnd()))

    @staticmethod
    def executions_handler(s: FakeOrderTws, conn: FakeConnection, req) -> None:
        acct = req.executionFilter.acctCode
        for contract, ex in s.execs:
            if not acct or ex.acctNumber == acct:
                conn.push(frame(IN.EXECUTION_DATA, ExecutionDetails(reqId=req.reqId,
                                                                    contract=contract,
                                                                    execution=ex)))
        conn.push(frame(IN.EXECUTION_DATA_END, ExecutionDetailsEnd(reqId=req.reqId)))

    @staticmethod
    def completed_handler(s: FakeOrderTws, conn: FakeConnection, req) -> None:
        for fo in s.orders.values():
            if fo.status in TERMINAL:
                conn.push(frame(IN.COMPLETED_ORDER, CompletedOrder(
                    contract=fo.contract, order=s._order_of(fo),
                    orderState=OrderState(status=fo.status, completedTime=fo.completed_at,
                                          completedStatus=fo.rejected or fo.status))))
        if s.foreign_completed:
            conn.push(frame(IN.COMPLETED_ORDER, CompletedOrder(
                contract=ContractProto(conId=TLT_CON_ID, symbol="TLT", secType="STK",
                                       currency="USD"),
                order=OrderProto(orderId=3, clientId=99, account="DU9990002", action="BUY",
                                 totalQuantity="1", orderType="LMT", lmtPrice=80.0, tif="DAY"),
                orderState=OrderState(status="Filled"))))
        conn.push(frame(IN.COMPLETED_ORDERS_END, CompletedOrdersEnd()))

    # ------------------------------------------------------------------ scripted pushes
    def push_foreign_execution(self) -> None:
        ex = Execution(orderId=1, clientId=99, execId="0000ffff.00000001.01.01",
                       time="20261111 10:00:00 US/Eastern", acctNumber="DU9990002", side="BOT",
                       shares="1", price=88.0, orderRef="x")
        self.push(frame(IN.EXECUTION_DATA, ExecutionDetails(
            reqId=-1, contract=ContractProto(conId=TLT_CON_ID, symbol="TLT", secType="STK",
                                             currency="USD"), execution=ex)))

    def placed(self) -> list[PlaceOrderRequest]:
        return [p for n, p in self.requests if n == "PLACE_ORDER"]
