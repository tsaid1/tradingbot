"""The owner's entry-rule decision of 12 November 2026, quote and account side (invented data):

3. Live streaming quotes only, each side fresh: the gateway and the execution core's pre-submit
   re-check refuse a delayed, frozen, missing, off-tick, locked, crossed or stale side, and a quote
   without per-side update times.
4. Paper and simulation only: an anchored entry reaches only a broker that positively declares SIM
   or PAPER (checked before the intent is recorded and at the last step before submission); the
   mapping file's ``environment`` accepts "PAPER" only; the runtime refuses the rule outside its
   allow-listed modes. The shipped TLT entry carries exactly the owner's values.
"""

from __future__ import annotations

import re
from datetime import timedelta
from decimal import Decimal

import pytest
from execution_harness import SYMBOL, Harness, open_action, source
from runtime_harness import MAPPING, Runtime, post
from test_entry_anchor_policy import ANCHOR_RULE, RULE, etf_entry, q_with
from test_multi_asset_gateway import NOW, act, gate, leg, mq, terms

from tradingbot.execution.broker import (
    BrokerEnvironment,
    BrokerQuote,
    ContractRef,
    QuoteQuality,
    broker_environment,
)
from tradingbot.execution.coordinator import ENTRY_ANCHOR_PAPER_ONLY
from tradingbot.execution.store import IntentState
from tradingbot.models import AssetClass, ExecutionPlan, GateDecision, PositionSide
from tradingbot.runtime.mapping import ANCHOR_RULE_MODES, MappingPolicy
from tradingbot.validator import ANCHOR_MAX_QUOTE_AGE_CEILING_SECONDS

# ================================================================== 3. live quotes, per side


def stamped(t, *, bid_age=0.0, ask_age=0.0, bid_at=True, ask_at=True, **kw):
    q = q_with(t, "5025", **kw)
    return q.model_copy(update={
        "bid_at": NOW - timedelta(seconds=bid_age) if bid_at else None,
        "ask_at": NOW - timedelta(seconds=ask_age) if ask_at else None})


def test_a_fresh_live_quote_carries_the_anchor_and_the_plan_records_the_rule():
    g = gate(act(entry=None), stamped(terms(**RULE), bid_age=3, ask_age=3))  # at the limit
    assert g.status == "APPROVE", g.reasons
    assert g.plan.anchor_max_quote_age_seconds == 3


@pytest.mark.parametrize("change,reasons", [
    ({"bid_at": False}, ["ENTRY_ANCHOR_QUOTE_SIDE_TIME_UNKNOWN", "SIDE:BID"]),
    ({"ask_at": False}, ["ENTRY_ANCHOR_QUOTE_SIDE_TIME_UNKNOWN", "SIDE:ASK"]),
    ({"bid_age": 3.5}, ["ENTRY_ANCHOR_QUOTE_SIDE_STALE", "SIDE:BID", "MAX_AGE:3"]),
    ({"ask_age": 4}, ["ENTRY_ANCHOR_QUOTE_SIDE_STALE", "SIDE:ASK", "MAX_AGE:3"]),
    ({"ask_age": -10}, ["ENTRY_ANCHOR_QUOTE_SIDE_FUTURE", "SIDE:ASK"]),
    ({"bid": "5025.25"}, ["ENTRY_ANCHOR_QUOTE_LOCKED_OR_CROSSED"]),  # locked
    ({"bid": "5024.80", "ask": "5025.30"}, ["ENTRY_ANCHOR_QUOTE_OFF_TICK"]),
])
def test_each_side_must_be_present_on_tick_uncrossed_and_fresh(change, reasons):
    g = gate(act(entry=None), stamped(terms(**RULE), **change))
    assert g.status == "HOLD" and g.plan is None
    assert g.reasons == [*reasons, "LEG:EXECUTION"], g.reasons


