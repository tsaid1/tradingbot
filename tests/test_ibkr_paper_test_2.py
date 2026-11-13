"""The experiment definition ``paper-test-2`` (YOUR CALL 17; NOT authorized, disabled by default)
on the fake transport. No socket is opened. Invented data only.

Every ending of its stages 2 (single unfillable order), 3 (unfillable bracket) and 4 (lifecycle
with the protective stop verified after the entry fills), the acknowledgement-timeout policy in
both settings, the gating, and that paper-test-1 is unchanged."""
from __future__ import annotations

import time
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

pytest.importorskip("ibapi")

from _ibkr_fake_tws_orders import TERMINAL, core_ref
from _ibkr_paper_testing import OfflineTestSession
from test_ibkr_paper_experiment import cfg_path, run, tws  # noqa: F401 - fixtures
from test_ibkr_paper_orders_pure import write_cfg

from tradingbot.ibkr_orders import caps as C
from tradingbot.ibkr_orders import experiment as X
from tradingbot.ibkr_orders.config import load_order_config
from tradingbot.ibkr_orders.counter import init_journal
from tradingbot.ibkr_orders.records import OrderSpec

S0, S1, S2, S3, S4 = X.PAPER_TEST_2_STAGES


@pytest.fixture
def p2(tmp_path, monkeypatch):
    """A paper-test-2 order config with its own journal; the stop hold shortened for tests."""
    monkeypatch.setattr(X, "STOP_HOLD_SECONDS", 0.3)
    root = tmp_path / "p2"
    root.mkdir()
    p = write_cfg(root, orders="paper-test-2")
    cfg = load_order_config(p)
    init_journal(cfg.state_dir, cfg.experiment, cfg.connection.masked_account())
    return p


def through(tws, path, last):  # noqa: F811
    """Run the stages before ``last`` (each must pass)."""
    tws.api_settings["readOnlyApi"] = True
    assert run(S0, path)["passed"]
    tws.api_settings["readOnlyApi"] = False
    for stage in X.PAPER_TEST_2_STAGES[1:X.PAPER_TEST_2_STAGES.index(last)]:
        ev = run(stage, path)
        assert ev["passed"], (stage, ev.get("result"), ev.get("why_failed"), ev.get("error"))


def policy(monkeypatch, value):
    monkeypatch.setitem(X.PLANS, "paper-test-2",
                        replace(X.PAPER_TEST_2_PLAN, ack_timeout=value))


# --- the definition -------------------------------------------------------------------------------
def test_paper_test_2_is_paper_test_1_with_ten_submissions():
    p1, p2c = C.PAPER_TEST_1, C.EXPERIMENTS["paper-test-2"]
    assert replace(p2c, name=p1.name, max_submissions=p1.max_submissions) == p1
    assert p2c.max_submissions == 10 == sum(X.PAPER_TEST_2_PLAN.submissions)
    assert X.PAPER_TEST_2_PLAN.submissions == (0, 0, 1, 2, 7)
    assert X.PAPER_TEST_2_PLAN.ack_timeout == X.ACK_HALT
    assert X.PAPER_TEST_1_PLAN.ack_timeout == X.ACK_CANCEL_IF_WORKING  # merged behaviour


def test_paper_test_1_plan_is_the_module_constants():
    p = X.PAPER_TEST_1_PLAN
    assert p.stages == X.STAGES and not p.codes
    assert {s: p.before(s) for s in p.stages} == X.SUBMISSIONS_BEFORE
    assert {s: p.of(s) for s in p.stages} == X.SUBMISSIONS_OF
    assert {s: p.prerequisite(s) for s in p.stages[1:]} == X.PREREQUISITE
    assert [X._passing_count(k) for k in (1, 2)] == [6, 8]
    assert [X._passing_count(k, X.PAPER_TEST_2_PLAN) for k in (1, 2)] == [7, 9]
    assert not p.callback_ack_required and not p.stop_proof_after_fill


