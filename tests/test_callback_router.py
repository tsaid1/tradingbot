"""SDK-free callback router over the merged lifecycle adapter and normalizers. Invented values only.

The control composes the actual adapter and normalizers: FIFO parity with direct feeding, three-slot
local cleanup, and contract output reported separately, outside lifecycle accounting. Paired
negatives: same-kind swaps, forged dispatch, mutation after enqueue, overflow, unknown and session
errors, a repeated positions window, post-END input and closure, and unknown cleanup.
"""
from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from tradingbot.paper_readiness import callback_router as cr
from tradingbot.paper_readiness.account_position_callbacks import (
    AccountSummaryCallbacks,
    PositionSnapshotCallbacks,
)
from tradingbot.paper_readiness.account_ref import REFERENCE_PREFIX
from tradingbot.paper_readiness.callback_router import CallbackRouter, StreamKind, StreamStatus
from tradingbot.paper_readiness.ibkr_callbacks import CallbackRequest, IbkrCallbackNormalizer
from tradingbot.paper_readiness.observations import ResponseKind
from tradingbot.paper_readiness.private_account import AccountOwner
from tradingbot.paper_readiness.read_retirement import ReadSlot
from tradingbot.paper_readiness.transport import FORBIDDEN_NAME_RE, FakeClock

NOW = datetime(2026, 3, 4, 16, 0, tzinfo=UTC)
CRID, QRID, SRID, PRID = 3001, 3002, 3003, 3004
CON_ID = 830001
ACCOUNT = "SYNTH-ROUTER0001"
LAST = str(int((NOW - timedelta(seconds=10)).timestamp()))
CANARY = "CANARY-ROUTER-TEXT"


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
    """An invented client double: connect, stop and disconnect only. The router supplies the queue."""

    def __init__(self) -> None:
        self.connection, self.reader = Connection(), Reader()
        self.stops: list = []

    def connect(self):
        return self

    def stop_read(self, slot):
        self.stops.append(slot)

    def disconnect(self):
        self.connection.connected = False


class Contract:
    def __init__(self, con_id: int) -> None:
        self.conId, self.symbol, self.localSymbol, self.secType = con_id, "RTRPOS", "RTRPOS", "STK"
        self.currency, self.exchange, self.primaryExchange = "USD", "SMART", "INV-LISTING"


class Details:
    def __init__(self) -> None:
        self.contract = Contract(CON_ID)
        self.longName, self.tradingHours = "INVENTED ROUTER ETF", "20260304:0400-20260304:2000"
        self.liquidHours, self.timeZoneId, self.minTick = "20260304:0930-20260304:1600", "America/New_York", 0.01


def router(client: Client | None = None) -> tuple[CallbackRouter, Client]:
    client = client or Client()
    r = CallbackRouter(client, _Lock(), FakeClock(NOW))
    r.start()
    r.register_contract(CRID)
    r.register_quote(QRID, CON_ID)
    r.register_account_summary(SRID)
    r.register_positions(PRID)
    return r, client


def quote_feed(target) -> None:
    target.marketDataType(QRID, 1)
    target.tickPrice(QRID, 1, 40.0, None)
    target.tickSize(QRID, 0, Decimal(200))
    target.tickPrice(QRID, 2, 40.02, None)
    target.tickSize(QRID, 3, Decimal(400))
    target.tickString(QRID, 45, LAST)
    target.tickSnapshotEnd(QRID)


def summary_feed(target) -> None:
    target.accountSummary(SRID, ACCOUNT, "AccountType", "INVENTED", "")
    for tag, value in (("NetLiquidation", "50000.00"), ("TotalCashValue", "40000.00"), ("BuyingPower", "1.00")):
        target.accountSummary(SRID, ACCOUNT, tag, value, "USD")
    target.accountSummaryEnd(SRID)


def positions_feed(target) -> None:
    target.position(ACCOUNT, Contract(840001), Decimal("-3.5"), 12.5)
    target.position(ACCOUNT, Contract(840002), Decimal(7), 3.0)
    target.positionEnd()


def everything(r: CallbackRouter) -> None:
    r.contractDetails(CRID, Details())
    r.contractDetailsEnd(CRID)
    quote_feed(r)
    summary_feed(r)
    positions_feed(r)


def drain(r: CallbackRouter) -> None:
    while r.consume_one():
        pass


def kinds(envelopes) -> list:
    return [e.kind for e in envelopes]


# ----------------------------------------------------------------------------- control

def test_actual_composition_matches_direct_feeding_and_cleans_up_three_slots():
    r, client = router()
    everything(r)
    drain(r)
    r.shutdown(join_timeout=0.01)
    report = r.report()
    assert report.completed is True and report.faults == () and report.queued_records == 0
    assert report.contract_details == "OUTSIDE_LIFECYCLE_ACCOUNTING"
    quote = IbkrCallbackNormalizer(QRID, CallbackRequest.QUOTE_SNAPSHOT, con_id=CON_ID, clock=FakeClock(NOW))
    summary, positions = AccountSummaryCallbacks(SRID), PositionSnapshotCallbacks(PRID)
    contract = IbkrCallbackNormalizer(CRID, CallbackRequest.CONTRACT_DETAILS)
    quote_feed(quote)
    summary_feed(summary)
    positions_feed(positions)
    contract.contractDetails(CRID, Details())
    contract.contractDetailsEnd(CRID)
    assert r.quote_envelopes() == quote.envelopes()  # FIFO parity
    assert r.account_summary_envelopes() == summary.envelopes()
    assert r.position_envelopes() == positions.envelopes()
    assert r.contract_envelopes() == contract.envelopes()
    slots = {s.slot for s in report.adapter.ledger.slots}
    assert slots == {"QUOTE_SNAPSHOT", "ACCOUNT_SUMMARY", "POSITIONS"}  # contract never borrows a slot
    assert report.adapter.ledger.local_quiescence_established is True
    assert report.adapter.ledger.broker_side_retirement == "NOT_ESTABLISHED_BY_INSPECTED_SOURCES"
    assert client.stops == [cr.ReadSlot.ACCOUNT_SUMMARY, cr.ReadSlot.POSITIONS]


