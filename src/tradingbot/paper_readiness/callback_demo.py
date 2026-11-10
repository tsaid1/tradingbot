"""Offline, callback-driven readiness demonstration. A SIMULATION, NON-AUTHORITATIVE.

Run it with ``python -m tradingbot.paper_readiness.callback_demo``. It takes no arguments: the package
admits only pure standard-library imports, so there is no ``sys`` or ``argparse`` here.

Each scenario feeds invented primitive values for documented IBKR callbacks into the merged
``IbkrCallbackNormalizer`` for the contract-details and quote-snapshot requests. The resulting
``ResponseEnvelope`` records go through the unchanged ``FakeReadOnlyTransport`` and readiness runner,
which produce the usual NON_AUTHORITATIVE report with ``trading_permission: false``.

The account summary and positions are **preassembled control envelopes**, invented here. They are not
produced from callbacks. No account or position callback adapter, installed SDK, broker connection or
network is involved, and nothing here authorizes paper readiness or trading.

Each run also composes one ``LifecycleAdapter`` and its ``RetirementLedger`` over invented SDK-free
doubles. Only the quote-snapshot callbacks pass through it: a reader double queues each one, and
only the adapter's consumer delivers it to the quote normalizer. The adapter then retires the
connection and observes the reader, socket and consumer separately. Contract-details callbacks have
no lifecycle read slot and reach their normalizer directly, and the account summary and positions
are not callbacks, so all three are reported as outside the lifecycle model. A completed lifecycle
is local only: broker-side retirement stays not established and PAPER-READ-1 stays not assessed.

Every fixture value is invented for this demonstration only, under one fixed ``FakeClock``.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Self

from tradingbot.models import AssetClass, Instrument
from tradingbot.paper_readiness.diagnostics import ReadinessOutcome, ReadinessReport
from tradingbot.paper_readiness.ibkr_callbacks import CallbackRequest, IbkrCallbackNormalizer
from tradingbot.paper_readiness.observations import (
    AccountSummaryObservation,
    ApiMessage,
    ReadinessExpectation,
    ResponseEnvelope,
    ResponseKind,
    SessionAttestation,
    TargetContract,
)
from tradingbot.paper_readiness.read_retirement import CallbackKind, ReadSlot
from tradingbot.paper_readiness.runner import REQUEST_IDS, render_text, run_readiness_check
from tradingbot.paper_readiness.sdk_lifecycle import AdapterReport, DirectCallback, LifecycleAdapter
from tradingbot.paper_readiness.transport import FakeClock, FakeReadOnlyTransport, FakeScript

__all__ = [
    "DEMO_CLOCK",
    "DEMO_EXPECTATION",
    "LIFECYCLE_NOTICE",
    "PREASSEMBLED_STREAMS",
    "SCENARIOS",
    "SIMULATION_NOTICE",
    "UNKNOWN_CLEANUP",
    "DemoResult",
    "DemoScenario",
    "main",
    "render_demo_json",
    "render_demo_text",
    "run_demo",
]

SIMULATION_NOTICE = (
    "SIMULATION: contract and quote envelopes come from invented callback values through "
    "IbkrCallbackNormalizer; account summary and positions are PREASSEMBLED CONTROL envelopes, not "
    "callbacks. No SDK, broker, network or account is involved. Not paper-readiness authorization."
)
# The two streams this demonstration supplies as preassembled envelopes instead of callbacks.
PREASSEMBLED_STREAMS = ("account_summary", "positions")
LIFECYCLE_NOTICE = (
    "LOCAL LIFECYCLE, SIMULATED: only the quote-snapshot callbacks pass through the SDK-free "
    "LifecycleAdapter and its RetirementLedger, over invented doubles. Contract details (direct "
    "callbacks, no lifecycle read slot), account summary and positions (preassembled controls) are "
    "outside the lifecycle model. A completed lifecycle is local only: broker-side retirement is "
    "not established, PAPER-READ-1 is not assessed and trading_permission is False."
)
# The one stream whose callbacks pass through the lifecycle adapter, and why the others do not.
_LIFECYCLE_STREAMS = ("quote_snapshot",)
_OUTSIDE_LIFECYCLE = {
    "contract_details": "callbacks go to the normalizer directly; no lifecycle read slot exists",
    "account_summary": "preassembled control envelopes, not callbacks",
    "positions": "preassembled control envelopes, not callbacks",
}

# ----------------------------------------------------------------------------- invented fixtures

# 2026-01-14 16:00 UTC is 11:00 America/New_York (EST), inside the invented regular session below.
DEMO_CLOCK = datetime(2026, 1, 14, 16, 0, tzinfo=UTC)
_ACCOUNT = "SYNTH-DEMO0001"
_CON_ID = 700001
_SYMBOL = "DEMOETF"
_PRIMARY = "DEMO-LISTING"
_ZONE = "America/New_York"
_LIQUID_HOURS = "20260114:0930-20260114:1600;20260115:0930-20260115:1600"
_TRADING_HOURS = "20260114:0400-20260114:2000;20260115:0400-20260115:2000"
_LAST_TRADE = str(int((DEMO_CLOCK - timedelta(seconds=8)).timestamp()))
_CRID = REQUEST_IDS["request_contract_details"]
_QRID = REQUEST_IDS["request_quote_snapshot"]

DEMO_EXPECTATION = ReadinessExpectation(
    account_id=_ACCOUNT,
    target=TargetContract(instrument=Instrument(asset_class=AssetClass.ETF, symbol=_SYMBOL,
                                                exchange="SMART"),
                          primary_exchange=_PRIMARY),
)


class _Values:
    """A plain object shaped like a documented SDK value, holding only the given attributes."""

    def __init__(self, **values: object) -> None:
        self.__dict__.update(values)


class _NoAttrib:
    """Stands in for the documented TickAttrib argument, which the normalizer does not interpret."""


def _details() -> _Values:
    contract = _Values(conId=_CON_ID, symbol=_SYMBOL, localSymbol=_SYMBOL, secType="STK",
                       currency="USD", exchange="SMART", primaryExchange=_PRIMARY)
    return _Values(contract=contract, longName="INVENTED DEMO ETF", tradingHours=_TRADING_HOURS,
                   liquidHours=_LIQUID_HOURS, timeZoneId=_ZONE, minTick=0.01)


def _session() -> SessionAttestation:
    """The fake transport's own invented attestation. It is not evidence about any real session."""
    return SessionAttestation(
        synthetic=True, connected=True, environment_claim="PAPER", managed_accounts=[_ACCOUNT],
        read_only_api_attested=True, server_version=178,
        connection_time=DEMO_CLOCK - timedelta(seconds=120),
        messages=[ApiMessage(code=2104), ApiMessage(code=2158)],
    )


