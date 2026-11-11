"""Offline, callback-driven readiness demonstration. A SIMULATION, NON-AUTHORITATIVE.

Run it with ``python -m tradingbot.paper_readiness.callback_demo``. It takes no arguments: the package
admits only pure standard-library imports, so there is no ``sys`` or ``argparse`` here.

Each scenario feeds invented primitive values for documented IBKR callbacks into the accepted
callback normalizers, one per readiness stream: ``IbkrCallbackNormalizer`` for contract details and
the quote snapshot, ``AccountSummaryCallbacks`` for the account summary (its broker request id is
kept) and ``PositionSnapshotCallbacks`` for positions (one local snapshot; its id is local, never a
broker request id). The resulting ``ResponseEnvelope`` records go through the unchanged
``FakeReadOnlyTransport`` and readiness runner, which produce the usual NON_AUTHORITATIVE report
with ``trading_permission: false``. No envelope is preassembled. No installed SDK, broker
connection, account or network is involved, and nothing here authorizes paper readiness or
trading.

Each run also composes one ``LifecycleAdapter`` and its ``RetirementLedger`` over invented SDK-free
doubles. Only the quote-snapshot callbacks pass through it: a reader double queues each one, and
only the adapter's consumer delivers it to the quote normalizer. The adapter then retires the
connection and observes the reader, socket and consumer separately. The contract-details,
account-summary and position callbacks are passed to their normalizers directly, so all three are
reported as outside the lifecycle model, with no lifecycle, retirement or cancellation coverage
claimed. A completed lifecycle is local only: broker-side retirement stays not established and
PAPER-READ-1 stays not assessed.

The quote reader double's buffer is the accepted ``FrameAccumulator``, fed fixed invented opaque
byte chunks. Normal processing hands each complete frame off to the reader double, which holds it
unprocessed; no payload is decoded, and nothing claims the bytes are SDK messages or carry the
quote callbacks. When the adapter joins the reader, it only ends input with ``finish()``: nothing is
drained or discarded. The adapter's reader observation reads the accumulator's retained byte count.
Refusals and untaken frames, which the lifecycle model cannot express, appear only in the unchanged
framing report. The local-components aggregate requires both reports and grants nothing.

Every fixture value is invented for this demonstration only, under one fixed ``FakeClock``.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Self

from tradingbot.models import AssetClass, Instrument
from tradingbot.paper_readiness.account_position_callbacks import (
    AccountSummaryCallbacks,
    PositionSnapshotCallbacks,
)
from tradingbot.paper_readiness.diagnostics import ReadinessOutcome, ReadinessReport
from tradingbot.paper_readiness.ibkr_callbacks import CallbackRequest, IbkrCallbackNormalizer
from tradingbot.paper_readiness.observations import (
    ApiMessage,
    ReadinessExpectation,
    SessionAttestation,
    TargetContract,
)
from tradingbot.paper_readiness.read_framing import (
    HEADER_BYTES,
    Frame,
    FrameAccumulator,
    FramingOutcome,
    FramingReport,
)
from tradingbot.paper_readiness.read_retirement import CallbackKind, ReadSlot
from tradingbot.paper_readiness.runner import REQUEST_IDS, render_text, run_readiness_check
from tradingbot.paper_readiness.sdk_lifecycle import AdapterReport, DirectCallback, LifecycleAdapter
from tradingbot.paper_readiness.transport import FakeClock, FakeReadOnlyTransport, FakeScript

__all__ = [
    "ACCOUNT_POSITION_FAILURES",
    "DEMO_CLOCK",
    "DEMO_EXPECTATION",
    "FRAMING_NOTICE",
    "LIFECYCLE_NOTICE",
    "READER_FRAMING_CASES",
    "SCENARIOS",
    "SIMULATION_NOTICE",
    "STREAM_SOURCES",
    "UNKNOWN_CLEANUP",
    "DemoResult",
    "DemoScenario",
    "main",
    "render_demo_json",
    "render_demo_text",
    "run_demo",
]

SIMULATION_NOTICE = (
    "SIMULATION: all four readiness streams come from INVENTED callback values through the "
    "accepted offline normalizers: IbkrCallbackNormalizer (contract details, quote snapshot), "
    "AccountSummaryCallbacks and PositionSnapshotCallbacks. No envelope is preassembled. No SDK, "
    "broker, network or account is involved. Not paper-readiness authorization."
)
# Where each stream's envelopes come from, and whether the local lifecycle adapter covers it.
STREAM_SOURCES = {
    "contract_details": "invented callbacks via IbkrCallbackNormalizer, called directly; outside "
                        "the lifecycle model",
    "quote_snapshot": "invented callbacks delivered by the LifecycleAdapter consumer to "
                      "IbkrCallbackNormalizer; local lifecycle only",
    "account_summary": "invented callbacks via AccountSummaryCallbacks, called directly; broker "
                       "request id kept; outside the lifecycle model",
    "positions": "invented callbacks via PositionSnapshotCallbacks, called directly; one local "
                 "snapshot whose id is local, never a broker request id; outside the lifecycle "
                 "model",
}
LIFECYCLE_NOTICE = (
    "LOCAL LIFECYCLE, SIMULATED: only the quote-snapshot callbacks pass through the SDK-free "
    "LifecycleAdapter and its RetirementLedger, over invented doubles. The contract-details, "
    "account-summary and position callbacks go to their normalizers directly and are outside the "
    "lifecycle model; no lifecycle, retirement or cancellation coverage is claimed for them. A "
    "completed lifecycle is local only: broker-side retirement is not established, PAPER-READ-1 is "
    "not assessed and trading_permission is False."
)
FRAMING_NOTICE = (
    "READER FRAMING, SIMULATED: the quote reader double buffers fixed INVENTED opaque byte chunks "
    "in the accepted FrameAccumulator. Payloads are never decoded and are not SDK messages or the "
    "quote callbacks. Its retained byte count feeds the lifecycle's reader observation; refusals "
    "and untaken frames appear only in the framing report. Local completion requires both reports. "
    "Handed-off frames are not processed or retired downstream. Nothing here is readiness, "
    "PAPER-READ-1, broker-side retirement or trading permission."
)
# The one stream whose callbacks pass through the lifecycle adapter, and why the others do not.
_LIFECYCLE_STREAMS = ("quote_snapshot",)
_DIRECT = "callbacks go to the normalizer directly, not through the lifecycle adapter"
_OUTSIDE_LIFECYCLE = {
    "contract_details": f"{_DIRECT}; the ledger has no read slot for it",
    "account_summary": f"{_DIRECT}; no lifecycle or retirement coverage is claimed",
    "positions": f"{_DIRECT}; no lifecycle or retirement coverage is claimed",
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
_ARID = REQUEST_IDS["request_account_summary"]  # the account-summary broker request id
_PSID = REQUEST_IDS["request_positions"]  # a local snapshot id; positions carry no broker id

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


def _position_contract(con_id: int, symbol: str) -> _Values:
    return _Values(conId=con_id, symbol=symbol, secType="STK", currency="USD")


def _length_prefixed(payload: bytes) -> bytes:
    """Fixture bytes in the accumulator's frame shape; the payload is an opaque control."""
    return len(payload).to_bytes(HEADER_BYTES, "big") + payload


