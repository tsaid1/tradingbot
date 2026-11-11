"""The IBKR Paper Check page shows the latest check report honestly. Invented reports only.

Drives ``dashboard/pages/7_IBKR_Paper_Check.py`` headlessly through ``streamlit.testing.v1.AppTest``
with reports written into a temporary directory (never the real ones).
"""

from __future__ import annotations

import ast
import copy
import http.client
import json
import socket
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from tradingbot.ibkr.report_view import INVALID_REPORT, load_report

REPO = Path(__file__).resolve().parents[1]
PAGE = REPO / "dashboard" / "pages" / "7_IBKR_Paper_Check.py"
FULL = "DU7654321"  # invented full account ID that must never be shown
MASKED = "DU…21"


def _iso(minutes_ago: float) -> str:
    return (datetime.now(UTC) - timedelta(minutes=minutes_ago)).isoformat(timespec="seconds")


def _attempt(route, **kw):
    base = {"con_id": 15547841, "symbol": "TLT", "bid": None, "bid_size": None, "ask": None,
            "ask_size": None, "last": None, "last_size": None, "market_data_type": None,
            "market_data_type_name": "UNREPORTED", "requested_market_data_type": 3,
            "received_at": "2026-11-12T10:00:06.000+00:00", "last_trade_time": None,
            "quality": "UNAVAILABLE", "flags": ["MISSING_BID", "MISSING_ASK",
                                                "QUOTE_AGE_UNKNOWN"],
            "notices": [], "unavailable_code": None, "route": route, "close": None,
            "snapshot_end_missing": False, "waited_ms": 8000, "tick_counts": {},
            "market_data_types_delivered": [], "tick_req_params": None, "request_events": [],
            "request_error": None, "first_tick_ms": None, "last_tick_ms": None,
            "quote_age_seconds": None, "ended_early": False, "window_ms": 8000}
    base.update(kw)
    return base


STREAM = _attempt(
    "stream", bid=11.11, bid_size="1800", ask=11.13, ask_size="2200", last=11.12,
    last_size="574", market_data_type=3, market_data_type_name="DELAYED", quality="DELAYED",
    flags=[], last_trade_time="2026-11-12T09:44:00+00:00", quote_age_seconds=906.5,
    close=11.0, waited_ms=6321, ended_early=True, first_tick_ms=5322, last_tick_ms=5329,
    tick_counts={"DELAYED_BID": 1, "DELAYED_ASK": 1, "DELAYED_LAST": 1},
    market_data_types_delivered=[3],
    tick_req_params={"minTick": 0.01, "bboExchange": "9c0001", "snapshotPermissions": 4})
SNAPSHOT = _attempt("snapshot", snapshot_end_missing=True, waited_ms=20004, window_ms=20000)


