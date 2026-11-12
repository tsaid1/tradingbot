"""Operator settlement of unresolved state (execution.settlement). SimBroker, invented data only.

A settlement only records what a human established at the broker: it never sends, amends or
cancels an order, it is refused when the broker contradicts it, and the instrument's block lifts
only after a full successful sync and reconciliation show the contract clean.
"""

from __future__ import annotations

import pytest
from execution_harness import START as START_AT
from execution_harness import SYMBOL, Harness, open_action

from tradingbot.execution import coordinator as C
from tradingbot.execution import operator_catalog as cat
from tradingbot.execution import replay as R
from tradingbot.execution import settlement as S
from tradingbot.execution import store as ST
from tradingbot.execution.broker import OrderIntent, OrderSide, OrderType
from tradingbot.execution.store import IntentState
from tradingbot.models import Action, AssetClass, Instrument

NOTE = "checked the invented account by hand"


@pytest.fixture
def h(tmp_path):
    return Harness(tmp_path)


_ids = iter(range(1, 10_000))


def settle(h, ref, outcome, *, confirm=None, note=NOTE, request_id=None, **kw):
    return S.settle_intent(
        h.coord, request_id=request_id or next(_ids), order_ref=ref, outcome=outcome,
        confirmation=confirm if confirm is not None else cat.settle_phrase(
            ref, outcome, kw.get("quantity"), kw.get("avg_price")),
        note=note, operator="tester", **kw,
    )


def open_kinds(h) -> list[str]:
    return sorted(e.kind for e in h.coord.store.exceptions(open_only=True))


def never_arrived(h, sid="p1", **kw):
    """Submitted, the request never reached the broker, the acknowledgement was lost."""
    h.broker.script_next_submit("LOST_ACK_NOT_PLACED")
    out = h.execute(sid, open_action(**kw))
    h.clock.advance(h.config.settle_seconds + 1)
    h.coord.recover()
    assert h.coord.intent(out.order_ref).state == IntentState.UNRESOLVED
    return out.order_ref


def filled_beyond_horizon(h, **kw):
    """Placed and filled, the ack lost, the first lookup failed, and the broker's history no
    longer reaches back to it: only the position remains visible."""
    h.broker.history_seconds = 3600
    h.broker.script_next_submit("LOST_ACK_PLACED")
    h.broker.lookup_failures_after_submit = 1
    out = h.execute("p1", open_action(**kw))
    h.clock.advance(h.config.lookup_horizon_seconds + 60)
    h.broker.set_quote(SYMBOL, "49.98", "50.02")
    h.coord.recover()
    return out.order_ref


def second_open_state(h, sid="p9"):
    h.broker.set_quote(SYMBOL, "49.98", "50.02")
    return h.execute(sid, open_action())


# ----------------------------------------------------------------------------------- NOT_AT_BROKER


def test_not_at_broker_settles_the_intent_and_lifts_the_block_after_a_clean_sync(h):
    ref = never_arrived(h)
    assert C.UNKNOWN_OUTCOME_ENTRY_NOT_RESENT in open_kinds(h)
    blocked = second_open_state(h, "p2")
    assert blocked.state == IntentState.REFUSED
    assert blocked.reasons == [C.UNRESOLVED_ENTRY_IN_INSTRUMENT]
    calls = h.broker.submit_calls

    result = settle(h, ref, "NOT_AT_BROKER")

    assert result.status == "APPLIED" and result.reasons == []
    assert h.broker.submit_calls == calls  # nothing sent
    intent = h.coord.intent(ref)
    assert intent.state == IntentState.CANCELLED
    assert intent.reasons == ["OPERATOR_SETTLED_NOT_AT_BROKER"]
    assert h.coord.store.position("p1:open").status == "NOT_FILLED"
    # The outcome exception ended; the verification block never lifts in the same call (F1).
    assert open_kinds(h) == [C.OPERATOR_SETTLEMENT_UNVERIFIED]
    assert result.lifted == [] and result.before["intent"]["state"] == "UNRESOLVED"
    assert result.after["intent"]["state"] == "CANCELLED"
    assert second_open_state(h, "p2b").state == IntentState.REFUSED
    assert wait_for_verification(h)
    assert open_kinds(h) == []
    events = h.events(ref)
    assert "OPERATOR_SETTLEMENT" in events and "OPERATOR_SETTLEMENT_VERIFIED" in events
    entry = next(e for e in h.coord.store.journal(ref) if e.event == "OPERATOR_SETTLEMENT")
    assert entry.detail["before"]["intent"]["state"] == "UNRESOLVED"
    assert entry.detail["note_sha256"] == S.note_digest(NOTE) and NOTE not in str(entry.detail)
    assert second_open_state(h, "p3").state == IntentState.FILLED  # exposure allowed again


