"""The operator request queue: what a human asked for (CLI or dashboard) and what came of it.

``<state_dir>/operator.sqlite3`` holds two append-only tables (SQLite triggers refuse every UPDATE
and DELETE): ``operator_requests`` (who, when, which action on which target, the declared outcome,
the note and the typed confirmation) and ``operator_results`` (applied or refused, the reasons and
the state before and after). A request is checked here for form and confirmation and applied later
by the process that holds the execution store (the running service, or ``scripts/operator.py
apply`` while the service is stopped), which checks it again against the broker.

No action in this queue sends, amends or cancels an order. Each one only records what a human has
established at the broker, or closes a record that blocks nothing, or lifts a lockout.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from tradingbot.execution import operator_catalog as cat

SETTLE = "SETTLE"
CLEAR = "CLEAR"
ACKNOWLEDGE = "ACKNOWLEDGE"
ACKNOWLEDGE_SIGNAL = "ACKNOWLEDGE_SIGNAL"
CLEAR_IDENTITY_LOCKOUT = "CLEAR_IDENTITY_LOCKOUT"
ACTIONS = (SETTLE, CLEAR, ACKNOWLEDGE, ACKNOWLEDGE_SIGNAL, CLEAR_IDENTITY_LOCKOUT)
MIN_NOTE_CHARS = 8

SCHEMA = """
CREATE TABLE IF NOT EXISTS operator_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    operator TEXT NOT NULL,
    origin TEXT NOT NULL,
    action TEXT NOT NULL,
    target TEXT NOT NULL,
    kind TEXT,
    outcome TEXT,
    quantity INTEGER,
    avg_price TEXT,
    note TEXT NOT NULL,
    confirmation TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS operator_results (
    request_id INTEGER PRIMARY KEY REFERENCES operator_requests(id),
    at TEXT NOT NULL,
    status TEXT NOT NULL,
    reasons TEXT NOT NULL,
    before TEXT NOT NULL,
    after TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS operator_requests_append_only_u BEFORE UPDATE ON operator_requests
BEGIN SELECT RAISE(ABORT, 'operator_requests is append-only'); END;
CREATE TRIGGER IF NOT EXISTS operator_requests_append_only_d BEFORE DELETE ON operator_requests
BEGIN SELECT RAISE(ABORT, 'operator_requests is append-only'); END;
CREATE TRIGGER IF NOT EXISTS operator_results_append_only_u BEFORE UPDATE ON operator_results
BEGIN SELECT RAISE(ABORT, 'operator_results is append-only'); END;
CREATE TRIGGER IF NOT EXISTS operator_results_append_only_d BEFORE DELETE ON operator_results
BEGIN SELECT RAISE(ABORT, 'operator_results is append-only'); END;
"""


class OperatorInputError(ValueError):
    """The request is malformed or its typed confirmation does not match; nothing was queued."""


def expected_confirmation(action: str, target: str, *, kind: str | None = None,
                          outcome: str | None = None) -> str:
    """The exact words the operator must type for this action (they name target and outcome)."""
    if action == SETTLE:
        return cat.settle_phrase(target, outcome or "?")
    if action == CLEAR:
        return cat.clear_phrase(int(target), kind or "?")
    if action == ACKNOWLEDGE:
        return cat.acknowledge_phrase(int(target), kind or "?")
    if action == ACKNOWLEDGE_SIGNAL:
        return cat.acknowledge_signal_phrase(int(target), kind or "?")
    if action == CLEAR_IDENTITY_LOCKOUT:
        return cat.lockout_phrase(target)
    raise OperatorInputError(f"unknown operator action {action!r}")


class OperatorQueue:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._tx() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA busy_timeout=10000")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def submit(
        self,
        *,
        at: datetime,
        operator: str,
        origin: str,
        action: str,
        target: str,
        note: str,
        confirmation: str,
        kind: str | None = None,
        outcome: str | None = None,
        quantity: int | None = None,
        avg_price: str | None = None,
    ) -> int:
        """Queue one request after checking its form and typed confirmation. Returns its id."""
        problems = []
        if action not in ACTIONS:
            raise OperatorInputError(f"unknown operator action {action!r}")
        if not operator.strip():
            problems.append("operator name required")
        if len(note.strip()) < MIN_NOTE_CHARS:
            problems.append(f"a note of at least {MIN_NOTE_CHARS} characters is required")
        if action in (CLEAR, ACKNOWLEDGE, ACKNOWLEDGE_SIGNAL) and not str(target).isdigit():
            problems.append("target must be the exception id")
        if action == SETTLE:
            if outcome not in cat.SETTLEMENT_OUTCOMES:
                problems.append(f"outcome must be one of {', '.join(cat.SETTLEMENT_OUTCOMES)}")
            if outcome == "FILLED" and (quantity is None or quantity <= 0 or not avg_price):
                problems.append("FILLED needs the quantity and average price seen at the broker")
            if outcome != "FILLED" and (quantity is not None or avg_price):
                problems.append("quantity and price are given only with FILLED")
        if not problems:
            want = expected_confirmation(action, str(target), kind=kind, outcome=outcome)
            if confirmation.strip() != want:
                problems.append(f"type exactly: {want}")
        if problems:
            raise OperatorInputError("; ".join(problems))
        with self._tx() as c:
            cur = c.execute(
                """INSERT INTO operator_requests (at,operator,origin,action,target,kind,outcome,
                quantity,avg_price,note,confirmation) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (at.isoformat(), operator.strip(), origin, action, str(target), kind, outcome,
                 quantity, avg_price, note.strip(), confirmation.strip()),
            )
            return int(cur.lastrowid)

    def record_result(self, request_id: int, at: datetime, *, status: str, reasons: list[str],
                      before: dict, after: dict) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT OR IGNORE INTO operator_results (request_id,at,status,reasons,before,after)"
                " VALUES (?,?,?,?,?,?)",
                (request_id, at.isoformat(), status, json.dumps(reasons),
                 json.dumps(before, sort_keys=True, default=str),
                 json.dumps(after, sort_keys=True, default=str)),
            )

    def requests(self) -> list[dict]:
        with self._tx() as c:
            rows = c.execute(
                """SELECT q.*, r.at AS result_at, r.status, r.reasons, r.before, r.after
                FROM operator_requests q LEFT JOIN operator_results r ON r.request_id = q.id
                ORDER BY q.id"""
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            for k in ("reasons", "before", "after"):
                d[k] = json.loads(d[k]) if d[k] else ([] if k == "reasons" else {})
            out.append(d)
        return out

    def pending(self) -> list[dict]:
        return [r for r in self.requests() if r["status"] is None]


def queue_path(state_dir: Path) -> Path:
    return Path(state_dir) / "operator.sqlite3"
