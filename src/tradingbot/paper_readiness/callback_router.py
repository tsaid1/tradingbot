"""SDK-free callback router over the merged lifecycle adapter and normalizers. NON_AUTHORITATIVE.

``CallbackRouter`` owns the association between a callback's payload and the adapter's consumer. It
receives EWrapper-named callbacks with invented primitive values, routes each by its request to the
accepted normalizer that owns that request, and builds the one ``LifecycleAdapter`` whose queue and
``dispatch`` it supplies. It imports no SDK, opens no socket and starts no thread. Real SDK integration
is a later, separately reviewed task.

* **Payload ownership.** Lifecycle reads (quote snapshot, account summary, positions) are admitted to
  the adapter and retained in a bounded FIFO of immutable records with unique ids. The adapter's
  consumer fetches one record at a time, and ``dispatch`` delivers only that fetched record. Equal
  ``(slot, kind)`` values therefore never let one DATA payload stand in for another. A refused
  admission retains nothing dispatchable.
* **Retention.** Primitives are validated and copied before retention, and contract fields are
  snapshot into frozen values. Anything that cannot be retained becomes a sentinel the normalizer
  rejects, so the malformed fault survives without keeping the payload.
* **Contract details** have no ledger slot. They are routed directly, outside lifecycle accounting,
  and never borrow a quote slot. Local cleanup covers the three lifecycle reads only.
* **Fixed routing.** Requests are registered once each, and the positions window opens once.
  Unknown ids and session errors follow fixed rules, keep their original ids and are never dropped
  silently. Contract and quote errors use ``error_1052``; account and position errors are fixed
  unsupported refusals plus a lifecycle ERROR. No version is inferred.
* **Direct and refused callbacks.** ``connectAck`` and ``connectionClosed`` are recorded by the adapter
  as direct observations, never socket or broker evidence. Every unsupported callback is a persistent
  router fault, even before any stream registers. An applicable error inside a stop hook is also
  reported to the adapter, which settles that stop UNKNOWN.

``completed`` is local only and never broker-side retirement, PAPER-READ-1 acceptance or trading
permission. See docs/CALLBACK_ROUTER.md.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Any

from tradingbot.paper_readiness.account_position_callbacks import (
    AccountSummaryCallbacks,
    PositionSnapshotCallbacks,
)
from tradingbot.paper_readiness.diagnostics import SESSION_OK_CODES
from tradingbot.paper_readiness.ibkr_callbacks import CallbackRequest, IbkrCallbackNormalizer
from tradingbot.paper_readiness.observations import ResponseEnvelope
from tradingbot.paper_readiness.read_retirement import CallbackKind, ReadSlot
from tradingbot.paper_readiness.sdk_lifecycle import AdapterReport, DirectCallback, LifecycleAdapter

__all__ = [
    "MAX_FAULTS",
    "MAX_QUEUE_BYTES",
    "MAX_QUEUE_RECORDS",
    "MAX_TEXT",
    "CallbackRouter",
    "RouterFault",
    "RouterReport",
]

MAX_QUEUE_RECORDS = 256    # retained records awaiting the consumer
MAX_QUEUE_BYTES = 64 * 1024  # estimated retained text and fields
MAX_TEXT = 4096            # longest retained string
MAX_INT_BITS = 128         # larger integers are not retained
MAX_DECIMAL_DIGITS = 40    # a finite Decimal's coefficient digits
MAX_DECIMAL_EXPONENT = 40  # and its exponent's magnitude, both checked without expanding it
MAX_FAULTS = 64
MAX_ID = 2**31 - 1
SESSION_REQUEST_ID = -1
_FIELD_COST = 16


class RouterFault(str, Enum):
    ADAPTER_NOT_OPEN = "ADAPTER_NOT_OPEN"
    REGISTRATION_REFUSED = "REGISTRATION_REFUSED"
    POSITIONS_WINDOW_REOPENED = "POSITIONS_WINDOW_REOPENED"
    POSITIONS_WINDOW_ABSENT = "POSITIONS_WINDOW_ABSENT"
    UNKNOWN_REQUEST_ID = "UNKNOWN_REQUEST_ID"
    UNROUTABLE_CALLBACK = "UNROUTABLE_CALLBACK"
    UNSUPPORTED_CALLBACK = "UNSUPPORTED_CALLBACK"
    SESSION_ERROR = "SESSION_ERROR"
    SESSION_ERROR_MALFORMED = "SESSION_ERROR_MALFORMED"
    ADMISSION_REFUSED = "ADMISSION_REFUSED"
    QUEUE_OVERFLOW = "QUEUE_OVERFLOW"
    ARRIVAL_AFTER_CLOSE = "ARRIVAL_AFTER_CLOSE"
    ARRIVAL_REENTERED = "ARRIVAL_REENTERED"
    FETCH_WHILE_OUTSTANDING = "FETCH_WHILE_OUTSTANDING"
    FETCHED_NOT_DISPATCHED = "FETCHED_NOT_DISPATCHED"
    DISPATCH_WITHOUT_FETCH = "DISPATCH_WITHOUT_FETCH"
    DISPATCH_MISMATCH = "DISPATCH_MISMATCH"
    DISPATCH_REENTERED = "DISPATCH_REENTERED"


class _Malformed:
    """The value retained in place of anything that cannot be retained. Every normalizer rejects it."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "<malformed>"


