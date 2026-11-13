"""Historical bars through the REAL ibapi 1052.1 client / reader / decoder over the fake transport
(``tests/_ibkr_fake_tws.py``). No socket is opened. Synthetic data only."""
from __future__ import annotations

import inspect
import json
import os
import socket
import stat
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

pytest.importorskip("ibapi")

import ibapi.client
from _ibkr_fake_tws import (
    PAPER,
    FakeTws,
    error_frame,
    hist_bar,
    hist_data_frame,
    hist_end_frame,
)
from ibapi import comm
from ibapi.client import EClient
from ibapi.common import PROTOBUF_MSG_ID
from ibapi.contract import Contract
from ibapi.message import OUT
from ibapi.protobuf.HistoricalDataRequest_pb2 import HistoricalDataRequest

from tradingbot.ibkr.config import validate_config
from tradingbot.ibkr.errors import ReadOnlyViolation, SessionStateError
from tradingbot.ibkr.historical import (
    HistoricalBarsRequest,
    HistoricalContract,
    HistoricalPacer,
    HistoricalRequestInvalid,
)
from tradingbot.ibkr.historical_batch import parse_plan, run_batch, write_private_report
from tradingbot.ibkr.session import (
    ALLOWED_CLIENT_METHODS,
    ALLOWED_OUT_MESSAGES,
    IbkrSession,
    SessionState,
    _Wrapper,
)

CFG = validate_config({"mode": "paper", "host": "127.0.0.1", "port": 7497, "client_id": 7,
                       "account": PAPER, "tws_profile_ini": "/Users/someone/Jts/jts.ini",
                       "tws_os_user": "someone", "read_only_check": "config-read"})  # synthetic
END = datetime(2026, 3, 9, 14, 15, tzinfo=UTC)
E0 = int(END.timestamp())
FUT = HistoricalContract(con_id=100000001, exchange="NYMEX", include_expired=True)  # invented
NY = ZoneInfo("America/New_York")


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.t += s


def pacer() -> HistoricalPacer:
    clock = FakeClock()
    return HistoricalPacer(clock=clock, sleep=clock.sleep)


@pytest.fixture(params=[219, 226], ids=lambda v: f"sv{v}")
def tws(monkeypatch, request):
    def no_network(*args, **kwargs):
        raise AssertionError("network access attempted in an offline test")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket.socket, "connect_ex", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    server = FakeTws()
    server.server_version = request.param
    monkeypatch.setattr(ibapi.client, "Connection", server.connection_factory)
    return server


def session(**kw) -> IbkrSession:
    opts = {"connect_timeout": 2.0, "request_timeout": 2.0, "shutdown_timeout": 3.0,
            "preflight": lambda cfg: {"stub": True}, "historical_pacer": pacer()}
    opts.update(kw)
    return IbkrSession(CFG, **opts)


def req(end: datetime = END, **kw) -> HistoricalBarsRequest:
    opts = {"contract": FUT, "end": end, "duration": "1800 S", "bar_size": "1 min",
            "what_to_show": "TRADES", "regular_hours_only": False}
    opts.update(kw)
    return HistoricalBarsRequest(**opts)


def minute_bars(n: int, last_start: int = E0 - 60) -> list:
    first = last_start - 60 * (n - 1)
    return [hist_bar(str(first + 60 * i), 70.0 + i, 70.5 + i, 69.5 + i, 70.2 + i, volume="12",
                     wap="70.1", bar_count=5) for i in range(n)]


def answer(*frames_for):
    """Handler: push the frames built by each callable(reqId) in order."""
    def handler(srv, c, r):
        for build in frames_for:
            c.push(build(r.reqId))
    return handler


def wire_requests(tws: FakeTws, name: str) -> list:
    return [p for n, p in tws.requests if n == name]


