"""``IbkrSession``: a small synchronous, read-only facade over the official ``ibapi`` EClient/EWrapper.

Threads: ``EClient.connect`` (blocking handshake) runs in a bounded helper thread; after it returns the
SDK's own ``EReader`` thread reads the socket and ``EClient.run()`` decodes messages in a daemon
thread, calling our ``EWrapper`` subclass. Callers block on a condition variable with explicit
timeouts. The session is single-use: to reconnect, create a new ``IbkrSession``.

Deny by default: the session talks to TWS only through ``_ReadOnlyClient``, which raises
``ReadOnlyViolation`` from every public EClient method not in ``ALLOWED_CLIENT_METHODS`` and refuses
any outgoing message id not in ``ALLOWED_OUT_MESSAGES``. An order module, when it exists, must be a
separate, separately reviewed class; it must not be added to this file.

Connect order: local preflight (paper profile + listener owner, no socket) -> handshake -> server
version >= MIN_SERVER_VERSION -> identity gate -> read-only check -> READY.
"""
from __future__ import annotations

import itertools
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Self, TypeVar

import ibapi
from ibapi.client import EClient
from ibapi.common import PROTOBUF_MSG_ID
from ibapi.const import UNSET_DECIMAL, UNSET_DOUBLE
from ibapi.contract import Contract
from ibapi.message import OUT
from ibapi.protobuf.ConfigRequest_pb2 import ConfigRequest
from ibapi.ticktype import TickTypeEnum
from ibapi.wrapper import EWrapper

from tradingbot.ibkr.config import PaperConnectionConfig
from tradingbot.ibkr.environment import run_preflight
from tradingbot.ibkr.errors import (
    CONNECTION_LOSS_CODES,
    AccountIdentityError,
    AmbiguousContract,
    CleanupError,
    ConfigReadRejected,
    ConfigReadTimeout,
    ConfigReadUncorrelated,
    ConfigReadUnsupported,
    ConfigResponseChanged,
    ContractNotFound,
    ErrorClass,
    IbkrConnectionError,
    IbkrError,
    IbkrRequestError,
    IbkrTimeout,
    QuoteStreamClosed,
    QuoteStreamConflict,
    QuoteStreamLimit,
    ReadOnlyNotVerified,
    ReadOnlySettingRefused,
    ReadOnlyViolation,
    SessionStateError,
    UnsupportedServerVersion,
    classify_error,
)
from tradingbot.ibkr.identity import check_managed_accounts, check_row_account
from tradingbot.ibkr.models import (
    CLOSE_TICKS,
    DELAYED_TICK_IDS,
    DISCOVERY_ERROR,
    DISCOVERY_FOUND,
    DISCOVERY_NOT_FOUND,
    LAST_TIMESTAMP_TICKS,
    MARKET_DATA_TYPE_NAMES,
    NO_SECURITY_DEFINITION,
    PRICE_TICKS,
    SIZE_TICKS,
    AccountSummary,
    AccountValue,
    ContractQuery,
    DiscoveredContract,
    DiscoveryResult,
    OptionChainParams,
    OptionChainResult,
    PositionRow,
    QuoteReport,
    QuoteSnapshot,
    ResolvedContract,
    SideEvidence,
    assess_quote_attempts,
    classify_quote,
    quote_age_seconds,
)
from tradingbot.ibkr.redact import mask_account, redact_accounts

log = logging.getLogger(__name__)
T = TypeVar("T")


def _silence_sdk_logging() -> None:
    """The SDK logs raw messages (incl. account IDs), and on a field-count mismatch logs the raw
    fields at ERROR. No ibapi record may reach any handler or Python's lastResort (stderr)."""
    sdk = logging.getLogger("ibapi")
    sdk.setLevel(logging.CRITICAL + 10)
    sdk.propagate = False
    if not any(isinstance(h, logging.NullHandler) for h in sdk.handlers):
        sdk.addHandler(logging.NullHandler())


_silence_sdk_logging()

ACCOUNT_SUMMARY_TAGS = ("AccountType", "NetLiquidation", "TotalCashValue", "BuyingPower",
                        "AvailableFunds")
MIN_SERVER_VER_CANCEL_CONTRACT_DATA = 215  # ibapi.server_versions, 1052.1
MIN_SERVER_VER_CONFIG = 219  # ibapi.server_versions, 1052.1: reqConfigProtoBuf
# Lowest TWS server version this adapter accepts: the lowest one the test suite exercises.
MIN_SERVER_VERSION = 219
SDK_VERSION = ibapi.__version__

# Every public EClient callable the session (or the SDK internally, on those paths) may use.
ALLOWED_CLIENT_METHODS = frozenset({
    # lifecycle / internals used by connect(), run(), disconnect() and the send path
    "connect", "disconnect", "isConnected", "run", "reset", "setConnState", "checkConnected",
    "validateInvalidSymbols", "useProtoBuf", "serverVersion", "twsConnectionTime", "logRequest",
    "msgLoopTmo", "msgLoopRec", "keyboardInterrupt", "keyboardInterruptHard", "sendMsg",
    "sendMsgProtoBuf", "startApi", "startApiProtoBuf",
    # the read requests and their cancels
    "reqContractDetails", "reqContractDataProtoBuf", "cancelContractData",
    "cancelContractDataProtoBuf", "reqAccountSummary", "reqAccountSummaryProtoBuf",
    "cancelAccountSummary", "cancelAccountSummaryProtoBuf", "reqPositions", "reqPositionsProtoBuf",
    "cancelPositions", "cancelPositionsProtoBuf", "reqMktData", "reqMarketDataProtoBuf",
    "cancelMktData", "cancelMarketDataProtoBuf", "reqMarketDataType", "reqMarketDataTypeProtoBuf",
    # read-only API configuration read (read_only_check = "config-read")
    "reqConfigProtoBuf",
    # option-chain parameters (expirations, strikes, exchanges, trading classes, multiplier)
    "reqSecDefOptParams", "reqSecDefOptParamsProtoBuf",
})
ALLOWED_OUT_MESSAGES = frozenset({
    OUT.START_API, OUT.REQ_CONTRACT_DATA, OUT.CANCEL_CONTRACT_DATA, OUT.REQ_ACCOUNT_SUMMARY,
    OUT.CANCEL_ACCOUNT_SUMMARY, OUT.REQ_POSITIONS, OUT.CANCEL_POSITIONS, OUT.REQ_MKT_DATA,
    OUT.CANCEL_MKT_DATA, OUT.REQ_MARKET_DATA_TYPE, OUT.REQ_CONFIG, OUT.REQ_SEC_DEF_OPT_PARAMS,
})
MAX_DISCOVERY_LIMIT = 1000  # upper bound on rows kept per discovery / option-chain request
MAX_QUOTE_STREAMS = 10  # hard cap on simultaneously held-open quote streams per session
HALTED_TICK = 49  # TickTypeEnum.HALTED (tickGeneric): 0 not halted, 1 / 2 halted


def _denied(name: str) -> Callable[..., None]:
    def method(self, *args: Any, **kwargs: Any) -> None:
        raise ReadOnlyViolation(f"EClient.{name} is not allowed by the read-only adapter")

    method.__name__ = name
    return method


def _outgoing_msg_id(frame: bytes, server_version: int | None) -> int | None:
    """Message id of a framed request (4-byte length prefix, then the body)."""
    body = frame[4:]
    if server_version is not None and server_version >= 201:  # raw 4-byte big-endian id
        if len(body) < 4:
            return None
        mid = int.from_bytes(body[:4], "big")
        return mid - PROTOBUF_MSG_ID if mid > PROTOBUF_MSG_ID else mid
    try:  # legacy text framing: "<id>\0..."
        return int(body.split(b"\0", 1)[0])
    except ValueError:
        return None


# The SDK ``Connection`` (and through it the socket) behind each ``_GuardedConnection``, keyed by an
# opaque int token. It is held ONLY here: never as an attribute, closure cell, default or weakref
# callback of anything the session reaches (the guard is not weak-referenceable), so no attribute
# or weak-reference path from a session leads to an unfiltered send. Reaching this dict needs
# module globals: out of scope for an in-process filter (docs/IBKR_ADAPTER.md, section 4).
_RAW_CONNECTIONS: dict[int, Any] = {}
_TOKENS = itertools.count(1)
_RAW_METHODS = frozenset({"connect", "disconnect", "isConnected", "recvMsg", "sendMsg"})


def _raw_call(guard: _GuardedConnection, name: str, *args: Any) -> Any:
    """Call ``name`` on the raw connection. The raw object is never bound to a local name, and an
    exception from it leaves with its traceback, context and cause cut, so no frame's locals in a
    handed-out exception lead back to it."""
    if name not in _RAW_METHODS:
        raise ReadOnlyViolation(f"connection method {name!r} is not exposed")
    err: BaseException | None = None
    try:
        return getattr(_RAW_CONNECTIONS[guard._token], name)(*args)
    except BaseException as exc:  # noqa: BLE001 - re-raised below, stripped
        err = exc
    err.__traceback__ = None
    err.__context__ = err.__cause__ = None
    raise err


