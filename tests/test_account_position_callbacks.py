"""Offline account-summary and position callback normalization. Invented values only; no SDK.

Covers the producer the readiness flow lacked: well-formed invented summary and position callbacks
reach the existing assessments' success through the unchanged FAKE flow. Foreign request ids and
accounts, non-synthetic accounts, malformed contracts and numbers, missing, duplicate and post-END
events, conflicting rows, overflow and repeated END cannot produce a successful completion.
"""
from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

from tradingbot.paper_readiness import account_position_callbacks as apc
from tradingbot.paper_readiness import scenarios as sc
from tradingbot.paper_readiness.account_position_callbacks import (
    AccountIssueCode,
    AccountSummaryCallbacks,
    PositionSnapshotCallbacks,
)
from tradingbot.paper_readiness.diagnostics import (
    ReadinessOutcome,
    Stage,
    assess_account_summary,
    assess_positions,
)
from tradingbot.paper_readiness.observations import ReadinessExpectation, ResponseKind
from tradingbot.paper_readiness.runner import (
    REQUEST_IDS,
    correlate,
    render_json,
    run_readiness_check,
)
from tradingbot.paper_readiness.transport import FORBIDDEN_NAME_RE, FakeClock, FakeReadOnlyTransport

ACCOUNT = "SYNTH-ACCTPOS01"      # invented for these tests
OTHER = "SYNTH-ACCTPOS02"
NON_SYNTHETIC = "CANARYREAL991"  # shaped like no real identifier; must never be stored
SRID = REQUEST_IDS["request_account_summary"]
PRID = REQUEST_IDS["request_positions"]  # a local snapshot id; positions carry no broker id
EXPECTATION = ReadinessExpectation(account_id=ACCOUNT, target=sc.TLT_TARGET)


def contract(**overrides) -> SimpleNamespace:
    base = {"conId": 700201, "symbol": "INVENTD", "secType": "STK", "currency": "USD"}
    base.update(overrides)
    return SimpleNamespace(**base)


def summary_rows(n: AccountSummaryCallbacks, *, account: str = ACCOUNT, rid: int = SRID) -> None:
    n.accountSummary(rid, account, "AccountType", "INVENTED-TYPE", "")
    n.accountSummary(rid, account, "NetLiquidation", "125000.50", "USD")
    n.accountSummary(rid, account, "TotalCashValue", "100000.00", "USD")
    n.accountSummary(rid, account, "BuyingPower", "250000.00", "USD")


def good_summary() -> AccountSummaryCallbacks:
    n = AccountSummaryCallbacks(SRID)
    summary_rows(n)
    n.accountSummaryEnd(SRID)
    return n


def good_positions() -> PositionSnapshotCallbacks:
    n = PositionSnapshotCallbacks(PRID)
    n.position(ACCOUNT, contract(), Decimal("-12.5"), 101.25)
    n.position(ACCOUNT, contract(conId=700202, symbol="INVENTE"), Decimal(300), 55.0)
    n.positionEnd()
    return n


def readiness(summary: AccountSummaryCallbacks, positions: PositionSnapshotCallbacks):
    session = sc.session(managed_accounts=[ACCOUNT])
    script = sc.control_script().model_copy(update={"session": session,
                                                    "account_summary": summary.envelopes(),
                                                    "positions": positions.envelopes()})
    clock = FakeClock(sc.CLOCK_START)
    return run_readiness_check(FakeReadOnlyTransport(script, clock), EXPECTATION, clock=clock)


def codes(report) -> list[str]:
    return [f.code for f in report.findings]


def summary_findings(n: AccountSummaryCallbacks) -> list[str]:
    c = correlate(n.envelopes(), request_id=SRID, stage=Stage.ACCOUNT,
                  end_kind=ResponseKind.ACCOUNT_SUMMARY_END, data_kinds=frozenset({ResponseKind.ACCOUNT_SUMMARY}))
    found = assess_account_summary(EXPECTATION, c.payloads(ResponseKind.ACCOUNT_SUMMARY), c.end_seen, c.errors,
                                   request_id=SRID)
    return [f.code for f in c.findings + found]