def test_not_at_broker_is_refused_while_the_order_is_visible(h):
    h.broker.open_orders_lag_seconds = 3600  # the resting order is not visible yet
    h.broker.script_next_submit("LOST_ACK_PLACED_RESTING")
    out = h.execute("p1", open_action())
    h.clock.advance(h.config.settle_seconds + 1)
    h.coord.recover()
    assert h.coord.intent(out.order_ref).state == IntentState.UNRESOLVED
    h.broker.open_orders_lag_seconds = 0  # now the broker shows it
    result = settle(h, out.order_ref, "NOT_AT_BROKER")
    assert result.status == "REFUSED" and result.reasons[0].startswith("BROKER_SHOWS_ORDER:")
    assert h.coord.intent(out.order_ref).state == IntentState.UNRESOLVED
    assert "OPERATOR_ACTION_REFUSED" in h.events(out.order_ref)
    filled = settle(h, out.order_ref, "FILLED", quantity=4, avg_price="50.02")
    assert filled.status == "REFUSED"
    assert filled.reasons == [f"BROKER_SHOWS_ORDER_WORKING:{out.order_ref}"]


def test_not_at_broker_is_refused_while_the_broker_holds_the_position(h):
    ref = filled_beyond_horizon(h, stop=None, target=None)
    assert C.UNKNOWN_OUTCOME_BEYOND_LOOKUP_HORIZON in open_kinds(h)
    for outcome in ("NOT_AT_BROKER", "CANCELLED"):
        result = settle(h, ref, outcome)
        assert result.status == "REFUSED"
        assert result.reasons == [
            f"BROKER_POSITION_CONTRADICTS_{outcome}:follower_after=0,broker=4"
        ]
    assert h.coord.intent(ref).state == IntentState.UNRESOLVED


# ----------------------------------------------------------------------------------- FILLED


def test_filled_beyond_the_horizon_creates_the_position_as_a_broker_fill_would(h):
    ref = filled_beyond_horizon(h, stop=None, target=None)
    result = settle(h, ref, "FILLED", quantity=4, avg_price="50.02", request_id=777)
    assert result.status == "APPLIED"
    pos = h.coord.store.position("p1:open")
    assert pos.quantity == 4 and pos.status == "OPEN" and pos.avg_price == "50.0200"
    [fill] = h.coord.store.fills(ref)
    assert fill["exec_id"] == "OPERATOR:777" and fill["quantity"] == 4
    assert h.coord.intent(ref).state == IntentState.FILLED
    assert open_kinds(h) == [C.OPERATOR_SETTLEMENT_UNVERIFIED]  # never lifted in the same call
    assert S.lift_verified_blocks(h.coord)  # the next normal cycle: clean, so it lifts
    assert open_kinds(h) == [] and h.coord.reconcile().clean
    # The same request applied again changes nothing (crash between journal and result).
    again = settle(h, ref, "FILLED", quantity=4, avg_price="50.02", request_id=777)
    assert again.status == "APPLIED" and len(h.coord.store.fills(ref)) == 1


