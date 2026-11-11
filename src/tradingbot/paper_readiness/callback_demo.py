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

Each run also composes one ``CallbackRouter``, with its ``LifecycleAdapter`` and ``RetirementLedger``,
over invented SDK-free doubles. The quote-snapshot, account-summary and position callbacks all pass
through it, and they travel in owned invented frames: each callback is one frame whose payload is a
whole invented key, not an SDK message. The accepted ``DecodeHandoff`` buffers the fragmented chunks in its ``FrameAccumulator``,
decodes each complete frame with a closed ``InventedDecoder`` into the ``ReadCallbackFacade``, and
settles the delivery through the router's own consumer before releasing the frame. The router then
retires the connection, with its invented stop hook returning for the continuing account-summary and
position reads (never an SDK cancellation), and observes the reader, socket and consumer separately.
Each of the three streams' completion and issue codes is read through ``CallbackRouter.stream_status()``.
The contract-details callbacks are passed to their normalizer directly, outside the lifecycle model,
with no lifecycle, retirement or cancellation coverage claimed. A completed lifecycle is local only: broker-side retirement stays not established and
PAPER-READ-1 stays not assessed.

The reader double's partial-byte observation reads the handoff's framing report. Refusals, untaken
frames and handoff failures appear in the unchanged framing and handoff reports. The local-components
aggregate requires the lifecycle, the framing, every handoff and every stream's own completion
together, and grants nothing. No
message-id, protobuf or wire-format selection is modelled.

Every fixture value is invented for this demonstration only, under one fixed ``FakeClock``.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Self

from tradingbot.models import AssetClass, Instrument
from tradingbot.paper_readiness.callback_router import CallbackRouter, RouterReport, StreamKind
from tradingbot.paper_readiness.decode_handoff import (
    DecodeHandoff,
    DecodeHandoffReport,
    Entry,
    Failure,
    InventedDecoder,
    Step,
    StepKind,
)
from tradingbot.paper_readiness.diagnostics import ReadinessOutcome, ReadinessReport
from tradingbot.paper_readiness.ibkr_callbacks import CallbackRequest, IbkrCallbackNormalizer
from tradingbot.paper_readiness.observations import (
    ApiMessage,
    ReadinessExpectation,
    SessionAttestation,
    TargetContract,
)
from tradingbot.paper_readiness.read_framing import HEADER_BYTES, FramingOutcome, FramingReport
from tradingbot.paper_readiness.read_retirement import ReadSlot
from tradingbot.paper_readiness.runner import REQUEST_IDS, render_text, run_readiness_check
from tradingbot.paper_readiness.sdk_lifecycle import AdapterReport
from tradingbot.paper_readiness.transport import FakeClock, FakeReadOnlyTransport, FakeScript