def position_findings(n: PositionSnapshotCallbacks) -> list[str]:
    c = correlate(n.envelopes(), request_id=PRID, stage=Stage.POSITIONS, end_kind=ResponseKind.POSITION_END,
                  data_kinds=frozenset({ResponseKind.POSITION}))
    found = assess_positions(EXPECTATION, c.payloads(ResponseKind.POSITION), c.end_seen, c.errors,
                             request_id=PRID)
    return [f.code for f in c.findings + found]


def issue_codes(n) -> list[AccountIssueCode]:
    return [i.code for i in n.issues]


# ----------------------------------------------------------------------------- the missing producer

def test_well_formed_summary_and_positions_complete_through_the_unchanged_fake_flow():
    summary, positions = good_summary(), good_positions()
    assert summary.completed and positions.completed and not summary.issues and not positions.issues
    row = next(e.account_summary for e in summary.envelopes() if e.kind == ResponseKind.ACCOUNT_SUMMARY)
    assert (row.account_id, row.account_type, row.currency) == (ACCOUNT, "INVENTED-TYPE", "USD")
    assert set(row.values) == {"NetLiquidation", "TotalCashValue", "BuyingPower"}
    report = readiness(summary, positions)
    assert report.outcome == ReadinessOutcome.CONTROL_COMPLETE
    assert {"ACCOUNT_SNAPSHOT_CONTROL", "POSITIONS_SNAPSHOT_CONTROL"} <= set(codes(report))
    assert "125000.50" not in render_json(report)  # values stay withheld from diagnostics


def test_fractional_and_negative_positions_are_kept_exactly():
    lines = [e.position for e in good_positions().envelopes() if e.kind == ResponseKind.POSITION]
    assert [(line.position, line.avg_cost) for line in lines] == [("-12.5", "101.25"), ("300", "55.0")]


def test_a_flat_book_is_one_end_marker():
    positions = PositionSnapshotCallbacks(PRID)
    positions.positionEnd()
    assert positions.completed
    assert readiness(good_summary(), positions).outcome == ReadinessOutcome.CONTROL_COMPLETE


def test_positions_carry_only_the_local_snapshot_id():
    assert {e.request_id for e in good_positions().envelopes()} == {PRID}


# ----------------------------------------------------------------------------- correlation and identity

def test_a_summary_row_for_another_request_id_is_uncorrelated():
    n = AccountSummaryCallbacks(SRID)
    summary_rows(n)
    n.accountSummary(SRID + 50, ACCOUNT, "NetLiquidation", "1.00", "USD")
    n.accountSummaryEnd(SRID)
    assert "UNCORRELATED_RESPONSE_IGNORED" in summary_findings(n)
    assert readiness(n, good_positions()).outcome == ReadinessOutcome.NOT_READY


def test_a_summary_end_for_another_request_id_does_not_complete():
    n = AccountSummaryCallbacks(SRID)
    summary_rows(n)
    n.accountSummaryEnd(SRID + 50)
    assert not n.completed and "ACCOUNT_SUMMARY_END_MISSING" in summary_findings(n)


def test_a_foreign_synthetic_account_keeps_the_existing_classifications():
    n = AccountSummaryCallbacks(SRID)
    summary_rows(n, account=OTHER)
    n.accountSummaryEnd(SRID)
    assert "ACCOUNT_ID_MISMATCH" in summary_findings(n)
    both = AccountSummaryCallbacks(SRID)
    summary_rows(both)
    summary_rows(both, account=OTHER)
    both.accountSummaryEnd(SRID)
    assert "ACCOUNT_SUMMARY_MULTIPLE" in summary_findings(both)
    p = PositionSnapshotCallbacks(PRID)
    p.position(OTHER, contract(), Decimal(1), 1.0)
    p.positionEnd()
    assert "POSITION_ACCOUNT_MISMATCH" in position_findings(p)


