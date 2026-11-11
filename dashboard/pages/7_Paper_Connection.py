"""Paper Connection: a READ-ONLY view of one saved paper-read diagnostic report. NON_AUTHORITATIVE.

The operator may upload one saved, sanitized JSON result of the read-only paper diagnostic. The page
checks it against a closed shape and renders only fixed, validated fields:
- the reported verdict and stop
- the identity-match status, never a raw account or key
- the TLT and read-stream statuses
- fixed failure and cleanup codes
- last-trade timing and local receipt time
- the stated unknowns
- ``trading_permission = False``

Sections that can carry payloads (envelopes, findings, router report, request and callback logs, the
operator attestation) are recognised and never displayed. A file with an unknown field, code or shape is
refused as a whole, with a fixed code that never echoes its content.

What this page is not: it connects to nothing, imports no SDK, starts nothing, writes nothing and reads no
file except the one upload, held in memory. It does not recalculate or loosen the reported verdict. A
reported PASS is evidence somebody reported; it is neither authentication nor live verification. The
upload's origin is not independently verified: compare its SHA-256 digest with the reviewed result.
tests/test_paper_connection_page.py verifies this; see docs/PAPER_CONNECTION_VIEW.md.
"""

from __future__ import annotations

import builtins
import hashlib
import json
import math
import re
from datetime import datetime

import streamlit as st

from tradingbot.paper_readiness.account_position_callbacks import AccountIssueCode
from tradingbot.paper_readiness.callback_router import StreamKind
from tradingbot.paper_readiness.ibkr_callbacks import IssueCode
from tradingbot.paper_readiness.managed_accounts_input import Arrival, Invalidation, Registration
from tradingbot.paper_readiness.observations import MarketDataType

MAX_REPORT_BYTES = 64 * 1024  # application cap, checked before parsing
MAX_DEPTH = 16
MAX_NODES = 20_000
MAX_LIST = 64

# The closed vocabulary of the diagnostic's result document (accepted output shape, head f018e00).
STOPS = frozenset({
    "COMPLETE", "CONNECT_FAILED", "IDENTITY_TIMEOUT", "ACCOUNT_NOT_EXPECTED", "READ_REGISTRATION_FAILED",
    "CONTRACT_TIMEOUT", "CONTRACT_REJECTED", "STREAM_REJECTED", "READS_TIMEOUT", "DEADLINE", "CALLBACK_CAP",
    "ROUTER_FAULT", "CONNECTION_LOST", "RUN_RAISED",
})
PHASES = frozenset({"NOT_STARTED", "CONNECT", "IDENTITY", "CONTRACT", "READS"})
FAILURE_CODES = frozenset({
    "CONNECT_RAISED", "SERVER_VERSION_RAISED", "SHUTDOWN_RAISED", "IS_CONNECTED_RAISED",
    "SOCKET_OBSERVATION_RAISED", "INDEPENDENT_DISCONNECT_RAISED", "READER_JOIN_RAISED", "RUN_RAISED",
    "INTERRUPTED", "MANAGED_CLOSE_RAISED", "RESULT_RAISED",
})
CLEANUP_CODES = frozenset({"INDEPENDENT_DISCONNECT"})
CHECKS = (
    "stop_complete", "no_run_failures", "streams_complete_without_issues", "owner_valid",
    "rows_match_expected_account_reference", "contract_accepted_before_quote", "assessments_all_info",
    "router_faults_none", "adapter_failures_documented_only", "read_stops_returned", "disconnect_returned",
    "socket_and_consumer_stopped", "ledger_faults_none", "reader_not_alive_after_join", "no_forbidden_calls",
)
PARTIAL_BYTES = frozenset({"NONE", "UNRESOLVED", "UNKNOWN"})
STREAM_ISSUES = frozenset(c.value for c in IssueCode) | frozenset(c.value for c in AccountIssueCode)
STREAMS = tuple(k.value for k in StreamKind)
LAST_TRADE_SOURCE = "tick 45 last-trade timestamp, as mapped by the unchanged normalizer"
STREAM_FIELDS = frozenset({"stream", "registered", "completed", "issue_codes", "issue_overflow"})
OWNER_FIELDS = frozenset({"arrival", "invalidation", "invalidating_events", "registration", "closed",
                          "router_completed", "completed", "authority"})
