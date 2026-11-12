"""The exception register: every open item that needs a human, the action that resolves it, and
the resolved history. Read-only: every database is opened ``mode=ro`` (a missing file reads as
empty); nothing here writes, decides or calls a broker. Used by the dashboard's Exceptions page and
``scripts/operator.py list``.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

from tradingbot.execution import operator_catalog as cat
from tradingbot.runtime.config import RuntimeConfig
from tradingbot.runtime.lifecycle import _kv, _ro, _rows, contract_label
from tradingbot.runtime.notify import read_alerts_file

UNRESOLVED = ("RECORDED", "UNRESOLVED")


def _age(at: str | None, now: datetime) -> float | None:
    if not at:
        return None
    return round(max((now - datetime.fromisoformat(at)).total_seconds(), 0.0), 1)


def _label(raw: str | None, symbol: str | None) -> str:
    if raw:
        try:
            return contract_label(json.loads(raw))
        except ValueError:
            pass
    return symbol or "-"


def _phrase(cls: str, exc_id: int, kind: str) -> str | None:
    if cls == cat.CLEAR:
        return cat.clear_phrase(exc_id, kind)
    if cls == cat.ACKNOWLEDGE:
        return cat.acknowledge_phrase(exc_id, kind)
    return None


def load_register(config: RuntimeConfig, now: datetime | None = None) -> dict:
    now = now or datetime.now(UTC)
    with _ro(config.execution_db) as ex, _ro(config.runtime_db) as rt, \
            _ro(config.operator_db) as op:
        intents = _rows(ex, "SELECT * FROM exec_intents ORDER BY created_at, rowid")
        positions = {p["idea_id"]: p for p in _rows(ex, "SELECT * FROM exec_positions")}
        exceptions = _rows(ex, "SELECT * FROM exec_exceptions ORDER BY id")
        signals = _rows(rt, "SELECT * FROM runtime_exceptions ORDER BY id")
        alerts = _rows(rt, "SELECT * FROM alerts ORDER BY opened_at, key")
        session = _kv(rt, "broker_session") or {"state": "CONNECTED"}
        heartbeat = _kv(rt, "heartbeat")
        queue_problem = None if op is not None else "NOT_INITIALISED"
        try:
            requests = _rows(
                op,
                """SELECT q.id, q.at, q.operator, q.origin, q.action, q.target, q.kind,
                q.outcome, q.quantity, q.avg_price, r.at AS result_at, r.status, r.reasons
                FROM operator_requests q LEFT JOIN operator_results r ON r.request_id = q.id
                ORDER BY q.id""",
            )
        except sqlite3.DatabaseError:  # corrupt: shown, never repaired here (review F3)
            requests, queue_problem = [], "UNREADABLE"
    by_ref = {i["order_ref"]: i for i in intents}

    def contract_of(idea_id: str | None, order_ref: str | None) -> str:
        i = by_ref.get(order_ref or "")
        if i and i.get("contract"):
            return _label(i["contract"], i["symbol"])
        p = positions.get(idea_id or "")
        return _label(p.get("contract"), p.get("symbol")) if p else "-"

    open_items: list[dict] = []
    history: list[dict] = []
    for e in exceptions:
        cls, instruction = cat.action_for(e["kind"])
        row = {
            "source": "execution",
            "id": e["id"],
            "kind": e["kind"],
            "contract": contract_of(e["idea_id"], e["order_ref"]),
            "idea_id": e["idea_id"],
            "order_ref": e["order_ref"],
            "opened_at": e["at"],
            "age_seconds": _age(e["at"], now),
            "action": cls,
            "instruction": instruction,
            "confirmation": _phrase(cls, e["id"], e["kind"]),
            "detail": e["detail"],
            "resolved_at": e["resolved_at"],
        }
        (history if e["resolved_at"] else open_items).append(row)
    unresolved = []
    for i in intents:
        if i["state"] not in UNRESOLVED or i["action"] == "MODIFY":
            continue
        unresolved.append(
            {
                "order_ref": i["order_ref"],
                "action": i["action"],
                "idea_id": i["idea_id"],
                "contract": _label(i.get("contract"), i["symbol"]),
                "side": i["side"],
                "quantity": i["quantity"],
                "limit_price": i["limit_price"],
                "state": i["state"],
                "attempts": i["attempts"],
                "submitted_at": i["submitted_at"],
                "age_seconds": _age(i["submitted_at"] or i["created_at"], now),
                "instruction": cat.UNRESOLVED_INTENT_ACTION[1],
                "confirmation": {o: cat.settle_phrase(i["order_ref"], o)
                                 for o in cat.SETTLEMENT_OUTCOMES},
            }
        )
    for s in signals:
        s["reasons"] = json.loads(s["reasons"]) if isinstance(s["reasons"], str) else s["reasons"]
        row = {
            "source": "signal",
            "id": s["id"],
            "kind": s["kind"],
            "contract": "-",
            "idea_id": None,
            "order_ref": s["order_ref"],
            "opened_at": s["at"],
            "age_seconds": _age(s["at"], now),
            "action": cat.SIGNAL_ACTION[0],
            "instruction": cat.SIGNAL_ACTION[1],
            "confirmation": cat.acknowledge_signal_phrase(s["id"], s["kind"]),
            "detail": f"{s['source_id']} r{s['revision']} {s['action_id']}: "
                      + ", ".join(s["reasons"]),
            "resolved_at": s["resolved_at"],
        }
        (history if s["resolved_at"] else open_items).append(row)
    for r in requests:
        r["reasons"] = json.loads(r["reasons"]) if r.get("reasons") else []
    history.sort(key=lambda r: r["resolved_at"] or "", reverse=True)
    return {
        "now": now.isoformat(),
        "halted": config.paths.halt_file.exists(),
        "halt_file": str(config.paths.halt_file),
        "session": session,
        "heartbeat_at": (heartbeat or {}).get("at"),
        "heartbeat_age_seconds": _age((heartbeat or {}).get("at"), now),
        "unresolved_intents": unresolved,
        "open": open_items,
        "history": history,
        "alerts_open": [a for a in alerts if a["resolved_at"] is None],
        "alerts_resolved": [a for a in alerts if a["resolved_at"] is not None][-50:],
        "alerts_feed": read_alerts_file(config.alerts_file, 100),
        "requests": requests,
        "operator_queue_problem": queue_problem,
        "expected_account": config.execution.expected_account,
        "lockout_confirmation": cat.lockout_phrase(config.execution.expected_account),
    }
