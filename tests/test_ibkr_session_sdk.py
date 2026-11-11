"""IbkrSession driven through the REAL ibapi 1052.1 EClient/EReader/Decoder/EWrapper over a fake
transport (``tests/_ibkr_fake_tws.py``). No socket is opened: ``socket.socket.connect`` raises."""
from __future__ import annotations

import inspect
import json
import os
import socket
import time

import pytest

pytest.importorskip("ibapi")

import ibapi.client
from _ibkr_fake_tws import OTHER_PAPER, PAPER, FakeTws, error_frame, frame, tlt_contract_frame
from ibapi.message import IN
from ibapi.protobuf.ContractDataEnd_pb2 import ContractDataEnd
from ibapi.protobuf.TickPrice_pb2 import TickPrice
from ibapi.protobuf.TickSnapshotEnd_pb2 import TickSnapshotEnd
from ibapi.server_versions import MIN_SERVER_VER_CANCEL_CONTRACT_DATA
from ibapi.ticktype import TickTypeEnum
from ibapi.wrapper import EWrapper

from tradingbot.ibkr import errors as E
from tradingbot.ibkr import models
from tradingbot.ibkr.check import run_check
from tradingbot.ibkr.config import validate_config
from tradingbot.ibkr.models import TLT_QUERY, QuoteQuality
from tradingbot.ibkr.session import (
    MIN_SERVER_VER_CANCEL_CONTRACT_DATA as OUR_MIN_CANCEL,
)
from tradingbot.ibkr.session import IbkrSession, SessionState, _Wrapper

CFG = validate_config({"host": "127.0.0.1", "port": 7497, "client_id": 7, "account": PAPER,
                       "mode": "paper"})
DATA_REQUESTS = {"REQ_CONTRACT_DATA", "REQ_ACCOUNT_SUMMARY", "REQ_POSITIONS", "REQ_MKT_DATA",
                 "REQ_MARKET_DATA_TYPE"}


@pytest.fixture
def tws(monkeypatch):
    def no_network(*args, **kwargs):
        raise AssertionError("network access attempted in an offline test")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket.socket, "connect_ex", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    server = FakeTws()
    monkeypatch.setattr(ibapi.client, "Connection", server.connection_factory)
    return server


def session(**kw) -> IbkrSession:
    opts = {"connect_timeout": 2.0, "request_timeout": 2.0, "quote_timeout": 2.0,
            "shutdown_timeout": 3.0}
    opts.update(kw)
    return IbkrSession(CFG, **opts)


def assert_closed(s: IbkrSession) -> None:
    assert s.state is SessionState.DISCONNECTED
    assert s.threads_alive() == []
    assert not s._client.isConnected()


# --- the adapter matches the installed SDK -------------------------------------------------------
def test_wrapper_callback_signatures_match_installed_sdk():
    overridden = [n for n, v in vars(_Wrapper).items() if callable(v) and not n.startswith("_")]
    assert len(overridden) >= 18
    for name in overridden:
        ours = list(inspect.signature(getattr(_Wrapper, name)).parameters)
        sdk = list(inspect.signature(getattr(EWrapper, name)).parameters)
        assert ours == sdk, name


def test_tick_and_version_constants_match_sdk():
    names = {1: "BID", 2: "ASK", 4: "LAST", 66: "DELAYED_BID", 67: "DELAYED_ASK", 68: "DELAYED_LAST"}
    for tick_id, name in names.items():
        assert getattr(TickTypeEnum, name) == tick_id and tick_id in models.PRICE_TICKS
    sizes = {0: "BID_SIZE", 3: "ASK_SIZE", 5: "LAST_SIZE", 69: "DELAYED_BID_SIZE",
             70: "DELAYED_ASK_SIZE", 71: "DELAYED_LAST_SIZE"}
    for tick_id, name in sizes.items():
        assert getattr(TickTypeEnum, name) == tick_id and tick_id in models.SIZE_TICKS
    assert TickTypeEnum.LAST_TIMESTAMP == 45 and TickTypeEnum.DELAYED_LAST_TIMESTAMP == 88
    assert OUR_MIN_CANCEL == MIN_SERVER_VER_CANCEL_CONTRACT_DATA
    assert IbkrSession.sdk_version == "1052.1"


