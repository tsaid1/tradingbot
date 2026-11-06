"""Shadow Run Health: a READ-ONLY view of a synthetic shadow run's artifacts (OPS-02, issue #18).

Renders identity, progress, run state, observation age, resumability and the existing ledger
reconciliation for the merged SHADOW-01 synthetic shadow runner through
`tradingbot.operational_health`. The inputs are synthetic scenarios built by the existing runner
into a task-local temporary directory. The page never opens the data/ directory, a broker or the
network, never displays source text, and has no control that starts, stops, resumes or recovers a
run or submits an order. The only controls are the scenario, a display-only fake clock and a
read-only refresh.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import streamlit as st

import tradingbot
from tradingbot.operational_health import (
    HEALTH_LABEL,
    PROGRESS_STALE_AFTER_SECONDS,
    SCENARIO_DESCRIPTIONS,
    SCENARIOS,
    ShadowRunArtifacts,
    ShadowRunHealth,
    assess_shadow_run,
    build_scenario,
    collect_evidence,
    fake_clock,
    new_scenario_directory,
)

SCENARIO_LABELS = {f"Synthetic: {name.replace('_', ' ')}": name for name in SCENARIOS}
CLOCK_MODES = {
    "Real wall clock": "real",
    "Fake: 30 s after the last write (FRESH)": "fresh",
    "Fake: 1 h 5 min after the last write (STALE)": "stale",
    "Fake: 1 h before the last write (FUTURE)": "future",
}

st.set_page_config(page_title="Shadow Run Health", page_icon="🩺", layout="wide")
st.title("Shadow Run Health (read-only)")
st.error(
    "SYNTHETIC SHADOW RUN — READ-ONLY HEALTH VIEW — NO START, STOP, RESUME, RECOVERY, ORDER OR "
    "ACCOUNT CONTROLS — NOT PRODUCTION"
)
st.caption(
    "Observes the artifacts of the merged synthetic shadow runner (checkpoint, mock ledger, optional "
    "report) built into a task-local temporary directory. The page never opens the data/ directory, "
    "a broker or the network, never displays source text, and no state shown here authorizes any "
    "trading, recovery or account action. The runner emits no heartbeat, so process liveness is "
    "always UNKNOWN."
)


def _fmt(ts: datetime | None) -> str:
    if ts is None:
        return "UNKNOWN"
    if ts.tzinfo is None:
        return ts.isoformat() + " (no timezone)"
    return ts.strftime("%Y-%m-%d %H:%M:%S UTC")


def _fmt_age(seconds: float | None) -> str:
    return "UNKNOWN" if seconds is None else f"{seconds:.0f} s"


def _ensure_scenario(scenario: str) -> ShadowRunArtifacts:
    """Build the synthetic scenario once per selection and session; later reruns only read it."""
    if st.session_state.get("srh_scenario") != scenario or "srh_artifacts" not in st.session_state:
        artifacts = build_scenario(new_scenario_directory(), scenario)  # type: ignore[arg-type]
        st.session_state["srh_scenario"] = scenario
        st.session_state["srh_artifacts"] = artifacts.model_dump(mode="json")
        st.session_state["srh_built_at"] = datetime.now(UTC).isoformat()
    return ShadowRunArtifacts.model_validate(st.session_state["srh_artifacts"])


with st.sidebar:
    st.header("Synthetic scenario")
    scenario_label = st.radio("Scenario", list(SCENARIO_LABELS), key="srh_scenario_label")
    scenario = SCENARIO_LABELS[scenario_label]
    st.caption(SCENARIO_DESCRIPTIONS[scenario])
    st.caption(
        "Every scenario is a fresh run of the existing synthetic runner into a temporary directory; "
        "fault scenarios damage only those freshly written synthetic files."
    )
    st.header("Fake clock (display only)")
    clock_label_choice = st.radio("As-of clock", list(CLOCK_MODES), key="srh_clock_label")
    untimed = st.checkbox(
        "Fake: drop file write times (UNTIMED)",
        key="srh_untimed",
        help="Judges the same evidence as if no write time were available. Nothing on disk changes.",
    )
    st.caption(
        "The fake clock changes only the as-of time the evidence is judged against. It never moves "
        "virtual event time and never writes a file."
    )
    if st.button(
        "Refresh view (read-only)",
        help="Re-reads the same files. Nothing is written, started, stopped or resumed.",
    ):
        st.caption("View refreshed.")
    st.caption(f"tradingbot import origin: `{Path(tradingbot.__file__).resolve().parent}`")

artifacts = _ensure_scenario(scenario)
evidence = collect_evidence(artifacts)
if untimed:
    evidence = evidence.without_write_times()
as_of, clock_label = fake_clock(evidence, CLOCK_MODES[clock_label_choice])  # type: ignore[arg-type]
health: ShadowRunHealth = assess_shadow_run(evidence, now=as_of, clock_label=clock_label)
counts = health.counts()
obs = health.observation

# ----------------------------------------------------------------------------- observation
st.subheader("Observation")
st.markdown(
    f"**Files examined at:** {_fmt(obs.collected_at)} (real wall clock). "
    f"**Judged as of:** {_fmt(obs.as_of)} ({obs.clock_label}). "
    f"**Fixture:** `{health.evidence_label}` built {st.session_state.get('srh_built_at', 'unknown')} "
    "in a task-local temporary directory."
)
st.dataframe(
    [
        {
            "artifact": f.role,
            "present": f.present,
            "size_bytes": f.size_bytes,
            "written_at (wall clock)": _fmt(f.written_at),
            "basis": f.written_at_basis,
        }
        for f in health.files
    ],
    hide_index=True,
)
freshness_text = (
    f"**Progress freshness:** {obs.progress_freshness} — last checkpoint write "
    f"{_fmt(obs.checkpoint_written_at)}, age {_fmt_age(obs.checkpoint_age_seconds)} at as-of "
    f"(display threshold {obs.stale_after_seconds} s, future tolerance {obs.future_tolerance_seconds} s)."
)
if obs.progress_freshness == "FRESH":
    st.markdown(freshness_text)
else:
    st.warning(freshness_text)
st.info(f"**Process liveness: {obs.process_liveness}.** {obs.liveness_note}")

# ----------------------------------------------------------------------------- run state
st.subheader("Run state")
m1, m2, m3, m4 = st.columns(4)
m1.metric("Run state", health.run_state)
m2.metric(
    "Checkpoint cursor",
    f"{health.progress.cursor if health.progress.cursor is not None else 'UNKNOWN'}"
    f" / {health.progress.deliveries_total}",
)
m3.metric("Resumability", health.resumability)
m4.metric("Serious findings", counts["serious_findings"] + counts["serious_ledger_discrepancies"])
if health.run_state in ("INVALID", "CONTRADICTORY"):
    st.error(health.headline)
elif health.run_state == "INCOMPLETE":
    st.warning(health.headline)
else:
    st.info(health.headline)
if health.attention_required:
    st.warning("Attention required: see the findings and the ledger reconciliation below.")
st.caption(
    "Run states are observations of the artifacts: COMPLETE and PAUSED are the strongest statements "
    "this page makes. It never reports a run as healthy or running, and unknown or contradictory "
    "evidence is never upgraded."
)

# ----------------------------------------------------------------------------- identity
st.subheader("Identity")
st.dataframe(
    [
        {
            "field": row.field,
            "expected (feed / policy in use)": row.expected,
            "checkpoint": row.checkpoint if row.checkpoint is not None else "—",
            "report": row.report if row.report is not None else "—",
            "consistent": "n/a" if row.consistent is None else ("yes" if row.consistent else "NO"),
        }
        for row in health.identity
    ],
    hide_index=True,
)
mismatched = [row.field for row in health.identity if row.consistent is False]
if mismatched:
    st.error(f"Identity mismatch on: {', '.join(mismatched)}. The artifacts describe another run.")

# ----------------------------------------------------------------------------- progress
st.subheader("Progress")
p = health.progress
if p.cursor is None:
    st.warning(
        "Cursor UNKNOWN: no valid checkpoint could be read against the feed in use, so no progress "
        "is shown."
    )
else:
    st.markdown(
        f"**Checkpoint cursor:** {p.cursor} of {p.deliveries_total} deliveries committed "
        f"({p.fraction_complete:.0%}); **remaining:** {p.remaining}"
        + (f"; **next delivery:** `{p.next_delivery_post_key}`." if p.next_delivery_post_key else ".")
    )
    st.markdown(
        f"**Virtual event time of the last committed delivery (checkpoint virtual_now):** "
        f"{_fmt(p.virtual_now)}"
        + (f"; **next virtual delivery:** {_fmt(p.next_virtual_delivery)}" if p.next_virtual_delivery else "")
        + f". _{p.virtual_time_note}._"
    )
if p.report_status is not None:
    st.markdown(
        f"**Report:** status `{p.report_status}` at cursor {p.report_cursor}, resumed from "
        f"{p.report_resumed_from}; this invocation processed {p.report_this_run_processed} deliveries "
        f"({p.report_this_run_duplicates} duplicate or tampered re-deliveries). _{p.report_this_run_note}._"
    )
else:
    st.markdown("**Report:** none readable for this run.")

# ----------------------------------------------------------------------------- resumability
st.subheader("Resumability")
if health.resumability == "RESUMABLE_BY_CONTRACT":
    st.info(
        "RESUMABLE_BY_CONTRACT: every read-only precondition of the runner's resume validation passes. "
        "This is a prediction, not an action and not an authorization."
    )
elif health.resumability == "NOTHING_TO_RESUME":
    st.info("NOTHING_TO_RESUME: the cursor is at the end of the feed; a resume would deliver nothing.")
elif health.resumability == "NOT_RESUMABLE":
    st.error("NOT_RESUMABLE: at least one precondition fails; the runner would refuse to resume.")
else:
    st.warning("Resumability UNKNOWN: at least one precondition could not be evaluated.")
st.dataframe(
    [c.model_dump(mode="json") for c in health.resumability_checks],
    hide_index=True,
)
st.caption(health.resumability_note)

# ----------------------------------------------------------------------------- findings
st.subheader("Findings")
for f in health.findings:
    message = f"{f.severity} {f.code} [{f.subject}]: {f.detail}"
    {"SERIOUS": st.error, "WARNING": st.warning, "INFO": st.info}[f.severity](message)
if not health.findings:
    st.info("No findings.")

# ----------------------------------------------------------------------------- ledger
st.subheader("Ledger reconciliation (OPS-01 read-model, virtual clock)")
ledger = health.ledger
if ledger is None:
    st.error("No ledger file exists for this run; nothing could be reconciled.")
elif not ledger.readable:
    st.error("The ledger file could not be opened read-only as a SQLite ledger; see the discrepancies.")
    for d in ledger.discrepancies:
        st.error(f"{d.severity} {d.code} [{d.engine}] {d.subject}: {d.detail}")
else:
    st.markdown(
        f"**Ledger evaluated on the virtual clock at:** {_fmt(ledger.virtual_as_of)} "
        f"(basis: {ledger.virtual_as_of_basis}). Ledger timestamps are virtual event time and are not "
        "aged against the wall clock."
    )
    l1, l2, l3, l4 = st.columns(4)
    l1.metric("Posts on record", ledger.posts_on_record)
    l2.metric("Ideas", len(ledger.ideas))
    l3.metric("Mock orders", len(ledger.fills))
    l4.metric("Outcomes needing review", ledger.outcomes_needing_review)
    for d in ledger.discrepancies:
        message = f"{d.severity} {d.code} [{d.engine}] {d.subject}: {d.detail}"
        (st.error if d.severity == "SERIOUS" else st.warning)(message)
    if not ledger.reconciliation_complete:
        st.error("Reconciliation is INCOMPLETE: the ledger was unreadable or only partly decoded.")
    elif not ledger.discrepancies:
        st.info(
            "No discrepancies were detected by the OPS-01 deterministic checks. This is not a health "
            "certification: protection is unverified and no broker state was compared."
        )
    if ledger.ideas:
        st.dataframe(
            [i.model_dump(mode="json", exclude={"protection_note", "engine"}) for i in ledger.ideas],
            hide_index=True,
        )
    else:
        st.info("No ideas are recorded in the ledger.")
    st.warning(ledger.flatness_note)
    st.markdown(f"**Outcome counts (latest attempt per post):** `{ledger.outcome_counts}`")
    with st.expander(f"Mock orders recorded in the ledger ({len(ledger.fills)})"):
        if ledger.fills:
            st.dataframe(
                [f.model_dump(mode="json", exclude={"engine"}) for f in ledger.fills], hide_index=True
            )
        else:
            st.markdown("No mock orders recorded.")
    with st.expander("Reconciliation checks performed"):
        for check in ledger.checks_performed:
            st.markdown(f"- {check}")

# ----------------------------------------------------------------------------- limits
st.subheader("Observation limits")
for limit in health.observation_limits:
    st.markdown(f"- {limit}")

st.divider()
st.caption(
    f"{HEALTH_LABEL}. This page has no control that starts, stops, resumes or recovers a run or "
    "submits an order, and rendering or refreshing it cannot change a checkpoint, ledger or report: "
    f"files are read as bytes or through SQLite read-only connections (tests/test_operational_health.py "
    f"verifies this). Progress freshness threshold: {PROGRESS_STALE_AFTER_SECONDS} s (display only)."
)
