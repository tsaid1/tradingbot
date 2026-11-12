"""Review round for the held quote streams (findings B1-B3, S1, S2, notes a, b, d). Pure: passes
WITHOUT ibapi (GitHub CI). The stream-level counterparts are in test_ibkr_quote_fixes_sdk.py."""
from __future__ import annotations

import itertools
from decimal import Decimal

import pytest

from tradingbot.ibkr.models import (
    ENTRY_QUOTE_REASONS,
    QUOTE_REASONS,
    UNREPORTED_LIVE_WINDOW_SECONDS,
    UNREPORTED_MIN_CHANGES,
    QuoteQuality,
    QuoteSnapshot,
    SideEvidence,
    classify_quote,
    entry_quote_check,
)

L, D, F, U = QuoteQuality.LIVE, QuoteQuality.DELAYED, QuoteQuality.FROZEN, QuoteQuality.UNAVAILABLE
NAN, INF = float("nan"), float("inf")


# --- B1: the initial image is the first VALID value; placeholders and non-finite never count ----
def side(*ticks):
    """ticks: (kind, value, mono) with live tick ids."""
    s = SideEvidence()
    for kind, value, mono in ticks:
        s.observe(kind, value, False, mono=mono)
    return s


def test_placeholder_then_real_image_is_the_image_not_an_update():
    s = side(("price", -1.0, 1.0), ("size", Decimal(0), 1.0), ("price", 2000.0, 2.0),
             ("size", Decimal(100), 2.0))
    assert s.image and s.changes == 0 and s.placeholder_seen
    assert (s.price, s.size, s.fresh_mono) == (2000.0, Decimal(100), 2.0)


def test_zero_price_is_a_placeholder_too():
    s = side(("price", 0.0, 1.0), ("price", 2000.0, 2.0))
    assert s.placeholder_seen and s.image and s.changes == 0


def test_size_alone_never_establishes_the_image():
    s = side(("size", Decimal(5), 1.0), ("size", Decimal(7), 2.0))
    assert not s.image and s.changes == 0 and s.fresh_mono is None
    s.observe("price", 2000.0, False, mono=3.0)
    s.observe("size", Decimal(9), False, mono=3.0)  # first size at/after the image: part of it
    assert s.image and s.changes == 0 and s.size == Decimal(9)


@pytest.mark.parametrize("bad", [NAN, INF, -INF])
def test_non_finite_values_never_count(bad):
    s = side(("price", 2000.0, 1.0), ("price", bad, 2.0), ("price", bad, 3.0),
             ("price", 2000.0, 4.0))
    assert s.non_finite_seen and s.changes == 0 and s.price == 2000.0 and s.fresh_mono == 1.0
    s = side(("price", bad, 1.0), ("price", 2000.0, 2.0))
    assert s.image and s.changes == 0
    s = side(("price", 2000.0, 1.0), ("size", Decimal(5), 1.0), ("size", Decimal("NaN"), 2.0),
             ("size", Decimal(5), 3.0))
    assert s.non_finite_seen and s.changes == 0


def test_negative_size_is_a_placeholder_and_zero_size_is_valid():
    s = side(("price", 2000.0, 1.0), ("size", Decimal(-1), 2.0), ("size", Decimal(0), 3.0),
             ("size", Decimal(4), 4.0))
    assert s.placeholder_seen and s.changes == 1 and s.size == Decimal(4)


# --- B3: freshness moves only on the image or a change; repeats never refresh it -----------------
def test_identical_repeats_never_refresh_the_side():
    s = side(("price", 2000.0, 1.0), ("size", Decimal(5), 1.0))
    for t in range(2, 62):
        s.observe("price", 2000.0, False, mono=float(t))
        s.observe("size", Decimal(5), False, mono=float(t))
    assert s.changes == 0 and s.fresh_mono == 1.0 and s.last_tick_mono == 61.0
    s.observe("size", Decimal(6), False, mono=62.0)
    assert s.changes == 1 and s.fresh_mono == 62.0


def test_tick_class_records_delayed_ids_even_on_placeholders():
    s = SideEvidence()
    s.observe("price", -1.0, True, mono=1.0)
    assert s.tick_class == "DELAYED" and not s.image


# --- B2 / B3: the unreported-type rule ----------------------------------------------------------
def cq(**kw):
    base = {"bid": 10.0, "ask": 10.02, "last": None, "market_data_type": None,
            "delayed_ticks_seen": False, "live_ticks_seen": True, "bid_updates": 2,
            "ask_updates": 2, "requested_market_data_type": 3, "bid_change_age_seconds": 0.5,
            "ask_change_age_seconds": 0.5, "bid_price_changes": 1, "ask_price_changes": 1,
            "bid_price_change_age_seconds": 0.5, "ask_price_change_age_seconds": 0.5}
    base.update(kw)
    return classify_quote(**base)


