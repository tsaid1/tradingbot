"""Read Retirement: an OFFLINE view of local read-retirement bookkeeping. NON-AUTHORITATIVE.

Each of three fixed invented event sequences is replayed through a fresh
``tradingbot.paper_readiness.read_retirement.RetirementLedger`` every time it is selected, and the
page shows that ledger's own ``report()``: admission, queued and in-flight callbacks,
consumer, reader and socket states, reserved and settled attempts, fault codes and local
quiescence. Nothing is hardcoded or reinterpreted.

The events are simulated. This is local-only bookkeeping: broker-side retirement stays
NOT_ESTABLISHED_BY_INSPECTED_SOURCES, PAPER-READ-1 stays NOT_ASSESSED and trading_permission is
false. The only input is the choice of sequence. The page reads no file, environment variable,
clock or network, writes nothing, and runs no SDK, client or callback
(tests/test_read_retirement_page.py verifies).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass

import streamlit as st

from tradingbot.paper_readiness.read_retirement import (
    CallbackKind,
    Component,
    Outcome,
    ReadSlot,
    RetirementLedger,
    RetirementReport,
)

BANNER = (
    "SIMULATED EVENTS, LOCAL BOOKKEEPING ONLY: invented event sequences replayed through the "
    "RetirementLedger. Broker-side retirement NOT_ESTABLISHED_BY_INSPECTED_SOURCES · PAPER-READ-1 "
    "NOT_ASSESSED · trading_permission = False. No broker, SDK, client, network, account or order "
    "path exists on this page."
)
QUALIFIED = (
    "Local quiescence is established for this invented sequence only. It is local bookkeeping: not "
    "broker-side retirement, not PAPER-READ-1 and never broker readiness or trading approval."
)
NOT_QUIESCENT = (
    "Local quiescence is NOT established: local work is unresolved, failed or unknown (listed "
    "below). Nothing here is broker readiness or trading approval."
)
# The ledger operations a step may name. Each takes fixed enum values only.
OPERATIONS = frozenset({
    "open_read", "callback_received", "callback_started", "callback_finished", "reserve_attempt",
    "settle_attempt", "reserve_disconnect", "settle_disconnect", "close_admission",
    "component_stopped", "seal",
})


@dataclass(frozen=True)
class Sequence:
    description: str
    steps: tuple[tuple, ...]


def _drained(slot: ReadSlot, *kinds: CallbackKind) -> tuple[tuple, ...]:
    """Each callback is received, then taken and finished by the consumer."""
    steps: list[tuple] = []
    for kind in kinds:
        steps += [("callback_received", slot, kind), ("callback_started",), ("callback_finished",)]
    return tuple(steps)


_AS, _POS = ReadSlot.ACCOUNT_SUMMARY, ReadSlot.POSITIONS
_DATA, _END = CallbackKind.DATA, CallbackKind.END
_STOPPED_ALL = tuple(("component_stopped", c, Outcome.STOPPED) for c in Component)

SEQUENCES = {
    "valid_local_cessation": Sequence(
        "Both continuing reads drain to END, each returned retirement attempt settles, admission "
        "closes, consumer, reader and socket stop, the disconnect returns, then the ledger seals.",
        (("open_read", _AS), ("open_read", _POS))
        + _drained(_AS, _DATA, _END) + _drained(_POS, _DATA, _END)
        + (("reserve_attempt", _AS), ("settle_attempt", _AS, Outcome.RETURNED),
           ("reserve_attempt", _POS), ("settle_attempt", _POS, Outcome.RETURNED),
           ("close_admission",))
        + _STOPPED_ALL
        + (("reserve_disconnect",), ("settle_disconnect", Outcome.RETURNED), ("seal",)),
    ),
    "reader_stopped_with_outstanding_work": Sequence(
        "The reader stops while the consumer still has a callback in flight and two queued; "
        "admission never closes. The ledger then seals.",
        (("open_read", _AS), ("callback_received", _AS, _DATA), ("callback_received", _AS, _DATA),
         ("callback_received", _AS, _END), ("callback_started",),
         ("component_stopped", Component.READER, Outcome.STOPPED), ("seal",)),
    ),
    "failed_or_unknown_attempt": Sequence(
        "Both reads drain to END. The account-summary attempt settles FAILED; the positions "
        "attempt is reserved, a late positions update is dispatched, and it never settles before "
        "the seal.",
        (("open_read", _AS), ("open_read", _POS))
        + _drained(_AS, _DATA, _END) + _drained(_POS, _DATA, _END)
        + (("reserve_attempt", _AS), ("settle_attempt", _AS, Outcome.FAILED),
           ("reserve_attempt", _POS))
        + _drained(_POS, _DATA)
        + (("close_admission",),)
        + _STOPPED_ALL
        + (("reserve_disconnect",), ("settle_disconnect", Outcome.RETURNED), ("seal",)),
    ),
}


def replay(steps: tuple[tuple, ...]) -> RetirementReport:
    """A fresh ledger per call, so nothing accumulates between selections."""
    ledger = RetirementLedger()
    for operation, *arguments in steps:
        if operation not in OPERATIONS:
            raise ValueError("unknown ledger operation")
        getattr(ledger, operation)(*arguments)
    return ledger.report()


def step_text(step: tuple) -> str:
    operation, *arguments = step
    return f"{operation}({', '.join(argument.value for argument in arguments)})"


def _state(value: str, settled: str) -> str:
    return value if value == settled else f"**{value}** (unresolved)"


st.set_page_config(page_title="Read Retirement (local)", page_icon="🧪", layout="wide")
st.title("Read Retirement (LOCAL BOOKKEEPING, SIMULATED)")
st.error(BANNER)

with st.sidebar:
    st.error("SIMULATED · local only · trading_permission = False")
    st.header("Invented event sequence")
    choice = st.radio("Sequence", list(SEQUENCES), key="read_retirement_sequence")
    st.caption(
        f"{len(SEQUENCES)} fixed invented sequences. Nothing can be typed, uploaded, pointed at a "
        "path or connected from this page."
    )

sequence = SEQUENCES[choice]
try:
    report = replay(sequence.steps)
except Exception as exc:  # noqa: BLE001 - never a traceback on the page
    st.error(f"Replay halted safely: {type(exc).__name__}. No report is shown; nothing was "
             "connected, written or authorized.")
    st.stop()

st.subheader("Sequence")
st.markdown(f"**`{choice}`** — {sequence.description}")
quiescent = report.local_quiescence_established
(st.warning if quiescent else st.error)(QUALIFIED if quiescent else NOT_QUIESCENT)

m1, m2, m3, m4 = st.columns(4)
m1.metric("Local quiescence established", "yes" if quiescent else "NO")
m2.metric("Admission", "closed" if report.admission_closed else "OPEN")
m3.metric("Queued callbacks", report.queued_callbacks)
m4.metric("In-flight callbacks", report.in_flight_callbacks)
st.markdown(
    f"authority **{report.authority}** · **trading_permission = {report.trading_permission}** · "
    f"PAPER-READ-1 **{report.paper_read_1}** · broker-side retirement "
    f"**{report.broker_side_retirement}** · snapshot retirement {report.snapshot_retirement} · "
    f"sealed {report.sealed}"
)

st.subheader("Resources")
st.markdown("\n".join([
    "| Resource | Ledger state |",
    "|---|---|",
    f"| Consumer | {_state(report.consumer, 'STOPPED')} |",
    f"| Reader | {_state(report.reader, 'STOPPED')} |",
    f"| Socket | {_state(report.socket, 'STOPPED')} |",
    f"| Disconnect attempt | {_state(report.disconnect_attempt, 'RETURNED')} |",
]))

st.subheader("Read slots and retirement attempts")
st.markdown("\n".join(
    ["| Slot | END seen | Errored | Read complete | Data callbacks | Retirement attempt |",
     "|---|---|---|---|---|---|"]
    + [f"| {s.slot} | {s.end_marker_seen} | {s.errored} | {s.read_complete} | {s.data_callbacks} "
       f"| {_state(s.retirement_attempt, 'RETURNED')} |" for s in report.slots]
))

st.subheader("Faults")
if report.faults:
    st.error("Fault codes: " + ", ".join(f"`{code}`" for code in report.faults)
             + f" · dropped beyond the bound: {report.faults_dropped}")
else:
    st.info(f"No fault code recorded · dropped beyond the bound: {report.faults_dropped}")

with st.expander("Invented event sequence"):
    st.markdown("\n".join(f"{n}. `{step_text(step)}`" for n, step in
                          enumerate(sequence.steps, start=1)))
with st.expander("Ledger report (RetirementLedger.report(), unmodified)"):
    st.code(json.dumps(asdict(report), indent=2), language="json")

st.divider()
st.caption(
    "This page has no control that connects, orders, cancels, replaces, resets, changes settings "
    "or handles credentials, and it accepts no free-form input. A locally quiescent sequence is "
    "never broker readiness or trading approval; docs/READ_ONLY_RETIREMENT.md has the model."
)
