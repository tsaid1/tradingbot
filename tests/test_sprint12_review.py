"""PR #5 review items: exit announcements, legacy refusal, context integrity, mixed summaries. Synthetic only."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from tradingbot.event_models import Timing
from tradingbot.event_replay import PaperEventReplay
from tradingbot.models import Action, MockPolicy, PositionSide, Quote
from tradingbot.source_events import Provenance, SourcePost
from tradingbot.tkl_interpreter import MessageClass, PointInTimeContext, TklInterpreter, replay_posts
from tradingbot.validator import gate_source

T0 = datetime(2026, 11, 5, 14, 30, tzinfo=timezone.utc)
OPEN_TLT = "We are now long $TLT at $88.50. Stop: $86.00. Target: $95.00."
ANNOUNCED_EXIT = "We have closed our $TLT long at $94.10 for a +6.3% gain."


def post(sid, text, *, minutes=0.0, verified=True, complete=True, repost=False, revision=1,
         provenance=Provenance.SYNTHETIC, received_offset=0.0):
    t = T0 + timedelta(minutes=minutes)
    return SourcePost(source_id=sid, post_id=f"{sid}-v{revision}", revision=revision,
                      revised_at=t if revision > 1 else None, author_id="A", verified_author=verified, text=text,
                      text_complete=complete, is_repost=repost, published_at=t,
                      received_at=t + timedelta(seconds=received_offset), provenance=provenance)


def tlt_quotes(mid="94.10"):
    def provider(p, now):
        return {"TLT": Quote(symbol="TLT", bid=Decimal(mid) - Decimal("0.05"), ask=Decimal(mid) + Decimal("0.05"),
                             observed_at=now)}
    return provider


def opened(tmp_path, quantity=4):
    """Coordinator and interpreter with one open follower position in TLT."""
    coord = PaperEventReplay(tmp_path / "paper.sqlite3", MockPolicy(quantity=quantity))
    it = TklInterpreter()
    step = replay_posts([post("open", OPEN_TLT)], coord, it, tlt_quotes("88.50"))[0]
    assert step.result.outcomes[0].status == "SIMULATED"
    return coord, it


# ----------------------------------------------------------------------------- 1. exit announcements

def test_fresh_exit_announcement_becomes_a_reduce_only_close_candidate(tmp_path):
    coord, it = opened(tmp_path)
    ip = it.interpret(post("x", ANNOUNCED_EXIT, minutes=1))
    c, a = ip.classifications[0], ip.parsed.actions[0]
    assert (c.message_class, c.exit_kind, c.idea_reference) == (MessageClass.EXIT_ANNOUNCEMENT, "announcement", "open:a1")
    assert a.action == Action.CLOSE and a.timing == Timing.CURRENT and a.idea_reference == "open:a1" and a.explicit_instruction
    step = replay_posts([post("x", ANNOUNCED_EXIT, minutes=1)], coord, it, tlt_quotes())[0]
    assert step.result.outcomes[0].status == "SIMULATED" and step.result.outcomes[0].reasons == ["PAPER_REDUCE_ONLY"]
    assert step.result.outcomes[0].order.side == "SELL" and step.result.outcomes[0].order.quantity == 4
    assert coord.ideas()[0]["status"] == "CLOSED"
    assert it.context.ideas["open:a1"].closed_at == T0 + timedelta(minutes=1)


def test_partial_exit_announcement_is_reduce_only_too(tmp_path):
    coord, it = opened(tmp_path)
    step = replay_posts([post("x", "We have taken profits on half of our $TLT long at $92.00.", minutes=1)],
                        coord, it, tlt_quotes("92.00"))[0]
    c = step.interpreted.classifications[0]
    assert c.message_class == MessageClass.EXIT_ANNOUNCEMENT and c.underlying == Action.REDUCE and c.close_fraction == "0.5"
    assert step.result.outcomes[0].status == "SIMULATED" and step.result.outcomes[0].order.quantity == 2
    assert coord.ideas()[0]["quantity"] == 2


def test_historical_recaps_never_generate_orders(tmp_path):
    coord, it = opened(tmp_path)
    recaps = ["We closed our $TLT long yesterday at $94.10 for a +6.3% gain.",
              "Recap: we closed our $TLT long at $94.10 earlier this week.",
              "We took profits on $TLT three times this month for an average of +4%.",
              "Our $TLT long was closed on Monday at $94.10."]
    for i, text in enumerate(recaps):
        step = replay_posts([post(f"r{i}", text, minutes=i + 1)], coord, it, tlt_quotes())[0]
        c = step.interpreted.classifications[0]
        assert c.message_class == MessageClass.RETROSPECTIVE and c.exit_kind == "recap", text
        assert step.result.outcomes[0].status == "RECONCILE" and step.result.outcomes[0].order is None, text
    assert coord.ideas()[0]["quantity"] == 4 and coord.ideas()[0]["status"] == "OPEN"


def test_stop_triggered_reports_require_broker_reconciliation(tmp_path):
    coord, it = opened(tmp_path)
    for i, text in enumerate(["We were stopped out of our $TLT long at $86.00.",
                              "Our stop was hit on $TLT at $86.00.",
                              "$TLT hit our stop at $86 and took us out."]):
        step = replay_posts([post(f"s{i}", text, minutes=i + 1)], coord, it, tlt_quotes("86.00"))[0]
        c = step.interpreted.classifications[0]
        assert c.message_class == MessageClass.RETROSPECTIVE and c.exit_kind == "stop_triggered", text
        assert step.result.outcomes[0].status == "RECONCILE" and step.result.outcomes[0].order is None, text
        assert step.interpreted.parsed.actions[0].reason.endswith("/stop_triggered")
    assert coord.ideas()[0]["quantity"] == 4                       # broker state must be reconciled, not assumed
    assert it.context.ideas["open:a1"].closed_at == T0 + timedelta(minutes=1)   # publisher's book is closed


def test_announcement_without_a_unique_open_position_fails_closed(tmp_path):
    # nothing open in the follower book: a plain recap
    fresh = TklInterpreter()
    c = fresh.interpret(post("x", ANNOUNCED_EXIT)).classifications[0]
    assert c.message_class == MessageClass.RETROSPECTIVE and c.exit_kind is None
    # two candidates: still an announcement, but ambiguous -> the gateway holds it
    coord, it = opened(tmp_path)
    it.ingest(post("open2", "Going long $TLT again at $89.00 with a stop at $87.", minutes=0.5))
    step = replay_posts([post("x", ANNOUNCED_EXIT, minutes=1)], coord, it, tlt_quotes())[0]
    c = step.interpreted.classifications[0]
    assert c.message_class == MessageClass.EXIT_ANNOUNCEMENT and "MULTIPLE_OPEN_IDEAS_IN_CONTEXT" in c.ambiguities
    assert step.result.outcomes[0].status == "HOLD" and "SOURCE_AMBIGUITIES" in step.result.outcomes[0].reasons
    assert coord.ideas()[0]["quantity"] == 4


def test_announcement_from_ineligible_posts_is_only_a_report(tmp_path):
    coord, it = opened(tmp_path)
    variants = {"unverified": post("u", ANNOUNCED_EXIT, minutes=1, verified=False),
                "truncated": post("t", ANNOUNCED_EXIT, minutes=2, complete=False),
                "repost": post("r", ANNOUNCED_EXIT, minutes=3, repost=True),
                "edit": post("e", ANNOUNCED_EXIT, minutes=4, revision=2)}
    for name, p in variants.items():
        step = replay_posts([p], coord, it, tlt_quotes())[0]
        assert step.interpreted.classifications[0].message_class == MessageClass.RETROSPECTIVE, name
        assert step.interpreted.parsed.actions[0].timing == Timing.HISTORICAL, name
        assert step.result.outcomes[0].order is None and step.result.outcomes[0].status in ("HOLD", "BLOCK", "NO_TRADE"), name
    assert coord.ideas()[0]["quantity"] == 4 and it.context.ideas["open:a1"].closed_at is None


def test_stale_announcement_is_blocked_by_the_canonical_freshness_rule(tmp_path):
    coord, it = opened(tmp_path)
    step = replay_posts([post("late", ANNOUNCED_EXIT, minutes=1)], coord, it, tlt_quotes(), lag_seconds=600)[0]
    assert step.interpreted.classifications[0].message_class == MessageClass.EXIT_ANNOUNCEMENT
    assert step.result.outcomes[0].status == "BLOCK" and step.result.outcomes[0].reasons == ["SOURCE_STALE_OR_FUTURE"]
    assert coord.ideas()[0]["quantity"] == 4


def test_close_followed_by_its_own_confirmation_reconciles_without_a_second_order(tmp_path):
    coord, it = opened(tmp_path)
    steps = replay_posts([post("c", "Closing our $TLT long here at $94.10.", minutes=1),
                          post("k", ANNOUNCED_EXIT, minutes=2)], coord, it, tlt_quotes())
    assert [s.result.outcomes[0].status for s in steps] == ["SIMULATED", "RECONCILE"]
    assert steps[1].interpreted.classifications[0].message_class == MessageClass.RETROSPECTIVE


# ----------------------------------------------------------------------------- 2. legacy conversion

@pytest.mark.parametrize("field, value, message", [
    ("verified_author", False, "author not verified"),
    ("text_complete", False, "text incomplete"),
    ("is_repost", True, "repost"),
    ("revision", 2, "edited post"),
])
def test_legacy_conversion_refuses_ineligible_posts(field, value, message):
    base = {"source_id": "s", "post_id": "s", "author_id": "A", "verified_author": True, "text": OPEN_TLT,
            "text_complete": True, "published_at": T0, "received_at": T0, "provenance": Provenance.SYNTHETIC}
    base[field] = value
    if field == "revision":
        base["revised_at"] = T0
    with pytest.raises(ValueError, match=message):
        SourcePost(**base).to_legacy_source_event()
    good = {"verified_author": True, "text_complete": True, "is_repost": False, "revision": 1}[field]
    event = SourcePost(**{**base, field: good, "revised_at": None}).to_legacy_source_event()
    assert event.text == OPEN_TLT and event.source_type == "manual_demo"


# ----------------------------------------------------------------------------- 3. context integrity

def test_ineligible_posts_never_advance_the_publisher_context():
    ctx = PointInTimeContext()
    it = TklInterpreter(ctx)
    ineligible = [post("u", OPEN_TLT, verified=False), post("t", OPEN_TLT, minutes=1, complete=False),
                  post("r", OPEN_TLT, minutes=2, repost=True), post("e", OPEN_TLT, minutes=3, revision=2),
                  post("b", OPEN_TLT, minutes=4, received_offset=-5)]   # receipt before publication
    for p in ineligible:
        ip = it.ingest(p)
        assert ip.classifications[0].message_class == MessageClass.OPEN        # interpreted...
        assert not it.is_source_eligible(p) and gate_source(p.to_replay_source(), p.received_at) is not None
    assert ctx.ideas == {} and ctx.observed_posts == set()                     # ...but never remembered
    assert len(ctx.seen_posts) == 5
    follow_up = it.interpret(post("c", "Closing our $TLT long at $90.", minutes=5))
    assert "NO_OPEN_IDEA_IN_CONTEXT" in follow_up.parsed.actions[0].ambiguities


def test_eligibility_mirrors_the_gateway_exactly():
    it = TklInterpreter()
    for p in (post("ok", OPEN_TLT), post("u", OPEN_TLT, verified=False), post("t", OPEN_TLT, complete=False),
              post("r", OPEN_TLT, repost=True), post("b", OPEN_TLT, received_offset=-5)):
        assert it.is_source_eligible(p) == (gate_source(p.to_replay_source(), p.received_at) is None)


def test_a_later_edit_retroactively_makes_the_original_revision_ineligible():
    it = TklInterpreter()
    it.ingest(post("p", OPEN_TLT, revision=2))          # the edit is seen first
    it.ingest(post("p", OPEN_TLT, minutes=1))           # then the original arrives
    assert it.context.ideas == {}                        # neither advances the book
    assert not it.is_source_eligible(post("p", OPEN_TLT, minutes=1))


def test_unverified_conditional_and_cancel_do_not_touch_intents():
    it = TklInterpreter()
    it.ingest(post("n", "If natural gas closes below $2.75 we will exit the position.", verified=False))
    assert it.context.intents == {}


# ----------------------------------------------------------------------------- 4. mixed summary posts

def test_summary_post_does_not_suppress_an_explicit_close(tmp_path):
    coord, it = opened(tmp_path)
    text = "Position update:\n1. Long $GLD (entry $280)\n2. Long $TLT (entry $88.50)\nClosing our $TLT long here at $94.10."
    step = replay_posts([post("m", text, minutes=1)], coord, it, tlt_quotes())[0]
    classes = [c.message_class for c in step.interpreted.classifications]
    assert MessageClass.CLOSE in classes and MessageClass.POSITION_SUMMARY in classes and MessageClass.OPEN not in classes
    close = next(o for o in step.result.outcomes if o.order is not None)
    assert close.status == "SIMULATED" and close.order.side == "SELL" and close.order.quantity == 4
    assert coord.ideas()[0]["status"] == "CLOSED" and len(coord.ideas()) == 1      # no GLD idea was opened


def test_summary_post_does_not_suppress_an_explicit_open_or_modify(tmp_path):
    coord = PaperEventReplay(tmp_path / "paper.sqlite3", MockPolicy(quantity=2))
    it = TklInterpreter()

    def quotes(p, now):
        return {"SPY": Quote(symbol="SPY", bid=Decimal("684.95"), ask=Decimal("685.05"), observed_at=now)}

    text = "Current positions: Long $TLT at $88.50.\nWe are now short $SPY at $685.00 with a stop at $695.00."
    step = replay_posts([post("m", text)], coord, it, quotes)[0]
    classes = [c.message_class for c in step.interpreted.classifications]
    assert classes.count(MessageClass.OPEN) == 1 and MessageClass.POSITION_SUMMARY in classes
    assert next(c for c in step.interpreted.classifications if c.message_class == MessageClass.OPEN).asset == "SPY"
    assert [o.status for o in step.result.outcomes if o.order] == ["SIMULATED"] and coord.ideas()[0]["symbol"] == "SPY"
    ip = it.interpret(post("m2", "Portfolio update: Short $SPY from $685.\nRaising our stop to $690.00 on $SPY.", minutes=1))
    assert [c.message_class for c in ip.classifications] == [MessageClass.MODIFY, MessageClass.POSITION_SUMMARY]
    assert ip.parsed.actions[0].stop_price == "690.00" and ip.parsed.actions[0].idea_reference == "m:a1"


def test_summary_lines_never_open_and_a_bare_headline_is_ambiguous():
    it = TklInterpreter()
    ip = it.interpret(post("s", "Current positions:\nLong $TLT at $88.50\nShort $SPY at $685"))
    assert [c.message_class for c in ip.classifications] == [MessageClass.POSITION_SUMMARY]
    assert ip.parsed.actions[0].action == Action.NO_TRADE
    bare = it.interpret(post("h", "Long $TLT at $88.50, stop $86, target $95."))
    c, a = bare.classifications[0], bare.parsed.actions[0]
    assert c.message_class == MessageClass.OPEN and "HEADLINE_POSITION_LINE_WITHOUT_ENTRY_VERB" in a.ambiguities
    assert c.side == PositionSide.LONG


# ----------------------------------------------------------------------------- grouping guard

def test_non_fillable_clauses_never_join_an_atomic_group(tmp_path):
    coord, it = opened(tmp_path)
    text = "Closing our $TLT long here at $94.10 and we were stopped out of the rest earlier today."
    step = replay_posts([post("g", text, minutes=1)], coord, it, tlt_quotes())[0]
    current, historical = step.result.outcomes
    assert current.status == "SIMULATED" and current.order.side == "SELL"
    assert historical.status == "RECONCILE" and "ATOMIC_GROUP_REJECTED" not in current.reasons

