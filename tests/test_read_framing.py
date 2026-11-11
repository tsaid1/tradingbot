"""Bounded length-prefixed read framing, on invented bytes only.

Every failure case is paired with a control. The checks cover fragmented headers and payloads,
concatenated frames, trailing partials, zero-length progress, oversized declarations, chunk, buffer and
queue limits, terminal refusals and leftovers, and unterminated opaque field data. Byte conservation is
checked after every step.
"""
from __future__ import annotations

import pickle
import random

import pytest

from tradingbot.paper_readiness import read_framing as rf
from tradingbot.paper_readiness.read_framing import (
    FeedStatus,
    Frame,
    FrameAccumulator,
    FramingLimits,
    FramingOutcome,
    Refusal,
)
from tradingbot.paper_readiness.transport import FORBIDDEN_NAME_RE

CANARY = b"INVENTED-CANARY-PAYLOAD"


def frame(payload: bytes) -> bytes:
    return len(payload).to_bytes(4, "big") + payload


def conserved(acc: FrameAccumulator) -> None:
    r = acc.report()
    assert r.bytes_accepted == r.bytes_consumed + r.bytes_retained
    assert r.bytes_consumed == 4 * r.frames_extracted + r.queued_payload_bytes + r.handed_off_payload_bytes
    assert r.frames_extracted == r.frames_queued + r.frames_handed_off


def take_all(acc: FrameAccumulator) -> list[bytes]:
    out = []
    while (f := acc.take()) is not None:
        out.append(f.payload)
    return out


def small(**overrides) -> FramingLimits:
    base = {"max_chunk_bytes": 32, "max_payload_bytes": 16, "max_buffered_bytes": 64,
            "max_queued_frames": 4, "max_queued_bytes": 32}
    base.update(overrides)
    return FramingLimits(**base)


# ----------------------------------------------------------------------------- complete frames


def test_control_one_complete_frame_consumes_header_plus_length_and_finishes_clean():
    acc = FrameAccumulator()
    result = acc.feed(frame(b"abc"))
    assert (result.status, result.refusal, result.frames_extracted, result.bytes_consumed,
            result.bytes_retained) == (FeedStatus.COMPLETE, None, 1, 7, 0)
    assert take_all(acc) == [b"abc"]
    report = acc.finish()
    assert report.outcome is FramingOutcome.COMPLETE and report.clean is True
    conserved(acc)


def test_concatenated_frames_in_one_chunk_are_extracted_in_order_and_the_suffix_retained():
    acc = FrameAccumulator()
    result = acc.feed(frame(b"one") + frame(b"") + frame(b"three") + b"\x00\x00")
    assert result.frames_extracted == 3 and result.bytes_consumed == 7 + 4 + 9
    assert result.status is FeedStatus.INCOMPLETE_HEADER and result.bytes_retained == 2
    assert [f.sequence for f in (acc.take(), acc.take(), acc.take())] == [1, 2, 3]
    conserved(acc)


def test_fragmented_header_then_payload_reports_each_partial_state_and_conserves_bytes():
    acc = FrameAccumulator()
    stream = frame(b"hello")
    expected = ([FeedStatus.INCOMPLETE_HEADER] * 3 + [FeedStatus.INCOMPLETE_PAYLOAD] * 5
                + [FeedStatus.COMPLETE])
    for i, status in enumerate(expected):
        result = acc.feed(stream[i:i + 1])
        assert result.status is status, i
        assert result.frames_extracted == (1 if i == len(stream) - 1 else 0)
        conserved(acc)
    assert take_all(acc) == [b"hello"]


def test_payload_split_across_chunks_is_retained_until_complete():
    acc = FrameAccumulator()
    stream = frame(b"0123456789")
    assert acc.feed(stream[:6]).status is FeedStatus.INCOMPLETE_PAYLOAD
    assert acc.report().bytes_retained == 6 and acc.report().bytes_consumed == 0
    assert acc.feed(stream[6:10]).status is FeedStatus.INCOMPLETE_PAYLOAD
    result = acc.feed(stream[10:])
    assert result.status is FeedStatus.COMPLETE and result.bytes_consumed == 14
    assert take_all(acc) == [b"0123456789"]
    conserved(acc)


