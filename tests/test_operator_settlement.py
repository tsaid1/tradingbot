"""Operator settlement of unresolved state (execution.settlement). SimBroker, invented data only.

A settlement only records what a human established at the broker: it never sends, amends or
cancels an order, it is refused when the broker contradicts it, and the instrument's block lifts
only after a full successful sync and reconciliation show the contract clean.
"""

from __future__ import annotations

import pytest
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
        confirmation=confirm if confirm is not None else cat.settle_phrase(ref, outcome),
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
    assert open_kinds(h) == []  # outcome exception ended, verification lifted at once (clean)
    assert result.lifted and result.before["intent"]["state"] == "UNRESOLVED"
    assert result.after["intent"]["state"] == "CANCELLED"
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
    real_sync = h.coord.sync
    state = {"fail": 2}

    def sync(**kw):
        # The settlement's own reads succeed; the broker is unreadable right after it.
        if state["fail"] and any(e.event == "OPERATOR_SETTLEMENT" for e in h.coord.store.journal()):
            state["fail"] -= 1
            return False
        return real_sync(**kw)

    monkeypatch.setattr(h.coord, "sync", sync)
    result = settle(h, ref, "NOT_AT_BROKER")
    assert result.status == "APPLIED" and result.lifted == []
    assert open_kinds(h) == [C.OPERATOR_SETTLEMENT_UNVERIFIED]
    blocked = second_open_state(h, "p2")
    assert blocked.reasons == [f"INSTRUMENT_BLOCKED_{C.OPERATOR_SETTLEMENT_UNVERIFIED}"]
    assert S.lift_verified_blocks(h.coord) != []
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
    }
    assert opened <= kinds
    assert opened == set(cat.EXCEPTION_ACTIONS)
    # Every blocking kind is resolved by evidence or by a verified clear, never by a declaration.
    for kind in (*C.INSTRUMENT_BLOCKING_KINDS, *C.ACCOUNT_BLOCKING_KINDS):
        assert cat.action_for(kind)[0] in (cat.EVIDENCE, cat.CLEAR, cat.SETTLE), kind
        assert cat.action_for(kind)[0] != cat.ACKNOWLEDGE
