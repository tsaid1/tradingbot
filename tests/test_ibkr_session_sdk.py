"""IbkrSession driven through the REAL ibapi 1052.1 EClient/EReader/Decoder/EWrapper over a fake
transport (``tests/_ibkr_fake_tws.py``). No socket is opened: ``socket.socket.connect`` raises."""
from __future__ import annotations

import collections
import inspect
import io
import json
import os
import re
import socket
import time
import types
import weakref
from decimal import Decimal

import pytest

pytest.importorskip("ibapi")

import _ibkr_fake_tws
import ibapi.client
from _ibkr_fake_tws import (
    OTHER_PAPER,
    PAPER,
    FakeTws,
    error_frame,
    frame,
    text_frame,
    tlt_contract_frame,
)
from ibapi import comm
from ibapi.client import EClient
from ibapi.common import PROTOBUF_MSG_ID
from ibapi.message import IN, OUT
from ibapi.protobuf.ContractDataEnd_pb2 import ContractDataEnd
from ibapi.protobuf.TickPrice_pb2 import TickPrice
from ibapi.protobuf.TickSnapshotEnd_pb2 import TickSnapshotEnd
from ibapi.protobuf.TickString_pb2 import TickString
from ibapi.server_versions import MIN_SERVER_VER_CANCEL_CONTRACT_DATA
from ibapi.ticktype import TickTypeEnum
from ibapi.wrapper import EWrapper

from tradingbot.ibkr import errors as E
from tradingbot.ibkr import models
from tradingbot.ibkr.check import run_check
from tradingbot.ibkr.config import validate_config
from tradingbot.ibkr.errors import ReadOnlyViolation
from tradingbot.ibkr.models import TLT_QUERY, QuoteQuality
from tradingbot.ibkr.session import (
    ALLOWED_CLIENT_METHODS,
    ALLOWED_OUT_MESSAGES,
    IbkrSession,
    SessionState,
    _Wrapper,
)
from tradingbot.ibkr.session import (
    MIN_SERVER_VER_CANCEL_CONTRACT_DATA as OUR_MIN_CANCEL,
)

CFG_DATA = {"mode": "paper", "host": "127.0.0.1", "port": 7497, "client_id": 7, "account": PAPER,
            "tws_profile_ini": "/Users/someone/Jts/jts.ini", "tws_os_user": "someone",
            "read_only_check": "config-read"}  # synthetic
CFG = validate_config(CFG_DATA)


def stub_preflight(cfg):  # the real preflight reads the local socket table; tested elsewhere
    return {"stub": True}
DATA_REQUESTS = {"REQ_CONTRACT_DATA", "REQ_ACCOUNT_SUMMARY", "REQ_POSITIONS", "REQ_MKT_DATA",
                 "REQ_MARKET_DATA_TYPE", "REQ_HISTORICAL_DATA"}
SERVER_VERSIONS = [219, 226]  # lowest accepted (MIN_SERVER_VERSION) and this SDK's maximum


@pytest.fixture(params=SERVER_VERSIONS, ids=lambda v: f"sv{v}")
def tws(monkeypatch, request):
    def no_network(*args, **kwargs):
        raise AssertionError("network access attempted in an offline test")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket.socket, "connect_ex", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    server = FakeTws()
    server.server_version = request.param
    monkeypatch.setattr(ibapi.client, "Connection", server.connection_factory)
    return server


def session(cfg=CFG, **kw) -> IbkrSession:
    opts = {"connect_timeout": 2.0, "request_timeout": 2.0, "quote_timeout": 2.0,
            "shutdown_timeout": 3.0, "preflight": stub_preflight}
    opts.update(kw)
    return IbkrSession(cfg, **opts)


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
    assert names[:2] == ["START_API", "REQ_CONFIG"]  # config read before any data request
    assert s.read_only_status == "VERIFIED"
    assert s.read_only_settings == {"readOnlyApi": True, "socketPort": 7497,
                                    "allowLocalhostOnly": True}
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


def test_quote_timeout_keeps_evidence_and_cancels(tws):
    with session() as s:
        contract = s.resolve_contract(TLT_QUERY)
        tws.on["REQ_MKT_DATA"] = lambda srv, c, r: None
        q = s.quote_snapshot(contract, timeout=0.3)
    assert q.snapshot_end_missing and "SNAPSHOT_END_MISSING" in q.flags
    assert q.quality is QuoteQuality.UNAVAILABLE and q.ticks_received == 0
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
    cfg_path.write_text("".join(f"{k} = {json.dumps(v)}\n" for k, v in CFG_DATA.items()))
    os.chmod(cfg_path, 0o600)
    report = run_check(cfg_path, session_factory=lambda cfg: session())
    assert report["overall"] in ("PASS", "PASS_WITH_WARNINGS"), report
    assert report["sdk_version"] == "1052.1"
    assert report["session"]["server_version"] == tws.server_version
    assert report["session"]["reader_joined"] and report["reconnect_session"]["reader_joined"]
    assert report["session"]["tws_connection_time"] == tws.connection_time
    assert PAPER not in json.dumps(report)
    assert tws.names().count("START_API") == 2
    # default fake quote is delayed-only: two-sided, broker-reported type 3 -> PASS_WITH_WARNINGS
    assert report["overall"] == "PASS_WITH_WARNINGS"
    step = next(st for st in report["steps"] if st["name"] == "quote")
    assert step["status"] == "PASS_WITH_WARNINGS" and "DELAYED" in step["detail"]["reason"]


