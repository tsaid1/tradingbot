"""Decode handoff: owned frames -> closed invented decoding -> one router's consumer, on invented data only.

Every case composes the actual FrameAccumulator, the closed InventedDecoder, the actual CallbackRouter
(through the accepted ReadCallbackFacade) and its adapter consumer. Valid runs are compared with the same
callbacks routed directly. Each failure case is tied to a known way a decoder can return without delivery or
a handoff can fail acceptance: zero, multiple, raising, late, refused and unsupported callbacks, version
mismatch, overflow, foreign queued work and retained ownership.

The opt-in framed managedAccounts path composes the same parts with the handoff's internally built
ManagedAccountsInput: invented frame -> input -> explicit owner-mode registration -> router producers and
consumer. Every refusal is paired with the control, including refusals the router never sees.
"""
from __future__ import annotations

import pickle
import threading
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from tradingbot.ibkr_read_facade import ReadCallbackFacade
from tradingbot.paper_readiness.callback_router import CallbackRouter, StreamKind
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
from tradingbot.paper_readiness.managed_accounts_input import Arrival, Invalidation, Registration
from tradingbot.paper_readiness.read_framing import FramingLimits, FramingOutcome
from tradingbot.paper_readiness.transport import FORBIDDEN_NAME_RE, FakeClock

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


def test_a_refused_call_form_stops_every_later_queued_and_direct_callback_in_its_frame():
    """Review P2: the refusal latches at the forwarding call, before another step can forward."""
    scripts = dict(CONTROL)
    scripts[b"INV-quote"] = ((call(Entry.TICK_PRICE, QRID, 1),) + CONTRACT + QUOTE)  # refused, then direct, queued
    router, _decoder, handoff = handoff_for(scripts)
    feed_fragmented(handoff, (b"INV-quote",))
    handoff.process_available()
    report = handoff.report()
    assert report.failure is Failure.ROUTER_REFUSED and report.held_sequence == 1
    assert router.report().queued_records == 0  # no later quote callback was admitted
    assert router.contract_envelopes() == []  # no later direct contract callback was delivered
    assert report.handoffs[0].records_admitted == 0


@pytest.mark.parametrize(("script", "failure", "admitted"), [
    (QUOTE[:3] + (Step(StepKind.RAISE),), Failure.DECODER_RAISED, 3),
    (QUOTE[:2] + (call(Entry.UNSUPPORTED),), Failure.UNSUPPORTED_CALLBACK, 2),
    (tuple(call(Entry.TICK_SIZE, QRID, 0, Decimal(200)) for _ in range(MAX_CALLBACKS_PER_FRAME + 1)),
     Failure.CALLBACK_OVERFLOW, MAX_CALLBACKS_PER_FRAME),
], ids=["raise-after-three", "unsupported-after-two", "overflow-after-limit"])
def test_a_failed_record_counts_every_record_its_frame_admitted(script, failure, admitted):
    """Review P2: admission accounting is current before the failure record freezes."""
    scripts = dict(CONTROL)
    scripts[b"INV-quote"] = script
    router, _decoder, handoff = handoff_for(scripts)
    feed_fragmented(handoff, (b"INV-quote",))
    handoff.process_available()
    record = handoff.report().handoffs[0]
    assert (record.status, record.failure) == (HandoffStatus.FAILED, failure)
    assert (record.records_admitted, record.records_dispatched) == (admitted, 0)
    assert router.report().queued_records == admitted  # the record matches the router's own evidence
    assert handoff._held.frame.payload == b"INV-quote"  # and the frame stays owned


# ----------------------------------------------------------------------------- opt-in framed managed accounts

