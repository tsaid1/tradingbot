"""Contract-discovery runner, query file and catalog. Must pass WITHOUT ibapi (fake session)."""
from __future__ import annotations

import importlib.util
import json
import os
import stat
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from tradingbot.ibkr import errors as E
from tradingbot.ibkr.check import write_report
from tradingbot.ibkr.config import validate_config
from tradingbot.ibkr.discovery import (
    CATALOG_PREFIX,
    CATALOG_SCHEMA,
    COMPLETE,
    FAILED,
    QUERIES_SCHEMA,
    QueryFileInvalid,
    exit_code,
    load_plan,
    nearest_unexpired,
    parse_plan,
    run_discovery,
    summarize,
)
from tradingbot.ibkr.models import (
    DiscoveredContract,
    DiscoveryResult,
    OptionChainParams,
    OptionChainResult,
)

ROOT = Path(__file__).resolve().parents[1]
ACCT = "DU9990001"  # synthetic
CFG = validate_config({"mode": "paper", "host": "127.0.0.1", "port": 7497, "client_id": 17,
                       "account": ACCT, "tws_profile_ini": "/Users/someone/Jts/jts.ini",
                       "tws_os_user": "someone", "read_only_check": "config-read"})


def contract(con_id: int, symbol: str = "ES", sec_type: str = "FUT",
             ltd: str = "") -> DiscoveredContract:
    return DiscoveredContract(
        con_id=con_id, symbol=symbol, local_symbol=symbol, sec_type=sec_type, exchange="CME",
        primary_exchange="", currency="USD", multiplier="50" if sec_type == "FUT" else "",
        min_tick=0.25, price_magnifier=1, trading_class=symbol,
        last_trade_date_or_contract_month=ltd, last_trade_date="", last_trade_time="",
        contract_month=ltd[:6], real_expiration_date=ltd, strike=None, right="",
        under_con_id=0, under_symbol="", under_sec_type="", long_name=f"{symbol} long",
        market_name=symbol, stock_type="", time_zone_id="US/Central", trading_hours="",
        liquid_hours="", valid_exchanges="CME", order_types="LMT", market_rule_ids="",
        min_size=Decimal(1), size_increment=Decimal(1), suggested_size_increment=None,
        query_match=True)


class FakeSession:
    sdk_version = "1052.1"

    def __init__(self, script=None, connect_exc=None, disconnect_exc=None):
        self.script = script or {}
        self.connect_exc, self.disconnect_exc = connect_exc, disconnect_exc
        self.calls: list[str] = []
        self.notices, self.connection_events, self.unrouted_errors = [], [], []
        self.cleanup_failures, self.internal_errors = [], []
        self.poisoned = False
        self.server_version, self.connection_time, self.connected_at = 226, "t", "t"
        self.preflight_result = {"stub": True}
        self.read_only_status, self.read_only_settings = "NOT_CHECKED", None
        self.reader_joined = True

    def connect(self):
        self.calls.append("connect")
        if self.connect_exc:
            raise self.connect_exc
        self.read_only_status = "VERIFIED"

    def disconnect(self):
        self.calls.append("disconnect")
        if self.disconnect_exc:
            raise self.disconnect_exc

    def threads_alive(self):
        return []

    def discover_contracts(self, query, *, limit, timeout):
        self.calls.append(f"discover:{query.symbol}")
        out = self.script.get(query.symbol)
        if isinstance(out, BaseException):
            raise out
        if out is None:
            return DiscoveryResult(query=query, status="NOT_FOUND", contracts=(), limit=limit,
                                   error_code=200, error_message="No security definition")
        rows = tuple(out[:limit])
        return DiscoveryResult(query=query, status="FOUND", contracts=rows, limit=limit,
                               truncated=len(out) > limit)

    def option_chain_params(self, underlying, *, fut_fop_exchange, limit, timeout):
        self.calls.append(f"chain:{underlying.con_id}")
        return OptionChainResult(
            underlying_symbol=underlying.symbol, underlying_sec_type=underlying.sec_type,
            underlying_con_id=underlying.con_id, fut_fop_exchange=fut_fop_exchange,
            status="FOUND", limit=limit, params=(OptionChainParams(
                "SMART", underlying.con_id, "SPY", "100", ("20261218",), (600.0,)),))


