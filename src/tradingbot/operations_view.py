"""Read-only operations read-model over the existing mock ledgers (OPS-01, issue #9).

What this module is
-------------------
A *view* of mock positions, fictional orders/fills, validation outcomes and reconciliation
discrepancies recorded by the two existing mock engines:

* ``storage.Journal`` (Sprint 1): tables ``events`` and ``positions``.
* ``event_replay.PaperEventReplay`` (Sprint 1.1): tables ``replay_posts``, ``replay_actions``
  and ``replay_ideas``.

Every read goes through a SQLite ``mode=ro`` URI connection, so rendering or refreshing a view
*cannot* write to a supplied ledger: SQLite itself refuses with ``attempt to write a readonly
database``. Rows are decoded with the engines' own recorded contracts (``MockOrder``,
``Decision``, ``ActionOutcome``/``PaperOrder``) and freshness is judged against the shared
``MockPolicy``. Source text is never selected, so paid post text can never reach the page.

What this module is not
-----------------------
Not an interpreter, validator, policy, journal, migration or trading control. There is no order
submission path and no network access. ``build_synthetic_ledgers`` only drives the *existing*
pipeline and replay engines with fictional data into a caller-supplied temporary directory;
it is the default input of the dashboard page and never points at the ``data/`` directory.
"""

from __future__ import annotations

import sqlite3
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from typing import Literal

from tradingbot.event_models import (
    ActionOutcome,
    ParsedPost,
    PriceBasis,
    ReplaySource,
    Timing,
    TradeAction,
)
from tradingbot.event_replay import PaperEventReplay
from tradingbot.interpreters import DemoInterpreter
from tradingbot.models import (
    Action,
    AssetClass,
    Decision,
    Instrument,
    MockOrder,
    MockPolicy,
    PositionSide,
    Quote,
    SourceEvent,
    StrictModel,
)
from tradingbot.pipeline import SignalPipeline
from tradingbot.storage import Journal

# Display thresholds of this view. They are NOT trading policy: risk limits live in MockPolicy.
SNAPSHOT_STALE_AFTER_SECONDS = 60
FUTURE_TOLERANCE_SECONDS = 5  # mirrors the gateway's tolerance for clock skew
JOURNAL_EVENT_LIMIT = 1000

Engine = Literal["journal", "replay"]
Freshness = Literal["FRESH", "STALE", "FUTURE", "UNKNOWN"]
ProtectionStatus = Literal["UNKNOWN", "NOT_RECORDED", "RECORDED_MOCK_UNVERIFIED"]
Severity = Literal["WARNING", "SERIOUS"]
Scenario = Literal["empty", "populated"]

PROTECTION_NOTES: dict[str, str] = {
    "UNKNOWN": "The journal ledger does not track stop/target levels; protection is unknown.",
    "NOT_RECORDED": "The ledger tracks protection levels but none is recorded for this idea.",
    "RECORDED_MOCK_UNVERIFIED": (
        "A level is recorded in the mock ledger only; no broker-native protection exists or was verified."
    ),
}
# Outcomes that an operator must look at: nothing was filled and nothing was finally rejected.
NEEDS_REVIEW_STATUSES = frozenset({"RECEIVED", "ERROR", "REVIEW", "HOLD", "RECONCILE"})

RECONCILIATION_CHECKS = [
    "LEDGER_FILE_MISSING / LEDGER_UNREADABLE: each configured ledger file opens read-only and has the engine's tables",
    "JOURNAL_RESERVATION_INCOMPLETE: no event is still RECEIVED (reserved but never completed)",
    "JOURNAL_HISTORY_TRUNCATED: every journal event was read, so fill totals are complete",
    (
        "JOURNAL_POSITION_QUANTITY_MISMATCH / JOURNAL_FILL_WITHOUT_POSITION / JOURNAL_POSITION_WITHOUT_FILL: "
        "journal position quantities equal the signed sum of recorded fictional fills per idea"
    ),
    (
        "REPLAY_IDEA_QUANTITY_MISMATCH / REPLAY_FILL_WITHOUT_IDEA / REPLAY_IDEA_WITHOUT_FILL: replay idea "
        "quantities equal the signed sum of recorded paper fills per idea (latest evaluation attempt of each post)"
    ),
    "REPLAY_IDEA_STATUS_INCONSISTENT: OPEN ideas hold quantity > 0 and CLOSED ideas hold 0",
    "REPLAY_HISTORICAL_REPORT_UNRECONCILED: no RECONCILE outcome (a reported past action without an order) is pending",
]


