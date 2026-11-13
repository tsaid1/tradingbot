"""PR #78 review items S1, S2, S3, N1 (paper-test-1 stage 2). Fake TWS only, no socket, invented
data. Each test states the input and what the stage or session must do with it."""
from __future__ import annotations

import json

import pytest

pytest.importorskip("ibapi")

from _ibkr_fake_tws import error_frame, frame
from _ibkr_fake_tws_orders import TERMINAL
from ibapi.message import IN
from ibapi.protobuf.CompletedOrder_pb2 import CompletedOrder
from ibapi.protobuf.Order_pb2 import Order as OrderProto
from ibapi.protobuf.OrderState_pb2 import OrderState
from test_ibkr_paper_experiment import cfg_path, run, stage0, tws  # noqa: F401 - fixtures
from test_ibkr_paper_orders_sdk import buy, make
from test_ibkr_paper_stage2_ack_sdk import rtws  # noqa: F401 - fixture

from tradingbot.execution.broker import OrderStatus
from tradingbot.ibkr_orders import experiment as X
from tradingbot.ibkr_orders.config import load_order_config

STAGE2 = "stage2-unfillable-bracket"


def to_stage2(tws, cfg_path):  # noqa: F811
    stage0(tws, cfg_path)
    assert run("stage1-lookups", cfg_path)["passed"]


def _cancel_frames(s, conn, fo) -> None:
    s._set(fo, "Cancelled")
    conn.push(error_frame(fo.order_id, 202, "Order Canceled - reason:"))
    conn.push(s._status(fo))


def parent_cancel_cancels_child_silently(refusal_code: int = 161):
    """What the real TWS did on 13 Nov: the parent's cancel also cancels the child, and this
    client hears nothing about the child. A later cancel of the child is refused."""
    def handler(s, conn, req):
        fo = s.orders.get(req.orderId)
        if fo is None or fo.status in TERMINAL:
            text = ("Cancel attempted when order is not in a cancellable state."
                    if refusal_code == 161 else
                    f"OrderId {req.orderId} that needs to be cancelled is not found.")
            conn.push(error_frame(req.orderId, refusal_code, text))
            return
        _cancel_frames(s, conn, fo)
        for c in s.children(fo.order_id):
            if c.status not in TERMINAL:
                s._set(c, "Cancelled")  # silently: no error, no status for the child
    return handler


def parent_cancel_answered_child_cancel_unanswered(s, conn, req):
    fo = s.orders[req.orderId]
    if fo.order.parentId:
        return  # the child's cancel: no answer at all, the child stays working at the broker
    _cancel_frames(s, conn, fo)  # the parent only; its child is NOT cancelled with it


# --- S1 -----------------------------------------------------------------------------------------
@pytest.mark.parametrize("late", [True, False], ids=["ack-by-reconciliation", "ack-by-callback"])
def test_a_child_whose_cancel_is_unanswered_is_never_reported_cancelled(tws, cfg_path, late):  # noqa: F811
    to_stage2(tws, cfg_path)
    tws.ack_lost = late
    tws.on["CANCEL_ORDER"] = parent_cancel_answered_child_cancel_unanswered
    ev = run(STAGE2, cfg_path)
    parent, child = sorted(tws.orders)
    assert not ev["passed"] and ev["result"] == "STAGE2_CHILD_NOT_CONFIRMED_CANCELLED", ev
    assert "were cancelled" not in ev["why_failed"]
    assert f"order {child}: in open orders" in ev["why_failed"]
    assert f"order {parent}: not in open orders, Cancelled in completed orders" in \
        ev["why_failed"]
    assert ev["orders_as_read"][str(child)]["in_open_orders"] is True
    assert ev["result"] in X.ABNORMAL_ENDINGS
    assert tws.orders[child].status not in TERMINAL  # still working at the broker
    assert tws.count("PLACE_ORDER") == 2


# --- S2 -----------------------------------------------------------------------------------------
@pytest.mark.parametrize("code", [161, 10147])
def test_normal_path_child_cancelled_by_the_broker_with_its_parent_ends_clean(tws, cfg_path,  # noqa: F811
                                                                              code):
    to_stage2(tws, cfg_path)
    tws.on["CANCEL_ORDER"] = parent_cancel_cancels_child_silently(code)
    ev = run(STAGE2, cfg_path)
    child = max(tws.orders)
    assert ev["passed"] and ev["result"] == "STAGE2_PASSED", ev
    assert ev["child_cancel_refused"]["order_id"] == child
    assert str(code) in ev["child_cancel_refused"]["error"]
    seen = ev["child_on_parent_cancel"]
    assert seen["callback_for_child_after_parent_cancel"] is None
    assert seen["in_open_orders"] is False and seen["completed_status"] == "Cancelled"


def test_late_ack_child_cancelled_by_the_broker_with_its_parent(tws, cfg_path):  # noqa: F811
    to_stage2(tws, cfg_path)
    tws.ack_lost = True
    tws.on["CANCEL_ORDER"] = parent_cancel_cancels_child_silently(161)
    ev = run(STAGE2, cfg_path)
    assert ev["result"] == "STAGE2_ACK_BY_RECONCILIATION" and not ev["passed"], ev
    assert "confirmed by a later read" in ev["why_failed"]
    assert ev["child_on_parent_cancel"]["completed_status"] == "Cancelled"
    assert "error" not in ev  # no bare CancelFailed