def plan(contracts, chains=(), **top):
    return parse_plan({"schema": QUERIES_SCHEMA, "pace_seconds": 0.5, **top,
                       "contract": list(contracts), "option_chain": list(chains)})


def run(p, fake, sleeps=None):
    return run_discovery(p, "unused", session_factory=lambda cfg: fake, loader=lambda _: CFG,
                         sleep=(sleeps.append if sleeps is not None else (lambda s: None)))


ES_ROWS = [contract(3, ltd="20270618"), contract(1, ltd="20261218"), contract(2, ltd="20270319"),
           contract(4, ltd="20260918")]


# --- query file --------------------------------------------------------------------------------
def test_default_query_file_parses_and_has_the_candidates():
    p = load_plan(ROOT / "config" / "contract_discovery.toml")
    symbols = {(q.query.symbol, q.query.sec_type, q.query.exchange) for q in p.queries}
    for want in [("TLT", "STK", "SMART"), ("SPY", "STK", "SMART"), ("GLD", "STK", "SMART"),
                 ("USO", "STK", "SMART"), ("UNG", "STK", "SMART"), ("SPX", "IND", "CBOE"),
                 ("ES", "FUT", "CME"), ("MES", "FUT", "CME"), ("CL", "FUT", "NYMEX"),
                 ("MCL", "FUT", "NYMEX"), ("NG", "FUT", "NYMEX"), ("GC", "FUT", "COMEX"),
                 ("MGC", "FUT", "COMEX"), ("XAUUSD", "CMDTY", "SMART"),
                 ("IBUS500", "CFD", "SMART")]:
        assert want in symbols, want
    assert all(q.query.currency == "USD" for q in p.queries)
    assert [c.underlying.symbol for c in p.option_chains] == ["SPY"]
    assert p.pace_seconds > 0 and p.limit <= 1000


@pytest.mark.parametrize("data,needle", [
    ({"schema": "x"}, "schema"),
    ({"schema": QUERIES_SCHEMA, "contract": [{"name": "a", "symbol": "A", "bogus": 1}]}, "unknown"),
    ({"schema": QUERIES_SCHEMA, "contract": [{"name": "a", "symbol": "A", "strike": "1"}]}, "type"),
    ({"schema": QUERIES_SCHEMA, "contract": [{"name": "a", "symbol": "A",
                                              "include_expired": 1}]}, "type"),
    ({"schema": QUERIES_SCHEMA, "contract": [{"symbol": "A"}]}, "required"),
    ({"schema": QUERIES_SCHEMA, "contract": [{"name": "a", "symbol": "A"},
                                             {"name": "a", "symbol": "B"}]}, "unique"),
    ({"schema": QUERIES_SCHEMA}, "no queries"),
    ({"schema": QUERIES_SCHEMA, "limit": 0, "contract": [{"name": "a", "symbol": "A"}]}, "limit"),
    ({"schema": QUERIES_SCHEMA, "pace_seconds": -1,
      "contract": [{"name": "a", "symbol": "A"}]}, "pace"),
    ({"schema": QUERIES_SCHEMA, "orders": [], "contract": [{"name": "a", "symbol": "A"}]},
     "top-level"),
])
def test_bad_query_files_are_refused(data, needle):
    with pytest.raises(QueryFileInvalid, match=needle):
        parse_plan(data)


