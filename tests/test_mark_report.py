"""MARK-1: read-only exposure and mark report, on invented ledgers and invented marks only.

Ledgers are written by the existing engines (``PaperEventReplay``, ``SignalPipeline`` + ``Journal``),
read through the existing ``operations_view.build_snapshot``, then valued against supplied invented
quotes. Every expected value is calculated by hand in the test. Each malformed or ambiguous case is
derived from a real snapshot and paired with its valid control.
"""
from __future__ import annotations

import decimal
import hashlib
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tradingbot.event_models import ParsedPost
from tradingbot.event_replay import PaperEventReplay
from tradingbot.interpreters import DemoInterpreter
from tradingbot.mark_report import EXPOSURE_CONVENTION, MarkReport, build_mark_report
from tradingbot.models import Action, AssetClass, MockPolicy, PositionSide, Quote, SourceEvent
from tradingbot.operations_view import (
    Discrepancy,
    MockLedgers,
    _quote,
    _replay_action,
    _replay_source,
    build_snapshot,
    build_synthetic_ledgers,
)
from tradingbot.pipeline import SignalPipeline
from tradingbot.storage import Journal

NOW = datetime(2026, 3, 4, 15, 0, tzinfo=UTC)
POLICY = MockPolicy(quantity=2, quantity_per_add=2)


# ----------------------------------------------------------------------------- invented ledgers


class Replay:
    """Drives the existing replay coordinator at a fixed clock."""

    def __init__(self, tmp_path: Path) -> None:
        self.path = tmp_path / "replay.sqlite3"
        self.engine = PaperEventReplay(self.path, POLICY)

    def run(self, sid: str, bid: str, ask: str, symbol: str, *actions) -> None:
        result = self.engine.process(_replay_source(sid, NOW), ParsedPost(actions=list(actions)),
                                     quotes={symbol: _quote(symbol, bid, ask, NOW)}, now=NOW)
        assert all(o.status == "SIMULATED" for o in result.outcomes), [o.reasons for o in result.outcomes]

    def snapshot(self):
        return build_snapshot(MockLedgers(replay_path=self.path), now=NOW, policy=POLICY)


def open_long(r: Replay, symbol="MOCKL", bid="99.95", ask="100.05", sid="r1") -> str:
    r.run(sid, bid, ask, symbol, _replay_action("open", Action.OPEN, symbol, AssetClass.ETF,
                                                side=PositionSide.LONG, ref="100", stop="95", target="110"))
    return f"{sid}:open"


def add(r: Replay, idea: str, bid: str, ask: str, ref: str, sid: str, symbol="MOCKL") -> None:
    r.run(sid, bid, ask, symbol, _replay_action("add", Action.ADD, symbol, AssetClass.ETF, idea=idea, ref=ref))


def reduce_half(r: Replay, idea: str, bid: str, ask: str, sid: str, symbol="MOCKL") -> None:
    r.run(sid, bid, ask, symbol, _replay_action("trim", Action.REDUCE, symbol, AssetClass.ETF, idea=idea,
                                                fraction="0.5"))


def mark(symbol: str, bid: str, ask: str, observed_at: datetime = NOW) -> Quote:
    return Quote(symbol=symbol, bid=Decimal(bid), ask=Decimal(ask), observed_at=observed_at)


def only(report: MarkReport):
    assert len(report.positions) == 1
    return report.positions[0]


@pytest.fixture
def long_with_add(tmp_path):
    """OPEN 2 @ 100.05, then ADD 2 @ 102.05: no exit, so the basis is order-invariant."""
    r = Replay(tmp_path)
    idea = open_long(r)
    add(r, idea, "101.95", "102.05", "102", "r2")
    return r


# ----------------------------------------------------------------------------- hand calculations


def test_long_with_an_add_is_valued_at_the_bid_against_its_weighted_average_cost(long_with_add):
    report = build_mark_report(long_with_add.snapshot(), {"MOCKL": mark("MOCKL", "103.00", "103.10")}, POLICY)
    row = only(report)
    # basis = (2 x 100.05 + 2 x 102.05) / 4 = 101.05; exposure = 4 x 103.00; P&L = (103.00 - 101.05) x 4
    assert (row.units, row.basis_status, row.average_cost) == (4, "KNOWN", Decimal("101.05"))
    assert (row.signed_exposure, row.gross_exposure) == (Decimal("412.00"), Decimal("412.00"))
    assert row.unrealized_pnl == Decimal("7.80")
    assert (row.stop_distance, row.target_distance) == (Decimal("-8.00"), Decimal("7.00"))  # 95-103, 110-103
    assert row.mark_status == "FRESH" and row.mark_scope == "SUPPLIED_INVENTED"
    assert row.protection == "RECORDED_MOCK_UNVERIFIED" and "not monitored" in row.level_note
    assert report.status == "COMPLETE" and report.exposure_convention == EXPOSURE_CONVENTION


