"""The Exceptions page lists open exceptions with the action that resolves each, the resolved
history and the halt, and queues operator requests only with the typed confirmation.

Drives ``dashboard/pages/9_Exceptions.py`` headlessly through ``streamlit.testing.v1.AppTest`` on a
state directory produced by the runtime service on invented posts.
"""

from __future__ import annotations

import ast
from pathlib import Path

from runtime_harness import ENTRY, Runtime, post
from streamlit.testing.v1 import AppTest
from test_execution_page import shown, write_config

from tradingbot.execution import operator_catalog as cat
from tradingbot.execution.store import IntentState
from tradingbot.runtime import operator_actions as Q

REPO = Path(__file__).resolve().parents[1]
PAGE = REPO / "dashboard" / "pages" / "9_Exceptions.py"
NOTE = "checked the invented account by hand"


def page(monkeypatch, config: Path) -> AppTest:
    monkeypatch.setenv("TRADINGBOT_RUNTIME_CONFIG", str(config))
    at = AppTest.from_file(str(PAGE), default_timeout=60)
    at.run()
    assert not at.exception
    return at


def unresolved(tmp_path) -> tuple[Runtime, Path, str]:
    rt = Runtime(tmp_path, [post("1", 0, ENTRY)])
    rt.broker.script_next_submit("LOST_ACK_NOT_PLACED")
    rt.run(4, 5)
    [a] = rt.actions()
    return rt, write_config(rt, tmp_path / "runtime.toml"), a["order_ref"]


def test_register_lists_the_exception_its_action_and_the_unresolved_intent(monkeypatch, tmp_path):
    rt, cfg, ref = unresolved(tmp_path)
    text = shown(page(monkeypatch, cfg))
    for needle in (
        "MOCK MODE", "Open exceptions", "UNKNOWN_OUTCOME_ENTRY_NOT_RESENT", "SETTLE",
        "check the order at the broker, then settle its intent", "SIGNAL_UNRESOLVED",
        "Intents of unknown outcome", ref, "TLT STK", "Emergency halt", "Resolved history",
        "UNKNOWN_ENTRY:",
    ):
        assert needle in text, needle


def test_settlement_is_queued_only_with_the_exact_words_and_applied_by_the_service(
    monkeypatch, tmp_path
):
    rt, cfg, ref = unresolved(tmp_path)
    at = page(monkeypatch, cfg)
    at.radio(key="settle_outcome").set_value("NOT_AT_BROKER").run()
    at.text_area(key="settle_note").input(NOTE).run()
    at.text_input(key="settle_confirm").input("SETTLE it").run()
    at.button(key="settle_submit").click().run()
    assert "Not queued" in shown(at)
    queue = Q.OperatorQueue(rt.config.operator_db)
    assert queue.requests() == []
    at.text_input(key="settle_confirm").input(cat.settle_phrase(ref, "NOT_AT_BROKER")).run()
    at.button(key="settle_submit").click().run()
    assert "Queued as request 1" in shown(at)
    [req] = queue.pending()
    assert req["origin"] == "dashboard" and req["outcome"] == "NOT_AT_BROKER"
    rt.step()  # the service applies it, after checking the broker
    assert rt.service.coordinator.intent(ref).state == IntentState.CANCELLED
    text = shown(page(monkeypatch, cfg))
    assert "APPLIED" in text and "No intent of unknown outcome" in text
    assert "UNKNOWN_OUTCOME_ENTRY_NOT_RESENT" in text  # now in the resolved history


def test_a_held_signal_is_acknowledged_through_the_queue(monkeypatch, tmp_path):
    rt, cfg, ref = unresolved(tmp_path)
    at = page(monkeypatch, cfg)
    [label] = [o for o in at.selectbox(key="ack_pick").options if "SIGNAL_UNRESOLVED" in o]
    at.selectbox(key="ack_pick").select(label).run()
    exc_id = int(label.split()[1])
    at.text_area(key="ack_note").input(NOTE).run()
    at.text_input(key="ack_confirm").input(
        cat.acknowledge_signal_phrase(exc_id, "SIGNAL_UNRESOLVED")
    ).run()
    at.button(key="ack_submit").click().run()
    assert "Queued as request" in shown(at)
    rt.step()
    assert Q.OperatorQueue(rt.config.operator_db).requests()[0]["status"] == "APPLIED"


def test_halt_control_works_as_on_the_execution_page(monkeypatch, tmp_path):
    rt, cfg, _ = unresolved(tmp_path)
    halt = rt.config.paths.halt_file
    at = page(monkeypatch, cfg)
    at.button(key="reg_halt_engage").click().run()
    assert halt.exists()
    at = page(monkeypatch, cfg)
    at.text_input(key="reg_halt_confirm").input("resume").run()
    at.button(key="reg_halt_clear").click().run()
    assert halt.exists() and "Not cleared" in shown(at)
    at.text_input(key="reg_halt_confirm").input("RESUME").run()
    at.button(key="reg_halt_clear").click().run()
    assert not halt.exists()


def test_missing_config_is_reported_not_raised(monkeypatch, tmp_path):
    assert "No usable runtime config" in shown(page(monkeypatch, tmp_path / "absent.toml"))


def test_page_has_no_broker_or_order_path():
    tree = ast.parse(PAGE.read_text())
    imported = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module} | {
        a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names
    }
    assert not any("execution" in m or "sim_broker" in m or "service" in m for m in imported)
    source = PAGE.read_text()
    for forbidden in (".submit(", ".modify(", ".cancel(", "SimBroker", "sqlite3.connect",
                      "settle_intent", "apply_pending"):
        assert forbidden not in source
