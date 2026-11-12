"""Second review round, through the REAL ibapi decoder over the fake TWS (N1, notes a, b, c)."""
from __future__ import annotations

import pytest

pytest.importorskip("ibapi")

from _ibkr_fake_tws import error_frame, frame
from ibapi.message import IN
from ibapi.protobuf.TickGeneric_pb2 import TickGeneric
from test_ibkr_quote_streams_sdk import CFD, GOLD, Feed, _image, _mdt, _price, _size
from test_ibkr_session_sdk import session, tws  # noqa: F401 - pytest fixture

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


def usable(q, now=None, age=3):
    now = q.observed_at_monotonic if now is None else now
    return entry_quote_check(q, now_monotonic=now, max_age_seconds=age).usable


def test_size_flicker_with_unchanged_prices_is_not_live(tws, feed, clock):  # noqa: F811
    with session() as s:
        st = s.open_quote_stream(GOLD)
        rid = feed.rid(0)
        feed.push(s, 0, *_image(rid))
        for v in ("200", "100"):
            clock["t"] += 0.5
            feed.push(s, 0, _size(rid, 0, v), _size(rid, 3, v))
        q = st.latest()
        assert (q.bid_updates, q.ask_updates) == (2, 2)
        assert (q.quality, q.quality_reason) == (F, "TYPE_UNREPORTED_NO_PRICE_CHANGE_YET")
        assert not usable(q)
        clock["t"] += 0.5
        feed.push(s, 0, _price(rid, 1, 2000.01), _price(rid, 2, 2000.49))
        q = st.latest()
        assert q.quality is L and usable(q)


def test_off_tick_price_changes_never_count(tws, feed):  # noqa: F811
    with session() as s:
        st = s.open_quote_stream(GOLD)  # min_tick 0.01
        rid = feed.rid(0)
        feed.push(s, 0, *_image(rid))
        feed.push(s, 0, _price(rid, 1, 2000.001), _price(rid, 2, 2000.503))
        feed.push(s, 0, _price(rid, 1, 2000.002), _price(rid, 2, 2000.504))
        q = st.latest()
        assert (q.bid_updates, q.ask_updates, q.quality) == (0, 0, F)
        assert {"BID_OFF_TICK_TICK", "ASK_OFF_TICK_TICK"} <= set(q.flags) and not usable(q)


def test_type_7_then_1_stays_not_live(tws, feed):  # noqa: F811
    with session() as s:
        st = s.open_quote_stream(CFD, market_data_type=1)
        rid = feed.rid(0)
        feed.push(s, 0, _mdt(rid, 1), *_image(rid), _mdt(rid, 7), _mdt(rid, 1),
                  *_image(rid, 2000.1, 2000.6))
        q = st.latest()
        assert (q.quality, q.quality_reason) == (F, "REPORTED_TYPE_HISTORY_NOT_LIVE")


def test_unreported_live_snapshot_rechecked_after_the_window(tws, feed, clock):  # noqa: F811
    with session() as s:
        st = s.open_quote_stream(GOLD)
        rid = feed.rid(0)
        feed.push(s, 0, *_image(rid))
        for k in (1, 2):
            clock["t"] += 0.5
            feed.push(s, 0, *_image(rid, 2000 + k / 10, 2000.5 + k / 10))
        q = st.latest()
        assert q.quality is L
    t = q.observed_at_monotonic
    assert usable(q, now=t + 2.9, age=3)
    assert not usable(q, now=t + 8, age=10)  # the label in the snapshot is not trusted


def test_halted_minus_one_counts_as_halted(tws, feed):  # noqa: F811
    with session() as s:
        st = s.open_quote_stream(CFD, market_data_type=1)
        rid = feed.rid(0)
        feed.push(s, 0, _mdt(rid, 1), *_image(rid),
                  frame(IN.TICK_GENERIC, TickGeneric(reqId=rid, tickType=49, value=-1.0)))
        q = st.latest()
        r = entry_quote_check(q, now_monotonic=q.observed_at_monotonic, max_age_seconds=3)
        assert r.reasons == ("HALTED",)


def test_10090_fails_the_stream(tws, feed):  # noqa: F811
    with session() as s:
        st = s.open_quote_stream(CFD, market_data_type=1)
        rid = feed.rid(0)
        feed.push(s, 0, _mdt(rid, 1), *_image(rid),
                  error_frame(rid, 10090, "Part of requested market data is not subscribed."))
        q = st.latest()
        assert (q.quality, q.quality_reason) == (U, "STREAM_FAILED")
