"""Fix round of the independent safety review of the entry rule (PR #66), on invented text only.

B1  the neighbour of an elliptical level is decided by an ALLOW-list (the action's own clause, or
    the whole-sentence allow-list SENTENCE_STRICT applies to a preceding sentence); the level- and
    cost-word deny-list only adds holds.
B1b optional ``level_after_own_protective_levels`` (default false): a sentence stating only the
    action's own stop and target may precede the level.
B2  an unknown market in the neighbour holds; the mention detector reads all-caps words of six or
    more letters, ``/`` and ``.`` parts and ``#WORD``.
S1  per-side quote times may be at most 0.5 s in the future.
S2  recovery of a never-submitted anchored entry applies the per-side rule and the plan's limit.
S3  an ``environment()`` that raises is undeclared.
S4  quotation marks and approximation or sign marks in the level's own clause hold.
N1  strict, finite anchor limits.  N2 the shipped mapping keeps end-to-end lifecycle coverage.
N3  anchored ADD under the paper-only lock.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from execution_harness import SYMBOL, Harness, action, open_action, source
from runtime_harness import CLOSE, ENTRY, MAPPING, RAISE_STOP, SERVICE_MAPPING, Runtime, post
from test_entry_anchor_policy import ANCHOR_RULE, MAP_NOW, RULE, etf_entry, policy, q_with
from test_entry_anchor_policy import action as etf_action
from test_entry_anchor_single_market import note
from test_entry_rule_paper_only import anchored_gate, run_entry
from test_multi_asset_gateway import NOW, act, gate, terms

import tradingbot.validator as V
from tradingbot.event_models import ParsedPost
from tradingbot.execution.broker import BrokerEnvironment, broker_environment
from tradingbot.execution.coordinator import ENTRY_ANCHOR_PAPER_ONLY
from tradingbot.execution.store import IntentState
from tradingbot.models import Action, GateDecision
from tradingbot.runtime.instruction_terms import MarketMention, unresolved_market_mentions
from tradingbot.runtime.mapping import MappingPolicy

NOT_QUOTE = "ENTRY_ANCHOR_LEVEL_NOT_MARKET_QUOTE"
NEIGHBOUR = "NEIGHBOUR_NOT_UNDERSTOOD"


def shipped_run(text):
    """One invented post through the whole mock service on the SHIPPED mapping."""
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as d:
        rt = Runtime(Path(d), [post("1", 0, text)], mapping_file=MAPPING)
        rt.run(3)
        out = [(a["action"], a["status"], a["reasons"]) for a in rt.actions()
               if a["action"] == "OPEN"]
        sent = rt.broker.submit_calls
        rt.service.close()
        return sent, out


# ================================================================== B1: the neighbour allow-list

B1_NEIGHBOURS = ["Our purchase price:", "Last Friday:", "A month ago:", "Where we would get out:",
                 "For the old lot:", "The put:", "Bid:", "Our cost:\n\nThe chart looks fine."]


@pytest.mark.parametrize("neighbour", B1_NEIGHBOURS)
def test_b1_an_elliptical_level_after_an_unknown_neighbour_never_trades(neighbour):
    sent, out = shipped_run(f"We are now long $TLT.\n{neighbour}\n88.42 now.\nStop 87.20.")
    assert sent == 0 and out and out[0][1] != "EXECUTED", out


@pytest.mark.parametrize("neighbour", B1_NEIGHBOURS)
@pytest.mark.parametrize("scope", ["SINGLE_MARKET_POST", "ACTION_BLOCK", "SENTENCE_STRICT"])
def test_b1_the_mapping_holds_them_in_every_scope(tmp_path, neighbour, scope):
    p = policy(tmp_path, etf_entry(rule=ANCHOR_RULE.replace("SENTENCE_STRICT", scope)))
    d = decide(p, "We are now long $QETF.", f"We are now long $QETF.\n{neighbour}\n50.10 now.")
    assert d.status == "UNRESOLVED" and d.entry_anchor is None, d.reason


def test_b1_the_single_market_hold_names_the_neighbour(tmp_path):
    p = policy(tmp_path, etf_entry(rule=ANCHOR_RULE.replace("SENTENCE_STRICT",
                                                            "SINGLE_MARKET_POST")))
    d = decide(p, "We are now long $QETF.", "We are now long $QETF.\nLast Friday:\n50.10 now.")
    assert d.reason.split(",")[:3] == [NOT_QUOTE, "LEVEL_KIND:AMBIGUOUS", NEIGHBOUR], d.reason


@pytest.mark.parametrize("text", [
    "We are now long $TLT.\n88.42 now.\nStop 87.20.",  # after the action's own clause
    "We are now long $TLT, 88.42 now. Stop 87.20.",  # in the instruction's own sentence
])
def test_b1_the_plain_shapes_still_trade(text):
    sent, out = shipped_run(text)
    assert sent == 1 and out[0][1] == "EXECUTED", out


def decide(p, clause, post_text, a=None, levels=(("REFERENCE", "50.10"), ("STOP", "48.90"))):
    _, [d, _] = p.apply(ParsedPost(actions=[a or etf_action(), note()]),
                        clauses={"a1": clause, "n1": ""}, now=MAP_NOW, post_text=post_text,
                        levels={"a1": list(levels)})
    return d


def test_b1_the_deny_list_only_adds_holds(tmp_path):
    """An allow-listed neighbour holding a stop word still holds (STOP_OR_TARGET)."""
    p = policy(tmp_path, etf_entry(rule=ANCHOR_RULE.replace("SENTENCE_STRICT",
                                                            "SINGLE_MARKET_POST")))
    d = decide(p, "We are now long $QETF.", "We are now long $QETF.\nStop 48.90.\n50.10 now.")
    assert d.reason.split(",")[:2] == [NOT_QUOTE, "LEVEL_KIND:STOP_OR_TARGET"], d.reason
    ok = decide(p, "We are now long $QETF.", "We are now long $QETF.\nWe are long.\n50.10 now.")
    assert ok.status == "SUPPORTED", ok.reason


# ================================================================== B1b: own protective levels

KEY = ANCHOR_RULE.replace("SENTENCE_STRICT", "SINGLE_MARKET_POST").replace(
    'environment = "PAPER"', 'environment = "PAPER", level_after_own_protective_levels = true')
LONG = "We are now long $QETF."


def keyed(tmp_path, on=True):
    return policy(tmp_path, etf_entry(rule=KEY if on else KEY.replace(
        "level_after_own_protective_levels = true", "level_after_own_protective_levels = false")))


def with_target(target="52.40"):
    return etf_action().model_copy(update={"target_price": target})


@pytest.mark.parametrize("neighbour,a", [
    ("Stop 48.90.", None),
    ("Our stop is at 48.90.", None),
    ("Stop-loss 48.90.", None),
    ("Stop 48.90, target 52.40.", "target"),
    ("Protective stop at 48.90 and a target of 52.40.", "target"),
])
def test_b1b_the_plain_protective_shapes_anchor_with_the_key_on(tmp_path, neighbour, a):
    act_ = with_target() if a else None
    levels = [("REFERENCE", "50.10"), ("STOP", "48.90")] + ([("TARGET", "52.40")] if a else [])
    post_text = f"{LONG}\n{neighbour}\n50.10 now."
    d = decide(keyed(tmp_path), LONG, post_text, act_, levels)
    assert d.status == "SUPPORTED" and d.entry_anchor == "50.10", d.reason
    assert "AFTER_OWN_PROTECTIVE_LEVELS" in d.anchor_attribution
    off = decide(keyed(tmp_path, on=False), LONG, post_text, act_, levels)
    assert off.status == "UNRESOLVED", off.reason


@pytest.mark.parametrize("neighbour,anchor", [
    ("We raised our stop.", "50.10"),  # no value
    ("Stop moved to 48.90.", "50.10"),  # moved
    ("Stop 48.70.", "50.10"),  # not the action's own stop
    ("Stop 48.90.", "48.50"),  # the stop is not below a long's level
    ("Stop 48.90, 2 lots.", "50.10"),  # an extra number
    ("Stop 48.90 today.", "50.10"),  # a time word
    ("Stop 48.90 if needed.", "50.10"),  # a condition word
    ("Stop 48.90 please.", "50.10"),  # an unknown word
    ("Stop 48.90?", "50.10"),  # a question
    ("Stop 48.90 on Dec 2026.", "50.10"),  # a month
])
def test_b1b_the_key_still_holds_everything_else(tmp_path, neighbour, anchor):
    d = decide(keyed(tmp_path), LONG, f"{LONG}\n{neighbour}\n{anchor} now.",
               levels=[("REFERENCE", anchor), ("STOP", "48.90")])
    assert d.status == "UNRESOLVED" and d.entry_anchor is None, (neighbour, d.reason)


def test_b1b_a_protective_sentence_that_is_not_the_neighbour_holds(tmp_path):
    d = decide(keyed(tmp_path), LONG, f"{LONG}\nStop 48.90.\nThe chart is fine.\n50.10 now.")
    assert d.status == "UNRESOLVED", d.reason


def test_b1b_a_short_needs_its_stop_above_and_target_below(tmp_path):
    from tradingbot.models import PositionSide
    short = etf_action().model_copy(update={"position_side": PositionSide.SHORT,
                                            "stop_price": "51.20", "target_price": "48.00"})
    lv = [("REFERENCE", "50.10"), ("STOP", "51.20"), ("TARGET", "48.00")]
    ok = decide(keyed(tmp_path), "We are now short $QETF.",
                "We are now short $QETF.\nStop 51.20, target 48.00.\n50.10 now.", short, lv)
    assert ok.status == "SUPPORTED", ok.reason
    wrong = short.model_copy(update={"target_price": "52.00"})
    lv2 = [("REFERENCE", "50.10"), ("STOP", "51.20"), ("TARGET", "52.00")]
    bad = decide(keyed(tmp_path), "We are now short $QETF.",
                 "We are now short $QETF.\nStop 51.20, target 52.00.\n50.10 now.", wrong, lv2)
    assert bad.status == "UNRESOLVED", bad.reason


@pytest.mark.parametrize("value,ok", [("true", True), ("false", True), ('"yes"', False),
                                      ("1", False)])
def test_b1b_the_key_is_a_strict_boolean(tmp_path, value, ok):
    text = etf_entry(rule=KEY.replace("level_after_own_protective_levels = true",
                                      f"level_after_own_protective_levels = {value}"))
    if ok:
        policy(tmp_path, text)
    else:
        with pytest.raises(ValueError, match="level_after_own_protective_levels"):
            policy(tmp_path, text)


def test_b1b_the_key_needs_the_rule(tmp_path):
    rule = ('entry_anchor = { max_adverse_move = "0.5%", max_favourable_move = "1%", '
            'max_signal_age_seconds = 60, anchor_scope = "SINGLE_MARKET_POST", '
            'max_quote_age_seconds = 3, environment = "PAPER", '
            'level_after_own_protective_levels = true }')
    with pytest.raises(ValueError, match="REFERENCE_AS_COLLAR"):
        policy(tmp_path, etf_entry(rule=rule))


def test_b1b_the_key_is_off_for_every_shipped_family():
    for e in MappingPolicy.from_toml(MAPPING).entries.values():
        if getattr(e, "entry_anchor", None) is not None:
            assert e.entry_anchor.level_after_own_protective_levels is False


# ================================================================== B2: unknown markets


@pytest.mark.parametrize("neighbour", ["Qorvex is heavy.", "EURUSD is heavy.", "spy is heavy.",
                                       "The ten-year is heavy.", "EUR/USD:"])
def test_b2_an_unknown_market_before_an_elliptical_level_never_trades(neighbour):
    sent, out = shipped_run(f"We are now long $TLT.\n{neighbour}\n88.42 now.\nStop 87.20.")
    assert sent == 0 and out[0][1] != "EXECUTED", out


@pytest.mark.parametrize("text,kind", [
    ("QORVEXA is heavy.", MarketMention.TICKER_SHAPED),  # 6+ capitals, even sentence-initial
    ("We watch EURUSD too.", MarketMention.TICKER_SHAPED),
    ("We watch EUR/USD too.", MarketMention.TICKER_SHAPED),  # split on "/"
    ("We watch BRK.B too.", MarketMention.TICKER_SHAPED),  # split on "."
    ("We watch #IEF too.", MarketMention.TICKER_SHAPED),  # "#WORD" read as WORD
])
def test_b2_the_detector_reads_more_ticker_shapes(text, kind):
    assert kind in unresolved_market_mentions(text, {"qetf"}, {"QETF"}, {"QETF"})


@pytest.mark.parametrize("text", ["We are CURRENTLY long.", "STOP is set.", "We watch $QETF/USD."])
def test_b2_understood_capitals_and_units_do_not_trigger(text):
    assert unresolved_market_mentions(text, {"qetf"}, {"QETF"}, {"QETF"}) == []


def test_b2_the_residual_a_lowercase_market_outside_the_neighbour_stays_invisible(tmp_path):
    """Documented residual: a market named by a lower-case common word outside the lists, in a
    sentence that is not the level's neighbour, in an otherwise understood post."""
    p = policy(tmp_path, etf_entry(rule=ANCHOR_RULE.replace("SENTENCE_STRICT",
                                                            "SINGLE_MARKET_POST")))
    c = "We are now long $QETF, 50.10 now."
    d = decide(p, c, f"{c} Stop 48.90. The widget fund is heavy.")
    assert d.status == "SUPPORTED", d.reason


