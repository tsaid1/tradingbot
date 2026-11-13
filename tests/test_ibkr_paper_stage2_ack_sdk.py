"""paper-test-1 stage 2 (13 Nov 2026): no acknowledgement for a bracket TWS visibly received.

Every message here is a protobuf (or, where named, text) frame as TWS frames it at server version
226, built from the SDK's own message classes and decoded by the REAL ``EReader``/``Decoder`` into
the session (bytes in), on the fake transport. What a real TWS sends is NOT known from the SDK
source; each variant below is a plausible omission or ordering, labelled as such. Invented data.

Groups:
* characterization: variants the CURRENT code already acknowledges (they rule candidates out);
* reproduction: variants for which the current code ends in ``OrderOutcomeUnknown`` (or loses the
  diagnostics) although the broker has the orders; these fail on 85f413f;
* safety: what must stay true after any correction (nothing resent, no foreign adoption).
"""
from __future__ import annotations

import json
import socket
import threading
from decimal import Decimal

import pytest

pytest.importorskip("ibapi")

import ibapi.client
from _ibkr_fake_tws import error_frame, frame, text_frame
from _ibkr_fake_tws_orders import FakeOrder, FakeOrderTws
from google.protobuf import json_format
from ibapi.common import PROTOBUF_MSG_ID
from ibapi.message import IN
from ibapi.protobuf.Contract_pb2 import Contract as ContractProto
from ibapi.protobuf.OpenOrder_pb2 import OpenOrder
from ibapi.protobuf.Order_pb2 import Order as OrderProto
from ibapi.protobuf.OrderState_pb2 import OrderState
from ibapi.protobuf.OrderStatus_pb2 import OrderStatus as OrderStatusProto
from test_ibkr_paper_experiment import cfg_path, run, stage0, tws  # noqa: F401 - fixtures
from test_ibkr_paper_orders_sdk import buy, make, placed, stop

from tradingbot.execution.broker import OrderStatus
from tradingbot.ibkr.errors import IbkrConnectionError
from tradingbot.ibkr_orders import experiment as X
from tradingbot.ibkr_orders.records import OrderOutcomeUnknown

PRESENT = "present"
ABSENT = "absent"

# The order fields the REAL paper TWS (server version 226) put in the completed-order messages
# for stage 2's two orders (diagnostic read-back, 13 Nov 2026, 15:16 UTC; account masked there,
# the synthetic paper account here; ids, perm ids and client id are the fake's at run time).
# A WORKING order's open-order message is assumed to carry the same order fields: that is a
# plausible assumption, not a fact (no open-order message from the real TWS has been seen).
REAL_ORDER_COMMON = {
    "adjustableTrailingUnit": 0, "adjustedOrderType": "None", "clearingIntent": "IB",
    "deltaNeutralOrderType": "None", "deltaNeutralShortSaleSlot": 0,
    "dontUseAutoPriceForHedge": True, "exemptCode": -1, "filledQuantity": "0", "ocaType": 3,
    "origin": 0, "referencePriceType": 0, "routeMarketableToBbo": 0, "seekPriceImprovement": 0,
    "shareholder": "Not an insider or substantial shareholder", "shortSaleSlot": 0,
    "submitter": "someone", "tif": "DAY", "totalQuantity": "1", "triggerMethod": 0,
    "usePriceMgmtAlgo": 0, "volatilityType": 0,
}
REAL_PARENT = {**REAL_ORDER_COMMON, "action": "BUY", "auxPrice": 0.0, "lmtPrice": 88.12,
               "orderType": "LMT", "parentId": 0, "trailStopPrice": 89.12}
REAL_CHILD = {**REAL_ORDER_COMMON, "action": "SELL", "auxPrice": 87.0,
              "lmtPrice": 86.97,  # a limit the broker added to the stop (we sent none)
              "orderType": "STP", "trailStopPrice": 87.0}
REAL_CONTRACT = {"conId": 15547841, "currency": "USD", "exchange": "SMART", "localSymbol": "TLT",
                 "secType": "STK", "strike": 0.0, "symbol": "TLT", "tradingClass": "NMS"}
MAX = 1.7976931348623157e308


