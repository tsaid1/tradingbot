"""S2-MAP-01 offline instrument-mapping diagnostics (issue #10). Synthetic inputs only; NON-AUTHORITATIVE.

Covers the positive direct-identity informational control, ambiguous/unknown basis, source-series and
proxy mismatches, incomplete option/futures identifiers, invalid economics, the unchanged unsupported
runtime for derivatives, determinism against a committed golden report, the absence of any
authorization vocabulary, and that the deterministic gateway's outcomes are untouched.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

import tradingbot
from tradingbot import instrument_diagnostics as diag
from tradingbot.event_models import PriceBasis, Timing, TradeAction
from tradingbot.instrument_diagnostics import (
    AUTHORITY,
    ContractSpec,
    FindingKind,
    Identity,
    MappingRecord,
    RuntimeScope,
    SeriesKind,
    SourceSeries,
    diagnose_record,
    diagnose_records,
    load_records,
    render_json,
    render_text,
)
from tradingbot.models import Action, AssetClass, Instrument, MockPolicy, PositionSide, Quote
from tradingbot.validator import gate_action

REPO = Path(__file__).resolve().parents[1]
FIXTURES = REPO / "fixtures" / "instrument_mapping_synthetic"
SCRIPT = REPO / "scripts" / "inspect_instrument_mapping.py"
SRC = REPO / "src"
NOW = datetime(2026, 11, 6, 14, 30, tzinfo=timezone.utc)


def record(**overrides) -> MappingRecord:
    """A direct equity control; override any top-level field."""
    base = {
        "record_id": "t", "synthetic": True,
        "source": SourceSeries(symbol="ABC", kind=SeriesKind.TRADED_SECURITY, currency="USD", unit="USD_PER_SHARE",
                               quoted_level="100.00"),
        "instrument": Instrument(asset_class=AssetClass.EQUITY, symbol="ABC", exchange="SYNTH-X"),
        "price_basis": PriceBasis.TRADED_CONTRACT,
        "contract": ContractSpec(currency="USD", unit="USD_PER_SHARE", multiplier="1", tick_size="0.01"),
    }
    base.update(overrides)
    return MappingRecord(**base)


def source(**kw) -> SourceSeries:
    base = {"symbol": "ABC", "kind": SeriesKind.TRADED_SECURITY, "currency": "USD", "unit": "USD_PER_SHARE",
            "quoted_level": "100.00"}
    base.update(kw)
    return SourceSeries(**base)


def codes(d) -> list[str]:
    return [f.code for f in d.findings]


def fixture_records() -> dict[str, MappingRecord]:
    return {r.record_id: r for r in load_records(FIXTURES / "records.json")}


# ----------------------------------------------------------------------------- import origin

def test_module_is_imported_from_this_checkout():
    """The shared editable venv may point at another worktree; the suite must exercise this one."""
    assert Path(tradingbot.__file__).resolve().parents[2] == REPO
    assert Path(diag.__file__).resolve() == SRC / "tradingbot" / "instrument_diagnostics.py"


# ----------------------------------------------------------------------------- inputs

def test_unmarked_or_malformed_records_are_refused():
    with pytest.raises(ValidationError):
        MappingRecord(record_id="x")  # synthetic flag absent
    with pytest.raises(ValidationError):
        MappingRecord(record_id="x", synthetic=False)
    with pytest.raises(ValidationError):
        record(broker="any")  # extra fields are forbidden
    with pytest.raises(ValidationError):
        record(contract=ContractSpec(settlement="NET"))


def test_load_records_accepts_list_or_object_and_refuses_unmarked(tmp_path):
    item = {"record_id": "a", "synthetic": True}
    (tmp_path / "list.json").write_text(json.dumps([item]))
    (tmp_path / "obj.json").write_text(json.dumps({"notice": "synthetic", "records": [item]}))
    assert [r.record_id for r in load_records(tmp_path / "list.json")] == ["a"]
    assert [r.record_id for r in load_records(tmp_path / "obj.json")] == ["a"]
    (tmp_path / "bad.json").write_text(json.dumps([{"record_id": "a"}]))
    with pytest.raises(ValueError, match="synthetic"):
        load_records(tmp_path / "bad.json")
    (tmp_path / "scalar.json").write_text("42")
    with pytest.raises(ValueError, match="expected a JSON list"):
        load_records(tmp_path / "scalar.json")
    with pytest.raises(ValueError, match="Cannot read"):
        load_records(tmp_path / "missing.json")


# ----------------------------------------------------------------------------- direct identity control

def test_direct_identity_is_an_informational_control_not_permission():
    for cls, symbol in ((AssetClass.EQUITY, "ABC"), (AssetClass.ETF, "TLT")):
        d = diagnose_record(record(source=source(symbol=f"${symbol}"),
                                   instrument=Instrument(asset_class=cls, symbol=symbol, exchange="SYNTH-X")))
        assert codes(d) == ["DIRECT_IDENTITY_CONTROL"]
        assert (d.identity, d.headline, d.runtime_scope) == (
            Identity.DIRECT, FindingKind.INFO, RuntimeScope.EQUITY_ETF_GATEWAY_CHECKS_STILL_APPLY)
        assert d.authority == AUTHORITY == "NON_AUTHORITATIVE"
        assert "not trading permission" in d.findings[0].detail


def test_control_requires_stated_traded_basis_and_orderable_source_kind():
    d = diagnose_record(record(price_basis=PriceBasis.UNKNOWN))
    assert codes(d) == ["PRICE_BASIS_UNKNOWN"] and d.headline == FindingKind.AMBIGUOUS and d.identity == Identity.DIRECT
    d = diagnose_record(record(source=source(kind=SeriesKind.UNKNOWN)))
    assert codes(d) == ["SOURCE_SERIES_KIND_UNKNOWN"] and d.headline == FindingKind.AMBIGUOUS
    d = diagnose_record(record(source=source(kind=SeriesKind.INDEX)))
    assert codes(d) == ["PRICE_BASIS_CLAIM_CONFLICT", "SOURCE_KIND_NOT_ORDERABLE"]
    d = diagnose_record(record(source=source(kind=SeriesKind.DATED_CONTRACT)))
    assert codes(d) == ["SOURCE_KIND_ASSET_CLASS_CONFLICT"]
    d = diagnose_record(record(price_basis=PriceBasis.SOURCE_SERIES))
    assert codes(d) == ["SOURCE_KIND_BASIS_CONFLICT", "PRICE_BASIS_NOT_TRADED_CONTRACT"]


def test_bare_ticker_reports_each_gap_explicitly():
    d = diagnose_record(record(source=source(currency=None, unit=None),
                               instrument=Instrument(asset_class=AssetClass.EQUITY, symbol="ABC"), contract=None))
    assert codes(d) == ["DIRECT_IDENTITY_CONTROL", "SOURCE_CURRENCY_MISSING", "SOURCE_UNIT_MISSING",
                        "CONTRACT_SPEC_MISSING", "LISTING_VENUE_UNSPECIFIED"]
    assert d.headline == FindingKind.MISSING
    assert d.counts == {"INFO": 1, "MISSING": 3, "AMBIGUOUS": 1, "MISMATCH": 0, "INVALID": 0, "UNSUPPORTED": 0}


# ----------------------------------------------------------------------------- proxies and basis

def test_index_level_against_etf_is_a_proxy_mismatch_with_no_conversion():
    spx = source(symbol="SPX", kind=SeriesKind.INDEX, unit="INDEX_POINT", quoted_level="6000.00")
    spy = Instrument(asset_class=AssetClass.ETF, symbol="SPY", exchange="SYNTH-X")
    d = diagnose_record(record(source=spx, instrument=spy, price_basis=PriceBasis.SOURCE_SERIES))
    assert codes(d) == ["PRICE_BASIS_NOT_TRADED_CONTRACT", "SOURCE_SERIES_PROXY_MISMATCH", "UNIT_MISMATCH"]
    assert (d.identity, d.headline) == (Identity.DIFFERENT, FindingKind.MISMATCH)
    claimed = diagnose_record(record(source=spx, instrument=spy, price_basis=PriceBasis.TRADED_CONTRACT))
    assert codes(claimed) == ["PRICE_BASIS_CLAIM_CONFLICT", "SOURCE_SERIES_PROXY_MISMATCH", "UNIT_MISMATCH"]
    # No converted or implied price appears anywhere: these details carry no number at all.
    for f in d.findings + claimed.findings:
        assert not re.search(r"\d", f.detail), f.detail


def test_spot_commodity_against_etf_reports_currency_and_unit_differences():
    d = diagnose_record(record(
        source=source(symbol="WTI_SPOT", kind=SeriesKind.SPOT, unit="USD_PER_BARREL", quoted_level="70.25"),
        instrument=Instrument(asset_class=AssetClass.ETF, symbol="OILF", exchange="SYNTH-LDN"),
        price_basis=PriceBasis.SOURCE_SERIES,
        contract=ContractSpec(currency="GBP", unit="GBP_PER_SHARE", multiplier="1", tick_size="0.01")))
    assert codes(d) == ["PRICE_BASIS_NOT_TRADED_CONTRACT", "SOURCE_SERIES_PROXY_MISMATCH", "CURRENCY_MISMATCH",
                        "UNIT_MISMATCH"]
    assert d.identity == Identity.DIFFERENT


def test_different_listed_security_is_a_symbol_mismatch():
    d = diagnose_record(record(source=source(symbol="XYZ")))
    assert codes(d) == ["PRICE_BASIS_CLAIM_CONFLICT", "SOURCE_INSTRUMENT_SYMBOL_MISMATCH"]


def test_off_tick_grid_only_when_the_level_claims_the_contract_basis():
    off = source(quoted_level="100.005")
    assert "QUOTED_LEVEL_OFF_TICK_GRID" in codes(diagnose_record(record(source=off)))
    assert "QUOTED_LEVEL_OFF_TICK_GRID" not in codes(diagnose_record(record(source=off, price_basis=PriceBasis.UNKNOWN)))


# ----------------------------------------------------------------------------- derivatives

OPTION = Instrument(asset_class=AssetClass.OPTION, symbol="SPX-SYN-20261218-6000-C", underlying="SPX",
                    expiry="2026-12-18", strike="6000", option_right="CALL", exchange="SYNTH-OPT")
OPTION_SPEC = ContractSpec(currency="USD", unit="INDEX_POINT", multiplier="10", tick_size="0.05", settlement="CASH",
                           settlement_time="PM", exercise_style="EUROPEAN")
FUTURE = Instrument(asset_class=AssetClass.FUTURE, symbol="IDXFZ26", underlying="SPX", expiry="2026-12-18",
                    futures_contract="IDXFZ26", exchange="SYNTH-FUT")
FUTURE_SPEC = ContractSpec(currency="USD", unit="INDEX_POINT", multiplier="10", tick_size="0.5", tick_value="5",
                           settlement="CASH", contract_month="2026-12")
SPX = source(symbol="SPX", kind=SeriesKind.INDEX, unit="INDEX_POINT", quoted_level="6000.00")


def test_complete_option_and_future_metadata_still_leave_the_runtime_unsupported():
    for inst, spec in ((OPTION, OPTION_SPEC), (FUTURE, FUTURE_SPEC)):
        own = source(symbol=inst.symbol, kind=SeriesKind.DATED_CONTRACT, unit="INDEX_POINT", quoted_level="45.50")
        d = diagnose_record(record(as_of="2026-11-06", source=own, instrument=inst, contract=spec))
        assert codes(d) == ["DIRECT_IDENTITY_CONTROL", "RUNTIME_ASSET_CLASS_UNSUPPORTED"]
        assert (d.identity, d.headline, d.runtime_scope) == (
            Identity.DIRECT, FindingKind.UNSUPPORTED, RuntimeScope.ASSET_CLASS_UNSUPPORTED_IN_CURRENT_RUNTIME)
        assert "ASSET_CLASS_PAPER_ADAPTER_UNSUPPORTED" in d.findings[-1].detail
        on_underlying = diagnose_record(record(as_of="2026-11-06", source=SPX, instrument=inst, contract=spec,
                                               price_basis=PriceBasis.SOURCE_SERIES))
        assert codes(on_underlying) == ["PRICE_BASIS_NOT_TRADED_CONTRACT", "SOURCE_IS_STATED_UNDERLYING",
                                        "RUNTIME_ASSET_CLASS_UNSUPPORTED"]
        assert on_underlying.identity == Identity.DERIVATIVE_OF_SOURCE


def test_incomplete_option_lists_every_missing_identifier():
    d = diagnose_record(record(source=SPX, price_basis=PriceBasis.UNKNOWN,
                               instrument=Instrument(asset_class=AssetClass.OPTION, symbol="SPXOPT", underlying="SPX"),
                               contract=ContractSpec(currency="USD", unit="INDEX_POINT")))
    missing = {f.code for f in d.findings if f.kind == FindingKind.MISSING}
    assert missing == {"MULTIPLIER_MISSING", "OPTION_EXPIRY_MISSING", "OPTION_STRIKE_MISSING", "OPTION_RIGHT_MISSING",
                       "OPTION_EXERCISE_STYLE_MISSING", "OPTION_SETTLEMENT_MISSING"}
    ambiguous = {f.code for f in d.findings if f.kind == FindingKind.AMBIGUOUS}
    assert ambiguous == {"PRICE_BASIS_UNKNOWN", "LISTING_VENUE_UNSPECIFIED", "OPTION_SETTLEMENT_TIME_UNSPECIFIED"}
    assert d.headline == FindingKind.MISSING and "RUNTIME_ASSET_CLASS_UNSUPPORTED" in codes(d)
    physical = diagnose_record(record(source=SPX, price_basis=PriceBasis.UNKNOWN, instrument=OPTION,
                                      contract=ContractSpec(settlement="PHYSICAL")))
    assert "OPTION_SETTLEMENT_TIME_UNSPECIFIED" not in codes(physical)


def test_incomplete_future_lists_every_missing_identifier_and_does_not_guess_the_underlying():
    d = diagnose_record(record(source=source(symbol="HENRY_HUB", kind=SeriesKind.SPOT, currency=None, unit=None,
                                             quoted_level="3.10"),
                               instrument=Instrument(asset_class=AssetClass.FUTURE, symbol="NGS"),
                               price_basis=PriceBasis.UNKNOWN, contract=None))
    missing = {f.code for f in d.findings if f.kind == FindingKind.MISSING}
    assert missing == {"SOURCE_CURRENCY_MISSING", "SOURCE_UNIT_MISSING", "CONTRACT_SPEC_MISSING", "UNDERLYING_MISSING",
                       "FUTURES_VENUE_MISSING", "FUTURES_EXPIRY_MISSING", "FUTURES_CONTRACT_CODE_MISSING"}
    assert "SOURCE_UNDERLYING_UNCOMPARABLE" in codes(d) and d.identity == Identity.DIFFERENT
    with_spec = diagnose_record(record(source=SPX, price_basis=PriceBasis.SOURCE_SERIES, instrument=FUTURE,
                                       contract=ContractSpec(currency="USD", unit="INDEX_POINT")))
    assert {"MULTIPLIER_MISSING", "TICK_SIZE_MISSING", "FUTURES_CONTRACT_MONTH_MISSING",
            "FUTURES_SETTLEMENT_MISSING"} <= set(codes(with_spec))


def test_futures_contract_code_reading_is_conservative():
    def fut(code, month):
        inst = FUTURE.model_copy(update={"symbol": code, "futures_contract": code})
        spec = FUTURE_SPEC.model_copy(update={"contract_month": month})
        return codes(diagnose_record(record(as_of="2026-11-06", source=SPX, instrument=inst, contract=spec,
                                            price_basis=PriceBasis.SOURCE_SERIES)))
    assert "FUTURES_CONTRACT_MONTH_CONFLICT" not in fut("IDXFZ26", "2026-12")
    assert "FUTURES_CONTRACT_MONTH_CONFLICT" in fut("IDXFG27", "2027-04")
    assert "FUTURES_CONTRACT_MONTH_CONFLICT" in fut("IDXFZ27", "2026-12")
    assert {"FUTURES_YEAR_DIGIT_AMBIGUOUS"} <= set(fut("IDXFZ6", "2026-12"))
    assert "FUTURES_CONTRACT_MONTH_CONFLICT" not in fut("IDXFZ6", "2026-12")
    assert "FUTURES_CONTRACT_CODE_FORMAT_UNRECOGNIZED" in fut("IDXF-DEC26", "2026-12")
    assert "FUTURES_CONTRACT_MONTH_FORMAT_INVALID" in fut("IDXFZ26", "Dec-2026")


def test_continuous_series_never_picks_a_roll_and_labels_are_not_aliased():
    cont = source(symbol="WTI_CONT", kind=SeriesKind.CONTINUOUS_FUTURES, unit="USD_PER_BARREL", quoted_level="70.25")
    crude = Instrument(asset_class=AssetClass.FUTURE, symbol="CRDG27", underlying="WTI", expiry="2027-01-20",
                       futures_contract="CRDG27", exchange="SYNTH-NRG")
    spec = ContractSpec(currency="USD", unit="USD_PER_BARREL", multiplier="500", tick_size="0.05", tick_value="25",
                        settlement="PHYSICAL", contract_month="2027-02")
    d = diagnose_record(record(source=cont, instrument=crude, contract=spec, price_basis=PriceBasis.SOURCE_SERIES))
    assert codes(d) == ["PRICE_BASIS_NOT_TRADED_CONTRACT", "SOURCE_UNDERLYING_LABEL_MISMATCH",
                        "CONTINUOUS_SERIES_ROLL_UNRESOLVED", "RUNTIME_ASSET_CLASS_UNSUPPORTED"]
    assert d.identity == Identity.DIFFERENT
    same_label = diagnose_record(record(source=cont, instrument=crude.model_copy(update={"underlying": "WTI_CONT"}),
                                        contract=spec, price_basis=PriceBasis.SOURCE_SERIES))
    assert codes(same_label) == ["PRICE_BASIS_NOT_TRADED_CONTRACT", "SOURCE_IS_STATED_UNDERLYING",
                                 "CONTINUOUS_SERIES_ROLL_UNRESOLVED", "RUNTIME_ASSET_CLASS_UNSUPPORTED"]
    assert same_label.identity == Identity.DERIVATIVE_OF_SOURCE


# ----------------------------------------------------------------------------- invalid economics

def test_invalid_numeric_metadata_is_reported_not_repaired():
    d = diagnose_record(record(
        as_of="2026-11-06",
        source=source(symbol="HENRY_HUB", kind=SeriesKind.SPOT, currency="usd", unit="USD_PER_MMBTU", quoted_level="-3.10"),
        instrument=Instrument(asset_class=AssetClass.FUTURE, symbol="NGSH27", underlying="HENRY_HUB", expiry="2026-02-24",
                              futures_contract="NGSH27", exchange="SYNTH-NRG"),
        price_basis=PriceBasis.SOURCE_SERIES,
        contract=ContractSpec(currency="USD", unit="USD_PER_MMBTU", multiplier="0", tick_size="abc", tick_value="NaN",
                              settlement="PHYSICAL", contract_month="2027-03")))
    invalid = {f.code for f in d.findings if f.kind == FindingKind.INVALID}
    assert invalid == {"CURRENCY_CODE_INVALID", "SOURCE_LEVEL_INVALID", "MULTIPLIER_INVALID", "TICK_SIZE_INVALID",
                       "TICK_VALUE_INVALID", "CONTRACT_EXPIRED_AT_AS_OF"}
    assert d.headline == FindingKind.INVALID
    inconsistent = diagnose_record(record(as_of="2026-11-06", instrument=FUTURE, price_basis=PriceBasis.TRADED_CONTRACT,
                                          source=source(symbol="IDXFZ26", kind=SeriesKind.DATED_CONTRACT, unit="INDEX_POINT",
                                                        quoted_level="6025.5"),
                                          contract=FUTURE_SPEC.model_copy(update={"tick_value": "7"})))
    assert "TICK_VALUE_INCONSISTENT" in codes(inconsistent)
    option = diagnose_record(record(as_of="2026-11-06", source=SPX, price_basis=PriceBasis.SOURCE_SERIES,
                                    instrument=OPTION.model_copy(update={"strike": "-95", "expiry": "18/12/2026"}),
                                    contract=OPTION_SPEC))
    assert {"OPTION_STRIKE_INVALID", "EXPIRY_FORMAT_INVALID"} <= set(codes(option))
    assert "AS_OF_DATE_INVALID" in codes(diagnose_record(record(as_of="yesterday")))


def test_expiry_is_compared_only_to_the_record_as_of_never_to_the_clock():
    past = OPTION.model_copy(update={"expiry": "2001-01-19"})
    without = diagnose_record(record(source=SPX, price_basis=PriceBasis.SOURCE_SERIES, instrument=past, contract=OPTION_SPEC))
    assert "CONTRACT_EXPIRED_AT_AS_OF" not in codes(without)
    with_as_of = diagnose_record(record(as_of="2026-11-06", source=SPX, price_basis=PriceBasis.SOURCE_SERIES,
                                        instrument=past, contract=OPTION_SPEC))
    assert "CONTRACT_EXPIRED_AT_AS_OF" in codes(with_as_of)
    text = Path(diag.__file__).read_text()
    assert "now(" not in text and "today(" not in text


def test_cash_instrument_identifier_rules():
    d = diagnose_record(record(instrument=Instrument(asset_class=AssetClass.EQUITY, symbol="ABC", exchange="SYNTH-X",
                                                     expiry="2026-12-18", strike="100", option_right="CALL")))
    assert codes(d) == ["DIRECT_IDENTITY_CONTROL", "DERIVATIVE_FIELDS_ON_CASH_INSTRUMENT"]
    d = diagnose_record(record(source=source(symbol="ABC.A", currency="CAD", unit="CAD_PER_SHARE"),
                               instrument=Instrument(asset_class=AssetClass.EQUITY, symbol="ABC.A", exchange="SYNTH-CA")))
    assert codes(d) == ["DIRECT_IDENTITY_CONTROL", "CURRENCY_MISMATCH", "UNIT_MISMATCH", "SYMBOL_SUFFIX_CONVENTION_AMBIGUOUS"]
    d = diagnose_record(record(source=source(symbol="abc"),
                               instrument=Instrument(asset_class=AssetClass.EQUITY, symbol="abc", exchange="SYNTH-X")))
    assert codes(d) == ["DIRECT_IDENTITY_CONTROL", "SYMBOL_FORMAT_INVALID"] and d.headline == FindingKind.INVALID
    d = diagnose_record(record(instrument=Instrument(asset_class=AssetClass.FUTURE, symbol="IDXFZ26", underlying="SPX",
                                                     expiry="2026-12-18", futures_contract="IDXFZ26", exchange="X",
                                                     strike="5", option_right="PUT"),
                               source=source(symbol="IDXFZ26", kind=SeriesKind.DATED_CONTRACT, unit="INDEX_POINT"),
                               contract=FUTURE_SPEC))
    assert "OPTION_FIELDS_ON_FUTURE" in codes(d)


def test_missing_sides_are_undetermined():
    d = diagnose_record(record(instrument=None, contract=None))
    assert codes(d) == ["INSTRUMENT_MISSING"]
    assert (d.identity, d.runtime_scope, d.asset_class) == (Identity.UNDETERMINED, RuntimeScope.UNDETERMINED, None)
    d = diagnose_record(record(source=None))
    assert codes(d) == ["SOURCE_SERIES_MISSING"] and d.identity == Identity.UNDETERMINED
    d = diagnose_record(record(source=source(symbol=None)))
    assert codes(d)[0] == "SOURCE_SERIES_MISSING"


# ----------------------------------------------------------------------------- determinism and vocabulary

def test_fixture_report_is_deterministic_and_matches_the_committed_golden():
    records = load_records(FIXTURES / "records.json")
    assert all(r.synthetic is True for r in records)
    first, second = render_json(diagnose_records(records)), render_json(diagnose_records(list(reversed(records))[::-1]))
    assert first == second
    assert first == (FIXTURES / "expected_report.json").read_text(encoding="utf-8")
    report = json.loads(first)
    assert [r["record_id"] for r in report["records"]] == [r.record_id for r in records]
    assert report["record_count"] == len(records) == 24
    assert report["rules_version"] == diag.RULES_VERSION
    assert sum(report["headlines"].values()) == len(records)
    assert sum(report["totals"].values()) == sum(len(r["findings"]) for r in report["records"])


def test_fixture_covers_every_required_boundary():
    by_id = {d.record_id: d for d in diagnose_records(load_records(FIXTURES / "records.json")).records}
    expected = {
        "eq_direct_control": (FindingKind.INFO, "DIRECT_IDENTITY_CONTROL"),
        "etf_direct_control": (FindingKind.INFO, "DIRECT_IDENTITY_CONTROL"),
        "eq_basis_unknown": (FindingKind.AMBIGUOUS, "PRICE_BASIS_UNKNOWN"),
        "etf_spx_index_proxy": (FindingKind.MISMATCH, "SOURCE_SERIES_PROXY_MISMATCH"),
        "etf_gold_spot_proxy": (FindingKind.MISMATCH, "SOURCE_SERIES_PROXY_MISMATCH"),
        "etf_crude_spot_proxy_currency": (FindingKind.MISMATCH, "CURRENCY_MISMATCH"),
        "opt_incomplete": (FindingKind.MISSING, "OPTION_STRIKE_MISSING"),
        "opt_premium_quoted_complete": (FindingKind.UNSUPPORTED, "RUNTIME_ASSET_CLASS_UNSUPPORTED"),
        "fut_dated_contract_quoted_complete": (FindingKind.UNSUPPORTED, "RUNTIME_ASSET_CLASS_UNSUPPORTED"),
        "fut_crude_continuous_series": (FindingKind.MISMATCH, "CONTINUOUS_SERIES_ROLL_UNRESOLVED"),
        "fut_gold_month_conflict": (FindingKind.MISMATCH, "FUTURES_CONTRACT_MONTH_CONFLICT"),
        "fut_natgas_invalid_economics": (FindingKind.INVALID, "MULTIPLIER_INVALID"),
        "fut_incomplete": (FindingKind.MISSING, "FUTURES_CONTRACT_CODE_MISSING"),
        "no_instrument": (FindingKind.MISSING, "INSTRUMENT_MISSING"),
    }
    for record_id, (headline, code) in expected.items():
        assert by_id[record_id].headline == headline, record_id
        assert code in codes(by_id[record_id]), record_id
    assert {d.asset_class for d in by_id.values()} == {None, *AssetClass}


def test_report_carries_no_authorization_vocabulary():
    report = diagnose_records(load_records(FIXTURES / "records.json"))
    for rendered in (render_json(report), render_text(report), render_text(report, summary=True)):
        lowered = rendered.lower()
        for token in ("approve", "executable", "eligible", "execute_mock", "simulated_filled", "paper_open", "order_id"):
            assert token not in lowered, token
        assert "NON_AUTHORITATIVE" in rendered
    assert all(d.authority == "NON_AUTHORITATIVE" for d in report.records)
    assert {d.headline for d in report.records} <= set(FindingKind)


def test_text_renderers_are_concise_and_complete():
    report = diagnose_records(load_records(FIXTURES / "records.json"))
    summary = render_text(report, summary=True).splitlines()
    assert sum(line.startswith("[") for line in summary) == report.record_count
    full = render_text(report)
    for d in report.records:
        assert f"[{d.record_id}]" in full
        for f in d.findings:
            assert f.code in full


# ----------------------------------------------------------------------------- the gateway is untouched

def test_module_has_no_order_network_or_engine_path():
    text = Path(diag.__file__).read_text()
    for forbidden in ("event_replay", "storage", "pipeline", "requests", "urllib", "socket", "http", "sqlite",
                      "openai", "fill_quantity", "MockOrder", "PaperOrder"):
        assert forbidden not in text, forbidden
    assert not hasattr(diag, "ResolvedInstrument")


def _action(inst: Instrument, basis: PriceBasis, source_symbol: str | None) -> TradeAction:
    return TradeAction(action_id="a", action=Action.OPEN, timing=Timing.CURRENT, instrument=inst,
                       position_side=PositionSide.LONG, price_basis=basis, source_market_symbol=source_symbol,
                       entry_reference="100", stop_price="95", target_price="110", explicit_instruction=True,
                       evidence=["synthetic"])


def test_gateway_outcomes_are_unchanged_by_diagnostics():
    quote = Quote(symbol="ABC", bid=Decimal("99.95"), ask=Decimal("100.05"), observed_at=NOW)
    policy = MockPolicy()
    cases = {
        "direct": (record(), _action(record().instrument, PriceBasis.TRADED_CONTRACT, "ABC"), "APPROVE", "PAPER_OPEN"),
        "basis_unknown": (record(price_basis=PriceBasis.UNKNOWN), _action(record().instrument, PriceBasis.UNKNOWN, "ABC"),
                          "HOLD", "SOURCE_TO_BROKER_PRICE_BASIS_UNRESOLVED"),
        "source_series": (record(source=SPX, price_basis=PriceBasis.SOURCE_SERIES),
                          _action(record().instrument, PriceBasis.SOURCE_SERIES, "SPX"),
                          "HOLD", "SOURCE_TO_BROKER_PRICE_BASIS_UNRESOLVED"),
        "proxy_claimed": (record(source=SPX), _action(record().instrument, PriceBasis.TRADED_CONTRACT, "SPX"),
                          "HOLD", "SOURCE_TO_BROKER_MAPPING_UNAPPROVED"),
        "option": (record(instrument=OPTION, contract=OPTION_SPEC,
                          source=source(symbol=OPTION.symbol, kind=SeriesKind.DATED_CONTRACT, unit="INDEX_POINT")),
                   _action(OPTION, PriceBasis.TRADED_CONTRACT, OPTION.symbol), "HOLD", "ASSET_CLASS_PAPER_ADAPTER_UNSUPPORTED"),
        "future": (record(instrument=FUTURE, contract=FUTURE_SPEC,
                          source=source(symbol=FUTURE.symbol, kind=SeriesKind.DATED_CONTRACT, unit="INDEX_POINT")),
                   _action(FUTURE, PriceBasis.TRADED_CONTRACT, FUTURE.symbol), "HOLD", "ASSET_CLASS_PAPER_ADAPTER_UNSUPPORTED"),
    }
    for name, (rec, action, status, reason) in cases.items():
        before = gate_action(action, NOW, quote, policy, None, now=NOW)
        d = diagnose_record(rec)
        after = gate_action(action, NOW, quote, policy, None, now=NOW)
        assert before == after and (after.status, after.reasons[0]) == (status, reason), name
        # The diagnostic's own vocabulary never includes the gateway's decision.
        assert d.headline in FindingKind and "APPROVE" not in render_text(diagnose_records([rec])), name
    # A direct equity control is an INFO finding, while the gateway alone produced the APPROVE above.
    assert codes(diagnose_record(record())) == ["DIRECT_IDENTITY_CONTROL"]


# ----------------------------------------------------------------------------- CLI

def _cli(*args, cwd=None):
    env = {"PYTHONPATH": str(SRC), "PATH": "", "HOME": str(cwd or REPO)}
    return subprocess.run([sys.executable, str(SCRIPT), *args], check=False, capture_output=True, text=True, env=env,
                          cwd=cwd or REPO)


def test_cli_text_summary_json_and_out(tmp_path):
    text = _cli("--examples")
    assert text.returncode == 0 and text.stdout.startswith("INSTRUMENT MAPPING DIAGNOSTIC") and "NON_AUTHORITATIVE" in text.stdout
    summary = _cli("--examples", "--summary")
    assert summary.returncode == 0 and sum(line.startswith("[") for line in summary.stdout.splitlines()) == 24
    as_json = _cli("--examples", "--json")
    assert as_json.returncode == 0
    assert as_json.stdout == (FIXTURES / "expected_report.json").read_text(encoding="utf-8")
    out = tmp_path / "report.json"
    written = _cli(str(FIXTURES / "records.json"), "--json", "--out", str(out), cwd=tmp_path)
    assert written.returncode == 0 and written.stdout == "" and "NON-AUTHORITATIVE" in written.stderr
    assert out.read_text(encoding="utf-8") == as_json.stdout


def test_cli_input_errors_exit_two_without_a_verdict(tmp_path):
    assert _cli().returncode == 2
    assert _cli("--examples", str(FIXTURES / "records.json")).returncode == 2
    missing = _cli(str(tmp_path / "nope.json"))
    assert missing.returncode == 2 and "Cannot read" in missing.stderr and missing.stdout == ""
    unmarked = tmp_path / "unmarked.json"
    unmarked.write_text(json.dumps([{"record_id": "a", "instrument": {"asset_class": "EQUITY", "symbol": "ABC"}}]))
    refused = _cli(str(unmarked))
    assert refused.returncode == 2 and "synthetic" in refused.stderr and refused.stdout == ""
