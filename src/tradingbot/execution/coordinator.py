"""ExecutionCoordinator: turns a gateway-approved action into broker orders and the follower ledger.

It never authorizes. ``execute`` accepts only an action whose ``GateDecision`` from
``validator.gate_action`` is APPROVE, and sizes it with ``validator.fill_quantity``. What it adds
is what only execution can know: a deterministic idempotency key, a write-ahead intent, broker-side
quote sanity and exposure, the halt switch, protective-order confirmation and recovery after a lost
acknowledgement or a restart. See docs/EXECUTION_CORE.md for the intent state machine.

The never-resend rule: an intent whose submission outcome is unknown (RECORDED after a restart, or
UNRESOLVED after a lost acknowledgement) is looked up at the broker by ``order_ref`` in
``open_orders`` and ``executions``. Whatever is found is adopted. Only a positive "no trace" leads
to a resubmission, and that reasoning is journaled.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, InvalidOperation
from pathlib import Path

from pydantic import Field

from tradingbot.event_models import ReplaySource, TradeAction
from tradingbot.execution.broker import (
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
from tradingbot.validator import fill_quantity

CENT = Decimal("0.01")
UNPROTECTED_POSITION = "UNPROTECTED_POSITION"
MODIFY_NOT_CONFIRMED = "MODIFY_NOT_CONFIRMED"


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


class ExecutionOutcome(StrictModel):
    order_ref: str
    source_id: str
    action_id: str
    action: Action
    idea_id: str
    state: IntentState
    filled_quantity: int = 0
    avg_fill_price: str | None = None
    protection: str = "NOT_REQUIRED"  # CONFIRMED | UNPROTECTED | PENDING | NOT_REQUIRED
    duplicate: bool = False
    gate_reasons: list[str] = Field(
        default_factory=list
    )  # the gateway's APPROVE reasons, as recorded
    reasons: list[str] = Field(default_factory=list)  # execution reasons (refusal, reject, halt)

    @property
    def replay_status(self) -> str:
        """The outcome in the replay journal's vocabulary (event_models.ActionOutcome.status)."""
        if self.protection == "UNPROTECTED":
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
    if status == OrderStatus.CANCELLED:
        return IntentState.CANCELLED
    if status == OrderStatus.UNKNOWN:
        return IntentState.UNRESOLVED
    return IntentState.WORKING


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
        self.store = ExecutionStore(store_path)
        self.policy = policy or MockPolicy()
        self.config = config or ExecutionConfig()
        self.clock = clock or (lambda: datetime.now(UTC))

    # ================================================================== public API

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
        positions = {p.idea_id: p for p in self.store.positions()}
        for idea_id in sorted(
            touched | {i for i, p in positions.items() if p.status in ("OPEN", "PENDING")}
        ):
            self._maintain_protection(idea_id)
        return True

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
        """REDUCE / CLOSE: reduce-only market orders that can never open or reverse a position."""
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
        order = OrderIntent(
            order_ref=ref,
            contract=contract,
            side=side,
            quantity=qty,
            order_type=OrderType.MKT,
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
            order_type="MKT",
        )
        self._record(intent, [order], role=Role.EXIT)
        self._submit(intent, order, [])
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
        self.store.set_state(intent.order_ref, intent.state, self.clock(), attempts_delta=1)
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
            self._mark_unresolved(intent, "ACK_LOST", str(exc))
            self._resolve(self.store.intent(intent.order_ref))
            return
        except BrokerError as exc:
            self._mark_unresolved(intent, "SUBMIT_ERROR", str(exc))
            self._resolve(self.store.intent(intent.order_ref))
            return
        self.store.log(
            self.clock(),
            "ACK",
            order_ref=intent.order_ref,
            idea_id=intent.idea_id,
            orders={a.order_ref: a.status.value for a in acks},
        )
        self._adopt(intent, acks, [])

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
        except BrokerError as exc:
            self._mark_unresolved(intent, "LOOKUP_FAILED", str(exc))
            return
        rows = self.store.orders(intent.order_ref)
        refs = {r.order_ref for r in rows}
        found_open = [o for o in open_orders if o.order_ref in refs]
        found_fills = [f for f in fills if f.order_ref in refs]
        if found_open or found_fills:
            self.store.log(
                self.clock(),
                "ADOPTED_FROM_BROKER",
                order_ref=intent.order_ref,
                idea_id=intent.idea_id,
                open_orders=[o.order_ref for o in found_open],
                executions=[f.exec_id for f in found_fills],
                rule="order found by order_ref; adopted, not resent",
            )
            self._adopt(intent, found_open, found_fills)
            return
        if intent.attempts >= self.config.max_submit_attempts:
            self._mark_unresolved(
                intent,
                "NO_TRACE_RESEND_BUDGET_EXHAUSTED",
                f"{intent.attempts} submissions already made",
            )
            return
        self.store.log(
            self.clock(),
            "NO_TRACE_AT_BROKER",
            order_ref=intent.order_ref,
            idea_id=intent.idea_id,
            checked=sorted(refs),
            attempts=intent.attempts,
            rule="open_orders and executions show no order or fill for this ref; resubmitting",
        )
        entry, children = self._rebuild(intent, rows)
        self._submit(intent, entry, children)

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
        """Size the idea's working stop/target to the ledger quantity (cancel when flat), then
        confirm
        at the broker that a working stop covers exactly that quantity. Raises UNPROTECTED_POSITION
        otherwise. Never reports protection it has not seen."""
        pos = self.store.position(idea_id)
        if pos is None:
            return
        now = self.clock()
        try:
            open_by_ref = {o.order_ref: o for o in self.broker.open_orders()}
        except BrokerError as exc:
            self.store.log(now, "PROTECTION_CHECK_FAILED", idea_id=idea_id, error=str(exc))
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
            return
        changed = False
        for ref in refs:
            o = open_by_ref.get(ref)
            if o is None:
                continue
            if o.remaining != pos.quantity:
                if self.halted():
                    self.store.log(
                        now,
                        "PROTECTIVE_RESIZE_HALTED",
                        order_ref=ref,
                        idea_id=idea_id,
                        working=o.remaining,
                        ledger=pos.quantity,
                    )
                    continue
                if self._try(
                    lambda o=o: self.broker.modify(
                        o.order_ref, quantity=o.filled_quantity + pos.quantity
                    ),
                    "PROTECTIVE_RESIZED",
                    idea_id,
                    ref,
                    before=o.remaining,
                    after=pos.quantity,
                ):
                    changed = True
        if changed:
            try:
                open_by_ref = {o.order_ref: o for o in self.broker.open_orders()}
            except BrokerError as exc:
                self.store.log(now, "PROTECTION_CHECK_FAILED", idea_id=idea_id, error=str(exc))
                return
        for ref in refs:
            o = open_by_ref.get(ref)
            if o is not None:
                self.store.update_order(ref, now, status=o.status.value, quantity=o.quantity)
        if pos.stop_price is None:
            return
        stop = open_by_ref.get(pos.stop_ref) if pos.stop_ref else None
        if (
            stop is not None
            and stop.working
            and stop.remaining == pos.quantity
            and stop.order_type == OrderType.STP
        ):
            if self.store.resolve_exceptions(now, UNPROTECTED_POSITION, idea_id=idea_id):
                self.store.log(now, "PROTECTION_RESTORED", order_ref=pos.stop_ref, idea_id=idea_id)
            return
        detail = (
            f"ledger holds {pos.quantity}; working stop covers "
            f"{stop.remaining if stop else 0} ({stop.status.value if stop else 'absent'})"
        )
        if self.store.raise_exception(
            now, UNPROTECTED_POSITION, detail, idea_id=idea_id, order_ref=pos.stop_ref
        ):
            self.store.log(
                now, UNPROTECTED_POSITION, order_ref=pos.stop_ref, idea_id=idea_id, detail=detail
            )

    def _protection(self, idea_id: str) -> str:
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
        return "CONFIRMED"

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
            now, "AMENDMENT_CONFIRMED", order_ref=intent.order_ref, idea_id=intent.idea_id, **fields
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
    ) -> tuple[Decimal | None, list[str]]:
        """Broker-quote sanity, the price collar and exposure caps, before any entry or add.
        Returns the collar-bounded limit price, or the reasons to refuse."""
        try:
            q: BrokerQuote = self.broker.quote(contract)
        except BrokerError:
            return None, ["BROKER_QUOTE_UNAVAILABLE"]
        if q.quality == QuoteQuality.UNAVAILABLE:
            return None, ["BROKER_QUOTE_UNAVAILABLE"]
        if q.quality not in self.config.accepted_quote_qualities:
            return None, [f"BROKER_QUOTE_QUALITY_{q.quality.value}"]
        age = (now - q.received_at).total_seconds()
        if age > self.policy.max_quote_age_seconds or age < -5:
            return None, ["BROKER_QUOTE_STALE"]
        if q.bid is None or q.ask is None or q.bid <= 0 or q.ask <= 0:
            return None, ["BROKER_QUOTE_NON_POSITIVE"]
        if q.bid > q.ask:
            return None, ["BROKER_QUOTE_CROSSED"]
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
        symbol_exposure, total_exposure = self._exposure(contract.symbol)
        if symbol_exposure + new > per_cap:
            return None, [f"INSTRUMENT_EXPOSURE_CAP:{symbol_exposure + new}>{per_cap}"]
        if total_exposure + new > self.policy.max_aggregate_exposure_usd:
            cap = self.policy.max_aggregate_exposure_usd
            return None, [f"AGGREGATE_EXPOSURE_CAP:{total_exposure + new}>{cap}"]
        return limit, []

    def _exposure(self, symbol: str) -> tuple[Decimal, Decimal]:
        """Gross exposure at cost of every open follower idea plus unfilled working entries."""
        per: dict[str, Decimal] = {}
        for p in self.store.positions():
            if p.quantity > 0 and p.avg_price:
                per[p.symbol] = per.get(p.symbol, Decimal(0)) + Decimal(p.avg_price) * p.quantity
        for row in self.store.orders():
            if (
                row.role == Role.ENTRY
                and row.status
                in {
                    s.value
                    for s in (
                        OrderStatus.PENDING_SUBMIT,
                        OrderStatus.SUBMITTED,
                        OrderStatus.PARTIALLY_FILLED,
                    )
                }
                and row.limit_price
            ):
                left = row.quantity - row.filled_quantity
                per[row.symbol] = per.get(row.symbol, Decimal(0)) + Decimal(row.limit_price) * left
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
        live = {"Recorded"} | {
            s.value
            for s in (
                OrderStatus.PENDING_SUBMIT,
                OrderStatus.PRE_SUBMITTED,
                OrderStatus.SUBMITTED,
                OrderStatus.PARTIALLY_FILLED,
            )
        }
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
