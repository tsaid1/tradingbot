"""Read-only view of the service's journal: the signal-to-execution lifecycle with stage latencies.

Used by the Execution dashboard page and the mock demo. Every database is opened read-only
(``mode=ro``); a missing file reads as empty. Nothing here writes, decides or calls a broker: the
broker side comes from the snapshot the service journals each cycle.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from tradingbot.runtime.config import RuntimeConfig

STAGES = (
    "published",
    "received",
    "interpreted",
    "gate_decided",
    "submitted",
    "acknowledged",
    "filled",
)


@contextmanager
def _ro(path: Path):
    if not path.exists():
        yield None
        return
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def _rows(conn, sql: str, args=()) -> list[dict]:
    if conn is None:
        return []
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    except sqlite3.OperationalError:  # table not created yet
        return []


def _kv(conn, key: str):
    rows = _rows(conn, "SELECT value FROM kv WHERE key=?", (key,))
    return json.loads(rows[0]["value"]) if rows else None


def _t(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _ms(a: datetime | None, b: datetime | None) -> float | None:
    if a is None or b is None:
        return None
    return round((b - a).total_seconds() * 1000, 1)


def load(config: RuntimeConfig) -> dict:
    """Everything the Execution page shows, as plain data."""
    with _ro(config.runtime_db) as rt, _ro(config.execution_db) as ex:
        signals = _rows(rt, "SELECT * FROM signals ORDER BY seq")
        actions = _rows(rt, "SELECT * FROM signal_actions ORDER BY seq, rowid")
        runtime_exceptions = _rows(rt, "SELECT * FROM runtime_exceptions ORDER BY id")
        alerts = _rows(rt, "SELECT * FROM alerts ORDER BY opened_at, key")
        events = _rows(rt, "SELECT * FROM service_events ORDER BY seq DESC LIMIT 50")
        heartbeat = _kv(rt, "heartbeat")
        snapshot = _kv(rt, "broker_snapshot") or {}
        reconciliation = _kv(rt, "reconciliation")
        intents = {r["order_ref"]: r for r in _rows(ex, "SELECT * FROM exec_intents")}
        orders = _rows(ex, "SELECT * FROM exec_orders ORDER BY rowid")
        fills = _rows(ex, "SELECT * FROM exec_fills ORDER BY executed_at, rowid")
        ledger = _rows(ex, "SELECT * FROM exec_positions ORDER BY idea_id")
        journal = _rows(
            ex, "SELECT seq, at, order_ref, idea_id, event FROM exec_journal ORDER BY seq"
        )
        exec_exceptions = _rows(ex, "SELECT * FROM exec_exceptions ORDER BY id")

    first: dict[tuple[str, str], datetime] = {}
    amends: dict[tuple[str, str], list[datetime]] = {}  # (idea, AMEND_SUBMIT | AMEND) -> times
    for j in journal:
        at = _t(j["at"])
        if j["order_ref"]:
            first.setdefault((j["order_ref"], j["event"]), at)
        if j["event"] in ("AMEND_SUBMIT", "AMEND") and j["idea_id"]:
            amends.setdefault((j["idea_id"], j["event"]), []).append(at)
    orders_by_intent: dict[str, list[dict]] = {}
    for o in orders:
        orders_by_intent.setdefault(o["intent_ref"], []).append(o)
    fills_by_ref: dict[str, list[dict]] = {}
    for f in fills:
        fills_by_ref.setdefault(f["order_ref"], []).append(f)
    working = {o["order_ref"]: o for o in snapshot.get("open_orders", [])}

    by_seq: dict[int, list[dict]] = {}
    for a in actions:
        a["reasons"] = json.loads(a["reasons"]) if isinstance(a["reasons"], str) else a["reasons"]
        by_seq.setdefault(a["seq"], []).append(a)

    lifecycle = []
    for s in signals:
        for a in by_seq.get(s["seq"], []):
            ref = a.get("order_ref")
            intent = intents.get(ref) if ref else None
            stamps = {
                "published": _t(s["published_at"]),
                "received": _t(s["received_at"]),
                "interpreted": _t(s["interpreted_at"]),
                "gate_decided": _t(a["gate_decided_at"]),
                "submitted": None,
                "acknowledged": None,
                "filled": None,
            }
            if intent is not None and not s["duplicate"]:
                if intent["action"] == "MODIFY":
                    # Submitted: the amendment request; acknowledged: the broker's answer to it.
                    gate = stamps["gate_decided"] or _t(intent["created_at"])
                    for stage, event in (("submitted", "AMEND_SUBMIT"), ("acknowledged", "AMEND")):
                        later = [t for t in amends.get((intent["idea_id"], event), []) if t >= gate]
                        stamps[stage] = later[0] if later else None
                else:
                    stamps["submitted"] = first.get((ref, "SUBMIT"))
                    stamps["acknowledged"] = first.get((ref, "ACK")) or first.get(
                        (ref, "ADOPTED_FROM_BROKER")
                    )
                    stamps["filled"] = first.get((ref, "FILL"))
            children = [
                {
                    "order_ref": o["order_ref"],
                    "role": o["role"],
                    "order_type": o["order_type"],
                    "quantity": o["quantity"],
                    "stop_price": o["stop_price"],
                    "limit_price": o["limit_price"],
                    "status": (working.get(o["order_ref"]) or {}).get("status", o["status"]),
                    "working": o["order_ref"] in working,
                }
                for o in orders_by_intent.get(ref, [])
                if o["role"] in ("STOP", "TARGET")
            ]
            order_fills = fills_by_ref.get(ref, []) if ref else []
            # An order placed by an earlier process (adopted after a restart) predates this
            # delivery's decision: its submit/ack stamps are the original ones, shown as such.
            recovered = bool(
                stamps["submitted"]
                and stamps["gate_decided"]
                and stamps["submitted"] < stamps["gate_decided"]
            )
            end = stamps["filled"] or stamps["acknowledged"] or stamps["gate_decided"]
            lifecycle.append(
                {
                    "seq": s["seq"],
                    "source_id": s["source_id"],
                    "post_id": s["post_id"],
                    "revision": s["revision"],
                    "duplicate": bool(s["duplicate"]),
                    "recovered": recovered,
                    "provenance": s["provenance"],
                    "action_id": a["action_id"],
                    "action": a["action"],
                    "timing": a["timing"],
                    "asset": a["asset"],
                    "mapping": a["mapping_status"],
                    "mapping_reason": a["mapping_reason"],
                    "broker_symbol": a["broker_symbol"],
                    "status": a["status"],
                    "reasons": a["reasons"],
                    "order_ref": ref,
                    "intent_state": intent["state"] if intent else None,
                    "order": (
                        {
                            "side": intent["side"],
                            "quantity": intent["quantity"],
                            "order_type": intent["order_type"],
                            "limit_price": intent["limit_price"],
                            "attempts": intent["attempts"],
                        }
                        if intent
                        else None
                    ),
                    "fills": [
                        {"exec_id": f["exec_id"], "quantity": f["quantity"], "price": f["price"]}
                        for f in order_fills
                    ],
                    "protective": children,
                    "stamps": {k: (v.isoformat() if v else None) for k, v in stamps.items()},
                    "latency_ms": {
                        "publish_to_receive": _ms(stamps["published"], stamps["received"]),
                        "receive_to_interpret": _ms(stamps["received"], stamps["interpreted"]),
                        "interpret_to_gate": _ms(stamps["interpreted"], stamps["gate_decided"]),
                        "gate_to_submit": None
                        if recovered
                        else _ms(stamps["gate_decided"], stamps["submitted"]),
                        "submit_to_ack": _ms(stamps["submitted"], stamps["acknowledged"]),
                        "ack_to_fill": _ms(stamps["acknowledged"], stamps["filled"]),
                        "end_to_end": None if s["duplicate"] else _ms(stamps["published"], end),
                    },
                }
            )

    broker_positions = {
        p["contract"]["symbol"]: p["quantity"] for p in snapshot.get("positions", [])
    }
    follower_net: dict[str, int] = {}
    for p in ledger:
        if p["quantity"] > 0:
            sign = 1 if p["position_side"] == "LONG" else -1
            follower_net[p["symbol"]] = follower_net.get(p["symbol"], 0) + sign * p["quantity"]
    positions = [
        {
            "symbol": sym,
            "follower": follower_net.get(sym, 0),
            "broker": broker_positions.get(sym, 0),
            "match": follower_net.get(sym, 0) == broker_positions.get(sym, 0),
        }
        for sym in sorted(set(follower_net) | set(broker_positions))
    ]
    role_by_ref = {o["order_ref"]: o for o in orders}
    working_orders = [
        {
            "order_ref": o["order_ref"],
            "role": (role_by_ref.get(o["order_ref"]) or {}).get("role", "UNKNOWN_TO_LEDGER"),
            "idea_id": (role_by_ref.get(o["order_ref"]) or {}).get("idea_id"),
            "symbol": o["contract"]["symbol"],
            "side": o["side"],
            "order_type": o["order_type"],
            "quantity": o["quantity"],
            "filled": o["filled_quantity"],
            "stop_price": o.get("stop_price"),
            "limit_price": o.get("limit_price"),
            "status": o["status"],
        }
        for o in snapshot.get("open_orders", [])
    ]
    open_unprotected = {
        e["idea_id"]
        for e in exec_exceptions
        if e["kind"] == "UNPROTECTED_POSITION" and e["resolved_at"] is None
    }
    protection = []
    for p in ledger:
        if p["quantity"] <= 0:
            continue
        stop = working.get(p["stop_ref"]) if p["stop_ref"] else None
        covered = (stop["quantity"] - stop["filled_quantity"]) if stop else 0
        confirmed = (
            p["stop_price"] is not None
            and stop is not None
            and stop["order_type"] == "STP"
            and covered == p["quantity"]
            and p["idea_id"] not in open_unprotected
        )
        protection.append(
            {
                "idea_id": p["idea_id"],
                "symbol": p["symbol"],
                "side": p["position_side"],
                "quantity": p["quantity"],
                "avg_price": p["avg_price"],
                "stop_price": p["stop_price"],
                "stop_ref": p["stop_ref"],
                "stop_covers": covered,
                "protection": "CONFIRMED"
                if confirmed
                else ("NOT_REQUIRED" if p["stop_price"] is None else "UNPROTECTED"),
            }
        )
    for e in runtime_exceptions:
        e["reasons"] = json.loads(e["reasons"]) if isinstance(e["reasons"], str) else e["reasons"]
    return {
        "heartbeat": heartbeat,
        "halted": config.paths.halt_file.exists(),
        "halt_file": str(config.paths.halt_file),
        "lifecycle": lifecycle,
        "positions": positions,
        "protection": protection,
        "working_orders": working_orders,
        "snapshot_at": snapshot.get("at"),
        "runtime_exceptions": runtime_exceptions,
        "execution_exceptions": exec_exceptions,
        "alerts": alerts,
        "reconciliation": reconciliation,
        "events": events,
    }


def latency_table(lifecycle: list[dict]) -> list[dict]:
    """One row per executed or attempted order (duplicates and never-ordered actions excluded)."""
    out = []
    for row in lifecycle:
        if row["duplicate"] or not row["order_ref"] or row["intent_state"] in (None, "REFUSED"):
            continue
        out.append(
            {
                "source_id": row["source_id"],
                "action": row["action"],
                "status": row["status"],
                "recovered": row["recovered"],
                **row["latency_ms"],
            }
        )
    return out
