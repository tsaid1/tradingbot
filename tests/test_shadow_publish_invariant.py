"""The shadow broker's publish invariant, pinned at the function that publishes a read (SDK-free).

For ANY sequence of reads, cancels and submits, an opening (not reduce-only) simulated order is
never filled on a read that fails the opening gate (``shadow_quotes.opening_problem``): not on a
plain read, not on the read a cancel call makes (the order the core is cancelling included), not
on the read a submit makes. The read-only session is a duck-typed double fed with snapshots built
exactly as the adapter builds them (``test_shadow_quote_mapping.snap``). Invented prices."""
from __future__ import annotations

import random
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from test_shadow_quote_mapping import snap

from tradingbot.execution.broker import OrderIntent, OrderNotFound, OrderSide, OrderType
from tradingbot.execution.shadow_broker import ShadowBroker
from tradingbot.execution.shadow_quotes import map_snapshot, opening_problem
from tradingbot.execution.sim_broker import SimBroker, SimClock
from tradingbot.ibkr.models import ResolvedContract
from tradingbot.models import AssetClass, Instrument

START = datetime(2026, 6, 1, 14, 0, tzinfo=UTC)
LIMIT = 3.0


class _State:
    value = "READY"


class FakeStream:
    def __init__(self, session):
        self.session, self.closed_reason = session, None

    @property
    def closed(self):
        return self.closed_reason is not None

    def latest(self):
        return self.session.snapshot

    def close(self):
        self.closed_reason = "CLOSED"
        return self.session.snapshot


class FakeSession:
    """The read-only session's surface the shadow broker uses; nothing here can send anything."""

    masked_account = "DU***0001"
    state = _State()
    poisoned = False
    max_quote_streams = 4

    def __init__(self):
        self.snapshot = snap(bid=88.30, ask=88.32)

    def resolve_contract(self, query):
        return ResolvedContract(con_id=4711, symbol=query.symbol, sec_type="STK", currency="USD",
                                exchange="SMART", primary_exchange="", local_symbol=query.symbol,
                                min_tick=0.01, time_zone_id="", trading_hours="",
                                liquid_hours="")

    def open_quote_stream(self, rc, market_data_type=3):
        return FakeStream(self)

    def disconnect(self):
        pass


def _broker(tmp_path):
    clock = SimClock(START)
    sim = SimBroker(tmp_path / "sim.sqlite3", account="SIM-SHADOW-0001", clock=clock.now)
    b = ShadowBroker(sim, FakeSession(), connect_session=FakeSession, clock=clock.now)
    b.opening_limit = lambda ref: LIMIT
    contract = b.resolve(Instrument(asset_class=AssetClass.ETF, symbol="QBND"))
    return b, sim, clock, contract


KINDS = {
    "live": {},
    "delayed": {"mdt": 3, "delayed": True, "live": False, "bid_class": "DELAYED",
                "ask_class": "DELAYED"},
    "frozen": {"mdt": 2},
    "ask_stale": {"ask_age": 4.0},
    "bid_stale": {"bid_age": 3.5},
    "unreported_expired": {"mdt": None, "ask_pc_age": 6.0},
    "unavailable": {"failed": True},
}


def test_property_no_opening_fill_ever_comes_from_a_read_that_fails_the_gate(tmp_path):
    rng = random.Random(69)
    b, sim, clock, contract = _broker(tmp_path)
    session = b._session
    n_fills = n_cancel_paths = 0
    for step in range(1500):
        kind = rng.choice(list(KINDS))
        ask = rng.choice([88.30, 88.32, 88.34, 88.36, 88.40])
        session.snapshot = snap(bid=round(ask - 0.02, 2), ask=ask, **KINDS[kind])
        working = [o for o in sim.open_orders() if o.parent_ref is None and not o.reduce_only]
        op = rng.random()
        before = {f.exec_id for f in sim.executions()}
        if not working or op < 0.3:
            ref = f"TB-{step}"
            entry = OrderIntent(order_ref=ref, contract=contract, side=OrderSide.BUY, quantity=1,
                                order_type=OrderType.LMT, limit_price=Decimal("88.35"))
            stop = OrderIntent(order_ref=ref + ".STP", contract=contract, side=OrderSide.SELL,
                               quantity=1, order_type=OrderType.STP,
                               stop_price=Decimal("80.00"), parent_ref=ref, reduce_only=True)
            b.submit(entry, [stop])
        elif op < 0.6:
            n_cancel_paths += 1
            try:  # the execution core cancels its resting entry on this very read
                out = b.cancel(working[0].order_ref)
            except OrderNotFound:  # filled or gone before: the core treats it as confirmed
                out = None
            assert out is None or out.status.value in ("Cancelled", "PendingCancel", "Filled")
        else:
            b.quote(contract)
        gate = opening_problem(*_mapped(session.snapshot, contract, b.last_evidence[
            contract.contract_key].read_at), LIMIT)
        new = [f for f in sim.executions() if f.exec_id not in before and f.side.value == "BUY"]
        if gate is not None:
            assert new == [], (step, kind, gate, new)
        n_fills += len(new)
        clock.advance(1.0)
        for f in list(sim.positions()):  # flatten so later steps see fresh openings
            sim.set_quote(f.contract.symbol, Decimal("88.30"), Decimal("88.32"),
                          contract=f.contract)
            sim.submit(OrderIntent(order_ref=f"FLAT-{step}", contract=f.contract,
                                   side=OrderSide.SELL, quantity=f.quantity,
                                   order_type=OrderType.LMT, limit_price=Decimal("80.00"),
                                   reduce_only=True))
            b._clear(f.contract)
            b._seen_execs |= {x.exec_id for x in sim.executions()}
    assert n_fills > 20 and n_cancel_paths > 10  # fills and the core's cancel path both exercised


def _mapped(snapshot, contract, read_at):
    at = datetime.fromisoformat(read_at)
    q, _ = map_snapshot(snapshot, contract, at=at)
    return q, at


def test_the_order_the_core_cancels_is_never_matched_on_a_not_live_read(tmp_path):
    for kind in ("delayed", "frozen", "ask_stale", "unreported_expired"):
        b, sim, clock, contract = _broker(tmp_path / kind)
        b._session.snapshot = snap(bid=88.40, ask=88.42)  # live, above the 88.35 limit: rests
        entry = OrderIntent(order_ref="TB-e", contract=contract, side=OrderSide.BUY, quantity=1,
                            order_type=OrderType.LMT, limit_price=Decimal("88.35"))
        b.submit(entry)
        b._session.snapshot = replace(snap(bid=88.30, ask=88.32, **KINDS[kind]))
        out = b.cancel("TB-e")  # the core's cancel: no fill, no exception, the order cancelled
        assert out.status.value == "Cancelled", (kind, out.status)
        assert sim.executions() == [] and sim.positions() == [], kind
        assert b.take_fill_log() == [], kind  # the core's reason only: nothing journaled here
        assert sim.open_orders() == []
        clock.advance(timedelta(seconds=1).total_seconds())