def test_arbitrary_chunk_splits_reconstruct_every_payload_exactly():
    rng = random.Random(20261111)
    payloads = [bytes(rng.randrange(256) for _ in range(rng.randrange(0, 40))) for _ in range(30)]
    stream = b"".join(frame(p) for p in payloads)
    acc = FrameAccumulator()
    out, i = [], 0
    while i < len(stream):
        n = rng.randrange(1, 12)
        assert acc.feed(stream[i:i + n]).refusal is None
        i += n
        conserved(acc)
        out += take_all(acc)
    assert out == payloads
    report = acc.finish()
    assert report.clean is True and report.bytes_accepted == report.bytes_consumed == len(stream)


# ----------------------------------------------------------------------------- zero-length frames


def test_a_zero_length_frame_advances_four_bytes_and_is_distinct_from_an_incomplete_header():
    acc = FrameAccumulator()
    result = acc.feed(b"\x00\x00\x00\x00")
    assert (result.status, result.frames_extracted, result.bytes_consumed) == (FeedStatus.COMPLETE, 1, 4)
    f = acc.take()
    assert f is not None and f.payload == b"" and f.length == 0
    assert acc.report().zero_length_frames == 1

    partial = FrameAccumulator()
    result = partial.feed(b"\x00\x00\x00")
    assert (result.status, result.frames_extracted, result.bytes_consumed, result.bytes_retained) == (
        FeedStatus.INCOMPLETE_HEADER, 0, 0, 3)
    assert partial.take() is None and partial.finish().outcome is FramingOutcome.PARTIAL_HEADER


def test_repeated_zero_length_frames_make_progress_and_stay_bounded_by_the_frame_count():
    acc = FrameAccumulator(small(max_queued_frames=3))
    result = acc.feed(b"\x00" * 16)
    assert result.frames_extracted == 3 and result.refusal is Refusal.QUEUED_FRAMES_FULL
    assert acc.report().bytes_retained == 4  # the fourth zero frame is retained, not dropped
    conserved(acc)


# ----------------------------------------------------------------------------- trailing partials


@pytest.mark.parametrize(("tail", "outcome"), [(b"\x00\x00", FramingOutcome.PARTIAL_HEADER),
                                               (frame(b"xyz")[:5], FramingOutcome.PARTIAL_PAYLOAD)])
def test_a_trailing_partial_is_terminal_evidence_never_clean(tail, outcome):
    acc = FrameAccumulator()
    acc.feed(frame(b"ok") + tail)
    assert take_all(acc) == [b"ok"]
    report = acc.finish()
    assert report.outcome is outcome and report.clean is False and report.bytes_retained == len(tail)
    assert acc.take() is None and acc.finish().outcome is outcome  # nothing drains it
    conserved(acc)


def test_untaken_frames_at_finish_are_not_clean_and_later_taking_does_not_change_that():
    acc = FrameAccumulator()
    acc.feed(frame(b"a") + frame(b"b"))
    assert acc.finish().outcome is FramingOutcome.UNTAKEN_FRAMES
    assert take_all(acc) == [b"a", b"b"]
    report = acc.report()
    assert report.outcome is FramingOutcome.UNTAKEN_FRAMES and report.clean is False
    assert report.frames_handed_off == 2


# ----------------------------------------------------------------------------- declared length


def test_an_oversized_declaration_is_refused_as_soon_as_the_header_is_complete():
    acc = FrameAccumulator(small())
    result = acc.feed((17).to_bytes(4, "big"))
    assert result.status is FeedStatus.REFUSED and result.refusal is Refusal.DECLARED_LENGTH_TOO_LARGE
    assert acc.report().bytes_retained == 4  # header kept as evidence; no payload awaited
    after = acc.feed(b"x" * 17)
    assert after.refusal is Refusal.ARRIVAL_AFTER_REFUSAL and acc.report().bytes_retained == 4
    report = acc.finish()
    assert report.outcome is FramingOutcome.REFUSED and report.clean is False
    assert report.first_refusal is Refusal.DECLARED_LENGTH_TOO_LARGE and report.bytes_refused == 17
    conserved(acc)


def test_control_a_declaration_at_the_payload_limit_is_accepted():
    acc = FrameAccumulator(small())
    assert acc.feed(frame(b"p" * 16)).status is FeedStatus.COMPLETE
    assert take_all(acc) == [b"p" * 16] and acc.finish().clean is True


def test_the_largest_unsigned_declaration_is_refused_not_read_as_negative():
    acc = FrameAccumulator()
    assert acc.feed(b"\xff\xff\xff\xff").refusal is Refusal.DECLARED_LENGTH_TOO_LARGE