def _preassembled_account_streams() -> dict[str, list[ResponseEnvelope]]:
    """PREASSEMBLED CONTROL envelopes for the two reads this demonstration does not drive by callback."""
    summary = AccountSummaryObservation(
        account_id=_ACCOUNT, account_type="INVENTED-DEMO", currency="USD",
        values={"NetLiquidation": "250000.00", "TotalCashValue": "250000.00",
                "BuyingPower": "500000.00"},
    )
    account_rid = REQUEST_IDS["request_account_summary"]
    positions_rid = REQUEST_IDS["request_positions"]
    return {
        "account_summary": [
            ResponseEnvelope(request_id=account_rid, sequence=1, kind=ResponseKind.ACCOUNT_SUMMARY,
                             account_summary=summary),
            ResponseEnvelope(request_id=account_rid, sequence=2, kind=ResponseKind.ACCOUNT_SUMMARY_END),
        ],
        "positions": [ResponseEnvelope(request_id=positions_rid, sequence=1,
                                       kind=ResponseKind.POSITION_END)],
    }


# ----------------------------------------------------------------------------- callback sequences

def _contract_callbacks(n: IbkrCallbackNormalizer) -> None:
    n.contractDetails(_CRID, _details())
    n.contractDetailsEnd(_CRID)


def _live_ticks(n: _QuoteReader) -> None:
    attrib = _NoAttrib()
    n.marketDataType(_QRID, 1)
    n.tickPrice(_QRID, 1, 48.25, attrib)
    n.tickSize(_QRID, 0, Decimal(700))
    n.tickPrice(_QRID, 2, 48.27, attrib)
    n.tickSize(_QRID, 3, Decimal(900))
    n.tickPrice(_QRID, 4, 48.26, attrib)
    n.tickString(_QRID, 45, _LAST_TRADE)


def _control(contract: IbkrCallbackNormalizer, quote: _QuoteReader) -> None:
    _contract_callbacks(contract)
    _live_ticks(quote)
    quote.tickSnapshotEnd(_QRID)


