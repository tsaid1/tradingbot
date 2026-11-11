"""The offline callback-driven readiness demonstration. Invented fixtures only; a SIMULATION.

Covers the complete path (invented callbacks -> the accepted callback normalizers ->
ResponseEnvelope -> FAKE transport -> unchanged runner -> NON_AUTHORITATIVE report) for all four
readiness streams, the successful control, the two failure scenarios tied to the PR #25 review
defects, the two account and position failure classes from the PR #35 review, and the stable CLI
and report semantics. It also covers the composed local lifecycle: quote, account-summary and
position callbacks delivered only by the merged LifecycleAdapter's consumer, and the CLI-only
unknown-cleanup counterpart, which stays incomplete beside CONTROL_COMPLETE. Finally it covers the
frame-to-callback path: each of those read callbacks travels in one owned invented frame through the
accepted DecodeHandoff, closed InventedDecoder, ReadCallbackFacade and CallbackRouter consumer, with
complete, partial, refused, untaken and handoff-failure controls, direct-normalizer envelope parity,
and every read stream's evidence read through CallbackRouter.stream_status().
"""
from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path

import pytest

from tradingbot.paper_readiness import callback_demo as demo
from tradingbot.paper_readiness import decode_handoff
from tradingbot.paper_readiness.account_position_callbacks import (
    AccountSummaryCallbacks,
    PositionSnapshotCallbacks,
)
from tradingbot.paper_readiness.decode_handoff import Failure, HandoffStatus
from tradingbot.paper_readiness.diagnostics import ReadinessOutcome, Stage
from tradingbot.paper_readiness.ibkr_callbacks import CallbackRequest, IbkrCallbackNormalizer
from tradingbot.paper_readiness.observations import ResponseKind
from tradingbot.paper_readiness.read_framing import FrameAccumulator, FramingOutcome
from tradingbot.paper_readiness.runner import REQUEST_IDS
from tradingbot.paper_readiness.sdk_lifecycle import LifecycleAdapter
from tradingbot.paper_readiness.transport import FORBIDDEN_NAME_RE, FakeClock

SRC = Path(__file__).resolve().parents[1] / "src"
UNKNOWN = demo.UNKNOWN_CLEANUP.name
CURRENCY_MISSING, MALFORMED_POSITION = (s.name for s in demo.ACCOUNT_POSITION_FAILURES)
PARTIAL_HEADER, PARTIAL_PAYLOAD, REFUSED, UNTAKEN = (s.name for s in demo.READER_FRAMING_CASES)
FRAMING = [PARTIAL_HEADER, PARTIAL_PAYLOAD, REFUSED, UNTAKEN]
[HANDOFF_RAISED] = (s.name for s in demo.HANDOFF_FAILURE_CASES)
ALL_NAMES = [*demo.SCENARIOS, UNKNOWN, CURRENCY_MISSING, MALFORMED_POSITION, *FRAMING, HANDOFF_RAISED]
STREAMS = ["contract_details", "quote_snapshot", "account_summary", "positions"]
CONTROL_CODES = {"SESSION_ATTESTATION_CONTROL", "CONTRACT_IDENTITY_CONTROL", "REGULAR_SESSION_CONTROL",
                 "QUOTE_LIVE_CONTROL", "ACCOUNT_SNAPSHOT_CONTROL", "POSITIONS_SNAPSHOT_CONTROL"}
ALL_READS = ["request_contract_details", "request_quote_snapshot", "request_account_summary",
             "request_positions"]


def codes(result) -> list[str]:
    return [f.code for f in result.report.findings]


def stage_codes(result, stage: str) -> list[str]:
    return [f.code for f in result.report.findings if f.stage.value == stage]


CASES = {s.name: s for s in (*demo.SCENARIOS.values(), demo.UNKNOWN_CLEANUP,
                             *demo.ACCOUNT_POSITION_FAILURES, *demo.READER_FRAMING_CASES,
                             *demo.HANDOFF_FAILURE_CASES)}


def captured(monkeypatch, name: str):
    """Run one case and capture the script the demo fed to the unchanged runner."""
    scripts = []
    real_script = demo.FakeScript

    def script(**fields):
        scripts.append(real_script(**fields))
        return scripts[-1]

    monkeypatch.setattr(demo, "FakeScript", script)
    result = demo.run_demo(name)
    [fed] = scripts
    return result, fed


def direct(name: str):
    """The parity reference only: the case's same invented callbacks fed straight into fresh accepted
    normalizers, with no frame, decoder, facade, router or consumer in between."""
    case = CASES[name]
    contract = IbkrCallbackNormalizer(REQUEST_IDS["request_contract_details"],
                                      CallbackRequest.CONTRACT_DETAILS)
    quote = IbkrCallbackNormalizer(REQUEST_IDS["request_quote_snapshot"], CallbackRequest.QUOTE_SNAPSHOT,
                                   con_id=700001, clock=FakeClock(demo.DEMO_CLOCK))
    summary = AccountSummaryCallbacks(REQUEST_IDS["request_account_summary"])
    positions = PositionSnapshotCallbacks(REQUEST_IDS["request_positions"])
    case.summary_feed(summary)
    case.positions_feed(positions)
    case.feed(contract, quote)
    return contract, quote, summary, positions


