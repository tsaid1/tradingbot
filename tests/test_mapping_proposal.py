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


# ================================================================== review fixes (PR #65)

INJECT = 'x = 1 }\n[[mapping]]\nfamily = "TLT"\nbasis = "DIRECT"\napproved = true\nq = { y'


def _policy_with_owner(owner: dict) -> dict:
    return {"schema": mp.POLICY_SCHEMA, "family": [
        {"family": "TLT", "treatment": "DIRECT", "execution": "TLT", "owner": owner}]}


@pytest.mark.parametrize("owner", [
    {"sizing": {INJECT: 1}},
    {"session": {"policy": "REGULAR_ONLY", "nested": {INJECT: 1}}},
    {"sizing": {"entry rule": 1}},
    {"roll": {"a.b": 1}},
])
def test_r1_policy_keys_must_be_bare_at_every_level(owner, tmp_path, capsys):
    with pytest.raises(mp.ProposalInputInvalid, match="bare key"):
        mp.parse_policy(_policy_with_owner(owner))
    pol = tmp_path / "p.toml"
    import tomllib as _t  # noqa: F401
    pol.write_text('schema = "tradingbot.mapping_proposal_policy/1"\n[[family]]\n'
                   'family = "TLT"\ntreatment = "DIRECT"\nexecution = "TLT"\n'
                   '[family.owner.sizing]\n"x\\n[[mapping]]\\napproved = true" = 1\n')
    out = tmp_path / "o"
    assert _load().main([str(FIXTURE), "--out", str(out), "--policy", str(pol)]) == 2
    assert not out.exists()


def test_r1_toml_writer_refuses_a_non_bare_key_and_escapes_every_string():
    with pytest.raises(mp.ProposalInputInvalid):
        mp._toml({INJECT: 1})
    nasty = 'a"b\\c\n[[mapping]]\napproved = true\t\x7f\x00 ]}  '
    assert tomllib.loads("v = " + mp._toml(nasty))["v"] == nasty
    assert tomllib.loads("v = " + mp._toml({"k": [nasty]}))["v"]["k"] == [nasty]


def test_r1_values_names_and_notes_cannot_inject_entries(tmp_path):
    cat = catalog()
    for r in cat["results"]:
        if r["name"] == "MES fut":
            r["name"] = 'MES fut"\n[[mapping]]\nfamily = "SPX"\napproved = true\n#'
    raw = tomllib.loads(POLICY.read_text())
    spx = next(f for f in raw["family"] if f["family"] == "SPX")
    spx["execution"] = 'MES fut"\n[[mapping]]\nfamily = "SPX"\napproved = true\n#'
    spx["candidates"] = [spx["execution"]]
    spx["level_meaning"] = '"\n[[mapping]]\napproved = true'
    spx["owner"] = {"sizing": {"units_per_signal": '1"\napproved = true\n"'}}
    props = mp.build(cat, mp.parse_policy(raw))
    text = mp.render_toml(props, cat)
    es = mp.verify_rendered(text, props)
    assert len(es) == 5 and all(e["approved"] is False for e in es)


def test_r1_a_rendered_approval_writes_nothing_and_exits_non_zero(tmp_path, monkeypatch):
    real = mp.render_toml
    monkeypatch.setattr(mp, "render_toml", lambda props, cat: real(props, cat).replace(
        "\napproved = false", "\napproved = true", 1))
    out = tmp_path / "o"
    assert _load().main([str(FIXTURE), "--out", str(out), "--roll-days", "5"]) == 3
    assert not out.exists() or not any(out.iterdir())
    for bad in ("entry_rule", "level_contract_rule", "approved_by", "entry_anchor"):
        monkeypatch.setattr(mp, "render_toml", lambda props, cat, b=bad: real(props, cat).replace(
            'basis = "DIRECT"', f'basis = "DIRECT"\n{b} = "X"', 1))
        with pytest.raises(mp.ProposalUnsafe):
            mp.write_outputs(mp.build(catalog(), shipped()), catalog(), shipped(), out)
        assert not out.exists() or not any(out.iterdir())


