"""Shadow mode, part 1: the pure mapping from the read-only adapter's ``QuoteSnapshot`` (a held
stream's ``latest()``) to the execution core's ``BrokerQuote`` (SDK-free; runs in CI).

The property that matters: the gateway's anchored-entry rule (``validator.anchor_quote_problem``)
refuses EVERY quote the adapter's own entry check (``models.entry_quote_check``) refuses, over the
whole fact space ``classify_quote`` decides on. Invented prices only."""
from __future__ import annotations

import itertools
import math
import random
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from tradingbot.execution.broker import ContractRef, QuoteQuality
from tradingbot.execution.shadow_quotes import (
    OPENING_PROBLEMS,
    SHADOW_QUOTE_PROBLEMS,
    map_for_simulator,
    map_snapshot,
    opening_problem,
    unavailable_quote,
)
from tradingbot.ibkr.models import (
    UNREPORTED_LIVE_WINDOW_SECONDS,
    QuoteSnapshot,
    classify_quote,
    entry_quote_check,
)
from tradingbot.ibkr.models import QuoteQuality as AdapterQuality
from tradingbot.models import AssetClass
from tradingbot.validator import anchor_quote_problem

AT = datetime(2026, 6, 1, 14, 0, tzinfo=UTC)  # the service clock at the read (invented)
OBSERVED = 5000.0  # the adapter's monotonic clock when it built the snapshot
TLT = ContractRef(symbol="QBND", asset_class=AssetClass.ETF, con_id=4711,
                  min_tick=Decimal("0.01"))  # invented ETF


def snap(*, bid=88.40, ask=88.42, last=88.41, mdt=1, history=None, delayed=False, live=True,
         bid_updates=3, ask_updates=3, bid_price_changes=2, ask_price_changes=2, notices=(),
         unavailable=None, failed=False, requested=3, bid_age=0.5, ask_age=0.5,
         bid_pc_age=0.5, ask_pc_age=0.5, bid_class="LIVE", ask_class="LIVE", halted=None,
         tick_flags=(), observed=OBSERVED, last_trade_time=None) -> QuoteSnapshot:
    """A snapshot exactly as ``IbkrSession._build_quote`` assembles one: label, reason and flags
    from ``classify_quote`` over the facts, then the per-side evidence."""
    def mono(age):
        return None if age is None else observed - age

    history = tuple(history if history is not None else ([] if mdt is None else [mdt]))
    v = classify_quote(
        bid=bid, ask=ask, last=last, market_data_type=mdt, delayed_ticks_seen=delayed,
        live_ticks_seen=live, bid_updates=bid_updates, ask_updates=ask_updates,
        notices=notices, unavailable_code=unavailable, stream_failed=failed,
        requested_market_data_type=requested,
        bid_change_age_seconds=bid_age if bid_updates else None,
        ask_change_age_seconds=ask_age if ask_updates else None,
        market_data_types_seen=history, bid_price_changes=bid_price_changes,
        ask_price_changes=ask_price_changes, bid_price_change_age_seconds=bid_pc_age,
        ask_price_change_age_seconds=ask_pc_age)
    flags = tuple(v.flags) + tuple(tick_flags)
    if halted is not None and halted != 0:
        flags += ("HALTED",)
    return QuoteSnapshot(
        con_id=4711, symbol="QBND", bid=bid, bid_size=Decimal(100), ask=ask,
        ask_size=Decimal(100), last=last, last_size=None, market_data_type=mdt,
        market_data_type_name="LIVE", requested_market_data_type=requested,
        received_at="2026-06-01T13:59:59.000+00:00", last_trade_time=last_trade_time,
        quality=v.quality, flags=flags, notices=tuple(notices), unavailable_code=unavailable,
        route="held_stream", bid_at="2001-01-01T00:00:00.000+00:00",  # wall stamps are ignored
        ask_at="2099-01-01T00:00:00.000+00:00", bid_at_monotonic=mono(bid_age),
        ask_at_monotonic=mono(ask_age), bid_updates=bid_updates, ask_updates=ask_updates,
        bid_tick_class=bid_class, ask_tick_class=ask_class, quality_reason=v.reason,
        observed_at_monotonic=observed, halted=halted,
        bid_price_change_monotonic=mono(bid_pc_age), ask_price_change_monotonic=mono(ask_pc_age))


