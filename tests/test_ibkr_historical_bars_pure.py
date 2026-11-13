"""Historical bars: request validation, closed sets, pacing, bar parsing, the batch file and the
private report path. SDK-free (runs with TRADINGBOT_BLOCK_IBAPI=1). Synthetic data only."""
from __future__ import annotations

import ast
import os
import stat
import tomllib
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from tradingbot.ibkr import historical as H
from tradingbot.ibkr.historical import (
    HistoricalBarsRequest,
    HistoricalBarsResult,
    HistoricalContract,
    HistoricalPacer,
    HistoricalRequestInvalid,
)
from tradingbot.ibkr.historical_batch import (
    OutputPathRefused,
    RequestFileInvalid,
    check_output_path,
    load_plan,
    parse_plan,
    run_batch,
    summarize,
    write_private_report,
)

ROOT = Path(__file__).resolve().parents[1]
END = datetime(2026, 3, 9, 14, 15, tzinfo=UTC)
NY = ZoneInfo("America/New_York")
FUT = HistoricalContract(con_id=100000001, exchange="NYMEX", include_expired=True)


def req(**kw) -> HistoricalBarsRequest:
    opts = {"contract": FUT, "end": END, "duration": "1800 S", "bar_size": "1 min",
            "what_to_show": "TRADES", "regular_hours_only": False}
    opts.update(kw)
    return HistoricalBarsRequest(**opts)


# --- request validation --------------------------------------------------------------------------
def test_closed_sets():
    assert H.WHAT_TO_SHOW == ("TRADES", "MIDPOINT", "BID", "ASK")
    assert H.DURATIONS["1 min"] == ("1800 S", "3600 S", "7200 S", "14400 S", "28800 S", "1 D")
    assert H.DURATIONS["1 day"] == ("1 W", "1 M", "3 M", "6 M", "1 Y")
    for bad in ({"bar_size": "1 secs"}, {"duration": "2 D"}, {"duration": "1 Y"},
                {"what_to_show": "BID_ASK"}, {"what_to_show": "trades"},
                {"regular_hours_only": 1}, {"bar_size": "1 day", "duration": "1800 S"}):
        with pytest.raises(HistoricalRequestInvalid):
            req(**bad)
    assert req(bar_size="1 day", duration="1 Y").intraday is False


def test_end_must_be_aware_whole_seconds_and_is_normalised_to_utc():
    with pytest.raises(HistoricalRequestInvalid, match="naive"):
        req(end=datetime(2026, 3, 9, 14, 15))
    with pytest.raises(HistoricalRequestInvalid, match="whole second"):
        req(end=END.replace(microsecond=5))
    with pytest.raises(HistoricalRequestInvalid):
        req(end="2026-03-09T14:15:00Z")
    r = req(end=datetime(2026, 3, 9, 10, 15, tzinfo=timezone(timedelta(hours=-4))))
    assert r.end == END and r.end.tzinfo is UTC and r.end_date_time == "20260309-14:15:00"


@pytest.mark.parametrize(("wall", "utc"), [
    (datetime(2026, 3, 8, 1, 59, tzinfo=NY), "20260308-06:59:00"),  # EST, just before
    (datetime(2026, 3, 8, 3, 0, tzinfo=NY), "20260308-07:00:00"),  # EDT, just after
    (datetime(2026, 11, 1, 0, 59, tzinfo=NY), "20261101-04:59:00"),  # EDT, before fall back
    (datetime(2026, 11, 1, 2, 0, tzinfo=NY), "20261101-07:00:00"),  # EST, after
])
def test_new_york_ends_around_dst_changes(wall, utc):
    assert req(end=wall).end_date_time == utc


