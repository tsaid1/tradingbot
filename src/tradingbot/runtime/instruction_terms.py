"""Contract terms stated in one instruction clause: a futures contract month, an option's expiry,
strike and right.

Deterministic and conservative. It only reads what the clause states explicitly; it never infers a
month, an expiry or a strike. The mapping policy (runtime/mapping.py) decides what a missing or
conflicting term means (HOLD). A month is recognised only with its year ("Dec 2026") or followed by
the word "contract" ("the Dec contract"); an option expiry only as an ISO date ("2026-12-18"); a
strike only directly before the right ("105 call", "105 strike put").
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
_MON = (
    r"(?P<mon>jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|"
    r"sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?"
)
MONTH_YEAR_RE = re.compile(rf"\b{_MON}\s+(?P<year>20\d{{2}})\b", re.IGNORECASE)
MONTH_CONTRACT_RE = re.compile(rf"\b{_MON}\s+(?:futures?\s+)?contract\b", re.IGNORECASE)
ISO_DATE_RE = re.compile(r"(?<![\d-])(?P<y>20\d{2})-(?P<m>\d{2})-(?P<d>\d{2})(?![\d-])")
STRIKE_RIGHT_RE = re.compile(
    r"(?<![\w.$-])\$?(?P<strike>\d+(?:\.\d+)?)\s*(?:strike\s+)?(?P<right>call|put)s?\b",
    re.IGNORECASE,
)
RIGHT_RE = re.compile(r"\b(?P<right>call|put)s?\b", re.IGNORECASE)
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

    @property
    def names_a_month(self) -> bool:
        return bool(self.months or self.bare_months)


def extract_terms(clause: str | None) -> InstructionTerms:
    out = InstructionTerms()
    if not clause:
        return out
    for m in MONTH_YEAR_RE.finditer(clause):
        month = f"{m.group('year')}{_MONTH_NAMES[m.group('mon')[:3].lower()]:02d}"
        if month not in out.months:
            out.months.append(month)
    for m in MONTH_CONTRACT_RE.finditer(clause):
        n = _MONTH_NAMES[m.group("mon")[:3].lower()]
        if n not in out.bare_months:
            out.bare_months.append(n)
    out.option_language = OPTION_LANGUAGE_RE.search(clause) is not None
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
    return out
