"""Synthetic-only Sprint 1.1 regression tests (no paid TKL content)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256

import pytest
from pydantic import ValidationError

from tradingbot.event_models import (
    ParsedPost, PriceBasis, ReplayPolicy, ReplaySource, Timing, TradeAction,
)
from tradingbot.event_replay import PaperEventReplay
from tradingbot.models import Action, AssetClass, Instrument, PositionSide, Quote

NOW = datetime(2026, 11, 5, 14, 30, tzinfo=timezone.utc)


def source(sid, **overrides):
    kw = {
        "source_id": sid, "published_at": NOW, "received_at": NOW,
        "verified_author": True, "complete_text": True,
        "post_hash": sha256(f"SYNTHETIC:{sid}".encode()).hexdigest(),
    }
    kw.update(overrides)
    return ReplaySource(**kw)


def quote(symbol="TLT", bid="99.95", ask="100.05", observed_at=NOW):
    return Quote(symbol=symbol, bid=Decimal(bid), ask=Decimal(ask), observed_at=observed_at)


def action(aid, instruction, *, timing=Timing.CURRENT, symbol="TLT", cls=AssetClass.ETF,
           side=None, idea=None, ref=None, stop=None, target=None, fraction=None, group=None,
           price_basis=PriceBasis.TRADED_CONTRACT, source_market_symbol=None,
           explicit=True, evidence=None, ambiguities=None):
    return TradeAction(
        action_id=aid, action=instruction, timing=timing,
        instrument=Instrument(symbol=symbol, asset_class=cls),
        position_side=side, idea_reference=idea, entry_reference=ref,
        stop_price=stop, target_price=target, close_fraction=fraction,
        atomic_group=group, price_basis=price_basis,
        source_market_symbol=source_market_symbol or symbol,
        explicit_instruction=explicit, evidence=evidence if evidence is not None else ["SYNTHETIC_TEST"],
        ambiguities=ambiguities or [],
    )


def open_tlt(aid="open", *, side=PositionSide.LONG):
    return action(aid, Action.OPEN, side=side, ref="100",
                  stop="95" if side == PositionSide.LONG else "105",
                  target="110" if side == PositionSide.LONG else "90")


def replay(tmp_path, **kw):
    # These scenarios were written for a 2-unit opening size; the shared MockPolicy default is 1.
    kw.setdefault("quantity_per_open", 2)
    return PaperEventReplay(tmp_path / "paper.sqlite3", ReplayPolicy(**kw))


def run(engine, sid, *actions, quotes=None, **source_overrides):
    return engine.process(source(sid, **source_overrides), ParsedPost(actions=list(actions)),
                          quotes=quotes or {"TLT": quote()}, now=NOW)


def test_tlt_open_then_explicit_exit_and_idempotent_replay(tmp_path):
    e = replay(tmp_path)
    entry = run(e, "tlt-1", open_tlt())
    assert entry.outcomes[0].status == "SIMULATED"
    assert entry.outcomes[0].order.idea_id == "tlt-1:open"
    assert e.ideas()[0]["quantity"] == 2
    close = action("exit", Action.CLOSE, idea="tlt-1:open")
    exit_result = run(e, "tlt-2", close)
    assert exit_result.outcomes[0].order.side == "SELL"
    assert exit_result.outcomes[0].order.quantity == 2
    assert e.ideas()[0]["status"] == "CLOSED"
    duplicate = run(e, "tlt-2", close)
    assert duplicate.duplicate
    assert e.ideas()[0]["quantity"] == 0


def test_old_stopped_idea_cannot_be_confused_with_new_tlt_idea(tmp_path):
    e = replay(tmp_path)
    run(e, "old-entry", open_tlt())
    run(e, "old-close", action("x", Action.CLOSE, idea="old-entry:open"))
    run(e, "new-entry", open_tlt())
    historical = run(e, "historical", action("h", Action.CLOSE,
                           timing=Timing.HISTORICAL, idea="old-entry:open"))
    assert historical.outcomes[0].status == "RECONCILE"
    old_attempt = run(e, "bad", action("x", Action.CLOSE, idea="old-entry:open"))
    assert old_attempt.outcomes[0].status == "HOLD"
    assert next(i for i in e.ideas() if i["idea_id"] == "new-entry:open")["quantity"] == 2
    new_close = run(e, "actual", action("x", Action.CLOSE, idea="new-entry:open"))
    assert new_close.outcomes[0].status == "SIMULATED"


def test_multi_action_post_processes_two_distinct_closes(tmp_path):
    e = replay(tmp_path, allowed_symbols={"TLT", "GDX"})
    run(e, "tl", open_tlt())
    gdx_open = action("open", Action.OPEN, symbol="GDX", side=PositionSide.SHORT,
                      ref="100", stop="105", target="90")
    run(e, "gd", gdx_open, quotes={"GDX": quote("GDX")})
    both = e.process(source("multi-close"), ParsedPost(actions=[
        action("cover", Action.CLOSE, symbol="GDX", idea="gd:open"),
        action("sell", Action.CLOSE, idea="tl:open"),
    ]), {"GDX": quote("GDX"), "TLT": quote()}, now=NOW)
    assert [r.order.side for r in both.outcomes] == ["BUY", "SELL"]
    assert all(r.status == "SIMULATED" for r in both.outcomes)
    assert all(i["quantity"] == 0 for i in e.ideas())
    assert e.process(source("multi-close"), ParsedPost(actions=[]), now=NOW).duplicate


def test_independent_failed_leg_does_not_repeat_successful_leg(tmp_path):
    e = replay(tmp_path)
    run(e, "entry", open_tlt())
    results = run(e, "two", action("unknown", Action.CLOSE, idea="wrong"),
                  action("real", Action.CLOSE, idea="entry:open"))
    assert [r.status for r in results.outcomes] == ["HOLD", "SIMULATED"]
    assert run(e, "two", action("unknown", Action.CLOSE, idea="wrong")).duplicate
    assert e.ideas()[0]["quantity"] == 0


def test_historical_confirmation_does_not_sell(tmp_path):
    e = replay(tmp_path)
    run(e, "entry", open_tlt())
    res = run(e, "history", action("report", Action.CLOSE, timing=Timing.HISTORICAL,
                                  idea="entry:open"))
    assert res.outcomes[0].status == "RECONCILE"
    assert res.outcomes[0].order is None
    assert e.ideas()[0]["quantity"] == 2


def test_conditional_and_summary_cannot_execute(tmp_path):
    e = replay(tmp_path)
    conditional = run(e, "conditional", action("c", Action.CLOSE,
                      timing=Timing.CONDITIONAL, idea="some-open-idea"))
    assert conditional.outcomes[0].status == "HOLD"
    summary = e.process(source("summary"), ParsedPost(actions=[]), now=NOW)
    assert summary.outcomes == []
    assert e.ideas() == []


def test_truncated_post_blocks_all_actions(tmp_path):
    e = replay(tmp_path)
    result = run(e, "trunc", open_tlt(), complete_text=False)
    assert result.outcomes[0].reasons == ["SOURCE_TEXT_TRUNCATED"]
    assert not e.ideas()


def test_unverified_author_and_repost_cannot_trade(tmp_path):
    e = replay(tmp_path)
    assert run(e, "wrong-author", open_tlt(), verified_author=False).outcomes[0].status == "BLOCK"
    assert run(e, "repost", open_tlt(), is_repost=True).outcomes[0].status == "NO_TRADE"
    assert e.ideas() == []


def test_edited_post_is_not_a_fresh_order(tmp_path):
    e = replay(tmp_path)
    run(e, "post", open_tlt())
    edited = run(e, "post", action("new", Action.OPEN, side=PositionSide.LONG,
                     ref="100", stop="95"), revision=2)
    assert edited.outcomes[0].reasons == ["SOURCE_EDIT_REQUIRES_REVIEW"]
    assert len(e.ideas()) == 1


def test_source_series_to_etf_mapping_requires_explicit_policy(tmp_path):
    e = replay(tmp_path, allowed_symbols={"TLT", "GLD"})
    req = action("gold", Action.OPEN, symbol="GLD", side=PositionSide.LONG,
                 ref="100", stop="95", price_basis=PriceBasis.SOURCE_SERIES,
                 source_market_symbol="GOLD")
    result = run(e, "gold", req, quotes={"GLD": quote("GLD")})
    assert "SOURCE_TO_BROKER_PRICE_BASIS_UNRESOLVED" in result.outcomes[0].reasons
    assert e.ideas() == []


def test_mismatch_even_if_price_basis_claims_traded_contract(tmp_path):
    e = replay(tmp_path, allowed_symbols={"GLD"})
    req = action("gold", Action.OPEN, symbol="GLD", side=PositionSide.LONG,
                 ref="100", stop="95", source_market_symbol="GOLD")
    result = run(e, "gold", req, quotes={"GLD": quote("GLD")})
    assert "SOURCE_TO_BROKER_MAPPING_UNAPPROVED" in result.outcomes[0].reasons


def test_group_rejects_add_and_stop_widening_together(tmp_path):
    e = replay(tmp_path)
    run(e, "entry", open_tlt())
    result = run(e, "change", action("add", Action.ADD, idea="entry:open", ref="100", group="bundle"),
                 action("widen", Action.MODIFY, idea="entry:open", stop="90", group="bundle"))
    assert [r.status for r in result.outcomes] == ["HOLD", "HOLD"]
    assert all("ATOMIC_GROUP_REJECTED" in r.reasons for r in result.outcomes)
    assert e.ideas()[0]["quantity"] == 2
    assert e.ideas()[0]["stop_price"] == "95"


def test_group_can_add_and_tighten_stop_atomically(tmp_path):
    e = replay(tmp_path)
    run(e, "entry", open_tlt())
    result = run(e, "change", action("add", Action.ADD, idea="entry:open", ref="100", group="bundle"),
                 action("tighten", Action.MODIFY, idea="entry:open", stop="97", group="bundle"))
    assert [r.status for r in result.outcomes] == ["SIMULATED", "SIMULATED"]
    assert e.ideas()[0]["quantity"] == 3
    assert e.ideas()[0]["stop_price"] == "97"
    assert e.ideas()[0]["target_price"] == "110"


def test_standalone_stop_change_retains_target(tmp_path):
    e = replay(tmp_path)
    run(e, "entry", open_tlt())
    change = run(e, "change", action("m", Action.MODIFY, idea="entry:open", stop="97"))
    assert change.outcomes[0].order.status == "SIMULATED_AMENDED"
    assert e.ideas()[0]["target_price"] == "110"


def test_wrong_contract_side_and_ref_fail_closed(tmp_path):
    e = replay(tmp_path, allowed_symbols={"TLT", "GDX"})
    run(e, "entry", open_tlt())
    wrong = run(e, "wrong", action("w", Action.CLOSE, symbol="GDX", idea="entry:open"),
                quotes={"GDX": quote("GDX")})
    assert "IDEA_INSTRUMENT_MISMATCH" in wrong.outcomes[0].reasons
    wrong_side = run(e, "wrong-side", action("w", Action.CLOSE, idea="entry:open",
                          side=PositionSide.SHORT))
    assert "IDEA_SIDE_MISMATCH" in wrong_side.outcomes[0].reasons
    assert e.ideas()[0]["quantity"] == 2


def test_quote_staleness_and_collar(tmp_path):
    e = replay(tmp_path)
    stale = run(e, "stale", open_tlt(), quotes={"TLT": quote(observed_at=NOW - timedelta(minutes=2))})
    assert stale.outcomes[0].reasons == ["QUOTE_STALE_OR_FUTURE"]
    collar = run(e, "collar", open_tlt(), quotes={"TLT": quote(bid="104", ask="104.5")})
    assert collar.outcomes[0].reasons == ["ENTRY_PRICE_COLLAR"]
    assert not e.ideas()


def test_source_age_blocks_current_but_historical_never_trades(tmp_path):
    e = replay(tmp_path)
    stale = run(e, "stale", open_tlt(), published_at=NOW - timedelta(minutes=3))
    assert stale.outcomes[0].reasons == ["SOURCE_STALE_OR_FUTURE"]
    report = run(e, "history", action("r", Action.CLOSE, timing=Timing.HISTORICAL),
                 published_at=NOW - timedelta(days=3), received_at=NOW)
    assert report.outcomes[0].status == "RECONCILE"


def test_unsupported_futures_and_options_are_never_executed(tmp_path):
    e = replay(tmp_path, allowed_symbols={"TLT", "CL", "SPY"})
    fut = run(e, "fut", action("f", Action.OPEN, symbol="CL", cls=AssetClass.FUTURE,
                        side=PositionSide.SHORT, ref="100", stop="105"), quotes={"CL": quote("CL")})
    opt = run(e, "opt", action("o", Action.OPEN, symbol="SPY", cls=AssetClass.OPTION,
                        side=PositionSide.LONG, ref="100", stop="95"), quotes={"SPY": quote("SPY")})
    assert fut.outcomes[0].status == "HOLD"
    assert opt.outcomes[0].status == "HOLD"


def test_missing_id_and_missing_stop_fail_closed(tmp_path):
    e = replay(tmp_path)
    missing_stop = run(e, "no-stop", action("open", Action.OPEN, side=PositionSide.LONG, ref="100"))
    assert missing_stop.outcomes[0].reasons == ["PROTECTIVE_STOP_MISSING"]
    no_id = run(e, "no-id", action("x", Action.CLOSE))
    assert no_id.outcomes[0].reasons == ["REFERENCED_ACTIVE_IDEA_NOT_FOUND"]


def test_duplicate_action_ids_rejected_by_schema(tmp_path):
    with pytest.raises(ValidationError):
        ParsedPost(actions=[open_tlt("same"), open_tlt("same")])


def test_malformed_decimal_never_opens(tmp_path):
    e = replay(tmp_path)
    bad = run(e, "nan", action("open", Action.OPEN, side=PositionSide.LONG,
                     ref="NaN", stop="95"))
    assert bad.outcomes[0].status == "HOLD"
    assert e.ideas() == []


def test_source_digest_change_without_revision_blocked(tmp_path):
    e = replay(tmp_path)
    run(e, "post", open_tlt())
    same_id_tampered = source("post", post_hash="b" * 64)
    out = e.process(same_id_tampered, ParsedPost(actions=[open_tlt()]),
                    {"TLT": quote()}, now=NOW)
    assert out.duplicate
    assert out.outcomes[0].reasons == ["SOURCE_HASH_CHANGED_WITHOUT_REVISION"]
    assert e.ideas()[0]["quantity"] == 2


def test_source_market_identity_missing_even_when_claimed_traded(tmp_path):
    e = replay(tmp_path)
    req = action("x", Action.OPEN, side=PositionSide.LONG, ref="100", stop="95")
    req.source_market_symbol = None
    out = run(e, "no-id", req)
    assert out.outcomes[0].reasons == ["SOURCE_PRICE_INSTRUMENT_IDENTITY_UNVERIFIED"]


def test_cannot_add_below_existing_long_stop(tmp_path):
    e = replay(tmp_path)
    run(e, "entry", open_tlt())
    out = run(e, "add-bad", action("add", Action.ADD, idea="entry:open", ref="93"),
              quotes={"TLT": quote(bid="92.95", ask="93.05")})
    assert out.outcomes[0].reasons == ["EXISTING_STOP_ALREADY_CROSSED"]
    assert e.ideas()[0]["quantity"] == 2


def test_entry_stop_must_be_correct_side_of_current_quote(tmp_path):
    e = replay(tmp_path)
    out = run(e, "crossed", action("entry", Action.OPEN, side=PositionSide.LONG,
              ref="100", stop="99.97"))
    assert out.outcomes[0].reasons == ["STOP_ALREADY_CROSSED_AT_ENTRY"]
    assert e.ideas() == []


def test_reduce_half_only_matched_idea(tmp_path):
    e = replay(tmp_path, quantity_per_open=4)
    run(e, "one", open_tlt())
    run(e, "two", open_tlt())
    out = run(e, "reduce", action("r", Action.REDUCE, idea="two:open", fraction="0.5"))
    assert out.outcomes[0].order.quantity == 2
    balances = {i["idea_id"]: i["quantity"] for i in e.ideas()}
    assert balances == {"one:open": 4, "two:open": 2}
