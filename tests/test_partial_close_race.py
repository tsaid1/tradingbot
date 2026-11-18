"""Review N7: a CLOSE that meets a partly filled entry whose remainder fills before its cancel.

The entry for 2 has 1 filled and is still working when the publisher's CLOSE arrives. The netting
cancels the remainder; the cancel loses the race and the remainder fills first. The entry's outcome
is then confirmed at the broker (fully filled), so the CLOSE exits the whole confirmed position,
once. With an outcome that is not confirmed nothing is sent and an exception stays open.

Invented ticker (the harness's), toy prices. No network, no broker port.
"""

from __future__ import annotations

from decimal import Decimal

from execution_harness import Harness, action, open_action

from tradingbot.execution.coordinator import EXIT_NOT_NETTED
from tradingbot.execution.store import IntentState
from tradingbot.models import Action, MockPolicy


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

CONFIRMED = {"normal": True, "FILL_FIRST": True, "LOST_ACK_APPLIED": True,
             "LOST_ACK_NOT_APPLIED": False, "REJECT": False}
CASES = [
    (size, filled, cancel, exit_)
    for size in (2, 3, 4)
    for filled in range(1, size)
    for cancel in CONFIRMED
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


def test_n7_bounded_enumeration(tmp_path):
    for n, (size, filled, cancel, exit_) in enumerate(CASES):
        case = (size, filled, cancel, exit_)
        h = sized(tmp_path / str(n), size)
        partly_filled(h, filled)
        sent = watch_exit_sends(h)
        final = size if cancel == "FILL_FIRST" else filled
        if cancel != "normal":
            h.broker.script_next_cancel(cancel)
        if exit_ == "CLOSE":
            act = action("close", Action.CLOSE, idea="p1:open")
        else:
            act = action("trim", Action.REDUCE, idea="p1:open", fraction="0.5")
        out = h.execute("p2", act)
        check_sends(sent)
        assert held(h) >= 0 and traded(h, "SELL") <= traded(h, "BUY"), case
        if exit_ == "REDUCE" and filled % 2:
            # 0.5 of an odd holding is not a whole unit: refused before any netting, as before.
            assert out.state == IntentState.REFUSED and not sent, (case, out)
            h.coord.close()
            continue
        expected = final if exit_ == "CLOSE" else final * (filled // 2) // filled
        if CONFIRMED[cancel]:
            assert out.state == IntentState.FILLED and out.filled_quantity == expected, (case, out)
            assert EXIT_NOT_NETTED not in open_kinds(h), case
        else:
            # Unknown outcome: nothing sent, the exception open, the entry still working.
            assert out.state == IntentState.REFUSED and EXIT_NOT_NETTED in out.reasons, (case, out)
            assert not sent and EXIT_NOT_NETTED in open_kinds(h), case
            assert held(h) == filled
            # A following cycle: the entry's cancel is now confirmed, so the exit is sent once
            # for the confirmed position.
            h.clock.advance(1)
            h.coord.recover()
            check_sends(sent)
            assert len(sent) == 1, (case, sent)
            assert h.coord.intent(out.order_ref).state == IntentState.FILLED, case
            assert EXIT_NOT_NETTED not in open_kinds(h), case
        assert traded(h, "SELL") == expected, case
        left = final - expected
        assert held(h) == left and h.coord.store.position("p1:open").quantity == left, case
        stops = [o for o in h.broker.open_orders() if o.order_ref.endswith(".STP")]
        if left == 0:
            assert h.broker.open_orders() == [], case  # nothing left behind, no orphaned stop
        else:
            assert [s.remaining for s in stops] == [left], case
        for _ in range(2):  # exactly once: later cycles send nothing more
            h.clock.advance(5)
            h.coord.recover()
        assert traded(h, "SELL") == expected and held(h) == left, case
        assert h.coord.reconcile().clean, case
        h.coord.close()


def test_an_awaiting_close_whose_entry_then_fills_is_sent_once_for_all_of_it(tmp_path):
    h = sized(tmp_path, 3)
    opened = partly_filled(h, 1)
    h.broker.script_next_cancel("REJECT")
    close = h.execute("p2", action("close", Action.CLOSE, idea="p1:open"))
    assert close.state == IntentState.REFUSED and EXIT_NOT_NETTED in open_kinds(h)
    h.broker.fill(opened.order_ref)  # the remainder fills before the next cycle
    assert held(h) == 3
    h.clock.advance(1)
    h.coord.recover()
    i = h.coord.intent(close.order_ref)
    assert i.state == IntentState.FILLED and i.quantity == 3, i
    assert held(h) == 0 and traded(h, "SELL") == 3 and h.broker.open_orders() == []
    assert EXIT_NOT_NETTED not in open_kinds(h)
    h.clock.advance(5)
    h.coord.recover()
    assert traded(h, "SELL") == 3 and h.coord.reconcile().clean
    h.coord.close()


def test_an_awaiting_close_sends_nothing_under_the_halt(tmp_path):
    h = sized(tmp_path, 2)
    partly_filled(h, 1)
    h.broker.script_next_cancel("REJECT")
    close = h.execute("p2", action("close", Action.CLOSE, idea="p1:open"))
    assert close.state == IntentState.REFUSED
    h.halt()
    h.clock.advance(1)
    h.coord.sync()
    assert traded(h, "SELL") == 0 and EXIT_NOT_NETTED in open_kinds(h)
    h.unhalt()
    h.clock.advance(1)
    h.coord.recover()
    assert h.coord.intent(close.order_ref).state == IntentState.FILLED
    assert held(h) == 0 and traded(h, "SELL") == traded(h, "BUY")
    h.coord.close()


def test_an_awaiting_reduce_applies_its_fraction_to_the_confirmed_final_quantity(tmp_path):
    h = sized(tmp_path, 4)
    opened = partly_filled(h, 2)
    h.broker.script_next_cancel("REJECT")
    trim = h.execute("p2", action("trim", Action.REDUCE, idea="p1:open", fraction="0.5"))
    assert trim.state == IntentState.REFUSED and traded(h, "SELL") == 0
    h.broker.fill(opened.order_ref)
    h.clock.advance(1)
    h.coord.recover()
    assert h.coord.intent(trim.order_ref).state == IntentState.FILLED
    assert traded(h, "SELL") == 2 and held(h) == 2  # half of the confirmed 4, not of the 2
    [stop] = [o for o in h.broker.open_orders() if o.order_ref.endswith(".STP")]
    assert stop.remaining == 2 and EXIT_NOT_NETTED not in open_kinds(h)
    h.coord.close()
