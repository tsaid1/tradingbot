"""Apply queued operator requests (``runtime.operator_actions``) in the process that holds the
execution store: the running service each cycle, or ``scripts/tb_operator.py apply`` while it is
stopped. Every request ends with exactly one appended result; nothing here sends an order.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from tradingbot.execution import settlement
from tradingbot.execution.broker import BrokerError, BrokerIdentityError, session_identity
from tradingbot.execution.coordinator import ExecutionCoordinator
from tradingbot.execution.operator_catalog import acknowledge_signal_phrase, lockout_phrase
from tradingbot.runtime import operator_actions as Q
from tradingbot.runtime.store import RuntimeStore

SESSION_KEY = "broker_session"
CONNECTED = "CONNECTED"
DISCONNECTED = "DISCONNECTED"
LOCKED_OUT = "LOCKED_OUT"


def session_state(store: RuntimeStore) -> dict:
    return store.get(SESSION_KEY) or {"state": CONNECTED}


def apply_pending(
    *,
    coordinator: ExecutionCoordinator,
    runtime_store: RuntimeStore,
    queue: Q.OperatorQueue,
    expected_account: str,
    clock: Callable[[], datetime],
    locked_out: bool = False,
    require_full_reconnect: bool = True,
    session_opened_at: datetime | None = None,
) -> list[dict]:
    """Apply every pending request in order. While the broker session is locked out (another
    account identity) only CLEAR_IDENTITY_LOCKOUT is applied; the rest wait. Returns one summary
    per request applied or refused (identifiers and reason codes only).
    ``require_full_reconnect`` (review N2): a lockout clears only through a full reconnect;
    ``session_opened_at``: when this process opened its broker session (a session opened after
    the lockout began is itself a full reconnect)."""
    out = []
    for req in queue.pending():
        if locked_out and req["action"] != Q.CLEAR_IDENTITY_LOCKOUT:
            continue
        result = _apply_one(coordinator, runtime_store, req, expected_account, clock,
                            require_full_reconnect, session_opened_at)
        queue.record_result(req["id"], clock(), status=result.status, reasons=result.reasons,
                            before=result.before, after=result.after)
        runtime_store.event(
            clock(), "OPERATOR_ACTION", request_id=req["uid"], request=req["id"],
            action=req["action"],
            target=req["target"], outcome=req["outcome"], status=result.status,
            reasons=result.reasons, operator=req["operator"], origin=req["origin"],
        )
        out.append({"request_id": req["id"], "uid": req["uid"], "action": req["action"],
                    "target": req["target"],
                    "outcome": req["outcome"], "status": result.status, "reasons": result.reasons,
                    "lifted": result.lifted})
    return out


def _apply_one(coord, store: RuntimeStore, req: dict, expected_account: str, clock,
               require_full_reconnect: bool = True, session_opened_at: datetime | None = None):
    # The globally unique id: the journal matches results on it, its target and outcome (F3).
    common = {"request_id": req["uid"], "confirmation": req["confirmation"], "note": req["note"],
              "operator": req["operator"]}
    action = req["action"]
    if action == Q.SETTLE:
        return settlement.settle_intent(
            coord, order_ref=req["target"], outcome=req["outcome"] or "",
            quantity=req["quantity"], avg_price=req["avg_price"], **common,
        )
    if action == Q.CLEAR:
        return settlement.clear_exception(
            coord, exc_id=int(req["target"]), kind=req["kind"] or "", **common
        )
    if action == Q.ACKNOWLEDGE:
        return settlement.acknowledge_exception(
            coord, exc_id=int(req["target"]), kind=req["kind"] or "", **common
        )
    if action == Q.ACKNOWLEDGE_SIGNAL:
        return _acknowledge_signal(store, req, clock)
    if action == Q.CLEAR_IDENTITY_LOCKOUT:
        return _clear_lockout(coord, store, req, expected_account, clock,
                              require_full_reconnect, session_opened_at)
    return settlement.OperatorResult(status="REFUSED", reasons=["UNKNOWN_ACTION"])


def _acknowledge_signal(store: RuntimeStore, req: dict, clock) -> settlement.OperatorResult:
    exc_id = int(req["target"])
    found = next((e for e in store.exceptions() if e["id"] == exc_id), None)
    reasons = []
    if req["confirmation"].strip() != acknowledge_signal_phrase(exc_id, req["kind"] or ""):
        reasons.append("CONFIRMATION_MISMATCH")
    if found is None:
        reasons.append("EXCEPTION_UNKNOWN")
    else:
        if found["kind"] != req["kind"]:
            reasons.append(f"KIND_MISMATCH:{found['kind']}")
        if not found["kind"].startswith("SIGNAL_"):
            reasons.append("NOT_A_HELD_SIGNAL")
        if found["resolved_at"] is not None:
            reasons.append("EXCEPTION_ALREADY_RESOLVED")
    before = {"open": found is not None and found["resolved_at"] is None, "kind": req["kind"]}
    if reasons:
        return settlement.OperatorResult(status="REFUSED", reasons=reasons, before=before,
                                         after=before)
    store.resolve_exception(exc_id, clock(), f"acknowledged by operator request {req['id']}")
    store.event(clock(), "OPERATOR_ACKNOWLEDGED_SIGNAL", request_id=req["uid"], note=req["note"],
                exception_id=exc_id, kind=found["kind"], before=before,
                after={"open": False, "kind": found["kind"]})
    return settlement.OperatorResult(status="APPLIED", before=before,
                                     after={"open": False, "kind": found["kind"]})


def _clear_lockout(coord, store: RuntimeStore, req: dict, expected_account: str, clock,
                   require_full_reconnect: bool = True,
                   session_opened_at: datetime | None = None):
    """Review F2: clearing a lockout forces a full reconnect through the adapter, so every
    identity check it has runs afresh, and succeeds only with the expected account AND the session
    identity this service last accepted. Otherwise the lockout stays and the reason is recorded.
    It never clears the halt. Review N2: the journal states what was done, FULL_RECONNECT or
    IDENTITY_REREAD_ONLY (an adapter without reconnect whose session predates the lockout); the
    latter is refused (FULL_RECONNECT_UNAVAILABLE) unless ``require_full_reconnect`` is off."""
    session = session_state(store)
    before = {"session": session.get("state"), "reason": session.get("reason")}
    reasons = []
    if req["target"] != expected_account:
        reasons.append("TARGET_IS_NOT_THE_EXPECTED_ACCOUNT")
    if req["confirmation"].strip() != lockout_phrase(req["target"]):
        reasons.append("CONFIRMATION_MISMATCH")
    if session.get("state") != LOCKED_OUT:
        reasons.append("NOT_LOCKED_OUT")
    mode, how = _reconnect_mode(coord.broker, session, session_opened_at)
    if not reasons and mode == IDENTITY_REREAD_ONLY and require_full_reconnect:
        reasons.append("FULL_RECONNECT_UNAVAILABLE")
    identity = None
    if not reasons:
        try:
            _force_reconnect(coord.broker)
            identity = session_identity(coord.broker)
        except BrokerIdentityError:
            reasons.append("IDENTITY_CHECK_FAILED")
        except BrokerError:
            reasons.append("BROKER_UNREADABLE")
    accepted = store.get("broker_identity")
    if identity is not None:
        if identity.get("account") != expected_account:
            reasons.append("BROKER_STILL_SHOWS_ANOTHER_ACCOUNT")
        elif accepted is not None and identity != accepted:
            reasons.append("SESSION_IDENTITY_NOT_ACCEPTED")
    if reasons:
        if session.get("state") == LOCKED_OUT:
            store.event(clock(), "IDENTITY_LOCKOUT_CLEAR_REFUSED", request_id=req["uid"],
                        reasons=reasons, lockout_reason=session.get("reason"))
        return settlement.OperatorResult(status="REFUSED", reasons=reasons, before=before,
                                         after=before)
    now = clock()
    # Back to DISCONNECTED: the next cycle runs the full resync before any exposure is allowed.
    store.put(SESSION_KEY, {"state": DISCONNECTED, "since": now.isoformat(),
                            "journal_mark": session.get("journal_mark", 0),
                            "reason": "LOCKOUT_CLEARED"}, now)
    store.put("broker_identity", identity, now)
    store.event(now, "IDENTITY_LOCKOUT_CLEARED", request_id=req["uid"], note=req["note"],
                before=before,
                identity=identity, mode=mode, how=how,
                rule="full reconnect, identity checks passed afresh" if mode == FULL_RECONNECT
                else "identity read only (the adapter cannot reconnect); its other checks did "
                "not run again")
    return settlement.OperatorResult(status="APPLIED", before=before,
                                     after={"session": DISCONNECTED, "reason": "LOCKOUT_CLEARED"})


FULL_RECONNECT = "FULL_RECONNECT"
IDENTITY_REREAD_ONLY = "IDENTITY_REREAD_ONLY"


def _reconnect_mode(broker, session: dict, session_opened_at: datetime | None):
    """What clearing the lockout will actually do: (mode, how)."""
    if callable(getattr(broker, "reconnect", None)):
        return FULL_RECONNECT, "ADAPTER_RECONNECT"
    since = session.get("since")
    if session_opened_at is not None and since and session_opened_at > datetime.fromisoformat(
        since
    ):
        return FULL_RECONNECT, "SESSION_OPENED_BY_THIS_PROCESS_AFTER_THE_LOCKOUT"
    return IDENTITY_REREAD_ONLY, "IDENTITY_READ"


def _force_reconnect(broker) -> None:
    """Drop the session and reconnect through the adapter (its identity checks run again). An
    adapter without session control has no checks beyond the identity read that follows."""
    reconnect = getattr(broker, "reconnect", None)
    if not callable(reconnect):
        return
    disconnect = getattr(broker, "disconnect", None)
    if callable(disconnect):
        disconnect()
    reconnect()
