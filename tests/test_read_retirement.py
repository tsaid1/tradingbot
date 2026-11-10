"""Offline read-retirement accounting. Invented event sequences only; no SDK, client or socket.

Each test drives the ledger through events and checks the resulting report. Covered: a valid local
control, and the known E3 failures from the contract review (a stopped reader alone, pending
callbacks, failed or unknown settlement, late events, positions reuse and an incomplete snapshot).
"""
from __future__ import annotations

import ast
from dataclasses import asdict
from pathlib import Path

import pytest

from tradingbot.paper_readiness import read_retirement as rr
from tradingbot.paper_readiness.read_retirement import (
    CallbackKind,
    Component,
    Outcome,
    ReadSlot,
    RetirementLedger,
)
from tradingbot.paper_readiness.transport import FORBIDDEN_NAME_RE

SUMMARY, POSITIONS, SNAPSHOT = ReadSlot.ACCOUNT_SUMMARY, ReadSlot.POSITIONS, ReadSlot.QUOTE_SNAPSHOT


def deliver(ledger: RetirementLedger, slot: ReadSlot, *kinds: CallbackKind) -> None:
    for kind in kinds:
        ledger.callback_received(slot, kind)
        ledger.callback_started()
        ledger.callback_finished()


def opened_reads(ledger: RetirementLedger, *, snapshot_end: bool = True) -> None:
    for slot in (SUMMARY, POSITIONS, SNAPSHOT):
        ledger.open_read(slot)
    deliver(ledger, SUMMARY, CallbackKind.DATA, CallbackKind.END)
    deliver(ledger, POSITIONS, CallbackKind.DATA, CallbackKind.END)
    deliver(ledger, SNAPSHOT, *((CallbackKind.DATA, CallbackKind.END) if snapshot_end
                                 else (CallbackKind.DATA,)))


def retire(ledger: RetirementLedger, *slots: ReadSlot, outcome: Outcome = Outcome.RETURNED) -> None:
    for slot in slots:
        ledger.reserve_attempt(slot)
        ledger.settle_attempt(slot, outcome)


def terminate(ledger: RetirementLedger, *, skip: tuple[Component, ...] = (),
              disconnect: Outcome = Outcome.RETURNED) -> None:
    ledger.close_admission()
    ledger.reserve_disconnect()
    ledger.settle_disconnect(disconnect)
    for component in Component:
        if component not in skip:
            ledger.component_stopped(component, Outcome.STOPPED)
    ledger.seal()


def control() -> RetirementLedger:
    ledger = RetirementLedger()
    opened_reads(ledger)
    retire(ledger, SUMMARY, POSITIONS)
    terminate(ledger)
    return ledger


def slot(report, name: ReadSlot):
    return next(s for s in report.slots if s.slot == name.value)


# ----------------------------------------------------------------------------- valid local control

def test_valid_local_control_establishes_local_quiescence_only():
    report = control().report()
    assert report.local_quiescence_established is True and report.faults == ()
    assert report.broker_side_retirement == "NOT_ESTABLISHED_BY_INSPECTED_SOURCES"
    assert report.paper_read_1 == "NOT_ASSESSED" and report.trading_permission is False
    assert report.authority == "NON_AUTHORITATIVE"
    assert report.snapshot_retirement == "MODELLED_INTENT_ONLY"
    assert slot(report, SNAPSHOT).retirement_attempt == "NOT_RESERVED"  # its END sufficed locally
    assert (report.consumer, report.reader, report.socket) == ("STOPPED", "STOPPED", "STOPPED")


# ----------------------------------------------------------------------------- termination components

def test_a_stopped_reader_alone_is_not_quiescence():
    ledger = RetirementLedger()
    opened_reads(ledger)
    retire(ledger, SUMMARY, POSITIONS)
    ledger.component_stopped(Component.READER, Outcome.STOPPED)
    ledger.seal()
    report = ledger.report()
    assert report.reader == "STOPPED" and report.local_quiescence_established is False
    assert (report.consumer, report.socket, report.disconnect_attempt) == ("UNKNOWN", "UNKNOWN",
                                                                           "NOT_RESERVED")
    assert report.admission_closed is False


