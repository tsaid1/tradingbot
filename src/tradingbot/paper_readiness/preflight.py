"""Environment and account attestation gate: runs BEFORE any read is dispatched (issue #16).

The gate reads only the operator's explicit ``ReadinessExpectation`` and the transport's
``SessionAttestation``. It refuses, with every applicable reason, when the environment is not
explicitly PAPER, when the expected synthetic account is not the single managed account, when the
Read-Only API setting is not attested, when the session is not connected or has lost connectivity.
A socket port is never evidence of anything: the documented defaults are user-configurable.
"""
from __future__ import annotations

from pydantic import Field

from tradingbot.models import StrictModel
from tradingbot.paper_readiness.account_ref import MatchStatus
from tradingbot.paper_readiness.diagnostics import Finding, FindingKind, Stage
from tradingbot.paper_readiness.observations import (
    SYNTHETIC_ACCOUNT_RE,
    ReadinessExpectation,
    ReadinessExpectationV2,
    SessionAttestation,
    SessionAttestationV2,
)

__all__ = ["CONNECTIVITY_LOST", "CONNECTIVITY_RESTORED", "PreflightDecision", "preflight", "preflight_v2"]

CONNECTIVITY_LOST = 1100       # "Connectivity between IB and the TWS has been lost."
CONNECTIVITY_RESTORED = frozenset({1101, 1102})  # restored, data lost / data maintained


class PreflightDecision(StrictModel):
    refused: bool
    refusals: list[Finding] = Field(default_factory=list)
    notes: list[Finding] = Field(default_factory=list)


def _refusal(code: str, detail: str) -> Finding:
    return Finding(code=code, kind=FindingKind.REFUSED, stage=Stage.PREFLIGHT, detail=detail)


def connectivity_is_lost(messages) -> bool:
    """True when the most recent connectivity message is a loss (1100) with no later restoration."""
    state = False
    for m in messages:
        if m.code == CONNECTIVITY_LOST:
            state = True
        elif m.code in CONNECTIVITY_RESTORED:
            state = False
    return state


def _session_head(environment: str, session, refusals: list[Finding]) -> None:
    """Connection, connectivity and environment refusals, shared by both versions in this order."""
    if not session.connected:
        refusals.append(_refusal("SESSION_NOT_CONNECTED", "The session attestation says the transport is "
                                 "not connected; nothing can be observed."))
    if connectivity_is_lost(session.messages):
        refusals.append(_refusal("SESSION_CONNECTIVITY_LOST", "Message code 1100 (connectivity between IB and "
                                 "TWS lost) was reported without a later 1101/1102 restoration."))

    claim = session.environment_claim
    if claim == "UNKNOWN":
        refusals.append(_refusal("ENVIRONMENT_UNKNOWN", "The session does not state whether it is PAPER or "
                                 "LIVE. Nothing is inferred from the socket port or anything else."))
    elif claim == "LIVE":
        refusals.append(_refusal("ENVIRONMENT_LIVE_REFUSED", "The session states a LIVE environment; this "
                                 "scaffold admits PAPER only."))
    elif claim != environment:
        refusals.append(_refusal("ENVIRONMENT_MISMATCH", f"The session states {claim} but the expectation is "
                                 f"{environment}."))


def _session_tail(session, refusals: list[Finding], notes: list[Finding]) -> None:
    """The Read-Only API refusal and the socket-port note, shared by both versions in this order."""
    if session.read_only_api_attested is not True:
        refusals.append(_refusal("READ_ONLY_API_NOT_ATTESTED", "The TWS/IB Gateway Read-Only API setting is not "
                                 "attested as enabled. It is not queryable through the API, so an explicit "
                                 "operator attestation is required."))

    if session.socket_port is not None:
        notes.append(Finding(code="SOCKET_PORT_NOT_EVIDENCE", kind=FindingKind.INFO, stage=Stage.PREFLIGHT,
                             detail="A socket port was attested. It is recorded for diagnostics only; the "
                                    "documented default ports are configurable and prove nothing about PAPER "
                                    "versus LIVE."))