RENDERED = frozenset({
    "authority", "trading_permission", "run_id", "plan_sha256", "stop", "phase", "failures", "cleanup",
    "market_data_type_delivered", "quote_timing", "tlt_con_id", "streams", "managed_accounts",
    "lifecycle_local_completion", "reader_partial_bytes", "reader_alive_after_join", "checks", "verdict",
})
SUPPRESSED = ("callbacks", "endpoint", "envelopes", "findings", "limits", "marks_s", "operator_attestation",
              "requests_sent", "router_report", "server_version")
REQUIRED = ("authority", "trading_permission", "plan_sha256", "verdict")
_FAILURE = re.compile(r"([A-Z_]{1,40})(?::([A-Za-z_][A-Za-z0-9_]{0,63}))?")
_HEX = re.compile(r"[0-9a-f]+")


class Refused(Exception):
    """A fixed refusal code. It never carries any part of the uploaded file."""


# ----------------------------------------------------------------------------- closed parsing


def _no_duplicates(pairs: list) -> dict:
    keys = [k for k, _ in pairs]
    if len(keys) != len(set(keys)):
        raise Refused("DUPLICATE_KEY")
    return dict(pairs)


def _non_finite(_: str) -> object:
    raise Refused("NON_FINITE_NUMBER")


def _bounded(document: object) -> None:
    """At most MAX_DEPTH levels and MAX_NODES values, walked iteratively."""
    stack, nodes = [(document, 1)], 0
    while stack:
        value, depth = stack.pop()
        nodes += 1
        if depth > MAX_DEPTH:
            raise Refused("TOO_DEEP")
        if nodes > MAX_NODES:
            raise Refused("TOO_MANY_VALUES")
        if isinstance(value, dict):
            stack.extend((v, depth + 1) for v in value.values())
        elif isinstance(value, list):
            stack.extend((v, depth + 1) for v in value)


def load(data: bytes) -> dict:
    if len(data) > MAX_REPORT_BYTES:
        raise Refused("OVERSIZE")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise Refused("NOT_UTF8") from None
    try:
        document = json.loads(text, object_pairs_hook=_no_duplicates, parse_constant=_non_finite)
    except (ValueError, RecursionError):
        raise Refused("MALFORMED_JSON") from None
    _bounded(document)
    if type(document) is not dict:
        raise Refused("NOT_AN_OBJECT")
    return document


def _bool(value: object, code: str, *, optional: bool = False) -> bool | None:
    if value is None and optional:
        return None
    if type(value) is not bool:
        raise Refused(code)
    return value


def _choice(value: object, allowed: frozenset, code: str, *, optional: bool = True) -> str | None:
    if value is None and optional:
        return None
    if type(value) is not str or value not in allowed:
        raise Refused(code)
    return value


def _count(value: object, code: str) -> int:
    if type(value) is not int or not 0 <= value <= 10**9:
        raise Refused(code)
    return value


def _number(value: object, code: str, *, optional: bool = True) -> float | None:
    if value is None and optional:
        return None
    if type(value) not in (int, float) or not math.isfinite(value) or abs(value) > 10**9:
        raise Refused(code)
    return float(value)


def _aware(value: object, code: str) -> datetime | None:
    if value is None:
        return None
    if type(value) is not str or len(value) > 40:
        raise Refused(code)
    try:
        stamp = datetime.fromisoformat(value)
    except ValueError:
        raise Refused(code) from None
    if stamp.tzinfo is None:
        raise Refused(code)
    return stamp


def _list(value: object, code: str) -> list:
    if type(value) is not list or len(value) > MAX_LIST:
        raise Refused(code)
    return value


def _failure(entry: object) -> str:
    match = _FAILURE.fullmatch(entry) if type(entry) is str else None
    if match is None or match.group(1) not in FAILURE_CODES:
        raise Refused("UNRECOGNIZED_FAILURE_CODE")
    name = match.group(2)
    if name is not None and name != "NON_BUILTIN_EXCEPTION":
        kind = getattr(builtins, name, None)
        if not (isinstance(kind, type) and issubclass(kind, BaseException)):
            raise Refused("UNRECOGNIZED_FAILURE_CODE")
    return entry


