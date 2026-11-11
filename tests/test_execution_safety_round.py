"""Safety review round (EXECUTION_CORE_safety_review.md): one reproducing test per finding.

Invented tickers, toy prices and invented wording only. No network, no broker port.
"""

from __future__ import annotations

from decimal import Decimal

from execution_harness import Harness, action, open_action

from tradingbot.execution.broker import (
    TERMINAL_STATUSES,
    WORKING_STATUSES,
    OrderStatus,
)
from tradingbot.execution.coordinator import PROTECTION_UNVERIFIED, _intent_state
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
