"""The account-level risk policy (``[risk]``) and the per-family risk terms. See docs/RISK_GATES.md.

No value here has a default that bounds anything: every limit is the owner's. A ``[risk]`` table
is complete or refuses to load (unknown key, missing field, zero, negative, NaN, infinite or
malformed value); the error names field paths only, never a value. With no table at all the fixed
cash baseline applies (``risk.gates.evaluate(policy=None, ...)``).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Literal

from pydantic import (
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    ValidationError,
    field_validator,
    model_validator,
)

from tradingbot.models import StrictModel

# "<decimal>" (account currency, or the instrument's own price units for a collar) or
# "<decimal>%" (of effective equity, or of the entry).
AMOUNT_OR_PCT = r"^\d+(?:\.\d+)?%?$"
_AMOUNT_OR_PCT = re.compile(AMOUNT_OR_PCT)

FUNDING = Literal["CASH", "MARGIN_BORROWING", "FUTURES_MARGIN", "OPTION_PREMIUM", "CFD_MARGIN"]
# The funding a family may declare for its execution security type.
FUNDING_FOR_SEC_TYPE: dict[str, frozenset[str]] = {
    "STK": frozenset({"CASH", "MARGIN_BORROWING"}),
    "FUT": frozenset({"FUTURES_MARGIN"}),
    "OPT": frozenset({"OPTION_PREMIUM"}),
    "CFD": frozenset({"CFD_MARGIN"}),
    "CMDTY": frozenset(),  # no eligibility key exists for it: never eligible
}
MARGIN_FUNDING = frozenset({"FUTURES_MARGIN", "CFD_MARGIN"})

# Positive, finite decimals. A TOML float is accepted as Decimal by pydantic; strings are the
# documented spelling.
_POS = {"gt": 0, "allow_inf_nan": False}


class RiskConfigError(ValueError):
    """The ``[risk]`` table or a family's risk fields are invalid (field paths only)."""


def parse_amount_or_pct(spec: str | None) -> tuple[Decimal, bool] | None:
    """(value, is_percent) for a finite positive ``"<decimal>"`` / ``"<decimal>%"``, else None."""
    if not isinstance(spec, str) or not _AMOUNT_OR_PCT.fullmatch(spec):
        return None
    pct = spec.endswith("%")
    try:
        v = Decimal(spec[:-1] if pct else spec)
    except (InvalidOperation, ValueError):
        return None
    if not v.is_finite() or v <= 0 or (pct and v > 100):
        return None
    return v, pct


class BorrowingRules(StrictModel):
    """G1. Conventional borrowing: a securities purchase beyond cash, and a short stock sale (a
    borrowed security). Futures performance margin is NOT borrowing (G6)."""
    permitted: StrictBool
    short_sale_permitted: StrictBool
    max_borrowed: Decimal | None = Field(default=None, **_POS)

    @model_validator(mode="after")
    def _limit(self):
        if self.permitted != (self.max_borrowed is not None):
            raise ValueError("max_borrowed is required when permitted = true and only then")
        return self


class EligibilityRules(StrictModel):
    """G2. Each derivative class separately; a family must also declare the matching funding."""
    futures: StrictBool
    options: StrictBool
    cfd: StrictBool