@pytest.mark.parametrize("stream", ["summary", "positions"])
def test_a_non_synthetic_account_is_refused_unstored_and_withholds_end(stream):
    if stream == "summary":
        n = AccountSummaryCallbacks(SRID)
        summary_rows(n)
        n.accountSummary(SRID, NON_SYNTHETIC, "NetLiquidation", "1.00", "USD")
        n.accountSummaryEnd(SRID)
    else:
        n = good_positions_with(lambda p: p.position(NON_SYNTHETIC, contract(), Decimal(1), 1.0))
    assert issue_codes(n) == [AccountIssueCode.ACCOUNT_NOT_SYNTHETIC] and not n.completed
    rendered = repr(n.envelopes()) + repr(n.issues)
    assert NON_SYNTHETIC not in rendered


def good_positions_with(extra) -> PositionSnapshotCallbacks:
    n = PositionSnapshotCallbacks(PRID)
    n.position(ACCOUNT, contract(), Decimal(1), 1.0)
    extra(n)
    n.positionEnd()
    return n


# ----------------------------------------------------------------------------- malformed evidence

@pytest.mark.parametrize("args", [
    ("1003", ACCOUNT, "NetLiquidation", "1.00", "USD"),
    (True, ACCOUNT, "NetLiquidation", "1.00", "USD"),
    (SRID, 7, "NetLiquidation", "1.00", "USD"),
    (SRID, ACCOUNT, "Net Liquidation", "1.00", "USD"),
    (SRID, ACCOUNT, "", "1.00", "USD"),
    (SRID, ACCOUNT, "NetLiquidation", 1.0, "USD"),
    (SRID, ACCOUNT, "NetLiquidation", "1" * 65, "USD"),
    (SRID, ACCOUNT, "NetLiquidation", "1.00\n", "USD"),
    (SRID, ACCOUNT, "NetLiquidation", "1.00", None),
], ids=["req-id-text", "req-id-bool", "account-int", "tag-space", "tag-empty", "value-float", "value-long",
        "value-control", "currency-none"])
def test_a_malformed_summary_row_latches_an_issue_and_withholds_end(args):
    n = AccountSummaryCallbacks(SRID)
    summary_rows(n)
    n.accountSummary(*args)
    n.accountSummaryEnd(SRID)
    assert issue_codes(n) == [AccountIssueCode.MALFORMED_CALLBACK] and not n.completed
    assert "ACCOUNT_SUMMARY_END_MISSING" in summary_findings(n)


def test_a_non_decimal_summary_value_is_carried_for_the_existing_check():
    n = AccountSummaryCallbacks(SRID)
    n.accountSummary(SRID, ACCOUNT, "AccountType", "INVENTED-TYPE", "")
    for tag in ("NetLiquidation", "TotalCashValue"):
        n.accountSummary(SRID, ACCOUNT, tag, "1.00", "USD")
    n.accountSummary(SRID, ACCOUNT, "BuyingPower", "NaN", "USD")
    n.accountSummaryEnd(SRID)
    assert n.completed and "ACCOUNT_VALUES_UNPARSEABLE" in summary_findings(n)


