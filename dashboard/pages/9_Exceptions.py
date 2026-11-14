"""Exceptions: the register of everything that needs a human, and the operator actions.

Read-only except for these, each a request and never an order: the emergency halt (as on the
Execution page), queueing an operator settlement of an intent of unknown outcome, queueing a clear
or acknowledgement of an exception and, only with ``[operator_confirmation] enabled``, one press to
confirm or dismiss an offer (the confirmed action then passes the gateway and the execution core
again, unchanged; docs/OPERATOR_CONFIRMATION.md). Every request needs a note and the exact confirmation
words, and is applied by the running service on its next cycle, which checks it against the
broker first (``tradingbot.runtime.operator_actions``, ``docs/OPERATIONS.md``).

The runtime config comes from ``TRADINGBOT_RUNTIME_CONFIG`` or ``~/.config/tradingbot/runtime.toml``.
"""

from __future__ import annotations

import getpass
import os
from datetime import UTC, datetime
from pathlib import Path

import streamlit as st

from tradingbot.runtime import operator_actions as Q
from tradingbot.runtime.config import DEFAULT_CONFIG_PATH, ConfigError, load_config
from tradingbot.runtime.confirmation_view import card, load_offers
from tradingbot.runtime.register import load_register
from tradingbot.runtime.store import RuntimeStore

CONFIRM_WORD = "RESUME"
BANNER = (
    "MOCK MODE: simulated broker only. This page places, amends and cancels nothing. Its controls "
    "are the emergency halt and operator requests that only record what you established at the "
    "broker; the service checks each one against the broker before applying it."
)


def _cell(value) -> str:
    if value is None or value == "":
        return "-"
    return str(value).replace("|", "/").replace("\n", " ")


