"""The Product Owner's decisions of 12 November 2026 on the entry rule (DECIDED 7), invented text.

D1  standalone_level_sentence on for TLT; a competing market holds the post only when it may be an
    actionable instrument: a mention with a trading context in its own sentence, or a sentence
    outside the action's own clause that carries an instruction word.
D3  a maximum protective-stop distance (TLT "5%"), from the stated reference and from the limit.
D5  TLT long-only; no market order on the TLT entry path.
"""

from __future__ import annotations

import tempfile
from decimal import Decimal
from pathlib import Path

import pytest
from execution_harness import Harness
from runtime_harness import MAPPING, Runtime, post
from test_entry_anchor_policy import ANCHOR_RULE, MAP_NOW, RULE, etf_entry, policy, q_with
from test_entry_anchor_policy import action as etf_action
from test_entry_anchor_single_market import note
from test_entry_rule_paper_only import anchored_gate, run_entry
from test_multi_asset_gateway import act, gate, terms

from tradingbot.event_models import ParsedPost, PriceBasis, Timing, TradeAction
from tradingbot.models import Action, AssetClass, Instrument, PositionSide
from tradingbot.runtime.mapping import MappingPolicy
from tradingbot.validator import ANCHOR_MAX_STOP_DISTANCE_PCT_CEILING


def run(text: str):
    with tempfile.TemporaryDirectory() as d:
        rt = Runtime(Path(d), [post("1", 0, text)], mapping_file=MAPPING)
        rt.run(3)
        out = [(a["action"], a["status"], a["reasons"]) for a in rt.actions()
               if a["action"] not in (None, "NO_TRADE")]
        orders = [o for o in rt.broker.all_orders()]
        rt.service.close()
        return out, orders


BASE = "We are now long $TLT, 88.42 now. Stop 87.20."


# ================================================================== D1: the shipped switch


def test_d1_the_switch_is_on_for_tlt_only():
    p = MappingPolicy.from_toml(MAPPING)
    on = {f for f, e in p.entries.items()
          if getattr(e, "entry_anchor", None) and e.entry_anchor.standalone_level_sentence}
    assert on == {"TLT"}
    assert "standalone_level_sentence" in p.entries["TLT"].note


def test_d1_a_level_line_after_the_instruction_trades_on_the_shipped_mapping():
    out, _ = run("We are now long $TLT.\n88.42 now.\nStop 87.20.")
    assert [o[1] for o in out] == ["EXECUTED"], out


# ================================================================== D1: competing markets

# Commentary that the interpreter itself reads as another family's (NO_TRADE) action, such as
# "Oil is down 3% this week.", still holds on the unchanged action rule
# (ENTRY_ANCHOR_POST_SEVERAL_MARKETS, ACTION_FOR_ANOTHER_FAMILY): see the report.
TRADE = [  # commentary without action semantics or a price-like level: no second instrument
    "Qorvex reported earnings.", "FOMC minutes are out later.", "London was quiet.", "NFA.",
    "Thanks to Sarah for the chart.", "Volatility is low into Friday.", "Posted from London.",
    "More details on our Discord.", "Inflation data is out on Thursday.", "Powell speaks later.",
    "Have a great weekend, LOL.", "Keep an eye on the spread.", "Equities had a rough week.",
    "We expect bonds to stay choppy in 2026.",  # (hyphenated side words: see the fix round)
]


@pytest.mark.parametrize("commentary", TRADE)
def test_d1_commentary_naming_something_else_no_longer_holds(commentary):
    out, _ = run(f"{BASE} {commentary}")
    assert [o[1] for o in out] == ["EXECUTED"], out


HOLD = [  # a second market with action semantics or a quoted level
    f"{BASE} We are buying spy as well.",  # lowercase, an instruction word, outside the clause
    f"{BASE} Also long the ten-year.",
    f"{BASE} Adding to gold.",
    f"{BASE} Qorvex is a buy at 41.20.",  # sentence-initial unknown name with a buy and a price
    f"{BASE} Gold is firm at 2,400.",  # a generic noun with a price after "at"
    f"{BASE} Watching IEF at 98.10.",  # ticker-shaped with a price
    f"{BASE} Our stop on Qorvex is 12.50.",  # a stop word and a price next to an unknown name
    f"{BASE} We sell oil above 80.",
    f"Short gold here.\n{BASE}",  # before the instruction
    "We are now long $TLT and EURUSD, 88.42 now. Stop 87.20.",  # inside the instruction
    "We are now long $TLT and spy.\n88.42 now.\nStop 87.20.",  # instruction not understood
    f"{BASE} Copper 4.12 now.",  # a second quoted level
]