@pytest.mark.parametrize("call", [
    lambda p: p.position(ACCOUNT, SimpleNamespace(conId=1), Decimal(1), 1.0),
    lambda p: p.position(ACCOUNT, contract(conId="700201"), Decimal(1), 1.0),
    lambda p: p.position(ACCOUNT, contract(conId=-1), Decimal(1), 1.0),
    lambda p: p.position(ACCOUNT, contract(symbol=7), Decimal(1), 1.0),
    lambda p: p.position(ACCOUNT, None, Decimal(1), 1.0),
    lambda p: p.position(ACCOUNT, contract(conId=700299), 1.5, 1.0),
    lambda p: p.position(ACCOUNT, contract(conId=700299), True, 1.0),
    lambda p: p.position(ACCOUNT, contract(conId=700299), Decimal("NaN"), 1.0),
    lambda p: p.position(ACCOUNT, contract(conId=700299), "1", 1.0),
    lambda p: p.position(ACCOUNT, contract(conId=700299), Decimal(1), float("inf")),
    lambda p: p.position(ACCOUNT, contract(conId=700299), Decimal(1), "1.0"),
    lambda p: p.position(ACCOUNT, contract(conId=700299), Decimal(1), False),
], ids=["contract-missing-fields", "con-id-text", "con-id-negative", "symbol-int", "contract-none",
        "pos-float", "pos-bool", "pos-nan", "pos-text", "cost-infinite", "cost-text", "cost-bool"])
def test_a_malformed_position_latches_an_issue_and_withholds_end(call):
    n = good_positions_with(call)
    assert issue_codes(n) == [AccountIssueCode.MALFORMED_CALLBACK] and not n.completed
    assert "POSITIONS_END_MISSING" in position_findings(n)


# ----------------------------------------------------------------------------- duplicates, conflicts, END

def test_a_duplicate_summary_tag_withholds_end_even_with_the_same_value():
    n = AccountSummaryCallbacks(SRID)
    summary_rows(n)
    n.accountSummary(SRID, ACCOUNT, "NetLiquidation", "125000.50", "USD")
    n.accountSummaryEnd(SRID)
    assert issue_codes(n) == [AccountIssueCode.DUPLICATE_ROW] and not n.completed


def test_a_duplicate_position_withholds_end():
    n = good_positions_with(lambda p: p.position(ACCOUNT, contract(), Decimal(1), 1.0))
    assert issue_codes(n) == [AccountIssueCode.DUPLICATE_ROW] and not n.completed


def test_conflicting_summary_currencies_withhold_end():
    n = AccountSummaryCallbacks(SRID)
    summary_rows(n)
    n.accountSummary(SRID, ACCOUNT, "SettledCash", "1.00", "EUR")
    n.accountSummaryEnd(SRID)
    assert issue_codes(n) == [AccountIssueCode.CONFLICTING_CURRENCY] and not n.completed


@pytest.mark.parametrize("missing", [("NetLiquidation",), ("TotalCashValue",), ("BuyingPower",),
                                     ("TotalCashValue", "BuyingPower")],
                         ids=["net-liquidation", "total-cash", "buying-power", "two-of-three"])
def test_a_required_monetary_row_without_currency_cannot_pass(missing):
    """Review P2: one row's currency does not establish the currency of the others."""
    n = AccountSummaryCallbacks(SRID)
    n.accountSummary(SRID, ACCOUNT, "AccountType", "INVENTED-TYPE", "")
    for tag in ("NetLiquidation", "TotalCashValue", "BuyingPower"):
        n.accountSummary(SRID, ACCOUNT, tag, "1.00", "" if tag in missing else "USD")
    n.accountSummaryEnd(SRID)
    row = next(e.account_summary for e in n.envelopes() if e.kind == ResponseKind.ACCOUNT_SUMMARY)
    assert row.currency is None
    assert "ACCOUNT_CURRENCY_MISSING" in summary_findings(n)
    report = readiness(n, good_positions())
    assert report.outcome == ReadinessOutcome.NOT_READY and "ACCOUNT_SNAPSHOT_CONTROL" not in codes(report)


def test_currency_less_nonmonetary_rows_still_complete():
    n = AccountSummaryCallbacks(SRID)
    summary_rows(n)  # AccountType carries no currency
    n.accountSummary(SRID, ACCOUNT, "Cushion", "0.85", "")
    n.accountSummaryEnd(SRID)
    assert summary_findings(n)[-1] == "ACCOUNT_SNAPSHOT_CONTROL"
    assert readiness(n, good_positions()).outcome == ReadinessOutcome.CONTROL_COMPLETE


