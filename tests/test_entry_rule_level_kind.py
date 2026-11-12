"""The owner's ambiguity safeguard for the entry rule (12 November 2026), on invented sentences.

1. Every candidate level is classified into one closed kind (``AnchorLevelKind``) and only an
   explicit contemporaneous market quotation (MARKET_QUOTE) anchors, in every anchor scope: the
   number's own clause must be a pure quotation (rule A), and an elliptical level must not take its
   topic from another level (rule B).
2. Under SINGLE_MARKET_POST, a market mention the bot cannot resolve rejects the post
   (``ENTRY_ANCHOR_POST_UNRESOLVED_MARKET``): an unknown market is never taken as "no second market".
"""

from __future__ import annotations

import pytest
from test_entry_anchor_policy import ANCHOR_RULE, MAP_NOW, action, etf_entry, policy, resolve
from test_entry_anchor_single_market import NOTE, POST, SMP, decide, note, two_families

from tradingbot.event_models import ParsedPost
from tradingbot.models import MockPolicy
from tradingbot.runtime.instruction_terms import (
    AnchorLevelKind,
    MarketMention,
    unresolved_market_mentions,
)
from tradingbot.runtime.mapping import AnchorLexicon
from tradingbot.validator import gate_action

SCOPES = ["SENTENCE_STRICT", "ACTION_BLOCK", "SINGLE_MARKET_POST"]
LONG = "We are now long $QETF."
NOT_QUOTE = "ENTRY_ANCHOR_LEVEL_NOT_MARKET_QUOTE"
UNRESOLVED = "ENTRY_ANCHOR_POST_UNRESOLVED_MARKET"


def scoped(tmp_path, scope, extra=""):
    return policy(tmp_path, etf_entry(rule=ANCHOR_RULE.replace("SENTENCE_STRICT", scope),
                                      extra=extra))


def run(p, clause, post=None, levels=(("REFERENCE", "50.10"), ("STOP", "48.90")), a=None):
    parsed = ParsedPost(actions=[a or action(), note()])
    _, [d, _] = p.apply(parsed, clauses={"a1": clause, "n1": ""}, now=MAP_NOW,
                        post_text=post or clause, levels={"a1": list(levels)})
    return d


# ================================================================== 1. what the level is


def test_the_kind_is_a_closed_public_enum():
    assert {k.value for k in AnchorLevelKind} == {
        "MARKET_QUOTE", "ENTRY_OR_COST_BASIS", "STOP_OR_TARGET", "HISTORICAL", "AMBIGUOUS"}


@pytest.mark.parametrize("scope", SCOPES)
def test_the_reviewers_case_a_position_level_is_a_possible_cost_basis(tmp_path, scope):
    """Every word of "Our position is 50.10 now." is on the allow-list, yet 50.10 may be the
    position's cost basis: it never anchors."""
    d = run(scoped(tmp_path, scope), LONG, f"{LONG}\nOur position is 50.10 now.")
    assert d.status == "UNRESOLVED" and d.entry_anchor is None, d.reason
    assert d.reason.split(",")[:3] == [NOT_QUOTE, "LEVEL_KIND:ENTRY_OR_COST_BASIS", "OWN_CLAUSE"]
    assert d.anchor_level_kind == "ENTRY_OR_COST_BASIS"