def _delayed_size_mixed_with_live(contract: IbkrCallbackNormalizer, quote: _QuoteReader) -> None:
    """PR #25 review P2: delayed bid size (tick 69) among otherwise live ticks."""
    _contract_callbacks(contract)
    _live_ticks(quote)
    quote.tickSize(_QRID, 69, Decimal(700))
    quote.tickSnapshotEnd(_QRID)


def _issue_with_repeated_end(contract: IbkrCallbackNormalizer, quote: _QuoteReader) -> None:
    """PR #25 review P1: a valid row, an unsupported callback, then contractDetailsEnd twice."""
    contract.contractDetails(_CRID, _details())
    contract.unsupported_callback("connectionClosed")
    contract.contractDetailsEnd(_CRID)
    contract.contractDetailsEnd(_CRID)
    _live_ticks(quote)
    quote.tickSnapshotEnd(_QRID)


@dataclass(frozen=True)
class DemoScenario:
    name: str
    description: str
    expected_outcome: ReadinessOutcome
    feed: object
    # Whether the invented connection double can report its socket closed after the disconnect.
    socket_closure_known: bool = True
    expected_lifecycle_completed: bool = True


SCENARIOS = {
    s.name: s for s in (
        DemoScenario("callback_control", "Documented contract and live snapshot callbacks.",
                     ReadinessOutcome.CONTROL_COMPLETE, _control),
        DemoScenario("delayed_size_mixed_with_live",
                     "Delayed bid size tick 69 mixed into live ticks (PR #25 review P2).",
                     ReadinessOutcome.NOT_READY, _delayed_size_mixed_with_live),
        DemoScenario("issue_with_repeated_end",
                     "Unsupported callback, then contractDetailsEnd twice (PR #25 review P1).",
                     ReadinessOutcome.NOT_READY, _issue_with_repeated_end),
    )
}

# The CLI's fourth scenario. It stays out of SCENARIOS, so the dashboard's three-scenario selector
# is unchanged. Same callbacks as the control: readiness is CONTROL_COMPLETE, but after the
# disconnect returns, the connection double cannot say whether its socket closed.
UNKNOWN_CLEANUP = DemoScenario(
    "control_with_unknown_socket_closure",
    "Control callbacks; after the returned disconnect the socket closure is unknown.",
    ReadinessOutcome.CONTROL_COMPLETE, _control,
    socket_closure_known=False, expected_lifecycle_completed=False,
)


# ----------------------------------------------------------------------------- lifecycle doubles

_QueueItem = tuple[ReadSlot, CallbackKind]
_Callback = tuple[str, tuple]  # a quote-normalizer method name and its invented arguments


class _SingleThreadLock:
    """The demonstration runs on one thread, so this lock only refuses re-entry, which is a bug."""

    def __init__(self) -> None:
        self._held = False

    def __enter__(self) -> Self:
        if self._held:
            raise RuntimeError("lock re-entered")
        self._held = True
        return self

    def __exit__(self, *exc: object) -> None:
        self._held = False


class _Connection:
    def __init__(self) -> None:
        self.connected = True
        self.closed: bool | None = False

    def is_connected(self) -> bool:
        return self.connected

    def socket_closed(self) -> bool | None:
        return self.closed  # None: closure unknown


class _Queue:
    """FIFO between the reader double and the adapter's consumer. Each item keeps its callback."""

    def __init__(self) -> None:
        self._items: list[tuple[_QueueItem, _Callback]] = []
        self.fetched: tuple[_QueueItem, _Callback] | None = None

    def put(self, item: _QueueItem, callback: _Callback) -> None:
        self._items.append((item, callback))

    def get_nowait(self) -> _QueueItem | None:
        if not self._items:
            return None
        self.fetched = self._items.pop(0)
        return self.fetched[0]

    def size(self) -> int:
        return len(self._items)


