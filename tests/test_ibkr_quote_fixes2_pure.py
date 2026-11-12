"""Second review round for the held quote streams (N1, notes a, b, d). Pure: passes WITHOUT
ibapi (GitHub CI). Stream-level counterparts: test_ibkr_quote_fixes2_sdk.py."""
from __future__ import annotations

import itertools
from decimal import Decimal

import pytest

from tradingbot.ibkr.models import (
    ENTRY_QUOTE_REASONS,
    QUOTE_REASONS,
    UNREPORTED_LIVE_WINDOW_SECONDS,
    QuoteQuality,
    QuoteSnapshot,
    SideEvidence,
    classify_quote,
    entry_quote_check,
)

L, D, F, U = QuoteQuality.LIVE, QuoteQuality.DELAYED, QuoteQuality.FROZEN, QuoteQuality.UNAVAILABLE
W = UNREPORTED_LIVE_WINDOW_SECONDS


# --- N1: size flicker is not a price change; off-tick prices never count -----------------------
def test_size_flicker_counts_changes_but_no_price_change():
    s = SideEvidence()
    s.observe("price", 2000.0, False, mono=1.0)
    s.observe("size", Decimal(100), False, mono=1.0)
    s.observe("size", Decimal(200), False, mono=1.5)
    s.observe("size", Decimal(100), False, mono=2.0)
    assert (s.changes, s.price_changes, s.price_change_mono, s.fresh_mono) == (2, 0, None, 2.0)
    s.observe("price", 2000.01, False, mono=3.0)
    assert (s.changes, s.price_changes, s.price_change_mono) == (3, 1, 3.0)


def test_off_tick_price_is_flagged_and_never_a_change():
    s = SideEvidence(min_tick=0.01)
    s.observe("price", 2000.0, False, mono=1.0)
    s.observe("price", 2000.001, False, mono=2.0)
    s.observe("price", 2000.002, False, mono=3.0)
    assert s.off_tick_seen and (s.changes, s.price_changes, s.price, s.fresh_mono) == (
        0, 0, 2000.0, 1.0)
    s.observe("price", 2000.03, False, mono=4.0)  # on the tick: a real change
    assert (s.changes, s.price_changes) == (1, 1)
    s = SideEvidence(min_tick=0.01)
    s.observe("price", 2000.005, False, mono=1.0)  # an off-tick first price is no image either
    assert not s.image and s.off_tick_seen


@pytest.mark.parametrize("tick", [None, 0.0, -1.0, float("nan")])
def test_unknown_min_tick_leaves_tick_checking_to_the_gateway(tick):
    s = SideEvidence(min_tick=tick)
    s.observe("price", 2000.0, False, mono=1.0)
    s.observe("price", 2000.001, False, mono=2.0)
    assert s.price_changes == 1 and not s.off_tick_seen


# --- N1: the unreported rule needs a recent PRICE change on each side ------------------------------
def cq(**kw):
    base = {"bid": 10.0, "ask": 10.02, "last": None, "market_data_type": None,
            "delayed_ticks_seen": False, "live_ticks_seen": True, "bid_updates": 2,
            "ask_updates": 2, "requested_market_data_type": 3, "bid_change_age_seconds": 0.5,
            "ask_change_age_seconds": 0.5, "bid_price_changes": 1, "ask_price_changes": 1,
            "bid_price_change_age_seconds": 0.5, "ask_price_change_age_seconds": 0.5}
    base.update(kw)
    return classify_quote(**base)


def test_unreported_live_with_recent_price_changes():
    assert (cq().quality, cq().reason) == (L, "TYPE_UNREPORTED_UPDATES_SEEN")


@pytest.mark.parametrize(("b", "a"), [(0, 0), (0, 1), (1, 0)])
def test_unreported_without_a_price_change_on_each_side_is_not_live(b, a):
    c = cq(bid_price_changes=b, ask_price_changes=a)
    assert (c.quality, c.reason) == (F, "TYPE_UNREPORTED_NO_PRICE_CHANGE_YET")


@pytest.mark.parametrize(("ba", "aa"), [(W + 0.001, 0.1), (0.1, 60.0), (None, 0.1),
                                        (0.1, float("nan")), (-1.0, 0.1)])
def test_unreported_price_change_must_be_within_the_window(ba, aa):
    c = cq(bid_price_change_age_seconds=ba, ask_price_change_age_seconds=aa)
    assert (c.quality, c.reason) == (F, "TYPE_UNREPORTED_PRICE_CHANGE_TOO_OLD")