def test_short_is_valued_at_the_ask_with_negative_signed_exposure(tmp_path):
    r = Replay(tmp_path)
    r.run("s1", "49.95", "50.05", "MOCKS", _replay_action("open", Action.OPEN, "MOCKS", AssetClass.EQUITY,
                                                          side=PositionSide.SHORT, ref="50", stop="55", target="40"))
    row = only(build_mark_report(r.snapshot(), {"MOCKS": mark("MOCKS", "48.00", "48.10")}, POLICY))
    # SELL 2 @ 49.95; exposure -(2 x 48.10); P&L = (49.95 - 48.10) x 2
    assert (row.average_cost, row.executable_mark) == (Decimal("49.95"), Decimal("48.10"))
    assert (row.signed_exposure, row.gross_exposure) == (Decimal("-96.20"), Decimal("96.20"))
    assert row.unrealized_pnl == Decimal("3.70")
    assert (row.stop_distance, row.target_distance) == (Decimal("6.90"), Decimal("-8.10"))


def test_a_reduction_removes_units_at_the_existing_average_not_the_exit_price(tmp_path):
    r = Replay(tmp_path)
    idea = open_long(r)
    reduce_half(r, idea, "100.95", "101.05", "r2")  # SELL 1 @ 100.95
    row = only(build_mark_report(r.snapshot(), {"MOCKL": mark("MOCKL", "102.00", "102.10")}, POLICY))
    assert (row.units, row.average_cost, row.unrealized_pnl) == (1, Decimal("100.05"), Decimal("1.95"))


def test_an_add_after_a_reduction_at_a_new_price_has_an_unprovable_basis(tmp_path):
    r = Replay(tmp_path)
    idea = open_long(r)
    reduce_half(r, idea, "100.95", "101.05", "r2")
    add(r, idea, "101.95", "102.05", "102", "r3")
    # In this order the true average is (1 x 100.05 + 2 x 102.05) / 3 = 101.38333..., while entries
    # alone average 101.05. The snapshot does not authenticate fill order, so neither is asserted.
    assert (Decimal("100.05") + 2 * Decimal("102.05")) / 3 != (2 * Decimal("100.05") + 2 * Decimal("102.05")) / 4
    row = only(build_mark_report(r.snapshot(), {"MOCKL": mark("MOCKL", "103.00", "103.10")}, POLICY))
    assert row.basis_status == "UNKNOWN" and "ORDER_DEPENDENT_BASIS_UNPROVEN" in row.basis_reasons
    assert (row.average_cost, row.unrealized_pnl) == (None, None)
    assert row.signed_exposure == Decimal("309.00")  # 3 x 103.00: exposure does not need the basis


def test_control_an_add_after_a_reduction_at_the_same_price_is_order_invariant(tmp_path):
    r = Replay(tmp_path)
    idea = open_long(r)
    reduce_half(r, idea, "100.95", "101.05", "r2")
    add(r, idea, "99.95", "100.05", "100", "r3")
    row = only(build_mark_report(r.snapshot(), {"MOCKL": mark("MOCKL", "101.00", "101.10")}, POLICY))
    assert (row.units, row.average_cost, row.unrealized_pnl) == (3, Decimal("100.05"), Decimal("2.85"))


def test_an_amendment_is_not_a_fill_and_its_new_level_is_reported(tmp_path):
    r = Replay(tmp_path)
    idea = open_long(r)
    r.run("r2", "99.95", "100.05", "MOCKL", _replay_action("tighten", Action.MODIFY, "MOCKL", AssetClass.ETF,
                                                           idea=idea, stop="97"))
    snapshot = r.snapshot()
    assert any(f.status == "SIMULATED_AMENDED" for f in snapshot.fills)
    row = only(build_mark_report(snapshot, {"MOCKL": mark("MOCKL", "101.00", "101.10")}, POLICY))
    assert (row.average_cost, row.unrealized_pnl) == (Decimal("100.05"), Decimal("1.90"))
    assert row.stop_price == "97" and row.stop_distance == Decimal("-4.00")


def test_entry_order_does_not_change_an_order_invariant_basis(long_with_add):
    snapshot = long_with_add.snapshot()
    reversed_fills = snapshot.model_copy(update={"fills": list(reversed(snapshot.fills))})
    marks = {"MOCKL": mark("MOCKL", "103.00", "103.10")}
    assert only(build_mark_report(reversed_fills, marks, POLICY)) == only(build_mark_report(snapshot, marks, POLICY))


