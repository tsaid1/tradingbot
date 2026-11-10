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
from tradingbot.paper_readiness.callback_router import CallbackRouter
from tradingbot.paper_readiness.ibkr_callbacks import CallbackRequest, IbkrCallbackNormalizer
from tradingbot.paper_readiness.observations import ResponseKind
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


@pytest.mark.parametrize("args", [(-1, "bad-time", 2104, "ignored"), (-1, 0, 2104), (-1, 0, True, "x"),
                                  (-1.0, 0, 2104, "ignored"), (Decimal(-1), 0, 2104, "ignored")],
                         ids=["time-text", "three-arguments", "code-bool", "id-float", "id-decimal"])
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
