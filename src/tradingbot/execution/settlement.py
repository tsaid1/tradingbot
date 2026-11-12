"""Operator settlement: a human records what they established at the broker. Nothing here sends,
amends or cancels an order; every function only reads the broker and writes the store. A
settlement reads with ``sync(record_only=True)`` and leaves protective upkeep (sizing or cancelling
protective orders, cancels under the halt) to the next normal cycle. ``lift_verified_blocks`` and
``clear_exception`` run inside the normal cycle and use its full sync, which may resize or cancel
protective orders and cancel working entries under the halt (risk-reducing only).

* ``settle_intent``: an intent of unknown outcome (RECORDED / UNRESOLVED; an entry or add is never
  resent, so past the lookup horizon only a human can end it) is settled as exactly one of
  NOT_AT_BROKER, FILLED (quantity and average price) or CANCELLED. The ledger moves exactly as it
  would from a broker execution (``ExecutionStore.apply_fill``, under a unique ``OPERATOR:<id>``
  execution id), so a declared fill creates the position and its protection must then be confirmed
  at the broker like any other fill. A settlement that contradicts what the broker shows now is
  refused (the order or a fill is visible, a limit was violated, the resulting follower position
  cannot match the broker's).
* ``clear_exception``: a CLEAR-class record (LEDGER_REVERSAL, SETTLED_INTENT_CONTRADICTED,
  PARTIAL_GROUP) closes only when a full sync and reconciliation made now show the contract clean
  (the record's own reconciliation line excepted).
* ``acknowledge_exception``: an informational record that blocks no exposure.
* ``lift_verified_blocks``: a settled intent leaves OPERATOR_SETTLEMENT_UNVERIFIED open, which
  blocks new exposure in its contract until a full successful sync and reconciliation show no
  blocking discrepancy there. Only that evidence resolves it, and never in the call that records
  the settlement. A NOT_AT_BROKER or CANCELLED settlement needs, AFTER it, the evidence rule the
  core uses before resending an exit: the settle period elapsed and at least
  ``exit_no_trace_lookups`` consecutive no-trace lookups (open orders, executions, completed
  orders) spread over that period, then a clean reconciliation of the contract (review F1). Broker
  evidence for such an intent later is a contradiction (``SETTLED_INTENT_CONTRADICTED``, see
  ``ExecutionCoordinator._settlement_contradictions``), never a normal adoption.

Every action, applied or refused, is journaled append-only in ``exec_journal`` with its time,
request id and the state before and after, and the operator's note itself (the operator's own
words, never publisher content) with its SHA-256 (review note d), so the reasoning survives the
loss of the operator queue.
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal, InvalidOperation

from pydantic import Field

from tradingbot.execution import operator_catalog as cat
from tradingbot.execution.broker import BrokerError, OrderSide, OrderStatus
from tradingbot.execution.coordinator import (
    COMPLETED_LOOKBACK,
    ENTRY_STATE_UNKNOWN,
    OPERATOR_SETTLEMENT_UNVERIFIED,
    SETTLED_WITHOUT_FILL_REASONS,
    UNKNOWN_OUTCOME_BEYOND_LOOKUP_HORIZON,
    UNKNOWN_OUTCOME_ENTRY_NOT_RESENT,
    ExecutionCoordinator,
    _contract_from,
)
from tradingbot.execution.reconcile import DiscrepancyKind
from tradingbot.execution.store import UNRESOLVED_STATES, Intent, IntentState, Role
from tradingbot.models import Action, PositionSide, StrictModel

# The intent-outcome exceptions a settlement ends (they describe the outcome now declared).
SETTLED_KINDS = (
    UNKNOWN_OUTCOME_ENTRY_NOT_RESENT,
    UNKNOWN_OUTCOME_BEYOND_LOOKUP_HORIZON,
    ENTRY_STATE_UNKNOWN,
)
MIN_NOTE_CHARS = 8


class OperatorResult(StrictModel):
    status: str  # APPLIED | REFUSED
    reasons: list[str] = Field(default_factory=list)
    before: dict = Field(default_factory=dict)
    after: dict = Field(default_factory=dict)
    lifted: list[int] = Field(default_factory=list)  # exception ids resolved by verification


def note_digest(note: str) -> str:
    return hashlib.sha256(note.encode()).hexdigest()


def _sign(side: str | None) -> int:
    return 1 if side == OrderSide.BUY.value else -1


def _contract_key(coord: ExecutionCoordinator, intent: Intent) -> str:
    pos = coord.store.position(intent.idea_id)
    asset = pos.asset_class if pos else None
    if intent.contract_key or asset is None:
        return intent.contract_key or f"SYMBOL:{intent.symbol}"
    return coord._key_of(intent.symbol, asset, None)


def _ideas_in(coord: ExecutionCoordinator, key: str) -> list:
    return [
        p for p in coord.store.positions()
        if coord._key_of(p.symbol, p.asset_class, p.contract_key) == key
    ]


def _state(coord: ExecutionCoordinator, intent_ref: str | None, idea_id: str | None) -> dict:
    out: dict = {}
    if intent_ref:
        i = coord.store.intent(intent_ref)
        if i is not None:
            out["intent"] = {
                "state": i.state.value, "attempts": i.attempts, "reasons": i.reasons,
                "filled": coord.store.filled_quantity(i.order_ref),
            }
    if idea_id:
        p = coord.store.position(idea_id)
        if p is not None:
            out["position"] = {
                "quantity": p.quantity, "status": p.status, "protection": p.protection,
                "avg_price": p.avg_price,
            }
        out["open_exceptions"] = sorted(
            e.kind for e in coord.store.exceptions(open_only=True) if e.idea_id == idea_id
        )
    return out


def _already_done(coord: ExecutionCoordinator, request_id, action: str, target: str,
                  outcome: str) -> OperatorResult | None:
    """The recorded result of this request (a crash between the journal and the queue result).
    Matched on the request's globally unique id AND its action, target and outcome (review F3):
    the same id with anything else is refused, never answered with another request's result."""
    for e in coord.store.journal():
        if not e.event.startswith("OPERATOR_") or e.detail.get("request_id") != request_id:
            continue
        same = (e.detail.get("action"), e.detail.get("target"), e.detail.get("outcome")) == (
            action, str(target), outcome)
        if not same:
            return OperatorResult(status="REFUSED", reasons=["REQUEST_ID_CONFLICT"])
        return OperatorResult(
            status=e.detail.get("status", "APPLIED"), reasons=e.detail.get("reasons", []),
            before=e.detail.get("before", {}), after=e.detail.get("after", {}),
        )
    return None


