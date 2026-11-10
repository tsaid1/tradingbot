"""Offline accounting of read retirement for one local connection. NON_AUTHORITATIVE.

``RetirementLedger`` is a pure, bounded event model. Its methods take only fixed enum values: there is
no SDK, client, socket, thread or callable here, and nothing is sent anywhere. A caller reports what a
connection did, and the ledger keeps the account:

* a retirement attempt is reserved before its outcome is reported, settles once, and is never
  retried; failed and unknown outcomes are kept
* dispatch admission, queued and in-flight callbacks, the consumer, the reader and the socket are
  separate, so a stopped reader alone never means the connection stopped
* late callbacks, post-END events and a second positions read on one connection are faults
* a snapshot END marks only that request's end; a snapshot without one stays incomplete, and a
  retirement attempt for it is modelled intent only, not a claim that it is supported or authorized
* sealing bounds settlement: anything still open becomes UNKNOWN, never success

The best the ledger can report is ``local_quiescence_established``. Broker-side retirement is
``NOT_ESTABLISHED_BY_INSPECTED_SOURCES``, and nothing here is PAPER-READ-1 acceptance or trading
permission. See docs/READ_ONLY_RETIREMENT.md.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Literal

__all__ = [
    "BROKER_SIDE_RETIREMENT",
    "MAX_EVENTS",
    "MAX_FAULTS",
    "CallbackKind",
    "Component",
    "Fault",
    "Outcome",
    "ReadSlot",
    "RetirementLedger",
    "RetirementReport",
    "SlotReport",
]

MAX_EVENTS = 4096
MAX_FAULTS = 64
BROKER_SIDE_RETIREMENT = "NOT_ESTABLISHED_BY_INSPECTED_SOURCES"


class ReadSlot(str, Enum):
    ACCOUNT_SUMMARY = "ACCOUNT_SUMMARY"  # keeps updating after its END
    POSITIONS = "POSITIONS"              # requestless, keeps updating after its END
    QUOTE_SNAPSHOT = "QUOTE_SNAPSHOT"    # its END marks only that snapshot's end


# Reads that keep delivering after their initial END, so a returned retirement attempt is required.
_CONTINUING = frozenset({ReadSlot.ACCOUNT_SUMMARY, ReadSlot.POSITIONS})


class CallbackKind(str, Enum):
    DATA = "DATA"
    END = "END"
    ERROR = "ERROR"


class Outcome(str, Enum):
    """A settled attempt or a component's termination. RETURNED means only that a local call returned."""
    RETURNED = "RETURNED"
    STOPPED = "STOPPED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


class Component(str, Enum):
    CONSUMER = "CONSUMER"  # the loop that dispatches queued callbacks
    READER = "READER"      # the thread that reads the socket into the queue
    SOCKET = "SOCKET"


class Fault(str, Enum):
    READ_REOPENED = "READ_REOPENED"
    POSITIONS_REUSED = "POSITIONS_REUSED"
    READ_NOT_OPENED = "READ_NOT_OPENED"
    LATE_CALLBACK = "LATE_CALLBACK"
    CALLBACK_AFTER_ADMISSION_CLOSED = "CALLBACK_AFTER_ADMISSION_CLOSED"
    POST_END_EVENT = "POST_END_EVENT"
    DISPATCH_OUT_OF_ORDER = "DISPATCH_OUT_OF_ORDER"
    ATTEMPT_ALREADY_RESERVED = "ATTEMPT_ALREADY_RESERVED"
    ATTEMPT_NOT_RESERVED = "ATTEMPT_NOT_RESERVED"
    ATTEMPT_AFTER_DISCONNECT = "ATTEMPT_AFTER_DISCONNECT"
    DUPLICATE_SETTLEMENT = "DUPLICATE_SETTLEMENT"
    INVALID_OUTCOME = "INVALID_OUTCOME"
    ADMISSION_ALREADY_CLOSED = "ADMISSION_ALREADY_CLOSED"
    EVENT_AFTER_SEAL = "EVENT_AFTER_SEAL"
    EVENT_LIMIT_EXCEEDED = "EVENT_LIMIT_EXCEEDED"


_ATTEMPT_OUTCOMES = frozenset({Outcome.RETURNED, Outcome.FAILED, Outcome.UNKNOWN})
_COMPONENT_OUTCOMES = frozenset({Outcome.STOPPED, Outcome.FAILED, Outcome.UNKNOWN})
_RESERVED = "RESERVED"
_NOT_RESERVED = "NOT_RESERVED"


class _Slot:
    def __init__(self) -> None:
        self.end_seen = False
        self.errored = False
        self.data_callbacks = 0
        self.attempt: str = _NOT_RESERVED