@pytest.mark.parametrize("quality", ["DELAYED", "FROZEN"])
def test_a_delayed_or_frozen_quote_is_never_eligible_even_where_accepted(quality):
    t = terms(**RULE, accepted_qualities=["LIVE", "DELAYED", "FROZEN"])
    g = gate(act(entry=None), stamped(t, quality=quality))
    assert g.status == "HOLD" and g.reasons[0] == "ENTRY_ANCHOR_QUOTE_NOT_LIVE", g.reasons
    # The same quote still serves an explicit entry exactly as before (the rule never touches it).
    assert gate(act(), stamped(t, quality=quality)).status == "APPROVE"


def test_the_reference_quote_needs_fresh_sides_too():
    t = terms("BASIS_SHIFT", **RULE)
    a = act(entry=None, stop="4980", target="5070")
    assert gate(a, q_with(t, "5010", reference=leg())).status == "APPROVE"
    stale = leg().model_copy(update={"ask_at": NOW - timedelta(seconds=4)})
    g = gate(a, q_with(t, "5010", reference=stale))
    assert g.reasons == ["ENTRY_ANCHOR_QUOTE_SIDE_STALE", "SIDE:ASK", "MAX_AGE:3",
                         "LEG:REFERENCE"], g.reasons
    one_sided = leg(ask=None)
    g = gate(a, q_with(t, "5010", reference=one_sided))
    assert g.reasons == ["ENTRY_ANCHOR_QUOTE_SIDE_MISSING", "LEG:REFERENCE"], g.reasons


def test_a_rule_without_its_quote_age_or_above_the_ceiling_holds():
    t = terms(**{**RULE, "anchor_max_quote_age_seconds": None})
    assert gate(act(entry=None), stamped(t)).reasons == ["ENTRY_ANCHOR_RULE_INCOMPLETE"]
    t = terms(**{**RULE, "anchor_max_quote_age_seconds": ANCHOR_MAX_QUOTE_AGE_CEILING_SECONDS + 1})
    assert gate(act(entry=None), stamped(t)).reasons[0] == "ENTRY_ANCHOR_LIMIT_ABOVE_CEILING"


def test_an_explicit_entry_needs_no_side_times():
    g = gate(act(), stamped(terms(**RULE), bid_at=False, ask_at=False))
    assert g.status == "APPROVE" and g.plan.entry_rule == "EXPLICIT_ENTRY_ONLY"


def test_the_limits_are_measured_to_the_side_the_order_trades_against():
    """A long is compared with the ask, a short with the bid (0.2% of 5025 is 10.05 points)."""
    t = terms(**RULE)
    wide = {"bid": "5013.00", "ask": "5025.25"}  # the bid 12 below the anchor, the ask at it
    long = gate(act(entry=None), stamped(t, **wide))
    assert long.status == "APPROVE" and long.plan.anchor_live == Decimal("5025.25")
    short = gate(act(entry=None, side=PositionSide.SHORT, stop="5055", target="4965"),
                 stamped(t, **wide))
    assert short.status == "HOLD" and short.reasons[0] == "ENTRY_ANCHOR_ADVERSE_MOVE"  # the bid
    away = {"bid": "5024.75", "ask": "5036.00"}  # only the ask ran away
    assert gate(act(entry=None), stamped(t, **away)).reasons[0] == "ENTRY_ANCHOR_ADVERSE_MOVE"
    s = gate(act(entry=None, side=PositionSide.SHORT, stop="5055", target="4965"),
             stamped(t, **away))
    assert s.status == "APPROVE" and s.plan.anchor_live == Decimal("5024.75"), s.reasons


def test_old_quote_json_without_side_times_still_loads():
    c = ContractRef(symbol="QETF", asset_class=AssetClass.ETF)
    raw = BrokerQuote(contract=c, bid=Decimal(1), ask=Decimal(2), received_at=NOW,
                      quality=QuoteQuality.LIVE).model_dump_json(exclude={"bid_at", "ask_at"})
    assert "bid_at" not in raw
    q = BrokerQuote.model_validate_json(raw)
    assert q.bid_at is None and q.ask_at is None
    assert mq(terms()).model_copy(update={"bid_at": None}).bid_at is None


