"""Multi-asset execution end to end through the runtime service (invented posts and contracts).

Reference-triggered exits at the strict SimBroker, protection confirmation, refusals that must
never reach the broker, contract identity across months, the roll-window flag, coverage, the
execution page section and the demo script.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tradingbot.execution.broker import ConditionDirection, OrderType
from tradingbot.models import AssetClass, Instrument, MappedQuote
from tradingbot.runtime.coverage import coverage_report
from tradingbot.runtime.mapping import MappingPolicy
from tradingbot.runtime.mock_multi_asset import (
    QIDX,
    QIF_JUN,
    QOF_JUL,
    STATIC,
    MockMultiAssetRun,
    Scenario,
    Step,
    run_all,
    scenarios,
)

REPO = Path(__file__).resolve().parents[1]
ENTRY = "We are now long $QIDX at 5,010. Stop 4,980. Target 5,070."


def started(tmp_path, steps, name="t", quotes=None) -> MockMultiAssetRun:
    s = Scenario(name, "QIDX", "REFERENCE_TRIGGER", steps)
    if quotes is not None:
        s.quotes = quotes
    run = MockMultiAssetRun(tmp_path / name, s)
    run.run()
    return run


def actions(run):
    return [a for a in run.store.signal_actions() if a["action"] not in (None, "NO_TRADE")]


def idea(run):
    [p] = [p for p in run.service.coordinator.ledger() if p.quantity > 0]
    return p


# ------------------------------------------------------------------ the demonstrations


@pytest.fixture(scope="module")
def demo_results(tmp_path_factory):
    return run_all(tmp_path_factory.mktemp("multi_asset_demo"))


def test_every_demonstration_passes(demo_results):
    assert [r.scenario.name for r in demo_results if not r.ok] == []
    by = {r.scenario.name: r for r in demo_results}
    for name in ("index", "energy", "metal", "option"):
        r = by[name]
        assert r.reconciliation_clean and r.flat and r.duplicate_refs == 0
        assert [s.action for s in r.steps[:4]] == ["OPEN", "MODIFY", "REDUCE", "CLOSE"]
        assert all(s.status == "EXECUTED" for s in r.steps[:4])
    assert any("basis 12.0" in b for b in by["metal"].basis_records)
    assert any("backstop 4995.00" in b for b in by["index"].basis_records)


def test_unapproved_families_never_reach_the_broker(demo_results):
    for r in demo_results:
        if "approved = false" in r.scenario.name:
            assert r.broker_orders == 0
            assert all(s.status == "HOLD" and "MAPPING_NOT_APPROVED" in s.reasons
                       for s in r.steps)
            assert all(c["mapping_status"] == "NOT APPROVED" and c["executed"] == 0
                       for c in r.coverage)


def test_the_demo_script_exits_zero(tmp_path):
    out = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "run_mock_multi_asset_demo.py"),
         "--state-dir", str(tmp_path / "demo")],
        capture_output=True, text=True, env={"PYTHONPATH": str(REPO / "src")}, timeout=300,
        check=False,
    )
    assert out.returncode == 0, out.stdout + out.stderr
    assert "8/8 scenarios passed" in out.stdout


# ------------------------------------------------------------------ reference-triggered exits


def test_a_reference_level_is_never_sent_as_an_execution_price(tmp_path):
    run = started(tmp_path, [Step(0, ENTRY, "EXECUTED")])
    orders = {o.order_ref.rsplit(".", 1)[-1] if "." in o.order_ref else "ENTRY": o
              for o in run.broker.all_orders()}
    entry, stop, target, backstop = (orders[k] for k in ("ENTRY", "STP", "TGT", "BKS"))
    assert entry.contract.contract_key == QIF_JUN.contract_key
    assert entry.avg_fill_price == Decimal("5025.25")  # the future's own ask, not 5,010
    assert entry.limit_price > Decimal(5025) and entry.limit_price % Decimal("0.25") == 0
    for child, direction, level in ((stop, ConditionDirection.BELOW, 4980),
                                    (target, ConditionDirection.ABOVE, 5070)):
        assert child.order_type == OrderType.MKT and child.stop_price is None
        assert child.limit_price is None and child.contract.contract_key == QIF_JUN.contract_key
        assert child.condition.contract.contract_key == QIDX.contract_key
        assert child.condition.direction == direction
        assert child.condition.trigger_price == Decimal(level)
    # The backstop: 4,980 + the observed basis of 15, in the future's own prices.
    assert backstop.order_type == OrderType.STP and backstop.stop_price == Decimal("4995.00")
    p = idea(run)
    assert p.protection == "CONFIRMED" and p.protection_series == "REFERENCE"
    assert p.reference_key == QIDX.contract_key and p.stop_price == "4980"


def test_the_condition_fires_on_the_reference_and_exits_in_the_execution_contract(tmp_path):
    run = started(tmp_path, [Step(0, ENTRY, "EXECUTED")])
    # The index falls through 4,980 while the future is still well above its backstop.
    run.scenario.quotes = lambda _t: {**STATIC, QIDX: ("4978.5", "4979.5", "4979")}
    for _ in range(3):
        run.step()
    by_ref = {o.order_ref: o for o in run.broker.all_orders()}
    stop = next(o for r, o in by_ref.items() if r.endswith(".STP"))
    assert stop.status.value == "Filled" and stop.avg_fill_price == Decimal("5024.75")
    assert all(o.status.value == "Cancelled" for r, o in by_ref.items()
               if r.endswith((".TGT", ".BKS")))  # one-cancels-other
    assert not run.broker.positions() and not run.broker.open_orders()
    [p] = run.service.coordinator.ledger()
    assert p.quantity == 0 and p.status == "CLOSED"
    assert run.service.last_reconciliation.clean


def test_the_backstop_protects_when_the_reference_does_not_move(tmp_path):
    run = started(tmp_path, [Step(0, ENTRY, "EXECUTED")])
    run.scenario.quotes = lambda _t: {**STATIC, QIF_JUN: ("4994.75", "4995.25", None)}
    for _ in range(3):
        run.step()
    bks = next(o for o in run.broker.all_orders() if o.order_ref.endswith(".BKS"))
    assert bks.status.value == "Filled" and not run.broker.positions()
    assert run.service.coordinator.ledger()[0].status == "CLOSED"
    assert run.service.last_reconciliation.clean


def test_protection_is_confirmed_only_for_the_right_trigger_level(tmp_path):
    run = started(tmp_path, [Step(0, ENTRY, "EXECUTED")])
    stop_ref = idea(run).stop_ref
    run.broker.modify_condition(stop_ref, trigger_price=Decimal(4900))  # changed behind our back
    run.step()
    assert idea(run).protection == "UNPROTECTED"
    kinds = {d.kind.value for d in run.service.last_reconciliation.discrepancies}
    assert "STOP_PRICE_MISMATCH" in kinds and not run.service.last_reconciliation.clean
    assert any(e.kind == "UNPROTECTED_POSITION"
               for e in run.service.coordinator.store.exceptions(open_only=True))


def test_protection_needs_the_required_backstop(tmp_path):
    run = started(tmp_path, [Step(0, ENTRY, "EXECUTED")])
    run.broker.cancel(idea(run).backstop_ref)
    run.step()
    assert idea(run).protection == "UNPROTECTED"
    assert any(d.order_ref == idea(run).backstop_ref
               for d in run.service.last_reconciliation.discrepancies)


def test_a_broker_without_price_conditions_is_never_sent_a_conditional_bracket(tmp_path):
    s = Scenario("noc", "QIDX", "REFERENCE_TRIGGER", [Step(0, ENTRY, "REFUSED")])
    run = MockMultiAssetRun(tmp_path / "noc", s)
    run.broker.conditions_supported = False
    run.run()
    [a] = actions(run)
    assert a["status"] == "REFUSED"
    assert "CONDITIONAL_ORDERS_UNSUPPORTED_BY_BROKER" in a["reasons"]
    assert run.broker.submit_calls == 0


def test_a_reference_trigger_amendment_moves_the_condition_and_the_backstop(tmp_path):
    run = started(tmp_path, [Step(0, ENTRY, "EXECUTED"),
                             Step(60, "Raising our $QIDX stop to 4,995.", "EXECUTED")])
    p = idea(run)
    assert (p.stop_price, p.backstop_price, p.protection) == ("4995", "5010.00", "CONFIRMED")
    stop = next(o for o in run.broker.open_orders() if o.order_ref == p.stop_ref)
    assert stop.condition.trigger_price == Decimal(4995)


# ------------------------------------------------------------------ refusals at the service


def test_a_stale_reference_quote_holds_a_basis_shift_entry(tmp_path):
    metal = next(s for s in scenarios() if s.name == "metal")
    from tradingbot.runtime.mock_multi_asset import QMS

    s = Scenario("stale", "QMTL", "BASIS_SHIFT", [metal.steps[0]])
    run = MockMultiAssetRun(tmp_path / "stale", s)
    stale_at = run.clock.now() - timedelta(seconds=20)
    for c, (bid, ask, last) in STATIC.items():
        run.broker.set_quote(c.symbol, bid, ask, last=last, contract=c,
                             received_at=stale_at if c == QMS else run.clock.now())
    s.quotes = lambda _t: {c: v for c, v in STATIC.items() if c != QMS}
    run.run()
    [a] = actions(run)
    assert a["status"] == "HOLD"
    assert a["reasons"] == ["BASIS_QUOTE_UNUSABLE", "REFERENCE_QUOTE_STALE"]
    assert run.broker.submit_calls == 0


def test_a_follow_up_naming_another_month_never_touches_the_held_contract(tmp_path):
    steps = [
        Step(0, "We are now long $QOIL Jul 2026 at 71.50. Stop 69.80.", "EXECUTED"),
        Step(60, "Closing our $QOIL Jun 2026 long.", "HOLD"),
    ]
    s = Scenario("months", "QOIL", "DIRECT", steps)
    run = MockMultiAssetRun(tmp_path / "months", s)
    run.run()
    opened, close = actions(run)
    assert opened["status"] == "EXECUTED"
    assert close["status"] == "HOLD" and "IDEA_CONTRACT_MISMATCH" in close["reasons"]
    [pos] = run.broker.positions()
    assert pos.contract.contract_key == QOF_JUL.contract_key and pos.quantity == 2
    assert idea(run).contract_key == QOF_JUL.contract_key


def test_a_stock_keeps_a_plain_quote_and_its_unchanged_path(tmp_path):
    from runtime_harness import ENTRY as TLT_ENTRY
    from runtime_harness import Runtime, post

    rt = Runtime(tmp_path, [post("1", 0, TLT_ENTRY)])
    rt.service._publish_mock_quotes(rt.clock.now())
    parsed, decisions = rt.service.mapping.apply(
        rt.service.interpreter.interpret(post("1", 0, TLT_ENTRY)).parsed
    )
    quotes = rt.service._gate_quotes(parsed, decisions)
    assert list(quotes) == ["TLT"] and not isinstance(quotes["TLT"], MappedQuote)
    assert decisions[0].status == "SUPPORTED" and not decisions[0].mapped
    rt.run(3)
    [a] = [a for a in rt.actions() if a["action"] == "OPEN"]
    assert a["status"] == "EXECUTED" and a["mapping_family"] == "TLT"
    rt.service.close()


# ------------------------------------------------------------------ roll window and coverage


def test_a_position_inside_its_roll_window_is_flagged_for_a_human(tmp_path):
    run = started(tmp_path, [Step(0, ENTRY, "EXECUTED")])
    assert not [a for a in run.store.alerts(open_only=True) if a["key"].startswith("ROLL:")]
    run.clock.advance(7 * 24 * 3600)  # Monday 8 June: 4 business days before 12 June
    run.step()
    assert not [a for a in run.store.alerts(open_only=True) if a["key"].startswith("ROLL:")]
    run.clock.advance(24 * 3600)  # Tuesday 9 June: 3 business days before, inside the window
    run.step()
    [alert] = [a for a in run.store.alerts(open_only=True) if a["key"].startswith("ROLL:")]
    assert alert["kind"] == "ROLL_DUE_FLATTEN_REQUIRED" and "2026-06-12" in alert["detail"]
    assert any(k == "ROLL_DUE_FLATTEN_REQUIRED" for k, _, _ in run.service.notifier.sent)
    assert run.broker.submit_calls == 1  # flagged, never flattened by the service itself


def test_coverage_shows_an_unmapped_family_as_unsupported(tmp_path):
    steps = [Step(0, ENTRY, "EXECUTED"),
             Step(60, "We are now long $QZZZ at 12.50. Stop 12.00.", "HOLD")]
    run = started(tmp_path, steps)
    cov = {c["family"]: c for c in coverage_report(MappingPolicy.from_toml(run.mapping_file),
                                                   run.store.signal_actions())}
    assert cov["QIDX"]["mapping_status"] == "APPROVED" and cov["QIDX"]["executed"] == 1
    assert cov["QIDX"]["basis"] == "REFERENCE_TRIGGER" and "QIF FUT" in cov["QIDX"]["execution"]
    z = cov["QZZZ"]
    assert z["mapping_status"] == "UNSUPPORTED" and not z["mapped"] and z["held"] == 1
    assert z["held_by_reason"] == {"UNSUPPORTED_INSTRUMENT_MAPPING": 1}
    assert cov["QOIL"]["signals"] == 0  # configured, not yet seen: still listed


def test_the_execution_page_shows_coverage(tmp_path, monkeypatch):
    from streamlit.testing.v1 import AppTest

    run = started(tmp_path, [Step(0, ENTRY, "EXECUTED"),
                             Step(60, "We are now long $QZZZ at 12.50. Stop 12.00.", "HOLD")])
    cfg = tmp_path / "runtime.toml"
    c = run.config
    cfg.write_text(
        f'mode = "mock"\n[paths]\nstate_dir = "{c.paths.state_dir}"\n'
        f'halt_file = "{c.paths.halt_file}"\nmapping_file = "{run.mapping_file}"\n'
        f'[source]\nkind = "replay"\nfile = "{c.source.file}"\n'
        + "".join(
            f"[{t}]\n" + "".join(f"{k} = {v!r}\n".replace("'", '"') for k, v in vals.items())
            for t, vals in (("service", _svc()), ("policy", _pol()), ("execution", _exe()))
        )
    )
    monkeypatch.setenv("TRADINGBOT_RUNTIME_CONFIG", str(cfg))
    at = AppTest.from_file(str(REPO / "dashboard" / "pages" / "8_Execution.py"),
                           default_timeout=60)
    at.run()
    assert not at.exception
    text = " ".join(str(e.value) for kind in ("subheader", "markdown", "warning")
                    for e in at.get(kind))
    assert "Instrument coverage by source family" in text
    assert "| QIDX | APPROVED |" in text and "| QZZZ | UNSUPPORTED |" in text
    assert "Signals held for families that cannot trade: QZZZ" in text


def _svc():
    from tradingbot.runtime.mock_multi_asset import SERVICE

    return SERVICE


def _pol():
    from tradingbot.runtime.mock_multi_asset import POLICY

    return POLICY


def _exe():
    from tradingbot.runtime.mock_multi_asset import EXECUTION

    return EXECUTION


def test_quote_keys_keep_contract_months_and_strikes_apart():
    a = Instrument(asset_class=AssetClass.FUTURE, symbol="QOF", futures_contract="202607")
    b = a.model_copy(update={"futures_contract": "202608"})
    assert a.quote_key != b.quote_key
    assert Instrument(asset_class=AssetClass.ETF, symbol="TLT").quote_key == "TLT"
