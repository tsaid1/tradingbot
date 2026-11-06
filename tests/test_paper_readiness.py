"""S2-PAPER-01 offline IBKR read-only readiness scaffold (issue #16). Synthetic inputs only; NON-AUTHORITATIVE.

Covers: import origin pinned to this checkout; the read-only surface (no mutation, settings, subscription or
credential vocabulary; no network, SDK or wall-clock use); explicitly synthetic records; preflight refusals
that dispatch nothing and never infer PAPER from a port; the positive deterministic fake-clock TLT control
and the clock-driven transitions to stale and out-of-session; every negative case named in the issue
(unknown/live/mismatched environment, absent account evidence, ambiguous contract identity, stale/delayed/
untimed quote, unavailable entitlement, incomplete/disconnected snapshots, duplicate/out-of-order/uncorrelated
responses); bounded correlation; documented liquid-hours formats; safe diagnostics without raw payload; the
CLI; and that the deterministic gateway and the shared policy are untouched.
"""
from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

import tradingbot
import tradingbot.paper_readiness as pr
from tradingbot.event_models import PriceBasis, Timing, TradeAction
from tradingbot.models import Action, AssetClass, Instrument, MockPolicy, PositionSide, Quote
from tradingbot.paper_readiness import scenarios as sc
from tradingbot.paper_readiness.diagnostics import (
    HEADLINE_ORDER,
    REQUEST_CODES,
    FindingKind,
    ReadinessOutcome,
    Stage,
    assess_account_summary,
    assess_contract,
    assess_positions,
    assess_quote,
    assess_session,
    classify_messages,
    parse_liquid_hours,
    redact_account,
)
from tradingbot.paper_readiness.observations import (
    RECORDS_VERSION,
    AccountSummaryObservation,
    ApiMessage,
    MarketDataType,
    PositionLine,
    ReadinessExpectation,
    ResponseEnvelope,
    ResponseKind,
    SessionAttestation,
    TargetContract,
)
from tradingbot.paper_readiness.preflight import preflight
from tradingbot.paper_readiness.runner import (
    MAX_RESPONSES_PER_REQUEST,
    REQUEST_IDS,
    correlate,
    render_json,
    render_text,
    run_readiness_check,
)
from tradingbot.paper_readiness.transport import (
    FORBIDDEN_NAME_RE,
    READ_METHODS,
    FakeClock,
    FakeReadOnlyTransport,
    ReadOnlyTransport,
)
from tradingbot.validator import gate_action

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"
PACKAGE = SRC / "tradingbot" / "paper_readiness"
SCRIPT = REPO / "scripts" / "inspect_paper_readiness.py"
NOW = sc.CLOCK_START
REFUSED, NOT_READY, COMPLETE = (ReadinessOutcome.REFUSED_BEFORE_DISPATCH, ReadinessOutcome.NOT_READY,
                                ReadinessOutcome.CONTROL_COMPLETE)
CONTROL_CODES = {"SESSION_ATTESTATION_CONTROL", "CONTRACT_IDENTITY_CONTROL", "REGULAR_SESSION_CONTROL",
                 "QUOTE_LIVE_CONTROL", "ACCOUNT_SNAPSHOT_CONTROL", "POSITIONS_SNAPSHOT_CONTROL"}
AUTHORIZATION_VOCABULARY = ("APPROVE", "EXECUTE_MOCK", "SIMULATED", "ELIGIBLE", "AUTHORIZED", '"quantity"',
                            "order_id", "placeOrder")
# Module roots the package may import: standard library (no I/O, no network), pydantic and this project.
ALLOWED_IMPORT_ROOTS = {"__future__", "re", "json", "datetime", "decimal", "enum", "hashlib", "typing",
                        "dataclasses", "zoneinfo", "pydantic", "tradingbot"}
NETWORK_OR_BROKER_MODULES = {"socket", "ssl", "http", "urllib", "urllib3", "requests", "httpx", "aiohttp",
                             "asyncio", "websocket", "websockets", "ibapi", "ib_insync", "ib_async", "ibind"}
WALL_CLOCK_RE = re.compile(r"datetime\.now\(|\.utcnow\(|time\.time\(|date\.today\(|\bimport time\b")


def codes(items) -> list[str]:
    findings = getattr(items, "findings", items)
    return [f.code for f in findings]


def control():
    return sc.SCENARIOS["tlt_regular_session_control"]


def quote(**overrides):
    base = {"received_at": NOW}
    base.update(overrides)
    return sc.quote_row(**base)


def assess(q, *, types=(MarketDataType.LIVE,), end=True, errors=(), con_id=sc.SYNTH_CON_ID, now=NOW):
    return assess_quote(sc.EXPECTATION, [q], list(types), end, list(errors), con_id, now=now,
                        policy=MockPolicy(), request_id=REQUEST_IDS["request_quote_snapshot"])


def contract(rows, *, end=True, errors=(), now=NOW, expectation=sc.EXPECTATION):
    return assess_contract(expectation, rows, end, list(errors), now=now,
                           request_id=REQUEST_IDS["request_contract_details"])


# ----------------------------------------------------------------------------- import origin and surface

def test_package_is_imported_from_this_checkout():
    """The shared editable venv may point at another worktree; the suite must exercise this one."""
    assert Path(tradingbot.__file__).resolve().parents[2] == REPO
    assert Path(pr.__file__).resolve().parent == PACKAGE
    assert Path(sc.__file__).resolve() == PACKAGE / "scenarios.py"


def test_read_only_surface_is_exactly_one_attestation_and_four_reads():
    expected = {"transport_kind", "attest_session", *READ_METHODS}
    fake_public = {n for n in dir(FakeReadOnlyTransport) if not n.startswith("_")}
    assert fake_public == expected
    protocol_public = ({n for n, v in vars(ReadOnlyTransport).items() if not n.startswith("_") and callable(v)}
                       | set(ReadOnlyTransport.__annotations__))
    assert protocol_public == expected
    transport, _ = sc.build_transport(control())
    assert isinstance(transport, ReadOnlyTransport)
    for name in expected | set(vars(transport)):
        assert not FORBIDDEN_NAME_RE.search(name), name
    for word in ("placeOrder", "cancelOrder", "reqGlobalCancel", "reset", "settings", "subscribe", "password"):
        assert FORBIDDEN_NAME_RE.search(word), word  # the guard itself catches the forbidden vocabulary