# --- review round: preflight, server version, read-only verification ----------------------------
def test_preflight_failure_opens_no_connection(tws):
    def refuse(cfg):
        raise E.PaperModeNotConfirmed("paper mode not confirmed: test", reason="NOT_PAPER")

    s = session(preflight=refuse)
    with pytest.raises(E.PaperModeNotConfirmed):
        s.connect()
    assert tws.connections == [] and tws.requests == []
    assert_closed(s)


def test_listener_preflight_failure_opens_no_connection(tws):
    def refuse(cfg):
        raise E.ListenerNotConfirmed("port 7497 listener owned by x", reason="WRONG_USER")

    with pytest.raises(E.ListenerNotConfirmed):
        session(preflight=refuse).connect()
    assert tws.connections == []


def test_default_preflight_is_the_real_one():
    from tradingbot.ibkr.environment import run_preflight

    assert inspect.signature(IbkrSession).parameters["preflight"].default is run_preflight


@pytest.mark.parametrize("version", [150, 218])
def test_old_server_version_refused(tws, version):
    tws.server_version = version
    s = session()
    with pytest.raises(E.UnsupportedServerVersion):
        s.connect()
    assert_closed(s)
    assert not DATA_REQUESTS & set(tws.names()) and "REQ_CONFIG" not in tws.names()


@pytest.mark.parametrize("settings,needle", [
    ({"readOnlyApi": False}, "readOnlyApi=False"),
    ({"readOnlyApi": None}, "readOnlyApi=None"),
    ({"socketPort": 7496}, "socketPort=7496"),
    ({"socketPort": None}, "socketPort=None"),
    ({"allowLocalhostOnly": False}, "allowLocalhostOnly=False"),
    ({"allowLocalhostOnly": None}, "allowLocalhostOnly=None"),
])
def test_config_read_refusals(tws, settings, needle):
    tws.api_settings = {**tws.api_settings, **settings}
    s = session()
    with pytest.raises(E.ReadOnlySettingRefused, match=needle):
        s.connect()
    assert_closed(s)
    assert not DATA_REQUESTS & set(tws.names())


def test_config_read_without_api_section_refused(tws):
    from ibapi.protobuf.ConfigResponse_pb2 import ConfigResponse

    tws.on["REQ_CONFIG"] = lambda srv, c, r: c.push(
        frame(IN.CONFIG_RESPONSE, ConfigResponse(reqId=r.reqId)))
    with pytest.raises(E.ReadOnlySettingRefused):
        session().connect()


def _config_response(req_id):
    from ibapi.protobuf.ApiConfig_pb2 import ApiConfig
    from ibapi.protobuf.ApiSettingsConfig_pb2 import ApiSettingsConfig
    from ibapi.protobuf.ConfigResponse_pb2 import ConfigResponse

    settings = ApiSettingsConfig(readOnlyApi=True, socketPort=7497, allowLocalhostOnly=True)
    resp = ConfigResponse(api=ApiConfig(settings=settings))
    if req_id is not None:
        resp.reqId = req_id
    return frame(IN.CONFIG_RESPONSE, resp)


@pytest.mark.parametrize("handler,exc", [
    (lambda srv, c, r: None, "ConfigReadTimeout"),
    (lambda srv, c, r: c.push(error_frame(r.reqId, 10999, "config not allowed")),
     "ConfigReadRejected"),
    (lambda srv, c, r: c.push(_config_response(r.reqId + 1)), "ConfigReadUncorrelated"),
    (lambda srv, c, r: c.push(_config_response(None)), "ConfigReadUncorrelated"),
])
def test_config_read_failure_modes_have_distinct_types(tws, handler, exc):
    tws.on["REQ_CONFIG"] = handler
    s = session(request_timeout=0.5)
    with pytest.raises(getattr(E, exc)) as ei:
        s.connect()
    assert type(ei.value).__name__ == exc and isinstance(ei.value, E.ReadOnlyNotVerified)
    assert_closed(s)
    assert not DATA_REQUESTS & set(tws.names())


def test_config_read_unsupported_server_version_is_distinct(tws, monkeypatch):
    import tradingbot.ibkr.session as sess

    monkeypatch.setattr(sess, "MIN_SERVER_VER_CONFIG", tws.server_version + 1)
    s = session()
    with pytest.raises(E.ConfigReadUnsupported):
        s.connect()
    assert_closed(s)
    assert "REQ_CONFIG" not in tws.names() and not DATA_REQUESTS & set(tws.names())


def test_unentitled_quote_on_both_routes_check_fails_with_evidence(tws, tmp_path):
    tws.on["REQ_MKT_DATA"] = lambda srv, c, r: c.push(error_frame(r.reqId, 354, "not subscribed"))
    cfg_path = tmp_path / "paper.toml"
    cfg_path.write_text("".join(f"{k} = {json.dumps(v)}\n" for k, v in CFG_DATA.items()))
    os.chmod(cfg_path, 0o600)
    from tradingbot.ibkr.check import summarize

    report = run_check(cfg_path, session_factory=lambda cfg: session(stream_seconds=0.3),
                       quote_snapshot_fallback=True)
    assert report["overall"] == "FAIL"
    assert "no market data delivered" in summarize(report).splitlines()[0]
    step = next(st for st in report["steps"] if st["name"] == "quote")
    assert step["status"] == "FAIL"
    attempts = step["detail"]["attempts"]
    assert [a["route"] for a in attempts] == ["stream", "snapshot"]
    assert all(a["request_events"][0][0] == 354 for a in attempts)


