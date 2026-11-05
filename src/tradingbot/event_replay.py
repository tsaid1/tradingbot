"""Deterministic, SQLite-atomic mock replay of multi-action source posts.

NO broker connectivity, NO real orders, NO automatic mapping of reference markets.
The entire event is committed in one local transaction. Atomic groups must all
validate before any position change in that group; unrelated groups may proceed.
"""
from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
import sqlite3

from tradingbot.event_models import (
    ActionOutcome, PaperOrder, ParsedPost, PriceBasis, ReplayPolicy,
    ReplayResult, ReplaySource, Timing, TradeAction,
)
from tradingbot.models import Action, AssetClass, PositionSide, Quote


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


def positive_decimal(value: str | None) -> Decimal | None:
    if value is None:
        return None
    try:
        v = Decimal(value)
    except (InvalidOperation, ValueError, TypeError):
        return None
    return v if v.is_finite() and v > 0 else None


class PaperEventReplay:
    def __init__(self, db_path: str | Path, policy: ReplayPolicy | None = None):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.policy = policy or ReplayPolicy()
        with self._connect() as conn:
            conn.executescript("""
            CREATE TABLE IF NOT EXISTS replay_posts (
                source_id TEXT NOT NULL,
                revision INTEGER NOT NULL,
                post_hash TEXT NOT NULL,
                published_at TEXT NOT NULL,
                received_at TEXT NOT NULL,
                processed_at TEXT NOT NULL,
                PRIMARY KEY (source_id, revision)
            );
            CREATE TABLE IF NOT EXISTS replay_actions (
                source_id TEXT NOT NULL,
                revision INTEGER NOT NULL,
                action_id TEXT NOT NULL,
                result_json TEXT NOT NULL,
                PRIMARY KEY (source_id, revision, action_id),
                FOREIGN KEY (source_id, revision)
                    REFERENCES replay_posts(source_id, revision)
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
            """)

    def _connect(self):
        conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def ideas(self) -> list[dict]:
        with self._connect() as c:
            return [dict(r) for r in c.execute("SELECT * FROM replay_ideas ORDER BY idea_id")]

    def outcomes(self, source_id: str, revision: int = 1) -> list[ActionOutcome]:
        with self._connect() as c:
            rows = c.execute(
                """SELECT result_json FROM replay_actions
                WHERE source_id=? AND revision=? ORDER BY rowid""",
                (source_id, revision),
            ).fetchall()
        return [ActionOutcome.model_validate_json(row["result_json"]) for row in rows]

    def _load_ideas(self, c: sqlite3.Connection) -> dict[str, Idea]:
        result = {}
        for r in c.execute("SELECT * FROM replay_ideas"):
            result[r["idea_id"]] = Idea(
                idea_id=r["idea_id"], symbol=r["symbol"],
                asset_class=AssetClass(r["asset_class"]),
                position_side=PositionSide(r["position_side"]),
                quantity=r["quantity"], stop_price=r["stop_price"],
                target_price=r["target_price"], status=r["status"],
            )
        return result

    def _save_ideas(self, c: sqlite3.Connection, ideas: dict[str, Idea]):
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
                (idea.idea_id, idea.symbol, idea.asset_class.value,
                 idea.position_side.value, idea.quantity, idea.stop_price,
                 idea.target_price, idea.status),
            )

    def process(
        self,
        source: ReplaySource,
        parsed: ParsedPost,
        quotes: dict[str, Quote] | None = None,
        *,
        now: datetime | None = None,
    ) -> ReplayResult:
        """Once-only processing of a post revision; do not persist paid source text.

        `now` makes offline historic replay deterministic. The caller must not set
        it to bypass freshness rules for live trading; this engine has no live route.
        """
        now = now or datetime.now(timezone.utc)
        quotes = quotes or {}
        with self._connect() as c:
            c.execute("BEGIN IMMEDIATE")
            prior = c.execute(
                "SELECT post_hash FROM replay_posts WHERE source_id=? AND revision=?",
                (source.source_id, source.revision),
            ).fetchone()
            if prior is not None:
                if prior["post_hash"] != source.post_hash:
                    return ReplayResult(
                        source_id=source.source_id, revision=source.revision,
                        duplicate=True,
                        outcomes=[ActionOutcome(
                            action_id="_source", status="BLOCK",
                            reasons=["SOURCE_HASH_CHANGED_WITHOUT_REVISION"],
                        )],
                    )
                rows = c.execute(
                    """SELECT result_json FROM replay_actions
                    WHERE source_id=? AND revision=? ORDER BY rowid""",
                    (source.source_id, source.revision),
                ).fetchall()
                return ReplayResult(
                    source_id=source.source_id, revision=source.revision,
                    duplicate=True,
                    outcomes=[ActionOutcome.model_validate_json(r["result_json"]) for r in rows],
                )
            is_edit = c.execute(
                "SELECT 1 FROM replay_posts WHERE source_id=? LIMIT 1", (source.source_id,)
            ).fetchone() is not None
            c.execute(
                """INSERT INTO replay_posts
                (source_id,revision,post_hash,published_at,received_at,processed_at)
                VALUES (?,?,?,?,?,?)""",
                (source.source_id, source.revision, source.post_hash,
                 source.published_at.isoformat(), source.received_at.isoformat(),
                 now.isoformat()),
            )
            original = self._load_ideas(c)
            committed = deepcopy(original)
            global_failure = self._global_block(source, now, is_edit)
            # An atomic group is an all-or-nothing position/risk change. Independent
            # groups are evaluated separately. Child action IDs are always unique.
            groups: OrderedDict[str, list[TradeAction]] = OrderedDict()
            for action in parsed.actions:
                key = f"atomic:{action.atomic_group}" if action.atomic_group else f"single:{action.action_id}"
                groups.setdefault(key, []).append(action)
            outcomes: list[ActionOutcome] = []
            for group in groups.values():
                if global_failure:
                    group_outcomes = [ActionOutcome(action_id=a.action_id,
                                                    status=global_failure[0],
                                                    reasons=[global_failure[1]]) for a in group]
                else:
                    staged = deepcopy(committed)
                    group_outcomes = [self._evaluate(source, a, quotes, staged, now) for a in group]
                    all_succeeded = all(o.status == "SIMULATED" for o in group_outcomes)
                    if all_succeeded:
                        committed = staged
                    elif len(group) > 1:
                        # A partial ADD + MODIFY would misrepresent risk; do neither.
                        group_outcomes = [
                            ActionOutcome(action_id=a.action_id, status="HOLD",
                                          reasons=["ATOMIC_GROUP_REJECTED", *o.reasons])
                            for a, o in zip(group, group_outcomes)
                        ]
                outcomes.extend(group_outcomes)
            self._save_ideas(c, committed)
            for item in outcomes:
                c.execute(
                    "INSERT INTO replay_actions (source_id,revision,action_id,result_json) VALUES (?,?,?,?)",
                    (source.source_id, source.revision, item.action_id, item.model_dump_json()),
                )
        return ReplayResult(source_id=source.source_id, revision=source.revision,
                            outcomes=outcomes)

    @staticmethod
    def _global_block(source: ReplaySource, now: datetime, is_edit: bool):
        if is_edit or source.revision != 1:
            return "HOLD", "SOURCE_EDIT_REQUIRES_REVIEW"
        if not source.verified_author:
            return "BLOCK", "SOURCE_NOT_AUTHENTICATED"
        if source.is_repost:
            return "NO_TRADE", "REPOST_NOT_AUTHOR_INSTRUCTION"
        if not source.complete_text:
            return "HOLD", "SOURCE_TEXT_TRUNCATED"
        if (source.published_at.tzinfo is None or source.received_at.tzinfo is None
                or now.tzinfo is None):
            return "BLOCK", "SOURCE_TIMEZONE_MISSING"
        if source.received_at < source.published_at:
            return "BLOCK", "SOURCE_RECEIPT_BEFORE_PUBLICATION"
        return None

    def _evaluate(self, source, a, quotes, ideas, now):
        def result(status, *reasons, order=None):
            return ActionOutcome(action_id=a.action_id, status=status,
                                 reasons=list(reasons), order=order)

        if a.timing == Timing.HISTORICAL:
            return result("RECONCILE", "HISTORICAL_REPORT_NO_ORDER")
        if a.timing == Timing.CONDITIONAL:
            return result("HOLD", "CONDITIONAL_TRIGGER_NOT_IMPLEMENTED")
        if a.timing == Timing.COMMENTARY or a.action == Action.NO_TRADE:
            return result("NO_TRADE", "COMMENTARY_OR_SUMMARY")
        if a.timing != Timing.CURRENT:
            return result("HOLD", "TEMPORAL_INTENT_UNCERTAIN")
        if not a.explicit_instruction or not a.evidence:
            return result("HOLD", "NOT_EXPLICITLY_AUTHORITATIVE")
        if a.ambiguities:
            return result("HOLD", "SOURCE_AMBIGUITIES", *a.ambiguities)
        if a.instrument is None:
            return result("HOLD", "ORDERABLE_INSTRUMENT_MISSING")
        inst = a.instrument
        if a.price_basis != PriceBasis.TRADED_CONTRACT:
            return result("HOLD", "SOURCE_TO_BROKER_PRICE_BASIS_UNRESOLVED")
        if not a.source_market_symbol:
            return result("HOLD", "SOURCE_PRICE_INSTRUMENT_IDENTITY_UNVERIFIED")
        if a.source_market_symbol != inst.symbol:
            return result("HOLD", "SOURCE_TO_BROKER_MAPPING_UNAPPROVED")
        if inst.asset_class not in (AssetClass.EQUITY, AssetClass.ETF):
            return result("HOLD", "ASSET_CLASS_PAPER_ADAPTER_UNSUPPORTED")
        if inst.symbol not in self.policy.allowed_symbols:
            return result("BLOCK", "INSTRUMENT_NOT_ALLOWLISTED")
        if a.action not in (Action.OPEN, Action.ADD, Action.REDUCE, Action.CLOSE, Action.MODIFY):
            return result("HOLD", "ACTION_UNSUPPORTED")
        age = (now - source.published_at).total_seconds()
        if age < -5 or age > self.policy.max_source_age_seconds:
            return result("BLOCK", "SOURCE_STALE_OR_FUTURE")
        q = quotes.get(inst.symbol)
        if not q or q.symbol != inst.symbol or q.observed_at.tzinfo is None:
            return result("BLOCK", "QUOTE_MISSING_OR_MISMATCHED")
        quote_age = (now - q.observed_at).total_seconds()
        if quote_age < -5 or quote_age > self.policy.max_quote_age_seconds:
            return result("BLOCK", "QUOTE_STALE_OR_FUTURE")
        if q.bid <= 0 or q.ask < q.bid:
            return result("BLOCK", "INVALID_BID_ASK")
        spread = ((q.ask - q.bid) / ((q.bid + q.ask) / 2)) * 100
        if spread > self.policy.max_spread_pct:
            return result("BLOCK", "SPREAD_TOO_WIDE")

        if a.action == Action.OPEN:
            if a.idea_reference is not None or a.position_side is None:
                return result("HOLD", "OPEN_MISSING_DIRECTION_OR_HAS_PRIOR_ID")
            ref = positive_decimal(a.entry_reference)
            if ref is None:
                return result("HOLD", "OPEN_ENTRY_PRICE_MISSING")
            price = q.ask if a.position_side == PositionSide.LONG else q.bid
            if abs(price - ref) / ref * 100 > self.policy.max_deviation_pct:
                return result("BLOCK", "ENTRY_PRICE_COLLAR")
            stop = positive_decimal(a.stop_price)
            target = positive_decimal(a.target_price)
            if self.policy.require_stop_on_open and stop is None:
                return result("HOLD", "PROTECTIVE_STOP_MISSING")
            if (a.stop_price is not None and stop is None) or (a.target_price is not None and target is None):
                return result("BLOCK", "MALFORMED_PROTECTION_LEVEL")
            if stop is not None and (stop >= ref if a.position_side == PositionSide.LONG else stop <= ref):
                return result("BLOCK", "STOP_DIRECTION_CONTRADICTION")
            if stop is not None and (
                (a.position_side == PositionSide.LONG and stop >= q.bid)
                or (a.position_side == PositionSide.SHORT and stop <= q.ask)
            ):
                return result("BLOCK", "STOP_ALREADY_CROSSED_AT_ENTRY")
            if target is not None and (target <= ref if a.position_side == PositionSide.LONG else target >= ref):
                return result("BLOCK", "TARGET_DIRECTION_CONTRADICTION")
            if price * self.policy.quantity_per_open > self.policy.max_notional_usd:
                return result("BLOCK", "NOTIONAL_LIMIT")
            idea_id = f"{source.source_id}:{a.action_id}"
            if idea_id in ideas:
                return result("BLOCK", "IDEA_ALREADY_EXISTS")
            ideas[idea_id] = Idea(
                idea_id, inst.symbol, inst.asset_class, a.position_side,
                self.policy.quantity_per_open, a.stop_price, a.target_price,
            )
            order = PaperOrder(
                order_id=f"paper-{source.source_id}-{source.revision}-{a.action_id}",
                idea_id=idea_id, action_id=a.action_id, action=a.action,
                symbol=inst.symbol,
                side="BUY" if a.position_side == PositionSide.LONG else "SELL",
                quantity=self.policy.quantity_per_open, price=str(price), status="SIMULATED_FILLED",
            )
            return result("SIMULATED", "PAPER_OPEN", order=order)

        idea = ideas.get(a.idea_reference or "")
        if idea is None or idea.status != "OPEN" or idea.quantity == 0:
            return result("HOLD", "REFERENCED_ACTIVE_IDEA_NOT_FOUND")
        if idea.symbol != inst.symbol or idea.asset_class != inst.asset_class:
            return result("BLOCK", "IDEA_INSTRUMENT_MISMATCH")
        if a.position_side is not None and a.position_side != idea.position_side:
            return result("BLOCK", "IDEA_SIDE_MISMATCH")
        if a.action in (Action.CLOSE, Action.REDUCE):
            qty = idea.quantity
            if a.action == Action.REDUCE:
                fraction = positive_decimal(a.close_fraction)
                if fraction is None or fraction > 1:
                    return result("HOLD", "REDUCTION_FRACTION_UNDEFINED")
                units = Decimal(qty) * fraction
                if units < 1 or units != units.to_integral_value():
                    return result("HOLD", "NON_INTEGER_REDUCTION")
                qty = int(units)
            side = "SELL" if idea.position_side == PositionSide.LONG else "BUY"
            price = q.bid if side == "SELL" else q.ask
            idea.quantity -= qty
            if idea.quantity == 0:
                idea.status = "CLOSED"
            order = PaperOrder(
                order_id=f"paper-{source.source_id}-{source.revision}-{a.action_id}",
                idea_id=idea.idea_id, action_id=a.action_id, action=a.action,
                symbol=inst.symbol, side=side, quantity=qty, price=str(price),
                status="SIMULATED_FILLED",
            )
            return result("SIMULATED", "PAPER_REDUCE_ONLY", order=order)

        if a.action == Action.ADD:
            prior_stop = positive_decimal(idea.stop_price)
            if prior_stop is not None and (
                (idea.position_side == PositionSide.LONG and prior_stop >= q.bid)
                or (idea.position_side == PositionSide.SHORT and prior_stop <= q.ask)
            ):
                return result("HOLD", "EXISTING_STOP_ALREADY_CROSSED")
            ref = positive_decimal(a.entry_reference)
            if ref is None:
                return result("HOLD", "ADD_ENTRY_REFERENCE_MISSING")
            price = q.ask if idea.position_side == PositionSide.LONG else q.bid
            if abs(price - ref) / ref * 100 > self.policy.max_deviation_pct:
                return result("BLOCK", "ENTRY_PRICE_COLLAR")
            if price * (idea.quantity + self.policy.quantity_per_add) > self.policy.max_notional_usd:
                return result("BLOCK", "NOTIONAL_LIMIT")
            idea.quantity += self.policy.quantity_per_add
            order = PaperOrder(
                order_id=f"paper-{source.source_id}-{source.revision}-{a.action_id}",
                idea_id=idea.idea_id, action_id=a.action_id, action=a.action,
                symbol=inst.symbol,
                side="BUY" if idea.position_side == PositionSide.LONG else "SELL",
                quantity=self.policy.quantity_per_add, price=str(price),
                status="SIMULATED_FILLED",
            )
            return result("SIMULATED", "PAPER_ADD", order=order)

        if a.action == Action.MODIFY:
            if a.stop_price is None and a.target_price is None:
                return result("HOLD", "NO_FIELDS_TO_MODIFY")
            stop = positive_decimal(a.stop_price)
            target = positive_decimal(a.target_price)
            if (a.stop_price is not None and stop is None) or (a.target_price is not None and target is None):
                return result("BLOCK", "INVALID_NEW_PROTECTION")
            if stop is not None:
                old_stop = positive_decimal(idea.stop_price)
                if old_stop is not None and (
                    (idea.position_side == PositionSide.LONG and stop < old_stop)
                    or (idea.position_side == PositionSide.SHORT and stop > old_stop)
                ):
                    return result("HOLD", "STOP_WIDENING_REQUIRES_POLICY_REVIEW")
                if (idea.position_side == PositionSide.LONG and stop >= q.bid) or (
                    idea.position_side == PositionSide.SHORT and stop <= q.ask
                ):
                    return result("BLOCK", "STOP_ON_WRONG_SIDE_OF_MARKET")
                idea.stop_price = a.stop_price
            if target is not None:
                if (idea.position_side == PositionSide.LONG and target <= q.bid) or (
                    idea.position_side == PositionSide.SHORT and target >= q.ask
                ):
                    return result("BLOCK", "TARGET_ON_WRONG_SIDE_OF_MARKET")
                idea.target_price = a.target_price
            order = PaperOrder(
                order_id=f"paper-{source.source_id}-{source.revision}-{a.action_id}",
                idea_id=idea.idea_id, action_id=a.action_id, action=a.action,
                symbol=inst.symbol, side="NONE", quantity=0,
                status="SIMULATED_AMENDED",
            )
            return result("SIMULATED", "PAPER_PROTECTION_AMENDMENT", order=order)
        return result("HOLD", "UNKNOWN_ACTION")
