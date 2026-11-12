# ruff: noqa: F811 - the pytest fixtures are imported from the shadow and session SDK suites
"""Shadow mode, review fixes for PR #69 (fake TWS; invented posts and prices).

B1  an opening (not reduce-only) simulated order is matched only on a LIVE quote with each side
    within the limit of its approval; the shadow submit refuses otherwise (a broker-side
    rejection, nothing rests); a resting opening order is cancelled, journaled, when a read is not
    live and fresh; reduce-only fills keep the simulator's rules and say what they were computed
    from (SIMULATED_AT_LIVE_QUOTE / _DELAYED_ / _FROZEN_).
B2  a hung feed that still reports LIVE is stale: explicit and anchored entries hold.
S2  a stream whose contract has a working order or a position is never evicted.
S3  a simulated fill's quote evidence survives a crash between the fill and its journal row.
S4  a start-up failure after the session connected leaves nothing connected.
"""
from __future__ import annotations

import pytest

pytest.importorskip("ibapi")

from decimal import Decimal

from _ibkr_fake_tws import frame
from ibapi.message import IN
from ibapi.protobuf.MarketDataType_pb2 import MarketDataType
from runtime_harness import post
from test_ibkr_session_sdk import tws  # noqa: F401 - fixture
from test_shadow_mode_sdk import (  # noqa: F401 - fixtures
    ANCHORED,
    CLOSE,
    EXPLICIT,
    Shadow,
    _multi_symbol,
    assert_only_read_only_messages,
    fast_session,
    mono,
    price,
    run_until_processed,
    shadow,
)

from tradingbot.execution import shadow_broker as SB
from tradingbot.execution.broker import OrderIntent, OrderSide, OrderType
from tradingbot.execution.shadow_broker import FILL_LABELS
from tradingbot.models import AssetClass, Instrument
from tradingbot.runtime.config import ConfigError


def _fresh(sh):
    moves = iter(range(100000))

    def fresh():
        i = next(moves) % 2
        sh.live(88.40, 88.42 + i / 100)
        sh.live(88.40 - i / 100, 88.42)

    return fresh


def _delayed(sh, bid=88.38, ask=88.40):
    rid = sh.rid()
    sh.tick(-1, frame(IN.MARKET_DATA_TYPE, MarketDataType(reqId=rid, marketDataType=3)),
            price(rid, 66, bid), price(rid, 67, ask))


def _open(sh):
    return [a for a in sh.actions() if a["action"] == "OPEN"]


def _no_entry_fill(sh):
    return [f for f in sh.events("SHADOW_SIMULATED_FILL") if f["side"] == "BUY"] == []


# ------------------------------------------------------------------ B1
def test_b1a_a_resting_entry_is_cancelled_not_filled_when_the_stream_turns_delayed(
        shadow, tws, mono):
    """The reviewer's (a): the ask ticks up between the re-check and the submit read, so the
    anchored limit rests; next cycle the stream is DELAYED with a lower ask."""
    sh = shadow([post("1", 0, ANCHORED)])
    fresh = _fresh(sh)
    orig = sh.broker.submit

    def submit(order, children=()):
        sh.live(88.40, 88.45)  # a live tick between the core's re-check and the shadow read
        return orig(order, children)

    sh.broker.submit = submit
    run_until_processed(sh, 1, before=fresh)
    assert _no_entry_fill(sh)
    _delayed(sh)
    for _ in range(2):
        sh.step()
    assert _no_entry_fill(sh) and sh.sim().positions() == []
    [c] = sh.events("SHADOW_OPENING_ORDER_CANCELLED")
    assert c["code"] == "SHADOW_OPENING_QUOTE_NOT_LIVE" and c["quote"]["adapter_label"] == "DELAYED"
    assert not [o for o in sh.sim().open_orders() if o.parent_ref is None]
    sh.close()
    assert_only_read_only_messages(tws)


