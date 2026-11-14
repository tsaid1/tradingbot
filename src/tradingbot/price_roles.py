"""Structured price readings: for every price-like number of a post, what kind of number it is.

Advisory and additive. The readings follow the adjudication rubric's taxonomy (version 2.1, sections 3 D,
6 and 13.3): CURRENT_MARKET, INTENDED_ENTRY, HISTORICAL_FILL, AVERAGE_OR_COST_BASIS, STOP, TARGET,
CHART_LEVEL, OTHER_CONTRACT_MONTH, UNRESOLVED, and NOT_A_PRICE for indicator values, dates, times,
sizes, periods and profits. NOTHING in the trading path reads them: the gateway, the mapping, the entry
anchor rule and the coordinator keep reading the interpreter's existing fields only.

How a role is decided. Context, never one phrase, and no default:
  * the numbers are those of the product's own number reader (``instruction_terms``: its token, edge
    and number patterns, outside an index's own name), in text order, so ``n`` is the same index as
    the adjudication bundle's ``numbers``;
  * each number is read against the words of its own segment up to the previous price, the words
    right after it (an order or a fill reported after the price: "at 71.20, the order is working"),
    the earlier segments of its sentence (for tense only) and the other numbers of the sentence (a
    chain "from 2,440 to 2,425" shares one role);
  * the cue classes are generic English: role nouns (stop, target, support, average, market), tense
    and voice about the publisher's own execution (bought, added, stopped out, long from), order and
    plan words (limit, order, bids, will, looking to), market-state words attached to the number
    (here at, now, trading at, with X at), market moves (bounced from, rallied to), conditions and
    thresholds (if ... closes above). No publisher's phrasing is in this module;
  * a bare entry verb with a price ("going long crude at 71.20") decides nothing: it is UNRESOLVED
    with ENTRY_KIND_NOT_STATED. Two readings that conflict (an order word AND a market-state word)
    are UNRESOLVED with CONFLICTING_CUES. Only INTENDED_ENTRY is a price instruction; a freshly
    reported fill stays HISTORICAL_FILL;
  * INTENDED_ENTRY and CURRENT_MARKET (the two roles an execution could one day rest on) need their
    positive cue AND no defeater in the number's own clause: a question, a counterfactual, a
    cancelled or withdrawn order, a negation, a third party's view, a forecast, a plan with no
    transaction, a position already held, a market state under a condition, a modal or a future
    time, a chain with an unresolved number. A defeated reading is UNRESOLVED with the defeater's
    code first; a hit or completed order is HISTORICAL_FILL; a negated fill is no fill;
  * an executed price is HISTORICAL_FILL even at the stop or the target ("exited at our 75 target",
    "stop hit at 69.50"); a level named AS the stop or target stays STOP or TARGET (rubric 14.1).
A number is tied to an action only by its own clause, or to the post's only trading action unless its
own clause is another action's that names a different market. Cost: one pass per post over at
most MAX_NUMBERS numbers (the rest UNRESOLVED, NUMBER_CAP_REACHED) with bounded word windows.
Exactness is how the number is written (a hedge word, an approximation mark, an end of a range).
The contract month is assigned only on evidence in the text: a month named as a contract month (not
as a time: "in December", "December was", "the December high" and "Dec 12" are times or dates), a
futures code, "front month", "second month" (not "the second month of the trade"); a month named in
another segment of the sentence counts only when that segment has no price of its own; never from
the post's date, the listed contracts or the price.

A fault anywhere in this path is counted in FAULT_COUNTS and logged with its public code only (never
text); the readings then carry READER_FAULT or NO_CLAUSE_CONTEXT."""
from __future__ import annotations

import bisect
import logging
import re
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Callable, Literal

from pydantic import Field

import tradingbot.runtime.instruction_terms as IT
from tradingbot.models import StrictModel


class PriceReadingRole(str, Enum):
    CURRENT_MARKET = "CURRENT_MARKET"
    INTENDED_ENTRY = "INTENDED_ENTRY"
    HISTORICAL_FILL = "HISTORICAL_FILL"
    AVERAGE_OR_COST_BASIS = "AVERAGE_OR_COST_BASIS"
    STOP = "STOP"
    TARGET = "TARGET"
    CHART_LEVEL = "CHART_LEVEL"
    OTHER_CONTRACT_MONTH = "OTHER_CONTRACT_MONTH"
    UNRESOLVED = "UNRESOLVED"
    NOT_A_PRICE = "NOT_A_PRICE"


class Exactness(str, Enum):
    EXACT = "EXACT"
    APPROXIMATE = "APPROXIMATE"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class ContractMonthEvidence(str, Enum):
    FRONT = "FRONT"
    NEXT = "NEXT"
    NAMED = "NAMED"
    NOT_STATED = "NOT_STATED"
    NOT_APPLICABLE = "NOT_APPLICABLE"


Association = Literal["CLAUSE", "LEVEL_LINE", "SOLE_ACTION_IN_POST", "NONE"]


class PriceReading(StrictModel):
    n: int                                   # index in the product number reader's order
    value: str                               # the number as a plain decimal ("2410")
    role: PriceReadingRole
    exactness: Exactness
    contract_month: ContractMonthEvidence
    month_named: str | None = None           # DEC, or a futures code (Z6), only when NAMED
    action_id: str | None = None             # the action of the post this price belongs to
    association: Association = "NONE"
    reasons: list[str] = Field(default_factory=list)   # public codes: the evidence that decided


# Every public reason code (role evidence first, then month evidence).
REASON_CODES = frozenset((
    # NOT_A_PRICE
    "SIZE_OR_COUNT", "TIME_PERIOD", "TIME_OF_DAY", "DISTANCE_OR_CHANGE", "MA_PERIOD", "INDICATOR_VALUE",
    "DATE", "DATE_OR_CONTRACT_YEAR", "YEAR", "PROFIT_OR_LOSS", "LIST_MARKER",
    # role words
    "STOP_WORD", "TARGET_WORD", "LEVEL_WORD", "AVERAGING_WORD", "MARKET_WORD", "ROLE_WORD_AFTER_NUMBER",
    "STOP_AT_LEVEL", "STOP_RELATIVE_TO_LEVEL", "SAME_ROLE_AS_PREVIOUS_NUMBER",
    # levels
    "CONDITION_LEVEL", "THRESHOLD_LEVEL", "MARKET_MOVE_LEVEL",
    # the publisher's execution, orders, plans, the market now
    "OWN_EXECUTION_PAST", "FILL_WORD", "POSITION_HELD_FROM", "CLAUSE_PAST_EXECUTION", "FILLED_ORDER",
    "ORDER_WORD", "PLANNED_TRANSACTION", "CONDITIONAL_PLAN", "MARKET_STATE_WORD", "WITH_MARKET_AT",
    "MONTH_DIFFERS_FROM_ACTION",
    "ORDER_COMPLETED", "EXECUTED_AT_STOP_OR_TARGET", "STOP_OR_TARGET_EXECUTED",
    # unresolved
    "ENTRY_KIND_NOT_STATED", "EXIT_KIND_NOT_STATED", "CONFLICTING_CUES", "PAST_MARKET_PRICE",
    "ENTRY_OR_TRIGGER_LEVEL", "OPTION_STRIKE", "NO_ROLE_EVIDENCE", "READER_FAULT", "NUMBER_CAP_REACHED",
    # defeaters of INTENDED_ENTRY, CURRENT_MARKET and HISTORICAL_FILL (the defeater's code comes first)
    "QUESTION", "COUNTERFACTUAL", "CANCELLED_OR_WITHDRAWN", "NEGATED", "NOT_FILLED", "THIRD_PARTY",
    "FORECAST", "NO_TRANSACTION_WORD", "POSITION_ALREADY_HELD", "CONDITIONAL_MARKET_STATE",
    "HYPOTHETICAL_MARKET_STATE", "FUTURE_MARKET_STATE", "CHAIN_WITH_UNRESOLVED",
    # the interpreter's clauses were not available (a fault, counted and logged)
    "NO_CLAUSE_CONTEXT",
    # contract month evidence
    "MONTH_NAMED_IN_SEGMENT", "MONTH_NAMED_IN_SENTENCE", "MONTH_OF_ACTION", "FRONT_MONTH_WORD",
    "NEXT_MONTH_WORD",
))

