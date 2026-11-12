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