def working_state(status: str, *, unset_numbers: bool = True, **extra) -> OrderState:
    """A plausible working-order state: margin and commission fields at the unset double (or
    infinity), empty currency and warning text."""
    big = MAX if unset_numbers else float("inf")
    return OrderState(status=status, initMarginBefore=big, maintMarginBefore=big,
                      equityWithLoanBefore=big, initMarginChange=big, maintMarginChange=big,
                      equityWithLoanChange=big, initMarginAfter=big, maintMarginAfter=big,
                      equityWithLoanAfter=big, commissionAndFees=big, minCommissionAndFees=big,
                      maxCommissionAndFees=big, commissionAndFeesCurrency="",
                      **{"warningText": "", **extra})


class RealisticTws(FakeOrderTws):
    """The order fake with knobs for how a real TWS might acknowledge (all plausible, none known).

    ``open_client_id`` / ``status_client_id``: PRESENT (this client's id), ABSENT (field not set in
    the protobuf message) or an int (explicitly that value). ``sequence(fo)`` gives the
    (status, whyHeld) pairs sent per acknowledged order. ``notice_only``: instead of any
    acknowledgement, only this (code, message) error for the order id. ``ack_delay``: seconds
    before the acknowledgement frames are delivered. ``text_status``: orderStatus text-framed.
    ``open_without_state``: the OpenOrder message lacks ``orderState`` (the SDK then returns
    without calling ``openOrder``)."""

    def __init__(self) -> None:
        super().__init__()
        self.server_version = 226
        self.open_client_id: object = PRESENT
        self.status_client_id: object = PRESENT
        self.send_open = True
        self.send_status = True
        self.sequence = lambda fo: [(fo.status, "")]
        self.notice_only: tuple[int, str] | None = None
        self.ack_delay = 0.0
        self.text_status = False
        self.open_without_state = False
        self.real_fields = False  # the order fields the real TWS sent (REAL_PARENT / REAL_CHILD)
        self.state_of = lambda fo, status: OrderState(status=status)
        self.child_first = False  # the child's frames before the parent's
        self.status_first = False  # each order's orderStatus before its openOrder
        self.raw_frames: dict[int, list[bytes]] = {}  # order id -> frames sent INSTEAD
        self._held_frames: list[bytes] = []
        self.timers: list[threading.Timer] = []

    def _order_of(self, fo: FakeOrder, account: str | None = None):
        o = super()._order_of(fo, account)
        if self.real_fields:
            real = REAL_CHILD if fo.order.parentId else REAL_PARENT
            json_format.ParseDict(real, o)
            o.lmtPrice = fo.order.lmtPrice if not fo.order.parentId else real["lmtPrice"]
            if fo.order.parentId:
                parent = self.orders[fo.order.parentId]
                o.auxPrice = fo.order.auxPrice
                o.parentPermId = parent.perm_id
                o.ocaGroup = str(parent.perm_id)  # the broker assigns it
        if self.open_client_id == ABSENT:
            o.ClearField("clientId")
        elif isinstance(self.open_client_id, int):
            o.clientId = self.open_client_id
        return o

    def _status_frame(self, fo: FakeOrder, status: str, why_held: str) -> bytes:
        cid = self.status_client_id
        if self.text_status:
            client = str(fo.client_id if cid == PRESENT else (0 if cid == ABSENT else cid))
            return text_frame(IN.ORDER_STATUS, str(fo.order_id), status, "0", "1", "0",
                              str(fo.perm_id), str(fo.order.parentId), "0", client, why_held,
                              "0")
        msg = OrderStatusProto(orderId=fo.order_id, status=status, filled="0", remaining="1",
                               avgFillPrice=0.0, permId=fo.perm_id, parentId=fo.order.parentId,
                               lastFillPrice=0.0, whyHeld=why_held, mktCapPrice=0.0)
        if cid == PRESENT:
            msg.clientId = fo.client_id
        elif isinstance(cid, int):
            msg.clientId = cid
        return frame(IN.ORDER_STATUS, msg)

    def _open_frame(self, fo: FakeOrder, status: str) -> bytes:
        contract = fo.contract
        if self.real_fields:
            contract = json_format.ParseDict(REAL_CONTRACT, ContractProto())
        msg = OpenOrder(orderId=fo.order_id, contract=contract, order=self._order_of(fo))
        if not self.open_without_state:
            msg.orderState.CopyFrom(self.state_of(fo, status))
        return frame(IN.OPEN_ORDER, msg)

    def announce(self, fo: FakeOrder) -> None:
        if fo.order_id in self.raw_frames:
            frames = list(self.raw_frames[fo.order_id])
        elif self.notice_only is not None:
            frames = [error_frame(fo.order_id, *self.notice_only)]
        else:
            frames = []
            for status, why in self.sequence(fo):
                pair = []
                if self.send_open:
                    pair.append(self._open_frame(fo, status))
                if self.send_status:
                    pair.append(self._status_frame(fo, status, why))
                frames += reversed(pair) if self.status_first else pair
        if self.child_first and not fo.order.parentId:
            self._held_frames = frames  # sent after the child's
            return
        if self.child_first:
            frames, self._held_frames = frames + self._held_frames, []
        conn = self.conn
        if self.ack_delay:
            t = threading.Timer(self.ack_delay, lambda: [conn.push(f) for f in frames])
            t.daemon = True
            self.timers.append(t)
            t.start()
        else:
            for f in frames:
                conn.push(f)