# ================================================================== S1: future skew


def test_s1_the_per_side_future_tolerance_is_half_a_second():
    assert getattr(V, "ANCHOR_QUOTE_FUTURE_TOLERANCE_SECONDS", None) == 0.5
    t = terms(**RULE)
    for ahead, ok in ((0.4, True), (0.6, False), (4.9, False)):
        q = q_with(t, "5025").model_copy(update={"bid_at": NOW + timedelta(seconds=ahead),
                                                 "ask_at": NOW + timedelta(seconds=ahead)})
        g = gate(act(entry=None), q)
        assert (g.status == "APPROVE") is ok, (ahead, g.reasons)
        if not ok:
            assert g.reasons[0] == "ENTRY_ANCHOR_QUOTE_SIDE_FUTURE"


def test_s1_the_execution_core_refuses_future_sides_too(tmp_path):
    h = Harness(tmp_path)
    at = h.clock.now()
    h.broker.set_quote(SYMBOL, "49.98", "50.02", received_at=at,
                       bid_at=at + timedelta(seconds=0.6))
    out = run_entry(h, anchored_gate())
    assert out.reasons[0] == "ENTRY_ANCHOR_QUOTE_SIDE_FUTURE" and h.broker.submit_calls == 0
    h.coord.close()


# ================================================================== S2: recovery


