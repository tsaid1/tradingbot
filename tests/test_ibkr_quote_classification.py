"""Quote classification (live / delayed / frozen), per-side evidence and the entry freshness
helper. Pure: must pass WITHOUT ibapi installed (GitHub CI)."""
from __future__ import annotations

import ast
import dataclasses
import itertools
import json
from pathlib import Path

import pytest

from tradingbot.ibkr.models import (
    ENTITLEMENT_NOTICE_CODES,
    ENTRY_QUOTE_REASONS,
    QUOTE_REASONS,
    QuoteQuality,
    QuoteSnapshot,
    assess_quote,
    assess_quote_attempts,
    classify_quote,
    entry_quote_check,
    to_jsonable,
)

ROOT = Path(__file__).resolve().parents[1]
L, D, F, U = QuoteQuality.LIVE, QuoteQuality.DELAYED, QuoteQuality.FROZEN, QuoteQuality.UNAVAILABLE


def cq(**kw):
    base = {"bid": 10.0, "ask": 10.02, "last": 10.01, "market_data_type": 1,
            "delayed_ticks_seen": False, "live_ticks_seen": True, "bid_updates": 0,
            "ask_updates": 0, "notices": (), "unavailable_code": None, "stream_failed": False,
            "requested_market_data_type": 3, "bid_change_age_seconds": 0.5,
            "ask_change_age_seconds": 0.5}
    base.update(kw)
    return classify_quote(**base)


# --- the table, row by row ------------------------------------------------------------------------
@pytest.mark.parametrize(("facts", "quality", "reason"), [
    ({"unavailable_code": 354}, U, "NOT_ENTITLED"),
    ({"unavailable_code": 354, "bid_updates": 5, "ask_updates": 5}, U, "NOT_ENTITLED"),
    ({"stream_failed": True}, U, "STREAM_FAILED"),
    ({"bid": None, "ask": None, "last": None}, U, "NO_PRICE"),
    ({"bid": 0.0, "ask": -1.0, "last": None}, U, "NO_PRICE"),
    ({"market_data_type": 3, "delayed_ticks_seen": True, "live_ticks_seen": False}, D,
     "REPORTED_DELAYED"),
    ({"market_data_type": 3}, D, "REPORTED_DELAYED"),  # type 3 with plain ticks: still delayed
    ({"market_data_type": 4, "delayed_ticks_seen": True, "live_ticks_seen": False}, F,
     "REPORTED_DELAYED_FROZEN"),
    ({"market_data_type": 2}, F, "REPORTED_FROZEN"),
    ({"market_data_type": 2, "bid_updates": 9, "ask_updates": 9}, F, "REPORTED_FROZEN"),
    ({"delayed_ticks_seen": True}, D, "DELAYED_TICKS"),  # type 1 + delayed ids
    ({"market_data_type": None, "delayed_ticks_seen": True, "live_ticks_seen": False}, D,
     "DELAYED_TICKS"),
    ({"market_data_type": None, "delayed_ticks_seen": True, "live_ticks_seen": False,
      "bid_updates": 30, "ask_updates": 30}, D, "DELAYED_TICKS"),
    ({"market_data_type": 7, "delayed_ticks_seen": True}, D, "DELAYED_TICKS"),
    ({"market_data_type": 7}, F, "UNKNOWN_MARKET_DATA_TYPE"),
    ({"live_ticks_seen": False}, F, "REPORTED_LIVE_NO_LIVE_TICKS"),
    ({}, L, "REPORTED_LIVE"),  # type 1: the initial image counts
    ({"market_data_type": None, "bid_updates": 31, "ask_updates": 35}, L,
     "TYPE_UNREPORTED_UPDATES_SEEN"),  # the spot-gold case
    ({"market_data_type": None}, F, "TYPE_UNREPORTED_NO_UPDATES_YET"),  # its mirror
    ({"market_data_type": None, "bid_updates": 4, "ask_updates": 1}, F,
     "TYPE_UNREPORTED_NO_UPDATES_YET"),
    ({"market_data_type": None, "ask_updates": 4}, F, "TYPE_UNREPORTED_NO_UPDATES_YET"),
    ({"market_data_type": None, "bid_updates": 31, "ask_updates": 35,
      "bid_change_age_seconds": 9.0}, F, "TYPE_UNREPORTED_UPDATES_TOO_OLD"),
    ({"market_data_type": None, "bid_updates": 31, "ask_updates": 35,
      "requested_market_data_type": 2}, F, "TYPE_UNREPORTED_REQUESTED_NOT_ELIGIBLE"),
    ({"market_data_types_seen": (3, 1)}, D, "REPORTED_TYPE_HISTORY_NOT_LIVE"),
    ({"notices": (10167,)}, D, "ENTITLEMENT_NOTICE"),
    ({"notices": (2186,)}, D, "ENTITLEMENT_NOTICE"),
    ({"market_data_type": None, "bid_updates": 2, "ask_updates": 2, "notices": (10090,)}, D,
     "ENTITLEMENT_NOTICE"),
    ({"notices": (2104, 2106)}, L, "REPORTED_LIVE"),  # farm notices are not entitlement notices
    ({"market_data_type": 3, "notices": (2186,)}, D, "REPORTED_DELAYED"),
])
def test_classification_table(facts, quality, reason):
    c = cq(**facts)
    assert (c.quality, c.reason) == (quality, reason)