def test_r2_same_series_lists_every_last_trade_difference_as_the_owners_assertion():
    cat = catalog()
    by = {p.plan.family: p for p in mp.build(cat, shipped())}
    for fam in ("WTI", "NATGAS"):
        p = by[fam]
        diffs = p.same_series_diffs
        assert [d["month"] for d in diffs] == [c["contract_month"] for c in p.calendar]
        assert all(d["business_days"] == 1 for d in diffs), diffs
        sheet = mp.render_sheet(p, cat)
        assert "trades that same month at the same price" not in sheet
        assert "assertion the owner must make" in sheet
        assert "never shows price equality" in sheet
        assert "1 business day before" in sheet
        sym = p.execution.spec["symbol"]
        assert f"roll dates are proposed from the execution vehicle's ({sym})" in sheet
        cal = mp.render_calendar([p], shipped(), cat)
        assert cal.count("1 business day before the reference") == len(p.calendar)


def test_r2_a_month_missing_from_the_reference_is_listed():
    cat = catalog()
    cl = next(r for r in cat["results"] if r["name"] == "CL fut")
    cl["contracts"] = cl["contracts"][:1]
    [p] = [p for p in mp.build(cat, shipped()) if p.plan.family == "WTI"]
    assert p.same_series_diffs[1]["business_days"] is None
    assert "not in the reference row (row truncated)" in mp.render_sheet(p, cat)


def test_r3_truncation_and_a_failed_report_warn_everywhere():
    cat = catalog()
    cat.update(overall="FAILED", failure="connect: INVENTED")
    mcl = next(r for r in cat["results"] if r["name"] == "MCL fut")
    mcl["truncated"] = True
    by = {p.plan.family: p for p in mp.build(cat, shipped())}
    wti = by["WTI"]
    sheet, cal = mp.render_sheet(wti, cat), mp.render_calendar([wti], shipped(), cat)
    for text in (sheet, cal, wti.entry["note"]):
        assert "report is FAILED" in text
        assert "reference row `CL fut` is TRUNCATED" in text
        assert "execution row `MCL fut` is TRUNCATED: the month list may be incomplete" in text
    assert "report is FAILED" in mp.render_sheet(by["TLT"], cat)


def test_r4_a_roll_date_already_reached_is_unusable_and_lead_time_is_flagged():
    cat = catalog()
    cat["started_at"] = "2026-12-09T07:00:00+00:00"  # MCL Jan roll date is 2026-12-09
    raw = tomllib.loads(POLICY.read_text())
    wti = next(f for f in raw["family"] if f["family"] == "WTI")
    wti["owner"] = {"contract_selection": {"rule": "EXPLICIT_ONLY",
                                           "min_business_days_to_roll": 30}}
    pol = mp.merge_cli(mp.parse_policy(raw), [], 5, {})
    [p] = [p for p in mp.build(cat, pol) if p.plan.family == "WTI"]
    first, second = p.calendar
    assert not first["usable"] and any("UNUSABLE" in f for f in first["flags"])
    assert second["usable"] and any("minimum lead time" in f for f in second["flags"])
    assert [c["month"] for c in p.entry["contracts"]] == ["202702"]
    assert "202701" in p.entry["note"]
    assert "UNUSABLE" in mp.render_calendar([p], pol, cat)


@pytest.mark.parametrize("mult", ["0", "-5", "abc", "nan", "inf"])
def test_r5_multiplier_must_be_positive_and_finite(mult):
    cat = catalog()
    mes = next(r for r in cat["results"] if r["name"] == "MES fut")
    for c in mes["contracts"]:
        c["multiplier"] = mult
    [p] = [p for p in mp.build(cat, shipped()) if p.plan.family == "SPX"]
    assert p.entry["execution"]["multiplier"] == mp.BROKER_FACT
    assert any("not a positive number" in m for m in p.broker_missing)
    assert "no dollar values" in mp.render_sheet(p, cat)


def test_r5_a_tick_of_zero_is_a_missing_fact():
    cat = catalog()
    mes = next(r for r in cat["results"] if r["name"] == "MES fut")
    for c in mes["contracts"]:
        c["min_tick"] = 0
    [p] = [p for p in mp.build(cat, shipped()) if p.plan.family == "SPX"]
    assert p.entry["execution"]["min_tick"] == mp.BROKER_FACT