def test_a_missing_end_never_completes():
    n = AccountSummaryCallbacks(SRID)
    summary_rows(n)
    p = PositionSnapshotCallbacks(PRID)
    p.position(ACCOUNT, contract(), Decimal(1), 1.0)
    assert not n.completed and not p.completed
    report = readiness(n, p)
    assert {"ACCOUNT_SUMMARY_END_MISSING", "POSITIONS_END_MISSING"} <= set(codes(report))
    assert report.outcome == ReadinessOutcome.NOT_READY


def test_rows_after_end_are_after_end_not_merged():
    n = good_summary()
    n.accountSummary(SRID, ACCOUNT, "SettledCash", "1.00", "USD")
    assert "RESPONSE_AFTER_END_IGNORED" in summary_findings(n)
    p = good_positions()
    p.position(ACCOUNT, contract(conId=700203), Decimal(1), 1.0)
    p.positionEnd()
    assert "RESPONSE_AFTER_END_IGNORED" in position_findings(p)


@pytest.mark.parametrize("stream", ["summary", "positions"])
def test_an_invalid_stream_cannot_recover_through_a_repeated_end(stream):
    if stream == "summary":
        n = AccountSummaryCallbacks(SRID)
        summary_rows(n)
        n.unsupported_callback("error")
        n.accountSummaryEnd(SRID)
        n.accountSummaryEnd(SRID)
        end = ResponseKind.ACCOUNT_SUMMARY_END
    else:
        n = PositionSnapshotCallbacks(PRID)
        n.position(ACCOUNT, contract(), Decimal(1), 1.0)
        n.unsupported_callback("error")
        n.positionEnd()
        n.positionEnd()
        end = ResponseKind.POSITION_END
    assert not n.completed and end not in [e.kind for e in n.envelopes()]
    report = readiness(n, good_positions()) if stream == "summary" else readiness(good_summary(), n)
    assert report.outcome == ReadinessOutcome.NOT_READY


# ----------------------------------------------------------------------------- bounds and surface

def test_the_row_bound_withholds_end():
    n = PositionSnapshotCallbacks(PRID)
    for i in range(apc.MAX_POSITION_ROWS + 1):
        n.position(ACCOUNT, contract(conId=800000 + i), Decimal(1), 1.0)
    n.positionEnd()
    assert issue_codes(n) == [AccountIssueCode.ROW_LIMIT_EXCEEDED] and not n.completed


def test_the_callback_bound_counts_each_callback_once_and_overflow_is_incomplete():
    n = PositionSnapshotCallbacks(PRID)
    n.positionEnd()
    for i in range(apc.MAX_CALLBACKS - 1):  # rows after END are records, not snapshot rows
        n.position(ACCOUNT, contract(conId=900000 + i), Decimal(1), 1.0)
    assert n.ignored_callbacks == 0 and n.issues == ()  # exactly the bound, counted once each
    n.position(ACCOUNT, contract(conId=999999), Decimal(1), 1.0)
    assert n.ignored_callbacks == 1 and issue_codes(n) == [AccountIssueCode.CALLBACK_LIMIT_EXCEEDED]
    assert not n.completed


def test_the_surface_has_only_documented_callbacks_and_no_forbidden_names():
    for cls, callbacks in ((AccountSummaryCallbacks, {"accountSummary", "accountSummaryEnd"}),
                           (PositionSnapshotCallbacks, {"position", "positionEnd"})):
        public = {name for name in dir(cls) if not name.startswith("_")}
        assert public == callbacks | {"unsupported_callback", "envelopes", "completed", "issues",
                                      "issue_overflow", "ignored_callbacks"}
        for name in public | set(apc.__all__):
            assert not FORBIDDEN_NAME_RE.search(name), name


def test_constructors_refuse_an_invalid_id():
    for cls in (AccountSummaryCallbacks, PositionSnapshotCallbacks):
        for bad in (-1, True, "1004"):
            with pytest.raises(ValueError):
                cls(bad)