def test_unreported_boundary_needs_two_changes_on_each_side():
    for b, a in ((0, 0), (1, 1), (2, 1), (1, 2)):
        assert cq(market_data_type=None, bid_updates=b, ask_updates=a).quality is F
    c = cq(market_data_type=None, bid_updates=2, ask_updates=2)
    assert (c.quality, c.reason) == (L, "TYPE_UNREPORTED_UPDATES_SEEN")


def test_usability_flags_locked_and_crossed_distinguished():
    assert "LOCKED" in cq(bid=10.0, ask=10.0).flags and "CROSSED" not in cq(bid=10.0,
                                                                           ask=10.0).flags
    assert "CROSSED" in cq(bid=10.05, ask=10.0).flags and "LOCKED" not in cq(bid=10.05,
                                                                            ask=10.0).flags
    assert {"MISSING_BID", "NON_POSITIVE_ASK"} <= set(cq(bid=None, ask=0.0).flags)
    # the label says what the data is; usability is separate (the entry check refuses these)
    assert cq(bid=10.05, ask=10.0).quality is L


def test_reason_flags_are_explicit():
    assert "TYPE_UNREPORTED_NO_UPDATES_YET" in cq(market_data_type=None).flags
    assert "ENTITLEMENT_NOTICE" in cq(notices=(10167,)).flags
    assert "STREAM_FAILED" in cq(stream_failed=True).flags
    assert "TYPE_TICK_CONFLICT" in cq(delayed_ticks_seen=True).flags


# --- exhaustive invariants over the whole fact space ---------------------------------------------
_PRICES = [(10.0, 10.02), (10.0, 10.0), (10.05, 10.0), (None, 10.02), (0.0, None), (None, None)]
_SPACE = itertools.product(
    [None, 1, 2, 3, 4, 7], [False, True], [False, True], [0, 1, 3], [0, 1, 3],
    [(), (2104,), (10167,), (2186,), (10090,)], [None, 354], [False, True], _PRICES,
    [None, 10.01])


def _rank(q: QuoteQuality) -> int:  # higher is better
    return {U: 0, F: 1, D: 1, L: 2}[q]


def test_exhaustive_never_live_without_evidence_and_never_improved_by_bad_news():
    n = 0
    for mdt, delayed, live, bu, au, notices, unav, failed, (bid, ask), last in _SPACE:
        kw = {"bid": bid, "ask": ask, "last": last, "market_data_type": mdt,
              "delayed_ticks_seen": delayed, "live_ticks_seen": live, "bid_updates": bu,
              "ask_updates": au, "notices": notices, "unavailable_code": unav,
              "stream_failed": failed}
        c = classify_quote(**kw)
        n += 1
        assert c.reason in QUOTE_REASONS
        if c.quality is L:
            assert not delayed and unav is None and not failed
            assert not ENTITLEMENT_NOTICE_CODES.intersection(notices)
            assert (mdt == 1 and live) or (mdt is None and bu >= 1 and au >= 1)
        if mdt in (2, 3, 4) or delayed:
            assert c.quality is not L
        if mdt == 2 and unav is None and not failed and c.quality is not U:
            assert c.quality is F
        # bad news never makes the label better
        for worse in ({"notices": (*notices, 10167)}, {"unavailable_code": 354},
                      {"stream_failed": True}, {"delayed_ticks_seen": True}):
            assert _rank(classify_quote(**{**kw, **worse}).quality) <= _rank(c.quality)
        # assess_quote (no update counts, no notices) is never more optimistic
        legacy, _ = assess_quote(bid=bid, ask=ask, last=last, market_data_type=mdt,
                                 delayed_ticks_seen=delayed, live_ticks_seen=live,
                                 unavailable_code=unav)
        if not failed and not notices:
            assert _rank(legacy) <= _rank(c.quality)
    assert n > 20000


