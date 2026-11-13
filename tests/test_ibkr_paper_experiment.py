"""The staged experiment (``tradingbot.ibkr_orders.experiment``, ``scripts/ibkr_paper_experiment.py``)
on the fake transport. No socket is opened. Invented data only."""
from __future__ import annotations

import importlib.util
import json
import re
import socket
import threading
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("ibapi")

import _clock
import ibapi.client
from _ibkr_fake_tws_orders import PAPER, FakeOrderTws, all_day_hours, core_ref
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
    # Every wall clock the stage, the guard (the session's clock), the execution core, the counter
    # and the fake TWS read starts at MIDDAY and runs from there, whatever the machine's clock says
    # (tests/_clock.py). Before this, only the stage's start check was pinned: the guard and the
    # fake's liquid hours followed the real date, so these tests failed outside 10-12 Nov 2026.
    _clock.install()
    monkeypatch.setattr(_clock.ShiftedDatetime, "offset", _clock.ShiftedDatetime.offset)
    _clock.set_now(MIDDAY)
    server = FakeOrderTws()
    server.server_version = 226
    server.liquid_hours = all_day_hours(MIDDAY)
    monkeypatch.setattr(ibapi.client, "Connection", server.connection_factory)
    # test-only seams, reached by patching module attributes inside the test
    monkeypatch.setattr(X, "PaperOrderSession", OfflineTestSession)
    monkeypatch.setattr(X, "READ_BACK_WAIT_SECONDS", 0.1)
    monkeypatch.setattr(X, "POLL_SECONDS", 0.05)
    monkeypatch.setattr(X, "FILL_TIMEOUT_SECONDS", 3.0)
    monkeypatch.setattr(X, "PROTECTION_SETTLE_SECONDS", 3.0)
    monkeypatch.setattr(X, "POSITION_SETTLE_SECONDS", 2.0)
    monkeypatch.setattr(X, "RECONCILE_SETTLE_SECONDS", 3.0)
    monkeypatch.setattr(X, "QUOTE_RETRY_SECONDS", 1.0)
    monkeypatch.setattr(X, "CLOSE_WAIT_SECONDS", 0.5)
    monkeypatch.setattr(X, "CLOSE_SETTLE_SECONDS", 4.0)
    monkeypatch.setattr(X, "CONFIRM_WAIT_SECONDS", 0.05)
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
    # stage 3 at worst: entry 1 + its stop 1 + one amendment 1 + per close attempt (the closing
    # limit 1 + the stop placed again, a NEW order, 1) x 2 attempts
    assert X.STAGE3_CLOSE_ATTEMPTS == 2
    assert X.SUBMISSIONS_OF["stage3-lifecycle"] == 1 + 1 + 1 + (1 + 1) * 2 == 7
    assert sum(X.SUBMISSIONS_OF.values()) == C.PAPER_TEST_1.max_submissions == 9
    # a clean pass: the first close fills (no stop placed again) or the second does
    assert [X._passing_count(k) for k in (1, 2)] == [6, 8]
    # Every way one close attempt can end, and what it places (closing limit, stop placed again).
    # Only "cancelled unfilled" leads to another attempt; every other ending stops stage 3.
    per_attempt = {
        "fills": (1, 0),  # done
        "cancelled unfilled, stop placed again": (1, 1),  # -> the next attempt (or the end)
        "rejected by the broker, stop placed again": (1, 1),  # ends
        "no usable quote after the netting, stop placed again": (0, 1),  # ends
        "refused by the guard after the stop cancel (ProtectionLost), stop placed again": (0, 1),
        "refused before the netting (the stop never touched)": (0, 0),  # ends
        "acknowledgement lost / cancel unconfirmed: nothing placed again": (1, 0),  # ends
    }
    worst_attempt = max(sum(v) for v in per_attempt.values())
    assert worst_attempt == 2
    before_close = X.SUBMISSIONS_BEFORE["stage3-lifecycle"] + 3  # entry, stop, amendment
    assert before_close + worst_attempt * X.STAGE3_CLOSE_ATTEMPTS == 9
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
    assert ev2["passed"], {k: ev2.get(k) for k in ("why_failed", "error", "result")}
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
    assert [s["step"] for s in ev3["steps"]] == ["open", "raise-stop", "close-1"]
    assert [s["submissions_used"] for s in ev3["steps"]] == [4, 5, 6]
    assert ev3["submissions_before"] == 2 and ev3["submissions_after"] == 6
    assert ev3["reconcile_clean"] and placed(tws) == 6
    close = tws.placed()[-1].order
    assert (close.action, close.orderType, close.tif) == ("SELL", "LMT", "DAY")
    assert close.lmtPrice == pytest.approx(tws.bid)  # at the bid: no 1 % haircut
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
    assert "abort" in ev["why_failed"] and X.TWS_SELL in ev["why_failed"]
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
    assert "is NOT working" in ev["why_failed"] and "is working (" not in ev["why_failed"]
    assert tws.count("CANCEL_ORDER") == 0  # the stage itself cancelled nothing
    with pytest.raises(X.StageRefused):
        X.run_stage("stage3-lifecycle", cfg_path)