__all__ = [
    "ACCOUNT_POSITION_FAILURES",
    "DEMO_CLOCK",
    "DEMO_EXPECTATION",
    "FRAMING_NOTICE",
    "HANDOFF_FAILURE_CASES",
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
    "quote_snapshot": "invented callbacks carried by owned invented frames (not an SDK wire "
                      "format) through DecodeHandoff, a closed InventedDecoder and the "
                      "ReadCallbackFacade to the CallbackRouter, whose adapter consumer delivers "
                      "them to IbkrCallbackNormalizer; local lifecycle only",
    "account_summary": "invented callbacks carried by owned invented frames (not an SDK wire "
                       "format) through DecodeHandoff to the CallbackRouter, whose adapter consumer "
                       "delivers them to AccountSummaryCallbacks; broker-style synthetic request id "
                       "kept; local lifecycle only",
    "positions": "invented callbacks carried by owned invented frames (not an SDK wire format) "
                 "through DecodeHandoff to the CallbackRouter, whose adapter consumer delivers them "
                 "to PositionSnapshotCallbacks; one local snapshot window whose id is local, never a "
                 "broker request id; local lifecycle only",
}
LIFECYCLE_NOTICE = (
    "LOCAL LIFECYCLE, SIMULATED: the quote-snapshot, account-summary and position callbacks pass "
    "through the SDK-free CallbackRouter's LifecycleAdapter and its RetirementLedger, over invented "
    "doubles, after DecodeHandoff settles each owned frame. The continuing account-summary and position "
    "reads end with the invented client's returned stop hook, never an SDK cancellation. The "
    "contract-details callbacks go to their normalizer directly and are outside the lifecycle model; no "
    "lifecycle, retirement or cancellation coverage is claimed for them. A "
    "completed lifecycle is local only: broker-side retirement is not established, PAPER-READ-1 is "
    "not assessed and trading_permission is False."
)
FRAMING_NOTICE = (
    "READER FRAMING AND DECODE HANDOFF, SIMULATED: each read callback travels in one owned "
    "INVENTED frame. DecodeHandoff buffers the chunks in its FrameAccumulator, decodes each frame "
    "with a closed InventedDecoder that maps whole invented payloads to fixed callbacks (no SDK "
    "message format, message id or protobuf selection), and settles the delivery through the "
    "router's consumer before releasing the frame. Its retained byte count feeds the lifecycle's "
    "reader observation; refusals, untaken frames and handoff failures appear in the framing and "
    "handoff reports. Local completion requires the lifecycle, the framing, every handoff and every "
    "stream's own completion. "
    "Nothing here is readiness, PAPER-READ-1, broker-side retirement or trading permission."
)
# The read streams whose callbacks pass through the lifecycle adapter, and why contract details do not.
_LIFECYCLE_STREAMS = ("quote_snapshot", "account_summary", "positions")
_DIRECT = "callbacks go to the normalizer directly, not through the lifecycle adapter"
_OUTSIDE_LIFECYCLE = {
    "contract_details": f"{_DIRECT}; the ledger has no read slot for it",
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


# Each quote callback travels in one owned invented frame whose payload is a whole invented key, not
# an SDK message: the closed InventedDecoder maps it to that one callback. The frames' bytes arrive
# fragmented across fixed-size chunks, so headers and payloads cross chunk boundaries.
_DEMO_SERVER_VERSION = 1  # invented version provenance for the closed decoder; no wire meaning
_READ_FRAME_KEY = b"invented-demo-read-frame-%02d"
_CHUNK_BYTES = 13
# Trailing reader bytes for the framing cases, after every quote frame.
_PARTIAL_HEADER = b"\x00\x00"  # two of a further frame's four header bytes
_PARTIAL_PAYLOAD = _length_prefixed(b"invented trailing frame D")[:10]  # header plus six bytes
_OVERSIZED_HEADER = (2**32 - 1).to_bytes(HEADER_BYTES, "big")  # beyond the default payload limit
_UNTAKEN_FRAME = _length_prefixed(b"invented trailing frame E")  # complete, but never handed off


# ----------------------------------------------------------------------------- callback sequences

def _summary_rows(n: _ReadRecorder, *, buying_power_currency: str = "USD") -> None:
    """One invented account, every row on the account-summary request id."""
    n.accountSummary(_ARID, _ACCOUNT, "AccountType", "INVENTED-DEMO", "")  # non-monetary
    n.accountSummary(_ARID, _ACCOUNT, "NetLiquidation", "250000.00", "USD")
    n.accountSummary(_ARID, _ACCOUNT, "TotalCashValue", "250000.00", "USD")
    n.accountSummary(_ARID, _ACCOUNT, "BuyingPower", "500000.00", buying_power_currency)


def _summary_callbacks(n: _ReadRecorder) -> None:
    _summary_rows(n)
    n.accountSummaryEnd(_ARID)


def _summary_with_monetary_currency_missing(n: _ReadRecorder) -> None:
    """PR #35 review P2: a required monetary row without its currency; one row cannot vouch for
    another, so the summary's currency stays missing."""
    _summary_rows(n, buying_power_currency="")
    n.accountSummaryEnd(_ARID)


def _position_rows(n: _ReadRecorder) -> None:
    """A fractional negative and a fractional positive quantity, kept exactly by the normalizer."""
    n.position(_ACCOUNT, _position_contract(700011, "DEMOPOSA"), Decimal("-12.5"), 101.25)
    n.position(_ACCOUNT, _position_contract(700012, "DEMOPOSB"), Decimal("300.25"), 55.0)


def _position_callbacks(n: _ReadRecorder) -> None:
    _position_rows(n)
    n.positionEnd()


def _malformed_position_then_repeated_end(n: _ReadRecorder) -> None:
    """PR #35 review P2: an oversized quantity is malformed and latches, so neither END nor a
    repeated END can present the lost row as a complete snapshot."""
    _position_rows(n)
    n.position(_ACCOUNT, _position_contract(700013, "DEMOPOSC"), Decimal("1E+5000"), 1.0)
    n.positionEnd()
    n.positionEnd()


def _contract_callbacks(n: IbkrCallbackNormalizer) -> None:
    n.contractDetails(_CRID, _details())
    n.contractDetailsEnd(_CRID)


def _live_ticks(n: _ReadRecorder) -> None:
    attrib = _NoAttrib()
    n.marketDataType(_QRID, 1)
    n.tickPrice(_QRID, 1, 48.25, attrib)
    n.tickSize(_QRID, 0, Decimal(700))
    n.tickPrice(_QRID, 2, 48.27, attrib)
    n.tickSize(_QRID, 3, Decimal(900))
    n.tickPrice(_QRID, 4, 48.26, attrib)
    n.tickString(_QRID, 45, _LAST_TRADE)


def _control(contract: IbkrCallbackNormalizer, quote: _ReadRecorder) -> None:
    _contract_callbacks(contract)
    _live_ticks(quote)
    quote.tickSnapshotEnd(_QRID)


def _delayed_size_mixed_with_live(contract: IbkrCallbackNormalizer, quote: _ReadRecorder) -> None:
    """PR #25 review P2: delayed bid size (tick 69) among otherwise live ticks."""
    _contract_callbacks(contract)
    _live_ticks(quote)
    quote.tickSize(_QRID, 69, Decimal(700))
    quote.tickSnapshotEnd(_QRID)


def _issue_with_repeated_end(contract: IbkrCallbackNormalizer, quote: _ReadRecorder) -> None:
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
    # Reader bytes after every quote frame, and whether the frames the final chunk completes are
    # handed to decoding.
    trailing_bytes: tuple[bytes, ...] = ()
    takes_final_frames: bool = True
    expected_framing_outcome: FramingOutcome = FramingOutcome.COMPLETE
    # Whether the invented decoder raises right after delivering the snapshot END, before the
    # handoff settles that frame, and the handoff failure the scenario expects.
    decoder_raises_after_end: bool = False
    expected_handoff_failure: Failure | None = None


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
                 positions_feed=_malformed_position_then_repeated_end,
                 # The positions read is in the lifecycle model, so the repeated positionEnd is a
                 # post-END ledger fault and the lifecycle does not complete.
                 expected_lifecycle_completed=False),
)
# The CLI's four reader-framing cases, also kept out of SCENARIOS. Every callback is the control's and
# is delivered, so readiness stays CONTROL_COMPLETE; only trailing reader bytes after the quote
# frames, or whether the final chunk's frame is handed to decoding, differ from the control.
READER_FRAMING_CASES = (
    DemoScenario("framing_partial_header",
                 "Control callbacks; the reader keeps two header bytes of a further frame.",
                 ReadinessOutcome.CONTROL_COMPLETE, _control, expected_lifecycle_completed=False,
                 trailing_bytes=(_PARTIAL_HEADER,),
                 expected_framing_outcome=FramingOutcome.PARTIAL_HEADER),
    DemoScenario("framing_partial_payload",
                 "Control callbacks; the reader keeps a further frame's header and part payload.",
                 ReadinessOutcome.CONTROL_COMPLETE, _control, expected_lifecycle_completed=False,
                 trailing_bytes=(_PARTIAL_PAYLOAD,),
                 expected_framing_outcome=FramingOutcome.PARTIAL_PAYLOAD),
    DemoScenario("framing_declared_length_refused",
                 "Control callbacks; a further header declares an oversized length and is refused.",
                 ReadinessOutcome.CONTROL_COMPLETE, _control, expected_lifecycle_completed=False,
                 trailing_bytes=(_OVERSIZED_HEADER,),
                 expected_framing_outcome=FramingOutcome.REFUSED),
    # Nothing is retained, so the lifecycle's reader observation completes: the lifecycle model
    # cannot express untaken frames, and only the framing report and the aggregate show them.
    DemoScenario("framing_untaken_frames",
                 "Control callbacks; the reader never hands off the frames its last chunk "
                 "completes.",
                 ReadinessOutcome.CONTROL_COMPLETE, _control, trailing_bytes=(_UNTAKEN_FRAME,),
                 takes_final_frames=False, expected_framing_outcome=FramingOutcome.UNTAKEN_FRAMES),
)
# The CLI's decode-handoff failure case, also kept out of SCENARIOS. Every frame is complete, but the
# invented decoder raises right after delivering the snapshot END, so the handoff never settles that
# frame: the failure stays held and blocks local completion, even though shutdown later drains the
# admitted END and the stream itself completes.
HANDOFF_FAILURE_CASES = (
    DemoScenario("handoff_decoder_raised_after_snapshot_end",
                 "Control callbacks; the invented decoder raises after delivering tickSnapshotEnd, "
                 "before the handoff settles that frame.",
                 ReadinessOutcome.CONTROL_COMPLETE, _control, expected_lifecycle_completed=False,
                 decoder_raises_after_end=True, expected_handoff_failure=Failure.DECODER_RAISED),
)


