"""Callback Demo: an OFFLINE SIMULATION view of the merged callback demonstration.

NON-AUTHORITATIVE. Shows what ``tradingbot.paper_readiness.callback_demo`` produces for its three
invented scenarios: the expected and actual outcome, the callback ingestion state, the request
sequence, the failure codes by stage, and the canonical redacted report through the existing
renderers.

The contract and quote envelopes come from invented callback values. The account summary and
positions are preassembled invented control envelopes, not callbacks, so nothing here is callback,
SDK, broker or paper-account proof.

The only control is the choice among the three bundled scenarios. The page reads no file, path,
environment variable, wall clock or network, writes nothing and builds no transport. Everything
shown comes from ``DemoResult`` and its ``ReadinessReport``; no input account or value record is
displayed. A ``CONTROL_COMPLETE`` outcome grants no trading permission
(tests/test_callback_demo_page.py verifies).
"""

from __future__ import annotations

import streamlit as st

from tradingbot.paper_readiness.callback_demo import (
    PREASSEMBLED_STREAMS,
    SCENARIOS,
    SIMULATION_NOTICE,
    DemoResult,
    render_demo_json,
    run_demo,
)
from tradingbot.paper_readiness.diagnostics import FindingKind, ReadinessOutcome, Stage
from tradingbot.paper_readiness.runner import render_text
from tradingbot.paper_readiness.transport import READ_METHODS

BANNER = (
    "SIMULATION ONLY: invented callback values replayed offline through the FAKE transport under a "
    "FAKE clock. No trading authority, trading_permission = False. No broker, SDK, network, "
    "credential, account or order path exists on this page."
)
PREASSEMBLED_NOTE = (
    "The account summary and positions are PREASSEMBLED invented control envelopes, not produced "
    "from callbacks. They are not callback, SDK, broker or paper-account proof."
)
OUTCOME_MEANING = {
    ReadinessOutcome.CONTROL_COMPLETE: (
        "this invented observation set was complete and consistent. That is all it means: it is "
        "not paper readiness and it never authorizes trading. trading_permission = False."
    ),
    ReadinessOutcome.NOT_READY: (
        "at least one finding is not INFO, so the invented observation set is missing, ambiguous, "
        "mismatched, stale, delayed, unentitled, incomplete or erroneous. "
        "trading_permission = False."
    ),
    ReadinessOutcome.REFUSED_BEFORE_DISPATCH: (
        "preflight refused the invented session attestation, so nothing was read. "
        "trading_permission = False."
    ),
}
CALLBACK_SOURCE = "invented callbacks, via IbkrCallbackNormalizer"
PREASSEMBLED_SOURCE = "PREASSEMBLED invented control envelopes, not callbacks"


def _cell(text: str) -> str:
    return text.replace("|", "\\|")


def _source(stream: str) -> str:
    return PREASSEMBLED_SOURCE if stream in PREASSEMBLED_STREAMS else CALLBACK_SOURCE


def _ingestion_rows(result: DemoResult) -> list[str]:
    rows = [
        "| Stream | Envelope source | Callbacks completed | Ingestion issues |",
        "|---|---|---|---|",
    ]
    for stream, completed, issues in (
        ("contract_details", result.contract_completed, result.contract_issues),
        ("quote_snapshot", result.quote_completed, result.quote_issues),
    ):
        listed = ", ".join(f"`{code}`" for code in issues) or "none"
        done = "yes" if completed else "NO"
        rows.append(f"| {stream} | {_source(stream)} | {done} | {listed} |")
    for stream in PREASSEMBLED_STREAMS:
        rows.append(f"| {stream} | {_source(stream)} | not applicable | not applicable |")
    return rows


def _sequence_rows(result: DemoResult) -> list[str]:
    """Reads in dispatch order. The runner records each read's digest together with its dispatch."""
    report = result.report
    rows = [
        "| # | Read dispatched | Request id | Stage | Rows kept | END seen | Envelope source |",
        "|---|---|---|---|---|---|---|",
    ]
    pairs = zip(report.dispatched, report.observations)
    for number, (method, digest) in enumerate(pairs, start=1):
        end = "yes" if digest.end_seen else "NO"
        source = _source(method.removeprefix("request_"))
        rows.append(f"| {number} | `{method}` | {digest.request_id} | {digest.stage.value} | "
                    f"{digest.rows} | {end} | {source} |")
    return rows


