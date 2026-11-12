"""The anchor's context window is an allow-list (confirmation review): every word around a marked
number must be understood, or the anchor is refused (ENTRY_ANCHOR_CONTEXT_UNCLEAR). Invented data.
"""

from __future__ import annotations

import pytest
from test_entry_anchor_policy import (
    LONG,
    action,
    etf_entry,
    oil_action,
    oil_entry,
    policy,
    resolve,
    run_posts,
    service_mapping,
)
from test_entry_anchor_policy import outcomes as _outcomes

from tradingbot.event_models import PriceBasis, Timing, TradeAction
from tradingbot.models import Action, PositionSide
from tradingbot.runtime.mapping import MappingPolicy

HOLDS = {"ENTRY_ANCHOR_CONTEXT_UNCLEAR", "ENTRY_ANCHOR_NOT_CURRENT",
         "ENTRY_ANCHOR_INSTRUMENT_UNCLEAR"}

# ------------------------------------------------------------------ the reviewer's probes

REVIEWER_POSTS = [
    "We are now long $QOIL, 71.60 now is the goal. Stop 69.80.",
    "We are now long $QOIL, now 72 is the magnet. Stop 69.80.",
    "We are now long $QOIL, avg 71.60 now. Stop 69.80.",
    "We are now long $QOIL, cost basis 71.60 now. Stop 69.80.",
    "We are now long $QOIL, sold some at 71.50 now. Stop 69.80.",
    "We are now long $QOIL, Dubai 71.45 now. Stop 69.80.",
    "We are now long $QOIL, QMF 71.45 now. Stop 69.80.",
]


@pytest.mark.parametrize("post", REVIEWER_POSTS)
def test_reviewer_probe_a_marked_number_in_an_unclear_context_never_trades(tmp_path, post):
    run = run_posts(tmp_path, "ctx", [post])
    [(kind, status, reasons)] = _outcomes(run)
    assert kind == "OPEN" and status == "HOLD", reasons
    assert run.broker.submit_calls == 0
    run.close()


def test_reviewer_probe_an_execution_price_is_never_compared_with_the_index(tmp_path):
    run = run_posts(tmp_path, "qif", ["We are now long $QIDX, QIF 5,025 now. Stop 4,980."])
    [(_, status, reasons)] = _outcomes(run)
    assert status == "HOLD" and "ENTRY_ANCHOR_CONTEXT_UNCLEAR" in reasons, reasons
    assert run.broker.submit_calls == 0
    run.close()


# ------------------------------------------------------------------ the mapping decision

LEVELS = [("REFERENCE", "50.10"), ("STOP", "48.90")]

UNCLEAR = [
    "We are now long $QETF, 50.10 now is the goal.",
    "We are now long $QETF, now 50.10 is the magnet.",
    "We are now long $QETF, avg 50.10 now.",
    "We are now long $QETF, cost basis 50.10 now.",
    "We are now long $QETF, sold some at 50.10 now.",
    "We are now long $QETF and sold some at 50.10 now.",
    "We are now long $QETF, Dubai 50.10 now.",
    "We are now long $QETF, QMF 50.10 now.",
    "We are now long $QETF, fair value 50.10 now.",
    "We are now long $QETF, 50.10 now looks cheap.",
    "We are now long $QETF, 50.10 now per my model.",
    "We are now long $QETF, average 50.10 now.",
    "We are now long $QETF, VWAP 50.10 now.",
    "We are now long $QETF, maybe 50.10 now.",
    "We are now long $QETF, sell 50.10 now.",
    "We are now long $QETF, bought 50.10 now.",
    "We are now long $QETF, the dividend 50.10 now.",
    "We are now long $QETF, 50.10 now basically.",
    "We are now long $QETF, premium 50.10 now.",
    "Long $QETF, the other fund 50.10 now.",
]


@pytest.mark.parametrize("clause", UNCLEAR)
def test_a_word_the_bot_does_not_understand_beside_the_number_refuses_the_anchor(tmp_path,
                                                                                 clause):
    d, inst = resolve(policy(tmp_path, etf_entry()), action(), clause, LEVELS)
    assert d.status == "UNRESOLVED" and inst is None and d.entry_anchor is None, d.reason
    assert d.reason.split(",")[0] in HOLDS


def test_the_context_check_names_its_own_reason(tmp_path):
    d, _ = resolve(policy(tmp_path, etf_entry()), action(), "We are now long $QETF, avg 50.10 now.",
                   LEVELS)
    assert d.reason.split(",")[0] == "ENTRY_ANCHOR_CONTEXT_UNCLEAR"


