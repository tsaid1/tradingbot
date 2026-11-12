"""Held-open quote streams and per-side evidence, through the REAL ibapi 1052.1 client/reader/
decoder over the in-repo fake TWS (``tests/_ibkr_fake_tws.py``). No socket is opened."""
from __future__ import annotations

import threading
import time

import pytest

pytest.importorskip("ibapi")

from _ibkr_fake_tws import OTHER_PAPER, FakeTws, error_frame, frame
from ibapi.message import IN, OUT
from ibapi.protobuf.ManagedAccounts_pb2 import ManagedAccounts
from ibapi.protobuf.MarketDataType_pb2 import MarketDataType
from ibapi.protobuf.TickGeneric_pb2 import TickGeneric
from ibapi.protobuf.TickPrice_pb2 import TickPrice
from ibapi.protobuf.TickSize_pb2 import TickSize
from test_ibkr_session_sdk import (  # the fixture and helpers of the main SDK suite
    _assert_no_raw_transport,
    _fire_send_like,
    _walk,
    assert_closed,
    session,
    tws,  # noqa: F401 - pytest fixture
)

from tradingbot.ibkr import errors as E
from tradingbot.ibkr import session as session_mod
from tradingbot.ibkr.models import (
    TLT_QUERY,
    QuoteQuality,
    ResolvedContract,
    entry_quote_check,
)
from tradingbot.ibkr.session import (
    ALLOWED_CLIENT_METHODS,
    ALLOWED_OUT_MESSAGES,
    MAX_QUOTE_STREAMS,
)

L, D, F, U = QuoteQuality.LIVE, QuoteQuality.DELAYED, QuoteQuality.FROZEN, QuoteQuality.UNAVAILABLE


# --- scripted market data ------------------------------------------------------------------------
class Feed:
    """Captures each REQ_MKT_DATA's connection and reqId so a test can push ticks later, then waits
    until the session has processed them (a sentinel tickGeneric per push, in order)."""

    def __init__(self, server: FakeTws) -> None:
        self.subs: list[tuple[object, int]] = []
        self.marks: dict[int, int] = {}
        server.on["REQ_MKT_DATA"] = lambda srv, c, r: self.subs.append((c, r.reqId))

    def push(self, s, idx: int, *frames: bytes) -> None:
        conn, rid = self.subs[idx]
        self.marks[rid] = self.marks.get(rid, 0) + 1
        for f in frames:
            conn.push(f)
        conn.push(frame(IN.TICK_GENERIC, TickGeneric(reqId=rid, tickType=49, value=1.0)))
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            with s._cond:
                p = s._pending.get(rid)
                if p is None or p.tick_counts.get("HALTED", 0) >= self.marks[rid]:
                    return
            time.sleep(0.005)
        raise AssertionError("fake TWS frames were not processed in time")

    def rid(self, idx: int) -> int:
        return self.subs[idx][1]


def _price(rid, tick, price, size="100"):
    return frame(IN.TICK_PRICE, TickPrice(reqId=rid, tickType=tick, price=price, size=size,
                                          attrMask=0))


def _size(rid, tick, size):
    return frame(IN.TICK_SIZE, TickSize(reqId=rid, tickType=tick, size=size))


def _mdt(rid, value):
    return frame(IN.MARKET_DATA_TYPE, MarketDataType(reqId=rid, marketDataType=value))


def _image(rid, bid=2000.0, ask=2000.5, live=True):
    b, a = (1, 2) if live else (66, 67)
    return [_price(rid, b, bid), _price(rid, a, ask)]


GOLD = ResolvedContract(con_id=69067924, symbol="XAUUSD", sec_type="CMDTY", currency="USD",
                        exchange="SMART", primary_exchange="", local_symbol="XAUUSD",
                        min_tick=0.01, time_zone_id="", trading_hours="", liquid_hours="")
CFD = ResolvedContract(con_id=4242, symbol="IBUS500", sec_type="CFD", currency="USD",
                       exchange="SMART", primary_exchange="", local_symbol="IBUS500",
                       min_tick=0.1, time_zone_id="", trading_hours="", liquid_hours="")


