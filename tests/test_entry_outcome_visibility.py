"""What the operator sees after an entry's life ends (review follow-ups to the entry-order life).

1. The signal-action journal row of an OPEN whose resting entry was cancelled (at its signal's age
   limit, or on the publisher's exit) reads CANCELLED with the public reason, from the durable
   execution records (also after a restart). A cancel that is not confirmed never reads as
   cancelled; a fill that beat the cancel reads as the fill.
2. A publisher exit that arrives while an entry's acknowledgement is unknown opens the
   ``EXIT_WHILE_ENTRY_UNKNOWN`` exception and its alert. Nothing is sent, cancelled or resent
   for it; it closes once the entry's outcome is known and the idea is flat, or when the operator
   acknowledges it.

Through the whole mock service where possible. Invented posts and prices only.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from execution_harness import Harness, action, open_action
from runtime_harness import MAPPING, Runtime, post

from tradingbot.execution import operator_catalog as cat
from tradingbot.execution.coordinator import (
    ENTRY_CANCELLED_BEFORE_FILL,
    ENTRY_EXPIRED_UNFILLED,
    EXIT_WHILE_ENTRY_UNKNOWN,
)
from tradingbot.execution.settlement import acknowledge_exception
from tradingbot.execution.store import IntentState
from tradingbot.models import Action
from tradingbot.runtime import notify as N

# Invented: the mock ask is 88.42, so a buy limited at 88.30 rests.
RESTING = "We are now long $TLT at 88.30. Stop 86.00."
CLOSE = "Closing our $TLT long at 88.40."


def service(*posts) -> Runtime:
    return Runtime(Path(tempfile.mkdtemp()), list(posts), mapping_file=MAPPING)


def coord(rt):
    return rt.service.coordinator


def open_row(rt) -> dict:
    [row] = [a for a in rt.actions() if a["action"] == "OPEN"]
    return row


# ---------------------------------------------------------------- 1. the OPEN's journal row


def test_the_open_row_reads_cancelled_after_the_publishers_exit():
    rt = service(post("1", 0, RESTING), post("2", 20, CLOSE))
    rt.run(2)
    assert open_row(rt)["status"] == "WORKING"
    rt.run(5)  # the close arrives; the resting entry is cancelled (confirmed)
    intent = coord(rt).intent(open_row(rt)["order_ref"])
    assert intent.state == IntentState.CANCELLED
    row = open_row(rt)
    assert row["status"] == "CANCELLED", row
    assert ENTRY_CANCELLED_BEFORE_FILL in row["reasons"]
    assert "BROKER_CANCELLED" in row["reasons"]
    settled = [e for e in rt.store.events() if e["event"] == "SIGNAL_ACTION_SETTLED"]
    assert [e["detail"]["status"] for e in settled] == ["CANCELLED"]
    rt.service.close()


def test_the_open_row_reads_cancelled_after_expiry():
    rt = service(post("1", 0, RESTING))
    rt.run(30)  # 150 s: past the 120 s limit
    row = open_row(rt)
    assert row["status"] == "CANCELLED", row
    assert ENTRY_EXPIRED_UNFILLED in row["reasons"]
    rt.service.close()


def test_the_row_is_settled_from_durable_records_after_a_restart():
    """The cancel happens with no service cycle after it (the process then dies): the new
    process settles the row from the execution store alone."""
    rt = service(post("1", 0, RESTING))
    rt.run(2)
    ref = open_row(rt)["order_ref"]
    idea = coord(rt).intent(ref).idea_id
    assert coord(rt).cancel_unfilled_entry(idea) == ENTRY_CANCELLED_BEFORE_FILL
    assert open_row(rt)["status"] == "WORKING"  # nothing settled it in this process
    rt.restart()
    rt.run(1)
    row = open_row(rt)
    assert row["status"] == "CANCELLED" and ENTRY_CANCELLED_BEFORE_FILL in row["reasons"], row
    rt.service.close()


def test_an_unconfirmed_cancel_never_reads_cancelled():
    rt = service(post("1", 0, RESTING), post("2", 20, CLOSE))
    rt.run(2)
    for _ in range(4):
        rt.broker.script_next_cancel("REJECT")
    rt.run(5)  # the close arrives; the entry's cancel is refused at the broker
    close = next(a for a in rt.actions() if a["action"] == "CLOSE")
    assert "ENTRY_CANCEL_UNCONFIRMED" in close["reasons"], close
    row = open_row(rt)
    assert row["status"] == "WORKING", row
    assert ENTRY_CANCELLED_BEFORE_FILL not in row["reasons"]
    rt.service.close()


def test_a_fill_that_beats_the_cancel_reads_as_the_fill():
    rt = service(post("1", 0, RESTING), post("2", 20, CLOSE))
    rt.run(2)
    rt.broker.script_next_cancel("FILL_FIRST")
    rt.run(5)
    ref = open_row(rt)["order_ref"]
    assert coord(rt).intent(ref).state == IntentState.FILLED
    row = open_row(rt)
    assert row["status"] == "EXECUTED", row
    assert ENTRY_CANCELLED_BEFORE_FILL not in row["reasons"]
    assert "BROKER_FILLED" in row["reasons"]
    rt.service.close()


# ---------------------------------------------------------------- 2. exit while entry unknown


def open_exc(h, kind=EXIT_WHILE_ENTRY_UNKNOWN):
    return [e for e in h.coord.store.exceptions(open_only=True) if e.kind == kind]


def _never_arrived(h):
    """The entry request never reached the broker and its acknowledgement was lost: within the
    settle period the lookup cannot tell, so the outcome stays unknown."""
    h.broker.script_next_submit("LOST_ACK_NOT_PLACED")
    h.post("p1", open_action(entry="50.02"))
    [i] = h.coord.store.intents()
    assert i.state == IntentState.UNRESOLVED
    return i


def _resting_but_unknown(h):
    """A resting entry whose outcome the store still holds as unknown when the exit is seen
    (the window before the lookup finds it). The flag is raised as the replay raises it."""
    h.broker.script_next_submit("REST")
    out = h.execute("p1", open_action(entry="50.00"))
    h.coord.store.set_state(out.order_ref, IntentState.UNRESOLVED, h.clock.now())
    assert h.coord.flag_exit_while_entry_unknown("p1:open", Action.CLOSE) == [out.order_ref]
    return out.order_ref


def test_an_exit_while_the_entry_is_unknown_is_flagged_and_sends_nothing(tmp_path):
    h = Harness(tmp_path)
    entry = _never_arrived(h)
    submits = h.broker.submit_calls
    orders = [(o.order_ref, o.status) for o in h.broker.all_orders()]
    res = h.post("p2", action("c", Action.CLOSE, idea="p1:open"))
    [e] = open_exc(h)
    assert e.idea_id == "p1:open" and e.order_ref == entry.order_ref
    assert h.broker.submit_calls == submits
    assert [(o.order_ref, o.status) for o in h.broker.all_orders()] == orders
    [o] = res.outcomes
    assert o.order_ref is None  # the exit itself is decided exactly as before: nothing sent
    h.post("p3", action("r", Action.REDUCE, idea="p1:open", fraction="0.5"))
    assert len(open_exc(h)) == 1  # one open record per entry, not one per exit
    assert h.broker.submit_calls == submits
    h.coord.close()


def test_the_flag_stays_while_the_entry_works_and_closes_once_it_ends_flat(tmp_path):
    h = Harness(tmp_path)
    ref = _resting_but_unknown(h)
    h.coord.recover()  # the lookup finds the resting entry
    assert h.coord.intent(ref).state == IntentState.WORKING
    assert open_exc(h)  # known, but still working: it could still fill
    h.clock.advance(121)
    h.coord.sync()  # expired and cancelled (confirmed); the idea holds nothing
    assert h.coord.intent(ref).state == IntentState.CANCELLED
    assert open_exc(h) == []
    h.coord.close()


def test_the_flag_stays_while_the_entry_filled_and_is_held(tmp_path):
    h = Harness(tmp_path)
    ref = _resting_but_unknown(h)
    h.broker.fill(ref, price="50.00")  # the resting buy fills
    h.coord.recover()
    h.coord.sync()
    assert h.coord.intent(ref).state == IntentState.FILLED
    [pos] = [p for p in h.coord.ledger() if p.quantity]
    assert open_exc(h)  # known now, but the idea holds a position the publisher has left
    assert h.broker.submit_calls == 1  # no exit was invented for it
    h.coord.close()


def test_the_operator_settlement_ends_the_flag_once_flat(tmp_path):
    from tradingbot.execution.settlement import settle_intent
    h = Harness(tmp_path)
    entry = _never_arrived(h)
    h.post("p2", action("c", Action.CLOSE, idea="p1:open"))
    assert open_exc(h)
    h.clock.advance(11)
    h.coord.recover()
    res = settle_intent(
        h.coord, request_id="invented-req-2", order_ref=entry.order_ref, outcome="NOT_AT_BROKER",
        confirmation=cat.settle_phrase(entry.order_ref, "NOT_AT_BROKER"),
        note="checked the invented account by hand", operator="tester")
    assert res.status == "APPLIED", res
    h.coord.sync()
    assert open_exc(h) == []
    h.coord.close()


def test_the_operator_can_acknowledge_the_flag(tmp_path):
    h = Harness(tmp_path)
    _never_arrived(h)
    h.post("p2", action("c", Action.CLOSE, idea="p1:open"))
    [e] = open_exc(h)
    assert cat.action_for(EXIT_WHILE_ENTRY_UNKNOWN)[0] == cat.ACKNOWLEDGE
    res = acknowledge_exception(
        h.coord, request_id="invented-req-1", exc_id=e.id, kind=e.kind,
        confirmation=cat.acknowledge_phrase(e.id, e.kind),
        note="checked the invented account by hand", operator="tester")
    assert res.status == "APPLIED", res
    assert open_exc(h) == []
    h.coord.close()


def test_the_flag_raises_an_alert_through_the_service(tmp_path):
    from runtime_harness import ENTRY
    rt = Runtime(tmp_path, [post("1", 0, ENTRY), post("2", 3, CLOSE)])
    rt.broker.script_next_submit("LOST_ACK_NOT_PLACED")
    rt.run(2)
    c = coord(rt)
    [e] = [x for x in c.store.exceptions(open_only=True) if x.kind == EXIT_WHILE_ENTRY_UNKNOWN]
    assert N.EXIT_WHILE_ENTRY_UNKNOWN in rt.notifier.kinds()
    assert any(a["key"] == f"EXC:{e.id}" and a["kind"] == N.EXIT_WHILE_ENTRY_UNKNOWN
               for a in rt.store.alerts(open_only=True))
    rt.service.close()