# ----------------------------------------------------------------------------- view contracts


class MockLedgers(StrictModel):
    """Where one Journal database and one PaperEventReplay database live. Either may be absent."""

    journal_path: Path | None = None
    replay_path: Path | None = None
    label: str = "unspecified"

    @classmethod
    def from_engines(
        cls,
        journal: Journal | None = None,
        replay: PaperEventReplay | None = None,
        label: str = "supplied",
    ) -> MockLedgers:
        return cls(
            journal_path=Path(journal.path) if journal else None,
            replay_path=Path(replay.db_path) if replay else None,
            label=label,
        )


class PositionRow(StrictModel):
    engine: Engine
    idea_id: str
    symbol: str
    asset_class: str
    side: str
    quantity: int
    idea_status: str
    stop_price: str | None = None
    target_price: str | None = None
    protection: ProtectionStatus
    protection_note: str


class FillRow(StrictModel):
    engine: Engine
    order_id: str
    source_id: str
    idea_id: str
    action: str
    symbol: str
    side: str
    quantity: int
    price: str | None = None
    status: str
    recorded_at: datetime | None = None
    recorded_at_basis: str


class ValidationRow(StrictModel):
    engine: Engine
    source_id: str
    revision: int | None = None
    action_id: str | None = None
    status: str
    reasons: list[str]
    published_at: datetime | None = None
    source_age_seconds: float | None = None
    source_freshness: Freshness
    needs_review: bool


class Discrepancy(StrictModel):
    engine: Engine | Literal["view"]
    code: str
    severity: Severity
    subject: str
    detail: str


class LedgerActivity(StrictModel):
    engine: Engine
    path: str
    readable: bool
    rows_on_record: int
    rows_read: int
    history_truncated: bool
    last_write_at: datetime | None = None
    last_write_basis: str
    seconds_before_as_of: float | None = None


class OperationsSnapshot(StrictModel):
    """Everything the page renders, read at ``as_of`` without touching the ledgers."""

    as_of: datetime
    ledgers: MockLedgers
    signal_window_seconds: int
    activity: list[LedgerActivity]
    positions: list[PositionRow]
    fills: list[FillRow]
    validations: list[ValidationRow]
    discrepancies: list[Discrepancy]
    checks_performed: list[str]
    reconciliation_complete: bool

    @property
    def open_positions(self) -> list[PositionRow]:
        return [p for p in self.positions if p.idea_status == "OPEN" and p.quantity > 0]

    @property
    def closed_ideas(self) -> list[PositionRow]:
        return [p for p in self.positions if not (p.idea_status == "OPEN" and p.quantity > 0)]

    @property
    def is_empty(self) -> bool:
        return not (self.positions or self.fills or self.validations)

    def counts(self) -> dict[str, int]:
        return {
            "open_positions": len(self.open_positions),
            "fills": len(self.fills),
            "validations": len(self.validations),
            "needs_review": sum(v.needs_review for v in self.validations),
            "serious_discrepancies": sum(d.severity == "SERIOUS" for d in self.discrepancies),
            "warning_discrepancies": sum(d.severity == "WARNING" for d in self.discrepancies),
        }


# ----------------------------------------------------------------------------- read-only access


@contextmanager
def read_only_connection(path: str | Path) -> Iterator[sqlite3.Connection]:
    """A connection that SQLite itself refuses to write through (``mode=ro``). Always closed."""
    conn = sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro", uri=True, timeout=10.0)
    try:
        conn.row_factory = sqlite3.Row
        yield conn
    finally:
        conn.close()


