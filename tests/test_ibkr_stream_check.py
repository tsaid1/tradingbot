"""The held-quote-stream check (``scripts/ibkr_quote_stream_check.py`` and
``tradingbot.ibkr.stream_check``) with a fake session. Pure: passes WITHOUT ibapi (GitHub CI)."""
from __future__ import annotations

import dataclasses
import importlib.util
import json
import os
from pathlib import Path

import pytest

from tradingbot.ibkr import errors as E
from tradingbot.ibkr.config import validate_config
from tradingbot.ibkr.discovery import QueryFileInvalid
from tradingbot.ibkr.models import (
    TLT_QUERY,
    ContractQuery,
    QuoteQuality,
    QuoteSnapshot,
    ResolvedContract,
)
from tradingbot.ibkr.stream_check import (
    COMPLETE,
    FAILED,
    parse_contract_arg,
    run_stream_check,
    summarize,
    validate_args,
)

ROOT = Path(__file__).resolve().parents[1]
ACCT = "DU9990001"  # synthetic
CFG = validate_config({"mode": "paper", "host": "127.0.0.1", "port": 7497, "client_id": 17,
                       "account": ACCT, "tws_profile_ini": "/Users/someone/Jts/jts.ini",
                       "tws_os_user": "someone", "read_only_check": "config-read"})


class FakeClock:
    def __init__(self) -> None:
        self.t = 500.0

    def __call__(self) -> float:
        return self.t

    def sleep(self, dt: float) -> None:
        self.t += dt


def _snap(clock: FakeClock, n: int) -> QuoteSnapshot:
    return QuoteSnapshot(
        1, "TLT", 88.0, None, 88.02, None, None, None, None, "UNREPORTED", 3, "t", None,
        QuoteQuality.LIVE if n else QuoteQuality.FROZEN, (), bid_at_monotonic=clock.t - 0.5,
        ask_at_monotonic=clock.t - 4.0 if n == 2 else clock.t - 0.5, bid_updates=n,
        bid_price_change_monotonic=clock.t - 0.5,
        ask_price_change_monotonic=clock.t - 4.0 if n == 2 else clock.t - 0.5,
        ask_updates=n, bid_tick_class="LIVE", ask_tick_class="LIVE",
        quality_reason="TYPE_UNREPORTED_UPDATES_SEEN" if n else "TYPE_UNREPORTED_NO_UPDATES_YET",
        observed_at_monotonic=clock.t, route="held_stream")


class FakeStream:
    def __init__(self, s: FakeSession, clock: FakeClock) -> None:
        self.s, self.clock, self.n, self.stream_id = s, clock, 0, 9001
        self.closed = False

    def latest(self) -> QuoteSnapshot:
        snap = _snap(self.clock, min(self.n, 2))
        self.n += 1
        return snap

    def close(self) -> QuoteSnapshot:
        self.closed = True
        self.s.calls.append("close")
        if self.s.fail == "close":
            raise E.CleanupError("cancel failed", failures=["quote_stream: x"])
        return _snap(self.clock, 2)


class FakeSession:
    sdk_version = "0.0-fake"

    def __init__(self, cfg, clock, fail=None):
        self.cfg, self.clock, self.fail = cfg, clock, fail
        self.server_version, self.connection_time, self.connected_at = 226, "t", "t"
        self.notices, self.connection_events, self.unrouted_errors = [], [], []
        self.internal_errors, self.cleanup_failures = [], []
        self.poisoned, self.preflight_result = False, {"stub": True}
        self.read_only_status, self.read_only_settings, self.reader_joined = "VERIFIED", None, True
        self.calls: list[str] = []
        self.streams: list[FakeStream] = []

    def threads_alive(self):
        return []

    def connect(self):
        self.calls.append("connect")
        if self.fail == "connect":
            raise E.AccountIdentityError("wrong account", reason="MISMATCH")

    def disconnect(self):
        self.calls.append("disconnect")

    def resolve_contract(self, q):
        self.calls.append(f"resolve:{q.symbol}")
        if q.symbol == "NOPE":
            raise E.ContractNotFound("no contract")
        return ResolvedContract(1, q.symbol, q.sec_type, q.currency, q.exchange, "", q.symbol,
                                0.01, "", "", "")

    def open_quote_stream(self, contract, *, market_data_type=3):
        self.calls.append(f"open:{contract.symbol}:{market_data_type}")
        if self.fail == "open":
            raise E.QuoteStreamLimit("too many")
        st = FakeStream(self, self.clock)
        self.streams.append(st)
        return st


