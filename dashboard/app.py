"""Streamlit GUI: text/chart -> typed interpretation -> MOCK order only."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
from uuid import uuid4

from dotenv import load_dotenv
import streamlit as st

from tradingbot.interpreters import DemoInterpreter, OpenAIVisionInterpreter
from tradingbot.models import MockPolicy, Quote, SourceEvent
from tradingbot.pipeline import SignalPipeline
from tradingbot.storage import Journal

load_dotenv()
st.set_page_config(page_title="Signal Lab", page_icon="🧪", layout="wide")
st.title("Signal-to-Execution Lab")
st.error("MOCK / PAPER SIMULATION ONLY — NO LIVE BROKER OR LIVE TRADING CAPABILITY")
st.caption("Sprint 1 foundation • private subscriber content is never committed to this repository")

if "source_id" not in st.session_state:
    st.session_state.source_id = f"manual-{uuid4().hex[:12]}"

with st.sidebar:
    st.header("Mode")
    mode = st.radio("Interpreter", ["Deterministic demo", "AI vision (API key required)"])
    st.caption("The deterministic demo accepts only exact synthetic commands. AI Vision uses API credits.")
    st.header("Illustrative mock policy")
    qty = st.number_input("Synthetic shares per OPEN", min_value=1, max_value=100, value=1)
    collar = st.number_input("Max entry deviation (%)", min_value=0.0, max_value=100.0, value=1.0, step=0.1)
    spread = st.number_input("Max quote spread (%)", min_value=0.0, max_value=100.0, value=1.0, step=0.1)
    notional = st.number_input("Max synthetic notional (USD)", min_value=1.0, value=2000.0)

left, right = st.columns([3, 2])
with left:
    st.subheader("Source")
    examples = {
        "Synthetic OPEN": "BUY ABC @ 100 STOP 95 TARGET 110",
        "Synthetic SHORT": "SHORT XYZ @ 50 STOP 55 TARGET 40",
        "Research (not executable)": "We remain bullish on ABC, with support near 95 and a target of 110.",
        "Synthetic CLOSE": "CLOSE ABC IDEA initial-idea",
        "Synthetic REDUCE": "REDUCE 50% ABC IDEA initial-idea",
    }
    example = st.selectbox("Synthetic example", list(examples))
    message = st.text_area("Signal text", value=examples[example], height=180)
    attachment = st.file_uploader("Optional chart image (AI Vision only)", type=["png", "jpg", "jpeg", "webp"])
    st.text_input("Source event ID (keep identical to test deduplication)", key="source_id")
    st.caption("For historical real reports, use analysis mode. Source timestamps here are manual demo timestamps, not proof of live publication latency.")
with right:
    st.subheader("Synthetic quote")
    symbol = st.text_input("Quote instrument symbol", value="ABC")
    bid = st.number_input("Bid", min_value=0.01, value=99.95, step=0.05)
    ask = st.number_input("Ask", min_value=0.01, value=100.05, step=0.05)
    st.caption("Quotes are typed by the operator. They are not live market data.")

journal = Journal(Path(os.environ.get("TRADINGBOT_DB", "data/tradingbot_demo.sqlite3")))
policy = MockPolicy(
    quantity=int(qty), max_price_deviation_pct=Decimal(str(collar)),
    max_spread_pct=Decimal(str(spread)), max_notional_usd=Decimal(str(notional)),
)
if st.button("Interpret → Validate → Simulate", type="primary"):
    image = attachment.getvalue() if attachment is not None else None
    try:
        interpreter = DemoInterpreter() if mode == "Deterministic demo" else OpenAIVisionInterpreter()
        pipeline = SignalPipeline(journal, interpreter, policy)
        source = SourceEvent(source_id=st.session_state.source_id, text=message)
        quote = Quote(
            symbol=symbol.upper(), bid=Decimal(str(bid)), ask=Decimal(str(ask)),
            observed_at=datetime.now(timezone.utc),
        )
        result = pipeline.process(source, quote, image=image)
        st.subheader(f"Decision: {result.decision.status}")
        st.json(result.model_dump(mode="json"))
        if result.order:
            st.success("Fictional order recorded. No external broker contacted.")
        st.info("Use a new event ID for a different signal. Reusing one tests duplicate rejection.")
    except Exception as exc:
        st.error(f"Processing halted safely: {type(exc).__name__}: {exc}")

st.divider()
st.subheader("Open synthetic positions")
st.dataframe(journal.positions(), use_container_width=True)
st.subheader("Source/event journal")
recent = journal.recent_events()
for r in recent:
    r["source_text"] = r["source_text"][:160]
    if r["decision_json"]:
        r["decision_json"] = json.loads(r["decision_json"])["reasons"]
    if r["order_json"]:
        r["order_json"] = json.loads(r["order_json"])["order_id"]
st.dataframe(recent, use_container_width=True)
