"""entry_rule = REFERENCE_AS_COLLAR and level_contract_rule = FRONT_MONTH (both OFF by default).

Every acceptance and every refusal reason, with invented families, levels, quotes and posts only:
the gateway on a ``MappedQuote`` (no broker), the mapping policy's anchor and front-month
decisions, the loader, the shipped configuration (unchanged), and the whole service path on the
strict simulated broker.
"""

from __future__ import annotations

import re
import tomllib
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from test_multi_asset_gateway import NOW, act, gate, leg, mq, position, terms

from tradingbot.event_models import ParsedPost, PriceBasis, Timing, TradeAction
from tradingbot.models import Action, AssetClass, Instrument, MockPolicy, PositionSide, Quote
from tradingbot.runtime.mapping import FamilyMapping, MappingPolicy
from tradingbot.runtime.mock_multi_asset import (
    QIDX,
    QIF_JUN,
    QOF_JUL,
    STATIC,
    MockMultiAssetRun,
    Scenario,
    Step,
)
from tradingbot.validator import gate_action

REPO = Path(__file__).resolve().parents[1]
SHIPPED = REPO / "config" / "instrument_mapping.toml"
RULE = {"entry_rule": "REFERENCE_AS_COLLAR", "anchor_max_adverse_move": "0.2%",
        "anchor_max_favourable_move": "0.5%", "anchor_max_signal_age_seconds": 30}


def anchored(anchor="5025", **kw):
    """An OPEN (or ADD) with no explicit entry price, and its quote carrying the anchor."""
    return act(entry=None, **kw), Decimal(anchor)


def q_with(t, anchor, **kw):
    return mq(t, **kw).model_copy(update={"entry_anchors": {"a1": Decimal(anchor)}})


def ref_terms(basis, **kw):
    return terms(basis, **{**RULE, **kw})


# ================================================================== the gateway


def test_direct_anchored_open_is_a_limit_at_the_ask_with_the_anchor_recorded():
    a, _ = anchored()
    g = gate(a, q_with(terms(**RULE), "5025"))
    assert g.status == "APPROVE", g.reasons
    assert "REFERENCE_AS_COLLAR" in g.reasons
    p = g.plan
    assert p.entry_rule == "REFERENCE_AS_COLLAR" and p.entry_anchor == Decimal(5025)
    assert p.anchor_series == "EXECUTION" and p.anchor_live == Decimal("5025.25")
    assert p.entry_limit == Decimal("5025.25") == p.execution_reference
    assert p.entry_reference is None  # the publisher's level is an anchor, never an entry
    assert p.stop == Decimal(4995) and p.quantity == 2


def test_a_short_enters_at_the_bid():
    a = act(entry=None, side=PositionSide.SHORT, stop="5055", target="4965")
    g = gate(a, q_with(terms(**RULE), "5025"))
    assert g.status == "APPROVE", g.reasons
    assert g.plan.entry_limit == Decimal("5024.75") and g.plan.anchor_live == Decimal("5024.75")


def test_an_off_tick_quote_is_snapped_never_past_the_touch():
    t = terms(**RULE)
    long = gate(act(entry=None), q_with(t, "5025", bid="5024.80", ask="5025.30"))
    assert long.plan.entry_limit == Decimal("5025.25")  # a buy limit down, never above the ask
    short = gate(act(entry=None, side=PositionSide.SHORT, stop="5055", target="4965"),
                 q_with(t, "5025", bid="5024.80", ask="5025.30"))
    assert short.plan.entry_limit == Decimal("5025.00")  # a sell limit up, never below the bid


@pytest.mark.parametrize("side,anchor", [(PositionSide.LONG, "5010"),
                                         (PositionSide.SHORT, "5040")])
def test_chasing_is_refused(side, anchor):
    long = side == PositionSide.LONG
    a = act(entry=None, side=side, stop="4980" if long else "5070",
            target="5085" if long else "4965")
    g = gate(a, q_with(terms(**RULE), anchor))
    assert g.status == "HOLD" and g.reasons[0] == "ENTRY_ANCHOR_ADVERSE_MOVE" and g.plan is None


@pytest.mark.parametrize("side,anchor", [(PositionSide.LONG, "5060"),
                                         (PositionSide.SHORT, "4990")])
def test_a_favourable_overshoot_is_refused(side, anchor):
    long = side == PositionSide.LONG
    a = act(entry=None, side=side, stop="4990" if long else "5070",
            target="5100" if long else "4900")
    if not long:
        a = a.model_copy(update={"stop_price": "5005"})
    g = gate(a, q_with(terms(**RULE), anchor))
    assert g.status == "HOLD" and g.reasons[0] == "ENTRY_ANCHOR_FAVOURABLE_MOVE"


def test_absolute_limits_are_in_the_anchors_own_price_units():
    t = terms(**{**RULE, "anchor_max_adverse_move": "5", "anchor_max_favourable_move": "5"})
    assert gate(act(entry=None), q_with(t, "5021")).status == "APPROVE"  # 4.25 adverse
    assert gate(act(entry=None), q_with(t, "5019")).reasons[0] == "ENTRY_ANCHOR_ADVERSE_MOVE"
    assert gate(act(entry=None), q_with(t, "5031")).reasons[0] == "ENTRY_ANCHOR_FAVOURABLE_MOVE"


def test_a_signal_older_than_the_rule_allows_is_refused():
    t = terms(**{**RULE, "anchor_max_signal_age_seconds": 1})
    g = gate(act(entry=None), q_with(t, "5025"))  # published 2 s ago, inside the policy's 90 s
    assert g.status == "HOLD" and g.reasons[0] == "ENTRY_ANCHOR_SIGNAL_TOO_OLD"