# --- normal answers ------------------------------------------------------------------------------
def test_normal_answer_bars_parsed_request_on_the_wire(tws):
    tws.on["REQ_HISTORICAL_DATA"] = answer(lambda rid: hist_data_frame(rid, minute_bars(3)),
                                           hist_end_frame)
    with session() as s:
        r = s.historical_bars(req())
    assert (r.status, r.reason, r.complete, r.count) == ("COMPLETE", "OK", True, 3)
    assert [b.start_utc for b in r.bars] == [END - timedelta(minutes=k) for k in (3, 2, 1)]
    b = r.bars[0]
    assert b.start_utc.tzinfo is UTC and b.session_date is None
    assert (b.open, b.high, b.low, b.close) == (70.0, 70.5, 69.5, 70.2)
    assert (b.volume, b.wap, b.bar_count) == (Decimal(12), Decimal("70.1"), 5)
    assert (r.end_marker_start, r.end_marker_end) == ("20260309 14:00:00", "20260309 15:00:00")
    assert r.sent and not r.cancel_sent and r.error_code is None
    (sent,) = wire_requests(tws, "REQ_HISTORICAL_DATA")
    assert (sent.contract.conId, sent.contract.exchange, sent.contract.includeExpired) == (
        100000001, "NYMEX", True)
    assert sent.endDateTime == "20260309-14:15:00"
    assert (sent.duration, sent.barSizeSetting, sent.whatToShow) == ("1800 S", "1 min", "TRADES")
    assert sent.formatDate == 2
    assert not sent.keepUpToDate and not sent.HasField("keepUpToDate")
    assert not sent.useRTH
    assert "CANCEL_HISTORICAL_DATA" not in tws.names()
    assert s.state is SessionState.DISCONNECTED and s.threads_alive() == []


def test_fields_contract_and_regular_hours_reach_the_wire(tws):
    tws.on["REQ_HISTORICAL_DATA"] = answer(lambda rid: hist_data_frame(rid, minute_bars(1)),
                                           hist_end_frame)
    k = HistoricalContract(symbol="XYZ", sec_type="FUT", exchange="NYMEX", currency="USD",
                           last_trade_date_or_contract_month="20261218", multiplier="1000",
                           trading_class="XYZ", include_expired=True)
    with session() as s:
        r = s.historical_bars(req(contract=k, regular_hours_only=True, what_to_show="MIDPOINT"))
    assert r.reason == "OK"
    (sent,) = wire_requests(tws, "REQ_HISTORICAL_DATA")
    c = sent.contract
    assert (c.symbol, c.secType, c.exchange, c.currency, c.lastTradeDateOrContractMonth,
            c.multiplier, c.tradingClass, c.includeExpired) == (
        "XYZ", "FUT", "NYMEX", "USD", "20261218", 1000.0, "XYZ", True)
    assert c.conId == 0 and sent.useRTH and sent.whatToShow == "MIDPOINT"  # SDK sends conId 0


def test_absent_volume_is_none_and_minus_one_volume_is_none(tws):
    bars = [hist_bar(str(E0 - 120), 1.0, 1.0, 1.0, 1.0, volume=None),
            hist_bar(str(E0 - 60), 1.0, 1.0, 1.0, 1.0, volume="-1", bar_count=-1)]
    tws.on["REQ_HISTORICAL_DATA"] = answer(lambda rid: hist_data_frame(rid, bars),
                                           hist_end_frame)
    with session() as s:
        r = s.historical_bars(req(what_to_show="MIDPOINT"))
    assert [b.volume for b in r.bars] == [None, None] and r.bars[1].bar_count is None


def test_empty_answer_is_complete_with_no_bars(tws):
    tws.on["REQ_HISTORICAL_DATA"] = answer(lambda rid: hist_data_frame(rid, []), hist_end_frame)
    with session() as s:
        r = s.historical_bars(req())
    assert (r.status, r.reason, r.complete, r.count, r.bars) == ("COMPLETE", "NO_BARS", True, 0, ())
    assert "CANCEL_HISTORICAL_DATA" not in tws.names()