def test_each_config_runs_only_its_own_stages(tws, cfg_path, p2):  # noqa: F811
    with pytest.raises(X.StageRefused):
        X.run_stage(S2, cfg_path)  # a paper-test-2 stage on the paper-test-1 config
    with pytest.raises(X.StageRefused):
        X.run_stage("stage2-unfillable-bracket", p2)
    assert tws.count("PLACE_ORDER") == 0


def test_the_lifecycle_needs_the_stop_hold_inside_the_time_to_close():
    now = datetime(2026, 11, 11, 19, 59, 30, tzinfo=UTC)  # 14:59:30 New York (EST)
    hours = f"{now:%Y%m%d}:0930-{now:%Y%m%d}:1600"  # 60.5 min left
    contract = SimpleNamespace(liquid_hours=hours, time_zone_id="US/Eastern")
    X._check_time_to_close(contract, "stage3-lifecycle", now)  # paper-test-1: 60 min is enough
    with pytest.raises(X.StageRefused, match="less than 61"):
        X._check_time_to_close(contract, S4, now, X.STOP_HOLD_SECONDS)


# --- stage 2: the single unfillable order -----------------------------------------------------------
def test_stage2_passes_only_on_callbacks_then_a_confirmed_cancel(tws, p2):  # noqa: F811
    through(tws, p2, S2)
    ev = run(S2, p2)
    assert ev["passed"] and ev["result"] == "STAGE2_PASSED", ev.get("why_failed")
    (oid,) = tws.orders
    ack = ev["callback_ack"][str(oid)]
    assert ack["ok"] and ack["openOrder"] and ack["orderStatus"]
    assert ev["orders_as_read"][str(oid)]["completed_status"] == "Cancelled"
    assert ev["position"] == "0" and not ev["filled"] and ev["submissions_after"] == 1
    sent = tws.placed()[0]
    assert sent.order.transmit is True and not sent.order.parentId
    assert sent.order.action == "BUY" and sent.order.orderType == "LMT"
    fields = ev["working_order_messages"]
    assert {w["msg"] for w in fields} >= {"openOrder", "orderStatus"}
    assert all(w["has_client_id"] for w in fields)
    assert any(w.get("has_account") for w in fields if w["msg"] == "openOrder")
    with pytest.raises(X.StageRefused):  # a stage that passed never runs again
        X.run_stage(S2, p2)


def test_stage2_ack_only_by_reconciliation_halts_by_default(tws, p2):  # noqa: F811
    through(tws, p2, S2)
    tws.ack_lost = True
    ev = run(S2, p2)
    assert not ev["passed"] and ev["result"] == "STAGE2_ACK_UNKNOWN_HALTED"
    assert "OrderOutcomeUnknown" in ev["error"]
    assert set(ev["reconciliation"].values()) == {"WORKING"}
    assert tws.count("CANCEL_ORDER") == 0 and tws.count("PLACE_ORDER") == 1  # halt: nothing sent
    assert "reconciled by hand" in ev["why_failed"]
    with pytest.raises(X.StageRefused):
        X.run_stage(S3, p2)
    again = run(S2, p2)  # placed: never again (refused after connecting, nothing sent)
    assert not again["passed"] and "starts from 0" in again["why_failed"]
    assert tws.count("PLACE_ORDER") == 1


def test_stage2_ack_only_by_reconciliation_with_the_alternative_policy(tws, p2, monkeypatch):  # noqa: F811
    through(tws, p2, S2)
    policy(monkeypatch, X.ACK_CANCEL_IF_WORKING)
    tws.ack_lost = True
    ev = run(S2, p2)
    assert not ev["passed"] and ev["result"] == "STAGE2_ACK_BY_RECONCILIATION"
    assert tws.count("CANCEL_ORDER") == 1 and tws.count("PLACE_ORDER") == 1  # never a resend
    assert all(o.status == "Cancelled" for o in tws.orders.values())
    with pytest.raises(X.StageRefused):
        X.run_stage(S3, p2)