# Three invented opaque frames of 29, 4 (zero-length) and 29 bytes. They are not SDK messages and
# are unrelated to the quote callbacks. Headers cross chunk boundaries at bytes 2 and 31 and the
# first payload at byte 20, so frames complete in the third and fourth chunks.
_OPAQUE = (_length_prefixed(b"invented opaque control A") + _length_prefixed(b"")
           + _length_prefixed(b"invented opaque control C"))
_COMPLETE_BYTES = (_OPAQUE[:2], _OPAQUE[2:20], _OPAQUE[20:31], _OPAQUE[31:])
_PARTIAL_HEADER = b"\x00\x00"  # two of a further frame's four header bytes
_PARTIAL_PAYLOAD = _length_prefixed(b"invented opaque control D")[:10]  # header plus six bytes
_OVERSIZED_HEADER = (2**32 - 1).to_bytes(HEADER_BYTES, "big")  # beyond the default payload limit


# ----------------------------------------------------------------------------- callback sequences

def _summary_rows(n: AccountSummaryCallbacks, *, buying_power_currency: str = "USD") -> None:
    """One invented account, every row on the account-summary request id."""
    n.accountSummary(_ARID, _ACCOUNT, "AccountType", "INVENTED-DEMO", "")  # non-monetary
    n.accountSummary(_ARID, _ACCOUNT, "NetLiquidation", "250000.00", "USD")
    n.accountSummary(_ARID, _ACCOUNT, "TotalCashValue", "250000.00", "USD")
    n.accountSummary(_ARID, _ACCOUNT, "BuyingPower", "500000.00", buying_power_currency)