CLEAR = [
    ("We are now long $QETF, 50.10 now.", PositionSide.LONG, None),
    ("Long $QETF here at 50.10.", PositionSide.LONG, None),
    ("Buying $QETF, currently 50.10.", PositionSide.LONG, None),
    ("We are long $QETF now at 50.10.", PositionSide.LONG, None),
    ("$QETF is at 50.10 now, we are long.", PositionSide.LONG, None),
    ("Long $QETF, trading at 50.10.", PositionSide.LONG, None),
    ("Long $QETF at the market, around 50.10 now.", PositionSide.LONG, None),
    ("Short $QETF, 50.10 right now.", PositionSide.SHORT, None),
    ("Selling $QETF short here at 50.10.", PositionSide.SHORT, None),
    ("Long $QETF ~50.10 now.", PositionSide.LONG, None),
    ("Long QETF, it is 50.10 now.", PositionSide.LONG, None),
    ("Long the bond fund, currently 50.10 USD.", PositionSide.LONG, 'aliases = ["bond fund"]'),
]


@pytest.mark.parametrize("clause,side,extra", CLEAR)
def test_ordinary_plain_phrasings_still_anchor(tmp_path, clause, side, extra):
    a = action().model_copy(update={"position_side": side, "stop_price": "48.90" if side ==
                                    PositionSide.LONG else "51.20"})
    levels = [("REFERENCE", "50.10"), ("STOP", a.stop_price)]
    d, _ = resolve(policy(tmp_path, etf_entry(extra=extra or "")), a, clause, levels)
    assert d.status == "SUPPORTED", d.reason
    assert d.entry_anchor == "50.10"


def test_an_add_may_use_its_own_verb(tmp_path):
    d, _ = resolve(policy(tmp_path, etf_entry()), action(kind=Action.ADD, idea="9:a1"),
                   "Adding $QETF here at 50.30.", [("REFERENCE", "50.30")])
    assert d.status == "SUPPORTED" and d.entry_anchor == "50.30", d.reason


def test_a_level_line_passes_the_same_window(tmp_path):
    for line, ok in (("$QETF 50.10 now.", True), ("50.10 now is the goal.", False),
                     ("Avg 50.10 now.", False)):
        d, _ = resolve(policy(tmp_path, etf_entry()), action(), LONG, [("REFERENCE", "50.10")],
                       post_text=f"{LONG}\n{line}")
        assert (d.status == "SUPPORTED") is ok, (line, d.reason)


def test_a_futures_month_beside_the_number_is_left_to_the_contract_check(tmp_path):
    d, inst = resolve(policy(tmp_path, oil_entry()), oil_action(),
                      "We are now long $QOIL Aug 2026 at 71.50 now.",
                      [("REFERENCE", "71.50"), ("STOP", "69.80")])
    assert d.status == "SUPPORTED", d.reason
    assert inst.futures_contract == "202608"


# ------------------------------------------------------------------ rule 3: reference-series names


def idx_policy(tmp_path) -> MappingPolicy:
    f = tmp_path / "m.toml"
    f.write_text(service_mapping())
    return MappingPolicy.from_toml(f)


def idx_action():
    return TradeAction(action_id="a1", action=Action.OPEN, timing=Timing.CURRENT,
                       position_side=PositionSide.LONG, price_basis=PriceBasis.SOURCE_SERIES,
                       source_market_symbol="QIDX", stop_price="4980",
                       explicit_instruction=True, evidence=["invented"])


@pytest.mark.parametrize("clause,ok", [
    ("We are now long $QIDX, 5,010 now.", True),
    ("Long $QIDX here at 5,010.", True),
    ("Long $QIDX, QIDX 5,010 now.", True),
    ("Long $QIDX, QIF 5,010 now.", False),  # the execution future's price
    ("Long $QIDX, $QIF at 5,010 now.", False),
    ("Long $QIDX, futures 5,010 now.", False),
])
def test_a_reference_series_anchor_is_attributed_to_the_reference_or_to_nothing(tmp_path,
                                                                                  clause, ok):
    d, _ = idx_policy(tmp_path).resolve(idx_action(), clause=clause, now=__import__(
        "test_entry_anchor_policy").MAP_NOW, levels=[("REFERENCE", "5010"), ("STOP", "4980")])
    assert (d.status == "SUPPORTED") is ok, d.reason
    if not ok:
        assert d.reason.split(",")[0] in HOLDS | {"ENTRY_ANCHOR_INSTRUMENT_AMBIGUOUS"}


# ------------------------------------------------------------------ a second marked level


def test_a_second_marked_level_on_another_line_holds(tmp_path):
    d, _ = resolve(policy(tmp_path, etf_entry()), action(), LONG, [("REFERENCE", "50.10")],
                   post_text=f"{LONG}\n50.10 now.\n50.40 now.")
    assert d.status == "UNRESOLVED"
    assert d.reason.split(",")[0] == "ENTRY_ANCHOR_SEVERAL_LEVELS"