def issue_codes(normalizer) -> tuple[str, ...]:
    return tuple(issue.code.value for issue in normalizer.issues)


def slot(result, name: str):
    [found] = [s for s in result.lifecycle.ledger.slots if s.slot == name]
    return found


def test_callback_control_completes_through_the_unchanged_runner():
    result = demo.run_demo("callback_control")
    assert result.report.outcome == ReadinessOutcome.CONTROL_COMPLETE and result.matches_expectation
    assert CONTROL_CODES <= set(codes(result))
    assert result.contract_completed and result.quote_completed
    assert result.account_summary_completed and result.positions_completed
    assert result.contract_issues == result.quote_issues == ()
    assert result.account_summary_issues == result.positions_issues == ()
    assert result.report.dispatched == ALL_READS
    assert all(o.end_seen for o in result.report.observations)
    rows = {o.stage.value: (o.request_id, o.rows) for o in result.report.observations}
    assert rows["ACCOUNT"] == (REQUEST_IDS["request_account_summary"], 1)
    assert rows["POSITIONS"] == (REQUEST_IDS["request_positions"], 2)


# Requirement: all four streams come from the accepted callback normalizers; none is preassembled.
# The three read streams reach theirs only through owned frames and the router's consumer, and what
# reaches the runner is exactly what feeding the same callbacks directly would produce.

@pytest.mark.parametrize("name", ALL_NAMES)
def test_every_stream_reaches_the_runner_exactly_as_direct_normalizers_produce_it(monkeypatch, name):
    result, fed = captured(monkeypatch, name)
    contract, quote, summary, positions = direct(name)
    assert fed.contract_details == contract.envelopes()
    assert fed.quote_snapshot == quote.envelopes()
    assert fed.account_summary == summary.envelopes() and fed.positions == positions.envelopes()
    assert fed.account_summary and fed.positions and fed.quote_snapshot
    # The summary keeps its broker-style synthetic request id; positions carry only the local id.
    assert {e.request_id for e in fed.account_summary} == {REQUEST_IDS["request_account_summary"]}
    assert {e.request_id for e in fed.positions} == {REQUEST_IDS["request_positions"]}
    # The router's own normalizer evidence, read through stream_status(), equals the direct verdict.
    assert (result.quote_completed, result.quote_issues) == (quote.completed, issue_codes(quote))
    assert (result.account_summary_completed, result.account_summary_issues) == (
        summary.completed, issue_codes(summary))
    assert (result.positions_completed, result.positions_issues) == (
        positions.completed, issue_codes(positions))


def test_the_demo_builds_no_read_normalizer_of_its_own():
    """One owning path: only the contract normalizer is the demo's; the read ones are the router's."""
    source = Path(demo.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    called = [node.func.id for node in ast.walk(tree)
              if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)]
    assert "AccountSummaryCallbacks" not in called and "PositionSnapshotCallbacks" not in called
    assert called.count("IbkrCallbackNormalizer") == 1  # the contract-details normalizer only
    assert called.count("CallbackRouter") == called.count("DecodeHandoff") == 1
    for registration in ("register_quote", "register_account_summary", "register_positions"):
        assert source.count(f"router.{registration}(") == 1, registration
    assert "owner=" not in source and "managedAccounts" not in source  # no owner or account override


def test_fractional_and_negative_positions_reach_the_script_exactly(monkeypatch):
    result, fed = captured(monkeypatch, "callback_control")
    lines = [e.position for e in fed.positions if e.kind == ResponseKind.POSITION]
    assert [(line.con_id, line.position) for line in lines] == [(700011, "-12.5"),
                                                                (700012, "300.25")]
    assert [Decimal(line.position) for line in lines] == [Decimal("-12.5"), Decimal("300.25")]
    assert fed.positions[-1].kind == ResponseKind.POSITION_END and result.positions_completed


def test_no_preassembled_envelope_or_observation_is_built_in_the_demo():
    tree = ast.parse(Path(demo.__file__).read_text(encoding="utf-8"))
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    names |= {alias.name for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
              for alias in node.names}
    for built in ("ResponseEnvelope", "ResponseKind", "AccountSummaryObservation", "PositionLine"):
        assert built not in names, built
    assert not hasattr(demo, "PREASSEMBLED_STREAMS")


# Requirement: the two PR #35 failure classes cannot reach success through this composition.

def test_a_monetary_row_without_currency_cannot_reach_success(monkeypatch):
    control = demo.run_demo("callback_control")
    result, fed = captured(monkeypatch, CURRENCY_MISSING)
    assert result.report.outcome == ReadinessOutcome.NOT_READY and result.matches_expectation
    assert "ACCOUNT_CURRENCY_MISSING" in codes(result)
    assert "ACCOUNT_SNAPSHOT_CONTROL" not in codes(result)
    [row] = [e.account_summary for e in fed.account_summary
             if e.kind == ResponseKind.ACCOUNT_SUMMARY]
    assert row.currency is None  # one row's currency never vouches for another
    # Delivered and settled through the real path; the existing readiness check decides.
    assert result.account_summary_completed and result.account_summary_issues == ()
    assert result.handoff.completed and slot(result, "ACCOUNT_SUMMARY").data_callbacks == 4
    # Paired with the control, only the account stage differs; the lifecycle report is unmodified.
    for stage in (s.value for s in Stage if s is not Stage.ACCOUNT):
        assert stage_codes(result, stage) == stage_codes(control, stage), stage
    assert asdict(result.lifecycle) == asdict(control.lifecycle)
    # Every local component completes, so the readiness verdict alone blocks the aggregate.
    assert result.local_components_complete and not result.aggregate_complete


