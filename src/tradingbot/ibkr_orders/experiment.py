"""The staged first paper experiment (``scripts/ibkr_paper_experiment.py``). CONNECTS to TWS.

Each stage is invoked on its own, writes one JSON evidence file (masked account only) and stops:

=========================== ============================================== ===========
stage                       what it does                                   submissions
=========================== ============================================== ===========
``stage0-refusal``          Read-Only ON: order mode must refuse to start          0
``stage1-lookups``          Read-Only OFF: lookups and positions, no order         0
``stage2-unfillable-bracket`` one bracket that cannot fill, read back, cancelled   2
``stage3-lifecycle``        the one-share lifecycle through the coordinator       4
``abort``                   cancels this client's working orders by order id       0
=========================== ============================================== ===========

2 + 4 = 6 = ``PAPER_TEST_1.max_submissions`` (asserted in a test).

Order: a stage refuses unless the previous stage's evidence exists and passed, and refuses if its
own evidence already passed. A stage whose evidence failed may run again only while the durable
submission count is still what the stage starts from (0 for stage 2, 2 for stage 3): a stage that
submitted can never be repeated. There is no flag to skip or repeat. ``abort`` can always run.

Every stage goes through the one ``PaperOrderSession`` (all pre-socket checks, the identity gate,
the order-mode config read, the guard and the durable counter). Evidence lives beside the journal
in ``<state_dir>/ibkr_paper_orders/<experiment>.evidence/``, which the owner controls.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import UTC, datetime
from decimal import ROUND_FLOOR, Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from tradingbot.event_models import PriceBasis, ReplaySource, Timing, TradeAction
from tradingbot.execution.broker import OrderStatus, QuoteQuality, TimeInForce
from tradingbot.execution.coordinator import ExecutionConfig, ExecutionCoordinator
from tradingbot.execution.store import IntentState
from tradingbot.ibkr.redact import redact_accounts
from tradingbot.ibkr_orders.broker import IbkrPaperBroker
from tradingbot.ibkr_orders.config import PaperOrderConfig, load_order_config
from tradingbot.ibkr_orders.counter import JOURNAL_DIR, CounterError, SubmissionCounter
from tradingbot.ibkr_orders.guard import parse_hours
from tradingbot.ibkr_orders.mapping import FINAL_STATUSES
from tradingbot.ibkr_orders.records import OrderModeSettingRefused, OrderSpec
from tradingbot.ibkr_orders.session import PaperOrderSession
from tradingbot.models import Action, AssetClass, GateDecision, Instrument, MockPolicy, PositionSide

STAGES = ("stage0-refusal", "stage1-lookups", "stage2-unfillable-bracket", "stage3-lifecycle")
ABORT = "abort"
PREREQUISITE = {STAGES[1]: STAGES[0], STAGES[2]: STAGES[1], STAGES[3]: STAGES[2]}
SUBMISSIONS_BEFORE = {STAGES[0]: 0, STAGES[1]: 0, STAGES[2]: 0, STAGES[3]: 2}
SUBMISSIONS_OF = {STAGES[0]: 0, STAGES[1]: 0, STAGES[2]: 2, STAGES[3]: 4}

UNFILLABLE_FRACTION = Decimal("0.90")  # stage 2: the BUY limit 10% below the bid
STOP_DISTANCE = Decimal("1.00")  # the protective stop this far below the entry
STOP_RAISE = Decimal("0.10")  # stage 3: the one amendment
FILL_TIMEOUT_SECONDS = 120.0  # stage 3: wait this long for the entry and for the close
POLL_SECONDS = 1.0
READ_BACK_WAIT_SECONDS = 2.0  # stage 2: let the views settle before reading back
# Stage 3: after the entry fill, the protection check is retried (reads only) this long, since
# the position read can lag the execution.
PROTECTION_SETTLE_SECONDS = 15.0
# Before an ending is reported, the reads are retried this long until the broker's position read
# agrees with the experiment's own fills; stage 3 retries a reconciliation that is not clean as long.
POSITION_SETTLE_SECONDS = 15.0
RECONCILE_SETTLE_SECONDS = 15.0
# Stages 2 and 3 start only if today's broker-reported liquid hours last this many more minutes.
MIN_MINUTES_TO_CLOSE = 60
# Stages 2 and 3 can leave an order or a share behind; neither STARTS at or after this New York
# wall-clock time (hour, minute). The runbook still recommends starting stage 3 by 14:30.
LATEST_START_NEW_YORK = (15, 0)
NEW_YORK = ZoneInfo("America/New_York")


def _wall_clock() -> datetime:
    """The wall clock, aware (UTC). Not a parameter: tests replace this module name."""
    return datetime.now(UTC)


# A protective stop counts as working ONLY with one of these statuses AND visible in a completed
# open-orders read; anything else (Inactive, PendingCancel, unknown, a failed read) is not.
STOP_WORKING_STATUSES = frozenset({OrderStatus.SUBMITTED, OrderStatus.PRE_SUBMITTED})


def _check_start_time(stage: str) -> None:
    now = _wall_clock().astimezone(NEW_YORK)
    h, m = LATEST_START_NEW_YORK
    if (now.hour, now.minute) >= (h, m):
        raise StageRefused(f"{stage} may not start at or after {h:02d}:{m:02d} New York time "
                           f"(now {now:%H:%M %Z}): it could leave an order or a share near the "
                           "close. Run it on another day, before 14:30 as the runbook says.")


def _minutes_to_close(contract, now: datetime) -> int | None:
    """Whole minutes from ``now`` to the end of the broker-reported liquid-hours interval that
    contains it (the same data and parser the guard uses). None when the hours cannot be
    understood or ``now`` is not inside any interval."""
    intervals = parse_hours(getattr(contract, "liquid_hours", None),
                            getattr(contract, "time_zone_id", None))
    if not intervals:
        return None
    for start, end in intervals:
        if start <= now < end:
            return int((end - now).total_seconds() // 60)
    return None


def _check_time_to_close(contract, stage: str, now: datetime) -> None:
    """After connecting, before any placement: today's liquid hours must last at least
    ``MIN_MINUTES_TO_CLOSE`` more minutes (an early-close day included). Unknown: refuse."""
    left = _minutes_to_close(contract, now)
    if left is None:
        raise StageRefused(f"{stage}: today's broker-reported liquid hours cannot be determined, "
                           "or now is outside them: refused before any order")
    if left < MIN_MINUTES_TO_CLOSE:
        raise StageRefused(f"{stage}: the broker-reported liquid hours end in {left} min, less "
                           f"than {MIN_MINUTES_TO_CLOSE}: refused before any order (an early "
                           "close counts)")


def _read_state(s: PaperOrderSession) -> dict:
    state: dict = {"position_read": None, "own_fills": None, "agree": False, "held": None,
                   "stop_working": False, "stop_status": None, "close_working": False,
                   "own_working": None, "read_errors": []}
    con_id = s.contract.con_id
    try:
        read = sum((r.quantity for r in s.positions() if r.con_id == con_id), Decimal(0))
        state["position_read"] = str(read)
        implied = s._implied_position(con_id)
        state["own_fills"] = None if implied is None else str(implied)
        if implied is not None and implied == read:
            state["agree"], state["held"] = True, str(read)
    except Exception as exc:  # noqa: BLE001 - evidence: unknown is reported as unknown
        state["read_errors"].append(redact_accounts(f"positions: {exc}"))
    try:
        own = [o for o in s.open_orders() if o.own]
    except Exception as exc:  # noqa: BLE001
        state["read_errors"].append(redact_accounts(f"open orders: {exc}"))
        return state
    stops = [o for o in own if o.action == "SELL" and o.order_type == "STP"]
    state["own_working"] = len(own)
    state["stop_status"] = ",".join(o.status.value for o in stops) or None
    state["stop_working"] = any(o.status in STOP_WORKING_STATUSES for o in stops)
    state["close_working"] = any(o.action == "SELL" and o.order_type == "LMT" for o in own)
    return state


def _position_state(s: PaperOrderSession) -> dict:
    """What the reads show, retried (reads only) for up to ``POSITION_SETTLE_SECONDS`` until the
    broker's position read and the experiment's own fills agree. ``held`` is set ONLY when they
    agree; otherwise both numbers are reported and nothing is stated as held."""
    deadline = time.monotonic() + POSITION_SETTLE_SECONDS
    while True:
        state = _read_state(s)
        if (state["agree"] and state["own_working"] is not None) or time.monotonic() >= deadline:
            return state
        time.sleep(POLL_SECONDS)


# Every manual sell this script prints is conditional on the TWS window, the source of truth.
NO_SELL_ON_NUMBERS = "do not sell on this script's numbers"
TWS_SELL = ("Look at the TLT position in the TWS window. Only if it shows 1: sell 1 there with a "
            "limit at the bid (lower it a tick at a time until it fills) and wait until it shows "
            "0. If it shows 0: do NOT sell.")
TWS_PROCEDURE = ("In the TWS window: (1) cancel a resting TLT closing limit only if you can see it "
                 "working there, never the stop; (2) " + TWS_SELL + " (3) Only once TWS shows 0, "
                 "run abort to cancel what remains. Never run abort while TWS shows 1 and a "
                 "working stop: abort cancels the stop.")

ABNORMAL_ENDINGS = frozenset({
    "STAGE2_UNEXPECTED_FILL", "STAGE2_PARENT_NOT_CONFIRMED_CANCELLED", "STAGE2_FAILED",
    "STAGE3_ENTRY_NOT_FILLED", "STAGE3_PROTECTION_UNCONFIRMED", "STAGE3_PROTECTION_LOST",
    "STAGE3_CLOSE_REFUSED", "STAGE3_CLOSE_NOT_FILLED", "STAGE3_NOT_CLEAN"})


def _instruction(state: dict) -> str:
    """What the owner does now. No manual sell is ever unconditional (it depends on what the TWS
    window shows), a disputed position is never stated as fact, and abort never comes first
    while TWS may show a working stop."""
    read, fills = state.get("position_read"), state.get("own_fills")
    if state.get("agree"):
        held = Decimal(state["held"])
        head = (f"this script reads a TLT position of {held} (the broker's read and the "
                "experiment's own fills agree). ")
    elif read is None:
        head = f"the broker's position could not be read: {NO_SELL_ON_NUMBERS}. "
        held = None
    else:
        head = (f"the broker's position read {read} and the experiment's own fills "
                f"{fills if fills is not None else 'unknown'} disagree: {NO_SELL_ON_NUMBERS}. ")
        held = None
    if state.get("own_working") is None:
        orders = "This client's working orders could not be read: check them in TWS. "
    else:
        orders = (f"Protective stop working ({state['stop_status']}). " if state["stop_working"]
                  else "No protective stop of this client is working: if TWS shows 1, the share "
                       "is UNPROTECTED. ")
        if state["close_working"]:
            orders += "A closing limit of this client is resting. "
    if held is not None and held <= 0 and state.get("own_working") is not None:
        if state["own_working"]:
            return (head + orders + f"Run abort to cancel this client's {state['own_working']} "
                    "working order(s), then confirm in TWS that the TLT position is 0 and no TLT "
                    "order is working (if not: the Emergency stop section).")
        return (head + orders + "Nothing to do; confirm in TWS that the TLT position is 0 and no "
                "TLT order is working.")
    return head + orders + TWS_PROCEDURE


def _ending_message(result: str, what: str, state: dict) -> str:
    return f"{result}: {what}. Now: {_instruction(state)}"


class StageRefused(RuntimeError):
    """The stage may not run now (order, repetition or count). Nothing was sent."""


def evidence_dir(cfg: PaperOrderConfig) -> Path:
    return cfg.state_dir / JOURNAL_DIR / f"{cfg.experiment.name}.evidence"


def evidence_path(cfg: PaperOrderConfig, stage: str) -> Path:
    return evidence_dir(cfg) / f"{stage}.json"


def _read(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _check_order(cfg: PaperOrderConfig, stage: str) -> None:
    if stage not in STAGES:
        raise StageRefused(f"unknown stage {stage!r}")
    need = PREREQUISITE.get(stage)
    if need is not None:
        prev = _read(evidence_path(cfg, need))
        if (prev is None or prev.get("passed") is not True or prev.get("stage") != need
                or prev.get("result") == "STAGE2_UNEXPECTED_FILL"):
            raise StageRefused(f"{stage} needs {need} to have passed (its evidence is missing or "
                               "failed)")
    own = _read(evidence_path(cfg, stage))
    if own is not None and own.get("passed") is True:
        raise StageRefused(f"{stage} has already passed; it cannot be repeated")


def _journal_used(cfg: PaperOrderConfig) -> int | None:
    """The durable count, read locally (no socket). None if unknown or locked."""
    c = SubmissionCounter(cfg.state_dir, cfg.experiment, cfg.connection.masked_account())
    try:
        c.open()
    except CounterError:
        return None
    try:
        return c.used
    finally:
        c.close()


def _write(cfg: PaperOrderConfig, stage: str, evidence: dict) -> Path:
    d = evidence_dir(cfg)
    d.mkdir(mode=0o700, parents=False, exist_ok=True)
    path = evidence_path(cfg, stage)
    if path.exists():  # an earlier failed attempt is kept, never overwritten
        path.rename(d / f"{stage}.attempt-{datetime.now(UTC):%Y%m%dT%H%M%S%fZ}.json")
    text = redact_accounts(json.dumps(evidence, indent=2, sort_keys=True, default=str))
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(text + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    return path


def _session(cfg: PaperOrderConfig) -> PaperOrderSession:
    return PaperOrderSession(cfg)


def _tick_floor(price: Decimal, tick: Decimal) -> Decimal:
    return (price / tick).to_integral_value(rounding=ROUND_FLOOR) * tick


def _row(s) -> dict:
    return {"order_id": s.order_id, "order_id_present": s.order_id > 0,
            "order_ref": s.order_ref, "order_ref_present": bool(s.order_ref),
            "experiment_prefix": s.experiment, "perm_id_present": s.perm_id > 0, "own": s.own,
            "status": s.status.value, "raw_status": s.raw_status, "action": s.action,
            "order_type": s.order_type, "filled": str(s.filled), "remaining": str(s.remaining)}


def _exec_row(e) -> dict:
    return {"order_id": e.order_id, "order_id_present": e.order_id > 0,
            "order_ref": e.order_ref, "order_ref_present": bool(e.order_ref),
            "experiment_prefix": e.experiment, "perm_id_present": e.perm_id > 0,
            "side": e.side, "shares": str(e.shares), "time": e.time}


def _lookup(name: str, fn, rowfn) -> dict:
    t0 = time.monotonic()
    try:
        rows = fn()
    except Exception as exc:  # noqa: BLE001 - evidence: the read failed
        return {"lookup": name, "completed": False,
                "error": redact_accounts(f"{type(exc).__name__}: {exc}")}
    return {"lookup": name, "completed": True, "seconds": round(time.monotonic() - t0, 3),
            "rows": [rowfn(r) for r in rows]}


def _lookups(s: PaperOrderSession) -> dict:
    return {
        "open_orders": _lookup("open_orders", s.open_orders, _row),
        "executions": _lookup("executions", s.executions, _exec_row),
        "completed_orders": _lookup("completed_orders", s.completed_orders, _row),
        "positions": _lookup("positions", s.positions, lambda r: {
            "con_id": r.con_id, "symbol": r.symbol, "quantity": str(r.quantity)}),
    }


# --- the stages -----------------------------------------------------------------------------------
def _stage0(cfg: PaperOrderConfig, ev: dict) -> None:
    s = _session(cfg)
    try:
        s.connect()
    except OrderModeSettingRefused as exc:
        ev["refused"] = True
        ev["refusal"] = redact_accounts(str(exc))
        ev["read_only_settings"] = s.read_only_settings
        ev["order_frames_sent"] = s.order_frames_sent
        ev["passed"] = (s.read_only_settings or {}).get("readOnlyApi") is True \
            and s.order_frames_sent == 0
        if not ev["passed"]:
            ev["why_failed"] = "refused, but not because Read-Only API is ON"
        return
    try:
        ev["refused"] = False
        ev["read_only_settings"] = s.read_only_settings
        ev["passed"] = False
        ev["why_failed"] = "order mode STARTED: Read-Only API is OFF. Turn it ON for stage 0."
    finally:
        ev["order_frames_sent"] = s.order_frames_sent
        s.disconnect()


def _stage1(cfg: PaperOrderConfig, ev: dict) -> None:
    s = _session(cfg)
    s.connect()
    try:
        c = s.contract
        ev["contract"] = {"con_id": c.con_id, "symbol": c.symbol, "min_tick": str(c.min_tick),
                          "time_zone_id": c.time_zone_id, "liquid_hours": c.liquid_hours}
        ev["read_only_settings"] = s.read_only_settings
        ev["lookups"] = _lookups(s)
        b = IbkrPaperBroker(s)
        try:
            q = b.quote(b.resolve(Instrument(asset_class=AssetClass.ETF, symbol=c.symbol)))
            ev["quote"] = {"quality": q.quality.value, "bid": str(q.bid), "ask": str(q.ask)}
        except Exception as exc:  # noqa: BLE001
            ev["quote"] = {"quality": None, "error": redact_accounts(str(exc))}
        ev["account_rows_checked"] = not s.poisoned  # a row naming another account poisons
        ev["order_frames_sent"] = s.order_frames_sent
        ev["passed"] = (all(v["completed"] for v in ev["lookups"].values())
                        and not s.poisoned and s.order_frames_sent == 0)
    finally:
        s.disconnect()


def _require_count(s: PaperOrderSession, stage: str) -> None:
    want = SUBMISSIONS_BEFORE[stage]
    if s.submissions_used != want:
        raise StageRefused(f"{stage} starts from {want} submissions; the journal shows "
                           f"{s.submissions_used}. It cannot run (again).")


def _stage2(cfg: PaperOrderConfig, ev: dict) -> None:
    _check_start_time(STAGES[2])  # an unexpected fill must be closable by hand before 16:00
    s = _session(cfg)
    s.connect()
    try:
        _require_count(s, STAGES[2])
        _check_time_to_close(s.contract, STAGES[2], _wall_clock())
        b = IbkrPaperBroker(s)
        c = s.contract
        q = b.quote(b.resolve(Instrument(asset_class=AssetClass.ETF, symbol=c.symbol)))
        tick = Decimal(str(c.min_tick))
        if q.bid is None or q.bid <= 0:
            ev.update(passed=False, why_failed="no bid to price an unfillable bracket from")
            return
        limit = _tick_floor(q.bid * UNFILLABLE_FRACTION, tick)
        stop = _tick_floor(limit - STOP_DISTANCE, tick)
        ev["quote"] = {"quality": q.quality.value, "bid": str(q.bid), "ask": str(q.ask)}
        ev["bracket"] = {"buy_limit": str(limit), "sell_stop": str(stop)}
        ref = f"TB-{cfg.experiment.name}-stage2"
        snaps = s.place([OrderSpec(ref, "BUY", 1, "LMT", limit_price=limit),
                         OrderSpec(f"{ref}.STP", "SELL", 1, "STP", stop_price=stop)])
        parent, child = snaps[0].order_id, snaps[1].order_id
        ev["placed"] = [_row(x) for x in snaps]
        time.sleep(READ_BACK_WAIT_SECONDS)
        ev["read_back"] = _lookups(s)
        # The parent first, confirmed. Only a parent confirmed cancelled with nothing filled lets
        # the child be cancelled: after any fill the stop is the share's protection and stays.
        ev["cancel"], ev["cancel_errors"] = [], []
        pt = s._tracks[parent]
        if pt.status not in FINAL_STATUSES:
            try:
                ev["cancel"].append(_row(s.cancel(parent)))
            except Exception as exc:  # noqa: BLE001 - evidence; the child is then left alone
                ev["cancel_errors"].append({"order_id": parent,
                                            "error": redact_accounts(str(exc))})
        parent_execs = [e for e in s.executions() if e.order_id == parent
                        and e.client_id == pt.client_id]
        parent_filled = pt.filled > 0 or bool(parent_execs)
        parent_clean = pt.status in (OrderStatus.CANCELLED, OrderStatus.API_CANCELLED) \
            and not parent_filled
        ev["parent_confirmed_cancelled_unfilled"] = parent_clean
        if parent_clean:
            ct = s._tracks[child]
            if ct.status not in FINAL_STATUSES:
                ev["cancel"].append(_row(s.cancel(child)))
        time.sleep(READ_BACK_WAIT_SECONDS)
        ev["after_cancel"] = _lookups(s)
        events = [e for e in s.order_events if e["order_id"] in (parent, child)]
        ev["events"] = events
        child_sent = next((e["mono"] for e in events if e["event"] == "SENT_PLACE"
                           and e["order_id"] == child), None)
        parent_first = next((e["mono"] for e in events if e["order_id"] == parent
                             and e["event"] in ("openOrder", "orderStatus")), None)
        ev["parent_answered_before_child_sent"] = (
            parent_first is not None and child_sent is not None and parent_first < child_sent)
        after = ev["after_cancel"]
        still_open = [r for r in after["open_orders"].get("rows", [])
                      if r["own"] and r["order_id"] in (parent, child)]
        done = {r["order_id"]: r["status"] for r in after["completed_orders"].get("rows", [])
                if r["own"]}
        filled = any(r["order_id"] in (parent, child)
                     for r in after["executions"].get("rows", []))
        ev["filled"] = filled
        ev["gone_from_open_orders"] = not still_open
        ev["in_completed_orders"] = {str(k): done.get(k) for k in (parent, child)}
        ev["submissions_used"] = s.submissions_used
        ev["passed"] = (not filled and not still_open and s.submissions_used == 2
                        and not ev["parent_answered_before_child_sent"]
                        and all(done.get(k) in ("Cancelled", "ApiCancelled")
                                for k in (parent, child))
                        and all(v["completed"] for v in ev["read_back"].values()))
        ev["result"] = "STAGE2_PASSED" if ev["passed"] else "STAGE2_FAILED"
        if parent_filled:
            ct = s._tracks[child]
            ev["result"] = "STAGE2_UNEXPECTED_FILL"
            ev["passed"] = False
            # The child's ACTUAL state: this stage never cancels it after a fill, but the broker
            # may have cancelled it with its parent (a partial fill, or a fill racing the cancel).
            child_status = ct.status.value if ct.status is not None else "UNKNOWN"
            in_open = any(r["own"] and r["order_id"] == child
                          for r in after["open_orders"].get("rows", []))
            stop_working = (ct.status in STOP_WORKING_STATUSES and in_open
                            and after["open_orders"].get("completed") is True)
            ev["held"] = {"symbol": c.symbol, "quantity": str(pt.filled or sum(
                (e.shares for e in parent_execs), Decimal(0))), "entry_order_id": parent,
                "stop_order_id": child, "stop_price": str(ct.stop_price),
                "stop_status": child_status, "stop_in_open_orders": in_open,
                "stop_working": stop_working}
            state = _position_state(s)
            state["stop_working"] = state["stop_working"] and stop_working  # the stricter
            ev["state"] = state
            stop_text = (f"working ({child_status}, in open orders)" if stop_working else
                         f"NOT working (status {child_status}, "
                         f"{'in' if in_open else 'not in'} open orders)")
            ev["why_failed"] = _ending_message(
                "STAGE2_UNEXPECTED_FILL", f"the entry (order {parent}) filled; its protective "
                f"stop (order {child}) is {stop_text}; stage 3 is blocked (runbook: 'If stage 2 "
                "fills')", state)
        elif not parent_clean:
            ev["result"] = "STAGE2_PARENT_NOT_CONFIRMED_CANCELLED"
            ev["passed"] = False
            ev["state"] = state = _position_state(s)
            ev["why_failed"] = _ending_message(
                "STAGE2_PARENT_NOT_CONFIRMED_CANCELLED", "the parent was not confirmed "
                "cancelled; its stop child was left alone", state)
        elif not ev["passed"]:
            ev["state"] = state = _position_state(s)
            ev["why_failed"] = _ending_message(
                "STAGE2_FAILED", "the read-back, the cancels or the completed orders did not "
                "show what stage 2 needs", state)
    finally:
        s.disconnect()


def _wait(pred, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        if pred():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(POLL_SECONDS)


def _stage3(cfg: PaperOrderConfig, ev: dict) -> None:
    _check_start_time(STAGES[3])
    quality = (_read(evidence_path(cfg, STAGES[1])) or {}).get("quote", {}).get("quality")
    if quality not in (QuoteQuality.LIVE.value, QuoteQuality.DELAYED.value):
        raise StageRefused(f"stage 1 recorded quote quality {quality!r}; stage 3 needs LIVE or "
                           "DELAYED")
    s = _session(cfg)
    s.connect()
    coord = None
    try:
        _require_count(s, STAGES[3])
        _check_time_to_close(s.contract, STAGES[3], _wall_clock())
        policy = MockPolicy(quantity=1, quantity_per_add=1, max_notional_usd=Decimal(150),
                            max_price_deviation_pct=Decimal(1))
        # the exit dry check prices the close exactly as the core will (bid less this collar)
        broker = IbkrPaperBroker(s, exit_collar_pct=policy.max_price_deviation_pct)
        config = ExecutionConfig(halt_file=evidence_dir(cfg) / "HALT",
                                 expected_account=s.account_id,
                                 accepted_quote_qualities=frozenset({QuoteQuality(quality)}),
                                 protective_tif=TimeInForce.DAY, confirm_attempts=10,
                                 confirm_wait_seconds=1.0)
        coord = ExecutionCoordinator(broker, evidence_dir(cfg) / "stage3-execution.sqlite3",
                                     policy, config)
        c = s.contract
        tick = Decimal(str(c.min_tick))
        q = broker.quote(broker.resolve(Instrument(asset_class=AssetClass.ETF, symbol=c.symbol)))
        entry = q.ask
        stop = _tick_floor(entry - STOP_DISTANCE, tick)
        now = datetime.now(UTC)
        src = ReplaySource(source_id=f"{cfg.experiment.name}-stage3", revision=1,
                           published_at=now, received_at=now, verified_author=True,
                           complete_text=True, post_hash=hashlib.sha256(
                               f"owner-authorized {cfg.experiment.name} stage3".encode()
                           ).hexdigest())
        gate = GateDecision(status="APPROVE",
                            reasons=[f"OWNER_AUTHORIZED_{cfg.experiment.name}_STAGE3"])

        def act(action_id, kind, **kw):
            return TradeAction(
                action_id=action_id, action=kind, timing=Timing.CURRENT,
                instrument=Instrument(asset_class=AssetClass.ETF, symbol=c.symbol),
                position_side=PositionSide.LONG, price_basis=PriceBasis.TRADED_CONTRACT,
                source_market_symbol=c.symbol, explicit_instruction=True,
                evidence=[f"owner-authorized {cfg.experiment.name} stage 3"], **kw)

        steps = ev["steps"] = []

        def step(name, outcome):
            steps.append({"step": name, "state": outcome.state.value,
                          "protection": outcome.protection, "reasons": list(outcome.reasons),
                          "submissions_used": s.submissions_used})
            return outcome

        opened = step("open", coord.execute(src, act("open", Action.OPEN,
                                                     entry_reference=str(entry),
                                                     stop_price=str(stop)), gate))
        def fail(result: str, what: str) -> None:
            ev["state"] = state = _position_state(s)  # retried until read and fills agree
            ev.update(passed=False, result=result,
                      why_failed=_ending_message(result, what, state))

        if not _wait(lambda: coord.sync() and bool(coord.ledger())
                     and coord.ledger()[0].quantity == 1, FILL_TIMEOUT_SECONDS):
            fail("STAGE3_ENTRY_NOT_FILLED", "the entry did not fill in time")
            return
        # The position read can lag the fill: the core then sees the stop sized above what the
        # broker shows. Retry the check (the same sync reads as above, no new order) briefly.
        _wait(lambda: coord.sync() and coord.ledger()[0].protection == "CONFIRMED",
              PROTECTION_SETTLE_SECONDS)
        ledger = coord.ledger()[0]
        ev["protection_after_fill"] = ledger.protection
        if ledger.protection != "CONFIRMED":
            fail("STAGE3_PROTECTION_UNCONFIRMED", "the entry filled but the core could not "
                 f"confirm its protective stop within {PROTECTION_SETTLE_SECONDS:.0f} s "
                 f"(protection {ledger.protection})")
            return
        step("raise-stop", coord.execute(src, act("raise-stop", Action.MODIFY,
                                                  idea_reference=ledger.idea_id,
                                                  stop_price=str(stop + STOP_RAISE)), gate))
        closed = step("close", coord.execute(src, act("close", Action.CLOSE,
                                                      idea_reference=ledger.idea_id), gate))
        if broker.protection_lost:  # the close was refused after its stop was cancelled
            coord.sync()  # the core raises UNPROTECTED_POSITION now, not at a later read
            ev["protection_lost"] = broker.protection_lost
            fail("STAGE3_PROTECTION_LOST", "POSITION OPEN, PROTECTION ABSENT: the close was not "
                 "sent after the stop was cancelled")
            return
        if closed.state in (IntentState.REFUSED, IntentState.REJECTED, IntentState.HALTED):
            fail("STAGE3_CLOSE_REFUSED", f"the close was {closed.state.value} and not working "
                 f"({', '.join(closed.reasons) or 'no reason given'})")
            return
        if not _wait(lambda: coord.sync() and coord.ledger()[0].quantity == 0,
                     FILL_TIMEOUT_SECONDS):
            fail("STAGE3_CLOSE_NOT_FILLED", "the closing limit was sent but did not fill in time")
            return
        # The position read can lag the closing fill: retry the reconciliation (reads only, no
        # order frame) before calling it not clean.
        reports = []
        _wait(lambda: coord.sync() and reports.append(coord.reconcile()) is None
              and bool(reports[-1].clean), RECONCILE_SETTLE_SECONDS)
        report = reports[-1] if reports else coord.reconcile()
        ev["reconcile_clean"] = bool(report.clean)
        ev["entry_ref"] = opened.order_ref
        ev["lookups_after"] = _lookups(s)
        ev["submissions_used"] = s.submissions_used
        ev["passed"] = bool(report.clean) and s.submissions_used == 6
        ev["result"] = "STAGE3_PASSED" if ev["passed"] else "STAGE3_NOT_CLEAN"
        if not ev["passed"]:
            fail("STAGE3_NOT_CLEAN", f"reconciliation clean={bool(report.clean)}, submissions "
                 f"used {s.submissions_used}")
    finally:
        if coord is not None:
            coord.close()
        s.disconnect()


def _abort(cfg: PaperOrderConfig, ev: dict) -> None:
    """Cancel every working order of THIS client by its own order id (children first), then
    report what remains. Never a global cancel. Works even when the journal is unknown."""
    s = _session(cfg)
    s.connect()
    try:
        mine = [o for o in s.open_orders() if o.own]
        ev["found"] = [_row(o) for o in mine]
        ev["cancelled"], ev["cancel_errors"] = [], []
        for o in sorted(mine, key=lambda o: (o.parent_id == 0, o.order_id)):
            t = s._tracks.get(o.order_id)
            if t is not None and t.status in FINAL_STATUSES:
                continue  # cancelled with its parent
            try:
                ev["cancelled"].append(_row(s.cancel(o.order_id)))
            except Exception as exc:  # noqa: BLE001 - reported, the next one is still tried
                ev["cancel_errors"].append({"order_id": o.order_id,
                                            "error": redact_accounts(str(exc))})
        time.sleep(READ_BACK_WAIT_SECONDS)
        after = _lookups(s)
        ev["after"] = after
        remaining = [r for r in after["open_orders"].get("rows", []) if r["own"]]
        ev["remaining_own_working"] = remaining
        ev["position"] = after["positions"].get("rows")
        ev["passed"] = after["open_orders"]["completed"] and not remaining
    finally:
        s.disconnect()


_RUN = {STAGES[0]: _stage0, STAGES[1]: _stage1, STAGES[2]: _stage2, STAGES[3]: _stage3,
        ABORT: _abort}


def run_stage(stage: str, config_path: str | os.PathLike | None = None) -> tuple[dict, Path]:
    """Run ONE stage (or ``abort``), write its evidence, return (evidence, path). Raises
    ``StageRefused`` without connecting when the order of stages does not allow it."""
    cfg = load_order_config(config_path)
    if stage != ABORT:
        _check_order(cfg, stage)
    ev: dict = {"stage": stage, "experiment": cfg.experiment.name,
                "account_masked": cfg.connection.masked_account(),
                "started_at": datetime.now(UTC).isoformat(), "passed": False,
                "submissions_before": _journal_used(cfg),
                "submissions_of_stage": SUBMISSIONS_OF.get(stage, 0)}
    try:
        _RUN[stage](cfg, ev)
    except StageRefused as exc:
        ev.update(passed=False, why_failed=str(exc))
    except Exception as exc:  # noqa: BLE001 - evidence of the failure, then re-raised below
        ev.update(passed=False, error=redact_accounts(f"{type(exc).__name__}: {exc}"))
    ev["submissions_after"] = _journal_used(cfg)
    ev["finished_at"] = datetime.now(UTC).isoformat()
    return ev, _write(cfg, stage, ev)