# ----------------------------------------------------------------------------- payload ownership

def test_two_same_kind_payloads_cannot_be_reordered_by_a_second_fetch():
    r, _ = router()
    r.tickPrice(QRID, 1, 40.0, None)
    r.tickPrice(QRID, 2, 40.02, None)  # same (slot, kind) as the first
    assert r._queue.get_nowait() == (cr.ReadSlot.QUOTE_SNAPSHOT, cr.CallbackKind.DATA)
    assert r._queue.get_nowait() is None  # a second fetch while one is outstanding is refused
    assert "FETCH_WHILE_OUTSTANDING" in r.report().faults
    assert r._queue.size() == 2  # nothing was lost


def test_direct_or_mismatched_dispatch_delivers_nothing():
    r, _ = router()
    boundary = r._adapter._boundary
    boundary.dispatch(cr.ReadSlot.QUOTE_SNAPSHOT, cr.CallbackKind.DATA)  # nothing fetched
    r.tickPrice(QRID, 1, 40.0, None)
    r._queue.get_nowait()
    boundary.dispatch(cr.ReadSlot.ACCOUNT_SUMMARY, cr.CallbackKind.DATA)  # not the fetched record
    assert {"DISPATCH_WITHOUT_FETCH", "DISPATCH_MISMATCH"} <= set(r.report().faults)
    assert r.quote_envelopes() == []
    assert not hasattr(CallbackRouter, "dispatch")  # no public dispatch to forge


def test_a_payload_mutated_after_enqueue_is_delivered_as_enqueued():
    r, _ = router()
    contract = Contract(840001)
    r.position(ACCOUNT, contract, Decimal(1), 1.0)
    contract.conId, contract.symbol = 999999, "CHANGED"
    r.positionEnd()
    drain(r)
    line = next(e.position for e in r.position_envelopes() if e.kind == ResponseKind.POSITION)
    assert (line.con_id, line.symbol) == (840001, "RTRPOS")


class ReentrantContract(Contract):
    """A contract whose field getter calls back into the router during snapshotting."""

    def __init__(self, con_id: int, router: CallbackRouter) -> None:
        super().__init__(con_id)
        self._router, self._fired = router, False

    def __getattribute__(self, name):
        if name == "conId" and not object.__getattribute__(self, "_fired"):
            object.__setattr__(self, "_fired", True)
            object.__getattribute__(self, "_router").position(ACCOUNT, Contract(849999), Decimal(1), 1.0)
        return object.__getattribute__(self, name)


def test_a_reentrant_contract_getter_is_refused_and_its_payload_never_delivered():
    """Review P2: the guard covers snapshotting, not only admission."""
    r, _ = router()
    everything(r)
    r.position(ACCOUNT, ReentrantContract(840003, r), Decimal(1), 1.0)
    drain(r)
    r.shutdown(join_timeout=0.01)
    report = r.report()
    con_ids = {e.position.con_id for e in r.position_envelopes() if e.kind == ResponseKind.POSITION}
    assert 849999 not in con_ids and 840003 in con_ids  # the outer row arrived, the nested one never did
    assert "ARRIVAL_REENTERED" in report.faults and report.completed is False


class ReentrantClock:
    """A clock whose now() calls back into the router while quote END is being dispatched."""

    def __init__(self) -> None:
        self.router: CallbackRouter | None = None
        self.calls = 0

    def now(self):
        self.calls += 1
        if self.router is not None:
            self.router.position(ACCOUNT, Contract(849998), Decimal(1), 1.0)
        return NOW


def test_an_arrival_during_dispatch_is_refused_and_its_payload_never_delivered():
    clock = ReentrantClock()
    r = CallbackRouter(Client(), _Lock(), clock)
    r.start()
    r.register_quote(QRID, CON_ID)
    r.register_positions(PRID)
    clock.router = r
    quote_feed(r)
    r.positionEnd()
    drain(r)
    r.shutdown(join_timeout=0.01)
    report = r.report()
    assert clock.calls >= 1  # tickSnapshotEnd read the clock during dispatch
    assert not [e for e in r.position_envelopes() if e.kind == ResponseKind.POSITION]
    assert "ARRIVAL_REENTERED" in report.faults and report.completed is False


def test_control_an_ordinary_arrival_after_a_refused_reentry_still_routes():
    r, _ = router()
    r.position(ACCOUNT, ReentrantContract(840003, r), Decimal(1), 1.0)
    r.position(ACCOUNT, Contract(840004), Decimal(2), 1.0)  # not reentrant: routed normally
    r.positionEnd()
    drain(r)
    con_ids = [e.position.con_id for e in r.position_envelopes() if e.kind == ResponseKind.POSITION]
    assert con_ids == [840003, 840004]


def test_overflow_is_refused_before_admission_and_blocks_completion():
    r, _ = router()
    for i in range(cr.MAX_QUEUE_RECORDS + 1):
        r.tickGeneric(QRID, 6, float(i))
    report = r.report()
    assert "QUEUE_OVERFLOW" in report.faults and report.queued_records == cr.MAX_QUEUE_RECORDS
    assert report.adapter.ledger.queued_callbacks == cr.MAX_QUEUE_RECORDS  # the refused one was not admitted


