"""Contract terms stated in one instruction clause: a futures contract month, an option's expiry,
strike and right.

Deterministic and conservative. It only reads what the clause states explicitly; it never infers a
month, an expiry or a strike. The mapping policy (runtime/mapping.py) decides what a missing or
conflicting term means (HOLD). A contract month is recognised as a month name with a two- or
four-digit year, with or without an apostrophe ("Dec 2026", "Dec '26", "Dec26"), a numeric year
and month ("2026-12", "12/2026", "202612"), a futures month code with a one- or two-digit year
("QIFZ6", "Z26"), or a month name followed by "contract" ("the Dec contract"). Anything else that
looks like a month or a contract code is kept as unrecognised, so the policy can HOLD on it rather
than fall back to a default month. An option expiry is recognised only as an ISO date
("2026-12-18"); a strike only directly before the right ("105 call", "105 strike put").
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from enum import Enum

_MONTH_NAMES = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
# Standard futures month codes.
_CODE_MONTH = {c: i + 1 for i, c in enumerate("FGHJKMNQUVXZ")}
_MON = (
    r"(?P<mon>jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|"
    r"sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?"
)
# A month name, an optional apostrophe, and a four- or two-digit year ("Sep 2026", "Sep '26").
MONTH_YEAR_RE = re.compile(
    rf"\b{_MON}\s*(?:['\u2018\u2019]\s*)?(?P<year>20\d{{2}}|\d{{2}})(?!\d|[.,:/-]\d)",
    re.IGNORECASE,
)
# Every month word. "may" and "march" are ordinary English words too: they count as a month only
# in one of the recognised forms above.
MONTH_WORD_RE = re.compile(
    r"\b(?P<word>jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|"
    r"sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\b\.?",
    re.IGNORECASE,
)
ENGLISH_WORDS = frozenset({"may", "march"})
# Year and month as numbers: 2026-09, 2026/09, 09/2026, 202609. A full ISO date is an expiry; the
# futures selection reads its month too.
NUMERIC_MONTH_RE = re.compile(
    r"(?<![\d./-])(?:(?P<y1>20\d{2})[-/](?P<m1>\d{1,2})(?!\d|[/.-]\d)"
    r"|(?P<m2>\d{1,2})/(?P<y2>20\d{2})(?!\d|[/.-]\d)"
    r"|(?P<y3>20\d{2})(?P<m3>\d{2})(?!\d|[./-]\d))"
)
# A futures month code with a one- or two-digit year, with or without its root: QIFU6, ESZ26, U6.
CONTRACT_CODE_RE = re.compile(
    r"(?<![\w$])(?P<root>[A-Z]{0,4}?)(?P<code>[FGHJKMNQUVXZ])(?P<yy>\d{1,2})(?!\w)"
)
# Upper-case words: one that extends the execution contract's root ("QIFU") is a contract code
# the policy could not read.
UPPER_WORD_RE = re.compile(r"(?<![\w$])[A-Z][A-Z0-9]{1,7}(?!\w)")
MONTH_CONTRACT_RE = re.compile(rf"\b{_MON}\s+(?:futures?\s+)?contract\b", re.IGNORECASE)
# An equity index whose own name carries a number ("S&P 500", "Nasdaq 100", "Russell 2000"): the
# number is part of the market's name, never a contract term, a level or a count.
INDEX_NAME_RE = re.compile(
    r"(?<![\w$])(?:s&p|nasdaq|russell|dow(?:\s+jones)?|ftse|nikkei|dax|(?:euro\s+)?stoxx|cac|asx)"
    r"\s?-?\s?(?:500|400|600|100|1000|2000|30|225|40|50|200)(?![\d.,]\d|\d)",
    re.IGNORECASE,
)
# What may follow a root in a contract code: a month code with an optional year (QIFU, QIFU6), a
# month name (QIFDEC), or anything that carries a digit (QIFX7Z).
_CODE_TAIL_RE = re.compile(
    r"[FGHJKMNQUVXZ]\d{0,2}|\w*\d\w*|(?:JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)\w*"
)


def code_shaped(word: str, root: str) -> bool:
    """Is the upper-case ``word`` a contract code of ``root`` that the policy could not read? Only a
    root followed by a month code, a month name or a digit is: an ordinary word that merely starts
    with the root's letters ("CLOSE" for a root CL, "STOP" for STO) is not."""
    return (
        bool(root) and word.startswith(root) and word != root
        and _CODE_TAIL_RE.fullmatch(word[len(root):]) is not None
    )
ISO_DATE_RE = re.compile(r"(?<![\d-])(?P<y>20\d{2})-(?P<m>\d{2})-(?P<d>\d{2})(?![\d-])")
STRIKE_RIGHT_RE = re.compile(
    r"(?<![\w.$-])\$?(?P<strike>\d+(?:\.\d+)?)\s*(?:strike\s+)?(?P<right>call|put)s?\b",
    re.IGNORECASE,
)
RIGHT_RE = re.compile(r"\b(?P<right>call|put)s?\b", re.IGNORECASE)
# A compact strike and right: 105C, 92.5P (review F7). It is option language on its own.
COMPACT_STRIKE_RE = re.compile(r"(?<![\w.$-])\$?(?P<strike>\d+(?:\.\d+)?)(?P<right>[CP])\b",
                               re.IGNORECASE)