def _parse_ts(value: object) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _age_seconds(ts: datetime | None, now: datetime) -> float | None:
    """Seconds between ``ts`` and ``now``; None when either side has no timezone (not comparable)."""
    if ts is None or ts.tzinfo is None or now.tzinfo is None:
        return None
    return (now - ts).total_seconds()


def freshness(age_seconds: float | None, window_seconds: float) -> Freshness:
    if age_seconds is None:
        return "UNKNOWN"
    if age_seconds < -FUTURE_TOLERANCE_SECONDS:
        return "FUTURE"
    if age_seconds > window_seconds:
        return "STALE"
    return "FRESH"


def snapshot_freshness(
    snapshot: OperationsSnapshot, now: datetime | None = None
) -> tuple[float | None, Freshness]:
    """How old the snapshot is at render time, judged against SNAPSHOT_STALE_AFTER_SECONDS."""
    age = _age_seconds(snapshot.as_of, now or datetime.now(timezone.utc))
    return age, freshness(age, SNAPSHOT_STALE_AFTER_SECONDS)


def _validation(
    engine: Engine,
    source_id: str,
    status: str,
    reasons: list[str],
    published_at: datetime | None,
    now: datetime,
    policy: MockPolicy,
    *,
    revision: int | None = None,
    action_id: str | None = None,
) -> ValidationRow:
    age = _age_seconds(published_at, now)
    return ValidationRow(
        engine=engine,
        source_id=source_id,
        revision=revision,
        action_id=action_id,
        status=status,
        reasons=reasons,
        published_at=published_at,
        source_age_seconds=age,
        source_freshness=freshness(age, policy.max_signal_age_seconds),
        needs_review=status in NEEDS_REVIEW_STATUSES,
    )


def _signed_units(action: str, quantity: int) -> int:
    if action in (Action.OPEN.value, Action.ADD.value):
        return quantity
    if action in (Action.CLOSE.value, Action.REDUCE.value):
        return -quantity
    return 0


def _reconcile_quantities(
    engine: Engine,
    expected: dict[str, int],
    actual: dict[str, int],
    opened: set[str],
    codes: tuple[str, str, str],
) -> list[Discrepancy]:
    mismatch, fill_without_position, position_without_fill = codes
    found: list[Discrepancy] = []
    for idea_id in sorted(set(expected) | set(actual)):
        exp, act = expected.get(idea_id, 0), actual.get(idea_id)
        if act is None:
            if exp != 0:
                found.append(
                    Discrepancy(
                        engine=engine,
                        code=fill_without_position,
                        severity="SERIOUS",
                        subject=idea_id,
                        detail=f"Recorded fills net to {exp} units but the ledger holds no row for this idea.",
                    )
                )
            continue
        if idea_id not in opened:
            found.append(
                Discrepancy(
                    engine=engine,
                    code=position_without_fill,
                    severity="SERIOUS",
                    subject=idea_id,
                    detail=f"The ledger holds {act} units but no OPEN fill is recorded for this idea.",
                )
            )
        elif exp != act:
            found.append(
                Discrepancy(
                    engine=engine,
                    code=mismatch,
                    severity="SERIOUS",
                    subject=idea_id,
                    detail=f"Ledger quantity is {act} but recorded fills net to {exp} units.",
                )
            )
    return found


# ----------------------------------------------------------------------------- journal (Sprint 1)


