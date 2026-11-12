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
SHADOW_BANNER = (
    "SHADOW MODE: every order, fill and position below is SIMULATED at real read-only quotes. "
    "Nothing was sent to any broker: the only broker connection is the read-only IBKR adapter "
    "(market data, contract details and reads). This page places, amends and cancels nothing. "
    "Its only control is the emergency halt."
)
SIM_LABEL = "SIMULATED at real quotes"


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


config_path = Path(os.environ.get("TRADINGBOT_RUNTIME_CONFIG", str(DEFAULT_CONFIG_PATH)))
try:
    config = load_config(config_path)
except ConfigError as exc:
    config = None
    config_problem = str(exc)
SHADOW = config is not None and config.mode == "shadow"
# Shadow mode: the "broker" is the simulator; say so wherever the page names it.
BROKER = "simulated broker" if SHADOW else "broker"

if SHADOW:
    st.set_page_config(page_title="Execution (shadow)", page_icon="🧪", layout="wide")
    st.title("Execution lifecycle (SHADOW: simulated fills at real quotes)")
    st.error(SHADOW_BANNER)
else:
    st.set_page_config(page_title="Execution (mock)", page_icon="🧪", layout="wide")
    st.title("Execution lifecycle (MOCK)")
    st.error(BANNER)
if config is None:
    st.warning(f"No usable runtime config at `{config_path}`: {config_problem}")
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
st.subheader(f"Source and {BROKER} health")
hb = view["heartbeat"]
if not hb:
    st.warning("No heartbeat yet: the service has not completed a cycle with this config.")
else:
    age = (datetime.now(UTC) - datetime.fromisoformat(hb["at"])).total_seconds()
    rec = hb.get("reconciliation") or {}
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Heartbeat age (s)", f"{age:.0f}")
    c2.metric("Source failures in a row", hb["source"]["consecutive_failures"])
    c3.metric(f"{BROKER.capitalize()} failures in a row", hb["broker"]["consecutive_failures"])
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
                    BROKER,
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
    if SHADOW and hb.get("shadow"):
        sh = hb["shadow"]
        st.markdown(
            f"**Read-only quote session** {sh.get('quote_session')} (account "
            f"{sh.get('quote_account')}, market-data type requested "
            f"{sh.get('market_data_type_requested')}, streams {len(sh.get('streams') or [])} of "
            f"{sh.get('stream_cap')}, reconnects {sh.get('reconnects')}). Fills: {SIM_LABEL}."
        )
        if sh.get("streams"):
            st.markdown(table(["Contract", "Quote label", "Reason"],
                              [[s["contract_key"], s["label"], s["reason"]]
                               for s in sh["streams"]]))

# ------------------------------------------------------------------ positions and orders
st.subheader(f"Positions: follower ledger next to the {BROKER}"
             + (f" ({SIM_LABEL})" if SHADOW else ""))
st.caption(f"{BROKER.capitalize()} snapshot taken {view['snapshot_at'] or 'never'}.")
if view["positions"]:
    st.markdown(
        table(
            ["Contract", "Multiplier", "Follower", BROKER.capitalize(), "Match"],
            [
                [
                    p["contract"],
                    p["multiplier"],
                    p["follower"],
                    p["broker"],
                    "yes" if p["match"] else "**NO**",
                ]
                for p in view["positions"]
            ],
        )
    )
else:
    st.info("Flat: no follower or broker position.")
if view["protection"]:
    st.markdown(
        table(
            ["Idea", "Contract", "Mult", "Side", "Qty", "Avg", "Stop", "Stop covers", "Protection"],
            [
                [
                    p["idea_id"],
                    p["contract"],
                    p["multiplier"],
                    p["side"],
                    p["quantity"],
                    p["avg_price"],
                    p["stop_price"],
                    p["stop_covers"],
                    (p["protection"] if p["protection"] != "UNPROTECTED" else "**UNPROTECTED**")
                    # Approval withdrawn after opening: exits and protection only.
                    + (f" / **{p['mapping_status']}**" if p.get("mapping_status") else ""),
                ]
                for p in view["protection"]
            ],
        )
    )

st.subheader("Working orders (including protective)" + (f": {SIM_LABEL}, never sent"
                                                          if SHADOW else ""))
