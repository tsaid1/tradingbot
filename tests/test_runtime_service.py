"""The runtime service end to end on invented posts: pipeline, holds, restart safety, backoff,
halt, heartbeat, notifications and stage timestamps. SimBroker and SimClock only."""

from __future__ import annotations

import json

import pytest
from runtime_harness import (
    ADD,
    CLOSE,
    COMMENTARY,
    DEMO_FIXTURE,
    ENTRY,
    INDEX_SHORT,
    RAISE_STOP,
    REDUCE_HALF,
    Runtime,
    post,
)

from tradingbot.execution.coordinator import order_ref_for
from tradingbot.execution.sim_broker import SimulatedCrash
from tradingbot.execution.store import IntentState
from tradingbot.runtime import notify as N
from tradingbot.runtime.app import build_service
from tradingbot.runtime.config import ConfigError
from tradingbot.runtime.lifecycle import latency_table, load
from tradingbot.runtime.mapping import UNSUPPORTED_INSTRUMENT_MAPPING
from tradingbot.runtime.sources import SourceError, SourceRateLimited

LIFECYCLE = [
    post("1", 0, ENTRY),
    post("2", 60, ADD),
    post("3", 120, RAISE_STOP),
    post("4", 180, REDUCE_HALF),
    post("5", 240, CLOSE),
]


def statuses(rt: Runtime) -> list[tuple[str, str | None, str]]:
    by_seq = {s["seq"]: s for s in rt.signals()}
    return [(by_seq[a["seq"]]["source_id"], a["action"], a["status"]) for a in rt.actions()]


# ------------------------------------------------------------------------------ the pipeline


def test_demo_fixture_runs_end_to_end_with_holds_and_no_duplicate_order(tmp_path):
    rt = Runtime(tmp_path, posts_file=DEMO_FIXTURE)
    rt.run(90)
    assert statuses(rt) == [
        ("9001", "OPEN", "EXECUTED"),
        ("9002", "ADD", "EXECUTED"),
        ("9003", "NO_TRADE", "NO_TRADE"),
        ("9002", "ADD", "EXECUTED"),  # the duplicate delivery: recorded outcome, no new order
        ("9001", "OPEN", "HOLD"),  # revision 2
        ("9005", "MODIFY", "EXECUTED"),
        ("9006", "OPEN", "HOLD"),  # index-quoted, unmapped
        ("9007", "REDUCE", "EXECUTED"),
        ("9008", "CLOSE", "EXECUTED"),
    ]
    assert [s["duplicate"] for s in rt.signals()] == [0, 0, 0, 1, 0, 0, 0, 0, 0]
    assert rt.refs_placed_more_than_once() == {}
    assert rt.service.last_reconciliation.clean
    assert rt.broker.positions() == [] and rt.broker.open_orders() == []
    held = {(e["source_id"], e["revision"]): e["reasons"] for e in rt.store.exceptions()}
    assert held[("9001", 2)] == ["SOURCE_EDIT_REQUIRES_REVIEW"]
    assert UNSUPPORTED_INSTRUMENT_MAPPING in held[("9006", 1)]
    assert len(held) == 2


def test_lifecycle_moves_protection_with_every_step(tmp_path):
    rt = Runtime(tmp_path, LIFECYCLE)
    stop_ref = order_ref_for("1", 1, "a1") + ".STP"

    def stop():
        return {o.order_ref: o for o in rt.broker.open_orders()}.get(stop_ref)

    rt.step(60)
    assert stop().quantity == 2 and str(stop().stop_price) == "87.20"
    rt.step(60)
    assert stop().quantity == 4
    rt.step(60)
    assert str(stop().stop_price) == "87.90"
    rt.step(60)
    assert stop().quantity - stop().filled_quantity == 2
    rt.step(60)
    assert stop() is None and rt.broker.positions() == []
    assert all(s == "EXECUTED" for _, _, s in statuses(rt))
    assert rt.service.last_reconciliation.clean


def test_unmapped_index_quoted_signal_never_reaches_the_broker(tmp_path):
    rt = Runtime(tmp_path, [post("1", 0, INDEX_SHORT)])
    rt.step()
    [a] = rt.actions()
    assert a["status"] == "HOLD" and a["mapping_status"] == "UNSUPPORTED"
    assert UNSUPPORTED_INSTRUMENT_MAPPING in a["reasons"] and a["order_ref"] is None
    assert rt.broker.submit_calls == 0 and rt.service.coordinator.store.intents() == []
    [exc] = rt.store.exceptions()
    assert exc["kind"] == "SIGNAL_HOLD"