# --- runner --------------------------------------------------------------------------------------
def test_mixed_found_not_found_and_timeout_rows_complete_and_pace():
    fake = FakeSession({"ES": ES_ROWS, "SPY": [contract(756733, "SPY", "STK")],
                        "SLOW": E.IbkrTimeout("contract (reqId 9001) timed out after 15.0s")})
    sleeps: list[float] = []
    p = plan([{"name": "ES", "symbol": "ES", "sec_type": "FUT", "exchange": "CME"},
              {"name": "MNG", "symbol": "MNG", "sec_type": "FUT", "exchange": "NYMEX"},
              {"name": "SLOW", "symbol": "SLOW"}],
             [{"name": "SPY chain", "symbol": "SPY"}])
    cat = run(p, fake, sleeps)
    assert cat["schema"] == CATALOG_SCHEMA and cat["overall"] == COMPLETE and exit_code(cat) == 0
    assert [r["status"] for r in cat["results"]] == ["FOUND", "NOT_FOUND", "TIMEOUT", "FOUND"]
    assert cat["status_counts"] == {"FOUND": 2, "NOT_FOUND": 1, "TIMEOUT": 1}
    assert cat["results"][1]["error_code"] == 200
    chain = cat["results"][3]
    assert chain["kind"] == "option_chain" and chain["underlying_con_id"] == 756733
    assert chain["params"][0]["strikes"] == [600.0]
    # a pause before every request but the first (4 contract-details + 1 option params = 5)
    assert sleeps == [0.5, 0.5, 0.5, 0.5]
    assert fake.calls[0] == "connect" and fake.calls[-1] == "disconnect"
    assert cat["account"] == "DU…01" and ACCT not in json.dumps(cat)
    assert cat["results"][0]["contracts"][0]["min_size"] == "1"  # Decimal -> str


def test_connect_failure_is_failed_and_runs_nothing():
    fake = FakeSession(connect_exc=E.AccountIdentityError("mismatch", reason="MISMATCH"))
    cat = run(plan([{"name": "ES", "symbol": "ES"}]), fake)
    assert cat["overall"] == FAILED and exit_code(cat) == 1 and "connect" in cat["failure"]
    assert not any(c.startswith("discover") for c in fake.calls)


def test_config_failure_is_failed_before_any_session():
    def loader(_):
        raise E.ConfigNotFound("no config")

    cat = run_discovery(plan([{"name": "ES", "symbol": "ES"}]), "x", loader=loader,
                        session_factory=lambda cfg: pytest.fail("session created"))
    assert cat["overall"] == FAILED and "ConfigNotFound" in cat["failure"]


@pytest.mark.parametrize("exc", [E.IbkrConnectionError("connection lost: 504"),
                                 E.AccountIdentityError("foreign", reason="POISONED"),
                                 E.CleanupError("contract cleanup failed", failures=["x"]),
                                 E.ReadOnlyViolation("refused")])
def test_safety_failure_mid_batch_stops_and_fails(exc):
    fake = FakeSession({"ES": ES_ROWS, "BAD": exc})
    cat = run(plan([{"name": "ES", "symbol": "ES"}, {"name": "BAD", "symbol": "BAD"},
                    {"name": "TLT", "symbol": "TLT"}]), fake)
    assert cat["overall"] == FAILED and exit_code(cat) == 1
    assert [r["status"] for r in cat["results"]] == ["FOUND", "SKIPPED", "SKIPPED"]
    assert "discover:TLT" not in fake.calls and fake.calls[-1] == "disconnect"


def test_disconnect_failure_fails():
    fake = FakeSession({"ES": ES_ROWS}, disconnect_exc=E.CleanupError("dirty", failures=["t"]))
    cat = run(plan([{"name": "ES", "symbol": "ES"}]), fake)
    assert cat["overall"] == FAILED and "disconnect" in cat["failure"]


def test_ambiguous_or_missing_underlying_sends_no_option_request():
    fake = FakeSession({"SPY": [contract(1, "SPY", "STK"), contract(2, "SPY", "STK")]})
    cat = run(plan([], [{"name": "SPY chain", "symbol": "SPY"},
                        {"name": "NOPE chain", "symbol": "NOPE"}]), fake)
    assert [r["status"] for r in cat["results"]] == ["AMBIGUOUS_UNDERLYING", "NOT_FOUND"]
    assert not any(c.startswith("chain") for c in fake.calls) and cat["overall"] == COMPLETE


