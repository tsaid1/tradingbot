"""SHADOW-02: bounded SYNTHETIC multi-cycle soak and repeated-process-restart proof. MOCK ONLY.

NOT PRODUCTION. SHADOW-01 (``shadow_runner``) proves every single-run boundary of a 17-delivery
synthetic fixture. This module closes its documented scale / repeated-restart gap and nothing else:

* the public SHADOW-01 fixture is repeated for ``cycles`` cycles with **disjoint source identities**
  (``c000-e01``, ``c001-e01``, ...) on a coherent virtual timeline (one fixed virtual period per
  cycle), so the coordinator's once-only key ``(source_id, revision, post_hash)`` sees a genuinely
  new post in every cycle while every duplicate, late, post-close, blocked and tampered re-delivery
  of the fixture is preserved verbatim inside each cycle;
* an **uninterrupted reference process** and a **chain of fresh OS processes** sharing one
  persistent mock ledger (seeded pause/resume, before-commit and after-commit interruptions, each
  under a different ``PYTHONHASHSEED``) must converge on the same ledger-derived orders, positions,
  outcomes, counts and digest;
* the final ledgers are measured **independently** (read-only SQLite, not the runner's own report)
  against expected per-cycle fill / reduce / close counts and final quantities derived from the
  SHADOW-01 specification, so digest equality alone can never carry a PASS.

Everything that decides is reused unchanged: ``ShadowRunner`` (delivery loop, virtual clock,
checkpoint), ``TklInterpreter``, ``validator`` gates inside ``PaperEventReplay`` and ``MockPolicy``.
No interpreter, validator, policy, journal, adapter or parsing rule is added here.

Bounds are explicit: cycles, restarts, a per-process wall budget and a total wall budget. Every
process launch is capped by the per-process bound AND by what remains of the total budget, nothing
is launched once the total is exhausted, and the total deadline is re-checked after the final
segment and the measurement: a run stopped or finished late by a wall budget is reported as
``INCOMPLETE``, never ``PASS``. Wall time is measured and
reported separately from virtual time; nothing here is an uptime or throughput claim. Only fresh
(absent or empty) directories are used; nothing on the host is scanned for ledgers. No network
socket may be opened while a soak runs (``no_network``). The evidence is synthetic and offline:
not broker, live-source, real-clock, performance or production-readiness evidence.
"""
from __future__ import annotations

import argparse
import os
import random
import re
import socket
import sqlite3
import subprocess
import sys
import time
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import Field, ValidationError

import tradingbot
from tradingbot.event_models import ActionOutcome
from tradingbot.models import StrictModel
from tradingbot.shadow_runner import (
    CHECKPOINT_FILE,
    LEDGER_FILE,
    InterruptSpec,
    Phase,
    ShadowCheckpoint,
    ShadowFeed,
    ShadowInterrupted,
    ShadowRunner,
    ShadowRunReport,
    ShadowStateError,
    policy_digest,
    shadow_policy,
    synthetic_feed,
)

SOAK_LABEL = (
    "SYNTHETIC SOAK - MOCK ONLY - OFFLINE RESTART EVIDENCE - NOT BROKER, LIVE-SOURCE, REAL-CLOCK, "
    "PERFORMANCE OR PRODUCTION-READINESS EVIDENCE"
)
REPORT_SCHEMA = "shadow-soak-report-1"
SOAK_REPORT_FILE = "soak_report.json"
REFERENCE_DIR, CHAIN_DIR, SEGMENTS_DIR = "reference", "chain", "segments"
DEFAULT_CYCLE_PERIOD_S = 12 * 3600
SOURCE_IDENTITY_SCHEME = "c{cycle:03d}-{base_key}"
MAX_CYCLES = 256
MAX_RESTARTS = 256
MAX_WALL_SECONDS_PER_PROCESS = 600.0
MAX_TOTAL_WALL_SECONDS = 1800.0
HASH_SEED_RANGE = range(1, 2**32)
# Segment (child process) exit codes, aligned with scripts/run_shadow_synthetic.py.
EXIT_OK, EXIT_REFUSED, EXIT_INTERRUPTED, EXIT_PAUSED = 0, 2, 3, 4

Verdict = Literal["PASS", "FAIL", "INCOMPLETE"]
Role = Literal["reference", "chain"]
Planned = Literal["complete", "pause", "interrupt_before_commit", "interrupt_after_commit"]
SegmentStatus = Literal["COMPLETE", "DELIVERY_CAP_REACHED", "WALL_BUDGET_EXHAUSTED", "INTERRUPTED",
                        "REFUSED", "ERROR"]
_CYCLE_RE = re.compile(r"^c(\d{3})-")


class SoakStateError(RuntimeError):
    """A refusal: occupied root directory, or a soak that cannot be planned within its bounds."""


class NetworkRefused(RuntimeError):
    """Raised by the no-network guard if anything tries to open a socket during a soak."""


# ----------------------------------------------------------------------------- no-network guard

_GUARDED = ("socket", "create_connection", "getaddrinfo", "socketpair")


def _refuse_network(*_args, **_kwargs):
    raise NetworkRefused("the synthetic soak must not open network sockets")


def network_guard_active() -> bool:
    return socket.socket is _refuse_network


@contextmanager
def no_network() -> Iterator[None]:
    """Replace the socket constructors with a refusing stub for the duration of the block."""
    originals = {name: getattr(socket, name) for name in _GUARDED}
    for name in _GUARDED:
        setattr(socket, name, _refuse_network)
    try:
        yield
    finally:
        for name, original in originals.items():
            setattr(socket, name, original)


# ----------------------------------------------------------------------------- profile contract