@pytest.mark.parametrize("quality,age,reason", [
    ("DELAYED", 0, "QUOTE_QUALITY_DELAYED"), ("FROZEN", 0, "QUOTE_QUALITY_FROZEN"),
    ("LIVE", 60, "QUOTE_STALE_OR_FUTURE"), ("UNAVAILABLE", 0, "QUOTE_UNAVAILABLE"),
])
def test_an_unusable_execution_quote_never_trades(quality, age, reason):
    g = gate(act(entry=None), q_with(terms(**RULE), "5025", quality=quality, age=age))
    assert g.status in ("BLOCK", "HOLD") and g.reasons == [reason]


@pytest.mark.parametrize("basis", ["BASIS_SHIFT", "REFERENCE_TRIGGER"])
@pytest.mark.parametrize("reference,reasons", [
    (leg(quality="DELAYED"), ["REFERENCE_QUOTE_QUALITY_DELAYED"]),
    (leg(quality="FROZEN"), ["REFERENCE_QUOTE_QUALITY_FROZEN"]),
    (leg(age=60), ["REFERENCE_QUOTE_NOT_FRESH", "REFERENCE_QUOTE_STALE"]),
    (leg().model_copy(update={"updated_at": None}),
     ["REFERENCE_QUOTE_NOT_FRESH", "REFERENCE_QUOTE_UPDATE_TIME_UNKNOWN"]),
    (None, ["REFERENCE_QUOTE_UNAVAILABLE"]),
])
def test_an_unusable_reference_quote_holds_the_anchor_comparison(basis, reference, reasons):
    g = gate(act(entry=None, stop="4980"), q_with(ref_terms(basis), "5010", reference=reference))
    assert g.status == "HOLD" and g.reasons == reasons and g.plan is None


def test_the_anchor_is_compared_in_its_own_series():
    """The same anchor and quotes: in execution prices (DIRECT) the market has run 15 points away;
    in reference prices (BASIS_SHIFT) it has not moved."""
    a = act(entry=None, stop="4980", target="5070")
    direct = gate(a, q_with(terms(**RULE), "5010"))
    assert direct.reasons[0] == "ENTRY_ANCHOR_ADVERSE_MOVE"
    shifted = gate(a, q_with(ref_terms("BASIS_SHIFT"), "5010", reference=leg()))
    assert shifted.status == "APPROVE", shifted.reasons
    p = shifted.plan
    assert p.anchor_series == "REFERENCE" and p.anchor_live == Decimal("5010.5")
    assert p.entry_limit == Decimal("5025.25") and p.basis_value == Decimal(15)
    assert p.stop == Decimal(4995)  # the publisher's 4980 shifted by the observed basis


def test_reference_trigger_anchor_keeps_protection_in_the_reference_series():
    t = ref_terms("REFERENCE_TRIGGER")
    g = gate(act(entry=None, stop="4980", target="5070"), q_with(t, "5010", reference=leg()))
    assert g.status == "APPROVE", g.reasons
    p = g.plan
    assert p.protection_series == "REFERENCE" and p.stop == Decimal(4980)
    assert p.entry_limit == p.execution_reference == Decimal("5025.25")
    assert p.anchor_series == "REFERENCE"
    early = NOW.replace(hour=13, minute=10)  # inside the session, before the reference hours
    outside = gate(act(entry=None, stop="4980"), q_with(t, "5010", reference=leg(at=early),
                                                         at=early), now=early)
    assert outside.reasons == ["REFERENCE_SERIES_NOT_UPDATING"]


@pytest.mark.parametrize("basis", ["DIRECT", "BASIS_SHIFT", "REFERENCE_TRIGGER"])
def test_the_stop_is_still_required_and_checked_in_the_right_series(basis):
    extra = {} if basis == "DIRECT" else {"reference": leg()}
    t = ref_terms(basis) if basis != "DIRECT" else terms(**RULE)
    anchor = "5025" if basis == "DIRECT" else "5010"
    missing = gate(act(entry=None, stop=None), q_with(t, anchor, **extra))
    assert missing.reasons == ["PROTECTIVE_STOP_MISSING"]
    wrong_side = gate(act(entry=None, stop=str(Decimal(anchor) + 5)), q_with(t, anchor, **extra))
    assert wrong_side.reasons == ["STOP_DIRECTION_CONTRADICTION"]


def test_a_stop_already_crossed_by_the_live_market_is_refused():
    g = gate(act(entry=None, stop="5026", target="5090"), q_with(terms(**RULE), "5030"))
    assert g.reasons == ["STOP_ALREADY_CROSSED_AT_ENTRY"]


def test_without_the_rule_an_anchor_on_the_quote_is_ignored():
    g = gate(act(entry=None), q_with(terms(), "5025"))
    assert g.status == "HOLD" and g.reasons == ["OPEN_ENTRY_PRICE_MISSING"]


def test_with_the_rule_and_no_stated_level_it_holds_as_today():
    g = gate(act(entry=None), mq(terms(**RULE)))
    assert g.reasons == ["OPEN_ENTRY_PRICE_MISSING", "ENTRY_ANCHOR_NOT_STATED"]


def test_an_incomplete_rule_on_the_terms_holds():
    t = terms(entry_rule="REFERENCE_AS_COLLAR")
    assert gate(act(entry=None), q_with(t, "5025")).reasons == ["ENTRY_ANCHOR_RULE_INCOMPLETE"]


@pytest.mark.parametrize("entry,quote", [("5025", {}), ("5100", {}), ("5025", {"age": 60}),
                                         ("5025", {"bid": "4990", "ask": "5100"})])
@pytest.mark.parametrize("basis", ["DIRECT", "BASIS_SHIFT", "REFERENCE_TRIGGER"])
def test_an_explicit_entry_is_decided_exactly_as_today(entry, quote, basis):
    extra = {} if basis == "DIRECT" else {"reference": leg()}
    a = act(entry=entry if basis == "DIRECT" else str(Decimal(entry) - 15))
    off = gate(a, mq(terms(basis), **quote, **extra))
    on = gate(a, q_with(terms(basis, **RULE), "5025", **quote, **extra))
    assert on == off