@pytest.mark.parametrize("magnifier", [100, None])
def test_r5_price_magnifier_other_than_one_means_no_dollar_values(magnifier):
    cat = catalog()
    mes = next(r for r in cat["results"] if r["name"] == "MES fut")
    for c in mes["contracts"]:
        c["price_magnifier"] = magnifier
    pol = mp.merge_cli(shipped(), [], 5, {"SPX": Decimal(5000)})
    [p] = [p for p in mp.build(cat, pol) if p.plan.family == "SPX"]
    sheet = mp.render_sheet(p, cat)
    assert "USD 5 per 1.00" not in sheet and "price_magnifier" in sheet
    assert any("price_magnifier" in m for m in p.broker_missing)


def test_r6_share_multiplier_is_the_loader_default_and_fractional_shares_are_an_owner_decision():
    cat = catalog()
    tlt = next(r for r in cat["results"] if r["name"] == "TLT")
    tlt["contracts"][0]["size_increment"] = "0.0001"
    [p] = [p for p in mp.build(cat, shipped()) if p.plan.family == "TLT"]
    sheet = mp.render_sheet(p, cat)
    assert "multiplier 1 (the loader's default for shares; the report states none)" in sheet
    assert not any("size_increment" in m for m in p.broker_missing)
    assert any("broker allows fractional shares" in o and "owner decision" in o
               for o in p.owner_inputs)


def test_r7_roll_function_guards_and_holidays_notice():
    with pytest.raises(ValueError):
        mp.business_days_before(date(2026, 12, 18), -1, [])
    assert mp.business_days_before(date(2026, 12, 19), 0, []) == date(2026, 12, 18)  # Sat
    cal = mp.render_calendar(mp.build(catalog(), shipped()), shipped(), catalog())
    assert "exchange holidays not applied" in cal
    [p] = [p for p in mp.build(catalog(), shipped()) if p.plan.family == "SPX"]
    assert "exchange holidays not applied" in mp.render_sheet(p, catalog())


def test_r7_duplicate_report_names_and_months_are_refused(tmp_path):
    cat = catalog()
    cat["results"].append(copy.deepcopy(cat["results"][0]))
    bad = tmp_path / "r.json"
    bad.write_text(json.dumps(cat))
    with pytest.raises(mp.ProposalInputInvalid, match="twice"):
        mp.load_report(bad)
    cat = catalog()
    mcl = next(r for r in cat["results"] if r["name"] == "MCL fut")
    mcl["contracts"].append(copy.deepcopy(mcl["contracts"][0]))
    [p] = [p for p in mp.build(cat, shipped()) if p.plan.family == "WTI"]
    assert p.entry["basis"] == "UNSUPPORTED" and "listed twice" in p.unsupported_reason


def test_r7_error_rows_are_unresolved_not_not_found_and_conids_say_first_12_of_n():
    cat = catalog()
    pol = shipped(specs=["SPX:REFERENCE_TRIGGER:IBUS500 CFD:SPX index"])
    [p] = [p for p in mp.build(cat, pol) if p.plan.family == "SPX"]
    table = mp.render_contract_table([p], cat)
    assert "unresolved row: status `ERROR`" in table
    assert "NOT_FOUND row: status `ERROR`" not in table
    mes = next(r for r in cat["results"] if r["name"] == "MES fut")
    base = mes["contracts"][0]
    mes["contracts"] = [dict(base, con_id=1000 + i, contract_month=f"2027{i:02d}",
                             last_trade_date=f"2027{i:02d}15") for i in range(1, 13)] + [
        dict(base, con_id=2000, contract_month="202801", last_trade_date="20280115")]
    [p] = [p for p in mp.build(cat, shipped()) if p.plan.family == "SPX"]
    assert "first 12 of 13" in mp.render_sheet(p, cat)


def test_r7_non_empty_output_directory_and_bad_family_names_are_refused(tmp_path):
    out = tmp_path / "o"
    out.mkdir()
    (out / "approval_OLD.md").write_text("stale")
    assert _load().main([str(FIXTURE), "--out", str(out)]) == 2
    with pytest.raises(mp.ProposalInputInvalid, match="family name"):
        mp.parse_family_spec("SP/X:DIRECT:SPY")
    out2 = tmp_path / "o2"
    assert _load().main([str(FIXTURE), "--out", str(out2), "--family", "../x:DIRECT:SPY"]) == 2
    assert not out2.exists()