def _refuse(coord, request_id, action, reasons, *, order_ref=None, idea_id=None, before=None,
            operator="", note="", target="", outcome="") -> OperatorResult:
    before = before if before is not None else _state(coord, order_ref, idea_id)
    coord.store.log(
        coord.clock(), "OPERATOR_ACTION_REFUSED", order_ref=order_ref, idea_id=idea_id,
        request_id=request_id, action=action, target=str(target), outcome=outcome,
        status="REFUSED", reasons=reasons, before=before, after=before, operator=operator,
        note=note, note_sha256=note_digest(note),
    )
    return OperatorResult(status="REFUSED", reasons=reasons, before=before, after=before)


def _basic_problems(confirmation: str, expected: str, note: str) -> list[str]:
    problems = []
    if confirmation.strip() != expected:
        problems.append("CONFIRMATION_MISMATCH")
    if len(note.strip()) < MIN_NOTE_CHARS:
        problems.append("OPERATOR_NOTE_REQUIRED")
    return problems


# ============================================================================ settlement


def settle_intent(
    coord: ExecutionCoordinator,
    *,
    request_id: str,
    order_ref: str,
    outcome: str,
    confirmation: str,
    note: str,
    operator: str,
    quantity: int | None = None,
    avg_price: str | None = None,
) -> OperatorResult:
    """Settle one intent of unknown outcome from what the operator established at the broker."""
    done = _already_done(coord, request_id, cat.SETTLE, order_ref, outcome)
    if done is not None:
        return done
    declared = coord.store.declared_fill(order_ref)
    if outcome == "FILLED" and declared is not None and declared["declared_state"] == "PARTIAL":
        return _correct_declaration(coord, request_id, order_ref, declared, confirmation, note,
                                    operator, quantity, avg_price)
    intent = coord.store.intent(order_ref)
    idea = intent.idea_id if intent else None
    refuse = lambda reasons, before=None: _refuse(
        coord, request_id, cat.SETTLE, reasons, order_ref=order_ref, idea_id=idea,
        before=before, operator=operator, note=note, target=order_ref, outcome=outcome,
    )
    problems = _basic_problems(
        confirmation, cat.settle_phrase(order_ref, outcome, quantity, avg_price), note
    )
    if outcome not in cat.SETTLEMENT_OUTCOMES:
        problems.append("UNKNOWN_OUTCOME")
    if intent is None:
        return refuse([*problems, "INTENT_UNKNOWN"])
    if intent.state not in UNRESOLVED_STATES:
        problems.append(f"INTENT_NOT_UNRESOLVED:{intent.state.value}")
    if intent.action == Action.MODIFY:
        problems.append("MODIFY_SETTLES_FROM_BROKER_EVIDENCE")
    if coord.settlement_record(order_ref) is not None:
        problems.append("ALREADY_SETTLED_BY_OPERATOR")
    price: Decimal | None = None
    if outcome == "FILLED":
        try:
            price = Decimal(str(avg_price)) if avg_price is not None else None
        except (InvalidOperation, ValueError):
            price = None
        if price is None or not price.is_finite() or price <= 0:
            problems.append("FILL_PRICE_REQUIRED")
        if quantity is None or quantity <= 0:
            problems.append("FILL_QUANTITY_REQUIRED")
    elif quantity is not None or avg_price is not None:
        problems.append("QUANTITY_OR_PRICE_ONLY_WITH_FILLED")
    if problems:
        return refuse(problems)
    before = _state(coord, order_ref, idea)

    # Broker evidence first: everything visible is recorded (fills of every known ref); nothing is
    # sent, amended or cancelled by this call (review note a).
    if not coord.sync(record_only=True):
        return refuse(["BROKER_UNREADABLE"], before)
    try:
        open_orders = coord.broker.open_orders()
        executions = coord.broker.executions()
        completed = coord.broker.completed_orders()
        broker_positions = coord.broker.positions()
    except BrokerError:
        coord.mark_broker_unreadable("operator settlement", BrokerError("read failed"))
        return refuse(["BROKER_UNREADABLE"], before)
    intent = coord.store.intent(order_ref)
    if intent.state not in UNRESOLVED_STATES:
        return refuse([f"SETTLED_FROM_BROKER_EVIDENCE:{intent.state.value}"], before)
    rows = coord.store.orders(order_ref)
    refs = {r.order_ref for r in rows}
    working = sorted(o.order_ref for o in open_orders if o.order_ref in refs)
    own_fills = [f for f in executions if f.order_ref == order_ref]
    done_rows = {o.order_ref: o for o in completed if o.order_ref in refs}
    applied = coord.store.filled_quantity(order_ref)
    if outcome == "NOT_AT_BROKER":
        if working or own_fills or done_rows:
            return refuse(
                [f"BROKER_SHOWS_ORDER:{','.join(sorted(set(working) | set(done_rows)))}"
                 + (":FILLS" if own_fills else "")], before)
        if applied:
            return refuse([f"LEDGER_HAS_FILLS_OF_THIS_ORDER:{applied}"], before)
    elif order_ref in working:
        # Visible: the coordinator adopts it from broker evidence; nothing to declare.
        return refuse([f"BROKER_SHOWS_ORDER_WORKING:{order_ref}"], before)
    if outcome == "CANCELLED":
        entry_done = done_rows.get(order_ref)
        if own_fills or (entry_done is not None and entry_done.status == OrderStatus.FILLED):
            return refuse(["BROKER_SHOWS_FILLS"], before)
        if working and not applied:  # an unfilled cancelled entry leaves no child working
            return refuse([f"BROKER_SHOWS_CHILD_WORKING:{','.join(working)}"], before)
    if outcome == "FILLED":
        entry_done = done_rows.get(order_ref)
        if entry_done is not None and entry_done.filled_quantity == 0 and entry_done.status in (
            OrderStatus.CANCELLED, OrderStatus.API_CANCELLED, OrderStatus.REJECTED,
            OrderStatus.INACTIVE,
        ):
            return refuse([f"BROKER_SHOWS_NOT_FILLED:{entry_done.status.value}"], before)
        problems = _fill_problems(coord, intent, quantity, price, applied)
        if problems:
            return refuse(problems, before)
    declared = (quantity or 0) if outcome == "FILLED" else applied
    contradiction = _position_contradiction(
        coord, intent, declared - applied, broker_positions, outcome
    )
    if contradiction:
        return refuse([contradiction], before)

    _apply_settlement(coord, intent, rows, outcome, declared - applied, price, request_id,
                      operator, note, before)
    # Read and record only: the next normal cycle sizes and confirms protection exactly as after
    # a broker fill (a declared fill with no confirmed stop opens UNPROTECTED_POSITION then), and
    # its verification pass is the earliest a block can lift (review F1, note a).
    after = _state(coord, order_ref, idea)
    return OperatorResult(status="APPLIED", before=before, after=after, lifted=[])


