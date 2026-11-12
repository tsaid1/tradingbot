"""PaperOrderSession and IbkrPaperBroker through the REAL ibapi 1052.1 EClient/EReader/Decoder over
the fake transport (``tests/_ibkr_fake_tws_orders.py``), and the real ExecutionCoordinator on top.
No socket is opened: ``socket.socket.connect`` raises. Invented data only."""
from __future__ import annotations

import inspect
import socket
import time
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from types import MappingProxyType

import pytest

pytest.importorskip("ibapi")

import ibapi.client
from _ibkr_fake_tws_orders import (
    PAPER,
    READ_ONLY_REJECTION,
    TLT_CON_ID,
    FakeOrderTws,
    core_ref,
)
from _ibkr_paper_testing import OfflineTestSession
from execution_harness import APPROVE, action, source
from ibapi.client import EClient
from ibapi.common import PROTOBUF_MSG_ID
from ibapi.message import OUT
from ibapi.protobuf.GlobalCancelRequest_pb2 import GlobalCancelRequest
from ibapi.protobuf.PlaceOrderRequest_pb2 import PlaceOrderRequest
from ibapi.wrapper import EWrapper
from test_ibkr_paper_orders_pure import write_cfg
from test_ibkr_session_sdk import _fire_send_like, _walk

from tradingbot.execution.broker import (
    AckLost,
    BrokerError,
    ContractRef,
    OrderIntent,
    OrderSide,
    OrderStatus,
    OrderType,
    TimeInForce,
)
from tradingbot.execution.coordinator import ExecutionConfig, ExecutionCoordinator, order_ref_for
from tradingbot.execution.store import IntentState
from tradingbot.ibkr import errors as E
from tradingbot.ibkr.config import validate_config
from tradingbot.ibkr.errors import ReadOnlyViolation
from tradingbot.ibkr.session import ALLOWED_OUT_MESSAGES, IbkrSession
from tradingbot.ibkr_orders import caps as C
from tradingbot.ibkr_orders import config as order_config_module
from tradingbot.ibkr_orders import session as order_session_module
from tradingbot.ibkr_orders.broker import IbkrPaperBroker, PartialBracket
from tradingbot.ibkr_orders.config import PaperOrderConfig, load_order_config
from tradingbot.ibkr_orders.counter import init_journal, journal_paths
from tradingbot.ibkr_orders.guard import RefusalReason
from tradingbot.ibkr_orders.records import (
    OrderModeSettingRefused,
    OrderOutcomeUnknown,
    OrderRefused,
    OrderSpec,
)
from tradingbot.ibkr_orders.session import (
    ORDER_MODE_CLIENT_METHODS,
    ORDER_MODE_OUT_MESSAGES,
    PaperOrderSession,
    _OrderGuardedConnection,
    _OrderWrapper,
)
from tradingbot.models import Action, AssetClass, Instrument, MockPolicy

D = Decimal




@pytest.fixture(params=[219, 226], ids=lambda v: f"sv{v}")
def tws(monkeypatch, request):
    def no_network(*args, **kwargs):
        raise AssertionError("network access attempted in an offline test")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket.socket, "connect_ex", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    server = FakeOrderTws()
    server.server_version = request.param
    monkeypatch.setattr(ibapi.client, "Connection", server.connection_factory)
    return server


def init_for(cfg) -> None:
    """The owner's one-time initialisation (scripts/ibkr_paper_init_journal.py)."""
    path, _ = journal_paths(cfg.state_dir, cfg.experiment.name)
    if not path.exists():
        init_journal(cfg.state_dir, cfg.experiment, cfg.connection.masked_account())


def make(tws, tmp_path, *, connect=True, clock=None, init=True, **kw) -> PaperOrderSession:
    cfg = load_order_config(write_cfg(tmp_path))
    if init:
        init_for(cfg)
    s = OfflineTestSession(cfg, **kw)
    s.test_clock = clock
    if connect:
        s.connect()
    return s


def placed(tws) -> int:
    """PLACE_ORDER frames that reached the fake wire."""
    n = 0
    for raw in tws.wire:
        if raw.startswith(b"API\0") or len(raw) < 8:
            continue
        mid = int.from_bytes(raw[4:8], "big")
        n += (mid - PROTOBUF_MSG_ID if mid > PROTOBUF_MSG_ID else mid) == OUT.PLACE_ORDER
    assert n == tws.count("PLACE_ORDER")
    return n


def buy(ref="TB-0001", price="88.12", qty=1):
    return OrderSpec(ref, "BUY", qty, "LMT", limit_price=D(price))


def stop(ref="TB-0001.STP", price="87.00", qty=1):
    return OrderSpec(ref, "SELL", qty, "STP", stop_price=D(price))


