"""Structured price readings (rubric 2.1 sections 3 D, 6, 13.3): every price-like number of a post gets a
role decided by its linguistic context, UNRESOLVED when the context does not decide. Invented text only:
the rubric's own invented scenarios (sections 12 and 13.6) and contrasts written for this test."""
from __future__ import annotations

import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tradingbot.price_roles import (
    PriceReadingRole as R, ContractMonthEvidence as M, Exactness as X, REASON_CODES, post_numbers, read_price_roles,
)
from tradingbot.source_events import Provenance, SourcePost
from tradingbot.tkl_interpreter import TklInterpreter

T0 = datetime(2026, 11, 5, 14, 30, tzinfo=timezone.utc)
CM, IE, HF, AVG = R.CURRENT_MARKET, R.INTENDED_ENTRY, R.HISTORICAL_FILL, R.AVERAGE_OR_COST_BASIS
STOP, TGT, CHART, OCM, UNR, NAP = R.STOP, R.TARGET, R.CHART_LEVEL, R.OTHER_CONTRACT_MONTH, R.UNRESOLVED, R.NOT_A_PRICE


def post(text, sid="p1", minutes=0):
    t = T0 + timedelta(minutes=minutes)
    return SourcePost(source_id=sid, post_id=sid, author_id="A", verified_author=True, text=text,
                      text_complete=True, is_repost=False, published_at=t, received_at=t,
                      provenance=Provenance.SYNTHETIC)


def readings(text):
    return TklInterpreter().interpret(post(text)).price_readings


def roles(text):
    return [r.role for r in readings(text)]


# ------------------------------------------------------------------ the rubric's invented scenarios

SCENARIOS = {
    1: ("Going long crude here around 71.20. Stop 69.50, target 75.", [CM, STOP, TGT]),
    2: ("Selling gold short at 2,410 on a limit. Stop 2,440.", [IE, STOP]),
    4: ("Bought the S&P at 5,012 a few minutes ago. Market now 5,020.", [HF, CM]),
    5: ("Added to our crude long at 70.10. Average is now 70.65. Stop stays 69.50.", [HF, AVG, STOP]),
    6: ("Lowering the stop on our gold short from 2,440 to 2,425.", [STOP, STOP]),
    8: ("Took half of the S&P long off at 5,080. Stop on the rest to 5,040.", [HF, STOP]),
    9: ("Closing the gold short here, market 2,395.", [CM]),
    10: ("We were stopped out of natural gas overnight at 2.84.", [HF]),
    11: ("Recap: last week we closed crude at 74.90 for a gain of 5.2%.", [HF]),
    12: ("If silver closes above 31.50 we will go long with a stop at 30.20.", [CHART, STOP]),
    13: ("Will buy silver at 30.80 if it pulls back.", [IE]),
    14: ("Closed the crude long at 74.90. We sold gold short at 2,410, stop 2,440.", [HF, HF, STOP]),
    15: ("Long December natural gas at 3.42 on a limit; January is trading at 3.61.", [IE, OCM]),
    16: ("Long natural gas at 3.42 on a limit; the market is trading at 3.61.", [IE, CM]),
    17: ("Bought gold at 2,395 after it held support near 2,380 with RSI at 31. Stop 2,360.", [HF, CHART, NAP, STOP]),
    18: ("Crude bounced from 70 last week. Our stop goes to 70.", [CHART, STOP]),
    25: ("Still long gold, stop unchanged at 2,360. Resistance around 2,450.", [STOP, CHART]),
    26: ("Going long crude at 71.20.", [UNR]),
    27: ("Going long crude at 71.20, the order is working.", [IE]),
    28: ("Going long crude at 71.20, filled on the open.", [HF]),
    29: ("Our crude long was stopped at 69.50 overnight. Buying gold now. If silver closes above 31.50 we will "
         "add it. Rates look heavy.", [HF, CHART]),
    30: ("Closed the crude long at 74.90 and reversed: we are now short crude from 74.85, stop 76.", [HF, HF, STOP]),
}