@pytest.mark.parametrize("missing", list(Component))
def test_each_component_must_report_stopped(missing):
    ledger = RetirementLedger()
    opened_reads(ledger)
    retire(ledger, SUMMARY, POSITIONS)
    terminate(ledger, skip=(missing,))
    report = ledger.report()
    assert getattr(report, missing.value.lower()) == "UNKNOWN"
    assert report.local_quiescence_established is False


def test_queued_or_in_flight_callbacks_at_seal_block_quiescence():
    queued = RetirementLedger()
    opened_reads(queued)
    retire(queued, SUMMARY, POSITIONS)
    queued.callback_received(SNAPSHOT, CallbackKind.DATA)  # admitted, never dispatched
    terminate(queued)
    assert queued.report().queued_callbacks == 1
    assert queued.report().local_quiescence_established is False
    in_flight = RetirementLedger()
    opened_reads(in_flight)
    retire(in_flight, SUMMARY, POSITIONS)
    in_flight.callback_received(SNAPSHOT, CallbackKind.DATA)
    in_flight.callback_started()  # the consumer stopped mid-dispatch
    terminate(in_flight)
    assert in_flight.report().in_flight_callbacks == 1
    assert in_flight.report().local_quiescence_established is False


def test_review_scenario_dispatch_after_consumer_stopped_blocks_quiescence():
    """Review P2: snapshot END queued, consumer reported STOPPED, then the callback is dispatched."""
    ledger = RetirementLedger()
    ledger.open_read(SNAPSHOT)
    ledger.callback_received(SNAPSHOT, CallbackKind.END)
    ledger.component_stopped(Component.CONSUMER, Outcome.STOPPED)
    ledger.callback_started()
    ledger.callback_finished()
    ledger.close_admission()
    ledger.reserve_disconnect()
    ledger.settle_disconnect(Outcome.RETURNED)
    ledger.component_stopped(Component.READER, Outcome.STOPPED)
    ledger.component_stopped(Component.SOCKET, Outcome.STOPPED)
    ledger.seal()
    report = ledger.report()
    assert report.faults == ("DISPATCH_AFTER_CONSUMER_STOPPED", "DISPATCH_AFTER_CONSUMER_STOPPED")
    assert report.local_quiescence_established is False


def test_an_in_flight_callback_finished_after_consumer_stopped_is_a_fault():
    ledger = RetirementLedger()
    opened_reads(ledger)
    retire(ledger, SUMMARY, POSITIONS)
    ledger.callback_received(SNAPSHOT, CallbackKind.DATA)
    ledger.callback_started()
    ledger.component_stopped(Component.CONSUMER, Outcome.STOPPED)
    ledger.callback_finished()
    terminate(ledger, skip=(Component.CONSUMER,))
    report = ledger.report()
    assert "DISPATCH_AFTER_CONSUMER_STOPPED" in report.faults
    assert report.local_quiescence_established is False


def test_a_receipt_after_reader_stopped_is_a_fault():
    ledger = RetirementLedger()
    opened_reads(ledger)
    retire(ledger, SUMMARY, POSITIONS)
    ledger.component_stopped(Component.READER, Outcome.STOPPED)
    ledger.callback_received(SNAPSHOT, CallbackKind.DATA)
    queued_before = ledger.report().queued_callbacks
    terminate(ledger, skip=(Component.READER,))
    report = ledger.report()
    assert queued_before == 1 and "RECEIPT_AFTER_READER_STOPPED" in report.faults
    assert report.local_quiescence_established is False


def test_a_callback_after_admission_closes_is_a_fault_not_queued():
    ledger = RetirementLedger()
    opened_reads(ledger)
    retire(ledger, SUMMARY, POSITIONS)
    ledger.close_admission()
    ledger.callback_received(SUMMARY, CallbackKind.DATA)
    report = ledger.report()
    assert report.faults == ("CALLBACK_AFTER_ADMISSION_CLOSED",) and report.queued_callbacks == 0


# ----------------------------------------------------------------------------- settlement

def test_a_failed_attempt_is_retained_never_retried_and_disconnect_is_independent():
    ledger = RetirementLedger()
    opened_reads(ledger)
    retire(ledger, SUMMARY, outcome=Outcome.FAILED)
    retire(ledger, SUMMARY)  # a retry is refused and cannot replace FAILED
    retire(ledger, POSITIONS)
    terminate(ledger)
    report = ledger.report()
    assert slot(report, SUMMARY).retirement_attempt == "FAILED"
    assert report.disconnect_attempt == "RETURNED"
    assert "ATTEMPT_ALREADY_RESERVED" in report.faults and "DUPLICATE_SETTLEMENT" in report.faults
    assert report.local_quiescence_established is False


