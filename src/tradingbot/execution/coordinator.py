"""ExecutionCoordinator: turns a gateway-approved action into broker orders and the follower ledger.

It never authorizes. ``execute`` accepts only an action whose ``GateDecision`` from
``validator.gate_action`` is APPROVE, and sizes it with ``validator.fill_quantity``. What it adds
is what only execution can know: a deterministic idempotency key, a write-ahead intent, broker-side
quote sanity and exposure, the halt switch, protective-order confirmation and recovery after a lost
acknowledgement or a restart. See docs/EXECUTION_CORE.md for the intent state machine.

The never-resend rule: an intent whose submission outcome is unknown (RECORDED after a restart, or
UNRESOLVED after a lost acknowledgement) is looked up at the broker by ``order_ref`` in
``open_orders``, ``executions`` and ``completed_orders``. Whatever is found is adopted, including a
terminal REJECTED or CANCELLED state. Only a positive "no trace" in all three leads to a
resubmission, and an entry or add is resubmitted only while its source post is still fresh;
otherwise it ends REFUSED with a STALE_AFTER_UNKNOWN_OUTCOME exception. The reasoning is journaled.
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
    ContractRef,
    OrderIntent,
    OrderNotFound,
    OrderSide,
    OrderStatus,
    OrderType,
    QuoteQuality,
    TimeInForce,
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
    GateDecision,
    Instrument,
    MockPolicy,
    Position,
    PositionSide,
    StrictModel,
)
from tradingbot.validator import fill_quantity, source_is_fresh

CENT = Decimal("0.01")
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
# An exit was not sent because the working entries, earlier exits or protective children could not
# be cancelled or resized first, with the result confirmed at the broker (review R1 / R6).
EXIT_NOT_NETTED = "EXIT_NOT_NETTED"
# An exit was not sent because the broker quote was not usable to price its limit (review S2); the
# broker-side protective stop remains the protection.
EXIT_WITHOUT_QUOTE = "EXIT_WITHOUT_QUOTE"


def default_halt_file() -> Path:
    override = os.environ.get("TRADINGBOT_HALT_FILE")
    return Path(override) if override else Path.home() / ".local" / "state" / "tradingbot" / "HALT"


class ExecutionConfig(StrictModel):
    # Emergency halt: while this file exists nothing is submitted, amended or resized.
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
    # An entry or add older than this (since its intent was recorded) is never resent.
    max_intent_age_seconds: int = Field(default=120, gt=0)
    # How far back the broker's executions / completed-orders queries reach (IBKR: the current
    # day). An unknown outcome older than this can never be proven absent: it is never resent.
    lookup_horizon_seconds: int = Field(default=6 * 3600, gt=0)
    # Cancel / resize confirmation before an exit: this many broker reads, this far apart.
    confirm_attempts: int = Field(default=3, ge=1, le=20)
    confirm_wait_seconds: float = Field(default=0.0, ge=0, le=5)


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


def _dec(value: str | None) -> Decimal | None:
    if value is None:
        return None
    try:
        d = Decimal(value)
    except (InvalidOperation, ValueError, TypeError):
        return None
    return d if d.is_finite() and d > 0 else None


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
        self.sync()
        account = self.broker.account_id()
        if self.config.expected_account and account != self.config.expected_account:
            return self._refuse(
                source, action, gate, ref, account, ["BROKER_ACCOUNT_MISMATCH"], now
            )
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
            self.store.log(
                self.clock(),
                "RECOVERY_LOOKUP",
                order_ref=intent.order_ref,
                idea_id=intent.idea_id,
                state=intent.state.value,
            )
            self._resolve(intent)
            out.append(self._outcome(intent.order_ref))
        self.sync()
        return out

    def sync(self) -> bool:
        """Apply new broker fills to the ledger, refresh working orders, keep protection sized.
        Returns False when the broker could not be read (nothing is changed then)."""
        now = self.clock()
        try:
            fills = self.broker.executions()
            open_orders = self.broker.open_orders()
        except BrokerError as exc:
            self.store.log(now, "SYNC_FAILED", error=str(exc))
            return False
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
        self._refresh_orders(open_orders, now)
        if self.halted() and self._cancel_entries_for_halt(open_orders):
            # Risk-reducing: re-read so a fill that beat the cancel is applied and covered at once.
            return self.sync()
        positions = {p.idea_id: p for p in self.store.positions()}
        for idea_id in sorted(
            touched | {i for i, p in positions.items() if p.status in ("OPEN", "PENDING")}
        ):
            self._maintain_protection(idea_id)
        return True

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

    # ================================================================== actions

    def _open(self, source, action, gate, ref, account, now) -> ExecutionOutcome:
        assert action.instrument is not None and action.position_side is not None
        idea_id = f"{source.source_id}:{action.action_id}"
        if self.store.position(idea_id) is not None:
            return self._refuse(source, action, gate, ref, account, ["IDEA_ALREADY_EXISTS"], now)
        contract, problem = self._resolve_contract(action.instrument)
        if problem:
            return self._refuse(source, action, gate, ref, account, [problem], now)
        long = action.position_side == PositionSide.LONG
        side = OrderSide.BUY if long else OrderSide.SELL
        qty = fill_quantity(Action.OPEN, self.policy, None)
        limit, reasons = self._entry_checks(
            contract, side, qty, action.entry_reference, idea_id, now
        )
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
        stop, target = _dec(action.stop_price), _dec(action.target_price)
        if stop is not None:
            children.append(
                OrderIntent(
                    order_ref=stop_ref(ref),
                    contract=contract,
                    side=exit_side,
                    quantity=qty,
                    order_type=OrderType.STP,
                    stop_price=stop,
                    tif=TimeInForce.GTC,
                    parent_ref=ref,
                    reduce_only=True,
                )
            )
        if target is not None:
            children.append(
                OrderIntent(
                    order_ref=target_ref(ref),
                    contract=contract,
                    side=exit_side,
                    quantity=qty,
                    order_type=OrderType.LMT,
                    limit_price=target,
                    tif=TimeInForce.GTC,
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
            side=side.value,
            quantity=qty,
            order_type="LMT",
            limit_price=str(limit),
        )
        ledger = LedgerPosition(
            idea_id=idea_id,
            symbol=contract.symbol,
            asset_class=contract.asset_class,
            position_side=action.position_side,
            quantity=0,
            stop_price=str(stop) if stop is not None else None,
            target_price=str(target) if target is not None else None,
            stop_ref=stop_ref(ref) if stop is not None else None,
            target_ref=target_ref(ref) if target is not None else None,
            status="PENDING",
            updated_at=now,
        )
        self._record(intent, [entry, *children], ledger)
        self._submit(intent, entry, children)
        return self._outcome(ref)

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
        long = pos.position_side == PositionSide.LONG
        side = OrderSide.BUY if long else OrderSide.SELL
        qty = fill_quantity(Action.ADD, self.policy, self._as_position(pos))
        limit, reasons = self._entry_checks(
            contract, side, qty, action.entry_reference, pos.idea_id, now
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
        try:
            held = sum(
                p.quantity for p in self.broker.positions() if p.contract.symbol == contract.symbol
            )
        except BrokerError:
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
        intent = self._new_intent(
            source,
            action,
            gate,
            ref,
            account,
            pos.idea_id,
            now,
            quantity=0,
            stop_price=action.stop_price,
            target_price=action.target_price,
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
                stop=action.stop_price,
                target=action.target_price,
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
        for ref in (pos.stop_ref, pos.target_ref):
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
        try:
            held = sum(
                p.quantity for p in self.broker.positions() if p.contract.symbol == intent.symbol
            )
            working = [
                o for o in self.broker.open_orders()
                if o.contract.symbol == intent.symbol and o.side.value == intent.side
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
            and i.symbol == intent.symbol
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
            if since < self.config.settle_seconds:
                # Not visible yet proves nothing: the order may still be settling at the broker.
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
        contract, problem = self._resolve_contract(
            Instrument(asset_class=pos.asset_class, symbol=intent.symbol)
        )
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
            # Not working at the broker but executions exist: the entry completed.
            filled = sum(f.quantity for f in fills if f.order_ref == intent.order_ref)
            state = IntentState.FILLED if filled >= intent.quantity else IntentState.CANCELLED
            broker_status, reasons = None, None
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

    def _rebuild(
        self, intent: Intent, rows: list[OrderRow]
    ) -> tuple[OrderIntent, list[OrderIntent]]:
        """Recreate exactly the orders recorded write-ahead (same refs, same prices, same sizes)."""
        assert intent.side is not None
        inst = Instrument(
            asset_class=self.store.position(intent.idea_id).asset_class, symbol=intent.symbol
        )
        contract = self.broker.resolve(inst)
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
                    tif=TimeInForce.GTC if r.role in (Role.STOP, Role.TARGET) else TimeInForce.DAY,
                    parent_ref=r.parent_ref,
                    reduce_only=r.role != Role.ENTRY,
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
            return
        refs = [r for r in (pos.stop_ref, pos.target_ref) if r]
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
        stop = open_by_ref.get(pos.stop_ref) if pos.stop_ref else None
        if (
            stop is not None
            and stop.working
            and stop.status != OrderStatus.PENDING_CANCEL
            and stop.remaining == cover
            and stop.order_type == OrderType.STP
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
        if self.store.raise_exception(
            now, UNPROTECTED_POSITION, detail, idea_id=idea_id, order_ref=pos.stop_ref
        ):
            self.store.log(
                now, UNPROTECTED_POSITION, order_ref=pos.stop_ref, idea_id=idea_id, detail=detail
            )

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
        if any(
            e.kind == UNPROTECTED_POSITION and e.idea_id == idea_id
            for e in self.store.exceptions(open_only=True)
        ):
            return "UNPROTECTED"
        if pos.quantity == 0:
            return "PENDING" if pos.status == "PENDING" else "NOT_REQUIRED"
        if pos.protection in ("CONFIRMED", "EXITING"):
            return pos.protection
        return "UNKNOWN"

    def _apply_modify(self, intent: Intent, *, recovering: bool = False) -> None:
        """Amend the working stop/target to absolute prices, then confirm them at the broker.
        An absolute-price amendment is idempotent: re-issuing it after a lost ack cannot double up.
        """
        pos = self.store.position(intent.idea_id)
        now = self.clock()
        wanted = [
            (pos.stop_ref, "stop_price", _dec(intent.stop_price)),
            (pos.target_ref, "limit_price", _dec(intent.target_price)),
        ]
        wanted = [(ref, field, price) for ref, field, price in wanted if price is not None and ref]
        try:
            open_by_ref = {o.order_ref: o for o in self.broker.open_orders()}
        except BrokerError as exc:
            self._mark_unresolved(intent, "LOOKUP_FAILED", str(exc))
            return
        pending = [
            (r, f, p) for r, f, p in wanted if r in open_by_ref and getattr(open_by_ref[r], f) != p
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
            r for r, f, p in wanted if r not in open_by_ref or getattr(open_by_ref[r], f) != p
        ]
        if unconfirmed:
            self._modify_failed(intent, f"amendment not visible at broker: {unconfirmed}")
            return
        fields = {}
        for ref, field, price in wanted:
            self.store.update_order(
                ref,
                now,
                status=open_by_ref[ref].status.value,
                **{"stop_price" if field == "stop_price" else "limit_price": str(price)},
            )
            fields["stop_price" if field == "stop_price" else "target_price"] = str(price)
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
        if self._reversal_blocks(contract.symbol):
            return None, ["INSTRUMENT_BLOCKED_LEDGER_REVERSAL"]
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
        limit = (
            (ref * (1 + band)).quantize(CENT, ROUND_FLOOR)
            if buy
            else (ref * (1 - band)).quantize(CENT, ROUND_CEILING)
        )
        per_cap = self.policy.max_instrument_exposure_usd or self.policy.max_notional_usd
        new = limit * qty
        try:
            symbol_exposure, total_exposure = self._exposure(contract.symbol, exclude_ref, now)
        except BrokerError:
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
        if side == OrderSide.SELL:
            return (q.bid * (1 - band)).quantize(CENT, ROUND_CEILING), None
        return (q.ask * (1 + band)).quantize(CENT, ROUND_FLOOR), None

    def _reversal_blocks(self, symbol: str) -> bool:
        """An open LEDGER_REVERSAL on any idea in this instrument blocks all new exposure in it."""
        ideas = {
            e.idea_id
            for e in self.store.exceptions(open_only=True)
            if e.kind == LEDGER_REVERSAL
        }
        return any(p.symbol == symbol for p in self.store.positions() if p.idea_id in ideas)

    def _exposure(
        self, symbol: str, exclude_ref: str | None = None, now: datetime | None = None
    ) -> tuple[Decimal, Decimal]:
        """Gross exposure per instrument: the larger of the follower ledger's and the broker's
        position (a broker-only position counts), valued at the current quote when a usable one is
        available and at cost otherwise (review S3); plus every non-terminal entry or add at its
        limit (``exclude_ref``: the intent being checked for its own resend). Raises BrokerError
        when the broker positions cannot be read."""
        now = now or self.clock()
        held: dict[str, int] = {}
        cost: dict[str, Decimal] = {}
        contracts: dict[str, ContractRef | Instrument] = {}
        for p in self.store.positions():
            if p.quantity > 0:
                held[p.symbol] = held.get(p.symbol, 0) + p.quantity
                if p.avg_price:
                    cost[p.symbol] = max(cost.get(p.symbol, Decimal(0)), Decimal(p.avg_price))
                contracts.setdefault(
                    p.symbol, Instrument(asset_class=p.asset_class, symbol=p.symbol)
                )
        broker_held: dict[str, int] = {}
        for bp in self.broker.positions():
            sym = bp.contract.symbol
            broker_held[sym] = broker_held.get(sym, 0) + bp.quantity
            if bp.avg_cost:
                cost[sym] = max(cost.get(sym, Decimal(0)), bp.avg_cost)
            contracts[sym] = bp.contract
        per: dict[str, Decimal] = {}
        for sym in set(held) | set(broker_held):
            qty = max(held.get(sym, 0), abs(broker_held.get(sym, 0)))
            if qty == 0:
                continue
            per[sym] = self._mark(contracts[sym], cost.get(sym), now) * qty
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
                per[intent.symbol] = per.get(intent.symbol, Decimal(0)) + price * left
        return per.get(symbol, Decimal(0)), sum(per.values(), Decimal(0))

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

    def _new_intent(self, source, action, gate, ref, account, idea_id, now, **fields) -> Intent:
        assert action.instrument is not None
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
                r = (
                    role
                    if o.parent_ref is None
                    else (Role.STOP if o.order_type == OrderType.STP else Role.TARGET)
                )
                self.store.insert_order(
                    c,
                    OrderRow(
                        order_ref=o.order_ref,
                        intent_ref=intent.order_ref,
                        idea_id=intent.idea_id,
                        role=r,
                        parent_ref=o.parent_ref,
                        symbol=o.contract.symbol,
                        side=o.side.value,
                        quantity=o.quantity,
                        order_type=o.order_type.value,
                        limit_price=str(o.limit_price) if o.limit_price else None,
                        stop_price=str(o.stop_price) if o.stop_price else None,
                        status="Recorded",
                        updated_at=intent.created_at,
                    ),
                )
            self.store.log(
                intent.created_at,
                "INTENT_RECORDED",
                order_ref=intent.order_ref,
                idea_id=intent.idea_id,
                c=c,
                action=intent.action.value,
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

    def _refresh_orders(self, open_orders: list[BrokerOrder], now: datetime) -> None:
        """Bring our order rows and working intents up to the broker's view."""
        open_by_ref = {o.order_ref: o for o in open_orders}
        live = {"Recorded"} | {s.value for s in WORKING_STATUSES}
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
                final = (
                    OrderStatus.FILLED
                    if row.filled_quantity >= row.quantity > 0
                    else OrderStatus.CANCELLED
                )
                self.store.update_order(row.order_ref, now, status=final.value)
        for intent in self.store.intents(frozenset({IntentState.WORKING})):
            entry = self.store.order(intent.order_ref)
            filled = self.store.filled_quantity(intent.order_ref)
            o = open_by_ref.get(intent.order_ref)
            if o is not None:
                state = _intent_state(o.status, filled, intent.quantity)
            elif filled >= intent.quantity:
                state = IntentState.FILLED
            else:
                state = IntentState.CANCELLED
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