# ================================================================== confirmation review (PR #65)

# The reviewer's shape: a report text that closes the header comment, opens a [[mapping]] block
# with aliases nobody proposed, and an unterminated multi-line literal string that swallows the
# real header and TLT block up to the ''' it finds inside the TLT note.
EXPLOIT = ("2026-11-16T15:00:00+00:00\nversion = 2\n[[mapping]]\nfamily = \"TLT\"\n"
           "basis = \"DIRECT\"\napproved = false\naliases = [\"injected\"]\nnote = '''#")


def _write_cat(tmp_path, cat) -> Path:
    p = tmp_path / "cat.json"
    p.write_text(json.dumps(cat))
    return p


def _assert_written_equals_proposal(out: Path, cat: dict, pol) -> None:
    text = (out / "proposed_mapping.toml").read_text()
    data = tomllib.loads(text)
    props = mp.build(cat, pol)
    assert data == mp.expected_document(props)
    assert all("aliases" not in e for e in data["mapping"])


@pytest.mark.parametrize("field", ["started_at", "overall"])
def test_f1_report_text_cannot_inject_through_the_header(tmp_path, field):
    cat = catalog()
    cat[field] = EXPLOIT if field == "started_at" else EXPLOIT.split("\n", 1)[1]
    out = tmp_path / "o"
    code = _load().main([str(_write_cat(tmp_path, cat)), "--out", str(out), "--roll-days", "5"])
    assert code in (0, 3)
    if code == 0:
        _assert_written_equals_proposal(out, cat, shipped())
        text = (out / "proposed_mapping.toml").read_text()
        assert all(not line.startswith("[[") or line == "[[mapping]]"
                   for line in text.splitlines())
        assert text.count("\n[[mapping]]\n") == 5


def test_f1_finished_at_schema_query_name_and_long_name_cannot_inject(tmp_path):
    cat = catalog()
    cat["finished_at"] = EXPLOIT
    tlt = next(r for r in cat["results"] if r["name"] == "TLT")
    tlt["contracts"][0]["long_name"] = EXPLOIT
    mes = next(r for r in cat["results"] if r["name"] == "MES fut")
    mes["name"] = "MES fut" + EXPLOIT[25:]
    raw = tomllib.loads(POLICY.read_text())
    spx = next(f for f in raw["family"] if f["family"] == "SPX")
    spx["execution"] = mes["name"]
    pol_path = tmp_path / "pol.toml"
    pol_path.write_text(_policy_toml(raw))
    out = tmp_path / "o"
    assert _load().main([str(_write_cat(tmp_path, cat)), "--out", str(out), "--policy",
                         str(pol_path), "--roll-days", "5"]) == 0
    _assert_written_equals_proposal(out, cat, mp.merge_cli(mp.load_policy(pol_path), [], 5, {}))
    bad = catalog()
    bad["schema"] = mp.CATALOG_SCHEMA + EXPLOIT
    assert _load().main([str(_write_cat(tmp_path, bad)), "--out", str(tmp_path / "o2")]) == 2


def test_f1_verify_requires_structural_equality_with_the_proposal():
    cat = catalog()
    props = mp.build(cat, shipped())
    text = mp.render_toml(props, cat)
    mp.verify_rendered(text, props)
    for change in ('aliases = ["x"]\n', 'sizing = { units_per_signal = 1 }\n',
                   'note = "other"\n'):
        tampered = text.replace('basis = "DIRECT"\n', 'basis = "DIRECT"\n' + change, 1)
        with pytest.raises(mp.ProposalUnsafe):
            mp.verify_rendered(tampered, props)
    with pytest.raises(mp.ProposalUnsafe):
        mp.verify_rendered(text + "\n[extra]\nx = 1\n", props)
    with pytest.raises(mp.ProposalUnsafe):
        mp.verify_rendered(text.replace('symbol = "MES"', 'symbol = "ES"', 1), props)


