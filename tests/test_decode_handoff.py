"""Decode handoff: owned frames -> closed invented decoding -> one router's consumer, on invented data only.

Every case composes the actual FrameAccumulator, the closed InventedDecoder, the actual CallbackRouter
(through the accepted ReadCallbackFacade) and its adapter consumer. Valid runs are compared with the same
callbacks routed directly. Each failure case is tied to a known way a decoder can return without delivery or
a handoff can fail acceptance: zero, multiple, raising, late, refused and unsupported callbacks, version
mismatch, overflow, foreign queued work and retained ownership.
"""
from __future__ import annotations

import pickle
import threading
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from tradingbot.ibkr_read_facade import ReadCallbackFacade
from tradingbot.paper_readiness.callback_router import CallbackRouter
from tradingbot.paper_readiness.decode_handoff import (
    MAX_CALLBACKS_PER_FRAME,
    DecodeHandoff,
    Entry,
    Failure,
    HandoffStatus,
    InventedDecoder,
    Step,
    StepKind,
)
from tradingbot.paper_readiness.read_framing import FramingLimits, FramingOutcome
from tradingbot.paper_readiness.transport import FakeClock

NOW = datetime(2026, 3, 4, 16, 0, tzinfo=UTC)
LAST = str(int((NOW - timedelta(seconds=10)).timestamp()))
CRID, QRID, SRID, PRID = 6001, 6002, 6003, 6004
CON_ID = 880001
ACCOUNT = "SYNTH-DECODE0001"
VERSION = 177  # an invented server version: provenance only, no wire meaning
CANARY = b"CANARY-DECODE-PAYLOAD"


class _Lock:
    def __init__(self) -> None:
        self._lock = threading.Lock()

    def __enter__(self):
        self._lock.acquire()

    def __exit__(self, *exc):
        self._lock.release()


class Connection:
    def __init__(self) -> None:
        self.connected, self.closed = True, True

    def is_connected(self):
        return self.connected

    def socket_closed(self):
        return self.closed


class Reader:
    def __init__(self) -> None:
        self.alive = True

    def join(self, timeout):
        self.alive = False

    def is_alive(self):
        return self.alive

    def partial_bytes(self):
        return 0


class Client:
    def __init__(self) -> None:
        self.connection, self.reader = Connection(), Reader()

    def connect(self):
        return self

    def stop_read(self, slot):
        pass

    def disconnect(self):
        self.connection.connected = False


class Contract:
    def __init__(self, con_id: int) -> None:
        self.conId, self.symbol, self.localSymbol, self.secType = con_id, "DECETF", "DECETF", "STK"
        self.currency, self.exchange, self.primaryExchange = "USD", "SMART", "INV-LISTING"


class Details:
    def __init__(self) -> None:
        self.contract = Contract(CON_ID)
        self.longName, self.tradingHours = "INVENTED DECODE ETF", "20260304:0400-20260304:2000"
        self.liquidHours, self.timeZoneId, self.minTick = "20260304:0930-20260304:1600", "America/New_York", 0.01


def call(entry: Entry, *args) -> Step:
    return Step(StepKind.CALL, entry, args)


# Invented payloads: whole-payload keys, deliberately not any wire format.
CONTRACT = (call(Entry.CONTRACT_DETAILS, CRID, Details()), call(Entry.CONTRACT_DETAILS_END, CRID))
QUOTE = (call(Entry.MARKET_DATA_TYPE, QRID, 1), call(Entry.TICK_PRICE, QRID, 1, 40.0, None),
         call(Entry.TICK_SIZE, QRID, 0, Decimal(200)), call(Entry.TICK_PRICE, QRID, 2, 40.02, None),
         call(Entry.TICK_SIZE, QRID, 3, Decimal(400)), call(Entry.TICK_STRING, QRID, 45, LAST))
