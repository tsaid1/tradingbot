"""The staged experiment (``tradingbot.ibkr_orders.experiment``, ``scripts/ibkr_paper_experiment.py``)
on the fake transport. No socket is opened. Invented data only."""
from __future__ import annotations

import importlib.util
import json
import socket
import threading
from datetime import UTC, datetime
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
MIDDAY = datetime(2026, 11, 11, 17, 0, tzinfo=UTC)  # 12:00 New York (EST), invented


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
    monkeypatch.setattr(X, "PROTECTION_SETTLE_SECONDS", 3.0)
    monkeypatch.setattr(X, "_wall_clock", lambda: MIDDAY)  # before the start cut-off
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


def test_stage3_refuses_without_a_live_or_delayed_quote_in_stage1(tws, cfg_path):
    stage0(tws, cfg_path)
    run("stage1-lookups", cfg_path)
    assert run("stage2-unfillable-bracket", cfg_path)["passed"]
    cfg = load_order_config(cfg_path)
    p1 = X.evidence_path(cfg, "stage1-lookups")
    data = json.loads(p1.read_text())
    p1.write_text(json.dumps({**data, "quote": {"quality": "FROZEN"}}))
    names = list(tws.names())
    ev = run("stage3-lifecycle", cfg_path)
    assert not ev["passed"] and "FROZEN" in ev["why_failed"]
    assert tws.names() == names and placed(tws) == 2  # refused before connecting


def test_stage3_refuses_when_the_journal_is_not_at_two(tws, cfg_path):
    """Forged stage-2 evidence lets stage 3 start, but the durable count (0) is not the 2 it
    starts from: it refuses before any order."""
    stage0(tws, cfg_path)
    run("stage1-lookups", cfg_path)
    cfg = load_order_config(cfg_path)
    X.evidence_path(cfg, "stage2-unfillable-bracket").write_text(json.dumps(
        {"stage": "stage2-unfillable-bracket", "passed": True, "result": "STAGE2_PASSED"}))
    ev = run("stage3-lifecycle", cfg_path)
    assert not ev["passed"] and "starts from 2" in ev["why_failed"]
    assert placed(tws) == 0 and ev["submissions_after"] == 0


@pytest.mark.parametrize("argv", [[], ["--config", "x.toml"], ["--help"], ["stage4"],
                                  ["all"], ["stage2-unfillable-bracket", "--count", "6"]])
