"""Second fix round (RUNTIME_AND_EXECUTION_FIXES_review.md): one reproducing test per item.

Invented tickers, toy prices and invented wording only. No network, no broker port.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from execution_harness import SYMBOL, Harness, action, open_action
from runtime_harness import ENTRY, Runtime, post

from tradingbot.execution.broker import BrokerError, Fill, OrderSide
from tradingbot.execution.coordinator import BROKER_STATE_UNKNOWN
from tradingbot.execution.store import IntentState
from tradingbot.models import Action


def _open_kinds(h: Harness) -> list[str]:
    return [e.kind for e in h.coord.store.exceptions(open_only=True)]


# ----------------------------------------------------------------------------------------- F1


def test_f1_a_failed_executions_read_blocks_the_add_and_leaves_protection_unknown(tmp_path):
    h = Harness(tmp_path)
    opened = h.execute("p1", open_action())  # long 4, stop 48
    idea = "p1:open"
    assert opened.protection == "CONFIRMED"
    h.broker.set_quote(SYMBOL, "47.90", "47.94")  # the stop fills at the broker
    h.broker.set_quote(SYMBOL, "49.98", "50.02")
    assert h.broker.positions() == []
    real = h.broker.executions

    def failing(since=None):
        raise BrokerError("invented: executions unavailable")

    h.broker.executions = failing
    submits = h.broker.submit_calls
    add = h.execute("p2", action("add", Action.ADD, idea=idea, entry="50"))
    assert add.state == IntentState.REFUSED and "BROKER_UNREADABLE" in add.reasons
    assert h.broker.submit_calls == submits and h.broker.positions() == []
    assert h.coord.store.position(idea).protection == "UNKNOWN"
    assert BROKER_STATE_UNKNOWN in _open_kinds(h)
    with pytest.raises(BrokerError):
        h.coord.recover()  # a failed sync is never swallowed
    h.broker.executions = real
    h.coord.recover()  # a full successful sync applies the stop fill
    pos = h.coord.store.position(idea)
    assert pos.quantity == 0 and pos.status == "CLOSED"
    assert BROKER_STATE_UNKNOWN not in _open_kinds(h)


def test_f1_protection_needs_the_fills_read_after_the_working_orders(tmp_path):
    h = Harness(tmp_path)
    opened = h.execute("p1", open_action())
    idea = "p1:open"
    real = h.broker.executions
    calls = {"n": 0}
    late = Fill(
        exec_id="INVENTED-LATE-1",
        broker_order_id="999",
        order_ref=opened.order_ref,
        contract=h.broker.resolve(open_action().instrument),
        side=OrderSide.BUY,
        quantity=1,
        price=Decimal("50.00"),
        executed_at=h.clock.now(),
    )

    def executions(since=None):
        calls["n"] += 1
        # The first read of this sync misses a fill that the later read shows.
        return real(since) + ([late] if calls["n"] > 1 else [])

    h.broker.executions = executions
    h.coord.sync()
    assert h.coord.store.position(idea).protection != "CONFIRMED"


@pytest.mark.parametrize("read", ["executions", "completed_orders", "positions"])
def test_f1_the_service_broker_check_exercises_every_read(tmp_path, read):
    rt = Runtime(tmp_path, [post("1", 0, ENTRY)])

    def failing(*a, **k):
        raise BrokerError(f"invented: {read} unavailable")

    setattr(rt.broker, read, failing)
    report = rt.step()
    assert report.skipped == "broker_unavailable"
    assert rt.signals() == []  # no signal consumed while a broker read fails
