"""Contract discovery and option-chain parameters through the REAL ibapi 1052.1 client/decoder over
the fake transport (``tests/_ibkr_fake_tws.py``). No socket is opened. Synthetic data only."""
from __future__ import annotations

import json
import os
import socket
from decimal import Decimal

import pytest

pytest.importorskip("ibapi")

import ibapi.client
from _ibkr_fake_tws import (
    OTHER_PAPER,
    PAPER,
    FakeTws,
    contract_end_frame,
    contract_frame,
    error_frame,
    frame,
    opt_param_end_frame,
    opt_param_frame,
    tlt_contract_frame,
)
from ibapi.message import IN, OUT
from ibapi.protobuf.ManagedAccounts_pb2 import ManagedAccounts

from tradingbot.ibkr import errors as E
from tradingbot.ibkr.config import validate_config
from tradingbot.ibkr.discovery import (
    CATALOG_SCHEMA,
    COMPLETE,
    exit_code,
    parse_plan,
    run_discovery,
)
from tradingbot.ibkr.models import TLT_QUERY, ContractQuery
from tradingbot.ibkr.session import (
    ALLOWED_CLIENT_METHODS,
    ALLOWED_OUT_MESSAGES,
    IbkrSession,
    SessionState,
)

CFG_DATA = {"mode": "paper", "host": "127.0.0.1", "port": 7497, "client_id": 7, "account": PAPER,
            "tws_profile_ini": "/Users/someone/Jts/jts.ini", "tws_os_user": "someone",
            "read_only_check": "config-read"}  # synthetic
CFG = validate_config(CFG_DATA)
ES = ContractQuery(symbol="ES", sec_type="FUT", exchange="CME")


@pytest.fixture(params=[219, 226], ids=lambda v: f"sv{v}")
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


def session(**kw) -> IbkrSession:
    opts = {"connect_timeout": 2.0, "request_timeout": 2.0, "shutdown_timeout": 3.0,
            "preflight": lambda cfg: {"stub": True}}
    opts.update(kw)
    return IbkrSession(CFG, **opts)


def assert_closed(s: IbkrSession) -> None:
    assert s.state is SessionState.DISCONNECTED and s.threads_alive() == []


def es_month(rid: int, con_id: int, ltd: str, **kw) -> bytes:
    return contract_frame(rid, con_id=con_id, symbol="ES", sec_type="FUT", exchange="CME",
                          ltd=ltd, multiplier=50.0, local_symbol=f"ES{ltd[4:6]}",
                          trading_class="ES", contract_month=ltd[:6], real_expiration=ltd,
                          under_con_id=11004968, long_name="E-mini S&P 500", **kw)


# --- allowlist: exactly one new outgoing id, and it is a pure read -------------------------------
def test_allowlist_adds_only_the_option_parameters_read():
    baseline = {OUT.START_API, OUT.REQ_CONTRACT_DATA, OUT.CANCEL_CONTRACT_DATA,
                OUT.REQ_ACCOUNT_SUMMARY, OUT.CANCEL_ACCOUNT_SUMMARY, OUT.REQ_POSITIONS,
                OUT.CANCEL_POSITIONS, OUT.REQ_MKT_DATA, OUT.CANCEL_MKT_DATA,
                OUT.REQ_MARKET_DATA_TYPE, OUT.REQ_CONFIG}
    assert ALLOWED_OUT_MESSAGES == baseline | {OUT.REQ_SEC_DEF_OPT_PARAMS}
    assert OUT.REQ_SEC_DEF_OPT_PARAMS == 78
    assert {"reqSecDefOptParams", "reqSecDefOptParamsProtoBuf"} <= ALLOWED_CLIENT_METHODS
    # the SDK defines no cancel for this request
    assert not [k for k in vars(OUT) if "SEC_DEF" in k and k != "REQ_SEC_DEF_OPT_PARAMS"]


