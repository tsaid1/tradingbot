"""Discovery report -> unapproved mapping proposal (offline). Invented report fixture only."""
from __future__ import annotations

import copy
import importlib.util
import json
import socket
import subprocess
import sys
import tomllib
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tradingbot.event_models import PriceBasis, Timing, TradeAction
from tradingbot.execution.broker import ContractRef
from tradingbot.models import (
    Action,
    AssetClass,
    Instrument,
    MappedQuote,
    MockPolicy,
    PositionSide,
)
from tradingbot.runtime import mapping_proposal as mp
from tradingbot.runtime.mapping import MAPPING_NOT_APPROVED, MappingPolicy
from tradingbot.validator import gate_action

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "fixtures" / "contract_discovery_invented" / "contract_catalog_INVENTED.json"
POLICY = ROOT / "config" / "mapping_proposal.toml"
MHNG = "MHNG fut (micro Henry Hub, symbol as reported by IBKR product search; UNCONFIRMED)"
SESSION = {"policy": "REGULAR_ONLY", "timezone": "America/New_York",
           "days": ["MON", "TUE", "WED", "THU", "FRI"], "regular": ["09:30-16:00"]}
SIZING = {"units_per_signal": 1, "max_units": 2, "max_notional": "50000"}
FUT_OWNER = {"sizing": SIZING, "session": SESSION, "roll": {"business_days_before": 2},
             "contract_selection": {"rule": "EXPLICIT_ONLY", "min_business_days_to_roll": 3}}