def bracket_sequence(fo: FakeOrder) -> list[tuple[str, str]]:
    """A plausible real sequence: the parent PendingSubmit -> PreSubmitted -> Submitted; the stop
    child PendingSubmit -> PreSubmitted, held ('child') until its parent fills."""
    if fo.order.parentId:
        return [("PendingSubmit", ""), ("PreSubmitted", "child")]
    return [("PendingSubmit", ""), ("PreSubmitted", ""), ("Submitted", "")]


@pytest.fixture
def rtws(monkeypatch):
    def no_network(*args, **kwargs):
        raise AssertionError("network access attempted in an offline test")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket.socket, "connect_ex", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    server = RealisticTws()
    monkeypatch.setattr(ibapi.client, "Connection", server.connection_factory)
    yield server
    for t in server.timers:
        t.cancel()


def place_bracket(s):
    return s.place([buy(), stop()])


# --- characterization: the current code acknowledges these (candidates ruled out) ---------------
def test_realistic_bracket_sequence_with_why_held_is_acknowledged(rtws, tmp_path):
    rtws.sequence = bracket_sequence
    s = make(rtws, tmp_path)
    try:
        parent, child = place_bracket(s)
        assert parent.status == OrderStatus.SUBMITTED  # the last status of the sequence
        assert child.status == OrderStatus.PRE_SUBMITTED and child.parent_id == parent.order_id
        assert s.internal_errors == []
    finally:
        s.disconnect()


def test_status_alone_without_client_id_is_an_acknowledgement(rtws, tmp_path):
    rtws.send_open, rtws.status_client_id = False, ABSENT
    s = make(rtws, tmp_path)
    try:
        assert [x.status for x in place_bracket(s)] == [OrderStatus.SUBMITTED,
                                                         OrderStatus.PRE_SUBMITTED]
    finally:
        s.disconnect()


def test_open_order_alone_with_this_client_id_is_an_acknowledgement(rtws, tmp_path):
    rtws.send_status = False
    s = make(rtws, tmp_path)
    try:
        assert all(x.status is not None for x in place_bracket(s))
    finally:
        s.disconnect()


def test_text_framed_order_status_at_226_is_decoded_and_acknowledges(rtws, tmp_path):
    rtws.send_open, rtws.text_status = False, True
    s = make(rtws, tmp_path)
    try:
        assert [x.status for x in place_bracket(s)] == [OrderStatus.SUBMITTED,
                                                         OrderStatus.PRE_SUBMITTED]
    finally:
        s.disconnect()


@pytest.mark.parametrize("raw", ["ApiPending", "PendingSubmit", "Inactive", "SomethingNew"])
def test_early_or_unknown_status_strings_still_count_as_an_answer(rtws, tmp_path, raw):
    rtws.sequence = lambda fo: [(raw, "")]
    s = make(rtws, tmp_path)
    try:
        snaps = place_bracket(s)
        assert all(x.raw_status == raw for x in snaps) and s.internal_errors == []
    finally:
        s.disconnect()


def test_status_with_another_explicit_client_id_is_dropped_by_design(rtws, tmp_path):
    rtws.send_open, rtws.status_client_id = False, 99
    s = make(rtws, tmp_path)
    try:
        with pytest.raises(OrderOutcomeUnknown):
            place_bracket(s)
        assert placed(rtws) == 2  # never resent
    finally:
        s.disconnect()