# --- discover_contracts -------------------------------------------------------------------------
def test_several_rows_then_end_maps_every_field_and_sends_query_fields(tws):
    def handler(srv, c, r):
        c.push(es_month(r.reqId, 3, "20270618", size_rules=("1", "1", "1")))
        c.push(es_month(r.reqId, 1, "20261218 08:30 US/Central", size_rules=("1", "1", "1")))
        c.push(es_month(r.reqId, 2, "20270319"))
        c.push(contract_end_frame(r.reqId))

    tws.on["REQ_CONTRACT_DATA"] = handler
    q = ContractQuery(symbol="ES", sec_type="FUT", exchange="CME", multiplier="50",
                      trading_class="ES", last_trade_date_or_contract_month="2026",
                      include_expired=False)
    with session() as s:
        r = s.discover_contracts(q, limit=10)
    assert r.status == "FOUND" and r.count == 3 and not r.truncated and r.error_code is None
    assert [k.con_id for k in r.contracts] == [1, 2, 3]  # sorted by expiry
    first = r.contracts[0]
    assert first.last_trade_date_or_contract_month == "20261218"
    assert first.last_trade_time == "08:30"  # the SDK splits the time part off
    assert (first.multiplier, first.min_tick, first.price_magnifier) == ("50", 0.25, 1)
    assert first.contract_month == "202612"
    assert first.real_expiration_date == "20261218 08:30 US/Central"
    assert (first.under_con_id, first.trading_class, first.local_symbol) == (11004968, "ES", "ES12")
    assert first.min_size == first.size_increment == first.suggested_size_increment == Decimal(1)
    assert first.strike is None and first.right == "" and first.query_match
    assert first.order_types == "LMT,MKT" and first.valid_exchanges == "CME"
    assert first.market_rule_ids == "67" and first.time_zone_id == "US/Central"
    assert r.contracts[1].min_size is None  # broker left the size rules unset
    sent = next(p for n, p in tws.requests if n == "REQ_CONTRACT_DATA").contract
    assert (sent.symbol, sent.secType, sent.exchange, sent.multiplier, sent.tradingClass,
            sent.lastTradeDateOrContractMonth) == ("ES", "FUT", "CME", 50.0, "ES", "2026")
    assert not sent.HasField("strike") and not sent.includeExpired
    assert "CANCEL_CONTRACT_DATA" not in tws.names()  # ended normally
    assert_closed(s)


def test_option_query_fields_reach_the_wire(tws):
    def handler(srv, c, r):
        c.push(contract_frame(r.reqId, con_id=9, symbol="SPY", sec_type="OPT", exchange="SMART",
                              ltd="20261218", multiplier=100.0, strike=600.0, right="C",
                              min_tick="0.01"))
        c.push(contract_end_frame(r.reqId))

    tws.on["REQ_CONTRACT_DATA"] = handler
    q = ContractQuery(symbol="SPY", sec_type="OPT", last_trade_date_or_contract_month="20261218",
                      strike=600.0, right="C", include_expired=True)
    with session() as s:
        r = s.discover_contracts(q)
    assert r.contracts[0].strike == 600.0 and r.contracts[0].right == "C"
    sent = next(p for n, p in tws.requests if n == "REQ_CONTRACT_DATA").contract
    assert (sent.strike, sent.right, sent.includeExpired) == (600.0, "C", True)


def test_zero_rows_with_not_found_error_is_a_normal_result(tws):
    tws.on["REQ_CONTRACT_DATA"] = lambda srv, c, r: c.push(
        error_frame(r.reqId, 200, "No security definition has been found for the request"))
    with session() as s:
        r = s.discover_contracts(ContractQuery(symbol="NOPE", sec_type="FUT", exchange="NYMEX"))
        assert r.status == "NOT_FOUND" and r.count == 0 and r.error_code == 200
        assert "No security definition" in r.error_message
        assert s.state is SessionState.READY
        tws.on["REQ_CONTRACT_DATA"] = lambda srv, c, r: (c.push(tlt_contract_frame(r.reqId)),
                                                          c.push(contract_end_frame(r.reqId)))
        assert s.discover_contracts(TLT_QUERY).status == "FOUND"  # batch continues


def test_empty_end_is_not_found_and_other_errors_are_error_rows(tws):
    tws.on["REQ_CONTRACT_DATA"] = lambda srv, c, r: c.push(contract_end_frame(r.reqId))
    with session() as s:
        r = s.discover_contracts(TLT_QUERY)
        assert (r.status, r.error_code) == ("NOT_FOUND", None)
        tws.on["REQ_CONTRACT_DATA"] = lambda srv, c, r: c.push(
            error_frame(r.reqId, 321, f"Error validating request for {PAPER}"))
        r = s.discover_contracts(TLT_QUERY)
        assert (r.status, r.error_code) == ("ERROR", 321) and PAPER not in r.error_message


