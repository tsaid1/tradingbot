"""INT-2: one explicitly wired managedAccounts input, on invented identities only.

Every case composes the real ``ReadCallbackFacade`` -> ``CallbackRouter`` -> adapter consumer -> owner-mode
producers, with ``ManagedAccountsInput`` wired explicitly for ``managedAccounts`` only. Outputs are compared
with the accepted producers fed directly with an equivalent owner. Each failure is paired with its control,
before and after delivery and completion.
"""
from __future__ import annotations

import copy
import pickle
import threading
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from tradingbot.ibkr_read_facade import ReadCallbackFacade
from tradingbot.paper_readiness.account_position_callbacks import (
    AccountSummaryCallbacks,
    PositionSnapshotCallbacks,
)
from tradingbot.paper_readiness.callback_router import CallbackRouter
from tradingbot.paper_readiness.managed_accounts_input import (
    MAX_ACCOUNTS_TEXT,
    Arrival,
    Invalidation,
    ManagedAccountsInput,
    Registration,
)
from tradingbot.paper_readiness.observations import ResponseKind
from tradingbot.paper_readiness.private_account import AccountOwner
from tradingbot.paper_readiness.transport import FakeClock

NOW = datetime(2026, 3, 4, 16, 0, tzinfo=UTC)
SRID, PRID = 5003, 5004
EXPECTED, FOREIGN = "INVENTEDACCT01", "INVENTEDACCT02"  # invented, deliberately not SYNTH-shaped
KEY = b"m" * 32


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
        self.conId, self.symbol, self.localSymbol, self.secType = con_id, "MAIPOS", "MAIPOS", "STK"
        self.currency, self.exchange, self.primaryExchange = "USD", "SMART", "INV-LISTING"


def composed(start: bool = True):
    router = CallbackRouter(Client(), _Lock(), FakeClock(NOW))
    if start:
        router.start()
    return router, ReadCallbackFacade(router), ManagedAccountsInput(router, EXPECTED, KEY)


def summary_rows(target, account=EXPECTED) -> None:
    target.accountSummary(SRID, account, "AccountType", "INVENTED", "")
    for tag, value in (("NetLiquidation", "50000.00"), ("TotalCashValue", "40000.00"), ("BuyingPower", "1.00")):
        target.accountSummary(SRID, account, tag, value, "USD")
    target.accountSummaryEnd(SRID)


def position_rows(target, account=EXPECTED) -> None:
    target.position(account, Contract(860001), Decimal("-3.5"), 12.5)
    target.position(account, Contract(860002), Decimal(7), 3.0)
    target.positionEnd()


def drain(router: CallbackRouter) -> None:
    while router.consume_one():
        pass


def direct():
    owner = AccountOwner(EXPECTED, [EXPECTED], KEY)
    return AccountSummaryCallbacks(SRID, owner=owner), PositionSnapshotCallbacks(PRID, owner=owner)


def kinds(envelopes) -> list:
    return [e.kind for e in envelopes]


def complete_run(router, facade, accounts) -> None:
    accounts.managedAccounts(EXPECTED)
    accounts.register_reads(SRID, PRID)
    summary_rows(facade)
    position_rows(facade)
    drain(router)
    router.shutdown(join_timeout=0.01)


# ----------------------------------------------------------------------------- the valid composition


def test_control_one_exact_snapshot_registers_owner_mode_reads_and_completes_with_the_router():
    router, facade, accounts = composed()
    complete_run(router, facade, accounts)
    summary, positions = direct()
    summary_rows(summary)
    position_rows(positions)
    assert router.account_summary_envelopes() == summary.envelopes()
    assert router.position_envelopes() == positions.envelopes()
    report = accounts.report()
    assert (report.arrival, report.registration, report.invalidation) == (Arrival.EXACT, Registration.REGISTERED,
                                                                          None)
    assert report.router_completed is True and report.completed is True
    accounts.close()  # a normal close, after shutdown: not an invalidation
    after = accounts.report()
    assert after.closed is True and after.invalidation is None and after.completed is True
    text = repr(router.position_envelopes()) + repr(router.report()) + repr(accounts) + repr(after)
    assert EXPECTED not in text


