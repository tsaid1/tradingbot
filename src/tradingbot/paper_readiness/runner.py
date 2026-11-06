"""One offline readiness check: transport kind gate, attestation preflight, four correlated reads,
fail-closed diagnostics, one NON-AUTHORITATIVE report (issue #16).

Order of operations is fixed and observable through ``ReadinessReport.dispatched``:

1. refuse any transport that is not the FAKE kind (connected checks are blocked in this scaffold);
2. take the session attestation and run ``preflight``; a refusal dispatches nothing;
3. request contract details; only a unique, consistent identity yields a conId for one quote snapshot;
4. request the account summary and the positions for the expected synthetic account;
5. correlate every response stream deterministically (stray, duplicate, permuted, truncated, post-END);
6. assess each stage and derive the outcome: ``CONTROL_COMPLETE`` only when every finding is INFO.

No wall clock is read: ``as_of`` and every freshness judgement come from the caller's ``FakeClock``.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256

from tradingbot.models import MockPolicy
from tradingbot.paper_readiness.diagnostics import (
    HEADLINE_ORDER,
    Finding,
    FindingKind,
    ObservationDigest,
    ReadinessOutcome,
    ReadinessReport,
    Stage,
    assess_account_summary,
    assess_contract,
    assess_positions,
    assess_quote,
    assess_session,
    redact_account,
)
from tradingbot.paper_readiness.observations import (
    END_KINDS,
    ApiMessage,
    ReadinessExpectation,
    ResponseEnvelope,
    ResponseKind,
    SessionAttestation,
)
from tradingbot.paper_readiness.preflight import preflight
from tradingbot.paper_readiness.transport import FAKE_TRANSPORT_KIND, FakeClock, ReadOnlyTransport

__all__ = [
    "MAX_RESPONSES_PER_REQUEST",
    "REQUEST_IDS",
    "Correlated",
    "correlate",
    "render_json",
    "render_text",
    "run_readiness_check",
]

# Bounded handling: a stream longer than this is truncated and reported INCOMPLETE.
MAX_RESPONSES_PER_REQUEST = 64
# Fixed request ids, so scripted envelopes (and future connected logs) correlate by construction.
REQUEST_IDS = {"request_contract_details": 1001, "request_quote_snapshot": 1002,
               "request_account_summary": 1003, "request_positions": 1004}
_STREAMS = {
    "request_contract_details": (Stage.CONTRACT, ResponseKind.CONTRACT_DETAILS_END,
                                 frozenset({ResponseKind.CONTRACT_DETAILS})),
    "request_quote_snapshot": (Stage.QUOTE, ResponseKind.TICK_SNAPSHOT_END,
                               frozenset({ResponseKind.MARKET_DATA_TYPE, ResponseKind.QUOTE})),
    "request_account_summary": (Stage.ACCOUNT, ResponseKind.ACCOUNT_SUMMARY_END,
                                frozenset({ResponseKind.ACCOUNT_SUMMARY})),
    "request_positions": (Stage.POSITIONS, ResponseKind.POSITION_END, frozenset({ResponseKind.POSITION})),
}


@dataclass
class Correlated:
    kept: list[ResponseEnvelope]
    errors: list[ApiMessage]
    end_seen: bool
    findings: list[Finding]
    digest: ObservationDigest

    def payloads(self, kind: ResponseKind) -> list:
        field = {ResponseKind.CONTRACT_DETAILS: "contract", ResponseKind.MARKET_DATA_TYPE: "market_data_type",
                 ResponseKind.QUOTE: "quote", ResponseKind.ACCOUNT_SUMMARY: "account_summary",
                 ResponseKind.POSITION: "position"}[kind]
        return [getattr(e, field) for e in self.kept if e.kind == kind]


def _c(code: str, kind: FindingKind, detail: str, request_id: int) -> Finding:
    return Finding(code=code, kind=kind, stage=Stage.CORRELATION, detail=detail, request_id=request_id)


def correlate(envelopes: list[ResponseEnvelope], *, request_id: int, stage: Stage,
              end_kind: ResponseKind, data_kinds: frozenset[ResponseKind]) -> Correlated:
    """Deterministic, bounded correlation of one response stream.

    Stray request ids are ignored (AMBIGUOUS), the stream is truncated at the bound (INCOMPLETE), arrival
    order is normalised by sequence (INFO when it had to be), exact duplicates are dropped (INFO) while
    conflicting duplicates are MISMATCH, anything after the first END marker is ignored (AMBIGUOUS) and
    kinds foreign to the request are dropped (AMBIGUOUS). ERROR envelopes become message codes.
    """
    findings: list[Finding] = []
    own = [e for e in envelopes if e.request_id == request_id]
    stray = len(envelopes) - len(own)
    if stray:
        findings.append(_c("UNCORRELATED_RESPONSE_IGNORED", FindingKind.AMBIGUOUS,
                           f"{stray} response(s) carried another request id and were ignored.", request_id))
    if len(own) > MAX_RESPONSES_PER_REQUEST:
        findings.append(_c("RESPONSE_LIMIT_EXCEEDED", FindingKind.INCOMPLETE,
                           f"More than {MAX_RESPONSES_PER_REQUEST} responses arrived; the stream was truncated.",
                           request_id))
        own = own[:MAX_RESPONSES_PER_REQUEST]
    sequences = [e.sequence for e in own]
    if sequences != sorted(sequences):
        findings.append(_c("OUT_OF_ORDER_RESPONSES_REORDERED", FindingKind.INFO,
                           "Responses arrived out of sequence and were reordered by sequence number.",
                           request_id))
    own = sorted(own, key=lambda e: e.sequence)

    kept: list[ResponseEnvelope] = []
    seen: dict[int, ResponseEnvelope] = {}
    duplicates = conflicts = 0
    for e in own:
        prior = seen.get(e.sequence)
        if prior is None:
            seen[e.sequence] = e
            kept.append(e)
        elif prior == e:
            duplicates += 1
        else:
            conflicts += 1
    if duplicates:
        findings.append(_c("DUPLICATE_RESPONSE_DROPPED", FindingKind.INFO,
                           f"{duplicates} exact duplicate response(s) were dropped.", request_id))
    if conflicts:
        findings.append(_c("CONFLICTING_DUPLICATE_RESPONSE", FindingKind.MISMATCH,
                           f"{conflicts} response(s) repeated a sequence number with different content.",
                           request_id))

    end_index = next((i for i, e in enumerate(kept) if e.kind == end_kind), None)
    end_seen = end_index is not None
    if end_seen and end_index + 1 < len(kept):
        findings.append(_c("RESPONSE_AFTER_END_IGNORED", FindingKind.AMBIGUOUS,
                           f"{len(kept) - end_index - 1} response(s) arrived after the END marker and were "
                           "ignored.", request_id))
        kept = kept[:end_index + 1]
    allowed = data_kinds | {end_kind, ResponseKind.ERROR}
    foreign = [e for e in kept if e.kind not in allowed]
    if foreign:
        findings.append(_c("UNEXPECTED_RESPONSE_KIND", FindingKind.AMBIGUOUS,
                           f"{len(foreign)} response(s) of a kind foreign to this request were dropped.",
                           request_id))
        kept = [e for e in kept if e.kind in allowed]

    errors = [e.message for e in kept if e.kind == ResponseKind.ERROR and e.message is not None]
    canonical = json.dumps([e.model_dump(mode="json") for e in kept], sort_keys=True, separators=(",", ":"))
    rows = sum(e.kind not in END_KINDS and e.kind != ResponseKind.ERROR for e in kept)
    digest = ObservationDigest(stage=stage, request_id=request_id, rows=rows, end_seen=end_seen,
                               digest=sha256(canonical.encode("utf-8")).hexdigest())
    return Correlated(kept=kept, errors=errors, end_seen=end_seen, findings=findings, digest=digest)


def _report(expectation: ReadinessExpectation, *, as_of, label, findings, dispatched, observations,
            outcome: ReadinessOutcome | None = None) -> ReadinessReport:
    counts = {kind.value: 0 for kind in FindingKind}
    for f in findings:
        counts[f.kind.value] += 1
    headline = next((k for k in HEADLINE_ORDER if counts[k.value]), FindingKind.INFO)
    if outcome is None:
        outcome = (ReadinessOutcome.CONTROL_COMPLETE if headline == FindingKind.INFO
                   else ReadinessOutcome.NOT_READY)
    return ReadinessReport(
        label=label, as_of=as_of, account_ref=redact_account(expectation.account_id),
        target_symbol=expectation.target.instrument.symbol, outcome=outcome, headline=headline,
        counts=counts, dispatched=dispatched, observations=observations, findings=findings,
    )


def run_readiness_check(transport: ReadOnlyTransport, expectation: ReadinessExpectation, *,
                        clock: FakeClock, policy: MockPolicy | None = None,
                        label: str | None = None) -> ReadinessReport:
    policy = policy or MockPolicy()
    now = clock.now()
    findings: list[Finding] = []
    dispatched: list[str] = []
    observations: list[ObservationDigest] = []
    refused = ReadinessOutcome.REFUSED_BEFORE_DISPATCH

    if getattr(transport, "transport_kind", None) != FAKE_TRANSPORT_KIND:
        findings.append(Finding(code="TRANSPORT_NOT_FAKE", kind=FindingKind.REFUSED, stage=Stage.PREFLIGHT,
                                detail="Only the deterministic FAKE transport is admitted by this scaffold; "
                                       "connected checks remain blocked until the owner prerequisites are "
                                       "verified. Nothing was attested or dispatched."))
        return _report(expectation, as_of=now, label=label, findings=findings, dispatched=dispatched,
                       observations=observations, outcome=refused)

    session = transport.attest_session()
    if not isinstance(session, SessionAttestation):
        findings.append(Finding(code="SESSION_ATTESTATION_INVALID", kind=FindingKind.REFUSED,
                                stage=Stage.PREFLIGHT,
                                detail="The transport did not return a SessionAttestation record."))
        return _report(expectation, as_of=now, label=label, findings=findings, dispatched=dispatched,
                       observations=observations, outcome=refused)
    decision = preflight(expectation, session)
    findings.extend(decision.notes)
    findings.extend(decision.refusals)
    if decision.refused:
        return _report(expectation, as_of=now, label=label, findings=findings, dispatched=dispatched,
                       observations=observations, outcome=refused)
    findings.extend(assess_session(session, now=now))

    def read(method: str, *args) -> Correlated:
        request_id = REQUEST_IDS[method]
        stage, end_kind, data_kinds = _STREAMS[method]
        envelopes = getattr(transport, method)(request_id, *args)
        dispatched.append(method)
        result = correlate(list(envelopes), request_id=request_id, stage=stage, end_kind=end_kind,
                           data_kinds=data_kinds)
        findings.extend(result.findings)
        observations.append(result.digest)
        return result

    contract = read("request_contract_details", expectation.target)
    contract_findings, con_id = assess_contract(
        expectation, contract.payloads(ResponseKind.CONTRACT_DETAILS), contract.end_seen, contract.errors,
        now=now, request_id=REQUEST_IDS["request_contract_details"])
    findings.extend(contract_findings)

    if con_id is None:
        findings.append(Finding(code="QUOTE_SKIPPED_NO_CONTRACT_IDENTITY", kind=FindingKind.INCOMPLETE,
                                stage=Stage.QUOTE, detail="No quote was requested because contract identity "
                                "was absent, ambiguous or inconsistent."))
    else:
        quote = read("request_quote_snapshot", con_id)
        findings.extend(assess_quote(
            expectation, quote.payloads(ResponseKind.QUOTE), quote.payloads(ResponseKind.MARKET_DATA_TYPE),
            quote.end_seen, quote.errors, con_id, now=now, policy=policy,
            request_id=REQUEST_IDS["request_quote_snapshot"]))

    account = read("request_account_summary", expectation.account_id)
    findings.extend(assess_account_summary(
        expectation, account.payloads(ResponseKind.ACCOUNT_SUMMARY), account.end_seen, account.errors,
        request_id=REQUEST_IDS["request_account_summary"]))

    positions = read("request_positions", expectation.account_id)
    findings.extend(assess_positions(
        expectation, positions.payloads(ResponseKind.POSITION), positions.end_seen, positions.errors,
        request_id=REQUEST_IDS["request_positions"]))

    return _report(expectation, as_of=now, label=label, findings=findings, dispatched=dispatched,
                   observations=observations)


def render_json(report: ReadinessReport) -> str:
    return json.dumps(report.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"


def render_text(report: ReadinessReport, *, summary: bool = False) -> str:
    head = (f"[{report.label or '-'}] outcome={report.outcome.value}  headline={report.headline.value}  "
            f"target={report.target_symbol}  account={report.account_ref}  as_of={report.as_of.isoformat()}")
    if summary:
        return head + "  " + " ".join(f"{f.kind.value}:{f.code}" for f in report.findings) + "\n"
    lines = [f"PAPER READINESS DIAGNOSTIC (records v{report.records_version}) - {report.authority}",
             report.notice, head,
             "dispatched: " + (", ".join(report.dispatched) if report.dispatched else "(nothing)"),
             "findings: " + "  ".join(f"{k.value}={report.counts[k.value]}" for k in FindingKind), ""]
    for f in report.findings:
        where = f" (req {f.request_id})" if f.request_id is not None else ""
        lines.append(f"  {f.stage.value:<11} {f.kind.value:<12} {f.code}{where}")
        lines.append(f"              {f.detail}")
    lines.append("")
    lines.append(f"{report.authority}: diagnostics only; trading_permission={report.trading_permission}; "
                 "no contract, price, size or policy decision was made.")
    return "\n".join(lines) + "\n"