def test_a_malformed_quantity_then_repeated_end_cannot_reach_success(monkeypatch):
    control = demo.run_demo("callback_control")
    result, fed = captured(monkeypatch, MALFORMED_POSITION)
    assert result.report.outcome == ReadinessOutcome.NOT_READY and result.matches_expectation
    assert result.positions_issues == ("MALFORMED_CALLBACK",) and not result.positions_completed
    assert ResponseKind.POSITION_END not in [e.kind for e in fed.positions]  # neither END survives
    assert "POSITIONS_END_MISSING" in codes(result)
    assert "POSITIONS_SNAPSHOT_CONTROL" not in codes(result)
    # The two valid rows stay visible records; the malformed one was never stored.
    assert [e.position.con_id for e in fed.positions] == [700011, 700012]
    for stage in (s.value for s in Stage if s is not Stage.POSITIONS):
        assert stage_codes(result, stage) == stage_codes(control, stage), stage
    # Through the real path every frame settles, and the ledger counts three position DATA callbacks
    # and the first END; the repeated END is a post-END event, so the lifecycle cannot complete.
    assert result.handoff.failure is None and len(result.handoff.handoffs) == 18
    assert all(r.status is HandoffStatus.SETTLED for r in result.handoff.handoffs)
    positions_slot = slot(result, "POSITIONS")
    assert (positions_slot.data_callbacks, positions_slot.end_marker_seen) == (3, True)
    assert result.lifecycle.ledger.faults == ("POST_END_EVENT",) and not result.lifecycle.completed
    assert result.scenario.expected_lifecycle_completed is False and result.matches_expectation
    # Cross-stream success never clears the failed stream.
    assert result.quote_completed and result.account_summary_completed and result.contract_completed
    assert not result.streams_completed and not result.local_components_complete
    assert not result.aggregate_complete and not result.handoff.completed


def test_delayed_size_mixed_with_live_ticks_cannot_become_a_live_control():
    result = demo.run_demo("delayed_size_mixed_with_live")
    assert result.report.outcome == ReadinessOutcome.NOT_READY and result.matches_expectation
    assert result.quote_issues == ("NON_LIVE_TICK_UNREPRESENTED",) and not result.quote_completed
    assert "QUOTE_LIVE_CONTROL" not in codes(result)
    assert "TICK_SNAPSHOT_END_MISSING" in codes(result)


def test_issue_with_repeated_end_cannot_establish_contract_identity():
    result = demo.run_demo("issue_with_repeated_end")
    assert result.report.outcome == ReadinessOutcome.NOT_READY and result.matches_expectation
    assert result.contract_issues == ("UNSUPPORTED_CALLBACK",) and not result.contract_completed
    assert {"CONTRACT_DETAILS_END_MISSING", "QUOTE_SKIPPED_NO_CONTRACT_IDENTITY"} <= set(codes(result))
    assert "request_quote_snapshot" not in result.report.dispatched
    assert not {"CONTRACT_IDENTITY_CONTROL", "QUOTE_LIVE_CONTROL"} & set(codes(result))


# Requirement: readiness and local lifecycle cleanup are composed and reported together.

def test_control_reports_readiness_beside_observed_local_lifecycle_completion():
    result = demo.run_demo("callback_control")
    life, ledger = result.lifecycle, result.lifecycle.ledger
    assert result.report.outcome == ReadinessOutcome.CONTROL_COMPLETE and result.matches_expectation
    assert life.completed is True and ledger.local_quiescence_established is True
    assert (life.state, life.pending_calls, life.failures) == ("CLOSED", 0, ())
    # Three separate observations, each STOPPED, plus the returned disconnect.
    assert (ledger.reader, ledger.socket, ledger.consumer) == ("STOPPED", "STOPPED", "STOPPED")
    assert life.reader_partial_bytes == "NONE" and ledger.disconnect_attempt == "RETURNED"
    assert ledger.admission_closed
    assert (ledger.queued_callbacks, ledger.in_flight_callbacks) == (0, 0)
    # Three read slots, each delivered and ended. The snapshot needs no stop; the continuing summary
    # and positions reads end with the invented client's returned stop hook, never an SDK cancellation.
    assert [(s.slot, s.data_callbacks, s.end_marker_seen, s.read_complete, s.retirement_attempt)
            for s in ledger.slots] == [("ACCOUNT_SUMMARY", 4, True, True, "RETURNED"),
                                       ("POSITIONS", 2, True, True, "RETURNED"),
                                       ("QUOTE_SNAPSHOT", 7, True, True, "NOT_RESERVED")]
    assert ledger.faults == () and result.streams_completed and result.aggregate_complete


