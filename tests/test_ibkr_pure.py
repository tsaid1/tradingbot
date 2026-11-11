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
    QuoteReport,
    QuoteSnapshot,
    ResolvedContract,
    assess_quote,
    assess_quote_attempts,
)
from tradingbot.ibkr.redact import mask_account, redact_accounts

ROOT = Path(__file__).resolve().parents[1]
ACCT = "DU9990001"  # synthetic
GOOD = {"mode": "paper", "host": "127.0.0.1", "port": 7497, "client_id": 17, "account": ACCT,
        "tws_profile_ini": "/Users/someone/Jts/jts.ini", "tws_os_user": "someone",
        "read_only_check": "config-read"}  # synthetic values


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


@pytest.mark.parametrize("port", [7496, 4001, 7498, 1, 4003])
def test_only_paper_ports_allowed(port):
    with pytest.raises(E.PortNotAllowed):
        validate_config({**GOOD, "port": port})


@pytest.mark.parametrize("port", [7497, 4002])
def test_paper_ports_accepted(port):
    assert validate_config({**GOOD, "port": port}).port == port


@pytest.mark.parametrize("patch", [{"read_only_check": "none"}, {"read_only_check": ""},
                                   {"tws_profile_ini": "Jts/jts.ini"}, {"tws_os_user": "Bad User"},
                                   {"tws_os_user": ""}])
def test_new_keys_validated(patch):
    with pytest.raises(E.ConfigInvalid):
        validate_config({**GOOD, **patch})


@pytest.mark.parametrize("key", ["tws_profile_ini", "tws_os_user", "read_only_check"])
def test_new_keys_required_no_default(key):
    with pytest.raises(E.ConfigInvalid):
        validate_config({k: v for k, v in GOOD.items() if k != key})


def test_final_key_set():
    from tradingbot.ibkr.config import _KEYS
    assert _KEYS == {"mode", "host", "port", "client_id", "account", "tws_profile_ini",
                     "tws_os_user", "read_only_check"}


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
             E.ConfigPermissionsTooOpen, E.ConfigInvalid, E.NonLoopbackHost, E.PortNotAllowed,
             E.NonPaperAccount, E.ModeNotPaper]
    assert len(set(types)) == 10 and all(issubclass(t, E.ConfigError) for t in types)


# --- masking ---------------------------------------------------------------------------------------
def test_masking():
    assert mask_account("DU1234567") == "DU…67"
    assert mask_account("abc") == "***"
    text = redact_accounts("rows for DU1234567 and U7654321, conId 15547841, TLT")
    assert "DU1234567" not in text and "U7654321" not in text
    assert "DU…67" in text and "U7…21" in text and "15547841" in text and "TLT" in text


@pytest.mark.parametrize("raw", ["DU1234567DU7654321", "aDU1234567", "DU1234567a",
                                 "_DU1234567_", "x9DU1234567", "acctDU1234567x"])
def test_redaction_ignores_what_adjoins_the_id(raw):
    out = redact_accounts(raw)
    assert "1234567" not in out and "7654321" not in out


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
             "reqAutoOpenOrders", "PLACE_ORDER", "CANCEL_ORDER", "REQ_GLOBAL_CANCEL",
             "updateConfigProtoBuf", "UPDATE_CONFIG", "replaceFA", "replaceFAProtoBuf",
             "REPLACE_FA", "setServerLogLevel", "updateDisplayGroup"}