def _correct_declaration(coord, request_id, order_ref, declared, confirmation, note, operator,
                         quantity, avg_price) -> OperatorResult:
    """Review F10: the one second settlement allowed. A FILLED declaration the broker evidences
    only in part may be corrected by a new FILLED settlement naming the true total: never below
    what the broker's executions already show, never above the order, and never one the broker's
    position contradicts. It only changes what the declaration still counts; the block lifts
    only when the broker's executions then cover it."""
    intent = coord.store.intent(order_ref)
    idea = intent.idea_id
    refuse = lambda reasons, before=None: _refuse(
        coord, request_id, cat.SETTLE, reasons, order_ref=order_ref, idea_id=idea,
        before=before, operator=operator, note=note, target=order_ref, outcome="FILLED",
    )
    problems = _basic_problems(
        confirmation, cat.settle_phrase(order_ref, "FILLED", quantity, avg_price), note
    )
    try:
        price = Decimal(str(avg_price)) if avg_price is not None else None
    except (InvalidOperation, ValueError):
        price = None
    if price is None or not price.is_finite() or price <= 0:
        problems.append("FILL_PRICE_REQUIRED")
    if quantity is None or quantity <= 0:
        problems.append("FILL_QUANTITY_REQUIRED")
    if problems:
        return refuse(problems)
    before = _state(coord, order_ref, idea)
    if not coord.sync(record_only=True):
        return refuse(["BROKER_UNREADABLE"], before)
    try:
        broker_positions = coord.broker.positions()
    except BrokerError:
        coord.mark_broker_unreadable("operator settlement", BrokerError("read failed"))
        return refuse(["BROKER_UNREADABLE"], before)
    declared = coord.store.declared_fill(order_ref)
    if declared["declared_state"] != "PARTIAL":
        return refuse([f"DECLARATION_ALREADY_{declared['declared_state']}"], before)
    got = sum(f["quantity"] for f in coord.store.evidenced_fills(order_ref))
    if quantity < got:
        problems.append(f"CORRECTION_BELOW_BROKER_EVIDENCE:{quantity}<{got}")
    if quantity > intent.quantity:
        problems.append(f"FILL_EXCEEDS_ORDER:{quantity}>{intent.quantity}")
    contract = _contract_from(intent.contract)
    if contract is not None and not contract.on_tick(price):
        problems.append("FILL_PRICE_OFF_TICK")
    if problems:
        return refuse(problems, before)
    counted = got + declared["quantity"]
    contradiction = _position_contradiction(coord, intent, quantity - counted, broker_positions,
                                            "FILLED")
    if contradiction:
        return refuse([contradiction], before)
    now = coord.clock()
    coord.store.set_declared_remainder(declared["exec_id"], max(quantity - got, 0), "PARTIAL",
                                       now, declared_quantity=quantity, price=str(price))
    coord.store.log(
        now, "OPERATOR_SETTLEMENT_CORRECTED", order_ref=order_ref, idea_id=idea,
        request_id=request_id, action=cat.SETTLE, target=order_ref, outcome="FILLED",
        status="APPLIED", declared_before=declared["declared_quantity"],
        declared_quantity=quantity, declared_price=str(price), evidenced=got, before=before,
        operator=operator, note=note, note_sha256=note_digest(note),
        rule="a partly evidenced FILLED declaration corrected; lifts only on broker evidence",
    )
    return OperatorResult(status="APPLIED", before=before, after=_state(coord, order_ref, idea))