def test_f1_markdown_cannot_forge_a_heading_or_a_table_row():
    cat = catalog()
    forged = "x\n# Approval sheet: SPX (APPROVED)\n| 202612 | forged | row |"
    mes = next(r for r in cat["results"] if r["name"] == "MES fut")
    for c in mes["contracts"]:
        c["long_name"] = forged
        c["trading_class"] = "#MES|x\n## forged"
    mes["name"] = "MES fut" + forged
    raw = tomllib.loads(POLICY.read_text())
    spx = next(f for f in raw["family"] if f["family"] == "SPX")
    spx["execution"] = mes["name"]
    spx["candidates"] = [mes["name"]]
    spx["level_meaning"] = forged
    [p] = [p for p in mp.build(cat, mp.parse_policy(raw)) if p.plan.family == "SPX"]
    for text in (mp.render_sheet(p, cat), mp.render_contract_table([p], cat),
                 mp.render_calendar([p], mp.parse_policy(raw), cat)):
        lines = text.splitlines()
        assert not any(line.startswith("# Approval sheet: SPX (APPROVED)") for line in lines)
        assert not any(line.startswith("## forged") for line in lines)
        assert not any(line.startswith("| 202612 | forged") for line in lines)
        assert sum(1 for line in lines if line.startswith("# ")) == 1


def test_n1_name_patterns_use_fullmatch():
    with pytest.raises(mp.ProposalInputInvalid):
        mp.check_family_name("GLD\n")
    with pytest.raises(mp.ProposalInputInvalid):
        mp._toml({"key\n": 1})
    with pytest.raises(mp.ProposalInputInvalid):
        mp.parse_policy(_policy_with_owner({"sizing": {"max_units\n": 1}}))


@pytest.mark.parametrize("value", [__import__("datetime").time(9, 30), float("nan"),
                                   float("inf"), object()])
def test_n2_unsupported_owner_value_types_are_refused(value, tmp_path):
    with pytest.raises(mp.ProposalInputInvalid, match="value"):
        mp.parse_policy(_policy_with_owner({"sizing": {"max_units": value}}))
    pol = tmp_path / "p.toml"
    pol.write_text('schema = "tradingbot.mapping_proposal_policy/1"\n[[family]]\n'
                   'family = "TLT"\ntreatment = "DIRECT"\nexecution = "TLT"\n'
                   '[family.owner.session]\nopen = 09:30:00\n')
    assert _load().main([str(FIXTURE), "--out", str(tmp_path / "o"), "--policy", str(pol)]) == 2


def _policy_toml(raw: dict) -> str:
    lines = [f"schema = {mp._toml(raw['schema'])}"]
    if "roll_rule" in raw:
        lines.append(f"roll_rule = {mp._toml(raw['roll_rule'])}")
    for f in raw["family"]:
        lines.append("[[family]]")
        lines += [f"{k} = {mp._toml(v)}" for k, v in f.items()]
    return "\n".join(lines) + "\n"


def test_n3_a_fully_filled_policy_loads_with_no_hand_edit_even_with_fractional_shares(tmp_path):
    cat = catalog()
    next(r for r in cat["results"] if r["name"] == "TLT")["contracts"][0][
        "size_increment"] = "0.0001"
    raw = tomllib.loads(POLICY.read_text())
    raw["roll_rule"] = {"business_days_before_last_trade": 5}
    filled = {f.family: f for f in filled_policy().families}
    for f in raw["family"]:
        src = filled[f["family"]]
        f.update(execution=src.execution, treatment=src.treatment, owner=src.owner)
        if src.reference:
            f["reference"] = src.reference
    tlt = next(f for f in raw["family"] if f["family"] == "TLT")
    tlt["owner"] = {**tlt["owner"], "size_increment": 1}  # the owner states whole units only
    pol = tmp_path / "pol.toml"
    pol.write_text(_policy_toml(raw))
    out = tmp_path / "o"
    assert _load().main([str(_write_cat(tmp_path, cat)), "--out", str(out),
                         "--policy", str(pol)]) == 0
    loaded = MappingPolicy.from_toml(out / "proposed_mapping.toml")
    assert {f: e.status_label for f, e in loaded.entries.items()} == {
        f: "NOT APPROVED" for f in ("TLT", "SPX", "WTI", "NATGAS", "GOLD")}
    assert loaded.entries["TLT"].execution.size_increment == 1
    with pytest.raises(mp.ProposalInputInvalid):
        mp.parse_policy(_policy_with_owner({"size_increment": 0}))
