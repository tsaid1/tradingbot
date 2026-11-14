"""The independent reviews' invented sentences (orchestration/reviews/PRICE_ROLES_review.md) as regression
sets for the structured price readings: the first review's 161 (TABLE) and the confirmation review's 118
(CONF). Invented text only.

Since the second correction round INTENDED_ENTRY, CURRENT_MARKET and HISTORICAL_FILL are granted by an
allow-list (price_roles._allow) and withheld otherwise; UNRESOLVED is the safe outcome. Each row: id,
sentence, the review's verdict on the readings it observed, the roles it observed, and its expectation.
  * D (dangerous: a wrong INTENDED_ENTRY, CURRENT_MARKET or HISTORICAL_FILL, or an entry-type price tied to
    the wrong action), 58 rows: every number is the expected role, or UNRESOLVED where one of the three
    roles was expected; no number has one of the three roles unless the expectation says so; the numbers
    of UNTIE are tied to no action (or, for P20, not to the gold action of their clause).
  * R (right): every number stays as observed, or becomes UNRESOLVED where it was one of the three roles
    (the recall cost, listed exactly in RECALL_COST); never a different role.
  * W (wrong in a safe direction) and C (too cautious): every number is the role observed, the role the
    review expects, or UNRESOLVED where either was one of the three roles; the rows a finding fixes are
    asserted as expected (UNRESOLVED accepted in place of one of the three roles)."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from tradingbot.price_roles import ContractMonthEvidence as M, Exactness as X, PriceReadingRole as R
from tradingbot.source_events import Provenance, SourcePost
from tradingbot.tkl_interpreter import TklInterpreter

CM, IE, HF, AVG = R.CURRENT_MARKET, R.INTENDED_ENTRY, R.HISTORICAL_FILL, R.AVERAGE_OR_COST_BASIS
STOP, TGT, CHART, OCM, UNR, NAP = R.STOP, R.TARGET, R.CHART_LEVEL, R.OTHER_CONTRACT_MONTH, R.UNRESOLVED, R.NOT_A_PRICE
EXEC = (IE, CM)
GRANTED = (IE, CM, HF)
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

# The confirmation review's 118 sentences (verdicts R 55, D 35, W 14, C 14). An expectation of None:
# the review found the observed roles right.
CONF = {
    "N01": ("We are not, as of this morning, bidding gold at 2,380.", "D", [IE], [UNR]),
    "N02": ("Not something we do often, but we are bidding crude at 70.", "R", [IE], None),
    "N03": ("I don't think we will be buying crude, at least not at 71.20.", "R", [UNR], None),
    "N04": ("There is no way we are bidding gold here, at 2,380.", "R", [UNR], None),
    "N05": ("We never said to buy gold, certainly not with a limit at 2,380.", "R", [UNR], None),
    "N06": ("It is not true that we are not bidding gold at 2,380.", "C", [UNR], [IE]),
    "N07": ("We would not say no to buying crude at 70 on a limit.", "R", [UNR], None),
    "N08": ("Not cancelling: our bid at 2,380 stays.", "C", [UNR], [IE]),
    "N09": ("Cancelled the 70 bid. New bid at 69 on a limit.", "R", [UNR, IE], None),
    "N10": ("Our old limit at 70 is cancelled; new limit at 69.", "R", [UNR, IE], None),
    "N11": ("We cancelled the gold order and placed a new limit at 2,375.", "C", [UNR], [IE]),
    "N12": ("Scrap that, buy crude at 70 on a limit instead.", "C", [UNR], [IE]),
    "N13": ('He said "buy gold at 2,380 on a limit" but we disagree.', "D", [IE], [UNR]),
    "N14": ('A subscriber asked: "should I bid crude at 70?"', "R", [UNR], None),
    "N15": ("My friend is bidding gold at 2,380.", "D", [IE], [UNR]),
    "N16": ("Goldman is bidding gold at 2,380 according to the desk.", "D", [IE], [UNR]),
    "N17": ("Clients are bidding crude at 70 on a limit.", "D", [IE], [UNR]),
    "N18": ("He bids at 2,380; we bid at 2,370.", "D", [IE, IE], [UNR, IE]),
    "N19": ("The desk next to us is buying crude at 70 on a limit, we stay out.", "D", [IE], [UNR]),
    "N20": ("Should we buy crude at 70? Yes, bid at 70 on a limit.", "R", [UNR, IE], None),
    "N21": ("Buy crude at 70 on a limit? No.", "R", [UNR], None),
    "N22": ("Bid gold at 2,380? Undecided for now.", "R", [UNR], None),
    "N23": ("Should crude trade at 71.20, we buy.", "R", [UNR], None),
    "N24": ("Were gold at 2,400, we would sell.", "R", [UNR], None),
    "N25": ("Assuming crude is trading at 71.20 at the open, we buy.", "D", [CM], [UNR]),
    "N26": ("Crude trading at 71.20 and we buy, otherwise we wait.", "D", [CM], [UNR]),
    "N27": ("In case gold trades at 2,400 we sell on a limit.", "R", [UNR], None),
    "N28": ("Gold could be trading at 2,500 by Christmas.", "D", [CM], [UNR]),
    "N29": ("Gold may be sitting at 2,500 in a month.", "D", [CM], [UNR]),
    "N30": ("Crude is trading at 80 in my dreams.", "D", [CM], [UNR]),
    "N31": ("I think gold trades at 2,500 by the end of the year.", "D", [CM], [UNR]),
    "N32": ("Gold seems headed to 2,500, we will see.", "R", [UNR], None),
    "N33": ("I reckon crude sits at 75 by spring.", "D", [CM], [UNR]),
    "N34": ("Gold might well be quoted at 2,500 in March.", "D", [CM], [UNR]),
    "N35": ("We hold crude from 70 and are happy.", "W", [UNR], [HF]),
    "N36": ("We own gold at 2,380 and keep it.", "W", [UNR], [HF]),
    "N37": ("We remain long crude at 70 with the stop at 68.", "W", [UNR, STOP], [HF, STOP]),
    "N38": ("We are holding the gold long at 2,380.", "W", [UNR], [HF]),
    "N39": ("Our gold long at 2,380 is open and working.", "W", [UNR], [HF]),
    "N40": ("Still bidding? No, the bid at 2,380 got filled yesterday.", "R", [HF], None),
    "N41": ("Crude is trading at 71.20 and gold at 2,400.", "C", [CM, UNR], [CM, CM]),
    "N42": ("We bid gold at 2,380 while crude trades at 71.", "R", [IE, CM], None),
    "N43": ("Long crude at 70 on a limit, with Brent at 74.", "R", [IE, CM], None),
    "N44": ("Long crude at 70 on a limit; Brent bids at 74.", "R", [IE, IE], None),
    "N45": ("Long crude at 70 on a limit. WTI-Brent spread bid at 4.", "D", [IE, IE], [IE, UNR]),
    "N46": ("Buy crude at 70 on a limit. The yellow metal bids at 2,380.", "R", [IE, IE], None),
    "N47": ("Buy crude at 70 on a limit. Copper bids at 4.10.", "R", [IE, IE], None),
    "N48": ("Buy gold at 2,380 on a limit; silver is trading at 31.", "R", [IE, CM], None),
    "N49": ("Bids at 2,380 to 2,390 on gold.", "W", [IE, IE], [IE, IE]),
    "N50": ("Bids between 2,380 and 2,390.", "R", [IE, IE], None),
    "N51": ("Buy limits at 70, 69 and 68.", "R", [IE, IE, IE], None),
    "N52": ("Levels to watch: 2,380, 2,400, 2,420. Bids at the first.", "R", [CHART, CHART, CHART], None),
    "N53": ("Support at 2,380 and 2,350, bid at 2,360.", "R", [CHART, CHART, IE], None),
    "N54": ("Bid 70 to 72 is the zone we like.", "W", [IE, IE], [IE, IE]),
    "N55": ("Crude at 71.20, not 72, is where we would bid.", "C", [UNR, UNR], None),
    "N56": ("No bid at 70 until crude closes above 72.", "R", [UNR, CHART], None),
    "N57": ("Bids at 70, unless crude breaks 69 first.", "R", [IE, CHART], None),
    "N58": ("Bid at 70 on a limit, but only if the dollar weakens.", "R", [IE], None),
    "N59": ("The market isn't at 2,400 yet, it is at 2,390.", "C", [UNR, UNR], [UNR, CM]),
    "N60": ("Gold is not trading at 2,400, it is trading at 2,390.", "R", [UNR, CM], None),
    "N61": ("Crude never traded at 71.20 today, it sits at 70.50.", "R", [CHART, CM], None),
    "N62": ("Spot is no longer 2,400.", "R", [UNR], None),
    "N63": ("We are buying gold at 2,380 on a limit, not at 2,400.", "R", [IE, UNR], None),
    "N64": ("Forget the 2,380 bid; bid at 2,370 instead.", "D", [IE, IE], [UNR, IE]),
    "N65": ("We withdrew the 2,380 order this morning and we bid 2,370 now.", "C", [UNR, UNR], [UNR, IE]),
    "N66": ("They told us to buy crude at 70, so we did.", "W", [UNR], [HF]),
    "N67": ("The market is quoting 71.20 if you can believe it.", "C", [UNR], [CM]),
    "N68": ("Bid at 70 on a limit, if nothing changes.", "C", [UNR], [IE]),
    "N69": ("We bid crude at 70 on a limit.", "R", [IE], None),
    "N70": ("Crude now trades at 71.20.", "R", [CM], None),
    "N71": ("Gold is trading at 2,400 right now.", "R", [CM], None),
    "N72": ("Buy crude at 70 on a limit, stop 68, target 75.", "R", [IE, STOP, TGT], None),
    "N73": ("Gold sits at 2,400 while we wait to buy at 2,380.", "C", [CM, UNR], [CM, IE]),
    "N74": ("Our order to buy gold at 2,380 is still working.", "R", [IE], None),
    "N75": ("Bought crude at 70 this morning. Brent bids at 74 look strong.", "D", [HF, IE], [HF, UNR]),
    "N76": ("We bought crude at 70. The bid at 2,380 on the yellow metal is ours too.", "D", [HF, IE], [HF, IE]),
    "N77": ("Long gold at 2,380 on a limit. Adding on any dip to 2,350 in silver.", "R", [IE, IE], None),
    "N78": ("We sold crude at 74 yesterday. Bids at 72 on the next one.", "R", [HF, IE], None),
    "N79": ("Never not bidding gold at 2,380, that's our rule.", "R", [UNR], None),
    "N80": ("We will not be bidding gold at 2,380, they will.", "R", [UNR], None),
    "P01": ("We are not going to, at these levels, bid gold at 2,380.", "D", [IE], [UNR]),
    "P02": ("The 2,380 bid is off.", "D", [IE], [UNR]),
    "P03": ("Ignore the bid at 2,380.", "D", [IE], [UNR]),
    "P04": ("Drop the 2,380 bid, it is void.", "D", [IE], [UNR]),
    "P05": ("The bid at 2,380 is no longer valid.", "R", [UNR], None),
    "P06": ("We were going to bid gold at 2,380 but changed our mind.", "D", [IE], [UNR]),
    "P07": ("She is buying crude at 70 on a limit.", "D", [IE], [UNR]),
    "P08": ("It bids at 2,380 according to the screen.", "D", [IE], [UNR]),
    "P09": ("Somebody on the forum is bidding gold at 2,380.", "R", [UNR], None),
    "P10": ("The fund I follow bids gold at 2,380.", "D", [IE], [UNR]),
    "P11": ("JPM sees gold at 2,600.", "R", [UNR], None),
    "P12": ("We are buying gold now. A reader suggested a limit at 2,350.", "D", [IE], [UNR]),
    "P13": ("We are buying gold now. My friend bids at 2,350.", "D", [IE], [UNR]),
    "P14": ("Long Dec gold at 2,380 on a limit. Bid Feb gold at 2,400.", "R", [IE, IE], None),
    "P15": ("We are buying gold at 2,380 on a limit. A friend is bidding at 2,360.", "D", [IE, IE], [IE, UNR]),
    "P16": ("We are buying gold now. Bids at 2,350 would have been better.", "R", [UNR], None),
    "P17": ("We are buying gold now. The old order at 2,350 is dead.", "D", [IE], [UNR]),
    "P18": ("We are buying gold now. Silver: bids at 30.", "R", [IE], None),
    "P19": ("We are buying gold now. The white metal: bids at 30.", "D", [IE], [IE]),
    "P20": ("We are buying gold now, Ag bids at 30.", "D", [IE], [IE]),
    "P21": ("We are buying gold at 2,380 on a limit. Watching platinum too, bid 950 there.", "D", [IE, IE], [IE, IE]),
    "P22": ("Gold could be trading at 2,500 soon.", "R", [UNR], None),
    "P23": ("Gold will be trading at 2,500.", "R", [UNR], None),
    "P24": ("Gold is going to be trading at 2,500.", "R", [UNR], None),
    "P25": ("Gold, if anything, is trading at 2,400.", "C", [UNR], [CM]),
    "P26": ("Gold trading at 2,400 is the base case for next week.", "R", [UNR], None),
    "P27": ("Bids at 2,380 are in, though nobody expects a fill.", "C", [UNR], [IE]),
    "P28": ("Bids at 2,380 are in.", "R", [IE], None),
    "P29": ("Gold at 2,410 now; our bid is not at 2,400 but at 2,390.", "C", [CM, UNR, UNR], [CM, UNR, IE]),
    "P30": ("We hold gold; the market sits at 2,410.", "R", [CM], None),
    "H01": ("He bought crude at 70.", "W", [HF], [UNR]),
    "H02": ("My friend bought gold at 2,380 this morning.", "W", [HF], [UNR]),
    "H03": ("We almost bought crude at 70.", "W", [HF], [UNR]),
    "H04": ("We nearly sold gold at 2,450.", "W", [HF], [UNR]),
    "H05": ("We bought crude at 70 in the backtest.", "W", [HF], [UNR]),
    "H06": ("Clients bought gold at 2,380.", "W", [HF], [UNR]),
    "H07": ("We are buying gold now. My friend bought at 2,350.", "D", [HF], [UNR]),
    "H08": ("We are buying gold now. He is out at 2,350.", "R", [UNR], None),
}

# D rows whose danger was (also) a wrong action tie: these numbers must be tied to no action.
UNTIE = {"N76": [1], "P12": [0], "P13": [0], "P15": [1], "P17": [0], "P19": [0], "P20": [0], "P21": [1], "H07": [0]}
# R rows in which a number with one of the three roles is now UNRESOLVED: the recall cost of the allow-list.
RECALL_COST = frozenset((
    "A18", "B27", "C06", "C18", "S13",                                              # first review
    "N02", "N40", "N44", "N46", "N47", "N57", "N58", "N60", "N61", "N63"))          # confirmation review
# W rows of the confirmation review the allow-list fixes (a third party's or a nearly made fill): exact.
FIXED_BY_ALLOWLIST = {"H01", "H02", "H03", "H04", "H06"}

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
# C9: both ends of a range joined by "to" are APPROXIMATE (a move "from X to Y" is not a range)
CONF_EXACTNESS = {"N49": [X.APPROXIMATE, X.APPROXIMATE], "N54": [X.APPROXIMATE, X.APPROXIMATE],
                  "N50": [X.APPROXIMATE, X.APPROXIMATE]}
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


def test_the_tables_are_the_reviews():
    verdicts = [v for _t, v, _o, _e in TABLE.values()]
    assert len(TABLE) == 161
    assert (verdicts.count("R"), verdicts.count("D"), verdicts.count("W"), verdicts.count("C")) == (107, 23, 20, 11)
    assert all((e is None) == (v == "R") for _t, v, _o, e in TABLE.values())
    verdicts = [v for _t, v, _o, _e in CONF.values()]
    assert len(CONF) == 118
    assert (verdicts.count("R"), verdicts.count("D"), verdicts.count("W"), verdicts.count("C")) == (55, 35, 14, 14)
    assert not set(TABLE) & set(CONF)
    assert sum(1 for row in list(TABLE.values()) + list(CONF.values()) if row[1] == "D") == 58


ROWS = {**TABLE, **CONF}


def _right_or_withheld(got, observed):
    """Each number as observed, or UNRESOLVED where it was one of the three granted roles."""
    return len(got) == len(observed) and all(g == o or (o in GRANTED and g == UNR) for g, o in zip(got, observed))


@pytest.mark.parametrize("key", sorted(k for k, row in ROWS.items() if row[1] == "R"))
def test_right_rows_stay_right_or_become_unresolved(key):
    text, _v, observed, _e = ROWS[key]
    got = [r.role for r in _read(text)]
    assert _right_or_withheld(got, observed)
    assert (got != observed) == (key in RECALL_COST), "the recall cost list is exact"


def test_the_recall_cost():
    """How many RIGHT rows lose one of the three roles to UNRESOLVED: 15 of 162 (5 of 107, 10 of 55)."""
    rights = [k for k, row in ROWS.items() if row[1] == "R"]
    lost = [k for k in rights if [r.role for r in _read(ROWS[k][0])] != ROWS[k][2]]
    assert sorted(lost) == sorted(RECALL_COST) and len(rights) == 162


def _ok_or_withheld(g, e):
    return _ok(g, e) or (not isinstance(e, set) and e in GRANTED and g == UNR)


@pytest.mark.parametrize("key", sorted(k for k, row in ROWS.items() if row[1] == "D"))
def test_dangerous_rows_are_safe(key):
    text, _v, _observed, expected = ROWS[key]
    got = _read(text)
    roles = [r.role for r in got]
    assert len(roles) == len(expected) and all(_ok_or_withheld(g, e) for g, e in zip(roles, expected))
    for g, e in zip(roles, expected):
        if g in GRANTED:
            assert _ok(g, e), "a granted role the review does not expect"
    for i in UNTIE.get(key, []):
        if key == "P20":   # the silver bid of a gold clause: never the gold action's
            assert got[i].role not in GRANTED or got[i].action_id is None
        else:
            assert got[i].action_id is None and got[i].association == "NONE"


@pytest.mark.parametrize("key", sorted(k for k, row in ROWS.items() if row[1] in ("W", "C")))
def test_wrong_and_cautious_rows_never_get_worse(key):
    text, _v, observed, expected = ROWS[key]
    got = [r.role for r in _read(text)]
    assert len(got) == len(observed)
    exp = expected or observed
    if key in FIXED_BY_FINDING or key in FIXED_BY_ALLOWLIST:
        assert all(_ok_or_withheld(g, e) for g, e in zip(got, exp))
    for g, o, e in zip(got, observed, exp):
        assert g == o or _ok(g, e) or (g == UNR and (o in GRANTED or (not isinstance(e, set) and e in GRANTED)))
        if g in GRANTED:
            assert g == o or _ok(g, e)


@pytest.mark.parametrize("key", sorted(CONF_EXACTNESS))
def test_range_ends_are_approximate(key):
    assert [r.exactness for r in _read(CONF[key][0])] == CONF_EXACTNESS[key]


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