# Public codes of the faults counted in FAULT_COUNTS (and logged, code only).
FAULT_CODES = frozenset(("READER_FAULT", "READER_FAULT_NO_NUMBERS", "CLAUSE_CONTEXT_FAULT", "CLAUSE_SPLIT_MISMATCH"))
FAULT_COUNTS: Counter = Counter()
_log = logging.getLogger(__name__)


def note_fault(code: str) -> None:
    """Count and log a fault of the advisory path by its public code (never post text)."""
    FAULT_COUNTS[code] += 1
    _log.warning("price_readings fault %s", code)


MAX_NUMBERS = 200   # numbers read per post; the rest are UNRESOLVED with NUMBER_CAP_REACHED
WINDOW = 64         # words read on either side of a number

# ------------------------------------------------------------------ generic English cue words

STOP_NOUNS = frozenset(("stop", "stops", "stoploss", "stop-loss", "sl", "protective"))
TARGET_NOUNS = frozenset(("target", "targets", "targeting", "objective", "objectives", "tp", "tp1", "tp2",
                          "tp3", "t1", "t2", "t3", "take-profit"))
LEVEL_NOUNS = frozenset(("support", "supports", "resistance", "resistances", "pivot", "pivots", "trendline",
                         "trend-line", "trendlines", "neckline", "fib", "fibonacci", "retracement", "gap",
                         "high", "highs", "low", "lows", "level", "levels", "channel", "breakout",
                         "breakdown", "vwap", "ma", "sma", "ema", "dma", "wma", "settlement", "floor",
                         "ceiling", "bottom", "top"))
AVERAGE_NOUNS = frozenset(("average", "avg", "basis", "breakeven", "break-even", "b/e", "blended", "cost"))
MA_CONTEXT = frozenset(("moving", "day", "dma", "week", "weekly", "daily", "period", "hour", "hourly"))
PRIOR_WORDS = frozenset(("prior", "previous", "yesterday's", "yesterdays", "last", "weekly", "daily",
                         "monthly", "session's", "today's"))
INDICATOR_NOUNS = frozenset(("rsi", "macd", "stochastic", "stochastics", "stoch", "adx", "cci", "atr", "mfi",
                             "obv", "vix"))
ENTRY_NOUNS = frozenset(("entry", "entries"))
EXIT_NOUNS = frozenset(("exit", "exits"))
MARKET_NOUNS = frozenset(("market", "spot"))
MARKET_STATE_ADVERBS = frozenset(("here", "now", "currently", "presently"))
MARKET_STATE_VERBS = frozenset(("trading", "trades", "sits", "sitting", "quoted", "printing", "priced"))
OWN_PAST = frozenset(("bought", "added", "entered", "exited", "covered", "shorted", "took", "booked",
                      "trimmed", "reduced", "initiated", "cashed", "banked", "locked", "scaled", "reversed",
                      "sold"))
OWN_OR_MARKET_PAST = frozenset(("closed", "opened"))
MARKET_MOVE_PAST = frozenset(("bounced", "rallied", "dropped", "fell", "rose", "climbed", "slid", "sank",
                              "plunged", "surged", "spiked", "tested", "touched", "reached", "hit", "broke",
                              "held", "failed", "rejected", "peaked", "bottomed", "topped", "traded", "tagged",
                              "printed", "settled", "jumped", "tumbled", "retraced", "rebounded", "recovered",
                              "collapsed", "gapped", "dipped", "declined"))
MARKET_MOVE_PRESENT = frozenset(("closes", "breaks", "holds", "reaches", "hits", "dips", "drops", "falls",
                                 "rises", "rallies", "trades", "tests", "touches", "moves", "pulls", "gets",
                                 "settles", "clears", "reclaims", "crosses", "exceeds", "goes"))
PLAN_WORDS = frozenset(("will", "shall", "would", "plan", "plans", "planning", "planned", "intend", "intends",
                        "intending", "aim", "aiming"))
PLAN_BEFORE_TO = frozenset(("looking", "look", "want", "wants", "waiting", "ready", "prepared", "going",
                            "seeking", "hoping"))
DIP_WORDS = frozenset(("pullback", "pullbacks", "dip", "dips", "retest"))
# "working", "resting", "pending" are not order cues by themselves ("long from 70 is working" is a
# position): they only qualify an order noun, which is itself the cue.
ORDER_WORDS = frozenset(("limit", "limits", "lmt", "order", "orders", "bid", "bids", "bidding", "offer",
                         "offers", "offered", "gtc", "stop-limit"))
FILL_WORDS = frozenset(("filled", "fill", "fills", "executed"))
CONDITION_WORDS = frozenset(("if", "once", "unless", "provided", "whenever", "when"))
CROSS_WORDS = frozenset(("above", "below", "under", "over", "beyond", "through", "north", "south"))
ACTION_CONTEXT = frozenset(("going", "buying", "selling", "shorting", "adding", "entering", "initiating",
                            "opening", "long", "short", "longs", "shorts", "buy", "sell", "add", "enter",
                            "go"))
EXIT_CONTEXT = frozenset(("closing", "exiting", "covering", "taking", "trimming", "reducing", "close", "cover",
                          "trim", "reduce", "out"))
PAST_MARKERS = frozenset(("was", "were", "had", "ago", "yesterday", "overnight", "earlier", "last",
                          "previously", "monday", "tuesday", "wednesday", "thursday", "friday"))
HOLDER_WORDS = frozenset(("we", "i", "our", "my", "us", "we're", "i'm", "we've", "i've", "we'd", "ours"))
POSITION_WORDS = frozenset(("long", "short", "longs", "shorts", "position", "positions", "trade", "half",
                            "rest", "remainder", "remaining", "partial", "profit", "profits", "stake"))
APPROX_BEFORE = frozenset(("around", "about", "near", "nearly", "roughly", "approximately", "approx",
                           "circa", "ca", "~"))
APPROX_AFTER = frozenset(("area", "zone", "region", "ish", "handle"))
ATTACHERS = frozenset(("at", "@", "is", "are", "of", "=", "to", "from", "the", "its", "$")) | APPROX_BEFORE
CHAIN_WORDS = frozenset(("to", "and", "&", "then", "or", "/", "-", "->", "from", "at"))
RELATIVE_PREPS = frozenset(("below", "above", "under", "over", "beyond", "behind", "past", "beneath"))
TIME_PREPS = frozenset(("in", "on", "since", "during", "by", "until", "till", "through", "of", "from",
                        "early", "late", "mid", "last", "this", "next", "every", "each", "end"))
SIZE_UNITS = frozenset(("contracts", "contract", "lots", "lot", "shares", "share", "units", "unit", "positions",
                        "trades", "times", "x", "tranche", "tranches", "cars", "micros", "minis"))
PERIOD_UNITS = frozenset(("days", "day", "d", "weeks", "week", "wk", "wks", "months", "month", "mo", "years",
                          "year", "yr", "yrs", "hours", "hour", "hr", "hrs", "h", "minutes", "minute", "min",
                          "mins", "seconds", "sec", "secs", "bars", "bar", "candles", "candle", "period",
                          "sessions", "session"))
TIME_UNITS = frozenset(("am", "pm", "a.m", "p.m", "et", "est", "edt", "ct", "cst", "cdt", "gmt", "utc", "bst",
                        "cet", "o'clock"))
