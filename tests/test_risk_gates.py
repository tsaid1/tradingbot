"""The nine independent risk gates (tradingbot.risk; docs/RISK_GATES.md).

EVERY VALUE IN THIS FILE IS INVENTED TEST DATA: no limit, margin figure, multiplier or price here
is a proposal, an owner's value or a contract specification.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal as D

import pytest

from tradingbot.risk import (
    AccountSnapshot,
    Exposure,
    FamilyRiskTerms,
    MarginFigure,
    Proposal,
    RiskConfigError,
    RiskPolicy,
    evaluate,
    what_if,
)
from tradingbot.risk import gates as G
from tradingbot.runtime.mapping import FamilyMapping

NOW = datetime(2026, 6, 1, 14, 0, tzinfo=UTC)

# Invented [risk] table (test data only).
TABLE = {
    "approved_by": "test-owner", "approved_at": datetime(2026, 5, 1, tzinfo=UTC),
    "currency": "USD", "environments": ["SIM", "PAPER"],
    "allocated_equity": "100000", "max_gross_notional_ratio": "2",
    "max_portfolio_risk": "3000", "max_margin_utilisation": "0.5",
    "min_excess_liquidity": "10000", "account_values_max_age_seconds": 60,
    "borrowing": {"permitted": False, "short_sale_permitted": False},
    "eligibility": {"futures": True, "options": False, "cfd": False},
}
POLICY = RiskPolicy.from_table(TABLE)
MARGIN = MarginFigure(initial=D(2000), maintenance=D(1800), currency="USD",
                      verified_at=date(2026, 5, 20), max_age_days=30)
FUT = FamilyRiskTerms(family="QOIL", sec_type="FUT", funding="FUTURES_MARGIN",
                      max_risk_per_idea=D(1000), margin=MARGIN, max_stop_distance="5%",
                      entry_collar="1%", multiplier=D(10))
ETF = FamilyRiskTerms(family="QETF", sec_type="STK", funding="CASH", max_risk_per_idea=D(500),
                      max_stop_distance="5%", entry_collar="0.5%", multiplier=D(1))


def account(**kw) -> AccountSnapshot:
    base = {"read_at": NOW - timedelta(seconds=5), "net_liquidation": D(100000),
                "total_cash": D(100000), "excess_liquidity": D(100000), "init_margin": D(0),
                "maint_margin": D(0)}
    base.update(kw)
    return AccountSnapshot(**base)


def fut(**kw) -> Proposal:
    base = {"sec_type": "FUT", "side": "LONG", "units": 1, "entry": D(100), "stop": D(96),
                "multiplier": D(10), "touch": D(100)}
    base.update(kw)
    return Proposal(**base)


def etf(**kw) -> Proposal:
    base = {"sec_type": "STK", "side": "LONG", "units": 10, "entry": D(100), "stop": D(98),
                "multiplier": D(1), "touch": D(100)}
    base.update(kw)
    return Proposal(**base)


def run(proposal, family=FUT, policy=POLICY, acct=None, portfolio=(), env="SIM", **kw):
    return evaluate(policy, family, proposal, account=acct or account(),
                    portfolio=list(portfolio), environment=env, now=NOW, **kw)


def gate(report, g):
    return next(r for r in report.results if r.gate == g)


def only(report, g, code):
    """Exactly gate ``g`` failed, with ``code`` among its codes."""
    assert [r.gate for r in report.failed] == [g], report.reasons()
    assert code in gate(report, g).codes


def test_a_valid_future_passes_all_nine_and_records_the_measures():
    r = run(fut())
    assert r.passed and not r.reasons()
    assert r.dollar_risk == D(40)  # |100 - 96| x 10 x 1
    assert r.performance_margin == D(2000) and r.borrowing == D(0)
    assert r.economic_leverage == D(1000) / D(100000)
    assert r.effective_equity == D(100000)


# ----------------------------------------------------------------------------- per-gate boundaries

def test_g1_cash_purchase_at_cash_passes_and_one_cent_over_is_borrowing():
    assert run(etf(), ETF, acct=account(total_cash=D(1000))).passed
    only(run(etf(), ETF, acct=account(total_cash=D("999.99"))), "G1", G.BORROWING_NOT_PERMITTED)


def test_g1_working_buy_entries_commit_cash():
    held = Exposure(sec_type="STK", units=1, mark=D(1), multiplier=D(1), entry=D(1),
                    stop=D("0.99"), buy_cost=D("0.01"))
    only(run(etf(), ETF, acct=account(total_cash=D(1000)), portfolio=[held]), "G1",
         G.BORROWING_NOT_PERMITTED)


def test_g1_borrowing_limit_when_permitted():
    t = deepcopy(TABLE)
    t["borrowing"] = {"permitted": True, "short_sale_permitted": False, "max_borrowed": "100"}
    p = RiskPolicy.from_table(t)
    assert run(etf(), ETF, p, acct=account(total_cash=D(900))).passed
    only(run(etf(), ETF, p, acct=account(total_cash=D("899.99"))), "G1", G.BORROWING_LIMIT)


def test_g1_short_stock_sale_is_borrowing_class():
    only(run(etf(side="SHORT", stop=D(102)), ETF), "G1", G.SHORT_SALE_NOT_PERMITTED)
    t = deepcopy(TABLE)
    t["borrowing"]["short_sale_permitted"] = True
    assert run(etf(side="SHORT", stop=D(102)), ETF, RiskPolicy.from_table(t)).passed


def test_g1_futures_margin_is_not_borrowing():
    r = run(fut(), acct=account(total_cash=D(0)))
    assert gate(r, "G1").status == "PASS" and r.borrowing == 0


def test_g2_each_derivative_class_needs_its_own_key_and_funding():
    t = deepcopy(TABLE)
    t["eligibility"]["futures"] = False
    only(run(fut(), policy=RiskPolicy.from_table(t)), "G2", f"{G.NOT_ELIGIBLE}:FUT")
    no_funding = FamilyRiskTerms(**{**FUT.__dict__, "funding": None})
    assert G.FUNDING_MISSING in gate(run(fut(), no_funding), "G2").codes


def test_g3_gross_notional_ratio_boundary():
    big = Exposure(sec_type="STK", units=1, mark=D(199000), multiplier=D(1), entry=D(199000),
                   stop=D(198999))
    assert gate(run(fut(), portfolio=[big]), "G3").status == "PASS"  # 200000 / 100000 = 2
    over = Exposure(**{**big.__dict__, "mark": D("199000.01")})
    assert G.GROSS_NOTIONAL_LIMIT in gate(run(fut(), portfolio=[over]), "G3").codes


def test_g3_effective_equity_is_the_smaller_of_stated_and_net_liquidation():
    r = run(fut(), acct=account(net_liquidation=D(499)))
    assert r.effective_equity == D(499) and r.allocated_equity == D(100000)
    assert r.net_liquidation == D(499) and G.GROSS_NOTIONAL_LIMIT in gate(r, "G3").codes


def test_g4_dollar_risk_boundary_and_unknown_inputs():
    assert run(fut(units=25)).dollar_risk == D(1000)  # at the limit
    assert gate(run(fut(units=25)), "G4").status == "PASS"
    assert G.PER_IDEA_LIMIT in gate(run(fut(units=25, stop=D("95.999"))), "G4").codes
    assert G.STOP_UNKNOWN in gate(run(fut(stop=None)), "G4").codes
    assert G.MULTIPLIER_UNKNOWN in gate(run(fut(multiplier=D(5))), "G4").codes
    assert G.MULTIPLIER_UNKNOWN in gate(run(fut(multiplier_confirmed=False)), "G4").codes
    unstated = FamilyRiskTerms(**{**FUT.__dict__, "multiplier": None})
    assert G.MULTIPLIER_UNKNOWN in gate(run(fut(), unstated), "G4").codes


def test_g4_add_counts_the_ideas_existing_risk():
    only(run(fut(action="ADD", held_risk=D("960.01"))), "G4", G.PER_IDEA_LIMIT)


def test_g5_portfolio_risk_boundary_and_unbounded_positions():
    held = Exposure(sec_type="FUT", units=1, mark=D(100), multiplier=D(10), entry=D(100),
                    stop=D(10), margin_initial=D(2000))  # risk 900 each
    full = [held, held, held]
    assert gate(run(fut(), portfolio=full), "G5").value == D(2740)
    assert gate(run(fut(stop=D(70)), portfolio=full), "G5").status == "PASS"  # exactly 3000
    assert G.PORTFOLIO_LIMIT in gate(run(fut(stop=D("69.99")), portfolio=full), "G5").codes
    naked = Exposure(**{**held.__dict__, "protected": False})
    assert G.PORTFOLIO_UNBOUNDED in gate(run(fut(), portfolio=[naked]), "G5").codes
    no_stop = Exposure(**{**held.__dict__, "stop": None})
    assert G.PORTFOLIO_UNBOUNDED in gate(run(fut(), portfolio=[no_stop]), "G5").codes


def test_g6_margin_utilisation_boundary_staleness_and_broker_figure():
    held = Exposure(sec_type="FUT", units=24, mark=D(1), multiplier=D(10), entry=D(1),
                    stop=D("0.99"), margin_initial=D(2000))
    assert gate(run(fut(), portfolio=[held]), "G6").status == "PASS"  # 50000 / 100000
    assert G.MARGIN_LIMIT in gate(run(fut(), acct=account(init_margin=D("48000.01"))),
                                  "G6").codes  # the broker's larger figure is used
    stale = FamilyRiskTerms(**{**FUT.__dict__, "margin": MarginFigure(
        **{**MARGIN.model_dump(), "verified_at": date(2026, 4, 1)})})
    assert G.MARGIN_FIGURE_STALE in gate(run(fut(), stale), "G6").codes
    missing = FamilyRiskTerms(**{**FUT.__dict__, "margin": None})
    assert G.MARGIN_FIGURE_MISSING in gate(run(fut(), missing), "G6").codes
    only(run(fut(), acct=account(init_margin=None)), "G6", f"{G.INPUT_MISSING}:G6:INIT_MARGIN")


def test_g7_excess_liquidity_buffer_boundary_and_percentage():
    assert gate(run(fut(), acct=account(excess_liquidity=D(12000))), "G7").status == "PASS"
    only(run(fut(), acct=account(excess_liquidity=D("11999.99"))), "G7", G.EXCESS_LIQUIDITY)
    t = deepcopy(TABLE)
    t["min_excess_liquidity"] = "20%"
    p = RiskPolicy.from_table(t)
    assert gate(run(fut(), policy=p, acct=account(excess_liquidity=D(22000))),
                "G7").status == "PASS"
    assert G.EXCESS_LIQUIDITY in gate(run(fut(), policy=p, acct=account(
        excess_liquidity=D("21999.99"))), "G7").codes


def test_g8_family_stop_and_price_rules_are_mandatory_and_the_familys_own():
    only(run(fut(stop=D("94.99"))), "G8", G.STOP_TOO_FAR)
    only(run(fut(touch=D("101.01"))), "G8", G.PRICE_OUTSIDE_COLLAR)
    bare = FamilyRiskTerms(**{**FUT.__dict__, "max_stop_distance": None, "entry_collar": None})
    codes = gate(run(fut(), bare), "G8").codes
    assert G.STOP_RULE_MISSING in codes and G.PRICE_RULE_MISSING in codes


def test_g9_environment_under_a_table():
    only(run(fut(), env="LIVE"), "G9", f"{G.ENVIRONMENT_NOT_AUTHORIZED}:LIVE")
    only(run(fut(), env=None), "G9", f"{G.ENVIRONMENT_NOT_AUTHORIZED}:UNDECLARED")
    t = deepcopy(TABLE)
    t["environments"] = ["SIM"]
    only(run(fut(), policy=RiskPolicy.from_table(t), env="PAPER"), "G9",
         f"{G.ENVIRONMENT_NOT_AUTHORIZED}:PAPER")


# ----------------------------------------------------------------------------- the model

def test_all_nine_failing_reports_every_gate():
    fam = FamilyRiskTerms(family="QSTK", sec_type="STK", funding=None, max_risk_per_idea=D(1),
                          multiplier=D(1))  # no funding, no stop or price rule
    huge = Exposure(sec_type="FUT", units=10, mark=D(100000), multiplier=D(10), entry=D(100),
                    stop=D(1), protected=False, margin_initial=None)
    r = evaluate(POLICY, fam, etf(side="SHORT", stop=D(150)),
                 account=account(excess_liquidity=D(0)), portfolio=[huge], environment="LIVE",
                 now=NOW)
    assert [g.gate for g in r.failed] == [f"G{i}" for i in range(1, 10)]
    reasons = r.reasons()
    assert reasons[0] == G.FAILED and len(reasons) == len(set(reasons))
    for code in (G.SHORT_SALE_NOT_PERMITTED, G.FUNDING_MISSING, G.GROSS_NOTIONAL_LIMIT,
                 G.PER_IDEA_LIMIT, G.PORTFOLIO_UNBOUNDED, G.MARGIN_FIGURE_MISSING,
                 G.EXCESS_LIQUIDITY, G.STOP_RULE_MISSING, f"{G.ENVIRONMENT_NOT_AUTHORIZED}:LIVE"):
        assert code in reasons


def test_gateway_stage_never_passes_account_gates():
    r = run(fut(), stage="GATEWAY", acct=None)
    statuses = {g.gate: g.status for g in r.results}
    assert statuses["G3"] == statuses["G5"] == statuses["G7"] == "NOT_EVALUATED"
    assert not r.passed and not r.refused


def test_stale_future_and_missing_account_values_refuse():
    r = run(fut(), acct=account(read_at=NOW - timedelta(seconds=61)))
    assert {g.gate for g in r.failed} == {"G3", "G6", "G7"}
    assert f"{G.INPUT_STALE}:G6:ACCOUNT_VALUES" in r.reasons()
    r = run(fut(), acct=account(read_at=NOW + timedelta(seconds=6)))
    assert f"{G.INPUT_STALE}:G7:ACCOUNT_VALUES" in r.reasons()
    r = run(etf(), ETF, acct=account(total_cash=None))
    only(r, "G1", f"{G.INPUT_MISSING}:G1:TOTAL_CASH")
    r = evaluate(POLICY, FUT, fut(), account=None, portfolio=[], environment="SIM", now=NOW)
    assert f"{G.INPUT_MISSING}:G7:ACCOUNT_VALUES" in r.reasons()


def test_currency_mismatch_refuses():
    assert G.CURRENCY_MISMATCH in run(fut(currency="EUR")).reasons()


# ----------------------------------------------------------------------------- the cash baseline

def baseline(proposal, family=None, env="SIM", **kw):
    family = family or FamilyRiskTerms(family="TLT", sec_type="STK", max_stop_distance="5%")
    return evaluate(None, family, proposal, account=None, portfolio=None, environment=env,
                    now=NOW, **kw)


def test_baseline_admits_a_long_cash_stock_and_records_it():
    r = baseline(etf(units=1))
    assert r.passed and r.baseline and not r.reasons()
    assert all(g.status == "BASELINE" and g.codes == (G.BASELINE,) for g in r.results)


def test_baseline_refuses_short_sale_borrowing_and_derivatives():
    only(baseline(etf(side="SHORT", stop=D(102))), "G1", G.SHORT_SALE_NOT_PERMITTED)
    margin = FamilyRiskTerms(family="QSTK", sec_type="STK", funding="MARGIN_BORROWING")
    only(baseline(etf(), margin), "G1", G.BORROWING_NOT_PERMITTED)
    for sec in ("FUT", "OPT", "CFD", "CMDTY"):
        only(baseline(fut(sec_type=sec), FamilyRiskTerms(family="X", sec_type=sec)), "G2",
             f"{G.NOT_ELIGIBLE}:{sec}")


def test_baseline_environment_is_todays_rule():
    assert baseline(etf(), env=None).passed  # an explicit entry, undeclared broker: as on main
    only(baseline(etf(anchored=True), env=None), "G9",
         f"{G.ENVIRONMENT_NOT_AUTHORIZED}:UNDECLARED")
    only(baseline(etf(), env="LIVE"), "G9", f"{G.ENVIRONMENT_NOT_AUTHORIZED}:LIVE")
    assert baseline(etf(anchored=True), env="PAPER").passed


# ----------------------------------------------------------------------------- config refusals

@pytest.mark.parametrize("path", [
    "approved_by", "approved_at", "currency", "environments", "allocated_equity",
    "max_gross_notional_ratio", "max_portfolio_risk", "max_margin_utilisation",
    "min_excess_liquidity", "account_values_max_age_seconds", "borrowing", "eligibility",
    "borrowing.permitted", "borrowing.short_sale_permitted", "eligibility.futures",
])
def test_a_missing_field_refuses(path):
    t = deepcopy(TABLE)
    *head, last = path.split(".")
    target = t
    for k in head:
        target = target[k]
    del target[last]
    with pytest.raises(RiskConfigError):
        RiskPolicy.from_table(t)


@pytest.mark.parametrize("key,value", [
    ("allocated_equity", "0"), ("allocated_equity", "-1"), ("allocated_equity", "NaN"),
    ("allocated_equity", "Infinity"), ("allocated_equity", "lots"),
    ("max_gross_notional_ratio", "0"), ("max_portfolio_risk", "-5"),
    ("max_margin_utilisation", "1.01"), ("max_margin_utilisation", "0"),
    ("min_excess_liquidity", "0"), ("min_excess_liquidity", "0%"),
    ("min_excess_liquidity", "101%"), ("min_excess_liquidity", "ten"),
    ("account_values_max_age_seconds", 0), ("environments", ["LIVE"]),
    ("environments", []), ("environments", ["SIM", "SIM"]), ("currency", "EUR"),
    ("approved_by", " "), ("unknown_key", 1), ("funded_authorization", "anything"),
    ("borrowing", {"permitted": True, "short_sale_permitted": False}),
    ("borrowing", {"permitted": False, "short_sale_permitted": False, "max_borrowed": "1"}),
    ("borrowing", {"permitted": "yes", "short_sale_permitted": False}),
])
def test_an_invalid_value_refuses_and_names_no_value(key, value):
    t = deepcopy(TABLE)
    t[key] = value
    with pytest.raises(RiskConfigError) as exc:
        RiskPolicy.from_table(t)
    if isinstance(value, str) and len(value) > 3:
        assert value not in str(exc.value)


# ----------------------------------------------------------------------------- family fields

ETF_ENTRY = {
    "family": "QETF", "basis": "DIRECT",
    "reference": {"sec_type": "STK", "asset_class": "ETF", "symbol": "QETF", "exchange": "SMART",
               "currency": "USD"},
    "execution": {"sec_type": "STK", "asset_class": "ETF", "symbol": "QETF", "exchange": "SMART",
               "currency": "USD"},
    "sizing": {"units_per_signal": 1, "max_units": 1, "max_notional": "150"},
    "session": {"policy": "REGULAR_ONLY", "timezone": "America/New_York", "days": ["MON"],
             "regular": ["09:30-16:00"]},
}


@pytest.mark.parametrize("extra", [
    {"funding": "FUTURES_MARGIN"},
    {"funding": "CASH", "margin": {"initial": "1", "maintenance": "1", "currency": "USD",
                                   "verified_at": date(2026, 1, 1), "max_age_days": 1}},
    {"entry_collar": "0%"}, {"entry_collar": "-1"}, {"entry_collar": "x"},
    {"risk": {"max_risk_per_idea": "0"}}, {"risk": {}}, {"funding": "LEVERAGE"},
])
def test_family_risk_fields_refuse_when_inconsistent(extra):
    with pytest.raises(ValueError):
        FamilyMapping.model_validate({**ETF_ENTRY, **extra})


def test_family_fields_parse_and_terms_are_the_familys_own():
    tlt = FamilyMapping.model_validate({**ETF_ENTRY, "family": "QETF", "max_stop_distance": "5%",
                                        "funding": "CASH", "entry_collar": "0.3%",
                                        "risk": {"max_risk_per_idea": "7"}})
    other = FamilyMapping.model_validate({
        **ETF_ENTRY, "family": "QOTH",
        "reference": {**ETF_ENTRY["reference"], "symbol": "QOTH"},
        "execution": {**ETF_ENTRY["execution"], "symbol": "QOTH"}, "funding": "CASH",
        "risk": {"max_risk_per_idea": "7"}})
    t1, t2 = FamilyRiskTerms.of(tlt), FamilyRiskTerms.of(other)
    assert (t1.max_stop_distance, t1.entry_collar) == ("5%", "0.3%")
    assert (t2.max_stop_distance, t2.entry_collar) == (None, None)  # never inherited
    r = evaluate(POLICY, t2, etf(units=1), account=account(), portfolio=[], environment="SIM",
                 now=NOW)
    only(r, "G8", G.STOP_RULE_MISSING)
    assert "5%" not in repr(r) and "0.3" not in repr(gate(r, "G8"))


# ----------------------------------------------------------------------------- the hook

def test_what_if_hook_returns_nine_results_and_is_pure():
    r = what_if(TABLE, FUT, side="LONG", entry_reference=D(100), stop=D(96), units=1, now=NOW)
    assert [g.gate for g in r.results] == [f"G{i}" for i in range(1, 10)] and r.passed
    r = what_if(TABLE, FUT, side="LONG", entry_reference=D(100), stop=D(90), units=20, now=NOW)
    assert {g.gate for g in r.failed} == {"G4", "G8"}
