"""Operator confirmation, milestone 1: the closed table, the waiver's four safeguards, the
pre-check, the binding hash and the evaluation hook. Pure; invented posts and markets only.
"""

from __future__ import annotations

import inspect
import json
import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tradingbot.execution.broker import ContractRef
from tradingbot.models import AssetClass, MappedQuote, MockPolicy
from tradingbot.runtime import confirmation as C
from tradingbot.runtime import confirmation_table as T
from tradingbot.runtime.mapping import (
    MappingPolicy,
    WaivedDecisionRefused,
    WaivedResolution,
    ensure_production,
)
from tradingbot.source_events import Provenance, SourcePost
from tradingbot.tkl_interpreter import TklInterpreter, group_linked_actions

SRC = Path(__file__).resolve().parents[1] / "src" / "tradingbot"
PUBLISHED = datetime(2026, 6, 1, 14, 0, tzinfo=UTC)  # Monday, inside the invented session
ENTRY = """version = 2
[[mapping]]
family = "QETF"
basis = "DIRECT"
approved = true
approved_by = "test-owner"
approved_at = 2026-05-29T12:00:00Z
reference = { sec_type = "STK", asset_class = "ETF", symbol = "QETF", exchange = "SMART", currency = "USD" }
execution = { sec_type = "STK", asset_class = "ETF", symbol = "QETF", exchange = "SMART", currency = "USD" }
sizing = { units_per_signal = 1, max_units = 1, max_notional = "150" }
session = { policy = "REGULAR_ONLY", timezone = "UTC", days = ["MON", "TUE", "WED", "THU", "FRI"], regular = ["13:00-21:00"] }
long_only = true
max_stop_distance = "5%"
entry_rule = "REFERENCE_AS_COLLAR"
entry_anchor = { max_adverse_move = "0.3%", max_favourable_move = "0.6%", max_signal_age_seconds = 90, anchor_scope = "SINGLE_MARKET_POST", max_quote_age_seconds = 3, environment = "PAPER", standalone_level_sentence = true }
"""
QETF = ContractRef(symbol="QETF", asset_class=AssetClass.ETF, con_id=7101)
POLICY = MockPolicy(max_signal_age_seconds=90, max_quote_age_seconds=30)

# Invented posts, by what the bot finds in them.
APPROX = "We are now long $QETF, ~50.10 now. Stop 48.90."
MENTION = "We are now long $QETF, 50.10 now. Stop 48.90. Quorvex sold off hard."
FOLLOWING = "We are now long $QETF. 50.10 now. Plorp zing. Stop 48.90."
QUESTION = "We are now long $QETF, 50.10 now? Stop 48.90."
UNATTRIBUTED = "We are now long $QETF, 50.10 now. Stop 48.90. Buy the dip."
WORDS_THEN_OWN_CLAUSE = "We are now long $QETF, 50.10 now plorp. Stop 48.90."
MENTION_THEN_TWO_LEVELS = "We are now long $QETF, 50.10 now. Stop 48.90. The miners at 31.20 now."
TWO_LEVELS = "We are now long $QETF, ~50.10 now, 51.30 next. Stop 48.90."
FAR_STOP = "We are now long $QETF, ~50.10 now. Stop 40.00."
CLEAN = "We are now long $QETF, 50.10 now. Stop 48.90."


@pytest.fixture
def mapping(tmp_path) -> MappingPolicy:
    path = tmp_path / "m.toml"
    path.write_text(ENTRY)
    return MappingPolicy.from_toml(path)


def source_post(text: str, *, revision: int = 1) -> SourcePost:
    return SourcePost(source_id="7001", post_id="7001", revision=revision, author_id="9",
                      verified_author=True, text=text, text_complete=True,
                      published_at=PUBLISHED, received_at=PUBLISHED + timedelta(seconds=12),
                      provenance=Provenance.SYNTHETIC)


def decide(mapping: MappingPolicy, text: str):
    """What the service does for one post: interpret, the production mapping, and (as the
    pre-check would) the waived evaluation of each held action."""
    post = source_post(text)
    interpreted = TklInterpreter().interpret(post)
    parsed = group_linked_actions(interpreted)
    kw = {"clauses": {c.action_id: c.clause for c in interpreted.classifications},
          "now": PUBLISHED, "post_text": text,
          "levels": {c.action_id: [(m.role.value, m.value) for m in c.prices]
                     for c in interpreted.classifications}}
    mapped, decisions = mapping.apply(parsed, **kw)
    return post, parsed, mapped, decisions, kw


