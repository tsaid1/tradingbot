"""Review N7: a CLOSE that meets a partly filled entry whose remainder fills before its cancel.

The entry for 2 has 1 filled and is still working when the publisher's CLOSE arrives. The netting
cancels the remainder; the cancel loses the race and the remainder fills first. The entry's outcome
is then confirmed at the broker (fully filled), so the CLOSE is resized to the whole confirmed
position in the same cycle. With an outcome that is not confirmed the exit is refused, nothing is
sent and the exception stays open: final, exactly as on main (no deferred retry).

Invented ticker (the harness's), toy prices. No network, no broker port. The last test runs the
same race through the whole mock service, on invented posts.
"""

from __future__ import annotations

import tempfile
from decimal import Decimal
from pathlib import Path

from execution_harness import Harness, action, open_action
from runtime_harness import Runtime, post

from tradingbot.execution.coordinator import EXIT_NOT_NETTED
from tradingbot.execution.store import IntentState
from tradingbot.models import Action, MockPolicy
from tradingbot.runtime import notify as N


def sized(tmp_path, size: int) -> Harness:
    policy = MockPolicy(quantity=size, quantity_per_add=1, max_notional_usd=Decimal(1000))
    return Harness(tmp_path, policy=policy, strict=True)


def partly_filled(h: Harness, filled: int):
    h.broker.script_next_submit("PARTIAL", quantity=filled)
    out = h.execute("p1", open_action())
    assert out.state == IntentState.WORKING and out.filled_quantity == filled, out
    return out


def held(h: Harness) -> int:
    return sum(p.quantity for p in h.broker.positions())


def traded(h: Harness, side: str) -> int:
    return sum(f.quantity for f in h.broker.executions() if f.side.value == side)


def open_kinds(h: Harness, idea: str = "p1:open") -> list[str]:
    return [e.kind for e in h.coord.store.exceptions(open_only=True) if e.idea_id == idea]


def test_a_close_whose_entry_cancel_loses_the_race_exits_the_whole_confirmed_position(tmp_path):
    h = sized(tmp_path, 2)
    opened = partly_filled(h, 1)
    h.broker.script_next_cancel("FILL_FIRST")  # the remainder fills before its cancel lands
    close = h.execute("p2", action("close", Action.CLOSE, idea="p1:open"))
    assert h.coord.intent(opened.order_ref).state == IntentState.FILLED
    assert close.state == IntentState.FILLED and close.filled_quantity == 2, close
    assert held(h) == 0 and traded(h, "BUY") == traded(h, "SELL") == 2
    assert h.coord.store.position("p1:open").quantity == 0
    assert h.broker.open_orders() == []  # the protective stop is not left behind
    assert EXIT_NOT_NETTED not in open_kinds(h)
    h.coord.sync()
    assert held(h) == 0 and traded(h, "SELL") == 2  # exactly once
    assert h.coord.reconcile().clean
    h.coord.close()


# ----------------------------------------------------------------- bounded enumeration (N7)

CANCELS = ("normal", "FILL_FIRST", "LOST_ACK_APPLIED", "LOST_ACK_NOT_APPLIED", "REJECT")
CASES = [
    (size, filled, cancel, exit_)
    for size in (2, 3, 4)
    for filled in range(1, size)
    for cancel in CANCELS
    for exit_ in ("CLOSE", "REDUCE")
]


def committed_sells(h: Harness) -> int:
    """Working SELL orders at the broker; one-cancels-other siblings once, at the larger."""
    groups: dict[str, int] = {}
    for o in h.broker.open_orders():
        if o.side.value == "SELL":
            key = o.parent_ref or o.order_ref
            groups[key] = max(groups.get(key, 0), o.remaining)
    return sum(groups.values())


def watch_exit_sends(h: Harness) -> list[tuple[int, int, int]]:
    """Every exit sent: (quantity, already committed on the exit side, held), read at sending."""
    sent: list[tuple[int, int, int]] = []
    real = h.broker.submit

    def submit(order, children=()):
        intent = h.coord.store.intent(order.order_ref)
        if intent is not None and intent.action in (Action.REDUCE, Action.CLOSE):
            sent.append((order.quantity, committed_sells(h), held(h)))
        return real(order, children)

    h.broker.submit = submit
    return sent


def check_sends(sent: list[tuple[int, int, int]]) -> None:
    for qty, committed, holding in sent:
        assert qty + committed <= holding, (qty, committed, holding)


def exit_action(exit_: str):
    if exit_ == "CLOSE":
        return action("close", Action.CLOSE, idea="p1:open")
    return action("trim", Action.REDUCE, idea="p1:open", fraction="0.5")