@pytest.mark.parametrize("quality", ["FROZEN", "DELAYED"])
def test_stage3_refuses_without_a_live_quote_in_stage1(tws, cfg_path, quality):
    """DELAYED passes stage 1 (unchanged) but no longer qualifies for stage 3."""
    stage0(tws, cfg_path)
    run("stage1-lookups", cfg_path)
    assert run("stage2-unfillable-bracket", cfg_path)["passed"]
    cfg = load_order_config(cfg_path)
    p1 = X.evidence_path(cfg, "stage1-lookups")
    data = json.loads(p1.read_text())
    p1.write_text(json.dumps({**data, "quote": {"quality": quality}}))
    names = list(tws.names())
    ev = run("stage3-lifecycle", cfg_path)
    assert not ev["passed"] and quality in ev["why_failed"] and "needs LIVE" in ev["why_failed"]
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
    assert [X.SUBMISSIONS_OF[s] for s in X.STAGES] == [0, 0, 2, 7]
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
    assert "NOT working" in ev["why_failed"] and "is working (" not in ev["why_failed"]


def _to_stage3(tws, cfg_path):
    stage0(tws, cfg_path)
    assert run("stage1-lookups", cfg_path)["passed"]
    assert run("stage2-unfillable-bracket", cfg_path)["passed"]
    tws.autofill = True


def test_a_refused_close_is_reported_refused_and_never_says_abort_first(tws, cfg_path):
    """SHOULD FIX 2 (i, ii): the close is refused for want of a bid: REFUSED with its reason;
    the stop still works, so: close by hand first, then abort. (Since the owner's exit decision
    the script's own read before the close passes; the core's read then has no bid.)"""
    from _ibkr_fake_tws_orders import FakeOrderTws
    _to_stage3(tws, cfg_path)
    raised = {"reads": 0}
    quote = tws.on["REQ_MKT_DATA"]

    def place(s, conn, req):
        amend = req.orderId in s.orders
        FakeOrderTws.place(s, conn, req)
        if amend:  # the stop raise: the next read is the script's, then no bid
            raised["reads"] = 1

    def read(s, conn, req):
        if raised["reads"]:
            raised["reads"] += 1
            if raised["reads"] > 2:
                s.bid = 0.0
        quote(s, conn, req)

    tws.on["PLACE_ORDER"] = place
    tws.on["REQ_MKT_DATA"] = read
    ev = run("stage3-lifecycle", cfg_path)
    assert not ev["passed"]
    assert ev["result"] == "STAGE3_CLOSE_REFUSED", ev
    assert "REFUSED" in ev["why_failed"] and "did not fill" not in ev["why_failed"]
    assert "EXIT_WITHOUT_QUOTE" in ev["why_failed"]
    assert ev["state"]["stop_working"] is True and ev["state"]["held"] == "1"
    assert X.TWS_PROCEDURE in ev["why_failed"]
    assert "run abort, then" not in ev["why_failed"]
    assert "Never cancel the stop while TWS shows 1" in ev["why_failed"]
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
    assert X.TWS_PROCEDURE in ev["why_failed"]
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
    from _ibkr_fake_tws_orders import all_day_hours
    july = datetime(2026, 7, 1, 18, 59, tzinfo=UTC)
    tws.liquid_hours = all_day_hours() + ";" + all_day_hours(july)  # both clocks inside
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


# --- final fix round after the review of fb0f645 -------------------------------------------------


def _state(read, fills, *, stop=False, close=False, own=0, errors=()):
    agree = read is not None and fills is not None and read == fills
    return {"position_read": read, "own_fills": fills, "agree": agree,
            "held": read if agree else None, "stop_working": stop,
            "stop_status": "Submitted" if stop else None, "close_working": close,
            "own_working": None if "orders" in errors else own, "read_errors": list(errors)}


STATES = {
    "held-stop": _state("1", "1", stop=True, own=1),
    "held-stop-and-close": _state("1", "1", stop=True, close=True, own=2),
    "held-close-no-stop": _state("1", "1", close=True, own=1),
    "held-no-stop": _state("1", "1"),
    "flat-working": _state("0", "0", own=2),
    "flat-nothing": _state("0", "0"),
    "read-1-fills-0": _state("1", "0"),  # the reviewer's probe: the close filled, the read lags
    "read-0-fills-1-stop": _state("0", "1", stop=True, own=1),
    "positions-unreadable": _state(None, None, stop=True, own=1, errors=("positions",)),
    "orders-unreadable": _state("1", "1", errors=("orders",)),
}


def _assert_no_unconditional_sell(text: str) -> None:
    assert "Only if it shows 1" in X.TWS_SELL and "If it shows 0: do NOT sell" in X.TWS_SELL
    rest = text.replace(X.TWS_SELL, "").replace(X.NO_SELL_ON_NUMBERS, "").replace(
        X.WAIT_FOR_CANCELLED, "").replace(X.NO_SALE_WHILE_RUNNING, "")  # prohibitions, not
    # instructions to sell
    assert not re.search(r"\bsell", rest, re.IGNORECASE), rest
    assert "by hand" not in rest.lower(), rest


@pytest.mark.parametrize("ending", sorted(X.ABNORMAL_ENDINGS))
@pytest.mark.parametrize("name", sorted(STATES))
def test_no_ending_prints_an_unconditional_sell_or_a_disputed_position_as_fact(ending, name):
    state = STATES[name]
    text = X._ending_message(ending, "invented cause", state)
    assert text.startswith(ending)
    _assert_no_unconditional_sell(text)
    if not state["agree"]:
        assert not re.search(r"\d+ TLT (is )?held", text), text
        assert "do not sell on this script's numbers" in text
    if state["position_read"] is not None and not state["agree"] and state["own_fills"]:
        assert f"position read {state['position_read']}" in text
        assert f"own fills {state['own_fills']}" in text and "disagree" in text