def test_package_imports_only_pure_standard_library_pydantic_and_this_project():
    for path in sorted(PACKAGE.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots = {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom):
                roots = {(node.module or "").split(".")[0]}
            else:
                continue
            assert roots <= ALLOWED_IMPORT_ROOTS, (path.name, roots)
        assert not WALL_CLOCK_RE.search(path.read_text(encoding="utf-8")), path.name
    assert not WALL_CLOCK_RE.search(SCRIPT.read_text(encoding="utf-8"))


def test_importing_the_package_loads_no_network_or_broker_module():
    probe = (
        "import sys, json\n"
        "import tradingbot.operations_view\n"           # the package's heaviest shared dependency
        "before = set(sys.modules)\n"
        "import tradingbot.paper_readiness, tradingbot.paper_readiness.scenarios\n"
        "print(json.dumps(sorted(m.split('.')[0] for m in set(sys.modules) - before)))\n"
    )
    out = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, check=True,
                         env={**os.environ, "PYTHONPATH": str(SRC)})
    loaded = set(json.loads(out.stdout))
    assert loaded & NETWORK_OR_BROKER_MODULES == set(), loaded & NETWORK_OR_BROKER_MODULES
    assert "tradingbot" in loaded


# ----------------------------------------------------------------------------- explicit synthetic records

def test_target_contract_admits_only_direct_identity_usd_stk():
    etf = sc.TLT_TARGET.instrument
    assert sc.TLT_TARGET.sec_type == "STK" and sc.TLT_TARGET.currency == "USD" and sc.TLT_TARGET.con_id is None
    rejected = [
        {"instrument": Instrument(asset_class=AssetClass.OPTION, symbol="TLT", exchange="SMART")},
        {"instrument": Instrument(asset_class=AssetClass.FUTURE, symbol="ZZF", exchange="SMART")},
        {"instrument": Instrument(asset_class=AssetClass.ETF, symbol="TLT")},                       # no exchange
        {"instrument": Instrument(asset_class=AssetClass.ETF, symbol="TLT", exchange="SMART", expiry="2026-12-18")},
        {"instrument": Instrument(asset_class=AssetClass.ETF, symbol="tlt", exchange="SMART")},     # gateway pattern
        {"instrument": etf, "currency": "EUR"},
        {"instrument": etf, "sec_type": "OPT"},
        {"instrument": etf, "primary_exchange": "   "},
        {"instrument": etf, "con_id": 0},
        {"instrument": etf, "broker": "any"},                                                      # extra field
    ]
    for overrides in rejected:
        kwargs = {"primary_exchange": "SYNTH-X", **overrides}
        with pytest.raises((ValidationError, ValueError)):
            TargetContract(**kwargs)


def test_expectation_and_attestation_admit_only_synthetic_paper_vocabulary():
    for bad in ("ACCOUNT-1", "synth-paper0001", "SYNTH-", "SYNTH-paper", "", "SYNTH-" + "A" * 25, "DU-SYNTH"):
        with pytest.raises(ValidationError):
            ReadinessExpectation(account_id=bad, target=sc.TLT_TARGET)
    with pytest.raises(ValidationError):
        ReadinessExpectation(account_id=sc.SYNTH_ACCOUNT, target=sc.TLT_TARGET, environment="LIVE")
    with pytest.raises(ValidationError):
        ReadinessExpectation(account_id=sc.SYNTH_ACCOUNT, target=sc.TLT_TARGET, session_scope="EXTENDED")
    with pytest.raises(ValidationError):
        SessionAttestation(synthetic=False, connected=True)
    with pytest.raises(ValidationError):
        SessionAttestation(connected=True)
    with pytest.raises(ValueError):
        FakeClock(NOW.replace(tzinfo=None))


def test_response_envelope_payload_must_match_its_kind():
    with pytest.raises(ValidationError):
        ResponseEnvelope(request_id=1, sequence=1, kind=ResponseKind.QUOTE)
    with pytest.raises(ValidationError):
        ResponseEnvelope(request_id=1, sequence=1, kind=ResponseKind.TICK_SNAPSHOT_END, quote=quote())
    with pytest.raises(ValidationError):
        ResponseEnvelope(request_id=1, sequence=1, kind=ResponseKind.QUOTE, quote=quote(), message=ApiMessage(code=1))
    assert ResponseEnvelope(request_id=1, sequence=1, kind=ResponseKind.ERROR, message=ApiMessage(code=354)).message.code == 354


def test_readiness_records_are_versioned_locally_and_not_adopted_by_the_shared_contracts():
    assert RECORDS_VERSION == 1 and sc.run_scenario(control()).records_version == 1
    for name in ("ReadinessReport", "ReadinessExpectation", "SessionAttestation", "TargetContract", "Finding"):
        assert not hasattr(tradingbot.models, name), name


# ----------------------------------------------------------------------------- preflight: refuse before dispatch

def test_preflight_collects_every_refusal_and_never_consults_the_port():
    bad = sc.session(environment_claim="UNKNOWN", socket_port=7497, managed_accounts=[], read_only_api_attested=None,
                     connected=False)
    decision = preflight(sc.EXPECTATION, bad)
    assert decision.refused
    assert codes(decision.refusals) == ["SESSION_NOT_CONNECTED", "ENVIRONMENT_UNKNOWN", "ACCOUNT_EVIDENCE_ABSENT",
                                        "READ_ONLY_API_NOT_ATTESTED"]
    assert codes(decision.notes) == ["SOCKET_PORT_NOT_EVIDENCE"]
    # An explicit PAPER claim passes on the documented live default port, the paper default and no port at all.
    for port in (7496, 7497, 4001, 4002, None):
        assert not preflight(sc.EXPECTATION, sc.session(socket_port=port)).refused
    # Paper default port with no explicit claim is still refused: the port is not evidence.
    assert "ENVIRONMENT_UNKNOWN" in codes(preflight(sc.EXPECTATION, sc.session(environment_claim="UNKNOWN",
                                                                                 socket_port=7497)).refusals)
    two = preflight(sc.EXPECTATION, sc.session(managed_accounts=[sc.SYNTH_ACCOUNT, sc.OTHER_SYNTH_ACCOUNT]))
    assert codes(two.refusals) == ["MANAGED_ACCOUNTS_NOT_SINGULAR"]
    unmarked = preflight(sc.EXPECTATION, sc.session(managed_accounts=["ACCOUNT-1"]))
    assert codes(unmarked.refusals) == ["ACCOUNT_ID_NOT_SYNTHETIC", "ACCOUNT_NOT_IN_MANAGED_LIST"]
    lost = preflight(sc.EXPECTATION, sc.session(messages=[ApiMessage(code=1102), ApiMessage(code=1100)]))
    assert codes(lost.refusals) == ["SESSION_CONNECTIVITY_LOST"]
    restored = preflight(sc.EXPECTATION, sc.session(messages=[ApiMessage(code=1100), ApiMessage(code=1102)]))
    assert not restored.refused


