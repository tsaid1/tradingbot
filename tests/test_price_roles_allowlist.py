"""The allow-list of INTENDED_ENTRY, CURRENT_MARKET and HISTORICAL_FILL (price_roles._allow), second
correction round: invented sentences written to break it, the tie rule, and the two quiet fault paths.
Invented text only."""
from __future__ import annotations

import random

import pytest
from test_price_roles import post, readings

import tradingbot.price_roles as PR
from tradingbot.price_roles import PriceReadingRole as R
from tradingbot.price_roles import read_price_roles
from tradingbot.tkl_interpreter import TklInterpreter, _norm_offsets, _normalise

CM, IE, HF, UNR, STOP = R.CURRENT_MARKET, R.INTENDED_ENTRY, R.HISTORICAL_FILL, R.UNRESOLVED, R.STOP
GRANTED = (IE, CM, HF)

# Written against the allow-list before it was final (five batches: 56 + 4, 20, 8, 17 and 5 sentences; on its
# first run each batch gave 2, 3, 4, 4 and 2 dangerous readings, all fixed in the rule, not the sentence).
# Each row: the sentence and the numbers that may carry a granted role (index: role); every other number
# must be withheld (UNRESOLVED or a non-granted role), and a listed number may also be withheld.
ADVERSARIAL = [
    ("My broker's bid at 2,380 is resting.", {}),
    ('Our client is long crude from 70.', {}),
    ('We think a bid at 2,380 works.', {}),
    ('Bid gold at 2,380 on a limit, they reckon.', {}),
    ("We'd bid gold at 2,380 on a limit.", {}),
    ("Gold's bid at 2,380 was ours, until we pulled it.", {}),
    ('We had been going to bid gold at 2,380.', {}),
    ('We had planned to buy crude at 70 on a limit.', {}),
    ('We wanted to buy crude at 70 on a limit.', {}),
    ('Our bid at 2,380 got scratched this morning.', {}),
    ('The 2,380 limit is voided.', {}),
    ('The bid at 2,380, as we said, stands.', {}),
    ('Gold at 2,400 per the screen.', {}),
    ('Crude, they tell us, is trading at 71.20.', {}),
    ('The yellow metal is trading at 2,400.', {}),
    ('Gold will trade at 2,500 eventually.', {}),
    ('Gold trades at 2,500 by year-end.', {}),
    ('Crude trades at 75 next week.', {}),
    ('Gold is trading at 2,400 unless the data surprises.', {}),
    ('Provided gold is trading at 2,400, we buy.', {}),
    ('Gold sitting at 2,400 is a dream scenario.', {}),
    ('Our desk bought crude at 70.', {}),
    ('Bought crude at 70 for a client.', {}),
    ('We bought crude at 70 on behalf of readers.', {}),
    ('The fund bought gold at 2,380 this morning.', {}),
    ('We came close to buying crude at 70.', {}),
    ('We just missed buying crude at 70.', {}),
    ('We were about to bid gold at 2,380.', {}),
    ('We almost got filled at 70.', {}),
    ('Bid gold at 2,380 on a limit, he says.', {}),
    ('"Bid gold at 2,380 on a limit."', {}),
    ('Gold bids at 2,380 on the screen.', {}),
    ('Buyers are bidding gold at 2,380 on a limit.', {}),
    ('A big seller offered crude at 74.', {}),
    ('We are buying gold now. Silver bid at 30 on a limit.', {}),
    ('We are buying gold now; the guy next door bids at 2,350.', {}),
    ('We are long gold. Crude limit at 70 is resting.', {0: IE}),
    ('We bought gold at 2,380. Silver stop 28.', {0: HF}),
    ('We are buying gold now, silver bids at 30 on a limit.', {}),
    ('Buy gold now. Platinum limit at 950.', {0: IE}),
    ('Bid gold at 2,380 on a limit — scrap that.', {}),
    ('Bid gold at 2,380 on a limit. Actually, cancel that.', {}),
    ('Gold may trade at 2,400 today.', {}),
    ('Crude is trading at 71.20, or so the screen says.', {}),
    ('Gold, which traded at 2,410 earlier, is quiet.', {}),
    ('If only we had bought crude at 70.', {}),
    ('Would that we bid gold at 2,380.', {}),
    ('Bids at 2,380 are in for my friends.', {}),
    ('Them buying crude at 70 surprised us.', {}),
    ('Bought crude at 70, they say.', {}),
    ('Long crude at 70 on a limit, unless you are already long.', {}),
    ('We are not long crude from 70.', {}),
    ('Clients long crude from 70 are happy.', {}),
    ('Bid at 2,380, nope.', {}),
    ('Gold printing 2,400 tomorrow is likely.', {}),
    ('Crude trades at 71.20 when London opens.', {}),
    ('Bid gold at 2,380 on a limit. Not anymore.', {}),
    ('Bid gold at 2,380 on a limit. Scratch it.', {}),
    ('Bid gold at 2,380 on a limit. We were going to, anyway.', {}),
    ('Bought gold at 2,380. Scrap the stop.', {0: HF}),
    ('We are buying gold now and silver at 30 on a limit.', {0: IE}),
    ('We bought crude at 70 and gold at 2,380.', {0: HF, 1: HF}),
    ('Long gold at 2,380 on a limit, silver too at 30.', {0: IE}),
    ('We sold gold at 2,450, the S&P at 5,000 on a limit.', {0: HF, 1: IE}),
    ('Buy crude at 70 on a limit and he buys gold at 2,380.', {}),
    ('Gold at 2,400 now and crude at 71.20.', {0: CM}),
    ('We bid gold at 2,380 and they bid at 2,370.', {}),
    ('Long crude at 70 on a limit, my friend at 69.', {}),
    ('We bought crude at 70 and readers at 71.', {}),
    ('Bought at 70. Readers bought at 71.', {0: HF}),
    ('The S&P limit at 5,000 is ours; the gold one at 2,380 is theirs.', {0: IE}),
    ('Our bid at 2,380 sits with theirs at 2,370.', {}),
    ('We are buying gold now. Bids at 2,350 too.', {0: IE}),
    ('We are buying gold now. Stop at 2,300.', {}),
    ('Buying gold now, bid at 2,350 for the add.', {0: IE}),
    ('We bought gold now; the market is 2,410.', {0: CM}),
    ('We bought gold at 2,380, someone else at 2,370.', {}),
    ('We are buying gold now. Crude is trading at 71.20.', {0: CM}),
    ('Long crude at 70 on a limit. That was last year.', {}),
    ('Long crude at 70 on a limit. Just kidding.', {}),
    ('Bid gold at 2,380 and we bought crude yesterday.', {}),
    ('Gold at 2,380, then we bought crude.', {}),
    ('Long gold at 2,380, then buy crude on a limit.', {}),
    ('Gold at 2,380 and buy crude on a limit.', {}),
    ('Sold crude, then bought gold at 2,380 on a limit.', {0: HF}),
    ('Gold here at 2,380 and we sell crude.', {0: CM}),
    ('Gold at 2,380 but we filled crude.', {}),
    ('Gold is quiet at 2,380 while we bought crude.', {}),
    ('Per the desk, gold is trading at 2,400.', {}),
    ('Our view: gold trades at 2,500 soon.', {}),
    ('We bought crude at 70 last year, for the record.', {0: HF}),
    ('Gold trading at 2,400 is what the screen shows, per Reuters.', {}),
    ('Bids at 2,380 on gold, as recommended.', {}),
    ('Crude printing 71.20, a level no one expected.', {}),
    ('Pretend we bought crude at 70.', {}),
    ('Say we buy crude at 70 on a limit.', {}),
    ('Picture gold trading at 2,500.', {}),
    ('Our bid at 2,380 has lapsed.', {}),
    ('The order at 2,380 is history.', {}),
    ('Limit at 70 was pulled this morning.', {}),
    ('We bought crude at 70 in paper only.', {0: HF}),
    ('Gold now 2,400, they quote.', {}),
    ('Long crude at 70 on a limit, or not.', {}),
    ('We bought crude at 70? Yes.', {}),
    ('Buy crude at 70 on a limit; just a thought.', {}),
    # fifth batch (first run: 2 of 2 attributed sentences dangerous)
    ("From a reader's email. Bid gold at 2,380 on a limit.", {}),
    ("Bid gold at 2,380 on a limit. That's what my friend does.", {}),
    ("Thank you for reading. Bid gold at 2,380 on a limit.", {0: IE}),
    ("Our analyst wrote this. Bought crude at 70.", {}),
    ("Bid gold at 2,380 on a limit. Clients asked for it.", {}),
]


