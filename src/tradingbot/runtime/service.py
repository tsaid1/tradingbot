"""The runtime service: source -> interpretation -> mapping -> gateway -> broker -> reconciliation.

One cycle (``run_once``):

1. Halt switch: read and, on a change, journal and notify. While it is engaged the coordinator
   refuses every submit or amendment (intents end HALTED), cancels working entries and still
   sizes protective orders; posts are still read and journaled.
2. Broker probe: account and open orders must be readable, otherwise nothing is consumed from the
   source this cycle (a signal is never burned on a broker outage) and the broker backs off.
3. Source poll. For each new post, in order: ``TklInterpreter.interpret`` ->
   ``group_linked_actions``
   -> ``MappingPolicy.apply`` (unmapped, unapproved or unresolved families are marked for HOLD;
   a supported one gets its execution instrument) -> the broker's quotes (a ``MappedQuote`` with
   both legs for a mapped family) ->
   ``BrokerEventReplay.process`` (``recover()`` first, then ``gate_source`` / ``gate_action`` and
   the ``ExecutionCoordinator``) -> ``interpreter.observe`` -> one journal transaction (signal,
   actions, held-signal exceptions, interpreter context) -> ``source.commit(post)``. A failure stops
   the cycle before the cursor moves past the failing post.
4. Reconciliation (read-only), verification of operator settlements, alerts and notifications, a
   broker snapshot and the heartbeat.

Before step 3, queued operator requests are applied (``runtime.operator_apply``; they only record
what a human established at the broker). Broker session policy (``docs/OPERATIONS.md``): any failed
probe marks the session lost (BROKER_RECONNECT_PENDING blocks every OPEN and ADD, protection becomes
UNKNOWN); reconnection is retried on the bounded broker backoff, re-running the adapter's identity
checks; a session under another account identity locks the service out of the broker and engages
the halt; otherwise a full sync and a reconciliation with nothing unexplained must succeed before
the block lifts, and what changed at the broker meanwhile is reported.

The service decides nothing itself. Every order comes from an action the gateway approved through
the replay path; the mapping policy can only add a HOLD reason.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from tradingbot.event_models import ParsedPost
from tradingbot.execution import settlement
from tradingbot.execution.broker import (
    Broker,
    BrokerError,
    BrokerIdentityError,
    ContractRef,
    QuoteQuality,
    broker_connected,
    reference_quote,
    session_identity,
)
from tradingbot.execution.coordinator import (
    BROKER_RECONNECT_PENDING,
    BROKER_STATE_UNKNOWN,
    ENTRY_STATE_UNKNOWN,
    EXIT_NOT_NETTED,
    EXIT_WITHOUT_QUOTE,
    OPERATOR_SETTLEMENT_UNVERIFIED,
    PROTECTION_UNVERIFIED,
    SETTLED_INTENT_CONTRADICTED,
    UNKNOWN_OUTCOME_BEYOND_LOOKUP_HORIZON,
    UNKNOWN_OUTCOME_ENTRY_NOT_RESENT,
    ExecutionConfig,
    ExecutionCoordinator,
)
from tradingbot.execution.coordinator import (
    UNPROTECTED_POSITION as EXEC_UNPROTECTED,
)
from tradingbot.execution.reconcile import DiscrepancyKind, ReconciliationReport
from tradingbot.execution.replay import BrokerEventReplay
from tradingbot.execution.store import LEDGER_REVERSAL, UNRESOLVED_STATES
from tradingbot.models import Action, Instrument, LegQuote, MappedQuote, Quote
from tradingbot.runtime import notify as N
from tradingbot.runtime.alerts import Alerter
from tradingbot.runtime.config import RuntimeConfig
from tradingbot.runtime.mapping import FamilyMapping, MappingDecision, MappingPolicy
from tradingbot.runtime.notify import Notifier
from tradingbot.runtime.operator_actions import OperatorQueue
from tradingbot.runtime.operator_apply import (
    CONNECTED,
    DISCONNECTED,
    LOCKED_OUT,
    SESSION_KEY,
    apply_pending,
    session_state,
)
from tradingbot.runtime.sources import SourceAdapter, SourceError
from tradingbot.runtime.store import RuntimeStore, write_json_atomic
from tradingbot.source_events import SourcePost
from tradingbot.tkl_interpreter import LexiconExtension, TklInterpreter, group_linked_actions
from tradingbot.validator import in_roll_window

# Outcomes that mean "this signal was not traded as instructed": each becomes an exception record.
HELD_STATUSES = frozenset(
    {"HOLD", "BLOCK", "REFUSED", "HALTED", "UNRESOLVED", "EXCEPTION", "REJECTED"}
)
COMPONENTS = ("source", "broker", "pipeline")
# A follower position in a futures contract inside its configured roll window (multi-asset).
ROLL_DUE = N.ROLL_DUE
RESYNCING = "RESYNCING"
# The alert kind each open execution exception raises (anything else: EXECUTION_EXCEPTION).
EXCEPTION_ALERT_KIND = {
    LEDGER_REVERSAL: N.REVERSAL,
    EXIT_NOT_NETTED: N.EXIT_REFUSED,
    EXIT_WITHOUT_QUOTE: N.EXIT_REFUSED,
    UNKNOWN_OUTCOME_ENTRY_NOT_RESENT: N.ENTRY_UNKNOWN_OUTCOME,
    UNKNOWN_OUTCOME_BEYOND_LOOKUP_HORIZON: N.ENTRY_UNKNOWN_OUTCOME,
    ENTRY_STATE_UNKNOWN: N.ENTRY_UNKNOWN_OUTCOME,
    OPERATOR_SETTLEMENT_UNVERIFIED: N.SETTLEMENT_UNVERIFIED,
    SETTLED_INTENT_CONTRADICTED: N.SETTLEMENT_CONTRADICTED,
}
# Alerted under their own keys (unprotected position, broker state), not as EXC:<id>.
SEPARATELY_ALERTED = frozenset(
    {"UNPROTECTED_POSITION", PROTECTION_UNVERIFIED, BROKER_STATE_UNKNOWN, BROKER_RECONNECT_PENDING}
)
# Held signals of these kinds for an exit or amendment are alerted as refused exits.
REFUSED_SIGNAL_KINDS = frozenset({"SIGNAL_REFUSED", "SIGNAL_REJECTED", "SIGNAL_HOLD", "SIGNAL_BLOCK"})
# Journal events that describe something adopted from the broker after a reconnect.
ADOPTED_EVENTS = frozenset(
    {"FILL", "INTENT_FILLED", "INTENT_CANCELLED", "INTENT_REJECTED", "ADOPTED_FROM_BROKER",
     "ENTRY_STATE_UNKNOWN", "ENTRY_REMAINDER_CANCELLED", "PROTECTIVE_CANCELLED", LEDGER_REVERSAL,
     "UNPROTECTED_POSITION"}
)
# Bounds the arithmetic only; max_backoff_seconds still bounds the wait itself.
MAX_BACKOFF_EXPONENT = 32
FAILURE_KIND = {
    "source": N.SOURCE_FAILURE,
    "broker": N.BROKER_FAILURE,
    "pipeline": N.PIPELINE_FAILURE,
}


def execution_config(config: RuntimeConfig) -> ExecutionConfig:
    """The coordinator's settings from the runtime config (service and operator CLI alike)."""
    return ExecutionConfig(
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
    )