# Words naming a multi-leg option strategy (review F3). Any of them in an option instruction HOLDs.
STRATEGY_RE = re.compile(
    r"\b(?P<word>spread|vertical|calendar|diagonal|straddle|strangle|condor|butterfl(?:y|ies)|"
    r"collar|ratio|covered|combo|fly|flies|reversal)s?\b",
    re.IGNORECASE,
)
# The ones that only ever name options: they route an instruction to its option family even
# without the words call, put or option.
OPTION_ONLY_STRATEGY_RE = re.compile(
    r"\b(?:straddles?|strangles?|condors?|butterfl(?:y|ies)|iron\s+fl(?:y|ies)|risk\s+reversals?|"
    r"(?:vertical|calendar|diagonal|ratio|credit|debit)\s+spreads?)\b",
    re.IGNORECASE,
)
# Two strikes written together before a right or a strategy word: 100/105, 100-105, 105 and 110.
STRIKE_PAIR_RE = re.compile(
    r"(?<![\w.$/-])\$?\d+(?:\.\d+)?\s*(?:/|-|,|&|and|to)\s*\$?\d+(?:\.\d+)?\s*"
    r"(?:strikes?\s+)?(?:calls?|puts?|spreads?|straddles?|strangles?|strikes?)\b",
    re.IGNORECASE,
)
OPTION_LANGUAGE_RE = re.compile(r"\b(?:calls?|puts?|options?|strike)\b", re.IGNORECASE)
# Verb and idiom uses of "call" and "put" (an instruction to put a stop in, to call it a day):
# removed before the words are read as option rights. A bare noun use that remains with no
# strike, expiry or the word option is unclear, and still routes an OPEN to the option family.
IDIOM_RE = re.compile(
    r"\bput(?:s|ting)?\s+(?:a|an|our|my|your|on|in|it|them|us|up|down|out|back|stops?)\b"
    r"|\bput\s+the\s+(?:stop|proceeds|position|trade)\b"
    r"|\bcall(?:s|ing)?\s+(?:it|me|us|them|this|that|for)\b"
    r"|\bclose\s+calls?\b",
    re.IGNORECASE,
)


@dataclass
class InstructionTerms:
    # Contract months named with their year, as YYYYMM.
    months: list[str] = field(default_factory=list)
    # Months named without a year ("the Dec contract"), as month numbers.
    bare_months: list[int] = field(default_factory=list)
    option_language: bool = False
    expiries: list[date] = field(default_factory=list)
    strikes: list[Decimal] = field(default_factory=list)
    rights: list[str] = field(default_factory=list)  # CALL | PUT

    # Futures month codes as written: (token, root, month number, year digits). The policy resolves
    # the year (one digit: the next year ending in it) and checks the root against its contract.
    month_codes: list[tuple[str, str, int, str]] = field(default_factory=list)
    # Tokens that look like a contract month or code but do not parse ("Sep", "2026-13").
    unrecognised_months: list[str] = field(default_factory=list)
    upper_words: list[str] = field(default_factory=list)
    # Why the instruction is not a single-leg option (several strikes, expiries or rights, a
    # strategy word). Empty for a single leg.
    multi_leg: list[str] = field(default_factory=list)
    # The clause and the spans of it read as a contract month (month and year, numeric month,
    # month code, "the Sep contract", an ISO date): the leftover-token rule skips those.
    clause: str = ""
    month_spans: list[tuple[int, int]] = field(default_factory=list)
    # Spans naming an index whose name carries a number ("S&P 500"): skipped by that rule too.
    name_spans: list[tuple[int, int]] = field(default_factory=list)

    @property
    def names_a_month(self) -> bool:
        return bool(self.months or self.bare_months or self.month_codes
                    or self.unrecognised_months)