def test_mapped_asset_with_a_non_direct_basis_is_held(tmp_path):
    mapping = tmp_path / "mapping.toml"
    mapping.write_text(
        "[[mapping]]\nsource_asset='TLT'\nbroker_symbol='TLT'\nbroker_asset_class='ETF'\n"
        "basis='PROXY'\n"
    )
    rt = Runtime(tmp_path, [post("1", 0, ENTRY)], mapping_file=mapping)
    rt.step()
    [a] = rt.actions()
    assert a["status"] == "HOLD" and a["mapping_reason"] == "BASIS_PROXY_NOT_EXECUTABLE"
    assert rt.broker.submit_calls == 0


def test_ticker_quoted_in_its_own_price_but_not_mapped_is_held(tmp_path):
    rt = Runtime(
        tmp_path,
        [post("1", 0, "We are now long $QQQ at 400.00. Stop 390.00.")],
        mock={"quotes": {"QQQ": {"bid": "400.00", "ask": "400.02"}}},
    )
    rt.step()
    [a] = rt.actions()
    assert a["status"] == "HOLD" and a["mapping_reason"] == "SOURCE_ASSET_NOT_MAPPED"
    assert rt.broker.submit_calls == 0


def test_commentary_is_journaled_but_not_an_exception(tmp_path):
    rt = Runtime(tmp_path, [post("1", 0, COMMENTARY)])
    rt.step()
    [a] = rt.actions()
    assert a["status"] == "NO_TRADE" and rt.store.exceptions() == []


def test_unverified_or_truncated_posts_are_held_by_the_gateway(tmp_path):
    rt = Runtime(
        tmp_path, [post("1", 0, ENTRY, verified=False), post("2", 1, ENTRY, complete=False)]
    )
    rt.run(2, 5)
    reasons = [a["reasons"] for a in rt.actions()]
    assert reasons == [["SOURCE_NOT_AUTHENTICATED"], ["SOURCE_TEXT_TRUNCATED"]]
    assert rt.broker.submit_calls == 0 and len(rt.store.exceptions()) == 2


def test_historical_replay_decides_at_availability_plus_lag(tmp_path):
    rt = Runtime(tmp_path, [post("1", 0, ENTRY)], compression=None, source={"lag_seconds": 3.0})
    rt.step()
    [s] = rt.signals()
    assert s["decision_now"] == "2026-03-02T15:00:03+00:00"
    [a] = rt.actions()
    assert a["status"] == "EXECUTED"


# ------------------------------------------------------------------------------ restart safety


def test_restart_keeps_the_cursor_and_the_interpreter_context(tmp_path):
    rt = Runtime(tmp_path, LIFECYCLE)
    rt.run(2, 60)  # entry and add
    cursor = rt.source.cursor()
    rt.restart()  # a new process
    assert rt.source.cursor() == cursor
    rt.run(4, 60)
    assert [s["source_id"] for s in rt.signals()] == ["1", "2", "3", "4", "5"]  # no skip, no repeat
    assert all(s == "EXECUTED" for _, _, s in statuses(rt))  # follow-ups still resolve the idea
    assert rt.refs_placed_more_than_once() == {}
    assert rt.service.last_reconciliation.clean


def test_kill_after_the_broker_accepted_the_order_places_no_second_order(tmp_path):
    rt = Runtime(tmp_path, LIFECYCLE[:2])
    rt.step(60)
    rt.broker.script_next_submit("CRASH_AFTER_PLACE")
    with pytest.raises(SimulatedCrash):
        rt.service.run_once()
    assert rt.source.cursor() == "1"  # the add was not committed
    rt.restart()
    rt.step()
    add_ref = order_ref_for("2", 1, "a1")
    assert len(rt.broker.orders_with_ref(add_ref)) == 1
    assert rt.service.coordinator.intent(add_ref).state == IntentState.FILLED
    [_, add] = rt.actions()
    assert "DUPLICATE_DELIVERY_NO_NEW_ORDER" in add["reasons"]
    assert rt.source.cursor() == "2" and rt.service.last_reconciliation.clean


