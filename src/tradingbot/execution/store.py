"""Durable execution state: intents (write-ahead), our view of each broker order, applied fills, the
follower ledger, an append-only journal and exception records. SQLite, one file, like the rest of
the repo.

The follower ledger changes only when a broker fill is applied (``apply_fill``); nothing here
infers a position from an order having been sent.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path

from pydantic import Field

from tradingbot.models import Action, AssetClass, PositionSide, StrictModel

SCHEMA = """
CREATE TABLE IF NOT EXISTS exec_intents (
    order_ref TEXT PRIMARY KEY,
    source_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    action_id TEXT NOT NULL,
    action TEXT NOT NULL,
    idea_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT,
    quantity INTEGER NOT NULL,
    order_type TEXT,
    limit_price TEXT,
    stop_price TEXT,
    target_price TEXT,
    gate_status TEXT NOT NULL,
    gate_reasons TEXT NOT NULL,
    account TEXT NOT NULL,
    state TEXT NOT NULL,
    broker_status TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    reasons TEXT NOT NULL DEFAULT '[]',
    source_published_at TEXT,
    entry_reference TEXT,
    submitted_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (source_id, revision, action_id)
);
CREATE TABLE IF NOT EXISTS exec_orders (
    order_ref TEXT PRIMARY KEY,
    intent_ref TEXT NOT NULL REFERENCES exec_intents(order_ref),
    idea_id TEXT NOT NULL,
    role TEXT NOT NULL,
    parent_ref TEXT,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    order_type TEXT NOT NULL,
    limit_price TEXT,
    stop_price TEXT,
    status TEXT NOT NULL,
    filled_quantity INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS exec_fills (
    exec_id TEXT PRIMARY KEY,
    order_ref TEXT NOT NULL,
    idea_id TEXT NOT NULL,
    role TEXT NOT NULL,
    side TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    price TEXT NOT NULL,
    executed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS exec_positions (
    idea_id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    asset_class TEXT NOT NULL,
    position_side TEXT NOT NULL,
    quantity INTEGER NOT NULL CHECK (quantity >= 0),
    avg_price TEXT,
    stop_price TEXT,
    target_price TEXT,
    stop_ref TEXT,
    target_ref TEXT,
    status TEXT NOT NULL,
    protection TEXT,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS exec_journal (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    order_ref TEXT,
    idea_id TEXT,
    event TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS exec_exceptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    kind TEXT NOT NULL,
    idea_id TEXT,
    order_ref TEXT,
    detail TEXT NOT NULL,
    resolved_at TEXT
);
"""


class IntentState(str, Enum):
    RECORDED = "RECORDED"  # durably written, submission outcome not yet known
    UNRESOLVED = "UNRESOLVED"  # ack lost or lookup failed: look up, never resend blindly
    WORKING = "WORKING"  # acknowledged; working or partially filled
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"  # terminal; may carry a partial fill
    REJECTED = "REJECTED"
    APPLIED = "APPLIED"  # a MODIFY confirmed at the broker
    HALTED = "HALTED"  # the halt switch was present; nothing submitted
    REFUSED = "REFUSED"  # an execution precondition failed; nothing submitted


UNRESOLVED_STATES = frozenset({IntentState.RECORDED, IntentState.UNRESOLVED})
# An exit fill larger than the follower position: the broker account went through flat.
LEDGER_REVERSAL = "LEDGER_REVERSAL"


class Role(str, Enum):
    ENTRY = "ENTRY"  # opens or adds
    EXIT = "EXIT"  # reduce-only REDUCE / CLOSE
    STOP = "STOP"  # protective stop child
    TARGET = "TARGET"  # protective target child
    # A stop in the execution instrument beside a reference-triggered stop (multi-asset mapping):
    # it protects the hours the reference series does not update.
    BACKSTOP = "BACKSTOP"


PROTECTIVE_ROLES = frozenset({Role.STOP, Role.TARGET, Role.BACKSTOP})


class Intent(StrictModel):
    order_ref: str
    source_id: str
    revision: int
    action_id: str
    action: Action
    idea_id: str
    symbol: str
    side: str | None = None
    quantity: int = Field(ge=0)
    order_type: str | None = None
    limit_price: str | None = None
    stop_price: str | None = None
    target_price: str | None = None
    gate_status: str
    gate_reasons: list[str] = Field(default_factory=list)
    account: str
    state: IntentState
    broker_status: str | None = None
    attempts: int = 0
    reasons: list[str] = Field(default_factory=list)
    # When the source post was published: a resend after an unknown outcome re-checks freshness.
    source_published_at: datetime | None = None
    # The source's entry level (OPEN / ADD), so a resend can re-run the entry checks around it.
    entry_reference: str | None = None
    # When the last submission request was made: the no-trace settle period counts from here.
    submitted_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    # The resolved contract (ContractRef JSON) and its identity; None on a refusal before resolve
    # and on intents recorded before round 2.
    contract_key: str | None = None
    contract: str | None = None
    # The gateway's execution plan for an action in a mapped family (models.ExecutionPlan JSON).
    plan: str | None = None


class OrderRow(StrictModel):
    order_ref: str
    intent_ref: str
    idea_id: str
    role: Role
    parent_ref: str | None = None
    symbol: str
    side: str
    quantity: int
    order_type: str
    limit_price: str | None = None
    stop_price: str | None = None
    status: str
    filled_quantity: int = 0
    updated_at: datetime
    contract_key: str | None = None
    # A broker-side price condition on a reference series (broker.PriceCondition JSON).
    condition: str | None = None


class LedgerPosition(StrictModel):
    """One follower idea. ``status``: PENDING (entry working, nothing filled yet), OPEN, CLOSED,
    NOT_FILLED (the entry ended without any fill), REVERSED (an exit fill exceeded the position:
    the ledger stops at zero and a LEDGER_REVERSAL exception holds the excess)."""

    idea_id: str
    symbol: str
    asset_class: AssetClass
    position_side: PositionSide
    quantity: int = Field(ge=0)
    avg_price: str | None = None
    stop_price: str | None = None
    target_price: str | None = None
    stop_ref: str | None = None
    target_ref: str | None = None
    status: str
    # CONFIRMED only after a successful broker check made after the last applied fill; every
    # applied fill resets it to UNKNOWN (safety review M1). UNPROTECTED when the check saw no cover.
    protection: str | None = None
    updated_at: datetime
    # The idea's contract (ContractRef JSON) and identity: positions, caps, netting and
    # reconciliation key on this, never on the symbol alone.
    contract_key: str | None = None
    contract: str | None = None
    # Multi-asset mapping. EXECUTION: stop and target are execution prices (as before). REFERENCE:
    # they are reference-series levels held as broker-side conditions on ``reference_key``; a
    # ``backstop_ref`` stop at ``backstop_price`` (execution prices) may work beside them.
    protection_series: str | None = None
    reference_key: str | None = None
    backstop_ref: str | None = None
    backstop_price: str | None = None
    # The publisher's own stop level when ``stop_price`` is a converted one (BASIS_SHIFT), so an
    # amendment can tell a publisher tightening from a loosening.
    publisher_stop: str | None = None
    # The version-2 mapping entry (runtime.mapping.FamilyMapping JSON) the idea was opened under.
    # Approval governs new exposure only: exits and protective amendments on this idea use these
    # terms if the family later loses its approval or its entry.
    mapping_terms: str | None = None


class JournalEntry(StrictModel):
    seq: int
    at: datetime
    order_ref: str | None = None
    idea_id: str | None = None
    event: str
    detail: dict = Field(default_factory=dict)


class ExceptionRecord(StrictModel):
    id: int
    at: datetime
    kind: str
    idea_id: str | None = None
    order_ref: str | None = None
    detail: str
    resolved_at: datetime | None = None


def _row(model, row: sqlite3.Row, **json_fields):
    data = dict(row)
    for name in json_fields:
        data[name] = json.loads(data[name]) if data[name] else []
    return model.model_validate(data)


class ExecutionStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.tx() as c:
            c.executescript(SCHEMA)
            cols = {r["name"] for r in c.execute("PRAGMA table_info(exec_intents)")}
            for col in ("source_published_at", "entry_reference", "submitted_at"):
                if col not in cols:  # a store created before this column existed
                    c.execute(f"ALTER TABLE exec_intents ADD COLUMN {col} TEXT")
            # round 2, Part B: contract identity; multi-asset mapping: the gateway's plan
            for col in ("contract_key", "contract", "plan"):
                if col not in cols:
                    c.execute(f"ALTER TABLE exec_intents ADD COLUMN {col} TEXT")
            pcols = {r["name"] for r in c.execute("PRAGMA table_info(exec_positions)")}
            for col in ("protection", "contract_key", "contract", "protection_series",
                        "reference_key", "backstop_ref", "backstop_price", "publisher_stop", "mapping_terms"):
                if col not in pcols:
                    c.execute(f"ALTER TABLE exec_positions ADD COLUMN {col} TEXT")
            ocols = {r["name"] for r in c.execute("PRAGMA table_info(exec_orders)")}
            for col in ("contract_key", "condition"):
                if col not in ocols:
                    c.execute(f"ALTER TABLE exec_orders ADD COLUMN {col} TEXT")

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

    # ------------------------------------------------------------------ intents

    def insert_intent(self, c: sqlite3.Connection, intent: Intent) -> None:
        d = intent.model_dump(mode="json")
        d["gate_reasons"] = json.dumps(d["gate_reasons"])
        d["reasons"] = json.dumps(d["reasons"])
        cols = ",".join(d)
        c.execute(
            f"INSERT INTO exec_intents ({cols}) VALUES ({','.join('?' * len(d))})", list(d.values())
        )

    def intent(self, order_ref: str) -> Intent | None:
        with self.tx() as c:
            row = c.execute("SELECT * FROM exec_intents WHERE order_ref=?", (order_ref,)).fetchone()
        return _row(Intent, row, gate_reasons=1, reasons=1) if row else None

    def intents(self, states: frozenset[IntentState] | None = None) -> list[Intent]:
        with self.tx() as c:
            rows = c.execute("SELECT * FROM exec_intents ORDER BY created_at, rowid").fetchall()
        out = [_row(Intent, r, gate_reasons=1, reasons=1) for r in rows]
        return [i for i in out if states is None or i.state in states]

    def set_state(
        self,
        order_ref: str,
        state: IntentState,
        at: datetime,
        *,
        broker_status: str | None = None,
        reasons: list[str] | None = None,
        attempts_delta: int = 0,
    ) -> None:
        with self.tx() as c:
            c.execute(
                """UPDATE exec_intents SET state=?, broker_status=COALESCE(?, broker_status),
                reasons=COALESCE(?, reasons), attempts=attempts+?, updated_at=?
                WHERE order_ref=?""",
                (
                    state.value,
                    broker_status,
                    json.dumps(reasons) if reasons is not None else None,
                    attempts_delta,
                    at.isoformat(),
                    order_ref,
                ),
            )

    def committed_exit_quantity(self, idea_id: str, *, exclude_ref: str | None = None) -> int:
        """Quantity of the idea already committed to exits that may be live at the broker: every
        REDUCE / CLOSE intent that is not terminal (recorded, unknown outcome or working), less what
        it has filled. Protective children are sized to the position minus this (review R1)."""
        total = 0
        for i in self.intents(UNRESOLVED_STATES | {IntentState.WORKING}):
            if i.idea_id != idea_id or i.order_ref == exclude_ref:
                continue
            if i.action not in (Action.REDUCE, Action.CLOSE):
                continue
            total += max(i.quantity - self.filled_quantity(i.order_ref), 0)
        return total

    def set_intent_limit(self, order_ref: str, limit_price: str, at: datetime) -> None:
        """Re-price an exit that was never sent (review F8); nothing else changes a limit."""
        with self.tx() as c:
            c.execute(
                "UPDATE exec_intents SET limit_price=?, updated_at=? WHERE order_ref=?",
                (limit_price, at.isoformat(), order_ref),
            )

    def mark_submitted(self, order_ref: str, at: datetime) -> None:
        with self.tx() as c:
            c.execute(
                "UPDATE exec_intents SET attempts=attempts+1, submitted_at=?, updated_at=? "
                "WHERE order_ref=?",
                (at.isoformat(), at.isoformat(), order_ref),
            )

    # ------------------------------------------------------------------ orders

    def insert_order(self, c: sqlite3.Connection, row: OrderRow) -> None:
        d = row.model_dump(mode="json")
        c.execute(
            f"INSERT INTO exec_orders ({','.join(d)}) VALUES ({','.join('?' * len(d))})",
            list(d.values()),
        )

    def orders(self, intent_ref: str | None = None) -> list[OrderRow]:
        with self.tx() as c:
            if intent_ref is None:
                rows = c.execute("SELECT * FROM exec_orders ORDER BY rowid").fetchall()
            else:
                rows = c.execute(
                    "SELECT * FROM exec_orders WHERE intent_ref=? ORDER BY rowid", (intent_ref,)
                ).fetchall()
        return [_row(OrderRow, r) for r in rows]

    def order(self, order_ref: str) -> OrderRow | None:
        with self.tx() as c:
            row = c.execute("SELECT * FROM exec_orders WHERE order_ref=?", (order_ref,)).fetchone()
        return _row(OrderRow, row) if row else None

    def update_order(
        self,
        order_ref: str,
        at: datetime,
        *,
        status: str,
        quantity: int | None = None,
        stop_price: str | None = None,
        limit_price: str | None = None,
    ) -> None:
        with self.tx() as c:
            c.execute(
                """UPDATE exec_orders SET status=?, quantity=COALESCE(?,quantity),
                stop_price=COALESCE(?,stop_price), limit_price=COALESCE(?,limit_price), updated_at=?
                WHERE order_ref=?""",
                (status, quantity, stop_price, limit_price, at.isoformat(), order_ref),
            )

    # ------------------------------------------------------------------ fills and ledger

    def has_fill(self, exec_id: str) -> bool:
        with self.tx() as c:
            return (
                c.execute("SELECT 1 FROM exec_fills WHERE exec_id=?", (exec_id,)).fetchone()
                is not None
            )

    def filled_quantity(self, order_ref: str) -> int:
        with self.tx() as c:
            return c.execute(
                "SELECT COALESCE(SUM(quantity),0) FROM exec_fills WHERE order_ref=?", (order_ref,)
            ).fetchone()[0]

    def fills(self, order_ref: str | None = None) -> list[dict]:
        with self.tx() as c:
            if order_ref is None:
                rows = c.execute("SELECT * FROM exec_fills ORDER BY executed_at, rowid").fetchall()
            else:
                rows = c.execute(
                    "SELECT * FROM exec_fills WHERE order_ref=? ORDER BY executed_at, rowid",
                    (order_ref,),
                ).fetchall()
        return [dict(r) for r in rows]

    def apply_fill(
        self,
        *,
        exec_id: str,
        order: OrderRow,
        quantity: int,
        price: Decimal,
        executed_at: datetime,
        at: datetime,
        c: sqlite3.Connection | None = None,
    ) -> LedgerPosition | None:
        """The only writer of follower quantities: one broker execution, applied exactly once.
        An exit fill larger than the position is recorded as it happened; the ledger stops at zero,
        the idea becomes REVERSED and a LEDGER_REVERSAL exception is opened in the same
        transaction (safety review R2). With ``c`` it runs inside the caller's transaction (an
        operator settlement applies its declared fill and its journal entry atomically)."""
        if c is None:
            with self.tx() as conn:
                return self.apply_fill(
                    exec_id=exec_id, order=order, quantity=quantity, price=price,
                    executed_at=executed_at, at=at, c=conn,
                )
        if c.execute("SELECT 1 FROM exec_fills WHERE exec_id=?", (exec_id,)).fetchone():
            return None
        c.execute(
            """INSERT INTO exec_fills
            (exec_id,order_ref,idea_id,role,side,quantity,price,executed_at)
            VALUES (?,?,?,?,?,?,?,?)""",
            (
                exec_id,
                order.order_ref,
                order.idea_id,
                order.role.value,
                order.side,
                quantity,
                str(price),
                executed_at.isoformat(),
            ),
        )
        c.execute(
            "UPDATE exec_orders SET filled_quantity=filled_quantity+?, updated_at=? "
            "WHERE order_ref=?",
            (quantity, at.isoformat(), order.order_ref),
        )
        pos = c.execute(
            "SELECT * FROM exec_positions WHERE idea_id=?", (order.idea_id,)
        ).fetchone()
        if pos is None:
            raise RuntimeError(f"fill for unknown idea {order.idea_id}")
        qty = pos["quantity"]
        avg = Decimal(pos["avg_price"]) if pos["avg_price"] else None
        excess = 0
        if order.role == Role.ENTRY:
            new_qty = qty + quantity
            avg = ((avg or Decimal(0)) * qty + price * quantity) / new_qty
        else:
            new_qty = qty - quantity
            if new_qty < 0:
                excess, new_qty = -new_qty, 0
        status = "OPEN" if new_qty > 0 else ("REVERSED" if excess else "CLOSED")
        if excess:
            detail = (
                f"{order.role.value} {order.side} {quantity} @ {price} ({exec_id}) against a "
                f"follower position of {qty}: the account went through flat by {excess}. "
                "New exposure in this instrument is blocked until a human resolves this."
            )
            c.execute(
                "INSERT INTO exec_exceptions (at,kind,idea_id,order_ref,detail) "
                "VALUES (?,?,?,?,?)",
                (at.isoformat(), LEDGER_REVERSAL, order.idea_id, order.order_ref, detail),
            )
            self.log(
                at,
                LEDGER_REVERSAL,
                order_ref=order.order_ref,
                idea_id=order.idea_id,
                c=c,
                exec_id=exec_id,
                fill_quantity=quantity,
                ledger_before=qty,
                excess=excess,
            )
        c.execute(
            "UPDATE exec_positions SET quantity=?, avg_price=?, status=?, "
            "protection='UNKNOWN', updated_at=? WHERE idea_id=?",
            (
                new_qty,
                str(avg.quantize(Decimal("0.0001"))) if avg is not None else None,
                status,
                at.isoformat(),
                order.idea_id,
            ),
        )
        row = c.execute(
            "SELECT * FROM exec_positions WHERE idea_id=?", (order.idea_id,)
        ).fetchone()
        return _row(LedgerPosition, row)

    def insert_position(self, c: sqlite3.Connection, pos: LedgerPosition) -> None:
        d = pos.model_dump(mode="json")
        c.execute(
            f"INSERT INTO exec_positions ({','.join(d)}) VALUES ({','.join('?' * len(d))})",
            list(d.values()),
        )

    def position(self, idea_id: str) -> LedgerPosition | None:
        with self.tx() as c:
            row = c.execute("SELECT * FROM exec_positions WHERE idea_id=?", (idea_id,)).fetchone()
        return _row(LedgerPosition, row) if row else None

    def positions(self) -> list[LedgerPosition]:
        with self.tx() as c:
            rows = c.execute("SELECT * FROM exec_positions ORDER BY idea_id").fetchall()
        return [_row(LedgerPosition, r) for r in rows]

    def update_position(self, idea_id: str, at: datetime, **fields) -> None:
        allowed = {"status", "stop_price", "target_price", "protection", "backstop_price",
                   "publisher_stop", "mapping_terms"}
        if not fields or set(fields) - allowed:
            raise ValueError(f"only {allowed} may be set directly; quantities change through fills")
        sets = ",".join(f"{k}=?" for k in fields)
        with self.tx() as c:
            c.execute(
                f"UPDATE exec_positions SET {sets}, updated_at=? WHERE idea_id=?",
                [*fields.values(), at.isoformat(), idea_id],
            )

    # ------------------------------------------------------------------ journal and exceptions

    def log(
        self,
        at: datetime,
        event: str,
        *,
        order_ref: str | None = None,
        idea_id: str | None = None,
        c: sqlite3.Connection | None = None,
        **detail,
    ) -> None:
        args = (
            at.isoformat(),
            order_ref,
            idea_id,
            event,
            json.dumps(detail, default=str, sort_keys=True),
        )
        sql = "INSERT INTO exec_journal (at,order_ref,idea_id,event,detail) VALUES (?,?,?,?,?)"
        if c is not None:
            c.execute(sql, args)
            return
        with self.tx() as conn:
            conn.execute(sql, args)

    def journal(self, order_ref: str | None = None) -> list[JournalEntry]:
        with self.tx() as c:
            if order_ref is None:
                rows = c.execute("SELECT * FROM exec_journal ORDER BY seq").fetchall()
            else:
                rows = c.execute(
                    "SELECT * FROM exec_journal WHERE order_ref=? ORDER BY seq", (order_ref,)
                ).fetchall()
        return [
            JournalEntry(
                seq=r["seq"],
                at=r["at"],
                order_ref=r["order_ref"],
                idea_id=r["idea_id"],
                event=r["event"],
                detail=json.loads(r["detail"]),
            )
            for r in rows
        ]

    def raise_exception(
        self,
        at: datetime,
        kind: str,
        detail: str,
        *,
        idea_id: str | None = None,
        order_ref: str | None = None,
    ) -> bool:
        """Open an exception record unless the same kind is already open for this idea/order."""
        with self.tx() as c:
            open_ = c.execute(
                "SELECT 1 FROM exec_exceptions WHERE kind=? AND idea_id IS ? AND order_ref IS ? "
                "AND resolved_at IS NULL",
                (kind, idea_id, order_ref),
            ).fetchone()
            if open_:
                return False
            c.execute(
                "INSERT INTO exec_exceptions (at,kind,idea_id,order_ref,detail) VALUES (?,?,?,?,?)",
                (at.isoformat(), kind, idea_id, order_ref, detail),
            )
        return True

    def resolve_exceptions(
        self, at: datetime, kind: str, *, idea_id: str | None = None, order_ref: str | None = None
    ) -> int:
        """Resolve the open exceptions of ``kind`` for ``idea_id`` (and, when given, only those
        recorded against ``order_ref``)."""
        sql = (
            "UPDATE exec_exceptions SET resolved_at=? WHERE kind=? AND idea_id IS ? "
            "AND resolved_at IS NULL"
        )
        args: list = [at.isoformat(), kind, idea_id]
        if order_ref is not None:
            sql += " AND order_ref=?"
            args.append(order_ref)
        with self.tx() as c:
            return c.execute(sql, args).rowcount

    def exceptions(self, *, open_only: bool = False) -> list[ExceptionRecord]:
        with self.tx() as c:
            rows = c.execute("SELECT * FROM exec_exceptions ORDER BY id").fetchall()
        out = [ExceptionRecord.model_validate(dict(r)) for r in rows]
        return [e for e in out if not open_only or e.resolved_at is None]

    def exception(self, exc_id: int) -> ExceptionRecord | None:
        with self.tx() as c:
            row = c.execute("SELECT * FROM exec_exceptions WHERE id=?", (exc_id,)).fetchone()
        return ExceptionRecord.model_validate(dict(row)) if row else None

    def resolve_exception_id(self, exc_id: int, at: datetime, c: sqlite3.Connection) -> bool:
        """Resolve one open exception record (operator actions; inside the caller's transaction)."""
        return c.execute(
            "UPDATE exec_exceptions SET resolved_at=? WHERE id=? AND resolved_at IS NULL",
            (at.isoformat(), exc_id),
        ).rowcount == 1
