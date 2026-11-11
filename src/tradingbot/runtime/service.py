"""The runtime service: source -> interpretation -> mapping -> gateway -> broker -> reconciliation.

One cycle (``run_once``):

1. Halt switch: read and, on a change, journal and notify. While it is engaged the coordinator
   refuses every submit or amendment (intents end HALTED), cancels working entries and still
   sizes protective orders; posts are still read and journaled.
2. Broker probe: account and open orders must be readable, otherwise nothing is consumed from the
   source this cycle (a signal is never burned on a broker outage) and the broker backs off.
3. Source poll. For each new post, in order: ``TklInterpreter.interpret`` ->
   ``group_linked_actions``
   -> ``MappingPolicy.apply`` (unmapped or non-DIRECT assets are marked for HOLD) ->
   ``BrokerEventReplay.process`` (``recover()`` first, then ``gate_source`` / ``gate_action`` and
   the ``ExecutionCoordinator``) -> ``interpreter.observe`` -> one journal transaction (signal,
   actions, held-signal exceptions, interpreter context) -> ``source.commit(post)``. A failure stops
   the cycle before the cursor moves past the failing post.
4. Reconciliation (read-only), alerts and notifications, a broker snapshot and the heartbeat.

The service decides nothing itself. Every order comes from an action the gateway approved through
the replay path; the mapping policy can only add a HOLD reason.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta

from tradingbot.event_models import ParsedPost
from tradingbot.execution.broker import Broker, BrokerError, QuoteQuality
from tradingbot.execution.coordinator import (
    PROTECTION_UNVERIFIED,
    ExecutionConfig,
    ExecutionCoordinator,
)
from tradingbot.execution.coordinator import (
    UNPROTECTED_POSITION as EXEC_UNPROTECTED,
)
from tradingbot.execution.reconcile import DiscrepancyKind, ReconciliationReport
from tradingbot.execution.replay import BrokerEventReplay
from tradingbot.execution.store import UNRESOLVED_STATES
from tradingbot.models import Quote
from tradingbot.runtime import notify as N
from tradingbot.runtime.config import RuntimeConfig
from tradingbot.runtime.mapping import MappingDecision, MappingPolicy
from tradingbot.runtime.notify import Notifier
from tradingbot.runtime.sources import SourceAdapter, SourceError
from tradingbot.runtime.store import RuntimeStore, write_json_atomic
from tradingbot.source_events import SourcePost
from tradingbot.tkl_interpreter import LexiconExtension, TklInterpreter, group_linked_actions

# Outcomes that mean "this signal was not traded as instructed": each becomes an exception record.
HELD_STATUSES = frozenset(
    {"HOLD", "BLOCK", "REFUSED", "HALTED", "UNRESOLVED", "EXCEPTION", "REJECTED"}
)
COMPONENTS = ("source", "broker", "pipeline")
FAILURE_KIND = {
    "source": N.SOURCE_FAILURE,
    "broker": N.BROKER_FAILURE,
    "pipeline": "PIPELINE_FAILURE",
}


@dataclass
class Health:
    last_success_at: str | None = None
    last_error: str | None = None
    last_error_at: str | None = None
    consecutive_failures: int = 0
    next_attempt_at: str | None = None  # set only while backing off after a failure

    def due(self, now: datetime) -> bool:
        return self.next_attempt_at is None or now >= datetime.fromisoformat(self.next_attempt_at)


@dataclass
class CycleReport:
    started_at: datetime
    halted: bool = False
    skipped: str | None = None
    polled: bool = False
    posts_processed: int = 0
    duplicates: int = 0
    held_actions: int = 0
    faults: list[str] = field(default_factory=list)
    reconciliation_clean: bool | None = None
    unexplained: list[str] = field(default_factory=list)


class _StampingReplay(BrokerEventReplay):
    """BrokerEventReplay that records when the gateway decided each action (no behaviour change)."""

    def __init__(self, db_path, coordinator, clock):
        super().__init__(db_path, coordinator)
        self._clock = clock
        self.gate_stamps: dict[str, datetime] = {}

    def _simulate(self, source, a, quotes, ideas, now):
        out = super()._simulate(source, a, quotes, ideas, now)
        self.gate_stamps[a.action_id] = self._clock()
        return out


def _safe_error(exc: BaseException) -> str:
    """Typed source and broker errors carry sanitized messages; anything else may echo input (for
    example a validation error quoting post text), so only its type is kept."""
    if isinstance(exc, (SourceError, BrokerError)):
        return f"{type(exc).__name__}: {exc}"[:300]
    return type(exc).__name__


class RuntimeService:
    def __init__(
        self,
        config: RuntimeConfig,
        *,
        source: SourceAdapter,
        broker: Broker,
        notifier: Notifier | None = None,
        clock: Callable[[], datetime] | None = None,
        store: RuntimeStore | None = None,
    ):
        if config.mode != "mock":
            raise ValueError("only mock mode may run")
        self.config = config
        self.clock = clock or (lambda: datetime.now(UTC))
        self.source = source
        self.broker = broker
        self.notifier = notifier or N.LogFileNotifier(config.notification_log, self.clock)
        config.paths.state_dir.mkdir(parents=True, exist_ok=True)
        self.store = store or RuntimeStore(config.runtime_db)
        self.mapping = MappingPolicy.from_toml(config.paths.mapping_file)
        self.coordinator = ExecutionCoordinator(
            broker,
            config.execution_db,
            config.policy,
            ExecutionConfig(
                halt_file=config.paths.halt_file,
                expected_account=config.execution.expected_account,
                max_submit_attempts=config.execution.max_submit_attempts,
                stuck_after_seconds=config.service.stuck_after_seconds,
                settle_seconds=config.execution.settle_seconds,
                exit_no_trace_lookups=config.execution.exit_no_trace_lookups,
                max_intent_age_seconds=config.execution.max_intent_age_seconds,
                max_exit_intent_age_seconds=config.execution.max_exit_intent_age_seconds,
                lookup_horizon_seconds=config.execution.lookup_horizon_seconds,
                confirm_attempts=config.execution.confirm_attempts,
                confirm_wait_seconds=config.execution.confirm_wait_seconds,
            ),
            clock=self.clock,
        )
        try:
            self.replay = _StampingReplay(config.replay_db, self.coordinator, self.clock)
            lexicon = None
            if config.interpreter.lexicon_path is not None:
                lexicon = LexiconExtension.from_json(config.interpreter.lexicon_path)
            self.interpreter = TklInterpreter(context=self.store.load_context(), lexicon=lexicon)
        except BaseException:
            self.coordinator.close()
            raise
        saved = self.store.get("health", {}) or {}
        self.health = {c: Health(**saved.get(c, {})) for c in COMPONENTS}
        self.cycles = int(self.store.get("cycles", 0) or 0)
        self.last_reconciliation: ReconciliationReport | None = None
        now = self.clock()
        self.store.event(
            now,
            "SERVICE_START",
            pid=os.getpid(),
            source=source.name,
            cursor=source.cursor(),
            restart=self.cycles > 0,
        )

    # ================================================================== cycle

    def close(self) -> None:
        """Release the execution store lock (one live coordinator per store)."""
        self.coordinator.close()

    @property
    def halted(self) -> bool:
        return self.coordinator.halted()

    def run_once(self) -> CycleReport:
        now = self.clock()
        report = CycleReport(started_at=now)
        self.cycles += 1
        report.halted = self._check_halt(now)
        broker_ok = False
        if not self.health["broker"].due(now):
            report.skipped = "broker_backoff"
        else:
            broker_ok = self._probe_broker(now)
            if not broker_ok:
                report.skipped = "broker_unavailable"
        if broker_ok:
            broker_ok = self._recover(now)
            if not broker_ok:
                report.skipped = "broker_unavailable"
        if broker_ok:
            self._refresh_runtime_exceptions(now)
            if not (self.health["source"].due(now) and self.health["pipeline"].due(now)):
                report.skipped = "source_backoff"
            else:
                self._poll_and_process(now, report)
            # Read-only; skipped while the broker is unreachable (the last report stays in view).
            self._reconcile(report)
        self._heartbeat(report)
        return report

    def run_forever(
        self,
        *,
        stop: Callable[[], bool] = lambda: False,
        sleep: Callable[[float], None] | None = None,
        max_cycles: int | None = None,
    ) -> None:
        import time

        sleep = sleep or time.sleep
        n = 0
        while not stop():
            self.run_once()
            n += 1
            if max_cycles is not None and n >= max_cycles:
                return
            sleep(self.next_delay())

    def next_delay(self) -> float:
        """Seconds until the next cycle: the source cadence (slowed by rate limits), or sooner when
        a backoff ends sooner; never below 50 ms."""
        cadence = self.config.service.cadence_seconds
        delay = self.source.next_poll_delay(cadence)
        now = self.clock()
        for h in self.health.values():
            if h.next_attempt_at is not None:
                wait = (datetime.fromisoformat(h.next_attempt_at) - now).total_seconds()
                delay = max(delay, wait) if wait > 0 else delay
        return max(0.05, delay)

    # ================================================================== stages

    def _check_halt(self, now: datetime) -> bool:
        halted = self.halted
        if halted:
            if self.store.open_alert("HALT", N.HALT_ENGAGED, str(self.config.paths.halt_file), now):
                self.store.event(now, "HALT_ENGAGED", halt_file=str(self.config.paths.halt_file))
                self.notifier.notify(
                    N.HALT_ENGAGED,
                    "Emergency halt engaged",
                    "Halt file present: no new order or amendment; working entries are "
                    "cancelled and protective orders still follow the position.",
                )
        elif self.store.resolve_alerts(now, keep=set(), prefix="HALT"):
            self.store.event(now, "HALT_CLEARED")
        return halted

    def _probe_broker(self, now: datetime) -> bool:
        try:
            # Every read the coordinator depends on (review F1), not only open orders.
            self.broker.account_id()
            self.broker.open_orders()
            self.broker.executions()
            self.broker.completed_orders(since=now)
            self.broker.positions()
            if self.source.realtime:
                self._publish_mock_quotes(now)
        except Exception as exc:  # noqa: BLE001 - any broker fault is a broker failure
            self._failure("broker", exc, now)
            return False
        self._success("broker", now)
        return True

    def _recover(self, now: datetime) -> bool:
        """Every cycle, not only when a post arrives: intents of unknown outcome are looked up
        again (they settle over time) and broker fills such as a triggered stop are applied."""
        try:
            self.coordinator.recover()
        except Exception as exc:  # noqa: BLE001
            self._failure("broker", exc, now)
            return False
        return True

    def _publish_mock_quotes(self, at: datetime) -> None:
        """Mock mode only: a static simulated market, republished so quotes stay fresh."""
        set_quote = getattr(self.broker, "set_quote", None)
        if set_quote is None:
            return
        for symbol, q in self.config.mock.quotes.items():
            set_quote(symbol, q.bid, q.ask, received_at=at)

    def _poll_and_process(self, now: datetime, report: CycleReport) -> None:
        report.polled = True
        try:
            posts = self.source.poll()
        except SourceError as exc:
            self._failure("source", exc, now, retry_after=exc.retry_after)
            report.faults.append(_safe_error(exc))
            return
        except Exception as exc:  # noqa: BLE001
            self._failure("source", exc, now)
            report.faults.append(_safe_error(exc))
            return
        self._success("source", now)
        for post in posts:
            try:
                held, duplicate = self._handle(post)
            except Exception as exc:  # noqa: BLE001 - recorded; the cursor stays before this post
                component = "broker" if isinstance(exc, BrokerError) else "pipeline"
                self.store.event(
                    self.clock(),
                    "POST_PROCESSING_FAILED",
                    source_id=post.source_id,
                    revision=post.revision,
                    post_id=post.post_id,
                    error=_safe_error(exc),
                )
                self._failure(component, exc, self.clock())
                report.faults.append(_safe_error(exc))
                return
            self.source.commit(post)
            report.posts_processed += 1
            report.held_actions += held
            report.duplicates += int(duplicate)
        self._success("pipeline", now)

    def _handle(self, post: SourcePost) -> tuple[int, bool]:
        """One post through the whole pipeline. Returns (held action count, duplicate)."""
        if self.source.realtime or post.is_live:
            decision_now = self.clock()
        else:
            # Historical replay: decide as of the moment the post became available, exactly as
            # tkl_interpreter.replay_posts does; the mock market is stamped at that moment too.
            lag = getattr(self.config.source, "lag_seconds", 0.0)
            decision_now = post.available_at + timedelta(seconds=lag)
            self._publish_mock_quotes(decision_now)
        interpreted = self.interpreter.interpret(post)
        interpreted_at = self.clock()
        mapped, decisions = self.mapping.apply(group_linked_actions(interpreted))
        quotes = self._gate_quotes(mapped, decisions)
        self.replay.gate_stamps = {}
        result = self.replay.process(post.to_replay_source(), mapped, quotes, now=decision_now)
        decided_at = self.clock()
        self.interpreter.observe(post, interpreted)

        by_id = {a.action_id: a for a in mapped.actions}
        decision_by_id = {d.action_id: d for d in decisions}
        rows, exceptions = [], []
        source_block = result.duplicate and any(o.action_id == "_source" for o in result.outcomes)
        for o in result.outcomes:
            a = by_id.get(o.action_id)
            d = decision_by_id.get(o.action_id)
            stamp = (
                None if result.duplicate else self.replay.gate_stamps.get(o.action_id, decided_at)
            )
            rows.append(
                {
                    "action_id": o.action_id,
                    "action": a.action.value if a else None,
                    "timing": a.timing.value if a else None,
                    "asset": a.source_market_symbol if a else None,
                    "side": a.position_side.value if a and a.position_side else None,
                    "mapping_status": d.status if d else None,
                    "mapping_basis": d.basis if d else None,
                    "mapping_reason": d.reason if d else None,
                    "broker_symbol": d.broker_symbol if d else None,
                    "status": o.status,
                    "reasons": _json(o.reasons),
                    "order_ref": o.order_ref,
                    "gate_decided_at": stamp.isoformat() if stamp else None,
                }
            )
            if o.status in HELD_STATUSES and (not result.duplicate or source_block):
                exceptions.append(
                    {
                        "kind": f"SIGNAL_{o.status}",
                        "source_id": post.source_id,
                        "revision": post.revision,
                        "action_id": o.action_id,
                        "order_ref": o.order_ref,
                        "reasons": list(o.reasons),
                        "detail": (a.action.value if a else "post") + " not traded",
                    }
                )
        signal = {
            "source_name": self.source.name,
            "source_id": post.source_id,
            "post_id": post.post_id,
            "revision": post.revision,
            "post_hash": post.text_sha256,
            "provenance": post.provenance.value,
            "verified_author": int(post.verified_author),
            "text_complete": int(post.text_complete),
            "text_len": len(post.text),
            "media_count": len(post.media),
            "links": _json(sorted({link.relation for link in post.links})),
            "published_at": post.published_at.isoformat(),
            "revised_at": post.revised_at.isoformat() if post.revised_at else None,
            "received_at": post.received_at.isoformat(),
            "interpreted_at": interpreted_at.isoformat(),
            "decided_at": decided_at.isoformat(),
            "decision_now": decision_now.isoformat(),
            "duplicate": int(result.duplicate),
            "hydrated": int(result.hydrated),
        }
        self.store.record_signal(
            signal=signal,
            actions=rows,
            exceptions=exceptions,
            context=self.interpreter.context,
            at=decided_at,
        )
        return len(exceptions), result.duplicate

    def _gate_quotes(
        self, parsed: ParsedPost, decisions: list[MappingDecision]
    ) -> dict[str, Quote]:
        """The broker's own quote for every instrument the mapping policy lets through. A missing or
        unusable quote is simply absent; the gateway then blocks with QUOTE_UNAVAILABLE."""
        supported = {d.action_id for d in decisions if d.status == "SUPPORTED"}
        quotes: dict[str, Quote] = {}
        for a in parsed.actions:
            if a.action_id not in supported or a.instrument is None:
                continue
            symbol = a.instrument.symbol
            if symbol in quotes:
                continue
            try:
                q = self.broker.quote(self.broker.resolve(a.instrument))
            except BrokerError:
                continue
            if q.quality == QuoteQuality.UNAVAILABLE or q.bid is None or q.ask is None:
                continue
            quotes[symbol] = Quote(symbol=symbol, bid=q.bid, ask=q.ask, observed_at=q.received_at)
        return quotes

    def _refresh_runtime_exceptions(self, now: datetime) -> None:
        """A signal held as UNRESOLVED is closed once its intent's outcome is known."""
        for e in self.store.exceptions(open_only=True):
            if e["kind"] != "SIGNAL_UNRESOLVED" or not e["order_ref"]:
                continue
            intent = self.coordinator.intent(e["order_ref"])
            if intent is not None and intent.state not in UNRESOLVED_STATES:
                self.store.resolve_exception(e["id"], now, f"intent {intent.state.value}")

    def _reconcile(self, report: CycleReport) -> None:
        now = self.clock()
        rec = self.coordinator.reconcile(now)
        self.last_reconciliation = rec
        unreadable = any(d.kind == DiscrepancyKind.BROKER_UNREADABLE for d in rec.discrepancies)
        if unreadable:
            detail = next(d.detail for d in rec.discrepancies)
            self._failure("broker", BrokerError(detail), now)
        report.reconciliation_clean = rec.clean
        report.unexplained = [d.kind.value for d in rec.blocking]
        keep_recon: set[str] = set()
        # An explained safety discrepancy already alerts through its exception (below).
        for d in rec.unexplained:
            if d.kind == DiscrepancyKind.BROKER_UNREADABLE:
                continue
            key = f"RECON:{d.kind.value}:{d.symbol}:{d.idea_id}:{d.order_ref}"
            keep_recon.add(key)
            kind = {
                DiscrepancyKind.INTENT_STUCK_UNRESOLVED: N.STUCK_INTENT,
                DiscrepancyKind.POSITION_WITHOUT_PROTECTIVE_STOP: N.UNPROTECTED_POSITION,
            }.get(d.kind, N.RECONCILIATION_DISCREPANCY)
            if self.store.open_alert(key, kind, d.detail, now):
                self.notifier.notify(
                    kind,
                    d.kind.value.replace("_", " ").capitalize(),
                    f"{d.symbol or '-'} idea {d.idea_id or '-'} ref {d.order_ref or '-'}: "
                    f"{d.detail}",
                )
        if not unreadable:
            self.store.resolve_alerts(now, keep=keep_recon, prefix="RECON:")
        keep_unprot: set[str] = set()
        for e in self.coordinator.store.exceptions(open_only=True):
            if e.kind not in (EXEC_UNPROTECTED, PROTECTION_UNVERIFIED):
                continue
            key = f"UNPROTECTED:{e.idea_id}"
            keep_unprot.add(key)
            if self.store.open_alert(key, N.UNPROTECTED_POSITION, e.detail, now):
                self.notifier.notify(
                    N.UNPROTECTED_POSITION,
                    "Position without confirmed stop",
                    f"{e.idea_id}: {e.detail}",
                )
        self.store.resolve_alerts(now, keep=keep_unprot, prefix="UNPROTECTED:")
        snapshot: dict = {"at": now.isoformat(), "account": rec.account}
        if not unreadable:
            try:
                snapshot["positions"] = [p.model_dump(mode="json") for p in self.broker.positions()]
                snapshot["open_orders"] = [
                    o.model_dump(mode="json") for o in self.broker.open_orders()
                ]
            except Exception as exc:  # noqa: BLE001
                snapshot["error"] = _safe_error(exc)
        self.store.put("broker_snapshot", snapshot, now)
        self.store.put(
            "reconciliation",
            {
                "at": now.isoformat(),
                "clean": rec.clean,
                "account": rec.account,
                "ledger_ideas_open": rec.ledger_ideas_open,
                "broker_positions": rec.broker_positions,
                "broker_open_orders": rec.broker_open_orders,
                "discrepancies": [d.model_dump(mode="json") for d in rec.discrepancies],
            },
            now,
        )

    def _heartbeat(self, report: CycleReport) -> None:
        now = self.clock()
        rec = self.last_reconciliation
        payload = {
            "at": now.isoformat(),
            "mode": self.config.mode,
            "pid": os.getpid(),
            "cycle": self.cycles,
            "halted": report.halted,
            "halt_file": str(self.config.paths.halt_file),
            "last_cycle": {
                "started_at": report.started_at.isoformat(),
                "skipped": report.skipped,
                "posts_processed": report.posts_processed,
                "duplicates": report.duplicates,
                "held_actions": report.held_actions,
                "faults": report.faults,
            },
            "source": {
                "name": self.source.name,
                "realtime": self.source.realtime,
                "cursor": self.source.cursor(),
                **asdict(self.health["source"]),
            },
            "broker": {
                "account": rec.account if rec else None,
                **asdict(self.health["broker"]),
            },
            "pipeline": asdict(self.health["pipeline"]),
            "reconciliation": {
                "at": rec.checked_at.isoformat() if rec else None,
                "clean": rec.clean if rec else None,
                "unexplained": [d.kind.value for d in rec.blocking] if rec else [],
                "explained": (len(rec.discrepancies) - len(rec.blocking)) if rec else 0,
            },
        }
        self.store.put("health", {c: asdict(h) for c, h in self.health.items()}, now)
        self.store.put("cycles", self.cycles, now)
        self.store.put("heartbeat", payload, now)
        write_json_atomic(self.config.heartbeat_file, payload)

    # ================================================================== health

    def _failure(
        self, component: str, exc: BaseException, now: datetime, *, retry_after: float | None = None
    ) -> None:
        h = self.health[component]
        h.consecutive_failures += 1
        h.last_error = _safe_error(exc)
        h.last_error_at = now.isoformat()
        cfg = self.config.service
        backoff = min(
            cfg.cadence_seconds * 2 ** (h.consecutive_failures - 1), cfg.max_backoff_seconds
        )
        if retry_after is not None:
            backoff = min(max(backoff, retry_after), max(cfg.max_backoff_seconds, retry_after))
        h.next_attempt_at = (now + timedelta(seconds=backoff)).isoformat()
        self.store.event(
            now,
            f"{component.upper()}_FAILURE",
            error=h.last_error,
            consecutive=h.consecutive_failures,
            backoff_seconds=backoff,
        )
        if h.consecutive_failures >= cfg.failure_alert_after:
            kind = FAILURE_KIND[component]
            if self.store.open_alert(f"FAILURE:{component}", kind, h.last_error, now):
                self.notifier.notify(
                    kind,
                    f"Repeated {component} failure",
                    f"{h.consecutive_failures} consecutive failures; last: {h.last_error}",
                )

    def _success(self, component: str, now: datetime) -> None:
        h = self.health[component]
        if h.consecutive_failures:
            self.store.event(now, f"{component.upper()}_RECOVERED", after=h.consecutive_failures)
        h.consecutive_failures = 0
        h.next_attempt_at = None
        h.last_success_at = now.isoformat()
        self.store.resolve_alerts(now, keep=set(), prefix=f"FAILURE:{component}")


def _json(value) -> str:
    return json.dumps(value, default=str)
