"""Second fix round (RUNTIME_AND_EXECUTION_FIXES_review.md): one reproducing test per item.

Invented tickers, toy prices and invented wording only. No network, no broker port.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import pytest
from execution_harness import SYMBOL, Harness, action, open_action
from runtime_harness import ENTRY, Runtime, post

from tradingbot.execution.broker import BrokerError, Fill, OrderSide
from tradingbot.execution.coordinator import BROKER_STATE_UNKNOWN
from tradingbot.execution.store import IntentState
from tradingbot.models import Action


def _open_kinds(h: Harness) -> list[str]:
    return [e.kind for e in h.coord.store.exceptions(open_only=True)]


# ----------------------------------------------------------------------------------------- F1


def test_f1_a_failed_executions_read_blocks_the_add_and_leaves_protection_unknown(tmp_path):
    h = Harness(tmp_path)
    opened = h.execute("p1", open_action())  # long 4, stop 48
    idea = "p1:open"
    assert opened.protection == "CONFIRMED"
    h.broker.set_quote(SYMBOL, "47.90", "47.94")  # the stop fills at the broker
    h.broker.set_quote(SYMBOL, "49.98", "50.02")
    assert h.broker.positions() == []
    real = h.broker.executions

    def failing(since=None):
        raise BrokerError("invented: executions unavailable")

    h.broker.executions = failing
    submits = h.broker.submit_calls
    add = h.execute("p2", action("add", Action.ADD, idea=idea, entry="50"))
    assert add.state == IntentState.REFUSED and "BROKER_UNREADABLE" in add.reasons
    assert h.broker.submit_calls == submits and h.broker.positions() == []
    assert h.coord.store.position(idea).protection == "UNKNOWN"
    assert BROKER_STATE_UNKNOWN in _open_kinds(h)
    with pytest.raises(BrokerError):
        h.coord.recover()  # a failed sync is never swallowed
    h.broker.executions = real
    h.coord.recover()  # a full successful sync applies the stop fill
    pos = h.coord.store.position(idea)
    assert pos.quantity == 0 and pos.status == "CLOSED"
    assert BROKER_STATE_UNKNOWN not in _open_kinds(h)


def test_f1_protection_needs_the_fills_read_after_the_working_orders(tmp_path):
    h = Harness(tmp_path)
    opened = h.execute("p1", open_action())
    idea = "p1:open"
    real = h.broker.executions
    calls = {"n": 0}
    late = Fill(
        exec_id="INVENTED-LATE-1",
        broker_order_id="999",
        order_ref=opened.order_ref,
        contract=h.broker.resolve(open_action().instrument),
        side=OrderSide.BUY,
        quantity=1,
        price=Decimal("50.00"),
        executed_at=h.clock.now(),
    )

    def executions(since=None):
        calls["n"] += 1
        # The first read of this sync misses a fill that the later read shows.
        return real(since) + ([late] if calls["n"] > 1 else [])

    h.broker.executions = executions
    h.coord.sync()
    assert h.coord.store.position(idea).protection != "CONFIRMED"


@pytest.mark.parametrize("read", ["executions", "completed_orders", "positions"])
def test_f1_the_service_broker_check_exercises_every_read(tmp_path, read):
    rt = Runtime(tmp_path, [post("1", 0, ENTRY)])

    def failing(*a, **k):
        raise BrokerError(f"invented: {read} unavailable")

    setattr(rt.broker, read, failing)
    report = rt.step()
    assert report.skipped == "broker_unavailable"
    assert rt.signals() == []  # no signal consumed while a broker read fails


# ----------------------------------------------------------------------------------------- F2


def _partial_then_stopped(h: Harness):
    h.broker.script_next_submit("PARTIAL", quantity=1)  # entry fills 1 of 4, 3 keep working
    out = h.execute("p1", open_action())
    assert h.coord.store.position("p1:open").quantity == 1
    assert h.working()[f"{out.order_ref}.STP"].remaining == 1
    return out


def test_f2_a_stop_out_cancels_the_working_entry_remainder(tmp_path):
    h = Harness(tmp_path)
    out = _partial_then_stopped(h)
    h.broker.set_quote(SYMBOL, "47.90", "47.94")  # the stop fires on the 1 held
    h.coord.sync()
    assert h.coord.store.position("p1:open").status == "CLOSED"
    assert out.order_ref not in h.working()  # the 3 left can no longer fill unprotected
    assert "ENTRY_REMAINDER_CANCELLED" in h.events(out.order_ref)
    h.broker.set_quote(SYMBOL, "49.98", "50.02")
    assert h.broker.positions() == []
    assert h.coord.reconcile().clean


def test_f2_an_unconfirmed_remainder_cancel_blocks_new_exposure_in_the_instrument(tmp_path):
    h = Harness(tmp_path)
    _partial_then_stopped(h)
    h.broker.set_quote(SYMBOL, "47.90", "47.94")
    for _ in range(4):  # every retry fails: this sync, its re-read, then the next pre-trade sync
        h.broker.script_next_cancel("REJECT")
    h.coord.sync()
    assert "ENTRY_REMAINDER_NOT_CANCELLED" in _open_kinds(h)
    h.broker.set_quote(SYMBOL, "49.98", "50.02")
    again = h.execute("p2", open_action())
    assert again.state == IntentState.REFUSED
    assert again.reasons[0].startswith("INSTRUMENT_BLOCKED")


def test_f2_a_resize_that_cannot_be_confirmed_blocks_new_exposure(tmp_path):
    h = Harness(tmp_path)
    out = _partial_then_stopped(h)
    for _ in range(3):  # this sync and the pre-trade sync of the next open both fail to resize
        h.broker.script_next_modify("REJECT")
    h.broker.fill(out.order_ref)  # the other 3 fill: the stop must grow from 1 to 4
    h.coord.sync()
    assert h.coord._outcome(out.order_ref).protection == "UNPROTECTED"
    again = h.execute("p2", open_action())
    assert again.state == IntentState.REFUSED
    assert again.reasons[0].startswith("INSTRUMENT_BLOCKED")


# ----------------------------------------------------------------------------------------- F3


def _kinds(report) -> list[str]:
    return [d.kind.value for d in report.discrepancies]


def test_f3_a_stop_price_changed_at_the_broker_is_never_confirmed_or_clean(tmp_path):
    h = Harness(tmp_path)
    out = h.execute("p1", open_action())  # long 4, ledger stop 48
    h.broker.modify(f"{out.order_ref}.STP", stop_price=Decimal(45))  # changed at the broker
    h.coord.sync()
    assert h.coord._outcome(out.order_ref).protection == "UNPROTECTED"
    report = h.coord.reconcile()
    assert "STOP_PRICE_MISMATCH" in _kinds(report) and not report.clean


def test_f3_a_break_even_stop_above_the_average_entry_is_valid(tmp_path):
    h = Harness(tmp_path)
    h.execute("p1", open_action())  # long 4 at 50.02
    h.broker.set_quote(SYMBOL, "52.98", "53.02")
    raised = h.execute("p2", action("raise", Action.MODIFY, idea="p1:open", stop="50.50"))
    assert raised.state == IntentState.APPLIED and raised.protection == "CONFIRMED"
    report = h.coord.reconcile()
    assert report.clean, report.discrepancies


def test_f3_a_stop_on_the_wrong_side_of_the_market_is_flagged(tmp_path):
    h = Harness(tmp_path)
    h.execute("p1", open_action())  # stop 48
    real = h.broker.quote

    def gapped(contract):  # the reconciliation's quote only; the simulated market is untouched
        return real(contract).model_copy(update={"bid": Decimal("47.50"), "ask": Decimal("47.54")})

    h.broker.quote = gapped
    report = h.coord.reconcile()
    [d] = [d for d in report.discrepancies if d.kind.value == "PROTECTIVE_STOP_INVALID"]
    assert "bid" in d.detail and not report.clean


# ----------------------------------------------------------------------------------------- F4


def _full_raw(tmp_path) -> dict:
    from runtime_harness import MAPPING, REQUIRED_EXECUTION, REQUIRED_POLICY, REQUIRED_SERVICE

    return {
        "mode": "mock",
        "paths": {"state_dir": "s", "halt_file": "h", "mapping_file": str(MAPPING)},
        "source": {"kind": "replay", "file": "p.jsonl"},
        "service": dict(REQUIRED_SERVICE),
        "policy": dict(REQUIRED_POLICY),
        "execution": dict(REQUIRED_EXECUTION),
    }


def test_f4_an_empty_policy_table_refuses_and_names_every_missing_value(tmp_path):
    from tradingbot.runtime.config import ConfigError, parse_config

    raw = _full_raw(tmp_path)
    raw["policy"] = {}
    with pytest.raises(ConfigError) as err:
        parse_config(raw, base=tmp_path)
    for key in ("quantity", "max_notional_usd", "max_aggregate_exposure_usd",
                "max_price_deviation_pct", "max_signal_age_seconds", "max_quote_age_seconds"):
        assert f"policy.{key}" in str(err.value)


@pytest.mark.parametrize(
    "table,key",
    [
        ("policy", "max_instrument_exposure_usd"),
        ("execution", "expected_account"),
        ("execution", "settle_seconds"),
        ("execution", "exit_no_trace_lookups"),
        ("service", "stuck_after_seconds"),
    ],
)
def test_f4_a_partial_config_refuses_naming_the_missing_value(tmp_path, table, key):
    from tradingbot.runtime.config import ConfigError, parse_config

    raw = _full_raw(tmp_path)
    del raw[table][key]
    with pytest.raises(ConfigError, match=rf"{table}\.{key}"):
        parse_config(raw, base=tmp_path)


def test_f4_the_service_runs_on_the_configured_values(tmp_path):
    rt = Runtime(tmp_path, [], execution={"settle_seconds": 17, "expected_account": "SIM-PAPER-0001"})
    cfg = rt.service.coordinator.config
    assert cfg.settle_seconds == 17 and cfg.expected_account == "SIM-PAPER-0001"


# ----------------------------------------------------------------------------------------- F5

UNKNOWN_OUTCOME_ENTRY_NOT_RESENT = "UNKNOWN_OUTCOME_ENTRY_NOT_RESENT"


def test_f5_late_visibility_probe_gives_one_order_not_two(tmp_path):
    """The reviewer's probe: open orders show a fresh order 30 s late, settle is 10 s."""
    h = Harness(tmp_path, settle_seconds=10)
    h.broker.open_orders_lag_seconds = 30
    h.broker.script_next_submit("LOST_ACK_PLACED_RESTING")  # live at the broker, ack lost
    out = h.execute("p1", open_action())
    assert out.state == IntentState.UNRESOLVED
    for _ in range(4):  # 20 s: past the settle period, still invisible at the broker
        h.clock.advance(5)
        h.broker.set_quote(SYMBOL, "49.98", "50.02")
        h.coord.recover()
    assert len(h.broker.orders_with_ref(out.order_ref)) == 1  # never resent
    assert h.coord.intent(out.order_ref).state == IntentState.UNRESOLVED
    assert UNKNOWN_OUTCOME_ENTRY_NOT_RESENT in _open_kinds(h)
    blocked = h.execute("p2", open_action())  # new exposure in the instrument is blocked
    assert blocked.state == IntentState.REFUSED
    assert "UNRESOLVED_ENTRY_IN_INSTRUMENT" in blocked.reasons
    h.clock.advance(15)  # now visible: adopted, still one order
    h.broker.set_quote(SYMBOL, "49.98", "50.02")
    h.coord.recover()
    assert h.coord.intent(out.order_ref).state == IntentState.WORKING
    assert len(h.broker.orders_with_ref(out.order_ref)) == 1
    assert UNKNOWN_OUTCOME_ENTRY_NOT_RESENT not in _open_kinds(h)


