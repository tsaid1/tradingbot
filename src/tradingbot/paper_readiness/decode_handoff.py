"""SDK-free handoff from owned frames to synchronous decoding and router settlement. NON_AUTHORITATIVE.

``DecodeHandoff`` owns one ``FrameAccumulator`` and drives one exact ``CallbackRouter`` through the accepted
``ReadCallbackFacade``. For each complete frame it reserves ownership, takes the frame, decodes it
synchronously with a closed invented decoder into a frame-scoped sink, and then settles every record that
decoding admitted through the router's own ``consume_one``. The frame stays owned until that settlement is
proven from public report evidence.

* **Closed decoder double.** ``InventedDecoder`` looks a whole payload up in a frozen script table. A script
  is a fixed sequence of steps naming fixed router entries; there is no parser, no field splitting, no
  numeric message-id or protobuf selection, no dynamic method name and no user callable. Wire selection and
  any real SDK decoder stay absent.
* **Ownership until settlement.** One frame at a time. A frame is reserved before it is taken; the taken
  frame itself is then kept privately on every failure path, and released only when decoding returned, at least one callback reached the router, no router fault or adapter failure
  appeared, and every admitted record was dispatched by the consumer. Delivery is never inferred from a
  callback count or an empty queue alone.
* **Latched failure.** A callback-free return, a decoder exception (also after callbacks), a version
  mismatch, a router fault (refused or unsupported callback), callback overflow, incomplete settlement,
  foreign queued work, a late callback or reentry latches one fixed failure. The failing frame stays held,
  processing stops, and the failure persists after END. Unsupported, refused and late callbacks also reach
  the router as its persistent refusal.
* **Reaching a normalizer is not stream completion.** A settled handoff means its callbacks were dispatched.
  Whether a stream completed is the normalizers' and router's verdict. Contract details stay outside ledger
  accounting.

``completed`` needs clean framing, every handoff settled, no failure and the router's own completion. One
owning execution context only; nothing is thread-safe. Reports, reprs and errors never carry payloads.
Shutdown stays with the caller, through the router's public ``shutdown``. Nothing here establishes broker
retirement, SDK compatibility or a connection. See docs/DECODE_HANDOFF.md.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from tradingbot.ibkr_read_facade import ReadCallbackFacade
from tradingbot.paper_readiness.callback_router import CallbackRouter, RouterReport
from tradingbot.paper_readiness.read_framing import (
    FeedResult,
    Frame,
    FrameAccumulator,
    FramingLimits,
    FramingOutcome,
    FramingReport,
)

__all__ = [
    "MAX_CALLBACKS_PER_FRAME",
    "MAX_HANDOFF_RECORDS",
    "MAX_SCRIPTS",
    "DecodeHandoff",
    "DecodeHandoffReport",
    "Entry",
    "Failure",
    "HandoffRecord",
    "HandoffStatus",
    "InventedDecoder",
    "Step",
    "StepKind",
]

MAX_CALLBACKS_PER_FRAME = 64
MAX_HANDOFF_RECORDS = 256
MAX_SCRIPTS = 256
MAX_SCRIPT_STEPS = 128  # above MAX_CALLBACKS_PER_FRAME, so overflow is expressible
MAX_PAYLOAD_KEY_BYTES = 4096
UNSUPPORTED_LABEL = "decodedUnsupported"
LATE_LABEL = "decodedLate"
REFUSED_LABEL = "decodedRefused"


class Entry(str, Enum):
    """The fixed router entries a decoded frame may reach, through the read facade."""

    CONTRACT_DETAILS = "contractDetails"
    CONTRACT_DETAILS_END = "contractDetailsEnd"
    MARKET_DATA_TYPE = "marketDataType"
    TICK_PRICE = "tickPrice"
    TICK_SIZE = "tickSize"
    TICK_STRING = "tickString"
    TICK_GENERIC = "tickGeneric"
    TICK_SNAPSHOT_END = "tickSnapshotEnd"
    ACCOUNT_SUMMARY = "accountSummary"
    ACCOUNT_SUMMARY_END = "accountSummaryEnd"
    POSITION = "position"
    POSITION_END = "positionEnd"
    ERROR = "error"              # the explicitly selected 1052-order error
    UNSUPPORTED = "unsupported"  # a callback the bridge does not convert, such as a raw protobuf callback


class StepKind(str, Enum):
    CALL = "CALL"                    # deliver one callback to the frame's sink
    RAISE = "RAISE"                  # the decoder raises at this point
    RETAIN_SINK = "RETAIN_SINK"      # the decoder keeps the sink, so it can call it after returning
    CLEAR_VERSION = "CLEAR_VERSION"  # the decoder's version is cleared, as a reset would


@dataclass(frozen=True, slots=True)
class Step:
    kind: StepKind
    entry: Entry | None = None
    args: tuple = ()

    def __post_init__(self) -> None:
        if type(self.kind) is not StepKind or type(self.args) is not tuple:
            raise TypeError("a Step needs a StepKind and a tuple of args")
        if (self.kind is StepKind.CALL) != (type(self.entry) is Entry):
            raise TypeError("a CALL step names exactly one Entry; other steps name none")


class _DecoderRaised(Exception):
    """The invented decoder's only exception. It carries no payload."""