class _GuardedConnection:
    """Stands in for the SDK ``Connection``. Every SDK send path (bound or unbound, base or
    subclass, text or protobuf) ends in ``self.conn.sendMsg(frame)``, so this is the lowest shared
    choke point: it allows the one initial ``API\\0`` handshake, then only ``ALLOWED_OUT_MESSAGES``.

    It exposes exactly the five methods the SDK uses (no ``__getattr__`` forwarding, no ``socket``,
    no ``lock``) and holds no reference to the raw connection (see ``_RAW_CONNECTIONS``)."""

    __slots__ = ("_client", "_handshake_sent", "_token")  # no __weakref__: see _RAW_CONNECTIONS

    def __init__(self, raw, client: EClient) -> None:
        self._token = next(_TOKENS)
        _RAW_CONNECTIONS[self._token] = raw
        self._client = client
        self._handshake_sent = False

    def __del__(self) -> None:  # the entry lives exactly as long as its guard
        _RAW_CONNECTIONS.pop(getattr(self, "_token", None), None)

    def sendMsg(self, msg: bytes):
        if not isinstance(msg, (bytes, bytearray)):
            raise ReadOnlyViolation("outgoing frame must be bytes")
        msg = bytes(msg)
        if msg.startswith(b"API\0"):
            if self._handshake_sent:
                raise ReadOnlyViolation("a second API handshake frame is refused")
            self._handshake_sent = True
            return _raw_call(self, "sendMsg", msg)
        if (not self._handshake_sent or len(msg) < 8
                or int.from_bytes(msg[:4], "big") != len(msg) - 4):
            raise ReadOnlyViolation("malformed or out-of-order outgoing frame refused")
        mid = _outgoing_msg_id(msg, self._client.serverVersion())
        if mid not in ALLOWED_OUT_MESSAGES:
            raise ReadOnlyViolation(f"outgoing message id {mid} refused at the connection")
        return _raw_call(self, "sendMsg", msg)

    def connect(self):
        return _raw_call(self, "connect")

    def disconnect(self):
        return _raw_call(self, "disconnect")

    def isConnected(self) -> bool:
        return bool(_raw_call(self, "isConnected"))

    def recvMsg(self) -> bytes:
        return _raw_call(self, "recvMsg")


class _ReadOnlyClient(EClient):
    """``EClient`` with every non-allowlisted public method replaced by one that raises, the two
    send primitives refusing ids outside ``ALLOWED_OUT_MESSAGES``, and ``conn`` always wrapped in
    ``_GuardedConnection`` (the SDK assigns ``self.conn = Connection(...)`` in ``connect``)."""

    @property
    def conn(self):
        return self.__dict__.get("_guarded_conn")

    @conn.setter
    def conn(self, value) -> None:
        self.__dict__["_guarded_conn"] = (
            None if value is None or isinstance(value, _GuardedConnection)
            else _GuardedConnection(value, self))

    def sendMsg(self, msgId: int, msg: str):
        if msgId not in ALLOWED_OUT_MESSAGES:
            raise ReadOnlyViolation(f"outgoing message id {msgId} is not allowed")
        return super().sendMsg(msgId, msg)

    def sendMsgProtoBuf(self, msgId: int, msg: bytes):
        if msgId - PROTOBUF_MSG_ID not in ALLOWED_OUT_MESSAGES:
            raise ReadOnlyViolation(f"outgoing protobuf message id {msgId} is not allowed")
        return super().sendMsgProtoBuf(msgId, msg)


for _name in dir(EClient):
    if (not _name.startswith("_") and callable(getattr(EClient, _name))
            and _name not in ALLOWED_CLIENT_METHODS):
        setattr(_ReadOnlyClient, _name, _denied(_name))
del _name


def _query_contract(query: ContractQuery) -> Contract:
    """SDK ``Contract`` for a query; unset optional fields keep the SDK defaults."""
    c = Contract()
    c.symbol, c.secType, c.exchange = query.symbol, query.sec_type, query.exchange
    c.currency, c.primaryExchange = query.currency, query.primary_exchange
    c.lastTradeDateOrContractMonth = query.last_trade_date_or_contract_month
    c.multiplier, c.tradingClass = query.multiplier, query.trading_class
    c.localSymbol, c.right = query.local_symbol, query.right
    if query.strike is not None:
        c.strike = float(query.strike)
    c.includeExpired = bool(query.include_expired)
    return c


def _mkt_contract(contract: ResolvedContract) -> Contract:
    """SDK ``Contract`` for a market-data request on a resolved contract."""
    c = Contract()
    c.conId, c.symbol, c.secType = contract.con_id, contract.symbol, contract.sec_type
    c.currency, c.exchange = contract.currency, contract.exchange or "SMART"
    c.primaryExchange = contract.primary_exchange
    return c


def _set_min_tick(p: _Pending, contract: ResolvedContract) -> None:
    """Give both sides the contract's minimum tick (off-tick prices never count as changes)."""
    tick = getattr(contract, "min_tick", None)
    for side in p.sides.values():
        side.min_tick = tick


def _check_limit(limit: int) -> int:
    if (isinstance(limit, bool) or not isinstance(limit, int)
            or not 1 <= limit <= MAX_DISCOVERY_LIMIT):
        raise ValueError(f"limit must be an int in 1..{MAX_DISCOVERY_LIMIT}")
    return limit


def _decimal_or_none(value: Any) -> Decimal | None:
    return None if value is None or value == UNSET_DECIMAL else Decimal(value)


def _discovered(cd: Any, query: ContractQuery) -> DiscoveredContract:
    """Map an SDK ``ContractDetails`` (1052.1 attribute names) to ``DiscoveredContract``."""
    k = cd.contract
    return DiscoveredContract(
        con_id=int(k.conId), symbol=k.symbol, local_symbol=k.localSymbol, sec_type=k.secType,
        exchange=k.exchange, primary_exchange=k.primaryExchange, currency=k.currency,
        multiplier=str(k.multiplier or ""), min_tick=float(cd.minTick),
        price_magnifier=int(cd.priceMagnifier), trading_class=k.tradingClass,
        last_trade_date_or_contract_month=k.lastTradeDateOrContractMonth,
        last_trade_date=k.lastTradeDate, last_trade_time=cd.lastTradeTime,
        contract_month=cd.contractMonth, real_expiration_date=cd.realExpirationDate,
        strike=None if k.strike == UNSET_DOUBLE else float(k.strike), right=k.right,
        under_con_id=int(cd.underConId), under_symbol=cd.underSymbol,
        under_sec_type=cd.underSecType, long_name=cd.longName, market_name=cd.marketName,
        stock_type=cd.stockType, time_zone_id=cd.timeZoneId, trading_hours=cd.tradingHours,
        liquid_hours=cd.liquidHours, valid_exchanges=cd.validExchanges,
        order_types=cd.orderTypes, market_rule_ids=cd.marketRuleIds,
        min_size=_decimal_or_none(cd.minSize), size_increment=_decimal_or_none(cd.sizeIncrement),
        suggested_size_increment=_decimal_or_none(cd.suggestedSizeIncrement),
        query_match=(k.secType, k.currency) == (query.sec_type, query.currency))


class SessionState(str, Enum):
    DISCONNECTED = "DISCONNECTED"
    CONNECTING = "CONNECTING"
    READY = "READY"
    CLOSING = "CLOSING"


# Monotonic clock for the per-side stamps and ``observed_at_monotonic`` (a seam for tests only).
_side_clock = time.monotonic


@dataclass
class _Pending:
    kind: str
    req_id: int
    done: bool = False
    ended: bool = False  # broker end marker received (no cancel needed for one-shot requests)
    error: BaseException | None = None
    rows: list = field(default_factory=list)
    notices: list[int] = field(default_factory=list)
    max_rows: int | None = None  # rows beyond this are counted in ``overflow``, not kept
    overflow: int = 0
    # quote-only
    prices: dict[str, float | None] = field(default_factory=dict)
    sizes: dict[str, Decimal | None] = field(default_factory=dict)
    market_data_type: int | None = None
    delayed_ticks: bool = False
    live_ticks: bool = False
    last_trade_time: str | None = None
    last_trade_epoch: int | None = None
    unavailable_code: int | None = None
    close: float | None = None
    tick_counts: dict[str, int] = field(default_factory=dict)
    mdt_values: list[int] = field(default_factory=list)
    tick_req_params: dict | None = None
    events: list[tuple[int, str]] = field(default_factory=list)
    t0: float = 0.0
    first_tick: float | None = None
    last_tick: float | None = None
    last_new_tick_type: float | None = None  # when a tick type not seen before last arrived
    ended_early: bool = False
    timed_out: bool = False
    request_error: str | None = None
    sides: dict[str, SideEvidence] = field(
        default_factory=lambda: {"bid": SideEvidence(), "ask": SideEvidence()})
    stream: bool = False  # a held-open stream (open_quote_stream), not a bounded request
    halted: float | None = None  # latest HALTED tick (49) value
    refused: bool = False  # refused before anything was sent: no cancel

    def fail(self, exc: BaseException) -> None:
        if self.error is None:
            self.error = exc
        self.done = True


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


