"""The staged paper experiments (``scripts/ibkr_paper_experiment.py``). CONNECTS to TWS.

Two experiment definitions share the stage functions (``ExperimentPlan``, ``PLANS``):
``paper-test-1`` (below; over, it can never place again) and ``paper-test-2`` (YOUR CALL 17,
NOT authorized: stage 2 one unfillable order, 1; stage 3 the unfillable bracket, 2; stage 4 the
lifecycle with the protective stop verified after the entry fills, at most 7; 10 in total;
acknowledgement timeout: HALT). See docs/IBKR_PAPER_ORDERS.md.

paper-test-1:

Each stage is invoked on its own, writes one JSON evidence file (masked account only) and stops:

=========================== ============================================== ===========
stage                       what it does                                   submissions
=========================== ============================================== ===========
``stage0-refusal``          Read-Only ON: order mode must refuse to start          0
``stage1-lookups``          Read-Only OFF: lookups and positions, no order         0
``stage2-unfillable-bracket`` one bracket that cannot fill, read back, cancelled   2
``stage3-lifecycle``        the one-share lifecycle through the coordinator  up to 7
``abort``                   cancels this client's working orders by order id       0
=========================== ============================================== ===========

Stage 3 at worst: entry 1 + stop 1 + one stop amendment 1 + two close attempts x (closing limit 1
+ the protective stop placed again after the unfilled limit was cancelled 1) = 7; a pass uses 4
(the first close fills) or 6 (the second does). 2 + 7 = 9 = ``PAPER_TEST_1.max_submissions``
(asserted in a test).

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
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import ROUND_FLOOR, Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from tradingbot.event_models import PriceBasis, ReplaySource, Timing, TradeAction
from tradingbot.execution.broker import OrderStatus, QuoteQuality, TimeInForce
from tradingbot.execution.coordinator import (
    EXIT_UNFILLED_CANCELLED,
    ExecutionConfig,
    ExecutionCoordinator,
)
from tradingbot.execution.store import UNRESOLVED_STATES, IntentState
from tradingbot.ibkr.redact import redact_accounts
from tradingbot.ibkr_orders.broker import IbkrPaperBroker
from tradingbot.ibkr_orders.config import PaperOrderConfig, load_order_config
from tradingbot.ibkr_orders.counter import JOURNAL_DIR, CounterError, SubmissionCounter
from tradingbot.ibkr_orders.guard import parse_hours
from tradingbot.ibkr_orders.mapping import FINAL_STATUSES
from tradingbot.ibkr_orders.records import (
    CancelFailed,
    OrderModeSettingRefused,
    OrderNotWorking,
    OrderOutcomeUnknown,
    OrderSpec,
    PartialBracket,
)
from tradingbot.ibkr_orders.session import PaperOrderSession
from tradingbot.models import Action, AssetClass, GateDecision, Instrument, MockPolicy, PositionSide

STAGES = ("stage0-refusal", "stage1-lookups", "stage2-unfillable-bracket", "stage3-lifecycle")
ABORT = "abort"
PREREQUISITE = {STAGES[1]: STAGES[0], STAGES[2]: STAGES[1], STAGES[3]: STAGES[2]}
SUBMISSIONS_BEFORE = {STAGES[0]: 0, STAGES[1]: 0, STAGES[2]: 0, STAGES[3]: 2}
SUBMISSIONS_OF = {STAGES[0]: 0, STAGES[1]: 0, STAGES[2]: 2, STAGES[3]: 7}

# --- what an experiment runs (its caps are in caps.py) --------------------------------------------
# After an acknowledgement timeout (the session has already made its read-only reconciliation):
ACK_HALT = "HALT"  # record the reconciliation, raise; nothing cancelled (owner's DECIDED 15 rule)
# in an UNFILLABLE stage only, when the read shows every order WORKING: the confirmed cancels of
# that stage, never a pass, never a resend (the behaviour merged for paper-test-1)
ACK_CANCEL_IF_WORKING = "CANCEL_IF_WORKING"
# stage kinds: what each stage function does
REFUSAL, LOOKUPS, SINGLE, BRACKET, LIFECYCLE = ("refusal", "lookups", "single", "bracket",
                                                "lifecycle")


@dataclass(frozen=True)
class ExperimentPlan:
    """The stages of one experiment, in order, with the submissions each may use. The stage
    functions are shared; ``kinds`` says which one each stage runs. Result codes are written by
    the functions with paper-test-1's stage numbers and renumbered for the stage that ran
    (``codes``), so paper-test-1's evidence is exactly as before."""

    experiment: str
    stages: tuple[str, ...]
    kinds: tuple[str, ...]
    submissions: tuple[int, ...]
    ack_timeout: str
    callback_ack_required: bool = False  # bracket: both orders' openOrder+orderStatus by callback
    stop_proof_after_fill: bool = False  # lifecycle: the protective stop after the entry fills
    codes: dict = field(default_factory=dict)  # kind -> (code prefix written, prefix reported)

    def stage(self, kind: str) -> str:
        return self.stages[self.kinds.index(kind)]

    def number(self, kind: str) -> int:
        return self.kinds.index(kind)

    def kind(self, stage: str) -> str:
        return self.kinds[self.stages.index(stage)]

    def before(self, stage: str) -> int:
        return sum(self.submissions[:self.stages.index(stage)])

    def of(self, stage: str) -> int:
        return self.submissions[self.stages.index(stage)]

    def prerequisite(self, stage: str) -> str | None:
        i = self.stages.index(stage)
        return self.stages[i - 1] if i else None


PAPER_TEST_1_PLAN = ExperimentPlan(
    experiment="paper-test-1", stages=STAGES, kinds=(REFUSAL, LOOKUPS, BRACKET, LIFECYCLE),
    submissions=tuple(SUBMISSIONS_OF[x] for x in STAGES), ack_timeout=ACK_CANCEL_IF_WORKING)
# paper-test-2 (YOUR CALL 17): a single unfillable order first, then the bracket, then the
# lifecycle with the protective stop verified after the entry fills. 0 + 0 + 1 + 2 + 7 = 10.
PAPER_TEST_2_STAGES = ("stage0-refusal", "stage1-lookups", "stage2-single-unfillable",
                       "stage3-unfillable-bracket", "stage4-lifecycle")
PAPER_TEST_2_PLAN = ExperimentPlan(
    experiment="paper-test-2", stages=PAPER_TEST_2_STAGES,
    kinds=(REFUSAL, LOOKUPS, SINGLE, BRACKET, LIFECYCLE), submissions=(0, 0, 1, 2, 7),
    ack_timeout=ACK_HALT,  # the owner's rule; ACK_CANCEL_IF_WORKING is the one-line alternative
    callback_ack_required=True, stop_proof_after_fill=True,
    codes={BRACKET: ("STAGE2_", "STAGE3_"), LIFECYCLE: ("STAGE3_", "STAGE4_")})
PLANS = {p.experiment: p for p in (PAPER_TEST_1_PLAN, PAPER_TEST_2_PLAN)}


def _plan(cfg: PaperOrderConfig) -> ExperimentPlan:
    plan = PLANS.get(cfg.experiment.name)
    if plan is None:
        raise StageRefused(f"no stage plan for experiment {cfg.experiment.name!r}")
    return plan