def test_declared_fill_without_a_confirmed_stop_is_flagged_unprotected_and_stays_blocked(h):
    h.broker.history_seconds = 3600
    h.broker.script_next_submit("LOST_ACK_PLACED")
    h.broker.lookup_failures_after_submit = 1
    out = h.execute("p1", open_action())
    h.clock.advance(h.config.lookup_horizon_seconds + 60)
    h.broker.set_quote(SYMBOL, "49.98", "50.02")
    h.coord.recover()  # only the bracket's children are visible: the entry's state is unknown
    assert C.ENTRY_STATE_UNKNOWN in open_kinds(h)
    h.broker.cancel(f"{out.order_ref}.STP")  # someone cancelled the stop at the broker
    h.clock.advance(1)
    h.broker.set_quote(SYMBOL, "49.98", "50.02")

    result = settle(h, out.order_ref, "FILLED", quantity=4, avg_price="50.02")

    assert result.status == "APPLIED"
    h.coord.sync()  # the next normal cycle sizes and confirms protection, as after a broker fill
    pos = h.coord.store.position("p1:open")
    assert pos.quantity == 4 and pos.protection == "UNPROTECTED"
    kinds = open_kinds(h)
    assert C.UNPROTECTED_POSITION in kinds and C.OPERATOR_SETTLEMENT_UNVERIFIED in kinds
    assert C.ENTRY_STATE_UNKNOWN not in kinds and result.lifted == []
    refused = second_open_state(h, "p2")
    assert refused.state == IntentState.REFUSED
    assert refused.reasons[0].startswith("INSTRUMENT_BLOCKED_")


@pytest.mark.parametrize(
    ("quantity", "price", "reason"),
    [
        (5, "50.02", "FILL_EXCEEDS_ORDER:5>4"),
        (4, "60", "FILL_PRICE_VIOLATES_LIMIT:60:50.50"),
        (4, "50.015", "FILL_PRICE_OFF_TICK"),
        (2, "50.02", "BROKER_POSITION_CONTRADICTS_FILLED:follower_after=2,broker=4"),
    ],
)
def test_filled_declarations_the_evidence_contradicts_are_refused(h, quantity, price, reason):
    ref = filled_beyond_horizon(h, stop=None, target=None)
    result = settle(h, ref, "FILLED", quantity=quantity, avg_price=price)
    assert result.status == "REFUSED" and reason in result.reasons
    assert h.coord.store.fills(ref) == [] and h.coord.intent(ref).state == IntentState.UNRESOLVED


# ----------------------------------------------------------------------------------- CANCELLED


def test_cancelled_beyond_the_horizon_ends_the_intent_without_a_fill(h):
    h.broker.history_seconds = 3600
    h.broker.script_next_submit("LOST_ACK_CANCELLED")
    h.broker.lookup_failures_after_submit = 1
    out = h.execute("p1", open_action())
    h.clock.advance(h.config.lookup_horizon_seconds + 60)
    h.broker.set_quote(SYMBOL, "49.98", "50.02")
    h.coord.recover()
    assert h.coord.intent(out.order_ref).state == IntentState.UNRESOLVED
    result = settle(h, out.order_ref, "CANCELLED")
    assert result.status == "APPLIED"
    assert h.coord.intent(out.order_ref).state == IntentState.CANCELLED
    assert h.coord.store.position("p1:open").status == "NOT_FILLED"
    assert open_kinds(h) == [C.OPERATOR_SETTLEMENT_UNVERIFIED]
    assert wait_for_verification(h)
    assert open_kinds(h) == []


# ----------------------------------------------------------------------------------- form


def test_settlement_form_is_checked_before_the_broker_is_read(h):
    ref = never_arrived(h)
    reads = {"n": 0}
    real = h.broker.open_orders

    def counting():
        reads["n"] += 1
        return real()

    h.broker.open_orders = counting
    cases = [
        ({"confirm": f"SETTLE {ref} FILLED"}, "CONFIRMATION_MISMATCH"),
        ({"note": "ok"}, "OPERATOR_NOTE_REQUIRED"),
        ({"quantity": 1}, "QUANTITY_OR_PRICE_ONLY_WITH_FILLED"),
    ]
    for kw, reason in cases:
        result = settle(h, ref, "NOT_AT_BROKER", **kw)
        assert result.status == "REFUSED" and reason in result.reasons
    assert settle(h, ref, "FILLED").reasons == ["FILL_PRICE_REQUIRED", "FILL_QUANTITY_REQUIRED"]
    assert settle(h, "TB-unknown", "CANCELLED").reasons == ["INTENT_UNKNOWN"]
    assert reads["n"] == 0
    assert h.coord.intent(ref).state == IntentState.UNRESOLVED


def test_only_an_intent_of_unknown_outcome_can_be_settled(h):
    out = h.execute("p1", open_action())
    assert out.state == IntentState.FILLED
    result = settle(h, out.order_ref, "CANCELLED")
    assert result.reasons == ["INTENT_NOT_UNRESOLVED:FILLED"]