def test_a_journal_position_is_valued_and_its_unknown_protection_carried(tmp_path):
    clock = datetime.now(UTC)
    journal = Journal(tmp_path / "journal.sqlite3")
    SignalPipeline(journal, DemoInterpreter(), MockPolicy()).process(
        SourceEvent(source_id="j1", text="BUY MOCKJ @ 100 STOP 95 TARGET 110", published_at=clock),
        _quote("MOCKJ", "99.95", "100.05", clock))
    snapshot = build_snapshot(MockLedgers(journal_path=journal.path), now=clock)
    row = only(build_mark_report(snapshot, {"MOCKJ": mark("MOCKJ", "101.00", "101.10", clock)}, MockPolicy()))
    assert (row.engine, row.units, row.average_cost, row.unrealized_pnl) == ("journal", 1, Decimal("100.05"),
                                                                          Decimal("0.95"))
    assert row.protection == "UNKNOWN" and row.level_reasons == ("STOP_NOT_RECORDED", "TARGET_NOT_RECORDED")


# ----------------------------------------------------------------------------- marks


@pytest.mark.parametrize(("offset", "status"), [
    (timedelta(seconds=-30), "FRESH"), (timedelta(seconds=-31), "STALE"),
    (timedelta(seconds=5), "FRESH"), (timedelta(seconds=6), "FUTURE"),
], ids=["age-at-window", "age-past-window", "future-at-tolerance", "future-past-tolerance"])
def test_mark_freshness_uses_the_existing_window_and_future_tolerance(long_with_add, offset, status):
    row = only(build_mark_report(long_with_add.snapshot(),
                                 {"MOCKL": mark("MOCKL", "103.00", "103.10", NOW + offset)}, POLICY))
    assert row.mark_status == status
    assert (row.signed_exposure is not None) is (status == "FRESH")
    assert (row.unrealized_pnl is not None) is (status == "FRESH")
    assert row.average_cost == Decimal("101.05")  # the basis never depends on the mark


def test_a_missing_mark_is_unknown_never_zero(long_with_add):
    row = only(build_mark_report(long_with_add.snapshot(), {}, POLICY))
    assert (row.mark_status, row.mark_reason, row.exposure_status) == ("UNKNOWN", "MARK_MISSING", "UNKNOWN")
    assert (row.signed_exposure, row.unrealized_pnl, row.stop_distance) == (None, None, None)


@pytest.mark.parametrize(("supplied", "reason"), [
    (mark("MOCKL", "103.00", "103.10", NOW.replace(tzinfo=None)), "MARK_TIME_NOT_TIMEZONE_AWARE"),
    (mark("OTHER", "103.00", "103.10"), "MARK_SYMBOL_MISMATCH"),
    (mark("MOCKL", "103.20", "103.10"), "MARK_BID_ASK_INVALID"),
    (mark("MOCKL", "0", "103.10"), "MARK_BID_ASK_INVALID"),
    (Quote.model_construct(symbol="MOCKL", bid=Decimal("NaN"), ask=Decimal("103.10"), observed_at=NOW),
     "MARK_PRICE_MALFORMED"),
    (Quote.model_construct(symbol="MOCKL", bid=Decimal("1E+30"), ask=Decimal("1E+30"), observed_at=NOW),
     "MARK_PRICE_MALFORMED"),
    ({"bid": "103.00", "ask": "103.10"}, "MARK_NOT_A_QUOTE"),
], ids=["naive-time", "symbol-mismatch", "crossed", "zero-bid", "nan", "unbounded", "not-a-quote"])
def test_a_malformed_mark_is_invalid_and_values_nothing(long_with_add, supplied, reason):
    report = build_mark_report(long_with_add.snapshot(), {"MOCKL": supplied}, POLICY)
    row = only(report)
    assert (row.mark_status, row.mark_reason) == ("INVALID", reason)
    assert (row.signed_exposure, row.unrealized_pnl, row.executable_mark) == (None, None, None)
    assert report.counts["marks_invalid"] == 1


def test_a_naive_snapshot_time_leaves_every_mark_unknown(long_with_add):
    snapshot = long_with_add.snapshot()
    naive = snapshot.model_copy(update={"as_of": NOW.replace(tzinfo=None)})
    report = build_mark_report(naive, {"MOCKL": mark("MOCKL", "103.00", "103.10")}, POLICY)
    assert report.status == "INCOMPLETE" and report.as_of_aware is False
    assert only(report).mark_reason == "AS_OF_NOT_TIMEZONE_AWARE" and only(report).signed_exposure is None