def _read_journal(
    path: Path, now: datetime, policy: MockPolicy, limit: int
) -> tuple[
    LedgerActivity, list[PositionRow], list[FillRow], list[ValidationRow], list[Discrepancy]
]:
    positions: list[PositionRow] = []
    fills: list[FillRow] = []
    validations: list[ValidationRow] = []
    found: list[Discrepancy] = []
    try:
        with read_only_connection(path) as c:
            total = c.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            last_write = _parse_ts(c.execute("SELECT MAX(created_at) FROM events").fetchone()[0])
            # Same shape as Journal.recent_events(), minus source_text: the view never shows post text.
            events = c.execute(
                """SELECT source_id, status, published_at, created_at, decision_json, order_json
                FROM events ORDER BY created_at DESC, rowid DESC LIMIT ?""",
                (limit,),
            ).fetchall()
            position_rows = c.execute(
                "SELECT idea_id, symbol, asset_class, side, quantity FROM positions ORDER BY idea_id"
            ).fetchall()
    except sqlite3.OperationalError as exc:
        code = "LEDGER_FILE_MISSING" if not path.exists() else "LEDGER_UNREADABLE"
        found.append(
            Discrepancy(
                engine="journal",
                code=code,
                severity="SERIOUS",
                subject=str(path),
                detail=f"Journal ledger could not be read: {exc}",
            )
        )
        activity = LedgerActivity(
            engine="journal",
            path=str(path),
            readable=False,
            rows_on_record=0,
            rows_read=0,
            history_truncated=False,
            last_write_basis="events.created_at (ledger write time)",
        )
        return activity, positions, fills, validations, found

    events = list(reversed(events))  # chronological for display
    truncated = total > len(events)
    activity = LedgerActivity(
        engine="journal",
        path=str(path),
        readable=True,
        rows_on_record=total,
        rows_read=len(events),
        history_truncated=truncated,
        last_write_at=last_write,
        last_write_basis="events.created_at (ledger write time)",
        seconds_before_as_of=_age_seconds(last_write, now),
    )
    expected: dict[str, int] = {}
    opened: set[str] = set()
    for r in events:
        decision = Decision.model_validate_json(r["decision_json"]) if r["decision_json"] else None
        order = MockOrder.model_validate_json(r["order_json"]) if r["order_json"] else None
        published_at = _parse_ts(r["published_at"])
        validations.append(
            _validation(
                "journal",
                r["source_id"],
                r["status"],
                list(decision.reasons) if decision else [],
                published_at,
                now,
                policy,
            )
        )
        if r["status"] == "RECEIVED":
            found.append(
                Discrepancy(
                    engine="journal",
                    code="JOURNAL_RESERVATION_INCOMPLETE",
                    severity="WARNING",
                    subject=r["source_id"],
                    detail="Source was reserved but never completed: no decision or fill is recorded and the "
                    "duplicate guard will refuse a retry under the same source_id.",
                )
            )
        if order is not None:
            fills.append(
                FillRow(
                    engine="journal",
                    order_id=order.order_id,
                    source_id=order.source_id,
                    idea_id=order.idea_id,
                    action=order.action.value,
                    symbol=order.symbol,
                    side=order.side,
                    quantity=order.quantity,
                    price=order.mock_fill_price,
                    status=order.status,
                    recorded_at=_parse_ts(r["created_at"]),
                    recorded_at_basis="events.created_at (ledger write time)",
                )
            )
            expected[order.idea_id] = expected.get(order.idea_id, 0) + _signed_units(
                order.action.value, order.quantity
            )
            if order.action == Action.OPEN:
                opened.add(order.idea_id)
    for p in position_rows:
        positions.append(
            PositionRow(
                engine="journal",
                idea_id=p["idea_id"],
                symbol=p["symbol"],
                asset_class=p["asset_class"],
                side=p["side"],
                quantity=p["quantity"],
                idea_status="OPEN",
                protection="UNKNOWN",
                protection_note=PROTECTION_NOTES["UNKNOWN"],
            )
        )
    if truncated:
        found.append(
            Discrepancy(
                engine="journal",
                code="JOURNAL_HISTORY_TRUNCATED",
                severity="WARNING",
                subject=str(path),
                detail=f"Only the latest {len(events)} of {total} events were read; fill totals were not reconciled.",
            )
        )
    else:
        found.extend(
            _reconcile_quantities(
                "journal",
                expected,
                {p["idea_id"]: p["quantity"] for p in position_rows},
                opened,
                (
                    "JOURNAL_POSITION_QUANTITY_MISMATCH",
                    "JOURNAL_FILL_WITHOUT_POSITION",
                    "JOURNAL_POSITION_WITHOUT_FILL",
                ),
            )
        )
    return activity, positions, fills, validations, found


# ----------------------------------------------------------------------------- replay (Sprint 1.1)