class InventedDecoder:
    """A closed invented decoder: whole payloads map to fixed scripts. Not a parser of any wire format."""

    __slots__ = ("_retained", "_scripts", "server_version")

    def __init__(self, server_version: int, scripts: dict) -> None:
        if type(server_version) is not int or server_version <= 0:
            raise ValueError("server_version must be a positive int")
        if type(scripts) is not dict or len(scripts) > MAX_SCRIPTS:
            raise ValueError("scripts must be a dict of at most 256 entries")
        for payload, script in scripts.items():
            if type(payload) is not bytes or len(payload) > MAX_PAYLOAD_KEY_BYTES:
                raise ValueError("each script key must be bytes of at most 4096 bytes")
            if (type(script) is not tuple or len(script) > MAX_SCRIPT_STEPS
                    or any(type(step) is not Step for step in script)):
                raise ValueError("each script must be a tuple of at most 128 Steps")
        self.server_version: int | None = server_version
        self._scripts = dict(scripts)
        self._retained: list[_FrameSink] = []

    def decode(self, payload: bytes, sink: _FrameSink) -> None:
        """Run the script for ``payload``. An unknown payload returns without any callback."""
        for step in self._scripts.get(payload, ()):
            if step.kind is StepKind.CALL:
                sink.deliver(step.entry, *step.args)
            elif step.kind is StepKind.RAISE:
                raise _DecoderRaised("invented decoder failure")
            elif step.kind is StepKind.RETAIN_SINK:
                self._retained.append(sink)
            else:
                self.server_version = None

    def call_retained(self, entry: Entry, *args: object) -> None:
        """Deliver through every sink the decoder kept: always after its frame's decoding returned."""
        for sink in self._retained:
            sink.deliver(entry, *args)

    def __repr__(self) -> str:
        return f"InventedDecoder(scripts={len(self._scripts)})"


class Failure(str, Enum):
    RETURNED_WITHOUT_DELIVERY = "RETURNED_WITHOUT_DELIVERY"
    DECODER_RAISED = "DECODER_RAISED"
    VERSION_MISMATCH = "VERSION_MISMATCH"
    ROUTER_REFUSED = "ROUTER_REFUSED"            # a new router fault or adapter failure during the handoff
    UNSUPPORTED_CALLBACK = "UNSUPPORTED_CALLBACK"
    REFUSED_CALLBACK = "REFUSED_CALLBACK"        # not an Entry, or a call form the facade refused
    CALLBACK_OVERFLOW = "CALLBACK_OVERFLOW"
    SETTLEMENT_INCOMPLETE = "SETTLEMENT_INCOMPLETE"
    FOREIGN_QUEUED_WORK = "FOREIGN_QUEUED_WORK"  # queued or fetched router work not from this handoff
    TAKE_FAILED = "TAKE_FAILED"
    LATE_CALLBACK = "LATE_CALLBACK"
    REENTERED = "REENTERED"


class HandoffStatus(str, Enum):
    HELD = "HELD"        # reserved and taken; not settled
    SETTLED = "SETTLED"  # decoded, delivered and every admitted record dispatched
    FAILED = "FAILED"    # a failure latched; the frame stays held


@dataclass(frozen=True, slots=True)
class HandoffRecord:
    sequence: int
    length: int
    status: HandoffStatus
    callbacks: int
    records_admitted: int
    records_dispatched: int
    failure: Failure | None


@dataclass(frozen=True, slots=True)
class DecodeHandoffReport:
    """Fixed codes and counts only. ``completed`` is local, never broker retirement or PAPER-READ-1."""

    framing: FramingReport
    handoffs: tuple[HandoffRecord, ...]
    handoffs_dropped: int
    held_sequence: int | None
    failure: Failure | None
    router_completed: bool
    completed: bool
    authority: str = "NON_AUTHORITATIVE"


