"""Paper Readiness: an OFFLINE, FAKE-ONLY operator view of the readiness scaffold (OPS-03, issue #22).

Shows the redacted NON-AUTHORITATIVE report of each bundled synthetic scenario through the merged
``tradingbot.paper_readiness`` runner, so an operator can read a synthetic control, a preflight refusal, an
ambiguous identity, stale or delayed data and the still-unknown prerequisites without the CLI.

The only controls are the choice of bundled scenario and a deterministic re-run. The page reads no file,
path, environment variable, wall clock or network, writes nothing, builds no transport of its own and never
shows a scenario script, an observation payload, an account identifier, a balance or a price: everything
displayed comes from ``ReadinessReport`` (tests/test_paper_readiness_page.py verifies all of this).
Nothing here is broker readiness, trading authorization or protection; the deterministic gateway
(``validator.py``) is unchanged and still decides every action.
"""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path

import streamlit as st

import tradingbot
from tradingbot.paper_readiness.diagnostics import (
    HEADLINE_ORDER,
    Finding,
    FindingKind,
    ReadinessOutcome,
    ReadinessReport,
    Stage,
)
from tradingbot.paper_readiness.runner import render_json
from tradingbot.paper_readiness.scenarios import SCENARIOS, run_scenario

BANNER = (
    "OFFLINE / FAKE ONLY — synthetic scenarios replayed through the scaffold's FAKE transport under a FAKE "
    "clock. Connected capability is UNKNOWN. trading_permission = False. No broker, network, credential, "
    "account or order path exists on this page."
)
SIDEBAR_BANNER = "OFFLINE / FAKE ONLY — connected capability unknown — trading_permission = False"
CONTROL_MEANING = (
    "CONTROL_COMPLETE means only that a synthetic observation set was complete and consistent. It is never "
    "real broker readiness, trading authorization or guaranteed protection."
)
OUTCOME_MEANING = {
    ReadinessOutcome.REFUSED_BEFORE_DISPATCH: (
        "Preflight refused the synthetic session attestation, so no read was dispatched and nothing was "
        "observed. Every connected capability is unknown; trading_permission = False."
    ),
    ReadinessOutcome.NOT_READY: (
        "At least one finding is not INFO: the synthetic observation set is missing, ambiguous, mismatched, "
        "stale, delayed, unentitled, incomplete or erroneous. Connected capability is unknown; "
        "trading_permission = False."
    ),
    ReadinessOutcome.CONTROL_COMPLETE: (
        CONTROL_MEANING + " Connected capability remains unknown; trading_permission = False."
    ),
}
KIND_MEANING = {
    FindingKind.REFUSED: "preflight refusal; nothing was dispatched",
    FindingKind.ERROR: "broker error or unclassified message",
    FindingKind.INCOMPLETE: "stream ended early, disconnected or truncated",
    FindingKind.MISMATCH: "observation contradicts the expectation or itself",
    FindingKind.AMBIGUOUS: "observations admit more than one reading",
    FindingKind.UNENTITLED: "market data entitlement absent or partial",
    FindingKind.DELAYED: "delayed-feed evidence",
    FindingKind.STALE: "too old or frozen to describe the current market",
    FindingKind.OUT_OF_SCOPE: "outside the approved regular-session / tradable scope",
    FindingKind.MISSING: "required observation or field absent",
    FindingKind.INFO: "informational control; not permission",
}
GAP_KINDS = (
    FindingKind.MISSING,
    FindingKind.INCOMPLETE,
    FindingKind.AMBIGUOUS,
    FindingKind.UNENTITLED,
)
READ_STAGES = (Stage.CONTRACT, Stage.QUOTE, Stage.ACCOUNT, Stage.POSITIONS)
STATE_KEY = "paper_readiness_last_run"

