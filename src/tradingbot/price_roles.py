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
    reported fill stays HISTORICAL_FILL.
Exactness is how the number is written (a hedge word, an approximation mark, an end of a range).
The contract month is assigned only on evidence in the text: a month named as a contract month (not
as a time: "in December" is a time), a futures code, "front month", "second month"; never from the
post's date, the listed contracts or the price."""
from __future__ import annotations

import re
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
    # unresolved
    "ENTRY_KIND_NOT_STATED", "EXIT_KIND_NOT_STATED", "CONFLICTING_CUES", "PAST_MARKET_PRICE",
    "ENTRY_OR_TRIGGER_LEVEL", "OPTION_STRIKE", "NO_ROLE_EVIDENCE", "READER_FAULT",
    # contract month evidence
    "MONTH_NAMED_IN_SEGMENT", "MONTH_NAMED_IN_SENTENCE", "MONTH_OF_ACTION", "FRONT_MONTH_WORD",
    "NEXT_MONTH_WORD",
))

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
ORDER_WORDS = frozenset(("limit", "limits", "lmt", "order", "orders", "bid", "bids", "bidding", "offer",
                         "offers", "offered", "working", "resting", "pending", "gtc", "stop-limit"))
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


def _tokens(text: str) -> list[_Tok]:
    names = [m.span() for m in IT.INDEX_NAME_RE.finditer(text)]
    marks = "".join(IT.APPROXIMATION_MARKS)
    out: list[_Tok] = []
    count = 0
    matches = list(IT._TOKEN_RE.finditer(text))
    for k, m in enumerate(matches):
        raw = m.group(0)
        start = m.start() + (len(raw) - len(raw.lstrip(IT._EDGE)))
        token = raw.strip(IT._EDGE)
        tail = raw[len(raw.rstrip(IT._EDGE)):] if token else raw
        gap_before = text[matches[k - 1].end():m.start()] if k else text[:m.start()]
        line_start = k == 0 or "\n" in gap_before
        if out and "\n" in gap_before:
            out[-1].seg_end = out[-1].sent_end = True
        if not token or token in _DASHES:
            if out:   # a lone punctuation mark or a dash ends the segment before it
                out[-1].seg_end = True
                out[-1].sent_end = out[-1].sent_end or any(ch in _SENT_CHARS for ch in raw)
                out[-1].colon_end = out[-1].colon_end or ":" in raw
            continue
        t = _Tok(i=len(out), start=start, token=token, line_start=line_start, raw_tail=tail,
                 word=token.lower().replace("’", "'").replace("‘", "'"))
        bare = token.lstrip(marks)
        if not any(a <= start < b for a, b in names):
            nm = IT._NUMBER_RE.match(bare)
            if nm and not nm.group(3):
                t.n, count = count, count + 1
                t.value = Decimal((nm.group(1) + (nm.group(2) or "")).replace(",", ""))
        t.seg_end = any(ch in _SEG_CHARS for ch in tail)
        t.sent_end = any(ch in _SENT_CHARS for ch in tail)
        t.colon_end = ":" in tail
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


# ------------------------------------------------------------------ reading

@dataclass
class _Cue:
    cat: str
    d: int          # distance from the number (1 = the word right before it)
    word: str


def _words(toks: list[_Tok]) -> list[str]:
    return [t.word for t in toks]


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
        if w in FILL_WORDS:
            if prev not in AVERAGE_NOUNS:
                cues.append(_Cue("FILL", d, w))
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
        if w in FILL_WORDS and prev not in AVERAGE_NOUNS:
            out.add("FILL")
        elif w in ORDER_WORDS or (w == "stop" and prev in ("buy", "sell")):
            out.add("ORDER")
        elif w in DIP_WORDS or (w in ("pulls", "pull") and nxt == "back"):
            out.add("DIP")
        elif w in PAST_MARKERS:
            out.add("PAST")
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