def test_an_unretainable_value_still_fails_as_malformed():
    r, _ = router()
    marker = object()
    r.tickPrice(QRID, 1, marker, None)
    r.tickSnapshotEnd(QRID)
    drain(r)
    assert all(e.quote is None or marker not in vars(e.quote).values() for e in r.quote_envelopes())
    assert ResponseKind.TICK_SNAPSHOT_END not in kinds(r.quote_envelopes())  # the malformed fault held


@pytest.mark.parametrize("quantity", [Decimal("0." + "1" * 1_000_000), Decimal("1E+1000000000"),
                                      Decimal("1E-1000000000")],
                         ids=["large-coefficient", "large-exponent", "small-exponent"])
def test_an_unbounded_decimal_is_not_retained_and_still_fails(quantity):
    """Review P2: bounded before retention, without expanding it downstream."""
    r, _ = router()
    r.position(ACCOUNT, Contract(840001), quantity, 1.0)
    r.tickSize(QRID, 0, quantity)
    r.positionEnd()
    r.tickSnapshotEnd(QRID)
    drain(r)
    assert ResponseKind.POSITION_END not in kinds(r.position_envelopes())
    assert ResponseKind.TICK_SNAPSHOT_END not in kinds(r.quote_envelopes())
    assert not [e for e in r.position_envelopes() if e.kind == ResponseKind.POSITION]


def test_bounded_decimals_at_the_limits_still_route():
    r, _ = router()
    r.position(ACCOUNT, Contract(840001), Decimal("1" * 29), 1.0)  # inside the normalizer's own bound too
    r.position(ACCOUNT, Contract(840002), Decimal("1E-40"), 1.0)  # the router's exponent bound, inclusive
    r.positionEnd()
    drain(r)
    lines = [e.position for e in r.position_envelopes() if e.kind == ResponseKind.POSITION]
    assert [line.position for line in lines] == ["1" * 29, "1E-40"]


# ----------------------------------------------------------------------------- routing rules

def test_an_unknown_request_id_is_a_recorded_stray_with_its_original_id():
    r, _ = router()
    r.tickPrice(QRID + 50, 1, 40.0, None)
    r.accountSummary(SRID + 50, ACCOUNT, "NetLiquidation", "1.00", "USD")
    r.error_1052(9999, 0, 200, CANARY)
    report = r.report()
    assert report.faults.count("UNKNOWN_REQUEST_ID") == 3
    assert {e.request_id for e in r.quote_envelopes()} == {QRID + 50}
    assert {e.request_id for e in r.account_summary_envelopes()} == {SRID + 50}
    assert 9999 in {e.request_id for e in r.contract_envelopes()}
    assert CANARY not in repr(r.contract_envelopes()) + repr(report)


def test_an_unattributable_error_latches_both_error_streams():
    r, _ = router()
    everything(r)
    r.error_1052("not-an-id", 0, 200, CANARY)
    drain(r)
    assert ResponseKind.CONTRACT_DETAILS_END not in kinds(r.contract_envelopes())
    assert ResponseKind.TICK_SNAPSHOT_END not in kinds(r.quote_envelopes())
    assert "UNKNOWN_REQUEST_ID" in r.report().faults


@pytest.mark.parametrize(("code", "fails"), [(2104, False), (1100, True)])
def test_session_errors_reach_every_stream_under_the_existing_ok_policy(code, fails):
    r, _ = router()
    everything(r)
    r.error_1052(-1, 0, code, CANARY)
    drain(r)
    r.shutdown(join_timeout=0.01)
    report = r.report()
    assert report.completed is (not fails)
    assert ("SESSION_ERROR" in report.faults) is fails


def account_only_router() -> CallbackRouter:
    r = CallbackRouter(Client(), _Lock(), FakeClock(NOW))
    r.start()
    r.register_account_summary(SRID)
    r.register_positions(PRID)
    summary_feed(r)
    positions_feed(r)
    return r


@pytest.mark.parametrize("args", [
    (-1, "bad-time", 2104, "ignored"), (-1, True, 2104, "ignored"), (-1, 0.0, 2104, "ignored"),
    (-1, 0, 2104), (-1, 0, 2104, "a", "b", "c"),
    (-1, 0, True, "x"), (-1, 0, 2104.0, "x"), (-1, 0, Decimal(2104), "x"), (-1, 0, -2104, "x"),
    (-1.0, 0, 2104, "ignored"), (Decimal(-1), 0, 2104, "ignored"),
], ids=["time-text", "time-bool", "time-float", "three-arguments", "six-arguments",
        "code-bool", "code-float", "code-decimal", "code-negative", "id-float", "id-decimal"])
def test_a_malformed_session_error_fails_account_and_position_streams(args):
    """Review P2: the OK-code exemption applies only to a well-formed 1052 call."""
    r = account_only_router()
    r.error_1052(*args)
    drain(r)
    r.shutdown(join_timeout=0.01)
    report = r.report()
    assert "SESSION_ERROR_MALFORMED" in report.faults and report.completed is False
    assert ResponseKind.ACCOUNT_SUMMARY_END not in kinds(r.account_summary_envelopes())
    assert ResponseKind.POSITION_END not in kinds(r.position_envelopes())