def gateway(q, max_age=3.0, at=AT):
    return anchor_quote_problem(q.bid, q.ask, q.bid_at, q.ask_at, q.quality, at, max_age,
                                TLT.min_tick)


# ------------------------------------------------------------------ the mapping, field by field
def test_a_fresh_live_quote_maps_one_to_one_and_passes_both_rules():
    s = snap(bid_age=1.25, ask_age=2.5)
    q, ev = map_snapshot(s, TLT, at=AT)
    assert (q.bid, q.ask, q.last) == (Decimal("88.40"), Decimal("88.42"), Decimal("88.41"))
    # received_at is the older side's time (review B2): the quote is as old as its staler side.
    assert q.quality is QuoteQuality.LIVE and q.received_at == q.ask_at and q.contract == TLT
    # Each side's time is the read time minus the age the ADAPTER measured on its monotonic clock.
    assert q.bid_at == AT - timedelta(seconds=1.25) and q.ask_at == AT - timedelta(seconds=2.5)
    assert entry_quote_check(s, now_monotonic=OBSERVED, max_age_seconds=3.0).usable
    assert gateway(q) == []
    assert (ev.label, ev.adapter_label, ev.reason) == ("LIVE", "LIVE", "REPORTED_LIVE")
    assert (ev.bid_age_seconds, ev.ask_age_seconds, ev.problems) == (1.25, 2.5, ())


@pytest.mark.parametrize("adapter,expected", [
    (AdapterQuality.LIVE, QuoteQuality.LIVE), (AdapterQuality.DELAYED, QuoteQuality.DELAYED),
    (AdapterQuality.FROZEN, QuoteQuality.FROZEN),
    (AdapterQuality.UNAVAILABLE, QuoteQuality.UNAVAILABLE),
    ("SOMETHING_NEW", QuoteQuality.UNAVAILABLE), (None, QuoteQuality.UNAVAILABLE),
])
def test_quality_maps_one_to_one_and_anything_unknown_is_the_worst_case(adapter, expected):
    s = replace(snap(), quality=adapter)
    q, ev = map_snapshot(s, TLT, at=AT)
    assert q.quality is expected
    if expected is QuoteQuality.UNAVAILABLE and adapter is not AdapterQuality.UNAVAILABLE:
        assert "QUALITY_UNKNOWN" in ev.problems


def test_wall_clock_stamps_of_the_adapter_are_never_used():
    """The snapshot's ``bid_at`` / ``ask_at`` strings are the adapter's wall clock at the tick; a
    wall clock that is wrong (2001, 2099 here) changes nothing: only monotonic ages count."""
    s = snap(bid_age=0.75, ask_age=0.25)
    q, _ = map_snapshot(s, TLT, at=AT)
    assert q.bid_at == AT - timedelta(seconds=0.75) and q.ask_at == AT - timedelta(seconds=0.25)


def test_the_service_clock_anchor_is_the_only_wall_time_and_ages_are_preserved_for_any_anchor():
    for at in (AT, AT - timedelta(days=400), AT + timedelta(hours=7)):
        q, _ = map_snapshot(snap(bid_age=2.0, ask_age=3.0), TLT, at=at)
        assert (at - q.bid_at).total_seconds() == 2.0
        assert (at - q.ask_at).total_seconds() == 3.0
        assert gateway(q, at=at) == []
        assert gateway(q, max_age=2.9, at=at) == ["ENTRY_ANCHOR_QUOTE_SIDE_STALE", "SIDE:ASK",
                                                  "MAX_AGE:2.9"]