# --- happy path ------------------------------------------------------------------------------------
def test_happy_path(tws):
    with session() as s:
        assert s.state is SessionState.READY
        assert s.server_version == tws.server_version
        assert s.connection_time == tws.connection_time
        c = s.resolve_contract(TLT_QUERY)
        assert (c.con_id, c.symbol, c.sec_type, c.currency) == (15547841, "TLT", "STK", "USD")
        assert (c.exchange, c.primary_exchange, c.local_symbol, c.min_tick) == (
            "SMART", "NASDAQ", "TLT", 0.01)
        assert c.trading_hours and c.liquid_hours and c.time_zone_id == "US/Eastern"
        summary = s.account_summary()
        assert summary.missing_tags == () and summary.values["AccountType"].value == "INDIVIDUAL"
        assert summary.account_masked == "DU…01"
        rows = s.positions()
        assert len(rows) == 1 and rows[0].account_masked == "DU…01" and str(rows[0].quantity) == "10"
        q = s.quote_snapshot(c)
        assert repr(s) == "IbkrSession(state=READY, account='DU…01')"
    assert_closed(s)
    names = tws.names()
    assert names[0] == "START_API"
    assert "CANCEL_ACCOUNT_SUMMARY" in names and "CANCEL_POSITIONS" in names
    assert "CANCEL_MKT_DATA" not in names  # snapshot ended by itself
    assert {code for _, code, _ in s.notices} >= {2104, 2106, 2158}
    assert not s.unrouted_errors and not s.internal_errors and not s.cleanup_failures
    # delayed-only quote reported honestly
    assert q.quality is QuoteQuality.DELAYED and q.market_data_type_name == "DELAYED"
    assert (q.bid, q.ask, q.last) == (88.10, 88.12, 88.11) and str(q.bid_size) == "300"
    assert q.last_trade_time.startswith("2026-11-11T16:00") and 10167 in q.notices
    assert q.two_sided_valid and q.flags == ()
    mdt = [r for n, r in tws.requests if n == "REQ_MARKET_DATA_TYPE"]
    assert mdt[0].marketDataType == 3
    mkt = next(r for n, r in tws.requests if n == "REQ_MKT_DATA")
    assert mkt.snapshot and not mkt.regulatorySnapshot and mkt.contract.conId == 15547841


def test_disconnect_is_idempotent_and_session_single_use(tws):
    s = session()
    s.connect()
    s.disconnect()
    s.disconnect()
    assert_closed(s)
    with pytest.raises(E.SessionStateError):
        s.connect()
    with pytest.raises(E.SessionStateError):
        s.positions()


# --- identity gate ---------------------------------------------------------------------------------
@pytest.mark.parametrize("accounts,reason", [
    (OTHER_PAPER, "MISMATCH"), (f"{PAPER},{OTHER_PAPER}", "MULTIPLE"), ("", "EMPTY"),
    ("U1234567", "MISMATCH")])
def test_identity_gate_rejects_and_sends_no_data_request(tws, accounts, reason):
    tws.managed_accounts = accounts
    s = session()
    with pytest.raises(E.AccountIdentityError) as ei:
        s.connect()
    assert ei.value.reason == reason
    assert PAPER not in str(ei.value) and OTHER_PAPER not in str(ei.value)
    assert "U1234567" not in str(ei.value)
    assert_closed(s)
    assert not DATA_REQUESTS & set(tws.names())


def test_identity_timeout_when_managed_accounts_never_arrive(tws):
    tws.managed_accounts = None
    s = session(connect_timeout=0.5)
    t0 = time.monotonic()
    with pytest.raises(E.AccountIdentityError) as ei:
        s.connect()
    assert ei.value.reason == "TIMEOUT" and time.monotonic() - t0 < 2
    assert_closed(s)
    assert not DATA_REQUESTS & set(tws.names())


def test_identity_timeout_when_next_valid_id_missing(tws):
    tws.send_next_valid_id = False
    s = session(connect_timeout=0.5)
    with pytest.raises(E.AccountIdentityError, match="nextValidId missing"):
        s.connect()
    assert_closed(s)


def test_handshake_hang_is_bounded(tws):
    tws.handshake = False  # socket accepted, TWS never answers (e.g. API prompt pending)
    s = session(connect_timeout=0.5)
    with pytest.raises(E.IbkrConnectionError, match="handshake"):
        s.connect()
    assert_closed(s)