def _fill_problems(coord, intent: Intent, quantity: int, price: Decimal, applied: int) -> list[str]:
    out = []
    contract = _contract_from(intent.contract)
    if quantity > intent.quantity:
        out.append(f"FILL_EXCEEDS_ORDER:{quantity}>{intent.quantity}")
    if quantity < applied:
        out.append(f"FILL_BELOW_APPLIED_FILLS:{quantity}<{applied}")
    if contract is not None:
        if not contract.on_increment(quantity):
            out.append("QUANTITY_OFF_INCREMENT")
        if not contract.on_tick(price):
            out.append("FILL_PRICE_OFF_TICK")
    limit = Decimal(intent.limit_price) if intent.limit_price else None
    if limit is not None and intent.order_type == "LMT":
        buy = intent.side == OrderSide.BUY.value
        if (buy and price > limit) or (not buy and price < limit):
            out.append(f"FILL_PRICE_VIOLATES_LIMIT:{price}:{limit}")
    if intent.action in (Action.REDUCE, Action.CLOSE):
        pos = coord.store.position(intent.idea_id)
        if pos is None or quantity - applied > pos.quantity:
            out.append("EXIT_FILL_EXCEEDS_FOLLOWER_POSITION")
    return out


def _position_contradiction(coord, intent: Intent, delta: int, broker_positions, outcome) -> str:
    """After the settlement the follower's net position in the contract must be one the broker can
    be holding: equal to the broker's, allowing only for other intents of unknown outcome there."""
    key = _contract_key(coord, intent)
    ledger = sum(
        (1 if p.position_side == PositionSide.LONG else -1) * p.quantity
        for p in _ideas_in(coord, key)
    )
    after = ledger + _sign(intent.side) * delta
    lo = hi = 0
    for other in coord.store.intents(UNRESOLVED_STATES):
        if other.order_ref == intent.order_ref or other.action == Action.MODIFY:
            continue
        if _contract_key(coord, other) != key:
            continue
        left = max(other.quantity - coord.store.filled_quantity(other.order_ref), 0)
        signed = _sign(other.side) * left
        lo, hi = lo + min(0, signed), hi + max(0, signed)
    broker = sum(p.quantity for p in broker_positions if p.contract.contract_key == key)
    if not (after + lo <= broker <= after + hi):
        return f"BROKER_POSITION_CONTRADICTS_{outcome}:follower_after={after},broker={broker}"
    return ""


