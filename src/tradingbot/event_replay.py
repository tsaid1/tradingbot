"""Deterministic, SQLite-atomic replay COORDINATOR for multi-action source posts.

Authorization lives in validator.py (gate_source / gate_action). This module only sequences the
calls, keeps linked actions atomic, enforces once-only processing per (source_id, revision),
defines the one safe path for a truncated post to be completed, and applies fictional fills
sized by validator.fill_quantity. NO broker connectivity, NO real orders, NO automatic mapping
of reference markets. Paid post text is never stored; only hashes and structured mock state.
"""
from __future__ import annotations

import sqlite3
from collections import OrderedDict
from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from tradingbot.event_models import (
    ActionOutcome, PaperOrder, ParsedPost, ReplayResult, ReplaySource, TradeAction,
)
from tradingbot.models import Action, AssetClass, MockPolicy, Position, PositionSide, Quote
from tradingbot.validator import ALL_ACTIONS, fill_quantity, gate_action, gate_source

SCHEMA = """
CREATE TABLE IF NOT EXISTS replay_posts (
    source_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    post_hash TEXT NOT NULL,
    complete_text INTEGER NOT NULL DEFAULT 1,
    hydrated_from_hash TEXT,
    published_at TEXT NOT NULL,
    received_at TEXT NOT NULL,
    processed_at TEXT NOT NULL,
    PRIMARY KEY (source_id, revision)
);
CREATE TABLE IF NOT EXISTS replay_actions (
    source_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 0,
    action_id TEXT NOT NULL,
    result_json TEXT NOT NULL,
    PRIMARY KEY (source_id, revision, attempt, action_id),
    FOREIGN KEY (source_id, revision) REFERENCES replay_posts(source_id, revision)
);
CREATE TABLE IF NOT EXISTS replay_ideas (
    idea_id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    asset_class TEXT NOT NULL,
    position_side TEXT NOT NULL,
    quantity INTEGER NOT NULL CHECK (quantity >= 0),
    stop_price TEXT,
    target_price TEXT,
    status TEXT NOT NULL
);
"""


@dataclass
class Idea:
    idea_id: str
    symbol: str
    asset_class: AssetClass
    position_side: PositionSide
    quantity: int
    stop_price: str | None
    target_price: str | None
    status: str = "OPEN"

    def as_position(self) -> Position | None:
        """The gateway's view of this idea; None once it is closed or empty."""
        if self.status != "OPEN" or self.quantity <= 0:
            return None
        return Position(idea_id=self.idea_id, symbol=self.symbol, asset_class=self.asset_class,
                        side=self.position_side, quantity=self.quantity,
                        stop_price=self.stop_price, target_price=self.target_price)