@pytest.mark.parametrize("case", ["delayed", "frozen", "unreported_expired", "one_side_stale"])
def test_b1b_the_submit_read_is_checked_and_a_bad_one_rejects_the_entry(shadow, tws, mono, case):
    """The reviewer's (b) and its siblings: the read inside the shadow submit is not live and
    fresh, so the bracket is rejected at the (simulated) broker: nothing rests, nothing fills."""
    sh = shadow([post("1", 0, ANCHORED)])
    fresh = _fresh(sh)
    orig = sh.broker.submit

    def submit(order, children=()):
        rid = sh.rid()
        if case == "delayed":
            _delayed(sh, 88.38, 88.41)
        elif case == "frozen":
            sh.tick(-1, frame(IN.MARKET_DATA_TYPE, MarketDataType(reqId=rid,
                                                                  marketDataType=2)))
        elif case == "unreported_expired":
            mono.t += 6.0  # the unreported-type evidence (if any) and both sides now too old
        else:
            mono.t += 2.0
            sh.tick(-1, price(rid, 1, 88.39))
            mono.t += 1.5  # the ask image is now 3.5 s old, the bid 1.5 s
        return orig(order, children)

    sh.broker.submit = submit
    run_until_processed(sh, 1, before=fresh)
    assert _no_entry_fill(sh) and sh.sim().positions() == []
    [a] = _open(sh)
    assert a["status"] != "EXECUTED", (a["status"], a["reasons"])
    [r] = sh.events("SHADOW_OPENING_ORDER_REJECTED")
    assert r["code"] in ("SHADOW_OPENING_QUOTE_NOT_LIVE", "SHADOW_OPENING_QUOTE_SIDE_STALE")
    assert all(o.status.value == "Rejected" for o in sh.sim().all_orders())
    sh.close()
    assert_only_read_only_messages(tws)


def test_b1b_unreported_type_evidence_expired_at_submit_rejects(shadow, mono):
    """Unreported type: LIVE only while both sides keep changing; it expires before the submit."""
    sh = shadow([post("1", 0, ANCHORED)])
    moves = iter(range(100000))

    def fresh_unreported():
        i = next(moves) % 2
        sh.live(88.40, 88.42 + i / 100, mdt=None)
        sh.live(88.40 - i / 100, 88.42, mdt=None)

    for _ in range(3):
        fresh_unreported()
    orig = sh.broker.submit

    def submit(order, children=()):
        mono.t += 5.5
        return orig(order, children)

    sh.broker.submit = submit
    run_until_processed(sh, 1, before=fresh_unreported)
    assert _no_entry_fill(sh)
    rejected = sh.events("SHADOW_OPENING_ORDER_REJECTED")
    a = _open(sh)[0]
    assert rejected or a["status"] != "EXECUTED"
    sh.close()


def test_b1_a_protective_stop_still_fills_on_a_delayed_quote_and_says_so(shadow, tws):
    sh = shadow([post("1", 0, EXPLICIT)])
    run_until_processed(sh, 1, before=lambda: sh.live(88.40, 88.42))
    [buy] = sh.events("SHADOW_SIMULATED_FILL")
    assert buy["label"] == FILL_LABELS["LIVE"] == "SIMULATED_AT_LIVE_QUOTE"
    _delayed(sh, 87.00, 87.02)  # the delayed market is through the stop
    sh.step()
    sells = [f for f in sh.events("SHADOW_SIMULATED_FILL") if f["side"] == "SELL"]
    assert [f["label"] for f in sells] == ["SIMULATED_AT_DELAYED_QUOTE"]
    assert sh.sim().positions() == []
    sh.close()
    assert_only_read_only_messages(tws)


def test_b1_fill_labels_are_exactly_three():
    assert FILL_LABELS == {"LIVE": "SIMULATED_AT_LIVE_QUOTE",
                           "DELAYED": "SIMULATED_AT_DELAYED_QUOTE",
                           "FROZEN": "SIMULATED_AT_FROZEN_QUOTE"}


def test_b1_no_real_quote_stays_in_the_simulator_after_a_read_or_on_close(shadow):
    sh = shadow([post("1", 0, EXPLICIT), post("2", 30, CLOSE)])
    run_until_processed(sh, 1, before=lambda: sh.live(88.40, 88.42))

    def rows():
        with sh.sim()._tx() as c:
            return [dict(r) for r in c.execute("SELECT * FROM sim_quotes")]

    assert rows() and all(r["quality"] == "UNAVAILABLE" and r["bid"] is None for r in rows())
    sh.close()
    assert all(r["quality"] == "UNAVAILABLE" and r["bid"] is None for r in rows())