CHANGE_UNITS = frozenset(("percent", "pct", "bps", "bp", "points", "pts", "pt", "ticks", "tick", "cents", "%"))
MA_UNITS = frozenset(("ma", "sma", "ema", "dma", "wma"))
PNL_WORDS = frozenset(("gain", "gains", "profit", "profits", "loss", "losses", "pnl", "p&l"))
PNL_VERBS = frozenset(("made", "lost", "netted", "pocketed"))
STRIKE_WORDS = frozenset(("call", "calls", "put", "puts", "strike"))
YEAR_PREPS = frozenset(("in", "since", "during", "year", "fy", "by"))
# defeaters (generic English)
NEGATIONS = frozenset(("not", "no", "never", "nobody", "none", "neither", "nor", "nothing", "don't", "dont",
                       "doesn't", "didn't", "won't", "wouldn't", "isn't", "aren't", "wasn't", "weren't", "can't",
                       "cannot", "couldn't", "shouldn't", "haven't", "hasn't", "hadn't", "without"))
CANCEL_WORDS = frozenset(("cancel", "cancels", "cancelled", "canceled", "cancelling", "canceling", "cancellation",
                          "pulled", "scrapped", "scrap", "scrapping", "removed", "withdrawn", "withdrew", "withdraw",
                          "deleted", "killed", "gone"))
COMPLETION_WORDS = frozenset(("hit", "triggered", "done", "elected", "taken"))
UNFILLED_WORDS = frozenset(("unfilled", "unexecuted"))
MODALS = frozenset(("would", "could", "should", "might", "may", "must", "will", "can"))
COUNTERFACTUAL_WORDS = frozenset(("would've", "could've", "should've", "might've", "must've", "hypothetically",
                                  "hypothetical", "imagine", "suppose", "supposing"))
THIRD_PARTY = frozenset(("others", "buyers", "sellers", "analysts", "traders", "bulls", "bears", "people",
                         "everyone", "everybody", "someone", "somebody", "anyone", "anybody", "they", "funds",
                         "banks", "dealers", "speculators", "economists", "strategists", "consensus"))
FORECAST_WORDS = frozenset(("expect", "expects", "expected", "expecting", "expectation", "expectations", "forecast",
                            "forecasts", "forecasting", "forecasted", "predict", "predicts", "predicted", "prediction",
                            "projected", "projection", "estimate", "estimates", "likely", "unlikely", "probably",
                            "possibly", "perhaps", "maybe", "outlook"))
FUTURE_TIME = frozenset(("tomorrow", "tonight", "later", "next", "soon", "eventually", "upcoming"))
TRANSACTION_WORDS = (frozenset(("buy", "buys", "buying", "sell", "sells", "selling", "short", "shorts", "shorting",
                                "long", "longs", "add", "adds", "adding", "enter", "enters", "entering", "go", "going",
                                "initiate", "initiating", "open", "opening", "cover", "covering", "close", "closing",
                                "exit", "exiting", "trim", "trimming", "reduce", "reducing", "take", "taking",
                                "accumulate", "accumulating", "reload", "re-enter", "reenter"))
                     | ORDER_WORDS)
AUX_WORDS = frozenset(("was", "were", "got", "been", "is", "has", "have", "just", "finally", "now", "our", "the"))
AT_LEVEL_WORDS = frozenset(("our", "the", "my", "a", "an", "its", "their", "of", "level"))
# a word after "high" / "low" that keeps it a level noun ("the high was", "high of", "high at");
# any other word makes it an adjective ("high conviction")
LEVEL_FOLLOW = frozenset(("of", "at", "@", "is", "was", "were", "near", "around", "about", "from", "to", "and", "or",
                          "in", "on", "for", "the", "so", "has", "had", "been", "held", "holds", "came", "comes",
                          "which", "that", "stays", "stands", "remains", "area", "zone", "region", "level", "there",
                          "here", "printed", "prints", "made", "set", "yesterday", "today", "this", "last"))
# a word after a month name that makes it a time, not a contract ("December was", "the December high")
MONTH_TIME_NEXT = frozenset(("was", "were", "has", "had", "been", "month", "months")) | LEVEL_NOUNS
_MONTH3 = {"jan": "JAN", "feb": "FEB", "mar": "MAR", "apr": "APR", "may": "MAY", "jun": "JUN", "jul": "JUL",
           "aug": "AUG", "sep": "SEP", "oct": "OCT", "nov": "NOV", "dec": "DEC"}
_SEG_CHARS = ".,;:!?"
_SENT_CHARS = ".!?;"
_DASHES = frozenset(("-", "–", "—", "--"))


# ------------------------------------------------------------------ the product number reader

def post_numbers(text: str | None) -> list[dict]:
    """Every price-like number of the text in order, read with the product's own number reader
    (instruction_terms: token, edge and number patterns, outside an index's own name). Identical to
    the adjudication bundle's ``numbers``: same tokens, same index ``n``."""
    return [{"n": t.n, "token": t.token} for t in _tokens(text or "") if t.n is not None]


@dataclass
class _Tok:
    i: int
    start: int          # raw offset of the token with its edges stripped
    token: str          # the token with its edges stripped
    word: str           # lower case, quotes normalised, possessive kept
    n: int | None = None
    value: Decimal | None = None
    seg_end: bool = False
    sent_end: bool = False
    colon_end: bool = False
    line_start: bool = False
    raw_tail: str = ""
    raw_head: str = ""  # the edges stripped before the token ("'" of "'26")
    q_end: bool = False  # a question mark ends the token's sentence here


def _tokens(text: str) -> list[_Tok]:
    names = [m.span() for m in IT.INDEX_NAME_RE.finditer(text)]
    name_starts = [a for a, _b in names]

    def in_name(p: int) -> bool:   # the spans are disjoint and in order
        k = bisect.bisect_right(name_starts, p) - 1
        return k >= 0 and names[k][0] <= p < names[k][1]
    marks = "".join(IT.APPROXIMATION_MARKS)
    out: list[_Tok] = []
    count = 0
    matches = list(IT.TOKEN_RE.finditer(text))
    for k, m in enumerate(matches):
        raw = m.group(0)
        head = len(raw) - len(raw.lstrip(IT.EDGE))
        start = m.start() + head
        token = raw.strip(IT.EDGE)
        tail = raw[len(raw.rstrip(IT.EDGE)):] if token else raw
        gap_before = text[matches[k - 1].end():m.start()] if k else text[:m.start()]
        line_start = k == 0 or "\n" in gap_before
        if out and "\n" in gap_before:
            out[-1].seg_end = out[-1].sent_end = True
        if not token or token in _DASHES:
            if out:   # a lone punctuation mark or a dash ends the segment before it
                out[-1].seg_end = True
                out[-1].sent_end = out[-1].sent_end or any(ch in _SENT_CHARS for ch in raw)
                out[-1].colon_end = out[-1].colon_end or ":" in raw
                out[-1].q_end = out[-1].q_end or "?" in raw
            continue
        t = _Tok(i=len(out), start=start, token=token, line_start=line_start, raw_tail=tail, raw_head=raw[:head],
                 word=token.lower().replace("’", "'").replace("‘", "'"))
        bare = token.lstrip(marks)
        if not in_name(start):
            nm = IT.NUMBER_RE.match(bare)
            if nm and not nm.group(3):
                t.n, count = count, count + 1
                t.value = Decimal((nm.group(1) + (nm.group(2) or "")).replace(",", ""))
        t.seg_end = any(ch in _SEG_CHARS for ch in tail)
        t.sent_end = any(ch in _SENT_CHARS for ch in tail)
        t.colon_end = ":" in tail
        t.q_end = "?" in tail
        out.append(t)
    if out:
        out[-1].seg_end = out[-1].sent_end = True
    return out


# ------------------------------------------------------------------ the interpreter's clauses

@dataclass(frozen=True)
class ClauseUnit:
    """A clause of the post as the interpreter read it, in the interpreter's normalised coordinates."""
    start: int
    end: int
    action_id: str | None          # the action read from this clause (or the host of a level line)
    trading: bool                  # that action is a real action (not NO_TRADE)
    kind: Literal["CLAUSE", "LEVEL_LINE", "OTHER"] = "CLAUSE"
    month_applicable: bool = True  # False when the clause's market is a ticker (no contract month)
    past_execution: bool = False   # the interpreter read the clause as the publisher's past execution
    asset: str | None = None       # the market the interpreter read for that action


# ------------------------------------------------------------------ reading