def test_an_unsettled_attempt_becomes_unknown_at_seal_and_cannot_settle_later():
    ledger = RetirementLedger()
    opened_reads(ledger)
    ledger.reserve_attempt(SUMMARY)  # the effect never reported: silence, not success
    retire(ledger, POSITIONS)
    terminate(ledger)
    ledger.settle_attempt(SUMMARY, Outcome.RETURNED)
    report = ledger.report()
    assert slot(report, SUMMARY).retirement_attempt == "UNKNOWN"
    assert "EVENT_AFTER_SEAL" in report.faults and report.local_quiescence_established is False


def test_an_outcome_cannot_be_reported_before_its_reservation():
    ledger = RetirementLedger()
    opened_reads(ledger)
    ledger.settle_attempt(SUMMARY, Outcome.RETURNED)
    ledger.settle_disconnect(Outcome.RETURNED)
    report = ledger.report()
    assert slot(report, SUMMARY).retirement_attempt == "NOT_RESERVED"
    assert report.disconnect_attempt == "NOT_RESERVED"
    assert report.faults.count("ATTEMPT_NOT_RESERVED") == 2


@pytest.mark.parametrize("outcome", [Outcome.FAILED, Outcome.UNKNOWN])
def test_a_failed_or_unknown_disconnect_or_component_is_retained(outcome):
    disconnect = RetirementLedger()
    opened_reads(disconnect)
    retire(disconnect, SUMMARY, POSITIONS)
    terminate(disconnect, disconnect=outcome)
    assert disconnect.report().disconnect_attempt == outcome.value
    assert disconnect.report().local_quiescence_established is False
    socket = RetirementLedger()
    opened_reads(socket)
    retire(socket, SUMMARY, POSITIONS)
    socket.component_stopped(Component.SOCKET, outcome)
    socket.component_stopped(Component.SOCKET, Outcome.STOPPED)  # never replaces the first
    terminate(socket)
    assert socket.report().socket == outcome.value
    assert socket.report().local_quiescence_established is False


def test_an_attempt_after_disconnect_is_reserved_is_a_fault():
    ledger = RetirementLedger()
    opened_reads(ledger)
    ledger.reserve_disconnect()
    ledger.reserve_attempt(SUMMARY)
    assert ledger.report().faults == ("ATTEMPT_AFTER_DISCONNECT",)


def test_retirement_can_start_from_requested_after_an_error_but_the_read_stays_incomplete():
    ledger = RetirementLedger()
    ledger.open_read(SUMMARY)
    deliver(ledger, SUMMARY, CallbackKind.ERROR)
    retire(ledger, SUMMARY)
    terminate(ledger)
    report = ledger.report()
    summary = slot(report, SUMMARY)
    assert summary.retirement_attempt == "RETURNED" and summary.end_marker_seen is False
    assert summary.read_complete is False and report.faults == ()
    assert report.local_quiescence_established is False


# ----------------------------------------------------------------------------- late and post-END events

def test_a_callback_after_the_retirement_attempt_is_a_late_fault():
    ledger = RetirementLedger()
    ledger.open_read(SUMMARY)
    retire(ledger, SUMMARY)
    deliver(ledger, SUMMARY, CallbackKind.DATA)
    report = ledger.report()
    assert report.faults == ("LATE_CALLBACK",) and slot(report, SUMMARY).data_callbacks == 0


@pytest.mark.parametrize("read", [SUMMARY, POSITIONS, SNAPSHOT])
def test_an_event_after_end_is_a_fault(read):
    ledger = RetirementLedger()
    ledger.open_read(read)
    deliver(ledger, read, CallbackKind.END, CallbackKind.DATA)
    assert ledger.report().faults == ("POST_END_EVENT",)


def test_dispatch_must_follow_receipt():
    ledger = RetirementLedger()
    ledger.callback_started()
    ledger.callback_finished()
    assert ledger.report().faults == ("DISPATCH_OUT_OF_ORDER", "DISPATCH_OUT_OF_ORDER")