@pytest.mark.parametrize("variant", ["open-only", "status-only", "status-without-client-id"])
def test_stage2_ack_without_both_callbacks_naming_this_client_is_not_a_pass(tws, p2, variant):  # noqa: F811
    through(tws, p2, S2)

    def announce(fo):
        if variant != "status-only":
            tws.push(tws._open(fo))
        if variant != "open-only":
            raw = tws._status(fo)
            if variant == "status-without-client-id":
                from _ibkr_fake_tws import frame
                from ibapi.message import IN
                from ibapi.protobuf.OrderStatus_pb2 import OrderStatus as P
                raw = frame(IN.ORDER_STATUS, P(orderId=fo.order_id, status=fo.status,
                                               filled="0", remaining="1"))
            tws.push(raw)

    tws.announce = announce
    ev = run(S2, p2)
    assert not ev["passed"] and ev["result"] == "STAGE2_ACK_NOT_BY_CALLBACK", ev.get("why_failed")
    assert all(o.status == "Cancelled" for o in tws.orders.values())  # cancelled, confirmed


def test_stage2_rejected(tws, p2):  # noqa: F811
    through(tws, p2, S2)
    tws.reject_refs["TB-paper-test-2-stage2"] = (201, "Order rejected - reason: invented")
    ev = run(S2, p2)
    assert ev["result"] == "STAGE2_REJECTED" and not ev["passed"]
    assert tws.count("CANCEL_ORDER") == 0


def test_stage2_unexpected_fill_blocks_stage3(tws, p2):  # noqa: F811
    through(tws, p2, S2)
    tws.autofill, tws.ask = True, 70.00  # invented: 90% of the bid is above the ask
    ev = run(S2, p2)
    assert ev["result"] == "STAGE2_UNEXPECTED_FILL" and not ev["passed"]
    assert ev["filled"]
    with pytest.raises(X.StageRefused):
        X.run_stage(S3, p2)


def test_stage2_cancel_not_confirmed_by_the_read(tws, p2):  # noqa: F811
    through(tws, p2, S2)
    tws.on["CANCEL_ORDER"] = lambda s, conn, req: None  # no answer, still working
    ev = run(S2, p2)
    oid = max(tws.orders)
    assert ev["result"] == "STAGE2_CANCEL_NOT_CONFIRMED_BY_READ" and not ev["passed"]
    assert f"order {oid}: in open orders" in ev["why_failed"]
    assert "cancelled (confirmed" not in ev["why_failed"]


# --- stage 3: the unfillable bracket ----------------------------------------------------------------
def test_stage3_bracket_passes_on_callbacks_and_records_the_child(tws, p2):  # noqa: F811
    through(tws, p2, S3)
    ev = run(S3, p2)
    assert ev["passed"] and ev["result"] == "STAGE3_PASSED", ev.get("why_failed")
    assert all(r["ok"] for r in ev["callback_ack"].values())
    assert "child_on_parent_cancel" in ev and ev["submissions_after"] == 3
    refs = [core_ref(p.order.orderRef) for p in tws.placed()[-2:]]
    assert refs == ["TB-paper-test-2-stage3", "TB-paper-test-2-stage3.STP"]


def test_stage3_ack_only_by_reconciliation_halts(tws, p2):  # noqa: F811
    through(tws, p2, S3)
    cancels = tws.count("CANCEL_ORDER")  # stage 2's one
    tws.ack_lost = True
    ev = run(S3, p2)
    assert ev["result"] == "STAGE3_ACK_UNKNOWN_HALTED" and not ev["passed"]
    assert ev["why_failed"].startswith("STAGE3_ACK_UNKNOWN_HALTED")
    assert tws.count("CANCEL_ORDER") == cancels and tws.count("PLACE_ORDER") == 3


def test_stage3_alternative_policy_cancels_and_never_passes(tws, p2, monkeypatch):  # noqa: F811
    through(tws, p2, S3)
    policy(monkeypatch, X.ACK_CANCEL_IF_WORKING)
    tws.ack_lost = True
    ev = run(S3, p2)
    assert ev["result"] == "STAGE3_ACK_BY_RECONCILIATION" and not ev["passed"]
    assert tws.count("PLACE_ORDER") == 3
    assert all(o.status == "Cancelled" for o in tws.orders.values())