# ----------------------------------------------------------------------------------- verification


def test_the_block_lifts_only_after_a_full_successful_sync(h, monkeypatch):
    ref = never_arrived(h)
    result = settle(h, ref, "NOT_AT_BROKER")
    assert result.status == "APPLIED" and result.lifted == []
    real_sync = h.coord.sync
    state = {"fail": True}

    def sync(**kw):
        return False if state["fail"] else real_sync(**kw)

    monkeypatch.setattr(h.coord, "sync", sync)
    for _ in range(10):  # the broker is unreadable after the settlement: nothing lifts
        h.clock.advance(3)
        assert S.lift_verified_blocks(h.coord) == []
    assert open_kinds(h) == [C.OPERATOR_SETTLEMENT_UNVERIFIED]
    state["fail"] = False
    blocked = second_open_state(h, "p2")
    assert blocked.reasons == [f"INSTRUMENT_BLOCKED_{C.OPERATOR_SETTLEMENT_UNVERIFIED}"]
    assert wait_for_verification(h) != []
    assert open_kinds(h) == []
    assert second_open_state(h, "p3").state == IntentState.FILLED


def test_a_settlement_a_foreign_broker_position_contradicts_is_refused(h):
    ref = never_arrived(h)
    foreign = h.broker.resolve(Instrument(asset_class=AssetClass.EQUITY, symbol=SYMBOL))
    h.broker.submit(OrderIntent(order_ref="FOREIGN-1", contract=foreign, side=OrderSide.BUY,
                                quantity=1, order_type=OrderType.MKT))
    # The settlement itself is refused: the broker holds 1 the follower cannot account for.
    result = settle(h, ref, "NOT_AT_BROKER")
    assert result.status == "REFUSED" and result.reasons[0].startswith("BROKER_POSITION_CONTRADICTS")


# ----------------------------------------------------------------------------------- clear / ack


def _reversal(h):
    out = h.execute("p1", open_action(target=None))
    stop = f"{out.order_ref}.STP"
    h.broker.modify(stop, quantity=6)  # someone resized the stop at the broker
    h.clock.advance(1)
    h.broker.set_quote(SYMBOL, "47.90", "47.95")  # the stop triggers: sells 6 against 4
    h.coord.sync()
    [rev] = [e for e in h.coord.store.exceptions(open_only=True) if e.kind == ST.LEDGER_REVERSAL]
    return rev, foreign_contract(h)


def foreign_contract(h):
    return h.broker.resolve(Instrument(asset_class=AssetClass.EQUITY, symbol=SYMBOL))


def test_a_reversal_clears_only_once_the_broker_is_flat_again(h):
    rev, contract = _reversal(h)
    phrase = cat.clear_phrase(rev.id, rev.kind)
    first = S.clear_exception(h.coord, request_id=901, exc_id=rev.id, kind=rev.kind,
                              confirmation=phrase, note=NOTE, operator="tester")
    assert first.status == "REFUSED"
    assert first.reasons[0].startswith("CONTRACT_NOT_CLEAN:")
    assert "UNEXPLAINED_BROKER_POSITION" in first.reasons[0]
    # The operator buys back the excess 2 at the broker by hand.
    h.broker.submit(OrderIntent(order_ref="MANUAL-1", contract=contract, side=OrderSide.BUY,
                                quantity=2, order_type=OrderType.MKT))
    done = S.clear_exception(h.coord, request_id=902, exc_id=rev.id, kind=rev.kind,
                             confirmation=phrase, note=NOTE, operator="tester")
    assert done.status == "APPLIED" and ST.LEDGER_REVERSAL not in open_kinds(h)
    assert "OPERATOR_CLEARED" in h.events()