@pytest.mark.parametrize("name", ["environment_unknown_port_7497", "environment_live", "account_mismatch",
                                  "account_evidence_absent", "read_only_api_not_attested", "session_disconnected"])
def test_refused_preflight_dispatches_no_read(name):
    scenario = sc.SCENARIOS[name]
    transport, clock = sc.build_transport(scenario)
    report = run_readiness_check(transport, scenario.expectation, clock=clock, label=name)
    assert report.outcome == REFUSED and report.headline == FindingKind.REFUSED
    assert transport.attestations == 1 and transport.dispatch_log == [] and report.dispatched == []
    assert report.observations == []
    assert set(scenario.expected_codes) <= set(codes(report))
    assert all(f.stage == Stage.PREFLIGHT for f in report.findings)


def test_non_fake_transport_is_refused_before_attestation():
    class SocketLikeTransport:
        transport_kind = "SOCKET"

        def __init__(self):
            self.calls = 0

        def attest_session(self):
            self.calls += 1
            raise AssertionError("a non-fake transport must never be attested or dispatched")

        def request_contract_details(self, request_id, target):
            raise AssertionError

        def request_quote_snapshot(self, request_id, con_id):
            raise AssertionError

        def request_account_summary(self, request_id, account_id):
            raise AssertionError

        def request_positions(self, request_id, account_id):
            raise AssertionError

    transport = SocketLikeTransport()
    assert isinstance(transport, ReadOnlyTransport)  # structurally compatible, still refused
    report = run_readiness_check(transport, sc.EXPECTATION, clock=FakeClock(NOW))
    assert (report.outcome, codes(report)) == (REFUSED, ["TRANSPORT_NOT_FAKE"])
    assert transport.calls == 0 and report.dispatched == []


# ----------------------------------------------------------------------------- positive control and the fake clock

def test_positive_tlt_control_is_complete_consistent_and_informational_only():
    report = sc.run_scenario(control())
    assert report.outcome == COMPLETE and report.headline == FindingKind.INFO
    assert report.dispatched == list(READ_METHODS)
    assert CONTROL_CODES <= set(codes(report))
    assert all(f.kind == FindingKind.INFO for f in report.findings)
    assert report.counts["INFO"] == len(report.findings) and sum(report.counts.values()) == len(report.findings)
    assert [o.stage for o in report.observations] == [Stage.CONTRACT, Stage.QUOTE, Stage.ACCOUNT, Stage.POSITIONS]
    assert all(o.end_seen for o in report.observations) and [o.rows for o in report.observations] == [1, 2, 1, 0]
    assert report.trading_permission is False and report.authority == "NON_AUTHORITATIVE"
    assert report.target_symbol == "TLT" and report.as_of == NOW
    text, payload = render_text(report), render_json(report)
    assert "trading_permission=False" in text and "NON_AUTHORITATIVE" in text
    for word in AUTHORIZATION_VOCABULARY:
        assert word not in payload and word not in text, word
    assert "CON_ID_OBSERVED_NOT_PINNED" in codes(report)  # the synthetic conId is observed, never pinned


def test_fake_clock_drives_the_same_control_from_complete_to_stale_to_out_of_session():
    scenario = control()
    transport, clock = sc.build_transport(scenario)
    assert run_readiness_check(transport, scenario.expectation, clock=clock).outcome == COMPLETE
    clock.advance(MockPolicy().max_quote_age_seconds + 1 - 10)  # the control quote is 10 s old at start
    stale = run_readiness_check(transport, scenario.expectation, clock=clock)
    assert stale.outcome == NOT_READY and stale.headline == FindingKind.STALE
    assert "QUOTE_STALE" in codes(stale) and "QUOTE_LIVE_CONTROL" not in codes(stale)
    assert "REGULAR_SESSION_CONTROL" in codes(stale)
    clock.advance(6 * 3600)  # 16:30 America/New_York: after the observed liquid hours
    late = run_readiness_check(transport, scenario.expectation, clock=clock)
    assert late.headline == FindingKind.STALE and {"OUTSIDE_REGULAR_SESSION", "QUOTE_STALE"} <= set(codes(late))
    assert late.as_of == clock.now() and transport.attestations == 3


def test_pinned_con_id_must_match_and_a_mismatch_prevents_the_quote():
    pinned = sc.EXPECTATION.model_copy(update={"target": sc.TLT_TARGET.model_copy(update={"con_id": sc.SYNTH_CON_ID})})
    findings, con_id = contract([sc.contract_row()], expectation=pinned)
    assert con_id == sc.SYNTH_CON_ID and "CON_ID_OBSERVED_NOT_PINNED" not in codes(findings)
    wrong = pinned.model_copy(update={"target": pinned.target.model_copy(update={"con_id": 123456})})
    findings, con_id = contract([sc.contract_row()], expectation=wrong)
    assert con_id is None and "CON_ID_MISMATCH" in codes(findings)
    transport, clock = sc.build_transport(control())
    report = run_readiness_check(transport, wrong, clock=clock)
    assert "request_quote_snapshot" not in report.dispatched and "QUOTE_SKIPPED_NO_CONTRACT_IDENTITY" in codes(report)