@dataclass
class _Cue:
    cat: str
    d: int          # distance from the number (1 = the word right before it)
    word: str


def _words(toks: list[_Tok]) -> list[str]:
    return [t.word for t in toks]


def _negation(w: str) -> bool:
    return w in NEGATIONS or w.endswith("n't")


def _neg_before(words: list[str], j: int, span: int = 3) -> bool:
    return any(_negation(w) for w in words[max(0, j - span):j])


def _left_cues(words: list[str], sentence_initial: bool, holder_in_sentence: bool) -> list[_Cue]:
    """The cues of the words before a number (nearest last in ``words``)."""
    cues: list[_Cue] = []
    L = len(words)
    for j, w in enumerate(words):
        d = L - j
        prev = words[j - 1] if j else ""
        prev2 = words[j - 2] if j > 1 else ""
        nxt = words[j + 1] if j + 1 < L else ""
        w0 = w.removesuffix("'s")
        if w == "protective" and nxt in STRIKE_WORDS:
            continue   # "protective puts": options, not a stop
        if w in STOP_NOUNS or (w == "stop" and nxt == "loss"):
            if prev in ("buy", "sell"):
                cues.append(_Cue("ORDER", d, w))
            elif not (w == "stop" and nxt == "out"):
                cues.append(_Cue("STOP", d, w))
            continue
        if w in TARGET_NOUNS or (w == "take" and nxt == "profit") or (w == "profit" and nxt == "target"):
            cues.append(_Cue("TARGET", d, w))
            continue
        if w == "average" and (prev in MA_CONTEXT or prev2 in MA_CONTEXT):
            cues.append(_Cue("LEVEL", d, w))
            continue
        if w0 in ("close", "open", "settle") and prev in PRIOR_WORDS:
            cues.append(_Cue("LEVEL", d, w))
            continue
        if w in LEVEL_NOUNS:
            if prev in STOP_NOUNS or prev in TARGET_NOUNS or prev in ENTRY_NOUNS or prev in AVERAGE_NOUNS:
                continue   # "stop level", "entry zone": the earlier noun says the role
            if w in ("high", "low") and nxt[:1].isalpha() and nxt not in LEVEL_FOLLOW:
                continue   # "high conviction": an adjective, not a level
            cues.append(_Cue("LEVEL", d, w))
            continue
        if w in AVERAGE_NOUNS or (w == "mean" and nxt in ("price", "cost")):
            cues.append(_Cue("AVERAGE", d, w))
            continue
        if w in INDICATOR_NOUNS:
            cues.append(_Cue("INDICATOR", d, w))
            continue
        if w in MARKET_NOUNS and nxt not in ("order", "orders", "month"):
            cues.append(_Cue("MARKET", d, w))
            continue
        if w in ENTRY_NOUNS:
            cues.append(_Cue("ENTRY_NOUN", d, w))
            continue
        if w in EXIT_NOUNS:
            cues.append(_Cue("EXIT_NOUN", d, w))
            continue
        if w in FILL_WORDS or w in UNFILLED_WORDS:
            if prev not in AVERAGE_NOUNS:
                neg = w in UNFILLED_WORDS or _neg_before(words, j)
                cues.append(_Cue("FILL_NEGATED" if neg else "FILL", d, w))
            continue
        if w == "got" and nxt in ("filled", "long", "short", "in", "out"):
            cues.append(_Cue("OWN_PAST", d, w))
            continue
        if w == "went" and nxt in ("long", "short"):
            cues.append(_Cue("OWN_PAST", d, w))
            continue
        if w == "stopped" and (nxt in ("out", "at", "us") or prev in ("was", "were", "got", "been", "get")):
            cues.append(_Cue("OWN_PAST", d, w))
            continue
        if w == "averaged" and nxt in ("in", "into", "down", "up"):
            cues.append(_Cue("OWN_PAST", d, w))
            continue
        if w == "sold" and nxt == "off":
            cues.append(_Cue("MOVE_PAST", d, w))
            continue
        if w in OWN_PAST:
            cues.append(_Cue("OWN_PAST", d, w))
            continue
        if w in OWN_OR_MARKET_PAST:
            own = holder_in_sentence or (sentence_initial and j == 0) or any(x in POSITION_WORDS for x in words[j + 1:])
            cues.append(_Cue("OWN_PAST" if own else "MOVE_PAST", d, w))
            continue
        if w in MARKET_MOVE_PAST:
            cues.append(_Cue("MOVE_PAST", d, w))
            continue
        if w in MARKET_MOVE_PRESENT:
            cues.append(_Cue("MOVE_PRESENT", d, w))
            continue
        if w in PLAN_WORDS or w.endswith("'ll") or (w in PLAN_BEFORE_TO and nxt == "to"):
            cues.append(_Cue("PLAN", d, w))
            continue
        if w in DIP_WORDS or (w in ("pulls", "pull") and nxt == "back"):
            cues.append(_Cue("DIP", d, w))
            continue
        if w in ORDER_WORDS:
            cues.append(_Cue("ORDER", d, w))
            continue
        if w in CONDITION_WORDS:
            cues.append(_Cue("CONDITION", d, w))
            continue
        if w in CROSS_WORDS:
            cues.append(_Cue("CROSS", d, w))
            continue
        if w in EXIT_CONTEXT:
            cues.append(_Cue("EXIT_CONTEXT", d, w))
            continue
        if w in ACTION_CONTEXT:
            cues.append(_Cue("ACTION_CONTEXT", d, w))
            continue
        if w in PAST_MARKERS:
            cues.append(_Cue("PAST", d, w))
    return cues


def _right_cues(words: list[str]) -> set[str]:
    out = set()
    for j, w in enumerate(words):
        prev = words[j - 1] if j else ""
        nxt = words[j + 1] if j + 1 < len(words) else ""
        neg = _neg_before(words, j)
        if (w in FILL_WORDS and prev not in AVERAGE_NOUNS) or w in UNFILLED_WORDS:
            out.add("FILL_NEGATED" if neg or w in UNFILLED_WORDS else "FILL")
        elif w in ORDER_WORDS or (w == "stop" and prev in ("buy", "sell")):
            out.add("ORDER")
        elif w in DIP_WORDS or (w in ("pulls", "pull") and nxt == "back"):
            out.add("DIP")
        elif w in PAST_MARKERS:
            out.add("PAST")
        if w in COMPLETION_WORDS:
            out.add("COMPLETED_NEGATED" if neg else "COMPLETED")
    return out


def _attached(words: list[str]) -> tuple[str, list[str]]:
    """The first word before the number that is not an attacher, and the attachers skipped."""
    skipped = []
    for w in reversed(words):
        if w in ATTACHERS:
            skipped.append(w)
            continue
        return w, skipped
    return "", skipped


def _not_a_price(toks: list[_Tok], t: _Tok, left: list[str]) -> str | None:
    nxt = toks[t.i + 1].word if t.i + 1 < len(toks) and not t.seg_end else ""
    nxt3 = [x.word for x in toks[t.i + 1:t.i + 4]]
    prev = toks[t.i - 1].word if t.i and not toks[t.i - 1].seg_end else ""
    integer = t.value is not None and t.value == t.value.to_integral_value() and "." not in t.token
    if nxt in SIZE_UNITS:
        return "SIZE_OR_COUNT"
    if nxt in MA_UNITS:
        return "MA_PERIOD"
    if nxt in PERIOD_UNITS:
        return "MA_PERIOD" if any(x in ("moving", "ma", "sma", "ema", "dma", "average") for x in nxt3) else "TIME_PERIOD"
    if nxt.rstrip(".") in TIME_UNITS:
        return "TIME_OF_DAY"
    if nxt in CHANGE_UNITS:
        return "DISTANCE_OR_CHANGE"
    if prev in MA_UNITS:
        return "MA_PERIOD"
    att, _skipped = _attached(left)
    if att in INDICATOR_NOUNS:
        return "INDICATOR_VALUE"
    month_prev = bool(prev) and IT.MONTH_WORD_RE.fullmatch(prev) is not None
    if month_prev and integer and len(t.token) == 2 and t.raw_head[-1:] in ("'", "’"):
        return "DATE_OR_CONTRACT_YEAR"   # "Dec '26"
    if month_prev and integer and 1 <= t.value <= 31:
        return "DATE"
    if month_prev and integer and (len(t.token) == 2 or 1990 <= t.value <= 2099):
        return "DATE_OR_CONTRACT_YEAR"
    if integer and 1 <= t.value <= 31 and nxt and IT.MONTH_WORD_RE.fullmatch(nxt) and nxt not in IT.ENGLISH_WORDS:
        return "DATE"
    if integer and "," not in t.token and 1990 <= t.value <= 2099 and prev in YEAR_PREPS:
        return "YEAR"
    if prev in PNL_VERBS or (prev in ("of", "is", "was") and t.i > 1 and toks[t.i - 2].word in PNL_WORDS) \
            or prev in PNL_WORDS:
        return "PROFIT_OR_LOSS"
    if t.line_start and integer and t.value < 50 and t.raw_tail[:1] in (")", ".") and t.i + 1 < len(toks) \
            and not toks[t.i + 1].line_start:
        return "LIST_MARKER"
    return None