def _failure_rows(result: DemoResult) -> list[str]:
    rows = ["| Stage | Kind | Code | Request id | Detail |", "|---|---|---|---|---|"]
    for stage in Stage:
        for f in result.report.findings:
            if f.stage == stage and f.kind != FindingKind.INFO:
                request = "—" if f.request_id is None else str(f.request_id)
                rows.append(f"| {stage.value} | `{f.kind.value}` | **{f.code}** | {request} | "
                            f"{_cell(f.detail)} |")
    return rows


st.set_page_config(page_title="Callback Demo (simulation)", page_icon="🧪", layout="wide")
st.title("Callback Demo (OFFLINE SIMULATION)")
st.error(BANNER)
st.warning(PREASSEMBLED_NOTE)
st.caption(SIMULATION_NOTICE)

with st.sidebar:
    st.error("SIMULATION ONLY · trading_permission = False")
    st.header("Invented scenario")
    choice = st.radio(
        "Scenario",
        list(SCENARIOS),
        key="callback_demo_scenario",
        format_func=lambda name: f"{name} · expected {SCENARIOS[name].expected_outcome.value}",
    )
    st.caption(
        f"{len(SCENARIOS)} fixed invented scenarios from tradingbot.paper_readiness.callback_demo. "
        "Nothing can be typed, uploaded, pointed at a path or connected from this page."
    )

try:
    result: DemoResult = run_demo(choice)
except Exception as exc:  # noqa: BLE001 - never a traceback: a message could carry a payload
    st.error(
        f"Simulation halted safely: {type(exc).__name__} while running `{choice}`. No result is "
        "shown; nothing was connected, written or authorized."
    )
    st.stop()

report = result.report
non_info = sum(n for kind, n in report.counts.items() if kind != FindingKind.INFO.value)

st.subheader("Expected versus actual")
st.markdown(f"**`{result.scenario.name}`** — {result.scenario.description}")
m1, m2, m3, m4 = st.columns(4)
m1.metric("Expected outcome", result.scenario.expected_outcome.value)
m2.metric("Actual outcome", report.outcome.value)
m3.metric("Matches expectation", "yes" if result.matches_expectation else "NO")
m4.metric("Non-INFO findings", non_info)
(st.warning if report.outcome == ReadinessOutcome.CONTROL_COMPLETE else st.error)(
    f"{report.outcome.value}: {OUTCOME_MEANING[report.outcome]}"
)
if not result.matches_expectation:
    st.error("UNEXPECTED: the actual outcome differs from the scenario's expected outcome.")
st.markdown(
    f"authority **{report.authority}** · **trading_permission = {report.trading_permission}** · "
    f"account_ref `{report.account_ref}` (redacted) · target `{report.target_symbol}` · "
    f"as_of {report.as_of.isoformat()} (**FAKE clock**)"
)

st.subheader("Callback ingestion")
st.markdown("\n".join(_ingestion_rows(result)))

st.subheader("Request sequence")
st.markdown("\n".join(_sequence_rows(result)))
skipped = [method for method in READ_METHODS if method not in report.dispatched]
if skipped:
    st.warning(
        "Not dispatched in this scenario (unknown, not passed): "
        + ", ".join(f"`{method}`" for method in skipped)
        + "."
    )

st.subheader("Failure codes by stage")
if non_info:
    st.markdown("\n".join(_failure_rows(result)))
else:
    st.info(
        "No non-INFO finding. This is still an invented simulation control, not paper readiness "
        "or trading authority."
    )
with st.expander("All findings, INFO included"):
    st.markdown(
        "\n".join(
            f"- `{f.stage.value}` `{f.kind.value}` **{f.code}**: {f.detail}"
            for f in report.findings
        )
        or "No finding."
    )

st.subheader("Report")
with st.expander("Report text (existing readiness renderer)"):
    st.code(render_text(report), language="text")
with st.expander("Report JSON (existing demo renderer)"):
    st.code(render_demo_json([result]), language="json")
st.caption(
    "Both views come from the existing redacted renderers. The report stays NON_AUTHORITATIVE with "
    "trading_permission false, and it carries no input account or value record."
)

st.divider()
st.caption(
    "This page has no control that connects, orders, cancels, replaces, resets, changes settings "
    "or handles credentials, and it accepts no free-form input. Its only input is the choice among "
    "the three fixed scenarios. The deterministic gateway (validator.py) still decides every "
    "action."
)