def test_quote_is_withheld_unless_the_contract_stream_establishes_identity():
    """Review follow-up (PR #19): a conflicting duplicate sequence with differing conIds, a missing
    contractDetailsEnd, a stray request id, a row after END and a broker error must each withhold the quote;
    exact duplicates and reordering (INFO) must not."""

    def run(contract_stream):
        script = sc.CONTROL.model_copy(update={"contract_details": contract_stream})
        transport = FakeReadOnlyTransport(script, FakeClock(NOW))
        report = run_readiness_check(transport, sc.EXPECTATION, clock=transport.clock)
        return report, sum(method == "request_quote_snapshot" for method, _ in transport.dispatch_log)

    row = sc.env("request_contract_details", 1, ResponseKind.CONTRACT_DETAILS, contract=sc.contract_row())
    end = sc.env("request_contract_details", 2, ResponseKind.CONTRACT_DETAILS_END)
    other = sc.env("request_contract_details", 1, ResponseKind.CONTRACT_DETAILS, contract=sc.contract_row(con_id=900002))
    withheld = {
        "conflicting_duplicate_con_ids": ([row, other, end], "CONFLICTING_DUPLICATE_RESPONSE"),
        "missing_contract_details_end": ([row], "CONTRACT_DETAILS_END_MISSING"),
        "stray_request_id": ([row, sc.env("request_contract_details", 5, ResponseKind.CONTRACT_DETAILS, request_id=4242,
                                          contract=sc.contract_row()), end], "UNCORRELATED_RESPONSE_IGNORED"),
        "row_after_end": ([row, end, sc.env("request_contract_details", 3, ResponseKind.CONTRACT_DETAILS,
                                            contract=sc.contract_row(con_id=900002))], "RESPONSE_AFTER_END_IGNORED"),
        "broker_error_with_row": ([row, sc.env("request_contract_details", 2, ResponseKind.ERROR, message=ApiMessage(code=321)),
                                   sc.env("request_contract_details", 3, ResponseKind.CONTRACT_DETAILS_END)],
                                  "BROKER_REQUEST_VALIDATION_ERROR"),
    }
    for name, (stream, code) in withheld.items():
        report, quote_calls = run(stream)
        assert report.outcome == NOT_READY, name
        assert code in codes(report) and "QUOTE_SKIPPED_NO_CONTRACT_IDENTITY" in codes(report), (name, codes(report))
        assert quote_calls == 0 and "request_quote_snapshot" not in report.dispatched, name
        assert report.dispatched == ["request_contract_details", "request_account_summary", "request_positions"], name
    incomplete, _ = run([row])
    assert "CONTRACT_IDENTITY_CONTROL" not in codes(incomplete)  # no identity control without the END marker
    for name, stream in (("exact_duplicates", [row, row, end, end]), ("reordered", [end, row])):
        report, quote_calls = run(stream)
        assert report.outcome == COMPLETE and quote_calls == 1 and report.dispatched == list(READ_METHODS), name
        assert "CONTRACT_IDENTITY_CONTROL" in codes(report) and "QUOTE_LIVE_CONTROL" in codes(report), name


# ----------------------------------------------------------------------------- every scripted scenario

@pytest.mark.parametrize("name", list(sc.SCENARIOS))
def test_scenarios_reach_their_scripted_outcome_deterministically(name):
    scenario = sc.SCENARIOS[name]
    transport, clock = sc.build_transport(scenario)
    report = run_readiness_check(transport, scenario.expectation, clock=clock, label=name)
    assert report.outcome == scenario.expected_outcome, codes(report)
    assert set(scenario.expected_codes) <= set(codes(report)), codes(report)
    assert report.as_of == scenario.clock_start and report.label == name
    assert transport.attestations == 1
    assert [method for method, _ in transport.dispatch_log] == report.dispatched
    assert [rid for _, rid in transport.dispatch_log] == [REQUEST_IDS[m] for m in report.dispatched]
    if "QUOTE_SKIPPED_NO_CONTRACT_IDENTITY" in codes(report):
        assert "request_quote_snapshot" not in report.dispatched
    non_info = [f for f in report.findings if f.kind != FindingKind.INFO]
    assert (report.outcome == COMPLETE) == (not non_info)
    assert report.headline == next((k for k in HEADLINE_ORDER if report.counts[k.value]), FindingKind.INFO)
    again, clock_again = sc.build_transport(scenario)
    assert render_json(run_readiness_check(again, scenario.expectation, clock=clock_again, label=name)) == render_json(report)


@pytest.mark.parametrize("name", list(sc.SCENARIOS))
def test_diagnostics_carry_no_account_identifier_balance_price_or_raw_payload(name):
    report = sc.run_scenario(sc.SCENARIOS[name])
    text = render_json(report) + render_text(report) + render_text(report, summary=True)
    for raw in (sc.SYNTH_ACCOUNT, sc.OTHER_SYNTH_ACCOUNT, "1000000", "4000000", "100.00", "100.02", "100.01",
                "50.00", "SYNTHETIC TLT PLACEHOLDER", "SYNTH-LISTING"):
        assert raw not in text, raw
    assert report.account_ref == redact_account(sc.SYNTH_ACCOUNT)
    assert re.fullmatch(r"SYNTH-\.\.\.[0-9a-f]{8}", report.account_ref)
    for digest in report.observations:
        assert re.fullmatch(r"[0-9a-f]{64}", digest.digest)