def test_evidence_kinds_can_be_neither_cleared_nor_acknowledged(h):
    h.broker.script_next_submit("DROP_STOP_CHILD")
    h.execute("p1", open_action())
    [e] = h.coord.store.exceptions(open_only=True)
    assert e.kind == C.UNPROTECTED_POSITION
    clr = S.clear_exception(h.coord, request_id=911, exc_id=e.id, kind=e.kind,
                            confirmation=cat.clear_phrase(e.id, e.kind), note=NOTE,
                            operator="tester")
    ack = S.acknowledge_exception(h.coord, request_id=912, exc_id=e.id, kind=e.kind,
                                  confirmation=cat.acknowledge_phrase(e.id, e.kind), note=NOTE,
                                  operator="tester")
    assert clr.reasons == ["NOT_OPERATOR_CLEARABLE:EVIDENCE"]
    assert ack.reasons == ["NOT_ACKNOWLEDGEABLE:EVIDENCE"]
    assert open_kinds(h) == [C.UNPROTECTED_POSITION]


def test_an_informational_exception_is_acknowledged_without_touching_the_broker(h):
    h.execute("p1", open_action())
    h.broker.set_quote(SYMBOL, None, None)  # no usable quote: the exit is not sent
    from execution_harness import action

    out = h.execute("p2", action("close", Action.CLOSE, idea="p1:open"))
    assert out.state == IntentState.REFUSED
    [e] = [x for x in h.coord.store.exceptions(open_only=True) if x.kind == C.EXIT_WITHOUT_QUOTE]
    calls = h.broker.submit_calls
    wrong = S.acknowledge_exception(h.coord, request_id=921, exc_id=e.id, kind="PARTIAL_GROUP",
                                    confirmation=cat.acknowledge_phrase(e.id, "PARTIAL_GROUP"),
                                    note=NOTE, operator="tester")
    assert wrong.reasons == [f"KIND_MISMATCH:{e.kind}"]
    ok = S.acknowledge_exception(h.coord, request_id=922, exc_id=e.id, kind=e.kind,
                                 confirmation=cat.acknowledge_phrase(e.id, e.kind), note=NOTE,
                                 operator="tester")
    assert ok.status == "APPLIED" and h.broker.submit_calls == calls
    assert C.EXIT_WITHOUT_QUOTE not in open_kinds(h)


# ----------------------------------------------------------------------------------- lagging broker


def resting_but_invisible(h, sid="p1"):
    """Placed and resting at the broker, the ack lost, and open orders lag: the operator sees
    nothing and (wrongly) declares NOT_AT_BROKER."""
    h.broker.open_orders_lag_seconds = 3600
    h.broker.script_next_submit("LOST_ACK_PLACED_RESTING")
    out = h.execute(sid, open_action())
    h.clock.advance(h.config.settle_seconds + 1)
    h.coord.recover()
    assert h.coord.intent(out.order_ref).state == IntentState.UNRESOLVED
    return out.order_ref


def broker_net(h) -> int:
    return sum(p.quantity for p in h.broker.positions())


def wait_for_verification(h, seconds=None, step=2.0):
    """Normal cycles after a settlement: recover, then the verification pass."""
    lifted = []
    end = seconds if seconds is not None else h.config.settle_seconds * 3
    t = 0.0
    while t <= end and not lifted:
        h.clock.advance(step)
        t += step
        h.broker.set_quote(SYMBOL, "49.98", "50.02")
        h.coord.recover()
        lifted = S.lift_verified_blocks(h.coord)
    return lifted


def test_probe_not_at_broker_while_open_orders_lag_never_lifts_in_the_same_call(h):
    """Review F1 probe: NOT_AT_BROKER declared on a lagging read. The block must not lift in the
    call that records it; a second OPEN stays refused; the original's later fill is a
    contradiction (recorded, blocking, alerted, reconciliation unclean), never a silent adoption."""
    ref = resting_but_invisible(h)
    result = settle(h, ref, "NOT_AT_BROKER")
    assert result.status == "APPLIED" and result.lifted == []
    assert C.OPERATOR_SETTLEMENT_UNVERIFIED in open_kinds(h)
    second = second_open_state(h, "p2")
    assert second.state == IntentState.REFUSED
    assert second.reasons == [f"INSTRUMENT_BLOCKED_{C.OPERATOR_SETTLEMENT_UNVERIFIED}"]

    h.broker.fill(ref)  # the original was resting all along and now fills
    h.coord.sync()

    assert h.coord.store.filled_quantity(ref) == 4  # recorded as it happened
    assert broker_net(h) == 4
    assert C.SETTLED_INTENT_CONTRADICTED in open_kinds(h)
    intent = h.coord.intent(ref)
    assert C.SETTLED_INTENT_CONTRADICTED in intent.reasons
    assert intent.state == IntentState.FILLED
    events = h.events(ref)
    assert events.index("OPERATOR_SETTLEMENT") < events.index(C.SETTLED_INTENT_CONTRADICTED)
    rec = h.coord.reconcile()
    assert not rec.clean
    assert "SETTLED_INTENT_CONTRADICTED" in [d.kind.value for d in rec.blocking]
    third = second_open_state(h, "p3")
    assert third.state == IntentState.REFUSED
    assert third.reasons[0].startswith("INSTRUMENT_BLOCKED_")


