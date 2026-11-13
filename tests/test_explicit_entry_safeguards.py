"""The owner's decision of 13 November 2026: "An average price or cost basis is NOT automatically a
fresh entry price. If the source announces a position and an average without an independently
resolved fresh actionable entry, HOLD. Explicit-entry TLT instructions must receive the same
relevant multi-market ambiguity checks as anchored entries. Do not silently choose one instrument
from a post containing multiple unresolved actions." Invented text only.

1. ENTRY_PRICE_IS_COST_BASIS (every family): an OPEN or ADD whose stated entry price is, as
   written in the post, introduced by an average or cost-basis word holds at the mapping layer,
   whatever the interpreter made of it.
2. For a family with anchor_scope = SINGLE_MARKET_POST (TLT), an explicit entry receives the
   post-level competing-instrument checks of an anchored entry (ENTRY_ANCHOR_POST_* codes).
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from runtime_harness import MAPPING, Runtime, post
from test_entry_anchor_policy import ANCHOR_RULE, MAP_NOW, action, etf_entry, policy

from tradingbot.models import Action
from tradingbot.runtime.mapping import ENTRY_PRICE_IS_COST_BASIS

NO_RULE = ""  # an explicit-entry-only family: the cost-basis rule applies to every family


def decide(tmp_path, post_text, entry, *, rule=ANCHOR_RULE, kind=Action.OPEN, idea=None):
    """The action exactly as the held interpreter builds it: the average as the explicit entry."""
    p = policy(tmp_path, etf_entry(rule=rule))
    a = action(entry=entry, kind=kind, idea=idea)
    levels = [("ENTRY", entry), ("STOP", "48.90")]
    d, _ = p.resolve(a, clause=post_text.split(" Stop")[0], now=MAP_NOW, levels=levels,
                     post_text=post_text, post_actions=[a])
    return d


# ---------------------------------------------------------------- 1. cost basis


@pytest.mark.parametrize("rule", [ANCHOR_RULE, NO_RULE], ids=["anchor-rule", "explicit-only"])
@pytest.mark.parametrize("text,entry", [
    ("We are now long $QETF, avg 50.10 now. Stop 48.90.", "50.10"),  # the reviewer's probes,
    ("We are now long $QETF, cost basis 50.10 now. Stop 48.90.", "50.10"),  # built the way the
    ("We are now long $QETF, avg cost, 50.00 now. Stop 48.90.", "50.00"),  # held interpreter does
    ("We are now long $QETF, avg. 50.10. Stop 48.90.", "50.10"),
    ("We are long $QETF at an average of 50.10. Stop 48.90.", "50.10"),
    ("We are now long $QETF, blended 50.10. Stop 48.90.", "50.10"),
    ("We are now long $QETF at 50.10 avg. Stop 48.90.", "50.10"),  # directly after the number
    ("We are now long $QETF at 50.10 average. Stop 48.90.", "50.10"),
    ("We are now long $QETF at 50.10 cost basis. Stop 48.90.", "50.10"),
    ("We are now long $QETF, basis 50.10. Stop 48.90.", "50.10"),
])
def test_an_entry_price_introduced_by_an_average_or_cost_basis_word_holds(tmp_path, text, entry,
                                                                         rule):
    d = decide(tmp_path, text, entry, rule=rule)
    assert d.status == "UNRESOLVED" and d.reason.split(",")[0] == ENTRY_PRICE_IS_COST_BASIS, d


def test_an_add_at_the_average_holds_too(tmp_path):
    d = decide(tmp_path, "Adding to $QETF, our avg 50.10. Stop 48.90.", "50.10", rule=NO_RULE,
               kind=Action.ADD, idea="p0:a1")
    assert d.reason.split(",")[0] == ENTRY_PRICE_IS_COST_BASIS, d


@pytest.mark.parametrize("text,entry", [
    ("We are now long $QETF at 50.10. Stop 48.90.", "50.10"),  # a plain explicit entry
    ("Our avg is 49.80. We are now long more $QETF at 50.10. Stop 48.90.", "50.10"),  # independent
])
def test_an_independent_fresh_entry_is_used_as_today(tmp_path, text, entry):
    d = decide(tmp_path, text, entry, rule=NO_RULE)
    assert d.status == "SUPPORTED", d


def test_when_the_entry_is_also_the_average_it_holds(tmp_path):
    d = decide(tmp_path, "Avg 50.10 and we are now long more $QETF at 50.10. Stop 48.90.", "50.10",
               rule=NO_RULE)
    assert d.reason.split(",")[0] == ENTRY_PRICE_IS_COST_BASIS, d


# ---------------------------------------------------------------- 2. explicit TLT, multi-market


def run(text, *more):
    rt = Runtime(Path(tempfile.mkdtemp()), [post(str(i + 1), i * 20, t)
                                            for i, t in enumerate((text, *more))],
                 mapping_file=MAPPING)
    rt.run(3 + 4 * len(more), 30)
    rows = [(a["action"], a["status"], a["mapping_reason"] or "") for a in rt.actions()]
    sent = [o for o in rt.broker.all_orders() if o.parent_ref is None]
    rt.service.close()
    return rows, sent


EXPLICIT = "We are now long $TLT at 88.42. Stop 87.20."


@pytest.mark.parametrize("extra,code", [
    ("Zorbex 12.40.", "ENTRY_ANCHOR_POST_UNRESOLVED_MARKET"),  # PR #73's pin: now holds
    ("Zorbex bought earlier.", "ENTRY_ANCHOR_POST_UNRESOLVED_MARKET"),
    ("$QQZ looks heavy.", "ENTRY_ANCHOR_POST_SEVERAL_MARKETS"),
    ("Also long $GLD at 180.", "ENTRY_ANCHOR_POST_SEVERAL"),
    ("Buy more on any dip.", "ENTRY_ANCHOR_POST_UNATTRIBUTED_ACTION"),
])
def test_an_explicit_tlt_entry_gets_the_multi_market_checks(extra, code):
    rows, sent = run(f"{EXPLICIT} {extra}")
    opens = [r for r in rows if r[0] == "OPEN" and r[2].startswith(code)]
    assert sent == [] and opens, rows


def test_the_additive_ellipsis_holds_an_explicit_entry():
    rows, sent = run("We are now long $TLT at 88.42. Zorbex too. Stop 87.20.")
    assert sent == [] and any("ENTRY_ANCHOR_POST" in r[2] for r in rows), rows


@pytest.mark.parametrize("text", [
    EXPLICIT,
    f"{EXPLICIT} Bonds look firm this week.",  # plain commentary
])
def test_a_clean_explicit_tlt_entry_still_sends(text):
    rows, sent = run(text)
    assert len(sent) == 1, rows


def test_an_exit_is_unchanged_by_a_mention():
    rows, _ = run(EXPLICIT, "Closing our $TLT long at 88.40. Zorbex 12.40.")
    assert ("CLOSE", "EXECUTED", "") in rows, rows


def test_a_family_without_the_scope_is_unchanged(tmp_path):
    """QETF (anchor_scope SENTENCE_STRICT): an explicit entry beside a mention is decided as
    before."""
    d = decide(tmp_path, "We are now long $QETF at 50.10. Stop 48.90. Zorbex 12.40.", "50.10")
    assert d.status == "SUPPORTED", d


# ---------------------------------------------------------------- fix round of PR #75


@pytest.mark.parametrize("text,entry", [
    # (i) a cost-basis word anywhere in the entry's sentence, before or after; ";" does not end it
    ("We are long $QETF at 50.10, our average. Stop 48.90.", "50.10"),
    ("We are long $QETF at 50.10 (our average). Stop 48.90.", "50.10"),
    ("We are long $QETF at 50.10 - our average. Stop 48.90.", "50.10"),
    ("We are long $QETF at 50.10 is our avg. Stop 48.90.", "50.10"),
    ("We are long $QETF at 50.10; cost basis. Stop 48.90.", "50.10"),
    ("We are now long $QETF at 50.10 on average volume. Stop 48.90.", "50.10"),  # collateral
    # (ii) the next sentence refers back and carries no number of its own
    ("We are long $QETF at 50.10. That is our average. Stop 48.90.", "50.10"),
    # a past fill sent as the entry
    ("Bought at 50.42 and 49.80, average 50.10. We are long $QETF. Stop 48.90.", "49.80"),
    # (iii) a cost-basis expression in the post and other prices in the entry's sentence
    ("Bought at 50.42 and 49.80. Average 50.10. We are long $QETF. Stop 48.90.", "50.42"),
    ("Bought at 50.42 and 49.80. Average 50.10. We are long $QETF. Stop 48.90.", "49.80"),
    # the word list: break-even forms, abbreviations, "mean price", glued "avg."
    ("We are now long $QETF, breakeven 50.10. Stop 48.90.", "50.10"),
    ("We are now long $QETF, break-even 50.10. Stop 48.90.", "50.10"),
    ("We are now long $QETF, b/e 50.10. Stop 48.90.", "50.10"),
    ("We are now long $QETF, av. 50.10. Stop 48.90.", "50.10"),
    ("We are now long $QETF, avrg 50.10. Stop 48.90.", "50.10"),
    ("We are now long $QETF, mean price 50.10. Stop 48.90.", "50.10"),
    ("We are now long $QETF, mean cost 50.10. Stop 48.90.", "50.10"),
    ("We are now long $QETF, avg.50.10. Stop 48.90.", "50.10"),
    # a non-ASCII letter in the checked window is unreadable: look-alikes of "avg" never pass
    ("We are now long $QETF, аvg 50.10. Stop 48.90.", "50.10"),  # Cyrillic a
    ("We are now long $QETF, ávg 50.10. Stop 48.90.", "50.10"),  # accented
    ("We are now long $QETF, ａｖｇ 50.10. Stop 48.90.", "50.10"),  # full-width
    # position-from phrasing
    ("We are long $QETF from 50.10. Stop 48.90.", "50.10"),
    ("We are in from 50.10 on $QETF. Stop 48.90.", "50.10"),
    ("Holding $QETF from 50.10. Stop 48.90.", "50.10"),
    ("We are long $QETF since 50.10. Stop 48.90.", "50.10"),
])
def test_an_average_or_position_from_price_is_never_the_fresh_entry(tmp_path, text, entry):
    d = decide(tmp_path, text, entry, rule=NO_RULE)
    assert d.status == "UNRESOLVED" and d.reason.split(",")[0] == ENTRY_PRICE_IS_COST_BASIS, d


@pytest.mark.parametrize("text,entry", [
    ("Our average is 50.10. We are long $QETF at 50.42. Stop 48.90.", "50.42"),  # kept pin
    ("We are long $QETF at 50.10. Our average is 49.90. Stop 48.90.", "50.10"),  # its own number
    ("We are long $QETF at 50.10, stop 48.90. Our avg is 49.90.", "50.10"),  # the stop is the
    ("We are long $QETF at 50.10, a mean reversion trade. Stop 48.90.", "50.10"),  # action's own
    # decided behaviour: a report of the publisher's own entry is followed at that price
    ("We filled at 50.10 on $QETF. Stop 48.90.", "50.10"),
    ("We are in at 50.10 on $QETF. Stop 48.90.", "50.10"),
])
def test_a_separately_stated_single_entry_still_sends(tmp_path, text, entry):
    d = decide(tmp_path, text, entry, rule=NO_RULE)
    assert d.status == "SUPPORTED", d


CASELESS = ["ゾルベックス",  # Japanese (katakana)
            "زوربكس",  # Arabic
            "זורבקס",  # Hebrew
            "ซอร์เบกซ์",  # Thai
            "佐贝克"]  # Chinese


@pytest.mark.parametrize("word", CASELESS, ids=["ja", "ar", "he", "th", "zh"])
@pytest.mark.parametrize("shape", [
    "We are now long $TLT at 88.42. Stop 87.20. {w} is near 12.40.",  # explicit
    "We are now long $TLT, 88.42 now. Stop 87.20. {w} is near 12.40.",  # anchored
], ids=["explicit", "anchored"])
def test_a_name_in_a_caseless_script_is_a_possible_market(word, shape):
    rows, sent = run(shape.format(w=word))
    assert sent == [], rows
    assert any(r[0] == "OPEN" and r[2].startswith("ENTRY_ANCHOR_POST_UNRESOLVED_MARKET")
               for r in rows), rows


@pytest.mark.parametrize("text", [
    "We are now long $TLT at 88.42; stop 87.20.",
    "Buying $TLT at 88.42; stop 87.20; target 90.",
])
def test_the_actions_own_protective_levels_after_a_semicolon_belong_to_it(text):
    rows, sent = run(text)
    assert len(sent) == 1, rows


@pytest.mark.parametrize("text", [
    "We are now long $TLT at 88.42; buy more at 87.50. Stop 87.20.",  # not only protective
    f"{EXPLICIT} Buy the dip.",  # figurative instruction words hold (decided)
    f"{EXPLICIT} The long end is bid.",
])
def test_other_semicolon_and_commentary_sentences_still_hold(text):
    rows, sent = run(text)
    assert sent == [], rows