@pytest.fixture
def feed(tws):  # noqa: F811
    return Feed(tws)


# --- classification over a live stream ----------------------------------------------------------
def test_gold_case_unreported_type_with_updates_is_live_and_its_mirror_is_not(tws, feed):  # noqa: F811
    with session() as s:
        st = s.open_quote_stream(GOLD)
        rid = feed.rid(0)
        feed.push(s, 0, *_image(rid))
        q = st.latest()
        assert q.market_data_type is None and q.market_data_type_name == "UNREPORTED"
        assert (q.quality, q.quality_reason) == (F, "TYPE_UNREPORTED_NO_UPDATES_YET")
        assert (q.bid_updates, q.ask_updates) == (0, 0) and q.bid_at and q.ask_at
        assert q.bid_tick_class == q.ask_tick_class == "LIVE"
        # a frozen feed repeating its image is still "no updates"
        feed.push(s, 0, *_image(rid))
        assert st.latest().quality is F and st.latest().bid_updates == 0
        # one side moves: still not live
        feed.push(s, 0, _price(rid, 1, 2000.1))
        q = st.latest()
        assert (q.bid_updates, q.ask_updates, q.quality) == (1, 0, F)
        # both sides moved after the image: live
        for i in range(30):
            feed.push(s, 0, _price(rid, 1, 2000.2 + i / 100), _price(rid, 2, 2000.7 + i / 100))
        q = st.latest()
        assert (q.quality, q.quality_reason) == (L, "TYPE_UNREPORTED_UPDATES_SEEN")
        assert q.bid_updates == 31 and q.ask_updates == 30 and q.route == "held_stream"
        assert q.bid_at_monotonic <= q.observed_at_monotonic
        final = st.close()
        assert final.quality is L
    assert [r.reqId for n, r in tws.requests if n == "CANCEL_MKT_DATA"] == [rid]


def test_reported_live_initial_image_counts(tws, feed):  # noqa: F811
    with session() as s:
        st = s.open_quote_stream(CFD, market_data_type=1)
        rid = feed.rid(0)
        feed.push(s, 0, _mdt(rid, 1), *_image(rid))
        q = st.latest()
        assert (q.quality, q.quality_reason) == (L, "REPORTED_LIVE")
        assert (q.bid_updates, q.ask_updates) == (0, 0) and q.requested_market_data_type == 1
        assert entry_quote_check(q, now_monotonic=q.observed_at_monotonic,
                                 max_age_seconds=3).usable


@pytest.mark.parametrize("mdt", [3, None])
def test_delayed_ticks_with_and_without_a_reported_type(tws, feed, mdt):  # noqa: F811
    with session() as s:
        st = s.open_quote_stream(s.resolve_contract(TLT_QUERY))
        rid = feed.rid(0)
        frames = ([] if mdt is None else [_mdt(rid, mdt)]) + _image(rid, 88.1, 88.12, live=False)
        feed.push(s, 0, *frames, error_frame(rid, 2186, "delayed market data instead"))
        for i in range(5):  # even many delayed updates never make it live
            feed.push(s, 0, *_image(rid, 88.11 + i / 100, 88.13 + i / 100, live=False))
        q = st.latest()
        assert q.quality is D and q.bid_updates == q.ask_updates == 5
        assert q.quality_reason == ("REPORTED_DELAYED" if mdt else "DELAYED_TICKS")
        assert q.bid_tick_class == q.ask_tick_class == "DELAYED" and 2186 in q.notices
        assert entry_quote_check(q, now_monotonic=q.observed_at_monotonic,
                                 max_age_seconds=3).reasons[:1] == ("NOT_LIVE",)


def test_type_2_is_frozen_even_with_updates(tws, feed):  # noqa: F811
    with session() as s:
        st = s.open_quote_stream(CFD)
        rid = feed.rid(0)
        feed.push(s, 0, _mdt(rid, 2), *_image(rid), *_image(rid, 2000.1, 2000.6))
        q = st.latest()
        assert (q.quality, q.quality_reason) == (F, "REPORTED_FROZEN")