QUOTE_END = (call(Entry.TICK_SNAPSHOT_END, QRID),)
SUMMARY = (call(Entry.ACCOUNT_SUMMARY, SRID, ACCOUNT, "AccountType", "INVENTED", ""),
           call(Entry.ACCOUNT_SUMMARY, SRID, ACCOUNT, "NetLiquidation", "50000.00", "USD"),
           call(Entry.ACCOUNT_SUMMARY, SRID, ACCOUNT, "TotalCashValue", "40000.00", "USD"),
           call(Entry.ACCOUNT_SUMMARY, SRID, ACCOUNT, "BuyingPower", "1.00", "USD"),
           call(Entry.ACCOUNT_SUMMARY_END, SRID))
POSITIONS = (call(Entry.POSITION, ACCOUNT, Contract(890001), Decimal("-3.5"), 12.5),
             call(Entry.POSITION, ACCOUNT, Contract(890002), Decimal(7), 3.0), call(Entry.POSITION_END))
CONTROL = {b"INV-contract": CONTRACT, b"INV-quote": QUOTE, b"INV-quote-end": QUOTE_END,
           b"INV-summary": SUMMARY, b"INV-positions": POSITIONS}
ORDER = (b"INV-contract", b"INV-quote", b"INV-quote-end", b"INV-summary", b"INV-positions")


def frame(payload: bytes) -> bytes:
    return len(payload).to_bytes(4, "big") + payload


def registered_router() -> CallbackRouter:
    router = CallbackRouter(Client(), _Lock(), FakeClock(NOW))
    router.start()
    router.register_contract(CRID)
    router.register_quote(QRID, CON_ID)
    router.register_account_summary(SRID)
    router.register_positions(PRID)
    return router


def handoff_for(scripts: dict, *, decoder_version: int = VERSION, limits=None):
    router = registered_router()
    decoder = InventedDecoder(decoder_version, scripts)
    return router, decoder, DecodeHandoff(router, decoder, server_version=VERSION, limits=limits)


def feed_fragmented(handoff: DecodeHandoff, payloads) -> None:
    stream = b"".join(frame(p) for p in payloads)
    for i in range(0, len(stream), 7):  # fragmented chunks: headers and payloads split across feeds
        handoff.feed(stream[i:i + 7])


def direct_router(payloads, scripts) -> CallbackRouter:
    """The same callbacks routed directly through the facade, as a client would without a handoff."""
    router = registered_router()
    facade = ReadCallbackFacade(router)
    for payload in payloads:
        for step in scripts[payload]:
            getattr(facade, step.entry.value)(*step.args)
    while router.consume_one():
        pass
    router.shutdown(join_timeout=0.01)
    return router


def outputs(router: CallbackRouter):
    return (router.contract_envelopes(), router.quote_envelopes(), router.account_summary_envelopes(),
            router.position_envelopes())


# ----------------------------------------------------------------------------- the valid composition


def test_control_owned_frames_decode_and_settle_exactly_as_direct_routing():
    router, _decoder, handoff = handoff_for(CONTROL)
    feed_fragmented(handoff, ORDER)
    assert handoff.process_available() == 5
    handoff.finish()
    router.shutdown(join_timeout=0.01)
    assert outputs(router) == outputs(direct_router(ORDER, CONTROL))
    report = handoff.report()
    assert [r.status for r in report.handoffs] == [HandoffStatus.SETTLED] * 5
    assert [r.callbacks for r in report.handoffs] == [2, 6, 1, 5, 3]
    assert report.handoffs[0].records_admitted == 0  # contract details stay outside ledger accounting
    assert [r.records_admitted for r in report.handoffs[1:]] == [6, 1, 5, 3]
    assert report.framing.outcome is FramingOutcome.COMPLETE and report.router_completed is True
    assert report.failure is None and report.held_sequence is None and report.completed is True


def test_one_frame_may_produce_several_callbacks_and_still_settle():
    scripts = dict(CONTROL)
    scripts[b"INV-quote"] = QUOTE + (call(Entry.TICK_SIZE, QRID, 0, Decimal(200)),)  # an extra converted size
    router, _decoder, handoff = handoff_for(scripts)
    feed_fragmented(handoff, ORDER)
    handoff.process_available()
    router.shutdown(join_timeout=0.01)
    assert outputs(router) == outputs(direct_router(ORDER, scripts))
    assert handoff.report().handoffs[1].callbacks == 7 and handoff.report().handoffs[1].status is HandoffStatus.SETTLED