def test_control_a_well_formed_ok_session_error_with_only_account_streams_completes():
    r = account_only_router()
    r.error_1052(-1, 0, 2104, "ignored")
    drain(r)
    r.shutdown(join_timeout=0.01)
    assert r.report().faults == () and r.report().completed is True


def test_account_errors_are_fixed_refusals_with_a_lifecycle_error():
    r, _ = router()
    everything(r)
    r.error_1052(SRID, 0, 321, CANARY)
    drain(r)
    r.shutdown(join_timeout=0.01)
    report = r.report()
    summary = next(s for s in report.adapter.ledger.slots if s.slot == "ACCOUNT_SUMMARY")
    assert report.completed is False and summary.errored is False  # arrived after END: post-END fault
    assert "POST_END_EVENT" in report.adapter.ledger.faults
    assert ResponseKind.ACCOUNT_SUMMARY_END not in kinds(r.account_summary_envelopes())


def test_the_positions_window_opens_once():
    r, _ = router()
    r.register_positions(PRID + 1)
    assert "POSITIONS_WINDOW_REOPENED" in r.report().faults


def test_duplicate_or_unopened_registration_is_refused():
    r, _ = router()
    r.register_quote(QRID, CON_ID)
    early = CallbackRouter(Client(), _Lock(), FakeClock(NOW))
    early.register_quote(QRID, CON_ID)  # before start
    assert "REGISTRATION_REFUSED" in r.report().faults
    assert "ADAPTER_NOT_OPEN" in early.report().faults


def test_post_end_input_is_kept_and_closure_rejects_new_arrivals():
    r, _ = router()
    everything(r)
    r.tickPrice(QRID, 1, 41.0, None)  # after the snapshot END
    drain(r)
    r.shutdown(join_timeout=0.01)
    r.tickPrice(QRID, 1, 42.0, None)  # after closure
    report = r.report()
    assert "POST_END_EVENT" in report.adapter.ledger.faults and "ARRIVAL_AFTER_CLOSE" in report.faults
    assert report.completed is False


def test_unknown_socket_cleanup_never_completes():
    client = Client()
    client.connection.closed = None
    r, _ = router(client)
    everything(r)
    drain(r)
    r.shutdown(join_timeout=0.01)
    report = r.report()
    assert report.adapter.ledger.socket == "UNKNOWN" and report.completed is False


def test_missing_currency_and_oversized_quantity_evidence_is_preserved():
    r, _ = router()
    r.accountSummary(SRID, ACCOUNT, "BuyingPower", "1.00", "")
    r.position(ACCOUNT, Contract(840001), 10**5000, 1.0)
    r.positionEnd()
    r.accountSummaryEnd(SRID)
    drain(r)
    row = next(e.account_summary for e in r.account_summary_envelopes() if e.kind == ResponseKind.ACCOUNT_SUMMARY)
    assert row.currency is None
    assert ResponseKind.POSITION_END not in kinds(r.position_envelopes())


def test_surface_has_no_forbidden_names_and_no_payload_repr():
    r, _ = router()
    r.accountSummary(SRID, ACCOUNT, "NetLiquidation", "12345.67", "USD")
    for name in [n for n in dir(CallbackRouter) if not n.startswith("_")] + cr.__all__:
        assert not FORBIDDEN_NAME_RE.search(name), name
    assert "12345.67" not in repr(r) + repr(r.report()) and ACCOUNT not in repr(r.report())
    with pytest.raises(TypeError):
        import pickle
        pickle.dumps(r)


# ----------------------------------------------------------------------------- direct callbacks and hooks


class HookClient(Client):
    """A client double that calls back into the router from inside its connect, stop or disconnect hook."""

    def __init__(self, on_connect=None, on_stop=None, on_disconnect=None) -> None:
        super().__init__()
        self.router: CallbackRouter | None = None
        self.on_connect, self.on_stop, self.on_disconnect = on_connect, on_stop, on_disconnect

    def connect(self):
        if self.on_connect is not None:
            self.on_connect(self.router)
        return self

    def stop_read(self, slot):
        super().stop_read(slot)
        if self.on_stop is not None:
            self.on_stop(self.router, slot)

    def disconnect(self):
        super().disconnect()
        if self.on_disconnect is not None:
            self.on_disconnect(self.router)


def hooked(**hooks) -> tuple[CallbackRouter, HookClient]:
    client = HookClient(**hooks)
    r = CallbackRouter(client, _Lock(), FakeClock(NOW))
    client.router = r
    r.start()
    r.register_contract(CRID)
    r.register_quote(QRID, CON_ID)
    r.register_account_summary(SRID)
    r.register_positions(PRID)
    return r, client


def full_run(r: CallbackRouter):
    everything(r)
    drain(r)
    r.shutdown(join_timeout=0.01)
    return r.report()


def slot_view(report, slot: ReadSlot):
    return next(s for s in report.adapter.ledger.slots if s.slot == slot.value)


@pytest.mark.parametrize("name", ["inventedAuxiliaryCallback", "inventedProtoVariant"])
def test_an_unsupported_callback_before_any_registration_is_a_persistent_failure(name):
    """Review: with no stream registered, the old route reached no normalizer and left no fault."""
    client = Client()
    r = CallbackRouter(client, _Lock(), FakeClock(NOW))
    r.start()
    r.unsupported_callback(name)
    r.register_contract(CRID)
    r.register_quote(QRID, CON_ID)
    r.register_account_summary(SRID)
    r.register_positions(PRID)
    report = full_run(r)
    assert "UNSUPPORTED_CALLBACK" in report.faults and report.completed is False
    assert name not in repr(report)


