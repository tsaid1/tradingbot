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
    """The reviewer's probe: the operator declares FILLED 4, the broker evidences only 2. The
    broker sized the children to the 2 it holds before the declaration. Returns what was working
    then."""
    h.broker.history_seconds = 3600
    h.broker.script_next_submit("LOST_ACK_PLACED")
    h.broker.lookup_failures_after_submit = 1
    out = h.execute("p1", open_action())
    h.clock.advance(h.config.lookup_horizon_seconds + 60)
    h.broker.set_quote(SYMBOL, "49.98", "50.02")
    h.coord.recover()
    ref = out.order_ref
    pos = h.coord.store.position("p1:open")
    for child in (pos.stop_ref, pos.target_ref):
        h.broker.modify(child, quantity=2)
    before = protective(h)
    result = S.settle_intent(
        h.coord, request_id="n1-probe", order_ref=ref, outcome="FILLED", quantity=4,
        avg_price="50.02", confirmation=cat.settle_phrase(ref, "FILLED", 4, "50.02"),
        note=NOTE, operator="tester",
    )
    assert result.status == "APPLIED"
    # The broker's own executions arrive: only 2 filled, and it holds 2.
    real_exec = h.broker.executions
    contract = h.broker.positions()[0].contract
    late = [Fill(exec_id="INV-N1-1", broker_order_id="1", order_ref=ref, contract=contract,
                 side=OrderSide.BUY, quantity=2, price=Decimal("50.02"),
                 executed_at=h.clock.now())]
    h.broker.executions = lambda since=None: [*real_exec(since), *late]
    broker_shows(h, 2)
    return ref, late, before


def differs_open(h) -> bool:
    return any(x.kind == C.UNPROTECTED_POSITION and "BROKER_POSITION_DIFFERS" in x.detail
               for x in h.coord.store.exceptions(open_only=True))


def cycles(h, seconds: float, step: float = 2.0) -> None:
    t = 0.0
    while t < seconds:
        h.clock.advance(step)
        t += step
        h.broker.set_quote(SYMBOL, "49.98", "50.02")
        h.coord.sync()


def test_n1_probe_declared_four_broker_two_never_resizes_cover_up(h):
    _, _, before = declared_four_broker_two(h)
    assert before == {"STP": 2, "TGT": 2}
    for _ in range(3):
        h.coord.sync()
        assert h.coord.store.position("p1:open").quantity == 4  # the ledger still counts 4
        now = protective(h)
        assert now == {"STP": 2, "TGT": 2}  # never 4 against the 2 held
        assert all(now[k] <= before[k] for k in before)  # never above what worked before
    kinds = open_kinds(h)
    assert C.UNPROTECTED_POSITION in kinds and C.OPERATOR_SETTLEMENT_UNVERIFIED in kinds
    assert differs_open(h)
    h.broker.set_quote(SYMBOL, "49.98", "50.02")
    assert h.execute("p2", open_action()).state == IntentState.REFUSED


def test_n1_partial_evidence_that_later_completes(h):
    ref, late, _ = declared_four_broker_two(h)
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
    assert differs_open(h)  # the contract stays blocked
    rec = h.coord.reconcile(h.clock.now())
    [d] = [x for x in rec.discrepancies if x.kind == DiscrepancyKind.QUANTITY_MISMATCH]
    assert d.explained_by is None and not rec.clean
    h.broker.set_quote(SYMBOL, "49.98", "50.02")
    assert h.execute("p2", open_action()).state == IntentState.REFUSED


# ------------------------------------------------- downward: only on corroborated evidence


def test_n1_a_transient_flat_read_never_cancels_the_stop(h):
    h.execute("p1", open_action())
    real = h.broker.positions
    h.broker.positions = list  # a still-loading view, returned as if complete
    h.coord.sync()
    assert protective(h) == {"STP": 4, "TGT": 4}  # the stop stays
    assert differs_open(h)
    h.broker.set_quote(SYMBOL, "49.98", "50.02")
    assert h.execute("p2", open_action()).state == IntentState.REFUSED
    h.broker.positions = real  # the view catches up
    h.coord.sync()
    assert protective(h) == {"STP": 4, "TGT": 4}
    assert not differs_open(h)
    assert h.coord.store.position("p1:open").protection == "CONFIRMED"


