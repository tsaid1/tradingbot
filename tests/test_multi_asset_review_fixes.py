"""Fixes after the independent review of the multi-asset mapping (PR #61).

Each test reproduces one reviewer probe or one rule the owner stated, with invented data only:
protective levels are never loosened by an amendment (F1), an unrecognised contract month is never
replaced by the nearest one (F2), a multi-leg option instruction never executes as one leg (F3),
and the SHOULD FIX rows of the review table.
"""

from __future__ import annotations

from decimal import Decimal

from test_multi_asset_gateway import act, gate, leg, mq, position, terms

from tradingbot.models import Action
from tradingbot.runtime.mock_multi_asset import (
    QIF_JUN,
    STATIC,
    MockMultiAssetRun,
    Scenario,
    Step,
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
