"""Execution: the runtime service's signal-to-execution lifecycle, read from its journal.

Read-only. It opens the service's SQLite files in read-only mode through
``tradingbot.runtime.lifecycle.load`` and never calls a broker. The one operational control is the
emergency halt: engaging it is one click (it creates the halt file the execution core checks before
every submit or amendment); clearing it needs the confirmation word typed first.

The runtime config comes from ``TRADINGBOT_RUNTIME_CONFIG`` or ``~/.config/tradingbot/runtime.toml``.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import streamlit as st

from tradingbot.runtime.config import DEFAULT_CONFIG_PATH, ConfigError, load_config
from tradingbot.runtime.lifecycle import latency_table, load

CONFIRM_WORD = "RESUME"
BANNER = (
    "MOCK MODE: simulated broker only. This page reads the runtime service's journal; it places, "
    "amends and cancels nothing. Its only control is the emergency halt."
)


def _cell(value) -> str:
    if value is None or value == "":
        return "-"
    return str(value).replace("|", "/")


def table(headers: list[str], rows: list[list]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    lines += ["| " + " | ".join(_cell(v) for v in row) + " |" for row in rows]
    return "\n".join(lines)


def _ms(v) -> str:
    return "-" if v is None else f"{v:.1f}"


st.set_page_config(page_title="Execution (mock)", page_icon="🧪", layout="wide")
st.title("Execution lifecycle (MOCK)")
st.error(BANNER)

config_path = Path(os.environ.get("TRADINGBOT_RUNTIME_CONFIG", str(DEFAULT_CONFIG_PATH)))
try:
    config = load_config(config_path)
except ConfigError as exc:
    st.warning(f"No usable runtime config at `{config_path}`: {exc}")
    st.stop()

view = load(config)
halt_file = config.paths.halt_file

# ------------------------------------------------------------------ halt control
st.subheader("Emergency halt")
if view["halted"]:
    st.error(f"HALT ENGAGED: no new order or amendment; working entries are cancelled. Halt file `{halt_file}`.")
    typed = st.text_input(f"Type {CONFIRM_WORD} to clear the halt", key="halt_confirm")
    if st.button("Clear halt", key="halt_clear"):
        if typed.strip() == CONFIRM_WORD:
            halt_file.unlink(missing_ok=True)
            st.success("Halt cleared. The service resumes on its next cycle.")
        else:
            st.error(f"Not cleared: type {CONFIRM_WORD} exactly to confirm.")
else:
    st.info(f"Halt not engaged (halt file `{halt_file}` absent).")
    if st.button("ENGAGE EMERGENCY HALT", key="halt_engage", type="primary"):
        halt_file.parent.mkdir(parents=True, exist_ok=True)
        halt_file.write_text(f"engaged from dashboard at {datetime.now(UTC).isoformat()}\n")
        st.error("HALT ENGAGED. No new order or amendment; protective orders still follow the position.")

# ------------------------------------------------------------------ health
st.subheader("Source and broker health")
hb = view["heartbeat"]
if not hb:
    st.warning("No heartbeat yet: the service has not completed a cycle with this config.")
else:
    age = (datetime.now(UTC) - datetime.fromisoformat(hb["at"])).total_seconds()
    rec = hb.get("reconciliation") or {}
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Heartbeat age (s)", f"{age:.0f}")
    c2.metric("Source failures in a row", hb["source"]["consecutive_failures"])
    c3.metric("Broker failures in a row", hb["broker"]["consecutive_failures"])
    c4.metric("Reconciliation", "clean" if rec.get("clean") else "DISCREPANCY")
    st.markdown(
        table(
            [
                "Component",
                "Last success",
                "Last error",
                "Error at",
                "Failures",
                "Backoff until",
                "Detail",
            ],
            [
                [
                    "source",
                    hb["source"]["last_success_at"],
                    hb["source"]["last_error"],
                    hb["source"]["last_error_at"],
                    hb["source"]["consecutive_failures"],
                    hb["source"]["next_attempt_at"],
                    f"{hb['source']['name']} cursor {hb['source']['cursor']}",
                ],
                [
                    "broker",
                    hb["broker"]["last_success_at"],
                    hb["broker"]["last_error"],
                    hb["broker"]["last_error_at"],
                    hb["broker"]["consecutive_failures"],
                    hb["broker"]["next_attempt_at"],
                    f"account {hb['broker']['account']}",
                ],
                [
                    "pipeline",
                    hb["pipeline"]["last_success_at"],
                    hb["pipeline"]["last_error"],
                    hb["pipeline"]["last_error_at"],
                    hb["pipeline"]["consecutive_failures"],
                    hb["pipeline"]["next_attempt_at"],
                    f"cycle {hb['cycle']}",
                ],
            ],
        )
    )
    if rec.get("unexplained"):
        st.error("Unexplained reconciliation discrepancies: " + ", ".join(rec["unexplained"]))

# ------------------------------------------------------------------ positions and orders
st.subheader("Positions: follower ledger next to the broker")
st.caption(f"Broker snapshot taken {view['snapshot_at'] or 'never'}.")
if view["positions"]:
    st.markdown(
        table(
            ["Symbol", "Follower", "Broker", "Match"],
            [
                [p["symbol"], p["follower"], p["broker"], "yes" if p["match"] else "**NO**"]
                for p in view["positions"]
            ],
        )
    )
else:
    st.info("Flat: no follower or broker position.")
if view["protection"]:
    st.markdown(
        table(
            ["Idea", "Symbol", "Side", "Qty", "Avg", "Stop", "Stop covers", "Protection"],
            [
                [
                    p["idea_id"],
                    p["symbol"],
                    p["side"],
                    p["quantity"],
                    p["avg_price"],
                    p["stop_price"],
                    p["stop_covers"],
                    p["protection"] if p["protection"] != "UNPROTECTED" else "**UNPROTECTED**",
                ]
                for p in view["protection"]
            ],
        )
    )

st.subheader("Working orders (including protective)")
if view["working_orders"]:
    st.markdown(
        table(
            [
                "Ref",
                "Role",
                "Idea",
                "Symbol",
                "Side",
                "Type",
                "Qty",
                "Filled",
                "Stop",
                "Limit",
                "Status",
            ],
            [
                [
                    o["order_ref"],
                    o["role"],
                    o["idea_id"],
                    o["symbol"],
                    o["side"],
                    o["order_type"],
                    o["quantity"],
                    o["filled"],
                    o["stop_price"],
                    o["limit_price"],
                    o["status"],
                ]
                for o in view["working_orders"]
            ],
        )
    )
else:
    st.info("No working order at the broker.")

# ------------------------------------------------------------------ lifecycle
st.subheader("Signal lifecycle")
st.caption(
    "source → interpretation → mapping → gate decision → order → fills → protective orders "
    "→ reconciliation. Post text is never stored; rows carry identities and reason codes."
)
rows = []
for r in view["lifecycle"]:
    order = r["order"] or {}
    fills = ", ".join(f"{f['quantity']}@{f['price']}" for f in r["fills"])
    protective = ", ".join(
        f"{p['role']} {p['quantity']} {p['stop_price'] or p['limit_price']} {p['status']}"
        for p in r["protective"]
    )
    rows.append(
        [
            r["seq"],
            f"{r['source_id']} r{r['revision']}" + (" (duplicate)" if r["duplicate"] else ""),
            f"{r['action']} {r['timing']}",
            r["asset"],
            f"{r['mapping']}" + (f" ({r['mapping_reason']})" if r["mapping_reason"] else ""),
            r["status"],
            ", ".join(r["reasons"]),
            f"{order.get('side')} {order.get('quantity')} {order.get('order_type')}"
            if order
            else "-",
            r["intent_state"],
            fills,
            protective,
        ]
    )
if rows:
    st.markdown(
        table(
            [
                "#",
                "Source",
                "Interpretation",
                "Asset",
                "Mapping",
                "Decision",
                "Reasons",
                "Order",
                "Intent",
                "Fills",
                "Protective",
            ],
            rows,
        )
    )
else:
    st.info("No signal has been processed yet.")
lat = latency_table(view["lifecycle"])
if lat:
    st.markdown("**Stage latency (ms, from journal timestamps)**")
    st.markdown(
        table(
            [
                "Source",
                "Action",
                "publish→receive",
                "receive→interpret",
                "interpret→gate",
                "gate→submit",
                "submit→ack",
                "ack→fill",
                "end to end",
                "Note",
            ],
            [
                [
                    x["source_id"],
                    x["action"],
                    _ms(x["publish_to_receive"]),
                    _ms(x["receive_to_interpret"]),
                    _ms(x["interpret_to_gate"]),
                    _ms(x["gate_to_submit"]),
                    _ms(x["submit_to_ack"]),
                    _ms(x["ack_to_fill"]),
                    _ms(x["end_to_end"]),
                    "adopted after restart" if x["recovered"] else "",
                ]
                for x in lat
            ],
        )
    )

# ------------------------------------------------------------------ exceptions
st.subheader("Held signals and exceptions")
held = [e for e in view["runtime_exceptions"]]
if held:
    st.markdown(
        table(
            ["At", "Kind", "Source", "Rev", "Action", "Reasons", "Resolved"],
            [
                [
                    e["at"],
                    e["kind"],
                    e["source_id"],
                    e["revision"],
                    e["action_id"],
                    ", ".join(e["reasons"]),
                    e["resolved_at"],
                ]
                for e in held
            ],
        )
    )
else:
    st.info("No held signal.")
ex_open = [e for e in view["execution_exceptions"] if e["resolved_at"] is None]
if ex_open:
    st.error("Open execution exceptions")
    st.markdown(
        table(
            ["At", "Kind", "Idea", "Ref", "Detail"],
            [[e["at"], e["kind"], e["idea_id"], e["order_ref"], e["detail"]] for e in ex_open],
        )
    )
alerts = [a for a in view["alerts"] if a["resolved_at"] is None]
if alerts:
    st.warning("Open alerts: " + ", ".join(f"{a['kind']} ({a['key']})" for a in alerts))

st.divider()
st.caption(
    "Read-only view of the mock service. docs/RUNTIME.md describes the service, the config "
    "and the mapping policy."
)