def test_reaching_a_normalizer_is_not_stream_completion():
    router, _decoder, handoff = handoff_for(CONTROL)
    feed_fragmented(handoff, ORDER[:2])  # no snapshot END, no account or positions frames
    handoff.process_available()
    handoff.finish()
    router.shutdown(join_timeout=0.01)
    report = handoff.report()
    assert all(r.status is HandoffStatus.SETTLED for r in report.handoffs)
    assert report.framing.outcome is FramingOutcome.COMPLETE
    assert report.router_completed is False and report.completed is False


# ----------------------------------------------------------------------------- return without delivery


def test_a_callback_free_return_latches_and_keeps_the_frame_held():
    router, _decoder, handoff = handoff_for(CONTROL)
    feed_fragmented(handoff, (b"INV-unknown", *ORDER))
    assert handoff.process_available() == 0
    handoff.finish()
    report = handoff.report()
    assert report.failure is Failure.RETURNED_WITHOUT_DELIVERY and report.held_sequence == 1
    assert report.handoffs[0].status is HandoffStatus.FAILED
    assert report.framing.outcome is FramingOutcome.UNTAKEN_FRAMES  # later frames were never taken
    router.shutdown(join_timeout=0.01)
    assert report.completed is False and handoff.report().completed is False


@pytest.mark.parametrize("raise_at", [0, 3], ids=["before-callbacks", "after-callbacks"])
def test_a_decoder_exception_latches_even_after_callbacks_and_nothing_is_settled(raise_at):
    scripts = dict(CONTROL)
    scripts[b"INV-quote"] = QUOTE[:raise_at] + (Step(StepKind.RAISE),) + QUOTE[raise_at:]
    router, _decoder, handoff = handoff_for(scripts)
    feed_fragmented(handoff, ORDER)
    handoff.process_available()
    report = handoff.report()
    assert report.failure is Failure.DECODER_RAISED and report.held_sequence == 2
    assert report.handoffs[1].records_dispatched == 0
    assert router.report().queued_records == raise_at  # admitted before the raise, never settled here
    router.shutdown(join_timeout=0.01)
    assert handoff.report().completed is False


# ----------------------------------------------------------------------------- refused, unsupported, late


def test_an_unsupported_callback_latches_and_reaches_the_router():
    scripts = dict(CONTROL)
    scripts[b"INV-quote"] = (call(Entry.UNSUPPORTED),) + QUOTE  # e.g. a raw callback before conversion
    router, _decoder, handoff = handoff_for(scripts)
    feed_fragmented(handoff, ORDER)
    handoff.process_available()
    assert handoff.report().failure is Failure.UNSUPPORTED_CALLBACK
    assert "UNSUPPORTED_CALLBACK" in router.report().faults
    assert router.report().queued_records == 0  # the failed frame's later callbacks were not forwarded


def test_a_call_form_the_facade_refuses_is_a_router_refusal():
    scripts = dict(CONTROL)
    scripts[b"INV-quote"] = (call(Entry.TICK_PRICE, QRID, 1),) + QUOTE  # wrong arity
    _router, _decoder, handoff = handoff_for(scripts)
    feed_fragmented(handoff, ORDER)
    handoff.process_available()
    assert handoff.report().failure is Failure.ROUTER_REFUSED and handoff.report().held_sequence == 2


