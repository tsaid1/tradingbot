"""Durable runtime state (SQLite): source cursors, the signal journal with stage timestamps, held
signals and other observable exceptions, alerts, the interpreter's point-in-time context, the last
broker snapshot and the heartbeat.

No post text is stored. A signal row keeps the post's identity, its SHA-256, lengths, link and media
counts and timestamps; everything else is structured interpretation and decision data. The
interpreter context holds asset codes, sides, price levels and times, never wording.
"""

from __future__ import annotations

import dataclasses
import json
import os
import sqlite3
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from enum import Enum
from pathlib import Path

from pydantic import BaseModel

import tradingbot.tkl_interpreter as tki
from tradingbot.tkl_interpreter import PointInTimeContext

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

OFFERS_SCHEMA = """
CREATE TABLE IF NOT EXISTS confirmation_offers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    hash TEXT NOT NULL UNIQUE,
    source_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    action_id TEXT NOT NULL,
    idea_id TEXT NOT NULL,
    family TEXT NOT NULL,
    state TEXT NOT NULL,
    state_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    record TEXT NOT NULL,
    quote_view TEXT,
    result TEXT,
    operator TEXT,
    request_uid TEXT
);
"""


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


# ----------------------------------------------------------------------------- context codec
#
# The interpreter's point-in-time context is a tree of dataclasses (tkl_interpreter) holding asset
# codes, sides, price levels, times and event histories. The codec is generic over those
# dataclasses (so a richer context keeps round-tripping) and refuses anything that could carry post
# text: a pydantic model (SourcePost) is never serialized, and context fields that hold posts are
# left out and come back empty.

EXCLUDED_FIELDS = {("PointInTimeContext", "posts")}


def _enum_types() -> dict[str, type[Enum]]:
    from tradingbot import models

    out: dict[str, type[Enum]] = {}
    for module in (models, tki):
        for name in dir(module):
            obj = getattr(module, name)
            if isinstance(obj, type) and issubclass(obj, Enum):
                out[name] = obj
    return out


def _encode(value):
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, datetime):
        return {"$dt": value.isoformat()}
    if isinstance(value, Enum):
        return {"$enum": [type(value).__name__, value.value]}
    if isinstance(value, BaseModel):
        raise TypeError("refusing to persist a model (it may carry post text)")
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        name = type(value).__name__
        return {
            "$dc": name,
            "f": {
                f.name: _encode(getattr(value, f.name))
                for f in dataclasses.fields(value)
                if (name, f.name) not in EXCLUDED_FIELDS
            },
        }
    if isinstance(value, tuple):
        return {"$t": [_encode(v) for v in value]}
    if isinstance(value, (set, frozenset)):
        return {"$s": sorted((_encode(v) for v in value), key=lambda x: json.dumps(x))}
    if isinstance(value, list):
        return [_encode(v) for v in value]
    if isinstance(value, dict):
        return {"$d": [[_encode(k), _encode(v)] for k, v in value.items()]}
    raise TypeError(f"cannot persist {type(value).__name__} in the interpreter context")


def _decode(value, enums: dict[str, type[Enum]]):
    if isinstance(value, list):
        return [_decode(v, enums) for v in value]
    if not isinstance(value, dict):
        return value
    if "$dt" in value:
        return datetime.fromisoformat(value["$dt"])
    if "$enum" in value:
        name, raw = value["$enum"]
        return enums[name](raw)
    if "$t" in value:
        return tuple(_decode(v, enums) for v in value["$t"])
    if "$s" in value:
        return {_decode(v, enums) for v in value["$s"]}
    if "$d" in value:
        return {_decode(k, enums): _decode(v, enums) for k, v in value["$d"]}
    if "$dc" in value:
        cls = getattr(tki, value["$dc"], None)
        if cls is None or not dataclasses.is_dataclass(cls):
            raise ValueError(f"unknown context type {value['$dc']}")
        obj = cls.__new__(cls)
        stored = value["f"]
        for f in dataclasses.fields(cls):
            if f.name in stored:
                setattr(obj, f.name, _decode(stored[f.name], enums))
            elif f.default is not dataclasses.MISSING:
                setattr(obj, f.name, f.default)
            elif f.default_factory is not dataclasses.MISSING:
                setattr(obj, f.name, f.default_factory())
            else:
                raise ValueError(f"context field {value['$dc']}.{f.name} missing")
        return obj
    raise ValueError("unrecognised context value")


