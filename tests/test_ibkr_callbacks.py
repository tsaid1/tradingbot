"""IBKR callback normalization into the existing readiness envelopes. Invented callback values only.

Each test names the requirement it covers: the positive contract + live snapshot control, determinism,
ambiguous or conflicting identity, wrong request id, missing END, malformed or unsupported tick payload,
delayed or frozen data, missing or stale timestamps, preservation until explicit completion, bounded
unsupported callbacks, and no inference of environment or account status. No SDK is imported.
"""
from __future__ import annotations

import ast
import json
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from tradingbot.models import MockPolicy
from tradingbot.paper_readiness import ibkr_callbacks as ic
from tradingbot.paper_readiness import scenarios as sc
from tradingbot.paper_readiness.diagnostics import ReadinessOutcome, Stage, assess_quote
from tradingbot.paper_readiness.ibkr_callbacks import (
    CallbackRequest,
    IbkrCallbackNormalizer,
    IssueCode,
)
from tradingbot.paper_readiness.observations import ResponseKind
from tradingbot.paper_readiness.runner import (
    REQUEST_IDS,
    correlate,
    render_json,
    run_readiness_check,
)
from tradingbot.paper_readiness.transport import (
    FORBIDDEN_NAME_RE,
    FakeClock,
    FakeReadOnlyTransport,
)

NOW = sc.CLOCK_START
CRID = REQUEST_IDS["request_contract_details"]
QRID = REQUEST_IDS["request_quote_snapshot"]
CON_ID = sc.SYNTH_CON_ID
LAST_TRADE = str(int((NOW - timedelta(seconds=10)).timestamp()))
ATTRIB = SimpleNamespace(canAutoExecute=False, pastLimit=False, preOpen=False)
CANARY = "CANARY-TEXT-NEVER-RETAINED"
CONTROL_CODES = {"SESSION_ATTESTATION_CONTROL", "CONTRACT_IDENTITY_CONTROL", "REGULAR_SESSION_CONTROL",
                 "QUOTE_LIVE_CONTROL", "ACCOUNT_SNAPSHOT_CONTROL", "POSITIONS_SNAPSHOT_CONTROL"}


# ----------------------------------------------------------------------------- invented SDK-shaped values

def details(**overrides) -> SimpleNamespace:
    contract = {"conId": CON_ID, "symbol": "TLT", "localSymbol": "TLT", "secType": "STK", "currency": "USD",
                "exchange": "SMART", "primaryExchange": "SYNTH-LISTING"}
    rest = {"longName": "SYNTHETIC TLT PLACEHOLDER",
            "tradingHours": "20261106:0400-20261106:2000;20261109:0400-20261109:2000",
            "liquidHours": sc.LIQUID_HOURS, "timeZoneId": sc.TIME_ZONE, "minTick": 0.01}
    for key, value in overrides.items():
        (contract if key in contract else rest)[key] = value
    return SimpleNamespace(contract=SimpleNamespace(**contract), **rest)


def contract_normalizer(*rows: SimpleNamespace, end: bool = True, req_id: int = CRID) -> IbkrCallbackNormalizer:
    n = IbkrCallbackNormalizer(CRID, CallbackRequest.CONTRACT_DETAILS)
    for row in rows or (details(),):
        n.contractDetails(req_id, row)
    if end:
        n.contractDetailsEnd(req_id)
    return n


def quote_normalizer(clock: FakeClock | None = None) -> IbkrCallbackNormalizer:
    return IbkrCallbackNormalizer(QRID, CallbackRequest.QUOTE_SNAPSHOT, con_id=CON_ID,
                                  clock=clock or FakeClock(NOW))


def live_ticks(n: IbkrCallbackNormalizer, *, data_type: int | None = 1, last_trade: str | None = LAST_TRADE,
               rid: int = QRID) -> IbkrCallbackNormalizer:
    """The documented snapshot sequence: marketDataType, price/size ticks, tick 45, tick 49, no END."""
    if data_type is not None:
        n.marketDataType(rid, data_type)
    n.tickPrice(rid, 1, 100.0, ATTRIB)
    n.tickSize(rid, 0, Decimal(300))
    n.tickPrice(rid, 2, 100.02, ATTRIB)
    n.tickSize(rid, 3, Decimal(500))
    n.tickPrice(rid, 4, 100.01, ATTRIB)
    n.tickSize(rid, 5, Decimal(100))
    if last_trade is not None:
        n.tickString(rid, 45, last_trade)
    n.tickGeneric(rid, 49, -1.0)
    return n


