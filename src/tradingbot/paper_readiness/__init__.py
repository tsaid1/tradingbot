"""Offline IBKR read-only readiness scaffold behind a deterministic fake transport (issue #16).

NON-AUTHORITATIVE. No socket, HTTP, broker SDK/client, credential loader, live endpoint or market-data
request exists in this package; connected checks are blocked until the owner prerequisites listed in
docs/PAPER_READINESS.md are verified separately. The shared contracts, the deterministic gateway and
the mock ledgers are reused read-only and unchanged.
"""
from tradingbot.paper_readiness.diagnostics import (
    NOTICE,
    Finding,
    FindingKind,
    ObservationDigest,
    ReadinessOutcome,
    ReadinessReport,
    Stage,
)
from tradingbot.paper_readiness.observations import (
    AUTHORITY,
    RECORDS_VERSION,
    ReadinessExpectation,
    SessionAttestation,
    TargetContract,
)
from tradingbot.paper_readiness.preflight import PreflightDecision, preflight
from tradingbot.paper_readiness.runner import render_json, render_text, run_readiness_check
from tradingbot.paper_readiness.transport import (
    FakeClock,
    FakeReadOnlyTransport,
    FakeScript,
    ReadOnlyTransport,
)

__all__ = [
    "AUTHORITY",
    "NOTICE",
    "RECORDS_VERSION",
    "FakeClock",
    "FakeReadOnlyTransport",
    "FakeScript",
    "Finding",
    "FindingKind",
    "ObservationDigest",
    "PreflightDecision",
    "ReadOnlyTransport",
    "ReadinessExpectation",
    "ReadinessOutcome",
    "ReadinessReport",
    "SessionAttestation",
    "Stage",
    "TargetContract",
    "preflight",
    "render_json",
    "render_text",
    "run_readiness_check",
]