@pytest.mark.parametrize("timing,status,reason", [
    (Timing.HISTORICAL, "RECONCILE", "HISTORICAL_REPORT_NO_ORDER"),
    (Timing.CONDITIONAL, "HOLD", "CONDITIONAL_TRIGGER_NOT_IMPLEMENTED"),
    (Timing.UNCERTAIN, "HOLD", "TEMPORAL_INTENT_UNCERTAIN"),
])
def test_retrospective_or_conditional_wording_never_uses_the_rule(timing, status, reason):
    a = act(entry=None).model_copy(update={"timing": timing})
    g = gate(a, q_with(terms(**RULE), "5025"))
    assert g.status == status and g.reasons == [reason] and g.plan is None


def test_an_anchored_add_enters_at_the_live_touch():
    pos = position(qty=1)
    a = act(Action.ADD, entry=None, idea="9:a1")
    t = terms(**{**RULE, "max_notional": Decimal(100000)})
    g = gate(a, q_with(t, "5025"), pos)
    assert g.status == "APPROVE" and g.reasons[0] == "PAPER_ADD", g.reasons
    assert g.plan.entry_limit == Decimal("5025.25") and g.plan.quantity == 2
    chase = gate(a, q_with(t, "5010"), pos)
    assert chase.reasons[0] == "ENTRY_ANCHOR_ADVERSE_MOVE"
    assert gate(a, mq(terms(**RULE)), pos).reasons == ["ADD_ENTRY_REFERENCE_MISSING",
                                                       "ENTRY_ANCHOR_NOT_STATED"]
    assert gate(a, mq(terms()), pos).reasons == ["ADD_ENTRY_REFERENCE_MISSING"]


def test_the_anchor_is_per_action_never_borrowed_from_another():
    q = mq(terms(**RULE)).model_copy(update={"entry_anchors": {"other": Decimal(5025)}})
    assert gate(act(entry=None), q).reasons == ["OPEN_ENTRY_PRICE_MISSING",
                                                "ENTRY_ANCHOR_NOT_STATED"]


# ================================================================== the mapping policy

MAP_NOW = datetime(2026, 6, 1, 14, 0, tzinfo=UTC)  # Monday
APPROVAL = 'approved = true\napproved_by = "test-owner"\napproved_at = 2026-05-29T12:00:00Z'
SESSION = ('session = { policy = "REGULAR_ONLY", timezone = "UTC", '
           'days = ["MON", "TUE", "WED", "THU", "FRI"], regular = ["13:00-21:00"] }')
ANCHOR_RULE = ('entry_rule = "REFERENCE_AS_COLLAR"\n'
               'entry_anchor = { max_adverse_move = "0.5%", max_favourable_move = "1%", '
               'max_signal_age_seconds = 60 }')
OIL_CONTRACTS = ('contracts = [ { month = "202607", roll_date = 2026-07-06 }, '
                 '{ month = "202608", roll_date = 2026-08-05 } ]')


def etf_entry(rule=ANCHOR_RULE, extra=""):
    return f"""
[[mapping]]
family = "QETF"
basis = "DIRECT"
{APPROVAL}
reference = {{ sec_type = "STK", asset_class = "ETF", symbol = "QETF", exchange = "SMART", currency = "USD" }}
execution = {{ sec_type = "STK", asset_class = "ETF", symbol = "QETF", exchange = "SMART", currency = "USD" }}
sizing = {{ units_per_signal = 10, max_units = 20, max_notional = "5000" }}
{SESSION}
{rule}
{extra}
"""


def oil_entry(rule=ANCHOR_RULE, level='level_contract_rule = "FRONT_MONTH"',
              contracts=OIL_CONTRACTS, selection="EXPLICIT_ONLY"):
    return f"""
[[mapping]]
family = "QOIL"
basis = "DIRECT"
{APPROVAL}
reference = {{ sec_type = "FUT", symbol = "QOF", exchange = "SIMNRG", currency = "USD" }}
execution = {{ sec_type = "FUT", symbol = "QOF", exchange = "SIMNRG", currency = "USD", multiplier = "10", min_tick = "0.01" }}
contract_selection = {{ rule = "{selection}", min_business_days_to_roll = 6 }}
{contracts}
sizing = {{ units_per_signal = 2, max_units = 4, max_notional = "5000" }}
{SESSION}
roll = {{ business_days_before = 3, action = "FLATTEN" }}
{rule}
{level}
"""


def policy(tmp_path, *entries) -> MappingPolicy:
    p = tmp_path / "m.toml"
    p.write_text("version = 2\n" + "".join(entries))
    return MappingPolicy.from_toml(p)


def action(asset="QETF", *, kind=Action.OPEN, entry=None, stop="48.90", timing=Timing.CURRENT,
           idea=None, action_id="a1", basis=PriceBasis.TRADED_CONTRACT) -> TradeAction:
    inst = Instrument(asset_class=AssetClass.ETF, symbol=asset) if asset == "QETF" else None
    return TradeAction(
        action_id=action_id, action=kind, timing=timing, instrument=inst,
        position_side=PositionSide.LONG, idea_reference=idea, price_basis=basis,
        source_market_symbol=asset, entry_reference=entry,
        stop_price=stop if kind == Action.OPEN else None,
        explicit_instruction=True, evidence=["invented"],
    )


def resolve(p, a, clause, levels, now=MAP_NOW, **kw):
    return p.resolve(a, clause=clause, now=now, levels=levels, **kw)


ETF_CLAUSE = "We are now long $QETF, 50.10 now."
ETF_LEVELS = [("REFERENCE", "50.10"), ("STOP", "48.90")]