def test_lifecycle_counts_follow_the_modelled_quote_callbacks():
    """The extra delayed-size callback is one more dispatched DATA; only readiness differs."""
    result = demo.run_demo("delayed_size_mixed_with_live")
    assert result.report.outcome == ReadinessOutcome.NOT_READY and result.lifecycle.completed
    assert slot(result, "QUOTE_SNAPSHOT").data_callbacks == 8


def test_read_envelopes_come_only_from_callbacks_the_adapter_consumer_dispatched(monkeypatch):
    monkeypatch.setattr(LifecycleAdapter, "consume_one", lambda self: False)
    result = demo.run_demo("callback_control")
    assert not (result.quote_completed or result.account_summary_completed
                or result.positions_completed)
    assert result.report.outcome == ReadinessOutcome.NOT_READY
    assert {"TICK_SNAPSHOT_END_MISSING", "ACCOUNT_SUMMARY_END_MISSING",
            "POSITIONS_END_MISSING"} <= set(codes(result))
    # The first frame's summary row was admitted but never dispatched, so the handoff holds it and
    # takes no further frame: settlement, not admission, releases a frame.
    assert (result.handoff.failure, result.handoff.held_sequence) == (Failure.SETTLEMENT_INCOMPLETE, 1)
    life, ledger = result.lifecycle, result.lifecycle.ledger
    assert life.completed is False and "QUEUE_NOT_EMPTY" in life.failures
    assert ledger.queued_callbacks == 1 and ledger.consumer == "UNKNOWN"
    # Every read's stop returned, and that alone clears nothing.
    assert [(s.data_callbacks, s.end_marker_seen, s.retirement_attempt) for s in ledger.slots] == [
        (0, False, "RETURNED")] * 3
    assert not result.matches_expectation and not result.aggregate_complete


def test_unknown_socket_closure_keeps_the_lifecycle_incomplete_beside_control_complete():
    control, unknown = demo.run_demo("callback_control"), demo.run_demo(UNKNOWN)
    life, ledger = unknown.lifecycle, unknown.lifecycle.ledger
    assert unknown.report.outcome == ReadinessOutcome.CONTROL_COMPLETE
    assert unknown.report.model_dump(mode="json") == control.report.model_dump(mode="json") | {
        "label": f"callback-demo:{UNKNOWN}"}
    assert life.completed is False and ledger.local_quiescence_established is False
    assert ledger.socket == "UNKNOWN" and life.failures == ("SOCKET_NOT_CLOSED",)
    # Present, and none clears it: the returned disconnect, the disconnected flag, the empty queue.
    assert ledger.disconnect_attempt == "RETURNED" and "CONNECTION_CLOSED" in life.direct_callbacks
    assert ledger.consumer == "STOPPED"
    assert (ledger.queued_callbacks, ledger.in_flight_callbacks) == (0, 0)
    assert ledger.reader == "STOPPED" and life.state == "CLOSED" and life.pending_calls == 0
    # Paired with the control, only the socket observation and what follows from it differ.
    a, b = asdict(control.lifecycle), asdict(life)
    assert {k for k in a if a[k] != b[k]} == {"completed", "failures", "ledger"}
    assert {k for k in a["ledger"] if a["ledger"][k] != b["ledger"][k]} == {
        "socket", "local_quiescence_established"}
    assert unknown.matches_expectation  # it expects the incomplete lifecycle


# Requirement: the reader buffer is the accepted FrameAccumulator; its observed retained bytes feed
# the lifecycle's reader observation, and its unchanged report is carried beside the lifecycle one.

CONTROL = "callback_control"
LOG_METHODS = ("feed", "take", "finish")


def run_logged(monkeypatch, name: str):
    """Run one case through a call-logging accumulator whose behaviour is the accepted one."""
    made = []

    class Logged(FrameAccumulator):
        __slots__ = ("calls",)

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.calls = []
            made.append(self)

    for method in LOG_METHODS:
        def logged(self, *args, _name=method):
            self.calls.append(_name)
            return getattr(FrameAccumulator, _name)(self, *args)
        setattr(Logged, method, logged)
    monkeypatch.setattr(decode_handoff, "FrameAccumulator", Logged)  # the handoff's own accumulator
    result = demo.run_demo(name)
    [accumulator] = made
    return result, accumulator


@pytest.mark.parametrize("name", ALL_NAMES)
def test_the_reader_observation_reads_the_accumulators_retained_bytes(monkeypatch, name):
    result, accumulator = run_logged(monkeypatch, name)
    report = accumulator.report()
    assert result.framing == report  # carried unchanged
    assert report.finished
    assert report.bytes_accepted == report.bytes_consumed + report.bytes_retained
    # No byte count is set: the lifecycle classifies exactly what the accumulator retains.
    life, ledger = result.lifecycle, result.lifecycle.ledger
    if report.bytes_retained == 0:
        assert life.reader_partial_bytes == "NONE" and ledger.reader == "STOPPED"
    else:
        assert life.reader_partial_bytes == "UNRESOLVED" and ledger.reader == "FAILED"
        assert "PARTIAL_BYTES_UNRESOLVED" in life.failures
    # Shutdown only ends input: finish() runs once, last, and nothing is fed or taken after it.
    assert accumulator.calls.count("finish") == 1 and accumulator.calls[-1] == "finish"
    assert result.local_components_complete is (life.completed and report.clean
                                                 and result.handoff.completed and result.streams_completed)
    assert result.matches_expectation


