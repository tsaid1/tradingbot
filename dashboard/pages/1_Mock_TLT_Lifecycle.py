"""Streamlit page: the synthetic, mock-only TLT lifecycle through the existing interpreter, gateway
and paper coordinator. Presentation only; the glue lives in src/tradingbot/demo_lifecycle.py.

Run from the repository root, as a page of the existing dashboard or on its own:
    streamlit run dashboard/app.py                        # then open "Mock TLT Lifecycle" in the sidebar
    streamlit run dashboard/pages/1_Mock_TLT_Lifecycle.py
"""
from __future__ import annotations

import os
from pathlib import Path

import streamlit as st

from tradingbot.demo_lifecycle import (
    MOCK_BANNER,
    STATUS_EXPLANATIONS,
    AuditEntry,
    DemoSession,
    fresh_database,
)

DB_PATH = Path(os.environ.get("TRADINGBOT_DEMO_DB", "data/demo_tlt_lifecycle.sqlite3"))
ICON = {"SIMULATED": "✅", "HOLD": "⏸️", "BLOCK": "⛔", "NO_TRADE": "➖", "RECONCILE": "🔎", "DUPLICATE": "🔁"}

st.set_page_config(page_title="Mock TLT lifecycle", page_icon="🧪", layout="wide")
st.title("Mock TLT lifecycle: synthetic end-to-end demonstration")
st.error(MOCK_BANNER)


def new_session(*, reset: bool) -> DemoSession:
    if reset:
        fresh_database(DB_PATH)
    return DemoSession(DB_PATH)


def provenance_text(entry: AuditEntry) -> str:
    p = entry.provenance
    return (("verified" if p.verified_author else "UNVERIFIED") + f" author · rev {p.revision} · "
            + ("complete" if p.complete_text else "truncated") + (" · repost" if p.is_repost else ""))


def interpretation_text(entry: AuditEntry) -> str:
    parts = []
    for i in entry.interpretations:
        levels = " ".join(f"{name} {value}" for name, value in (
            ("@", i.entry_reference), ("stop", i.stop_price), ("target", i.target_price), ("½" if i.close_fraction == "0.5" else "fraction", i.close_fraction)) if value)
        ref = f" → {i.idea_reference}" if i.idea_reference else ""
        parts.append(f"{i.message_class}/{i.tense} {i.asset or '?'} {i.side or ''} {levels}{ref}".strip())
    return "; ".join(parts) or "no clause"


def ledger_text(entry: AuditEntry) -> str:
    return ", ".join(f"{r.idea_id} {r.status} {r.quantity}u stop {r.stop_price or '-'} tgt {r.target_price or '-'}"
                     for r in entry.ledger_after) or "empty"


def result_icon(entry: AuditEntry) -> str:
    if entry.duplicate:
        return ICON["DUPLICATE"]
    return " ".join(ICON.get(o.status, "•") for o in entry.outcomes) or "•"


if "demo" not in st.session_state:
    st.session_state.demo = new_session(reset=False)
demo: DemoSession = st.session_state.demo
script = demo.script
st.caption(script.notice)

with st.sidebar:
    st.header("Controls")
    if st.button("Run next step", key="run_next", type="primary", disabled=demo.is_complete):
        demo.run_next()
    if st.button("Run all remaining steps", key="run_all", disabled=demo.is_complete):
        demo.run_all()
    if st.button("Replay last processed post (idempotency check)", key="replay", disabled=demo.steps_done == 0):
        demo.replay()
    if st.button("Reset demo (empty mock ledger)", key="reset"):
        st.session_state.demo = new_session(reset=True)
        st.rerun()
    st.header("Mock policy (read-only)")
    policy = demo.policy
    st.table({
        "Units per OPEN": [policy.quantity], "Units per ADD": [policy.quantity_per_add],
        "Mock notional cap (USD)": [str(policy.max_notional_usd)], "Max entry deviation (%)": [str(policy.max_price_deviation_pct)],
        "Max spread (%)": [str(policy.max_spread_pct)], "Stop required on OPEN": [policy.require_stop_on_open],
        "Paper only": [policy.paper_only],
    })
    st.caption(f"Mock ledger: `{DB_PATH}` (SQLite, git-ignored). The interpreter, gateway and policy are the "
               "repository's own; the demo bypasses none of them.")

st.progress(demo.steps_done / demo.total_steps, text=f"{demo.steps_done} of {demo.total_steps} synthetic posts processed")