def _yearish(t: _Tok | None) -> bool:
    """A contract year after a month name: "2026", "'26", or two digits that cannot be a day."""
    if t is None or t.n is None:
        return False
    if re.fullmatch(r"20\d{2}", t.token):
        return True
    return re.fullmatch(r"\d{2}", t.token) is not None and (t.raw_head[-1:] in ("'", "’") or int(t.token) > 31)


def _month_evidence(toks: list[_Tok]) -> tuple[ContractMonthEvidence, str | None] | None:
    words = _words(toks)
    for j, t in enumerate(toks):
        w = words[j]
        prev = words[j - 1] if j else ""
        nxt = words[j + 1] if j + 1 < len(words) else ""
        nxt2 = words[j + 2] if j + 2 < len(words) else ""
        code = IT.CONTRACT_CODE_RE.fullmatch(t.token)
        if code and (code.group("root") or nxt in ("contract", "futures", "month") or prev in ("the",)):
            return ContractMonthEvidence.NAMED, code.group("code") + code.group("yy")
        if w in ("front", "front-month", "nearby", "prompt") and (w == "front-month" or nxt in ("month", "contract", "months")):
            return ContractMonthEvidence.FRONT, None
        if w == "spot" and nxt == "month":
            return ContractMonthEvidence.FRONT, None
        if (w in ("second", "deferred", "back", "next") and nxt in ("month", "contract")
                and (w != "next" or nxt2 in ("contract", "futures") or nxt == "contract")
                and not (nxt == "month" and nxt2 == "of")) \
                or w in ("second-month", "next-month", "deferred"):
            return ContractMonthEvidence.NEXT, None   # never "the second month OF the trade"
        mw = IT.MONTH_WORD_RE.fullmatch(t.token.lower())
        if mw:
            mon = _MONTH3[mw.group("word")[:3].lower()]
            nt = toks[j + 1] if j + 1 < len(toks) else None
            yearish = _yearish(nt)
            contractish = nxt in ("contract", "futures") or yearish
            if w.rstrip(".") in IT.ENGLISH_WORDS and not contractish:
                continue
            if prev in TIME_PREPS and not contractish:
                continue
            if nxt in MONTH_TIME_NEXT and not contractish:
                continue   # "December was a good month", "the December high": a time, not a contract
            if t.n is None and nt is not None and nt.value is not None and not yearish \
                    and nt.value == nt.value.to_integral_value() and 1 <= nt.value <= 31 \
                    and nxt not in ("at",):
                continue   # "Dec 5", "Dec 12": a date
            return ContractMonthEvidence.NAMED, mon
    return None


@dataclass
class _Ctx:
    """What the decision of one number reads: its own words and the sentence's flags."""
    left: list[str]                  # the words of its segment before it, after the previous price
    cues: list[_Cue]
    ext_words: list[str]             # the earlier segments of its sentence (tense only)
    ext_cues: list[_Cue]
    right: set[str]                  # cue classes of the words after it
    right_words: list[str]           # the words after it, to the next number of its segment (+ a numberless segment)
    wide_words: list[str]            # the rest of its segment and a following numberless segment
    first_after: str
    prev_price: dict | None
    same_seg_prev: bool
    unit: ClauseUnit | None
    holder_before: bool              # a holder word (we, our) earlier in the sentence
    cond_before: bool                # a condition word (if, once) earlier in the sentence
    question: bool                   # the sentence is a question


