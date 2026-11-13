"""``IbkrPaperBroker`` for the owner's exit decision (13 Nov 2026), through the REAL ibapi over the
fake transport, and the real ExecutionCoordinator on top. No socket. Invented data only.

* ``exit_at_touch``: the exit estimate used by the stop-cancel dry check is the fresh bid itself.
* ``strict_quotes``: the quote handed to the core never looks fresher or more live than the
  adapter measured it: per-side times from the stream evidence (``shadow_quotes.map_snapshot``),
  ``received_at`` the OLDER side, a side without a live time makes the quote UNAVAILABLE.
* The bounded close through the coordinator: an unfilled closing limit is cancelled (confirmed)
  and the protective stop placed again as one NEW placement that counts as one submission.
"""
from __future__ import annotations

import socket
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

pytest.importorskip("ibapi")

import ibapi.client
from _ibkr_fake_tws_orders import PAPER, FakeOrderTws
from execution_harness import action
from test_ibkr_paper_orders_sdk import buy, fake_id, make, placed, run, settle, stop, tlt_open

from tradingbot.execution.broker import QuoteQuality, TimeInForce
from tradingbot.execution.coordinator import (
    EXIT_UNFILLED_CANCELLED,
    ExecutionConfig,
    ExecutionCoordinator,
)
from tradingbot.execution.store import IntentState
from tradingbot.ibkr_orders.broker import IbkrPaperBroker
from tradingbot.models import Action, AssetClass, Instrument, MockPolicy

D = Decimal
TLT = Instrument(asset_class=AssetClass.ETF, symbol="TLT")


@pytest.fixture
def tws(monkeypatch):
    def no_network(*args, **kwargs):
        raise AssertionError("network access attempted in an offline test")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket.socket, "connect_ex", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    server = FakeOrderTws()
    server.server_version = 226
    monkeypatch.setattr(ibapi.client, "Connection", server.connection_factory)
    return server


def bounded_coordinator(tws, tmp_path):
    """Stage 3's wiring: the touch, strict quotes, a bounded exit (short here), LIVE only."""
    s = make(tws, tmp_path)
    policy = MockPolicy(quantity=1, quantity_per_add=1, max_notional_usd=D(150),
                        max_price_deviation_pct=D(1), max_quote_age_seconds=3)
    broker = IbkrPaperBroker(s, exit_at_touch=True, strict_quotes=True)
    config = ExecutionConfig(halt_file=tmp_path / "HALT", expected_account=PAPER,
                             accepted_quote_qualities=frozenset({QuoteQuality.LIVE}),
                             protective_tif=TimeInForce.DAY, confirm_attempts=10,
                             confirm_wait_seconds=0.05, exit_price_policy="TOUCH",
                             exit_validity_seconds=0.5)
    return s, broker, ExecutionCoordinator(broker, tmp_path / "exec.sqlite3", policy, config)


def held_with_stop(tws, s):
    snaps = s.place([buy(), stop()])
    tws.fill(snaps[0].order_id, 1, 88.11)
    assert settle(lambda: s.positions() and s.positions()[0].quantity == 1)
    return snaps


def test_the_default_exit_estimate_keeps_the_collar(tws, tmp_path):
    s = make(tws, tmp_path)
    try:
        assert IbkrPaperBroker(s, exit_collar_pct=D(1)).exit_limit() == D("87.22")  # 88.10 x .99
    finally:
        s.disconnect()


def test_the_touch_exit_estimate_is_the_fresh_bid(tws, tmp_path):
    s = make(tws, tmp_path)
    try:
        assert IbkrPaperBroker(s, exit_at_touch=True).exit_limit() == D("88.10")
        tws.bid = 88.05
        assert IbkrPaperBroker(s, exit_at_touch=True, strict_quotes=True).exit_limit() == \
            D("88.05")
    finally:
        s.disconnect()


def test_a_touch_estimate_lets_the_dry_check_cancel_a_held_stop(tws, tmp_path):
    s = make(tws, tmp_path)
    try:
        held_with_stop(tws, s)
        b = IbkrPaperBroker(s, exit_at_touch=True, strict_quotes=True)
        b.cancel("TB-0001.STP")
        assert tws.count("CANCEL_ORDER") == 1
    finally:
        s.disconnect()


def test_the_default_quote_is_unchanged(tws, tmp_path):
    s = make(tws, tmp_path)
    try:
        b = IbkrPaperBroker(s)
        q = b.quote(b.resolve(TLT))
        assert q.quality == QuoteQuality.LIVE and q.bid_at is None and q.ask_at is None
        assert "bid_age_seconds" not in b.last_quote_report
    finally:
        s.disconnect()