# ----------------------------------------------------------------------------- chunk and buffer limits


def test_an_oversized_chunk_is_refused_before_any_growth():
    acc = FrameAccumulator(small())
    acc.feed(frame(b"ab")[:3])
    result = acc.feed(b"y" * 33)
    assert result.refusal is Refusal.CHUNK_TOO_LARGE and acc.report().bytes_retained == 3
    assert acc.report().bytes_refused == 33 and acc.finish().outcome is FramingOutcome.REFUSED
    conserved(acc)


def test_control_a_chunk_at_the_chunk_limit_is_accepted():
    acc = FrameAccumulator(small())
    stream = frame(b"a" * 12) + frame(b"b" * 12)  # 32 bytes
    assert acc.feed(stream).frames_extracted == 2


def test_buffer_overflow_is_refused_before_growth_and_retained_bytes_survive():
    acc = FrameAccumulator(small(max_buffered_bytes=24, max_chunk_bytes=16))
    acc.feed(frame(b"z" * 16)[:16])
    result = acc.feed(b"z" * 9)  # 16 + 9 > 24
    assert result.refusal is Refusal.BUFFER_OVERFLOW and acc.report().bytes_retained == 16
    report = acc.finish()
    assert report.outcome is FramingOutcome.REFUSED and report.bytes_retained == 16
    conserved(acc)


def test_control_filling_the_buffer_exactly_to_its_limit_is_accepted():
    acc = FrameAccumulator(small(max_buffered_bytes=24, max_chunk_bytes=16))
    acc.feed(frame(b"z" * 16)[:16])
    result = acc.feed(b"z" * 4)  # 20 bytes: the frame completes
    assert result.status is FeedStatus.COMPLETE and take_all(acc) == [b"z" * 16]


# ----------------------------------------------------------------------------- queued-output limits


def test_the_queued_frame_count_limit_refuses_and_keeps_the_next_frame_retained():
    acc = FrameAccumulator(small(max_queued_frames=2))
    result = acc.feed(frame(b"1") + frame(b"2") + frame(b"3"))
    assert result.frames_extracted == 2 and result.refusal is Refusal.QUEUED_FRAMES_FULL
    assert acc.report().bytes_retained == 5
    assert take_all(acc) == [b"1", b"2"]
    assert acc.finish().outcome is FramingOutcome.REFUSED  # taking does not make it clean
    conserved(acc)


def test_the_queued_byte_limit_refuses_and_keeps_the_next_frame_retained():
    acc = FrameAccumulator(small(max_queued_bytes=20))
    result = acc.feed(frame(b"a" * 12) + frame(b"b" * 12))
    assert result.frames_extracted == 1 and result.refusal is Refusal.QUEUED_BYTES_FULL
    assert acc.report().queued_payload_bytes == 12 and acc.report().bytes_retained == 16
    conserved(acc)


def test_control_taking_between_chunks_keeps_the_queue_within_its_limits():
    acc = FrameAccumulator(small(max_queued_frames=1, max_queued_bytes=16))
    for payload in (b"a" * 16, b"b" * 16, b"c"):
        assert acc.feed(frame(payload)).status is FeedStatus.COMPLETE
        assert take_all(acc) == [payload]
    assert acc.finish().clean is True


# ----------------------------------------------------------------------------- input types and terminality


@pytest.mark.parametrize("chunk", [bytearray(b"\x00\x00\x00\x00"), memoryview(b"\x00\x00\x00\x00"), "abcd",
                                   4, True, None, type("B", (bytes,), {})(b"\x00\x00\x00\x00")],
                         ids=["bytearray", "memoryview", "str", "int", "bool", "none", "bytes-subclass"])
def test_anything_but_exact_bytes_is_refused_and_nothing_is_retained(chunk):
    acc = FrameAccumulator()
    result = acc.feed(chunk)
    assert result.refusal is Refusal.CHUNK_NOT_BYTES and acc.report().bytes_retained == 0
    assert acc.report().bytes_accepted == 0 and acc.finish().outcome is FramingOutcome.REFUSED


def test_input_after_finish_is_refused_and_a_clean_finish_does_not_stay_clean():
    acc = FrameAccumulator()
    acc.feed(frame(b"done"))
    take_all(acc)
    assert acc.finish().clean is True
    result = acc.feed(frame(b"late"))
    assert result.refusal is Refusal.ARRIVAL_AFTER_FINISH and acc.take() is None
    report = acc.report()
    assert report.outcome is FramingOutcome.REFUSED and report.clean is False and report.bytes_refused == 8