def test_correlation_anomalies_are_handled_deterministically_and_bounded():
    rid = REQUEST_IDS["request_quote_snapshot"]
    kw = {"request_id": rid, "stage": Stage.QUOTE, "end_kind": ResponseKind.TICK_SNAPSHOT_END,
          "data_kinds": frozenset({ResponseKind.MARKET_DATA_TYPE, ResponseKind.QUOTE})}
    clean = sc.CONTROL.quote_snapshot
    base = correlate(clean, **kw)
    doubled = correlate([e for e in clean for _ in range(2)], **kw)
    permuted = correlate(list(reversed(clean)), **kw)
    assert codes(base.findings) == []
    assert codes(doubled.findings) == ["DUPLICATE_RESPONSE_DROPPED"]
    assert codes(permuted.findings) == ["OUT_OF_ORDER_RESPONSES_REORDERED"]
    assert base.digest.digest == doubled.digest.digest == permuted.digest.digest  # same kept set
    assert base.end_seen and base.digest.rows == 2
    retransmit = clean[1].model_copy(update={"quote": quote(bid="1.00", ask="1.01")})  # same sequence, other content
    conflict = correlate(clean[:2] + [retransmit] + clean[2:], **kw)
    assert [(f.code, f.kind) for f in conflict.findings] == [("CONFLICTING_DUPLICATE_RESPONSE", FindingKind.MISMATCH)]
    assert conflict.digest.digest == base.digest.digest  # the first delivery is kept; the conflict is only reported
    after_end = correlate(clean + [sc.env("request_quote_snapshot", 9, ResponseKind.QUOTE, quote=quote())], **kw)
    assert codes(after_end.findings) == ["RESPONSE_AFTER_END_IGNORED"] and after_end.digest.digest == base.digest.digest
    stray = correlate(clean + [sc.env("request_quote_snapshot", 9, ResponseKind.QUOTE, request_id=4242, quote=quote())], **kw)
    assert [(f.code, f.kind) for f in stray.findings] == [("UNCORRELATED_RESPONSE_IGNORED", FindingKind.AMBIGUOUS)]
    foreign = correlate([sc.env("request_quote_snapshot", 0, ResponseKind.POSITION,
                                position=PositionLine(account_id=sc.SYNTH_ACCOUNT, con_id=1, position="1"))] + clean, **kw)
    assert codes(foreign.findings) == ["UNEXPECTED_RESPONSE_KIND"] and foreign.digest.digest == base.digest.digest
    flood = [sc.env("request_quote_snapshot", i, ResponseKind.QUOTE, quote=quote()) for i in range(MAX_RESPONSES_PER_REQUEST + 5)]
    bounded = correlate(flood, **kw)
    assert codes(bounded.findings) == ["RESPONSE_LIMIT_EXCEEDED"] and len(bounded.kept) == MAX_RESPONSES_PER_REQUEST
    assert not bounded.end_seen
    assert correlate([], **kw).end_seen is False and correlate([], **kw).digest.rows == 0


# ----------------------------------------------------------------------------- contract identity and session scope

def test_liquid_hours_parsing_follows_the_documented_formats_and_fails_closed():
    tz = ZoneInfo("America/New_York")
    modern = parse_liquid_hours("20180323:0930-20180323:1600;20180326:0930-20180326:1600", tz)
    assert [(s.hour, s.minute, e.hour) for s, e in modern] == [(9, 30, 16), (9, 30, 16)]
    assert modern[0][0].tzinfo is tz
    legacy = parse_liquid_hours("20090507:0700-1830,1830-2330;20090508:CLOSED", tz)
    assert [(s.hour, e.hour, e.minute) for s, e in legacy] == [(7, 18, 30), (18, 23, 30)]
    assert parse_liquid_hours("20261106:CLOSED;20261107:CLOSED", tz) == []
    for unreadable in ("20090507:1830-0700", "Mon-Fri 9:30-16:00", "20261106:0930-1600x", "0930-1600",
                       "20261399:0930-1600"):
        assert parse_liquid_hours(unreadable, tz) is None, unreadable


def test_regular_session_scope_is_judged_only_from_observed_liquid_hours():
    open_et = datetime(2026, 11, 6, 14, 30, tzinfo=UTC)   # 09:30 America/New_York: inclusive start
    close_et = datetime(2026, 11, 6, 21, 0, tzinfo=UTC)   # 16:00: exclusive end
    assert "REGULAR_SESSION_CONTROL" in codes(contract([sc.contract_row()], now=open_et)[0])
    assert "OUTSIDE_REGULAR_SESSION" in codes(contract([sc.contract_row()], now=close_et)[0])
    assert "OUTSIDE_REGULAR_SESSION" in codes(contract([sc.contract_row(liquid_hours="20261106:CLOSED")])[0])
    unresolved, con_id = contract([sc.contract_row(time_zone_id="Mars/Olympus")])
    assert "TIME_ZONE_UNRESOLVED" in codes(unresolved) and con_id == sc.SYNTH_CON_ID  # identity fine, scope not
    garbled, _ = contract([sc.contract_row(liquid_hours="9:30am-4pm")])
    assert "LIQUID_HOURS_UNPARSEABLE" in codes(garbled)
    missing, con_id = contract([sc.contract_row(liquid_hours=None, time_zone_id=None)])
    assert "CONTRACT_FIELDS_MISSING" in codes(missing) and con_id == sc.SYNTH_CON_ID
    assert not any(c in codes(missing) for c in ("REGULAR_SESSION_CONTROL", "OUTSIDE_REGULAR_SESSION"))


def test_contract_identity_is_explicit_and_every_mismatch_or_gap_withholds_the_con_id():
    for field, value, code in (("symbol", "TLTX", "CONTRACT_SYMBOL_MISMATCH"),
                               ("sec_type", "OPT", "CONTRACT_SEC_TYPE_MISMATCH"),
                               ("currency", "EUR", "CONTRACT_CURRENCY_MISMATCH"),
                               ("exchange", "SYNTH-OTHER", "CONTRACT_EXCHANGE_MISMATCH"),
                               ("primary_exchange", "SYNTH-OTHER", "CONTRACT_PRIMARY_EXCHANGE_MISMATCH")):
        findings, con_id = contract([sc.contract_row(**{field: value})])
        assert code in codes(findings) and con_id is None, field
        assert "CONTRACT_IDENTITY_CONTROL" not in codes(findings)
    for field in ("con_id", "symbol", "sec_type", "currency", "exchange", "primary_exchange"):
        findings, con_id = contract([sc.contract_row(**{field: None})])
        assert "CONTRACT_FIELDS_MISSING" in codes(findings) and con_id is None, field
    assert "CONTRACT_IDENTITY_CONTROL" in codes(contract([sc.contract_row(symbol=" tlt ")])[0])  # trimmed/upper only
    # A single matching row establishes nothing while the stream is incomplete or a broker error accompanies it.
    findings, con_id = contract([sc.contract_row()], end=False)
    assert con_id is None and "CONTRACT_DETAILS_END_MISSING" in codes(findings)
    assert "CONTRACT_IDENTITY_CONTROL" not in codes(findings)
    for code in (321, 1100):
        findings, con_id = contract([sc.contract_row()], errors=[ApiMessage(code=code)])
        assert con_id is None and "CONTRACT_IDENTITY_CONTROL" not in codes(findings), code
    findings, con_id = contract([], end=False)
    assert codes(findings) == ["CONTRACT_DETAILS_END_MISSING", "CONTRACT_OBSERVATION_MISSING"] and con_id is None
    findings, con_id = contract([sc.contract_row(), sc.contract_row(con_id=900002)])
    assert codes(findings) == ["CONTRACT_IDENTITY_AMBIGUOUS"] and con_id is None
    findings, con_id = contract([], errors=[ApiMessage(code=200)])
    assert codes(findings) == ["CONTRACT_NOT_FOUND"] and findings[0].kind == FindingKind.ERROR