# ----------------------------------------------------------------------------- ledger evidence


def amended(snapshot, *, fills=None, positions=None, **update):
    data = dict(update)
    if fills is not None:
        data["fills"] = fills
    if positions is not None:
        data["positions"] = positions
    return snapshot.model_copy(update=data)


def entry(snapshot, action="ADD"):
    return next(f for f in snapshot.fills if f.action == action)


MARKS = {"MOCKL": mark("MOCKL", "103.00", "103.10")}


@pytest.mark.parametrize(("change", "reason"), [
    (lambda s: amended(s, fills=s.fills + [entry(s).model_copy()]), "DUPLICATE_ORDER_ID"),
    (lambda s: amended(s, fills=[f.model_copy(update={"symbol": "OTHER"}) if f.action == "ADD" else f
                                 for f in s.fills]), "FILL_SYMBOL_MISMATCH"),
    (lambda s: amended(s, fills=[f.model_copy(update={"side": "SELL"}) if f.action == "ADD" else f
                                 for f in s.fills]), "FILL_DIRECTION_MISMATCH"),
    (lambda s: amended(s, fills=s.fills + [entry(s, "OPEN").model_copy(update={"order_id": "x2"})]),
     "REPEATED_OPEN_FILL"),
    (lambda s: amended(s, fills=[f for f in s.fills if f.action != "OPEN"]), "OPEN_FILL_MISSING"),
    (lambda s: amended(s, fills=s.fills + [entry(s).model_copy(update={"order_id": "x3", "action": "CLOSE",
                                                                       "side": "SELL", "quantity": 1})]),
     "CLOSE_FILL_ON_OPEN_POSITION"),
    (lambda s: amended(s, positions=[p.model_copy(update={"quantity": 5}) for p in s.positions]),
     "FILL_QUANTITY_DISAGREES_WITH_POSITION"),
    (lambda s: amended(s, fills=[f.model_copy(update={"status": "SIMULATED_AMENDED"}) if f.action == "ADD" else f
                                 for f in s.fills]), "FILL_RECORD_INVALID"),
    (lambda s: amended(s, fills=s.fills + [entry(s).model_copy(update={
        "order_id": "x4", "action": "MODIFY", "status": "SIMULATED_AMENDED", "side": "NONE", "quantity": 0,
        "price": "1"})]), "AMENDMENT_RECORD_INVALID"),
], ids=["duplicate-order", "symbol", "direction", "repeated-open", "missing-open", "close-on-open",
        "quantity-disagrees", "status", "amendment-with-price"])
def test_an_inconsistent_fill_history_leaves_the_basis_unknown(long_with_add, change, reason):
    row = only(build_mark_report(change(long_with_add.snapshot()), MARKS, POLICY))
    assert row.basis_status == "UNKNOWN" and reason in row.basis_reasons
    assert (row.average_cost, row.unrealized_pnl) == (None, None)


@pytest.mark.parametrize("price", [None, "abc", "1e3", "-1", "0", " 102.05", "1" * 16, "1.12345678901"],
                         ids=["none", "text", "exponent", "negative", "zero", "space", "too-many-digits",
                              "too-many-places"])
def test_an_unparseable_fill_price_leaves_the_basis_unknown(long_with_add, price):
    s = long_with_add.snapshot()
    changed = amended(s, fills=[f.model_copy(update={"price": price}) if f.action == "ADD" else f for f in s.fills])
    row = only(build_mark_report(changed, MARKS, POLICY))
    assert "FILL_PRICE_INVALID" in row.basis_reasons and row.average_cost is None


def test_incomplete_reconciliation_or_any_engine_discrepancy_leaves_the_basis_unknown(long_with_add):
    s = long_with_add.snapshot()
    incomplete = build_mark_report(amended(s, reconciliation_complete=False), MARKS, POLICY)
    assert incomplete.status == "INCOMPLETE"
    assert "RECONCILIATION_INCOMPLETE" in only(incomplete).basis_reasons
    assert only(incomplete).signed_exposure == Decimal("412.00")  # position units are read directly

    warning = Discrepancy(engine="replay", code="INVENTED_WARNING", severity="WARNING", subject="elsewhere",
                          detail="invented")
    row = only(build_mark_report(amended(s, discrepancies=[warning]), MARKS, POLICY))
    assert "ENGINE_DISCREPANCY_PRESENT" in row.basis_reasons and row.signed_exposure == Decimal("412.00")

    naming = warning.model_copy(update={"subject": s.positions[0].idea_id})
    row = only(build_mark_report(amended(s, discrepancies=[naming]), MARKS, POLICY))
    assert "DISCREPANCY_FOR_POSITION" in row.exposure_reasons and row.signed_exposure is None


