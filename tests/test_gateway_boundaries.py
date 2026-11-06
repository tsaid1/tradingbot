"""S2-01 offline gateway regression specification: synthetic cases that pin the EXISTING
deterministic boundaries of validator.py and the Sprint 1.1 coordinator.

Each test closes a gap recorded in the S2-01 coverage map (QA_BOUNDARY_COVERAGE.md, AO artifact):
price-basis and identity rules on follow-ups, quote integrity and freshness edges, fully specified
derivative contracts, unauthenticated or timezone-naive provenance, historical / uncertain timing
and the exact legacy (Sprint 1) status and reason vocabulary. No policy, schema or engine change;
every symbol, price, hash and timestamp here is invented. No broker, no network.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
from typing import get_args

import pytest

from tradingbot.event_models import ParsedPost, PriceBasis, ReplaySource, Timing, TradeAction
from tradingbot.event_replay import PaperEventReplay
from tradingbot.interpreters import DemoInterpreter
from tradingbot.models import (
    Action,
    AssetClass,
    GateDecision,
    Instrument,
    MockPolicy,
    Position,
    PositionSide,
    Quote,
    SignalCandidate,
    SourceEvent,
)
from tradingbot.pipeline import SignalPipeline
from tradingbot.storage import Journal
from tradingbot.validator import (
    LEGACY_STATUS,
    gate_action,
    gate_source,
    to_legacy_decision,
    validate,
)

NOW = datetime(2026, 11, 6, 15, 0, tzinfo=timezone.utc)
ETF = Instrument(asset_class=AssetClass.ETF, symbol="ZZT")
OPTION = Instrument(asset_class=AssetClass.OPTION, symbol="ZZT", underlying="ZZT", expiry="2026-12-18",
                    strike="100", option_right="CALL", exchange="SYNTH")
FUTURE = Instrument(asset_class=AssetClass.FUTURE, symbol="ZZF", underlying="ZZF", expiry="2026-12-18",
                    exchange="SYNTH", futures_contract="ZZFZ6")
EVIDENCE = ["SYNTHETIC_S2_01"]
_SAME = object()


# ----------------------------------------------------------------------------- synthetic helpers

def src(sid, **overrides) -> ReplaySource:
    kw = {"source_id": sid, "published_at": NOW, "received_at": NOW, "verified_author": True,
          "complete_text": True, "post_hash": sha256(f"S2-01:{sid}".encode()).hexdigest()}
    kw.update(overrides)
    return ReplaySource(**kw)


def quote(symbol="ZZT", bid="99.95", ask="100.05", observed_at=NOW) -> Quote:
    return Quote(symbol=symbol, bid=Decimal(bid), ask=Decimal(ask), observed_at=observed_at)


def act(aid, action, *, timing=Timing.CURRENT, instrument=ETF, side=None, idea=None, ref=None, stop=None,
        target=None, fraction=None, group=None, basis=PriceBasis.TRADED_CONTRACT, market=_SAME,
        explicit=True, evidence=None, ambiguities=None) -> TradeAction:
    if market is _SAME:
        market = instrument.symbol if instrument is not None else None
    return TradeAction(action_id=aid, action=action, timing=timing, instrument=instrument, position_side=side,
                       idea_reference=idea, atomic_group=group, price_basis=basis, source_market_symbol=market,
                       entry_reference=ref, stop_price=stop, target_price=target, close_fraction=fraction,
                       explicit_instruction=explicit, evidence=EVIDENCE if evidence is None else evidence,
                       ambiguities=ambiguities or [])


def open_action(aid="open", instrument=ETF) -> TradeAction:
    return act(aid, Action.OPEN, instrument=instrument, side=PositionSide.LONG, ref="100", stop="95", target="110")


def engine(tmp_path, **policy) -> PaperEventReplay:
    policy.setdefault("quantity", 2)
    return PaperEventReplay(tmp_path / "s2_01.sqlite3", MockPolicy(**policy))


def run(e, sid, *actions, quotes=None, now=NOW, **source_overrides):
    return e.process(src(sid, **source_overrides), ParsedPost(actions=list(actions)),
                     {"ZZT": quote()} if quotes is None else quotes, now=now)


def opened(tmp_path, **policy) -> PaperEventReplay:
    """A coordinator holding one fictional long idea 'base:open': 2 units, stop 95, target 110."""
    e = engine(tmp_path, **policy)
    assert run(e, "base", open_action()).outcomes[0].status == "SIMULATED"
    return e


def ledger(e, idea_id="base:open") -> tuple:
    row = next(i for i in e.ideas() if i["idea_id"] == idea_id)
    return row["quantity"], row["stop_price"], row["target_price"], row["status"]


def legacy_candidate(**overrides) -> SignalCandidate:
    kw = {"action": Action.OPEN, "instrument": ETF, "side": PositionSide.LONG, "entry_reference": "100",
          "stop_price": "95", "target_price": "110", "explicit_instruction": True, "evidence": EVIDENCE}
    kw.update(overrides)
    return SignalCandidate(**kw)


def legacy(candidate, q, policy=None, position=None, published_at=NOW):
    return validate(SourceEvent(source_id="s2-01", text="synthetic", published_at=published_at), candidate, q,
                    policy or MockPolicy(), position, now=NOW)


# ----------------------------------------------------------------------------- A. price basis and identity

def test_omitted_price_basis_defaults_to_unknown_and_holds_even_with_an_orderable_instrument(tmp_path):
    e = engine(tmp_path)
    omitted = TradeAction(action_id="o", action=Action.OPEN, timing=Timing.CURRENT, instrument=ETF,
                          position_side=PositionSide.LONG, source_market_symbol="ZZT", entry_reference="100",
                          stop_price="95", explicit_instruction=True, evidence=EVIDENCE)
    assert omitted.price_basis == PriceBasis.UNKNOWN
    out = run(e, "unknown-basis", omitted)
    assert out.outcomes[0].status == "HOLD"
    assert out.outcomes[0].reasons == ["SOURCE_TO_BROKER_PRICE_BASIS_UNRESOLVED"]
    assert e.ideas() == []


@pytest.mark.parametrize("name, follow_up, reason", [
    ("series_close", {"action": Action.CLOSE, "basis": PriceBasis.SOURCE_SERIES},
     "SOURCE_TO_BROKER_PRICE_BASIS_UNRESOLVED"),
    ("unknown_reduce", {"action": Action.REDUCE, "fraction": "0.5", "basis": PriceBasis.UNKNOWN},
     "SOURCE_TO_BROKER_PRICE_BASIS_UNRESOLVED"),
    ("unmapped_modify", {"action": Action.MODIFY, "stop": "97", "market": "ZZTX"},
     "SOURCE_TO_BROKER_MAPPING_UNAPPROVED"),
    ("lowercase_identity_close", {"action": Action.CLOSE, "market": "zzt"}, "SOURCE_TO_BROKER_MAPPING_UNAPPROVED"),
    ("identity_missing_close", {"action": Action.CLOSE, "market": None},
     "SOURCE_PRICE_INSTRUMENT_IDENTITY_UNVERIFIED"),
])
def test_price_basis_and_identity_rules_hold_follow_ups_before_the_position_is_consulted(tmp_path, name,
                                                                                         follow_up, reason):
    e = opened(tmp_path)
    kwargs = dict(follow_up)
    action = kwargs.pop("action")
    out = run(e, name, act("f", action, idea="base:open", **kwargs))
    assert out.outcomes[0].status == "HOLD" and out.outcomes[0].reasons == [reason]
    assert out.outcomes[0].order is None
    assert ledger(e) == (2, "95", "110", "OPEN")


# ----------------------------------------------------------------------------- B. quote integrity and freshness

@pytest.mark.parametrize("clock, age_seconds, status, reason", [
    ("quote", 30, "APPROVE", "PAPER_OPEN"), ("quote", 31, "BLOCK", "QUOTE_STALE_OR_FUTURE"),
    ("quote", -5, "APPROVE", "PAPER_OPEN"), ("quote", -6, "BLOCK", "QUOTE_STALE_OR_FUTURE"),
    ("source", 90, "APPROVE", "PAPER_OPEN"), ("source", 91, "BLOCK", "SOURCE_STALE_OR_FUTURE"),
    ("source", -5, "APPROVE", "PAPER_OPEN"), ("source", -6, "BLOCK", "SOURCE_STALE_OR_FUTURE"),
])
def test_freshness_windows_are_inclusive_with_a_five_second_future_tolerance(clock, age_seconds, status, reason):
    assert MockPolicy().max_quote_age_seconds == 30 and MockPolicy().max_signal_age_seconds == 90
    published = NOW - timedelta(seconds=age_seconds) if clock == "source" else NOW
    observed = NOW - timedelta(seconds=age_seconds) if clock == "quote" else NOW
    gate = gate_action(open_action(), published, quote(observed_at=observed), MockPolicy(), None, now=NOW)
    assert (gate.status, gate.reasons) == (status, [reason])


def test_quote_without_a_timezone_is_unavailable_in_both_engines():
    naive = quote(observed_at=NOW.replace(tzinfo=None))
    gate = gate_action(open_action(), NOW, naive, MockPolicy(), None, now=NOW)
    assert (gate.status, gate.reasons) == ("BLOCK", ["QUOTE_UNAVAILABLE"])
    decision = legacy(legacy_candidate(), naive)
    assert (decision.status, decision.reasons) == ("BLOCK", ["QUOTE_UNAVAILABLE"])


@pytest.mark.parametrize("bid, ask", [("0", "100.05"), ("99.95", "0"), ("-99.95", "100.05"), ("100.05", "99.95")],
                         ids=["zero_bid", "zero_ask", "negative_bid", "crossed"])
def test_invalid_or_crossed_quotes_are_blocked_in_both_engines(tmp_path, bid, ask):
    e = engine(tmp_path)
    out = run(e, "bad-quote", open_action(), quotes={"ZZT": quote(bid=bid, ask=ask)})
    assert out.outcomes[0].status == "BLOCK" and out.outcomes[0].reasons == ["INVALID_BID_ASK"]
    assert e.ideas() == []
    decision = legacy(legacy_candidate(), quote(bid=bid, ask=ask))
    assert (decision.status, decision.reasons) == ("BLOCK", ["INVALID_BID_ASK"])


def test_exit_with_a_stale_or_missing_quote_is_blocked_and_the_idea_stays_open(tmp_path):
    e = opened(tmp_path)
    close = act("x", Action.CLOSE, idea="base:open")
    stale = run(e, "stale-exit", close, quotes={"ZZT": quote(observed_at=NOW - timedelta(seconds=31))})
    assert stale.outcomes[0].status == "BLOCK" and stale.outcomes[0].reasons == ["QUOTE_STALE_OR_FUTURE"]
    # a quote for some other symbol is no quote for the orderable instrument
    missing = run(e, "other-symbol-quote", close, quotes={"ZZQ": quote("ZZQ")})
    assert missing.outcomes[0].status == "BLOCK" and missing.outcomes[0].reasons == ["QUOTE_UNAVAILABLE"]
    assert all(o.order is None for o in stale.outcomes + missing.outcomes)
    assert ledger(e) == (2, "95", "110", "OPEN")
    fresh = run(e, "fresh-exit", close)
    assert fresh.outcomes[0].status == "SIMULATED" and fresh.outcomes[0].order.quantity == 2
    assert ledger(e) == (0, "95", "110", "CLOSED")

    # Same rule in the Sprint 1 journal: a blocked exit leaves the position; a fresh quote closes it.
    live = datetime.now(timezone.utc)
    journal = Journal(tmp_path / "legacy.sqlite3")
    pipeline = SignalPipeline(journal, DemoInterpreter(), MockPolicy(quantity=2))

    def live_quote(age=0):
        return Quote(symbol="ABC", bid=Decimal("99.95"), ask=Decimal("100.05"),
                     observed_at=live - timedelta(seconds=age))

    def event(sid, text):
        return SourceEvent(source_id=sid, text=text, published_at=live)

    assert pipeline.process(event("entry", "BUY ABC @ 100 STOP 95 TARGET 110"), live_quote()).decision.status == "EXECUTE_MOCK"
    blocked = pipeline.process(event("stale-exit", "CLOSE ABC IDEA entry"), live_quote(age=200))
    assert blocked.decision.status == "BLOCK" and blocked.decision.reasons == ["STALE_OR_FUTURE_QUOTE"]
    assert blocked.order is None and journal.position("entry").quantity == 2
    assert pipeline.process(event("fresh-exit", "CLOSE ABC IDEA entry"), live_quote()).decision.status == "EXECUTE_MOCK"
    assert journal.position("entry") is None


# ----------------------------------------------------------------------------- C. options and futures

@pytest.mark.parametrize("instrument", [OPTION, FUTURE], ids=["option", "future"])
def test_fully_specified_derivative_contracts_hold_for_every_action_even_when_allowlisted(tmp_path, instrument):
    e = engine(tmp_path, allowed_symbols={"ZZT", "ZZF"})
    sym = instrument.symbol
    quotes = {sym: quote(sym)}
    out = run(e, "deriv-open", open_action("o", instrument), quotes=quotes)
    assert out.outcomes[0].status == "HOLD" and out.outcomes[0].reasons == ["ASSET_CLASS_PAPER_ADAPTER_UNSUPPORTED"]
    assert e.ideas() == []
    follow_ups = [act("c", Action.CLOSE, instrument=instrument, idea="deriv-open:o"),
                  act("r", Action.REDUCE, instrument=instrument, idea="deriv-open:o", fraction="0.5"),
                  act("a", Action.ADD, instrument=instrument, idea="deriv-open:o", ref="100"),
                  act("m", Action.MODIFY, instrument=instrument, idea="deriv-open:o", stop="97")]
    out = run(e, "deriv-follow-ups", *follow_ups, quotes=quotes)
    assert [o.status for o in out.outcomes] == ["HOLD"] * 4
    assert all(o.reasons == ["ASSET_CLASS_PAPER_ADAPTER_UNSUPPORTED"] and o.order is None for o in out.outcomes)
    assert e.ideas() == []
    # Legacy vocabulary for a derivative exit (the OPEN case is pinned in test_pipeline.py)
    held = Position(idea_id="d", symbol=sym, asset_class=instrument.asset_class, side=PositionSide.LONG, quantity=2)
    decision = legacy(legacy_candidate(action=Action.CLOSE, instrument=instrument, side=None, entry_reference=None,
                                       stop_price=None, target_price=None, idea_reference="d"),
                      quote(sym), position=held)
    assert (decision.status, decision.reasons) == ("REVIEW", ["MOCK_EXECUTION_EQUITIES_ONLY"])


# ----------------------------------------------------------------------------- D. provenance

def test_unauthenticated_post_blocks_every_action_and_the_revision_stays_sealed(tmp_path):
    e = opened(tmp_path)
    actions = [act("exit", Action.CLOSE, idea="base:open", group="same-sentence"),
               act("tighten", Action.MODIFY, idea="base:open", stop="97", group="same-sentence"),
               open_action("new")]
    unverified = run(e, "unauth", *actions, verified_author=False)
    assert [o.status for o in unverified.outcomes] == ["BLOCK"] * 3
    assert all(o.reasons == ["SOURCE_NOT_AUTHENTICATED"] and o.order is None for o in unverified.outcomes)
    assert len(e.ideas()) == 1 and ledger(e) == (2, "95", "110", "OPEN")
    # Re-delivery of the same revision (same digest), now claiming a verified author, is a duplicate:
    # once-only processing returns the recorded BLOCKs and never re-evaluates.
    later = run(e, "unauth", *actions, verified_author=True)
    assert later.duplicate and [o.status for o in later.outcomes] == ["BLOCK"] * 3
    assert [o.status for o in e.outcomes("unauth")] == ["BLOCK"] * 3
    # A new revision of the same post is the only other path, and revisions are held for review.
    revised = run(e, "unauth", *actions, verified_author=True, revision=2, post_hash="e" * 64)
    assert [o.status for o in revised.outcomes] == ["HOLD"] * 3
    assert all(o.reasons == ["SOURCE_EDIT_REQUIRES_REVIEW"] for o in revised.outcomes)
    assert len(e.ideas()) == 1 and ledger(e) == (2, "95", "110", "OPEN")


def test_timestamps_without_a_timezone_are_blocked_at_both_gate_levels():
    naive = NOW.replace(tzinfo=None)
    for post in (src("naive-published", published_at=naive), src("naive-received", received_at=naive)):
        gate = gate_source(post, NOW)
        assert (gate.status, gate.reasons) == ("BLOCK", ["SOURCE_TIMEZONE_MISSING"])
    gate = gate_source(src("naive-clock"), naive)
    assert (gate.status, gate.reasons) == ("BLOCK", ["SOURCE_TIMEZONE_MISSING"])
    gate = gate_action(open_action(), naive, quote(), MockPolicy(), None, now=NOW)
    assert (gate.status, gate.reasons) == ("BLOCK", ["SOURCE_TIMEZONE_MISSING"])
    decision = legacy(legacy_candidate(), quote(), published_at=naive)
    assert (decision.status, decision.reasons) == ("BLOCK", ["SOURCE_TIMESTAMP_MISSING_TIMEZONE"])


# ----------------------------------------------------------------------------- E. historical and uncertain timing

@pytest.mark.parametrize("action, fields", [
    (Action.OPEN, {"side": PositionSide.LONG, "ref": "100", "stop": "95"}),
    (Action.ADD, {"idea": "base:open", "ref": "100"}),
    (Action.REDUCE, {"idea": "base:open", "fraction": "0.5"}),
    (Action.CLOSE, {"idea": "base:open"}),
    (Action.MODIFY, {"idea": "base:open", "stop": "97"}),
], ids=lambda v: v.value if isinstance(v, Action) else "")
def test_historical_timing_reconciles_every_action_before_quote_freshness_or_instrument_rules(tmp_path, action,
                                                                                           fields):
    e = opened(tmp_path)
    # No quote at all and a three-day-old post: neither is consulted, the report is reconciled, never filled.
    report = run(e, f"hist-{action.value}", act("h", action, timing=Timing.HISTORICAL, **fields),
                 quotes={}, published_at=NOW - timedelta(days=3), received_at=NOW)
    assert report.outcomes[0].status == "RECONCILE" and report.outcomes[0].reasons == ["HISTORICAL_REPORT_NO_ORDER"]
    assert report.outcomes[0].order is None
    # A report quoted at an index or spot level reconciles as well; the series rule never runs.
    bare = run(e, f"hist-bare-{action.value}",
               act("b", action, timing=Timing.HISTORICAL, instrument=None, basis=PriceBasis.SOURCE_SERIES,
                   market="ZZTX", **fields), quotes={})
    assert bare.outcomes[0].status == "RECONCILE" and bare.outcomes[0].reasons == ["HISTORICAL_REPORT_NO_ORDER"]
    assert len(e.ideas()) == 1 and ledger(e) == (2, "95", "110", "OPEN")


@pytest.mark.parametrize("timing, status, reason", [
    (Timing.UNCERTAIN, "HOLD", "TEMPORAL_INTENT_UNCERTAIN"),
    (Timing.COMMENTARY, "NO_TRADE", "COMMENTARY_OR_SUMMARY"),
])
def test_uncertain_and_commentary_timing_never_fill_an_otherwise_valid_exit(tmp_path, timing, status, reason):
    e = opened(tmp_path)
    out = run(e, f"timing-{timing.value}", act("t", Action.CLOSE, timing=timing, idea="base:open"))
    assert (out.outcomes[0].status, out.outcomes[0].reasons) == (status, [reason])
    assert out.outcomes[0].order is None and ledger(e) == (2, "95", "110", "OPEN")


def test_a_historical_leg_inside_an_atomic_group_rejects_the_current_leg(tmp_path):
    e = opened(tmp_path)
    grouped = run(e, "mixed-group",
                  act("now", Action.CLOSE, idea="base:open", group="same-sentence"),
                  act("then", Action.CLOSE, timing=Timing.HISTORICAL, idea="base:open", group="same-sentence"))
    assert [o.status for o in grouped.outcomes] == ["HOLD", "HOLD"]
    assert grouped.outcomes[0].reasons == ["ATOMIC_GROUP_REJECTED", "PAPER_REDUCE_ONLY"]
    assert grouped.outcomes[1].reasons == ["ATOMIC_GROUP_REJECTED", "HISTORICAL_REPORT_NO_ORDER"]
    assert all(o.order is None for o in grouped.outcomes) and ledger(e) == (2, "95", "110", "OPEN")
    # The same two clauses as independent legs: the current exit fills, the report reconciles.
    split = run(e, "split-legs",
                act("now", Action.CLOSE, idea="base:open"),
                act("then", Action.CLOSE, timing=Timing.HISTORICAL, idea="base:open"))
    assert [o.status for o in split.outcomes] == ["SIMULATED", "RECONCILE"]
    assert ledger(e) == (0, "95", "110", "CLOSED")


def test_only_approve_translates_to_a_legacy_execute():
    canonical = set(get_args(GateDecision.model_fields["status"].annotation))
    assert canonical == set(LEGACY_STATUS)                       # every canonical status has a translation
    assert [c for c, legacy_status in LEGACY_STATUS.items() if legacy_status == "EXECUTE_MOCK"] == ["APPROVE"]
    reconciled = to_legacy_decision(GateDecision(status="RECONCILE", reasons=["HISTORICAL_REPORT_NO_ORDER"]))
    assert (reconciled.status, reconciled.reasons) == ("REVIEW", ["HISTORICAL_REPORT_NO_ORDER"])


# ----------------------------------------------------------------------------- F. legacy vocabulary lock

_OPEN_POSITION = Position(idea_id="e", symbol="ZZT", asset_class=AssetClass.ETF, side=PositionSide.LONG, quantity=10)
_FOLLOW_UP = {"side": None, "entry_reference": None, "stop_price": None, "target_price": None, "idea_reference": "e"}

LEGACY_VOCABULARY = {
    # name: (candidate overrides, quote, policy kwargs, position, legacy status, legacy reasons)
    "conditional": ({"conditional": True}, quote(), {}, None, "REVIEW", ["CONDITIONAL_SIGNAL_UNSUPPORTED"]),
    "not_explicit": ({"explicit_instruction": False}, quote(), {}, None, "REVIEW",
                     ["NO_EXPLICIT_VERIFIABLE_INSTRUCTION"]),
    "no_evidence": ({"evidence": []}, quote(), {}, None, "REVIEW", ["NO_EXPLICIT_VERIFIABLE_INSTRUCTION"]),
    "ambiguous": ({"ambiguities": ["WHICH_POSITION"]}, quote(), {}, None, "REVIEW",
                  ["AMBIGUOUS_SOURCE", "WHICH_POSITION"]),
    "no_instrument": ({"instrument": None}, quote(), {}, None, "REVIEW", ["MISSING_INSTRUMENT"]),
    "lowercase_symbol": ({"instrument": Instrument(asset_class=AssetClass.ETF, symbol="zzt")}, quote("zzt"), {},
                         None, "BLOCK", ["INVALID_SYMBOL"]),
    "not_allowlisted": ({}, quote(), {"allowed_symbols": {"ZZQ"}}, None, "BLOCK", ["INSTRUMENT_NOT_ALLOWLISTED"]),
    "quote_missing": ({}, None, {}, None, "BLOCK", ["QUOTE_UNAVAILABLE"]),
    "open_references_idea": ({"idea_reference": "e"}, quote(), {}, None, "REVIEW",
                             ["OPEN_SHOULD_NOT_REFERENCE_EXISTING_IDEA"]),
    "direction_missing": ({"side": None}, quote(), {}, None, "REVIEW", ["DIRECTION_MISSING"]),
    "entry_missing": ({"entry_reference": None}, quote(), {}, None, "REVIEW", ["NO_EXPLICIT_ENTRY_REFERENCE"]),
    "malformed_stop": ({"stop_price": "ninety"}, quote(), {}, None, "BLOCK", ["INVALID_STOP_PRICE"]),
    "malformed_target": ({"target_price": "high"}, quote(), {}, None, "BLOCK", ["INVALID_TARGET_PRICE"]),
    "target_contradiction": ({"target_price": "90"}, quote(), {}, None, "BLOCK", ["TARGET_SIDE_CONTRADICTION"]),
    "notional_cap": ({}, quote(), {"max_notional_usd": Decimal("50")}, None, "BLOCK", ["MOCK_NOTIONAL_CAP"]),
    "approved_open": ({}, quote(), {}, None, "EXECUTE_MOCK", ["PREFLIGHT_OK_MOCK_ONLY"]),
    "close_unknown_idea": (dict(action=Action.CLOSE, **{**_FOLLOW_UP, "idea_reference": "nope"}), quote(), {}, None,
                           "REVIEW", ["REFERENCED_POSITION_NOT_FOUND"]),
    "close_wrong_instrument": (dict(action=Action.CLOSE, **_FOLLOW_UP), quote(), {},
                               _OPEN_POSITION.model_copy(update={"symbol": "ZZQ"}), "BLOCK",
                               ["POSITION_INSTRUMENT_MISMATCH"]),
    "close_wrong_side": (dict(action=Action.CLOSE, **{**_FOLLOW_UP, "side": PositionSide.SHORT}), quote(), {},
                         _OPEN_POSITION, "BLOCK", ["IDEA_SIDE_MISMATCH"]),
    "reduce_bad_fraction": (dict(action=Action.REDUCE, close_fraction="1.5", **_FOLLOW_UP), quote(), {},
                            _OPEN_POSITION, "REVIEW", ["INVALID_REDUCE_FRACTION"]),
    "approved_close": (dict(action=Action.CLOSE, **_FOLLOW_UP), quote(), {}, _OPEN_POSITION, "EXECUTE_MOCK",
                       ["REDUCE_ONLY_MOCK_OK"]),
}


@pytest.mark.parametrize("name", sorted(LEGACY_VOCABULARY))
def test_legacy_status_and_reason_vocabulary_is_unchanged(name):
    overrides, q, policy_kw, position, status, reasons = LEGACY_VOCABULARY[name]
    decision = legacy(legacy_candidate(**overrides), q, MockPolicy(**policy_kw), position)
    assert (decision.status, decision.reasons) == (status, reasons), name
