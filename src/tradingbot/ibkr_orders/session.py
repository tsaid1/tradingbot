"""``PaperOrderSession``: the ONE order-capable session, for a named, code-capped paper experiment.

It is a separate, explicitly constructed subclass of the read-only ``IbkrSession`` (which is not
changed and still cannot send an order). It adds:

* construction only from a ``PaperOrderConfig`` (a hand-written ``orders = "<experiment>"`` entry
  naming an experiment defined in ``caps.py``); every pre-socket check and the identity gate of the
  read-only session still run first;
* the order-mode config read: TWS must report Read-Only API explicitly OFF, localhost-only ON and
  the configured socket port (``OrderModeSettingRefused`` otherwise, before any order);
* the TLT contract resolved and pinned by the session itself (its conId, min tick, liquid hours);
* ``_OrderClient``: deny by default like ``_ReadOnlyClient``, with exactly the order methods added;
* ``_OrderGuardedConnection``: the wire choke point. A PLACE_ORDER or CANCEL_ORDER frame passes
  only if it is protobuf-framed and byte-identical to a one-shot permit that ``_send_place`` /
  ``_send_cancel`` issued AFTER ``order_guard`` returned ALLOW and AFTER the durable counter
  recorded the submission; the decoded PLACE_ORDER is re-checked against the caps there too;
* order, status, execution and completed-order callbacks, any of which naming another account
  poisons the session.
"""
from __future__ import annotations

import itertools
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from ibapi.client import EClient
from ibapi.client_utils import (
    createCancelOrderRequestProto,
    createExecutionRequestProto,
    createOrderProto,
    createPlaceOrderRequestProto,
)
from ibapi.common import PROTOBUF_MSG_ID
from ibapi.const import UNSET_DECIMAL, UNSET_DOUBLE, UNSET_INTEGER
from ibapi.contract import Contract
from ibapi.execution import ExecutionFilter
from ibapi.message import OUT
from ibapi.order import Order
from ibapi.order_cancel import OrderCancel
from ibapi.protobuf.AllOpenOrdersRequest_pb2 import AllOpenOrdersRequest
from ibapi.protobuf.CancelOrderRequest_pb2 import CancelOrderRequest
from ibapi.protobuf.CompletedOrdersRequest_pb2 import CompletedOrdersRequest
from ibapi.protobuf.ConfigRequest_pb2 import ConfigRequest
from ibapi.protobuf.IdsRequest_pb2 import IdsRequest
from ibapi.protobuf.PlaceOrderRequest_pb2 import PlaceOrderRequest

from tradingbot.execution.broker import WORKING_STATUSES, OrderStatus
from tradingbot.ibkr.errors import (
    CONNECTION_CODES,
    AccountIdentityError,
    ConfigReadRejected,
    ConfigReadTimeout,
    ConfigReadUnsupported,
    IbkrRequestError,
    IbkrTimeout,
    ReadOnlyNotVerified,
    SessionStateError,
)
from tradingbot.ibkr.identity import check_row_account
from tradingbot.ibkr.models import ContractQuery, ResolvedContract
from tradingbot.ibkr.redact import redact_accounts
from tradingbot.ibkr.session import (
    MIN_SERVER_VER_CONFIG,
    IbkrSession,
    SessionState,
    _denied,
    _Wrapper,
)
from tradingbot.ibkr_orders.caps import EXPERIMENTS, ExperimentCaps
from tradingbot.ibkr_orders.config import PaperOrderConfig
from tradingbot.ibkr_orders.counter import CounterCorrupt, SubmissionCounter
from tradingbot.ibkr_orders.guard import (
    ContractFacts,
    GuardContext,
    ProposedOrder,
    Refusal,
    RefusalReason,
    WorkingOrder,
    order_guard,
)
from tradingbot.ibkr_orders.mapping import (
    FINAL_STATUSES,
    OPEN_STATUSES,
    classify_order_error,
    map_status,
    merge_status,
)
from tradingbot.ibkr_orders.records import (
    AmendRejected,
    CancelFailed,
    ExecRecord,
    ExperimentContractMismatch,
    OrderModeSettingRefused,
    OrderModeViolation,
    OrderNotWorking,
    OrderOutcomeUnknown,
    OrderRefused,
    OrderSnapshot,
    OrderSpec,
    PartialBracket,
)
from tradingbot.ibkr_orders.timefmt import parse_broker_time

# --- the order-mode allowlists ------------------------------------------------------------------
ORDER_MESSAGES = frozenset({OUT.PLACE_ORDER, OUT.CANCEL_ORDER})  # need a one-shot permit each
ORDER_READ_MESSAGES = frozenset({OUT.REQ_ALL_OPEN_ORDERS, OUT.REQ_EXECUTIONS,
                                 OUT.REQ_COMPLETED_ORDERS, OUT.REQ_IDS})
# The read ids order mode allows: an EXPLICIT set, exactly the read-only allowlist before
# historical bars were added. It does not follow later additions to ``ALLOWED_OUT_MESSAGES``
# (e.g. historical bars, 20 / 25): each one needs its own decision and review for order mode.
ORDER_MODE_READ_MESSAGES = frozenset({
    OUT.START_API, OUT.REQ_CONTRACT_DATA, OUT.CANCEL_CONTRACT_DATA, OUT.REQ_ACCOUNT_SUMMARY,
    OUT.CANCEL_ACCOUNT_SUMMARY, OUT.REQ_POSITIONS, OUT.CANCEL_POSITIONS, OUT.REQ_MKT_DATA,
    OUT.CANCEL_MKT_DATA, OUT.REQ_MARKET_DATA_TYPE, OUT.REQ_CONFIG, OUT.REQ_SEC_DEF_OPT_PARAMS,
})
ORDER_MODE_OUT_MESSAGES = ORDER_MODE_READ_MESSAGES | ORDER_MESSAGES | ORDER_READ_MESSAGES
# The read client methods order mode allows: EXPLICIT, as for the ids (no historical methods).
ORDER_MODE_READ_CLIENT_METHODS = frozenset({
    "connect", "disconnect", "isConnected", "run", "reset", "setConnState", "checkConnected",
    "validateInvalidSymbols", "useProtoBuf", "serverVersion", "twsConnectionTime", "logRequest",
    "msgLoopTmo", "msgLoopRec", "keyboardInterrupt", "keyboardInterruptHard", "sendMsg",
    "sendMsgProtoBuf", "startApi", "startApiProtoBuf",
    "reqContractDetails", "reqContractDataProtoBuf", "cancelContractData",
    "cancelContractDataProtoBuf", "reqAccountSummary", "reqAccountSummaryProtoBuf",
    "cancelAccountSummary", "cancelAccountSummaryProtoBuf", "reqPositions", "reqPositionsProtoBuf",
    "cancelPositions", "cancelPositionsProtoBuf", "reqMktData", "reqMarketDataProtoBuf",
    "cancelMktData", "cancelMarketDataProtoBuf", "reqMarketDataType", "reqMarketDataTypeProtoBuf",
    "reqConfigProtoBuf", "reqSecDefOptParams", "reqSecDefOptParamsProtoBuf",
})
ORDER_MODE_CLIENT_METHODS = ORDER_MODE_READ_CLIENT_METHODS | frozenset({
    # placing goes through our own protobuf (explicit transmit/outsideRth presence), never the
    # text-or-proto ``placeOrder`` / ``cancelOrder`` wrappers, which stay denied
    "placeOrderProtoBuf", "validateOrderParameters", "validateAttachedOrdersParameters",
    "cancelOrderProtoBuf", "reqAllOpenOrdersProtoBuf", "reqExecutionsProtoBuf",
    "reqCompletedOrdersProtoBuf", "reqIdsProtoBuf",
})
# An earlier session's submission found in no lookup counts as possibly live for this long.
JOURNAL_SETTLE_SECONDS = 120.0
# The exit dry check judges the liquid-hours rule this far ahead: between a stop's cancel and the
# close, the binding and the core each wait for a quote (up to the stream and quote timeouts).
EXIT_HOURS_MARGIN_SECONDS = 60
REQ_ID_BASE = 1_900_000_000  # request ids live above every order id, so error routing is exact