def test_the_control_buffer_is_complete_and_every_frame_is_handed_off():
    result = demo.run_demo(CONTROL)
    framing = result.framing
    assert framing.outcome is FramingOutcome.COMPLETE and framing.clean
    # Sixteen owned frames, one per read callback: four summary rows and their END, two positions and
    # their END, then the quote's marketDataType, six ticks and tickSnapshotEnd. Each is a 4-byte
    # header and a 27-byte invented key, arriving fragmented in 13-byte chunks.
    assert (framing.chunks_accepted, framing.bytes_accepted, framing.bytes_retained) == (39, 496, 0)
    assert (framing.frames_extracted, framing.zero_length_frames) == (16, 0)
    assert (framing.frames_handed_off, framing.frames_queued) == (16, 0)
    assert framing.downstream_retirement == "NOT_ESTABLISHED"
    records = result.handoff.handoffs
    assert [r.sequence for r in records] == list(range(1, 17))
    assert all(r.status is HandoffStatus.SETTLED and (r.callbacks, r.records_admitted,
               r.records_dispatched) == (1, 1, 1) for r in records)
    assert result.handoff.completed and result.router.faults == ()


def test_quote_evidence_is_the_router_normalizers_status():
    result = demo.run_demo("delayed_size_mixed_with_live")
    assert result.quote_issues == ("NON_LIVE_TICK_UNREPRESENTED",) and result.quote_completed is False
    assert result.handoff.completed  # every frame settled: delivery is not stream validity
    # The summary and positions succeed beside it, and that never clears the failed quote stream.
    assert result.account_summary_completed and result.positions_completed
    assert not result.streams_completed and not result.local_components_complete


@pytest.mark.parametrize("name", ALL_NAMES)
def test_no_stream_failure_is_cleared_by_another_streams_success(name):
    result = demo.run_demo(name)
    failed = not (result.contract_completed and result.quote_completed
                  and result.account_summary_completed and result.positions_completed)
    assert result.streams_completed is not failed
    if failed:
        assert not result.local_components_complete and not result.aggregate_complete
    assert result.aggregate_complete is (result.local_components_complete
                                         and result.report.outcome == ReadinessOutcome.CONTROL_COMPLETE)


def test_a_decoder_failure_after_a_complete_frame_blocks_local_completion_even_after_end():
    control, failed = demo.run_demo(CONTROL), demo.run_demo(HANDOFF_RAISED)
    # Framing is complete and clean: the failure lies between a complete frame and its settlement.
    assert failed.framing.outcome is FramingOutcome.COMPLETE and failed.framing.clean
    handoff = failed.handoff
    assert (handoff.failure, handoff.held_sequence, handoff.completed) == (Failure.DECODER_RAISED, 16, False)
    *settled, last = handoff.handoffs
    assert len(settled) == 15 and all(r.status is HandoffStatus.SETTLED for r in settled)
    assert (last.status, last.callbacks, last.records_admitted, last.records_dispatched) == (
        HandoffStatus.FAILED, 1, 1, 0)
    # Shutdown later drains the admitted END, so the stream completes and readiness is unchanged, but
    # the END arrived after the stop attempt: the lifecycle records it late and stays incomplete.
    assert failed.quote_completed and failed.report.outcome == control.report.outcome
    quote = slot(failed, "QUOTE_SNAPSHOT")
    assert (quote.end_marker_seen, quote.read_complete, quote.retirement_attempt) == (False, False, "RETURNED")
    assert failed.lifecycle.ledger.faults == ("LATE_CALLBACK",) and not failed.lifecycle.completed
    # The summary and positions slots completed earlier; their success does not clear the failure.
    assert slot(failed, "ACCOUNT_SUMMARY").read_complete and slot(failed, "POSITIONS").read_complete
    assert failed.streams_completed and not failed.local_components_complete
    assert not failed.aggregate_complete and failed.matches_expectation


@pytest.mark.parametrize("name, outcome, retained", [
    (PARTIAL_HEADER, FramingOutcome.PARTIAL_HEADER, 2),
    (PARTIAL_PAYLOAD, FramingOutcome.PARTIAL_PAYLOAD, 10),
])
def test_a_retained_partial_frame_keeps_lifecycle_and_framing_incomplete(name, outcome, retained):
    control, partial = demo.run_demo(CONTROL), demo.run_demo(name)
    # The pair differs only in a trailing partial chunk after the control's complete input.
    case = next(s for s in demo.READER_FRAMING_CASES if s.name == name)
    base = demo.SCENARIOS[CONTROL]
    assert case.feed is base.feed and len(case.trailing_bytes) == 1 and base.trailing_bytes == ()
    assert case.takes_final_frames and case.socket_closure_known
    # Readiness is unchanged and stays CONTROL_COMPLETE; lifecycle closure cannot hide the bytes.
    assert partial.report.model_dump(mode="json") == control.report.model_dump(mode="json") | {
        "label": f"callback-demo:{name}"}
    framing = partial.framing
    assert framing.outcome is outcome and framing.bytes_retained == retained and not framing.clean
    assert framing.frames_handed_off == control.framing.frames_handed_off == 16
    assert partial.handoff.failure is None and len(partial.handoff.handoffs) == 16
    life = partial.lifecycle
    assert life.completed is False and life.failures == ("PARTIAL_BYTES_UNRESOLVED",)
    a, b = asdict(control.lifecycle), asdict(life)
    assert {k for k in a if a[k] != b[k]} == {"completed", "failures", "reader_partial_bytes",
                                              "ledger"}
    assert {k for k in a["ledger"] if a["ledger"][k] != b["ledger"][k]} == {
        "reader", "local_quiescence_established"}
    assert not partial.local_components_complete and partial.matches_expectation


