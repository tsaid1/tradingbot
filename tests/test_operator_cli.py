"""scripts/operator.py and scripts/watchdog.py as an operator runs them (subprocess, mock state)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from runtime_harness import ENTRY, Runtime, post
from test_execution_page import write_config

from tradingbot.execution import operator_catalog as cat
from tradingbot.execution.store import ExecutionStore, IntentState

REPO = Path(__file__).resolve().parents[1]
ENV = {**os.environ, "PYTHONPATH": str(REPO / "src")}
NOTE = "checked the invented account by hand"


def run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, *args], check=False, capture_output=True, text=True,
                          env=ENV, cwd=REPO, timeout=120)


def unresolved(tmp_path) -> tuple[Runtime, Path, str]:
    rt = Runtime(tmp_path, [post("1", 0, ENTRY)])
    rt.broker.script_next_submit("LOST_ACK_NOT_PLACED")
    rt.run(4, 5)
    [a] = rt.actions()
    return rt, write_config(rt, tmp_path / "runtime.toml"), a["order_ref"]


def test_cli_lists_queues_and_applies_a_settlement_while_the_service_is_stopped(tmp_path):
    rt, cfg, ref = unresolved(tmp_path)
    out = run("scripts/operator.py", "--config", str(cfg), "list")
    assert out.returncode == 0, out.stderr
    listed = json.loads(out.stdout)
    assert [i["order_ref"] for i in listed["unresolved_intents"]] == [ref]

    # The service still holds the execution store: the request waits for its next cycle.
    good = cat.settle_phrase(ref, "NOT_AT_BROKER")
    out = run("scripts/operator.py", "--config", str(cfg), "--operator", "tester", "settle", ref,
              "NOT_AT_BROKER", "--note", NOTE, "--confirm", good)
    assert out.returncode == 0 and "holds the execution store" in out.stdout

    bad = run("scripts/operator.py", "--config", str(cfg), "settle", ref, "CANCELLED",
              "--note", NOTE, "--confirm", "yes")
    assert bad.returncode == 2 and "type exactly" in bad.stderr

    rt.service.close()  # the service stops; the CLI applies pending requests itself
    out = run("scripts/operator.py", "--config", str(cfg), "apply")
    assert out.returncode == 0, out.stderr
    [result] = json.loads(out.stdout)
    assert result["status"] == "APPLIED" and result["action"] == "SETTLE"
    assert ExecutionStore(rt.config.execution_db).intent(ref).state == IntentState.CANCELLED
    history = json.loads(run("scripts/operator.py", "--config", str(cfg), "history").stdout)
    assert [h["status"] for h in history] == ["APPLIED"]


def test_cli_reports_a_refused_settlement_with_exit_3(tmp_path):
    rt, cfg, ref = unresolved(tmp_path)
    rt.service.close()
    out = run("scripts/operator.py", "--config", str(cfg), "settle", ref, "FILLED",
              "--quantity", "2", "--price", "88.42", "--note", NOTE,
              "--confirm", cat.settle_phrase(ref, "FILLED", 2, "88.42"))
    assert out.returncode == 3, out.stdout + out.stderr
    [result] = json.loads(out.stdout.split("\n", 1)[1])
    assert result["status"] == "REFUSED"
    assert result["reasons"][0].startswith("BROKER_POSITION_CONTRADICTS_FILLED")


def test_watchdog_exits_1_and_alerts_when_the_heartbeat_is_missing(tmp_path):
    rt, cfg, _ = unresolved(tmp_path)
    rt.service.close()
    ok = run("scripts/watchdog.py", "--config", str(cfg), "--max-age", "1000000000")
    assert ok.returncode == 0 and json.loads(ok.stdout)["stale"] is False
    rt.config.heartbeat_file.unlink()
    out = run("scripts/watchdog.py", "--config", str(cfg))
    assert out.returncode == 1 and json.loads(out.stdout)["notified"] == "HEARTBEAT_STALE"
    assert "HEARTBEAT_STALE" in rt.config.alerts_file.read_text()


def test_cli_init_is_the_only_way_to_create_the_queue_and_refuses_to_hide_a_lost_one(tmp_path):
    rt, cfg, ref = unresolved(tmp_path)
    rt.service.close()
    rt.config.operator_db.unlink()
    out = run("scripts/operator.py", "--config", str(cfg), "settle", ref, "NOT_AT_BROKER",
              "--note", NOTE, "--confirm", cat.settle_phrase(ref, "NOT_AT_BROKER"))
    assert out.returncode == 2 and "operator queue unavailable" in out.stderr
    assert not rt.config.operator_db.exists()
    out = run("scripts/operator.py", "--config", str(cfg), "init")
    assert out.returncode == 0, out.stderr
    assert rt.config.operator_db.exists()
    assert run("scripts/operator.py", "--config", str(cfg), "init").returncode == 0  # idempotent
    # Once the execution journal references requests, a lost queue is not silently replaced.
    out = run("scripts/operator.py", "--config", str(cfg), "--operator", "tester", "settle", ref,
              "NOT_AT_BROKER", "--note", NOTE, "--confirm", cat.settle_phrase(ref, "NOT_AT_BROKER"))
    assert out.returncode == 0, out.stderr
    rt.config.operator_db.unlink()
    out = run("scripts/operator.py", "--config", str(cfg), "init")
    assert out.returncode == 2 and "--replace-lost" in out.stderr
    assert not rt.config.operator_db.exists()
    out = run("scripts/operator.py", "--config", str(cfg), "init", "--replace-lost")
    assert out.returncode == 0 and rt.config.operator_db.exists()


def test_cli_reports_a_corrupt_queue_instead_of_crashing(tmp_path):
    rt, cfg, _ = unresolved(tmp_path)
    rt.service.close()
    rt.config.operator_db.write_bytes(b"garbage" * 200)
    out = run("scripts/operator.py", "--config", str(cfg), "history")
    assert out.returncode == 2 and "UNREADABLE" in out.stderr
    out = run("scripts/operator.py", "--config", str(cfg), "list")
    assert out.returncode == 0, out.stderr
