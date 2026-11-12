"""Fixes after the independent review of the multi-asset mapping (PR #61).

Each test reproduces one reviewer probe or one rule the owner stated, with invented data only:
protective levels are never loosened by an amendment (F1), an unrecognised contract month is never
replaced by the nearest one (F2), a multi-leg option instruction never executes as one leg (F3),
and the SHOULD FIX rows of the review table.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from test_multi_asset_gateway import NOW, act, gate, leg, mq, position, terms

from tradingbot.event_models import PriceBasis, Timing, TradeAction
from tradingbot.execution.broker import ContractRef
from tradingbot.models import Action, AssetClass, Instrument, PositionSide
from tradingbot.runtime.mapping import MappingPolicy
from tradingbot.runtime.mock_multi_asset import (
    QIF_JUN,
    START,
    STATIC,
    MockMultiAssetRun,
    Scenario,
    Step,
    mapping_toml,
)

ENTRY = "We are now long $QIDX at 5,010. Stop 4,980. Target 5,070."


def run_steps(tmp_path, steps, *, name="t", quotes=None, family="QIDX", treatment="X",
              mapping_text=None, hook=None, extra=30):
    s = Scenario(name, family, treatment, steps)
    if quotes is not None:
        s.quotes = quotes
    run = MockMultiAssetRun(tmp_path / name, s, mapping_text=mapping_text)
    last = max(x.offset for x in steps)
    start = run.clock.now()
    while (run.clock.now() - start).total_seconds() <= last + extra:
        if hook:
            hook(run)
        run.step()
    return run


def traded(run):
    return [a for a in run.store.signal_actions() if a["action"] not in (None, "NO_TRADE")]


def exception_kinds(run):
    return [e.kind for e in run.service.coordinator.store.exceptions(open_only=True)]


# ================================================================== F1: never loosen protection


def test_f1_reference_trigger_tightening_never_lowers_the_backstop():
    """Reviewer F1: stop 4980 and backstop 4995 (basis +15). The basis moves to -5 and the
    publisher raises the stop to 4985: the backstop would come out at 4980. It stays at 4995."""
    t = terms("REFERENCE_TRIGGER", require_backstop=True)
    pos = position(stop="4980").model_copy(update={"backstop_price": "4995.00"})
    q = mq(t, bid="5004.75", ask="5005.25", reference=leg())  # basis -5
    g = gate(act(Action.MODIFY, idea="9:a1", stop="4985", target=None), q, pos)
    assert g.status == "APPROVE"
    assert "PROTECTIVE_LEVEL_NOT_LOOSENED" in g.reasons
    assert g.plan.stop == Decimal(4985)  # the trigger tightens as the publisher said
    assert g.plan.backstop_stop == Decimal("4995.00")  # the working backstop is kept
    assert g.plan.protective_levels_kept and "BACKSTOP" in g.plan.protective_levels_kept[0]


def test_f1_reference_trigger_tightening_that_also_tightens_the_backstop_moves_it():
    t = terms("REFERENCE_TRIGGER", require_backstop=True)
    pos = position(stop="4980").model_copy(update={"backstop_price": "4995.00"})
    q = mq(t, reference=leg())  # basis +15
    g = gate(act(Action.MODIFY, idea="9:a1", stop="4985", target=None), q, pos)
    assert g.status == "APPROVE" and "PROTECTIVE_LEVEL_NOT_LOOSENED" not in g.reasons
    assert g.plan.backstop_stop == Decimal("5000.00") and g.plan.protective_levels_kept == []


def test_f1_basis_shift_tightening_keeps_the_tighter_execution_stop():
    """The publisher tightens 1842 -> 1845 but the basis fell from 14 to 5: the converted stop
    (1850) would be looser than the working 1856. The working stop is kept and flagged."""
    t = terms("BASIS_SHIFT", min_tick=Decimal("0.10"), max_abs_basis=Decimal(20))
    pos = position(stop="1856.0").model_copy(update={"publisher_stop": "1842"})
    q = mq(t, bid="1859.9", ask="1860.1", reference=leg("1854.9", "1855.1", None))  # basis 5
    g = gate(act(Action.MODIFY, idea="9:a1", stop="1845", target=None), q, pos)
    assert g.status == "APPROVE" and "PROTECTIVE_LEVEL_NOT_LOOSENED" in g.reasons
    assert g.plan.stop == Decimal("1856.0") and g.plan.publisher_stop == Decimal(1845)
    assert "STOP" in g.plan.protective_levels_kept[0]


def test_f1_a_publisher_loosening_still_holds_in_every_treatment():
    widen = act(Action.MODIFY, idea="9:a1", stop="4970", target=None)
    rt = terms("REFERENCE_TRIGGER", require_backstop=True)
    pos = position(stop="4980").model_copy(update={"backstop_price": "4995.00"})
    assert gate(widen, mq(rt, reference=leg()), pos).reasons == [
        "STOP_WIDENING_REQUIRES_POLICY_REVIEW"]
    bs = terms("BASIS_SHIFT", min_tick=Decimal("0.10"), max_abs_basis=Decimal(20))
    pos = position(stop="1856.0").model_copy(update={"publisher_stop": "1842"})
    # Basis up to 19: the converted 1840 + 19 = 1859 is TIGHTER in execution terms, but the
    # publisher's own level loosened: the existing widening rule applies, unchanged.
    q = mq(bs, bid="1868.9", ask="1869.1", reference=leg("1849.9", "1850.1", None))
    g = gate(act(Action.MODIFY, idea="9:a1", stop="1840", target=None), q, pos)
    assert g.reasons == ["STOP_WIDENING_REQUIRES_POLICY_REVIEW"]


def test_f1_end_to_end_backstop_kept_and_exception_raised(tmp_path):
    """Reviewer probe E1 through the service: the broker backstop stays at 4995, the ledger and
    protection agree with what is actually working, and the divergence is a visible exception."""

    def quotes(elapsed):
        return {**STATIC, QIF_JUN: ("5004.75", "5005.25", None)} if elapsed >= 40 else STATIC

    run = run_steps(tmp_path, [Step(0, ENTRY, "EXECUTED"),
                               Step(60, "Raising our $QIDX stop to 4,985.", "EXECUTED")],
                    quotes=quotes)
    [p] = [p for p in run.service.coordinator.ledger() if p.quantity > 0]
    backstop = next(o for o in run.broker.open_orders() if o.order_ref == p.backstop_ref)
    assert backstop.stop_price == Decimal("4995.00")
    assert (p.stop_price, p.backstop_price, p.protection) == ("4985", "4995.00", "CONFIRMED")
    assert "PROTECTIVE_LEVEL_NOT_LOOSENED" in exception_kinds(run)
    run.close()


# ================================================================== F2: contract-month recognition

MAP_NOW = datetime(2026, 6, 1, 14, 0, tzinfo=UTC)


def demo_policy(tmp_path, text=None) -> MappingPolicy:
    p = tmp_path / "m.toml"
    p.write_text(text or mapping_toml(approved=True))
    return MappingPolicy.from_toml(p)


def opening(asset, action=Action.OPEN, *, idea=None) -> TradeAction:
    return TradeAction(
        action_id="a1", action=action, timing=Timing.CURRENT,
        instrument=Instrument(asset_class=AssetClass.EQUITY, symbol=asset),
        position_side=PositionSide.LONG, idea_reference=idea,
        price_basis=PriceBasis.TRADED_CONTRACT, source_market_symbol=asset,
        entry_reference="10" if action in (Action.OPEN, Action.ADD) else None,
        explicit_instruction=True, evidence=["invented"],
    )


@pytest.mark.parametrize(
    "form",
    ["Sep 2026", "September 2026", "Sept 2026", "Sep. 2026", "Sep '26", "Sep'26", "Sep 26",
     "Sep26", "SEPTEMBER '26", "Sep ’26", "2026-09", "2026/09", "09/2026", "9/2026", "202609",
     "QIFU6", "QIFU26", "U6", "U26", "the Sep contract"],
)
def test_f2_each_common_month_form_selects_that_month(tmp_path, form):
    d, inst = demo_policy(tmp_path).resolve(
        idx_open(), clause=f"We are now long $QIDX {form} at 5,010. Stop 4,980.", now=MAP_NOW)
    assert d.status == "SUPPORTED", d.reason
    assert inst.futures_contract == "202609"  # never the nearest (June)


@pytest.mark.parametrize(
    "form",
    ["Sep", "Sept.", "September", "Sep '27", "Sep 2027", "Sep 18", "QIFX6", "QIFU7", "QABU6",
     "QIFU", "2026-13", "13/2026", "2026-00", "Sep 202", "Nov 2026", "(QIFZ26)", "Dec contract"],
)
def test_f2_a_month_like_token_that_does_not_resolve_holds(tmp_path, form):
    """Reviewer probe: "Sep '26" and "(QIFU6)" executed in June. Every near miss now HOLDs."""
    d, inst = demo_policy(tmp_path).resolve(
        opening("QIDX"), clause=f"We are now long $QIDX {form} at 5,010. Stop 4,980.", now=MAP_NOW)
    assert d.status == "UNRESOLVED" and inst is None
    assert d.reason.split(",")[0] == "CONTRACT_MONTH_UNRECOGNISED"


def test_f2_the_nearest_rule_applies_only_without_any_month_like_token(tmp_path):
    p = demo_policy(tmp_path)
    for clause in ["We are now long $QIDX at 5,010. Stop 4,980.",
                   "We may add to $QIDX at 5,010 and will march on. Stop 4,980."]:
        d, inst = p.resolve(idx_open(), clause=clause, now=MAP_NOW)
        assert d.status == "SUPPORTED" and inst.futures_contract == "202606", clause


def test_f2_reviewer_probes_through_the_service(tmp_path):
    for i, text in enumerate([
        "We are now long $QIDX Sep '26 at 5,010. Stop 4,980. Target 5,070.",
        "We are now long $QIDX (QIFU6) at 5,010. Stop 4,980. Target 5,070.",
    ]):
        run = run_steps(tmp_path, [Step(0, text, "EXECUTED")], name=f"p{i}")
        refs = [o.contract.expiry for o in run.broker.all_orders()]
        assert refs and all(e == "20260918" for e in refs), refs  # September, never June
        run.close()
    run = run_steps(tmp_path, [Step(0, "We are now long $QIDX Sep '27 at 5,010. Stop 4,980.",
                                    "HOLD")], name="miss")
    [a] = traded(run)
    assert a["status"] == "HOLD" and "CONTRACT_MONTH_UNRECOGNISED" in a["reasons"]
    assert run.broker.submit_calls == 0
    run.close()


# ================================================================== F3: single-leg options only

SINGLE = "Buying $QSTK 2026-09-18 105 call at 4.22. Stop 3.40."


@pytest.mark.parametrize(
    "clause,detail",
    [
        ("Buying the $QSTK 2026-09-18 100/105 call spread at 4.22. Stop 3.40.", None),
        ("We are now long $QSTK 2026-09-18 105 calls at 4.22, short the 110 calls.",
         "SEVERAL_STRIKES"),
        ("Buying $QSTK 2026-09-18 100-105 calls at 4.22.", "SEVERAL_STRIKES"),
        ("Buying $QSTK 2026-09-18 105 and 110 calls at 4.22.", "SEVERAL_STRIKES"),
        ("Buying $QSTK 2026-09-18 105 call and 110 put at 4.22.", None),
        ("Buying $QSTK 2026-09-18 105 call and 105 put at 4.22.", "SEVERAL_RIGHTS"),
        ("Buying $QSTK 2026-09-18 and 2026-12-18 105 calls at 4.22.", "SEVERAL_EXPIRIES"),
        ("Buying the $QSTK 2026-09-18 105 straddle at 8.10.", "STRATEGY_WORD:straddle"),
        ("Buying the $QSTK 2026-09-18 100 105 strangle at 3.10.", "STRATEGY_WORD:strangle"),
        ("Buying the $QSTK 2026-09-18 105 call vertical at 2.10.", "STRATEGY_WORD:vertical"),
        ("Buying the $QSTK 2026-09-18 105 call calendar at 1.10.", "STRATEGY_WORD:calendar"),
        ("Buying the $QSTK 2026-09-18 105 call diagonal at 1.10.", "STRATEGY_WORD:diagonal"),
        ("Buying the $QSTK 2026-09-18 105 iron condor at 1.10.", "STRATEGY_WORD:condor"),
        ("Buying the $QSTK 2026-09-18 105 call butterfly at 1.10.", "STRATEGY_WORD:butterfly"),
        ("Buying the $QSTK 2026-09-18 105 put collar at 1.10.", "STRATEGY_WORD:collar"),
        ("Buying the $QSTK 2026-09-18 105 call ratio at 1.10.", "STRATEGY_WORD:ratio"),
        ("Buying the $QSTK 2026-09-18 105 covered call at 1.10.", "STRATEGY_WORD:covered"),
        ("Buying the $QSTK 2026-09-18 105 call combo at 1.10.", "STRATEGY_WORD:combo"),
        ("Buying the $QSTK 2026-09-18 105 call spread at 1.10.", "STRATEGY_WORD:spread"),
    ],
)
def test_f3_a_multi_leg_option_instruction_holds(tmp_path, clause, detail):
    d, inst = demo_policy(tmp_path).resolve(opening("QSTK"), clause=clause, now=MAP_NOW)
    assert d.status == "UNRESOLVED" and inst is None and d.family == "QSTK.OPT"
    reasons = d.reason.split(",")
    assert reasons[0] == "OPTION_STRATEGY_NOT_SINGLE_LEG", reasons
    if detail:
        assert detail in reasons


def test_f3_exactly_one_underlying_expiry_strike_and_right_is_a_single_leg(tmp_path):
    d, inst = demo_policy(tmp_path).resolve(opening("QSTK"), clause=SINGLE, now=MAP_NOW)
    assert d.status == "SUPPORTED" and (inst.strike, inst.option_right) == ("105", "CALL")


def test_f3_reviewer_probe_spread_never_reaches_the_broker(tmp_path):
    run = run_steps(tmp_path, [Step(0, "We are now long the $QSTK 2026-09-18 100/105 call spread at "
                                       "4.22. Stop 3.40.", "HOLD")], family="QSTK.OPT")
    [a] = traded(run)
    assert a["status"] == "HOLD" and "OPTION_STRATEGY_NOT_SINGLE_LEG" in a["reasons"]
    assert run.broker.submit_calls == 0
    run.close()


# ================================================================== F4: approval governs new exposure

OIL_OPEN = "We are now long $QOIL Jul 2026 at 71.50. Stop 69.80. Target 75.00."


def revoke_at(seconds, text_of=lambda: mapping_toml(approved=False)):
    """A hook that rewrites the mapping file and reloads it, as an owner withdrawing approval."""

    def hook(run):
        if (run.clock.now() - START).total_seconds() >= seconds and not getattr(
                run, "_revoked", False):
            run._revoked = True
            run.mapping_file.write_text(text_of())
            run.service.mapping = MappingPolicy.from_toml(run.mapping_file)

    return hook


def without_family(family):
    text = mapping_toml(approved=True)
    head, _, rest = text.partition(f'[[mapping]]\nfamily = "{family}"')
    _, sep, tail = rest.partition("[[mapping]]")
    return head + (sep + tail if sep else "")


@pytest.mark.parametrize("change", ["unapproved", "removed"])
def test_f4_a_position_can_still_be_closed_after_its_family_loses_approval(tmp_path, change):
    """Reviewer probe E2: QOIL opened, approval withdrawn, CLOSE held. It now executes on the
    mapping terms recorded on the idea when it was opened."""
    text_of = (lambda: mapping_toml(approved=False)) if change == "unapproved" else (
        lambda: without_family("QOIL"))
    run = run_steps(tmp_path, [
        Step(0, OIL_OPEN, "EXECUTED"),
        Step(60, "Raising our $QOIL stop to 70.40.", "EXECUTED"),
        Step(120, "Adding to our $QOIL long at 71.50.", "HOLD"),
        Step(180, "Closing the rest of our $QOIL long.", "EXECUTED")],
        family="QOIL", hook=revoke_at(30, text_of))
    by = {a["action"]: a for a in traded(run)}
    assert by["OPEN"]["status"] == "EXECUTED"
    assert by["MODIFY"]["status"] == "EXECUTED", by["MODIFY"]["reasons"]
    assert by["ADD"]["status"] == "HOLD"  # approval governs new exposure
    assert {"MAPPING_NOT_APPROVED", "UNSUPPORTED_INSTRUMENT_MAPPING"} & set(by["ADD"]["reasons"])
    assert by["CLOSE"]["status"] == "EXECUTED", by["CLOSE"]["reasons"]
    assert all(p.quantity == 0 for p in run.service.coordinator.ledger())
    run.close()


def test_f4_mapping_terms_are_recorded_on_the_idea(tmp_path):
    run = run_steps(tmp_path, [Step(0, OIL_OPEN, "EXECUTED")], family="QOIL")
    [p] = run.service.coordinator.ledger()
    from tradingbot.runtime.mapping import FamilyMapping

    recorded = FamilyMapping.model_validate_json(p.mapping_terms)
    assert recorded.family == "QOIL" and recorded.approved
    run.close()


def test_f4_an_open_under_revoked_mapping_is_shown_in_coverage_and_on_the_page(tmp_path):
    run = run_steps(tmp_path, [Step(0, OIL_OPEN, "EXECUTED")], family="QOIL",
                    hook=revoke_at(10))
    from tradingbot.runtime import lifecycle

    view = lifecycle.load(run.config)
    row = next(c for c in view["coverage"] if c["family"] == "QOIL")
    assert row["position_status"] == "OPEN_UNDER_REVOKED_MAPPING"
    assert row["open_positions"] == 1
    [p] = [p for p in view["protection"] if p["quantity"] > 0]
    assert p["mapping_status"] == "OPEN_UNDER_REVOKED_MAPPING"
    run.close()


def test_f4_terms_for_exits_only_never_approve_new_exposure():
    from tradingbot.models import PositionSide as Side

    t = terms(exits_only=True)
    g = gate(act(), mq(t))
    assert g.status == "HOLD" and g.reasons[0] == "MAPPING_NOT_APPROVED"
    g = gate(act(Action.ADD, idea="9:a1", stop=None, target=None), mq(t), position())
    assert g.status == "HOLD" and g.reasons[0] == "MAPPING_NOT_APPROVED"
    g = gate(act(Action.CLOSE, idea="9:a1", side=Side.LONG), mq(t), position())
    assert g.status == "APPROVE"


def test_f4_the_execution_page_shows_an_open_under_revoked_mapping(tmp_path, monkeypatch):
    from streamlit.testing.v1 import AppTest
    from test_multi_asset_execution import REPO, _exe, _pol, _svc

    run = run_steps(tmp_path, [Step(0, OIL_OPEN, "EXECUTED")], family="QOIL",
                    hook=revoke_at(10))
    cfg = tmp_path / "runtime.toml"
    c = run.config
    cfg.write_text(
        f'mode = "mock"\n[paths]\nstate_dir = "{c.paths.state_dir}"\n'
        f'halt_file = "{c.paths.halt_file}"\nmapping_file = "{run.mapping_file}"\n'
        f'[source]\nkind = "replay"\nfile = "{c.source.file}"\n'
        + "".join(
            f"[{t}]\n" + "".join(f"{k} = {v!r}\n".replace("'", '"') for k, v in vals.items())
            for t, vals in (("service", _svc()), ("policy", _pol()), ("execution", _exe()))
        )
    )
    monkeypatch.setenv("TRADINGBOT_RUNTIME_CONFIG", str(cfg))
    at = AppTest.from_file(str(REPO / "dashboard" / "pages" / "8_Execution.py"),
                           default_timeout=60)
    at.run()
    assert not at.exception
    text = " ".join(str(e.value) for kind in ("markdown", "warning") for e in at.get(kind))
    assert "| QOIL | NOT APPROVED |" in text
    assert "OPEN_UNDER_REVOKED_MAPPING" in text and "withdrawn mapping" in text
    run.close()


# ================================================================== F5: reference freshness

def ref_leg(bid="5009.5", ask="5010.5", last="5010", *, updated_age=0.0, received_age=0.0,
            known=True):
    """A reference quote received ``received_age`` ago whose series last updated
    ``updated_age`` ago (None when the broker does not say)."""
    return leg(bid, ask, last, age=received_age).model_copy(update={
        "updated_at": NOW - timedelta(seconds=updated_age) if known else None})


BS_TERMS = {"min_tick": Decimal("0.10"), "max_abs_basis": Decimal(20)}


def bs_open(reference):
    t = terms("BASIS_SHIFT", **BS_TERMS)
    return gate(act(entry="1850", stop="1835", target="1880"),
                mq(t, bid="1861.9", ask="1862.1", reference=reference))


def rt_open(reference, **kw):
    return gate(act(entry="5010", stop="4980", target="5070"),
                mq(terms("REFERENCE_TRIGGER", **kw), reference=reference))


def test_f5_a_fresh_reference_quote_with_its_own_update_time_is_used():
    assert bs_open(ref_leg("1849.9", "1850.1", None)).status == "APPROVE"
    assert rt_open(ref_leg()).status == "APPROVE"


@pytest.mark.parametrize("treatment", ["BASIS_SHIFT", "REFERENCE_TRIGGER"])
@pytest.mark.parametrize(
    "case,detail",
    [
        # Reviewer probe: a frozen value received just now. Its series last updated 20 s ago.
        ({"updated_age": 45}, "REFERENCE_QUOTE_STALE"),
        ({"known": False}, "REFERENCE_QUOTE_UPDATE_TIME_UNKNOWN"),
        # Reviewer probe: a crossed reference (bid above ask) passed through its last price.
        ({"bid": "5012", "ask": "5008"}, "REFERENCE_QUOTE_CROSSED"),
    ],
)
def test_f5_an_unfresh_reference_quote_holds_every_treatment(treatment, case, detail):
    if treatment == "BASIS_SHIFT":
        kw = {"bid": "1849.9", "ask": "1850.1", "last": None, **case}
        if "bid" in case:
            kw.update(bid="1851", ask="1849", last="1850")
        g = bs_open(ref_leg(**kw))
    else:
        g = rt_open(ref_leg(**case))
    assert g.status == "HOLD" and "REFERENCE_QUOTE_NOT_FRESH" in g.reasons, g.reasons
    assert detail in g.reasons


def test_f5_a_reference_quote_outside_its_declared_hours_holds_an_amendment():
    late = NOW.replace(hour=20, minute=30)  # the session is open, the index is not
    t = terms("REFERENCE_TRIGGER")
    q = mq(t, reference=leg(at=late).model_copy(update={"updated_at": late}), at=late)
    g = gate(act(Action.MODIFY, idea="9:a1", stop="4990", target=None), q, position(stop="4980"),
             now=late)
    assert g.status == "HOLD" and g.reasons == ["REFERENCE_QUOTE_NOT_FRESH",
                                                "REFERENCE_QUOTE_OUTSIDE_HOURS"]


def test_f5_the_backstop_needs_a_fresh_reference_too():
    g = rt_open(ref_leg(updated_age=8), require_backstop=True)  # the basis allows 5 s
    assert g.status == "HOLD" and "REFERENCE_QUOTE_NOT_FRESH" in g.reasons


def test_f5_the_sim_broker_reports_the_series_update_time(tmp_path):
    from tradingbot.runtime.mock_multi_asset import QIDX

    run = MockMultiAssetRun(tmp_path / "s", Scenario("s", "QIDX", "X", [Step(0, ENTRY, "X")]))
    at = run.clock.now()
    run.broker.set_quote("QIDX", "5009.5", "5010.5", last="5010", contract=QIDX,
                         updated_at=at - timedelta(seconds=40))
    q = run.broker.reference_quote(QIDX)
    assert q.received_at == at and q.updated_at == at - timedelta(seconds=40)
    run.close()


def test_f5_a_frozen_reference_holds_through_the_service(tmp_path):
    """The broker re-sends the index every cycle, but its own update time stopped 40 s ago."""
    from tradingbot.runtime.mock_multi_asset import QIDX

    run = MockMultiAssetRun(tmp_path / "f", Scenario("f", "QIDX", "X", [Step(0, ENTRY, "X")]))
    frozen_at = run.clock.now() - timedelta(seconds=40)
    publish = run.publish_quotes

    def publish_frozen():
        publish()
        run.broker.set_quote("QIDX", "5009.5", "5010.5", last="5010", contract=QIDX,
                             updated_at=frozen_at)

    run.publish_quotes = publish_frozen
    for _ in range(6):
        run.step()
    [a] = traded(run)
    assert a["status"] == "HOLD" and "REFERENCE_QUOTE_NOT_FRESH" in a["reasons"]
    assert run.broker.submit_calls == 0
    run.close()


def test_f5_without_a_broker_update_time_only_a_seen_change_counts(tmp_path):
    """A broker that reports no update time: the first sighting of a value is not evidence of
    freshness (HOLD); a value seen to change since the last decision is, at its receipt time."""
    from tradingbot.models import Instrument as Inst
    from tradingbot.runtime.mock_multi_asset import QIDX

    run = MockMultiAssetRun(tmp_path / "u", Scenario("u", "QIDX", "X", [Step(0, ENTRY, "X")]))
    run.broker.reports_update_time = False
    run.publish_quotes()
    svc, entry = run.service, run.service.mapping.entries["QIDX"]
    inst = Inst(asset_class=AssetClass.FUTURE, symbol="QIF", exchange="SIMFUT",
                futures_contract="202606")
    first = svc._mapped_quote(entry, inst, run.clock.now())
    assert first.reference.updated_at is None
    run.clock.advance(2)
    run.publish_quotes()
    same = svc._mapped_quote(entry, inst, run.clock.now())
    assert same.reference.updated_at is None  # unchanged value: still no evidence
    run.clock.advance(2)
    run.broker.set_quote("QIDX", "5010.5", "5011.5", last="5011", contract=QIDX)
    moved = svc._mapped_quote(entry, inst, run.clock.now())
    assert moved.reference.updated_at == run.clock.now()
    run.clock.advance(20)
    run.broker.set_quote("QIDX", "5010.5", "5011.5", last="5011", contract=QIDX)
    stuck = svc._mapped_quote(entry, inst, run.clock.now())
    assert stuck.reference.updated_at == run.clock.now() - timedelta(seconds=20)
    run.close()


# ================================================================== F6: the bond ETF regression

SHIPPED = Path(__file__).resolve().parents[1] / "config" / "instrument_mapping.toml"
TLT_HELD = {"9:a0": ContractRef(symbol="TLT", asset_class=AssetClass.ETF, con_id=7)}


def tlt(action, idea="9:a0"):
    a = opening("TLT", action, idea=idea)
    return a.model_copy(update={"instrument": Instrument(asset_class=AssetClass.ETF,
                                                         symbol="TLT")})


@pytest.mark.parametrize("clause", [
    "Closing our TLT long.", "Calling it: closing our TLT long.",
    "We'll call it a day and close TLT.", "Closing TLT, no options for now.",
    "Closing TLT, put the proceeds in cash.",
])
@pytest.mark.parametrize("held", [TLT_HELD, None])
def test_f6_a_stock_exit_is_never_routed_to_options_by_a_word(clause, held):
    """Reviewer probe: a TLT CLOSE containing "call", "put" or "options" went to TLT.OPT and was
    held, where main executed it. An exit binds to its idea, never to option language."""
    pol = MappingPolicy.from_toml(SHIPPED)
    d, inst = pol.resolve(tlt(Action.CLOSE), clause=clause, now=MAP_NOW, held=held)
    assert d.status == "SUPPORTED" and d.family == "TLT", (d.family, d.reason)
    assert inst.symbol == "TLT"


@pytest.mark.parametrize("clause", [
    "We are now long TLT options at 90.", "Buying TLT calls at 90.", "Buying the TLT 92 put.",
])
def test_f6_an_opening_option_mention_without_terms_never_buys_the_stock(clause):
    pol = MappingPolicy.from_toml(SHIPPED)
    d, inst = pol.resolve(tlt(Action.OPEN, idea=None), clause=clause, now=MAP_NOW)
    assert d.status != "SUPPORTED" and inst is None and d.family == "TLT.OPT"


def test_f6_an_option_idea_exit_binds_to_its_own_contract_without_option_words(tmp_path):
    held = {"9:a1": ContractRef(symbol="QSTK", asset_class=AssetClass.OPTION, sec_type="OPT",
                                expiry="20260918", strike=Decimal(105), right="C", con_id=1)}
    d, inst = demo_policy(tmp_path).resolve(opening("QSTK", Action.CLOSE, idea="9:a1"),
                                            clause="Closing our $QSTK position.", now=MAP_NOW,
                                            held=held)
    assert d.status == "SUPPORTED" and d.family == "QSTK.OPT"
    assert (inst.strike, inst.option_right, inst.expiry) == ("105", "CALL", "2026-09-18")


# ================================================================== F7: compact strike-right

@pytest.mark.parametrize("token,right", [("105C", "CALL"), ("105c", "CALL"), ("105P", "PUT")])
def test_f7_a_compact_strike_and_right_is_option_language(tmp_path, token, right):
    """Reviewer probe: "$QSTK 2026-09-18 105C" resolved to the stock family."""
    d, inst = demo_policy(tmp_path).resolve(
        opening("QSTK"), clause=f"We are now long $QSTK 2026-09-18 {token} at 4.22.", now=MAP_NOW)
    assert d.family == "QSTK.OPT"
    if right == "CALL":
        assert d.status == "SUPPORTED" and (inst.strike, inst.option_right) == ("105", "CALL")
    else:  # the shipped demo lists no put: the broker would not resolve it, but the terms do
        assert d.status == "SUPPORTED" and inst.option_right == "PUT"


def test_f7_compact_strikes_count_towards_the_single_leg_rule(tmp_path):
    d, _ = demo_policy(tmp_path).resolve(
        opening("QSTK"), clause="We are now long $QSTK 2026-09-18 105C 110C at 4.22.",
        now=MAP_NOW)
    assert d.status == "UNRESOLVED" and d.reason.startswith("OPTION_STRATEGY_NOT_SINGLE_LEG")


# ================================================================== F8: reference hours on ADD

def test_f8_a_reference_trigger_add_outside_reference_hours_holds():
    """Reviewer probe P7b: an ADD at 13:10 (session open, index not yet updating) approved."""
    from tradingbot.models import MockPolicy

    t13 = NOW.replace(hour=13, minute=10)
    t = terms("REFERENCE_TRIGGER", max_units=6, max_notional=Decimal(10**7))
    q = mq(t, at=t13, reference=leg(at=t13))
    g = gate(act(Action.ADD, idea="9:a1", entry="5010", stop=None, target=None), q,
             position(qty=2, stop="4995"), now=t13,
             policy=MockPolicy(max_notional_usd=Decimal(10**7)))
    assert g.status == "HOLD" and g.reasons == ["REFERENCE_SERIES_NOT_UPDATING"]


# ================================================================== F9: DIRECT means traded prices

@pytest.mark.parametrize("action", [Action.OPEN, Action.MODIFY, Action.CLOSE])
def test_f9_direct_refuses_levels_marked_as_the_source_series(action):
    """Reviewer probe P3: DIRECT with price_basis SOURCE_SERIES approved; the stock path refuses."""
    a = act(action, basis=PriceBasis.SOURCE_SERIES, entry="5025", stop="5000", target="5085",
            idea=None if action == Action.OPEN else "9:a1")
    g = gate(a, mq(terms()), None if action == Action.OPEN else position())
    assert g.status == "HOLD" and g.reasons == ["SOURCE_TO_BROKER_PRICE_BASIS_UNRESOLVED"]


def test_f9_other_treatments_still_take_source_series_levels():
    a = act(basis=PriceBasis.SOURCE_SERIES, entry="5010", stop="4980", target="5070")
    assert gate(a, mq(terms("REFERENCE_TRIGGER"), reference=leg())).status == "APPROVE"


# ================================================================== F10 / F11: the loader

APP = 'approved = true\napproved_by = "o"\napproved_at = 2026-01-01T00:00:00Z\n'
SIZE = ('sizing = { units_per_signal = 1, max_units = 1, max_notional = "100000" }\n'
        'session = { policy = "REGULAR_ONLY", timezone = "UTC", days = ["MON"], '
        'regular = ["13:00-21:00"] }\n')
SPY = 'sec_type = "STK", asset_class = "ETF", symbol = "SPY", exchange = "SMART", currency = "USD"'
IDX = 'sec_type = "IND", symbol = "SPX", exchange = "CBOE", currency = "USD"'


def load(tmp_path, text):
    p = tmp_path / "m.toml"
    p.write_text(text)
    return MappingPolicy.from_toml(p)


def entry(family, basis, reference, execution, extra="", approval=APP):
    return (f'version = 2\n[[mapping]]\nfamily = "{family}"\nbasis = "{basis}"\n{approval}'
            f"reference = {{ {reference} }}\nexecution = {{ {execution} }}\n{SIZE}{extra}")


@pytest.mark.parametrize("text,match", [
    # Reviewer probe: the publisher's SPX mapped DIRECT onto SPY.
    (entry("SPX", "DIRECT", SPY, SPY), "same price series"),
    # Reviewer probe: an index-to-ETF BASIS_SHIFT with a cap of 6000 (a ratio treated as a gap).
    (entry("SPX", "BASIS_SHIFT", IDX, SPY,
           'basis_shift = { max_abs_basis = "6000", max_age_seconds = 5, scale_ratio = "1" }\n'),
     "price scale"),
    (entry("SPX", "BASIS_SHIFT", IDX, SPY,
           'basis_shift = { max_abs_basis = "60", max_age_seconds = 5, scale_ratio = "0.1" }\n'),
     "scale_ratio"),
    # BASIS_SHIFT must declare its scale ratio.
    (entry("QMTL", "BASIS_SHIFT",
           'sec_type = "CMDTY", symbol = "QMS", exchange = "X", currency = "USD"',
           'sec_type = "CFD", symbol = "QMC", exchange = "X", currency = "USD"',
           'basis_shift = { max_abs_basis = "20", max_age_seconds = 5 }\n'), "scale_ratio"),
    # A declared same series needs its reason.
    (entry("SPX", "DIRECT", SPY, SPY, 'same_series = true\n'), "same_series joins"),
    (entry("SPX", "DIRECT", IDX, SPY, 'same_series = true\nsame_series_reason = "x"\n'),
     "same_series joins"),
    (entry("QMTL", "BASIS_SHIFT",
           'sec_type = "CMDTY", symbol = "QMS", exchange = "X", currency = "USD"',
           'sec_type = "CFD", symbol = "QMC", exchange = "X", currency = "USD"',
           'basis_shift = { max_abs_basis = "20", max_age_seconds = 5, scale_ratio = "1" }\n'
           'same_series = true\nsame_series_reason = "x"\n'), "same_series joins"),
])
def test_f10_the_loader_refuses_pairs_that_are_not_the_same_series_or_scale(tmp_path, text,
                                                                          match):
    with pytest.raises(ValueError, match=match):
        load(tmp_path, text)


def test_f10_legal_pairs_still_load(tmp_path):
    ok = load(tmp_path, entry("SPY", "DIRECT", SPY, SPY))
    assert ok.entries["SPY"].approved
    ok = load(tmp_path, entry(
        "QMTL", "BASIS_SHIFT", 'sec_type = "CMDTY", symbol = "QMS", exchange = "X", currency = "USD"',
        'sec_type = "CFD", symbol = "QMC", exchange = "X", currency = "USD"',
        'basis_shift = { max_abs_basis = "20", max_age_seconds = 5, scale_ratio = "1" }\n'))
    assert ok.entries["QMTL"].basis_shift.scale_ratio == 1


TLT_REF = 'sec_type = "STK", asset_class = "ETF", symbol = "TLT", exchange = "SMART", currency = "USD"'


def tlt_entry(approval):
    return (f'version = 2\n[[mapping]]\nfamily = "TLT"\nbasis = "DIRECT"\n{approval}\n'
            f"reference = {{ {TLT_REF} }}\nexecution = {{ {TLT_REF} }}\n")


@pytest.mark.parametrize("approval", [
    # Reviewer probes: a string and an integer loaded as approved.
    'approved = "yes"\napproved_by = "o"\napproved_at = 2026-01-01T00:00:00Z',
    'approved = 1\napproved_by = "o"\napproved_at = 2026-01-01T00:00:00Z',
    'approved = "true"\napproved_by = "o"\napproved_at = 2026-01-01T00:00:00Z',
    'approved = true\napproved_by = ""\napproved_at = 2026-01-01T00:00:00Z',
    'approved = true\napproved_by = "   "\napproved_at = 2026-01-01T00:00:00Z',
    'approved = true\napproved_by = "o"\napproved_at = "2026-01-01T00:00:00Z"',
    'approved = true\napproved_by = "o"\napproved_at = 2026-01-01',
    'approved = true\napproved_by = "o"\napproved_at = 2026-01-01T00:00:00',  # no offset
    'approved = true\napproved_by = "o"\napproved_at = 2999-01-01T00:00:00Z',  # in the future
    'approved = true\napproved_by = "o"',
])
def test_f11_an_approval_must_be_a_toml_boolean_with_an_approver_and_a_time(tmp_path, approval):
    with pytest.raises(ValueError):
        load(tmp_path, tlt_entry(approval))


def test_f11_a_proper_approval_loads(tmp_path):
    p = load(tmp_path, tlt_entry('approved = true\napproved_by = "o"\n'
                                 'approved_at = 2026-01-01T00:00:00Z'))
    assert p.executable_assets == ["TLT"]
    assert load(tmp_path, tlt_entry("approved = false")).executable_assets == []


# ================================================================== same_series: source-series levels

ENERGY = """version = 2
[[mapping]]
family = "QNRG"
basis = "DIRECT"
approved = true
approved_by = "o"
approved_at = 2026-05-29T12:00:00Z
same_series = true
same_series_reason = "the micro QMOF trades at the standard QOF contract month's own price"
equivalent_roots = ["QOF", "QMOF"]
reference = { sec_type = "FUT", symbol = "QOF", exchange = "SIMNRG", currency = "USD" }
execution = { sec_type = "FUT", symbol = "QMOF", exchange = "SIMNRG", currency = "USD", multiplier = "10", min_tick = "0.01" }
contract_selection = { rule = "EXPLICIT_ONLY", min_business_days_to_roll = 6 }
contracts = [ { month = "202607", roll_date = 2026-07-06 }, { month = "202608", roll_date = 2026-08-05 } ]
sizing = { units_per_signal = 1, max_units = 2, max_notional = "5000" }
session = { policy = "REGULAR_ONLY", timezone = "UTC", days = ["MON"], regular = ["13:00-21:00"] }
roll = { business_days_before = 3, action = "FLATTEN" }
"""


def energy_action(action=Action.OPEN):
    """What the interpreter emits for a source-series code: no instrument, SOURCE_SERIES levels."""
    return opening("QNRG", action).model_copy(update={
        "instrument": None, "price_basis": PriceBasis.SOURCE_SERIES, "entry_reference": "71.50",
        "stop_price": "69.80" if action == Action.OPEN else None})


def test_same_series_direct_resolves_the_named_month_only(tmp_path):
    p = load(tmp_path, ENERGY)
    d, inst = p.resolve(energy_action(), clause="long QNRG Jul 2026 at 71.50", now=MAP_NOW)
    assert d.status == "SUPPORTED" and inst.symbol == "QMOF" and inst.futures_contract == "202607"
    for clause in ["long QNRG at 71.50", "long QNRG Sep '27 at 71.50", "long QNRG Aug at 71.5"]:
        d, inst = p.resolve(energy_action(), clause=clause, now=MAP_NOW)
        assert d.status == "UNRESOLVED" and inst is None, clause  # never another month


def energy_terms(**kw):
    base = {"family": "QNRG", "execution_symbol": "QXF", "same_series": True,
            "min_tick": Decimal("0.25")}
    return terms(**{**base, **kw})


def energy_act(action=Action.OPEN, **kw):
    a = act(action, family="QNRG", basis=PriceBasis.SOURCE_SERIES, **kw)
    return a


def test_same_series_direct_accepts_source_series_levels_in_the_execution_series():
    g = gate(energy_act(), mq(energy_terms()))
    assert g.status == "APPROVE" and "MAPPING_DIRECT" in g.reasons
    p = g.plan
    assert (p.execution_reference, p.stop, p.target) == (Decimal(5025), Decimal(4995),
                                                         Decimal(5085))
    # The usual checks, all in the execution series.
    assert gate(energy_act(entry="5200", stop="5150", target="5300"), mq(energy_terms())).reasons == ["ENTRY_PRICE_COLLAR"]
    assert gate(energy_act(stop="4995.1"), mq(energy_terms())).reasons == [
        "STOP_PRICE_OFF_TICK"]
    assert gate(energy_act(stop="5030"), mq(energy_terms())).reasons == [
        "STOP_DIRECTION_CONTRADICTION"]
    m = gate(energy_act(Action.MODIFY, idea="9:a1", stop="5000", target=None),
             mq(energy_terms()), position())
    assert m.status == "APPROVE" and m.plan.stop == Decimal(5000)


def test_direct_without_same_series_still_refuses_source_series_levels():
    g = gate(energy_act(), mq(energy_terms(same_series=False)))
    assert g.status == "HOLD" and g.reasons == ["SOURCE_TO_BROKER_PRICE_BASIS_UNRESOLVED"]
    g = gate(act(family="QNRG", basis=PriceBasis.UNKNOWN), mq(energy_terms()))
    assert g.reasons == ["SOURCE_TO_BROKER_PRICE_BASIS_UNRESOLVED"]


def test_same_series_reaches_the_gateway_terms(tmp_path):
    e = load(tmp_path, ENERGY).entries["QNRG"]
    inst = Instrument(asset_class=AssetClass.FUTURE, symbol="QMOF", futures_contract="202607")
    assert e.terms(inst, None, None).same_series is True
    tlt_e = MappingPolicy.from_toml(SHIPPED).entries["TLT"]
    assert tlt_e.same_series is False


# ================================================================== N1: what same_series may join

FUT_X = 'sec_type = "FUT", symbol = "QMOF", exchange = "SIMNRG", currency = "USD", multiplier = "10", min_tick = "0.01"'


def energy_with(reference, execution=FUT_X, extra=""):
    text = ENERGY.replace(
        'reference = { sec_type = "FUT", symbol = "QOF", exchange = "SIMNRG", currency = "USD" }',
        f"reference = {{ {reference} }}")
    text = text.replace(
        'execution = { sec_type = "FUT", symbol = "QMOF", exchange = "SIMNRG", currency = "USD", '
        'multiplier = "10", min_tick = "0.01" }', f"execution = {{ {execution} }}")
    return text.replace('equivalent_roots = ["QOF", "QMOF"]\n', extra)


@pytest.mark.parametrize("text", [
    # The reviewer's probes: an index, a spot commodity and a CFD declared the same series as a
    # future, and one ETF declared the same series as another.
    energy_with('sec_type = "IND", symbol = "QIDX", exchange = "SIMIDX", currency = "USD"'),
    energy_with('sec_type = "CMDTY", symbol = "QMS", exchange = "SIMSPOT", currency = "USD"'),
    energy_with('sec_type = "CFD", symbol = "QOC", exchange = "SIMCFD", currency = "USD"'),
    entry("SPX", "DIRECT", SPY, SPY.replace('"SPY"', '"IVV"'),
          'same_series = true\nsame_series_reason = "they track the same index"\n'),
    entry("SPY", "DIRECT", SPY.replace('"SMART"', '"ARCA"'), SPY,
          'same_series = true\nsame_series_reason = "one ETF, two venues"\n'),
    # Two futures with different roots and no equivalent_roots pair; another currency; a pair
    # that does not name both roots; a selection rule that could put the level in another month.
    energy_with('sec_type = "FUT", symbol = "QOF", exchange = "SIMNRG", currency = "USD"'),
    energy_with('sec_type = "FUT", symbol = "QOF", exchange = "SIMNRG", currency = "EUR"',
                extra='equivalent_roots = ["QOF", "QMOF"]\n'),
    energy_with('sec_type = "FUT", symbol = "QOF", exchange = "SIMNRG", currency = "USD"',
                extra='equivalent_roots = ["QOF", "QXX"]\n'),
    ENERGY.replace('rule = "EXPLICIT_ONLY"', 'rule = "EXPLICIT_OR_NEAREST"'),
])
def test_n1_same_series_refuses_anything_but_two_equivalent_futures(tmp_path, text):
    with pytest.raises(ValueError, match="same_series joins"):
        load(tmp_path, text)


def test_n1_a_micro_and_its_standard_future_may_be_declared_one_series(tmp_path):
    e = load(tmp_path, ENERGY).entries["QNRG"]
    assert e.same_series and e.equivalent_roots == ["QOF", "QMOF"]
    same_root = energy_with('sec_type = "FUT", symbol = "QMOF", exchange = "SIMNRG", '
                            'currency = "USD"')
    assert load(tmp_path, same_root).entries["QNRG"].same_series
    # A stock quoted in its own price needs no flag.
    assert not load(tmp_path, entry("SPY", "DIRECT", SPY, SPY)).entries["SPY"].same_series


# ================================================================== F2 (confirmation): default HOLD

def idx_open(entry="5010", stop="4980", target="5070", fraction=None):
    return opening("QIDX").model_copy(update={"entry_reference": entry, "stop_price": stop,
                                              "target_price": target,
                                              "close_fraction": fraction})


def idx(clause, action=None):
    return demo_policy_cached().resolve(action or idx_open(), clause=clause, now=MAP_NOW)


_POLICY_CACHE = {}


def demo_policy_cached():
    if "p" not in _POLICY_CACHE:
        import tempfile

        _POLICY_CACHE["p"] = demo_policy(Path(tempfile.mkdtemp()))
    return _POLICY_CACHE["p"]


@pytest.mark.parametrize("clause", [
    "We are now long $QIDX at 5,010. Stop 4,980. Target 5,070.",
    "We are now long $QIDX at $5,010.00, stop 4980, target 5,070!",
    "Long $QIDX at 5010 (stop 4980; target 5070).",
])
def test_f2c_a_clause_whose_only_digits_are_its_levels_uses_the_nearest_rule(clause):
    d, inst = idx(clause)
    assert d.status == "SUPPORTED" and inst.futures_contract == "202606", d.reason


@pytest.mark.parametrize("clause", [
    # The reviewer's three inputs: they executed in June.
    "We are now long $QIDX 09/26 at 5,010. Stop 4,980.",
    "We are now long $QIDX qifu6 at 5,010. Stop 4,980.",
    "We are now long $QIDX U 26 at 5,010. Stop 4,980.",
    # Anything else the bot has not understood: a percentage, a date, a time, a second price, a
    # count, a lower-case code without year, a bare month.
    "We are now long $QIDX at 5,010 with 2% of the book. Stop 4,980.",
    "We are now long $QIDX at 5,010 until 6/12. Stop 4,980.",
    "We are now long $QIDX at 5,010 from 14:30. Stop 4,980.",
    "We are now long $QIDX at 5,010, then 4,990. Stop 4,980.",
    "We are now long 3 $QIDX at 5,010. Stop 4,980.",
    "We are now long $QIDX qifu at 5,010. Stop 4,980.",
    "We are now long $QIDX sept at 5,010. Stop 4,980.",
    "We are now long $QIDX at 5,010. Stop 4,980. Target 5,070 and 5,100.",
])
def test_f2c_any_leftover_token_holds_rather_than_guess(clause):
    d, inst = idx(clause)
    assert d.status == "UNRESOLVED" and inst is None, clause
    assert d.reason.split(",")[0] == "CONTRACT_MONTH_UNRECOGNISED"


@pytest.mark.parametrize("clause", [
    "We are now long $QIDX Sep 2026 at 5,010. Stop 4,980.",
    "We are now long $QIDX (QIFU6) at 5,010. Stop 4,980.",
    "We are now long $QIDX 2026-09 at 5,010. Stop 4,980.",
])
def test_f2c_a_recognised_month_beside_the_levels_still_resolves(clause):
    d, inst = idx(clause)
    assert d.status == "SUPPORTED" and inst.futures_contract == "202609", d.reason


def test_f2c_a_percentage_that_is_the_reduction_is_consumed(tmp_path):
    held = {"9:a1": ContractRef(symbol="QIF", asset_class=AssetClass.FUTURE, sec_type="FUT",
                                expiry="20260619", con_id=1)}
    red = opening("QIDX", Action.REDUCE, idea="9:a1").model_copy(
        update={"close_fraction": "0.5", "entry_reference": None})
    d, inst = demo_policy(tmp_path).resolve(red, clause="Selling 50% of our $QIDX long at 5,030.",
                                            now=MAP_NOW, held=held)
    assert d.status == "SUPPORTED" and inst.futures_contract == "202606"


# ================================================================== F3 (confirmation): whole post

OPT_OPEN = "We are now long $QSTK 2026-09-18 105 call at 4.22. Stop 3.40."


def opt_resolve(tmp_path, post, action=None):
    """The action's clause is its own sentence; the rest of the post is elsewhere."""
    return demo_policy(tmp_path).resolve(action or opening("QSTK"), clause=OPT_OPEN, now=MAP_NOW,
                                         post_text=post)