def _month_evidence(toks: list[_Tok]) -> tuple[ContractMonthEvidence, str | None] | None:
    words = _words(toks)
    for j, t in enumerate(toks):
        w = words[j]
        prev = words[j - 1] if j else ""
        nxt = words[j + 1] if j + 1 < len(words) else ""
        code = IT.CONTRACT_CODE_RE.fullmatch(t.token)
        if code and (code.group("root") or nxt in ("contract", "futures", "month") or prev in ("the",)):
            return ContractMonthEvidence.NAMED, code.group("code") + code.group("yy")
        if w in ("front", "front-month", "nearby", "prompt") and (w == "front-month" or nxt in ("month", "contract", "months")):
            return ContractMonthEvidence.FRONT, None
        if w == "spot" and nxt == "month":
            return ContractMonthEvidence.FRONT, None
        if (w in ("second", "deferred", "back", "next") and nxt in ("month", "contract")
                and (w != "next" or (j + 2 < len(words) and words[j + 2] in ("contract", "futures")) or nxt == "contract")) \
                or w in ("second-month", "next-month", "deferred"):
            return ContractMonthEvidence.NEXT, None
        mw = IT.MONTH_WORD_RE.fullmatch(t.token.lower())
        if mw:
            mon = _MONTH3[mw.group("word")[:3].lower()]
            yearish = bool(re.fullmatch(r"'?(?:20)?\d{2}", nxt))
            contractish = nxt in ("contract", "futures") or yearish
            if w.rstrip(".") in IT.ENGLISH_WORDS and not contractish:
                continue
            if prev in TIME_PREPS and not contractish:
                continue
            if t.n is None and j + 1 < len(toks) and toks[j + 1].value is not None and not yearish \
                    and toks[j + 1].value == toks[j + 1].value.to_integral_value() and 1 <= toks[j + 1].value <= 31 \
                    and nxt not in ("at",):
                continue   # "Dec 5": a date
            return ContractMonthEvidence.NAMED, mon
    return None


def read_price_roles(text: str | None, units: list[ClauseUnit] | tuple = (),
                     to_norm: Callable[[int], int] | None = None) -> list[PriceReading]:
    """The structured price readings of a post. ``units`` and ``to_norm`` come from the interpreter
    (its clauses and the map from a raw text offset to its normalised offset); without them the
    readings carry no action."""
    toks = _tokens(text or "")
    if not any(t.n is not None for t in toks):
        return []
    # segments and sentences
    seg_id, sent_id = [], []
    s = g = 0
    for t in toks:
        seg_id.append(g)
        sent_id.append(s)
        if t.seg_end:
            g += 1
        if t.sent_end:
            s += 1
    first_of_sent = {}
    for t in toks:
        first_of_sent.setdefault(sent_id[t.i], t.i)
    trading_ids = list(dict.fromkeys(u.action_id for u in units if u.trading and u.action_id))
    results: dict[int, dict] = {}
    last_price_tok: dict[int, int] = {}      # sentence -> token index of its last price number so far
    for t in toks:
        if t.n is None:
            continue
        si, gi = sent_id[t.i], seg_id[t.i]
        floor = last_price_tok.get(si, -1)
        seg_start = next(k for k in range(t.i, -1, -1) if k == 0 or seg_id[k - 1] != gi)
        lo = max(seg_start, floor + 1)
        left_toks = [x for x in toks[lo:t.i] if x.n is None or results.get(x.n, {}).get("role") == PriceReadingRole.NOT_A_PRICE]
        if not left_toks and seg_start > floor + 1 and seg_start > 0 and sent_id[seg_start - 1] == si:
            prev_seg = seg_id[seg_start - 1]   # "Stop: 86.00", "stop at yesterday's low, 69.80": a label
            left_toks = [x for x in toks[max(floor + 1, 0):seg_start] if seg_id[x.i] == prev_seg and x.n is None]
        sent_start = first_of_sent[si]
        ext_toks = [x for x in toks[max(sent_start, floor + 1):lo] if x.n is None]
        all_before = [x for x in toks[sent_start:t.i] if x.n is None]
        # the words after: to the next number in the segment, plus the next segment when it has no number
        right_toks = []
        k = t.i + 1
        while k < len(toks) and seg_id[k] == gi and toks[k].n is None and sent_id[k] == si:
            right_toks.append(toks[k])
            k += 1
        if k < len(toks) and seg_id[k] != gi and sent_id[k] == si and not t.sent_end:
            nxt_seg = [x for x in toks[k:] if seg_id[x.i] == seg_id[k]]
            if all(x.n is None for x in nxt_seg):
                right_toks += nxt_seg
        left = _words(left_toks)
        holder = any(w in HOLDER_WORDS for w in _words(all_before))
        sentence_initial = bool(left_toks) and left_toks[0].i == sent_start
        cues = _left_cues(left, sentence_initial, holder)
        ext_cues = _left_cues(_words(ext_toks), bool(ext_toks) and ext_toks[0].i == sent_start, holder)
        right = _right_cues(_words(right_toks))
        first_after = right_toks[0].word if right_toks and not t.seg_end else ""
        prev_price = results.get(_prev_price_n(results, toks, t, sent_id))
        res = _decide(toks, t, left, cues, ext_cues, _words(all_before), right, first_after, prev_price,
                      prev_price is not None and seg_id[prev_price["tok"].i] == gi, units, to_norm)
        res["tok"] = t
        results[t.n] = res
        if res["role"] != PriceReadingRole.NOT_A_PRICE:
            last_price_tok[si] = t.i
    # association, contract month, other month
    readings: list[PriceReading] = []
    unit_of: dict[int, ClauseUnit | None] = {}
    for n, r in results.items():
        unit_of[n] = _unit_for(r["tok"], units, to_norm)
    month_of: dict[int, tuple] = {}
    action_month: dict[str, tuple] = {}
    for n, r in results.items():
        t = r["tok"]
        seg = [x for x in toks if seg_id[x.i] == seg_id[t.i]]
        sen = [x for x in toks if sent_id[x.i] == sent_id[t.i]]
        ev = _month_evidence(seg)
        where = "MONTH_NAMED_IN_SEGMENT"
        if ev is None:
            ev, where = _month_evidence(sen), "MONTH_NAMED_IN_SENTENCE"
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
                action_id, assoc = trading_ids[0], "SOLE_ACTION_IN_POST"
        month, named = ContractMonthEvidence.NOT_STATED, None
        applicable = role != PriceReadingRole.NOT_A_PRICE
        unit_for_month = u if u is not None else next((x for x in units if x.action_id == action_id), None)
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
    return readings