def report_view(document: dict) -> dict:
    """The closed, validated fields to render, plus the names of recognised sections left undisplayed."""
    unknown = set(document) - RENDERED - set(SUPPRESSED)
    if unknown:
        raise Refused("UNRECOGNIZED_FIELD")
    if any(key not in document for key in REQUIRED):
        raise Refused("REQUIRED_FIELD_MISSING")
    if document["authority"] != "NON_AUTHORITATIVE":
        raise Refused("NOT_NON_AUTHORITATIVE")
    if document["trading_permission"] is not False:
        raise Refused("TRADING_PERMISSION_NOT_FALSE")
    view: dict = {"verdict": _choice(document["verdict"], frozenset({"PASS", "FAIL"}), "VERDICT_INVALID",
                                     optional=False)}
    plan = document["plan_sha256"]
    if type(plan) is not str or len(plan) != 64 or not _HEX.fullmatch(plan):
        raise Refused("PLAN_DIGEST_INVALID")
    view["plan_sha256"] = plan
    run_id = document.get("run_id")
    if run_id is not None and (type(run_id) is not str or len(run_id) != 32 or not _HEX.fullmatch(run_id)):
        raise Refused("RUN_ID_INVALID")
    view["run_id"] = run_id
    view["stop"] = _choice(document.get("stop"), STOPS, "STOP_INVALID")
    view["phase"] = _choice(document.get("phase"), PHASES, "PHASE_INVALID")
    view["failures"] = [_failure(f) for f in _list(document.get("failures", []), "FAILURES_INVALID")]
    view["cleanup"] = [_choice(c, CLEANUP_CODES, "UNRECOGNIZED_CLEANUP_CODE", optional=False)
                       for c in _list(document.get("cleanup", []), "CLEANUP_INVALID")]
    types = document.get("market_data_type_delivered")
    view["data_types"] = None if types is None else [
        _choice(t, frozenset(m.value for m in MarketDataType), "MARKET_DATA_TYPE_INVALID", optional=False)
        for t in _list(types, "MARKET_DATA_TYPE_INVALID")]
    con_id = document.get("tlt_con_id")
    if con_id is not None and (type(con_id) is not int or not 0 < con_id < 2**31):
        raise Refused("CON_ID_INVALID")
    view["con_id"] = con_id
    view["streams"] = _streams(document.get("streams"))
    view["owner"] = _owner(document.get("managed_accounts"))
    view["timing"] = _timing(document.get("quote_timing"))
    view["lifecycle_local_completion"] = _bool(document.get("lifecycle_local_completion"), "LIFECYCLE_INVALID",
                                               optional=True)
    view["reader_partial_bytes"] = _choice(document.get("reader_partial_bytes"), PARTIAL_BYTES,
                                           "PARTIAL_BYTES_INVALID")
    view["reader_alive"] = _bool(document.get("reader_alive_after_join"), "READER_ALIVE_INVALID", optional=True)
    checks = document.get("checks")
    if checks is not None and (type(checks) is not dict or set(checks) != set(CHECKS)):
        raise Refused("CHECKS_INVALID")
    view["checks"] = None if checks is None else {k: _bool(checks[k], "CHECKS_INVALID") for k in CHECKS}
    # Narrowing only: a PASS is displayed only when the document itself is consistent with one.
    if view["verdict"] == "PASS" and (view["stop"] != "COMPLETE" or view["failures"] or view["checks"] is None
                                      or not all(view["checks"].values())):
        raise Refused("INCONSISTENT_PASS")
    view["suppressed"] = [key for key in SUPPRESSED if key in document]
    return view


def _streams(value: object) -> dict | None:
    if value is None:
        return None
    if type(value) is not dict or not set(value) <= set(STREAMS):
        raise Refused("STREAMS_INVALID")
    out = {}
    for name, status in value.items():
        if type(status) is not dict or set(status) != STREAM_FIELDS or status["stream"] != name:
            raise Refused("STREAMS_INVALID")
        out[name] = {
            "registered": _bool(status["registered"], "STREAMS_INVALID"),
            "completed": _bool(status["completed"], "STREAMS_INVALID"),
            "issue_codes": [_choice(c, STREAM_ISSUES, "UNRECOGNIZED_STREAM_ISSUE", optional=False)
                            for c in _list(status["issue_codes"], "STREAMS_INVALID")],
            "issue_overflow": _count(status["issue_overflow"], "STREAMS_INVALID"),
        }
    return out