def _adapter_sources() -> list[Path]:
    files = sorted((ROOT / "src" / "tradingbot" / "ibkr").glob("*.py"))
    files.append(ROOT / "scripts" / "ibkr_paper_check.py")
    files.append(ROOT / "scripts" / "ibkr_contract_discovery.py")
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
            "tradingbot.ibkr.check, tradingbot.ibkr.environment, tradingbot.ibkr.discovery; "
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
        self.preflight_result = {"stub": True}
        self.read_only_status = "VERIFIED"
        self.read_only_settings = None
        self.reader_joined = True

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

    def quote(self, c, *, snapshot_fallback=False):
        self._maybe("quote")
        self.snapshot_fallback = snapshot_fallback
        q = {QuoteQuality.LIVE: _quote(route="stream"),
             QuoteQuality.DELAYED: _quote(mdt=3, route="stream"),
             QuoteQuality.UNAVAILABLE: _quote(None, None, None, counts={}, route="stream"),
             }[self.quality]
        attempts = (q,) if q.two_sided_valid or not snapshot_fallback else (
            q, _quote(None, None, None, counts={}, route="snapshot"))
        status, reason = assess_quote_attempts(attempts)
        return QuoteReport(attempts, status, reason)


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
    assert "read_only_check=config-read" in summarize(report).splitlines()[0]
    path = write_report(report, tmp_path / "reports")
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(tmp_path / "reports").st_mode) == 0o700
    assert path.name.startswith("ibkr_paper_check_") and ACCT not in path.read_text()


@pytest.mark.parametrize("step", ["connect", "resolve_contract", "account_summary", "positions",
                                  "quote", "disconnect"])
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


def test_delayed_two_sided_quote_is_pass_with_warnings(tmp_path):
    report, _ = _run(tmp_path, quality=QuoteQuality.DELAYED)
    assert report["overall"] == "PASS_WITH_WARNINGS"
    step = next(st for st in report["steps"] if st["name"] == "quote")
    assert step["status"] == "PASS_WITH_WARNINGS" and "DELAYED" in step["detail"]["reason"]


def test_nothing_on_the_stream_is_fail_with_evidence(tmp_path):
    report, _ = _run(tmp_path, quality=QuoteQuality.UNAVAILABLE)
    assert report["overall"] == "FAIL"
    step = next(st for st in report["steps"] if st["name"] == "quote")
    assert step["status"] == "FAIL" and step["detail"]["routes"] == ["stream"]
    assert step["detail"]["snapshot_fallback"] is False
    assert "no market data delivered" in summarize(report).splitlines()[0]


def test_nothing_on_both_routes_with_snapshot_fallback_is_fail_with_evidence(tmp_path):
    made = []

    def factory(cfg):
        made.append(FakeSession(cfg, quality=QuoteQuality.UNAVAILABLE))
        return made[-1]

    report = run_check(_write(tmp_path), session_factory=factory, quote_snapshot_fallback=True)
    assert report["overall"] == "FAIL" and made[0].snapshot_fallback is True
    step = next(st for st in report["steps"] if st["name"] == "quote")
    assert step["status"] == "FAIL" and len(step["detail"]["attempts"]) == 2
    assert [a["route"] for a in step["detail"]["attempts"]] == ["stream", "snapshot"]
    assert "no market data delivered" in summarize(report).splitlines()[0]


def test_quote_age_is_receipt_minus_last_trade_and_unknown_without_it():
    from datetime import UTC, datetime

    from tradingbot.ibkr.models import quote_age_seconds

    received = datetime(2026, 11, 11, 21, 45, 13, 310000, tzinfo=UTC)
    last_trade = int(datetime(2026, 11, 11, 21, 30, 10, tzinfo=UTC).timestamp())
    assert quote_age_seconds(received, last_trade) == 903.31
    assert quote_age_seconds(received, None) is None
    doc = QuoteSnapshot.__doc__
    assert "LAST TRADE ONLY" in doc and "bid or ask" in doc


@pytest.mark.parametrize("value", ["owner-attested", "none", "config_read"])
def test_read_only_check_has_exactly_one_legal_value(value):
    with pytest.raises(E.ConfigInvalid):
        validate_config({**GOOD, "read_only_check": value})


def test_unverified_read_only_status_fails(tmp_path):
    def factory(cfg):
        s = FakeSession(cfg)
        s.read_only_status = "NOT_CHECKED"
        return s

    assert run_check(_write(tmp_path), session_factory=factory)["overall"] == "FAIL"


