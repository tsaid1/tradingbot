"""Single-use lifecycle adapter over the merged RetirementLedger. Invented doubles only; no SDK.

Paired regressions for the known lifecycle hazards (blocked and raising hooks, callbacks outside the
consumer flow, late enqueue and post-END events, reset-erased references, unknown partial bytes)
next to a returning control, all through the adapter and the existing ledger.
"""
from __future__ import annotations

import ast
import threading
from pathlib import Path

import pytest

from tradingbot.paper_readiness import sdk_lifecycle as sl
from tradingbot.paper_readiness.read_retirement import CallbackKind, ReadSlot
from tradingbot.paper_readiness.sdk_lifecycle import DirectCallback, LifecycleAdapter
from tradingbot.paper_readiness.transport import FORBIDDEN_NAME_RE

SUMMARY, POSITIONS, SNAPSHOT = ReadSlot.ACCOUNT_SUMMARY, ReadSlot.POSITIONS, ReadSlot.QUOTE_SNAPSHOT
DATA, END = CallbackKind.DATA, CallbackKind.END
CANARY = "CANARY-EXCEPTION-TEXT"


class RecordingLock:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.held = False

    def __enter__(self):
        self._lock.acquire()
        self.held = True
        return self

    def __exit__(self, *exc):
        self.held = False
        self._lock.release()


class Connection:
    def __init__(self) -> None:
        self.connected = True
        self.closed = False

    def is_connected(self):
        return self.connected

    def socket_closed(self):
        return self.closed


class Reader:
    def __init__(self) -> None:
        self.alive = True
        self.stops_on_join = True
        self.partial = 0
        self.on_join = None

    def join(self, timeout):
        if self.on_join is not None:
            self.on_join()
        if self.stops_on_join:
            self.alive = False

    def is_alive(self):
        return self.alive

    def partial_bytes(self):
        return self.partial


class Queue:
    def __init__(self) -> None:
        self.items: list = []

    def get_nowait(self):
        return self.items.pop(0) if self.items else None

    def size(self):
        return len(self.items)


class Resources:
    def __init__(self) -> None:
        self.connection, self.reader, self.queue = Connection(), Reader(), Queue()


class Boundary:
    """Invented client double. ``reset_on_disconnect`` erases its own references, as a reset would."""

    def __init__(self, lock: RecordingLock) -> None:
        self.lock = lock
        self.resources = Resources()
        self.adapter: LifecycleAdapter | None = None
        self.calls: list = []
        self.lock_held_in_hooks: list[bool] = []
        self.stop_behaviour: dict = {}
        self.disconnect_raises = False
        self.dispatch_raises = False
        self.reset_on_disconnect = False
        self.closed_on_disconnect = True

    def _enter(self, name, *args):
        self.lock_held_in_hooks.append(self.lock.held)
        self.calls.append((name, *args))

    def connect(self):
        self._enter("connect")
        self.adapter.on_direct_callback(DirectCallback.CONNECT_ACK)
        return self.resources

    def stop_read(self, slot):
        self._enter("stop_read", slot)
        behaviour = self.stop_behaviour.get(slot)
        if behaviour is not None:
            behaviour()

    def disconnect(self):
        self._enter("disconnect")
        connection = self.resources.connection
        connection.connected = False
        connection.closed = self.closed_on_disconnect
        self.adapter.on_direct_callback(DirectCallback.CONNECTION_CLOSED)
        if self.reset_on_disconnect:
            self.resources.connection = self.resources.reader = self.resources.queue = None
        if self.disconnect_raises:
            raise RuntimeError(CANARY)

    def dispatch(self, slot, kind):
        self._enter("dispatch", slot, kind)
        if self.dispatch_raises:
            raise RuntimeError(CANARY)


def harness():
    lock = RecordingLock()
    boundary = Boundary(lock)
    adapter = LifecycleAdapter(boundary, lock)
    boundary.adapter = adapter
    adapter.start()
    resources = boundary.resources  # the test keeps its own handle, as the adapter must
    return adapter, boundary, resources


def enqueue(adapter, resources, slot, *kinds):
    for kind in kinds:
        adapter.on_enqueued(slot, kind)
        resources.queue.items.append((slot, kind))