def test_reason_determines_quality():
    expected = {"NOT_ENTITLED": U, "STREAM_FAILED": U, "NO_PRICE": U, "REPORTED_DELAYED": D,
                "REPORTED_DELAYED_FROZEN": F, "REPORTED_FROZEN": F, "DELAYED_TICKS": D,
                "UNKNOWN_MARKET_DATA_TYPE": F, "REPORTED_LIVE_NO_LIVE_TICKS": F,
                "TYPE_UNREPORTED_NO_UPDATES_YET": F, "ENTITLEMENT_NOTICE": D,
                "REPORTED_LIVE": L, "TYPE_UNREPORTED_UPDATES_SEEN": L,
                "TYPE_UNREPORTED_REQUESTED_NOT_ELIGIBLE": F,
                "TYPE_UNREPORTED_UPDATES_TOO_OLD": F, "REPORTED_TYPE_HISTORY_NOT_LIVE": (D, F)}
    assert set(expected) == set(QUOTE_REASONS)
    for mdt, delayed, live, bu, au, notices, unav, failed, (bid, ask), last in _SPACE:
        c = classify_quote(bid=bid, ask=ask, last=last, market_data_type=mdt,
                           delayed_ticks_seen=delayed, live_ticks_seen=live, bid_updates=bu,
                           ask_updates=au, notices=notices, unavailable_code=unav,
                           stream_failed=failed)
        want = expected[c.reason]
        assert c.quality in (want if isinstance(want, tuple) else (want,))


def test_assess_quote_is_the_same_function_without_update_counts():
    assert assess_quote(bid=1.0, ask=1.1, last=None, market_data_type=None,
                        delayed_ticks_seen=False, live_ticks_seen=True)[0] is F
    assert assess_quote(bid=1.0, ask=1.1, last=None, market_data_type=1,
                        delayed_ticks_seen=False, live_ticks_seen=True) == (L, ())


# --- snapshots, the verdict and old reports ----------------------------------------------------------
def snap(**kw) -> QuoteSnapshot:
    base = {"con_id": 1, "symbol": "XAUUSD", "bid": 2000.0, "bid_size": None, "ask": 2000.5,
            "ask_size": None, "last": None, "last_size": None, "market_data_type": None,
            "market_data_type_name": "UNREPORTED", "requested_market_data_type": 3,
            "received_at": "2026-11-12T10:00:00.000+00:00", "last_trade_time": None,
            "quality": L, "flags": ("MARKET_DATA_TYPE_NOT_REPORTED",), "route": "stream",
            "tick_counts": {"BID": 31, "ASK": 35}, "bid_at_monotonic": 100.0,
            "ask_at_monotonic": 100.0, "bid_updates": 31, "ask_updates": 35,
            "bid_tick_class": "LIVE", "ask_tick_class": "LIVE",
            "quality_reason": "TYPE_UNREPORTED_UPDATES_SEEN", "observed_at_monotonic": 100.5}
    base.update(kw)
    return QuoteSnapshot(**base)


def test_gold_stream_sample_with_updates_passes_the_quote_verdict():
    status, reason = assess_quote_attempts((snap(),))
    assert status == "PASS" and "stream" in reason
    # unreported and unconfirmed (frozen image) stays out of the two-sided set, as before
    status, reason = assess_quote_attempts((snap(quality=F, bid_updates=0, ask_updates=0),))
    assert status == "PASS_WITH_WARNINGS" and "partial" in reason


def test_reports_written_before_the_new_fields_still_load():
    old = to_jsonable(snap())
    for k in ("bid_at", "ask_at", "bid_at_monotonic", "ask_at_monotonic", "bid_updates",
              "ask_updates", "bid_tick_class", "ask_tick_class", "quality_reason",
              "observed_at_monotonic"):
        old.pop(k)
    old["quality"] = QuoteQuality(old["quality"])
    old["flags"] = tuple(old["flags"])
    q = QuoteSnapshot(**old)
    assert (q.bid_updates, q.ask_updates, q.bid_at, q.quality_reason) == (0, 0, None, "")
    assert json.loads(json.dumps(to_jsonable(q)))["bid_updates"] == 0
    # ...and such a snapshot can never pass the entry check
    assert not entry_quote_check(q, now_monotonic=1.0, max_age_seconds=3).usable