# Order-proto fields this session sets itself; every other field must equal the SDK default.
_OWN_ORDER_FIELDS = frozenset({"action", "totalQuantity", "orderType", "lmtPrice", "auxPrice",
                               "tif", "account", "outsideRth", "orderRef", "transmit", "parentId",
                               "ocaGroup", "ocaType"})
_DEFAULT_ORDER_FIELDS = {f.name: v for f, v in createOrderProto(Order()).ListFields()}
_ALLOWED_CONTRACT_FIELDS = frozenset({"conId", "symbol", "secType", "exchange", "currency",
                                      "primaryExch"})


def _frame_id(msg: bytes) -> tuple[int, bool]:
    """(message id, protobuf?) of a framed request at server version >= 201 (raw 4-byte id)."""
    mid = int.from_bytes(msg[4:8], "big")
    return (mid - PROTOBUF_MSG_ID, True) if mid > PROTOBUF_MSG_ID else (mid, False)


def _wire_check(req: PlaceOrderRequest, policy: tuple[str, int, ExperimentCaps]) -> list[str]:
    """Defence in depth at the wire: the decoded PLACE_ORDER against the caps and SDK defaults."""
    account, con_id, caps = policy
    o, c = req.order, req.contract
    problems = []
    if req.attachedOrders.ByteSize():
        problems.append("attached orders")
    for f, value in o.ListFields():
        if f.name not in _OWN_ORDER_FIELDS and _DEFAULT_ORDER_FIELDS.get(f.name) != value:
            problems.append(f"order field {f.name} differs from the SDK default")
    for f, _ in c.ListFields():
        if f.name not in _ALLOWED_CONTRACT_FIELDS:
            problems.append(f"contract field {f.name}")
    if not (o.HasField("account") and o.account == account):
        problems.append("account")
    if not (c.HasField("conId") and c.conId == con_id and c.symbol == caps.symbol
            and c.secType == caps.sec_type and c.currency == caps.currency):
        problems.append("contract")
    if o.orderType not in caps.order_types or o.tif not in caps.tifs:
        problems.append("order type / tif")
    if o.action == "BUY" and o.orderType != "LMT":
        problems.append("a BUY must be a limit order")
    if not (o.HasField("outsideRth") and o.outsideRth is False) and not caps.outside_rth_allowed:
        problems.append("outsideRth must be explicitly false")
    if (not o.HasField("transmit") or not o.orderRef.startswith(caps.ref_prefix)
            or len(o.orderRef) <= len(caps.ref_prefix) or o.action not in ("BUY", "SELL")):
        problems.append("transmit / orderRef / action")
    try:
        qty = Decimal(o.totalQuantity)
        if not 0 < qty <= caps.max_order_quantity:
            problems.append("quantity")
    except InvalidOperation:
        problems.append("quantity")
    return problems


# --- the guarded connection (module-private registries, see tradingbot.ibkr.session) -------------
_RAW: dict[int, Any] = {}
_PERMITS: dict[int, list[tuple[int, bytes]]] = {}
_POLICY: dict[int, tuple[str, int, ExperimentCaps]] = {}
_TOKENS = itertools.count(1)
_RAW_METHODS = frozenset({"connect", "disconnect", "isConnected", "recvMsg", "sendMsg"})


def _raw_call(guard: _OrderGuardedConnection, name: str, *args: Any) -> Any:
    if name not in _RAW_METHODS:
        raise OrderModeViolation(f"connection method {name!r} is not exposed")
    err: BaseException | None = None
    try:
        return getattr(_RAW[guard._token], name)(*args)
    except BaseException as exc:  # noqa: BLE001 - re-raised below, stripped
        err = exc
    err.__traceback__ = None
    err.__context__ = err.__cause__ = None
    raise err


def _issue_permit(conn: _OrderGuardedConnection, msg_id: int, body: bytes) -> None:
    _PERMITS[conn._token].append((msg_id, body))


def _set_policy(conn: _OrderGuardedConnection, policy: tuple[str, int, ExperimentCaps]) -> None:
    _POLICY[conn._token] = policy


class _OrderGuardedConnection:
    __slots__ = ("_client", "_handshake_sent", "_token")  # no __weakref__

    def __init__(self, raw, client: EClient) -> None:
        self._token = next(_TOKENS)
        _RAW[self._token] = raw
        _PERMITS[self._token] = []
        self._client = client
        self._handshake_sent = False

    def __del__(self) -> None:
        token = getattr(self, "_token", None)
        for registry in (_RAW, _PERMITS, _POLICY):
            registry.pop(token, None)

    def sendMsg(self, msg: bytes):
        if not isinstance(msg, (bytes, bytearray)):
            raise OrderModeViolation("outgoing frame must be bytes")
        msg = bytes(msg)
        if msg.startswith(b"API\0"):
            if self._handshake_sent:
                raise OrderModeViolation("a second API handshake frame is refused")
            self._handshake_sent = True
            return _raw_call(self, "sendMsg", msg)
        if (not self._handshake_sent or len(msg) < 8
                or int.from_bytes(msg[:4], "big") != len(msg) - 4
                or (self._client.serverVersion() or 0) < 201):
            raise OrderModeViolation("malformed or out-of-order outgoing frame refused")
        mid, proto = _frame_id(msg)
        if mid not in ORDER_MODE_OUT_MESSAGES:
            raise OrderModeViolation(f"outgoing message id {mid} refused at the connection")
        if mid in ORDER_MESSAGES:
            body = msg[8:]
            permits = _PERMITS.get(self._token, [])
            if not proto or (mid, body) not in permits:
                raise OrderModeViolation(f"message id {mid} without a guard permit refused")
            permits.remove((mid, body))
            if mid == OUT.PLACE_ORDER:
                policy = _POLICY.get(self._token)
                problems = (["no contract pinned"] if policy is None
                            else _wire_check(PlaceOrderRequest.FromString(body), policy))
                if problems:
                    raise OrderModeViolation(f"PLACE_ORDER refused at the wire: {problems}")
        return _raw_call(self, "sendMsg", msg)

    def connect(self):
        return _raw_call(self, "connect")

    def disconnect(self):
        return _raw_call(self, "disconnect")

    def isConnected(self) -> bool:
        return bool(_raw_call(self, "isConnected"))

    def recvMsg(self) -> bytes:
        return _raw_call(self, "recvMsg")


class _OrderClient(EClient):
    @property
    def conn(self):
        return self.__dict__.get("_guarded_conn")

    @conn.setter
    def conn(self, value) -> None:
        self.__dict__["_guarded_conn"] = (
            None if value is None or isinstance(value, _OrderGuardedConnection)
            else _OrderGuardedConnection(value, self))

    def sendMsg(self, msgId: int, msg: str):  # text framing: never an order message
        if msgId not in ORDER_MODE_OUT_MESSAGES or msgId in ORDER_MESSAGES:
            raise OrderModeViolation(f"outgoing message id {msgId} is not allowed (text)")
        return super().sendMsg(msgId, msg)

    def sendMsgProtoBuf(self, msgId: int, msg: bytes):
        if msgId - PROTOBUF_MSG_ID not in ORDER_MODE_OUT_MESSAGES:
            raise OrderModeViolation(f"outgoing protobuf message id {msgId} is not allowed")
        return super().sendMsgProtoBuf(msgId, msg)


for _name in dir(EClient):
    if (not _name.startswith("_") and callable(getattr(EClient, _name))
            and _name not in ORDER_MODE_CLIENT_METHODS):
        setattr(_OrderClient, _name, _denied(_name))
del _name


