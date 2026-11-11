"""The Callback Demo page renders the merged offline demonstration honestly. Synthetic only.

Drives ``dashboard/pages/5_Callback_Demo.py`` headlessly through ``streamlit.testing.v1.AppTest``
and compares what it shows with ``callback_demo.run_demo`` and the existing renderers. The
demonstration itself is covered by tests/test_ibkr_callback_demo.py and is not mirrored here.
"""

from __future__ import annotations

import ast
import http.client
import json
import re
import socket
import tempfile
import urllib.request
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

import tradingbot
from tradingbot.paper_readiness import callback_demo as demo
from tradingbot.paper_readiness.diagnostics import FindingKind, ReadinessOutcome
from tradingbot.paper_readiness.runner import render_text

REPO = Path(__file__).resolve().parents[1]
PAGE = REPO / "dashboard" / "pages" / "5_Callback_Demo.py"
VIEW_DOCS = REPO / "docs" / "CALLBACK_DEMO_VIEW.md"
CONTROL = "callback_control"
FAILURES = {
    "delayed_size_mixed_with_live": ("TICK_SNAPSHOT_END_MISSING",),
    "issue_with_repeated_end": (
        "CONTRACT_DETAILS_END_MISSING",
        "QUOTE_SKIPPED_NO_CONTRACT_IDENTITY",
    ),
}
# Invented input records in callback_demo.py that no page element may show, including the
# account-summary values and the position rows now produced by callbacks.
RAW_INPUTS = ("SYNTH-DEMO0001", "DEMO0001", "250000", "500000", "48.25", "48.27", "48.26",
              "700001", "INVENTED DEMO ETF", "INVENTED-DEMO", "DEMO-LISTING", "DEMOPOSA",
              "DEMOPOSB", "700011", "700012", "-12.5", "300.25", "101.25")
STREAMS = ("contract_details", "quote_snapshot", "account_summary", "positions")
INPUT_WIDGETS = ("text_input", "number_input", "text_area", "selectbox", "multiselect",
                 "checkbox", "toggle", "slider", "select_slider", "file_uploader",
                 "download_button", "chat_input", "date_input", "time_input", "color_picker",
                 "data_editor", "camera_input", "button")


def page(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AppTest:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    return AppTest.from_file(str(PAGE), default_timeout=60)


def select(at: AppTest, name: str) -> AppTest:
    at.radio[0].set_value(name).run()
    assert not at.exception
    return at


def shown(at: AppTest) -> str:
    parts: list[str] = []
    for kind in ("title", "header", "subheader", "error", "warning", "info", "success",
                 "markdown", "caption", "text", "code", "json"):
        parts += [str(e.value) for e in at.get(kind)]
    parts += [f"{m.label} {m.value}" for m in at.metric]
    parts += [e.label for e in at.expander] + [r.label for r in at.radio]
    parts += [str(option) for r in at.radio for option in r.options]
    return "\n".join(parts)


def masked(text: str) -> str:
    """Hide the digests and redacted account reference the page legitimately shows."""
    text = re.sub(r"\b[0-9a-f]{64}\b", "<digest>", text)
    return re.sub(r"SYNTH-\.\.\.[0-9a-f]{8}", "<account_ref>", text)


def metrics(at: AppTest) -> dict[str, str]:
    return {m.label: str(m.value) for m in at.metric}


# Requirement: the page imports only public demo and report surfaces, and touches no network,
# environment, clock, file, transport or input record.


def test_page_source_uses_only_the_demo_and_existing_renderers():
    assert Path(tradingbot.__file__).resolve().parents[2] == REPO and PAGE.is_file()
    tree = ast.parse(PAGE.read_text(encoding="utf-8"))
    modules = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    modules |= {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import)
                for alias in node.names}
    assert modules == {"__future__", "streamlit", "tradingbot.paper_readiness.callback_demo",
                       "tradingbot.paper_readiness.diagnostics",
                       "tradingbot.paper_readiness.runner",
                       "tradingbot.paper_readiness.transport"}, modules
    on_st = {node.func.attr for node in ast.walk(tree)
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
             and isinstance(node.func.value, ast.Name) and node.func.value.id == "st"}
    assert on_st <= {"set_page_config", "title", "error", "warning", "caption", "header", "radio",
                     "subheader", "markdown", "columns", "info", "expander", "code", "divider",
                     "stop"}, on_st
    source = PAGE.read_text(encoding="utf-8")
    for token in ("FakeScript", "FakeReadOnlyTransport", "FakeClock", "run_readiness_check",
                  "_preassembled", "_ACCOUNT", "SYNTH-", "subprocess", "socket", "urllib",
                  "requests", "getenv", "os.environ", "open(", "datetime", "secrets",
                  "query_params"):
        assert token not in source, token