def _apply_settlement(coord, intent, rows, outcome, fill_qty, price, request_id, operator, note,
                      before) -> None:
    """One transaction: the declared fill (through ``apply_fill``), the intent's final state, its
    order rows, the outcome exceptions it ends, the verification block it opens, the journal."""
    store = coord.store
    now = coord.clock()
    entry_row = next(r for r in rows if r.order_ref == intent.order_ref)
    total = store.filled_quantity(intent.order_ref) + fill_qty
    if outcome == "FILLED":
        state = IntentState.FILLED if total >= intent.quantity else IntentState.CANCELLED
        entry_status = OrderStatus.FILLED.value if state == IntentState.FILLED else "Cancelled"
    else:
        state = IntentState.CANCELLED
        entry_status = "NotAtBroker" if outcome == "NOT_AT_BROKER" else "Cancelled"
    reason = f"OPERATOR_SETTLED_{outcome}"
    with store.tx() as c:
        if fill_qty > 0:
            store.apply_fill(
                exec_id=f"OPERATOR:{request_id}", order=entry_row, quantity=fill_qty,
                price=price, executed_at=now, at=now, c=c, declared=True,
            )
        c.execute(
            "UPDATE exec_intents SET state=?, broker_status=?, reasons=?, updated_at=? "
            "WHERE order_ref=?",
            (state.value, "OperatorSettled", json.dumps([reason]), now.isoformat(),
             intent.order_ref),
        )
        c.execute(
            "UPDATE exec_orders SET status=?, updated_at=? WHERE order_ref=?",
            (entry_status, now.isoformat(), intent.order_ref),
        )
        if outcome != "FILLED":  # no bracket child of an entry that never filled can work
            c.execute(
                "UPDATE exec_orders SET status=?, updated_at=? WHERE intent_ref=? AND role IN "
                "(?,?,?) AND status IN ('Recorded','Unknown')",
                (entry_status, now.isoformat(), intent.order_ref, Role.STOP.value,
                 Role.TARGET.value, Role.BACKSTOP.value),
            )
        pos = c.execute(
            "SELECT quantity, status FROM exec_positions WHERE idea_id=?", (intent.idea_id,)
        ).fetchone()
        if intent.action == Action.OPEN and pos is not None and pos["quantity"] == 0 \
                and pos["status"] == "PENDING":
            c.execute(
                "UPDATE exec_positions SET status='NOT_FILLED', updated_at=? WHERE idea_id=?",
                (now.isoformat(), intent.idea_id),
            )
        ended = []
        for kind in SETTLED_KINDS:
            cur = c.execute(
                "UPDATE exec_exceptions SET resolved_at=? WHERE kind=? AND order_ref=? "
                "AND resolved_at IS NULL",
                (now.isoformat(), kind, intent.order_ref),
            )
            if cur.rowcount:
                ended.append(kind)
        c.execute(
            "INSERT INTO exec_exceptions (at,kind,idea_id,order_ref,detail) VALUES (?,?,?,?,?)",
            (now.isoformat(), OPERATOR_SETTLEMENT_UNVERIFIED, intent.idea_id, intent.order_ref,
             (f"operator request {request_id} settled {intent.order_ref} as {outcome}; new "
              "exposure in this contract stays blocked until a full broker sync and "
              "reconciliation show it clean")),
        )
        store.log(
            now, "OPERATOR_SETTLEMENT", order_ref=intent.order_ref, idea_id=intent.idea_id, c=c,
            request_id=request_id, action=cat.SETTLE, target=intent.order_ref,
            status="APPLIED", outcome=outcome,
            declared_quantity=total if outcome == "FILLED" else 0, declared_filled=total,
            declared_price=str(price) if price is not None else None,
            fill_applied=fill_qty, intent_state=state.value, exceptions_ended=ended,
            before=before, operator=operator, note=note, note_sha256=note_digest(note),
            rule="operator declaration after checking the broker; ledger moved as by a fill",
        )