def test_size_only_updates_confirm_a_side(tws, feed):  # noqa: F811
    with session() as s:
        st = s.open_quote_stream(GOLD)
        rid = feed.rid(0)
        feed.push(s, 0, *_image(rid))
        before = st.latest()
        feed.push(s, 0, _size(rid, 0, "250"), _size(rid, 3, "175"))  # BID_SIZE, ASK_SIZE
        q = st.latest()
        assert (q.bid, q.ask, str(q.bid_size), str(q.ask_size)) == (2000.0, 2000.5, "250", "175")
        assert (q.bid_updates, q.ask_updates) == (1, 1) and q.quality is L
        assert q.bid_at_monotonic >= before.bid_at_monotonic
        # an unset size is no confirmation
        feed.push(s, 0, frame(IN.TICK_SIZE, TickSize(reqId=rid, tickType=0)))
        assert st.latest().bid_updates == 1


def test_one_side_goes_stale_while_the_other_keeps_updating(tws, feed, monkeypatch):  # noqa: F811
    clock = {"t": 1000.0}
    monkeypatch.setattr(session_mod, "_side_clock", lambda: clock["t"])
    with session() as s:
        st = s.open_quote_stream(CFD, market_data_type=1)
        rid = feed.rid(0)
        feed.push(s, 0, _mdt(rid, 1), *_image(rid))
        for i in range(5):  # bid keeps moving every second, ask stays silent
            clock["t"] += 1.0
            feed.push(s, 0, _price(rid, 1, 1999.0 + i / 10))
        q = st.latest()
        assert (q.bid_at_monotonic, q.ask_at_monotonic, q.observed_at_monotonic) == (
            1005.0, 1000.0, 1005.0)
        r = entry_quote_check(q, now_monotonic=q.observed_at_monotonic, max_age_seconds=3.0)
        assert not r.usable and r.reasons == ("ASK_STALE",) and r.bid_age_seconds == 0.0
        assert r.ask_age_seconds == 5.0
        clock["t"] += 0.5
        feed.push(s, 0, _size(rid, 3, "900"))  # an ask size tick refreshes the ask side
        q = st.latest()
        assert entry_quote_check(q, now_monotonic=q.observed_at_monotonic,
                                 max_age_seconds=3.0).usable


@pytest.mark.parametrize(("bid", "ask", "flag"), [(2000.5, 2000.5, "LOCKED"),
                                                   (2001.0, 2000.5, "CROSSED")])
def test_locked_and_crossed_streams_are_not_usable(tws, feed, bid, ask, flag):  # noqa: F811
    with session() as s:
        st = s.open_quote_stream(CFD, market_data_type=1)
        rid = feed.rid(0)
        feed.push(s, 0, _mdt(rid, 1), *_image(rid, bid, ask))
        q = st.latest()
        assert flag in q.flags and q.quality is L
        r = entry_quote_check(q, now_monotonic=q.observed_at_monotonic, max_age_seconds=3)
        assert r.reasons == (flag,)


def test_notice_mid_stream_only_makes_it_worse(tws, feed):  # noqa: F811
    with session() as s:
        st = s.open_quote_stream(CFD, market_data_type=1)
        rid = feed.rid(0)
        feed.push(s, 0, _mdt(rid, 1), *_image(rid))
        assert st.latest().quality is L
        feed.push(s, 0, error_frame(rid, 10167, "Displaying delayed market data."))
        q = st.latest()
        assert (q.quality, q.quality_reason) == (D, "ENTITLEMENT_NOTICE") and 10167 in q.notices
        feed.push(s, 0, *_image(rid, 2000.1, 2000.6))  # more live-looking ticks change nothing
        assert st.latest().quality is D
        feed.push(s, 0, error_frame(rid, 354, "Requested market data is not subscribed."))
        q = st.latest()
        assert (q.quality, q.quality_reason) == (U, "NOT_ENTITLED") and q.unavailable_code == 354


