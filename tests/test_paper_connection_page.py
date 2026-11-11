"""The Paper Connection page shows one saved diagnostic report honestly. Invented reports only.

Drives ``dashboard/pages/7_Paper_Connection.py`` headlessly through ``streamlit.testing.v1.AppTest``
with invented documents in the diagnostic's accepted output shape. Nothing here is a real result: the
"PASS" fixture is invented to prove the page renders a reported PASS as reported evidence only. Canaries
in the payload-bearing sections (raw-looking accounts, balances, finding text) must never be shown.
"""
from __future__ import annotations

import ast
import copy
import hashlib
import http.client
import json
import socket
import sys
import urllib.request
from pathlib import Path

import pytest
import streamlit
from streamlit.testing.v1 import AppTest

import tradingbot

REPO = Path(__file__).resolve().parents[1]
PAGE = REPO / "dashboard" / "pages" / "7_Paper_Connection.py"
DOCS = REPO / "docs" / "PAPER_CONNECTION_VIEW.md"
CHECKS = ("stop_complete", "no_run_failures", "streams_complete_without_issues", "owner_valid",
          "rows_match_expected_account_reference", "contract_accepted_before_quote", "assessments_all_info",
          "router_faults_none", "adapter_failures_documented_only", "read_stops_returned", "disconnect_returned",
          "socket_and_consumer_stopped", "ledger_faults_none", "reader_not_alive_after_join", "no_forbidden_calls")
# Canaries that sit only in sections the page must never display.
CANARIES = ("SYNTH-CANARY-ACCT", "987654.32", "CANARY-FINDING-TEXT", "CANARY-ATTESTED", "acctref-v1:canary",
            "CANARY-ROUTER")
SOURCE = "tick 45 last-trade timestamp, as mapped by the unchanged normalizer"
INPUT_WIDGETS = ("text_input", "number_input", "text_area", "selectbox", "multiselect", "checkbox", "toggle",
                 "slider", "select_slider", "radio", "button", "download_button", "date_input", "time_input",
                 "chat_input", "color_picker", "camera_input", "data_editor")


def stream(name: str, *, completed: bool = True, codes: tuple = ()) -> dict:
    return {"stream": name, "registered": True, "completed": completed, "issue_codes": list(codes),
            "issue_overflow": 0}


def invented_pass() -> dict:
    """An INVENTED document in the accepted result shape, reporting PASS. Not a real result."""
    return {
        "authority": "NON_AUTHORITATIVE", "trading_permission": False, "run_id": "0123456789abcdef" * 2,
        "plan_sha256": "ab" * 32, "endpoint": {"host": "127.0.0.1", "port": 7497, "client_id": 6601},
        "stop": "COMPLETE", "phase": "READS", "failures": [], "cleanup": [],
        "marks_s": {"contract_accepted": 1.25}, "server_version": 999,
        "requests_sent": ["reqAccountSummary", "reqPositions", "reqContractDetails", "reqMktData"],
        "callbacks": {"nextValidId": 1}, "market_data_type_delivered": ["LIVE"],
        "quote_timing": {"last_trade": {"source": SOURCE, "timestamp": "2026-03-04T15:59:52+00:00", "age_s": 8.0,
                                        "max_age_s": 30.0, "within_max_age": True},
                         "local_receipt_at": "2026-03-04T15:59:59.500000+00:00",
                         "bid_ask_timestamp_freshness": "UNKNOWN"},
        "tlt_con_id": 900001,
        "streams": {k: stream(k) for k in ("CONTRACT_DETAILS", "QUOTE_SNAPSHOT", "ACCOUNT_SUMMARY", "POSITIONS")},
        "findings": {"quote": [{"code": "QUOTE_LIVE_CONTROL", "detail": "CANARY-FINDING-TEXT"}]},
        "envelopes": {"account_summary": [{"account_summary": {"account_id": "acctref-v1:canary",
                                                               "values": {"NetLiquidation": "987654.32"}}}],
                      "positions": [{"raw": "SYNTH-CANARY-ACCT"}]},
        "router_report": {"faults": [], "note": "CANARY-ROUTER"},
        "managed_accounts": {"arrival": "EXACT", "invalidation": None, "invalidating_events": 0,
                             "registration": "REGISTERED", "closed": True, "router_completed": True,
                             "completed": True, "authority": "NON_AUTHORITATIVE"},
        "lifecycle_local_completion": False, "reader_partial_bytes": "UNKNOWN", "reader_alive_after_join": False,
        "checks": dict.fromkeys(CHECKS, True), "verdict": "PASS",
        "limits": ["local disconnection only; no broker-side retirement proof"],
        "operator_attestation": {"attested_at_utc": "CANARY-ATTESTED", "age_s_at_start": 12.0,
                                 "nature": "operator claim, not SDK proof"},
    }


