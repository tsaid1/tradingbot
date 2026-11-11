"""Recovery after a lost acknowledgement consults open orders, executions AND completed orders.

An order the broker rejected or cancelled leaves no trace in ``open_orders()`` or ``executions()``;
only ``completed_orders()`` shows it. Recovery must adopt that terminal state rather than resend,
and must resend an entry or add only when all three sources show nothing and the source post is
still fresh. Invented ticker and prices only.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from execution_harness import Harness, action, open_action

from tradingbot.execution.broker import BrokerError, OrderStatus
from tradingbot.execution.coordinator import (
    STALE_AFTER_UNKNOWN_OUTCOME,
    SUBMIT_ERROR,
    UNKNOWN_OUTCOME_BEYOND_LOOKUP_HORIZON,
    order_ref_for,
)
from tradingbot.execution.store import IntentState
from tradingbot.models import Action


@pytest.fixture
def h(tmp_path):
    return Harness(tmp_path)


def _open(h: Harness, sid: str = "p1"):
    return h.execute(sid, open_action())


def _unknown_and_unlookable(h: Harness, behaviour: str) -> None:
    """The next submit's ack is lost and the follow-up lookup fails, so the intent stays UNRESOLVED
    (the pre-trade sync and the post-ack-loss lookup each consume one failure)."""
    h.broker.script_next_submit(behaviour)
    h.broker.lookup_failures_after_submit = 1  # the post-submit lookup fails (the pre-trade sync must succeed, F1)


# ------------------------------------------------------------------------------- SimBroker surface


def test_sim_broker_reports_terminal_orders_with_ref_and_final_state(h):
    h.broker.script_next_submit("REJECT", reason="INVENTED_REJECT")
    out = _open(h)
    done = {o.order_ref: o for o in h.broker.completed_orders()}
    assert done[out.order_ref].status == OrderStatus.REJECTED
    assert done[out.order_ref].reason == "INVENTED_REJECT"
    assert all(o.status not in (OrderStatus.SUBMITTED,) for o in done.values())
    later = h.clock.now() + timedelta(seconds=1)
    assert h.broker.completed_orders(since=later) == []
    assert out.order_ref not in {o.order_ref for o in h.broker.open_orders()}


def test_completed_orders_is_part_of_the_broker_protocol():
    from tradingbot.execution.broker import Broker

    assert hasattr(Broker, "completed_orders")


# ---------------------------------------------------------------------- terminal at broker: adopt


def test_lost_ack_then_broker_rejected_is_adopted_rejected_not_resent(h):
    h.broker.script_next_submit("LOST_ACK_REJECTED")
    out = _open(h)
    assert out.state == IntentState.REJECTED and out.replay_status == "REJECTED"
    assert h.broker.submit_calls == 1 and len(h.broker.orders_with_ref(out.order_ref)) == 1
    events = h.events(out.order_ref)
    assert events.index("ACK_LOST") < events.index("ADOPTED_FROM_BROKER")
    assert "NO_TRACE_AT_BROKER" not in events
    [adopted] = [
        e for e in h.coord.store.journal(out.order_ref) if e.event == "ADOPTED_FROM_BROKER"
    ]
    assert adopted.detail["completed_orders"][out.order_ref] == "Rejected"
    assert h.coord.store.position("p1:open").status == "NOT_FILLED"
    assert h.coord.reconcile().clean


def test_lost_ack_then_broker_cancelled_is_adopted_cancelled_not_resent(h):
    h.broker.script_next_submit("LOST_ACK_CANCELLED")
    out = _open(h)
    assert out.state == IntentState.CANCELLED and out.replay_status == "CANCELLED"
    assert h.broker.submit_calls == 1
    assert h.coord.store.position("p1:open").status == "NOT_FILLED"
    assert h.coord.reconcile().clean


def test_restart_recovery_adopts_a_rejection_seen_only_in_completed_orders(h):
    _unknown_and_unlookable(h, "LOST_ACK_REJECTED")
    out = _open(h)
    assert out.state == IntentState.UNRESOLVED
    h.restart()
    [recovered] = h.coord.recover()
    assert recovered.state == IntentState.REJECTED
    assert h.broker.submit_calls == 0  # the restarted broker object made no submission at all
    assert len(h.broker.orders_with_ref(out.order_ref)) == 1


def test_failed_completed_orders_lookup_keeps_the_intent_unresolved(h):
    _unknown_and_unlookable(h, "LOST_ACK_REJECTED")
    out = _open(h)

    def unavailable(since=None):
        from tradingbot.execution.broker import BrokerError

        raise BrokerError("invented: completed orders unavailable")

    h.broker.completed_orders = unavailable
    with pytest.raises(BrokerError):  # a failed sync is never swallowed (review F1)
        h.coord.recover()
    assert h.coord.intent(out.order_ref).state == IntentState.UNRESOLVED
    assert h.broker.submit_calls == 1
    assert h.events(out.order_ref).count("LOOKUP_FAILED") == 2


# ------------------------------------------------------------------------ no trace: fresh or stale


def test_no_trace_in_all_three_sources_and_fresh_resubmits(h):
    seen = []
    real = h.broker.completed_orders

    def spy(since=None):
        seen.append(since)
        return real(since)

    h.broker.completed_orders = spy
    h.broker.script_next_submit("LOST_ACK_NOT_PLACED")
    _open(h)
    h.clock.advance(h.config.settle_seconds + 1)
    [out] = h.coord.recover()
    assert seen, "completed orders were not consulted before the resend"
    assert out.state == IntentState.FILLED and h.broker.submit_calls == 2
    [no_trace] = [
        e for e in h.coord.store.journal(out.order_ref) if e.event == "NO_TRACE_AT_BROKER"
    ]
    assert "completed_orders" in no_trace.detail["rule"]


def test_no_trace_and_stale_entry_ends_in_explicit_exception_never_resent(h):
    _unknown_and_unlookable(h, "LOST_ACK_NOT_PLACED")
    out = _open(h)
    h.clock.advance(h.policy.max_signal_age_seconds + 30)
    h.coord.recover()
    intent = h.coord.intent(out.order_ref)
    assert intent.state == IntentState.REFUSED
    assert intent.reasons == [STALE_AFTER_UNKNOWN_OUTCOME, "SOURCE_NOT_FRESH"]
    assert h.broker.submit_calls == 1 and h.broker.orders_with_ref(out.order_ref) == []
    [exc] = [e for e in h.coord.store.exceptions(open_only=True)]
    assert exc.kind == STALE_AFTER_UNKNOWN_OUTCOME and exc.order_ref == out.order_ref
    assert STALE_AFTER_UNKNOWN_OUTCOME in h.events(out.order_ref)
    assert "NO_TRACE_AT_BROKER" not in h.events(out.order_ref)
    assert h.coord.store.position("p1:open").status == "NOT_FILLED"
    h.coord.recover()  # terminal: a later recovery leaves it alone
    assert h.broker.submit_calls == 1
    assert h.coord.reconcile().clean


def test_no_trace_and_stale_add_is_not_resent(h):
    opened = _open(h)
    assert opened.state == IntentState.FILLED
    _unknown_and_unlookable(h, "LOST_ACK_NOT_PLACED")
    add = h.execute("p2", action("add", Action.ADD, idea="p1:open", entry="50"))
    assert add.state == IntentState.UNRESOLVED
    calls = h.broker.submit_calls
    h.clock.advance(h.policy.max_signal_age_seconds + 30)
    h.coord.recover()
    assert h.coord.intent(add.order_ref).state == IntentState.REFUSED
    assert h.broker.submit_calls == calls
    assert h.coord.store.position("p1:open").quantity == 4


def test_no_trace_exit_is_resent_even_when_stale_because_it_only_reduces(h):
    _open(h)
    # The exit is netted against the protective orders before it is sent (review R1), which reads
    # the broker; so the lookup is made to fail only after the submission itself.
    real = h.broker.submit

    def submit_then_unreadable(order, children=()):
        h.broker.submit = real
        h.broker.lookup_failures = 1
        return real(order, children)

    h.broker.script_next_submit("LOST_ACK_NOT_PLACED")
    h.broker.submit = submit_then_unreadable
    close = h.execute("p3", action("close", Action.CLOSE, idea="p1:open"))
    assert close.state == IntentState.UNRESOLVED
    h.clock.advance(h.policy.max_signal_age_seconds + 30)
    h.coord.recover()
    assert h.coord.intent(close.order_ref).state == IntentState.FILLED
    assert h.coord.store.position("p1:open").quantity == 0
    assert h.coord.reconcile().clean


def test_intent_without_a_recorded_publication_time_is_treated_as_stale(h):
    _unknown_and_unlookable(h, "LOST_ACK_NOT_PLACED")
    out = _open(h)
    with h.coord.store.tx() as c:
        c.execute(
            "UPDATE exec_intents SET source_published_at=NULL WHERE order_ref=?", (out.order_ref,)
        )
    h.clock.advance(h.config.settle_seconds + 1)
    h.coord.recover()
    assert h.coord.intent(out.order_ref).state == IntentState.REFUSED
    assert h.broker.submit_calls == 1


def test_intent_records_the_source_publication_time(h):
    out = _open(h)
    assert h.coord.intent(out.order_ref).source_published_at == h.clock.now()
    assert out.order_ref == order_ref_for("p1", 1, "open")


# ------------------------------------------------- safety review R3: no unchecked stale resend


def _lost_then_unlookable_for(h: Harness, seconds: float):
    """Ack lost, the lookup fails, and the broker stays unreadable for ``seconds``."""
    _unknown_and_unlookable(h, "LOST_ACK_NOT_PLACED")
    out = _open(h)
    assert out.state == IntentState.UNRESOLVED
    h.clock.advance(seconds)
    return out


def test_r3_ack_lost_monday_is_not_resent_on_wednesday_at_mondays_limit(h):
    out = _lost_then_unlookable_for(h, 2 * 24 * 3600)
    h.broker.set_quote("ZZQ", "49.98", "50.02")  # Wednesday's (fresh) quote
    h.coord.recover()
    intent = h.coord.intent(out.order_ref)
    assert h.broker.submit_calls == 1 and h.broker.orders_with_ref(out.order_ref) == []
    # Beyond the lookup horizon nothing can be proven absent: a human decides, never a resend.
    assert intent.state == IntentState.UNRESOLVED
    kinds = [e.kind for e in h.coord.store.exceptions(open_only=True)]
    assert kinds == [UNKNOWN_OUTCOME_BEYOND_LOOKUP_HORIZON]


def test_r3_redelivery_of_an_old_unresolved_entry_does_not_bypass_the_checks(h):
    out = _lost_then_unlookable_for(h, h.config.settle_seconds + 1)
    h.broker.set_quote("ZZQ", "52.00", "52.04")  # the market moved outside the collar
    again = h.execute("p1", open_action())  # redelivery (the PRIOR_INTENT path)
    assert again.duplicate and again.state == IntentState.REFUSED
    intent = h.coord.intent(out.order_ref)
    assert intent.reasons == [STALE_AFTER_UNKNOWN_OUTCOME, "BROKER_QUOTE_OUTSIDE_COLLAR"]
    assert h.broker.submit_calls == 1


@pytest.mark.parametrize(
    "setup,reason",
    [
        (
            lambda h: h.broker.set_quote(
                "ZZQ",
                "49.98",
                "50.02",
                received_at=h.clock.now() - timedelta(seconds=h.policy.max_quote_age_seconds + 5),
            ),
            "BROKER_QUOTE_STALE",
        ),
        (lambda h: h.broker.set_quote("ZZQ", "50.10", "50.00"), "BROKER_QUOTE_CROSSED"),
        (
            lambda h: setattr(h.coord.policy, "max_aggregate_exposure_usd", 1),
            "AGGREGATE_EXPOSURE_CAP",
        ),
    ],
)
def test_r3_resend_reruns_quote_sanity_and_exposure_caps(h, setup, reason):
    out = _lost_then_unlookable_for(h, h.config.settle_seconds + 1)
    setup(h)
    h.coord.recover()
    intent = h.coord.intent(out.order_ref)
    assert intent.state == IntentState.REFUSED and intent.reasons[0] == STALE_AFTER_UNKNOWN_OUTCOME
    assert intent.reasons[1].startswith(reason)
    assert h.broker.submit_calls == 1


def test_r3_intent_older_than_the_maximum_age_is_not_resent_even_if_the_source_is_fresh(tmp_path):
    h = Harness(tmp_path, max_intent_age_seconds=30)
    out = _lost_then_unlookable_for(h, 40)  # source still fresh (90 s), intent too old (30 s)
    h.coord.recover()
    assert h.coord.intent(out.order_ref).reasons == [STALE_AFTER_UNKNOWN_OUTCOME, "INTENT_TOO_OLD"]
    assert h.broker.submit_calls == 1


def test_r3_resend_is_still_blocked_by_the_halt(h):
    out = _lost_then_unlookable_for(h, h.config.settle_seconds + 1)
    h.halt()
    h.coord.recover()
    assert h.coord.intent(out.order_ref).state == IntentState.HALTED
    assert h.broker.submit_calls == 1


# --------------------------------------------- safety review R4: no premature "no trace"


def test_r4_definite_submit_error_is_recorded_and_never_resent(h):
    def refuse(order, children=()):
        h.broker.submit_calls += 1
        from tradingbot.execution.broker import BrokerError

        raise BrokerError("invented: order rejected by validation before acceptance")

    h.broker.submit = refuse
    out = _open(h)
    assert out.state == IntentState.REJECTED and out.reasons == [SUBMIT_ERROR]
    h.clock.advance(h.config.settle_seconds + 1)
    h.coord.recover()
    h.execute("p1", open_action())  # redelivery
    assert h.broker.submit_calls == 1
    assert "NO_TRACE_AT_BROKER" not in h.events(out.order_ref)
    assert h.coord.store.position("p1:open").status == "NOT_FILLED"


def test_r4_open_order_lag_does_not_cause_a_duplicate(h):
    """The order is live (resting) at the broker but open_orders() does not show it for 5 s. The
    old code looked once, saw nothing and resent: two live orders. Now it waits for the settle."""
    h.broker.open_orders_lag_seconds = 5
    h.broker.script_next_submit("LOST_ACK_PLACED_RESTING")
    out = _open(h)
    assert out.state == IntentState.UNRESOLVED
    assert "LOOKUP_NOT_SETTLED" in h.events(out.order_ref)
    h.clock.advance(2)
    h.coord.recover()  # inside the lag and the settle: no conclusion, no resend
    assert h.coord.intent(out.order_ref).state == IntentState.UNRESOLVED
    h.clock.advance(h.config.settle_seconds)
    h.coord.recover()  # now visible: adopted
    assert h.coord.intent(out.order_ref).state == IntentState.WORKING
    assert h.broker.submit_calls == 1 and len(h.broker.orders_with_ref(out.order_ref)) == 1
    assert "NO_TRACE_AT_BROKER" not in h.events(out.order_ref)


def test_r4_without_the_settle_the_same_lag_would_have_duplicated(tmp_path):
    """Control for the test above: with settle 0 the lagging order is resent. This proves the lag
    is real in the SimBroker, so it is the settle that prevents the duplicate."""
    h = Harness(tmp_path, settle_seconds=0)
    h.broker.open_orders_lag_seconds = 5
    h.broker.script_next_submit("LOST_ACK_PLACED_RESTING")
    out = _open(h)
    assert len(h.broker.orders_with_ref(out.order_ref)) == 2


def test_r4_filled_order_outside_the_executions_window_is_never_resent(h):
    """Filled long ago: IBKR's executions and completed orders no longer show it. Absence proves
    nothing beyond the lookup horizon, so the intent stays for a human."""
    h.broker.history_seconds = 3600
    h.broker.script_next_submit("LOST_ACK_PLACED")  # placed and filled, ack lost
    h.broker.lookup_failures_after_submit = 1  # the post-submit lookup fails (the pre-trade sync must succeed, F1)
    # No protective children: nothing of this order is left in any lookup once history rolls off.
    out = h.execute("p1", open_action(stop=None, target=None))
    assert out.state == IntentState.UNRESOLVED
    h.clock.advance(h.config.lookup_horizon_seconds + 60)
    h.coord.recover()
    assert h.broker.submit_calls == 1
    assert h.coord.intent(out.order_ref).state == IntentState.UNRESOLVED
    assert len(h.broker.orders_with_ref(out.order_ref)) == 1  # the original fill only
    assert UNKNOWN_OUTCOME_BEYOND_LOOKUP_HORIZON in h.events(out.order_ref)
    h.coord.recover()
    assert h.events(out.order_ref).count(UNKNOWN_OUTCOME_BEYOND_LOOKUP_HORIZON) == 1


def test_r4_sim_broker_history_window_hides_old_executions_and_completed_orders(h):
    out = _open(h)
    assert any(f.order_ref == out.order_ref for f in h.broker.executions())
    h.broker.history_seconds = 3600
    h.clock.advance(7200)
    assert h.broker.executions() == [] and h.broker.completed_orders() == []
