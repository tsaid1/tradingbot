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
    ContractNotFound,
    ErrorClass,
    IbkrConnectionError,
    IbkrError,
    IbkrRequestError,
    IbkrTimeout,
    ReadOnlyNotVerified,
    ReadOnlySettingRefused,
    ReadOnlyViolation,
    SessionStateError,
    UnsupportedServerVersion,
    classify_error,
)
from tradingbot.ibkr.identity import check_managed_accounts, check_row_account
from tradingbot.ibkr.models import (
    LAST_TIMESTAMP_TICKS,
    MARKET_DATA_TYPE_NAMES,
    PRICE_TICKS,
    SIZE_TICKS,
    AccountSummary,
    AccountValue,
    ContractQuery,
    PositionRow,
    QuoteSnapshot,
    ResolvedContract,
    assess_quote,
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
})
ALLOWED_OUT_MESSAGES = frozenset({
    OUT.START_API, OUT.REQ_CONTRACT_DATA, OUT.CANCEL_CONTRACT_DATA, OUT.REQ_ACCOUNT_SUMMARY,
    OUT.CANCEL_ACCOUNT_SUMMARY, OUT.REQ_POSITIONS, OUT.CANCEL_POSITIONS, OUT.REQ_MKT_DATA,
    OUT.CANCEL_MKT_DATA, OUT.REQ_MARKET_DATA_TYPE, OUT.REQ_CONFIG,
})


def _denied(name: str) -> Callable[..., None]:
    def method(self, *args: Any, **kwargs: Any) -> None:
        raise ReadOnlyViolation(f"EClient.{name} is not allowed by the read-only adapter")

    method.__name__ = name
    return method


class _ReadOnlyClient(EClient):
    """``EClient`` with every non-allowlisted public method replaced by one that raises, and the
    two send primitives refusing any message id outside ``ALLOWED_OUT_MESSAGES``."""

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


class SessionState(str, Enum):
    DISCONNECTED = "DISCONNECTED"
    CONNECTING = "CONNECTING"
    READY = "READY"
    CLOSING = "CLOSING"


@dataclass
class _Pending:
    kind: str
    req_id: int
    done: bool = False
    ended: bool = False  # broker end marker received (no cancel needed for one-shot requests)
    error: BaseException | None = None
    rows: list = field(default_factory=list)
    notices: list[int] = field(default_factory=list)
    # quote-only
    prices: dict[str, float | None] = field(default_factory=dict)
    sizes: dict[str, Decimal | None] = field(default_factory=dict)
    market_data_type: int | None = None
    delayed_ticks: bool = False
    live_ticks: bool = False
    last_trade_time: str | None = None
    unavailable_code: int | None = None

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
        pass

    def tickReqParams(self, tickerId, minTick, bboExchange, snapshotPermissions):
        pass

    def tickSnapshotEnd(self, reqId):
        self._call(self._s._on_end, reqId)

    def configResponseProtoBuf(self, configResponseProto):
        self._call(self._s._on_config_response, configResponseProto)


