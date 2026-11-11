# Read framing (SDK-free, NON_AUTHORITATIVE)

`src/tradingbot/paper_readiness/read_framing.py` adds `FrameAccumulator`, a bounded accumulator for
length-prefixed read frames. Each frame is a four-byte unsigned big-endian (network) payload length
followed by that many payload bytes.

It addresses a reader-buffer gap: a plain "extract one frame" step gives no memory bound and no
explicit partial state, and a zero declared length alone cannot tell a complete empty frame from an
incomplete header. This module makes retained bytes, partial frames and refusals observable and
accountable.

It imports no SDK, opens no socket, starts no thread and reads no clock. It is exercised only with
invented bytes. **Wiring it to a real reader is a later, separately reviewed task.**

## Use

```python
acc = FrameAccumulator(FramingLimits())  # or FrameAccumulator() for the defaults
result = acc.feed(chunk)  # FeedResult: status, refusal, frames_extracted, bytes_consumed, bytes_retained
frame = acc.take()        # oldest complete Frame (sequence, payload bytes) or None
report = acc.finish()     # FramingReport: latched outcome, fixed codes and counts
```

## Model

| Input | Result |
|---|---|
| Fewer than four buffered bytes | `INCOMPLETE_HEADER`. Nothing consumed; every byte retained |
| A full header, payload incomplete | `INCOMPLETE_PAYLOAD`. Nothing consumed; every byte retained |
| A complete frame | Consumes exactly `4 + length`. The suffix is retained and scanned for further frames |
| A zero declared length | A complete frame with empty payload. Consumes four bytes and is counted in `zero_length_frames` |
| Several frames in one chunk | All are extracted in order. A trailing partial is retained |

`COMPLETE` means only that no partial frame is retained. It says nothing about what the payloads mean.

**Conservation.** After every call:
- `bytes_accepted == bytes_consumed + bytes_retained`
- `bytes_consumed == 4 * frames_extracted + queued_payload_bytes + handed_off_payload_bytes`

Refused chunks are never retained. Exact-bytes refused input is counted in `bytes_refused`.

**Opaque payloads.** A payload is returned byte for byte as an immutable `bytes` value. Nothing splits,
decodes or validates fields. A payload whose final field lacks a terminator is preserved unchanged and is
never reported as valid. Field interpretation is a separate concern.

## Limits (defensive model policy)

These are application limits chosen for this model, **not protocol facts**:

| Limit | Default |
|---|---|
| `max_chunk_bytes` | 64 KiB |
| `max_payload_bytes` | 1 MiB |
| `max_buffered_bytes` | 4 bytes + 1 MiB + 64 KiB |
| `max_queued_frames` | 256 |
| `max_queued_bytes` | 4 MiB |

Each limit must be an exact `int` between 1 and its ceiling (64 MiB, or 65536 frames). The limits must
also be consistent: the buffer holds a header plus the largest payload, and the largest chunk; the queue
holds the largest payload. Invalid limits raise on construction.

Every check precedes growth:
- a chunk that is not exact `bytes` is refused (`CHUNK_NOT_BYTES`). This includes mutable buffers,
  views, text and subclasses
- an oversized chunk is refused before it is appended (`CHUNK_TOO_LARGE`)
- a chunk that would overflow the buffer is refused before it is appended (`BUFFER_OVERFLOW`)
- an oversized declared length is refused as soon as its header is complete, without waiting for its
  payload (`DECLARED_LENGTH_TOO_LARGE`)
- a frame that would exceed the queued frame count or queued payload bytes is refused before it is
  queued (`QUEUED_FRAMES_FULL`, `QUEUED_BYTES_FULL`)

A refused frame's bytes stay retained as evidence.

## Terminal evidence

- **Refusals latch.** After any refusal, later chunks are refused (`ARRIVAL_AFTER_REFUSAL`) and not
  retained. The first refusal and a per-code count are reported.
- **`finish()` latches the outcome:**
  - `COMPLETE` only with nothing retained, nothing queued and no refusal
  - `PARTIAL_HEADER` or `PARTIAL_PAYLOAD` when bytes remain
  - `UNTAKEN_FRAMES` when complete frames were never taken
  - `REFUSED` on any refusal
- Input after `finish()` is refused (`ARRIVAL_AFTER_FINISH`) and turns any outcome into `REFUSED`.
- **Nothing can manufacture a clean result.** There is no drain, clear or decode operation. Retained
  bytes are never dropped. Taking frames after a refusal or after finishing does not change the outcome.

## Ownership and limits of the claim

`take()` hands a frame to the caller. That is local buffer accounting only. Whether the caller processes
the frame, and any broker-side retirement, is outside this module (`downstream_retirement` is
`NOT_ESTABLISHED`).

- `clean` is a local framing result. It is never message validity, PAPER-READ-1 acceptance or trading
  permission.
- **Single-threaded.** Concurrent use is not supported.
- **No secrecy.** Encapsulation guards against ordinary misuse, not hostile code in the same process.
- **Payload-free reports.** Reports and reprs carry fixed codes and counts, never payload bytes. The
  accumulator refuses serialization.
- **Not connected.** It has no caller yet. Socket reading, message decoding, protocol versioning and
  connected use remain separately gated.