class _QuoteReader:
    """Stands in for the reader thread on the quote read. Each invented quote callback it receives
    is queued whole; only the adapter's consumer delivers it to the quote normalizer. It holds no
    partial message, and its invented input has ended by the time the adapter joins it."""

    def __init__(self, adapter: LifecycleAdapter, queue: _Queue) -> None:
        self._adapter = adapter
        self._queue = queue
        self._alive = True

    def _receive(self, kind: CallbackKind, name: str, *args: object) -> None:
        item = (ReadSlot.QUOTE_SNAPSHOT, kind)
        self._queue.put(item, (name, args))
        self._adapter.on_enqueued(*item)

    # Only the documented quote callbacks these scenarios use; no other read type is modelled here.
    def marketDataType(self, reqId: int, marketDataType: int) -> None:
        self._receive(CallbackKind.DATA, "marketDataType", reqId, marketDataType)

    def tickPrice(self, reqId: int, tickType: int, price: float, attrib: object) -> None:
        self._receive(CallbackKind.DATA, "tickPrice", reqId, tickType, price, attrib)

    def tickSize(self, reqId: int, tickType: int, size: object) -> None:
        self._receive(CallbackKind.DATA, "tickSize", reqId, tickType, size)

    def tickString(self, reqId: int, tickType: int, value: str) -> None:
        self._receive(CallbackKind.DATA, "tickString", reqId, tickType, value)

    def tickSnapshotEnd(self, reqId: int) -> None:
        self._receive(CallbackKind.END, "tickSnapshotEnd", reqId)

    def join(self, timeout: float) -> None:
        self._alive = False

    def is_alive(self) -> bool:
        return self._alive

    def partial_bytes(self) -> int:
        return 0


class _QuoteBoundary:
    """Invented SDK-free client double for one local connection carrying the quote read."""

    def __init__(self, quote: IbkrCallbackNormalizer, socket_closure_known: bool) -> None:
        self._quote = quote
        self._socket_closure_known = socket_closure_known
        self.adapter: LifecycleAdapter | None = None
        self.connection, self.queue = _Connection(), _Queue()
        self.reader: _QuoteReader | None = None

    def connect(self) -> _QuoteBoundary:
        return self  # the adapter retains .connection, .reader and .queue

    def stop_read(self, slot: ReadSlot) -> None:
        """Not reached here: the adapter makes no attempt for a snapshot whose END it delivered."""

    def disconnect(self) -> None:
        self.connection.connected = False
        self.connection.closed = True if self._socket_closure_known else None
        self.adapter.on_direct_callback(DirectCallback.CONNECTION_CLOSED)  # not evidence of closure

    def dispatch(self, slot: ReadSlot, kind: CallbackKind) -> None:
        fetched, self.queue.fetched = self.queue.fetched, None
        if fetched is None or fetched[0] != (slot, kind):
            raise RuntimeError("dispatched item is not the fetched one")
        name, args = fetched[1]
        getattr(self._quote, name)(*args)


def _run_lifecycle(scenario: DemoScenario, contract: IbkrCallbackNormalizer,
                   quote: IbkrCallbackNormalizer) -> AdapterReport:
    """Feed the scenario with its quote callbacks routed through one adapter, then shut it down."""
    boundary = _QuoteBoundary(quote, scenario.socket_closure_known)
    adapter = LifecycleAdapter(boundary, _SingleThreadLock())
    boundary.adapter = adapter
    boundary.reader = _QuoteReader(adapter, boundary.queue)
    adapter.start()
    adapter.open_read(ReadSlot.QUOTE_SNAPSHOT)
    scenario.feed(contract, boundary.reader)  # contract callbacks reach their normalizer directly
    while adapter.consume_one():  # delivers each queued quote callback, END included, in order
        pass
    adapter.shutdown(join_timeout=0.0)  # the reader double's join returns at once
    return adapter.report()


# ----------------------------------------------------------------------------- running and rendering

@dataclass(frozen=True)
class DemoResult:
    scenario: DemoScenario
    report: ReadinessReport
    contract_completed: bool
    contract_issues: tuple[str, ...]
    quote_completed: bool
    quote_issues: tuple[str, ...]
    lifecycle: AdapterReport

    @property
    def matches_expectation(self) -> bool:
        """Both expected outcomes occurred. Only that: never readiness, retirement or authority."""
        return (self.report.outcome == self.scenario.expected_outcome
                and self.lifecycle.completed == self.scenario.expected_lifecycle_completed)


def _scenario(name: str) -> DemoScenario:
    return UNKNOWN_CLEANUP if name == UNKNOWN_CLEANUP.name else SCENARIOS[name]