class IbkrSession:
    """Read-only paper session. Use as a context manager::

        with IbkrSession(load_config()) as s:
            tlt = s.resolve_contract(TLT_QUERY)
    """

    sdk_version = SDK_VERSION

    def __init__(self, config: PaperConnectionConfig, *, connect_timeout: float = 15.0,
                 request_timeout: float = 10.0, quote_timeout: float = 15.0,
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
        self._poisoned: AccountIdentityError | None = None
        self._pending: dict[int, _Pending] = {}
        self._positions: _Pending | None = None
        self._next_req_id = 9000

        self.notices: list[tuple[int, int, str]] = []
        self.connection_events: list[tuple[int, int, str]] = []
        self.unrouted_errors: list[tuple[int, int, str]] = []
        self.internal_errors: list[str] = []
        self.cleanup_failures: list[str] = []
        self.server_version: int | None = None
        self.connection_time: str | None = None
        self.connected_at: str | None = None
        self.preflight_result: dict | None = None
        self.read_only_status = "NOT_CHECKED"  # VERIFIED | OWNER_ATTESTED_NOT_VERIFIED
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
        if self._cfg.read_only_check == "owner-attested":
            self.read_only_status = "OWNER_ATTESTED_NOT_VERIFIED"
            return
        if self._cfg.read_only_check != "config-read":
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
        failures: list[str] = []
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
        c = Contract()
        c.symbol, c.secType, c.exchange = query.symbol, query.sec_type, query.exchange
        c.currency, c.primaryExchange = query.currency, query.primary_exchange
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

    def quote_snapshot(self, contract: ResolvedContract, *, market_data_type: int = 3,
                       timeout: float | None = None) -> QuoteSnapshot:
        """One snapshot. ``market_data_type`` 3 asks for live data and lets TWS fall back to delayed
        if the account is not entitled; the type actually delivered is reported, not assumed."""
        if market_data_type not in MARKET_DATA_TYPE_NAMES:
            raise ValueError("market_data_type must be 1, 2, 3 or 4")
        c = Contract()
        c.conId, c.symbol, c.secType = contract.con_id, contract.symbol, contract.sec_type
        c.currency, c.exchange = contract.currency, contract.exchange or "SMART"
        c.primaryExchange = contract.primary_exchange

        def send(rid: int) -> None:
            self._client.reqMarketDataType(market_data_type)
            self._client.reqMktData(rid, c, "", True, False, [])

        def build(p: _Pending) -> QuoteSnapshot:
            bid, ask, last = p.prices.get("bid"), p.prices.get("ask"), p.prices.get("last")
            quality, flags = assess_quote(
                bid=bid, ask=ask, last=last, market_data_type=p.market_data_type,
                delayed_ticks_seen=p.delayed_ticks, live_ticks_seen=p.live_ticks,
                unavailable_code=p.unavailable_code)
            return QuoteSnapshot(
                con_id=contract.con_id, symbol=contract.symbol, bid=bid,
                bid_size=p.sizes.get("bid"), ask=ask, ask_size=p.sizes.get("ask"), last=last,
                last_size=p.sizes.get("last"), market_data_type=p.market_data_type,
                market_data_type_name=MARKET_DATA_TYPE_NAMES.get(p.market_data_type, "UNREPORTED"),
                requested_market_data_type=market_data_type, received_at=_now_iso(),
                last_trade_time=p.last_trade_time, quality=quality, flags=flags,
                notices=tuple(p.notices), unavailable_code=p.unavailable_code)

        return self._request(
            "quote", send, lambda rid: self._client.cancelMktData(rid),
            timeout or self._quote_timeout, cancel_always=False, build=build)

    # ------------------------------------------------------------------ request machinery
    def _require_ready(self, gate: bool = False) -> None:
        if self._identity_failure is not None:
            raise AccountIdentityError(f"identity failed: {self._identity_failure}",
                                       reason="IDENTITY_FAILED")
        if self._poisoned is not None:
            raise AccountIdentityError(f"session poisoned: {self._poisoned}", reason="POISONED")
        if self._connection_lost is not None:
            raise IbkrConnectionError(f"connection lost: {self._connection_lost}")
        if self._connectivity_interrupted:
            raise IbkrConnectionError("TWS reports its server connectivity is interrupted")
        allowed = (SessionState.CONNECTING,) if gate else (SessionState.READY,)
        if self._state not in allowed:
            raise SessionStateError(f"session is {self._state.value}, not READY")

    def _request(self, kind: str, send: Callable[[int], None],
                 cancel: Callable[[int], None] | None, timeout: float, *, cancel_always: bool,
                 build: Callable[[_Pending], T], gate: bool = False) -> T:
        with self._cond:
            self._require_ready(gate)
            self._next_req_id += 1
            p = _Pending(kind, self._next_req_id)
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
                send(p.req_id)
            with self._cond:
                completed = self._cond.wait_for(lambda: p.done, timeout)
            if not completed:
                primary = IbkrTimeout(f"{kind} (reqId {p.req_id}) timed out after {timeout:.1f}s")
            elif p.error is not None:
                primary = p.error
        except BaseException as exc:  # noqa: BLE001 - cleanup must still run (incl. Ctrl-C)
            primary = exc
        finally:
            if cancel is not None and (cancel_always or not p.ended):
                failures = self._cancel(kind, p.req_id, cancel)
            with self._cond:
                if kind == "positions":
                    self._positions = None
                else:
                    self._pending.pop(p.req_id, None)
        if isinstance(primary, AccountIdentityError):
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

    def _poison(self, exc: AccountIdentityError) -> None:
        with self._cond:
            if self._poisoned is None:
                self._poisoned = exc
            self._fail_all(exc)
            self._cond.notify_all()
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
        """Correlate strictly: a response without our reqId fails the outstanding config read."""
        rid = proto.reqId if proto.HasField("reqId") else None
        with self._cond:
            waiting = [p for p in self._pending.values() if p.kind == "config" and not p.done]
            for p in waiting:
                if rid == p.req_id:
                    p.rows.append(proto)
                    p.ended = p.done = True
                else:
                    p.fail(ConfigReadUncorrelated(
                        f"config response reqId {rid} does not match request {p.req_id}"))
            if not waiting:
                self.unrouted_errors.append((rid if rid is not None else -1, 0,
                                             "unsolicited config response ignored"))
            self._cond.notify_all()

    def _on_error(self, req_id: int, code: int, message: str) -> None:
        msg = redact_accounts(message)
        cls = classify_error(code)
        captured = getattr(self._capture, "errors", None)
        if captured is not None:  # synchronous SDK error raised inside one of our cancel calls
            captured.append((req_id, code, msg))
            return
        with self._cond:
            p = self._positions if req_id == -1 and self._positions else self._pending.get(req_id)
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

    def _on_market_data_type(self, req_id: int, market_data_type: int) -> None:
        with self._cond:
            p = self._pending.get(req_id)
            if p is not None and p.kind == "quote":
                p.market_data_type = int(market_data_type)

    def _on_tick_price(self, req_id: int, tick_type: int, price: float) -> None:
        mapped = PRICE_TICKS.get(int(tick_type))
        with self._cond:
            p = self._pending.get(req_id)
            if p is None or p.kind != "quote" or mapped is None:
                return
            name, delayed = mapped
            p.prices[name] = None if price == UNSET_DOUBLE else float(price)
            p.delayed_ticks |= delayed
            p.live_ticks |= not delayed

    def _on_tick_size(self, req_id: int, tick_type: int, size) -> None:
        mapped = SIZE_TICKS.get(int(tick_type))
        with self._cond:
            p = self._pending.get(req_id)
            if p is None or p.kind != "quote" or mapped is None:
                return
            p.sizes[mapped[0]] = None if size == UNSET_DECIMAL else Decimal(size)

    def _on_tick_string(self, req_id: int, tick_type: int, value: str) -> None:
        if int(tick_type) not in LAST_TIMESTAMP_TICKS:
            return
        try:
            ts = datetime.fromtimestamp(int(value), UTC).isoformat()
        except (TypeError, ValueError, OverflowError, OSError):
            ts = f"unparsed:{value!r}"
        with self._cond:
            p = self._pending.get(req_id)
            if p is not None and p.kind == "quote":
                p.last_trade_time = ts