# ----------------------------------------------------------------------------- quote, entitlement, freshness

def test_quote_freshness_mirrors_the_shared_policy_window_with_the_gateway_tolerance():
    window = MockPolicy().max_quote_age_seconds
    assert "QUOTE_LIVE_CONTROL" in codes(assess(quote(last_timestamp=NOW - timedelta(seconds=window))))
    assert "QUOTE_STALE" in codes(assess(quote(last_timestamp=NOW - timedelta(seconds=window + 1))))
    assert "QUOTE_LIVE_CONTROL" in codes(assess(quote(last_timestamp=NOW + timedelta(seconds=5))))
    assert "QUOTE_TIMESTAMP_FUTURE" in codes(assess(quote(last_timestamp=NOW + timedelta(seconds=6))))
    assert "QUOTE_UNTIMED" in codes(assess(quote(last_timestamp=None)))
    assert "QUOTE_UNTIMED" in codes(assess(quote(last_timestamp=NOW.replace(tzinfo=None))))
    assert "QUOTE_RECEIPT_UNTIMED" in codes(assess(quote(received_at=None)))
    assert "QUOTE_RECEIPT_INCONSISTENT" in codes(assess(quote(received_at=NOW + timedelta(seconds=30))))
    tight = assess_quote(sc.EXPECTATION, [quote(last_timestamp=NOW - timedelta(seconds=20))], [MarketDataType.LIVE],
                         True, [], sc.SYNTH_CON_ID, now=NOW, policy=MockPolicy(max_quote_age_seconds=10),
                         request_id=1002)
    assert "QUOTE_STALE" in codes(tight)  # the window is read from the shared policy, never from this package


def test_delayed_frozen_unknown_and_entitlement_evidence_each_fail_closed():
    assert [f.kind for f in assess(quote(), types=[MarketDataType.DELAYED]) if f.code == "QUOTE_DELAYED"] == [FindingKind.DELAYED]
    assert "QUOTE_DELAYED" in codes(assess(quote(tick_types_received=[66, 67, 68, 88])))
    assert "QUOTE_DELAYED" in codes(assess(quote(market_data_type=MarketDataType.DELAYED_FROZEN), types=[]))
    frozen = assess(quote(market_data_type=MarketDataType.FROZEN), types=[MarketDataType.FROZEN])
    assert [(f.code, f.kind) for f in frozen if f.code == "QUOTE_FROZEN"] == [("QUOTE_FROZEN", FindingKind.STALE)]
    assert "MARKET_DATA_TYPE_UNKNOWN" in codes(assess(quote(market_data_type=MarketDataType.UNKNOWN), types=[]))
    assert "MARKET_DATA_TYPE_CHANGED" in codes(assess(quote(), types=[MarketDataType.LIVE, MarketDataType.DELAYED]))
    assert "MARKET_DATA_TYPE_INCONSISTENT" in codes(assess(quote(market_data_type=MarketDataType.DELAYED)))
    for code, name, kind in ((354, "ENTITLEMENT_UNAVAILABLE", FindingKind.UNENTITLED),
                             (10090, "ENTITLEMENT_PARTIAL", FindingKind.UNENTITLED),
                             (10197, "COMPETING_SESSION_NO_DATA", FindingKind.UNENTITLED),
                             (504, "NOT_CONNECTED", FindingKind.ERROR),
                             (9999, "BROKER_MESSAGE_UNCLASSIFIED", FindingKind.ERROR)):
        found = assess_quote(sc.EXPECTATION, [], [], True, [ApiMessage(code=code)], sc.SYNTH_CON_ID, now=NOW,
                             policy=MockPolicy(), request_id=1002)
        assert [(f.code, f.kind) for f in found] == [(name, kind)], code
    assert "QUOTE_OBSERVATION_MISSING" in codes(assess_quote(sc.EXPECTATION, [], [], True, [], sc.SYNTH_CON_ID, now=NOW,
                                                             policy=MockPolicy(), request_id=1002))
    assert "TICK_SNAPSHOT_END_MISSING" in codes(assess(quote(), end=False))
    assert "QUOTE_OBSERVATIONS_MULTIPLE" in codes(assess_quote(sc.EXPECTATION, [quote(), quote()], [MarketDataType.LIVE],
                                                               True, [], sc.SYNTH_CON_ID, now=NOW, policy=MockPolicy(),
                                                               request_id=1002))


def test_quote_integrity_and_identity_are_checked_without_using_the_price():
    assert "QUOTE_BID_ASK_INVALID" in codes(assess(quote(bid="100.05", ask="99.95")))
    assert "QUOTE_BID_ASK_INVALID" in codes(assess(quote(bid="0", ask="100.02")))
    assert "QUOTE_BID_ASK_INVALID" in codes(assess(quote(bid="ninety", ask="100.02")))
    assert "QUOTE_BID_ASK_MISSING" in codes(assess(quote(bid=None)))
    assert "QUOTE_SIZES_MISSING" in codes(assess(quote(ask_size=None)))
    assert "QUOTE_CON_ID_MISMATCH" in codes(assess(quote(con_id=900002)))
    assert "QUOTE_CON_ID_MISSING" in codes(assess(quote(con_id=None)))
    assert [(f.code, f.kind) for f in assess(quote(halted=1)) if f.code == "INSTRUMENT_HALTED"] == [
        ("INSTRUMENT_HALTED", FindingKind.OUT_OF_SCOPE)]
    assert "HALT_STATUS_CLEAR" in codes(assess(quote(halted=0)))
    for findings in (assess(quote(bid="100.05", ask="99.95")), assess(quote())):
        for f in findings:
            assert not re.search(r"\d+\.\d+", f.detail), f.detail  # no price ever reaches a diagnostic


# ----------------------------------------------------------------------------- account, positions, session