def opened(adapter, resources, *, snapshot_end=True):
    for slot in (SUMMARY, POSITIONS, SNAPSHOT):
        adapter.open_read(slot)
    enqueue(adapter, resources, SUMMARY, DATA, END)
    enqueue(adapter, resources, POSITIONS, DATA, END)
    enqueue(adapter, resources, SNAPSHOT, *((DATA, END) if snapshot_end else (DATA,)))
    while adapter.consume_one():
        pass


def stops(boundary):
    return [call[1] for call in boundary.calls if call[0] == "stop_read"]


# ----------------------------------------------------------------------------- returning control

def test_returning_control_completes_only_after_observed_local_cessation():
    adapter, boundary, resources = harness()
    opened(adapter, resources)
    assert adapter.report().completed is False  # open: nothing has ceased
    adapter.shutdown(join_timeout=0.01)
    report = adapter.report()
    assert report.completed is True and report.failures == () and report.pending_calls == 0
    assert report.ledger.local_quiescence_established is True
    assert report.ledger.broker_side_retirement == "NOT_ESTABLISHED_BY_INSPECTED_SOURCES"
    assert (report.trading_permission, report.paper_read_1) == (False, "NOT_ASSESSED")
    assert stops(boundary) == [SUMMARY, POSITIONS]  # the ended snapshot needs no attempt
    assert report.reader_partial_bytes == "NONE"
    assert report.direct_callbacks == ("CONNECT_ACK", "CONNECTION_CLOSED")
    assert boundary.lock_held_in_hooks and not any(boundary.lock_held_in_hooks)


def test_connection_closed_callback_is_not_socket_cessation():
    adapter, boundary, resources = harness()
    boundary.closed_on_disconnect = False
    opened(adapter, resources)
    adapter.shutdown(join_timeout=0.01)
    report = adapter.report()
    assert "CONNECTION_CLOSED" in report.direct_callbacks
    assert report.ledger.socket == "FAILED" and report.completed is False


# ----------------------------------------------------------------------------- blocked and raising hooks

def test_a_blocked_stop_hook_keeps_the_adapter_pending_until_it_returns():
    adapter, boundary, resources = harness()
    opened(adapter, resources)
    entered, release = threading.Event(), threading.Event()

    def block():
        entered.set()
        assert release.wait(5)

    boundary.stop_behaviour[SUMMARY] = block
    worker = threading.Thread(target=adapter.shutdown, args=(0.01,))
    worker.start()
    assert entered.wait(5)
    blocked = adapter.report()  # the lock is free, so the report is readable while the hook blocks
    assert blocked.pending_calls == 1 and blocked.state == "SHUTTING_DOWN"
    assert blocked.completed is False
    summary = next(s for s in blocked.ledger.slots if s.slot == "ACCOUNT_SUMMARY")
    assert summary.retirement_attempt == "RESERVED"  # reserved before the hook ran
    release.set()
    worker.join(5)
    assert not worker.is_alive() and adapter.report().completed is True


def test_a_raising_stop_hook_is_retained_once_and_other_resources_still_settle():
    adapter, boundary, resources = harness()
    opened(adapter, resources)

    def fail():
        raise RuntimeError(CANARY)

    boundary.stop_behaviour[SUMMARY] = fail
    adapter.shutdown(join_timeout=0.01)
    report = adapter.report()
    summary = next(s for s in report.ledger.slots if s.slot == "ACCOUNT_SUMMARY")
    assert summary.retirement_attempt == "FAILED" and "STOP_HOOK_RAISED" in report.failures
    assert stops(boundary) == [SUMMARY, POSITIONS]  # no retry
    assert ("disconnect",) in boundary.calls and report.ledger.reader == "STOPPED"
    assert report.completed is False and CANARY not in repr(report)


def test_a_raising_disconnect_still_observes_reader_and_socket():
    adapter, boundary, resources = harness()
    boundary.disconnect_raises = True
    opened(adapter, resources)
    adapter.shutdown(join_timeout=0.01)
    report = adapter.report()
    assert report.ledger.disconnect_attempt == "FAILED" and "DISCONNECT_HOOK_RAISED" in report.failures
    assert (report.ledger.reader, report.ledger.socket) == ("STOPPED", "STOPPED")
    assert report.completed is False and CANARY not in repr(report)


