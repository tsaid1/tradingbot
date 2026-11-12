"""Fixes after the independent review of the multi-asset mapping (PR #61).

Each test reproduces one reviewer probe or one rule the owner stated, with invented data only:
protective levels are never loosened by an amendment (F1), an unrecognised contract month is never
replaced by the nearest one (F2), a multi-leg option instruction never executes as one leg (F3),
and the SHOULD FIX rows of the review table.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from test_multi_asset_gateway import act, gate, leg, mq, position, terms

from tradingbot.event_models import PriceBasis, Timing, TradeAction
from tradingbot.models import Action, AssetClass, Instrument, PositionSide
from tradingbot.runtime.mapping import MappingPolicy
from tradingbot.runtime.mock_multi_asset import (
    QIF_JUN,
    STATIC,
    MockMultiAssetRun,
    Scenario,
    Step,
    mapping_toml,
)

ENTRY = "We are now long $QIDX at 5,010. Stop 4,980. Target 5,070."


def run_steps(tmp_path, steps, *, name="t", quotes=None, family="QIDX", treatment="X",
              mapping_text=None, hook=None, extra=30):
    s = Scenario(name, family, treatment, steps)
    if quotes is not None:
        s.quotes = quotes
    run = MockMultiAssetRun(tmp_path / name, s, mapping_text=mapping_text)
    last = max(x.offset for x in steps)
    start = run.clock.now()
    while (run.clock.now() - start).total_seconds() <= last + extra:
        if hook:
            hook(run)
        run.step()
    return run


def traded(run):
    return [a for a in run.store.signal_actions() if a["action"] not in (None, "NO_TRADE")]


def exception_kinds(run):
    return [e.kind for e in run.service.coordinator.store.exceptions(open_only=True)]


# ================================================================== F1: never loosen protection


def test_f1_reference_trigger_tightening_never_lowers_the_backstop():
    """Reviewer F1: stop 4980 and backstop 4995 (basis +15). The basis moves to -5 and the
    publisher raises the stop to 4985: the backstop would come out at 4980. It stays at 4995."""
    t = terms("REFERENCE_TRIGGER", require_backstop=True)
    pos = position(stop="4980").model_copy(update={"backstop_price": "4995.00"})
    q = mq(t, bid="5004.75", ask="5005.25", reference=leg())  # basis -5
    g = gate(act(Action.MODIFY, idea="9:a1", stop="4985", target=None), q, pos)
    assert g.status == "APPROVE"
    assert "PROTECTIVE_LEVEL_NOT_LOOSENED" in g.reasons
    assert g.plan.stop == Decimal(4985)  # the trigger tightens as the publisher said
    assert g.plan.backstop_stop == Decimal("4995.00")  # the working backstop is kept
    assert g.plan.protective_levels_kept and "BACKSTOP" in g.plan.protective_levels_kept[0]


def test_f1_reference_trigger_tightening_that_also_tightens_the_backstop_moves_it():
    t = terms("REFERENCE_TRIGGER", require_backstop=True)
    pos = position(stop="4980").model_copy(update={"backstop_price": "4995.00"})
    q = mq(t, reference=leg())  # basis +15
    g = gate(act(Action.MODIFY, idea="9:a1", stop="4985", target=None), q, pos)
    assert g.status == "APPROVE" and "PROTECTIVE_LEVEL_NOT_LOOSENED" not in g.reasons
    assert g.plan.backstop_stop == Decimal("5000.00") and g.plan.protective_levels_kept == []


def test_f1_basis_shift_tightening_keeps_the_tighter_execution_stop():
    """The publisher tightens 1842 -> 1845 but the basis fell from 14 to 5: the converted stop
    (1850) would be looser than the working 1856. The working stop is kept and flagged."""
    t = terms("BASIS_SHIFT", min_tick=Decimal("0.10"), max_abs_basis=Decimal(20))
    pos = position(stop="1856.0").model_copy(update={"publisher_stop": "1842"})
    q = mq(t, bid="1859.9", ask="1860.1", reference=leg("1854.9", "1855.1", None))  # basis 5
    g = gate(act(Action.MODIFY, idea="9:a1", stop="1845", target=None), q, pos)
    assert g.status == "APPROVE" and "PROTECTIVE_LEVEL_NOT_LOOSENED" in g.reasons
    assert g.plan.stop == Decimal("1856.0") and g.plan.publisher_stop == Decimal(1845)
    assert "STOP" in g.plan.protective_levels_kept[0]


def test_f1_a_publisher_loosening_still_holds_in_every_treatment():
    widen = act(Action.MODIFY, idea="9:a1", stop="4970", target=None)
    rt = terms("REFERENCE_TRIGGER", require_backstop=True)
    pos = position(stop="4980").model_copy(update={"backstop_price": "4995.00"})
    assert gate(widen, mq(rt, reference=leg()), pos).reasons == [
        "STOP_WIDENING_REQUIRES_POLICY_REVIEW"]
    bs = terms("BASIS_SHIFT", min_tick=Decimal("0.10"), max_abs_basis=Decimal(20))
    pos = position(stop="1856.0").model_copy(update={"publisher_stop": "1842"})
    # Basis up to 19: the converted 1840 + 19 = 1859 is TIGHTER in execution terms, but the
    # publisher's own level loosened: the existing widening rule applies, unchanged.
    q = mq(bs, bid="1868.9", ask="1869.1", reference=leg("1849.9", "1850.1", None))
    g = gate(act(Action.MODIFY, idea="9:a1", stop="1840", target=None), q, pos)
    assert g.reasons == ["STOP_WIDENING_REQUIRES_POLICY_REVIEW"]


def test_f1_end_to_end_backstop_kept_and_exception_raised(tmp_path):
    """Reviewer probe E1 through the service: the broker backstop stays at 4995, the ledger and
    protection agree with what is actually working, and the divergence is a visible exception."""

    def quotes(elapsed):
        return {**STATIC, QIF_JUN: ("5004.75", "5005.25", None)} if elapsed >= 40 else STATIC

    run = run_steps(tmp_path, [Step(0, ENTRY, "EXECUTED"),
                               Step(60, "Raising our $QIDX stop to 4,985.", "EXECUTED")],
                    quotes=quotes)
    [p] = [p for p in run.service.coordinator.ledger() if p.quantity > 0]
    backstop = next(o for o in run.broker.open_orders() if o.order_ref == p.backstop_ref)
    assert backstop.stop_price == Decimal("4995.00")
    assert (p.stop_price, p.backstop_price, p.protection) == ("4985", "4995.00", "CONFIRMED")
    assert "PROTECTIVE_LEVEL_NOT_LOOSENED" in exception_kinds(run)
    run.close()


# ================================================================== F2: contract-month recognition

MAP_NOW = datetime(2026, 6, 1, 14, 0, tzinfo=UTC)


def demo_policy(tmp_path, text=None) -> MappingPolicy:
    p = tmp_path / "m.toml"
    p.write_text(text or mapping_toml(approved=True))
    return MappingPolicy.from_toml(p)


def opening(asset, action=Action.OPEN, *, idea=None) -> TradeAction:
    return TradeAction(
        action_id="a1", action=action, timing=Timing.CURRENT,
        instrument=Instrument(asset_class=AssetClass.EQUITY, symbol=asset),
        position_side=PositionSide.LONG, idea_reference=idea,
        price_basis=PriceBasis.TRADED_CONTRACT, source_market_symbol=asset,
        entry_reference="10" if action in (Action.OPEN, Action.ADD) else None,
        explicit_instruction=True, evidence=["invented"],
    )


@pytest.mark.parametrize(
    "form",
    ["Sep 2026", "September 2026", "Sept 2026", "Sep. 2026", "Sep '26", "Sep'26", "Sep 26",
     "Sep26", "SEPTEMBER '26", "Sep ’26", "2026-09", "2026/09", "09/2026", "9/2026", "202609",
     "QIFU6", "QIFU26", "U6", "U26", "the Sep contract"],
)
def test_f2_each_common_month_form_selects_that_month(tmp_path, form):
    d, inst = demo_policy(tmp_path).resolve(
        opening("QIDX"), clause=f"We are now long $QIDX {form} at 5,010. Stop 4,980.", now=MAP_NOW)
    assert d.status == "SUPPORTED", d.reason
    assert inst.futures_contract == "202609"  # never the nearest (June)


@pytest.mark.parametrize(
    "form",
    ["Sep", "Sept.", "September", "Sep '27", "Sep 2027", "Sep 18", "QIFX6", "QIFU7", "QABU6",
     "QIFU", "2026-13", "13/2026", "2026-00", "Sep 202", "Nov 2026", "(QIFZ26)", "Dec contract"],
)
def test_f2_a_month_like_token_that_does_not_resolve_holds(tmp_path, form):
    """Reviewer probe: "Sep '26" and "(QIFU6)" executed in June. Every near miss now HOLDs."""
    d, inst = demo_policy(tmp_path).resolve(
        opening("QIDX"), clause=f"We are now long $QIDX {form} at 5,010. Stop 4,980.", now=MAP_NOW)
    assert d.status == "UNRESOLVED" and inst is None
    assert d.reason.split(",")[0] == "CONTRACT_MONTH_UNRECOGNISED"


def test_f2_the_nearest_rule_applies_only_without_any_month_like_token(tmp_path):
    p = demo_policy(tmp_path)
    for clause in ["We are now long $QIDX at 5,010. Stop 4,980.",
                   "We may add to $QIDX at 5,010 and will march on. Stop 4,980."]:
        d, inst = p.resolve(opening("QIDX"), clause=clause, now=MAP_NOW)
        assert d.status == "SUPPORTED" and inst.futures_contract == "202606", clause


def test_f2_reviewer_probes_through_the_service(tmp_path):
    for i, text in enumerate([
        "We are now long $QIDX Sep '26 at 5,010. Stop 4,980. Target 5,070.",
        "We are now long $QIDX (QIFU6) at 5,010. Stop 4,980. Target 5,070.",
    ]):
        run = run_steps(tmp_path, [Step(0, text, "EXECUTED")], name=f"p{i}")
        refs = [o.contract.expiry for o in run.broker.all_orders()]
        assert refs and all(e == "20260918" for e in refs), refs  # September, never June
        run.close()
    run = run_steps(tmp_path, [Step(0, "We are now long $QIDX Sep '27 at 5,010. Stop 4,980.",
                                    "HOLD")], name="miss")
    [a] = traded(run)
    assert a["status"] == "HOLD" and "CONTRACT_MONTH_UNRECOGNISED" in a["reasons"]
    assert run.broker.submit_calls == 0
    run.close()