def test_completion_needs_the_router_too():
    router, facade, accounts = composed()
    accounts.managedAccounts(EXPECTED)
    accounts.register_reads(SRID, PRID)
    summary_rows(facade)
    position_rows(facade)
    drain(router)  # no shutdown: the router has not completed
    report = accounts.report()
    assert report.registration is Registration.REGISTERED and report.invalidation is None
    assert report.router_completed is False and report.completed is False


def test_foreign_malformed_error_and_end_evidence_is_preserved_exactly():
    router, facade, accounts = composed()
    accounts.managedAccounts(EXPECTED)
    accounts.register_reads(SRID, PRID)
    summary_rows(facade)
    facade.error(SRID, 0, 321, "invented")  # account error after END
    position_rows(facade, FOREIGN)
    facade.position(123, Contract(860003), Decimal(1), 1.0)  # malformed identity
    drain(router)
    summary, positions = direct()
    summary_rows(summary)
    summary.unsupported_callback("error")
    position_rows(positions, FOREIGN)
    positions.position(123, Contract(860003), Decimal(1), 1.0)
    assert router.account_summary_envelopes() == summary.envelopes()
    assert router.position_envelopes() == positions.envelopes()
    text = repr(router.position_envelopes())
    assert FOREIGN not in text and "acctref-v1:" in text  # a distinct reference, so mismatch survives
    assert ResponseKind.ACCOUNT_SUMMARY_END not in kinds(router.account_summary_envelopes())


# ----------------------------------------------------------------------------- the first arrival


@pytest.mark.parametrize(("args", "kwargs"), [
    ((), {}), ((EXPECTED, EXPECTED), {}), ((), {"accountsList": EXPECTED}), ((EXPECTED,), {"extra": 1}),
    ((EXPECTED.encode(),), {}), (([EXPECTED],), {}), (("",), {}), (("A" * (MAX_ACCOUNTS_TEXT + 1),), {}),
    ((f"{EXPECTED},,{FOREIGN}",), {}), ((f"{EXPECTED},",), {}), ((",",), {}),
], ids=["no-args", "two-args", "keyword", "extra-keyword", "bytes", "list", "empty", "oversized",
        "empty-segment", "trailing-separator", "only-separator"])
def test_a_malformed_first_arrival_is_sticky_and_never_registers(args, kwargs):
    router, _facade, accounts = composed()
    accounts.managedAccounts(*args, **kwargs)
    accounts.managedAccounts(EXPECTED)  # a valid second arrival cannot recover
    accounts.register_reads(SRID, PRID)
    report = accounts.report()
    assert (report.arrival, report.invalidation, report.registration) == (
        Arrival.MALFORMED, Invalidation.DUPLICATE, Registration.NOT_READY)
    assert router.report().adapter.ledger.slots == () and router.report().faults == ()  # router untouched


@pytest.mark.parametrize(("text", "arrival"), [
    (FOREIGN, Arrival.MISMATCH), (f"{EXPECTED},{FOREIGN}", Arrival.AMBIGUOUS), (f"{EXPECTED},{EXPECTED}", Arrival.AMBIGUOUS),
    (" " + EXPECTED, Arrival.MISMATCH), (",".join([EXPECTED] * 17), Arrival.REFUSED),
    ("A" * 65, Arrival.REFUSED), ("INVENTED\x01ACCT", Arrival.REFUSED),
], ids=["mismatch", "ambiguous", "duplicate-entry", "untrimmed", "too-many", "entry-too-long", "control-char"])
def test_a_non_exact_snapshot_is_a_fixed_status_and_registers_nothing(text, arrival):
    router, _facade, accounts = composed()
    accounts.managedAccounts(text)
    accounts.register_reads(SRID, PRID)
    report = accounts.report()
    assert (report.arrival, report.registration, report.invalidation) == (arrival, Registration.NOT_READY, None)
    assert router.report().adapter.ledger.slots == ()
    assert EXPECTED not in repr(report) + repr(accounts)