def test_an_owned_error_raised_inside_its_stop_hook_settles_the_attempt_unknown():
    """Review: the error was only queued for the normalizer, so the stop settled as a clean return."""
    def on_stop(r, slot):
        if slot is ReadSlot.ACCOUNT_SUMMARY:
            r.error_1052(SRID, 0, 300, CANARY)

    r, client = hooked(on_stop=on_stop)
    report = full_run(r)
    assert client.stops == [ReadSlot.ACCOUNT_SUMMARY, ReadSlot.POSITIONS]
    assert "ERROR_DURING_STOP" in report.adapter.failures
    assert slot_view(report, ReadSlot.ACCOUNT_SUMMARY).retirement_attempt == "UNKNOWN"
    assert report.completed is False and CANARY not in repr(report)


def test_control_connect_and_disconnect_hook_notifications_are_recorded_and_complete():
    r, _ = hooked(on_connect=lambda r: r.connectAck(), on_disconnect=lambda r: r.connectionClosed())
    report = full_run(r)
    assert report.adapter.direct_callbacks == ("CONNECT_ACK", "CONNECTION_CLOSED")
    assert report.faults == () and report.completed is True


def test_connection_closed_is_an_observation_never_socket_closure():
    r, client = hooked(on_connect=lambda r: r.connectAck(), on_disconnect=lambda r: r.connectionClosed())
    client.connection.closed = False  # the socket observation disagrees with the callback
    report = full_run(r)
    assert "CONNECTION_CLOSED" in report.adapter.direct_callbacks
    assert "SOCKET_NOT_CLOSED" in report.adapter.failures and report.completed is False


def test_direct_callbacks_after_closure_are_refused_and_not_recorded():
    r, _ = router()
    full_run(r)
    r.connectAck()
    r.connectionClosed()
    report = r.report()
    assert report.adapter.direct_callbacks == ()
    assert report.faults.count("ARRIVAL_AFTER_CLOSE") == 2 and report.completed is False


class AckingContract(Contract):
    """A contract whose field getter calls a direct callback while the router is snapshotting it."""

    def __init__(self, con_id: int, router: CallbackRouter) -> None:
        super().__init__(con_id)
        self._router = router

    def __getattribute__(self, name):
        if name == "conId":
            object.__getattribute__(self, "_router").connectAck()
        return object.__getattribute__(self, name)


class ClosingClock:
    """A clock whose now() calls connectionClosed while quote END is being dispatched."""

    def __init__(self) -> None:
        self.router: CallbackRouter | None = None

    def now(self):
        if self.router is not None:
            self.router.connectionClosed()
        return NOW


def test_a_direct_callback_nested_in_an_arrival_or_a_dispatch_is_refused():
    r, _ = router()
    r.position(ACCOUNT, AckingContract(840009, r), Decimal(1), 1.0)
    assert r.report().faults == ("ARRIVAL_REENTERED",) and r.report().adapter.direct_callbacks == ()

    clock = ClosingClock()
    r2 = CallbackRouter(Client(), _Lock(), clock)
    r2.start()
    r2.register_quote(QRID, CON_ID)
    quote_feed(r2)
    clock.router = r2
    drain(r2)
    assert "ARRIVAL_REENTERED" in r2.report().faults and r2.report().adapter.direct_callbacks == ()


def test_an_unsupported_callback_for_a_registered_owner_keeps_the_owner_refusal():
    r, _ = router()
    r.unsupported_callback("inventedAuxiliaryCallback", QRID)
    report = full_run(r)
    assert ResponseKind.TICK_SNAPSHOT_END not in kinds(r.quote_envelopes())  # the normalizer refusal
    assert ResponseKind.CONTRACT_DETAILS_END in kinds(r.contract_envelopes())  # only the owner
    assert "UNSUPPORTED_CALLBACK" in report.faults and report.completed is False


def test_control_a_stop_hook_that_returns_normally_settles_returned():
    r, client = hooked()
    report = full_run(r)
    assert client.stops == [ReadSlot.ACCOUNT_SUMMARY, ReadSlot.POSITIONS]
    assert slot_view(report, ReadSlot.ACCOUNT_SUMMARY).retirement_attempt == "RETURNED"
    assert report.completed is True


@pytest.mark.parametrize(("slot", "args"), [
    (ReadSlot.POSITIONS, (-1, 0, 1100, CANARY)),
    (ReadSlot.POSITIONS, (-1, "bad-time", 2104, CANARY)),
], ids=["session-non-ok", "session-malformed"])
def test_a_session_error_inside_any_stop_hook_settles_that_attempt_unknown(slot, args):
    def on_stop(r, stopped):
        if stopped is slot:
            r.error_1052(*args)

    r, _ = hooked(on_stop=on_stop)
    report = full_run(r)
    assert "ERROR_DURING_STOP" in report.adapter.failures
    assert slot_view(report, slot).retirement_attempt == "UNKNOWN" and report.completed is False


@pytest.mark.parametrize("args", [(-1, 0, 2104, "ignored"), (CRID, 0, 200, "ignored")],
                         ids=["informational-session-code", "another-request"])
def test_an_inapplicable_error_inside_a_stop_hook_does_not_taint_the_stop(args):
    def on_stop(r, slot):
        if slot is ReadSlot.ACCOUNT_SUMMARY:
            r.error_1052(*args)

    r, _ = hooked(on_stop=on_stop)
    report = full_run(r)
    assert "ERROR_DURING_STOP" not in report.adapter.failures
    assert slot_view(report, ReadSlot.ACCOUNT_SUMMARY).retirement_attempt == "RETURNED"


