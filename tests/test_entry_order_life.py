"""The life of an entry order (lead's decision, 12 November 2026): an unfilled entry never outlives
the validity of its signal, and never outlives the publisher's own exit.

1. EXPIRY: an OPEN or ADD still working when its source post is older than the limit that allowed
   it to be sent (the execution ``max_intent_age_seconds``; for an anchored entry the smaller of
   that and the anchor's signal age) is cancelled and confirmed by the execution core's sync, and
   ends CANCELLED with ``ENTRY_EXPIRED_UNFILLED``. A partial fill keeps its position and protection;
   only the remainder is cancelled. Never resent, never re-priced.
2. PUBLISHER'S EXIT: a CLOSE or REDUCE on an idea whose entry is still working and unfilled
   cancels that entry and is recorded as handled (``ENTRY_CANCELLED_BEFORE_FILL``); any other
   follow-up on such an idea cancels it too and is held.

Through the whole mock service on the shipped mapping wherever possible. Invented data.
"""

from __future__ import annotations

import tempfile
from decimal import Decimal
from pathlib import Path

from execution_harness import SYMBOL, Harness, action, open_action
from runtime_harness import MAPPING, Runtime, post
from test_entry_rule_paper_only import anchored_gate, run_entry

from tradingbot.execution.coordinator import (
    ENTRY_CANCELLED_BEFORE_FILL,
    ENTRY_EXPIRED_UNFILLED,
    ENTRY_REMAINDER_NOT_CANCELLED,
)
from tradingbot.execution.store import IntentState
from tradingbot.models import Action

RESTING = "We are now long $TLT at 88.30. Stop 86.00."  # the mock ask is 88.42: it rests
CLOSE = "Closing our $TLT long at 88.40."


def entries(rt):
    return [o for o in rt.broker.all_orders() if o.parent_ref is None and o.side.value == "BUY"]


def service(*posts):
    rt = Runtime(Path(tempfile.mkdtemp()), list(posts), mapping_file=MAPPING)
    return rt


def coord(rt):
    return rt.service.coordinator


def entry_intent(rt):
    [e] = entries(rt)
    return coord(rt).intent(e.order_ref)


def row(rt, action):
    return next(a for a in rt.actions() if a["action"] == action)


def open_kinds(c):
    return [e.kind for e in c.store.exceptions(open_only=True)]


def flat_and_clean(rt):
    assert not rt.broker.positions() or all(p.quantity == 0 for p in rt.broker.positions())
    assert rt.broker.open_orders() == []
    assert coord(rt).reconcile().clean


# ---------------------------------------------------------------- the reviewer's two scenarios


def test_a_resting_buy_is_cancelled_when_the_publisher_closes():
    rt = service(post("1", 0, RESTING), post("2", 20, CLOSE))
    rt.run(2)
    assert entries(rt)[0].status.value in ("Submitted", "PreSubmitted")
    rt.run(5)  # the close arrives
    close = row(rt, "CLOSE")
    assert close["status"] == "CANCELLED", close
    assert ENTRY_CANCELLED_BEFORE_FILL in close["reasons"]
    assert entry_intent(rt).state == IntentState.CANCELLED
    assert ENTRY_CANCELLED_BEFORE_FILL in entry_intent(rt).reasons
    rt.clock.advance(3600)
    rt.broker.set_quote("TLT", "88.23", "88.25", received_at=rt.clock.now())
    rt.run(3)
    assert entries(rt)[0].filled_quantity == 0
    flat_and_clean(rt)
    rt.service.close()