# --- managedAccounts: every value, from the first on ---------------------------------------------
@pytest.mark.parametrize("sequence", [["U1234567", PAPER], [PAPER, OTHER_PAPER],
                                      [PAPER, f"{PAPER},{OTHER_PAPER}"], [PAPER, ""]])
def test_any_bad_managed_accounts_fails_gate_permanently(tws, sequence):
    tws.managed_accounts = sequence
    s = session()
    with pytest.raises(E.AccountIdentityError):
        s.connect()
    assert_closed(s)
    assert not DATA_REQUESTS & set(tws.names())
    assert "U1234567" not in repr(s) and PAPER not in repr(s)


def test_repeated_identical_managed_accounts_is_fine(tws):
    tws.managed_accounts = [PAPER, PAPER]
    with session() as s:
        assert s.state is SessionState.READY


def test_bad_managed_accounts_after_ready_poisons(tws):
    from ibapi.protobuf.ManagedAccounts_pb2 import ManagedAccounts

    tws.on["REQ_POSITIONS"] = lambda srv, c, r: c.push(
        frame(IN.MANAGED_ACCTS, ManagedAccounts(accountsList=OTHER_PAPER)))
    s = session()
    s.connect()
    with pytest.raises(E.AccountIdentityError):
        s.positions(timeout=2)
    assert s.poisoned
    assert_closed(s)


def test_poison_between_check_and_send_sends_nothing(tws, monkeypatch):
    s = session()
    s.connect()
    real = s._require_ready
    calls = {"n": 0}

    def racing(gate=False):
        real(gate)
        calls["n"] += 1
        if calls["n"] == 1:  # poison arrives right after the first check passed
            s._poisoned = E.AccountIdentityError("foreign row", reason="FOREIGN_ROW")

    monkeypatch.setattr(s, "_require_ready", racing)
    with pytest.raises(E.AccountIdentityError):
        s.positions()
    assert "REQ_POSITIONS" not in tws.names()
    s._poisoned = None
    s.disconnect()


# --- deny-by-default client surface ----------------------------------------------------------------
def _public_callables() -> list[str]:
    return [n for n in dir(EClient) if not n.startswith("_") and callable(getattr(EClient, n))]


def test_allowlist_is_exactly_known_and_exists_in_sdk():
    assert ALLOWED_CLIENT_METHODS <= set(_public_callables())
    assert {"placeOrder", "cancelOrder", "reqGlobalCancel", "exerciseOptions",
            "updateConfigProtoBuf", "replaceFA", "replaceFAProtoBuf"}.isdisjoint(
        ALLOWED_CLIENT_METHODS)
    sends = {n for n in ALLOWED_CLIENT_METHODS if n.startswith(("req", "cancel"))}
    assert sends == {
        "reqContractDetails", "reqContractDataProtoBuf", "cancelContractData",
        "cancelContractDataProtoBuf", "reqAccountSummary", "reqAccountSummaryProtoBuf",
        "cancelAccountSummary", "cancelAccountSummaryProtoBuf", "reqPositions",
        "reqPositionsProtoBuf", "cancelPositions", "cancelPositionsProtoBuf", "reqMktData",
        "reqMarketDataProtoBuf", "cancelMktData", "cancelMarketDataProtoBuf", "reqMarketDataType",
        "reqMarketDataTypeProtoBuf", "reqConfigProtoBuf", "reqSecDefOptParams",
        "reqSecDefOptParamsProtoBuf", "reqHistoricalData", "reqHistoricalDataProtoBuf",
        "cancelHistoricalData", "cancelHistoricalDataProtoBuf"}


def test_every_non_allowlisted_eclient_method_raises_and_sends_nothing(tws):
    s = session()
    s.connect()
    before = list(tws.requests)
    denied = [n for n in _public_callables() if n not in ALLOWED_CLIENT_METHODS]
    assert len(denied) > 100
    for name in denied:
        with pytest.raises(ReadOnlyViolation):
            getattr(s._client, name)(1, 2, 3)
    assert tws.requests == before and s._client.isConnected()
    s.disconnect()
    assert_closed(s)


def test_wire_allowlist_refuses_other_message_ids(tws):
    s = session()
    s.connect()
    before = list(tws.requests)
    out_ids = {v for k, v in vars(OUT).items() if not k.startswith("_") and isinstance(v, int)}
    refused = sorted(out_ids - ALLOWED_OUT_MESSAGES)
    assert OUT.PLACE_ORDER in refused and OUT.UPDATE_CONFIG in refused
    for msg_id in refused:
        with pytest.raises(ReadOnlyViolation):
            s._client.sendMsgProtoBuf(msg_id + PROTOBUF_MSG_ID, b"")
        with pytest.raises(ReadOnlyViolation):
            s._client.sendMsg(msg_id, "")
    assert tws.requests == before
    s.disconnect()


