"""The final anchor rule (last review pass): no window. The scope of a marked number is its whole
sentence; every word in it must be on the allow-list, a question or a second unexplained number
refuses it, and a standalone level sentence also needs a clean sentence before it. Invented data.
"""

from __future__ import annotations

import pytest
from test_entry_anchor_policy import (
    LONG,
    action,
    etf_entry,
    policy,
    resolve,
    run_posts,
)
from test_entry_anchor_policy import outcomes as _outcomes

from tradingbot.models import PositionSide
from tradingbot.runtime.mapping import AnchorLexicon

REFUSED = {"ENTRY_ANCHOR_CONTEXT_UNCLEAR", "ENTRY_ANCHOR_INSTRUMENT_UNCLEAR",
           "ENTRY_ANCHOR_NOT_CURRENT", "ENTRY_ANCHOR_INSTRUMENT_AMBIGUOUS"}
LEVELS = [("REFERENCE", "50.10"), ("STOP", "48.90")]

# ------------------------------------------------------------------ the reviewer's three classes

REVIEWER_POSTS = [
    # the window start reset at a family name or a side word
    "We are now Dubai long $QOIL 71.50 now. Stop 69.80.",
    "Cost basis long $QOIL 71.50 now. Stop 69.80.",
    # the window end stopped at a comma, colon or dash
    "We are now long $QOIL avg cost, 71.50 now. Stop 69.80.",
    "We are now long $QOIL, goal: 71.50 now. Stop 69.80.",
    "We are now long $QOIL, Dubai looks heavy - 71.50 now. Stop 69.80.",
    # a question
    "We are now long $QOIL, 71.50 now? Stop 69.80.",
]


@pytest.mark.parametrize("post", REVIEWER_POSTS)
def test_reviewer_probe_the_window_cannot_be_gamed(tmp_path, post):
    run = run_posts(tmp_path, "sent", [post])
    outs = _outcomes(run)
    assert all(status != "EXECUTED" for _, status, _ in outs), outs
    assert run.broker.submit_calls == 0
    run.close()


REFUSE = [
    "We are now Dubai long $QETF 50.10 now.",
    "Cost basis long $QETF 50.10 now.",
    "Avg cost long $QETF, 50.10 now.",
    "We are now long $QETF avg cost, 50.10 now.",
    "We are now long $QETF, goal: 50.10 now.",
    "We are now long $QETF, Dubai looks heavy - 50.10 now.",
    "We are now long $QETF (cost basis) 50.10 now.",
    "We are now long $QETF; the other fund is weak, 50.10 now.",
    "We are now long $QETF, 50.10 now?",
    "Long $QETF here at 50.10 now, right?",
    "We are now long $QETF, 50.10 now, was 49.20.",
    "We are now long $QETF 50.10 now, 3 lots of 49.",
    "We are now long $QETF/QBOND 50.10 now.",
    "Long $QETF, 50.10 now - fair value.",
]


@pytest.mark.parametrize("clause", REFUSE)
def test_an_unknown_word_anywhere_in_the_sentence_refuses_the_anchor(tmp_path, clause):
    d, inst = resolve(policy(tmp_path, etf_entry()), action(), clause, LEVELS)
    assert d.status == "UNRESOLVED" and inst is None and d.entry_anchor is None, d.reason
    assert d.reason.split(",")[0] in REFUSED, d.reason


def test_a_question_has_its_own_reason(tmp_path):
    d, _ = resolve(policy(tmp_path, etf_entry()), action(), "We are now long $QETF, 50.10 now?",
                   LEVELS)
    assert d.reason.split(",")[:2] == ["ENTRY_ANCHOR_CONTEXT_UNCLEAR", "QUESTION"]


PASS = [
    ("We are now long $QETF, 50.10 now.", PositionSide.LONG),
    ("We are long $QETF here at 50.10.", PositionSide.LONG),
    ("I am buying $QETF, currently 50.10.", PositionSide.LONG),
    ("New position: long $QETF, trading at 50.10.", PositionSide.LONG),
    ("We are now long $QETF, 50.10 now, stop 48.90.", PositionSide.LONG),
    ("Buy $QETF at the market, around 50.10 now.", PositionSide.LONG),
    ("Short $QETF, 50.10 right now, stop 51.20.", PositionSide.SHORT),
    ("We are selling $QETF short here at 50.10.", PositionSide.SHORT),
    ("Long $QETF, our new trade, ~50.10 now.", PositionSide.LONG),
    ("This is a new trade: long $QETF, it is 50.10 now.", PositionSide.LONG),
]


