"""anchor_scope = "SINGLE_MARKET_POST": the level's own sentence is checked as in SENTENCE_STRICT;
attribution is by the whole post (one marked level, one market, one current entry). Invented data.
"""

from __future__ import annotations

import pytest
from test_entry_anchor_policy import (
    ANCHOR_RULE,
    MAP_NOW,
    action,
    etf_entry,
    oil_entry,
    policy,
    service_mapping,
)
from test_entry_anchor_sentence import REFUSE

from tradingbot.event_models import ParsedPost, Timing, TradeAction
from tradingbot.models import Action
from tradingbot.runtime.mapping import AnchorLexicon, MappingPolicy

SMP = ANCHOR_RULE.replace('"SENTENCE_STRICT"', '"SINGLE_MARKET_POST"')
OWN = "We are now long $QETF."  # fallback round: the instruction must be understood
NOTE = "The fund has been basing for weeks."
STOP = "Stop 48.90."
# Final review round: the sentence before an elliptical level must be the action's own
# instruction and the sentence after it understood (here: the end of the post).
POST = f"{NOTE}\n{STOP}\nThe chart looks constructive.\n{OWN}\n50.10 now."


def two_families(tmp_path) -> MappingPolicy:
    return policy(tmp_path, etf_entry(rule=SMP, extra='aliases = ["bond fund"]'),
                  oil_entry(rule=""))


def note(action_id="n1", asset="QETF", timing=Timing.COMMENTARY, kind=Action.NO_TRADE,
         ambiguities=()):
    return TradeAction(action_id=action_id, action=kind, timing=timing,
                       source_market_symbol=asset, evidence=[], ambiguities=list(ambiguities))


def decide(p, post=POST, extra_actions=(), clauses=None):
    parsed = ParsedPost(actions=[action(), note(), *extra_actions])
    clauses = clauses or {"a1": OWN, "n1": NOTE}
    _, decisions = p.apply(parsed, clauses=clauses, now=MAP_NOW, post_text=post,
                           levels={"a1": [("REFERENCE", "50.10"), ("STOP", "48.90")]})
    return decisions[0]


def test_the_scope_is_a_legal_value(tmp_path):
    assert two_families(tmp_path).entries["QETF"].entry_anchor.anchor_scope == \
        "SINGLE_MARKET_POST"


def test_a_closing_level_sentence_in_a_one_market_post_anchors(tmp_path):
    d = decide(two_families(tmp_path))
    assert d.status == "SUPPORTED" and d.entry_anchor == "50.10", d.reason
    assert d.anchor_attribution == "SINGLE_MARKET_POST"


def test_the_neighbour_rule_is_the_same_in_every_scope(tmp_path):
    """Final review round: the neighbour rule is applied in every scope; SENTENCE_STRICT and
    ACTION_BLOCK keep their stricter whole-sentence checks on top (OWN holds "from the lows"), so
    they are never looser than SINGLE_MARKET_POST."""
    assert decide(two_families(tmp_path)).status == "SUPPORTED"
    foreign = POST.replace(f"\n{OWN}", f"\n{OWN} The chart looks fine.")
    assert decide(two_families(tmp_path), post=foreign).status == "UNRESOLVED"
    for scope in ("SENTENCE_STRICT", "ACTION_BLOCK"):
        p = policy(tmp_path, etf_entry(rule=ANCHOR_RULE.replace("SENTENCE_STRICT", scope)))
        assert decide(p).status == "SUPPORTED", scope  # the same clean post
        assert decide(p, post=foreign).status == "UNRESOLVED", scope
    chart = POST.replace(f"\n{OWN}", "").replace("The chart", f"{OWN} The chart")
    assert decide(two_families(tmp_path), post=chart).status == "UNRESOLVED"


@pytest.mark.parametrize("extra", [
    "Compare $QOIL.",  # another family's cashtag
    "QOIL is quiet.",  # another family's code
    "Also watching QOF.",  # another family's execution symbol
    "$ZZZ is weak.",  # a cashtag the mapping does not know
])
def test_a_second_market_anywhere_in_the_post_refuses(tmp_path, extra):
    d = decide(two_families(tmp_path), post=POST.replace(NOTE, f"{NOTE} {extra}"))
    assert d.status == "UNRESOLVED", d.reason
    assert d.reason.split(",")[0] == "ENTRY_ANCHOR_POST_SEVERAL_MARKETS"


def test_a_name_from_the_private_vocabulary_refuses(tmp_path):
    p = two_families(tmp_path)
    post = POST.replace(NOTE, f"{NOTE} Crude is heavy.")
    assert decide(p, post=post).reason.split(",")[0] != "ENTRY_ANCHOR_POST_SEVERAL_MARKETS"
    p.anchor_lexicon = AnchorLexicon(family_aliases={"QOIL": ["crude"]})
    assert decide(p, post=post).reason.split(",")[0] == "ENTRY_ANCHOR_POST_SEVERAL_MARKETS"


def test_an_action_for_another_family_refuses(tmp_path):
    d = decide(two_families(tmp_path), extra_actions=[note("n2", asset="QOIL")])
    assert d.reason.split(",")[0] == "ENTRY_ANCHOR_POST_SEVERAL_MARKETS"