def settle(pred, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not pred():
        time.sleep(0.02)
    return pred()


# --- structural --------------------------------------------------------------------------------
def test_read_only_session_still_cannot_send_any_order_message():
    assert not ALLOWED_OUT_MESSAGES & {OUT.PLACE_ORDER, OUT.CANCEL_ORDER, OUT.REQ_GLOBAL_CANCEL,
                                       OUT.REQ_ALL_OPEN_ORDERS, OUT.REQ_EXECUTIONS,
                                       OUT.REQ_COMPLETED_ORDERS, OUT.REQ_IDS}
    with pytest.raises(E.SessionStateError):
        IbkrSession(object())  # type: ignore[arg-type]


def test_order_mode_allowlist_is_exactly_the_read_set_plus_six_ids():
    assert ORDER_MODE_OUT_MESSAGES - ALLOWED_OUT_MESSAGES == {
        OUT.PLACE_ORDER, OUT.CANCEL_ORDER, OUT.REQ_ALL_OPEN_ORDERS, OUT.REQ_EXECUTIONS,
        OUT.REQ_COMPLETED_ORDERS, OUT.REQ_IDS}
    assert {int(m) for m in ORDER_MODE_OUT_MESSAGES - ALLOWED_OUT_MESSAGES} == {3, 4, 7, 8, 16, 99}
    for never in (OUT.REQ_GLOBAL_CANCEL, OUT.EXERCISE_OPTIONS, OUT.REPLACE_FA, OUT.REQ_FA,
                  OUT.UPDATE_CONFIG, OUT.REQ_OPEN_ORDERS, OUT.REQ_AUTO_OPEN_ORDERS,
                  OUT.UPDATE_DISPLAY_GROUP, OUT.SET_SERVER_LOGLEVEL):
        assert never not in ORDER_MODE_OUT_MESSAGES
    added = ORDER_MODE_CLIENT_METHODS - order_session_module.ALLOWED_CLIENT_METHODS
    assert added == {"placeOrderProtoBuf", "validateOrderParameters",
                     "validateAttachedOrdersParameters", "cancelOrderProtoBuf",
                     "reqAllOpenOrdersProtoBuf", "reqExecutionsProtoBuf",
                     "reqCompletedOrdersProtoBuf", "reqIdsProtoBuf"}
    assert all(callable(getattr(EClient, m)) for m in added)


def test_order_wrapper_callback_signatures_match_installed_sdk():
    names = [n for n, v in vars(_OrderWrapper).items() if callable(v) and not n.startswith("_")]
    assert {"openOrder", "orderStatus", "execDetails", "commissionAndFeesReport",
            "completedOrder", "completedOrdersEnd", "openOrderEnd", "nextValidId"} <= set(names)
    for name in names:
        assert list(inspect.signature(getattr(_OrderWrapper, name)).parameters) == \
            list(inspect.signature(getattr(EWrapper, name)).parameters), name


def test_order_session_needs_a_loaded_order_config(tmp_path):
    plain = validate_config({"mode": "paper", "host": "127.0.0.1", "port": 7497, "client_id": 7,
                             "account": PAPER, "tws_profile_ini": "/Users/x/Jts/jts.ini",
                             "tws_os_user": "x", "read_only_check": "config-read"})
    with pytest.raises(E.SessionStateError):
        PaperOrderSession(plain)  # type: ignore[arg-type]
    real = load_order_config(write_cfg(tmp_path))
    forged = PaperOrderConfig(connection=real.connection, experiment=real.experiment,
                              state_dir=real.state_dir)
    with pytest.raises(E.SessionStateError):
        PaperOrderSession(forged)
    odd = replace(C.PAPER_TEST_1, max_submissions=60)
    widened = PaperOrderConfig(connection=real.connection, experiment=odd,
                               state_dir=real.state_dir)
    order_config_module._ISSUED.append(widened)  # even an "issued" config with non-code caps
    try:
        with pytest.raises(E.SessionStateError):
            PaperOrderSession(widened)
    finally:
        order_config_module._ISSUED.remove(widened)


@pytest.mark.parametrize(("settings", "needle"), [
    ({"readOnlyApi": True}, "readOnlyApi=True"),
    ({"readOnlyApi": None}, "readOnlyApi=None"),
    ({"socketPort": 7496}, "socketPort=7496"),
    ({"allowLocalhostOnly": False}, "allowLocalhostOnly=False"),
    ({"allowLocalhostOnly": None}, "allowLocalhostOnly=None"),
])
def test_order_mode_refuses_unless_read_only_is_explicitly_off(tws, tmp_path, settings, needle):
    tws.api_settings.update(settings)
    s = make(tws, tmp_path, connect=False)
    with pytest.raises(OrderModeSettingRefused, match=needle):
        s.connect()
    assert placed(tws) == 0 and "REQ_IDS" not in tws.names()
    assert s.threads_alive() == [] and not s._client.isConnected()
    assert s.submissions_used is None  # the counter lock was released


def test_connect_order_and_explicit_bracket_flags(tws, tmp_path):
    s = make(tws, tmp_path)
    try:
        assert s.order_mode_status == "VERIFIED" and s.contract.con_id == TLT_CON_ID
        assert tws.names()[:3] == ["START_API", "REQ_CONFIG", "REQ_CONTRACT_DATA"]
        snaps = s.place([buy(), stop()])
        parent, child = tws.placed()
        assert parent.order.HasField("transmit") and parent.order.transmit is False
        assert child.order.HasField("transmit") and child.order.transmit is True
        for p in (parent, child):
            assert p.order.HasField("outsideRth") and p.order.outsideRth is False
            assert p.order.account == PAPER and p.order.tif == "DAY"
            assert p.contract.conId == TLT_CON_ID
        assert parent.order.orderRef == "paper-test-1:TB-0001"  # experiment prefix + core ref
        assert child.order.orderRef == "paper-test-1:TB-0001.STP"
        assert [x.order_ref for x in snaps] == ["TB-0001", "TB-0001.STP"]  # lookups by core ref
        assert child.order.parentId == parent.orderId == snaps[0].order_id
        assert [x.status for x in snaps] == [OrderStatus.SUBMITTED, OrderStatus.PRE_SUBMITTED]
        assert s.submissions_used == 2
        assert tws.names().count("REQ_IDS") == 1  # ids come from the broker's nextValidId
    finally:
        s.disconnect()


@pytest.mark.parametrize("name", ["placeOrder", "cancelOrder", "reqGlobalCancel",
                                  "exerciseOptions", "updateConfigProtoBuf", "reqAutoOpenOrders",
                                  "reqOpenOrders", "replaceFA", "reqGlobalCancelProtoBuf",
                                  "exerciseOptionsProtoBuf", "replaceFAProtoBuf"])
def test_denied_methods_raise_and_send_nothing(tws, tmp_path, name):
    s = make(tws, tmp_path)
    try:
        wire = list(tws.wire)
        with pytest.raises(ReadOnlyViolation):
            getattr(s._client, name)(1, None, None)
        assert tws.wire == wire
    finally:
        s.disconnect()


def test_direct_sends_without_a_guard_permit_never_reach_the_wire(tws, tmp_path):
    s = make(tws, tmp_path)
    try:
        s.place([buy()])
        good = tws.placed()[0]
        wire, used = list(tws.wire), s.submissions_used
        replay = PlaceOrderRequest()
        replay.CopyFrom(good)
        with pytest.raises(ReadOnlyViolation):  # replaying exactly the permitted bytes
            s._client.placeOrderProtoBuf(replay)
        with pytest.raises(ReadOnlyViolation):
            EClient.sendMsgProtoBuf(s._client, OUT.PLACE_ORDER + PROTOBUF_MSG_ID,
                                    good.SerializeToString())
        with pytest.raises(ReadOnlyViolation):
            EClient.sendMsg(s._client, OUT.PLACE_ORDER, "1\0")
        with pytest.raises(ReadOnlyViolation):
            EClient.reqGlobalCancelProtoBuf(s._client, GlobalCancelRequest())
        with pytest.raises(ReadOnlyViolation):
            EClient.sendMsgProtoBuf(s._client, OUT.UPDATE_CONFIG + PROTOBUF_MSG_ID, b"")
        with pytest.raises(ReadOnlyViolation):  # a cancel needs its own permit too
            EClient.cancelOrderProtoBuf(s._client, order_session_module.CancelOrderRequest(
                orderId=good.orderId))
        assert tws.wire == wire and s.submissions_used == used
    finally:
        s.disconnect()


def test_object_graph_of_order_session_reaches_no_unguarded_order_send(tws, tmp_path):
    s = make(tws, tmp_path)
    try:
        s.place([buy(), stop()])
        reachable = _walk(s)
        assert len(reachable) > 1000
        assert not [p for p, o in reachable if isinstance(o, socket.socket)
                    or type(o).__name__ == "FakeConnection"]
        assert any(isinstance(o, _OrderGuardedConnection) for _, o in reachable)
        wire, used = list(tws.wire), s.submissions_used
        called = _fire_send_like(reachable)
        assert called
        assert placed(tws) == 2 and s.submissions_used == used
        assert [w for w in tws.wire[len(wire):] if b"" != w] == []
    finally:
        s.disconnect()


# --- guard refusals over the fake: nothing reaches the wire -------------------------------------
def test_second_share_refused(tws, tmp_path):
    s = make(tws, tmp_path)
    try:
        snaps = s.place([buy(), stop()])
        tws.fill(snaps[0].order_id, 1, 88.11)
        assert settle(lambda: s.open_orders()[0].status == OrderStatus.SUBMITTED)
        with pytest.raises(OrderRefused) as info:
            s.place([buy("TB-0002")])
        assert info.value.refusal.reason == RefusalReason.POSITION_CAP
        assert placed(tws) == 2
    finally:
        s.disconnect()


@pytest.mark.parametrize(("spec", "reason"), [
    (buy(price="151.00"), RefusalReason.NOTIONAL_CAP),
    (buy(qty=2), RefusalReason.ORDER_QUANTITY_CAP),
    (OrderSpec("TB-M", "BUY", 1, "MKT"), RefusalReason.MARKET_ORDER),
    (OrderSpec("TB-S", "SELL", 1, "LMT", limit_price=D("88.00")), RefusalReason.SHORT_SALE),
    (buy(price="88.115"), RefusalReason.OFF_TICK_PRICE),
    (OrderSpec("TB-G", "BUY", 1, "LMT", limit_price=D("88.12"), tif="GTC"),
     RefusalReason.TIF_NOT_ALLOWED),
])
def test_cap_refusals_send_nothing(tws, tmp_path, spec, reason):
    s = make(tws, tmp_path)
    try:
        with pytest.raises(OrderRefused) as info:
            s.place([spec])
        assert info.value.refusal.reason == reason
        assert placed(tws) == 0 and s.submissions_used == 0
    finally:
        s.disconnect()


def test_sell_exceeding_position_net_of_working_sells_refused(tws, tmp_path):
    s = make(tws, tmp_path)
    try:
        snaps = s.place([buy(), stop()])
        tws.fill(snaps[0].order_id, 1, 88.11)
        assert settle(lambda: any(o.status == OrderStatus.SUBMITTED for o in s.open_orders()))
        with pytest.raises(OrderRefused) as info:  # 1 held, 1 already committed to the stop
            s.place([OrderSpec("TB-X", "SELL", 1, "LMT", limit_price=D("88.50"))])
        assert info.value.refusal.reason == RefusalReason.SELL_EXCEEDS_POSITION
        assert placed(tws) == 2
    finally:
        s.disconnect()


def test_outside_liquid_hours_refused(tws, tmp_path):
    tws.liquid_hours = "20261111:0930-20261111:1600;20261112:0930-20261112:1600"
    s = make(tws, tmp_path, clock=lambda: datetime(2026, 11, 11, 8, 0, tzinfo=UTC))
    try:
        with pytest.raises(OrderRefused) as info:
            s.place([buy()])
        assert info.value.refusal.reason == RefusalReason.OUTSIDE_LIQUID_HOURS
        assert placed(tws) == 0
    finally:
        s.disconnect()


def test_wrong_symbol_and_wrong_contract_id_refused_by_the_broker(tws, tmp_path):
    s = make(tws, tmp_path)
    b = IbkrPaperBroker(s)
    try:
        for contract in (ContractRef(symbol="SPY", asset_class=AssetClass.ETF, con_id=756733),
                         ContractRef(symbol="TLT", asset_class=AssetClass.ETF, con_id=999)):
            with pytest.raises(BrokerError):
                b.submit(OrderIntent(order_ref="TB-W", contract=contract, side=OrderSide.BUY,
                                     quantity=1, order_type=OrderType.LMT,
                                     limit_price=D("88.12")))
        with pytest.raises(BrokerError):
            b.resolve(Instrument(asset_class=AssetClass.EQUITY, symbol="SPY"))
        assert placed(tws) == 0
    finally:
        s.disconnect()


def test_a_session_resolving_another_contract_refuses_order_mode(tws, tmp_path, monkeypatch):
    monkeypatch.setattr(C, "PAPER_TEST_1", C.PAPER_TEST_1)  # unchanged; the fake answers SPY
    original = FakeOrderTws.contract_handler

    def spy(s, conn, req):
        from _ibkr_fake_tws import frame
        from ibapi.message import IN
        from ibapi.protobuf.Contract_pb2 import Contract as CP
        from ibapi.protobuf.ContractData_pb2 import ContractData
        from ibapi.protobuf.ContractDataEnd_pb2 import ContractDataEnd
        from ibapi.protobuf.ContractDetails_pb2 import ContractDetails as CD
        conn.push(frame(IN.CONTRACT_DATA, ContractData(reqId=req.reqId, contract=CP(
            conId=1, symbol="TLT", secType="OPT", currency="USD"), contractDetails=CD(
            minTick="0.01", timeZoneId="US/Eastern"))))
        conn.push(frame(IN.CONTRACT_DATA_END, ContractDataEnd(reqId=req.reqId)))

    tws.on["REQ_CONTRACT_DATA"] = spy
    s = make(tws, tmp_path, connect=False)
    with pytest.raises(E.IbkrError):
        s.connect()
    assert placed(tws) == 0 and not s._client.isConnected()
    assert original is FakeOrderTws.contract_handler


def test_seventh_submission_refused_including_after_a_restart(tws, tmp_path):
    s = make(tws, tmp_path)
    try:
        snaps = s.place([buy(), stop()])  # 1, 2
        for i, price in enumerate(("87.10", "87.20", "87.30", "87.40"), start=3):
            assert s.modify(snaps[1].order_id, stop_price=D(price)).stop_price == D(price)
            assert s.submissions_used == i
        with pytest.raises(OrderRefused) as info:  # the seventh: an amendment
            s.modify(snaps[1].order_id, stop_price=D("87.50"))
        assert info.value.refusal.reason == RefusalReason.EXPERIMENT_FINISHED
        assert placed(tws) == 6
    finally:
        s.disconnect()
    again = make(tws, tmp_path)  # a new process on the same state directory
    try:
        assert again.submissions_used == 6 and again.submissions_remaining == 0
        with pytest.raises(OrderRefused) as info:
            again.place([buy("TB-0009")])
        assert info.value.refusal.reason == RefusalReason.EXPERIMENT_FINISHED
        assert placed(tws) == 6
        child = max(again.order_ids())  # cancels stay possible: they only reduce risk
        assert again.cancel(child).status == OrderStatus.CANCELLED
    finally:
        again.disconnect()


def test_bracket_that_would_cross_the_cap_sends_nothing(tws, tmp_path):
    s = make(tws, tmp_path)
    try:
        snaps = s.place([buy(), stop()])
        for price in ("87.10", "87.20", "87.30"):
            s.modify(snaps[1].order_id, stop_price=D(price))
        s.cancel(snaps[0].order_id)
        assert s.submissions_used == 5
        with pytest.raises(OrderRefused) as info:
            s.place([buy("TB-0002"), stop("TB-0002.STP")])
        assert info.value.refusal.reason == RefusalReason.SUBMISSION_CAP
        assert placed(tws) == 5
    finally:
        s.disconnect()


def test_read_only_on_at_the_broker_rejects_orders(tws, tmp_path):
    """The config read said OFF but TWS still answers every order with 321 (Read-Only mode)."""
    s = make(tws, tmp_path)
    tws.reject_all = READ_ONLY_REJECTION
    try:
        snaps = s.place([buy(), stop()])
        assert [x.status for x in snaps] == [OrderStatus.REJECTED, OrderStatus.REJECTED]
        assert "321" in snaps[0].reason
    finally:
        s.disconnect()


# --- lifecycle through the real coordinator ----------------------------------------------------
def coordinator(tws, tmp_path, **cfg):
    s = make(tws, tmp_path)
    policy = MockPolicy(quantity=1, quantity_per_add=1, max_notional_usd=D(150),
                        max_price_deviation_pct=D(1))
    broker = IbkrPaperBroker(s, exit_collar_pct=policy.max_price_deviation_pct)
    config = ExecutionConfig(halt_file=tmp_path / "HALT", expected_account=PAPER,
                             protective_tif=TimeInForce.DAY, confirm_attempts=10,
                             confirm_wait_seconds=0.05, **cfg)
    coord = ExecutionCoordinator(broker, tmp_path / "exec.sqlite3", policy, config)
    return s, broker, coord


def run(coord, source_id, act):
    now = datetime.now(UTC)
    return coord.execute(source(source_id, at=now), act, APPROVE, now=now)


def tlt_open(**kw):
    return action("open", Action.OPEN, symbol="TLT", entry=kw.pop("entry", "88.11"),
                  stop=kw.pop("stop", "87.00"), **kw)


def fake_id(tws, ref):
    return max(o.order_id for o in tws.orders.values() if core_ref(o.order.orderRef) == ref)


def test_full_lifecycle_within_the_six_submission_cap(tws, tmp_path):
    s, _broker, coord = coordinator(tws, tmp_path)
    try:
        out = run(coord, "p1", tlt_open())
        assert out.state == IntentState.WORKING, out
        assert s.submissions_used == 2  # bracket: entry + protective stop
        tws.fill(fake_id(tws, out.order_ref), 1, 88.11)
        assert settle(lambda: coord.sync() and coord.ledger()[0].quantity == 1)
        assert coord.intent(out.order_ref).state == IntentState.FILLED
        assert coord.ledger()[0].protection == "CONFIRMED"  # the stop is seen working
        idea = coord.ledger()[0].idea_id
        mod = run(coord, "p2", action("raise", Action.MODIFY, idea=idea, stop="87.50",
                                      symbol="TLT"))
        assert mod.state == IntentState.APPLIED, mod
        assert s.submissions_used == 3
        stop_id = fake_id(tws, f"{out.order_ref}.STP")
        assert tws.orders[stop_id].order.auxPrice == 87.5
        close = run(coord, "p3", action("close", Action.CLOSE, idea=idea, symbol="TLT"))
        assert close.state == IntentState.WORKING, close
        assert s.submissions_used == 4  # the stop was cancelled (not counted), the close placed
        assert tws.orders[stop_id].status == "Cancelled"
        tws.fill(fake_id(tws, close.order_ref), 1)
        assert settle(lambda: coord.sync() and coord.ledger()[0].quantity == 0)
        report = coord.reconcile()
        assert report.clean, report
        assert placed(tws) == 4 and s.submissions_remaining == 2
        assert s.submissions_used <= C.PAPER_TEST_1.max_submissions
    finally:
        coord.close()
        s.disconnect()


def test_lost_acknowledgement_is_recovered_by_lookup_without_a_second_order(tws, tmp_path):
    s, _broker, coord = coordinator(tws, tmp_path)
    try:
        tws.ack_lost = True
        out = run(coord, "p1", tlt_open())
        assert placed(tws) == 2
        assert "ACK_LOST" in [e.event for e in coord.store.journal(out.order_ref)]
        assert "ADOPTED_FROM_BROKER" in [e.event for e in coord.store.journal(out.order_ref)]
        assert coord.intent(out.order_ref).state == IntentState.WORKING
        tws.ack_lost = False
        coord.recover()
        assert placed(tws) == 2 and s.submissions_used == 2  # never resent
    finally:
        coord.close()
        s.disconnect()


def test_submit_without_any_acknowledgement_raises_ack_lost(tws, tmp_path):
    s = make(tws, tmp_path)
    b = IbkrPaperBroker(s)
    tws.ack_lost = True
    try:
        contract = b.resolve(Instrument(asset_class=AssetClass.ETF, symbol="TLT"))
        assert contract.con_id == TLT_CON_ID
        with pytest.raises(AckLost):
            b.submit(OrderIntent(order_ref="TB-L", contract=contract, side=OrderSide.BUY,
                                 quantity=1, order_type=OrderType.LMT, limit_price=D("88.12")))
        assert placed(tws) == 1
        assert [o.order_ref for o in b.open_orders()] == ["TB-L"]  # visible by orderRef
    finally:
        s.disconnect()


def test_rejection_is_a_known_outcome(tws, tmp_path):
    s, _broker, coord = coordinator(tws, tmp_path)
    try:
        ref = order_ref_for("p1", 1, "open")
        tws.reject_refs[ref] = (201, "Order rejected - reason: invented test rejection")
        tws.reject_refs[ref + ".STP"] = (201, "Order rejected - reason: parent rejected")
        out = run(coord, "p1", tlt_open())
        assert out.state == IntentState.REJECTED, out
        assert placed(tws) == 2
        coord.recover()
        assert placed(tws) == 2
    finally:
        coord.close()
        s.disconnect()


def test_partial_bracket_is_never_reported_as_success(tws, tmp_path):
    """Parent accepted, protective child rejected: the broker cancels the parent and raises
    PartialBracket (an AckLost); the coordinator looks up and adopts the cancelled entry."""
    s, _broker, coord = coordinator(tws, tmp_path)
    try:
        ref = order_ref_for("p1", 1, "open")
        tws.reject_refs[ref + ".STP"] = (201, "Order rejected - reason: invented child reject")
        out = run(coord, "p1", tlt_open())
        events = [e.event for e in coord.store.journal(out.order_ref)]
        assert "ACK_LOST" in events and "ACK" not in events
        assert out.state in (IntentState.CANCELLED, IntentState.UNRESOLVED), out
        assert tws.orders[fake_id(tws, ref)].status == "Cancelled"
        assert placed(tws) == 2
    finally:
        coord.close()
        s.disconnect()


def test_partial_bracket_at_session_level(tws, tmp_path):
    s = make(tws, tmp_path)
    b = IbkrPaperBroker(s)
    tws.reject_refs["TB-0001.STP"] = (201, "Order rejected - reason: invented")
    try:
        with pytest.raises(OrderOutcomeUnknown) as info:
            s.place([buy(), stop()])
        assert type(info.value).__name__ == "PartialBracket"
        assert "TB-0001" in str(info.value) or info.value.orders
        assert tws.count("CANCEL_ORDER") == 1
        contract = b.resolve(Instrument(asset_class=AssetClass.ETF, symbol="TLT"))
        tws.reject_refs["TB-0002.STP"] = (201, "Order rejected - reason: invented")
        entry = OrderIntent(order_ref="TB-0002", contract=contract, side=OrderSide.BUY,
                            quantity=1, order_type=OrderType.LMT, limit_price=D("88.12"))
        child = OrderIntent(order_ref="TB-0002.STP", contract=contract, side=OrderSide.SELL,
                            quantity=1, order_type=OrderType.STP, stop_price=D("87.00"),
                            parent_ref="TB-0002")
        with pytest.raises(PartialBracket):
            b.submit(entry, [child])
        assert issubclass(PartialBracket, AckLost)
    finally:
        s.disconnect()


def test_partial_fill_maps_to_partially_filled(tws, tmp_path, monkeypatch):
    """Two-share test-only experiment (caps registry patched in this test only)."""
    two = replace(C.PAPER_TEST_1, name="test-two-shares", max_order_quantity=2,
                  max_net_position=2, max_order_notional=D(300))
    reg = MappingProxyType({two.name: two})
    monkeypatch.setattr(order_config_module, "EXPERIMENTS", reg)
    monkeypatch.setattr(order_session_module, "EXPERIMENTS", reg)
    cfg = load_order_config(write_cfg(tmp_path, orders="test-two-shares"))
    init_for(cfg)
    s = OfflineTestSession(cfg)
    s.connect()
    b = IbkrPaperBroker(s)
    try:
        snaps = s.place([buy(qty=2)])
        tws.fill(snaps[0].order_id, 1, 88.10)
        assert settle(lambda: b.open_orders()[0].status == OrderStatus.PARTIALLY_FILLED)
        o = b.open_orders()[0]
        assert (o.quantity, o.filled_quantity, o.remaining) == (2, 1, 1)
        assert [f.quantity for f in b.executions()] == [1]
        tws.fill(snaps[0].order_id, 1, 88.12)
        assert settle(lambda: b.open_orders() == [])
        assert sum(f.quantity for f in b.executions()) == 2
        done = [x for x in b.completed_orders() if x.order_ref == "TB-0001"]
        assert done and done[0].status == OrderStatus.FILLED
    finally:
        s.disconnect()


def test_duplicate_and_stale_callbacks_never_double_count(tws, tmp_path):
    s, broker, coord = coordinator(tws, tmp_path)
    tws.duplicate_callbacks = True
    tws.stale_after_fill = True
    try:
        out = run(coord, "p1", tlt_open())
        exec_id = tws.fill(fake_id(tws, out.order_ref), 1, 88.11)
        assert settle(lambda: coord.sync() and coord.ledger()[0].quantity == 1)
        coord.sync()
        coord.sync()
        assert coord.ledger()[0].quantity == 1
        assert [f.exec_id for f in broker.executions()] == [exec_id]
        entry = s._tracks[fake_id(tws, out.order_ref)]
        assert entry.status == OrderStatus.FILLED  # the late 'Submitted' was ignored
        assert placed(tws) == 2
    finally:
        coord.close()
        s.disconnect()


@pytest.mark.parametrize("where", ["open_order", "execution", "completed"])
def test_foreign_account_callback_poisons_the_session(tws, tmp_path, where):
    s = make(tws, tmp_path)
    b = IbkrPaperBroker(s)
    try:
        b.resolve(Instrument(asset_class=AssetClass.ETF, symbol="TLT"))
        if where == "open_order":
            tws.foreign_open_order = True
            with pytest.raises(BrokerError):
                b.open_orders()
        elif where == "execution":
            tws.push_foreign_execution()
            assert settle(lambda: s.poisoned)
        else:
            tws.foreign_completed = True
            with pytest.raises(BrokerError):
                b.completed_orders()
        assert s.poisoned
        with pytest.raises(BrokerError):
            b.submit(OrderIntent(order_ref="TB-P", contract=b.resolve(Instrument(
                asset_class=AssetClass.ETF, symbol="TLT")), side=OrderSide.BUY, quantity=1,
                order_type=OrderType.LMT, limit_price=D("88.12")))
        assert placed(tws) == 0
    finally:
        s.disconnect()


def test_broker_reports_contract_ids_and_raises_on_unreadable(tws, tmp_path):
    s = make(tws, tmp_path)
    b = IbkrPaperBroker(s)
    try:
        contract = b.resolve(Instrument(asset_class=AssetClass.ETF, symbol="TLT"))
        q = b.quote(contract)
        assert q.quality.value == "LIVE" and q.bid == D("88.1")
        snaps = s.place([buy(), stop()])
        tws.fill(snaps[0].order_id, 1, 88.11)
        assert settle(lambda: b.positions() and b.positions()[0].quantity == 1)
        pos = b.positions()[0]
        assert pos.contract.con_id == TLT_CON_ID and pos.account == PAPER
        oo = b.open_orders()
        assert [(o.order_ref, o.contract.con_id, o.order_type, o.side, o.stop_price, o.parent_ref)
                for o in oo] == [("TB-0001.STP", TLT_CON_ID, OrderType.STP, OrderSide.SELL,
                                  D("87.00"), "TB-0001")]
        assert all(f.contract.con_id == TLT_CON_ID for f in b.executions())
        tws.fail_send = {"REQ_ALL_OPEN_ORDERS"}
        with pytest.raises(BrokerError):
            b.open_orders()  # a failed read raises; it never returns an empty list
    finally:
        s.disconnect()


def test_delayed_quote_is_not_upgraded(tws, tmp_path):
    tws.market_data_type = 3
    s = make(tws, tmp_path)
    b = IbkrPaperBroker(s)
    try:
        q = b.quote(b.resolve(Instrument(asset_class=AssetClass.ETF, symbol="TLT")))
        assert q.quality.value == "DELAYED"
    finally:
        s.disconnect()


# --- review round 2: Inactive is not final at this broker -------------------------------------
def test_inactive_order_still_counts_as_working_and_can_be_cancelled(tws, tmp_path):
    s = make(tws, tmp_path)
    b = IbkrPaperBroker(s)
    try:
        snaps = s.place([buy(), stop()])
        tws.set_status(snaps[0].order_id, "Inactive")
        assert settle(lambda: s._tracks[snaps[0].order_id].raw_status == "Inactive")
        with pytest.raises(OrderRefused) as info:  # the inactive BUY can still become live
            s.place([buy("TB-0002")])
        assert info.value.refusal.reason == RefusalReason.POSITION_CAP
        surfaced = {o.order_ref: o for o in b.open_orders()}["TB-0001"]
        assert surfaced.status not in (OrderStatus.REJECTED, OrderStatus.INACTIVE)
        assert surfaced.status == OrderStatus.UNKNOWN and "INACTIVE" in surfaced.reason
        assert s.cancel(snaps[0].order_id).status == OrderStatus.CANCELLED
        assert placed(tws) == 2
    finally:
        s.disconnect()


def test_inactive_order_can_become_active_again_or_be_rejected(tws, tmp_path):
    s = make(tws, tmp_path)
    try:
        snaps = s.place([buy(), stop()])
        parent, child = snaps[0].order_id, snaps[1].order_id
        tws.set_status(parent, "Inactive")
        assert settle(lambda: s._tracks[parent].raw_status == "Inactive")
        tws.set_status(parent, "Submitted")  # merely held: active again
        assert settle(lambda: s._tracks[parent].status == OrderStatus.SUBMITTED)
        tws.set_status(child, "Inactive")
        assert settle(lambda: s._tracks[child].raw_status == "Inactive")
        from _ibkr_fake_tws import error_frame
        tws.push(error_frame(child, 201, "Order rejected - reason: invented"))
        assert settle(lambda: s._tracks[child].status == OrderStatus.REJECTED)
    finally:
        s.disconnect()


# --- review round 2: the position read can lag this session's own fills -----------------------
def test_probe_second_buy_refused_while_the_position_read_lags_the_entry_fill(tws, tmp_path):
    tws.positions = []
    s = make(tws, tmp_path)
    try:
        snaps = s.place([buy(), stop()])
        tws.positions_lag = True
        tws.fill(snaps[0].order_id, 1, 88.11)  # long 1; the read still says 0
        assert settle(lambda: s._tracks[snaps[0].order_id].status == OrderStatus.FILLED)
        with pytest.raises(OrderRefused) as info:
            s.place([buy("TB-0002")])
        assert info.value.refusal.reason == RefusalReason.POSITION_DISAGREEMENT
        tws.catch_up_positions()  # the read agrees again: the cap decides
        with pytest.raises(OrderRefused) as info:
            s.place([buy("TB-0003")])
        assert info.value.refusal.reason == RefusalReason.POSITION_CAP
        assert placed(tws) == 2
    finally:
        s.disconnect()


def test_probe_second_sell_refused_while_the_position_read_lags_the_closing_fill(tws, tmp_path):
    tws.positions = []
    s = make(tws, tmp_path)
    try:
        snaps = s.place([buy(), stop()])
        tws.fill(snaps[0].order_id, 1, 88.11)
        assert settle(lambda: s.positions() and s.positions()[0].quantity == 1)
        s.cancel(snaps[1].order_id)  # the stop is gone
        close = s.place([OrderSpec("TB-C", "SELL", 1, "LMT", limit_price=D("88.00"))])
        tws.positions_lag = True
        tws.fill(close[0].order_id, 1, 88.00)  # flat; the read still says 1
        assert settle(lambda: s._tracks[close[0].order_id].status == OrderStatus.FILLED)
        with pytest.raises(OrderRefused) as info:
            s.place([OrderSpec("TB-C2", "SELL", 1, "LMT", limit_price=D("88.00"))])
        assert info.value.refusal.reason == RefusalReason.POSITION_DISAGREEMENT
        assert placed(tws) == 3
    finally:
        s.disconnect()


def test_order_status_fill_ahead_of_its_execution_is_counted(tws, tmp_path):
    """orderStatus says Filled but execDetails has not arrived: the session's own figure is the
    larger of the two per order, so the read (0) and the session (1) disagree."""
    tws.positions = []
    s = make(tws, tmp_path)
    try:
        snaps = s.place([buy(), stop()])
        tws.positions_lag = True
        fo = tws.orders[snaps[0].order_id]
        fo.filled = D(1)
        tws.set_status(fo.order_id, "Filled")  # status only, no execution
        assert settle(lambda: s._tracks[fo.order_id].status == OrderStatus.FILLED)
        with pytest.raises(OrderRefused) as info:
            s.place([buy("TB-0002")])
        assert info.value.refusal.reason == RefusalReason.POSITION_DISAGREEMENT
    finally:
        s.disconnect()


# --- review round 2: an own order sent but not yet visible still counts ------------------------
def _long_one_without_stop(tws, s):
    snaps = s.place([buy(), stop()])
    tws.fill(snaps[0].order_id, 1, 88.11)
    assert settle(lambda: s.positions() and s.positions()[0].quantity == 1)
    s.cancel(snaps[1].order_id)
    return snaps


def _sell(ref, price="88.00"):
    return OrderSpec(ref, "SELL", 1, "LMT", limit_price=D(price))


def test_sell_sent_but_not_yet_visible_blocks_a_second_sell(tws, tmp_path):
    s = make(tws, tmp_path)
    try:
        _long_one_without_stop(tws, s)
        tws.invisible = True  # accepted by the broker, in no callback or lookup yet
        with pytest.raises(OrderOutcomeUnknown):
            s.place([_sell("TB-C1")])
        assert s.open_orders() == []  # the broker's view does not show it
        with pytest.raises(OrderRefused) as info:
            s.place([_sell("TB-C2")])
        assert info.value.refusal.reason == RefusalReason.SELL_EXCEEDS_POSITION
        assert placed(tws) == 3
    finally:
        s.disconnect()


def test_buy_sent_but_not_yet_visible_blocks_a_second_buy(tws, tmp_path):
    s = make(tws, tmp_path)
    try:
        tws.invisible = True
        with pytest.raises(OrderOutcomeUnknown):
            s.place([buy()])
        with pytest.raises(OrderRefused) as info:
            s.place([buy("TB-0002")])
        assert info.value.refusal.reason == RefusalReason.POSITION_CAP
        assert placed(tws) == 1
    finally:
        s.disconnect()


def test_a_confirmed_cancel_frees_the_second_sell(tws, tmp_path):
    s = make(tws, tmp_path)
    try:
        _long_one_without_stop(tws, s)
        tws.invisible = True
        with pytest.raises(OrderOutcomeUnknown):
            s.place([_sell("TB-C1")])
        tws.invisible = False
        first = max(s.order_ids())
        assert s.cancel(first).status == OrderStatus.CANCELLED  # 202: confirmed terminal
        s.place([_sell("TB-C2")])
        assert placed(tws) == 4
    finally:
        s.disconnect()


# --- review round 2: the submission budget cannot be reset -------------------------------------
def _journal(tmp_path):
    return tmp_path / "state" / "ibkr_paper_orders" / "paper-test-1.submissions"


def test_deleting_the_journal_never_gives_a_fresh_budget(tws, tmp_path):
    s = make(tws, tmp_path)
    try:
        s.place([buy(), stop()])
    finally:
        s.disconnect()
    _journal(tmp_path).unlink()
    again = make(tws, tmp_path, init=False)
    try:
        with pytest.raises(OrderRefused) as info:
            again.place([buy("TB-0009")])
        assert info.value.refusal.reason == RefusalReason.SUBMISSION_COUNT_UNKNOWN
        assert not _journal(tmp_path).exists()  # the session never creates it
        assert placed(tws) == 2
    finally:
        again.disconnect()


def test_truncating_the_journal_refuses_everything(tws, tmp_path):
    s = make(tws, tmp_path)
    try:
        snaps = s.place([buy(), stop()])
        for price in ("87.10", "87.20", "87.30"):
            s.modify(snaps[1].order_id, stop_price=D(price))
    finally:
        s.disconnect()
    lines = _journal(tmp_path).read_bytes().splitlines(keepends=True)
    _journal(tmp_path).write_bytes(b"".join(lines[:2]))  # the reviewer's 5 -> 1 cut
    again = make(tws, tmp_path)
    try:
        with pytest.raises(OrderRefused) as info:
            again.place([buy("TB-0009")])
        assert info.value.refusal.reason == RefusalReason.SUBMISSION_COUNT_UNKNOWN
        assert placed(tws) == 5
    finally:
        again.disconnect()


def test_a_new_journal_while_the_broker_shows_more_is_a_mismatch(tws, tmp_path):
    """Journal and head deleted, a new journal initialised: the broker's own history (orderRef
    prefix) shows 2 orders of the experiment, the journal 0."""
    s = make(tws, tmp_path)
    try:
        snaps = s.place([buy(), stop()])
        s.cancel(snaps[0].order_id)
    finally:
        s.disconnect()
    path, head = journal_paths(tmp_path / "state", "paper-test-1")
    path.unlink()
    head.unlink()
    again = make(tws, tmp_path)  # re-initialised
    try:
        assert again.submissions_used == 0
        with pytest.raises(OrderRefused) as info:
            again.place([buy("TB-0009")])
        assert info.value.refusal.reason == RefusalReason.SUBMISSION_COUNT_MISMATCH
        assert placed(tws) == 2
    finally:
        again.disconnect()


def test_an_unconfirmed_sell_from_the_journal_blocks_a_second_sell_after_restart(tws, tmp_path):
    s = make(tws, tmp_path)
    try:
        _long_one_without_stop(tws, s)
        tws.invisible = True
        with pytest.raises(OrderOutcomeUnknown):
            s.place([_sell("TB-C1")])
    finally:
        s.disconnect()
    again = make(tws, tmp_path)  # a new process: only the journal knows about TB-C1
    try:
        assert again.open_orders() == []
        with pytest.raises(OrderRefused) as info:
            again.place([_sell("TB-C2")])
        assert info.value.refusal.reason == RefusalReason.SELL_EXCEEDS_POSITION
        assert placed(tws) == 3
    finally:
        again.disconnect()


def test_a_confirmed_cancel_frees_the_second_sell_after_restart(tws, tmp_path):
    s = make(tws, tmp_path)
    try:
        _long_one_without_stop(tws, s)
        tws.invisible = True
        with pytest.raises(OrderOutcomeUnknown):
            s.place([_sell("TB-C1")])
        tws.invisible = False
        assert s.cancel(max(s.order_ids())).status == OrderStatus.CANCELLED
    finally:
        s.disconnect()
    again = make(tws, tmp_path)
    try:
        again.place([_sell("TB-C2")])  # completed orders show TB-C1 cancelled
        assert placed(tws) == 4
    finally:
        again.disconnect()


def test_the_production_constructor_always_runs_the_local_checks(tws, tmp_path):
    """No argument can skip run_preflight: on this machine the configured jts.ini does not exist,
    so connect() refuses before any socket (the fake transport sees no frame)."""
    cfg = load_order_config(write_cfg(tmp_path))
    init_for(cfg)
    s = PaperOrderSession(cfg, connect_timeout=2.0)
    with pytest.raises(E.IbkrError):
        s.connect()
    assert tws.wire == [] and s.preflight_result is None
    for hook in ("preflight", "clock"):
        with pytest.raises(TypeError):
            PaperOrderSession(cfg, **{hook: lambda *a: None})


def test_contract_refs_carry_mains_contract_fields_and_key(tws, tmp_path):
    s = make(tws, tmp_path)
    b = IbkrPaperBroker(s)
    try:
        ref = b.resolve(Instrument(asset_class=AssetClass.ETF, symbol="TLT"))
        assert (ref.con_id, ref.sec_type, ref.local_symbol) == (TLT_CON_ID, "STK", "TLT")
        assert (ref.multiplier, ref.min_tick, ref.size_increment) == (D(1), D("0.01"), 1)
        assert (ref.expiry, ref.strike, ref.right) == (None, None, None)
        assert ref.contract_key == f"STK:TLT:-:-:-:{TLT_CON_ID}"
        snaps = s.place([buy(), stop()])
        tws.fill(snaps[0].order_id, 1, 88.11)
        assert settle(lambda: b.positions() and b.positions()[0].quantity == 1)
        keys = {p.contract.contract_key for p in b.positions()}
        keys |= {o.contract.contract_key for o in b.open_orders()}
        keys |= {f.contract.contract_key for f in b.executions()}
        assert keys == {ref.contract_key}
        assert b.positions()[0].contract.local_symbol == "TLT"
        o = b.open_orders()[0]  # each order's own status and quantities, never inferred
        assert (o.status, o.quantity, o.filled_quantity, o.remaining) == \
            (OrderStatus.SUBMITTED, 1, 0, 1)
    finally:
        s.disconnect()


# --- confirmation review: the cross-check survives a restart -----------------------------------
def test_probe_restart_while_the_read_lags_an_entry_fill_refuses_a_second_buy(tws, tmp_path):
    s = make(tws, tmp_path)
    try:
        snaps = s.place([buy(), stop()])
        tws.positions_lag = True
        tws.fill(snaps[0].order_id, 1, 88.11)  # long 1; the read still says 0
        assert settle(lambda: s._tracks[snaps[0].order_id].status == OrderStatus.FILLED)
    finally:
        s.disconnect()
    again = make(tws, tmp_path)  # restart while the read still lags
    try:
        with pytest.raises(OrderRefused) as info:
            again.place([buy("TB-0002")])
        assert info.value.refusal.reason == RefusalReason.POSITION_DISAGREEMENT
        tws.catch_up_positions()  # the read agrees again: the cap decides
        with pytest.raises(OrderRefused) as info:
            again.place([buy("TB-0003")])
        assert info.value.refusal.reason == RefusalReason.POSITION_CAP
        assert placed(tws) == 2
    finally:
        again.disconnect()


def test_probe_restart_while_the_read_lags_a_closing_fill_refuses_a_second_sell(tws, tmp_path):
    s = make(tws, tmp_path)
    try:
        _long_one_without_stop(tws, s)
        close = s.place([_sell("TB-C1")])
        tws.positions_lag = True
        tws.fill(close[0].order_id, 1, 88.00)  # flat; the read still says 1
        assert settle(lambda: s._tracks[close[0].order_id].status == OrderStatus.FILLED)
    finally:
        s.disconnect()
    again = make(tws, tmp_path)
    try:
        with pytest.raises(OrderRefused) as info:
            again.place([_sell("TB-C2")])
        assert info.value.refusal.reason == RefusalReason.POSITION_DISAGREEMENT
        assert placed(tws) == 3
    finally:
        again.disconnect()


# --- confirmation review: never cancel the stop for an exit the guard would refuse --------------
def _held_with_stop(tws, tmp_path):
    s, broker, coord = coordinator(tws, tmp_path)
    out = run(coord, "p1", tlt_open())
    tws.fill(fake_id(tws, out.order_ref), 1, 88.11)
    assert settle(lambda: coord.sync() and coord.ledger()[0].quantity == 1)
    return s, broker, coord, out


def test_a_close_the_guard_would_refuse_cancels_nothing(tws, tmp_path):
    s, _broker, coord, out = _held_with_stop(tws, tmp_path)
    try:
        idea = coord.ledger()[0].idea_id
        for i, price in enumerate(("87.10", "87.20", "87.30", "87.40")):
            assert run(coord, f"m{i}", action(f"raise{i}", Action.MODIFY, idea=idea, stop=price,
                                              symbol="TLT")).state == IntentState.APPLIED
        assert s.submissions_used == 6  # the close would be EXPERIMENT_FINISHED
        close = run(coord, "c1", action("close", Action.CLOSE, idea=idea, symbol="TLT"))
        assert close.state != IntentState.WORKING
        stop_id = fake_id(tws, f"{out.order_ref}.STP")
        assert tws.orders[stop_id].status == "Submitted"  # the stop still protects the share
        assert tws.count("CANCEL_ORDER") == 0 and placed(tws) == 6
    finally:
        coord.close()
        s.disconnect()


def test_a_close_refused_after_the_stop_was_cancelled_reports_protection_absent(
        tws, tmp_path, monkeypatch):
    from tradingbot.ibkr_orders.broker import ProtectionLost
    from tradingbot.ibkr_orders.guard import Refusal
    s, broker, coord, _out = _held_with_stop(tws, tmp_path)
    try:
        real_place = s.place

        def refuse_sells(specs):
            if specs[0].action == "SELL":  # the real send is refused after the dry check passed
                raise OrderRefused(Refusal(RefusalReason.OUTSIDE_LIQUID_HOURS, "invented"))
            return real_place(specs)

        monkeypatch.setattr(s, "place", refuse_sells)
        idea = coord.ledger()[0].idea_id
        close = run(coord, "c1", action("close", Action.CLOSE, idea=idea, symbol="TLT"))
        assert close.state != IntentState.WORKING
        journal = coord.store.journal(close.order_ref)
        assert any("POSITION_OPEN_PROTECTION_ABSENT" in str(e.detail) for e in journal)
        assert broker.protection_lost and "TLT" in broker.protection_lost
        coord.sync()  # the next read raises the core's own exception
        assert any(e.kind == "UNPROTECTED_POSITION" for e in coord.store.exceptions(
            open_only=True))
        assert issubclass(ProtectionLost, BrokerError)
    finally:
        coord.close()
        s.disconnect()


# --- merge with main: the Broker protocol on main ---------------------------------------------
def test_the_binding_satisfies_mains_protocol_and_places_no_conditional_order(tws, tmp_path):
    from tradingbot.execution.broker import (
        Broker,
        ConditionalOrders,
        ConditionDirection,
        PriceCondition,
        ReconnectableBroker,
        supports_price_conditions,
    )
    s = make(tws, tmp_path)
    try:
        b = IbkrPaperBroker(s)
        assert isinstance(b, Broker)
        assert not isinstance(b, ConditionalOrders) and not isinstance(b, ReconnectableBroker)
        assert supports_price_conditions(b) is False
        contract = b.resolve(Instrument(asset_class=AssetClass.ETF, symbol="TLT"))
        cond = PriceCondition(contract=contract, direction=ConditionDirection.BELOW,
                              trigger_price=D("80.00"), released_order_type=OrderType.LMT)
        intent = OrderIntent(order_ref="TB-C", contract=contract, side=OrderSide.BUY,
                             quantity=1, order_type=OrderType.LMT, limit_price=D("80.00"),
                             condition=cond)
        with pytest.raises(BrokerError, match="PRICE_CONDITION_NOT_SUPPORTED"):
            b.submit(intent)
        assert placed(tws) == 0 and s.submissions_used == 0
    finally:
        s.disconnect()


# --- note (a): the exit dry check prices the exit limit actually to be sent --------------------
def test_the_exit_dry_check_prices_the_exit_limit_not_the_stop(tws, tmp_path, monkeypatch):
    from tradingbot.execution.coordinator import snap_to_tick
    s, broker, coord, _out = _held_with_stop(tws, tmp_path)
    try:
        seen = []
        real = s.dry_check_exit

        def spy(order_id, *, limit_price):
            seen.append(limit_price)
            return real(order_id, limit_price=limit_price)

        monkeypatch.setattr(s, "dry_check_exit", spy)
        expected = snap_to_tick(D(str(tws.bid)) * (1 - D(1) / 100), D("0.01"), "ROUND_CEILING")
        assert broker.exit_limit() == expected == D("87.22")  # bid 88.10 less 1 %, up to tick
        idea = coord.ledger()[0].idea_id
        close = run(coord, "c1", action("close", Action.CLOSE, idea=idea, symbol="TLT"))
        assert close.state == IntentState.WORKING, close
        assert seen == [expected]  # the dry run used the limit that was then sent
        sent = tws.placed()[-1]
        assert sent.order.action == "SELL" and D(str(sent.order.lmtPrice)) == expected
        assert D(str(sent.order.lmtPrice)) != D("87.00")  # not the stop price
    finally:
        coord.close()
        s.disconnect()


def test_an_exit_limit_the_guard_refuses_keeps_the_stop_even_if_the_stop_price_passes(
        tws, tmp_path):
    """The stop (87.00) is inside the 150 USD cap, but the close the core would send now (the bid
    160.00 less 1 %) is not: the stop is never cancelled for it."""
    s, broker, coord, out = _held_with_stop(tws, tmp_path)
    try:
        tws.bid, tws.ask, tws.last = 160.00, 160.02, 160.01  # invented
        idea = coord.ledger()[0].idea_id
        close = run(coord, "c1", action("close", Action.CLOSE, idea=idea, symbol="TLT"))
        assert close.state != IntentState.WORKING
        stop_id = fake_id(tws, f"{out.order_ref}.STP")
        assert tws.orders[stop_id].status == "Submitted"
        assert tws.count("CANCEL_ORDER") == 0 and placed(tws) == 2
        journal = " ".join(str(e.detail) for e in coord.store.journal(close.order_ref))
        assert "PROTECTIVE_CANCEL_UNCONFIRMED" in journal  # the core: nothing sent
        assert not broker.protection_lost
        own_stop = next(i for i, r in s.order_ids().items() if r.endswith(".STP"))
        with pytest.raises(OrderRefused) as info:  # what the binding's dry run saw
            s.dry_check_exit(own_stop, limit_price=broker.exit_limit())
        assert info.value.refusal.reason == RefusalReason.NOTIONAL_CAP
        s.dry_check_exit(own_stop, limit_price=D("87.00"))  # the stop price would have passed
    finally:
        coord.close()
        s.disconnect()


def test_without_an_exit_collar_no_held_stop_can_be_cancelled(tws, tmp_path):
    s = make(tws, tmp_path)
    try:
        snaps = s.place([buy(), stop()])
        tws.fill(snaps[0].order_id, 1, 88.11)
        assert settle(lambda: s.positions() and s.positions()[0].quantity == 1)
        b = IbkrPaperBroker(s)  # a read-only use of the binding: no collar
        assert b.exit_limit() is None
        with pytest.raises(BrokerError, match="EXIT_WOULD_BE_REFUSED"):
            b.cancel("TB-0001.STP")
        assert tws.count("CANCEL_ORDER") == 0
    finally:
        s.disconnect()