def live_snapshot(**kwargs) -> IbkrCallbackNormalizer:
    n = live_ticks(quote_normalizer(), **kwargs)
    n.tickSnapshotEnd(QRID)
    return n


def readiness(contract: IbkrCallbackNormalizer, quote: IbkrCallbackNormalizer | None = None):
    """The normalized streams through the existing FAKE transport and unchanged runner."""
    quote = quote or live_snapshot()
    script = sc.control_script().model_copy(update={"contract_details": contract.envelopes(),
                                                    "quote_snapshot": quote.envelopes()})
    clock = FakeClock(NOW)
    return run_readiness_check(FakeReadOnlyTransport(script, clock), sc.EXPECTATION, clock=clock)


def quote_findings(n: IbkrCallbackNormalizer, *, now=NOW) -> list[str]:
    """Correlation and assessment of a quote stream directly, without the fake transport's stamping."""
    c = correlate(n.envelopes(), request_id=QRID, stage=Stage.QUOTE, end_kind=ResponseKind.TICK_SNAPSHOT_END,
                  data_kinds=frozenset({ResponseKind.MARKET_DATA_TYPE, ResponseKind.QUOTE}))
    assessed = assess_quote(sc.EXPECTATION, c.payloads(ResponseKind.QUOTE),
                            c.payloads(ResponseKind.MARKET_DATA_TYPE), c.end_seen, c.errors, CON_ID, now=now,
                            policy=MockPolicy(), request_id=QRID)
    return [f.code for f in c.findings + assessed]


def codes(report) -> list[str]:
    return [f.code for f in report.findings]


def issue_codes(n: IbkrCallbackNormalizer) -> list[IssueCode]:
    return [i.code for i in n.issues]


# ----------------------------------------------------------------------------- positive control and determinism

def test_documented_contract_and_live_snapshot_sequences_complete_the_existing_control():
    contract, quote = contract_normalizer(), live_snapshot()
    assert contract.completed and quote.completed and not contract.issues and not quote.issues
    assert [e.kind for e in contract.envelopes()] == [ResponseKind.CONTRACT_DETAILS,
                                                      ResponseKind.CONTRACT_DETAILS_END]
    assert [e.kind for e in quote.envelopes()] == [ResponseKind.MARKET_DATA_TYPE, ResponseKind.QUOTE,
                                                   ResponseKind.TICK_SNAPSHOT_END]
    observed = quote.envelopes()[1].quote
    assert (observed.bid, observed.ask, observed.last, observed.bid_size, observed.ask_size) == (
        "100.0", "100.02", "100.01", 300, 500)
    assert observed.con_id == CON_ID and observed.received_at == NOW and observed.halted == -1
    assert observed.tick_types_received == [0, 1, 2, 3, 4, 5, 45, 49]
    report = readiness(contract, quote)
    assert report.outcome == ReadinessOutcome.CONTROL_COMPLETE
    assert CONTROL_CODES <= set(codes(report))
    assert report.dispatched == ["request_contract_details", "request_quote_snapshot",
                                 "request_account_summary", "request_positions"]


def test_the_same_callback_sequence_yields_identical_envelopes_and_report():
    first, second = readiness(contract_normalizer(), live_snapshot()), readiness(contract_normalizer(),
                                                                                 live_snapshot())
    assert render_json(first) == render_json(second)
    n = live_snapshot()
    assert [e.model_dump() for e in n.envelopes()] == [e.model_dump() for e in n.envelopes()]


# ----------------------------------------------------------------------------- contract identity

def test_two_contract_rows_are_ambiguous_and_no_quote_is_requested():
    report = readiness(contract_normalizer(details(), details(conId=CON_ID + 1)))
    assert report.outcome == ReadinessOutcome.NOT_READY
    assert {"CONTRACT_IDENTITY_AMBIGUOUS", "QUOTE_SKIPPED_NO_CONTRACT_IDENTITY"} <= set(codes(report))
    assert "request_quote_snapshot" not in report.dispatched