def test_f5_an_entry_with_no_trace_is_never_resent(tmp_path):
    h = Harness(tmp_path)
    h.broker.script_next_submit("LOST_ACK_NOT_PLACED")
    out = h.execute("p1", open_action())
    for _ in range(5):
        h.clock.advance(h.config.settle_seconds)
        h.broker.set_quote(SYMBOL, "49.98", "50.02")
        h.coord.recover()
    assert h.broker.submit_calls == 1
    assert h.coord.intent(out.order_ref).state == IntentState.UNRESOLVED
    assert UNKNOWN_OUTCOME_ENTRY_NOT_RESENT in _open_kinds(h)


def test_f5_an_exit_is_resent_only_after_three_no_trace_lookups_over_the_settle(tmp_path):
    h = Harness(tmp_path, settle_seconds=10)
    h.execute("p1", open_action())
    real = h.broker.submit

    def lost_then_restore(order, children=()):
        h.broker.submit = real
        h.broker.script_next_submit("LOST_ACK_NOT_PLACED")
        return real(order, children)

    h.broker.submit = lost_then_restore
    close = h.execute("p2", action("close", Action.CLOSE, idea="p1:open"))  # lookup 1, t=0
    assert close.state == IntentState.UNRESOLVED
    h.clock.advance(11)
    h.broker.set_quote(SYMBOL, "49.98", "50.02")
    h.coord.recover()  # lookup 2, t=11: past the settle, but only two lookups
    assert h.broker.submit_calls == 2 and close.order_ref not in h.working()
    h.clock.advance(1)
    h.broker.set_quote(SYMBOL, "49.98", "50.02")
    h.coord.recover()  # lookup 3, t=12: three no-trace lookups spread over >= 10 s
    assert h.coord.intent(close.order_ref).state == IntentState.FILLED
    assert len(h.broker.orders_with_ref(close.order_ref)) == 1