def test_connection_refused(tws):
    tws.refuse = True
    s = session()
    with pytest.raises(E.IbkrConnectionError, match="502"):
        s.connect()
    assert_closed(s)


# --- contract resolution --------------------------------------------------------------------------
def test_ambiguous_contract(tws):
    tws.on["REQ_CONTRACT_DATA"] = lambda srv, c, r: (
        c.push(tlt_contract_frame(r.reqId, con_id=1)), c.push(tlt_contract_frame(r.reqId, con_id=2)),
        c.push(frame(IN.CONTRACT_DATA_END, ContractDataEnd(reqId=r.reqId))))
    with session() as s, pytest.raises(E.AmbiguousContract) as ei:
        s.resolve_contract(TLT_QUERY)
    assert ei.value.count == 2
    assert_closed(s)


def test_missing_contract_empty_end(tws):
    tws.on["REQ_CONTRACT_DATA"] = lambda srv, c, r: c.push(
        frame(IN.CONTRACT_DATA_END, ContractDataEnd(reqId=r.reqId)))
    with session() as s, pytest.raises(E.ContractNotFound):
        s.resolve_contract(TLT_QUERY)


def test_missing_contract_error_200(tws):
    tws.on["REQ_CONTRACT_DATA"] = lambda srv, c, r: c.push(
        error_frame(r.reqId, 200, "No security definition has been found for the request"))
    with session() as s:
        with pytest.raises(E.ContractNotFound):
            s.resolve_contract(TLT_QUERY)
        assert s.state is SessionState.READY  # a rejected request does not end the session


def test_error_callback_rejection_is_typed_and_redacted(tws):
    tws.on["REQ_CONTRACT_DATA"] = lambda srv, c, r: c.push(
        error_frame(r.reqId, 321, f"Error validating request: account {PAPER} not allowed"))
    with session() as s:
        with pytest.raises(E.IbkrRequestError) as ei:
            s.resolve_contract(TLT_QUERY)
        assert ei.value.code == 321 and ei.value.req_id is not None
        assert PAPER not in str(ei.value) and "DU…01" in str(ei.value)
        assert s.positions()  # session still usable afterwards


# --- foreign account rows poison the session ------------------------------------------------------
def test_foreign_account_summary_row_poisons(tws):
    tws.summary_account = OTHER_PAPER
    s = session()
    s.connect()
    with pytest.raises(E.AccountIdentityError) as ei:
        s.account_summary()
    assert ei.value.reason == "FOREIGN_ROW" and OTHER_PAPER not in str(ei.value)
    assert s.poisoned
    assert_closed(s)
    with pytest.raises(E.AccountIdentityError):
        s.positions()


def test_foreign_position_row_poisons(tws):
    tws.positions = [(PAPER, "TLT", 1, "5"), (OTHER_PAPER, "IEF", 2, "3")]
    s = session()
    s.connect()
    with pytest.raises(E.AccountIdentityError):
        s.positions()
    assert s.poisoned
    assert_closed(s)


# --- quotes ------------------------------------------------------------------------------------------
def test_quote_without_entitlement_is_unavailable_and_cancelled(tws):
    tws.on["REQ_MKT_DATA"] = lambda srv, c, r: c.push(error_frame(
        r.reqId, 354, "Requested market data is not subscribed."))
    with session() as s:
        q = s.quote_snapshot(s.resolve_contract(TLT_QUERY))
    assert q.quality is QuoteQuality.UNAVAILABLE and q.unavailable_code == 354
    assert "NOT_ENTITLED" in q.flags and not q.two_sided_valid
    assert "CANCEL_MKT_DATA" in tws.names()


def test_quote_without_market_data_type_callback_is_never_live(tws):
    def ticks(srv, c, r):
        c.push(frame(IN.TICK_PRICE, TickPrice(reqId=r.reqId, tickType=1, price=88.0, size="1")))
        c.push(frame(IN.TICK_PRICE, TickPrice(reqId=r.reqId, tickType=2, price=88.2, size="1")))
        c.push(frame(IN.TICK_SNAPSHOT_END, TickSnapshotEnd(reqId=r.reqId)))

    tws.on["REQ_MKT_DATA"] = ticks
    with session() as s:
        q = s.quote_snapshot(s.resolve_contract(TLT_QUERY), market_data_type=1)
    assert q.quality is QuoteQuality.FROZEN and "MARKET_DATA_TYPE_NOT_REPORTED" in q.flags
    assert q.last is None and q.last_trade_time is None


