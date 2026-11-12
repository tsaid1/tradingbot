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