def test_an_error_outside_any_stop_hook_is_not_reported_as_a_direct_error():
    r, _ = router()
    r.error_1052(SRID, 0, 321, CANARY)
    drain(r)
    assert "DIRECT_ERROR" not in r.report().adapter.failures


# ----------------------------------------------------------------------------- INT-1: owner-mode registration

EXPECTED_ACCOUNT, FOREIGN_ACCOUNT = "INVENTEDACCT01", "INVENTEDACCT02"  # invented, deliberately not SYNTH-shaped
OWNER_KEY = b"r" * 32


def account_owner(managed=(EXPECTED_ACCOUNT,)) -> AccountOwner:
    return AccountOwner(EXPECTED_ACCOUNT, list(managed), OWNER_KEY)


def owner_router(owner) -> CallbackRouter:
    r = CallbackRouter(Client(), _Lock(), FakeClock(NOW))
    r.start()
    r.register_account_summary(SRID, owner=owner)
    r.register_positions(PRID, owner=owner)
    return r


def owned_summary(target, account=EXPECTED_ACCOUNT) -> None:
    target.accountSummary(SRID, account, "AccountType", "INVENTED", "")
    for tag, value in (("NetLiquidation", "50000.00"), ("TotalCashValue", "40000.00"), ("BuyingPower", "1.00")):
        target.accountSummary(SRID, account, tag, value, "USD")
    target.accountSummaryEnd(SRID)


def owned_positions(target, account=EXPECTED_ACCOUNT) -> None:
    target.position(account, Contract(840001), Decimal("-3.5"), 12.5)
    target.position(account, Contract(840002), Decimal(7), 3.0)
    target.positionEnd()


def direct_producers(owner):
    return AccountSummaryCallbacks(SRID, owner=owner), PositionSnapshotCallbacks(PRID, owner=owner)


def assert_parity(r: CallbackRouter, summary, positions) -> None:
    assert r.account_summary_envelopes() == summary.envelopes()
    assert r.position_envelopes() == positions.envelopes()
    assert r._account.issues == summary.issues and r._positions.issues == positions.issues


def test_owner_mode_routing_matches_direct_owner_mode_producers_and_projects_references():
    owner = account_owner()
    r = owner_router(owner)
    owned_summary(r)
    owned_positions(r)
    drain(r)
    r.shutdown(join_timeout=0.01)
    summary, positions = direct_producers(owner)
    owned_summary(summary)
    owned_positions(positions)
    assert_parity(r, summary, positions)
    text = repr(r.account_summary_envelopes()) + repr(r.position_envelopes()) + repr(r.report())
    assert owner.binding.reference in text and EXPECTED_ACCOUNT not in text
    assert ResponseKind.POSITION_END in kinds(r.position_envelopes())
    assert r.report().completed is True and r.report().faults == ()
    assert owner.owns(owner.binding)  # the caller's owner is never closed by the router


def test_a_foreign_identity_keeps_its_own_distinct_reference_through_the_router():
    owner = account_owner()
    r = owner_router(owner)
    owned_positions(r, FOREIGN_ACCOUNT)
    drain(r)
    _summary, positions = direct_producers(owner)
    owned_positions(positions, FOREIGN_ACCOUNT)
    assert r.position_envelopes() == positions.envelopes()
    text = repr(r.position_envelopes())
    assert owner.binding.reference not in text and FOREIGN_ACCOUNT not in text
    assert REFERENCE_PREFIX in text  # a distinct keyed reference, so downstream mismatch evidence survives


@pytest.mark.parametrize("account", [123, "x" * 5000, None], ids=["int", "oversized", "none"])
def test_a_malformed_identity_is_refused_and_withholds_end_exactly_as_directly(account):
    owner = account_owner()
    r = owner_router(owner)
    owned_positions(r, account)
    drain(r)
    _, positions = direct_producers(owner)
    owned_positions(positions, account)
    assert r.position_envelopes() == positions.envelopes() and r._positions.issues == positions.issues
    assert ResponseKind.POSITION_END not in kinds(r.position_envelopes()) and r._positions.issues


def test_errors_duplicates_and_post_end_input_fail_exactly_as_directly():
    owner = account_owner()
    r = owner_router(owner)
    owned_summary(r)
    r.accountSummaryEnd(SRID)  # duplicate END
    r.error_1052(SRID, 0, 321, CANARY)  # account error: a fixed refusal plus a lifecycle ERROR
    owned_positions(r)
    r.position(EXPECTED_ACCOUNT, Contract(840003), Decimal(1), 1.0)  # after positionEnd
    drain(r)
    summary, positions = direct_producers(owner)
    owned_summary(summary)
    summary.accountSummaryEnd(SRID)
    summary.unsupported_callback("error")
    owned_positions(positions)
    positions.position(EXPECTED_ACCOUNT, Contract(840003), Decimal(1), 1.0)
    assert_parity(r, summary, positions)  # the producers judge duplicates, errors and post-END rows
    assert ResponseKind.ACCOUNT_SUMMARY_END not in kinds(r.account_summary_envelopes())
    assert "POST_END_EVENT" in r.report().adapter.ledger.faults  # and the lifecycle records the late rows
    assert CANARY not in repr(r.report())


def closed_owner():
    owner = account_owner()
    owner.close()
    return owner


