"""SDK-free bounded accumulator for length-prefixed read frames. NON_AUTHORITATIVE.

``FrameAccumulator`` takes opaque byte chunks and extracts frames that each carry a four-byte unsigned
big-endian (network) payload length followed by that many payload bytes. It makes the reader-buffer
state explicit: after every chunk the retained bytes are either nothing, an incomplete header or an
incomplete payload, and every accepted byte is either consumed into a frame or still retained.

* **Exact accounting.** A complete frame consumes ``4 + length`` bytes and the suffix is retained. A
  zero-length frame is a complete frame that consumes four bytes. It is never confused with an
  incomplete header, which consumes nothing. ``bytes_accepted == bytes_consumed + bytes_retained``.
* **Bounded before growth.** Chunk size, declared payload length, buffered bytes and the queued output's
  frame count and payload bytes are each checked before the buffer or queue grows. A limit is
  defensive model policy chosen here, not a protocol fact.
* **Terminal refusal.** Any refusal latches. Later chunks are refused and not retained, and retained
  bytes are never dropped. Taking frames or finishing cannot turn a refused, partial or late-input
  accumulator into a clean one. There is no drain, clear or decode operation.
* **Opaque payloads.** Payload bytes are returned exactly as received. Nothing here splits, decodes or
  validates fields, so a payload without a final field terminator is preserved and never called valid.
* **Local only.** Taking a frame hands it to the caller. Whether the caller processes it, and any
  broker-side retirement, is outside this module (``downstream_retirement`` stays NOT_ESTABLISHED).

It imports no SDK, opens no socket, starts no thread and reads no clock. Reports carry fixed codes and
counts only, never payload bytes. See docs/READ_FRAMING.md.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

HEADER_BYTES = 4

# Defensive model policy, not vendor protocol facts. Every configured limit must lie in [1, ceiling].
DEFAULT_MAX_CHUNK_BYTES = 64 * 1024
DEFAULT_MAX_PAYLOAD_BYTES = 1024 * 1024
DEFAULT_MAX_BUFFERED_BYTES = HEADER_BYTES + DEFAULT_MAX_PAYLOAD_BYTES + DEFAULT_MAX_CHUNK_BYTES
DEFAULT_MAX_QUEUED_FRAMES = 256
DEFAULT_MAX_QUEUED_BYTES = 4 * 1024 * 1024
CEILING_BYTES = 64 * 1024 * 1024
CEILING_FRAMES = 65536

DOWNSTREAM_RETIREMENT = "NOT_ESTABLISHED"


class FeedStatus(str, Enum):
    """What one ``feed`` call left behind."""

    COMPLETE = "COMPLETE"  # no partial frame is retained; it says nothing about the payloads' meaning
    INCOMPLETE_HEADER = "INCOMPLETE_HEADER"  # one to three header bytes retained
    INCOMPLETE_PAYLOAD = "INCOMPLETE_PAYLOAD"  # a full header retained, its payload not yet complete
    REFUSED = "REFUSED"


class Refusal(str, Enum):
    CHUNK_NOT_BYTES = "CHUNK_NOT_BYTES"
    CHUNK_TOO_LARGE = "CHUNK_TOO_LARGE"
    BUFFER_OVERFLOW = "BUFFER_OVERFLOW"
    DECLARED_LENGTH_TOO_LARGE = "DECLARED_LENGTH_TOO_LARGE"
    QUEUED_FRAMES_FULL = "QUEUED_FRAMES_FULL"
    QUEUED_BYTES_FULL = "QUEUED_BYTES_FULL"
    ARRIVAL_AFTER_REFUSAL = "ARRIVAL_AFTER_REFUSAL"
    ARRIVAL_AFTER_FINISH = "ARRIVAL_AFTER_FINISH"


class FramingOutcome(str, Enum):
    OPEN = "OPEN"  # not finished
    COMPLETE = "COMPLETE"  # finished with nothing retained, nothing queued and no refusal ever
    PARTIAL_HEADER = "PARTIAL_HEADER"  # finished with an incomplete header retained
    PARTIAL_PAYLOAD = "PARTIAL_PAYLOAD"  # finished with an incomplete payload retained
    UNTAKEN_FRAMES = "UNTAKEN_FRAMES"  # finished with complete frames still queued
    REFUSED = "REFUSED"  # any refusal, at any time, including input after finishing


def _limit(name: str, value: object, ceiling: int) -> None:
    if type(value) is not int:
        raise TypeError(f"{name} must be an exact int")
    if not 1 <= value <= ceiling:
        raise ValueError(f"{name} must be between 1 and {ceiling}")


@dataclass(frozen=True, slots=True)
class FramingLimits:
    """The accumulator's defensive limits, validated on construction."""

    max_chunk_bytes: int = DEFAULT_MAX_CHUNK_BYTES
    max_payload_bytes: int = DEFAULT_MAX_PAYLOAD_BYTES
    max_buffered_bytes: int = DEFAULT_MAX_BUFFERED_BYTES
    max_queued_frames: int = DEFAULT_MAX_QUEUED_FRAMES
    max_queued_bytes: int = DEFAULT_MAX_QUEUED_BYTES

    def __post_init__(self) -> None:
        _limit("max_chunk_bytes", self.max_chunk_bytes, CEILING_BYTES)
        _limit("max_payload_bytes", self.max_payload_bytes, CEILING_BYTES)
        _limit("max_buffered_bytes", self.max_buffered_bytes, CEILING_BYTES)
        _limit("max_queued_frames", self.max_queued_frames, CEILING_FRAMES)
        _limit("max_queued_bytes", self.max_queued_bytes, CEILING_BYTES)
        # Every frame the payload limit allows must fit in the buffer and in the queue, and a chunk the
        # chunk limit allows must fit in an empty buffer.
        if self.max_buffered_bytes < HEADER_BYTES + self.max_payload_bytes:
            raise ValueError("max_buffered_bytes must hold a header plus the largest payload")
        if self.max_buffered_bytes < self.max_chunk_bytes:
            raise ValueError("max_buffered_bytes must hold the largest chunk")
        if self.max_queued_bytes < self.max_payload_bytes:
            raise ValueError("max_queued_bytes must hold the largest payload")