class SoakProfile(StrictModel):
    """Explicit bounds of one soak. Every limit is a hard cap, not a target."""
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,31}$")
    cycles: int = Field(ge=1, le=MAX_CYCLES)
    restarts: int = Field(ge=0, le=MAX_RESTARTS)
    seed: int = Field(ge=0, le=2**32 - 1)
    cycle_period_s: int = Field(default=DEFAULT_CYCLE_PERIOD_S, ge=1)
    max_wall_seconds_per_process: float = Field(default=120.0, gt=0, le=MAX_WALL_SECONDS_PER_PROCESS)
    max_total_wall_seconds: float = Field(default=900.0, gt=0, le=MAX_TOTAL_WALL_SECONDS)


PROFILES: dict[str, SoakProfile] = {
    # Fast CI profile: 3 cycles = 51 deliveries, 5 restarts (7 processes including the reference).
    "ci": SoakProfile(name="ci", cycles=3, restarts=5, seed=20261106),
    # Bounded representative local profile: 64 cycles = 1088 deliveries, 24 restarts (26 processes).
    "representative": SoakProfile(name="representative", cycles=64, restarts=24, seed=20261106),
}


# ----------------------------------------------------------------------------- multi-cycle feed

def cycle_prefix(cycle: int) -> str:
    return SOURCE_IDENTITY_SCHEME.format(cycle=cycle, base_key="")


def cycle_of(identifier: str) -> int | None:
    """Cycle number encoded in a soak source id / idea id / post key, or None."""
    match = _CYCLE_RE.match(identifier)
    return int(match.group(1)) if match else None


def base_span_s(base: ShadowFeed) -> int:
    """Virtual seconds between the base feed's epoch and its last delivery or post availability."""
    return max(max(d.offset_s for d in base.deliveries), max(p.available_offset_s for p in base.posts))


def build_soak_feed(cycles: int, *, cycle_period_s: int = DEFAULT_CYCLE_PERIOD_S,
                    base: ShadowFeed | None = None) -> ShadowFeed:
    """Repeat the public SHADOW-01 fixture ``cycles`` times with disjoint source identities.

    Cycle ``c`` shifts every publication, revision and delivery offset by ``c * cycle_period_s``
    and prefixes every post key and source id with ``c{c:03d}-``. Text, quotes, revisions,
    duplicate/late/post-close re-deliveries and the tampered ``text_override`` are copied verbatim,
    so parser semantics, references and the existing policy are untouched. The period must exceed
    the base span so cycles never overlap on the virtual timeline.
    """
    base = base or synthetic_feed()
    if not 1 <= cycles <= MAX_CYCLES:
        raise ValueError(f"cycles must be within 1..{MAX_CYCLES}, got {cycles}")
    span = base_span_s(base)
    if cycle_period_s <= span:
        raise ValueError(f"cycle_period_s must exceed the base feed span of {span}s so cycles never overlap")
    posts: list[dict] = []
    deliveries: list[dict] = []
    for cycle in range(cycles):
        prefix, shift = cycle_prefix(cycle), cycle * cycle_period_s
        for post in base.posts:
            data = post.model_dump()
            data["key"] = prefix + data["key"]
            data["source_id"] = prefix + data["source_id"]
            data["published_offset_s"] += shift
            if data["revised_offset_s"] is not None:
                data["revised_offset_s"] += shift
            posts.append(data)
        for delivery in base.deliveries:
            data = delivery.model_dump()
            data["post_key"] = prefix + data["post_key"]
            data["offset_s"] += shift
            deliveries.append(data)
    feed_id = f"shadow-02-soak-{cycles:03d}c-{cycle_period_s}s-{base.feed_id}"
    return ShadowFeed.model_validate({"feed_id": feed_id, "epoch": base.epoch, "half_spread": base.half_spread,
                                      "posts": posts, "deliveries": deliveries})


def prefix_unique_counts(feed: ShadowFeed) -> list[int]:
    """``result[i]`` = number of unique post versions among ``feed.deliveries[:i]``."""
    seen: set[tuple[str, int]] = set()
    counts = [0]
    for delivery in feed.deliveries:
        post = feed.post(delivery.post_key)
        seen.add((post.source_id, post.revision))
        counts.append(len(seen))
    return counts


# ----------------------------------------------------------------------------- independent expectations
#
# Derived from the SHADOW-01 specification (docs/SHADOW_01_SYNTHETIC_SHADOW_RUNNER.md), NOT from any run
# of this module. The tests restate the key numbers a second time so the comparison is never circular.

