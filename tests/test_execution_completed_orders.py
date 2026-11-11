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

from tradingbot.execution.broker import OrderStatus
from tradingbot.execution.coordinator import STALE_AFTER_UNKNOWN_OUTCOME, order_ref_for
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
    h.broker.lookup_failures = 2


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
    [adopted] = [e for e in h.coord.store.journal(out.order_ref) if e.event == "ADOPTED_FROM_BROKER"]
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
    out = _open(h)
    assert seen, "completed orders were not consulted before the resend"
    assert out.state == IntentState.FILLED and h.broker.submit_calls == 2
    [no_trace] = [e for e in h.coord.store.journal(out.order_ref) if e.event == "NO_TRACE_AT_BROKER"]
    assert "completed_orders" in no_trace.detail["rule"]


def test_no_trace_and_stale_entry_ends_in_explicit_exception_never_resent(h):
    _unknown_and_unlookable(h, "LOST_ACK_NOT_PLACED")
    out = _open(h)
    h.clock.advance(h.policy.max_signal_age_seconds + 30)
    h.coord.recover()
    intent = h.coord.intent(out.order_ref)
    assert intent.state == IntentState.REFUSED and intent.reasons == [STALE_AFTER_UNKNOWN_OUTCOME]
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
    _unknown_and_unlookable(h, "LOST_ACK_NOT_PLACED")
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
        c.execute("UPDATE exec_intents SET source_published_at=NULL WHERE order_ref=?",
                  (out.order_ref,))
    h.coord.recover()
    assert h.coord.intent(out.order_ref).state == IntentState.REFUSED
    assert h.broker.submit_calls == 1


def test_intent_records_the_source_publication_time(h):
    out = _open(h)
    assert h.coord.intent(out.order_ref).source_published_at == h.clock.now()
    assert out.order_ref == order_ref_for("p1", 1, "open")