# --- entry freshness helper -----------------------------------------------------------------------------
def test_entry_check_usable():
    r = entry_quote_check(snap(), now_monotonic=103.0, max_age_seconds=3.0)
    assert r.usable and r.reasons == () and (r.bid_age_seconds, r.ask_age_seconds) == (3.0, 3.0)


@pytest.mark.parametrize(("kw", "now", "reasons"), [
    ({"quality": D}, 100.5, ["NOT_LIVE"]),
    ({"quality": F}, 100.5, ["NOT_LIVE"]),
    ({"quality": U}, 100.5, ["NOT_LIVE"]),
    ({"bid": None}, 100.5, ["BID_MISSING"]),
    ({"ask": 0.0}, 100.5, ["ASK_NON_POSITIVE"]),
    ({"bid": 2000.5}, 100.5, ["LOCKED"]),
    ({"bid": 2001.0}, 100.5, ["CROSSED"]),
    ({"bid_tick_class": "DELAYED"}, 100.5, ["BID_TICKS_NOT_LIVE"]),
    ({"ask_tick_class": "MIXED"}, 100.5, ["ASK_TICKS_NOT_LIVE"]),
    ({"ask_tick_class": None}, 100.5, ["ASK_TICKS_NOT_LIVE"]),
    ({"bid_at_monotonic": None}, 100.5, ["BID_TIME_UNKNOWN"]),
    ({"ask_at_monotonic": float("nan")}, 100.5, ["ASK_TIME_UNKNOWN"]),
    ({}, 103.001, ["BID_STALE", "ASK_STALE"]),
    ({"bid_at_monotonic": 102.0}, 103.5, ["ASK_STALE"]),  # one side stale, the other fresh
    ({"bid_at_monotonic": 101.0}, 100.5, ["BID_FROM_FUTURE"]),
])
def test_entry_check_reasons(kw, now, reasons):
    r = entry_quote_check(snap(**kw), now_monotonic=now, max_age_seconds=3.0)
    assert not r.usable and list(r.reasons) == reasons
    assert set(r.reasons) <= set(ENTRY_QUOTE_REASONS)


@pytest.mark.parametrize("bad", [0, -1, float("nan"), float("inf"), True, "3", None])
def test_entry_check_invalid_limit_or_now(bad):
    r = entry_quote_check(snap(), now_monotonic=100.5, max_age_seconds=bad)
    assert not r.usable and "INVALID_MAX_AGE" in r.reasons
    r = entry_quote_check(snap(), now_monotonic=bad if bad not in (0, -1) else float("nan"),
                          max_age_seconds=3.0)
    assert not r.usable and "INVALID_NOW" in r.reasons


def test_entry_check_lists_every_failure():
    r = entry_quote_check(snap(quality=D, bid=None, ask_at_monotonic=90.0), now_monotonic=100.5,
                          max_age_seconds=3)
    assert list(r.reasons) == ["NOT_LIVE", "BID_MISSING", "ASK_STALE"]


def test_snapshot_fields_are_additive_with_safe_defaults():
    new = {"bid_at", "ask_at", "bid_at_monotonic", "ask_at_monotonic", "bid_updates",
           "ask_updates", "bid_tick_class", "ask_tick_class", "quality_reason",
           "observed_at_monotonic"}
    fields = {f.name: f for f in dataclasses.fields(QuoteSnapshot)}
    assert new <= set(fields)
    assert all(fields[n].default is not dataclasses.MISSING for n in new)
    assert list(fields)[:15] == [  # the positional prefix existing callers rely on
        "con_id", "symbol", "bid", "bid_size", "ask", "ask_size", "last", "last_size",
        "market_data_type", "market_data_type_name", "requested_market_data_type",
        "received_at", "last_trade_time", "quality", "flags"]


def test_adapter_does_not_import_execution_runtime_or_dashboard():
    for path in (ROOT / "src" / "tradingbot" / "ibkr").glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            names = ([a.name for a in node.names] if isinstance(node, ast.Import)
                     else [node.module or ""] if isinstance(node, ast.ImportFrom) else [])
            for n in names:
                assert not n.startswith(("tradingbot.execution", "tradingbot.runtime",
                                         "dashboard")), (path.name, n)