def _owner(value: object) -> dict | None:
    if value is None:
        return None
    if type(value) is not dict or set(value) != OWNER_FIELDS or value["authority"] != "NON_AUTHORITATIVE":
        raise Refused("IDENTITY_STATUS_INVALID")
    return {
        "arrival": _choice(value["arrival"], frozenset(a.value for a in Arrival), "IDENTITY_STATUS_INVALID",
                           optional=False),
        "invalidation": _choice(value["invalidation"], frozenset(i.value for i in Invalidation),
                                "IDENTITY_STATUS_INVALID"),
        "invalidating_events": _count(value["invalidating_events"], "IDENTITY_STATUS_INVALID"),
        "registration": _choice(value["registration"], frozenset(r.value for r in Registration),
                                "IDENTITY_STATUS_INVALID", optional=False),
        "closed": _bool(value["closed"], "IDENTITY_STATUS_INVALID"),
        "completed": _bool(value["completed"], "IDENTITY_STATUS_INVALID"),
    }


def _timing(value: object) -> dict | None:
    if value is None:
        return None
    if (type(value) is not dict or set(value) != {"last_trade", "local_receipt_at", "bid_ask_timestamp_freshness"}
            or value["bid_ask_timestamp_freshness"] != "UNKNOWN"):
        raise Refused("QUOTE_TIMING_INVALID")
    last = value["last_trade"]
    if (type(last) is not dict or set(last) != {"source", "timestamp", "age_s", "max_age_s", "within_max_age"}
            or last["source"] != LAST_TRADE_SOURCE):
        raise Refused("QUOTE_TIMING_INVALID")
    return {
        "timestamp": _aware(last["timestamp"], "QUOTE_TIMING_INVALID"),
        "age_s": _number(last["age_s"], "QUOTE_TIMING_INVALID"),
        "max_age_s": _number(last["max_age_s"], "QUOTE_TIMING_INVALID", optional=False),
        "within_max_age": _bool(last["within_max_age"], "QUOTE_TIMING_INVALID", optional=True),
        "received_at": _aware(value["local_receipt_at"], "QUOTE_TIMING_INVALID"),
    }


# ----------------------------------------------------------------------------- rendering


def _flag(value: bool | None) -> str:
    return "UNKNOWN" if value is None else ("yes" if value else "NO")


def _shown(value: object) -> str:
    return "UNKNOWN" if value is None else str(value)


BANNER = (
    "SAVED REPORT VIEW ONLY: NON_AUTHORITATIVE. This page connects to nothing, imports no SDK and has no "
    "trading, order or connection control. trading_permission = False."
)
MEANING = (
    "A reported PASS is evidence somebody reported, not authentication or live verification. The upload's "
    "origin is not independently verified: compare its SHA-256 digest with the reviewed result. Nothing here "
    "is paper readiness, trading authority, protection or broker-side retirement."
)

st.set_page_config(page_title="Paper Connection (saved report)", page_icon="📄", layout="wide")
st.title("Paper Connection (saved report, read-only)")
st.error(BANNER)
st.warning(MEANING)

uploaded = st.file_uploader(
    "Saved diagnostic report (JSON, at most 64 KiB)", type=["json"], accept_multiple_files=False,
    key="paper_connection_report", max_upload_size=1,  # transport limit (whole MB); the 64 KiB cap is below
    help="At most 64 KiB is accepted; the uploader's 1 MB is only its transport limit. Held in memory for this "
    "session only. Nothing is saved, run or connected.",
)
if uploaded is None:
    st.info("NO REPORT: no saved diagnostic report is loaded. Nothing has been run or connected.")
    st.stop()

if uploaded.size > MAX_REPORT_BYTES:  # the declared size, checked before anything is read or parsed
    st.error("REPORT REFUSED: OVERSIZE. Nothing from the file is read or shown; no digest is computed.")
    st.stop()
data = uploaded.read(MAX_REPORT_BYTES + 1)  # never more than the cap plus one byte
if len(data) > MAX_REPORT_BYTES:
    st.error("REPORT REFUSED: OVERSIZE. Nothing from the file is shown; no digest is computed.")
    st.stop()
digest = hashlib.sha256(data).hexdigest()
try:
    view = report_view(load(data))
except Refused as refusal:
    st.error(f"REPORT REFUSED: {refusal}. Nothing from the file is shown.")
    st.markdown(f"**Input SHA-256:** `{digest}` ({len(data)} bytes)")
    st.stop()