def test_kill_after_journaling_but_before_the_cursor_moved_is_a_harmless_duplicate(tmp_path):
    rt = Runtime(tmp_path, [post("1", 0, ENTRY)])
    real_commit = rt.source.commit

    def die(p):
        raise SimulatedCrash("invented: died after the journal, before the cursor")

    rt.source.commit = die
    with pytest.raises(SimulatedCrash):
        rt.service.run_once()
    rt.source.commit = real_commit
    rt.restart()
    rt.step()
    assert [s["duplicate"] for s in rt.signals()] == [0, 1]
    assert rt.broker.submit_calls == 0  # the new process placed nothing
    assert len(rt.broker.all_orders()) == 2  # entry + stop, from the first process only
    assert rt.source.cursor() == "1"


# ------------------------------------------------------------------------------ failures


class FlakySource:
    """Wraps a source; raises the scripted errors on the next polls."""

    def __init__(self, inner, errors):
        self.inner, self.errors = inner, list(errors)
        self.name, self.realtime = inner.name, inner.realtime
        self.polls = 0

    def poll(self):
        self.polls += 1
        if self.errors:
            raise self.errors.pop(0)
        return self.inner.poll()

    def commit(self, p):
        self.inner.commit(p)

    def cursor(self):
        return self.inner.cursor()

    def next_poll_delay(self, cadence):
        return cadence


def test_source_errors_back_off_exponentially_keep_the_cursor_and_notify_once(tmp_path):
    rt = Runtime(tmp_path, [post("1", 0, ENTRY)])
    flaky = FlakySource(rt.source, [SourceError(f"invented outage {i}") for i in range(4)])
    rt.restart(source=flaky)
    inner = flaky.inner
    reports = []
    for _ in range(200):
        reports.append(rt.step(1))
        if rt.signals():
            break
    assert flaky.polls == 5 and inner.cursor() == "1"
    skipped = sum(1 for r in reports if r.skipped == "source_backoff")
    assert skipped == (5 - 1) + (10 - 1) + (20 - 1) + (40 - 1)  # 5, 10, 20, 40 s at 1 s steps
    assert rt.notifier.kinds().count(N.SOURCE_FAILURE) == 1  # at the 3rd consecutive failure
    hb = rt.heartbeat()
    assert hb["source"]["consecutive_failures"] == 0 and hb["source"]["last_error"]
    assert [a["status"] for a in rt.actions()] == ["EXECUTED"]
    assert [a["key"] for a in rt.store.alerts(open_only=True)] == []
    events = [e["event"] for e in rt.store.events()]
    assert events.count("SOURCE_FAILURE") == 4 and "SOURCE_RECOVERED" in events


def test_backoff_is_bounded(tmp_path):
    from datetime import datetime

    rt = Runtime(tmp_path, [], service={"max_backoff_seconds": 8})
    rt.restart(source=FlakySource(rt.source, [SourceError("invented")] * 6))
    gaps = []
    for _ in range(6):
        at = rt.clock.now()
        rt.service.run_once()
        nxt = datetime.fromisoformat(rt.service.health["source"].next_attempt_at)
        gaps.append((nxt - at).total_seconds())
        rt.clock.advance(60)
    assert gaps == [5, 8, 8, 8, 8, 8]


def test_rate_limit_retry_after_is_honoured(tmp_path):
    from datetime import datetime

    rt = Runtime(tmp_path, [post("1", 0, ENTRY)])
    rt.restart(source=FlakySource(rt.source, [SourceRateLimited("invented 429", retry_after=30)]))
    at = rt.clock.now()
    rt.service.run_once()
    nxt = datetime.fromisoformat(rt.service.health["source"].next_attempt_at)
    assert (nxt - at).total_seconds() == 30


def test_broker_outage_consumes_nothing_and_backs_off(tmp_path):
    rt = Runtime(tmp_path, [post("1", 0, ENTRY)])
    rt.broker.lookup_failures = 3  # the next three open_orders()/executions() calls fail
    reports = [rt.step(1) for _ in range(40)]
    assert [r.skipped for r in reports][:2] == ["broker_unavailable", "broker_backoff"]
    assert rt.notifier.kinds().count(N.BROKER_FAILURE) == 1
    [s] = rt.signals()  # processed once the broker answered, never before
    assert s["received_at"] > "2026-06-01T14:00:00"
    assert [a["status"] for a in rt.actions()] == ["EXECUTED"]
    assert rt.heartbeat()["broker"]["consecutive_failures"] == 0


