"""Durable source journal + atomic mock order/position ledger.

This is purposely NOT a production order router. All fills are fictional.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import sqlite3

from tradingbot.models import (
    Action, Decision, MockOrder, MockPolicy, Position, PositionSide,
    ProcessResult, Quote, SignalCandidate, SourceEvent,
)
from tradingbot.validator import fill_quantity, validate


class Journal:
    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as c, c:
            c.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS events (
                    source_id TEXT PRIMARY KEY,
                    source_type TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    source_text TEXT NOT NULL,
                    published_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    candidate_json TEXT,
                    decision_json TEXT,
                    order_json TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS positions (
                    idea_id TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    asset_class TEXT NOT NULL,
                    side TEXT NOT NULL,
                    quantity INTEGER NOT NULL CHECK (quantity > 0)
                );
            """)

    def connect(self) -> sqlite3.Connection:
        """Open a connection. Callers close it (see `closing(...)` below); nothing is left to GC."""
        c = sqlite3.connect(self.path, timeout=10.0)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA busy_timeout=10000")
        return c

    def reserve(self, event: SourceEvent) -> bool:
        with closing(self.connect()) as c, c:
            c.execute("BEGIN IMMEDIATE")
            cur = c.execute(
                """INSERT OR IGNORE INTO events
                (source_id, source_type, provider, source_text, published_at, status, created_at)
                VALUES (?, ?, ?, ?, ?, 'RECEIVED', ?)""",
                (
                    event.source_id, event.source_type, event.provider, event.text,
                    event.published_at.isoformat(), datetime.now(timezone.utc).isoformat(),
                ),
            )
            return cur.rowcount == 1

    def position(self, idea_id: str | None) -> Position | None:
        if not idea_id:
            return None
        with closing(self.connect()) as c, c:
            row = c.execute("SELECT * FROM positions WHERE idea_id = ?", (idea_id,)).fetchone()
        return Position.model_validate(dict(row)) if row else None

    def commit(
        self,
        source: SourceEvent,
        candidate: SignalCandidate,
        quote: Quote | None,
        policy: MockPolicy,
    ) -> ProcessResult:
        """Re-check preflight under a SQLite writer lock; then apply fictional fill atomically."""
        with closing(self.connect()) as c, c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT status FROM events WHERE source_id=?", (source.source_id,)).fetchone()
            if row is None or row["status"] != "RECEIVED":
                raise RuntimeError("Event was not reserved or has already been completed")
            idea = candidate.idea_reference if candidate.action != Action.OPEN else None
            position_row = (
                c.execute("SELECT * FROM positions WHERE idea_id=?", (idea,)).fetchone()
                if idea else None
            )
            position = Position.model_validate(dict(position_row)) if position_row else None
            decision = validate(source, candidate, quote, policy, position)
            order = None
            if decision.status == "EXECUTE_MOCK":
                assert candidate.instrument is not None and quote is not None
                if candidate.action == Action.OPEN:
                    assert candidate.side is not None
                    qty = fill_quantity(Action.OPEN, policy, None)
                    side = "BUY" if candidate.side == PositionSide.LONG else "SELL"
                    idea_id = source.source_id
                    c.execute(
                        "INSERT INTO positions (idea_id,symbol,asset_class,side,quantity) VALUES (?,?,?,?,?)",
                        (idea_id, candidate.instrument.symbol, candidate.instrument.asset_class.value,
                         candidate.side.value, qty),
                    )
                else:
                    assert position is not None
                    idea_id = position.idea_id
                    side = "SELL" if position.side == PositionSide.LONG else "BUY"
                    qty = fill_quantity(candidate.action, policy, position, candidate.close_fraction)
                    remaining = position.quantity - qty
                    if remaining == 0:
                        c.execute("DELETE FROM positions WHERE idea_id=?", (idea_id,))
                    else:
                        c.execute(
                            "UPDATE positions SET quantity=? WHERE idea_id=?", (remaining, idea_id)
                        )
                mock_fill = quote.ask if side == "BUY" else quote.bid
                order = MockOrder(
                    order_id=f"mock-{source.source_id}", source_id=source.source_id,
                    idea_id=idea_id, action=candidate.action,
                    symbol=candidate.instrument.symbol, asset_class=candidate.instrument.asset_class,
                    side=side, quantity=qty, mock_fill_price=str(mock_fill),
                    stop_price=candidate.stop_price if candidate.action == Action.OPEN else None,
                    target_price=candidate.target_price if candidate.action == Action.OPEN else None,
                )
            c.execute(
                """UPDATE events SET status=?, candidate_json=?, decision_json=?, order_json=?
                WHERE source_id=?""",
                (
                    decision.status, candidate.model_dump_json(), decision.model_dump_json(),
                    order.model_dump_json() if order else None, source.source_id,
                ),
            )
        return ProcessResult(source_id=source.source_id, candidate=candidate, decision=decision, order=order)

    def mark_error(self, source_id: str, message: str):
        with closing(self.connect()) as c, c:
            c.execute(
                "UPDATE events SET status='ERROR',decision_json=? WHERE source_id=? AND status='RECEIVED'",
                (Decision(status="BLOCK", reasons=["INTERPRETER_ERROR", message[:300]]).model_dump_json(),
                 source_id),
            )

    def recent_events(self, limit: int = 50) -> list[dict]:
        with closing(self.connect()) as c, c:
            rows = c.execute(
                """SELECT source_id,provider,status,source_text,published_at,decision_json,order_json
                FROM events ORDER BY created_at DESC LIMIT ?""", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def positions(self) -> list[dict]:
        with closing(self.connect()) as c, c:
            rows = c.execute("SELECT * FROM positions ORDER BY idea_id").fetchall()
        return [dict(r) for r in rows]