def test_every_result_code_in_the_experiment_is_a_known_ending():
    src = (ROOT / "src/tradingbot/ibkr_orders/experiment.py").read_text()
    codes = set(re.findall(r'"((?:STAGE[23]|ABORT)_[A-Z_]+)"', src))
    assert codes - {"STAGE2_PASSED", "STAGE3_PASSED"} <= X.ABNORMAL_ENDINGS
    assert X.ABNORMAL_ENDINGS <= codes


def _lag_the_close(tws, *, resolve_after: float | None):
    from _ibkr_fake_tws_orders import FakeOrderTws

    def place(s, conn, req):
        o = req.order
        if req.orderId not in s.orders and o.action == "SELL" and o.orderType == "LMT":
            s.positions_lag = True  # the close fills; the position read still says 1
            if resolve_after is not None:
                threading.Timer(resolve_after, s.catch_up_positions).start()
        FakeOrderTws.place(s, conn, req)

    tws.on["PLACE_ORDER"] = place


def test_a_close_filled_while_the_read_lags_passes_once_the_read_agrees(tws, cfg_path):
    """N1, the reviewer's probe, the lag resolving inside the retry: flat, clean, PASS."""
    _to_stage3(tws, cfg_path)
    _lag_the_close(tws, resolve_after=1.0)
    ev = run("stage3-lifecycle", cfg_path)
    assert ev["passed"], ev
    assert ev["result"] == "STAGE3_PASSED" and placed(tws) == 6


def test_a_close_filled_while_the_read_keeps_lagging_never_says_sell(tws, cfg_path):
    """N1, the lag not resolving: both numbers, 'they disagree', no sell on these numbers."""
    _to_stage3(tws, cfg_path)
    _lag_the_close(tws, resolve_after=None)
    ev = run("stage3-lifecycle", cfg_path)
    assert not ev["passed"] and ev["result"] == "STAGE3_NOT_CLEAN", ev
    assert ev["state"]["position_read"] == "1" and ev["state"]["own_fills"] == "0"
    text = ev["why_failed"]
    assert "position read 1" in text and "own fills 0" in text and "disagree" in text
    assert "do not sell on this script's numbers" in text
    assert not re.search(r"\d+ TLT (is )?held", text)
    _assert_no_unconditional_sell(text)
    assert placed(tws) == 6


def test_the_stage2_fill_endings_carry_only_the_tws_conditioned_sell(tws, cfg_path):
    from _ibkr_fake_tws_orders import FakeOrderTws
    stage0(tws, cfg_path)
    run("stage1-lookups", cfg_path)
    tws.autofill = True
    tws.ask = 70.00

    def place_then_broker_cancels_child(s, conn, req):
        FakeOrderTws.place(s, conn, req)
        fo = s.orders[req.orderId]
        if fo.order.parentId and s.orders[fo.order.parentId].status == "Filled":
            s._set(fo, "Cancelled")
            conn.push(s._status(fo))

    tws.on["PLACE_ORDER"] = place_then_broker_cancels_child
    ev = run("stage2-unfillable-bracket", cfg_path)
    assert ev["result"] == "STAGE2_UNEXPECTED_FILL"
    _assert_no_unconditional_sell(ev["why_failed"])
    assert X.TWS_SELL in ev["why_failed"]


# --- S1: today's broker-reported liquid hours must last 60 more minutes ---------------------------
def _contract(hours, tz="US/Eastern"):
    return SimpleNamespace(liquid_hours=hours, time_zone_id=tz)


@pytest.mark.parametrize(("now", "minutes"), [
    (datetime(2026, 3, 9, 19, 30, tzinfo=UTC), 30),  # 15:30 EDT, the day after DST starts
    (datetime(2026, 3, 6, 20, 30, tzinfo=UTC), 30),  # 15:30 EST, the Friday before
    (datetime(2026, 11, 2, 20, 30, tzinfo=UTC), 30),  # 15:30 EST, the day after DST ends
    (datetime(2026, 10, 30, 19, 30, tzinfo=UTC), 30),  # 15:30 EDT, the Friday before
])
def test_minutes_to_the_close_follow_daylight_saving(now, minutes):
    d = now.astimezone(X.NEW_YORK).strftime("%Y%m%d")
    assert X._minutes_to_close(_contract(f"{d}:0930-{d}:1600"), now) == minutes


def test_a_half_day_refuses_a_start_less_than_an_hour_before_its_close():
    half = _contract("20261127:0930-20261127:1300;20261130:0930-20261130:1600")
    assert X.MIN_MINUTES_TO_CLOSE == 60
    with pytest.raises(X.StageRefused, match="liquid hours"):
        X._check_time_to_close(half, "stage3-lifecycle", datetime(2026, 11, 27, 17, 1, tzinfo=UTC))
    X._check_time_to_close(half, "stage3-lifecycle", datetime(2026, 11, 27, 16, 59, tzinfo=UTC))


def test_a_normal_day_allows_a_start_at_1459():
    day = _contract("20261111:0930-20261111:1600")
    X._check_time_to_close(day, "stage3-lifecycle", datetime(2026, 11, 11, 19, 59, tzinfo=UTC))


