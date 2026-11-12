"""entry_anchor.anchor_scope: required with the entry rule, SENTENCE_STRICT or ACTION_BLOCK, no
default. Both allow-list the marked number's whole sentence; they differ only in how a standalone
level sentence is attributed to the action. Invented data only.
"""

from __future__ import annotations

import tomllib

import pytest
from test_entry_anchor_policy import ANCHOR_RULE, action, etf_entry, policy, resolve
from test_entry_anchor_sentence import REFUSE

from tradingbot.runtime.mapping import FamilyMapping

STRICT = ANCHOR_RULE
BLOCK = ANCHOR_RULE.replace('"SENTENCE_STRICT"', '"ACTION_BLOCK"')
OWN = "We are now long $QETF from the lows."
LEVELS = [("REFERENCE", "50.10"), ("STOP", "48.90")]


def load(text):
    return FamilyMapping.model_validate(tomllib.loads(text)["mapping"][0])


@pytest.mark.parametrize("rule,message", [
    (ANCHOR_RULE.replace(', anchor_scope = "SENTENCE_STRICT"', ""), "Field required"),
    (ANCHOR_RULE.replace('"SENTENCE_STRICT"', '"WINDOW"'), "Input should be"),
    (ANCHOR_RULE.replace('"SENTENCE_STRICT"', '"sentence_strict"'), "Input should be"),
])
def test_the_scope_is_required_and_closed(rule, message):
    with pytest.raises(ValueError, match=message):
        load(etf_entry(rule=rule))


def test_both_scopes_load_and_reach_the_entry():
    assert load(etf_entry(rule=STRICT)).entry_anchor.anchor_scope == "SENTENCE_STRICT"
    assert load(etf_entry(rule=BLOCK)).entry_anchor.anchor_scope == "ACTION_BLOCK"


def decide(tmp_path, rule, post, clause=OWN, others=()):
    return resolve(policy(tmp_path, etf_entry(rule=rule)), action(), clause,
                   [("REFERENCE", "50.10")], post_text=post, others=list(others))[0]


# ------------------------------------------------------------------ ACTION_BLOCK attribution


def test_a_level_sentence_right_after_the_actions_own_instruction_anchors(tmp_path):
    clean = "We are now long $QETF."
    d = decide(tmp_path, BLOCK, f"{clean}\n50.10 now.", clause=clean)
    assert d.status == "SUPPORTED" and d.entry_anchor == "50.10", d.reason
    assert d.anchor_attribution == "AFTER_OWN_INSTRUCTION"
    # Every sentence of the action's own clause is allow-listed too (final review): "from the
    # lows" is not on the list, so neither scope attributes the level.
    d = decide(tmp_path, BLOCK, f"{OWN}\n50.10 now.")
    assert d.status == "UNRESOLVED" and d.reason.split(",")[:2] == [
        "ENTRY_ANCHOR_CONTEXT_UNCLEAR", "OWN_CLAUSE"]
    d = decide(tmp_path, STRICT, f"{OWN}\n50.10 now.")
    assert d.status == "UNRESOLVED" and d.reason.startswith("ENTRY_ANCHOR_INSTRUMENT_UNCLEAR")


@pytest.mark.parametrize("rule", [STRICT, BLOCK])
def test_a_level_sentence_after_an_allow_listed_sentence_now_holds(tmp_path, rule):
    """Final review round: an allow-listed sentence that is not the action's own instruction is
    no longer a neighbour that attributes an elliptical level (NEIGHBOUR_NOT_UNDERSTOOD)."""
    d = decide(tmp_path, rule, f"{OWN} We are long.\n50.10 now.")
    assert d.status == "UNRESOLVED", d.reason


@pytest.mark.parametrize("rule", [STRICT, BLOCK])
def test_a_level_sentence_after_a_stop_sentence_now_holds(tmp_path, rule):
    """Owner's safeguard (12 Nov 2026): "Stop 48.90." then "50.10 now." may be the stop."""
    d = decide(tmp_path, rule, f"{OWN} Stop 48.90.\n50.10 now.")
    assert d.status == "UNRESOLVED", d.reason
    assert d.reason.split(",")[0] == "ENTRY_ANCHOR_LEVEL_NOT_MARKET_QUOTE", d.reason