DELIVERIES_PER_CYCLE = 17
UNIQUE_POSTS_PER_CYCLE = 11
DUPLICATE_DELIVERIES_PER_CYCLE = 5     # relative indices 1, 4, 7, 11, 16: immediate / late / post-close / blocked / late
TAMPERED_DELIVERIES_PER_CYCLE = 1      # relative index 15: same identity, different bytes
# (base post, action, side, quantity, price, base idea) -- the only positive fills of a cycle.
EXPECTED_CYCLE_ORDERS: tuple[tuple[str, str, str, int, str, str], ...] = (
    ("e01", "OPEN", "BUY", 4, "41.25", "e01:a1"),
    ("e03", "REDUCE", "SELL", 2, "43.55", "e01:a1"),
    ("e05", "CLOSE", "SELL", 2, "44.65", "e01:a1"),
    ("e07", "OPEN", "SELL", 4, "120.45", "e07:a1"),
    ("e09", "CLOSE", "BUY", 4, "118.45", "e07:a1"),
)
# (base idea, symbol, side, status, final quantity, stop, target) -- the stale MODIFY never applies.
EXPECTED_CYCLE_IDEAS: tuple[tuple[str, str, str, str, int, str, str], ...] = (
    ("e01:a1", "ZQX", "LONG", "CLOSED", 0, "39.80", "45.00"),
    ("e07:a1", "VBN", "SHORT", "CLOSED", 0, "124.00", "112.00"),
)
FIRST_CYCLE_OUTCOMES = {"BLOCK": 2, "HOLD": 2, "NO_TRADE": 1, "RECONCILE": 1, "SIMULATED": 5}
FIRST_CYCLE_REASONS = {
    "COMMENTARY_OR_SUMMARY": 1, "CONDITIONAL_TRIGGER_NOT_IMPLEMENTED": 1, "HISTORICAL_REPORT_NO_ORDER": 1,
    "INSTRUMENT_NOT_ALLOWLISTED": 1, "PAPER_OPEN": 2, "PAPER_REDUCE_ONLY": 3,
    "SOURCE_EDIT_REQUIRES_REVIEW": 1, "SOURCE_STALE_OR_FUTURE": 1,
}
# Cumulative-context behaviour surfaced by the soak (existing policy, unchanged, documented): the
# interpreter's publisher-book view records the first cycle's off-allowlist KPL long because the
# publisher announced it and never closed it (the GATEWAY blocked the fill, the publisher did not
# exit). From the second cycle on the identical "We are now long $KPL" clause therefore carries
# EXISTING_OPEN_IDEA_SAME_SIDE_IN_CONTEXT and the validator HOLDs on the ambiguity before it reaches
# the allowlist check. HOLD and BLOCK both place no order; fills, positions and rows are unaffected.
LATER_CYCLE_OUTCOMES = {"BLOCK": 1, "HOLD": 3, "NO_TRADE": 1, "RECONCILE": 1, "SIMULATED": 5}
LATER_CYCLE_REASONS = {
    **{k: v for k, v in FIRST_CYCLE_REASONS.items() if k != "INSTRUMENT_NOT_ALLOWLISTED"},
    "EXISTING_OPEN_IDEA_SAME_SIDE_IN_CONTEXT": 1, "SOURCE_AMBIGUITIES": 1,
}


class LedgerTotals(StrictModel):
    """Totals of one persistent mock ledger: expected (from the specification) or measured (from
    a read-only SQLite connection). Quantities are mock units."""
    cycles: int
    post_rows: int
    action_rows: int
    idea_rows: int
    closed_ideas: int
    open_position_quantity: int
    orders: int
    open_fills: int
    reduce_fills: int
    close_fills: int
    amendments: int
    open_quantity: int
    reduce_quantity: int
    close_quantity: int
    outcome_counts: dict[str, int]
    reason_counts: dict[str, int]


def expected_totals(cycles: int) -> LedgerTotals:
    """Specification-derived totals for ``cycles`` cycles on one cumulative ledger."""
    outcomes: Counter[str] = Counter()
    reasons: Counter[str] = Counter()
    for cycle in range(cycles):
        outcomes.update(FIRST_CYCLE_OUTCOMES if cycle == 0 else LATER_CYCLE_OUTCOMES)
        reasons.update(FIRST_CYCLE_REASONS if cycle == 0 else LATER_CYCLE_REASONS)
    by_action: dict[str, list[int]] = {}
    for _, action, _, quantity, _, _ in EXPECTED_CYCLE_ORDERS:
        by_action.setdefault(action, []).append(quantity)
    return LedgerTotals(
        cycles=cycles,
        post_rows=UNIQUE_POSTS_PER_CYCLE * cycles, action_rows=UNIQUE_POSTS_PER_CYCLE * cycles,
        idea_rows=len(EXPECTED_CYCLE_IDEAS) * cycles,
        closed_ideas=sum(1 for i in EXPECTED_CYCLE_IDEAS if i[3] == "CLOSED") * cycles,
        open_position_quantity=sum(i[4] for i in EXPECTED_CYCLE_IDEAS if i[3] == "OPEN") * cycles,
        orders=len(EXPECTED_CYCLE_ORDERS) * cycles,
        open_fills=len(by_action.get("OPEN", [])) * cycles, reduce_fills=len(by_action.get("REDUCE", [])) * cycles,
        close_fills=len(by_action.get("CLOSE", [])) * cycles, amendments=len(by_action.get("MODIFY", [])) * cycles,
        open_quantity=sum(by_action.get("OPEN", [])) * cycles, reduce_quantity=sum(by_action.get("REDUCE", [])) * cycles,
        close_quantity=sum(by_action.get("CLOSE", [])) * cycles,
        outcome_counts=dict(sorted(outcomes.items())), reason_counts=dict(sorted(reasons.items())),
    )


def expected_cycle_orders(cycle: int) -> list[tuple[str, str, str, str, int, str]]:
    """(order id, idea id, action, side, quantity, price) expected for one cycle, in delivery order."""
    prefix = cycle_prefix(cycle)
    return [(f"paper-{prefix}{post}-1-a1", f"{prefix}{idea}", action, side, quantity, price)
            for post, action, side, quantity, price, idea in EXPECTED_CYCLE_ORDERS]


def expected_cycle_ideas(cycle: int) -> list[tuple[str, str, str, str, int, str, str]]:
    prefix = cycle_prefix(cycle)
    return [(prefix + idea, *rest) for idea, *rest in EXPECTED_CYCLE_IDEAS]


# ----------------------------------------------------------------------------- independent measurement

def _readonly(path: Path) -> sqlite3.Connection:
    """Read-only SQLite connection: the measurement can never modify the ledger it measures."""
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)


def _latest_outcomes(con: sqlite3.Connection) -> list[tuple[str, int, ActionOutcome]]:
    """(source_id, revision, outcome) of the latest evaluation pass per post, in insertion order,
    parsed with the coordinator's own ``ActionOutcome`` contract (no new schema)."""
    rows = con.execute(
        """SELECT a.source_id, a.revision, a.result_json FROM replay_actions a
        WHERE a.attempt = (SELECT MAX(b.attempt) FROM replay_actions b
                           WHERE b.source_id = a.source_id AND b.revision = a.revision)
        ORDER BY a.rowid"""
    ).fetchall()
    return [(sid, rev, ActionOutcome.model_validate_json(result)) for sid, rev, result in rows]