@pytest.mark.parametrize("wall", [
    datetime(2026, 3, 8, 2, 0, tzinfo=NY), datetime(2026, 3, 8, 2, 59, tzinfo=NY),  # skipped
    datetime(2026, 11, 1, 1, 0, tzinfo=NY), datetime(2026, 11, 1, 1, 59, tzinfo=NY),  # repeated
    datetime(2026, 11, 1, 1, 30, tzinfo=NY).replace(fold=1),
])
def test_ambiguous_or_nonexistent_wall_times_are_refused(wall):
    with pytest.raises(HistoricalRequestInvalid, match="ambiguous or does not exist"):
        req(end=wall)


def test_contract_must_be_fully_specified():
    HistoricalContract(con_id=1, exchange="NYMEX")
    HistoricalContract(symbol="ABC", sec_type="IND", exchange="CBOE", currency="USD")
    HistoricalContract(symbol="XYZ", sec_type="CONTFUT", exchange="NYMEX", currency="USD")
    for bad in ({"con_id": 1}, {"symbol": "XYZ", "sec_type": "FUT", "exchange": "NYMEX",
                                "currency": "USD"},
                {"symbol": "T", "sec_type": "STK", "exchange": "SMART", "currency": "USD"},
                {"con_id": 1, "exchange": "X", "sec_type": "OPT"},
                {"con_id": 1, "exchange": "X", "sec_type": "STK", "include_expired": True},
                {"con_id": -1, "exchange": "X"}, {"con_id": True, "exchange": "X"},
                {"con_id": 1, "exchange": "X", "last_trade_date_or_contract_month": "2026-12"},
                {"con_id": 1, "exchange": "X", "include_expired": "yes"}):
        with pytest.raises(HistoricalRequestInvalid):
            HistoricalContract(**bad)


def test_contract_from_a_discovery_row():
    from tradingbot.ibkr.models import DiscoveredContract

    fields = {f: "" for f in DiscoveredContract.__dataclass_fields__}
    fields.update(con_id=7, symbol="XYZ", sec_type="FUT", exchange="NYMEX", currency="USD",
                  last_trade_date_or_contract_month="20261218", multiplier="1000",
                  trading_class="XYZ", local_symbol="XYZZ6", min_tick=0.01, price_magnifier=1,
                  under_con_id=0, strike=None, min_size=None, size_increment=None,
                  suggested_size_increment=None, query_match=True)
    k = HistoricalContract.from_discovered(DiscoveredContract(**fields), include_expired=True)
    assert (k.con_id, k.local_symbol, k.include_expired) == (7, "XYZZ6", True)


def test_no_keep_up_to_date_anywhere_in_the_request_model():
    assert not [f for f in HistoricalBarsRequest.__dataclass_fields__ if "keep" in f.lower()]
    assert H.request_to_jsonable(req())["keep_up_to_date"] is False
    src = (ROOT / "src/tradingbot/ibkr/session.py").read_text()
    call = src[src.index("self._client.reqHistoricalData("):]
    call = call[:call.index(")") + 1]
    # positional: reqId, contract, end, duration, bar size, what, useRTH, formatDate, keepUpToDate
    assert call.replace(" ", "").replace("\n", "").endswith("FORMAT_DATE_EPOCH,False,[])")


# --- bar time parsing (formatDate 2) -------------------------------------------------------------
def test_intraday_times_are_epoch_seconds_only():
    t, d = H.parse_bar_time("1773065700", intraday=True)
    assert t == datetime(2026, 3, 9, 14, 15, tzinfo=UTC) and t.tzinfo is UTC and d is None
    for bad in ("20260309 10:15:00 US/Eastern", "20260309  10:15:00", "-1773065700",
                " 1773065700", "1773065700.0", "", "17730657", 1773065700, None):
        with pytest.raises(H.BarMalformed):
            H.parse_bar_time(bad, intraday=True)


def test_day_bars_are_yyyymmdd_only():
    assert H.parse_bar_time("20260309", intraday=False) == (None, "2026-03-09")
    for bad in ("20260230", "2026039", "1773065700", "2026-03-09", "20260309 00:00:00"):
        with pytest.raises(H.BarMalformed):
            H.parse_bar_time(bad, intraday=False)