def test_n7_bounded_enumeration(tmp_path):
    branches: dict[str, int] = {}
    for n, (size, filled, cancel, exit_) in enumerate(CASES):
        case = (size, filled, cancel, exit_)
        h = sized(tmp_path / str(n), size)
        opened = partly_filled(h, filled)
        sent = watch_exit_sends(h)
        if cancel != "normal":
            h.broker.script_next_cancel(cancel)
        out = h.execute("p2", exit_action(exit_))
        check_sends(sent)
        assert held(h) >= 0 and traded(h, "SELL") <= traded(h, "BUY"), case
        events = h.events(out.order_ref)
        if exit_ == "REDUCE" and filled % 2:
            # 0.5 of an odd holding is not a whole unit: refused before any netting, as on main.
            branch = "refused before netting"
            assert out.state == IntentState.REFUSED and not sent, (case, out)
        elif cancel in ("normal", "FILL_FIRST"):
            # The entry is confirmed over: cancelled with its fill known, or filled in the race.
            branch = "resized in the same cycle" if cancel == "FILL_FIRST" else "sent unchanged"
            assert ("EXIT_RESIZED_TO_CONFIRMED_ENTRY" in events) == (cancel == "FILL_FIRST")
            final = size if cancel == "FILL_FIRST" else filled
            expected = final if exit_ == "CLOSE" else final * (filled // 2) // filled
            assert out.state == IntentState.FILLED and out.filled_quantity == expected, (case, out)
            assert len(sent) == 1 and EXIT_NOT_NETTED not in open_kinds(h), case
            left = final - expected
            assert held(h) == left == h.coord.store.position("p1:open").quantity, case
            stops = [o.remaining for o in h.broker.open_orders() if o.order_ref.endswith(".STP")]
            if left == 0:
                assert h.broker.open_orders() == [], case  # no orphaned stop
            else:
                assert stops == [left], case
        else:
            # The cancel is not confirmed (refused, or its acknowledgement lost): refused, final.
            branch = "refused as before"
            assert out.state == IntentState.REFUSED and EXIT_NOT_NETTED in out.reasons, (case, out)
            assert "EXIT_RESIZED_TO_CONFIRMED_ENTRY" not in events, case
            assert not sent and EXIT_NOT_NETTED in open_kinds(h), case
            if opened.order_ref in h.working():
                h.broker.fill(opened.order_ref)  # the remainder fills in between
            for _ in range(3):
                h.clock.advance(5)
                h.coord.sync()
            check_sends(sent)
            assert not sent and traded(h, "SELL") == 0, case
            assert h.coord.intent(out.order_ref).state == IntentState.REFUSED, case
        branches[branch] = branches.get(branch, 0) + 1
        h.coord.close()
    assert branches == {
        "refused before netting": 20,
        "sent unchanged": 8,
        "resized in the same cycle": 8,
        "refused as before": 24,
    }, branches


# ----------------------------------------------------------------- pinned limits and races


def test_a_close_refused_for_an_unconfirmed_entry_cancel_stays_refused(tmp_path):
    """The documented limit, as on main: the refusal is final and the follower keeps holding."""
    h = sized(tmp_path, 3)
    opened = partly_filled(h, 1)
    sent = watch_exit_sends(h)
    h.broker.script_next_cancel("REJECT")
    close = h.execute("p2", action("close", Action.CLOSE, idea="p1:open"))
    assert close.state == IntentState.REFUSED and EXIT_NOT_NETTED in open_kinds(h)
    h.broker.fill(opened.order_ref)  # the remainder fills after the refusal
    for _ in range(3):
        h.clock.advance(5)
        h.coord.sync()
    assert h.coord.intent(close.order_ref).state == IntentState.REFUSED
    assert not sent and traded(h, "SELL") == 0 and held(h) == 3
    [stop] = [o for o in h.broker.open_orders() if o.order_ref.endswith(".STP")]
    assert stop.remaining == 3  # protected, and the alert stays open
    assert EXIT_NOT_NETTED in open_kinds(h)
    h.coord.close()


def test_a_reduce_then_a_close_with_the_fill_first_race_sells_exactly_what_is_held(tmp_path):
    for n, close_cancel in enumerate(("normal", "FILL_FIRST")):
        h = sized(tmp_path / str(n), 4)
        partly_filled(h, 2)
        sent = watch_exit_sends(h)
        h.broker.script_next_cancel("FILL_FIRST")  # the entry fills during the reduce's netting
        h.broker.script_next_submit("REST")  # the reduction rests
        trim = h.execute("p2", action("trim", Action.REDUCE, idea="p1:open", fraction="0.5"))
        assert trim.state == IntentState.WORKING and h.coord.intent(trim.order_ref).quantity == 2
        if close_cancel != "normal":
            h.broker.script_next_cancel(close_cancel)  # the reduction fills before its cancel
        close = h.execute("p3", action("close", Action.CLOSE, idea="p1:open"))
        check_sends(sent)
        assert held(h) >= 0 and traded(h, "SELL") + held(h) == traded(h, "BUY") == 4
        if close_cancel == "normal":
            assert close.state == IntentState.FILLED and close.filled_quantity == 4, close
            assert held(h) == 0 and traded(h, "SELL") == 4 and h.broker.open_orders() == []
        else:
            # The position shrank during the close's netting: refused as on main, nothing sent.
            assert close.state == IntentState.REFUSED and EXIT_NOT_NETTED in close.reasons
            assert len(sent) == 1 and held(h) == 2 and traded(h, "SELL") == 2
        h.coord.close()


def test_a_second_netting_failure_after_the_resize_is_refused_with_nothing_sent(tmp_path):
    h = sized(tmp_path, 2)
    partly_filled(h, 1)
    sent = watch_exit_sends(h)
    h.broker.script_next_cancel("FILL_FIRST")  # the entry fills during the first netting
    h.broker.script_next_cancel("FILL_FIRST")  # the stop fills before its cancel, in the second
    close = h.execute("p2", action("close", Action.CLOSE, idea="p1:open"))
    assert "EXIT_RESIZED_TO_CONFIRMED_ENTRY" in h.events(close.order_ref)
    assert close.state == IntentState.REFUSED and EXIT_NOT_NETTED in close.reasons, close
    assert any(r.startswith("EXIT_NETTING_POSITION_CHANGED") for r in close.reasons)
    # Reachable: the stop (sized to 1 beside the 1 the close then covered) filled in the race.
    assert not sent and held(h) == 1 and traded(h, "SELL") == 1  # never short through a close
    # As on main when a stop sized beside a working exit fills in the netting race: the unit
    # left is reported unprotected, with both alerts open.
    assert h.coord.store.position("p1:open").quantity == 1
    assert {"EXIT_NOT_NETTED", "UNPROTECTED_POSITION"} <= set(open_kinds(h))
    h.coord.close()


# ----------------------------------------------------------------- through the mock service

# Invented posts: the mock ask is 88.42, so a buy limited at 88.30 rests. The service-mechanics
# mapping sizes the entry at 2 shares.
RESTING = "We are now long $TLT at 88.30. Stop 86.00."
CLOSE = "Closing our $TLT long at 88.40."


def test_through_the_service_the_close_reads_executed_and_the_follower_ends_flat():
    """The scenario of review note N7, end to end: 1 of 2 filled, the publisher closes, the cancel
    of the remainder loses the race. On the earlier code the CLOSE row read REFUSED, the follower
    held 2 and the EXIT_REFUSED alert was sent."""
    rt = Runtime(Path(tempfile.mkdtemp()), [post("1", 0, RESTING), post("2", 20, CLOSE)])
    rt.broker.script_next_submit("PARTIAL", quantity=1)
    rt.run(2)
    coord = rt.service.coordinator
    [opened] = [a for a in rt.actions() if a["action"] == "OPEN"]
    entry = coord.intent(opened["order_ref"])
    assert opened["status"] == "WORKING" and entry.quantity == 2
    assert coord.store.filled_quantity(entry.order_ref) == 1
    rt.broker.script_next_cancel("FILL_FIRST")  # the remainder fills before its cancel lands
    rt.run(5)  # the close arrives
    rt.run(6)  # nothing more is sent afterwards
    rows = {a["action"]: a for a in rt.actions()}
    assert rows["OPEN"]["status"] == "EXECUTED" and rows["CLOSE"]["status"] == "EXECUTED", rows
    assert EXIT_NOT_NETTED not in rows["CLOSE"]["reasons"]
    fills = rt.broker.executions()
    assert sum(f.quantity for f in fills if f.side.value == "BUY") == 2
    assert sum(f.quantity for f in fills if f.side.value == "SELL") == 2
    assert sum(p.quantity for p in rt.broker.positions()) == 0
    assert rt.broker.open_orders() == [] and rt.refs_placed_more_than_once() == {}
    assert coord.store.exceptions(open_only=True) == []
    assert N.EXIT_REFUSED not in rt.notifier.kinds()
    rt.service.close()
