"""IBKR accountSummary/position callback normalization, SDK-free. Synthetic only; NON-AUTHORITATIVE.

Every account, contract, value and event below is invented. Each test names the acceptance
requirement it covers: complete snapshots feed the existing assessments; wrong request or account,
conflicting duplicates, malformed or non-finite decimals, a missing END, events after END and
repeated END never reach success; the callback bound; synthetic-only accounts; the local positions
correlation; safe diagnostics; and an SDK-free, dispatch-free surface.
"""
from __future__ import annotations

import ast
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

import tradingbot
from tradingbot.paper_readiness import ibkr_account_callbacks as cb
from tradingbot.paper_readiness import scenarios as sc
from tradingbot.paper_readiness.diagnostics import (
    FindingKind,
    ReadinessOutcome,
    Stage,
    assess_account_summary,
    assess_positions,
)
from tradingbot.paper_readiness.observations import ResponseKind
from tradingbot.paper_readiness.runner import REQUEST_IDS, correlate, run_readiness_check
from tradingbot.paper_readiness.transport import FORBIDDEN_NAME_RE, FakeClock, FakeReadOnlyTransport

ACCOUNT_RID = REQUEST_IDS["request_account_summary"]
POSITIONS_RID = REQUEST_IDS["request_positions"]
FOREIGN_RID = 9999
SYNTH, OTHER = sc.SYNTH_ACCOUNT, sc.OTHER_SYNTH_ACCOUNT
NOT_SYNTHETIC = "NOT-SYNTHETIC-0001"
USD_ROWS = (
    ("AccountType", "SYNTHETIC-PAPER", ""),
    ("NetLiquidation", "1000000.00", "USD"),
    ("TotalCashValue", "1000000.00", "USD"),
    ("BuyingPower", "4000000.00", "USD"),
)


def contract(con_id: object = sc.SYNTH_CON_ID, **overrides) -> SimpleNamespace:
    fields = {"conId": con_id, "symbol": "TLT", "secType": "STK", "currency": "USD", **overrides}
    return SimpleNamespace(**fields)


LINE = (SYNTH, contract(), Decimal(100), 95.5)


def summary(rows=USD_ROWS, *, end: bool = True, request_id: int = ACCOUNT_RID,
            account: str = SYNTH) -> cb.AccountSummaryCollector:
    collector = cb.AccountSummaryCollector(ACCOUNT_RID)
    for tag, value, currency in rows:
        collector.accountSummary(request_id, account, tag, value, currency)
    if end:
        collector.accountSummaryEnd(ACCOUNT_RID)
    return collector


def positions(lines=(LINE,), *, end: bool = True) -> cb.PositionSnapshotCollector:
    collector = cb.PositionSnapshotCollector(POSITIONS_RID)
    for line in lines:
        collector.position(*line)
    if end:
        collector.positionEnd()
    return collector


def judge_account(snapshot: cb.CallbackSnapshot) -> list:
    """Exactly what the runner does with the account-summary stream."""
    result = correlate(list(snapshot.envelopes), request_id=ACCOUNT_RID, stage=Stage.ACCOUNT,
                       end_kind=ResponseKind.ACCOUNT_SUMMARY_END,
                       data_kinds=frozenset({ResponseKind.ACCOUNT_SUMMARY}))
    return result.findings + assess_account_summary(
        sc.EXPECTATION, result.payloads(ResponseKind.ACCOUNT_SUMMARY), result.end_seen,
        result.errors, request_id=ACCOUNT_RID)


def judge_positions(snapshot: cb.CallbackSnapshot) -> list:
    """Exactly what the runner does with the positions stream."""
    result = correlate(list(snapshot.envelopes), request_id=POSITIONS_RID, stage=Stage.POSITIONS,
                       end_kind=ResponseKind.POSITION_END,
                       data_kinds=frozenset({ResponseKind.POSITION}))
    return result.findings + assess_positions(
        sc.EXPECTATION, result.payloads(ResponseKind.POSITION), result.end_seen, result.errors,
        request_id=POSITIONS_RID)


def problems(findings) -> list[str]:
    return sorted(f.code for f in findings if f.kind != FindingKind.INFO)


def codes(snapshot: cb.CallbackSnapshot) -> list[str]:
    return [f.code for f in snapshot.findings]


def kinds(snapshot: cb.CallbackSnapshot) -> list[ResponseKind]:
    return [e.kind for e in snapshot.envelopes]


