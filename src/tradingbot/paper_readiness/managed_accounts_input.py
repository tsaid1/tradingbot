"""One explicitly wired ``managedAccounts`` input for one router. NON_AUTHORITATIVE.

``ManagedAccountsInput`` is constructed for one exact ``CallbackRouter`` with the operator's private
expected account identifier and a per-run key. It receives only the converted ``managedAccounts``
callback, by explicit wiring: the read facade's default refusal of that callback is unchanged, and every
other callback still goes through the facade. Its one job is to build the existing ``AccountOwner`` from
one bounded snapshot and to register the router's account-summary and positions reads in owner mode
(INT-1), once.

* **Bounded, exact input.** The expected identifier must be an exact ``str`` of 1 to 64 UTF-8 bytes and
  the key exact ``bytes`` of 16 to 64 bytes, checked before anything is retained. The callback takes one
  positional exact ``str`` of at most 1040 characters, split on ``,`` exactly, with no trimming, case
  folding or deduplication. An empty string or an empty segment is ``MALFORMED``; so ``ABSENT`` is not
  reachable through this parser. Entry count and per-entry validity are judged by ``AccountOwner``.
* **Sticky outcomes.** The first arrival's outcome never changes: a malformed first arrival cannot later
  recover. The first invalidating event (a second arrival, an arrival after close, a second or failed
  registration, reentry) is latched and never replaced.
* **One registration, judged by public evidence.** ``register_reads`` calls the router's two owner-mode
  registrations at most once, and only after an exact match. Both return ``None``, so success is read
  from the router's public report: both slots newly open and no new fault. Anything less is kept as
  evidence and invalidates; there is no rollback, retry or no-owner fallback.
* **Invalidation reaches the router.** Once registration has been attempted, every invalidating event
  also calls the router's public ``unsupported_callback`` with the fixed label ``managedAccounts`` and
  closes this component's own owner, even after END or completion. Already produced records stay as
  historical evidence. ``completed`` requires this component's current success and the router's own
  completion, never either alone.
* **Lifetime.** The caller keeps the component open through consumer delivery and shutdown, then calls
  ``close()``, which closes only the owner it created. A normal close is distinct from an invalidating
  arrival after it.

One owning execution context only: arrival, registration and close must be serialized, and a reentrant
call is refused. Nothing here is thread-safe, authenticates a source, proves a connection, PAPER or
consent, or reads anything but its arguments. An ``EXACT`` status is a comparison. Values never appear in
a status, repr or error. See docs/MANAGED_ACCOUNTS_INPUT.md.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from tradingbot.paper_readiness.account_ref import (
    MAX_IDENTIFIER_BYTES,
    MAX_KEY_BYTES,
    MAX_MANAGED_ACCOUNTS,
    MIN_KEY_BYTES,
    MatchStatus,
)
from tradingbot.paper_readiness.callback_router import CallbackRouter
from tradingbot.paper_readiness.private_account import AccountOwner
from tradingbot.paper_readiness.read_retirement import ReadSlot

__all__ = [
    "MAX_ACCOUNTS_TEXT",
    "Arrival",
    "Invalidation",
    "ManagedAccountsInput",
    "ManagedAccountsReport",
    "Registration",
]

MAX_ACCOUNTS_TEXT = MAX_MANAGED_ACCOUNTS * MAX_IDENTIFIER_BYTES + (MAX_MANAGED_ACCOUNTS - 1)  # 1040
REFUSAL_LABEL = "managedAccounts"
_READ_SLOTS = frozenset({ReadSlot.ACCOUNT_SUMMARY.value, ReadSlot.POSITIONS.value})


class Arrival(str, Enum):
    AWAITING = "AWAITING"
    MALFORMED = "MALFORMED"
    EXACT = "EXACT"
    MISMATCH = "MISMATCH"
    AMBIGUOUS = "AMBIGUOUS"
    REFUSED = "REFUSED"  # AccountOwner refused the snapshot: too many entries or an invalid entry


class Invalidation(str, Enum):
    DUPLICATE = "DUPLICATE"                        # a second managedAccounts arrival
    LATE = "LATE"                                  # an arrival after a normal close
    SECOND_REGISTRATION = "SECOND_REGISTRATION"    # register_reads called again
    REGISTRATION_INCOMPLETE = "REGISTRATION_INCOMPLETE"  # the router's report did not show both reads
    REENTERED = "REENTERED"                        # a call while another call of this component ran


class Registration(str, Enum):
    NOT_ATTEMPTED = "NOT_ATTEMPTED"
    NOT_READY = "NOT_READY"    # attempted without a current exact match: the router was not called
    REGISTERED = "REGISTERED"  # both reads newly open and no new router fault
    PARTIAL = "PARTIAL"        # one read newly open: kept as evidence, never rolled back
    FAILED = "FAILED"          # no read newly open


_ARRIVAL = {MatchStatus.EXACT: Arrival.EXACT, MatchStatus.MISMATCH: Arrival.MISMATCH,
            MatchStatus.AMBIGUOUS: Arrival.AMBIGUOUS, MatchStatus.REFUSED: Arrival.REFUSED}


@dataclass(frozen=True, slots=True)
class ManagedAccountsReport:
    """Fixed codes only. ``completed`` is local and never PAPER-READ-1 or trading permission."""

    arrival: Arrival
    invalidation: Invalidation | None
    invalidating_events: int
    registration: Registration
    closed: bool
    router_completed: bool
    completed: bool
    authority: str = "NON_AUTHORITATIVE"


class ManagedAccountsInput:
    """Receives one ``managedAccounts`` snapshot for one router and registers its owner-mode reads once."""

    __slots__ = ("_arrival", "_associated", "_busy", "_closed", "_events", "_expected", "_invalidation",
                 "_key", "_owner", "_registration", "_router")

    def __init__(self, router: CallbackRouter, expected: str, key: bytes) -> None:
        if type(router) is not CallbackRouter:
            raise TypeError("router must be a CallbackRouter")
        if type(expected) is not str or not expected or len(expected) > MAX_IDENTIFIER_BYTES:
            raise ValueError("expected must be a str of 1 to 64 characters")
        try:
            encoded = expected.encode("utf-8", "strict")
        except UnicodeEncodeError:
            raise ValueError("expected must be valid UTF-8") from None
        if len(encoded) > MAX_IDENTIFIER_BYTES:
            raise ValueError("expected must be at most 64 UTF-8 bytes")
        if type(key) is not bytes or not MIN_KEY_BYTES <= len(key) <= MAX_KEY_BYTES:
            raise ValueError("key must be bytes of 16 to 64 bytes")
        self._router = router
        self._expected = expected
        self._key = key
        self._owner: AccountOwner | None = None
        self._arrival = Arrival.AWAITING
        self._invalidation: Invalidation | None = None
        self._events = 0
        self._registration = Registration.NOT_ATTEMPTED
        self._associated = False
        self._closed = False
        self._busy = False

    # ------------------------------------------------------------------ the one callback

    def managedAccounts(self, *args: object, **kwargs: object) -> None:
        """``managedAccounts(accountsList)``: the first arrival is the snapshot; any later one invalidates."""
        if self._busy:
            self._invalidate(Invalidation.REENTERED)
            return
        self._busy = True
        try:
            if self._closed:
                self._invalidate(Invalidation.LATE)
            elif self._arrival is not Arrival.AWAITING:
                self._invalidate(Invalidation.DUPLICATE)
            else:
                self._first_arrival(args, kwargs)
        finally:
            self._busy = False

    def _first_arrival(self, args: tuple, kwargs: dict) -> None:
        text = args[0] if len(args) == 1 else None
        if kwargs or type(text) is not str or not text or len(text) > MAX_ACCOUNTS_TEXT:
            self._arrival = Arrival.MALFORMED
            return
        parts = tuple(text.split(","))
        if any(not part for part in parts):
            self._arrival = Arrival.MALFORMED
            return
        owner = AccountOwner(self._expected, parts, self._key)
        self._owner = owner
        self._arrival = _ARRIVAL.get(owner.status, Arrival.REFUSED)

    # ------------------------------------------------------------------ registration

    def register_reads(self, summary_request_id: int, positions_snapshot_id: int) -> None:
        """Register the router's account-summary and positions reads in owner mode, at most once."""
        if self._busy:
            self._invalidate(Invalidation.REENTERED)
            return
        self._busy = True
        try:
            if self._registration is not Registration.NOT_ATTEMPTED:
                self._invalidate(Invalidation.SECOND_REGISTRATION)
                return
            owner = self._owner
            if (self._closed or self._invalidation is not None or self._arrival is not Arrival.EXACT
                    or owner is None or not owner.owns(owner.binding)):
                self._registration = Registration.NOT_READY  # the router is not called
                return
            self._associated = True
            before = self._router.report()
            self._router.register_account_summary(summary_request_id, owner=owner)
            self._router.register_positions(positions_snapshot_id, owner=owner)
            after = self._router.report()
            opened = ({s.slot for s in after.adapter.ledger.slots} - {s.slot for s in before.adapter.ledger.slots})
            new_faults = (len(after.faults) != len(before.faults)
                          or after.faults_dropped != before.faults_dropped)
            if opened == _READ_SLOTS and not new_faults:
                self._registration = Registration.REGISTERED
                return
            self._registration = Registration.PARTIAL if opened & _READ_SLOTS else Registration.FAILED
            self._invalidate(Invalidation.REGISTRATION_INCOMPLETE)
        finally:
            self._busy = False

    # ------------------------------------------------------------------ lifetime and evidence

    def close(self) -> None:
        """A normal close, after shutdown: closes only the owner this component created. Idempotent."""
        if self._busy:
            self._invalidate(Invalidation.REENTERED)
            return
        self._closed = True
        if self._owner is not None:
            self._owner.close()

    def _invalidate(self, reason: Invalidation) -> None:
        if self._invalidation is None:
            self._invalidation = reason
        self._events += 1
        if self._owner is not None:
            self._owner.close()  # undelivered rows can no longer be projected
        if self._associated:
            self._router.unsupported_callback(REFUSAL_LABEL)  # a persistent router fault, after END too

    def report(self) -> ManagedAccountsReport:
        router_completed = self._router.report().completed
        ours = (self._arrival is Arrival.EXACT and self._invalidation is None
                and self._registration is Registration.REGISTERED)
        return ManagedAccountsReport(
            arrival=self._arrival, invalidation=self._invalidation, invalidating_events=self._events,
            registration=self._registration, closed=self._closed, router_completed=router_completed,
            completed=ours and router_completed)

    def __repr__(self) -> str:
        return (f"ManagedAccountsInput(arrival={self._arrival.value}, registration={self._registration.value}, "
                f"invalidated={self._invalidation is not None})")

    def __reduce_ex__(self, protocol: object) -> object:
        raise TypeError("a ManagedAccountsInput holds private run state and cannot be serialized")

    def __copy__(self) -> object:
        raise TypeError("a ManagedAccountsInput cannot be copied")

    def __deepcopy__(self, memo: object) -> object:
        raise TypeError("a ManagedAccountsInput cannot be copied")