def held(mapping, text, *, now=None, waive=T.WAIVE_ALL, quote="live", **over) -> C.HeldDecision:
    post, parsed, _mapped, decisions, kw = decide(mapping, text)
    [a] = [x for x in parsed.actions if x.action.value == "OPEN"]
    d = next(x for x in decisions if x.action_id == a.action_id)
    w = mapping.apply_waived(parsed, a.action_id, waive=waive, **kw)
    now = now or PUBLISHED + timedelta(seconds=14)
    fields = {
        "source_id": post.source_id, "post_id": post.post_id, "revision": post.revision,
        "post_hash": post.text_sha256, "published_at": post.published_at,
        "received_at": post.received_at, "verified_author": True, "text_complete": True,
        "action_id": a.action_id, "actions": tuple(parsed.actions), "production": d, "waived": w,
        "outcome_status": "HOLD" if d.status != "SUPPORTED" else "BLOCK",
        "outcome_reasons": ("SOURCE_AMBIGUITIES", *C.mapping_marks(d)),
        "entry": mapping.entries["QETF"], "policy": POLICY, "now": now,
        "quote": market(mapping, now) if quote == "live" else quote,
        "lexicon_hash": C.lexicon_hash(mapping)}
    fields.update(over)
    return C.HeldDecision(**fields)


def market(mapping, now, bid="50.09", ask="50.11", age=1.0) -> MappedQuote:
    entry = mapping.entries["QETF"]
    from tradingbot.models import Instrument
    inst = Instrument(asset_class=AssetClass.ETF, symbol="QETF")
    terms = entry.terms(inst, QETF, QETF)
    at = now - timedelta(seconds=age)
    return MappedQuote(symbol="QETF", bid=Decimal(bid), ask=Decimal(ask), observed_at=at,
                       quality="LIVE", terms=terms, bid_at=at, ask_at=at)


# ================================================================== the closed table

def _source_text() -> str:
    return "\n".join(p.read_text() for p in SRC.rglob("*.py"))


def test_every_code_and_detail_in_the_table_exists_in_the_code_base():
    src = _source_text()
    for code, key in T.TABLE:
        assert f'"{code}"' in src, code
        if key.startswith("LEVEL_KIND:"):
            from tradingbot.runtime.instruction_terms import AnchorLevelKind
            AnchorLevelKind(key.split(":", 1)[1])
        elif key:
            assert key in src, (code, key)