def _bar(date="1773065640", o=1.0, h=2.0, lo=0.5, c=1.5, prev=None, request=None, **kw):
    opts = {"volume": Decimal(3), "wap": None, "bar_count": 2}
    opts.update(kw)
    return H.build_bar(date, o, h, lo, c, request=request or req(), previous=prev, **opts)


def test_build_bar_validation():
    b = _bar()
    assert b.start_utc == END - timedelta(minutes=1) and b.volume == Decimal(3)
    assert _bar(volume=Decimal(-1), bar_count=-1).volume is None
    assert _bar(o=-37.6, h=-30.0, lo=-40.0, c=-37.0).close == -37.0  # negative prices are data
    for kw in ({"h": 0.9}, {"lo": 1.6}, {"o": float("inf")}, {"c": True}, {"o": "1"},
               {"date": "1773065700"}, {"volume": Decimal("NaN")}, {"bar_count": 1.5}):
        with pytest.raises(H.BarMalformed):
            _bar(**kw)
    with pytest.raises(H.BarMalformed, match="increasing"):
        _bar(date="1773065580", prev=b)
    daily = req(bar_size="1 day", duration="1 M")
    assert _bar(date="20260310", request=daily).session_date == "2026-03-10"  # session close day
    with pytest.raises(H.BarMalformed, match="after the requested end"):
        _bar(date="20260311", request=daily)


# --- results -------------------------------------------------------------------------------------
def test_result_never_carries_bars_when_failed_or_incomplete():
    b = _bar()
    ok = HistoricalBarsResult(request=req(), status=H.COMPLETE, reason=H.R_OK, complete=True,
                              bars=(b,), count=1)
    assert ok.count == 1
    with pytest.raises(Exception):  # noqa: B017 - frozen
        ok.bars = ()  # type: ignore[misc]
    for kw in ({"status": H.FAILED, "reason": H.R_TIMEOUT, "complete": False, "bars": (b,),
                "count": 1},
               {"status": H.COMPLETE, "reason": H.R_OK, "complete": False, "bars": (b,),
                "count": 1},
               {"status": H.COMPLETE, "reason": H.R_OK, "complete": True, "bars": (b,),
                "count": 2},
               {"status": H.FAILED, "reason": H.R_OK, "complete": True},
               {"status": H.FAILED, "reason": "WHATEVER", "complete": False}):
        with pytest.raises(ValueError):
            HistoricalBarsResult(request=req(), **kw)


@pytest.mark.parametrize(("code", "text", "reason"), [
    (162, "Historical Market Data Service error message:HMDS query returned no data: x",
     H.R_NO_DATA),
    (162, "Historical Market Data Service error message:No market data permissions for X",
     H.R_NO_PERMISSION),
    (162, "Historical Market Data Service error message:Pacing violation", H.R_PACING_VIOLATION),
    (162, "API historical data query cancelled: 1", H.R_HMDS_ERROR),
    (165, "Historical Market Data Service query message:x", H.R_HMDS_QUERY),
    (166, "HMDS Expired Contract Violation", H.R_EXPIRED_CONTRACT),
    (200, "No security definition", H.R_CONTRACT_NOT_FOUND),
    (321, "Error validating request", H.R_REQUEST_INVALID),
    (354, "Not subscribed", H.R_NO_PERMISSION), (10089, "x", H.R_NO_PERMISSION),
    (504, "Not connected", H.R_BROKER_ERROR),
])
def test_broker_error_classification(code, text, reason):
    assert H.classify_broker_error(code, text) == reason


# --- pacing --------------------------------------------------------------------------------------
class Clock:
    def __init__(self) -> None:
        self.t, self.sleeps = 0.0, []

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.t += s