def test_n1_a_transient_flat_read_alerts_through_the_service(tmp_path):
    from runtime_harness import ENTRY, Runtime, post

    from tradingbot.runtime import notify as N

    rt = Runtime(tmp_path, [post("1", 0, ENTRY)])
    rt.step()
    pos = rt.service.coordinator.store.position("1:a1")
    assert pos.quantity == 2
    real = rt.broker.positions
    rt.broker.positions = list
    rt.step()
    working = {o.order_ref: o for o in rt.broker.open_orders()}
    assert working[pos.stop_ref].remaining == 2  # never cancelled on the read alone
    assert N.UNPROTECTED_POSITION in rt.notifier.kinds()
    rt.broker.positions = real


def test_n1_a_flat_read_persisting_over_the_settle_period_cancels_the_stop(h):
    h.execute("p1", open_action())
    broker_shows(h, 0)
    h.coord.sync()
    assert protective(h) == {"STP": 4, "TGT": 4}
    cycles(h, h.config.settle_seconds - 2)
    assert protective(h) == {"STP": 4, "TGT": 4}  # not yet spread over the settle period
    cycles(h, 4)
    assert protective(h) == {}  # corroborated: nothing may cover what the broker does not hold
    assert C.UNPROTECTED_POSITION in open_kinds(h)


def test_n1_a_failed_read_restarts_the_run(h):
    h.execute("p1", open_action())
    broker_shows(h, 0)
    h.coord.sync()
    cycles(h, h.config.settle_seconds - 2)
    shown = h.broker.positions

    def broken():
        raise BrokerError("invented: positions unreadable")

    h.broker.positions = broken
    h.coord.sync()
    h.broker.positions = shown
    cycles(h, 4)
    assert protective(h) == {"STP": 4, "TGT": 4}  # the run started again after the failure


def foreign(h, side: OrderSide, quantity: int, ref: str) -> None:
    """Someone else trades the contract in the account (invented), visible in executions."""
    from tradingbot.execution.broker import OrderIntent, OrderType

    h.broker.set_quote(SYMBOL, "49.98", "50.02")
    h.broker.submit(OrderIntent(
        order_ref=ref, contract=h.broker.resolve(open_action().instrument), side=side,
        quantity=quantity, order_type=OrderType.MKT,
    ))


def test_n1_an_exit_fill_after_the_ideas_last_change_reduces_cover_by_exactly_its_quantity(h):
    h.execute("p1", open_action())
    h.clock.advance(1)
    foreign(h, OrderSide.SELL, 1, "FOREIGN-1")
    assert h.broker.positions()[0].quantity == 3
    broker_shows(h, 0)  # and the position view is (transiently) flat as well
    h.coord.sync()
    assert protective(h) == {"STP": 3, "TGT": 3}  # exactly the 1 sold, never the flat read
    assert differs_open(h)


def test_p1_probe_an_old_manual_sale_never_explains_a_later_flat_read(h):
    """Reviewer's probe: a stray manual long was bought (yesterday) and sold by hand earlier
    today; later the system opens 4; a transient flat read must change nothing."""
    foreign(h, OrderSide.BUY, 2, "MANUAL-BUY")
    h.clock.advance(10 * 3600)  # past midnight UTC: only today's executions are visible
    foreign(h, OrderSide.SELL, 2, "MANUAL-SELL")
    h.clock.advance(60)
    h.broker.set_quote(SYMBOL, "49.98", "50.02")
    assert h.execute("p1", open_action()).state == IntentState.FILLED
    assert protective(h) == {"STP": 4, "TGT": 4}
    h.broker.positions = list  # a transient flat read
    h.coord.sync()
    assert protective(h) == {"STP": 4, "TGT": 4}
    assert differs_open(h)


def test_p1_an_old_sale_plus_a_transient_flat_read_changes_nothing(h):
    foreign(h, OrderSide.SELL, 1, "OLD-SALE")  # predates the idea (invented)
    h.clock.advance(60)
    h.broker.set_quote(SYMBOL, "49.98", "50.02")
    h.execute("p1", open_action())
    broker_shows(h, 0)
    h.coord.sync()
    assert protective(h) == {"STP": 4, "TGT": 4} and differs_open(h)