@pytest.mark.parametrize("scope", SCOPES)
@pytest.mark.parametrize("clause,post,kind", [
    # A: a holder, position or trade word in the number's own clause
    ("We are long $QETF here at 50.10.", None, "ENTRY_OR_COST_BASIS"),  # "long at N": an entry
    ("Long $QETF now 50.10.", None, "ENTRY_OR_COST_BASIS"),
    ("We are now long $QETF, my trade 50.10 now.", None, "ENTRY_OR_COST_BASIS"),
    # A: a pronoun subject: some other thing's level
    (LONG, f"{LONG}\nIt is 50.10 now.", "AMBIGUOUS"),
    (LONG, f"{LONG}\nThis is 50.10 now.", "AMBIGUOUS"),
    ("We are now long $QETF, it is 50.10 now.", None, "AMBIGUOUS"),
    # B: the level sentence right after a stop or target sentence
    # (fallback round: "Stop 48.90." then a level line anchors with standalone_level_sentence on,
    # and holds with it off; see test_entry_rule_standalone)
    ("We are now long $QETF, stop 48.90, 50.10 now.", None, "STOP_OR_TARGET"),
])
def test_a_level_that_may_be_something_else_holds_in_every_scope(tmp_path, scope, clause, post,
                                                                   kind):
    d = run(scoped(tmp_path, scope), clause, post)
    assert d.status == "UNRESOLVED" and d.entry_anchor is None, d.reason
    assert d.reason.split(",")[:2] == [NOT_QUOTE, f"LEVEL_KIND:{kind}"], d.reason
    assert d.anchor_level_kind == kind


def test_a_level_after_a_target_sentence_holds(tmp_path):
    for scope in SCOPES:
        d = run(scoped(tmp_path, scope), LONG, f"{LONG} Target 52.40.\n50.10 now.",
                levels=[("REFERENCE", "50.10"), ("STOP", "48.90"), ("TARGET", "52.40")])
        assert d.reason.split(",")[0] == NOT_QUOTE, (scope, d.reason)


def test_a_level_after_a_raised_stop_holds_even_where_only_the_post_attributes_it(tmp_path):
    """Review B1: the neighbour "We raised our stop." is not on the allow-list, so it decides
    first (AMBIGUOUS); the deny-list would hold it too."""
    post = f"{LONG}\nWe raised our stop.\n50.10 now."
    d = run(scoped(tmp_path, "SINGLE_MARKET_POST"), LONG, post)
    assert d.reason.split(",")[:3] == [NOT_QUOTE, "LEVEL_KIND:AMBIGUOUS",
                                       "NEIGHBOUR_NOT_UNDERSTOOD"], d.reason
    for scope in ("SENTENCE_STRICT", "ACTION_BLOCK"):
        assert run(scoped(tmp_path, scope), LONG, post).status == "UNRESOLVED"


@pytest.mark.parametrize("context,detail", [
    ("Our average is low.", "CONTEXT_WORD:average"),
    ("We are at breakeven.", "CONTEXT_WORD:breakeven"),
    ("The cost basis is fine.", "CONTEXT_WORD:basis"),
    ("We filled the order.", "CONTEXT_WORD:filled"),
    ("The fund rose 1.20 this week.", "CONTEXT_NUMBER"),  # another number: another level
])
def test_an_elliptical_level_after_cost_basis_language_or_another_number_holds(tmp_path, context,
                                                                               detail):
    d = run(scoped(tmp_path, "SINGLE_MARKET_POST"), LONG, f"{LONG}\n{context}\n50.10 now.")
    assert d.status == "UNRESOLVED", d.reason
    # Review B1: each of these neighbours is off the allow-list, which now decides first.
    assert d.reason.split(",")[0] == NOT_QUOTE, d.reason
    assert d.reason.split(",")[2] in (detail, "NEIGHBOUR_NOT_UNDERSTOOD"), d.reason


@pytest.mark.parametrize("scope", SCOPES)
@pytest.mark.parametrize("clause,post", [
    ("We are now long $QETF, average 50.10 now.", None),  # cost basis
    ("We are now long $QETF, breakeven 50.10 now.", None),
    (LONG, f"{LONG}\nAvg cost 50.10 now."),
])
def test_cost_basis_wording_holds_and_is_classified(tmp_path, scope, clause, post):
    d = run(scoped(tmp_path, scope), clause, post)
    assert d.status == "UNRESOLVED" and d.entry_anchor is None, d.reason
    assert d.anchor_level_kind == "ENTRY_OR_COST_BASIS", d.reason