def in_activity_window(window: str | None, now: datetime) -> bool:
    """``HH:MM-HH:MM`` UTC, Monday to Friday; None means always."""
    if window is None:
        return True
    if now.astimezone(UTC).weekday() >= 5:
        return False
    start, end = window.split("-")
    hm = now.astimezone(UTC).strftime("%H:%M")
    return start <= hm < end if start <= end else (hm >= start or hm < end)


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
        self.alerter = Alerter(
            self.store, self.notifier, realert_seconds=config.notify.realert_seconds,
            clock=self.clock,
        )
        self.coordinator = ExecutionCoordinator(
            broker, config.execution_db, config.policy, execution_config(config), clock=self.clock
        )
        try:
            self.queue = OperatorQueue(config.operator_db)
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
        # Review F7: a live post published before the service first ever started is never acted on.
        first = self.store.get("first_start_at")
        if first is None:
            self.store.put("first_start_at", now.isoformat(), now)
        self.first_start_at = datetime.fromisoformat(first) if first else now
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
        if session_state(self.store)["state"] == LOCKED_OUT:
            # Another account identity: no broker call at all (not even a cancel) until an
            # operator clears the lockout against the expected account.
            report.skipped = "identity_lockout"
            self._apply_operator_requests(locked_out=True)
            self._raise_alerts(self.clock(), report)
            self._heartbeat(report)
            return report
        broker_ok = False
        if not self.health["broker"].due(now):
            report.skipped = "broker_backoff"
        else:
            broker_ok = self._probe_broker(now)
            if not broker_ok:
                locked = session_state(self.store)["state"] == LOCKED_OUT
                report.skipped = "identity_lockout" if locked else "broker_unavailable"
        if broker_ok:
            broker_ok = self._recover(now)
            if not broker_ok:
                report.skipped = "broker_unavailable"
        if broker_ok and session_state(self.store)["state"] != CONNECTED:
            self._complete_resync(now)
        if broker_ok:
            self._apply_operator_requests()
            self._refresh_runtime_exceptions(now)
            if not (self.health["source"].due(now) and self.health["pipeline"].due(now)):
                report.skipped = "source_backoff"
            else:
                self._poll_and_process(now, report)
            # Read-only; skipped while the broker is unreachable (the last report stays in view).
            self._reconcile(report)
            settlement.lift_verified_blocks(self.coordinator)
            self._check_roll_windows(now)
        report.halted = self._check_halt(self.clock()) or report.halted
        self._raise_alerts(self.clock(), report)
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
            was_open = any(a["key"] == "HALT" for a in self.store.alerts(open_only=True))
            self.alerter.raise_(
                "HALT",
                N.HALT_ENGAGED,
                "Emergency halt engaged",
                "Halt file present: no new order or amendment; working entries are "
                "cancelled and protective orders still follow the position.",
                now,
            )
            if not was_open:
                self.store.event(now, "HALT_ENGAGED", halt_file=str(self.config.paths.halt_file))
        elif self.alerter.sweep("HALT", set(), now, cleared_kind=N.HALT_CLEARED,
                                cleared_title="Emergency halt cleared"):
            self.store.event(now, "HALT_CLEARED")
        return halted

    def _probe_broker(self, now: datetime) -> bool:
        try:
            if not broker_connected(self.broker):
                reconnect = getattr(self.broker, "reconnect", None)
                if not callable(reconnect):
                    raise BrokerError("broker session is down")
                self.store.event(now, "BROKER_RECONNECT_ATTEMPT",
                                 after_failures=self.health["broker"].consecutive_failures)
                reconnect()  # the adapter's own identity checks run again here
            identity = session_identity(self.broker)
        except BrokerIdentityError as exc:
            self._lockout(now, "IDENTITY_CHECK_FAILED", _safe_error(exc))
            return False
        except Exception as exc:  # noqa: BLE001 - any broker fault is a broker failure
            self._failure("broker", exc, now)
            self._session_lost(now, exc)
            return False
        problem = self._identity_problem(identity, now)
        if problem:
            self._lockout(now, problem, f"session account {identity.get('account')}")
            return False
        try:
            # Every read the coordinator depends on (review F1), not only open orders.
            self.broker.open_orders()
            self.broker.executions()
            self.broker.completed_orders(since=now)
            self.broker.positions()
            if self.source.realtime:
                self._publish_mock_quotes(now)
        except Exception as exc:  # noqa: BLE001 - any broker fault is a broker failure
            self._failure("broker", exc, now)
            self._session_lost(now, exc)
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
            self._session_lost(now, exc)
            return False
        return True

    # ================================================================== broker session

    def _identity_problem(self, identity: dict[str, str], now: datetime) -> str | None:
        """The session must be the configured account, and the same session identity as the last
        one this service accepted (an operator lockout clear re-accepts it)."""
        if identity.get("account") != self.config.execution.expected_account:
            return "ACCOUNT_MISMATCH"
        known = self.store.get("broker_identity")
        if known is None:
            self.store.put("broker_identity", identity, now)
            return None
        return None if known == identity else "SESSION_IDENTITY_CHANGED"

    def _session_lost(self, now: datetime, exc: BaseException) -> None:
        """The broker is unreadable: block new exposure until reconnect and resync complete."""
        session = session_state(self.store)
        if session["state"] == LOCKED_OUT:
            return
        if session["state"] == CONNECTED:
            mark = max((e.seq for e in self.coordinator.store.journal()), default=0)
            self.store.put(SESSION_KEY, {"state": DISCONNECTED, "since": now.isoformat(),
                                         "journal_mark": mark, "reason": _safe_error(exc)}, now)
            self.store.event(now, "BROKER_SESSION_LOST", error=_safe_error(exc))
        self.coordinator.store.raise_exception(
            now, BROKER_RECONNECT_PENDING,
            "broker session lost; every OPEN and ADD is refused until it is back, a full sync "
            "succeeded and a reconciliation shows nothing unexplained",
        )
        self.coordinator.mark_broker_unreadable("probe", BrokerError(_safe_error(exc)))

    def _complete_resync(self, now: datetime) -> None:
        """After a reconnect (recover() has just completed a full sync): reconcile, report what
        changed at the broker meanwhile, and lift the block only when nothing is unexplained."""
        session = session_state(self.store)
        rec = self.coordinator.reconcile(now)
        if any(d.kind == DiscrepancyKind.BROKER_UNREADABLE for d in rec.discrepancies):
            return
        if not session.get("reported"):
            changes = self._changes_since(int(session.get("journal_mark", 0)))
            down = (now - datetime.fromisoformat(session["since"])).total_seconds() \
                if session.get("since") else None
            self.store.event(now, "BROKER_RECONNECTED", down_seconds=down, **changes)
            self.alerter.event(
                f"EVENT:RECONNECTED:{now.isoformat()}",
                N.BROKER_RECONNECTED,
                "Broker session restored",
                f"down {down:.0f} s; adopted from broker evidence: "
                f"{len(changes['fills'])} fill(s) {changes['fills']}, ended {changes['ended']}"
                if down is not None else f"adopted: {changes}",
                now,
            )
            session = {**session, "state": RESYNCING, "reported": True}
            self.store.put(SESSION_KEY, session, now)
        unexplained = [d.kind.value for d in rec.unexplained]
        if unexplained:
            if session.get("waiting_on") != unexplained:
                self.store.event(now, "BROKER_RESYNC_WAITING", unexplained=unexplained)
                self.store.put(SESSION_KEY, {**session, "waiting_on": unexplained}, now)
            return
        self.coordinator.store.resolve_exceptions(now, BROKER_RECONNECT_PENDING, idea_id=None)
        self.store.put(SESSION_KEY, {"state": CONNECTED, "since": now.isoformat()}, now)
        self.store.event(now, "BROKER_RESYNCED", rule="full sync and reconciliation succeeded")

    def _changes_since(self, mark: int) -> dict:
        fills, ended = [], {}
        for e in self.coordinator.store.journal():
            if e.seq <= mark or e.event not in ADOPTED_EVENTS:
                continue
            if e.event == "FILL":
                fills.append(f"{e.order_ref}:{e.detail.get('quantity')}")
            elif e.event.startswith("INTENT_"):
                ended[e.order_ref] = e.event.removeprefix("INTENT_")
            elif e.order_ref:
                ended.setdefault(e.order_ref, e.event)
        return {"fills": fills, "ended": ended}

    def _lockout(self, now: datetime, reason: str, detail: str) -> None:
        """Another account identity (or a failed identity check): stop touching the broker, engage
        the halt, block exposure, alert. Only an operator request clears it."""
        session = session_state(self.store)
        mark = session.get("journal_mark") or max(
            (e.seq for e in self.coordinator.store.journal()), default=0
        )
        self.store.put(SESSION_KEY, {"state": LOCKED_OUT, "since": now.isoformat(),
                                     "reason": reason, "journal_mark": mark}, now)
        halt = self.config.paths.halt_file
        if not halt.exists():
            halt.parent.mkdir(parents=True, exist_ok=True)
            halt.write_text(f"engaged by the service at {now.isoformat()}: {reason}\n")
        self.coordinator.store.raise_exception(
            now, BROKER_RECONNECT_PENDING, f"broker identity lockout: {reason}"
        )
        self.store.event(now, "BROKER_IDENTITY_LOCKOUT", reason=reason, detail=detail)
        self._check_halt(now)

    def _apply_operator_requests(self, *, locked_out: bool = False) -> None:
        results = apply_pending(
            coordinator=self.coordinator, runtime_store=self.store, queue=self.queue,
            expected_account=self.config.execution.expected_account, clock=self.clock,
            locked_out=locked_out,
        )
        for r in results:
            self.alerter.event(
                f"EVENT:OPERATOR:{r['request_id']}",
                N.OPERATOR_ACTION,
                f"Operator {r['action']} {r['status']}",
                f"request {r['request_id']} {r['action']} {r['target']} {r['outcome'] or ''}: "
                f"{r['status']} {', '.join(r['reasons'])}".strip(),
            )

    def _publish_mock_quotes(self, at: datetime) -> None:
        """Mock mode only: a static simulated market, republished so quotes stay fresh."""
        set_quote = getattr(self.broker, "set_quote", None)
        if set_quote is None:
            return
        for symbol, q in self.config.mock.quotes.items():
            set_quote(symbol, q.bid, q.ask, received_at=at)

    def _poll_and_process(self, now: datetime, report: CycleReport) -> None:
        report.polled = True
        before = self.source.cursor()
        try:
            self._poll_and_process_inner(now, report)
        finally:
            after = self.source.cursor()
            if after != before or self.store.get("cursor_changed_at") is None:
                self.store.put("cursor_changed_at", self.clock().isoformat(), self.clock())

    def _poll_and_process_inner(self, now: datetime, report: CycleReport) -> None:
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
        if post.is_live and post.published_at < self.first_start_at:
            # Existed before the first run: consumed and journaled by identity, never acted on.
            self.store.event(
                self.clock(),
                "POST_BEFORE_FIRST_START",
                source_id=post.source_id,
                post_id=post.post_id,
                revision=post.revision,
                published_at=post.published_at.isoformat(),
                first_start_at=self.first_start_at.isoformat(),
            )
            return 0, False
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
        recorded = self._recorded_mappings()
        mapped, decisions = self.mapping.apply(
            group_linked_actions(interpreted),
            clauses={c.action_id: c.clause for c in interpreted.classifications},
            now=decision_now,
            held=self._held_contracts(),
            recorded=recorded,
            post_text=post.text,  # an option must be one leg across the whole post
        )
        quotes = self._gate_quotes(mapped, decisions, decision_now, recorded)
        self.replay.gate_stamps = {}
        source = post.to_replay_source()
        result = self.replay.process(source, mapped, quotes, now=decision_now)
        self._record_idea_mappings(source.source_id, mapped, decisions)
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
                    "mapping_family": d.family if d else None,
                    "broker_symbol": d.broker_symbol if d else None,
                    "status": o.status,
                    "reasons": _json(o.reasons),
                    "order_ref": o.order_ref,
                    "gate_decided_at": stamp.isoformat() if stamp else None,
                }
            )
            if (
                o.status == "HALTED"
                and a is not None
                and a.action in (Action.REDUCE, Action.CLOSE, Action.MODIFY)
                and not result.duplicate
            ):
                # Review F12: the stop at the broker stays; a human must know the exit was refused.
                self.notifier.notify(
                    N.HALTED_ACTION_REFUSED,
                    "Exit or amendment refused: halt engaged",
                    f"idea {a.idea_reference or '-'} action {a.action.value} ({o.action_id}) "
                    f"ref {o.order_ref or '-'}: {', '.join(o.reasons)}",
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

    def _held_contracts(self) -> dict[str, ContractRef]:
        """Each open follower idea's contract: a follow-up acts on its own idea's contract."""
        out = {}
        for p in self.coordinator.ledger():
            if p.contract and p.status in ("OPEN", "PENDING"):
                out[p.idea_id] = ContractRef.model_validate_json(p.contract)
        return out

    def _recorded_mappings(self) -> dict[str, FamilyMapping]:
        """The mapping entry each open idea was opened under (approval governs new exposure only:
        its exits and protective amendments keep working if the family loses approval)."""
        out = {}
        for p in self.coordinator.ledger():
            if p.mapping_terms and p.status in ("OPEN", "PENDING"):
                try:
                    out[p.idea_id] = FamilyMapping.model_validate_json(p.mapping_terms)
                except ValueError:  # unreadable terms: the idea's exits HOLD as before
                    continue
        return out

    def _record_idea_mappings(
        self, source_id: str, parsed: ParsedPost, decisions: list[MappingDecision]
    ) -> None:
        """Record on each idea opened by this post the version-2 entry it was opened under."""
        by_id = {a.action_id: a for a in parsed.actions}
        store = self.coordinator.store
        for d in decisions:
            a = by_id.get(d.action_id)
            entry = self.mapping.entries.get(d.family or "")
            if d.status != "SUPPORTED" or a is None or a.action != Action.OPEN \
                    or not isinstance(entry, FamilyMapping):
                continue
            idea_id = f"{source_id}:{a.action_id}"
            pos = store.position(idea_id)
            if pos is not None and pos.mapping_terms is None:
                store.update_position(idea_id, self.clock(), mapping_terms=entry.model_dump_json())

    def _gate_quotes(
        self,
        parsed: ParsedPost,
        decisions: list[MappingDecision],
        now: datetime | None = None,
        recorded: dict[str, FamilyMapping] | None = None,
    ) -> dict[str, Quote]:
        """The broker's own quote for every instrument the mapping policy lets through. A missing or
        unusable quote is simply absent; the gateway then blocks with QUOTE_UNAVAILABLE. A mapped
        family gets a ``MappedQuote``: its terms and both legs (the gateway decides on them)."""
        now = now or self.clock()
        supported = {d.action_id: d for d in decisions if d.status == "SUPPORTED"}
        quotes: dict[str, Quote] = {}
        for a in parsed.actions:
            d = supported.get(a.action_id)
            if d is None or a.instrument is None:
                continue
            if d.mapped:
                key = a.instrument.quote_key
                if key not in quotes:
                    if d.under_revoked_mapping:  # the idea's own recorded terms, exits only
                        entry, exits_only = (recorded or {})[a.idea_reference], True
                    else:
                        entry, exits_only = self.mapping.entries[d.family], False
                    quotes[key] = self._mapped_quote(entry, a.instrument, now,
                                                     exits_only=exits_only)
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

    def _mapped_quote(
        self, entry: FamilyMapping, instrument, now: datetime, *, exits_only: bool = False
    ) -> MappedQuote:
        """Resolve the execution and reference contracts and read both quotes. Nothing is dropped:
        a contract that does not resolve, a missing quote or a specification that differs from the
        approved entry is carried in the terms, and the gateway holds on it."""
        contract = reference = None
        problems: list[str] = []
        try:
            contract = self.broker.resolve(instrument)
        except BrokerError:
            pass
        ref_instrument = entry.reference_instrument()
        if entry.basis in ("BASIS_SHIFT", "REFERENCE_TRIGGER") and ref_instrument is not None:
            try:
                reference = self.broker.resolve(ref_instrument)
            except BrokerError:
                pass
        elif entry.basis == "DIRECT":
            reference = contract
        terms = entry.terms(instrument, contract, reference, problems, exits_only=exits_only)
        bid = ask = None
        quality, observed = QuoteQuality.UNAVAILABLE.value, now
        if contract is not None:
            try:
                q = self.broker.quote(contract)
                bid, ask, quality, observed = q.bid, q.ask, q.quality.value, q.received_at
            except BrokerError:
                pass
        if bid is None or ask is None:
            bid = ask = Decimal(0)
            quality = QuoteQuality.UNAVAILABLE.value
        leg = None
        if reference is not None and entry.basis != "DIRECT":
            rq = reference_quote(self.broker, reference, now)
            leg = LegQuote(contract_key=reference.contract_key, bid=rq.bid, ask=rq.ask,
                           last=rq.last, observed_at=rq.received_at, quality=rq.quality.value,
                           # Freshness only from the series' own update time (None: HOLD).
                           updated_at=rq.updated_at)
        return MappedQuote(symbol=instrument.symbol, bid=bid, ask=ask, observed_at=observed,
                           quality=quality, terms=terms, reference=leg)

    def _check_roll_windows(self, now: datetime) -> None:
        """A follower position in a contract inside its roll window is flagged (alert and
        notification) for a human to flatten or roll by a new instruction. The service never
        sends an order the gateway did not approve from a publisher action."""
        keep: set[str] = set()
        for p in self.coordinator.ledger():
            if p.quantity <= 0 or not p.contract:
                continue
            contract = ContractRef.model_validate_json(p.contract)
            entry = self.mapping.entry_for_contract(contract)
            if entry is None and p.mapping_terms:  # a family that lost its entry: recorded terms
                try:
                    entry = FamilyMapping.model_validate_json(p.mapping_terms)
                except ValueError:
                    entry = None
            if entry is None or entry.roll is None or not contract.expiry:
                continue
            month = contract.expiry[:6]
            roll = next((c.roll_date for c in entry.contracts if c.month == month), None)
            if roll is None:
                continue
            terms = entry.terms(
                Instrument(asset_class=contract.asset_class, symbol=contract.symbol,
                           futures_contract=month), contract, None)
            if not in_roll_window(terms, now):
                continue
            key = f"ROLL:{p.idea_id}"
            keep.add(key)
            detail = (
                f"{p.idea_id} holds {p.quantity} {contract.contract_key}; roll date {roll}, "
                f"{entry.roll.business_days_before} business days before it the position must be "
                "flattened (never held into a delivery window)"
            )
            if self.alerter.raise_(key, ROLL_DUE, "Contract inside its roll window", detail, now):
                self.store.event(now, ROLL_DUE, idea_id=p.idea_id, contract=contract.contract_key)
        self.alerter.sweep("ROLL:", keep, now)

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
            self.alerter.raise_(
                key,
                kind,
                d.kind.value.replace("_", " ").capitalize(),
                f"{d.symbol or '-'} idea {d.idea_id or '-'} ref {d.order_ref or '-'}: {d.detail}",
                now,
            )
        if not unreadable:
            self.alerter.sweep("RECON:", keep_recon, now)
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

    # ================================================================== alerts

    def _raise_alerts(self, now: datetime, report: CycleReport) -> None:
        """Every state that needs a human, derived from the stores at the end of every cycle (also
        when the broker is down), so an alert goes out in the cycle that caused it. See
        runtime/alerts.py for the de-duplication and re-alert rule."""
        ex = self.coordinator.store
        open_exc = ex.exceptions(open_only=True)
        # 1. Entries and adds of unknown outcome: at once, not after the settle or stuck period.
        keep: set[str] = set()
        unknown_refs = set()
        for i in ex.intents(UNRESOLVED_STATES):
            if i.action not in (Action.OPEN, Action.ADD):
                continue
            unknown_refs.add(i.order_ref)
            key = f"UNKNOWN_ENTRY:{i.order_ref}"
            keep.add(key)
            self.alerter.raise_(
                key, N.ENTRY_UNKNOWN_OUTCOME, "Entry of unknown outcome",
                f"{i.action.value} ref {i.order_ref} idea {i.idea_id} {i.contract_key or i.symbol}"
                f": {i.state.value}, {i.attempts} submission(s); never resent, new exposure in "
                "this contract blocked; check the broker and settle it",
                now,
            )
        self.alerter.sweep("UNKNOWN_ENTRY:", keep, now)
        # 2. Unprotected or unverified positions.
        keep = set()
        for e in open_exc:
            if e.kind not in (EXEC_UNPROTECTED, PROTECTION_UNVERIFIED):
                continue
            key = f"UNPROTECTED:{e.idea_id}"
            keep.add(key)
            self.alerter.raise_(key, N.UNPROTECTED_POSITION, "Position without confirmed stop",
                                f"{e.idea_id}: {e.kind}: {e.detail}", now)
        self.alerter.sweep("UNPROTECTED:", keep, now)
        # 3. Every other open execution exception (reversal, refused exits, settlements, ...).
        keep = set()
        for e in open_exc:
            if e.kind in SEPARATELY_ALERTED:
                continue
            kind = EXCEPTION_ALERT_KIND.get(e.kind, N.EXECUTION_EXCEPTION)
            if kind == N.ENTRY_UNKNOWN_OUTCOME and e.order_ref in unknown_refs:
                continue  # already alerted under UNKNOWN_ENTRY for the same intent
            key = f"EXC:{e.id}"
            keep.add(key)
            self.alerter.raise_(key, kind, e.kind.replace("_", " ").capitalize(),
                                f"exception {e.id} {e.kind} idea {e.idea_id or '-'} ref "
                                f"{e.order_ref or '-'}: {e.detail[:300]}", now)
        self.alerter.sweep("EXC:", keep, now)
        # 4. Broker state unknown (a read failed inside the coordinator) or a session to resync.
        #    While the probe itself fails, FAILURE:broker already says so (one alert, not three).
        keep = set()
        state = [e for e in open_exc if e.kind in (BROKER_STATE_UNKNOWN, BROKER_RECONNECT_PENDING)]
        if state and self.health["broker"].consecutive_failures == 0:
            keep.add("BROKER:STATE")
            self.alerter.raise_(
                "BROKER:STATE", N.BROKER_FAILURE, "Broker state not verified",
                "; ".join(f"{e.kind}: {e.detail[:200]}" for e in state), now,
            )
        session = session_state(self.store)
        if session["state"] == LOCKED_OUT:
            keep.add("BROKER:IDENTITY")
            self.alerter.raise_(
                "BROKER:IDENTITY", N.BROKER_IDENTITY_MISMATCH, "Broker identity lockout",
                f"{session.get('reason')}: the service no longer touches the broker and the halt "
                "is engaged; clear the lockout once the expected account is connected", now,
            )
        self.alerter.sweep("BROKER:", keep, now)
        # 5. Exits and amendments the gateway or the coordinator refused (halted ones are
        #    notified at once as HALTED_ACTION_REFUSED).
        keep = set()
        for e in self.store.exceptions(open_only=True):
            action = (e["detail"] or "").split(" ", 1)[0]
            if e["kind"] in REFUSED_SIGNAL_KINDS and action in ("REDUCE", "CLOSE", "MODIFY"):
                key = f"EXIT_REFUSED:{e['id']}"
                keep.add(key)
                self.alerter.raise_(
                    key, N.EXIT_REFUSED, "Exit or amendment not executed",
                    f"held signal {e['id']} {e['kind']} {action} source {e['source_id']} "
                    f"r{e['revision']} {e['action_id']} ref {e['order_ref'] or '-'}: "
                    f"{', '.join(e['reasons'])}", now,
                )
        self.alerter.sweep("EXIT_REFUSED:", keep, now)
        # 6. Open positions whose family lost its approval (exits and protection only).
        keep = set()
        for p in self.coordinator.ledger():
            if p.quantity <= 0 or not p.mapping_terms:
                continue
            try:
                family = FamilyMapping.model_validate_json(p.mapping_terms).family
            except ValueError:
                family = None
            entry = self.mapping.entries.get(family or "")
            if family is not None and (entry is None or not entry.executable):
                key = f"REVOKED:{p.idea_id}"
                keep.add(key)
                self.alerter.raise_(
                    key, N.MAPPING_REVOKED_OPEN_POSITION, "Open position under a withdrawn mapping",
                    f"{p.idea_id} holds {p.quantity} {p.contract_key}; family {family} is no "
                    "longer approved: it can be reduced, closed and protected, never added to",
                    now,
                )
        self.alerter.sweep("REVOKED:", keep, now)
        # 7. The source cursor not advancing while the publisher is expected to post.
        keep = set()
        stall = self.config.service.source_stall_after_seconds
        changed = self.store.get("cursor_changed_at")
        if stall and changed and in_activity_window(self.config.service.activity_window_utc, now):
            quiet = (now - datetime.fromisoformat(changed)).total_seconds()
            if quiet >= stall:
                keep.add("SOURCE:STALLED")
                self.alerter.raise_(
                    "SOURCE:STALLED", N.SOURCE_STALLED, "Source cursor not advancing",
                    f"{self.source.name} cursor {self.source.cursor()} unchanged for {quiet:.0f} s "
                    "inside the expected activity window", now,
                )
        self.alerter.sweep("SOURCE:", keep, now)

    def _heartbeat(self, report: CycleReport) -> None:
        now = self.clock()
        rec = self.last_reconciliation
        session = session_state(self.store)
        payload = {
            "at": now.isoformat(),
            "mode": self.config.mode,
            "pid": os.getpid(),
            "cycle": self.cycles,
            "halted": report.halted,
            "broker_session": {"state": session["state"], "since": session.get("since"),
                               "reason": session.get("reason")},
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
        # The exponent is capped (review F6): 2 ** n overflowed a float after ~1,024 failures.
        exponent = min(h.consecutive_failures - 1, MAX_BACKOFF_EXPONENT)
        backoff = min(cfg.cadence_seconds * 2**exponent, cfg.max_backoff_seconds)
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
        # An unreadable broker needs a human at once (exposure is blocked meanwhile); the source
        # and the pipeline alert once they fail repeatedly.
        threshold = 1 if component == "broker" else cfg.failure_alert_after
        if h.consecutive_failures >= threshold:
            self.alerter.raise_(
                f"FAILURE:{component}",
                FAILURE_KIND[component],
                "Broker unreadable" if component == "broker" else f"Repeated {component} failure",
                f"{h.consecutive_failures} consecutive failures; last: {h.last_error}",
                now,
            )

    def _success(self, component: str, now: datetime) -> None:
        h = self.health[component]
        if h.consecutive_failures:
            self.store.event(now, f"{component.upper()}_RECOVERED", after=h.consecutive_failures)
        h.consecutive_failures = 0
        h.next_attempt_at = None
        h.last_success_at = now.isoformat()
        self.alerter.sweep(f"FAILURE:{component}", set(), now)


def _json(value) -> str:
    return json.dumps(value, default=str)
