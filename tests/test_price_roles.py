"""Structured price readings (rubric 2.1 sections 3 D, 6, 13.3): every price-like number of a post gets a
role decided by its linguistic context, UNRESOLVED when the context does not decide. Invented text only:
the rubric's own invented scenarios (sections 12 and 13.6) and contrasts written for this test."""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pytest

import tradingbot.price_roles as PR
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
    # rubric: INTENDED_ENTRY; the allow-list withholds an order under a condition (CONDITIONAL): a recall cost
    13: ("Will buy silver at 30.80 if it pulls back.", [UNR]),
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


# ------------------------------------------------------------------ every reason code has an asserting example

# code: (invented text, index of the number that must carry the code)
CODE_EXAMPLES = {
    "SIZE_OR_COUNT": ("Bought 2 contracts of crude at 70.10.", 0),
    "TIME_PERIOD": ("Holding for 3 weeks, long crude at 70 on a limit.", 0),
    "TIME_OF_DAY": ("At 10 am we will buy crude at 70.10 on a limit.", 0),
    "DISTANCE_OR_CHANGE": ("Crude is up 2 points at 72.", 0),
    "MA_PERIOD": ("The 50 ma is at 70.", 0),
    "INDICATOR_VALUE": ("Bought gold at 2,395 with RSI at 31.", 1),
    "DATE": ("On Dec 5 we bought crude at 70.", 0),
    "DATE_OR_CONTRACT_YEAR": ("Long crude Dec 2026 at 70 on a limit.", 0),
    "YEAR": ("Holding since 2024.", 0),
    "PROFIT_OR_LOSS": ("We made 500 on the trade.", 0),
    "LIST_MARKER": ("1) Long crude at 70 on a limit\n2) Stop 68", 0),
    "STOP_WORD": ("Stop at 72 on the crude long.", 0),
    "TARGET_WORD": ("Target 75 on the crude long.", 0),
    "LEVEL_WORD": ("Resistance at 2,450.", 0),
    "AVERAGING_WORD": ("Our average is 70.65 on the crude long.", 0),
    "MARKET_WORD": ("Market now 5,020.", 0),
    "ROLE_WORD_AFTER_NUMBER": ("The 69.50 stop was hit, filled 69.40.", 0),
    "STOP_AT_LEVEL": ("Stop at yesterday's low of 69.80.", 0),
    "STOP_RELATIVE_TO_LEVEL": ("Stop below support at 70 on the crude long.", 0),
    "SAME_ROLE_AS_PREVIOUS_NUMBER": ("Lowering the stop on our gold short from 2,440 to 2,425.", 1),
    "CONDITION_LEVEL": ("If silver closes above 31.50 we will go long.", 0),
    "THRESHOLD_LEVEL": ("Gold looks heavy below 2,400.", 0),
    "MARKET_MOVE_LEVEL": ("Gold rallied to 2,450 last week.", 0),
    "OWN_EXECUTION_PAST": ("We bought crude at 71.20.", 0),
    "FILL_WORD": ("Going long crude at 71.20, filled on the open.", 0),
    "POSITION_HELD_FROM": ("We are long crude from 70.", 0),
    "CLAUSE_PAST_EXECUTION": ("We are out of the crude long at 74.90.", 0),
    "FILLED_ORDER": ("Our limit at 71.20 was filled.", 0),
    "ORDER_WORD": ("Bids at 68.", 0),
    "PLANNED_TRANSACTION": ("Will buy silver at 30.80 if it pulls back.", 0),
    "CONDITIONAL_PLAN": ("If CPI is soft we go long gold at 2,380.", 0),
    "MARKET_STATE_WORD": ("Gold is trading at 2,400.", 0),
    "WITH_MARKET_AT": ("With crude at 71, we hold.", 0),
    "MONTH_DIFFERS_FROM_ACTION": ("Long December natural gas at 3.42 on a limit; January is trading at 3.61.", 1),
    "ORDER_COMPLETED": ("Gold limit at 2,380 was hit this morning.", 0),
    "EXECUTED_AT_STOP_OR_TARGET": ("Exited at our 75 target.", 0),
    "STOP_OR_TARGET_EXECUTED": ("Stop hit at 69.50.", 0),
    "ENTRY_KIND_NOT_STATED": ("Going long crude at 71.20.", 0),
    "EXIT_KIND_NOT_STATED": ("Out of crude at 74.90 this morning.", 0),
    "CONFLICTING_CUES": ("We are short gold here at 2,410 on a limit.", 0),
    "PAST_MARKET_PRICE": ("Gold was trading at 2,400 yesterday.", 0),
    "ENTRY_OR_TRIGGER_LEVEL": ("Long crude above 72.", 0),
    "OPTION_STRIKE": ("Bought the 2,400 calls.", 0),
    "NO_ROLE_EVIDENCE": ("Crude near 70.", 0),
    "QUESTION": ("Would you buy gold at 2,380?", 0),
    "COUNTERFACTUAL": ("We would have bought crude at 71.20.", 0),
    "CANCELLED_OR_WITHDRAWN": ("Our limit at 71.20 was cancelled.", 0),
    "NEGATED": ("I would not chase crude at 74.", 0),
    "NOT_FILLED": ("The limit at 2,380 has not filled yet.", 0),
    "THIRD_PARTY": ("Others are bidding crude at 71.20.", 0),
    "FORECAST": ("Crude will likely retest 70.", 0),
    "NO_TRANSACTION_WORD": ("We will look at gold again near 2,380.", 0),
    "POSITION_ALREADY_HELD": ("Long from 70 on a limit.", 0),
    "CONDITIONAL_MARKET_STATE": ("If crude is trading at 71.20 tomorrow we will buy.", 0),
    "HYPOTHETICAL_MARKET_STATE": ("Crude trading at 71.20 would be a gift.", 0),
    "FUTURE_MARKET_STATE": ("Crude is trading at 71.20 tomorrow.", 0),
    "CHAIN_WITH_UNRESOLVED": ("Long crude between 70 and 72 on a limit.", 1),
    "REPORTED_SPEECH": ('He said "buy gold at 2,380 on a limit" but we disagree.', 0),
    "ABANDONED_PLAN": ("We had planned to buy crude at 70 on a limit.", 0),
    "NEAR_MISS": ("We almost bought crude at 70.", 0),
    "CONDITIONAL": ("Will buy silver at 30.80 if it pulls back.", 0),
    "MODAL_OR_OPINION": ("We think a bid at 2,380 works.", 0),
    "SUBJECT_NOT_PUBLISHER": ("The white metal: bids at 30.", 0),
    "SUBJECT_NOT_THE_MARKET": ("The yellow metal is trading at 2,400.", 0),
    "CUE_OUTSIDE_CLAUSE": ("Long gold at 2,380, then buy crude on a limit.", 0),
    "WITHDRAWN_LATER_IN_POST": ("Bid gold at 2,380 on a limit. Actually, cancel that.", 0),
    "ATTRIBUTED_NEXT_SENTENCE": ("From a reader's email. Bid gold at 2,380 on a limit.", 0),
    "MONTH_NAMED_IN_SEGMENT": ("Long the Z6 contract at 70 on a limit.", 0),
    "MONTH_NAMED_IN_SENTENCE": ("Long the December contract, limit 70.", 0),
    "MONTH_OF_ACTION": ("Going long December crude at 71.20 on a limit. Stop 69.50.", 1),
    "FRONT_MONTH_WORD": ("Front month crude trades at 71.", 0),
    "NEXT_MONTH_WORD": ("Long crude at 70 on a limit, next month contract at 71.", 1),
}
# codes asserted by their own tests below (a fault, the number cap, missing clause context)
CODES_WITH_OWN_TEST = {"READER_FAULT", "NUMBER_CAP_REACHED", "NO_CLAUSE_CONTEXT"}