# Static text from docs/PAPER_READINESS.md ("Documented versus synthetic proof versus still unknown",
# "Limitations"). Nothing on this page can observe, attest or change any of it.
STILL_UNKNOWN = (
    "The actual serverVersion, handshake timing and whether the owner's gateway accepts the client id.",
    "The owner's actual port configuration; a port is never evidence of PAPER versus LIVE.",
    "Whether the Read-Only API setting is enabled on the real TWS/IB Gateway (not API-queryable; it must be "
    "attested and verified on the machine).",
    "The real account-id format, whether a paper account exists and whether it is the only managed account.",
    "TLT's actual conId and primary listing, and whether a SMART description resolves uniquely.",
    "The timeZoneId spelling IBKR actually sends and the real holiday calendar.",
    "Which market data type the owner's paper username receives and whether any entitlement is shared to paper.",
    "The real accountSummary tag set and base currency, and whether subscriptions are cancelled after one "
    "snapshot.",
    "The message sequences the owner's gateway actually emits.",
    "Eligibility, protection and fill behaviour, restart survival and weekly reauthentication: none is "
    "exercised offline and none may be inferred from a complete control.",
)
# Static text from docs/PAPER_READINESS.md ("Gate checklist for future read-only paper checks"). The bold
# labels are the documented item names; tests/test_paper_readiness_page.py checks each against the docs.
OWNER_FACTS = (
    "residence country; individual/joint/entity account type; existing IBKR paper/API availability; existing "
    "relevant data entitlements; whether periodic weekly/manual reauthentication is acceptable"
)
CHECKLIST = (
    (
        "Eligibility and account",
        "verified by the owner (residence, entity, account type), with the current "
        "requirements page read by a human.",
    ),
    (
        "Paper account exists",
        "and is the only account the API session manages; its identifier stays in the "
        "owner's local private store, never in chat, Git or any model context.",
    ),
    (
        "API enabled and Read-Only API enabled",
        "on the owner's TWS/IB Gateway, verified on the machine (the "
        "setting is not API-queryable); order precautions and presets must not be bypassed.",
    ),
    (
        "Isolation",
        "a dedicated machine or OS user, a dedicated port chosen by the owner, no live username "
        "logged in elsewhere, no credentials in the environment of any agent process.",
    ),
    (
        "Entitlement",
        "whether live data is shared to the paper username is known before the first quote "
        "request; a delayed or frozen answer is a stop, not a fallback.",
    ),
    (
        "Reauthentication",
        "the owner has accepted the weekly manual reauthentication and the daily restart, "
        "or the check is scheduled inside a freshly authenticated session.",
    ),
    (
        "A connected transport",
        "implementing exactly the five-method read-only surface with the fake's "
        "semantics, introduced by a separate reviewed change; only that change lifts the FAKE-only gate.",
    ),
    (
        "Stop conditions",
        "unknown account or environment, stale or delayed data, ambiguous contract, "
        "inconsistent state, insufficient entitlement, any unclassified message code or any correlation anomaly "
        "beyond an exact duplicate stops a connected check.",
    ),
    (
        "Scope",
        "TLT direct identity, USD, regular session only; no proxy, derivative, sizing or risk-policy "
        "change. Paper order acceptance, protection, reset/correction/bust behaviour and recovery remain a later "
        "material-risk package, fail-closed and unresolved.",
    ),
    (
        "Record",
        "the connected report, redacted as here, is stored locally and summarised to the supervisor; "
        "raw payloads, balances and identifiers never leave the owner's machine.",
    ),
)


def _most_pressing(findings: list[Finding]) -> FindingKind | None:
    """The report's own ordering (HEADLINE_ORDER) applied to one stage; display only, no rule is re-derived."""
    return next((kind for kind in HEADLINE_ORDER if any(f.kind == kind for f in findings)), None)


def _codes(findings: list[Finding]) -> str:
    counts: dict[str, int] = {}
    for f in findings:
        counts[f.code] = counts.get(f.code, 0) + 1
    return (
        ", ".join(f"`{code}` ×{n}" if n > 1 else f"`{code}`" for code, n in counts.items()) or "—"
    )


def _stage_rows(report: ReadinessReport) -> list[str]:
    """One markdown table row per stage, built only from the report's findings, dispatch list and digests."""
    observed = {o.stage: o for o in report.observations}
    refused = report.outcome == ReadinessOutcome.REFUSED_BEFORE_DISPATCH
    rows = [
        "| Stage | Evidence | Rows kept | END seen | Most pressing finding | Codes |",
        "|---|---|---|---|---|---|",
    ]
    for stage in Stage:
        found = [f for f in report.findings if f.stage == stage]
        rows_kept = end_seen = "—"
        unknown = False
        if stage == Stage.PREFLIGHT:
            evidence = "synthetic session attestation"
        elif stage == Stage.SESSION:
            unknown = refused
            evidence = (
                "NOT ASSESSED (refused before dispatch)"
                if refused
                else "synthetic session attestation"
            )
        elif stage == Stage.CORRELATION:
            unknown = not report.dispatched
            evidence = "every dispatched stream" if report.dispatched else "nothing dispatched"
        else:
            digest = observed.get(stage)
            unknown = digest is None
            if digest is None:
                evidence = "NOT DISPATCHED (unknown)"
            else:
                evidence = f"read dispatched (request {digest.request_id})"
                rows_kept, end_seen = str(digest.rows), ("yes" if digest.end_seen else "NO")
        top = _most_pressing(found)
        if top is None:
            status = "UNKNOWN (nothing observed)" if unknown else "no finding recorded"
        elif top == FindingKind.INFO:
            status = "INFO only (control, not permission)"
        else:
            status = top.value
        rows.append(
            f"| {stage.value} | {evidence} | {rows_kept} | {end_seen} | {status} | {_codes(found)} |"
        )
    return rows