def test_daily_bars_carry_the_session_date(tws):
    bars = [hist_bar("20260302", 10, 11, 9, 10.5), hist_bar("20260303", 10.5, 12, 10, 11)]
    tws.on["REQ_HISTORICAL_DATA"] = answer(lambda rid: hist_data_frame(rid, bars),
                                           hist_end_frame)
    with session() as s:
        r = s.historical_bars(req(bar_size="1 day", duration="1 M"))
    assert [(b.session_date, b.start_utc) for b in r.bars] == [("2026-03-02", None),
                                                               ("2026-03-03", None)]


# --- broker errors: reported as such, never worked around ----------------------------------------
@pytest.mark.parametrize(("code", "text", "reason"), [
    (162, ("Historical Market Data Service error message:HMDS query returned no data: XYZ@NYMEX "
          "Trades"), "NO_DATA"),
    (162, "Historical Market Data Service error message:No market data permissions for NYMEX FUT",
     "NO_PERMISSION"),
    (354, "Requested market data is not subscribed.", "NO_PERMISSION"),
    (166, "HMDS Expired Contract Violation", "EXPIRED_CONTRACT_UNAVAILABLE"),
    (200, "No security definition has been found for the request", "CONTRACT_NOT_FOUND"),
    (321, "Error validating request.-'bP' : cause - Invalid duration", "REQUEST_INVALID"),
    (162, "Historical Market Data Service error message:something else", "HMDS_ERROR"),
])
def test_broker_errors_are_failed_reads_with_their_reason(tws, code, text, reason):
    tws.on["REQ_HISTORICAL_DATA"] = answer(
        lambda rid: hist_data_frame(rid, minute_bars(2)),  # bars before the error: not kept
        lambda rid: error_frame(rid, code, text))
    with session() as s:
        r = s.historical_bars(req())
        follow = s.historical_bars(req(duration="3600 S"))  # the session still reads
    assert (r.status, r.reason, r.complete, r.bars, r.count) == ("FAILED", reason, False, (), 0)
    assert (r.error_code, r.error_message) == (code, text) and r.bars_discarded == 2
    assert follow.reason == reason  # the same honest answer again, from a live session
    assert tws.names().count("REQ_HISTORICAL_DATA") == 2  # no retry, no workaround request


def test_broker_error_text_is_redacted(tws):
    tws.on["REQ_HISTORICAL_DATA"] = answer(
        lambda rid: error_frame(rid, 162, f"No market data permissions for {PAPER}"))
    with session() as s:
        r = s.historical_bars(req())
    assert r.reason == "NO_PERMISSION" and PAPER not in r.error_message
    assert "DU…01" in r.error_message


def test_pacing_violation_has_its_own_reason_and_latches_the_pacer(tws):
    tws.on["REQ_HISTORICAL_DATA"] = answer(lambda rid: error_frame(
        rid, 162, "Historical Market Data Service error message:API historical data query "
                  "cancelled: 1 Pacing violation"))
    p = pacer()
    with session(historical_pacer=p) as s:
        r = s.historical_bars(req())
        again = s.historical_bars(req(end=END - timedelta(hours=1)))
    assert r.reason == "PACING_VIOLATION" and r.stops_batch and r.error_code == 162
    assert p.latched is not None
    assert (again.reason, again.sent) == ("LOCAL_PACING_LATCHED", False)
    assert tws.names().count("REQ_HISTORICAL_DATA") == 1


# --- fail closed: never-ending, late, unrouted and malformed answers -----------------------------
def test_answer_that_never_ends_times_out_and_is_cancelled(tws):
    tws.on["REQ_HISTORICAL_DATA"] = answer(lambda rid: hist_data_frame(rid, minute_bars(2)))
    with session() as s:
        r = s.historical_bars(req(), timeout=0.4)
        rid = r.req_id
        conn = tws.connections[0]
        conn.push(hist_data_frame(rid, minute_bars(1)))  # late: the request is closed
        conn.push(hist_end_frame(rid))
        deadline = time.monotonic() + 2.0
        while s.historical_unrouted_ends < 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        late_bars, late_ends = s.historical_unrouted_bars, s.historical_unrouted_ends
    assert (r.status, r.reason, r.complete, r.bars) == ("FAILED", "TIMEOUT", False, ())
    assert r.cancel_sent and r.bars_discarded == 2
    (cancel,) = wire_requests(tws, "CANCEL_HISTORICAL_DATA")
    assert cancel.reqId == rid
    assert (late_bars, late_ends) == (1, 1)
    assert s.cleanup_failures == []