def _quote(bid=88.10, ask=88.12, last=88.11, mdt=1, counts=None, route="snapshot", flags=()):
    from tradingbot.ibkr.models import MARKET_DATA_TYPE_NAMES
    quality, f = assess_quote(bid=bid, ask=ask, last=last, market_data_type=mdt,
                              delayed_ticks_seen=mdt in (3, 4), live_ticks_seen=mdt in (1, 2))
    return QuoteSnapshot(1, "TLT", bid, None, ask, None, last, None, mdt,
                         MARKET_DATA_TYPE_NAMES.get(mdt, "UNREPORTED"), 3, "t", None, quality,
                         (*f, *flags), route=route, waited_ms=8000 if route == "stream" else 20000,
                         tick_counts={"BID": 1} if counts is None else counts)


def test_quote_attempt_verdicts():
    assert assess_quote_attempts((_quote(),))[0] == "PASS"
    status, reason = assess_quote_attempts((_quote(mdt=3),))
    assert status == "PASS_WITH_WARNINGS" and "DELAYED" in reason
    status, reason = assess_quote_attempts((_quote(bid=None, ask=None),))
    assert status == "PASS_WITH_WARNINGS" and "partial" in reason and "last" in reason
    nothing = _quote(bid=None, ask=None, last=None, counts={})
    status, reason = assess_quote_attempts((nothing, _quote(None, None, None, counts={},
                                                            route="stream")))
    assert status == "FAIL" and "snapshot 20000 ms" in reason and "stream 8000 ms" in reason
    assert assess_quote_attempts((nothing, _quote(route="stream")))[0] == "PASS"
    no_type = _quote(mdt=None)
    assert assess_quote_attempts((no_type,))[0] == "PASS_WITH_WARNINGS"  # no delivered type


def test_reader_not_joined_is_fail_for_both_sessions(tmp_path):
    made = []

    def factory(cfg):
        s = FakeSession(cfg)
        if made:
            s.reader_joined = False  # only the reconnect session
        made.append(s)
        return s

    report = run_check(_write(tmp_path), session_factory=factory)
    assert report["overall"] == "FAIL"
    assert any(st["name"] == "reconnect_threads_joined" and st["status"] == "FAIL"
               for st in report["steps"])


def test_script_exit_codes(tmp_path, monkeypatch):
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import ibkr_paper_check
    finally:
        sys.path.pop(0)
    for overall, code in (("PASS", 0), ("PASS_WITH_WARNINGS", 0), ("FAIL", 1)):
        monkeypatch.setattr(ibkr_paper_check, "run_check",
                            lambda path, o=overall, **kw: {"overall": o, "steps": [],
                                                           "warnings": []})
        assert ibkr_paper_check.main(["--report-dir", str(tmp_path)]) == code


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
    from tradingbot.ibkr.check import default_report_dir

    assert default_report_dir() == tmp_path  # the dashboard page uses the same function
    monkeypatch.delenv("TRADINGBOT_IBKR_REPORT_DIR")
    (tmp_path / "ws" / "private").mkdir(parents=True)
    assert default_report_dir(tmp_path / "ws" / "repo") == tmp_path / "ws" / "private" / \
        "ibkr_reports"
    assert default_report_dir(tmp_path / "ws" / "a" / "repo") == tmp_path / "ws" / "private" / \
        "ibkr_reports"


def test_script_passes_snapshot_fallback_flag(tmp_path, monkeypatch):
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import ibkr_paper_check
    finally:
        sys.path.pop(0)
    seen = []
    monkeypatch.setattr(ibkr_paper_check, "run_check", lambda path, **kw: (
        seen.append(kw), {"overall": "PASS", "steps": [], "warnings": []})[1])
    ibkr_paper_check.main(["--report-dir", str(tmp_path)])
    ibkr_paper_check.main(["--report-dir", str(tmp_path), "--quote-snapshot-fallback"])
    assert seen == [{"quote_snapshot_fallback": False}, {"quote_snapshot_fallback": True}]