def _summary_callbacks(n: AccountSummaryCallbacks) -> None:
    _summary_rows(n)
    n.accountSummaryEnd(_ARID)


def _summary_with_monetary_currency_missing(n: AccountSummaryCallbacks) -> None:
    """PR #35 review P2: a required monetary row without its currency; one row cannot vouch for
    another, so the summary's currency stays missing."""
    _summary_rows(n, buying_power_currency="")
    n.accountSummaryEnd(_ARID)


def _position_rows(n: PositionSnapshotCallbacks) -> None:
    """A fractional negative and a fractional positive quantity, kept exactly by the normalizer."""
    n.position(_ACCOUNT, _position_contract(700011, "DEMOPOSA"), Decimal("-12.5"), 101.25)
    n.position(_ACCOUNT, _position_contract(700012, "DEMOPOSB"), Decimal("300.25"), 55.0)


def _position_callbacks(n: PositionSnapshotCallbacks) -> None:
    _position_rows(n)
    n.positionEnd()


def _malformed_position_then_repeated_end(n: PositionSnapshotCallbacks) -> None:
    """PR #35 review P2: an oversized quantity is malformed and latches, so neither END nor a
    repeated END can present the lost row as a complete snapshot."""
    _position_rows(n)
    n.position(_ACCOUNT, _position_contract(700013, "DEMOPOSC"), Decimal("1E+5000"), 1.0)
    n.positionEnd()
    n.positionEnd()


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
    # The invented account-summary and position callbacks, passed to their normalizers directly.
    summary_feed: object = _summary_callbacks
    positions_feed: object = _position_callbacks
    # The fixed opaque chunks the quote reader double buffers, and whether it hands off the frames
    # that its final chunk completes.
    reader_bytes: tuple[bytes, ...] = _COMPLETE_BYTES
    takes_final_frames: bool = True
    expected_framing_outcome: FramingOutcome = FramingOutcome.COMPLETE


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
# The CLI's two account and position failure cases, also kept out of SCENARIOS. Contract and quote
# callbacks are the control's; each case changes one accepted-normalizer stream only.
ACCOUNT_POSITION_FAILURES = (
    DemoScenario("summary_monetary_currency_missing",
                 "Control callbacks, but BuyingPower arrives without its currency "
                 "(PR #35 review P2).",
                 ReadinessOutcome.NOT_READY, _control,
                 summary_feed=_summary_with_monetary_currency_missing),
    DemoScenario("malformed_position_then_repeated_end",
                 "Control callbacks, then an oversized position quantity and positionEnd twice "
                 "(PR #35 review P2).",
                 ReadinessOutcome.NOT_READY, _control,
                 positions_feed=_malformed_position_then_repeated_end),
)
# The CLI's four reader-framing cases, also kept out of SCENARIOS. Every callback is the control's,
# so readiness stays CONTROL_COMPLETE; only the reader double's fixed opaque bytes, or whether it
# hands off its final frames, differ from the control.
READER_FRAMING_CASES = (
    DemoScenario("framing_partial_header",
                 "Control callbacks; the reader keeps two header bytes of a further frame.",
                 ReadinessOutcome.CONTROL_COMPLETE, _control, expected_lifecycle_completed=False,
                 reader_bytes=(*_COMPLETE_BYTES, _PARTIAL_HEADER),
                 expected_framing_outcome=FramingOutcome.PARTIAL_HEADER),
    DemoScenario("framing_partial_payload",
                 "Control callbacks; the reader keeps a further frame's header and part payload.",
                 ReadinessOutcome.CONTROL_COMPLETE, _control, expected_lifecycle_completed=False,
                 reader_bytes=(*_COMPLETE_BYTES, _PARTIAL_PAYLOAD),
                 expected_framing_outcome=FramingOutcome.PARTIAL_PAYLOAD),
    DemoScenario("framing_declared_length_refused",
                 "Control callbacks; a further header declares an oversized length and is refused.",
                 ReadinessOutcome.CONTROL_COMPLETE, _control, expected_lifecycle_completed=False,
                 reader_bytes=(*_COMPLETE_BYTES, _OVERSIZED_HEADER),
                 expected_framing_outcome=FramingOutcome.REFUSED),
    # Nothing is retained, so the lifecycle's reader observation completes: the lifecycle model
    # cannot express untaken frames, and only the framing report and the aggregate show them.
    DemoScenario("framing_untaken_frames",
                 "Control callbacks; the reader never hands off the frames its last chunk "
                 "completes.",
                 ReadinessOutcome.CONTROL_COMPLETE, _control, takes_final_frames=False,
                 expected_framing_outcome=FramingOutcome.UNTAKEN_FRAMES),
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
    is queued whole; only the adapter's consumer delivers it to the quote normalizer.

    Its buffer is the accepted ``FrameAccumulator``, fed fixed opaque chunks that are unrelated to
    the callbacks. Normal processing hands complete frames off to ``handed_off``, where they stay
    unprocessed. Joining only ends input with ``finish()``; ``partial_bytes`` reports the
    accumulator's retained byte count."""

    def __init__(self, adapter: LifecycleAdapter, queue: _Queue, framing: FrameAccumulator) -> None:
        self._adapter = adapter
        self._queue = queue
        self._framing = framing
        self._alive = True
        self.handed_off: list[Frame] = []  # the downstream owner; nothing processes or retires them

    def receive_bytes(self, chunks: tuple[bytes, ...], *, takes_final_frames: bool) -> None:
        """Normal processing: buffer each chunk, then hand off every frame it completed."""
        for index, chunk in enumerate(chunks):
            self._framing.feed(chunk)
            if index < len(chunks) - 1 or takes_final_frames:
                while (frame := self._framing.take()) is not None:
                    self.handed_off.append(frame)

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
        self._framing.finish()  # ends input only: retained bytes and queued frames stay as they are
        self._alive = False

    def is_alive(self) -> bool:
        return self._alive

    def partial_bytes(self) -> int:
        return self._framing.report().bytes_retained  # observed, never set to force a status


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
                   quote: IbkrCallbackNormalizer) -> tuple[AdapterReport, FramingReport]:
    """Feed the scenario with its quote callbacks routed through one adapter, then shut it down.
    Returns the adapter's report and the reader buffer's framing report, both unmodified."""
    boundary = _QuoteBoundary(quote, scenario.socket_closure_known)
    adapter = LifecycleAdapter(boundary, _SingleThreadLock())
    framing = FrameAccumulator()
    boundary.adapter = adapter
    boundary.reader = _QuoteReader(adapter, boundary.queue, framing)
    adapter.start()
    adapter.open_read(ReadSlot.QUOTE_SNAPSHOT)
    boundary.reader.receive_bytes(scenario.reader_bytes,
                                  takes_final_frames=scenario.takes_final_frames)
    scenario.feed(contract, boundary.reader)  # contract callbacks reach their normalizer directly
    while adapter.consume_one():  # delivers each queued quote callback, END included, in order
        pass
    adapter.shutdown(join_timeout=0.0)  # the reader double's join returns at once
    return adapter.report(), framing.report()