def extract_terms(clause: str | None) -> InstructionTerms:
    out = InstructionTerms()
    if not clause:
        return out
    out.clause = clause
    recognised: list[tuple[int, int]] = []  # spans of month words read as a month

    def add_month(year: int, month: int, token: str) -> None:
        if not 1 <= month <= 12:
            out.unrecognised_months.append(token)
            return
        value = f"{year}{month:02d}"
        if value not in out.months:
            out.months.append(value)

    for m in MONTH_YEAR_RE.finditer(clause):
        year = m.group("year")
        year_n = int(year) if len(year) == 4 else 2000 + int(year)
        add_month(year_n, _MONTH_NAMES[m.group("mon")[:3].lower()], m.group(0))
        recognised.append(m.span("mon"))
        out.month_spans.append(m.span())
    for m in MONTH_CONTRACT_RE.finditer(clause):
        n = _MONTH_NAMES[m.group("mon")[:3].lower()]
        if n not in out.bare_months:
            out.bare_months.append(n)
        recognised.append(m.span("mon"))
        out.month_spans.append(m.span())
    for m in MONTH_WORD_RE.finditer(clause):
        inside = any(a <= m.start("word") < b for a, b in recognised)
        if not inside and m.group("word").lower() not in ENGLISH_WORDS:
            out.unrecognised_months.append(m.group(0))
    for m in NUMERIC_MONTH_RE.finditer(clause):
        y = m.group("y1") or m.group("y2") or m.group("y3")
        mo = m.group("m1") or m.group("m2") or m.group("m3")
        add_month(int(y), int(mo), m.group(0))
        out.month_spans.append(m.span())
    for m in CONTRACT_CODE_RE.finditer(clause):
        out.month_spans.append(m.span())
        out.month_codes.append(
            (m.group(0), m.group("root"), _CODE_MONTH[m.group("code")], m.group("yy")))
    out.name_spans = [m.span() for m in INDEX_NAME_RE.finditer(clause)]
    out.upper_words = UPPER_WORD_RE.findall(clause)
    words = IDIOM_RE.sub(" ", clause)  # call and put as nouns only
    out.option_language = (OPTION_LANGUAGE_RE.search(words) is not None
                           or OPTION_ONLY_STRATEGY_RE.search(clause) is not None
                           or COMPACT_STRIKE_RE.search(clause) is not None)
    for m in ISO_DATE_RE.finditer(clause):
        out.month_spans.append(m.span())
        try:
            d = date(int(m.group("y")), int(m.group("m")), int(m.group("d")))
        except ValueError:
            continue
        if d not in out.expiries:
            out.expiries.append(d)
    for m in STRIKE_RIGHT_RE.finditer(clause):
        try:
            strike = Decimal(m.group("strike"))
        except InvalidOperation:
            continue
        if strike > 0 and strike not in out.strikes:
            out.strikes.append(strike)
    for m in RIGHT_RE.finditer(words):
        right = m.group("right").upper()
        if right not in out.rights:
            out.rights.append(right)
    for m in COMPACT_STRIKE_RE.finditer(clause):
        strike = Decimal(m.group("strike"))
        right = "CALL" if m.group("right").upper() == "C" else "PUT"
        if strike > 0 and strike not in out.strikes:
            out.strikes.append(strike)
        if right not in out.rights:
            out.rights.append(right)
    for m in STRATEGY_RE.finditer(clause):
        word = m.group("word").lower()
        word = "butterfly" if word.startswith("butterfl") else word
        if f"STRATEGY_WORD:{word}" not in out.multi_leg:
            out.multi_leg.append(f"STRATEGY_WORD:{word}")
    if len(out.strikes) > 1 or STRIKE_PAIR_RE.search(clause):
        out.multi_leg.append("SEVERAL_STRIKES")
    if len(out.expiries) > 1:
        out.multi_leg.append("SEVERAL_EXPIRIES")
    if len(out.rights) > 1:
        out.multi_leg.append("SEVERAL_RIGHTS")
    return out


_TOKEN_RE = re.compile(r"\S+")
_EDGE = "()[]{}.,;:!?\"'\u2018\u2019"
_NUMBER_RE = re.compile(r"^\$?(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?(%?)$")


def leftover_tokens(terms: InstructionTerms, consumed: set[Decimal],
                    fractions: set[Decimal], roots: set[str]) -> list[str]:
    """What the clause still names after its contract months and the action's own levels
    (review F2, confirmation): any token with a digit that is not a consumed level (``consumed``)
    or a consumed reduction percentage (``fractions`` x 100), any month name, and any token shaped
    like a contract code for ``roots`` (case-insensitive; ``code_shaped``). The digits of an index's
    own name ("S&P 500") are part of the market's name and are skipped. A futures selection may fall
    back to its nearest-contract rule only when this is empty; otherwise the clause names something
    the bot has not understood."""
    out = []
    upper_roots = {r.upper() for r in roots if r}
    for m in _TOKEN_RE.finditer(terms.clause):
        raw = m.group(0)
        start = m.start() + (len(raw) - len(raw.lstrip(_EDGE)))
        token = raw.strip(_EDGE)
        if not token:
            continue
        end = start + len(token)
        if any(a <= start and end <= b for a, b in (*terms.month_spans, *terms.name_spans)):
            continue
        word = token.upper().lstrip("$")
        if any(ch.isdigit() for ch in token):
            n = _NUMBER_RE.match(token)
            if n:
                value = Decimal((n.group(1) + (n.group(2) or "")).replace(",", ""))
                if (n.group(3) and value / 100 in fractions) or (not n.group(3)
                                                                and value in consumed):
                    continue
            out.append(token)
        else:
            month = MONTH_WORD_RE.fullmatch(token) and token.lower().rstrip(".") not in ENGLISH_WORDS
            code = any(code_shaped(word, r) for r in upper_roots)
            if month or code:
                out.append(token)
    return out


def clause_numbers(clause: str) -> set[Decimal]:
    """Every plain number written in the clause as a price could be ("4,512.25", "$88.4"), outside
    the spans of an index's own name: where a stated level must appear to anchor an entry."""
    names = [m.span() for m in INDEX_NAME_RE.finditer(clause or "")]
    out = set()
    for m in _TOKEN_RE.finditer(clause or ""):
        raw = m.group(0)
        start = m.start() + (len(raw) - len(raw.lstrip(_EDGE)))
        token = raw.strip(_EDGE).lstrip("~")  # "~41.20 now": the market, about
        if any(a <= start < b for a, b in names):
            continue
        n = _NUMBER_RE.match(token)
        if n and not n.group(3):
            out.add(Decimal((n.group(1) + (n.group(2) or "")).replace(",", "")))
    return out