def test_full_account_in_catalog_is_masked_and_fails():
    fake = FakeSession({"ES": [contract(1, ltd="20261218")]})
    fake.notices = [(-1, 2104, f"farm ok {ACCT}")]
    cat = run(plan([{"name": "ES", "symbol": "ES"}]), fake)
    assert ACCT not in json.dumps(cat) and cat["overall"] == FAILED


# --- catalog file and table -----------------------------------------------------------------------
def test_nearest_unexpired_and_summary_table():
    fake = FakeSession({"ES": ES_ROWS + [contract(5, ltd="20271217")],
                        "TLT": [contract(15547841, "TLT", "STK")]})
    cat = run(plan([{"name": "ES fut", "symbol": "ES"}, {"name": "TLT", "symbol": "TLT"},
                    {"name": "XX", "symbol": "XX"}], limit=10), fake)
    near = nearest_unexpired(cat["results"][0]["contracts"], date(2026, 11, 12))
    assert [c["con_id"] for c in near] == [1, 2, 3]  # 20260918 has expired; three nearest
    text = summarize(cat, today=date(2026, 11, 12))
    es_line = next(line for line in text.splitlines() if "ES fut" in line)
    assert "20261218 conId=1 x50 tick=0.25" in es_line and "conId=4" not in es_line
    assert "conId=5" not in es_line
    assert "NOT_FOUND" in text and "200 No security definition" in text
    assert "conId=15547841" in text and ACCT not in text


def test_truncated_row_is_flagged_in_table():
    fake = FakeSession({"ES": ES_ROWS})
    cat = run(plan([{"name": "ES", "symbol": "ES"}], limit=2), fake)
    assert cat["results"][0]["truncated"] and cat["results"][0]["count"] == 2
    assert "TRUNCATED at 2" in summarize(cat)


def test_catalog_file_name_mode_and_schema(tmp_path):
    cat = run(plan([{"name": "ES", "symbol": "ES"}]), FakeSession({"ES": ES_ROWS}))
    path = write_report(cat, tmp_path / "r", prefix=CATALOG_PREFIX)
    assert path.name.startswith("contract_catalog_") and path.suffix == ".json"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert json.loads(path.read_text())["schema"] == "tradingbot.ibkr.contract_catalog/1"
    # the check script's default file name is unchanged
    assert write_report({}, tmp_path / "r").name.startswith("ibkr_paper_check_")


def _script():
    spec = importlib.util.spec_from_file_location(
        "ibkr_contract_discovery", ROOT / "scripts" / "ibkr_contract_discovery.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_script_refuses_bad_query_file_before_anything(tmp_path, monkeypatch, capsys):
    mod = _script()
    monkeypatch.setattr(mod, "run_discovery", lambda *a, **k: pytest.fail("ran"))
    bad = tmp_path / "q.toml"
    bad.write_text('schema = "nope"\n')
    assert mod.main(["--queries", str(bad), "--report-dir", str(tmp_path)]) == 2
    assert "query file refused" in capsys.readouterr().err
    assert mod.main(["--limit", "0", "--report-dir", str(tmp_path)]) == 2


def test_script_writes_catalog_and_exit_code(tmp_path, monkeypatch, capsys):
    mod = _script()
    fake = FakeSession({"ES": ES_ROWS})
    seen = {}

    def fake_run(p, config_path):
        seen["pace"], seen["limit"], seen["config"] = p.pace_seconds, p.limit, config_path
        return run(p, fake)

    monkeypatch.setattr(mod, "run_discovery", fake_run)
    code = mod.main(["--report-dir", str(tmp_path), "--pace", "2", "--limit", "7",
                     "--config", "/nonexistent/paper.toml"])
    out = capsys.readouterr().out
    assert code == 0 and "IBKR contract discovery: COMPLETE" in out and "catalog:" in out
    assert seen == {"pace": 2.0, "limit": 7, "config": "/nonexistent/paper.toml"}
    assert len(list(tmp_path.glob("contract_catalog_*.json"))) == 1