def test_every_reason_code_has_an_example():
    assert set(CODE_EXAMPLES) | CODES_WITH_OWN_TEST == REASON_CODES
    assert not set(CODE_EXAMPLES) & CODES_WITH_OWN_TEST


@pytest.mark.parametrize("code", sorted(CODE_EXAMPLES))
def test_reason_code_example(code):
    text, n = CODE_EXAMPLES[code]
    got = readings(text)
    assert code in got[n].reasons
    assert all(c in REASON_CODES for r in got for c in r.reasons)


@pytest.mark.parametrize("text,word", [
    ("Stop 69.50 area.", "area"), ("Target 75 zone.", "zone"), ("Support 70 region.", "region"),
    ("Target 75 ish.", "ish"), ("Crude is sitting at the 71 handle.", "handle"), ("Target 75 or so.", "or so"),
])
def test_approximation_words_after_the_number(text, word):
    assert readings(text)[0].exactness == X.APPROXIMATE


@pytest.mark.parametrize("text,code", [("Long CLZ6 at 70 on a limit.", "Z6"), ("Long the Z6 contract at 70 on a limit.", "Z6")])
def test_futures_codes_name_the_month(text, code):
    got = readings(text)[0]
    assert (got.role, got.contract_month, got.month_named) == (IE, M.NAMED, code)


