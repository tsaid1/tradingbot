"""SHADOW-01: deterministic, finite, SYNTHETIC shadow runner with checkpoint/restart.

NOT PRODUCTION. This module replays a newly authored synthetic event feed under a virtual clock
through the existing public chain and proves two operational properties offline:

* a finite run and an interrupted-then-resumed run converge on the same mock ledger and the same
  aggregate outcomes (restart proof);
* duplicate, late and tampered re-deliveries never add a mock order (idempotency proof).

Everything that decides is reused, nothing is re-implemented:

* ``tkl_interpreter.TklInterpreter`` turns synthetic ``SourcePost`` text into ``TradeAction``s;
* ``validator.gate_source`` / ``gate_action`` authorize (called inside the coordinator);
* ``event_replay.PaperEventReplay`` is the mock ledger of record (SQLite), with its own
  once-only protection per ``(source_id, revision, post_hash)``.

The runner owns only the delivery loop, the virtual clock, the checkpoint file and the report.
It has no network code, no broker, no credentials, and it refuses to touch a working directory
that already holds data unless explicitly resumed.

Crash-boundary claim (honest version). The checkpoint is written *after* the coordinator's
SQLite transaction commits. A crash between the two re-delivers the last event on resume; the
coordinator recognises it as a duplicate and fills nothing again. Deliveries are therefore
processed **at least once**; the *ledger effect* of an identical delivery is at most once because
of the coordinator, not because of this runner. Delivery statistics (how many duplicates were
seen) legitimately differ between a straight run and a resumed run; the ledger-derived report
does not. Nothing here says anything about broker, live-source or wall-clock reliability.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Literal

from pydantic import Field, ValidationError, model_validator

from tradingbot.event_models import ReplayResult
from tradingbot.event_replay import PaperEventReplay
from tradingbot.models import MockPolicy, Quote, StrictModel
from tradingbot.source_events import Provenance, SourcePost
from tradingbot.tkl_interpreter import InterpretedPost, TklInterpreter, group_linked_actions

NON_PRODUCTION_LABEL = (
    "SYNTHETIC SHADOW RUN - MOCK ONLY - NOT BROKER, LIVE-SOURCE OR PAPER-ACCEPTANCE EVIDENCE"
)
CHECKPOINT_SCHEMA = "shadow-checkpoint-1"
CHECKPOINT_FILE = "checkpoint.json"
LEDGER_FILE = "shadow_ledger.sqlite3"
SYNTHETIC_AUTHOR = "SYNTHETIC-AUTHOR"

Phase = Literal["before_commit", "after_commit"]
RunStatus = Literal["COMPLETE", "DELIVERY_CAP_REACHED", "WALL_BUDGET_EXHAUSTED"]


class ShadowStateError(RuntimeError):
    """A refusal: unsafe working directory, invalid checkpoint, or ledger/checkpoint mismatch."""


class ShadowInterrupted(RuntimeError):
    """Raised by an injected interruption. Simulates the process dying at a chosen boundary."""

    def __init__(self, delivery_index: int, phase: Phase, checkpoint_cursor: int):
        super().__init__(
            f"interrupted at delivery {delivery_index} ({phase}); checkpoint cursor={checkpoint_cursor}"
        )
        self.delivery_index = delivery_index
        self.phase = phase
        self.checkpoint_cursor = checkpoint_cursor


# ----------------------------------------------------------------------------- canonical hashing

def canonical_digest(data: object) -> str:
    """SHA-256 of a canonical JSON rendering (sorted keys, no whitespace)."""
    text = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def policy_digest(policy: MockPolicy) -> str:
    """Digest of the mock policy. ``allowed_symbols`` is a frozenset, whose JSON order would follow
    the per-process hash seed, so it is sorted first."""
    data = policy.model_dump(mode="json")
    if data.get("risk") is None:
        data.pop("risk", None)  # the cash baseline: the digest is the one recorded before
    if data.get("allowed_symbols") is not None:
        data["allowed_symbols"] = sorted(data["allowed_symbols"])
    return canonical_digest(data)


def _positive_decimal(value: str, what: str) -> None:
    try:
        number = Decimal(value)
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"{what} is not a decimal string: {value!r}") from exc
    if not number.is_finite() or number <= 0:
        raise ValueError(f"{what} must be a positive finite decimal: {value!r}")


# ----------------------------------------------------------------------------- synthetic feed contract

class ShadowPost(StrictModel):
    """One synthetic post version. Every word, price and timestamp is invented."""
    key: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,31}$")
    source_id: str = Field(min_length=1, max_length=128)
    revision: int = Field(default=1, ge=1)
    text: str = Field(min_length=1, max_length=2000)
    published_offset_s: int = Field(ge=0)          # seconds after the feed epoch
    revised_offset_s: int | None = Field(default=None, ge=0)   # required for revision > 1
    verified_author: bool = True
    text_complete: bool = True
    is_repost: bool = False
    quotes: dict[str, str] = Field(default_factory=dict)  # symbol -> synthetic mid price

    @model_validator(mode="after")
    def _consistent(self):
        if (self.revision > 1) != (self.revised_offset_s is not None):
            raise ValueError("revised_offset_s is required for revision > 1 and forbidden for revision 1")
        if self.revised_offset_s is not None and self.revised_offset_s < self.published_offset_s:
            raise ValueError("a revision cannot precede its publication")
        for symbol, mid in self.quotes.items():
            _positive_decimal(mid, f"quote mid for {symbol}")
        return self

    @property
    def available_offset_s(self) -> int:
        return self.revised_offset_s if self.revised_offset_s is not None else self.published_offset_s


class ShadowDelivery(StrictModel):
    """One delivery of a post to the runner at a virtual time. ``text_override`` models a corrupted
    re-delivery: same identity, different bytes. ``note`` is for humans and never a decision input."""
    post_key: str
    offset_s: int = Field(ge=0)
    text_override: str | None = Field(default=None, min_length=1, max_length=2000)
    note: str = Field(default="", max_length=120)


class ShadowFeed(StrictModel):
    feed_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_.-]{0,63}$")
    epoch: datetime
    half_spread: str = "0.05"
    posts: list[ShadowPost] = Field(min_length=1)
    deliveries: list[ShadowDelivery] = Field(min_length=1)

    @model_validator(mode="after")
    def _well_formed(self):
        if self.epoch.tzinfo is None:
            raise ValueError("epoch must be timezone-aware")
        _positive_decimal(self.half_spread, "half_spread")
        keys = [p.key for p in self.posts]
        if len(keys) != len(set(keys)):
            raise ValueError("post keys must be unique")
        identities = [(p.source_id, p.revision) for p in self.posts]
        if len(identities) != len(set(identities)):
            raise ValueError("(source_id, revision) must be unique across posts")
        by_key = {p.key: p for p in self.posts}
        previous = -1
        for index, d in enumerate(self.deliveries):
            post = by_key.get(d.post_key)
            if post is None:
                raise ValueError(f"delivery {index} references unknown post {d.post_key!r}")
            if d.offset_s < previous:
                raise ValueError(f"delivery {index} goes back in virtual time")
            if d.offset_s < post.available_offset_s:
                raise ValueError(f"delivery {index} precedes the availability of {d.post_key!r}")
            previous = d.offset_s
        return self

    def post(self, key: str) -> ShadowPost:
        return next(p for p in self.posts if p.key == key)

    def digest(self) -> str:
        return canonical_digest(self.model_dump(mode="json"))

    def virtual_time(self, offset_s: int) -> datetime:
        return self.epoch + timedelta(seconds=offset_s)


# ----------------------------------------------------------------------------- checkpoint contract

class ShadowCheckpoint(StrictModel):
    """Durable cursor. Deliveries ``[0, cursor)`` are committed to the ledger. Extras are rejected,
    digests must match the feed and policy in use, and the cursor must fit the feed."""
    schema_version: Literal["shadow-checkpoint-1"] = CHECKPOINT_SCHEMA
    mock_only: Literal[True] = True
    feed_id: str
    feed_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    ledger_file: str
    cursor: int = Field(ge=0, strict=True)
    virtual_now: datetime | None = None


# ----------------------------------------------------------------------------- report contract

class LedgerIdea(StrictModel):
    idea_id: str
    symbol: str
    asset_class: str
    position_side: str
    quantity: int
    stop_price: str | None
    target_price: str | None
    status: str


class RecordedOrder(StrictModel):
    order_id: str
    idea_id: str
    action: str
    symbol: str
    side: str
    quantity: int
    price: str | None
    status: str


class PostOutcome(StrictModel):
    source_id: str
    revision: int
    statuses: list[str]
    reasons: list[list[str]]


class LedgerSummary(StrictModel):
    """Everything here is read back from the coordinator's SQLite ledger, never from memory."""
    ideas: list[LedgerIdea]
    orders: list[RecordedOrder]
    posts: list[PostOutcome]
    outcome_counts: dict[str, int]
    reason_counts: dict[str, int]

    def digest(self) -> str:
        return canonical_digest(self.model_dump(mode="json"))