@pytest.mark.parametrize("rest", [
    # The reviewer's input: the second leg in its own clause.
    "Sell the 110 against it at 1.10.",
    "Selling the 110 call against it.",
    "Writing the 110 calls against it.",
    "Short the 2026-12-18 105 call.",
    "Also long the 110 call at 1.20.",
    "Also buying the 105 put at 2.00.",
    "It is half of an iron fly.",
    "Think of it as a risk reversal.",
    "Sold the 95 put to pay for it.",
])
def test_f3c_a_second_leg_anywhere_in_the_post_holds(tmp_path, rest):
    d, inst = opt_resolve(tmp_path, f"{OPT_OPEN} {rest}")
    assert d.status == "UNRESOLVED" and inst is None, rest
    assert d.reason.split(",")[0] == "OPTION_STRATEGY_NOT_SINGLE_LEG"


def test_f3c_a_post_with_one_strike_expiry_and_right_executes(tmp_path):
    d, inst = opt_resolve(tmp_path, f"{OPT_OPEN} Target 6.00. Risk is the premium.")
    assert d.status == "SUPPORTED" and (inst.strike, inst.option_right) == ("105", "CALL")


def test_f3c_the_whole_post_reaches_the_policy_from_the_service(tmp_path):
    run = run_steps(tmp_path, [Step(0, "We are now long $QSTK 2026-09-18 105 call at 4.22. "
                                       "Stop 3.40. Sell the 110 against it at 1.10.", "HOLD")],
                    family="QSTK.OPT")
    acts = traded(run)
    assert acts and all(a["status"] != "EXECUTED" for a in acts)
    assert run.broker.submit_calls == 0
    run.close()