def test_unreported_live_needs_eligible_request_two_changes_and_recent_changes():
    assert (cq().quality, cq().reason) == (L, "TYPE_UNREPORTED_UPDATES_SEEN")
    assert cq(requested_market_data_type=1).quality is L
    assert UNREPORTED_MIN_CHANGES == 2 and UNREPORTED_LIVE_WINDOW_SECONDS == 5.0


@pytest.mark.parametrize("requested", [2, 4, None, 7])
def test_unreported_with_requested_frozen_type_is_never_live(requested):
    c = cq(requested_market_data_type=requested, bid_updates=50, ask_updates=50)
    assert (c.quality, c.reason) == (F, "TYPE_UNREPORTED_REQUESTED_NOT_ELIGIBLE")


@pytest.mark.parametrize(("b", "a"), [(1, 1), (1, 5), (5, 1), (0, 9)])
def test_unreported_needs_two_changes_on_each_side(b, a):
    c = cq(bid_updates=b, ask_updates=a)
    assert (c.quality, c.reason) == (F, "TYPE_UNREPORTED_NO_UPDATES_YET")


@pytest.mark.parametrize(("ba", "aa"), [(5.001, 0.1), (0.1, 60.0), (None, 0.1), (0.1, NAN),
                                        (-0.5, 0.1)])
def test_unreported_needs_each_side_changed_within_the_window(ba, aa):
    c = cq(bid_change_age_seconds=ba, ask_change_age_seconds=aa)
    assert (c.quality, c.reason) == (F, "TYPE_UNREPORTED_UPDATES_TOO_OLD")


def test_unreported_window_boundary_is_inclusive():
    assert cq(bid_change_age_seconds=5.0, ask_change_age_seconds=5.0).quality is L


def test_reported_live_label_unchanged_image_counts():
    c = cq(market_data_type=1, bid_updates=0, ask_updates=0, bid_change_age_seconds=None,
           ask_change_age_seconds=None, requested_market_data_type=2)
    assert (c.quality, c.reason) == (L, "REPORTED_LIVE")


# --- note (a): bad news is sticky per subscription ------------------------------------------------
@pytest.mark.parametrize(("history", "quality"), [((1, 3), D), ((3, 1), D), ((1, 4, 1), D),
                                                  ((2, 1), F), ((1, 2, 1), F)])
def test_an_earlier_not_live_type_keeps_the_subscription_not_live(history, quality):
    c = cq(market_data_type=history[-1], market_data_types_seen=history)
    if history[-1] == 1:
        assert (c.quality, c.reason) == (quality, "REPORTED_TYPE_HISTORY_NOT_LIVE")
    assert c.quality is not L


def test_history_of_only_live_types_is_fine():
    assert cq(market_data_type=1, market_data_types_seen=(1, 1)).quality is L


# --- S2: non-finite prices are flagged and never two-sided valid ---------------------------------
@pytest.mark.parametrize(("bid", "ask", "last", "flag"), [
    (10.0, NAN, None, "NON_FINITE_ASK"), (INF, 10.02, None, "NON_FINITE_BID"),
    (10.0, 10.02, NAN, "NON_FINITE_LAST"), (-INF, 10.02, None, "NON_FINITE_BID")])
def test_non_finite_prices_are_flagged(bid, ask, last, flag):
    c = cq(market_data_type=1, bid=bid, ask=ask, last=last)
    assert flag in c.flags and "CROSSED" not in c.flags
    q = snap(bid=bid, ask=ask, last=last, quality=c.quality, flags=c.flags)
    assert not q.two_sided_valid


def test_only_non_finite_prices_is_no_price():
    c = cq(market_data_type=1, bid=NAN, ask=INF, last=None)
    assert (c.quality, c.reason) == (U, "NO_PRICE")


# --- S1 and note (b): the entry check ----------------------------------------------------------
def snap(**kw) -> QuoteSnapshot:
    base = {"con_id": 1, "symbol": "X", "bid": 10.0, "bid_size": None, "ask": 10.02,
            "ask_size": None, "last": None, "last_size": None, "market_data_type": 1,
            "market_data_type_name": "LIVE", "requested_market_data_type": 1,
            "received_at": "2026-11-12T10:00:00.000+00:00", "last_trade_time": None,
            "quality": L, "flags": (), "route": "held_stream", "bid_at_monotonic": 100.0,
            "ask_at_monotonic": 100.0, "bid_tick_class": "LIVE", "ask_tick_class": "LIVE",
            "quality_reason": "REPORTED_LIVE", "observed_at_monotonic": 100.5}
    base.update(kw)
    return QuoteSnapshot(**base)