def test_an_identical_repeated_row_is_not_collapsed_into_one_identity():
    report = readiness(contract_normalizer(details(), details()))
    assert "CONTRACT_IDENTITY_AMBIGUOUS" in codes(report)
    assert "request_quote_snapshot" not in report.dispatched


@pytest.mark.parametrize(("overrides", "code"), [
    ({"currency": "EUR"}, "CONTRACT_CURRENCY_MISMATCH"),
    ({"symbol": "TLH"}, "CONTRACT_SYMBOL_MISMATCH"),
    ({"primaryExchange": "OTHER"}, "CONTRACT_PRIMARY_EXCHANGE_MISMATCH"),
    ({"conId": 0}, "CONTRACT_FIELDS_MISSING"),           # 0 is IBKR's unset default
    ({"primaryExchange": ""}, "CONTRACT_FIELDS_MISSING"),  # "" is IBKR's unset default
])
def test_conflicting_or_unset_identity_fields_keep_existing_classifications(overrides, code):
    report = readiness(contract_normalizer(details(**overrides)))
    assert code in codes(report)
    assert "request_quote_snapshot" not in report.dispatched


@pytest.mark.parametrize("overrides", [
    {"conId": "900001"}, {"conId": -5}, {"symbol": 7}, {"minTick": "0.01"}, {"minTick": float("nan")},
    {"liquidHours": "X" * 5000},
], ids=["con-id-text", "con-id-negative", "symbol-int", "min-tick-text", "min-tick-nan", "hours-oversize"])
def test_malformed_contract_payload_withholds_the_end_marker(overrides):
    n = contract_normalizer(details(**overrides))
    assert issue_codes(n) == [IssueCode.MALFORMED_CALLBACK] and not n.completed
    report = readiness(n)
    assert "CONTRACT_DETAILS_END_MISSING" in codes(report)
    assert "request_quote_snapshot" not in report.dispatched


def test_contract_payload_without_documented_attributes_is_malformed():
    n = contract_normalizer(SimpleNamespace(contract=SimpleNamespace(conId=CON_ID)))
    assert issue_codes(n) == [IssueCode.MALFORMED_CALLBACK]
    assert n.envelopes() == []


def test_broker_error_on_the_contract_request_keeps_its_classification():
    n = IbkrCallbackNormalizer(CRID, CallbackRequest.CONTRACT_DETAILS)
    n.error(CRID, 200, CANARY)
    n.contractDetailsEnd(CRID)
    report = readiness(n)
    assert "CONTRACT_NOT_FOUND" in codes(report)
    assert CANARY not in render_json(report) and CANARY not in repr(n.envelopes())


# ----------------------------------------------------------------------------- request id correlation

def test_contract_callbacks_for_another_request_id_are_uncorrelated():
    n = IbkrCallbackNormalizer(CRID, CallbackRequest.CONTRACT_DETAILS)
    n.contractDetails(CRID + 50, details())
    n.contractDetailsEnd(CRID + 50)
    n.contractDetailsEnd(CRID)
    report = readiness(n)
    assert {"UNCORRELATED_RESPONSE_IGNORED", "CONTRACT_OBSERVATION_MISSING"} <= set(codes(report))
    assert "request_quote_snapshot" not in report.dispatched


def test_quote_ticks_for_another_request_id_are_uncorrelated_not_merged():
    n = live_ticks(quote_normalizer())
    n.tickPrice(QRID + 50, 1, 99.0, ATTRIB)
    n.tickSnapshotEnd(QRID)
    own = [e.quote for e in n.envelopes() if e.kind == ResponseKind.QUOTE and e.request_id == QRID]
    assert [q.bid for q in own] == ["100.0"]  # the own aggregate kept its value
    report = readiness(contract_normalizer(), n)
    assert report.outcome == ReadinessOutcome.NOT_READY
    assert "UNCORRELATED_RESPONSE_IGNORED" in codes(report)


def test_an_end_marker_for_another_request_id_does_not_complete_the_snapshot():
    n = live_ticks(quote_normalizer())
    n.tickSnapshotEnd(QRID + 50)
    assert not n.completed
    assert {"UNCORRELATED_RESPONSE_IGNORED", "TICK_SNAPSHOT_END_MISSING"} <= set(quote_findings(n))