# ------------------------------------------------------------------ entry anchor evidence
#
# entry_rule = REFERENCE_AS_COLLAR (docs/MULTI_ASSET_MAPPING.md, "What makes a level an anchor").
# A stated number is the market's level at the time of the post only with POSITIVE evidence next
# to it: one of these present-market markers, written directly before it ("now 41.20", "trading at
# 41.20"; "now at 41.20" too) or directly after it ("41.20 now"). The list is closed and generic on
# purpose; a publisher's own phrasing is added only through the private lexicon extension, never
# here. Anything else is not an anchor (default HOLD).
PRESENT_MARKERS: tuple[str, ...] = (
    "now", "right now", "currently", "here at", "trading at", "at the market",
)
# Any of these words in the number's own clause makes the number something other than the market
# now: a time other than now, a chart or order level, a condition, a range or a comparison. A
# marker never overrides them, and no marker or alias may contain one.
# a time other than now
TIME_WORDS = frozenset((
    "yesterday", "today", "tonight", "tomorrow", "earlier", "later", "ago", "before", "after",
    "previously", "previous", "prior", "last", "next", "was", "were", "been", "had", "will",
    "would", "could", "should", "might", "shall", "going", "gonna", "morning", "afternoon",
    "evening", "overnight", "weekend", "week", "weeks", "monday", "tuesday", "wednesday",
    "thursday", "friday", "saturday", "sunday", "premarket", "pre", "postmarket", "hours",
    "session", "yesterdays",
))
# a chart, session or order level; the first group is a protective or profit level
STOP_TARGET_WORDS = frozenset((
    "target", "targets", "objective", "objectives", "tp", "stop", "stops", "sl",
))
LEVEL_WORDS = STOP_TARGET_WORDS | frozenset((
    "high", "highs", "low", "lows", "open", "opening", "opened", "close", "closing", "closed",
    "settle", "settled", "settlement", "resistance", "support", "level", "levels", "pivot", "zone",
    "area", "entry", "exit",
))
NOT_CURRENT_WORDS = TIME_WORDS | LEVEL_WORDS | frozenset((
    # a condition or a level to cross
    "from", "to", "above", "below", "over", "under", "through", "break", "breaks", "broke",
    "breakout", "breakdown", "if", "when", "once", "until", "unless", "whenever",
    # a range or a comparison
    "between", "range", "vs", "versus", "than", "higher", "lower", "up", "down", "more",
    "less", "or",
    "<", ">", "<=", ">=", "\u2264", "\u2265",
))
# A currency or unit may stand on a level line beside the number.
UNIT_WORDS = frozenset(("usd", "us$", "$", "dollar", "dollars", "pt", "pts", "point", "points",
                        "tick", "ticks", "bbl", "oz", "mmbtu", "cents"))
_SEGMENT_RE = re.compile(r"[;!?\n]|[.,:](?!\d)|\s[-–—]\s")
_PHRASE_RE = re.compile(r"^[a-z][a-z&']*(?: [a-z][a-z&']*)*$")


def _word(token: str) -> str:
    w = token.strip(_EDGE).lower()
    return w.removesuffix("'s")


def _value(token: str) -> Decimal | None:
    t = token.strip(_EDGE).lstrip("~")
    n = _NUMBER_RE.match(t)
    if not n or n.group(3):
        return None
    return Decimal((n.group(1) + (n.group(2) or "")).replace(",", ""))


def check_phrase(phrase: str, what: str, max_words: int) -> str:
    """A marker or alias: one to ``max_words`` words of letters, none a disqualifying word."""
    p = " ".join(phrase.lower().split())
    if not _PHRASE_RE.match(p) or len(p) > 40 or len(p.split()) > max_words:
        raise ValueError(f"{what} {phrase!r} must be one to {max_words} words of letters")
    bad = set(p.split()) & NOT_CURRENT_WORDS
    if bad:
        raise ValueError(f"{what} {phrase!r} contains {min(bad)!r}, which disqualifies a level")
    return p


def anchor_not_current(text: str, value: Decimal, markers: tuple[str, ...]) -> list[str]:
    """Why ``value``, as written in ``text`` (the clause or line it is taken from), is not the
    market's level at the time of the post. Every place it is written is checked: its own clause
    (the part of ``text`` between punctuation) must carry no disqualifying word
    (``NOT_CURRENT_WORDS``) and must attach a present-market marker to it. Empty when it may be
    an anchor."""
    out: list[str] = []
    places = 0
    phrases = [m.split() for m in markers]
    for seg in _SEGMENT_RE.split(text or ""):
        tokens = seg.split()
        hits = [i for i, t in enumerate(tokens) if _value(t) == value]
        if not hits:
            continue
        places += 1
        words = [_word(t) for t in tokens]
        parts = {p for w in words for p in (w, *w.split("-"), *w.split("/")) if p}
        bad = sorted(parts & NOT_CURRENT_WORDS)
        out += [f"NOT_CURRENT_WORD:{w}" for w in bad if f"NOT_CURRENT_WORD:{w}" not in out]
        for i in hits:
            if not any(_attached(words, i, m) for m in phrases):
                out.append("NO_PRESENT_MARKER")
                break
    if not places:
        out.append("NO_PRESENT_MARKER")
    return out


# The anchor's context window is an allow-list (confirmation review). Besides the markers, the
# family's own names and the action's own instruction words, only these may stand there.
CONNECTOR_WORDS = frozenset(("at", "is", "are", "around", "near", "here", "the", "a", "it", "@"))
# The action's own instruction words, by side and kind: the words the interpreter reads as this
# action's side and verb.
SIDE_WORDS = {"LONG": ("long", "buy", "buying"),
              "SHORT": ("short", "shorting", "sell", "selling")}