def test_the_simulated_broker_stamps_each_side(tmp_path):
    h = Harness(tmp_path)
    c = h.broker.resolve(open_action().instrument)
    at = h.clock.now()
    h.broker.set_quote(SYMBOL, "49.98", "50.02", received_at=at,
                       ask_at=at - timedelta(seconds=7))
    q = h.broker.quote(c)
    assert q.bid_at == at and q.ask_at == at - timedelta(seconds=7)
    h.broker.reports_side_times = False
    assert h.broker.quote(c).bid_at is None
    h.coord.close()


# ------------------------------------------------------------------ the execution core


def anchored_gate(limit="50.02", age=3.0, rule="REFERENCE_AS_COLLAR") -> GateDecision:
    plan = ExecutionPlan(family=SYMBOL, basis="DIRECT", execution_key=SYMBOL, quantity=1,
                         execution_reference=Decimal(limit), stop=Decimal(48),
                         target=Decimal(55), entry_rule=rule,
                         entry_anchor=Decimal(50) if rule == "REFERENCE_AS_COLLAR" else None,
                         anchor_series="EXECUTION" if rule == "REFERENCE_AS_COLLAR" else None,
                         anchor_live=Decimal(limit) if rule == "REFERENCE_AS_COLLAR" else None,
                         entry_limit=Decimal(limit) if rule == "REFERENCE_AS_COLLAR" else None,
                         anchor_max_quote_age_seconds=age if rule == "REFERENCE_AS_COLLAR"
                         else None,
                         anchor_max_signal_age_seconds=90 if rule == "REFERENCE_AS_COLLAR"
                         else None)
    return GateDecision(status="APPROVE", reasons=["PAPER_OPEN"], plan=plan)


def run_entry(h: Harness, g: GateDecision, sid="p1"):
    a = open_action(entry=None)
    return h.coord.execute(source(sid, at=h.clock.now()), a, g, now=h.clock.now())


def test_the_execution_core_sends_an_anchored_limit_on_a_fresh_quote(tmp_path):
    h = Harness(tmp_path)
    out = run_entry(h, anchored_gate())
    assert h.broker.submit_calls == 1 and out.state != IntentState.REFUSED, out.reasons
    [entry] = [o for o in h.broker.all_orders() if o.parent_ref is None]
    assert entry.order_type.value == "LMT" and entry.limit_price == Decimal("50.02")
    h.coord.close()


@pytest.mark.parametrize("quote,reason", [
    ({"ask_at": -4}, "ENTRY_ANCHOR_QUOTE_SIDE_STALE"),  # the ask has not ticked for 4 s
    ({"quality": QuoteQuality.DELAYED}, "BROKER_QUOTE_QUALITY_DELAYED"),
    ({"bid": "50.02"}, "ENTRY_ANCHOR_QUOTE_LOCKED_OR_CROSSED"),
    ({"times": False}, "ENTRY_ANCHOR_QUOTE_SIDE_TIME_UNKNOWN"),
])
def test_the_pre_submit_recheck_applies_the_same_per_side_rule(tmp_path, quote, reason):
    h = Harness(tmp_path)
    at = h.clock.now()
    if quote.pop("times", True) is False:
        h.broker.reports_side_times = False
    ask_at = at + timedelta(seconds=quote.pop("ask_at")) if "ask_at" in quote else None
    h.broker.set_quote(SYMBOL, quote.pop("bid", "49.98"), "50.02", received_at=at, ask_at=ask_at,
                       **quote)
    out = run_entry(h, anchored_gate())
    assert out.state == IntentState.REFUSED and out.reasons[0] == reason, out.reasons
    assert h.broker.submit_calls == 0
    # A non-anchored entry on the same quote is decided exactly as before.
    if reason != "BROKER_QUOTE_QUALITY_DELAYED":
        plain = run_entry(h, anchored_gate(rule="EXPLICIT_ENTRY_ONLY"), sid="p2")
        assert plain.state != IntentState.REFUSED, plain.reasons
    h.coord.close()