KEY = b"INVENTED-DECODE-KEY-0001"  # invented per-run keys, 24 bytes each
OTHER_KEY = b"INVENTED-DECODE-KEY-0002"
FOREIGN = "SYNTH-DECODEFRN1"
ACCOUNTS = (call(Entry.MANAGED_ACCOUNTS, ACCOUNT),)
FOREIGN_SUMMARY = tuple(call(st.entry, *(FOREIGN if a == ACCOUNT else a for a in st.args)) for st in SUMMARY)
WIRED = {**CONTROL, b"INV-accounts": ACCOUNTS, b"INV-accounts-again": ACCOUNTS,
         b"INV-summary-foreign": FOREIGN_SUMMARY,
         b"INV-accounts-malformed": (call(Entry.MANAGED_ACCOUNTS, ACCOUNT + ","),),
         b"INV-accounts-empty": (call(Entry.MANAGED_ACCOUNTS, ""),),
         b"INV-accounts-arity": (call(Entry.MANAGED_ACCOUNTS, ACCOUNT, ACCOUNT),),
         b"INV-accounts-ambiguous": (call(Entry.MANAGED_ACCOUNTS, f"SYNTH-DECODEOTH1,{ACCOUNT}"),),
         b"INV-error": (call(Entry.ERROR, SRID, 1772640000000, 321, "INVENTED error", ""),)}
READS = (b"INV-summary", b"INV-positions")


def account_router() -> CallbackRouter:
    """Started, with no read registered: owner-mode reads open only through explicit registration."""
    router = CallbackRouter(Client(), _Lock(), FakeClock(NOW))
    router.start()
    return router


def wired(router=None, *, expected=ACCOUNT, key=KEY):
    router = router if router is not None else account_router()
    handoff = DecodeHandoff(router, InventedDecoder(VERSION, WIRED), server_version=VERSION,
                            expected_account=expected, account_key=key)
    return router, handoff


def drive(handoff: DecodeHandoff, *payloads: bytes) -> int:
    feed_fragmented(handoff, payloads)
    return handoff.process_available()


def end(router: CallbackRouter, handoff: DecodeHandoff) -> None:
    handoff.finish()
    router.shutdown(join_timeout=0.01)
    handoff.close_accounts()


def references(router: CallbackRouter) -> set[str]:
    rows = {e.account_summary.account_id for e in router.account_summary_envelopes() if e.account_summary}
    return rows | {e.position.account_id for e in router.position_envelopes() if e.position}


def streams(router: CallbackRouter) -> dict:
    return {st.stream: (st.completed, st.issue_codes) for st in router.stream_status()}


def wired_control():
    router, handoff = wired()
    assert drive(handoff, b"INV-accounts") == 1
    handoff.register_reads(SRID, PRID)
    assert drive(handoff, *READS) == 2
    end(router, handoff)
    return router, handoff


def test_control_a_framed_snapshot_registers_owner_reads_and_the_input_report_joins_completion():
    router, handoff = account_router(), None
    router, handoff = wired(router)
    assert drive(handoff, b"INV-accounts") == 1
    # The frame settled with no router record, and no read opened: registration is never automatic.
    first = handoff.report()
    assert first.handoffs[0].status is HandoffStatus.SETTLED
    assert (first.handoffs[0].callbacks, first.handoffs[0].records_admitted) == (1, 0)
    assert router.report().adapter.ledger.slots == ()
    assert first.managed_accounts.registration is Registration.NOT_ATTEMPTED and not first.completed
    handoff.register_reads(SRID, PRID)
    assert {s.slot for s in router.report().adapter.ledger.slots} == {"ACCOUNT_SUMMARY", "POSITIONS"}
    assert drive(handoff, *READS) == 2
    end(router, handoff)
    report = handoff.report()
    accounts = report.managed_accounts
    assert (accounts.arrival, accounts.registration, accounts.invalidation) == (
        Arrival.EXACT, Registration.REGISTERED, None)
    assert accounts.completed and accounts.router_completed and accounts.closed
    assert report.failure is None and report.completed is True and router.report().faults == ()
    status = streams(router)
    assert status[StreamKind.ACCOUNT_SUMMARY] == status[StreamKind.POSITIONS] == (True, ())
    # Owner mode: every row carries the run's one keyed reference, never the invented identifier.
    [reference] = references(router)
    assert reference.startswith("acctref-v1:") and ACCOUNT not in reference