def run(account: cb.CallbackSnapshot, held: cb.CallbackSnapshot):
    clock = FakeClock(sc.CLOCK_START)
    script = sc.control_script().model_copy(update={"account_summary": list(account.envelopes),
                                                    "positions": list(held.envelopes)})
    return run_readiness_check(FakeReadOnlyTransport(script, clock), sc.EXPECTATION, clock=clock)


def assert_unsupported(snapshot: cb.CallbackSnapshot, code: str) -> None:
    assert snapshot.status is cb.SnapshotStatus.UNSUPPORTED
    assert snapshot.envelopes == ()
    assert code in codes(snapshot) and "CALLBACK_SNAPSHOT_UNSUPPORTED" in codes(snapshot)


# Requirement: a correct complete USD summary and position or empty-position snapshots feed the
# existing assessments unchanged.


def test_complete_usd_account_summary_feeds_the_existing_assessment():
    snapshot = summary().snapshot()
    assert snapshot.status is cb.SnapshotStatus.END_RECEIVED and snapshot.findings == ()
    assert kinds(snapshot) == [ResponseKind.ACCOUNT_SUMMARY, ResponseKind.ACCOUNT_SUMMARY_END]
    observation = snapshot.envelopes[0].account_summary
    assert (observation.account_type, observation.currency) == ("SYNTHETIC-PAPER", "USD")
    assert observation.values == {"NetLiquidation": "1000000.00", "TotalCashValue": "1000000.00",
                                  "BuyingPower": "4000000.00"}
    findings = judge_account(snapshot)
    assert problems(findings) == [] and "ACCOUNT_SNAPSHOT_CONTROL" in [f.code for f in findings]


@pytest.mark.parametrize("lines", [(LINE,), ()], ids=["one-position", "empty-book"])
def test_position_and_empty_position_snapshots_feed_the_existing_assessment(lines):
    snapshot = positions(lines).snapshot()
    assert snapshot.status is cb.SnapshotStatus.END_RECEIVED and snapshot.findings == ()
    assert kinds(snapshot) == [ResponseKind.POSITION] * len(lines) + [ResponseKind.POSITION_END]
    if lines:
        line = snapshot.envelopes[0].position
        assert (line.account_id, line.con_id, line.position, line.avg_cost) == (
            SYNTH, sc.SYNTH_CON_ID, "100", "95.5")
    findings = judge_positions(snapshot)
    assert problems(findings) == [] and "POSITIONS_SNAPSHOT_CONTROL" in [f.code for f in findings]


def test_normalized_snapshots_complete_the_existing_offline_readiness_run():
    report = run(summary().snapshot(), positions().snapshot())
    assert report.outcome is ReadinessOutcome.CONTROL_COMPLETE
    assert report.trading_permission is False


# Requirement: wrong request or account never reaches success, and nothing is relabelled or dropped.


def _two_accounts() -> cb.AccountSummaryCollector:
    collector = summary(end=False)
    for tag, value, currency in USD_ROWS:
        collector.accountSummary(ACCOUNT_RID, OTHER, tag, value, currency)
    collector.accountSummaryEnd(ACCOUNT_RID)
    return collector


@pytest.mark.parametrize(
    "build, judge, expected",
    [
        (lambda: summary(request_id=FOREIGN_RID), judge_account,
         {"UNCORRELATED_RESPONSE_IGNORED", "ACCOUNT_SUMMARY_MISSING"}),
        (lambda: summary(account=OTHER), judge_account, {"ACCOUNT_ID_MISMATCH"}),
        (_two_accounts, judge_account, {"ACCOUNT_SUMMARY_MULTIPLE"}),
        (lambda: positions(((OTHER, contract(), Decimal(1), 1.0),)), judge_positions,
         {"POSITION_ACCOUNT_MISMATCH"}),
    ],
    ids=["foreign-request-id", "other-account", "two-accounts", "position-other-account"],
)
def test_wrong_request_or_account_remains_non_success(build, judge, expected):
    snapshot = build().snapshot()
    assert snapshot.status is cb.SnapshotStatus.END_RECEIVED
    assert expected <= set(problems(judge(snapshot)))


def test_a_foreign_request_id_keeps_its_own_id_and_a_foreign_end_ends_nothing():
    collector = summary(request_id=FOREIGN_RID, end=False)
    collector.accountSummaryEnd(FOREIGN_RID)
    snapshot = collector.snapshot()
    assert snapshot.status is cb.SnapshotStatus.END_NOT_RECEIVED
    assert {e.request_id for e in snapshot.envelopes} == {FOREIGN_RID}
    assert {"UNCORRELATED_RESPONSE_IGNORED", "ACCOUNT_SUMMARY_END_MISSING"} <= set(
        problems(judge_account(snapshot)))