def test_request_error_mid_stream_fails_closed(tws, feed):  # noqa: F811
    with session() as s:
        st = s.open_quote_stream(CFD, market_data_type=1)
        rid = feed.rid(0)
        feed.push(s, 0, _mdt(rid, 1), *_image(rid))
        feed.push(s, 0, error_frame(rid, 322, "Error processing request"))
        q = st.latest()
        assert (q.quality, q.quality_reason) == (U, "STREAM_FAILED")
        assert "REQUEST_ERROR" in q.flags and "322" in q.request_error
        st.close()
    assert "CANCEL_MKT_DATA" in tws.names()


def test_connectivity_interruption_fails_open_streams(tws, feed):  # noqa: F811
    with session() as s:
        st = s.open_quote_stream(CFD, market_data_type=1)
        rid = feed.rid(0)
        feed.push(s, 0, _mdt(rid, 1), *_image(rid))
        feed.push(s, 0, error_frame(-1, 1100, "Connectivity between IB and TWS has been lost."))
        assert st.latest().quality_reason == "STREAM_FAILED"
        feed.push(s, 0, error_frame(-1, 1102, "Connectivity restored, data maintained."))
        assert st.latest().quality is U  # stays failed until reopened
        st.close()


# --- two streams, the cap, the session-wide type ------------------------------------------------------
def test_two_streams_are_independent(tws, feed):  # noqa: F811
    with session() as s:
        a = s.open_quote_stream(GOLD, market_data_type=1)
        b = s.open_quote_stream(CFD, market_data_type=1)
        assert {x.stream_id for x in s.quote_streams()} == {a.stream_id, b.stream_id}
        ra, rb = feed.rid(0), feed.rid(1)
        assert (ra, rb) == (a.stream_id, b.stream_id)
        feed.push(s, 1, _mdt(rb, 1), *_image(rb, 5000.0, 5000.5))
        feed.push(s, 0, *_image(ra))
        qa, qb = a.latest(), b.latest()
        assert (qa.symbol, qa.bid, qa.quality) == ("XAUUSD", 2000.0, F)
        assert (qb.symbol, qb.bid, qb.quality) == ("IBUS500", 5000.0, L)
        a.close()
        assert b.latest().quality is L and s.quote_streams() == (b,)
    assert sorted(r.reqId for n, r in tws.requests if n == "CANCEL_MKT_DATA") == sorted([ra, rb])


def test_stream_cap_refuses_and_sends_nothing(tws, feed):  # noqa: F811
    with session() as s:
        s.max_quote_streams = 2
        s.open_quote_stream(GOLD)
        s.open_quote_stream(CFD)
        before = list(tws.requests)
        with pytest.raises(E.QuoteStreamLimit):
            s.open_quote_stream(GOLD)
        assert tws.requests == before
        s.max_quote_streams = MAX_QUOTE_STREAMS + 1
        with pytest.raises(ValueError):
            s.open_quote_stream(GOLD)
        assert tws.requests == before
    assert tws.names().count("CANCEL_MKT_DATA") == 2


def test_market_data_type_is_one_per_session_while_streams_are_open(tws, feed):  # noqa: F811
    with session() as s:
        contract = s.resolve_contract(TLT_QUERY)
        st = s.open_quote_stream(GOLD, market_data_type=3)
        before = list(tws.requests)
        with pytest.raises(E.QuoteStreamConflict):
            s.open_quote_stream(CFD, market_data_type=1)
        with pytest.raises(E.QuoteStreamConflict):
            s.quote_stream_sample(contract, market_data_type=1, seconds=0.2)
        with pytest.raises(E.QuoteStreamConflict):
            s.quote_snapshot(contract, market_data_type=1, timeout=0.2)
        assert tws.requests == before  # refused before anything was sent
        s.quote_stream_sample(contract, market_data_type=3, seconds=0.2)  # same type: fine
        st.close()
        s.open_quote_stream(CFD, market_data_type=1)  # no open stream: any type
        types = [r.marketDataType for n, r in tws.requests if n == "REQ_MARKET_DATA_TYPE"]
        assert types == [3, 3, 1]