NEVER = [
    "UNSUPPORTED_INSTRUMENT_MAPPING", "MAPPING_NOT_APPROVED", "CONTRACT_SELECTION_AMBIGUOUS",
    "CONTRACT_MONTH_UNRECOGNISED", "FRONT_MONTH_IN_ROLL_WINDOW", "FRONT_MONTH_AMBIGUOUS",
    "OPTION_TERMS_INCOMPLETE", "OPTION_STRATEGY_NOT_SINGLE_LEG", "IDEA_CONTRACT_MISMATCH",
    "MAPPING_LONG_ONLY", "PROTECTIVE_STOP_NOT_UNIQUE", "PROTECTIVE_STOP_MISSING",
    "PROTECTIVE_STOP_TOO_FAR_FROM_ENTRY", "PROTECTIVE_STOP_TOO_FAR_FROM_LIMIT",
    "ENTRY_PRICE_IS_COST_BASIS", "ENTRY_ANCHOR_SEVERAL_LEVELS", "ENTRY_ANCHOR_SECOND_PRICE",
    "ENTRY_ANCHOR_UNREADABLE", "ENTRY_ANCHOR_NOT_IN_POST", "ENTRY_ANCHOR_SOURCE_AMBIGUOUS",
    "ENTRY_ANCHOR_INSTRUMENT_AMBIGUOUS", "ENTRY_ANCHOR_UNEXPLAINED_PRICE",
    "ENTRY_ANCHOR_POST_SEVERAL_LEVELS", "ENTRY_ANCHOR_POST_SEVERAL_MARKETS",
    "ENTRY_ANCHOR_POST_SEVERAL_ACTIONS", "ENTRY_ANCHOR_REFERENCE_UNNAMED",
    "ENTRY_TEXT_INVISIBLE_CHARACTER", "ENTRY_TEXT_NUMBER_UNREADABLE", "SIDE_UNCLEAR",
    "ASSET_UNRESOLVED", "MULTIPLE_OPEN_IDEAS_IN_CONTEXT", "EXISTING_OPEN_IDEA_SAME_SIDE_IN_CONTEXT",
    "NO_OPEN_IDEA_IN_CONTEXT", "FUTURE_INTENT_WITHOUT_EXPLICIT_CONDITION",
    "SOURCE_EDIT_REQUIRES_REVIEW", "SOURCE_TEXT_TRUNCATED", "SOURCE_NOT_AUTHENTICATED",
    "POST_BEFORE_FIRST_START", "SOURCE_STALE_OR_FUTURE", "ENTRY_ANCHOR_SIGNAL_TOO_OLD",
    "ENTRY_PRICE_COLLAR", "ENTRY_ANCHOR_ADVERSE_MOVE", "QUOTE_STALE_OR_FUTURE",
    "ENTRY_ANCHOR_QUOTE_NOT_LIVE", "NOTIONAL_LIMIT", "ENTRY_ANCHOR_PAPER_ONLY",
    "BROKER_RECONNECT_PENDING", "HALTED", "SHORT_NOT_PERMITTED_FOR_FAMILY",
]
NEVER_DETAILS = [
    ("ENTRY_ANCHOR_CONTEXT_UNCLEAR", "SECOND_NUMBER"),
    ("ENTRY_ANCHOR_LEVEL_NOT_MARKET_QUOTE", "LEVEL_KIND:STOP_OR_TARGET"),
    ("ENTRY_ANCHOR_LEVEL_NOT_MARKET_QUOTE", "CONTEXT_UNKNOWN"),
    ("ENTRY_ANCHOR_LEVEL_NOT_MARKET_QUOTE", "STANDALONE_LEVEL_NOT_ENABLED"),
    ("ENTRY_ANCHOR_LEVEL_NOT_MARKET_QUOTE", "OWN_CLAUSE"),
    ("ENTRY_ANCHOR_POST_UNRESOLVED_MARKET", "RULE:ADDITIVE_ELLIPSIS"),
]
# The lead's decision of 14 November 2026: never confirmable in version 1, reported only.
REPORT_ONLY = [
    ("HEADLINE_POSITION_LINE_WITHOUT_ENTRY_VERB", ""), ("POSITION_STATEMENT_OR_ENTRY", ""),
    ("ENTRY_ANCHOR_POST_UNATTRIBUTED_ACTION", "RULE:UNATTRIBUTED_ACTION_SENTENCE"),
    ("ENTRY_ANCHOR_NOT_CURRENT", "NOT_CURRENT_WORD"),
    ("ENTRY_ANCHOR_LEVEL_NOT_MARKET_QUOTE", "LEVEL_KIND:ENTRY_OR_COST_BASIS"),
    ("ENTRY_ANCHOR_LEVEL_NOT_MARKET_QUOTE", "LEVEL_KIND:HISTORICAL"),
    ("ENTRY_ANCHOR_CONTEXT_UNCLEAR", "QUESTION"),
]


def test_the_never_list_is_absent_and_the_reporting_tier_is_never_confirmable():
    codes = {c for c, _ in T.TABLE}
    assert not set(NEVER) & codes
    for pair in NEVER_DETAILS:
        assert pair not in T.TABLE
    for pair in REPORT_ONLY:
        assert pair in T.REPORT_ONLY and pair not in T.CONFIRMABLE


def test_default_deny_an_unknown_or_later_code_is_refused_by_the_waiver(mapping):
    with pytest.raises(ValueError):
        T.check_pairs({("ENTRY_ANCHOR_SOME_FUTURE_CODE", "")})
    with pytest.raises(ValueError):
        T.check_pairs({("MAPPING_NOT_APPROVED", "")})
    _, parsed, _, _, kw = decide(mapping, APPROX)
    with pytest.raises(ValueError):
        mapping.apply_waived(parsed, parsed.actions[0].action_id, waive={("PROTECTIVE_STOP_NOT_UNIQUE", "")}, **kw)
    with pytest.raises(TypeError):  # keyword-only
        mapping.resolve_waived(parsed.actions[0], T.WAIVE_ALL)


# ================================================================== the waiver's safeguards

def test_the_production_decision_never_takes_a_waiver():
    from tradingbot.runtime.mapping import MappingPolicy as P
    assert "waive" not in inspect.signature(P.resolve).parameters
    assert "waive" not in inspect.signature(P.apply).parameters
    for fn in (P.apply, P.resolve, P.decide):
        assert "resolve_waived" not in inspect.getsource(fn)
        assert "apply_waived" not in inspect.getsource(fn)
    service = (SRC / "runtime" / "service.py").read_text()
    # With the switch off the waived evaluation is never run (milestone 2 wires it behind the
    # switch only; the production mapping call stays the plain apply).
    assert "self.mapping.apply(" in service