@pytest.mark.parametrize("hours,tz", [("", "US/Eastern"), ("garbage", "US/Eastern"),
                                      (None, "US/Eastern"), ("20261111:CLOSED", "US/Eastern"),
                                      ("20261111:0930-20261111:1600", "Nowhere/Invented"),
                                      ("20261111:0930-20261111:1600", "EST"),
                                      ("20261111:0930-20261111:1600", "Etc/GMT+5"),
                                      ("20261112:0930-20261112:1600", "US/Eastern")])
def test_unknown_or_missing_liquid_hours_refuse(hours, tz):
    with pytest.raises(X.StageRefused):
        X._check_time_to_close(_contract(hours, tz), "stage2-unfillable-bracket",
                               datetime(2026, 11, 11, 17, 0, tzinfo=UTC))


@pytest.mark.parametrize("stage", ["stage2-unfillable-bracket", "stage3-lifecycle"])
def test_stages_check_the_brokers_liquid_hours_after_connecting(tws, cfg_path, monkeypatch,
                                                                stage):
    stage0(tws, cfg_path)
    run("stage1-lookups", cfg_path)
    if stage == "stage3-lifecycle":
        assert run("stage2-unfillable-bracket", cfg_path)["passed"]
        tws.autofill = True
    tws.liquid_hours = "20261127:0930-20261127:1300"  # invented half day
    monkeypatch.setattr(X, "_wall_clock", lambda: datetime(2026, 11, 27, 17, 1, tzinfo=UTC))
    frames = placed(tws)
    ev = run(stage, cfg_path)
    assert not ev["passed"] and "liquid hours" in ev["why_failed"]
    assert placed(tws) == frames


# --- follow-up after PR #60 (main f9d7256) ----------------------------------------------------------
def test_stage1_fails_and_stays_repeatable_without_a_live_or_delayed_quote(tws, cfg_path):
    """Item 1: a FROZEN label fails stage 1 (nothing sent); it can run again and then pass."""
    stage0(tws, cfg_path)
    tws.market_data_type = 2  # invented: the adapter labels every sample FROZEN
    ev = run("stage1-lookups", cfg_path)
    assert not ev["passed"] and ev["result"] == "STAGE1_QUOTE_NOT_USABLE"
    assert "FROZEN" in ev["why_failed"] and "no order was sent; run stage 1 again" in ev["why_failed"]
    assert len(ev["quote_samples"]) == X.STAGE1_QUOTE_SAMPLES == 3
    assert all(q["quality"] == "FROZEN" and "reason" in q for q in ev["quote_samples"])
    with pytest.raises(X.StageRefused):
        X.run_stage("stage2-unfillable-bracket", cfg_path)
    tws.market_data_type = 1
    ev = run("stage1-lookups", cfg_path)  # repeatable: it submitted nothing
    assert ev["passed"] and ev["quote"]["quality"] == "LIVE" and len(ev["quote_samples"]) == 1
    assert placed(tws) == 0


def test_stage1_passes_on_the_first_live_or_delayed_sample(tws, cfg_path):
    stage0(tws, cfg_path)
    n = {"req": 0}
    orig = tws.on["REQ_MKT_DATA"]

    def quote(s, conn, req):
        n["req"] += 1
        s.market_data_type = 2 if n["req"] == 1 else 1  # the first sample FROZEN, then LIVE
        orig(s, conn, req)

    tws.on["REQ_MKT_DATA"] = quote
    ev = run("stage1-lookups", cfg_path)
    assert ev["passed"], ev
    assert [q["quality"] for q in ev["quote_samples"]] == ["FROZEN", "LIVE"]
    assert ev["quote"]["quality"] == "LIVE"


def _held_with_working_stop(tws, cfg_path, *, lag=False):
    from decimal import Decimal

    from tradingbot.ibkr_orders.records import OrderSpec
    s = OfflineTestSession(load_order_config(cfg_path))
    s.connect()
    try:
        snaps = s.place([OrderSpec("TB-A", "BUY", 1, "LMT", limit_price=Decimal("80.00")),
                         OrderSpec("TB-A.STP", "SELL", 1, "STP", stop_price=Decimal("79.00"))])
        tws.positions_lag = lag
        tws.fill(snaps[0].order_id, 1, 80.00)  # the entry fills after "run abort" was printed
    finally:
        s.disconnect()
    return snaps


@pytest.mark.parametrize("lag", [False, True], ids=["held", "read-lags-the-fill"])
def test_abort_never_cancels_the_stop_of_a_held_or_disputed_share(tws, cfg_path, lag):
    """Item 2: abort re-reads first; held, disputed or unreadable: the stop stays."""
    stage0(tws, cfg_path)
    run("stage1-lookups", cfg_path)
    snaps = _held_with_working_stop(tws, cfg_path, lag=lag)
    ev = run("abort", cfg_path)
    assert not ev["passed"] and ev["result"] == "ABORT_STOP_LEFT_WORKING"
    assert tws.orders[snaps[1].order_id].status == "Submitted"  # the stop still protects
    assert tws.count("CANCEL_ORDER") == 0
    assert "left working" in ev["why_failed"] and X.TWS_SELL in ev["why_failed"]
    _assert_no_unconditional_sell(ev["why_failed"])