# ------------------------------------------------------------------- F8: stale recorded exit


def _close_recorded_never_sent(h: Harness) -> str:
    h.execute("p1", open_action())
    ref = h.record_never_sent("p2", action("close", Action.CLOSE, idea="p1:open"))
    h.clock.advance(3 * 24 * 3600)  # found days later
    return ref


def test_f8_an_old_recorded_exit_is_repriced_from_a_fresh_quote(tmp_path):
    h = Harness(tmp_path)
    ref = _close_recorded_never_sent(h)
    old = h.coord.store.order(ref).limit_price
    h.broker.set_quote(SYMBOL, "52.98", "53.02")  # today's market
    h.coord.recover()
    [sent] = h.broker.orders_with_ref(ref)
    band = h.policy.max_price_deviation_pct / Decimal(100)
    fresh = Decimal("52.98") * (1 - band)
    assert sent.limit_price != Decimal(old)
    assert sent.limit_price == fresh.quantize(Decimal("0.01"), rounding="ROUND_CEILING")
    assert "EXIT_REPRICED" in h.events(ref)


def test_f8_an_old_recorded_exit_without_a_usable_quote_becomes_an_exception(tmp_path):
    h = Harness(tmp_path)
    ref = _close_recorded_never_sent(h)  # the last quote is three days old
    h.coord.recover()
    assert h.broker.orders_with_ref(ref) == []
    intent = h.coord.intent(ref)
    assert intent.state == IntentState.REFUSED and "EXIT_WITHOUT_QUOTE" in intent.reasons
    assert "EXIT_WITHOUT_QUOTE" in _open_kinds(h)