def test_an_anchored_plan_without_its_quote_age_is_refused(tmp_path):
    h = Harness(tmp_path)
    out = run_entry(h, anchored_gate(age=None))
    assert out.reasons == ["ENTRY_ANCHOR_RULE_INCOMPLETE"] and h.broker.submit_calls == 0
    h.coord.close()


# ================================================================== 4. paper only


def test_the_environment_is_a_closed_declaration():
    assert {e.value for e in BrokerEnvironment} == {"SIM", "PAPER", "LIVE"}

    class Undeclared:
        pass

    class Odd:
        def environment(self):
            return "FUNDED"

    class Paper:
        def environment(self):
            return BrokerEnvironment.PAPER

    assert broker_environment(Undeclared()) is None and broker_environment(Odd()) is None
    assert broker_environment(Paper()) == BrokerEnvironment.PAPER


def test_the_simulated_broker_declares_sim(tmp_path):
    h = Harness(tmp_path)
    assert broker_environment(h.broker) == BrokerEnvironment.SIM
    h.coord.close()


@pytest.mark.parametrize("declared,label", [(lambda: BrokerEnvironment.LIVE, "LIVE"),
                                            (None, "UNDECLARED"), (lambda: "FUNDED", "UNDECLARED")])
def test_an_anchored_entry_never_reaches_a_live_or_undeclared_broker(tmp_path, declared, label):
    h = Harness(tmp_path)
    if declared is None:
        h.broker.environment = None  # an adapter without the declaration
    else:
        h.broker.environment = declared
    out = run_entry(h, anchored_gate())
    assert out.state == IntentState.REFUSED and h.broker.submit_calls == 0
    assert out.reasons == [ENTRY_ANCHOR_PAPER_ONLY, f"ENVIRONMENT:{label}"]
    [exc] = [e for e in h.coord.store.exceptions(open_only=True)
             if e.kind == ENTRY_ANCHOR_PAPER_ONLY]
    assert label in exc.detail
    # A non-anchored entry is unaffected.
    plain = run_entry(h, anchored_gate(rule="EXPLICIT_ENTRY_ONLY"), sid="p2")
    assert plain.state != IntentState.REFUSED and h.broker.submit_calls == 1, plain.reasons
    h.coord.close()


def test_the_last_step_before_submission_checks_again(tmp_path):
    """An adapter whose declaration changes after the pre-trade checks is still refused."""
    h = Harness(tmp_path)
    answers = iter([BrokerEnvironment.PAPER])
    h.broker.environment = lambda: next(answers, BrokerEnvironment.LIVE)
    out = run_entry(h, anchored_gate())
    assert out.state == IntentState.REFUSED and h.broker.submit_calls == 0
    assert out.reasons[0] == ENTRY_ANCHOR_PAPER_ONLY
    assert all(o.status in ("NotSubmitted",) for o in h.coord.store.orders(out.order_ref))
    h.coord.close()


@pytest.mark.parametrize("environment", ['"LIVE"', '"FUNDED"', '"paper"', '""', "true"])
def test_the_mapping_file_accepts_only_paper(tmp_path, environment):
    f = tmp_path / "m.toml"
    f.write_text("version = 2\n" + etf_entry(
        rule=ANCHOR_RULE.replace('environment = "PAPER"', f"environment = {environment}")))
    with pytest.raises(ValueError, match="environment"):
        MappingPolicy.from_toml(f)
    f.write_text("version = 2\n" + etf_entry(rule=ANCHOR_RULE.replace(', environment = "PAPER"',
                                                                      "")))
    with pytest.raises(ValueError, match="environment"):
        MappingPolicy.from_toml(f)