TRADE_LEVEL_ROLES = frozenset((PriceReadingRole.INTENDED_ENTRY, PriceReadingRole.HISTORICAL_FILL, PriceReadingRole.STOP,
                               PriceReadingRole.TARGET, PriceReadingRole.AVERAGE_OR_COST_BASIS))


def _plain(v: Decimal | None) -> str:
    return "" if v is None else format(v, "f")


def _prev_price_n(results, toks, t, sent_id):
    for k in range(t.i - 1, -1, -1):
        x = toks[k]
        if sent_id[k] != sent_id[t.i]:
            return None
        if x.n is not None and results.get(x.n, {}).get("role") != PriceReadingRole.NOT_A_PRICE:
            return x.n
    return None


def _unit_for(t: _Tok, units, to_norm) -> ClauseUnit | None:
    if not units or to_norm is None:
        return None
    p = to_norm(t.start)
    inside = [u for u in units if u.start <= p < u.end]
    return inside[0] if inside else None


def _nearest(cues: list[_Cue], cats) -> _Cue | None:
    hits = [c for c in cues if c.cat in cats]
    return min(hits, key=lambda c: c.d) if hits else None


ROLE_CATS = {"STOP": PriceReadingRole.STOP, "TARGET": PriceReadingRole.TARGET,
             "LEVEL": PriceReadingRole.CHART_LEVEL, "AVERAGE": PriceReadingRole.AVERAGE_OR_COST_BASIS,
             "MARKET": PriceReadingRole.CURRENT_MARKET}
ROLE_REASON = {"STOP": "STOP_WORD", "TARGET": "TARGET_WORD", "LEVEL": "LEVEL_WORD", "AVERAGE": "AVERAGING_WORD",
               "MARKET": "MARKET_WORD"}
DECISIVE_TENSE = ("OWN_PAST", "PLAN", "MOVE_PAST")