# ------------------------------------------------------------------ the review's findings (named inputs)

@pytest.mark.parametrize("text,code", [
    # M1: an order, plan or market word is not enough when the clause defeats it
    ("Our limit at 71.20 was cancelled.", "CANCELLED_OR_WITHDRAWN"),
    ("Pulled our bid at 2,380.", "CANCELLED_OR_WITHDRAWN"),
    ("Cancel the buy limit at 70.", "CANCELLED_OR_WITHDRAWN"),
    ("The order at 71.20 is gone, we are now flat.", "CANCELLED_OR_WITHDRAWN"),
    ("Gold at 2,410 on a limit order earlier, we cancelled it.", "CANCELLED_OR_WITHDRAWN"),
    ("We are no longer bidding gold at 2,380.", "NEGATED"),
    ("Nobody should be bidding gold at 2,400.", "NEGATED"),
    ("Others are bidding crude at 71.20.", "THIRD_PARTY"),
    ("I would not chase crude at 74.", "NEGATED"),
    ("Would you buy gold at 2,380?", "QUESTION"),
    ("We will look at gold again near 2,380.", "NO_TRANSACTION_WORD"),
    ("Gold will probably test 2,380 before rallying.", "FORECAST"),
    ("Crude will likely retest 70.", "FORECAST"),
    ("Gold buyers will step in at 2,380.", "THIRD_PARTY"),
    ("The market expects crude at 80 by year end.", "FORECAST"),
    ("Spot gold forecast 2,600 next year.", "FORECAST"),
    ("If crude is trading at 71.20 tomorrow we will buy.", "CONDITIONAL_MARKET_STATE"),
    ("Gold trades at 2,400 tomorrow if CPI is soft.", "CONDITIONAL_MARKET_STATE"),
    ("Crude trading at 71.20 would be a gift.", "HYPOTHETICAL_MARKET_STATE"),
    # M2: a position held is not an order
    ("Crude long from 70 is working nicely.", "ENTRY_KIND_NOT_STATED"),
    # S2: no fill from negated or counterfactual text
    ("We had a bid at 71.20 yesterday but it never filled.", "NOT_FILLED"),
    ("The limit at 2,380 has not filled yet.", "NOT_FILLED"),
    ("We would have bought crude at 71.20.", "COUNTERFACTUAL"),
    ("Crude was at 71.20 when we went long.", "PAST_MARKET_PRICE"),
])
def test_m1_m2_s2_defeated_readings_are_unresolved(text, code):
    got = readings(text)[0]
    assert got.role == UNR and got.reasons[0] == code


def test_m1_range_ends_never_split():
    got = readings("Long crude between 70 and 72 on a limit.")
    assert [r.role for r in got] == [UNR, UNR]
    assert [r.exactness for r in got] == [X.APPROXIMATE, X.APPROXIMATE]


def test_m2_a_hit_limit_is_a_fill_and_a_held_position_is_not_an_order():
    hit = readings("Gold limit at 2,380 was hit this morning.")[0]
    assert hit.role == HF and hit.reasons[:2] == ["ORDER_COMPLETED", "FILLED_ORDER"]
    assert readings("Our crude long from 70 is still working.")[0].role == HF
    assert readings("Gold limit at 2,380 was not hit.")[0].role == UNR


def test_m3_another_markets_plan_is_not_tied_to_the_only_action():
    got = readings("We bought crude at 70 a minute ago. Gold looks heavy, bids at 2,380 would be my plan.")
    assert (got[0].role, got[0].action_id) == (HF, "a1")
    assert (got[1].action_id, got[1].association) == (None, "NONE")
    # the same market (or a clause naming no market) still ties to the only action
    same = readings(SCENARIOS[5][0])
    assert (same[1].action_id, same[1].association) == ("a1", "SOLE_ACTION_IN_POST")