def measure_ledger(workdir: str | Path, cycles: int) -> LedgerTotals:
    """Totals of the ledger in ``workdir`` from a read-only connection, independent of the runner."""
    with closing(_readonly(Path(workdir) / LEDGER_FILE)) as con:
        post_rows = con.execute("SELECT COUNT(*) FROM replay_posts").fetchone()[0]
        action_rows = con.execute("SELECT COUNT(*) FROM replay_actions").fetchone()[0]
        ideas = con.execute("SELECT status, quantity FROM replay_ideas").fetchall()
        outcomes = _latest_outcomes(con)
    counts: Counter[str] = Counter()
    reasons: Counter[str] = Counter()
    fills: Counter[str] = Counter()
    quantities: Counter[str] = Counter()
    for _, _, outcome in outcomes:
        counts[outcome.status] += 1
        reasons.update(outcome.reasons)
        if outcome.order is not None:
            fills[outcome.order.action.value] += 1
            quantities[outcome.order.action.value] += outcome.order.quantity
    return LedgerTotals(
        cycles=cycles, post_rows=post_rows, action_rows=action_rows, idea_rows=len(ideas),
        closed_ideas=sum(1 for status, _ in ideas if status == "CLOSED"),
        open_position_quantity=sum(quantity for status, quantity in ideas if status == "OPEN"),
        orders=sum(fills.values()),
        open_fills=fills["OPEN"], reduce_fills=fills["REDUCE"], close_fills=fills["CLOSE"], amendments=fills["MODIFY"],
        open_quantity=quantities["OPEN"], reduce_quantity=quantities["REDUCE"], close_quantity=quantities["CLOSE"],
        outcome_counts=dict(sorted(counts.items())), reason_counts=dict(sorted(reasons.items())),
    )


def cycle_mismatches(workdir: str | Path, cycles: int) -> list[str]:
    """Per-cycle check of the ledger in ``workdir``: every cycle must hold exactly its two closed
    ideas at quantity 0 with the specified stops, and exactly its five orders with ids and idea
    references inside the same cycle (coherent references). Returns human-readable mismatches."""
    with closing(_readonly(Path(workdir) / LEDGER_FILE)) as con:
        idea_rows = con.execute(
            "SELECT idea_id, symbol, position_side, status, quantity, stop_price, target_price "
            "FROM replay_ideas ORDER BY idea_id").fetchall()
        outcomes = _latest_outcomes(con)
    ideas_by_cycle: dict[int | None, list[tuple]] = {}
    for row in idea_rows:
        ideas_by_cycle.setdefault(cycle_of(row[0]), []).append(tuple(row))
    orders_by_cycle: dict[int | None, list[tuple]] = {}
    for source_id, _, outcome in outcomes:
        if outcome.order is not None:
            o = outcome.order
            cycle = cycle_of(source_id)
            if cycle_of(o.idea_id) != cycle:
                orders_by_cycle.setdefault(None, []).append((o.order_id, o.idea_id))
            orders_by_cycle.setdefault(cycle, []).append(
                (o.order_id, o.idea_id, o.action.value, o.side, o.quantity, o.price))
    problems: list[str] = []
    for cycle in range(cycles):
        if ideas_by_cycle.pop(cycle, []) != expected_cycle_ideas(cycle):
            problems.append(f"cycle {cycle}: ideas differ from the specification")
        if orders_by_cycle.pop(cycle, []) != expected_cycle_orders(cycle):
            problems.append(f"cycle {cycle}: orders differ from the specification")
    for cycle, rows in sorted(ideas_by_cycle.items(), key=lambda kv: str(kv[0])):
        problems.append(f"unexpected ideas outside the planned cycles ({cycle}): {len(rows)}")
    for cycle, rows in sorted(orders_by_cycle.items(), key=lambda kv: str(kv[0])):
        problems.append(f"unexpected orders outside the planned cycles ({cycle}): {len(rows)}")
    return problems


def checkpoint_cursor(workdir: str | Path) -> int:
    return ShadowCheckpoint.model_validate_json((Path(workdir) / CHECKPOINT_FILE).read_text("utf-8")).cursor


# ----------------------------------------------------------------------------- seeded restart plan

class RestartStep(StrictModel):
    """One planned process boundary. ``cut_index`` is the delivery index at which the process
    stops: a pause leaves the checkpoint cursor there; an interruption crashes at that delivery."""
    segment: int = Field(ge=1)
    planned: Literal["pause", "interrupt_before_commit", "interrupt_after_commit"]
    cut_index: int = Field(ge=1)
    hash_seed: int = Field(ge=1, le=2**32 - 1)

    @property
    def phase(self) -> Phase | None:
        if self.planned == "interrupt_before_commit":
            return "before_commit"
        if self.planned == "interrupt_after_commit":
            return "after_commit"
        return None


class SoakPlan(StrictModel):
    deliveries: int
    steps: list[RestartStep]
    reference_hash_seed: int
    final_hash_seed: int


def make_plan(profile: SoakProfile, deliveries: int) -> SoakPlan:
    """Deterministic plan from ``profile.seed``: strictly increasing cut points in ``[1, n-1]``, the
    three restart kinds assigned round-robin then shuffled (so every kind appears once there are
    three or more restarts), and a distinct ``PYTHONHASHSEED`` for every process."""
    if profile.restarts > deliveries - 1:
        raise SoakStateError(f"{profile.restarts} restarts need at least {profile.restarts + 1} deliveries, "
                             f"the feed has {deliveries}")
    rng = random.Random(profile.seed)
    cuts = sorted(rng.sample(range(1, deliveries), profile.restarts))
    kinds: list[Literal["pause", "interrupt_before_commit", "interrupt_after_commit"]] = [
        ("pause", "interrupt_before_commit", "interrupt_after_commit")[i % 3] for i in range(profile.restarts)]
    rng.shuffle(kinds)
    seeds = rng.sample(HASH_SEED_RANGE, profile.restarts + 2)
    steps = [RestartStep(segment=i + 1, planned=kind, cut_index=cut, hash_seed=seed)
             for i, (kind, cut, seed) in enumerate(zip(kinds, cuts, seeds[2:], strict=True))]
    return SoakPlan(deliveries=deliveries, steps=steps, reference_hash_seed=seeds[0], final_hash_seed=seeds[1])