# ------------------------------------------------------------------ B2
@pytest.mark.parametrize("text", [EXPLICIT, ANCHORED])
def test_b2_a_hung_feed_still_reporting_live_holds_every_entry(shadow, mono, text):
    sh = shadow([post("1", 0, text)])
    sh.live(88.40, 88.42)
    mono.t += 600  # nothing updates for ten minutes; the label stays REPORTED_LIVE
    run_until_processed(sh, 1)
    [a] = _open(sh)
    assert a["status"] in ("HOLD", "BLOCK"), (a["status"], a["reasons"])
    assert sh.sim().all_orders() == [] and sh.events("SHADOW_SIMULATED_FILL") == []
    quotes = [q for d in sh.events("SHADOW_DECISION_QUOTES") for q in d["quotes"]]
    assert quotes and all(q["adapter_label"] == "LIVE" and q["ask_age_seconds"] >= 600
                          for q in quotes)
    sh.close()


# ------------------------------------------------------------------ S2
def test_s2_a_stream_with_a_working_stop_is_never_evicted_and_the_cap_alerts(shadow, tws):
    _multi_symbol(tws)
    sh = shadow([post("1", 0, EXPLICIT)], factory=lambda cfg: fast_session(cfg, cap=2))
    run_until_processed(sh, 1, before=lambda: sh.live(88.40, 88.42))
    b = sh.broker
    tlt = next(k for k in b._streams if ":TLT:" in k)
    qa, qb = (b.resolve(Instrument(asset_class=AssetClass.ETF, symbol=s)) for s in ("QAA", "QBB"))
    b.quote(qa)  # the second slot
    b.quote(qb)  # the cap: QAA is evictable (no order, no position); TLT never is
    assert tlt in b._streams and qb.contract_key in b._streams and qa.contract_key not in b._streams
    b.quote(qa)  # evicts QBB, never TLT
    assert tlt in b._streams
    # A working simulated order on QAA (placed in the simulator directly, far from the market):
    # now both slots hold contracts the simulation depends on, so nothing is evictable.
    b._sim.submit(OrderIntent(order_ref="TEST-QAA", contract=qa, side=OrderSide.BUY, quantity=1,
                              order_type=OrderType.LMT, limit_price=Decimal("1.00")))
    q = b.quote(qb)
    assert q.quality.value == "UNAVAILABLE"
    assert "STREAM_CAP_REACHED" in b.last_evidence[qb.contract_key].problems
    assert tlt in b._streams and qa.contract_key in b._streams
    sh.step()
    assert any(a["kind"] == "SHADOW_STREAM_CAP_REACHED"
               for a in sh.service.store.alerts(open_only=True))
    sh.close()


# ------------------------------------------------------------------ S3
class Crash(BaseException):
    pass


def test_s3_a_crash_between_the_fill_and_its_journal_row_keeps_the_evidence(shadow, tmp_path,
                                                                             tws, mono):
    sh = shadow([post("1", 0, EXPLICIT)])
    sh.live(88.40, 88.42)
    real = sh.broker.journal

    def crashing(event, detail):
        if event == "SHADOW_SIMULATED_FILL":
            raise Crash("the process dies after the simulator committed the fill")
        real(event, detail)

    sh.broker.journal = crashing
    with pytest.raises(Crash):
        run_until_processed(sh, 1, before=lambda: sh.live(88.40, 88.42))
    assert sh.events("SHADOW_SIMULATED_FILL") == []
    assert len(sh.sim().executions()) == 1
    sh.service.coordinator.close()  # the dead process's store lock goes with it
    sh.broker.close()
    again = Shadow(tmp_path, tws, mono, restart=True, clock=sh.clock)
    try:
        [f] = again.events("SHADOW_SIMULATED_FILL")
        assert f["recovered"] is True and f["label"] == "SIMULATED_AT_LIVE_QUOTE"
        assert f["price"] == "88.42" and f["quote"]["label"] == "LIVE"
        again.step()
        assert len(again.events("SHADOW_SIMULATED_FILL")) == 1  # never twice
    finally:
        again.close()


# ------------------------------------------------------------------ S4
def test_s4_a_start_up_failure_after_connecting_leaves_nothing_connected(tmp_path, tws, mono,
                                                                        monkeypatch):
    def boom(*a, **k):
        raise OSError("the simulator's database cannot be opened")

    monkeypatch.setattr(SB, "SimBroker", boom)
    with pytest.raises((ConfigError, OSError)):
        Shadow(tmp_path, tws, mono)
    assert not [c for c in tws.connections if c.isConnected()]