# --- SDK logging never reaches a handler or stderr ------------------------------------------------
def test_sdk_field_mismatch_log_never_leaks_account(tws, capfd, caplog, monkeypatch):
    import logging

    from ibapi.decoder import Decoder

    seen = []
    real = Decoder.interpretWithSignature

    def spy(self, fields, handleInfo):
        seen.append(list(fields))
        return real(self, fields, handleInfo)

    monkeypatch.setattr(Decoder, "interpretWithSignature", spy)
    caplog.set_level(logging.DEBUG)
    # legacy text managedAccounts with one field too many -> decoder logs raw fields at ERROR
    tws.extra_after_start = [text_frame(IN.MANAGED_ACCTS, "1", PAPER, "extra-field")]
    with session() as s:
        assert s.state is SessionState.READY
    assert any(PAPER.encode() in fields for fields in seen), "mismatch path was not exercised"
    out, err = capfd.readouterr()
    assert PAPER not in out and PAPER not in err
    assert PAPER not in caplog.text
    assert all(PAPER not in r.getMessage() for r in caplog.records)
    sdk = logging.getLogger("ibapi")
    assert sdk.propagate is False and any(isinstance(h, logging.NullHandler) for h in sdk.handlers)


# --- no reachable object can submit orders or change config --------------------------------------
ORDER_LIKE = ("placeOrder", "cancelOrder", "reqGlobalCancel", "updateConfigProtoBuf")


def _order_args(name):
    from ibapi.contract import Contract
    from ibapi.order import Order
    from ibapi.order_cancel import OrderCancel
    from ibapi.protobuf.UpdateConfigRequest_pb2 import UpdateConfigRequest

    return {"placeOrder": (1, Contract(), Order()), "cancelOrder": (1, OrderCancel()),
            "reqGlobalCancel": (OrderCancel(),),
            "updateConfigProtoBuf": (UpdateConfigRequest(reqId=1),)}[name]


def _attrs(obj):
    for name in dir(obj):
        if name.startswith("__"):
            continue
        try:
            yield name, getattr(obj, name)
        except Exception:  # noqa: BLE001, S112 - a property that refuses is fine
            continue


def _reachable(s):
    """Every public and single-underscore attribute of the session, and of each of those."""
    seen, out = {id(s)}, []
    for name, value in _attrs(s):
        if id(value) not in seen:
            seen.add(id(value))
            out.append((name, value))
            for sub, v in _attrs(value):
                if id(v) not in seen:
                    seen.add(id(v))
                    out.append((f"{name}.{sub}", v))
    return out


def test_no_attribute_of_a_connected_session_can_submit_orders(tws):
    s = session()
    s.connect()
    before = list(tws.requests)
    hits = []
    for path, obj in _reachable(s):
        for meth in ORDER_LIKE:
            fn = getattr(obj, meth, None)
            if fn is None or isinstance(obj, type):
                continue
            hits.append(f"{path}.{meth}")
            with pytest.raises(ReadOnlyViolation):
                fn(*_order_args(meth))
    assert "_client.placeOrder" in hits and "_client.updateConfigProtoBuf" in hits
    assert tws.requests == before and s._client.isConnected()
    s.disconnect()


@pytest.mark.parametrize("meth", ORDER_LIKE)
def test_unbound_base_class_calls_send_nothing(tws, meth):
    """Even bypassing the subclass (EClient.<meth>(client, ...)), the wire allowlist refuses the
    message; where the SDK itself refuses first (version gate), nothing is sent either."""
    s = session()
    s.connect()
    before = list(tws.requests)
    try:
        getattr(EClient, meth)(s._client, *_order_args(meth))
    except ReadOnlyViolation:
        pass
    assert tws.requests == before
    s.disconnect()


# --- quote evidence: snapshot without end, streaming fallback ------------------------------------
def _tick(req_id, tick_type, price, size="100"):
    return frame(IN.TICK_PRICE, TickPrice(reqId=req_id, tickType=tick_type, price=price,
                                          size=size, attrMask=0))


def _mdt(req_id, value):
    from ibapi.protobuf.MarketDataType_pb2 import MarketDataType

    return frame(IN.MARKET_DATA_TYPE, MarketDataType(reqId=req_id, marketDataType=value))


def _req_params(req_id, permissions=3):
    from ibapi.protobuf.TickReqParams_pb2 import TickReqParams

    return frame(IN.TICK_REQ_PARAMS, TickReqParams(reqId=req_id, minTick="0.01",
                                                   bboExchange="9c0001",
                                                   snapshotPermissions=permissions))


def _resolve(s):
    return s.resolve_contract(TLT_QUERY)


def test_snapshot_with_ticks_but_no_end_returns_evidence(tws):
    def handler(srv, c, r):
        c.push(_mdt(r.reqId, 3))
        c.push(_req_params(r.reqId))
        c.push(_tick(r.reqId, 66, 88.10))
        c.push(_tick(r.reqId, 67, 88.12))
        # no TickSnapshotEnd

    tws.on["REQ_MKT_DATA"] = handler
    with session() as s:
        q = s.quote_snapshot(_resolve(s), timeout=0.5)
    status, reason = models.assess_quote_attempts((q,))
    assert q.route == "snapshot" and q.snapshot_end_missing and "SNAPSHOT_END_MISSING" in q.flags
    assert (q.bid, q.ask) == (88.10, 88.12) and q.quality is QuoteQuality.DELAYED
    assert q.tick_counts == {"DELAYED_BID": 1, "DELAYED_BID_SIZE": 1, "DELAYED_ASK": 1,
                             "DELAYED_ASK_SIZE": 1}
    assert q.market_data_types_delivered == (3,)
    assert q.tick_req_params == {"minTick": 0.01, "bboExchange": "9c0001",
                                 "snapshotPermissions": 3}
    assert q.first_tick_ms is not None and q.last_tick_ms >= q.first_tick_ms
    assert status == "PASS_WITH_WARNINGS" and "DELAYED" in reason
    assert "QUOTE_AGE_UNKNOWN" in q.flags and q.quote_age_seconds is None
    assert tws.names().count("CANCEL_MKT_DATA") == 1  # unended snapshot cancelled