UNFILLABLE_FRACTION = Decimal("0.90")  # stage 2: the BUY limit 10% below the bid
STOP_DISTANCE = Decimal("1.00")  # the protective stop this far below the entry
STOP_RAISE = Decimal("0.10")  # stage 3: the one amendment
FILL_TIMEOUT_SECONDS = 120.0  # stage 3: wait this long for the entry to fill
POLL_SECONDS = 1.0
READ_BACK_WAIT_SECONDS = 2.0  # stage 2: let the views settle before reading back
# Stage 2: the read after the cancels is repeated (reads only) up to this many times, each after
# READ_BACK_WAIT_SECONDS, until it confirms both orders gone and Cancelled.
STAGE2_CONFIRM_READS = 3
CANCELLED_TEXT = ("Cancelled", "ApiCancelled")
# Stage 1 takes up to this many bounded quote samples in a row and passes on the first one the
# adapter labels LIVE or DELAYED with a bid and an ask (each sample is recorded).
STAGE1_QUOTE_SAMPLES = 3
USABLE_QUALITIES = (QuoteQuality.LIVE.value, QuoteQuality.DELAYED.value)
# Stage 3: after the entry fill, the protection check is retried (reads only) this long, since
# the position read can lag the execution.
PROTECTION_SETTLE_SECONDS = 15.0
# Before an ending is reported, the reads are retried this long until the broker's position read
# agrees with the experiment's own fills; stage 3 retries a reconciliation that is not clean as long.
POSITION_SETTLE_SECONDS = 15.0
# paper-test-2 stage 4: after the protective stop is verified (within PROTECTION_SETTLE_SECONDS of
# the entry's fill), hold this long (reads nothing, sends nothing), then read it all again.
STOP_HOLD_SECONDS = 30.0
RECONCILE_SETTLE_SECONDS = 15.0
# Stages 2 and 3 start only if today's broker-reported liquid hours last this many more minutes.
MIN_MINUTES_TO_CLOSE = 60
# Stages 2 and 3 can leave an order or a share behind; neither STARTS at or after this New York
# wall-clock time (hour, minute). The runbook still recommends starting stage 3 by 14:30.
LATEST_START_NEW_YORK = (15, 0)
NEW_YORK = ZoneInfo("America/New_York")
# Stage 3 quotes (owner's decision, 13 Nov 2026): LIVE, a bid and an ask, each side at most this many
# seconds old (the core's policy.max_quote_age_seconds, measured on the older side), for the entry
# price and for every close attempt. A read that is not usable is retried, reads only, for at most
# QUOTE_RETRY_SECONDS before the entry and before each close attempt; then nothing is sent.
STAGE3_QUOTE_MAX_AGE_SECONDS = 3
QUOTE_RETRY_SECONDS = 20.0
# The close: at most this many attempts. Each is a SELL 1 LMT at the fresh bid (the core's
# exit_price_policy TOUCH) working at most CLOSE_WAIT_SECONDS (the core's exit_validity_seconds);
# unfilled, the core cancels it with broker confirmation and places the protective stop again. The
# script waits, reads only, at most CLOSE_SETTLE_SECONDS more for that, then PROTECTION_SETTLE_SECONDS
# for the restored stop to be CONFIRMED, before the next attempt may start.
STAGE3_CLOSE_ATTEMPTS = 2
CLOSE_WAIT_SECONDS = 10.0
CLOSE_SETTLE_SECONDS = 25.0
# The core's cancel / resize confirmation: this many broker reads, this far apart.
CONFIRM_ATTEMPTS = 10
CONFIRM_WAIT_SECONDS = 1.0


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


