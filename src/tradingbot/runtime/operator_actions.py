"""The operator request queue: what a human asked for (CLI or dashboard) and what came of it.

``<state_dir>/operator.sqlite3`` holds two append-only tables (SQLite triggers refuse every UPDATE
and DELETE): ``operator_requests`` (who, when, which action on which target, the declared outcome,
the note and the typed confirmation) and ``operator_results`` (applied or refused, the reasons and
the state before and after). A request is checked here for form and confirmation and applied later
by the process that holds the execution store (the running service, or ``scripts/tb_operator.py
apply`` while the service is stopped), which checks it again against the broker.

No action in this queue sends, amends or cancels an order. Each one only records what a human has
established at the broker, or closes a record that blocks nothing, or lifts a lockout.

Review F3: the queue is created only by an explicit initialisation step
(``OperatorQueue.initialise``, ``scripts/tb_operator.py init``); opening a missing, unreadable or
corrupt queue raises ``OperatorQueueUnavailable`` and never recreates it. Every request carries a
globally unique ``uid`` (the integer ``id`` is only a display counter and restarts with a new
queue); the execution journal records results under the ``uid``.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
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
    uid TEXT NOT NULL UNIQUE,
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


TABLES = ("operator_requests", "operator_results")
NOT_INITIALISED = "NOT_INITIALISED"
UNREADABLE = "UNREADABLE"


class OperatorQueueUnavailable(RuntimeError):
    """The queue is missing (never initialised, or lost), unreadable or corrupt. Operator actions
    are disabled; nothing is recreated implicitly. ``reason``: NOT_INITIALISED or UNREADABLE."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason


class OperatorInputError(ValueError):
    """The request is malformed or its typed confirmation does not match; nothing was queued."""


def expected_confirmation(action: str, target: str, *, kind: str | None = None,
                          outcome: str | None = None, quantity: int | None = None,
                          avg_price: str | None = None) -> str:
    """The exact words the operator must type for this action: they name the target and the
    outcome and, for FILLED, the quantity and price declared (review F5)."""
    if action == SETTLE:
        return cat.settle_phrase(target, outcome or "?", quantity, avg_price)
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
        """Open an existing, initialised queue. Raises OperatorQueueUnavailable otherwise."""
        self.path = Path(path)
        if not self.path.exists():
            raise OperatorQueueUnavailable(NOT_INITIALISED, str(self.path))
        self.check()

    @classmethod
    def initialise(cls, path: str | Path) -> OperatorQueue:
        """The explicit initialisation step: create the queue if it does not exist (an existing
        one is opened and checked, never replaced)."""
        path = Path(path)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(path), timeout=10.0)
            try:
                conn.executescript(SCHEMA)
                conn.commit()
            finally:
                conn.close()
        return cls(path)

    def check(self) -> None:
        """Raise OperatorQueueUnavailable(UNREADABLE) unless the file is a readable queue
        (NOT_INITIALISED when it is gone)."""
        if not self.path.exists():
            raise OperatorQueueUnavailable(NOT_INITIALISED, str(self.path))
        try:
            conn = sqlite3.connect(f"{self.path.resolve().as_uri()}?mode=ro", uri=True,
                                   timeout=10.0)
            try:
                names = {r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}
                cols = {r[1] for r in conn.execute("PRAGMA table_info(operator_requests)")}
            finally:
                conn.close()
        except (sqlite3.Error, OSError) as exc:
            raise OperatorQueueUnavailable(UNREADABLE, type(exc).__name__) from None
        if not set(TABLES) <= names or "uid" not in cols:
            raise OperatorQueueUnavailable(UNREADABLE, "not an operator queue")

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        if not self.path.exists():  # deleted while open: never recreated by a connect
            raise OperatorQueueUnavailable(NOT_INITIALISED, str(self.path))
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

    def enqueue(
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
            want = expected_confirmation(action, str(target), kind=kind, outcome=outcome,
                                         quantity=quantity, avg_price=avg_price)
            if confirmation.strip() != want:
                problems.append(f"type exactly: {want}")
        if problems:
            raise OperatorInputError("; ".join(problems))
        with self._tx() as c:
            cur = c.execute(
                """INSERT INTO operator_requests (uid,at,operator,origin,action,target,kind,
                outcome,quantity,avg_price,note,confirmation) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (uuid.uuid4().hex, at.isoformat(), operator.strip(), origin, action, str(target),
                 kind, outcome, quantity, avg_price, note.strip(), confirmation.strip()),
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
