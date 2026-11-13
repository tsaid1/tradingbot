"""The exit price policy and the bounded life of an exit order (owner's decision, 13 Nov 2026):

"For the closing sell limit, use the fresh executable bid as the initial price rather than
automatically applying a 1% downward haircut. If it doesn't fill, use bounded cancellation and
broker-state reconciliation. Preserve protection until flat. Never submit another exit while the
previous order's outcome is unknown."

1. ``exit_price_policy="TOUCH"``: a SELL exit exactly at the fresh bid (snapped up, never below it),
   a BUY exit at the ask (snapped down). The default ("COLLAR") is unchanged.
2. ``exit_validity_seconds``: an exit still working that long after it was sent is cancelled by
   ``sync`` with broker confirmation; confirmed unfilled, it ends CANCELLED with
   EXIT_UNFILLED_CANCELLED and the protective stop is placed again for what is held, once, in the
   same sync. Unconfirmed: EXIT_CANCEL_UNCONFIRMED, nothing placed. A fill that races the cancel
   ends flat with nothing placed. Default None: unchanged.
3. No second exit while an earlier exit's outcome is unknown (pinned; it was already refused).

SimBroker (strict), invented data only.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from execution_harness import SYMBOL, Harness, action, open_action

from tradingbot.execution.broker import OrderSide, OrderStatus, OrderType
from tradingbot.execution.coordinator import (
    EXIT_CANCEL_UNCONFIRMED,
    EXIT_NOT_NETTED,
    EXIT_OUTCOME_UNKNOWN,
    EXIT_UNFILLED_CANCELLED,
    UNPROTECTED_POSITION,
    ExecutionConfig,
)
from tradingbot.execution.store import IntentState
from tradingbot.models import Action, MockPolicy

BOUNDED = {"exit_price_policy": "TOUCH", "exit_validity_seconds": 10.0}


def one_share(tmp_path, **config) -> Harness:
    return Harness(tmp_path, policy=MockPolicy(quantity=1, quantity_per_add=1,
                                               max_notional_usd=Decimal(1000)), **config)


def opened(h: Harness):
    out = h.execute("p1", open_action(entry="50.02", stop="48", target=None))
    assert out.filled_quantity == 1
    [pos] = h.coord.ledger()
    assert h.coord._protection(pos.idea_id) == "CONFIRMED"
    return pos


def close(h: Harness, n: int = 1, *, rest: bool = True):
    if rest:
        h.broker.script_next_submit("REST")
    return h.execute(f"c{n}", action(f"close{n}", Action.CLOSE, idea="p1:open"))


def open_kinds(h: Harness) -> list[str]:
    return [e.kind for e in h.coord.store.exceptions(open_only=True)]


def stops(h: Harness):
    return [o for o in h.broker.open_orders() if o.order_type == OrderType.STP]


# ---------------------------------------------------------------- 1. the exit price policy


def test_the_default_exit_price_keeps_the_collar(tmp_path):
    h = one_share(tmp_path)
    assert ExecutionConfig().exit_price_policy == "COLLAR"
    opened(h)
    out = close(h, rest=False)
    assert out.state == IntentState.FILLED
    assert h.coord.intent(out.order_ref).limit_price == "49.49"  # 49.98 x 0.99 = 49.4802, up
    h.coord.close()


def test_a_touch_close_sells_at_the_fresh_bid(tmp_path):
    h = one_share(tmp_path, exit_price_policy="TOUCH")
    opened(h)
    h.broker.set_quote(SYMBOL, "49.97", "49.99")
    out = close(h, rest=False)
    assert out.state == IntentState.FILLED
    assert h.coord.intent(out.order_ref).limit_price == "49.97"
    assert h.broker.executions()[-1].price == Decimal("49.97")
    h.coord.close()


@pytest.mark.parametrize(("side", "bid", "ask", "want"), [
    (OrderSide.SELL, "49.971", "49.99", "49.98"),  # snapped UP: never below the observed bid
    (OrderSide.BUY, "49.97", "49.989", "49.98"),  # snapped DOWN: never above the observed ask
    (OrderSide.SELL, "49.97", "49.99", "49.97"),
    (OrderSide.BUY, "49.97", "49.99", "49.99"),
])
def test_the_touch_is_snapped_never_through_the_observed_touch(tmp_path, side, bid, ask, want):
    h = one_share(tmp_path, exit_price_policy="TOUCH")
    h.broker.set_quote(SYMBOL, bid, ask)
    contract = h.broker.resolve(open_action().instrument)
    limit, problem = h.coord._exit_limit(contract, side, h.clock.now())
    assert problem is None and limit == Decimal(want)
    h.coord.close()


def test_a_touch_exit_still_needs_a_usable_quote(tmp_path):
    h = one_share(tmp_path, exit_price_policy="TOUCH")
    opened(h)
    h.clock.advance(31)  # the quote is older than max_quote_age_seconds
    out = close(h, rest=False)
    assert out.state == IntentState.REFUSED and "BROKER_QUOTE_STALE" in out.reasons
    assert stops(h) and h.broker.submit_calls == 1  # the stop stays; nothing sent
    h.coord.close()


# ---------------------------------------------------------------- 2. the bounded life of an exit


def test_without_a_validity_an_unfilled_exit_keeps_working(tmp_path):
    h = one_share(tmp_path, exit_price_policy="TOUCH")
    opened(h)
    out = close(h)
    assert out.state == IntentState.WORKING
    h.clock.advance(3600)
    h.coord.sync()
    assert h.coord.intent(out.order_ref).state == IntentState.WORKING
    assert out.order_ref in h.working() and not stops(h)
    assert h.broker.submit_calls == 2
    h.coord.close()


def test_an_unfilled_exit_is_cancelled_and_the_stop_is_placed_again(tmp_path):
    h = one_share(tmp_path, **BOUNDED)
    pos = opened(h)
    out = close(h)
    assert out.state == IntentState.WORKING and not stops(h)  # the stop was cancelled for it
    assert h.coord._protection(pos.idea_id) == "EXITING"
    h.clock.advance(10)
    h.coord.sync()
    assert h.coord.intent(out.order_ref).state == IntentState.WORKING  # exactly 10 s: valid
    h.clock.advance(1)
    h.coord.sync()
    i = h.coord.intent(out.order_ref)
    assert i.state == IntentState.CANCELLED and EXIT_UNFILLED_CANCELLED in i.reasons
    assert out.order_ref not in h.working()
    events = h.events(out.order_ref)
    assert "EXIT_EXPIRED_CANCEL_CONFIRMED" in events and "PROTECTIVE_STOP_REPLACED" in events
    [new] = h.coord.ledger()
    assert new.quantity == 1 and new.stop_ref == f"{pos.stop_ref}2"
    [stop] = stops(h)
    assert stop.order_ref == new.stop_ref and stop.parent_ref is None
    assert stop.side == OrderSide.SELL and stop.remaining == 1 and stop.stop_price == 48
    assert h.coord._protection(new.idea_id) == "CONFIRMED"
    assert UNPROTECTED_POSITION not in open_kinds(h)
    assert h.broker.submit_calls == 3  # bracket, exit, restored stop
    for _ in range(3):  # placed once: later syncs place nothing
        h.clock.advance(30)
        h.coord.sync()
    assert h.broker.submit_calls == 3 and len(stops(h)) == 1
    assert h.coord.reconcile().clean
    h.coord.close()


def test_a_second_close_after_a_restored_stop_closes_flat(tmp_path):
    h = one_share(tmp_path, **BOUNDED)
    opened(h)
    close(h, 1)
    h.clock.advance(11)
    h.coord.sync()
    assert len(stops(h)) == 1
    h.broker.set_quote(SYMBOL, "49.96", "49.98")
    out = close(h, 2, rest=False)
    assert out.state == IntentState.FILLED, out
    assert h.coord.intent(out.order_ref).limit_price == "49.96"
    [flat] = h.coord.ledger()
    assert flat.quantity == 0 and h.broker.open_orders() == []
    assert h.broker.submit_calls == 4  # bracket, exit 1, restored stop, exit 2
    assert h.coord.reconcile().clean
    h.coord.close()


def test_a_fill_that_races_the_cancel_ends_flat_once_with_nothing_placed(tmp_path):
    h = one_share(tmp_path, **BOUNDED)
    opened(h)
    out = close(h)
    h.clock.advance(11)
    h.broker.script_next_cancel("FILL_FIRST")
    h.coord.sync()
    i = h.coord.intent(out.order_ref)
    assert i.state == IntentState.FILLED and EXIT_UNFILLED_CANCELLED not in i.reasons
    [pos] = h.coord.ledger()
    assert pos.quantity == 0 and pos.status == "CLOSED"
    assert h.broker.open_orders() == [] and len(h.broker.executions()) == 2
    assert h.broker.submit_calls == 2  # no stop placed again, no second sale
    assert "PROTECTIVE_STOP_REPLACE_SUBMIT" not in h.events(out.order_ref)
    h.clock.advance(60)
    h.coord.sync()
    assert h.broker.submit_calls == 2 and h.coord.reconcile().clean
    h.coord.close()


def test_an_unconfirmed_exit_cancel_places_nothing_and_resolves_on_broker_evidence(tmp_path):
    h = one_share(tmp_path, **BOUNDED)
    pos = opened(h)
    out = close(h)
    h.clock.advance(11)
    h.broker.script_next_cancel("REJECT")
    h.coord.sync()
    i = h.coord.intent(out.order_ref)
    assert i.state == IntentState.WORKING and out.order_ref in h.working()
    assert EXIT_CANCEL_UNCONFIRMED in open_kinds(h)
    assert not stops(h) and h.broker.submit_calls == 2  # nothing placed for the idea
    assert h.coord._protection(pos.idea_id) == "EXITING"
    h.coord.sync()  # the next sync asks again; confirmed now
    i = h.coord.intent(out.order_ref)
    assert i.state == IntentState.CANCELLED and EXIT_UNFILLED_CANCELLED in i.reasons
    assert EXIT_CANCEL_UNCONFIRMED not in open_kinds(h)
    assert len(stops(h)) == 1 and h.broker.submit_calls == 3
    h.coord.close()


def test_a_restored_stop_whose_ack_is_lost_is_never_resent(tmp_path):
    h = one_share(tmp_path, **BOUNDED)
    opened(h)
    close(h)
    h.clock.advance(11)
    h.broker.script_next_submit("LOST_ACK_NOT_PLACED")
    h.coord.sync()
    [pos] = h.coord.ledger()
    assert not stops(h) and UNPROTECTED_POSITION in open_kinds(h)
    assert h.coord._protection(pos.idea_id) == "UNPROTECTED"
    for _ in range(3):
        h.clock.advance(30)
        h.coord.sync()
    assert h.broker.submit_calls == 3 and not stops(h)
    h.coord.close()


def test_a_restored_stop_the_broker_rejects_is_reported_unprotected(tmp_path):
    h = one_share(tmp_path, **BOUNDED)
    opened(h)
    close(h)
    h.clock.advance(11)
    h.broker.script_next_submit("REJECT")
    h.coord.sync()
    assert not stops(h) and UNPROTECTED_POSITION in open_kinds(h)
    h.clock.advance(30)
    h.coord.sync()
    assert h.broker.submit_calls == 3  # one placement, never repeated
    h.coord.close()


def test_an_expired_exit_found_after_a_restart_is_cancelled_and_protected(tmp_path):
    h = one_share(tmp_path, **BOUNDED)
    opened(h)
    out = close(h)
    h.clock.advance(600)  # the process was down; the exit kept working at the broker
    h.restart()
    h.coord.sync()
    assert h.coord.intent(out.order_ref).state == IntentState.CANCELLED
    assert len(stops(h)) == 1 and h.broker.submit_calls == 1  # counted since the restart
    h.coord.close()


# ---------------------------------------------------------------- 3. no second exit while unknown


@pytest.mark.parametrize("config", [BOUNDED, {}], ids=["bounded", "default"])
def test_no_second_close_while_the_first_ones_ack_is_lost(tmp_path, config):
    h = one_share(tmp_path, **config)
    opened(h)
    h.broker.script_next_submit("LOST_ACK_NOT_PLACED")
    first = h.execute("c1", action("close1", Action.CLOSE, idea="p1:open"))
    assert first.state == IntentState.UNRESOLVED
    second = close(h, 2, rest=False)
    assert second.state == IntentState.REFUSED and EXIT_NOT_NETTED in second.reasons, second
    assert h.broker.submit_calls == 2
    h.coord.close()


def test_no_second_close_while_the_first_was_recorded_and_never_answered(tmp_path):
    h = one_share(tmp_path, **BOUNDED)
    opened(h)
    ref = h.record_never_sent("c1", action("close1", Action.CLOSE, idea="p1:open"))
    assert h.coord.intent(ref).state == IntentState.RECORDED  # the process restarted
    second = close(h, 2, rest=False)
    assert second.state == IntentState.REFUSED and EXIT_NOT_NETTED in second.reasons, second
    assert h.broker.submit_calls == 0  # nothing sent since the restart
    h.coord.close()


def test_no_second_close_while_the_first_ones_cancel_is_unconfirmed(tmp_path):
    h = one_share(tmp_path, **BOUNDED)
    opened(h)
    first = close(h, 1)
    h.clock.advance(11)
    for _ in range(4):  # every cancel fails: expiry, the next syncs and the netting's own
        h.broker.script_next_cancel("REJECT")
    h.coord.sync()
    second = close(h, 2, rest=False)
    assert second.state == IntentState.REFUSED and EXIT_NOT_NETTED in second.reasons, second
    assert any(r.startswith("EXIT_CANCEL_UNCONFIRMED") for r in second.reasons)
    assert h.broker.submit_calls == 2 and first.order_ref in h.working()
    h.coord.close()


def test_in_bounded_mode_an_exit_of_unknown_outcome_is_never_resent(tmp_path):
    h = one_share(tmp_path, **BOUNDED)
    opened(h)
    h.broker.script_next_submit("LOST_ACK_NOT_PLACED")
    first = h.execute("c1", action("close1", Action.CLOSE, idea="p1:open"))
    assert first.state == IntentState.UNRESOLVED
    for _ in range(6):  # well past the settle period and the no-trace lookups
        h.clock.advance(5)
        h.coord.recover()
    assert h.broker.submit_calls == 2
    assert h.coord.intent(first.order_ref).state == IntentState.UNRESOLVED
    assert EXIT_OUTCOME_UNKNOWN in open_kinds(h)
    h.coord.close()


def test_by_default_an_exit_of_unknown_outcome_is_still_resent_after_no_trace(tmp_path):
    h = one_share(tmp_path, exit_price_policy="TOUCH")
    opened(h)
    h.broker.script_next_submit("LOST_ACK_NOT_PLACED")
    first = h.execute("c1", action("close1", Action.CLOSE, idea="p1:open"))
    for _ in range(6):
        h.clock.advance(5)
        h.broker.set_quote(SYMBOL, "49.98", "50.02")
        h.coord.recover()
    assert h.broker.submit_calls == 3  # the existing no-trace resend, unchanged
    assert h.coord.intent(first.order_ref).state == IntentState.FILLED
    assert OrderStatus.FILLED in {o.status for o in h.broker.orders_with_ref(first.order_ref)}
    h.coord.close()
