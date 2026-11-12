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
    r"collar|ratio|covered|combo)s?\b",
    re.IGNORECASE,
)
# The ones that only ever name options: they route an instruction to its option family even
# without the words call, put or option.
OPTION_ONLY_STRATEGY_RE = re.compile(
    r"\b(?:straddles?|strangles?|condors?|butterfl(?:y|ies)|"
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

    @property
    def names_a_month(self) -> bool:
        return bool(self.months or self.bare_months or self.month_codes
                    or self.unrecognised_months)


def extract_terms(clause: str | None) -> InstructionTerms:
    out = InstructionTerms()
    if not clause:
        return out
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
    for m in MONTH_CONTRACT_RE.finditer(clause):
        n = _MONTH_NAMES[m.group("mon")[:3].lower()]
        if n not in out.bare_months:
            out.bare_months.append(n)
        recognised.append(m.span("mon"))
    for m in MONTH_WORD_RE.finditer(clause):
        inside = any(a <= m.start("word") < b for a, b in recognised)
        if not inside and m.group("word").lower() not in ENGLISH_WORDS:
            out.unrecognised_months.append(m.group(0))
    for m in NUMERIC_MONTH_RE.finditer(clause):
        y = m.group("y1") or m.group("y2") or m.group("y3")
        mo = m.group("m1") or m.group("m2") or m.group("m3")
        add_month(int(y), int(mo), m.group(0))
    for m in CONTRACT_CODE_RE.finditer(clause):
        out.month_codes.append(
            (m.group(0), m.group("root"), _CODE_MONTH[m.group("code")], m.group("yy")))
    out.upper_words = UPPER_WORD_RE.findall(clause)
    out.option_language = (OPTION_LANGUAGE_RE.search(clause) is not None
                           or OPTION_ONLY_STRATEGY_RE.search(clause) is not None
                           or COMPACT_STRIKE_RE.search(clause) is not None)
    for m in ISO_DATE_RE.finditer(clause):
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
    for m in RIGHT_RE.finditer(clause):
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