# ----------------------------------------------------------------------------- lifecycle doubles


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


class _ReadRecorder:
    """Records a scenario's invented quote, account-summary and position callbacks, in order, as fixed
    decoder steps. Nothing is delivered here: each step becomes the decoding of one owned invented
    frame."""

    def __init__(self) -> None:
        self.steps: list[Step] = []

    def _add(self, entry: Entry, *args: object) -> None:
        self.steps.append(Step(StepKind.CALL, entry, args))

    # Only the documented read callbacks these scenarios use; contract details go directly.
    def marketDataType(self, reqId: int, marketDataType: int) -> None:
        self._add(Entry.MARKET_DATA_TYPE, reqId, marketDataType)

    def tickPrice(self, reqId: int, tickType: int, price: float, attrib: object) -> None:
        self._add(Entry.TICK_PRICE, reqId, tickType, price, attrib)

    def tickSize(self, reqId: int, tickType: int, size: object) -> None:
        self._add(Entry.TICK_SIZE, reqId, tickType, size)

    def tickString(self, reqId: int, tickType: int, value: str) -> None:
        self._add(Entry.TICK_STRING, reqId, tickType, value)

    def tickSnapshotEnd(self, reqId: int) -> None:
        self._add(Entry.TICK_SNAPSHOT_END, reqId)

    def accountSummary(self, reqId: int, account: str, tag: str, value: str, currency: str) -> None:
        self._add(Entry.ACCOUNT_SUMMARY, reqId, account, tag, value, currency)

    def accountSummaryEnd(self, reqId: int) -> None:
        self._add(Entry.ACCOUNT_SUMMARY_END, reqId)

    def position(self, account: str, contract: object, pos: object, avgCost: float) -> None:
        self._add(Entry.POSITION, account, contract, pos, avgCost)

    def positionEnd(self) -> None:
        self._add(Entry.POSITION_END)