def _report(minutes_ago: float = 5, **over) -> dict:
    preflight = {"paper_profile": {"profile": "/Users/someone/Jts/jts.ini", "owner": "someone",
                                   "tradingMode": "p"},
                 "listener": {"port": 7497, "listeners": ["*.7497"], "pids": [4242],
                              "user": "someone"}}
    session = {"server_version": 226, "tws_connection_time": "20261112 12:00:00 EET",
               "connected_at": _iso(minutes_ago + 1), "notices": [], "connection_events": [],
               "unrouted_errors": [], "cleanup_failures": [], "internal_errors": [],
               "poisoned": False, "preflight": preflight, "read_only_status": "VERIFIED",
               "read_only_settings": {"readOnlyApi": True, "socketPort": 7497,
                                      "allowLocalhostOnly": True},
               "reader_joined": True}
    steps = [
        {"name": "load_config", "status": "PASS", "duration_ms": 1},
        {"name": "create_session", "status": "PASS", "duration_ms": 170},
        {"name": "connect_identity_gate", "status": "PASS", "duration_ms": 59},
        {"name": "resolve_contract", "status": "PASS", "duration_ms": 143, "detail": {
            "con_id": 15547841, "symbol": "TLT", "sec_type": "STK", "currency": "USD",
            "exchange": "SMART", "primary_exchange": "NASDAQ", "local_symbol": "TLT",
            "min_tick": 0.01, "time_zone_id": "US/Eastern", "trading_hours": "x",
            "liquid_hours": "y"}},
        {"name": "account_summary", "status": "PASS", "duration_ms": 1157, "detail": {
            "account_masked": MASKED, "values": {
                "AccountType": {"value": "INDIVIDUAL", "currency": ""},
                "NetLiquidation": {"value": "123456.78", "currency": "USD"}},
            "missing_tags": []}},
        {"name": "positions", "status": "PASS", "duration_ms": 101, "detail": {
            "count": 1, "rows": [{"account_masked": MASKED, "con_id": 15547841,
                                  "symbol": "TLT", "quantity": "7", "avg_cost": 10.5}]}},
        {"name": "quote", "status": "PASS_WITH_WARNINGS", "duration_ms": 6330, "detail": {
            "verdict": "PASS_WITH_WARNINGS",
            "reason": "only DELAYED two-sided data (quality DELAYED) via stream",
            "snapshot_fallback": True, "routes": ["stream", "snapshot"],
            "best_route": "stream", "attempts": [STREAM, SNAPSHOT]}},
        {"name": "disconnect", "status": "PASS", "duration_ms": 326},
        {"name": "reconnect_identity_gate", "status": "PASS", "duration_ms": 49},
        {"name": "reconnect_disconnect", "status": "SKIPPED", "duration_ms": 0,
         "reason": "invented skip reason"},
    ]
    report = {"schema": "tradingbot.ibkr.paper_check/1", "started_at": _iso(minutes_ago + 1),
              "read_only": True, "notes": [], "account": MASKED, "mode": "paper",
              "host": "127.0.0.1", "port": 7497, "client_id": 6601, "tws_os_user": "someone",
              "read_only_check": "config-read", "sdk_version": "1052.1", "session": session,
              "steps": steps, "warnings": ["TLT quote: only DELAYED two-sided data"],
              "finished_at": _iso(minutes_ago), "overall": "PASS_WITH_WARNINGS"}
    report.update(over)
    return report


def _write(d: Path, report, name="ibkr_paper_check_20261112T100000Z.json") -> Path:
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text(report if isinstance(report, str) else json.dumps(report), encoding="utf-8")
    return p


def page(monkeypatch, report_dir: Path) -> AppTest:
    monkeypatch.setenv("TRADINGBOT_IBKR_REPORT_DIR", str(report_dir))
    at = AppTest.from_file(str(PAGE), default_timeout=60)
    at.run()
    assert not at.exception, at.exception
    return at


def shown(at: AppTest) -> str:
    parts: list[str] = []
    for kind in ("title", "header", "subheader", "error", "warning", "info", "success",
                 "markdown", "caption", "code", "text", "json"):
        parts += [str(e.value) for e in at.get(kind)]
    parts += [t.value.to_string() for t in at.table]
    parts += [f"{m.label} {m.value}" for m in at.metric]
    parts += [e.label for e in at.expander]
    return "\n".join(parts)