# ------------------------------------------------------------- F9: one net position per contract


def _short_open(action_id: str = "open"):
    from tradingbot.models import PositionSide

    return open_action(action_id, side=PositionSide.SHORT, entry="50", stop="52", target="45")


def test_f9_an_opposite_open_in_the_same_contract_is_held(tmp_path):
    h = Harness(tmp_path)
    h.post("p1", open_action())  # long 4
    result = h.post("p2", _short_open())
    [o] = result.outcomes
    assert o.status == "HOLD" and "OPPOSITE_IDEA_IN_SAME_CONTRACT" in o.reasons
    assert [p.quantity for p in h.broker.positions()] == [4]
    direct = h.execute("p3", _short_open())  # the coordinator refuses it as well
    assert direct.state == IntentState.REFUSED
    assert direct.reasons == ["OPPOSITE_IDEA_IN_SAME_CONTRACT"]


def test_f9_a_publisher_reversal_is_a_close_then_an_open(tmp_path):
    h = Harness(tmp_path)
    h.post("p1", open_action())
    result = h.post("p2", action("close", Action.CLOSE, idea="p1:open"), _short_open("open2"))
    assert [o.status for o in result.outcomes] == ["EXECUTED", "EXECUTED"]
    assert [p.quantity for p in h.broker.positions()] == [-4]
    assert h.coord.reconcile().clean


