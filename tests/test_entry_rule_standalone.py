"""Fallback of PR #66 (lead's decision after review F1): an elliptical level ("88.42 now." on its
own) never anchors by default. The owner's switch ``standalone_level_sentence`` (default false,
shipped false) allows it only where it would have been accepted inside the instruction sentence:
every own instruction sentence must pass the same whole-sentence allow-list as a level's own
sentence. Invented text, whole mock service, the shipped mapping and a copy with the switch on.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from runtime_harness import MAPPING, Runtime, post
from test_entry_anchor_policy import ANCHOR_RULE, etf_entry, policy

from tradingbot.runtime.mapping import MappingPolicy

CUT = "# ------------------------------------------------------------------ templates"


def switched(folder: Path) -> Path:
    head, tail = MAPPING.read_text().split(CUT, 1)
    assert head.count('environment = "PAPER" }') == 1
    head = head.replace('environment = "PAPER" }',
                        'environment = "PAPER", standalone_level_sentence = true }')
    f = folder / "switched.toml"
    f.write_text(head + CUT + tail)
    return f


def run(text: str, key: bool):
    with tempfile.TemporaryDirectory() as d:
        mapping = switched(Path(d)) if key else MAPPING
        (Path(d) / "rt").mkdir()
        rt = Runtime(Path(d) / "rt", [post("1", 0, text)], mapping_file=mapping)
        rt.run(3)
        out = [(a["status"], a["reasons"]) for a in rt.actions() if a["action"] == "OPEN"]
        sent = rt.broker.submit_calls
        rt.service.close()
        return sent, out


F1 = [
    "We are now long $TLT and spy.\n88.42 now.\nStop 87.20.",
    "We are now long $TLT, long ief.\n88.42 now.\nStop 87.20.",
    "We are now long $TLT like the long end.\n88.42 now.\nStop 87.20.",
    "We are now long $TLT, bought last week.\n88.42 now.\nStop 87.20.",
]
ELLIPTICAL = [
    "We are now long $TLT.\n88.42 now.\nStop 87.20.",  # after the instruction
    "We are now long $TLT.\nStop 87.20.\n88.42 now.",  # after the stop sentence
    "88.42 now.\nWe are now long $TLT. Stop 87.20.",  # first in the post
]
INSIDE = [
    "We are now long $TLT, 88.42 now. Stop 87.20.",
    "We are now long $TLT, 88.42 now, stop 87.20.",
]
# The held interpreter does not tag a level line naming the family as this action's level, so the
# family-naming case is tested at the mapping stage (test_a_level_line_naming_the_family_...).
CLEAN = [
    "We are now long $TLT.\n88.42 now.\nStop 87.20.",
    "We are now long $TLT.\nStop 87.20.\n88.42 now.",
    "We are now long $TLT.\nStop 87.20, target 89.50.\n88.42 now.",
]
EARLIER_HOLDS = [  # earlier rounds' probes, all must still hold with the switch on
    "We are now long $TLT.\nOur position.\n88.42 now.\nStop 87.20.",
    "We are now long $TLT.\n88.42 now.\nThat is our cost basis.\nStop 87.20.",
    "We are now long $TLT.\nLast Friday:\n88.42 now.\nStop 87.20.",
    "We are now long $TLT.\nQorvex is heavy.\n88.42 now.\nStop 87.20.",
    "We are now long $TLT.\nStop 87.20, target 89.50.\n88.42 now.\nThe chart looks constructive.",
    "We are now long $TLT, \"88.42 now\". Stop 87.20.",
]


@pytest.mark.parametrize("key", [False, True])
@pytest.mark.parametrize("text", F1)
def test_f1_an_instruction_naming_another_market_never_lends_its_topic(text, key):
    sent, out = run(text, key)
    assert sent == 0 and out and out[0][0] != "EXECUTED", out


@pytest.mark.parametrize("text", ELLIPTICAL)
def test_by_default_an_elliptical_level_never_anchors(text):
    sent, out = run(text, False)
    assert sent == 0 and out and out[0][0] != "EXECUTED", out


def test_the_default_hold_names_the_switch(tmp_path):
    from test_entry_anchor_policy import MAP_NOW
    from test_entry_anchor_policy import action as etf_action
    from test_entry_anchor_single_market import note

    from tradingbot.event_models import ParsedPost
    p = policy(tmp_path, etf_entry(rule=ANCHOR_RULE.replace(", standalone_level_sentence = true",
                                                            "")))
    _, [d, _] = p.apply(ParsedPost(actions=[etf_action(), note()]),
                        clauses={"a1": "We are now long $QETF.", "n1": ""}, now=MAP_NOW,
                        post_text="We are now long $QETF.\n50.10 now.",
                        levels={"a1": [("REFERENCE", "50.10"), ("STOP", "48.90")]})
    assert d.reason.split(",")[:3] == ["ENTRY_ANCHOR_LEVEL_NOT_MARKET_QUOTE",
                                       "LEVEL_KIND:AMBIGUOUS", "STANDALONE_LEVEL_NOT_ENABLED"]


@pytest.mark.parametrize("key", [False, True])
@pytest.mark.parametrize("text", INSIDE)
def test_a_level_inside_the_instruction_or_naming_the_family_still_trades(text, key):
    sent, out = run(text, key)
    assert sent == 1 and out[0][0] == "EXECUTED", out


@pytest.mark.parametrize("text", CLEAN)
def test_with_the_switch_the_clean_standalone_shapes_trade(text):
    sent, out = run(text, True)
    assert sent == 1 and out[0][0] == "EXECUTED", out


@pytest.mark.parametrize("text", EARLIER_HOLDS)
def test_with_the_switch_every_earlier_hold_still_holds(text):
    sent, out = run(text, True)
    assert sent == 0 and out[0][0] != "EXECUTED", out


def test_the_switch_is_off_in_the_shipped_file_and_strict():
    for e in MappingPolicy.from_toml(MAPPING).entries.values():
        if getattr(e, "entry_anchor", None) is not None:
            assert e.entry_anchor.standalone_level_sentence is False


def test_the_old_key_name_is_refused_with_a_clear_message(tmp_path):
    rule = ANCHOR_RULE.replace("standalone_level_sentence", "level_after_own_protective_levels")
    with pytest.raises(ValueError, match="renamed to standalone_level_sentence"):
        policy(tmp_path, etf_entry(rule=rule))
    with pytest.raises(ValueError, match="standalone_level_sentence"):
        policy(tmp_path, etf_entry(rule=ANCHOR_RULE.replace(
            "standalone_level_sentence = true", 'standalone_level_sentence = "yes"')))


@pytest.mark.parametrize("on", [False, True])
def test_a_level_line_naming_the_family_anchors_with_the_switch_off_or_on(tmp_path, on):
    from test_entry_anchor_policy import MAP_NOW
    from test_entry_anchor_policy import action as etf_action
    from test_entry_anchor_single_market import note

    from tradingbot.event_models import ParsedPost
    rule = ANCHOR_RULE if on else ANCHOR_RULE.replace(", standalone_level_sentence = true", "")
    p = policy(tmp_path, etf_entry(rule=rule))
    _, [d, _] = p.apply(ParsedPost(actions=[etf_action(), note()]),
                        clauses={"a1": "We are now long $QETF.", "n1": ""}, now=MAP_NOW,
                        post_text="We are now long $QETF.\n$QETF 50.10 now.\nStop 48.90.",
                        levels={"a1": [("REFERENCE", "50.10"), ("STOP", "48.90")]})
    assert d.status == "SUPPORTED" and d.entry_anchor == "50.10", d.reason
