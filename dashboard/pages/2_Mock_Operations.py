"""Mock Operations: a READ-ONLY view of synthetic mock ledgers (OPS-01, issue #9).

Renders positions, fictional fills, validation outcomes and reconciliation discrepancies from the
existing mock engines through `tradingbot.operations_view`. Inputs are synthetic ledgers built in a
task-local temporary directory; the page never opens the data/ directory, a broker or the network,
never displays source text, and has no control that submits or changes a trade.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import streamlit as st

import tradingbot
from tradingbot.operations_view import (
    PROTECTION_NOTES,
    SNAPSHOT_STALE_AFTER_SECONDS,
    MockLedgers,
    OperationsSnapshot,
    build_snapshot,
    build_synthetic_ledgers,
    new_fixture_directory,
    snapshot_freshness,
)

SCENARIOS = {"Synthetic: populated run": "populated", "Synthetic: empty run": "empty"}

st.set_page_config(page_title="Mock Operations", page_icon="📋", layout="wide")
st.title("Mock Operations (read-only)")
st.error("MOCK / PAPER SIMULATION ONLY — READ-ONLY VIEW — NO ORDER SUBMISSION, NO LEDGER CHANGES")
st.caption(
    "Synthetic, task-local ledgers only. This page never opens the data/ directory, a broker or the network, "
    "and never displays source text. Nothing here certifies a position as healthy or protected."
)


def _fmt(ts: datetime | None) -> str:
    return (
        ts.strftime("%Y-%m-%d %H:%M:%S UTC")
        if ts and ts.tzinfo
        else (ts.isoformat() + " (no timezone)" if ts else "none")
    )


def _ensure_fixture(scenario: str) -> MockLedgers:
    """Build the synthetic ledgers once per scenario and session; later reruns only read them."""
    if (
        st.session_state.get("mock_ops_scenario") != scenario
        or "mock_ops_ledgers" not in st.session_state
    ):
        ledgers = build_synthetic_ledgers(new_fixture_directory(), scenario)  # type: ignore[arg-type]
        st.session_state["mock_ops_scenario"] = scenario
        st.session_state["mock_ops_ledgers"] = ledgers.model_dump(mode="json")
        st.session_state["mock_ops_built_at"] = datetime.now(timezone.utc).isoformat()
    return MockLedgers.model_validate(st.session_state["mock_ops_ledgers"])


with st.sidebar:
    st.header("Synthetic data source")
    choice = st.radio("Scenario", list(SCENARIOS), key="mock_ops_scenario_label")
    st.caption(
        "Both scenarios are fictional ledgers written by the existing mock engines into a temporary directory."
    )
    if st.button(
        "Refresh view (read-only)",
        help="Re-reads the ledgers through read-only connections. Nothing is written.",
    ):
        st.caption("View refreshed.")
    st.caption(f"tradingbot import origin: `{Path(tradingbot.__file__).resolve().parent}`")

ledgers = _ensure_fixture(SCENARIOS[choice])
snapshot: OperationsSnapshot = build_snapshot(ledgers, now=datetime.now(timezone.utc))
counts = snapshot.counts()

# ----------------------------------------------------------------------------- as-of
st.subheader("As-of")
age, fresh = snapshot_freshness(snapshot)
st.markdown(
    f"**Snapshot as-of:** {_fmt(snapshot.as_of)} — ledgers were read at render time. "
    "The browser does not auto-refresh; what you see is only as current as this timestamp."
)
if fresh != "FRESH":
    st.warning(
        f"Snapshot freshness: {fresh} (older than {SNAPSHOT_STALE_AFTER_SECONDS} s). Refresh before relying on it."
    )
built = st.session_state.get("mock_ops_built_at")
st.markdown(
    f"**Fixture:** `{ledgers.label}` built {built or 'unknown'} in a task-local temporary directory."
)
for act in snapshot.activity:
    name = "Journal ledger (Sprint 1)" if act.engine == "journal" else "Replay ledger (Sprint 1.1)"
    if not act.readable:
        st.error(
            f"{name}: UNREADABLE — `{act.path}` could not be opened read-only. See reconciliation."
        )
        continue
    if act.untimed_rows:
        st.warning(
            f"{name}: {act.rows_on_record} rows on record; last ledger write UNKNOWN because "
            f"{act.untimed_rows} row(s) carry a write timestamp without a timezone (basis: {act.last_write_basis})."
        )
    elif act.last_write_at is None:
        st.markdown(
            f"**{name}:** {act.rows_on_record} rows on record; **no ledger write recorded** (nothing to observe)."
        )
    else:
        age_text = (
            f"{act.seconds_before_as_of:.0f} s before as-of"
            if act.seconds_before_as_of is not None
            else "age UNKNOWN"
        )
        st.markdown(
            f"**{name}:** {act.rows_on_record} rows on record; last ledger write {_fmt(act.last_write_at)} "
            f"({age_text}; basis: {act.last_write_basis})."
        )
    if act.undecodable_rows:
        st.error(
            f"{name}: {act.undecodable_rows} stored record(s) did not decode; fills were not reconciled. "
            "See reconciliation."
        )
    if act.history_truncated:
        st.warning(
            f"{name}: only {act.rows_read} of {act.rows_on_record} rows were read; reconciliation is incomplete."
        )

m1, m2, m3, m4 = st.columns(4)
m1.metric("Open mock positions", counts["open_positions"])
m2.metric("Fictional fills", counts["fills"])
m3.metric("Outcomes needing review", counts["needs_review"])
m4.metric("Discrepancies", counts["serious_discrepancies"] + counts["warning_discrepancies"])
if snapshot.is_empty:
    st.info("Empty run: no positions, fills or validation outcomes are recorded in either ledger.")

# ----------------------------------------------------------------------------- positions
st.subheader("Mock positions")
open_positions = snapshot.open_positions
if not open_positions:
    st.info("No open mock positions recorded in either ledger.")
else:
    st.dataframe(
        [p.model_dump(mode="json", exclude={"protection_note"}) for p in open_positions],
        hide_index=True,
    )
    by_status: dict[str, int] = {}
    for p in open_positions:
        by_status[p.protection] = by_status.get(p.protection, 0) + 1
    st.warning(
        "Protection is not verified for any position: "
        + " | ".join(
            f"{n} × {status}: {PROTECTION_NOTES[status].rstrip('.')}"
            for status, n in sorted(by_status.items())
        )
        + "."
    )
st.caption(
    "No market quote is stored in the ledgers, so valuation and P&L are unknown and are not displayed."
)
if snapshot.closed_ideas:
    with st.expander(
        f"Closed or empty replay ideas retained in the ledger ({len(snapshot.closed_ideas)})"
    ):
        st.dataframe(
            [p.model_dump(mode="json", exclude={"protection_note"}) for p in snapshot.closed_ideas],
            hide_index=True,
        )

# ----------------------------------------------------------------------------- fills
st.subheader("Fictional orders and fills")
if not snapshot.fills:
    st.info("No fictional fills recorded.")
else:
    st.dataframe([f.model_dump(mode="json") for f in snapshot.fills], hide_index=True)
    st.caption(
        "Every row is a simulated fill or amendment recorded by a mock engine. No broker was contacted."
    )

# ----------------------------------------------------------------------------- validation
st.subheader("Validation outcomes")
if not snapshot.validations:
    st.info("No validation outcomes recorded.")
else:
    if counts["needs_review"]:
        st.warning(
            f"{counts['needs_review']} outcome(s) need operator review (RECEIVED, ERROR, REVIEW, HOLD or RECONCILE)."
        )
    rows = []
    for v in snapshot.validations:
        row = v.model_dump(mode="json")
        row["reasons"] = "; ".join(v.reasons)
        rows.append(row)
    st.dataframe(rows, hide_index=True)
    st.caption(
        f"source_freshness is the source timestamp's age at as-of against the shared MockPolicy window "
        f"({snapshot.signal_window_seconds} s): STALE/FUTURE/UNKNOWN means the source could not authorize anything now; "
        "it does not change the recorded outcome."
    )

# ----------------------------------------------------------------------------- reconciliation
st.subheader("Reconciliation")
if snapshot.discrepancies:
    for d in snapshot.discrepancies:
        message = f"{d.severity} {d.code} [{d.engine}] {d.subject}: {d.detail}"
        (st.error if d.severity == "SERIOUS" else st.warning)(message)
if not snapshot.reconciliation_complete:
    st.error(
        "Reconciliation is INCOMPLETE: at least one ledger was unreadable or only partly read."
    )
elif not snapshot.discrepancies:
    if snapshot.is_empty:
        st.info("Nothing to reconcile: both ledgers are empty.")
    else:
        st.info(
            "No discrepancies were detected by the deterministic checks below. This is not a health certification: "
            "protection is unverified, no quotes exist and no broker state was compared."
        )
with st.expander("Checks performed"):
    for check in snapshot.checks_performed:
        st.markdown(f"- {check}")

st.divider()
st.caption(
    "This page has no control that submits, amends or cancels a trade, and refreshing it cannot change a ledger: "
    "all reads use SQLite read-only connections (tests/test_mock_operations.py verifies both)."
)
