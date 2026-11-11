"""Safety review round (EXECUTION_CORE_safety_review.md): one reproducing test per finding.

Invented tickers, toy prices and invented wording only. No network, no broker port.
"""

from __future__ import annotations

import os
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest
from execution_harness import SYMBOL, Harness, action, open_action, source

from tradingbot.event_models import ParsedPost
from tradingbot.execution.broker import (
    TERMINAL_STATUSES,
    WORKING_STATUSES,
    OrderIntent,
    OrderSide,
    OrderStatus,
    OrderType,
)
from tradingbot.execution.coordinator import (
    PROTECTION_UNVERIFIED,
    ExecutionCoordinator,
    ExecutionStoreLocked,
    _intent_state,
)
from tradingbot.execution.sim_broker import SimBroker
from tradingbot.execution.store import IntentState
from tradingbot.models import Action, MockPolicy

# ----------------------------------------------------------------------------------------- N1


def test_n1_pending_cancel_is_working_and_api_cancelled_is_terminal(tmp_path):
    assert OrderStatus("PendingCancel") in WORKING_STATUSES
    assert OrderStatus("ApiCancelled") in TERMINAL_STATUSES
    assert _intent_state(OrderStatus("ApiCancelled"), 0, 4) == IntentState.CANCELLED
    assert _intent_state(OrderStatus("PendingCancel"), 1, 4) == IntentState.WORKING
    h = Harness(tmp_path)
    out = h.execute("p1", open_action())
    stop_ref = f"{out.order_ref}.STP"
    real = h.broker.open_orders

    def pending_cancel_view():
        return [
            o.model_copy(update={"status": OrderStatus("PendingCancel")})
            if o.order_ref == stop_ref
            else o
            for o in real()
        ]

    h.broker.open_orders = pending_cancel_view
    h.coord.sync()
    # Still working at the broker: the row is not concluded Cancelled while the cancel is pending.
    assert h.coord.store.order(stop_ref).status == "PendingCancel"


# ----------------------------------------------------------------------------------------- M1


def test_m1_protection_is_not_confirmed_when_the_post_fill_check_failed(tmp_path):
    h = Harness(tmp_path, strict=True)
    opened = h.execute("p1", open_action())
    idea = "p1:open"
    assert opened.protection == "CONFIRMED"
    real = h.broker.open_orders

    def failing_after_the_add_fill():
        # The ADD fill has been applied (4 -> 6): every later open-orders read fails.
        if h.coord.store.position(idea).quantity == 6:
            from tradingbot.execution.broker import BrokerError

            raise BrokerError("invented: open orders unavailable")
        return real()

    h.broker.open_orders = failing_after_the_add_fill
    add = h.execute("p2", action("add", Action.ADD, idea=idea, entry="50"))
    assert add.state == IntentState.FILLED and h.coord.store.position(idea).quantity == 6
    assert add.protection == "UNKNOWN" and add.replay_status == "EXCEPTION"
    assert PROTECTION_UNVERIFIED in [e.kind for e in h.coord.store.exceptions(open_only=True)]
    h.broker.open_orders = real
    h.coord.sync()  # a successful check after the last fill: resized and confirmed
    assert h.coord._outcome(add.order_ref).protection == "CONFIRMED"
    assert h.working()[f"{opened.order_ref}.STP"].remaining == 6
    assert h.coord.store.exceptions(open_only=True) == []


# ----------------------------------------------------------------------------------------- M2


def _capped(tmp_path, cap: int = 250) -> Harness:
    return Harness(
        tmp_path,
        strict=True,
        policy=MockPolicy(
            quantity=4, max_notional_usd=Decimal(1000), max_aggregate_exposure_usd=Decimal(cap)
        ),
    )


def test_m2_an_entry_whose_ack_was_lost_still_counts_against_the_caps(tmp_path):
    h = _capped(tmp_path)
    h.broker.script_next_submit("LOST_ACK_PLACED_RESTING")  # live at the broker, ack lost
    h.broker.lookup_failures = 2  # the pre-trade sync and the follow-up lookup both fail
    first = h.execute("p1", open_action())
    assert first.state == IntentState.UNRESOLVED
    h.broker.set_quote("QQZ", "19.99", "20.01")
    second = h.execute("p2", open_action(symbol="QQZ", entry="20", stop="19", target="22"))
    assert second.state == IntentState.REFUSED
    assert second.reasons[0].startswith("AGGREGATE_EXPOSURE_CAP")


def test_m2_a_recorded_entry_counts_and_does_not_count_against_its_own_resend(tmp_path):
    h = _capped(tmp_path)
    h.broker.script_next_submit("LOST_ACK_NOT_PLACED")
    h.broker.lookup_failures = 2
    first = h.execute("p1", open_action())
    assert first.state == IntentState.UNRESOLVED
    _, total = h.coord._exposure("ZZQ")
    assert total == Decimal("50.50") * 4  # the full intended quantity at its limit
    h.clock.advance(h.config.settle_seconds + 1)
    h.coord.recover()  # no trace: the resend is not refused by its own exposure
    assert h.coord.intent(first.order_ref).state == IntentState.FILLED


# ----------------------------------------------------------------------------------------- M3


