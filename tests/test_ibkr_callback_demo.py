"""The offline callback-driven readiness demonstration. Invented fixtures only; a SIMULATION.

Covers the complete path (invented callbacks -> IbkrCallbackNormalizer -> ResponseEnvelope -> FAKE
transport -> unchanged runner -> NON_AUTHORITATIVE report), the successful control, the two failure
scenarios tied to the PR #25 review defects, and the stable CLI and report semantics. It also
covers the composed local lifecycle: quote callbacks delivered only by the merged LifecycleAdapter's
consumer, and the CLI-only unknown-cleanup counterpart, which stays incomplete beside
CONTROL_COMPLETE.
"""
from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

import pytest

from tradingbot.paper_readiness import callback_demo as demo
from tradingbot.paper_readiness.diagnostics import ReadinessOutcome
from tradingbot.paper_readiness.sdk_lifecycle import LifecycleAdapter
from tradingbot.paper_readiness.transport import FORBIDDEN_NAME_RE

SRC = Path(__file__).resolve().parents[1] / "src"
UNKNOWN = demo.UNKNOWN_CLEANUP.name
ALL_NAMES = [*demo.SCENARIOS, UNKNOWN]
CONTROL_CODES = {"SESSION_ATTESTATION_CONTROL", "CONTRACT_IDENTITY_CONTROL", "REGULAR_SESSION_CONTROL",
                 "QUOTE_LIVE_CONTROL", "ACCOUNT_SNAPSHOT_CONTROL", "POSITIONS_SNAPSHOT_CONTROL"}
ALL_READS = ["request_contract_details", "request_quote_snapshot", "request_account_summary",
             "request_positions"]


def codes(result) -> list[str]:
    return [f.code for f in result.report.findings]


def test_callback_control_completes_through_the_unchanged_runner():
    result = demo.run_demo("callback_control")
    assert result.report.outcome == ReadinessOutcome.CONTROL_COMPLETE and result.matches_expectation
    assert CONTROL_CODES <= set(codes(result))
    assert result.contract_completed and result.quote_completed
    assert result.contract_issues == () and result.quote_issues == ()
    assert result.report.dispatched == ALL_READS
    assert all(o.end_seen for o in result.report.observations)


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
    # Only the quote read is modelled: seven data callbacks, then its END; no stop was needed.
    [slot] = ledger.slots
    assert (slot.slot, slot.end_marker_seen, slot.read_complete) == ("QUOTE_SNAPSHOT", True, True)
    assert (slot.data_callbacks, slot.retirement_attempt) == (7, "NOT_RESERVED")


def test_lifecycle_counts_follow_the_modelled_quote_callbacks():
    """The extra delayed-size callback is one more dispatched DATA; only readiness differs."""
    result = demo.run_demo("delayed_size_mixed_with_live")
    assert result.report.outcome == ReadinessOutcome.NOT_READY and result.lifecycle.completed
    assert result.lifecycle.ledger.slots[0].data_callbacks == 8


def test_quote_envelopes_come_only_from_callbacks_the_adapter_consumer_dispatched(monkeypatch):
    monkeypatch.setattr(LifecycleAdapter, "consume_one", lambda self: False)
    result = demo.run_demo("callback_control")
    assert not result.quote_completed and result.report.outcome == ReadinessOutcome.NOT_READY
    assert "TICK_SNAPSHOT_END_MISSING" in codes(result)
    life, ledger = result.lifecycle, result.lifecycle.ledger
    assert life.completed is False and "QUEUE_NOT_EMPTY" in life.failures
    assert ledger.queued_callbacks == 8 and ledger.consumer == "UNKNOWN"
    # The stop for the unended snapshot returned, and that alone clears nothing.
    assert ledger.slots[0].retirement_attempt == "RETURNED" and not result.matches_expectation


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