def test_a_late_callback_after_settlement_and_end_latches_and_revokes_completion():
    scripts = dict(CONTROL)
    scripts[b"INV-positions"] = (Step(StepKind.RETAIN_SINK),) + POSITIONS
    router, decoder, handoff = handoff_for(scripts)
    feed_fragmented(handoff, ORDER)
    handoff.process_available()
    handoff.finish()
    router.shutdown(join_timeout=0.01)
    assert handoff.report().completed is True
    decoder.call_retained(Entry.POSITION, ACCOUNT, Contract(890003), Decimal(1), 1.0)  # after END
    report = handoff.report()
    assert report.failure is Failure.LATE_CALLBACK and report.completed is False
    assert "ARRIVAL_AFTER_CLOSE" in router.report().faults  # the persistent refusal reached the router


def test_a_non_entry_through_a_kept_sink_is_refused():
    scripts = dict(CONTROL)
    scripts[b"INV-contract"] = (Step(StepKind.RETAIN_SINK),) + CONTRACT
    router, decoder, handoff = handoff_for(scripts)
    feed_fragmented(handoff, ORDER[:1])
    handoff.process_available()
    decoder._retained[0]._open = True  # simulate a sink still open to an unconverted name
    decoder._retained[0].deliver("tickPrice", QRID, 1, 40.0, None)
    assert handoff.report().failure is Failure.REFUSED_CALLBACK
    assert "UNSUPPORTED_CALLBACK" in router.report().faults


# ----------------------------------------------------------------------------- version, overflow, ownership


def test_a_decoder_version_other_than_the_pinned_provenance_never_decodes():
    router, _decoder, handoff = handoff_for(CONTROL, decoder_version=VERSION + 1)
    feed_fragmented(handoff, ORDER)
    handoff.process_available()
    report = handoff.report()
    assert report.failure is Failure.VERSION_MISMATCH and report.handoffs[0].callbacks == 0
    assert router.contract_envelopes() == []


def test_a_version_cleared_mid_stream_stops_the_next_frame():
    scripts = dict(CONTROL)
    scripts[b"INV-contract"] = CONTRACT + (Step(StepKind.CLEAR_VERSION),)
    _router, _decoder, handoff = handoff_for(scripts)
    feed_fragmented(handoff, ORDER)
    assert handoff.process_available() == 1
    report = handoff.report()
    assert report.failure is Failure.VERSION_MISMATCH and report.held_sequence == 2


def test_callback_overflow_in_one_frame_latches():
    scripts = dict(CONTROL)
    scripts[b"INV-quote"] = tuple(call(Entry.TICK_SIZE, QRID, 0, Decimal(200))
                                  for _ in range(MAX_CALLBACKS_PER_FRAME + 1))
    _router, _decoder, handoff = handoff_for(scripts)
    feed_fragmented(handoff, ORDER)
    handoff.process_available()
    assert handoff.report().failure is Failure.CALLBACK_OVERFLOW


def test_foreign_queued_work_is_never_attributed_to_a_frame():
    router, _decoder, handoff = handoff_for(CONTROL)
    ReadCallbackFacade(router).marketDataType(QRID, 1)  # queued outside the handoff
    feed_fragmented(handoff, ORDER)
    assert handoff.process_one() is False
    report = handoff.report()
    assert report.failure is Failure.FOREIGN_QUEUED_WORK and report.held_sequence is None
    assert report.framing.frames_queued == 5  # nothing was taken


def test_a_framing_refusal_blocks_completion_even_when_every_handoff_settled():
    limits = FramingLimits(max_chunk_bytes=8, max_payload_bytes=32, max_buffered_bytes=64)
    router, _decoder, handoff = handoff_for(CONTROL, limits=limits)
    feed_fragmented(handoff, ORDER)
    handoff.feed(b"x" * 9)  # an oversized chunk: refused, so framing is not clean
    handoff.process_available()
    handoff.finish()
    router.shutdown(join_timeout=0.01)
    report = handoff.report()
    assert all(r.status is HandoffStatus.SETTLED for r in report.handoffs) and report.router_completed is True
    assert report.framing.outcome is FramingOutcome.REFUSED and report.completed is False