def test_a_raising_dispatch_keeps_its_callback_in_flight_and_the_rest_queued():
    adapter, boundary, resources = harness()
    for slot in (SUMMARY, POSITIONS):
        adapter.open_read(slot)
    boundary.dispatch_raises = True
    enqueue(adapter, resources, SUMMARY, DATA, END)
    assert adapter.consume_one() is False and adapter.consume_one() is False
    adapter.shutdown(join_timeout=0.01)
    report = adapter.report()
    assert "DISPATCH_HOOK_RAISED" in report.failures and "QUEUE_NOT_EMPTY" in report.failures
    assert report.ledger.in_flight_callbacks == 1 and report.ledger.queued_callbacks == 1
    assert report.completed is False


def test_a_stop_while_disconnected_is_unknown_and_its_hook_never_runs():
    adapter, boundary, resources = harness()
    opened(adapter, resources)
    resources.connection.connected = False
    adapter.shutdown(join_timeout=0.01)
    report = adapter.report()
    assert stops(boundary) == []
    assert {s.retirement_attempt for s in report.ledger.slots if s.slot != "QUOTE_SNAPSHOT"} == {"UNKNOWN"}
    assert "STOP_WHILE_DISCONNECTED" in report.failures and report.completed is False


# ----------------------------------------------------------------------------- callbacks outside the flow

def test_a_direct_read_callback_is_recorded_and_never_applied():
    adapter, _boundary, _resources = harness()
    adapter.open_read(SNAPSHOT)
    adapter.on_direct_callback(DirectCallback.READ)  # an END delivered outside the consumer
    adapter.shutdown(join_timeout=0.01)
    report = adapter.report()
    assert "DIRECT_READ_CALLBACK" in report.failures
    assert report.ledger.slots[0].end_marker_seen is False and report.completed is False


def test_an_error_callback_during_a_returning_stop_makes_it_unknown():
    adapter, boundary, resources = harness()
    opened(adapter, resources)
    boundary.stop_behaviour[SUMMARY] = lambda: adapter.on_direct_callback(DirectCallback.ERROR)
    adapter.shutdown(join_timeout=0.01)
    report = adapter.report()
    summary = next(s for s in report.ledger.slots if s.slot == "ACCOUNT_SUMMARY")
    assert summary.retirement_attempt == "UNKNOWN" and "ERROR_DURING_STOP" in report.failures
    assert report.completed is False


def test_a_queued_item_never_admitted_is_not_dispatched_silently():
    adapter, boundary, resources = harness()
    adapter.open_read(SUMMARY)
    resources.queue.items.append((SUMMARY, DATA))  # enqueued without notifying the adapter
    adapter.consume_one()
    report = adapter.report()
    assert ("dispatch", SUMMARY, DATA) not in boundary.calls
    assert "ITEM_NOT_ADMITTED" in report.failures


# ----------------------------------------------------------------------------- late and post-END work

def test_an_enqueue_after_admission_closes_blocks_completion():
    adapter, _boundary, resources = harness()
    opened(adapter, resources)
    resources.reader.on_join = lambda: enqueue(adapter, resources, SUMMARY, DATA)
    adapter.shutdown(join_timeout=0.01)
    report = adapter.report()
    assert "CALLBACK_AFTER_ADMISSION_CLOSED" in report.ledger.faults
    assert report.completed is False


def test_data_after_end_is_a_post_end_fault():
    adapter, _boundary, resources = harness()
    opened(adapter, resources)
    enqueue(adapter, resources, SUMMARY, DATA)
    adapter.consume_one()
    adapter.shutdown(join_timeout=0.01)
    assert "POST_END_EVENT" in adapter.report().ledger.faults
    assert adapter.report().completed is False


def test_a_snapshot_without_end_gets_a_modelled_attempt_and_stays_incomplete():
    adapter, boundary, resources = harness()
    opened(adapter, resources, snapshot_end=False)
    adapter.shutdown(join_timeout=0.01)
    report = adapter.report()
    assert SNAPSHOT in stops(boundary) and report.completed is False
    assert next(s for s in report.ledger.slots if s.slot == "QUOTE_SNAPSHOT").read_complete is False


# ----------------------------------------------------------------------------- retained references and bytes