left, right = st.columns(2)
with left:
    st.subheader("Mock position")
    ledger = demo.ledger()
    if not ledger:
        st.info("No mock position yet. Run the first step to open one.")
    for idea in ledger:
        is_open = idea.status == "OPEN" and idea.quantity > 0
        st.markdown(f"**{idea.symbol} {idea.position_side}** · idea `{idea.idea_id}` · "
                    + ("🟢 OPEN, protected in the mock ledger" if is_open else "⚪ CLOSED"))
        c1, c2, c3 = st.columns(3)
        c1.metric("Units", idea.quantity)
        c2.metric("Stop", idea.stop_price or "—")
        c3.metric("Target", idea.target_price or "—")
    st.caption("Protection levels are ledger records only. No broker-native stop or target exists anywhere in this application.")
with right:
    st.subheader("Session summary")
    s = demo.summary()
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Mock fills", s["mock_fills"])
    m2.metric("Protection amendments", s["protection_amendments"])
    m3.metric("Holds / blocks", f"{s['holds']} / {s['blocks']}")
    m4.metric("Duplicates rejected", s["duplicates_rejected"])
    m5, m6, m7, m8 = st.columns(4)
    m5.metric("No-trades", s["no_trades"])
    m6.metric("Reconciles", s["reconciles"])
    m7.metric("As expected", "yes" if s["expectations_met"] else "NO")
    m8.metric("Book agrees", "yes" if s["ledger_agrees_with_publisher"] else "NO")

st.subheader("Lifecycle audit")
if not demo.entries:
    st.info("Nothing processed yet. Use the controls on the left.")
else:
    st.dataframe([{
        "#": e.step, "Source": e.source_id, "As of (UTC)": e.processed_as_of.strftime("%Y-%m-%d %H:%M"),
        "Provenance": provenance_text(e), "Interpretation": interpretation_text(e),
        "Result": f"{result_icon(e)} {e.headline}", "Ledger after": ledger_text(e),
        "As expected": "✅" if e.matches_expectation else "❌",
    } for e in demo.entries], use_container_width=True, hide_index=True)
    for e in demo.entries:
        with st.expander(f"Step {e.step} · {e.source_id} · {result_icon(e)} {e.headline[:110]}"):
            if e.narration:
                st.markdown(f"**What this step shows.** {e.narration}")
            st.markdown("**Synthetic source text**")
            st.code(e.text, language=None)
            a, b = st.columns(2)
            with a:
                st.markdown("**Provenance (trusted metadata, never model output)**")
                st.json(e.provenance.model_dump(mode="json"))
            with b:
                st.markdown("**Synthetic quote used (operator-typed, not market data)**")
                st.json(e.quotes)
            st.markdown("**Interpretation (advisory; authorizes nothing)**")
            st.dataframe([i.model_dump() for i in e.interpretations], use_container_width=True, hide_index=True)
            st.markdown("**Gateway decision and mock result**")
            for o in e.outcomes:
                st.markdown(f"{ICON.get(o.status, '•')} **{o.status}** · `{'`, `'.join(o.reasons) or '-'}` · action `{o.action_id}`")
                for text in o.explanations:
                    st.markdown(f"- {text}")
                if o.reconciliation_note:
                    st.info(o.reconciliation_note)
                if o.order is not None:
                    st.json(o.order.model_dump(mode="json"))
            if e.duplicate:
                st.warning(STATUS_EXPLANATIONS["DUPLICATE"])
            st.markdown(f"**Expected:** statuses `{e.expected_statuses}`, orders `{e.expected_orders}` → "
                        + ("✅ matched" if e.matches_expectation else "❌ not matched"))
            st.markdown("**Mock ledger after this step**")
            st.dataframe([r.model_dump() for r in e.ledger_after], use_container_width=True, hide_index=True)

st.subheader("Reconciliation: publisher's stated book vs mock ledger")
rows = demo.reconciliation()
if not rows:
    st.info("Nothing to reconcile yet.")
else:
    st.dataframe([{
        "Idea": r.idea_id, "Publisher says": r.publisher_state or "unknown", "Publisher stop": r.publisher_stop or "—",
        "Publisher target": r.publisher_target or "—", "Mock ledger": r.ledger_state or "never filled",
        "Ledger units": r.ledger_quantity if r.ledger_quantity is not None else "—",
        "Ledger stop": r.ledger_stop or "—", "Ledger target": r.ledger_target or "—",
        "Agreement": "✅ agrees" if r.agrees else "❌ operator check required",
    } for r in rows], use_container_width=True, hide_index=True)
    st.caption("A disagreement is an operator task (check the broker book; a stop may have fired). It is never turned into an order.")

with st.expander("Status vocabulary"):
    for status, text in STATUS_EXPLANATIONS.items():
        st.markdown(f"{ICON.get(status, '•')} **{status}**: {text}")