def test_callbacks_of_a_foreign_kind_are_unexpected_on_the_contract_request():
    n = contract_normalizer(end=False)
    n.tickPrice(CRID, 1, 100.0, ATTRIB)
    n.contractDetailsEnd(CRID)
    report = readiness(n)
    assert "UNEXPECTED_RESPONSE_KIND" in codes(report)
    assert "request_quote_snapshot" not in report.dispatched


# ----------------------------------------------------------------------------- END markers and preservation

def test_missing_contract_end_withholds_identity():
    report = readiness(contract_normalizer(end=False))
    assert "CONTRACT_DETAILS_END_MISSING" in codes(report)
    assert "request_quote_snapshot" not in report.dispatched


def test_missing_snapshot_end_preserves_ticks_but_never_stamps_receipt_or_completes():
    n = live_ticks(quote_normalizer())
    kinds = [e.kind for e in n.envelopes()]
    assert kinds == [ResponseKind.MARKET_DATA_TYPE, ResponseKind.QUOTE]
    held = n.envelopes()[1].quote
    assert held.bid == "100.0" and held.received_at is None and not n.completed
    found = quote_findings(n)
    assert {"TICK_SNAPSHOT_END_MISSING", "QUOTE_RECEIPT_UNTIMED"} <= set(found)
    assert "QUOTE_LIVE_CONTROL" not in found
    assert readiness(contract_normalizer(), n).outcome == ReadinessOutcome.NOT_READY


def test_receipt_is_stamped_from_the_injected_clock_only_at_explicit_completion():
    clock = FakeClock(NOW - timedelta(seconds=5))
    n = live_ticks(quote_normalizer(clock))
    clock.advance(5)
    n.tickSnapshotEnd(QRID)
    assert n.envelopes()[1].quote.received_at == NOW


def test_callbacks_after_the_end_marker_are_not_merged_into_the_snapshot():
    n = live_snapshot()
    n.tickPrice(QRID, 1, 50.0, ATTRIB)
    n.tickSnapshotEnd(QRID)
    assert n.envelopes()[1].quote.bid == "100.0"
    assert "RESPONSE_AFTER_END_IGNORED" in quote_findings(n)
    c = contract_normalizer()
    c.contractDetails(CRID, details(conId=CON_ID + 1))
    assert "RESPONSE_AFTER_END_IGNORED" in codes(readiness(c))


# ----------------------------------------------------------------------------- tick payloads

@pytest.mark.parametrize(("feed", "issue"), [
    (lambda n: n.tickPrice(QRID, 1, "100.0", ATTRIB), IssueCode.MALFORMED_CALLBACK),
    (lambda n: n.tickPrice(QRID, 1, True, ATTRIB), IssueCode.MALFORMED_CALLBACK),
    (lambda n: n.tickSize(QRID, 0, Decimal(-1)), IssueCode.MALFORMED_CALLBACK),
    (lambda n: n.tickSize(QRID, 0, Decimal("0.5")), IssueCode.MALFORMED_CALLBACK),
    (lambda n: n.tickSize(QRID, 0, 300.0), IssueCode.MALFORMED_CALLBACK),
    (lambda n: n.tickString(QRID, 45, "yesterday"), IssueCode.MALFORMED_CALLBACK),
    (lambda n: n.tickString(QRID, 45, 1762443000), IssueCode.MALFORMED_CALLBACK),
    (lambda n: n.tickGeneric(QRID, 49, 5.0), IssueCode.MALFORMED_CALLBACK),
    (lambda n: n.tickPrice(QRID, True, 100.0, ATTRIB), IssueCode.MALFORMED_CALLBACK),
    (lambda n: n.tickPrice(QRID, 10_000, 100.0, ATTRIB), IssueCode.MALFORMED_CALLBACK),
    (lambda n: n.tickPrice("1002", 1, 100.0, ATTRIB), IssueCode.MALFORMED_CALLBACK),
    (lambda n: n.marketDataType(QRID, "1"), IssueCode.MALFORMED_CALLBACK),
    (lambda n: n.tickString(QRID, 1, "100.0"), IssueCode.TICK_TYPE_CALLBACK_MISMATCH),
    (lambda n: n.tickPrice(QRID, 45, 1762443000.0, ATTRIB), IssueCode.TICK_TYPE_CALLBACK_MISMATCH),
    (lambda n: n.tickSize(QRID, 1, Decimal(100)), IssueCode.TICK_TYPE_CALLBACK_MISMATCH),
], ids=["price-text", "price-bool", "size-negative", "size-fractional", "size-float", "time-text",
        "time-int", "halted-out-of-range", "tick-type-bool", "tick-type-out-of-range", "req-id-text",
        "data-type-text", "bid-via-string", "time-via-price", "bid-via-size"])