# ----------------------------------------------------------------------------- running and rendering

@dataclass(frozen=True)
class DemoResult:
    scenario: DemoScenario
    report: ReadinessReport
    contract_completed: bool
    contract_issues: tuple[str, ...]
    quote_completed: bool
    quote_issues: tuple[str, ...]
    account_summary_completed: bool
    account_summary_issues: tuple[str, ...]
    positions_completed: bool
    positions_issues: tuple[str, ...]
    lifecycle: AdapterReport
    framing: FramingReport

    @property
    def matches_expectation(self) -> bool:
        """Every expected outcome occurred. Only that: never readiness, retirement or authority."""
        return (self.report.outcome == self.scenario.expected_outcome
                and self.lifecycle.completed == self.scenario.expected_lifecycle_completed
                and self.framing.outcome == self.scenario.expected_framing_outcome)

    @property
    def local_components_complete(self) -> bool:
        """Both local components finished cleanly. Local and NON_AUTHORITATIVE only: it never
        changes the readiness outcome and is never PAPER-READ-1, retirement or trading
        permission."""
        return self.lifecycle.completed and self.framing.clean


def _cli_only() -> tuple[DemoScenario, ...]:
    """The CLI's extra fixed cases, after the three SCENARIOS, in the order they are printed."""
    return (UNKNOWN_CLEANUP, *ACCOUNT_POSITION_FAILURES, *READER_FRAMING_CASES)


