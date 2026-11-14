"""The risk gates in the order path: gateway, coordinator, simulated account, shadow, config and
the read-only adapter (docs/RISK_GATES.md). Every value here is INVENTED test data."""
from __future__ import annotations

import json
import tomllib
from copy import deepcopy
from decimal import Decimal as D

import pytest
from _risk_fixture import INVENTED_RISK, INVENTED_RISK_TABLE, TOML_TABLE
from execution_harness import SYMBOL, Harness, action, open_action

from tradingbot.execution.broker import ContractRef, OrderIntent, OrderSide, OrderType
from tradingbot.execution.sim_broker import SimBroker, SimClock
from tradingbot.models import Action, AssetClass, MockPolicy, PositionSide
from tradingbot.risk import RiskPolicy
from tradingbot.risk import gates as G
from tradingbot.runtime.config import ConfigError, parse_config


def failing_table() -> RiskPolicy:
    """Invented: a table under which every gate fails for an add to a stock idea in the sim."""
    t = deepcopy(INVENTED_RISK_TABLE)
    t.update({"environments": ["PAPER"], "max_portfolio_risk": "0.01",
              "borrowing": {"permitted": False, "short_sale_permitted": False}})
    return RiskPolicy.from_table(t)


def risk_events(h: Harness) -> list[dict]:
    return [json.loads(e.detail) if isinstance(e.detail, str) else e.detail
            for e in h.coord.store.journal() if e.event == "RISK_GATES"]


# ----------------------------------------------------------------------------- the coordinator

def test_baseline_cash_stock_entry_is_unchanged_and_records_the_gates(tmp_path):
    h = Harness(tmp_path)
    out = h.execute("p1", open_action())
    assert out.replay_status == "EXECUTED"
    [rec] = risk_events(h)
    assert rec["baseline"] is True and rec["stage"] == "EXECUTION"
    assert {g["status"] for g in rec["gates"].values()} == {"BASELINE"}
    assert rec["gross_notional"] is not None  # recorded; no equity figure in the baseline sim
    assert rec["economic_leverage"] is None and rec["borrowing"] is None


def test_baseline_refuses_a_short_stock_entry_at_the_last_check(tmp_path):
    h = Harness(tmp_path)
    out = h.execute("p1", open_action(side=PositionSide.SHORT, entry="49.98", stop="52",
                                      target="45"))
    assert out.replay_status != "EXECUTED" and h.broker.submit_calls == 0
    assert G.SHORT_SALE_NOT_PERMITTED in h.coord.intent(out.order_ref).reasons


def test_baseline_refuses_any_entry_to_a_broker_declaring_live(tmp_path):
    h = Harness(tmp_path)
    h.broker.environment = lambda: "LIVE"
    out = h.execute("p1", open_action())
    assert f"{G.ENVIRONMENT_NOT_AUTHORIZED}:LIVE" in h.coord.intent(out.order_ref).reasons
    assert h.broker.submit_calls == 0


def test_exits_reductions_and_stop_tightening_pass_with_every_gate_failing(tmp_path):
    h = Harness(tmp_path)
    h.execute("p1", open_action())
    pos = h.coord.store.position("p1:open")
    assert pos.quantity == 4
    h.coord.policy = h.coord.policy.model_copy(update={"risk": failing_table()})
    add = h.execute("p2", action("add", Action.ADD, idea="p1:open", entry="50.02"))
    reasons = h.coord.intent(add.order_ref).reasons
    gates = risk_events(h)[-1]["gates"]
    assert {g for g, r in gates.items() if r["status"] == "FAIL"} == {f"G{i}" for i in range(1, 10)}
    assert G.FAILED in reasons
    tighten = h.execute("p3", action("mod", Action.MODIFY, idea="p1:open", stop="49"))
    assert tighten.replay_status == "EXECUTED"
    reduce = h.execute("p4", action("cut", Action.REDUCE, idea="p1:open", fraction="0.5"))
    assert reduce.replay_status == "EXECUTED"
    close = h.execute("p5", action("out", Action.CLOSE, idea="p1:open"))
    assert close.replay_status == "EXECUTED"
    assert h.coord.store.position("p1:open").quantity == 0