def test_malformed_or_misrouted_tick_payload_withholds_completion(feed, issue):
    n = live_ticks(quote_normalizer())
    feed(n)
    n.tickSnapshotEnd(QRID)
    assert issue_codes(n) == [issue] and not n.completed
    found = quote_findings(n)
    assert "TICK_SNAPSHOT_END_MISSING" in found and "QUOTE_LIVE_CONTROL" not in found
    assert readiness(contract_normalizer(), n).outcome == ReadinessOutcome.NOT_READY


@pytest.mark.parametrize(("bid", "ask"), [(float("nan"), 100.02), (-1.0, 100.02), (100.05, 100.02),
                                          (float("inf"), 100.02), (0.0, 100.02)])
def test_well_typed_but_invalid_prices_are_carried_for_the_existing_integrity_check(bid, ask):
    n = quote_normalizer()
    n.marketDataType(QRID, 1)
    n.tickPrice(QRID, 1, bid, ATTRIB)
    n.tickPrice(QRID, 2, ask, ATTRIB)
    n.tickSize(QRID, 0, Decimal(1))
    n.tickSize(QRID, 3, Decimal(1))
    n.tickString(QRID, 45, LAST_TRADE)
    n.tickSnapshotEnd(QRID)
    assert n.completed
    assert "QUOTE_BID_ASK_INVALID" in quote_findings(n)


def test_missing_price_and_size_ticks_are_missing_not_defaulted():
    n = quote_normalizer()
    n.marketDataType(QRID, 1)
    n.tickString(QRID, 45, LAST_TRADE)
    n.tickSnapshotEnd(QRID)
    found = quote_findings(n)
    assert {"QUOTE_BID_ASK_MISSING", "QUOTE_SIZES_MISSING"} <= set(found)


def test_a_tick_repeated_with_another_value_is_ambiguous_and_never_overwritten():
    n = live_ticks(quote_normalizer())
    n.tickPrice(QRID, 1, 100.01, ATTRIB)
    n.tickSnapshotEnd(QRID)
    quotes = [e.quote for e in n.envelopes() if e.kind == ResponseKind.QUOTE]
    assert [q.bid for q in quotes] == ["100.0", "100.01"]
    assert "QUOTE_OBSERVATIONS_MULTIPLE" in quote_findings(n)


def test_a_tick_repeated_with_the_same_value_stays_one_observation():
    n = live_ticks(quote_normalizer())
    n.tickPrice(QRID, 1, 100.0, ATTRIB)
    n.tickSnapshotEnd(QRID)
    assert "QUOTE_LIVE_CONTROL" in quote_findings(n)


def test_halted_tick_keeps_the_existing_halt_classification():
    halted = quote_normalizer()
    halted.marketDataType(QRID, 1)
    halted.tickPrice(QRID, 1, 100.0, ATTRIB)
    halted.tickPrice(QRID, 2, 100.02, ATTRIB)
    halted.tickSize(QRID, 0, Decimal(1))
    halted.tickSize(QRID, 3, Decimal(1))
    halted.tickString(QRID, 45, LAST_TRADE)
    halted.tickGeneric(QRID, 49, 1.0)
    halted.tickSnapshotEnd(QRID)
    assert halted.completed and "INSTRUMENT_HALTED" in quote_findings(halted)


# ----------------------------------------------------------------------------- live, delayed and frozen