def test_pacing_constants_are_at_or_inside_the_documented_limits():
    assert H.HIST_MIN_SPACING_SECONDS >= 2.0 / 5  # documented: < 6 per 2 s for one contract
    assert H.HIST_MAX_PER_WINDOW <= 60 and H.HIST_WINDOW_SECONDS >= 600
    assert H.HIST_IDENTICAL_INTERVAL_SECONDS >= 15
    assert (H.HIST_MIN_SPACING_SECONDS, H.HIST_WINDOW_SECONDS, H.HIST_MAX_PER_WINDOW,
            H.HIST_IDENTICAL_INTERVAL_SECONDS) == (3.0, 600.0, 50, 60.0)


def test_pacer_spacing_cap_identical_and_latch():
    clock = Clock()
    p = HistoricalPacer(clock=clock, sleep=clock.sleep)
    assert p.admit(("a",)) is None and clock.sleeps == []
    clock.t += 1.0
    assert p.admit(("b",)) is None and clock.sleeps == [2.0]  # waited to 3 s after the first
    assert p.admit(("a",)) == H.R_LOCAL_IDENTICAL  # within 60 s; nothing recorded
    clock.t += 61
    assert p.admit(("a",)) is None  # identical allowed again after the interval
    for i in range(H.HIST_MAX_PER_WINDOW - 3):
        assert p.admit((i,)) is None
    assert p.sent_in_window() == H.HIST_MAX_PER_WINDOW
    assert p.admit(("x",)) == H.R_LOCAL_PACING_CAP
    clock.t += H.HIST_WINDOW_SECONDS
    assert p.admit(("x",)) is None  # the rolling window moved on
    p.latch("broker pacing violation 162")
    assert p.admit(("y",)) == H.R_LOCAL_PACING_LATCHED and p.latched.startswith("broker")


def test_rolling_cap_counts_any_ten_minutes():
    clock = Clock()
    p = HistoricalPacer(clock=clock, sleep=clock.sleep, max_per_window=3, min_spacing=0.0)
    for k in range(3):
        assert p.admit((k,)) is None
        clock.t += 200
    assert p.admit(("late",)) is None  # the first left the window at t=600
    assert p.admit(("more",)) == H.R_LOCAL_PACING_CAP


def test_default_pacer_is_shared_by_sessions():
    src = (ROOT / "src/tradingbot/ibkr/session.py").read_text()
    assert "self._hist_pacer = historical_pacer or DEFAULT_HISTORICAL_PACER" in src


# --- request file and batch ----------------------------------------------------------------------
def test_example_request_file_parses_and_uses_invented_values():
    plan = load_plan(ROOT / "config" / "historical_bars.example.toml")
    assert len(plan.requests) == 3
    assert plan.requests[1].request.end_date_time == "20261102-14:30:00"  # NY local -> UTC (EST)
    text = (ROOT / "config" / "historical_bars.example.toml").read_text()
    assert "INVENTED" in text and "100000001" in text


BASE = {"name": "r", "end": END, "duration": "1800 S", "bar_size": "1 min",
        "what_to_show": "TRADES", "regular_hours_only": False,
        "contract": {"con_id": 1, "exchange": "NYMEX"}}


def plan_of(*entries, **top):
    return {"schema": "tradingbot.ibkr.historical_bars_requests/1", "request": list(entries),
            **top}


def test_request_file_refusals():
    parse_plan(plan_of(BASE))
    local = tomllib.loads('end = 2026-11-01T01:30:00')["end"]
    assert local.tzinfo is None
    for bad in (plan_of({**BASE, "end": local.replace(hour=9)}),  # naive, no zone
                plan_of({**BASE, "end": local, "time_zone": "America/New_York"}),  # ambiguous
                plan_of({**BASE, "time_zone": "America/New_York"}),  # zone and offset
                plan_of({**BASE, "end": local.replace(hour=9), "time_zone": "Mars/Base"}),
                plan_of({**BASE, "end": "2026-03-09T14:15:00Z"}),
                plan_of({**BASE, "keep_up_to_date": True}),
                plan_of({**BASE, "contract": {"con_id": 1, "exchange": "X", "strike": 1.0}}),
                plan_of({**BASE, "contract": {"con_id": "1", "exchange": "X"}}),
                plan_of(BASE, BASE), plan_of(), plan_of(BASE, request_timeout=0),
                plan_of(*({**BASE, "name": f"r{i}"} for i in range(51))),
                {**plan_of(BASE), "schema": "other"}, {**plan_of(BASE), "extra": 1}):
        with pytest.raises(RequestFileInvalid):
            parse_plan(bad)
    ok = parse_plan(plan_of({**BASE, "end": local.replace(hour=9), "time_zone":
                             "America/New_York"}))
    assert ok.requests[0].request.end_date_time == "20261101-14:30:00"


