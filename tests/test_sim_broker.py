"""SimBroker: deterministic, durable simulated broker behaviour (invented data only)."""

from __future__ import annotations

from decimal import Decimal

import pytest
from execution_harness import INSTRUMENT, START

from tradingbot.execution.broker import (
    AckLost,
    Broker,
    BrokerError,
    OrderIntent,
    OrderNotFound,
    OrderSide,
    OrderStatus,
    OrderType,
    QuoteQuality,
)
from tradingbot.execution.sim_broker import SimBroker, SimClock, SimulatedCrash
from tradingbot.models import AssetClass, Instrument


@pytest.fixture
def sim(tmp_path):
    clock = SimClock(START)
    b = SimBroker(tmp_path / "b.sqlite3", clock=clock.now)
    b.set_quote("ZZQ", "49.98", "50.02")
    return b


def bracket(b: SimBroker, ref="E1", qty=4, limit="50.50", stop="48", target="55"):
    c = b.resolve(INSTRUMENT)
    entry = OrderIntent(
        order_ref=ref,
        contract=c,
        side=OrderSide.BUY,
        quantity=qty,
        order_type=OrderType.LMT,
        limit_price=Decimal(limit),
    )
    kids = [
        OrderIntent(
            order_ref=f"{ref}.STP",
            contract=c,
            side=OrderSide.SELL,
            quantity=qty,
            order_type=OrderType.STP,
            stop_price=Decimal(stop),
            parent_ref=ref,
            reduce_only=True,
        )
    ]
    if target:
        kids.append(
            OrderIntent(
                order_ref=f"{ref}.TGT",
                contract=c,
                side=OrderSide.SELL,
                quantity=qty,
                order_type=OrderType.LMT,
                limit_price=Decimal(target),
                parent_ref=ref,
                reduce_only=True,
            )
        )
    return entry, kids


def net(b: SimBroker) -> int:
    return sum(p.quantity for p in b.positions())


def test_implements_protocol(sim):
    assert isinstance(sim, Broker)
    assert sim.account_id() == "SIM-PAPER-0001"


def test_bracket_fills_at_quote_and_activates_children_with_order_ref_echoed(sim):
    acks = sim.submit(*bracket(sim))
    assert [a.order_ref for a in acks] == ["E1", "E1.STP", "E1.TGT"]
    assert acks[0].status == OrderStatus.FILLED and acks[0].avg_fill_price == Decimal("50.02")
    working = {o.order_ref: o for o in sim.open_orders()}
    assert set(working) == {"E1.STP", "E1.TGT"}
    assert all(o.status == OrderStatus.SUBMITTED and o.quantity == 4 for o in working.values())
    assert working["E1.STP"].parent_ref == "E1"
    [fill] = sim.executions()
    assert fill.order_ref == "E1" and fill.quantity == 4 and fill.price == Decimal("50.02")
    assert net(sim) == 4


def test_resting_entry_keeps_children_presubmitted(sim):
    entry, kids = bracket(sim, limit="49.00")  # below the ask: not marketable
    sim.submit(entry, kids)
    states = {o.order_ref: o.status for o in sim.open_orders()}
    assert states == {
        "E1": OrderStatus.SUBMITTED,
        "E1.STP": OrderStatus.PRE_SUBMITTED,
        "E1.TGT": OrderStatus.PRE_SUBMITTED,
    }
    sim.set_quote("ZZQ", "48.90", "48.95")  # the limit becomes marketable
    assert {o.order_ref for o in sim.open_orders()} == {"E1.STP", "E1.TGT"}


def test_stop_triggers_on_later_quote_and_cancels_target(sim):
    sim.submit(*bracket(sim))
    sim.set_quote("ZZQ", "47.90", "47.95")
    assert sim.open_orders() == []
    stop = sim.orders_with_ref("E1.STP")[0]
    target = sim.orders_with_ref("E1.TGT")[0]
    assert stop.status == OrderStatus.FILLED and stop.avg_fill_price == Decimal("47.90")
    assert target.status == OrderStatus.CANCELLED and target.reason == "OCA_SIBLING_FILLED"
    assert net(sim) == 0


def test_target_triggers_and_cancels_stop(sim):
    sim.submit(*bracket(sim))
    sim.set_quote("ZZQ", "55.10", "55.20")
    assert sim.orders_with_ref("E1.TGT")[0].status == OrderStatus.FILLED
    assert sim.orders_with_ref("E1.STP")[0].status == OrderStatus.CANCELLED
    assert net(sim) == 0


def test_partial_fill_sizes_children_to_filled_quantity_then_grows(sim):
    sim.script_next_submit("PARTIAL", quantity=1)
    sim.submit(*bracket(sim))
    working = {o.order_ref: o for o in sim.open_orders()}
    assert (
        working["E1"].status == OrderStatus.PARTIALLY_FILLED and working["E1"].filled_quantity == 1
    )
    assert working["E1.STP"].quantity == 1 and working["E1.STP"].status == OrderStatus.SUBMITTED
    sim.fill("E1")
    working = {o.order_ref: o for o in sim.open_orders()}
    assert "E1" not in working and working["E1.STP"].quantity == 4
    assert net(sim) == 4