# ----------------------------------------------------------------------------- segments (one OS process each)

class SegmentSpec(StrictModel):
    segment: int = Field(ge=0)
    role: Role
    planned: Planned
    resume: bool
    max_deliveries: int | None = Field(default=None, ge=1)
    interrupt_index: int | None = Field(default=None, ge=0)
    interrupt_phase: Phase | None = None
    hash_seed: int = Field(ge=1, le=2**32 - 1)
    max_wall_seconds: float = Field(gt=0)


class SegmentOutcome(StrictModel):
    """What one process reported, plus what the orchestrator verified about it afterwards."""
    segment: int
    role: Role
    planned: Planned
    hash_seed: int
    hash_seed_confirmed: bool
    import_origin_verified: bool
    network_guard: bool
    exit_code: int
    status: SegmentStatus
    resumed_from: int | None = None
    cursor: int | None = None
    processed: int = 0
    fresh_results: int = 0
    duplicate_returns: int = 0
    hash_mismatch_blocks: int = 0
    convergence_digest: str | None = None
    wall_budget_s: float = Field(gt=0)      # the bound handed to this process: per-process cap AND remaining total
    wall_seconds: float
    window_verified: bool | None = None
    error: str | None = None


@dataclass(frozen=True)
class SegmentRequest:
    spec: SegmentSpec
    workdir: Path
    report_path: Path
    cycles: int
    cycle_period_s: int
    feed_digest: str
    import_origin: Path


Launcher = Callable[[SegmentRequest], SegmentOutcome]


def import_origin() -> Path:
    """The ``src`` root the running interpreter imported ``tradingbot`` from."""
    return Path(tradingbot.__file__).resolve().parents[1]


def run_segment(request: SegmentRequest) -> SegmentOutcome:
    """Execute one segment in THIS process: verify the import origin and feed digest, open or
    resume the runner on ``request.workdir`` and run it to its planned bound or interruption."""
    spec = request.spec
    started = time.monotonic()
    common = {"segment": spec.segment, "role": spec.role, "planned": spec.planned, "hash_seed": spec.hash_seed,
              "hash_seed_confirmed": os.environ.get("PYTHONHASHSEED") == str(spec.hash_seed),
              "import_origin_verified": import_origin() == request.import_origin.resolve(),
              "network_guard": network_guard_active(), "wall_budget_s": spec.max_wall_seconds}

    def refused(message: str) -> SegmentOutcome:
        return SegmentOutcome(**common, exit_code=EXIT_REFUSED, status="REFUSED", error=message,
                              wall_seconds=time.monotonic() - started)

    if not common["import_origin_verified"]:
        return refused("import origin mismatch: this process did not import tradingbot from the pinned src")
    feed = build_soak_feed(request.cycles, cycle_period_s=request.cycle_period_s)
    if feed.digest() != request.feed_digest:
        return refused("feed digest mismatch: this process built a different soak feed")
    try:
        runner = ShadowRunner(request.workdir, feed, shadow_policy(), resume=spec.resume)
    except ShadowStateError as exc:
        return refused(f"runner refused: {exc}")
    resumed_from = runner.cursor
    seen: Counter[str] = Counter()

    def count(record) -> None:
        seen["processed"] += 1
        if not record.result.duplicate:
            seen["fresh_results"] += 1
        elif any("SOURCE_HASH_CHANGED_WITHOUT_REVISION" in o.reasons for o in record.result.outcomes):
            seen["hash_mismatch_blocks"] += 1
        else:
            seen["duplicate_returns"] += 1

    interrupt = (InterruptSpec(spec.interrupt_index, spec.interrupt_phase)
                 if spec.interrupt_index is not None and spec.interrupt_phase is not None else None)
    try:
        report = runner.run(max_deliveries=spec.max_deliveries, max_wall_seconds=spec.max_wall_seconds,
                            interrupt=interrupt, on_delivery=count)
    except ShadowInterrupted as exc:
        return SegmentOutcome(**common, exit_code=EXIT_INTERRUPTED, status="INTERRUPTED", resumed_from=resumed_from,
                              cursor=exc.checkpoint_cursor, **seen, wall_seconds=time.monotonic() - started)
    request.report_path.parent.mkdir(parents=True, exist_ok=True)
    request.report_path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    stats = report.this_run
    return SegmentOutcome(**common, exit_code=EXIT_OK if report.status == "COMPLETE" else EXIT_PAUSED,
                          status=report.status, resumed_from=report.resumed_from, cursor=report.cursor,
                          processed=stats.processed, fresh_results=stats.fresh_results,
                          duplicate_returns=stats.duplicate_returns, hash_mismatch_blocks=stats.hash_mismatch_blocks,
                          convergence_digest=report.convergence_digest, wall_seconds=time.monotonic() - started)


def segment_argv(request: SegmentRequest) -> list[str]:
    spec = request.spec
    argv = ["segment", "--workdir", str(request.workdir), "--report", str(request.report_path),
            "--cycles", str(request.cycles), "--cycle-period-s", str(request.cycle_period_s),
            "--expect-feed-digest", request.feed_digest, "--expect-import-origin", str(request.import_origin),
            "--segment", str(spec.segment), "--role", spec.role, "--planned", spec.planned,
            "--hash-seed", str(spec.hash_seed), "--max-seconds", repr(spec.max_wall_seconds)]
    if spec.resume:
        argv.append("--resume")
    if spec.max_deliveries is not None:
        argv += ["--max-deliveries", str(spec.max_deliveries)]
    if spec.interrupt_index is not None and spec.interrupt_phase is not None:
        argv += ["--interrupt-at", str(spec.interrupt_index), "--interrupt-phase", spec.interrupt_phase]
    return argv