# ============================================================================ verification


def _blocking_in(coord, report, key: str, exclude=None) -> list[str]:
    """Blocking discrepancy kinds in the contract (``exclude``: the exception being cleared, whose
    own reconciliation line is not evidence against clearing it)."""
    ideas = {p.idea_id for p in _ideas_in(coord, key)}
    symbol = next((p.symbol for p in _ideas_in(coord, key)), None)
    out = []
    for d in report.blocking:
        if exclude is not None and d.kind.value == exclude.kind \
                and d.order_ref == exclude.order_ref and d.idea_id == exclude.idea_id:
            continue
        if (
            d.contract_key == key
            or (d.idea_id is not None and d.idea_id in ideas)
            or (d.contract_key is None and d.symbol is not None and d.symbol == symbol)
        ):
            out.append(d.kind.value)
    return sorted(set(out))


def _no_fill_settlement(coord, e) -> tuple[Intent | None, object]:
    """The intent and settlement journal entry of a NOT_AT_BROKER or CANCELLED settlement."""
    intent = coord.store.intent(e.order_ref) if e.order_ref else None
    if intent is None or not any(r in SETTLED_WITHOUT_FILL_REASONS for r in intent.reasons):
        return None, None
    return intent, coord.settlement_record(intent.order_ref)


def _no_trace_run(coord, intent, record, now) -> tuple[float, list]:
    """Seconds since the settlement and the consecutive no-trace lookups after it (a failed
    lookup breaks the run)."""
    since = (now - record.at).total_seconds()
    run = []
    for j in coord.store.journal(intent.order_ref):
        if j.seq <= record.seq:
            continue
        if j.event == "SETTLEMENT_LOOKUP_FAILED":
            run = []
        elif j.event == "SETTLEMENT_NO_TRACE_LOOKUP" and j.at > record.at:
            run.append(j.at)
    return since, run