def _check_time_to_close(contract, stage: str, now: datetime, extra_seconds: float = 0) -> None:
    """After connecting, before any placement: today's liquid hours must last at least
    ``MIN_MINUTES_TO_CLOSE`` more minutes, plus ``extra_seconds`` rounded up to whole minutes (a
    stage that waits longer, e.g. paper-test-2's stop hold); an early-close day included.
    Unknown: refuse."""
    need = MIN_MINUTES_TO_CLOSE + -(-int(extra_seconds) // 60)
    left = _minutes_to_close(contract, now)
    if left is None:
        raise StageRefused(f"{stage}: today's broker-reported liquid hours cannot be determined, "
                           "or now is outside them: refused before any order")
    if left < need:
        raise StageRefused(f"{stage}: the broker-reported liquid hours end in {left} min, less "
                           f"than {need}: refused before any order (an early "
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
# A closing limit whose cancel TWS has not completed can still fill beside a sale by hand (a
# one-share short), whoever asked for that cancel (abort, the execution core or the owner).
NO_SALE_WHILE_RUNNING = "Never sell by hand in TWS while this script is still running."
WAIT_FOR_CANCELLED = ("While any TLT closing limit of this experiment shows working or PendingCancel "
                      "in the TWS window (whoever cancelled it), do not sell by hand and do nothing "
                      "more; wait until it shows Cancelled or Filled.")
TWS_PROCEDURE = (NO_SALE_WHILE_RUNNING + " In the TWS window: (1) open the Trades panel: if it already shows a TLT sale of "
                 "this experiment today, skip step (3); (2) cancel a resting TLT closing limit "
                 "only if you can see it working there, never the stop. " + WAIT_FOR_CANCELLED +
                 " (3) " + TWS_SELL + " (4) "
                 "After a manual sale, run abort at once: the protective stop is still working "
                 "until then and must not trigger on a share you no longer hold. Abort cancels "
                 "the stop only when this script itself reads the account flat; if it reports the "
                 "stop left working while TWS shows 0, cancel that TLT stop in the TWS window "
                 "yourself at once. Never cancel the stop while TWS shows 1.")
ABORT_STOP_NOTE = ("Cancel that TLT stop in the TWS window yourself at once if the TWS window shows "
                   "TLT 0 (for example after your manual sale): it is still working. If TWS shows "
                   "1, leave the stop working.")

ABNORMAL_ENDINGS = frozenset({
    "STAGE2_UNEXPECTED_FILL", "STAGE2_ACK_BY_RECONCILIATION",
    "STAGE2_PARENT_NOT_CONFIRMED_CANCELLED", "STAGE2_CHILD_NOT_CONFIRMED_CANCELLED",
    "STAGE2_CANCEL_NOT_CONFIRMED_BY_READ", "STAGE2_FAILED", "STAGE2_ACK_UNKNOWN_HALTED",
    "STAGE2_ACK_NOT_BY_CALLBACK", "STAGE2_REJECTED",
    "STAGE3_ENTRY_NOT_FILLED", "STAGE3_PROTECTION_UNCONFIRMED", "STAGE3_PROTECTION_LOST",
    "STAGE3_CLOSE_REFUSED", "STAGE3_CLOSE_NOT_FILLED", "STAGE3_NOT_CLEAN",
    "STAGE3_NO_USABLE_QUOTE", "STAGE3_ENTRY_REFUSED", "STAGE3_CLOSE_NO_QUOTE",
    "STAGE3_CLOSE_OUTCOME_UNKNOWN", "STAGE3_PROTECTION_NOT_RESTORED",
    "ABORT_STOP_LEFT_WORKING", "ABORT_NOT_FLAT_NO_STOP"})


# paper-test-2 stage 2 (the single unfillable order)
SINGLE_ENDINGS = frozenset({
    "STAGE2_REJECTED", "STAGE2_UNEXPECTED_FILL", "STAGE2_CANCEL_NOT_CONFIRMED_BY_READ",
    "STAGE2_ACK_BY_RECONCILIATION", "STAGE2_ACK_NOT_BY_CALLBACK", "STAGE2_ACK_UNKNOWN_HALTED",
    "STAGE2_FAILED"})


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
    plan = _plan(cfg)
    if stage not in plan.stages:
        raise StageRefused(f"unknown stage {stage!r} for {plan.experiment} (its stages: "
                           f"{', '.join(plan.stages)})")
    need = plan.prerequisite(stage)
    if need is not None:
        prev = _read(evidence_path(cfg, need))
        if (prev is None or prev.get("passed") is not True or prev.get("stage") != need
                or str(prev.get("result", "")).endswith("_UNEXPECTED_FILL")):
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
    # serialised first: a failure here leaves the earlier attempt's file where it is
    text = redact_accounts(json.dumps(evidence, indent=2, sort_keys=True, default=str))
    if path.exists():  # an earlier failed attempt is kept, never overwritten
        path.rename(d / f"{stage}.attempt-{datetime.now(UTC):%Y%m%dT%H%M%S%fZ}.json")
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
            "client_id": s.client_id, "parent_id": s.parent_id, "reason": s.reason,
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


def _diagnose(s: PaperOrderSession, ev: dict) -> None:
    """Every ending (normal or not) keeps what the session observed (masked by ``_write``)."""
    try:
        ev["session_diagnostics"] = s.diagnostics()
    except Exception as exc:  # noqa: BLE001 - evidence must never mask the real ending
        ev["session_diagnostics"] = {"error": redact_accounts(f"{type(exc).__name__}: {exc}")}


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
        _diagnose(s, ev)
        return
    except BaseException:
        _diagnose(s, ev)  # any other failed connect: still in the evidence
        raise
    try:
        ev["refused"] = False
        ev["read_only_settings"] = s.read_only_settings
        ev["passed"] = False
        ev["why_failed"] = "order mode STARTED: Read-Only API is OFF. Turn it ON for stage 0."
    finally:
        ev["order_frames_sent"] = s.order_frames_sent
        _diagnose(s, ev)
        s.disconnect()


def _stage1(cfg: PaperOrderConfig, ev: dict) -> None:
    s = _session(cfg)
    try:
        s.connect()  # inside: a failed connect still leaves its diagnostics in the evidence
        c = s.contract
        ev["contract"] = {"con_id": c.con_id, "symbol": c.symbol, "min_tick": str(c.min_tick),
                          "time_zone_id": c.time_zone_id, "liquid_hours": c.liquid_hours}
        ev["read_only_settings"] = s.read_only_settings
        ev["lookups"] = _lookups(s)
        b = IbkrPaperBroker(s)
        contract = b.resolve(Instrument(asset_class=AssetClass.ETF, symbol=c.symbol))
        samples = ev["quote_samples"] = []
        usable = None
        for _ in range(STAGE1_QUOTE_SAMPLES):  # bounded; passes on the first usable label
            b.last_quote_report = None
            try:
                q = b.quote(contract)
                rec = {"quality": q.quality.value, "bid": str(q.bid), "ask": str(q.ask),
                       **(b.last_quote_report or {})}
                rec.setdefault("reason", "")
                ok = (q.quality.value in USABLE_QUALITIES and q.bid is not None and q.bid > 0
                      and q.ask is not None and q.ask > 0)
            except Exception as exc:  # noqa: BLE001 - evidence: the sample failed
                rec = {"quality": None, "reason": redact_accounts(f"{type(exc).__name__}: "
                                                                  f"{exc}")}
                ok = False
            samples.append(rec)
            if ok:
                usable = rec
                break
        ev["quote"] = usable or samples[-1]
        ev["account_rows_checked"] = not s.poisoned  # a row naming another account poisons
        ev["order_frames_sent"] = s.order_frames_sent
        ev["passed"] = (all(v["completed"] for v in ev["lookups"].values())
                        and not s.poisoned and s.order_frames_sent == 0 and usable is not None)
        if usable is None:
            last = samples[-1]
            ev["result"] = "STAGE1_QUOTE_NOT_USABLE"
            ev["why_failed"] = (
                f"STAGE1_QUOTE_NOT_USABLE: {len(samples)} bounded quote sample(s) labelled "
                f"{[x['quality'] for x in samples]} by the adapter (last reason: "
                f"{last.get('reason') or 'none given'}; flags {last.get('flags', [])}). "
                + ("Stage 2 needs a LIVE or DELAYED quote with a bid and an ask, stage 3 a LIVE "
                   "one." if _plan(cfg) is PAPER_TEST_1_PLAN else
                   "Stages 2 and 3 need a LIVE or DELAYED quote with a bid and an ask, stage 4 a "
                   "LIVE one.") + " no order was sent; run stage 1 again.")
    finally:
        _diagnose(s, ev)
        s.disconnect()


def _require_count(s: PaperOrderSession, stage: str, plan: ExperimentPlan | None = None) -> None:
    want = (plan or PAPER_TEST_1_PLAN).before(stage)
    if s.submissions_used != want:
        raise StageRefused(f"{stage} starts from {want} submissions; the journal shows "
                           f"{s.submissions_used}. It cannot run (again).")


def _orders_as_read(after: dict, ids: tuple[int, ...]) -> dict[int, dict]:
    """Each order as one read after the cancels shows it (own rows only). ``in_open_orders`` is
    None when the open-orders read failed; ``completed_status`` None when there is no row."""
    opened, done = after["open_orders"], after["completed_orders"]
    out = {}
    for k in ids:
        o = next((r for r in opened.get("rows", []) if r["own"] and r["order_id"] == k), None)
        d = next((r for r in done.get("rows", []) if r["own"] and r["order_id"] == k), None)
        out[k] = {"in_open_orders": (o is not None) if opened.get("completed") else None,
                  "open_status": o["status"] if o else None,
                  "completed_read": bool(done.get("completed")),
                  "completed_status": d["raw_status"] if d else None,
                  "completed_reason": d.get("reason") if d else None}
    return out


def _confirmed_cancelled(r: dict) -> bool:
    return (r["in_open_orders"] is False and r["completed_read"]
            and r["completed_status"] in CANCELLED_TEXT)


def _as_read_text(k: int, r: dict) -> str:
    where = ("open orders not read" if r["in_open_orders"] is None else
             f"in open orders (status {r['open_status']})" if r["in_open_orders"] else
             "not in open orders")
    done = (f"{r['completed_status']} in completed orders" if r["completed_status"] else
            "no completed-order row" if r["completed_read"] else "completed orders not read")
    return f"order {k}: {where}, {done}"


def _refusal_kind(exc: BaseException) -> str:
    """A refused cancel: the session's own refusal, or the broker's code (``CancelFailed`` reads
    'cancel of order N refused: <code> <text>', see ``PaperOrderSession.cancel``)."""
    if isinstance(exc, OrderNotWorking):
        return "NOT_A_WORKING_ORDER_OF_THIS_CLIENT"
    m = re.search(r"refused: (\d+) ", str(exc))
    return {"161": "NOT_CANCELLABLE", "10147": "NOT_FOUND"}.get(m.group(1) if m else "", "OTHER")


def _callback_ack(s: PaperOrderSession, ids: list[int]) -> dict[int, dict]:
    """Per order id: did an ``openOrder`` AND an ``orderStatus`` NAMING THIS CLIENT (its client id
    in the message) arrive by callback within the acknowledgement wait after the order's send?
    Waits (reads nothing, sends nothing) until both are in or the wait has passed. Read from the
    session's own event record; a row from any later lookup (the reconciliation included) is
    outside the wait and never counts."""
    cid, wait = s._cfg.client_id, s._ack_timeout

    def look() -> dict[int, dict]:
        with s._cond:
            events = [dict(e) for e in s.order_events if e["order_id"] in ids]
        out = {}
        for oid in ids:
            mine = [e for e in events if e["order_id"] == oid]
            sent = next((e["mono"] for e in mine if e["event"] == "SENT_PLACE"), None)
            rec: dict = {"wait_seconds": wait}
            for name in ("openOrder", "orderStatus"):
                first = next((e for e in mine if e["event"] == name and e.get("client_id") == cid
                              and not e.get("lookup")
                              and sent is not None and sent <= e["mono"] <= sent + wait), None)
                rec[name] = None if first is None else {
                    "at": first["at"], "seconds_after_send": round(first["mono"] - sent, 3),
                    "status": first.get("status")}
            rec["ok"] = rec["openOrder"] is not None and rec["orderStatus"] is not None
            rec["sent_mono"] = sent
            out[oid] = rec
        return out

    while True:
        got = look()
        last = max((r["sent_mono"] or 0) for r in got.values()) + wait
        if all(r["ok"] for r in got.values()) or time.monotonic() > last:
            for r in got.values():
                r.pop("sent_mono", None)
            return got
        time.sleep(0.02)


def _wire_of(s: PaperOrderSession, oid: int, kinds=("openOrder", "orderStatus")) -> list[dict]:
    """The protobuf order messages received for ``oid`` (field presence and order fields as the
    broker sent them; never the account)."""
    with s._cond:
        return [{k: v for k, v in w.items() if k != "mono"} for w in s.wire_events
                if w.get("order_id") == oid and w.get("msg") in kinds]


def _halt_unknown(s: PaperOrderSession, ev: dict, what: str) -> None:
    """ACK_HALT: the reconciliation is already in the evidence; record the ending; the caller
    re-raises. Nothing is cancelled or re-sent: the outcome is unknown, reconciled by hand."""
    ev["ack_timeout_policy"] = ACK_HALT
    ev["result"] = "STAGE2_ACK_UNKNOWN_HALTED"
    ev["state"] = state = _position_state(s)
    ev["why_failed"] = _ending_message(
        "STAGE2_ACK_UNKNOWN_HALTED", f"no acknowledgement of {what} within the wait; the read-only "
        f"reconciliation shows {ev.get('reconciliation') or 'nothing'}; nothing was cancelled or "
        "re-sent: the outcome is treated as unknown and reconciled by hand in TWS", state)


def _stage_single(cfg: PaperOrderConfig, ev: dict) -> None:
    """paper-test-2 stage 2: ONE BUY 1 LMT at 90% of the bid, transmitted on its own (cannot
    fill). Passes only if its openOrder AND orderStatus naming this client arrive BY CALLBACK
    within the acknowledgement wait, its cancel is confirmed, and a later read shows it gone and
    Cancelled, no execution and position 0. 1 submission."""
    plan = _plan(cfg)
    st = plan.stage(SINGLE)
    _check_start_time(st)
    s = _session(cfg)
    try:
        s.connect()
        _require_count(s, st, plan)
        _check_time_to_close(s.contract, st, _wall_clock())
        b = IbkrPaperBroker(s)
        c = s.contract
        q = b.quote(b.resolve(Instrument(asset_class=AssetClass.ETF, symbol=c.symbol)))
        tick = Decimal(str(c.min_tick))
        if q.bid is None or q.bid <= 0:
            ev.update(passed=False, why_failed="no bid to price an unfillable order from")
            return
        limit = _tick_floor(q.bid * UNFILLABLE_FRACTION, tick)
        ev["quote"] = {"quality": q.quality.value, "bid": str(q.bid), "ask": str(q.ask)}
        ev["order"] = {"buy_limit": str(limit), "transmit": True, "child": None}
        ref = f"TB-{cfg.experiment.name}-stage{plan.number(SINGLE)}"
        late_ack = False
        try:
            snap = s.place([OrderSpec(ref, "BUY", 1, "LMT", limit_price=limit)])[0]
        except OrderOutcomeUnknown as exc:
            rec = exc.reconciliation or {}
            ev["reconciliation"] = {str(k): v for k, v in sorted(rec.items())}
            ev["ack_error"] = redact_accounts(str(exc))
            if plan.ack_timeout == ACK_HALT:
                _halt_unknown(s, ev, "the single order")
                raise
            if len(rec) != 1 or set(rec.values()) != {"WORKING"}:
                raise
            late_ack = True
            with s._cond:
                snap = s._snap(s._tracks[next(iter(rec))])
        oid = snap.order_id
        ev["placed"] = [_row(snap)]
        ev["callback_ack"] = {str(k): v for k, v in _callback_ack(s, [oid]).items()}
        by_callback = ev["callback_ack"][str(oid)]["ok"] and not late_ack
        ev["ack_by"] = "callback" if by_callback else (
            "reconciliation" if late_ack else "incomplete callbacks")
        ev["working_order_messages"] = _wire_of(s, oid)
        t = s._tracks[oid]
        if t.status == OrderStatus.REJECTED or t.rejected:
            ev["result"], ev["passed"] = "STAGE2_REJECTED", False
            ev["state"] = state = _position_state(s)
            ev["why_failed"] = _ending_message(
                "STAGE2_REJECTED", f"the broker rejected order {oid} ({t.rejected or 'no text'}); "
                "nothing is working; nothing was re-sent", state)
            return
        ev["cancel"], ev["cancel_errors"] = [], []
        if t.status not in FINAL_STATUSES:
            try:
                ev["cancel"].append(_row(s.cancel(oid)))
            except Exception as exc:  # noqa: BLE001 - evidence; the read decides
                ev["cancel_errors"].append({"order_id": oid, "error": redact_accounts(str(exc))})
        as_read: dict[int, dict] = {}
        after: dict = {}
        for _ in range(STAGE2_CONFIRM_READS):  # reads only
            time.sleep(READ_BACK_WAIT_SECONDS)
            after = _lookups(s)
            as_read = _orders_as_read(after, (oid,))
            if _confirmed_cancelled(as_read[oid]):
                break
        ev["after_cancel"] = after
        ev["orders_as_read"] = {str(k): v for k, v in as_read.items()}
        execs = [r for r in after["executions"].get("rows", []) if r["order_id"] == oid]
        pos_rows = after["positions"].get("rows")
        position = (None if pos_rows is None or not after["positions"].get("completed") else
                    sum((Decimal(r["quantity"]) for r in pos_rows if r["con_id"] == c.con_id),
                        Decimal(0)))
        filled = bool(execs) or t.filled > 0
        ev["filled"], ev["position"] = filled, None if position is None else str(position)
        confirmed = _confirmed_cancelled(as_read[oid])
        ev["submissions_used"] = s.submissions_used
        ev["passed"] = (by_callback and confirmed and not filled and position == 0
                        and s.submissions_used == plan.before(st) + 1)
        ev["result"] = "STAGE2_PASSED" if ev["passed"] else "STAGE2_FAILED"
        if ev["passed"]:
            return
        ev["state"] = state = _position_state(s)
        read_text = _as_read_text(oid, as_read[oid])
        if filled:
            result, what = "STAGE2_UNEXPECTED_FILL", (
                f"the unfillable order {oid} FILLED (no protective stop exists for it); "
                f"{read_text}; stage {plan.number(SINGLE) + 1} is blocked")
        elif not confirmed:
            result, what = "STAGE2_CANCEL_NOT_CONFIRMED_BY_READ", (
                f"after the cancel the read shows {read_text}; nothing was re-sent")
        elif late_ack:
            result, what = "STAGE2_ACK_BY_RECONCILIATION", (
                "no acknowledgement callback within the wait; the read-only lookup showed the "
                f"order working; it is cancelled (confirmed by a later read: {read_text}); "
                "nothing was re-sent")
        elif not by_callback:
            result, what = "STAGE2_ACK_NOT_BY_CALLBACK", (
                "the order was acknowledged, but not by BOTH an openOrder and an orderStatus "
                f"naming this client within {s._ack_timeout:.1f} s "
                f"({ev['callback_ack'][str(oid)]}); it is cancelled (confirmed by a later read: "
                f"{read_text}); nothing was re-sent")
        else:
            result, what = "STAGE2_FAILED", (
                f"the read after the cancel shows {read_text}, position "
                f"{ev['position']}, submissions used {s.submissions_used}")
        ev["result"] = result
        ev["why_failed"] = _ending_message(result, what, state)
    finally:
        _diagnose(s, ev)
        s.disconnect()


def _stage2(cfg: PaperOrderConfig, ev: dict) -> None:
    plan = _plan(cfg)
    st, n = plan.stage(BRACKET), plan.number(BRACKET)
    _check_start_time(st)  # an unexpected fill must be closable by hand before 16:00
    s = _session(cfg)
    try:
        s.connect()
        _require_count(s, st, plan)
        _check_time_to_close(s.contract, st, _wall_clock())
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
        ref = f"TB-{cfg.experiment.name}-stage{n}"
        late_ack = False
        try:
            snaps = s.place([OrderSpec(ref, "BUY", 1, "LMT", limit_price=limit),
                             OrderSpec(f"{ref}.STP", "SELL", 1, "STP", stop_price=stop)])
        except PartialBracket:
            raise
        except OrderOutcomeUnknown as exc:
            rec = exc.reconciliation or {}
            ev["reconciliation"] = {str(k): v for k, v in sorted(rec.items())}
            ev["ack_error"] = redact_accounts(str(exc))
            if plan.ack_timeout == ACK_HALT:
                _halt_unknown(s, ev, "the bracket")
                raise
            # Never re-sent. Only when the read-only lookup shows BOTH orders working does the
            # stage go on (read back, then the same confirmed cancels, parent first); any other
            # picture (held, not found, another client's row) ends here: run abort.
            if len(rec) != 2 or set(rec.values()) != {"WORKING"}:
                raise
            late_ack = True
            with s._cond:
                snaps = [s._snap(s._tracks[i]) for i in sorted(rec)]
        parent, child = snaps[0].order_id, snaps[1].order_id
        ev["placed"] = [_row(x) for x in snaps]
        by_callback = True
        if plan.callback_ack_required:
            ack = _callback_ack(s, [parent, child])
            ev["callback_ack"] = {str(k): v for k, v in ack.items()}
            by_callback = not late_ack and all(r["ok"] for r in ack.values())
            ev["working_order_messages"] = {str(k): _wire_of(s, k) for k in (parent, child)}
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
                try:
                    ev["cancel"].append(_row(s.cancel(child)))
                except (CancelFailed, OrderNotWorking) as exc:
                    # TWS cancels a child with its parent and may tell this client nothing about
                    # the child (13 Nov 2026): the refusal is recorded and the READ decides.
                    kind = _refusal_kind(exc)
                    ev["child_cancel_refused"] = {"order_id": child, "kind": kind,
                                                  "error": redact_accounts(
                                                      f"{type(exc).__name__}: {exc}")}
                    if kind == "OTHER":
                        ev["cancel_errors"].append({"order_id": child,
                                                    "error": redact_accounts(str(exc))})
        after: dict = {}
        as_read: dict[int, dict] = {}
        for _ in range(STAGE2_CONFIRM_READS):  # reads only
            time.sleep(READ_BACK_WAIT_SECONDS)
            after = _lookups(s)
            as_read = _orders_as_read(after, (parent, child))
            if not parent_clean or all(_confirmed_cancelled(r) for r in as_read.values()):
                break
        ev["after_cancel"] = after
        ev["orders_as_read"] = {str(k): v for k, v in as_read.items()}
        both_confirmed = parent_clean and all(_confirmed_cancelled(r) for r in as_read.values())
        # what TWS did to the child when the parent was cancelled: the first callback for the
        # child after the parent's cancel (and before any cancel of the child), then the read
        parent_cancel_at = next((e["mono"] for e in s.order_events if e["event"] == "SENT_CANCEL"
                                 and e["order_id"] == parent), None)
        if parent_cancel_at is not None:
            child_cancel_at = next((e["mono"] for e in s.order_events
                                    if e["event"] == "SENT_CANCEL" and e["order_id"] == child),
                                   float("inf"))
            first = next((e for e in s.order_events if e["order_id"] == child
                          and parent_cancel_at < e["mono"] < child_cancel_at
                          and e["event"] in ("openOrder", "orderStatus", "error")), None)
            ev["child_on_parent_cancel"] = {
                "callback_for_child_after_parent_cancel": None if first is None else {
                    k: v for k, v in first.items() if k in ("at", "event", "status", "code")},
                "child_cancel_sent": child_cancel_at != float("inf"),
                **{k: as_read[child][k] for k in ("in_open_orders", "completed_status",
                                                  "completed_reason")}}
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
        ev["passed"] = (not filled and not still_open
                        and s.submissions_used == plan.before(st) + plan.of(st)
                        and both_confirmed and by_callback
                        and not ev["parent_answered_before_child_sent"]
                        and all(done.get(k) in CANCELLED_TEXT for k in (parent, child))
                        and all(v["completed"] for v in ev["read_back"].values()))
        ev["result"] = "STAGE2_PASSED" if ev["passed"] else "STAGE2_FAILED"
        ev["ack_by"] = "reconciliation" if late_ack else "callback"
        read_text = "; ".join(_as_read_text(k, as_read[k]) for k in (parent, child)) \
            if as_read else "the read after the cancels failed"
        refused = ev.get("child_cancel_refused")
        refused_text = (f"; the child's cancel was refused ({refused['kind']})" if refused
                        else "")
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
                f"stop (order {child}) is {stop_text}; stage {n + 1} is blocked (runbook: 'If "
                f"stage {n} fills')", state)
        elif not parent_clean:
            ev["result"] = "STAGE2_PARENT_NOT_CONFIRMED_CANCELLED"
            ev["passed"] = False
            ev["state"] = state = _position_state(s)
            ev["why_failed"] = _ending_message(
                "STAGE2_PARENT_NOT_CONFIRMED_CANCELLED", "the parent was not confirmed "
                "cancelled; its stop child was left alone", state)
        elif not both_confirmed:
            # Never "cancelled" without the confirming read: each order is stated as read.
            result = ("STAGE2_CHILD_NOT_CONFIRMED_CANCELLED"
                      if _confirmed_cancelled(as_read[parent]) else
                      "STAGE2_CANCEL_NOT_CONFIRMED_BY_READ")
            ev["result"], ev["passed"] = result, False
            ev["state"] = state = _position_state(s)
            ev["why_failed"] = _ending_message(
                result, f"after the cancels the read shows {read_text}{refused_text}; "
                "nothing was re-sent" + ("; the acknowledgement came only from the read-only "
                                         "lookup after the wait" if late_ack else ""), state)
        elif late_ack:
            # The acknowledgement came only from the read-only lookup after the wait: the ack
            # path is NOT proven. The cancels are confirmed by a later read. Not a pass.
            ev["passed"] = False
            ev["result"] = "STAGE2_ACK_BY_RECONCILIATION"
            ev["state"] = state = _position_state(s)
            ev["why_failed"] = _ending_message(
                "STAGE2_ACK_BY_RECONCILIATION", "no acknowledgement callback within the wait; the "
                "read-only lookup showed both orders working; both are cancelled (confirmed by "
                f"a later read: {read_text}){refused_text}; nothing was re-sent", state)
        elif not by_callback:
            ev["passed"] = False
            ev["result"] = "STAGE2_ACK_NOT_BY_CALLBACK"
            ev["state"] = state = _position_state(s)
            ev["why_failed"] = _ending_message(
                "STAGE2_ACK_NOT_BY_CALLBACK", "the bracket was acknowledged, but not by BOTH an "
                "openOrder and an orderStatus naming this client for each order within "
                f"{s._ack_timeout:.1f} s ({ev['callback_ack']}); both are cancelled (confirmed by "
                f"a later read: {read_text}){refused_text}; nothing was re-sent", state)
        elif not ev["passed"]:
            ev["state"] = state = _position_state(s)
            ev["why_failed"] = _ending_message(
                "STAGE2_FAILED", "the read-back, the cancels or the completed orders did not "
                f"show what stage {n} needs", state)
    finally:
        _diagnose(s, ev)
        s.disconnect()


def _wait(pred, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        if pred():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(POLL_SECONDS)


def _passing_count(filled_on_attempt: int, plan: ExperimentPlan | None = None) -> int:
    """The durable count after a CLEAN stage 3 whose close filled on attempt ``k``: stage 2's 2,
    the entry and its stop 2, the amendment 1, then per attempt the closing limit 1, and per
    unfilled attempt before it the stop placed again 1. 6 for the first attempt, 8 for the second;
    STAGE3_PASSED accepts exactly that number and nothing else."""
    plan = plan or PAPER_TEST_1_PLAN
    return plan.before(plan.stage(LIFECYCLE)) + 3 + 1 + 2 * (filled_on_attempt - 1)


def _stop_proof_after_fill(s: PaperOrderSession, coord, entry_ref: str, seen_filled: float,
                            ev: dict) -> tuple[str | None, str]:
    """paper-test-2 stage 4, after the entry filled (READS ONLY, nothing is sent). Within
    ``PROTECTION_SETTLE_SECONDS`` of the entry's fill: (1) a status of the protective stop by
    callback after the fill, naming this client, PreSubmitted or Submitted; (2) an open-orders
    read showing it working at its stop price, quantity 1, linked to the entry; (3) the position
    read 1 equal to the experiment's own fills 1. Then ``STOP_HOLD_SECONDS`` and the read again:
    the stop still working, the position still 1. Returns (an existing protection ending or
    None, what was seen)."""
    ledger = coord.ledger()[0]
    ids = s.order_ids()
    entry_id = next((i for i, r in ids.items() if r == entry_ref), None)
    stop_id = next((i for i, r in ids.items() if r == ledger.stop_ref), None)
    cid = s._cfg.client_id
    with s._cond:
        entry_events = [dict(e) for e in s.order_events if e["order_id"] == entry_id]
    filled_at = next((e["mono"] for e in entry_events if e["event"] == "execDetails"), None)
    if filled_at is None:
        filled_at = next((e["mono"] for e in entry_events if e["event"] == "orderStatus"
                          and e.get("status") == "Filled"), seen_filled)
    deadline = filled_at + PROTECTION_SETTLE_SECONDS
    want_stop = Decimal(ledger.stop_price) if ledger.stop_price else None
    rec = ev["stop_after_fill"] = {
        "entry_order_id": entry_id, "stop_order_id": stop_id, "stop_ref": ledger.stop_ref,
        "stop_price_expected": None if want_stop is None else str(want_stop),
        "bound_seconds": PROTECTION_SETTLE_SECONDS, "hold_seconds": STOP_HOLD_SECONDS}

    def look() -> dict:
        with s._cond:
            cbs = [dict(e) for e in s.order_events if e["order_id"] == stop_id
                   and e["event"] in ("orderStatus", "openOrder") and e["mono"] >= filled_at]
        why = [w for w in _wire_of(s, stop_id, ("orderStatus",)) if w.get("why_held")]
        callbacks = [{"event": e["event"], "status": e.get("status"), "at": e["at"],
                      "seconds_after_fill": round(e["mono"] - filled_at, 3),
                      "names_this_client": e.get("client_id") == cid,
                      "during_a_lookup": bool(e.get("lookup"))} for e in cbs]
        try:
            row = next((o for o in s.open_orders() if o.own and o.order_id == stop_id), None)
            read_error = None
        except Exception as exc:  # noqa: BLE001 - a failed read is not a working stop
            row, read_error = None, redact_accounts(f"{type(exc).__name__}: {exc}")
        state = _read_state(s)
        got = {
            "callbacks_after_fill": callbacks, "why_held_seen": [w["why_held"] for w in why],
            # an orderStatus for the stop naming this client, after the fill, working. TWS sends
            # the same message unprompted and in reply to a lookup; which one it was cannot be
            # told from the API, so each is marked (during_a_lookup) and both count
            "callback_working": any(c["event"] == "orderStatus" and c["names_this_client"]
                                    and c["status"] in ("PreSubmitted", "Submitted")
                                    for c in callbacks),
            "open_orders_row": None if row is None else {
                "status": row.status.value, "raw_status": row.raw_status,
                "stop_price": str(row.stop_price), "quantity": str(row.total_quantity),
                "remaining": str(row.remaining), "parent_id": row.parent_id},
            "open_orders_error": read_error,
            # from the broker's answer itself: never a row the session adds from its own state
            "row_from_broker": row is not None and row.reason != "LOCAL_STATE_NOT_IN_SNAPSHOT",
            "row_working": (row is not None and row.reason != "LOCAL_STATE_NOT_IN_SNAPSHOT"
                            and row.status in STOP_WORKING_STATUSES
                            and row.stop_price == want_stop and row.total_quantity == 1
                            and row.parent_id == entry_id),
            "position_read": state["position_read"], "own_fills": state["own_fills"],
            "position_one": bool(state["agree"]) and state["held"] == "1",
            "seconds_after_fill": round(time.monotonic() - filled_at, 3)}
        got["stop_fields_as_reported"] = (_wire_of(s, stop_id, ("openOrder",)) or [None])[-1]
        return got

    while True:
        got = look()
        ok = got["callback_working"] and got["row_working"] and got["position_one"]
        if ok or time.monotonic() >= deadline:
            break
        time.sleep(POLL_SECONDS)
    rec["after_fill"] = got
    if not ok:
        parts = (("a working stop status by callback", "callback_working"),
                 (("the stop working in an open-orders read at its price, quantity 1, linked "
                   "to the entry"), "row_working"),
                 ("position read 1 = own fills 1", "position_one"))
        missing = [name for name, key in parts if not got[key]]
        return "STAGE3_PROTECTION_UNCONFIRMED", (
            f"the entry filled, but within {PROTECTION_SETTLE_SECONDS:.0f} s of its fill this was "
            f"not shown: {'; '.join(missing)} (position read {got['position_read']}, own fills "
            f"{got['own_fills']}); nothing more was sent")
    time.sleep(STOP_HOLD_SECONDS)  # reads nothing, sends nothing
    again = look()
    rec["after_hold"] = again
    if not (again["row_working"] and again["position_one"]):
        return "STAGE3_PROTECTION_LOST", (
            f"the protective stop was verified after the fill, but at the read "
            f"{STOP_HOLD_SECONDS:.0f} s later: stop working as read {again['row_working']} "
            f"(row {again['open_orders_row']}), position read {again['position_read']}, own fills "
            f"{again['own_fills']}; nothing more was sent")
    return None, "verified"


def _stage3_quote(broker: IbkrPaperBroker, contract) -> tuple[object | None, list[dict]]:
    """READS ONLY, bounded by ``QUOTE_RETRY_SECONDS``: the first strict quote that is LIVE, has a
    positive bid and ask (bid not above ask) and both sides at most
    ``STAGE3_QUOTE_MAX_AGE_SECONDS`` old, each age measured from the read's reference time (the
    same stamps the core measures). Returns (that quote or None, every read as evidence)."""
    reads: list[dict] = []
    deadline = time.monotonic() + QUOTE_RETRY_SECONDS
    while True:
        t = datetime.now(UTC)
        broker.set_reference_time(t)
        q = None
        try:
            q = broker.quote(contract)
            ages = [None if at is None else round((t - at).total_seconds(), 6)
                    for at in (q.bid_at, q.ask_at)]
            ok = (q.quality == QuoteQuality.LIVE and q.bid is not None and q.ask is not None
                  and q.bid > 0 and q.ask > 0 and q.bid <= q.ask
                  and all(a is not None and a <= STAGE3_QUOTE_MAX_AGE_SECONDS for a in ages))
            rep = broker.last_quote_report or {}
            rec = {"quality": q.quality.value, "bid": str(q.bid), "ask": str(q.ask),
                   "bid_age_seconds": ages[0], "ask_age_seconds": ages[1],
                   "adapter_label": rep.get("adapter_label"),
                   "problems": list(rep.get("problems", [])), "usable": ok}
        except Exception as exc:  # noqa: BLE001 - evidence: the read failed, nothing was sent
            ok = False
            rec = {"quality": None, "usable": False,
                   "error": redact_accounts(f"{type(exc).__name__}: {exc}")}
        rec["read_at"] = t.isoformat()
        reads.append(rec)
        if ok or time.monotonic() >= deadline:
            # later reads (the core's exit reads) anchor at their own read time; the core then
            # measures their age after the read (touch mode)
            broker.set_reference_time(None)
            return (q if ok else None), reads
        time.sleep(POLL_SECONDS)


def _stage3(cfg: PaperOrderConfig, ev: dict) -> None:
    plan = _plan(cfg)
    st, n = plan.stage(LIFECYCLE), plan.number(LIFECYCLE)
    _check_start_time(st)
    quality = (_read(evidence_path(cfg, plan.stage(LOOKUPS))) or {}).get("quote", {}).get(
        "quality")
    if quality != QuoteQuality.LIVE.value:
        raise StageRefused(f"stage 1 recorded quote quality {quality!r}; stage {n} needs LIVE (a "
                           f"DELAYED quote does not qualify for stage {n})")
    s = _session(cfg)
    coord = None
    try:
        s.connect()
        _require_count(s, st, plan)
        _check_time_to_close(s.contract, st, _wall_clock(),
                             STOP_HOLD_SECONDS if plan.stop_proof_after_fill else 0)
        policy = MockPolicy(quantity=1, quantity_per_add=1, max_notional_usd=Decimal(150),
                            max_price_deviation_pct=Decimal(1),
                            max_quote_age_seconds=STAGE3_QUOTE_MAX_AGE_SECONDS)
        # The owner's exit decision: the close is a SELL limit AT the fresh bid (no haircut); the
        # exit dry check prices its probe the same way. Quotes are strict: never fresher or more
        # live than the adapter measured them (both sides' times, the older one is the quote's).
        broker = IbkrPaperBroker(s, exit_at_touch=True, strict_quotes=True)
        config = ExecutionConfig(halt_file=evidence_dir(cfg) / "HALT",
                                 expected_account=s.account_id,
                                 accepted_quote_qualities=frozenset({QuoteQuality.LIVE}),
                                 protective_tif=TimeInForce.DAY, confirm_attempts=CONFIRM_ATTEMPTS,
                                 confirm_wait_seconds=CONFIRM_WAIT_SECONDS,
                                 exit_price_policy="TOUCH",
                                 exit_validity_seconds=CLOSE_WAIT_SECONDS)
        store = ("stage3-execution.sqlite3" if plan is PAPER_TEST_1_PLAN
                 else f"{st}-execution.sqlite3")
        coord = ExecutionCoordinator(broker, evidence_dir(cfg) / store, policy, config)
        c = s.contract
        tick = Decimal(str(c.min_tick))
        contract = broker.resolve(Instrument(asset_class=AssetClass.ETF, symbol=c.symbol))

        def fail(result: str, what: str) -> None:
            ev["state"] = state = _position_state(s)  # retried until read and fills agree
            ev.update(passed=False, result=result,
                      why_failed=_ending_message(result, what, state))

        q, reads = _stage3_quote(broker, contract)
        ev["entry_quote_reads"] = reads
        if q is None:
            fail("STAGE3_NO_USABLE_QUOTE", f"no LIVE quote with a bid and an ask, each side at "
                 f"most {STAGE3_QUOTE_MAX_AGE_SECONDS} s old, in {QUOTE_RETRY_SECONDS:.0f} s of "
                 f"reads ({len(reads)} read(s)); nothing was sent and stage {n} may run again")
            return
        # The owner's choice (private config, 0 when absent, at most caps ceiling): the entry is
        # STATED at the ask plus that many ticks; the core sends exactly the stated price.
        ticks = cfg.stage3_entry_allowance_ticks
        entry = q.ask + ticks * tick
        ev["entry_price"] = {"ask": str(q.ask), "allowance_ticks": ticks, "tick": str(tick),
                             "stated_entry": str(entry)}
        stop = _tick_floor(entry - STOP_DISTANCE, tick)
        now = datetime.now(UTC)
        src = ReplaySource(source_id=f"{cfg.experiment.name}-stage{n}", revision=1,
                           published_at=now, received_at=now, verified_author=True,
                           complete_text=True, post_hash=hashlib.sha256(
                               f"owner-authorized {cfg.experiment.name} stage{n}".encode()
                           ).hexdigest())
        gate = GateDecision(status="APPROVE",
                            reasons=[f"OWNER_AUTHORIZED_{cfg.experiment.name}_STAGE{n}"])

        def act(action_id, kind, **kw):
            return TradeAction(
                action_id=action_id, action=kind, timing=Timing.CURRENT,
                instrument=Instrument(asset_class=AssetClass.ETF, symbol=c.symbol),
                position_side=PositionSide.LONG, price_basis=PriceBasis.TRADED_CONTRACT,
                source_market_symbol=c.symbol, explicit_instruction=True,
                evidence=[f"owner-authorized {cfg.experiment.name} stage {n}"], **kw)

        steps = ev["steps"] = []

        def step(name, outcome):
            steps.append({"step": name, "state": outcome.state.value,
                          "protection": outcome.protection, "reasons": list(outcome.reasons),
                          "submissions_used": s.submissions_used})
            return outcome

        broker.set_reference_time(datetime.now(UTC))  # no later than the core's own now
        opened = step("open", coord.execute(src, act("open", Action.OPEN,
                                                     entry_reference=str(entry),
                                                     stop_price=str(stop)), gate))
        broker.set_reference_time(None)
        if opened.state in (IntentState.REFUSED, IntentState.REJECTED, IntentState.HALTED):
            fail("STAGE3_ENTRY_REFUSED", f"the entry was {opened.state.value} "
                 f"({', '.join(opened.reasons) or 'no reason given'})")
            return
        if not _wait(lambda: coord.sync() and bool(coord.ledger())
                     and coord.ledger()[0].quantity == 1, FILL_TIMEOUT_SECONDS):
            fail("STAGE3_ENTRY_NOT_FILLED", "the entry did not fill in time")
            return
        seen_filled = time.monotonic()
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
        if plan.stop_proof_after_fill:  # paper-test-2: the owner's requirement (DECIDED 16)
            code, what = _stop_proof_after_fill(s, coord, opened.order_ref, seen_filled, ev)
            if code is not None:
                fail(code, what)
                return
        step("raise-stop", coord.execute(src, act("raise-stop", Action.MODIFY,
                                                  idea_reference=ledger.idea_id,
                                                  stop_price=str(stop + STOP_RAISE)), gate))
        idea = ledger.idea_id
        attempts = ev["close_attempts"] = []
        filled_on = None
        for k in range(1, STAGE3_CLOSE_ATTEMPTS + 1):
            rec: dict = {"attempt": k, "submissions_before": s.submissions_used}
            attempts.append(rec)
            q, reads = _stage3_quote(broker, contract)
            rec["quote_reads"] = reads
            if q is None:
                rec["outcome"] = "NOT_SENT_NO_USABLE_QUOTE"
                fail("STAGE3_CLOSE_NO_QUOTE", f"close attempt {k}: no LIVE quote with a bid and "
                     f"an ask, each side at most {STAGE3_QUOTE_MAX_AGE_SECONDS} s old, in "
                     f"{QUOTE_RETRY_SECONDS:.0f} s of reads; no closing limit was sent and the "
                     "protective stop was left as it was")
                return
            rec["quote"] = reads[-1]
            first_read = len(broker.quote_reports)
            # no reference time here: in touch mode the core measures each exit read's age
            # after the read itself (before the netting, and again after it, for the price sent)
            closed = step(f"close-{k}", coord.execute(src, act(f"close-{k}", Action.CLOSE,
                                                               idea_reference=idea), gate))
            # in order: the core's read before the netting (no stop is cancelled without it),
            # the stop cancel's dry-check estimate, then the read(s) after the netting that
            # priced the limit actually sent
            rec["core_quote_reads"] = broker.quote_reports[first_read:]
            ref = closed.order_ref
            sent = coord.intent(ref)
            journal = coord.store.journal(ref)
            priced = next((e.detail for e in journal if e.event == "EXIT_PRICED_AFTER_NETTING"),
                          None)
            rec.update(order_ref=ref, limit=sent.limit_price if sent else None,
                       priced_after_netting=priced, state_after_send=closed.state.value,
                       wait_seconds=CLOSE_WAIT_SECONDS)

            def stop_as_read(ref=ref, rec=rec) -> str:
                """Reads only: the stop placed again (if the core placed one) confirmed, then
                what the open-orders read shows; never assumed."""
                _wait(lambda: coord.sync() and coord.ledger()[0].protection == "CONFIRMED"
                      and _read_state(s)["stop_working"], PROTECTION_SETTLE_SECONDS)
                read = _read_state(s)
                again = any(e.event == "PROTECTIVE_STOP_REPLACE_SUBMIT"
                            for e in coord.store.journal(ref))
                rec.update(stop_placed_again=again,
                           protection_after=coord.ledger()[0].protection,
                           stop_ref_after=coord.ledger()[0].stop_ref,
                           stop_status_after=read["stop_status"],
                           stop_working_after=read["stop_working"],
                           submissions_after=s.submissions_used)
                head = "the core placed the protective stop again; " if again else ""
                if read["stop_working"]:
                    return head + f"a protective stop is working ({read['stop_status']}) as read"
                return head + ("NO protective stop of this client is working as read: a share "
                               "still held is UNPROTECTED")

            if broker.protection_lost:  # the close was refused after its stop was cancelled
                ev["protection_lost"] = broker.protection_lost
                rec["outcome"] = "NOT_SENT_PROTECTION_LOST"
                fail("STAGE3_PROTECTION_LOST", "POSITION_OPEN_PROTECTION_ABSENT when the close "
                     "was refused after the stop was cancelled (nothing sent); "
                     + stop_as_read())
                return
            if closed.state in (IntentState.REFUSED, IntentState.REJECTED, IntentState.HALTED):
                rec["outcome"] = closed.state.value
                fail("STAGE3_CLOSE_REFUSED", f"the close was {closed.state.value} and not working "
                     f"({', '.join(closed.reasons) or 'no reason given'}); " + stop_as_read())
                return
            t0 = time.monotonic()
            if closed.state not in UNRESOLVED_STATES:
                # the core cancels it once CLOSE_WAIT_SECONDS have passed (sync, confirmed)
                _wait(lambda ref=ref: coord.sync() and (coord.ledger()[0].quantity == 0
                                                        or coord.intent(ref).state
                                                        != IntentState.WORKING),
                      CLOSE_WAIT_SECONDS + CLOSE_SETTLE_SECONDS)
            now_i = coord.intent(ref)
            events = {e.event for e in coord.store.journal(ref)}
            rec.update(waited_seconds=round(time.monotonic() - t0, 3),
                       outcome=now_i.state.value, reasons=list(now_i.reasons),
                       cancel_requested="EXIT_EXPIRY_CANCEL_REQUESTED" in events,
                       cancel_confirmed="EXIT_EXPIRED_CANCEL_CONFIRMED" in events)
            if now_i.state == IntentState.FILLED or coord.ledger()[0].quantity == 0:
                rec["submissions_after"] = s.submissions_used
                filled_on = k
                break
            if now_i.state != IntentState.CANCELLED or EXIT_UNFILLED_CANCELLED not in now_i.reasons:
                rec["submissions_after"] = s.submissions_used
                fail("STAGE3_CLOSE_OUTCOME_UNKNOWN", f"close attempt {k}: the closing limit {ref} "
                     f"(1 share, limit {rec['limit']}, DAY) MAY STILL BE WORKING OR MAY HAVE FILLED: "
                     f"its outcome is not known from broker evidence (core state "
                     f"{now_i.state.value}, cancel requested {rec['cancel_requested']}, cancel "
                     f"confirmed {rec['cancel_confirmed']}); nothing more was sent and no "
                     "protective stop was placed again, so a share still held may be UNPROTECTED")
                return
            # cancelled unfilled: the core placed the stop again in the same sync; wait (reads
            # only) until it is CONFIRMED by the core and seen working, before anything else
            restored = _wait(lambda: coord.sync() and coord.ledger()[0].protection == "CONFIRMED"
                             and _read_state(s)["stop_working"], PROTECTION_SETTLE_SECONDS)
            after = coord.ledger()[0]
            read = _read_state(s)
            rec.update(protection_after=after.protection, stop_ref_after=after.stop_ref,
                       stop_status_after=read["stop_status"],
                       stop_working_after=read["stop_working"],
                       submissions_after=s.submissions_used)
            if not restored:
                fail("STAGE3_PROTECTION_NOT_RESTORED", "SHARE POSSIBLY UNPROTECTED: close attempt "
                     f"{k}'s closing limit {ref} was cancelled unfilled (confirmed), but the "
                     "protective stop placed again for the share was not confirmed working within "
                     f"{PROTECTION_SETTLE_SECONDS:.0f} s (core protection {after.protection}, stop "
                     f"read {read['stop_status'] or 'absent'}); nothing more was sent")
                return
        if filled_on is None:
            read = _read_state(s)
            fail("STAGE3_CLOSE_NOT_FILLED", f"the closing limit (1 share at the bid) did not "
                 f"fill in {STAGE3_CLOSE_ATTEMPTS} attempts of {CLOSE_WAIT_SECONDS:.0f} s; each "
                 "was cancelled with broker confirmation and the protective stop was placed "
                 f"again: a protective stop is working ({read['stop_status'] or 'not seen'}) as "
                 "read; nothing more was sent")
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
        ev["close_filled_on_attempt"] = filled_on
        want = _passing_count(filled_on, plan)
        ev["passed"] = bool(report.clean) and s.submissions_used == want
        ev["result"] = "STAGE3_PASSED" if ev["passed"] else "STAGE3_NOT_CLEAN"
        if not ev["passed"]:
            fail("STAGE3_NOT_CLEAN", f"reconciliation clean={bool(report.clean)}, submissions "
                 f"used {s.submissions_used} (a clean close on attempt {filled_on} uses {want})")
    finally:
        _diagnose(s, ev)
        if coord is not None:
            coord.close()
        s.disconnect()


def _abort(cfg: PaperOrderConfig, ev: dict) -> None:
    """Re-read the position and the experiment's own fills FIRST. Only when they agree on flat
    does abort cancel every working order of THIS client by its own order id (children first).
    Otherwise (a share held, the read and the fills disagree, or a read fails) it never cancels a
    protective SELL stop, nor the parent of one: it cancels only this client's other working
    orders (an unfilled entry without a working stop, a resting closing limit) and prints the
    TWS procedure. Never a global cancel. Works even when the journal is unknown."""
    s = _session(cfg)
    try:
        s.connect()
        state = ev["state"] = _position_state(s)
        flat = (state["agree"] and Decimal(state["held"]) == 0
                and state["own_working"] is not None)
        ev["flat"] = flat
        mine = [o for o in s.open_orders() if o.own]
        ev["found"] = [_row(o) for o in mine]

        def protective(o) -> bool:
            return o.action == "SELL" and o.order_type == "STP"

        guarded = {o.parent_id for o in mine if protective(o) and o.parent_id}
        targets = mine if flat else [o for o in mine if not protective(o)
                                     and o.order_id not in guarded]
        ev["left_working"] = [_row(o) for o in mine if o not in targets]
        # "requested": a cancel frame was sent (the answer may still say PendingCancel);
        # "confirmed": a LATER read shows the order gone from open orders and Cancelled in the
        # completed orders. Only a confirmed cancel is ever reported as cancelled.
        ev["cancels_requested"], ev["cancel_errors"] = [], []
        for o in sorted(targets, key=lambda o: (o.parent_id == 0, o.order_id)):
            t = s._tracks.get(o.order_id)
            if t is not None and t.status in FINAL_STATUSES:
                continue  # cancelled with its parent
            try:
                ev["cancels_requested"].append(_row(s.cancel(o.order_id)))
            except Exception as exc:  # noqa: BLE001 - reported, the next one is still tried
                ev["cancel_errors"].append({"order_id": o.order_id,
                                            "error": redact_accounts(str(exc))})
        time.sleep(READ_BACK_WAIT_SECONDS)
        after = _lookups(s)
        ev["after"] = after
        remaining = [r for r in after["open_orders"].get("rows", []) if r["own"]]
        ev["remaining_own_working"] = remaining
        ev["position"] = after["positions"].get("rows")
        still = {r["order_id"] for r in remaining}
        done = {r["order_id"]: r["status"] for r in after["completed_orders"].get("rows", [])
                if r["own"]}
        reads_ok = after["open_orders"].get("completed") and after["completed_orders"].get(
            "completed")
        ev["cancels_confirmed"] = [
            r for r in ev["cancels_requested"]
            if reads_ok and r["order_id"] not in still
            and done.get(r["order_id"]) in ("Cancelled", "ApiCancelled")]
        confirmed = {r["order_id"] for r in ev["cancels_confirmed"]}
        ev["cancels_unconfirmed"] = [
            {**r, "status_now": next((x["status"] for x in remaining
                                      if x["order_id"] == r["order_id"]),
                                     done.get(r["order_id"], "unknown"))}
            for r in ev["cancels_requested"] if r["order_id"] not in confirmed]
        if flat:
            ev["passed"] = bool(after["open_orders"]["completed"]) and not remaining
            return
        ev["passed"] = False
        kept = [r["order_id"] for r in ev["left_working"]]
        result = "ABORT_STOP_LEFT_WORKING" if kept else "ABORT_NOT_FLAT_NO_STOP"
        ev["result"] = result
        n_req, n_ok = len(ev["cancels_requested"]), len(ev["cancels_confirmed"])
        cancels = f"asked TWS to cancel {n_req} other order(s); {n_ok} confirmed cancelled by a " \
                  "later read"
        if ev["cancels_unconfirmed"]:
            cancels += "; NOT confirmed: " + ", ".join(
                f"order {r['order_id']} ({r['status_now']})" for r in ev["cancels_unconfirmed"])
            cancels += " - treat it as still working"
        what = (f"this script does not read the account flat, so abort left this client's "
                f"protective stop(s) {kept} working; it {cancels}" if kept else
                "this script does not read the account flat; no protective stop of this client "
                f"is working; abort {cancels}")
        # the instruction follows what is read now, after the cancel requests
        ev["state_after"] = now = _position_state(s)
        ev["why_failed"] = _ending_message(result, what, now) + (
            " " + ABORT_STOP_NOTE if kept else "")
    finally:
        _diagnose(s, ev)
        s.disconnect()


_RUN = {STAGES[0]: _stage0, STAGES[1]: _stage1, STAGES[2]: _stage2, STAGES[3]: _stage3,
        ABORT: _abort}
_BY_KIND = {REFUSAL: _stage0, LOOKUPS: _stage1, SINGLE: _stage_single, BRACKET: _stage2,
            LIFECYCLE: _stage3}


def _renumber(plan: ExperimentPlan, stage: str, ev: dict) -> None:
    """Report the stage function's result code with this experiment's stage number."""
    if stage == ABORT or plan.kind(stage) not in plan.codes:
        return
    old, new = plan.codes[plan.kind(stage)]
    for key in ("result", "why_failed"):
        value = ev.get(key)
        if isinstance(value, str) and value.startswith(old):
            ev[key] = new + value[len(old):]


def ending_codes(plan: ExperimentPlan) -> frozenset[str]:
    """Every abnormal ending code a stage of ``plan`` can report."""
    out = set()
    for kind in plan.kinds:
        prefix = {SINGLE: "STAGE2_", BRACKET: "STAGE2_", LIFECYCLE: "STAGE3_"}.get(kind)
        if prefix is None:
            continue
        old, new = plan.codes.get(kind, (prefix, prefix))
        own = SINGLE_ENDINGS if kind == SINGLE else {
            c for c in ABNORMAL_ENDINGS if c.startswith(prefix) and c != "STAGE2_REJECTED"}
        out |= {new + c[len(old):] if c.startswith(old) else c for c in own}
    return frozenset(out)


def run_stage(stage: str, config_path: str | os.PathLike | None = None) -> tuple[dict, Path]:
    """Run ONE stage (or ``abort``), write its evidence, return (evidence, path). Raises
    ``StageRefused`` without connecting when the order of stages does not allow it."""
    cfg = load_order_config(config_path)
    plan = _plan(cfg)
    if stage != ABORT:
        _check_order(cfg, stage)
    ev: dict = {"stage": stage, "experiment": cfg.experiment.name,
                "account_masked": cfg.connection.masked_account(),
                "started_at": datetime.now(UTC).isoformat(), "passed": False,
                "submissions_before": _journal_used(cfg),
                "submissions_of_stage": 0 if stage == ABORT else plan.of(stage)}
    run = _abort if stage == ABORT else _BY_KIND[plan.kind(stage)]
    try:
        run(cfg, ev)
    except StageRefused as exc:
        ev.update(passed=False, why_failed=str(exc))
    except Exception as exc:  # noqa: BLE001 - evidence of the failure, then re-raised below
        ev.update(passed=False, error=redact_accounts(f"{type(exc).__name__}: {exc}"))
    _renumber(plan, stage, ev)
    ev["submissions_after"] = _journal_used(cfg)
    ev["finished_at"] = datetime.now(UTC).isoformat()
    try:
        return ev, _write(cfg, stage, ev)
    except (TypeError, ValueError) as exc:  # the diagnostics must never cost the evidence
        if "session_diagnostics" not in ev:
            raise
        ev["session_diagnostics"] = {"error": redact_accounts(
            f"diagnostics not serialisable: {type(exc).__name__}: {exc}")}
        return ev, _write(cfg, stage, ev)