def test_a_child_refused_and_still_open_is_a_named_ending_as_read(tws, cfg_path):  # noqa: F811
    """The child's cancel is refused as not cancellable but the read still shows it open."""
    to_stage2(tws, cfg_path)

    def handler(s, conn, req):
        fo = s.orders[req.orderId]
        if fo.order.parentId:
            conn.push(error_frame(fo.order_id, 161, "Cancel attempted when order is not in a "
                                                    "cancellable state."))
            return
        _cancel_frames(s, conn, fo)

    tws.on["CANCEL_ORDER"] = handler
    ev = run(STAGE2, cfg_path)
    child = max(tws.orders)
    assert ev["result"] == "STAGE2_CHILD_NOT_CONFIRMED_CANCELLED" and not ev["passed"]
    assert f"order {child}: in open orders" in ev["why_failed"] and "error" not in ev


# --- S3 -----------------------------------------------------------------------------------------
@pytest.mark.parametrize("stage", ["stage1-lookups", STAGE2, "stage3-lifecycle", "abort"])
def test_a_failed_connect_still_writes_the_session_diagnostics(tws, cfg_path, stage):  # noqa: F811
    stage0(tws, cfg_path)
    if stage != "stage1-lookups":
        assert run("stage1-lookups", cfg_path)["passed"]
    if stage == "stage3-lifecycle":
        assert run(STAGE2, cfg_path)["passed"]
    tws.api_settings["readOnlyApi"] = True  # connect refuses (order mode)
    ev = run(stage, cfg_path)
    assert "OrderModeSettingRefused" in ev["error"]
    diag = ev["session_diagnostics"]
    assert "order_events" in diag and "internal_errors" in diag


def test_stage0_writes_the_session_diagnostics_too(tws, cfg_path):  # noqa: F811
    assert "order_events" in stage0(tws, cfg_path)["session_diagnostics"]


@pytest.mark.parametrize("broken", ["raises", "unserializable"])
def test_broken_diagnostics_never_mask_the_original_error(tws, cfg_path, monkeypatch, broken):  # noqa: F811
    stage0(tws, cfg_path)
    tws.api_settings["readOnlyApi"] = True

    def diagnostics(self):
        if broken == "raises":
            raise RuntimeError("invented diagnostics failure")
        loop: dict = {}
        loop["self"] = loop  # json.dumps: circular reference
        return loop

    monkeypatch.setattr(X.PaperOrderSession, "diagnostics", diagnostics)
    _ev, path = X.run_stage("stage1-lookups", cfg_path)
    data = json.loads(path.read_text())
    assert "OrderModeSettingRefused" in data["error"] and data["passed"] is False
    assert "error" in data["session_diagnostics"]
    cfg = load_order_config(cfg_path)
    assert path == X.evidence_path(cfg, "stage1-lookups")


# --- N1 -----------------------------------------------------------------------------------------
def test_a_stale_completed_row_with_the_same_id_is_not_merged(rtws, tmp_path):  # noqa: F811
    """After an order-id reset, a completed row of an OLD order with the same id (another
    reference, another permanent id) must not label the live new order final."""
    s = make(rtws, tmp_path)
    try:
        live = s.place([buy("TB-NEW")])[0]
        assert live.status == OrderStatus.SUBMITTED
        stale = CompletedOrder(
            contract=rtws.orders[live.order_id].contract,
            order=OrderProto(orderId=live.order_id, clientId=rtws.client_id, permId=12345,
                             account=rtws.order_account, action="BUY", totalQuantity="1",
                             orderType="LMT", lmtPrice=80.0, tif="DAY",
                             orderRef="paper-test-1:TB-OLD"),
            orderState=OrderState(status="Cancelled", completedStatus="Cancelled by Trader",
                                  completedTime="20261110 10:00:00 US/Eastern"))
        base = rtws.on["REQ_COMPLETED_ORDERS"]

        def with_stale(srv, conn, req):
            conn.push(frame(IN.COMPLETED_ORDER, stale))
            base(srv, conn, req)

        rtws.on["REQ_COMPLETED_ORDERS"] = with_stale
        s.completed_orders()
        assert s._tracks[live.order_id].status == OrderStatus.SUBMITTED  # not FINAL:Cancelled
        diag = s.diagnostics()
        assert diag["ignored"]["completed_same_id_other_order"] == 1
        assert any(e["event"] == "COMPLETED_ROW_NOT_MERGED" for e in diag["order_events"])
    finally:
        s.disconnect()


def test_the_own_completed_row_is_still_merged(rtws, tmp_path):  # noqa: F811
    s = make(rtws, tmp_path)
    try:
        live = s.place([buy("TB-NEW")])[0]
        fo = rtws.orders[live.order_id]
        rtws._set(fo, "Cancelled")  # cancelled at the broker; this client heard nothing
        s.completed_orders()
        assert s._tracks[live.order_id].status == OrderStatus.CANCELLED
    finally:
        s.disconnect()
