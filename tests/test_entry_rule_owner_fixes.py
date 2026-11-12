"""Fix round of PR #70 (review of 510350e), invented text only.

Blocker: a second protective stop anywhere in the post makes the anchored entry's stop not unique.
a-d: narrow rules for missed secondary-market references; e: commentary does not share a stop;
f: closing a held long still works under long_only. Pinned: the documented over-blocking of the
unattributed-action rule and the lowercase-verb residual.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from runtime_harness import MAPPING, Runtime, post
from test_entry_anchor_policy import ANCHOR_RULE, MAP_NOW, etf_entry, policy
from test_entry_anchor_policy import action as etf_action
from test_entry_anchor_single_market import note

from tradingbot.event_models import ParsedPost

SMP = ANCHOR_RULE.replace("SENTENCE_STRICT", "SINGLE_MARKET_POST")
C = "We are now long $QETF, 50.10 now. Stop 48.90."


def decide(tmp_path, post_text, clause=C, levels=(("REFERENCE", "50.10"), ("STOP", "48.90"))):
    p = policy(tmp_path, etf_entry(rule=SMP))
    _, [d, _] = p.apply(ParsedPost(actions=[etf_action(), note()]),
                        clauses={"a1": clause, "n1": ""}, now=MAP_NOW, post_text=post_text,
                        levels={"a1": list(levels)})
    return d


def run(posts):
    with tempfile.TemporaryDirectory() as d:
        rt = Runtime(Path(d), [post(str(i + 1), 30 * i, t) for i, t in enumerate(posts)],
                     mapping_file=MAPPING)
        rt.run(30)
        out = [(a["action"], a["status"], a["reasons"]) for a in rt.actions()
               if a["action"] not in (None, "NO_TRADE")]
        orders = rt.broker.all_orders()
        flat = not rt.broker.positions()
        rt.service.close()
        return out, orders, flat


BASE = "We are now long $TLT, 88.42 now. Stop 87.20."


# ================================================================== blocker: a second stop


@pytest.mark.parametrize("extra", ["Stop 87.10.", "Hard stop 86.90.", "Mental stop 87.50.",
                                   "Trailing stop 87.40.", "Stop-loss 87.00.", "Stoploss 87.00."])
def test_a_second_stop_anywhere_in_the_post_holds_the_anchored_entry(extra):
    out, orders, _ = run([f"{BASE} {extra}"])
    assert not orders and "PROTECTIVE_STOP_NOT_UNIQUE" in out[0][2], out


def test_the_hold_names_the_text_level_reason(tmp_path):
    d = decide(tmp_path, f"{C} Hard stop 48.50.")
    assert d.reason.split(",")[:2] == ["PROTECTIVE_STOP_NOT_UNIQUE", "SEVERAL_STOP_VALUES"]
    d = decide(tmp_path, "We are now long $QETF, 50.10 now. Stop 48.50.")  # text differs
    assert d.reason.split(",")[:2] == ["PROTECTIVE_STOP_NOT_UNIQUE", "STOP_TEXT_DIFFERS"]


@pytest.mark.parametrize("extra", ["Stop 87.20 again.", "The stop stays at 87.20.",
                                   "We got stopped out last month."])
def test_a_repeated_stop_or_a_report_of_being_stopped_is_not_a_second_stop(extra):
    out, _, _ = run([f"{BASE} {extra}"])
    assert all("PROTECTIVE_STOP_NOT_UNIQUE" not in o[2] for o in out), out


# ================================================================== a-d: missed second instruments


@pytest.mark.parametrize("extra,rule", [
    ("Qorvex-long as well.", None),  # a: hyphen split (a mention with it holds first)
    ("Long/short Qorvex.", None),  # a: slash split
    ("We stay long-only here as well.", "RULE:UNATTRIBUTED_ACTION_SENTENCE"),  # a, no unresolved mention
    ("We went long/short.", "RULE:UNATTRIBUTED_ACTION_SENTENCE"),
    ("We bought Qorvex too.", None),  # b: past form with a mention
    ("Accumulating Qorvex here.", "RULE:MENTION_WITH_CONTEXT"),  # b: acquisition verb
    ("We are picking up Qorvex.", "RULE:MENTION_WITH_CONTEXT"),
    ("Qorvex closed the gap.", "RULE:MENTION_WITH_CONTEXT"),  # b with a sentence-initial name
    ("Qorvex stop 44.", None),  # c (a second stop value also holds it)
    ("Qorvex target 44.", "RULE:MENTION_WITH_CONTEXT"),  # c: sentence-initial name, target
    ("Qorvex at 12.50 is cheap.", "RULE:MENTION_WITH_CONTEXT"),  # c: price-like level
])
def test_a_second_instrument_these_rules_catch_holds(tmp_path, extra, rule):
    d = decide(tmp_path, f"{C} {extra}")
    assert d.status == "UNRESOLVED", d.reason
    if rule:
        assert rule in d.reason.split(","), d.reason


@pytest.mark.parametrize("text", [
    "We are now long $QETF, 50.10 now.\nQorvex too.\nStop 48.90.",
    "We are now long $QETF, 50.10 now. Doing the same with Qorvex. Stop 48.90.",
    "We are now long $QETF, 50.10 now. Likewise Qorvex. Stop 48.90.",
])
def test_d_an_additive_ellipsis_after_the_instruction_holds(tmp_path, text):
    d = decide(tmp_path, text, clause="We are now long $QETF, 50.10 now.")
    assert d.status == "UNRESOLVED" and "RULE:ADDITIVE_ELLIPSIS" in d.reason.split(","), d.reason


@pytest.mark.parametrize("extra", ["We bought more last week.", "It was quiet into the close.",
                                   "Qorvex reported earnings.", "Qorvex is quiet."])
def test_b_c_do_not_fire_without_a_mention_or_context(tmp_path, extra):
    d = decide(tmp_path, f"{C} {extra}")
    assert d.status == "SUPPORTED", d.reason


@pytest.mark.parametrize("extra", ["Get qorvex.", "Load qorvex."])  # pinned residual
def test_residual_a_lowercase_market_with_another_verb_is_not_detected(tmp_path, extra):
    """Documented residual: a lowercase unknown market with a verb outside the lists."""
    assert decide(tmp_path, f"{C} {extra}").status == "SUPPORTED"


OVER_BLOCKING = ["The long end is bid.", "In short, we like it.", "Enjoy the long weekend.",
                 "Add to that the CPI print.", "Buy the dip, as always.",
                 "Exit plan: below the stop.", "Long story short, it works.",
                 "Do not sell in panic.", "We like the long-term picture.",
                 "Long-duration Treasuries look oversold."]


@pytest.mark.parametrize("extra", OVER_BLOCKING)
def test_pinned_what_the_unattributed_action_rule_costs(tmp_path, extra):
    """Documented over-blocking (not fixed): a side, add or exit word outside the instruction."""
    d = decide(tmp_path, f"{C} {extra}")
    assert d.status == "UNRESOLVED", d.reason


# ================================================================== e: commentary shares no stop


def test_e_a_commentary_action_repeating_the_stop_does_not_share_it(tmp_path):
    p = policy(tmp_path, etf_entry(rule=SMP))
    commentary = note("n1")
    _, [d, _] = p.apply(ParsedPost(actions=[etf_action(), commentary]),
                        clauses={"a1": C, "n1": "The 48.90 area matters."}, now=MAP_NOW,
                        post_text=f"{C} The 48.90 area matters.",
                        levels={"a1": [("REFERENCE", "50.10"), ("STOP", "48.90")],
                                "n1": [("REFERENCE", "48.90")]})
    assert "STOP_SHARED_WITH_ANOTHER_ACTION" not in d.reason.split(","), d.reason


# ================================================================== f: long_only still closes


def test_f_a_held_long_is_closed_with_a_sell_under_long_only():
    out, orders, flat = run(["We are now long $TLT at 88.40. Stop 87.20.",
                             "Closing the rest of our $TLT long at 88.40."])
    assert [(o[0], o[1]) for o in out] == [("OPEN", "EXECUTED"), ("CLOSE", "EXECUTED")], out
    assert any(o.side.value == "SELL" and o.parent_ref is None and not o.order_ref.endswith(
        (".STP", ".TGT")) for o in orders)
    assert flat


def test_f_a_reduction_of_one_share_holds_for_the_size_not_for_long_only():
    out, _, _ = run(["We are now long $TLT at 88.40. Stop 87.20.",
                     "Selling half of our $TLT long at 88.40."])
    reduce = [o for o in out if o[0] == "REDUCE"]
    assert reduce and reduce[0][1] == "HOLD" and "NON_INTEGER_REDUCTION" in reduce[0][2], out
    assert "MAPPING_LONG_ONLY" not in reduce[0][2]