def test_the_tws_procedure_checks_trades_and_aborts_right_after_a_manual_sale():
    assert "Trades panel" in X.TWS_PROCEDURE
    assert "run abort at once" in X.TWS_PROCEDURE and "still working" in X.TWS_PROCEDURE


def _bump_ask_on_entry(tws):
    def place(s, conn, req):
        if req.orderId not in s.orders and req.order.action == "BUY" and not req.order.parentId:
            s.ask = round(s.ask + 0.01, 2)  # the ask ticks up before the entry is live
        FakeOrderTws.place(s, conn, req)
    tws.on["PLACE_ORDER"] = place


@pytest.mark.parametrize(("ticks", "limit"), [(None, 80.73), (0, 80.73), (3, 80.76)])
def test_stage3_states_its_entry_as_the_ask_plus_the_owners_allowance(tws, tmp_path, ticks,
                                                                     limit):
    """Item 3: absent (today's behaviour) or 0: exactly the ask; 3 ticks: ask + 0.03. The limit on
    the wire is the stated price (the core adds nothing)."""
    p = write_cfg(tmp_path, **({} if ticks is None else {"stage3_entry_allowance_ticks": ticks}))
    cfg = load_order_config(p)
    init_journal(cfg.state_dir, cfg.experiment, cfg.connection.masked_account())
    stage0(tws, p)
    assert run("stage1-lookups", p)["passed"]
    assert run("stage2-unfillable-bracket", p)["passed"]
    tws.bid, tws.ask = 80.71, 80.73  # invented
    tws.autofill = True
    ev = run("stage3-lifecycle", p)
    assert ev["passed"], ev
    entry = tws.placed()[2]
    assert entry.order.action == "BUY" and entry.order.lmtPrice == pytest.approx(limit)
    assert ev["entry_price"] == {"ask": "80.73", "allowance_ticks": ticks or 0, "tick": "0.01",
                                 "stated_entry": f"{limit:.2f}"}
    assert placed(tws) == 6


@pytest.mark.parametrize(("ticks", "fills"), [(0, False), (3, True)])
def test_an_allowance_absorbs_a_one_tick_rise_of_the_ask(tws, tmp_path, ticks, fills):
    p = write_cfg(tmp_path, stage3_entry_allowance_ticks=ticks)
    cfg = load_order_config(p)
    init_journal(cfg.state_dir, cfg.experiment, cfg.connection.masked_account())
    stage0(tws, p)
    run("stage1-lookups", p)
    run("stage2-unfillable-bracket", p)
    tws.bid, tws.ask = 80.71, 80.73
    tws.autofill = True
    _bump_ask_on_entry(tws)
    ev = run("stage3-lifecycle", p)
    assert ev["passed"] is fills
    if not fills:
        assert ev["result"] == "STAGE3_ENTRY_NOT_FILLED"


# --- the owner's exit decision (13 Nov 2026): the close at the bid, bounded, protected ----------
def _close_fills_on(tws, attempt):
    """The fake fills a closing limit only from close attempt ``attempt`` on (None: never)."""
    n = {"close": 0}

    def place(s, conn, req):
        o, auto = req.order, s.autofill
        if req.orderId not in s.orders and o.action == "SELL" and o.orderType == "LMT":
            n["close"] += 1
            s.autofill = attempt is not None and n["close"] >= attempt
        try:
            FakeOrderTws.place(s, conn, req)
        finally:
            s.autofill = auto

    tws.on["PLACE_ORDER"] = place
    return n


def _sent(tws):
    return [(p.order.action, p.order.orderType, p.order.parentId != 0) for p in tws.placed()]


def test_the_first_close_does_not_fill_and_the_second_does(tws, cfg_path):
    _to_stage3(tws, cfg_path)
    _close_fills_on(tws, 2)
    ev = run("stage3-lifecycle", cfg_path)
    assert ev["passed"] and ev["result"] == "STAGE3_PASSED", ev
    assert ev["close_filled_on_attempt"] == 2 and ev["submissions_after"] == 8
    assert _sent(tws)[2:] == [("BUY", "LMT", False), ("SELL", "STP", True),  # the bracket
                              ("SELL", "STP", True),  # the amendment (same order id)
                              ("SELL", "LMT", False),  # close 1
                              ("SELL", "STP", False),  # the stop placed again: a new order
                              ("SELL", "LMT", False)]  # close 2
    first, second = ev["close_attempts"]
    assert first["outcome"] == "CANCELLED" and first["cancel_confirmed"] is True
    assert "EXIT_UNFILLED_CANCELLED" in first["reasons"]
    assert first["protection_after"] == "CONFIRMED" and first["stop_working_after"] is True
    assert first["stop_ref_after"].endswith(".STP2")
    assert (first["submissions_before"], first["submissions_after"]) == (5, 7)
    assert second["outcome"] == "FILLED" and second["submissions_after"] == 8
    for a in (first, second):
        q = a["quote"]
        assert q["usable"] and q["quality"] == "LIVE"
        assert 0 <= q["bid_age_seconds"] <= 3 and 0 <= q["ask_age_seconds"] <= 3
        assert Decimal(a["limit"]) == Decimal(q["bid"])  # at the bid read before it
        assert a["core_quote_reads"] and a["wait_seconds"] == X.CLOSE_WAIT_SECONDS
    restored = tws.placed()[6].order
    assert restored.auxPrice == pytest.approx(float(Decimal(ev["entry_price"]["stated_entry"])
                                                    - 1 + Decimal("0.1")))  # the raised stop
    assert not [o for o in tws.orders.values() if o.working]
    assert ev["reconcile_clean"]