def run(queries=(TLT_QUERY,), fail=None, **kw):
    clock = FakeClock()
    sessions = []

    def factory(cfg):
        sessions.append(FakeSession(cfg, clock, fail))
        return sessions[0]

    rep = run_stream_check(list(queries), None, session_factory=factory, loader=lambda p: CFG,
                           sleep=clock.sleep, clock=clock, **kw)
    return rep, sessions[0] if sessions else None


def test_complete_run_records_the_evidence_and_closes_every_stream():
    rep, s = run(hold_seconds=2.0)
    assert rep["overall"] == COMPLETE and rep["account"] == "DU…01"
    (row,) = rep["results"]
    assert row["status"] == "DONE" and row["quality"] == "LIVE"
    assert row["updates"] == {"bid": 2, "ask": 2}
    assert row["first_update_after_open_seconds"] == {"bid": 0.25, "ask": 0.25}
    assert row["max_age_while_held_seconds"] == {"bid": 0.5, "ask": 4.0}
    assert row["entry_usable_at_end"] is False and row["entry_reasons_at_end"] == ["ASK_STALE"]
    assert row["polls"] == 9 and row["polls_usable_for_entry"] == 1
    assert s.calls == ["connect", "resolve:TLT", "open:TLT:3", "close", "disconnect"]
    assert ACCT not in json.dumps(rep, default=str)
    text = summarize(rep)
    assert "TLT" in text and "updates bid=2 ask=2" in text and "COMPLETE" in text


def test_several_contracts_in_turn_and_not_found_is_not_a_failure():
    qs = [TLT_QUERY, ContractQuery("NOPE"), ContractQuery("ES", "FUT", "CME")]
    rep, s = run(qs, hold_seconds=1.0, market_data_type=1)
    assert rep["overall"] == COMPLETE
    assert [r["status"] for r in rep["results"]] == ["DONE", "NOT_RESOLVED", "DONE"]
    assert [c for c in s.calls if c.startswith("open")] == ["open:TLT:1", "open:ES:1"]
    assert all(st.closed for st in s.streams)


@pytest.mark.parametrize("fail", ["connect", "open", "close"])
def test_failures_make_the_report_failed_and_always_disconnect(fail):
    rep, s = run([TLT_QUERY, ContractQuery("IEF")], fail=fail, hold_seconds=1.0)
    assert rep["overall"] == FAILED and rep["failure"]
    if fail != "connect":
        assert rep["results"][-1]["status"] == "SKIPPED"
        assert s.calls[-1] == "disconnect"


def test_stream_closed_even_when_latest_raises():
    clock = FakeClock()
    s = FakeSession(CFG, clock)

    def boom():
        raise E.QuoteStreamClosed("gone")

    real_open = s.open_quote_stream

    def open_(contract, **kw):
        st = real_open(contract, **kw)
        st.latest = boom
        return st

    s.open_quote_stream = open_
    rep = run_stream_check([TLT_QUERY], None, session_factory=lambda cfg: s,
                           loader=lambda p: CFG, sleep=clock.sleep, clock=clock, hold_seconds=1)
    assert rep["overall"] == FAILED and s.streams[0].closed and s.calls[-1] == "disconnect"