def invented_failure() -> dict:
    """An INVENTED failed run: contract timeout, raised join, unknown timing, partial streams."""
    doc = invented_pass()
    doc.update(
        stop="CONTRACT_TIMEOUT", phase="CONTRACT", verdict="FAIL",
        failures=["READER_JOIN_RAISED:TimeoutError", "RUN_RAISED:NON_BUILTIN_EXCEPTION"],
        cleanup=["INDEPENDENT_DISCONNECT"], tlt_con_id=None, market_data_type_delivered=[],
        reader_partial_bytes="UNRESOLVED", reader_alive_after_join=True, lifecycle_local_completion=None,
    )
    doc["quote_timing"] = {"last_trade": {"source": SOURCE, "timestamp": None, "age_s": None, "max_age_s": 30.0,
                                          "within_max_age": None},
                           "local_receipt_at": None, "bid_ask_timestamp_freshness": "UNKNOWN"}
    doc["streams"] = {"CONTRACT_DETAILS": stream("CONTRACT_DETAILS", completed=False),
                      "ACCOUNT_SUMMARY": stream("ACCOUNT_SUMMARY", codes=("MALFORMED_CALLBACK",))}
    doc["checks"] = {**dict.fromkeys(CHECKS, False), "no_forbidden_calls": True}
    return doc


def encode(doc: object) -> bytes:
    return json.dumps(doc, indent=1, sort_keys=True).encode()


def page() -> AppTest:
    return AppTest.from_file(str(PAGE), default_timeout=60)


# Driving an upload needs AppTest file-uploader support; without it these cases are skipped, never faked.
CAN_UPLOAD = hasattr(AppTest, "file_uploader")


def upload(data: bytes, name: str = "result.json") -> AppTest:
    if not CAN_UPLOAD:
        pytest.skip("this Streamlit's AppTest cannot drive st.file_uploader")
    at = page().run()
    at.file_uploader[0].set_value((name, data, "application/json"))
    return at.run()


def shown(at: AppTest) -> str:
    parts: list[str] = []
    for kind in ("title", "subheader", "error", "warning", "info", "markdown", "caption", "code"):
        parts += [str(e.value) for e in at.get(kind)]
    parts += [f"{m.label} {m.value}" for m in at.metric]
    return "\n".join(parts)


def metrics(at: AppTest) -> dict[str, str]:
    return {m.label: str(m.value) for m in at.metric}


# ----------------------------------------------------------------------------- the page's surface


