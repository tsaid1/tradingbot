"""Confirmation review notes N2-N4 on the operator controls. Invented data, mock broker only."""

from __future__ import annotations

from runtime_harness import Runtime

from tradingbot.execution import operator_catalog as cat
from tradingbot.runtime import operator_actions as Q
from tradingbot.runtime.operator_apply import session_state

NOTE = "checked the invented account by hand"
ACCOUNT = "SIM-PAPER-0001"


def locked_out(rt) -> None:
    """The session came back under another account: the service locks out and halts."""
    rt.step()
    rt.broker.disconnect()
    rt.broker.next_session_account = "SIM-PAPER-9999"
    rt.step()
    assert session_state(rt.store)["state"] == "LOCKED_OUT"
    rt.broker.account = ACCOUNT  # the expected account is connected again


def no_reconnect(rt) -> None:
    """An adapter without session control (no reconnect)."""
    rt.broker.reconnect = None


def clear_lockout(rt) -> dict:
    queue = Q.OperatorQueue(rt.config.operator_db)
    queue.enqueue(at=rt.clock.now(), operator="tester", origin="test",
                  action=Q.CLEAR_IDENTITY_LOCKOUT, target=ACCOUNT, note=NOTE,
                  confirmation=cat.lockout_phrase(ACCOUNT))
    rt.step()
    return queue.requests()[-1]


def cleared_event(rt) -> dict:
    [e] = [e for e in rt.store.events() if e["event"] == "IDENTITY_LOCKOUT_CLEARED"]
    return e["detail"]


# ------------------------------------------------------------------------------------------ N2


def test_n2_a_lockout_cleared_through_a_reconnect_journals_full_reconnect(tmp_path):
    rt = Runtime(tmp_path, [])
    locked_out(rt)
    assert clear_lockout(rt)["status"] == "APPLIED"
    assert cleared_event(rt)["mode"] == "FULL_RECONNECT"


def test_n2_strict_by_default_an_adapter_without_reconnect_stays_locked_out(tmp_path):
    rt = Runtime(tmp_path, [])
    locked_out(rt)
    no_reconnect(rt)
    req = clear_lockout(rt)
    assert req["status"] == "REFUSED" and req["reasons"] == ["FULL_RECONNECT_UNAVAILABLE"]
    assert session_state(rt.store)["state"] == "LOCKED_OUT"
    # After a restart the session was opened by the new process, after the lockout began.
    rt.clock.advance(60)
    rt.restart()
    no_reconnect(rt)
    assert clear_lockout(rt)["status"] == "APPLIED"
    detail = cleared_event(rt)
    assert detail["mode"] == "FULL_RECONNECT"
    assert detail["how"] == "SESSION_OPENED_BY_THIS_PROCESS_AFTER_THE_LOCKOUT"


def test_n2_when_allowed_an_identity_read_alone_clears_and_says_so(tmp_path):
    rt = Runtime(tmp_path, [], service={"lockout_clear_requires_full_reconnect": False})
    locked_out(rt)
    no_reconnect(rt)
    assert clear_lockout(rt)["status"] == "APPLIED"
    detail = cleared_event(rt)
    assert detail["mode"] == "IDENTITY_REREAD_ONLY"
    assert "full reconnect" not in detail["rule"]


# ------------------------------------------------------------------------------------------ N3


def journaled_request(rt) -> str:
    """One operator request applied (here refused) and recorded in the execution journal."""
    queue = Q.OperatorQueue(rt.config.operator_db)
    queue.enqueue(at=rt.clock.now(), operator="tester", origin="test", action=Q.SETTLE,
                  target="TB-invented-unknown", outcome="NOT_AT_BROKER", note=NOTE,
                  confirmation=cat.settle_phrase("TB-invented-unknown", "NOT_AT_BROKER"))
    rt.step()
    [req] = queue.requests()
    assert req["status"] == "REFUSED"
    assert any(e.detail.get("request_id") == req["uid"]
               for e in rt.service.coordinator.store.journal())
    return req["uid"]


def swap_for_empty(rt) -> None:
    rt.config.operator_db.unlink()
    Q.OperatorQueue.initialise(rt.config.operator_db)  # an empty, perfectly valid queue


def queue_alert(rt) -> list[dict]:
    return [a for a in rt.store.alerts(open_only=True) if a["key"] == "OPERATOR:QUEUE"]


def test_n3_a_queue_swapped_for_an_empty_valid_one_is_treated_as_lost(tmp_path):
    from runtime_harness import CLOSE, ENTRY, post

    rt = Runtime(tmp_path, [post("1", 0, ENTRY), post("2", 60, CLOSE)])
    journaled_request(rt)
    swap_for_empty(rt)
    queue = Q.OperatorQueue(rt.config.operator_db)
    queue.enqueue(at=rt.clock.now(), operator="tester", origin="test", action=Q.SETTLE,
                  target="TB-invented-other", outcome="NOT_AT_BROKER", note=NOTE,
                  confirmation=cat.settle_phrase("TB-invented-other", "NOT_AT_BROKER"))
    rt.step()
    [alert] = queue_alert(rt)
    assert "QUEUE_LOST" in alert["detail"]
    assert rt.heartbeat()["operator_queue"]["state"] == "UNAVAILABLE"
    assert queue.pending()  # nothing from the swapped queue is applied
    # At start-up too, and the trading loop carries on meanwhile.
    rt.restart()
    rt.run(20, 5)
    assert queue_alert(rt) and queue.pending()
    assert rt.service.coordinator.store.position("1:a1").status == "CLOSED"


def test_n3_a_replacement_initialised_for_the_lost_requests_is_accepted(tmp_path):
    rt = Runtime(tmp_path, [])
    uid = journaled_request(rt)
    rt.config.operator_db.unlink()
    Q.OperatorQueue.initialise(rt.config.operator_db, replaces_lost=[uid])
    rt.step()
    assert not queue_alert(rt)
    assert rt.heartbeat()["operator_queue"]["state"] != "UNAVAILABLE"