@dataclass(frozen=True, slots=True, repr=False)
class Frame:
    """One complete frame. ``payload`` is the exact opaque bytes; nothing about its fields is claimed."""

    sequence: int
    payload: bytes

    @property
    def length(self) -> int:
        return len(self.payload)

    def __repr__(self) -> str:
        return f"Frame(sequence={self.sequence}, length={len(self.payload)})"


@dataclass(frozen=True, slots=True)
class FeedResult:
    """One ``feed`` call: fixed codes and counts only."""

    status: FeedStatus
    refusal: Refusal | None
    frames_extracted: int
    bytes_consumed: int
    bytes_retained: int


@dataclass(frozen=True, slots=True)
class FramingReport:
    """Local buffer accounting. Fixed codes and counts only, never payload bytes."""

    outcome: FramingOutcome
    clean: bool
    finished: bool
    first_refusal: Refusal | None
    refusals: tuple[tuple[Refusal, int], ...]  # one entry per code that occurred, in enum order
    chunks_accepted: int
    chunks_refused: int
    bytes_accepted: int
    bytes_consumed: int
    bytes_retained: int
    bytes_refused: int  # exact-bytes chunks refused and never retained; other refused values are uncounted
    frames_extracted: int
    zero_length_frames: int
    frames_queued: int
    queued_payload_bytes: int
    frames_handed_off: int
    handed_off_payload_bytes: int
    downstream_retirement: str = DOWNSTREAM_RETIREMENT
    authority: str = "NON_AUTHORITATIVE"