def test_exactly_one_stated_level_becomes_the_anchor(tmp_path):
    d, inst = resolve(policy(tmp_path, etf_entry()), action(), ETF_CLAUSE, ETF_LEVELS)
    assert d.status == "SUPPORTED" and d.mapped and d.entry_anchor == "50.10"
    assert inst.symbol == "QETF"


def test_without_the_rule_no_anchor_is_formed(tmp_path):
    d, _ = resolve(policy(tmp_path, etf_entry(rule="")), action(), ETF_CLAUSE, ETF_LEVELS)
    assert d.status == "SUPPORTED" and d.entry_anchor is None


@pytest.mark.parametrize("clause,levels,reason", [
    ("We are now long $QETF, 50.10 now, 50.40 now.",
     [("REFERENCE", "50.10"), ("REFERENCE", "50.40")], "ENTRY_ANCHOR_SEVERAL_LEVELS"),
    ("We are now long $QETF, 50.10 now, above 50.40.",
     [("REFERENCE", "50.10"), ("TRIGGER", "50.40")], "ENTRY_ANCHOR_SECOND_PRICE"),
    ("We are now long $QETF, 50.10 now, out at 52.",
     [("REFERENCE", "50.10"), ("EXIT", "52")], "ENTRY_ANCHOR_SECOND_PRICE"),
    ("We are now long $QETF, 50.10 now.", [("REFERENCE", "50.20")], "ENTRY_ANCHOR_NOT_IN_POST"),
    ("We are now long $QETF, 50.10 now.", [("REFERENCE", "n/a")], "ENTRY_ANCHOR_UNREADABLE"),
    ("We are now long $QETF vs $QBOND, 50.10 now.", [("REFERENCE", "50.10")],
     "ENTRY_ANCHOR_INSTRUMENT_AMBIGUOUS"),
    ("We are now long $QETF, 50.10 now, up 3 today.", [("REFERENCE", "50.10")],
     "ENTRY_ANCHOR_UNEXPLAINED_PRICE"),
    ("We are now long $QETF, 50.10 now, 49.80 support.", [("REFERENCE", "50.10")],
     "ENTRY_ANCHOR_UNEXPLAINED_PRICE"),
])
def test_anything_but_one_clear_level_holds(tmp_path, clause, levels, reason):
    d, inst = resolve(policy(tmp_path, etf_entry()), action(), clause, levels)
    assert d.status == "UNRESOLVED" and inst is None and d.entry_anchor is None
    assert d.reason.split(",")[0] == reason


LONG = "We are now long $QETF."


@pytest.mark.parametrize("post,anchor", [
    ("We are now long $QETF.\n\n50.10 now.", "50.10"),
    ("We are now long $QETF. Stop 48.90.\n~$50.10 now.", "50.10"),
])
def test_a_level_line_of_the_post_may_be_the_anchors_source(tmp_path, post, anchor):
    d, _ = resolve(policy(tmp_path, etf_entry()), action(), LONG, [("REFERENCE", anchor)],
                   post_text=post)
    assert d.status == "SUPPORTED", d.reason
    assert d.entry_anchor == anchor


@pytest.mark.parametrize("post,reason", [
    ("We are now long $QETF.\n50.10 now.\nYesterday it was 50.10 now too.",
     "ENTRY_ANCHOR_SOURCE_AMBIGUOUS"),
    ("We are now long $QETF.\n50.10 now, 49.20.", "ENTRY_ANCHOR_UNEXPLAINED_PRICE"),
    ("We are now long $QETF.\n50.10 now, 49.20 support.", "ENTRY_ANCHOR_INSTRUMENT_UNCLEAR"),
    ("We are now long $QETF.\n$QBOND 50.10 now.", "ENTRY_ANCHOR_INSTRUMENT_AMBIGUOUS"),
    ("We are now long $QETF.", "ENTRY_ANCHOR_NOT_IN_POST"),
])
def test_a_level_line_with_anything_else_in_it_holds(tmp_path, post, reason):
    d, _ = resolve(policy(tmp_path, etf_entry()), action(), LONG, [("REFERENCE", "50.10")],
                   post_text=post)
    assert d.status == "UNRESOLVED" and d.reason.split(",")[0] == reason


def test_a_commentary_clause_quoting_the_market_may_be_the_source(tmp_path):
    p = policy(tmp_path, etf_entry())
    note = TradeAction(action_id="a2", action=Action.NO_TRADE, timing=Timing.COMMENTARY,
                       source_market_symbol="QETF", evidence=[])
    parsed = ParsedPost(actions=[action(), note])
    post = "We are now long $QETF. $QETF is trading at 50.10."
    _, [d, _] = p.apply(parsed, clauses={"a1": LONG, "a2": "$QETF trading at 50.10."},
                        now=MAP_NOW, post_text=post, levels={"a1": [("REFERENCE", "50.10")],
                                                             "a2": [("REFERENCE", "50.10")]})
    assert d.status == "SUPPORTED" and d.entry_anchor == "50.10"
    _, [d, _] = p.apply(parsed, clauses={"a1": LONG, "a2": "$QETF trading at 50.10, 48."},
                        now=MAP_NOW, post_text=post, levels={"a1": [("REFERENCE", "50.10")]})
    assert d.status == "UNRESOLVED" and d.reason.startswith("ENTRY_ANCHOR_UNEXPLAINED_PRICE")


def test_the_marks_reach_the_gateway_as_a_hold(tmp_path):
    p = policy(tmp_path, etf_entry())
    parsed = ParsedPost(actions=[action()])
    marked, [_d] = p.apply(parsed, clauses={"a1": "We are now long $QETF, 50.10 now, 50.40 now."},
                          now=MAP_NOW, levels={"a1": [("REFERENCE", "50.10"),
                                                      ("REFERENCE", "50.40")]})
    g = gate_action(marked.actions[0], MAP_NOW, None, MockPolicy(), None, now=MAP_NOW)
    assert g.status == "HOLD" and g.reasons[:2] == ["SOURCE_AMBIGUITIES",
                                                    "ENTRY_ANCHOR_SEVERAL_LEVELS"]


