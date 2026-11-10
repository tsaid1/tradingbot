"""Single-use, SDK-free lifecycle adapter over the merged ``RetirementLedger``. NON_AUTHORITATIVE.

``LifecycleAdapter`` drives one ``RetirementLedger`` from observations of one local connection made
through a fixed mock-only ``LifecycleBoundary``. It imports no SDK, opens no socket and starts no
thread. The boundary and its resources are invented doubles today; real SDK integration is a later,
separately reviewed task, and nothing here claims compatibility with any SDK.

Rules:

* single use: one ``start`` and one ``shutdown``; no recovery, retry or reuse
* the connection, reader and queue references are retained the moment ``connect`` returns, so a later
  reset or callback inside the boundary cannot erase what must be observed at shutdown
* every retirement attempt is reserved in the ledger before its hook runs, and settles once
* the adapter's lock is never held while a hook or observation runs, and every such call counts as
  pending until it returns, so a blocked hook keeps the adapter incomplete
* a normally returned or skipped (disconnected) stop is never success, and an error callback during
  a stop makes that attempt UNKNOWN
* admission closes after disconnect; later enqueues are ledger faults. Read callbacks delivered
  outside the consumer flow are recorded faults, never applied. ``CONNECTION_CLOSED`` is not evidence
* a bounded join only bounds the wait. A live reader, unresolved or unknown partial bytes, a busy
  consumer, a non-empty queue or an unclosed socket all keep the adapter incomplete
* each owned resource is settled even if an earlier step raised; failures are fixed codes, and no
  exception text is kept

``completed`` means local completion only. Broker-side retirement stays
``NOT_ESTABLISHED_BY_INSPECTED_SOURCES``, and nothing here is PAPER-READ-1 acceptance or trading
permission. There is no hard total-time or termination guarantee.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Literal, Protocol

from tradingbot.paper_readiness.read_retirement import (
    CallbackKind,
    Component,
    Outcome,
    ReadSlot,
    RetirementLedger,
    RetirementReport,
)

__all__ = [
    "MAX_DRAIN",
    "MAX_FAILURES",
    "AdapterReport",
    "AdapterState",
    "DirectCallback",
    "Failure",
    "LifecycleAdapter",
    "LifecycleBoundary",
]

MAX_DRAIN = 1024
MAX_FAILURES = 64


class AdapterState(str, Enum):
    NOT_STARTED = "NOT_STARTED"
    STARTING = "STARTING"
    OPEN = "OPEN"
    SHUTTING_DOWN = "SHUTTING_DOWN"
    CLOSED = "CLOSED"
    START_FAILED = "START_FAILED"


class DirectCallback(str, Enum):
    """Callbacks that can arrive outside the consumer flow."""
    CONNECT_ACK = "CONNECT_ACK"
    CONNECTION_CLOSED = "CONNECTION_CLOSED"
    ERROR = "ERROR"
    READ = "READ"


class Failure(str, Enum):
    CONNECT_HOOK_RAISED = "CONNECT_HOOK_RAISED"
    RESOURCES_INVALID = "RESOURCES_INVALID"
    STOP_HOOK_RAISED = "STOP_HOOK_RAISED"
    STOP_WHILE_DISCONNECTED = "STOP_WHILE_DISCONNECTED"
    ERROR_DURING_STOP = "ERROR_DURING_STOP"
    DISCONNECT_HOOK_RAISED = "DISCONNECT_HOOK_RAISED"
    DISPATCH_HOOK_RAISED = "DISPATCH_HOOK_RAISED"
    OBSERVATION_RAISED = "OBSERVATION_RAISED"
    DIRECT_READ_CALLBACK = "DIRECT_READ_CALLBACK"
    DIRECT_ERROR = "DIRECT_ERROR"
    REUSE_REFUSED = "REUSE_REFUSED"
    READ_AFTER_SHUTDOWN = "READ_AFTER_SHUTDOWN"
    READ_BEFORE_OPEN = "READ_BEFORE_OPEN"
    ITEM_NOT_ADMITTED = "ITEM_NOT_ADMITTED"
    READER_ALIVE = "READER_ALIVE"
    PARTIAL_BYTES_UNRESOLVED = "PARTIAL_BYTES_UNRESOLVED"
    PARTIAL_BYTES_UNKNOWN = "PARTIAL_BYTES_UNKNOWN"
    QUEUE_NOT_EMPTY = "QUEUE_NOT_EMPTY"
    CONSUMER_BUSY = "CONSUMER_BUSY"
    SOCKET_NOT_CLOSED = "SOCKET_NOT_CLOSED"
    DRAIN_LIMIT = "DRAIN_LIMIT"


class LifecycleBoundary(Protocol):
    """The fixed mock-only boundary. ``connect`` returns an object with ``connection``, ``reader``
    and ``queue`` attributes:

    * ``connection.is_connected() -> bool | None`` and ``connection.socket_closed() -> bool | None``
    * ``reader.join(timeout) -> None``, ``reader.is_alive() -> bool`` and
      ``reader.partial_bytes() -> int | None`` (unread bytes it holds, None if unknown)
    * ``queue.get_nowait() -> (ReadSlot, CallbackKind) | None`` and ``queue.size() -> int | None``
    """

    def connect(self) -> Any: ...

    def stop_read(self, slot: ReadSlot) -> None: ...

    def disconnect(self) -> None: ...

    def dispatch(self, slot: ReadSlot, kind: CallbackKind) -> None: ...


class _Lock(Protocol):
    def __enter__(self) -> object: ...

    def __exit__(self, *exc: object) -> object: ...


def _same_item(item: object, admitted: tuple[ReadSlot, CallbackKind]) -> bool:
    return type(item) is tuple and len(item) == 2 and item[0] == admitted[0] and item[1] == admitted[1]


class _Raised(Exception):
    """An observation or hook raised. Its text is never kept."""


@dataclass(frozen=True)
class AdapterReport:
    authority: Literal["NON_AUTHORITATIVE"]
    trading_permission: Literal[False]
    paper_read_1: Literal["NOT_ASSESSED"]
    state: str
    pending_calls: int
    failures: tuple[str, ...]
    failures_dropped: int
    direct_callbacks: tuple[str, ...]
    reader_partial_bytes: str
    ledger: RetirementReport
    completed: bool


class LifecycleAdapter:
    """One connection, used once. All public methods are safe to call from boundary hooks."""

    def __init__(self, boundary: LifecycleBoundary, lock: _Lock) -> None:
        self._boundary = boundary
        self._lock = lock
        self._ledger = RetirementLedger()
        self._state = AdapterState.NOT_STARTED
        self._shutdown_requested = False
        self._connection: Any = None
        self._reader: Any = None
        self._queue: Any = None
        self._pending = 0
        self._dispatching = 0
        self._consumer_failed = False
        self._consumer_owned = False
        self._admitted: list[tuple[ReadSlot, CallbackKind]] = []
        self._deferred_timeout: float | None = None
        self._active_stop: ReadSlot | None = None
        self._stop_error = False
        self._failures: list[Failure] = []
        self._failures_dropped = 0
        self._direct: list[DirectCallback] = []
        self._partial_bytes = "NOT_OBSERVED"

    # ------------------------------------------------------------------ internals

    def _fail(self, failure: Failure) -> None:
        """Caller holds the lock."""
        if len(self._failures) < MAX_FAILURES:
            self._failures.append(failure)
        else:
            self._failures_dropped += 1

    def _call(self, function: Any, *args: Any) -> Any:
        """Run one hook or observation without the lock, counted as pending until it returns."""
        with self._lock:
            self._pending += 1
        try:
            return function(*args)
        except Exception:  # noqa: BLE001 - recorded as a fixed code by the caller; text is dropped
            raise _Raised from None
        finally:
            with self._lock:
                self._pending -= 1

    def _observe(self, function: Any, *args: Any) -> Any:
        try:
            return self._call(function, *args)
        except _Raised:
            with self._lock:
                self._fail(Failure.OBSERVATION_RAISED)
            return None

    # ------------------------------------------------------------------ start and reads

    def start(self) -> None:
        with self._lock:
            if self._state is not AdapterState.NOT_STARTED:
                self._fail(Failure.REUSE_REFUSED)
                return
            self._state = AdapterState.STARTING
        try:
            resources = self._call(self._boundary.connect)
        except _Raised:
            with self._lock:
                self._fail(Failure.CONNECT_HOOK_RAISED)
                self._state = AdapterState.START_FAILED
            return
        connection = getattr(resources, "connection", None)
        reader = getattr(resources, "reader", None)
        queue = getattr(resources, "queue", None)
        with self._lock:
            # Retained now: a later reset inside the boundary cannot erase these.
            self._connection, self._reader, self._queue = connection, reader, queue
            if connection is None or reader is None or queue is None:
                self._fail(Failure.RESOURCES_INVALID)
            self._state = AdapterState.OPEN
            deferred = self._deferred_timeout
        if deferred is not None:
            # A shutdown requested while connect was pending runs now, with the references retained.
            self._shutdown(deferred)

    def open_read(self, slot: ReadSlot) -> None:
        with self._lock:
            if self._shutdown_requested or self._state in (AdapterState.SHUTTING_DOWN,
                                                           AdapterState.CLOSED):
                self._fail(Failure.READ_AFTER_SHUTDOWN)
                return
            if self._state is not AdapterState.OPEN:
                self._fail(Failure.READ_BEFORE_OPEN)
                return
            self._ledger.open_read(slot)

    def on_enqueued(self, slot: ReadSlot, kind: CallbackKind) -> None:
        """The reader put one callback on the queue. After admission closes the ledger faults it."""
        item = (ReadSlot(slot), CallbackKind(kind))
        with self._lock:
            before = self._ledger.report().queued_callbacks
            self._ledger.callback_received(*item)
            if self._ledger.report().queued_callbacks == before + 1:
                self._admitted.append(item)  # the exact record a fetched item must match

    def on_direct_callback(self, callback: DirectCallback) -> None:
        """A callback outside the consumer flow. Recorded; a read callback here is never applied."""
        callback = DirectCallback(callback)
        with self._lock:
            if len(self._direct) < MAX_FAILURES:
                self._direct.append(callback)
            if callback is DirectCallback.READ:
                self._fail(Failure.DIRECT_READ_CALLBACK)
            elif callback is DirectCallback.ERROR:
                if self._active_stop is not None:
                    self._stop_error = True
                else:
                    self._fail(Failure.DIRECT_ERROR)

    def consume_one(self) -> bool:
        """The adapter-owned consumer: take one queued callback and dispatch it. False if none.

        One logical owner at a time: a competing or recursive call returns False before it removes
        anything. The fetched item must equal the oldest admitted record, or it is not dispatched."""
        with self._lock:
            if self._queue is None or self._consumer_failed or self._consumer_owned:
                return False
            self._consumer_owned = True
            queue = self._queue
        try:
            return self._consume(queue)
        finally:
            with self._lock:
                self._consumer_owned = False

    def _consume(self, queue: Any) -> bool:
        item = self._observe(queue.get_nowait)
        if item is None:
            return False
        with self._lock:
            if not self._admitted or not _same_item(item, self._admitted[0]):
                self._fail(Failure.ITEM_NOT_ADMITTED)  # removed from the queue, never dispatched
                return True
            slot, kind = self._admitted.pop(0)
            self._ledger.callback_started()
            self._dispatching += 1
        try:
            self._call(self._boundary.dispatch, slot, kind)
        except _Raised:
            with self._lock:
                self._dispatching -= 1
                self._consumer_failed = True  # stays in flight; the queue keeps what is left
                self._fail(Failure.DISPATCH_HOOK_RAISED)
            return False
        with self._lock:
            self._dispatching -= 1
            self._ledger.callback_finished()
        return True

    # ------------------------------------------------------------------ shutdown

    def shutdown(self, join_timeout: float) -> None:
        """Retire reads, disconnect, close admission, drain, then observe each resource. Once only.

        Requested while ``connect`` is still pending, it is deferred, never blocking, and runs as
        soon as ``start`` has retained the returned references."""
        with self._lock:
            if self._state is AdapterState.START_FAILED:
                return  # nothing was acquired; the report already shows the failed start
            if self._shutdown_requested or self._state not in (AdapterState.STARTING,
                                                               AdapterState.OPEN):
                self._fail(Failure.REUSE_REFUSED)
                return
            self._shutdown_requested = True
            if self._state is AdapterState.STARTING:
                self._deferred_timeout = join_timeout
                return
        self._shutdown(join_timeout)

    def _shutdown(self, join_timeout: float) -> None:
        with self._lock:
            self._state = AdapterState.SHUTTING_DOWN
            ledger_view = self._ledger.report()
        for slot_view in ledger_view.slots:
            slot = ReadSlot(slot_view.slot)
            if slot is ReadSlot.QUOTE_SNAPSHOT and slot_view.end_marker_seen:
                continue  # its END suffices locally; no attempt is made
            self._stop(slot)
        self._disconnect()
        with self._lock:
            self._ledger.close_admission()
        self._drain()
        self._observe_reader(join_timeout)
        self._observe_socket()
        self._observe_consumer()
        with self._lock:
            self._ledger.seal()
            self._state = AdapterState.CLOSED

    def _stop(self, slot: ReadSlot) -> None:
        with self._lock:
            self._ledger.reserve_attempt(slot)
            connection = self._connection
        connected = self._observe(connection.is_connected) if connection is not None else None
        if connected is not True:
            with self._lock:
                self._fail(Failure.STOP_WHILE_DISCONNECTED)
                self._ledger.settle_attempt(slot, Outcome.UNKNOWN)
            return
        with self._lock:
            self._active_stop, self._stop_error = slot, False
        try:
            self._call(self._boundary.stop_read, slot)
            outcome = Outcome.RETURNED
        except _Raised:
            outcome = Outcome.FAILED
        with self._lock:
            if outcome is Outcome.FAILED:
                self._fail(Failure.STOP_HOOK_RAISED)
            elif self._stop_error:
                self._fail(Failure.ERROR_DURING_STOP)
                outcome = Outcome.UNKNOWN
            self._active_stop = None
            self._ledger.settle_attempt(slot, outcome)

    def _disconnect(self) -> None:
        with self._lock:
            self._ledger.reserve_disconnect()
        try:
            self._call(self._boundary.disconnect)
            outcome = Outcome.RETURNED
        except _Raised:
            outcome = Outcome.FAILED
        with self._lock:
            if outcome is Outcome.FAILED:
                self._fail(Failure.DISCONNECT_HOOK_RAISED)
            self._ledger.settle_disconnect(outcome)

    def _drain(self) -> None:
        for _ in range(MAX_DRAIN):
            if not self.consume_one():
                return
        with self._lock:
            self._fail(Failure.DRAIN_LIMIT)

    def _observe_reader(self, join_timeout: float) -> None:
        reader = self._reader
        outcome = Outcome.UNKNOWN
        if reader is not None:
            self._observe(reader.join, join_timeout)  # bounds only this wait
            alive = self._observe(reader.is_alive)
            partial = self._observe(reader.partial_bytes) if alive is False else None
            with self._lock:
                if alive is not False:
                    self._fail(Failure.READER_ALIVE)
                elif type(partial) is int and partial == 0:
                    self._partial_bytes = "NONE"
                    outcome = Outcome.STOPPED
                elif type(partial) is int and partial > 0:
                    self._partial_bytes = "UNRESOLVED"
                    self._fail(Failure.PARTIAL_BYTES_UNRESOLVED)
                    outcome = Outcome.FAILED
                else:
                    self._partial_bytes = "UNKNOWN"
                    self._fail(Failure.PARTIAL_BYTES_UNKNOWN)
        with self._lock:
            self._ledger.component_stopped(Component.READER, outcome)

    def _observe_socket(self) -> None:
        connection = self._connection
        closed = self._observe(connection.socket_closed) if connection is not None else None
        with self._lock:
            if closed is True:
                outcome = Outcome.STOPPED
            else:
                self._fail(Failure.SOCKET_NOT_CLOSED)
                outcome = Outcome.FAILED if closed is False else Outcome.UNKNOWN
            self._ledger.component_stopped(Component.SOCKET, outcome)

    def _observe_consumer(self) -> None:
        queue = self._queue
        size = self._observe(queue.size) if queue is not None else None
        with self._lock:
            if self._dispatching:
                self._fail(Failure.CONSUMER_BUSY)
                outcome = Outcome.UNKNOWN
            elif size != 0:
                self._fail(Failure.QUEUE_NOT_EMPTY)
                outcome = Outcome.UNKNOWN
            else:
                outcome = Outcome.STOPPED
            self._ledger.component_stopped(Component.CONSUMER, outcome)

    # ------------------------------------------------------------------ report

    def report(self) -> AdapterReport:
        with self._lock:
            ledger = self._ledger.report()
            completed = (
                self._state is AdapterState.CLOSED
                and self._pending == 0
                and not self._failures
                and not self._failures_dropped
                and ledger.local_quiescence_established
            )
            return AdapterReport(
                authority="NON_AUTHORITATIVE", trading_permission=False, paper_read_1="NOT_ASSESSED",
                state=self._state.value, pending_calls=self._pending,
                failures=tuple(f.value for f in self._failures), failures_dropped=self._failures_dropped,
                direct_callbacks=tuple(d.value for d in self._direct),
                reader_partial_bytes=self._partial_bytes, ledger=ledger, completed=completed,
            )
