"""Safety review round (EXECUTION_CORE_safety_review.md): one reproducing test per finding.

Invented tickers, toy prices and invented wording only. No network, no broker port.
"""

from __future__ import annotations

from execution_harness import Harness, open_action

from tradingbot.execution.broker import (
    TERMINAL_STATUSES,
    WORKING_STATUSES,
    OrderStatus,
)
from tradingbot.execution.coordinator import _intent_state
from tradingbot.execution.store import IntentState

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