_MALFORMED = _Malformed()


def _value(value: object) -> object:
    """A bounded immutable primitive, or the malformed sentinel. Never a reference to a mutable object."""
    kind = type(value)
    if value is None or kind is float:
        return value
    if kind is Decimal:
        if not value.is_finite():
            return value  # NaN and infinities are small; the normalizers reject them
        _sign, digits, exponent = value.as_tuple()
        bounded = len(digits) <= MAX_DECIMAL_DIGITS and abs(exponent) <= MAX_DECIMAL_EXPONENT
        return value if bounded else _MALFORMED
    if kind is int:
        return value if value.bit_length() <= MAX_INT_BITS else _MALFORMED
    if kind is str:
        return value if len(value) <= MAX_TEXT else _MALFORMED
    return _MALFORMED


def _attribute(obj: object, name: str) -> object:
    try:
        return _value(getattr(obj, name))
    except Exception:  # noqa: BLE001 - a missing or failing attribute is retained as malformed
        return _MALFORMED


@dataclass(frozen=True)
class _ContractSnapshot:
    conId: object
    symbol: object
    localSymbol: object
    secType: object
    currency: object
    exchange: object
    primaryExchange: object


@dataclass(frozen=True)
class _DetailsSnapshot:
    contract: object
    longName: object
    tradingHours: object
    liquidHours: object
    timeZoneId: object
    minTick: object


def _contract(obj: object) -> _ContractSnapshot:
    return _ContractSnapshot(*(_attribute(obj, name) for name in (
        "conId", "symbol", "localSymbol", "secType", "currency", "exchange", "primaryExchange")))


def _details(obj: object) -> _DetailsSnapshot:
    try:
        contract = obj.contract
    except Exception:  # noqa: BLE001 - retained as a malformed contract
        contract = None
    return _DetailsSnapshot(_contract(contract), *(_attribute(obj, name) for name in (
        "longName", "tradingHours", "liquidHours", "timeZoneId", "minTick")))


class _Method(str, Enum):
    """The fixed set of deliveries a retained record can make; there is no dynamic lookup."""
    MARKET_DATA_TYPE = "marketDataType"
    TICK_PRICE = "tickPrice"
    TICK_SIZE = "tickSize"
    TICK_STRING = "tickString"
    TICK_GENERIC = "tickGeneric"
    TICK_SNAPSHOT_END = "tickSnapshotEnd"
    QUOTE_ERROR = "error_1052"
    ACCOUNT_SUMMARY = "accountSummary"
    ACCOUNT_SUMMARY_END = "accountSummaryEnd"
    ACCOUNT_ERROR = "accountError"
    POSITION = "position"
    POSITION_END = "positionEnd"