class RiskPolicy(StrictModel):
    """The owner's ``[risk]`` table. Every field is required."""
    approved_by: StrictStr = Field(min_length=1)
    approved_at: datetime = Field(strict=True)
    currency: Literal["USD"]
    # G9: LIVE is not a value this build accepts.
    environments: list[Literal["SIM", "PAPER"]] = Field(min_length=1)
    allocated_equity: Decimal = Field(**_POS)  # G3, G6, G7: compared with net liquidation
    max_gross_notional_ratio: Decimal = Field(**_POS)  # G3: gross notional / effective equity
    max_portfolio_risk: Decimal = Field(**_POS)  # G5: account currency
    max_margin_utilisation: Decimal = Field(gt=0, le=1, allow_inf_nan=False)  # G6: fraction
    min_excess_liquidity: StrictStr = Field(pattern=AMOUNT_OR_PCT)  # G7
    account_values_max_age_seconds: StrictFloat | StrictInt = Field(gt=0, allow_inf_nan=False)
    borrowing: BorrowingRules
    eligibility: EligibilityRules

    @model_validator(mode="before")
    @classmethod
    def _no_funded(cls, raw):
        if isinstance(raw, dict) and "funded_authorization" in raw:
            raise ValueError("funded_authorization is not supported by this build: the funded "
                             "account is out of scope")
        return raw

    @field_validator("approved_by")
    @classmethod
    def _named(cls, v):
        if not v.strip():
            raise ValueError("approved_by is empty")
        return v

    @model_validator(mode="after")
    def _consistent(self):
        if self.approved_at.tzinfo is None or self.approved_at > datetime.now(UTC):
            raise ValueError("approved_at must be a past date-time with its UTC offset")
        if len(set(self.environments)) != len(self.environments):
            raise ValueError("environments lists a value twice")
        if parse_amount_or_pct(self.min_excess_liquidity) is None:
            raise ValueError("min_excess_liquidity must be a positive amount or percentage")
        return self

    @classmethod
    def from_table(cls, raw: dict) -> RiskPolicy:
        """Validate a ``[risk]`` table, raising ``RiskConfigError`` with field paths only."""
        try:
            return cls.model_validate(raw)
        except ValidationError as exc:
            paths = sorted({".".join(str(p) for p in e["loc"]) or "risk" for e in exc.errors()})
            kinds = sorted({e["type"] for e in exc.errors()})
            raise RiskConfigError(
                f"invalid [risk] table: {exc.error_count()} error(s) at {', '.join(paths)} "
                f"({', '.join(kinds)})") from None


# ----------------------------------------------------------------------------- family fields

class FamilyRisk(StrictModel):
    """A family's own risk limits (G4)."""
    max_risk_per_idea: Decimal = Field(**_POS)  # account currency, per idea


class MarginFigure(StrictModel):
    """The owner's per-unit performance-margin figure for a margin-funded family (G6, G7), from
    the broker's published table, with the date it was checked and how long it may be used."""
    initial: Decimal = Field(**_POS)  # per unit (contract), in ``currency``
    maintenance: Decimal = Field(**_POS)
    currency: Literal["USD"]
    verified_at: date
    max_age_days: StrictInt = Field(gt=0)

    @model_validator(mode="after")
    def _order(self):
        if self.maintenance > self.initial:
            raise ValueError("maintenance margin exceeds initial margin")
        if self.verified_at > datetime.now(UTC).date():
            raise ValueError("verified_at is in the future")
        return self

    def stale(self, today: date) -> bool:
        return (today - self.verified_at).days > self.max_age_days


def check_family_risk_fields(family: str, sec_type: str | None, funding: str | None,
                             margin: MarginFigure | None, entry_collar: str | None) -> list[str]:
    """Load-time consistency of a family's risk fields (``FamilyMapping`` calls it)."""
    problems = []
    if funding is not None and sec_type is not None \
            and funding not in FUNDING_FOR_SEC_TYPE.get(sec_type, frozenset()):
        problems.append(f"funding {funding} does not fit execution sec_type {sec_type}")
    if margin is not None and funding not in MARGIN_FUNDING:
        problems.append("margin is stated only for FUTURES_MARGIN or CFD_MARGIN funding")
    if entry_collar is not None and parse_amount_or_pct(entry_collar) is None:
        problems.append("entry_collar must be a positive price distance or percentage")
    return [f"family {family}: {p}" for p in problems]


@dataclass(frozen=True)
class FamilyRiskTerms:
    """What the gates read from one family entry (None: the entry does not state it)."""
    family: str | None
    sec_type: str
    funding: str | None = None
    max_risk_per_idea: Decimal | None = None
    margin: MarginFigure | None = None
    max_stop_distance: str | None = None
    entry_collar: str | None = None
    multiplier: Decimal | None = None  # as stated in the family entry

    @classmethod
    def of(cls, entry) -> FamilyRiskTerms:
        """From a ``runtime.mapping.FamilyMapping`` (duck-typed: no import cycle)."""
        x = entry.execution
        stated_mult = None
        if x is not None and "multiplier" in x.model_fields_set:
            stated_mult = x.multiplier
        elif x is not None and x.sec_type == "STK":
            stated_mult = Decimal(1)  # a share is its own unit
        return cls(family=entry.family, sec_type=x.sec_type if x else "UNKNOWN",
                   funding=entry.funding,
                   max_risk_per_idea=entry.risk.max_risk_per_idea if entry.risk else None,
                   margin=entry.margin, max_stop_distance=entry.max_stop_distance,
                   entry_collar=entry.entry_collar, multiplier=stated_mult)