def test_bars_for_a_request_id_that_is_not_open_are_ignored_and_counted(tws):
    tws.on["REQ_HISTORICAL_DATA"] = answer(
        lambda rid: hist_data_frame(rid + 777, minute_bars(4)),
        lambda rid: hist_data_frame(None, minute_bars(1)),  # no reqId at all
        lambda rid: hist_end_frame(rid + 777),
        lambda rid: hist_data_frame(rid, minute_bars(2)),
        hist_end_frame)
    with session() as s:
        r = s.historical_bars(req())
    assert r.reason == "OK" and r.count == 2
    assert (s.historical_unrouted_bars, s.historical_unrouted_ends) == (5, 1)


@pytest.mark.parametrize(("bars", "why"), [
    ([hist_bar(str(E0 - 60), 1, 2, 0.5, 1)], None),  # control: valid
    ([hist_bar("20260309 10:14:00 US/Eastern", 1, 2, 0.5, 1)], "epoch"),
    ([hist_bar(str(E0 - 60), 1, 0.5, 2, 1)], "high / low"),
    ([hist_bar(str(E0 - 60), 1, 2, 0.5, 1), hist_bar(str(E0 - 60), 1, 2, 0.5, 1)], "increasing"),
    ([hist_bar(str(E0), 1, 2, 0.5, 1)], "at or after"),
    ([hist_bar(str(E0 - 60), float("nan"), 2, 0.5, 1)], "finite"),
])
def test_malformed_bars_fail_the_read_and_cancel(tws, bars, why):
    tws.on["REQ_HISTORICAL_DATA"] = answer(lambda rid: hist_data_frame(rid, bars),
                                           hist_end_frame)
    with session() as s:
        r = s.historical_bars(req())
    if why is None:
        assert r.reason == "OK"
        return
    assert (r.status, r.reason, r.bars, r.complete) == ("FAILED", "MALFORMED", (), False)
    assert why in r.detail and r.cancel_sent


def test_bar_missing_a_price_field_is_malformed_not_zero(tws):
    from ibapi.protobuf.HistoricalDataBar_pb2 import HistoricalDataBar

    no_close = HistoricalDataBar(date=str(E0 - 60), open=1.0, high=2.0, low=0.5)
    tws.on["REQ_HISTORICAL_DATA"] = answer(lambda rid: hist_data_frame(rid, [no_close]),
                                           hist_end_frame)
    with session() as s:
        r = s.historical_bars(req())
    assert r.reason == "MALFORMED" and "close" in r.detail and r.bars == ()


def test_epoch_time_on_a_daily_bar_is_malformed(tws):
    tws.on["REQ_HISTORICAL_DATA"] = answer(
        lambda rid: hist_data_frame(rid, [hist_bar(str(E0 - 86400), 1, 2, 0.5, 1)]),
        hist_end_frame)
    with session() as s:
        r = s.historical_bars(req(bar_size="1 day", duration="1 M"))
    assert r.reason == "MALFORMED"


# --- time zones and daylight saving --------------------------------------------------------------
def test_new_york_end_around_spring_forward_is_sent_as_utc(tws):
    tws.on["REQ_HISTORICAL_DATA"] = answer(lambda rid: hist_data_frame(rid, []), hist_end_frame)
    before = datetime(2026, 3, 7, 9, 30, tzinfo=NY)  # EST, UTC-5
    after = datetime(2026, 3, 9, 9, 30, tzinfo=NY)  # EDT, UTC-4
    with session() as s:
        s.historical_bars(req(end=before))
        s.historical_bars(req(end=after))
    sent = [p.endDateTime for p in wire_requests(tws, "REQ_HISTORICAL_DATA")]
    assert sent == ["20260307-14:30:00", "20260309-13:30:00"]