@pytest.mark.parametrize(("data_type", "code"), [(2, "QUOTE_FROZEN"), (3, "QUOTE_DELAYED"),
                                                 (4, "QUOTE_DELAYED"), (9, "MARKET_DATA_TYPE_UNKNOWN"),
                                                 (None, "MARKET_DATA_TYPE_UNKNOWN")])
def test_non_live_market_data_type_is_never_reported_live(data_type, code):
    n = live_snapshot(data_type=data_type)
    found = quote_findings(n)
    assert code in found and "QUOTE_LIVE_CONTROL" not in found
    assert readiness(contract_normalizer(), n).outcome == ReadinessOutcome.NOT_READY


def test_delayed_ticks_are_recorded_and_never_mapped_onto_live_fields():
    n = quote_normalizer()
    n.marketDataType(QRID, 1)
    n.tickPrice(QRID, 66, 100.0, ATTRIB)
    n.tickPrice(QRID, 67, 100.02, ATTRIB)
    n.tickSize(QRID, 69, Decimal(300))
    n.tickString(QRID, 88, LAST_TRADE)
    n.tickSnapshotEnd(QRID)
    quote = n.envelopes()[1].quote
    assert (quote.bid, quote.ask, quote.bid_size, quote.last_timestamp) == (None, None, None, None)
    assert {"QUOTE_DELAYED", "QUOTE_BID_ASK_MISSING", "QUOTE_UNTIMED"} <= set(quote_findings(n))


def test_a_changing_market_data_type_is_ambiguous():
    n = live_ticks(quote_normalizer())
    n.marketDataType(QRID, 3)
    n.tickSnapshotEnd(QRID)
    assert "MARKET_DATA_TYPE_CHANGED" in quote_findings(n)


# ----------------------------------------------------------------------------- timestamps

def test_missing_last_trade_timestamp_is_untimed():
    assert "QUOTE_UNTIMED" in quote_findings(live_snapshot(last_trade=None))


@pytest.mark.parametrize(("offset", "code"), [(-3600, "QUOTE_STALE"), (3600, "QUOTE_TIMESTAMP_FUTURE")])
def test_stale_or_future_last_trade_timestamp_keeps_its_classification(offset, code):
    stamp = str(int((NOW + timedelta(seconds=offset)).timestamp()))
    n = live_snapshot(last_trade=stamp)
    assert code in quote_findings(n)
    assert readiness(contract_normalizer(), n).outcome == ReadinessOutcome.NOT_READY


def test_an_unusable_clock_leaves_the_receipt_unstamped_and_incomplete():
    n = live_ticks(IbkrCallbackNormalizer(QRID, CallbackRequest.QUOTE_SNAPSHOT, con_id=CON_ID,
                                          clock=SimpleNamespace(now=lambda: "not a time")))
    n.tickSnapshotEnd(QRID)
    assert issue_codes(n) == [IssueCode.CLOCK_INVALID] and not n.completed
    assert {"TICK_SNAPSHOT_END_MISSING", "QUOTE_RECEIPT_UNTIMED"} <= set(quote_findings(n))


# ----------------------------------------------------------------------------- messages and unsupported callbacks

def test_entitlement_error_on_the_snapshot_keeps_its_classification():
    n = live_ticks(quote_normalizer())
    n.error(QRID, 354, CANARY, CANARY)
    n.tickSnapshotEnd(QRID)
    found = quote_findings(n)
    assert "ENTITLEMENT_UNAVAILABLE" in found
    assert CANARY not in repr(n.envelopes()) and CANARY not in repr(n.issues)


def test_session_level_messages_are_recorded_and_only_non_ok_codes_withhold_completion():
    ok = live_ticks(quote_normalizer())
    ok.error(ic.SESSION_REQUEST_ID, 2104, CANARY)
    ok.tickSnapshotEnd(QRID)
    assert ok.completed and [m.code for m in ok.session_messages] == [2104]
    lost = live_ticks(quote_normalizer())
    lost.error(ic.SESSION_REQUEST_ID, 1100, CANARY)
    lost.tickSnapshotEnd(QRID)
    assert issue_codes(lost) == [IssueCode.SESSION_MESSAGE_DURING_REQUEST] and not lost.completed
    assert "TICK_SNAPSHOT_END_MISSING" in quote_findings(lost)