def test_not_at_broker_lifts_only_after_the_settle_period_and_spread_no_trace_lookups(h):
    ref = never_arrived(h)
    t0 = h.clock.now()
    assert settle(h, ref, "NOT_AT_BROKER").lifted == []
    lifted = wait_for_verification(h)
    assert lifted
    elapsed = (h.clock.now() - t0).total_seconds()
    assert elapsed >= h.config.settle_seconds
    lookups = [e for e in h.coord.store.journal(ref) if e.event == "SETTLEMENT_NO_TRACE_LOOKUP"]
    assert len(lookups) >= h.config.exit_no_trace_lookups
    assert (lookups[-1].at - lookups[0].at).total_seconds() >= h.config.settle_seconds
    assert all(e.at > t0 for e in lookups)
    assert "OPERATOR_SETTLEMENT_VERIFIED" in h.events(ref)
    assert second_open_state(h, "p2").state == IntentState.FILLED


def test_a_failed_lookup_restarts_the_no_trace_run(h):
    ref = never_arrived(h)
    settle(h, ref, "NOT_AT_BROKER")
    for _ in range(4):
        h.clock.advance(3)
        S.lift_verified_blocks(h.coord)
    h.broker.lookup_failures = 1
    h.clock.advance(3)
    assert S.lift_verified_blocks(h.coord) == []
    h.clock.advance(1)
    assert S.lift_verified_blocks(h.coord) == []  # the run restarted: too few, too close
    assert "SETTLEMENT_LOOKUP_FAILED" in h.events(ref)
    assert C.OPERATOR_SETTLEMENT_UNVERIFIED in open_kinds(h)


def test_contradiction_when_the_order_appears_as_working(h):
    ref = resting_but_invisible(h)
    settle(h, ref, "NOT_AT_BROKER")
    h.broker.open_orders_lag_seconds = 0  # the broker's view catches up: resting, unfilled
    h.coord.sync()
    assert C.SETTLED_INTENT_CONTRADICTED in open_kinds(h)
    assert C.OPERATOR_SETTLEMENT_UNVERIFIED not in open_kinds(h)  # superseded, still blocked
    assert h.coord.intent(ref).state == IntentState.WORKING
    assert not h.coord.reconcile().clean
    assert second_open_state(h, "p2").state == IntentState.REFUSED
    assert S.lift_verified_blocks(h.coord) == []


def test_contradiction_after_a_restart(h):
    ref = resting_but_invisible(h)
    settle(h, ref, "NOT_AT_BROKER")
    h.restart()  # a new process: the broker's view no longer lags
    h.broker.set_quote(SYMBOL, "49.98", "50.02")
    h.broker.fill(ref)
    h.coord.recover()
    assert C.SETTLED_INTENT_CONTRADICTED in open_kinds(h)
    assert h.coord.store.filled_quantity(ref) == 4
    assert not h.coord.reconcile().clean


def test_contradiction_when_the_lag_outlasts_the_settle_period(h):
    """The broker's view lags longer than the evidence window: the block lifts on the evidence
    the system has, a second OPEN fills, then the original fills. The account holds 8; that is
    raised at once as a contradiction, blocks the contract and keeps reconciliation unclean."""
    ref = resting_but_invisible(h)
    settle(h, ref, "NOT_AT_BROKER")
    assert wait_for_verification(h)
    second = second_open_state(h, "p2")
    assert second.state == IntentState.FILLED
    h.broker.fill(ref)
    h.coord.sync()
    assert broker_net(h) == 8
    assert C.SETTLED_INTENT_CONTRADICTED in open_kinds(h)
    rec = h.coord.reconcile()
    assert not rec.clean
    assert second_open_state(h, "p3").state == IntentState.REFUSED
    [e] = [x for x in h.coord.store.exceptions(open_only=True)
           if x.kind == C.SETTLED_INTENT_CONTRADICTED]
    assert e.order_ref == ref and "NOT_AT_BROKER" in e.detail


