"""Pure-logic tests for tradingbot.ibkr. Must pass WITHOUT ibapi installed (GitHub CI)."""
from __future__ import annotations

import ast
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from tradingbot.ibkr import errors as E
from tradingbot.ibkr.check import run_check, summarize, write_report
from tradingbot.ibkr.config import load_config, validate_config
from tradingbot.ibkr.identity import check_managed_accounts, check_row_account
from tradingbot.ibkr.models import (
    AccountSummary,
    AccountValue,
    QuoteQuality,
    QuoteSnapshot,
    ResolvedContract,
    assess_quote,
)
from tradingbot.ibkr.redact import mask_account, redact_accounts

ROOT = Path(__file__).resolve().parents[1]
ACCT = "DU9990001"  # synthetic
GOOD = {"host": "127.0.0.1", "port": 7497, "client_id": 17, "account": ACCT, "mode": "paper"}


def _toml(d: dict) -> str:
    return "\n".join(f"{k} = {json.dumps(v)}" for k, v in d.items()) + "\n"


def _write(tmp_path: Path, d: dict | str = GOOD, mode: int = 0o600) -> Path:
    p = tmp_path / "paper.toml"
    p.write_text(d if isinstance(d, str) else _toml(d))
    os.chmod(p, mode)
    return p


# --- config ----------------------------------------------------------------------------------------
def test_valid_config_loads_and_masks(tmp_path):
    cfg = load_config(_write(tmp_path))
    assert (cfg.host, cfg.port, cfg.client_id, cfg.mode) == ("127.0.0.1", 7497, 17, "paper")
    assert cfg.account == ACCT
    assert cfg.masked_account() == "DU…01"
    assert ACCT not in repr(cfg) and ACCT not in str(cfg) and "DU…01" in repr(cfg)


def test_missing_file(tmp_path):
    with pytest.raises(E.ConfigNotFound):
        load_config(tmp_path / "nope.toml")


def test_symlink_rejected(tmp_path):
    real = _write(tmp_path)
    link = tmp_path / "link.toml"
    link.symlink_to(real)
    with pytest.raises(E.ConfigIsSymlink):
        load_config(link)


def test_directory_rejected(tmp_path):
    d = tmp_path / "dir.toml"
    d.mkdir()
    with pytest.raises(E.ConfigNotRegularFile):
        load_config(d)


@pytest.mark.parametrize("mode", [0o640, 0o604, 0o644, 0o620, 0o660])
def test_group_or_world_permissions_rejected(tmp_path, mode):
    with pytest.raises(E.ConfigPermissionsTooOpen):
        load_config(_write(tmp_path, mode=mode))


def test_wrong_owner_rejected(tmp_path, monkeypatch):
    p = _write(tmp_path)
    monkeypatch.setattr(os, "getuid", lambda: os.stat(p).st_uid + 1)
    with pytest.raises(E.ConfigWrongOwner):
        load_config(p)


@pytest.mark.parametrize("host", ["10.0.0.5", "0.0.0.0", "192.168.1.2", "localhost", "tws.example"])
def test_non_loopback_host_rejected(host):
    with pytest.raises(E.NonLoopbackHost):
        validate_config({**GOOD, "host": host})


def test_ipv6_loopback_allowed():
    assert validate_config({**GOOD, "host": "::1"}).host == "::1"


@pytest.mark.parametrize("port", [7496, 4001])
def test_live_ports_rejected(port):
    with pytest.raises(E.LivePortRejected):
        validate_config({**GOOD, "port": port})


@pytest.mark.parametrize("account", ["U1234567", "F7654321", "I1234567"])
def test_live_style_accounts_rejected_without_leaking(account):
    with pytest.raises(E.NonPaperAccount) as ei:
        validate_config({**GOOD, "account": account})
    assert account not in str(ei.value)


def test_mode_must_be_paper():
    with pytest.raises(E.ModeNotPaper):
        validate_config({**GOOD, "mode": "live"})


@pytest.mark.parametrize("patch", [{"client_id": 0}, {"client_id": True}, {"port": "7497"},
                                   {"account": "du123456"}, {"account": 12345}, {"extra": 1}])
def test_invalid_values_rejected(patch):
    with pytest.raises(E.ConfigInvalid):
        validate_config({**GOOD, **patch})


