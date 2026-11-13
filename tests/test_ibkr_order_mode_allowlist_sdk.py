"""The order session's outgoing allowlist is pinned by value and does NOT follow additions to the
read-only adapter's ``ALLOWED_OUT_MESSAGES`` (historical bars, ids 20 and 25, stay read-only-only).
Real ibapi 1052.1 over the fake order TWS; no socket. Synthetic data only."""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

pytest.importorskip("ibapi")

from ibapi import comm
from ibapi.client import EClient
from ibapi.common import PROTOBUF_MSG_ID
from ibapi.contract import Contract
from ibapi.message import OUT
from ibapi.protobuf.CancelHistoricalData_pb2 import CancelHistoricalData
from ibapi.protobuf.HistoricalDataRequest_pb2 import HistoricalDataRequest
from test_ibkr_paper_orders_sdk import make, tws  # noqa: F401 - the order fixture

from tradingbot.ibkr.errors import ReadOnlyViolation
from tradingbot.ibkr.historical import HistoricalBarsRequest, HistoricalContract, HistoricalPacer
from tradingbot.ibkr.session import ALLOWED_OUT_MESSAGES
from tradingbot.ibkr_orders import session as order_session_module
from tradingbot.ibkr_orders.session import ORDER_MODE_OUT_MESSAGES

# Exactly the order-mode set on origin/main 5835566, by value.
ORDER_MODE_ON_MAIN = frozenset({1, 2, 3, 4, 7, 8, 9, 16, 59, 61, 62, 63, 64, 71, 78, 99, 106, 108})
HISTORICAL = frozenset({20, 25})  # REQ_HISTORICAL_DATA, CANCEL_HISTORICAL_DATA


def test_order_mode_set_is_exactly_what_it_was_on_main():
    assert {int(m) for m in ORDER_MODE_OUT_MESSAGES} == ORDER_MODE_ON_MAIN
    assert ORDER_MODE_OUT_MESSAGES == {
        OUT.START_API, OUT.REQ_CONTRACT_DATA, OUT.CANCEL_CONTRACT_DATA, OUT.REQ_ACCOUNT_SUMMARY,
        OUT.CANCEL_ACCOUNT_SUMMARY, OUT.REQ_POSITIONS, OUT.CANCEL_POSITIONS, OUT.REQ_MKT_DATA,
        OUT.CANCEL_MKT_DATA, OUT.REQ_MARKET_DATA_TYPE, OUT.REQ_CONFIG, OUT.REQ_SEC_DEF_OPT_PARAMS,
        OUT.PLACE_ORDER, OUT.CANCEL_ORDER, OUT.REQ_ALL_OPEN_ORDERS, OUT.REQ_EXECUTIONS,
        OUT.REQ_COMPLETED_ORDERS, OUT.REQ_IDS}
    assert not ORDER_MODE_OUT_MESSAGES & {OUT.REQ_HISTORICAL_DATA, OUT.CANCEL_HISTORICAL_DATA}


def test_a_new_read_only_id_needs_a_conscious_order_mode_decision():
    # Fails when ALLOWED_OUT_MESSAGES gains (or loses) an id: decide then whether order mode
    # takes it (add it to ORDER_MODE_READ_MESSAGES) or not (add it here).
    explicit = order_session_module.ORDER_MODE_READ_MESSAGES
    assert {int(m) for m in ALLOWED_OUT_MESSAGES - explicit} == HISTORICAL
    assert explicit <= ALLOWED_OUT_MESSAGES


def _hist_proto(rid: int) -> HistoricalDataRequest:
    return HistoricalDataRequest(reqId=rid, endDateTime="20260309-14:15:00", duration="1800 S",
                                 barSizeSetting="1 min", whatToShow="TRADES", formatDate=2)


def test_historical_request_and_cancel_refused_on_the_order_session_by_every_send_path(
        tws, tmp_path):  # noqa: F811
    s = make(tws, tmp_path)
    s._hist_pacer = HistoricalPacer(sleep=lambda _s: None)  # not the process-wide pacer
    try:
        wire = list(tws.wire)
        c = Contract()
        c.conId, c.exchange = 100000001, "NYMEX"
        req = HistoricalBarsRequest(
            contract=HistoricalContract(con_id=100000001, exchange="NYMEX"),
            end=datetime(2026, 3, 9, 14, 15, tzinfo=UTC), duration="1800 S", bar_size="1 min",
            what_to_show="TRADES", regular_hours_only=False)
        with pytest.raises(ReadOnlyViolation):  # the inherited adapter method
            s.historical_bars(req)
        with pytest.raises(ReadOnlyViolation):  # bound client methods
            s._client.reqHistoricalData(1, c, "20260309-14:15:00", "1800 S", "1 min", "TRADES",
                                        0, 2, False, [])
        with pytest.raises(ReadOnlyViolation):
            s._client.cancelHistoricalData(1)
        with pytest.raises(ReadOnlyViolation):  # unbound base-class protobuf methods
            EClient.reqHistoricalDataProtoBuf(s._client, _hist_proto(2))
        with pytest.raises(ReadOnlyViolation):
            EClient.cancelHistoricalDataProtoBuf(s._client, CancelHistoricalData(reqId=2))
        for mid, payload in ((OUT.REQ_HISTORICAL_DATA, _hist_proto(3).SerializeToString()),
                             (OUT.CANCEL_HISTORICAL_DATA,
                              CancelHistoricalData(reqId=3).SerializeToString())):
            with pytest.raises(ReadOnlyViolation):  # unbound send primitives
                EClient.sendMsgProtoBuf(s._client, mid + PROTOBUF_MSG_ID, payload)
            with pytest.raises(ReadOnlyViolation):
                EClient.sendMsg(s._client, mid, "1\0")
            with pytest.raises(ReadOnlyViolation):  # a framed message straight at the guard
                s._client.conn.sendMsg(comm.make_msg_proto(mid + PROTOBUF_MSG_ID, payload))
        assert tws.wire == wire
        assert not {"REQ_HISTORICAL_DATA", "CANCEL_HISTORICAL_DATA"} & {
            n for n, _ in tws.requests}
    finally:
        s.disconnect()