def test_page_shows_every_required_part_of_the_report(tmp_path, monkeypatch):
    _write(tmp_path, _report())
    at = page(monkeypatch, tmp_path)
    text = shown(at)
    assert at.text_input[0].value == str(tmp_path)  # default comes from the script's env var
    metrics = {m.label: m.value for m in at.metric}
    assert metrics["Overall"] == "PASS_WITH_WARNINGS" and metrics["Account (masked)"] == MASKED
    assert metrics["Report age"] == "5 min"
    assert any("Overall verdict: PASS_WITH_WARNINGS" in w.value for w in at.warning)
    for needle in (
        "Profile mode (tradingMode)", "Listener owner", "someone",  # pre-socket checks
        "VERIFIED", "readOnlyApi", "socketPort", "allowLocalhostOnly", "config-read",
        "1052.1", "226",  # SDK and server versions
        "15547841", "NASDAQ", "US/Eastern",  # contract
        "NetLiquidation", "123456.78", "INDIVIDUAL",  # account summary
        "count: 1", "avg_cost",  # positions
        "3 (DELAYED)", "DELAYED", "2026-11-12T09:44:00+00:00", "906.5",  # quote
        "snapshotPermissions", "snapshot_end_missing",
        "Attempt 1: stream evidence", "Attempt 2: snapshot evidence", "ended_early",
        "connect_identity_gate", "1157", "6330", "invented skip reason",  # steps
        "WARNING TLT quote: only DELAYED two-sided data",
        "Last-trade age dates the last trade only",
    ):
        assert needle in text, needle
    assert not any("STALE" in e.value for e in at.error)


def test_newest_report_by_name_is_shown(tmp_path, monkeypatch):
    _write(tmp_path, _report(overall="FAIL"), "ibkr_paper_check_20261111T090000Z.json")
    _write(tmp_path, _report(), "ibkr_paper_check_20261112T090000Z.json")
    _write(tmp_path, _report(overall="PASS"), "ibkr_paper_check_20261112T090000Z_1.json")
    at = page(monkeypatch, tmp_path)
    assert {m.label: m.value for m in at.metric}["Overall"] == "PASS"
    assert "ibkr_paper_check_20261112T090000Z_1.json" in shown(at)


def test_text_input_overrides_the_directory(tmp_path, monkeypatch):
    _write(tmp_path / "a", _report(overall="FAIL"))
    _write(tmp_path / "b", _report(overall="PASS"))
    at = page(monkeypatch, tmp_path / "a")
    assert {m.label: m.value for m in at.metric}["Overall"] == "FAIL"
    at.text_input[0].set_value(str(tmp_path / "b")).run()
    assert {m.label: m.value for m in at.metric}["Overall"] == "PASS"


def test_stale_banner_follows_the_configurable_threshold(tmp_path, monkeypatch):
    _write(tmp_path, _report(minutes_ago=180))
    at = page(monkeypatch, tmp_path)
    stale = [e.value for e in at.error if e.value.startswith("STALE REPORT")]
    assert len(stale) == 1 and "3.0 h" in stale[0] and "60-minute" in stale[0]
    at.number_input[0].set_value(240).run()
    assert not any(e.value.startswith("STALE REPORT") for e in at.error)


BAD_REPORTS = {
    "not_json": "{this is not json " + FULL,
    "nan": json.dumps(_report()).replace('"duration_ms": 1}', '"duration_ms": NaN}', 1),
    "list_root": json.dumps([_report()]),
    "wrong_schema": json.dumps(_report(schema="tradingbot.ibkr.paper_check/2")),
    "no_steps": json.dumps({k: v for k, v in _report().items() if k != "steps"}),
    "bad_step": json.dumps(_report(steps=[{"name": FULL, "status": "MAYBE",
                                           "duration_ms": 1}])),
    "bad_verdict": json.dumps(_report(overall="GREEN")),
    "naive_time": json.dumps(_report(finished_at="2026-11-12T10:00:00")),
    "not_read_only": json.dumps(_report(read_only=False)),
    "binary": None,  # written as invalid UTF-8 bytes
}


@pytest.mark.parametrize("name", list(BAD_REPORTS))
def test_anything_but_a_valid_report_gets_the_fixed_message(tmp_path, monkeypatch, name):
    if name == "nan":
        assert "NaN" in BAD_REPORTS[name]  # the mutation really applied
    p = tmp_path / "ibkr_paper_check_20261112T100000Z.json"
    if name == "binary":
        p.write_bytes(b"\xff\xfe\x00garbage")
    else:
        _write(tmp_path, BAD_REPORTS[name])
    at = page(monkeypatch, tmp_path)
    assert [e.value for e in at.error] == [INVALID_REPORT]
    assert not at.metric and not at.table and not at.json
    assert FULL not in shown(at)


