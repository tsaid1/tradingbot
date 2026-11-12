"""The staged experiment (``tradingbot.ibkr_orders.experiment``, ``scripts/ibkr_paper_experiment.py``)
on the fake transport. No socket is opened. Invented data only."""
from __future__ import annotations

import importlib.util
import json
import socket
from pathlib import Path

import pytest

pytest.importorskip("ibapi")

import ibapi.client
from _ibkr_fake_tws_orders import PAPER, FakeOrderTws
from _ibkr_paper_testing import OfflineTestSession
from test_ibkr_paper_orders_pure import write_cfg

from tradingbot.ibkr_orders import caps as C
from tradingbot.ibkr_orders import experiment as X
from tradingbot.ibkr_orders.config import load_order_config
from tradingbot.ibkr_orders.counter import init_journal

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def tws(monkeypatch):
    def no_network(*args, **kwargs):
        raise AssertionError("network access attempted in an offline test")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket.socket, "connect_ex", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    server = FakeOrderTws()
    server.server_version = 226
    monkeypatch.setattr(ibapi.client, "Connection", server.connection_factory)
    # test-only seams, reached by patching module attributes inside the test
    monkeypatch.setattr(X, "PaperOrderSession", OfflineTestSession)
    monkeypatch.setattr(X, "READ_BACK_WAIT_SECONDS", 0.1)
    monkeypatch.setattr(X, "POLL_SECONDS", 0.05)
    monkeypatch.setattr(X, "FILL_TIMEOUT_SECONDS", 3.0)
    return server


@pytest.fixture
def cfg_path(tmp_path):
    p = write_cfg(tmp_path)
    cfg = load_order_config(p)
    init_journal(cfg.state_dir, cfg.experiment, cfg.connection.masked_account())
    return p


def placed(tws) -> int:
    return tws.count("PLACE_ORDER")


def run(stage, cfg_path):
    ev, path = X.run_stage(stage, cfg_path)
    assert PAPER not in path.read_text()  # masked account only
    assert json.loads(path.read_text())["passed"] == ev["passed"]
    return ev


def stage0(tws, cfg_path):
    tws.api_settings["readOnlyApi"] = True
    ev = run("stage0-refusal", cfg_path)
    tws.api_settings["readOnlyApi"] = False
    return ev


def test_submission_arithmetic_is_exactly_the_cap():
    assert X.SUBMISSIONS_OF["stage2-unfillable-bracket"] == 2
    assert X.SUBMISSIONS_OF["stage3-lifecycle"] == 4
    assert sum(X.SUBMISSIONS_OF.values()) == C.PAPER_TEST_1.max_submissions == 6
    for stage, before in X.SUBMISSIONS_BEFORE.items():
        i = X.STAGES.index(stage)
        assert before == sum(X.SUBMISSIONS_OF[s] for s in X.STAGES[:i])


def test_stage0_passes_only_when_order_mode_refuses_and_sends_no_order_frame(tws, cfg_path):
    ev = stage0(tws, cfg_path)
    assert ev["passed"] and ev["refused"] and ev["order_frames_sent"] == 0
    assert ev["read_only_settings"]["readOnlyApi"] is True
    assert placed(tws) == 0 and "REQ_IDS" not in tws.names()
    with pytest.raises(X.StageRefused):  # never repeated
        X.run_stage("stage0-refusal", cfg_path)


def test_stage0_with_read_only_off_fails_and_blocks_stage1(tws, cfg_path):
    ev = run("stage0-refusal", cfg_path)
    assert not ev["passed"] and not ev["refused"] and "OFF" in ev["why_failed"]
    with pytest.raises(X.StageRefused):
        X.run_stage("stage1-lookups", cfg_path)
    assert placed(tws) == 0


@pytest.mark.parametrize("stage", X.STAGES[1:])
def test_every_stage_refuses_without_its_predecessor(tws, cfg_path, stage):
    names = list(tws.names())
    with pytest.raises(X.StageRefused):
        X.run_stage(stage, cfg_path)
    assert tws.names() == names  # refused before connecting