def test_ambiguous_and_skipped_new_york_times_are_refused_before_any_socket(tws):
    for wall in (datetime(2026, 11, 1, 1, 30, tzinfo=NY), datetime(2026, 3, 8, 2, 30, tzinfo=NY)):
        with pytest.raises(HistoricalRequestInvalid, match="ambiguous or does not exist"):
            req(end=wall)
    assert tws.wire == [] and tws.connections == []


def test_bars_across_the_fall_back_hour_stay_strictly_increasing_in_utc(tws):
    # 2026-11-01: 01:00-02:00 New York time happens twice; epoch seconds keep the bars distinct.
    first = int(datetime(2026, 11, 1, 5, 58, tzinfo=UTC).timestamp())  # 01:58 EDT
    bars = [hist_bar(str(first + 60 * i), 1, 2, 0.5, 1.5) for i in range(5)]
    end = datetime(2026, 11, 1, 1, 10, tzinfo=NY).replace(fold=1)  # refused even with fold
    with pytest.raises(HistoricalRequestInvalid):
        req(end=end)
    tws.on["REQ_HISTORICAL_DATA"] = answer(lambda rid: hist_data_frame(rid, bars), hist_end_frame)
    with session() as s:
        r = s.historical_bars(req(end=datetime(2026, 11, 1, 6, 3, tzinfo=UTC)))
    starts = [b.start_utc for b in r.bars]
    assert r.reason == "OK" and starts == sorted(set(starts)) and len(starts) == 5
    walls = [t.astimezone(NY).strftime("%H:%M") for t in starts]
    assert walls == ["01:58", "01:59", "01:00", "01:01", "01:02"]  # repeated wall clock


def test_naive_end_is_refused_before_any_socket_use(tws):
    with pytest.raises(HistoricalRequestInvalid, match="naive"):
        req(end=datetime(2026, 3, 9, 10, 15))
    s = session()
    with pytest.raises(HistoricalRequestInvalid):
        s.historical_bars("not a request")  # type: ignore[arg-type]
    with pytest.raises(SessionStateError):  # not connected: nothing sent
        s.historical_bars(req())
    assert tws.wire == [] and tws.connections == []


# --- keepUpToDate can never be set ---------------------------------------------------------------
def _hist_proto(rid: int, keep: bool) -> HistoricalDataRequest:
    p = HistoricalDataRequest(reqId=rid, endDateTime="20260309-14:15:00", duration="1800 S",
                              barSizeSetting="1 min", whatToShow="TRADES", formatDate=2)
    if keep:
        p.keepUpToDate = True
    return p


def test_keep_up_to_date_is_impossible_to_set(tws):
    assert "keep" not in " ".join(inspect.signature(IbkrSession.historical_bars).parameters)
    assert not [f for f in HistoricalBarsRequest.__dataclass_fields__ if "keep" in f.lower()]
    with pytest.raises(TypeError):
        req(keep_up_to_date=True)
    with session() as s:
        with pytest.raises(TypeError):
            s.historical_bars(req(), keep_up_to_date=True)  # type: ignore[call-arg]
        before = list(tws.wire)
        c = Contract()
        c.conId, c.exchange = 100000001, "NYMEX"
        with pytest.raises(ReadOnlyViolation, match="keepUpToDate"):
            s._client.reqHistoricalData(1, c, "", "1800 S", "1 min", "TRADES", 0, 2, True, [])
        with pytest.raises(ReadOnlyViolation, match="keepUpToDate"):
            EClient.reqHistoricalDataProtoBuf(s._client, _hist_proto(2, True))
        frame = comm.make_msg_proto(OUT.REQ_HISTORICAL_DATA + PROTOBUF_MSG_ID,
                                    _hist_proto(3, True).SerializeToString())
        with pytest.raises(ReadOnlyViolation, match="keepUpToDate"):
            s._client.conn.sendMsg(frame)
        with pytest.raises(ReadOnlyViolation, match="text-framed"):
            EClient.sendMsg(s._client, OUT.REQ_HISTORICAL_DATA, "1\0")
        with pytest.raises(ReadOnlyViolation, match="unparseable"):
            s._client.conn.sendMsg(comm.make_msg_proto(OUT.REQ_HISTORICAL_DATA + PROTOBUF_MSG_ID,
                                                       b"\xff\xff\xff"))
        assert tws.wire == before
        assert s.state is SessionState.READY
    assert not any(n == "REQ_HISTORICAL_DATA" for n in tws.names())