def _scenario(name: str) -> DemoScenario:
    return next((s for s in _cli_only() if s.name == name), None) or SCENARIOS[name]


def _codes(normalizer: object) -> tuple[str, ...]:
    return tuple(issue.code.value for issue in normalizer.issues)


def run_demo(name: str) -> DemoResult:
    """Run one named scenario from fresh normalizers, doubles and adapter under the fixed clock."""
    scenario = _scenario(name)
    clock = FakeClock(DEMO_CLOCK)
    contract = IbkrCallbackNormalizer(_CRID, CallbackRequest.CONTRACT_DETAILS)
    quote = IbkrCallbackNormalizer(_QRID, CallbackRequest.QUOTE_SNAPSHOT, con_id=_CON_ID, clock=clock)
    summary = AccountSummaryCallbacks(_ARID)
    positions = PositionSnapshotCallbacks(_PSID)
    lifecycle, framing = _run_lifecycle(scenario, contract, quote)
    scenario.summary_feed(summary)  # passed to the accepted normalizers directly
    scenario.positions_feed(positions)
    script = FakeScript(session=_session(), contract_details=contract.envelopes(),
                        quote_snapshot=quote.envelopes(), account_summary=summary.envelopes(),
                        positions=positions.envelopes())
    report = run_readiness_check(FakeReadOnlyTransport(script, clock), DEMO_EXPECTATION, clock=clock,
                                 label=f"callback-demo:{name}")
    return DemoResult(
        scenario=scenario, report=report,
        contract_completed=contract.completed, contract_issues=_codes(contract),
        quote_completed=quote.completed, quote_issues=_codes(quote),
        account_summary_completed=summary.completed, account_summary_issues=_codes(summary),
        positions_completed=positions.completed, positions_issues=_codes(positions),
        lifecycle=lifecycle, framing=framing,
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
            "account_summary": {"completed": result.account_summary_completed,
                                "issues": list(result.account_summary_issues)},
            "positions": {"completed": result.positions_completed,
                          "issues": list(result.positions_issues)},
        },
        "stream_sources": dict(STREAM_SOURCES),
        "local_lifecycle": {
            "modelled_streams": list(_LIFECYCLE_STREAMS),
            "outside_lifecycle_model": dict(_OUTSIDE_LIFECYCLE),
            "expected_completed": result.scenario.expected_lifecycle_completed,
            "completed": result.lifecycle.completed,
            "local_quiescence_established": result.lifecycle.ledger.local_quiescence_established,
        },
        "reader_framing": {
            "expected_outcome": result.scenario.expected_framing_outcome.value,
            "outcome": result.framing.outcome.value,
            "clean": result.framing.clean,
            "bytes_retained": result.framing.bytes_retained,
            "lifecycle_reader_partial_bytes": result.lifecycle.reader_partial_bytes,
            "downstream_retirement": result.framing.downstream_retirement,
        },
        "local_components": {
            "lifecycle_completed": result.lifecycle.completed,
            "framing_clean": result.framing.clean,
            "both_complete": result.local_components_complete,
            "authority": "NON_AUTHORITATIVE",
            "trading_permission": False,
            "paper_read_1": "NOT_ASSESSED",
        },
    }


