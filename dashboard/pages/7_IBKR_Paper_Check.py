"""IBKR Paper Check: a READ-ONLY view of the latest report written by scripts/ibkr_paper_check.py.

The page reads one JSON file, the newest ``ibkr_paper_check_*.json`` in the report directory (the
script's default: ``$TRADINGBOT_IBKR_REPORT_DIR``, else ``private/ibkr_reports`` in the workspace;
a text input overrides it). It shows the file only if it is valid JSON of schema
``tradingbot.ibkr.paper_check/1``; anything else gets one fixed message. Every string passes through
the package's account redaction before it is rendered. The page imports no SDK and no session,
opens no socket, writes nothing and has no control that connects to the broker
(tests/test_ibkr_paper_check_page.py verifies).
"""

from __future__ import annotations

import json

import pandas as pd
import streamlit as st

from tradingbot.ibkr.check import REPORT_DIR_ENV, default_report_dir
from tradingbot.ibkr.report_view import (
    INVALID_REPORT,
    InvalidReport,
    best_attempt,
    human_age,
    latest_report_path,
    load_report,
    redact_deep,
    report_age_seconds,
)

BANNER = ("READ-ONLY REPORT VIEW: this page shows a file written earlier by "
          "scripts/ibkr_paper_check.py. It never connects to TWS, places no order and changes no "
          "setting. A report describes the moment it ran, not the broker now.")
DEFAULT_STALE_MINUTES = 60
EVIDENCE_KEYS = ("route", "market_data_type", "market_data_type_name",
                 "requested_market_data_type", "quality", "flags", "bid", "bid_size", "ask",
                 "ask_size", "last", "last_size", "close", "last_trade_time", "quote_age_seconds",
                 "received_at", "window_ms", "waited_ms", "ended_early", "first_tick_ms",
                 "last_tick_ms", "snapshot_end_missing", "tick_counts",
                 "market_data_types_delivered", "tick_req_params", "request_events",
                 "request_error", "notices", "unavailable_code")


def text(value) -> str:
    if value is None:
        return "—"
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True)
    return str(value)


def table(rows: list[dict] | dict, columns: tuple[str, str] = ("Field", "Value")) -> None:
    """Plain-text table (no markdown is interpreted in cells)."""
    if isinstance(rows, dict):
        rows = [{columns[0]: k, columns[1]: text(v)} for k, v in rows.items()]
    else:
        rows = [{k: text(v) for k, v in r.items()} for r in rows]
    if rows:
        st.table(pd.DataFrame(rows))
    else:
        st.text("none")


def step(report: dict, name: str) -> dict | None:
    return next((s for s in report["steps"] if s.get("name") == name), None)


def detail(report: dict, name: str) -> dict:
    s = step(report, name)
    d = s.get("detail") if s else None
    return d if isinstance(d, dict) else {}


st.set_page_config(page_title="IBKR Paper Check (report)", page_icon="📄", layout="wide")
st.title("IBKR Paper Check (latest report, read-only)")
st.info(BANNER)

with st.sidebar:
    st.header("Report")
    default_dir = default_report_dir()
    report_dir = st.text_input(
        "Report directory", value=str(default_dir) if default_dir else "",
        key="ibkr_report_dir",
        help=f"Default: ${REPORT_DIR_ENV}, else private/ibkr_reports in the workspace.")
    stale_minutes = st.number_input("Stale after (minutes)", min_value=1, max_value=100_000,
                                    value=DEFAULT_STALE_MINUTES, step=5, key="ibkr_stale_minutes")
    st.caption("Nothing on this page connects to the broker. Re-run the check script to refresh.")

if not report_dir.strip():
    st.warning("No report directory: set one in the sidebar or set "
               f"{REPORT_DIR_ENV}.")
    st.stop()
path = latest_report_path(report_dir.strip())
if path is None:
    st.warning("No ibkr_paper_check_*.json report found in that directory.")
    st.stop()

try:
    loaded = load_report(path)
    r = loaded.report
    age = report_age_seconds(r)
except InvalidReport:
    st.error(INVALID_REPORT)
    st.stop()