# --- the allowlist proof, extended ---------------------------------------------------------------
BEFORE = {OUT.START_API, OUT.REQ_CONTRACT_DATA, OUT.CANCEL_CONTRACT_DATA, OUT.REQ_ACCOUNT_SUMMARY,
          OUT.CANCEL_ACCOUNT_SUMMARY, OUT.REQ_POSITIONS, OUT.CANCEL_POSITIONS, OUT.REQ_MKT_DATA,
          OUT.CANCEL_MKT_DATA, OUT.REQ_MARKET_DATA_TYPE, OUT.REQ_CONFIG,
          OUT.REQ_SEC_DEF_OPT_PARAMS}


def test_allowlists_add_exactly_the_historical_request_and_its_cancel():
    assert ALLOWED_OUT_MESSAGES - BEFORE == {OUT.REQ_HISTORICAL_DATA, OUT.CANCEL_HISTORICAL_DATA}
    assert BEFORE <= ALLOWED_OUT_MESSAGES
    assert (OUT.REQ_HISTORICAL_DATA, OUT.CANCEL_HISTORICAL_DATA) == (20, 25)
    never = {OUT.PLACE_ORDER, OUT.CANCEL_ORDER, OUT.REQ_GLOBAL_CANCEL, OUT.EXERCISE_OPTIONS,
             OUT.UPDATE_CONFIG, OUT.REQ_IDS, OUT.REQ_REAL_TIME_BARS, OUT.REQ_HISTORICAL_TICKS,
             OUT.REQ_HEAD_TIMESTAMP, OUT.REQ_TICK_BY_TICK_DATA, OUT.REQ_HISTOGRAM_DATA}
    assert not never & ALLOWED_OUT_MESSAGES
    hist_methods = {n for n in ALLOWED_CLIENT_METHODS if "istorical" in n}
    assert hist_methods == {"reqHistoricalData", "reqHistoricalDataProtoBuf",
                            "cancelHistoricalData", "cancelHistoricalDataProtoBuf"}


def test_incoming_side_handles_exactly_the_historical_answer_and_end():
    handled = {n for n, v in vars(_Wrapper).items() if callable(v) and not n.startswith("_")}
    assert {n for n in handled if "istorical" in n} == {
        "historicalData", "historicalDataEnd", "historicalDataProtoBuf"}
    assert "historicalDataUpdate" not in handled  # keepUpToDate updates are never consumed
    assert "historicalDataUpdateProtoBuf" not in handled


def test_order_and_streaming_frames_still_impossible_with_historical_open(tws):
    tws.on["REQ_HISTORICAL_DATA"] = answer(lambda rid: hist_data_frame(rid, minute_bars(1)),
                                           hist_end_frame)
    out_ids = {v for k, v in vars(OUT).items() if not k.startswith("_") and isinstance(v, int)}
    with session() as s:
        assert s.historical_bars(req()).reason == "OK"
        before = list(tws.wire)
        for mid in sorted(out_ids - ALLOWED_OUT_MESSAGES):
            with pytest.raises(ReadOnlyViolation):
                EClient.sendMsgProtoBuf(s._client, mid + PROTOBUF_MSG_ID, b"")
            with pytest.raises(ReadOnlyViolation):
                s._client.conn.sendMsg(comm.make_msg_proto(mid + PROTOBUF_MSG_ID, b""))
        for name in ("placeOrder", "cancelOrder", "reqGlobalCancel", "updateConfigProtoBuf",
                     "reqRealTimeBars", "reqHistoricalTicks", "reqHeadTimeStamp"):
            with pytest.raises(ReadOnlyViolation):
                getattr(s._client, name)(1, 2, 3)
        assert tws.wire == before