def test_processing_fault_stops_before_the_cursor_and_never_logs_post_text(tmp_path):
    rt = Runtime(tmp_path, [post("1", 0, ENTRY), post("2", 1, ADD)])
    real = rt.service.interpreter.interpret
    calls = {"n": 0}

    def flaky(p):
        calls["n"] += 1
        if p.post_id == "2" and calls["n"] == 2:
            raise ValueError(f"invented parser failure on: {p.text}")
        return real(p)

    rt.service.interpreter.interpret = flaky
    rt.run(2, 1)  # post 1, then post 2 fails
    assert rt.source.cursor() == "1"
    hb = rt.heartbeat()
    assert hb["pipeline"]["consecutive_failures"] == 1
    assert hb["pipeline"]["last_error"] == "ValueError"  # the message (with text) is dropped
    rt.run(10, 1)
    assert rt.source.cursor() == "2"
    assert [a["status"] for a in rt.actions()] == ["EXECUTED", "EXECUTED"]
    blob = json.dumps(rt.store.events())
    assert ADD not in blob


# ------------------------------------------------------------------------------ halt


def test_halt_blocks_orders_is_journaled_and_notified_once_and_clears(tmp_path):
    rt = Runtime(tmp_path, [post("1", 0, ENTRY), post("2", 30, COMMENTARY)])
    rt.halt()
    rt.step(30)
    rt.step(30)
    [a, _] = rt.actions()
    assert a["status"] == "HALTED" and rt.broker.submit_calls == 0
    assert rt.notifier.kinds().count(N.HALT_ENGAGED) == 1
    assert rt.heartbeat()["halted"] is True
    rt.config.paths.halt_file.unlink()
    rt.step()
    events = [e["event"] for e in rt.store.events()]
    assert "HALT_ENGAGED" in events and "HALT_CLEARED" in events
    assert rt.heartbeat()["halted"] is False
    assert [e["kind"] for e in rt.store.exceptions()] == ["SIGNAL_HALTED"]


# ------------------------------------------------------------------------------ observability


def test_heartbeat_reports_source_and_broker_health_and_reconciliation(tmp_path):
    rt = Runtime(tmp_path, [post("1", 0, ENTRY)])
    rt.step()
    hb = rt.heartbeat()
    assert hb["mode"] == "mock" and hb["cycle"] == 1
    assert hb["source"]["name"] == "replay" and hb["source"]["cursor"] == "1"
    assert hb["source"]["last_success_at"] and hb["source"]["consecutive_failures"] == 0
    assert hb["broker"]["account"] == "SIM-PAPER-0001" and hb["broker"]["last_success_at"]
    assert hb["reconciliation"]["clean"] is True and hb["reconciliation"]["unexplained"] == []
    assert rt.store.get("heartbeat") == hb


def test_every_stage_is_timestamped_and_latency_comes_from_the_stamps(tmp_path):
    rt = Runtime(tmp_path, [post("1", 0, ENTRY)])
    rt.step()
    view = load(rt.config)
    [row] = view["lifecycle"]
    assert all(
        row["stamps"][k]
        for k in (
            "published",
            "received",
            "interpreted",
            "gate_decided",
            "submitted",
            "acknowledged",
            "filled",
        )
    )
    stamps = [
        row["stamps"][k]
        for k in (
            "published",
            "received",
            "interpreted",
            "gate_decided",
            "submitted",
            "acknowledged",
            "filled",
        )
    ]
    assert stamps == sorted(stamps)
    [lat] = latency_table(view["lifecycle"])
    assert lat["end_to_end"] is not None and lat["end_to_end"] >= 0
    assert row["protective"][0]["role"] == "STOP" and row["protective"][0]["working"]
    assert view["protection"][0]["protection"] == "CONFIRMED"
    assert view["positions"] == [{"symbol": "TLT", "follower": 2, "broker": 2, "match": True}]


def test_unprotected_position_is_notified(tmp_path):
    rt = Runtime(tmp_path, [post("1", 0, ENTRY)])
    rt.broker.script_next_submit("DROP_STOP_CHILD")
    rt.step()
    [a] = rt.actions()
    assert a["status"] == "EXCEPTION"
    assert N.UNPROTECTED_POSITION in rt.notifier.kinds()
    rt.step()
    assert rt.notifier.kinds().count(N.UNPROTECTED_POSITION) == 1  # not repeated every cycle