def read_price_roles(text: str | None, units: list[ClauseUnit] | tuple = (),
                     to_norm: Callable[[int], int] | None = None) -> list[PriceReading]:
    """The structured price readings of a post. ``units`` and ``to_norm`` come from the interpreter
    (its clauses and the map from a raw text offset to its normalised offset); without them the
    readings carry no action. One pass over the tokens: segment and sentence tables are built once,
    every window is bounded (WINDOW), and at most MAX_NUMBERS numbers are read."""
    toks = _tokens(text or "")
    nums = [t for t in toks if t.n is not None]
    if not nums:
        return []
    n_tok = len(toks)
    seg_id, sent_id = [0] * n_tok, [0] * n_tok
    seg_first: dict[int, int] = {}
    seg_last: dict[int, int] = {}
    sent_first: dict[int, int] = {}
    sent_segs: dict[int, list[int]] = {}
    question: dict[int, bool] = {}
    s = g = 0
    for t in toks:
        seg_id[t.i], sent_id[t.i] = g, s
        seg_first.setdefault(g, t.i)
        seg_last[g] = t.i
        if sent_first.setdefault(s, t.i) == t.i or sent_segs[s][-1] != g:
            sent_segs.setdefault(s, []).append(g)
        if t.sent_end:
            question[s] = t.q_end
        if t.seg_end:
            g += 1
        if t.sent_end:
            s += 1
    seg_has_number = {seg_id[t.i] for t in nums}
    holder_before, cond_before = [False] * n_tok, [False] * n_tok
    cur, h, c = -1, False, False
    for t in toks:
        if sent_id[t.i] != cur:
            cur, h, c = sent_id[t.i], False, False
        holder_before[t.i], cond_before[t.i] = h, c
        if t.n is None:
            h = h or t.word in HOLDER_WORDS
            c = c or t.word in CONDITION_WORDS
    units = list(units)
    starts = [u.start for u in units]
    ordered = all(units[k].end <= units[k + 1].start for k in range(len(units) - 1)) and \
        all(u.start <= u.end for u in units)
    trading_ids = list(dict.fromkeys(u.action_id for u in units if u.trading and u.action_id))
    asset_of: dict[str, str | None] = {}
    first_unit_of: dict[str, ClauseUnit] = {}
    for u in units:
        if u.action_id is not None:
            first_unit_of.setdefault(u.action_id, u)
            if u.trading:
                asset_of.setdefault(u.action_id, u.asset)

    def unit_for(t: _Tok) -> ClauseUnit | None:
        if not units or to_norm is None:
            return None
        p = to_norm(t.start)
        if not ordered:
            inside = [u for u in units if u.start <= p < u.end]
            return inside[0] if inside else None
        # disjoint units in order: only the last unit starting at or before p can contain it
        k = bisect.bisect_right(starts, p) - 1
        return units[k] if k >= 0 and units[k].start <= p < units[k].end else None

    results: dict[int, dict] = {}
    unit_of: dict[int, ClauseUnit | None] = {}
    last_price_tok: dict[int, int] = {}      # sentence -> token index of its last price number so far
    capped: list[_Tok] = []
    for t in nums:
        if t.n >= MAX_NUMBERS:
            capped.append(t)
            continue
        si, gi = sent_id[t.i], seg_id[t.i]
        floor = last_price_tok.get(si, -1)
        seg_start = seg_first[gi]
        lo0 = max(seg_start, floor + 1)
        lo = max(lo0, t.i - WINDOW)
        left_toks = [x for x in toks[lo:t.i] if x.n is None or results.get(x.n, {}).get("role") == PriceReadingRole.NOT_A_PRICE]
        if not left_toks and seg_start > floor + 1 and seg_start > 0 and sent_id[seg_start - 1] == si:
            prev_seg = seg_id[seg_start - 1]   # "Stop: 86.00", "stop at yesterday's low, 69.80": a label
            a = max(floor + 1, seg_first[prev_seg], seg_start - WINDOW)
            left_toks = [x for x in toks[a:seg_start] if x.n is None]
        sent_start = sent_first[si]
        ext_toks = [x for x in toks[max(sent_start, floor + 1, lo0 - WINDOW):lo0] if x.n is None]
        # the words after: to the next number in the segment, plus the next segment when it has no number
        right_toks: list[_Tok] = []
        k = t.i + 1
        while k < n_tok and seg_id[k] == gi and toks[k].n is None and sent_id[k] == si and len(right_toks) < WINDOW:
            right_toks.append(toks[k])
            k += 1
        nxt_seg: list[_Tok] = []
        end_own = seg_last[gi] + 1
        if end_own < n_tok and sent_id[end_own] == si and not t.sent_end and seg_id[end_own] not in seg_has_number:
            g2 = seg_id[end_own]
            nxt_seg = toks[seg_first[g2]:min(seg_last[g2] + 1, seg_first[g2] + WINDOW)]
        if k < n_tok and seg_id[k] != gi and sent_id[k] == si and not t.sent_end and nxt_seg:
            right_toks += nxt_seg
        wide = _words(toks[t.i + 1:min(end_own, t.i + 1 + WINDOW)] + nxt_seg)
        left = _words(left_toks)
        holder = holder_before[t.i]
        sentence_initial = bool(left_toks) and left_toks[0].i == sent_start
        cues = _left_cues(left, sentence_initial, holder)
        ext_words = _words(ext_toks)
        ext_cues = _left_cues(ext_words, bool(ext_toks) and ext_toks[0].i == sent_start, holder)
        right_words = _words(right_toks)
        pk = last_price_tok.get(si)
        prev_price = results.get(toks[pk].n) if pk is not None else None
        unit_of[t.n] = unit_for(t)
        ctx = _Ctx(left=left, cues=cues, ext_words=ext_words, ext_cues=ext_cues, right=_right_cues(right_words),
                   right_words=right_words, wide_words=wide,
                   first_after=right_toks[0].word if right_toks and not t.seg_end else "",
                   prev_price=prev_price, same_seg_prev=prev_price is not None and seg_id[prev_price["tok"].i] == gi,
                   unit=unit_of[t.n], holder_before=holder, cond_before=cond_before[t.i],
                   question=question.get(si, False))
        res = _decide(toks, t, ctx)
        res["tok"] = t
        results[t.n] = res
        if res["role"] != PriceReadingRole.NOT_A_PRICE:
            last_price_tok[si] = t.i
    # contract month (evidence of the number's own segment; else of a segment of its sentence that has no
    # price of its own), association, other month
    price_segs = {seg_id[r["tok"].i] for r in results.values() if r["role"] != PriceReadingRole.NOT_A_PRICE} \
        | {seg_id[t.i] for t in capped}
    seg_ev: dict[int, tuple | None] = {}

    def seg_month(gx: int):
        if gx not in seg_ev:
            seg_ev[gx] = _month_evidence(toks[seg_first[gx]:seg_last[gx] + 1])
        return seg_ev[gx]
    sent_ev: dict[int, tuple | None] = {}

    def sent_month(sx: int):
        if sx not in sent_ev:
            sent_ev[sx] = next((ev for gx in sent_segs[sx] if gx not in price_segs for ev in [seg_month(gx)] if ev),
                               None)
        return sent_ev[sx]
    readings: list[PriceReading] = []
    month_of: dict[int, tuple] = {}
    action_month: dict[str, tuple] = {}
    for n, r in results.items():
        t = r["tok"]
        ev, where = seg_month(seg_id[t.i]), "MONTH_NAMED_IN_SEGMENT"
        if ev is None:
            ev, where = sent_month(sent_id[t.i]), "MONTH_NAMED_IN_SENTENCE"
        if ev is not None:
            month_of[n] = (ev, where)
            u = unit_of[n]
            if u is not None and u.trading and u.action_id and r["role"] != PriceReadingRole.NOT_A_PRICE:
                action_month.setdefault(u.action_id, ev)
            elif r["role"] in TRADE_LEVEL_ROLES:   # the month of the publisher's own trade levels
                action_month.setdefault("_levels_", ev)
    for n in sorted(results):
        r = results[n]
        t = r["tok"]
        role, reasons, exact = r["role"], list(r["reasons"]), r["exactness"]
        u = unit_of[n]
        action_id, assoc = None, "NONE"
        if role != PriceReadingRole.NOT_A_PRICE:
            if u is not None and u.trading and u.action_id:
                action_id, assoc = u.action_id, ("LEVEL_LINE" if u.kind == "LEVEL_LINE" else "CLAUSE")
            elif len(trading_ids) == 1 and role in (PriceReadingRole.STOP, PriceReadingRole.TARGET,
                                                    PriceReadingRole.AVERAGE_OR_COST_BASIS,
                                                    PriceReadingRole.HISTORICAL_FILL,
                                                    PriceReadingRole.INTENDED_ENTRY):
                # the post's only trading action, never when the number's own clause is another action's
                # about a market that is not the target action's ("we bought crude ... gold looks heavy,
                # bids at 2,380")
                target = trading_ids[0]
                other_market = u is not None and u.action_id is not None and u.asset is not None \
                    and u.asset != asset_of.get(target)
                if not other_market:
                    action_id, assoc = target, "SOLE_ACTION_IN_POST"
        month, named = ContractMonthEvidence.NOT_STATED, None
        applicable = role != PriceReadingRole.NOT_A_PRICE
        unit_for_month = u if u is not None else first_unit_of.get(action_id) if action_id else None
        if unit_for_month is not None and not unit_for_month.month_applicable:
            applicable = False
        if not applicable:
            month = ContractMonthEvidence.NOT_APPLICABLE
        elif n in month_of:
            (month, named), where = month_of[n]
            reasons.append(where if month == ContractMonthEvidence.NAMED else
                           "FRONT_MONTH_WORD" if month == ContractMonthEvidence.FRONT else "NEXT_MONTH_WORD")
            own_actions = set(action_month.values())
            if own_actions and (month, named) not in own_actions and role in (
                    PriceReadingRole.CURRENT_MARKET, PriceReadingRole.CHART_LEVEL, PriceReadingRole.UNRESOLVED) \
                    and action_id is None:
                role, reasons = PriceReadingRole.OTHER_CONTRACT_MONTH, ["MONTH_DIFFERS_FROM_ACTION"] + reasons
        elif action_id is not None and action_id in action_month:
            month, named = action_month[action_id]
            reasons.append("MONTH_OF_ACTION")
        readings.append(PriceReading(n=n, value=_plain(t.value), role=role, exactness=exact, contract_month=month,
                                     month_named=named if month == ContractMonthEvidence.NAMED else None,
                                     action_id=action_id, association=assoc, reasons=reasons))
    for t in capped:
        readings.append(PriceReading(n=t.n, value=_plain(t.value), role=PriceReadingRole.UNRESOLVED,
                                     exactness=Exactness.NOT_APPLICABLE, contract_month=ContractMonthEvidence.NOT_STATED,
                                     reasons=["NUMBER_CAP_REACHED"]))
    return readings


TRADE_LEVEL_ROLES = frozenset((PriceReadingRole.INTENDED_ENTRY, PriceReadingRole.HISTORICAL_FILL, PriceReadingRole.STOP,
                               PriceReadingRole.TARGET, PriceReadingRole.AVERAGE_OR_COST_BASIS))


def _plain(v: Decimal | None) -> str:
    return "" if v is None else format(v, "f")