def test_the_only_input_is_the_fixed_three_scenario_selector(tmp_path, monkeypatch):
    at = page(tmp_path, monkeypatch).run()
    assert not at.exception
    assert [r.label for r in at.radio] == ["Scenario"] and at.radio[0].value == CONTROL
    assert [str(o).split(" · ")[0] for o in at.radio[0].options] == list(demo.SCENARIOS)
    assert len(demo.SCENARIOS) == 3
    for widget in INPUT_WIDGETS:
        assert not list(getattr(at, widget, [])), widget


# Requirement: selecting each scenario renders its actual existing result.


@pytest.mark.parametrize("name", list(demo.SCENARIOS))
def test_each_scenario_renders_its_actual_result(tmp_path, monkeypatch, name):
    at = select(page(tmp_path, monkeypatch).run(), name)
    result = demo.run_demo(name)
    report = result.report
    assert metrics(at) == {
        "Expected outcome": result.scenario.expected_outcome.value,
        "Actual outcome": report.outcome.value,
        "Matches expectation": "yes",
        "Non-INFO findings": str(sum(n for k, n in report.counts.items() if k != "INFO")),
    }
    text = shown(at)
    assert f"**`{name}`** — {result.scenario.description}" in text
    for number, (method, digest) in enumerate(zip(report.dispatched, report.observations), start=1):
        assert f"| {number} | `{method}` | {digest.request_id} | {digest.stage.value} |" in text
    for f in report.findings:
        if f.kind != FindingKind.INFO:
            assert f"| {f.stage.value} | `{f.kind.value}` | **{f.code}** |" in text, f.code
    issues = (result.contract_issues + result.quote_issues + result.account_summary_issues
              + result.positions_issues)
    for code in issues:
        assert f"`{code}`" in text
    # One ingestion row per stream, each labelled with the demonstration's own source text.
    for stream in STREAMS:
        assert f"| {stream} | {demo.STREAM_SOURCES[stream]} |" in text, stream
    assert "not applicable" not in text and "PREASSEMBLED" not in text
    codes = [c.value.rstrip("\n") for c in at.code]
    assert render_text(report).rstrip("\n") in codes
    assert demo.render_demo_json([result]).rstrip("\n") in codes
    payload = json.loads(demo.render_demo_json([result]))["scenarios"][0]["report"]
    assert payload["authority"] == "NON_AUTHORITATIVE" and payload["trading_permission"] is False
    assert not at.success and "UNEXPECTED" not in text


# Requirement: the page shows the actual modelled scope, the three read slots and every part of the
# local aggregate, read from the demo's own reports.


@pytest.mark.parametrize("name", list(demo.SCENARIOS))
def test_the_local_read_composition_shows_each_read_slot_and_the_aggregate(tmp_path, monkeypatch, name):
    at = select(page(tmp_path, monkeypatch).run(), name)
    result = demo.run_demo(name)
    text = shown(at)
    assert "Local read composition" in [s.value for s in at.subheader]
    slots = result.lifecycle.ledger.slots
    assert [s.slot for s in slots] == ["ACCOUNT_SUMMARY", "POSITIONS", "QUOTE_SNAPSHOT"]
    for s in slots:
        end, complete = ("yes" if s.end_marker_seen else "NO"), ("yes" if s.read_complete else "NO")
        assert f"| {s.slot} | {s.data_callbacks} | {end} | {complete} | {s.retirement_attempt} |" in text
    handoff = result.handoff
    assert f"{len(handoff.handoffs)}/{len(handoff.handoffs)} settled, failure: none |" in text
    flags = {"lifecycle": result.lifecycle.completed, "framing": result.framing.clean,
             "decode handoff": handoff.completed, "every stream": result.streams_completed,
             "local components": result.local_components_complete,
             "all_complete": result.aggregate_complete}
    for row, flag in flags.items():
        assert f"| {row} | {'yes' if flag else 'NO'} |" in text, row
    assert "| CONTRACT_DETAILS |" not in text  # contract details have no lifecycle slot
    assert result.aggregate_complete is (name == CONTROL)  # one failed stream is never cleared
    note = [c.value for c in at.caption if c.value.startswith("Local and NON_AUTHORITATIVE")]
    assert len(note) == 1
    for phrase in ("returned stop hook, never an SDK cancellation", "requires CONTROL_COMPLETE",
                   "none of it is broker-side retirement, PAPER-READ-1 or trading permission"):
        assert phrase in note[0], phrase


# Requirement: the two failure scenarios cannot display readiness or trading success.


