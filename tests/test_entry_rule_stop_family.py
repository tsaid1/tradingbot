"""The owner's decision: the 5% maximum protective-stop distance governs EVERY TLT entry (explicit
and anchored) as a family rule. An outer rejection boundary; the bot never synthesizes, moves or
chooses a stop. Invented text; the whole mock service on the shipped mapping where it matters.
"""

from __future__ import annotations

import tempfile
from decimal import Decimal
from pathlib import Path

import pytest
from execution_harness import Harness, open_action, source
from runtime_harness import MAPPING, Runtime, post
from test_entry_anchor_policy import ANCHOR_RULE, RULE, etf_entry, policy, q_with
from test_entry_rule_paper_only import anchored_gate
from test_multi_asset_gateway import act, gate, mq, position, terms

from tradingbot.execution.store import IntentState
from tradingbot.models import Action
from tradingbot.validator import stop_distance_problem


def run(text, collar="0.1"):
    with tempfile.TemporaryDirectory() as d:
        rt = Runtime(Path(d), [post("1", 0, text)], mapping_file=MAPPING,
                     policy={"max_price_deviation_pct": collar})
        rt.run(3)
        out = [(a["action"], a["status"], a["reasons"]) for a in rt.actions()
               if a["action"] == "OPEN"]
        sent = rt.broker.submit_calls
        rt.service.close()
        return sent, out


@pytest.mark.parametrize("stop,ok,reason", [
    ("84.09", True, None),  # 4.9 % below the 88.42 entry (and the 88.42 limit)
    ("83.91", False, "PROTECTIVE_STOP_TOO_FAR_FROM_ENTRY"),  # 5.1 %
    ("1.20", False, "PROTECTIVE_STOP_TOO_FAR_FROM_ENTRY"),  # a distance written as a stop
    ("89.00", False, "STOP_DIRECTION_CONTRADICTION"),  # above a long's entry
])
def test_an_explicit_tlt_entry_is_bounded_by_the_family_stop_rule(stop, ok, reason):
    sent, out = run(f"We are now long $TLT at 88.42. Stop {stop}.")
    if ok:
        assert sent == 1 and out[0][1] == "EXECUTED", out
    else:
        assert sent == 0 and reason in out[0][2], out


def test_two_different_stops_hold_an_explicit_entry():
    sent, out = run("We are now long $TLT at 88.42. Stop 87.20. Hard stop 86.90.")
    assert sent == 0 and "PROTECTIVE_STOP_NOT_UNIQUE" in out[0][2], out


def test_a_stop_just_inside_the_limit_trades_under_the_default_collar():
    """The explicit limit is the stated entry (the 1 % collar only rejects), so the entry and
    limit checks coincide up to the tick: 4.9 % below trades with the default collar."""
    sent, out = run("We are now long $TLT at 88.42. Stop 84.09.", collar="1.0")
    assert sent == 1 and out[0][1] == "EXECUTED", out


def test_exactly_at_the_limit_is_accepted():
    """Inclusive: farther than the limit is refused, exactly at it is not."""
    assert stop_distance_problem(Decimal(95), Decimal(100), Decimal(5), "X") == []
    assert stop_distance_problem(Decimal("94.99"), Decimal(100), Decimal(5), "X")[0] == "X"


@pytest.mark.parametrize("prior,reason", [
    (None, "PROTECTIVE_STOP_MISSING"),  # an add needs the idea's stop as it stands
    ("4700", "PROTECTIVE_STOP_TOO_FAR_FROM_ENTRY"),
    ("4995", None),
])
def test_an_explicit_add_is_checked_against_the_ideas_stop(prior, reason):
    t = terms(max_stop_distance="5%", max_notional=Decimal(100000))
    g = gate(act(Action.ADD, entry="5025", idea="9:a1"), mq(t), position(qty=1, stop=prior))
    if reason is None:
        assert g.status == "APPROVE", g.reasons
        assert g.plan.max_stop_distance == "5%"
    else:
        assert g.status != "APPROVE" and g.reasons[0] == reason, g.reasons


def test_a_family_without_the_key_is_unaffected():
    g = gate(act(stop="4700"), mq(terms()))
    assert g.status == "APPROVE", g.reasons


def test_the_execution_core_checks_an_explicit_entry_against_its_limit(tmp_path):
    h = Harness(tmp_path)
    g = anchored_gate(rule="EXPLICIT_ENTRY_ONLY")
    g = g.model_copy(update={"plan": g.plan.model_copy(update={"max_stop_distance": "5%",
                                                               "stop": Decimal("47.40")})})
    a = open_action(entry="50.02")  # the collar limit is 50.52: 47.40 is 6.2 % below it
    out = h.coord.execute(source("p1", at=h.clock.now()), a, g, now=h.clock.now())
    assert out.state == IntentState.REFUSED and out.reasons[0] == \
        "PROTECTIVE_STOP_TOO_FAR_FROM_LIMIT" and h.broker.submit_calls == 0
    h.coord.close()


def test_the_key_is_family_level_and_required_with_the_rule(tmp_path):
    with pytest.raises(ValueError, match="family-level key"):
        policy(tmp_path, etf_entry(rule=ANCHOR_RULE.replace(
            'environment = "PAPER"', 'environment = "PAPER", max_stop_distance = "5%"', 1)))
    with pytest.raises(ValueError, match="max_stop_distance"):
        policy(tmp_path, etf_entry(extra="").replace('max_stop_distance = "5%"\n', ""))
    plain = etf_entry(rule="", extra="")
    assert policy(tmp_path, plain).entries["QETF"].max_stop_distance is None


def test_the_shipped_key_is_tlt_only():
    from tradingbot.runtime.mapping import MappingPolicy
    for f, e in MappingPolicy.from_toml(MAPPING).entries.items():
        assert (getattr(e, "max_stop_distance", None) == "5%") is (f == "TLT")


def test_anchored_entries_use_the_same_codes():
    g = gate(act(entry=None, stop="4770"), q_with(terms(**RULE), "5025"))
    assert g.reasons[0] == "PROTECTIVE_STOP_TOO_FAR_FROM_ENTRY", g.reasons