def test_a_level_shared_with_another_instrument_holds(tmp_path):
    p = policy(tmp_path, etf_entry())
    same_clause = "We are now long $QETF and $QOIL, 50.10 now."
    parsed = ParsedPost(actions=[action(), action("QOIL", action_id="a2",
                                                  basis=PriceBasis.SOURCE_SERIES)])
    _, decisions = p.apply(parsed, clauses={"a1": same_clause, "a2": same_clause}, now=MAP_NOW,
                           levels={"a1": [("REFERENCE", "50.10")], "a2": [("REFERENCE", "50.10")]})
    assert decisions[0].reason.startswith("ENTRY_ANCHOR_INSTRUMENT_AMBIGUOUS")
    # The same number claimed by another instrument's action in another clause.
    _, decisions = p.apply(parsed, clauses={"a1": ETF_CLAUSE, "a2": "And $QOIL is 50.10 now."},
                           now=MAP_NOW, levels={"a1": [("REFERENCE", "50.10")],
                                                "a2": [("REFERENCE", "50.10")]})
    assert decisions[0].reason.startswith("ENTRY_ANCHOR_INSTRUMENT_AMBIGUOUS")
    # A different number for the other instrument does not make this one ambiguous.
    _, decisions = p.apply(parsed, clauses={"a1": ETF_CLAUSE, "a2": "And $QOIL is 71.50 now."},
                           now=MAP_NOW, levels={"a1": [("REFERENCE", "50.10")],
                                                "a2": [("REFERENCE", "71.50")]})
    assert decisions[0].status == "SUPPORTED" and decisions[0].entry_anchor == "50.10"


def test_an_explicit_entry_never_forms_an_anchor(tmp_path):
    p = policy(tmp_path, etf_entry())
    d, _ = resolve(p, action(entry="50.10"), "We are now long $QETF at 50.10 entry.",
                   [("ENTRY", "50.10"), ("STOP", "48.90")])
    assert d.status == "SUPPORTED" and d.entry_anchor is None


@pytest.mark.parametrize("timing", [Timing.HISTORICAL, Timing.CONDITIONAL, Timing.COMMENTARY,
                                    Timing.UNCERTAIN])
def test_retrospective_or_conditional_wording_forms_no_anchor(tmp_path, timing):
    d, _ = resolve(policy(tmp_path, etf_entry()), action(timing=timing), ETF_CLAUSE, ETF_LEVELS)
    assert d.status == "SUPPORTED" and d.entry_anchor is None


def test_follow_ups_other_than_add_form_no_anchor(tmp_path):
    p = policy(tmp_path, etf_entry())
    for kind in (Action.REDUCE, Action.CLOSE, Action.MODIFY):
        d, _ = resolve(p, action(kind=kind, idea="9:a1"), ETF_CLAUSE, [("REFERENCE", "50.10")])
        assert d.entry_anchor is None, kind
    d, _ = resolve(p, action(kind=Action.ADD, idea="9:a1"), "Adding to our $QETF long, 50.30 now.",
                   [("REFERENCE", "50.30")])
    assert d.status == "SUPPORTED" and d.entry_anchor == "50.30"


# ------------------------------------------------------------------ the contract-month check

OIL_CLAUSE = "We are now long $QOIL, 71.50 now."
OIL_LEVELS = [("REFERENCE", "71.50"), ("STOP", "69.80")]


def oil_action(**kw):
    return action("QOIL", stop="69.80", **kw)


def test_the_accepted_anchor_is_the_only_level_the_month_check_counts_as_read(tmp_path):
    on = policy(tmp_path, oil_entry())
    d, inst = resolve(on, oil_action(), OIL_CLAUSE, OIL_LEVELS)
    assert d.status == "SUPPORTED", d.reason
    assert d.entry_anchor == "71.50" and inst.futures_contract == "202607"
    assert d.month_source == "FRONT_MONTH"
    # Without the entry rule the stated level is not consumed: the hand-off gap, exactly as before.
    off = policy(tmp_path, oil_entry(rule=""))
    d, _ = resolve(off, oil_action(), OIL_CLAUSE, OIL_LEVELS)
    assert d.status == "UNRESOLVED"
    assert d.reason.split(",")[:2] == ["CONTRACT_MONTH_UNRECOGNISED", "UNREAD_TOKEN:71.50"]
    # A second number is still unread, with the rule.
    d, _ = resolve(on, oil_action(), "We are now long $QOIL, 71.50 now, 72.10 earlier.",
                   OIL_LEVELS)
    assert d.status == "UNRESOLVED" and "UNREAD_TOKEN:72.10" in d.reason


# ================================================================== FRONT_MONTH


def test_front_month_is_the_first_listed_contract_outside_its_roll_margin(tmp_path):
    p = policy(tmp_path, oil_entry(rule=""))
    d, inst = resolve(p, oil_action(entry="71.50"), "We are now long $QOIL at 71.50.",
                      [("ENTRY", "71.50")])
    assert d.status == "SUPPORTED" and inst.futures_contract == "202607"
    assert d.month_source == "FRONT_MONTH"


def test_without_front_month_an_unnamed_month_holds_as_today(tmp_path):
    p = policy(tmp_path, oil_entry(rule="", level=""))
    d, _ = resolve(p, oil_action(entry="71.50"), "We are now long $QOIL at 71.50.", [])
    assert d.status == "UNRESOLVED"
    assert d.reason == "CONTRACT_SELECTION_AMBIGUOUS,CONTRACT_MONTH_NOT_STATED"