# ----------------------------------------------------------------------------- invalidation


def test_a_duplicate_before_registration_latches_and_prohibits_registration():
    router, _facade, accounts = composed()
    accounts.managedAccounts(EXPECTED)
    accounts.managedAccounts(EXPECTED)
    accounts.register_reads(SRID, PRID)
    report = accounts.report()
    assert (report.invalidation, report.registration) == (Invalidation.DUPLICATE, Registration.NOT_READY)
    assert router.report().faults == () and router.report().adapter.ledger.slots == ()  # not associated


def test_a_duplicate_after_enqueue_reaches_the_router_and_fails_undelivered_rows():
    router, facade, accounts = composed()
    accounts.managedAccounts(EXPECTED)
    accounts.register_reads(SRID, PRID)
    position_rows(facade)  # enqueued, not delivered
    accounts.managedAccounts(EXPECTED)
    drain(router)
    router.shutdown(join_timeout=0.01)
    assert "UNSUPPORTED_CALLBACK" in router.report().faults
    assert ResponseKind.POSITION_END not in kinds(router.position_envelopes())
    assert not [e for e in router.position_envelopes() if e.kind == ResponseKind.POSITION]
    report = accounts.report()
    assert report.invalidation is Invalidation.DUPLICATE and report.completed is False


def test_an_invalidating_arrival_after_completion_revokes_completion_but_keeps_history():
    router, facade, accounts = composed()
    complete_run(router, facade, accounts)
    produced = router.position_envelopes()
    assert accounts.report().completed is True
    accounts.managedAccounts(EXPECTED)  # duplicate after END and router completion
    assert router.position_envelopes() == produced  # history is not rewritten
    assert accounts.report().invalidation is Invalidation.DUPLICATE
    assert router.report().completed is False and accounts.report().completed is False


def test_an_arrival_after_a_normal_close_is_late_and_distinct_from_the_close():
    router, facade, accounts = composed()
    complete_run(router, facade, accounts)
    accounts.close()
    assert accounts.report().invalidation is None and accounts.report().completed is True
    accounts.managedAccounts(EXPECTED)
    report = accounts.report()
    assert report.invalidation is Invalidation.LATE and report.completed is False
    assert router.report().completed is False


def test_the_first_invalidation_is_never_replaced():
    router, _facade, accounts = composed()
    accounts.managedAccounts(EXPECTED)
    accounts.register_reads(SRID, PRID)
    accounts.managedAccounts(EXPECTED)
    accounts.register_reads(SRID, PRID)
    accounts.close()
    accounts.managedAccounts(EXPECTED)
    report = accounts.report()
    assert report.invalidation is Invalidation.DUPLICATE and report.invalidating_events == 3
    assert router.report().faults.count("UNSUPPORTED_CALLBACK") == 3


# ----------------------------------------------------------------------------- registration


def test_a_second_registration_attempt_invalidates_without_calling_registration_again():
    router, facade, accounts = composed()
    accounts.managedAccounts(EXPECTED)
    accounts.register_reads(SRID, PRID)
    slots = router.report().adapter.ledger.slots
    accounts.register_reads(SRID + 10, PRID + 10)
    assert router.report().adapter.ledger.slots == slots  # no new slot
    assert "REGISTRATION_REFUSED" not in router.report().faults and "UNSUPPORTED_CALLBACK" in router.report().faults
    report = accounts.report()
    assert report.registration is Registration.REGISTERED and report.invalidation is Invalidation.SECOND_REGISTRATION
    summary_rows(facade)
    position_rows(facade)
    drain(router)
    router.shutdown(join_timeout=0.01)
    assert accounts.report().completed is False