@pytest.mark.parametrize(("payload", "arrival"), [
    (b"INV-accounts-malformed", Arrival.MALFORMED), (b"INV-accounts-empty", Arrival.MALFORMED),
    (b"INV-accounts-arity", Arrival.MALFORMED), (b"INV-accounts-ambiguous", Arrival.AMBIGUOUS),
], ids=["trailing-separator", "empty", "arity", "ambiguous"])
def test_a_refused_snapshot_latches_before_registration_although_the_router_sees_nothing(payload, arrival):
    router, handoff = wired()
    assert drive(handoff, payload, *READS) == 0
    report = handoff.report()
    assert report.failure is Failure.ACCOUNTS_REFUSED and report.held_sequence == 1
    assert report.handoffs[0].status is HandoffStatus.FAILED
    assert router.report().faults == ()  # no router-fault delta: only the input's own report shows it
    assert report.managed_accounts.arrival is arrival
    handoff.register_reads(SRID, PRID)  # still explicit, and still refused: the router is not called
    assert handoff.report().managed_accounts.registration is Registration.NOT_READY
    assert router.report().adapter.ledger.slots == ()
    assert handoff.report().framing.frames_queued == 2  # later frames are never taken
    end(router, handoff)
    assert handoff.report().failure is Failure.ACCOUNTS_REFUSED and not handoff.report().completed


def test_a_mismatched_snapshot_never_registers_and_a_registration_before_arrival_is_refused():
    router, handoff = wired(expected="SYNTH-DECODENOT1")
    drive(handoff, b"INV-accounts")
    assert handoff.report().managed_accounts.arrival is Arrival.MISMATCH
    assert handoff.report().failure is Failure.ACCOUNTS_REFUSED and router.report().faults == ()
    early_router, early = wired()
    early.register_reads(SRID, PRID)  # before any arrival
    report = early.report()
    assert report.failure is Failure.ACCOUNTS_REFUSED
    assert report.managed_accounts.registration is Registration.NOT_READY
    assert early_router.report().adapter.ledger.slots == () and early_router.report().faults == ()


def test_a_duplicate_snapshot_before_registration_is_refused_without_a_router_fault():
    router, handoff = wired()
    assert drive(handoff, b"INV-accounts", b"INV-accounts-again") == 1
    report = handoff.report()
    assert (report.failure, report.held_sequence) == (Failure.ACCOUNTS_REFUSED, 2)
    assert report.managed_accounts.invalidation is Invalidation.DUPLICATE and router.report().faults == ()
    handoff.register_reads(SRID, PRID)
    assert handoff.report().managed_accounts.registration is Registration.NOT_READY


def test_a_duplicate_snapshot_after_every_end_invalidates_and_revokes_completion():
    router, control = wired_control()
    assert control.report().completed
    router, handoff = wired()
    drive(handoff, b"INV-accounts")
    handoff.register_reads(SRID, PRID)
    assert drive(handoff, *READS, b"INV-accounts-again") == 2  # both ENDs delivered, then the duplicate
    end(router, handoff)
    report = handoff.report()
    assert (report.failure, report.held_sequence) == (Failure.ACCOUNTS_REFUSED, 4)
    assert [r.status for r in report.handoffs] == [HandoffStatus.SETTLED] * 3 + [HandoffStatus.FAILED]
    assert report.managed_accounts.invalidation is Invalidation.DUPLICATE
    # The invalidation reaches the router as a persistent fault, after END too.
    assert router.report().faults == ("UNSUPPORTED_CALLBACK",) and not router.report().completed
    status = streams(router)
    assert status[StreamKind.ACCOUNT_SUMMARY] == (False, ("UNSUPPORTED_CALLBACK",))
    assert status[StreamKind.POSITIONS] == (False, ("UNSUPPORTED_CALLBACK",))
    assert not report.completed and not report.managed_accounts.completed