def test_stage3_an_order_acknowledged_without_its_open_order_callback(tws, p2):  # noqa: F811
    through(tws, p2, S3)
    orig = tws.announce

    def announce(fo):
        if fo.order.parentId:
            tws.push(tws._status(fo))  # the child: status only
        else:
            orig(fo)

    tws.announce = announce
    ev = run(S3, p2)
    assert ev["result"] == "STAGE3_ACK_NOT_BY_CALLBACK" and not ev["passed"]
    with pytest.raises(X.StageRefused):
        X.run_stage(S4, p2)


# --- stage 4: the lifecycle, the stop verified after the fill ---------------------------------------
def test_stage4_passes_with_the_stop_verified_after_the_fill_and_after_the_hold(tws, p2):  # noqa: F811
    through(tws, p2, S4)
    fill_later(tws)
    ev = run(S4, p2)
    assert ev["passed"] and ev["result"] == "STAGE4_PASSED", ev.get("why_failed")
    rec = ev["stop_after_fill"]
    after = rec["after_fill"]
    assert after["callback_working"] and after["row_working"] and after["position_one"]
    assert after["row_from_broker"]
    first = next(c for c in after["callbacks_after_fill"] if c["event"] == "orderStatus")
    assert first["status"] == "Submitted" and first["names_this_client"]
    assert after["seconds_after_fill"] <= X.PROTECTION_SETTLE_SECONDS
    assert after["open_orders_row"]["quantity"] == "1"
    assert after["open_orders_row"]["parent_id"] == rec["entry_order_id"]
    assert after["stop_fields_as_reported"]["auxPrice"] == pytest.approx(
        float(rec["stop_price_expected"]))
    assert rec["after_hold"]["row_working"] and rec["after_hold"]["position_one"]
    assert [s["step"] for s in ev["steps"]] == ["open", "raise-stop", "close-1"]
    assert ev["submissions_after"] == 7


def fill_later(tws, delay=0.3):  # noqa: F811
    """Fill the entry at the ask once its bracket is acknowledged and working (not inside the
    placement, as ``autofill`` does): the stop's own acknowledgement then comes BEFORE the fill,
    and only what TWS sends after the fill can count as 'after the fill'."""
    import threading

    def go():
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            entry = next((o for o in tws.orders.values() if not o.order.parentId
                          and o.status == "Submitted" and o.order.action == "BUY"
                          and tws.children(o.order_id)), None)
            if entry is not None:
                time.sleep(delay)
                tws.fill(entry.order_id, 1, tws.ask)
                tws.autofill = True  # the later closing limit fills at the bid
                return
            time.sleep(0.02)

    t = threading.Thread(target=go, daemon=True)
    t.start()
    return t


def _fill_without_child_callback(tws):  # noqa: F811
    orig = tws.fill

    def fill(order_id, qty=1, price=None):
        fo = tws.orders[order_id]
        kids = tws.children(order_id) if not fo.order.parentId else []
        real_children = tws.children
        tws.children = lambda pid: [] if pid == order_id else real_children(pid)
        try:
            out = orig(order_id, qty, price)
        finally:
            tws.children = real_children
        for c in kids:
            if c.working:
                tws._set(c, "Submitted")  # active at the broker; this client is told nothing
        return out

    tws.fill = fill