def test_ages_are_rounded_up_to_the_microsecond_never_down():
    """timedelta has microsecond resolution; 3.0000004 s must not become 3.000000 s and pass a
    3 s limit the adapter refuses."""
    s = snap(bid_age=3.0000004, ask_age=1.0)
    assert entry_quote_check(s, now_monotonic=OBSERVED, max_age_seconds=3.0).reasons == (
        "BID_STALE",)
    q, _ = map_snapshot(s, TLT, at=AT)
    assert (AT - q.bid_at) == timedelta(seconds=3, microseconds=1)
    assert gateway(q)[0] == "ENTRY_ANCHOR_QUOTE_SIDE_STALE"
    # The stamp one float below 4997: the age is the smallest float step above 3 s.
    tiny = replace(snap(), bid_at_monotonic=math.nextafter(OBSERVED - 3.0, 0.0))
    assert "BID_STALE" in entry_quote_check(tiny, now_monotonic=OBSERVED,
                                            max_age_seconds=3.0).reasons
    assert gateway(map_snapshot(tiny, TLT, at=AT)[0])[0] == "ENTRY_ANCHOR_QUOTE_SIDE_STALE"


def test_a_side_time_after_the_observation_is_unknown_not_future_tolerated():
    """The gateway tolerates 0.5 s in the future; the adapter tolerates none. The mapping drops
    the time, so the gateway holds with SIDE_TIME_UNKNOWN."""
    s = snap(bid_age=-0.2)
    assert "BID_FROM_FUTURE" in entry_quote_check(s, now_monotonic=OBSERVED,
                                                  max_age_seconds=3.0).reasons
    q, ev = map_snapshot(s, TLT, at=AT)
    assert q.bid_at is None and "BID_FROM_FUTURE" in ev.problems
    assert q.quality is QuoteQuality.UNAVAILABLE and "SIDE_TIME_UNKNOWN" in ev.problems
    assert gateway(q)[0] == "ENTRY_ANCHOR_QUOTE_NOT_LIVE"


@pytest.mark.parametrize("side", ["BID", "ASK"])
@pytest.mark.parametrize("value,code", [(None, "MISSING"), (-1.0, "NON_POSITIVE"),
                                        (0.0, "NON_POSITIVE"), (math.nan, "NOT_FINITE"),
                                        (math.inf, "NOT_FINITE")])
def test_a_placeholder_or_non_finite_side_never_arrives_as_a_usable_side(side, value, code):
    s = snap(**{side.lower(): value})
    q, ev = map_snapshot(s, TLT, at=AT)
    assert getattr(q, side.lower()) is None and getattr(q, f"{side.lower()}_at") is None
    assert f"{side}_{code}" in ev.problems
    assert gateway(q)[0] in ("ENTRY_ANCHOR_QUOTE_NOT_LIVE", "ENTRY_ANCHOR_QUOTE_SIDE_MISSING")


@pytest.mark.parametrize("side", ["BID", "ASK"])
def test_an_off_tick_flag_removes_the_side_until_the_stream_is_reopened(side):
    s = snap(tick_flags=(f"{side}_OFF_TICK_TICK",))
    q, ev = map_snapshot(s, TLT, at=AT)
    assert getattr(q, side.lower()) is None and f"{side}_OFF_TICK" in ev.problems
    assert gateway(q)[0] == "ENTRY_ANCHOR_QUOTE_NOT_LIVE"  # no side time: UNAVAILABLE (B2)
    assert getattr(map_for_simulator(s, TLT, at=AT), side.lower()) is None


@pytest.mark.parametrize("halted", [1.0, 2.0, -1.0, math.nan])
def test_a_halted_series_has_no_usable_side(halted):
    q, ev = map_snapshot(snap(halted=halted), TLT, at=AT)
    assert q.bid is None and q.ask is None and "HALTED" in ev.problems
    assert gateway(q)[0] == "ENTRY_ANCHOR_QUOTE_NOT_LIVE"
    sim = map_for_simulator(snap(halted=halted), TLT, at=AT)
    assert sim.bid is None and sim.ask is None  # nothing fills on a halted series
    q, ev = map_snapshot(snap(halted=0.0), TLT, at=AT)  # 0: not halted
    assert q.bid is not None and "HALTED" not in ev.problems


