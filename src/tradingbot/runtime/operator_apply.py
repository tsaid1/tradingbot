"""Apply queued operator requests (``runtime.operator_actions``) in the process that holds the
execution store: the running service each cycle, or ``scripts/operator.py apply`` while it is
stopped. Every request ends with exactly one appended result; nothing here sends an order.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from tradingbot.execution import settlement
from tradingbot.execution.broker import BrokerError, session_identity
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
) -> list[dict]:
    """Apply every pending request in order. While the broker session is locked out (another
    account identity) only CLEAR_IDENTITY_LOCKOUT is applied; the rest wait. Returns one summary
    per request applied or refused (identifiers and reason codes only)."""
    out = []
    for req in queue.pending():
        if locked_out and req["action"] != Q.CLEAR_IDENTITY_LOCKOUT:
            continue
        result = _apply_one(coordinator, runtime_store, req, expected_account, clock)
        queue.record_result(req["id"], clock(), status=result.status, reasons=result.reasons,
                            before=result.before, after=result.after)
        runtime_store.event(
            clock(), "OPERATOR_ACTION", request_id=req["id"], action=req["action"],
            target=req["target"], outcome=req["outcome"], status=result.status,
            reasons=result.reasons, operator=req["operator"], origin=req["origin"],
        )
        out.append({"request_id": req["id"], "action": req["action"], "target": req["target"],
                    "outcome": req["outcome"], "status": result.status, "reasons": result.reasons,
                    "lifted": result.lifted})
    return out


def _apply_one(coord, store: RuntimeStore, req: dict, expected_account: str, clock):
    common = {"request_id": req["id"], "confirmation": req["confirmation"], "note": req["note"],
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
        return _clear_lockout(coord, store, req, expected_account, clock)
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
    store.event(clock(), "OPERATOR_ACKNOWLEDGED_SIGNAL", request_id=req["id"],
                exception_id=exc_id, kind=found["kind"], before=before,
                after={"open": False, "kind": found["kind"]})
    return settlement.OperatorResult(status="APPLIED", before=before,
                                     after={"open": False, "kind": found["kind"]})


def _clear_lockout(coord, store: RuntimeStore, req: dict, expected_account: str, clock):
    session = session_state(store)
    before = {"session": session.get("state"), "reason": session.get("reason")}
    reasons = []
    if req["target"] != expected_account:
        reasons.append("TARGET_IS_NOT_THE_EXPECTED_ACCOUNT")
    if req["confirmation"].strip() != lockout_phrase(req["target"]):
        reasons.append("CONFIRMATION_MISMATCH")
    if session.get("state") != LOCKED_OUT:
        reasons.append("NOT_LOCKED_OUT")
    identity = None
    if not reasons:
        try:
            identity = session_identity(coord.broker)
        except BrokerError:
            reasons.append("BROKER_UNREADABLE")
    if identity is not None and identity.get("account") != expected_account:
        reasons.append("BROKER_STILL_SHOWS_ANOTHER_ACCOUNT")
    if reasons:
        return settlement.OperatorResult(status="REFUSED", reasons=reasons, before=before,
                                         after=before)
    now = clock()
    # Back to DISCONNECTED: the next cycle runs the full resync before any exposure is allowed.
    store.put(SESSION_KEY, {"state": DISCONNECTED, "since": now.isoformat(),
                            "journal_mark": session.get("journal_mark", 0),
                            "reason": "LOCKOUT_CLEARED"}, now)
    store.put("broker_identity", identity, now)
    store.event(now, "IDENTITY_LOCKOUT_CLEARED", request_id=req["id"], before=before,
                identity=identity)
    return settlement.OperatorResult(status="APPLIED", before=before,
                                     after={"session": DISCONNECTED, "reason": "LOCKOUT_CLEARED"})