@pytest.mark.parametrize("k", sorted(SCENARIOS))
def test_rubric_scenario_roles(k):
    text, want = SCENARIOS[k]
    got = readings(text)
    assert [r.n for r in got] == list(range(len(post_numbers(text))))
    assert [r.role for r in got] == want


def test_every_reading_carries_public_reason_codes():
    for text, _want in SCENARIOS.values():
        for r in readings(text):
            assert r.reasons and all(code in REASON_CODES for code in r.reasons)


def test_scenario_1_exactness_and_actions():
    got = readings(SCENARIOS[1][0])
    assert [r.exactness for r in got] == [X.APPROXIMATE, X.EXACT, X.EXACT]
    assert [r.action_id for r in got] == ["a1", "a1", "a1"]
    assert {r.contract_month for r in got} == {M.NOT_STATED}


def test_scenario_2_limit_is_the_only_price_instruction():
    got = readings(SCENARIOS[2][0])
    assert (got[0].role, got[0].exactness, got[0].value) == (IE, X.EXACT, "2410")


def test_scenario_4_fresh_fill_is_not_a_limit():
    got = readings(SCENARIOS[4][0])
    assert got[0].role == HF and "OWN_EXECUTION_PAST" in got[0].reasons
    assert got[1].role == CM and got[1].action_id is None


def test_scenario_5_average_belongs_to_the_add():
    got = readings(SCENARIOS[5][0])
    assert got[1].role == AVG and got[1].action_id == "a1"


def test_scenario_6_amendment_chain():
    got = readings(SCENARIOS[6][0])
    assert got[1].reasons[0] == "SAME_ROLE_AS_PREVIOUS_NUMBER"


def test_scenario_15_named_months_and_other_month():
    got = readings(SCENARIOS[15][0])
    assert (got[0].contract_month, got[0].month_named) == (M.NAMED, "DEC")
    assert (got[1].contract_month, got[1].month_named) == (M.NAMED, "JAN")
    assert got[1].action_id is None


def test_scenario_16_month_never_inferred():
    got = readings(SCENARIOS[16][0])
    assert [r.contract_month for r in got] == [M.NOT_STATED, M.NOT_STATED]
    assert all(r.month_named is None for r in got)


def test_scenario_17_level_and_indicator():
    got = readings(SCENARIOS[17][0])
    assert got[1].exactness == X.APPROXIMATE
    assert (got[2].exactness, got[2].contract_month, got[2].action_id) == (X.NOT_APPLICABLE, M.NOT_APPLICABLE, None)


def test_scenario_26_no_default_role():
    got = readings(SCENARIOS[26][0])
    assert got[0].role == UNR and got[0].reasons == ["ENTRY_KIND_NOT_STATED"]


# ------------------------------------------------------------------ the same words, a different context

@pytest.mark.parametrize("text,want", [
    ("Going long crude at 71.20.", [UNR]),
    ("Going long crude here at 71.20.", [CM]),
    ("Going long crude at 71.20 on a limit.", [IE]),
    ("Going long crude at 71.20, filled on the open.", [HF]),
    ("Going long crude at 71.20 a few minutes ago, got filled.", [HF]),
    ("Going long crude here at 71.20 on a limit.", [UNR]),
])
def test_going_long_at_depends_on_context(text, want):
    assert roles(text) == want


@pytest.mark.parametrize("text,want", [
    ("We closed crude at 74.90.", [HF]),
    ("Crude closed at 74.90.", [CHART]),
    ("Closed the crude long at 74.90.", [HF]),
])
def test_closed_at_depends_on_whose_action(text, want):
    assert roles(text) == want