def test_the_quote_age_is_required_and_capped_on_load(tmp_path):
    f = tmp_path / "m.toml"
    for text, message in ((", max_quote_age_seconds = 3", "max_quote_age_seconds"),
                          ("max_quote_age_seconds = 3", "ceiling")):
        rule = ANCHOR_RULE.replace(text, "" if text.startswith(",") else
                                   "max_quote_age_seconds = 6")
        f.write_text("version = 2\n" + etf_entry(rule=rule))
        with pytest.raises(ValueError, match=message):
            MappingPolicy.from_toml(f)


def test_the_rule_runs_only_in_allow_listed_modes():
    # Both allow-listed modes simulate every order and fill (shadow: at read-only real quotes);
    # tests/test_ibkr_paper_disabled_by_default.py proves neither can build an order path.
    assert ANCHOR_RULE_MODES == ("mock", "shadow")
    p = MappingPolicy.from_toml(MAPPING)
    p.check_mode("mock")
    p.check_mode("shadow")
    for mode in ("paper", "live", "ibkr", "MOCK", "SHADOW", "shadow-live", "paper-test-1"):
        with pytest.raises(ValueError, match="paper and simulation only"):
            p.check_mode(mode)


def test_the_service_checks_the_mode_on_start(tmp_path, monkeypatch):
    seen = []
    original = MappingPolicy.check_mode
    monkeypatch.setattr(MappingPolicy, "check_mode",
                        lambda self, mode: seen.append(mode) or original(self, mode))
    rt = Runtime(tmp_path, [], mapping_file=MAPPING)
    assert seen == ["mock"]
    rt.service.close()


def test_the_shipped_mapping_gives_the_rule_to_the_bond_etf_only_with_the_owners_values():
    text = MAPPING.read_text()
    active = text.split("templates (not active)")[0]
    assert len(re.findall(r'^entry_rule = "REFERENCE_AS_COLLAR"$', active, re.MULTILINE)) == 1
    p = MappingPolicy.from_toml(MAPPING)
    [family] = [f for f, e in p.entries.items() if e.entry_rule == "REFERENCE_AS_COLLAR"]
    e = p.entries[family]
    assert family == "TLT" and e.entry_anchor.environment == "PAPER"
    assert re.search(r"paper and simulation only; does not authorize broker order submission",
                     e.note)


# ------------------------------------------------------------------ the whole service path


TLT_ANCHORED = "We are now long $TLT, 88.42 now. Stop 87.20."


def test_the_shipped_bond_etf_enters_one_share_at_the_ask_on_the_simulated_broker(tmp_path):
    rt = Runtime(tmp_path, [post("1", 0, TLT_ANCHORED)], mapping_file=MAPPING)
    rt.run(3)
    [a] = [a for a in rt.actions() if a["action"] == "OPEN"]
    assert a["status"] == "EXECUTED", (a["status"], a["reasons"], a["mapping_reason"])
    [entry] = [o for o in rt.broker.all_orders() if o.parent_ref is None]
    assert entry.order_type.value == "LMT" and entry.limit_price == Decimal("88.42")
    assert entry.quantity == 1
    assert any(o.order_type.value == "STP" for o in rt.broker.all_orders())
    rt.service.close()


def test_the_shipped_bond_etf_never_reaches_a_broker_declaring_live(tmp_path):
    rt = Runtime(tmp_path, [post("1", 0, TLT_ANCHORED)], mapping_file=MAPPING)
    rt.broker.environment = lambda: BrokerEnvironment.LIVE
    rt.run(3)
    [a] = [a for a in rt.actions() if a["action"] == "OPEN"]
    assert a["status"] != "EXECUTED" and rt.broker.submit_calls == 0
    assert ENTRY_ANCHOR_PAPER_ONLY in a["reasons"]
    rt.service.close()
