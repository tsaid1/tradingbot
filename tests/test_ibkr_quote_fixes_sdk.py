"""Review round for the held quote streams, through the REAL ibapi decoder over the fake TWS:
the reviewer's probes P2, P3, P4, P5, P7, P11 as permanent tests, plus HALTED (note b)."""
from __future__ import annotations

import pytest

pytest.importorskip("ibapi")

from _ibkr_fake_tws import frame
from ibapi.message import IN
from ibapi.protobuf.TickGeneric_pb2 import TickGeneric
from test_ibkr_quote_streams_sdk import CFD, GOLD, Feed, _image, _mdt, _price, _size
from test_ibkr_session_sdk import session, tws  # noqa: F401 - pytest fixture

from tradingbot.ibkr import errors as E
from tradingbot.ibkr import session as session_mod
from tradingbot.ibkr.models import QuoteQuality, entry_quote_check

L, D, F, U = QuoteQuality.LIVE, QuoteQuality.DELAYED, QuoteQuality.FROZEN, QuoteQuality.UNAVAILABLE


@pytest.fixture
def feed(tws):  # noqa: F811
    return Feed(tws)


@pytest.fixture
def clock(monkeypatch):
    c = {"t": 1000.0}
    monkeypatch.setattr(session_mod, "_side_clock", lambda: c["t"])
    return c


def usable(q):
    return entry_quote_check(q, now_monotonic=q.observed_at_monotonic, max_age_seconds=3).usable


# --- B1 (P3): placeholders first, then the real image ------------------------------------------
def test_P3_placeholder_then_image_is_not_an_update(tws, feed):  # noqa: F811
    with session() as s:
        st = s.open_quote_stream(GOLD)
        rid = feed.rid(0)
        feed.push(s, 0, _price(rid, 1, -1.0, "0"), _price(rid, 2, -1.0, "0"))
        feed.push(s, 0, *_image(rid))
        q = st.latest()
        assert (q.bid_updates, q.ask_updates) == (0, 0)
        assert (q.quality, q.quality_reason) == (F, "TYPE_UNREPORTED_NO_UPDATES_YET")
        assert {"BID_PLACEHOLDER_TICK", "ASK_PLACEHOLDER_TICK"} <= set(q.flags)
        assert not usable(q)


# --- S2 (P4): NaN / inf ---------------------------------------------------------------------------
def test_P4_non_finite_ticks_are_flagged_never_counted_never_valid(tws, feed):  # noqa: F811
    with session() as s:
        st = s.open_quote_stream(GOLD)
        rid = feed.rid(0)
        feed.push(s, 0, _price(rid, 1, 2000.0), _price(rid, 2, float("inf")))
        feed.push(s, 0, _price(rid, 1, 2000.1), _price(rid, 2, float("nan")))
        feed.push(s, 0, _price(rid, 2, float("nan")))
        q = st.latest()
        assert q.ask_updates == 0 and "ASK_NON_FINITE_TICK" in q.flags
        assert "NON_FINITE_ASK" in q.flags and not q.two_sided_valid and not usable(q)


# --- B3 (P2): one change, then a minute of identical repeats -------------------------------------
def test_P2_identical_repeats_neither_refresh_nor_keep_it_live(tws, feed, clock):  # noqa: F811
    with session() as s:
        st = s.open_quote_stream(GOLD)
        rid = feed.rid(0)
        feed.push(s, 0, *_image(rid))
        clock["t"] += 0.5
        feed.push(s, 0, *_image(rid, 2000.1, 2000.6))
        clock["t"] += 0.5
        feed.push(s, 0, *_image(rid, 2000.2, 2000.7))  # two changes per side: live
        q = st.latest()
        assert (q.quality, q.bid_updates, q.ask_updates) == (L, 2, 2) and usable(q)
        for _ in range(60):
            clock["t"] += 1.0
            feed.push(s, 0, *_image(rid, 2000.2, 2000.7))
        q = st.latest()
        assert q.bid_at_monotonic == q.ask_at_monotonic == 1001.0
        assert q.bid_last_tick_monotonic == 1061.0  # diagnostics only
        assert (q.quality, q.quality_reason) == (F, "TYPE_UNREPORTED_UPDATES_TOO_OLD")
        assert not usable(q)
        clock["t"] += 1.0
        feed.push(s, 0, *_image(rid, 2000.3, 2000.8))  # moving again: live again
        q = st.latest()
        assert q.quality is L and usable(q)