def lift_verified_blocks(coord: ExecutionCoordinator) -> list[int]:
    """Resolve each open OPERATOR_SETTLEMENT_UNVERIFIED whose contract a full successful sync and a
    reconciliation made now show without blocking discrepancy. A NOT_AT_BROKER or CANCELLED
    settlement first needs, after it, the settle period and ``exit_no_trace_lookups`` consecutive
    no-trace lookups spread over at least that period (each call is one lookup). Returns the ids
    resolved."""
    pending = [
        e for e in coord.store.exceptions(open_only=True)
        if e.kind == OPERATOR_SETTLEMENT_UNVERIFIED
    ]
    if not pending:
        return []
    no_fill = {e.id: _no_fill_settlement(coord, e) for e in pending}
    no_fill = {k: v for k, v in no_fill.items() if v[0] is not None and v[1] is not None}
    synced = coord.sync()
    reads = None
    if synced and no_fill:
        try:
            since = min(r.at for _, r in no_fill.values()) - COMPLETED_LOOKBACK
            reads = (coord.broker.open_orders(), coord.broker.executions(),
                     coord.broker.completed_orders(since=since))
        except BrokerError as exc:
            coord.mark_broker_unreadable("settlement lookup", exc)
    now = coord.clock()
    for intent, _ in no_fill.values():
        if reads is None:
            coord.store.log(now, "SETTLEMENT_LOOKUP_FAILED", order_ref=intent.order_ref,
                            idea_id=intent.idea_id, rule="a failed lookup restarts the run")
            continue
        fresh = coord.store.intent(intent.order_ref)
        if fresh is None or fresh.state != intent.state or fresh.reasons != intent.reasons:
            continue  # contradicted by the sync just made
        trace = coord.settlement_trace(fresh, *reads)
        if trace:  # seen between the sync and this read: the next sync raises the contradiction
            continue
        coord.store.log(now, "SETTLEMENT_NO_TRACE_LOOKUP", order_ref=intent.order_ref,
                        idea_id=intent.idea_id,
                        rule="no trace in open orders, executions or completed orders")
    if not synced or (reads is None and no_fill):
        return []
    report = coord.reconcile()
    if any(d.kind == DiscrepancyKind.BROKER_UNREADABLE for d in report.discrepancies):
        return []
    lifted = []
    for e in pending:
        if coord.store.exception(e.id).resolved_at is not None:
            continue  # superseded by a contradiction during the sync
        intent = coord.store.intent(e.order_ref) if e.order_ref else None
        if intent is None:
            continue
        declared = coord.store.declared_fill(intent.order_ref)
        if declared is not None and declared["declared_state"] == "PARTIAL":
            continue  # part of the declared fill is not evidenced yet (review F10)
        if e.id in no_fill:
            record = no_fill[e.id][1]
            since, run = _no_trace_run(coord, intent, record, now)
            cfg = coord.config
            span = (run[-1] - run[0]).total_seconds() if run else 0.0
            if since < cfg.settle_seconds or len(run) < cfg.exit_no_trace_lookups \
                    or span < cfg.settle_seconds:
                continue
        key = _contract_key(coord, intent)
        blocking = _blocking_in(coord, report, key)
        if blocking:
            continue
        now = coord.clock()
        with coord.store.tx() as c:
            if coord.store.resolve_exception_id(e.id, now, c):
                coord.store.log(
                    now, "OPERATOR_SETTLEMENT_VERIFIED", order_ref=e.order_ref,
                    idea_id=e.idea_id, c=c, exception_id=e.id, contract=key,
                    reconciliation_at=report.checked_at.isoformat(),
                    rule="full sync and reconciliation after the settlement: contract clean",
                )
                lifted.append(e.id)
    return lifted


# ============================================================================ clear / acknowledge


