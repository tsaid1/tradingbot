"""Part B of round 2: one execution core for every instrument, keyed by contract.

An invented futures-like contract (multiplier 50, tick 0.25, two contract months) beside an
invented stock that shares its symbol. Invented data only; no network, no broker.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from execution_harness import Harness, action, open_action

from tradingbot.execution.broker import (
    BrokerError,
    ContractRef,
    OrderIntent,
    OrderSide,
    OrderType,
)
from tradingbot.execution.store import IntentState
from tradingbot.models import Action, AssetClass, Instrument, MockPolicy, PositionSide

FUT = "ZQF"  # invented
MAR = ContractRef(
    symbol=FUT, asset_class=AssetClass.FUTURE, sec_type="FUT", exchange="SIMEX", con_id=9001,
    expiry="202703", multiplier=Decimal(50), min_tick=Decimal("0.25"), local_symbol="ZQFH7",
)
JUN = MAR.model_copy(update={"con_id": 9002, "expiry": "202706", "local_symbol": "ZQFM7"})


def future(month: str) -> Instrument:
    return Instrument(asset_class=AssetClass.FUTURE, symbol=FUT, futures_contract=month)


def fut_open(action_id: str, month: str, side=PositionSide.LONG, **kw):
    a = open_action(action_id, entry="100", stop="97" if side == PositionSide.LONG else "103",
                    target="110" if side == PositionSide.LONG else "90", side=side, **kw)
    return a.model_copy(update={"instrument": future(month)})


def harness(tmp_path, **policy) -> Harness:
    h = Harness(
        tmp_path,
        policy=MockPolicy(
            quantity=1,
            quantity_per_add=1,
            max_notional_usd=Decimal(policy.get("notional", 6000)),
            max_instrument_exposure_usd=Decimal(policy.get("instrument", 6000)),
            max_aggregate_exposure_usd=Decimal(policy.get("aggregate", 20000)),
        ),
        # Futures and short sales need an owner's [risk] table: an INVENTED one (docs/RISK_GATES.md).
        risk_families=((FUT, "FUT", "50"), ("ZZQ", "STK", "1"), (FUT, "STK", "1")),
    )
    for c in (MAR, JUN):
        h.broker.register_contract(c)
        h.broker.set_quote(FUT, "99.75", "100.00", contract=c)
    return h


def test_contract_defaults_keep_a_stock_unchanged():
    stock = ContractRef(symbol="ZZQ", asset_class=AssetClass.EQUITY, con_id=1)
    assert stock.multiplier == 1 and stock.min_tick == Decimal("0.01")
    assert stock.size_increment == 1 and stock.expiry is None
    assert stock.contract_key != MAR.contract_key != JUN.contract_key


def test_exposure_uses_the_multiplier(tmp_path):
    h = harness(tmp_path)
    out = h.execute("p1", fut_open("open", "202703"))
    assert out.state == IntentState.FILLED
    per, _ = h.coord._exposure(MAR)
    assert per == Decimal("99.875") * 50  # one contract at the mid, times 50
    again = h.execute("p2", fut_open("open", "202703"))  # 5,000 + 5,050 > 6,000
    assert again.state == IntentState.REFUSED
    assert again.reasons[0].startswith("INSTRUMENT_EXPOSURE_CAP")


def test_notional_of_one_contract_counts_the_multiplier(tmp_path):
    h = harness(tmp_path, notional=4000, instrument=100000)
    out = h.execute("p1", fut_open("open", "202703"))  # 1 x 100 x 50 = 5,000 > 4,000
    assert out.state == IntentState.REFUSED and out.reasons[0].startswith("NOTIONAL_LIMIT")


def test_an_off_tick_stop_is_refused_and_limits_are_snapped_to_the_tick(tmp_path):
    h = harness(tmp_path)
    bad = fut_open("open", "202703").model_copy(update={"stop_price": "97.10"})
    out = h.execute("p1", bad)
    assert out.state == IntentState.REFUSED and out.reasons == ["STOP_PRICE_OFF_TICK"]
    assert h.broker.submit_calls == 0
    ok = h.execute("p2", fut_open("open", "202703").model_copy(update={"entry_reference": "99.9"}))
    [entry] = h.broker.orders_with_ref(ok.order_ref)
    # The stated entry itself (owner, 12 Nov 2026: the collar never prices the order), snapped
    # down to the tick: 99.9 -> 99.75 (was 99.9 x 1.01 = 100.899 -> 100.75).
    assert entry.limit_price % Decimal("0.25") == 0
    assert entry.limit_price == Decimal("99.75")


def test_an_off_tick_amendment_is_refused(tmp_path):
    h = harness(tmp_path)
    h.execute("p1", fut_open("open", "202703"))
    mod = action("raise", Action.MODIFY, idea="p1:open", stop="98.30")
    mod = mod.model_copy(update={"instrument": future("202703")})
    out = h.execute("p2", mod)
    assert out.state == IntentState.REFUSED and out.reasons == ["STOP_PRICE_OFF_TICK"]


def test_the_sim_broker_refuses_an_off_tick_or_off_increment_order(tmp_path):
    h = harness(tmp_path)
    with pytest.raises(BrokerError, match="tick"):
        h.broker.submit(OrderIntent(order_ref="X1", contract=MAR, side=OrderSide.BUY, quantity=1,
                                    order_type=OrderType.LMT, limit_price=Decimal("100.10")))
    lot = MAR.model_copy(update={"size_increment": 2})
    with pytest.raises(BrokerError, match="increment"):
        h.broker.submit(OrderIntent(order_ref="X2", contract=lot, side=OrderSide.BUY, quantity=3,
                                    order_type=OrderType.LMT, limit_price=Decimal("100.00")))


def test_two_contract_months_are_tracked_capped_and_reconciled_separately(tmp_path):
    h = harness(tmp_path)
    mar = h.execute("p1", fut_open("open", "202703"))
    jun = h.execute("p2", fut_open("open", "202706"))
    assert mar.state == jun.state == IntentState.FILLED  # each month has its own 6,000 cap
    held = {p.contract.contract_key: p.quantity for p in h.broker.positions()}
    assert held == {MAR.contract_key: 1, JUN.contract_key: 1}
    assert {p.contract_key for p in h.coord.ledger()} == {MAR.contract_key, JUN.contract_key}
    assert h.coord.reconcile().clean
    h.broker.submit(OrderIntent(order_ref="FOREIGN-1", contract=JUN, side=OrderSide.BUY,
                                quantity=1, order_type=OrderType.MKT))
    report = h.coord.reconcile()
    mismatched = [d for d in report.discrepancies if d.kind.value == "QUANTITY_MISMATCH"]
    assert [d.contract_key for d in mismatched] == [JUN.contract_key]
    close = h.execute("p3", action("close", Action.CLOSE, idea="p1:open").model_copy(
        update={"instrument": future("202703")}))
    assert close.state == IntentState.FILLED  # the March exit sees only the March position
    assert {p.contract.contract_key: p.quantity for p in h.broker.positions()} == {
        JUN.contract_key: 2
    }


def test_a_stock_and_a_future_sharing_a_symbol_do_not_net(tmp_path):
    h = harness(tmp_path, notional=20000, instrument=20000, aggregate=50000)
    h.broker.set_quote(FUT, "49.98", "50.02")  # the stock's own quote (by symbol)
    stock = h.execute("p1", open_action(symbol=FUT))  # long 1 share
    short = h.execute("p2", fut_open("open", "202703", side=PositionSide.SHORT).model_copy(
        update={"entry_reference": "99.75"}))  # at the bid: an explicit sell limit is its price
    assert stock.state == short.state == IntentState.FILLED
    held = {p.contract.contract_key: p.quantity for p in h.broker.positions()}
    assert held[MAR.contract_key] == -1 and sum(held.values()) == 0
    assert len(held) == 2  # two instruments, never netted to zero
    assert h.coord.reconcile().clean
    assert stock.protection == "CONFIRMED" and short.protection == "CONFIRMED"


def test_the_dashboard_view_shows_contract_identity_and_multiplier(tmp_path):
    from tradingbot.runtime.lifecycle import contract_label

    assert contract_label(MAR.model_dump(mode="json")) == "ZQF FUT 202703 x50 (9001)"
    stock = ContractRef(symbol="ZZQ", asset_class=AssetClass.EQUITY, con_id=1)
    assert contract_label(stock.model_dump(mode="json")) == "ZZQ STK x1 (1)"