def _framing_json(report: FramingReport) -> dict:
    """The accumulator's report field by field, enum members as values. Counts and codes only."""
    fields = asdict(report)
    fields["outcome"] = report.outcome.value
    fields["first_refusal"] = None if report.first_refusal is None else report.first_refusal.value
    fields["refusals"] = [[code.value, count] for code, count in report.refusals]
    return fields


def render_demo_json(results: list[DemoResult]) -> str:
    payload = {"notice": SIMULATION_NOTICE, "lifecycle_notice": LIFECYCLE_NOTICE,
               "framing_notice": FRAMING_NOTICE,
               "scenarios": [{**_summary(r), "report": r.report.model_dump(mode="json"),
                              "lifecycle_report": asdict(r.lifecycle),
                              "framing_report": _framing_json(r.framing)} for r in results]}
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def render_demo_text(results: list[DemoResult]) -> str:
    lines = [SIMULATION_NOTICE, LIFECYCLE_NOTICE, FRAMING_NOTICE, ""]
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
            lines.append(f"      source: {STREAM_SOURCES[stream]}")
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
        framing = r.framing
        refusal = "none" if framing.first_refusal is None else framing.first_refusal.value
        lines.append(f"   reader framing: expected={r.scenario.expected_framing_outcome.value} "
                     f"outcome={framing.outcome.value} clean={framing.clean} "
                     f"retained={framing.bytes_retained} extracted={framing.frames_extracted} "
                     f"handed_off={framing.frames_handed_off} queued={framing.frames_queued} "
                     f"first_refusal={refusal} "
                     f"downstream_retirement={framing.downstream_retirement}")
        lines.append(f"   local components (NON_AUTHORITATIVE): "
                     f"lifecycle_completed={life.completed} framing_clean={framing.clean} "
                     f"both_complete={r.local_components_complete}")
        lines.append(render_text(r.report, summary=True).rstrip("\n"))
        lines.append("")
    matched = sum(r.matches_expectation for r in results)
    lines.append(f"{matched}/{len(results)} scenarios matched their expected readiness, lifecycle "
                 "and framing outcomes. A match means only that: NON_AUTHORITATIVE; "
                 "trading_permission=False; never PAPER-READ-1, broker-side retirement or trading "
                 "authority.")
    return "\n".join(lines) + "\n"


def main() -> int:
    """Print the three scenarios, then the CLI-only cases. Exit 0 only if each matches all of its
    expected outcomes; never a verdict on any real broker, account, retirement or readiness."""
    results = [run_demo(name) for name in (*SCENARIOS, *(s.name for s in _cli_only()))]
    print(render_demo_text(results), end="")
    return 0 if all(r.matches_expectation for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