def run_demo(name: str) -> DemoResult:
    """Run one named scenario from fresh normalizers, doubles and adapter under the fixed clock."""
    scenario = _scenario(name)
    clock = FakeClock(DEMO_CLOCK)
    contract = IbkrCallbackNormalizer(_CRID, CallbackRequest.CONTRACT_DETAILS)
    quote = IbkrCallbackNormalizer(_QRID, CallbackRequest.QUOTE_SNAPSHOT, con_id=_CON_ID, clock=clock)
    lifecycle = _run_lifecycle(scenario, contract, quote)
    script = FakeScript(session=_session(), contract_details=contract.envelopes(),
                        quote_snapshot=quote.envelopes(), **_preassembled_account_streams())
    report = run_readiness_check(FakeReadOnlyTransport(script, clock), DEMO_EXPECTATION, clock=clock,
                                 label=f"callback-demo:{name}")
    return DemoResult(
        scenario=scenario, report=report,
        contract_completed=contract.completed, contract_issues=tuple(i.code.value for i in contract.issues),
        quote_completed=quote.completed, quote_issues=tuple(i.code.value for i in quote.issues),
        lifecycle=lifecycle,
    )


def _summary(result: DemoResult) -> dict:
    return {
        "scenario": result.scenario.name,
        "description": result.scenario.description,
        "expected_outcome": result.scenario.expected_outcome.value,
        "outcome": result.report.outcome.value,
        "matches_expectation": result.matches_expectation,
        "callback_ingestion": {
            "contract_details": {"completed": result.contract_completed,
                                 "issues": list(result.contract_issues)},
            "quote_snapshot": {"completed": result.quote_completed, "issues": list(result.quote_issues)},
        },
        "preassembled_control_streams": list(PREASSEMBLED_STREAMS),
        "local_lifecycle": {
            "modelled_streams": list(_LIFECYCLE_STREAMS),
            "outside_lifecycle_model": dict(_OUTSIDE_LIFECYCLE),
            "expected_completed": result.scenario.expected_lifecycle_completed,
            "completed": result.lifecycle.completed,
            "local_quiescence_established": result.lifecycle.ledger.local_quiescence_established,
        },
    }


def render_demo_json(results: list[DemoResult]) -> str:
    payload = {"notice": SIMULATION_NOTICE, "lifecycle_notice": LIFECYCLE_NOTICE,
               "scenarios": [{**_summary(r), "report": r.report.model_dump(mode="json"),
                              "lifecycle_report": asdict(r.lifecycle)} for r in results]}
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def render_demo_text(results: list[DemoResult]) -> str:
    lines = [SIMULATION_NOTICE, LIFECYCLE_NOTICE, ""]
    for r in results:
        s = _summary(r)
        ingestion = s["callback_ingestion"]
        life, ledger = r.lifecycle, r.lifecycle.ledger
        lines.append(f"== {r.scenario.name}: {r.scenario.description}")
        lines.append(f"   expected={s['expected_outcome']} outcome={s['outcome']} "
                     f"matches_expectation={s['matches_expectation']}")
        for stream, state in ingestion.items():
            issues = ", ".join(state["issues"]) or "none"
            lines.append(f"   {stream}: completed={state['completed']} issues={issues}")
        lines.append(f"   preassembled control streams: {', '.join(PREASSEMBLED_STREAMS)}")
        lines.append(f"   local lifecycle ({', '.join(_LIFECYCLE_STREAMS)} only): "
                     f"expected_completed={r.scenario.expected_lifecycle_completed} "
                     f"completed={life.completed} "
                     f"local_quiescence_established={ledger.local_quiescence_established}")
        lines.append(f"   observed: reader={ledger.reader} socket={ledger.socket} "
                     f"consumer={ledger.consumer} disconnect={ledger.disconnect_attempt} "
                     f"state={life.state} failures={', '.join(life.failures) or 'none'}")
        lines.append(f"   outside the lifecycle model: {', '.join(_OUTSIDE_LIFECYCLE)}; "
                     f"broker_side_retirement={ledger.broker_side_retirement} "
                     f"paper_read_1={life.paper_read_1}")
        lines.append(render_text(r.report, summary=True).rstrip("\n"))
        lines.append("")
    matched = sum(r.matches_expectation for r in results)
    lines.append(f"{matched}/{len(results)} scenarios matched their expected readiness and "
                 "lifecycle outcomes. A match means only that: NON_AUTHORITATIVE; "
                 "trading_permission=False; never PAPER-READ-1, broker-side retirement or trading "
                 "authority.")
    return "\n".join(lines) + "\n"


def main() -> int:
    """Print every scenario, the CLI-only counterpart last. Exit 0 only if each matches both of its
    expected outcomes; never a verdict on any real broker, account, retirement or readiness."""
    results = [run_demo(name) for name in (*SCENARIOS, UNKNOWN_CLEANUP.name)]
    print(render_demo_text(results), end="")
    return 0 if all(r.matches_expectation for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