def test_truncation_cancels_and_says_so(tws):
    def handler(srv, c, r):
        for i in range(7):
            c.push(es_month(r.reqId, 100 + i, f"2027{i + 1:02d}15"))
        # no end: the session must stop on its own once the limit is exceeded

    tws.on["REQ_CONTRACT_DATA"] = handler
    with session() as s:
        r = s.discover_contracts(ES, limit=3, timeout=2.0)
        assert r.truncated and r.count == 3 and r.limit == 3 and r.status == "FOUND"
        assert "CANCEL_CONTRACT_DATA" in tws.names() and not s.cleanup_failures
    with pytest.raises(ValueError):
        s.discover_contracts(ES, limit=0)


def test_timeout_raises_and_cancels(tws):
    tws.on["REQ_CONTRACT_DATA"] = lambda srv, c, r: None
    with session() as s:
        with pytest.raises(E.IbkrTimeout):
            s.discover_contracts(ES, timeout=0.3)
        assert "CANCEL_CONTRACT_DATA" in tws.names() and s.state is SessionState.READY


# --- identity gate and poison ---------------------------------------------------------------------
def test_foreign_account_gate_sends_no_discovery_request(tws):
    tws.managed_accounts = OTHER_PAPER
    s = session()
    with pytest.raises(E.AccountIdentityError):
        s.connect()
    with pytest.raises(E.AccountIdentityError):
        s.discover_contracts(ES)
    assert not {"REQ_CONTRACT_DATA", "REQ_SEC_DEF_OPT_PARAMS"} & set(tws.names())
    assert_closed(s)


def test_poisoned_session_refuses_discovery_and_option_params(tws):
    tws.on["REQ_CONTRACT_DATA"] = lambda srv, c, r: c.push(
        frame(IN.MANAGED_ACCTS, ManagedAccounts(accountsList=OTHER_PAPER)))
    s = session()
    s.connect()
    with pytest.raises(E.AccountIdentityError):
        s.discover_contracts(ES, timeout=2)
    assert s.poisoned
    assert_closed(s)
    before = list(tws.names())
    with pytest.raises(E.AccountIdentityError):
        s.discover_contracts(ES)
    with pytest.raises(E.AccountIdentityError):
        s.option_chain_params(type("U", (), {"con_id": 1, "symbol": "SPY", "sec_type": "STK"}))
    assert tws.names() == before


# --- option-chain parameters ---------------------------------------------------------------------
SPY_U = type("U", (), {"con_id": 756733, "symbol": "SPY", "sec_type": "STK"})


def test_option_chain_params_rows_sorted_and_request_fields(tws):
    def handler(srv, c, r):
        c.push(opt_param_frame(r.reqId, exchange="SMART", under_con_id=756733,
                               trading_class="SPY", multiplier="100",
                               expirations=["20261218", "20261120"], strikes=[610.0, 590.0]))
        c.push(opt_param_frame(r.reqId, exchange="CBOE", under_con_id=756733,
                               trading_class="SPY", multiplier="100",
                               expirations=["20261120"], strikes=[600.0]))
        c.push(opt_param_end_frame(r.reqId))

    tws.on["REQ_SEC_DEF_OPT_PARAMS"] = handler
    with session() as s:
        r = s.option_chain_params(SPY_U)
    assert r.status == "FOUND" and not r.truncated and r.underlying_con_id == 756733
    assert [p.exchange for p in r.params] == ["CBOE", "SMART"]
    smart = r.params[1]
    assert smart.expirations == ("20261120", "20261218") and smart.strikes == (590.0, 610.0)
    assert (smart.multiplier, smart.trading_class) == ("100", "SPY")
    req = next(p for n, p in tws.requests if n == "REQ_SEC_DEF_OPT_PARAMS")
    assert (req.underlyingSymbol, req.underlyingSecType, req.underlyingConId) == (
        "SPY", "STK", 756733)
    assert not req.HasField("futFopExchange")  # "" = all exchanges
    assert_closed(s)