def test_unverified_read_only_is_failed_and_opens_nothing():
    clock = FakeClock()
    s = FakeSession(CFG, clock)
    s.read_only_status = "NOT_CHECKED"
    rep = run_stream_check([TLT_QUERY], None, session_factory=lambda cfg: s,
                           loader=lambda p: CFG, sleep=clock.sleep, clock=clock, hold_seconds=1)
    assert rep["overall"] == FAILED and not any(c.startswith("open") for c in s.calls)


@pytest.mark.parametrize(("seconds", "mdt", "age", "n"), [
    (0.5, 3, 3, 1), (121, 3, 3, 1), (True, 3, 3, 1), (20, 5, 3, 1), (20, 3, 0, 1), (20, 3, 61, 1),
    (20, 3, 3, 0), (20, 3, 3, 11), (20, 2, 3, 1), (20, 4, 3, 1)])
def test_arguments_validated_before_anything(seconds, mdt, age, n):
    with pytest.raises(QueryFileInvalid):
        validate_args([TLT_QUERY] * n, seconds, mdt, age)


def test_hold_has_a_hard_maximum():
    validate_args([TLT_QUERY], 120, 3, 3)
    validate_args([TLT_QUERY], 120, 1, 3)
    with pytest.raises(QueryFileInvalid):
        run_stream_check([TLT_QUERY], None, hold_seconds=600,
                         loader=lambda p: pytest.fail("config read"))


def test_contract_argument_uses_the_discovery_fields():
    q = parse_contract_arg("symbol=ES,sec_type=FUT,exchange=CME,"
                           "last_trade_date_or_contract_month=202612,include_expired=false")
    assert q == ContractQuery(symbol="ES", sec_type="FUT", exchange="CME",
                              last_trade_date_or_contract_month="202612")
    assert parse_contract_arg("symbol=SPY,sec_type=OPT,strike=500,right=C").strike == 500.0
    for bad in ("sec_type=STK", "symbol=X,bogus=1", "symbol=X,symbol=Y", "symbol",
                "symbol=X,strike=abc", "symbol=X,include_expired=yes"):
        with pytest.raises(QueryFileInvalid):
            parse_contract_arg(bad)
    assert {f.name for f in dataclasses.fields(ContractQuery)} >= {"symbol", "strike"}


def _script():
    spec = importlib.util.spec_from_file_location(
        "ibkr_quote_stream_check_script", ROOT / "scripts" / "ibkr_quote_stream_check.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("argv", [["--seconds", "500"], ["--contract", "bogus=1"],
                                  ["--market-data-type", "9"], ["--market-data-type", "2"],
                                  ["--market-data-type", "4"]])
def test_script_refuses_bad_arguments_before_reading_config(argv, tmp_path, monkeypatch, capsys):
    mod = _script()
    monkeypatch.setattr(mod, "run_stream_check", lambda *a, **k: pytest.fail("ran"))
    assert mod.main([*argv, "--report-dir", str(tmp_path)]) == 2
    assert "refused" in capsys.readouterr().err


def test_script_writes_a_private_report(tmp_path, monkeypatch, capsys):
    mod = _script()
    seen = {}

    def fake_run(queries, config, **kw):
        seen.update(queries=queries, **kw)
        rep, _ = run(queries, hold_seconds=kw["hold_seconds"])
        return rep

    monkeypatch.setattr(mod, "run_stream_check", fake_run)
    rc = mod.main(["--report-dir", str(tmp_path / "r"), "--seconds", "5"])
    assert rc == 0 and seen["queries"] == [TLT_QUERY] and seen["hold_seconds"] == 5.0
    (path,) = (tmp_path / "r").glob("ibkr_quote_stream_check_*.json")
    assert oct(os.stat(path).st_mode & 0o777) == "0o600"
    data = json.loads(path.read_text())
    assert data["schema"] == "tradingbot.ibkr.quote_stream_check/1" and ACCT not in path.read_text()
    assert "report:" in capsys.readouterr().out