def test_the_adversarial_set_is_large_enough():
    assert len(ADVERSARIAL) >= 110 and len({s for s, _ in ADVERSARIAL}) == len(ADVERSARIAL)


@pytest.mark.parametrize("text,granted", ADVERSARIAL, ids=[f"adv{i:03d}" for i in range(len(ADVERSARIAL))])
def test_adversarial_sentences_grant_nothing_wrongly(text, granted):
    got = readings(text)
    for i, r in enumerate(got):
        if r.role in GRANTED:
            assert granted.get(i) == r.role, (i, r.role, r.reasons)
            # a granted role is tied only to the action of its own clause, never by fallback
            assert r.association in ("CLAUSE", "NONE")
        elif i in granted:
            assert r.role == UNR


@pytest.mark.parametrize("text,n", [
    ("We are buying gold now and silver at 30 on a limit.", 0),   # a clause naming two markets
    ("We are buying gold now. Bids at 2,350 too.", 0),            # no fallback tie for an order price
    ("We are buying gold now. A reader suggested a limit at 2,350.", 0),
    ("We are buying gold now. My friend bought at 2,350.", 0),
])
def test_granted_or_entry_prices_are_never_tied_by_fallback(text, n):
    r = readings(text)[n]
    assert r.action_id is None and r.association == "NONE"