def test_output_path_refusals(tmp_path):
    with pytest.raises(OutputPathRefused, match="inside the repository"):
        check_output_path(ROOT / "bars.json", ROOT)
    with pytest.raises(OutputPathRefused, match="inside the repository"):
        check_output_path(ROOT / "docs" / ".." / "x.json", ROOT)
    link = tmp_path / "into_repo"
    link.symlink_to(ROOT)
    with pytest.raises(OutputPathRefused, match="inside the repository"):
        check_output_path(link / "x.json", ROOT)
    existing = tmp_path / "exists.json"
    existing.write_text("{}")
    with pytest.raises(OutputPathRefused, match="exists"):
        check_output_path(existing, ROOT)
    with pytest.raises(OutputPathRefused, match="does not exist"):
        check_output_path(tmp_path / "missing" / "x.json", ROOT)
    out = check_output_path(tmp_path / "ok.json", ROOT)
    write_private_report({"a": 1}, out)
    assert stat.S_IMODE(os.stat(out).st_mode) == 0o600
    with pytest.raises(FileExistsError):
        write_private_report({"a": 1}, out)


def test_script_validates_before_reading_config_or_connecting(tmp_path, monkeypatch, capsys):
    import importlib.util

    spec = importlib.util.spec_from_file_location("ibkr_historical_bars_script",
                                                  ROOT / "scripts" / "ibkr_historical_bars.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    called = []
    monkeypatch.setattr(mod, "run_batch", lambda *a, **k: called.append(a) or {})
    bad = tmp_path / "bad.toml"
    bad.write_text('schema = "nope"\n')
    example = str(ROOT / "config" / "historical_bars.example.toml")
    assert mod.main(["--requests", str(bad), "--out", str(tmp_path / "o.json")]) == 2
    assert mod.main(["--requests", example, "--out", str(ROOT / "o.json")]) == 2
    assert called == [] and not (ROOT / "o.json").exists()


def test_batch_setup_failure_and_summary_prints_only_counts_and_reasons():
    plan = parse_plan(plan_of({**BASE, "name": "secret-post-label"}))

    def loader(_p):
        raise OSError("no config")

    rep = run_batch(plan, loader=loader)
    assert rep["overall"] == "FAILED" and rep["failure"].startswith("setup")
    rep2 = {"overall": "COMPLETE", "account": "DU…01", "session": {"read_only_status": "VERIFIED"},
            "bars_total": 30, "reason_counts": {"OK": 1}, "failure": None,
            "results": [{"index": 0, "name": "secret-post-label", "reason": "OK", "count": 30,
                         "error_code": None, "error_message": "broker text",
                         "bars": [{"close": 71.23}]}]}
    text = summarize(rep2)
    assert "secret-post-label" not in text and "71.23" not in text and "broker text" not in text
    assert "OK" in text and "bars=30" in text


def test_historical_module_is_sdk_free_and_has_no_order_vocabulary():
    for name in ("historical.py", "historical_batch.py"):
        tree = ast.parse((ROOT / "src/tradingbot/ibkr" / name).read_text())
        imports = {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        imports |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        assert not [m for m in imports if m.startswith(("ibapi", "tradingbot.ibkr_orders"))]
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        names |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        assert not names & {"placeOrder", "cancelOrder", "reqGlobalCancel",
                            "updateConfigProtoBuf", "exerciseOptions"}