def table(headers: list[str], rows: list[list]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    lines += ["| " + " | ".join(_cell(v) for v in row) + " |" for row in rows]
    return "\n".join(lines)


def _age(seconds) -> str:
    if seconds is None:
        return "-"
    if seconds < 120:
        return f"{seconds:.0f} s"
    if seconds < 7200:
        return f"{seconds / 60:.0f} min"
    return f"{seconds / 3600:.1f} h"


st.set_page_config(page_title="Exceptions (mock)", page_icon="🧪", layout="wide")
st.title("Exception register (MOCK)")
st.error(BANNER)

config_path = Path(os.environ.get("TRADINGBOT_RUNTIME_CONFIG", str(DEFAULT_CONFIG_PATH)))
try:
    config = load_config(config_path)
except ConfigError as exc:
    st.warning(f"No usable runtime config at `{config_path}`: {exc}")
    st.stop()

reg = load_register(config)
halt_file = config.paths.halt_file
try:
    queue = Q.OperatorQueue(config.operator_db)
except Q.OperatorQueueUnavailable as exc:  # never created or repaired from here (review F3)
    queue = None
    st.error(f"Operator actions disabled: the operator queue is unavailable ({exc}). Run "
             "`scripts/tb_operator.py init` once, or restore the queue; see docs/OPERATIONS.md.")


def queue_request(**fields) -> None:
    if queue is None:
        st.error("Not queued: the operator queue is unavailable.")
        return
    try:
        rid = queue.enqueue(at=datetime.now(UTC), origin="dashboard", **fields)
    except Q.OperatorInputError as exc:
        st.error(f"Not queued: {exc}")
        return
    st.success(f"Queued as request {rid}. The service applies it on its next cycle, after "
               "checking it against the broker; the result appears under Operator requests.")


# ------------------------------------------------------------------ status and halt
st.subheader("Status")
c1, c2, c3, c4 = st.columns(4)
c1.metric("Open exceptions", len(reg["open"]))
c2.metric("Unresolved intents", len(reg["unresolved_intents"]))
c3.metric("Broker session", reg["session"].get("state", "?"))
c4.metric("Heartbeat age", _age(reg["heartbeat_age_seconds"]))
if reg["session"].get("state") == "LOCKED_OUT":
    st.error(f"BROKER IDENTITY LOCKOUT ({reg['session'].get('reason')}): the service does not "
             "touch the broker. Connect the expected account, then clear the lockout below.")

st.subheader("Emergency halt")
if reg["halted"]:
    st.error(f"HALT ENGAGED: no new order or amendment; working entries are cancelled. Halt file `{halt_file}`.")
    typed = st.text_input(f"Type {CONFIRM_WORD} to clear the halt", key="reg_halt_confirm")
    if st.button("Clear halt", key="reg_halt_clear"):
        if typed.strip() == CONFIRM_WORD:
            halt_file.unlink(missing_ok=True)
            st.success("Halt cleared. The service resumes on its next cycle.")
        else:
            st.error(f"Not cleared: type {CONFIRM_WORD} exactly to confirm.")
else:
    st.info(f"Halt not engaged (halt file `{halt_file}` absent).")
    if st.button("ENGAGE EMERGENCY HALT", key="reg_halt_engage", type="primary"):
        halt_file.parent.mkdir(parents=True, exist_ok=True)
        halt_file.write_text(f"engaged from dashboard at {datetime.now(UTC).isoformat()}\n")
        st.error("HALT ENGAGED. No new order or amendment; protective orders still follow the position.")

# ------------------------------------------------------------------ open exceptions
st.subheader("Open exceptions")
if reg["open"]:
    st.markdown(
        table(
            ["Source", "Id", "Reason code", "Contract", "Idea", "Ref", "Age", "Action", "What to do"],
            [
                [e["source"], e["id"], e["kind"], e["contract"], e["idea_id"], e["order_ref"],
                 _age(e["age_seconds"]), e["action"], e["instruction"]]
                for e in reg["open"]
            ],
        )
    )
    st.caption("EVIDENCE: only broker evidence resolves it (never an operator). SETTLE: settle its "
               "intent below. CLEAR: verified by a clean reconciliation. ACKNOWLEDGE: blocks "
               "nothing; acknowledge after review.")
else:
    st.success("No open exception.")

op_name = st.text_input("Your name", value=getpass.getuser(), key="op_name")

# ------------------------------------------------------------------ operator confirmation
if config.operator_confirmation.enabled:
    st.subheader("Held signals you can confirm")
    open_offers = load_offers(RuntimeStore(config.runtime_db), open_only=True)
    if not open_offers:
        st.info("No offer open.")
    for row in open_offers:
        c = card(row, datetime.now(UTC))
        with st.container(border=True):
            st.text(c["what a confirmation does"])  # plain text only, never Markdown
            for label in ("offer", "proposed action", "instrument", "price reference",
                          "current quote", "would pass now", "stop", "target",
                          "reason for the hold", "time left", "post"):
                st.text(f"{label}: {c[label]}")
            st.text("uncertainty (what a confirmation resolves):")
            for line in c["uncertainty"]:
                st.text(f"  - {line}")
            b1, b2 = st.columns(2)
            for col, action, word in ((b1, Q.CONFIRM_SIGNAL, "Confirm"),
                                      (b2, Q.DISMISS_SIGNAL, "Dismiss")):
                if col.button(f"{word} {row['id']} ({row['hash'][:8]})",
                              key=f"offer_{word}_{row['id']}_{row['hash'][:8]}"):
                    queue_request(operator=op_name, action=action, target=str(row["id"]),
                                  kind=row["hash"], note="",
                                  confirmation=Q.expected_confirmation(
                                      action, str(row["id"]), kind=row["hash"]))

# ------------------------------------------------------------------ settlement
st.subheader("Intents of unknown outcome")
if reg["unresolved_intents"]:
    st.markdown(
        table(
            ["Ref", "Action", "Idea", "Contract", "Side", "Qty", "Limit", "State", "Submissions",
             "Age"],
            [
                [i["order_ref"], i["action"], i["idea_id"], i["contract"], i["side"],
                 i["quantity"], i["limit_price"], i["state"], i["attempts"],
                 _age(i["age_seconds"])]
                for i in reg["unresolved_intents"]
            ],
        )
    )
    st.warning("An entry or add of unknown outcome is never resent and blocks new exposure in its "
               "contract. Check the order at the broker yourself, then declare what you saw. A "
               "declaration the broker contradicts is refused; after it, the contract stays "
               "blocked until repeated broker lookups over the settle period and a clean "
               "reconciliation confirm it, and an order that appears later is raised as a "
               "contradiction.")
    ref = st.selectbox("Intent", [i["order_ref"] for i in reg["unresolved_intents"]],
                       key="settle_ref")
    outcome = st.radio("What the broker shows", ["NOT_AT_BROKER", "FILLED", "CANCELLED"],
                       key="settle_outcome", horizontal=True)
    qty = price = None
    if outcome == "FILLED":
        qty = int(st.number_input("Filled quantity", min_value=0, step=1, key="settle_qty"))
        price = st.text_input("Average fill price", key="settle_price").strip() or None
    note = st.text_area("Note: what you checked at the broker", key="settle_note")
    phrase = Q.expected_confirmation(Q.SETTLE, ref, outcome=outcome, quantity=qty,
                                     avg_price=price)
    typed = st.text_input(f"Type exactly: {phrase}", key="settle_confirm")
    if st.button("Queue settlement", key="settle_submit"):
        queue_request(operator=op_name, action=Q.SETTLE, target=ref, outcome=outcome,
                      quantity=qty if outcome == "FILLED" else None,
                      avg_price=price if outcome == "FILLED" else None, note=note,
                      confirmation=typed)
else:
    st.info("No intent of unknown outcome.")

# ------------------------------------------------------------------ clear / acknowledge
actionable = [e for e in reg["open"] if e["action"] in ("CLEAR", "ACKNOWLEDGE")]
if actionable:
    st.subheader("Clear or acknowledge")
    labels = {f"{e['source']} {e['id']} {e['kind']}": e for e in actionable}
    pick = labels[st.selectbox("Exception", list(labels), key="ack_pick")]
    if pick["source"] == "signal":
        action = Q.ACKNOWLEDGE_SIGNAL
    else:
        action = Q.CLEAR if pick["action"] == "CLEAR" else Q.ACKNOWLEDGE
    ack_note = st.text_area("Note: what you reviewed", key="ack_note")
    phrase = Q.expected_confirmation(action, str(pick["id"]), kind=pick["kind"])
    ack_typed = st.text_input(f"Type exactly: {phrase}", key="ack_confirm")
    if st.button("Queue", key="ack_submit"):
        queue_request(operator=op_name, action=action, target=str(pick["id"]), kind=pick["kind"],
                      note=ack_note, confirmation=ack_typed)

if reg["session"].get("state") == "LOCKED_OUT":
    st.subheader("Clear the broker identity lockout")
    lock_note = st.text_area("Note: which session is connected now", key="lock_note")
    lock_typed = st.text_input(f"Type exactly: {reg['lockout_confirmation']}", key="lock_confirm")
    if st.button("Queue lockout clear", key="lock_submit"):
        queue_request(operator=op_name, action=Q.CLEAR_IDENTITY_LOCKOUT,
                      target=reg["expected_account"], note=lock_note, confirmation=lock_typed)

# ------------------------------------------------------------------ requests and history
st.subheader("Operator requests")
if reg["requests"]:
    st.markdown(
        table(
            ["#", "At", "Operator", "From", "Action", "Target", "Outcome", "Result", "Reasons"],
            [
                [r["id"], r["at"], r["operator"], r["origin"], r["action"], r["target"],
                 r["outcome"] or r["kind"], r["status"] or "PENDING", ", ".join(r["reasons"])]
                for r in reversed(reg["requests"])
            ],
        )
    )
else:
    st.info("No operator request yet.")

st.subheader("Alerts")
if reg["alerts_open"]:
    st.markdown(
        table(
            ["Key", "Kind", "Opened", "Last seen", "Notified"],
            [[a["key"], a["kind"], a["opened_at"], a["last_seen_at"], a.get("notified")]
             for a in reg["alerts_open"]],
        )
    )
else:
    st.success("No open alert.")
if reg["alerts_feed"]:
    st.caption("Latest notifications (alerts file, newest first)")
    st.markdown(
        table(["At", "Kind", "Title", "Message"],
              [[a.get("at"), a.get("kind"), a.get("title"), a.get("message")]
               for a in reg["alerts_feed"][:30]])
    )

st.subheader("Resolved history")
if reg["history"]:
    st.markdown(
        table(
            ["Source", "Id", "Reason code", "Contract", "Idea", "Ref", "Opened", "Resolved"],
            [[e["source"], e["id"], e["kind"], e["contract"], e["idea_id"], e["order_ref"],
              e["opened_at"], e["resolved_at"]] for e in reg["history"][:100]],
        )
    )
else:
    st.info("Nothing resolved yet.")

st.divider()
st.caption("docs/OPERATIONS.md: what each alert means and the operator's action.")