def test_stage4_stop_not_reported_by_callback_within_the_bound(tws, p2, monkeypatch):  # noqa: F811
    monkeypatch.setattr(X, "PROTECTION_SETTLE_SECONDS", 1.0)
    through(tws, p2, S4)
    _fill_without_child_callback(tws)

    def open_orders_without_status(srv, conn, req):  # this TWS answers lookups with openOrder only
        from _ibkr_fake_tws import frame
        from ibapi.message import IN
        from ibapi.protobuf.OpenOrdersEnd_pb2 import OpenOrdersEnd
        for fo in srv.orders.values():
            if fo.working:
                conn.push(srv._open(fo))
        conn.push(frame(IN.OPEN_ORDER_END, OpenOrdersEnd()))

    tws.on["REQ_ALL_OPEN_ORDERS"] = open_orders_without_status
    fill_later(tws)
    ev = run(S4, p2)
    assert ev["result"] == "STAGE4_PROTECTION_UNCONFIRMED" and not ev["passed"]
    assert "by callback" in ev["why_failed"]
    assert ev["stop_after_fill"]["after_fill"]["row_working"]  # the read shows it; not enough
    assert [s["step"] for s in ev["steps"]] == ["open"]  # nothing more was sent
    assert tws.count("PLACE_ORDER") == 5


def test_stage4_stop_missing_at_the_read_after_the_hold(tws, p2, monkeypatch):  # noqa: F811
    through(tws, p2, S4)
    fill_later(tws)
    real_sleep = time.sleep

    def sleep(seconds):
        if seconds == X.STOP_HOLD_SECONDS:
            stop = tws.orders[max(i for i, o in tws.orders.items() if o.order.parentId)]
            tws._set(stop, "Cancelled")  # gone at the broker, silently (invented)
        real_sleep(seconds)

    monkeypatch.setattr(X, "time", SimpleNamespace(monotonic=time.monotonic, sleep=sleep))
    ev = run(S4, p2)
    assert ev["result"] == "STAGE4_PROTECTION_LOST" and not ev["passed"], ev.get("why_failed")
    assert ev["stop_after_fill"]["after_fill"]["row_working"]
    assert not ev["stop_after_fill"]["after_hold"]["row_working"]
    assert [s["step"] for s in ev["steps"]] == ["open"] and tws.count("PLACE_ORDER") == 5


def test_stage4_position_and_own_fills_disagree(tws, p2, monkeypatch):  # noqa: F811
    monkeypatch.setattr(X, "PROTECTION_SETTLE_SECONDS", 1.0)
    through(tws, p2, S4)
    tws.autofill, tws.positions_lag = True, True  # the position read stays 0 after the fill
    ev = run(S4, p2)
    assert ev["result"] == "STAGE4_PROTECTION_UNCONFIRMED" and not ev["passed"]
    assert ev["state"]["position_read"] == "0" and ev["state"]["own_fills"] == "1"
    assert tws.count("PLACE_ORDER") == 5


def test_stage4_entry_not_filled_keeps_its_ending(tws, p2):  # noqa: F811
    through(tws, p2, S4)
    ev = run(S4, p2)  # no autofill
    assert ev["result"] == "STAGE4_ENTRY_NOT_FILLED" and not ev["passed"]


def test_every_paper_test_2_ending_has_its_stage_number():
    codes = X.ending_codes(X.PAPER_TEST_2_PLAN)
    assert "STAGE2_REJECTED" in codes and "STAGE3_ACK_UNKNOWN_HALTED" in codes
    assert "STAGE4_PROTECTION_LOST" in codes and "STAGE4_PROTECTION_UNCONFIRMED" in codes
    assert not any(c.startswith("STAGE3_PROTECTION") for c in codes)
    assert X.ending_codes(X.PAPER_TEST_1_PLAN) <= X.ABNORMAL_ENDINGS


# --- abort --------------------------------------------------------------------------------------------
def test_abort_works_for_paper_test_2(tws, p2):  # noqa: F811
    through(tws, p2, S2)
    s = OfflineTestSession(load_order_config(p2))
    s.connect()
    try:
        s.place([OrderSpec("TB-A", "BUY", 1, "LMT", limit_price=Decimal("80.00"))])
    finally:
        s.disconnect()
    ev = run("abort", p2)
    assert ev["passed"], ev
    assert all(o.status in TERMINAL for o in tws.orders.values())
    assert tws.count("PLACE_ORDER") == 1