def _read_replay(
    path: Path, now: datetime, policy: MockPolicy
) -> tuple[
    LedgerActivity, list[PositionRow], list[FillRow], list[ValidationRow], list[Discrepancy]
]:
    positions: list[PositionRow] = []
    fills: list[FillRow] = []
    validations: list[ValidationRow] = []
    found: list[Discrepancy] = []
    basis = "replay_posts.processed_at (ledger write time)"
    try:
        with read_only_connection(path) as c:
            posts = c.execute(
                """SELECT source_id, revision, complete_text, hydrated_from_hash, published_at, received_at,
                processed_at FROM replay_posts ORDER BY processed_at, rowid"""
            ).fetchall()
            # Post text is never stored by the engine; post_hash is deliberately not selected either.
            action_rows = c.execute(
                "SELECT source_id, revision, attempt, action_id, result_json FROM replay_actions ORDER BY rowid"
            ).fetchall()
            idea_rows = c.execute(
                """SELECT idea_id, symbol, asset_class, position_side, quantity, stop_price, target_price, status
                FROM replay_ideas ORDER BY idea_id"""
            ).fetchall()
    except sqlite3.OperationalError as exc:
        code = "LEDGER_FILE_MISSING" if not path.exists() else "LEDGER_UNREADABLE"
        found.append(
            Discrepancy(
                engine="replay",
                code=code,
                severity="SERIOUS",
                subject=str(path),
                detail=f"Replay ledger could not be read: {exc}",
            )
        )
        activity = LedgerActivity(
            engine="replay",
            path=str(path),
            readable=False,
            rows_on_record=0,
            rows_read=0,
            history_truncated=False,
            last_write_basis=basis,
        )
        return activity, positions, fills, validations, found

    last_write = max(
        (t for t in (_parse_ts(p["processed_at"]) for p in posts) if t is not None), default=None
    )
    activity = LedgerActivity(
        engine="replay",
        path=str(path),
        readable=True,
        rows_on_record=len(posts),
        rows_read=len(posts),
        history_truncated=False,
        last_write_at=last_write,
        last_write_basis=basis,
        seconds_before_as_of=_age_seconds(last_write, now),
    )
    # Latest evaluation attempt per post revision, exactly as PaperEventReplay.outcomes() reports it.
    latest_attempt: dict[tuple[str, int], int] = {}
    for r in action_rows:
        key = (r["source_id"], r["revision"])
        latest_attempt[key] = max(latest_attempt.get(key, 0), r["attempt"])
    outcomes: dict[tuple[str, int], list[ActionOutcome]] = {}
    for r in action_rows:
        key = (r["source_id"], r["revision"])
        if r["attempt"] == latest_attempt[key]:
            outcomes.setdefault(key, []).append(ActionOutcome.model_validate_json(r["result_json"]))

    expected: dict[str, int] = {}
    opened: set[str] = set()
    for p in posts:
        key = (p["source_id"], p["revision"])
        published_at = _parse_ts(p["published_at"])
        processed_at = _parse_ts(p["processed_at"])
        for o in outcomes.get(key, []):
            validations.append(
                _validation(
                    "replay",
                    p["source_id"],
                    o.status,
                    list(o.reasons),
                    published_at,
                    now,
                    policy,
                    revision=p["revision"],
                    action_id=o.action_id,
                )
            )
            if o.status == "RECONCILE":
                found.append(
                    Discrepancy(
                        engine="replay",
                        code="REPLAY_HISTORICAL_REPORT_UNRECONCILED",
                        severity="WARNING",
                        subject=f"{p['source_id']} r{p['revision']} / {o.action_id}",
                        detail="The source reported a past action that this ledger did not execute "
                        f"({', '.join(o.reasons) or 'no reason recorded'}); compare it manually with the "
                        "referenced idea before trusting the position.",
                    )
                )
            if o.order is not None:
                order = o.order
                fills.append(
                    FillRow(
                        engine="replay",
                        order_id=order.order_id,
                        source_id=p["source_id"],
                        idea_id=order.idea_id,
                        action=order.action.value,
                        symbol=order.symbol,
                        side=order.side,
                        quantity=order.quantity,
                        price=order.price,
                        status=order.status,
                        recorded_at=processed_at,
                        recorded_at_basis=basis,
                    )
                )
                expected[order.idea_id] = expected.get(order.idea_id, 0) + _signed_units(
                    order.action.value, order.quantity
                )
                if order.action == Action.OPEN:
                    opened.add(order.idea_id)
    for i in idea_rows:
        has_stop = bool(i["stop_price"] or i["target_price"])
        protection: ProtectionStatus = "RECORDED_MOCK_UNVERIFIED" if has_stop else "NOT_RECORDED"
        positions.append(
            PositionRow(
                engine="replay",
                idea_id=i["idea_id"],
                symbol=i["symbol"],
                asset_class=i["asset_class"],
                side=i["position_side"],
                quantity=i["quantity"],
                idea_status=i["status"],
                stop_price=i["stop_price"],
                target_price=i["target_price"],
                protection=protection,
                protection_note=PROTECTION_NOTES[protection],
            )
        )
        if (i["status"] == "OPEN") != (i["quantity"] > 0):
            found.append(
                Discrepancy(
                    engine="replay",
                    code="REPLAY_IDEA_STATUS_INCONSISTENT",
                    severity="SERIOUS",
                    subject=i["idea_id"],
                    detail=f"Idea status is {i['status']} while quantity is {i['quantity']}.",
                )
            )
    found.extend(
        _reconcile_quantities(
            "replay",
            expected,
            {i["idea_id"]: i["quantity"] for i in idea_rows},
            opened,
            (
                "REPLAY_IDEA_QUANTITY_MISMATCH",
                "REPLAY_FILL_WITHOUT_IDEA",
                "REPLAY_IDEA_WITHOUT_FILL",
            ),
        )
    )
    return activity, positions, fills, validations, found