@pytest.mark.parametrize("scope", SCOPES)
def test_the_existing_checks_feed_the_classification(tmp_path, scope):
    p = scoped(tmp_path, scope)
    question = run(p, "We are now long $QETF, 50.10 now?")
    assert question.reason.split(",")[:2] == ["ENTRY_ANCHOR_CONTEXT_UNCLEAR", "QUESTION"]
    assert question.anchor_level_kind == "AMBIGUOUS"
    past = run(p, LONG, f"{LONG}\n50.10 now, last week.")
    assert past.status == "UNRESOLVED" and past.anchor_level_kind == "HISTORICAL", past.reason
    stop = run(p, LONG, f"{LONG}\nStop 50.10 now.")
    assert stop.status == "UNRESOLVED" and stop.anchor_level_kind == "STOP_OR_TARGET"


PASS = [
    ("We are now long $QETF, 50.10 now.", None),
    ("I am buying $QETF, currently 50.10.", None),
    ("New position: long $QETF, trading at 50.10.", None),
    ("We are now long $QETF, 50.10 now, stop 48.90.", None),
    (LONG, f"{LONG}\n$QETF trading at 50.10."),
    (LONG, f"{LONG}\n50.10 now."),
    (LONG, f"{LONG}\n50.10 now.\nStop 48.90."),
]


@pytest.mark.parametrize("scope", SCOPES)
@pytest.mark.parametrize("clause,post", PASS)
def test_an_explicit_market_quotation_still_anchors(tmp_path, scope, clause, post):
    d = run(scoped(tmp_path, scope), clause, post)
    assert d.status == "SUPPORTED" and d.entry_anchor == "50.10", d.reason
    assert d.anchor_level_kind == "MARKET_QUOTE"


@pytest.mark.parametrize("scope", ["SENTENCE_STRICT", "ACTION_BLOCK"])
def test_a_level_naming_the_family_is_explicit_whatever_its_neighbour(tmp_path, scope):
    d = run(scoped(tmp_path, scope), LONG, f"{LONG} Stop is 48.90.\n$QETF 50.10 now.")
    assert d.status == "SUPPORTED" and d.anchor_level_kind == "MARKET_QUOTE", d.reason


def test_a_held_level_reaches_the_gateway_as_a_hold(tmp_path):
    p = scoped(tmp_path, "SENTENCE_STRICT")
    marked, _ = p.apply(ParsedPost(actions=[action(), note()]), clauses={"a1": LONG},
                        now=MAP_NOW, post_text=f"{LONG}\nOur position is 50.10 now.",
                        levels={"a1": [("REFERENCE", "50.10")]})
    g = gate_action(marked.actions[0], MAP_NOW, None, MockPolicy(), None, now=MAP_NOW)
    assert g.status == "HOLD" and NOT_QUOTE in g.reasons


def test_without_a_stated_level_nothing_is_classified(tmp_path):
    d, _ = resolve(scoped(tmp_path, "SENTENCE_STRICT"), action(), LONG, [("STOP", "48.90")])
    assert d.anchor_level_kind is None


# ================================================================== 2. unresolved market mentions


@pytest.mark.parametrize("extra,kind", [
    ("ZQX looks weak.", "TICKER_SHAPED"),  # an unknown ticker, even sentence-initial
    ("We also watch ZQXZ6 here.", "TICKER_SHAPED"),  # with a futures month suffix
    ("We also like Qorvaline here.", "PROPER_NOUN"),  # an unknown company name mid-sentence
    ("Copper is heavy.", "MARKET_NOUN"),  # a generic commodity noun
    ("The #gold chart is heavy.", "MARKET_NOUN"),
    ("Equities are heavy.", "MARKET_NOUN"),
    ("Zorb futures look heavy.", "INSTRUMENT_PHRASE"),  # "X futures" with an unknown X
    ("The bond ETF is heavy.", "MARKET_NOUN"),
    ("Stocks are heavy.", "INSTRUMENT_PHRASE"),
])
def test_an_unresolved_market_mention_holds_the_single_market_post(tmp_path, extra, kind):
    d = decide(two_families(tmp_path), post=POST.replace(NOTE, f"{NOTE} {extra}"))
    assert d.status == "UNRESOLVED" and d.entry_anchor is None, d.reason
    assert d.reason.split(",")[0] == UNRESOLVED and f"MENTION:{kind}" in d.reason, d.reason


