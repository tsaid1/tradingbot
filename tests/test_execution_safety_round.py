"""Safety review round (EXECUTION_CORE_safety_review.md): one reproducing test per finding.

Invented tickers, toy prices and invented wording only. No network, no broker port.
"""

from __future__ import annotations

import os
import subprocess
import sys
from decimal import ROUND_CEILING, Decimal
from pathlib import Path

import pytest
from execution_harness import SYMBOL, Harness, action, open_action, source

from tradingbot.event_models import ParsedPost
from tradingbot.execution.broker import (
    TERMINAL_STATUSES,
    WORKING_STATUSES,
    OrderIntent,
    OrderNotFound,
    OrderSide,
    OrderStatus,
    OrderType,
    QuoteQuality,
)
from tradingbot.execution.coordinator import (
    PROTECTION_UNVERIFIED,
    ExecutionCoordinator,
    ExecutionStoreLocked,
    _intent_state,
)
from tradingbot.execution.sim_broker import SimBroker
from tradingbot.execution.store import IntentState
from tradingbot.models import Action, MockPolicy

# ----------------------------------------------------------------------------------------- N1


def test_n1_pending_cancel_is_working_and_api_cancelled_is_terminal(tmp_path):
    assert OrderStatus("PendingCancel") in WORKING_STATUSES
    assert OrderStatus("ApiCancelled") in TERMINAL_STATUSES
    assert _intent_state(OrderStatus("ApiCancelled"), 0, 4) == IntentState.CANCELLED
    assert _intent_state(OrderStatus("PendingCancel"), 1, 4) == IntentState.WORKING
    h = Harness(tmp_path)
    out = h.execute("p1", open_action())
    stop_ref = f"{out.order_ref}.STP"
    real = h.broker.open_orders

    def pending_cancel_view():
        return [
            o.model_copy(update={"status": OrderStatus("PendingCancel")})
            if o.order_ref == stop_ref
            else o
            for o in real()
        ]

    h.broker.open_orders = pending_cancel_view
    h.coord.sync()
    # Still working at the broker: the row is not concluded Cancelled while the cancel is pending.
    assert h.coord.store.order(stop_ref).status == "PendingCancel"


# ----------------------------------------------------------------------------------------- M1


def test_m1_protection_is_not_confirmed_when_the_post_fill_check_failed(tmp_path):
    h = Harness(tmp_path, strict=True)
    opened = h.execute("p1", open_action())
    idea = "p1:open"
    assert opened.protection == "CONFIRMED"
    real = h.broker.open_orders

    def failing_after_the_add_fill():
        # The ADD fill has been applied (4 -> 6): every later open-orders read fails.
        if h.coord.store.position(idea).quantity == 6:
            from tradingbot.execution.broker import BrokerError

            raise BrokerError("invented: open orders unavailable")
        return real()

    h.broker.open_orders = failing_after_the_add_fill
    add = h.execute("p2", action("add", Action.ADD, idea=idea, entry="50"))
    assert add.state == IntentState.FILLED and h.coord.store.position(idea).quantity == 6
    assert add.protection == "UNKNOWN" and add.replay_status == "EXCEPTION"
    assert PROTECTION_UNVERIFIED in [e.kind for e in h.coord.store.exceptions(open_only=True)]
    h.broker.open_orders = real
    h.coord.sync()  # a successful check after the last fill: resized and confirmed
    assert h.coord._outcome(add.order_ref).protection == "CONFIRMED"
    assert h.working()[f"{opened.order_ref}.STP"].remaining == 6
    assert h.coord.store.exceptions(open_only=True) == []


# ----------------------------------------------------------------------------------------- M2


def _capped(tmp_path, cap: int = 250) -> Harness:
    return Harness(
        tmp_path,
        strict=True,
        policy=MockPolicy(
            quantity=4, max_notional_usd=Decimal(1000), max_aggregate_exposure_usd=Decimal(cap)
        ),
    )


def test_m2_an_entry_whose_ack_was_lost_still_counts_against_the_caps(tmp_path):
    h = _capped(tmp_path)
    h.broker.script_next_submit("LOST_ACK_PLACED_RESTING")  # live at the broker, ack lost
    h.broker.lookup_failures = 2  # the pre-trade sync and the follow-up lookup both fail
    first = h.execute("p1", open_action())
    assert first.state == IntentState.UNRESOLVED
    h.broker.set_quote("QQZ", "19.99", "20.01")
    second = h.execute("p2", open_action(symbol="QQZ", entry="20", stop="19", target="22"))
    assert second.state == IntentState.REFUSED
    assert second.reasons[0].startswith("AGGREGATE_EXPOSURE_CAP")