def test_a_stop_sentence_with_other_words_holds_because_its_block_is_not_exposed(tmp_path):
    """The interpreter does not expose which sentence its stop came from (PriceMention has no
    sentence index), so a stop sentence is attributed only if it is entirely allow-listed."""
    d = decide(tmp_path, BLOCK, f"{OWN} Stop below 48.90.\n50.10 now.")
    assert d.status == "UNRESOLVED" and d.reason.startswith("ENTRY_ANCHOR_INSTRUMENT_UNCLEAR")


@pytest.mark.parametrize("post,others", [
    (f"{OWN}\nDubai looks heavy.\n50.10 now.", ()),  # another market
    (f"{OWN}\nThe tape is choppy today.\n50.10 now.", ()),  # commentary
    (f"{OWN}\nWe are short $QBOND.\n50.10 now.", (("QBOND", "We are short $QBOND.", set()),)),
    (f"{OWN}\nWe are long $QETF.\nStill here.\n50.10 now.", ()),  # a sentence in between
])
def test_a_level_sentence_after_anything_but_the_actions_block_holds(tmp_path, post, others):
    d = decide(tmp_path, BLOCK, post, others=others)
    assert d.status == "UNRESOLVED" and d.entry_anchor is None, d.reason


def test_the_own_sentence_case_is_recorded(tmp_path):
    d = decide(tmp_path, BLOCK, "We are now long $QETF, 50.10 now.",
               clause="We are now long $QETF, 50.10 now.")
    assert d.status == "SUPPORTED" and d.anchor_attribution == "OWN_SENTENCE"


@pytest.mark.parametrize("rule", [STRICT, BLOCK])
@pytest.mark.parametrize("clause", REFUSE)
def test_the_bypass_classes_hold_under_both_scopes(tmp_path, rule, clause):
    d, _ = resolve(policy(tmp_path, etf_entry(rule=rule)), action(), clause, LEVELS)
    assert d.status == "UNRESOLVED" and d.entry_anchor is None, d.reason


@pytest.mark.parametrize("rule", [STRICT, BLOCK])
def test_a_level_sentence_that_is_itself_unclear_holds_under_both(tmp_path, rule):
    for line in ("50.10 now is the goal.", "Avg 50.10 now.", "50.10 now?"):
        d = decide(tmp_path, rule, f"{OWN}\n{line}")
        assert d.status == "UNRESOLVED", (line, d.reason)


@pytest.mark.parametrize("remark", ["Dubai looks heavy.", "The other fund is weak.",
                                    "Gold for 52 next."])
def test_action_block_allow_lists_every_sentence_of_the_actions_own_clause(tmp_path, remark):
    clause = f"We are now long $QETF. {remark}"
    d = decide(tmp_path, BLOCK, f"{clause}\n50.10 now.", clause=clause)
    assert d.status == "UNRESOLVED" and d.entry_anchor is None, d.reason


def test_action_block_accepts_a_clean_clause_and_its_own_stop_after_the_level(tmp_path):
    clause = "We are now long $QETF."
    d = decide(tmp_path, BLOCK, f"{clause}\n50.10 now.\nStop 48.90.", clause=clause)
    assert d.status == "SUPPORTED", d.reason
    # Fallback round: with standalone_level_sentence on (as in BLOCK here), a level line right
    # after the action's own stop sentence is accepted (AFTER_OWN_PROTECTIVE_LEVELS); with it
    # off, no level line anchors (test_entry_rule_standalone).
    clause = "We are now long $QETF. Stop 48.90."
    d = decide(tmp_path, BLOCK, f"{clause}\n50.10 now.", clause=clause)
    assert d.status == "SUPPORTED" and "AFTER_OWN_PROTECTIVE_LEVELS" in d.anchor_attribution