def test_a_snapshot_after_the_input_closed_is_late():
    router, handoff = wired()
    drive(handoff, b"INV-accounts")
    handoff.register_reads(SRID, PRID)
    drive(handoff, *READS)
    router.shutdown(join_timeout=0.01)
    handoff.close_accounts()  # the normal close, after shutdown: not an invalidation
    assert handoff.report().managed_accounts.invalidation is None
    drive(handoff, b"INV-accounts-again")  # framing input is still open; the input is not
    handoff.finish()
    report = handoff.report()
    assert report.failure is Failure.ACCOUNTS_REFUSED
    assert report.managed_accounts.invalidation is Invalidation.LATE and not report.completed
    assert router.report().faults == ("ARRIVAL_AFTER_CLOSE",)  # the router is shut down: still a fault


def test_the_owner_lives_only_as_long_as_the_input_on_the_same_router():
    router, handoff = wired()
    drive(handoff, b"INV-accounts")
    handoff.register_reads(SRID, PRID)
    drive(handoff, b"INV-summary")
    handoff.close_accounts()  # too early: the owner closes, so later rows cannot be projected
    drive(handoff, b"INV-positions")
    handoff.finish()
    router.shutdown(join_timeout=0.01)
    status = streams(router)
    assert status[StreamKind.ACCOUNT_SUMMARY] == (True, ())
    assert status[StreamKind.POSITIONS] == (False, ("MALFORMED_CALLBACK",) * 2)  # each closed-owner row
    assert "POSITION_END" not in {e.kind.value for e in router.position_envelopes()}  # END withheld
    # Delivery still settled: the closed owner shows as the stream's own failure, not a handoff failure.
    report = handoff.report()
    assert report.failure is None and all(r.status is HandoffStatus.SETTLED for r in report.handoffs)


def test_foreign_rows_and_another_run_never_share_the_runs_reference():
    control_router, _ = wired_control()
    [binding] = references(control_router)
    router, handoff = wired()
    drive(handoff, b"INV-accounts")
    handoff.register_reads(SRID, PRID)
    drive(handoff, b"INV-summary-foreign", b"INV-positions")
    end(router, handoff)
    summary_refs = {e.account_summary.account_id for e in router.account_summary_envelopes()
                    if e.account_summary}
    assert binding not in summary_refs and len(summary_refs) == 1  # a foreign row keeps its own reference
    other_router, other = wired(key=OTHER_KEY)  # another run: same invented identity, another key
    drive(other, b"INV-accounts")
    other.register_reads(SRID, PRID)
    drive(other, *READS)
    end(other_router, other)
    assert other.report().completed and binding not in references(other_router)
    # Nothing crosses handoffs: a fresh handoff cannot register on another's exact snapshot.
    _fresh_router, fresh = wired()
    fresh.register_reads(SRID, PRID)
    assert fresh.report().managed_accounts.registration is Registration.NOT_READY


def test_a_partial_registration_is_kept_invalidates_and_is_never_retried_or_replaced():
    router = account_router()
    router.register_positions(PRID)  # a read opened separately, outside the input
    router, handoff = wired(router)
    drive(handoff, b"INV-accounts")
    handoff.register_reads(SRID, PRID)
    report = handoff.report()
    assert report.failure is Failure.ACCOUNTS_REFUSED
    accounts = report.managed_accounts
    assert (accounts.registration, accounts.invalidation) == (
        Registration.PARTIAL, Invalidation.REGISTRATION_INCOMPLETE)
    assert set(router.report().faults) == {"POSITIONS_WINDOW_REOPENED", "UNSUPPORTED_CALLBACK"}
    handoff.register_reads(SRID, PRID)  # no retry
    accounts = handoff.report().managed_accounts
    assert accounts.registration is Registration.PARTIAL and accounts.invalidating_events == 2
    assert accounts.invalidation is Invalidation.REGISTRATION_INCOMPLETE  # the first stays latched
    end(router, handoff)
    assert not handoff.report().completed