def test_m2_a_recorded_entry_counts_and_does_not_count_against_its_own_resend(tmp_path):
    h = _capped(tmp_path)
    h.broker.script_next_submit("LOST_ACK_NOT_PLACED")
    h.broker.lookup_failures = 2
    first = h.execute("p1", open_action())
    assert first.state == IntentState.UNRESOLVED
    _, total = h.coord._exposure("ZZQ")
    assert total == Decimal("50.50") * 4  # the full intended quantity at its limit
    h.clock.advance(h.config.settle_seconds + 1)
    h.coord.recover()  # no trace: the resend is not refused by its own exposure
    assert h.coord.intent(first.order_ref).state == IntentState.FILLED


# ----------------------------------------------------------------------------------------- M3


def test_m3_a_refused_close_stops_the_group_and_holds_the_open(tmp_path):
    h = Harness(tmp_path, strict=True)
    h.post("p1", open_action())  # idea p1:open, 4 held
    foreign = OrderIntent(
        order_ref="FOREIGN-1",
        contract=h.broker.resolve(open_action().instrument),
        side=OrderSide.SELL,
        quantity=3,
        order_type=OrderType.MKT,
    )
    h.broker.submit(foreign)  # someone else trades the account: the CLOSE will be refused
    h.broker.set_quote("QQZ", "19.99", "20.01")
    quotes = {**h.quotes(), **h.quotes("QQZ")}
    calls = h.broker.submit_calls
    result = h.replay.process(
        source("p2", at=h.clock.now()),
        ParsedPost(
            actions=[
                action("close", Action.CLOSE, idea="p1:open", group="swap"),
                open_action(
                    "open-b", symbol="QQZ", entry="20", stop="19", target="22", group="swap"
                ),
            ]
        ),
        quotes,
        now=h.clock.now(),
    )
    assert [o.status for o in result.outcomes] == ["REFUSED", "HOLD"]
    assert "PARTIAL_GROUP" in result.outcomes[1].reasons
    assert h.broker.submit_calls == calls  # OPEN B never reached the broker
    assert h.coord.store.position("p2:open-b") is None
    assert "PARTIAL_GROUP" in [e.kind for e in h.coord.store.exceptions(open_only=True)]
    report = h.coord.reconcile()
    assert "PARTIAL_GROUP" in [d.kind.value for d in report.discrepancies]
    assert not report.clean


# ----------------------------------------------------------------------------------------- R2


def test_r2_a_reversing_fill_is_recorded_and_blocks_only_that_instrument(tmp_path):
    h = Harness(tmp_path, strict=True)
    opened = h.execute("p1", open_action())
    stop = f"{opened.order_ref}.STP"
    h.broker.modify(stop, quantity=9)  # enlarged outside the bot (e.g. by hand at the broker)
    h.broker.open_orders()  # the pending amendment takes effect
    h.broker.set_quote(SYMBOL, "47.90", "47.95")  # the stop sells 9 against 4 held
    assert sum(p.quantity for p in h.broker.positions()) == -5
    assert h.coord.sync() is True  # no RuntimeError, and again on the next call
    assert h.coord.sync() is True
    [fill] = [f for f in h.coord.store.fills() if f["order_ref"] == stop]
    assert fill["quantity"] == 9  # recorded as it happened
    pos = h.coord.store.position("p1:open")
    assert pos.quantity == 0 and pos.status == "REVERSED"
    [exc] = [e for e in h.coord.store.exceptions(open_only=True) if e.kind == "LEDGER_REVERSAL"]
    assert exc.idea_id == "p1:open" and "5" in exc.detail
    h.broker.set_quote(SYMBOL, "49.98", "50.02")
    blocked = h.execute("p2", open_action())
    assert blocked.state == IntentState.REFUSED
    assert blocked.reasons == ["INSTRUMENT_BLOCKED_LEDGER_REVERSAL"]
    h.broker.set_quote("QQZ", "19.99", "20.01")
    other = h.execute("p3", open_action(symbol="QQZ", entry="20", stop="19", target="22"))
    assert other.state == IntentState.FILLED  # everything else keeps working
    r = h.replay.process(
        source("p4", at=h.clock.now()),
        ParsedPost(actions=[open_action("again", symbol="QQZ", entry="20", stop="19")]),
        h.quotes("QQZ"),
        now=h.clock.now(),
    )
    assert [o.status for o in r.outcomes] == ["EXECUTED"]
    assert "UNEXPLAINED_BROKER_POSITION" in [d.kind.value for d in h.coord.reconcile().discrepancies]