def test_neither_close_fills_the_share_is_held_and_the_stop_confirmed(tws, cfg_path):
    _to_stage3(tws, cfg_path)
    _close_fills_on(tws, None)
    ev = run("stage3-lifecycle", cfg_path)
    assert not ev["passed"] and ev["result"] == "STAGE3_CLOSE_NOT_FILLED", ev
    assert placed(tws) == 9 and ev["submissions_after"] == 9 == C.PAPER_TEST_1.max_submissions
    assert [a["outcome"] for a in ev["close_attempts"]] == ["CANCELLED", "CANCELLED"]
    assert all(a["protection_after"] == "CONFIRMED" for a in ev["close_attempts"])
    assert ev["state"]["held"] == "1" and ev["state"]["stop_working"] is True
    assert not ev["state"]["close_working"]
    text = ev["why_failed"]
    assert "a protective stop is working (Submitted) as read" in text
    assert "Protective stop working (Submitted)" in text and X.TWS_PROCEDURE in text
    _assert_no_unconditional_sell(text)
    working = [o for o in tws.orders.values() if o.working]
    assert [(o.order.action, o.order.orderType) for o in working] == [("SELL", "STP")]


def test_a_close_cancel_that_is_not_confirmed_sends_nothing_more(tws, cfg_path):
    _to_stage3(tws, cfg_path)
    _close_fills_on(tws, None)
    cancel = tws.on["CANCEL_ORDER"]

    def ignore_close_cancels(s, conn, req):
        fo = s.orders.get(req.orderId)
        if fo is not None and fo.order.action == "SELL" and fo.order.orderType == "LMT":
            return  # invented: TWS never answers the closing limit's cancel
        cancel(s, conn, req)

    tws.on["CANCEL_ORDER"] = ignore_close_cancels
    ev = run("stage3-lifecycle", cfg_path)
    assert not ev["passed"] and ev["result"] == "STAGE3_CLOSE_OUTCOME_UNKNOWN", ev
    assert placed(tws) == 6  # no stop placed again, no second close
    [a] = ev["close_attempts"]
    assert a["cancel_requested"] and not a["cancel_confirmed"] and a["outcome"] == "WORKING"
    text = ev["why_failed"]
    assert "MAY STILL BE WORKING OR MAY HAVE FILLED" in text and "UNPROTECTED" in text
    assert "A closing limit of this client is resting" in text and X.TWS_PROCEDURE in text
    _assert_no_unconditional_sell(text)


def test_a_stop_that_is_not_placed_again_says_possibly_unprotected_first(tws, cfg_path):
    _to_stage3(tws, cfg_path)
    _close_fills_on(tws, None)
    place = tws.on["PLACE_ORDER"]

    def reject_restored_stop(s, conn, req):
        o = req.order
        if req.orderId not in s.orders and o.orderType == "STP" and not o.parentId:
            s.reject_refs[core_ref(o.orderRef)] = (201, "Order rejected - reason: invented")
        place(s, conn, req)

    tws.on["PLACE_ORDER"] = reject_restored_stop
    ev = run("stage3-lifecycle", cfg_path)
    assert not ev["passed"] and ev["result"] == "STAGE3_PROTECTION_NOT_RESTORED", ev
    assert ev["why_failed"].startswith("STAGE3_PROTECTION_NOT_RESTORED: SHARE POSSIBLY UNPROTECTED")
    assert placed(tws) == 7  # the rejected stop counts; nothing after it
    assert ev["state"]["stop_working"] is False
    _assert_no_unconditional_sell(ev["why_failed"])


def test_no_usable_quote_before_the_entry_sends_nothing_and_stage3_may_run_again(tws, cfg_path):
    _to_stage3(tws, cfg_path)
    tws.market_data_type = 3  # invented: delayed only now; stage 1 had LIVE
    ev = run("stage3-lifecycle", cfg_path)
    assert not ev["passed"] and ev["result"] == "STAGE3_NO_USABLE_QUOTE", ev
    assert placed(tws) == 2 and len(ev["entry_quote_reads"]) >= 2  # retried, reads only
    assert all(not r["usable"] for r in ev["entry_quote_reads"])
    tws.market_data_type = 1
    assert run("stage3-lifecycle", cfg_path)["passed"]


def test_no_usable_quote_before_a_close_sends_no_close_and_keeps_the_stop(tws, cfg_path):
    _to_stage3(tws, cfg_path)

    def place(s, conn, req):
        amend = req.orderId in s.orders
        FakeOrderTws.place(s, conn, req)
        if amend:
            s.market_data_type = 3  # invented: delayed only from now on

    tws.on["PLACE_ORDER"] = place
    ev = run("stage3-lifecycle", cfg_path)
    assert not ev["passed"] and ev["result"] == "STAGE3_CLOSE_NO_QUOTE", ev
    assert placed(tws) == 5 and tws.count("CANCEL_ORDER") == 1  # stage 2's only
    assert ev["state"]["stop_working"] is True and ev["state"]["held"] == "1"
    assert ev["close_attempts"][0]["outcome"] == "NOT_SENT_NO_USABLE_QUOTE"