# ----------------------------------------------------------------------------- snapshot


def build_snapshot(
    ledgers: MockLedgers,
    *,
    now: datetime | None = None,
    policy: MockPolicy | None = None,
    journal_event_limit: int = JOURNAL_EVENT_LIMIT,
) -> OperationsSnapshot:
    """Read both ledgers once, through read-only connections, as of ``now``."""
    now = now or datetime.now(timezone.utc)
    policy = policy or MockPolicy()
    activity: list[LedgerActivity] = []
    positions: list[PositionRow] = []
    fills: list[FillRow] = []
    validations: list[ValidationRow] = []
    discrepancies: list[Discrepancy] = []
    if ledgers.journal_path is not None:
        act, pos, fil, val, dis = _read_journal(
            Path(ledgers.journal_path), now, policy, journal_event_limit
        )
        activity.append(act)
        positions += pos
        fills += fil
        validations += val
        discrepancies += dis
    if ledgers.replay_path is not None:
        act, pos, fil, val, dis = _read_replay(Path(ledgers.replay_path), now, policy)
        activity.append(act)
        positions += pos
        fills += fil
        validations += val
        discrepancies += dis
    complete = all(a.readable and not a.history_truncated for a in activity)
    return OperationsSnapshot(
        as_of=now,
        ledgers=ledgers,
        signal_window_seconds=policy.max_signal_age_seconds,
        activity=activity,
        positions=positions,
        fills=fills,
        validations=validations,
        discrepancies=discrepancies,
        checks_performed=list(RECONCILIATION_CHECKS),
        reconciliation_complete=complete,
    )


# ----------------------------------------------------------------------------- synthetic fixtures


def new_fixture_directory() -> Path:
    """A fresh task-local temporary directory; never the repository's data/ directory."""
    return Path(tempfile.mkdtemp(prefix="tradingbot-mock-ops-"))


def _quote(symbol: str, bid: str, ask: str, observed_at: datetime) -> Quote:
    return Quote(symbol=symbol, bid=Decimal(bid), ask=Decimal(ask), observed_at=observed_at)