ADD_WORDS = ("add", "adding")


def _tag(words: list[str], phrases, tag: str, tags: list[str | None]) -> None:
    for phrase in sorted({p for p in phrases if p}, key=lambda p: -len(p.split())):
        pw = phrase.split()
        n = len(pw)
        for k in range(len(words) - n + 1):
            if words[k:k + n] == pw and all(t is None for t in tags[k:k + n]):
                tags[k:k + n] = [tag] * n


# The final rule (last review pass): the scope of a marked number is its whole SENTENCE. Only a
# full stop (not a decimal point), an exclamation mark, a question mark or a line break ends one;
# commas, colons, semicolons, dashes, slashes and brackets do not. Besides markers, connectors,
# units, the family's names and the action's own words, only these generic function words of a
# plain instruction may stand in it. Publisher phrasing is added only by the private lexicon.
# Final review: no preposition that can make the number a goal, a condition, a comparison or the
# price of something else ("for", "to", "on", "of", "with", "in") is on the list.
FUNCTION_WORDS = frozenset((
    "we", "i", "are", "am", "is", "and", "our", "my", "a", "the", "this", "new", "position",
    "positions", "trade",
))
_SENTENCE_END_RE = re.compile(r"\n|[!?]|\.(?!\d)")


def sentences(text: str) -> list[tuple[str, bool]]:
    """The sentences of ``text`` and whether each ends in a question mark."""
    out, start = [], 0
    for m in [*_SENTENCE_END_RE.finditer(text or ""), None]:
        end = m.start() if m else len(text or "")
        s = (text or "")[start:end].strip()
        if s:
            out.append((s, bool(m and m.group(0) == "?")))
        start = m.end() if m else end
    return out


def unknown_words(sentence: str, allowed, skip_spans: list[tuple[int, int]] = ()) -> int:
    return len(unknown_tokens(sentence, allowed, skip_spans))


def unknown_tokens(sentence: str, allowed, skip_spans: list[tuple[int, int]] = ()) -> list[str]:
    """How many alphabetic tokens of ``sentence`` are not on the allow-list: ``allowed`` phrases
    (markers, the family's names, the action's own words, private additions; lower case), the
    connectors, the function words and the units. Tokens with a digit are left to the number
    check, ``skip_spans`` (a future's contract months) to the contract check."""
    seg = [(t.start(), t.end(), t.group(0)) for t in _TOKEN_RE.finditer(sentence or "")]
    words = [_word(tok).lstrip("~").lstrip("$") for _, _, tok in seg]
    tags: list[str | None] = [None] * len(words)
    _tag(words, allowed, "allowed", tags)
    unknown = []
    for (a, b, tok), w, tag in zip(seg, words, tags, strict=True):
        if tag is not None or any(x <= a and b <= y for x, y in skip_spans):
            continue
        if not any(ch.isalpha() for ch in w) or any(ch.isdigit() for ch in w):
            continue
        if w in CONNECTOR_WORDS or w in FUNCTION_WORDS or w in UNIT_WORDS:
            continue
        unknown.append(w)
    return unknown


def marked_numbers(text: str, markers) -> set[Decimal]:
    """Every number in ``text`` with a present-market marker attached to it."""
    phrases = [m.split() for m in markers]
    out = set()
    for seg in _SEGMENT_RE.split(text or ""):
        tokens = seg.split()
        words = [_word(t) for t in tokens]
        for i, tok in enumerate(tokens):
            v = _value(tok)
            if v is not None and any(_attached(words, i, m) for m in phrases):
                out.add(v)
    return out


def _attached(words: list[str], i: int, marker: list[str]) -> bool:
    n = len(marker)
    if words[i + 1:i + 1 + n] == marker:
        return True
    if i >= n and words[i - n:i] == marker:
        return True
    return marker[-1] != "at" and i >= n + 1 and words[i - 1] == "at" \
        and words[i - 1 - n:i - 1] == marker


def foreign_words(text: str, value: Decimal, allowed: set[str],
                  skip_spans: list[tuple[int, int]] = ()) -> list[str]:
    """The words of a separate level line (or attached quote) that are not this family's: every
    alphabetic token must be part of a present-market marker, a currency or unit, or a name or
    alias of this family (``allowed``: lower-case phrases, markers included). ``skip_spans`` are
    contract-month spans the contract check reads instead."""
    low = (text or "").lower()
    keep = [True] * len(low)
    for a, b in skip_spans:
        for k in range(a, min(b, len(low))):
            keep[k] = False
    masked = "".join(ch if k else " " for ch, k in zip(low, keep, strict=True))
    for phrase in sorted(allowed, key=len, reverse=True):
        masked = re.sub(rf"(?<![\w$]){re.escape(phrase)}(?![\w])", " ", masked)
        masked = re.sub(rf"\${re.escape(phrase)}(?![\w])", " ", masked)
    out = []
    for token in masked.split():
        if _value(token) is not None:
            continue
        w = _word(token).lstrip("~")
        if not w or w in UNIT_WORDS or not any(ch.isalpha() for ch in w):
            continue
        if w.startswith("$") and w[1:] in UNIT_WORDS:
            continue
        out.append(w)
    return out