if view["working_orders"]:
    st.markdown(
        table(
            [
                "Ref",
                "Role",
                "Idea",
                "Contract",
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
                    o["contract"],
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
    st.info(f"No working order at the {BROKER}.")

# ------------------------------------------------------------------ lifecycle
st.subheader("Signal lifecycle")
st.caption(
    "source → interpretation → mapping → gate decision → order → fills → protective orders "
    "→ reconciliation. Post text is never stored; rows carry identities and reason codes."
    + (f" Orders, fills and protective orders: {SIM_LABEL}, never sent to any broker."
       if SHADOW else "")
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
                "Simulated order" if SHADOW else "Order",
                "Intent",
                "Simulated fills" if SHADOW else "Fills",
                "Simulated protective" if SHADOW else "Protective",
            ],
            rows,
        )
    )
else:
    st.info("No signal has been processed yet.")
if SHADOW:
    st.subheader(f"Fills ({SIM_LABEL})")
    st.caption("Each fill was computed by the simulator from the real quote shown; no order "
               "reached any broker.")
    if view["shadow_fills"]:
        st.markdown(table(
            ["Exec", "Ref", "Contract", "Side", "Qty", "Price", "Quote label", "Reason",
             "Bid age (s)", "Ask age (s)", "Fill"],
            [[f["exec_id"], f["order_ref"], f["contract_key"], f["side"], f["quantity"],
              f["price"], (f.get("quote") or {}).get("label"), (f.get("quote") or {}).get("reason"),
              (f.get("quote") or {}).get("bid_age_seconds"),
              (f.get("quote") or {}).get("ask_age_seconds"), f["label"]]
             for f in view["shadow_fills"]]))
    else:
        st.info("No simulated fill yet.")
    st.subheader("Quotes each decision was made on")
    rows = [[d["source_id"], d["revision"], ", ".join(f"{x['action_id']} {x['status']}"
                                                     for x in d["decisions"]),
             q["contract_key"], q["label"], q["reason"], q["bid_age_seconds"],
             q["ask_age_seconds"], ", ".join(q["problems"])]
            for d in view["shadow_decisions"] for q in d["quotes"]]
    if rows:
        st.markdown(table(["Source", "Rev", "Decisions", "Contract", "Quote label", "Reason",
                           "Bid age (s)", "Ask age (s)", "Problems"], rows))
    else:
        st.info("No decision on a quote yet.")
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
                "gate→simulated submit" if SHADOW else "gate→submit",
                "simulated submit→ack" if SHADOW else "submit→ack",
                "ack→simulated fill" if SHADOW else "ack→fill",
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

# ------------------------------------------------------------------ coverage
st.subheader("Instrument coverage by source family")
st.caption(
    "Every family with a mapping entry, and every family the publisher named. Only an APPROVED "
    "family can execute; NOT APPROVED and UNSUPPORTED families are held, never ignored."
)
if view.get("coverage_error"):
    st.error(f"Mapping file unreadable: {view['coverage_error']}")
elif view.get("coverage"):
    cov = view["coverage"]
    st.markdown(
        table(
            ["Family", "Mapping", "Execution instrument", "Basis", "Signals", "Executed", "Held",
             "Exceptions", "Held by reason", "Open positions"],
            [
                [
                    c["family"],
                    c["mapping_status"],
                    c["execution"],
                    c["basis"],
                    c["signals"],
                    c["executed"],
                    c["held"],
                    c["exceptions"],
                    ", ".join(f"{k} x{n}" for k, n in c["held_by_reason"].items()),
                    f"{c.get('open_positions', 0)}"
                    + (f" **{c['position_status']}**" if c.get("position_status") else ""),
                ]
                for c in cov
            ],
        )
    )
    unsupported = [c["family"] for c in cov if c["mapping_status"] != "APPROVED" and c["signals"]]
    if unsupported:
        st.warning("Signals held for families that cannot trade: " + ", ".join(unsupported))
    revoked = [c["family"] for c in cov if c.get("position_status")]
    if revoked:
        st.warning(
            "Open positions under a withdrawn mapping (OPEN_UNDER_REVOKED_MAPPING; they can be "
            "reduced, closed and protected on their recorded terms, never added to): "
            + ", ".join(revoked)
        )
else:
    st.info("No mapping entry and no signal yet.")

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
    f"Read-only view of the {'shadow' if SHADOW else 'mock'} service. docs/RUNTIME.md describes "
    "the service, the config and the mapping policy."
)