def context_to_json(ctx: PointInTimeContext) -> str:
    return json.dumps(_encode(ctx), sort_keys=True)


def context_from_json(text: str) -> PointInTimeContext:
    ctx = _decode(json.loads(text), _enum_types())
    if not isinstance(ctx, PointInTimeContext):
        raise TypeError("stored interpreter context has the wrong type")
    return ctx


# ----------------------------------------------------------------------------- store


class RuntimeStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.tx() as c:
            c.executescript(SCHEMA)
            cols = {r["name"] for r in c.execute("PRAGMA table_info(signal_actions)")}
            if "mapping_family" not in cols:  # multi-asset mapping: the family the action fell in
                c.execute("ALTER TABLE signal_actions ADD COLUMN mapping_family TEXT")
            for col in ("settled_state", "settled_reasons"):  # a WORKING row's later outcome
                if col not in cols:
                    c.execute(f"ALTER TABLE signal_actions ADD COLUMN {col} TEXT")
            acols = {r["name"] for r in c.execute("PRAGMA table_info(alerts)")}
            for col, kind in (("last_notified_at", "TEXT"), ("notified", "INTEGER DEFAULT 0")):
                if col not in acols:  # operations controls: re-alert schedule
                    c.execute(f"ALTER TABLE alerts ADD COLUMN {col} {kind}")

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

    def settleable_signal_actions(self) -> list[dict]:
        """The journal rows written WORKING that name an order: those that still read WORKING and
        those already settled from their intent (``settled_state``), which follow it if it
        changes again."""
        with self.tx() as c:
            rows = c.execute(
                "SELECT seq, action_id, order_ref, status, reasons, settled_state, "
                "settled_reasons FROM signal_actions WHERE order_ref IS NOT NULL "
                "AND (status='WORKING' OR settled_state IS NOT NULL) ORDER BY seq, rowid"
            ).fetchall()
        return [{**dict(r), "reasons": json.loads(r["reasons"]),
                 "settled_reasons": json.loads(r["settled_reasons"] or "[]")} for r in rows]

    def settle_signal_action(self, seq: int, action_id: str, *, expect: str, status: str,
                             reasons: list[str], settled_state: str | None,
                             settled_reasons: list[str], at: datetime, detail: dict) -> bool:
        """Set one row to the status its intent shows now and journal SIGNAL_ACTION_SETTLED, in
        ONE transaction. Only a row still reading ``expect`` is changed. True when it was."""
        with self.tx() as c:
            cur = c.execute(
                "UPDATE signal_actions SET status=?, reasons=?, settled_state=?, "
                "settled_reasons=? WHERE seq=? AND action_id=? AND status=?",
                (status, json.dumps(list(reasons)), settled_state,
                 json.dumps(list(settled_reasons)) if settled_state else None,
                 seq, action_id, expect),
            )
            if cur.rowcount == 0:
                return False
            c.execute(
                "INSERT INTO service_events (at,event,detail) VALUES (?,?,?)",
                (at.isoformat(), "SIGNAL_ACTION_SETTLED",
                 json.dumps({"seq": seq, "action_id": action_id, "was": expect,
                             "status": status, **detail}, sort_keys=True, default=str)),
            )
        return True

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
                resolved_at=NULL, last_notified_at=NULL""",
                (key, kind, detail, at.isoformat(), at.isoformat()),
            )
            return True

    def alert_notify_due(self, key: str, at: datetime, every_seconds: float) -> bool:
        """True when the open alert ``key`` was never notified, or last notified at least
        ``every_seconds`` ago. It is not marked here: ``mark_alert_notified`` does that once a
        channel has accepted the notification (review note c)."""
        with self.tx() as c:
            row = c.execute(
                "SELECT last_notified_at, resolved_at FROM alerts WHERE key=?", (key,)
            ).fetchone()
            if row is None or row["resolved_at"] is not None:
                return False
            last = _dt(row["last_notified_at"])
            return last is None or (at - last).total_seconds() >= every_seconds

    def mark_alert_notified(self, key: str, at: datetime) -> None:
        with self.tx() as c:
            c.execute(
                "UPDATE alerts SET last_notified_at=?, notified=COALESCE(notified,0)+1 WHERE key=?",
                (at.isoformat(), key),
            )

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


    # ------------------------------------------------------------------ operator confirmation
    # Created only when [operator_confirmation] is enabled (ensure_offers): with the switch off
    # the store's schema and content are exactly as before. Typed fields only, never post text.

    def ensure_offers(self) -> None:
        with self.tx() as c:
            c.executescript(OFFERS_SCHEMA)

    def has_offers(self) -> bool:
        with self.tx() as c:
            return c.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                             "AND name='confirmation_offers'").fetchone() is not None

    def add_offer(self, record: dict, at: datetime) -> int | None:
        """Store a new offer (state OFFERED); None when an offer with this hash exists."""
        with self.tx() as c:
            cur = c.execute(
                """INSERT OR IGNORE INTO confirmation_offers (hash,source_id,revision,action_id,
                idea_id,family,state,created_at,updated_at,expires_at,record)
                VALUES (?,?,?,?,?,?,'OFFERED',?,?,?,?)""",
                (record["hash"], record["source_id"], record["revision"], record["action_id"],
                 record["idea_id"], record["family"], at.isoformat(), at.isoformat(),
                 record["expires_at"], json.dumps(record, sort_keys=True, default=str)))
            return int(cur.lastrowid) if cur.rowcount else None

    def offers(self, *, states: tuple[str, ...] | None = None) -> list[dict]:
        if not self.has_offers():
            return []
        with self.tx() as c:
            rows = c.execute("SELECT * FROM confirmation_offers ORDER BY id").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            if states is not None and d["state"] not in states:
                continue
            for k in ("record", "quote_view", "result"):
                d[k] = json.loads(d[k]) if d.get(k) else None
            out.append(d)
        return out

    def offer(self, offer_id: int) -> dict | None:
        return next((o for o in self.offers() if o["id"] == offer_id), None)

    def set_offer_state(self, offer_id: int, *, expect: tuple[str, ...], state: str,
                        at: datetime, reason: str | None = None, result: dict | None = None,
                        operator: str | None = None, request_uid: str | None = None) -> bool:
        """Compare and set: moves the offer only from one of ``expect`` (one-shot)."""
        with self.tx() as c:
            cur = c.execute(
                f"""UPDATE confirmation_offers SET state=?, updated_at=?,
                state_reason=COALESCE(?, state_reason), result=COALESCE(?, result),
                operator=COALESCE(?, operator), request_uid=COALESCE(?, request_uid)
                WHERE id=? AND state IN ({",".join("?" * len(expect))})""",
                (state, at.isoformat(), reason,
                 json.dumps(result, sort_keys=True, default=str) if result is not None else None,
                 operator, request_uid, offer_id, *expect))
            return cur.rowcount == 1

    def set_offer_quote(self, offer_id: int, view: dict, at: datetime) -> None:
        with self.tx() as c:
            c.execute("UPDATE confirmation_offers SET quote_view=? WHERE id=?",
                      (json.dumps(view, sort_keys=True, default=str), offer_id))


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