# A contract described rather than named: which one is meant is the publisher's, not the bot's.
RELATIVE_CONTRACT_RE = re.compile(
    r"(?<![\w$])(?P<word>back|next|following|second|2nd|deferred|far|nearby|front|spot\s+month|"
    r"calendar|spreads?|roll|rolls|rolled|rolling|rollover)(?!\w)",
    re.IGNORECASE,
)


def relative_contract_words(terms: InstructionTerms) -> list[str]:
    """Relative or descriptive contract words in the clause, outside a recognised contract month
    ("the Aug contract", "Dec 2026"). Any one HOLDs a futures selection."""
    out = []
    for m in RELATIVE_CONTRACT_RE.finditer(terms.clause):
        if any(a <= m.start() < b for a, b in terms.month_spans):
            continue
        w = " ".join(m.group("word").lower().split()).replace(" ", "_")
        if w not in out:
            out.append(w)
    return out


# Language that adds a leg: selling, shorting or writing an option, or one put on "against" another.
SECOND_LEG_RE = re.compile(
    r"\b(?P<word>sell|sells|selling|sold|short|shorts|shorting|shorted|write|writes|writing|"
    r"wrote|written|against)\b",
    re.IGNORECASE,
)


def post_multi_leg(post_text: str | None) -> list[str]:
    """Why a whole post is not one single-leg option (review F3, confirmation): single leg is the
    exception. The post as a whole must state exactly one strike, one expiry and one right, with
    no strategy word and no selling, shorting, writing or "against" language. Empty when it is
    one leg, or when there is no post text to judge."""
    if not post_text:
        return []
    t = extract_terms(post_text)
    out = list(t.multi_leg)
    m = SECOND_LEG_RE.search(post_text)
    if m:
        out.append(f"SECOND_LEG_LANGUAGE:{m.group('word').lower()}")
    return out


# ------------------------------------------------------------------ entry anchor: what the level is
#
# The owner's ambiguity safeguard (12 November 2026): a candidate level is classified into one
# closed kind, and only an explicit contemporaneous market quotation may anchor. Two tightenings,
# in every anchor scope: (A) the number's own clause is a pure market quotation; (B) a level whose
# own clause does not name the family takes its topic from the words just before it, which must not
# be about another level. See docs/MULTI_ASSET_MAPPING.md, "What the level is".
class AnchorLevelKind(str, Enum):
    MARKET_QUOTE = "MARKET_QUOTE"  # an explicit contemporaneous market quotation: may anchor
    ENTRY_OR_COST_BASIS = "ENTRY_OR_COST_BASIS"  # an entry price or an average cost basis
    STOP_OR_TARGET = "STOP_OR_TARGET"
    HISTORICAL = "HISTORICAL"  # a historical or retrospective level
    AMBIGUOUS = "AMBIGUOUS"  # any other numerical reference


# The only plain words a market quotation's own clause may hold besides the number, its attached
# marker, a currency or unit and a name of this family ("here" only as part of "here at").
QUOTE_CONNECTORS = frozenset(("at", "is", "are", "around", "near", "@"))
# A holder, position or trade word: the number may be the holder's entry price or average cost.
HOLDER_WORDS = frozenset((
    "we", "i", "us", "me", "our", "my", "position", "positions", "trade", "trades", "long",
    "longs", "buy", "buying", "bought", "short", "shorts", "shorting", "sell", "selling", "sold",
    "add", "adding", "added",
))
# A pronoun subject: the number may be some other thing's level.
PRONOUN_WORDS = frozenset(("it", "its", "this", "that", "these", "those", "they"))
# Cost-basis words (added to the order and chart level words for the context check).
COST_BASIS_WORDS = frozenset((
    "cost", "costs", "basis", "average", "averaged", "averaging", "avg", "breakeven", "paid",
    "fill", "filled", "fills", "entered",
))
CONTEXT_LEVEL_WORDS = LEVEL_WORDS | COST_BASIS_WORDS


def level_kind_of_words(words) -> AnchorLevelKind | None:
    """The kind a set of words makes a level, or None when there are none."""
    ws = set(words)
    if not ws:
        return None
    if ws & STOP_TARGET_WORDS:
        return AnchorLevelKind.STOP_OR_TARGET
    if ws & (HOLDER_WORDS | COST_BASIS_WORDS | {"entry", "exit"}):
        return AnchorLevelKind.ENTRY_OR_COST_BASIS
    if ws & TIME_WORDS:
        return AnchorLevelKind.HISTORICAL
    return AnchorLevelKind.AMBIGUOUS


def level_places(text: str, value: Decimal) -> list[tuple[str, str, list[str]]]:
    """Each place ``value`` is written in ``text``: (its sentence, its own clause, the earlier
    clauses of that sentence). A clause is delimited as in ``anchor_not_current``."""
    out = []
    for sentence, _ in sentences(text):
        segs = [s for s in _SEGMENT_RE.split(sentence) if s and s.strip()]
        for k, seg in enumerate(segs):
            if any(_value(t) == value for t in seg.split()):
                out.append((sentence, seg, segs[:k]))
    return out