def test_the_script_does_nothing_without_one_explicit_stage(tws, monkeypatch, argv):
    spec = importlib.util.spec_from_file_location("ibkr_paper_experiment",
                                                  ROOT / "scripts" / "ibkr_paper_experiment.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    def never(*a, **k):
        raise AssertionError("run_stage must not be reached")

    monkeypatch.setattr(mod, "run_stage", never)
    with pytest.raises(SystemExit):
        mod.main(argv)
    assert tws.names() == [] and tws.connections == []


def test_stage_names_and_per_stage_budget_are_the_runbooks():
    assert X.STAGES == ("stage0-refusal", "stage1-lookups", "stage2-unfillable-bracket",
                        "stage3-lifecycle")
    assert [X.SUBMISSIONS_OF[s] for s in X.STAGES] == [0, 0, 2, 4]
    assert [X.SUBMISSIONS_BEFORE[s] for s in X.STAGES] == [0, 0, 0, 2]
    assert X.SUBMISSIONS_OF.get(X.ABORT, 0) == 0


# --- fix round after the review of f272640 -------------------------------------------------------
@pytest.mark.parametrize("child_status", ["Inactive", "PendingCancel"])
def test_a_stage2_stop_that_is_not_submitted_is_never_reported_working(tws, cfg_path,
                                                                      child_status):
    """SHOULD FIX 1: only Submitted or PreSubmitted AND visible in open orders is working."""
    from _ibkr_fake_tws_orders import FakeOrderTws
    stage0(tws, cfg_path)
    run("stage1-lookups", cfg_path)
    tws.autofill = True
    tws.ask = 70.00

    def place_then_child_status(s, conn, req):
        FakeOrderTws.place(s, conn, req)
        fo = s.orders[req.orderId]
        if fo.order.parentId and s.orders[fo.order.parentId].status == "Filled":
            s.set_status(fo.order_id, child_status)

    tws.on["PLACE_ORDER"] = place_then_child_status
    ev = run("stage2-unfillable-bracket", cfg_path)
    assert ev["result"] == "STAGE2_UNEXPECTED_FILL"
    assert ev["held"]["stop_working"] is False and ev["held"]["stop_status"] == child_status
    assert "NOT working" in ev["why_failed"] and "Do NOT run abort first" not in ev["why_failed"]


def _to_stage3(tws, cfg_path):
    stage0(tws, cfg_path)
    assert run("stage1-lookups", cfg_path)["passed"]
    assert run("stage2-unfillable-bracket", cfg_path)["passed"]
    tws.autofill = True


def test_a_refused_close_is_reported_refused_and_never_says_abort_first(tws, cfg_path):
    """SHOULD FIX 2 (i, ii): the close is refused for want of a bid: REFUSED with its reason;
    the stop still works, so: close by hand first, then abort."""
    from _ibkr_fake_tws_orders import FakeOrderTws
    _to_stage3(tws, cfg_path)

    def place_then_no_bid(s, conn, req):
        amend = req.orderId in s.orders
        FakeOrderTws.place(s, conn, req)
        if amend:  # the stop raise: from now on the quote has no bid
            s.bid = 0.0

    tws.on["PLACE_ORDER"] = place_then_no_bid
    ev = run("stage3-lifecycle", cfg_path)
    assert not ev["passed"]
    assert ev["result"] == "STAGE3_CLOSE_REFUSED", ev
    assert "REFUSED" in ev["why_failed"] and "did not fill" not in ev["why_failed"]
    assert "EXIT_WITHOUT_QUOTE" in ev["why_failed"]
    assert ev["state"]["stop_working"] is True and ev["state"]["held"] == "1"
    assert "close by hand first, then run abort" in ev["why_failed"]
    assert "run abort, then" not in ev["why_failed"]
    assert "Do NOT run abort first" in ev["why_failed"]
    assert placed(tws) == 5  # the close was never sent


def test_a_position_read_lagging_the_entry_fill_is_waited_for_with_reads_only(tws, cfg_path):
    """SHOULD FIX 2 (iii): the protection check is retried for a bounded time; no extra frame."""
    _to_stage3(tws, cfg_path)
    tws.positions_lag = True
    threading.Timer(1.0, tws.catch_up_positions).start()
    ev = run("stage3-lifecycle", cfg_path)
    assert ev["passed"], ev
    assert placed(tws) == 6 and ev["submissions_after"] == 6


def test_a_position_read_that_keeps_lagging_says_what_is_known(tws, cfg_path):
    _to_stage3(tws, cfg_path)
    tws.positions_lag = True
    ev = run("stage3-lifecycle", cfg_path)
    assert not ev["passed"] and ev["result"] == "STAGE3_PROTECTION_UNCONFIRMED"
    assert ev["state"]["stop_working"] is True
    assert "close by hand first, then run abort" in ev["why_failed"]
    assert placed(tws) == 4  # the bracket only: no raise, no close
    assert tws.count("CANCEL_ORDER") == 1  # stage 2's parent cancel only: nothing new


@pytest.mark.parametrize("stage", ["stage2-unfillable-bracket", "stage3-lifecycle"])
def test_stages_that_place_refuse_to_start_after_the_cut_off(tws, cfg_path, monkeypatch, stage):
    """SHOULD FIX 3: no stage that can leave anything working starts after 15:00 New York."""
    stage0(tws, cfg_path)
    run("stage1-lookups", cfg_path)
    if stage == "stage3-lifecycle":
        assert run("stage2-unfillable-bracket", cfg_path)["passed"]
        tws.autofill = True
    assert X.LATEST_START_NEW_YORK == (15, 0)
    monkeypatch.setattr(X, "_wall_clock", lambda: datetime(2026, 11, 11, 20, 1, tzinfo=UTC))
    names, frames = list(tws.names()), placed(tws)
    ev = run(stage, cfg_path)
    assert not ev["passed"] and "15:00" in ev["why_failed"]
    assert tws.names() == names and placed(tws) == frames  # refused before connecting
    monkeypatch.setattr(X, "_wall_clock", lambda: datetime(2026, 7, 1, 18, 59, tzinfo=UTC))
    assert run(stage, cfg_path)["passed"]  # 14:59 EDT: allowed


def test_stage1_reminds_to_run_the_full_suite_with_the_sdk(tws, cfg_path, capsys):
    spec = importlib.util.spec_from_file_location("ibkr_paper_experiment",
                                                  ROOT / "scripts" / "ibkr_paper_experiment.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    stage0(tws, cfg_path)
    assert mod.main(["stage1-lookups", "--config", str(cfg_path)]) == 0
    out = capsys.readouterr().out
    assert "WITH the SDK" in out and "before stage 2" in out