@pytest.mark.parametrize("name, failure_codes", list(FAILURES.items()))
def test_failure_scenarios_never_show_readiness_or_trading_success(
    tmp_path, monkeypatch, name, failure_codes
):
    at = select(page(tmp_path, monkeypatch).run(), name)
    assert metrics(at)["Actual outcome"] == ReadinessOutcome.NOT_READY.value
    verdict = [e.value for e in at.error if e.value.startswith("NOT_READY:")]
    assert len(verdict) == 1 and "trading_permission = False" in verdict[0]
    assert not [w.value for w in at.warning if w.value.startswith("CONTROL_COMPLETE:")]
    assert not at.success
    text = shown(at)
    for code in failure_codes:
        assert f"**{code}**" in text, code
    for word in ("READY FOR", "AUTHORIZED", "APPROVED", "PASSED", "✅", "✔"):
        assert word not in text, word
    if name == "issue_with_repeated_end":
        skipped = "Not dispatched in this scenario (unknown, not passed): `request_quote_snapshot`."
        assert skipped in text


def test_the_control_is_labelled_a_simulation_without_trading_authority(tmp_path, monkeypatch):
    at = page(tmp_path, monkeypatch).run()
    assert metrics(at)["Actual outcome"] == ReadinessOutcome.CONTROL_COMPLETE.value
    assert at.title[0].value == "Callback Demo (OFFLINE SIMULATION)"
    banner = at.error[0].value
    assert banner.startswith("SIMULATION ONLY") and "No trading authority" in banner
    source_note = [w.value for w in at.warning if w.value.startswith("Every stream is produced")]
    assert len(source_note) == 1
    for phrase in ("INVENTED callback values", "none is preassembled",
                   "quote snapshot, account summary and positions callbacks travel",
                   "in owned invented frames", "local lifecycle adapter",
                   "contract details are outside the lifecycle model", "never a broker request id",
                   "not SDK, broker or paper-account proof"):
        assert phrase in source_note[0], phrase
    assert "Only the quote snapshot" not in shown(at)
    verdict = [w.value for w in at.warning if w.value.startswith("CONTROL_COMPLETE:")]
    assert len(verdict) == 1 and "never authorizes trading" in verdict[0]
    assert not at.success and demo.SIMULATION_NOTICE in shown(at)
    assert "**trading_permission = False**" in shown(at)


# Requirement: no input account or value record is exposed, and the page neither connects nor
# writes.


def test_no_input_account_or_value_record_reaches_the_page(tmp_path, monkeypatch):
    at = page(tmp_path, monkeypatch).run()
    for name in demo.SCENARIOS:
        text = masked(shown(select(at, name)))
        for raw in RAW_INPUTS:
            assert raw not in text, (name, raw)
        assert "<account_ref>" in text


def test_the_page_neither_connects_nor_writes(tmp_path, monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError("the page attempted a network connection")

    for target, name in ((socket, "create_connection"), (socket, "getaddrinfo"),
                         (socket.socket, "connect"), (socket.socket, "connect_ex"),
                         (urllib.request, "urlopen"), (http.client.HTTPConnection, "connect")):
        monkeypatch.setattr(target, name, deny)
    at = page(tmp_path, monkeypatch)
    before = {p.relative_to(tmp_path) for p in tmp_path.rglob("*")}
    at.run()
    for name in demo.SCENARIOS:
        select(at, name)
    assert {p.relative_to(tmp_path) for p in tmp_path.rglob("*")} == before


def test_a_demo_failure_halts_safely_without_a_traceback_or_payload(tmp_path, monkeypatch):
    def boom(name):
        raise RuntimeError("SENTINEL-EXC-PAYLOAD account=SYNTH-DEMO0001 bid=48.25")

    monkeypatch.setattr(demo, "run_demo", boom)
    at = page(tmp_path, monkeypatch).run()
    assert not at.exception and not at.metric
    prefix = "Simulation halted safely: RuntimeError"
    halted = [e.value for e in at.error if e.value.startswith(prefix)]
    assert len(halted) == 1 and "nothing was connected, written or authorized" in halted[0]
    text = shown(at)
    for sentinel in ("SENTINEL-EXC-PAYLOAD", "SYNTH-DEMO0001", "48.25"):
        assert sentinel not in text, sentinel


# Requirement: concise run instructions.


def test_view_docs_give_run_instructions_and_limits():
    text = VIEW_DOCS.read_text(encoding="utf-8")
    for token in ("PYTHONPATH=src streamlit run dashboard/app.py",
                  "PYTHONPATH=src streamlit run dashboard/pages/5_Callback_Demo.py",
                  "PYTHONPATH=src python -m pytest tests/test_callback_demo_page.py",
                  "AccountSummaryCallbacks", "PositionSnapshotCallbacks", "local snapshot",
                  "outside the lifecycle model", "Local read composition", "never an SDK cancellation",
                  "trading_permission", "NON_AUTHORITATIVE",
                  "## Limits"):
        assert token in text, token