def _replay_source(sid: str, now: datetime, **overrides: object) -> ReplaySource:
    kw: dict[str, object] = {
        "source_id": sid,
        "published_at": now,
        "received_at": now,
        "verified_author": True,
        "complete_text": True,
        "post_hash": sha256(f"SYNTHETIC:{sid}".encode()).hexdigest(),
    }
    kw.update(overrides)
    return ReplaySource(**kw)  # type: ignore[arg-type]


def _replay_action(
    aid: str,
    instruction: Action,
    symbol: str,
    cls: AssetClass,
    *,
    timing: Timing = Timing.CURRENT,
    side: PositionSide | None = None,
    idea: str | None = None,
    ref: str | None = None,
    stop: str | None = None,
    target: str | None = None,
    fraction: str | None = None,
    group: str | None = None,
) -> TradeAction:
    return TradeAction(
        action_id=aid,
        action=instruction,
        timing=timing,
        instrument=Instrument(symbol=symbol, asset_class=cls),
        position_side=side,
        idea_reference=idea,
        entry_reference=ref,
        stop_price=stop,
        target_price=target,
        close_fraction=fraction,
        atomic_group=group,
        price_basis=PriceBasis.TRADED_CONTRACT,
        source_market_symbol=symbol,
        explicit_instruction=True,
        evidence=["SYNTHETIC_FIXTURE"],
    )


