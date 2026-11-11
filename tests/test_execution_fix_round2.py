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


# ----------------------------------------------------------------------------------------- F2


def _partial_then_stopped(h: Harness):
    h.broker.script_next_submit("PARTIAL", quantity=1)  # entry fills 1 of 4, 3 keep working
    out = h.execute("p1", open_action())
    assert h.coord.store.position("p1:open").quantity == 1
    assert h.working()[f"{out.order_ref}.STP"].remaining == 1
    return out


def test_f2_a_stop_out_cancels_the_working_entry_remainder(tmp_path):
    h = Harness(tmp_path)
    out = _partial_then_stopped(h)
    h.broker.set_quote(SYMBOL, "47.90", "47.94")  # the stop fires on the 1 held
    h.coord.sync()
    assert h.coord.store.position("p1:open").status == "CLOSED"
    assert out.order_ref not in h.working()  # the 3 left can no longer fill unprotected
    assert "ENTRY_REMAINDER_CANCELLED" in h.events(out.order_ref)
    h.broker.set_quote(SYMBOL, "49.98", "50.02")
    assert h.broker.positions() == []
    assert h.coord.reconcile().clean


def test_f2_an_unconfirmed_remainder_cancel_blocks_new_exposure_in_the_instrument(tmp_path):
    h = Harness(tmp_path)
    _partial_then_stopped(h)
    h.broker.set_quote(SYMBOL, "47.90", "47.94")
    for _ in range(4):  # every retry fails: this sync, its re-read, then the next pre-trade sync
        h.broker.script_next_cancel("REJECT")
    h.coord.sync()
    assert "ENTRY_REMAINDER_NOT_CANCELLED" in _open_kinds(h)
    h.broker.set_quote(SYMBOL, "49.98", "50.02")
    again = h.execute("p2", open_action())
    assert again.state == IntentState.REFUSED
    assert again.reasons[0].startswith("INSTRUMENT_BLOCKED")


def test_f2_a_resize_that_cannot_be_confirmed_blocks_new_exposure(tmp_path):
    h = Harness(tmp_path)
    out = _partial_then_stopped(h)
    for _ in range(3):  # this sync and the pre-trade sync of the next open both fail to resize
        h.broker.script_next_modify("REJECT")
    h.broker.fill(out.order_ref)  # the other 3 fill: the stop must grow from 1 to 4
    h.coord.sync()
    assert h.coord._outcome(out.order_ref).protection == "UNPROTECTED"
    again = h.execute("p2", open_action())
    assert again.state == IntentState.REFUSED
    assert again.reasons[0].startswith("INSTRUMENT_BLOCKED")