def test_account_summary_checks_identity_currency_and_completeness_without_rendering_values():
    rid = REQUEST_IDS["request_account_summary"]

    def account(rows, end=True, errors=()):
        return assess_account_summary(sc.EXPECTATION, rows, end, list(errors), request_id=rid)

    assert codes(account([sc.account_row()])) == ["ACCOUNT_SNAPSHOT_CONTROL"]
    assert codes(account([sc.account_row()], end=False)) == ["ACCOUNT_SUMMARY_END_MISSING"]
    assert codes(account([])) == ["ACCOUNT_SUMMARY_MISSING"]
    assert codes(account([sc.account_row(), sc.account_row()])) == ["ACCOUNT_SUMMARY_MULTIPLE"]
    other = account([sc.account_row(account_id=sc.OTHER_SYNTH_ACCOUNT)])
    assert codes(other) == ["ACCOUNT_ID_MISMATCH"]
    assert sc.SYNTH_ACCOUNT not in other[0].detail and sc.OTHER_SYNTH_ACCOUNT not in other[0].detail
    assert redact_account(sc.SYNTH_ACCOUNT) in other[0].detail
    assert codes(account([sc.account_row(currency="EUR")])) == ["ACCOUNT_CURRENCY_MISMATCH"]
    assert codes(account([sc.account_row(currency=None, account_type=None)])) == ["ACCOUNT_TYPE_MISSING",
                                                                                 "ACCOUNT_CURRENCY_MISSING"]
    partial = account([sc.account_row(values={"NetLiquidation": "1000000.00"})])
    assert codes(partial) == ["ACCOUNT_TAGS_MISSING"] and "TotalCashValue, BuyingPower" in partial[0].detail
    bad = account([AccountSummaryObservation(account_id=sc.SYNTH_ACCOUNT, account_type="SYNTHETIC-PAPER", currency="USD",
                                             values={"NetLiquidation": "lots", "TotalCashValue": "1.0",
                                                     "BuyingPower": "NaN"})])
    assert codes(bad) == ["ACCOUNT_VALUES_UNPARSEABLE"]
    assert "BuyingPower, NetLiquidation" in bad[0].detail and "lots" not in bad[0].detail
    assert codes(account([sc.account_row()], errors=[ApiMessage(code=1100)])) == ["CONNECTIVITY_LOST_MID_REQUEST"]


def test_account_summary_diagnostics_render_only_documented_tag_names():
    """Review follow-up (PR #19): an untrusted or sensitive key among accountSummary values must never reach a
    rendered diagnostic in JSON, full-text or summary form; documented tag names still may."""
    sentinel_bad, sentinel_ok = "SYNTH-PRIVATE1234", "SYNTH-PRIVATE5678"
    required = {"NetLiquidation": "1.00", "TotalCashValue": "1.00", "BuyingPower": "1.00"}

    def run(values):
        row = AccountSummaryObservation(account_id=sc.SYNTH_ACCOUNT, account_type="SYNTHETIC-PAPER", currency="USD",
                                        values=values)
        script = sc.CONTROL.model_copy(update={"account_summary": [
            sc.env("request_account_summary", 1, ResponseKind.ACCOUNT_SUMMARY, account_summary=row),
            sc.env("request_account_summary", 2, ResponseKind.ACCOUNT_SUMMARY_END)]})
        transport = FakeReadOnlyTransport(script, FakeClock(NOW))
        report = run_readiness_check(transport, sc.EXPECTATION, clock=transport.clock)
        rendered = render_json(report) + render_text(report) + render_text(report, summary=True)
        account = [f for f in report.findings if f.stage == Stage.ACCOUNT]
        return report, rendered, account

    leaky, rendered, account = run({**required, sentinel_bad: "not-a-decimal"})
    assert leaky.outcome == NOT_READY and codes(account) == ["ACCOUNT_TAGS_UNRECOGNIZED_WITHHELD", "ACCOUNT_VALUES_UNPARSEABLE"]
    assert sentinel_bad not in rendered and "not-a-decimal" not in rendered
    assert "1 unrecognised tag(s) (names withheld)" in account[1].detail
    withheld, rendered, account = run({**required, sentinel_ok: "1.00"})
    assert withheld.outcome == COMPLETE and codes(account) == ["ACCOUNT_TAGS_UNRECOGNIZED_WITHHELD", "ACCOUNT_SNAPSHOT_CONTROL"]
    assert sentinel_ok not in rendered and "1 tag name(s) outside the documented" in account[0].detail
    known, rendered, account = run({**required, "SMA": "lots", "Cushion": "NaN"})
    assert known.outcome == NOT_READY and codes(account) == ["ACCOUNT_VALUES_UNPARSEABLE"]
    assert "documented tags Cushion, SMA" in account[0].detail and "lots" not in rendered and "NaN" not in rendered
    mixed, rendered, account = run({**required, "SMA": "lots", sentinel_bad: "x"})
    assert mixed.outcome == NOT_READY and mixed.headline == FindingKind.MISMATCH
    assert account[-1].detail == "Values that are not finite decimals: documented tags SMA; 1 unrecognised tag(s) (names withheld)."
    assert sentinel_bad not in rendered
    clean, rendered, account = run({**required, "SMA": "5.00", "Leverage": "1.5"})
    assert clean.outcome == COMPLETE and codes(account) == ["ACCOUNT_SNAPSHOT_CONTROL"]
    assert "5.00" not in rendered and "1.5" not in rendered


def test_positions_snapshot_requires_completeness_and_the_expected_account():
    rid = REQUEST_IDS["request_positions"]
    line = PositionLine(account_id=sc.SYNTH_ACCOUNT, con_id=900003, symbol="ZZQ", sec_type="STK", currency="USD",
                        position="5", avg_cost="50.00")
    assert codes(assess_positions(sc.EXPECTATION, [line], True, [], request_id=rid)) == ["POSITIONS_SNAPSHOT_CONTROL"]
    assert codes(assess_positions(sc.EXPECTATION, [], True, [], request_id=rid)) == ["POSITIONS_SNAPSHOT_CONTROL"]
    assert codes(assess_positions(sc.EXPECTATION, [line], False, [], request_id=rid)) == ["POSITIONS_END_MISSING"]
    foreign = line.model_copy(update={"account_id": sc.OTHER_SYNTH_ACCOUNT})
    assert codes(assess_positions(sc.EXPECTATION, [line, foreign], True, [], request_id=rid)) == ["POSITION_ACCOUNT_MISMATCH"]
    broken = [line.model_copy(update={"con_id": None}), line.model_copy(update={"position": "five"})]
    found = assess_positions(sc.EXPECTATION, broken, True, [], request_id=rid)
    assert codes(found) == ["POSITION_LINES_INCOMPLETE"] and "2 position line" in found[0].detail
    assert codes(assess_positions(sc.EXPECTATION, [line], False, [ApiMessage(code=1100)], request_id=rid)) == [
        "CONNECTIVITY_LOST_MID_REQUEST", "POSITIONS_END_MISSING"]