# ------------------------------------------------------------------ N1 (confirmation review)
def _failed_bad_opening(sh):
    """An opening order on a read with no live data (refused), and a transient error between
    the refusal and the simulator call."""
    b = sh.broker
    c = next(iter(b._by_query.values()))
    calls = {"n": 0}
    orig = b._cancel_unqualified_openings

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient store error")
        return orig(*a, **k)

    b._cancel_unqualified_openings = flaky
    opening = OrderIntent(order_ref="TB-open", contract=c, side=OrderSide.BUY, quantity=1,
                          order_type=OrderType.LMT, limit_price=Decimal("88.42"))
    with pytest.raises(RuntimeError):
        b.submit(opening)
    assert list(b._sim._submit_script) == []  # nothing left behind by the failed call
    return b, c


def test_n1_a_failed_bad_opening_leaves_no_reject_for_the_next_exit(shadow):
    sh = shadow()
    b, c = _failed_bad_opening(sh)
    sh.live(88.40, 88.42)
    exit_ = OrderIntent(order_ref="TB-exit", contract=c, side=OrderSide.SELL, quantity=1,
                        order_type=OrderType.LMT, limit_price=Decimal("88.00"), reduce_only=True)
    [o] = b.submit(exit_)
    assert o.status.value == "Filled" and o.reason == "", (o.status, o.reason)
    assert list(b._sim._submit_script) == []
    sh.close()


def test_n1_a_failed_bad_opening_leaves_nothing_for_a_stop_amendment(shadow):
    sh = shadow()
    b, c = _failed_bad_opening(sh)
    sh.live(88.40, 88.42)
    stop = OrderIntent(order_ref="TB-stop", contract=c, side=OrderSide.SELL, quantity=1,
                       order_type=OrderType.STP, stop_price=Decimal("80.00"), reduce_only=True)
    b._sim.submit(stop)  # a working protective stop, placed in the simulator directly
    b._sim.script_next_submit("REJECT", reason="STALE")  # a stale script, however it got there
    amended = b.modify("TB-stop", stop_price=Decimal("81.00"))
    assert amended.status.value != "Rejected"
    assert list(b._sim._submit_script) == []  # drained at the start of the call, and journaled
    [d] = sh.events("SHADOW_STALE_SCRIPT_DRAINED")
    assert d["codes"] == ["STALE"] and d["where"] == "modify"
    exit_ = OrderIntent(order_ref="TB-exit", contract=c, side=OrderSide.SELL, quantity=1,
                        order_type=OrderType.LMT, limit_price=Decimal("88.00"), reduce_only=True)
    [o] = b.submit(exit_)
    assert o.status.value == "Filled"
    sh.close()


# ------------------------------------------------------------------ the life of an entry (PR #70)
RESTING = "We are now long $TLT at 88.40. Stop 87.20."  # below the ask: the entry rests


def _above_the_limit(sh):
    """Live and fresh, both sides changing, the ask never down to the 88.40 limit."""
    moves = iter(range(100000))

    def fresh():
        i = next(moves) % 2
        sh.live(88.39 + i / 100, 88.42 + i / 100)
        sh.live(88.40 - i / 100, 88.43 - i / 100)

    return fresh


def _entry_life(sh):
    [intent] = [i for i in sh.service.coordinator.store.intents() if i.action.value == "OPEN"]
    journal = [e.event for e in sh.service.coordinator.store.journal()
               if e.order_ref == intent.order_ref]
    return intent, journal


def _assert_clean_end(sh, intent):
    assert sh.sim().open_orders() == [] and sh.sim().positions() == []
    orders = {o.order_ref: o for o in sh.sim().all_orders()}
    child = [o for o in orders.values() if o.parent_ref == intent.order_ref]
    assert child and all(o.status.value == "Cancelled" for o in child)  # the stop went with it
    assert orders[intent.order_ref].status.value == "Cancelled"
    assert list(sh.sim()._submit_script) == [] and sh.events("SHADOW_STALE_SCRIPT_DRAINED") == []
    kinds = {e.kind for e in sh.service.coordinator.store.exceptions(open_only=True)}
    assert "ENTRY_REMAINDER_NOT_CANCELLED" not in kinds, kinds
    assert sh.events("SHADOW_SIMULATED_FILL") == []