class _HandoffReader:
    """Stands in for the reader thread on the quote read. Its buffer is the handoff's own accumulator,
    read only through the handoff's public report. Joining only ends input with ``finish()``."""

    def __init__(self) -> None:
        self.handoff: DecodeHandoff | None = None
        self._alive = True

    def join(self, timeout: float) -> None:
        self.handoff.finish()  # ends input only: retained bytes and queued frames stay as they are
        self._alive = False

    def is_alive(self) -> bool:
        return self._alive

    def partial_bytes(self) -> int:
        return self.handoff.report().framing.bytes_retained  # observed, never set to force a status


class _QuoteClient:
    """Invented SDK-free client double for one local connection carrying the quote read. The router
    supplies the queue and dispatch; this double supplies only connect, stop and disconnect."""

    def __init__(self, socket_closure_known: bool) -> None:
        self._socket_closure_known = socket_closure_known
        self.connection, self.reader = _Connection(), _HandoffReader()
        self.router: CallbackRouter | None = None

    def connect(self) -> _QuoteClient:
        return self  # the router keeps .connection and .reader

    def stop_read(self, slot: ReadSlot) -> None:
        """Reached only for a snapshot whose END was not delivered; it returns, which clears nothing."""

    def disconnect(self) -> None:
        self.connection.connected = False
        self.connection.closed = True if self._socket_closure_known else None
        self.router.connectionClosed()  # an observation only, never evidence of closure