def test_under_a_table_a_broker_only_position_makes_portfolio_risk_unbounded(tmp_path):
    from tradingbot.execution.broker import OrderIntent, OrderType
    h = Harness(tmp_path, risk_families=((SYMBOL, "STK", "1"),))
    h.broker.submit(OrderIntent(order_ref="FOREIGN", contract=h.broker.resolve(
        open_action().instrument), side=OrderSide.BUY, quantity=1, order_type=OrderType.MKT))
    out = h.execute("p1", open_action())
    assert G.PORTFOLIO_UNBOUNDED in h.coord.intent(out.order_ref).reasons
    assert [o.order_ref for o in h.broker.all_orders()] == ["FOREIGN"]


def test_under_a_table_a_stock_entry_beyond_cash_is_borrowing(tmp_path):
    h = Harness(tmp_path, risk_families=((SYMBOL, "STK", "1"),))
    h.broker.account_cash = D(200)  # invented: 4 x 50.02 = 200.08 exceeds the cash
    out = h.execute("p1", open_action())
    assert G.BORROWING_NOT_PERMITTED not in (h.coord.intent(out.order_ref).reasons or [])
    # the invented table permits borrowing up to its limit; a table that forbids it refuses
    t = deepcopy(INVENTED_RISK_TABLE)
    t["borrowing"] = {"permitted": False, "short_sale_permitted": False}
    h.coord.policy = h.coord.policy.model_copy(update={"risk": RiskPolicy.from_table(t)})
    out = h.execute("p2", open_action(symbol=SYMBOL).model_copy(update={"action_id": "o2"}))
    assert G.BORROWING_NOT_PERMITTED in h.coord.intent(out.order_ref).reasons


# ----------------------------------------------------------------------------- the gateway

def test_the_unmapped_stock_path_is_gated_by_the_execution_core_not_the_gateway():
    """The unmapped stock path of the gateway serves the fictional Sprint 1 journal and paper
    replay (no broker): its short approves there as on main; a broker entry is refused by the
    execution core (test_baseline_refuses_a_short_stock_entry_at_the_last_check)."""
    from datetime import UTC, datetime

    from tradingbot.models import Quote
    from tradingbot.validator import gate_action
    now = datetime(2026, 6, 1, 14, tzinfo=UTC)
    a = open_action(side=PositionSide.SHORT, entry="49.98", stop="52", target="45")
    q = Quote(symbol=SYMBOL, bid=D("49.98"), ask=D("50.02"), observed_at=now)
    assert gate_action(a, now, q, MockPolicy(), None, now=now).status == "APPROVE"


def test_gateway_holds_a_mapped_short_etf_in_the_baseline_with_its_code():
    from test_multi_asset_gateway import NOW, act, gate, mq, terms

    from tradingbot.models import AssetClass
    t = terms(asset_class=AssetClass.ETF, multiplier=D(1), min_tick=D("0.01"))
    d = gate(act(entry="5025", stop="5055", target="4965", side=PositionSide.SHORT), mq(t),
             policy=MockPolicy(max_notional_usd=D(100000)), now=NOW)
    assert d.status == "HOLD" and d.reasons[:2] == [G.FAILED, G.SHORT_SALE_NOT_PERMITTED]


# ----------------------------------------------------------------------------- the simulated account

def _fill(sim: SimBroker, ref: str, contract: ContractRef, side: OrderSide, qty: int, price: str):
    sim.register_contract(contract)
    key = contract.contract_key if contract.sec_type != "STK" else contract.symbol
    sim.set_quote(key, price, price)
    sim.submit(OrderIntent(order_ref=ref, contract=contract, side=side, quantity=qty,
                           order_type=OrderType.LMT, limit_price=D(price)))
    if any(o.order_ref == ref for o in sim.open_orders()):
        sim.fill(ref)