def test_an_oversized_declared_length_is_refused_and_its_header_stays_retained():
    result = demo.run_demo(REFUSED)
    framing = result.framing
    assert framing.outcome is FramingOutcome.REFUSED and not framing.clean
    assert framing.first_refusal.value == "DECLARED_LENGTH_TOO_LARGE"
    assert [(code.value, count) for code, count in framing.refusals] == [
        ("DECLARED_LENGTH_TOO_LARGE", 1)]
    assert framing.bytes_retained == 4 and framing.frames_handed_off == 16
    assert result.lifecycle.completed is False
    assert result.lifecycle.reader_partial_bytes == "UNRESOLVED"
    assert result.report.outcome == ReadinessOutcome.CONTROL_COMPLETE
    assert not result.local_components_complete


def test_untaken_frames_show_only_in_the_framing_report_and_the_aggregate():
    control, untaken = demo.run_demo(CONTROL), demo.run_demo(UNTAKEN)
    framing = untaken.framing
    assert framing.outcome is FramingOutcome.UNTAKEN_FRAMES and not framing.clean
    assert (framing.bytes_retained, framing.frames_handed_off, framing.frames_queued) == (0, 16, 1)
    assert untaken.handoff.failure is None and not untaken.handoff.completed
    # Nothing is retained, so the lifecycle model, which cannot express untaken frames, completes.
    assert asdict(untaken.lifecycle) == asdict(control.lifecycle)
    assert untaken.lifecycle.completed and not untaken.local_components_complete
    assert untaken.report.outcome == ReadinessOutcome.CONTROL_COMPLETE


def test_an_unknown_socket_closure_stays_unknown_beside_a_complete_buffer():
    result = demo.run_demo(UNKNOWN)
    assert result.framing.clean and result.lifecycle.ledger.socket == "UNKNOWN"
    assert not result.lifecycle.completed and not result.local_components_complete


def test_a_framing_mismatch_alone_is_not_a_match(monkeypatch):
    case = demo.READER_FRAMING_CASES[0]
    altered = demo.DemoScenario(case.name, case.description, case.expected_outcome, case.feed,
                                expected_lifecycle_completed=False, trailing_bytes=case.trailing_bytes,
                                expected_framing_outcome=FramingOutcome.COMPLETE)
    monkeypatch.setattr(demo, "READER_FRAMING_CASES", (altered, *demo.READER_FRAMING_CASES[1:]))
    result = demo.run_demo(case.name)
    assert result.report.outcome == altered.expected_outcome and not result.lifecycle.completed
    assert not result.matches_expectation


def test_reports_and_the_aggregate_carry_codes_and_counts_but_never_payload_bytes():
    results = [demo.run_demo(name) for name in ALL_NAMES]
    text, payload = demo.render_demo_text(results), json.loads(demo.render_demo_json(results))
    assert payload["framing_notice"] == demo.FRAMING_NOTICE and demo.FRAMING_NOTICE in text
    for phrase in ("each read callback", "closed InventedDecoder", "no SDK message format",
                   "requires the lifecycle, the framing, every handoff and every stream's own completion"):
        assert phrase in demo.FRAMING_NOTICE, phrase
    for rendered in (text, json.dumps(payload)):
        assert "invented-demo-read-frame" not in rendered and "invented trailing frame" not in rendered
    for entry, result in zip(payload["scenarios"], results):
        framing = entry["framing_report"]
        assert framing["outcome"] == result.framing.outcome.value
        assert framing["bytes_retained"] == result.framing.bytes_retained
        assert framing["downstream_retirement"] == "NOT_ESTABLISHED"
        assert framing["authority"] == "NON_AUTHORITATIVE"
        local = entry["local_components"]
        assert local["local_components_complete"] is (
            local["lifecycle_completed"] and local["framing_clean"] and local["handoff_completed"]
            and local["streams_completed"])
        assert local["streams_completed"] is all(
            stream["completed"] for stream in entry["callback_ingestion"].values())
        assert local["all_complete"] is (local["local_components_complete"]
                                         and entry["outcome"] == "CONTROL_COMPLETE")
        assert entry["decode_handoff"]["failure"] == (
            None if result.handoff.failure is None else result.handoff.failure.value)
        assert (local["authority"], local["trading_permission"], local["paper_read_1"]) == (
            "NON_AUTHORITATIVE", False, "NOT_ASSESSED")
        assert entry["outcome"] == result.report.outcome.value  # readiness is never reinterpreted


