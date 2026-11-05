"""Targeted corrections measured by the synthetic evaluation (fixture e04 and the four planted phrasings)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from tradingbot.event_models import Timing
from tradingbot.models import Action, PositionSide
from tradingbot.source_events import Provenance, SourcePost
from tradingbot.tkl_interpreter import MessageClass, PriceRole, Tense, TklInterpreter

T0 = datetime(2026, 10, 1, 14, 30, tzinfo=timezone.utc)


def post(sid, text, minutes=0):
    t = T0 + timedelta(minutes=minutes)
    return SourcePost(source_id=sid, post_id=sid, author_id="S", verified_author=True, text=text, text_complete=True,
                      published_at=t, received_at=t, provenance=Provenance.SYNTHETIC)


def roles(c):
    return {p.role: p.value for p in c.prices}


def one(ip):
    assert len(ip.classifications) == 1
    return ip.classifications[0], ip.parsed.actions[0]


def test_decimals_survive_a_following_comma_and_roles_follow_the_nearest_keyword():
    c, a = one(TklInterpreter().interpret(post("e04", "Buying $SLV here at $31.20, stop $29.50, target $36.")))
    assert c.message_class == MessageClass.OPEN
    assert roles(c) == {PriceRole.ENTRY: "31.20", PriceRole.STOP: "29.50", PriceRole.TARGET: "36"}
    assert (a.entry_reference, a.stop_price, a.target_price) == ("31.20", "29.50", "36")
    # earlier forms keep their roles
    c, _ = one(TklInterpreter().interpret(post("x", "We are now long $TLT at $88.50 with a stop at $86.00 and a target of $95.00.")))
    assert roles(c) == {PriceRole.ENTRY: "88.50", PriceRole.STOP: "86.00", PriceRole.TARGET: "95.00"}
    c, _ = one(TklInterpreter().interpret(post("y", "We are now short the S&P 500 at 6,850 with a stop at 6,950.")))
    assert roles(c) == {PriceRole.ENTRY: "6850", PriceRole.STOP: "6950"}
    c, _ = one(TklInterpreter().interpret(post("z", "We are now long $TLT: target $95, stop $86, entry $88.50.", minutes=1)))
    assert roles(c)[PriceRole.TARGET] == "95" and roles(c)[PriceRole.STOP] == "86" and roles(c)[PriceRole.ENTRY] == "88.50"


def test_asset_between_verb_and_stop_is_a_modify():
    it = TklInterpreter()
    it.ingest(post("g", "Going long $GLD at $280.00 with a stop at $270.00 and a target of $310.00."))
    c, a = one(it.interpret(post("m", "Moving our $GLD stop up to $285.", minutes=1)))
    assert c.message_class == MessageClass.MODIFY and a.action == Action.MODIFY and a.timing == Timing.CURRENT
    assert a.stop_price == "285" and a.idea_reference == "g:a1" and a.ambiguities == []


def test_terse_and_idiomatic_exits_are_closes():
    it = TklInterpreter()
    it.ingest(post("s", "Buying $SLV here at $31.20, stop $29.50, target $36."))
    it.ingest(post("i", "We are now long $IWM at $220 with a stop at $212.", minutes=1))
    c, a = one(it.interpret(post("o", "Out of $SLV here at $33.80, +8.3%.", minutes=2)))
    assert c.message_class == MessageClass.CLOSE and a.action == Action.CLOSE and a.timing == Timing.CURRENT
    assert roles(c) == {PriceRole.PNL_PERCENT: "+8.3", PriceRole.EXIT: "33.80"} and a.idea_reference == "s:a1"
    c, a = one(it.interpret(post("k", "Cutting our $IWM long loose at $214.", minutes=3)))
    assert c.message_class == MessageClass.CLOSE and a.idea_reference == "i:a1" and roles(c)[PriceRole.EXIT] == "214"
    # idioms that are not exits stay commentary
    c, a = one(it.interpret(post("c", "The rally is running out of steam here.", minutes=4)))
    assert c.message_class == MessageClass.COMMENTARY and a.action == Action.NO_TRADE


def test_past_tense_entry_announcement_registers_the_position_but_does_not_execute():
    it = TklInterpreter()
    c, a = one(it.ingest(post("e05", "Initiated a long in $IWM this morning at $220 with a stop at $212.")))
    assert (c.message_class, c.tense, c.underlying, c.side) == (MessageClass.RETROSPECTIVE, Tense.PAST, Action.OPEN, PositionSide.LONG)
    assert roles(c) == {PriceRole.ENTRY: "220", PriceRole.STOP: "212"}
    assert a.timing == Timing.HISTORICAL                      # entry announcements are a separate product decision
    assert "e05:a1" in it.context.ideas and it.context.ideas["e05:a1"].stop_price == "212"
    c, a = one(it.interpret(post("e31", "Cutting our $IWM long loose at $214.", minutes=5)))
    assert c.message_class == MessageClass.CLOSE and a.idea_reference == "e05:a1"