def test_the_fallback_tie_reads_the_market_the_clause_names():
    # STOP, TARGET and AVERAGE keep the fallback tie, unless their clause names another market itself
    other = readings("Long crude here at 71.20. Silver stop 30.20 for those still holding.")[1]
    assert (other.role, other.action_id, other.association) == (STOP, None, "NONE")
    same = readings("Added to our crude long at 70.10. Average is now 70.65. Stop stays 69.50.")
    assert [(r.action_id, r.association) for r in same[1:]] == [("a1", "SOLE_ACTION_IN_POST")] * 2
    named_same = readings("We are buying gold now. Gold stop at 2,300.")[0]
    assert (named_same.role, named_same.action_id) == (STOP, "a1")


# one asserting example per allow-list code (index of the number that must carry it first)
ALLOW_CODES = {
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
    "THIRD_PARTY": ("My friend bought gold at 2,380 this morning.", 0),
    "CANCELLED_OR_WITHDRAWN": ("Forget the 2,380 bid; bid at 2,370 instead.", 0),
    "NEGATED": ("We are not, as of this morning, bidding gold at 2,380.", 0),
    "HYPOTHETICAL_MARKET_STATE": ("Gold could be trading at 2,500 by Christmas.", 0),
    "FUTURE_MARKET_STATE": ("Market is 2,395 but we will wait.", 0),
    "CONDITIONAL_MARKET_STATE": ("Assuming crude is trading at 71.20 at the open, we buy.", 0),
    "COUNTERFACTUAL": ("Crude is trading at 80 in my dreams.", 0),
}


@pytest.mark.parametrize("code", sorted(ALLOW_CODES))
def test_allow_list_codes(code):
    text, n = ALLOW_CODES[code]
    r = readings(text)[n]
    assert r.role == UNR and r.reasons[0] == code


@pytest.mark.parametrize("text,want", [
    ("Bid gold at 2,380 on a limit, they reckon.", UNR),       # (c) a veto anywhere in the sentence
    ("We bid crude at 70 on a limit.", IE),                    # (b) the publisher
    ("Bids in at 68.", IE),                                    # (b) no explicit subject
    ("Long gold at 2,395 on a limit.", IE),
    ("Clients are bidding crude at 70 on a limit.", UNR),      # (b) another explicit subject
    ("Gold is trading at 2,400.", CM),                         # (d) the market itself, present tense
    ("Gold was trading at 2,400 yesterday.", UNR),
    ("Gold bids at 2,380 on the screen.", UNR),                # a market quote is not the publisher's order
])
def test_the_four_conditions(text, want):
    assert readings(text)[0].role == want


# ------------------------------------------------------------------ C11: the two quiet fault paths

def test_a_fault_while_counting_a_fault_still_leaves_a_counted_code(monkeypatch):
    import tradingbot.tkl_interpreter as TI
    text = "Going long crude here around 71.20. Stop 69.50."
    before = TklInterpreter().interpret(post(text))

    def boom(*_a, **_k):
        raise RuntimeError("invented fault")
    monkeypatch.setattr(TI.TklInterpreter, "_clause_spans", lambda self, t: [])   # a split mismatch ...
    monkeypatch.setattr(TI, "note_fault", boom)                                    # ... whose counting fails
    count = PR.FAULT_COUNTS["CLAUSE_CONTEXT_FAULT"]
    after = TklInterpreter().interpret(post(text))
    assert after.model_dump(exclude={"price_readings"}) == before.model_dump(exclude={"price_readings"})
    assert PR.FAULT_COUNTS["CLAUSE_CONTEXT_FAULT"] == count + 1
    assert after.price_readings and all(r.reasons[-1] == "NO_CLAUSE_CONTEXT" and r.action_id is None
                                        for r in after.price_readings)


def test_a_wrong_offset_map_is_counted_not_silent():
    from tradingbot.price_roles import ClauseUnit
    text = "Bid gold at 2,380 on a limit."
    units = [ClauseUnit(start=0, end=len(text), action_id="a1", trading=True)]
    count = PR.FAULT_COUNTS["CLAUSE_OFFSET_MISMATCH"]
    got = read_price_roles(text, units, lambda p: p - 5000)
    assert PR.FAULT_COUNTS["CLAUSE_OFFSET_MISMATCH"] == count + 1
    assert all(r.reasons[-1] == "NO_CLAUSE_CONTEXT" and r.action_id is None for r in got)
    assert "CLAUSE_OFFSET_MISMATCH" in PR.FAULT_CODES


@pytest.mark.parametrize("raw", ["Gold &amp; crude &gt; 70 \u2014 now &nbsp;72 &amp 73 &notin; 74",
                                 "&#8212; 71 &nbsp;72 \u00a0 73 \u2019 74 \u2013 75", "a&amp;b c&lt;d  e"])
def test_the_entity_offset_map_is_exact_in_any_order(raw):
    want = [len(_normalise(raw[:p])) for p in range(len(raw) + 1)]
    f = _norm_offsets(raw)
    order = list(range(len(raw) + 1))
    random.Random(7).shuffle(order)
    assert {p: f(p) for p in order} == dict(enumerate(want))
