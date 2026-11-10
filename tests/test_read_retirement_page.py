"""The Read Retirement page shows local bookkeeping honestly. Invented events only.

Drives ``dashboard/pages/6_Read_Retirement.py`` headlessly through ``streamlit.testing.v1.AppTest``.
Each check targets one operator false-green risk: a reinterpreted report, a local control read as
broker readiness or trading approval, or reader-only and failed or unknown work shown as success.
The ledger itself is covered by tests/test_read_retirement.py.
"""

from __future__ import annotations

import ast
import http.client
import json
import re
import socket
import tempfile
import urllib.request
from dataclasses import asdict
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from tradingbot.paper_readiness.read_retirement import (
    CallbackKind,
    Component,
    Outcome,
    ReadSlot,
    RetirementLedger,
)

REPO = Path(__file__).resolve().parents[1]
PAGE = REPO / "dashboard" / "pages" / "6_Read_Retirement.py"
VIEW_DOCS = REPO / "docs" / "READ_RETIREMENT_VIEW.md"
CONTROL = "valid_local_cessation"
UNRESOLVED = ("reader_stopped_with_outstanding_work", "failed_or_unknown_attempt")
VALUES = {member.value: member for enum in (ReadSlot, CallbackKind, Component, Outcome)
          for member in enum}