class _Wrapper(EWrapper):
    """Forwards the callbacks we use to the session. Signatures match ibapi 1052.1 ``EWrapper``.

    The SDK's base callbacks log their arguments; we override instead of calling ``super()`` so no
    account ID is logged. Every callback is guarded: an exception here would kill ``EClient.run()``.
    """

    def __init__(self, session: IbkrSession) -> None:
        super().__init__()
        self._s = session

    def _call(self, fn: Callable[..., None], *args: Any) -> None:
        try:
            fn(*args)
        except Exception as exc:  # noqa: BLE001 - must never propagate into the SDK loop
            self._s._on_internal_error(exc)

    def error(self, reqId, errorTime, errorCode, errorString, advancedOrderRejectJson=""):
        self._call(self._s._on_error, reqId, errorCode, errorString)

    def connectAck(self):
        pass

    def connectionClosed(self):
        self._call(self._s._on_connection_closed)

    def nextValidId(self, orderId):
        self._call(self._s._on_next_valid_id)  # the order id itself is deliberately not kept

    def managedAccounts(self, accountsList):
        self._call(self._s._on_managed_accounts, accountsList)

    def contractDetails(self, reqId, contractDetails):
        self._call(self._s._on_row, reqId, contractDetails)

    def contractDetailsEnd(self, reqId):
        self._call(self._s._on_end, reqId)

    def securityDefinitionOptionParameter(self, reqId, exchange, underlyingConId, tradingClass,
                                          multiplier, expirations, strikes):
        self._call(self._s._on_row, reqId, (exchange, underlyingConId, tradingClass, multiplier,
                                            expirations, strikes))

    def securityDefinitionOptionParameterEnd(self, reqId):
        self._call(self._s._on_end, reqId)

    def accountSummary(self, reqId, account, tag, value, currency):
        self._call(self._s._on_account_summary, reqId, account, tag, value, currency)

    def accountSummaryEnd(self, reqId):
        self._call(self._s._on_end, reqId)

    def position(self, account, contract, position, avgCost):
        self._call(self._s._on_position, account, contract, position, avgCost)

    def positionEnd(self):
        self._call(self._s._on_position_end)

    def marketDataType(self, reqId, marketDataType):
        self._call(self._s._on_market_data_type, reqId, marketDataType)

    def tickPrice(self, reqId, tickType, price, attrib):
        self._call(self._s._on_tick_price, reqId, tickType, price)

    def tickSize(self, reqId, tickType, size):
        self._call(self._s._on_tick_size, reqId, tickType, size)

    def tickString(self, reqId, tickType, value):
        self._call(self._s._on_tick_string, reqId, tickType, value)

    def tickGeneric(self, reqId, tickType, value):
        self._call(self._s._on_tick_generic, reqId, tickType, value)

    def tickReqParams(self, tickerId, minTick, bboExchange, snapshotPermissions):
        self._call(self._s._on_tick_req_params, tickerId, minTick, bboExchange,
                   snapshotPermissions)

    def tickSnapshotEnd(self, reqId):
        self._call(self._s._on_end, reqId)

    def configResponseProtoBuf(self, configResponseProto):
        self._call(self._s._on_config_response, configResponseProto)


class QuoteStream:
    """Handle for one held-open streaming ``reqMktData`` (see ``IbkrSession.open_quote_stream``).

    ``latest()`` returns a consistent snapshot of everything received so far (built under the
    session lock, so no tick callback interleaves). ``close()`` cancels the subscription and
    returns the final snapshot. Once closed (by ``close``, ``disconnect``, poison or a lost
    connection) ``latest()`` raises ``QuoteStreamClosed``; ``final`` keeps the last snapshot."""

    def __init__(self, session: IbkrSession, stream_id: int, contract: ResolvedContract,
                 requested_market_data_type: int) -> None:
        self._session = session
        self.stream_id = stream_id
        self.contract = contract
        self.requested_market_data_type = requested_market_data_type
        self.opened_at = _now_iso()
        self.closed_reason: str | None = None
        self.final: QuoteSnapshot | None = None

    @property
    def closed(self) -> bool:
        return self.closed_reason is not None

    def latest(self) -> QuoteSnapshot:
        return self._session._stream_latest(self)

    def close(self) -> QuoteSnapshot:
        return self._session.close_quote_stream(self)

    def __repr__(self) -> str:
        state = self.closed_reason or "OPEN"
        return f"QuoteStream(id={self.stream_id}, {self.contract.symbol}, {state})"