def recorded_not_sent(h):
    orig = h.coord._submit
    h.coord._submit = lambda *a, **k: None  # the process dies before the submission
    out = run_entry(h, anchored_gate())
    h.coord._submit = orig
    h.clock.advance(1)
    return out.order_ref


@pytest.mark.parametrize("quote,reason", [
    ({}, None),
    ({"ask_age": 60}, "ENTRY_ANCHOR_QUOTE_SIDE_STALE"),
    ({"quality": "DELAYED"}, "BROKER_QUOTE_QUALITY_DELAYED"),
])
def test_s2_recovery_of_a_never_sent_anchored_entry_applies_the_per_side_rule(tmp_path, quote,
                                                                             reason):
    from tradingbot.execution.broker import QuoteQuality
    h = Harness(tmp_path)
    ref = recorded_not_sent(h)
    assert h.broker.submit_calls == 0
    at = h.clock.now()
    kw = {"quality": QuoteQuality(quote["quality"])} if "quality" in quote else {}
    if "ask_age" in quote:
        kw["ask_at"] = at - timedelta(seconds=quote["ask_age"])
    h.broker.set_quote(SYMBOL, "49.98", "50.02", received_at=at, **kw)
    h.coord.recover()
    intent = h.coord.store.intent(ref)
    if reason is None:
        assert h.broker.submit_calls == 1, intent.reasons
        [entry] = [o for o in h.broker.all_orders() if o.parent_ref is None]
        assert entry.limit_price == Decimal("50.02")  # the plan's anchored limit
    else:
        assert h.broker.submit_calls == 0 and intent.state == IntentState.REFUSED
        assert reason in intent.reasons, intent.reasons
    h.coord.close()