def test_an_explicit_month_overrides_the_front(tmp_path):
    p = policy(tmp_path, oil_entry())
    d, inst = resolve(p, oil_action(), "We are now long $QOIL Aug 2026, 71.50 now.", OIL_LEVELS)
    assert d.status == "SUPPORTED" and inst.futures_contract == "202608"
    assert d.month_source == "NAMED"


def test_inside_the_roll_window_front_month_holds_never_the_next(tmp_path):
    p = policy(tmp_path, oil_entry())
    d, inst = resolve(p, oil_action(), OIL_CLAUSE, OIL_LEVELS,
                      now=datetime(2026, 7, 1, 14, 0, tzinfo=UTC))  # 3 business days before roll
    assert d.status == "UNRESOLVED" and inst is None
    assert d.reason.split(",")[:2] == ["FRONT_MONTH_IN_ROLL_WINDOW", "FRONT:202607"]
    # Past the July roll date the August contract is the front.
    d, inst = resolve(p, oil_action(), OIL_CLAUSE, OIL_LEVELS,
                      now=datetime(2026, 7, 7, 14, 0, tzinfo=UTC))
    assert d.status == "SUPPORTED" and inst.futures_contract == "202608"


@pytest.mark.parametrize("contracts,detail", [
    (('contracts = [ { month = "202608", roll_date = 2026-08-05 }, '
      '{ month = "202607", roll_date = 2026-07-06 } ]'), "CONTRACT_LIST_NOT_IN_ROLL_ORDER"),
    (('contracts = [ { month = "202607", roll_date = 2026-08-05 }, '
      '{ month = "202608", roll_date = 2026-08-05 } ]'), "CONTRACT_LIST_NOT_IN_ROLL_ORDER"),
    ('contracts = [ { month = "202604", roll_date = 2026-03-20 } ]',
     "NO_LISTED_CONTRACT_BEFORE_ITS_ROLL_DATE"),
])
def test_an_ambiguous_front_holds(tmp_path, contracts, detail):
    p = policy(tmp_path, oil_entry(contracts=contracts))
    d, inst = resolve(p, oil_action(), OIL_CLAUSE, OIL_LEVELS)
    assert d.status == "UNRESOLVED" and inst is None
    assert d.reason == f"FRONT_MONTH_AMBIGUOUS,{detail}"


def test_a_follow_up_keeps_its_ideas_contract(tmp_path):
    from tradingbot.execution.broker import ContractRef
    held = {"9:a1": ContractRef(symbol="QOF", asset_class=AssetClass.FUTURE, sec_type="FUT",
                                exchange="SIMNRG", con_id=1, expiry="20260805")}
    p = policy(tmp_path, oil_entry())
    d, inst = resolve(p, oil_action(kind=Action.ADD, idea="9:a1"),
                      "Adding to our $QOIL long, 71.60 now.", [("REFERENCE", "71.60")], held=held)
    assert d.status == "SUPPORTED" and inst.futures_contract == "202608"
    assert d.month_source == "IDEA" and d.entry_anchor == "71.60"


# ================================================================== the loader


def load(text):
    return FamilyMapping.model_validate(tomllib.loads(text)["mapping"][0])


@pytest.mark.parametrize("text,message", [
    (etf_entry(rule='entry_rule = "REFERENCE_AS_COLLAR"'), "needs its [entry_anchor] limits"),
    (etf_entry(rule='entry_anchor = { max_adverse_move = "1%", max_favourable_move = "1%", '
                    'max_signal_age_seconds = 60 }'), "needs its [entry_anchor] limits"),
    (etf_entry(rule=ANCHOR_RULE.replace(', max_signal_age_seconds = 60', '')), "Field required"),
    (etf_entry(rule=ANCHOR_RULE.replace('"0.5%"', '"abc"')), "String should match"),
    (etf_entry(rule=ANCHOR_RULE.replace('"0.5%"', '"0"')), "must be positive"),
    (etf_entry(rule=ANCHOR_RULE.replace('"0.5%"', '"-1%"')), "String should match"),
    (etf_entry(rule='entry_rule = "LOOSE"'), "Input should be"),
    (oil_entry(level='level_contract_rule = "NEAREST"'), "Input should be"),
    (oil_entry(selection="EXPLICIT_OR_NEAREST"), "FRONT_MONTH is only for a DIRECT futures"),
    (etf_entry(rule='level_contract_rule = "FRONT_MONTH"'), "FRONT_MONTH is only for a DIRECT"),
])
def test_the_loader_refuses_an_incomplete_or_misplaced_rule(text, message):
    with pytest.raises(ValueError, match=re.escape(message)):
        load(text)


def test_a_legacy_stock_entry_cannot_take_the_rule():
    text = f"""
[[mapping]]
family = "QETF"
basis = "DIRECT"
{APPROVAL}
reference = {{ sec_type = "STK", asset_class = "ETF", symbol = "QETF", exchange = "SMART", currency = "USD" }}
execution = {{ sec_type = "STK", asset_class = "ETF", symbol = "QETF", exchange = "SMART", currency = "USD" }}
{ANCHOR_RULE}
"""
    with pytest.raises(ValueError, match="decided on the mapped path"):
        load(text)


def test_an_option_family_cannot_take_the_rule():
    text = f"""
[[mapping]]
family = "QSTK.OPT"
basis = "DIRECT"
reference = {{ sec_type = "OPT", symbol = "QSTK", exchange = "SMART", currency = "USD" }}
execution = {{ sec_type = "OPT", symbol = "QSTK", exchange = "SMART", currency = "USD", multiplier = "100", min_tick = "0.05" }}
sizing = {{ units_per_signal = 1, max_units = 2, max_notional = "2000" }}
{SESSION}
{ANCHOR_RULE}
"""
    with pytest.raises(ValueError, match="non-option traded family only"):
        load(text)