@pytest.mark.parametrize("text,want", [
    # S1 (rubric 14.1): an executed price is a fill even at the stop or the target
    ("Exited at our 75 target.", [HF]), ("Took profit at the 75 target.", [HF]),
    ("We were stopped at our stop of 69.50.", [HF]), ("Target hit at 75 this morning, we are flat.", [HF]),
    ("Stop hit at 69.50.", [HF]), ("Stopped out at 2.84.", [HF]), ("Took profits at 75.", [HF]),
    # a level named AS the stop or the target stays STOP or TARGET
    ("The 69.50 stop was hit, filled 69.40.", [STOP, HF]), ("Our stop at 69.50 was hit overnight.", [STOP]),
    ("Hit our target of 75 and exited.", [TGT]), ("Target 75 reached, we sold at 75.10.", [TGT, HF]),
])
def test_s1_executed_price_at_stop_or_target(text, want):
    assert roles(text) == want


@pytest.mark.parametrize("text,want", [
    ("Gold is trading at 2,400 and we are long from 2,350.", [CM, HF]),
    ("Crude is trading at 71.20, we bought at 70.90 a minute ago.", [CM, HF]),
])
def test_s2_a_market_word_of_the_number_beats_the_clause_tense(text, want):
    assert roles(text) == want


@pytest.mark.parametrize("text,n", [
    ("On Dec 12 we bought crude at 70.", 1), ("Long Dec 26 crude at 70 on a limit.", 1),
    ("December was a good month: we bought crude at 70.", 0), ("Long crude at 70 on a limit; the December high was 75.", 1),
    ("In the second month of the trade we bought crude at 70.", 0), ("Long crude at 70 on a limit, next month contract at 71.", 0),
])
def test_s3_no_contract_month_from_dates_times_or_another_segment(text, n):
    got = readings(text)[n]
    assert (got.contract_month, got.month_named) == (M.NOT_STATED, None)


def test_s3_a_contract_year_after_a_month_still_names_it():
    got = readings("Long crude Dec '26 at 70 on a limit.")
    assert got[0].reasons == ["DATE_OR_CONTRACT_YEAR"]
    assert (got[1].contract_month, got[1].month_named) == (M.NAMED, "DEC")


@pytest.mark.parametrize("text,want", [
    # no longer CHART_LEVEL; "conviction" is outside the allow-list, so the order is withheld (recall cost)
    ("High conviction long crude at 71.20 on a limit.", [UNR]),
    ("Bought protective puts at 2,300.", [UNR]),
    ("Raise the stop to breakeven at 70.10.", [STOP]),
    ("Crude made a new high at 75.", [CHART]),
])
def test_n4_one_word_errors(text, want):
    assert roles(text) == want


# ------------------------------------------------------------------ cost (S4)

@pytest.mark.parametrize("text", [
    ", ".join(str(70 + i % 50) for i in range(2000)),
    " - ".join(str(70 + i % 9) for i in range(3000)),
    ("Long crude at 71.20 on a limit, stop 69.50 and target 75 " * 400)[:20000],
    (" ".join(f"buy {70 + i % 9} contracts gold" for i in range(4000)))[:20000],
    ("Gold &amp; crude at 71.20 &gt; 70 now " * 600)[:20000],
], ids=["commas", "dashes", "sentences", "one_sentence", "entities"])
def test_cost_per_post_is_bounded(text):
    """Each of these took 1.7 to 3.8 s before the fix (quadratic); now well under 0.1 s. The bound is
    generous so the test is not flaky on a slow machine."""
    t0 = time.perf_counter()
    got = readings(text)
    assert time.perf_counter() - t0 < 1.5
    assert [r.n for r in got] == list(range(len(post_numbers(text))))
    capped = [r for r in got if r.n >= PR.MAX_NUMBERS]
    assert all(r.role == UNR and r.reasons == ["NUMBER_CAP_REACHED"] and r.exactness == X.NOT_APPLICABLE
               for r in capped)
    assert all("NUMBER_CAP_REACHED" not in r.reasons for r in got if r.n < PR.MAX_NUMBERS)


@pytest.mark.parametrize("raw", ["Crude — 71.20 — stop 69.50", "Gold &amp; crude &gt; 70 — now", "plain 70 text",
                                 "&#8212; 71 &nbsp;72 \u00a0 73 \u2019 74 \u2013 75", ""])