@pytest.mark.parametrize(("kw", "reason"), [({"ask": INF}, "ASK_NOT_FINITE"),
                                            ({"bid": NAN}, "BID_NOT_FINITE"),
                                            ({"bid": -INF}, "BID_NOT_FINITE")])
def test_entry_check_requires_finite_prices(kw, reason):
    r = entry_quote_check(snap(**kw), now_monotonic=100.5, max_age_seconds=3)
    assert not r.usable and reason in r.reasons and set(r.reasons) <= set(ENTRY_QUOTE_REASONS)


@pytest.mark.parametrize("halted", [1.0, 2.0, -1.0, NAN])
def test_halted_quote_is_not_usable_but_label_unchanged(halted):
    q = snap(halted=halted)
    r = entry_quote_check(q, now_monotonic=100.5, max_age_seconds=3)
    assert r.reasons == ("HALTED",) and q.quality is L


def test_not_halted_is_usable():
    assert entry_quote_check(snap(halted=0.0), now_monotonic=100.5, max_age_seconds=3).usable
    assert entry_quote_check(snap(), now_monotonic=100.5, max_age_seconds=3).usable


def test_snapshot_keeps_a_diagnostic_last_tick_time_separate_from_freshness():
    q = snap(bid_last_tick_monotonic=104.0, ask_last_tick_monotonic=104.0)
    r = entry_quote_check(q, now_monotonic=104.0, max_age_seconds=3)
    assert r.reasons == ("BID_STALE", "ASK_STALE")  # last-tick time is never freshness


# --- note (d): invariants over the extended fact space -------------------------------------------
_SPACE = itertools.product(
    [None, 1, 2, 3, 4, 7],  # latest reported type
    [(), (1,), (3,), (2,), (1, 3)],  # earlier delivered types (prefix of the history)
    [False, True],  # delayed ticks
    [0, 1, 2, 3],  # bid changes
    [0, 2],  # ask changes
    [None, 0.5, 5.0, 6.0],  # change age (both sides)
    [1, 2, 3, 4],  # requested type
    [(), (10167,), (2104,)],  # notices
    [None, 354],
    [False, True],  # stream failed
    [(10.0, 10.02), (NAN, 10.02), (None, None)])


def _live_allowed(mdt, hist, delayed, bu, au, age, req, notices, unav, failed, bid, ask):
    if delayed or unav is not None or failed or 10167 in notices:
        return False
    if any(t in (2, 3, 4) for t in hist):
        return False
    if mdt == 1:
        return True
    if mdt is None:
        return (req in (1, 3) and bu >= 2 and au >= 2 and age is not None and 0 <= age <= 5.0)
    return False


def test_exhaustive_live_only_with_evidence_and_bad_news_never_helps():
    n = 0
    for mdt, prefix, delayed, bu, au, age, req, notices, unav, failed, (bid, ask) in _SPACE:
        hist = prefix + ((mdt,) if mdt is not None else ())
        if mdt is None and prefix:
            continue  # a type once delivered stays reported
        kw = {"bid": bid, "ask": ask, "last": None, "market_data_type": mdt,
              "delayed_ticks_seen": delayed, "live_ticks_seen": not delayed or bu > 0,
              "bid_updates": bu, "ask_updates": au, "notices": notices,
              "unavailable_code": unav, "stream_failed": failed,
              "requested_market_data_type": req, "bid_change_age_seconds": age,
              "ask_change_age_seconds": age, "market_data_types_seen": hist}
        c = classify_quote(**kw)
        n += 1
        assert c.reason in QUOTE_REASONS
        if c.quality is L:
            assert _live_allowed(mdt, hist, delayed, bu, au, age, req, notices, unav, failed,
                                 bid, ask), kw
        worse = [{"notices": (*notices, 2186)}, {"unavailable_code": 354},
                 {"stream_failed": True}, {"delayed_ticks_seen": True},
                 {"market_data_types_seen": (*hist, 3)} if mdt is not None else {},
                 {"requested_market_data_type": 2} if mdt is None else {},
                 {"bid_change_age_seconds": 60.0} if mdt is None else {},
                 {"bid_updates": min(bu, 1)} if mdt is None else {}]
        for w in worse:
            c2 = classify_quote(**{**kw, **w})
            if c.quality is not L:
                assert c2.quality is not L, (kw, w)  # never upgraded to LIVE
            if c.quality is U:
                assert c2.quality is U, (kw, w)  # UNAVAILABLE is absorbing
    assert n > 50000

