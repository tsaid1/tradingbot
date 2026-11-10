"""The offline callback-driven readiness demonstration. Invented fixtures only; a SIMULATION.

Covers the complete path (invented callbacks -> IbkrCallbackNormalizer -> ResponseEnvelope -> FAKE
transport -> unchanged runner -> NON_AUTHORITATIVE report), the successful control, the two failure
scenarios tied to the PR #25 review defects, and the stable CLI and report semantics.
"""
from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tradingbot.paper_readiness import callback_demo as demo
from tradingbot.paper_readiness.diagnostics import ReadinessOutcome
from tradingbot.paper_readiness.transport import FORBIDDEN_NAME_RE

SRC = Path(__file__).resolve().parents[1] / "src"
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


@pytest.mark.parametrize("name", sorted(demo.SCENARIOS))
def test_every_report_is_non_authoritative_without_trading_permission(name):
    report = demo.run_demo(name).report
    assert report.authority == "NON_AUTHORITATIVE" and report.trading_permission is False
    assert report.account_ref != demo.DEMO_EXPECTATION.account_id  # redacted


def test_account_and_position_streams_are_labelled_preassembled_controls():
    results = [demo.run_demo(name) for name in demo.SCENARIOS]
    text, payload = demo.render_demo_text(results), json.loads(demo.render_demo_json(results))
    assert "PREASSEMBLED CONTROL" in demo.SIMULATION_NOTICE and "not callbacks" in demo.SIMULATION_NOTICE
    assert payload["notice"] == demo.SIMULATION_NOTICE and demo.SIMULATION_NOTICE in text
    for entry in payload["scenarios"]:
        assert entry["preassembled_control_streams"] == ["account_summary", "positions"]
        assert set(entry["callback_ingestion"]) == {"contract_details", "quote_snapshot"}
        assert entry["report"]["trading_permission"] is False


def test_json_and_text_rendering_are_deterministic():
    first = [demo.run_demo(name) for name in demo.SCENARIOS]
    second = [demo.run_demo(name) for name in demo.SCENARIOS]
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
    assert "3/3 scenarios matched their expected outcome" in out
    assert "NON_AUTHORITATIVE; trading_permission=False" in out
    for name, outcome in (("callback_control", "CONTROL_COMPLETE"),
                          ("delayed_size_mixed_with_live", "NOT_READY"),
                          ("issue_with_repeated_end", "NOT_READY")):
        assert f"== {name}:" in out and f"expected={outcome} outcome={outcome}" in out
    assert runs[0].stderr == ""


def test_main_exit_status_reports_an_unexpected_outcome(monkeypatch, capsys):
    original = demo.SCENARIOS["delayed_size_mixed_with_live"]
    altered = demo.DemoScenario(original.name, original.description, ReadinessOutcome.CONTROL_COMPLETE,
                                original.feed)
    monkeypatch.setitem(demo.SCENARIOS, original.name, altered)
    assert demo.main() == 1
    assert "2/3 scenarios matched" in capsys.readouterr().out


def test_fixtures_are_invented_here_and_no_held_fixture_or_forbidden_surface_is_used():
    tree = ast.parse(Path(demo.__file__).read_text(encoding="utf-8"))
    imported = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert "tradingbot.paper_readiness.scenarios" not in imported
    assert demo.DEMO_EXPECTATION.account_id.startswith("SYNTH-")
    assert demo.DEMO_CLOCK.tzinfo is not None
    for name in demo.__all__:
        assert not FORBIDDEN_NAME_RE.search(name), name