def test_a_waived_evaluation_is_refused_on_the_ordinary_path(mapping):
    h = held(mapping, APPROX)
    with pytest.raises(WaivedDecisionRefused):
        ensure_production([h.production, h.waived])
    ensure_production([h.production])  # a production decision passes


@pytest.mark.parametrize("text,pairs", [
    (APPROX, [("ENTRY_ANCHOR_LEVEL_NOT_MARKET_QUOTE", "OWN_CLAUSE:APPROXIMATION_MARK")]),
    (MENTION, [("ENTRY_ANCHOR_POST_UNRESOLVED_MARKET", "RULE:MENTION_WITH_CONTEXT")]),
    (FOLLOWING, [("ENTRY_ANCHOR_LEVEL_NOT_MARKET_QUOTE", "FOLLOWING_NOT_UNDERSTOOD")]),
])
def test_a_confirmable_hold_is_stepped_over_at_its_site_and_the_level_is_unchanged(
        mapping, text, pairs):
    h = held(mapping, text)
    assert h.production.status == "UNRESOLVED" and h.production.entry_anchor is None
    assert h.waived.status == "SUPPORTED" and h.waived.pairs == pairs
    assert h.waived.entry_anchor == "50.10"  # the number as written: nothing widened or moved
    assert isinstance(h.waived, WaivedResolution)


@pytest.mark.parametrize("text,blocker", [
    (WORDS_THEN_OWN_CLAUSE, "ENTRY_ANCHOR_LEVEL_NOT_MARKET_QUOTE"),
    (MENTION_THEN_TWO_LEVELS, "ENTRY_ANCHOR_POST_SEVERAL_LEVELS"),
])
def test_the_later_checks_still_run_behind_a_stepped_hold(mapping, text, blocker):
    h = held(mapping, text)
    assert h.waived.pairs  # a confirmable hold was stepped over ...
    assert h.waived.status == "UNRESOLVED" and h.waived.reason.split(",")[0] == blocker
    out = C.offer_for(h)
    assert isinstance(out, C.NoOffer) and out.reason == "NOT_CONFIRMABLE"
    assert out.detail[0] == blocker and out.confirmable_doubts


def test_a_waived_evaluation_of_a_clean_post_equals_the_production_decision(mapping):
    _, parsed, _, decisions, kw = decide(mapping, CLEAN)
    [d] = [x for x in decisions if x.status == "SUPPORTED"]
    w = mapping.apply_waived(parsed, d.action_id, waive=T.WAIVE_ALL, **kw)
    assert w.status == "SUPPORTED" and w.pairs == [] and w.entry_anchor == d.entry_anchor


def test_a_never_hold_is_not_stepped_over(mapping):
    h = held(mapping, TWO_LEVELS)
    assert h.waived.status == "UNRESOLVED" and not h.waived.pairs
    assert C.offer_for(h).reason == "NOT_CONFIRMABLE"


# ================================================================== the pre-check and the hook

def test_a_confirmable_hold_becomes_one_offer_bound_by_hash_and_expiring_at_the_age_limit(mapping):
    h = held(mapping, APPROX)
    offer = C.offer_for(h)
    assert isinstance(offer, C.Offer) and offer.tier == T.CONFIRMABLE_TIER
    assert offer.price_reference == "50.10" and offer.price_reference_kind == "STATED_LEVEL"
    assert offer.stop == "48.90" and offer.proposal.ambiguities == []
    assert offer.expires_at == PUBLISHED + timedelta(seconds=90)  # the existing limit, unchanged
    assert offer.hash == C.offer_hash(offer) and len(offer.short_hash) == 8
    assert offer.gate_now_status == "APPROVE"
    assert C.Offer.from_record(json.loads(json.dumps(offer.to_record()))) == offer


def test_the_hash_binds_the_post_revision_and_the_typed_proposal(mapping):
    offer = C.offer_for(held(mapping, APPROX))
    for change in ({"revision": 2}, {"stop": "48.80"}, {"post_hash": "0" * 64},
                   {"entry_terms_hash": "x"}, {"lexicon_hash": "y"},
                   {"expires_at": offer.expires_at + timedelta(seconds=30)}):
        assert C.offer_hash(replace(offer, **change)) != offer.hash, change
    record = offer.to_record()
    record["stop"] = "47.00"
    with pytest.raises(ValueError):
        C.Offer.from_record(record)


@pytest.mark.parametrize("text", [QUESTION, UNATTRIBUTED])
def test_the_reporting_tier_is_counted_but_never_offered(mapping, text):
    out = C.offer_for(held(mapping, text))
    assert isinstance(out, C.NoOffer) and out.reason == "TIER_NOT_ALLOWED"
    assert out.tier == T.REPORT_ONLY_TIER and out.would_be is not None
    assert out.would_be.tier == T.REPORT_ONLY_TIER


