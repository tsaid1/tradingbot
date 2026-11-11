"""BrokerEventReplay: the existing replay path executing through a Broker, gateway unchanged."""

from __future__ import annotations

import pytest
from execution_harness import SYMBOL, Harness, action, open_action, source

from tradingbot.event_models import ParsedPost
from tradingbot.event_replay import PaperEventReplay
from tradingbot.execution.coordinator import order_ref_for
from tradingbot.execution.replay import PRIOR_INTENT
from tradingbot.execution.sim_broker import SimulatedCrash
from tradingbot.models import Action

IDEA = "p1:open"


@pytest.fixture
def h(tmp_path):
    return Harness(tmp_path)


def statuses(result):
    return [o.status for o in result.outcomes]


def replay_idea(h: Harness, idea_id: str = IDEA) -> dict:
    return {i["idea_id"]: i for i in h.replay.ideas()}[idea_id]


def test_mock_default_is_unchanged(tmp_path, h):
    mock = PaperEventReplay(tmp_path / "mock.sqlite3", h.policy)
    r = mock.process(
        source("p1"), ParsedPost(actions=[open_action()]), h.quotes(), now=h.clock.now()
    )
    assert statuses(r) == ["SIMULATED"] and r.outcomes[0].order is not None
    assert r.outcomes[0].order_ref is None


def test_full_lifecycle_through_the_replay(h):
    assert statuses(h.post("p1", open_action())) == ["EXECUTED"]
    assert statuses(h.post("p2", action("add", Action.ADD, idea=IDEA, entry="50"))) == ["EXECUTED"]
    assert statuses(h.post("p3", action("raise", Action.MODIFY, idea=IDEA, stop="49"))) == [
        "EXECUTED"
    ]
    assert statuses(h.post("p4", action("trim", Action.REDUCE, idea=IDEA, fraction="0.5"))) == [
        "EXECUTED"
    ]
    idea = replay_idea(h)
    assert (idea["quantity"], idea["stop_price"], idea["status"]) == (3, "49", "OPEN")
    assert h.coord.reconcile().clean
    assert statuses(h.post("p5", action("close", Action.CLOSE, idea=IDEA))) == ["EXECUTED"]
    idea = replay_idea(h)
    assert (idea["quantity"], idea["status"]) == (0, "CLOSED")
    assert h.working() == {} and h.broker.positions() == []
    assert h.coord.reconcile().clean


def test_outcome_carries_order_ref_and_broker_reasons(h):
    [o] = h.post("p1", open_action()).outcomes
    assert o.order_ref == order_ref_for("p1", 1, "open")
    assert "PAPER_OPEN" in o.reasons and "BROKER_FILLED" in o.reasons
    assert "PROTECTION_CONFIRMED" in o.reasons


def test_gateway_hold_never_reaches_the_broker(h):
    r = h.post("p1", open_action(stop=None))  # require_stop_on_open -> HOLD
    assert statuses(r) == ["HOLD"] and r.outcomes[0].reasons == ["PROTECTIVE_STOP_MISSING"]
    assert h.broker.submit_calls == 0 and h.coord.store.intents() == []


def test_atomic_group_with_a_held_member_submits_nothing(h):
    h.post("p1", open_action())
    calls = h.broker.submit_calls
    r = h.post(
        "p2",
        action("add", Action.ADD, idea=IDEA, entry="50", group="g"),
        action("mod", Action.MODIFY, idea=IDEA, stop="40", group="g"),
    )  # widening -> HOLD
    assert statuses(r) == ["HOLD", "HOLD"]
    assert h.broker.submit_calls == calls and replay_idea(h)["quantity"] == 4


def test_duplicate_delivery_through_the_replay(h):
    h.post("p1", open_action())
    calls = h.broker.submit_calls
    again = h.post("p1", open_action())
    assert again.duplicate and statuses(again) == ["EXECUTED"]
    assert h.broker.submit_calls == calls


def test_crash_mid_order_then_redelivery_adopts_instead_of_reordering(h):
    h.broker.script_next_submit("CRASH_AFTER_PLACE")
    with pytest.raises(SimulatedCrash):
        h.post("p1", open_action())
    assert h.replay.outcomes("p1") == []  # the replay transaction rolled back
    h.restart()
    r = h.post("p1", open_action())
    [o] = r.outcomes
    assert o.status == "EXECUTED" and PRIOR_INTENT in o.reasons
    assert "DUPLICATE_DELIVERY_NO_NEW_ORDER" in o.reasons
    assert len(h.broker.orders_with_ref(order_ref_for("p1", 1, "open"))) == 1
    assert replay_idea(h)["quantity"] == 4 and h.coord.reconcile().clean


def test_stop_fired_between_posts_holds_the_later_close(h):
    h.post("p1", open_action())
    h.broker.set_quote(SYMBOL, "47.90", "47.95")  # stop triggers at the broker
    h.broker.set_quote(SYMBOL, "47.95", "47.99")
    r = h.post("p2", action("close", Action.CLOSE, idea=IDEA))
    assert statuses(r) == ["HOLD"] and r.outcomes[0].reasons == ["REFERENCED_ACTIVE_IDEA_NOT_FOUND"]
    assert replay_idea(h)["status"] == "CLOSED"
    assert h.coord.reconcile().clean


def test_halt_through_the_replay(h):
    h.halt()
    r = h.post("p1", open_action())
    assert statuses(r) == ["HALTED"] and h.broker.submit_calls == 0
    assert replay_idea(h)["status"] == "NOT_FILLED"
    h.unhalt()
    # The post is recorded: redelivery is a duplicate, never a silent retry once unhalted.
    assert h.post("p1", open_action()).duplicate and h.broker.submit_calls == 0


def test_rejected_entry_through_the_replay(h):
    h.broker.script_next_submit("REJECT")
    assert statuses(h.post("p1", open_action())) == ["REJECTED"]
    assert replay_idea(h)["quantity"] == 0
    r = h.post("p2", action("close", Action.CLOSE, idea=IDEA))
    assert statuses(r) == ["HOLD"]  # nothing to close: the gate holds


def test_partial_fill_updates_replay_ideas_from_fills_only(h):
    h.broker.script_next_submit("PARTIAL", quantity=1)
    assert statuses(h.post("p1", open_action())) == ["WORKING"]
    assert replay_idea(h)["quantity"] == 1
    h.broker.fill(order_ref_for("p1", 1, "open"))
    h.post("p2", action("trim", Action.REDUCE, idea=IDEA, fraction="0.25"))
    assert replay_idea(h)["quantity"] == 3


def test_mock_lifecycle_script_runs_clean(capsys):
    import runpy
    from pathlib import Path

    script = Path(__file__).resolve().parents[1] / "scripts" / "run_mock_lifecycle.py"
    module = runpy.run_path(str(script), run_name="mock_lifecycle")
    assert module["main"]() == 0
    out = capsys.readouterr().out
    assert "RESULT: OK (0 unexplained discrepancies)" in out
    for step in (
        "entry+stop",
        "add",
        "stop amend",
        "partial reduce",
        "full close",
        "duplicate",
        "lost ack",
        "restart+redeliver",
        "rejected",
        "halt",
    ):
        assert step in out