class FrameAccumulator:
    """Single-threaded, local accumulator. ``feed`` chunks, ``take`` frames, then ``finish``."""

    __slots__ = ("_accepted", "_buffer", "_chunks_accepted", "_chunks_refused", "_consumed",
                 "_finish_outcome", "_finished", "_first_refusal", "_frames", "_handed_off",
                 "_handed_off_bytes", "_limits", "_queue", "_queued_bytes", "_refusal_counts",
                 "_refused_bytes", "_zero_length")

    def __init__(self, limits: FramingLimits | None = None) -> None:
        if limits is None:
            limits = FramingLimits()
        if type(limits) is not FramingLimits:
            raise TypeError("limits must be a FramingLimits")
        self._limits = limits
        self._buffer = bytearray()
        self._queue: list[Frame] = []
        self._queued_bytes = 0
        self._accepted = 0
        self._consumed = 0
        self._refused_bytes = 0
        self._chunks_accepted = 0
        self._chunks_refused = 0
        self._frames = 0
        self._zero_length = 0
        self._handed_off = 0
        self._handed_off_bytes = 0
        self._first_refusal: Refusal | None = None
        self._refusal_counts: dict[Refusal, int] = {}
        self._finished = False
        self._finish_outcome = FramingOutcome.OPEN

    def __reduce_ex__(self, protocol: object) -> object:
        raise TypeError("FrameAccumulator is not serializable")

    @property
    def limits(self) -> FramingLimits:
        return self._limits

    # ------------------------------------------------------------------ input

    def feed(self, chunk: object) -> FeedResult:
        """Accept one exact ``bytes`` chunk and extract every complete frame it completes."""
        if self._finished:
            return self._refuse_chunk(Refusal.ARRIVAL_AFTER_FINISH, chunk)
        if self._first_refusal is not None:
            return self._refuse_chunk(Refusal.ARRIVAL_AFTER_REFUSAL, chunk)
        if type(chunk) is not bytes:
            return self._refuse_chunk(Refusal.CHUNK_NOT_BYTES, chunk)
        if len(chunk) > self._limits.max_chunk_bytes:
            return self._refuse_chunk(Refusal.CHUNK_TOO_LARGE, chunk)
        if len(self._buffer) + len(chunk) > self._limits.max_buffered_bytes:
            return self._refuse_chunk(Refusal.BUFFER_OVERFLOW, chunk)
        self._buffer += chunk
        self._accepted += len(chunk)
        self._chunks_accepted += 1
        return self._extract()

    def _extract(self) -> FeedResult:
        frames = 0
        consumed = 0
        refusal: Refusal | None = None
        while len(self._buffer) >= HEADER_BYTES:
            length = int.from_bytes(self._buffer[:HEADER_BYTES], "big", signed=False)
            if length > self._limits.max_payload_bytes:
                refusal = Refusal.DECLARED_LENGTH_TOO_LARGE
                break
            if len(self._buffer) < HEADER_BYTES + length:
                break
            if len(self._queue) + 1 > self._limits.max_queued_frames:
                refusal = Refusal.QUEUED_FRAMES_FULL
                break
            if self._queued_bytes + length > self._limits.max_queued_bytes:
                refusal = Refusal.QUEUED_BYTES_FULL
                break
            payload = bytes(self._buffer[HEADER_BYTES:HEADER_BYTES + length])
            del self._buffer[:HEADER_BYTES + length]
            self._frames += 1
            self._queue.append(Frame(self._frames, payload))
            self._queued_bytes += length
            self._consumed += HEADER_BYTES + length
            consumed += HEADER_BYTES + length
            frames += 1
            if length == 0:
                self._zero_length += 1
        if refusal is not None:
            # The refused frame's bytes stay retained as evidence; nothing is consumed or dropped.
            self._record(refusal)
            return FeedResult(FeedStatus.REFUSED, refusal, frames, consumed, len(self._buffer))
        return FeedResult(self._retained_status(), None, frames, consumed, len(self._buffer))

    def _retained_status(self) -> FeedStatus:
        if not self._buffer:
            return FeedStatus.COMPLETE
        return FeedStatus.INCOMPLETE_HEADER if len(self._buffer) < HEADER_BYTES else FeedStatus.INCOMPLETE_PAYLOAD

    def _refuse_chunk(self, refusal: Refusal, chunk: object) -> FeedResult:
        self._chunks_refused += 1
        if type(chunk) is bytes:
            self._refused_bytes += len(chunk)
        self._record(refusal)
        return FeedResult(FeedStatus.REFUSED, refusal, 0, 0, len(self._buffer))

    def _record(self, refusal: Refusal) -> None:
        if self._first_refusal is None:
            self._first_refusal = refusal
        self._refusal_counts[refusal] = self._refusal_counts.get(refusal, 0) + 1

    # ------------------------------------------------------------------ output

    def take(self) -> Frame | None:
        """Hand the oldest complete frame to the caller, or ``None``. It never changes an outcome."""
        if not self._queue:
            return None
        frame = self._queue.pop(0)
        self._queued_bytes -= len(frame.payload)
        self._handed_off += 1
        self._handed_off_bytes += len(frame.payload)
        return frame

    def finish(self) -> FramingReport:
        """End input and latch the outcome. Retained bytes stay retained; later input is refused."""
        if not self._finished:
            self._finished = True
            if self._buffer:
                self._finish_outcome = (FramingOutcome.PARTIAL_HEADER if len(self._buffer) < HEADER_BYTES
                                        else FramingOutcome.PARTIAL_PAYLOAD)
            elif self._queue:
                self._finish_outcome = FramingOutcome.UNTAKEN_FRAMES
            else:
                self._finish_outcome = FramingOutcome.COMPLETE
        return self.report()

    def report(self) -> FramingReport:
        if self._first_refusal is not None:
            outcome = FramingOutcome.REFUSED
        else:
            outcome = self._finish_outcome
        return FramingReport(
            outcome=outcome,
            clean=outcome is FramingOutcome.COMPLETE,
            finished=self._finished,
            first_refusal=self._first_refusal,
            refusals=tuple((code, self._refusal_counts[code]) for code in Refusal if code in self._refusal_counts),
            chunks_accepted=self._chunks_accepted,
            chunks_refused=self._chunks_refused,
            bytes_accepted=self._accepted,
            bytes_consumed=self._consumed,
            bytes_retained=len(self._buffer),
            bytes_refused=self._refused_bytes,
            frames_extracted=self._frames,
            zero_length_frames=self._zero_length,
            frames_queued=len(self._queue),
            queued_payload_bytes=self._queued_bytes,
            frames_handed_off=self._handed_off,
            handed_off_payload_bytes=self._handed_off_bytes,
        )