# ------------------------------------------------------------------- F6: backoff never overflows


def test_f6_a_long_failure_run_keeps_the_backoff_bounded(tmp_path):
    rt = Runtime(tmp_path, [])
    now = rt.clock.now()
    for _ in range(5000):  # far beyond the ~1,024 failures that used to overflow 2 ** n
        rt.service._failure("broker", BrokerError("invented: still down"), now)
    h = rt.service.health["broker"]
    assert h.consecutive_failures == 5000
    wait = (datetime.fromisoformat(h.next_attempt_at) - now).total_seconds()
    assert wait == rt.config.service.max_backoff_seconds


# ------------------------------------------------------------------- F7: a real source baseline


def test_f7_an_empty_first_page_still_sets_a_real_baseline(tmp_path):
    from test_runtime_sources import RATE, FakeX, make, raw

    from tradingbot.runtime.store import RuntimeStore

    store = RuntimeStore(tmp_path / "runtime.sqlite3")
    fake = FakeX()
    fake.timeline.append((200, {"data": []}, RATE))  # the account has no posts yet
    src, _ = make(store, fake)
    assert src.poll() == [] and src.cursor() != "0"
    before = raw("50", ENTRY, created="2026-05-30T10:00:00Z")  # existed before the first run
    after = raw("60", ENTRY, created="2026-06-01T14:00:05Z")
    fake.timeline.append((200, {"data": [before, after]}, RATE))
    assert [p.post_id for p in src.poll()] == ["60"]
    assert "start_time=" in fake.timeline_urls()[-1]


class _LiveStub:
    """A realtime source handing over one live post once (invented)."""

    name = "live_stub"
    realtime = True

    def __init__(self, posts):
        self.posts, self.committed = list(posts), []

    def poll(self):
        return [p for p in self.posts if p not in self.committed]

    def commit(self, p):
        self.committed.append(p)

    def cursor(self):
        return str(len(self.committed))

    def next_poll_delay(self, cadence):
        return cadence


def test_f7_a_live_post_older_than_the_first_start_is_never_acted_on(tmp_path):
    from datetime import timedelta

    from tradingbot.source_events import Provenance

    rt = Runtime(tmp_path, [])
    start = rt.clock.now()
    old = post("1", 0, ENTRY).model_copy(
        update={
            "provenance": Provenance.LIVE_API_USER_CONTEXT,
            "published_at": start - timedelta(hours=1),
            "received_at": start,
        }
    )
    stub = _LiveStub([old])
    rt.restart(source=stub)
    rt.step()
    assert stub.committed == [old]  # consumed, so it never comes back
    assert rt.signals() == [] and rt.broker.all_orders() == []
    assert "POST_BEFORE_FIRST_START" in [e["event"] for e in rt.store.events()]