class _OrderWrapper(_Wrapper):
    """Adds the order callbacks. Signatures match ibapi 1052.1 ``EWrapper`` (asserted in tests)."""

    def nextValidId(self, orderId):
        self._call(self._s._on_next_valid_id_value, orderId)

    def openOrder(self, orderId, contract, order, orderState):
        self._call(self._s._on_open_order, orderId, contract, order, orderState)

    def openOrderEnd(self):
        self._call(self._s._on_kind_end, "open_orders")

    def orderStatus(self, orderId, status, filled, remaining, avgFillPrice, permId, parentId,
                    lastFillPrice, clientId, whyHeld, mktCapPrice):
        self._call(self._s._on_order_status, orderId, status, filled, remaining, avgFillPrice,
                   permId, parentId, clientId)

    def execDetails(self, reqId, contract, execution):
        self._call(self._s._on_exec_details, reqId, contract, execution)

    def execDetailsEnd(self, reqId):
        self._call(self._s._on_end, reqId)

    def commissionAndFeesReport(self, commissionAndFeesReport):
        self._call(self._s._on_commission, commissionAndFeesReport)

    def completedOrder(self, contract, order, orderState):
        self._call(self._s._on_completed_order, contract, order, orderState)

    def completedOrdersEnd(self):
        self._call(self._s._on_kind_end, "completed")


# --- order tracking ------------------------------------------------------------------------------
def _split_ref(raw: str, prefix: str) -> tuple[str, bool]:
    """(the execution core's reference, carries the experiment prefix?)"""
    if prefix and raw.startswith(prefix) and len(raw) > len(prefix):
        return raw[len(prefix):], True
    return raw, False


def _dec(value) -> Decimal | None:
    if value is None or value == UNSET_DECIMAL:
        return None
    try:
        d = Decimal(str(value))
    except InvalidOperation:
        return None
    return d if d.is_finite() else None


def _price(value) -> Decimal | None:
    if value is None or value == UNSET_DOUBLE:
        return None
    d = _dec(value)
    return d if d is not None and d > 0 else None


@dataclass
class _Track:
    order_id: int
    own: bool
    client_id: int = 0
    perm_id: int = 0
    order_ref: str = ""  # the execution core's reference (the experiment prefix removed)
    experiment: bool = False  # the broker's orderRef carries this experiment's prefix
    parent_id: int = 0
    con_id: int = 0
    symbol: str = ""
    sec_type: str = ""
    currency: str = ""
    action: str = ""
    total: Decimal = Decimal(0)
    order_type: str = ""
    limit_price: Decimal | None = None
    stop_price: Decimal | None = None
    tif: str = ""
    outside_rth: bool = False
    oca_group: str = ""
    account: str = ""
    status: OrderStatus | None = None
    raw_status: str = ""
    filled: Decimal = Decimal(0)
    remaining: Decimal | None = None
    avg_fill: Decimal | None = None
    proposed: ProposedOrder | None = None  # what this session last sent for this id
    errors: list[tuple[int, str, str]] = field(default_factory=list)
    rejected: str = ""
    send_failed: bool = False
    placed_here: bool = False  # a new order (not an amendment) this session sent
    cancel_error: str = ""
    modify_pending: bool = False
    modify_error: str = ""
    open_seq: int = 0
    event_seq: int = 0
    error_seq: int = 0
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def fill_from_order(self, contract, order, state_status: str | None,
                        prefix: str = "") -> None:
        self.client_id = int(order.clientId) if order.clientId != UNSET_INTEGER else 0
        self.perm_id = int(order.permId) if order.permId not in (None, UNSET_INTEGER) else 0
        self.order_ref, self.experiment = _split_ref(order.orderRef or "", prefix)
        self.parent_id = int(order.parentId or 0)
        self.con_id = int(contract.conId or 0)
        self.symbol, self.sec_type, self.currency = contract.symbol, contract.secType, \
            contract.currency
        self.action = order.action
        self.total = _dec(order.totalQuantity) or Decimal(0)
        self.order_type = order.orderType
        self.limit_price = _price(order.lmtPrice) if order.orderType == "LMT" else None
        self.stop_price = _price(order.auxPrice) if order.orderType == "STP" else None
        self.tif = order.tif
        self.outside_rth = bool(order.outsideRth)
        self.oca_group = order.ocaGroup or ""
        self.account = order.account
        if state_status:
            self.raw_status = state_status
            self.status = merge_status(self.status, map_status(state_status, self.filled,
                                                               self.remaining),
                                       filled=self.filled, total=self.total)
        self.updated_at = datetime.now(UTC)

    def as_proposed(self) -> ProposedOrder:
        return self.proposed or ProposedOrder(
            order_id=self.order_id, order_ref=self.order_ref, account=self.account,
            con_id=self.con_id, symbol=self.symbol, sec_type=self.sec_type,
            currency=self.currency, action=self.action, quantity=self.total,
            order_type=self.order_type, limit_price=self.limit_price, stop_price=self.stop_price,
            tif=self.tif, outside_rth=self.outside_rth, parent_id=self.parent_id, transmit=True,
            oca_group=self.oca_group)

    @property
    def working(self) -> bool:
        """Not final. Includes INACTIVE: at IBKR an inactive order can still become active."""
        return self.status is not None and self.status in OPEN_STATUSES