def clear_exception(
    coord: ExecutionCoordinator, *, request_id: str, exc_id: int, kind: str, confirmation: str,
    note: str, operator: str,
) -> OperatorResult:
    """Close a CLEAR-class exception (LEDGER_REVERSAL) only when a full sync and reconciliation
    made now show its contract without blocking discrepancy."""
    done = _already_done(coord, request_id, cat.CLEAR, str(exc_id), kind)
    if done is not None:
        return done
    e = coord.store.exception(exc_id)
    idea = e.idea_id if e else None
    ref = e.order_ref if e else None
    refuse = lambda reasons: _refuse(
        coord, request_id, cat.CLEAR, reasons, order_ref=ref, idea_id=idea, operator=operator,
        note=note, target=str(exc_id), outcome=kind,
    )
    problems = _basic_problems(confirmation, cat.clear_phrase(exc_id, kind), note)
    if e is None:
        return refuse([*problems, "EXCEPTION_UNKNOWN"])
    if e.kind != kind:
        problems.append(f"KIND_MISMATCH:{e.kind}")
    if e.resolved_at is not None:
        problems.append("EXCEPTION_ALREADY_RESOLVED")
    if cat.action_for(e.kind)[0] != cat.CLEAR:
        problems.append(f"NOT_OPERATOR_CLEARABLE:{cat.action_for(e.kind)[0]}")
    if problems:
        return refuse(problems)
    before = _state(coord, ref, idea)
    if not coord.sync():
        return refuse(["BROKER_UNREADABLE"])
    report = coord.reconcile()
    if any(d.kind == DiscrepancyKind.BROKER_UNREADABLE for d in report.discrepancies):
        return refuse(["BROKER_UNREADABLE"])
    pos = coord.store.position(idea) if idea else None
    key = (
        coord._key_of(pos.symbol, pos.asset_class, pos.contract_key) if pos else f"IDEA:{idea}"
    )
    blocking = _blocking_in(coord, report, key, exclude=e)
    if blocking:
        return refuse([f"CONTRACT_NOT_CLEAN:{','.join(blocking)}"])
    now = coord.clock()
    with coord.store.tx() as c:
        coord.store.resolve_exception_id(exc_id, now, c)
        coord.store.log(
            now, "OPERATOR_CLEARED", order_ref=ref, idea_id=idea, c=c, request_id=request_id,
            action=cat.CLEAR, target=str(exc_id), outcome=kind, status="APPLIED",
            exception_id=exc_id, kind=e.kind, before=before,
            after=_state_after_resolve(before, e.kind), contract=key, operator=operator,
            note=note, note_sha256=note_digest(note),
            rule="cleared only after a full sync and reconciliation show the contract clean",
        )
    return OperatorResult(status="APPLIED", before=before, after=_state(coord, ref, idea))


def acknowledge_exception(
    coord: ExecutionCoordinator, *, request_id: str, exc_id: int, kind: str, confirmation: str,
    note: str, operator: str,
) -> OperatorResult:
    """Close an ACKNOWLEDGE-class record (it blocks no exposure); no broker read is needed."""
    done = _already_done(coord, request_id, cat.ACKNOWLEDGE, str(exc_id), kind)
    if done is not None:
        return done
    e = coord.store.exception(exc_id)
    idea = e.idea_id if e else None
    ref = e.order_ref if e else None
    problems = _basic_problems(confirmation, cat.acknowledge_phrase(exc_id, kind), note)
    if e is None:
        problems.append("EXCEPTION_UNKNOWN")
    else:
        if e.kind != kind:
            problems.append(f"KIND_MISMATCH:{e.kind}")
        if e.resolved_at is not None:
            problems.append("EXCEPTION_ALREADY_RESOLVED")
        if cat.action_for(e.kind)[0] != cat.ACKNOWLEDGE:
            problems.append(f"NOT_ACKNOWLEDGEABLE:{cat.action_for(e.kind)[0]}")
    if problems:
        return _refuse(coord, request_id, cat.ACKNOWLEDGE, problems, order_ref=ref, idea_id=idea,
                       operator=operator, note=note, target=str(exc_id), outcome=kind)
    before = _state(coord, ref, idea)
    now = coord.clock()
    with coord.store.tx() as c:
        coord.store.resolve_exception_id(exc_id, now, c)
        coord.store.log(
            now, "OPERATOR_ACKNOWLEDGED", order_ref=ref, idea_id=idea, c=c,
            request_id=request_id, action=cat.ACKNOWLEDGE, target=str(exc_id), outcome=kind,
            status="APPLIED", exception_id=exc_id,
            kind=e.kind, before=before, after=_state_after_resolve(before, e.kind),
            operator=operator, note=note, note_sha256=note_digest(note),
        )
    return OperatorResult(status="APPLIED", before=before, after=_state(coord, ref, idea))


def _state_after_resolve(before: dict, kind: str) -> dict:
    after = json.loads(json.dumps(before))
    kinds = list(after.get("open_exceptions", []))
    if kind in kinds:
        kinds.remove(kind)
    after["open_exceptions"] = kinds
    return after