# Requirement: conflicting duplicates are explicitly unsupported and never silently overwritten;
# exact duplicates are reported, not double counted.


@pytest.mark.parametrize(
    "build, code",
    [
        (lambda: summary(USD_ROWS + (("NetLiquidation", "999.00", "USD"),)),
         "CALLBACK_TAG_CONFLICT"),
        (lambda: summary(USD_ROWS + (("NetLiquidation", "1000000.00", "EUR"),)),
         "CALLBACK_TAG_CONFLICT"),
        (lambda: summary(USD_ROWS + (("AccountType", "SYNTHETIC-OTHER", ""),)),
         "CALLBACK_TAG_CONFLICT"),
        (lambda: summary(USD_ROWS + (("SettledCash", "1.00", "EUR"),)),
         "CALLBACK_CURRENCY_CONFLICT"),
        (lambda: positions((LINE, (SYNTH, contract(), Decimal(101), 95.5))),
         "CALLBACK_POSITION_CONFLICT"),
    ],
    ids=["tag-value", "tag-currency", "account-type", "cross-tag-currency", "position-identity"],
)
def test_conflicting_duplicates_are_unsupported(build, code):
    assert_unsupported(build().snapshot(), code)


def test_exact_duplicates_are_dropped_once_and_reported():
    account = summary(USD_ROWS + (USD_ROWS[1],)).snapshot()
    held = positions((LINE, LINE)).snapshot()
    for snapshot in (account, held):
        assert snapshot.status is cb.SnapshotStatus.END_RECEIVED
        assert codes(snapshot) == ["CALLBACK_DUPLICATE_DROPPED"]
    assert len(account.envelopes[0].account_summary.values) == 3
    assert kinds(held) == [ResponseKind.POSITION, ResponseKind.POSITION_END]
    assert problems(judge_account(account)) == [] and problems(judge_positions(held)) == []


# Requirement: malformed or non-finite decimals never reach success. Values the assessments judge
# are passed verbatim to them; what no assessment reads, or what has another type, is unsupported.


@pytest.mark.parametrize("value", ["not-a-number", "NaN", "Infinity"])
def test_malformed_account_values_are_kept_verbatim_and_judged_unparseable(value):
    rows = USD_ROWS[:1] + (("NetLiquidation", value, "USD"),) + USD_ROWS[2:]
    snapshot = summary(rows).snapshot()
    assert snapshot.envelopes[0].account_summary.values["NetLiquidation"] == value
    assert "ACCOUNT_VALUES_UNPARSEABLE" in problems(judge_account(snapshot))


def test_a_non_finite_position_is_kept_verbatim_and_judged_incomplete():
    snapshot = positions(((SYNTH, contract(), Decimal("NaN"), 95.5),)).snapshot()
    assert snapshot.envelopes[0].position.position == "NaN"
    assert "POSITION_LINES_INCOMPLETE" in problems(judge_positions(snapshot))


@pytest.mark.parametrize(
    "line, code",
    [
        ((SYNTH, contract(), Decimal(1), float("nan")), "CALLBACK_AVERAGE_COST_NOT_FINITE"),
        ((SYNTH, contract(), Decimal(1), float("inf")), "CALLBACK_AVERAGE_COST_NOT_FINITE"),
        ((SYNTH, contract(), Decimal(1), Decimal("Infinity")),
         "CALLBACK_AVERAGE_COST_NOT_FINITE"),
        ((SYNTH, contract(), 1.0, 95.5), "CALLBACK_ARGUMENT_UNSUPPORTED"),
        ((SYNTH, contract(), Decimal(1), "95.5"), "CALLBACK_ARGUMENT_UNSUPPORTED"),
        ((SYNTH, contract(0), Decimal(1), 95.5), "CALLBACK_CONTRACT_ID_UNSUPPORTED"),
        ((SYNTH, contract(True), Decimal(1), 95.5), "CALLBACK_CONTRACT_ID_UNSUPPORTED"),
        ((SYNTH, SimpleNamespace(conId=1, symbol="TLT"), Decimal(1), 95.5),
         "CALLBACK_ARGUMENT_UNSUPPORTED"),
    ],
    ids=["cost-nan", "cost-inf", "cost-decimal-inf", "float-position", "text-cost", "con-id-zero",
         "con-id-bool", "contract-missing-fields"],
)
def test_unrepresentable_position_arguments_are_unsupported(line, code):
    assert_unsupported(positions((line,)).snapshot(), code)


