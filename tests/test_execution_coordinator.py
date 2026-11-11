"""ExecutionCoordinator: write-ahead, never-resend recovery, protection, follow-ups, checks."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from execution_harness import APPROVE, SYMBOL, Harness, action, open_action, source

from tradingbot.execution.broker import OrderIntent, OrderSide, OrderStatus, OrderType, QuoteQuality
from tradingbot.execution.coordinator import (
    UNPROTECTED_POSITION,
    ExecutionConfig,
    ExecutionRefused,
    default_halt_file,
    order_ref_for,
)
from tradingbot.execution.sim_broker import SimulatedCrash
from tradingbot.execution.store import IntentState
from tradingbot.models import Action, GateDecision, MockPolicy, PositionSide


@pytest.fixture
def h(tmp_path):
    return Harness(tmp_path)


def opened(h: Harness, sid: str = "p1", **kw):
    out = h.execute(sid, open_action(**kw))
    return out, h.coord.store.position(f"{sid}:open")


# -------------------------------------------------------------------------- identity and authority


def test_order_ref_is_deterministic_per_source_revision_action():
    a = order_ref_for("p1", 1, "open")
    assert a == order_ref_for("p1", 1, "open")
    assert (
        len(
            {
                a,
                order_ref_for("p1", 2, "open"),
                order_ref_for("p1", 1, "add"),
                order_ref_for("p2", 1, "open"),
            }
        )
        == 4
    )
    assert a.startswith("TB-") and len(a) <= 64


@pytest.mark.parametrize("status", ["HOLD", "BLOCK", "NO_TRADE", "RECONCILE"])
def test_only_an_approved_gate_decision_reaches_the_broker(h, status):
    with pytest.raises(ExecutionRefused):
        h.coord.execute(source("p1"), open_action(), GateDecision(status=status), now=h.clock.now())
    assert h.broker.submit_calls == 0 and h.coord.store.intents() == []


def test_intent_is_durable_before_submission(h):
    seen = {}
    real_submit = h.broker.submit

    def checking_submit(order, children=()):
        # A separate connection, as a restarted process would see it.
        from tradingbot.execution.store import ExecutionStore

        intent = ExecutionStore(h.folder / "exec.sqlite3").intent(order.order_ref)
        seen["state"] = intent.state if intent else None
        seen["orders"] = [
            o.order_ref for o in ExecutionStore(h.folder / "exec.sqlite3").orders(order.order_ref)
        ]
        return real_submit(order, children)

    h.broker.submit = checking_submit
    out, _ = opened(h)
    assert seen["state"] == IntentState.RECORDED
    assert seen["orders"] == [out.order_ref, f"{out.order_ref}.STP", f"{out.order_ref}.TGT"]


# --------------------------------------------------------------------------- entry with protection


def test_open_with_stop_fills_and_confirms_protection(h):
    out, pos = opened(h)
    assert out.state == IntentState.FILLED and out.filled_quantity == 4
    assert out.avg_fill_price == "50.0200" and out.protection == "CONFIRMED"
    assert out.replay_status == "EXECUTED"
    assert pos.status == "OPEN" and pos.quantity == 4 and pos.stop_price == "48"
    working = h.working()
    assert working[pos.stop_ref].quantity == 4 and working[pos.stop_ref].stop_price == Decimal(48)
    assert working[pos.target_ref].limit_price == Decimal(55)
    entry = h.broker.orders_with_ref(out.order_ref)[0]
    assert entry.order_type == OrderType.LMT and entry.limit_price == Decimal(
        "50.50"
    )  # collar bound
    assert h.events(out.order_ref)[:3] == ["INTENT_RECORDED", "SUBMIT", "ACK"]
    assert "FILL" in h.events(out.order_ref)
    assert h.coord.reconcile().clean


def test_missing_stop_child_raises_unprotected_position(h):
    h.broker.script_next_submit("DROP_STOP_CHILD")
    out, pos = opened(h)
    assert out.state == IntentState.FILLED
    assert out.protection == "UNPROTECTED" and out.replay_status == "EXCEPTION"
    [exc] = h.coord.store.exceptions(open_only=True)
    assert exc.kind == UNPROTECTED_POSITION and exc.idea_id == pos.idea_id
    report = h.coord.reconcile()
    [d] = report.discrepancies
    assert d.kind.value == "POSITION_WITHOUT_PROTECTIVE_STOP" and d.explained_by == exc.id
    # Explained by the open exception, yet never clean: a position without confirmed protection is
    # a safety discrepancy (safety review S1).
    assert not report.clean


def test_partial_entry_fill_is_working_and_protected_for_the_filled_quantity(h):
    h.broker.script_next_submit("PARTIAL", quantity=1)
    out, pos = opened(h)
    assert out.state == IntentState.WORKING and out.filled_quantity == 1
    assert out.protection == "CONFIRMED" and pos.quantity == 1
    assert h.working()[pos.stop_ref].remaining == 1
    h.broker.fill(out.order_ref)
    h.coord.sync()
    assert h.coord.intent(out.order_ref).state == IntentState.FILLED
    assert h.coord.store.position(pos.idea_id).quantity == 4
    assert h.working()[pos.stop_ref].remaining == 4
    assert h.coord.reconcile().clean


def test_resting_entry_then_cancelled_marks_idea_not_filled(h):
    h.broker.script_next_submit("REST")
    out, pos = opened(h)
    assert (
        out.state == IntentState.WORKING and out.protection == "PENDING" and pos.status == "PENDING"
    )
    h.broker.cancel(out.order_ref)
    h.coord.sync()
    assert h.coord.intent(out.order_ref).state == IntentState.CANCELLED
    assert h.coord.store.position(pos.idea_id).status == "NOT_FILLED"
    assert h.working() == {} and h.coord.reconcile().clean


def test_rejected_order(h):
    h.broker.script_next_submit("REJECT", reason="INVENTED_REJECT")
    out, pos = opened(h)
    assert out.state == IntentState.REJECTED and out.replay_status == "REJECTED"
    assert out.reasons == ["INVENTED_REJECT"]
    assert pos.status == "NOT_FILLED" and pos.quantity == 0
    assert h.broker.positions() == [] and h.coord.reconcile().clean


# ---------------------------------------------------------------------------- never resend blindly


def test_duplicate_delivery_produces_no_second_order(h):
    first, _ = opened(h)
    calls = h.broker.submit_calls
    again = h.execute("p1", open_action())
    assert (
        again.duplicate and again.order_ref == first.order_ref and again.state == IntentState.FILLED
    )
    assert h.broker.submit_calls == calls
    assert len(h.broker.orders_with_ref(first.order_ref)) == 1
    assert "DUPLICATE_DELIVERY" in h.events(first.order_ref)


def test_lost_ack_with_order_placed_is_adopted_not_resent(h):
    h.broker.script_next_submit("LOST_ACK_PLACED")
    out, pos = opened(h)
    assert out.state == IntentState.FILLED and out.protection == "CONFIRMED"
    assert h.broker.submit_calls == 1 and len(h.broker.orders_with_ref(out.order_ref)) == 1
    events = h.events(out.order_ref)
    assert events.index("ACK_LOST") < events.index("ADOPTED_FROM_BROKER")
    assert "NO_TRACE_AT_BROKER" not in events
    assert pos.quantity == 4


def test_lost_ack_without_order_is_never_resent_and_opens_an_exception(h):
    # Round 2 (F5): a submitted entry of unknown outcome is never resent automatically.
    h.broker.script_next_submit("LOST_ACK_NOT_PLACED")
    out, _ = opened(h)
    assert out.state == IntentState.UNRESOLVED  # "no trace" is not concluded before the settle
    assert h.broker.submit_calls == 1
    h.clock.advance(h.config.settle_seconds + 1)
    [out] = h.coord.recover()
    assert out.state == IntentState.UNRESOLVED
    assert h.broker.submit_calls == 1 and h.broker.orders_with_ref(out.order_ref) == []
    assert "UNKNOWN_OUTCOME_ENTRY_NOT_RESENT" in h.events(out.order_ref)
    assert "NO_TRACE_AT_BROKER" not in h.events(out.order_ref)


def _three_lookups(h):
    for _ in range(3):
        h.clock.advance(h.config.settle_seconds / 2 + 1)
        h.broker.set_quote("ZZQ", "49.98", "50.02")
        h.coord.recover()


def test_resend_budget_exhausted_stays_unresolved_and_is_reported_stuck(h):
    # The budget now bounds exit resends only (entries are never resent, F5).
    opened(h)
    h.broker.script_next_submit("LOST_ACK_NOT_PLACED")
    h.broker.script_next_submit("LOST_ACK_NOT_PLACED")
    close = h.execute("p2", action("close", Action.CLOSE, idea="p1:open"))
    assert close.state == IntentState.UNRESOLVED and close.replay_status == "UNRESOLVED"
    calls = h.broker.submit_calls
    _three_lookups(h)  # settled, no trace in enough lookups: the one resend (also lost)
    assert h.broker.submit_calls == calls + 1
    _three_lookups(h)
    _three_lookups(h)  # still no trace, budget spent: no third send
    assert h.broker.submit_calls == calls + 1
    h.clock.advance(120)
    kinds = [d.kind.value for d in h.coord.reconcile().unexplained]
    assert "INTENT_STUCK_UNRESOLVED" in kinds


def test_failed_lookup_keeps_intent_unresolved_until_the_broker_answers(h):
    h.broker.script_next_submit("LOST_ACK_PLACED")
    h.broker.lookup_failures_after_submit = 1  # the post-submit lookup fails (the pre-trade sync must succeed, F1)
    out, _ = opened(h)
    assert out.state == IntentState.UNRESOLVED
    assert "LOOKUP_FAILED" in h.events(out.order_ref)
    h.coord.recover()
    assert h.coord.intent(out.order_ref).state == IntentState.FILLED
    assert h.broker.submit_calls == 1 and h.coord.reconcile().clean


def test_restart_after_broker_accepted_adopts_the_order(h):
    h.broker.script_next_submit("CRASH_AFTER_PLACE")
    with pytest.raises(SimulatedCrash):
        opened(h)
    ref = order_ref_for("p1", 1, "open")
    assert h.coord.intent(ref).state == IntentState.RECORDED
    h.restart()
    [recovered] = h.coord.recover()
    assert recovered.state == IntentState.FILLED and recovered.protection == "CONFIRMED"
    assert len(h.broker.orders_with_ref(ref)) == 1
    assert "RECOVERY_LOOKUP" in h.events(ref) and "ADOPTED_FROM_BROKER" in h.events(ref)
    again = h.execute("p1", open_action())  # redelivery after the restart
    assert again.duplicate and len(h.broker.orders_with_ref(ref)) == 1
    assert h.coord.reconcile().clean


def test_restart_inside_the_submit_call_is_an_unknown_outcome_never_resent(h):
    # Dying inside broker.submit cannot be told apart from dying after the request left (F5).
    def dying_submit(order, children=()):
        raise SimulatedCrash("died inside the submit call")

    h.broker.submit = dying_submit
    with pytest.raises(SimulatedCrash):
        opened(h)
    h.restart()
    h.clock.advance(h.config.settle_seconds + 1)
    [recovered] = h.coord.recover()
    ref = order_ref_for("p1", 1, "open")
    assert recovered.state == IntentState.RECORDED and h.broker.orders_with_ref(ref) == []
    assert "UNKNOWN_OUTCOME_ENTRY_NOT_RESENT" in h.events(ref)


def test_restart_before_the_request_was_made_sends_it_after_positive_no_trace(h):
    ref = h.record_never_sent("p1", open_action())
    [recovered] = h.coord.recover()
    assert recovered.state == IntentState.FILLED
    assert "NO_TRACE_AT_BROKER" in h.events(ref) and len(h.broker.orders_with_ref(ref)) == 1


# -------------------------------------------------------------------------------------------- halt


def test_halt_switch_blocks_submission_and_is_journaled(h):
    h.halt()
    out, pos = opened(h)
    assert out.state == IntentState.HALTED and out.replay_status == "HALTED"
    assert h.broker.submit_calls == 0 and pos.status == "NOT_FILLED"
    assert "HALTED" in h.events(out.order_ref)
    assert h.coord.reconcile().clean


def test_halt_also_blocks_a_recovery_resubmission(h):
    ref = h.record_never_sent("p1", open_action())
    h.halt()
    h.coord.recover()
    assert h.coord.intent(ref).state == IntentState.HALTED
    assert h.broker.submit_calls == 0


def test_default_halt_file_location(monkeypatch):
    monkeypatch.delenv("TRADINGBOT_HALT_FILE", raising=False)
    assert default_halt_file() == Path.home() / ".local" / "state" / "tradingbot" / "HALT"
    monkeypatch.setenv("TRADINGBOT_HALT_FILE", "/invented/elsewhere/HALT")
    assert ExecutionConfig().halt_file == Path("/invented/elsewhere/HALT")


# -------------------------------------------------------------------------------------- follow-ups


def test_add_resizes_protective_orders(h):
    _, pos = opened(h)
    out = h.execute("p2", action("add", Action.ADD, idea=pos.idea_id, entry="50"))
    assert (
        out.state == IntentState.FILLED
        and out.filled_quantity == 2
        and out.protection == "CONFIRMED"
    )
    assert h.coord.store.position(pos.idea_id).quantity == 6
    w = h.working()
    assert w[pos.stop_ref].remaining == 6 and w[pos.target_ref].remaining == 6
    assert "PROTECTIVE_RESIZED" in h.events(pos.stop_ref)
    assert h.coord.reconcile().clean


def test_partial_reduce_is_reduce_only_and_shrinks_protection(h):
    _, pos = opened(h)
    out = h.execute("p2", action("trim", Action.REDUCE, idea=pos.idea_id, fraction="0.25"))
    assert out.state == IntentState.FILLED and out.filled_quantity == 1
    order = h.broker.orders_with_ref(out.order_ref)[0]
    # A marketable limit from the fresh quote, not a market order (safety review S2).
    assert order.reduce_only and order.side == OrderSide.SELL and order.order_type == OrderType.LMT
    assert h.coord.store.position(pos.idea_id).quantity == 3
    w = h.working()
    assert w[pos.stop_ref].remaining == 3 and w[pos.target_ref].remaining == 3
    assert h.coord.reconcile().clean


def test_close_cancels_protective_orders_and_leaves_no_orphan(h):
    _, pos = opened(h)
    out = h.execute("p2", action("close", Action.CLOSE, idea=pos.idea_id))
    assert out.state == IntentState.FILLED and out.filled_quantity == 4
    p = h.coord.store.position(pos.idea_id)
    assert p.status == "CLOSED" and p.quantity == 0
    assert h.working() == {} and h.broker.positions() == []
    assert h.coord.reconcile().clean


def test_close_after_the_stop_already_fired_cannot_open_a_short(h):
    _, pos = opened(h)
    h.broker.set_quote(SYMBOL, "47.50", "47.55")  # stop triggers at the broker
    out = h.execute("p2", action("close", Action.CLOSE, idea=pos.idea_id))
    assert out.state == IntentState.REFUSED and out.reasons == ["NO_OPEN_FOLLOWER_POSITION"]
    assert h.coord.store.position(pos.idea_id).status == "CLOSED"
    assert h.broker.positions() == [] and h.working() == {}
    assert h.coord.reconcile().clean


def test_exit_larger_than_broker_position_is_refused(h):
    _, pos = opened(h)
    foreign = OrderIntent(
        order_ref="FOREIGN-1",
        contract=h.broker.resolve(open_action().instrument),
        side=OrderSide.SELL,
        quantity=3,
        order_type=OrderType.MKT,
    )
    h.broker.submit(foreign)  # someone else trades the account
    out = h.execute("p2", action("close", Action.CLOSE, idea=pos.idea_id))
    assert out.state == IntentState.REFUSED and out.reasons[0].startswith(
        "EXIT_EXCEEDS_BROKER_POSITION"
    )
    kinds = {d.kind.value for d in h.coord.reconcile().unexplained}
    assert "QUANTITY_MISMATCH" in kinds


def test_stop_trigger_is_applied_to_the_ledger_from_the_fill(h):
    _, pos = opened(h)
    h.broker.set_quote(SYMBOL, "47.90", "47.95")
    assert h.coord.store.position(pos.idea_id).quantity == 4  # nothing inferred before sync
    h.coord.sync()
    p = h.coord.store.position(pos.idea_id)
    assert p.status == "CLOSED" and p.quantity == 0
    assert [f["role"] for f in h.coord.store.fills()] == ["ENTRY", "STOP"]


def test_modify_amends_the_working_stop_and_confirms_it(h):
    _, pos = opened(h)
    out = h.execute("p2", action("raise", Action.MODIFY, idea=pos.idea_id, stop="49"))
    assert out.state == IntentState.APPLIED and out.replay_status == "EXECUTED"
    assert h.working()[pos.stop_ref].stop_price == Decimal(49)
    assert h.coord.store.position(pos.idea_id).stop_price == "49"
    assert "AMENDMENT_CONFIRMED" in h.events(out.order_ref)


def test_modify_with_lost_ack_is_confirmed_by_lookup(h):
    _, pos = opened(h)
    h.broker.script_next_modify("LOST_ACK_APPLIED")
    out = h.execute("p2", action("raise", Action.MODIFY, idea=pos.idea_id, stop="49"))
    assert out.state == IntentState.APPLIED
    assert "ACK_LOST" in h.events(out.order_ref)


def test_modify_lost_before_reaching_broker_is_reissued(h):
    _, pos = opened(h)
    h.broker.script_next_modify("LOST_ACK_NOT_APPLIED")
    out = h.execute("p2", action("raise", Action.MODIFY, idea=pos.idea_id, stop="49"))
    assert out.state == IntentState.APPLIED
    assert "AMENDMENT_NOT_SEEN" in h.events(out.order_ref)
    assert h.working()[pos.stop_ref].stop_price == Decimal(49)


def test_modify_rejected_raises_exception_and_keeps_old_stop(h):
    _, pos = opened(h)
    h.broker.script_next_modify("REJECT")
    out = h.execute("p2", action("raise", Action.MODIFY, idea=pos.idea_id, stop="49"))
    assert out.state == IntentState.REJECTED
    assert h.coord.store.position(pos.idea_id).stop_price == "48"
    assert [e.kind for e in h.coord.store.exceptions(open_only=True)] == ["MODIFY_NOT_CONFIRMED"]


# ----------------------------------------------------------------------- quote sanity and exposure


@pytest.mark.parametrize(
    "quote, reason",
    [
        (
            {"bid": None, "ask": None, "quality": QuoteQuality.UNAVAILABLE},
            "BROKER_QUOTE_UNAVAILABLE",
        ),
        (
            {"bid": "49.98", "ask": "50.02", "quality": QuoteQuality.DELAYED},
            "BROKER_QUOTE_QUALITY_DELAYED",
        ),
        (
            {"bid": "49.98", "ask": "50.02", "quality": QuoteQuality.FROZEN},
            "BROKER_QUOTE_QUALITY_FROZEN",
        ),
        ({"bid": "50.05", "ask": "50.01"}, "BROKER_QUOTE_CROSSED"),
        ({"bid": "0", "ask": "50.02"}, "BROKER_QUOTE_NON_POSITIVE"),
        ({"bid": "51.40", "ask": "51.45"}, "BROKER_QUOTE_OUTSIDE_COLLAR"),
    ],
)
def test_entry_refused_on_bad_broker_quote(h, quote, reason):
    h.broker.set_quote(SYMBOL, **quote)
    out, pos = opened(h)
    assert out.state == IntentState.REFUSED and out.reasons == [reason]
    assert h.broker.submit_calls == 0 and pos is None
    assert "REFUSED" in h.events(out.order_ref)


def test_entry_refused_on_stale_broker_quote(h):
    h.clock.advance(h.policy.max_quote_age_seconds + 1)
    out, _ = opened(h)
    assert out.reasons == ["BROKER_QUOTE_STALE"] and h.broker.submit_calls == 0


def test_delayed_quotes_can_be_accepted_by_configuration(tmp_path):
    h = Harness(
        tmp_path, accepted_quote_qualities=frozenset({QuoteQuality.LIVE, QuoteQuality.DELAYED})
    )
    h.broker.set_quote(SYMBOL, "49.98", "50.02", quality=QuoteQuality.DELAYED)
    out, _ = opened(h)
    assert out.state == IntentState.FILLED


def test_per_instrument_exposure_cap(tmp_path):
    h = Harness(
        tmp_path,
        policy=MockPolicy(
            quantity=4, max_notional_usd=Decimal(1000), max_instrument_exposure_usd=Decimal(300)
        ),
    )
    first, _ = opened(h, "p1")
    assert first.state == IntentState.FILLED
    second, _ = opened(h, "p2")
    assert second.state == IntentState.REFUSED
    assert second.reasons[0].startswith("INSTRUMENT_EXPOSURE_CAP")


def test_aggregate_exposure_cap_counts_working_entries(tmp_path):
    h = Harness(
        tmp_path,
        policy=MockPolicy(
            quantity=4, max_notional_usd=Decimal(1000), max_aggregate_exposure_usd=Decimal(250)
        ),
    )
    h.broker.script_next_submit("REST")
    first, _ = opened(h, "p1")
    assert first.state == IntentState.WORKING
    h.broker.set_quote("QQZ", "19.99", "20.01")
    out = h.execute("p2", open_action(symbol="QQZ", entry="20", stop="19", target="22"))
    assert out.state == IntentState.REFUSED and out.reasons[0].startswith("AGGREGATE_EXPOSURE_CAP")


def test_account_mismatch_is_refused(tmp_path):
    h = Harness(tmp_path, expected_account="SIM-PAPER-9999")
    out, _ = opened(h)
    assert out.state == IntentState.REFUSED and out.reasons == ["BROKER_ACCOUNT_MISMATCH"]
    assert h.broker.submit_calls == 0


def test_refused_intent_is_not_retried_on_redelivery(h):
    h.broker.set_quote(SYMBOL, None, None, quality=QuoteQuality.UNAVAILABLE)
    opened(h)
    h.broker.set_quote(SYMBOL, "49.98", "50.02")
    again = h.execute("p1", open_action())
    assert again.duplicate and again.state == IntentState.REFUSED and h.broker.submit_calls == 0


def test_short_entry_collar_and_protection(h):
    out = h.execute("s1", open_action(side=PositionSide.SHORT, stop="52", target="45"))
    assert out.state == IntentState.FILLED and out.protection == "CONFIRMED"
    entry = h.broker.orders_with_ref(out.order_ref)[0]
    assert entry.side == OrderSide.SELL and entry.limit_price == Decimal("49.50")
    assert [p.quantity for p in h.broker.positions()] == [-4]
    stop = h.working()[f"{out.order_ref}.STP"]
    assert stop.side == OrderSide.BUY and stop.status == OrderStatus.SUBMITTED
    assert h.coord.reconcile().clean


def test_gate_reasons_are_carried_on_the_intent(h):
    out, _ = opened(h)
    assert h.coord.intent(out.order_ref).gate_reasons == APPROVE.reasons