def test_simulated_account_values_follow_fills_and_owner_margin(tmp_path):
    clock = SimClock(__import__("datetime").datetime(2026, 6, 1, 14, tzinfo=__import__(
        "datetime").UTC))
    sim = SimBroker(tmp_path / "b.sqlite3", clock=clock.now)
    assert sim.account_values().net_liquidation is None  # no account in the shipped simulation
    sim.account_cash = D(10000)
    stock = ContractRef(symbol="ZZQ", asset_class=AssetClass.EQUITY)
    _fill(sim, "s1", stock, OrderSide.BUY, 10, "50")
    a = sim.account_values()
    assert (a.net_liquidation, a.total_cash, a.init_margin) == (D(10000), D(9500), D(0))
    fut = ContractRef(symbol="QZF", asset_class=AssetClass.FUTURE, sec_type="FUT",
                      exchange="SIMX", con_id=77, expiry="20260918", multiplier=D(10))
    _fill(sim, "f1", fut, OrderSide.BUY, 2, "100")
    assert sim.account_values().init_margin is None  # no owner figure: unknown, never zero
    sim.margin_per_unit = {"QZF": (D(500), D(400))}
    a = sim.account_values()
    assert a.init_margin == D(1000) and a.maint_margin == D(800)
    assert a.net_liquidation == D(10000) and a.excess_liquidity == D(9200)
    assert a.total_cash == D(9500)  # performance margin is not a cash purchase


def test_shadow_account_values_are_the_simulations_and_read_nothing_real(tmp_path):
    from tradingbot.execution.shadow_broker import ShadowBroker

    calls: list[str] = []

    class Session:
        masked_account = "DU***1"

        def __getattr__(self, name):
            calls.append(name)
            raise AssertionError(f"read-only session used: {name}")

    sim = SimBroker(tmp_path / "b.sqlite3", strict=True)
    sim.account_cash = D(5)
    shadow = ShadowBroker(sim, Session(), connect_session=lambda: Session())
    assert shadow.account_values().net_liquidation == D(5) and calls == []


# ----------------------------------------------------------------------------- config

def _raw(tmp_path, extra: str = "") -> dict:
    from tradingbot.runtime.mock_multi_asset import EXECUTION, POLICY, SERVICE
    raw = {"mode": "mock",
           "paths": {"state_dir": str(tmp_path / "s"), "halt_file": str(tmp_path / "H"),
                     "mapping_file": str(tmp_path / "m.toml")},
           "source": {"kind": "replay", "file": str(tmp_path / "p.jsonl"), "compression": 1.0},
           "service": dict(SERVICE), "policy": dict(POLICY), "execution": dict(EXECUTION)}
    raw.update(tomllib.loads(extra))
    return raw


def test_config_risk_table_is_optional_complete_and_its_own_table(tmp_path):
    assert parse_config(_raw(tmp_path), base=tmp_path).policy.risk is None
    cfg = parse_config(_raw(tmp_path, TOML_TABLE), base=tmp_path)
    assert cfg.policy.risk == INVENTED_RISK
    partial = TOML_TABLE.replace('max_portfolio_risk = "100000000"\n', "")
    with pytest.raises(ConfigError, match="max_portfolio_risk"):
        parse_config(_raw(tmp_path, partial), base=tmp_path)
    funded = TOML_TABLE + 'funded_authorization = "x"\n'
    with pytest.raises(ConfigError):
        parse_config(_raw(tmp_path, funded), base=tmp_path)
    raw = _raw(tmp_path)
    raw["policy"]["risk"] = {}
    with pytest.raises(ConfigError, match="own"):
        parse_config(raw, base=tmp_path)


def test_policy_digest_is_unchanged_without_a_table():
    from tradingbot.shadow_runner import canonical_digest, policy_digest
    p = MockPolicy()
    assert policy_digest(p) == canonical_digest(p.model_dump(mode="json", exclude={"risk"}))
    assert policy_digest(p.model_copy(update={"risk": INVENTED_RISK})) != policy_digest(p)


def test_shipped_configs_carry_no_risk_values():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1] / "config"
    for f in root.glob("*.toml"):
        data = tomllib.loads(f.read_text())
        assert "risk" not in data, f.name
        for entry in data.get("mapping", []):
            assert not {"funding", "risk", "margin", "entry_collar"} & set(entry), f.name


# ----------------------------------------------------------------------------- the read-only adapter