def test_with_no_close_a_resting_buy_expires_and_never_fills():
    rt = service(post("1", 0, RESTING))
    rt.run(2)
    assert entry_intent(rt).state == IntentState.WORKING
    rt.run(30)  # 150 s: past the 120 s limit
    intent = entry_intent(rt)
    assert intent.state == IntentState.CANCELLED and ENTRY_EXPIRED_UNFILLED in intent.reasons
    assert "ENTRY_EXPIRED_UNFILLED" in [e.event for e in coord(rt).store.journal(intent.order_ref)]
    rt.clock.advance(3600)
    rt.broker.set_quote("TLT", "88.23", "88.25", received_at=rt.clock.now())
    rt.run(3)
    assert entries(rt)[0].filled_quantity == 0 and rt.broker.submit_calls == 1  # never resent
    flat_and_clean(rt)
    assert ENTRY_REMAINDER_NOT_CANCELLED not in open_kinds(coord(rt))
    rt.service.close()


# ---------------------------------------------------------------- unaffected cases


def test_an_entry_that_fills_before_expiry_is_unaffected():
    rt = service(post("1", 0, RESTING), post("2", 300, CLOSE))
    rt.run(2)
    rt.broker.set_quote("TLT", "88.28", "88.30", received_at=rt.clock.now())  # inside the window
    rt.run(2)
    assert entries(rt)[0].filled_quantity == 1
    assert entry_intent(rt).state == IntentState.FILLED
    rt.run(30)  # past the limit: nothing to expire
    assert ENTRY_EXPIRED_UNFILLED not in entry_intent(rt).reasons
    [pos] = [p for p in coord(rt).ledger() if p.quantity]
    stop = next(o for o in rt.broker.open_orders() if o.order_ref == pos.stop_ref)
    assert stop.stop_price == Decimal("86.00")
    rt.broker.set_quote("TLT", "88.38", "88.40", received_at=rt.clock.now())
    rt.run(40)  # the close at 300 s
    assert row(rt, "CLOSE")["status"] == "EXECUTED"
    flat_and_clean(rt)
    rt.service.close()


def test_a_resting_explicit_entry_lifecycle():
    """Restores the lifecycle coverage the fixture move to the touch removed: rest, fill when the
    ask comes down inside the window, protected by its stop, then closed."""
    rt = service(post("1", 0, RESTING), post("2", 60, CLOSE))
    rt.run(3)
    [e] = entries(rt)
    assert e.limit_price == Decimal("88.30") and e.filled_quantity == 0
    rt.broker.set_quote("TLT", "88.27", "88.29", received_at=rt.clock.now())
    rt.run(1)
    assert entries(rt)[0].filled_quantity == 1
    [pos] = [p for p in coord(rt).ledger() if p.quantity]
    assert coord(rt)._protection(pos.idea_id) == "CONFIRMED"
    rt.broker.set_quote("TLT", "88.38", "88.40", received_at=rt.clock.now())
    rt.run(12)
    assert row(rt, "CLOSE")["status"] == "EXECUTED"
    flat_and_clean(rt)
    rt.service.close()


# ---------------------------------------------------------------- the boundary, clocks, restarts


def _rest(h, entry="50.00", **kw):
    h.broker.script_next_submit("REST")
    return h.execute("p1", open_action(entry=entry, **kw))


def test_expiry_is_exclusive_at_the_limit(tmp_path):
    h = Harness(tmp_path)  # max_intent_age_seconds 120 by default
    out = _rest(h)
    h.clock.advance(120)
    h.coord.sync()
    assert h.coord.intent(out.order_ref).state == IntentState.WORKING  # exactly 120 s: still valid
    h.clock.advance(1)
    h.coord.sync()
    i = h.coord.intent(out.order_ref)
    assert i.state == IntentState.CANCELLED and ENTRY_EXPIRED_UNFILLED in i.reasons
    assert out.order_ref not in h.working()
    h.coord.close()


def test_a_clock_that_jumps_expires_at_the_next_sync(tmp_path):
    h = Harness(tmp_path)
    out = _rest(h)
    h.clock.advance(7200)
    h.coord.sync()
    assert h.coord.intent(out.order_ref).state == IntentState.CANCELLED
    h.broker.set_quote(SYMBOL, "49.90", "49.95")
    h.coord.sync()
    assert h.broker.executions() == [] and h.broker.submit_calls == 1
    h.coord.close()