def preflight(expectation: ReadinessExpectation, session: SessionAttestation) -> PreflightDecision:
    """Collect every refusal; the caller dispatches reads only when the list is empty."""
    refusals: list[Finding] = []
    notes: list[Finding] = []

    _session_head(expectation.environment, session, refusals)

    accounts = session.managed_accounts
    if not accounts:
        refusals.append(_refusal("ACCOUNT_EVIDENCE_ABSENT", "No managed account was attested (empty "
                                 "managedAccounts list); account identity cannot be established."))
    else:
        if any(not SYNTHETIC_ACCOUNT_RE.fullmatch(a) for a in accounts):
            refusals.append(_refusal("ACCOUNT_ID_NOT_SYNTHETIC", "A managed account identifier does not carry the "
                                     "explicit synthetic marker; this offline scaffold refuses it unseen."))
        if expectation.account_id not in accounts:
            refusals.append(_refusal("ACCOUNT_NOT_IN_MANAGED_LIST", "The expected synthetic account is not among "
                                     "the attested managed accounts."))
        if len(accounts) != 1:
            refusals.append(_refusal("MANAGED_ACCOUNTS_NOT_SINGULAR", f"{len(accounts)} managed accounts were "
                                     "attested; the approved paper scope is a single isolated account."))

    _session_tail(session, refusals, notes)
    return PreflightDecision(refused=bool(refusals), refusals=refusals, notes=notes)


# The owner's comparison status maps onto the existing account refusals, with fixed redacted detail.
_V2_ACCOUNT_REFUSALS = {
    MatchStatus.ABSENT: ("ACCOUNT_EVIDENCE_ABSENT",
                         ("No managed account was observed; account identity cannot be established.")),
    MatchStatus.MISMATCH: ("ACCOUNT_NOT_IN_MANAGED_LIST",
                           ("The expected account is not the observed managed account.")),
    MatchStatus.AMBIGUOUS: ("MANAGED_ACCOUNTS_NOT_SINGULAR",
                            ("More than one managed account was observed; the approved paper scope is a "
                             "single isolated account.")),
    MatchStatus.REFUSED: ("ACCOUNT_EVIDENCE_REFUSED",
                          ("The expected or managed account evidence was malformed and was refused before "
                           "comparison.")),
}


def preflight_v2(expectation: object, session: object, account_status: object,
                 binding_owned: bool) -> PreflightDecision:
    """Version 2 gate. Account identity comes only from the private owner's own comparison status and its
    confirmation that it issued the binding; the expectation's reference alone is never authority.

    Version 1 records here refuse as ``RECORDS_VERSION_MISMATCH``; nothing is converted."""
    refusals: list[Finding] = []
    notes: list[Finding] = []
    if type(session) is not SessionAttestationV2 or (
            expectation is not None and type(expectation) is not ReadinessExpectationV2):
        refusals.append(_refusal("RECORDS_VERSION_MISMATCH", "Version 2 preflight admits only version 2 "
                                 "expectation and session records; nothing was converted."))
        return PreflightDecision(refused=True, refusals=refusals, notes=notes)

    _session_head("PAPER", session, refusals)

    refusal = _V2_ACCOUNT_REFUSALS.get(account_status) if type(account_status) is MatchStatus else (
        "ACCOUNT_EVIDENCE_REFUSED", "The account comparison status is not a closed comparison result.")
    if refusal is not None:
        refusals.append(_refusal(*refusal))
    elif expectation is None or binding_owned is not True:
        refusals.append(_refusal("BINDING_REFUSED", "No binding issued by this run's owner was confirmed; a "
                                 "reference or caller result is never accepted as one."))

    _session_tail(session, refusals, notes)
    return PreflightDecision(refused=bool(refusals), refusals=refusals, notes=notes)