def test_a_stale_side_is_retried_with_reads_only(tws, cfg_path, monkeypatch):
    _to_stage3(tws, cfg_path)

    class Later(datetime):  # the script's reference 10 s after the reads: every side too old
        @classmethod
        def now(cls, tz=None):
            return datetime.now(tz) + timedelta(seconds=10)

    monkeypatch.setattr(X, "datetime", Later)
    ev = run("stage3-lifecycle", cfg_path)
    assert ev["result"] == "STAGE3_NO_USABLE_QUOTE" and placed(tws) == 2
    assert all(r["quality"] == "LIVE" and not r["usable"] for r in ev["entry_quote_reads"])


def test_the_abort_note_says_cancel_the_stop_in_tws_first():
    assert X.ABORT_STOP_NOTE.startswith("Cancel that TLT stop in the TWS window")


def test_every_wall_clock_the_stage_reads_is_the_tests(tws):
    """No test here depends on the machine's date or time of day: the stage, the session (the
    guard's clock), the read-only session, the counter, the execution core and the fake TWS all
    read the fixture's moment."""
    import _ibkr_fake_tws_orders as fake

    from tradingbot.execution import coordinator, store
    from tradingbot.ibkr import session as read_session
    from tradingbot.ibkr_orders import counter
    from tradingbot.ibkr_orders import session as order_session
    for mod in (X, order_session, read_session, counter, coordinator, store, fake):
        assert abs((mod.datetime.now(UTC) - MIDDAY).total_seconds()) < 60, mod.__name__
    assert X._wall_clock() == MIDDAY
    assert MIDDAY.strftime("%Y%m%d") in tws.liquid_hours


# --- review round: priced after the netting; the stop back after any unflat exit ----------------
def _after_the_stop_cancel(tws, then):
    """``then(s)`` runs once the stop has been cancelled for the close (invented market move)."""
    cancel = tws.on["CANCEL_ORDER"]

    def hook(s, conn, req):
        fo = s.orders.get(req.orderId)
        cancel(s, conn, req)
        if fo is not None and fo.order.orderType == "STP" and fo.status == "Cancelled":
            then(s)

    tws.on["CANCEL_ORDER"] = hook


def test_the_close_is_priced_from_the_bid_read_after_the_stop_cancel(tws, cfg_path):
    _to_stage3(tws, cfg_path)
    _after_the_stop_cancel(tws, lambda s: setattr(s, "bid", 88.05))
    ev = run("stage3-lifecycle", cfg_path)
    assert ev["passed"], ev
    assert tws.placed()[-1].order.lmtPrice == pytest.approx(88.05)  # not the earlier 88.10
    [a] = ev["close_attempts"]
    assert a["limit"] == "88.05" and a["priced_after_netting"]["sent"] == "88.05"
    assert a["priced_after_netting"]["before_netting"] == "88.1"


def test_no_usable_quote_after_the_netting_sends_no_close_and_the_stop_is_back(tws, cfg_path,
                                                                             monkeypatch):
    import tradingbot.execution.coordinator as core
    monkeypatch.setattr(core, "EXIT_REQUOTE_SECONDS", 0.3)
    _to_stage3(tws, cfg_path)
    _after_the_stop_cancel(tws, lambda s: setattr(s, "market_data_type", 3))  # delayed only
    ev = run("stage3-lifecycle", cfg_path)
    assert not ev["passed"] and ev["result"] == "STAGE3_CLOSE_REFUSED", ev
    assert placed(tws) == 6  # 5, then the stop placed again; no closing limit
    assert _sent(tws)[-1] == ("SELL", "STP", False)
    text = ev["why_failed"]
    assert "EXIT_WITHOUT_QUOTE" in text and "AFTER_NETTING" in text
    assert "protective stop is working (Submitted) as read" in text
    assert ev["state"]["stop_working"] is True and ev["state"]["held"] == "1"
    _assert_no_unconditional_sell(text)


def test_a_close_the_broker_rejects_ends_with_the_stop_back(tws, cfg_path):
    _to_stage3(tws, cfg_path)
    place = tws.on["PLACE_ORDER"]

    def reject_close(s, conn, req):
        o = req.order
        if req.orderId not in s.orders and o.action == "SELL" and o.orderType == "LMT":
            s.reject_refs[core_ref(o.orderRef)] = (201, "Order rejected - reason: invented")
        place(s, conn, req)

    tws.on["PLACE_ORDER"] = reject_close
    ev = run("stage3-lifecycle", cfg_path)
    assert not ev["passed"] and ev["result"] == "STAGE3_CLOSE_REFUSED", ev
    assert placed(tws) == 7  # the rejected close counts, then the stop placed again
    assert "protective stop is working (Submitted) as read" in ev["why_failed"]
    assert ev["state"]["stop_working"] is True