def test_open_requires_ready_and_sends_nothing(tws):  # noqa: F811
    s = session()
    with pytest.raises(E.SessionStateError):
        s.open_quote_stream(GOLD)
    s.connect()
    s.disconnect()
    with pytest.raises(E.SessionStateError):
        s.open_quote_stream(GOLD)
    assert "REQ_MKT_DATA" not in tws.names()
    with pytest.raises(ValueError):
        session().open_quote_stream(GOLD, market_data_type=5)


# --- cleanup: close, disconnect, poison, loss -----------------------------------------------------------
def test_close_cancels_once_and_latest_then_refuses(tws, feed):  # noqa: F811
    with session() as s:
        st = s.open_quote_stream(GOLD)
        feed.push(s, 0, *_image(feed.rid(0)))
        final = st.close()
        assert st.closed and st.closed_reason == "CLOSED" and st.final is final
        assert final.bid == 2000.0
        with pytest.raises(E.QuoteStreamClosed):
            st.latest()
        assert st.close() is final  # idempotent, nothing sent again
        assert s.quote_streams() == () and s._stream_mdt is None
    assert [r.reqId for n, r in tws.requests if n == "CANCEL_MKT_DATA"] == [st.stream_id]


def test_cancel_failure_on_close_is_recorded(tws, feed):  # noqa: F811
    with session() as s:
        st = s.open_quote_stream(GOLD)
        tws.fail_send = {"CANCEL_MKT_DATA"}
        with pytest.raises(E.CleanupError) as ei:
            st.close()
        assert ei.value.result is st.final and st.closed
        assert any("quote_stream" in f for f in s.cleanup_failures)
        tws.fail_send = set()


def test_disconnect_cancels_every_stream(tws, feed):  # noqa: F811
    s = session()
    s.connect()
    a, b = s.open_quote_stream(GOLD), s.open_quote_stream(CFD)
    s.disconnect()
    assert_closed(s)
    assert a.closed_reason == b.closed_reason == "SESSION_DISCONNECTED"
    assert sorted(r.reqId for n, r in tws.requests if n == "CANCEL_MKT_DATA") == sorted(
        [a.stream_id, b.stream_id])
    names = tws.names()
    assert max(i for i, n in enumerate(names) if n == "CANCEL_MKT_DATA") == len(names) - 1
    assert not s.cleanup_failures
    with pytest.raises(E.QuoteStreamClosed):
        a.latest()


def test_poison_cancels_every_stream(tws, feed):  # noqa: F811
    s = session()
    s.connect()
    st = s.open_quote_stream(GOLD)
    feed.push(s, 0, *_image(feed.rid(0)))
    conn = feed.subs[0][0]
    conn.push(frame(IN.MANAGED_ACCTS, ManagedAccounts(accountsList=OTHER_PAPER)))
    deadline = time.monotonic() + 3
    while not st.closed and time.monotonic() < deadline:
        time.sleep(0.01)
    assert st.closed_reason == "SESSION_POISONED" and s.poisoned
    assert [r.reqId for n, r in tws.requests if n == "CANCEL_MKT_DATA"] == [st.stream_id]
    assert not s.cleanup_failures  # sent before the transport closed
    with pytest.raises(E.AccountIdentityError):
        s.open_quote_stream(GOLD)
    s.disconnect()
    assert_closed(s)


def test_connection_loss_closes_streams_and_records_cleanup(tws, feed):  # noqa: F811
    s = session()
    s.connect()
    st = s.open_quote_stream(GOLD)
    tws.drop()
    deadline = time.monotonic() + 3
    while not st.closed and time.monotonic() < deadline:
        time.sleep(0.01)
    assert st.closed_reason == "CONNECTION_LOST"
    assert any("quote_stream" in f and "connection is down" in f for f in s.cleanup_failures)
    s.disconnect()
    assert_closed(s)