def test_missing_key_rejected():
    with pytest.raises(E.ConfigInvalid):
        validate_config({k: v for k, v in GOOD.items() if k != "account"})


def test_bad_toml_does_not_echo_content(tmp_path):
    with pytest.raises(E.ConfigInvalid) as ei:
        load_config(_write(tmp_path, f'account = "{ACCT}"\nport = = 1\n'))
    assert ACCT not in str(ei.value)


def test_config_errors_are_distinct_types():
    types = [E.ConfigNotFound, E.ConfigIsSymlink, E.ConfigNotRegularFile, E.ConfigWrongOwner,
             E.ConfigPermissionsTooOpen, E.ConfigInvalid, E.NonLoopbackHost, E.LivePortRejected,
             E.NonPaperAccount, E.ModeNotPaper]
    assert len(set(types)) == 10 and all(issubclass(t, E.ConfigError) for t in types)


# --- masking ---------------------------------------------------------------------------------------
def test_masking():
    assert mask_account("DU1234567") == "DU…67"
    assert mask_account("abc") == "***"
    text = redact_accounts("rows for DU1234567 and U7654321, conId 15547841, TLT")
    assert "DU1234567" not in text and "U7654321" not in text
    assert "DU…67" in text and "U7…21" in text and "15547841" in text and "TLT" in text


def test_exceptions_redact_account_ids():
    exc = E.IbkrRequestError(321, f"Error validating request for {ACCT}", 9001)
    assert ACCT not in str(exc) and ACCT not in exc.broker_message and exc.code == 321


# --- identity gate ---------------------------------------------------------------------------------
@pytest.mark.parametrize("raw", [ACCT, f"{ACCT},", f" {ACCT} "])
def test_identity_gate_accepts_exactly_the_configured_account(raw):
    check_managed_accounts(raw, ACCT)


@pytest.mark.parametrize("raw,reason", [
    ("DU9990002", "MISMATCH"), (f"{ACCT},DU9990002", "MULTIPLE"), (f"{ACCT},{ACCT}", "MULTIPLE"),
    ("", "EMPTY"), (",", "EMPTY"), (None, "TIMEOUT"), ("du9990001", "MISMATCH")])
def test_identity_gate_rejects(raw, reason):
    with pytest.raises(E.AccountIdentityError) as ei:
        check_managed_accounts(raw, ACCT)
    assert ei.value.reason == reason
    assert ACCT not in str(ei.value) and "DU9990002" not in str(ei.value)


def test_foreign_row_rejected():
    check_row_account(ACCT, ACCT, "position")
    with pytest.raises(E.AccountIdentityError) as ei:
        check_row_account("DU9990002", ACCT, "position")
    assert ei.value.reason == "FOREIGN_ROW"


# --- quote quality ---------------------------------------------------------------------------------
def _q(**kw):
    base = {"bid": 10.0, "ask": 10.02, "last": 10.01, "market_data_type": 1,
            "delayed_ticks_seen": False, "live_ticks_seen": True, "unavailable_code": None}
    base.update(kw)
    return assess_quote(**base)


def test_live_quote():
    assert _q() == (QuoteQuality.LIVE, ())


def test_delayed_quote_is_reported_as_delayed():
    q, flags = _q(market_data_type=3, delayed_ticks_seen=True, live_ticks_seen=False)
    assert q is QuoteQuality.DELAYED and flags == ()


@pytest.mark.parametrize("mdt", [2, 4])
def test_frozen_quotes(mdt):
    assert _q(market_data_type=mdt)[0] is QuoteQuality.FROZEN


def test_never_live_without_broker_reported_type():
    q, flags = _q(market_data_type=None)
    assert q is QuoteQuality.FROZEN and "MARKET_DATA_TYPE_NOT_REPORTED" in flags
    q, _ = _q(market_data_type=None, delayed_ticks_seen=True, live_ticks_seen=False)
    assert q is QuoteQuality.DELAYED


def test_live_type_with_delayed_ticks_is_not_live():
    q, flags = _q(delayed_ticks_seen=True)
    assert q is QuoteQuality.DELAYED and "TYPE_TICK_CONFLICT" in flags