def test_entry_life_the_shadow_cancel_first_then_the_core_expiry_finds_nothing(shadow, tws):
    sh = shadow([post("1", 0, RESTING)])
    fresh = _above_the_limit(sh)
    run_until_processed(sh, 1, before=fresh)
    intent, _ = _entry_life(sh)
    assert intent.state.value == "WORKING"
    _delayed(sh, 88.38, 88.40)  # the stream turns delayed: the simulation cannot keep the order
    sh.step()
    [c] = sh.events("SHADOW_OPENING_ORDER_CANCELLED")
    assert c["order_ref"] == intent.order_ref and c["code"] == "SHADOW_OPENING_QUOTE_NOT_LIVE"
    for _ in range(30):  # well past max_intent_age_seconds: the core has nothing left to expire
        fresh()
        sh.step(5.0)
    intent, journal = _entry_life(sh)
    assert intent.state.value == "CANCELLED", intent.state
    assert "ENTRY_EXPIRED_UNFILLED" not in journal, journal  # one reason: the shadow cancel
    assert len(sh.events("SHADOW_OPENING_ORDER_CANCELLED")) == 1
    _assert_clean_end(sh, intent)
    sh.close()
    assert_only_read_only_messages(tws)


@pytest.mark.parametrize("market_at_expiry", ["live", "delayed"])
def test_entry_life_the_core_expiry_first_and_the_shadow_never_cancels_twice(
        shadow, tws, market_at_expiry):
    """The core expires the entry first. "delayed": the stream turns delayed exactly at the read
    the core's own cancel makes, so both mechanisms see the same order at once."""
    sh = shadow([post("1", 0, RESTING)])
    fresh = _above_the_limit(sh)
    run_until_processed(sh, 1, before=fresh)
    intent, _ = _entry_life(sh)
    if market_at_expiry == "delayed":
        orig = sh.broker.cancel

        def cancel(order_ref):
            _delayed(sh, 88.38, 88.41)
            return orig(order_ref)

        sh.broker.cancel = cancel
    for _ in range(40):
        fresh()
        sh.step(5.0)
        if _entry_life(sh)[0].state.value != "WORKING":
            break
    intent, journal = _entry_life(sh)
    assert intent.state.value == "CANCELLED" and "ENTRY_EXPIRED_UNFILLED" in journal, journal
    assert sh.events("SHADOW_OPENING_ORDER_CANCELLED") == []  # one reason: the core's expiry
    for _ in range(3):  # later reads, live or not, find nothing to cancel again
        _delayed(sh)
        sh.step()
    assert sh.events("SHADOW_OPENING_ORDER_CANCELLED") == []
    _assert_clean_end(sh, intent)
    sh.close()
    assert_only_read_only_messages(tws)


# ------------------------------------------------------------------ the spared order (round 3)
def _turn(sh, mono, how):
    """The stream stops qualifying an opening order, with an ask BELOW the resting 88.35 limit."""
    rid = sh.rid()
    if how == "delayed":
        _delayed(sh, 88.30, 88.32)
    elif how == "frozen":
        sh.tick(-1, price(rid, 1, 88.30), price(rid, 2, 88.32),
                frame(IN.MARKET_DATA_TYPE, MarketDataType(reqId=rid, marketDataType=2)))
    else:  # one side stale: the ask sits at 88.32 beyond the 30 s limit, only the bid moves
        sh.tick(-1, price(rid, 2, 88.32))
        mono.t += 31.0
        sh.tick(-1, price(rid, 1, 88.29))


@pytest.mark.parametrize("how", ["delayed", "frozen", "one_side_stale"])
def test_round3_the_reviewers_probe_the_cancelled_entry_never_fills_on_the_cancel_read(
        shadow, tws, mono, how):
    sh = shadow()
    b, sim = sh.broker, sh.sim()
    c = next(iter(b._by_query.values()))
    sh.live(88.40, 88.42)
    b.opening_limit = lambda ref: 30.0
    entry = OrderIntent(order_ref="TB-e", contract=c, side=OrderSide.BUY, quantity=1,
                        order_type=OrderType.LMT, limit_price=Decimal("88.35"))
    stop = OrderIntent(order_ref="TB-e.STP", contract=c, side=OrderSide.SELL, quantity=1,
                       order_type=OrderType.STP, stop_price=Decimal("87.00"), parent_ref="TB-e",
                       reduce_only=True)
    b.submit(entry, [stop])
    _turn(sh, mono, how)
    out = b.cancel("TB-e")  # the core cancels its resting entry: no exception
    assert out.status.value == "Cancelled"
    assert sim.executions() == [] and sim.positions() == [] and sim.open_orders() == []
    assert {o.order_ref: o.status.value for o in sim.all_orders()} == {
        "TB-e": "Cancelled", "TB-e.STP": "Cancelled"}
    assert [r for r in b.take_fill_log()] == []  # the core's reason only
    sh.close()
    assert_only_read_only_messages(tws)