# --- review S3: the client-method set is pinned by value too; historical_bars refused first -----
MAIN_READ_CLIENT_METHODS = frozenset({  # ALLOWED_CLIENT_METHODS on origin/main 2ff2961
    "connect", "disconnect", "isConnected", "run", "reset", "setConnState", "checkConnected",
    "validateInvalidSymbols", "useProtoBuf", "serverVersion", "twsConnectionTime", "logRequest",
    "msgLoopTmo", "msgLoopRec", "keyboardInterrupt", "keyboardInterruptHard", "sendMsg",
    "sendMsgProtoBuf", "startApi", "startApiProtoBuf", "reqContractDetails",
    "reqContractDataProtoBuf", "cancelContractData", "cancelContractDataProtoBuf",
    "reqAccountSummary", "reqAccountSummaryProtoBuf", "cancelAccountSummary",
    "cancelAccountSummaryProtoBuf", "reqPositions", "reqPositionsProtoBuf", "cancelPositions",
    "cancelPositionsProtoBuf", "reqMktData", "reqMarketDataProtoBuf", "cancelMktData",
    "cancelMarketDataProtoBuf", "reqMarketDataType", "reqMarketDataTypeProtoBuf",
    "reqConfigProtoBuf", "reqSecDefOptParams", "reqSecDefOptParamsProtoBuf"})
ORDER_METHODS = frozenset({"placeOrderProtoBuf", "validateOrderParameters",
                           "validateAttachedOrdersParameters", "cancelOrderProtoBuf",
                           "reqAllOpenOrdersProtoBuf", "reqExecutionsProtoBuf",
                           "reqCompletedOrdersProtoBuf", "reqIdsProtoBuf"})
HIST_METHODS = frozenset({"reqHistoricalData", "reqHistoricalDataProtoBuf",
                          "cancelHistoricalData", "cancelHistoricalDataProtoBuf"})


def test_order_mode_client_methods_are_exactly_what_they_were_on_main():
    from tradingbot.ibkr.session import ALLOWED_CLIENT_METHODS

    assert order_session_module.ORDER_MODE_CLIENT_METHODS == MAIN_READ_CLIENT_METHODS | ORDER_METHODS
    assert not order_session_module.ORDER_MODE_CLIENT_METHODS & HIST_METHODS
    # a new read-only method needs a conscious order-mode decision, as for the ids
    assert ALLOWED_CLIENT_METHODS - order_session_module.ORDER_MODE_READ_CLIENT_METHODS == HIST_METHODS


def test_historical_bars_on_the_order_session_is_refused_before_the_pacer(tws, tmp_path):  # noqa: F811
    from tradingbot.ibkr.historical import DEFAULT_HISTORICAL_PACER
    from tradingbot.ibkr_orders.records import OrderModeViolation

    s = make(tws, tmp_path)
    try:
        s._hist_pacer = DEFAULT_HISTORICAL_PACER
        before_budget, wire = DEFAULT_HISTORICAL_PACER.sent_in_window(), list(tws.wire)
        req = HistoricalBarsRequest(
            contract=HistoricalContract(con_id=100000001, exchange="NYMEX"),
            end=datetime(2026, 3, 9, 14, 15, tzinfo=UTC), duration="3600 S", bar_size="1 min",
            what_to_show="MIDPOINT", regular_hours_only=False)
        with pytest.raises(OrderModeViolation, match="not available on the order session"):
            s.historical_bars(req)
        assert DEFAULT_HISTORICAL_PACER.sent_in_window() == before_budget
        assert s._pending == {} and tws.wire == wire
        for name in HIST_METHODS:  # the client surface denies them by name now
            with pytest.raises(ReadOnlyViolation):
                getattr(s._client, name)(1, 2, 3)
    finally:
        s.disconnect()