def quote_clause_words(clause: str, allowed, skip_spans: list[tuple[int, int]] = ()) -> list[str]:
    """Rule A: the words of a number's own clause that a pure market quotation does not hold.
    ``allowed`` (lower-case phrases) are the present-market markers and this family's names; the
    quote connectors and units are always allowed; numbers are left to the number checks and
    ``skip_spans`` (a future's months) to the contract check."""
    seg = [(t.start(), t.end(), t.group(0)) for t in _TOKEN_RE.finditer(clause or "")]
    words = [_word(tok).lstrip("~").lstrip("$") for _, _, tok in seg]
    tags: list[str | None] = [None] * len(words)
    _tag(words, allowed, "allowed", tags)
    out = []
    for (a, b, tok), w, tag in zip(seg, words, tags, strict=True):
        if tag is not None or any(x <= a and b <= y for x, y in skip_spans):
            continue
        if not any(ch.isalpha() for ch in w) or any(ch.isdigit() for ch in w):
            continue
        if w in QUOTE_CONNECTORS or w in UNIT_WORDS:
            continue
        out.append(w)
    return out


def clause_kind(words: list[str]) -> AnchorLevelKind | None:
    """Rule A's kind for the foreign words of a number's own clause: a holder, position or trade
    word reads as an entry price or a cost basis, a pronoun subject (or any other word) as
    ambiguous; None when the clause is a pure quotation."""
    if not words:
        return None
    ws = set(words)
    if ws & STOP_TARGET_WORDS:
        return AnchorLevelKind.STOP_OR_TARGET
    if ws & (HOLDER_WORDS | COST_BASIS_WORDS):
        return AnchorLevelKind.ENTRY_OR_COST_BASIS
    return AnchorLevelKind.AMBIGUOUS


def context_problem(texts: list[str]) -> tuple[AnchorLevelKind, str] | None:
    """Rule B: an elliptical level takes its topic from the words before it, which must not be about
    another level: no order, chart or cost-basis word, and no other number."""
    words = set()
    for t in texts:
        for tok in (t or "").split():
            w = _word(tok)
            words |= {p for p in (w, *w.split("-"), *w.split("/")) if p}
    hit = words & CONTEXT_LEVEL_WORDS
    if hit:
        return level_kind_of_words(hit), f"CONTEXT_WORD:{min(hit)}"
    for t in texts:
        # A contract month ("Aug 2026") is the contract check's, not another level.
        spans = extract_terms(t).month_spans
        if clause_numbers("".join(" " if any(a <= k < b for a, b in spans) else ch
                                  for k, ch in enumerate(t or ""))):
            return AnchorLevelKind.AMBIGUOUS, "CONTEXT_NUMBER"
    return None


# ------------------------------------------------------------------ unresolved market references
#
# anchor_scope = SINGLE_MARKET_POST: a mention of a market the bot cannot resolve rejects the post
# (fail closed: an unknown candidate means HOLD). Closed, public, generic lists only; a publisher's
# own vocabulary goes in the private lexicon. Not named-entity recognition: a market named only by a
# lower-case word outside these lists, or by a sentence-initial unknown word, stays invisible.
class MarketMention(str, Enum):
    CASHTAG = "CASHTAG"
    TICKER_SHAPED = "TICKER_SHAPED"
    MARKET_NOUN = "MARKET_NOUN"
    INSTRUMENT_PHRASE = "INSTRUMENT_PHRASE"
    PROPER_NOUN = "PROPER_NOUN"


# Generic market and asset nouns, written from general knowledge (lower case, whole words).
MARKET_NOUNS = frozenset((
    # energy
    "oil", "crude", "brent", "wti", "gasoline", "gas", "natural gas", "natgas", "nat gas",
    "diesel", "heating oil", "energy", "lng", "propane",
    # metals
    "gold", "silver", "copper", "platinum", "palladium", "aluminium", "aluminum", "nickel",
    "zinc", "metals", "bullion", "iron ore",
    # rates and bonds
    "bond", "bonds", "treasury", "treasuries", "bund", "bunds", "gilt", "gilts", "jgb", "jgbs",
    "t-bill", "t-bills", "t-note", "t-notes", "fed funds", "sofr",
    # equity indices and equities
    "s&p", "spx", "nasdaq", "dow", "russell", "dax", "ftse", "nikkei", "hang seng", "stoxx",
    "cac", "small caps", "large caps", "equities", "equity", "indices",
    # currencies
    "dollar index", "dxy", "euro", "yen", "sterling", "franc", "yuan", "renminbi", "peso",
    "rupee", "loonie", "aussie", "forex", "fx", "currency", "currencies",
    # crypto
    "bitcoin", "btc", "ethereum", "ether", "eth", "crypto", "cryptos", "cryptocurrency",
    "solana", "altcoin", "altcoins",
    # agricultural commodities
    "corn", "wheat", "soybean", "soybeans", "soy", "soymeal", "coffee", "cocoa", "sugar",
    "cotton", "cattle", "hogs", "oats", "rice", "orange juice", "lumber", "grains", "softs",
    # volatility and other asset classes
    "vix", "volatility", "commodity", "commodities", "real estate", "reit", "reits", "miners",
    "semis", "semiconductors",
))
# Instrument-kind nouns: each must have a name of this family directly before it.
INSTRUMENT_NOUNS = frozenset((
    "futures", "calls", "puts", "options", "shares", "stock", "stocks", "etf", "etfs", "index",
    "contract", "contracts", "yield", "yields", "spread", "spreads",
))
# Upper-case acronyms that are not instruments: times, reports, institutions, chart studies.
NON_INSTRUMENT_ACRONYMS = frozenset((
    "ok", "am", "pm", "et", "est", "edt", "ct", "cst", "utc", "gmt", "eod", "eow", "ytd", "mtd",
    "qtd", "yoy", "qoq", "imo", "imho", "fyi", "tbd", "tba", "eta", "asap", "pnl", "rsi", "macd",
    "ema", "sma", "ma", "vwap", "atr", "ath", "atl", "dca", "ceo", "cfo", "us", "usa", "uk",
    "eu", "ny", "nyc", "fed", "fomc", "ecb", "boj", "boe", "snb", "rba", "boc", "pboc", "cpi",
    "ppi", "pce", "gdp", "nfp", "pmi", "ism",
))
# Capitalised words that are not markets: weekdays, months, institutions, macro releases, holidays.
NON_MARKET_PROPER_NOUNS = frozenset((
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
    "january", "february", "march", "april", "may", "june", "july", "august", "september",
    "october", "november", "december", "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep",
    "sept", "oct", "nov", "dec", "fed", "federal", "reserve", "fomc", "congress", "senate",
    "christmas", "thanksgiving", "easter", "i'm", "i've", "i'll", "i'd",
))
_TICKER_RE = re.compile(r"^[A-Z]{2,5}(?:[FGHJKMNQUVXZ]\d{1,2})?$")
_PROPER_RE = re.compile(r"^[A-Z][A-Za-z&'.-]*$")