def test_offset_map_equals_the_normalised_prefix(raw):
    from tradingbot.tkl_interpreter import _norm_offsets, _normalise
    f = _norm_offsets(raw)
    assert [f(p) for p in range(len(raw) + 1)] == [len(_normalise(raw[:p])) for p in range(len(raw) + 1)]


# ------------------------------------------------------------------ no execution decision changes (S6)

def test_price_readings_are_additive(monkeypatch, tmp_path):
    """The readings are additive, for this change and every later one: on the invented corpus (every
    test string and fixture post) the interpreter's whole existing output and the replay (gateway +
    coordinator) and shipped-mapping decisions are identical with the price reader and with the price
    reader patched to return nothing."""
    import _decision_corpus as DC
    import tradingbot.tkl_interpreter as TI
    texts = DC.corpus_texts() + [p.text for p in DC.fixture_posts()]
    seen: list[int] = []
    with_readings = DC.interpreter_digests(texts, seen)
    decisions_with = DC.decision_digests(tmp_path / "with")
    assert sum(seen) > 1000   # the reader really ran
    monkeypatch.setattr(TI.TklInterpreter, "_price_readings", lambda self, *a, **k: [])
    monkeypatch.setattr(TI, "safe_read_price_roles", lambda *a, **k: [])
    none_seen: list[int] = []
    without = DC.interpreter_digests(texts, none_seen)
    assert sum(none_seen) == 0
    assert without == with_readings
    assert DC.decision_digests(tmp_path / "without") == decisions_with


# ------------------------------------------------------------------ faults leave a trace (N3)

def test_a_reader_fault_never_disturbs_the_interpretation(monkeypatch):
    text = SCENARIOS[1][0]
    before = TklInterpreter().interpret(post(text))

    def boom(*_a, **_k):
        raise RuntimeError("invented fault")
    monkeypatch.setattr(PR, "read_price_roles", boom)
    count = PR.FAULT_COUNTS["READER_FAULT"]
    after = TklInterpreter().interpret(post(text))
    assert after.model_dump(exclude={"price_readings"}) == before.model_dump(exclude={"price_readings"})
    assert [r.reasons for r in after.price_readings] == [["READER_FAULT"]] * 3
    assert {r.exactness for r in after.price_readings} == {X.NOT_APPLICABLE}
    assert PR.FAULT_COUNTS["READER_FAULT"] == count + 1


def test_a_clause_fault_is_counted_and_visible(monkeypatch, caplog):
    text = SCENARIOS[1][0]
    before = TklInterpreter().interpret(post(text))

    def boom(*_a, **_k):
        raise RuntimeError("invented fault")
    monkeypatch.setattr(TklInterpreter, "_price_units_checked", boom)
    count = PR.FAULT_COUNTS["CLAUSE_CONTEXT_FAULT"]
    with caplog.at_level("WARNING", logger="tradingbot.price_roles"):
        after = TklInterpreter().interpret(post(text))
    assert after.model_dump(exclude={"price_readings"}) == before.model_dump(exclude={"price_readings"})
    assert [r.role for r in after.price_readings] == [CM, STOP, TGT]
    assert all(r.reasons[-1] == "NO_CLAUSE_CONTEXT" and r.action_id is None for r in after.price_readings)
    assert PR.FAULT_COUNTS["CLAUSE_CONTEXT_FAULT"] == count + 1
    assert any("CLAUSE_CONTEXT_FAULT" in m for m in caplog.messages)
    assert not any("71.20" in m or "crude" in m.lower() for m in caplog.messages)   # code only, never text


def test_a_clause_split_mismatch_is_counted(monkeypatch):
    text = SCENARIOS[1][0]
    monkeypatch.setattr(TklInterpreter, "_clause_spans", lambda self, t: [])
    count = PR.FAULT_COUNTS["CLAUSE_SPLIT_MISMATCH"]
    got = TklInterpreter().interpret(post(text)).price_readings
    assert all(r.reasons[-1] == "NO_CLAUSE_CONTEXT" for r in got)
    assert PR.FAULT_COUNTS["CLAUSE_SPLIT_MISMATCH"] == count + 1
    assert set(PR.FAULT_CODES) >= {"READER_FAULT", "CLAUSE_CONTEXT_FAULT", "CLAUSE_SPLIT_MISMATCH"}