def test_no_updates_yet_and_no_price_change_yet_are_distinct():
    assert cq(bid_updates=1).reason == "TYPE_UNREPORTED_NO_UPDATES_YET"
    assert cq(bid_price_changes=0).reason == "TYPE_UNREPORTED_NO_PRICE_CHANGE_YET"
    assert {"TYPE_UNREPORTED_NO_PRICE_CHANGE_YET",
            "TYPE_UNREPORTED_PRICE_CHANGE_TOO_OLD"} <= set(QUOTE_REASONS)


# --- note a: any reported type other than 1 is sticky ------------------------------------------
@pytest.mark.parametrize(("history", "quality"), [((7, 1), F), ((1, 7, 1), F), ((0, 1), F),
                                                  ((1, 3, 1), D), ((2, 1), F)])
def test_any_non_live_type_in_the_history_is_sticky(history, quality):
    c = cq(market_data_type=1, market_data_types_seen=history)
    assert (c.quality, c.reason) == (quality, "REPORTED_TYPE_HISTORY_NOT_LIVE")


# --- note b: the entry check re-applies the unreported window at its own "now" ----------------
def snap(**kw) -> QuoteSnapshot:
    base = {"con_id": 1, "symbol": "X", "bid": 10.0, "bid_size": None, "ask": 10.02,
            "ask_size": None, "last": None, "last_size": None, "market_data_type": None,
            "market_data_type_name": "UNREPORTED", "requested_market_data_type": 3,
            "received_at": "2026-11-12T10:00:00.000+00:00", "last_trade_time": None,
            "quality": L, "flags": (), "route": "held_stream", "bid_at_monotonic": 100.0,
            "ask_at_monotonic": 100.0, "bid_price_change_monotonic": 100.0,
            "ask_price_change_monotonic": 100.0, "bid_tick_class": "LIVE",
            "ask_tick_class": "LIVE", "quality_reason": "TYPE_UNREPORTED_UPDATES_SEEN",
            "observed_at_monotonic": 100.0}
    base.update(kw)
    return QuoteSnapshot(**base)


def test_unreported_live_snapshot_expires_with_the_window_even_with_a_long_max_age():
    assert entry_quote_check(snap(), now_monotonic=100.0 + W, max_age_seconds=10).usable
    r = entry_quote_check(snap(), now_monotonic=100.0 + W + 0.5, max_age_seconds=10)
    assert r.reasons == ("UNREPORTED_EVIDENCE_EXPIRED",)
    assert "UNREPORTED_EVIDENCE_EXPIRED" in ENTRY_QUOTE_REASONS


@pytest.mark.parametrize("kw", [{"bid_price_change_monotonic": 90.0},
                                {"ask_price_change_monotonic": None},
                                {"ask_at_monotonic": 94.0, "ask_price_change_monotonic": 94.0}])
def test_unreported_price_change_evidence_is_rechecked(kw):
    r = entry_quote_check(snap(**kw), now_monotonic=100.5, max_age_seconds=10)
    assert "UNREPORTED_EVIDENCE_EXPIRED" in r.reasons and not r.usable


def test_reported_live_snapshot_is_not_subject_to_the_unreported_window():
    q = snap(market_data_type=1, market_data_type_name="LIVE", quality_reason="REPORTED_LIVE",
             bid_price_change_monotonic=None, ask_price_change_monotonic=None)
    assert entry_quote_check(q, now_monotonic=100.0 + W + 1, max_age_seconds=10).usable


# --- note d: the exhaustive space reaches the unreported-LIVE branch ---------------------------
def test_exhaustive_unreported_live_requires_recent_price_changes_on_both_sides():
    space = itertools.product([1, 2, 3, 4, None], [0, 1, 2, 3], [0, 1, 2], [0, 1], [0, 1],
                              [None, 0.5, W, W + 0.5], [None, 0.5, W, W + 0.5],
                              [False, True], [(), (10167,)])
    lives = 0
    for req, bu, au, bp, ap, age, page, delayed, notices in space:
        kw = {"requested_market_data_type": req, "bid_updates": bu, "ask_updates": au,
              "bid_price_changes": bp, "ask_price_changes": ap, "bid_change_age_seconds": age,
              "ask_change_age_seconds": age, "bid_price_change_age_seconds": page,
              "ask_price_change_age_seconds": page, "delayed_ticks_seen": delayed,
              "notices": notices}
        c = cq(**kw)
        assert c.reason in QUOTE_REASONS
        if c.quality is L:
            lives += 1
            assert req in (1, 3) and bu >= 2 and au >= 2 and bp >= 1 and ap >= 1
            assert age is not None and age <= W and page is not None and page <= W
            assert not delayed and not notices
    assert lives > 0  # the LIVE branch is reached, not only refused