class PaperEventReplay:
    def __init__(self, db_path: str | Path, policy: MockPolicy | None = None):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.policy = policy or MockPolicy()
        with self._connection() as conn:
            conn.executescript(SCHEMA)

    # ------------------------------------------------------------------ connections (always closed)

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=10000")
            yield conn
        finally:
            conn.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        """One writer transaction; commits on success, rolls back on any exception, always closes."""
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.rollback()
                raise
            else:
                conn.commit()

    # ------------------------------------------------------------------ reads

    def ideas(self) -> list[dict]:
        with self._connection() as c:
            return [dict(r) for r in c.execute("SELECT * FROM replay_ideas ORDER BY idea_id")]

    def outcomes(self, source_id: str, revision: int = 1) -> list[ActionOutcome]:
        with self._connection() as c:
            return self._recorded(c, source_id, revision)

    @staticmethod
    def _recorded(c: sqlite3.Connection, source_id: str, revision: int) -> list[ActionOutcome]:
        """Outcomes of the latest evaluation pass (attempt) for a post revision."""
        rows = c.execute(
            """SELECT result_json FROM replay_actions
            WHERE source_id=? AND revision=? AND attempt=(
                SELECT COALESCE(MAX(attempt), 0) FROM replay_actions WHERE source_id=? AND revision=?)
            ORDER BY rowid""",
            (source_id, revision, source_id, revision),
        ).fetchall()
        return [ActionOutcome.model_validate_json(row["result_json"]) for row in rows]

    def _load_ideas(self, c: sqlite3.Connection) -> dict[str, Idea]:
        result: dict[str, Idea] = {}
        for r in c.execute("SELECT * FROM replay_ideas"):
            result[r["idea_id"]] = Idea(
                idea_id=r["idea_id"], symbol=r["symbol"], asset_class=AssetClass(r["asset_class"]),
                position_side=PositionSide(r["position_side"]), quantity=r["quantity"],
                stop_price=r["stop_price"], target_price=r["target_price"], status=r["status"],
            )
        return result

    def _save_ideas(self, c: sqlite3.Connection, ideas: dict[str, Idea]) -> None:
        for idea in ideas.values():
            c.execute(
                """INSERT INTO replay_ideas
                (idea_id,symbol,asset_class,position_side,quantity,stop_price,target_price,status)
                VALUES (?,?,?,?,?,?,?,?)
                ON CONFLICT(idea_id) DO UPDATE SET
                symbol=excluded.symbol,asset_class=excluded.asset_class,
                position_side=excluded.position_side,quantity=excluded.quantity,
                stop_price=excluded.stop_price,target_price=excluded.target_price,
                status=excluded.status""",
                (idea.idea_id, idea.symbol, idea.asset_class.value, idea.position_side.value,
                 idea.quantity, idea.stop_price, idea.target_price, idea.status),
            )

    # ------------------------------------------------------------------ coordinator

    def process(
        self,
        source: ReplaySource,
        parsed: ParsedPost,
        quotes: dict[str, Quote] | None = None,
        *,
        now: datetime | None = None,
    ) -> ReplayResult:
        """Once-only processing of a post revision.

        Duplicate protection: the same (source_id, revision, post_hash) returns the recorded
        outcomes and never fills again. A different hash under the same revision is blocked, with
        one exception, hydration: a post first received truncated (complete_text=False, which only
        ever yields HOLD) may be evaluated once more when its complete text arrives. The row is then
        sealed as complete, so any further hash change is blocked, and the pass still runs through
        gate_source (so an edit recorded meanwhile, or a revision other than 1, is still held) and
        through the freshness rules (so a late completion is stale).

        `now` makes offline historic replay deterministic. The caller must not set it to bypass
        freshness rules for live trading; this engine has no live route.
        """
        now = now or datetime.now(timezone.utc)
        quotes = quotes or {}
        with self._transaction() as c:
            prior = c.execute(
                "SELECT post_hash, complete_text FROM replay_posts WHERE source_id=? AND revision=?",
                (source.source_id, source.revision),
            ).fetchone()
            attempt = 0
            hydrated = False
            if prior is not None:
                if prior["post_hash"] == source.post_hash:
                    return ReplayResult(source_id=source.source_id, revision=source.revision, duplicate=True,
                                        outcomes=self._recorded(c, source.source_id, source.revision))
                if not prior["complete_text"] and source.complete_text:
                    hydrated = True
                    attempt = c.execute(
                        "SELECT COALESCE(MAX(attempt), 0) + 1 FROM replay_actions WHERE source_id=? AND revision=?",
                        (source.source_id, source.revision),
                    ).fetchone()[0]
                    c.execute(
                        """UPDATE replay_posts SET post_hash=?, complete_text=1, hydrated_from_hash=?,
                        received_at=?, processed_at=? WHERE source_id=? AND revision=?""",
                        (source.post_hash, prior["post_hash"], source.received_at.isoformat(),
                         now.isoformat(), source.source_id, source.revision),
                    )
                else:
                    return ReplayResult(
                        source_id=source.source_id, revision=source.revision, duplicate=True,
                        outcomes=[ActionOutcome(action_id="_source", status="BLOCK",
                                                reasons=["SOURCE_HASH_CHANGED_WITHOUT_REVISION"])],
                    )
            else:
                c.execute(
                    """INSERT INTO replay_posts
                    (source_id,revision,post_hash,complete_text,published_at,received_at,processed_at)
                    VALUES (?,?,?,?,?,?,?)""",
                    (source.source_id, source.revision, source.post_hash, int(source.complete_text),
                     source.published_at.isoformat(), source.received_at.isoformat(), now.isoformat()),
                )
            is_edit = c.execute(
                "SELECT 1 FROM replay_posts WHERE source_id=? AND revision<>? LIMIT 1",
                (source.source_id, source.revision),
            ).fetchone() is not None
            global_block = gate_source(source, now, is_edit=is_edit)

            original = self._load_ideas(c)
            committed = deepcopy(original)
            # An atomic group is an all-or-nothing position/risk change. Independent groups are
            # evaluated separately. Child action IDs are unique within a post (schema-enforced).
            groups: OrderedDict[str, list[TradeAction]] = OrderedDict()
            for action in parsed.actions:
                key = f"atomic:{action.atomic_group}" if action.atomic_group else f"single:{action.action_id}"
                groups.setdefault(key, []).append(action)
            outcomes: list[ActionOutcome] = []
            for group in groups.values():
                if global_block is not None:
                    group_outcomes = [ActionOutcome(action_id=a.action_id, status=global_block.status,
                                                    reasons=list(global_block.reasons)) for a in group]
                else:
                    staged = deepcopy(committed)
                    group_outcomes = [self._simulate(source, a, quotes, staged, now) for a in group]
                    if all(o.status == "SIMULATED" for o in group_outcomes):
                        group_outcomes = self._execute_group(source, group, group_outcomes, staged, now)
                        committed = staged
                    elif len(group) > 1:
                        # A partial ADD + MODIFY would misrepresent risk; do neither.
                        group_outcomes = [
                            ActionOutcome(action_id=a.action_id, status="HOLD",
                                          reasons=["ATOMIC_GROUP_REJECTED", *o.reasons])
                            for a, o in zip(group, group_outcomes, strict=True)
                        ]
                outcomes.extend(group_outcomes)
            self._save_ideas(c, committed)
            for item in outcomes:
                c.execute(
                    "INSERT INTO replay_actions (source_id,revision,attempt,action_id,result_json) VALUES (?,?,?,?,?)",
                    (source.source_id, source.revision, attempt, item.action_id, item.model_dump_json()),
                )
        return ReplayResult(source_id=source.source_id, revision=source.revision,
                            hydrated=hydrated, outcomes=outcomes)

    def _execute_group(self, source: ReplaySource, group: list[TradeAction],
                       outcomes: list[ActionOutcome], ideas: dict[str, Idea],
                       now: datetime) -> list[ActionOutcome]:
        """Execution seam, called only for a group the gateway approved in full. The mock ledger keeps
        its fictional fills as they are; a broker-backed subclass (execution.replay.BrokerEventReplay)
        executes the group and replaces outcomes and staged ideas with what the broker actually did."""
        return outcomes

    def _simulate(self, source: ReplaySource, a: TradeAction, quotes: dict[str, Quote],
                  ideas: dict[str, Idea], now: datetime) -> ActionOutcome:
        """Ask the gateway; on APPROVE apply a fictional fill to the staged ledger."""
        idea = ideas.get(a.idea_reference) if a.idea_reference else None
        position = idea.as_position() if idea is not None else None
        quote = quotes.get(a.instrument.symbol) if a.instrument is not None else None
        gate = gate_action(a, source.published_at, quote, self.policy, position, now=now,
                           supported_actions=ALL_ACTIONS)
        if gate.status != "APPROVE":
            return ActionOutcome(action_id=a.action_id, status=gate.status, reasons=list(gate.reasons))
        assert a.instrument is not None and quote is not None
        inst = a.instrument
        order_id = f"paper-{source.source_id}-{source.revision}-{a.action_id}"

        def filled(idea_id: str, side: str, qty: int, price: Decimal | None) -> ActionOutcome:
            order = PaperOrder(order_id=order_id, idea_id=idea_id, action_id=a.action_id, action=a.action,
                               symbol=inst.symbol, side=side, quantity=qty,
                               price=str(price) if price is not None else None,
                               status="SIMULATED_AMENDED" if a.action == Action.MODIFY else "SIMULATED_FILLED")
            return ActionOutcome(action_id=a.action_id, status="SIMULATED", reasons=list(gate.reasons), order=order)

        if a.action == Action.OPEN:
            assert a.position_side is not None
            idea_id = f"{source.source_id}:{a.action_id}"
            if idea_id in ideas:
                return ActionOutcome(action_id=a.action_id, status="BLOCK", reasons=["IDEA_ALREADY_EXISTS"])
            long = a.position_side == PositionSide.LONG
            qty = fill_quantity(Action.OPEN, self.policy, None)
            ideas[idea_id] = Idea(idea_id, inst.symbol, inst.asset_class, a.position_side, qty,
                                  a.stop_price, a.target_price)
            return filled(idea_id, "BUY" if long else "SELL", qty, quote.ask if long else quote.bid)
        assert idea is not None and position is not None
        long = idea.position_side == PositionSide.LONG
        if a.action in (Action.CLOSE, Action.REDUCE):
            qty = fill_quantity(a.action, self.policy, position, a.close_fraction)
            idea.quantity -= qty
            if idea.quantity == 0:
                idea.status = "CLOSED"
            return filled(idea.idea_id, "SELL" if long else "BUY", qty, quote.bid if long else quote.ask)
        if a.action == Action.ADD:
            qty = fill_quantity(Action.ADD, self.policy, position)
            idea.quantity += qty
            return filled(idea.idea_id, "BUY" if long else "SELL", qty, quote.ask if long else quote.bid)
        if a.action == Action.MODIFY:
            if a.stop_price is not None:
                idea.stop_price = a.stop_price
            if a.target_price is not None:
                idea.target_price = a.target_price
            return filled(idea.idea_id, "NONE", 0, None)
        return ActionOutcome(action_id=a.action_id, status="HOLD", reasons=["UNKNOWN_ACTION"])
