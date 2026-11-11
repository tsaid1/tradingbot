"""Durable runtime state (SQLite): source cursors, the signal journal with stage timestamps, held
signals and other observable exceptions, alerts, the interpreter's point-in-time context, the last
broker snapshot and the heartbeat.

No post text is stored. A signal row keeps the post's identity, its SHA-256, lengths, link and media
counts and timestamps; everything else is structured interpretation and decision data. The
interpreter context holds asset codes, sides, price levels and times, never wording.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from tradingbot.models import Action, PositionSide
from tradingbot.tkl_interpreter import IdeaView, PendingIntent, PointInTimeContext

SCHEMA = """
CREATE TABLE IF NOT EXISTS source_state (
    name TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS signals (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    source_name TEXT NOT NULL,
    source_id TEXT NOT NULL,
    post_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    post_hash TEXT NOT NULL,
    provenance TEXT NOT NULL,
    verified_author INTEGER NOT NULL,
    text_complete INTEGER NOT NULL,
    text_len INTEGER NOT NULL,
    media_count INTEGER NOT NULL,
    links TEXT NOT NULL,
    published_at TEXT NOT NULL,
    revised_at TEXT,
    received_at TEXT NOT NULL,
    interpreted_at TEXT NOT NULL,
    decided_at TEXT NOT NULL,
    decision_now TEXT NOT NULL,
    duplicate INTEGER NOT NULL,
    hydrated INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS signal_actions (
    seq INTEGER NOT NULL REFERENCES signals(seq),
    action_id TEXT NOT NULL,
    action TEXT,
    timing TEXT,
    asset TEXT,
    side TEXT,
    mapping_status TEXT,
    mapping_basis TEXT,
    mapping_reason TEXT,
    broker_symbol TEXT,
    status TEXT NOT NULL,
    reasons TEXT NOT NULL,
    order_ref TEXT,
    gate_decided_at TEXT,
    PRIMARY KEY (seq, action_id)
);
CREATE TABLE IF NOT EXISTS runtime_exceptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    kind TEXT NOT NULL,
    signal_seq INTEGER,
    source_id TEXT,
    revision INTEGER,
    action_id TEXT,
    order_ref TEXT,
    reasons TEXT NOT NULL DEFAULT '[]',
    detail TEXT NOT NULL DEFAULT '',
    resolved_at TEXT
);
CREATE TABLE IF NOT EXISTS alerts (
    key TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    detail TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    resolved_at TEXT
);
CREATE TABLE IF NOT EXISTS service_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    event TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


# ----------------------------------------------------------------------------- context codec


def context_to_json(ctx: PointInTimeContext) -> str:
    def idea(i: IdeaView) -> dict:
        d = asdict(i)
        d["side"] = i.side.value
        d["opened_at"] = _iso(i.opened_at)
        d["closed_at"] = _iso(i.closed_at)
        return d

    def intent(p: PendingIntent) -> dict:
        d = asdict(p)
        d["action"] = p.action.value
        d["created_at"] = _iso(p.created_at)
        d["cancelled_at"] = _iso(p.cancelled_at)
        return d

    return json.dumps(
        {
            "ideas": [idea(i) for i in ctx.ideas.values()],
            "intents": [intent(p) for p in ctx.intents.values()],
            "observed_posts": sorted([list(k) for k in ctx.observed_posts]),
            "seen_posts": sorted([list(k) for k in ctx.seen_posts]),
        },
        sort_keys=True,
    )


def context_from_json(text: str) -> PointInTimeContext:
    raw = json.loads(text)
    ctx = PointInTimeContext()
    for d in raw.get("ideas", []):
        ctx.ideas[d["idea_id"]] = IdeaView(
            idea_id=d["idea_id"],
            source_id=d["source_id"],
            asset=d["asset"],
            side=PositionSide(d["side"]),
            opened_at=_dt(d["opened_at"]),
            closed_at=_dt(d["closed_at"]),
            stop_price=d.get("stop_price"),
            target_price=d.get("target_price"),
        )
    for d in raw.get("intents", []):
        ctx.intents[d["intent_id"]] = PendingIntent(
            intent_id=d["intent_id"],
            asset=d["asset"],
            action=Action(d["action"]),
            trigger=d.get("trigger"),
            created_at=_dt(d["created_at"]),
            cancelled_at=_dt(d["cancelled_at"]),
        )
    ctx.observed_posts = {(s, int(r)) for s, r in raw.get("observed_posts", [])}
    ctx.seen_posts = {(s, int(r)) for s, r in raw.get("seen_posts", [])}
    return ctx


# ----------------------------------------------------------------------------- store


class RuntimeStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.tx() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
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

    # ------------------------------------------------------------------ source cursors

    def source_state(self, name: str) -> dict:
        with self.tx() as c:
            row = c.execute("SELECT value FROM source_state WHERE name=?", (name,)).fetchone()
        return json.loads(row["value"]) if row else {}

    def put_source_state(self, name: str, value: dict, at: datetime) -> None:
        with self.tx() as c:
            c.execute(
                """INSERT INTO source_state (name,value,updated_at) VALUES (?,?,?)
                ON CONFLICT(name) DO UPDATE SET value=excluded.value,
                updated_at=excluded.updated_at""",
                (name, json.dumps(value, sort_keys=True), at.isoformat()),
            )

    # ------------------------------------------------------------------ key/value

    def get(self, key: str, default=None):
        with self.tx() as c:
            row = c.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def put(self, key: str, value, at: datetime, c: sqlite3.Connection | None = None) -> None:
        sql = """INSERT INTO kv (key,value,updated_at) VALUES (?,?,?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value,
                updated_at=excluded.updated_at"""
        args = (key, json.dumps(value, sort_keys=True, default=str), at.isoformat())
        if c is not None:
            c.execute(sql, args)
            return
        with self.tx() as conn:
            conn.execute(sql, args)

    def load_context(self) -> PointInTimeContext:
        with self.tx() as c:
            row = c.execute("SELECT value FROM kv WHERE key='interpreter_context'").fetchone()
        return context_from_json(json.loads(row["value"])) if row else PointInTimeContext()

    # ------------------------------------------------------------------ signal journal

    def record_signal(
        self,
        *,
        signal: dict,
        actions: list[dict],
        exceptions: list[dict],
        context: PointInTimeContext,
        at: datetime,
    ) -> int:
        """One transaction: the signal, its actions, any held-signal exceptions and the interpreter
        context as advanced by this post."""
        with self.tx() as c:
            cols = list(signal)
            cur = c.execute(
                f"INSERT INTO signals ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                [signal[k] for k in cols],
            )
            seq = cur.lastrowid
            for a in actions:
                row = {"seq": seq, **a}
                cols = list(row)
                c.execute(
                    f"INSERT INTO signal_actions ({','.join(cols)}) "
                    f"VALUES ({','.join('?' * len(cols))})",
                    [row[k] for k in cols],
                )
            for e in exceptions:
                self._insert_exception(c, at, signal_seq=seq, **e)
            self.put("interpreter_context", context_to_json(context), at, c=c)
        return seq

    def signals(self) -> list[dict]:
        with self.tx() as c:
            rows = c.execute("SELECT * FROM signals ORDER BY seq").fetchall()
        return [dict(r) for r in rows]

    def signal_actions(self, seq: int | None = None) -> list[dict]:
        with self.tx() as c:
            if seq is None:
                rows = c.execute("SELECT * FROM signal_actions ORDER BY seq, rowid").fetchall()
            else:
                rows = c.execute(
                    "SELECT * FROM signal_actions WHERE seq=? ORDER BY rowid", (seq,)
                ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["reasons"] = json.loads(d["reasons"])
            out.append(d)
        return out

    # ------------------------------------------------------------------ exceptions

    @staticmethod
    def _insert_exception(c, at: datetime, *, kind: str, reasons=(), detail: str = "", **ids):
        c.execute(
            """INSERT INTO runtime_exceptions
            (at,kind,signal_seq,source_id,revision,action_id,order_ref,reasons,detail)
            VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                at.isoformat(),
                kind,
                ids.get("signal_seq"),
                ids.get("source_id"),
                ids.get("revision"),
                ids.get("action_id"),
                ids.get("order_ref"),
                json.dumps(list(reasons)),
                detail,
            ),
        )

    def add_exception(self, at: datetime, kind: str, **fields) -> None:
        with self.tx() as c:
            self._insert_exception(c, at, kind=kind, **fields)

    def exceptions(self, *, open_only: bool = False) -> list[dict]:
        with self.tx() as c:
            rows = c.execute("SELECT * FROM runtime_exceptions ORDER BY id").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["reasons"] = json.loads(d["reasons"])
            if not open_only or d["resolved_at"] is None:
                out.append(d)
        return out

    def resolve_exception(self, exc_id: int, at: datetime, note: str) -> None:
        with self.tx() as c:
            c.execute(
                "UPDATE runtime_exceptions SET resolved_at=?, detail=detail || ? "
                "WHERE id=? AND resolved_at IS NULL",
                (at.isoformat(), f" [resolved: {note}]", exc_id),
            )

    # ------------------------------------------------------------------ alerts and events

    def open_alert(self, key: str, kind: str, detail: str, at: datetime) -> bool:
        """True when the alert is new (or re-opened): the caller notifies only then."""
        with self.tx() as c:
            row = c.execute("SELECT resolved_at FROM alerts WHERE key=?", (key,)).fetchone()
            if row is not None and row["resolved_at"] is None:
                c.execute(
                    "UPDATE alerts SET last_seen_at=?, detail=? WHERE key=?",
                    (at.isoformat(), detail, key),
                )
                return False
            c.execute(
                """INSERT INTO alerts (key,kind,detail,opened_at,last_seen_at) VALUES (?,?,?,?,?)
                ON CONFLICT(key) DO UPDATE SET kind=excluded.kind, detail=excluded.detail,
                opened_at=excluded.opened_at, last_seen_at=excluded.last_seen_at,
                resolved_at=NULL""",
                (key, kind, detail, at.isoformat(), at.isoformat()),
            )
            return True

    def resolve_alerts(self, at: datetime, *, keep: set[str], prefix: str) -> list[str]:
        """Resolve every open alert under ``prefix`` whose key is not in ``keep``."""
        with self.tx() as c:
            rows = c.execute(
                "SELECT key FROM alerts WHERE resolved_at IS NULL AND key LIKE ?", (prefix + "%",)
            ).fetchall()
            gone = [r["key"] for r in rows if r["key"] not in keep]
            for key in gone:
                c.execute("UPDATE alerts SET resolved_at=? WHERE key=?", (at.isoformat(), key))
        return gone

    def alerts(self, *, open_only: bool = False) -> list[dict]:
        with self.tx() as c:
            rows = c.execute("SELECT * FROM alerts ORDER BY opened_at, key").fetchall()
        return [dict(r) for r in rows if not open_only or r["resolved_at"] is None]

    def event(self, at: datetime, event: str, **detail) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO service_events (at,event,detail) VALUES (?,?,?)",
                (at.isoformat(), event, json.dumps(detail, sort_keys=True, default=str)),
            )

    def events(self) -> list[dict]:
        with self.tx() as c:
            rows = c.execute("SELECT * FROM service_events ORDER BY seq").fetchall()
        return [{**dict(r), "detail": json.loads(r["detail"])} for r in rows]


def write_json_atomic(path: Path, payload: dict) -> None:
    """Write-then-rename so a reader never sees a half-written heartbeat."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, sort_keys=True, default=str)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)