def test_p1_the_same_execution_explains_a_reduction_only_once(h):
    h.execute("p1", open_action())
    h.clock.advance(1)
    foreign(h, OrderSide.SELL, 1, "FOREIGN-1")
    h.coord.sync()
    assert protective(h) == {"STP": 3, "TGT": 3}
    assert "EXECUTION_ATTRIBUTED" in h.events()
    h.restart()  # the attribution is journaled, not held in memory
    broker_shows(h, 0)  # a later transient flat read: the used sale explains nothing more
    h.coord.sync()
    assert protective(h) == {"STP": 3, "TGT": 3}


def test_p1_a_sale_belongs_to_the_idea_it_followed_not_to_a_later_one(h):
    h.broker.script_next_submit("REST")
    a = h.execute("p1", open_action())  # idea A (sorted first), its entry resting
    assert a.state == IntentState.WORKING
    h.clock.advance(1)
    h.execute("p2", open_action())  # idea B, same contract, filled
    h.clock.advance(1)
    foreign(h, OrderSide.SELL, 1, "FOREIGN-1")  # after B's fill, before A's
    h.clock.advance(1)
    h.broker.fill(a.order_ref)  # A's ledger change comes after the sale
    broker_shows(h, 0)
    h.coord.sync()
    assert protective(h, "p1:open") == {"STP": 4, "TGT": 4}  # A: the sale predates it
    assert protective(h, "p2:open") == {"STP": 3, "TGT": 3}  # B: reduced by exactly 1


def test_p1_a_sale_on_the_wrong_side_explains_nothing(h):
    h.execute("p1", open_action())
    h.clock.advance(1)
    foreign(h, OrderSide.BUY, 1, "FOREIGN-BUY")  # adds to a long: no reduction to explain
    broker_shows(h, 0)
    h.coord.sync()
    assert protective(h) == {"STP": 4, "TGT": 4} and differs_open(h)


def test_n1_short_mirror(h):
    h.execute("p1", open_action(side=PositionSide.SHORT, stop="52", target="45"))
    assert h.broker.positions()[0].quantity == -4
    real = h.broker.positions
    broker_shows(h, -2)  # read alone: the stop stays
    h.coord.sync()
    assert protective(h) == {"STP": 4, "TGT": 4} and differs_open(h)
    h.broker.positions = real
    h.coord.sync()
    assert protective(h) == {"STP": 4, "TGT": 4} and not differs_open(h)
    broker_shows(h, -2)  # persisting over the settle period: reduced
    cycles(h, h.config.settle_seconds + 2)
    assert protective(h) == {"STP": 2, "TGT": 2}
    h.broker.positions = real
    h.coord.sync()


def test_p1_short_mirror(h):
    foreign(h, OrderSide.BUY, 1, "OLD-BUY")  # an old buy, then a short idea opens
    h.clock.advance(60)
    h.broker.set_quote(SYMBOL, "49.98", "50.02")
    h.execute("p1", open_action(side=PositionSide.SHORT, stop="52", target="45"))
    real = h.broker.positions
    broker_shows(h, 0)
    h.coord.sync()
    assert protective(h) == {"STP": 4, "TGT": 4}  # the old buy explains nothing
    h.broker.positions = real
    h.clock.advance(1)
    foreign(h, OrderSide.SELL, 1, "WRONG-SIDE")  # adds to the short: explains nothing
    h.coord.sync()
    assert protective(h) == {"STP": 4, "TGT": 4}
    h.clock.advance(1)
    # The old buy (+1) and the wrong-side sell (-1) cancel: broker -4, ledger -4. A later buy of
    # 2 (broker -2) is an exit for a short after the idea's last change: cover reduced by 2.
    foreign(h, OrderSide.BUY, 2, "COVER-2")
    h.coord.sync()
    assert protective(h) == {"STP": 2, "TGT": 2}


def test_n1_a_failed_positions_read_changes_nothing(h):
    h.execute("p1", open_action())
    real = h.broker.positions
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] > 1:  # the sync's own read succeeds, the protection check's fails
            raise BrokerError("invented: positions unreadable")
        return real()

    h.broker.positions = flaky
    h.coord.sync()
    assert protective(h) == {"STP": 4, "TGT": 4}
    assert h.coord.store.position("p1:open").protection != "CONFIRMED"
    assert C.BROKER_STATE_UNKNOWN in open_kinds(h)