def test_a_close_refused_after_the_stop_cancel_reports_the_stop_back_as_read(tws, cfg_path,
                                                                            monkeypatch):
    from tradingbot.ibkr_orders.guard import Refusal, RefusalReason
    from tradingbot.ibkr_orders.records import OrderRefused

    class RefusingSession(OfflineTestSession):
        def place(self, specs):
            if specs[0].action == "SELL" and specs[0].order_type == "LMT":
                raise OrderRefused(Refusal(RefusalReason.OUTSIDE_LIQUID_HOURS, "invented"))
            return super().place(specs)

    _to_stage3(tws, cfg_path)
    monkeypatch.setattr(X, "PaperOrderSession", RefusingSession)
    ev = run("stage3-lifecycle", cfg_path)
    assert not ev["passed"] and ev["result"] == "STAGE3_PROTECTION_LOST", ev
    assert "POSITION_OPEN_PROTECTION_ABSENT" in ev["protection_lost"]
    assert placed(tws) == 6  # no closing limit reached the wire; the stop placed again
    text = ev["why_failed"]
    assert "the core placed the protective stop again" in text
    assert "protective stop is working (Submitted) as read" in text
    _assert_no_unconditional_sell(text)


def test_abort_with_no_stop_says_so(tws, cfg_path):
    from tradingbot.ibkr_orders.records import OrderSpec
    stage0(tws, cfg_path)
    run("stage1-lookups", cfg_path)
    s = OfflineTestSession(load_order_config(cfg_path))
    s.connect()
    try:
        [snap] = s.place([OrderSpec("TB-A", "BUY", 1, "LMT", limit_price=Decimal("80.00"))])
        tws.fill(snap.order_id, 1, 80.00)  # held, and no stop at all (invented)
    finally:
        s.disconnect()
    ev = run("abort", cfg_path)
    assert ev["result"] == "ABORT_NOT_FLAT_NO_STOP" and ev["left_working"] == []
    assert "no protective stop of this client is working" in ev["why_failed"]
    assert "stop(s) []" not in ev["why_failed"] and X.ABORT_STOP_NOTE not in ev["why_failed"]


# --- review round 2: no manual sale beside a closing limit still pending its cancel -------------
def test_the_procedure_waits_for_the_closing_limit_to_show_cancelled():
    assert X.WAIT_FOR_CANCELLED in X.TWS_PROCEDURE
    # W1: the rule does not depend on who asked for the cancel (abort, the core or the owner)
    assert X.WAIT_FOR_CANCELLED == (
        "While any TLT closing limit of this experiment shows working or PendingCancel in the TWS "
        "window (whoever cancelled it), do not sell by hand and do nothing more; wait until it "
        "shows Cancelled or Filled.")
    assert "If you cancelled one" not in X.TWS_PROCEDURE
    assert "only if you can see it working there, never the stop" in X.TWS_PROCEDURE
    # the plain rule, once, at the top
    assert X.TWS_PROCEDURE.startswith(X.NO_SALE_WHILE_RUNNING)
    assert X.NO_SALE_WHILE_RUNNING == "Never sell by hand in TWS while this script is still running."
    assert X.TWS_PROCEDURE.count(X.NO_SALE_WHILE_RUNNING) == 1
    # step (2) comes before the sale in step (3)
    assert X.TWS_PROCEDURE.index(X.WAIT_FOR_CANCELLED) < X.TWS_PROCEDURE.index(X.TWS_SELL)
    assert "ABORT_NOT_FLAT_NO_STOP" in X.ABNORMAL_ENDINGS


def test_abort_reports_cancels_requested_apart_from_cancels_confirmed(tws, cfg_path):
    """B2: TWS never answers the closing limit's cancel; stage 3 ends OUTCOME_UNKNOWN, then abort
    asks again. It must not say "cancelled" for a request a later read does not confirm."""
    _to_stage3(tws, cfg_path)
    _close_fills_on(tws, None)
    cancel = tws.on["CANCEL_ORDER"]

    def ignore_close_cancels(s, conn, req):
        fo = s.orders.get(req.orderId)
        if fo is not None and fo.order.action == "SELL" and fo.order.orderType == "LMT":
            return
        cancel(s, conn, req)

    tws.on["CANCEL_ORDER"] = ignore_close_cancels
    ev3 = run("stage3-lifecycle", cfg_path)
    assert ev3["result"] == "STAGE3_CLOSE_OUTCOME_UNKNOWN"
    assert X.WAIT_FOR_CANCELLED in ev3["why_failed"]
    ev = run("abort", cfg_path)
    assert ev["result"] == "ABORT_NOT_FLAT_NO_STOP", ev
    assert len(ev["cancels_requested"]) == 1 and ev["cancels_confirmed"] == []
    assert len(ev["cancels_unconfirmed"]) == 1
    text = ev["why_failed"]
    assert "asked TWS to cancel 1 other order(s); 0 confirmed cancelled by a later read" in text
    assert "NOT confirmed" in text and "cancelled 1 other" not in text
    assert "A closing limit of this client is resting" in text  # the state read AFTER the cancel
    assert X.WAIT_FOR_CANCELLED in text
    _assert_no_unconditional_sell(text)


def test_abort_says_cancelled_only_for_a_cancel_a_later_read_confirms(tws, cfg_path):
    _to_stage3(tws, cfg_path)
    _close_fills_on(tws, None)
    from tradingbot.ibkr_orders.records import OrderSpec
    s = OfflineTestSession(load_order_config(cfg_path))
    s.connect()
    try:
        s.place([OrderSpec("TB-A", "BUY", 1, "LMT", limit_price=Decimal("80.00"))])
    finally:
        s.disconnect()
    ev = run("abort", cfg_path)
    assert ev["passed"], ev  # flat: every working order cancelled, confirmed by the read after
    assert len(ev["cancels_requested"]) == len(ev["cancels_confirmed"]) == 1