class DeliveryStats(StrictModel):
    """Counters for the deliveries processed by ONE invocation. These may differ between a straight
    run and a resumed run by design (a re-delivered event is counted as a duplicate)."""
    processed: int = 0
    fresh_results: int = 0
    duplicate_returns: int = 0
    hash_mismatch_blocks: int = 0


class ShadowRunReport(StrictModel):
    label: str = NON_PRODUCTION_LABEL
    feed_id: str
    feed_digest: str
    policy_digest: str
    deliveries_total: int
    resumed_from: int
    cursor: int
    status: RunStatus
    this_run: DeliveryStats
    ledger: LedgerSummary
    convergence_digest: str


@dataclass(frozen=True)
class InterruptSpec:
    """Inject a simulated crash at ``delivery_index``: before the coordinator transaction, or after
    it committed but before the checkpoint was written (the widest unsafe-looking window)."""
    delivery_index: int
    phase: Phase


@dataclass(frozen=True)
class DeliveryRecord:
    index: int
    post_key: str
    note: str
    virtual_now: datetime
    post: SourcePost
    interpreted: InterpretedPost
    result: ReplayResult


# ----------------------------------------------------------------------------- runner

class ShadowRunner:
    """Finite, deterministic delivery loop over a ``ShadowFeed`` with a durable cursor.

    ``wall_clock`` is used ONLY for the optional runtime budget; no decision input reads it.
    """

    def __init__(self, workdir: str | Path, feed: ShadowFeed, policy: MockPolicy, *,
                 resume: bool = False, wall_clock: Callable[[], float] = time.monotonic):
        self.workdir = Path(workdir)
        self.feed = feed
        self.policy = policy
        self.wall_clock = wall_clock
        self.feed_digest = feed.digest()
        self.policy_digest = policy_digest(policy)
        self.checkpoint_path = self.workdir / CHECKPOINT_FILE
        self.ledger_path = self.workdir / LEDGER_FILE
        if resume:
            self._open_existing()
        else:
            self._start_fresh()

    # ------------------------------------------------------------------ start / resume

    def _start_fresh(self) -> None:
        if self.workdir.exists():
            if not self.workdir.is_dir():
                raise ShadowStateError(f"{self.workdir} is not a directory")
            if any(self.workdir.iterdir()):
                raise ShadowStateError(
                    f"refusing to start in non-empty {self.workdir}; pass resume=True to continue a "
                    "previous shadow run, or choose a fresh directory"
                )
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.coordinator = PaperEventReplay(self.ledger_path, self.policy)
        self.interpreter = TklInterpreter()
        self.cursor = 0
        self._write_checkpoint()

    def _open_existing(self) -> None:
        if not self.checkpoint_path.is_file():
            raise ShadowStateError(f"no checkpoint at {self.checkpoint_path}")
        if not self.ledger_path.is_file():
            raise ShadowStateError(f"no ledger at {self.ledger_path}")
        try:
            checkpoint = ShadowCheckpoint.model_validate_json(self.checkpoint_path.read_text("utf-8"))
        except (ValidationError, ValueError) as exc:
            raise ShadowStateError(f"invalid checkpoint {self.checkpoint_path}: {exc}") from exc
        if checkpoint.feed_id != self.feed.feed_id or checkpoint.feed_digest != self.feed_digest:
            raise ShadowStateError("checkpoint belongs to a different feed (id or digest mismatch)")
        if checkpoint.policy_digest != self.policy_digest:
            raise ShadowStateError("checkpoint was written under a different mock policy")
        if checkpoint.ledger_file != LEDGER_FILE:
            raise ShadowStateError(f"checkpoint names an unexpected ledger file {checkpoint.ledger_file!r}")
        total = len(self.feed.deliveries)
        if checkpoint.cursor > total:
            raise ShadowStateError(f"checkpoint cursor {checkpoint.cursor} exceeds {total} deliveries")
        self.coordinator = PaperEventReplay(self.ledger_path, self.policy)
        self.cursor = checkpoint.cursor
        self._rebuild_context(check_ledger=True)

    def _rebuild_context(self, *, check_ledger: bool) -> None:
        """Replay the committed prefix through the interpreter only (no ledger writes) so the
        point-in-time context equals the one the original run had. With ``check_ledger`` every
        prefix delivery that produced actions must have a recorded outcome, otherwise the ledger is
        behind the checkpoint (for example, replaced by an empty database) and resuming would skip
        events."""
        self.interpreter = TklInterpreter()
        for index in range(self.cursor):
            post = self._materialize(index)
            interpreted = self.interpreter.interpret(post)
            if check_ledger and interpreted.parsed.actions \
                    and not self.coordinator.outcomes(post.source_id, post.revision):
                raise ShadowStateError(
                    f"checkpoint cursor {self.cursor} is ahead of the ledger: delivery {index} "
                    f"({self.feed.deliveries[index].post_key}) has no recorded outcome"
                )
            self.interpreter.observe(post, interpreted)

    # ------------------------------------------------------------------ materialisation

    def _materialize(self, index: int) -> SourcePost:
        delivery = self.feed.deliveries[index]
        spec = self.feed.post(delivery.post_key)
        published = self.feed.virtual_time(spec.published_offset_s)
        revised = (self.feed.virtual_time(spec.revised_offset_s)
                   if spec.revised_offset_s is not None else None)
        post_id = spec.source_id if spec.revision == 1 else f"{spec.source_id}-v{spec.revision}"
        return SourcePost(
            source_id=spec.source_id, post_id=post_id, revision=spec.revision,
            author_id=SYNTHETIC_AUTHOR, verified_author=spec.verified_author,
            text=delivery.text_override or spec.text, text_complete=spec.text_complete,
            is_repost=spec.is_repost, published_at=published, revised_at=revised,
            received_at=self.feed.virtual_time(delivery.offset_s), provenance=Provenance.SYNTHETIC,
        )

    def _quotes(self, spec: ShadowPost, now: datetime) -> dict[str, Quote]:
        half = Decimal(self.feed.half_spread)
        return {
            symbol: Quote(symbol=symbol, bid=Decimal(mid) - half, ask=Decimal(mid) + half, observed_at=now)
            for symbol, mid in spec.quotes.items()
        }

    # ------------------------------------------------------------------ checkpoint

    def _write_checkpoint(self) -> None:
        """Atomic: write a sibling temp file, then rename over the checkpoint."""
        virtual_now = (self.feed.virtual_time(self.feed.deliveries[self.cursor - 1].offset_s)
                       if self.cursor > 0 else None)
        checkpoint = ShadowCheckpoint(
            feed_id=self.feed.feed_id, feed_digest=self.feed_digest, policy_digest=self.policy_digest,
            ledger_file=LEDGER_FILE, cursor=self.cursor, virtual_now=virtual_now,
        )
        temp = self.checkpoint_path.with_name(CHECKPOINT_FILE + ".tmp")
        temp.write_text(checkpoint.model_dump_json(indent=2), encoding="utf-8")
        os.replace(temp, self.checkpoint_path)

    def read_checkpoint(self) -> ShadowCheckpoint:
        return ShadowCheckpoint.model_validate_json(self.checkpoint_path.read_text("utf-8"))

    # ------------------------------------------------------------------ run

    def run(self, *, max_deliveries: int | None = None, max_wall_seconds: float | None = None,
            interrupt: InterruptSpec | None = None,
            on_delivery: Callable[[DeliveryRecord], None] | None = None) -> ShadowRunReport:
        """Deliver from the checkpoint cursor to the end of the finite feed, or until a bound stops
        the run. Raises ``ShadowInterrupted`` when the injected interruption fires; the checkpoint on
        disk then still points at the last delivery whose checkpoint write completed."""
        if max_deliveries is not None and max_deliveries < 1:
            raise ValueError("max_deliveries must be >= 1")
        if max_wall_seconds is not None and max_wall_seconds <= 0:
            raise ValueError("max_wall_seconds must be > 0")
        started = self.wall_clock()
        start_cursor = self.cursor
        stats = DeliveryStats()
        total = len(self.feed.deliveries)
        status: RunStatus = "COMPLETE"
        while self.cursor < total:
            if max_deliveries is not None and stats.processed >= max_deliveries:
                status = "DELIVERY_CAP_REACHED"
                break
            if max_wall_seconds is not None and self.wall_clock() - started > max_wall_seconds:
                status = "WALL_BUDGET_EXHAUSTED"
                break
            index = self.cursor
            if interrupt is not None and interrupt.delivery_index == index \
                    and interrupt.phase == "before_commit":
                raise ShadowInterrupted(index, "before_commit", self.cursor)
            record = self._deliver(index)
            stats.processed += 1
            if record.result.duplicate:
                hash_changed = any("SOURCE_HASH_CHANGED_WITHOUT_REVISION" in o.reasons
                                   for o in record.result.outcomes)
                if hash_changed:
                    stats.hash_mismatch_blocks += 1
                else:
                    stats.duplicate_returns += 1
            else:
                stats.fresh_results += 1
            if on_delivery is not None:
                on_delivery(record)
            if interrupt is not None and interrupt.delivery_index == index \
                    and interrupt.phase == "after_commit":
                # The ledger transaction committed; the checkpoint write is deliberately skipped.
                raise ShadowInterrupted(index, "after_commit", self.cursor)
            self.cursor = index + 1
            self._write_checkpoint()
        return self.report(status=status, resumed_from=start_cursor, stats=stats)

    def _deliver(self, index: int) -> DeliveryRecord:
        delivery = self.feed.deliveries[index]
        spec = self.feed.post(delivery.post_key)
        now = self.feed.virtual_time(delivery.offset_s)
        post = self._materialize(index)
        interpreted = self.interpreter.interpret(post)
        parsed = group_linked_actions(interpreted)
        result = self.coordinator.process(post.to_replay_source(), parsed, self._quotes(spec, now), now=now)
        self.interpreter.observe(post, interpreted)
        return DeliveryRecord(index=index, post_key=delivery.post_key, note=delivery.note,
                              virtual_now=now, post=post, interpreted=interpreted, result=result)

    # ------------------------------------------------------------------ report (ledger-derived)

    def ledger_summary(self) -> LedgerSummary:
        """Read back from SQLite: ideas, the latest recorded outcome per delivered post, orders."""
        ideas = [LedgerIdea.model_validate(row) for row in self.coordinator.ideas()]
        seen: set[tuple[str, int]] = set()
        posts: list[PostOutcome] = []
        orders: list[RecordedOrder] = []
        outcome_counts: Counter[str] = Counter()
        reason_counts: Counter[str] = Counter()
        for delivery in self.feed.deliveries[:self.cursor]:
            spec = self.feed.post(delivery.post_key)
            identity = (spec.source_id, spec.revision)
            if identity in seen:
                continue
            seen.add(identity)
            outcomes = self.coordinator.outcomes(spec.source_id, spec.revision)
            posts.append(PostOutcome(source_id=spec.source_id, revision=spec.revision,
                                     statuses=[o.status for o in outcomes],
                                     reasons=[list(o.reasons) for o in outcomes]))
            for outcome in outcomes:
                outcome_counts[outcome.status] += 1
                for reason in outcome.reasons:
                    reason_counts[reason] += 1
                if outcome.order is not None:
                    o = outcome.order
                    orders.append(RecordedOrder(order_id=o.order_id, idea_id=o.idea_id, action=o.action.value,
                                                symbol=o.symbol, side=o.side, quantity=o.quantity,
                                                price=o.price, status=o.status))
        return LedgerSummary(ideas=ideas, orders=orders, posts=posts,
                             outcome_counts=dict(sorted(outcome_counts.items())),
                             reason_counts=dict(sorted(reason_counts.items())))

    def report(self, *, status: RunStatus, resumed_from: int, stats: DeliveryStats) -> ShadowRunReport:
        ledger = self.ledger_summary()
        return ShadowRunReport(
            feed_id=self.feed.feed_id, feed_digest=self.feed_digest, policy_digest=self.policy_digest,
            deliveries_total=len(self.feed.deliveries), resumed_from=resumed_from, cursor=self.cursor,
            status=status, this_run=stats, ledger=ledger, convergence_digest=ledger.digest(),
        )