@pytest.mark.parametrize(
    "call",
    [
        lambda r: r.accountSummary(True, SYNTH, "NetLiquidation", "1", "USD"),
        lambda r: r.accountSummary(ACCOUNT_RID, SYNTH, "NetLiquidation", 1, "USD"),
        lambda r: r.accountSummary(ACCOUNT_RID, SYNTH, "NetLiquidation", "1" * 257, "USD"),
        lambda r: r.accountSummaryEnd(-1),
    ],
    ids=["bool-request-id", "number-value", "oversized-text", "negative-end-id"],
)
def test_unrepresentable_summary_arguments_are_unsupported(call):
    collector = summary(end=False)
    call(collector)
    collector.accountSummaryEnd(ACCOUNT_RID)
    assert_unsupported(collector.snapshot(), "CALLBACK_ARGUMENT_UNSUPPORTED")


# Requirement: a missing END is never fabricated, and events after END stay after it.


@pytest.mark.parametrize(
    "build, judge, code",
    [
        (lambda: summary(end=False), judge_account, "ACCOUNT_SUMMARY_END_MISSING"),
        (lambda: positions(end=False), judge_positions, "POSITIONS_END_MISSING"),
        (lambda: positions((), end=False), judge_positions, "POSITIONS_END_MISSING"),
    ],
    ids=["summary", "positions", "empty-positions"],
)
def test_a_missing_end_is_never_fabricated(build, judge, code):
    snapshot = build().snapshot()
    assert snapshot.status is cb.SnapshotStatus.END_NOT_RECEIVED
    assert not [k for k in kinds(snapshot) if k in (ResponseKind.ACCOUNT_SUMMARY_END,
                                                     ResponseKind.POSITION_END)]
    assert code in problems(judge(snapshot))


def test_events_after_end_stay_after_the_end_marker():
    account = summary()
    account.accountSummary(ACCOUNT_RID, SYNTH, "SettledCash", "1.00", "USD")
    held = positions()
    held.position(SYNTH, contract(), Decimal(200), 95.5)
    account_snapshot, held_snapshot = account.snapshot(), held.snapshot()
    assert kinds(account_snapshot) == [ResponseKind.ACCOUNT_SUMMARY,
                                       ResponseKind.ACCOUNT_SUMMARY_END,
                                       ResponseKind.ACCOUNT_SUMMARY]
    assert "SettledCash" not in account_snapshot.envelopes[0].account_summary.values
    assert kinds(held_snapshot) == [ResponseKind.POSITION, ResponseKind.POSITION_END,
                                    ResponseKind.POSITION]
    assert "RESPONSE_AFTER_END_IGNORED" in problems(judge_account(account_snapshot))
    assert "RESPONSE_AFTER_END_IGNORED" in problems(judge_positions(held_snapshot))


# Requirement (regression from a sibling adapter's review): a repeated END never turns a stream with
# recorded issues, overflow or foreign data into a complete one.


def test_a_repeated_end_never_completes_a_failed_or_incomplete_stream():
    clean = summary()
    clean.accountSummaryEnd(ACCOUNT_RID)
    repeated = positions()
    repeated.positionEnd()
    assert "RESPONSE_AFTER_END_IGNORED" in problems(judge_account(clean.snapshot()))
    assert "RESPONSE_AFTER_END_IGNORED" in problems(judge_positions(repeated.snapshot()))
    assert run(clean.snapshot(), positions().snapshot()).outcome is ReadinessOutcome.NOT_READY
    assert run(summary().snapshot(), repeated.snapshot()).outcome is ReadinessOutcome.NOT_READY

    refused = summary(account=NOT_SYNTHETIC)
    refused.accountSummaryEnd(ACCOUNT_RID)
    overflowing = positions([(SYNTH, contract(i), Decimal(1), 1.0) for i in range(1, 65)])
    overflowing.positionEnd()
    assert_unsupported(refused.snapshot(), "CALLBACK_ACCOUNT_NOT_SYNTHETIC")
    assert_unsupported(overflowing.snapshot(), "CALLBACK_LIMIT_EXCEEDED")

    foreign = summary(request_id=FOREIGN_RID)
    foreign.accountSummaryEnd(ACCOUNT_RID)
    assert run(foreign.snapshot(), positions().snapshot()).outcome is ReadinessOutcome.NOT_READY