_SLOT = {
    _Method.MARKET_DATA_TYPE: ReadSlot.QUOTE_SNAPSHOT, _Method.TICK_PRICE: ReadSlot.QUOTE_SNAPSHOT,
    _Method.TICK_SIZE: ReadSlot.QUOTE_SNAPSHOT, _Method.TICK_STRING: ReadSlot.QUOTE_SNAPSHOT,
    _Method.TICK_GENERIC: ReadSlot.QUOTE_SNAPSHOT, _Method.TICK_SNAPSHOT_END: ReadSlot.QUOTE_SNAPSHOT,
    _Method.QUOTE_ERROR: ReadSlot.QUOTE_SNAPSHOT, _Method.ACCOUNT_SUMMARY: ReadSlot.ACCOUNT_SUMMARY,
    _Method.ACCOUNT_SUMMARY_END: ReadSlot.ACCOUNT_SUMMARY, _Method.ACCOUNT_ERROR: ReadSlot.ACCOUNT_SUMMARY,
    _Method.POSITION: ReadSlot.POSITIONS, _Method.POSITION_END: ReadSlot.POSITIONS,
}
_END = frozenset({_Method.TICK_SNAPSHOT_END, _Method.ACCOUNT_SUMMARY_END, _Method.POSITION_END})
_ERROR = frozenset({_Method.QUOTE_ERROR, _Method.ACCOUNT_ERROR})


@dataclass(frozen=True)
class _Record:
    """One retained callback: a unique id, its fixed delivery and its copied arguments."""
    record_id: int
    method: _Method
    args: tuple

    @property
    def item(self) -> tuple[ReadSlot, CallbackKind]:
        kind = (CallbackKind.END if self.method in _END
                else CallbackKind.ERROR if self.method in _ERROR else CallbackKind.DATA)
        return _SLOT[self.method], kind

    def __repr__(self) -> str:
        return f"_Record({self.record_id}, {self.method.value})"


def _cost(args: tuple) -> int:
    cost = 0
    for arg in args:
        if isinstance(arg, _ContractSnapshot):
            cost += _cost(tuple(vars(arg).values()))
        else:
            cost += _FIELD_COST + (len(arg) if type(arg) is str else
                                   len(arg.as_tuple().digits) if type(arg) is Decimal else 0)
    return cost


@dataclass(frozen=True)
class RouterReport:
    """Fixed fields only: fault codes and counts, never a payload."""
    authority: str
    trading_permission: bool
    contract_details: str
    faults: tuple[str, ...]
    faults_dropped: int
    queued_records: int
    outstanding_fetch: bool
    adapter: AdapterReport
    completed: bool


class _Queue:
    """The queue the adapter consumes. It hands out one record's ``(slot, kind)`` at a time."""

    __slots__ = ("_router",)

    def __init__(self, router: CallbackRouter) -> None:
        self._router = router

    def get_nowait(self) -> tuple[ReadSlot, CallbackKind] | None:
        return self._router._fetch()

    def size(self) -> int:
        return self._router._outstanding()


class _Resources:
    __slots__ = ("connection", "queue", "reader")

    def __init__(self, connection: object, reader: object, queue: _Queue) -> None:
        self.connection, self.reader, self.queue = connection, reader, queue


class _Boundary:
    """Wraps the caller's client double: its connect, stop and disconnect hooks are used as they are,
    while the queue and ``dispatch`` belong to the router."""

    __slots__ = ("_client", "_router")

    def __init__(self, client: object, router: CallbackRouter) -> None:
        self._client, self._router = client, router

    def connect(self) -> _Resources:
        resources = self._client.connect()
        return _Resources(getattr(resources, "connection", None), getattr(resources, "reader", None),
                          self._router._queue)

    def stop_read(self, slot: ReadSlot) -> None:
        # The stop window: an applicable error arriving synchronously inside this hook is also
        # reported to the adapter, which settles the attempt UNKNOWN.
        router = self._router
        router._stopping = slot
        try:
            self._client.stop_read(slot)
        finally:
            router._stopping = None

    def disconnect(self) -> None:
        self._client.disconnect()

    def dispatch(self, slot: ReadSlot, kind: CallbackKind) -> None:
        self._router._dispatch(slot, kind)