@pytest.mark.parametrize("text", HOLD)
def test_d1_a_second_market_with_action_semantics_or_a_level_holds(text):
    out, orders = run(text)
    assert not orders and all(o[1] != "EXECUTED" for o in out), out


def decide(p, post_text, clause="We are now long $QETF, 50.10 now."):
    _, [d, _] = p.apply(ParsedPost(actions=[etf_action(), note()]),
                        clauses={"a1": clause, "n1": ""}, now=MAP_NOW, post_text=post_text,
                        levels={"a1": [("REFERENCE", "50.10"), ("STOP", "48.90")]})
    return d


SMP = ANCHOR_RULE.replace("SENTENCE_STRICT", "SINGLE_MARKET_POST")
C = "We are now long $QETF, 50.10 now. Stop 48.90."


@pytest.mark.parametrize("extra,detail", [
    ("Watching IEF at 98.10.", "RULE:MENTION_WITH_CONTEXT"),
    ("We stay long-only here as well.", "RULE:UNATTRIBUTED_ACTION_SENTENCE"),
    ("We are buying spy as well.", "RULE:UNATTRIBUTED_ACTION_SENTENCE"),
])
def test_d1_the_hold_names_the_rule_that_fired(tmp_path, extra, detail):
    d = decide(policy(tmp_path, etf_entry(rule=SMP)), f"{C} {extra}", C)
    assert d.status == "UNRESOLVED" and detail in d.reason.split(","), d.reason


@pytest.mark.parametrize("extra", [
    "Stop 48.90.",  # the action's own stop sentence (no instruction word)
    "Target 52.40.",
    "Shortly after the open it was quiet.",  # a different word
    "The chart is clean.",
])
def test_d1_the_unattributed_action_rule_does_not_fire_on_these(tmp_path, extra):
    d = decide(policy(tmp_path, etf_entry(rule=SMP)), f"{C} {extra}", C)
    assert "RULE:UNATTRIBUTED_ACTION_SENTENCE" not in d.reason.split(","), d.reason


# ================================================================== D3: maximum stop distance


STOP_RULE = {**RULE, "max_stop_distance": "5%"}


def test_d3_the_ceiling_is_a_code_constant():
    assert ANCHOR_MAX_STOP_DISTANCE_PCT_CEILING == Decimal(10)


@pytest.mark.parametrize("stop,reason", [
    ("4995", None),  # 0.6 % below the anchor
    ("4773.75", "PROTECTIVE_STOP_TOO_FAR_FROM_LIMIT"),  # 5.00 % from 5025, 5.005 % from 5025.25
    ("4770", "PROTECTIVE_STOP_TOO_FAR_FROM_ENTRY"),
    (None, "PROTECTIVE_STOP_MISSING"),
    ("5030", "STOP_DIRECTION_CONTRADICTION"),
])
def test_d3_the_gateway_bounds_the_stop_distance(stop, reason):
    g = gate(act(entry=None, stop=stop), q_with(terms(**STOP_RULE), "5025"))
    if reason is None:
        assert g.status == "APPROVE", g.reasons
        assert g.plan.max_stop_distance == "5%"
    else:
        assert g.status != "APPROVE" and g.reasons[0] == reason, g.reasons


def test_d3_an_anchored_rule_without_the_limit_holds():
    g = gate(act(entry=None), q_with(terms(**{**RULE, "max_stop_distance": None}), "5025"))
    assert g.reasons == ["ENTRY_ANCHOR_RULE_INCOMPLETE"]


def test_d3_explicit_entries_are_bounded_too():
    """Owner, 12 November 2026 (later decision): the stop rule governs explicit entries too."""
    g = gate(act(stop="4700"), q_with(terms(**STOP_RULE), "5025"))
    assert g.reasons[0] == "PROTECTIVE_STOP_TOO_FAR_FROM_ENTRY", g.reasons
    assert gate(act(stop="4995"), q_with(terms(**STOP_RULE), "5025")).status == "APPROVE"