@pytest.mark.parametrize("text,want", [
    ("Stop at 72 on the crude long.", [STOP]),
    ("Buy stop at 72 for a new crude long.", [IE]),
    ("Stop below support at 70 on the crude long.", [CHART]),
    ("Stop at yesterday's low of 69.80.", [STOP]),
    ("Stop at yesterday's low, 69.80.", [STOP]),
    ("With crude at 71, we hold.", [CM]),
    ("With a stop at 71, we hold.", [STOP]),
])
def test_stop_words_and_levels(text, want):
    assert roles(text) == want


@pytest.mark.parametrize("text,want", [
    ("Our average is 70.65 on the crude long.", [AVG]),
    ("Crude is holding its 50 day moving average at 70.65.", [NAP, CHART]),
    ("Our cost basis in gold is 2,388.", [AVG]),
])
def test_average_is_cost_basis_but_moving_average_is_a_level(text, want):
    assert roles(text) == want


@pytest.mark.parametrize("text,want", [
    ("Gold is trading at 2,400.", [CM]),
    ("Gold was trading at 2,400 yesterday.", [UNR]),
    ("Gold rallied to 2,450 last week.", [CHART]),
    ("Gold looks heavy below 2,400.", [CHART]),
])
def test_market_words_and_tense(text, want):
    assert roles(text) == want


@pytest.mark.parametrize("text,want", [
    ("We are long crude from 70.", [HF]),
    ("Will add to crude at 69.80 on a dip.", [IE]),
    ("Looking to buy gold at 2,380.", [IE]),
    ("Bought 2 contracts of crude at 70.10.", [NAP, HF]),
    ("Crude long, entry 70.10.", [UNR]),
])
def test_fills_plans_and_sizes(text, want):
    assert roles(text) == want


def test_month_word_as_contract_versus_as_time():
    named = readings("Long December crude at 70 on a limit.")
    assert (named[0].contract_month, named[0].month_named) == (M.NAMED, "DEC")
    timed = readings("In December we bought crude at 70.")
    assert (timed[0].role, timed[0].contract_month, timed[0].month_named) == (HF, M.NOT_STATED, None)


def test_equity_ticker_has_no_contract_month():
    got = readings("We are now long $TLT at 88.50 on a limit. Stop: 86.00.")
    assert [r.contract_month for r in got] == [M.NOT_APPLICABLE, M.NOT_APPLICABLE]


def test_dates_times_and_years_are_not_prices():
    got = readings("On Dec 5 at 10 am we will buy crude at 70.10 on a limit. Holding since 2024.")
    assert [r.role for r in got] == [NAP, NAP, IE, NAP]


# ------------------------------------------------------------------ the number reader

def test_number_reader_matches_the_product_reader():
    text = "Long the S&P 500 at ~6,850, stop 6,950 (+1.5%), target 6,700."
    assert [x["token"] for x in post_numbers(text)] == ["~6,850", "6,950", "6,700"]
    assert [r.value for r in read_price_roles(text)] == ["6850", "6950", "6700"]


def test_reader_without_interpreter_context_has_no_action():
    got = read_price_roles("Going long crude here around 71.20. Stop 69.50.")
    assert [r.role for r in got] == [CM, STOP]
    assert all(r.action_id is None and r.association == "NONE" for r in got)


def test_empty_and_numberless_posts():
    assert readings("Nothing to do today.") == []
    assert read_price_roles("") == []


# ------------------------------------------------------------------ no execution decision changes

def test_price_roles_no_decision_change():
    """The readings are additive: on the invented corpus (every test string and fixture post) the
    interpreter's existing output and the replay and mapping decisions equal those of origin/main
    5f8a18f, recorded before the readings existed."""
    import json
    import _decision_corpus as DC
    golden = json.loads(DC.GOLDEN.read_text())
    texts = DC.corpus_texts() + [p.text for p in DC.fixture_posts()]
    now = DC.interpreter_digests(texts)
    missing = [k for k in golden["interpreter"] if k not in now]
    changed = [k for k, v in golden["interpreter"].items() if k in now and now[k] != v]
    assert not missing and not changed
    with tempfile.TemporaryDirectory() as d:
        assert DC.decision_digests(Path(d)) == golden["decisions"]
