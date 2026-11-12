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

from tradingbot.event_models import PriceBasis, ReplaySource, Timing, TradeAction
from tradingbot.execution.broker import OrderStatus, QuoteQuality, TimeInForce
from tradingbot.execution.coordinator import ExecutionConfig, ExecutionCoordinator
from tradingbot.ibkr.redact import redact_accounts
from tradingbot.ibkr_orders.broker import IbkrPaperBroker
from tradingbot.ibkr_orders.config import PaperOrderConfig, load_order_config
from tradingbot.ibkr_orders.counter import JOURNAL_DIR, CounterError, SubmissionCounter
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
    s = _session(cfg)
    s.connect()
    try:
        _require_count(s, STAGES[2])
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
            ev["held"] = {"symbol": c.symbol, "quantity": str(pt.filled or sum(
                (e.shares for e in parent_execs), Decimal(0))), "entry_order_id": parent,
                "stop_order_id": child, "stop_price": str(ct.stop_price),
                "stop_working": ct.status not in FINAL_STATUSES}
            ev["why_failed"] = (
                f"STAGE2_UNEXPECTED_FILL: the account HOLDS {ev['held']['quantity']} "
                f"{c.symbol} share(s); the protective SELL STP {ev['held']['stop_price']} "
                f"(order {child}) was left working. Do NOT run abort first (it cancels the "
                "stop). Close by hand in TWS: SELL the share with a limit at the bid, confirm "
                "the position is 0, then run abort to cancel the stop (runbook: 'If stage 2 "
                "fills'). Stage 3 is blocked.")
        elif not parent_clean:
            ev["result"] = "STAGE2_PARENT_NOT_CONFIRMED_CANCELLED"
            ev["passed"] = False
            ev["why_failed"] = ("the parent was not confirmed cancelled; its stop child was left "
                                "alone. Check TWS, then run abort.")
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
    quality = (_read(evidence_path(cfg, STAGES[1])) or {}).get("quote", {}).get("quality")
    if quality not in (QuoteQuality.LIVE.value, QuoteQuality.DELAYED.value):
        raise StageRefused(f"stage 1 recorded quote quality {quality!r}; stage 3 needs LIVE or "
                           "DELAYED")
    s = _session(cfg)
    s.connect()
    coord = None
    try:
        _require_count(s, STAGES[3])
        broker = IbkrPaperBroker(s)
        policy = MockPolicy(quantity=1, quantity_per_add=1, max_notional_usd=Decimal(150),
                            max_price_deviation_pct=Decimal(1))
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
        if not _wait(lambda: coord.sync() and bool(coord.ledger())
                     and coord.ledger()[0].quantity == 1, FILL_TIMEOUT_SECONDS):
            ev.update(passed=False, why_failed="the entry did not fill in time: run abort")
            return
        ledger = coord.ledger()[0]
        ev["protection_after_fill"] = ledger.protection
        if ledger.protection != "CONFIRMED":
            ev.update(passed=False, why_failed="the stop was not confirmed working: run abort "
                      "and close by hand")
            return
        step("raise-stop", coord.execute(src, act("raise-stop", Action.MODIFY,
                                                  idea_reference=ledger.idea_id,
                                                  stop_price=str(stop + STOP_RAISE)), gate))
        step("close", coord.execute(src, act("close", Action.CLOSE,
                                             idea_reference=ledger.idea_id), gate))
        if broker.protection_lost:  # the close was refused after its stop was cancelled
            coord.sync()  # the core raises UNPROTECTED_POSITION now, not at a later read
            ev.update(passed=False, protection_lost=broker.protection_lost,
                      why_failed="POSITION OPEN, PROTECTION ABSENT: the close was not sent after "
                      "the stop was cancelled. Close the share by hand in TWS now (runbook: "
                      "Emergency stop).")
            return
        if not _wait(lambda: coord.sync() and coord.ledger()[0].quantity == 0,
                     FILL_TIMEOUT_SECONDS):
            ev.update(passed=False, why_failed="the closing limit did not fill in time: run "
                      "abort, then close the share by hand in TWS")
            return
        report = coord.reconcile()
        ev["reconcile_clean"] = bool(report.clean)
        ev["entry_ref"] = opened.order_ref
        ev["lookups_after"] = _lookups(s)
        ev["submissions_used"] = s.submissions_used
        ev["passed"] = bool(report.clean) and s.submissions_used == 6
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