@pytest.mark.parametrize("levels,others,reason", [
    ([("REFERENCE", "50.10"), ("STOP", "48.90"), ("STOP", "48.50")], (), "TWO_STOP_VALUES"),
    ([("REFERENCE", "50.10"), ("STOP", "48.50")], (), "STOP_DIFFERS_FROM_ACTION"),
    ([("REFERENCE", "50.10"), ("STOP", "48.90")], (("QOIL", "Long $QOIL.", {Decimal("48.90")}),),
     "STOP_SHARED_WITH_ANOTHER_ACTION"),
])
def test_d3_a_stop_that_is_not_uniquely_this_actions_holds(tmp_path, levels, others, reason):
    p = policy(tmp_path, etf_entry())
    d, _ = p.resolve(etf_action(), clause="We are now long $QETF, 50.10 now.", now=MAP_NOW,
                     levels=levels, others=list(others))
    assert d.reason.split(",")[:2] == ["PROTECTIVE_STOP_NOT_UNIQUE", reason], d.reason


@pytest.mark.parametrize("value", ['"11%"', '"5"', "5", "true", '"nan%"'])
def test_d3_the_loader_takes_a_strict_percentage_under_the_ceiling(tmp_path, value):
    with pytest.raises(ValueError, match="max_stop_distance"):
        policy(tmp_path, etf_entry(rule=ANCHOR_RULE.replace('max_stop_distance = "5%"',
                                                            f"max_stop_distance = {value}")))
    with pytest.raises(ValueError, match="max_stop_distance"):
        policy(tmp_path, etf_entry(rule=ANCHOR_RULE.replace('max_stop_distance = "5%"\n', "")))


def test_d3_the_execution_core_checks_the_limit_again(tmp_path):
    h = Harness(tmp_path)
    far = anchored_gate()
    far = far.model_copy(update={"plan": far.plan.model_copy(update={"stop": Decimal("47.50")})})
    out = run_entry(h, far)  # 5.04 % below the 50.02 limit
    assert out.reasons[0] == "PROTECTIVE_STOP_TOO_FAR_FROM_LIMIT" and h.broker.submit_calls == 0
    ok = run_entry(h, anchored_gate(), sid="p2")  # 48 is 4.04 % below
    assert h.broker.submit_calls == 1, ok.reasons
    h.coord.close()


def test_d3_the_shipped_limit_is_tlt_only():
    p = MappingPolicy.from_toml(MAPPING)
    assert p.entries["TLT"].max_stop_distance == "5%"


# ================================================================== D5: long only


def test_d5_tlt_is_long_only_in_the_shipped_file():
    p = MappingPolicy.from_toml(MAPPING)
    assert p.entries["TLT"].long_only is True
    short = TradeAction(action_id="a1", action=Action.OPEN, timing=Timing.CURRENT,
                        instrument=Instrument(asset_class=AssetClass.ETF, symbol="TLT"),
                        position_side=PositionSide.SHORT, price_basis=PriceBasis.TRADED_CONTRACT,
                        source_market_symbol="TLT", entry_reference="88.40", stop_price="89.50",
                        explicit_instruction=True, evidence=["invented"])
    d, inst = p.resolve(short, clause="We are now short $TLT at 88.40.", now=MAP_NOW)
    assert d.status == "UNRESOLVED" and d.reason == "MAPPING_LONG_ONLY" and inst is None


@pytest.mark.parametrize("text", ["We are now short $TLT at 88.40. Stop 89.50.",
                                  "We are now short $TLT, 88.42 now. Stop 89.50."])
def test_d5_a_short_tlt_entry_never_trades(text):
    out, orders = run(text)
    assert not orders and out and "MAPPING_LONG_ONLY" in out[0][2], out


def test_d5_long_only_is_opt_in(tmp_path):
    p = policy(tmp_path, etf_entry())
    assert p.entries["QETF"].long_only is False


@pytest.mark.parametrize("text", [BASE, "We are now long $TLT at 88.40. Stop 87.20."])
def test_d5_tlt_entries_are_limit_orders(text):
    out, orders = run(text)
    entries = [o for o in orders if o.parent_ref is None]
    assert entries and all(o.order_type.value == "LMT" for o in entries), out