@pytest.mark.parametrize("extra", [
    "We are NOW long, STOP below the lows.",  # emphasised words the anchor rule understands
    "The Fed meets in December.",  # a non-market proper noun
    "See you on Monday, I'm out.",
    "$QETF shares look cheap.",  # an instrument noun headed by this family
    "The bond fund is firm.",  # this family's alias
    "RSI is firm, OK.",  # non-instrument acronyms
])
def test_generic_vocabulary_does_not_false_trigger(tmp_path, extra):
    d = decide(two_families(tmp_path), post=POST.replace(NOTE, f"{NOTE} {extra}"))
    assert d.status == "SUPPORTED", (extra, d.reason)


def test_an_emphasised_understood_word_in_the_instruction_still_anchors(tmp_path):
    post = POST.replace("We are now long", "We are NOW LONG")
    d = decide(two_families(tmp_path), post=post,
               clauses={"a1": "We are NOW LONG $QETF from the lows.", "n1": NOTE})
    assert d.reason.split(",")[0] != UNRESOLVED, d.reason


def test_a_private_alias_or_non_market_word_resolves_a_mention(tmp_path):
    p = two_families(tmp_path)
    bonds = POST.replace(NOTE, f"{NOTE} Treasuries are firm.")
    assert decide(p, post=bonds).reason.split(",")[0] == UNRESOLVED
    person = POST.replace(NOTE, f"{NOTE} We spoke with Qorvaline.")
    assert decide(p, post=person).reason.split(",")[0] == UNRESOLVED
    p.anchor_lexicon = AnchorLexicon(family_aliases={"QETF": ["treasuries"]},
                                     non_market_words=["qorvaline"])
    assert decide(p, post=bonds).status == "SUPPORTED"
    assert decide(p, post=person).status == "SUPPORTED"


@pytest.mark.parametrize("word,why", [("gold", "market"), ("futures", "market"),
                                      ("above", "disqualifies"), ("two words", "one to 1"),
                                      ("q0rv", "letters")])
def test_the_private_non_market_list_is_checked_on_load(word, why):
    with pytest.raises(ValueError, match=why):
        AnchorLexicon(non_market_words=[word])


def test_the_residual_a_lowercase_unknown_market_word_stays_invisible(tmp_path):
    """Documented residual (restated after review B2): no named-entity recognition. A market named
    by a lower-case common word outside the lists, or an unknown sentence-initial word, in a
    sentence that is NOT the level's neighbour, in an otherwise understood post, is not seen; the
    collar, signal age, stop and size caps bound that case. Before an elliptical level it holds
    (test_entry_rule_review_fixes)."""
    for extra in ("The widget fund is heavy.", "Qorvaline is heavy."):
        d = decide(two_families(tmp_path), post=POST.replace(NOTE, f"{NOTE} {extra}"))
        assert d.status == "SUPPORTED", d.reason


def test_the_other_scopes_do_not_run_the_post_check(tmp_path):
    for scope in ("SENTENCE_STRICT", "ACTION_BLOCK"):
        p = scoped(tmp_path, scope)
        d = run(p, "We are now long $QETF, 50.10 now.",
                "We are now long $QETF, 50.10 now.\nCopper is heavy.")
        assert d.reason.split(",")[0] != UNRESOLVED, (scope, d.reason)


def test_the_detector_returns_kinds_only():
    out = unresolved_market_mentions("Watching ZQX and Copper, Qorvaline too.", {"qetf"},
                                     {"QETF"}, {"QETF"})
    assert out == [MarketMention.MARKET_NOUN, MarketMention.PROPER_NOUN,
                   MarketMention.TICKER_SHAPED]
    assert all(isinstance(k, MarketMention) for k in out)
    own_future = unresolved_market_mentions("Long the Aug contract, QOFQ6 too.", {"qoil"},
                                            {"QOF", "QOIL"}, {"QOF"}, futures=True)
    assert own_future == []
    assert SMP  # the single-market scope these probes use