def test_session_capability_findings_and_message_classification_fail_closed():
    assert codes(assess_session(sc.session(), now=NOW)) == ["SESSION_ATTESTATION_CONTROL"]
    assert codes(assess_session(sc.session(server_version=None), now=NOW)) == ["SERVER_VERSION_MISSING"]
    assert codes(assess_session(sc.session(connection_time=None), now=NOW)) == ["CONNECTION_TIME_MISSING"]
    assert codes(assess_session(sc.session(connection_time=NOW.replace(tzinfo=None)), now=NOW)) == ["CONNECTION_TIME_UNTIMED"]
    assert codes(assess_session(sc.session(connection_time=NOW + timedelta(minutes=1)), now=NOW)) == ["CONNECTION_TIME_INCONSISTENT"]
    quiet = assess_session(sc.session(messages=[]), now=NOW)
    assert codes(quiet) == ["DATA_FARM_STATUS_NOT_OBSERVED", "SESSION_ATTESTATION_CONTROL"]
    data_lost = assess_session(sc.session(messages=[ApiMessage(code=1101)]), now=NOW)
    assert (data_lost[0].code, data_lost[0].kind) == ("CONNECTIVITY_RESTORED_DATA_LOST", FindingKind.INCOMPLETE)
    broken = assess_session(sc.session(messages=[ApiMessage(code=2110)]), now=NOW)
    assert (broken[0].code, broken[0].kind) == ("SESSION_CONNECTIVITY_BROKEN", FindingKind.ERROR)
    unknown = classify_messages([ApiMessage(code=31337)], Stage.SESSION, None)
    assert [(f.code, f.kind) for f in unknown] == [("BROKER_MESSAGE_UNCLASSIFIED", FindingKind.ERROR)]
    assert classify_messages([ApiMessage(code=2104), ApiMessage(code=2158)], Stage.SESSION, None) == []
    assert set(REQUEST_CODES) >= {200, 354, 504, 1100, 10090, 10197}


# ----------------------------------------------------------------------------- the gateway stays authoritative

def test_gateway_decisions_and_shared_policy_are_untouched_by_readiness_checks():
    policy = MockPolicy()
    before = policy.model_dump()
    action = TradeAction(action_id="o", action=Action.OPEN, timing=Timing.CURRENT, instrument=sc.TLT_TARGET.instrument,
                         position_side=PositionSide.LONG, price_basis=PriceBasis.TRADED_CONTRACT,
                         source_market_symbol="TLT", entry_reference="100", stop_price="95", target_price="110",
                         explicit_instruction=True, evidence=["SYNTHETIC_S2_PAPER_01"])
    q = Quote(symbol="TLT", bid=Decimal("99.95"), ask=Decimal("100.05"), observed_at=NOW)
    gate_before = gate_action(action, NOW, q, policy, None, now=NOW)
    reports = [sc.run_scenario(s, policy=policy) for s in sc.SCENARIOS.values()]
    gate_after = gate_action(action, NOW, q, policy, None, now=NOW)
    assert gate_before == gate_after and (gate_after.status, gate_after.reasons) == ("APPROVE", ["PAPER_OPEN"])
    assert policy.model_dump() == before
    # A refused or not-ready readiness report changes nothing for the gateway, and a complete control adds nothing:
    # the gateway still blocks a stale quote exactly as before.
    stale = gate_action(action, NOW, q.model_copy(update={"observed_at": NOW - timedelta(seconds=31)}), policy, None, now=NOW)
    assert (stale.status, stale.reasons) == ("BLOCK", ["QUOTE_STALE_OR_FUTURE"])
    for report in reports:
        payload = render_json(report)
        assert report.trading_permission is False
        for word in AUTHORIZATION_VOCABULARY:
            assert word not in payload, (report.label, word)


# ----------------------------------------------------------------------------- CLI

def run_cli(*args):
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True, check=False,
                          env={**os.environ, "PYTHONPATH": str(SRC)})


def test_cli_lists_runs_and_renders_the_bundled_scenarios_offline(tmp_path):
    listed = run_cli("--list")
    assert listed.returncode == 0 and len(listed.stdout.splitlines()) == len(sc.SCENARIOS)
    assert "import origin" in listed.stderr and str(SRC / "tradingbot") in listed.stderr
    one = run_cli("--scenario", "tlt_regular_session_control", "--json")
    assert one.returncode == 0
    assert one.stdout == render_json(sc.run_scenario(control()))  # byte-identical across processes
    assert json.loads(one.stdout)["outcome"] == "CONTROL_COMPLETE"
    everything = run_cli("--all", "--json")
    assert everything.returncode == 0 and [r["label"] for r in json.loads(everything.stdout)] == list(sc.SCENARIOS)
    summary = run_cli("--all", "--summary")
    assert summary.returncode == 0 and len(summary.stdout.splitlines()) == len(sc.SCENARIOS)
    text = run_cli("--scenario", "quote_stale")
    assert text.returncode == 0 and "scenario expectation: NOT_READY -> actual NOT_READY (as scripted)" in text.stdout
    assert "trading_permission=False" in text.stdout
    out = tmp_path / "report.json"
    written = run_cli("--scenario", "environment_live", "--json", "--out", str(out))
    assert written.returncode == 0 and json.loads(out.read_text())["outcome"] == "REFUSED_BEFORE_DISPATCH"
    unknown = run_cli("--scenario", "no_such_scenario")
    assert unknown.returncode == 2 and "unknown scenario" in unknown.stderr
    assert run_cli().returncode == 2  # argparse: one of --list/--scenario/--all is required