@pytest.mark.parametrize("reader_stops", [True, False], ids=["reader-stopped", "reader-alive"])
def test_references_retained_at_connect_survive_a_reset_in_disconnect(reader_stops):
    adapter, boundary, resources = harness()
    boundary.reset_on_disconnect = True
    resources.reader.stops_on_join = reader_stops
    opened(adapter, resources)
    adapter.shutdown(join_timeout=0.01)
    report = adapter.report()
    assert boundary.resources.reader is None  # the boundary forgot it; the adapter did not
    assert report.completed is reader_stops
    if not reader_stops:
        assert "READER_ALIVE" in report.failures and report.ledger.reader == "UNKNOWN"


@pytest.mark.parametrize(("partial", "label", "reader"), [(7, "UNRESOLVED", "FAILED"),
                                                          (None, "UNKNOWN", "UNKNOWN")])
def test_unresolved_or_unknown_partial_bytes_keep_a_stopped_reader_incomplete(partial, label, reader):
    adapter, _boundary, resources = harness()
    resources.reader.partial = partial
    opened(adapter, resources)
    adapter.shutdown(join_timeout=0.01)
    report = adapter.report()
    assert report.reader_partial_bytes == label and report.ledger.reader == reader
    assert resources.reader.alive is False and report.completed is False


# ----------------------------------------------------------------------------- single use

def test_the_adapter_is_single_use_and_positions_are_never_reopened():
    adapter, boundary, resources = harness()
    opened(adapter, resources)
    adapter.open_read(POSITIONS)
    adapter.start()
    adapter.shutdown(join_timeout=0.01)
    adapter.shutdown(join_timeout=0.01)
    adapter.open_read(SUMMARY)
    report = adapter.report()
    assert "POSITIONS_REUSED" in report.ledger.faults
    assert report.failures.count("REUSE_REFUSED") == 2 and "READ_AFTER_SHUTDOWN" in report.failures
    assert stops(boundary) == [SUMMARY, POSITIONS] and [c for c in boundary.calls if c[0] == "connect"]
    assert len([c for c in boundary.calls if c[0] == "connect"]) == 1
    assert report.completed is False


def test_a_raising_connect_leaves_nothing_to_complete():
    lock = RecordingLock()

    class Failing(Boundary):
        def connect(self):
            raise RuntimeError(CANARY)

    adapter = LifecycleAdapter(Failing(lock), lock)
    adapter.start()
    adapter.shutdown(join_timeout=0.01)
    report = adapter.report()
    assert report.state == "START_FAILED" and report.failures == ("CONNECT_HOOK_RAISED",)
    assert report.completed is False and CANARY not in repr(report)