def _live_stream(srv, c, r, last_ts=None):
    c.push(_mdt(r.reqId, 1))
    c.push(_tick(r.reqId, 1, 88.00))
    c.push(_tick(r.reqId, 2, 88.02))
    c.push(_tick(r.reqId, 4, 88.01))
    if last_ts is not None:
        c.push(frame(IN.TICK_STRING, TickString(reqId=r.reqId, tickType=45, value=str(last_ts))))


def test_default_quote_is_one_stream_that_ends_early_once_complete(tws):
    last_ts = int(time.time()) - 90
    tws.on["REQ_MKT_DATA"] = lambda srv, c, r: _live_stream(srv, c, r, last_ts)
    with session(stream_seconds=5.0, stream_settle_seconds=0.3) as s:
        contract = _resolve(s)
        t0 = time.monotonic()
        report = s.quote(contract)
        elapsed = time.monotonic() - t0
    (q,) = report.attempts
    assert q.route == "stream" and q.ended_early and q.window_ms == 5000
    assert 0.3 <= elapsed < 2.0 and q.waited_ms < 2000
    assert q.quality is QuoteQuality.LIVE and report.status == "PASS"
    assert 89 <= q.quote_age_seconds < 95 and "QUOTE_AGE_UNKNOWN" not in q.flags
    assert [r.snapshot for n, r in tws.requests if n == "REQ_MKT_DATA"] == [False]
    assert tws.names().count("CANCEL_MKT_DATA") == 1  # a stream is always cancelled


def test_stream_without_last_waits_its_window_and_age_is_unknown(tws):
    def handler(srv, c, r):
        c.push(_mdt(r.reqId, 3))
        c.push(_tick(r.reqId, 66, 88.10))
        c.push(_tick(r.reqId, 67, 88.12))  # no last, no last timestamp

    tws.on["REQ_MKT_DATA"] = handler
    with session(stream_settle_seconds=0.1) as s:
        report = s.quote(_resolve(s), stream_seconds=0.6)
    (q,) = report.attempts
    assert not q.ended_early and q.waited_ms >= 600
    assert q.quote_age_seconds is None and "QUOTE_AGE_UNKNOWN" in q.flags
    assert report.status == "PASS_WITH_WARNINGS" and "CANCEL_MKT_DATA" in tws.names()


def test_stream_without_market_data_type_does_not_end_early(tws):
    def handler(srv, c, r):
        for tick, price in ((1, 88.0), (2, 88.02), (4, 88.01)):
            c.push(_tick(r.reqId, tick, price))

    tws.on["REQ_MKT_DATA"] = handler
    with session(stream_settle_seconds=0.1) as s:
        q = s.quote_stream_sample(_resolve(s), seconds=0.5)
    assert not q.ended_early and q.waited_ms >= 500


def test_empty_stream_then_requested_snapshot_success(tws):
    def handler(srv, c, r):
        if not r.snapshot:
            return
        _live_stream(srv, c, r)
        c.push(frame(IN.TICK_SNAPSHOT_END, TickSnapshotEnd(reqId=r.reqId)))

    tws.on["REQ_MKT_DATA"] = handler
    with session() as s:
        report = s.quote(_resolve(s), stream_seconds=0.4, snapshot_fallback=True)
    stream, snap = report.attempts
    assert stream.route == "stream" and stream.ticks_received == 0 and not stream.ended_early
    assert snap.route == "snapshot" and not snap.snapshot_end_missing
    assert snap.quality is QuoteQuality.LIVE
    assert report.status == "PASS" and "snapshot" in report.reason
    reqs = [r for n, r in tws.requests if n == "REQ_MKT_DATA"]
    assert [r.snapshot for r in reqs] == [False, True]
    assert tws.names().count("CANCEL_MKT_DATA") == 1  # stream always cancelled; ended snapshot not


def test_snapshot_is_not_tried_unless_asked(tws):
    tws.on["REQ_MKT_DATA"] = lambda srv, c, r: None
    with session() as s:
        report = s.quote(_resolve(s), stream_seconds=0.3)
    assert [a.route for a in report.attempts] == ["stream"] and report.status == "FAIL"
    assert [r.snapshot for n, r in tws.requests if n == "REQ_MKT_DATA"] == [False]


def test_nothing_on_both_routes(tws):
    tws.on["REQ_MKT_DATA"] = lambda srv, c, r: None
    with session() as s:
        report = s.quote(_resolve(s), snapshot_timeout=0.3, stream_seconds=0.3,
                         snapshot_fallback=True)
    assert report.status == "FAIL" and "no market data delivered" in report.reason
    stream, snap = report.attempts
    assert snap.snapshot_end_missing and not stream.snapshot_end_missing
    assert snap.ticks_received == stream.ticks_received == 0
    assert snap.waited_ms >= 300 and stream.waited_ms >= 300
    assert tws.names().count("CANCEL_MKT_DATA") == 2


