"""Synthetic lifecycle scenarios through interpreter -> validator gateway -> Sprint 1.1 coordinator.

Shapes mirror real publisher episodes (TLT lifecycle, an S&P reversal, staged partial exits, a
conditional natural-gas exit) but every word, price and date here is invented.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from tradingbot.event_models import ParsedPost
from tradingbot.event_replay import PaperEventReplay
from tradingbot.models import MockPolicy, Quote
from tradingbot.source_events import Provenance, SourcePost
from tradingbot.tkl_interpreter import MessageClass, TklInterpreter, replay_posts

T0 = datetime(2026, 11, 5, 14, 30, tzinfo=timezone.utc)


def post(sid, text, *, hours=0.0, complete=True, repost=False, provenance=Provenance.SYNTHETIC):
    t = T0 + timedelta(hours=hours)
    return SourcePost(source_id=sid, post_id=sid, author_id="A", verified_author=True, text=text,
                      text_complete=complete, is_repost=repost, published_at=t, received_at=t, provenance=provenance)


def quotes(mids: dict[str, dict[str, str]]):
    """mids: post_id -> {symbol: mid price}. Quotes are stamped at `now` so they are always fresh."""
    def provider(p: SourcePost, now: datetime) -> dict[str, Quote]:
        return {sym: Quote(symbol=sym, bid=Decimal(mid) - Decimal("0.05"), ask=Decimal(mid) + Decimal("0.05"),
                           observed_at=now) for sym, mid in mids.get(p.post_id, {}).items()}
    return provider


def run(tmp_path, posts, mids, **policy):
    coordinator = PaperEventReplay(tmp_path / "paper.sqlite3", MockPolicy(**policy))
    steps = replay_posts(posts, coordinator, TklInterpreter(), quotes(mids))
    return coordinator, {s.post.post_id: s for s in steps}


def statuses(step):
    return [o.status for o in step.result.outcomes]


def orders(step):
    return [o.order for o in step.result.outcomes if o.order is not None]


# ----------------------------------------------------------------------------- TLT lifecycle

TLT_POSTS = [
    post("p1", "We are now long $TLT at $88.50. Stop: $86.00. Target: $95.00.", hours=0),
    post("p2", "Adding to our $TLT long at $89.20 and raising our stop to $87.00.", hours=24),
    post("p3", "Taking profits on half of our $TLT long at $92.00.", hours=120),
    post("p4", "Closing the remaining $TLT long here at $94.10.", hours=216),
    post("p5", "We have closed our $TLT long at $94.10 for a +6.3% gain.", hours=217),
    post("p6", "Current positions: none. We closed $TLT for a +6.3% gain earlier this week.", hours=240),
]
TLT_MIDS = {"p1": {"TLT": "88.50"}, "p2": {"TLT": "89.20"}, "p3": {"TLT": "92.00"}, "p4": {"TLT": "94.10"},
            "p5": {"TLT": "94.10"}, "p6": {"TLT": "94.00"}}


def test_tlt_lifecycle_open_add_modify_reduce_close_confirm_summary(tmp_path):
    coord = PaperEventReplay(tmp_path / "paper.sqlite3", MockPolicy(quantity=4, quantity_per_add=2))
    it = TklInterpreter()
    s = {}
    for p in TLT_POSTS:   # one post at a time so the ledger can be checked between posts
        s[p.post_id] = replay_posts([p], coord, it, quotes(TLT_MIDS))[0]
        if p.post_id == "p1":
            idea = coord.ideas()[0]
            assert (idea["idea_id"], idea["quantity"], idea["stop_price"], idea["target_price"]) == ("p1:a1", 4, "86.00", "95.00")
        if p.post_id == "p2":
            idea = coord.ideas()[0]
            assert (idea["quantity"], idea["stop_price"], idea["target_price"]) == (6, "87.00", "95.00")
        if p.post_id == "p3":
            assert coord.ideas()[0]["quantity"] == 3
    assert statuses(s["p1"]) == ["SIMULATED"] and orders(s["p1"])[0].side == "BUY" and orders(s["p1"])[0].quantity == 4
    assert statuses(s["p2"]) == ["SIMULATED", "SIMULATED"]           # ADD + MODIFY, one atomic group
    assert statuses(s["p3"]) == ["SIMULATED"] and orders(s["p3"])[0].side == "SELL" and orders(s["p3"])[0].quantity == 3
    assert statuses(s["p4"]) == ["SIMULATED"] and orders(s["p4"])[0].quantity == 3
    assert coord.ideas()[0]["status"] == "CLOSED" and coord.ideas()[0]["quantity"] == 0
    assert statuses(s["p5"]) == ["RECONCILE"] and orders(s["p5"]) == []  # past-tense confirmation: no order
    assert statuses(s["p6"]) == ["NO_TRADE"] and orders(s["p6"]) == []    # position summary: no order
    assert s["p6"].interpreted.classifications[0].message_class == MessageClass.POSITION_SUMMARY
    assert sum(len(orders(step)) for step in s.values()) == 5
    # Replaying an executed post is a duplicate and never fills again
    again = coord.process(TLT_POSTS[3].to_replay_source(), s["p4"].interpreted.parsed, {}, now=TLT_POSTS[3].published_at)
    assert again.duplicate and coord.ideas()[0]["quantity"] == 0


def test_tlt_lifecycle_is_order_independent_and_never_leaks_the_future(tmp_path):
    shuffled = [TLT_POSTS[3], TLT_POSTS[0], TLT_POSTS[5], TLT_POSTS[2], TLT_POSTS[1], TLT_POSTS[4]]
    coord, s = run(tmp_path, shuffled, TLT_MIDS, quantity=4, quantity_per_add=2)
    assert [statuses(s[k]) for k in ("p1", "p2", "p3", "p4", "p5", "p6")] == [
        ["SIMULATED"], ["SIMULATED", "SIMULATED"], ["SIMULATED"], ["SIMULATED"], ["RECONCILE"], ["NO_TRADE"]]
    assert coord.ideas()[0]["status"] == "CLOSED"


# ----------------------------------------------------------------------------- S&P reversal

def test_index_level_reversal_fails_closed_but_is_classified(tmp_path):
    posts = [post("q1", "We are now short the S&P 500 at 6,850 with a stop at 6,950.", hours=0),
             post("q2", "Closing our S&P 500 short at 6,700 and going long at 6,700 with a stop at 6,600.", hours=48)]
    coord, s = run(tmp_path, posts, {})
    assert statuses(s["q1"]) == ["HOLD"] and s["q1"].result.outcomes[0].reasons == ["ORDERABLE_INSTRUMENT_MISSING"]
    assert statuses(s["q2"]) == ["HOLD", "HOLD"]
    assert [c.message_class for c in s["q2"].interpreted.classifications] == [MessageClass.CLOSE, MessageClass.OPEN]
    assert [c.asset for c in s["q2"].interpreted.classifications] == ["SPX", "SPX"]
    assert coord.ideas() == [] and sum(len(orders(x)) for x in s.values()) == 0


def test_etf_reversal_executes_both_legs_atomically(tmp_path):
    posts = [post("r1", "We are now short $SPY at $685.00 with a stop at $695.00.", hours=0),
             post("r2", "Covering our $SPY short at $670.00 and going long $SPY at $670.00 with a stop at $660.00.", hours=48)]
    mids = {"r1": {"SPY": "685.00"}, "r2": {"SPY": "670.00"}}
    coord, s = run(tmp_path, posts, mids, quantity=2)
    assert statuses(s["r1"]) == ["SIMULATED"] and orders(s["r1"])[0].side == "SELL"
    assert statuses(s["r2"]) == ["SIMULATED", "SIMULATED"]
    cover, opn = orders(s["r2"])
    assert (cover.side, cover.quantity, cover.idea_id) == ("BUY", 2, "r1:a1")
    assert (opn.side, opn.quantity, opn.idea_id) == ("BUY", 2, "r2:a2")
    ideas = {i["idea_id"]: i for i in coord.ideas()}
    assert ideas["r1:a1"]["status"] == "CLOSED" and ideas["r2:a2"]["quantity"] == 2 and ideas["r2:a2"]["position_side"] == "LONG"


def test_etf_reversal_with_a_bad_open_leg_changes_nothing(tmp_path):
    posts = [post("r1", "We are now short $SPY at $685.00 with a stop at $695.00.", hours=0),
             post("r2", "Covering our $SPY short at $670.00 and going long $SPY at $670.00 with a stop at $675.00.", hours=48)]
    mids = {"r1": {"SPY": "685.00"}, "r2": {"SPY": "670.00"}}
    coord, s = run(tmp_path, posts, mids, quantity=2)
    assert statuses(s["r2"]) == ["HOLD", "HOLD"]
    assert all("ATOMIC_GROUP_REJECTED" in o.reasons for o in s["r2"].result.outcomes)
    assert "STOP_DIRECTION_CONTRADICTION" in s["r2"].result.outcomes[1].reasons
    assert coord.ideas()[0]["quantity"] == 2 and coord.ideas()[0]["status"] == "OPEN"   # short still open, no cover


# ----------------------------------------------------------------------------- staged partial exits

def test_staged_partial_exits_with_a_non_integer_leg_held(tmp_path):
    posts = [post("s1", "We are now long $GLD at $280.00 with a stop at $270.00 and a target of $310.00.", hours=0),
             post("s2", "Taking profits on a quarter of our $GLD long at $292.00.", hours=72),
             post("s3", "Selling half of our remaining $GLD long at $298.00.", hours=120),
             post("s4", "Taking profits on a third of our $GLD long at $301.00.", hours=168),
             post("s5", "Closing the rest of our $GLD long at $305.00.", hours=240),
             post("s6", "We took profits on $GLD three times in April for an average gain of +8%.", hours=300)]
    mids = {"s1": {"GLD": "280.00"}, "s2": {"GLD": "292.00"}, "s3": {"GLD": "298.00"}, "s4": {"GLD": "301.00"},
            "s5": {"GLD": "305.00"}, "s6": {"GLD": "305.00"}}
    coord, s = run(tmp_path, posts, mids, quantity=8, max_notional_usd=Decimal("5000"))
    assert statuses(s["s1"]) == ["SIMULATED"] and orders(s["s1"])[0].quantity == 8
    assert statuses(s["s2"]) == ["SIMULATED"] and orders(s["s2"])[0].quantity == 2
    assert statuses(s["s3"]) == ["SIMULATED"] and orders(s["s3"])[0].quantity == 3
    assert statuses(s["s4"]) == ["HOLD"] and s["s4"].result.outcomes[0].reasons == ["NON_INTEGER_REDUCTION"]
    assert statuses(s["s5"]) == ["SIMULATED"] and orders(s["s5"])[0].quantity == 3
    assert statuses(s["s6"]) == ["RECONCILE"] and orders(s["s6"]) == []
    assert coord.ideas()[0]["status"] == "CLOSED" and sum(len(orders(x)) for x in s.values()) == 4


# ----------------------------------------------------------------------------- conditional natural-gas exit

def test_natural_gas_conditional_exit_never_creates_an_order(tmp_path):
    posts = [post("n1", "We are long natural gas from $2.90. If natural gas closes below $2.75 we will exit the position.", hours=0),
             post("n2", "We are no longer looking to exit natural gas on a break below $2.75. Stop now at $2.60.", hours=24),
             post("n4", "Exiting our natural gas long now at $2.80.", hours=48),
             post("n3", "Natural gas has broken below $2.60 and we have exited our long at $2.58.", hours=72)]
    coord, s = run(tmp_path, posts, {})
    assert statuses(s["n1"]) == ["RECONCILE", "HOLD"]
    assert s["n1"].result.outcomes[1].reasons == ["CONDITIONAL_TRIGGER_NOT_IMPLEMENTED"]
    assert [c.message_class for c in s["n2"].interpreted.classifications] == [MessageClass.CANCEL, MessageClass.MODIFY]
    assert statuses(s["n2"]) == ["NO_TRADE", "HOLD"]
    assert statuses(s["n4"]) == ["HOLD"] and s["n4"].result.outcomes[0].reasons == ["ORDERABLE_INSTRUMENT_MISSING"]
    assert statuses(s["n3"]) == ["RECONCILE"]
    assert coord.ideas() == [] and sum(len(orders(x)) for x in s.values()) == 0


# ----------------------------------------------------------------------------- provenance safety

def test_truncated_reposted_and_unverified_posts_fail_closed(tmp_path):
    text = "We are now long $TLT at $88.50. Stop: $86.00. Target: $95.00."
    unverified = post("u", text, hours=3)
    unverified = unverified.model_copy(update={"verified_author": False})
    posts = [post("t", text, hours=0, complete=False), post("r", text, hours=1, repost=True), unverified]
    coord, s = run(tmp_path, posts, {"t": {"TLT": "88.50"}, "r": {"TLT": "88.50"}, "u": {"TLT": "88.50"}}, quantity=1)
    assert s["t"].result.outcomes[0].reasons == ["SOURCE_TEXT_TRUNCATED"]
    assert s["r"].result.outcomes[0].reasons == ["REPOST_NOT_AUTHOR_INSTRUCTION"]
    assert s["u"].result.outcomes[0].reasons == ["SOURCE_NOT_AUTHENTICATED"]
    assert coord.ideas() == []


def test_hydrated_completion_executes_once_and_only_once(tmp_path):
    text = "We are now long $TLT at $88.50. Stop: $86.00. Target: $95.00."
    coord = PaperEventReplay(tmp_path / "paper.sqlite3", MockPolicy(quantity=1))
    it = TklInterpreter()
    truncated = post("h", "We are now long $TLT at $88.50. Stop…", complete=False)
    full = post("h", text, complete=True)
    first = replay_posts([truncated], coord, it, quotes({"h": {"TLT": "88.50"}}))[0]
    assert first.result.outcomes[0].reasons == ["SOURCE_TEXT_TRUNCATED"] and coord.ideas() == []
    second = replay_posts([full], coord, it, quotes({"h": {"TLT": "88.50"}}))[0]
    assert second.result.hydrated and statuses(second) == ["SIMULATED"] and coord.ideas()[0]["quantity"] == 1
    third = replay_posts([full], coord, it, quotes({"h": {"TLT": "88.50"}}))[0]
    assert third.result.duplicate and coord.ideas()[0]["quantity"] == 1


def test_archive_posts_replay_at_publication_time_plus_lag(tmp_path):
    p = post("a", "We are now long $TLT at $88.50. Stop: $86.00. Target: $95.00.", provenance=Provenance.ARCHIVE_API_USER_CONTEXT)
    p = p.model_copy(update={"received_at": T0 + timedelta(days=30)})   # archive receipt is not a latency
    coord = PaperEventReplay(tmp_path / "paper.sqlite3", MockPolicy(quantity=1))
    seen = {}

    def provider(post_, now):
        seen["now"] = now
        return {"TLT": Quote(symbol="TLT", bid=Decimal("88.45"), ask=Decimal("88.55"), observed_at=now)}

    step = replay_posts([p], coord, TklInterpreter(), provider, lag_seconds=13.3)[0]
    assert seen["now"] == T0 + timedelta(seconds=13.3) and statuses(step) == ["SIMULATED"]


def test_empty_post_yields_no_actions(tmp_path):
    coord, s = run(tmp_path, [post("e", "")], {})
    assert coord.ideas() == [] and s["e"].result.outcomes == [] and s["e"].interpreted.parsed == ParsedPost(actions=[])