def page(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AppTest:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    return AppTest.from_file(str(PAGE), default_timeout=60)


def select(at: AppTest, name: str) -> AppTest:
    at.radio[0].set_value(name).run()
    assert not at.exception
    return at


def shown(at: AppTest) -> str:
    parts: list[str] = []
    for kind in ("title", "header", "subheader", "error", "warning", "info", "success",
                 "markdown", "caption", "code"):
        parts += [str(e.value) for e in at.get(kind)]
    return "\n".join(parts + [f"{m.label} {m.value}" for m in at.metric])


def displayed_report(at: AppTest) -> dict:
    return json.loads(at.code[0].value)


def replayed_from_displayed_steps(at: AppTest) -> dict:
    """An independent fresh ledger, driven by exactly the steps the page lists."""
    steps = next(m.value for m in at.markdown if m.value.startswith("1. `"))
    ledger = RetirementLedger()
    for operation, arguments in re.findall(r"`(\w+)\(([^)]*)\)`", steps):
        values = [VALUES[a.strip()] for a in arguments.split(",") if a.strip()]
        getattr(ledger, operation)(*values)
    return json.loads(json.dumps(asdict(ledger.report())))


# Requirement: the page reproduces the ledger outcome without reinterpretation, freshly per choice.


@pytest.mark.parametrize("name", [CONTROL, *UNRESOLVED])
def test_page_shows_the_ledger_report_for_its_listed_steps(tmp_path, monkeypatch, name):
    at = select(page(tmp_path, monkeypatch).run(), name)
    report = displayed_report(at)
    assert report == replayed_from_displayed_steps(at)
    metrics = {m.label: str(m.value) for m in at.metric}
    assert metrics == {
        "Local quiescence established": "yes" if report["local_quiescence_established"] else "NO",
        "Admission": "closed" if report["admission_closed"] else "OPEN",
        "Queued callbacks": str(report["queued_callbacks"]),
        "In-flight callbacks": str(report["in_flight_callbacks"]),
    }
    assert report["trading_permission"] is False and report["paper_read_1"] == "NOT_ASSESSED"
    assert report["broker_side_retirement"] == "NOT_ESTABLISHED_BY_INSPECTED_SOURCES"


def test_each_selection_replays_a_fresh_ledger(tmp_path, monkeypatch):
    at = page(tmp_path, monkeypatch).run()
    first = displayed_report(at)
    select(at, "failed_or_unknown_attempt")
    select(at, CONTROL)
    assert displayed_report(at) == first


# Requirement: a locally quiescent control is qualified and never reads as readiness or approval.


def test_local_control_is_qualified_and_never_green(tmp_path, monkeypatch):
    at = page(tmp_path, monkeypatch).run()
    assert displayed_report(at)["local_quiescence_established"] is True
    assert at.error[0].value.startswith("SIMULATED EVENTS, LOCAL BOOKKEEPING ONLY")
    prefix = "Local quiescence is established"
    qualified = [w.value for w in at.warning if w.value.startswith(prefix)]
    assert len(qualified) == 1 and "never broker readiness or trading approval" in qualified[0]
    text = shown(at)
    assert "NOT_ESTABLISHED_BY_INSPECTED_SOURCES" in text
    assert "PAPER-READ-1 **NOT_ASSESSED**" in text
    assert "**trading_permission = False**" in text
    assert not at.success
    for word in ("READY FOR", "APPROVED", "AUTHORIZED", "✅"):
        assert word not in text, word


# Requirement: reader-only and failed or unknown work stay incomplete and visibly non-success.


@pytest.mark.parametrize("name", UNRESOLVED)
def test_unresolved_sequences_stay_visibly_incomplete(tmp_path, monkeypatch, name):
    at = select(page(tmp_path, monkeypatch).run(), name)
    report = displayed_report(at)
    assert report["local_quiescence_established"] is False
    assert any(e.value.startswith("Local quiescence is NOT established") for e in at.error)
    assert not any(w.value.startswith("Local quiescence is established") for w in at.warning)
    assert not at.success
    text = shown(at)
    if name == "reader_stopped_with_outstanding_work":
        assert report["reader"] == "STOPPED" and report["queued_callbacks"] == 2
        assert report["in_flight_callbacks"] == 1 and report["admission_closed"] is False
        assert "| Consumer | **UNKNOWN** (unresolved) |" in text
    else:
        attempts = {s["slot"]: s["retirement_attempt"] for s in report["slots"]}
        assert attempts == {"ACCOUNT_SUMMARY": "FAILED", "POSITIONS": "UNKNOWN"}
        assert "**FAILED** (unresolved)" in text and "**UNKNOWN** (unresolved)" in text
        assert any("Fault codes:" in e.value and "`POST_END_EVENT`" in e.value for e in at.error)


# Requirement: no input, file, environment, clock, network, write, SDK or broker path.


def test_page_has_one_fixed_input_and_no_side_channel(tmp_path, monkeypatch):
    source = PAGE.read_text(encoding="utf-8")
    tree = ast.parse(source)
    modules = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    modules |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert modules == {"__future__", "json", "dataclasses", "streamlit",
                       "tradingbot.paper_readiness.read_retirement"}, modules
    # Imports are pinned above; these catch call-level side channels the prose never names.
    for token in ("getenv(", "os.environ", "open(", "time.time", "datetime.", "secrets",
                  "query_params", "ibapi", "EClient", "EWrapper", "subprocess"):
        assert token not in source, token

    def deny(*args, **kwargs):
        raise AssertionError("the page attempted a network connection")

    for target, name in ((socket, "create_connection"), (socket.socket, "connect"),
                         (urllib.request, "urlopen"), (http.client.HTTPConnection, "connect")):
        monkeypatch.setattr(target, name, deny)
    at = page(tmp_path, monkeypatch)
    before = set(tmp_path.rglob("*"))
    at.run()
    assert not at.exception and [r.label for r in at.radio] == ["Sequence"]
    assert [str(o) for o in at.radio[0].options] == [CONTROL, *UNRESOLVED]
    for widget in ("text_input", "number_input", "text_area", "selectbox", "file_uploader",
                   "button", "checkbox", "toggle", "slider", "download_button", "chat_input"):
        assert not list(getattr(at, widget, [])), widget
    assert set(tmp_path.rglob("*")) == before


def test_view_docs_give_run_instructions_and_limits():
    text = VIEW_DOCS.read_text(encoding="utf-8")
    for token in ("PYTHONPATH=src streamlit run dashboard/pages/6_Read_Retirement.py",
                  "PYTHONPATH=src python -m pytest tests/test_read_retirement_page.py",
                  "NOT_ESTABLISHED_BY_INSPECTED_SOURCES", "NOT_ASSESSED", "trading_permission",
                  "## Limits"):
        assert token in text, token