def test_delayed_tick_types_only_are_all_mapped(tws):
    def handler(srv, c, r):
        c.push(_mdt(r.reqId, 4))
        c.push(_tick(r.reqId, 66, 88.10))  # DELAYED_BID (+ DELAYED_BID_SIZE)
        c.push(_tick(r.reqId, 67, 88.12))  # DELAYED_ASK (+ DELAYED_ASK_SIZE)
        c.push(_tick(r.reqId, 68, 88.11))  # DELAYED_LAST (+ DELAYED_LAST_SIZE)
        c.push(_tick(r.reqId, 75, 87.90))  # DELAYED_CLOSE
        c.push(_tick(r.reqId, 72, 88.50))  # DELAYED_HIGH
        c.push(frame(IN.TICK_STRING, TickString(reqId=r.reqId, tickType=88,
                                                value="1794412800")))
        c.push(frame(IN.TICK_SNAPSHOT_END, TickSnapshotEnd(reqId=r.reqId)))

    tws.on["REQ_MKT_DATA"] = handler
    with session() as s:
        q = s.quote_snapshot(_resolve(s))
    assert (q.bid, q.ask, q.last, q.close) == (88.10, 88.12, 88.11, 87.90)
    assert (str(q.bid_size), str(q.ask_size), str(q.last_size)) == ("100", "100", "100")
    assert q.last_trade_time.startswith("2026-11-11T16:00")
    assert q.quality is QuoteQuality.FROZEN and q.market_data_type_name == "DELAYED_FROZEN"
    assert set(q.tick_counts) == {"DELAYED_BID", "DELAYED_BID_SIZE", "DELAYED_ASK",
                                  "DELAYED_ASK_SIZE", "DELAYED_LAST", "DELAYED_LAST_SIZE",
                                  "DELAYED_CLOSE", "DELAYED_HIGH", "DELAYED_LAST_TIMESTAMP"}
    assert "MIXED_TICK_TYPES" not in q.flags


def test_live_two_sided_quote_check_is_plain_pass(tws, tmp_path):
    def handler(srv, c, r):
        c.push(_mdt(r.reqId, 1))
        c.push(_tick(r.reqId, 1, 88.00))
        c.push(_tick(r.reqId, 2, 88.02))
        c.push(frame(IN.TICK_SNAPSHOT_END, TickSnapshotEnd(reqId=r.reqId)))

    tws.on["REQ_MKT_DATA"] = handler
    cfg_path = tmp_path / "paper.toml"
    cfg_path.write_text("".join(f"{k} = {json.dumps(v)}\n" for k, v in CFG_DATA.items()))
    os.chmod(cfg_path, 0o600)
    report = run_check(cfg_path, session_factory=lambda cfg: session())
    assert report["overall"] == "PASS", report["steps"]


# --- S-A: the connection-level choke point -----------------------------------------------------
def test_reviewer_probe_unbound_base_send_is_refused_at_connection(tws):
    """Reviewer's probe: EClient.sendMsgProtoBuf(client, PLACE_ORDER/UPDATE_CONFIG, real payload)."""
    from ibapi.client_utils import createPlaceOrderRequestProto
    from ibapi.contract import Contract
    from ibapi.order import Order
    from ibapi.protobuf.UpdateConfigRequest_pb2 import UpdateConfigRequest

    s = session()
    s.connect()
    before = list(tws.requests)
    order_payload = createPlaceOrderRequestProto(1, Contract(), Order()).SerializeToString()
    for mid, payload in ((OUT.PLACE_ORDER, order_payload),
                         (OUT.UPDATE_CONFIG, UpdateConfigRequest(reqId=1).SerializeToString())):
        with pytest.raises(ReadOnlyViolation):
            EClient.sendMsgProtoBuf(s._client, mid + PROTOBUF_MSG_ID, payload)
        with pytest.raises(ReadOnlyViolation):
            EClient.sendMsg(s._client, mid, "1\0")
        with pytest.raises(ReadOnlyViolation):
            s._client.conn.sendMsg(comm.make_msg_proto(mid + PROTOBUF_MSG_ID, payload))
    assert tws.requests == before and not any(n.startswith("OUT_") for n in tws.names())
    s.disconnect()


def test_every_disallowed_id_refused_by_unbound_base_sends(tws):
    s = session()
    s.connect()
    before = list(tws.requests)
    out_ids = {v for k, v in vars(OUT).items() if not k.startswith("_") and isinstance(v, int)}
    for mid in sorted(out_ids - ALLOWED_OUT_MESSAGES):
        with pytest.raises(ReadOnlyViolation):
            EClient.sendMsgProtoBuf(s._client, mid + PROTOBUF_MSG_ID, b"")
        with pytest.raises(ReadOnlyViolation):
            EClient.sendMsg(s._client, mid, "")
    assert tws.requests == before
    s.disconnect()


def test_conn_is_always_guarded():
    from tradingbot.ibkr.session import _GuardedConnection, _ReadOnlyClient

    c = _ReadOnlyClient(None)
    assert c.conn is None
    c.conn = object()
    assert isinstance(c.conn, _GuardedConnection)


def test_second_handshake_frame_is_refused(tws):
    s = session()
    s.connect()
    with pytest.raises(ReadOnlyViolation):
        s._client.conn.sendMsg(b"API\0" + comm.make_initial_msg("v100..226"))
    s.disconnect()


# --- S-B / config response changes ----------------------------------------------------------
def test_later_differing_config_response_poisons(tws):
    from ibapi.protobuf.ApiConfig_pb2 import ApiConfig
    from ibapi.protobuf.ApiSettingsConfig_pb2 import ApiSettingsConfig
    from ibapi.protobuf.ConfigResponse_pb2 import ConfigResponse

    changed = ConfigResponse(reqId=1, api=ApiConfig(settings=ApiSettingsConfig(
        readOnlyApi=False, socketPort=7497, allowLocalhostOnly=True)))
    tws.on["REQ_POSITIONS"] = lambda srv, c, r: c.push(frame(IN.CONFIG_RESPONSE, changed))
    s = session()
    s.connect()
    with pytest.raises(E.ConfigResponseChanged):
        s.positions(timeout=2)
    assert s.poisoned
    assert_closed(s)