@dataclass(frozen=True)
class SlotReport:
    slot: str
    end_marker_seen: bool
    errored: bool
    read_complete: bool
    data_callbacks: int
    retirement_attempt: str


@dataclass(frozen=True)
class RetirementReport:
    """Fixed fields only: no account, position, price or other value inputs."""
    authority: Literal["NON_AUTHORITATIVE"]
    trading_permission: Literal[False]
    paper_read_1: Literal["NOT_ASSESSED"]
    broker_side_retirement: str
    snapshot_retirement: Literal["MODELLED_INTENT_ONLY"]
    sealed: bool
    slots: tuple[SlotReport, ...]
    admission_closed: bool
    queued_callbacks: int
    in_flight_callbacks: int
    consumer: str
    reader: str
    socket: str
    disconnect_attempt: str
    faults: tuple[str, ...]
    faults_dropped: int
    local_quiescence_established: bool


class RetirementLedger:
    """Accounting for one local connection and its fixed read slots. Every method takes values only."""

    def __init__(self) -> None:
        self._slots: dict[ReadSlot, _Slot] = {}
        self._queued: list[tuple[ReadSlot, CallbackKind]] = []
        self._in_flight: tuple[ReadSlot, CallbackKind] | None = None
        self._admission_closed = False
        self._components: dict[Component, str] = {}
        self._disconnect: str = _NOT_RESERVED
        self._faults: list[Fault] = []
        self._faults_dropped = 0
        self._events = 0
        self._sealed = False

    # ------------------------------------------------------------------ bookkeeping

    def _fault(self, fault: Fault) -> None:
        if len(self._faults) < MAX_FAULTS:
            self._faults.append(fault)
        else:
            self._faults_dropped += 1

    def _admit(self) -> bool:
        """Count one event. False once sealed or over the bound; the event then changes nothing."""
        if self._sealed:
            self._fault(Fault.EVENT_AFTER_SEAL)
            return False
        self._events += 1
        if self._events > MAX_EVENTS:
            if self._events == MAX_EVENTS + 1:
                self._fault(Fault.EVENT_LIMIT_EXCEEDED)
            return False
        return True

    # ------------------------------------------------------------------ reads and callbacks

    def open_read(self, slot: ReadSlot) -> None:
        if not self._admit():
            return
        slot = ReadSlot(slot)
        if slot in self._slots:
            # A requestless positions read cannot be told apart from an earlier one on this connection.
            self._fault(Fault.POSITIONS_REUSED if slot is ReadSlot.POSITIONS else Fault.READ_REOPENED)
            return
        self._slots[slot] = _Slot()

    def callback_received(self, slot: ReadSlot, kind: CallbackKind) -> None:
        """The reader queued one callback. After admission closes it is a fault and is not queued."""
        if not self._admit():
            return
        if self._admission_closed:
            self._fault(Fault.CALLBACK_AFTER_ADMISSION_CLOSED)
            return
        self._queued.append((ReadSlot(slot), CallbackKind(kind)))

    def callback_started(self) -> None:
        """The consumer took the oldest queued callback. One consumer, so one callback in flight."""
        if not self._admit():
            return
        if self._in_flight is not None or not self._queued:
            self._fault(Fault.DISPATCH_OUT_OF_ORDER)
            return
        self._in_flight = self._queued.pop(0)

    def callback_finished(self) -> None:
        """The in-flight callback was dispatched; its effect is applied now."""
        if not self._admit():
            return
        if self._in_flight is None:
            self._fault(Fault.DISPATCH_OUT_OF_ORDER)
            return
        slot, kind = self._in_flight
        self._in_flight = None
        state = self._slots.get(slot)
        if state is None:
            self._fault(Fault.READ_NOT_OPENED)
        elif state.end_seen:
            self._fault(Fault.POST_END_EVENT)
        elif state.attempt != _NOT_RESERVED:
            self._fault(Fault.LATE_CALLBACK)
        elif kind is CallbackKind.END:
            state.end_seen = True
        elif kind is CallbackKind.ERROR:
            state.errored = True
        else:
            state.data_callbacks += 1

    # ------------------------------------------------------------------ retirement attempts

    def reserve_attempt(self, slot: ReadSlot) -> None:
        """Reserve the slot's one retirement attempt, before anything reports its outcome.

        Allowed from an open read with or without its END, including after an error."""
        if not self._admit():
            return
        state = self._slots.get(ReadSlot(slot))
        if state is None:
            self._fault(Fault.READ_NOT_OPENED)
        elif self._disconnect != _NOT_RESERVED:
            self._fault(Fault.ATTEMPT_AFTER_DISCONNECT)
        elif state.attempt != _NOT_RESERVED:
            self._fault(Fault.ATTEMPT_ALREADY_RESERVED)
        else:
            state.attempt = _RESERVED

    def settle_attempt(self, slot: ReadSlot, outcome: Outcome) -> None:
        if not self._admit():
            return
        state = self._slots.get(ReadSlot(slot))
        if state is None:
            self._fault(Fault.READ_NOT_OPENED)
            return
        state.attempt = self._settle(state.attempt, Outcome(outcome), _ATTEMPT_OUTCOMES)

    def reserve_disconnect(self) -> None:
        """Reserved independently of the read attempts, so a failed attempt never blocks it."""
        if not self._admit():
            return
        if self._disconnect != _NOT_RESERVED:
            self._fault(Fault.ATTEMPT_ALREADY_RESERVED)
            return
        self._disconnect = _RESERVED

    def settle_disconnect(self, outcome: Outcome) -> None:
        if not self._admit():
            return
        self._disconnect = self._settle(self._disconnect, Outcome(outcome), _ATTEMPT_OUTCOMES)

    def _settle(self, current: str, outcome: Outcome, allowed: frozenset[Outcome]) -> str:
        """Once only: a second settlement is a fault and never replaces the first."""
        if current == _NOT_RESERVED:
            self._fault(Fault.ATTEMPT_NOT_RESERVED)
            return current
        if current != _RESERVED:
            self._fault(Fault.DUPLICATE_SETTLEMENT)
            return current
        if outcome not in allowed:
            self._fault(Fault.INVALID_OUTCOME)
            return Outcome.UNKNOWN.value
        return outcome.value

    # ------------------------------------------------------------------ termination

    def close_admission(self) -> None:
        """Terminal: no further callback can be admitted. Already queued callbacks may still drain."""
        if not self._admit():
            return
        if self._admission_closed:
            self._fault(Fault.ADMISSION_ALREADY_CLOSED)
            return
        self._admission_closed = True

    def component_stopped(self, component: Component, outcome: Outcome) -> None:
        if not self._admit():
            return
        component = Component(component)
        if component in self._components:
            self._fault(Fault.DUPLICATE_SETTLEMENT)
            return
        outcome = Outcome(outcome)
        if outcome not in _COMPONENT_OUTCOMES:
            self._fault(Fault.INVALID_OUTCOME)
            outcome = Outcome.UNKNOWN
        self._components[component] = outcome.value

    def seal(self) -> None:
        """The settlement bound. Unsettled attempts and unreported components become UNKNOWN."""
        if self._sealed:
            self._fault(Fault.EVENT_AFTER_SEAL)
            return
        self._sealed = True
        for state in self._slots.values():
            if state.attempt == _RESERVED:
                state.attempt = Outcome.UNKNOWN.value
        if self._disconnect == _RESERVED:
            self._disconnect = Outcome.UNKNOWN.value
        for component in Component:
            self._components.setdefault(component, Outcome.UNKNOWN.value)

    # ------------------------------------------------------------------ report

    def report(self) -> RetirementReport:
        slots = []
        reads_retired = True
        for slot in ReadSlot:
            state = self._slots.get(slot)
            if state is None:
                continue
            complete = state.end_seen and not state.errored
            needs_attempt = slot in _CONTINUING
            if not complete or (needs_attempt and state.attempt != Outcome.RETURNED.value):
                reads_retired = False
            slots.append(SlotReport(slot.value, state.end_seen, state.errored, complete,
                                    state.data_callbacks, state.attempt))
        components = {c: self._components.get(c, "NOT_REPORTED") for c in Component}
        quiescent = (
            self._sealed
            and not self._faults
            and not self._faults_dropped
            and bool(slots)
            and reads_retired
            and self._admission_closed
            and not self._queued
            and self._in_flight is None
            and all(value == Outcome.STOPPED.value for value in components.values())
            and self._disconnect == Outcome.RETURNED.value
        )
        return RetirementReport(
            authority="NON_AUTHORITATIVE", trading_permission=False, paper_read_1="NOT_ASSESSED",
            broker_side_retirement=BROKER_SIDE_RETIREMENT, snapshot_retirement="MODELLED_INTENT_ONLY",
            sealed=self._sealed, slots=tuple(slots), admission_closed=self._admission_closed,
            queued_callbacks=len(self._queued), in_flight_callbacks=int(self._in_flight is not None),
            consumer=components[Component.CONSUMER], reader=components[Component.READER],
            socket=components[Component.SOCKET], disconnect_attempt=self._disconnect,
            faults=tuple(f.value for f in self._faults), faults_dropped=self._faults_dropped,
            local_quiescence_established=quiescent,
        )
