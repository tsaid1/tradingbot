"""The independent review's 161 invented sentences (orchestration/reviews/PRICE_ROLES_review.md) as a
regression set for the structured price readings. Invented text only.

Each row: id, sentence, the review's verdict on the readings it observed, the roles observed by the
review (before the fixes), and the roles expected now.
  * R (right): the readings must stay exactly as observed.
  * D (dangerous: a wrong INTENDED_ENTRY or CURRENT_MARKET, or an entry price tied to the wrong
    action): the expected roles below are asserted exactly, and no number may be INTENDED_ENTRY or
    CURRENT_MARKET unless the expected role says so.
  * W (wrong in a safe direction) and C (too cautious): where a finding of the review fixes the row
    (S1, S2, S3, N4) the expected roles are asserted exactly; otherwise each number must be either
    the role observed before or the role the review expects (never a third role), and never a new
    INTENDED_ENTRY or CURRENT_MARKET that the review does not expect."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from tradingbot.price_roles import ContractMonthEvidence as M, Exactness as X, PriceReadingRole as R
from tradingbot.source_events import Provenance, SourcePost
from tradingbot.tkl_interpreter import TklInterpreter

CM, IE, HF, AVG = R.CURRENT_MARKET, R.INTENDED_ENTRY, R.HISTORICAL_FILL, R.AVERAGE_OR_COST_BASIS
STOP, TGT, CHART, OCM, UNR, NAP = R.STOP, R.TARGET, R.CHART_LEVEL, R.OTHER_CONTRACT_MONTH, R.UNRESOLVED, R.NOT_A_PRICE
EXEC = (IE, CM)
T0 = datetime(2026, 11, 5, 14, 30, tzinfo=timezone.utc)

# id: (sentence, verdict, observed before, expected now). In the expected list a set means "either".
TABLE = {
    "S1": ("Going long crude here around 71.20. Stop 69.50, target 75.", "R", [CM, STOP, TGT], None),
    "S2": ("Selling gold short at 2,410 on a limit. Stop 2,440.", "R", [IE, STOP], None),
    "S4": ("Bought the S&P at 5,012 a few minutes ago. Market now 5,020.", "R", [HF, CM], None),
    "S5": ("Added to our crude long at 70.10. Average is now 70.65. Stop stays 69.50.", "R", [HF, AVG, STOP], None),
    "S6": ("Lowering the stop on our gold short from 2,440 to 2,425.", "R", [STOP, STOP], None),
    "S8": ("Took half of the S&P long off at 5,080. Stop on the rest to 5,040.", "R", [HF, STOP], None),
    "S9": ("Closing the gold short here, market 2,395.", "R", [CM], None),
    "S10": ("We were stopped out of natural gas overnight at 2.84.", "R", [HF], None),
    "S11": ("Recap: last week we closed crude at 74.90 for a gain of 5.2%.", "R", [HF], None),
    "S12": ("If silver closes above 31.50 we will go long with a stop at 30.20.", "R", [CHART, STOP], None),
    "S13": ("Will buy silver at 30.80 if it pulls back.", "R", [IE], None),
    "S15": ("Long December natural gas at 3.42 on a limit; January is trading at 3.61.", "R", [IE, OCM], None),
    "S16": ("Long natural gas at 3.42 on a limit; the market is trading at 3.61.", "R", [IE, CM], None),
    "S17": ("Bought gold at 2,395 after it held support near 2,380 with RSI at 31. Stop 2,360.", "R",
            [HF, CHART, NAP, STOP], None),
    "S18": ("Crude bounced from 70 last week. Our stop goes to 70.", "R", [CHART, STOP], None),
    "S26": ("Going long crude at 71.20.", "R", [UNR], None),
    "S27": ("Going long crude at 71.20, the order is working.", "R", [IE], None),
    "S28": ("Going long crude at 71.20, filled on the open.", "R", [HF], None),
    "S29": ("Our crude long was stopped at 69.50 overnight. Buying gold now. If silver closes above 31.50 we will "
            "add it. Rates look heavy.", "R", [HF, CHART], None),
    "S30": ("Closed the crude long at 74.90 and reversed: we are now short crude from 74.85, stop 76.", "R",
            [HF, HF, STOP], None),
    "A01": ("Gold is trading at 2,400 and we are long from 2,350.", "W", [HF, HF], [CM, HF]),
    "A02": ("We bought gold this morning at 2,395.", "R", [HF], None),
    "A03": ("Buying gold at 2,395 earlier today worked out well.", "C", [UNR], [HF]),
    "A04": ("Crude was at 71.20 when we went long.", "W", [HF], [UNR]),
    "A05": ("Crude is now 71.20, below our entry of 72.", "C", [CM, UNR], [CM, HF]),
    "A06": ("Our limit at 71.20 was filled.", "R", [HF], None),
    "A07": ("Our limit at 71.20 was cancelled.", "D", [IE], [UNR]),
    "A08": ("We had a bid at 71.20 yesterday but it never filled.", "W", [HF], [UNR]),
    "A09": ("Don't buy crude at 71.20; wait for 69.", "R", [UNR, UNR], None),
    "A10": ("Nobody should be bidding gold at 2,400.", "D", [IE], [UNR]),
    "A11": ("Gold trades at 2,400 tomorrow if CPI is soft.", "D", [CM], [UNR]),
    "A12": ("Target hit at 75 this morning, we are flat.", "W", [CHART], [HF]),
    "A13": ("Took profits at 75.", "R", [HF], None),
    "A14": ("The 69.50 stop was hit, filled 69.40.", "R", [STOP, HF], None),
    "A15": ("Stopped out at 2.84.", "R", [HF], None),
    "A16": ("Our stop at 69.50 was hit overnight.", "R", [STOP], None),
    "A17": ("Sell limit at 75 to take profit.", "R", [IE], None),
    "A18": ("Market is 2,395 but we will wait.", "R", [CM], None),
    "A19": ("We will look at gold again near 2,380.", "D", [IE], [UNR]),
    "A20": ("Gold will probably test 2,380 before rallying.", "D", [IE], [UNR]),
    "A21": ("Analysts expect crude to reach 80 by year end.", "R", [UNR], None),
    "A22": ("I would not chase crude at 74.", "D", [IE], [UNR]),
    "A23": ("Gold could hit 2,500 if the dollar weakens.", "R", [CHART], None),
    "A24": ("We are waiting to buy gold at 2,380.", "R", [IE], None),
    "A25": ("Order to buy crude at 70 is working.", "R", [IE], None),
    "A26": ("Bids at 68.", "R", [IE], None),
    "A27": ("Crude at 71.20 now.", "R", [CM], None),
    "A28": ("Crude was at 71.20, now it is 72.", "C", [UNR, UNR], [UNR, CM]),
    "A30": ("Long the December contract at 70 on a limit.", "R", [IE], None),
    "A31": ("Long crude at 70 on a limit, rolling in December.", "R", [IE], None),
    "A32": ("Long crude Dec at 70 on a limit.", "R", [IE], None),
    "A34": ("Buy gold at 2,400 next month.", "R", [UNR], None),
    "A35": ("In the second month of the trade we bought crude at 70.", "W", [HF], [HF]),
    "A36": ("Long crude at about 71.", "R", [UNR], None),
    "A37": ("Long crude at 71 on a limit, the market is roughly flat.", "R", [IE], None),
    "A38": ("Long crude at 71.20 on a limit around noon.", "R", [IE], None),
    "A39": ("Target 75 or so.", "R", [TGT], None),
    "A40": ("Crude is sitting near the 71 handle.", "R", [CM], None),
    "A41": ("Buy 5 contracts of crude at 70.", "C", [NAP, UNR], [NAP, IE]),
    "A42": ("Buy crude at 70.", "C", [UNR], [IE]),
    "A43": ("Sell gold at 2,410 limit.", "R", [IE], None),
    "A44": ("Gold at 2,410 is a good place to sell.", "R", [UNR], None),
    "A45": ("Short gold at 2,410, it's trading at 2,405 now.", "R", [UNR, CM], None),
    "A46": ("We are short gold here at 2,410 on a limit.", "R", [UNR], None),
    "A47": ("With the stop at 2,440 and gold at 2,410 we stay short.", "C", [STOP, UNR], [STOP, CM]),
    "A48": ("Crude settled at 70.50 yesterday.", "R", [CHART], None),
    "A50": ("Gold is up 25 today at 2,410.", "C", [UNR, UNR], [NAP, CM]),
    "A52": ("Down 2% at 69.80 now.", "R", [CM], None),
    "A54": ("The order at 71.20 is gone, we are now flat.", "D", [IE], [UNR]),
    "A55": ("Pulled our bid at 2,380.", "D", [IE], [UNR]),
    "A56": ("Cancel the buy limit at 70.", "D", [IE], [UNR]),
    "A57": ("If you missed it, crude was 71.20 at the time of the alert.", "R", [UNR], None),
    "A59": ("Hypothetically, buying crude at 60 would have been great.", "R", [UNR], None),
    "A60": ("We filled half at 70 and have a bid for the rest at 69.", "R", [HF, IE], None),
    "A62": ("Gold sits at 2,400.", "R", [CM], None),
    "A63": ("Selling gold at 2,410 was a mistake.", "R", [UNR], None),
    "A65": ("Stop at 2,440, which is just above resistance at 2,435.", "R", [STOP, CHART], None),
    "A66": ("Raise the stop to breakeven at 70.10.", "W", [AVG], [STOP]),
    "A67": ("Our average entry is 70.10 and the stop is 69.", "R", [AVG, STOP], None),
    "A68": ("Gold at 2,410 on a limit order earlier, we cancelled it.", "D", [IE], [UNR]),
    "A69": ("Buy the dip at 68.", "R", [IE], None),
    "A70": ("Price was 71.20 at entry.", "R", [UNR], None),
    "A71": ("Crude at 71.20 would be a gift for buyers.", "R", [UNR], None),
    "A72": ("Crude is trading at 71.20, we bought at 70.90 a minute ago.", "W", [HF, HF], [CM, HF]),
    "A73": ("Gold buyers will step in at 2,380.", "D", [IE], [UNR]),
    "A74": ("We sold the rally at 2,450 last week and will buy back at 2,380.", "R", [HF, IE], None),
    "A75": ("Crude long from 70 is working nicely.", "D", [IE], [UNR]),
    "A76": ("At 10:30 we bought crude at 70.10.", "R", [HF], None),
    "A77": ("Gold short, entry 2,410, target 2,300.", "R", [UNR, TGT], None),
    "A78": ("Our bid is at 2,380, gold is trading at 2,395.", "R", [IE, CM], None),
    "A79": ("Gold should find buyers at 2,380.", "R", [UNR], None),
    "A80": ("Would you buy gold at 2,380?", "D", [IE], [UNR]),
    "B01": ("Gold is about to break 2,400.", "R", [UNR], None),
    "B02": ("Long crude between 70 and 72 on a limit.", "D", [UNR, IE], [UNR, UNR]),
    "B03": ("Stop 69.50 area, target 75 zone.", "R", [STOP, TGT], None),
    "B05": ("Long crude at 70 on a limit; the December high was 75.", "W", [IE, CHART], [IE, CHART]),
    "B06": ("December was a good month: we bought crude at 70.", "W", [HF], [HF]),
    "B07": ("Buy March crude at 70 on a limit.", "C", [IE], [IE]),
    "B08": ("Long CLZ6 at 70 on a limit.", "R", [IE], None),
    "B09": ("Long the Z6 contract at 70 on a limit.", "R", [IE], None),
    "B10": ("Front month crude trades at 71.", "R", [CM], None),
    "B11": ("We will buy in March at 70 on a limit.", "R", [IE], None),
    "B12": ("Since December crude has rallied from 60 to 75.", "R", [CHART, CHART], None),
    "B13": ("Long Dec 26 crude at 70 on a limit.", "W", [NAP, IE], [NAP, IE]),
    "B14": ("On Dec 12 we bought crude at 70.", "W", [NAP, HF], [NAP, HF]),
    "B15": ("Long crude at 70 on a limit, next month contract at 71.", "W", [IE, UNR], [IE, {OCM, UNR}]),
    "B16": ("Bought crude at roughly 70.", "R", [HF], None),
    "B17": ("Bought crude at 70, roughly flat on the day.", "R", [HF], None),
    "B19": ("Crude near 71.20 now.", "R", [CM], None),
    "B20": ("Long gold at 2,400 on a limit, about 1 hour ago.", "R", [IE, NAP], None),
    "B21": ("Our crude limit at 70 is resting; crude trades near 71.", "R", [IE, CM], None),
    "B22": ("We are no longer bidding gold at 2,380.", "D", [IE], [UNR]),
    "B23": ("Our crude long from 70 is still working.", "R", [HF], None),
    "B24": ("Long crude from 70; order working for the add at 69.", "R", [UNR, IE], None),
    "B25": ("Not buying crude at 70 here.", "R", [UNR], None),
    "B26": ("Never chase gold at 2,450.", "R", [UNR], None),
    "B27": ("You could bid gold at 2,380 if you like.", "R", [IE], None),
    "B28": ("What if crude drops to 65?", "R", [CHART], None),
    "B29": ("Expect sellers at 2,450.", "R", [UNR], None),
    "B30": ("Crude will likely retest 70.", "D", [IE], [UNR]),
    "C01": ("The market expects crude at 80 by year end.", "D", [CM], [UNR]),
    "C02": ("Spot gold forecast 2,600 next year.", "D", [CM], [UNR]),
    "C03": ("High conviction long crude at 71.20 on a limit.", "W", [CHART], [IE]),
    "C04": ("Bought protective puts at 2,300.", "W", [STOP], [UNR]),
    "C05": ("Gold now looks like it can reach 2,600.", "R", [UNR], None),
    "C06": ("Gold currently 2,410 but it could trade 2,600 later.", "R", [CM, UNR], None),
    "C07": ("Crude is trading at 71.20.", "R", [CM], None),
    "C08": ("Crude was trading at 71.20 when the report came out.", "R", [UNR], None),
    "C09": ("If crude is trading at 71.20 tomorrow we will buy.", "D", [CM], [UNR]),
    "C10": ("Crude trading at 71.20 would be a gift.", "D", [CM], [UNR]),
    "C11": ("We bought crude at 71.20.", "R", [HF], None),
    "C12": ("We will buy crude at 71.20.", "R", [IE], None),
    "C13": ("We would have bought crude at 71.20.", "W", [HF], [UNR]),
    "C14": ("We bought crude at 71.20 on a limit.", "R", [HF], None),
    "C15": ("We are bidding crude at 71.20.", "R", [IE], None),
    "C16": ("Others are bidding crude at 71.20.", "D", [IE], [UNR]),
    "C17": ("Crude now 71.20, our bid is 70.50.", "R", [CM, IE], None),
    "C18": ("Here at 70 I would not buy.", "R", [CM], None),
    "C19": ("Gold traded at 2,410 at the open, now 2,395.", "R", [CHART, CM], None),
    "C20": ("Last week gold was 2,300, now it is 2,410.", "C", [UNR, UNR], [UNR, CM]),
    "C21": ("At the moment gold is 2,410.", "C", [UNR], [CM]),
    "C22": ("Gold 2,410 at the time of writing.", "C", [UNR], [CM]),
    "C23": ("The market is at 2,410 in gold right now.", "R", [CM], None),
    "C24": ("Market now 2,410, entry was 2,380.", "R", [CM, UNR], None),
    "C25": ("Our entry yesterday was 2,380.", "R", [UNR], None),
    "C26": ("Bought gold at 2,380 yesterday, market now 2,410.", "R", [HF, CM], None),
    "C27": ("Sold half at 2,410 and will sell the rest at 2,450.", "R", [HF, IE], None),
    "C28": ("Gold limit at 2,380 was hit this morning.", "D", [IE], [HF]),
    "C29": ("We are long gold, filled at 2,380 on the limit.", "R", [HF], None),
    "C30": ("The limit at 2,380 has not filled yet.", "W", [HF], [UNR]),
    "D01": ("We were stopped at our stop of 69.50.", "W", [STOP], [HF]),
    "D02": ("Hit our target of 75 and exited.", "R", [TGT], None),
    "D03": ("Exited at our 75 target.", "W", [TGT], [HF]),
    "D04": ("Target 75 reached, we sold at 75.10.", "R", [TGT, HF], None),
    "D05": ("Took profit at the 75 target.", "W", [TGT], [HF]),
    "D06": ("Stop hit at 69.50.", "W", [CHART], [HF]),
    "E02": ("Long crude about here at 71.20.", "R", [CM], None),
    "E03": ("We sold crude around noon at 74.90.", "R", [HF], None),
    "E04": ("Gold, near the highs, trades at 2,410.", "R", [CM], None),
    "F01": ("We bought crude at 70 a minute ago. Gold looks heavy, bids at 2,380 would be my plan.", "D",
            [HF, IE], [HF, IE]),
    "F02": ("Long crude here at 71.20. Silver stop 30.20 for those still holding.", "R", [CM, STOP], None),
}

# W and C rows a finding of the review fixes in this round: asserted exactly.
FIXED_BY_FINDING = {"A01": "S2", "A04": "S2", "A08": "S2", "A72": "S2", "C13": "S2", "C30": "S2",
                    "A12": "S1", "D01": "S1", "D03": "S1", "D05": "S1", "D06": "S1",
                    "A35": "S3", "B05": "S3", "B06": "S3", "B13": "S3", "B14": "S3", "B15": "S3",
                    "A66": "N4", "C03": "N4", "C04": "N4"}

# Extra properties the review observed or expects (exactness, contract month, the action tie).
EXACTNESS = {"S1": [X.APPROXIMATE, X.EXACT, X.EXACT], "S17": [X.EXACT, X.APPROXIMATE, X.NOT_APPLICABLE, X.EXACT],
             "A36": [X.APPROXIMATE], "A37": [X.EXACT], "A38": [X.EXACT], "A39": [X.APPROXIMATE], "A40": [X.APPROXIMATE],
             "B01": [X.EXACT], "B02": [X.APPROXIMATE, X.APPROXIMATE], "B03": [X.APPROXIMATE, X.APPROXIMATE],
             "B16": [X.APPROXIMATE], "B17": [X.EXACT], "B19": [X.APPROXIMATE], "E02": [X.EXACT], "E03": [X.EXACT],
             "E04": [X.EXACT]}
NS = (M.NOT_STATED, None)
MONTHS = {"S15": [(M.NAMED, "DEC"), (M.NAMED, "JAN")], "S16": [NS, NS], "A30": [(M.NAMED, "DEC")], "A31": [NS],
          "A32": [(M.NAMED, "DEC")], "A34": [NS], "A35": [NS], "B05": [NS, NS], "B06": [NS],
          "B08": [(M.NAMED, "Z6")], "B09": [(M.NAMED, "Z6")], "B10": [(M.FRONT, None)], "B11": [NS],
          "B13": [(M.NOT_APPLICABLE, None), NS], "B14": [(M.NOT_APPLICABLE, None), NS]}


def _post(text):
    return SourcePost(source_id="r", post_id="r", author_id="A", verified_author=True, text=text, text_complete=True,
                      is_repost=False, published_at=T0, received_at=T0, provenance=Provenance.SYNTHETIC)


def _read(text):
    return TklInterpreter().interpret(_post(text)).price_readings


def _ok(got, want):
    return got in want if isinstance(want, set) else got == want


def test_the_table_is_the_reviews():
    verdicts = [v for _t, v, _o, _e in TABLE.values()]
    assert len(TABLE) == 161
    assert (verdicts.count("R"), verdicts.count("D"), verdicts.count("W"), verdicts.count("C")) == (107, 23, 20, 11)
    assert all((e is None) == (v == "R") for _t, v, _o, e in TABLE.values())


@pytest.mark.parametrize("key", sorted(k for k, row in TABLE.items() if row[1] == "R"))
def test_right_rows_stay_right(key):
    text, _v, observed, _e = TABLE[key]
    assert [r.role for r in _read(text)] == observed


@pytest.mark.parametrize("key", sorted(k for k, row in TABLE.items() if row[1] == "D"))
def test_dangerous_rows_are_safe(key):
    text, _v, observed, expected = TABLE[key]
    got = [r.role for r in _read(text)]
    assert len(got) == len(expected) and all(_ok(g, e) for g, e in zip(got, expected))
    for g, e in zip(got, expected):
        if g in EXEC:
            assert _ok(g, e), "an execution-relevant role the review does not expect"


@pytest.mark.parametrize("key", sorted(k for k, row in TABLE.items() if row[1] in ("W", "C")))
def test_wrong_and_cautious_rows_never_get_worse(key):
    text, _v, observed, expected = TABLE[key]
    got = [r.role for r in _read(text)]
    assert len(got) == len(expected)
    if key in FIXED_BY_FINDING:
        assert all(_ok(g, e) for g, e in zip(got, expected))
    else:
        assert all(_ok(g, e) or g == o for g, o, e in zip(got, observed, expected))
    for g, o, e in zip(got, observed, expected):
        if g in EXEC:
            assert _ok(g, e) or g == o


@pytest.mark.parametrize("key", sorted(EXACTNESS))
def test_exactness(key):
    assert [r.exactness for r in _read(TABLE[key][0])] == EXACTNESS[key]


@pytest.mark.parametrize("key", sorted(MONTHS))
def test_contract_months(key):
    assert [(r.contract_month, r.month_named) for r in _read(TABLE[key][0])] == MONTHS[key]


def test_f01_gold_plan_is_not_tied_to_the_crude_action():
    got = _read(TABLE["F01"][0])
    assert got[0].action_id == "a1"
    assert got[1].action_id != "a1" and got[1].association != "SOLE_ACTION_IN_POST"
