"""The owner's clarification (12 November 2026): the policy collar is a rejection boundary, never
the price sent. An explicit entry's limit is the publisher's stated entry price itself (snapped to
the tick, never past it): a buy limit never above it, a sell limit never below it. Anchored entries
keep the gateway's limit at the fresh executable quote. Invented data.
"""

from __future__ import annotations

import tempfile
from decimal import Decimal
from pathlib import Path

import pytest
from execution_harness import SYMBOL, Harness, action, open_action, source
from runtime_harness import MAPPING, Runtime, post
from test_entry_rule_paper_only import anchored_gate, run_entry

from tradingbot.execution.store import IntentState
from tradingbot.models import Action, GateDecision, PositionSide


def entries(rt):
    return [o for o in rt.broker.all_orders() if o.parent_ref is None and o.side.value == "BUY"]


def service(text):
    d = tempfile.mkdtemp()
    rt = Runtime(Path(d), [post("1", 0, text)], mapping_file=MAPPING)  # ask 88.42, 1 % collar
    rt.run(3)
    return rt


def test_a_buy_above_the_ask_inside_the_collar_is_sent_at_the_stated_entry_and_rests():
    rt = service("We are now long $TLT at 88.41. Stop 86.00.")
    [entry] = entries(rt)
    assert entry.limit_price == Decimal("88.41")  # not 89.29, the old collar-priced limit
    assert entry.status.value in ("Submitted", "PreSubmitted") and entry.filled_quantity == 0
    rt.broker.set_quote("TLT", "88.39", "88.41", received_at=rt.clock.now())  # the ask comes down
    rt.run(2)
    [entry] = entries(rt)
    assert entry.filled_quantity == 1 and rt.broker.positions()
    rt.service.close()


def test_with_the_ask_at_the_stated_entry_it_fills_at_the_ask():
    rt = service("We are now long $TLT at 88.42. Stop 86.00.")
    [entry] = entries(rt)
    assert entry.limit_price == Decimal("88.42") and entry.filled_quantity == 1
    rt.service.close()


def test_outside_the_collar_nothing_is_sent():
    rt = service("We are now long $TLT at 87.00. Stop 84.00.")  # the ask is 1.6 % above
    assert entries(rt) == [] and rt.broker.submit_calls == 0
    rt.service.close()


def test_a_stop_four_point_nine_percent_below_trades_under_the_default_collar():
    rt = service("We are now long $TLT at 88.42. Stop 84.09.")  # 4.9 %; 5.1 % is 83.91
    assert len(entries(rt)) == 1, [a["reasons"] for a in rt.actions()]
    rt.service.close()
    rt = service("We are now long $TLT at 88.42. Stop 83.91.")
    assert entries(rt) == []
    rt.service.close()


def test_a_sell_limit_is_never_below_the_stated_entry(tmp_path):
    # A short sale needs an owner's [risk] table: an INVENTED one (docs/RISK_GATES.md).
    h = Harness(tmp_path, risk_families=((SYMBOL, "STK", "1"),))  # 49.98 / 50.02, 1 % collar
    out = h.execute("p1", open_action(entry="50.01", stop="52", target="45",
                                      side=PositionSide.SHORT))
    [entry] = [o for o in h.broker.all_orders() if o.order_ref == out.order_ref]
    assert entry.side.value == "SELL" and entry.limit_price == Decimal("50.01")  # was 49.50
    h.coord.close()


def test_an_explicit_add_is_sent_at_its_stated_entry(tmp_path):
    h = Harness(tmp_path)
    h.execute("p1", open_action(entry="50.02"))
    out = h.execute("p2", action("add", Action.ADD, idea="p1:open", entry="50.01"))
    [add] = [o for o in h.broker.all_orders() if o.order_ref == out.order_ref]
    assert add.limit_price == Decimal("50.01")  # was 50.51
    h.coord.close()


def test_the_anchored_limit_is_unchanged(tmp_path):
    h = Harness(tmp_path)
    out = run_entry(h, anchored_gate(limit="50.02"))
    [entry] = [o for o in h.broker.all_orders() if o.order_ref == out.order_ref]
    assert entry.limit_price == Decimal("50.02")
    h.coord.close()