def build_synthetic_ledgers(
    directory: str | Path, scenario: Scenario = "populated", *, now: datetime | None = None
) -> MockLedgers:
    """Create fictional ledgers in ``directory`` by driving the existing engines; return their paths.

    ``empty`` creates both schemas with no rows. ``populated`` records fills, rejections, holds, an
    error, an incomplete reservation and a historical report so that every section of the view has
    something to show. All symbols (MOCKA..MOCKE) and texts are invented for this fixture.
    """
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    policy = MockPolicy(
        quantity=2, quantity_per_add=2
    )  # even sizes so a 50% reduction is an integer
    journal = Journal(directory / "synthetic_journal.sqlite3")
    replay = PaperEventReplay(directory / "synthetic_replay.sqlite3", policy)
    ledgers = MockLedgers.from_engines(journal, replay, label=f"synthetic:{scenario}")
    if scenario == "empty":
        return ledgers

    # --- Journal: freshness is wall-clock relative inside the pipeline, so these use the real clock.
    clock = datetime.now(timezone.utc)
    pipe = SignalPipeline(journal, DemoInterpreter(), policy)

    def event(sid: str, text: str, age: int = 0) -> SourceEvent:
        return SourceEvent(source_id=sid, text=text, published_at=clock - timedelta(seconds=age))

    pipe.process(
        event("syn-j-open-1", "BUY MOCKA @ 100 STOP 95 TARGET 110"),
        _quote("MOCKA", "99.95", "100.05", clock),
    )
    pipe.process(
        event("syn-j-reduce-1", "REDUCE 50% MOCKA IDEA syn-j-open-1"),
        _quote("MOCKA", "101.00", "101.10", clock),
    )
    pipe.process(
        event("syn-j-open-2", "SHORT MOCKB @ 50 STOP 55 TARGET 40"),
        _quote("MOCKB", "49.95", "50.05", clock),
    )
    pipe.process(
        event("syn-j-close-2", "CLOSE MOCKB IDEA syn-j-open-2"),
        _quote("MOCKB", "48.00", "48.10", clock),
    )
    pipe.process(
        event("syn-j-commentary", "We remain constructive on MOCKA with support near 95."),
        _quote("MOCKA", "99.95", "100.05", clock),
    )
    pipe.process(
        event("syn-j-stale", "BUY MOCKC @ 20 STOP 19 TARGET 22", age=300),
        _quote("MOCKC", "19.99", "20.01", clock),
    )
    pipe.process(
        event("syn-j-orphan-ref", "CLOSE MOCKC IDEA does-not-exist"),
        _quote("MOCKC", "19.99", "20.01", clock),
    )
    pipe.process(
        event("syn-j-open-1", "BUY MOCKA @ 100 STOP 95 TARGET 110"),
        _quote("MOCKA", "99.95", "100.05", clock),
    )
    journal.reserve(event("syn-j-error", "BUY MOCKA @ 100 STOP 95 TARGET 110"))
    journal.mark_error("syn-j-error", "SyntheticInterpreterFault: injected by the fixture")
    journal.reserve(
        event("syn-j-inflight", "BUY MOCKA @ 100 STOP 95 TARGET 110")
    )  # never completed

    # --- Replay: deterministic with an explicit clock.
    now = now or clock
    quotes = {
        "MOCKD": _quote("MOCKD", "99.95", "100.05", now),
        "MOCKE": _quote("MOCKE", "49.95", "50.05", now),
    }

    def run(
        sid: str,
        *actions: TradeAction,
        quotes_in: dict[str, Quote] | None = None,
        **source_overrides: object,
    ):
        return replay.process(
            _replay_source(sid, now, **source_overrides),
            ParsedPost(actions=list(actions)),
            quotes=quotes_in or quotes,
            now=now,
        )

    run(
        "syn-r-1",
        _replay_action(
            "open",
            Action.OPEN,
            "MOCKD",
            AssetClass.ETF,
            side=PositionSide.LONG,
            ref="100",
            stop="95",
            target="110",
        ),
    )
    run(
        "syn-r-2",
        _replay_action(
            "add", Action.ADD, "MOCKD", AssetClass.ETF, idea="syn-r-1:open", ref="100", group="g1"
        ),
        _replay_action(
            "tighten",
            Action.MODIFY,
            "MOCKD",
            AssetClass.ETF,
            idea="syn-r-1:open",
            stop="97",
            group="g1",
        ),
    )
    run(
        "syn-r-3",
        _replay_action(
            "trim", Action.REDUCE, "MOCKD", AssetClass.ETF, idea="syn-r-1:open", fraction="0.5"
        ),
    )
    run(
        "syn-r-4",
        _replay_action(
            "open",
            Action.OPEN,
            "MOCKE",
            AssetClass.EQUITY,
            side=PositionSide.SHORT,
            ref="50",
            stop="55",
            target="40",
        ),
    )
    run(
        "syn-r-5",
        _replay_action("cover", Action.CLOSE, "MOCKE", AssetClass.EQUITY, idea="syn-r-4:open"),
    )
    run(
        "syn-r-6",
        _replay_action(
            "report",
            Action.CLOSE,
            "MOCKD",
            AssetClass.ETF,
            idea="syn-r-1:open",
            timing=Timing.HISTORICAL,
        ),
    )
    run(
        "syn-r-7",
        _replay_action(
            "maybe",
            Action.OPEN,
            "MOCKE",
            AssetClass.EQUITY,
            side=PositionSide.LONG,
            ref="50",
            stop="48",
            target="55",
            timing=Timing.CONDITIONAL,
        ),
    )
    run(
        "syn-r-8",
        _replay_action(
            "open",
            Action.OPEN,
            "MOCKE",
            AssetClass.EQUITY,
            side=PositionSide.LONG,
            ref="50",
            stop="48",
            target="55",
        ),
        verified_author=False,
    )
    run(
        "syn-r-9",
        _replay_action(
            "open",
            Action.OPEN,
            "MOCKE",
            AssetClass.EQUITY,
            side=PositionSide.LONG,
            ref="50",
            stop="48",
            target="55",
        ),
        is_repost=True,
    )
    run(
        "syn-r-10",
        _replay_action(
            "open",
            Action.OPEN,
            "MOCKE",
            AssetClass.EQUITY,
            side=PositionSide.LONG,
            ref="50",
            stop="48",
            target="55",
        ),
        complete_text=False,
    )
    run(
        "syn-r-11",
        _replay_action("exit", Action.CLOSE, "MOCKD", AssetClass.ETF, idea="syn-r-1:open"),
        quotes_in={"MOCKD": _quote("MOCKD", "99.95", "100.05", now - timedelta(seconds=120))},
    )
    run(
        "syn-r-3",
        _replay_action(
            "trim", Action.REDUCE, "MOCKD", AssetClass.ETF, idea="syn-r-1:open", fraction="0.5"
        ),
    )
    return ledgers