def test_option_chain_params_not_found_truncated_and_timeout(tws):
    tws.on["REQ_SEC_DEF_OPT_PARAMS"] = lambda srv, c, r: c.push(
        error_frame(r.reqId, 200, "No security definition has been found for the request"))
    with session() as s:
        r = s.option_chain_params(SPY_U)
        assert (r.status, r.error_code, r.params) == ("NOT_FOUND", 200, ())
        tws.on["REQ_SEC_DEF_OPT_PARAMS"] = lambda srv, c, r: c.push(opt_param_end_frame(r.reqId))
        assert s.option_chain_params(SPY_U).status == "NOT_FOUND"

        def many(srv, c, r):
            for i in range(4):
                c.push(opt_param_frame(r.reqId, exchange=f"X{i}", under_con_id=1,
                                       trading_class="SPY", multiplier="100",
                                       expirations=["20261218"], strikes=[1.0]))
            c.push(opt_param_end_frame(r.reqId))

        tws.on["REQ_SEC_DEF_OPT_PARAMS"] = many
        r = s.option_chain_params(SPY_U, limit=2)
        assert r.truncated and len(r.params) == 2
        tws.on["REQ_SEC_DEF_OPT_PARAMS"] = lambda srv, c, r: None
        with pytest.raises(E.IbkrTimeout):
            s.option_chain_params(SPY_U, timeout=0.3)
        assert s.state is SessionState.READY and not s.cleanup_failures
    # no cancel exists for this request, and nothing outside the allowlist was sent
    assert not any(n.startswith("OUT_") for n in tws.names())


# --- the runner end to end over the real SDK ------------------------------------------------------
def test_run_discovery_end_to_end_mixed_found_and_not_found(tws, tmp_path):
    def contracts(srv, c, r):
        sym = r.contract.symbol
        if sym == "ES":
            for i, ltd in enumerate(("20270319", "20261218", "20270618", "20270917")):
                c.push(es_month(r.reqId, 10 + i, ltd))
            c.push(contract_end_frame(r.reqId))
        elif sym == "SPY":
            c.push(contract_frame(r.reqId, con_id=756733, symbol="SPY", sec_type="STK",
                                  exchange="SMART", min_tick="0.01"))
            c.push(contract_end_frame(r.reqId))
        else:
            c.push(error_frame(r.reqId, 200, "No security definition has been found"))

    tws.on["REQ_CONTRACT_DATA"] = contracts
    tws.on["REQ_SEC_DEF_OPT_PARAMS"] = lambda srv, c, r: (
        c.push(opt_param_frame(r.reqId, exchange="SMART", under_con_id=756733,
                               trading_class="SPY", multiplier="100",
                               expirations=["20261218"], strikes=[600.0])),
        c.push(opt_param_end_frame(r.reqId)))
    plan = parse_plan({
        "schema": "tradingbot.ibkr.contract_discovery_queries/1", "pace_seconds": 0.0,
        "contract": [{"name": "ES fut", "symbol": "ES", "sec_type": "FUT", "exchange": "CME"},
                     {"name": "MNG fut", "symbol": "MNG", "sec_type": "FUT",
                      "exchange": "NYMEX"}],
        "option_chain": [{"name": "SPY chain", "symbol": "SPY"}]})
    cfg_path = tmp_path / "paper.toml"
    cfg_path.write_text("".join(f"{k} = {json.dumps(v)}\n" for k, v in CFG_DATA.items()))
    os.chmod(cfg_path, 0o600)
    cat = run_discovery(plan, cfg_path, session_factory=lambda cfg: session())
    assert cat["schema"] == CATALOG_SCHEMA and cat["overall"] == COMPLETE and exit_code(cat) == 0
    assert PAPER not in json.dumps(cat) and cat["account"] == "DU…01"
    rows = {r["name"]: r for r in cat["results"]}
    assert rows["ES fut"]["status"] == "FOUND" and rows["ES fut"]["count"] == 4
    assert [c["con_id"] for c in rows["ES fut"]["contracts"]] == [11, 10, 12, 13]
    assert rows["MNG fut"]["status"] == "NOT_FOUND" and rows["MNG fut"]["error_code"] == 200
    chain = rows["SPY chain"]
    assert chain["status"] == "FOUND" and chain["underlying_con_id"] == 756733
    assert cat["session"]["read_only_status"] == "VERIFIED" and cat["session"]["reader_joined"]
    json.dumps(cat)  # fully serialisable