# --- reproduction: fail on 85f413f -----------------------------------------------------------
def test_open_order_without_client_id_is_this_clients_acknowledgement(rtws, tmp_path):
    """CANDIDATE: the protobuf OpenOrder carries no clientId (decoded as 0, the SDK default), no
    orderStatus follows within the wait. The row has this session's order id, its exact orderRef
    and its contract, yet the current code files it as FOREIGN and raises OrderOutcomeUnknown."""
    rtws.open_client_id, rtws.send_status = ABSENT, False
    s = make(rtws, tmp_path)
    try:
        parent, child = place_bracket(s)
        assert parent.own and child.own
        assert parent.status == OrderStatus.SUBMITTED
        assert placed(rtws) == 2
    finally:
        s.disconnect()


def test_open_order_without_client_id_then_status_with_client_id_zero(rtws, tmp_path):
    """CANDIDATE: neither message names this client (openOrder without clientId, orderStatus
    with clientId 0). The orderStatus stays ignored; the openOrder must still be recognised."""
    rtws.open_client_id, rtws.status_client_id = ABSENT, 0
    s = make(rtws, tmp_path)
    try:
        assert all(x.own and x.status is not None for x in place_bracket(s))
        diag = s.diagnostics()
        assert diag["ignored"]["status_other_client"] >= 2
    finally:
        s.disconnect()


def test_late_acknowledgement_is_found_by_a_bounded_read_only_reconciliation(rtws, tmp_path):
    """CANDIDATE: TWS acknowledges after the wait (slow first order, or held until a person
    confirmed). The current code raises at once; the reads would have shown the orders."""
    rtws.ack_delay = 0.6  # > ack_timeout 0.4 (tests/_ibkr_paper_testing.py)
    s = make(rtws, tmp_path)
    try:
        with pytest.raises(OrderOutcomeUnknown) as info:
            place_bracket(s)
        rec = info.value.reconciliation
        assert set(rec) == set(rtws.orders)
        assert set(rec.values()) == {"WORKING"}
        assert placed(rtws) == 2 and rtws.count("CANCEL_ORDER") == 0  # read-only, no resend
    finally:
        s.disconnect()


def test_notice_only_answer_then_reconciliation_shows_the_orders_working(rtws, tmp_path):
    """CANDIDATE: only an order warning (399, a NOTICE here) arrives for each id."""
    rtws.notice_only = (399, "Order Message: invented warning")
    s = make(rtws, tmp_path)
    try:
        with pytest.raises(OrderOutcomeUnknown) as info:
            place_bracket(s)
        assert set(info.value.reconciliation.values()) == {"WORKING"}
        events = s.diagnostics()["order_events"]
        assert [e["code"] for e in events if e["event"] == "error"] == [399, 399]
    finally:
        s.disconnect()


def test_orders_held_at_the_broker_are_reported_held_not_working(rtws, tmp_path):
    """CANDIDATE: TWS holds the bracket (a confirmation, a precaution, not transmitted) and its
    rows say PendingSubmit/ApiPending: "at the broker, not working" (unknown how a real TWS shows
    a held API order: only a live read can say)."""
    rtws.ack_lost = True
    s = make(rtws, tmp_path)
    try:
        orig = rtws._open

        def held_open(fo, account=None):
            fo.status = "ApiPending"
            return orig(fo, account)

        rtws._open = held_open
        with pytest.raises(OrderOutcomeUnknown) as info:
            place_bracket(s)
        assert set(info.value.reconciliation.values()) == {"AT_BROKER_NOT_WORKING"}
        assert placed(rtws) == 2
    finally:
        s.disconnect()


def test_open_order_message_without_order_state_is_recorded_not_silently_lost(rtws, tmp_path):
    """SDK: ``processOpenOrderMsgProtoBuf`` returns without calling ``openOrder`` when the message
    lacks contract, order or orderState. The session must at least record that it arrived."""
    rtws.open_without_state, rtws.send_status = True, False
    rtws.ack_lost = False
    s = make(rtws, tmp_path)
    try:
        with pytest.raises(OrderOutcomeUnknown):
            place_bracket(s)
        wire = [w for w in s.diagnostics()["wire"] if w["msg"] == "openOrder"]
        assert {w["order_id"] for w in wire if w["has_order_state"] is False} == set(rtws.orders)
    finally:
        s.disconnect()


def test_wire_record_shows_field_presence_without_the_account(rtws, tmp_path):
    rtws.open_client_id = ABSENT
    s = make(rtws, tmp_path)
    try:
        place_bracket(s)
        diag = s.diagnostics()
        opens = [w for w in diag["wire"] if w["msg"] == "openOrder"]
        assert opens and all(w["has_client_id"] is False and w["has_account"] is True
                             for w in opens)
        assert "DU" not in json.dumps(diag, default=str)  # presence only, never the account
    finally:
        s.disconnect()