def test_unavailable_cases():
    assert _q(bid=None, ask=None, last=None)[0] is QuoteQuality.UNAVAILABLE
    assert _q(bid=-1.0, ask=-1.0, last=None)[0] is QuoteQuality.UNAVAILABLE
    q, flags = _q(unavailable_code=354)
    assert q is QuoteQuality.UNAVAILABLE and "NOT_ENTITLED" in flags


def test_validity_flags():
    assert "MISSING_BID" in _q(bid=None)[1]
    assert "NON_POSITIVE_ASK" in _q(ask=0.0)[1]
    assert "CROSSED" in _q(bid=10.05, ask=10.0)[1]
    assert "CROSSED" not in _q(bid=10.0, ask=10.0)[1]


def test_last_trade_time_plays_no_part_in_quality():
    # assess_quote has no timestamp input at all: bid/ask freshness can't come from last-trade time
    import inspect
    assert not any("time" in p for p in inspect.signature(assess_quote).parameters)


# --- error classification ----------------------------------------------------------------------------
@pytest.mark.parametrize("code", [2104, 2106, 2107, 2108, 2158, 2119, 2103, 2150, 10167])
def test_notice_codes(code):
    assert E.classify_error(code) is E.ErrorClass.NOTICE


@pytest.mark.parametrize("code", [502, 504, 1100, 1101, 1102, 1300, 2110])
def test_connection_codes(code):
    assert E.classify_error(code) is E.ErrorClass.CONNECTION


@pytest.mark.parametrize("code", [354, 10089, 10168, 10197])
def test_market_data_unavailable_codes(code):
    assert E.classify_error(code) is E.ErrorClass.MARKET_DATA_UNAVAILABLE


@pytest.mark.parametrize("code", [200, 321, 162, 326, 300, 10147])
def test_other_codes_are_errors(code):
    assert E.classify_error(code) is E.ErrorClass.ERROR


# --- structural: no order-affecting calls anywhere in the adapter ---------------------------------
FORBIDDEN = {"placeOrder", "cancelOrder", "reqGlobalCancel", "exerciseOptions", "placeOrderProtoBuf",
             "cancelOrderProtoBuf", "reqGlobalCancelProtoBuf", "exerciseOptionsProtoBuf",
             "reqAutoOpenOrders", "PLACE_ORDER", "CANCEL_ORDER", "REQ_GLOBAL_CANCEL"}


def _adapter_sources() -> list[Path]:
    files = sorted((ROOT / "src" / "tradingbot" / "ibkr").glob("*.py"))
    files.append(ROOT / "scripts" / "ibkr_paper_check.py")
    assert len(files) >= 8
    return files


def _order_refs(source: str) -> list[str]:
    hits = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Attribute) and node.attr in FORBIDDEN:
            hits.append(node.attr)
        elif isinstance(node, ast.Name) and node.id in FORBIDDEN:
            hits.append(node.id)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            hits += [f for f in FORBIDDEN if f in node.value]
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in FORBIDDEN:
            hits.append(node.name)
    return hits


def test_order_scanner_detects_references():
    assert _order_refs("self._client.placeOrder(1, c, o)") == ["placeOrder"]
    assert _order_refs("getattr(c, 'reqGlobalCancel')()") == ["reqGlobalCancel"]
    assert _order_refs("from ibapi.client import EClient\nEClient.cancelOrder") == ["cancelOrder"]


@pytest.mark.parametrize("path", _adapter_sources(), ids=lambda p: p.name)
def test_no_order_methods_referenced(path):
    source = path.read_text()
    assert _order_refs(source) == [], f"{path.name} references order-affecting API"
    assert "getattr(self._client" not in source  # no dynamic method lookup on EClient


# --- import isolation ----------------------------------------------------------------------------
def test_pure_modules_do_not_import_ibapi():
    code = ("import sys; import tradingbot.ibkr.config, tradingbot.ibkr.models, "
            "tradingbot.ibkr.errors, tradingbot.ibkr.identity, tradingbot.ibkr.redact, "
            "tradingbot.ibkr.check; "
            "bad = [m for m in sys.modules if m.split('.')[0] == 'ibapi']; "
            "assert not bad, bad")
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    r = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True,
                       check=False)
    assert r.returncode == 0, r.stderr