class IbkrSession:
    """Read-only paper session. Use as a context manager::

        with IbkrSession(load_config()) as s:
            tlt = s.resolve_contract(TLT_QUERY)
    """

    sdk_version = SDK_VERSION
    max_quote_streams = 4  # simultaneous held streams; at most MAX_QUOTE_STREAMS

    def __init__(self, config: PaperConnectionConfig, *, connect_timeout: float = 15.0,
                 request_timeout: float = 10.0, quote_timeout: float = 20.0,
                 stream_seconds: float = 8.0, stream_settle_seconds: float = 1.0,
                 shutdown_timeout: float = 5.0,
                 preflight: Callable[[PaperConnectionConfig], dict] = run_preflight) -> None:
        if not isinstance(config, PaperConnectionConfig) or config.mode != "paper":
            raise SessionStateError("IbkrSession requires a validated PaperConnectionConfig")
        _silence_sdk_logging()  # again, in case something reconfigured logging since import
        self._cfg = config
        self._preflight = preflight
        self._connect_timeout = connect_timeout
        self._request_timeout = request_timeout
        self._quote_timeout = quote_timeout
        self._stream_seconds = stream_seconds
        self._stream_settle_seconds = stream_settle_seconds
        self._shutdown_timeout = shutdown_timeout

        self._cond = threading.Condition(threading.RLock())
        self._state = SessionState.DISCONNECTED
        self._used = False
        self._wrapper = _Wrapper(self)
        self._client = _ReadOnlyClient(self._wrapper)
        self._handshake_thread: threading.Thread | None = None
        self._handshake_exc: BaseException | None = None
        self._run_thread: threading.Thread | None = None
        self._reader: threading.Thread | None = None
        self._capture = threading.local()

        self._next_valid_id_seen = False
        self._managed_accounts: str | None = None
        self._connect_failure: BaseException | None = None
        self._identity_failure: AccountIdentityError | None = None
        self._connection_lost: str | None = None
        self._connectivity_interrupted = False
        self._poisoned: IbkrError | None = None
        self._config_first_api: bytes | None = None
        self._pending: dict[int, _Pending] = {}
        self._positions: _Pending | None = None
        self._next_req_id = 9000
        self._streams: dict[int, QuoteStream] = {}  # open held streams by reqId
        self._stream_mdt: int | None = None  # the one market-data type the open streams use

        self.notices: list[tuple[int, int, str]] = []
        self.connection_events: list[tuple[int, int, str]] = []
        self.unrouted_errors: list[tuple[int, int, str]] = []
        self.internal_errors: list[str] = []
        self.cleanup_failures: list[str] = []
        self.server_version: int | None = None
        self.connection_time: str | None = None
        self.connected_at: str | None = None
        self.preflight_result: dict | None = None
        self.read_only_status = "NOT_CHECKED"  # VERIFIED once the config read passed
        self.read_only_settings: dict | None = None

    # ------------------------------------------------------------------ public properties
    @property
    def state(self) -> SessionState:
        return self._state

    @property
    def poisoned(self) -> bool:
        return self._poisoned is not None

    @property
    def masked_account(self) -> str:
        return self._cfg.masked_account()

    def __repr__(self) -> str:
        return f"IbkrSession(state={self._state.value}, account={self.masked_account!r})"

    @property
    def reader_joined(self) -> bool:
        """True only if an EReader thread existed and has exited."""
        return self._reader is not None and not self._reader.is_alive()

    def threads_alive(self) -> list[str]:
        return [t.name for t in (self._handshake_thread, self._run_thread, self._reader)
                if t is not None and t.is_alive()]

    # ------------------------------------------------------------------ lifecycle
    def __enter__(self) -> Self:
        if self._state is not SessionState.READY:
            self.connect()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        try:
            self.disconnect()
        except CleanupError as cleanup:
            if exc is None:
                raise
            exc.add_note(f"additionally: {cleanup}")
        return False

    def connect(self) -> None:
        """Handshake, then the identity gate. Raises and disconnects on any failure."""
        with self._cond:
            if self._used:
                raise SessionStateError("IbkrSession is single-use; create a new one to reconnect")
            self._used = True
            self._state = SessionState.CONNECTING
        try:  # local checks; no socket has been opened yet
            self.preflight_result = self._preflight(self._cfg)
        except BaseException:
            with self._cond:
                self._state = SessionState.DISCONNECTED
            raise
        deadline = time.monotonic() + self._connect_timeout

        self._handshake_thread = threading.Thread(target=self._handshake, name="ibkr-handshake",
                                                  daemon=True)
        self._handshake_thread.start()
        self._handshake_thread.join(max(0.0, deadline - time.monotonic()))
        if self._handshake_thread.is_alive():
            self._close_transport()  # unblocks EClient.connect's handshake loop
            self._abort_connect(IbkrConnectionError(
                f"TWS API handshake with {self._cfg.host}:{self._cfg.port} did not complete within "
                f"{self._connect_timeout:.1f}s"))
        if self._handshake_exc is not None:
            self._abort_connect(IbkrConnectionError(
                f"EClient.connect raised {type(self._handshake_exc).__name__}: "
                f"{self._handshake_exc}"))
        with self._cond:
            failure = self._connect_failure
        if failure is not None or not self._client.isConnected():
            self._abort_connect(failure or IbkrConnectionError(
                f"could not connect to TWS at {self._cfg.host}:{self._cfg.port}"))

        self.server_version = self._client.serverVersion()
        conn_time = self._client.twsConnectionTime()  # 1052.1 returns the raw handshake bytes
        self.connection_time = (conn_time.decode("ascii", "replace")
                                if isinstance(conn_time, bytes) else conn_time)
        self.connected_at = _now_iso()
        self._reader = self._client.reader
        if not isinstance(self.server_version, int) or self.server_version < MIN_SERVER_VERSION:
            self._abort_connect(UnsupportedServerVersion(
                f"TWS server version {self.server_version} is below the minimum "
                f"{MIN_SERVER_VERSION} this adapter supports"))
        self._run_thread = threading.Thread(target=self._run_loop, name="ibkr-run", daemon=True)
        self._run_thread.start()

        with self._cond:
            self._cond.wait_for(
                lambda: self._connect_failure is not None
                or (self._next_valid_id_seen and self._managed_accounts is not None),
                timeout=max(0.0, deadline - time.monotonic()))
            failure = self._connect_failure
            seen_id, accounts = self._next_valid_id_seen, self._managed_accounts
        if failure is not None:
            self._abort_connect(failure)
        if not seen_id or accounts is None:
            self._abort_connect(AccountIdentityError(
                f"identity gate timed out after {self._connect_timeout:.1f}s (nextValidId "
                f"{'seen' if seen_id else 'missing'}, managedAccounts "
                f"{'seen' if accounts is not None else 'missing'})", reason="TIMEOUT"))
        try:
            check_managed_accounts(accounts, self._cfg.account)
        except AccountIdentityError as exc:
            self._abort_connect(exc)
        try:
            self._check_read_only()
        except BaseException as exc:  # noqa: BLE001 - always disconnect, then re-raise
            self._abort_connect(exc)
        with self._cond:  # final re-compare under the lock, then READY
            failure = self._identity_failure or self._poisoned or self._connect_failure
            if failure is None:
                try:
                    check_managed_accounts(self._managed_accounts, self._cfg.account)
                except AccountIdentityError as exc:
                    failure = exc
            if failure is None:
                self._state = SessionState.READY
                return
        self._abort_connect(failure)

    def _check_read_only(self) -> None:
        """After the identity gate, before READY (so before any data request).

        config-read: ``reqConfigProtoBuf(ConfigRequest(reqId))`` and require, from the correlated
        ``ConfigResponse``, ``api.settings.readOnlyApi`` explicitly present and True,
        ``socketPort`` present and equal to the configured port, ``allowLocalhostOnly`` present and
        True. Presence is checked with ``HasField`` (these are proto3 ``optional`` fields with
        explicit presence), so an unset field is never read as a value. Each failure mode has its
        own type; the caller disconnects.
        """
        if self._cfg.read_only_check != "config-read":  # the only legal value
            raise ReadOnlyNotVerified(f"unknown read_only_check {self._cfg.read_only_check!r}")
        if self.server_version is None or self.server_version < MIN_SERVER_VER_CONFIG:
            raise ConfigReadUnsupported(
                f"config read needs server version >= {MIN_SERVER_VER_CONFIG}, TWS reports "
                f"{self.server_version}")

        def build(p: _Pending) -> object:
            return p.rows[0]

        try:
            resp = self._request(
                "config", lambda rid: self._client.reqConfigProtoBuf(ConfigRequest(reqId=rid)),
                None, self._request_timeout, cancel_always=False, build=build, gate=True)
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
        if fields["readOnlyApi"] is not True:
            problems.append(f"readOnlyApi={fields['readOnlyApi']}")
        if fields["socketPort"] != self._cfg.port:
            problems.append(f"socketPort={fields['socketPort']} (configured {self._cfg.port})")
        if fields["allowLocalhostOnly"] is not True:
            problems.append(f"allowLocalhostOnly={fields['allowLocalhostOnly']}")
        if problems:
            raise ReadOnlySettingRefused("TWS API settings refused (None = absent): "
                                         + ", ".join(problems))
        self.read_only_status = "VERIFIED"

    def disconnect(self) -> None:
        """Bounded shutdown: close the socket, join every thread, state DISCONNECTED.

        Raises ``CleanupError`` if a thread did not stop or the client still reports connected.
        """
        with self._cond:
            if self._state is SessionState.DISCONNECTED and not self.threads_alive():
                return
            self._state = SessionState.CLOSING
        failures: list[str] = self._close_all_streams("SESSION_DISCONNECTED")
        reader = self._reader or self._client.reader
        try:
            if self._handshake_thread is not None and self._handshake_thread.is_alive():
                self._close_transport()  # EClient.connect's handshake loop exits on a closed conn
                self._handshake_thread.join(self._shutdown_timeout)
                if self._handshake_thread.is_alive():
                    failures.append("handshake thread did not exit")
            if self._run_thread is not None and self._run_thread.is_alive():
                self._close_transport()  # run() drains its queue, exits and calls disconnect()
                self._run_thread.join(self._shutdown_timeout)
                if self._run_thread.is_alive():
                    failures.append("EClient.run thread did not exit")
            else:
                self._client.disconnect()
            reader = reader or self._client.reader
            if reader is not None:
                self._reader = reader
                reader.join(self._shutdown_timeout)
                if reader.is_alive():
                    failures.append("EReader thread did not exit")
            if self._client.isConnected():
                failures.append("EClient still reports connected")
        except Exception as exc:  # noqa: BLE001 - reported, never swallowed
            failures.append(f"disconnect raised {type(exc).__name__}: {exc}")
        with self._cond:
            self._state = SessionState.DISCONNECTED
            self._fail_all(SessionStateError("session disconnected"))
            self._cond.notify_all()
        if failures:
            self.cleanup_failures.extend(failures)
            raise CleanupError("disconnect was not clean", failures=failures)

    # ------------------------------------------------------------------ read requests
    def resolve_contract(self, query: ContractQuery, *, timeout: float | None = None,
                         ) -> ResolvedContract:
        c = _query_contract(query)
        can_cancel = (self.server_version or 0) >= MIN_SERVER_VER_CANCEL_CONTRACT_DATA

        def build(p: _Pending) -> list:
            return list(p.rows)

        try:
            rows = self._request(
                "contract", lambda rid: self._client.reqContractDetails(rid, c),
                (lambda rid: self._client.cancelContractData(rid)) if can_cancel else None,
                timeout or self._request_timeout, cancel_always=False, build=build)
        except IbkrRequestError as exc:
            if exc.code == 200:  # "No security definition has been found for the request"
                raise ContractNotFound(f"no contract for {query}: {exc.broker_message}") from exc
            raise
        if not rows:
            raise ContractNotFound(f"no contract matched {query}")
        if len(rows) > 1:
            raise AmbiguousContract(f"{len(rows)} contracts matched {query}", count=len(rows))
        cd = rows[0]
        k = cd.contract
        if (k.symbol, k.secType, k.currency) != (query.symbol, query.sec_type, query.currency):
            raise ContractNotFound(f"returned contract {k.symbol}/{k.secType}/{k.currency} does "
                                   f"not match {query}")
        return ResolvedContract(
            con_id=int(k.conId), symbol=k.symbol, sec_type=k.secType, currency=k.currency,
            exchange=k.exchange, primary_exchange=k.primaryExchange, local_symbol=k.localSymbol,
            min_tick=float(cd.minTick), time_zone_id=cd.timeZoneId, trading_hours=cd.tradingHours,
            liquid_hours=cd.liquidHours)

    def discover_contracts(self, query: ContractQuery, *, limit: int = 50,
                           timeout: float | None = None) -> DiscoveryResult:
        """``reqContractDetails`` returning EVERY matching contract (sorted by last trade date or
        contract month, then strike, right, conId), at most ``limit`` of them. If more arrive, the
        wait ends early, the request is cancelled (``cancelContractData``) and the result is
        ``truncated``. Error 200 or an empty end -> ``NOT_FOUND``; any other request-scoped broker
        error -> ``ERROR`` (both carry the broker's code and message). Identity, poison, connection,
        cleanup and timeout failures raise, exactly as for every other request."""
        limit = _check_limit(limit)
        c = _query_contract(query)

        def build(p: _Pending) -> tuple[list, bool]:
            return list(p.rows), p.overflow > 0

        try:
            rows, truncated = self._request(
                "contract", lambda rid: self._client.reqContractDetails(rid, c),
                lambda rid: self._client.cancelContractData(rid),
                timeout or self._request_timeout, cancel_always=False, build=build,
                max_rows=limit, early=lambda p: p.overflow > 0)
        except IbkrRequestError as exc:
            status = DISCOVERY_NOT_FOUND if exc.code == NO_SECURITY_DEFINITION else DISCOVERY_ERROR
            return DiscoveryResult(query=query, status=status, contracts=(), limit=limit,
                                   error_code=exc.code, error_message=exc.broker_message)
        found = sorted((_discovered(cd, query) for cd in rows), key=lambda d: (
            d.last_trade_date_or_contract_month, d.strike or 0.0, d.right, d.con_id))
        status = DISCOVERY_FOUND if found else DISCOVERY_NOT_FOUND
        return DiscoveryResult(query=query, status=status, contracts=tuple(found), limit=limit,
                               truncated=truncated)

    def option_chain_params(self, underlying: Any, *, fut_fop_exchange: str = "",
                            limit: int = 100, timeout: float | None = None) -> OptionChainResult:
        """``reqSecDefOptParams`` for an underlying (anything with ``con_id``, ``symbol`` and
        ``sec_type``, e.g. a ``DiscoveredContract``): one row per exchange / trading class with
        its expirations, strikes and multiplier. Read-only; the SDK has no cancel for it, so the
        request always waits for its end (rows beyond ``limit`` are counted, not kept)."""
        limit = _check_limit(limit)
        symbol, sec_type = str(underlying.symbol), str(underlying.sec_type)
        con_id = int(underlying.con_id)

        def build(p: _Pending) -> tuple[list, bool]:
            return list(p.rows), p.overflow > 0

        common = {"underlying_symbol": symbol, "underlying_sec_type": sec_type,
                  "underlying_con_id": con_id, "fut_fop_exchange": fut_fop_exchange,
                  "limit": limit}
        try:
            rows, truncated = self._request(
                "option_params", lambda rid: self._client.reqSecDefOptParams(
                    rid, symbol, fut_fop_exchange, sec_type, con_id),
                None, timeout or self._request_timeout, cancel_always=False, build=build,
                max_rows=limit)
        except IbkrRequestError as exc:
            status = DISCOVERY_NOT_FOUND if exc.code == NO_SECURITY_DEFINITION else DISCOVERY_ERROR
            return OptionChainResult(status=status, params=(), error_code=exc.code,
                                     error_message=exc.broker_message, **common)
        params = sorted((OptionChainParams(
            exchange=str(ex), underlying_con_id=int(ucid), trading_class=str(tc),
            multiplier=str(mult), expirations=tuple(sorted(str(e) for e in exps)),
            strikes=tuple(sorted(float(k) for k in strikes)))
            for ex, ucid, tc, mult, exps, strikes in rows),
            key=lambda o: (o.exchange, o.trading_class))
        return OptionChainResult(status=DISCOVERY_FOUND if params else DISCOVERY_NOT_FOUND,
                                 params=tuple(params), truncated=truncated, **common)

    def account_summary(self, *, timeout: float | None = None) -> AccountSummary:
        tags = ",".join(ACCOUNT_SUMMARY_TAGS)

        def build(p: _Pending) -> AccountSummary:
            values = {tag: AccountValue(value=value, currency=currency)
                      for tag, value, currency in p.rows}
            missing = tuple(t for t in ACCOUNT_SUMMARY_TAGS if t not in values)
            return AccountSummary(account_masked=self.masked_account, values=values,
                                  missing_tags=missing)

        return self._request(
            "account_summary", lambda rid: self._client.reqAccountSummary(rid, "All", tags),
            lambda rid: self._client.cancelAccountSummary(rid), timeout or self._request_timeout,
            cancel_always=True, build=build)

    def positions(self, *, timeout: float | None = None) -> tuple[PositionRow, ...]:
        def build(p: _Pending) -> tuple[PositionRow, ...]:
            return tuple(p.rows)

        return self._request(
            "positions", lambda rid: self._client.reqPositions(),
            lambda rid: self._client.cancelPositions(), timeout or self._request_timeout,
            cancel_always=True, build=build)

    def quote(self, contract: ResolvedContract, *, market_data_type: int = 3,
              stream_seconds: float | None = None, snapshot_fallback: bool = False,
              snapshot_timeout: float | None = None) -> QuoteReport:
        """ONE bounded streaming sample first (it ends early once the quote is complete, see
        ``quote_stream_sample``). Only if the caller asks (``snapshot_fallback``) and the stream
        did not deliver a valid two-sided quote: one snapshot as a second attempt. Every attempt is
        kept as evidence; the verdict comes from ``assess_quote_attempts``.

        Why stream first: on the paper account the snapshot delivered nothing at all in 20 s (no
        tick, no type, no error, no end) while the stream delivered a full delayed quote."""
        attempts = [self.quote_stream_sample(contract, market_data_type=market_data_type,
                                             seconds=stream_seconds)]
        if snapshot_fallback and not attempts[0].two_sided_valid:
            attempts.append(self.quote_snapshot(contract, market_data_type=market_data_type,
                                                timeout=snapshot_timeout))
        status, reason = assess_quote_attempts(tuple(attempts))
        return QuoteReport(attempts=tuple(attempts), status=status, reason=reason)

    def quote_snapshot(self, contract: ResolvedContract, *, market_data_type: int = 3,
                       timeout: float | None = None) -> QuoteSnapshot:
        """One snapshot. ``market_data_type`` 3 asks for live data and lets TWS fall back to delayed
        if the account is not entitled; the type actually delivered is reported, not assumed.
        A snapshot that does not end in time is returned with what arrived, flagged
        ``snapshot_end_missing`` (and cancelled)."""
        return self._quote_attempt(contract, market_data_type, snapshot=True,
                                   wait=timeout or self._quote_timeout)

    def quote_stream_sample(self, contract: ResolvedContract, *, market_data_type: int = 3,
                            seconds: float | None = None) -> QuoteSnapshot:
        """Streaming ``reqMktData`` for a bounded window (default ``stream_seconds``, 8 s), then
        always ``cancelMktData``. Ends early (``ended_early``) once bid, ask, last and the
        market-data type have all arrived and ``stream_settle_seconds`` (1 s) have passed with
        no new tick type."""
        settle = self._stream_settle_seconds

        def complete(p: _Pending) -> bool:
            return (all(k in p.prices for k in ("bid", "ask", "last"))
                    and p.market_data_type is not None and p.last_new_tick_type is not None
                    and time.monotonic() - p.last_new_tick_type >= settle)

        return self._quote_attempt(contract, market_data_type, snapshot=False,
                                   wait=seconds or self._stream_seconds, early=complete)

    def _quote_attempt(self, contract: ResolvedContract, market_data_type: int, *,
                       snapshot: bool, wait: float,
                       early: Callable[[_Pending], bool] | None = None) -> QuoteSnapshot:
        if market_data_type not in MARKET_DATA_TYPE_NAMES:
            raise ValueError("market_data_type must be 1, 2, 3 or 4")
        c = _mkt_contract(contract)

        def send(rid: int) -> None:
            _set_min_tick(self._pending[rid], contract)
            try:  # under the lock, before anything is sent
                self._check_stream_type(market_data_type)
            except QuoteStreamConflict:
                self._pending[rid].refused = True  # nothing was requested: send no cancel
                raise
            self._client.reqMarketDataType(market_data_type)
            self._client.reqMktData(rid, c, "", snapshot, False, [])

        def build(p: _Pending) -> QuoteSnapshot:
            return self._build_quote(p, contract, market_data_type,
                                     route="snapshot" if snapshot else "stream",
                                     snapshot=snapshot, window=wait)

        with self._cond:  # refuse before registering anything (the type is session-wide)
            self._check_stream_type(market_data_type)
        return self._request(
            "quote", send, lambda rid: self._client.cancelMktData(rid), wait,
            cancel_always=not snapshot, build=build, soft=True, early=early)

    def _check_stream_type(self, market_data_type: int) -> None:
        """Caller holds the lock. ``reqMarketDataType`` has no reqId: it is session-wide in the
        SDK. While held streams are open, every quote request must ask for their type."""
        if self._streams and self._stream_mdt != market_data_type:
            raise QuoteStreamConflict(
                f"market data type {market_data_type} conflicts with the open quote streams' "
                f"type {self._stream_mdt} (reqMarketDataType is session-wide)")

    def _build_quote(self, p: _Pending, contract: ResolvedContract, requested: int, *,
                     route: str, snapshot: bool, window: float) -> QuoteSnapshot:
        """One consistent ``QuoteSnapshot`` of ``p``, built under the session lock."""
        with self._cond:
            bid, ask, last = p.prices.get("bid"), p.prices.get("ask"), p.prices.get("last")
            b, a = p.sides["bid"], p.sides["ask"]
            received = datetime.now(UTC)
            mono = _side_clock()

            def change_age(side: SideEvidence) -> float | None:
                if side.changes < 1 or side.fresh_mono is None:
                    return None
                return mono - side.fresh_mono  # fresh_mono is the latest change once changed

            def price_change_age(side: SideEvidence) -> float | None:
                return None if side.price_change_mono is None else mono - side.price_change_mono

            verdict = classify_quote(
                bid=bid, ask=ask, last=last, market_data_type=p.market_data_type,
                delayed_ticks_seen=p.delayed_ticks, live_ticks_seen=p.live_ticks,
                bid_updates=b.changes, ask_updates=a.changes, notices=tuple(p.notices),
                unavailable_code=p.unavailable_code,
                stream_failed=p.stream and p.error is not None,
                requested_market_data_type=requested, bid_change_age_seconds=change_age(b),
                ask_change_age_seconds=change_age(a), market_data_types_seen=tuple(p.mdt_values),
                bid_price_changes=b.price_changes, ask_price_changes=a.price_changes,
                bid_price_change_age_seconds=price_change_age(b),
                ask_price_change_age_seconds=price_change_age(a))
            flags = verdict.flags
            for name, sd in (("BID", b), ("ASK", a)):
                if sd.placeholder_seen:
                    flags = (*flags, f"{name}_PLACEHOLDER_TICK")
                if sd.non_finite_seen:
                    flags = (*flags, f"{name}_NON_FINITE_TICK")
                if sd.off_tick_seen:
                    flags = (*flags, f"{name}_OFF_TICK_TICK")
            if p.halted is not None and p.halted != 0:
                flags = (*flags, "HALTED")
            end_missing = snapshot and not p.ended
            age = quote_age_seconds(received, p.last_trade_epoch)
            if age is None:
                flags = (*flags, "QUOTE_AGE_UNKNOWN")
            if end_missing:
                flags = (*flags, "SNAPSHOT_END_MISSING")
            request_error = p.request_error
            if p.stream and p.error is not None:
                request_error = redact_accounts(str(p.error))
            if request_error:
                flags = (*flags, "REQUEST_ERROR")

            def ms(t: float | None) -> int | None:
                return None if t is None else int((t - p.t0) * 1000)

            def iso(t: datetime | None) -> str | None:
                return None if t is None else t.isoformat(timespec="milliseconds")

            return QuoteSnapshot(
                con_id=contract.con_id, symbol=contract.symbol, bid=bid,
                bid_size=p.sizes.get("bid"), ask=ask, ask_size=p.sizes.get("ask"), last=last,
                last_size=p.sizes.get("last"), market_data_type=p.market_data_type,
                market_data_type_name=MARKET_DATA_TYPE_NAMES.get(p.market_data_type, "UNREPORTED"),
                requested_market_data_type=requested,
                received_at=received.isoformat(timespec="milliseconds"),
                last_trade_time=p.last_trade_time, quality=verdict.quality, flags=flags,
                notices=tuple(p.notices), unavailable_code=p.unavailable_code,
                route=route, close=p.close, snapshot_end_missing=end_missing,
                waited_ms=int((time.monotonic() - p.t0) * 1000), tick_counts=dict(p.tick_counts),
                market_data_types_delivered=tuple(p.mdt_values),
                tick_req_params=dict(p.tick_req_params) if p.tick_req_params else None,
                request_events=tuple(p.events), request_error=request_error,
                first_tick_ms=ms(p.first_tick), last_tick_ms=ms(p.last_tick),
                quote_age_seconds=age, ended_early=p.ended_early, window_ms=int(window * 1000),
                bid_at=iso(b.fresh_wall), ask_at=iso(a.fresh_wall),
                bid_at_monotonic=b.fresh_mono, ask_at_monotonic=a.fresh_mono,
                bid_updates=b.changes, ask_updates=a.changes,
                bid_tick_class=b.tick_class, ask_tick_class=a.tick_class,
                quality_reason=verdict.reason, observed_at_monotonic=mono,
                bid_last_tick_monotonic=b.last_tick_mono,
                ask_last_tick_monotonic=a.last_tick_mono, halted=p.halted,
                bid_price_change_monotonic=b.price_change_mono,
                ask_price_change_monotonic=a.price_change_mono)

    # ------------------------------------------------------------------ held-open quote streams
    def open_quote_stream(self, contract: ResolvedContract, *,
                          market_data_type: int = 3) -> QuoteStream:
        """Open a streaming ``reqMktData`` (``snapshot=False``) for one resolved contract and keep
        it open until ``close_quote_stream`` / ``QuoteStream.close``, ``disconnect``, poison or
        connection loss (each of which cancels it). Sends exactly what a bounded sample sends:
        ``reqMarketDataType(market_data_type)`` then ``reqMktData``.

        Refused (nothing sent) when the session is not READY, when ``max_quote_streams`` streams
        are open (``QuoteStreamLimit``), or when open streams use another market-data type
        (``QuoteStreamConflict``: the SDK's ``reqMarketDataType`` is session-wide, so one type
        per session at a time)."""
        if market_data_type not in MARKET_DATA_TYPE_NAMES:
            raise ValueError("market_data_type must be 1, 2, 3 or 4")
        cap = self.max_quote_streams
        if isinstance(cap, bool) or not isinstance(cap, int) or not 1 <= cap <= MAX_QUOTE_STREAMS:
            raise ValueError(f"max_quote_streams must be an int in 1..{MAX_QUOTE_STREAMS}")
        c = _mkt_contract(contract)
        primary: BaseException | None = None
        with self._cond:
            self._require_ready()
            if len(self._streams) >= cap:
                raise QuoteStreamLimit(f"{len(self._streams)} quote streams already open "
                                       f"(limit {cap})")
            self._check_stream_type(market_data_type)
            self._next_req_id += 1
            p = _Pending("quote", self._next_req_id, stream=True)
            _set_min_tick(p, contract)
            p.t0 = time.monotonic()
            handle = QuoteStream(self, p.req_id, contract, market_data_type)
            self._pending[p.req_id] = p
            self._streams[p.req_id] = handle
            self._stream_mdt = market_data_type
            try:  # sent under the lock: no poison/loss callback can interleave
                self._client.reqMarketDataType(market_data_type)
                self._client.reqMktData(p.req_id, c, "", False, False, [])
            except BaseException as exc:  # noqa: BLE001 - cancel below, then re-raise
                primary = exc
            if primary is None and p.error is not None:
                primary = p.error  # reported synchronously by the SDK during the send
        if primary is None:
            return handle
        failures: list[str] = []
        try:
            self._detach_stream(handle, "OPEN_FAILED")
            failures = self._cancel("quote_stream", p.req_id,
                                    lambda rid: self._client.cancelMktData(rid))
        finally:
            if failures:
                self.cleanup_failures.extend(failures)
                primary.add_note("cleanup also failed: " + "; ".join(failures))
        raise primary

    def quote_streams(self) -> tuple[QuoteStream, ...]:
        """The currently open held streams."""
        with self._cond:
            return tuple(self._streams.values())

    def _stream_latest(self, handle: QuoteStream) -> QuoteSnapshot:
        with self._cond:
            if handle.closed:
                raise QuoteStreamClosed(f"quote stream {handle.stream_id} is closed "
                                        f"({handle.closed_reason})")
            p = self._pending.get(handle.stream_id)
            if p is None or self._streams.get(handle.stream_id) is not handle:
                raise QuoteStreamClosed(f"quote stream {handle.stream_id} is not open")
            return self._build_quote(p, handle.contract, handle.requested_market_data_type,
                                     route="held_stream", snapshot=False, window=0.0)

    def _detach_stream(self, handle: QuoteStream, reason: str) -> bool:
        """Mark closed, keep the final snapshot, unregister. False if it was already closed."""
        with self._cond:
            if handle.closed:
                return False
            p = self._pending.get(handle.stream_id)
            if p is not None:
                handle.final = self._build_quote(
                    p, handle.contract, handle.requested_market_data_type, route="held_stream",
                    snapshot=False, window=0.0)
            handle.closed_reason = reason
            self._pending.pop(handle.stream_id, None)
            self._streams.pop(handle.stream_id, None)
            if not self._streams:
                self._stream_mdt = None
            return True

    def close_quote_stream(self, handle: QuoteStream) -> QuoteSnapshot:
        """Cancel one held stream (``cancelMktData``) and return its final snapshot. Idempotent:
        closing a closed stream returns its final snapshot and sends nothing. A failed cancel is
        recorded in ``cleanup_failures`` and raises ``CleanupError`` (``result`` = the final
        snapshot), never reported as success."""
        if handle._session is not self:
            raise SessionStateError("quote stream belongs to another session")
        if not self._detach_stream(handle, "CLOSED"):
            if handle.final is None:
                raise QuoteStreamClosed(f"quote stream {handle.stream_id} was never opened")
            return handle.final
        failures = self._cancel("quote_stream", handle.stream_id,
                                lambda rid: self._client.cancelMktData(rid))
        if failures:
            self.cleanup_failures.extend(failures)
            raise CleanupError("quote stream cleanup failed", failures=failures,
                               result=handle.final)
        return handle.final

    def _close_all_streams(self, reason: str) -> list[str]:
        """Detach and cancel every open stream; returns the cancel failures (not recorded)."""
        with self._cond:
            handles = list(self._streams.values())
        failures: list[str] = []
        for h in handles:
            if self._detach_stream(h, reason):
                failures += self._cancel("quote_stream", h.stream_id,
                                         lambda rid: self._client.cancelMktData(rid))
        return failures

    # ------------------------------------------------------------------ request machinery
    def _require_ready(self, gate: bool = False) -> None:
        if self._identity_failure is not None:
            raise AccountIdentityError(f"identity failed: {self._identity_failure}",
                                       reason="IDENTITY_FAILED")
        if isinstance(self._poisoned, AccountIdentityError):
            raise AccountIdentityError(f"session poisoned: {self._poisoned}", reason="POISONED")
        if self._poisoned is not None:
            raise ConfigResponseChanged(f"session poisoned: {self._poisoned}")
        if self._connection_lost is not None:
            raise IbkrConnectionError(f"connection lost: {self._connection_lost}")
        if self._connectivity_interrupted:
            raise IbkrConnectionError("TWS reports its server connectivity is interrupted")
        allowed = (SessionState.CONNECTING,) if gate else (SessionState.READY,)
        if self._state not in allowed:
            raise SessionStateError(f"session is {self._state.value}, not READY")

    def _request(self, kind: str, send: Callable[[int], None],
                 cancel: Callable[[int], None] | None, timeout: float, *, cancel_always: bool,
                 build: Callable[[_Pending], T], gate: bool = False, soft: bool = False,
                 early: Callable[[_Pending], bool] | None = None,
                 max_rows: int | None = None) -> T:
        """``soft`` (quotes): a timeout or a request-scoped broker error does not raise; the
        result is built from what arrived. Connection loss and poisoning always raise.
        ``early`` (streaming quotes): polled under the lock; True ends the wait as complete."""
        with self._cond:
            self._require_ready(gate)
            self._next_req_id += 1
            p = _Pending(kind, self._next_req_id, max_rows=max_rows)
            if kind == "positions":
                if self._positions is not None:
                    raise SessionStateError("a positions request is already outstanding")
                self._positions = p
            else:
                self._pending[p.req_id] = p
        primary: BaseException | None = None
        failures: list[str] = []
        try:
            with self._cond:
                # Re-check and send under the lock: no poison/loss callback can interleave.
                self._require_ready(gate)
                p.t0 = time.monotonic()
                send(p.req_id)
            with self._cond:
                if early is None:
                    completed = self._cond.wait_for(lambda: p.done, timeout)
                else:
                    completed = self._wait_or_early(p, timeout, early)
            if not completed:
                if soft:
                    p.timed_out = True
                else:
                    primary = IbkrTimeout(
                        f"{kind} (reqId {p.req_id}) timed out after {timeout:.1f}s")
            elif p.error is not None:
                if soft and isinstance(p.error, IbkrRequestError):
                    p.request_error = str(p.error)
                else:
                    primary = p.error
        except BaseException as exc:  # noqa: BLE001 - cleanup must still run (incl. Ctrl-C)
            primary = exc
        finally:
            if cancel is not None and not p.refused and (cancel_always or not p.ended):
                failures = self._cancel(kind, p.req_id, cancel)
            with self._cond:
                if kind == "positions":
                    self._positions = None
                else:
                    self._pending.pop(p.req_id, None)
        if isinstance(primary, (AccountIdentityError, ConfigResponseChanged)):
            try:
                self.disconnect()
            except CleanupError as exc:
                failures.extend(exc.failures)
        if failures:
            self.cleanup_failures.extend(failures)
        if primary is not None:
            if failures:
                primary.add_note("cleanup also failed: " + "; ".join(failures))
            raise primary
        result = build(p)
        if failures:
            raise CleanupError(f"{kind} cleanup failed", failures=failures, result=result)
        return result

    def _wait_or_early(self, p: _Pending, timeout: float,
                       early: Callable[[_Pending], bool]) -> bool:
        """Caller holds the lock. Tick callbacks do not notify, so poll every 50 ms."""
        deadline = time.monotonic() + timeout
        while not p.done:
            if early(p):
                p.ended_early = True
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            self._cond.wait(min(remaining, 0.05))
        return True

    def _cancel(self, kind: str, req_id: int, cancel: Callable[[int], None]) -> list[str]:
        """Send a cancel. TWS does not acknowledge cancels, so 'clean' means: the connection was up
        and the SDK reported no send error synchronously on this thread."""
        if not self._client.isConnected() or self._connection_lost is not None:
            return [f"{kind} (reqId {req_id}): cancel not sent, connection is down"]
        self._capture.errors = []
        failures: list[str] = []
        try:
            cancel(req_id)
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{kind} (reqId {req_id}) cancel raised {type(exc).__name__}: {exc}")
        finally:
            captured, self._capture.errors = self._capture.errors, None
        for _rid, code, msg in captured:
            if classify_error(code) is not ErrorClass.NOTICE:
                failures.append(f"{kind} (reqId {req_id}) cancel failed: {code} {msg}")
        return [redact_accounts(f) for f in failures]

    def _fail_all(self, exc: BaseException) -> None:
        for p in list(self._pending.values()) + ([self._positions] if self._positions else []):
            p.fail(exc)

    # ------------------------------------------------------------------ SDK-thread plumbing
    def _handshake(self) -> None:
        try:
            self._client.connect(self._cfg.host, self._cfg.port, self._cfg.client_id)
        except BaseException as exc:  # noqa: BLE001
            self._handshake_exc = exc

    def _run_loop(self) -> None:
        try:
            self._client.run()
        except Exception as exc:  # noqa: BLE001
            self.internal_errors.append(redact_accounts(f"EClient.run raised {exc!r}"))
        finally:
            self._on_connection_closed()

    def _close_transport(self) -> None:
        conn = self._client.conn
        if conn is not None:
            conn.disconnect()

    def _abort_connect(self, exc: BaseException):
        try:
            self.disconnect()
        except CleanupError as cleanup:
            exc.add_note(f"additionally: {cleanup}")
        raise exc

    def _poison(self, exc: IbkrError) -> None:
        with self._cond:
            if self._poisoned is None:
                self._poisoned = exc
            self._fail_all(exc)
            self._cond.notify_all()
        # The poison is set, so nothing new can be sent; cancel the held streams while the
        # transport is still open, then close it.
        failures = self._close_all_streams("SESSION_POISONED")
        if failures:
            self.cleanup_failures.extend(failures)
        self._close_transport()

    def _lost(self, reason: str) -> None:
        with self._cond:
            if self._state is SessionState.CLOSING:
                return
            if self._connection_lost is None:
                self._connection_lost = reason
            exc = IbkrConnectionError(f"connection lost: {reason}")
            if self._state is SessionState.CONNECTING and self._connect_failure is None:
                self._connect_failure = exc
            self._fail_all(exc)
            for h in list(self._streams.values()):  # TWS drops them; a cancel cannot be sent
                if self._detach_stream(h, "CONNECTION_LOST"):
                    self.cleanup_failures.append(
                        f"quote_stream (reqId {h.stream_id}): cancel not sent, connection is down")
            self._cond.notify_all()

    # ------------------------------------------------------------------ callbacks (SDK threads)
    def _on_internal_error(self, exc: BaseException) -> None:
        self.internal_errors.append(redact_accounts(f"{type(exc).__name__}: {exc}"))
        with self._cond:
            self._fail_all(IbkrError(f"internal callback error: {type(exc).__name__}"))
            self._cond.notify_all()

    def _on_connection_closed(self) -> None:
        self._lost("connection closed")

    def _on_next_valid_id(self) -> None:
        with self._cond:
            self._next_valid_id_seen = True
            self._cond.notify_all()

    def _on_managed_accounts(self, accounts_list: str) -> None:
        """Every managedAccounts, from the first one on, must be exactly the allowlisted account;
        one bad value fails the gate permanently (e.g. a live account first, paper second)."""
        try:
            check_managed_accounts(accounts_list, self._cfg.account)
            exc = None
        except AccountIdentityError as e:
            exc = e
        with self._cond:
            self._managed_accounts = accounts_list
            if exc is not None:
                self._identity_failure = self._identity_failure or exc
                if self._state is SessionState.CONNECTING:
                    self._connect_failure = self._connect_failure or exc
            ready = self._state is SessionState.READY
            self._cond.notify_all()
        if exc is not None and ready:
            self._poison(exc)

    def _on_config_response(self, proto) -> None:
        """Correlate strictly: a response without our reqId fails the outstanding config read.
        After the first correlated response, any later one (same or other reqId) whose ``api``
        section differs from the first poisons the session; an identical one is ignored."""
        rid = proto.reqId if proto.HasField("reqId") else None
        api = proto.api.SerializeToString(deterministic=True) if proto.HasField("api") else b""
        changed: ConfigResponseChanged | None = None
        with self._cond:
            if self._config_first_api is not None:
                if api == self._config_first_api:
                    self.unrouted_errors.append((rid if rid is not None else -1, 0,
                                                 "repeated identical config response ignored"))
                    return
                changed = ConfigResponseChanged(
                    f"a later config response (reqId {rid}) differs from the first")
                if self._state is SessionState.CONNECTING:
                    self._connect_failure = self._connect_failure or changed
            else:
                waiting = [p for p in self._pending.values() if p.kind == "config" and not p.done]
                for p in waiting:
                    if rid == p.req_id:
                        p.rows.append(proto)
                        p.ended = p.done = True
                        self._config_first_api = api
                    else:
                        p.fail(ConfigReadUncorrelated(
                            f"config response reqId {rid} does not match request {p.req_id}"))
                if not waiting:
                    self.unrouted_errors.append((rid if rid is not None else -1, 0,
                                                 "unsolicited config response ignored"))
            self._cond.notify_all()
        if changed is not None:
            self._poison(changed)

    def _on_error(self, req_id: int, code: int, message: str) -> None:
        msg = redact_accounts(message)
        cls = classify_error(code)
        captured = getattr(self._capture, "errors", None)
        if captured is not None:  # synchronous SDK error raised inside one of our cancel calls
            captured.append((req_id, code, msg))
            return
        with self._cond:
            p = self._positions if req_id == -1 and self._positions else self._pending.get(req_id)
            if p is not None and p.kind == "quote":
                p.events.append((code, msg))
            if cls is ErrorClass.NOTICE:
                self.notices.append((req_id, code, msg))
                if p is not None:
                    p.notices.append(code)
                return
            if cls is ErrorClass.CONNECTION:
                self.connection_events.append((req_id, code, msg))
                if code in (1101, 1102):
                    self._connectivity_interrupted = False
                elif code in (1100, 2110):
                    self._connectivity_interrupted = True
                    exc = IbkrConnectionError(f"TWS connectivity event {code}: {msg}")
                    if self._state is SessionState.CONNECTING:
                        self._connect_failure = self._connect_failure or exc
                    self._fail_all(exc)
                elif code in CONNECTION_LOSS_CODES:
                    self._lost(f"{code}: {msg}")
                self._cond.notify_all()
                return
            if cls is ErrorClass.MARKET_DATA_UNAVAILABLE and p is not None and p.kind == "quote":
                p.unavailable_code = code
                p.notices.append(code)
                p.done = True
                self._cond.notify_all()
                return
            if p is not None:
                p.fail(IbkrRequestError(code, msg, req_id))
            elif self._state is SessionState.CONNECTING:
                self._connect_failure = self._connect_failure or IbkrRequestError(code, msg, req_id)
            else:
                self.unrouted_errors.append((req_id, code, msg))
            self._cond.notify_all()

    def _on_row(self, req_id: int, row: object) -> None:
        with self._cond:
            p = self._pending.get(req_id)
            if p is not None:
                if p.max_rows is not None and len(p.rows) >= p.max_rows:
                    p.overflow += 1
                else:
                    p.rows.append(row)

    def _on_end(self, req_id: int) -> None:
        with self._cond:
            p = self._pending.get(req_id)
            if p is not None:
                p.ended = p.done = True
                self._cond.notify_all()

    def _on_account_summary(self, req_id, account, tag, value, currency) -> None:
        try:
            check_row_account(account, self._cfg.account, "accountSummary")
        except AccountIdentityError as exc:
            self._poison(exc)
            return
        with self._cond:
            p = self._pending.get(req_id)
            if p is not None:
                p.rows.append((tag, value, currency))

    def _on_position(self, account, contract, position, avg_cost) -> None:
        try:
            check_row_account(account, self._cfg.account, "position")
        except AccountIdentityError as exc:
            self._poison(exc)
            return
        row = PositionRow(
            account_masked=mask_account(account), con_id=int(contract.conId),
            symbol=contract.symbol, sec_type=contract.secType, currency=contract.currency,
            exchange=contract.exchange or contract.primaryExchange, quantity=Decimal(position),
            avg_cost=float(avg_cost))
        with self._cond:
            if self._positions is not None:
                self._positions.rows.append(row)

    def _on_position_end(self) -> None:
        with self._cond:
            if self._positions is not None:
                self._positions.ended = self._positions.done = True
                self._cond.notify_all()

    def _quote_pending(self, req_id: int) -> _Pending | None:
        p = self._pending.get(req_id)
        return p if p is not None and p.kind == "quote" else None

    def _count_tick(self, p: _Pending, tick_type: int) -> None:
        try:
            name = TickTypeEnum.toStr(int(tick_type))
        except Exception:  # noqa: BLE001 - unknown id: count it by number
            name = f"TICK_{tick_type}"
        now = time.monotonic()
        if name not in p.tick_counts:
            p.last_new_tick_type = now
        p.tick_counts[name] = p.tick_counts.get(name, 0) + 1
        p.first_tick = p.first_tick if p.first_tick is not None else now
        p.last_tick = now
        if int(tick_type) in DELAYED_TICK_IDS:
            p.delayed_ticks = True

    def _on_market_data_type(self, req_id: int, market_data_type: int) -> None:
        with self._cond:
            p = self._quote_pending(req_id)
            if p is not None:
                p.market_data_type = int(market_data_type)
                p.mdt_values.append(int(market_data_type))

    def _on_tick_req_params(self, req_id, min_tick, bbo_exchange, snapshot_permissions) -> None:
        with self._cond:
            p = self._quote_pending(req_id)
            if p is not None:
                p.tick_req_params = {"minTick": float(min_tick), "bboExchange": str(bbo_exchange),
                                     "snapshotPermissions": int(snapshot_permissions)}

    def _on_tick_generic(self, req_id: int, tick_type: int, value) -> None:
        with self._cond:
            p = self._quote_pending(req_id)
            if p is None:
                return
            self._count_tick(p, tick_type)
            if int(tick_type) == HALTED_TICK:
                try:
                    p.halted = float(value)
                except (TypeError, ValueError):
                    p.halted = float("nan")  # unreadable: treated as halted

    def _on_tick_other(self, req_id: int, tick_type: int) -> None:
        with self._cond:
            p = self._quote_pending(req_id)
            if p is not None:
                self._count_tick(p, tick_type)

    def _on_tick_price(self, req_id: int, tick_type: int, price: float) -> None:
        tick = int(tick_type)
        with self._cond:
            p = self._quote_pending(req_id)
            if p is None:
                return
            self._count_tick(p, tick)
            value = None if price == UNSET_DOUBLE else float(price)
            if tick in CLOSE_TICKS:
                p.close = value
                return
            mapped = PRICE_TICKS.get(tick)
            if mapped is None:
                return
            name, delayed = mapped
            p.prices[name] = value
            p.live_ticks |= not delayed
            if name in p.sides:
                p.sides[name].observe("price", value, delayed, mono=_side_clock(),
                                      wall=datetime.now(UTC))

    def _on_tick_size(self, req_id: int, tick_type: int, size) -> None:
        with self._cond:
            p = self._quote_pending(req_id)
            if p is None:
                return
            self._count_tick(p, tick_type)
            mapped = SIZE_TICKS.get(int(tick_type))
            if mapped is not None:
                value = None if size == UNSET_DECIMAL else Decimal(size)
                p.sizes[mapped[0]] = value
                if mapped[0] in p.sides:
                    p.sides[mapped[0]].observe("size", value, mapped[1], mono=_side_clock(),
                                               wall=datetime.now(UTC))

    def _on_tick_string(self, req_id: int, tick_type: int, value: str) -> None:
        with self._cond:
            p = self._quote_pending(req_id)
            if p is None:
                return
            self._count_tick(p, tick_type)
            if int(tick_type) not in LAST_TIMESTAMP_TICKS:
                return
            try:
                epoch = int(value)
                p.last_trade_time = datetime.fromtimestamp(epoch, UTC).isoformat()
                p.last_trade_epoch = epoch
            except (TypeError, ValueError, OverflowError, OSError):
                p.last_trade_time = f"unparsed:{value!r}"
                p.last_trade_epoch = None