def _read_frames(scenario: DemoScenario, steps: list[Step]) -> tuple[tuple[bytes, ...], dict]:
    """One owned invented frame per recorded callback, fragmented into fixed chunks, then the
    scenario's trailing bytes; and the closed decoder's script for each frame's whole payload."""
    keys = [_READ_FRAME_KEY % i for i in range(len(steps))]
    scripts = {key: (step,) for key, step in zip(keys, steps, strict=True)}
    if scenario.decoder_raises_after_end and steps and steps[-1].entry is Entry.TICK_SNAPSHOT_END:
        scripts[keys[-1]] = (steps[-1], Step(StepKind.RAISE))
    stream = b"".join(_length_prefixed(key) for key in keys)
    chunks = tuple(stream[i:i + _CHUNK_BYTES] for i in range(0, len(stream), _CHUNK_BYTES))
    return chunks + scenario.trailing_bytes, scripts


def _run_lifecycle(scenario: DemoScenario, contract: IbkrCallbackNormalizer,
                   clock: FakeClock) -> tuple[CallbackRouter, DecodeHandoff]:
    """Carry the scenario's account-summary, position and quote callbacks, in that order, in owned
    frames through the decode handoff to the router's consumer, then shut the router down. The quote
    frames come last, so the snapshot END is the final frame. Contract callbacks reach their normalizer
    directly."""
    summary, positions, quote = _ReadRecorder(), _ReadRecorder(), _ReadRecorder()
    scenario.summary_feed(summary)
    scenario.positions_feed(positions)
    scenario.feed(contract, quote)
    chunks, scripts = _read_frames(scenario, summary.steps + positions.steps + quote.steps)
    client = _QuoteClient(scenario.socket_closure_known)
    router = CallbackRouter(client, _SingleThreadLock(), clock)
    client.router = router
    router.start()
    router.register_account_summary(_ARID)  # its broker-style synthetic request id
    router.register_positions(_PSID)  # the single local positions window; its id is local
    router.register_quote(_QRID, _CON_ID)
    handoff = DecodeHandoff(router, InventedDecoder(_DEMO_SERVER_VERSION, scripts),
                            server_version=_DEMO_SERVER_VERSION)
    client.reader.handoff = handoff
    for index, chunk in enumerate(chunks):
        handoff.feed(chunk)
        if index < len(chunks) - 1 or scenario.takes_final_frames:
            handoff.process_available()  # each complete frame: decoded, delivered and settled in turn
    router.shutdown(join_timeout=0.0)  # the reader double's join returns at once
    return router, handoff


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
    handoff: DecodeHandoffReport
    router: RouterReport

    @property
    def matches_expectation(self) -> bool:
        """Every expected outcome occurred. Only that: never readiness, retirement or authority."""
        return (self.report.outcome == self.scenario.expected_outcome
                and self.lifecycle.completed == self.scenario.expected_lifecycle_completed
                and self.framing.outcome == self.scenario.expected_framing_outcome
                and self.handoff.failure == self.scenario.expected_handoff_failure)

    @property
    def streams_completed(self) -> bool:
        """Every stream's own normalizer completed: one stream's success never clears another's
        failure. Normalizer evidence only; the readiness runner still decides readiness."""
        return (self.contract_completed and self.quote_completed and self.account_summary_completed
                and self.positions_completed)

    @property
    def local_components_complete(self) -> bool:
        """The lifecycle, the framing, every decode handoff and every stream's normalizer finished
        cleanly, together. Local and NON_AUTHORITATIVE only: it never changes the readiness outcome and
        is never PAPER-READ-1, retirement or trading permission."""
        return (self.lifecycle.completed and self.framing.clean and self.handoff.completed
                and self.streams_completed)

    @property
    def aggregate_complete(self) -> bool:
        """Every local component and stream completed **and** the unchanged readiness runner returned
        CONTROL_COMPLETE, so no local success can sit beside a readiness failure. It only ever narrows:
        it never changes or upgrades the readiness outcome, and is never PAPER-READ-1, retirement or
        trading permission."""
        return self.local_components_complete and self.report.outcome == ReadinessOutcome.CONTROL_COMPLETE


def _cli_only() -> tuple[DemoScenario, ...]:
    """The CLI's extra fixed cases, after the three SCENARIOS, in the order they are printed."""
    return (UNKNOWN_CLEANUP, *ACCOUNT_POSITION_FAILURES, *READER_FRAMING_CASES, *HANDOFF_FAILURE_CASES)


def _scenario(name: str) -> DemoScenario:
    return next((s for s in _cli_only() if s.name == name), None) or SCENARIOS[name]