def test_m3_a_refused_close_stops_the_group_and_holds_the_open(tmp_path):
    h = Harness(tmp_path, strict=True)
    h.post("p1", open_action())  # idea p1:open, 4 held
    foreign = OrderIntent(
        order_ref="FOREIGN-1",
        contract=h.broker.resolve(open_action().instrument),
        side=OrderSide.SELL,
        quantity=3,
        order_type=OrderType.MKT,
    )
    h.broker.submit(foreign)  # someone else trades the account: the CLOSE will be refused
    h.broker.set_quote("QQZ", "19.99", "20.01")
    quotes = {**h.quotes(), **h.quotes("QQZ")}
    calls = h.broker.submit_calls
    result = h.replay.process(
        source("p2", at=h.clock.now()),
        ParsedPost(
            actions=[
                action("close", Action.CLOSE, idea="p1:open", group="swap"),
                open_action(
                    "open-b", symbol="QQZ", entry="20", stop="19", target="22", group="swap"
                ),
            ]
        ),
        quotes,
        now=h.clock.now(),
    )
    assert [o.status for o in result.outcomes] == ["REFUSED", "HOLD"]
    assert "PARTIAL_GROUP" in result.outcomes[1].reasons
    assert h.broker.submit_calls == calls  # OPEN B never reached the broker
    assert h.coord.store.position("p2:open-b") is None
    assert "PARTIAL_GROUP" in [e.kind for e in h.coord.store.exceptions(open_only=True)]
    report = h.coord.reconcile()
    assert "PARTIAL_GROUP" in [d.kind.value for d in report.discrepancies]
    assert not report.clean


# ----------------------------------------------------------------------------------------- R2


def test_r2_a_reversing_fill_is_recorded_and_blocks_only_that_instrument(tmp_path):
    h = Harness(tmp_path, strict=True)
    opened = h.execute("p1", open_action())
    stop = f"{opened.order_ref}.STP"
    h.broker.modify(stop, quantity=9)  # enlarged outside the bot (e.g. by hand at the broker)
    h.broker.open_orders()  # the pending amendment takes effect
    h.broker.set_quote(SYMBOL, "47.90", "47.95")  # the stop sells 9 against 4 held
    assert sum(p.quantity for p in h.broker.positions()) == -5
    assert h.coord.sync() is True  # no RuntimeError, and again on the next call
    assert h.coord.sync() is True
    [fill] = [f for f in h.coord.store.fills() if f["order_ref"] == stop]
    assert fill["quantity"] == 9  # recorded as it happened
    pos = h.coord.store.position("p1:open")
    assert pos.quantity == 0 and pos.status == "REVERSED"
    [exc] = [e for e in h.coord.store.exceptions(open_only=True) if e.kind == "LEDGER_REVERSAL"]
    assert exc.idea_id == "p1:open" and "5" in exc.detail
    h.broker.set_quote(SYMBOL, "49.98", "50.02")
    blocked = h.execute("p2", open_action())
    assert blocked.state == IntentState.REFUSED
    assert blocked.reasons == ["INSTRUMENT_BLOCKED_LEDGER_REVERSAL"]
    h.broker.set_quote("QQZ", "19.99", "20.01")
    other = h.execute("p3", open_action(symbol="QQZ", entry="20", stop="19", target="22"))
    assert other.state == IntentState.FILLED  # everything else keeps working
    r = h.replay.process(
        source("p4", at=h.clock.now()),
        ParsedPost(actions=[open_action("again", symbol="QQZ", entry="20", stop="19")]),
        h.quotes("QQZ"),
        now=h.clock.now(),
    )
    assert [o.status for o in r.outcomes] == ["EXECUTED"]
    assert "UNEXPLAINED_BROKER_POSITION" in [d.kind.value for d in h.coord.reconcile().discrepancies]


# ----------------------------------------------------------------------------------------- R5

_HOLDER = """
import sys
from tradingbot.execution.coordinator import ExecutionCoordinator
from tradingbot.execution.sim_broker import SimBroker
folder = sys.argv[1]
try:
    coord = ExecutionCoordinator(SimBroker(folder + "/b2.sqlite3"), folder + "/exec.sqlite3")
except Exception as exc:
    print("REFUSED", type(exc).__name__, exc, flush=True)
    sys.exit(3)
print("HOLDING", flush=True)
sys.stdin.readline()
"""


def _child(folder: Path) -> subprocess.Popen:
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
    return subprocess.Popen(
        [sys.executable, "-c", _HOLDER, str(folder)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        env=env,
    )


def test_r5_a_second_coordinator_on_the_same_store_refuses_to_start(tmp_path):
    holder = _child(tmp_path)
    try:
        assert holder.stdout.readline().strip() == "HOLDING"
        # This process cannot start a coordinator on the store the other process holds ...
        with pytest.raises(ExecutionStoreLocked, match="in use by another"):
            ExecutionCoordinator(SimBroker(tmp_path / "b.sqlite3"), tmp_path / "exec.sqlite3")
        # ... nor can a third process.
        third = _child(tmp_path)
        out, _ = third.communicate(timeout=30)
        assert third.returncode == 3 and out.startswith("REFUSED ExecutionStoreLocked")
    finally:
        holder.communicate("\n", timeout=30)
    # Once the holder has gone, the store can be taken; and this process then keeps it.
    mine = ExecutionCoordinator(SimBroker(tmp_path / "b.sqlite3"), tmp_path / "exec.sqlite3")
    late = _child(tmp_path)
    out, _ = late.communicate(timeout=30)
    assert late.returncode == 3 and "in use by another" in out
    mine.close()