def test_not_offered_without_one_fully_formed_current_open(mapping):
    assert C.offer_for(held(mapping, APPROX, revision=2)).reason == "SOURCE_NOT_ELIGIBLE"
    assert C.offer_for(held(mapping, APPROX, is_edit=True)).reason == "SOURCE_NOT_ELIGIBLE"
    assert C.offer_for(held(mapping, APPROX, text_complete=False)).reason == "SOURCE_NOT_ELIGIBLE"
    assert C.offer_for(held(mapping, APPROX, duplicate=True)).reason == "DUPLICATE"
    assert C.offer_for(held(mapping, APPROX, before_first_start=True)).reason \
        == "POST_BEFORE_FIRST_START"
    h = held(mapping, APPROX)
    second = h.actions[0].model_copy(update={"action_id": "a9"})
    assert C.offer_for(replace(h, actions=(*h.actions, second))).reason \
        == "NOT_SINGLE_TRADING_ACTION"
    assert C.offer_for(replace(h, entry=None)).reason == "MAPPING_NOT_APPROVED"
    no_stop = tuple(a.model_copy(update={"stop_price": None}) for a in h.actions)
    assert C.offer_for(replace(h, actions=no_stop)).reason == "STOP_MISSING"


def test_a_static_gateway_refusal_makes_the_hold_not_confirmable(mapping):
    out = C.offer_for(held(mapping, FAR_STOP))
    assert isinstance(out, C.NoOffer) and out.reason == "STATIC_BLOCKER"
    assert out.detail[0] == "PROTECTIVE_STOP_TOO_FAR_FROM_ENTRY"


def test_a_market_refusal_at_the_decision_is_shown_not_a_reason_to_withhold(mapping):
    h = held(mapping, APPROX)
    stale = market(mapping, h.now, age=10)
    offer = C.offer_for(replace(h, quote=stale))
    assert isinstance(offer, C.Offer) and offer.gate_now_status != "APPROVE"
    assert offer.gate_now_reasons[0] == "ENTRY_ANCHOR_QUOTE_SIDE_STALE"


def test_a_hold_that_is_not_about_interpretation_is_never_offered(mapping):
    h = held(mapping, CLEAN)
    assert h.production.status == "SUPPORTED"
    out = C.offer_for(replace(h, outcome_status="BLOCK", outcome_reasons=("QUOTE_UNAVAILABLE",)))
    assert out.reason == "HOLD_NOT_FROM_INTERPRETATION"


def test_no_offer_at_or_after_expiry_and_the_gate_never_passes_after_it(mapping):
    late = PUBLISHED + timedelta(seconds=90)
    assert C.offer_for(held(mapping, APPROX, now=late)).reason == C.CONFIRMATION_EXPIRED
    offer = C.offer_for(held(mapping, APPROX))
    fresh = market(mapping, late)
    gate = C.gate_confirmed(offer, fresh, POLICY, now=late)
    assert gate.status == "HOLD" and gate.reasons == [C.CONFIRMATION_EXPIRED]


def test_confirming_an_approximation_adds_no_tolerance(mapping):
    """The stated level is the anchor, as written; the collar and move limits apply unchanged
    against the fresh quote (0.3% adverse on the invented family)."""
    offer = C.offer_for(held(mapping, APPROX))
    now = PUBLISHED + timedelta(seconds=40)
    at_level = C.gate_confirmed(offer, market(mapping, now), POLICY, now=now)
    assert at_level.status == "APPROVE" and at_level.plan.entry_anchor == Decimal("50.10")
    # 0.31% above the stated level: refused exactly as an unconfirmed anchored entry would be.
    away = C.gate_confirmed(offer, market(mapping, now, bid="50.24", ask="50.26"), POLICY,
                            now=now)
    assert away.status == "HOLD" and away.reasons[0] == "ENTRY_ANCHOR_ADVERSE_MOVE"


def test_the_hook_carries_no_word_of_the_post(mapping):
    for text in (APPROX, MENTION, FOLLOWING, QUESTION, WORDS_THEN_OWN_CLAUSE):
        out = C.offer_for(held(mapping, text))
        offer = out if isinstance(out, C.Offer) else out.would_be
        blob = json.dumps(offer.to_record() if offer else repr(out), default=str).lower()
        for word in set(re.findall(r"[a-z]{4,}", text.lower())) - {"long", "stop", "qetf"}:
            assert word not in blob, word