def launch_subprocess(request: SegmentRequest) -> SegmentOutcome:
    """Run one segment as a FRESH OS process: ``PYTHONPATH`` pinned to the orchestrator's own
    import origin (the child re-verifies it), ``PYTHONHASHSEED`` set from the plan, no isolated
    mode (so the pin is honoured), stdout limited to one JSON line."""
    env = {**os.environ, "PYTHONHASHSEED": str(request.spec.hash_seed),
           "PYTHONPATH": str(request.import_origin)
           + (os.pathsep + os.environ["PYTHONPATH"] if os.environ.get("PYTHONPATH") else "")}
    command = [sys.executable, "-m", "tradingbot.shadow_soak", *segment_argv(request)]
    started = time.monotonic()
    try:
        proc = subprocess.run(command, capture_output=True, text=True, env=env, check=False,
                              timeout=request.spec.max_wall_seconds + 60.0)
    except subprocess.TimeoutExpired:
        return _errored(request, "process exceeded its hard timeout", -1, time.monotonic() - started)
    lines = proc.stdout.strip().splitlines()
    try:
        outcome = SegmentOutcome.model_validate_json(lines[-1]) if lines else None
    except (ValidationError, ValueError):
        outcome = None
    if outcome is None or outcome.exit_code != proc.returncode:
        tail = (proc.stderr.strip().splitlines() or ["no output"])[-1][:200]
        return _errored(request, f"child exit {proc.returncode}: {tail}", proc.returncode, time.monotonic() - started)
    return outcome


def _errored(request: SegmentRequest, message: str, exit_code: int, wall: float) -> SegmentOutcome:
    spec = request.spec
    return SegmentOutcome(segment=spec.segment, role=spec.role, planned=spec.planned, hash_seed=spec.hash_seed,
                          hash_seed_confirmed=False, import_origin_verified=False, network_guard=False,
                          exit_code=exit_code, status="ERROR", error=message, wall_budget_s=spec.max_wall_seconds,
                          wall_seconds=wall)


