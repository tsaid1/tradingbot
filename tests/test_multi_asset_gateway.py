"""The one gateway on mapped families: every refusal reason and each basis treatment.

``validator.gate_action`` with a ``MappedQuote`` built from invented terms and quotes. No broker.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from _risk_fixture import INVENTED_RISK, market_terms_fields

from tradingbot.event_models import PriceBasis, Timing, TradeAction
from tradingbot.models import (
    Action,
    AssetClass,
    Instrument,
    LegQuote,
    MappedQuote,
    MarketTerms,
    MockPolicy,
    Position,
    PositionSide,
    Quote,
)
from tradingbot.validator import business_days_until, fill_quantity, gate_action

NOW = datetime(2026, 6, 1, 14, 0, tzinfo=UTC)  # Monday
# Futures need an owner's [risk] table (docs/RISK_GATES.md): an INVENTED, deliberately wide one.
POLICY = MockPolicy(max_notional_usd=Decimal(100000), risk=INVENTED_RISK)
FUT = Instrument(asset_class=AssetClass.FUTURE, symbol="QXF", exchange="SIMX",
                 futures_contract="202609")
KEY = FUT.quote_key


def terms(basis="DIRECT", **kw) -> MarketTerms:
    base = {
        "family": "QFAM", "approved": True, "basis": basis, "execution_symbol": "QXF",
        "execution_key": KEY, "execution_contract_key": "FUT:QXF:20260918:-:-:77",
        "asset_class": AssetClass.FUTURE, "multiplier": Decimal(5), "min_tick": Decimal("0.25"),
        "units_per_signal": 2, "max_units": 4, "max_notional": Decimal(60000),
        "permitted_windows": [(13 * 60, 21 * 60)], "roll_date": date(2026, 9, 11),
        "roll_business_days": 3,
    }
    if basis in ("BASIS_SHIFT", "REFERENCE_TRIGGER"):
        base.update({
            "reference_contract_key": "IND:QREF:-:-:-:78",
            "reference_contract": '{"symbol":"QREF","asset_class":"INDEX","sec_type":"IND"}',
            "reference_windows": [(13 * 60 + 30, 20 * 60)],
            "max_abs_basis": Decimal(40), "max_basis_age_seconds": 5,
        })
    base.update(kw)
    base = {**market_terms_fields(base["asset_class"], base["multiplier"]), **base}
    return MarketTerms(**base)


def leg(bid="5009.5", ask="5010.5", last="5010", *, age=0.0, quality="LIVE", at=NOW):
    # The series' own update time, and each side's, is the receipt time here (a live, moving
    # series).
    seen = at - timedelta(seconds=age)
    return LegQuote(contract_key="IND:QREF:-:-:-:78", bid=Decimal(bid) if bid else None,
                    ask=Decimal(ask) if ask else None, last=Decimal(last) if last else None,
                    observed_at=seen, quality=quality, updated_at=seen, bid_at=seen, ask_at=seen)


def mq(t: MarketTerms, bid="5024.75", ask="5025.25", *, age=0.0, quality="LIVE",
       reference: LegQuote | None = None, at=NOW) -> MappedQuote:
    seen = at - timedelta(seconds=age)
    return MappedQuote(symbol="QXF", bid=Decimal(bid), ask=Decimal(ask), observed_at=seen,
                       quality=quality, terms=t, reference=reference, bid_at=seen, ask_at=seen)


def act(action=Action.OPEN, *, entry="5025", stop="4995", target="5085", side=PositionSide.LONG,
        idea=None, family="QFAM", basis=PriceBasis.TRADED_CONTRACT, instrument=FUT,
        fraction=None) -> TradeAction:
    return TradeAction(
        action_id="a1", action=action, timing=Timing.CURRENT, instrument=instrument,
        position_side=side, idea_reference=idea, price_basis=basis, source_market_symbol=family,
        entry_reference=entry if action in (Action.OPEN, Action.ADD) else None,
        stop_price=stop if action in (Action.OPEN, Action.MODIFY) else None,
        target_price=target if action in (Action.OPEN, Action.MODIFY) else None,
        close_fraction=fraction, explicit_instruction=True, evidence=["invented"],
    )


def position(qty=2, stop="4995", contract_key="FUT:QXF:20260918:-:-:77", side=PositionSide.LONG):
    return Position(idea_id="9:a1", symbol="QXF", asset_class=AssetClass.FUTURE, side=side,
                    quantity=qty, stop_price=stop, contract_key=contract_key)


def gate(a, q, pos=None, now=NOW, policy=POLICY):
    return gate_action(a, now - timedelta(seconds=2), q, policy, pos, now=now)


# ------------------------------------------------------------------ approval and identity


def test_unapproved_mapping_holds():
    g = gate(act(), mq(terms(approved=False)))
    assert g.status == "HOLD" and g.reasons[0] == "MAPPING_NOT_APPROVED" and g.plan is None


def test_unsupported_treatment_holds():
    g = gate(act(), mq(terms(basis="UNSUPPORTED")))
    assert g.status == "HOLD" and g.reasons[0] == "UNSUPPORTED_INSTRUMENT_MAPPING"


@pytest.mark.parametrize(
    "problem", ["CONTRACT_UNRESOLVED", "CONTRACT_SPEC_MISMATCH:MULTIPLIER:5!=50",
                "REFERENCE_SERIES_UNRESOLVED"]
)
def test_problems_found_while_building_terms_hold(problem):
    g = gate(act(), mq(terms(problems=[problem])))
    assert g.status == "HOLD" and g.reasons == [problem]


def test_family_and_instrument_must_be_the_mapped_ones():
    assert gate(act(family="OTHER"), mq(terms())).reasons == ["SOURCE_TO_BROKER_MAPPING_UNAPPROVED"]
    assert gate(act(basis=PriceBasis.UNKNOWN), mq(terms())).reasons == [
        "SOURCE_TO_BROKER_PRICE_BASIS_UNRESOLVED"]
    other_month = FUT.model_copy(update={"futures_contract": "202612"})
    g = gate(act(instrument=other_month), mq(terms()))
    assert g.status == "BLOCK" and g.reasons == ["QUOTE_INSTRUMENT_MISMATCH"]


def test_a_plain_quote_keeps_a_future_on_the_unchanged_path():
    q = Quote(symbol="QXF", bid=Decimal("5024.75"), ask=Decimal("5025.25"), observed_at=NOW)
    g = gate(act(family="QXF"), q)
    assert g.status == "HOLD" and g.reasons == ["ASSET_CLASS_PAPER_ADAPTER_UNSUPPORTED"]


# ------------------------------------------------------------------ session, roll, contract


def test_outside_permitted_session_holds_trading_but_not_an_amendment():
    late = NOW.replace(hour=22)
    q = mq(terms(), at=late)
    assert gate(act(), q, now=late).reasons == ["OUTSIDE_PERMITTED_SESSION"]
    saturday = NOW + timedelta(days=5)
    assert gate(act(), mq(terms(), at=saturday), now=saturday).reasons == [
        "OUTSIDE_PERMITTED_SESSION"]
    exit_ = act(Action.CLOSE, idea="9:a1")
    assert gate(exit_, q, position(), now=late).reasons == ["OUTSIDE_PERMITTED_SESSION"]
    modify = act(Action.MODIFY, idea="9:a1", stop="5000", target=None)
    assert gate(modify, q, position(), now=late).status == "APPROVE"


def test_contract_in_roll_window_refuses_new_exposure_only():
    t = terms(roll_date=date(2026, 6, 4))  # three business days after Monday 1 June
    assert business_days_until(date(2026, 6, 1), date(2026, 6, 4)) == 3
    g = gate(act(), mq(t))
    assert g.status == "HOLD" and g.reasons[0] == "CONTRACT_IN_ROLL_WINDOW"
    add = act(Action.ADD, idea="9:a1", entry="5025")
    assert gate(add, mq(t), position()).reasons[0] == "CONTRACT_IN_ROLL_WINDOW"
    close = act(Action.CLOSE, idea="9:a1")
    assert gate(close, mq(t), position()).status == "APPROVE"  # an exit is always allowed
    assert gate(act(), mq(terms(roll_date=date(2026, 6, 5)))).status == "APPROVE"


def test_business_days_skip_weekends_and_holidays():
    assert business_days_until(date(2026, 6, 5), date(2026, 6, 8)) == 1  # Fri -> Mon
    assert business_days_until(date(2026, 6, 1), date(2026, 6, 8), [date(2026, 6, 3)]) == 4
    assert business_days_until(date(2026, 6, 8), date(2026, 6, 1)) == -5


def test_follow_up_in_another_contract_month_blocks():
    g = gate(act(Action.CLOSE, idea="9:a1"), mq(terms()),
             position(contract_key="FUT:QXF:20261218:-:-:79"))
    assert g.status == "BLOCK" and g.reasons == ["IDEA_CONTRACT_MISMATCH"]


# ------------------------------------------------------------------ DIRECT


def test_direct_entry_checks_in_the_execution_series_and_plans_its_prices():
    g = gate(act(), mq(terms()))
    assert g.status == "APPROVE" and g.reasons == ["PAPER_OPEN", "MAPPING_DIRECT"]
    p = g.plan
    assert p.quantity == 2 and p.execution_reference == Decimal(5025)
    assert (p.stop, p.target, p.protection_series) == (Decimal(4995), Decimal(5085), "EXECUTION")
    assert fill_quantity(Action.OPEN, POLICY, None, per_signal=p.quantity) == 2


@pytest.mark.parametrize(
    "kw,reason",
    [
        ({"entry": "4900", "stop": "4880", "target": "4950"}, "ENTRY_PRICE_COLLAR"),
        ({"stop": "4995.10"}, "STOP_PRICE_OFF_TICK"),
        ({"stop": "5030"}, "STOP_DIRECTION_CONTRADICTION"),
        ({"entry": "5050", "stop": "5025"}, "STOP_ALREADY_CROSSED_AT_ENTRY"),
    ],
)
def test_direct_entry_refusals(kw, reason):
    g = gate(act(**kw), mq(terms()))
    assert g.status == "BLOCK" and reason in g.reasons


def test_notional_uses_the_multiplier_and_the_family_cap():
    # 5025.25 x 2 x 5 = 50,252.50: inside 60,000, outside 50,000.
    assert gate(act(), mq(terms(max_notional=Decimal(60000)))).status == "APPROVE"
    g = gate(act(), mq(terms(max_notional=Decimal(50000))))
    assert g.status == "BLOCK" and g.reasons == ["NOTIONAL_LIMIT"]
    small = POLICY.model_copy(update={"max_notional_usd": Decimal(50000)})
    assert gate(act(), mq(terms()), policy=small).reasons == ["NOTIONAL_LIMIT"]


def test_add_respects_the_unit_cap():
    add = act(Action.ADD, idea="9:a1", entry="5025")
    t = terms(units_per_signal=1, max_units=2)
    assert gate(add, mq(t), position(qty=1)).status == "APPROVE"
    assert gate(add, mq(t), position(qty=2)).reasons == ["UNIT_CAP"]
    # (2 + 2) x 5,025.25 x 5 is over the family cap even within the unit cap.
    assert gate(add, mq(terms()), position(qty=2)).reasons == ["NOTIONAL_LIMIT"]


def test_reduction_must_be_whole_size_increments():
    red = act(Action.REDUCE, idea="9:a1", fraction="0.5")
    assert gate(red, mq(terms(size_increment=2)), position(qty=2)).reasons == [
        "REDUCTION_OFF_SIZE_INCREMENT"]
    assert gate(red, mq(terms()), position(qty=2)).status == "APPROVE"


@pytest.mark.parametrize(
    "quote,reason",
    [
        ({"quality": "DELAYED"}, "QUOTE_QUALITY_DELAYED"),
        ({"quality": "UNAVAILABLE"}, "QUOTE_UNAVAILABLE"),
        ({"age": 60}, "QUOTE_STALE_OR_FUTURE"),
        ({"bid": "5030", "ask": "5025"}, "INVALID_BID_ASK"),
        ({"bid": "4900", "ask": "5025.25"}, "SPREAD_TOO_WIDE"),
    ],
)
def test_execution_quote_must_be_live_fresh_and_sane(quote, reason):
    g = gate(act(), mq(terms(), **quote))
    assert g.status == "BLOCK" and g.reasons == [reason]


def test_an_option_cannot_be_opened_short():
    g = gate(act(side=PositionSide.SHORT, stop="5060", target="4990"),
             mq(terms(allow_short=False)))
    assert g.status == "HOLD" and g.reasons == ["SHORT_NOT_PERMITTED_FOR_FAMILY"]


# ------------------------------------------------------------------ BASIS_SHIFT


def test_basis_shift_converts_levels_with_the_observed_basis_and_records_it():
    # Spot 1850 (mid), future 1862 (mid): basis +12; tick 0.10.
    t = terms("BASIS_SHIFT", min_tick=Decimal("0.10"), max_abs_basis=Decimal(20))
    q = mq(t, bid="1861.9", ask="1862.1", reference=leg("1849.9", "1850.1", None))
    g = gate(act(entry="1850", stop="1835.03", target="1880.07"), q)
    assert g.status == "APPROVE" and "MAPPING_BASIS_SHIFT" in g.reasons
    p = g.plan
    assert p.basis_value == Decimal("12.0") and p.basis_reference_mid == Decimal("1850.0")
    assert p.execution_reference == Decimal("1862.0")
    # Converted protection: the stop snapped up (tighter for a long), the target down (nearer).
    assert p.stop == Decimal("1847.1") and p.target == Decimal("1892.0")
    assert p.publisher_stop == Decimal("1835.03")  # the reference level is kept, never sent


def test_basis_shift_short_snaps_the_other_way():
    t = terms("BASIS_SHIFT", min_tick=Decimal("0.10"), max_abs_basis=Decimal(20))
    q = mq(t, bid="1861.9", ask="1862.1", reference=leg("1849.9", "1850.1", None))
    g = gate(act(side=PositionSide.SHORT, entry="1850", stop="1865.07", target="1820.03"), q)
    assert g.status == "APPROVE"
    assert g.plan.stop == Decimal("1877.0") and g.plan.target == Decimal("1832.1")


def test_basis_over_its_cap_holds():
    t = terms("BASIS_SHIFT", min_tick=Decimal("0.10"), max_abs_basis=Decimal(20))
    q = mq(t, bid="1879.9", ask="1880.1", reference=leg("1849.9", "1850.1", None))
    g = gate(act(entry="1850", stop="1835", target="1880"), q)
    assert g.status == "HOLD" and g.reasons == ["BASIS_EXCEEDS_CAP", "BASIS:30.0"]


@pytest.mark.parametrize(
    "execution,reference,reason",
    [
        ({"quality": "DELAYED"}, {}, "EXECUTION_QUOTE_QUALITY_DELAYED"),
        ({"age": 8}, {}, "EXECUTION_QUOTE_STALE"),
        ({}, {"quality": "DELAYED"}, "REFERENCE_QUOTE_QUALITY_DELAYED"),
        ({}, {"quality": "FROZEN"}, "REFERENCE_QUOTE_QUALITY_FROZEN"),
        ({}, {"age": 8}, "REFERENCE_QUOTE_STALE"),
        ({}, None, "REFERENCE_QUOTE_UNAVAILABLE"),
        ({"age": 4.5}, {"age": -1}, "QUOTES_NOT_SIMULTANEOUS"),
    ],
)
def test_basis_shift_entry_holds_on_any_unusable_leg(execution, reference, reason):
    t = terms("BASIS_SHIFT", min_tick=Decimal("0.10"), max_abs_basis=Decimal(20))
    ref = None if reference is None else leg("1849.9", "1850.1", None, **reference)
    q = mq(t, bid="1861.9", ask="1862.1", reference=ref, **execution)
    g = gate(act(entry="1850", stop="1835", target="1880"), q)
    if execution.get("quality") == "DELAYED":
        # A delayed execution quote is refused by the execution-quote rule itself, before any
        # basis is computed; either way nothing is converted from it.
        assert g.status == "BLOCK" and g.reasons == ["QUOTE_QUALITY_DELAYED"]
        return
    # A stale reference is also named as not fresh (review F5).
    fresh = ["REFERENCE_QUOTE_NOT_FRESH"] if reason == "REFERENCE_QUOTE_STALE" else []
    assert g.status == "HOLD" and g.reasons == ["BASIS_QUOTE_UNUSABLE", *fresh, reason]


def test_basis_shift_amendment_is_converted_at_its_own_decision():
    t = terms("BASIS_SHIFT", min_tick=Decimal("0.10"), max_abs_basis=Decimal(20))
    q = mq(t, bid="1863.9", ask="1864.1", reference=leg("1849.9", "1850.1", None))  # basis 14
    g = gate(act(Action.MODIFY, idea="9:a1", stop="1842", target=None), q, position(stop="1847.0"))
    assert g.status == "APPROVE" and g.plan.stop == Decimal("1856.0")
    assert g.plan.basis_value == Decimal("14.0")
    widen = act(Action.MODIFY, idea="9:a1", stop="1830", target=None)
    assert gate(widen, q, position(stop="1847.0")).reasons == ["STOP_WIDENING_REQUIRES_POLICY_REVIEW"]


# ------------------------------------------------------------------ REFERENCE_TRIGGER


def ref_terms(**kw):
    return terms("REFERENCE_TRIGGER", **kw)


def test_reference_trigger_prices_the_entry_from_the_execution_quote():
    g = gate(act(entry="5010", stop="4980", target="5070"), mq(ref_terms(), reference=leg()))
    assert g.status == "APPROVE" and "MAPPING_REFERENCE_TRIGGER" in g.reasons
    p = g.plan
    # The reference level 5010 is never an execution price: the entry is the future's own ask.
    assert p.execution_reference == Decimal("5025.25") and p.entry_reference == Decimal(5010)
    assert (p.stop, p.target, p.protection_series) == (Decimal(4980), Decimal(5070), "REFERENCE")
    assert p.reference_contract_key == "IND:QREF:-:-:-:78" and p.backstop_stop is None


def test_reference_trigger_collar_and_stop_side_use_the_reference_quote():
    q = mq(ref_terms(), reference=leg())
    # 5025 is the future's price but 15 points away from where the index trades: collar is fine
    # on the future yet the publisher's entry is judged on the index.
    assert gate(act(entry="5100", stop="4980", target="5170"), q).reasons == ["ENTRY_PRICE_COLLAR"]
    g = gate(act(entry="5010", stop="5009.75", target="5070"), q)
    assert g.status == "BLOCK" and g.reasons == ["STOP_ALREADY_CROSSED_AT_ENTRY"]


@pytest.mark.parametrize(
    "reference,reason",
    [(None, "REFERENCE_QUOTE_UNAVAILABLE"), ({"age": 60}, "REFERENCE_QUOTE_STALE"),
     ({"quality": "DELAYED"}, "REFERENCE_QUOTE_QUALITY_DELAYED")],
)
def test_reference_trigger_needs_a_usable_reference_quote(reference, reason):
    ref = None if reference is None else leg(**reference)
    g = gate(act(entry="5010", stop="4980", target="5070"), mq(ref_terms(), reference=ref))
    fresh = ["REFERENCE_QUOTE_NOT_FRESH"] if reason == "REFERENCE_QUOTE_STALE" else []
    assert g.status == "HOLD" and g.reasons == [*fresh, reason]


def test_reference_trigger_entry_outside_reference_hours_holds():
    early = NOW.replace(hour=13, minute=10)  # inside the session, before the index updates
    g = gate(act(entry="5010", stop="4980", target="5070"),
             mq(ref_terms(), reference=leg(at=early), at=early), now=early)
    assert g.status == "HOLD" and g.reasons == ["REFERENCE_SERIES_NOT_UPDATING"]


def test_reference_trigger_backstop_is_basis_shifted_in_the_execution_instrument():
    g = gate(act(entry="5010", stop="4980.1", target="5070"),
             mq(ref_terms(require_backstop=True), reference=leg()))
    assert g.status == "APPROVE"
    assert g.plan.stop == Decimal("4980.1")  # the condition stays in index points
    assert g.plan.basis_value == Decimal("15.00") and g.plan.backstop_stop == Decimal("4995.25")
    wide = mq(ref_terms(require_backstop=True, max_abs_basis=Decimal(10)), reference=leg())
    g = gate(act(entry="5010", stop="4980", target="5070"), wide)
    assert g.status == "HOLD" and g.reasons[:2] == ["BACKSTOP_UNPRICEABLE", "BASIS_EXCEEDS_CAP"]


def test_reference_trigger_amendment_is_checked_against_the_reference():
    q = mq(ref_terms(), reference=leg())
    pos = position(stop="4980")
    assert gate(act(Action.MODIFY, idea="9:a1", stop="4995", target=None), q, pos).plan.stop == \
        Decimal(4995)
    # 5015 is below the future's bid but above the index: on the wrong side of its own series.
    g = gate(act(Action.MODIFY, idea="9:a1", stop="5015", target=None), q, pos)
    assert g.status == "BLOCK" and g.reasons == ["STOP_ON_WRONG_SIDE_OF_MARKET"]