@pytest.mark.parametrize("flag", ["ASSET_UNRESOLVED", "MIXED_SERIES_AND_TICKER",
                                  "MULTIPLE_TICKERS_IN_CLAUSE"])
def test_an_unresolved_instrument_refuses(tmp_path, flag):
    d = decide(two_families(tmp_path), extra_actions=[note("n2", asset=None, ambiguities=[flag])])
    assert d.reason.split(",")[0] == "ENTRY_ANCHOR_POST_SEVERAL_MARKETS"


def test_a_second_marked_level_refuses(tmp_path):
    d = decide(two_families(tmp_path), post=POST.replace("The chart", "49.90 now. The chart"))
    assert d.reason.split(",")[0] == "ENTRY_ANCHOR_POST_SEVERAL_LEVELS"


@pytest.mark.parametrize("kind,timing", [
    (Action.CLOSE, Timing.CURRENT),  # a second current action
    (Action.OPEN, Timing.CONDITIONAL),
    (Action.CLOSE, Timing.HISTORICAL),
    (Action.ADD, Timing.CURRENT),
])
def test_another_current_conditional_or_retrospective_action_refuses(tmp_path, kind, timing):
    other = note("n2", kind=kind, timing=timing).model_copy(update={"idea_reference": "9:a1"})
    d = decide(two_families(tmp_path), extra_actions=[other])
    assert d.reason.split(",")[0] == "ENTRY_ANCHOR_POST_SEVERAL_ACTIONS", d.reason


def test_without_the_posts_actions_the_scope_holds(tmp_path):
    d, _ = two_families(tmp_path).resolve(action(), clause=OWN, now=MAP_NOW, post_text=POST,
                                          levels=[("REFERENCE", "50.10")])
    assert d.status == "UNRESOLVED"
    assert d.reason.split(",")[0] == "ENTRY_ANCHOR_POST_SEVERAL_ACTIONS"


@pytest.mark.parametrize("clause", REFUSE)
def test_the_bypass_classes_inside_the_levels_own_sentence_still_refuse(tmp_path, clause):
    p = two_families(tmp_path)
    d = decide(p, post=clause, clauses={"a1": clause, "n1": ""})
    assert d.status == "UNRESOLVED" and d.entry_anchor is None, d.reason


@pytest.mark.parametrize("line", ["50.10 now is the goal.", "Avg 50.10 now.", "50.10 now?",
                                  "Dubai 50.10 now."])
def test_an_unclear_level_sentence_still_refuses(tmp_path, line):
    d = decide(two_families(tmp_path), post=POST.replace("50.10 now.", line))
    assert d.status == "UNRESOLVED", d.reason


def test_a_reference_series_level_names_nothing_or_the_reference(tmp_path):
    f = tmp_path / "m.toml"
    f.write_text(service_mapping(scope="SINGLE_MARKET_POST"))
    p = MappingPolicy.from_toml(f)
    from test_entry_anchor_context import idx_action
    own = "We are now long $QIDX."
    # Fallback round: ANCHOR_RULE carries standalone_level_sentence = true, so a bare level line
    # after the own stop sentence is accepted; the shipped default is in test_entry_rule_standalone.
    for line, reason, stop_first in (
            ("5,010 now.", None, False), ("QIDX 5,010 now.", None, False),
            ("QIDX 5,010 now.", None, True),  # names the reference: explicit, any neighbour
            ("5,010 now.", None, True),  # fallback round: after the own stop, switch on
            ("QIF 5,010 now.", "ENTRY_ANCHOR_REFERENCE_UNNAMED", False)):
        post = f"{own}\nStop 4,980.\n{line}" if stop_first else f"{own}\n{line}\nStop 4,980."
        parsed = ParsedPost(actions=[idx_action()])
        _, [d] = p.apply(parsed, clauses={"a1": own}, now=MAP_NOW, post_text=post,
                         levels={"a1": [("REFERENCE", "5010"), ("STOP", "4980")]})
        if reason is None:
            assert d.status == "SUPPORTED", (line, d.reason)
        else:
            assert d.reason.split(",")[0] == reason, (line, d.reason)


@pytest.mark.parametrize("extra", ["qoil is quiet.", "Watching $qof.", "(Qof) is quiet.",
                                   "Also qof, quiet.", "The BOND FUND is quiet."])
def test_check_b_is_case_and_punctuation_insensitive(tmp_path, extra):
    p = policy(tmp_path, etf_entry(rule=SMP), oil_entry(rule="").replace(
        'family = "QOIL"', 'family = "QOIL"\naliases = ["bond fund"]'))
    d = decide(p, post=POST.replace(NOTE, f"{NOTE} {extra}"))
    assert d.reason.split(",")[0] == "ENTRY_ANCHOR_POST_SEVERAL_MARKETS", d.reason


@pytest.mark.parametrize("extra", ["Qoils are quiet.", "Qofstad wrote.", "A qoffee break."])
def test_check_b_matches_whole_tokens_only(tmp_path, extra):
    d = decide(two_families(tmp_path), post=POST.replace(NOTE, f"{NOTE} {extra}"))
    assert d.reason.split(",")[0] != "ENTRY_ANCHOR_POST_SEVERAL_MARKETS", d.reason