def test_expiry_across_a_restart_cancels_at_the_first_cycle():
    rt = service(post("1", 0, RESTING))
    rt.run(2)
    ref = entries(rt)[0].order_ref
    rt.clock.advance(600)  # the process was down; the order kept working at the broker
    rt.restart()
    assert ref in {o.order_ref for o in rt.broker.open_orders()}
    rt.run(1)
    assert coord(rt).intent(ref).state == IntentState.CANCELLED
    assert ref not in {o.order_ref for o in rt.broker.open_orders()}
    rt.service.close()


def test_an_unconfirmed_expiry_cancel_blocks_the_instrument_and_resends_nothing(tmp_path):
    h = Harness(tmp_path)
    out = _rest(h)
    h.clock.advance(121)
    for _ in range(2):  # this sync and the one the next execute runs
        h.broker.script_next_cancel("REJECT")
    h.coord.sync()
    kinds = open_kinds(h.coord)
    assert ENTRY_REMAINDER_NOT_CANCELLED in kinds
    assert out.order_ref in h.working() and h.broker.submit_calls == 1
    assert ENTRY_EXPIRED_UNFILLED not in h.coord.intent(out.order_ref).reasons
    # the instrument is blocked for new exposure
    blocked = h.execute("p2", open_action("o2", entry="50.02"))
    assert blocked.state == IntentState.REFUSED and h.broker.submit_calls == 1
    h.coord.sync()  # the next sync retries; once it is confirmed the block lifts
    assert h.coord.intent(out.order_ref).state == IntentState.CANCELLED
    assert ENTRY_REMAINDER_NOT_CANCELLED not in open_kinds(h.coord)
    h.coord.close()


def test_an_anchored_entry_that_rests_expires_by_its_own_tighter_limit(tmp_path):
    h = Harness(tmp_path)
    h.broker.script_next_submit("REST")  # the ask moved away from the anchored limit
    out = run_entry(h, anchored_gate(limit="50.02"))  # anchor signal age 90 s < 120 s
    assert out.state == IntentState.WORKING
    h.clock.advance(90)
    h.coord.sync()
    assert h.coord.intent(out.order_ref).state == IntentState.WORKING
    h.clock.advance(1)
    h.coord.sync()
    i = h.coord.intent(out.order_ref)
    assert i.state == IntentState.CANCELLED and ENTRY_EXPIRED_UNFILLED in i.reasons
    h.coord.close()


def test_a_partial_fill_keeps_its_position_and_protection_and_cancels_the_remainder(tmp_path):
    h = Harness(tmp_path)  # 4 shares
    h.broker.script_next_submit("PARTIAL", quantity=1)
    out = h.execute("p1", open_action(entry="50.02"))
    assert out.filled_quantity == 1 and out.state == IntentState.WORKING
    h.broker.set_quote(SYMBOL, "50.04", "50.06")  # the rest no longer marketable
    h.clock.advance(121)
    h.coord.sync()
    i = h.coord.intent(out.order_ref)
    assert i.state == IntentState.CANCELLED and ENTRY_EXPIRED_UNFILLED in i.reasons
    assert out.order_ref not in h.working()
    [pos] = h.coord.ledger()
    assert pos.quantity == 1 and pos.status == "OPEN"
    stop = h.working()[pos.stop_ref]
    assert stop.remaining == 1 and h.coord._protection(pos.idea_id) == "CONFIRMED"
    assert h.broker.submit_calls == 1
    h.coord.close()


def test_an_add_that_rests_expires_too(tmp_path):
    h = Harness(tmp_path)
    h.execute("p1", open_action(entry="50.02"))
    h.broker.script_next_submit("REST")
    add = h.execute("p2", action("add", Action.ADD, idea="p1:open", entry="50.00"))
    assert add.state == IntentState.WORKING
    h.clock.advance(121)
    h.coord.sync()
    i = h.coord.intent(add.order_ref)
    assert i.state == IntentState.CANCELLED and ENTRY_EXPIRED_UNFILLED in i.reasons
    [pos] = h.coord.ledger()
    assert pos.quantity == 4 and h.coord._protection(pos.idea_id) == "CONFIRMED"
    h.coord.close()