def test_full_account_id_is_never_displayed(tmp_path, monkeypatch):
    r = _report(account=FULL, overall="FAIL")
    r["session"]["notices"] = [{"reqId": -1, "code": 2104, "message": f"farm ok for {FULL}"}]
    r["steps"][4]["detail"]["account_masked"] = FULL
    r["steps"][4]["detail"]["values"][FULL] = {"value": FULL, "currency": "USD"}
    r["steps"][5]["detail"]["rows"][0]["account_masked"] = FULL
    r["steps"].append({"name": "redaction_guard", "status": "FAIL", "duration_ms": 0,
                       "error": f"leaked {FULL} and U1234567"})
    r["warnings"].append(f"warning about {FULL}")
    _write(tmp_path, r, f"ibkr_paper_check_20261112T100000Z_{FULL}.json")
    at = page(monkeypatch, tmp_path)
    text = shown(at)
    assert FULL not in text and "U1234567" not in text
    assert MASKED in text and "leaked DU…21 and U1…67" in text


def test_redaction_applies_to_keys_and_values():
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        r = _report()
        r["session"][FULL] = [FULL, {FULL: FULL}]
        loaded = load_report(_write(Path(d), r))
    assert FULL not in json.dumps(loaded.report, ensure_ascii=False)


def test_missing_directory_or_report_is_reported_plainly(tmp_path, monkeypatch):
    at = page(monkeypatch, tmp_path / "nowhere")
    assert any("No ibkr_paper_check_" in w.value for w in at.warning) and not at.metric


def test_old_report_without_new_quote_fields_still_renders(tmp_path, monkeypatch):
    r = _report()
    detail = r["steps"][6]["detail"]
    for key in ("routes", "best_route", "snapshot_fallback"):
        del detail[key]
    for a in detail["attempts"]:
        for key in ("quote_age_seconds", "ended_early", "window_ms"):
            del a[key]
    _write(tmp_path, copy.deepcopy(r))
    at = page(monkeypatch, tmp_path)
    text = shown(at)
    assert "not in report" in text and "3 (DELAYED)" in text
    assert INVALID_REPORT not in text


def test_page_has_no_broker_path_and_no_side_effects(tmp_path, monkeypatch):
    source = PAGE.read_text(encoding="utf-8")
    tree = ast.parse(source)
    modules = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    modules |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert modules == {"__future__", "json", "pandas", "streamlit", "tradingbot.ibkr.check",
                       "tradingbot.ibkr.report_view"}, modules
    for token in ("ibapi", "IbkrSession", "session_factory", "run_check", "connect(",
                  "subprocess", "placeOrder", "write_report", "open(", "socket.", "import socket"):
        assert token not in source, token

    def deny(*args, **kwargs):
        raise AssertionError("the page attempted a network connection")

    for target, name in ((socket, "create_connection"), (socket.socket, "connect"),
                         (socket.socket, "connect_ex"), (urllib.request, "urlopen"),
                         (http.client.HTTPConnection, "connect")):
        monkeypatch.setattr(target, name, deny)
    _write(tmp_path, _report())
    before = {p: p.stat().st_mtime_ns for p in tmp_path.rglob("*")}
    at = page(monkeypatch, tmp_path)
    assert [w.label for w in at.text_input] == ["Report directory"]
    assert [w.label for w in at.number_input] == ["Stale after (minutes)"]
    for widget in ("button", "checkbox", "toggle", "selectbox", "radio", "file_uploader",
                   "text_area", "slider", "download_button", "chat_input"):
        assert not list(getattr(at, widget, [])), widget
    assert {p: p.stat().st_mtime_ns for p in tmp_path.rglob("*")} == before