def test_a_sticky_placeholder_flag_with_a_valid_current_value_keeps_the_side():
    """TWS sends -1 / 0 before the first quote. The adapter never counts a placeholder as an image
    or a change, so the side's time is that of a valid value; the CURRENT value must be valid."""
    q, _ = map_snapshot(snap(tick_flags=("BID_PLACEHOLDER_TICK", "ASK_NON_FINITE_TICK")), TLT,
                         at=AT)
    assert q.bid == Decimal("88.40") and q.ask == Decimal("88.42") and gateway(q) == []


@pytest.mark.parametrize("cls", ["DELAYED", "MIXED", None])
def test_a_side_with_non_live_ticks_has_no_time(cls):
    q, ev = map_snapshot(snap(ask_class=cls), TLT, at=AT)
    assert q.ask is None and q.ask_at is None and "ASK_TICKS_NOT_LIVE" in ev.problems
    assert q.quality is QuoteQuality.UNAVAILABLE and gateway(q)[0] == "ENTRY_ANCHOR_QUOTE_NOT_LIVE"
    assert map_for_simulator(snap(ask_class=cls), TLT, at=AT).ask == Decimal("88.42")


def test_unreported_live_evidence_is_rechecked_at_the_read_and_downgraded_when_expired():
    """Live with no reported type is evidence that expires (adapter rule 16 at the check's now). A
    size change keeps ``bid_at`` fresh while the price-change stamp, which the gateway cannot see,
    is older than the 5 s window: the mapped quote is FROZEN."""
    s = snap(mdt=None, bid_age=0.5, ask_age=0.5, bid_pc_age=0.5, ask_pc_age=0.5)
    assert s.quality is AdapterQuality.LIVE
    assert map_snapshot(s, TLT, at=AT)[0].quality is QuoteQuality.LIVE
    expired = snap(mdt=None, bid_age=0.5, ask_age=0.5, bid_pc_age=0.5, ask_pc_age=4.0)
    expired = replace(expired, ask_price_change_monotonic=OBSERVED
                      - UNREPORTED_LIVE_WINDOW_SECONDS - 0.001)
    assert "UNREPORTED_EVIDENCE_EXPIRED" in entry_quote_check(
        expired, now_monotonic=OBSERVED, max_age_seconds=3.0).reasons
    q, ev = map_snapshot(expired, TLT, at=AT)
    assert q.quality is QuoteQuality.FROZEN and "UNREPORTED_EVIDENCE_EXPIRED" in ev.problems
    assert gateway(q)[0] == "ENTRY_ANCHOR_QUOTE_NOT_LIVE"


def test_no_observation_time_means_no_side_time():
    s = replace(snap(), observed_at_monotonic=None)
    q, ev = map_snapshot(s, TLT, at=AT)
    assert q.bid_at is None and q.ask_at is None and "OBSERVED_TIME_UNKNOWN" in ev.problems


def test_the_series_update_time_is_the_last_trade_time_never_later_than_the_read():
    q, _ = map_snapshot(snap(last_trade_time="2026-06-01T13:59:30+00:00"), TLT, at=AT)
    assert q.updated_at == datetime(2026, 6, 1, 13, 59, 30, tzinfo=UTC)
    q, _ = map_snapshot(snap(last_trade_time="2026-06-01T14:00:30+00:00"), TLT, at=AT)
    assert q.updated_at == AT  # an exchange clock ahead of ours never makes it fresher than now
    for bad in (None, "unparsed:'x'", "2026-06-01T13:59:30"):  # absent, unparsed, naive
        assert map_snapshot(snap(last_trade_time=bad), TLT, at=AT)[0].updated_at is None


def test_the_unavailable_quote_carries_nothing():
    q = unavailable_quote(TLT, AT)
    assert q.quality is QuoteQuality.UNAVAILABLE and q.bid is None and q.ask is None
    assert q.bid_at is None and q.ask_at is None and q.last is None and q.received_at == AT


