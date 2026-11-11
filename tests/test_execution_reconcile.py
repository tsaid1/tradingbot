"""Reconciliation: typed, read-only discrepancies between the follower ledger and the broker."""

from __future__ import annotations

from decimal import Decimal

import pytest
from execution_harness import INSTRUMENT, SYMBOL, Harness, open_action

from tradingbot.execution.broker import OrderIntent, OrderSide, OrderType
from tradingbot.execution.reconcile import DiscrepancyKind as K


@pytest.fixture
def h(tmp_path):
    return Harness(tmp_path)


def kinds(h: Harness) -> list[K]:
    return [d.kind for d in h.coord.reconcile().unexplained]


def foreign(h: Harness, ref: str, side: OrderSide, qty: int, limit: str | None = None):
    c = h.broker.resolve(INSTRUMENT)
    kind = OrderType.LMT if limit else OrderType.MKT
    h.broker.submit(
        OrderIntent(
            order_ref=ref,
            contract=c,
            side=side,
            quantity=qty,
            order_type=kind,
            limit_price=Decimal(limit) if limit else None,
        )
    )


def test_clean_after_open(h):
    h.execute("p1", open_action())
    report = h.coord.reconcile()
    assert report.clean and report.ledger_ideas_open == 1 and report.broker_open_orders == 2


def test_unexplained_broker_position(h):
    foreign(h, "OTHER-1", OrderSide.BUY, 3)
    assert kinds(h) == [K.UNEXPLAINED_BROKER_POSITION]


def test_ledger_position_missing_at_broker_and_orphaned_protection(h):
    h.execute("p1", open_action())
    foreign(h, "OTHER-1", OrderSide.SELL, 4)  # the account is flattened outside the ledger
    got = kinds(h)
    assert K.LEDGER_POSITION_MISSING_AT_BROKER in got


def test_quantity_mismatch(h):
    h.execute("p1", open_action())
    foreign(h, "OTHER-1", OrderSide.BUY, 2)
    assert kinds(h) == [K.QUANTITY_MISMATCH]


def test_position_without_protective_stop(h):
    out = h.execute("p1", open_action())
    h.broker.cancel(f"{out.order_ref}.STP")  # cancelled at the broker, ledger not told
    assert kinds(h) == [K.POSITION_WITHOUT_PROTECTIVE_STOP]


def test_orphan_working_order(h):
    foreign(h, "OTHER-2", OrderSide.BUY, 1, limit="40")
    assert kinds(h) == [K.ORPHAN_WORKING_ORDER]


def test_oversized_protective_order_is_reported(h):
    out = h.execute("p1", open_action())
    h.broker.modify(f"{out.order_ref}.STP", quantity=9)
    got = kinds(h)
    assert K.ORPHAN_WORKING_ORDER in got and K.POSITION_WITHOUT_PROTECTIVE_STOP in got


def test_unapplied_execution_until_synced(h):
    h.execute("p1", open_action())
    h.broker.set_quote(SYMBOL, "47.90", "47.95")  # the stop fires; the ledger has not synced
    got = kinds(h)
    assert K.UNAPPLIED_EXECUTION in got and K.LEDGER_POSITION_MISSING_AT_BROKER in got
    h.coord.sync()
    assert kinds(h) == []


def test_intent_stuck_unresolved(h):
    h.broker.script_next_submit("LOST_ACK_PLACED")
    h.broker.lookup_failures = 2
    h.execute("p1", open_action())
    assert K.INTENT_STUCK_UNRESOLVED not in kinds(h)  # not yet stuck
    h.clock.advance(61)
    assert K.INTENT_STUCK_UNRESOLVED in kinds(h)


def test_broker_unreadable_is_a_discrepancy_not_a_pass(h):
    h.broker.lookup_failures = 1
    assert kinds(h) == [K.BROKER_UNREADABLE]


def test_reconcile_is_read_only(h):
    h.execute("p1", open_action())
    h.broker.set_quote(SYMBOL, "47.90", "47.95")
    before_orders = h.broker.all_orders()
    before_ledger = h.coord.ledger()
    before_journal = len(h.coord.store.journal())
    calls = h.broker.submit_calls
    h.coord.reconcile()
    assert h.broker.all_orders() == before_orders and h.coord.ledger() == before_ledger
    assert len(h.coord.store.journal()) == before_journal and h.broker.submit_calls == calls