def test_defaults_are_unchanged_without_the_opt_in():
    router = registered_router()
    handoff = DecodeHandoff(router, InventedDecoder(VERSION, WIRED), server_version=VERSION)
    drive(handoff, b"INV-accounts")
    report = handoff.report()
    # The facade's fixed refusal, exactly as a direct call: a router fault and ROUTER_REFUSED.
    direct = registered_router()
    ReadCallbackFacade(direct).managedAccounts(ACCOUNT)
    assert router.report().faults == direct.report().faults == ("UNSUPPORTED_CALLBACK",)
    assert (report.failure, report.held_sequence, report.managed_accounts) == (Failure.ROUTER_REFUSED, 1, None)
    unwired_router = account_router()
    unwired = DecodeHandoff(unwired_router, InventedDecoder(VERSION, WIRED), server_version=VERSION)
    unwired.register_reads(SRID, PRID)  # nothing is wired: never a no-owner registration
    assert unwired.report().failure is Failure.ACCOUNTS_REFUSED
    assert unwired_router.report().adapter.ledger.slots == () and unwired_router.report().faults == ()
    unwired.close_accounts()  # harmless without the opt-in
    assert unwired.report().managed_accounts is None


def test_errors_and_ends_behave_the_same_with_or_without_the_opt_in():
    plain_router = registered_router()
    plain = DecodeHandoff(plain_router, InventedDecoder(VERSION, WIRED), server_version=VERSION)
    drive(plain, b"INV-error", *READS)
    wired_router = account_router()
    wired_router.register_contract(CRID)
    wired_router.register_quote(QRID, CON_ID)
    wired_router, handoff = wired(wired_router)
    drive(handoff, b"INV-accounts")
    handoff.register_reads(SRID, PRID)
    drive(handoff, b"INV-error", *READS)
    a, b = plain.report(), handoff.report()
    assert a.failure == b.failure and a.failure is not Failure.ACCOUNTS_REFUSED
    assert [r.status for r in a.handoffs] == [r.status for r in b.handoffs[1:]]
    assert plain_router.report().faults == wired_router.report().faults
    assert streams(plain_router)[StreamKind.ACCOUNT_SUMMARY] == streams(wired_router)[StreamKind.ACCOUNT_SUMMARY]


@pytest.mark.parametrize(("expected", "key"), [
    (ACCOUNT, None), (None, KEY), (ACCOUNT, b"short"), ("", KEY), (ACCOUNT, "not-bytes"),
], ids=["no-key", "no-identifier", "short-key", "empty-identifier", "str-key"])
def test_the_opt_in_takes_both_exact_values_or_neither(expected, key):
    with pytest.raises(ValueError):
        DecodeHandoff(account_router(), InventedDecoder(VERSION, WIRED), server_version=VERSION,
                      expected_account=expected, account_key=key)


def test_the_input_is_never_exposed_and_registration_during_a_call_is_reentry():
    router, handoff = wired()
    public = {name for name in dir(handoff) if not name.startswith("_")}
    assert public == {"close_accounts", "feed", "finish", "process_available", "process_one", "register_reads",
                      "report"}
    for name in public:
        assert not FORBIDDEN_NAME_RE.search(name), name
    drive(handoff, b"INV-accounts")
    handoff._busy = True  # simulate a registration arriving while another call of this handoff runs
    handoff.register_reads(SRID, PRID)
    handoff._busy = False
    report = handoff.report()
    assert report.failure is Failure.REENTERED
    assert report.managed_accounts.registration is Registration.NOT_ATTEMPTED  # the input was not called
    assert router.report().adapter.ledger.slots == ()


def test_wired_reports_and_reprs_carry_no_identifier_or_key():
    router, handoff = wired_control()
    text = repr(handoff) + repr(handoff.report()) + repr(router.report())
    for secret in (ACCOUNT, KEY.decode(), FOREIGN):
        assert secret not in text
    with pytest.raises(TypeError):
        pickle.dumps(handoff)