def test_cancelled_settlement_contradicted_by_a_later_fill(h):
    ref = resting_but_invisible(h)
    settle(h, ref, "CANCELLED")
    h.broker.fill(ref)
    h.coord.sync()
    assert C.SETTLED_INTENT_CONTRADICTED in open_kinds(h)


def test_reconciliation_is_unclean_when_fills_exceed_what_the_settlement_declared(h):
    """Review F1.3, independently of the exception: the ledger holds fills of an intent settled as
    never filled; reconciliation counts them as unexplained."""
    ref = never_arrived(h)
    settle(h, ref, "NOT_AT_BROKER")
    row = h.coord.store.order(ref)
    from decimal import Decimal

    h.coord.store.apply_fill(exec_id="INVENTED-LATE-1", order=row, quantity=4,
                             price=Decimal("50.02"), executed_at=h.clock.now(),
                             at=h.clock.now())
    rec = h.coord.reconcile()
    assert "SETTLED_INTENT_CONTRADICTED" in [d.kind.value for d in rec.blocking]
    assert not rec.clean


def test_every_exception_kind_has_an_operator_class():
    kinds = {
        v for mod in (C, ST, R) for k, v in vars(mod).items()
        if k.isupper() and isinstance(v, str) and v.isupper() and "_" in v
        and k not in ("SCHEMA",)
    }
    opened = {  # the kinds raise_exception / INSERT are called with
        C.UNPROTECTED_POSITION, C.PROTECTION_UNVERIFIED, C.MODIFY_NOT_CONFIRMED,
        C.PROTECTIVE_LEVEL_NOT_LOOSENED, C.STALE_AFTER_UNKNOWN_OUTCOME,
        C.UNKNOWN_OUTCOME_BEYOND_LOOKUP_HORIZON, C.UNKNOWN_OUTCOME_ENTRY_NOT_RESENT,
        C.EXIT_NOT_NETTED, C.EXIT_WITHOUT_QUOTE, C.ENTRY_STATE_UNKNOWN, C.BROKER_STATE_UNKNOWN,
        C.ENTRY_REMAINDER_NOT_CANCELLED, C.OPERATOR_SETTLEMENT_UNVERIFIED,
        C.BROKER_RECONNECT_PENDING, ST.LEDGER_REVERSAL, R.PARTIAL_GROUP,
        C.SETTLED_INTENT_CONTRADICTED,
    }
    assert opened <= kinds
    assert opened == set(cat.EXCEPTION_ACTIONS)
    # Every blocking kind is resolved by evidence or by a verified clear, never by a declaration.
    for kind in (*C.INSTRUMENT_BLOCKING_KINDS, *C.ACCOUNT_BLOCKING_KINDS):
        assert cat.action_for(kind)[0] in (cat.EVIDENCE, cat.CLEAR, cat.SETTLE), kind
        assert cat.action_for(kind)[0] != cat.ACKNOWLEDGE


def test_a_filled_confirmation_names_the_quantity_and_price_declared(h):
    """Review F5: a mistyped size or price cannot be confirmed by habit."""
    ref = filled_beyond_horizon(h, stop=None, target=None)
    assert cat.settle_phrase(ref, "FILLED", 4, "50.02") == f"SETTLE {ref} FILLED 4 @ 50.02"
    for phrase in (f"SETTLE {ref} FILLED", f"SETTLE {ref} FILLED 5 @ 50.02",
                   f"SETTLE {ref} FILLED 4 @ 50.20"):
        result = settle(h, ref, "FILLED", quantity=4, avg_price="50.02", confirm=phrase)
        assert result.status == "REFUSED" and "CONFIRMATION_MISMATCH" in result.reasons
    ok = settle(h, ref, "FILLED", quantity=4, avg_price="50.02",
                confirm=f"SETTLE {ref} FILLED 4 @ 50.02")
    assert ok.status == "APPLIED"