def test_the_extra_cases_are_cli_only_and_the_three_scenarios_are_unchanged():
    assert list(demo.SCENARIOS) == ["callback_control", "delayed_size_mixed_with_live",
                                    "issue_with_repeated_end"]
    assert not {UNKNOWN, CURRENCY_MISSING, MALFORMED_POSITION, *FRAMING} & set(demo.SCENARIOS)
    assert len(demo.ACCOUNT_POSITION_FAILURES) == 2 and len(demo.READER_FRAMING_CASES) == 4
    assert len(demo.HANDOFF_FAILURE_CASES) == 1 and HANDOFF_RAISED not in demo.SCENARIOS
    assert all(s.expected_lifecycle_completed for s in demo.SCENARIOS.values())
    # The currency case's lifecycle completes; the repeated position END is a post-END ledger fault.
    assert [s.expected_lifecycle_completed for s in demo.ACCOUNT_POSITION_FAILURES] == [True, False]
    assert demo.UNKNOWN_CLEANUP.expected_lifecycle_completed is False
    # Each failure case keeps the control's contract and quote callbacks.
    control = demo.SCENARIOS["callback_control"]
    assert all(s.feed is control.feed for s in (*demo.ACCOUNT_POSITION_FAILURES,
                                                *demo.HANDOFF_FAILURE_CASES))


@pytest.mark.parametrize("name", ALL_NAMES)
def test_every_report_is_non_authoritative_without_trading_permission(name):
    result = demo.run_demo(name)
    report = result.report
    assert report.authority == "NON_AUTHORITATIVE" and report.trading_permission is False
    assert report.account_ref != demo.DEMO_EXPECTATION.account_id  # redacted
    life, ledger = result.lifecycle, result.lifecycle.ledger
    assert (life.authority, life.trading_permission, life.paper_read_1) == (
        "NON_AUTHORITATIVE", False, "NOT_ASSESSED")
    assert (ledger.authority, ledger.trading_permission, ledger.paper_read_1) == (
        "NON_AUTHORITATIVE", False, "NOT_ASSESSED")
    assert ledger.broker_side_retirement == "NOT_ESTABLISHED_BY_INSPECTED_SOURCES"


def test_every_stream_is_labelled_with_its_callback_source_and_lifecycle_coverage():
    results = [demo.run_demo(name) for name in ALL_NAMES]
    text, payload = demo.render_demo_text(results), json.loads(demo.render_demo_json(results))
    assert "No envelope is preassembled" in demo.SIMULATION_NOTICE
    for normalizer in ("IbkrCallbackNormalizer", "AccountSummaryCallbacks",
                       "PositionSnapshotCallbacks"):
        assert normalizer in demo.SIMULATION_NOTICE, normalizer
    assert payload["notice"] == demo.SIMULATION_NOTICE and demo.SIMULATION_NOTICE in text
    assert payload["lifecycle_notice"] == demo.LIFECYCLE_NOTICE and demo.LIFECYCLE_NOTICE in text
    for phrase in ("quote-snapshot, account-summary and position callbacks", "returned stop hook",
                   "never an SDK cancellation", "contract-details callbacks go to their normalizer",
                   "directly and are outside the lifecycle model"):
        assert phrase in demo.LIFECYCLE_NOTICE, phrase
    sources = demo.STREAM_SOURCES
    assert list(sources) == STREAMS
    for stream in ("quote_snapshot", "account_summary", "positions"):
        for phrase in ("owned invented frames", "not an SDK wire format", "DecodeHandoff",
                       "adapter consumer", "local lifecycle only"):
            assert phrase in sources[stream], (stream, phrase)
        assert "outside the lifecycle model" not in sources[stream], stream
    assert "broker-style synthetic request id kept" in sources["account_summary"]
    assert "never a broker request id" in sources["positions"]
    assert "outside the lifecycle model" in sources["contract_details"]
    for stream, source in sources.items():
        assert f"   {stream}: completed=" in text and f"      source: {source}" in text, stream
    for entry, result in zip(payload["scenarios"], results):
        assert "preassembled_control_streams" not in entry
        assert entry["stream_sources"] == sources
        assert set(entry["callback_ingestion"]) == set(STREAMS)  # the JSON sorts its keys
        assert entry["callback_ingestion"]["positions"] == {
            "completed": result.positions_completed, "issues": list(result.positions_issues)}
        assert entry["callback_ingestion"]["account_summary"] == {
            "completed": result.account_summary_completed,
            "issues": list(result.account_summary_issues)}
        assert entry["report"]["trading_permission"] is False
        lifecycle = entry["local_lifecycle"]
        assert lifecycle["modelled_streams"] == ["quote_snapshot", "account_summary", "positions"]
        assert set(lifecycle["outside_lifecycle_model"]) == {"contract_details"}
        slots = entry["lifecycle_report"]["ledger"]["slots"]
        assert [s["slot"] for s in slots] == ["ACCOUNT_SUMMARY", "POSITIONS", "QUOTE_SNAPSHOT"]
        # The summary reads the adapter report itself, which the JSON carries unmodified.
        assert entry["lifecycle_report"] == json.loads(json.dumps(asdict(result.lifecycle)))
        assert lifecycle["completed"] is entry["lifecycle_report"]["completed"]
        assert lifecycle["local_quiescence_established"] is (
            entry["lifecycle_report"]["ledger"]["local_quiescence_established"])