@pytest.mark.parametrize("by", ["expiry", "close"])
@pytest.mark.parametrize("when", ["before", "at", "after"])
def test_round3_the_core_cancels_by_expiry_or_close_and_the_stream_turns_around_it(
        shadow, tws, mono, by, when):
    posts = [post("1", 0, RESTING)] + ([post("2", 30, CLOSE)] if by == "close" else [])
    sh = shadow(posts)
    fresh = _above_the_limit(sh)
    run_until_processed(sh, 1, before=fresh)
    intent, _ = _entry_life(sh)
    if when == "at":
        orig = sh.broker.cancel

        def cancel(order_ref):
            _turn(sh, mono, "delayed")
            return orig(order_ref)

        sh.broker.cancel = cancel
    turned = False
    for _ in range(40):
        due = (sh.service.clock() - intent.created_at).total_seconds() > (
            25 if by == "close" else 115)
        if when == "before" and due and not turned:
            _turn(sh, mono, "delayed")
            turned = True
        else:
            fresh()
        sh.step(5.0)
        if _entry_life(sh)[0].state.value != "WORKING":
            break
    if when == "after":
        for _ in range(2):
            _turn(sh, mono, "delayed")
            sh.step()
    intent, journal = _entry_life(sh)
    assert intent.state.value == "CANCELLED", (intent.state, journal)
    core = {"expiry": "ENTRY_EXPIRED_UNFILLED", "close": "ENTRY_CANCELLED_BEFORE_FILL"}[by]
    shadow_reasons = sh.events("SHADOW_OPENING_ORDER_CANCELLED")
    if when == "before":
        assert len(shadow_reasons) == 1 and core not in journal, journal
    else:
        assert shadow_reasons == [] and core in journal, (journal, shadow_reasons)
    assert not [e for e in sh.service.store.events() if e["event"] == "POST_PROCESSING_FAILED"]
    assert "CANCEL_FAILED" not in journal
    _assert_clean_end(sh, intent)
    sh.close()
    assert_only_read_only_messages(tws)


def test_round3_an_unconfirmed_core_cancel_on_a_not_live_stream_blocks_and_never_fills(
        shadow, tws, mono):
    sh = shadow([post("1", 0, RESTING)])
    fresh = _above_the_limit(sh)
    run_until_processed(sh, 1, before=fresh)
    intent, _ = _entry_life(sh)
    orig = sh.broker.cancel
    state = {"armed": True}

    def cancel(order_ref):
        if state["armed"]:  # the simulated broker refuses the cancel requests of this call
            state["armed"] = False
            _turn(sh, mono, "delayed")
            sh.sim().script_next_cancel("REJECT")
            sh.sim().script_next_cancel("REJECT")
        return orig(order_ref)

    sh.broker.cancel = cancel
    for _ in range(30):
        fresh() if state["armed"] else _turn(sh, mono, "delayed")
        sh.step(5.0)
        kinds = {e.kind for e in sh.service.coordinator.store.exceptions(open_only=True)}
        if not state["armed"] and "ENTRY_REMAINDER_NOT_CANCELLED" in kinds:
            break
    assert "ENTRY_REMAINDER_NOT_CANCELLED" in kinds  # the instrument is blocked
    assert sh.sim().executions() == []
    for _ in range(3):  # later: the cancel goes through and is confirmed; the block lifts
        _turn(sh, mono, "delayed")
        sh.step(5.0)
    kinds = {e.kind for e in sh.service.coordinator.store.exceptions(open_only=True)}
    assert "ENTRY_REMAINDER_NOT_CANCELLED" not in kinds, kinds
    intent, _ = _entry_life(sh)
    assert intent.state.value == "CANCELLED" and sh.sim().executions() == []
    assert sh.sim().positions() == [] and sh.sim().open_orders() == []
    sh.close()
    assert_only_read_only_messages(tws)