def _request_id(value: object) -> int | None:
    return value if type(value) is int and 0 <= value <= MAX_ID else None


def _well_formed_session_error(copied: tuple) -> bool:
    """Every field the accepted normalizer requires of a session 1052 call, checked together: 4 or 5
    arguments, the exact integer id -1 (a numerically equal -1.0 or Decimal is malformed), an exact
    integer time and an exact non-negative integer code. The text arguments are never read."""
    if len(copied) not in (4, 5):
        return False
    req_id, error_time, error_code = copied[0], copied[1], copied[2]
    return (type(req_id) is int and req_id == SESSION_REQUEST_ID and type(error_time) is int
            and type(error_code) is int and error_code >= 0)


class CallbackRouter:
    """Routes invented callbacks for one connection. Single-threaded arrival is the supported model."""

    __slots__ = ("_account", "_adapter", "_arriving", "_bytes", "_clock", "_closed", "_contract",
                 "_dispatching", "_faults", "_faults_dropped", "_fetched", "_fifo", "_next_id",
                 "_positions", "_positions_opened", "_queue", "_quote", "_stopping")

    def __init__(self, client: object, lock: object, clock: object) -> None:
        self._queue = _Queue(self)
        self._adapter = LifecycleAdapter(_Boundary(client, self), lock)
        self._clock = clock
        self._contract: IbkrCallbackNormalizer | None = None
        self._quote: IbkrCallbackNormalizer | None = None
        self._account: AccountSummaryCallbacks | None = None
        self._positions: PositionSnapshotCallbacks | None = None
        self._positions_opened = False
        self._fifo: list[_Record] = []
        self._fetched: _Record | None = None
        self._bytes = 0
        self._next_id = 0
        self._faults: list[RouterFault] = []
        self._faults_dropped = 0
        self._arriving = False
        self._dispatching = False
        self._closed = False
        self._stopping: ReadSlot | None = None

    # ------------------------------------------------------------------ lifecycle and registration

    def start(self) -> None:
        self._adapter.start()

    def consume_one(self) -> bool:
        return self._adapter.consume_one()

    def shutdown(self, join_timeout: float) -> None:
        self._adapter.shutdown(join_timeout)
        self._closed = True

    def _fault(self, fault: RouterFault) -> None:
        if len(self._faults) < MAX_FAULTS:
            self._faults.append(fault)
        else:
            self._faults_dropped += 1

    def _registered_ids(self) -> set[int]:
        return {n.request_id for n in (self._contract, self._quote, self._account) if n is not None}

    def _can_register(self, request_id: object, existing: object) -> int | None:
        rid = _request_id(request_id)
        if self._closed or existing is not None or rid is None or rid in self._registered_ids():
            self._fault(RouterFault.REGISTRATION_REFUSED)
            return None
        if self._adapter.report().state != "OPEN":
            self._fault(RouterFault.ADAPTER_NOT_OPEN)
            return None
        return rid

    def register_contract(self, request_id: int) -> None:
        """Contract details, routed outside lifecycle accounting."""
        rid = self._can_register(request_id, self._contract)
        if rid is not None:
            self._contract = IbkrCallbackNormalizer(rid, CallbackRequest.CONTRACT_DETAILS)

    def register_quote(self, request_id: int, con_id: int) -> None:
        rid = self._can_register(request_id, self._quote)
        if rid is None:
            return
        try:
            self._quote = IbkrCallbackNormalizer(rid, CallbackRequest.QUOTE_SNAPSHOT, con_id=con_id,
                                                 clock=self._clock)
        except ValueError:
            self._fault(RouterFault.REGISTRATION_REFUSED)
            return
        self._adapter.open_read(ReadSlot.QUOTE_SNAPSHOT)

    def register_account_summary(self, request_id: int) -> None:
        rid = self._can_register(request_id, self._account)
        if rid is not None:
            self._account = AccountSummaryCallbacks(rid)
            self._adapter.open_read(ReadSlot.ACCOUNT_SUMMARY)

    def register_positions(self, snapshot_id: int) -> None:
        """The one positions window. It never reopens, even after it ends or closes."""
        if self._positions_opened:
            self._fault(RouterFault.POSITIONS_WINDOW_REOPENED)
            return
        rid = _request_id(snapshot_id)
        if self._closed or rid is None or self._adapter.report().state != "OPEN":
            self._fault(RouterFault.REGISTRATION_REFUSED)
            return
        self._positions_opened = True
        self._positions = PositionSnapshotCallbacks(rid)
        self._adapter.open_read(ReadSlot.POSITIONS)

    # ------------------------------------------------------------------ admission

    def _arrive(self, route: Any, *args: object) -> None:
        """One guard around a whole arrival: snapshotting, direct routes and admission. An arrival while
        another arrival or a dispatch is in progress is refused and delivers nothing."""
        if self._closed:
            self._fault(RouterFault.ARRIVAL_AFTER_CLOSE)
            return
        if self._arriving or self._dispatching:
            self._fault(RouterFault.ARRIVAL_REENTERED)
            return
        self._arriving = True
        try:
            route(*args)
        finally:
            self._arriving = False

    def _admit(self, method: _Method, args: tuple) -> None:
        """Retain and admit as one step: a record is queued only if the adapter admitted it."""
        cost = _cost(args)
        if len(self._fifo) >= MAX_QUEUE_RECORDS or self._bytes + cost > MAX_QUEUE_BYTES:
            self._fault(RouterFault.QUEUE_OVERFLOW)
            return
        record = _Record(self._next_id, method, args)
        self._next_id += 1
        slot, kind = record.item
        before = self._adapter.report().ledger.queued_callbacks
        self._adapter.on_enqueued(slot, kind)
        if self._adapter.report().ledger.queued_callbacks != before + 1:
            self._fault(RouterFault.ADMISSION_REFUSED)  # nothing dispatchable is retained
            return
        self._fifo.append(record)
        self._bytes += cost

    # ------------------------------------------------------------------ the adapter's consumer side

    def _outstanding(self) -> int:
        return len(self._fifo) + (self._fetched is not None)

    def _fetch(self) -> tuple[ReadSlot, CallbackKind] | None:
        """One fetched record at a time. A fetch while one is outstanding is refused."""
        if self._fetched is not None:
            self._fault(RouterFault.FETCH_WHILE_OUTSTANDING)
            return None
        if not self._fifo:
            return None
        self._fetched = self._fifo.pop(0)
        self._bytes -= _cost(self._fetched.args)
        return self._fetched.item

    def _dispatch(self, slot: ReadSlot, kind: CallbackKind) -> None:
        """Deliver only the one fetched record, once."""
        if self._dispatching or self._arriving:
            self._fault(RouterFault.DISPATCH_REENTERED)  # the record stays outstanding
            return
        record = self._fetched
        if record is None:
            self._fault(RouterFault.DISPATCH_WITHOUT_FETCH)
            return
        if (slot, kind) != record.item:
            self._fault(RouterFault.DISPATCH_MISMATCH)  # stays outstanding, so cleanup cannot pass
            return
        self._fetched = None
        self._dispatching = True
        try:
            self._deliver(record)
        finally:
            self._dispatching = False

    def _deliver(self, record: _Record) -> None:
        m, a = record.method, record.args
        q, acct, pos = self._quote, self._account, self._positions
        if m is _Method.MARKET_DATA_TYPE:
            q.marketDataType(*a)
        elif m is _Method.TICK_PRICE:
            q.tickPrice(*a)
        elif m is _Method.TICK_SIZE:
            q.tickSize(*a)
        elif m is _Method.TICK_STRING:
            q.tickString(*a)
        elif m is _Method.TICK_GENERIC:
            q.tickGeneric(*a)
        elif m is _Method.TICK_SNAPSHOT_END:
            q.tickSnapshotEnd(*a)
        elif m is _Method.QUOTE_ERROR:
            q.error_1052(*a)
        elif m is _Method.ACCOUNT_SUMMARY:
            acct.accountSummary(*a)
        elif m is _Method.ACCOUNT_SUMMARY_END:
            acct.accountSummaryEnd(*a)
        elif m is _Method.ACCOUNT_ERROR:
            acct.unsupported_callback("error")
        elif m is _Method.POSITION:
            pos.position(*a)
        else:
            pos.positionEnd()

    # ------------------------------------------------------------------ EWrapper-named arrivals

    def contractDetails(self, reqId: object, contractDetails: object) -> None:
        self._arrive(self._direct_contract, reqId, lambda n, rid: n.contractDetails(rid, _details(contractDetails)))

    def contractDetailsEnd(self, reqId: object) -> None:
        self._arrive(self._direct_contract, reqId, lambda n, rid: n.contractDetailsEnd(rid))

    def _direct_contract(self, req_id: object, deliver: Any) -> None:
        """Outside lifecycle accounting. Another id keeps its own value and is a recorded stray."""
        if self._contract is None:
            self._fault(RouterFault.UNROUTABLE_CALLBACK)
            return
        rid = _value(req_id)
        if rid != self._contract.request_id:
            self._fault(RouterFault.UNKNOWN_REQUEST_ID)
        deliver(self._contract, rid)

    def _quote_arrival(self, method: _Method, req_id: object, *rest: object) -> None:
        self._arrive(self._route_quote, method, req_id, *rest)

    def _route_quote(self, method: _Method, req_id: object, *rest: object) -> None:
        if self._quote is None:
            self._fault(RouterFault.UNROUTABLE_CALLBACK)
            return
        args = (_value(req_id), *(_value(v) for v in rest))
        if args[0] == self._quote.request_id:
            self._admit(method, args)
            return
        # Another id: kept as its own stray in the quote stream, outside lifecycle accounting.
        self._fault(RouterFault.UNKNOWN_REQUEST_ID)
        self._quote_stray(method, args)

    def _quote_stray(self, method: _Method, args: tuple) -> None:
        q = self._quote
        if method is _Method.MARKET_DATA_TYPE:
            q.marketDataType(*args)
        elif method is _Method.TICK_PRICE:
            q.tickPrice(*args)
        elif method is _Method.TICK_SIZE:
            q.tickSize(*args)
        elif method is _Method.TICK_STRING:
            q.tickString(*args)
        elif method is _Method.TICK_GENERIC:
            q.tickGeneric(*args)
        else:
            q.tickSnapshotEnd(*args)

    def marketDataType(self, reqId: object, marketDataType: object) -> None:
        self._quote_arrival(_Method.MARKET_DATA_TYPE, reqId, marketDataType)

    def tickPrice(self, reqId: object, tickType: object, price: object, attrib: object) -> None:
        self._quote_arrival(_Method.TICK_PRICE, reqId, tickType, price, None)  # attrib is not retained

    def tickSize(self, reqId: object, tickType: object, size: object) -> None:
        self._quote_arrival(_Method.TICK_SIZE, reqId, tickType, size)

    def tickString(self, reqId: object, tickType: object, value: object) -> None:
        self._quote_arrival(_Method.TICK_STRING, reqId, tickType, value)

    def tickGeneric(self, reqId: object, tickType: object, value: object) -> None:
        self._quote_arrival(_Method.TICK_GENERIC, reqId, tickType, value)

    def tickSnapshotEnd(self, reqId: object) -> None:
        self._quote_arrival(_Method.TICK_SNAPSHOT_END, reqId)

    def accountSummary(self, reqId: object, account: object, tag: object, value: object,
                       currency: object) -> None:
        self._account_arrival(_Method.ACCOUNT_SUMMARY, reqId, account, tag, value, currency)

    def accountSummaryEnd(self, reqId: object) -> None:
        self._account_arrival(_Method.ACCOUNT_SUMMARY_END, reqId)

    def _account_arrival(self, method: _Method, req_id: object, *rest: object) -> None:
        self._arrive(self._route_account, method, req_id, *rest)

    def _route_account(self, method: _Method, req_id: object, *rest: object) -> None:
        if self._account is None:
            self._fault(RouterFault.UNROUTABLE_CALLBACK)
            return
        args = (_value(req_id), *(_value(v) for v in rest))
        if args[0] == self._account.request_id:
            self._admit(method, args)
            return
        self._fault(RouterFault.UNKNOWN_REQUEST_ID)
        if method is _Method.ACCOUNT_SUMMARY:
            self._account.accountSummary(*args)  # its own stray record, original id kept
        else:
            self._account.accountSummaryEnd(*args)

    def position(self, account: object, contract: object, pos: object, avgCost: object) -> None:
        self._arrive(self._route_position, account, contract, pos, avgCost)

    def _route_position(self, account: object, contract: object, pos: object, avg_cost: object) -> None:
        if self._positions is None:
            self._fault(RouterFault.POSITIONS_WINDOW_ABSENT)
            return
        self._admit(_Method.POSITION, (_value(account), _contract(contract), _value(pos), _value(avg_cost)))

    def positionEnd(self) -> None:
        self._arrive(self._route_position_end)

    def _route_position_end(self) -> None:
        if self._positions is None:
            self._fault(RouterFault.POSITIONS_WINDOW_ABSENT)
            return
        self._admit(_Method.POSITION_END, ())

    def error_1052(self, *args: object) -> None:
        """The SDK 1052 error order. Routed by its request id under fixed rules; text is never kept."""
        self._arrive(self._route_error, *args)

    def _route_error(self, *args: object) -> None:
        copied = tuple(_value(v) for v in args[:3]) + tuple(_MALFORMED for _ in args[3:])
        rid = copied[0] if copied else _MALFORMED
        if self._stopping is not None and self._stop_error_applies(rid, copied):
            self._adapter.on_direct_callback(DirectCallback.ERROR)  # in addition to the routing below
        if self._quote is not None and rid == self._quote.request_id:
            self._admit(_Method.QUOTE_ERROR, copied)
        elif self._account is not None and rid == self._account.request_id:
            self._admit(_Method.ACCOUNT_ERROR, ())  # a fixed refusal; account streams have no error_1052
        elif self._contract is not None and rid == self._contract.request_id:
            self._contract.error_1052(*copied)  # outside lifecycle accounting
        elif rid == SESSION_REQUEST_ID:
            self._session_error(copied)
        else:
            self._fault(RouterFault.UNKNOWN_REQUEST_ID)
            # A valid unknown id goes to the one fixed stray sink, the contract stream, with its original
            # id. An id that is not a valid request id cannot be attributed, so it latches both
            # error_1052 streams.
            sinks = (self._contract,) if _request_id(rid) is not None else (self._contract, self._quote)
            for normalizer in sinks:
                if normalizer is not None:
                    normalizer.error_1052(*copied)

    def _stop_error_applies(self, rid: object, copied: tuple) -> bool:
        """Whether an error arriving inside the stop hook bears on that stop: an error for the stopped
        read's own request, or a session error that is not an exempt well-formed OK code. Errors for
        another request and informational session codes do not."""
        stopping = self._stopping
        if stopping is ReadSlot.QUOTE_SNAPSHOT and self._quote is not None and rid == self._quote.request_id:
            return True
        if stopping is ReadSlot.ACCOUNT_SUMMARY and self._account is not None and rid == self._account.request_id:
            return True
        if rid == SESSION_REQUEST_ID:
            return not (_well_formed_session_error(copied) and copied[2] in SESSION_OK_CODES)
        return False

    def _session_error(self, copied: tuple) -> None:
        """A session-level error reaches every stream directly. Only a well-formed 1052 call with an OK
        code is exempt; a malformed call or a non-OK code fails each stream."""
        for normalizer in (self._contract, self._quote):
            if normalizer is not None:
                normalizer.error_1052(*copied)
        well_formed = _well_formed_session_error(copied)
        if well_formed and copied[2] in SESSION_OK_CODES:
            return
        self._fault(RouterFault.SESSION_ERROR if well_formed else RouterFault.SESSION_ERROR_MALFORMED)
        for stream in (self._account, self._positions):
            if stream is not None:
                stream.unsupported_callback("error")

    def connectAck(self) -> None:
        """Recorded by the adapter as a direct callback. An observation only: never socket state,
        session validity or broker acknowledgement."""
        self._arrive(self._adapter.on_direct_callback, DirectCallback.CONNECT_ACK)

    def connectionClosed(self) -> None:
        """Recorded by the adapter as a direct callback. An observation only: the socket is observed
        separately at shutdown, and this never shows closure or broker-side retirement."""
        self._arrive(self._adapter.on_direct_callback, DirectCallback.CONNECTION_CLOSED)

    def unsupported_callback(self, name: object, reqId: object = None) -> None:
        """Any other callback, including excluded protobuf variants. Always a router fault, so it blocks
        completion even before any stream registers. Also routed to its owner when the id is registered,
        otherwise to every registered stream. The name is never retained."""
        self._arrive(self._route_unsupported, name, reqId)

    def _route_unsupported(self, name: object, req_id: object) -> None:
        self._fault(RouterFault.UNSUPPORTED_CALLBACK)
        label = name if type(name) is str and len(name) <= 64 else "INVALID_NAME"
        owners = [n for n in (self._contract, self._quote, self._account)
                  if n is not None and req_id == n.request_id]
        if not owners:
            owners = [n for n in (self._contract, self._quote, self._account, self._positions) if n is not None]
        for normalizer in owners:
            normalizer.unsupported_callback(label)

    # ------------------------------------------------------------------ outputs

    def contract_envelopes(self) -> list[ResponseEnvelope]:
        """Contract details: outside lifecycle accounting."""
        return [] if self._contract is None else self._contract.envelopes()

    def quote_envelopes(self) -> list[ResponseEnvelope]:
        return [] if self._quote is None else self._quote.envelopes()

    def account_summary_envelopes(self) -> list[ResponseEnvelope]:
        return [] if self._account is None else self._account.envelopes()

    def position_envelopes(self) -> list[ResponseEnvelope]:
        return [] if self._positions is None else self._positions.envelopes()

    def report(self) -> RouterReport:
        adapter = self._adapter.report()
        faults = list(self._faults)
        if self._closed and self._fetched is not None and RouterFault.FETCHED_NOT_DISPATCHED not in faults:
            faults.append(RouterFault.FETCHED_NOT_DISPATCHED)
        completed = (self._closed and adapter.completed and not faults and not self._faults_dropped
                     and self._outstanding() == 0)
        return RouterReport(
            authority="NON_AUTHORITATIVE", trading_permission=False,
            contract_details="OUTSIDE_LIFECYCLE_ACCOUNTING", faults=tuple(f.value for f in faults),
            faults_dropped=self._faults_dropped, queued_records=len(self._fifo),
            outstanding_fetch=self._fetched is not None, adapter=adapter, completed=completed,
        )

    def __repr__(self) -> str:
        return f"CallbackRouter(queued={len(self._fifo)}, faults={len(self._faults)}, closed={self._closed})"

    def __reduce_ex__(self, protocol: object) -> object:
        raise TypeError("a CallbackRouter holds live routing state and cannot be serialized")