# ================================================================== verbs are not option language

@pytest.mark.parametrize("clause", [
    "We are now long TLT at 90. Put a stop at 88.",  # the reviewer's input, as main executes it
    "Long TLT at 90, put in a stop at 88.",
    "We put on a TLT long at 90. Stop 88.",
    "Buying TLT at 90; put it on with a stop at 88.",
    "Putting a stop at 88 on our new TLT long at 90.",
    "Call it a starter: long TLT at 90. Stop 88.",
    "A close call, but we are long TLT at 90. Stop 88.",
    "Calling the low: long TLT at 90. Stop 88.",
    "As called last week, long TLT at 90. Stop 88.",
    "Call me cautious: long TLT at 90, stop 88.",
])
def test_verb_and_idiom_uses_of_call_and_put_buy_the_stock_as_on_main(clause):
    d, inst = MappingPolicy.from_toml(SHIPPED).resolve(tlt(Action.OPEN, idea=None), clause=clause,
                                                       now=MAP_NOW)
    assert d.status == "SUPPORTED" and d.family == "TLT" and inst.symbol == "TLT", clause


@pytest.mark.parametrize("clause", [
    "Buying TLT calls at 1.20.", "Long the TLT puts at 2.10.", "Buying a TLT call at 1.20.",
    "Buying TLT 92 puts at 2.10.", "Buying TLT 2026-09-18 call at 1.20.",
    "Buying TLT options at 1.20.", "Buying the TLT 92 strike at 1.20.",
])
def test_a_noun_or_unclear_use_of_call_or_put_on_an_open_still_holds(clause):
    d, inst = MappingPolicy.from_toml(SHIPPED).resolve(tlt(Action.OPEN, idea=None), clause=clause,
                                                       now=MAP_NOW)
    assert d.family == "TLT.OPT" and d.status != "SUPPORTED" and inst is None, clause