# ---------------------------------------------------------------- follow-ups


def test_a_follow_up_other_than_an_exit_cancels_the_entry_and_holds(tmp_path):
    h = Harness(tmp_path)
    h.broker.script_next_submit("REST")
    h.post("p1", open_action(entry="50.00"))
    [ref] = list(h.working())[:1]
    res = h.post("p2", action("m", Action.MODIFY, idea="p1:open", stop="49"))
    [o] = res.outcomes
    assert o.status == "HOLD" and ENTRY_CANCELLED_BEFORE_FILL in o.reasons, o
    assert not any(r.order_ref == ref for r in h.broker.open_orders())
    h.coord.close()


def test_a_close_whose_entry_cancel_is_unconfirmed_holds_and_blocks(tmp_path):
    h = Harness(tmp_path)
    h.broker.script_next_submit("REST")
    h.post("p1", open_action(entry="50.00"))
    for _ in range(4):
        h.broker.script_next_cancel("REJECT")
    res = h.post("p2", action("c", Action.CLOSE, idea="p1:open"))
    [o] = res.outcomes
    assert o.status == "HOLD" and "ENTRY_CANCEL_UNCONFIRMED" in o.reasons, o
    assert ENTRY_REMAINDER_NOT_CANCELLED in open_kinds(h.coord)
    h.coord.close()


def test_a_close_after_a_partial_fill_cancels_the_remainder_and_closes_what_is_held(tmp_path):
    h = Harness(tmp_path)
    h.broker.script_next_submit("PARTIAL", quantity=1)
    h.post("p1", open_action(entry="50.02"))
    h.broker.set_quote(SYMBOL, "50.04", "50.06")
    h.broker.set_quote(SYMBOL, "49.98", "50.06")
    res = h.post("p2", action("c", Action.CLOSE, idea="p1:open"))
    [o] = res.outcomes
    assert o.status == "EXECUTED", o
    assert h.broker.open_orders() == [] and h.coord.reconcile().clean
    h.coord.close()


# ---------------------------------------------------------------- unchanged behaviour


def test_the_halt_still_cancels_a_resting_entry(tmp_path):
    h = Harness(tmp_path)
    out = _rest(h)
    h.halt()
    h.coord.sync()
    assert out.order_ref not in h.working()
    assert "HALT_ENTRY_CANCELLED" in h.events(out.order_ref)
    h.coord.close()


def test_a_second_open_still_holds_while_an_entry_rests():
    rt = service(post("1", 0, RESTING), post("2", 20, "We are now long $TLT at 88.42. Stop 86.00."))
    rt.run(7)
    assert len(entries(rt)) == 1
    second = [a for a in rt.actions() if a["action"] == "OPEN"][1]
    assert second["status"] == "HOLD", second
    assert "EXISTING_OPEN_IDEA_SAME_SIDE_IN_CONTEXT" in second["reasons"]
    rt.service.close()


def test_the_stop_distance_where_limit_and_stated_entry_differ_by_tick_snapping():
    """Restores the case where the limit and the stated entry differ: only by tick snapping. A
    stated 88.425 is sent at 88.42 (a buy never above it). A stop at 84.00 is 4.9989 % below the
    limit but 5.0042 % below the stated entry: it holds; 84.01 (4.993 %) trades."""
    rt = service(post("1", 0, "We are now long $TLT at 88.425. Stop 84.00."))
    rt.run(3)
    assert entries(rt) == [], [a["reasons"] for a in rt.actions()]
    assert "PROTECTIVE_STOP_TOO_FAR_FROM_ENTRY" in rt.actions()[0]["reasons"]
    rt.service.close()
    rt = service(post("1", 0, "We are now long $TLT at 88.425. Stop 84.01."))
    rt.run(3)
    [e] = entries(rt)
    assert e.limit_price == Decimal("88.42") and e.filled_quantity == 1
    rt.service.close()