def test_second_differing_config_response_during_connect_fails(tws):
    def twice(srv, c, r):
        FakeTws.config_handler(srv, c, r)
        srv.api_settings = {**srv.api_settings, "allowLocalhostOnly": False}
        FakeTws.config_handler(srv, c, r)

    tws.on["REQ_CONFIG"] = twice
    s = session()
    with pytest.raises(E.ConfigResponseChanged):
        s.connect()
    assert_closed(s)
    assert not DATA_REQUESTS & set(tws.names())


def test_repeated_identical_config_response_is_ignored(tws):
    def twice(srv, c, r):
        FakeTws.config_handler(srv, c, r)
        FakeTws.config_handler(srv, c, r)

    tws.on["REQ_CONFIG"] = twice
    with session() as s:
        assert s.positions() is not None
        assert not s.poisoned


def test_malformed_frames_are_refused(tws):
    s = session()
    s.connect()
    before = list(tws.requests)
    good = comm.make_msg_proto(OUT.REQ_POSITIONS + PROTOBUF_MSG_ID, b"")
    for bad in (good[:6], good + b"x", b"\0\0\0\x09" + good[4:]):
        with pytest.raises(ReadOnlyViolation):
            s._client.conn.sendMsg(bad)
    assert tws.requests == before
    s.disconnect()


# --- no attribute path from a session reaches an unfiltered send ----------------------------------
SEND_LIKE = re.compile(r"(?:^|_)(?:send|write)|Send|Write")  # not "contractDetailsEnd"
_LEAVES = (str, bytes, bytearray, int, float, complex, bool, type(None), Decimal, memoryview)
# Not followed: classes and modules (anything in them is reachable by ``import`` anyway, as are
# ``socket`` and ``gc``), function/frame globals and builtins (the same thing by another name), and
# the process's stdio streams (``Thread._stderr`` is ``sys.stderr``, not a broker socket). Code
# objects hold only constants.
_NOT_FOLLOWED_TYPES = (type, types.ModuleType, io.IOBase, types.CodeType)
# ``f_back`` climbs from a handed-out traceback into the CALLER's own stack (here: pytest and the
# test, which holds the fake server); the package's frames are reached through ``tb_next``.
_NOT_FOLLOWED_ATTRS = {"f_globals", "f_builtins", "f_back"}
# Dunder attributes that hold references (the rest are slot wrappers of the type's protocol).
_DUNDERS = ("__self__", "__func__", "__closure__", "__wrapped__", "__defaults__", "__kwdefaults__",
            "__traceback__", "__context__", "__cause__", "__notes__", "__args__",
            "__callback__")
MAX_DEPTH = 16


def _children(obj):
    """(edge, child) for every reference reachable by attribute traversal from ``obj``."""
    if isinstance(obj, dict):
        for k, v in list(obj.items()):
            yield f"[{k!r}]", v
            yield f"<key {k!r}>", k
    elif isinstance(obj, (list, tuple, set, frozenset, collections.deque)):
        for i, v in enumerate(list(obj)):
            yield f"[{i}]", v
    try:
        d = object.__getattribute__(obj, "__dict__")
    except Exception:  # noqa: BLE001
        d = None
    if isinstance(d, dict):
        for k, v in list(d.items()):
            yield f".__dict__[{k!r}]", v
    for klass in type(obj).__mro__:
        slots = klass.__dict__.get("__slots__", ())
        for slot in (slots,) if isinstance(slots, str) else slots:
            try:
                yield f".<slot {slot}>", object.__getattribute__(obj, slot)
            except Exception:  # noqa: BLE001, S112 - unset slot
                continue
    if isinstance(obj, types.CellType):
        try:
            yield ".cell_contents", obj.cell_contents
        except ValueError:
            pass
    if isinstance(obj, weakref.ref):
        yield "()", obj()
        yield ".__callback__", obj.__callback__
    try:
        refs = weakref.getweakrefs(obj)  # reverse weak references and their callbacks
    except TypeError:
        refs = []
    for i, ref in enumerate(refs):
        yield f"<getweakrefs[{i}]>", ref
    if isinstance(obj, (types.FunctionType, types.MethodType)):
        fn = getattr(obj, "__func__", obj)
        yield ".__defaults__", fn.__defaults__
        yield ".__kwdefaults__", fn.__kwdefaults__
        yield ".__closure__", fn.__closure__
    for name in dir(obj):
        if name in _NOT_FOLLOWED_ATTRS or (name.startswith("__") and name not in _DUNDERS):
            continue
        try:
            yield f".{name}", getattr(obj, name)  # properties, bound methods, __self__, __func__,
        except Exception:  # noqa: BLE001, S112   # __closure__, __wrapped__, __traceback__, f_locals
            continue