@pytest.mark.parametrize("make", [
    lambda: object(), lambda: account_owner().binding, closed_owner,
    lambda: account_owner((FOREIGN_ACCOUNT,)), lambda: type("Sub", (), {})(),
], ids=["object", "binding", "closed", "unbound-mismatch", "foreign-type"])
def test_a_refused_owner_latches_a_fixed_fault_and_opens_no_slot(make):
    r = CallbackRouter(Client(), _Lock(), FakeClock(NOW))
    r.start()
    r.register_account_summary(SRID, owner=make())
    r.register_positions(PRID, owner=make())
    report = r.report()
    assert report.faults.count("OWNER_REFUSED") == 2
    assert report.adapter.ledger.slots == ()  # no producer stored, no ledger slot opened
    assert r._account is None and r._positions is None
    r.accountSummary(SRID, EXPECTED_ACCOUNT, "NetLiquidation", "1.00", "USD")
    assert "UNROUTABLE_CALLBACK" in r.report().faults  # nothing can arrive at a refused stream
    r.shutdown(join_timeout=0.01)
    assert r.report().completed is False and EXPECTED_ACCOUNT not in repr(r.report())


def test_closing_the_owner_between_enqueue_and_delivery_fails_never_as_empty_success():
    owner = account_owner()
    r = owner_router(owner)
    owned_positions(r)
    assert r.report().queued_records == 3  # enqueued, not yet delivered
    owner.close()
    drain(r)
    _, positions = direct_producers(account_owner())
    owned_positions(positions)
    assert r.position_envelopes() != positions.envelopes()
    assert r._positions.issues and ResponseKind.POSITION_END not in kinds(r.position_envelopes())
    assert not [e for e in r.position_envelopes() if e.kind == ResponseKind.POSITION]


def test_shutdown_drains_queued_rows_while_the_caller_keeps_the_owner_open():
    owner = account_owner()
    r = owner_router(owner)
    owned_summary(r)
    owned_positions(r)
    r.shutdown(join_timeout=0.01)  # the adapter's drain delivers every queued record, projected
    summary, positions = direct_producers(owner)
    owned_summary(summary)
    owned_positions(positions)
    assert_parity(r, summary, positions)
    assert r.report().queued_records == 0 and owner.owns(owner.binding)
    # Rows drained after the stop attempts are late, exactly as without an owner: owner mode changes no
    # lifecycle verdict.
    without = CallbackRouter(Client(), _Lock(), FakeClock(NOW))
    without.start()
    without.register_account_summary(SRID)
    without.register_positions(PRID)
    summary_feed(without)
    positions_feed(without)
    without.shutdown(join_timeout=0.01)
    assert r.report().adapter.ledger.faults == without.report().adapter.ledger.faults
    assert r.report().completed is without.report().completed is False


def test_owner_mode_keeps_fifo_and_request_accounting_identical_to_no_owner_routing():
    owner = account_owner()
    with_owner = owner_router(owner)
    owned_summary(with_owner)
    owned_positions(with_owner)
    without = CallbackRouter(Client(), _Lock(), FakeClock(NOW))
    without.start()
    without.register_account_summary(SRID)
    without.register_positions(PRID, owner=None)
    summary_feed(without)
    positions_feed(without)
    for r in (with_owner, without):
        drain(r)
        r.shutdown(join_timeout=0.01)
    a, b = with_owner.report(), without.report()
    assert a.adapter.ledger.slots == b.adapter.ledger.slots and a.faults == b.faults == ()
    assert (a.completed, a.queued_records) == (b.completed, b.queued_records) == (True, 0)
    assert with_owner.register_positions(PRID, owner=owner) is None
    assert "POSITIONS_WINDOW_REOPENED" in with_owner.report().faults  # the single window holds in owner mode


# ----------------------------------------------------------------------------- ROUTER-STATUS: stream_status


def status_by_kind(r: CallbackRouter) -> dict:
    return {s.stream: s for s in r.stream_status()}


def direct_normalizers():
    contract = IbkrCallbackNormalizer(CRID, CallbackRequest.CONTRACT_DETAILS)
    quote = IbkrCallbackNormalizer(QRID, CallbackRequest.QUOTE_SNAPSHOT, con_id=CON_ID, clock=FakeClock(NOW))
    return {StreamKind.CONTRACT_DETAILS: contract, StreamKind.QUOTE_SNAPSHOT: quote,
            StreamKind.ACCOUNT_SUMMARY: AccountSummaryCallbacks(SRID),
            StreamKind.POSITIONS: PositionSnapshotCallbacks(PRID)}


def assert_status_parity(r: CallbackRouter, direct: dict) -> None:
    status = status_by_kind(r)
    for kind, normalizer in direct.items():
        expected = StreamStatus(kind, True, normalizer.completed,
                                tuple(i.code.value for i in normalizer.issues), normalizer.issue_overflow)
        assert status[kind] == expected, kind


def test_status_distinguishes_unregistered_streams_in_fixed_order():
    r = CallbackRouter(Client(), _Lock(), FakeClock(NOW))
    r.start()
    r.register_quote(QRID, CON_ID)
    status = r.stream_status()
    assert [s.stream for s in status] == list(StreamKind)
    assert [s.registered for s in status] == [False, True, False, False]
    assert status[0] == StreamStatus(StreamKind.CONTRACT_DETAILS, False, False, (), 0)
    assert status[1] == StreamStatus(StreamKind.QUOTE_SNAPSHOT, True, False, (), 0)