def test_a_held_frame_blocks_every_later_frame_and_stays_reported():
    _router, _decoder, handoff = handoff_for(CONTROL)
    feed_fragmented(handoff, (b"INV-unknown", *ORDER))
    handoff.process_available()
    for _ in range(3):
        assert handoff.process_one() is False
    assert handoff.report().framing.frames_queued == 5 and handoff.report().held_sequence == 1


# ----------------------------------------------------------------------------- surface


def test_reports_and_reprs_are_payload_free_and_the_handoff_is_not_serializable():
    scripts = dict(CONTROL)
    scripts[CANARY] = QUOTE_END
    router, decoder, handoff = handoff_for(scripts)
    feed_fragmented(handoff, (b"INV-contract", CANARY))
    handoff.process_available()
    text = repr(handoff) + repr(handoff.report()) + repr(decoder) + repr(router.report())
    assert CANARY.decode() not in text and "INVENTED DECODE" not in text
    with pytest.raises(TypeError):
        pickle.dumps(handoff)


@pytest.mark.parametrize(("router", "decoder", "version"), [
    (object(), "decoder", VERSION), ("router", object(), VERSION), ("router", "decoder", 0),
    ("router", "decoder", True),
], ids=["router", "decoder", "zero-version", "bool-version"])
def test_construction_refuses_anything_but_the_exact_parts(router, decoder, version):
    real_router = registered_router()
    real_decoder = InventedDecoder(VERSION, CONTROL)
    with pytest.raises((TypeError, ValueError)):
        DecodeHandoff(real_router if router == "router" else router,
                      real_decoder if decoder == "decoder" else decoder, server_version=version)


def test_the_invented_decoder_is_closed():
    with pytest.raises((TypeError, ValueError)):
        InventedDecoder(VERSION, {"not-bytes": QUOTE})
    with pytest.raises((TypeError, ValueError)):
        InventedDecoder(VERSION, {b"x": [call(Entry.TICK_SNAPSHOT_END, QRID)]})
    with pytest.raises(TypeError):
        Step(StepKind.CALL, None)
    with pytest.raises(TypeError):
        Step(StepKind.RAISE, Entry.TICK_PRICE)


def test_reentry_is_refused():
    _router, _decoder, handoff = handoff_for(CONTROL)
    feed_fragmented(handoff, ORDER)
    handoff._busy = True  # simulate a call arriving while another call of this handoff runs
    assert handoff.process_one() is False
    handoff._busy = False
    assert handoff.report().failure is Failure.REENTERED


@pytest.mark.parametrize(("scripts_change", "failure"), [
    ({}, Failure.RETURNED_WITHOUT_DELIVERY),
    ({b"INV-unknown": (Step(StepKind.RAISE),)}, Failure.DECODER_RAISED),
], ids=["unknown-payload", "decoder-raised"])
def test_a_failed_frame_keeps_its_exact_payload_owned_and_reports_stay_payload_free(scripts_change, failure):
    """Review P2: take() removes the frame from the accumulator, so the held reservation must keep it."""
    scripts = dict(CONTROL)
    scripts.update(scripts_change)
    _router, _decoder, handoff = handoff_for(scripts)
    feed_fragmented(handoff, (b"INV-unknown", *ORDER))
    handoff.process_available()
    assert handoff.report().failure is failure
    held = handoff._held.frame  # private retention: the exact taken frame, still owned
    assert (held.sequence, held.payload) == (1, b"INV-unknown")
    assert "INV-unknown" not in repr(handoff.report()) + repr(handoff) + repr(held)


def test_a_version_mismatch_keeps_the_taken_frame_owned():
    _router, _decoder, handoff = handoff_for(CONTROL, decoder_version=VERSION + 1)
    feed_fragmented(handoff, ORDER)
    handoff.process_available()
    assert handoff.report().failure is Failure.VERSION_MISMATCH
    assert handoff._held.frame.payload == b"INV-contract"


def test_control_a_settled_frame_is_released():
    _router, _decoder, handoff = handoff_for(CONTROL)
    feed_fragmented(handoff, ORDER[:1])
    assert handoff.process_one() is True
    assert handoff._held is None and handoff.report().held_sequence is None