def test_the_rule_and_its_limits_reach_the_gateway_terms(tmp_path):
    entry = policy(tmp_path, etf_entry()).entries["QETF"]
    inst = Instrument(asset_class=AssetClass.ETF, symbol="QETF")
    from tradingbot.execution.broker import ContractRef
    c = ContractRef(symbol="QETF", asset_class=AssetClass.ETF, sec_type="STK", exchange="SMART",
                    con_id=5)
    t = entry.terms(inst, c, c)
    assert (t.entry_rule, t.anchor_max_adverse_move, t.anchor_max_favourable_move,
            t.anchor_max_signal_age_seconds) == ("REFERENCE_AS_COLLAR", "0.5%", "1%", 60)


# ================================================================== the shipped configuration


def test_the_shipped_configuration_keeps_both_rules_off():
    p = MappingPolicy.from_toml(SHIPPED)
    assert p.entries, "the shipped file maps at least the bond ETF"
    for e in p.entries.values():
        assert e.entry_rule == "EXPLICIT_ENTRY_ONLY" and e.entry_anchor is None
        assert e.level_contract_rule == "EXPLICIT_MONTH_ONLY"
    tlt = p.entries["TLT"]
    assert tlt.legacy_equivalent and tlt.approved


def test_the_shipped_bond_etf_still_holds_a_stated_market_level():
    p = MappingPolicy.from_toml(SHIPPED)
    a = TradeAction(action_id="a1", action=Action.OPEN, timing=Timing.CURRENT,
                    instrument=Instrument(asset_class=AssetClass.ETF, symbol="TLT"),
                    position_side=PositionSide.LONG, price_basis=PriceBasis.TRADED_CONTRACT,
                    source_market_symbol="TLT", stop_price="87.20", explicit_instruction=True,
                    evidence=["invented"])
    d, inst = p.resolve(a, clause="We are now long $TLT, 88.40 now.", now=MAP_NOW,
                        levels=[("REFERENCE", "88.40"), ("STOP", "87.20")])
    assert d.status == "SUPPORTED" and not d.mapped and d.entry_anchor is None
    q = Quote(symbol="TLT", bid=Decimal("88.39"), ask=Decimal("88.41"), observed_at=MAP_NOW)
    g = gate_action(a.model_copy(update={"instrument": inst}), MAP_NOW - timedelta(seconds=2),
                    q, MockPolicy(), None, now=MAP_NOW)
    assert g.status == "HOLD" and g.reasons == ["OPEN_ENTRY_PRICE_MISSING"]


def test_the_shipped_templates_show_the_new_keys_and_load_when_uncommented():
    text = SHIPPED.read_text()
    templates = text.split("templates (not active)")[1]
    for key in ("entry_rule", "entry_anchor", "max_adverse_move", "max_favourable_move",
                "max_signal_age_seconds", "level_contract_rule"):
        assert key in templates, key
    body = "\n".join(line[2:] for line in templates.splitlines()
                     if line == "# [[mapping]]" or _toml_line(line[2:]))
    parsed = MappingPolicy.from_toml(_write(body))
    rules = {f: (e.entry_rule, e.level_contract_rule) for f, e in parsed.entries.items()}
    assert ("REFERENCE_AS_COLLAR", "FRONT_MONTH") in rules.values()
    assert not any(e.approved for e in parsed.entries.values())


def _toml_line(text: str) -> bool:
    try:
        return bool(re.match(r"^[a-z_]+ = ", text)) and bool(tomllib.loads(text))
    except tomllib.TOMLDecodeError:
        return False


def _write(body: str) -> Path:
    import tempfile
    f = Path(tempfile.mkdtemp()) / "templates.toml"
    f.write_text("version = 2\n" + body)
    return f


# ================================================================== the whole service path

RULE_TOML = ('entry_rule = "REFERENCE_AS_COLLAR"\n'
             'entry_anchor = {{ max_adverse_move = "{adverse}", max_favourable_move = "1%", '
             'max_signal_age_seconds = 60 }}')


def service_mapping(*, rule=True, adverse="0.2%", front=True, oil_contracts=None) -> str:
    anchor = RULE_TOML.format(adverse=adverse) if rule else ""
    oil = oil_contracts or 'contracts = [ { month = "202607", roll_date = 2026-07-06 } ]'
    return f"""
version = 2

[[mapping]]
family = "QIDX"
basis = "REFERENCE_TRIGGER"
{APPROVAL}
reference = {{ sec_type = "IND", symbol = "QIDX", exchange = "SIMIDX", currency = "USD" }}
execution = {{ sec_type = "FUT", symbol = "QIF", exchange = "SIMFUT", currency = "USD", multiplier = "5", min_tick = "0.25" }}
contract_selection = {{ rule = "EXPLICIT_OR_NEAREST", min_business_days_to_roll = 6 }}
contracts = [ {{ month = "202606", roll_date = 2026-06-12 }}, {{ month = "202609", roll_date = 2026-09-11 }} ]
sizing = {{ units_per_signal = 2, max_units = 4, max_notional = "60000" }}
{SESSION}
roll = {{ business_days_before = 3, action = "FLATTEN" }}
reference_trigger = {{ released_order_type = "MKT", reference_hours = ["13:30-20:00"], require_backstop = true }}
basis_shift = {{ max_abs_basis = "40", max_age_seconds = 5, scale_ratio = "1" }}
{anchor}

[[mapping]]
family = "QOIL"
basis = "DIRECT"
{APPROVAL}
reference = {{ sec_type = "FUT", symbol = "QOF", exchange = "SIMNRG", currency = "USD" }}
execution = {{ sec_type = "FUT", symbol = "QOF", exchange = "SIMNRG", currency = "USD", multiplier = "10", min_tick = "0.01" }}
contract_selection = {{ rule = "EXPLICIT_ONLY", min_business_days_to_roll = 6 }}
{oil}
sizing = {{ units_per_signal = 2, max_units = 4, max_notional = "5000" }}
{SESSION}
roll = {{ business_days_before = 3, action = "FLATTEN" }}
{anchor}
{'level_contract_rule = "FRONT_MONTH"' if front else ''}
"""