# ----------------------------------------------------------------------------- the synthetic feed

SHADOW_EPOCH = datetime(2026, 11, 6, 14, 0, tzinfo=UTC)
MIN = 60


def shadow_policy() -> MockPolicy:
    """Illustrative mock limits for the shadow run. Not a trading setting."""
    return MockPolicy(quantity=4, allowed_symbols=frozenset({"ZQX", "VBN"}))


def synthetic_feed() -> ShadowFeed:
    """Newly authored synthetic feed. Tickers ZQX, VBN and KPL are invented; so is every price.

    Eleven post versions, seventeen deliveries. The lifecycle covers OPEN -> conditional HOLD ->
    REDUCE -> commentary NO_TRADE -> CLOSE -> past-tense RECONCILE on one ticker, a short OPEN -> late
    MODIFY (stale BLOCK) -> cover CLOSE on another, an edited revision (HOLD) and an off-allowlist
    OPEN (BLOCK). Deliveries include immediate, late and post-close duplicates, a re-delivery of a
    blocked post, and a tampered re-delivery (same identity, different bytes).
    """
    posts = [
        ShadowPost(key="e01", source_id="e01", published_offset_s=0,
                   text="We are now long $ZQX at $41.20. Stop: $39.80. Target: $45.00.",
                   quotes={"ZQX": "41.20"}),
        ShadowPost(key="e02", source_id="e02", published_offset_s=60 * MIN,
                   text="If $ZQX breaks above $43.00 we will add to the position.",
                   quotes={"ZQX": "42.40"}),
        ShadowPost(key="e03", source_id="e03", published_offset_s=120 * MIN,
                   text="Taking profits on half of our $ZQX long at $43.60.",
                   quotes={"ZQX": "43.60"}),
        ShadowPost(key="e04", source_id="e04", published_offset_s=180 * MIN,
                   text="We remain bullish on $ZQX and expect a move toward $45.00.",
                   quotes={"ZQX": "43.90"}),
        ShadowPost(key="e05", source_id="e05", published_offset_s=240 * MIN,
                   text="Closing the remaining $ZQX long here at $44.70.",
                   quotes={"ZQX": "44.70"}),
        ShadowPost(key="e06", source_id="e06", published_offset_s=245 * MIN,
                   text="We have closed our $ZQX long at $44.70 for a +8.5% gain.",
                   quotes={"ZQX": "44.70"}),
        ShadowPost(key="e07", source_id="e07", published_offset_s=300 * MIN,
                   text="We are now short $VBN at $120.50. Stop: $124.00. Target: $112.00.",
                   quotes={"VBN": "120.50"}),
        ShadowPost(key="e08", source_id="e08", published_offset_s=360 * MIN,
                   text="Lowering our $VBN stop to $122.00.",
                   quotes={"VBN": "119.10"}),
        ShadowPost(key="e09", source_id="e09", published_offset_s=420 * MIN,
                   text="Covering our $VBN short here at $118.40.",
                   quotes={"VBN": "118.40"}),
        ShadowPost(key="e05-v2", source_id="e05", revision=2, published_offset_s=240 * MIN,
                   revised_offset_s=500 * MIN,
                   text="Closing the remaining $ZQX long here at $44.75.",
                   quotes={"ZQX": "44.75"}),
        ShadowPost(key="e10", source_id="e10", published_offset_s=540 * MIN,
                   text="We are now long $KPL at $18.30. Stop: $17.60. Target: $20.00.",
                   quotes={"KPL": "18.30"}),
    ]
    lag = 5  # seconds between virtual publication and a timely delivery
    deliveries = [
        ShadowDelivery(post_key="e01", offset_s=0 + lag, note="fresh OPEN long"),
        ShadowDelivery(post_key="e01", offset_s=0 + lag + 3, note="immediate duplicate of the OPEN"),
        ShadowDelivery(post_key="e02", offset_s=60 * MIN + lag, note="fresh conditional add -> HOLD"),
        ShadowDelivery(post_key="e03", offset_s=120 * MIN + lag, note="fresh REDUCE half"),
        ShadowDelivery(post_key="e03", offset_s=125 * MIN, note="late duplicate of the REDUCE"),
        ShadowDelivery(post_key="e04", offset_s=180 * MIN + lag, note="fresh commentary -> NO_TRADE"),
        ShadowDelivery(post_key="e05", offset_s=240 * MIN + lag, note="fresh CLOSE remaining"),
        ShadowDelivery(post_key="e01", offset_s=241 * MIN, note="OPEN replayed after the close"),
        ShadowDelivery(post_key="e06", offset_s=245 * MIN + lag, note="fresh past-tense exit -> RECONCILE"),
        ShadowDelivery(post_key="e07", offset_s=300 * MIN + lag, note="fresh OPEN short"),
        ShadowDelivery(post_key="e08", offset_s=380 * MIN + lag, note="MODIFY delivered 20 min late -> BLOCK"),
        ShadowDelivery(post_key="e08", offset_s=381 * MIN, note="re-delivery of the blocked MODIFY"),
        ShadowDelivery(post_key="e09", offset_s=420 * MIN + lag, note="fresh cover CLOSE"),
        ShadowDelivery(post_key="e05-v2", offset_s=500 * MIN + lag, note="edited revision -> HOLD"),
        ShadowDelivery(post_key="e10", offset_s=540 * MIN + lag, note="off-allowlist OPEN -> BLOCK"),
        ShadowDelivery(post_key="e05", offset_s=600 * MIN,
                       text_override="Closing the remaining $ZQX long here at $44.71.",
                       note="tampered re-delivery of the CLOSE (same identity, different bytes)"),
        ShadowDelivery(post_key="e09", offset_s=700 * MIN, note="late duplicate of the cover"),
    ]
    return ShadowFeed(feed_id="shadow-01-synthetic", epoch=SHADOW_EPOCH, posts=posts, deliveries=deliveries)