def test_evidence_carries_public_codes_and_numbers_only():
    _, ev = map_snapshot(snap(bid=None, tick_flags=("ASK_OFF_TICK_TICK",)), TLT, at=AT)
    d = ev.as_dict()
    assert set(d) == {"contract_key", "label", "adapter_label", "simulator_label", "reason",
                      "market_data_type", "requested_market_data_type", "bid_age_seconds",
                      "ask_age_seconds", "problems", "read_at"}
    assert set(ev.problems) <= set(SHADOW_QUOTE_PROBLEMS)
    for v in d.values():
        assert v is None or isinstance(v, (str, int, float, list))


# ------------------------------------------------------------------ the property
PRICES = [None, -1.0, 0.0, math.nan, math.inf, 88.40, 88.41, 88.415, 88.42, 88.43]
AGES = [None, -0.2, 0.0, 0.5, 2.999999, 3.0, 3.0000004, math.nextafter(3.0, 4.0), 4.9, 5.0,
        5.0001, 7.0, math.nan]
TYPES = [None, 1, 2, 3, 4, 7]
HISTORIES = [None, [1], [3], [1, 3, 1], [7, 1], [2]]
CLASSES = ["LIVE", "DELAYED", "MIXED", None]
TICK_FLAGS = [(), ("BID_OFF_TICK_TICK",), ("ASK_OFF_TICK_TICK",), ("BID_PLACEHOLDER_TICK",),
              ("ASK_NON_FINITE_TICK",)]
MAX_AGES = [3.0, 2.5, 5.0, 0.0, -1.0, math.nan]


def _facts(rng: random.Random) -> dict:
    mdt = rng.choice(TYPES)
    history = rng.choice(HISTORIES)
    if history is not None and mdt is not None and mdt not in history:
        history = [*history, mdt]
    return {
        "bid": rng.choice(PRICES), "ask": rng.choice(PRICES), "last": rng.choice(PRICES),
        "mdt": mdt, "history": history if mdt is not None else (history or []),
        "delayed": rng.random() < 0.2, "live": rng.random() < 0.9,
        "bid_updates": rng.choice([0, 1, 2, 5]), "ask_updates": rng.choice([0, 1, 2, 5]),
        "bid_price_changes": rng.choice([0, 1, 3]), "ask_price_changes": rng.choice([0, 1, 3]),
        "notices": rng.choice([(), (2186,), (10167,), (2104,)]),
        "unavailable": rng.choice([None, None, None, 354]), "failed": rng.random() < 0.1,
        "requested": rng.choice([3, 3, 1, 2]),
        "bid_age": rng.choice(AGES), "ask_age": rng.choice(AGES),
        "bid_pc_age": rng.choice(AGES), "ask_pc_age": rng.choice(AGES),
        "bid_class": rng.choice(CLASSES), "ask_class": rng.choice(CLASSES),
        "halted": rng.choice([None, None, 0.0, 1.0, -1.0, math.nan]),
        "tick_flags": rng.choice(TICK_FLAGS),
    }


def _assert_gateway_refuses_whatever_the_adapter_refuses(s: QuoteSnapshot, max_age) -> bool:
    """Returns whether the adapter accepted (for the coverage count). Also (review B2): the age
    the gateway computes from ``received_at`` is never less than the larger per-side age the
    adapter measured, and a quote it may decide on has both side times."""
    adapter = entry_quote_check(s, now_monotonic=OBSERVED, max_age_seconds=max_age)
    q, ev = map_snapshot(s, TLT, at=AT)
    problem = gateway(q, max_age=max_age)
    if not adapter.usable:
        assert problem, (adapter.reasons, s, q, ev)
    if q.quality is not QuoteQuality.UNAVAILABLE:
        assert q.bid_at is not None and q.ask_at is not None, (s, q)
        ages = [OBSERVED - t for t in (s.bid_at_monotonic, s.ask_at_monotonic)]
        assert (AT - q.received_at).total_seconds() >= max(ages), (s, q)
    return adapter.usable


