"""Operator confirmation, milestone 2: the service, the operator queue, the card. Mock mode on the
strict simulated broker; invented posts and markets only. Each test proves one invariant of
docs/OPERATOR_CONFIRMATION.md (section 8)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from runtime_harness import Runtime, post
from test_operator_confirmation_core import APPROX, ENTRY, MENTION

from tradingbot.event_models import ParsedPost
from tradingbot.execution.broker import BrokerEnvironment
from tradingbot.execution.operator_catalog import offer_phrase
from tradingbot.execution.sim_broker import SimBroker
from tradingbot.execution.store import IntentState
from tradingbot.models import ExecutionPlan
from tradingbot.runtime import confirmation as C
from tradingbot.runtime import notify as N
from tradingbot.runtime import operator_actions as Q
from tradingbot.runtime.config import ConfigError, MockQuote
from tradingbot.runtime.confirmation_view import WHAT_IT_DOES, card
from tradingbot.runtime.mapping import MappingPolicy
from tradingbot.runtime.operator_apply import apply_pending

LATER = "Closing our $QETF long here at 50.12."
QUOTES = {"QETF": {"bid": "50.09", "ask": "50.11"}}


def rt(tmp_path, posts=(APPROX,), *, enabled=True, gap=0, **over) -> Runtime:
    mapping = tmp_path / "m.toml"
    mapping.write_text(ENTRY)
    over.setdefault("mock", {"quotes": QUOTES})
    return Runtime(tmp_path, [post(str(7000 + i), i * gap, t) for i, t in enumerate(posts)],
                   mapping_file=mapping, operator_confirmation={"enabled": enabled}, **over)


def offers(r: Runtime) -> list[dict]:
    return r.store.offers()


def request(r: Runtime, offer: dict, action=Q.CONFIRM_SIGNAL, *, digest=None) -> None:
    digest = digest or offer["hash"]
    Q.OperatorQueue(r.config.operator_db).enqueue(
        at=r.clock.now(), operator="owner", origin="test", action=action,
        target=str(offer["id"]), kind=digest, note="",
        confirmation=offer_phrase(action, offer["id"], digest))


def results(r: Runtime) -> list[dict]:
    return [x for x in Q.OperatorQueue(r.config.operator_db).requests()
            if x["action"] in Q.SIGNAL_OFFER_ACTIONS]


def events(r: Runtime, prefix="CONFIRMATION") -> list[dict]:
    return [e for e in r.store.events() if e["event"].startswith(prefix)
            or e["event"].startswith("OPERATOR_CONFIRMED") or e["event"].startswith(
                "OPERATOR_DISMISSED")]


def entries(r: Runtime):
    return [o for o in r.broker.all_orders() if o.parent_ref is None]


def offered(tmp_path, **kw) -> tuple[Runtime, dict]:
    r = rt(tmp_path, **kw)
    r.step(1)
    [o] = offers(r)
    assert o["state"] == "OFFERED", o
    return r, o


# ================================================================== off equals main

def test_with_the_switch_off_nothing_of_it_runs_or_exists(tmp_path, monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("the waived evaluation ran with the switch off")

    monkeypatch.setattr(MappingPolicy, "apply_waived", refuse)
    monkeypatch.setattr(MappingPolicy, "resolve_waived", refuse)
    r = rt(tmp_path, (APPROX, MENTION), enabled=False, gap=10)
    r.run(6, 5)
    assert [a["status"] for a in r.actions() if a["action"] == "OPEN"] == ["HOLD", "HOLD"]
    assert r.service.confirmation is None and not r.store.has_offers()
    with sqlite3.connect(r.config.runtime_db) as c:
        tables = {n for (n,) in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "confirmation_offers" not in tables
    assert not events(r) and N.SIGNAL_CONFIRMABLE not in r.notifier.kinds()
    assert not entries(r)


class _Live(SimBroker):
    def environment(self):
        return BrokerEnvironment.LIVE


class _Undeclared(SimBroker):
    def environment(self):
        raise RuntimeError("no declaration")


@pytest.mark.parametrize("broker", [_Live, _Undeclared])
def test_the_switch_is_refused_unless_the_broker_declares_sim_or_paper(tmp_path, broker):
    r = rt(tmp_path, enabled=False)
    from tradingbot.runtime.service import RuntimeService
    config = r.config.model_copy(update={"operator_confirmation": r.config.operator_confirmation
                                         .model_copy(update={"enabled": True})})
    r.service.close()
    with pytest.raises(ConfigError, match="SIM or PAPER"):
        RuntimeService(config, source=r.source, broker=broker(r.config.broker_db,
                                                              clock=r.clock.now),
                       clock=r.clock.now, store=r.store)


# ================================================================== the whole path

def test_a_confirmable_hold_is_offered_alerted_and_once_confirmed_runs_the_chain(tmp_path):
    r, o = offered(tmp_path)
    assert N.SIGNAL_CONFIRMABLE in r.notifier.kinds()
    assert [e["event"] for e in events(r)] == ["CONFIRMATION_OFFERED"]
    assert not entries(r)  # held: nothing traded yet
    request(r, o)
    r.step(1)
    [o] = offers(r)
    assert o["state"] == "EXECUTED", o
    [entry] = entries(r)
    assert entry.limit_price is not None and str(entry.limit_price) == "50.11"
    [intent] = r.service.coordinator.store.intents()
    plan = ExecutionPlan.model_validate_json(intent.plan)
    assert str(plan.entry_anchor) == "50.10"  # the stated level, as written
    assert not [e for e in r.store.exceptions(open_only=True) if e["kind"].startswith("SIGNAL_")]
    # One-shot: a second press is refused, and nothing is placed twice.
    request(r, o)
    r.step(1)
    assert results(r)[-1]["status"] == "REFUSED"
    assert results(r)[-1]["reasons"] == ["OFFER_NOT_OPEN:EXECUTED"]
    assert r.refs_placed_more_than_once() == {} and len(entries(r)) == 1
    assert [e["event"] for e in events(r)] == ["CONFIRMATION_OFFERED",
                                               "OPERATOR_CONFIRMED_SIGNAL",
                                               "CONFIRMATION_RESULT"]


# ================================================================== the chain still refuses

def _move(r: Runtime, bid: str | None, ask: str | None) -> None:
    quotes = dict(r.service.config.mock.quotes)
    if bid is None:
        quotes.pop("QETF")
    else:
        quotes["QETF"] = MockQuote(bid=bid, ask=ask)
    r.service.config.mock.__dict__["quotes"] = quotes


def test_the_market_ran_away_the_confirmed_entry_holds_with_the_gateway_reason(tmp_path):
    r, o = offered(tmp_path)
    _move(r, "50.30", "50.32")
    request(r, o)
    r.step(1)
    [o] = offers(r)
    assert o["state"] == "HELD" and o["result"]["reasons"][0] == "ENTRY_ANCHOR_ADVERSE_MOVE"
    assert not entries(r)


def test_a_quote_that_stopped_updating_holds_the_confirmed_entry(tmp_path):
    r, o = offered(tmp_path)
    _move(r, None, None)  # the simulated market stops publishing: the last quote ages
    r.clock.advance(5)
    request(r, o)
    r.step(1)
    [o] = offers(r)
    assert o["state"] == "HELD" and o["result"]["reasons"][0] == "ENTRY_ANCHOR_QUOTE_SIDE_STALE"
    assert not entries(r)


def test_the_exposure_cap_still_refuses_a_confirmed_entry(tmp_path):
    r, o = offered(tmp_path, policy={"max_instrument_exposure_usd": "10"})
    request(r, o)
    r.step(1)
    [o] = offers(r)
    assert o["state"] == "REJECTED" and not entries(r), o
    assert any(x.startswith("INSTRUMENT_EXPOSURE_CAP") for x in o["result"]["reasons"])


def test_a_pending_reconnect_still_refuses_a_confirmed_entry(tmp_path):
    r, o = offered(tmp_path)
    from tradingbot.execution.coordinator import BROKER_RECONNECT_PENDING
    r.service.coordinator.store.raise_exception(r.clock.now(), BROKER_RECONNECT_PENDING,
                                                "test: session to resync")
    request(r, o)
    r.step(1)
    [o] = offers(r)
    assert o["state"] == "REJECTED" and not entries(r), o
    assert "BROKER_UNREADABLE" in o["result"]["reasons"]  # a stale ledger never adds exposure


def test_the_account_check_still_refuses_a_confirmed_entry(tmp_path):
    r, o = offered(tmp_path)
    coordinator = r.service.coordinator
    coordinator.config = coordinator.config.model_copy(
        update={"expected_account": "SIM-OTHER-0001"})
    request(r, o)
    r.step(1)
    [o] = offers(r)
    assert o["state"] == "REJECTED" and "BROKER_ACCOUNT_MISMATCH" in o["result"]["reasons"]
    assert not entries(r)


def test_the_halt_invalidates_every_open_offer_and_refuses_the_confirmation(tmp_path):
    r, o = offered(tmp_path)
    request(r, o)
    r.halt()
    r.step(1)
    [o] = offers(r)
    assert o["state"] == "INVALIDATED" and o["state_reason"] == "HALT"
    assert results(r)[-1]["status"] == "REFUSED" and not entries(r)


def test_an_offer_is_never_applied_after_its_expiry(tmp_path):
    r, o = offered(tmp_path)
    request(r, o)
    r.clock.advance(91)  # the request waits past the 90 s signal age limit
    r.step(1)
    [o] = offers(r)
    assert o["state"] == "EXPIRED" and results(r)[-1]["reasons"] == [C.CONFIRMATION_EXPIRED]
    assert not entries(r)


def test_an_unanswered_offer_expires_and_is_journaled_so(tmp_path):
    r, o = offered(tmp_path)
    r.run(20, 5)
    [o] = offers(r)
    assert o["state"] == "EXPIRED"
    [e] = [e for e in events(r) if e["event"] == "CONFIRMATION_EXPIRED"]
    assert e["detail"]["unanswered"] is True
    assert not [a for a in r.store.alerts(open_only=True) if a["key"].startswith("OFFER:")]


def test_a_broker_that_stops_declaring_paper_is_refused_at_apply(tmp_path):
    r, o = offered(tmp_path)
    r.broker.environment = lambda: BrokerEnvironment.LIVE
    request(r, o)
    r.step(1)
    [o] = offers(r)
    assert o["state_reason"] == "ENVIRONMENT_NOT_SIM_OR_PAPER" and not entries(r)


# ================================================================== binding

def test_a_stale_card_cannot_confirm_another_proposal(tmp_path):
    r, o = offered(tmp_path)
    request(r, o, digest="0" * 64)
    r.step(1)
    assert results(r)[-1]["reasons"] == ["PROPOSAL_CHANGED"]
    assert offers(r)[0]["state"] == "OFFERED" and not entries(r)


def test_a_dismissed_offer_cannot_be_confirmed(tmp_path):
    r, o = offered(tmp_path)
    request(r, o, Q.DISMISS_SIGNAL)
    request(r, o)
    r.step(1)
    assert offers(r)[0]["state"] == "DISMISSED"
    assert [x["status"] for x in results(r)] == ["APPLIED", "REFUSED"] and not entries(r)


def test_a_later_instruction_on_the_same_market_invalidates_the_offer(tmp_path):
    r = rt(tmp_path, (APPROX, LATER), gap=4)
    r.run(3, 2)
    [o] = offers(r)
    assert o["state"] == "INVALIDATED" and o["state_reason"] == "LATER_INSTRUCTION"


def test_a_revision_of_the_post_invalidates_the_offer(tmp_path):
    mapping = tmp_path / "m.toml"
    mapping.write_text(ENTRY)
    edited = post("7100", 4, APPROX.replace("48.90", "48.80"), source_id="7000", revision=2,
                  revised=4)
    r = Runtime(tmp_path, [post("7000", 0, APPROX), edited], mapping_file=mapping,
                mock={"quotes": QUOTES}, operator_confirmation={"enabled": True})
    r.run(3, 2)
    [o] = offers(r)
    assert o["state"] == "INVALIDATED" and o["state_reason"] == "REVISION"


def test_a_restart_in_applying_never_runs_the_action_again(tmp_path):
    r, o = offered(tmp_path)
    assert r.store.set_offer_state(o["id"], expect=("OFFERED",), state="APPLYING",
                                   at=r.clock.now())
    r.restart()
    r.step(1)
    [o] = offers(r)
    assert o["state"] == "REJECTED" and o["state_reason"] == "APPLY_INTERRUPTED"
    assert not entries(r)


def test_a_restart_with_another_mapping_entry_ends_the_offer(tmp_path):
    r, o = offered(tmp_path)
    r.config.paths.mapping_file.write_text(ENTRY.replace('max_notional = "150"',
                                                         'max_notional = "140"'))
    r.restart()
    [o] = offers(r)
    assert o["state"] == "INVALIDATED" and o["state_reason"] == "MAPPING_CHANGED"


def test_switching_off_ends_open_offers(tmp_path):
    r, _o = offered(tmp_path)
    r.service.close()
    r.config = r.config.model_copy(update={"operator_confirmation": r.config
                                           .operator_confirmation.model_copy(
                                               update={"enabled": False})})
    r.service = None
    r.restart()
    assert offers(r)[0]["state_reason"] == "FEATURE_DISABLED"


# ================================================================== the ordinary path refuses it

def test_the_ordinary_path_cannot_release_a_held_action(tmp_path):
    r, o = offered(tmp_path)
    offer = C.Offer.from_record(o["record"])
    source = C.source_of(offer)
    # The ordinary replay: the post is on record, so the confirmed action is a duplicate.
    result = r.service.replay.process(source, ParsedPost(actions=[offer.proposal]), {},
                                      now=r.clock.now())
    assert result.duplicate and not entries(r)
    # The confirmed path without a confirmed offer's authority.
    with pytest.raises(PermissionError):
        C.authorize(offer, state="OFFERED", now=r.clock.now())
    with pytest.raises(PermissionError):
        r.service.replay.process_confirmed(source, offer.proposal, {}, now=r.clock.now(),
                                           authority=None)
    forged = C.authorize(offer, state="APPLYING", now=r.clock.now())
    other = offer.proposal.model_copy(update={"stop_price": "45.00"})
    with pytest.raises(PermissionError):
        r.service.replay.process_confirmed(source, other, {}, now=r.clock.now(),
                                           authority=forged)
    assert not entries(r)


def test_the_offline_apply_refuses_a_confirmation(tmp_path):
    r, o = offered(tmp_path)
    request(r, o)
    out = apply_pending(coordinator=r.service.coordinator, runtime_store=r.store,
                        queue=Q.OperatorQueue(r.config.operator_db),
                        expected_account=r.config.execution.expected_account,
                        clock=r.clock.now)
    assert out[0]["status"] == "REFUSED" and out[0]["reasons"] == ["CONFIRM_NEEDS_RUNNING_SERVICE"]


# ================================================================== no post text anywhere

def test_no_word_of_the_post_in_any_row_event_alert_or_card(tmp_path):
    r, o = offered(tmp_path, posts=(MENTION,))
    request(r, o)
    r.step(1)
    r.step(1)
    blobs = []
    for db in Path(r.config.paths.state_dir).glob("*.sqlite3"):
        with sqlite3.connect(db) as c:
            for (name,) in c.execute("SELECT name FROM sqlite_master WHERE type='table'"):
                blobs += [json.dumps(row, default=str) for row in c.execute(
                    f"SELECT * FROM {name}")]
    for f in (r.config.alerts_file, r.config.notification_log):
        if f.exists():
            blobs.append(f.read_text())
    blobs += [json.dumps(r.notifier.sent)]
    blobs += [json.dumps(card(x, r.clock.now()), default=str) for x in offers(r)]
    text = "\n".join(blobs).lower()
    for word in ("quorvex", "sold off", "hard"):
        assert word not in text, word
    assert offers(r)[0]["state"] == "EXECUTED"


def test_the_card_states_what_a_confirmation_does_and_its_fields(tmp_path):
    r, _o = offered(tmp_path)
    c = card(offers(r)[0], r.clock.now())
    assert c["what a confirmation does"] == WHAT_IT_DOES
    assert "stated level 50.10" in c["price reference"] and c["stop"] == "48.90"
    assert "QETF" in c["instrument"] and c["uncertainty"] and "s (expires" in c["time left"]
    assert "post 7000" in c["post"] and c["current quote"].startswith("bid 50.09")
    assert IntentState  # imported for the reviewer: the card never shows an order state


# ================================================================== the card on the page

def test_the_page_shows_the_card_and_one_press_confirms(monkeypatch, tmp_path):
    from runtime_harness import required_toml
    from streamlit.testing.v1 import AppTest

    r, o = offered(tmp_path, posts=(MENTION,))
    r.step(1)  # the card's live view is refreshed each cycle
    c = r.config
    cfg = tmp_path / "runtime.toml"
    cfg.write_text(
        f'mode = "mock"\n[paths]\nstate_dir = "{c.paths.state_dir}"\n'
        f'halt_file = "{c.paths.halt_file}"\nmapping_file = "{c.paths.mapping_file}"\n'
        f'[source]\nkind = "replay"\nfile = "{c.source.file}"\n' + required_toml()
        + "[operator_confirmation]\nenabled = true\n")
    monkeypatch.setenv("TRADINGBOT_RUNTIME_CONFIG", str(cfg))
    page = Path(__file__).resolve().parents[1] / "dashboard" / "pages" / "9_Exceptions.py"
    at = AppTest.from_file(str(page), default_timeout=60)
    at.run()
    assert not at.exception
    text = "\n".join(str(t.value) for t in at.text)
    assert WHAT_IT_DOES in text and "stated level 50.10" in text and "stop: 48.90" in text
    assert "market name the bot does not know" in text and "quorvex" not in text.lower()
    at.button(key=f"offer_Confirm_{o['id']}_{o['hash'][:8]}").click().run()
    assert not at.exception
    r.step(1)
    assert offers(r)[0]["state"] == "EXECUTED" and len(entries(r)) == 1


def test_acknowledging_the_held_signal_ends_its_offer(tmp_path):
    from tradingbot.execution.operator_catalog import acknowledge_signal_phrase
    r, _o = offered(tmp_path)
    [e] = [x for x in r.store.exceptions(open_only=True) if x["kind"].startswith("SIGNAL_")]
    Q.OperatorQueue(r.config.operator_db).enqueue(
        at=r.clock.now(), operator="owner", origin="test", action=Q.ACKNOWLEDGE_SIGNAL,
        target=str(e["id"]), kind=e["kind"], note="read the invented post",
        confirmation=acknowledge_signal_phrase(e["id"], e["kind"]))
    r.step(1)
    assert offers(r)[0]["state_reason"] == "SIGNAL_ACKNOWLEDGED"
