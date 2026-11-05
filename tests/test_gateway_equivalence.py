"""Sprint 1.1 integration revision: one gateway, one policy, safe hydration, closed connections.

Synthetic data only. Proves that the Sprint 1 pipeline path (SignalCandidate -> validate()) and the
Sprint 1.1 replay path (TradeAction -> gate_action()) receive equivalent decisions for equivalent
inputs, that risk parameters are defined once, and that a truncated post can be completed without
bypassing revision or duplicate protections.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256

import pytest
from pydantic import ValidationError

from tradingbot import event_replay, storage
from tradingbot.event_models import ParsedPost, PriceBasis, ReplayPolicy, ReplaySource, Timing, TradeAction
from tradingbot.event_replay import PaperEventReplay
from tradingbot.models import (
    Action, AssetClass, Instrument, MockPolicy, Position, PositionSide, Quote, SignalCandidate, SourceEvent,
)
from tradingbot.pipeline import SignalPipeline
from tradingbot.storage import Journal
from tradingbot.validator import (
    ALL_ACTIONS, LEGACY_REASON_CODES, LEGACY_STATUS, SPRINT1_ACTIONS, gate_action, gate_source, validate,
)

NOW = datetime(2026, 11, 5, 14, 30, tzinfo=timezone.utc)


# ----------------------------------------------------------------------------- one policy contract

def test_policy_is_defined_once_and_locked():
    assert ReplayPolicy is MockPolicy
    p = MockPolicy(quantity_per_open=3, max_deviation_pct=Decimal("2"), max_source_age_seconds=45,
                   allowed_symbols={"TLT"})
    assert p.quantity == 3 and p.max_price_deviation_pct == 2 and p.max_signal_age_seconds == 45
    assert MockPolicy(quantity=4).quantity == 4
    assert MockPolicy().require_stop_on_open is True and MockPolicy().allowed_symbols is None
    for bad in ({"paper_only": False}, {"allow_stop_widening": True}, {"live": True}):
        with pytest.raises(ValidationError):
            MockPolicy(**bad)


def test_llm_schemas_carry_no_sizing_or_routing():
    forbidden = {"quantity", "qty", "size", "notional", "account", "venue", "exchange_route", "broker"}
    for model in (TradeAction, ParsedPost, SignalCandidate):
        assert not forbidden & set(model.model_fields), model.__name__
    with pytest.raises(ValidationError):
        TradeAction(action_id="x", action=Action.OPEN, timing=Timing.CURRENT, quantity=5)
    with pytest.raises(ValidationError):
        SignalCandidate(action=Action.OPEN, quantity=5)


# ----------------------------------------------------------------------------- equivalence table

def _quote(symbol="ABC", bid="99.95", ask="100.05", age=0):
    return Quote(symbol=symbol, bid=Decimal(bid), ask=Decimal(ask), observed_at=NOW - timedelta(seconds=age))


SCENARIOS = {
    # name: (candidate overrides, quote or None, source age s, position or None, policy kwargs)
    "open_long_ok": ({}, _quote(), 0, None, {}),
    "open_short_ok": ({"side": PositionSide.SHORT, "symbol": "XYZ", "ref": "50", "stop": "55", "target": "40"},
                      _quote("XYZ", "49.95", "50.05"), 0, None, {}),
    "collar_breach": ({}, _quote(bid="103.9", ask="104.0"), 0, None, {}),
    "spread_too_wide": ({}, _quote(bid="99", ask="101"), 0, None, {}),
    "stale_quote": ({}, _quote(age=200), 0, None, {}),
    "stale_source": ({}, _quote(), 300, None, {}),
    "future_source": ({}, _quote(), -60, None, {}),
    "quote_symbol_mismatch": ({}, _quote("XYZ"), 0, None, {}),
    "quote_missing": ({}, None, 0, None, {}),
    "stop_contradiction": ({"stop": "105"}, _quote(), 0, None, {}),
    "stop_already_crossed": ({"stop": "99.97"}, _quote(), 0, None, {}),
    "target_contradiction": ({"target": "90"}, _quote(), 0, None, {}),
    "stop_missing": ({"stop": None}, _quote(), 0, None, {}),
    "malformed_stop": ({"stop": "ninety"}, _quote(), 0, None, {}),
    "entry_reference_missing": ({"ref": None}, _quote(), 0, None, {}),
    "notional_cap": ({}, _quote(), 0, None, {"max_notional_usd": Decimal("50")}),
    "option_not_executable": ({"cls": AssetClass.OPTION}, _quote(), 0, None, {}),
    "future_not_executable": ({"cls": AssetClass.FUTURE}, _quote(), 0, None, {}),
    "conditional": ({"conditional": True}, _quote(), 0, None, {}),
    "not_explicit": ({"explicit": False}, _quote(), 0, None, {}),
    "ambiguous": ({"ambiguities": ["WHICH_POSITION"]}, _quote(), 0, None, {}),
    "no_instrument": ({"symbol": None}, _quote(), 0, None, {}),
    "close_ok": ({"action": Action.CLOSE, "idea": "entry-7", "ref": None, "stop": None, "target": None, "side": None},
                 _quote(), 0, Position(idea_id="entry-7", symbol="ABC", asset_class=AssetClass.EQUITY,
                                       side=PositionSide.LONG, quantity=4), {}),
    "close_unknown_idea": ({"action": Action.CLOSE, "idea": "nope", "ref": None, "stop": None, "target": None,
                            "side": None}, _quote(), 0, None, {}),
    "reduce_half_ok": ({"action": Action.REDUCE, "idea": "e", "fraction": "0.5", "ref": None, "stop": None,
                        "target": None, "side": None}, _quote(), 0,
                       Position(idea_id="e", symbol="ABC", asset_class=AssetClass.EQUITY, side=PositionSide.LONG,
                                quantity=10), {}),
    "reduce_non_integer": ({"action": Action.REDUCE, "idea": "e", "fraction": "0.5", "ref": None, "stop": None,
                            "target": None, "side": None}, _quote(), 0,
                           Position(idea_id="e", symbol="ABC", asset_class=AssetClass.EQUITY,
                                    side=PositionSide.LONG, quantity=5), {}),
    "reduce_bad_fraction": ({"action": Action.REDUCE, "idea": "e", "fraction": "1.5", "ref": None, "stop": None,
                             "target": None, "side": None}, _quote(), 0,
                            Position(idea_id="e", symbol="ABC", asset_class=AssetClass.EQUITY,
                                     side=PositionSide.LONG, quantity=10), {}),
    "position_instrument_mismatch": ({"action": Action.CLOSE, "idea": "e", "ref": None, "stop": None,
                                      "target": None, "side": None}, _quote(), 0,
                                     Position(idea_id="e", symbol="XYZ", asset_class=AssetClass.EQUITY,
                                              side=PositionSide.LONG, quantity=4), {}),
    "no_trade": ({"action": Action.NO_TRADE, "explicit": False}, _quote(), 0, None, {}),
}


def _inputs(overrides):
    base = {"action": Action.OPEN, "symbol": "ABC", "cls": AssetClass.EQUITY, "side": PositionSide.LONG,
            "ref": "100", "stop": "95", "target": "110", "fraction": None, "idea": None, "conditional": False,
            "explicit": True, "ambiguities": []}
    base.update(overrides)
    return base


def _candidate(s) -> SignalCandidate:
    inst = Instrument(asset_class=s["cls"], symbol=s["symbol"]) if s["symbol"] else None
    return SignalCandidate(action=s["action"], instrument=inst, side=s["side"], entry_reference=s["ref"],
                           stop_price=s["stop"], target_price=s["target"], close_fraction=s["fraction"],
                           idea_reference=s["idea"], conditional=s["conditional"],
                           explicit_instruction=s["explicit"], evidence=["SYNTHETIC"] if s["explicit"] else [],
                           ambiguities=s["ambiguities"], reason="synthetic")


def _trade_action(s) -> TradeAction:
    inst = Instrument(asset_class=s["cls"], symbol=s["symbol"]) if s["symbol"] else None
    return TradeAction(action_id="a1", action=s["action"],
                       timing=Timing.CONDITIONAL if s["conditional"] else Timing.CURRENT,
                       instrument=inst, position_side=s["side"], idea_reference=s["idea"],
                       price_basis=PriceBasis.TRADED_CONTRACT if inst else PriceBasis.UNKNOWN,
                       source_market_symbol=s["symbol"], entry_reference=s["ref"], stop_price=s["stop"],
                       target_price=s["target"], close_fraction=s["fraction"], explicit_instruction=s["explicit"],
                       evidence=["SYNTHETIC"] if s["explicit"] else [], ambiguities=s["ambiguities"])


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_both_paths_receive_equivalent_decisions(name):
    overrides, quote, source_age, position, policy_kw = SCENARIOS[name]
    s = _inputs(overrides)
    policy = MockPolicy(**policy_kw)
    published = NOW - timedelta(seconds=source_age)
    legacy = validate(SourceEvent(source_id="s", text="synthetic", published_at=published),
                      _candidate(s), quote, policy, position, now=NOW)
    gate = gate_action(_trade_action(s), published, quote, policy, position, now=NOW,
                       supported_actions=ALL_ACTIONS)
    assert legacy.status == LEGACY_STATUS[gate.status], (name, legacy, gate)
    if s["action"] != Action.NO_TRADE:   # NO_TRADE keeps the interpreter's own reason text in Sprint 1
        assert legacy.reasons == [LEGACY_REASON_CODES.get(r, r) for r in gate.reasons], (name, legacy, gate)
    expected_status = {
        "open_long_ok": "EXECUTE_MOCK", "open_short_ok": "EXECUTE_MOCK", "close_ok": "EXECUTE_MOCK",
        "reduce_half_ok": "EXECUTE_MOCK", "no_trade": "NO_TRADE",
    }.get(name)
    if expected_status:
        assert legacy.status == expected_status
    else:
        assert legacy.status in ("REVIEW", "BLOCK"), name


def test_legacy_wrapper_keeps_add_and_modify_under_review_because_the_journal_cannot_fill_them():
    pos = Position(idea_id="e", symbol="ABC", asset_class=AssetClass.EQUITY, side=PositionSide.LONG,
                   quantity=2, stop_price="95")
    add = _inputs({"action": Action.ADD, "idea": "e", "stop": None, "target": None, "side": None})
    legacy = validate(SourceEvent(source_id="s", text="x", published_at=NOW), _candidate(add), _quote(),
                      MockPolicy(), pos, now=NOW)
    assert legacy.status == "REVIEW" and legacy.reasons == ["ACTION_UNSUPPORTED_IN_SPRINT_1"]
    replay_side = gate_action(_trade_action(add), NOW, _quote(), MockPolicy(), pos, now=NOW)
    assert replay_side.status == "APPROVE" and replay_side.reasons == ["PAPER_ADD"]
    assert SPRINT1_ACTIONS < ALL_ACTIONS


# ----------------------------------------------------------------------------- end to end, both engines

class _Stub:
    def __init__(self, candidate):
        self.candidate = candidate

    def parse(self, text, image=None):
        return self.candidate


def _source(sid, complete=True, **o):
    kw = {"source_id": sid, "published_at": NOW, "received_at": NOW, "verified_author": True,
          "complete_text": complete, "post_hash": sha256(f"SYNTHETIC:{sid}:{complete}".encode()).hexdigest()}
    kw.update(o)
    return ReplaySource(**kw)


def test_engines_agree_end_to_end_open_collar_and_close(tmp_path):
    policy = MockPolicy(quantity=2)
    journal = Journal(tmp_path / "sprint1.sqlite3")
    replay = PaperEventReplay(tmp_path / "replay.sqlite3", policy)
    live_now = datetime.now(timezone.utc)
    q = Quote(symbol="ABC", bid=Decimal("99.95"), ask=Decimal("100.05"), observed_at=live_now)
    open_s = _inputs({})
    # Sprint 1 pipeline opens 2 units
    r1 = SignalPipeline(journal, _Stub(_candidate(open_s)), policy).process(
        SourceEvent(source_id="p1", text="x", published_at=live_now), q)
    # Replay opens 2 units from the same instruction
    r2 = replay.process(_source("p1", published_at=live_now, received_at=live_now),
                        ParsedPost(actions=[_trade_action(open_s)]), {"ABC": q})
    assert r1.decision.status == "EXECUTE_MOCK" and r2.outcomes[0].status == "SIMULATED"
    assert journal.position("p1").quantity == 2 == replay.ideas()[0]["quantity"]
    assert r1.order.quantity == r2.outcomes[0].order.quantity == 2
    # Both reject the same collar breach without touching state
    wide = Quote(symbol="ABC", bid=Decimal("103.9"), ask=Decimal("104.0"), observed_at=live_now)
    r3 = SignalPipeline(journal, _Stub(_candidate(open_s)), policy).process(
        SourceEvent(source_id="p2", text="x", published_at=live_now), wide)
    r4 = replay.process(_source("p2", published_at=live_now, received_at=live_now),
                        ParsedPost(actions=[_trade_action(open_s)]), {"ABC": wide})
    assert r3.decision.status == "BLOCK" and r4.outcomes[0].status == "BLOCK"
    assert r3.decision.reasons == ["PRICE_COLLAR_BREACH"] and r4.outcomes[0].reasons == ["ENTRY_PRICE_COLLAR"]
    assert LEGACY_REASON_CODES["ENTRY_PRICE_COLLAR"] == "PRICE_COLLAR_BREACH"
    # Both close the referenced idea flat
    close_s = _inputs({"action": Action.CLOSE, "idea": "p1", "ref": None, "stop": None, "target": None, "side": None})
    r5 = SignalPipeline(journal, _Stub(_candidate(close_s)), policy).process(
        SourceEvent(source_id="p3", text="x", published_at=live_now), q)
    close_a = _trade_action(close_s).model_copy(update={"idea_reference": "p1:a1"})
    r6 = replay.process(_source("p3", published_at=live_now, received_at=live_now),
                        ParsedPost(actions=[close_a]), {"ABC": q})
    assert r5.order.side == r6.outcomes[0].order.side == "SELL"
    assert r5.order.quantity == r6.outcomes[0].order.quantity == 2
    assert journal.position("p1") is None and replay.ideas()[0]["quantity"] == 0


# ----------------------------------------------------------------------------- hydration of truncated posts

def _open_action(aid="open"):
    return _trade_action(_inputs({"symbol": "TLT", "cls": AssetClass.ETF})).model_copy(update={"action_id": aid})


def _tlt(bid="99.95", ask="100.05", at=NOW):
    return {"TLT": Quote(symbol="TLT", bid=Decimal(bid), ask=Decimal(ask), observed_at=at)}


def test_truncated_post_holds_then_completes_exactly_once(tmp_path):
    e = PaperEventReplay(tmp_path / "p.sqlite3")
    held = e.process(_source("t", complete=False), ParsedPost(actions=[_open_action()]), _tlt(), now=NOW)
    assert held.outcomes[0].status == "HOLD" and held.outcomes[0].reasons == ["SOURCE_TEXT_TRUNCATED"]
    assert not held.hydrated and e.ideas() == []
    # Same source_id and revision, complete text (different hash): evaluated once as hydration
    done = e.process(_source("t", complete=True), ParsedPost(actions=[_open_action()]), _tlt(), now=NOW)
    assert done.hydrated and not done.duplicate and done.outcomes[0].status == "SIMULATED"
    assert e.ideas()[0]["quantity"] == 1
    # Replaying the complete post is a duplicate that returns the hydrated outcomes, no second fill
    again = e.process(_source("t", complete=True), ParsedPost(actions=[_open_action()]), _tlt(), now=NOW)
    assert again.duplicate and again.outcomes[0].status == "SIMULATED" and e.ideas()[0]["quantity"] == 1
    assert [o.status for o in e.outcomes("t")] == ["SIMULATED"]
    # Any further content change under the same revision is blocked: the row is sealed
    tampered = e.process(_source("t", complete=True, post_hash="c" * 64), ParsedPost(actions=[_open_action()]),
                         _tlt(), now=NOW)
    assert tampered.duplicate and tampered.outcomes[0].reasons == ["SOURCE_HASH_CHANGED_WITHOUT_REVISION"]
    assert e.ideas()[0]["quantity"] == 1


def test_complete_post_cannot_be_downgraded_or_rehydrated(tmp_path):
    e = PaperEventReplay(tmp_path / "p.sqlite3")
    e.process(_source("c", complete=True), ParsedPost(actions=[_open_action()]), _tlt(), now=NOW)
    flipped = e.process(_source("c", complete=False), ParsedPost(actions=[_open_action("again")]),
                        _tlt(), now=NOW)
    assert flipped.duplicate and flipped.outcomes[0].reasons == ["SOURCE_HASH_CHANGED_WITHOUT_REVISION"]
    assert len(e.ideas()) == 1 and e.ideas()[0]["quantity"] == 1


def test_hydration_does_not_bypass_edit_or_revision_protection(tmp_path):
    e = PaperEventReplay(tmp_path / "p.sqlite3")
    e.process(_source("x", complete=False), ParsedPost(actions=[_open_action()]), _tlt(), now=NOW)
    e.process(_source("x", complete=True, revision=2), ParsedPost(actions=[_open_action()]),
              _tlt(), now=NOW)
    late_v1 = e.process(_source("x", complete=True), ParsedPost(actions=[_open_action()]),
                        _tlt(), now=NOW)
    assert late_v1.hydrated and late_v1.outcomes[0].reasons == ["SOURCE_EDIT_REQUIRES_REVIEW"]
    v2 = e.process(_source("y", complete=False, revision=2), ParsedPost(actions=[_open_action()]),
                   _tlt(), now=NOW)
    v2_full = e.process(_source("y", complete=True, revision=2), ParsedPost(actions=[_open_action()]),
                        _tlt(), now=NOW)
    assert v2.outcomes[0].reasons == ["SOURCE_EDIT_REQUIRES_REVIEW"]
    assert v2_full.outcomes[0].reasons == ["SOURCE_EDIT_REQUIRES_REVIEW"]
    assert e.ideas() == []


def test_late_hydration_is_stale_and_second_truncated_variant_is_blocked(tmp_path):
    e = PaperEventReplay(tmp_path / "p.sqlite3")
    e.process(_source("l", complete=False), ParsedPost(actions=[_open_action()]), _tlt(), now=NOW)
    other_truncation = e.process(_source("l", complete=False, post_hash="d" * 64),
                                 ParsedPost(actions=[_open_action()]), _tlt(), now=NOW)
    assert other_truncation.duplicate
    assert other_truncation.outcomes[0].reasons == ["SOURCE_HASH_CHANGED_WITHOUT_REVISION"]
    later = NOW + timedelta(minutes=10)
    stale = e.process(_source("l", complete=True, received_at=later),
                      ParsedPost(actions=[_open_action()]), _tlt(at=later), now=later)
    assert stale.hydrated and stale.outcomes[0].reasons == ["SOURCE_STALE_OR_FUTURE"]
    assert e.ideas() == []


def test_gate_source_rejects_receipt_before_publication():
    bad = _source("r", received_at=NOW - timedelta(seconds=1))
    assert gate_source(bad, NOW).reasons == ["SOURCE_RECEIPT_BEFORE_PUBLICATION"]
    assert gate_source(_source("ok"), NOW) is None


# ----------------------------------------------------------------------------- connection hygiene

def _track_connections(monkeypatch):
    counts = {"opened": 0, "closed": 0}
    real_connect = sqlite3.connect

    class Tracked(sqlite3.Connection):
        def close(self):
            counts["closed"] += 1
            super().close()

    def connect(*args, **kwargs):
        counts["opened"] += 1
        kwargs.setdefault("factory", Tracked)
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(event_replay.sqlite3, "connect", connect)
    monkeypatch.setattr(storage.sqlite3, "connect", connect)
    return counts


def test_replay_and_journal_close_every_connection(tmp_path, monkeypatch):
    counts = _track_connections(monkeypatch)
    e = PaperEventReplay(tmp_path / "p.sqlite3")
    e.process(_source("a"), ParsedPost(actions=[_open_action()]), _tlt(), now=NOW)
    # second call takes the duplicate path
    e.process(_source("a"), ParsedPost(actions=[_open_action()]), _tlt(), now=NOW)
    for _ in range(5):
        e.ideas()
        e.outcomes("a")
    j = Journal(tmp_path / "j.sqlite3")
    live_now = datetime.now(timezone.utc)
    SignalPipeline(j, _Stub(_candidate(_inputs({}))), MockPolicy()).process(
        SourceEvent(source_id="s", text="x", published_at=live_now),
        Quote(symbol="ABC", bid=Decimal("99.95"), ask=Decimal("100.05"), observed_at=live_now),
    )
    j.positions()
    j.recent_events()
    j.position("s")
    j.mark_error("zzz", "noop")
    assert counts["opened"] > 10
    assert counts["opened"] == counts["closed"]


def test_exception_inside_process_rolls_back_and_closes(tmp_path, monkeypatch):
    counts = _track_connections(monkeypatch)
    e = PaperEventReplay(tmp_path / "p.sqlite3")

    class Boom(Exception):
        pass

    monkeypatch.setattr(e, "_simulate", lambda *a, **k: (_ for _ in ()).throw(Boom()))
    with pytest.raises(Boom):
        e.process(_source("crash"), ParsedPost(actions=[_open_action()]), _tlt(), now=NOW)
    monkeypatch.undo()
    counts2 = _track_connections(monkeypatch)
    assert e.outcomes("crash") == [] and e.ideas() == []
    ok = e.process(_source("crash"), ParsedPost(actions=[_open_action()]), _tlt(), now=NOW)
    assert not ok.duplicate and ok.outcomes[0].status == "SIMULATED"
    assert counts["opened"] == counts["closed"] and counts2["opened"] == counts2["closed"]