def test_property_the_gateway_refuses_every_quote_the_adapter_refuses_random_facts():
    rng = random.Random(20261112)
    accepted = 0
    for i in range(60000):
        f = _facts(rng)
        if i % 2:  # half the samples perturb only three facts of a quote both rules accept
            f = {k: v for k, v in f.items() if k in rng.sample(sorted(f), 3)}
        accepted += _assert_gateway_refuses_whatever_the_adapter_refuses(
            snap(**f), rng.choice(MAX_AGES))
    assert accepted > 50  # the sample also covers quotes both rules accept


def test_property_exhaustive_over_the_freshness_and_validity_grid():
    """Every combination of the facts that decide freshness and validity, on reported-live and
    unreported-live quotes."""
    accepted = 0
    for mdt, bid, ask, bid_age, ask_age, pc_age, flags, halted, max_age in itertools.product(
            [1, None], [None, 0.0, math.nan, 88.40, 88.415, 88.42],
            [None, -1.0, math.inf, 88.40, 88.42, 88.43], AGES, AGES, [0.5, 4.9, 5.0001, None],
            TICK_FLAGS[:3], [None, 1.0], [3.0, 5.0]):
        s = snap(mdt=mdt, bid=bid, ask=ask, bid_age=bid_age, ask_age=ask_age, bid_pc_age=pc_age,
                 ask_pc_age=0.5, tick_flags=flags, halted=halted)
        accepted += _assert_gateway_refuses_whatever_the_adapter_refuses(s, max_age)
    assert accepted > 100


def test_property_every_classification_row_that_is_not_live_is_refused():
    """One quote per ``classify_quote`` row: only the two LIVE rows can ever anchor."""
    rows = [
        snap(unavailable=354), snap(failed=True), snap(bid=None, ask=None, last=None),
        snap(mdt=3), snap(mdt=4), snap(mdt=2), snap(delayed=True), snap(history=[3, 1]),
        snap(mdt=7), snap(live=False), snap(mdt=None, requested=2),
        snap(mdt=None, bid_updates=1), snap(mdt=None, bid_price_changes=0),
        snap(mdt=None, bid_age=5.5), snap(mdt=None, bid_pc_age=5.5),
        snap(notices=(2186,)),
    ]
    for s in rows:
        assert s.quality is not AdapterQuality.LIVE, s.quality_reason
        q, _ = map_snapshot(s, TLT, at=AT)
        assert gateway(q)[0] == "ENTRY_ANCHOR_QUOTE_NOT_LIVE", s.quality_reason
    for live in (snap(), snap(mdt=None)):
        assert live.quality is AdapterQuality.LIVE
        assert gateway(map_snapshot(live, TLT, at=AT)[0]) == []


# ------------------------------------------------------------------ review B2: received_at
def test_received_at_is_the_older_side_so_the_quote_age_limit_applies():
    q, _ = map_snapshot(snap(bid_age=1.0, ask_age=600.0), TLT, at=AT)
    assert q.received_at == q.ask_at == AT - timedelta(seconds=600)
    q, _ = map_snapshot(snap(bid_age=2.5, ask_age=0.25), TLT, at=AT)
    assert q.received_at == q.bid_at


@pytest.mark.parametrize("change", [
    {"bid_class": "DELAYED"}, {"ask_class": None}, {"bid_age": None}, {"ask_age": -0.5},
    {"observed": math.nan}])
def test_a_side_without_a_known_time_makes_the_quote_unavailable_for_the_execution_core(change):
    """Exactly how: the quote the gateway and the execution core see is UNAVAILABLE with no
    sides and no side times (so no entry, add or exit is priced on it); the evidence keeps the
    adapter's label and the code SIDE_TIME_UNKNOWN; the simulator still gets the real sides."""
    s = snap(**change)
    q, ev = map_snapshot(s, TLT, at=AT)
    assert q.quality is QuoteQuality.UNAVAILABLE and q.bid is None and q.ask is None
    assert q.bid_at is None and q.ask_at is None and "SIDE_TIME_UNKNOWN" in ev.problems
    assert ev.adapter_label == "LIVE" and ev.label == "UNAVAILABLE"
    sim = map_for_simulator(s, TLT, at=AT)
    assert sim.quality is QuoteQuality.LIVE and sim.bid == Decimal("88.40")