# --- safety ---------------------------------------------------------------------------------------
def test_explicit_other_client_id_with_our_ref_is_never_adopted(rtws, tmp_path):
    rtws.open_client_id, rtws.send_status = 99, False
    s = make(rtws, tmp_path)
    try:
        with pytest.raises(OrderOutcomeUnknown) as info:
            place_bracket(s)
        assert set(info.value.reconciliation.values()) == {"FOREIGN_CLIENT_ROW"}
        assert all(s._tracks[i].status is None for i in rtws.orders)
        assert placed(rtws) == 2
    finally:
        s.disconnect()


def test_orders_not_found_anywhere_stay_unknown_and_are_never_resent(rtws, tmp_path):
    rtws.ack_lost, rtws.invisible = True, True
    s = make(rtws, tmp_path)
    try:
        with pytest.raises(OrderOutcomeUnknown) as info:
            place_bracket(s)
        assert set(info.value.reconciliation.values()) == {"NOT_FOUND"}
        assert placed(rtws) == 2 and rtws.count("CANCEL_ORDER") == 0
        assert s.submissions_used == 2
    finally:
        s.disconnect()


# --- the experiment ------------------------------------------------------------------------------
def to_stage2(tws, cfg_path):  # noqa: F811
    stage0(tws, cfg_path)
    assert run("stage1-lookups", cfg_path)["passed"]


def test_stage2_unknown_ack_persists_the_session_diagnostics(tws, cfg_path):  # noqa: F811
    to_stage2(tws, cfg_path)
    tws.ack_lost, tws.invisible = True, True
    ev = run("stage2-unfillable-bracket", cfg_path)
    assert not ev["passed"] and "OrderOutcomeUnknown" in ev["error"]
    diag = ev["session_diagnostics"]
    sent = [e for e in diag["order_events"] if e["event"] == "SENT_PLACE"]
    assert [e["order_id"] for e in sent] == sorted(tws.orders) and all("at" in e for e in sent)
    for key in ("internal_errors", "notices", "unrouted_errors", "connection_events", "ignored",
                "wire"):
        assert key in diag
    assert ev["reconciliation"] == {str(i): "NOT_FOUND" for i in tws.orders}
    assert tws.count("CANCEL_ORDER") == 0 and placed(tws) == 2


def test_stage2_after_an_unknown_ack_can_never_run_again(tws, cfg_path):  # noqa: F811
    to_stage2(tws, cfg_path)
    tws.ack_lost, tws.invisible = True, True
    run("stage2-unfillable-bracket", cfg_path)
    tws.ack_lost = tws.invisible = False
    ev = run("stage2-unfillable-bracket", cfg_path)
    assert not ev["passed"] and "starts from 0" in ev["why_failed"]
    assert placed(tws) == 2
    with pytest.raises(X.StageRefused):
        X.run_stage("stage3-lifecycle", cfg_path)


def test_stage2_late_ack_with_both_orders_working_cancels_them_and_does_not_pass(
        tws, cfg_path):  # noqa: F811
    """Recommendation (d): the reconciliation shows both working and unfilled: the stage cancels
    them (parent first, confirmed) and ends STAGE2_ACK_BY_RECONCILIATION, not passed."""
    to_stage2(tws, cfg_path)
    tws.ack_lost = True  # created at the broker, no callback; visible to the reads
    ev = run("stage2-unfillable-bracket", cfg_path)
    assert ev["result"] == "STAGE2_ACK_BY_RECONCILIATION" and not ev["passed"]
    assert ev["reconciliation"] == {str(i): "WORKING" for i in tws.orders}
    assert all(o.status == "Cancelled" for o in tws.orders.values())
    assert placed(tws) == 2


def test_abort_after_an_unknown_stage2_cancels_only_this_clients_orders(tws, cfg_path):  # noqa: F811
    to_stage2(tws, cfg_path)
    tws.ack_lost, tws.invisible = True, True
    run("stage2-unfillable-bracket", cfg_path)
    for fo in tws.orders.values():
        fo.hidden = False  # the broker shows them now
    tws.ack_lost = tws.invisible = False
    ev = run("abort", cfg_path)
    assert ev["passed"], ev
    assert placed(tws) == 2  # abort places nothing
    assert all(o.status == "Cancelled" for o in tws.orders.values())
    assert "session_diagnostics" in ev
    rows = ev["after"]["completed_orders"]["rows"]
    assert all({"client_id", "parent_id", "perm_id_present"} <= set(r) for r in rows)