def test_recovery_recomputes_the_same_explicit_limit(tmp_path):
    h = Harness(tmp_path)
    orig = h.coord._submit
    h.coord._submit = lambda *a, **k: None  # the process dies before the submission
    out = h.coord.execute(source("p1", at=h.clock.now()), open_action(entry="50.01"),
                          GateDecision(status="APPROVE", reasons=["TEST"]), now=h.clock.now())
    h.coord._submit = orig
    h.clock.advance(1)
    h.broker.set_quote(SYMBOL, "49.98", "50.02", received_at=h.clock.now())
    h.coord.recover()
    assert h.coord.store.intent(out.order_ref).state != IntentState.REFUSED
    [entry] = [o for o in h.broker.all_orders() if o.order_ref == out.order_ref]
    assert entry.limit_price == Decimal("50.01")
    h.coord.close()


@pytest.mark.parametrize("text", [
    "We are now long $TLT, 88.42 now. Stop 87.20. Protective level 87.10.",  # anchored
    "We are now long $TLT at 88.42. Stop 87.20. Protective level 87.10.",  # explicit
])
def test_a_protective_level_is_a_second_stop(text):
    rt = service(text)
    assert entries(rt) == [] and any(
        "PROTECTIVE_STOP_NOT_UNIQUE" in a["reasons"] for a in rt.actions())
    rt.service.close()


def test_a_protective_stop_that_is_the_actions_own_still_anchors(tmp_path):
    """The guard lives at the mapping stage, with the stop tagged as the action's own. Through the
    whole service "Protective stop N." alone is read by the interpreter as commentary and attaches
    no stop, so such a post HOLDS for a missing stop (safe); it does not trade."""
    from test_entry_anchor_policy import ANCHOR_RULE, MAP_NOW, etf_entry, policy
    from test_entry_anchor_policy import action as etf_action
    from test_entry_anchor_single_market import note

    from tradingbot.event_models import ParsedPost
    p = policy(tmp_path, etf_entry(rule=ANCHOR_RULE.replace("SENTENCE_STRICT",
                                                            "SINGLE_MARKET_POST")))
    c = "We are now long $QETF, 50.10 now."
    _, [d, _] = p.apply(ParsedPost(actions=[etf_action(), note()]), clauses={"a1": c, "n1": ""},
                        now=MAP_NOW, post_text=f"{c} Protective stop 48.90.",
                        levels={"a1": [("REFERENCE", "50.10"), ("STOP", "48.90")]})
    assert d.status == "SUPPORTED", d.reason
    _, [d, _] = p.apply(ParsedPost(actions=[etf_action(), note()]), clauses={"a1": c, "n1": ""},
                        now=MAP_NOW, post_text=f"{c} Stop 48.90. Protective level 48.70.",
                        levels={"a1": [("REFERENCE", "50.10"), ("STOP", "48.90")]})
    assert d.reason.split(",")[:2] == ["PROTECTIVE_STOP_NOT_UNIQUE", "SEVERAL_STOP_VALUES"]


@pytest.mark.parametrize("text", ["We are now long $TLT at 88.38-88.42. Stop 86.00.",
                                  "We are now long $TLT at 88.38 to 88.42. Stop 86.00."])
def test_a_stated_range_never_buys_above_its_upper_bound(text):
    """A range reaches the typed action as its first entry value (the lower bound here), so the
    buy limit is never above the stated maximum; it rests below the 88.42 ask."""
    rt = service(text)
    [entry] = entries(rt)
    assert entry.limit_price == Decimal("88.38") and entry.limit_price <= Decimal("88.42")
    rt.service.close()


def test_a_protective_stop_alone_holds_through_the_service_for_a_missing_stop():
    """The honest statement of the docstring above, pinned: the interpreter attaches no stop."""
    rt = service("We are now long $TLT at 88.42. Protective stop 87.20.")
    assert entries(rt) == []
    assert rt.actions()[0]["status"] == "HOLD"
    assert "PROTECTIVE_STOP_MISSING" in rt.actions()[0]["reasons"]
    rt.service.close()