st.set_page_config(page_title="Paper Readiness (offline)", page_icon="🧪", layout="wide")
st.title("Paper Readiness (OFFLINE / FAKE ONLY)")
st.error(BANNER)
st.caption(
    "NON-AUTHORITATIVE diagnostics over the merged offline readiness scaffold (docs/PAPER_READINESS.md). "
    + CONTROL_MEANING
    + " Unknown prerequisites are shown as unknown, never as satisfied."
)

with st.sidebar:
    st.error(SIDEBAR_BANNER)
    st.header("Bundled synthetic scenario")
    choice = st.radio(
        "Scenario",
        list(SCENARIOS),
        key="paper_readiness_scenario",
        format_func=lambda name: f"{name} · scripted {SCENARIOS[name].expected_outcome.value}",
    )
    st.caption(
        f"{len(SCENARIOS)} invented fake-clock scripts bundled with the scaffold. Nothing can be typed, "
        "uploaded, pointed at a path or connected from this page."
    )
    if st.button(
        "Re-run selected scenario (deterministic)",
        help="Replays the same FAKE script again. The report digest must be identical; nothing is written or connected.",
    ):
        st.caption("Scenario re-run.")
    st.caption(f"tradingbot import origin: `{Path(tradingbot.__file__).resolve().parent}`")

scenario = SCENARIOS[choice]
try:
    report: ReadinessReport = run_scenario(scenario)
except Exception as exc:  # never a traceback: an exception message could carry a payload
    st.error(
        f"Diagnostic halted safely: {type(exc).__name__} while replaying `{scenario.name}`. No report is "
        "shown; nothing was connected, written or authorized."
    )
    st.stop()

digest = sha256(render_json(report).encode("utf-8")).hexdigest()
last = st.session_state.get(STATE_KEY)
if last and last["scenario"] == scenario.name:
    runs, identical = last["runs"] + 1, last["digest"] == digest
else:
    runs, identical = 1, None
st.session_state[STATE_KEY] = {"scenario": scenario.name, "digest": digest, "runs": runs}

# ----------------------------------------------------------------------------- scenario
st.subheader("Scenario")
st.markdown(f"**`{scenario.name}`** — {scenario.description}")
as_scripted = report.outcome == scenario.expected_outcome
present = {f.code for f in report.findings}
codes_present = all(code in present for code in scenario.expected_codes)
scripted = ", ".join(f"`{code}`" for code in scenario.expected_codes) or "none listed"
line = (
    f"Scripted expectation: **{scenario.expected_outcome.value}** → actual **{report.outcome.value}** "
    f"({'as scripted' if as_scripted else 'UNEXPECTED'}). Codes the script exercises: {scripted} — "
    f"{'all present' if codes_present else 'NOT all present'} in this report."
)
(st.markdown if as_scripted and codes_present else st.error)(line)

# ----------------------------------------------------------------------------- outcome
st.subheader("Synthetic outcome")
non_info = sum(n for kind, n in report.counts.items() if kind != FindingKind.INFO.value)
m1, m2, m3, m4 = st.columns(4)
m1.metric("Outcome", report.outcome.value)
m2.metric("Headline finding kind", report.headline.value)
m3.metric("Reads dispatched", f"{len(report.dispatched)} of {len(READ_STAGES)}")
m4.metric("Non-INFO findings", non_info)
(st.warning if report.outcome == ReadinessOutcome.CONTROL_COMPLETE else st.error)(
    f"{report.outcome.value}: {OUTCOME_MEANING[report.outcome]}"
)
st.markdown(
    f"authority **{report.authority}** · records v{report.records_version} · "
    f"**trading_permission = {report.trading_permission}** · account_ref `{report.account_ref}` (redacted: "
    "prefix plus 8 hex digits of a SHA-256, never the identifier) · "
    f"target `{report.target_symbol}` · as_of {report.as_of.isoformat()} (**FAKE clock**, not the wall clock) · "
    "dispatched: "
    + (", ".join(f"`{m}`" for m in report.dispatched) if report.dispatched else "nothing")
)
st.caption(report.notice)
st.markdown(
    f"Deterministic replay: run {runs} of `{scenario.name}` in this browser session · report digest "
    f"`sha256:{digest}` · "
    + (
        "first run (nothing to compare)"
        if identical is None
        else "identical to the previous run"
        if identical
        else "DIFFERENT from the previous run"
    )
)
if identical is False:
    st.error(
        "The same FAKE script produced a different report on re-run. The scaffold is specified to be "
        "deterministic; treat this as a defect, not as new evidence."
    )