@pytest.mark.parametrize(("name", "label"), [("tickEFP", "tickEFP"), ("connectionClosed", "connectionClosed"),
                                             ("bad name!", "INVALID_NAME"), (None, "INVALID_NAME")])
def test_unsupported_callbacks_are_recorded_by_name_and_never_interpreted(name, label):
    n = live_ticks(quote_normalizer())
    n.unsupported_callback(name, QRID)
    n.tickSnapshotEnd(QRID)
    assert [(i.code, i.callback) for i in n.issues] == [(IssueCode.UNSUPPORTED_CALLBACK, label)]
    assert not n.completed and "TICK_SNAPSHOT_END_MISSING" in quote_findings(n)


def test_callback_count_is_bounded_and_overflow_is_incomplete():
    n = live_ticks(quote_normalizer())
    for _ in range(ic.MAX_CALLBACKS):
        n.tickPrice(QRID, 1, 100.0, ATTRIB)
    n.tickSnapshotEnd(QRID)
    assert issue_codes(n) == [IssueCode.CALLBACK_LIMIT_EXCEEDED] and not n.completed
    assert n.ignored_callbacks > 0 and len(n.envelopes()) <= ic.MAX_CALLBACKS


def test_issue_list_is_bounded():
    n = quote_normalizer()
    for _ in range(ic.MAX_ISSUES + 5):
        n.unsupported_callback("tickEFP", QRID)
    assert len(n.issues) == ic.MAX_ISSUES and n.issue_overflow == 5


# ----------------------------------------------------------------------------- surface and non-inference

def test_surface_is_documented_callbacks_and_read_only_outputs_only():
    public = {name for name in dir(IbkrCallbackNormalizer) if not name.startswith("_")}
    assert public == {"contractDetails", "contractDetailsEnd", "marketDataType", "tickPrice", "tickSize",
                      "tickString", "tickGeneric", "tickSnapshotEnd", "error", "unsupported_callback",
                      "envelopes", "completed", "issues", "issue_overflow", "ignored_callbacks",
                      "session_messages"}
    for name in public | set(vars(quote_normalizer())):
        assert not FORBIDDEN_NAME_RE.search(name), name
    for absent in ("environment_claim", "account_id", "managedAccounts", "read_only_api_attested",
                   "socket_port", "attest_session", "connected"):
        assert not hasattr(quote_normalizer(), absent), absent


def test_nothing_about_environment_or_account_is_inferred_from_market_data():
    n = live_snapshot()
    dumped = json.dumps([e.model_dump(mode="json") for e in n.envelopes()])
    for word in ("PAPER", "SYNTH-", "7497", "4002", "environment"):
        assert word not in dumped, word
    assert all(e.account_summary is None and e.position is None for e in n.envelopes())
    assert {e.kind for e in n.envelopes()} <= {ResponseKind.MARKET_DATA_TYPE, ResponseKind.QUOTE,
                                               ResponseKind.TICK_SNAPSHOT_END}


def test_module_imports_no_sdk_or_network_and_reads_no_wall_clock():
    source = ic.__file__
    with open(source, encoding="utf-8") as handle:
        text = handle.read()
    roots = set()
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, ast.Import):
            roots |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            roots.add((node.module or "").split(".")[0])
    assert roots <= {"__future__", "re", "dataclasses", "datetime", "decimal", "enum", "typing", "tradingbot"}
    assert "datetime.now(" not in text and "import time" not in text


@pytest.mark.parametrize(("args", "kwargs"), [
    ((-1, CallbackRequest.CONTRACT_DETAILS), {}),
    ((True, CallbackRequest.CONTRACT_DETAILS), {}),
    ((QRID, CallbackRequest.QUOTE_SNAPSHOT), {"clock": FakeClock(NOW)}),
    ((QRID, CallbackRequest.QUOTE_SNAPSHOT), {"con_id": CON_ID}),
    ((CRID, CallbackRequest.CONTRACT_DETAILS), {"con_id": CON_ID}),
    ((CRID, "ORDERS"), {}),
])
def test_constructor_refuses_an_unbound_or_mismatched_request(args, kwargs):
    with pytest.raises(ValueError):
        IbkrCallbackNormalizer(*args, **kwargs)