# ----------------------------------------------------------------------------------------- R5

_HOLDER = """
import sys
from tradingbot.execution.coordinator import ExecutionCoordinator
from tradingbot.execution.sim_broker import SimBroker
folder = sys.argv[1]
try:
    coord = ExecutionCoordinator(SimBroker(folder + "/b2.sqlite3"), folder + "/exec.sqlite3")
except Exception as exc:
    print("REFUSED", type(exc).__name__, exc, flush=True)
    sys.exit(3)
print("HOLDING", flush=True)
sys.stdin.readline()
"""


def _child(folder: Path) -> subprocess.Popen:
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
    return subprocess.Popen(
        [sys.executable, "-c", _HOLDER, str(folder)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        env=env,
    )


def test_r5_a_second_coordinator_on_the_same_store_refuses_to_start(tmp_path):
    holder = _child(tmp_path)
    try:
        assert holder.stdout.readline().strip() == "HOLDING"
        # This process cannot start a coordinator on the store the other process holds ...
        with pytest.raises(ExecutionStoreLocked, match="in use by another"):
            ExecutionCoordinator(SimBroker(tmp_path / "b.sqlite3"), tmp_path / "exec.sqlite3")
        # ... nor can a third process.
        third = _child(tmp_path)
        out, _ = third.communicate(timeout=30)
        assert third.returncode == 3 and out.startswith("REFUSED ExecutionStoreLocked")
    finally:
        holder.communicate("\n", timeout=30)
    # Once the holder has gone, the store can be taken; and this process then keeps it.
    mine = ExecutionCoordinator(SimBroker(tmp_path / "b.sqlite3"), tmp_path / "exec.sqlite3")
    late = _child(tmp_path)
    out, _ = late.communicate(timeout=30)
    assert late.returncode == 3 and "in use by another" in out
    mine.close()


# ------------------------------------------------------------------------------------ R1 / R6


def _exit_commitment(h: Harness, idea: str = "p1:open") -> int:
    """Working exit quantity at the broker for one long idea: resting exits plus the protective
    pair (one-cancels-other siblings count once, at the larger remaining)."""
    pos = h.coord.store.position(idea)
    working = h.working()
    children = [working[r].remaining for r in (pos.stop_ref, pos.target_ref) if r in working]
    exits = sum(
        working[r.order_ref].remaining
        for r in h.coord.store.orders()
        if r.idea_id == idea and r.role.value == "EXIT" and r.order_ref in working
    )
    return exits + max(children, default=0)


def test_r1_a_resting_reduce_is_netted_against_the_protective_children(tmp_path):
    h = Harness(tmp_path, strict=True)
    opened = h.execute("p1", open_action())
    h.broker.script_next_submit("REST")  # the reduction rests (e.g. outside regular hours)
    trim = h.execute("p2", action("trim", Action.REDUCE, idea="p1:open", fraction="0.5"))
    assert trim.state == IntentState.WORKING
    assert _exit_commitment(h) <= 4  # was 6: STP 4 + REDUCE 2 against 4 held
    events = h.events()
    assert events.index("PROTECTIVE_RESIZED") < max(
        i for i, e in enumerate(events) if e == "SUBMIT"
    )
    h.broker.fill(trim.order_ref)
    h.broker.set_quote(SYMBOL, "47.90", "47.95")  # the stop fires before any sync
    assert sum(p.quantity for p in h.broker.positions()) == 0  # flat, never short
    h.coord.sync()
    assert h.coord.store.position("p1:open").status == "CLOSED"
    assert not [e for e in h.coord.store.exceptions(open_only=True)]
    assert opened.order_ref


def test_r1_a_close_after_a_resting_reduce_cannot_leave_the_account_short(tmp_path):
    h = Harness(tmp_path, strict=True)
    h.execute("p1", open_action())
    h.broker.script_next_submit("REST")
    trim = h.execute("p2", action("trim", Action.REDUCE, idea="p1:open", fraction="0.5"))
    close = h.execute("p3", action("close", Action.CLOSE, idea="p1:open"))
    assert close.state == IntentState.FILLED and close.filled_quantity == 4
    assert h.working() == {}
    with pytest.raises(OrderNotFound):  # the resting reduction was cancelled before the close
        h.broker.fill(trim.order_ref)
    h.broker.set_quote(SYMBOL, "47.90", "47.95")
    assert h.broker.positions() == []
    assert h.coord.reconcile().clean


def test_r1_an_unconfirmed_resize_means_no_exit_is_sent(tmp_path):
    h = Harness(tmp_path, strict=True)
    h.execute("p1", open_action())
    h.broker.pending_seconds = 30  # cancels and amendments do not take effect in time
    calls = h.broker.submit_calls
    trim = h.execute("p2", action("trim", Action.REDUCE, idea="p1:open", fraction="0.5"))
    assert trim.state == IntentState.REFUSED and trim.reasons[0] == "EXIT_NOT_NETTED"
    assert h.broker.submit_calls == calls
    assert "EXIT_NOT_NETTED" in [e.kind for e in h.coord.store.exceptions(open_only=True)]


def test_r1_a_cancel_that_loses_the_race_to_the_stop_sends_no_close(tmp_path):
    h = Harness(tmp_path, strict=True)
    h.execute("p1", open_action())
    h.broker.script_next_cancel("FILL_FIRST")  # the stop fills before its cancel lands
    calls = h.broker.submit_calls
    close = h.execute("p2", action("close", Action.CLOSE, idea="p1:open"))
    assert close.state == IntentState.REFUSED and h.broker.submit_calls == calls
    assert h.broker.positions() == []  # flat through the stop, not short through a close
    assert h.coord.store.position("p1:open").status == "CLOSED"


def test_r6_a_close_cancels_the_partly_filled_entry_first(tmp_path):
    h = Harness(tmp_path, strict=True)
    h.broker.script_next_submit("PARTIAL", quantity=1)
    opened = h.execute("p1", open_action())
    assert opened.state == IntentState.WORKING and opened.filled_quantity == 1
    close = h.execute("p2", action("close", Action.CLOSE, idea="p1:open"))
    assert close.state == IntentState.FILLED and close.filled_quantity == 1
    assert opened.order_ref not in h.working()
    with pytest.raises(OrderNotFound):  # the other 3 can no longer fill and reopen it
        h.broker.fill(opened.order_ref)
    h.coord.sync()
    assert h.coord.intent(opened.order_ref).state == IntentState.CANCELLED
    assert h.broker.positions() == [] and h.coord.reconcile().clean


def test_r6_reconcile_flags_a_working_entry_of_a_closed_idea(tmp_path):
    h = Harness(tmp_path, strict=True)
    h.broker.script_next_submit("PARTIAL", quantity=1)
    opened = h.execute("p1", open_action())
    h.broker.set_quote(SYMBOL, "47.90", "47.95")  # the stop closes the 1 held
    h.coord.sync()
    assert h.coord.store.position("p1:open").status == "CLOSED"
    kinds = [d.kind.value for d in h.coord.reconcile().discrepancies]
    assert "WORKING_ENTRY_ON_CLOSED_IDEA" in kinds and not h.coord.reconcile().clean
    assert opened.order_ref in h.working()


# ----------------------------------------------------------------------------------------- R7


def test_r7_halt_cancels_working_entries_and_still_resizes_the_stop(tmp_path):
    h = Harness(tmp_path, strict=True)
    opened = h.execute("p1", open_action())
    stop = f"{opened.order_ref}.STP"
    h.broker.script_next_submit("REST")
    add = h.execute("p2", action("add", Action.ADD, idea="p1:open", entry="50"))
    assert add.state == IntentState.WORKING
    h.halt()
    h.broker.script_next_cancel("FILL_FIRST")  # the add fills before the halt's cancel lands
    h.coord.sync()
    assert add.order_ref not in h.working()  # no entry keeps working under the halt
    assert h.coord.store.position("p1:open").quantity == 6
    assert h.working()[stop].remaining == 6  # the protective resize is not blocked by the halt
    assert h.coord._outcome(add.order_ref).protection == "CONFIRMED"
    # Anything that would add exposure stays blocked.
    calls = h.broker.submit_calls
    more = h.execute("p3", action("add2", Action.ADD, idea="p1:open", entry="50"))
    assert more.state == IntentState.HALTED and h.broker.submit_calls == calls


def test_r7_halt_cancels_a_resting_entry_without_a_fill(tmp_path):
    h = Harness(tmp_path, strict=True)
    h.broker.script_next_submit("PARTIAL", quantity=1)
    opened = h.execute("p1", open_action())
    h.halt()
    h.coord.sync()
    assert opened.order_ref not in h.working()
    assert h.coord.intent(opened.order_ref).state == IntentState.CANCELLED
    assert h.working()[f"{opened.order_ref}.STP"].remaining == 1
    assert "HALT_ENTRY_CANCELLED" in h.events(opened.order_ref)


# ----------------------------------------------------------------------------------------- S1


def test_s1_an_explained_unprotected_position_is_never_clean(tmp_path):
    h = Harness(tmp_path, strict=True)
    h.broker.script_next_submit("DROP_STOP_CHILD")
    h.execute("p1", open_action())
    report = h.coord.reconcile()
    [d] = [d for d in report.discrepancies if d.kind.value == "POSITION_WITHOUT_PROTECTIVE_STOP"]
    assert d.explained_by is not None  # the open UNPROTECTED_POSITION exception covers it ...
    assert not report.clean  # ... but a position without confirmed protection is never clean


def _stop_view(h: Harness, ref: str, **update):
    real = h.broker.open_orders

    def view():
        return [o.model_copy(update=update) if o.order_ref == ref else o for o in real()]

    h.broker.open_orders = view


@pytest.mark.parametrize(
    "update",
    [{"side": OrderSide.BUY}, {"order_type": OrderType.LMT, "limit_price": Decimal(48)}],
)
def test_s1_a_stop_of_the_wrong_side_or_type_is_flagged(tmp_path, update):
    h = Harness(tmp_path, strict=True)
    out = h.execute("p1", open_action())
    _stop_view(h, f"{out.order_ref}.STP", **update)
    report = h.coord.reconcile()
    assert "PROTECTIVE_STOP_INVALID" in [d.kind.value for d in report.discrepancies]
    assert not report.clean


def test_s1_a_stop_on_the_wrong_side_of_the_average_entry_is_flagged(tmp_path):
    h = Harness(tmp_path, strict=True)
    out = h.execute("p1", open_action())  # long 4 at 50.02, stop 48
    h.broker.set_quote(SYMBOL, "52.98", "53.02")
    h.broker.modify(f"{out.order_ref}.STP", stop_price=Decimal(51))  # changed at the broker
    report = h.coord.reconcile()
    [d] = [d for d in report.discrepancies if d.kind.value == "PROTECTIVE_STOP_INVALID"]
    assert "51" in d.detail and not report.clean


# ----------------------------------------------------------------------------------------- S2


def test_s2_an_exit_is_a_marketable_limit_priced_from_a_fresh_quote(tmp_path):
    h = Harness(tmp_path, strict=True)
    h.execute("p1", open_action())
    close = h.execute("p2", action("close", Action.CLOSE, idea="p1:open"))
    [order] = h.broker.orders_with_ref(close.order_ref)
    band = h.policy.max_price_deviation_pct / Decimal(100)
    assert order.order_type == OrderType.LMT
    assert order.limit_price == (Decimal("49.98") * (1 - band)).quantize(
        Decimal("0.01"), ROUND_CEILING
    )
    assert close.state == IntentState.FILLED and order.avg_fill_price == Decimal("49.98")


@pytest.mark.parametrize(
    "quote",
    [
        {"bid": None, "ask": None, "quality": QuoteQuality.UNAVAILABLE},
        {"bid": "50.10", "ask": "50.00"},
        {"bid": "49.98", "ask": "50.02", "quality": QuoteQuality.DELAYED},
    ],
)
def test_s2_no_usable_quote_means_no_exit_and_an_exception(tmp_path, quote):
    h = Harness(tmp_path, strict=True)
    opened = h.execute("p1", open_action())
    h.broker.set_quote(SYMBOL, **quote)
    calls = h.broker.submit_calls
    trim = h.execute("p2", action("trim", Action.REDUCE, idea="p1:open", fraction="0.5"))
    assert trim.state == IntentState.REFUSED and trim.reasons[0] == "EXIT_WITHOUT_QUOTE"
    assert h.broker.submit_calls == calls
    assert "EXIT_WITHOUT_QUOTE" in [e.kind for e in h.coord.store.exceptions(open_only=True)]
    assert h.working()[f"{opened.order_ref}.STP"].remaining == 4  # the broker stop stays