def test_the_queue_wants_the_quantity_and_price_in_a_filled_confirmation(tmp_path):
    from tradingbot.runtime import operator_actions as Q

    queue = Q.OperatorQueue.initialise(tmp_path / "operator.sqlite3")
    want = Q.expected_confirmation(Q.SETTLE, "TB-x", outcome="FILLED", quantity=2,
                                   avg_price="88.42")
    assert want == "SETTLE TB-x FILLED 2 @ 88.42"
    common = {"at": START_AT, "operator": "tester", "origin": "test", "action": Q.SETTLE,
              "target": "TB-x", "outcome": "FILLED", "quantity": 2, "avg_price": "88.42",
              "note": NOTE}
    with pytest.raises(Q.OperatorInputError, match="type exactly"):
        queue.enqueue(**common, confirmation="SETTLE TB-x FILLED")
    assert queue.enqueue(**common, confirmation=want) == 1


def test_a_settlement_only_reads_and_records_and_leaves_upkeep_to_the_normal_cycle(h):
    """Review note (a): the settlement's own call sends, amends and cancels nothing, not even
    the risk-reducing housekeeping a sync does (here: cancelling a working entry under the halt).
    The next normal cycle does that."""
    h.broker.set_quote("ZZR", "19.98", "20.02")
    h.broker.script_next_submit("REST")
    working = h.execute("p0", open_action(symbol="ZZR", entry="20", stop="19", target="22"))
    assert working.state == IntentState.WORKING
    ref = never_arrived(h, "p1")
    h.halt()
    calls = []
    for name in ("submit", "modify", "cancel", "modify_condition"):
        real = getattr(h.broker, name)

        def counting(*a, _real=real, _name=name, **k):
            calls.append(_name)
            return _real(*a, **k)

        setattr(h.broker, name, counting)
    result = settle(h, ref, "NOT_AT_BROKER")
    assert result.status == "APPLIED" and calls == []
    assert "HALT_ENTRY_CANCELLED" not in h.events(working.order_ref)
    h.coord.sync()  # the normal cycle
    assert "cancel" in calls and "HALT_ENTRY_CANCELLED" in h.events(working.order_ref)


def test_a_partial_group_is_cleared_on_evidence_never_acknowledged(h):
    """Review note (b): an open PARTIAL_GROUP keeps reconciliation unclean, so closing it can lift
    blocks that wait on a clean reconciliation. It is therefore CLEAR-class, verified like any
    clear, not a plain acknowledgement."""
    out = h.execute("p1", open_action())
    assert out.state == IntentState.FILLED and h.coord.reconcile().clean
    h.coord.store.raise_exception(h.clock.now(), R.PARTIAL_GROUP, "invented: group half done",
                                  idea_id="p1:open")
    [e] = [x for x in h.coord.store.exceptions(open_only=True) if x.kind == R.PARTIAL_GROUP]
    assert not h.coord.reconcile().clean
    assert cat.action_for(R.PARTIAL_GROUP)[0] == cat.CLEAR
    ack = S.acknowledge_exception(h.coord, request_id="ack-pg", exc_id=e.id, kind=e.kind,
                                  confirmation=cat.acknowledge_phrase(e.id, e.kind), note=NOTE,
                                  operator="tester")
    assert ack.reasons == ["NOT_ACKNOWLEDGEABLE:CLEAR"]
    foreign = foreign_contract(h)
    h.broker.submit(OrderIntent(order_ref="FOREIGN-PG", contract=foreign, side=OrderSide.BUY,
                                quantity=1, order_type=OrderType.MKT))
    phrase = cat.clear_phrase(e.id, e.kind)
    refused = S.clear_exception(h.coord, request_id="clr-pg-1", exc_id=e.id, kind=e.kind,
                                confirmation=phrase, note=NOTE, operator="tester")
    assert refused.status == "REFUSED" and refused.reasons[0].startswith("CONTRACT_NOT_CLEAN:")
    h.broker.submit(OrderIntent(order_ref="FOREIGN-PG2", contract=foreign, side=OrderSide.SELL,
                                quantity=1, order_type=OrderType.MKT))
    done = S.clear_exception(h.coord, request_id="clr-pg-2", exc_id=e.id, kind=e.kind,
                             confirmation=phrase, note=NOTE, operator="tester")
    assert done.status == "APPLIED" and h.coord.reconcile().clean