def test_stuck_intent_is_notified_once_and_its_held_signal_stays_open(tmp_path):
    rt = Runtime(tmp_path, [post("1", 0, ENTRY)])
    rt.broker.script_next_submit("LOST_ACK_NOT_PLACED")
    rt.broker.script_next_submit("LOST_ACK_NOT_PLACED")
    rt.step(5)
    [a] = rt.actions()
    assert a["status"] == "UNRESOLVED"
    rt.run(20, 5)  # round 2 (F5): an entry of unknown outcome is never resent
    assert rt.broker.submit_calls == 1
    assert rt.notifier.kinds().count(N.STUCK_INTENT) == 1
    [exc] = rt.store.exceptions(open_only=True)
    assert exc["kind"] == "SIGNAL_UNRESOLVED"


def test_unknown_outcome_is_settled_on_a_later_cycle_without_a_new_post(tmp_path):
    rt = Runtime(tmp_path, [post("1", 0, ENTRY)])
    rt.broker.script_next_submit("LOST_ACK_PLACED")  # placed and filled, the ack is lost
    rt.broker.lookup_failures_after_submit = 1  # and the first lookup fails
    rt.step(5)
    [a] = rt.actions()
    assert a["status"] == "UNRESOLVED" and rt.broker.submit_calls == 1
    rt.run(3, 5)  # no new post; the service keeps looking the intent up
    intent = rt.service.coordinator.intent(a["order_ref"])
    assert intent.state == IntentState.FILLED and rt.broker.submit_calls == 1
    assert rt.store.exceptions(open_only=True) == []  # the held UNRESOLVED signal was closed
    assert rt.service.last_reconciliation.clean


def test_unexplained_broker_position_is_notified(tmp_path):
    from tradingbot.execution.broker import OrderIntent, OrderSide, OrderType
    from tradingbot.models import AssetClass, Instrument

    rt = Runtime(tmp_path, [])
    rt.step()
    contract = rt.broker.resolve(Instrument(asset_class=AssetClass.ETF, symbol="TLT"))
    rt.broker.submit(
        OrderIntent(
            order_ref="FOREIGN-1",
            contract=contract,
            side=OrderSide.BUY,
            quantity=1,
            order_type=OrderType.MKT,
        )
    )
    rt.step()
    assert N.RECONCILIATION_DISCREPANCY in rt.notifier.kinds()
    assert "UNEXPLAINED_BROKER_POSITION" in rt.heartbeat()["reconciliation"]["unexplained"]


def test_no_post_text_is_written_anywhere_in_the_state(tmp_path):
    texts = [ENTRY, ADD, INDEX_SHORT, COMMENTARY]
    rt = Runtime(tmp_path, [post(str(i), i * 30, t) for i, t in enumerate(texts, 1)])
    rt.run(6, 30)
    for f in rt.config.paths.state_dir.iterdir():
        blob = f.read_bytes()
        for t in texts:
            assert t.encode() not in blob, f"post text found in {f.name}"


# ------------------------------------------------------------------------------ wiring


def test_build_service_refuses_a_missing_lexicon_and_loads_a_present_one(tmp_path):
    from tradingbot.runtime.config import InterpreterConfig

    lex = tmp_path / "lexicon.json"
    lex.write_text(json.dumps({"open_present": [r"\binvented entry verb\b"]}))
    rt = Runtime(tmp_path, [], interpreter={"lexicon_path": str(lex)})
    rt.service.close()  # one live coordinator per execution store (safety review R5)
    svc = build_service(rt.config)
    assert svc.interpreter.lexicon.open_present == [r"\binvented entry verb\b"]
    missing = rt.config.model_copy(
        update={"interpreter": InterpreterConfig(lexicon_path=tmp_path / "absent.json")}
    )
    with pytest.raises(ConfigError, match="lexicon"):
        build_service(missing)


def test_interpreter_context_round_trips_and_never_carries_post_text():
    from tradingbot.runtime.store import _encode, context_from_json, context_to_json
    from tradingbot.tkl_interpreter import TklInterpreter

    it = TklInterpreter()
    for p in (post("1", 0, ENTRY), post("2", 60, ADD), post("3", 120, RAISE_STOP)):
        it.observe(p, it.interpret(p))
    text = context_to_json(it.context)
    assert ENTRY not in text and ADD not in text
    back = context_from_json(text)
    assert back.ideas == it.context.ideas and back.observed_posts == it.context.observed_posts
    with pytest.raises(TypeError):
        _encode(post("4", 0, ENTRY))