def test_page_imports_only_streamlit_the_stdlib_and_sdk_free_enums():
    assert Path(tradingbot.__file__).resolve().parents[2] == REPO and PAGE.is_file()
    source = PAGE.read_text(encoding="utf-8")
    tree = ast.parse(source)
    modules = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    modules |= {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
    assert modules == {"__future__", "builtins", "hashlib", "inspect", "json", "math", "re", "datetime", "streamlit",
                       "tradingbot.paper_readiness.account_position_callbacks",
                       "tradingbot.paper_readiness.callback_router", "tradingbot.paper_readiness.ibkr_callbacks",
                       "tradingbot.paper_readiness.managed_accounts_input",
                       "tradingbot.paper_readiness.observations"}, modules
    for token in ("ibapi", "paper_read_diagnostic", "sdk_read_wrapper", "socket.", "subprocess", "urllib",
                  "import requests", "open(", "os.", "environ", "eval(", "exec(", "pickle", "st.json", "st.write",
                  "st.dataframe", "st.code", "query_params"):
        assert token not in source, token


def test_default_is_no_report_and_the_only_input_is_one_upload():
    at = page().run()
    assert not at.exception
    if CAN_UPLOAD:
        assert [u.label for u in at.file_uploader] == ["Saved diagnostic report (JSON, at most 64 KiB)"]
        assert not at.file_uploader[0].accept_multiple_files and at.file_uploader[0].allowed_type == [".json"]
    for widget in INPUT_WIDGETS:
        assert not list(getattr(at, widget, [])), widget
    text = shown(at)
    assert "NO REPORT" in text and not at.metric
    assert "NON_AUTHORITATIVE" in text and "trading_permission = False" in text
    assert "PASS" not in [m.value for m in at.metric]


def test_the_page_opens_with_an_uploader_that_has_no_max_upload_size_parameter(monkeypatch):
    """Review P2: pyproject allows streamlit>=1.36, whose st.file_uploader has no max_upload_size. With that
    older signature (simulated here exactly; passing the argument raises TypeError) the page must still open on
    NO REPORT, and its own 64 KiB cap still applies."""
    real = streamlit.file_uploader

    def uploader_1_36(label, type=None, accept_multiple_files=False, key=None, help=None, on_change=None,
                      args=None, kwargs=None, *, disabled=False, label_visibility="visible"):
        return real(label, type=type, accept_multiple_files=accept_multiple_files, key=key, help=help,
                    on_change=on_change, args=args, kwargs=kwargs, disabled=disabled,
                    label_visibility=label_visibility)

    monkeypatch.setattr(streamlit, "file_uploader", uploader_1_36)
    at = page().run()
    assert not at.exception
    assert "NO REPORT" in shown(at) and not at.metric
    if CAN_UPLOAD:
        assert len(at.file_uploader) == 1
        data = encode(invented_pass())
        oversize = data + b" " * (64 * 1024 + 1 - len(data))
        at.file_uploader[0].set_value(("result.json", oversize, "application/json"))
        at.run()
        assert not at.exception
        assert [e.value for e in at.error if e.value.startswith("REPORT REFUSED")] == [
            "REPORT REFUSED: OVERSIZE. Nothing from the file is read or shown; no digest is computed."]


# ----------------------------------------------------------------------------- reported PASS, as reported


def test_an_invented_reported_pass_renders_only_fixed_fields_as_reported_evidence():
    data = encode(invented_pass())
    at = upload(data)
    assert not at.exception
    assert metrics(at) == {"Reported verdict": "PASS", "Reported stop": "COMPLETE", "Phase at stop": "READS"}
    text = shown(at)
    assert f"**Input SHA-256:** `{hashlib.sha256(data).hexdigest()}` ({len(data)} bytes)" in text
    for phrase in ("PASS is reported evidence only", "not authentication or live verification",
                   "origin is not independently verified", "origin **not independently verified**",
                   "**trading_permission = False**", "never recalculated or upgraded"):
        assert phrase in text, phrase
    # Identity: fixed statuses only; streams; timing; unknowns.
    assert "| EXACT | REGISTERED | none | 0 | yes | yes |" in text
    for name in ("CONTRACT_DETAILS", "QUOTE_SNAPSHOT", "ACCOUNT_SUMMARY", "POSITIONS"):
        assert f"| {name} | yes | yes | none | 0 |" in text, name
    assert "**TLT conId (reported):** 900001" in text and "`LIVE`" in text
    assert "| 2026-03-04T15:59:52+00:00 | 8.0 | 30.0 | yes | 2026-03-04T15:59:59.500000+00:00 |" in text
    assert "**Bid/ask timestamp freshness: UNKNOWN.**" in text
    assert "**Reader partial bytes:** UNKNOWN" in text and "**Local lifecycle completion:** NO" in text
    assert "**Broker-side retirement:** NOT ESTABLISHED" in text
    assert all(f"| `{name}` | yes |" in text for name in CHECKS)
    # Payload-bearing sections are recognised and never displayed.
    for canary in CANARIES:
        assert canary not in text, canary
    assert "`envelopes`" in text and "`findings`" in text and "`operator_attestation`" in text
    for claim in ("READY", "AUTHORIZED", "APPROVED", "PROTECTED", "✅"):
        assert claim not in text, claim
    assert not at.success


def test_an_invented_failure_shows_fail_with_codes_and_unknowns_never_pass():
    at = upload(encode(invented_failure()))
    assert not at.exception
    assert metrics(at) == {"Reported verdict": "FAIL", "Reported stop": "CONTRACT_TIMEOUT",
                           "Phase at stop": "CONTRACT"}
    text = shown(at)
    assert [e.value for e in at.error if e.value.startswith("Reported FAIL")]
    assert "`READER_JOIN_RAISED:TimeoutError`, `RUN_RAISED:NON_BUILTIN_EXCEPTION`" in text
    assert "**Cleanup actions:** `INDEPENDENT_DISCONNECT`" in text
    assert "| CONTRACT_DETAILS | yes | NO | none | 0 |" in text
    assert "| QUOTE_SNAPSHOT | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN |" in text  # absent, never zero
    assert "| ACCOUNT_SUMMARY | yes | yes | `MALFORMED_CALLBACK` | 0 |" in text
    assert "| UNKNOWN | UNKNOWN | 30.0 | UNKNOWN | UNKNOWN |" in text
    assert "**TLT conId (reported):** UNKNOWN" in text and "Market data types delivered:** UNKNOWN" in text
    assert "**Reader partial bytes:** UNRESOLVED" in text and "**Reader alive after join:** yes" in text
    assert "**Local lifecycle completion:** UNKNOWN" in text
    assert "PASS is reported evidence only" not in text
    for canary in CANARIES:
        assert canary not in text, canary


def test_the_minimal_result_raised_document_renders_with_unknown_sections():
    doc = {"authority": "NON_AUTHORITATIVE", "trading_permission": False, "run_id": None, "plan_sha256": "cd" * 32,
           "stop": None, "failures": ["RESULT_RAISED:KeyError"], "verdict": "FAIL"}
    at = upload(encode(doc))
    assert not at.exception
    assert metrics(at) == {"Reported verdict": "FAIL", "Reported stop": "UNKNOWN", "Phase at stop": "UNKNOWN"}
    text = shown(at)
    for unknown in ("Identity status: UNKNOWN", "Stream statuses: UNKNOWN", "Quote timing: UNKNOWN",
                    "Checks: UNKNOWN"):
        assert unknown in text, unknown
    assert "`RESULT_RAISED:KeyError`" in text and "Recognised sections not displayed" not in text


# ----------------------------------------------------------------------------- refusals, never echoed


def _mutate(change) -> bytes:
    doc = invented_pass()
    change(doc)
    return encode(doc)


REFUSALS = {
    "not-utf8": (b"\xff\xfe{}", "NOT_UTF8"),
    "malformed": (b'{"verdict": "PASS",', "MALFORMED_JSON"),
    "top-level-list": (b"[1, 2, 3]", "NOT_AN_OBJECT"),
    "duplicate-key": (b'{"verdict": "PASS", "verdict": "FAIL"}', "DUPLICATE_KEY"),
    "non-finite": (b'{"verdict": NaN}', "NON_FINITE_NUMBER"),
    "too-deep": (b"[" * 40 + b"]" * 40, "TOO_DEEP"),
    "unknown-field": (_mutate(lambda d: d.update(CANARY_FIELD_PAYLOAD="CANARY-UNKNOWN-TEXT")),
                      "UNRECOGNIZED_FIELD"),
    "missing-required": (_mutate(lambda d: d.pop("plan_sha256")), "REQUIRED_FIELD_MISSING"),
    "authoritative": (_mutate(lambda d: d.update(authority="AUTHORITATIVE")), "NOT_NON_AUTHORITATIVE"),
    "trading-permission": (_mutate(lambda d: d.update(trading_permission=True)), "TRADING_PERMISSION_NOT_FALSE"),
    "verdict-text": (_mutate(lambda d: d.update(verdict="READY CANARY-VERDICT")), "VERDICT_INVALID"),
    "failure-code": (_mutate(lambda d: d.update(verdict="FAIL", failures=["CANARY_FAILURE_PAYLOAD"])),
                     "UNRECOGNIZED_FAILURE_CODE"),
    "failure-suffix": (_mutate(lambda d: d.update(verdict="FAIL", failures=["RUN_RAISED:CanaryPayloadText"])),
                       "UNRECOGNIZED_FAILURE_CODE"),
    "stream-issue": (_mutate(lambda d: d["streams"]["POSITIONS"].update(issue_codes=["CANARY_ISSUE"])),
                     "UNRECOGNIZED_STREAM_ISSUE"),
    "stream-extra-field": (_mutate(lambda d: d["streams"]["POSITIONS"].update(detail="CANARY-STREAM-TEXT")),
                           "STREAMS_INVALID"),
    "identity-raw-account": (_mutate(lambda d: d["managed_accounts"].update(account="SYNTH-CANARY-ACCT")),
                             "IDENTITY_STATUS_INVALID"),
    "bid-ask-claimed-fresh": (_mutate(lambda d: d["quote_timing"].update(bid_ask_timestamp_freshness="FRESH")),
                              "QUOTE_TIMING_INVALID"),
    "source-text": (_mutate(lambda d: d["quote_timing"]["last_trade"].update(source="CANARY-SOURCE-TEXT")),
                    "QUOTE_TIMING_INVALID"),
    "naive-timestamp": (_mutate(lambda d: d["quote_timing"]["last_trade"].update(timestamp="2026-03-04T15:59:52")),
                        "QUOTE_TIMING_INVALID"),
    "bool-con-id": (_mutate(lambda d: d.update(tlt_con_id=True)), "CON_ID_INVALID"),
    "pass-with-false-check": (_mutate(lambda d: d["checks"].update(owner_valid=False)), "INCONSISTENT_PASS"),
    "pass-not-complete": (_mutate(lambda d: d.update(stop="READS_TIMEOUT")), "INCONSISTENT_PASS"),
    "pass-with-failure": (_mutate(lambda d: d.update(failures=["SHUTDOWN_RAISED"])), "INCONSISTENT_PASS"),
    "pass-without-checks": (_mutate(lambda d: d.pop("checks")), "INCONSISTENT_PASS"),
    "missing-check": (_mutate(lambda d: d["checks"].pop("owner_valid")), "CHECKS_INVALID"),
}


@pytest.mark.parametrize("case", list(REFUSALS))
def test_every_unrecognised_or_malformed_report_is_refused_whole_without_echo(case):
    data, code = REFUSALS[case]
    at = upload(data)
    assert not at.exception
    refusals = [e.value for e in at.error if e.value.startswith("REPORT REFUSED")]
    assert refusals == [f"REPORT REFUSED: {code}. Nothing from the file is shown."]
    assert not at.metric  # no verdict is displayed
    text = shown(at)
    assert f"`{hashlib.sha256(data).hexdigest()}`" in text  # digest for comparison, never content
    for marker in ("CANARY", "SYNTH-", "987654", "READY"):
        assert marker not in text.replace("REPORT REFUSED", ""), marker


@pytest.mark.parametrize("field", ["age_s", "max_age_s"])
@pytest.mark.parametrize("sign", ["", "-"], ids=["positive", "negative"])
def test_a_huge_json_integer_is_a_fixed_refusal_not_a_crash(field, sign):
    """Review P2: an integer too large for a float must be refused before any float conversion."""
    text = encode(invented_pass()).decode()
    stamp = '"age_s": 8.0' if field == "age_s" else '"max_age_s": 30.0'
    assert text.count(stamp) == 1
    data = text.replace(stamp, f'"{field}": {sign}1{"0" * 400}').encode()
    at = upload(data)
    assert not at.exception
    assert [e.value for e in at.error if e.value.startswith("REPORT REFUSED")] == [
        "REPORT REFUSED: QUOTE_TIMING_INVALID. Nothing from the file is shown."]
    assert not at.metric  # no verdict is displayed


def test_an_oversize_upload_is_refused_before_parsing_and_never_digested():
    data = encode(invented_pass())
    data += b" " * (64 * 1024 + 1 - len(data))
    at = upload(data)
    assert not at.exception
    assert [e.value for e in at.error if e.value.startswith("REPORT REFUSED")] == [
        "REPORT REFUSED: OVERSIZE. Nothing from the file is read or shown; no digest is computed."]
    assert hashlib.sha256(data).hexdigest() not in shown(at) and not at.metric


def test_a_report_at_the_cap_is_accepted():
    data = encode(invented_pass())
    data += b" " * (64 * 1024 - len(data))  # trailing whitespace is valid JSON
    assert len(data) == 64 * 1024
    assert metrics(upload(data))["Reported verdict"] == "PASS"


# ----------------------------------------------------------------------------- nothing runs or connects


def test_viewing_neither_connects_imports_an_sdk_nor_writes(tmp_path, monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError("the page attempted a network connection")

    for target, name in ((socket, "create_connection"), (socket, "getaddrinfo"), (socket.socket, "connect"),
                         (socket.socket, "connect_ex"), (urllib.request, "urlopen"),
                         (http.client.HTTPConnection, "connect")):
        monkeypatch.setattr(target, name, deny)
    monkeypatch.chdir(tmp_path)
    before = set(tmp_path.rglob("*"))
    for doc in (invented_pass(), invented_failure()):
        at = upload(encode(doc))
        assert not at.exception
    assert not any(name == "ibapi" or name.startswith("ibapi.") for name in sys.modules)
    assert not any("paper_read_diagnostic" in name for name in sys.modules)
    assert set(tmp_path.rglob("*")) == before


def test_the_invented_fixtures_are_not_mutated_by_viewing():
    doc = invented_pass()
    frozen = copy.deepcopy(doc)
    upload(encode(doc))
    assert doc == frozen


def test_view_docs_give_run_instructions_and_limits():
    text = DOCS.read_text(encoding="utf-8")
    for token in ("PYTHONPATH=src streamlit run dashboard/pages/7_Paper_Connection.py", "NO REPORT", "64 KiB",
                  "SHA-256", "not independently verified", "NON_AUTHORITATIVE", "trading_permission",
                  "UNKNOWN", "## Limits", "invented"):
        assert token in text, token