st.subheader("Reported result")
m1, m2, m3 = st.columns(3)
m1.metric("Reported verdict", view["verdict"])
m2.metric("Reported stop", _shown(view["stop"]))
m3.metric("Phase at stop", _shown(view["phase"]))
(st.warning if view["verdict"] == "PASS" else st.error)(
    f"Reported {view['verdict']}: shown exactly as reported, never recalculated or upgraded. "
    + ("PASS is reported evidence only." if view["verdict"] == "PASS" else "Nothing here passed.")
)
st.markdown(
    f"**Input SHA-256:** `{digest}` ({len(data)} bytes) · **plan_sha256:** `{view['plan_sha256']}` · "
    f"**run_id:** `{_shown(view['run_id'])}` · authority **NON_AUTHORITATIVE** · "
    "**trading_permission = False** · origin **not independently verified**"
)

st.subheader("Identity match (keyed comparison, never a raw account or key)")
owner = view["owner"]
rows = view["checks"]["rows_match_expected_account_reference"] if view["checks"] else None
if owner is None:
    st.warning("Identity status: UNKNOWN (not in this report).")
else:
    st.markdown("\n".join([
        "| Arrival | Registration | Invalidation | Invalidating events | Closed | Rows match expected reference |",
        "|---|---|---|---|---|---|",
        (f"| {owner['arrival']} | {owner['registration']} | {owner['invalidation'] or 'none'} | "
         f"{owner['invalidating_events']} | {_flag(owner['closed'])} | {_flag(rows)} |"),
    ]))
st.caption("A keyed reference comparison, not authentication. No account identifier or key is displayed.")

st.subheader("TLT and read streams")
streams = view["streams"]
if streams is None:
    st.warning("Stream statuses: UNKNOWN (not in this report).")
else:
    lines = ["| Stream | Registered | Completed | Issue codes | Issue overflow |", "|---|---|---|---|---|"]
    for name in STREAMS:
        status = streams.get(name)
        if status is None:
            lines.append(f"| {name} | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN |")
            continue
        codes = ", ".join(f"`{c}`" for c in status["issue_codes"]) or "none"
        lines.append(f"| {name} | {_flag(status['registered'])} | {_flag(status['completed'])} | {codes} | "
                     f"{status['issue_overflow']} |")
    st.markdown("\n".join(lines))
st.markdown(f"**TLT conId (reported):** {_shown(view['con_id'])} · **Market data types delivered:** "
            + (", ".join(f"`{t}`" for t in view["data_types"]) if view["data_types"] else "UNKNOWN"))

st.subheader("Quote timing")
timing = view["timing"]
if timing is None:
    st.warning("Quote timing: UNKNOWN (not in this report).")
else:
    st.markdown("\n".join([
        "| Last-trade timestamp | Last-trade age (s) | Max age (s) | Within max age | Local receipt time |",
        "|---|---|---|---|---|",
        (f"| {_shown(timing['timestamp'] and timing['timestamp'].isoformat())} | {_shown(timing['age_s'])} | "
         f"{timing['max_age_s']} | {_flag(timing['within_max_age'])} | "
         f"{_shown(timing['received_at'] and timing['received_at'].isoformat())} |"),
    ]))
    st.caption(f"Last-trade source: {LAST_TRADE_SOURCE}.")
st.markdown("**Bid/ask timestamp freshness: UNKNOWN.** Nothing dates the bid or ask; market data type is not "
            "freshness.")

st.subheader("Checks, failures and cleanup (as reported)")
if view["checks"] is None:
    st.warning("Checks: UNKNOWN (not in this report).")
else:
    st.markdown("\n".join(["| Check | Reported |", "|---|---|"]
                          + [f"| `{name}` | {_flag(view['checks'][name])} |" for name in CHECKS]))
st.markdown("**Run failures:** " + (", ".join(f"`{f}`" for f in view["failures"]) or "none reported"))
st.markdown("**Cleanup actions:** " + (", ".join(f"`{c}`" for c in view["cleanup"]) or "none reported"))

st.subheader("Stated unknowns")
st.markdown("\n".join([
    f"- **Reader partial bytes:** {_shown(view['reader_partial_bytes'])} (the stock reader's buffer is not observable)",
    f"- **Local lifecycle completion:** {_flag(view['lifecycle_local_completion'])}",
    f"- **Reader alive after join:** {_flag(view['reader_alive'])}",
    "- **Broker-side retirement:** NOT ESTABLISHED (disconnection is local only)",
    "- **trading_permission:** False",
]))
if view["suppressed"]:
    st.caption("Recognised sections not displayed (they can carry payloads): "
               + ", ".join(f"`{name}`" for name in view["suppressed"]) + ".")

st.divider()
st.caption(
    "This page has no control that connects, orders, cancels, starts a run or changes settings. Its only input "
    "is the one upload, held in memory and never saved."
)