# --- the real TWS's order fields (read-back of 13 Nov 2026) with working-order states ----------
def real_states(fo: FakeOrder, status: str) -> OrderState:
    return working_state(status, warningText="" if fo.order.parentId else "invented warning")


@pytest.mark.parametrize("child_first", [False, True], ids=["parent-first", "child-first"])
@pytest.mark.parametrize("status_first", [False, True], ids=["open-then-status",
                                                             "status-then-open"])
def test_real_order_fields_with_working_states_are_acknowledged_in_any_order(
        rtws, tmp_path, child_first, status_first):
    """The broker-added stop limit, the broker's ocaGroup / ocaType 3 / parentPermId, the
    filledQuantity string, unset-double margins and a warning text: no handler raises, both
    orders are acknowledged, in every order a real TWS can send the four messages."""
    rtws.real_fields, rtws.state_of, rtws.sequence = True, real_states, bracket_sequence
    rtws.child_first, rtws.status_first = child_first, status_first
    s = make(rtws, tmp_path)
    try:
        parent, child = place_bracket(s)
        assert (parent.status, child.status) == (OrderStatus.SUBMITTED, OrderStatus.PRE_SUBMITTED)
        assert child.order_type == "STP" and child.limit_price is None  # the added limit ignored
        assert child.stop_price == Decimal(87) and s.internal_errors == []
        assert s._tracks[child.order_id].oca_group == str(rtws.orders[parent.order_id].perm_id)
    finally:
        s.disconnect()


def test_infinite_margin_values_do_not_break_the_order_state_decode(rtws, tmp_path):
    rtws.real_fields, rtws.state_of = True, lambda fo, st: working_state(st, unset_numbers=False)
    s = make(rtws, tmp_path)
    try:
        assert all(x.status is not None for x in place_bracket(s)) and s.internal_errors == []
    finally:
        s.disconnect()


# --- the SDK reader loop when ONE incoming message fails to decode --------------------------------
def test_a_decode_exception_stops_the_reader_and_ends_as_connection_lost_not_unknown(rtws,
                                                                                     tmp_path):
    """``EClient.run`` catches only BadMessage (and KeyboardInterrupt/SystemExit); the decoder's
    ``processProtoBuf`` catches BadMessage and protobuf DecodeError. Any other exception raised
    while decoding (here: ``Decimal('x')`` in ``decodeOrder``) leaves ``run``; the session's run
    thread records it and reports the connection lost. So a decode raise during stage 2 would have
    ended as IbkrConnectionError, NOT as OrderOutcomeUnknown: ruled out for 13 Nov."""
    broken = frame(IN.OPEN_ORDER, OpenOrder(
        orderId=100, contract=json_format.ParseDict(REAL_CONTRACT, ContractProto()),
        order=OrderProto(orderId=100, totalQuantity="x"), orderState=OrderState(status="Submitted")))
    rtws.raw_frames = {100: [broken], 101: []}  # the fake's first two order ids
    s = make(rtws, tmp_path)
    try:
        with pytest.raises(IbkrConnectionError):
            place_bracket(s)
        assert any("EClient.run raised" in e for e in s.internal_errors)
    finally:
        s.disconnect()


def test_an_unparseable_protobuf_frame_is_reported_on_id_minus_one_and_reading_continues(
        rtws, tmp_path):
    """A protobuf DecodeError becomes ``error(-1, ERROR_DECODING_PROTOBUF)`` and the loop goes
    on: the later orderStatus messages still acknowledge. The error is kept (unrouted_errors)
    but, before the correction, never written to the evidence."""
    body = (IN.OPEN_ORDER + PROTOBUF_MSG_ID).to_bytes(4, "big") + b"\xff\xff\xff\xff"
    garbage = len(body).to_bytes(4, "big") + body
    rtws.send_open = False
    orig = rtws.announce

    def with_garbage(fo):
        rtws.conn.push(garbage)
        orig(fo)

    rtws.announce = with_garbage
    s = make(rtws, tmp_path)
    try:
        assert all(x.status is not None for x in place_bracket(s))
        assert sum(1 for rid, _c, _m in s.unrouted_errors if rid == -1) >= 2
    finally:
        s.disconnect()