def test_refusals_are_counted_per_fixed_code_and_the_first_is_latched():
    acc = FrameAccumulator(small())
    acc.feed("text")
    acc.feed(frame(b"x"))
    acc.feed(frame(b"x"))
    report = acc.finish()
    assert report.first_refusal is Refusal.CHUNK_NOT_BYTES
    assert report.refusals == ((Refusal.CHUNK_NOT_BYTES, 1), (Refusal.ARRIVAL_AFTER_REFUSAL, 2))
    assert report.chunks_refused == 3 and report.chunks_accepted == 0


def test_no_operation_can_drain_clear_or_decode_retained_bytes():
    public = {n for n in dir(FrameAccumulator) if not n.startswith("_")}
    assert public == {"feed", "take", "finish", "report", "limits"}
    for name in public | {n for n in dir(rf) if not n.startswith("_")}:
        assert not FORBIDDEN_NAME_RE.search(name), name


# ----------------------------------------------------------------------------- opaque payloads


@pytest.mark.parametrize("payload", [b"f1\x00f2\x00", b"f1\x00unterminated"], ids=["terminated", "missing-nul"])
def test_field_data_is_opaque_and_an_unterminated_final_field_is_preserved_byte_for_byte(payload):
    acc = FrameAccumulator()
    acc.feed(frame(payload))
    f = acc.take()
    assert f is not None and f.payload == payload and type(f.payload) is bytes
    assert {n for n in dir(f) if not n.startswith("_")} == {"sequence", "payload", "length"}
    report = acc.finish()
    assert report.clean is True  # framing is complete; nothing is claimed about the fields
    public = [n for n in dir(report) if not n.startswith("_")]
    assert not any("field" in n or "valid" in n or "decod" in n for n in public)


# ----------------------------------------------------------------------------- surface


def test_reports_and_reprs_carry_no_payload_and_the_accumulator_is_not_serializable():
    acc = FrameAccumulator()
    acc.feed(frame(CANARY) + frame(CANARY)[:10])
    f = acc.take()
    text = repr(acc.report()) + repr(f) + repr(acc) + repr(acc.feed(b""))
    assert CANARY.decode() not in text and "INVENTED" not in text
    assert acc.report().downstream_retirement == "NOT_ESTABLISHED"
    assert acc.report().authority == "NON_AUTHORITATIVE"
    with pytest.raises(TypeError):
        pickle.dumps(acc)
    with pytest.raises(AttributeError):
        acc.extra = 1  # type: ignore[attr-defined]
    with pytest.raises(AttributeError):
        f.payload = b"other"  # type: ignore[misc]


def test_frames_are_immutable_snapshots_of_the_received_bytes():
    acc = FrameAccumulator()
    acc.feed(frame(b"keep") + frame(b"next")[:2])
    f = acc.take()
    acc.feed(frame(b"next")[2:])
    assert f == Frame(1, b"keep") and acc.take() == Frame(2, b"next")


@pytest.mark.parametrize("overrides", [
    {"max_chunk_bytes": True}, {"max_payload_bytes": 1.0}, {"max_queued_frames": 0},
    {"max_queued_bytes": -1}, {"max_buffered_bytes": rf.CEILING_BYTES + 1},
    {"max_queued_frames": rf.CEILING_FRAMES + 1},
    {"max_payload_bytes": 61},  # buffer 64 cannot hold header + 61
    {"max_chunk_bytes": 65},  # larger than the buffer
    {"max_queued_bytes": 15},  # smaller than the largest payload
], ids=["bool", "float", "zero", "negative", "byte-ceiling", "frame-ceiling", "buffer-vs-payload",
        "buffer-vs-chunk", "queue-vs-payload"])
def test_limits_are_validated_by_exact_type_range_and_consistency(overrides):
    with pytest.raises((TypeError, ValueError)):
        small(**overrides)


def test_control_defaults_are_consistent_and_a_non_limits_object_is_refused():
    limits = FramingLimits()
    assert limits.max_buffered_bytes >= 4 + limits.max_payload_bytes
    assert FrameAccumulator().limits == limits
    with pytest.raises(TypeError):
        FrameAccumulator({"max_chunk_bytes": 1})  # type: ignore[arg-type]