class _FrameSink:
    """One frame's callback sink. It forwards through fixed facade entries while its frame decodes."""

    __slots__ = ("_handoff", "_open", "callbacks")

    def __init__(self, handoff: DecodeHandoff) -> None:
        self._handoff = handoff
        self._open = True
        self.callbacks = 0

    def close(self) -> None:
        self._open = False

    def deliver(self, entry: object, *args: object) -> None:
        handoff = self._handoff
        if not self._open:
            handoff._latch(Failure.LATE_CALLBACK, label=LATE_LABEL)
            return
        if type(entry) is not Entry:
            handoff._latch(Failure.REFUSED_CALLBACK, label=REFUSED_LABEL)
            return
        if self.callbacks >= MAX_CALLBACKS_PER_FRAME:
            handoff._latch(Failure.CALLBACK_OVERFLOW)
            return
        self.callbacks += 1
        if entry is Entry.UNSUPPORTED:
            handoff._latch(Failure.UNSUPPORTED_CALLBACK, label=UNSUPPORTED_LABEL)
            return
        if handoff._failure is not None:
            return  # after a latched failure the frame's remaining callbacks are not forwarded
        handoff._forward(entry, args)

    def __repr__(self) -> str:
        return f"_FrameSink(open={self._open}, callbacks={self.callbacks})"


def _evidence(report: RouterReport) -> tuple:
    return (len(report.faults), report.faults_dropped, len(report.adapter.failures),
            report.adapter.failures_dropped)


