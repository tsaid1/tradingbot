"""Operations controls in the runtime service: immediate de-duplicated alerts, the operator queue,
the broker disconnect / reconnect policy, the identity lockout and the heartbeat watchdog.
SimBroker and SimClock only; invented posts."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from runtime_harness import CLOSE, COMMENTARY, ENTRY, MAPPING, Runtime, post

from tradingbot.execution import operator_catalog as cat
from tradingbot.execution.coordinator import BROKER_RECONNECT_PENDING
from tradingbot.execution.store import LEDGER_REVERSAL, IntentState
from tradingbot.runtime import notify as N
from tradingbot.runtime import operator_actions as Q
from tradingbot.runtime.app import build_notifier
from tradingbot.runtime.config import ConfigError
from tradingbot.runtime.mapping import MappingPolicy
from tradingbot.runtime.operator_apply import session_state
from tradingbot.runtime.register import load_register
from tradingbot.runtime.watchdog import check

NOTE = "checked the invented account by hand"


def kinds(rt) -> list[str]:
    return rt.notifier.kinds()


def exec_kinds(rt) -> list[str]:
    return sorted(e.kind for e in rt.service.coordinator.store.exceptions(open_only=True))


def unknown_entry(tmp_path, **overrides) -> tuple[Runtime, str]:
    rt = Runtime(tmp_path, [post("1", 0, ENTRY)], **overrides)
    rt.broker.script_next_submit("LOST_ACK_NOT_PLACED")
    rt.step(5)
    [a] = rt.actions()
    assert a["status"] == "UNRESOLVED"
    return rt, a["order_ref"]


# ------------------------------------------------------------------------------ alerts


def test_an_entry_of_unknown_outcome_alerts_in_the_same_cycle_once_then_on_schedule(tmp_path):
    rt, ref = unknown_entry(tmp_path, notify={"realert_seconds": 600})
    assert kinds(rt).count(N.ENTRY_UNKNOWN_OUTCOME) == 1  # the cycle that caused it
    rt.run(20, 5)  # 100 s: still open, not repeated every cycle
    assert kinds(rt).count(N.ENTRY_UNKNOWN_OUTCOME) == 1
    rt.clock.advance(600)
    rt.step(5)
    sent = [t for k, t, _ in rt.notifier.sent if k == N.ENTRY_UNKNOWN_OUTCOME]
    assert sent == ["Entry of unknown outcome", "Still open: Entry of unknown outcome"]
    assert any(a["key"] == f"UNKNOWN_ENTRY:{ref}" for a in rt.store.alerts(open_only=True))


def test_broker_unreadable_alerts_at_the_first_failure(tmp_path):
    rt = Runtime(tmp_path, [])
    rt.step()
    rt.broker.lookup_failures = 1
    report = rt.step()
    assert report.skipped == "broker_unavailable"
    assert kinds(rt) == [N.BROKER_FAILURE]


def test_halt_engaged_and_cleared_both_notify_once(tmp_path):
    rt = Runtime(tmp_path, [])
    rt.halt()
    rt.run(3)
    rt.config.paths.halt_file.unlink()
    rt.run(2)
    assert kinds(rt) == [N.HALT_ENGAGED, N.HALT_CLEARED]


def test_reversal_and_other_execution_exceptions_alert_in_the_cycle_they_open(tmp_path):
    rt = Runtime(tmp_path, [])
    rt.step()
    rt.service.coordinator.store.raise_exception(
        rt.clock.now(), LEDGER_REVERSAL, "invented: exit fill exceeded the position",
        idea_id="x:1", order_ref="TB-x",
    )
    rt.step()
    assert kinds(rt) == [N.REVERSAL]
    rt.run(3)
    assert kinds(rt) == [N.REVERSAL]


def test_a_refused_exit_alerts(tmp_path):
    rt = Runtime(tmp_path, [post("1", 0, ENTRY), post("2", 30, CLOSE)])
    rt.step(30)
    # The position vanishes at the broker before the close: the coordinator refuses the exit.
    contract = rt.service.coordinator.store.position("1:a1")
    assert contract is not None and contract.quantity == 2
    rt.service.coordinator.store.update_position("1:a1", rt.clock.now(), status="CLOSED")
    rt.step(30)
    [_, close] = rt.actions()
    assert close["status"] in ("REFUSED", "HOLD", "BLOCK")
    assert N.EXIT_REFUSED in kinds(rt)


def test_an_open_position_under_a_revoked_mapping_alerts(tmp_path):
    rt = Runtime(tmp_path, [post("1", 0, ENTRY)])
    rt.step()
    revoked = tmp_path / "revoked.toml"
    text = MAPPING.read_text().replace("approved = true", "approved = false")
    revoked.write_text("\n".join(
        line for line in text.splitlines() if not line.startswith(("approved_by", "approved_at"))
    ))
    rt.service.mapping = MappingPolicy.from_toml(revoked)  # as after a restart with the edit
    rt.run(2)
    assert kinds(rt).count(N.MAPPING_REVOKED_OPEN_POSITION) == 1


def test_a_stalled_source_cursor_alerts_inside_the_activity_window(tmp_path):
    rt = Runtime(tmp_path, [], service={"source_stall_after_seconds": 60})
    rt.run(12, 10)
    assert kinds(rt).count(N.SOURCE_STALLED) == 1


def test_the_alerts_file_channel_is_durable_and_an_unknown_channel_is_refused(tmp_path):
    rt = Runtime(tmp_path, [], notify={"channels": ["log", "alerts_file"]})
    notifier = build_notifier(rt.config, rt.clock.now)
    notifier.notify(N.HALT_ENGAGED, "Emergency halt engaged", "invented")
    line = json.loads(rt.config.alerts_file.read_text().splitlines()[-1])
    assert line["kind"] == N.HALT_ENGAGED and line["message"] == "invented"
    assert rt.config.notification_log.exists()
    bad = rt.config.model_copy(
        update={"notify": rt.config.notify.model_copy(update={"channels": ["pager"]})}
    )
    with pytest.raises(ConfigError, match="pager"):
        build_notifier(bad)


# ------------------------------------------------------------------------------ operator queue


def test_a_queued_settlement_is_applied_by_the_service_on_its_next_cycle(tmp_path):
    rt, ref = unknown_entry(tmp_path)
    rt.run(3, 5)  # past the settle period: UNKNOWN_OUTCOME_ENTRY_NOT_RESENT is open
    queue = Q.OperatorQueue(rt.config.operator_db)
    with pytest.raises(Q.OperatorInputError, match="type exactly"):
        queue.enqueue(at=rt.clock.now(), operator="tester", origin="test", action=Q.SETTLE,
                     target=ref, outcome="NOT_AT_BROKER", note=NOTE, confirmation="yes")
    rid = queue.enqueue(at=rt.clock.now(), operator="tester", origin="test", action=Q.SETTLE,
                       target=ref, outcome="NOT_AT_BROKER", note=NOTE,
                       confirmation=cat.settle_phrase(ref, "NOT_AT_BROKER"))
    calls = rt.broker.submit_calls
    rt.step()
    [req] = queue.requests()
    assert req["id"] == rid and req["status"] == "APPLIED"
    assert rt.service.coordinator.intent(ref).state == IntentState.CANCELLED
    assert rt.broker.submit_calls == calls
    assert N.OPERATOR_ACTION in kinds(rt)
    # Still blocked: the settle period and spread no-trace lookups must follow it (review F1).
    assert exec_kinds(rt) == ["OPERATOR_SETTLEMENT_UNVERIFIED"]
    rt.run(5, 5)
    assert exec_kinds(rt) == []
    reg = load_register(rt.config, rt.clock.now())
    assert reg["unresolved_intents"] == [] and reg["requests"][0]["status"] == "APPLIED"
    with pytest.raises(Exception, match="append-only"), queue._tx() as c:
        c.execute("UPDATE operator_requests SET note='x'")


def test_a_contradicted_settlement_alerts_at_once_and_keeps_reconciliation_unclean(tmp_path):
    """Review F1 at the service: NOT_AT_BROKER declared while the broker's open orders lag; the
    resting entry then fills. Alerted in that cycle, blocked, reconciliation unclean; clearable
    only by a verified CLEAR once reconciliation is otherwise clean."""
    rt = Runtime(tmp_path, [post("1", 0, ENTRY)])
    rt.broker.open_orders_lag_seconds = 3600
    rt.broker.script_next_submit("LOST_ACK_PLACED_RESTING")
    rt.run(4, 5)
    [a] = rt.actions()
    ref = a["order_ref"]
    queue = Q.OperatorQueue(rt.config.operator_db)
    queue.enqueue(at=rt.clock.now(), operator="tester", origin="test", action=Q.SETTLE,
                  target=ref, outcome="NOT_AT_BROKER", note=NOTE,
                  confirmation=cat.settle_phrase(ref, "NOT_AT_BROKER"))
    rt.step()
    assert queue.requests()[-1]["status"] == "APPLIED"
    assert "OPERATOR_SETTLEMENT_UNVERIFIED" in exec_kinds(rt)
    rt.broker.fill(ref)
    rt.step()
    assert "SETTLED_INTENT_CONTRADICTED" in exec_kinds(rt)
    assert N.SETTLEMENT_CONTRADICTED in kinds(rt)
    assert rt.service.last_reconciliation.clean is False
    assert "SETTLED_INTENT_CONTRADICTED" in rt.heartbeat()["reconciliation"]["unexplained"]
    rt.run(3)
    assert kinds(rt).count(N.SETTLEMENT_CONTRADICTED) == 1
    [e] = [x for x in rt.service.coordinator.store.exceptions(open_only=True)
           if x.kind == "SETTLED_INTENT_CONTRADICTED"]
    assert cat.action_for(e.kind)[0] == cat.CLEAR
    rt.broker.open_orders_lag_seconds = 0  # the broker's view catches up: the stop is visible
    rt.step()
    assert "SETTLED_INTENT_CONTRADICTED" in rt.heartbeat()["reconciliation"]["unexplained"]
    queue.enqueue(at=rt.clock.now(), operator="tester", origin="test", action=Q.CLEAR,
                  target=str(e.id), kind=e.kind, note=NOTE,
                  confirmation=cat.clear_phrase(e.id, e.kind))
    rt.step()
    assert queue.requests()[-1]["status"] == "APPLIED", queue.requests()[-1]["reasons"]
    rt.step()
    assert rt.service.last_reconciliation.clean


def _settle_request(rt, queue, ref):
    return queue.enqueue(at=rt.clock.now(), operator="tester", origin="test", action=Q.SETTLE,
                         target=ref, outcome="NOT_AT_BROKER", note=NOTE,
                         confirmation=cat.settle_phrase(ref, "NOT_AT_BROKER"))


def test_a_missing_queue_is_never_created_implicitly(tmp_path):
    path = tmp_path / "state" / "operator.sqlite3"
    with pytest.raises(Q.OperatorQueueUnavailable, match="NOT_INITIALISED"):
        Q.OperatorQueue(path)
    assert not path.exists()
    queue = Q.OperatorQueue.initialise(path)
    assert path.exists() and queue.requests() == []


def test_request_ids_are_globally_unique_so_a_lost_queue_cannot_replay_an_old_result(tmp_path):
    """Review F3: the queue is lost and re-initialised; its counter restarts at 1. A new genuine
    settlement must be applied on its own merits, never answered with the old journal result."""
    rt, ref = unknown_entry(tmp_path)
    rt.run(3, 5)
    queue = Q.OperatorQueue(rt.config.operator_db)
    queue.enqueue(at=rt.clock.now(), operator="tester", origin="test", action=Q.SETTLE,
                  target=ref, outcome="FILLED", quantity=2, avg_price="88.42", note=NOTE,
                  confirmation=cat.settle_phrase(ref, "FILLED", 2, "88.42"))
    rt.step()
    [old] = queue.requests()
    assert old["id"] == 1 and old["status"] == "REFUSED"  # the broker holds nothing
    rt.config.operator_db.unlink()  # the queue is lost
    rt.step()
    assert not rt.config.operator_db.exists()  # never silently recreated
    alert = [a for a in rt.store.alerts(open_only=True) if a["key"] == "OPERATOR:QUEUE"]
    assert alert and "QUEUE_LOST" in alert[0]["detail"]
    assert N.OPERATOR_QUEUE_UNAVAILABLE in kinds(rt)
    # The explicit initialisation step: a fresh counter, fresh globally unique ids.
    queue = Q.OperatorQueue.initialise(rt.config.operator_db)
    rid = _settle_request(rt, queue, ref)
    assert rid == 1 and queue.requests()[0]["uid"] != old["uid"]
    rt.step()
    assert queue.requests()[0]["status"] == "APPLIED"
    assert rt.service.coordinator.intent(ref).state == IntentState.CANCELLED
    assert not any(a["key"] == "OPERATOR:QUEUE" for a in rt.store.alerts(open_only=True))


def test_a_corrupt_queue_disables_operator_actions_but_the_trading_loop_keeps_running(tmp_path):
    rt = Runtime(tmp_path, [post("1", 0, ENTRY), post("2", 30, CLOSE)])
    rt.step(5)
    assert rt.service.coordinator.store.position("1:a1").quantity == 2
    rt.config.operator_db.write_bytes(b"not a database at all" * 100)
    for _ in range(8):
        report = rt.step(5)  # never raises
    assert rt.service.coordinator.store.position("1:a1").status == "CLOSED"  # the exit ran
    assert rt.heartbeat()["cycle"] == rt.service.cycles
    assert rt.heartbeat()["operator_queue"]["state"] == "UNAVAILABLE"
    assert report.skipped is None
    [alert] = [a for a in rt.store.alerts(open_only=True) if a["key"] == "OPERATOR:QUEUE"]
    assert "QUEUE_UNREADABLE" in alert["detail"]
    assert kinds(rt).count(N.OPERATOR_QUEUE_UNAVAILABLE) == 1


def test_a_service_started_without_a_queue_runs_and_says_so(tmp_path):
    rt = Runtime(tmp_path, [post("1", 0, ENTRY)])
    rt.service.close()
    rt.config.operator_db.unlink()
    rt.restart()
    rt.run(3)
    assert not rt.config.operator_db.exists()
    assert rt.service.coordinator.store.position("1:a1").quantity == 2
    [alert] = [a for a in rt.store.alerts(open_only=True) if a["key"] == "OPERATOR:QUEUE"]
    assert "QUEUE_NOT_INITIALISED" in alert["detail"]


def test_a_journal_result_matches_only_the_same_id_target_and_outcome(tmp_path):
    from tradingbot.execution import settlement as S

    rt, ref = unknown_entry(tmp_path)
    rt.run(3, 5)
    coord = rt.service.coordinator
    first = S.settle_intent(coord, request_id="uid-1", order_ref=ref, outcome="NOT_AT_BROKER",
                            confirmation=cat.settle_phrase(ref, "NOT_AT_BROKER"), note=NOTE,
                            operator="tester")
    assert first.status == "APPLIED"
    again = S.settle_intent(coord, request_id="uid-1", order_ref=ref, outcome="NOT_AT_BROKER",
                            confirmation=cat.settle_phrase(ref, "NOT_AT_BROKER"), note=NOTE,
                            operator="tester")
    assert again.status == "APPLIED"  # the same request after a crash: the recorded result
    other = S.settle_intent(coord, request_id="uid-1", order_ref=ref, outcome="CANCELLED",
                            confirmation=cat.settle_phrase(ref, "CANCELLED"), note=NOTE,
                            operator="tester")
    assert other.status == "REFUSED" and other.reasons == ["REQUEST_ID_CONFLICT"]


# ------------------------------------------------------------------------------ reconnect


def test_reconnect_adopts_what_changed_at_the_broker_and_unblocks_only_after_resync(tmp_path):
    rt = Runtime(tmp_path, [post("1", 0, ENTRY)])
    rt.step(5)
    pos = rt.service.coordinator.store.position("1:a1")
    assert pos.quantity == 2 and pos.protection == "CONFIRMED"
    rt.broker.disconnect()
    rt.broker.reconnect_failures = 1  # the workstation is still restarting on the next cycle
    report = rt.step(5)
    assert report.skipped == "broker_unavailable"
    assert session_state(rt.store)["state"] == "DISCONNECTED"
    assert BROKER_RECONNECT_PENDING in exec_kinds(rt)
    assert kinds(rt) == [N.BROKER_FAILURE]
    # While the session is down the stop triggers at the broker.
    rt.broker.set_quote("TLT", "87.00", "87.02")
    rt.clock.advance(60)
    checks = rt.broker.identity_checks
    report = rt.step(5)
    assert rt.broker.identity_checks == checks + 1  # the adapter's identity checks ran again
    assert report.skipped is None
    assert session_state(rt.store)["state"] == "CONNECTED"
    assert BROKER_RECONNECT_PENDING not in exec_kinds(rt)
    pos = rt.service.coordinator.store.position("1:a1")
    assert pos.quantity == 0 and pos.status == "CLOSED"  # the stop fill adopted
    [ev] = [e for e in rt.store.events() if e["event"] == "BROKER_RECONNECTED"]
    assert any(f.endswith(".STP:2") for f in ev["detail"]["fills"])
    assert N.BROKER_RECONNECTED in kinds(rt)
    assert rt.service.last_reconciliation.clean


def test_reconnect_backs_off_boundedly(tmp_path):
    from datetime import datetime

    rt = Runtime(tmp_path, [], service={"max_backoff_seconds": 20})
    rt.step()
    rt.broker.disconnect()
    rt.broker.reconnect_failures = 6
    gaps = []
    for _ in range(7):
        at = rt.clock.now()
        rt.service.run_once()
        nxt = rt.service.health["broker"].next_attempt_at
        gaps.append((datetime.fromisoformat(nxt) - at).total_seconds() if nxt else None)
        rt.clock.advance(60)
    assert gaps == [5, 10, 20, 20, 20, 20, None]  # the seventh attempt reconnects
    assert session_state(rt.store)["state"] == "CONNECTED"


def test_exposure_stays_blocked_until_the_resync_shows_nothing_unexplained(tmp_path):
    from tradingbot.execution.broker import OrderIntent, OrderSide, OrderType
    from tradingbot.models import AssetClass, Instrument

    rt = Runtime(tmp_path, [post("0", 0, COMMENTARY), post("1", 120, ENTRY)])
    rt.step()
    rt.broker.disconnect()
    rt.broker.reconnect_failures = 1
    rt.step()
    rt.broker._connected = True  # someone trades the account by hand while we are away
    foreign = rt.broker.resolve(Instrument(asset_class=AssetClass.ETF, symbol="TLT"))
    rt.broker.submit(OrderIntent(order_ref="FOREIGN-1", contract=foreign, side=OrderSide.BUY,
                                 quantity=1, order_type=OrderType.MKT))
    rt.broker._connected = False
    rt.clock.advance(60)
    rt.run(40, 5)  # the entry arrives after the reconnect
    assert session_state(rt.store)["state"] == "RESYNCING"
    [_, a] = rt.actions()
    assert a["status"] == "REFUSED" and "BROKER_UNREADABLE" in a["reasons"]
    assert BROKER_RECONNECT_PENDING in exec_kinds(rt)


def test_a_second_outage_during_the_resync_is_reported_and_keeps_exposure_blocked(tmp_path):
    """Review F4: the session drops again while RESYNCING. It goes back to DISCONNECTED, the loss
    is reported, and what changed in the second gap is reported on the next reconnect."""
    from tradingbot.execution.broker import OrderIntent, OrderSide, OrderType
    from tradingbot.models import AssetClass, Instrument

    rt = Runtime(tmp_path, [post("1", 0, ENTRY)])
    rt.step(5)
    assert rt.service.coordinator.store.position("1:a1").quantity == 2
    rt.broker.disconnect()
    rt.broker.reconnect_failures = 1
    rt.step(5)
    rt.broker._connected = True  # someone trades the account by hand while we are away
    foreign = rt.broker.resolve(Instrument(asset_class=AssetClass.ETF, symbol="IEF"))
    rt.broker.submit(OrderIntent(order_ref="FOREIGN-1", contract=foreign, side=OrderSide.BUY,
                                 quantity=1, order_type=OrderType.MKT))
    rt.broker._connected = False
    rt.clock.advance(60)
    rt.step(5)
    assert session_state(rt.store)["state"] == "RESYNCING"  # waits on the foreign position
    # The second outage, during the resync; the stop triggers in that gap.
    rt.broker.disconnect()
    rt.broker.reconnect_failures = 1
    lost_before = sum(e["event"] == "BROKER_SESSION_LOST" for e in rt.store.events())
    rt.step(5)
    assert session_state(rt.store)["state"] == "DISCONNECTED"
    assert sum(e["event"] == "BROKER_SESSION_LOST" for e in rt.store.events()) == lost_before + 1
    assert BROKER_RECONNECT_PENDING in exec_kinds(rt)
    rt.broker.set_quote("TLT", "87.00", "87.02")
    rt.clock.advance(60)
    rt.step(5)
    reports = [e for e in rt.store.events() if e["event"] == "BROKER_RECONNECTED"]
    assert len(reports) == 2
    assert any(f.endswith(".STP:2") for f in reports[-1]["detail"]["fills"])
    assert kinds(rt).count(N.BROKER_RECONNECTED) == 2
    assert BROKER_RECONNECT_PENDING in exec_kinds(rt)  # the foreign position still waits


def test_a_session_under_another_account_halts_and_stops_touching_the_broker(tmp_path):
    rt = Runtime(tmp_path, [post("0", 0, COMMENTARY), post("1", 600, ENTRY)])
    rt.step()
    rt.broker.disconnect()
    rt.broker.next_session_account = "SIM-PAPER-9999"
    rt.step()
    rt.clock.advance(60)
    report = rt.step()
    assert report.skipped == "identity_lockout"
    assert session_state(rt.store)["state"] == "LOCKED_OUT"
    assert rt.config.paths.halt_file.exists()
    assert N.BROKER_IDENTITY_MISMATCH in kinds(rt) and N.HALT_ENGAGED in kinds(rt)
    touched = {"n": 0}
    for name in ("open_orders", "executions", "positions", "submit", "cancel", "modify"):
        real = getattr(rt.broker, name)

        def counting(*a, _real=real, **k):
            touched["n"] += 1
            return _real(*a, **k)

        setattr(rt.broker, name, counting)
    seen = len(rt.signals())
    rt.run(5, 300)  # the entry post is due meanwhile: nothing is read, consumed or sent
    assert touched["n"] == 0 and len(rt.signals()) == seen
    queue = Q.OperatorQueue(rt.config.operator_db)
    phrase = cat.lockout_phrase("SIM-PAPER-0001")
    queue.enqueue(at=rt.clock.now(), operator="tester", origin="test",
                 action=Q.CLEAR_IDENTITY_LOCKOUT, target="SIM-PAPER-0001", note=NOTE,
                 confirmation=phrase)
    rt.step()
    assert queue.requests()[-1]["reasons"] == ["BROKER_STILL_SHOWS_ANOTHER_ACCOUNT"]
    rt.broker.account = "SIM-PAPER-0001"  # the right session is connected again
    queue.enqueue(at=rt.clock.now(), operator="tester", origin="test",
                 action=Q.CLEAR_IDENTITY_LOCKOUT, target="SIM-PAPER-0001", note=NOTE,
                 confirmation=phrase)
    rt.step()
    assert queue.requests()[-1]["status"] == "APPLIED"
    rt.step()
    assert session_state(rt.store)["state"] == "CONNECTED"
    assert rt.config.paths.halt_file.exists()  # the halt is the operator's to clear


def _clear_lockout(rt, queue):
    queue.enqueue(at=rt.clock.now(), operator="tester", origin="test",
                  action=Q.CLEAR_IDENTITY_LOCKOUT, target="SIM-PAPER-0001", note=NOTE,
                  confirmation=cat.lockout_phrase("SIM-PAPER-0001"))
    rt.step()
    return queue.requests()[-1]


def test_clearing_a_lockout_reruns_every_identity_check_through_a_full_reconnect(tmp_path):
    """Review F2: the identity check failed but the session came back up by itself. Clearing the
    lockout must reconnect and run the checks afresh; while they fail the lockout stays."""
    rt = Runtime(tmp_path, [])
    rt.step()
    rt.broker.disconnect()
    rt.broker.fail_identity_check = True
    rt.step()
    assert session_state(rt.store)["state"] == "LOCKED_OUT"
    assert session_state(rt.store)["reason"] == "IDENTITY_CHECK_FAILED"
    rt.broker._connected = True  # the workstation reconnected on its own; the check still fails
    queue = Q.OperatorQueue(rt.config.operator_db)
    checks = rt.broker.identity_checks
    req = _clear_lockout(rt, queue)
    assert req["status"] == "REFUSED" and req["reasons"] == ["IDENTITY_CHECK_FAILED"]
    assert rt.broker.identity_checks == checks + 1  # the checks ran again
    assert session_state(rt.store)["state"] == "LOCKED_OUT"
    assert any(e["event"] == "IDENTITY_LOCKOUT_CLEAR_REFUSED" for e in rt.store.events())
    rt.broker.fail_identity_check = False
    req = _clear_lockout(rt, queue)
    assert req["status"] == "APPLIED"
    assert rt.broker.identity_checks == checks + 2
    rt.step()
    assert session_state(rt.store)["state"] == "CONNECTED"
    assert rt.config.paths.halt_file.exists()  # the halt stays the operator's to clear


def test_clearing_a_lockout_needs_the_accepted_session_identity_too(tmp_path):
    rt = Runtime(tmp_path, [])
    rt.step()
    accepted = rt.store.get("broker_identity")
    rt.broker.disconnect()
    real = rt.broker.session_identity
    rt.broker.session_identity = lambda: {**real(), "session": "SIMULATED_LIVE"}
    rt.step()
    assert session_state(rt.store)["reason"] == "SESSION_IDENTITY_CHANGED"
    queue = Q.OperatorQueue(rt.config.operator_db)
    req = _clear_lockout(rt, queue)
    assert req["status"] == "REFUSED" and req["reasons"] == ["SESSION_IDENTITY_NOT_ACCEPTED"]
    assert rt.store.get("broker_identity") == accepted
    rt.broker.session_identity = real
    assert _clear_lockout(rt, queue)["status"] == "APPLIED"


# ------------------------------------------------------------------------------ watchdog


def test_the_watchdog_alerts_on_a_stale_heartbeat_once_then_on_schedule_and_on_recovery(tmp_path):
    rt = Runtime(tmp_path, [], notify={"realert_seconds": 600})
    rt.step()
    sent = []

    class Rec:
        def notify(self, kind, title, message):
            sent.append((kind, title))

    now = rt.clock.now()
    assert check(rt.config, now, notifier=Rec())["stale"] is False
    late = now + timedelta(seconds=rt.config.heartbeat_stale_after + 1)
    assert check(rt.config, late, notifier=Rec())["stale"] is True
    check(rt.config, late + timedelta(seconds=60), notifier=Rec())
    check(rt.config, late + timedelta(seconds=601), notifier=Rec())
    assert [k for k, _ in sent] == [N.HEARTBEAT_STALE, N.HEARTBEAT_STALE]
    assert sent[1][1].startswith("Still open")
    rt.clock.advance(rt.config.heartbeat_stale_after + 700)
    rt.step()  # the service writes a fresh heartbeat
    check(rt.config, rt.clock.now(), notifier=Rec())
    assert sent[-1][0] == N.HEARTBEAT_RESTORED


def test_the_watchdog_alerts_when_there_is_no_heartbeat_at_all(tmp_path):
    rt = Runtime(tmp_path, [])
    rt.config.heartbeat_file.unlink(missing_ok=True)
    out = check(rt.config, rt.clock.now(), notifier=build_notifier(rt.config, rt.clock.now))
    assert out["stale"] and out["age_seconds"] is None
    assert N.HEARTBEAT_STALE in rt.config.alerts_file.read_text()


def test_a_day_entry_that_expired_while_disconnected_is_adopted_and_reported(tmp_path):
    rt = Runtime(tmp_path, [post("1", 0, ENTRY)])
    rt.broker.script_next_submit("REST")
    rt.step(5)
    [a] = rt.actions()
    assert rt.service.coordinator.intent(a["order_ref"]).state == IntentState.WORKING
    rt.broker.disconnect()
    rt.broker.reconnect_failures = 1
    rt.step(5)
    assert rt.broker.expire_day_orders() == 1  # the session ended while we were away
    rt.clock.advance(60)
    rt.run(3, 5)
    assert rt.service.coordinator.intent(a["order_ref"]).state == IntentState.CANCELLED
    assert rt.service.coordinator.store.position("1:a1").status == "NOT_FILLED"
    [ev] = [e for e in rt.store.events() if e["event"] == "BROKER_RECONNECTED"]
    assert ev["detail"]["ended"][a["order_ref"]] == "CANCELLED"
    assert session_state(rt.store)["state"] == "CONNECTED"
    assert rt.broker.open_orders() == []  # the waiting children were cancelled with it