def _nearest(cues: list[_Cue], cats) -> _Cue | None:
    hits = [c for c in cues if c.cat in cats]
    return min(hits, key=lambda c: c.d) if hits else None


ROLE_CATS = {"STOP": PriceReadingRole.STOP, "TARGET": PriceReadingRole.TARGET,
             "LEVEL": PriceReadingRole.CHART_LEVEL, "AVERAGE": PriceReadingRole.AVERAGE_OR_COST_BASIS,
             "MARKET": PriceReadingRole.CURRENT_MARKET}
ROLE_REASON = {"STOP": "STOP_WORD", "TARGET": "TARGET_WORD", "LEVEL": "LEVEL_WORD", "AVERAGE": "AVERAGING_WORD",
               "MARKET": "MARKET_WORD"}
DECISIVE_TENSE = ("OWN_PAST", "PLAN", "MOVE_PAST")
_ROLE_NOUNS = STOP_NOUNS | TARGET_NOUNS
_REACHED = frozenset(("hit", "reached", "touched", "tagged"))


def _decide(toks, t, c: _Ctx) -> dict:
    """The role from the positive evidence, then the defeaters of an execution-relevant role."""
    return _defeat(_evidence(toks, t, c), c)


def _evidence(toks, t, c: _Ctx) -> dict:
    R = PriceReadingRole
    left, cues, ext_cues, right, first_after, prev_price = c.left, c.cues, c.ext_cues, c.right, c.first_after, c.prev_price
    nap = _not_a_price(toks, t, left)
    if nap is not None:
        return {"role": R.NOT_A_PRICE, "reasons": [nap], "exactness": Exactness.NOT_APPLICABLE}
    att, skipped = _attached(left)
    approx = (t.token[:1] in IT.APPROXIMATION_MARKS or any(w in APPROX_BEFORE for w in skipped)
              or first_after in APPROX_AFTER or att == "between"
              or (first_after == "or" and t.i + 2 < len(toks) and toks[t.i + 2].word == "so"))
    if prev_price is not None and left in (["and"], ["-"]) and prev_price.get("between"):
        approx = True
    exact = Exactness.APPROXIMATE if approx else Exactness.EXACT
    chained_unresolved = False

    def done(role, *reasons):
        return {"role": role, "reasons": list(reasons), "exactness": exact, "between": att == "between",
                "skipped": skipped, "chained_unresolved": chained_unresolved}

    nxt_word = toks[t.i + 1].word if t.i + 1 < len(toks) else ""
    if nxt_word in STRIKE_WORDS or att == "strike" or att in STRIKE_WORDS:
        return done(R.UNRESOLVED, "OPTION_STRIKE")
    tense = _nearest(cues, DECISIVE_TENSE)
    # 0. the price obtained at the stop or the target is a fill (rubric 14.1): "exited at our 75 target",
    #    "we were stopped at our stop of 69.50", "stop hit at 69.50"
    if tense is not None and tense.cat == "OWN_PAST":
        span = left[len(left) - tense.d + 1:]
        ats = [k for k, w in enumerate(span) if w in ("at", "@")]
        if ats:
            tail = span[ats[-1] + 1:]
            if all(w in AT_LEVEL_WORDS or w in _ROLE_NOUNS for w in tail) and (
                    any(w in _ROLE_NOUNS for w in tail) or first_after in _ROLE_NOUNS):
                return done(R.HISTORICAL_FILL, "OWN_EXECUTION_PAST", "EXECUTED_AT_STOP_OR_TARGET")
    if tense is not None and tense.cat == "MOVE_PAST" and tense.word in _REACHED and skipped[:1] in (["at"], ["@"]):
        named = [x for x in cues if x.cat in ("STOP", "TARGET") and x.d > tense.d]
        if named:
            rc = min(named, key=lambda x: x.d)
            if all(w in AUX_WORDS for w in left[len(left) - rc.d + 1:len(left) - tense.d]):
                return done(R.HISTORICAL_FILL, "STOP_OR_TARGET_EXECUTED")
    # 1. a role word written right after the number ("75 target", "2,450 resistance")
    if first_after in STOP_NOUNS:
        return done(R.STOP, "ROLE_WORD_AFTER_NUMBER")
    if first_after in TARGET_NOUNS:
        return done(R.TARGET, "ROLE_WORD_AFTER_NUMBER")
    if first_after in LEVEL_NOUNS - {"top", "bottom", "level", "levels"} or first_after in ("level", "levels"):
        return done(R.CHART_LEVEL, "ROLE_WORD_AFTER_NUMBER")
    if first_after in AVERAGE_NOUNS - {"cost"}:
        return done(R.AVERAGE_OR_COST_BASIS, "ROLE_WORD_AFTER_NUMBER")
    # 2. a chain of numbers ("from 2,440 to 2,425", "targets 75 and 78", "75, 78") shares one role; a chain
    #    with an UNRESOLVED number never yields a price instruction or a market price on its other end
    if prev_price is not None and \
            ((c.same_seg_prev and left and all(w in CHAIN_WORDS for w in left)) or (not c.same_seg_prev and not left)):
        if prev_price["role"] != R.UNRESOLVED:
            return done(prev_price["role"], "SAME_ROLE_AS_PREVIOUS_NUMBER", *prev_price["reasons"][:1])
        chained_unresolved = True
    role_cue = _nearest(cues, ROLE_CATS)
    # 3. a role noun of the number's own segment, unless a decisive verb is nearer to the number
    if role_cue is not None and (tense is None or role_cue.d < tense.d):
        if role_cue.cat in ("LEVEL", "AVERAGE"):
            st = [x for x in cues if x.cat in ("STOP", "TARGET") and x.d > role_cue.d]
            if st:
                far = max(st, key=lambda x: x.d)
                between = left[len(left) - far.d + 1:len(left) - role_cue.d]
                if any(w in RELATIVE_PREPS for w in between):
                    if role_cue.cat == "LEVEL":
                        return done(R.CHART_LEVEL, "STOP_RELATIVE_TO_LEVEL")
                else:   # "stop at yesterday's low of 69.80", "raise the stop to breakeven at 70.10"
                    return done(ROLE_CATS[far.cat], "STOP_AT_LEVEL")
        if role_cue.cat == "MARKET" and ("PAST" in right or any(x.cat == "PAST" for x in cues)):
            return done(R.UNRESOLVED, "PAST_MARKET_PRICE")
        return done(ROLE_CATS[role_cue.cat], ROLE_REASON[role_cue.cat])
    cond = any(x.cat == "CONDITION" for x in cues + ext_cues) or c.cond_before
    cross = _nearest(cues, ("CROSS",))
    move_now = _nearest(cues, ("MOVE_PRESENT",))
    action_ctx = _nearest(cues, ("ACTION_CONTEXT", "ENTRY_NOUN"))
    exit_ctx = _nearest(cues, ("EXIT_CONTEXT", "EXIT_NOUN"))
    # 4. a level of a condition ("if silver closes above 31.50")
    if cond and (cross is not None or move_now is not None) and (tense is None or tense.cat != "OWN_PAST"):
        return done(R.CHART_LEVEL, "CONDITION_LEVEL")
    # 5. a threshold ("heavy below 2,400"); with an entry verb it may be an order trigger
    if att in CROSS_WORDS and (tense is None or tense.cat == "MOVE_PAST"):
        if action_ctx is not None or exit_ctx is not None or "ORDER" in right:
            return done(R.UNRESOLVED, "ENTRY_OR_TRIGGER_LEVEL")
        return done(R.CHART_LEVEL, "THRESHOLD_LEVEL")
    # 6. a market move ("bounced from 70", "rallied to 2,450")
    if tense is not None and tense.cat == "MOVE_PAST":
        return done(R.CHART_LEVEL, "MARKET_MOVE_LEVEL")
    # 7. entry or exit kind: the publisher's fill, an order or plan, the market now
    ext_tense = _nearest(ext_cues, DECISIVE_TENSE) if tense is None else None
    t_cat = tense.cat if tense is not None else (ext_tense.cat if ext_tense is not None else None)
    market = []
    if att in MARKET_STATE_ADVERBS or att in MARKET_STATE_VERBS or first_after == "now":
        market.append("MARKET_STATE_WORD")
    elif skipped[:1] in (["at"], ["@"]) and "with" in left[-5:] and \
            all(w not in STOP_NOUNS | TARGET_NOUNS | LEVEL_NOUNS | AVERAGE_NOUNS | INDICATOR_NOUNS
                for w in left[len(left) - 1 - left[::-1].index("with"):]):
        market.append("WITH_MARKET_AT")
    fill = []
    # an earlier segment's tense, or the clause's, never overrides a market-state word of the number itself
    if t_cat == "OWN_PAST" and not (tense is None and market):
        fill.append("OWN_EXECUTION_PAST")
    if "FILL" in right or any(x.cat == "FILL" for x in cues):
        fill.append("FILL_WORD")
    fill_negated = "FILL_NEGATED" in right or any(x.cat == "FILL_NEGATED" for x in cues)
    if not fill and t_cat is None and skipped[:1] == ["from"] and any(w in ("long", "short", "in") for w in left) \
            and c.holder_before:
        fill.append("POSITION_HELD_FROM")
    unit = c.unit
    if not fill and t_cat is None and not market and unit is not None and unit.past_execution \
            and skipped[:1] in (["at"], ["@"], ["from"]) and not any(w in ("was", "were") for w in left):
        fill.append("CLAUSE_PAST_EXECUTION")
    order_words = "ORDER" in right or any(x.cat == "ORDER" for x in cues)
    if not fill and order_words and "COMPLETED" in right:
        fill.append("ORDER_COMPLETED")   # "the limit at 2,380 was hit": a filled order, not an instruction
    dip = "DIP" in right or any(x.cat == "DIP" for x in cues)
    order = []
    if t_cat == "PLAN":
        order.append("PLANNED_TRANSACTION")
    if order_words:
        order.append("ORDER_WORD")
    if dip and not order:
        order.append("PLANNED_TRANSACTION")
    if not order and cond and action_ctx is not None and skipped[:1] in (["at"], ["@"]):
        order.append("CONDITIONAL_PLAN")
    past = "PAST" in right or any(x.cat == "PAST" for x in cues)
    if fill:
        if order and t_cat == "PLAN":
            return done(R.UNRESOLVED, "CONFLICTING_CUES")
        if order_words and not dip:
            return done(R.HISTORICAL_FILL, *fill, "FILLED_ORDER")
        return done(R.HISTORICAL_FILL, *fill)
    if fill_negated:
        return done(R.UNRESOLVED, "NOT_FILLED", *order)
    if order:
        if market:
            return done(R.UNRESOLVED, "CONFLICTING_CUES")
        return done(R.INTENDED_ENTRY, *order)
    if market:
        if past:
            return done(R.UNRESOLVED, "PAST_MARKET_PRICE")
        return done(R.CURRENT_MARKET, *market)
    if action_ctx is not None:
        return done(R.UNRESOLVED, "ENTRY_KIND_NOT_STATED")
    if exit_ctx is not None:
        return done(R.UNRESOLVED, "EXIT_KIND_NOT_STATED")
    if past:
        return done(R.UNRESOLVED, "PAST_MARKET_PRICE")
    return done(R.UNRESOLVED, "NO_ROLE_EVIDENCE")