class DecodeHandoff:
    """Owned frames -> closed invented decoding -> one router's consumer, one frame at a time."""

    __slots__ = ("_accumulator", "_busy", "_decoder", "_dropped", "_facade", "_failure", "_held",
                 "_records", "_router", "_server_version")

    def __init__(self, router: CallbackRouter, decoder: InventedDecoder, *, server_version: int,
                 limits: FramingLimits | None = None) -> None:
        if type(router) is not CallbackRouter:
            raise TypeError("router must be a CallbackRouter")
        if type(decoder) is not InventedDecoder:
            raise TypeError("decoder must be an InventedDecoder")
        if type(server_version) is not int or server_version <= 0:
            raise ValueError("server_version must be a positive int")
        self._router = router
        self._facade = ReadCallbackFacade(router)
        self._decoder = decoder
        self._server_version = server_version  # the provenance every frame is decoded under
        self._accumulator = FrameAccumulator(limits)
        self._records: list[HandoffRecord] = []
        self._dropped = 0
        self._held: _Held | None = None
        self._failure: Failure | None = None
        self._busy = False

    # ------------------------------------------------------------------ input

    def feed(self, chunk: object) -> FeedResult:
        """Opaque bytes into the owned accumulator. Its own bounds and refusals apply unchanged."""
        return self._accumulator.feed(chunk)

    def finish(self) -> FramingReport:
        """End framing input. Retained partial bytes and untaken frames stay evidence."""
        return self._accumulator.finish()

    # ------------------------------------------------------------------ one frame

    def process_one(self) -> bool:
        """Hand one complete frame to decoding and settle it. True only when that frame settled."""
        if self._busy:
            self._latch(Failure.REENTERED)
            return False
        if self._failure is not None or self._held is not None:
            return False  # a failed or unsettled frame stays held; nothing else is taken
        if self._accumulator.report().frames_queued == 0:
            return False
        self._busy = True
        try:
            return self._process()
        finally:
            self._busy = False

    def process_available(self) -> int:
        """Process frames until none is queued or one does not settle. Returns how many settled."""
        settled = 0
        while self.process_one():
            settled += 1
        return settled

    def _process(self) -> bool:
        before = self._router.report()
        if before.queued_records or before.outstanding_fetch:
            self._latch(Failure.FOREIGN_QUEUED_WORK)
            return False
        held = _Held()  # ownership is reserved before the frame leaves the accumulator
        self._held = held
        frame = self._accumulator.take()
        if frame is None:
            self._latch(Failure.TAKE_FAILED)
            return False
        held.frame = frame  # the exact taken frame stays owned until settlement releases it
        held.sequence, held.length = frame.sequence, len(frame.payload)
        if self._decoder.server_version != self._server_version:
            self._latch(Failure.VERSION_MISMATCH)
            return False
        sink = _FrameSink(self)
        held.sink = sink
        try:
            self._decoder.decode(frame.payload, sink)
        except Exception:  # noqa: BLE001 - any decoder exception, before or after callbacks, is a fixed failure
            self._latch(Failure.DECODER_RAISED)
            return False
        finally:
            sink.close()
        if self._failure is not None:
            return False
        if sink.callbacks == 0:
            self._latch(Failure.RETURNED_WITHOUT_DELIVERY)
            return False
        delivered = self._router.report()
        if _evidence(delivered) != _evidence(before):
            self._latch(Failure.ROUTER_REFUSED)
            return False
        held.admitted = delivered.queued_records
        for _ in range(held.admitted):
            if not self._router.consume_one():
                break
            held.dispatched += 1
        settled = self._router.report()
        if (held.dispatched != held.admitted or settled.queued_records or settled.outstanding_fetch
                or settled.adapter.ledger.in_flight_callbacks):
            self._latch(Failure.SETTLEMENT_INCOMPLETE)
            return False
        if _evidence(settled) != _evidence(before):
            self._latch(Failure.ROUTER_REFUSED)
            return False
        self._record(held, HandoffStatus.SETTLED, None)
        self._held = None
        return True

    # ------------------------------------------------------------------ forwarding and failure

    def _forward(self, entry: Entry, args: tuple) -> None:
        f = self._facade
        if entry is Entry.CONTRACT_DETAILS:
            f.contractDetails(*args)
        elif entry is Entry.CONTRACT_DETAILS_END:
            f.contractDetailsEnd(*args)
        elif entry is Entry.MARKET_DATA_TYPE:
            f.marketDataType(*args)
        elif entry is Entry.TICK_PRICE:
            f.tickPrice(*args)
        elif entry is Entry.TICK_SIZE:
            f.tickSize(*args)
        elif entry is Entry.TICK_STRING:
            f.tickString(*args)
        elif entry is Entry.TICK_GENERIC:
            f.tickGeneric(*args)
        elif entry is Entry.TICK_SNAPSHOT_END:
            f.tickSnapshotEnd(*args)
        elif entry is Entry.ACCOUNT_SUMMARY:
            f.accountSummary(*args)
        elif entry is Entry.ACCOUNT_SUMMARY_END:
            f.accountSummaryEnd(*args)
        elif entry is Entry.POSITION:
            f.position(*args)
        elif entry is Entry.POSITION_END:
            f.positionEnd(*args)
        else:
            f.error(*args)

    def _latch(self, failure: Failure, *, label: str | None = None) -> None:
        if self._failure is None:
            self._failure = failure
        if label is not None:
            self._router.unsupported_callback(label)  # a persistent router refusal, after END too
        held = self._held
        if held is not None and not held.recorded:
            self._record(held, HandoffStatus.FAILED, failure)  # the frame stays held

    def _record(self, held: _Held, status: HandoffStatus, failure: Failure | None) -> None:
        held.recorded = True
        record = HandoffRecord(sequence=held.sequence, length=held.length, status=status,
                               callbacks=held.sink.callbacks if held.sink is not None else 0,
                               records_admitted=held.admitted, records_dispatched=held.dispatched,
                               failure=failure)
        if len(self._records) < MAX_HANDOFF_RECORDS:
            self._records.append(record)
        else:
            self._dropped += 1

    # ------------------------------------------------------------------ evidence

    def report(self) -> DecodeHandoffReport:
        framing = self._accumulator.report()
        router_completed = self._router.report().completed
        settled = all(r.status is HandoffStatus.SETTLED for r in self._records)
        completed = (framing.outcome is FramingOutcome.COMPLETE and framing.clean and settled
                     and not self._dropped and self._held is None and self._failure is None
                     and router_completed)
        held = self._held
        return DecodeHandoffReport(
            framing=framing, handoffs=tuple(self._records), handoffs_dropped=self._dropped,
            held_sequence=held.sequence if held is not None else None, failure=self._failure,
            router_completed=router_completed, completed=completed)

    def __repr__(self) -> str:
        return (f"DecodeHandoff(handoffs={len(self._records)}, held={self._held is not None}, "
                f"failure={self._failure.value if self._failure else None})")

    def __reduce_ex__(self, protocol: object) -> object:
        raise TypeError("a DecodeHandoff holds live frames and cannot be serialized")


class _Held:
    """The reservation for the one frame in flight. It keeps the exact taken ``Frame`` (immutable, with a
    payload-free repr) on every failure path; only settlement releases it."""

    __slots__ = ("admitted", "dispatched", "frame", "length", "recorded", "sequence", "sink")

    def __init__(self) -> None:
        self.frame: Frame | None = None
        self.sequence = 0
        self.length = 0
        self.admitted = 0
        self.dispatched = 0
        self.recorded = False
        self.sink: _FrameSink | None = None