def run_posts(tmp_path, name, texts, *, quotes=None, **mapping):
    steps = [Step(10 * i, t, "EXECUTED") for i, t in enumerate(texts)]
    s = Scenario(name, "QIDX", "X", steps)
    if quotes is not None:
        s.quotes = quotes
    run = MockMultiAssetRun(tmp_path / name, s, mapping_text=service_mapping(**mapping))
    for _ in range(12):
        run.step()
    return run


def outcomes(run):
    return [(a["action"], a["status"], a["reasons"]) for a in run.store.signal_actions()
            if a["action"] not in (None, "NO_TRADE")]


def entries(run):
    return [o for o in run.broker.all_orders() if o.parent_ref is None]


IDX_POST = "We are now long $QIDX, 5,010 now. Stop 4,980. Target 5,070."
OIL_POST = "We are now long $QOIL, 71.50 now. Stop 69.80."


def test_service_index_entry_from_a_stated_level(tmp_path):
    run = run_posts(tmp_path, "idx", [IDX_POST])
    [(kind, status, reasons)] = outcomes(run)
    assert kind == "OPEN" and status == "EXECUTED", reasons
    [entry] = entries(run)
    assert entry.order_type.value == "LMT" and entry.limit_price == Decimal("5025.25")
    assert entry.contract.expiry == QIF_JUN.expiry
    [intent] = run.service.coordinator.store.intents()
    from tradingbot.models import ExecutionPlan
    plan = ExecutionPlan.model_validate_json(intent.plan)
    assert plan.entry_anchor == Decimal(5010) and plan.anchor_series == "REFERENCE"
    assert plan.stop == Decimal(4980) and plan.protection_series == "REFERENCE"
    run.close()


def test_service_without_the_rule_holds_and_sends_nothing(tmp_path):
    run = run_posts(tmp_path, "off", [IDX_POST], rule=False)
    [(_, status, reasons)] = outcomes(run)
    # Today's behaviour exactly: for a futures family the unconsumed stated level is an unread
    # token at the contract-month check (the hand-off gap), so it never reaches the gateway.
    assert status == "HOLD" and "CONTRACT_MONTH_UNRECOGNISED" in reasons
    assert run.broker.submit_calls == 0
    run.close()


def test_service_refuses_to_chase(tmp_path):
    moved = {**STATIC, QIDX: ("5039.5", "5040.5", "5040"), QIF_JUN: ("5054.75", "5055.25", None)}
    run = run_posts(tmp_path, "chase", [IDX_POST], quotes=lambda _e: moved)
    [(_, status, reasons)] = outcomes(run)
    assert status == "HOLD" and "ENTRY_ANCHOR_ADVERSE_MOVE" in reasons
    assert run.broker.submit_calls == 0
    run.close()


def test_service_energy_front_month_entry(tmp_path):
    run = run_posts(tmp_path, "oil", [OIL_POST])
    [(_, status, reasons)] = outcomes(run)
    assert status == "EXECUTED", reasons
    [entry] = entries(run)
    assert entry.contract.expiry == QOF_JUL.expiry and entry.limit_price == Decimal("71.52")
    run.close()


def test_service_energy_without_front_month_holds(tmp_path):
    run = run_posts(tmp_path, "oil_off", [OIL_POST], front=False)
    [(_, status, reasons)] = outcomes(run)
    assert status == "HOLD" and "CONTRACT_SELECTION_AMBIGUOUS" in reasons
    assert run.broker.submit_calls == 0
    run.close()


def test_service_energy_front_in_roll_window_holds(tmp_path):
    contracts = ('contracts = [ { month = "202606", roll_date = 2026-06-03 }, '
                 '{ month = "202607", roll_date = 2026-07-06 } ]')
    run = run_posts(tmp_path, "oil_roll", [OIL_POST], oil_contracts=contracts)
    [(_, status, reasons)] = outcomes(run)
    assert status == "HOLD" and "FRONT_MONTH_IN_ROLL_WINDOW" in reasons
    assert run.broker.submit_calls == 0
    run.close()


def test_service_two_stated_levels_hold(tmp_path):
    run = run_posts(tmp_path, "two", ["We are now long $QIDX, 5,010 now, 5,020 now. Stop 4,980."])
    [(_, status, reasons)] = outcomes(run)
    assert status == "HOLD" and "ENTRY_ANCHOR_SEVERAL_LEVELS" in reasons
    assert run.broker.submit_calls == 0
    run.close()


def test_service_a_conditional_never_uses_the_rule(tmp_path):
    run = run_posts(tmp_path, "cond", ["If $QIDX breaks 5,010 we will go long. Stop 4,980."])
    [(_, status, reasons)] = outcomes(run)
    assert status == "HOLD" and "CONDITIONAL_TRIGGER_NOT_IMPLEMENTED" in reasons
    assert run.broker.submit_calls == 0
    run.close()


def test_service_an_explicit_entry_is_unchanged_by_the_rule(tmp_path):
    post = "We are now long $QIDX at 5,010. Stop 4,980. Target 5,070."
    on, off = run_posts(tmp_path, "x_on", [post]), run_posts(tmp_path, "x_off", [post], rule=False)
    assert outcomes(on) == outcomes(off) and outcomes(on)[0][1] == "EXECUTED"
    assert [o.limit_price for o in entries(on)] == [o.limit_price for o in entries(off)]
    on.close()
    off.close()