def test_a_failed_predecessor_blocks_the_next_stage(tws, cfg_path):
    assert stage0(tws, cfg_path)["passed"]
    cfg = load_order_config(cfg_path)
    path = X.evidence_path(cfg, "stage1-lookups")
    path.write_text(json.dumps({"stage": "stage1-lookups", "passed": False}))
    with pytest.raises(X.StageRefused):
        X.run_stage("stage2-unfillable-bracket", cfg_path)
    assert placed(tws) == 0


def test_stage1_runs_every_lookup_and_places_nothing(tws, cfg_path):
    stage0(tws, cfg_path)
    ev = run("stage1-lookups", cfg_path)
    assert ev["passed"], ev
    assert set(ev["lookups"]) == {"open_orders", "executions", "completed_orders", "positions"}
    assert all(v["completed"] for v in ev["lookups"].values())
    assert ev["account_rows_checked"] and ev["order_frames_sent"] == 0
    assert ev["quote"]["quality"] == "LIVE"
    assert placed(tws) == 0 and ev["submissions_after"] == 0


def test_stages_0_to_3_then_nothing_more(tws, cfg_path):
    stage0(tws, cfg_path)
    assert run("stage1-lookups", cfg_path)["passed"]
    ev2 = run("stage2-unfillable-bracket", cfg_path)
    assert ev2["passed"], ev2
    assert ev2["submissions_before"] == 0 and ev2["submissions_after"] == 2
    assert not ev2["filled"] and ev2["gone_from_open_orders"]
    assert set(ev2["in_completed_orders"].values()) == {"Cancelled"}
    assert ev2["parent_answered_before_child_sent"] is False
    assert {e["event"] for e in ev2["events"]} >= {"SENT_PLACE", "openOrder", "orderStatus",
                                                   "SENT_CANCEL"}
    assert all("at" in e for e in ev2["events"])
    assert placed(tws) == 2 and "REQ_GLOBAL_CANCEL" not in tws.names()
    with pytest.raises(X.StageRefused):  # a stage that submitted can never repeat
        X.run_stage("stage2-unfillable-bracket", cfg_path)

    tws.autofill = True
    ev3 = run("stage3-lifecycle", cfg_path)
    assert ev3["passed"], ev3
    assert [s["step"] for s in ev3["steps"]] == ["open", "raise-stop", "close"]
    assert [s["submissions_used"] for s in ev3["steps"]] == [4, 5, 6]
    assert ev3["submissions_before"] == 2 and ev3["submissions_after"] == 6
    assert ev3["reconcile_clean"] and placed(tws) == 6
    with pytest.raises(X.StageRefused):
        X.run_stage("stage3-lifecycle", cfg_path)


def test_a_failed_stage_that_submitted_cannot_run_again(tws, cfg_path):
    stage0(tws, cfg_path)
    run("stage1-lookups", cfg_path)
    tws.bid = 200.0  # 90% of the bid is above the notional cap: the guard refuses, nothing sent
    ev = run("stage2-unfillable-bracket", cfg_path)
    assert not ev["passed"] and "NOTIONAL_CAP" in ev["error"] and placed(tws) == 0
    tws.bid = 88.10  # nothing was submitted: it may run again
    assert run("stage2-unfillable-bracket", cfg_path)["passed"]
    cfg = load_order_config(cfg_path)
    path = X.evidence_path(cfg, "stage2-unfillable-bracket")
    data = json.loads(path.read_text())
    path.write_text(json.dumps({**data, "passed": False}))  # even if its evidence says failed
    ev = run("stage2-unfillable-bracket", cfg_path)
    assert not ev["passed"] and "starts from 0" in ev["why_failed"]
    assert placed(tws) == 2