def test_round3_a_restart_between_the_shadow_cancel_and_the_core_expiry(shadow, tmp_path, tws,
                                                                         mono):
    sh = shadow([post("1", 0, RESTING)])
    fresh = _above_the_limit(sh)
    run_until_processed(sh, 1, before=fresh)
    _turn(sh, mono, "delayed")
    sh.step()
    assert len(sh.events("SHADOW_OPENING_ORDER_CANCELLED")) == 1
    sh.service.coordinator.close()  # the process dies here
    sh.broker.close()
    again = Shadow(tmp_path, tws, mono, restart=True, clock=sh.clock)
    try:
        fresh2 = _above_the_limit(again)
        for _ in range(30):
            fresh2()
            again.step(5.0)
        [intent] = [i for i in again.service.coordinator.store.intents()
                    if i.action.value == "OPEN"]
        journal = [e.event for e in again.service.coordinator.store.journal()
                   if e.order_ref == intent.order_ref]
        assert intent.state.value == "CANCELLED" and "ENTRY_EXPIRED_UNFILLED" not in journal
        assert len(again.events("SHADOW_OPENING_ORDER_CANCELLED")) == 1
        assert again.sim().executions() == [] and again.sim().open_orders() == []
    finally:
        again.close()
    assert_only_read_only_messages(tws)


# ------------------------------------------------------------------ a refused shadow cancel alerts
def _refused_alerts(sh):
    return [a for a in sh.service.store.alerts(open_only=True)
            if a["kind"] == "SHADOW_OPENING_CANCEL_REFUSED"]


def test_a_refused_shadow_opening_cancel_alerts_until_the_cancel_succeeds(shadow, tws):
    sh = shadow([post("1", 0, RESTING)])
    fresh = _above_the_limit(sh)
    run_until_processed(sh, 1, before=fresh)
    intent, _ = _entry_life(sh)
    assert intent.state.value == "WORKING" and _refused_alerts(sh) == []
    sh.sim().script_next_cancel("REJECT")  # the simulator refuses the shadow's own cancel
    _delayed(sh, 88.38, 88.40)
    sh.step()
    [r] = sh.events("SHADOW_OPENING_CANCEL_REFUSED")
    assert r["order_ref"] == intent.order_ref
    [a] = _refused_alerts(sh)
    assert a["key"] == f"SHADOW:CANCEL_REFUSED:{intent.order_ref}"
    assert "SHADOW_OPENING_QUOTE_NOT_LIVE" in a["detail"]
    assert sh.sim().executions() == []  # the read was never published
    _delayed(sh, 88.38, 88.40)
    sh.step()  # the next read cancels it: the alert is swept
    assert len(sh.events("SHADOW_OPENING_ORDER_CANCELLED")) == 1
    assert _refused_alerts(sh) == []
    assert sh.sim().executions() == []
    sh.close()
    assert_only_read_only_messages(tws)


def test_a_refused_shadow_cancel_alert_is_swept_once_the_order_is_otherwise_terminal(
        shadow, tws):
    sh = shadow([post("1", 0, RESTING)])
    fresh = _above_the_limit(sh)
    run_until_processed(sh, 1, before=fresh)
    intent, _ = _entry_life(sh)
    sh.sim().script_next_cancel("REJECT")
    _delayed(sh, 88.38, 88.40)
    sh.step()
    assert _refused_alerts(sh)
    sh.sim().cancel(intent.order_ref)  # the order ends by another path
    sh.step()
    assert _refused_alerts(sh) == []
    sh.close()


def test_a_refused_shadow_cancel_alert_is_swept_when_a_later_read_qualifies_the_order(
        shadow, tws, monkeypatch):
    sh = shadow([post("1", 0, RESTING)])
    fresh = _above_the_limit(sh)
    run_until_processed(sh, 1, before=fresh)
    intent, _ = _entry_life(sh)
    sh.sim().script_next_cancel("REJECT")
    _delayed(sh, 88.39, 88.43)  # the ask stays above the 88.40 limit: nothing can fill
    sh.step()
    [a] = _refused_alerts(sh)
    assert "next read that does not qualify it" in a["detail"]
    # The next reads qualify the order (the opening test passes), so no cancel is needed now.
    monkeypatch.setattr(SB, "opening_problem", lambda *a, **k: None)
    sh.step()
    assert intent.order_ref in {o.order_ref for o in sh.sim().open_orders()}
    assert len(sh.events("SHADOW_OPENING_CANCEL_REFUSED")) == 1
    assert _refused_alerts(sh) == []
    sh.close()