# --- one request at a time, gates first ---------------------------------------------------------
def test_one_historical_request_at_a_time(tws):
    with session() as s:
        s._hist_busy = True
        with pytest.raises(SessionStateError, match="already outstanding"):
            s.historical_bars(req())
        s._hist_busy = False
    assert "REQ_HISTORICAL_DATA" not in tws.names()


def test_poisoned_session_sends_no_historical_request(tws):
    from tradingbot.ibkr import errors as E

    with session() as s:
        s._poisoned = E.AccountIdentityError("foreign row", reason="FOREIGN_ROW")
        with pytest.raises(E.AccountIdentityError):
            s.historical_bars(req())
        s._poisoned = None
    assert "REQ_HISTORICAL_DATA" not in tws.names()


def test_local_pacing_spaces_requests_and_refuses_identical_ones(tws):
    tws.on["REQ_HISTORICAL_DATA"] = answer(lambda rid: hist_data_frame(rid, []), hist_end_frame)
    clock = FakeClock()
    p = HistoricalPacer(clock=clock, sleep=clock.sleep)
    with session(historical_pacer=p) as s:
        assert s.historical_bars(req()).reason == "NO_BARS"
        same = s.historical_bars(req())
        assert s.historical_bars(req(end=END - timedelta(hours=1))).reason == "NO_BARS"
    assert (same.reason, same.sent) == ("LOCAL_IDENTICAL_REQUEST", False)
    assert clock.sleeps == [3.0]
    assert tws.names().count("REQ_HISTORICAL_DATA") == 2


# --- the batch, end to end over the fake TWS -----------------------------------------------------
PLAN = {"schema": "tradingbot.ibkr.historical_bars_requests/1", "request_timeout": 2.0,
        "request": [
            {"name": f"r{i}", "end": END - timedelta(hours=i), "duration": "1800 S",
             "bar_size": "1 min", "what_to_show": "TRADES", "regular_hours_only": False,
             "contract": {"con_id": 100000001, "exchange": "NYMEX", "include_expired": True}}
            for i in range(4)]}


def test_batch_stops_on_a_broker_pacing_violation_and_writes_one_private_report(tws, tmp_path):
    calls = []

    def handler(srv, c, r):
        calls.append(r.reqId)
        if len(calls) == 1:
            c.push(hist_data_frame(r.reqId, minute_bars(2)))
            c.push(hist_end_frame(r.reqId))
        elif len(calls) == 2:
            c.push(error_frame(r.reqId, 162, "HMDS query returned no data: x"))
        else:
            c.push(error_frame(r.reqId, 162, "Historical Market Data Service error message:"
                                             "Pacing violation"))

    tws.on["REQ_HISTORICAL_DATA"] = handler
    rep = run_batch(parse_plan(PLAN), loader=lambda _p: CFG,
                    session_factory=lambda cfg: session(historical_pacer=pacer()))
    assert [r["reason"] for r in rep["results"]] == ["OK", "NO_DATA", "PACING_VIOLATION",
                                                     "SKIPPED"]
    assert rep["overall"] == "FAILED" and rep["bars_total"] == 2
    assert tws.names().count("REQ_HISTORICAL_DATA") == 3
    assert rep["results"][0]["bars"][0]["start_utc"] == "2026-03-09T14:13:00+00:00"
    out = write_private_report(rep, tmp_path / "bars.json")
    assert stat.S_IMODE(os.stat(out).st_mode) == 0o600
    text = out.read_text()
    assert PAPER not in text and json.loads(text)["account"] == CFG.masked_account()


def test_batch_complete_when_every_request_ran(tws):
    tws.on["REQ_HISTORICAL_DATA"] = answer(lambda rid: error_frame(rid, 354, "not subscribed"))
    rep = run_batch(parse_plan(PLAN), loader=lambda _p: CFG,
                    session_factory=lambda cfg: session(historical_pacer=pacer()))
    assert rep["overall"] == "COMPLETE" and rep["reason_counts"] == {"NO_PERMISSION": 4}
    assert rep["session"]["read_only_status"] == "VERIFIED"