def test_P1_one_size_refresh_per_side_is_not_enough(tws, feed):  # noqa: F811
    with session() as s:
        st = s.open_quote_stream(GOLD)
        rid = feed.rid(0)
        feed.push(s, 0, *_image(rid))
        feed.push(s, 0, _size(rid, 0, "101"), _size(rid, 3, "99"))
        q = st.latest()
        assert (q.bid_updates, q.ask_updates, q.quality) == (1, 1, F) and not usable(q)
        feed.push(s, 0, _size(rid, 0, "102"), _size(rid, 3, "98"))
        assert st.latest().quality is L


def test_reported_live_quiet_side_goes_stale_by_its_last_change(tws, feed, clock):  # noqa: F811
    with session() as s:
        st = s.open_quote_stream(CFD, market_data_type=1)
        rid = feed.rid(0)
        feed.push(s, 0, _mdt(rid, 1), *_image(rid))
        for _ in range(4):
            clock["t"] += 1.0
            feed.push(s, 0, *_image(rid))  # identical: TWS says live, nothing changed
        q = st.latest()
        assert q.quality is L
        r = entry_quote_check(q, now_monotonic=q.observed_at_monotonic, max_age_seconds=3)
        assert r.reasons == ("BID_STALE", "ASK_STALE")


# --- B2 (P7): requested frozen, type unreported ------------------------------------------------
@pytest.mark.parametrize("requested", [2, 4])
def test_P7_requested_frozen_type_is_never_live(tws, feed, requested):  # noqa: F811
    with session() as s:
        st = s.open_quote_stream(GOLD, market_data_type=requested)
        rid = feed.rid(0)
        feed.push(s, 0, *_image(rid))
        for i in range(1, 5):
            feed.push(s, 0, _size(rid, 0, str(5 + i)), _size(rid, 3, str(6 + i)))
        q = st.latest()
        assert (q.quality, q.quality_reason) == (F, "TYPE_UNREPORTED_REQUESTED_NOT_ELIGIBLE")
        assert not usable(q)


# --- note (a) (P5): reported 1 -> 3 -> 1 stays not live --------------------------------------------
def test_P5_bad_type_is_sticky_for_the_subscription(tws, feed):  # noqa: F811
    with session() as s:
        st = s.open_quote_stream(CFD, market_data_type=1)
        rid = feed.rid(0)
        feed.push(s, 0, _mdt(rid, 1), *_image(rid))
        assert st.latest().quality is L
        feed.push(s, 0, _mdt(rid, 3))
        assert st.latest().quality is D
        feed.push(s, 0, _mdt(rid, 1), *_image(rid, 2000.1, 2000.6))
        q = st.latest()
        assert (q.quality, q.quality_reason) == (D, "REPORTED_TYPE_HISTORY_NOT_LIVE")
        assert not usable(q)
        st.close()
        st2 = s.open_quote_stream(CFD, market_data_type=1)  # a new subscription starts clean
        r2 = feed.rid(1)
        feed.push(s, 1, _mdt(r2, 1), *_image(r2))
        assert st2.latest().quality is L


# --- note (b): HALTED ----------------------------------------------------------------------------
def test_halted_tick_makes_the_entry_unusable(tws, feed):  # noqa: F811
    with session() as s:
        st = s.open_quote_stream(CFD, market_data_type=1)
        rid = feed.rid(0)
        feed.push(s, 0, _mdt(rid, 1), *_image(rid))
        feed.push(s, 0, frame(IN.TICK_GENERIC, TickGeneric(reqId=rid, tickType=49, value=1.0)))
        q = st.latest()
        assert q.halted == 1.0 and q.quality is L and "HALTED" in q.flags
        r = entry_quote_check(q, now_monotonic=q.observed_at_monotonic, max_age_seconds=3)
        assert r.reasons == ("HALTED",)
        feed.push(s, 0, frame(IN.TICK_GENERIC, TickGeneric(reqId=rid, tickType=49, value=0.0)))
        assert usable(st.latest())


# --- S3 (P11): a type conflict found at the send sends nothing, not even a cancel -----------
def test_P11_conflict_at_send_sends_no_cancel(tws):  # noqa: F811
    with session() as s:
        orig = s._check_stream_type
        calls = {"n": 0}

        def flip(mdt):
            calls["n"] += 1
            if calls["n"] == 2:  # the re-check inside the send: a stream opened in between
                s._streams[999] = object()
                s._stream_mdt = 1
            try:
                return orig(mdt)
            finally:
                if calls["n"] == 2:
                    s._streams.pop(999)
                    s._stream_mdt = None

        s._check_stream_type = flip
        before = len(tws.requests)
        with pytest.raises(E.QuoteStreamConflict):
            s.quote_stream_sample(GOLD, market_data_type=3, seconds=0.2)
        assert [n for n, _ in tws.requests[before:]] == [] and not s.cleanup_failures