def test_failed_open_send_is_cancelled_and_raised(tws, feed):  # noqa: F811
    with session() as s:
        tws.fail_send = {"REQ_MKT_DATA"}
        with pytest.raises(E.IbkrRequestError) as ei:  # the SDK reports 510 synchronously
            s.open_quote_stream(GOLD)
        assert ei.value.code == 510
        tws.fail_send = set()
        assert s.quote_streams() == () and s._stream_mdt is None
        assert "CANCEL_MKT_DATA" in tws.names()


# --- thread safety -------------------------------------------------------------------------------------
def test_latest_builds_under_the_session_lock(tws, feed, monkeypatch):  # noqa: F811
    """Deterministic: while latest() classifies, another thread (where the SDK's tick callbacks
    run) cannot take the session lock."""
    seen = []
    real = session_mod.classify_quote

    def probe(**kw):
        result = {}
        t = threading.Thread(target=lambda: result.setdefault(
            "got", s._cond.acquire(blocking=False)))
        t.start()
        t.join()
        if result["got"]:
            s._cond.release()
        seen.append(result["got"])
        return real(**kw)

    with session() as s:
        st = s.open_quote_stream(GOLD)
        feed.push(s, 0, *_image(feed.rid(0)))
        monkeypatch.setattr(session_mod, "classify_quote", probe)
        st.latest()
        st.close()
    assert seen and not any(seen)


def test_latest_is_consistent_under_concurrent_ticks(tws, feed):  # noqa: F811
    """Ticks are delivered on another thread through the same callbacks the SDK calls. Every
    snapshot must satisfy bid == 100 + bid_updates and ask == 200 + ask_updates exactly, which
    holds only if no snapshot mixes two states, whatever the interleaving."""
    n = 1500
    with session() as s:
        st = s.open_quote_stream(GOLD)
        rid = st.stream_id
        s._on_tick_price(rid, 1, 100.0)
        s._on_tick_price(rid, 2, 200.0)
        go = threading.Event()

        def ticks():
            go.wait()
            for i in range(1, n + 1):
                s._on_tick_price(rid, 1, 100.0 + i)
                s._on_tick_size(rid, 0, 5)  # repeated size: never an update
                s._on_tick_price(rid, 2, 200.0 + i)

        t = threading.Thread(target=ticks)
        t.start()
        go.set()
        snaps = []
        while t.is_alive() or not snaps:
            snaps.append(st.latest())
        t.join()
        snaps.append(st.latest())
        for q in snaps:
            assert q.bid == 100.0 + q.bid_updates and q.ask == 200.0 + q.ask_updates
            assert q.ask_updates <= q.bid_updates <= q.ask_updates + 1
        assert (snaps[-1].bid_updates, snaps[-1].ask_updates) == (n, n)
        st.close()


# --- the bounded sample uses the same classification -------------------------------------------
def test_bounded_sample_gold_case_is_live_and_image_only_is_not(tws):  # noqa: F811
    def gold(srv, c, r):
        c.push(_price(r.reqId, 1, 2000.0))
        c.push(_price(r.reqId, 2, 2000.5))
        for i in range(1, 4):
            c.push(_price(r.reqId, 1, 2000.0 + i / 10))
            c.push(_price(r.reqId, 2, 2000.5 + i / 10))

    def image_only(srv, c, r):
        c.push(_price(r.reqId, 1, 2000.0))
        c.push(_price(r.reqId, 2, 2000.5))

    with session(stream_settle_seconds=0.1) as s:
        tws.on["REQ_MKT_DATA"] = gold
        report = s.quote(GOLD, stream_seconds=0.5)
        (q,) = report.attempts
        assert (q.quality, q.quality_reason) == (L, "TYPE_UNREPORTED_UPDATES_SEEN")
        assert (q.bid_updates, q.ask_updates) == (3, 3) and report.status == "PASS"
        tws.on["REQ_MKT_DATA"] = image_only
        q = s.quote_stream_sample(GOLD, seconds=0.4)
        assert (q.quality, q.quality_reason) == (F, "TYPE_UNREPORTED_NO_UPDATES_YET")
        assert s.quote(GOLD, stream_seconds=0.4).status == "PASS_WITH_WARNINGS"