def _counterfactual(words: list[str]) -> bool:
    return any(w in COUNTERFACTUAL_WORDS or (w in MODALS - {"will", "can", "may"} and j + 1 < len(words)
                                             and words[j + 1] == "have")
               for j, w in enumerate(words))


def _cancelled(words: list[str]) -> bool:
    for j, w in enumerate(words):
        nxt = words[j + 1] if j + 1 < len(words) else ""
        if w in CANCEL_WORDS and not (w == "gone" and nxt in ("long", "short", "in")) \
                and not (w == "pulled" and nxt == "back"):
            return True
    return False


_EXEC_ROLES = (PriceReadingRole.INTENDED_ENTRY, PriceReadingRole.CURRENT_MARKET)


def _defeat(res: dict, c: _Ctx) -> dict:
    """INTENDED_ENTRY and CURRENT_MARKET (and a fill) stand only when nothing in the number's own clause
    defeats them; a defeated reading is UNRESOLVED with the defeater's code first."""
    R = PriceReadingRole
    role = res["role"]
    if role not in (R.INTENDED_ENTRY, R.CURRENT_MARKET, R.HISTORICAL_FILL):
        return res
    left, near = c.left, c.right_words
    near_mkt: list[str] = []   # the words after a market price, up to the publisher's own clause
    for w in near:
        if w in HOLDER_WORDS:
            break
        near_mkt.append(w)
    reasons = res["reasons"]
    chained = reasons[:1] == ["SAME_ROLE_AS_PREVIOUS_NUMBER"]
    code = None
    if c.question:
        code = "QUESTION"
    elif _counterfactual(left + near):
        code = "COUNTERFACTUAL"
    elif role in _EXEC_ROLES and (_cancelled(left) or _cancelled(c.ext_words) or _cancelled(c.wide_words)):
        code = "CANCELLED_OR_WITHDRAWN"
    elif any(_negation(w) for w in (left + near if role == R.INTENDED_ENTRY else
                                    left + near_mkt if role == R.CURRENT_MARKET else left)):
        code = "NEGATED"
    elif any(w in THIRD_PARTY for w in left) and not any(w in HOLDER_WORDS for w in left):
        code = "THIRD_PARTY"
    elif role in _EXEC_ROLES and any(w in FORECAST_WORDS for w in left + (near if role == R.INTENDED_ENTRY else near_mkt)):
        code = "FORECAST"
    elif role == R.INTENDED_ENTRY and not chained and set(reasons) <= {"PLANNED_TRANSACTION"} \
            and not any(w in TRANSACTION_WORDS for w in left + c.ext_words + near):
        code = "NO_TRANSACTION_WORD"   # "we will look at gold near 2,380", "crude will retest 70"
    elif role == R.INTENDED_ENTRY and res.get("skipped", [])[:1] == ["from"] \
            and any(w in ("long", "short", "in", "position") for w in left):
        code = "POSITION_ALREADY_HELD"   # "long from 70": a held position, never an order
    elif role == R.CURRENT_MARKET:
        if c.cond_before or any(x.cat == "CONDITION" for x in c.cues + c.ext_cues) \
                or any(w in CONDITION_WORDS for w in near_mkt):
            code = "CONDITIONAL_MARKET_STATE"
        elif c.first_after in MODALS:
            code = "HYPOTHETICAL_MARKET_STATE"
        elif any(w in FUTURE_TIME for w in left + near_mkt):
            code = "FUTURE_MARKET_STATE"
    if code is None and res.get("chained_unresolved") and role in _EXEC_ROLES:
        code = "CHAIN_WITH_UNRESOLVED"
    if code is None:
        return res
    out = dict(res)
    out["role"], out["reasons"] = R.UNRESOLVED, [code] + list(reasons)
    return out


def safe_read_price_roles(text: str | None, units=(), to_norm=None) -> list[PriceReading]:
    """``read_price_roles`` that can never raise: the readings are advisory and must not disturb the
    interpreter. A fault is counted and logged (code only) and yields one UNRESOLVED reading per number
    (READER_FAULT, exactness NOT_APPLICABLE)."""
    try:
        return read_price_roles(text, units, to_norm)
    except Exception:  # noqa: BLE001
        _note("READER_FAULT")
        try:
            return [PriceReading(n=x["n"], value="", role=PriceReadingRole.UNRESOLVED, exactness=Exactness.NOT_APPLICABLE,
                                 contract_month=ContractMonthEvidence.NOT_STATED, reasons=["READER_FAULT"])
                    for x in post_numbers(text)]
        except Exception:  # noqa: BLE001
            _note("READER_FAULT_NO_NUMBERS")
            return []


def _note(code: str) -> None:
    try:
        note_fault(code)
    except Exception:  # noqa: BLE001 - counting a fault must never become a fault
        pass