def segment_main(argv: list[str] | None = None) -> int:
    """Internal child entry: ``python -m tradingbot.shadow_soak segment ...``. Prints exactly one
    JSON ``SegmentOutcome`` line and exits with its exit code. Runs under the no-network guard."""
    parser = argparse.ArgumentParser(prog="python -m tradingbot.shadow_soak", add_help=True)
    parser.add_argument("command", choices=["segment"])
    parser.add_argument("--workdir", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--cycles", type=int, required=True)
    parser.add_argument("--cycle-period-s", type=int, required=True)
    parser.add_argument("--expect-feed-digest", required=True)
    parser.add_argument("--expect-import-origin", type=Path, required=True)
    parser.add_argument("--segment", type=int, required=True)
    parser.add_argument("--role", choices=["reference", "chain"], required=True)
    parser.add_argument("--planned", choices=["complete", "pause", "interrupt_before_commit",
                                              "interrupt_after_commit"], required=True)
    parser.add_argument("--hash-seed", type=int, required=True)
    parser.add_argument("--max-seconds", type=float, required=True)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--max-deliveries", type=int, default=None)
    parser.add_argument("--interrupt-at", type=int, default=None)
    parser.add_argument("--interrupt-phase", choices=["before_commit", "after_commit"], default=None)
    args = parser.parse_args(argv)
    spec = SegmentSpec(segment=args.segment, role=args.role, planned=args.planned, resume=args.resume,
                       max_deliveries=args.max_deliveries, interrupt_index=args.interrupt_at,
                       interrupt_phase=args.interrupt_phase, hash_seed=args.hash_seed,
                       max_wall_seconds=args.max_seconds)
    request = SegmentRequest(spec=spec, workdir=args.workdir, report_path=args.report, cycles=args.cycles,
                             cycle_period_s=args.cycle_period_s, feed_digest=args.expect_feed_digest,
                             import_origin=args.expect_import_origin)
    with no_network():
        outcome = run_segment(request)
    print(outcome.model_dump_json())
    return outcome.exit_code


# ----------------------------------------------------------------------------- report contract

class FeedIdentity(StrictModel):
    feed_id: str
    feed_digest: str
    base_feed_id: str
    base_feed_digest: str
    policy_digest: str
    cycles: int
    cycle_period_s: int
    posts: int
    deliveries: int
    duplicate_deliveries: int
    tampered_deliveries: int
    source_identity_scheme: str
    virtual_epoch: datetime
    virtual_end: datetime
    virtual_span_s: int


class WallTime(StrictModel):
    """Measured wall-clock seconds, including process start-up. Reported separately from virtual
    time; never a decision input and not a throughput or uptime figure."""
    reference_s: float
    chain_s: float
    total_s: float
    per_process_budget_s: float
    total_budget_s: float


class SoakReport(StrictModel):
    schema_version: Literal["shadow-soak-report-1"] = REPORT_SCHEMA
    label: str = SOAK_LABEL
    mock_only: Literal[True] = True
    profile: SoakProfile
    feed: FeedIdentity
    plan: SoakPlan
    segments: list[SegmentOutcome]
    processes_launched: int
    restarts_planned: int
    restarts_executed: int
    distinct_hash_seeds: int
    reference_digest: str | None
    chain_digest: str | None
    digests_converge: bool
    ledgers_equal: bool
    expected: LedgerTotals
    actual_reference: LedgerTotals | None
    actual_chain: LedgerTotals | None
    cycle_mismatches: list[str]
    wall: WallTime
    verdict: Verdict
    findings: list[str]


# ----------------------------------------------------------------------------- orchestration

def claim_fresh_root(root: Path) -> None:
    """The soak root must be absent or an empty directory; nothing else is ever read or written."""
    if root.exists():
        if not root.is_dir():
            raise SoakStateError(f"{root} is not a directory")
        if any(root.iterdir()):
            raise SoakStateError(f"refusing to use non-empty {root}; choose a fresh directory for the soak")
    root.mkdir(parents=True, exist_ok=True)


def feed_identity(feed: ShadowFeed, base: ShadowFeed, profile: SoakProfile) -> FeedIdentity:
    span = feed.deliveries[-1].offset_s
    return FeedIdentity(
        feed_id=feed.feed_id, feed_digest=feed.digest(), base_feed_id=base.feed_id, base_feed_digest=base.digest(),
        policy_digest=policy_digest(shadow_policy()), cycles=profile.cycles, cycle_period_s=profile.cycle_period_s,
        posts=len(feed.posts), deliveries=len(feed.deliveries),
        duplicate_deliveries=DUPLICATE_DELIVERIES_PER_CYCLE * profile.cycles,
        tampered_deliveries=TAMPERED_DELIVERIES_PER_CYCLE * profile.cycles,
        source_identity_scheme=SOURCE_IDENTITY_SCHEME, virtual_epoch=feed.epoch,
        virtual_end=feed.virtual_time(span), virtual_span_s=span,
    )


class _Findings:
    def __init__(self) -> None:
        self.fail: list[str] = []
        self.incomplete: list[str] = []

    @property
    def verdict(self) -> Verdict:
        return "FAIL" if self.fail else ("INCOMPLETE" if self.incomplete else "PASS")

    @property
    def all(self) -> list[str]:
        return [f"FAIL: {f}" for f in self.fail] + [f"INCOMPLETE: {f}" for f in self.incomplete]


def run_soak(profile: SoakProfile, root: str | Path, *, launcher: Launcher = launch_subprocess,
             wall_clock: Callable[[], float] = time.monotonic,
             on_segment: Callable[[SegmentOutcome], None] | None = None) -> SoakReport:
    """Run the reference process, then the restart chain, on fresh directories under ``root``;
    measure both ledgers independently and write ``soak_report.json``. ``root`` is never removed."""
    root = Path(root)
    base = synthetic_feed()
    feed = build_soak_feed(profile.cycles, cycle_period_s=profile.cycle_period_s, base=base)
    total = len(feed.deliveries)
    plan = make_plan(profile, total)          # refuses an unplannable profile before any directory is touched
    claim_fresh_root(root)
    identity = feed_identity(feed, base, profile)
    unique_prefix = prefix_unique_counts(feed)
    expected = expected_totals(profile.cycles)
    origin = import_origin()
    reference_dir, chain_dir, segments_dir = root / REFERENCE_DIR, root / CHAIN_DIR, root / SEGMENTS_DIR
    segments: list[SegmentOutcome] = []
    findings = _Findings()
    started = wall_clock()

    def launch(spec: SegmentSpec, workdir: Path) -> SegmentOutcome:
        request = SegmentRequest(spec=spec, workdir=workdir, report_path=segments_dir / f"{spec.segment:03d}-{spec.role}.json",
                                 cycles=profile.cycles, cycle_period_s=profile.cycle_period_s,
                                 feed_digest=identity.feed_digest, import_origin=origin)
        outcome = launcher(request)
        if outcome.error:
            outcome = outcome.model_copy(update={"error": outcome.error.replace(str(root), "<root>")})
        segments.append(outcome)
        return outcome

    def notify() -> None:
        if on_segment is not None:
            on_segment(segments[-1])

    def generic_checks(outcome: SegmentOutcome, label: str) -> bool:
        ok = True
        if outcome.status in ("REFUSED", "ERROR"):
            findings.fail.append(f"{label} did not run: {outcome.status} ({outcome.error})")
            return False
        for flag, text in ((outcome.import_origin_verified, "import origin"), (outcome.hash_seed_confirmed, "PYTHONHASHSEED"),
                           (outcome.network_guard, "no-network guard")):
            if not flag:
                findings.fail.append(f"{label}: {text} not verified in the child process")
                ok = False
        if outcome.status == "WALL_BUDGET_EXHAUSTED":
            findings.incomplete.append(f"{label} paused on its wall budget at cursor {outcome.cursor} (bounded pause, not a PASS)")
            ok = False
        return ok

    exhausted = False

    def launch_budget(label: str) -> float | None:
        """Wall budget for the next launch: the per-process bound, further capped by what is left of
        the total budget. Nothing is launched once the total budget is exhausted."""
        nonlocal exhausted
        remaining = profile.max_total_wall_seconds - (wall_clock() - started)
        if remaining <= 0:
            exhausted = True
            findings.incomplete.append(f"total wall budget exhausted before {label}")
            return None
        return min(profile.max_wall_seconds_per_process, remaining)

    # 1. uninterrupted reference process
    reference = launch(SegmentSpec(segment=0, role="reference", planned="complete", resume=False,
                                   hash_seed=plan.reference_hash_seed,      # nothing has elapsed yet by construction
                                   max_wall_seconds=min(profile.max_wall_seconds_per_process,
                                                        profile.max_total_wall_seconds)), reference_dir)
    if generic_checks(reference, "reference") and (reference.status != "COMPLETE" or reference.cursor != total):
        findings.fail.append(f"reference ended {reference.status} at cursor {reference.cursor}, expected COMPLETE at {total}")
    notify()
    reference_wall = wall_clock() - started
    chain_started = wall_clock()

    # 2. the restart chain on ONE persistent ledger
    chain_final: SegmentOutcome | None = None
    proceed = reference.status == "COMPLETE"
    previous_cut = 0
    for step in plan.steps:
        budget = launch_budget(f"chain segment {step.segment}") if proceed else None
        if budget is None:
            proceed = False
            break
        pause = step.planned == "pause"
        spec = SegmentSpec(segment=step.segment, role="chain", planned=step.planned, resume=step.segment > 1,
                           max_deliveries=step.cut_index - previous_cut if pause else None,
                           interrupt_index=None if pause else step.cut_index, interrupt_phase=step.phase,
                           hash_seed=step.hash_seed, max_wall_seconds=budget)
        outcome = launch(spec, chain_dir)
        if not generic_checks(outcome, f"chain segment {step.segment}"):
            notify()
            proceed = False
            break
        expected_status, expected_exit = ("DELIVERY_CAP_REACHED", EXIT_PAUSED) if pause else ("INTERRUPTED", EXIT_INTERRUPTED)
        cursor = checkpoint_cursor(chain_dir)
        committed = measure_ledger(chain_dir, profile.cycles).post_rows
        window = unique_prefix[step.cut_index + (1 if step.planned == "interrupt_after_commit" else 0)]
        verified = (outcome.status, outcome.exit_code, outcome.resumed_from, outcome.cursor, cursor, committed) == \
            (expected_status, expected_exit, previous_cut, step.cut_index, step.cut_index, window)
        segments[-1] = outcome.model_copy(update={"window_verified": verified})
        notify()
        if not verified:
            findings.fail.append(
                f"chain segment {step.segment} planned {step.planned} at {step.cut_index}: got {outcome.status} "
                f"exit {outcome.exit_code} resumed_from {outcome.resumed_from} cursor {outcome.cursor}/{cursor}, "
                f"{committed} committed post rows (expected {window})")
            proceed = False
            break
        previous_cut = step.cut_index
    final_budget = launch_budget("the final chain segment") if proceed else None
    if final_budget is not None:
        chain_final = launch(SegmentSpec(segment=len(plan.steps) + 1, role="chain", planned="complete",
                                         resume=bool(plan.steps), hash_seed=plan.final_hash_seed,
                                         max_wall_seconds=final_budget), chain_dir)
        if generic_checks(chain_final, "final chain segment") and \
                (chain_final.status != "COMPLETE" or chain_final.cursor != total or chain_final.resumed_from != previous_cut):
            findings.fail.append(f"final chain segment ended {chain_final.status} at cursor {chain_final.cursor} "
                                 f"from {chain_final.resumed_from}, expected COMPLETE at {total} from {previous_cut}")
        notify()
    elif not findings.incomplete and not findings.fail:
        findings.fail.append("the restart chain did not reach its final segment")
    chain_wall = wall_clock() - chain_started

    # 3. independent measurement and convergence
    actual_reference = actual_chain = None
    mismatches: list[str] = []
    if reference.status == "COMPLETE":
        actual_reference = measure_ledger(reference_dir, profile.cycles)
        mismatches += [f"reference: {m}" for m in cycle_mismatches(reference_dir, profile.cycles)]
        if actual_reference != expected:
            findings.fail.append("reference ledger totals differ from the specification-derived expectation")
    if chain_final is not None and chain_final.status == "COMPLETE":
        actual_chain = measure_ledger(chain_dir, profile.cycles)
        mismatches += [f"chain: {m}" for m in cycle_mismatches(chain_dir, profile.cycles)]
        if actual_chain != expected:
            findings.fail.append("chain ledger totals differ from the specification-derived expectation")
    if mismatches:
        findings.fail.append(f"{len(mismatches)} per-cycle mismatch(es), see cycle_mismatches")
    chain_digest = chain_final.convergence_digest if chain_final is not None else None
    digests_converge = reference.convergence_digest is not None and reference.convergence_digest == chain_digest
    ledgers_equal = False
    if reference.status == "COMPLETE" and chain_final is not None and chain_final.status == "COMPLETE":
        ledgers_equal = _ledger_of(segments_dir / "000-reference.json") == \
            _ledger_of(segments_dir / f"{chain_final.segment:03d}-chain.json")
        if not digests_converge or not ledgers_equal:
            findings.fail.append("reference and chain ledgers do not converge")
    seeds = {s.hash_seed for s in segments}
    if len(segments) >= 2 and len(seeds) != len(segments):
        findings.fail.append("process hash seeds were not all distinct")
    executed = sum(1 for s in segments if s.role == "chain" and s.planned != "complete" and s.window_verified)
    if findings.verdict == "PASS" and executed != profile.restarts:
        findings.fail.append(f"{executed} of {profile.restarts} planned restarts verified")

    # 4. the total deadline is re-checked after the final segment and the measurement: finishing late
    #    is a bounded pause in disguise and can never be a PASS
    elapsed = wall_clock() - started
    if elapsed > profile.max_total_wall_seconds and not exhausted:
        findings.incomplete.append(f"total wall budget exceeded: {elapsed:.3f}s measured against "
                                   f"{profile.max_total_wall_seconds:g}s; a late finish is not a PASS")

    report = SoakReport(
        profile=profile, feed=identity, plan=plan, segments=segments, processes_launched=len(segments),
        restarts_planned=profile.restarts, restarts_executed=executed, distinct_hash_seeds=len(seeds),
        reference_digest=reference.convergence_digest, chain_digest=chain_digest,
        digests_converge=digests_converge, ledgers_equal=ledgers_equal, expected=expected,
        actual_reference=actual_reference, actual_chain=actual_chain, cycle_mismatches=mismatches,
        wall=WallTime(reference_s=round(reference_wall, 3), chain_s=round(chain_wall, 3),
                      total_s=round(elapsed, 3),
                      per_process_budget_s=profile.max_wall_seconds_per_process,
                      total_budget_s=profile.max_total_wall_seconds),
        verdict=findings.verdict, findings=findings.all,
    )
    (root / SOAK_REPORT_FILE).write_text(report.model_dump_json(indent=2), encoding="utf-8")
    return report


def _ledger_of(report_path: Path):
    return ShadowRunReport.model_validate_json(report_path.read_text("utf-8")).ledger


if __name__ == "__main__":   # pragma: no cover - exercised through subprocesses in the tests
    sys.exit(segment_main())