def test_s2_recovery_applies_the_anchors_signal_age(tmp_path):
    h = Harness(tmp_path)
    ref = recorded_not_sent(h)
    h.clock.advance(60)  # 61 s after publication: inside the policy's 90 s, past the plan's 30 s
    at = h.clock.now()
    h.broker.set_quote(SYMBOL, "49.98", "50.02", received_at=at)
    plan_age = 30
    import json
    row = h.coord.store.intent(ref)
    plan = json.loads(row.plan)
    plan["anchor_max_signal_age_seconds"] = plan_age
    with h.coord.store.tx() as c:
        c.execute("UPDATE exec_intents SET plan=? WHERE order_ref=?", (json.dumps(plan), ref))
    h.coord.recover()
    assert h.broker.submit_calls == 0
    assert "ENTRY_ANCHOR_SIGNAL_TOO_OLD" in h.coord.store.intent(ref).reasons
    h.coord.close()


# ================================================================== S3: a raising declaration


def test_s3_an_environment_that_raises_is_undeclared_and_recorded_once(tmp_path):
    h = Harness(tmp_path)

    def boom():
        raise RuntimeError("adapter fault")
    h.broker.environment = boom
    assert broker_environment(h.broker) is None
    out = run_entry(h, anchored_gate())
    assert out.reasons == [ENTRY_ANCHOR_PAPER_ONLY, "ENVIRONMENT:UNDECLARED"]
    again = run_entry(h, anchored_gate())  # the same delivery again: no new attempt
    assert again.duplicate and h.broker.submit_calls == 0
    kinds = [e.kind for e in h.coord.store.exceptions(open_only=True)]
    assert kinds.count(ENTRY_ANCHOR_PAPER_ONLY) == 1
    h.coord.close()