def test_status_matches_direct_normalizers_before_delivery_and_after_normal_completion():
    r, _ = router()
    everything(r)  # quote, account and position callbacks are queued, not yet delivered
    direct = direct_normalizers()
    direct[StreamKind.CONTRACT_DETAILS].contractDetails(CRID, Details())  # contract is direct
    direct[StreamKind.CONTRACT_DETAILS].contractDetailsEnd(CRID)
    assert_status_parity(r, direct)
    assert not status_by_kind(r)[StreamKind.QUOTE_SNAPSHOT].completed  # queued is not delivered
    drain(r)
    quote_feed(direct[StreamKind.QUOTE_SNAPSHOT])
    summary_feed(direct[StreamKind.ACCOUNT_SUMMARY])
    positions_feed(direct[StreamKind.POSITIONS])
    assert_status_parity(r, direct)
    assert all(s.registered and s.completed and s.issue_codes == () for s in r.stream_status())


def test_status_carries_the_delayed_size_non_live_tick_issue():
    r, _ = router()
    for target in (r, direct := direct_normalizers()[StreamKind.QUOTE_SNAPSHOT]):
        target.marketDataType(QRID, 1)
        target.tickPrice(QRID, 1, 40.0, None)
        target.tickSize(QRID, 69, Decimal(700))  # delayed bid size among live ticks
        target.tickSnapshotEnd(QRID)
    drain(r)
    quote = status_by_kind(r)[StreamKind.QUOTE_SNAPSHOT]
    assert quote.issue_codes == ("NON_LIVE_TICK_UNREPRESENTED",) and quote.completed is False
    assert quote.issue_codes == tuple(i.code.value for i in direct.issues)


def test_status_matches_malformed_repeated_end_and_refusal_evidence():
    r, _ = router()
    direct = direct_normalizers()
    r.contractDetails(CRID, Details())
    r.unsupported_callback("connectionClosed", CRID)  # as the demo's repeated-END case
    direct[StreamKind.CONTRACT_DETAILS].contractDetails(CRID, Details())
    direct[StreamKind.CONTRACT_DETAILS].unsupported_callback("connectionClosed")
    for target in (r, direct[StreamKind.CONTRACT_DETAILS]):
        target.contractDetailsEnd(CRID)
        target.contractDetailsEnd(CRID)  # repeated END
    for target in (r, direct[StreamKind.POSITIONS]):
        target.position(ACCOUNT, Contract(840001), Decimal("1E+5000"), 1.0)  # unretainable quantity
        target.positionEnd()
    r.error_1052(SRID, 0, 321, CANARY)  # an account error: a fixed refusal in the summary stream
    direct[StreamKind.ACCOUNT_SUMMARY].unsupported_callback("error")
    quote_feed(r)
    quote_feed(direct[StreamKind.QUOTE_SNAPSHOT])
    drain(r)
    assert_status_parity(r, direct)
    status = status_by_kind(r)
    assert status[StreamKind.POSITIONS].issue_codes and not status[StreamKind.POSITIONS].completed
    assert status[StreamKind.ACCOUNT_SUMMARY].issue_codes == ("UNSUPPORTED_CALLBACK",)
    assert status[StreamKind.CONTRACT_DETAILS].issue_codes and not status[StreamKind.CONTRACT_DETAILS].completed


def test_a_refused_owner_is_reported_as_not_registered():
    r = CallbackRouter(Client(), _Lock(), FakeClock(NOW))
    r.start()
    r.register_account_summary(SRID, owner=object())
    assert status_by_kind(r)[StreamKind.ACCOUNT_SUMMARY].registered is False


def test_a_snapshot_keeps_its_capture_values_while_a_later_call_sees_invalidation():
    r, _ = router()
    r.contractDetails(CRID, Details())
    r.contractDetailsEnd(CRID)
    before = r.stream_status()
    assert before[0].completed is True and before[0].issue_codes == ()
    r.unsupported_callback("inventedLateCallback", CRID)  # reaches the contract normalizer after END
    after = status_by_kind(r)[StreamKind.CONTRACT_DETAILS]
    assert after.completed is False and after.issue_codes == ("UNSUPPORTED_CALLBACK",)  # not cached
    assert before[0].completed is True and before[0].issue_codes == ()  # the snapshot is unchanged
    with pytest.raises(AttributeError):
        before[0].completed = False  # type: ignore[misc]
    assert type(before) is tuple and type(before[0].issue_codes) is tuple


def test_status_is_normalizer_evidence_not_a_lifecycle_verdict():
    r, _ = router()
    quote_feed(r)
    drain(r)
    r.tickPrice(QRID, 1, 41.0, None)  # after END: refused by the lifecycle, never delivered
    drain(r)
    assert status_by_kind(r)[StreamKind.QUOTE_SNAPSHOT].completed is True  # the normalizer's own flag
    assert "POST_END_EVENT" in r.report().adapter.ledger.faults  # the lifecycle records the failure


def test_reading_status_delivers_nothing_and_changes_no_evidence():
    r, _ = router()
    everything(r)
    report, envelopes = r.report(), (r.quote_envelopes(), r.account_summary_envelopes(), r.position_envelopes())
    for _ in range(3):
        r.stream_status()
    assert r.report() == report and report.queued_records > 0
    assert (r.quote_envelopes(), r.account_summary_envelopes(), r.position_envelopes()) == envelopes


def test_status_is_payload_free_in_owner_mode():
    owner = account_owner()
    r = owner_router(owner)
    owned_positions(r, FOREIGN_ACCOUNT)
    r.error_1052(SRID, 0, 321, CANARY)
    drain(r)
    text = repr(r.stream_status())
    for secret in (EXPECTED_ACCOUNT, FOREIGN_ACCOUNT, owner.binding.reference, CANARY, "RTRPOS", "840001"):
        assert secret not in text, secret
    assert set(StreamStatus.__slots__) == {"stream", "registered", "completed", "issue_codes", "issue_overflow"}