# --- check runner with a fake session --------------------------------------------------------------
class FakeSession:
    sdk_version = "0.0-fake"

    def __init__(self, cfg, *, fail: str | None = None, quality=QuoteQuality.LIVE):
        self.cfg, self.fail, self.quality = cfg, fail, quality
        self.server_version, self.connection_time, self.connected_at = 226, "t", "t"
        self.notices, self.connection_events, self.unrouted_errors = [], [], []
        self.internal_errors, self.cleanup_failures = [], []
        self.poisoned = False
        self.calls: list[str] = []

    def _maybe(self, name):
        self.calls.append(name)
        if self.fail == name:
            raise E.CleanupError("boom", failures=["x"]) if name == "disconnect" else \
                E.IbkrTimeout(f"{name} timed out")

    def threads_alive(self):
        return []

    def connect(self):
        self._maybe("connect")

    def disconnect(self):
        self._maybe("disconnect")

    def resolve_contract(self, q):
        self._maybe("resolve_contract")
        return ResolvedContract(1, "TLT", "STK", "USD", "SMART", "NASDAQ", "TLT", 0.01, "", "", "")

    def account_summary(self):
        self._maybe("account_summary")
        return AccountSummary(self.cfg.masked_account(),
                              {t: AccountValue("1", "USD") for t in
                               ("AccountType", "NetLiquidation", "TotalCashValue", "BuyingPower",
                                "AvailableFunds")}, ())

    def positions(self):
        self._maybe("positions")
        return ()

    def quote_snapshot(self, c):
        self._maybe("quote_snapshot")
        return QuoteSnapshot(1, "TLT", 1.0, None, 1.1, None, None, None, 3, "DELAYED", 3, "t",
                             None, self.quality, ())


def _run(tmp_path, **kw):
    made = []

    def factory(cfg):
        made.append(FakeSession(cfg, **kw))
        return made[-1]

    return run_check(_write(tmp_path), session_factory=factory), made


def test_check_pass_and_report_is_masked(tmp_path):
    report, made = _run(tmp_path)
    assert report["overall"] == "PASS", summarize(report)
    assert len(made) == 2 and made[1].calls == ["connect", "disconnect"]  # recovery proof
    text = json.dumps(report)
    assert ACCT not in text and report["account"] == "DU…01"
    assert report["sdk_version"] == "0.0-fake" and report["session"]["server_version"] == 226
    path = write_report(report, tmp_path / "reports")
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(tmp_path / "reports").st_mode) == 0o700
    assert path.name.startswith("ibkr_paper_check_") and ACCT not in path.read_text()


@pytest.mark.parametrize("step", ["connect", "resolve_contract", "account_summary", "positions",
                                  "quote_snapshot", "disconnect"])
def test_any_failure_including_cleanup_is_fail(tmp_path, step):
    report, _ = _run(tmp_path, fail=step)
    assert report["overall"] == "FAIL"


def test_cleanup_failure_recorded_on_session_is_fail(tmp_path):
    def factory(cfg):
        s = FakeSession(cfg)
        s.cleanup_failures.append("positions cancel failed")
        return s

    report = run_check(_write(tmp_path), session_factory=factory)
    assert report["overall"] == "FAIL"


def test_delayed_quote_passes_with_warning(tmp_path):
    report, _ = _run(tmp_path, quality=QuoteQuality.DELAYED)
    assert report["overall"] == "PASS" and any("DELAYED" in w for w in report["warnings"])


def test_bad_config_is_fail_without_session(tmp_path):
    made = []
    report = run_check(_write(tmp_path, {**GOOD, "port": 7496}),
                       session_factory=lambda c: made.append(c))
    assert report["overall"] == "FAIL" and not made


def test_redaction_guard(tmp_path):
    def factory(cfg):
        s = FakeSession(cfg)
        s.connection_time = cfg.account  # simulate a leak through an unredacted field
        return s

    report = run_check(_write(tmp_path), session_factory=factory)
    assert ACCT not in json.dumps(report) and report["overall"] == "FAIL"


def test_script_report_dir_env(monkeypatch, tmp_path):
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import ibkr_paper_check
    finally:
        sys.path.pop(0)
    monkeypatch.setenv("TRADINGBOT_IBKR_REPORT_DIR", str(tmp_path))
    assert ibkr_paper_check.default_report_dir() == tmp_path