def test_read_only_allow_lists_are_unchanged_and_only_tags_were_added():
    pytest.importorskip("ibapi")
    from ibapi.message import OUT

    from tradingbot.ibkr import session as S
    assert S.ALLOWED_OUT_MESSAGES == frozenset({
        OUT.START_API, OUT.REQ_CONTRACT_DATA, OUT.CANCEL_CONTRACT_DATA, OUT.REQ_ACCOUNT_SUMMARY,
        OUT.CANCEL_ACCOUNT_SUMMARY, OUT.REQ_POSITIONS, OUT.CANCEL_POSITIONS, OUT.REQ_MKT_DATA,
        OUT.CANCEL_MKT_DATA, OUT.REQ_MARKET_DATA_TYPE, OUT.REQ_CONFIG, OUT.REQ_SEC_DEF_OPT_PARAMS,
        OUT.REQ_HISTORICAL_DATA, OUT.CANCEL_HISTORICAL_DATA})
    assert OUT.PLACE_ORDER not in S.ALLOWED_OUT_MESSAGES
    assert S.ALLOWED_CLIENT_METHODS == frozenset({
        "connect", "disconnect", "isConnected", "run", "reset", "setConnState", "checkConnected",
        "validateInvalidSymbols", "useProtoBuf", "serverVersion", "twsConnectionTime",
        "logRequest", "msgLoopTmo", "msgLoopRec", "keyboardInterrupt", "keyboardInterruptHard",
        "sendMsg", "sendMsgProtoBuf", "startApi", "startApiProtoBuf", "reqContractDetails",
        "reqContractDataProtoBuf", "cancelContractData", "cancelContractDataProtoBuf",
        "reqAccountSummary", "reqAccountSummaryProtoBuf", "cancelAccountSummary",
        "cancelAccountSummaryProtoBuf", "reqPositions", "reqPositionsProtoBuf",
        "cancelPositions", "cancelPositionsProtoBuf", "reqMktData", "reqMarketDataProtoBuf",
        "cancelMktData", "cancelMarketDataProtoBuf", "reqMarketDataType",
        "reqMarketDataTypeProtoBuf", "reqConfigProtoBuf", "reqSecDefOptParams",
        "reqSecDefOptParamsProtoBuf", "reqHistoricalData", "reqHistoricalDataProtoBuf",
        "cancelHistoricalData", "cancelHistoricalDataProtoBuf"})
    assert S.ACCOUNT_SUMMARY_TAGS[-3:] == ("ExcessLiquidity", "InitMarginReq", "MaintMarginReq")


# ----------------------------------------------------------------------------- mock scenarios

def test_multi_asset_scenarios_without_a_table_are_refused_by_eligibility(tmp_path):
    from tradingbot.runtime.mock_multi_asset import run_scenario, scenarios
    for s in scenarios():
        r = run_scenario(tmp_path / s.name, s, risk=False)
        first = r.steps[0]
        assert first.status == "HOLD" and G.FAILED in first.reasons, s.name
        assert any(c.startswith(G.NOT_ELIGIBLE) for c in first.reasons), s.name
        assert r.broker_orders == 0, s.name


def test_a_stale_margin_figure_holds_a_future_at_the_gateway(tmp_path):
    from tradingbot.runtime.mock_multi_asset import MockMultiAssetRun, mapping_toml, scenarios
    s = next(x for x in scenarios() if x.name == "energy")
    text = mapping_toml(approved=True).replace("verified_at = 2026-05-29, max_age_days = 3650",
                                               "verified_at = 2026-01-01, max_age_days = 30")
    run = MockMultiAssetRun(tmp_path, s, mapping_text=text)
    try:
        run.run()
        first = next(a for a in run.store.signal_actions() if a["action"] == "OPEN")
        assert first["status"] == "HOLD" and G.MARGIN_FIGURE_STALE in first["reasons"]
        assert run.broker.submit_calls == 0
    finally:
        run.close()


def test_simulated_account_is_wired_only_under_a_table(tmp_path):
    from tradingbot.runtime.mock_multi_asset import MockMultiAssetRun, scenarios
    s = next(x for x in scenarios() if x.name == "energy")
    on = MockMultiAssetRun(tmp_path / "on", s)
    off = MockMultiAssetRun(tmp_path / "off", s, risk=False)
    try:
        assert on.broker.account_cash == D(100000000)  # the demo table's allocated equity
        assert on.broker.margin_per_unit == {"QIF": (D(1), D(1)), "QOF": (D(1), D(1)),
                                             "QMF": (D(1), D(1))}
        assert off.broker.account_cash is None
    finally:
        on.close()
        off.close()
