"""ExecutionCoordinator: turns a gateway-approved action into broker orders and the follower ledger.

It never authorizes. ``execute`` accepts only an action whose ``GateDecision`` from
``validator.gate_action`` is APPROVE, and sizes it with ``validator.fill_quantity``. What it adds
is what only execution can know: a deterministic idempotency key, a write-ahead intent, broker-side
quote sanity and exposure, the halt switch, protective-order confirmation and recovery after a lost
acknowledgement or a restart. See docs/EXECUTION_CORE.md for the intent state machine.

The never-resend rule: an intent whose submission outcome is unknown (RECORDED after a restart, or
UNRESOLVED after a lost acknowledgement) is looked up at the broker by ``order_ref`` in
``open_orders``, ``executions`` and ``completed_orders``. Whatever is found is adopted, including a
terminal REJECTED or CANCELLED state. An entry or add that was submitted is never resent: it stays
UNRESOLVED, is looked up on every cycle and, after the settle period, opens
UNKNOWN_OUTCOME_ENTRY_NOT_RESENT for a human while new exposure in its instrument is blocked (review
F5). An exit is resent only after the settle period and ``exit_no_trace_lookups`` consecutive
no-trace lookups spread over at least that period. An intent recorded but never submitted (the
process died before the request) is sent once its entry checks pass again. The reasoning is
journaled.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import os
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, InvalidOperation
from pathlib import Path

from pydantic import Field

from tradingbot.event_models import ReplaySource, TradeAction
from tradingbot.execution.broker import (
    WORKING_STATUSES,
    AckLost,
    Broker,
    BrokerError,
    BrokerOrder,
    BrokerQuote,
    ConditionDirection,
    ContractRef,
    OrderIntent,
    OrderNotFound,
    OrderSide,
    OrderStatus,
    OrderType,
    PriceCondition,
    QuoteQuality,
    TimeInForce,
    supports_price_conditions,
)
from tradingbot.execution.store import (
    LEDGER_REVERSAL,
    UNRESOLVED_STATES,
    ExecutionStore,
    Intent,
    IntentState,
    LedgerPosition,
    OrderRow,
    Role,
)
from tradingbot.models import (
    Action,
    ExecutionPlan,
    GateDecision,
    Instrument,
    MockPolicy,
    Position,
    PositionSide,
    StrictModel,
)
from tradingbot.validator import fill_quantity, source_is_fresh

UNPROTECTED_POSITION = "UNPROTECTED_POSITION"
# The broker could not be read after a fill: protection is unknown, never assumed (review M1).
PROTECTION_UNVERIFIED = "PROTECTION_UNVERIFIED"
MODIFY_NOT_CONFIRMED = "MODIFY_NOT_CONFIRMED"
# An entry or add whose first submission outcome was unknown, that the broker shows no trace of,
# and whose source post is no longer fresh enough to act on: it is never resent.
STALE_AFTER_UNKNOWN_OUTCOME = "STALE_AFTER_UNKNOWN_OUTCOME"
# How far before an intent's creation the completed-orders lookup reaches (clock skew allowance).
COMPLETED_LOOKBACK = timedelta(minutes=5)
# A submit that failed with a definite error (not a lost acknowledgement): never resent.
SUBMIT_ERROR = "SUBMIT_ERROR"
# An unknown outcome older than the broker's lookup horizon: only a human can settle it.
UNKNOWN_OUTCOME_BEYOND_LOOKUP_HORIZON = "UNKNOWN_OUTCOME_BEYOND_LOOKUP_HORIZON"
# An entry or add whose submission outcome is unknown and that the broker still shows no trace of
# after the settle period: it is never resent automatically; a human must settle it (review F5).
UNKNOWN_OUTCOME_ENTRY_NOT_RESENT = "UNKNOWN_OUTCOME_ENTRY_NOT_RESENT"
UNRESOLVED_ENTRY_IN_INSTRUMENT = "UNRESOLVED_ENTRY_IN_INSTRUMENT"
# An exit was not sent because the working entries, earlier exits or protective children could not
# be cancelled or resized first, with the result confirmed at the broker (review R1 / R6).
EXIT_NOT_NETTED = "EXIT_NOT_NETTED"
# An exit was not sent because the broker quote was not usable to price its limit (review S2); the
# broker-side protective stop remains the protection.
EXIT_WITHOUT_QUOTE = "EXIT_WITHOUT_QUOTE"
# The entry's own evidence (its row in open or completed orders, its executions) is missing or
# incomplete, so whether and how much it filled is unknown. Never inferred from a child order: at
# IBKR a bracket child is PreSubmitted before the parent fills and may stay so after it. The
# intent stays UNRESOLVED and new exposure in the instrument is blocked until it is settled.
ENTRY_STATE_UNKNOWN = "ENTRY_STATE_UNKNOWN"
# A broker read failed (open orders, executions, completed orders or positions): the ledger may be
# stale, every position's protection is UNKNOWN and nothing that creates or increases exposure is
# sent until a full sync succeeds (review F1).
BROKER_STATE_UNKNOWN = "BROKER_STATE_UNKNOWN"
BROKER_UNREADABLE = "BROKER_UNREADABLE"
# An idea was stopped out (or closed) while part of its entry or add was still working, and the
# cancel of that remainder could not be confirmed at the broker (review F2).
ENTRY_REMAINDER_NOT_CANCELLED = "ENTRY_REMAINDER_NOT_CANCELLED"
# The broker nets one position per contract: an idea on the opposite side of an open idea in the
# same contract is never opened beside it. A publisher reversal is a close followed by an open.
OPPOSITE_IDEA_IN_SAME_CONTRACT = "OPPOSITE_IDEA_IN_SAME_CONTRACT"
# Multi-asset mapping: a REFERENCE_TRIGGER family needs broker-side price conditions; a broker that
# does not report supporting them never receives a conditional order.
CONDITIONS_UNSUPPORTED = "CONDITIONAL_ORDERS_UNSUPPORTED_BY_BROKER"
# The contract the broker resolves now differs from the one the gateway decided on.
CONTRACT_CHANGED = "CONTRACT_CHANGED_SINCE_DECISION"


class BrokerUnreadable(BrokerError):
    """A full broker sync failed: the coordinator cannot tell what the account holds."""

# An open exception of one of these kinds on any idea blocks new exposure in that instrument.
INSTRUMENT_BLOCKING_KINDS = (
    LEDGER_REVERSAL,
    ENTRY_REMAINDER_NOT_CANCELLED,
    UNPROTECTED_POSITION,
    PROTECTION_UNVERIFIED,
    ENTRY_STATE_UNKNOWN,
)


def default_halt_file() -> Path:
    override = os.environ.get("TRADINGBOT_HALT_FILE")
    return Path(override) if override else Path.home() / ".local" / "state" / "tradingbot" / "HALT"


class ExecutionConfig(StrictModel):
    # Emergency halt: while this file exists nothing that could add exposure is submitted or
    # amended; working entries are cancelled and protective orders are still sized (review R7).
    halt_file: Path = Field(default_factory=default_halt_file)
    accepted_quote_qualities: frozenset[QuoteQuality] = frozenset({QuoteQuality.LIVE})
    # When set, the coordinator refuses to trade against any other broker account.
    expected_account: str | None = None
    # A lookup that finds no trace leads to at most this many submissions in total for one intent.
    max_submit_attempts: int = Field(default=2, ge=1, le=5)
    # Reconciliation reports an unresolved intent older than this as stuck.
    stuck_after_seconds: int = Field(default=60, gt=0)
    # After an unknown submit outcome, "no trace at the broker" may be concluded only once this long
    # has passed since the request (open orders can lag a fresh submission at the broker).
    settle_seconds: float = Field(default=10.0, ge=0)
    # An exit of unknown outcome is resent only after at least this many consecutive lookups that
    # all show no trace, spread over at least ``settle_seconds`` (review F5).
    exit_no_trace_lookups: int = Field(default=3, ge=3, le=20)
    # An entry or add older than this (since its intent was recorded) is never resent.
    max_intent_age_seconds: int = Field(default=120, gt=0)
    # An exit older than this is re-priced from a fresh quote before it is sent; with no usable
    # quote it ends REFUSED with an EXIT_WITHOUT_QUOTE exception. Never sent at a stale price.
    max_exit_intent_age_seconds: int = Field(default=120, gt=0)
    # How far back the broker's executions / completed-orders queries reach (IBKR: the current
    # day). An unknown outcome older than this can never be proven absent: it is never resent.
    lookup_horizon_seconds: int = Field(default=6 * 3600, gt=0)
    # Cancel / resize confirmation before an exit: this many broker reads, this far apart.
    confirm_attempts: int = Field(default=3, ge=1, le=20)
    confirm_wait_seconds: float = Field(default=0.0, ge=0, le=5)
    # Time in force of protective stop and target children. GTC by default; DAY for an account or
    # experiment that allows DAY orders only (a DAY stop expires at the session end and the
    # position is then reported without protection until a human acts).
    protective_tif: TimeInForce = TimeInForce.GTC


class ExecutionOutcome(StrictModel):
    order_ref: str
    source_id: str
    action_id: str
    action: Action
    idea_id: str
    state: IntentState
    filled_quantity: int = 0
    avg_fill_price: str | None = None
    protection: str = "NOT_REQUIRED"  # CONFIRMED | UNPROTECTED | UNKNOWN | PENDING | NOT_REQUIRED
    duplicate: bool = False
    gate_reasons: list[str] = Field(
        default_factory=list
    )  # the gateway's APPROVE reasons, as recorded
    reasons: list[str] = Field(default_factory=list)  # execution reasons (refusal, reject, halt)

    @property
    def replay_status(self) -> str:
        """The outcome in the replay journal's vocabulary (event_models.ActionOutcome.status)."""
        if self.protection in ("UNPROTECTED", "UNKNOWN"):
            return "EXCEPTION"
        if self.state in (IntentState.FILLED, IntentState.APPLIED):
            return "EXECUTED"
        if self.state == IntentState.CANCELLED:
            return "EXECUTED" if self.filled_quantity else "CANCELLED"
        if self.state in UNRESOLVED_STATES:
            return "UNRESOLVED"
        return self.state.value  # WORKING, REJECTED, HALTED, REFUSED


def order_ref_for(source_id: str, revision: int, action_id: str) -> str:
    """Deterministic idempotency key: one post revision and action always map to one ref."""
    digest = hashlib.sha256(f"{source_id}\x1f{revision}\x1f{action_id}".encode()).hexdigest()
    return f"TB-{digest[:20]}"


def stop_ref(entry_ref: str) -> str:
    return f"{entry_ref}.STP"


def target_ref(entry_ref: str) -> str:
    return f"{entry_ref}.TGT"


def backstop_ref(entry_ref: str) -> str:
    return f"{entry_ref}.BKS"


def _plan_of(raw: str | None) -> ExecutionPlan | None:
    return ExecutionPlan.model_validate_json(raw) if raw else None


def _exit_condition(plan: ExecutionPlan, long: bool, level, kind: str) -> PriceCondition:
    """The broker-side condition on the reference series that releases an exit: a long's stop
    below / target above its level, a short's the other way round."""
    below = (kind == "stop") == long
    return PriceCondition(
        contract=ContractRef.model_validate_json(plan.reference_contract),
        direction=ConditionDirection.BELOW if below else ConditionDirection.ABOVE,
        trigger_price=level,
        released_order_type=OrderType.MKT,
    )


def _dec(value: str | None) -> Decimal | None:
    if value is None:
        return None
    try:
        d = Decimal(value)
    except (InvalidOperation, ValueError, TypeError):
        return None
    return d if d.is_finite() and d > 0 else None


def snap_to_tick(price: Decimal, tick: Decimal, rounding: str) -> Decimal:
    """``price`` moved to a multiple of ``tick``: ROUND_FLOOR (down) or ROUND_CEILING (up). Used
    only for collar-bounded limits, always in the direction that keeps the limit inside the
    collar (a buy limit down, a sell limit up). Stop and target prices are never snapped: an
    off-tick one is refused, so a protective stop can never be loosened by rounding."""
    return (price / tick).to_integral_value(rounding) * tick


def _contract_from(raw: str | None) -> ContractRef | None:
    return ContractRef.model_validate_json(raw) if raw else None


def _rule_problems(contract: ContractRef, quantity: int | None, **prices) -> list[str]:
    """The contract's tick and size rules, as typed refusal reasons (never silently rounded)."""
    out = []
    if quantity is not None and not contract.on_increment(quantity):
        out.append("QUANTITY_OFF_INCREMENT")
    for name, price in prices.items():
        if price is not None and not contract.on_tick(price):
            out.append(f"{name.upper()}_PRICE_OFF_TICK")
    return out


