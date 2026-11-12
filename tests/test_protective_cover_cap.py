"""Confirmation review N1: protective cover never exceeds what the broker holds.

The total quantity of working protective exits for a contract is capped at the broker's position
in that contract, whatever the ledger believes. All data is invented; mock broker only.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from execution_harness import SYMBOL, Harness, open_action

from tradingbot.execution import coordinator as C
from tradingbot.execution import operator_catalog as cat
from tradingbot.execution import settlement as S
from tradingbot.execution.broker import BrokerError, Fill, OrderSide
from tradingbot.execution.reconcile import DiscrepancyKind
from tradingbot.execution.store import IntentState
from tradingbot.models import PositionSide

NOTE = "checked the invented account by hand"


@pytest.fixture
def h(tmp_path):
    return Harness(tmp_path)


def open_kinds(h) -> list[str]:
    return sorted(e.kind for e in h.coord.store.exceptions(open_only=True))


def protective(h, idea="p1:open") -> dict[str, int]:
    pos = h.coord.store.position(idea)
    working = h.working()
    return {
        ref.rsplit(".", 1)[1]: working[ref].remaining
        for ref in (pos.stop_ref, pos.target_ref)
        if ref and ref in working
    }


def broker_shows(h, quantity: int) -> None:
    """The broker's position view reports ``quantity`` (signed) for the contract (invented)."""
    real = h.broker.positions
    h.broker.positions = lambda: [p.model_copy(update={"quantity": quantity}) for p in real()]


def declared_four_broker_two(h):
    """The reviewer's probe: the operator declares FILLED 4, the broker evidences only 2."""
    h.broker.history_seconds = 3600
    h.broker.script_next_submit("LOST_ACK_PLACED")
    h.broker.lookup_failures_after_submit = 1
    out = h.execute("p1", open_action())
    h.clock.advance(h.config.lookup_horizon_seconds + 60)
    h.broker.set_quote(SYMBOL, "49.98", "50.02")
    h.coord.recover()
    ref = out.order_ref
    result = S.settle_intent(
        h.coord, request_id="n1-probe", order_ref=ref, outcome="FILLED", quantity=4,
        avg_price="50.02", confirmation=cat.settle_phrase(ref, "FILLED", 4, "50.02"),
        note=NOTE, operator="tester",
    )
    assert result.status == "APPLIED"
    # The broker's own executions arrive: only 2 filled; it holds 2 and sized the children to 2.
    real_exec = h.broker.executions
    contract = h.broker.positions()[0].contract
    late = [Fill(exec_id="INV-N1-1", broker_order_id="1", order_ref=ref, contract=contract,
                 side=OrderSide.BUY, quantity=2, price=Decimal("50.02"),
                 executed_at=h.clock.now())]
    h.broker.executions = lambda since=None: [*real_exec(since), *late]
    pos = h.coord.store.position("p1:open")
    for child in (pos.stop_ref, pos.target_ref):
        h.broker.modify(child, quantity=2)
    broker_shows(h, 2)
    return ref, late


def test_n1_probe_declared_four_broker_two_never_resizes_cover_up(h):
    declared_four_broker_two(h)
    h.coord.sync()
    assert h.coord.store.position("p1:open").quantity == 4  # the ledger still counts 4
    assert protective(h) == {"STP": 2, "TGT": 2}  # never 4 against the 2 held
    kinds = open_kinds(h)
    assert C.UNPROTECTED_POSITION in kinds and C.OPERATOR_SETTLEMENT_UNVERIFIED in kinds
    [e] = [x for x in h.coord.store.exceptions(open_only=True) if x.kind == C.UNPROTECTED_POSITION]
    assert "BROKER_POSITION_DIFFERS" in e.detail
    h.broker.set_quote(SYMBOL, "49.98", "50.02")
    assert h.execute("p2", open_action()).state == IntentState.REFUSED
    h.coord.sync()
    assert protective(h) == {"STP": 2, "TGT": 2}


def test_n1_a_protective_order_above_the_broker_position_is_resized_down_at_once(h):
    h.execute("p1", open_action())
    assert protective(h) == {"STP": 4, "TGT": 4}
    broker_shows(h, 3)
    h.coord.sync()
    assert protective(h) == {"STP": 3, "TGT": 3}
    assert C.UNPROTECTED_POSITION in open_kinds(h)


def test_n1_partial_evidence_that_later_completes(h):
    ref, late = declared_four_broker_two(h)
    h.coord.sync()
    assert protective(h) == {"STP": 2, "TGT": 2}
    # The rest of the broker's executions arrive and its position view shows all 4.
    contract = late[0].contract
    late.append(Fill(exec_id="INV-N1-2", broker_order_id="1", order_ref=ref, contract=contract,
                     side=OrderSide.BUY, quantity=2, price=Decimal("50.02"),
                     executed_at=h.clock.now()))
    broker_shows(h, 4)
    h.coord.sync()
    assert h.coord.store.position("p1:open").quantity == 4
    assert protective(h) == {"STP": 4, "TGT": 4}
    assert h.coord.store.position("p1:open").protection == "CONFIRMED"
    kinds = open_kinds(h)
    assert C.UNPROTECTED_POSITION not in kinds and C.OPERATOR_SETTLEMENT_UNVERIFIED not in kinds


def test_n1_broker_holding_more_covers_only_the_ledger_and_flags_the_excess(h):
    h.execute("p1", open_action())
    broker_shows(h, 6)  # 2 the ledger cannot explain (invented)
    h.coord.sync()
    assert protective(h) == {"STP": 4, "TGT": 4}  # no stop the system invented for the excess
    assert C.UNPROTECTED_POSITION in open_kinds(h)  # the contract stays blocked
    rec = h.coord.reconcile(h.clock.now())
    [d] = [x for x in rec.discrepancies if x.kind == DiscrepancyKind.QUANTITY_MISMATCH]
    assert d.explained_by is None and not rec.clean
    h.broker.set_quote(SYMBOL, "49.98", "50.02")
    assert h.execute("p2", open_action()).state == IntentState.REFUSED


def test_n1_short_position_is_capped_at_the_broker_position(h):
    h.execute("p1", open_action(side=PositionSide.SHORT, stop="52", target="45"))
    assert h.broker.positions()[0].quantity == -4
    assert protective(h) == {"STP": 4, "TGT": 4}
    broker_shows(h, -2)
    h.coord.sync()
    assert protective(h) == {"STP": 2, "TGT": 2}
    assert C.UNPROTECTED_POSITION in open_kinds(h)


def test_n1_an_unreadable_broker_position_never_resizes_cover_up(h):
    declared_four_broker_two(h)
    real = h.broker.positions
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] > 1:  # the sync's own read succeeds, the protection check's fails
            raise BrokerError("invented: positions unreadable")
        return real()

    h.broker.positions = flaky
    h.coord.sync()
    assert protective(h) == {"STP": 2, "TGT": 2}
    assert h.coord.store.position("p1:open").protection != "CONFIRMED"