# --- the deny-by-default surface is unchanged ----------------------------------------------------
def test_allowlists_are_byte_for_byte_unchanged():
    assert ALLOWED_OUT_MESSAGES == frozenset({
        OUT.START_API, OUT.REQ_CONTRACT_DATA, OUT.CANCEL_CONTRACT_DATA, OUT.REQ_ACCOUNT_SUMMARY,
        OUT.CANCEL_ACCOUNT_SUMMARY, OUT.REQ_POSITIONS, OUT.CANCEL_POSITIONS, OUT.REQ_MKT_DATA,
        OUT.CANCEL_MKT_DATA, OUT.REQ_MARKET_DATA_TYPE, OUT.REQ_CONFIG,
        OUT.REQ_SEC_DEF_OPT_PARAMS})
    assert ALLOWED_CLIENT_METHODS == frozenset({
        "connect", "disconnect", "isConnected", "run", "reset", "setConnState",
        "checkConnected", "validateInvalidSymbols", "useProtoBuf", "serverVersion",
        "twsConnectionTime", "logRequest", "msgLoopTmo", "msgLoopRec", "keyboardInterrupt",
        "keyboardInterruptHard", "sendMsg", "sendMsgProtoBuf", "startApi", "startApiProtoBuf",
        "reqContractDetails", "reqContractDataProtoBuf", "cancelContractData",
        "cancelContractDataProtoBuf", "reqAccountSummary", "reqAccountSummaryProtoBuf",
        "cancelAccountSummary", "cancelAccountSummaryProtoBuf", "reqPositions",
        "reqPositionsProtoBuf", "cancelPositions", "cancelPositionsProtoBuf", "reqMktData",
        "reqMarketDataProtoBuf", "cancelMktData", "cancelMarketDataProtoBuf",
        "reqMarketDataType", "reqMarketDataTypeProtoBuf", "reqConfigProtoBuf",
        "reqSecDefOptParams", "reqSecDefOptParamsProtoBuf"})


def test_streams_send_only_market_data_messages(tws, feed):  # noqa: F811
    with session() as s:
        before = len(tws.requests)
        s.open_quote_stream(GOLD).close()
        s.open_quote_stream(CFD)
    sent = {n for n, _ in tws.requests[before:]}
    assert sent == {"REQ_MARKET_DATA_TYPE", "REQ_MKT_DATA", "CANCEL_MKT_DATA"}


def test_object_graph_with_open_streams_reaches_no_unfiltered_send(tws, feed):  # noqa: F811
    s = session()
    s.connect()
    a = s.open_quote_stream(GOLD)
    feed.push(s, 0, *_image(feed.rid(0)))
    snap = a.latest()
    closed = s.open_quote_stream(CFD)
    closed.close()
    reachable = _walk(s, a, snap, closed)
    _assert_no_raw_transport(reachable)
    wire, requests = list(tws.wire), list(tws.requests)
    list(_fire_send_like(reachable))
    assert tws.wire == wire and tws.requests == requests
    assert a.latest().bid == 2000.0
    s.disconnect()
    assert_closed(s)


# --- the check script's logic end to end over the fake TWS --------------------------------------
def test_stream_check_end_to_end_over_the_fake_tws(tws):  # noqa: F811
    from test_ibkr_session_sdk import CFG

    from tradingbot.ibkr.stream_check import run_stream_check, summarize

    rep = run_stream_check([TLT_QUERY], None, hold_seconds=1.0, session_factory=lambda c: session(),
                           loader=lambda p: CFG)
    assert rep["overall"] == "COMPLETE", rep.get("failure")
    (row,) = rep["results"]
    assert row["quality"] == "DELAYED" and row["reported_market_data_type"] == "DELAYED"
    assert row["tick_classes"] == {"bid": "DELAYED", "ask": "DELAYED"} and 10167 in row["notices"]
    assert row["entry_usable_at_end"] is False and "NOT_LIVE" in row["entry_reasons_at_end"]
    assert tws.names().count("CANCEL_MKT_DATA") == 1 and "DELAYED" in summarize(rep)