def _walk(*roots):
    """Breadth-first over the attribute graph, with cycle protection (objects are kept alive so
    ``id`` is never reused during the walk)."""
    seen, keep, out = set(), [], []
    frontier = [(f"root{i}", r) for i, r in enumerate(roots)]
    for _depth in range(MAX_DEPTH):
        nxt = []
        for path, obj in frontier:
            if id(obj) in seen:
                continue
            seen.add(id(obj))
            keep.append(obj)
            out.append((path, obj))
            if isinstance(obj, (*_LEAVES, *_NOT_FOLLOWED_TYPES)):
                continue
            nxt.extend((path + edge, child) for edge, child in _children(obj))
        frontier = nxt
    return out


def _hostile_frames():
    from ibapi.client_utils import createPlaceOrderRequestProto
    from ibapi.contract import Contract
    from ibapi.order import Order
    from ibapi.protobuf.UpdateConfigRequest_pb2 import UpdateConfigRequest

    place = createPlaceOrderRequestProto(1, Contract(), Order()).SerializeToString()
    update = UpdateConfigRequest(reqId=1).SerializeToString()
    calls = []
    for mid, payload in ((OUT.PLACE_ORDER, place), (OUT.UPDATE_CONFIG, update)):
        framed = comm.make_msg_proto(mid + PROTOBUF_MSG_ID, payload)
        calls += [(framed,), (mid + PROTOBUF_MSG_ID, payload), (mid, "1\0"),
                  (comm.make_msg(mid, True, "1\0"),)]
    return calls


def _fire_send_like(reachable):
    """Call every reachable callable whose name looks like a send or write with each hostile
    frame, in each argument shape the SDK's send primitives take. Returns (path, callable) pairs."""
    called = []
    for path, obj in reachable:
        if isinstance(obj, _NOT_FOLLOWED_TYPES) or not callable(obj):
            continue
        last = re.split(r"[.\[]", path)[-1]
        if not (SEND_LIKE.search(last) or SEND_LIKE.search(getattr(obj, "__name__", "") or "")):
            continue
        called.append((path, obj))
        for args in _hostile_frames():
            try:
                obj(*args)
            except KeyboardInterrupt:
                raise
            except BaseException:  # noqa: BLE001, S110 - refused, wrong shape: all fine
                pass
    return called


def _assert_no_raw_transport(reachable):
    from tradingbot.ibkr.session import _GuardedConnection

    raw = [p for p, o in reachable if isinstance(o, (_ibkr_fake_tws.FakeConnection,
                                                      socket.socket))]
    assert raw == [], f"raw transport reachable: {raw[:5]}"
    assert any(isinstance(o, _GuardedConnection) for _, o in reachable), "walk never hit the guard"


def _caught(fn):
    """The exception ``fn`` raises, caught here so its traceback starts in package code (a
    ``pytest.raises`` traceback would include this test's own frame and its ``tws`` fixture)."""
    try:
        fn()
    except BaseException as exc:  # noqa: BLE001
        return exc
    raise AssertionError("expected an exception")


def test_object_graph_of_connected_session_reaches_no_unfiltered_send(tws):
    s = session()
    s.connect()
    tws.fail_send = {"REQ_POSITIONS"}  # also walk an exception the package hands out
    exc = _caught(s.positions)
    tws.fail_send = set()
    assert isinstance(exc, E.IbkrRequestError)
    reachable = _walk(s, exc)
    assert len(reachable) > 1000
    _assert_no_raw_transport(reachable)
    wire, requests = list(tws.wire), list(tws.requests)
    owners = {id(s._client): "client", id(s._client.conn): "conn"}
    called = {(owners.get(id(getattr(fn, "__self__", None))), fn.__name__)
              for _, fn in _fire_send_like(reachable)}
    assert {("client", "sendMsg"), ("client", "sendMsgProtoBuf"), ("conn", "sendMsg")} <= called
    assert s._client.reader.conn is s._client.conn
    assert tws.wire == wire and tws.requests == requests
    assert s._client.isConnected() and len(s.positions()) == 1  # still a working read session
    s.disconnect()
    assert_closed(s)


def test_reviewer_weakref_probe_finds_nothing(tws):
    """Merge review probe: weakref.getweakrefs(conn)[0].__callback__.__defaults__[0]() once
    returned the raw-connection mapping. The guard is now not weak-referenceable at all."""
    s = session()
    s.connect()
    conn = s._client.conn
    assert weakref.getweakrefs(conn) == []
    with pytest.raises(TypeError):
        weakref.ref(conn)
    assert not hasattr(conn, "__dict__") and type(conn._token) is int
    s.disconnect()


def test_raw_connection_entry_is_dropped_with_its_guard(tws):
    import gc

    from tradingbot.ibkr import session as sess

    before = set(sess._RAW_CONNECTIONS)
    s = session()
    s.connect()
    token = s._client.conn._token
    assert token in sess._RAW_CONNECTIONS
    s.disconnect()
    del s
    gc.collect()
    assert token not in sess._RAW_CONNECTIONS and set(sess._RAW_CONNECTIONS) <= before


def test_exception_from_raw_send_carries_no_raw_frame(tws):
    """An error raised inside the transport's send surfaces with its traceback and context cut at
    the guard, so its frames' locals cannot lead to the raw connection."""
    s = session()
    s.connect()
    tws.fail_send = {"REQ_POSITIONS"}
    frame_ = comm.make_msg_proto(OUT.REQ_POSITIONS + PROTOBUF_MSG_ID, b"")
    exc = _caught(lambda: s._client.conn.sendMsg(frame_))
    tws.fail_send = set()
    assert isinstance(exc, OSError) and exc.__context__ is None
    _assert_no_raw_transport(_walk(exc, s))
    s.disconnect()