# ================================================================== S4: marks in the own clause


@pytest.mark.parametrize("clause", [
    'We are now long $QETF, "50.10 now".', "We are now long $QETF, “50.10 now”.",
    "We are now long $QETF, '50.10 now'.", "We are now long $QETF, ~50.10 now.",
    "We are now long $QETF, ~ 50.10 now.", "We are now long $QETF, ≈50.10 now.",
    "We are now long $QETF, ±50.10 now.", "We are now long $QETF, <50.10 now.",
    "We are now long $QETF, >50.10 now.", "We are now long $QETF, +50.10 now.",
    "We are now long $QETF, -50.10 now.",
])
def test_s4_quotation_and_approximation_marks_hold(tmp_path, clause):
    p = policy(tmp_path, etf_entry())
    d = decide(p, clause, clause)
    assert d.status == "UNRESOLVED" and d.entry_anchor is None, (clause, d.reason)


# ================================================================== N1: strict, finite limits


@pytest.mark.parametrize("old,new", [
    ("max_quote_age_seconds = 3", 'max_quote_age_seconds = "3"'),
    ("max_quote_age_seconds = 3", "max_quote_age_seconds = true"),
    ("max_quote_age_seconds = 3", "max_quote_age_seconds = nan"),
    ("max_quote_age_seconds = 3", "max_quote_age_seconds = inf"),
    ("max_signal_age_seconds = 60", "max_signal_age_seconds = true"),
    ("max_signal_age_seconds = 60", 'max_signal_age_seconds = "60"'),
])
def test_n1_the_loader_takes_only_strict_finite_numbers(tmp_path, old, new):
    with pytest.raises(ValueError):
        policy(tmp_path, etf_entry(rule=ANCHOR_RULE.replace(old, new)))