def test_a_real_delayed_quote_reaches_the_simulator_labelled_delayed():
    s = snap(mdt=3, delayed=True, live=False, bid_class="DELAYED", ask_class="DELAYED")
    q, ev = map_snapshot(s, TLT, at=AT)
    assert q.quality is QuoteQuality.UNAVAILABLE and ev.adapter_label == "DELAYED"
    assert ev.simulator_label == "DELAYED"
    sim = map_for_simulator(s, TLT, at=AT)
    assert sim.quality is QuoteQuality.DELAYED and (sim.bid, sim.ask) == (Decimal("88.40"),
                                                                         Decimal("88.42"))


def test_the_simulator_quote_keeps_the_value_rules_and_the_downgrade():
    assert map_for_simulator(snap(halted=1.0), TLT, at=AT).bid is None
    assert map_for_simulator(snap(tick_flags=("ASK_OFF_TICK_TICK",)), TLT, at=AT).ask is None
    expired = replace(snap(mdt=None), ask_price_change_monotonic=OBSERVED - 6.0)
    assert map_for_simulator(expired, TLT, at=AT).quality is QuoteQuality.FROZEN


# ------------------------------------------------------------------ review B1: opening orders
def test_an_opening_order_needs_a_live_quote_with_each_side_within_its_limit():
    q, _ = map_snapshot(snap(bid_age=2.0, ask_age=3.0), TLT, at=AT)
    assert opening_problem(q, AT, 3.0) is None
    assert opening_problem(q, AT, 2.9) == "SHADOW_OPENING_QUOTE_SIDE_STALE"
    assert opening_problem(q, AT + timedelta(seconds=0.2), 3.0) == \
        "SHADOW_OPENING_QUOTE_SIDE_STALE"
    for s, code in ((snap(mdt=3), "SHADOW_OPENING_QUOTE_NOT_LIVE"),
                    (snap(mdt=2), "SHADOW_OPENING_QUOTE_NOT_LIVE"),
                    (replace(snap(mdt=None), bid_price_change_monotonic=OBSERVED - 6.0),
                     "SHADOW_OPENING_QUOTE_NOT_LIVE"),
                    (snap(bid=None), "SHADOW_OPENING_QUOTE_NOT_LIVE"),
                    (snap(bid=88.42), "SHADOW_OPENING_QUOTE_LOCKED_OR_CROSSED")):
        assert opening_problem(map_snapshot(s, TLT, at=AT)[0], AT, 3.0) == code, s.quality_reason
    for bad in (None, 0, -1.0, math.nan, math.inf):
        assert opening_problem(q, AT, bad) == "SHADOW_OPENING_LIMIT_UNKNOWN"
    assert set(OPENING_PROBLEMS) == {"SHADOW_OPENING_QUOTE_NOT_LIVE",
                                     "SHADOW_OPENING_QUOTE_LOCKED_OR_CROSSED",
                                     "SHADOW_OPENING_QUOTE_SIDE_STALE",
                                     "SHADOW_OPENING_LIMIT_UNKNOWN"}


def test_property_an_opening_order_never_matches_where_the_adapter_refuses():
    """At the read, an opening order's test is at least as strict as entry_quote_check."""
    rng = random.Random(7)
    for _ in range(20000):
        s = snap(**_facts(rng))
        limit = rng.choice([3.0, 2.5, 5.0, 30.0])
        q, _ = map_snapshot(s, TLT, at=AT)
        if opening_problem(q, AT, limit) is None:
            assert entry_quote_check(s, now_monotonic=OBSERVED, max_age_seconds=limit).usable, s