# Requirement: the existing finite response bound holds for callbacks.


def test_callbacks_are_bounded_by_the_existing_response_limit():
    limit = cb.MAX_CALLBACKS_PER_SNAPSHOT
    rows = [(f"SyntheticTag{i:03d}", "1", "USD") for i in range(limit - 1)]
    assert summary(rows).snapshot().status is cb.SnapshotStatus.END_RECEIVED
    assert_unsupported(summary(rows + [("SyntheticTagLast", "1", "USD")]).snapshot(),
                       "CALLBACK_LIMIT_EXCEEDED")
    lines = [(SYNTH, contract(i), Decimal(1), 1.0) for i in range(1, limit)]
    assert positions(lines).snapshot().status is cb.SnapshotStatus.END_RECEIVED
    assert_unsupported(positions(lines + [(SYNTH, contract(limit), Decimal(1), 1.0)]).snapshot(),
                       "CALLBACK_LIMIT_EXCEEDED")


# Requirement: synthetic-only accounts; a non-synthetic identifier is refused and never retained.


def test_a_non_synthetic_account_is_refused_and_not_retained():
    account = summary(account=NOT_SYNTHETIC)
    held = positions(((NOT_SYNTHETIC, contract(), Decimal(1), 1.0),))
    for collector in (account, held):
        snapshot = collector.snapshot()
        assert_unsupported(snapshot, "CALLBACK_ACCOUNT_NOT_SYNTHETIC")
        assert NOT_SYNTHETIC not in repr(snapshot) + repr(vars(collector))


# Requirement: positions carry no request id, so correlation is local to one active snapshot.


def test_positions_use_only_a_local_single_snapshot_correlation():
    held, account = positions().snapshot(), summary().snapshot()
    assert held.correlation is cb.Correlation.LOCAL_SINGLE_ACTIVE_SNAPSHOT
    assert {e.request_id for e in held.envelopes} == {POSITIONS_RID}
    assert account.correlation is cb.Correlation.BROKER_REQUEST_ID
    for bad in (-1, True, "1004"):
        with pytest.raises(ValueError):
            cb.PositionSnapshotCollector(bad)


# Requirement: diagnostics never render an account identifier, a value or an undocumented tag name.


def test_adapter_findings_render_no_account_value_or_undocumented_tag():
    secret_tag, secret_value = "SyntheticWithheldTag", "123456.78"
    snapshots = [
        summary(USD_ROWS + ((secret_tag, secret_value, "USD"),
                            (secret_tag, "1.00", "USD"))).snapshot(),
        summary(USD_ROWS + (("NetLiquidation", secret_value, "USD"),)).snapshot(),
        summary(USD_ROWS + (USD_ROWS[1],)).snapshot(),
        positions((LINE, (SYNTH, contract(), Decimal(secret_value), 95.5))).snapshot(),
        summary(account=NOT_SYNTHETIC).snapshot(),
    ]
    rendered = " ".join(f.detail for snapshot in snapshots for f in snapshot.findings)
    assert "names withheld" in rendered and "NetLiquidation" in rendered
    for secret in (secret_tag, secret_value, "1000000.00", SYNTH, OTHER, NOT_SYNTHETIC):
        assert secret not in rendered


# Requirement: an SDK-free injected adapter with no dispatch, order, mutation or credential surface.


def test_module_is_sdk_free_and_exposes_only_callbacks_and_snapshot():
    module_path = Path(cb.__file__)
    assert module_path.resolve().is_relative_to(Path(tradingbot.__file__).resolve().parent)
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    imported = {alias.name.split(".")[0] for node in ast.walk(tree)
                if isinstance(node, ast.Import) for alias in node.names}
    imported |= {node.module.split(".")[0] for node in ast.walk(tree)
                 if isinstance(node, ast.ImportFrom) and node.module}
    assert imported == {"__future__", "dataclasses", "decimal", "enum", "tradingbot"}
    surface = {"AccountSummaryCollector": {"accountSummary", "accountSummaryEnd", "snapshot"},
               "PositionSnapshotCollector": {"position", "positionEnd", "snapshot"}}
    for name, methods in surface.items():
        public = {n for n in dir(getattr(cb, name)) if not n.startswith("_")}
        assert public == methods | {"correlation"}
    names = set(cb.__all__) | set().union(*surface.values())
    assert not [n for n in names if FORBIDDEN_NAME_RE.search(n)]