def test_json_and_text_rendering_are_deterministic():
    first = [demo.run_demo(name) for name in ALL_NAMES]
    second = [demo.run_demo(name) for name in ALL_NAMES]
    assert demo.render_demo_json(first) == demo.render_demo_json(second)
    assert demo.render_demo_text(first) == demo.render_demo_text(second)


def test_module_cli_runs_all_scenarios_deterministically_and_exits_zero():
    command = [sys.executable, "-B", "-m", "tradingbot.paper_readiness.callback_demo"]
    environment = {**os.environ, "PYTHONPATH": str(SRC), "PYTHONDONTWRITEBYTECODE": "1"}
    runs = [subprocess.run(command, capture_output=True, text=True, env=environment, timeout=60,
                           check=False)
            for _ in range(2)]
    assert [r.returncode for r in runs] == [0, 0] and runs[0].stdout == runs[1].stdout
    out = runs[0].stdout
    assert "11/11 scenarios matched their expected readiness, lifecycle, framing and handoff outcomes" in out
    assert "NON_AUTHORITATIVE; trading_permission=False" in out
    assert "never PAPER-READ-1, broker-side retirement or trading authority" in out
    expected = (("callback_control", "CONTROL_COMPLETE"),
                ("delayed_size_mixed_with_live", "NOT_READY"),
                ("issue_with_repeated_end", "NOT_READY"),
                (UNKNOWN, "CONTROL_COMPLETE"),
                (CURRENCY_MISSING, "NOT_READY"),
                (MALFORMED_POSITION, "NOT_READY"),
                *((name, "CONTROL_COMPLETE") for name in FRAMING),
                (HANDOFF_RAISED, "CONTROL_COMPLETE"))
    for name, outcome in expected:
        assert f"== {name}:" in out and f"expected={outcome} outcome={outcome}" in out
    order = [out.index(f"== {name}:") for name, _ in expected]
    assert order == sorted(order)  # the three scenarios first, then the CLI-only cases
    assert "expected_completed=False completed=False local_quiescence_established=False" in out
    assert "socket=UNKNOWN" in out and "failures=SOCKET_NOT_CLOSED" in out
    assert out.count("completed=True local_quiescence_established=True") == 5
    assert out.count("failures=PARTIAL_BYTES_UNRESOLVED") == 3
    for outcome in ("PARTIAL_HEADER", "PARTIAL_PAYLOAD", "REFUSED", "UNTAKEN_FRAMES"):
        assert f"expected={outcome} outcome={outcome} clean=False" in out, outcome
    # The control alone is complete overall; the currency case completes locally but not in readiness.
    assert out.count("all_complete=True") == 1 and out.count("all_complete=False") == 10
    assert out.count("local_components_complete=True") == 2
    assert "failure=DECODER_RAISED held=16 settled=15/16" in out
    assert out.count("ledger_faults=POST_END_EVENT") == out.count("ledger_faults=LATE_CALLBACK") == 1
    assert "MISSING:ACCOUNT_CURRENCY_MISSING" in out and "INCOMPLETE:POSITIONS_END_MISSING" in out
    assert "positions: completed=False issues=MALFORMED_CALLBACK" in out
    assert runs[0].stderr == ""


def test_main_exit_status_reports_an_unexpected_outcome(monkeypatch, capsys):
    original = demo.SCENARIOS["delayed_size_mixed_with_live"]
    altered = demo.DemoScenario(original.name, original.description, ReadinessOutcome.CONTROL_COMPLETE,
                                original.feed)
    monkeypatch.setitem(demo.SCENARIOS, original.name, altered)
    assert demo.main() == 1
    assert "10/11 scenarios matched" in capsys.readouterr().out


def test_a_readiness_match_alone_is_not_a_match(monkeypatch, capsys):
    """Expecting a completed lifecycle where cleanup is unknown fails, though readiness matches."""
    altered = demo.DemoScenario(UNKNOWN, demo.UNKNOWN_CLEANUP.description,
                                ReadinessOutcome.CONTROL_COMPLETE, demo.UNKNOWN_CLEANUP.feed,
                                socket_closure_known=False, expected_lifecycle_completed=True)
    monkeypatch.setattr(demo, "UNKNOWN_CLEANUP", altered)
    result = demo.run_demo(UNKNOWN)
    assert result.report.outcome == ReadinessOutcome.CONTROL_COMPLETE
    assert result.lifecycle.completed is False and not result.matches_expectation
    assert demo.main() == 1
    assert "10/11 scenarios matched" in capsys.readouterr().out


def test_fixtures_are_invented_here_and_no_held_fixture_or_forbidden_surface_is_used():
    tree = ast.parse(Path(demo.__file__).read_text(encoding="utf-8"))
    imported = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert "tradingbot.paper_readiness.scenarios" not in imported
    assert demo.DEMO_EXPECTATION.account_id.startswith("SYNTH-")
    assert demo.DEMO_CLOCK.tzinfo is not None
    for name in demo.__all__:
        assert not FORBIDDEN_NAME_RE.search(name), name