class PaperOrderSession(IbkrSession):
    """Order-capable paper session for ONE experiment. Use as a context manager. Every order
    operation re-reads the position and the working orders, runs ``order_guard`` immediately
    before each ``placeOrder`` and refuses on anything but ALLOW."""

    def __init__(self, config: PaperOrderConfig, *, ack_timeout: float = 5.0,
                 cancel_wait: float = 3.0, connect_timeout: float = 15.0,
                 request_timeout: float = 10.0, quote_timeout: float = 20.0,
                 stream_seconds: float = 8.0, stream_settle_seconds: float = 1.0,
                 shutdown_timeout: float = 5.0) -> None:
        """Only the config and timeouts: there is no parameter that replaces or skips the local
        profile and listener checks (the read-only session's ``run_preflight``), the identity
        gate, the order-mode config read, the guard, the counter or the clock (structural
        test)."""
        if (not isinstance(config, PaperOrderConfig) or not config.issued
                or EXPERIMENTS.get(config.experiment.name) is not config.experiment):
            raise SessionStateError("PaperOrderSession needs a PaperOrderConfig loaded from the "
                                    "private file, naming an experiment defined in code")
        super().__init__(config.connection, connect_timeout=connect_timeout,
                         request_timeout=request_timeout, quote_timeout=quote_timeout,
                         stream_seconds=stream_seconds,
                         stream_settle_seconds=stream_settle_seconds,
                         shutdown_timeout=shutdown_timeout)  # preflight: always run_preflight
        self._order_cfg = config
        self._caps = config.experiment
        self._ack_timeout = ack_timeout
        self._cancel_wait = cancel_wait
        self._wrapper = _OrderWrapper(self)
        self._client = _OrderClient(self._wrapper)  # the read-only client is discarded unused
        self._next_req_id = REQ_ID_BASE
        self._counter = SubmissionCounter(config.state_dir, config.experiment,
                                          config.connection.masked_account())
        self.count_problem = ""  # why the submission count is unknown, if it is
        self._broker_count: int | None = None  # taken once, before the first submission
        self._journal_resolved: dict[int, str] = {}  # earlier sessions' orders seen final
        self._order_lock = threading.RLock()
        self._tracks: dict[int, _Track] = {}
        self._status_cache: dict[int, tuple] = {}
        self._execs: dict[str, ExecRecord] = {}
        self.commissions: dict[str, tuple[float, str]] = {}
        self._next_valid_id: int | None = None
        self._nvid_seq = 0
        self._next_local_id = 0
        self._contract: ResolvedContract | None = None
        self._baseline_position: Decimal | None = None  # read once at connect
        self.position_check: dict | None = None  # the read vs the experiment's executions
        self.order_mode_status = "NOT_CHECKED"
        self.order_frames_sent = 0  # PLACE_ORDER and CANCEL_ORDER frames handed to the SDK
        # this session's own order events with receipt times (evidence; no account in it)
        self.order_events: list[dict] = []

    def _event(self, event: str, order_id: int, **detail) -> None:
        """Caller holds the lock (or is the only writer). Receipt times for the evidence."""
        self.order_events.append({"at": datetime.now(UTC).isoformat(timespec="microseconds"),
                                  "mono": time.monotonic(), "event": event,
                                  "order_id": order_id, **detail})

    def _now(self) -> datetime:
        """The wall clock, aware (UTC). Not a parameter: tests replace it in a test-only
        subclass."""
        return datetime.now(UTC)

    # ------------------------------------------------------------------ properties
    def __repr__(self) -> str:
        return (f"PaperOrderSession(state={self._state.value}, account={self.masked_account!r}, "
                f"experiment={self._caps.name!r})")

    @property
    def account_id(self) -> str:
        return self._cfg.account

    @property
    def caps(self) -> ExperimentCaps:
        return self._caps

    @property
    def contract(self) -> ResolvedContract:
        if self._contract is None:
            raise SessionStateError("no contract pinned: session is not in order mode")
        return self._contract

    @property
    def submissions_used(self) -> int | None:
        return self._counter.used

    @property
    def submissions_remaining(self) -> int | None:
        used = self._counter.used
        return None if used is None else max(self._caps.max_submissions - used, 0)

    def order_ids(self) -> dict[int, str]:
        with self._cond:
            return {i: t.order_ref for i, t in self._tracks.items() if t.own}

    def historical_bars(self, request, *, timeout=None):
        """Not available on the order session: refused before the pacer or anything else is
        touched. Use a read-only ``IbkrSession``."""
        raise OrderModeViolation("historical bars are not available on the order session; use a "
                                 "read-only IbkrSession")

    # ------------------------------------------------------------------ lifecycle
    def connect(self) -> None:
        try:  # local, before any socket. A locked counter refuses to connect; a missing or
            self._counter.open()  # broken one leaves the count unknown: nothing can be placed
        except CounterCorrupt as exc:  # (SUBMISSION_COUNT_UNKNOWN), cancels stay possible
            self.count_problem = str(exc)
        try:
            super().connect()
            caps = self._caps
            resolved = self.resolve_contract(ContractQuery(
                symbol=caps.symbol, sec_type=caps.sec_type, exchange="SMART",
                currency=caps.currency, primary_exchange=caps.primary_exchange))
            if ((resolved.symbol, resolved.sec_type, resolved.currency)
                    != (caps.symbol, caps.sec_type, caps.currency) or resolved.con_id <= 0
                    or not resolved.min_tick or resolved.min_tick <= 0):
                raise ExperimentContractMismatch(f"resolved contract {resolved.symbol}/"
                                                 f"{resolved.sec_type} does not fit {caps.name}")
            with self._cond:
                self._contract = resolved
                _set_policy(self._client.conn, (self._cfg.account, resolved.con_id, caps))
            self._take_baseline()
        except BaseException as exc:
            try:
                self.disconnect()
            except Exception as cleanup:  # noqa: BLE001
                exc.add_note(f"additionally: {cleanup}")
            self._counter.close()
            raise

    def disconnect(self) -> None:
        try:
            super().disconnect()
        finally:
            self._counter.close()

    def _check_read_only(self) -> None:
        """ORDER MODE: the config read must show readOnlyApi present and False, socketPort present
        and equal to the configured port, allowLocalhostOnly present and True. Never a write."""
        if self._cfg.read_only_check != "config-read":
            raise ReadOnlyNotVerified(f"unknown read_only_check {self._cfg.read_only_check!r}")
        if self.server_version is None or self.server_version < MIN_SERVER_VER_CONFIG:
            raise ConfigReadUnsupported(f"config read needs server version >= "
                                        f"{MIN_SERVER_VER_CONFIG}")
        try:
            resp = self._request(
                "config", lambda rid: self._client.reqConfigProtoBuf(ConfigRequest(reqId=rid)),
                None, self._request_timeout, cancel_always=False, build=lambda p: p.rows[0],
                gate=True)
        except IbkrTimeout as exc:
            raise ConfigReadTimeout(f"no config response: {exc}") from exc
        except IbkrRequestError as exc:
            raise ConfigReadRejected(exc.code, exc.broker_message) from exc
        settings = resp.api.settings if resp.HasField("api") and resp.api.HasField("settings") \
            else None

        def get(name: str) -> object:
            return getattr(settings, name) if settings is not None and settings.HasField(name) \
                else None

        fields = {"readOnlyApi": get("readOnlyApi"), "socketPort": get("socketPort"),
                  "allowLocalhostOnly": get("allowLocalhostOnly")}
        self.read_only_settings = fields
        problems = []
        if fields["readOnlyApi"] is not False:
            problems.append(f"readOnlyApi={fields['readOnlyApi']} (order mode needs explicit "
                            "False)")
        if fields["socketPort"] != self._cfg.port:
            problems.append(f"socketPort={fields['socketPort']} (configured {self._cfg.port})")
        if fields["allowLocalhostOnly"] is not True:
            problems.append(f"allowLocalhostOnly={fields['allowLocalhostOnly']}")
        if problems:
            raise OrderModeSettingRefused("TWS API settings refused for order mode (None = "
                                          "absent): " + ", ".join(problems))
        self.read_only_status = "OFF_VERIFIED_FOR_ORDER_MODE"
        self.order_mode_status = "VERIFIED"

    # ------------------------------------------------------------------ order operations
    def place(self, specs: Sequence[OrderSpec]) -> list[OrderSnapshot]:
        """Place one order, or a bracket: ``specs[0]`` the BUY parent, the rest its protective
        children. Nothing is live until the last order is transmitted. Raises ``OrderRefused``
        (nothing sent), ``OrderOutcomeUnknown`` (no acknowledgement; never retried) or
        ``PartialBracket``. A rejection by the broker is returned as REJECTED snapshots."""
        specs = list(specs)
        if not specs:
            raise ValueError("nothing to place")
        with self._order_lock:
            self._require_order_mode()
            facts, position, working = self._fresh_state()
            ids = self._allocate_ids(len(specs))
            proposed = self._build(specs, ids)
            parent = proposed[0]
            for i, p in enumerate(proposed):  # the whole bracket first: nothing sent on a refusal
                decision = self._evaluate(p, facts, position, working, len(proposed) - 1 - i,
                                          parent=parent if i else None)
                if not decision:
                    raise OrderRefused(decision)
            sent: list[ProposedOrder] = []
            try:
                for i, p in enumerate(proposed):
                    kind = "SINGLE" if len(proposed) == 1 else ("PARENT" if i == 0 else "CHILD")
                    self._send_place(p, kind, lambda p=p, i=i: self._evaluate(
                        p, facts, position, working, len(proposed) - 1 - i,
                        parent=parent if i else None))
                    sent.append(p)
            except BaseException as exc:
                if not sent:
                    raise
                self._cancel_quietly(sent[0].order_id)
                raise PartialBracket(
                    f"bracket interrupted after {len(sent)} of {len(proposed)} orders: "
                    f"{type(exc).__name__}: {exc}; parent cancel requested",
                    orders=tuple(self._snap(self._tracks[p.order_id]) for p in sent)) from exc
            return self._await_bracket(ids)

    def modify(self, order_id: int, *, limit_price: Decimal | None = None,
               stop_price: Decimal | None = None, quantity: int | None = None) -> OrderSnapshot:
        """Re-place a working order under its own id (counts as one submission)."""
        with self._order_lock:
            self._require_order_mode()
            t = self._own_working(order_id)
            original = t.as_proposed()
            new = replace(
                original, transmit=True,
                limit_price=limit_price if limit_price is not None else original.limit_price,
                stop_price=stop_price if stop_price is not None else original.stop_price,
                quantity=Decimal(quantity) if quantity is not None else original.quantity)
            facts, position, working = self._fresh_state()
            t = self._own_working(order_id)

            def evaluate() -> object:
                return self._evaluate(new, facts, position, working, 0, original=original)

            decision = evaluate()
            if not decision:
                raise OrderRefused(decision)
            with self._cond:
                before_open, before_err = t.open_seq, t.error_seq
                t.modify_pending, t.modify_error = True, ""
            try:
                self._send_place(new, "MODIFY", evaluate)
                with self._cond:
                    self._wait(lambda: t.error_seq > before_err or (
                        t.open_seq > before_open and self._matches(t, new)), self._ack_timeout)
                    error, matched = t.modify_error, t.open_seq > before_open \
                        and self._matches(t, new)
            finally:
                with self._cond:
                    t.modify_pending = False
            if error and not matched:
                raise AmendRejected(f"amendment of order {order_id} rejected: {error}")
            if not matched:
                raise OrderOutcomeUnknown(f"amendment of order {order_id}: no acknowledgement in "
                                          f"{self._ack_timeout:.1f}s", orders=(self._snap(t),))
            return self._snap(t)

    def cancel(self, order_id: int) -> OrderSnapshot:
        """Cancel one order this client placed. Cancels never count against the submission cap
        and stay possible after the cap is reached (they only reduce risk)."""
        with self._order_lock:
            self._require_ready()
            with self._cond:
                t = self._tracks.get(order_id)
                if t is None or not t.own or (t.status in FINAL_STATUSES):
                    raise OrderNotWorking(f"order {order_id} is not a working order of this "
                                          "client")
                before = t.error_seq
                t.cancel_error = ""
            self._send_cancel(t)
            with self._cond:
                self._wait(lambda: t.status in FINAL_STATUSES or (
                    t.error_seq > before and bool(t.cancel_error)), self._cancel_wait)
                if t.cancel_error and t.status not in FINAL_STATUSES:
                    raise CancelFailed(f"cancel of order {order_id} refused: {t.cancel_error}")
                return self._snap(t)

    def dry_check_exit(self, order_id: int, *, limit_price: Decimal | None) -> None:
        """Before a protective SELL of this client is cancelled or shrunk for an exit: would a
        closing SELL LMT at ``limit_price`` for what is held pass the guard once this order is
        gone? ``limit_price`` is the binding's own estimate of the core's exit limit, priced the
        same way from a FRESH quote (``IbkrPaperBroker.exit_limit``), not the price the core
        computed earlier and not the stop price. Same inputs as at send time (fresh position, own
        fills, working orders without this one, the count), except that the liquid-hours rule is
        judged at now + ``EXIT_HOURS_MARGIN_SECONDS``. Raises ``OrderRefused`` if not, and when
        something is held but the limit is unknown (``None``). Sends nothing, counts nothing."""
        with self._order_lock:
            self._require_order_mode()
            with self._cond:
                t = self._tracks.get(order_id)
                if t is None or not t.own:
                    raise OrderNotWorking(f"order {order_id} is not an order of this client")
            facts, position, working = self._fresh_state()
            held = position[0]
            if held is not None and held <= 0 and position[1] == held:
                return  # nothing held: the cancel only reduces risk
            if limit_price is None:
                raise OrderRefused(Refusal(RefusalReason.PRICE_INVALID,
                                           "the exit limit to be sent is unknown"))
            price = limit_price
            qty = min(held if held is not None and held > 0 else Decimal(1),
                      Decimal(self._caps.max_order_quantity))
            c = self.contract
            with self._cond:
                probe_id = max(self._next_local_id, self._counter.max_order_id + 1,
                               max(self._tracks, default=0) + 1, 1)
            probe = ProposedOrder(
                order_id=probe_id, order_ref="EXIT-DRY-CHECK", account=self._cfg.account,
                con_id=c.con_id, symbol=c.symbol, sec_type=c.sec_type, currency=c.currency,
                action="SELL", quantity=qty, order_type="LMT", limit_price=price,
                stop_price=None, tif="DAY", outside_rth=False)
            rest = [w for w in working if w.order_id != order_id]
            # the hours rule at now + margin: the close must still be inside liquid hours
            # when it is sent, after the cancel and the quotes in between
            decision = self._evaluate(probe, facts, position, rest, 0,
                                      ahead_seconds=EXIT_HOURS_MARGIN_SECONDS)
            if not decision:
                raise OrderRefused(decision)

    # ------------------------------------------------------------------ reads
    def open_orders(self, *, timeout: float | None = None) -> list[OrderSnapshot]:
        """``reqAllOpenOrders`` (every client and TWS) until ``openOrderEnd``. Raises on failure.
        Orders of this client that the snapshot does not show but whose latest callback says
        working are added (open orders can lag a fresh submission). Final orders are left out;
        INACTIVE orders are kept (not final at this broker)."""
        return [s for s in self._open_snapshot(timeout) if s.status not in FINAL_STATUSES]

    def _open_snapshot(self, timeout: float | None = None) -> list[OrderSnapshot]:
        """Every row of the broker's open-orders answer, whatever its status, plus this client's
        orders that are not final but not (yet) in the answer."""
        rows = self._request(
            "open_orders", lambda rid: self._client.reqAllOpenOrdersProtoBuf(
                AllOpenOrdersRequest()), None, timeout or self._request_timeout,
            cancel_always=False, build=lambda p: list(p.rows))
        with self._cond:
            out: dict[tuple, OrderSnapshot] = {}
            for kind, key in rows:
                t = self._tracks.get(key) if kind == "own" else key
                if isinstance(t, _Track):
                    out[(True, t.order_id) if t.own else (False, id(t))] = self._snap(t)
            seen_own = {s.order_id for s in out.values() if s.own}
            for t in self._tracks.values():
                if t.own and t.working and t.order_id not in seen_own and t.order_type:
                    out[(True, t.order_id)] = self._snap(
                        t, reason="LOCAL_STATE_NOT_IN_SNAPSHOT")
            return list(out.values())

    def executions(self, *, timeout: float | None = None) -> list[ExecRecord]:
        """``reqExecutions`` filtered to the account. TWS returns the current day's executions
        only (ibapi docstring: 'beyond the past 24 hours' needs the TWS Trade Log). Executions
        that arrived live during this session are included; duplicates (same execId) are
        reported once."""
        flt = ExecutionFilter()
        flt.acctCode = self._cfg.account
        ids = self._request(
            "executions", lambda rid: self._client.reqExecutionsProtoBuf(
                createExecutionRequestProto(rid, flt)), None, timeout or self._request_timeout,
            cancel_always=False, build=lambda p: list(p.rows))
        with self._cond:
            wanted = dict.fromkeys(ids)
            wanted.update(dict.fromkeys(self._execs))
            return [self._execs[i] for i in wanted if i in self._execs]

    def completed_orders(self, *, timeout: float | None = None) -> list[OrderSnapshot]:
        """``reqCompletedOrders(apiOnly=True)`` until ``completedOrdersEnd``."""
        rows = self._request(
            "completed", lambda rid: self._client.reqCompletedOrdersProtoBuf(
                CompletedOrdersRequest(apiOnly=True)), None, timeout or self._request_timeout,
            cancel_always=False, build=lambda p: list(p.rows))
        return rows

    # ------------------------------------------------------------------ internals: placing
    def _require_order_mode(self) -> None:
        self._require_ready()
        if self._contract is None or self.order_mode_status != "VERIFIED":
            raise SessionStateError("order mode is not verified")
        if self._counter.used is None:
            raise OrderRefused(Refusal(RefusalReason.SUBMISSION_COUNT_UNKNOWN,
                                       self.count_problem or "submission counter is not open"))

    def _take_baseline(self) -> None:
        """At connect: today's executions (``reqExecutions``), then the position read, compared
        (``position_check``). The guard repeats the comparison before every submission; the first
        submission of a session re-reads the executions (``_count_at_broker``)."""
        self.executions()
        rows = self.positions()
        with self._cond:
            self._baseline_position = sum((r.quantity for r in rows
                                           if r.con_id == self.contract.con_id), Decimal(0))
        implied = self._implied_position(self.contract.con_id)
        self.position_check = {"read": str(self._baseline_position), "implied": str(implied),
                               "agree": implied == self._baseline_position}

    def _implied_position(self, con_id: int) -> Decimal | None:
        """The experiment's own position, derived from the broker's executions (today's, from
        ``reqExecutions``, plus every one received live) that belong to the experiment: an
        ``orderRef`` with the experiment prefix, or this client's order id recorded in the
        journal or placed here. For an order this session placed, the larger of its execution
        total and its ``orderStatus`` filled quantity (either can arrive first). It does not use
        the position read, so a read that lags across a restart is caught. It assumes the account
        holds none of the contract outside the experiment (otherwise: a disagreement, refused).
        None when unknown."""
        with self._cond:
            if self._baseline_position is None:
                return None
            here = {t.order_id: t for t in self._tracks.values()
                    if t.own and t.placed_here and t.con_id == con_id}
            journal_ids = {e["order_id"] for e in self._counter.entries}
            sign = {"BUY": Decimal(1), "SELL": Decimal(-1)}
            total = Decimal(0)
            by_order: dict[int, Decimal] = {}
            for rec in self._execs.values():
                own = rec.client_id == self._cfg.client_id
                if rec.con_id != con_id or not (rec.experiment or (own and (
                        rec.order_id in journal_ids or rec.order_id in here))):
                    continue
                if rec.side not in sign:
                    return None
                if own and rec.order_id in here:
                    by_order[rec.order_id] = by_order.get(rec.order_id, Decimal(0)) + rec.shares
                else:
                    total += sign[rec.side] * rec.shares
            for oid, t in here.items():
                if t.action not in sign:
                    return None
                total += sign[t.action] * max(t.filled, by_order.get(oid, Decimal(0)))
            return total

    def _fresh_state(self) -> tuple[ContractFacts, tuple[Decimal | None, Decimal | None],
                                    list[WorkingOrder]]:
        c = self.contract
        facts = ContractFacts(con_id=c.con_id, symbol=c.symbol, sec_type=c.sec_type,
                              currency=c.currency, min_tick=_dec(c.min_tick),
                              time_zone_id=c.time_zone_id, liquid_hours=c.liquid_hours)
        rows = self.positions()
        position = sum((r.quantity for r in rows if r.con_id == c.con_id), Decimal(0))
        snaps = self._open_snapshot()  # every row, whatever its status (an Inactive BUY counts)
        with self._cond:  # own orders sent and not final count even if no view shows them yet
            seen = {s.order_id for s in snaps if s.own}
            snaps += [self._snap(t, reason="SENT_NOT_VISIBLE") for t in self._tracks.values()
                      if t.own and t.proposed is not None and t.order_id not in seen
                      and not t.rejected and t.status not in FINAL_STATUSES]
        working_ids = {(s.client_id, s.order_id) for s in snaps}
        # another client's order ids are not ours: give them ids that cannot collide (< 0)
        working = [WorkingOrder(order_id=s.order_id if s.own else -(n + 1),
                                con_id=s.con_id, action=s.action, remaining=s.remaining,
                                parent_id=s.parent_id if s.own else 0,
                                parent_working=(s.client_id, s.parent_id) in working_ids)
                   for n, s in enumerate(snaps)]
        journal = self._journal_working({s.order_id for s in snaps if s.own})
        ids = {w.order_id for w in working} | {w.order_id for w in journal}
        working += [replace(w, parent_working=w.parent_id in ids) for w in journal]
        if self._broker_count is None:
            self._broker_count = self._count_at_broker()
        return facts, (position, self._implied_position(c.con_id)), working

    def _count_at_broker(self) -> int:
        """Distinct orders of this experiment (orderRef prefix) that the broker itself shows in
        open orders (every client), executions and completed orders. Raises if a read fails."""
        keys: set[tuple] = set()

        def key(perm: int, client: int, oid: int) -> tuple:
            return ("perm", perm) if perm else ("id", client, oid)

        for row in [*self._open_snapshot(), *self.completed_orders()]:
            if row.experiment:
                keys.add(key(row.perm_id, row.client_id, row.order_id))
        for e in self.executions():
            if e.experiment:
                keys.add(key(e.perm_id, e.client_id, e.order_id))
        return len(keys)

    def _journal_working(self, seen_own: set[int]) -> list[WorkingOrder]:
        """Submissions recorded by EARLIER sessions with no terminal evidence yet: counted as
        working at their full size. Evidence: a final status for the order, a completed-orders
        row (by order id or reference), executions covering its quantity, or absence from every
        lookup once the submission is older than ``JOURNAL_SETTLE_SECONDS``."""
        latest: dict[int, dict] = {}
        for e in self._counter.entries:
            latest[e["order_id"]] = e  # an amendment's line carries the current size
        pending = []
        with self._cond:
            for oid, e in latest.items():
                t = self._tracks.get(oid)
                if oid in self._journal_resolved or oid in seen_own:
                    continue
                if t is not None and t.own and t.proposed is not None:
                    continue  # placed or amended in this session: tracked as SENT_NOT_VISIBLE
                if t is not None and t.own and t.status in FINAL_STATUSES:
                    self._journal_resolved[oid] = t.status.value
                    continue
                pending.append(e)
        if not pending:
            return []
        done = self.completed_orders()
        execs = self.executions()
        out = []
        now = self._now()
        for e in pending:
            oid = e["order_id"]
            ref = _split_ref(e["order_ref"], self._caps.ref_prefix)[0]
            if any(d.status in FINAL_STATUSES and d.experiment and (
                    (d.own and d.order_id == oid) or d.order_ref == ref) for d in done):
                self._journal_resolved[oid] = "COMPLETED"
                continue
            filled = sum((x.shares for x in execs if x.client_id == self._cfg.client_id
                          and x.order_id == oid), Decimal(0))
            qty = Decimal(e["quantity"])
            if filled >= qty:
                self._journal_resolved[oid] = "FILLED"
                continue
            try:
                age = (now - datetime.fromisoformat(e["at"])).total_seconds()
            except ValueError:
                age = 0.0
            if age > JOURNAL_SETTLE_SECONDS:
                self._journal_resolved[oid] = "ABSENT_FROM_EVERY_LOOKUP"
                continue
            out.append(WorkingOrder(order_id=oid, con_id=self.contract.con_id,
                                    action=e["action"], remaining=qty - filled,
                                    parent_id=e["parent_id"]))
        return out

    def _allocate_ids(self, n: int) -> list[int]:
        """``reqIds`` then ``nextValidId``; ids are never reused (above every id this session or
        the submission journal has seen)."""
        with self._cond:
            self._require_ready()
            seq = self._nvid_seq
            self._client.reqIdsProtoBuf(IdsRequest(numIds=1))
            if not self._cond.wait_for(lambda: self._nvid_seq > seq or self._poisoned is not None
                                       or self._connection_lost is not None,
                                       self._request_timeout):
                raise IbkrTimeout("no nextValidId after reqIds")
            self._require_ready()
            base = max(self._next_valid_id or 0, self._counter.max_order_id + 1,
                       max(self._tracks, default=0) + 1, self._next_local_id, 1)
            if base + n >= REQ_ID_BASE:
                raise SessionStateError("order ids would reach the request-id range")
            self._next_local_id = base + n
            return list(range(base, base + n))

    def _build(self, specs: list[OrderSpec], ids: list[int]) -> list[ProposedOrder]:
        c = self.contract
        oca = f"{specs[0].order_ref}.OCA" if len(specs) > 2 else ""
        out = []
        for i, (s, oid) in enumerate(zip(specs, ids, strict=True)):
            out.append(ProposedOrder(
                order_id=oid, order_ref=s.order_ref, account=self._cfg.account, con_id=c.con_id,
                symbol=c.symbol, sec_type=c.sec_type, currency=c.currency, action=s.action,
                quantity=Decimal(s.quantity), order_type=s.order_type,
                limit_price=s.limit_price, stop_price=s.stop_price, tif=s.tif, outside_rth=False,
                parent_id=ids[0] if i else 0, transmit=(i == len(specs) - 1),
                oca_group=oca if i else ""))
        return out

    def _evaluate(self, p: ProposedOrder, facts: ContractFacts,
                  position: tuple[Decimal | None, Decimal | None], working: list[WorkingOrder], still: int, *, parent: ProposedOrder | None = None,
                  original: ProposedOrder | None = None, ahead_seconds: int = 0) -> object:
        return order_guard(p, GuardContext(
            caps=self._caps, submissions_used=self._counter.used,
            broker_submissions=self._broker_count, contract=facts,
            expected_account=self._cfg.account, position=position[0], working=tuple(working),
            now=self._now() + timedelta(seconds=ahead_seconds), implied_position=position[1],
            still_to_send=still, parent=parent, original=original))

    def _proto(self, p: ProposedOrder) -> PlaceOrderRequest:
        c = Contract()
        c.conId, c.symbol, c.secType, c.currency = p.con_id, p.symbol, p.sec_type, p.currency
        c.exchange = "SMART"
        c.primaryExchange = self.contract.primary_exchange
        o = Order()
        o.action, o.totalQuantity, o.orderType, o.tif = p.action, p.quantity, p.order_type, p.tif
        o.account, o.orderRef, o.parentId = p.account, self._caps.ref_prefix + p.order_ref, \
            p.parent_id
        if p.limit_price is not None:
            o.lmtPrice = float(p.limit_price)
        if p.stop_price is not None:
            o.auxPrice = float(p.stop_price)
        if p.oca_group:
            o.ocaGroup, o.ocaType = p.oca_group, 1
        req = createPlaceOrderRequestProto(p.order_id, c, o)
        # The SDK omits a False bool (``if order.transmit: ...``); TWS's default for an absent
        # field is not documented, so presence is made explicit for both flags.
        req.order.transmit = bool(p.transmit)
        req.order.outsideRth = bool(p.outside_rth)
        return req

    def _send_place(self, p: ProposedOrder, kind: str, evaluate: Callable[[], object]) -> None:
        """The ONLY path to a PLACE_ORDER frame: under the lock, the guard (immediately before the
        send, after every other check), then the durable count (fsync'd), then a one-shot permit
        for exactly these bytes, then the SDK send."""
        with self._cond:
            self._require_ready()
            decision = evaluate()
            if not decision:
                raise OrderRefused(decision)
            req = self._proto(p)
            body = req.SerializeToString()
            self._counter.record(  # fsync'd before the wire
                order_id=p.order_id, order_ref=req.order.orderRef, kind=kind, action=p.action,
                quantity=p.quantity, order_type=p.order_type, limit_price=p.limit_price,
                stop_price=p.stop_price, parent_id=p.parent_id)
            t = self._tracks.get(p.order_id)
            if t is None:
                t = self._tracks[p.order_id] = _Track(order_id=p.order_id, own=True,
                                                      client_id=self._cfg.client_id)
            self._apply_proposed(t, p)
            if kind != "MODIFY":
                t.placed_here = True
            _issue_permit(self._client.conn, OUT.PLACE_ORDER, body)
            self._event("SENT_PLACE", p.order_id, kind=kind, transmit=bool(p.transmit))
            self._client.placeOrderProtoBuf(req)
            self.order_frames_sent += 1

    @staticmethod
    def _apply_proposed(t: _Track, p: ProposedOrder) -> None:
        t.proposed = p
        t.order_ref, t.parent_id, t.con_id = p.order_ref, p.parent_id, p.con_id
        t.experiment = True
        t.symbol, t.sec_type, t.currency, t.account = p.symbol, p.sec_type, p.currency, p.account
        t.action, t.order_type, t.tif, t.oca_group = p.action, p.order_type, p.tif, p.oca_group
        if t.status is None:  # a new order: the original fields until the broker echoes them
            t.total, t.limit_price, t.stop_price = p.quantity, p.limit_price, p.stop_price

    def _send_cancel(self, t: _Track) -> None:
        with self._cond:
            self._require_ready()
            req: CancelOrderRequest = createCancelOrderRequestProto(t.order_id, OrderCancel())
            _issue_permit(self._client.conn, OUT.CANCEL_ORDER, req.SerializeToString())
            if t.status is not None:
                t.status = merge_status(t.status, OrderStatus.PENDING_CANCEL, filled=t.filled,
                                        total=t.total)
            self._event("SENT_CANCEL", t.order_id)
            self._client.cancelOrderProtoBuf(req)
            self.order_frames_sent += 1

    def _cancel_quietly(self, order_id: int) -> None:
        try:
            t = self._tracks.get(order_id)
            if t is not None and t.status not in FINAL_STATUSES:
                self._send_cancel(t)
        except Exception as exc:  # noqa: BLE001 - recorded; the caller reports an unknown outcome
            self.internal_errors.append(redact_accounts(f"parent cancel failed: {exc}"))

    def _own_working(self, order_id: int) -> _Track:
        with self._cond:
            t = self._tracks.get(order_id)
            if (t is None or not t.own or t.status not in WORKING_STATUSES
                    or t.status == OrderStatus.PENDING_CANCEL or not t.order_type):
                raise OrderNotWorking(f"order {order_id} is not a working order of this client")
            return t

    @staticmethod
    def _matches(t: _Track, p: ProposedOrder) -> bool:
        return (t.total == p.quantity and t.limit_price == p.limit_price
                and t.stop_price == p.stop_price)

    def _wait(self, predicate: Callable[[], bool], timeout: float) -> bool:
        """Caller holds the lock."""
        return self._cond.wait_for(lambda: predicate() or self._poisoned is not None
                                   or self._connection_lost is not None, timeout)

    def _await_bracket(self, ids: list[int]) -> list[OrderSnapshot]:
        tracks = [self._tracks[i] for i in ids]

        def answered(t: _Track) -> bool:
            return t.status is not None or bool(t.rejected) or t.send_failed

        with self._cond:
            self._wait(lambda: all(answered(t) for t in tracks), self._ack_timeout)
            self._require_ready()
            snaps = tuple(self._snap(t) for t in tracks)
            parent, children = tracks[0], tracks[1:]
            if parent.rejected and parent.status in (None, OrderStatus.REJECTED):
                return list(snaps)  # definite: the broker rejected the entry
            acked = [t.status is not None and not t.send_failed for t in tracks]
            if all(acked) and not any(c.rejected for c in children):
                return list(snaps)
            bad_child = any(c.rejected or not a for c, a in zip(children, acked[1:], strict=True))
        if children and (acked[0] or any(c.rejected for c in children)) and bad_child:
            self._cancel_quietly(parent.order_id)
            raise PartialBracket(
                "partial bracket: parent "
                f"{'acknowledged' if acked[0] else 'unacknowledged'}, children "
                + ", ".join(f"{c.order_id}={'rejected ' + c.rejected if c.rejected else 'no ack'}"
                            for c in children if c.rejected or not c.status)
                + "; parent cancel requested", orders=snaps)
        raise OrderOutcomeUnknown(
            f"no acknowledgement for orders {[t.order_id for t in tracks if t.status is None]} in "
            f"{self._ack_timeout:.1f}s" + (" (the SDK reported a send failure)"
                                           if any(t.send_failed for t in tracks) else ""),
            orders=snaps)

    # ------------------------------------------------------------------ snapshots
    def _snap(self, t: _Track, reason: str = "") -> OrderSnapshot:
        status = t.status
        if status is None:
            status = (OrderStatus.REJECTED if t.rejected else
                      OrderStatus.UNKNOWN if t.send_failed else OrderStatus.PENDING_SUBMIT)
        remaining = t.remaining if t.remaining is not None else max(t.total - t.filled,
                                                                    Decimal(0))
        parent = self._tracks.get(t.parent_id) if t.parent_id else None
        return OrderSnapshot(
            order_id=t.order_id, client_id=t.client_id, perm_id=t.perm_id,
            order_ref=t.order_ref, experiment=t.experiment, parent_id=t.parent_id,
            parent_ref=parent.order_ref if parent is not None and t.own else None,
            con_id=t.con_id, symbol=t.symbol, sec_type=t.sec_type, currency=t.currency,
            action=t.action, total_quantity=t.total, order_type=t.order_type,
            limit_price=t.limit_price, stop_price=t.stop_price, tif=t.tif, status=status,
            raw_status=t.raw_status, filled=t.filled, remaining=remaining,
            avg_fill_price=t.avg_fill, reason=reason or t.rejected or t.modify_error,
            updated_at=t.updated_at, own=t.own)

    # ------------------------------------------------------------------ callbacks (SDK threads)
    def _on_next_valid_id_value(self, order_id) -> None:
        with self._cond:
            try:
                value = int(order_id)
            except (TypeError, ValueError):
                value = 0
            if value > 0:
                self._next_valid_id = max(self._next_valid_id or 0, value)
            self._nvid_seq += 1
            self._cond.notify_all()
        self._on_next_valid_id()

    def _account_ok(self, account: str, where: str) -> bool:
        try:
            check_row_account(account, self._cfg.account, where)
            return True
        except AccountIdentityError as exc:
            self._poison(exc)
            return False

    def _on_open_order(self, order_id, contract, order, state) -> None:
        if not self._account_ok(order.account, "openOrder"):
            return
        with self._cond:
            own = (order.clientId == self._cfg.client_id and int(order_id) > 0)
            if own:
                self._event("openOrder", int(order_id), status=state.status)
                t = self._tracks.get(int(order_id))
                if t is None:
                    t = self._tracks[int(order_id)] = _Track(order_id=int(order_id), own=True)
                cached = self._status_cache.pop(int(order_id), None)
                t.fill_from_order(contract, order, state.status, self._caps.ref_prefix)
                if cached is not None:
                    self._apply_status(t, *cached)
                t.open_seq += 1
                t.event_seq += 1
                row: tuple = ("own", t.order_id)
            else:
                t = _Track(order_id=int(order_id), own=False)
                t.fill_from_order(contract, order, state.status, self._caps.ref_prefix)
                t.remaining = t.total  # nothing known filled: counted at full size (conservative)
                row = ("foreign", t)
            for p in self._pending.values():
                if p.kind == "open_orders" and not p.done:
                    p.rows.append(row)
            self._cond.notify_all()

    def _apply_status(self, t: _Track, status, filled, remaining, avg, perm_id) -> None:
        f, r = _dec(filled), _dec(remaining)
        if f is not None:
            t.filled = max(t.filled, f)
        if r is not None and (t.remaining is None or f is None or f >= t.filled):
            t.remaining = r
        if avg is not None and avg != UNSET_DOUBLE and avg:
            t.avg_fill = _dec(avg)
        if perm_id and perm_id != UNSET_INTEGER:
            t.perm_id = int(perm_id)
        if t.total <= 0 and f is not None and r is not None:
            t.total = f + r
        t.raw_status = status or t.raw_status
        t.status = merge_status(t.status, map_status(status, t.filled, t.remaining),
                                filled=t.filled, total=t.total)
        t.updated_at = datetime.now(UTC)
        t.event_seq += 1

    def _on_order_status(self, order_id, status, filled, remaining, avg, perm_id, parent_id,
                         client_id) -> None:
        """No account field in this callback; only this client's order ids are applied."""
        with self._cond:
            if client_id not in (self._cfg.client_id, UNSET_INTEGER):
                return
            self._event("orderStatus", int(order_id), status=status, filled=str(filled),
                        remaining=str(remaining))
            t = self._tracks.get(int(order_id))
            if t is None:
                self._status_cache[int(order_id)] = (status, filled, remaining, avg, perm_id)
                return
            self._apply_status(t, status, filled, remaining, avg, perm_id)
            self._cond.notify_all()

    def _on_exec_details(self, req_id, contract, execution) -> None:
        if not self._account_ok(execution.acctNumber, "execDetails"):
            return
        side = {"BOT": "BUY", "SLD": "SELL"}.get(execution.side, execution.side)
        rec = ExecRecord(
            exec_id=execution.execId, order_id=int(execution.orderId or 0),
            client_id=int(execution.clientId or 0), perm_id=int(execution.permId or 0),
            order_ref=_split_ref(execution.orderRef or "", self._caps.ref_prefix)[0],
            experiment=_split_ref(execution.orderRef or "", self._caps.ref_prefix)[1],
            con_id=int(contract.conId or 0),
            symbol=contract.symbol, sec_type=contract.secType, currency=contract.currency,
            side=side, shares=_dec(execution.shares) or Decimal(0),
            price=_dec(execution.price) or Decimal(0), time=execution.time,
            received_at=datetime.now(UTC))
        with self._cond:
            if rec.client_id == self._cfg.client_id and rec.exec_id not in self._execs:
                self._event("execDetails", rec.order_id, shares=str(rec.shares))
            if rec.exec_id and rec.exec_id not in self._execs:
                self._execs[rec.exec_id] = rec  # first report wins; repeats are ignored
            p = self._pending.get(req_id)
            if p is not None and p.kind == "executions" and rec.exec_id:
                p.rows.append(rec.exec_id)

    def _on_commission(self, report) -> None:
        with self._cond:
            self.commissions[report.execId] = (float(report.commissionAndFees), report.currency)

    def _on_completed_order(self, contract, order, state) -> None:
        if not self._account_ok(order.account, "completedOrder"):
            return
        with self._cond:
            t = _Track(order_id=int(order.orderId or 0) if order.orderId != UNSET_INTEGER else 0,
                       own=order.clientId == self._cfg.client_id)
            t.fill_from_order(contract, order, None, self._caps.ref_prefix)
            known = self._tracks.get(t.order_id) if t.own else None
            status = map_status(state.status)
            if known is not None and known.rejected and status in (
                    OrderStatus.CANCELLED, OrderStatus.INACTIVE, OrderStatus.UNKNOWN):
                status = OrderStatus.REJECTED
            t.status, t.raw_status = status, state.status
            t.filled = (t.total if status == OrderStatus.FILLED
                        else known.filled if known is not None else Decimal(0))
            t.remaining = Decimal(0)
            t.updated_at = parse_broker_time(state.completedTime) or datetime.now(UTC)
            if known is not None and known.status not in FINAL_STATUSES and \
                    status in FINAL_STATUSES:
                known.status = merge_status(known.status, status, filled=known.filled,
                                            total=known.total)
            reason = state.completedStatus or (known.rejected if known is not None else "")
            snap = replace(self._snap(t), reason=reason,
                           parent_ref=(self._tracks[t.parent_id].order_ref
                                       if t.own and t.parent_id in self._tracks else None))
            for p in self._pending.values():
                if p.kind == "completed" and not p.done:
                    p.rows.append(snap)

    def _on_kind_end(self, kind: str) -> None:
        with self._cond:
            for p in self._pending.values():
                if p.kind == kind and not p.done:
                    p.ended = p.done = True
            self._cond.notify_all()

    def _on_error(self, req_id: int, code: int, message: str) -> None:
        if code not in CONNECTION_CODES:
            with self._cond:
                t = self._tracks.get(req_id)
                if t is not None:
                    self._on_order_error(t, code, redact_accounts(message))
                    self._cond.notify_all()
                    return
        super()._on_error(req_id, code, message)

    def _on_order_error(self, t: _Track, code: int, msg: str) -> None:
        cls = classify_order_error(code)
        self._event("error", t.order_id, code=code, cls=cls)
        t.errors.append((code, msg, cls))
        t.event_seq += 1
        t.error_seq += 1
        text = f"{code} {msg}"
        if cls == "NOTICE":
            return
        if cls == "CANCELLED":
            t.status = merge_status(t.status, OrderStatus.CANCELLED, filled=t.filled,
                                    total=t.total)
        elif cls == "CANCEL_FAILED":
            t.cancel_error = text
        elif cls == "SEND_FAILED":
            t.send_failed = True
        elif t.modify_pending:
            t.modify_error = text  # the original order stands
        elif t.status in (None, OrderStatus.PENDING_SUBMIT, OrderStatus.INACTIVE):
            t.rejected = text  # an explicit rejection makes an Inactive order final
            t.status = OrderStatus.REJECTED
        else:
            t.rejected = t.rejected or text  # an error on a live order: recorded, not guessed
        t.updated_at = datetime.now(UTC)


__all__ = ["ORDER_MODE_CLIENT_METHODS", "ORDER_MODE_OUT_MESSAGES", "PaperOrderSession",
           "Refusal", "RefusalReason", "SessionState"]