# ----------------------------------------------------------------------------- stages
st.subheader("Stages")
st.markdown("\n".join(_stage_rows(report)))
st.caption(
    "Evidence, rows and END markers come from the report's dispatch list and observation digests; the most "
    "pressing finding per stage uses the report's own kind order. A stage that was not dispatched or not "
    "assessed is unknown, not passed."
)

# ----------------------------------------------------------------------------- findings
st.subheader("Findings (canonical, redacted)")
st.caption(
    "Every line is a Finding from the report: stage, kind, code, request id and the documented detail. INFO "
    "is an informational control, not permission. Details name codes, field names, counts and documented "
    "message texts only."
)
if not report.findings:
    st.info("The report carries no finding.")
for stage in Stage:
    found = [f for f in report.findings if f.stage == stage]
    if found:
        lines = [f"**{stage.value}**"]
        for f in found:
            where = f" (request {f.request_id})" if f.request_id is not None else ""
            lines.append(f"- `{f.kind.value}` **{f.code}**{where}: {f.detail}")
        st.markdown("\n".join(lines))
with st.expander("Finding kinds, most pressing first"):
    st.markdown("\n".join(f"- `{kind.value}`: {KIND_MEANING[kind]}" for kind in HEADLINE_ORDER))

# ----------------------------------------------------------------------------- digests
st.subheader("Observation digests")
if not report.observations:
    st.info(
        "No read was dispatched, so nothing was observed: every connected capability is unknown."
    )
else:
    st.markdown(
        "\n".join(
            [
                "| Stage | Request id | Rows kept | END seen | SHA-256 of the kept envelopes |",
                "|---|---|---|---|---|",
            ]
            + [
                f"| {o.stage.value} | {o.request_id} | {o.rows} | {'yes' if o.end_seen else 'NO'} | `{o.digest}` |"
                for o in report.observations
            ]
        )
    )
    st.caption(
        "A digest ties the report to its correlated inputs without carrying them. No observation payload, "
        "price, size, balance or identifier is available to this page."
    )

# ----------------------------------------------------------------------------- unknowns
st.subheader("Unknown and missing evidence")
not_dispatched = [
    stage.value for stage in READ_STAGES if stage not in {o.stage for o in report.observations}
]
if not_dispatched:
    st.warning(
        "Not observed in this scenario (unknown, not failed): " + ", ".join(not_dispatched) + "."
    )
gaps = [f for f in report.findings if f.kind in GAP_KINDS]
if gaps:
    st.warning(
        "Findings whose kind records absent, incomplete, ambiguous or unentitled evidence: "
        + ", ".join(f"`{f.code}` ({f.kind.value})" for f in gaps)
        + "."
    )
if not not_dispatched and not gaps:
    st.info(
        "No evidence gap in the synthetic observation set. That is still a FAKE control: nothing listed below "
        "has been observed."
    )
st.markdown(
    "**Still unknown whatever the outcome (from docs/PAPER_READINESS.md):**\n"
    + "\n".join(f"- {item}" for item in STILL_UNKNOWN)
)

# ----------------------------------------------------------------------------- checklist
st.subheader("Prerequisite checklist (static, from docs/PAPER_READINESS.md)")
st.warning(
    "None of these ten items is recorded as satisfied, and this page cannot attest, verify or configure any of "
    "them: it only lists them. A connected read-only check may be considered only after the owner or supervisor "
    "records every item together with the owner's non-secret facts ("
    + OWNER_FACTS
    + "). A synthetic control "
    "changes none of them."
)
st.markdown("\n".join(f"- **NOT RECORDED** — **{label}**: {text}" for label, text in CHECKLIST))

st.divider()
st.caption(
    "This page has no control that connects, orders, cancels, replaces, resets, starts or resumes anything; it "
    "reads no path, environment variable or wall clock and writes nothing. Its two inputs are the "
    "bundled-scenario choice and the deterministic re-run (tests/test_paper_readiness_page.py verifies). The "
    "deterministic gateway (validator.py) is unchanged and still decides every action."
)