def test_abort_cancels_only_this_clients_orders_by_id(tws, cfg_path):
    stage0(tws, cfg_path)
    run("stage1-lookups", cfg_path)
    s = OfflineTestSession(load_order_config(cfg_path))
    s.connect()
    from decimal import Decimal

    from tradingbot.ibkr_orders.records import OrderSpec
    try:
        s.place([OrderSpec("TB-A", "BUY", 1, "LMT", limit_price=Decimal("80.00")),
                 OrderSpec("TB-A.STP", "SELL", 1, "STP", stop_price=Decimal("79.00"))])
    finally:
        s.disconnect()
    ev = run("abort", cfg_path)
    assert ev["passed"], ev
    assert len(ev["found"]) == 2 and ev["remaining_own_working"] == []
    assert "REQ_GLOBAL_CANCEL" not in tws.names() and tws.count("CANCEL_ORDER") >= 1
    assert all(o.status == "Cancelled" for o in tws.orders.values())


def test_the_script_has_no_flag_to_skip_or_repeat(tws, cfg_path, capsys):
    spec = importlib.util.spec_from_file_location("ibkr_paper_experiment",
                                                  ROOT / "scripts" / "ibkr_paper_experiment.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.main(["stage1-lookups", "--config", str(cfg_path)]) == 2  # refused
    with pytest.raises(SystemExit):
        mod.main(["stage2-unfillable-bracket", "--force", "--config", str(cfg_path)])
    tws.api_settings["readOnlyApi"] = True
    assert mod.main(["stage0-refusal", "--config", str(cfg_path)]) == 0
    assert PAPER not in capsys.readouterr().out


def test_an_unexpected_stage2_fill_keeps_the_stop_and_blocks_stage3(tws, cfg_path, capsys):
    stage0(tws, cfg_path)
    run("stage1-lookups", cfg_path)
    tws.autofill = True
    tws.ask = 70.00  # invented: the "unfillable" limit (90% of the bid) is above the ask
    ev = run("stage2-unfillable-bracket", cfg_path)
    assert not ev["passed"] and ev["result"] == "STAGE2_UNEXPECTED_FILL"
    assert "abort" in ev["why_failed"] and "by hand" in ev["why_failed"]
    assert ev["held"]["quantity"] == "1" and ev["held"]["stop_working"] is True
    child = max(o.order_id for o in tws.orders.values())
    assert tws.orders[child].status == "Submitted"  # the protective stop was left working
    assert tws.count("CANCEL_ORDER") == 0
    with pytest.raises(X.StageRefused):
        X.run_stage("stage3-lifecycle", cfg_path)


def test_a_stage2_fill_whose_stop_the_broker_cancelled_reports_it_unprotected(tws, cfg_path):
    """Note (b): the stage never cancels the child after a fill, but the broker may have (with its
    parent). The evidence and the text report the child's actual state, never 'left working'."""
    from _ibkr_fake_tws_orders import FakeOrderTws
    stage0(tws, cfg_path)
    run("stage1-lookups", cfg_path)
    tws.autofill = True
    tws.ask = 70.00  # invented: the "unfillable" limit fills at once

    def place_then_broker_cancels_child(s, conn, req):
        FakeOrderTws.place(s, conn, req)
        fo = s.orders[req.orderId]
        if fo.order.parentId and s.orders[fo.order.parentId].status == "Filled":
            s._set(fo, "Cancelled")
            conn.push(s._status(fo))

    tws.on["PLACE_ORDER"] = place_then_broker_cancels_child
    ev = run("stage2-unfillable-bracket", cfg_path)
    assert ev["result"] == "STAGE2_UNEXPECTED_FILL" and not ev["passed"]
    assert ev["held"]["stop_working"] is False and ev["held"]["stop_status"] == "Cancelled"
    assert "NOT working" in ev["why_failed"] and "UNPROTECTED" in ev["why_failed"]
    assert "left working" not in ev["why_failed"] and "is working" not in ev["why_failed"]
    assert tws.count("CANCEL_ORDER") == 0  # the stage itself cancelled nothing
    with pytest.raises(X.StageRefused):
        X.run_stage("stage3-lifecycle", cfg_path)
