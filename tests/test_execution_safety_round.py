"""Safety review round (EXECUTION_CORE_safety_review.md): one reproducing test per finding.

Invented tickers, toy prices and invented wording only. No network, no broker port.
"""

from __future__ import annotations

from execution_harness import Harness, action, open_action

from tradingbot.execution.broker import (
    TERMINAL_STATUSES,
    WORKING_STATUSES,
    OrderStatus,
)
from tradingbot.execution.coordinator import PROTECTION_UNVERIFIED, _intent_state
from tradingbot.execution.store import IntentState
from tradingbot.models import Action

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