def understood_words(extra=()) -> frozenset[str]:
    """Every word the anchor rule already understands (connectors, function words, units, markers,
    side and add words, the disqualifying words, holder, pronoun and cost-basis words), plus the
    private additions ``extra``: such a word in capitals is emphasis, not a market."""
    marker_words = {w for m in PRESENT_MARKERS for w in m.split()}
    side = {w for ws in SIDE_WORDS.values() for w in ws}
    return frozenset(
        CONNECTOR_WORDS | FUNCTION_WORDS | UNIT_WORDS | NOT_CURRENT_WORDS | marker_words | side
        | set(ADD_WORDS) | HOLDER_WORDS | PRONOUN_WORDS | COST_BASIS_WORDS
        | {w for p in extra for w in p.split()}
    )


def unresolved_market_mentions(text: str, own: set[str], own_codes: set[str], roots: set[str],
                               *, understood: frozenset[str] = frozenset(),
                               non_market: set[str] = frozenset(),
                               futures: bool = False) -> list[MarketMention]:
    """The kinds of market mention in ``text`` that do not resolve to this family. ``own`` are the
    family's lower-case names and aliases (public and private), ``own_codes`` its upper-case codes
    and symbols, ``roots`` its futures roots (a contract code of its own is its own). Cashtags are
    decided by the caller. Kinds only: never the words."""
    text = (text or "").replace("’", "'").replace("‘", "'")
    own = {o.lower() for o in own if o}
    understood = understood or understood_words()
    starts = {0}
    for m in _SENTENCE_END_RE.finditer(text):
        starts.add(m.end())
    first_tokens = set()
    for s in starts:
        m = _TOKEN_RE.search(text, s)
        if m:
            first_tokens.add(m.start())
    masked = text
    for phrase in sorted(own, key=len, reverse=True):
        masked = re.sub(rf"(?<![\w])\$?{re.escape(phrase)}(?![\w])",
                        lambda m: " " * len(m.group(0)), masked, flags=re.IGNORECASE)
    found: set[MarketMention] = set()
    low = masked.lower()
    for noun in MARKET_NOUNS:
        if re.search(rf"(?<![\w]){re.escape(noun)}(?![\w])", low):
            found.add(MarketMention.MARKET_NOUN)
            break
    head_re = (rf"(?<![\w])\$?(?:{'|'.join(re.escape(o) for o in sorted(own, key=len, reverse=True))})"
               r"(?:'s)?[\s-]+$") if own else None
    for m in _TOKEN_RE.finditer(masked):
        raw = m.group(0)
        word = raw.strip(_EDGE)
        if not word or word.startswith(("$", "@", "#")):
            continue
        start = m.start() + raw.index(word[0])
        bare = word[:-2] if word.lower().endswith("'s") else word
        lw = bare.lower()
        if lw in INSTRUMENT_NOUNS:
            before = text[:start]
            head = before.split()[-1].strip(_EDGE) if before.split() else ""
            own_head = bool(head_re and re.search(head_re, before, re.IGNORECASE))
            month_head = futures and (MONTH_WORD_RE.fullmatch(head) is not None or any(
                head.upper() == r or code_shaped(head.upper(), r) for r in roots))
            if not (own_head or month_head):
                found.add(MarketMention.INSTRUMENT_PHRASE)
            continue
        if not bare[:1].isupper():
            continue
        if lw in understood or lw in non_market or bare.upper() in own_codes \
                or any(code_shaped(bare.upper(), r) for r in roots):
            continue
        if _TICKER_RE.match(bare):
            if lw not in NON_INSTRUMENT_ACRONYMS and lw not in NON_MARKET_PROPER_NOUNS:
                found.add(MarketMention.TICKER_SHAPED)
            continue
        if not _PROPER_RE.match(bare) or start in first_tokens:
            continue
        if lw not in NON_MARKET_PROPER_NOUNS and lw not in NON_INSTRUMENT_ACRONYMS:
            found.add(MarketMention.PROPER_NOUN)
    return sorted(found, key=lambda k: k.value)