def test_a_partial_registration_is_kept_as_evidence_and_invalidates():
    router, _facade, accounts = composed()
    router.register_positions(PRID)  # the single window is already used by a no-owner registration
    accounts.managedAccounts(EXPECTED)
    accounts.register_reads(SRID, PRID + 1)
    report = accounts.report()
    assert (report.registration, report.invalidation) == (Registration.PARTIAL, Invalidation.REGISTRATION_INCOMPLETE)
    slots = {s.slot for s in router.report().adapter.ledger.slots}
    assert slots == {"ACCOUNT_SUMMARY", "POSITIONS"}  # the opened slot stays: no rollback
    assert {"POSITIONS_WINDOW_REOPENED", "UNSUPPORTED_CALLBACK"} <= set(router.report().faults)


def test_a_failed_registration_opens_nothing_and_invalidates():
    router, _facade, accounts = composed(start=False)  # the adapter is not open
    accounts.managedAccounts(EXPECTED)
    accounts.register_reads(SRID, PRID)
    report = accounts.report()
    assert (report.registration, report.invalidation) == (Registration.FAILED, Invalidation.REGISTRATION_INCOMPLETE)
    assert router.report().adapter.ledger.slots == ()


# ----------------------------------------------------------------------------- default, construction, surface


def test_the_facade_default_managed_accounts_refusal_is_unchanged():
    router, facade, _accounts = composed()
    facade.managedAccounts(EXPECTED)  # the facade still refuses; only explicit wiring reaches the input
    assert "UNSUPPORTED_CALLBACK" in router.report().faults


def test_no_owner_registration_is_unchanged_beside_the_input():
    router, facade, _accounts = composed()
    router.register_account_summary(SRID)
    facade.accountSummary(SRID, EXPECTED, "NetLiquidation", "1.00", "USD")
    drain(router)
    plain = AccountSummaryCallbacks(SRID)
    plain.accountSummary(SRID, EXPECTED, "NetLiquidation", "1.00", "USD")
    assert router.account_summary_envelopes() == plain.envelopes()  # non-SYNTH refused as before


@pytest.mark.parametrize(("router", "expected", "key"), [
    (object(), EXPECTED, KEY), (type("Sub", (CallbackRouter,), {}), EXPECTED, KEY),
    (None, EXPECTED, KEY), ("router", b"x", KEY), ("router", "", KEY), ("router", "A" * 65, KEY),
    ("router", "\ud800", KEY), ("router", EXPECTED, b"short"), ("router", EXPECTED, "k" * 32),
    ("router", EXPECTED, bytearray(b"m" * 32)),
], ids=["object", "subclass", "none", "expected-bytes", "expected-empty", "expected-long", "expected-surrogate",
        "key-short", "key-str", "key-bytearray"])
def test_construction_refuses_wrong_types_and_bounds_without_values(router, expected, key):
    real = CallbackRouter(Client(), _Lock(), FakeClock(NOW))
    with pytest.raises((TypeError, ValueError)) as caught:
        ManagedAccountsInput(real if router == "router" else router, expected, key)
    assert EXPECTED not in str(caught.value) and "m" * 32 not in str(caught.value)


def test_the_input_cannot_be_copied_or_serialized_and_refuses_reentry():
    _router, _facade, accounts = composed()
    for attempt in (copy.copy, copy.deepcopy, pickle.dumps):
        with pytest.raises(TypeError):
            attempt(accounts)
    with pytest.raises(AttributeError):
        accounts.extra = 1  # type: ignore[attr-defined]
    accounts._busy = True  # simulate a call arriving while another call of this component runs
    accounts.managedAccounts(EXPECTED)
    accounts._busy = False
    assert accounts.report().invalidation is Invalidation.REENTERED
    assert accounts.report().arrival is Arrival.AWAITING  # the reentrant arrival was not taken