# ----------------------------------------------------------------------------- positions and snapshot

def test_positions_cannot_be_reused_on_the_same_connection_after_local_retirement():
    ledger = RetirementLedger()
    ledger.open_read(POSITIONS)
    deliver(ledger, POSITIONS, CallbackKind.END)
    retire(ledger, POSITIONS)
    ledger.open_read(POSITIONS)
    report = ledger.report()
    assert report.faults == ("POSITIONS_REUSED",)
    assert [s.slot for s in report.slots] == ["POSITIONS"]


def test_a_snapshot_without_end_stays_incomplete_even_with_a_returned_attempt():
    ledger = RetirementLedger()
    opened_reads(ledger, snapshot_end=False)
    retire(ledger, SUMMARY, POSITIONS, SNAPSHOT)
    terminate(ledger)
    report = ledger.report()
    snapshot = slot(report, SNAPSHOT)
    assert snapshot.end_marker_seen is False and snapshot.read_complete is False
    assert snapshot.retirement_attempt == "RETURNED"  # modelled intent only, not completion
    assert report.local_quiescence_established is False


@pytest.mark.parametrize("settle", [None, Outcome.UNKNOWN, Outcome.FAILED],
                         ids=["unsettled", "unknown", "failed"])
def test_a_reserved_snapshot_attempt_after_end_must_return(settle):
    """Review P2: once reserved, a snapshot attempt that is not RETURNED blocks local quiescence."""
    ledger = RetirementLedger()
    opened_reads(ledger)
    retire(ledger, SUMMARY, POSITIONS)
    ledger.reserve_attempt(SNAPSHOT)
    if settle is not None:
        ledger.settle_attempt(SNAPSHOT, settle)
    terminate(ledger)
    report = ledger.report()
    assert slot(report, SNAPSHOT).read_complete is True
    assert slot(report, SNAPSHOT).retirement_attempt == (settle or Outcome.UNKNOWN).value
    assert report.faults == () and report.local_quiescence_established is False


def test_a_returned_snapshot_attempt_after_end_keeps_the_control():
    ledger = RetirementLedger()
    opened_reads(ledger)
    retire(ledger, SUMMARY, POSITIONS, SNAPSHOT)
    terminate(ledger)
    assert ledger.report().local_quiescence_established is True


def test_a_snapshot_end_alone_is_not_connection_retirement():
    ledger = RetirementLedger()
    ledger.open_read(SNAPSHOT)
    deliver(ledger, SNAPSHOT, CallbackKind.DATA, CallbackKind.END)
    ledger.seal()
    report = ledger.report()
    assert slot(report, SNAPSHOT).read_complete is True
    assert report.local_quiescence_established is False


# ----------------------------------------------------------------------------- bounds and surface

def test_events_are_bounded():
    ledger = RetirementLedger()
    ledger.open_read(SUMMARY)
    for _ in range(rr.MAX_EVENTS + 10):
        ledger.callback_received(SUMMARY, CallbackKind.DATA)
    report = ledger.report()
    assert report.queued_callbacks == rr.MAX_EVENTS - 1
    assert report.faults == ("EVENT_LIMIT_EXCEEDED",)


def test_report_carries_only_fixed_fields_and_no_values():
    report = control().report()
    assert set(asdict(report)) == {
        "authority", "trading_permission", "paper_read_1", "broker_side_retirement",
        "snapshot_retirement", "sealed", "slots", "admission_closed", "queued_callbacks",
        "in_flight_callbacks", "consumer", "reader", "socket", "disconnect_attempt", "faults",
        "faults_dropped", "local_quiescence_established",
    }
    assert set(asdict(report.slots[0])) == {"slot", "end_marker_seen", "errored", "read_complete",
                                            "data_callbacks", "retirement_attempt"}


def test_module_has_no_forbidden_names_no_callables_taken_and_no_io_imports():
    for name in rr.__all__ + [n for n in dir(RetirementLedger) if not n.startswith("_")]:
        assert not FORBIDDEN_NAME_RE.search(name), name
    tree = ast.parse(Path(rr.__file__).read_text(encoding="utf-8"))
    roots = {(n.module or "").split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    roots |= {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert roots <= {"__future__", "dataclasses", "enum", "typing"}