def test_the_counterpart_is_cli_only_and_the_three_scenarios_are_unchanged():
    assert list(demo.SCENARIOS) == ["callback_control", "delayed_size_mixed_with_live",
                                    "issue_with_repeated_end"]
    assert UNKNOWN not in demo.SCENARIOS
    assert all(s.expected_lifecycle_completed for s in demo.SCENARIOS.values())
    assert demo.UNKNOWN_CLEANUP.expected_lifecycle_completed is False


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


def test_account_and_position_streams_are_labelled_preassembled_controls():
    results = [demo.run_demo(name) for name in ALL_NAMES]
    text, payload = demo.render_demo_text(results), json.loads(demo.render_demo_json(results))
    assert "PREASSEMBLED CONTROL" in demo.SIMULATION_NOTICE and "not callbacks" in demo.SIMULATION_NOTICE
    assert payload["notice"] == demo.SIMULATION_NOTICE and demo.SIMULATION_NOTICE in text
    assert payload["lifecycle_notice"] == demo.LIFECYCLE_NOTICE and demo.LIFECYCLE_NOTICE in text
    assert "outside the lifecycle model" in demo.LIFECYCLE_NOTICE
    for entry, result in zip(payload["scenarios"], results):
        assert entry["preassembled_control_streams"] == ["account_summary", "positions"]
        assert set(entry["callback_ingestion"]) == {"contract_details", "quote_snapshot"}
        assert entry["report"]["trading_permission"] is False
        lifecycle = entry["local_lifecycle"]
        assert lifecycle["modelled_streams"] == ["quote_snapshot"]
        assert set(lifecycle["outside_lifecycle_model"]) == {"contract_details", "account_summary",
                                                             "positions"}
        slots = entry["lifecycle_report"]["ledger"]["slots"]
        assert [s["slot"] for s in slots] == ["QUOTE_SNAPSHOT"]
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
    assert "4/4 scenarios matched their expected readiness and lifecycle outcomes" in out
    assert "NON_AUTHORITATIVE; trading_permission=False" in out
    assert "never PAPER-READ-1, broker-side retirement or trading authority" in out
    for name, outcome in (("callback_control", "CONTROL_COMPLETE"),
                          ("delayed_size_mixed_with_live", "NOT_READY"),
                          ("issue_with_repeated_end", "NOT_READY"),
                          (UNKNOWN, "CONTROL_COMPLETE")):
        assert f"== {name}:" in out and f"expected={outcome} outcome={outcome}" in out
    assert out.index(f"== {UNKNOWN}:") > out.index("== issue_with_repeated_end:")  # printed last
    assert "expected_completed=False completed=False local_quiescence_established=False" in out
    assert "socket=UNKNOWN" in out and "failures=SOCKET_NOT_CLOSED" in out
    assert out.count("completed=True local_quiescence_established=True") == 3
    assert runs[0].stderr == ""


def test_main_exit_status_reports_an_unexpected_outcome(monkeypatch, capsys):
    original = demo.SCENARIOS["delayed_size_mixed_with_live"]
    altered = demo.DemoScenario(original.name, original.description, ReadinessOutcome.CONTROL_COMPLETE,
                                original.feed)
    monkeypatch.setitem(demo.SCENARIOS, original.name, altered)
    assert demo.main() == 1
    assert "3/4 scenarios matched" in capsys.readouterr().out


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
    assert "3/4 scenarios matched" in capsys.readouterr().out


def test_fixtures_are_invented_here_and_no_held_fixture_or_forbidden_surface_is_used():
    tree = ast.parse(Path(demo.__file__).read_text(encoding="utf-8"))
    imported = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert "tradingbot.paper_readiness.scenarios" not in imported
    assert demo.DEMO_EXPECTATION.account_id.startswith("SYNTH-")
    assert demo.DEMO_CLOCK.tzinfo is not None
    for name in demo.__all__:
        assert not FORBIDDEN_NAME_RE.search(name), name
