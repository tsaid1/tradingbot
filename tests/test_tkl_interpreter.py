"""TKL interpreter grammar, tense, price roles, point-in-time context and AI gating. Synthetic text only."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from tradingbot.event_models import ParsedPost, PriceBasis, Timing, TradeAction
from tradingbot.models import Action, AssetClass, PositionSide
from tradingbot.source_events import Provenance, SourcePost
from tradingbot.tkl_interpreter import (
    MessageClass, PointInTimeContext, PriceRole, Tense, TklInterpreter, group_linked_actions,
)

T0 = datetime(2026, 11, 5, 14, 30, tzinfo=timezone.utc)
OPEN_TLT = "We are now long $TLT at $88.50. Stop: $86.00. Target: $95.00."


def post(sid, text, *, minutes=0, provenance=Provenance.SYNTHETIC, complete=True, revision=1, repost=False):
    t = T0 + timedelta(minutes=minutes)
    return SourcePost(source_id=sid, post_id=f"{sid}-v{revision}", revision=revision,
                      revised_at=t if revision > 1 else None, author_id="A", verified_author=True, text=text,
                      text_complete=complete, is_repost=repost, published_at=t, received_at=t, provenance=provenance)


def roles(c):
    return {p.role: p.value for p in c.prices}


def one(interpreted):
    assert len(interpreted.classifications) == 1 == len(interpreted.parsed.actions)
    return interpreted.classifications[0], interpreted.parsed.actions[0]


def test_explicit_open_with_levels_on_separate_lines():
    c, a = one(TklInterpreter().interpret(post("p1", OPEN_TLT)))
    assert (c.message_class, c.tense, c.asset, c.side) == (MessageClass.OPEN, Tense.PRESENT, "TLT", PositionSide.LONG)
    assert roles(c) == {PriceRole.ENTRY: "88.50", PriceRole.STOP: "86.00", PriceRole.TARGET: "95.00"}
    assert a.action == Action.OPEN and a.timing == Timing.CURRENT
    assert a.instrument.symbol == "TLT" and a.instrument.asset_class == AssetClass.ETF
    assert a.price_basis == PriceBasis.TRADED_CONTRACT and a.source_market_symbol == "TLT"
    assert (a.entry_reference, a.stop_price, a.target_price) == ("88.50", "86.00", "95.00")
    assert a.explicit_instruction and a.ambiguities == [] and a.evidence


def test_index_level_open_is_a_source_series_without_instrument():
    c, a = one(TklInterpreter().interpret(post("q1", "We are now short the S&P 500 at 6,850 with a stop at 6,950.")))
    assert c.asset == "SPX" and c.side == PositionSide.SHORT
    assert roles(c) == {PriceRole.ENTRY: "6850", PriceRole.STOP: "6950"}
    assert a.instrument is None and a.price_basis == PriceBasis.SOURCE_SERIES and a.source_market_symbol == "SPX"


def test_multi_action_post_add_and_modify_share_sentence_and_idea():
    it = TklInterpreter()
    it.ingest(post("p1", OPEN_TLT))
    ip = it.interpret(post("p2", "Adding to our $TLT long at $89.20 and raising our stop to $87.00.", minutes=1))
    assert [c.message_class for c in ip.classifications] == [MessageClass.ADD, MessageClass.MODIFY]
    assert all(c.idea_reference == "p1:a1" and c.asset == "TLT" and c.sentence == 0 for c in ip.classifications)
    add, mod = ip.parsed.actions
    assert add.action == Action.ADD and add.entry_reference == "89.20" and add.idea_reference == "p1:a1"
    assert mod.action == Action.MODIFY and mod.stop_price == "87.00" and mod.target_price is None
    grouped = group_linked_actions(ip).actions
    assert grouped[0].atomic_group == grouped[1].atomic_group is not None


def test_reversal_splits_into_close_and_open_in_one_group():
    it = TklInterpreter()
    it.ingest(post("r1", "We are now short $SPY at $685.00 with a stop at $695.00."))
    ip = it.interpret(post("r2", "Covering our $SPY short at $670.00 and going long $SPY at $670.00 with a stop at $660.00.", minutes=5))
    close, opn = ip.classifications
    assert (close.message_class, close.side, close.idea_reference) == (MessageClass.CLOSE, PositionSide.SHORT, "r1:a1")
    assert (opn.message_class, opn.side) == (MessageClass.OPEN, PositionSide.LONG)
    assert roles(opn) == {PriceRole.ENTRY: "670.00", PriceRole.STOP: "660.00"} and roles(close)[PriceRole.EXIT] == "670.00"
    grouped = group_linked_actions(ip).actions
    assert grouped[0].atomic_group == grouped[1].atomic_group is not None


def test_partial_exit_fractions():
    it = TklInterpreter()
    it.ingest(post("p1", OPEN_TLT))
    for text, fraction in [
        ("Taking profits on half of our $TLT long at $92.00.", "0.5"),
        ("Selling a quarter of our $TLT long at $92.00.", "0.25"),
        ("Taking profits on 25% of our $TLT long at $92.", "0.25"),
        ("Closing two-thirds of our $TLT long at $92.", "0.666667"),
        ("Taking some profits on our $TLT long at $92.", None),
    ]:
        c, a = one(it.interpret(post("x", text, minutes=1)))
        assert c.message_class == MessageClass.REDUCE and a.action == Action.REDUCE, text
        assert a.close_fraction == fraction and c.idea_reference == "p1:a1", text
        assert roles(c).get(PriceRole.EXIT) is not None, text


def test_past_tense_reports_are_retrospective_and_historical():
    it = TklInterpreter()
    it.ingest(post("p1", OPEN_TLT))
    c, a = one(it.interpret(post("p5", "We have closed our $TLT long at $94.10 for a +6.3% gain.", minutes=10)))
    assert (c.message_class, c.tense, c.underlying) == (MessageClass.RETROSPECTIVE, Tense.PAST, Action.CLOSE)
    assert roles(c) == {PriceRole.PNL_PERCENT: "+6.3", PriceRole.EXIT: "94.10"} and c.idea_reference == "p1:a1"
    assert a.action == Action.CLOSE and a.timing == Timing.HISTORICAL
    c2, a2 = one(it.interpret(post("p6", "We have raised our stop to $90 on our $TLT long.", minutes=11)))
    assert c2.tense == Tense.PAST and c2.message_class == MessageClass.RETROSPECTIVE and a2.action == Action.MODIFY and a2.timing == Timing.HISTORICAL
    c3, a3 = one(it.interpret(post("p7", "We took profits on $GLD three times in April for an average gain of +8%.", minutes=12)))
    assert c3.underlying == Action.CLOSE and c3.message_class == MessageClass.RETROSPECTIVE and a3.action == Action.CLOSE and a3.timing == Timing.HISTORICAL
    assert roles(c3) == {PriceRole.PNL_PERCENT: "+8"}


def test_position_summary_is_a_single_no_trade():
    for text in ("Current positions: none. We closed $TLT for a +6.3% gain earlier this week.",
                 "Position update:\n1. Long $TLT (entry $88.50)\n2. Short $SPY (entry $685)",
                 "Long $TLT from $88.50\nShort $SPY from $685"):
        c, a = one(TklInterpreter().interpret(post("s", text)))
        assert c.message_class == MessageClass.POSITION_SUMMARY, text
        assert a.action == Action.NO_TRADE and a.timing == Timing.COMMENTARY and not a.explicit_instruction


def test_commentary_never_trades():
    for text in ("We remain bullish on $GLD with a 320 target.",
                 "We remain long $TLT at these levels.",
                 "If the S&P 500 breaks 6,000 we could see 5,800.",
                 "Treasuries look attractive here."):
        c, a = one(TklInterpreter().interpret(post("c", text)))
        assert c.message_class == MessageClass.COMMENTARY and a.action == Action.NO_TRADE, text
        assert a.timing == Timing.COMMENTARY and not a.explicit_instruction


def test_conditional_exit_then_cancel_and_future_intent():
    it = TklInterpreter()
    ip = it.ingest(post("n1", "We are long natural gas from $2.90. If natural gas closes below $2.75 we will exit the position."))
    stated, cond = ip.classifications
    assert (stated.message_class, stated.underlying, stated.side) == (MessageClass.RETROSPECTIVE, Action.OPEN, PositionSide.LONG)
    assert (cond.message_class, cond.tense, cond.underlying, cond.asset) == (MessageClass.CONDITIONAL, Tense.FUTURE, Action.CLOSE, "NATGAS")
    assert roles(cond) == {PriceRole.TRIGGER: "2.75"}
    assert ip.parsed.actions[1].timing == Timing.CONDITIONAL and ip.parsed.actions[1].action == Action.CLOSE
    assert len(it.context.pending_intents("NATGAS", T0 + timedelta(minutes=1))) == 1
    ip2 = it.ingest(post("n2", "We are no longer looking to exit natural gas on a break below $2.75. Stop now at $2.60.", minutes=2))
    cancel, modify = ip2.classifications
    assert cancel.message_class == MessageClass.CANCEL and ip2.parsed.actions[0].action == Action.NO_TRADE
    assert modify.message_class == MessageClass.MODIFY and modify.idea_reference == "n1:a1" and roles(modify) == {PriceRole.STOP: "2.60"}
    assert it.context.pending_intents("NATGAS", T0 + timedelta(minutes=3)) == []
    assert len(it.context.pending_intents("NATGAS", T0 + timedelta(minutes=1))) == 1   # history is preserved
    c, a = one(it.interpret(post("n9", "We will add to our natural gas long on a pullback to $2.70.", minutes=4)))
    assert c.message_class == MessageClass.CONDITIONAL and "FUTURE_INTENT_WITHOUT_EXPLICIT_CONDITION" in a.ambiguities


def test_modify_variants_and_unresolved_asset_fail_closed():
    it = TklInterpreter()
    it.ingest(post("p1", OPEN_TLT))
    c, a = one(it.interpret(post("m1", "Stop raised to $90 on our $TLT long.", minutes=1)))
    assert c.tense == Tense.PRESENT and c.message_class == MessageClass.MODIFY and a.action == Action.MODIFY and a.stop_price == "90" and a.idea_reference == "p1:a1"
    c2, a2 = one(it.interpret(post("m2", "Moving our stop to $90.", minutes=2)))
    assert c2.message_class == MessageClass.MODIFY and "ASSET_UNRESOLVED" in a2.ambiguities and a2.instrument is None


def test_ambiguous_references_fail_closed():
    it = TklInterpreter()
    assert "NO_OPEN_IDEA_IN_CONTEXT" in one(it.interpret(post("c0", "Closing our $TLT long at $90.")))[1].ambiguities
    it.ingest(post("p1", OPEN_TLT))
    it.ingest(post("p1b", "Going long $TLT again at $89.", minutes=1))
    _, a = one(it.interpret(post("c1", "Closing our $TLT long at $90.", minutes=2)))
    assert "MULTIPLE_OPEN_IDEAS_IN_CONTEXT" in a.ambiguities and a.idea_reference is None
    _, a2 = one(it.interpret(post("c2", "Closing our S&P 500 short via $SPY at $670.", minutes=2)))
    assert "MIXED_SERIES_AND_TICKER" in a2.ambiguities
    _, a3 = one(it.interpret(post("c3", "Closing our long here at $90.", minutes=2)))
    assert "ASSET_UNRESOLVED" in a3.ambiguities


def test_second_open_on_the_same_side_is_flagged():
    it = TklInterpreter()
    it.ingest(post("p1", OPEN_TLT))
    c, a = one(it.interpret(post("p2", "We are now long $TLT at $89.00 with a stop at $87.", minutes=1)))
    assert c.message_class == MessageClass.OPEN and "EXISTING_OPEN_IDEA_SAME_SIDE_IN_CONTEXT" in a.ambiguities


def test_point_in_time_resolution_never_sees_later_posts():
    it = TklInterpreter()
    it.ingest(post("A", OPEN_TLT))                                                   # T0
    b = post("B", "Closing our $TLT long at $92.00.", minutes=2)                      # T0 + 2
    before = it.interpret(b)
    it.ingest(post("C", "Closing our $TLT long at $94.00.", minutes=3))               # T0 + 3 closes the idea
    after = it.interpret(b)
    assert before == after                                                            # no leakage from C into B
    assert before.classifications[0].idea_reference == "A:a1" and before.parsed.actions[0].ambiguities == []
    late = it.interpret(post("D", "Closing our $TLT long at $95.00.", minutes=4))     # T0 + 4: nothing open
    assert "NO_OPEN_IDEA_IN_CONTEXT" in late.parsed.actions[0].ambiguities
    assert it.context.ideas["A:a1"].closed_at == T0 + timedelta(minutes=3)


def test_edits_reposts_and_truncated_posts_do_not_change_context():
    it = TklInterpreter()
    for p in (post("e", OPEN_TLT, revision=2), post("r", OPEN_TLT, repost=True), post("t", OPEN_TLT, complete=False)):
        ip = it.ingest(p)
        assert ip.classifications[0].message_class == MessageClass.OPEN     # still interpreted
    assert it.context.ideas == {} and it.context.observed_posts == set()
    it.ingest(post("ok", OPEN_TLT))
    assert set(it.context.ideas) == {"ok:a1"}
    it.ingest(post("ok", OPEN_TLT))   # same revision twice: observed once
    assert set(it.context.ideas) == {"ok:a1"}


class _FakeBackend:
    def __init__(self):
        self.calls = 0

    def parse(self, text, image=None):
        self.calls += 1
        return ParsedPost(actions=[TradeAction(action_id="ai1", action=Action.NO_TRADE, timing=Timing.COMMENTARY)])


def test_ai_backend_is_gated_for_paid_content():
    backend = _FakeBackend()
    it = TklInterpreter(backend=backend)
    with pytest.raises(PermissionError):
        it.interpret(post("live", OPEN_TLT, provenance=Provenance.LIVE_API_USER_CONTEXT))
    with pytest.raises(PermissionError):
        it.interpret(post("arch", OPEN_TLT, provenance=Provenance.ARCHIVE_API_USER_CONTEXT))
    assert backend.calls == 0
    ip = it.interpret(post("syn", OPEN_TLT))
    assert ip.backend == "ai" and backend.calls == 1 and ip.classifications[0].message_class == MessageClass.COMMENTARY
    permitted = TklInterpreter(backend=backend, external_processing_permitted=True)
    assert permitted.interpret(post("live", OPEN_TLT, provenance=Provenance.LIVE_API_USER_CONTEXT)).backend == "ai"
    assert TklInterpreter().interpret(post("live", OPEN_TLT, provenance=Provenance.LIVE_API_USER_CONTEXT)).backend == "deterministic"


def test_context_is_shareable_and_append_only():
    ctx = PointInTimeContext()
    a, b = TklInterpreter(ctx), TklInterpreter(ctx)
    a.ingest(post("p1", OPEN_TLT))
    assert b.interpret(post("c", "Closing our $TLT long at $90.", minutes=1)).classifications[0].idea_reference == "p1:a1"