@pytest.mark.parametrize("clause,side", PASS)
def test_ordinary_plain_instructions_still_anchor(tmp_path, clause, side):
    stop = "48.90" if side == PositionSide.LONG else "51.20"
    a = action().model_copy(update={"position_side": side, "stop_price": stop})
    d, _ = resolve(policy(tmp_path, etf_entry()), a, clause, [("REFERENCE", "50.10"),
                                                              ("STOP", stop)])
    assert d.status == "SUPPORTED", d.reason
    assert d.entry_anchor == "50.10"


# ------------------------------------------------------------------ the level sentence


@pytest.mark.parametrize("post,ok", [
    (f"{LONG}\n50.10 now.", True),
    (f"{LONG} Stop 48.90.\n~$50.10 now.", True),
    (f"{LONG}\n$QETF trading at 50.10.", True),
    (f"{LONG}\nDubai looks heavy.\n50.10 now.", False),
    (f"{LONG}\nThe other fund is weak.\n50.10 now.", False),
    (f"{LONG}\n$QBOND is weak.\n50.10 now.", False),
])
def test_a_level_sentence_needs_a_clean_sentence_before_it(tmp_path, post, ok):
    d, _ = resolve(policy(tmp_path, etf_entry()), action(), LONG, [("REFERENCE", "50.10")],
                   post_text=post)
    assert (d.status == "SUPPORTED") is ok, d.reason
    if not ok:
        assert d.reason.split(",")[0] in REFUSED


def test_the_private_lexicon_may_add_instruction_phrasing(tmp_path):
    clause = "Adding exposure: long $QETF, 50.10 now."
    p = policy(tmp_path, etf_entry())
    d, _ = resolve(p, action(), clause, LEVELS)
    assert d.reason.startswith("ENTRY_ANCHOR_CONTEXT_UNCLEAR")
    p.anchor_lexicon = AnchorLexicon(context_words=["adding exposure"])
    d, _ = resolve(p, action(), clause, LEVELS)
    assert d.status == "SUPPORTED", d.reason


@pytest.mark.parametrize("word", ["the high", "above", "yesterday"])
def test_a_private_context_word_may_not_be_a_qualifier(word):
    with pytest.raises(ValueError):
        AnchorLexicon(context_words=[word])


@pytest.mark.parametrize("scope", ["SENTENCE_STRICT", "ACTION_BLOCK", "SINGLE_MARKET_POST"])
@pytest.mark.parametrize("clause", [
    "Long $QETF for 50.10 now.",  # a target-style phrase
    "Long $QETF to 50.10 now.",
    "Long $QETF on 50.10 now.",
    "Long $QETF with 50.10 now.",
    "Long $QETF in 50.10 now.",
    "We are long $QETF, half of 50.10 now.",
])
def test_a_preposition_that_can_make_the_number_something_else_refuses(tmp_path, scope, clause):
    from test_entry_anchor_policy import ANCHOR_RULE, MAP_NOW

    from tradingbot.event_models import ParsedPost
    p = policy(tmp_path, etf_entry(rule=ANCHOR_RULE.replace("SENTENCE_STRICT", scope)))
    _, [d] = p.apply(ParsedPost(actions=[action()]), clauses={"a1": clause}, now=MAP_NOW,
                     post_text=clause, levels={"a1": LEVELS})
    assert d.status == "UNRESOLVED" and d.entry_anchor is None, d.reason


def test_the_generic_function_words_are_the_closed_list():
    from tradingbot.runtime.instruction_terms import FUNCTION_WORDS
    assert FUNCTION_WORDS == frozenset(("we", "i", "are", "am", "is", "and", "our", "my", "a",
                                        "the", "this", "new", "position", "positions", "trade"))
