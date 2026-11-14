"""An INVENTED [risk] table and family risk fields for tests and mock scenarios that use futures,
options, CFDs or a short stock sale (docs/RISK_GATES.md).

EVERY VALUE HERE IS INVENTED TEST DATA, deliberately wide so that a scenario decides exactly as it
did before the risk gates existed. None is a proposal, an owner's value or a contract
specification; nothing here may be copied into a configuration.
"""
from __future__ import annotations

from datetime import UTC, date, datetime

from tradingbot.risk.policy import FamilyRiskTerms, MarginFigure, RiskPolicy

INVENTED_RISK_TABLE = {
    "approved_by": "invented-test-owner", "approved_at": datetime(2026, 1, 1, tzinfo=UTC),
    "currency": "USD", "environments": ["SIM", "PAPER"],
    "allocated_equity": "100000000", "max_gross_notional_ratio": "1000",
    "max_portfolio_risk": "100000000", "max_margin_utilisation": "1",
    "min_excess_liquidity": "1", "account_values_max_age_seconds": 86400,
    "borrowing": {"permitted": True, "short_sale_permitted": True, "max_borrowed": "100000000"},
    "eligibility": {"futures": True, "options": True, "cfd": True},
}
INVENTED_RISK = RiskPolicy.from_table(INVENTED_RISK_TABLE)

FUNDING = {"STK": "CASH", "FUT": "FUTURES_MARGIN", "OPT": "OPTION_PREMIUM", "CFD": "CFD_MARGIN"}
INVENTED_MARGIN = {"initial": "1", "maintenance": "1", "currency": "USD",
                   "verified_at": date(2026, 1, 1), "max_age_days": 36500}


def family_fields(sec_type: str) -> dict:
    """Invented family risk fields for a mapping entry of this execution sec_type (a dict for
    FamilyMapping)."""
    out = {"funding": FUNDING[sec_type],
           "risk": {"max_risk_per_idea": "100000000", "max_stop_distance": "100%"},
           "entry_collar": "100%"}
    if sec_type in ("FUT", "CFD"):
        out["margin"] = dict(INVENTED_MARGIN)
    return out


def toml_fields(sec_type: str) -> str:
    """The same fields as TOML lines for a [[mapping]] entry."""
    f = family_fields(sec_type)
    lines = [f'funding = "{f["funding"]}"',
             'risk = { max_risk_per_idea = "100000000", max_stop_distance = "100%" }',
             f'entry_collar = "{f["entry_collar"]}"']
    if "margin" in f:
        lines.append('margin = { initial = "1", maintenance = "1", currency = "USD", '
                     'verified_at = 2026-01-01, max_age_days = 36500 }')
    return "\n".join(lines)


TOML_TABLE = """
[risk]
approved_by = "invented-test-owner"
approved_at = 2026-01-01T00:00:00Z
currency = "USD"
environments = ["SIM", "PAPER"]
allocated_equity = "100000000"
max_gross_notional_ratio = "1000"
max_portfolio_risk = "100000000"
max_margin_utilisation = "1"
min_excess_liquidity = "1"
account_values_max_age_seconds = 86400
borrowing = { permitted = true, short_sale_permitted = true, max_borrowed = "100000000" }
eligibility = { futures = true, options = true, cfd = true }
"""


def terms(sec_type: str, family: str = "QX", multiplier=None) -> FamilyRiskTerms:
    f = family_fields(sec_type)
    margin = MarginFigure(**f["margin"]) if "margin" in f else None
    from decimal import Decimal
    return FamilyRiskTerms(family=family, sec_type=sec_type, funding=f["funding"],
                           max_risk_per_idea=Decimal(f["risk"]["max_risk_per_idea"]),
                           margin=margin, max_stop_distance="100%", entry_collar="100%",
                           multiplier=Decimal(multiplier) if multiplier is not None else None)


SEC_OF_ASSET = {"EQUITY": "STK", "ETF": "STK", "FUTURE": "FUT", "OPTION": "OPT", "CFD": "CFD"}


def market_terms_fields(asset_class, multiplier) -> dict:
    """Invented MarketTerms risk fields for a gateway test of this asset class."""
    from decimal import Decimal
    sec = SEC_OF_ASSET.get(getattr(asset_class, "value", asset_class), "STK")
    out = {"funding": FUNDING[sec], "max_risk_per_idea": Decimal(100000000),
           "risk_max_stop_distance": "100%", "entry_collar": "100%",
           "multiplier_stated": Decimal(multiplier),
           # one reference point = one execution point, as a family states it (basis_shift)
           "reference_scale_ratio": Decimal(1)}
    if sec in ("FUT", "CFD"):
        out.update({"margin_initial": Decimal(1), "margin_maintenance": Decimal(1),
                    "margin_verified_at": date(2026, 1, 1), "margin_max_age_days": 36500})
    return out



def arm(coord, broker, families) -> None:
    """Give a coordinator and its SimBroker the invented table's family terms and account:
    ``families`` = (symbol, sec_type, multiplier)."""
    from decimal import Decimal
    coord.risk_families = {f"{sym}:{sec}": terms(sec, sym, mult) for sym, sec, mult in families}
    coord.risk_margin_by_symbol = {sym: Decimal(1) for sym, sec, _ in families
                                   if sec in ("FUT", "CFD")}
    broker.account_cash = INVENTED_RISK.allocated_equity
    broker.margin_per_unit = {sym: (Decimal(1), Decimal(1)) for sym, sec, _ in families
                              if sec in ("FUT", "CFD")}