def render(r: dict, name: str, age: float) -> None:
    session = r.get("session") if isinstance(r.get("session"), dict) else {}
    overall = r["overall"]
    stale = age > stale_minutes * 60
    if stale:
        st.error(f"STALE REPORT: finished {human_age(age)} ago, older than the "
                 f"{stale_minutes}-minute threshold. It does not describe the broker now.")
    verdict_box = {"PASS": st.success, "PASS_WITH_WARNINGS": st.warning}.get(overall, st.error)
    verdict_box(f"Overall verdict: {overall}")
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Overall", overall)
    m2.metric("Finished (UTC)", r["finished_at"])
    m3.metric("Report age", human_age(age))
    m4.metric("Account (masked)", text(r.get("account")))
    st.caption(f"File: {name} · started {r['started_at']} · finished {r['finished_at']}")

    st.subheader("Pre-socket checks")
    pre = session.get("preflight") if isinstance(session.get("preflight"), dict) else {}
    profile = pre.get("paper_profile") if isinstance(pre.get("paper_profile"), dict) else {}
    listener = pre.get("listener") if isinstance(pre.get("listener"), dict) else {}
    table({"Profile mode (tradingMode)": profile.get("tradingMode"),
           "Profile file": profile.get("profile"), "Profile owner": profile.get("owner"),
           "Listener port": listener.get("port"), "Listener owner": listener.get("user"),
           "Listener sockets": listener.get("listeners"), "Listener PIDs": listener.get("pids")})

    st.subheader("Read-Only API")
    settings = session.get("read_only_settings")
    settings = settings if isinstance(settings, dict) else {}
    table({"Status": session.get("read_only_status"), "Check": r.get("read_only_check"),
           "readOnlyApi": settings.get("readOnlyApi"), "socketPort": settings.get("socketPort"),
           "allowLocalhostOnly": settings.get("allowLocalhostOnly")})

    st.subheader("Connection")
    table({"Account (masked)": r.get("account"), "Mode": r.get("mode"), "Host": r.get("host"),
           "Port": r.get("port"), "Client ID": r.get("client_id"),
           "SDK version": r.get("sdk_version"), "Server version": session.get("server_version"),
           "TWS connection time": session.get("tws_connection_time"),
           "Connected at": session.get("connected_at"),
           "Reader thread joined": session.get("reader_joined"),
           "Poisoned": session.get("poisoned")})

    st.subheader("TLT contract")
    table(detail(r, "resolve_contract"))

    st.subheader("Account summary")
    summary = detail(r, "account_summary")
    values = summary.get("values") if isinstance(summary.get("values"), dict) else {}
    table([{"Tag": tag, "Value": v.get("value") if isinstance(v, dict) else v,
            "Currency": v.get("currency") if isinstance(v, dict) else None}
           for tag, v in values.items()])
    if summary.get("missing_tags"):
        st.error(f"Missing tags: {text(summary['missing_tags'])}")

    st.subheader("Positions")
    positions = detail(r, "positions")
    rows = positions.get("rows") if isinstance(positions.get("rows"), list) else []
    st.text(f"count: {text(positions.get('count'))}")
    table([p for p in rows if isinstance(p, dict)])

    st.subheader("Quote")
    quote = detail(r, "quote")
    best = best_attempt(quote)
    qstep = step(r, "quote")
    table({"Step status": qstep.get("status") if qstep else None,
           "Verdict": quote.get("verdict"), "Reason": quote.get("reason"),
           "Routes tried": quote.get("routes") or [a.get("route") for a in
                                                   quote.get("attempts", [])
                                                   if isinstance(a, dict)],
           "Snapshot fallback requested": quote.get("snapshot_fallback")})
    if best is not None:
        age_q = best.get("quote_age_seconds") if "quote_age_seconds" in best else "not in report"
        table({"Route": best.get("route"),
               "Market data type": f"{text(best.get('market_data_type'))} "
                                   f"({text(best.get('market_data_type_name'))})",
               "Quality": best.get("quality"), "Flags": best.get("flags"),
               "Bid": best.get("bid"), "Ask": best.get("ask"), "Last": best.get("last"),
               "Last-trade time": best.get("last_trade_time"),
               "Last-trade age (s)": age_q})
        st.caption("Last-trade age dates the last trade only; it says nothing about how fresh the "
                   "bid or ask is.")
    for n, attempt in enumerate(quote.get("attempts", []), start=1):
        if not isinstance(attempt, dict):
            continue
        with st.expander(f"Attempt {n}: {text(attempt.get('route'))} evidence"):
            table({k: attempt.get(k) for k in EVIDENCE_KEYS if k in attempt})

    st.subheader("Steps")
    table([{"Step": s.get("name"), "Status": s.get("status"), "Duration (ms)": s.get("duration_ms"),
            "Error / reason": s.get("error") or s.get("reason")} for s in r["steps"]])

    st.subheader("Warnings")
    if r["warnings"]:
        for w in r["warnings"]:
            st.text(f"WARNING {w}")
    else:
        st.text("none")
    for note in r.get("notes") or []:
        st.text(f"NOTE {text(note)}")

    with st.expander("Full report (redacted)"):
        st.json(redact_deep(r))


try:
    render(r, loaded.name, age)
except Exception:  # noqa: BLE001 - a structurally odd report never shows a traceback
    st.error(INVALID_REPORT)

st.divider()
st.caption("This page has no control that connects, orders, cancels, resets or changes settings. "
           "docs/IBKR_ADAPTER.md has the model.")