def _load():
    spec = importlib.util.spec_from_file_location("tb_mapping_proposal",
                                                  ROOT / "scripts" / "tb_mapping_proposal.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def catalog() -> dict:
    return json.loads(FIXTURE.read_text())


def shipped(**kw) -> mp.ProposalPolicy:
    pol = mp.load_policy(POLICY)
    return mp.merge_cli(pol, kw.get("specs", []), kw.get("roll_days", 5), {})


def filled_policy() -> mp.ProposalPolicy:
    """The shipped vehicles with every owner input stated (invented values), GOLD switched to
    the explicit MGC/BASIS_SHIFT alternative, and a single-leg option family."""
    raw = tomllib.loads(POLICY.read_text())
    raw["roll_rule"] = {"business_days_before_last_trade": 5}
    by = {f["family"]: f for f in raw["family"]}
    by["TLT"]["owner"] = {"sizing": {"units_per_signal": 10, "max_units": 20,
                                     "max_notional": "5000"}, "session": SESSION}
    by["SPX"]["owner"] = {**FUT_OWNER, "contract_selection": {
        "rule": "EXPLICIT_OR_NEAREST", "min_business_days_to_roll": 3},
        "reference_trigger": {"released_order_type": "MKT", "reference_hours": ["09:30-16:00"],
                              "require_backstop": True},
        "basis_shift": {"max_abs_basis": "40", "max_age_seconds": 5, "scale_ratio": "1"}}
    for fam in ("WTI", "NATGAS"):
        by[fam]["owner"] = {**FUT_OWNER, "same_series_reason": "INVENTED: micro = standard month"}
    by["GOLD"].update(execution="MGC fut", reference="XAUUSD spot gold", treatment="BASIS_SHIFT")
    by["GOLD"]["owner"] = {**FUT_OWNER, "contract_selection": {
        "rule": "EXPLICIT_OR_NEAREST", "min_business_days_to_roll": 3},
        "basis_shift": {"max_abs_basis": "30", "max_age_seconds": 5, "scale_ratio": "1"}}
    return mp.parse_policy(raw)


def write(tmp_path, text: str) -> Path:
    p = tmp_path / "m.toml"
    p.write_text(text)
    return p


def entries(text: str) -> list[dict]:
    return tomllib.loads(text)["mapping"]


def one_entry_file(text: str, family: str) -> str:
    [e] = [e for e in entries(text) if e["family"] == family]
    return "version = 2\n\n[[mapping]]\n" + "\n".join(
        f"{k} = {mp._toml(v)}" for k, v in e.items()) + "\n"


# ------------------------------------------------------------------ the script


def test_script_writes_every_output_offline_and_approves_nothing(tmp_path, monkeypatch, capsys):
    def no_socket(*a, **k):
        raise AssertionError("the proposal tool must not open a socket")

    monkeypatch.setattr(socket, "socket", no_socket)
    monkeypatch.setattr(socket, "create_connection", no_socket)
    out = tmp_path / "out"
    code = _load().main([str(FIXTURE), "--out", str(out), "--roll-days", "5",
                         "--price", "SPX=5000"])
    assert code == 0
    names = sorted(p.name for p in out.iterdir())
    assert names == ["approval_GOLD.md", "approval_NATGAS.md", "approval_SPX.md",
                     "approval_TLT.md", "approval_WTI.md", "contract_table.md",
                     "proposed_mapping.toml", "roll_calendar.md"]
    es = entries((out / "proposed_mapping.toml").read_text())
    assert [e["family"] for e in es] == ["TLT", "SPX", "WTI", "NATGAS", "GOLD"]
    assert all(e["approved"] is False and "approved_by" not in e for e in es)
    assert "approved=false" in capsys.readouterr().out


def test_module_and_script_need_no_ibapi():
    code = ("import sys\n"
            "import tradingbot.runtime.mapping_proposal\n"
            "assert not any(m == 'ibapi' or m.startswith('ibapi.') for m in sys.modules)\n")
    r = subprocess.run([sys.executable, "-c", code], check=False, cwd=ROOT,
                       capture_output=True, text=True,
                       env={"PYTHONPATH": str(ROOT / "src"), "PATH": "/usr/bin:/bin"})
    assert r.returncode == 0, r.stderr


def test_bad_inputs_exit_2(tmp_path, capsys):
    bad = tmp_path / "r.json"
    bad.write_text(json.dumps({"schema": "something/else", "results": []}))
    assert _load().main([str(bad), "--out", str(tmp_path / "o")]) == 2
    assert _load().main([str(FIXTURE), "--out", str(tmp_path / "o"), "--price", "SPX=-1"]) == 2
    assert _load().main([str(FIXTURE), "--out", str(tmp_path / "o"),
                         "--family", "SPX:PROXY:SPY"]) == 2
    assert "refused" in capsys.readouterr().err


# ------------------------------------------------------------------ a proposal cannot approve


@pytest.mark.parametrize("extra", [{"approved": True}, {"approved_by": "x"},
                                   {"owner": {"approved": True}},
                                   {"owner": {"entry_rule": "REFERENCE_AS_COLLAR"}}])
def test_the_policy_can_never_carry_an_approval(extra):
    raw = {"schema": mp.POLICY_SCHEMA, "family": [
        {"family": "SPX", "treatment": "DIRECT", "execution": "MES fut", **extra}]}
    with pytest.raises(mp.ProposalInputInvalid):
        mp.parse_policy(raw)


def test_unknown_treatment_and_duplicate_family_refuse():
    with pytest.raises(mp.ProposalInputInvalid, match="treatment"):
        mp.parse_family_spec("SPX:UNSUPPORTED:MES fut")
    pol = mp.ProposalPolicy([mp.parse_family_spec("SPX:DIRECT:SPY")] * 2)
    with pytest.raises(mp.ProposalInputInvalid, match="twice"):
        mp.build(catalog(), pol)


# ------------------------------------------------------------------ the real loader and gateway


def test_raw_proposal_entries_with_open_inputs_are_refused_by_the_real_loader(tmp_path):
    text = mp.render_toml(mp.build(catalog(), shipped()), catalog())
    with pytest.raises(ValueError, match="owner_input_required"):
        MappingPolicy.from_toml(write(tmp_path, text))
    gold = one_entry_file(text, "GOLD")  # XAUUSD: the report states no multiplier
    assert mp.BROKER_FACT in gold and "broker_fact_missing" in gold
    with pytest.raises(ValueError):
        MappingPolicy.from_toml(write(tmp_path, gold))
    # TLT (a share quoted in its own price, no sizing) has nothing open: it loads, unapproved.
    tlt = MappingPolicy.from_toml(write(tmp_path, one_entry_file(text, "TLT")))
    assert tlt.entries["TLT"].status_label == "NOT APPROVED" and tlt.executable_assets == []


def test_filled_proposal_loads_through_the_real_loader_as_unapproved(tmp_path):
    cat = catalog()
    pol = mp.merge_cli(filled_policy(), ["SPY.OPT:DIRECT:SPY option chain"], None, {})
    props = mp.build(cat, pol)
    opt = next(p for p in props if p.plan.family == "SPY.OPT")
    assert "execution.min_tick: not in an option-chain report" in opt.broker_missing
    text = mp.render_toml([p for p in props if p.plan.family != "SPY.OPT"], cat)
    body = "\n".join(line for line in text.splitlines() if not line.startswith("#"))
    assert mp.PLACEHOLDER not in body and mp.BROKER_FACT not in body
    loaded = MappingPolicy.from_toml(write(tmp_path, text))
    assert {f: e.status_label for f, e in loaded.entries.items()} == {
        f: "NOT APPROVED" for f in ("TLT", "SPX", "WTI", "NATGAS", "GOLD")}
    assert loaded.executable_assets == []
    wti = loaded.entries["WTI"]
    assert wti.same_series and sorted(wti.equivalent_roots) == ["CL", "MCL"]
    assert [(c.month, c.roll_date) for c in wti.contracts] == [
        ("202701", date(2026, 12, 9)), ("202702", date(2027, 1, 12))]
    assert loaded.entries["SPX"].execution.multiplier == Decimal(5)
    assert loaded.entries["NATGAS"].execution.symbol == "MHNG"


FAMILY_INSTRUMENTS = {
    "SPX": (Instrument(asset_class=AssetClass.FUTURE, symbol="MES", exchange="CME",
                       futures_contract="202612"), "5000", "4950"),
    "WTI": (Instrument(asset_class=AssetClass.FUTURE, symbol="MCL", exchange="NYMEX",
                       futures_contract="202701"), "70.00", "69.00"),
    "NATGAS": (Instrument(asset_class=AssetClass.FUTURE, symbol="MHNG", exchange="NYMEX",
                          futures_contract="202701"), "3.000", "2.900"),
    "GOLD": (Instrument(asset_class=AssetClass.FUTURE, symbol="MGC", exchange="COMEX",
                        futures_contract="202612"), "2000.0", "1980.0"),
}


@pytest.mark.parametrize("family", sorted(FAMILY_INSTRUMENTS))
def test_every_proposed_family_holds_in_the_mapping_stage_and_the_gateway(tmp_path, family):
    loaded = MappingPolicy.from_toml(write(tmp_path, mp.render_toml(
        mp.build(catalog(), filled_policy()), catalog())))
    inst, entry, stop = FAMILY_INSTRUMENTS[family]
    now = datetime(2026, 11, 17, 15, 0, tzinfo=UTC)
    action = TradeAction(
        action_id="a1", action=Action.OPEN, timing=Timing.CURRENT, instrument=inst,
        position_side=PositionSide.LONG, price_basis=PriceBasis.TRADED_CONTRACT,
        source_market_symbol=family, entry_reference=entry, stop_price=stop,
        explicit_instruction=True, evidence=["invented"])
    decision, mapped = loaded.resolve(action, clause=f"long {family} {inst.futures_contract}",
                                      now=now)
    assert decision.status == "NOT_APPROVED" and decision.reason == MAPPING_NOT_APPROVED
    assert mapped is None
    e = loaded.entries[family]
    x = e.execution
    contract = ContractRef(symbol=x.symbol, asset_class=x.asset, sec_type=x.sec_type,
                           exchange=x.exchange, con_id=1, expiry=inst.futures_contract,
                           multiplier=x.multiplier, min_tick=x.min_tick)
    ref = None
    if e.basis != "DIRECT":
        ref = ContractRef(symbol=e.reference.symbol, asset_class=e.reference.asset,
                          sec_type=e.reference.sec_type, exchange=e.reference.exchange, con_id=2)
    terms = e.terms(inst, contract, ref)
    px = Decimal(entry)
    q = MappedQuote(symbol=x.symbol, bid=px - x.min_tick, ask=px, observed_at=now,
                    quality="LIVE", terms=terms)
    g = gate_action(action, now - timedelta(seconds=2), q,
                    MockPolicy(max_notional_usd=Decimal(100000)), None, now=now)
    assert g.status == "HOLD" and g.reasons[0] == MAPPING_NOT_APPROVED and g.plan is None


# ------------------------------------------------------------------ facts, NOT_FOUND, absent


def test_a_not_found_vehicle_is_an_unsupported_entry_with_the_broker_code(tmp_path):
    cat = catalog()
    pol = shipped(specs=["NATGAS:DIRECT:MNG fut (micro NG):NG fut"])
    [p] = [p for p in mp.build(cat, pol) if p.plan.family == "NATGAS"]
    assert p.entry["basis"] == "UNSUPPORTED" and p.entry["approved"] is False
    assert "NOT_FOUND (code 200)" in p.unsupported_reason
    loaded = MappingPolicy.from_toml(write(tmp_path, one_entry_file(
        mp.render_toml([p], cat), "NATGAS")))
    assert loaded.entries["NATGAS"].status_label == "UNSUPPORTED"
    table = mp.render_contract_table([p], cat)
    assert "NOT_FOUND row: status `NOT_FOUND`, broker code 200" in table
    assert "Not proposable" in mp.render_sheet(p, cat)


def test_a_query_absent_from_the_report_or_an_error_row_is_never_substituted():
    cat = catalog()
    pol = shipped(specs=["SPX:REFERENCE_TRIGGER:IBUS500 CFD:SPX index",
                         "GOLD:DIRECT:no such query"])
    by = {p.plan.family: p for p in mp.build(cat, pol)}
    assert by["SPX"].entry["basis"] == "UNSUPPORTED" and "ERROR (code 10299)" in \
        by["SPX"].unsupported_reason
    assert by["GOLD"].entry["basis"] == "UNSUPPORTED" and "NOT_IN_REPORT" in \
        by["GOLD"].unsupported_reason


def test_absent_fields_are_shown_absent_and_never_filled():
    cat = catalog()
    props = mp.build(cat, shipped())
    table = mp.render_contract_table(props, cat)
    assert "| 910003 | SPX | SPX | IND | CBOE | absent |" in table
    gold = next(p for p in props if p.plan.family == "GOLD")
    assert gold.entry["execution"]["multiplier"] == mp.BROKER_FACT
    assert "multiplier absent" in mp.render_sheet(gold, cat)


def test_months_that_disagree_on_a_rule_leave_it_a_missing_broker_fact():
    cat = catalog()
    row = next(r for r in cat["results"] if r["name"] == "MCL fut")
    row["contracts"][1]["multiplier"] = "1000"
    [p] = [p for p in mp.build(cat, shipped()) if p.plan.family == "WTI"]
    assert p.entry["execution"]["multiplier"] == mp.BROKER_FACT
    assert any("2 different values" in m for m in p.broker_missing)


def test_a_failed_report_is_flagged():
    cat = catalog()
    cat.update(overall="FAILED", failure="connect: INVENTED")
    assert "WARNING: the report is FAILED" in mp.render_contract_table(
        mp.build(cat, shipped()), cat)


# ------------------------------------------------------------------ roll calendar


def test_business_days_skip_weekends_and_listed_holidays():
    fri = date(2026, 12, 18)
    assert mp.business_days_before(fri, 5, []) == date(2026, 12, 11)
    assert mp.business_days_before(date(2026, 12, 21), 1, []) == fri  # Monday -> Friday
    assert mp.business_days_before(fri, 5, [date(2026, 12, 14)]) == date(2026, 12, 10)
    assert mp.business_days_before(fri, 0, []) == fri


def test_roll_calendar_flags_first_notice_and_never_guesses():
    cat = catalog()
    row = next(r for r in cat["results"] if r["name"] == "CL fut")
    mcl = next(r for r in cat["results"] if r["name"] == "MCL fut")
    mcl["contracts"][1]["contract_month"] = ""
    mcl["contracts"][1]["last_trade_date"] = ""
    mcl["contracts"][1]["last_trade_date_or_contract_month"] = "202702"
    assert row  # the reference rows are not rolled; only the execution vehicle is
    [p] = [p for p in mp.build(cat, shipped()) if p.plan.family == "WTI"]
    assert all(mp.FND_FLAG in r["flags"] for r in p.calendar)
    late = [r for r in p.calendar if r["contract_month"] is None]
    assert late and "last-trade date not in report: no roll date proposed" in late[0]["flags"]
    assert [c["month"] for c in p.entry["contracts"]] == ["202701"]  # never inferred from 202702
    assert mp.FND_FLAG in mp.render_calendar([p], shipped(), cat)


def test_without_the_owner_parameter_every_roll_date_is_an_owner_input():
    pol = shipped(roll_days=None)
    [p] = [p for p in mp.build(catalog(), pol) if p.plan.family == "SPX"]
    assert all(c["roll_date"] == mp.PLACEHOLDER for c in p.entry["contracts"])
    assert "contracts.roll_date" in p.owner_inputs
    assert "no rule parameter set" in mp.render_calendar([p], pol, catalog())


# ------------------------------------------------------------------ the approval sheet


def test_sheet_dollar_values_come_from_report_facts_and_an_owner_price():
    cat = catalog()
    pol = mp.merge_cli(shipped(), [], 5, {"WTI": Decimal(70)})
    by = {p.plan.family: p for p in mp.build(cat, pol)}
    wti = mp.render_sheet(by["WTI"], cat)
    assert "USD 100 per 1.00 price point" in wti and "USD 1 per tick" in wti
    assert "0.01 x 70 x 100 = **USD 70.00**" in wti and "not a market price" in wti
    assert "same_series_reason" in wti and "FRONT_MONTH" in wti
    spx = mp.render_sheet(by["SPX"], cat)
    assert "USD 0.05 x P" in spx and "--price SPX=P" in spx  # no price invented
    assert "Reference hours" in spx and "Market data" in spx and "Trading permission" in spx


def test_cli_replacing_a_vehicle_drops_owner_inputs_decided_for_another():
    pol = mp.merge_cli(filled_policy(), ["GOLD:DIRECT:XAUUSD spot gold"], None, {})
    gold = next(f for f in pol.families if f.family == "GOLD")
    assert gold.owner == {} and gold.source == "command line"


def test_written_toml_round_trips():
    base = copy.deepcopy(mp.build(catalog(), shipped())[1].entry)
    assert tomllib.loads("x = " + mp._toml(base["contracts"]))["x"][0]["month"] == "202612"