# --- timeouts always clean up -----------------------------------------------------------------
@pytest.mark.parametrize("handler,call,cancel", [
    ("REQ_CONTRACT_DATA", lambda s: s.resolve_contract(TLT_QUERY, timeout=0.3),
     "CANCEL_CONTRACT_DATA"),
    ("REQ_ACCOUNT_SUMMARY", lambda s: s.account_summary(timeout=0.3), "CANCEL_ACCOUNT_SUMMARY"),
    ("REQ_POSITIONS", lambda s: s.positions(timeout=0.3), "CANCEL_POSITIONS"),
])
def test_timeout_raises_and_cancels(tws, handler, call, cancel):
    tws.on[handler] = lambda srv, c, r: None  # never answers
    with session() as s:
        with pytest.raises(E.IbkrTimeout):
            call(s)
        assert cancel in tws.names()
        assert s.state is SessionState.READY and not s.cleanup_failures
    assert_closed(s)


def test_quote_timeout_cancels_market_data(tws):
    with session() as s:
        contract = s.resolve_contract(TLT_QUERY)
        tws.on["REQ_MKT_DATA"] = lambda srv, c, r: None
        with pytest.raises(E.IbkrTimeout):
            s.quote_snapshot(contract, timeout=0.3)
    assert "CANCEL_MKT_DATA" in tws.names()


def test_cancel_failure_is_reported_not_swallowed(tws):
    tws.fail_send = {"CANCEL_ACCOUNT_SUMMARY"}
    with session() as s:
        with pytest.raises(E.CleanupError) as ei:
            s.account_summary()
        assert ei.value.result is not None and ei.value.result.missing_tags == ()
        assert s.cleanup_failures and "account_summary" in s.cleanup_failures[0]


# --- connection drop and recovery -----------------------------------------------------------------
def test_dropped_connection_fails_pending_then_fresh_session_recovers(tws):
    s = session()
    s.connect()
    tws.on["REQ_POSITIONS"] = lambda srv, c, r: srv.drop()
    t0 = time.monotonic()
    with pytest.raises(E.IbkrConnectionError):
        s.positions(timeout=5)
    assert time.monotonic() - t0 < 2  # failed fast, not by timeout
    with pytest.raises(E.IbkrConnectionError):
        s.account_summary()
    s.disconnect()
    assert_closed(s)

    tws.on["REQ_POSITIONS"] = FakeTws.positions_handler
    with session() as s2:  # recovery: identity gate re-runs on the new connection
        assert len(s2.positions()) == 1
    assert tws.names().count("START_API") == 2
    assert_closed(s2)


def test_connectivity_lost_code_1100_fails_pending(tws):
    tws.on["REQ_ACCOUNT_SUMMARY"] = lambda srv, c, r: c.push(
        error_frame(-1, 1100, "Connectivity between IB and Trader Workstation has been lost."))
    with session() as s:
        with pytest.raises(E.IbkrConnectionError):
            s.account_summary(timeout=5)
        assert any(code == 1100 for _, code, _ in s.connection_events)


# --- end-to-end check runner over the real SDK + fake transport ---------------------------------
def test_check_runner_end_to_end(tws, tmp_path):
    cfg_path = tmp_path / "paper.toml"
    cfg_path.write_text(f'host = "127.0.0.1"\nport = 7497\nclient_id = 7\naccount = "{PAPER}"\n'
                        'mode = "paper"\n')
    os.chmod(cfg_path, 0o600)
    report = run_check(cfg_path, session_factory=lambda cfg: session())
    assert report["overall"] == "PASS", report
    assert report["sdk_version"] == "1052.1" and report["session"]["server_version"] == 226
    assert report["session"]["tws_connection_time"] == tws.connection_time
    assert PAPER not in json.dumps(report)
    assert tws.names().count("START_API") == 2
    assert any("DELAYED" in w for w in report["warnings"])