def test_rejection_leaves_nothing_working(sim):
    sim.script_next_submit("REJECT", reason="INVENTED_MARGIN_REASON")
    acks = sim.submit(*bracket(sim))
    assert {a.status for a in acks} == {OrderStatus.REJECTED}
    assert acks[0].reason == "INVENTED_MARGIN_REASON"
    assert sim.open_orders() == [] and sim.executions() == [] and net(sim) == 0


def test_lost_ack_variants(sim):
    sim.script_next_submit("LOST_ACK_PLACED")
    with pytest.raises(AckLost):
        sim.submit(*bracket(sim, ref="A"))
    assert sim.orders_with_ref("A")[0].status == OrderStatus.FILLED
    sim.script_next_submit("LOST_ACK_NOT_PLACED")
    with pytest.raises(AckLost):
        sim.submit(*bracket(sim, ref="B"))
    assert sim.orders_with_ref("B") == []
    sim.script_next_submit("CRASH_AFTER_PLACE")
    with pytest.raises(SimulatedCrash):
        sim.submit(*bracket(sim, ref="C"))
    assert sim.orders_with_ref("C")[0].status == OrderStatus.FILLED


def test_drop_stop_child_goes_inactive(sim):
    sim.script_next_submit("DROP_STOP_CHILD")
    sim.submit(*bracket(sim))
    assert sim.orders_with_ref("E1.STP")[0].status == OrderStatus.INACTIVE
    assert {o.order_ref for o in sim.open_orders()} == {"E1.TGT"}


def test_reduce_only_never_opens_or_reverses(sim):
    c = sim.resolve(INSTRUMENT)
    sell = OrderIntent(
        order_ref="R0",
        contract=c,
        side=OrderSide.SELL,
        quantity=3,
        order_type=OrderType.MKT,
        reduce_only=True,
    )
    acks = sim.submit(sell)
    assert acks[0].status == OrderStatus.CANCELLED and acks[0].reason == "REDUCE_ONLY_NO_POSITION"
    sim.submit(*bracket(sim, qty=2, target=None))
    sell5 = sell.model_copy(update={"order_ref": "R1", "quantity": 5})
    acks = sim.submit(sell5)
    assert acks[0].filled_quantity == 2 and net(sim) == 0


def test_modify_and_cancel(sim):
    sim.submit(*bracket(sim))
    o = sim.modify("E1.STP", stop_price=Decimal(49))
    assert o.stop_price == Decimal(49)
    o = sim.modify("E1.STP", quantity=2)
    assert o.quantity == 2
    assert sim.cancel("E1.TGT").status == OrderStatus.CANCELLED
    with pytest.raises(OrderNotFound):
        sim.cancel("E1.TGT")
    with pytest.raises(OrderNotFound):
        sim.modify("NOPE", stop_price=Decimal(1))


def test_cancelling_a_resting_entry_cancels_waiting_children(sim):
    sim.submit(*bracket(sim, limit="40"))
    sim.cancel("E1")
    assert sim.open_orders() == []
    assert sim.orders_with_ref("E1.STP")[0].reason == "PARENT_CANCELLED"


def test_quotes_unavailable_stale_and_frozen(sim):
    other = sim.resolve(Instrument(asset_class=AssetClass.ETF, symbol="QQZ"))
    assert sim.quote(other).quality == QuoteQuality.UNAVAILABLE
    sim.set_quote("QQZ", "10", "10.02", quality=QuoteQuality.FROZEN)
    assert sim.quote(other).quality == QuoteQuality.FROZEN
    sim.set_quote("ZZQ", None, None, quality=QuoteQuality.UNAVAILABLE)
    c = sim.resolve(INSTRUMENT)
    mkt = OrderIntent(
        order_ref="M", contract=c, side=OrderSide.BUY, quantity=1, order_type=OrderType.MKT
    )
    assert sim.submit(mkt)[0].status == OrderStatus.SUBMITTED  # cannot fill without a quote


def test_state_survives_restart(sim, tmp_path):
    sim.submit(*bracket(sim))
    again = SimBroker(tmp_path / "b.sqlite3", clock=sim.clock)
    assert {o.order_ref for o in again.open_orders()} == {"E1.STP", "E1.TGT"}
    assert net(again) == 4 and len(again.executions()) == 1


def test_options_do_not_resolve(sim):
    with pytest.raises(BrokerError):
        sim.resolve(Instrument(asset_class=AssetClass.OPTION, symbol="ZZQ"))


def test_order_intent_price_rules():
    c = SimBroker.resolve(None, INSTRUMENT)  # resolve does not touch instance state
    with pytest.raises(ValueError):
        OrderIntent(
            order_ref="x", contract=c, side=OrderSide.BUY, quantity=1, order_type=OrderType.LMT
        )
    with pytest.raises(ValueError):
        OrderIntent(
            order_ref="x", contract=c, side=OrderSide.BUY, quantity=1, order_type=OrderType.STP
        )
    with pytest.raises(ValueError):
        OrderIntent(
            order_ref="x",
            contract=c,
            side=OrderSide.BUY,
            quantity=1,
            order_type=OrderType.MKT,
            limit_price=Decimal(1),
        )