@pytest.mark.parametrize("field", ["anchor_max_quote_age_seconds", "anchor_max_signal_age_seconds"])
@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -1.0])
def test_n1_a_non_finite_limit_at_the_gateway_holds(field, bad):
    g = gate(act(entry=None), q_with(terms(**{**RULE, field: bad}), "5025"))
    assert g.status == "HOLD" and g.reasons[0] == "ENTRY_ANCHOR_RULE_INCOMPLETE", g.reasons


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -1.0, 0.0])
def test_n1_a_non_finite_limit_in_the_plan_is_refused(tmp_path, bad):
    h = Harness(tmp_path)
    out = run_entry(h, anchored_gate(age=bad))
    assert out.reasons == ["ENTRY_ANCHOR_RULE_INCOMPLETE"] and h.broker.submit_calls == 0
    h.coord.close()


# ================================================================== N2: shipped lifecycle


def test_n2_the_shipped_mapping_runs_a_one_share_explicit_lifecycle(tmp_path):
    rt = Runtime(tmp_path, [post("1", 0, ENTRY), post("2", 30, RAISE_STOP),
                            post("3", 60, CLOSE)], mapping_file=MAPPING)
    rt.run(30)
    rows = {a["action"]: a for a in rt.actions() if a["action"] in ("OPEN", "MODIFY", "CLOSE")}
    assert {k: v["status"] for k, v in rows.items()} == {
        "OPEN": "EXECUTED", "MODIFY": "EXECUTED", "CLOSE": "EXECUTED"}, rows
    [entry] = [o for o in rt.broker.all_orders() if o.parent_ref is None and o.side.value == "BUY"]
    assert entry.quantity == 1
    assert not rt.broker.positions()
    rt.service.close()


def test_n2_an_anchored_open_on_the_shipped_mapping_ends_protected(tmp_path):
    rt = Runtime(tmp_path, [post("1", 0, "We are now long $TLT, 88.42 now. Stop 87.20.")],
                 mapping_file=MAPPING)
    rt.run(3)
    [ledger] = rt.service.coordinator.ledger()
    assert ledger.quantity == 1 and ledger.status == "OPEN"
    stops = [o for o in rt.broker.open_orders() if o.order_type.value == "STP"]
    assert len(stops) == 1 and stops[0].stop_price == Decimal("87.20") and stops[0].quantity == 1
    rt.service.close()


def test_n2_the_fixture_mapping_says_it_is_not_a_production_mapping():
    head = SERVICE_MAPPING.read_text().splitlines()[0]
    assert "test fixture" in head.lower() and "not a production mapping" in head.lower()


# ================================================================== N3: anchored ADD, paper only


def test_n3_an_anchored_add_never_reaches_a_live_broker(tmp_path):
    h = Harness(tmp_path)
    opened = h.coord.execute(source("p1", at=h.clock.now()), open_action(),
                             anchored_gate(rule="EXPLICIT_ENTRY_ONLY"), now=h.clock.now())
    assert opened.state != IntentState.REFUSED and h.broker.submit_calls == 1
    h.broker.environment = lambda: BrokerEnvironment.LIVE
    g = anchored_gate()
    add_gate = GateDecision(status="APPROVE", reasons=["PAPER_ADD"], plan=g.plan)
    add = action("add", Action.ADD, idea="p1:open", entry=None)
    out = h.coord.execute(source("p2", at=h.clock.now()), add, add_gate, now=h.clock.now())
    assert out.state == IntentState.REFUSED and out.reasons[0] == ENTRY_ANCHOR_PAPER_ONLY
    assert h.broker.submit_calls == 1
    h.coord.close()
