"""Read-only transport surface and the deterministic fake behind it (issue #16).

``ReadOnlyTransport`` is the *entire* broker surface this scaffold knows: one session attestation and
four read requests (contract details, one quote snapshot, account summary, positions). There is no
order, cancel, replace, reset, settings, subscription or credential method, and ``FORBIDDEN_NAME_RE``
lets the tests prove that nothing of the kind has crept in.

``FakeReadOnlyTransport`` is the only implementation. It replays a scripted ``FakeScript`` under a
``FakeClock``; it opens no socket, speaks no HTTP, instantiates no broker SDK/client and reads no
environment variable, file or credential store. The runner refuses any transport whose
``transport_kind`` is not ``"FAKE"``, so connected checks stay blocked until the owner prerequisites
recorded in docs/PAPER_READINESS.md are verified separately.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import ClassVar, Protocol, runtime_checkable

from pydantic import Field

from tradingbot.models import StrictModel
from tradingbot.paper_readiness.observations import (
    ResponseEnvelope,
    ResponseKind,
    SessionAttestation,
    TargetContract,
)

__all__ = [
    "FAKE_TRANSPORT_KIND",
    "FORBIDDEN_NAME_RE",
    "READ_METHODS",
    "FakeClock",
    "FakeReadOnlyTransport",
    "FakeScript",
    "ReadOnlyTransport",
]

FAKE_TRANSPORT_KIND = "FAKE"
# The complete read surface, in dispatch order. Anything else on a transport is out of contract.
READ_METHODS = ("request_contract_details", "request_quote_snapshot", "request_account_summary",
                "request_positions")
# Vocabulary that must never appear in a public name on this surface.
FORBIDDEN_NAME_RE = re.compile(
    r"order|cancel|replace|modif|reset|setting|subscri|credential|password|secret|token|login|"
    r"place|submit|transmit|exercise|transfer|fund|withdraw|deposit|flatten|liquidat",
    re.IGNORECASE,
)


@runtime_checkable
class ReadOnlyTransport(Protocol):
    """Read-only broker surface. Each read returns the raw, correlated response envelopes the broker
    delivered for the given request id; the runner, not the transport, interprets them."""

    transport_kind: str

    def attest_session(self) -> SessionAttestation: ...

    def request_contract_details(self, request_id: int, target: TargetContract) -> list[ResponseEnvelope]: ...

    def request_quote_snapshot(self, request_id: int, con_id: int) -> list[ResponseEnvelope]: ...

    def request_account_summary(self, request_id: int, account_id: str) -> list[ResponseEnvelope]: ...

    def request_positions(self, request_id: int, account_id: str) -> list[ResponseEnvelope]: ...


class FakeClock:
    """Deterministic, timezone-aware clock. The wall clock is never consulted anywhere in the scaffold."""

    def __init__(self, start: datetime) -> None:
        if start.tzinfo is None:
            raise ValueError("FakeClock requires a timezone-aware start")
        self._now = start

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> datetime:
        self._now = self._now + timedelta(seconds=seconds)
        return self._now


class FakeScript(StrictModel):
    """Everything the fake transport will say, authored up front. Response lists are delivered verbatim
    (including any duplicate, permuted, stray or missing envelope the author scripted)."""
    session: SessionAttestation
    contract_details: list[ResponseEnvelope] = Field(default_factory=list)
    quote_snapshot: list[ResponseEnvelope] = Field(default_factory=list)
    account_summary: list[ResponseEnvelope] = Field(default_factory=list)
    positions: list[ResponseEnvelope] = Field(default_factory=list)


class FakeReadOnlyTransport:
    """Scripted, offline, read-only. Records every dispatch so tests can prove that a refused preflight
    dispatched nothing. Quote envelopes without a ``received_at`` are stamped from the fake clock."""

    transport_kind: ClassVar[str] = FAKE_TRANSPORT_KIND

    def __init__(self, script: FakeScript, clock: FakeClock) -> None:
        self.script = script
        self.clock = clock
        self.attestations = 0
        self.dispatch_log: list[tuple[str, int]] = []

    def attest_session(self) -> SessionAttestation:
        self.attestations += 1
        return self.script.session.model_copy(deep=True)

    def _replay(self, method: str, request_id: int, envelopes: list[ResponseEnvelope]) -> list[ResponseEnvelope]:
        self.dispatch_log.append((method, request_id))
        return [e.model_copy(deep=True) for e in envelopes]

    def request_contract_details(self, request_id: int, target: TargetContract) -> list[ResponseEnvelope]:
        return self._replay("request_contract_details", request_id, self.script.contract_details)

    def request_quote_snapshot(self, request_id: int, con_id: int) -> list[ResponseEnvelope]:
        delivered = self._replay("request_quote_snapshot", request_id, self.script.quote_snapshot)
        stamped = []
        for env in delivered:
            if env.kind == ResponseKind.QUOTE and env.quote is not None and env.quote.received_at is None:
                env = env.model_copy(update={"quote": env.quote.model_copy(update={"received_at": self.clock.now()})})
            stamped.append(env)
        return stamped

    def request_account_summary(self, request_id: int, account_id: str) -> list[ResponseEnvelope]:
        return self._replay("request_account_summary", request_id, self.script.account_summary)

    def request_positions(self, request_id: int, account_id: str) -> list[ResponseEnvelope]:
        return self._replay("request_positions", request_id, self.script.positions)