def _field(order: BrokerOrder, name: str):
    """A working order's amendable level: a price field, or its condition's trigger level."""
    if name == "trigger_price":
        return order.condition.trigger_price if order.condition else None
    return getattr(order, name)


def _intent_state(status: OrderStatus, filled: int, quantity: int) -> IntentState:
    if status == OrderStatus.FILLED or (filled >= quantity > 0):
        return IntentState.FILLED
    if status in (OrderStatus.REJECTED, OrderStatus.INACTIVE):
        return IntentState.CANCELLED if filled else IntentState.REJECTED
    if status in (OrderStatus.CANCELLED, OrderStatus.API_CANCELLED):
        return IntentState.CANCELLED
    if status == OrderStatus.UNKNOWN:
        return IntentState.UNRESOLVED
    return IntentState.WORKING


class ExecutionStoreLocked(RuntimeError):
    """Another live coordinator holds this execution store (safety review R5)."""


def _lock_store(store_path: Path) -> int:
    """Take an exclusive, non-blocking lock beside the store for the life of the coordinator.
    The OS releases it when the process dies, so a crash never leaves a stale lock."""
    lock_path = store_path.with_name(store_path.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        holder = os.pread(fd, 64, 0).decode(errors="replace").strip() or "unknown"
        os.close(fd)
        raise ExecutionStoreLocked(
            f"execution store {store_path} is in use by another coordinator (pid {holder}); "
            "refusing to start a second one on it"
        ) from None
    os.ftruncate(fd, 0)
    os.pwrite(fd, str(os.getpid()).encode(), 0)
    return fd


class ExecutionRefused(ValueError):
    """Raised when a caller tries to execute something the gateway did not approve."""


class ExecutionCoordinator:
    def __init__(
        self,
        broker: Broker,
        store_path: str | Path,
        policy: MockPolicy | None = None,
        config: ExecutionConfig | None = None,
        clock: Callable[[], datetime] | None = None,
    ):
        self.broker = broker
        self._lock_fd: int | None = _lock_store(Path(store_path))
        self.store = ExecutionStore(store_path)
        self.policy = policy or MockPolicy()
        self.config = config or ExecutionConfig()
        self.clock = clock or (lambda: datetime.now(UTC))

    # ================================================================== public API

    def close(self) -> None:
        """Release the store lock (process exit releases it too)."""
        if self._lock_fd is not None:
            fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            os.close(self._lock_fd)
            self._lock_fd = None

    def __del__(self):
        with contextlib.suppress(Exception):  # interpreter shutdown
            self.close()

    def halted(self) -> bool:
        return self.config.halt_file.exists()

    def ledger(self) -> list[LedgerPosition]:
        return self.store.positions()

    def intent(self, order_ref: str) -> Intent | None:
        return self.store.intent(order_ref)

    def opposite_idea(
        self, instrument: Instrument, side: PositionSide, staged=()
    ) -> str | None:
        """The id of an open (or pending) idea on the other side of ``side`` in the same contract,
        from the follower ledger (by contract key) and from ``staged`` replay ideas that are not
        yet in the ledger (by symbol and asset class: conservative); None when there is none."""
        contract, _ = self._resolve_contract(instrument)
        key = contract.contract_key if contract is not None else None
        ledger = self.store.positions()
        for p in ledger:
            if p.position_side == side or not (p.quantity > 0 or p.status == "PENDING"):
                continue
            if key is not None and p.contract_key is not None:
                if p.contract_key == key:
                    return p.idea_id
            elif p.symbol == instrument.symbol and p.asset_class == instrument.asset_class:
                return p.idea_id
        known = {p.idea_id for p in ledger}
        for i in staged:
            if (
                i.idea_id not in known
                and i.position_side != side
                and i.quantity > 0
                and i.status not in ("CLOSED", "REVERSED", "NOT_FILLED")
                and i.symbol == instrument.symbol
                and i.asset_class == instrument.asset_class
            ):
                return i.idea_id
        return None

    def _contract_for(self, intent: Intent) -> tuple[ContractRef | None, str | None]:
        """The intent's recorded contract; an intent recorded before round 2 is resolved again."""
        contract = _contract_from(intent.contract)
        if contract is not None:
            return contract, None
        pos = self.store.position(intent.idea_id)
        if pos is None:
            return None, "IDEA_UNKNOWN"
        return self._resolve_contract(Instrument(asset_class=pos.asset_class, symbol=intent.symbol))

    def _key_of(self, symbol: str, asset_class, contract_key: str | None) -> str:
        """A row's contract key; rows written before round 2 are resolved by symbol once."""
        if contract_key:
            return contract_key
        contract, _ = self._resolve_contract(Instrument(asset_class=asset_class, symbol=symbol))
        return contract.contract_key if contract is not None else f"SYMBOL:{symbol}"

    def intent_for(self, source: ReplaySource, action: TradeAction) -> Intent | None:
        return self.store.intent(order_ref_for(source.source_id, source.revision, action.action_id))

    def execute(
        self,
        source: ReplaySource,
        action: TradeAction,
        gate: GateDecision,
        *,
        now: datetime | None = None,
    ) -> ExecutionOutcome:
        """Execute one gateway-approved action. Idempotent per (source_id, revision, action_id)."""
        if gate.status != "APPROVE":
            raise ExecutionRefused(f"gate decision {gate.status} cannot reach the broker")
        now = now or self.clock()
        ref = order_ref_for(source.source_id, source.revision, action.action_id)
        existing = self.store.intent(ref)
        if existing is not None:
            if existing.state in UNRESOLVED_STATES:
                self._resolve(existing)
            else:
                self.sync()
            self.store.log(
                self.clock(),
                "DUPLICATE_DELIVERY",
                order_ref=ref,
                idea_id=existing.idea_id,
                detail="intent already on record; no new order",
                state=existing.state.value,
            )
            return self._outcome(ref, duplicate=True)
        synced = self.sync()
        account = self.broker.account_id()
        if self.config.expected_account and account != self.config.expected_account:
            return self._refuse(
                source, action, gate, ref, account, ["BROKER_ACCOUNT_MISMATCH"], now
            )
        if action.action in (Action.OPEN, Action.ADD) and (
            not synced or self._broker_state_unknown()
        ):
            # Review F1: a stale ledger never authorizes new exposure.
            return self._refuse(source, action, gate, ref, account, [BROKER_UNREADABLE], now)
        if action.action == Action.OPEN:
            return self._open(source, action, gate, ref, account, now)
        if action.action == Action.ADD:
            return self._add(source, action, gate, ref, account, now)
        if action.action in (Action.REDUCE, Action.CLOSE):
            return self._exit(source, action, gate, ref, account, now)
        if action.action == Action.MODIFY:
            return self._modify(source, action, gate, ref, account, now)
        return self._refuse(source, action, gate, ref, account, ["ACTION_NOT_EXECUTABLE"], now)

    def reconcile(self, now: datetime | None = None):
        """Read-only comparison of the ledger with the broker (see reconcile.py)."""
        from tradingbot.execution.reconcile import reconcile

        return reconcile(
            self.store,
            self.broker,
            now=now or self.clock(),
            stuck_after_seconds=self.config.stuck_after_seconds,
        )

    def recover(self) -> list[ExecutionOutcome]:
        """After a restart (or any time): resolve every intent of unknown outcome, then sync."""
        out = []
        for intent in self.store.intents(UNRESOLVED_STATES):
            # Journaled when the intent's state changes, not on every cycle while it is stuck.
            last = next(
                (
                    e
                    for e in reversed(self.store.journal(intent.order_ref))
                    if e.event == "RECOVERY_LOOKUP"
                ),
                None,
            )
            seen = {"state": intent.state.value, "attempts": intent.attempts}
            if last is None or {k: last.detail.get(k) for k in seen} != seen:
                self.store.log(
                    self.clock(),
                    "RECOVERY_LOOKUP",
                    order_ref=intent.order_ref,
                    idea_id=intent.idea_id,
                    **seen,
                )
            self._resolve(intent)
            out.append(self._outcome(intent.order_ref))
        if not self.sync():
            # Never swallowed (review F1): the caller must not act on a ledger it cannot verify.
            raise BrokerUnreadable("broker state could not be read; recovery incomplete")
        return out

    def sync(self, *, _rerun: bool = True) -> bool:
        """Apply new broker fills to the ledger, refresh working orders, keep protection sized.
        Returns False when the broker could not be read (nothing is changed then)."""
        now = self.clock()
        try:
            fills = self.broker.executions()
            open_orders = self.broker.open_orders()
            # Every read the coordinator depends on must succeed (F1). Completed orders are the
            # only evidence of an order that has left the working set (it is never inferred).
            completed = self.broker.completed_orders(
                since=now - timedelta(seconds=self.config.lookup_horizon_seconds)
            )
            self.broker.positions()
        except BrokerError as exc:
            self._broker_unreadable("sync", exc)
            return False
        if self.store.resolve_exceptions(now, BROKER_STATE_UNKNOWN, idea_id=None):
            self.store.log(now, "BROKER_STATE_RESTORED", rule="full broker sync succeeded")
        ours = {o.order_ref: o for o in self.store.orders()}
        touched: set[str] = set()
        for f in fills:
            row = ours.get(f.order_ref)
            if row is None or self.store.has_fill(f.exec_id):
                continue
            pos = self.store.apply_fill(
                exec_id=f.exec_id,
                order=row,
                quantity=f.quantity,
                price=f.price,
                executed_at=f.executed_at,
                at=now,
            )
            if pos is not None:
                touched.add(row.idea_id)
                self.store.log(
                    now,
                    "FILL",
                    order_ref=row.order_ref,
                    idea_id=row.idea_id,
                    role=row.role.value,
                    side=f.side.value,
                    quantity=f.quantity,
                    price=str(f.price),
                    exec_id=f.exec_id,
                    ledger_quantity=pos.quantity,
                )
        self._refresh_orders(open_orders, now, completed)
        asked = self._cancel_stopped_out_entries(open_orders)
        if _rerun and self.halted() and self._cancel_entries_for_halt(open_orders):
            asked = True
        if asked and _rerun:
            # Risk-reducing: re-read once so a fill that beat the cancel is applied and covered.
            return self.sync(_rerun=False)
        positions = {p.idea_id: p for p in self.store.positions()}
        for idea_id in sorted(
            touched | {i for i, p in positions.items() if p.status in ("OPEN", "PENDING")}
        ):
            self._maintain_protection(idea_id)
        return True

    def _cancel_stopped_out_entries(self, open_orders: list[BrokerOrder]) -> bool:
        """Review F2: once a protective stop has filled for an idea, or the idea is closed, no
        remainder of its entry or add orders may keep working (it would later fill with no stop).
        Each is cancelled and the cancel confirmed at the broker; an unconfirmed cancel opens
        ENTRY_REMAINDER_NOT_CANCELLED, which blocks new exposure in the instrument. Returns True
        when a cancel was requested."""
        stopped = {
            f["idea_id"]
            for f in self.store.fills()
            if f["role"] in (Role.STOP.value, Role.BACKSTOP.value)
        }
        stopped |= {
            p.idea_id for p in self.store.positions() if p.status in ("CLOSED", "REVERSED")
        }
        if not stopped:
            return False
        working = {o.order_ref for o in open_orders}
        remainder: dict[str, list[str]] = {}
        for r in self.store.orders():
            if r.role == Role.ENTRY and r.idea_id in stopped and r.order_ref in working:
                remainder.setdefault(r.idea_id, []).append(r.order_ref)
        now = self.clock()
        for idea in sorted(stopped - set(remainder)):
            self.store.resolve_exceptions(now, ENTRY_REMAINDER_NOT_CANCELLED, idea_id=idea)
        for idea, refs in sorted(remainder.items()):
            for ref in refs:
                if self._cancel_confirmed(ref, idea, "idea stopped out: entry remainder"):
                    self.store.log(
                        self.clock(), "ENTRY_REMAINDER_CANCELLED", order_ref=ref, idea_id=idea,
                        rule="a stopped-out or closed idea keeps no working entry or add",
                    )
                    continue
                detail = (
                    f"{ref} is still working after {idea} was stopped out or closed and its "
                    "cancel was not confirmed; new exposure in this instrument is blocked"
                )
                if self.store.raise_exception(
                    self.clock(), ENTRY_REMAINDER_NOT_CANCELLED, detail, idea_id=idea,
                    order_ref=ref,
                ):
                    self.store.log(
                        self.clock(), ENTRY_REMAINDER_NOT_CANCELLED, order_ref=ref, idea_id=idea
                    )
        return bool(remainder)

    def _cancel_entries_for_halt(self, open_orders: list[BrokerOrder]) -> bool:
        """Under the halt no entry or add may keep working (it could fill while nothing else may
        be sent): every working one is cancelled. Returns True when a cancel was requested."""
        entries = {r.order_ref: r for r in self.store.orders() if r.role == Role.ENTRY}
        asked = False
        for o in open_orders:
            row = entries.get(o.order_ref)
            if row is None or o.status == OrderStatus.PENDING_CANCEL:
                continue
            asked |= self._try(
                lambda r=o.order_ref: self.broker.cancel(r),
                "HALT_ENTRY_CANCELLED",
                row.idea_id,
                o.order_ref,
                reason="halt engaged: working entries and adds are cancelled",
                halt_file=str(self.config.halt_file),
            )
        return asked

    def _broker_state_unknown(self) -> bool:
        return any(
            e.kind == BROKER_STATE_UNKNOWN for e in self.store.exceptions(open_only=True)
        )

    def _broker_unreadable(self, read: str, exc: BaseException) -> None:
        """A broker read failed: every open position's protection becomes UNKNOWN and an open
        BROKER_STATE_UNKNOWN exception blocks new exposure until a full sync succeeds (F1)."""
        now = self.clock()
        self.store.log(now, "SYNC_FAILED", read=read, error=str(exc))
        for p in self.store.positions():
            if p.quantity > 0 and p.stop_price is not None and p.protection != "UNKNOWN":
                self.store.update_position(p.idea_id, now, protection="UNKNOWN")
        detail = (
            f"broker {read} read failed: {exc}; protection unknown and new exposure blocked "
            "until a full broker sync succeeds"
        )
        if self.store.raise_exception(now, BROKER_STATE_UNKNOWN, detail):
            self.store.log(now, BROKER_STATE_UNKNOWN, read=read, error=str(exc))

    # ================================================================== actions

    def _open(self, source, action, gate, ref, account, now) -> ExecutionOutcome:
        assert action.instrument is not None and action.position_side is not None
        idea_id = f"{source.source_id}:{action.action_id}"
        if self.store.position(idea_id) is not None:
            return self._refuse(source, action, gate, ref, account, ["IDEA_ALREADY_EXISTS"], now)
        if self.opposite_idea(action.instrument, action.position_side):
            return self._refuse(
                source, action, gate, ref, account, [OPPOSITE_IDEA_IN_SAME_CONTRACT], now
            )
        contract, problem = self._resolve_contract(action.instrument)
        if problem:
            return self._refuse(source, action, gate, ref, account, [problem], now)
        plan = gate.plan
        problem = self._plan_problem(plan, contract)
        if problem:
            return self._refuse(source, action, gate, ref, account, [problem], now)
        long = action.position_side == PositionSide.LONG
        side = OrderSide.BUY if long else OrderSide.SELL
        qty = fill_quantity(
            Action.OPEN, self.policy, None, per_signal=plan.quantity if plan else None
        )
        if plan is None:
            stop, target = _dec(action.stop_price), _dec(action.target_price)
            entry_level = action.entry_reference
        else:  # the gateway's prices, each in its own series
            stop, target = plan.stop, plan.target
            entry_level = str(plan.execution_reference)
        reference_series = plan is not None and plan.protection_series == "REFERENCE"
        rules = _rule_problems(
            contract,
            qty,
            stop=None if reference_series else stop,
            target=None if reference_series else target,
            backstop=plan.backstop_stop if plan else None,
        )
        if rules:
            return self._refuse(source, action, gate, ref, account, rules, now)
        limit, reasons = self._entry_checks(contract, side, qty, entry_level, idea_id, now)
        if reasons:
            return self._refuse(source, action, gate, ref, account, reasons, now, idea_id=idea_id)
        exit_side = OrderSide.SELL if long else OrderSide.BUY
        entry = OrderIntent(
            order_ref=ref,
            contract=contract,
            side=side,
            quantity=qty,
            order_type=OrderType.LMT,
            limit_price=limit,
            tif=TimeInForce.DAY,
        )
        children = []
        if reference_series:
            children = self._reference_children(ref, contract, exit_side, qty, plan, long)
            if not supports_price_conditions(self.broker):
                return self._refuse(
                    source, action, gate, ref, account, [CONDITIONS_UNSUPPORTED], now,
                    idea_id=idea_id,
                )
        elif stop is not None:
            children.append(
                OrderIntent(
                    order_ref=stop_ref(ref),
                    contract=contract,
                    side=exit_side,
                    quantity=qty,
                    order_type=OrderType.STP,
                    stop_price=stop,
                    tif=self.config.protective_tif,
                    parent_ref=ref,
                    reduce_only=True,
                )
            )
        if target is not None and not reference_series:
            children.append(
                OrderIntent(
                    order_ref=target_ref(ref),
                    contract=contract,
                    side=exit_side,
                    quantity=qty,
                    order_type=OrderType.LMT,
                    limit_price=target,
                    tif=self.config.protective_tif,
                    parent_ref=ref,
                    reduce_only=True,
                )
            )
        intent = self._new_intent(
            source,
            action,
            gate,
            ref,
            account,
            idea_id,
            now,
            contract=contract,
            side=side.value,
            quantity=qty,
            order_type="LMT",
            limit_price=str(limit),
        )
        ledger = LedgerPosition(
            idea_id=idea_id,
            symbol=contract.symbol,
            asset_class=contract.asset_class,
            contract_key=contract.contract_key,
            contract=contract.model_dump_json(),
            position_side=action.position_side,
            quantity=0,
            stop_price=str(stop) if stop is not None else None,
            target_price=str(target) if target is not None else None,
            stop_ref=stop_ref(ref) if stop is not None else None,
            target_ref=target_ref(ref) if target is not None else None,
            status="PENDING",
            updated_at=now,
            protection_series=plan.protection_series if plan else None,
            reference_key=plan.reference_contract_key if reference_series else None,
            backstop_ref=backstop_ref(ref) if plan and plan.backstop_stop else None,
            backstop_price=str(plan.backstop_stop) if plan and plan.backstop_stop else None,
        )
        self._record(intent, [entry, *children], ledger)
        self._submit(intent, entry, children)
        return self._outcome(ref)

    @staticmethod
    def _idea_contract_problem(pos: LedgerPosition, contract: ContractRef) -> str | None:
        """A follow-up acts only on the idea's own contract (two months are never confused)."""
        if pos.contract_key and contract.contract_key != pos.contract_key:
            return "IDEA_CONTRACT_MISMATCH"
        return None

    def _plan_problem(self, plan: ExecutionPlan | None, contract: ContractRef) -> str | None:
        """A mapped action executes only in the contract the gateway decided on."""
        if plan is None:
            return None
        if self._key_for_plan(contract) != plan.execution_key:
            return CONTRACT_CHANGED
        return None

    @staticmethod
    def _key_for_plan(contract: ContractRef) -> str:
        """``Instrument.quote_key`` of a resolved contract (stocks: the symbol)."""
        if contract.sec_type == "STK":
            return contract.symbol
        month = contract.expiry or "-"
        if contract.asset_class.value == "FUTURE":
            month = month[:6]
        elif month != "-" and len(month) == 8:
            month = f"{month[:4]}-{month[4:6]}-{month[6:]}"
        strike = format(contract.strike.normalize(), "f") if contract.strike is not None else "-"
        right = {"C": "CALL", "P": "PUT"}.get(contract.right or "", "-")
        return f"{contract.asset_class.value}:{contract.symbol}:{month}:{strike}:{right}"

    def _reference_children(
        self, ref: str, contract: ContractRef, exit_side: OrderSide, qty: int,
        plan: ExecutionPlan, long: bool,
    ) -> list[OrderIntent]:
        """REFERENCE_TRIGGER: the stop and target are exits held at the broker as conditions on
        the reference series, released as market orders in the execution contract; a backstop
        stop in the execution contract (BASIS_SHIFT priced) may work beside them. All reduce-only
        children of one bracket (one-cancels-other)."""
        out = []
        for level, kind, child_ref in (
            (plan.stop, "stop", stop_ref(ref)),
            (plan.target, "target", target_ref(ref)),
        ):
            if level is None:
                continue
            out.append(
                OrderIntent(
                    order_ref=child_ref,
                    contract=contract,
                    side=exit_side,
                    quantity=qty,
                    order_type=OrderType.MKT,
                    tif=self.config.protective_tif,
                    parent_ref=ref,
                    reduce_only=True,
                    condition=_exit_condition(plan, long, level, kind),
                )
            )
        if plan.backstop_stop is not None:
            out.append(
                OrderIntent(
                    order_ref=backstop_ref(ref),
                    contract=contract,
                    side=exit_side,
                    quantity=qty,
                    order_type=OrderType.STP,
                    stop_price=plan.backstop_stop,
                    tif=self.config.protective_tif,
                    parent_ref=ref,
                    reduce_only=True,
                )
            )
        return out

    def _add(self, source, action, gate, ref, account, now) -> ExecutionOutcome:
        pos = self._open_position(action.idea_reference)
        if pos is None:
            return self._refuse(
                source, action, gate, ref, account, ["NO_OPEN_FOLLOWER_POSITION"], now
            )
        assert action.instrument is not None
        contract, problem = self._resolve_contract(action.instrument)
        if problem:
            return self._refuse(source, action, gate, ref, account, [problem], now)
        plan = gate.plan
        problem = self._plan_problem(plan, contract) or self._idea_contract_problem(pos, contract)
        if problem:
            return self._refuse(source, action, gate, ref, account, [problem], now)
        long = pos.position_side == PositionSide.LONG
        side = OrderSide.BUY if long else OrderSide.SELL
        qty = fill_quantity(
            Action.ADD, self.policy, self._as_position(pos),
            per_signal=plan.quantity if plan else None,
        )
        rules = _rule_problems(contract, qty)
        if rules:
            return self._refuse(source, action, gate, ref, account, rules, now, idea_id=pos.idea_id)
        entry_level = str(plan.execution_reference) if plan else action.entry_reference
        limit, reasons = self._entry_checks(
            contract, side, qty, entry_level, pos.idea_id, now
        )
        if reasons:
            return self._refuse(
                source, action, gate, ref, account, reasons, now, idea_id=pos.idea_id
            )
        entry = OrderIntent(
            order_ref=ref,
            contract=contract,
            side=side,
            quantity=qty,
            order_type=OrderType.LMT,
            limit_price=limit,
        )
        intent = self._new_intent(
            source,
            action,
            gate,
            ref,
            account,
            pos.idea_id,
            now,
            contract=contract,
            side=side.value,
            quantity=qty,
            order_type="LMT",
            limit_price=str(limit),
        )
        self._record(intent, [entry])
        self._submit(intent, entry, [])
        return self._outcome(ref)

    def _exit(self, source, action, gate, ref, account, now) -> ExecutionOutcome:
        """REDUCE / CLOSE: a marketable limit, priced from a fresh broker quote inside the policy
        collar and netted against every working exit before it is sent (review R1, S2)."""
        pos = self._open_position(action.idea_reference)
        if pos is None:
            return self._refuse(
                source, action, gate, ref, account, ["NO_OPEN_FOLLOWER_POSITION"], now
            )
        assert action.instrument is not None
        contract, problem = self._resolve_contract(action.instrument)
        if problem:
            return self._refuse(source, action, gate, ref, account, [problem], now)
        problem = self._plan_problem(gate.plan, contract) or self._idea_contract_problem(
            pos, contract
        )
        if problem:
            return self._refuse(source, action, gate, ref, account, [problem], now)
        try:
            qty = fill_quantity(
                action.action, self.policy, self._as_position(pos), action.close_fraction
            )
        except ValueError as exc:
            return self._refuse(source, action, gate, ref, account, [f"UNSIZEABLE_EXIT:{exc}"], now)
        long = pos.position_side == PositionSide.LONG
        side = OrderSide.SELL if long else OrderSide.BUY
        if qty > pos.quantity:
            return self._refuse(
                source, action, gate, ref, account, ["EXIT_EXCEEDS_FOLLOWER_POSITION"], now
            )
        rules = _rule_problems(contract, qty)
        if rules:
            return self._refuse(source, action, gate, ref, account, rules, now, idea_id=pos.idea_id)
        key = contract.contract_key
        try:
            held = sum(
                p.quantity for p in self.broker.positions() if p.contract.contract_key == key
            )
        except BrokerError as exc:
            self._broker_unreadable("positions", exc)
            return self._refuse(
                source, action, gate, ref, account, ["BROKER_POSITIONS_UNAVAILABLE"], now
            )
        reducible = held if long else -held
        if reducible < qty:
            return self._refuse(
                source,
                action,
                gate,
                ref,
                account,
                [f"EXIT_EXCEEDS_BROKER_POSITION:{reducible}<{qty}"],
                now,
            )
        limit, problem = self._exit_limit(contract, side, now)
        if problem:
            detail = (
                f"{action.action.value} {qty} of {pos.idea_id} not sent: {problem}; the protective "
                "stop at the broker remains the protection"
            )
            self.store.raise_exception(
                now, EXIT_WITHOUT_QUOTE, detail, idea_id=pos.idea_id, order_ref=ref
            )
            return self._refuse(
                source, action, gate, ref, account, [EXIT_WITHOUT_QUOTE, problem], now,
                idea_id=pos.idea_id,
            )
        order = OrderIntent(
            order_ref=ref,
            contract=contract,
            side=side,
            quantity=qty,
            order_type=OrderType.LMT,
            limit_price=limit,
            reduce_only=True,
        )
        intent = self._new_intent(
            source,
            action,
            gate,
            ref,
            account,
            pos.idea_id,
            now,
            contract=contract,
            side=side.value,
            quantity=qty,
            order_type="LMT",
            limit_price=str(limit),
        )
        self._record(intent, [order], role=Role.EXIT)
        self._submit_exit(self.store.intent(ref), order)
        return self._outcome(ref)

    def _modify(self, source, action, gate, ref, account, now) -> ExecutionOutcome:
        pos = self._open_position(action.idea_reference)
        if pos is None:
            return self._refuse(
                source, action, gate, ref, account, ["NO_OPEN_FOLLOWER_POSITION"], now
            )
        if action.stop_price is not None and pos.stop_ref is None:
            return self._refuse(
                source, action, gate, ref, account, ["NO_PROTECTIVE_STOP_TO_AMEND"], now
            )
        if action.target_price is not None and pos.target_ref is None:
            return self._refuse(
                source, action, gate, ref, account, ["NO_TARGET_ORDER_TO_AMEND"], now
            )
        contract = _contract_from(pos.contract)
        plan = gate.plan
        if plan is None:
            stop, target = action.stop_price, action.target_price
        else:  # the gateway's levels, in the series the idea's protection lives in
            stop = str(plan.stop) if plan.stop is not None else None
            target = str(plan.target) if plan.target is not None else None
            if (plan.protection_series == "REFERENCE") != (pos.protection_series == "REFERENCE"):
                return self._refuse(
                    source, action, gate, ref, account, ["PROTECTION_SERIES_MISMATCH"], now,
                    idea_id=pos.idea_id,
                )
            if pos.backstop_ref and stop is not None and plan.backstop_stop is None:
                return self._refuse(
                    source, action, gate, ref, account, ["BACKSTOP_NOT_PRICED"], now,
                    idea_id=pos.idea_id,
                )
            if contract is not None and self._key_for_plan(contract) != plan.execution_key:
                return self._refuse(
                    source, action, gate, ref, account, [CONTRACT_CHANGED], now,
                    idea_id=pos.idea_id,
                )
        if contract is not None:
            reference_series = pos.protection_series == "REFERENCE"
            rules = _rule_problems(
                contract,
                None,
                stop=None if reference_series else _dec(stop),
                target=None if reference_series else _dec(target),
                backstop=plan.backstop_stop if plan else None,
            )
            if rules:
                return self._refuse(
                    source, action, gate, ref, account, rules, now, idea_id=pos.idea_id
                )
        intent = self._new_intent(
            source,
            action,
            gate,
            ref,
            account,
            pos.idea_id,
            now,
            contract=contract,
            quantity=0,
            stop_price=stop,
            target_price=target,
            plan=plan.model_dump_json() if plan else None,
        )
        with self.store.tx() as c:
            self.store.insert_intent(c, intent)
            self.store.log(
                now,
                "INTENT_RECORDED",
                order_ref=ref,
                idea_id=pos.idea_id,
                c=c,
                action="MODIFY",
                stop=stop,
                target=target,
                backstop=str(plan.backstop_stop) if plan and plan.backstop_stop else None,
                basis=str(plan.basis_value) if plan and plan.basis_value is not None else None,
                gate=gate.reasons,
            )
        self._apply_modify(self.store.intent(ref))
        return self._outcome(ref)

    # ================================================================== submission and recovery

    def _submit(self, intent: Intent, entry: OrderIntent, children: list[OrderIntent]) -> None:
        if self._halt_blocks(intent):
            return
        attempt = self.store.intent(intent.order_ref).attempts + 1
        self.store.mark_submitted(intent.order_ref, self.clock())
        self.store.log(
            self.clock(),
            "SUBMIT",
            order_ref=intent.order_ref,
            idea_id=intent.idea_id,
            attempt=attempt,
            children=[c.order_ref for c in children],
        )
        try:
            acks = self.broker.submit(entry, children)
        except AckLost as exc:
            # Unknown outcome: look up now (adopt if visible); "no trace" waits for the settle.
            self._mark_unresolved(intent, "ACK_LOST", str(exc))
            self._resolve(self.store.intent(intent.order_ref))
            return
        except BrokerError as exc:
            self._submit_failed(intent, str(exc))
            return
        self.store.log(
            self.clock(),
            "ACK",
            order_ref=intent.order_ref,
            idea_id=intent.idea_id,
            orders={a.order_ref: a.status.value for a in acks},
        )
        self._adopt(intent, acks, [])

    # ================================================================== exit netting

    def _submit_exit(self, intent: Intent, order: OrderIntent) -> None:
        """Send a REDUCE / CLOSE only once the idea's working exit quantity is netted (review R1):
        working entry and add orders are cancelled and confirmed first (R6), a CLOSE cancels the
        earlier resting exits, and the protective children are resized (or cancelled) so that
        children + resting exits + this exit never exceed the position. Any step that cannot be
        confirmed at the broker means nothing is sent and EXIT_NOT_NETTED is opened."""
        if self._halt_blocks(intent):
            return
        problems = self._net_exit(intent)
        if problems:
            self._exit_not_netted(intent, problems)
            return
        self._submit(intent, order, [])

    def _net_exit(self, intent: Intent) -> list[str]:
        idea = intent.idea_id
        pos = self.store.position(idea)
        if pos is None or pos.quantity <= 0:
            return ["NO_OPEN_FOLLOWER_POSITION"]
        before = pos.quantity
        try:
            open_by_ref = {o.order_ref: o for o in self.broker.open_orders()}
        except BrokerError:
            return ["BROKER_OPEN_ORDERS_UNAVAILABLE"]
        rows = [r for r in self.store.orders() if r.idea_id == idea]
        for r in rows:  # R6: an entry or add still working would reopen what this exit closes
            if (
                r.role == Role.ENTRY
                and r.order_ref in open_by_ref
                and not self._cancel_confirmed(r.order_ref, idea, "working entry before an exit")
            ):
                return [f"ENTRY_CANCEL_UNCONFIRMED:{r.order_ref}"]
        for r in rows:  # a CLOSE replaces every earlier resting exit of the idea
            if (
                intent.action == Action.CLOSE
                and r.role == Role.EXIT
                and r.intent_ref != intent.order_ref
                and r.order_ref in open_by_ref
                and not self._cancel_confirmed(r.order_ref, idea, "replaced by a close")
            ):
                return [f"EXIT_CANCEL_UNCONFIRMED:{r.order_ref}"]
        # Apply anything that filled meanwhile; the children are resized to what stays uncommitted.
        if not self.sync():
            return ["BROKER_UNREADABLE"]
        pos = self.store.position(idea)
        if pos.quantity != before:
            return [f"EXIT_NETTING_POSITION_CHANGED:{before}->{pos.quantity}"]
        committed = self.store.committed_exit_quantity(idea, exclude_ref=intent.order_ref)
        left = pos.quantity - committed - intent.quantity
        if left < 0:
            return [f"EXIT_EXCEEDS_UNCOMMITTED_POSITION:{pos.quantity}-{committed}<{intent.quantity}"]
        try:
            open_by_ref = {o.order_ref: o for o in self.broker.open_orders()}
        except BrokerError:
            return ["BROKER_OPEN_ORDERS_UNAVAILABLE"]
        for ref in (pos.stop_ref, pos.target_ref, pos.backstop_ref):
            o = open_by_ref.get(ref) if ref else None
            if o is None:
                continue
            if left == 0:
                if not self._cancel_confirmed(ref, idea, "position fully committed to exits"):
                    return [f"PROTECTIVE_CANCEL_UNCONFIRMED:{ref}"]
            elif o.remaining > left and not self._resize_confirmed(o, left, idea):
                return [f"PROTECTIVE_RESIZE_UNCONFIRMED:{ref}"]
        # A cancel or resize can lose the race with a fill: only an unchanged position may exit.
        if not self.sync():
            return ["BROKER_UNREADABLE"]
        after = self.store.position(idea).quantity
        if after != before:
            return [f"EXIT_NETTING_POSITION_CHANGED:{before}->{after}"]
        return self._account_exit_check(intent)

    def _account_exit_check(self, intent: Intent) -> list[str]:
        """Broker truth: every working order on the exit side of this instrument (one-cancels-other
        siblings once, at the larger remaining), plus exits of unknown outcome, plus this exit,
        within the broker position. Never relies on a broker-side reduce-only clamp."""
        key = self._key_of(intent.symbol, self.store.position(intent.idea_id).asset_class,
                           intent.contract_key)
        try:
            held = sum(
                p.quantity for p in self.broker.positions() if p.contract.contract_key == key
            )
            working = [
                o for o in self.broker.open_orders()
                if o.contract.contract_key == key and o.side.value == intent.side
            ]
        except BrokerError:
            return ["BROKER_POSITIONS_UNAVAILABLE"]
        reducible = held if intent.side == OrderSide.SELL.value else -held
        groups: dict[str, int] = {}
        for o in working:
            key = o.parent_ref or o.order_ref
            groups[key] = max(groups.get(key, 0), o.remaining)
        visible = {o.order_ref for o in working}
        unknown = sum(
            max(i.quantity - self.store.filled_quantity(i.order_ref), 0)
            for i in self.store.intents(UNRESOLVED_STATES)
            if i.action in (Action.REDUCE, Action.CLOSE)
            and (i.contract_key or i.symbol) == (intent.contract_key or intent.symbol)
            and i.order_ref not in visible
            and i.order_ref != intent.order_ref
        )
        committed = sum(groups.values()) + unknown
        if committed + intent.quantity > reducible:
            return [f"EXIT_WOULD_OVERCOMMIT:{committed}+{intent.quantity}>{reducible}"]
        return []

    def _confirm(self, predicate) -> bool:
        for attempt in range(self.config.confirm_attempts):
            if attempt and self.config.confirm_wait_seconds:
                time.sleep(self.config.confirm_wait_seconds)
            try:
                if predicate({o.order_ref: o for o in self.broker.open_orders()}):
                    return True
            except BrokerError:
                continue
        return False

    def _cancel_confirmed(self, ref: str, idea_id: str, reason: str) -> bool:
        """Cancel a working order and confirm it has left the broker's working orders and reached a
        terminal state (cancelled, or filled if the cancel lost the race)."""
        now = self.clock()
        try:
            self.broker.cancel(ref)
        except OrderNotFound:
            pass  # no longer working: confirmed below
        except BrokerError as exc:
            self.store.log(now, "CANCEL_FAILED", order_ref=ref, idea_id=idea_id, error=str(exc))
            return False

        def gone(open_by_ref) -> bool:
            if ref in open_by_ref:
                return False
            return any(o.order_ref == ref for o in self.broker.completed_orders())

        if self._confirm(gone):
            self.store.log(self.clock(), "CANCEL_CONFIRMED", order_ref=ref, idea_id=idea_id,
                           reason=reason)
            return True
        self.store.log(self.clock(), "CANCEL_UNCONFIRMED", order_ref=ref, idea_id=idea_id,
                       reason=reason)
        return False

    def _resize_confirmed(self, o: BrokerOrder, left: int, idea_id: str) -> bool:
        try:
            self.broker.modify(o.order_ref, quantity=o.filled_quantity + left)
        except BrokerError as exc:
            self.store.log(self.clock(), "PROTECTIVE_RESIZE_FAILED", order_ref=o.order_ref,
                           idea_id=idea_id, error=str(exc))
            return False

        def sized(open_by_ref) -> bool:
            x = open_by_ref.get(o.order_ref)
            return (
                x is not None and x.remaining == left and x.status != OrderStatus.PENDING_CANCEL
            )

        if self._confirm(sized):
            self.store.log(self.clock(), "PROTECTIVE_RESIZED", order_ref=o.order_ref,
                           idea_id=idea_id, before=o.remaining, after=left, confirmed=True)
            return True
        self.store.log(self.clock(), "PROTECTIVE_RESIZE_UNCONFIRMED", order_ref=o.order_ref,
                       idea_id=idea_id, wanted=left)
        return False

    def _exit_not_netted(self, intent: Intent, problems: list[str]) -> None:
        now = self.clock()
        self.store.set_state(
            intent.order_ref, IntentState.REFUSED, now, reasons=[EXIT_NOT_NETTED, *problems]
        )
        for row in self.store.orders(intent.order_ref):
            self.store.update_order(row.order_ref, now, status="NotSubmitted")
        detail = f"{intent.action.value} {intent.quantity} not sent: {problems}"
        self.store.raise_exception(
            now, EXIT_NOT_NETTED, detail, idea_id=intent.idea_id, order_ref=intent.order_ref
        )
        self.store.log(
            now,
            EXIT_NOT_NETTED,
            order_ref=intent.order_ref,
            idea_id=intent.idea_id,
            problems=problems,
            rule="working exits could not be netted against the position: nothing sent",
        )
        self.sync()  # the intent is terminal now: children are sized back to the position

    # ================================================================== submission helpers

    def _mark_unresolved(self, intent: Intent, event: str, error: str) -> None:
        self.store.set_state(
            intent.order_ref, IntentState.UNRESOLVED, self.clock(), broker_status="Unknown"
        )
        self.store.log(
            self.clock(),
            event,
            order_ref=intent.order_ref,
            idea_id=intent.idea_id,
            error=error,
            rule="outcome unknown: look up by order_ref before any resend",
        )

    def _resolve(self, intent: Intent) -> None:
        """Look the order up at the broker; adopt what is found; resend only on a no-trace."""
        if intent.action == Action.MODIFY:
            self._apply_modify(intent, recovering=True)
            return
        try:
            open_orders = self.broker.open_orders()
            fills = self.broker.executions()
            completed = self.broker.completed_orders(since=intent.created_at - COMPLETED_LOOKBACK)
        except BrokerError as exc:
            self._mark_unresolved(intent, "LOOKUP_FAILED", str(exc))
            self._broker_unreadable("lookup", exc)
            return
        rows = self.store.orders(intent.order_ref)
        refs = {r.order_ref for r in rows}
        found_open = [o for o in open_orders if o.order_ref in refs]
        found_fills = [f for f in fills if f.order_ref in refs]
        # A ref still working wins over an older terminal row under the same ref (a resend).
        working_refs = {o.order_ref for o in found_open}
        found_done = [
            o for o in completed if o.order_ref in refs and o.order_ref not in working_refs
        ]
        if found_open or found_fills or found_done:
            if self.store.resolve_exceptions(
                self.clock(), UNKNOWN_OUTCOME_ENTRY_NOT_RESENT, idea_id=intent.idea_id,
                order_ref=intent.order_ref,
            ):
                self.store.log(
                    self.clock(), "UNKNOWN_OUTCOME_SETTLED", order_ref=intent.order_ref,
                    idea_id=intent.idea_id, rule="the order appeared at the broker: adopted",
                )
            self.store.log(
                self.clock(),
                "ADOPTED_FROM_BROKER",
                order_ref=intent.order_ref,
                idea_id=intent.idea_id,
                open_orders=[o.order_ref for o in found_open],
                executions=[f.exec_id for f in found_fills],
                completed_orders={o.order_ref: o.status.value for o in found_done},
                rule="order found by order_ref; adopted, not resent",
            )
            self._adopt(intent, [*found_done, *found_open], found_fills)
            return
        now = self.clock()
        if intent.attempts > 0 and intent.submitted_at is not None:
            since = (now - intent.submitted_at).total_seconds()
            if since > self.config.lookup_horizon_seconds:
                self._beyond_horizon(intent, sorted(refs), since)
                return
            if intent.action in (Action.OPEN, Action.ADD):
                self._entry_not_resent(intent, sorted(refs), since)
                return
            lookups = self._no_trace_lookups(intent, now, since)
            span = (lookups[-1] - lookups[0]).total_seconds()
            if (
                since < self.config.settle_seconds
                or len(lookups) < self.config.exit_no_trace_lookups
                or span < self.config.settle_seconds
            ):
                self.store.log(
                    now,
                    "LOOKUP_NOT_SETTLED",
                    order_ref=intent.order_ref,
                    idea_id=intent.idea_id,
                    seconds_since_submit=round(since, 3),
                    settle_seconds=self.config.settle_seconds,
                    no_trace_lookups=len(lookups),
                    needed=self.config.exit_no_trace_lookups,
                    rule=(
                        "exit: resent only after the settle period and enough consecutive "
                        "no-trace lookups spread over it"
                    ),
                )
                return
        if intent.attempts >= self.config.max_submit_attempts:
            # Journaled once; the intent's age keeps counting so reconcile reports it stuck.
            if not any(
                e.event == "NO_TRACE_RESEND_BUDGET_EXHAUSTED"
                for e in self.store.journal(intent.order_ref)
            ):
                self._mark_unresolved(
                    intent,
                    "NO_TRACE_RESEND_BUDGET_EXHAUSTED",
                    f"{intent.attempts} submissions already made",
                )
            return
        if intent.action in (Action.OPEN, Action.ADD):
            problems = self._resend_checks(intent, now)
            if problems:
                self._expire(intent, sorted(refs), problems)
                return
        elif (now - intent.created_at).total_seconds() > self.config.max_exit_intent_age_seconds:
            if not self._reprice_exit(intent, now):
                return
            rows = self.store.orders(intent.order_ref)
        self.store.log(
            now,
            "NO_TRACE_AT_BROKER",
            order_ref=intent.order_ref,
            idea_id=intent.idea_id,
            checked=sorted(refs),
            attempts=intent.attempts,
            rule=(
                "open_orders, executions and completed_orders show no order or fill for this ref; "
                "resubmitting"
            ),
        )
        entry, children = self._rebuild(intent, rows)
        if intent.action in (Action.REDUCE, Action.CLOSE):
            self._submit_exit(intent, entry)
        else:
            self._submit(intent, entry, children)

    def _no_trace_lookups(self, intent: Intent, now: datetime, since: float) -> list[datetime]:
        """Journal this no-trace lookup and return the times of the consecutive no-trace lookups
        since the last submission (a failed lookup breaks the run)."""
        self.store.log(
            now, "NO_TRACE_LOOKUP", order_ref=intent.order_ref, idea_id=intent.idea_id,
            seconds_since_submit=round(since, 3),
        )
        times = []
        for e in reversed(self.store.journal(intent.order_ref)):
            if e.event in ("SUBMIT", "LOOKUP_FAILED"):
                break
            if e.event == "NO_TRACE_LOOKUP":
                times.append(e.at)
        return sorted(times)

    def _entry_not_resent(self, intent: Intent, checked: list[str], since: float) -> None:
        """Review F5: a submitted entry or add of unknown outcome is never resent. It stays
        UNRESOLVED and is looked up again on every cycle; after the settle period an exception asks
        a human to settle it, and new exposure in its instrument stays blocked meanwhile."""
        now = self.clock()
        if since < self.config.settle_seconds:
            self.store.log(
                now,
                "LOOKUP_NOT_SETTLED",
                order_ref=intent.order_ref,
                idea_id=intent.idea_id,
                seconds_since_submit=round(since, 3),
                settle_seconds=self.config.settle_seconds,
                rule="no trace yet; stays UNRESOLVED and is looked up again later",
            )
            return
        detail = (
            f"submitted {since:.0f} s ago, outcome unknown, no trace of {checked} yet; an entry or "
            "add is never resent automatically. It is looked up every cycle and adopted if it "
            "appears; new exposure in this instrument is blocked until then."
        )
        if self.store.raise_exception(
            now, UNKNOWN_OUTCOME_ENTRY_NOT_RESENT, detail, idea_id=intent.idea_id,
            order_ref=intent.order_ref,
        ):
            self.store.log(
                now, UNKNOWN_OUTCOME_ENTRY_NOT_RESENT, order_ref=intent.order_ref,
                idea_id=intent.idea_id, checked=checked,
                rule="unknown outcome: an entry or add is never resent",
            )

    def _resend_checks(self, intent: Intent, now: datetime) -> list[str]:
        """Everything an entry or add must pass at the moment of a resend, exactly as if it were
        new: source freshness, intent age, then the broker-quote sanity, collar and exposure caps
        of ``_entry_checks`` (the halt is checked in ``_submit``). Empty means it may be resent."""
        problems = []
        if intent.source_published_at is None or not source_is_fresh(
            intent.source_published_at, now, self.policy
        ):
            problems.append("SOURCE_NOT_FRESH")
        if (now - intent.created_at).total_seconds() > self.config.max_intent_age_seconds:
            problems.append("INTENT_TOO_OLD")
        if problems:
            return problems
        pos = self.store.position(intent.idea_id)
        if pos is None or intent.side is None:
            return ["IDEA_UNKNOWN"]
        contract, problem = self._contract_for(intent)
        if problem:
            return [problem]
        limit, reasons = self._entry_checks(
            contract, OrderSide(intent.side), intent.quantity, intent.entry_reference,
            intent.idea_id, now, exclude_ref=intent.order_ref,
        )
        if reasons:
            return reasons
        if intent.limit_price is not None and _dec(intent.limit_price) != limit:
            return ["RESEND_LIMIT_WOULD_DIFFER"]
        return []

    def _reprice_exit(self, intent: Intent, now: datetime) -> bool:
        """An exit older than ``max_exit_intent_age_seconds`` is re-priced from a fresh quote
        exactly as a new exit would be. Without a usable quote it ends REFUSED with an open
        EXIT_WITHOUT_QUOTE exception; it is never sent at its old limit. True when re-priced."""
        pos = self.store.position(intent.idea_id)
        contract, problem = (None, "IDEA_UNKNOWN") if pos is None or intent.side is None else (
            self._contract_for(intent)
        )
        limit = None
        if problem is None:
            limit, problem = self._exit_limit(contract, OrderSide(intent.side), now)
        if problem:
            self.store.set_state(
                intent.order_ref, IntentState.REFUSED, now, reasons=[EXIT_WITHOUT_QUOTE, problem]
            )
            for row in self.store.orders(intent.order_ref):
                self.store.update_order(row.order_ref, now, status="NotSubmitted")
            detail = (
                f"{intent.action.value} {intent.quantity} of {intent.idea_id}, recorded "
                f"{intent.created_at.isoformat()}, not sent: {problem}; never sent at its old "
                "limit. The protective stop at the broker, if any, remains the protection."
            )
            self.store.raise_exception(
                now, EXIT_WITHOUT_QUOTE, detail, idea_id=intent.idea_id, order_ref=intent.order_ref
            )
            self.store.log(
                now, EXIT_WITHOUT_QUOTE, order_ref=intent.order_ref, idea_id=intent.idea_id,
                problem=problem, rule="stale exit and no usable quote: not sent",
            )
            self.sync()  # terminal now: protection is sized back to the position
            return False
        old = intent.limit_price
        self.store.set_intent_limit(intent.order_ref, str(limit), now)
        row = self.store.order(intent.order_ref)
        self.store.update_order(intent.order_ref, now, status=row.status, limit_price=str(limit))
        self.store.log(
            now, "EXIT_REPRICED", order_ref=intent.order_ref, idea_id=intent.idea_id,
            old_limit=old, new_limit=str(limit),
            rule="exit older than the maximum intent age: priced from a fresh quote",
        )
        return True

    def _submit_failed(self, intent: Intent, error: str) -> None:
        """A definite submit error is a known outcome: recorded, never resent."""
        now = self.clock()
        self.store.set_state(
            intent.order_ref, IntentState.REJECTED, now, broker_status="SubmitError",
            reasons=[SUBMIT_ERROR],
        )
        for row in self.store.orders(intent.order_ref):
            self.store.update_order(row.order_ref, now, status="NotSubmitted")
        if intent.action == Action.OPEN:
            pos = self.store.position(intent.idea_id)
            if pos is not None and pos.quantity == 0:
                self.store.update_position(intent.idea_id, now, status="NOT_FILLED")
        self.store.log(
            now,
            SUBMIT_ERROR,
            order_ref=intent.order_ref,
            idea_id=intent.idea_id,
            error=error,
            rule="definite submit error: outcome known, recorded, never resent",
        )

    def _beyond_horizon(self, intent: Intent, checked: list[str], since: float) -> None:
        """Older than the broker's lookup history: absence cannot be proven. Never resent; it stays
        UNRESOLVED with an open exception for a human."""
        now = self.clock()
        detail = (
            f"submitted {since:.0f} s ago, beyond the {self.config.lookup_horizon_seconds} s "
            f"lookup horizon; no trace of {checked} proves nothing. Resolve at the broker by hand."
        )
        if self.store.raise_exception(
            now,
            UNKNOWN_OUTCOME_BEYOND_LOOKUP_HORIZON,
            detail,
            idea_id=intent.idea_id,
            order_ref=intent.order_ref,
        ):
            self.store.log(
                now,
                UNKNOWN_OUTCOME_BEYOND_LOOKUP_HORIZON,
                order_ref=intent.order_ref,
                idea_id=intent.idea_id,
                checked=checked,
                rule="outcome unknown beyond the lookup horizon: never resent",
            )

    def _expire(self, intent: Intent, checked: list[str], problems: list[str]) -> None:
        """No trace anywhere at the broker, and a resend would not pass the entry checks now: the
        entry is never sent. The intent ends REFUSED and an exception record makes it visible."""
        now = self.clock()
        self.store.set_state(
            intent.order_ref,
            IntentState.REFUSED,
            now,
            reasons=[STALE_AFTER_UNKNOWN_OUTCOME, *problems],
        )
        for row in self.store.orders(intent.order_ref):
            self.store.update_order(row.order_ref, now, status="NotSubmitted")
        if intent.action == Action.OPEN:
            pos = self.store.position(intent.idea_id)
            if pos is not None and pos.quantity == 0:
                self.store.update_position(intent.idea_id, now, status="NOT_FILLED")
        published = intent.source_published_at.isoformat() if intent.source_published_at else None
        detail = (
            f"no trace in open orders, executions or completed orders for {checked}; a resend "
            f"now fails {problems} (source published {published or 'at an unknown time'})"
        )
        self.store.raise_exception(
            now,
            STALE_AFTER_UNKNOWN_OUTCOME,
            detail,
            idea_id=intent.idea_id,
            order_ref=intent.order_ref,
        )
        self.store.log(
            now,
            STALE_AFTER_UNKNOWN_OUTCOME,
            order_ref=intent.order_ref,
            idea_id=intent.idea_id,
            checked=checked,
            problems=problems,
            source_published_at=published,
            rule="outcome unknown, no trace at the broker, resend checks fail: never resent",
        )

    def _adopt(self, intent: Intent, orders: list[BrokerOrder], fills) -> None:
        """Take the broker's view of the intent's orders as the truth, then apply fills."""
        now = self.clock()
        by_ref = {o.order_ref: o for o in orders}
        rows = self.store.orders(intent.order_ref)
        for row in rows:
            o = by_ref.get(row.order_ref)
            if o is not None:
                self.store.update_order(
                    row.order_ref, now, status=o.status.value, quantity=o.quantity
                )
        entry = by_ref.get(intent.order_ref)
        if entry is not None:
            state = _intent_state(entry.status, entry.filled_quantity, entry.quantity)
            broker_status = entry.status.value
            reasons = [entry.reason] if entry.reason else None
        else:
            # The entry's own row is not visible: only its own executions can settle it. A child
            # order's visibility or status proves nothing about the parent (IBKR brackets).
            filled = sum(f.quantity for f in fills if f.order_ref == intent.order_ref)
            if filled < intent.quantity:
                self._entry_state_unknown(
                    intent,
                    f"the entry's own row is in neither open nor completed orders and its "
                    f"executions show {filled} of {intent.quantity}",
                )
                self.sync()
                return
            state, broker_status, reasons = IntentState.FILLED, None, None
        if intent.action in (Action.OPEN, Action.ADD) and self.store.resolve_exceptions(
            now, ENTRY_STATE_UNKNOWN, idea_id=intent.idea_id, order_ref=intent.order_ref
        ):
            self.store.log(now, "ENTRY_STATE_SETTLED", order_ref=intent.order_ref,
                           idea_id=intent.idea_id, state=state.value)
        self.store.set_state(
            intent.order_ref, state, now, broker_status=broker_status, reasons=reasons
        )
        if state == IntentState.CANCELLED and intent.action == Action.OPEN:
            pos = self.store.position(intent.idea_id)
            if pos is not None and pos.quantity == 0 and not fills:
                self.store.update_position(intent.idea_id, now, status="NOT_FILLED")
        if state == IntentState.REJECTED:
            for row in rows:
                self.store.update_order(row.order_ref, now, status=OrderStatus.REJECTED.value)
            if intent.action == Action.OPEN:
                self.store.update_position(intent.idea_id, now, status="NOT_FILLED")
            self.store.log(
                now,
                "REJECTED",
                order_ref=intent.order_ref,
                idea_id=intent.idea_id,
                reason=entry.reason if entry else "",
            )
        self.sync()

    def _entry_state_unknown(self, intent: Intent, detail: str) -> None:
        """Never FILLED or CANCELLED on inference: the intent stays (or becomes) UNRESOLVED, is
        looked up again every cycle, and an ENTRY_STATE_UNKNOWN exception blocks new exposure in
        the instrument until the entry's own evidence settles it."""
        now = self.clock()
        self.store.set_state(intent.order_ref, IntentState.UNRESOLVED, now, broker_status="Unknown")
        if intent.action not in (Action.OPEN, Action.ADD):
            self.store.log(now, "EXIT_STATE_UNKNOWN", order_ref=intent.order_ref,
                           idea_id=intent.idea_id, detail=detail)
            return
        if self.store.raise_exception(
            now, ENTRY_STATE_UNKNOWN, f"{intent.order_ref}: {detail}", idea_id=intent.idea_id,
            order_ref=intent.order_ref,
        ):
            self.store.log(
                now, ENTRY_STATE_UNKNOWN, order_ref=intent.order_ref, idea_id=intent.idea_id,
                detail=detail, rule="an entry's fill is decided only from its own evidence",
            )

    def _rebuild(
        self, intent: Intent, rows: list[OrderRow]
    ) -> tuple[OrderIntent, list[OrderIntent]]:
        """Recreate exactly the orders recorded write-ahead (same refs, same prices, same sizes)."""
        assert intent.side is not None
        contract = _contract_from(intent.contract) or self.broker.resolve(
            Instrument(
                asset_class=self.store.position(intent.idea_id).asset_class, symbol=intent.symbol
            )
        )
        built = []
        for r in rows:
            built.append(
                OrderIntent(
                    order_ref=r.order_ref,
                    contract=contract,
                    side=OrderSide(r.side),
                    quantity=r.quantity,
                    order_type=OrderType(r.order_type),
                    limit_price=_dec(r.limit_price),
                    stop_price=_dec(r.stop_price),
                    tif=(
                        self.config.protective_tif
                        if r.role in (Role.STOP, Role.TARGET, Role.BACKSTOP)
                        else TimeInForce.DAY
                    ),
                    parent_ref=r.parent_ref,
                    reduce_only=r.role != Role.ENTRY,
                    condition=(
                        PriceCondition.model_validate_json(r.condition) if r.condition else None
                    ),
                )
            )
        return built[0], built[1:]

    def _halt_blocks(self, intent: Intent) -> bool:
        if not self.halted():
            return False
        now = self.clock()
        self.store.set_state(
            intent.order_ref, IntentState.HALTED, now, reasons=["HALT_SWITCH_PRESENT"]
        )
        for row in self.store.orders(intent.order_ref):
            self.store.update_order(row.order_ref, now, status="NotSubmitted")
        if intent.action == Action.OPEN:
            self.store.update_position(intent.idea_id, now, status="NOT_FILLED")
        self.store.log(
            now,
            "HALTED",
            order_ref=intent.order_ref,
            idea_id=intent.idea_id,
            halt_file=str(self.config.halt_file),
            detail="halt switch present; nothing submitted",
        )
        return True

    # ================================================================== protective orders

    def _maintain_protection(self, idea_id: str) -> None:
        """Size the idea's working stop/target to the ledger quantity less what is committed to
        exits that may be live (cancel when that is zero or the idea is flat), then confirm at the
        broker that a working stop covers exactly that quantity. Raises UNPROTECTED_POSITION
        otherwise. Never reports protection it has not seen."""
        pos = self.store.position(idea_id)
        if pos is None:
            return
        now = self.clock()
        try:
            open_by_ref = {o.order_ref: o for o in self.broker.open_orders()}
        except BrokerError as exc:
            self._protection_unverified(pos, str(exc))
            self._broker_unreadable("open_orders", exc)
            return
        refs = [r for r in (pos.stop_ref, pos.target_ref, pos.backstop_ref) if r]
        if pos.quantity == 0:
            if pos.status == "PENDING":
                return
            for ref in refs:
                if ref in open_by_ref:
                    # Risk-reducing housekeeping; allowed even while halted.
                    self._try(
                        lambda r=ref: self.broker.cancel(r),
                        "PROTECTIVE_CANCELLED",
                        idea_id,
                        ref,
                        reason="follower position is flat",
                    )
            self.store.resolve_exceptions(now, UNPROTECTED_POSITION, idea_id=idea_id)
            self.store.resolve_exceptions(now, PROTECTION_UNVERIFIED, idea_id=idea_id)
            return
        cover = max(pos.quantity - self.store.committed_exit_quantity(idea_id), 0)
        changed = False
        for ref in refs:
            o = open_by_ref.get(ref)
            if o is None or o.status == OrderStatus.PENDING_CANCEL:
                continue
            if cover == 0:
                # Everything held is committed to working exits: a child would over-commit.
                changed |= self._try(
                    lambda r=ref: self.broker.cancel(r),
                    "PROTECTIVE_CANCELLED",
                    idea_id,
                    ref,
                    reason="position fully committed to working exits",
                )
                continue
            # Allowed under the halt: sizing protection to the position never adds exposure.
            if o.remaining != cover and self._try(
                lambda o=o: self.broker.modify(o.order_ref, quantity=o.filled_quantity + cover),
                "PROTECTIVE_RESIZED",
                idea_id,
                ref,
                before=o.remaining,
                after=cover,
            ):
                changed = True
        if changed:
            try:
                open_by_ref = {o.order_ref: o for o in self.broker.open_orders()}
            except BrokerError as exc:
                self._protection_unverified(pos, str(exc))
                self._broker_unreadable("open_orders", exc)
                return
        for ref in refs:
            o = open_by_ref.get(ref)
            if o is not None:
                self.store.update_order(ref, now, status=o.status.value, quantity=o.quantity)
        if pos.stop_price is None:
            return
        # This check succeeded after every fill applied so far: its verdict is the protection.
        self.store.resolve_exceptions(now, PROTECTION_UNVERIFIED, idea_id=idea_id)
        if cover == 0:
            # Fully committed to working exits; no stop may work beside them (never CONFIRMED).
            self.store.update_position(idea_id, now, protection="EXITING")
            return
        # CONFIRMED needs the fills read after the working orders: a fill the ledger has not applied
        # yet means the working-orders view may describe a position that no longer exists (F1).
        mine = {r.order_ref for r in self.store.orders() if r.idea_id == idea_id}
        try:
            late = [
                f.exec_id
                for f in self.broker.executions()
                if f.order_ref in mine and not self.store.has_fill(f.exec_id)
            ]
        except BrokerError as exc:
            self._protection_unverified(pos, str(exc))
            self._broker_unreadable("executions", exc)
            return
        if late:
            self.store.update_position(idea_id, now, protection="UNKNOWN")
            self.store.log(
                now, "PROTECTION_CHECK_STALE", idea_id=idea_id, unapplied=late,
                rule="fills arrived after the working-orders read; checked again next sync",
            )
            return
        stop = open_by_ref.get(pos.stop_ref) if pos.stop_ref else None
        exit_side = OrderSide.SELL if pos.position_side == PositionSide.LONG else OrderSide.BUY
        # Review F3: type, side, quantity AND price against the ledger's confirmed stop. A
        # reference-triggered stop is confirmed only as a working conditional exit in the right
        # contract, on the right reference series, direction and trigger level (multi-asset).
        price_ok = stop is not None and self._stop_matches(stop, pos)
        # A PreSubmitted child is present only once its parent's fill is established by the
        # parent's own executions (at IBKR it is PreSubmitted before the fill as well).
        parent_ok = stop is not None and (
            stop.status != OrderStatus.PRE_SUBMITTED
            or self.store.filled_quantity(stop.parent_ref or pos.stop_ref.rsplit(".", 1)[0]) > 0
        )
        backstop_problem = self._backstop_problem(pos, open_by_ref, cover, exit_side)
        if (
            stop is not None
            and stop.working
            and stop.status != OrderStatus.PENDING_CANCEL
            and stop.remaining == cover
            and stop.side == exit_side
            and price_ok
            and parent_ok
            and backstop_problem is None
        ):
            self.store.update_position(idea_id, now, protection="CONFIRMED")
            if self.store.resolve_exceptions(now, UNPROTECTED_POSITION, idea_id=idea_id):
                self.store.log(now, "PROTECTION_RESTORED", order_ref=pos.stop_ref, idea_id=idea_id)
            return
        self.store.update_position(idea_id, now, protection="UNPROTECTED")
        detail = (
            f"ledger holds {pos.quantity} ({cover} not committed to exits); working stop covers "
            f"{stop.remaining if stop else 0} ({stop.status.value if stop else 'absent'})"
        )
        if stop is not None and not price_ok:
            working = (
                f"condition {stop.condition.direction.value} {stop.condition.trigger_price} on "
                f"{stop.condition.contract.contract_key}" if stop.condition
                else f"{stop.order_type.value} {stop.stop_price}"
            )
            detail += f"; STOP_PRICE_MISMATCH: working stop {working}, ledger {pos.stop_price}"
        if backstop_problem:
            detail += f"; {backstop_problem}"
        if self.store.raise_exception(
            now, UNPROTECTED_POSITION, detail, idea_id=idea_id, order_ref=pos.stop_ref
        ):
            self.store.log(
                now, UNPROTECTED_POSITION, order_ref=pos.stop_ref, idea_id=idea_id, detail=detail
            )

    @staticmethod
    def _stop_matches(stop: BrokerOrder, pos: LedgerPosition) -> bool:
        level = _dec(pos.stop_price)
        if pos.protection_series != "REFERENCE":
            return stop.order_type == OrderType.STP and stop.condition is None \
                and stop.stop_price == level
        c = stop.condition
        long = pos.position_side == PositionSide.LONG
        want = ConditionDirection.BELOW if long else ConditionDirection.ABOVE
        return (
            c is not None
            and c.contract.contract_key == pos.reference_key
            and c.direction == want
            and c.trigger_price == level
            and stop.order_type == c.released_order_type
            and (pos.contract_key is None or stop.contract.contract_key == pos.contract_key)
        )

    @staticmethod
    def _backstop_problem(pos, open_by_ref, cover: int, exit_side: OrderSide) -> str | None:
        """A required backstop counts only as a working STP in the execution contract at the
        ledger's backstop price covering the same quantity as the stop."""
        if not pos.backstop_ref:
            return None
        b = open_by_ref.get(pos.backstop_ref)
        if (
            b is None
            or not b.working
            or b.status == OrderStatus.PENDING_CANCEL
            or b.order_type != OrderType.STP
            or b.side != exit_side
            or b.remaining != cover
            or b.stop_price != _dec(pos.backstop_price)
        ):
            have = f"{b.order_type.value} {b.stop_price} x{b.remaining}" if b else "absent"
            return f"BACKSTOP_NOT_CONFIRMED: want STP {pos.backstop_price} x{cover}, have {have}"
        return None

    def _protection_unverified(self, pos: LedgerPosition, error: str) -> None:
        """The broker could not be read: protection is UNKNOWN until a later check succeeds."""
        now = self.clock()
        self.store.log(now, "PROTECTION_CHECK_FAILED", idea_id=pos.idea_id, error=error)
        if pos.quantity == 0 or pos.stop_price is None:
            return
        self.store.update_position(pos.idea_id, now, protection="UNKNOWN")
        detail = f"ledger holds {pos.quantity}; open orders unreadable after the last fill: {error}"
        if self.store.raise_exception(
            now, PROTECTION_UNVERIFIED, detail, idea_id=pos.idea_id, order_ref=pos.stop_ref
        ):
            self.store.log(
                now, PROTECTION_UNVERIFIED, order_ref=pos.stop_ref, idea_id=pos.idea_id,
                detail=detail,
            )

    def _protection(self, idea_id: str) -> str:
        """CONFIRMED only when a successful broker check after the last fill saw the stop."""
        pos = self.store.position(idea_id)
        if pos is None or pos.stop_price is None:
            return "NOT_REQUIRED"
        open_ = self.store.exceptions(open_only=True)
        if any(e.kind == UNPROTECTED_POSITION and e.idea_id == idea_id for e in open_):
            return "UNPROTECTED"
        if pos.quantity == 0:
            return "PENDING" if pos.status == "PENDING" else "NOT_REQUIRED"
        if any(e.kind == BROKER_STATE_UNKNOWN for e in open_):
            return "UNKNOWN"
        if pos.protection in ("CONFIRMED", "EXITING"):
            return pos.protection
        return "UNKNOWN"

    def _apply_modify(self, intent: Intent, *, recovering: bool = False) -> None:
        """Amend the working stop/target to absolute prices, then confirm them at the broker.
        An absolute-price amendment is idempotent: re-issuing it after a lost ack cannot double up.
        """
        pos = self.store.position(intent.idea_id)
        now = self.clock()
        reference_series = pos.protection_series == "REFERENCE"
        level_field = "trigger_price" if reference_series else "stop_price"
        plan = _plan_of(intent.plan)
        wanted = [
            (pos.stop_ref, level_field, _dec(intent.stop_price)),
            (pos.target_ref, "trigger_price" if reference_series else "limit_price",
             _dec(intent.target_price)),
            (pos.backstop_ref, "stop_price", plan.backstop_stop if plan else None),
        ]
        wanted = [(ref, field, price) for ref, field, price in wanted if price is not None and ref]
        try:
            open_by_ref = {o.order_ref: o for o in self.broker.open_orders()}
        except BrokerError as exc:
            self._mark_unresolved(intent, "LOOKUP_FAILED", str(exc))
            return
        pending = [
            (r, f, p) for r, f, p in wanted if r in open_by_ref and _field(open_by_ref[r], f) != p
        ]
        missing = [r for r, _, _ in wanted if r not in open_by_ref]
        if missing:
            self._modify_failed(intent, f"protective order not working at broker: {missing}")
            return
        if pending:
            if recovering:
                self.store.log(
                    now,
                    "AMENDMENT_NOT_SEEN",
                    order_ref=intent.order_ref,
                    idea_id=intent.idea_id,
                    rule="absolute-price amendment is idempotent; re-issuing",
                )
            if self._halt_blocks(intent):
                return
            self.store.set_state(intent.order_ref, IntentState.RECORDED, now, attempts_delta=1)
            for ref, field, price in pending:
                self.store.log(self.clock(), "AMEND_SUBMIT", order_ref=ref, idea_id=intent.idea_id)
                try:
                    if field == "trigger_price":
                        amend = getattr(self.broker, "modify_condition", None)
                        if not callable(amend):
                            raise BrokerError("broker cannot amend price conditions")
                        amend(ref, trigger_price=price)
                    else:
                        self.broker.modify(ref, **{field: price})
                    self.store.log(
                        self.clock(),
                        "AMEND",
                        order_ref=ref,
                        idea_id=intent.idea_id,
                        **{field: str(price)},
                    )
                except AckLost as exc:
                    self._mark_unresolved(intent, "ACK_LOST", str(exc))
                    if not recovering:
                        self._apply_modify(self.store.intent(intent.order_ref), recovering=True)
                    return
                except BrokerError as exc:
                    self._modify_failed(intent, str(exc))
                    return
            try:
                open_by_ref = {o.order_ref: o for o in self.broker.open_orders()}
            except BrokerError as exc:
                self._mark_unresolved(intent, "LOOKUP_FAILED", str(exc))
                return
        unconfirmed = [
            r for r, f, p in wanted if r not in open_by_ref or _field(open_by_ref[r], f) != p
        ]
        if unconfirmed:
            self._modify_failed(intent, f"amendment not visible at broker: {unconfirmed}")
            return
        fields = {}
        for ref, field, price in wanted:
            column = "limit_price" if field == "limit_price" else "stop_price"
            self.store.update_order(
                ref, now, status=open_by_ref[ref].status.value, **{column: str(price)}
            )
            if ref == pos.backstop_ref:
                fields["backstop_price"] = str(price)
            elif ref == pos.stop_ref:
                fields["stop_price"] = str(price)
            else:
                fields["target_price"] = str(price)
        self.store.update_position(intent.idea_id, now, **fields)
        self.store.set_state(intent.order_ref, IntentState.APPLIED, now)
        self.store.log(
            self.clock(),
            "AMENDMENT_CONFIRMED",
            order_ref=intent.order_ref,
            idea_id=intent.idea_id,
            **fields,
        )

    def _modify_failed(self, intent: Intent, detail: str) -> None:
        now = self.clock()
        self.store.set_state(
            intent.order_ref, IntentState.REJECTED, now, reasons=[MODIFY_NOT_CONFIRMED]
        )
        self.store.raise_exception(
            now, MODIFY_NOT_CONFIRMED, detail, idea_id=intent.idea_id, order_ref=intent.order_ref
        )
        self.store.log(
            now,
            MODIFY_NOT_CONFIRMED,
            order_ref=intent.order_ref,
            idea_id=intent.idea_id,
            detail=detail,
        )

    # ================================================================== checks and helpers

    def _entry_checks(
        self,
        contract: ContractRef,
        side: OrderSide,
        qty: int,
        reference: str | None,
        idea_id: str,
        now: datetime,
        exclude_ref: str | None = None,
    ) -> tuple[Decimal | None, list[str]]:
        """Broker-quote sanity, the price collar and exposure caps, before any entry or add.
        Returns the collar-bounded limit price, or the reasons to refuse."""
        key = contract.contract_key
        blocked = self._instrument_block(contract)
        if blocked:
            return None, [f"INSTRUMENT_BLOCKED_{blocked}"]
        if any(
            i.action in (Action.OPEN, Action.ADD)
            and ((i.contract_key == key) if i.contract_key else (i.symbol == contract.symbol))
            and i.order_ref != exclude_ref
            for i in self.store.intents(UNRESOLVED_STATES)
        ):
            # Review F5: while an entry or add of unknown outcome may be live, nothing is added.
            return None, [UNRESOLVED_ENTRY_IN_INSTRUMENT]
        q, problem = self._usable_quote(contract, now)
        if problem:
            return None, [problem]
        ref = _dec(reference)
        if ref is None:
            return None, ["ENTRY_REFERENCE_MISSING"]
        buy = side == OrderSide.BUY
        price = q.ask if buy else q.bid
        band = self.policy.max_price_deviation_pct / Decimal(100)
        if abs(price - ref) / ref > band:
            return None, ["BROKER_QUOTE_OUTSIDE_COLLAR"]
        # Snapped to the contract's tick inside the collar: a buy limit down, a sell limit up.
        limit = (
            snap_to_tick(ref * (1 + band), contract.min_tick, ROUND_FLOOR)
            if buy
            else snap_to_tick(ref * (1 - band), contract.min_tick, ROUND_CEILING)
        )
        mult = contract.multiplier
        pos = self.store.position(idea_id)
        idea_notional = price * ((pos.quantity if pos else 0) + qty) * mult
        if idea_notional > self.policy.max_notional_usd:
            # The gateway's per-idea notional, with the contract multiplier it cannot see.
            return None, [f"NOTIONAL_LIMIT:{idea_notional}>{self.policy.max_notional_usd}"]
        per_cap = self.policy.max_instrument_exposure_usd or self.policy.max_notional_usd
        new = limit * qty * mult
        try:
            symbol_exposure, total_exposure = self._exposure(contract, exclude_ref, now)
        except BrokerError as exc:
            self._broker_unreadable("positions", exc)
            return None, ["BROKER_POSITIONS_UNAVAILABLE"]
        if symbol_exposure + new > per_cap:
            return None, [f"INSTRUMENT_EXPOSURE_CAP:{symbol_exposure + new}>{per_cap}"]
        if total_exposure + new > self.policy.max_aggregate_exposure_usd:
            cap = self.policy.max_aggregate_exposure_usd
            return None, [f"AGGREGATE_EXPOSURE_CAP:{total_exposure + new}>{cap}"]
        return limit, []

    def _mark(
        self, contract: ContractRef | Instrument, fallback: Decimal | None, now: datetime
    ) -> Decimal:
        """The quote midpoint when a usable quote is available, otherwise the larger known cost."""
        if isinstance(contract, Instrument):
            contract, problem = self._resolve_contract(contract)
            if problem:
                return fallback or Decimal(0)
        q, problem = self._usable_quote(contract, now)
        if problem:
            return fallback or Decimal(0)
        return (q.bid + q.ask) / 2

    def _usable_quote(
        self, contract: ContractRef, now: datetime
    ) -> tuple[BrokerQuote | None, str | None]:
        """The broker's own quote, if it is fresh, of an accepted quality, positive and uncrossed."""
        try:
            q: BrokerQuote = self.broker.quote(contract)
        except BrokerError:
            return None, "BROKER_QUOTE_UNAVAILABLE"
        if q.quality == QuoteQuality.UNAVAILABLE:
            return None, "BROKER_QUOTE_UNAVAILABLE"
        if q.quality not in self.config.accepted_quote_qualities:
            return None, f"BROKER_QUOTE_QUALITY_{q.quality.value}"
        age = (now - q.received_at).total_seconds()
        if age > self.policy.max_quote_age_seconds or age < -5:
            return None, "BROKER_QUOTE_STALE"
        if q.bid is None or q.ask is None or q.bid <= 0 or q.ask <= 0:
            return None, "BROKER_QUOTE_NON_POSITIVE"
        if q.bid > q.ask:
            return None, "BROKER_QUOTE_CROSSED"
        return q, None

    def _exit_limit(
        self, contract: ContractRef, side: OrderSide, now: datetime
    ) -> tuple[Decimal | None, str | None]:
        """A marketable limit for an exit: the touch it trades against (bid for a sell, ask for a
        buy) moved by the policy collar, so it fills now but never far from a fresh reference."""
        q, problem = self._usable_quote(contract, now)
        if problem:
            return None, problem
        band = self.policy.max_price_deviation_pct / Decimal(100)
        tick = contract.min_tick
        if side == OrderSide.SELL:  # snapped up / down: always inside the collar
            return snap_to_tick(q.bid * (1 - band), tick, ROUND_CEILING), None
        return snap_to_tick(q.ask * (1 + band), tick, ROUND_FLOOR), None

    def _instrument_block(self, contract: ContractRef) -> str | None:
        """The first open exception, of a kind in ``INSTRUMENT_BLOCKING_KINDS``, on any idea in this
        contract: while it is open no new exposure is taken in that instrument (review R2, F2)."""
        key = contract.contract_key
        in_instrument = {
            p.idea_id
            for p in self.store.positions()
            if self._key_of(p.symbol, p.asset_class, p.contract_key) == key
        }
        open_ = [
            e for e in self.store.exceptions(open_only=True) if e.idea_id in in_instrument
        ]
        for kind in INSTRUMENT_BLOCKING_KINDS:
            if any(e.kind == kind for e in open_):
                return kind
        return None

    def _exposure(
        self, contract: ContractRef, exclude_ref: str | None = None, now: datetime | None = None
    ) -> tuple[Decimal, Decimal]:
        """Gross exposure per contract (keyed by ``contract_key``, never by symbol): the larger of
        the follower ledger's and the broker's position (a broker-only position counts), valued
        at quantity x the current quote x the contract multiplier when a usable quote is available
        and at cost otherwise (review S3); plus every non-terminal entry or add at its limit
        (``exclude_ref``: the intent being checked for its own resend). Returns the exposure in
        ``contract`` and the total. Raises BrokerError when the broker positions cannot be read."""
        now = now or self.clock()
        held: dict[str, int] = {}
        cost: dict[str, Decimal] = {}
        contracts: dict[str, ContractRef | Instrument] = {}
        mult: dict[str, Decimal] = {}
        for p in self.store.positions():
            if p.quantity > 0:
                key = self._key_of(p.symbol, p.asset_class, p.contract_key)
                held[key] = held.get(key, 0) + p.quantity
                if p.avg_price:
                    cost[key] = max(cost.get(key, Decimal(0)), Decimal(p.avg_price))
                stored = _contract_from(p.contract)
                contracts.setdefault(
                    key, stored or Instrument(asset_class=p.asset_class, symbol=p.symbol)
                )
                mult.setdefault(key, stored.multiplier if stored else Decimal(1))
        broker_held: dict[str, int] = {}
        for bp in self.broker.positions():
            key = bp.contract.contract_key
            broker_held[key] = broker_held.get(key, 0) + bp.quantity
            if bp.avg_cost:
                cost[key] = max(cost.get(key, Decimal(0)), bp.avg_cost)
            contracts[key] = bp.contract
            mult[key] = bp.contract.multiplier
        per: dict[str, Decimal] = {}
        for key in set(held) | set(broker_held):
            qty = max(held.get(key, 0), abs(broker_held.get(key, 0)))
            if qty == 0:
                continue
            per[key] = self._mark(contracts[key], cost.get(key), now) * qty * mult[key]
        # Every entry or add that is not terminal may be live at the broker, whatever our view of
        # its order rows (recorded, ack lost, held PreSubmitted): its full intended quantity counts,
        # less what has already been applied to the ledger (counted above). Review M2.
        for intent in self.store.intents(UNRESOLVED_STATES | {IntentState.WORKING}):
            if intent.action not in (Action.OPEN, Action.ADD) or intent.order_ref == exclude_ref:
                continue
            price = _dec(intent.limit_price)
            if price is None:
                continue
            left = intent.quantity - self.store.filled_quantity(intent.order_ref)
            if left > 0:
                stored = _contract_from(intent.contract)
                key = stored.contract_key if stored else self._key_of(
                    intent.symbol, self.store.position(intent.idea_id).asset_class, None
                )
                m = stored.multiplier if stored else Decimal(1)
                per[key] = per.get(key, Decimal(0)) + price * left * m
        return per.get(contract.contract_key, Decimal(0)), sum(per.values(), Decimal(0))

    def _resolve_contract(self, instrument: Instrument) -> tuple[ContractRef | None, str | None]:
        try:
            return self.broker.resolve(instrument), None
        except BrokerError:
            return None, "CONTRACT_UNRESOLVED"

    def _open_position(self, idea_id: str | None) -> LedgerPosition | None:
        pos = self.store.position(idea_id) if idea_id else None
        return pos if pos is not None and pos.status == "OPEN" and pos.quantity > 0 else None

    @staticmethod
    def _as_position(pos: LedgerPosition) -> Position:
        return Position(
            idea_id=pos.idea_id,
            symbol=pos.symbol,
            asset_class=pos.asset_class,
            side=pos.position_side,
            quantity=pos.quantity,
            stop_price=pos.stop_price,
            target_price=pos.target_price,
        )

    def _new_intent(
        self, source, action, gate, ref, account, idea_id, now, *, contract=None, **fields
    ) -> Intent:
        assert action.instrument is not None
        if contract is not None:
            fields.update(contract_key=contract.contract_key, contract=contract.model_dump_json())
        if gate.plan is not None:
            fields.setdefault("plan", gate.plan.model_dump_json())
        return Intent(
            order_ref=ref,
            source_id=source.source_id,
            revision=source.revision,
            action_id=action.action_id,
            action=action.action,
            idea_id=idea_id,
            symbol=action.instrument.symbol,
            gate_status=gate.status,
            gate_reasons=list(gate.reasons),
            account=account,
            state=IntentState.RECORDED,
            source_published_at=source.published_at,
            entry_reference=action.entry_reference,
            created_at=now,
            updated_at=now,
            **fields,
        )

    def _record(
        self,
        intent: Intent,
        orders: list[OrderIntent],
        ledger: LedgerPosition | None = None,
        *,
        role: Role = Role.ENTRY,
    ) -> None:
        """Write-ahead: the intent, every order it will place and (for an OPEN) the pending ledger
        row
        are committed in one transaction before the broker is called."""
        with self.store.tx() as c:
            self.store.insert_intent(c, intent)
            if ledger is not None:
                self.store.insert_position(c, ledger)
            for o in orders:
                if o.parent_ref is None:
                    r = role
                elif o.order_ref == backstop_ref(o.parent_ref):
                    r = Role.BACKSTOP
                elif o.condition is not None:
                    r = Role.STOP if o.order_ref == stop_ref(o.parent_ref) else Role.TARGET
                else:
                    r = Role.STOP if o.order_type == OrderType.STP else Role.TARGET
                self.store.insert_order(
                    c,
                    OrderRow(
                        order_ref=o.order_ref,
                        intent_ref=intent.order_ref,
                        idea_id=intent.idea_id,
                        role=r,
                        parent_ref=o.parent_ref,
                        symbol=o.contract.symbol,
                        contract_key=o.contract.contract_key,
                        side=o.side.value,
                        quantity=o.quantity,
                        order_type=o.order_type.value,
                        limit_price=str(o.limit_price) if o.limit_price else None,
                        stop_price=(
                            str(o.stop_price) if o.stop_price
                            else str(o.condition.trigger_price) if o.condition else None
                        ),
                        status="Recorded",
                        updated_at=intent.created_at,
                        condition=o.condition.model_dump_json() if o.condition else None,
                    ),
                )
            self.store.log(
                intent.created_at,
                "INTENT_RECORDED",
                order_ref=intent.order_ref,
                idea_id=intent.idea_id,
                c=c,
                action=intent.action.value,
                contract=intent.contract_key,
                side=intent.side,
                quantity=intent.quantity,
                limit=intent.limit_price,
                gate=intent.gate_reasons,
                orders=[o.order_ref for o in orders],
            )

    def _refuse(
        self, source, action, gate, ref, account, reasons, now, *, idea_id=None
    ) -> ExecutionOutcome:
        idea = idea_id or action.idea_reference or f"{source.source_id}:{action.action_id}"
        intent = self._new_intent(source, action, gate, ref, account, idea, now, quantity=0)
        intent = intent.model_copy(update={"state": IntentState.REFUSED, "reasons": list(reasons)})
        with self.store.tx() as c:
            self.store.insert_intent(c, intent)
            self.store.log(
                now,
                "REFUSED",
                order_ref=ref,
                idea_id=idea,
                c=c,
                reasons=reasons,
                detail="execution precondition failed; nothing submitted",
            )
        return self._outcome(ref)

    def _refresh_orders(
        self, open_orders: list[BrokerOrder], now: datetime, completed: list[BrokerOrder] = ()
    ) -> None:
        """Bring our order rows and working intents up to the broker's view. An order that left the
        working set takes its final state from its own completed-orders row (or, for a full fill,
        its own executions); with neither it is UNKNOWN, never assumed cancelled or filled."""
        open_by_ref = {o.order_ref: o for o in open_orders}
        done_by_ref = {o.order_ref: o for o in completed}
        live = {"Recorded", "Unknown"} | {s.value for s in WORKING_STATUSES}
        intents = {i.order_ref: i for i in self.store.intents()}
        for row in self.store.orders():
            intent = intents.get(row.intent_ref)
            if intent is None or intent.state in UNRESOLVED_STATES:
                continue  # unknown outcome: only _resolve may decide
            o = open_by_ref.get(row.order_ref)
            if o is not None:
                if o.status.value != row.status or o.quantity != row.quantity:
                    self.store.update_order(
                        row.order_ref, now, status=o.status.value, quantity=o.quantity
                    )
            elif row.status in live:
                done = done_by_ref.get(row.order_ref)
                if done is not None:
                    final = done.status.value
                elif row.filled_quantity >= row.quantity > 0:
                    final = OrderStatus.FILLED.value
                else:
                    final = "Unknown"
                if final != row.status:
                    self.store.update_order(row.order_ref, now, status=final)
        for intent in self.store.intents(frozenset({IntentState.WORKING})):
            entry = self.store.order(intent.order_ref)
            filled = self.store.filled_quantity(intent.order_ref)
            o = open_by_ref.get(intent.order_ref) or done_by_ref.get(intent.order_ref)
            if o is not None:
                state = _intent_state(o.status, max(filled, o.filled_quantity), intent.quantity)
            elif filled >= intent.quantity:
                state = IntentState.FILLED
            else:
                self._entry_state_unknown(
                    intent,
                    f"left the working orders with {filled} of {intent.quantity} filled and no "
                    "completed-orders row",
                )
                continue
            if state != intent.state:
                self.store.set_state(
                    intent.order_ref,
                    state,
                    now,
                    broker_status=o.status.value if o else entry.status,
                )
                self.store.log(
                    now,
                    f"INTENT_{state.value}",
                    order_ref=intent.order_ref,
                    idea_id=intent.idea_id,
                    filled=filled,
                )
            if (
                state in (IntentState.CANCELLED, IntentState.REJECTED)
                and intent.action == Action.OPEN
            ):
                pos = self.store.position(intent.idea_id)
                if pos is not None and pos.status == "PENDING" and pos.quantity == 0:
                    self.store.update_position(intent.idea_id, now, status="NOT_FILLED")

    def _try(self, call, event: str, idea_id: str, order_ref: str, **detail) -> bool:
        try:
            call()
        except OrderNotFound as exc:
            self.store.log(
                self.clock(),
                f"{event}_SKIPPED",
                order_ref=order_ref,
                idea_id=idea_id,
                error=str(exc),
            )
            return False
        except BrokerError as exc:
            self.store.log(
                self.clock(),
                f"{event}_FAILED",
                order_ref=order_ref,
                idea_id=idea_id,
                error=str(exc),
            )
            return False
        self.store.log(self.clock(), event, order_ref=order_ref, idea_id=idea_id, **detail)
        return True

    def _outcome(self, ref: str, *, duplicate: bool = False) -> ExecutionOutcome:
        intent = self.store.intent(ref)
        assert intent is not None
        fills = self.store.fills(ref)
        qty = sum(f["quantity"] for f in fills)
        avg = None
        if qty:
            avg = str(
                (sum(Decimal(f["price"]) * f["quantity"] for f in fills) / qty).quantize(
                    Decimal("0.0001")
                )
            )
        return ExecutionOutcome(
            order_ref=ref,
            source_id=intent.source_id,
            action_id=intent.action_id,
            action=intent.action,
            idea_id=intent.idea_id,
            state=intent.state,
            filled_quantity=qty,
            avg_fill_price=avg,
            protection=self._protection(intent.idea_id),
            duplicate=duplicate,
            gate_reasons=list(intent.gate_reasons),
            reasons=list(intent.reasons),
        )