def test_module_surface_and_imports_stay_sdk_free():
    names = sl.__all__ + [n for n in dir(LifecycleAdapter) if not n.startswith("_")]
    for name in names:
        assert not FORBIDDEN_NAME_RE.search(name), name
    tree = ast.parse(Path(sl.__file__).read_text(encoding="utf-8"))
    roots = {(n.module or "").split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    roots |= {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert roots <= {"__future__", "dataclasses", "enum", "typing", "tradingbot"}


# ----------------------------------------------------------------------------- review: startup and shutdown

def test_a_shutdown_during_a_blocked_connect_runs_once_the_references_are_retained():
    lock = RecordingLock()
    entered, release = threading.Event(), threading.Event()

    class Blocking(Boundary):
        def connect(self):
            entered.set()
            assert release.wait(5)
            return super().connect()

    boundary = Blocking(lock)
    adapter = LifecycleAdapter(boundary, lock)
    boundary.adapter = adapter
    worker = threading.Thread(target=adapter.start)
    worker.start()
    assert entered.wait(5)
    adapter.shutdown(join_timeout=0.01)  # deferred, not blocked, while connect is pending
    pending = adapter.report()
    assert pending.state == "STARTING" and pending.completed is False and pending.pending_calls == 1
    release.set()
    worker.join(5)
    report = adapter.report()
    assert not worker.is_alive() and report.state == "CLOSED"
    assert ("disconnect",) in boundary.calls and boundary.resources.reader.alive is False
    assert (report.ledger.reader, report.ledger.socket) == ("STOPPED", "STOPPED")
    assert "REUSE_REFUSED" not in report.failures


def test_a_shutdown_from_inside_the_connect_hook_is_deferred_not_lost():
    lock = RecordingLock()

    class Reentrant(Boundary):
        def connect(self):
            self.adapter.shutdown(join_timeout=0.01)
            return super().connect()

    boundary = Reentrant(lock)
    adapter = LifecycleAdapter(boundary, lock)
    boundary.adapter = adapter
    adapter.start()
    report = adapter.report()
    assert report.state == "CLOSED" and ("disconnect",) in boundary.calls
    assert (report.ledger.reader, report.ledger.socket) == ("STOPPED", "STOPPED")
    assert not any(boundary.lock_held_in_hooks)


# ----------------------------------------------------------------------------- review: exclusive consumer

def _blocked_dispatch(boundary, entered, release):
    original = boundary.dispatch

    def dispatch(slot, kind):
        if kind is DATA and not entered.is_set():
            entered.set()
            assert release.wait(5)
        original(slot, kind)

    boundary.dispatch = dispatch


def test_a_competing_consume_during_a_blocked_dispatch_removes_nothing():
    adapter, boundary, resources = harness()
    adapter.open_read(SUMMARY)
    enqueue(adapter, resources, SUMMARY, DATA, END)
    entered, release = threading.Event(), threading.Event()
    _blocked_dispatch(boundary, entered, release)
    worker = threading.Thread(target=adapter.consume_one)
    worker.start()
    assert entered.wait(5)
    assert adapter.consume_one() is False and resources.queue.items == [(SUMMARY, END)]
    release.set()
    worker.join(5)
    assert adapter.consume_one() is True
    report = adapter.report()
    assert report.ledger.faults == () and report.failures == ()
    assert report.ledger.slots[0].end_marker_seen is True


def test_a_shutdown_during_a_blocked_dispatch_stays_incomplete():
    adapter, boundary, resources = harness()
    adapter.open_read(SUMMARY)
    enqueue(adapter, resources, SUMMARY, DATA, END)
    entered, release = threading.Event(), threading.Event()
    _blocked_dispatch(boundary, entered, release)
    worker = threading.Thread(target=adapter.consume_one)
    worker.start()
    assert entered.wait(5)
    adapter.shutdown(join_timeout=0.01)
    assert resources.queue.items == [(SUMMARY, END)]  # the drain did not take over the consumer
    release.set()
    worker.join(5)
    report = adapter.report()
    assert "CONSUMER_BUSY" in report.failures and report.completed is False


def test_recursive_consumption_from_dispatch_is_refused_before_removing_an_item():
    adapter, boundary, resources = harness()
    adapter.open_read(SUMMARY)
    enqueue(adapter, resources, SUMMARY, DATA, END)
    nested: list[bool] = []
    original = boundary.dispatch

    def dispatch(slot, kind):
        if kind is DATA:
            nested.append(adapter.consume_one())
            nested.append(len(resources.queue.items) == 1)
        original(slot, kind)

    boundary.dispatch = dispatch
    assert adapter.consume_one() is True and nested == [False, True]
    assert adapter.consume_one() is True
    assert adapter.report().ledger.faults == () and adapter.report().failures == ()


# ----------------------------------------------------------------------------- review: item correlation

@pytest.mark.parametrize(("admitted", "physical"), [
    ([(SNAPSHOT, END)], [(SNAPSHOT, DATA)]),
    ([(SUMMARY, END)], [(POSITIONS, END)]),
    ([(SUMMARY, END)], [(SUMMARY, DATA), (SUMMARY, END)]),
], ids=["wrong-kind", "wrong-slot", "unadmitted-ahead-of-admitted"])
def test_a_fetched_item_must_match_its_admission_record(admitted, physical):
    adapter, boundary, resources = harness()
    for slot in (SUMMARY, POSITIONS, SNAPSHOT):
        adapter.open_read(slot)
    for slot, kind in admitted:
        adapter.on_enqueued(slot, kind)
    resources.queue.items.extend(physical)
    adapter.consume_one()
    report = adapter.report()
    assert "ITEM_NOT_ADMITTED" in report.failures
    assert not [c for c in boundary.calls if c[0] == "dispatch"]
    assert report.ledger.in_flight_callbacks == 0 and report.ledger.queued_callbacks == 1
    adapter.shutdown(join_timeout=0.01)
    assert adapter.report().completed is False