def test_a_strict_quote_is_as_old_as_its_older_side(tws, tmp_path):
    s = make(tws, tmp_path)
    try:
        b = IbkrPaperBroker(s, strict_quotes=True)
        t = datetime.now(UTC)
        b.set_reference_time(t)
        q = b.quote(b.resolve(TLT))
        rep = b.last_quote_report
        assert q.quality == QuoteQuality.LIVE and q.bid == D("88.1") and q.ask == D("88.12")
        assert q.bid_at is not None and q.ask_at is not None
        assert q.received_at == min(q.bid_at, q.ask_at)
        # stamped at the reference: the age the core measures is at least the adapter's own (the
        # evidence rounds it to the millisecond; the stamps are exact, rounded UP)
        assert (t - q.bid_at).total_seconds() >= rep["bid_age_seconds"] - 0.0005
        assert (t - q.ask_at).total_seconds() >= rep["ask_age_seconds"] - 0.0005
        assert rep["bid_age_seconds"] >= 0.0 and rep["label"] == "LIVE"
        b.set_reference_time(t - timedelta(seconds=30))  # an older reference only ages it
        assert (t - b.quote(b.resolve(TLT)).received_at).total_seconds() >= 30
    finally:
        s.disconnect()


def test_a_strict_quote_without_live_side_times_is_unavailable(tws, tmp_path):
    s = make(tws, tmp_path)
    try:
        tws.market_data_type = 3  # invented: delayed ticks only
        b = IbkrPaperBroker(s, strict_quotes=True)
        q = b.quote(b.resolve(TLT))
        assert q.quality == QuoteQuality.UNAVAILABLE and q.bid is None and q.ask is None
        assert "BID_TICKS_NOT_LIVE" in b.last_quote_report["problems"]
        assert IbkrPaperBroker(s).quote(b.resolve(TLT)).quality == QuoteQuality.DELAYED
    finally:
        s.disconnect()


def test_a_one_sided_strict_quote_has_no_bid(tws, tmp_path):
    s = make(tws, tmp_path)
    try:
        tws.bid = 0.0  # invented: no bid on the book
        b = IbkrPaperBroker(s, strict_quotes=True)
        q = b.quote(b.resolve(TLT))
        assert q.bid is None
        assert {"BID_NON_POSITIVE", "SIDE_TIME_UNKNOWN"} & set(b.last_quote_report["problems"]) \
            or q.quality == QuoteQuality.UNAVAILABLE
    finally:
        s.disconnect()


def test_an_unfilled_close_is_cancelled_and_the_stop_placed_again_as_one_new_order(tws,
                                                                                  tmp_path):
    s, _broker, coord = bounded_coordinator(tws, tmp_path)
    try:
        out = run(coord, "p1", tlt_open(entry="88.12"))
        tws.fill(fake_id(tws, out.order_ref), 1, 88.12)
        assert settle(lambda: coord.sync() and coord.ledger()[0].protection == "CONFIRMED")
        idea = coord.ledger()[0].idea_id
        assert s.submissions_used == 2
        close = run(coord, "p3", action("close", Action.CLOSE, idea=idea, symbol="TLT"))
        assert close.state == IntentState.WORKING, close
        assert coord.intent(close.order_ref).limit_price == "88.1"  # the bid, no haircut
        assert s.submissions_used == 3 and tws.placed()[-1].order.lmtPrice == 88.10
        time.sleep(0.6)
        assert settle(lambda: coord.sync() and coord.intent(close.order_ref).state
                      == IntentState.CANCELLED, 5)
        assert EXIT_UNFILLED_CANCELLED in coord.intent(close.order_ref).reasons
        assert settle(lambda: coord.sync() and coord.ledger()[0].protection == "CONFIRMED", 5)
        [pos] = coord.ledger()
        assert pos.quantity == 1 and pos.stop_ref == f"{out.order_ref}.STP2"
        restored = tws.placed()[-1]
        assert restored.order.orderType == "STP" and restored.order.action == "SELL"
        assert restored.order.auxPrice == 87.0 and restored.order.tif == "DAY"
        assert restored.order.parentId == 0 and restored.orderId not in (
            o.order_id for o in tws.orders.values() if o.status == "Cancelled")
        assert s.submissions_used == 4 and placed(tws) == 4  # a NEW placement: it counts
        assert [e["kind"] for e in s._counter.entries][-1] == "SINGLE"
        # the second close cancels the restored stop and fills
        tws.autofill = True
        second = run(coord, "p4", action("close2", Action.CLOSE, idea=idea, symbol="TLT"))
        assert second.state == IntentState.FILLED, second
        assert settle(lambda: coord.sync() and coord.ledger()[0].quantity == 0, 5)
        assert s.submissions_used == 5 and coord.reconcile().clean
        assert not [o for o in tws.orders.values() if o.working]
    finally:
        coord.close()
        s.disconnect()
