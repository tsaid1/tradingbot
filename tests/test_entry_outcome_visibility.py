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

import pytest
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
    assert [p.quantity for p in h.coord.ledger()] == [4]
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


# ---------------------------------------------------------------- review fixes (PR #74)


def _add_unknown_then_close(tmp_path, monkeypatch, *, fault: bool):
    """An OPEN of 2 that fills, an ADD whose acknowledgement is lost, then the publisher's CLOSE.
    With ``fault`` only the flag's exception write fails."""
    from runtime_harness import ADD, ENTRY, SERVICE_MAPPING

    from tradingbot.execution import store as xs

    if fault:
        orig = xs.ExecutionStore.raise_exception

        def boom(self, at, kind, *a, **k):
            if kind == EXIT_WHILE_ENTRY_UNKNOWN:
                raise RuntimeError("invented: flag write failed")
            return orig(self, at, kind, *a, **k)

        monkeypatch.setattr(xs.ExecutionStore, "raise_exception", boom)
    tmp_path.mkdir(parents=True)
    rt = Runtime(tmp_path, [post("1", 0, ENTRY), post("2", 10, ADD), post("3", 13, CLOSE)],
                 mapping_file=SERVICE_MAPPING)
    rt.run(2)
    rt.broker.script_next_submit("LOST_ACK_NOT_PLACED")
    rt.run(8)
    broker = (
        [(o.order_ref, o.side.value, o.quantity, o.order_type.value, str(o.limit_price),
          str(o.stop_price), o.status.value, o.filled_quantity) for o in rt.broker.all_orders()],
        [(p.contract.contract_key, p.quantity) for p in rt.broker.positions()],
        rt.broker.submit_calls,
    )
    rows = [(a["action"], a["status"]) for a in rt.actions()]
    journal = [e.event for e in coord(rt).store.journal()]
    events = [e["event"] for e in rt.store.events()]
    rt.service.close()
    return broker, rows, journal, events


def test_a_failure_in_the_flag_path_never_alters_the_exit(tmp_path, monkeypatch):
    clean = _add_unknown_then_close(tmp_path / "a", monkeypatch, fault=False)
    faulty = _add_unknown_then_close(tmp_path / "b", monkeypatch, fault=True)
    assert EXIT_WHILE_ENTRY_UNKNOWN in clean[2]
    assert faulty[0] == clean[0]  # every broker-facing order, fill and position is identical
    assert faulty[1] == clean[1]
    assert ("CLOSE", "EXECUTED") in faulty[1]
    assert "EXIT_FLAG_FAILED" in faulty[2] and "EXIT_FLAG_FAILED" not in clean[2]
    assert "POST_PROCESSING_FAILED" not in faulty[3]


def _settle_cancelled_by_operator(rt, ref):
    """The entry's own order becomes invisible, so its intent turns UNRESOLVED; the operator
    then settles it CANCELLED."""
    from tradingbot.execution.settlement import settle_intent
    c = coord(rt)
    rt.broker.open_orders_lag_seconds = 10_000
    rt.run(4)
    for _ in range(6):
        rt.clock.advance(5)
        c.recover()
    assert c.intent(ref).state in (IntentState.RECORDED, IntentState.UNRESOLVED)
    res = settle_intent(c, request_id="invented-q1", order_ref=ref, outcome="CANCELLED",
                        confirmation=cat.settle_phrase(ref, "CANCELLED"),
                        note="invented: looked it up by hand", operator="tester")
    assert res.status == "APPLIED", res


@pytest.mark.parametrize("restart", [False, True])
def test_a_row_settled_cancelled_follows_the_intent_when_the_broker_contradicts(restart):
    rt = service(post("1", 0, RESTING))
    rt.run(2)
    ref = open_row(rt)["order_ref"]
    _settle_cancelled_by_operator(rt, ref)
    rt.run(1)
    assert open_row(rt)["status"] == "CANCELLED"
    if restart:
        rt.restart()
    rt.broker.open_orders_lag_seconds = 0  # the broker shows the order after all
    rt.run(2)
    intent = coord(rt).intent(ref)
    assert intent.state == IntentState.WORKING, intent.state
    assert open_row(rt)["status"] == "WORKING", open_row(rt)  # no terminal claim it lost
    if restart:
        rt.restart()
    rt.broker.fill(ref, price="88.30")
    rt.run(3)
    assert coord(rt).intent(ref).state == IntentState.FILLED
    assert [p.quantity for p in coord(rt).ledger() if p.quantity] == [1]
    row = open_row(rt)
    assert row["status"] == "EXECUTED", row
    assert "BROKER_CANCELLED" not in row["reasons"] and "BROKER_FILLED" in row["reasons"]
    settled = [e["detail"] for e in rt.store.events() if e["event"] == "SIGNAL_ACTION_SETTLED"]
    assert [d["status"] for d in settled] == ["CANCELLED", "WORKING", "EXECUTED"], settled
    rt.service.close()


def test_a_half_filled_entry_that_expires_reads_its_fill_without_an_unfilled_reason():
    from runtime_harness import SERVICE_MAPPING
    rt = Runtime(Path(tempfile.mkdtemp()), [post("1", 0, RESTING)], mapping_file=SERVICE_MAPPING)
    rt.broker.script_next_submit("PARTIAL", quantity=1)
    rt.run(30)  # 150 s: the remainder expires
    ref = open_row(rt)["order_ref"]
    intent = coord(rt).intent(ref)
    assert intent.state == IntentState.CANCELLED and ENTRY_EXPIRED_UNFILLED in intent.reasons
    assert coord(rt).store.filled_quantity(ref) == 1
    row = open_row(rt)
    assert row["status"] == "EXECUTED", row
    assert not any("UNFILLED" in r for r in row["reasons"]), row["reasons"]
    assert "ENTRY_REMAINDER_CANCELLED" in row["reasons"]
    [d] = [e["detail"] for e in rt.store.events() if e["event"] == "SIGNAL_ACTION_SETTLED"]
    assert d["filled"] == 1 and d["quantity"] == 2
    assert ENTRY_EXPIRED_UNFILLED in d["remainder_reasons"]
    rt.service.close()


def test_the_row_change_and_its_event_are_one_transaction(tmp_path):
    from tradingbot.runtime.store import RuntimeStore
    store = RuntimeStore(tmp_path / "rt.sqlite3")
    with store.tx() as c:
        c.execute("INSERT INTO signals (source_name,source_id,post_id,revision,post_hash,"
                  "provenance,verified_author,text_complete,text_len,media_count,links,"
                  "published_at,received_at,interpreted_at,decided_at,decision_now,duplicate,"
                  "hydrated) VALUES ('s','x','x',1,'h','SYNTHETIC',1,1,1,0,'[]','t','t','t','t',"
                  "'t',0,0)")
        c.execute("INSERT INTO signal_actions (seq,action_id,action,status,reasons,order_ref) "
                  "VALUES (1,'a','OPEN','WORKING','[]','TB-INVENTED')")
        c.execute("DROP TABLE service_events")  # the event write will fail
    import sqlite3
    from datetime import UTC, datetime
    with pytest.raises(sqlite3.OperationalError):
        store.settle_signal_action(1, "a", expect="WORKING", status="CANCELLED",
                                   reasons=["BROKER_CANCELLED"], settled_state="CANCELLED",
                                   settled_reasons=["BROKER_CANCELLED"],
                                   at=datetime(2026, 6, 1, tzinfo=UTC), detail={})
    assert store.signal_actions()[0]["status"] == "WORKING"  # rolled back with the event