def test_the_populated_synthetic_ledgers_keep_basis_unknown_behind_their_warnings(tmp_path):
    ledgers = build_synthetic_ledgers(tmp_path, "populated", now=NOW)
    snapshot = build_snapshot(ledgers, now=NOW, policy=POLICY)
    assert snapshot.reconciliation_complete is True and snapshot.discrepancies  # complete is not clean
    report = build_mark_report(snapshot, {}, POLICY)
    assert report.positions and all(r.basis_status == "UNKNOWN" for r in report.positions)
    assert all("ENGINE_DISCREPANCY_PRESENT" in r.basis_reasons for r in report.positions)


def test_a_duplicate_position_key_is_never_valued(long_with_add):
    s = long_with_add.snapshot()
    report = build_mark_report(amended(s, positions=s.positions + [s.positions[0].model_copy()]), MARKS, POLICY)
    assert len(report.positions) == 2
    assert all("DUPLICATE_POSITION" in r.exposure_reasons and r.signed_exposure is None for r in report.positions)


def test_a_derivative_position_is_unsupported_not_valued(long_with_add):
    s = long_with_add.snapshot()
    option = amended(s, positions=[p.model_copy(update={"asset_class": "OPTION"}) for p in s.positions])
    row = only(build_mark_report(option, MARKS, POLICY))
    assert (row.exposure_status, row.basis_status) == ("UNSUPPORTED", "UNSUPPORTED")
    assert (row.signed_exposure, row.average_cost) == (None, None)


@pytest.mark.parametrize("level", ["abc", "-5", "0", "95.123456789012"], ids=["text", "negative", "zero", "precision"])
def test_an_invalid_recorded_level_has_an_unknown_distance_and_protection_is_unchanged(long_with_add, level):
    s = long_with_add.snapshot()
    changed = amended(s, positions=[p.model_copy(update={"stop_price": level}) for p in s.positions])
    row = only(build_mark_report(changed, MARKS, POLICY))
    assert "STOP_LEVEL_INVALID" in row.level_reasons and row.stop_distance is None
    assert row.target_distance == Decimal("7.00") and row.protection == s.positions[0].protection


# ----------------------------------------------------------------------------- read-only and surface


def test_inputs_ledgers_and_the_decimal_context_are_unchanged(long_with_add):
    digest = hashlib.sha256(long_with_add.path.read_bytes()).hexdigest()
    snapshot = long_with_add.snapshot()
    before_snapshot, before_marks = snapshot.model_dump(), {k: v.model_dump() for k, v in MARKS.items()}
    context_before = decimal.getcontext().copy()
    build_mark_report(snapshot, MARKS, POLICY)
    assert hashlib.sha256(long_with_add.path.read_bytes()).hexdigest() == digest
    assert snapshot.model_dump() == before_snapshot
    assert {k: v.model_dump() for k, v in MARKS.items()} == before_marks
    current = decimal.getcontext()
    assert (current.prec, current.rounding, current.Emax) == (context_before.prec, context_before.rounding,
                                                             context_before.Emax)


def test_the_report_has_no_monetary_total_and_no_authority_vocabulary(long_with_add):
    report = build_mark_report(long_with_add.snapshot(), MARKS, POLICY)
    assert report.authority == "NON_AUTHORITATIVE" and report.trading_permission is False
    assert all(type(v) is int for v in report.counts.values())
    text = report.model_dump_json().lower()
    for word in ("approve", "execute", "eligible", "trigger", "alert", "protected", "usd"):
        assert word not in text, word


@pytest.mark.parametrize("args", [(None, {}, POLICY), ("snapshot", {}, POLICY)], ids=["none", "text"])
def test_only_an_operations_snapshot_is_accepted(args):
    with pytest.raises(TypeError):
        build_mark_report(*args)


def test_fills_join_by_engine_and_idea_so_another_engine_or_an_orphan_never_contaminates(long_with_add):
    s = long_with_add.snapshot()
    other_engine = entry(s).model_copy(update={"engine": "journal", "order_id": "j-x", "price": "1.00"})
    orphan = entry(s).model_copy(update={"idea_id": "no-such-idea", "order_id": "o-x", "price": "1.00"})
    report = build_mark_report(amended(s, fills=s.fills + [other_engine, orphan]), MARKS, POLICY)
    row = only(report)
    assert (row.engine, row.average_cost, row.unrealized_pnl) == ("replay", Decimal("101.05"), Decimal("7.80"))