def _decide(toks, t, left, cues, ext_cues, before_all, right, first_after, prev_price, same_seg_prev,
            units, to_norm) -> dict:
    R = PriceReadingRole
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

    def done(role, *reasons):
        return {"role": role, "reasons": list(reasons), "exactness": exact, "between": att == "between"}

    nxt_word = toks[t.i + 1].word if t.i + 1 < len(toks) else ""
    if nxt_word in STRIKE_WORDS or att == "strike":
        return done(R.UNRESOLVED, "OPTION_STRIKE")
    # 1. a role word written right after the number ("75 target", "2,450 resistance")
    if first_after in STOP_NOUNS:
        return done(R.STOP, "ROLE_WORD_AFTER_NUMBER")
    if first_after in TARGET_NOUNS:
        return done(R.TARGET, "ROLE_WORD_AFTER_NUMBER")
    if first_after in LEVEL_NOUNS - {"top", "bottom", "level", "levels"} or first_after in ("level", "levels"):
        return done(R.CHART_LEVEL, "ROLE_WORD_AFTER_NUMBER")
    if first_after in AVERAGE_NOUNS - {"cost"}:
        return done(R.AVERAGE_OR_COST_BASIS, "ROLE_WORD_AFTER_NUMBER")
    # 2. a chain of numbers ("from 2,440 to 2,425", "targets 75 and 78", "75, 78") shares one role
    if prev_price is not None and prev_price["role"] not in (R.UNRESOLVED,) and \
            ((same_seg_prev and left and all(w in CHAIN_WORDS for w in left)) or (not same_seg_prev and not left)):
        return done(prev_price["role"], "SAME_ROLE_AS_PREVIOUS_NUMBER", *prev_price["reasons"][:1])
    role_cue = _nearest(cues, ROLE_CATS)
    tense = _nearest(cues, DECISIVE_TENSE)
    # 3. a role noun of the number's own segment, unless a decisive verb is nearer to the number
    if role_cue is not None and (tense is None or role_cue.d < tense.d):
        if role_cue.cat == "LEVEL":
            st = [c for c in cues if c.cat in ("STOP", "TARGET") and c.d > role_cue.d]
            if st:
                far = max(st, key=lambda c: c.d)
                between = left[len(left) - far.d + 1:len(left) - role_cue.d]
                if any(w in RELATIVE_PREPS for w in between):
                    return done(R.CHART_LEVEL, "STOP_RELATIVE_TO_LEVEL")
                return done(ROLE_CATS[far.cat], "STOP_AT_LEVEL")
        if role_cue.cat == "MARKET" and ("PAST" in right or any(c.cat == "PAST" for c in cues)):
            return done(R.UNRESOLVED, "PAST_MARKET_PRICE")
        return done(ROLE_CATS[role_cue.cat], ROLE_REASON[role_cue.cat])
    cond = any(c.cat == "CONDITION" for c in cues + ext_cues) or any(w in CONDITION_WORDS for w in before_all)
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
    fill, order, market = [], [], []
    if t_cat == "OWN_PAST":
        fill.append("OWN_EXECUTION_PAST")
    if "FILL" in right or any(c.cat == "FILL" for c in cues):
        fill.append("FILL_WORD")
    if not fill and t_cat is None and skipped[:1] == ["from"] and any(w in ("long", "short", "in") for w in left) \
            and any(w in HOLDER_WORDS for w in before_all):
        fill.append("POSITION_HELD_FROM")
    unit = _unit_for(t, units, to_norm)
    if not fill and t_cat is None and unit is not None and unit.past_execution and skipped[:1] in (["at"], ["@"], ["from"]):
        fill.append("CLAUSE_PAST_EXECUTION")
    order_words = "ORDER" in right or any(c.cat == "ORDER" for c in cues)
    dip = "DIP" in right or any(c.cat == "DIP" for c in cues)
    if t_cat == "PLAN":
        order.append("PLANNED_TRANSACTION")
    if order_words:
        order.append("ORDER_WORD")
    if dip and not order:
        order.append("PLANNED_TRANSACTION")
    if not order and cond and action_ctx is not None and skipped[:1] in (["at"], ["@"]):
        order.append("CONDITIONAL_PLAN")
    if att in MARKET_STATE_ADVERBS or att in MARKET_STATE_VERBS or first_after == "now":
        market.append("MARKET_STATE_WORD")
    elif skipped[:1] in (["at"], ["@"]) and "with" in left[-5:] and \
            all(w not in STOP_NOUNS | TARGET_NOUNS | LEVEL_NOUNS | AVERAGE_NOUNS | INDICATOR_NOUNS
                for w in left[len(left) - 1 - left[::-1].index("with"):]):
        market.append("WITH_MARKET_AT")
    past = "PAST" in right or any(c.cat == "PAST" for c in cues)
    if fill:
        if order and t_cat == "PLAN":
            return done(R.UNRESOLVED, "CONFLICTING_CUES")
        if order_words and not dip:
            return done(R.HISTORICAL_FILL, *fill, "FILLED_ORDER")
        return done(R.HISTORICAL_FILL, *fill)
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


def safe_read_price_roles(text: str | None, units=(), to_norm=None) -> list[PriceReading]:
    """``read_price_roles`` that can never raise: the readings are advisory and must not disturb the
    interpreter. A fault yields one UNRESOLVED reading per number (READER_FAULT)."""
    try:
        return read_price_roles(text, units, to_norm)
    except Exception:  # noqa: BLE001
        try:
            return [PriceReading(n=x["n"], value="", role=PriceReadingRole.UNRESOLVED, exactness=Exactness.EXACT,
                                 contract_month=ContractMonthEvidence.NOT_STATED, reasons=["READER_FAULT"])
                    for x in post_numbers(text)]
        except Exception:  # noqa: BLE001
            return []