def _codes(normalizer: object) -> tuple[str, ...]:
    return tuple(issue.code.value for issue in normalizer.issues)


def run_demo(name: str) -> DemoResult:
    """Run one named scenario from fresh normalizers, doubles and adapter under the fixed clock."""
    scenario = _scenario(name)
    clock = FakeClock(DEMO_CLOCK)
    contract = IbkrCallbackNormalizer(_CRID, CallbackRequest.CONTRACT_DETAILS)
    router, handoff = _run_lifecycle(scenario, contract, clock)
    router_report, handoff_report = router.report(), handoff.report()
    # The quote, account-summary and position normalizers are the router's own; their evidence is read
    # through stream_status(), and their envelopes through the router.
    status = {st.stream: st for st in router.stream_status()}
    quote = status[StreamKind.QUOTE_SNAPSHOT]
    summary = status[StreamKind.ACCOUNT_SUMMARY]
    positions = status[StreamKind.POSITIONS]
    script = FakeScript(session=_session(), contract_details=contract.envelopes(),
                        quote_snapshot=router.quote_envelopes(),
                        account_summary=router.account_summary_envelopes(),
                        positions=router.position_envelopes())
    report = run_readiness_check(FakeReadOnlyTransport(script, clock), DEMO_EXPECTATION, clock=clock,
                                 label=f"callback-demo:{name}")
    return DemoResult(
        scenario=scenario, report=report,
        contract_completed=contract.completed, contract_issues=_codes(contract),
        quote_completed=quote.completed, quote_issues=quote.issue_codes,
        account_summary_completed=summary.completed, account_summary_issues=summary.issue_codes,
        positions_completed=positions.completed, positions_issues=positions.issue_codes,
        lifecycle=router_report.adapter, framing=handoff_report.framing, handoff=handoff_report,
        router=router_report,
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
        "decode_handoff": {
            "expected_failure": _value(result.scenario.expected_handoff_failure),
            "failure": _value(result.handoff.failure),
            "held_sequence": result.handoff.held_sequence,
            "handoffs": len(result.handoff.handoffs),
            "settled": sum(h.status.value == "SETTLED" for h in result.handoff.handoffs),
            "router_faults": list(result.router.faults),
            "router_completed": result.router.completed,
            "completed": result.handoff.completed,
        },
        "local_components": {
            "lifecycle_completed": result.lifecycle.completed,
            "framing_clean": result.framing.clean,
            "handoff_completed": result.handoff.completed,
            "streams_completed": result.streams_completed,
            "local_components_complete": result.local_components_complete,
            "all_complete": result.aggregate_complete,
            "authority": "NON_AUTHORITATIVE",
            "trading_permission": False,
            "paper_read_1": "NOT_ASSESSED",
        },
    }


def _value(member: object) -> str | None:
    return None if member is None else member.value


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
                              "framing_report": _framing_json(r.framing),
                              "handoff_records": [{**asdict(h), "status": h.status.value,
                                                   "failure": _value(h.failure)}
                                                  for h in r.handoff.handoffs]} for r in results]}
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
                     f"state={life.state} failures={', '.join(life.failures) or 'none'} "
                     f"ledger_faults={', '.join(ledger.faults) or 'none'}")
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
        handoff = s["decode_handoff"]
        lines.append(f"   decode handoff: expected_failure={handoff['expected_failure']} "
                     f"failure={handoff['failure']} held={handoff['held_sequence']} "
                     f"settled={handoff['settled']}/{handoff['handoffs']} "
                     f"router_faults={', '.join(handoff['router_faults']) or 'none'} "
                     f"completed={handoff['completed']}")
        lines.append(f"   local components (NON_AUTHORITATIVE): "
                     f"lifecycle_completed={life.completed} framing_clean={framing.clean} "
                     f"handoff_completed={r.handoff.completed} "
                     f"streams_completed={r.streams_completed} "
                     f"local_components_complete={r.local_components_complete} "
                     f"all_complete={r.aggregate_complete}")
        lines.append(render_text(r.report, summary=True).rstrip("\n"))
        lines.append("")
    matched = sum(r.matches_expectation for r in results)
    lines.append(f"{matched}/{len(results)} scenarios matched their expected readiness, lifecycle, "
                 "framing and handoff outcomes. A match means only that: NON_AUTHORITATIVE; "
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
