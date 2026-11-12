"""The pre-trade guard: a pure function, called immediately before EVERY ``placeOrder`` (SDK-free).

``order_guard(order, ctx)`` returns ``ALLOW`` or a ``Refusal`` with a typed reason. It reads
nothing and writes nothing: everything it decides on is passed in (the proposed order, the
experiment caps, the durable submission count, the session-resolved contract, the current position
and working orders, the clock and the contract's liquid hours). The order session refuses to send
on anything but ``ALLOW``. The guard, not its caller, is the last line of defence: it re-checks
things the caller also checks (account, contract, side rules), because a caller bug must not be
enough to send an order outside the caps.
"""
from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import Enum
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from tradingbot.ibkr_orders.caps import ExperimentCaps


class RefusalReason(str, Enum):
    EXPERIMENT_FINISHED = "EXPERIMENT_FINISHED"  # the submission cap is already used up
    SUBMISSION_CAP = "SUBMISSION_CAP"  # this order (or the rest of its bracket) would exceed it
    SUBMISSION_COUNT_UNKNOWN = "SUBMISSION_COUNT_UNKNOWN"
    SUBMISSION_COUNT_MISMATCH = "SUBMISSION_COUNT_MISMATCH"  # the broker shows more than the journal
    WRONG_ACCOUNT = "WRONG_ACCOUNT"
    WRONG_SYMBOL = "WRONG_SYMBOL"
    WRONG_SEC_TYPE = "WRONG_SEC_TYPE"
    WRONG_CURRENCY = "WRONG_CURRENCY"
    WRONG_CONTRACT_ID = "WRONG_CONTRACT_ID"
    MARKET_ORDER = "MARKET_ORDER"
    ORDER_TYPE_NOT_ALLOWED = "ORDER_TYPE_NOT_ALLOWED"
    TIF_NOT_ALLOWED = "TIF_NOT_ALLOWED"
    OUTSIDE_RTH_FLAG = "OUTSIDE_RTH_FLAG"
    OUTSIDE_LIQUID_HOURS = "OUTSIDE_LIQUID_HOURS"
    LIQUID_HOURS_UNKNOWN = "LIQUID_HOURS_UNKNOWN"
    CLOCK_NOT_AWARE = "CLOCK_NOT_AWARE"
    BAD_SIDE = "BAD_SIDE"
    QUANTITY_INVALID = "QUANTITY_INVALID"
    ORDER_QUANTITY_CAP = "ORDER_QUANTITY_CAP"
    POSITION_CAP = "POSITION_CAP"
    NOTIONAL_CAP = "NOTIONAL_CAP"
    PRICE_INVALID = "PRICE_INVALID"
    OFF_TICK_PRICE = "OFF_TICK_PRICE"
    TICK_UNKNOWN = "TICK_UNKNOWN"
    POSITION_UNKNOWN = "POSITION_UNKNOWN"
    # the broker's position read and the position implied by this session's own fills differ
    POSITION_DISAGREEMENT = "POSITION_DISAGREEMENT"
    SHORT_POSITION_PRESENT = "SHORT_POSITION_PRESENT"
    SHORT_SALE = "SHORT_SALE"  # a sell with nothing long to sell, and not a protective child
    SELL_EXCEEDS_POSITION = "SELL_EXCEEDS_POSITION"  # net of other working sells
    CHILD_INVALID = "CHILD_INVALID"  # a child that is not a SELL protecting a BUY parent
    ORDER_REF_MISSING = "ORDER_REF_MISSING"
    MODIFICATION_INVALID = "MODIFICATION_INVALID"  # changes side/type/contract/parent/TIF/account
    BUY_NOT_LIMIT = "BUY_NOT_LIMIT"  # a BUY STP becomes a market order when triggered
    STOP_NOT_PROTECTIVE = "STOP_NOT_PROTECTIVE"  # a SELL STP that neither protects nor reduces


@dataclass(frozen=True)
class Allow:
    def __bool__(self) -> bool:
        return True

    def __repr__(self) -> str:
        return "ALLOW"


ALLOW = Allow()


@dataclass(frozen=True)
class Refusal:
    reason: RefusalReason
    detail: str = ""

    def __bool__(self) -> bool:  # a refusal is never truthy
        return False


@dataclass(frozen=True)
class ProposedOrder:
    """Exactly the fields that will go on the wire (the session builds the protobuf from this)."""

    order_id: int
    order_ref: str
    account: str
    con_id: int
    symbol: str
    sec_type: str
    currency: str
    action: str  # "BUY" | "SELL"
    quantity: Decimal
    order_type: str  # IBKR orderType: "LMT", "STP", ...
    limit_price: Decimal | None
    stop_price: Decimal | None
    tif: str
    outside_rth: bool
    parent_id: int = 0  # 0 = no parent
    transmit: bool = True
    oca_group: str = ""


@dataclass(frozen=True)
class WorkingOrder:
    """A non-terminal order on the account, as the broker (or this session) last reported it."""

    order_id: int
    con_id: int
    action: str
    remaining: Decimal
    parent_id: int = 0
    parent_working: bool = False  # the parent is itself still working (child not yet active)


@dataclass(frozen=True)
class ContractFacts:
    con_id: int
    symbol: str
    sec_type: str
    currency: str
    min_tick: Decimal | None
    time_zone_id: str
    liquid_hours: str


@dataclass(frozen=True)
class GuardContext:
    caps: ExperimentCaps
    submissions_used: int | None  # from the durable counter; None = unknown
    contract: ContractFacts  # what the session resolved for caps.symbol
    expected_account: str
    position: Decimal | None  # the broker's position read in contract.con_id; None = unknown
    working: Sequence[WorkingOrder]
    now: datetime
    # the position implied by the session's baseline read plus every fill it has received since
    # (executions and order statuses, the larger per order); None = unknown
    implied_position: Decimal | None
    # distinct orders of this experiment (orderRef prefix) the broker itself shows in open orders,
    # executions and completed orders, counted once per session before its first submission
    broker_submissions: int | None
    still_to_send: int = 0  # further placeOrder calls of the same bracket after this one
    parent: ProposedOrder | None = None  # set for a child of a bracket being placed now
    original: ProposedOrder | None = None  # set for a modification: the order as it stands


def _refuse(reason: RefusalReason, detail: str = "") -> Refusal:
    return Refusal(reason, detail)


def _on_tick(price: Decimal, tick: Decimal) -> bool:
    try:
        return (price / tick) == (price / tick).to_integral_value()
    except (InvalidOperation, ZeroDivisionError):
        return False


_SEGMENT = re.compile(r"^(\d{8}):(.*)$")
_RANGE = re.compile(r"^(?:(\d{8}):)?(\d{4})-(?:(\d{8}):)?(\d{4})$")


def parse_hours(text: str, time_zone_id: str) -> list[tuple[datetime, datetime]] | None:
    """IBKR ``liquidHours``/``tradingHours``. Both formats are accepted:
    ``20261111:0930-20261111:1600;20261112:CLOSED`` (current) and
    ``20261111:0930-1600,1700-1800;20261112:CLOSED`` (older). Returns aware intervals, or None when
    the text or the time zone cannot be understood (the guard then refuses). Only a REGION zone
    (``US/Eastern``, ``America/New_York``) is accepted: a fixed-offset or abbreviation zone
    (``EST``, ``EST5EDT``, ``UTC``, ``Etc/GMT+5``) would misplace the hours across daylight
    saving, so it is refused."""
    if (not isinstance(time_zone_id, str) or "/" not in time_zone_id
            or time_zone_id.split("/", 1)[0] in ("Etc", "SystemV")):
        return None
    try:
        tz = ZoneInfo(time_zone_id)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        return None
    out: list[tuple[datetime, datetime]] = []
    if not text or not isinstance(text, str):
        return None
    for seg in text.split(";"):
        seg = seg.strip()
        if not seg:
            continue
        m = _SEGMENT.match(seg)
        if m is None:
            return None
        day, rest = m.groups()
        if rest == "CLOSED":
            continue
        for part in rest.split(","):
            r = _RANGE.match(part.strip())  # "0930-20261111:1600" or "0930-1600"
            if r is None:
                return None
            d1, t1, d2, t2 = r.groups()
            try:
                start = datetime.strptime((d1 or day) + t1, "%Y%m%d%H%M").replace(tzinfo=tz)
                end = datetime.strptime((d2 or d1 or day) + t2, "%Y%m%d%H%M").replace(tzinfo=tz)
            except ValueError:
                return None
            if end <= start:  # older format can roll past midnight
                end += timedelta(days=1)
            out.append((start, end))
    return out


def _in_hours(now: datetime, facts: ContractFacts) -> Refusal | None:
    intervals = parse_hours(facts.liquid_hours, facts.time_zone_id)
    if intervals is None:
        return _refuse(RefusalReason.LIQUID_HOURS_UNKNOWN,
                       f"cannot parse liquid hours {facts.liquid_hours!r} in "
                       f"{facts.time_zone_id!r}")
    if not any(start <= now < end for start, end in intervals):
        return _refuse(RefusalReason.OUTSIDE_LIQUID_HOURS, f"{now.isoformat()} is outside the "
                       "contract's liquid hours as reported by the broker")
    return None


def _other_working_sells(ctx: GuardContext, order: ProposedOrder) -> Decimal:
    """Working sells in the contract other than this order. One-cancels-other siblings (children
    of the same parent) count once, at the larger remaining; this order's own siblings do not count
    against it (they are the same protection)."""
    groups: dict[object, Decimal] = {}
    for w in ctx.working:
        if w.order_id == order.order_id or w.action != "SELL" or w.con_id != order.con_id:
            continue
        if order.parent_id and w.parent_id == order.parent_id:
            continue  # an OCA sibling of this order
        key = ("p", w.parent_id) if w.parent_id else ("o", w.order_id)
        groups[key] = max(groups.get(key, Decimal(0)), w.remaining)
    return sum(groups.values(), Decimal(0))


def _working_buys(ctx: GuardContext, order: ProposedOrder) -> Decimal:
    return sum((w.remaining for w in ctx.working
                if w.order_id != order.order_id and w.action == "BUY"
                and w.con_id == order.con_id), Decimal(0))


def order_guard(order: ProposedOrder, ctx: GuardContext) -> Allow | Refusal:
    """ALLOW, or the first refusal found. Pure: no I/O, no clock, no globals."""
    caps, facts = ctx.caps, ctx.contract
    # --- the experiment's submission budget (every placeOrder counts) ---
    used = ctx.submissions_used
    if not isinstance(used, int) or isinstance(used, bool) or used < 0:
        return _refuse(RefusalReason.SUBMISSION_COUNT_UNKNOWN)
    broker = ctx.broker_submissions
    if not isinstance(broker, int) or isinstance(broker, bool) or broker < 0:
        return _refuse(RefusalReason.SUBMISSION_COUNT_UNKNOWN, "the broker's count is unknown")
    if broker > used:
        return _refuse(RefusalReason.SUBMISSION_COUNT_MISMATCH,
                       f"the broker shows {broker} orders of this experiment, the journal {used}")
    used = max(used, broker)
    if used >= caps.max_submissions:
        return _refuse(RefusalReason.EXPERIMENT_FINISHED,
                       f"{used} of {caps.max_submissions} submissions used")
    if used + 1 + max(ctx.still_to_send, 0) > caps.max_submissions:
        return _refuse(RefusalReason.SUBMISSION_CAP,
                       f"{used} used + {1 + ctx.still_to_send} needed > {caps.max_submissions}")
    # --- identity: account and contract ---
    if order.account != ctx.expected_account or not order.account:
        return _refuse(RefusalReason.WRONG_ACCOUNT)
    if not order.order_ref:
        return _refuse(RefusalReason.ORDER_REF_MISSING)
    if order.symbol != caps.symbol or facts.symbol != caps.symbol:
        return _refuse(RefusalReason.WRONG_SYMBOL, f"{order.symbol!r} is not {caps.symbol!r}")
    if order.sec_type != caps.sec_type or facts.sec_type != caps.sec_type:
        return _refuse(RefusalReason.WRONG_SEC_TYPE)
    if order.currency != caps.currency or facts.currency != caps.currency:
        return _refuse(RefusalReason.WRONG_CURRENCY)
    if not facts.con_id or order.con_id != facts.con_id:
        return _refuse(RefusalReason.WRONG_CONTRACT_ID,
                       f"{order.con_id} is not the session-resolved {facts.con_id}")
    # --- order shape ---
    if order.order_type == "MKT" or "MKT" in order.order_type.split():
        return _refuse(RefusalReason.MARKET_ORDER)
    if order.order_type not in caps.order_types:
        return _refuse(RefusalReason.ORDER_TYPE_NOT_ALLOWED, order.order_type)
    if order.tif not in caps.tifs:
        return _refuse(RefusalReason.TIF_NOT_ALLOWED, order.tif)
    if order.outside_rth is not False and not caps.outside_rth_allowed:
        return _refuse(RefusalReason.OUTSIDE_RTH_FLAG)
    if order.action not in ("BUY", "SELL"):
        return _refuse(RefusalReason.BAD_SIDE, order.action)
    qty = order.quantity
    if (not isinstance(qty, Decimal) or not qty.is_finite() or qty <= 0
            or qty != qty.to_integral_value()):
        return _refuse(RefusalReason.QUANTITY_INVALID, str(qty))
    if qty > caps.max_order_quantity:
        return _refuse(RefusalReason.ORDER_QUANTITY_CAP, f"{qty} > {caps.max_order_quantity}")
    if order.order_type == "LMT":
        price, other = order.limit_price, order.stop_price
    else:  # STP
        price, other = order.stop_price, order.limit_price
    if (other is not None or not isinstance(price, Decimal) or not price.is_finite()
            or price <= 0):
        return _refuse(RefusalReason.PRICE_INVALID, f"{order.order_type} needs exactly one "
                       "positive price of its own kind")
    if facts.min_tick is None or not facts.min_tick.is_finite() or facts.min_tick <= 0:
        return _refuse(RefusalReason.TICK_UNKNOWN)
    if not _on_tick(price, facts.min_tick):
        return _refuse(RefusalReason.OFF_TICK_PRICE, f"{price} is not a multiple of "
                       f"{facts.min_tick}; refused, never rounded")
    if qty * price > caps.max_order_notional:
        return _refuse(RefusalReason.NOTIONAL_CAP, f"{qty} x {price} > {caps.max_order_notional}")
    # --- the clock and the broker-reported liquid hours ---
    if ctx.now.tzinfo is None or ctx.now.utcoffset() is None:
        return _refuse(RefusalReason.CLOCK_NOT_AWARE)
    hours = _in_hours(ctx.now, facts)
    if hours is not None:
        return hours
    # --- a modification may change price or size only ---
    if ctx.original is not None:
        o = ctx.original
        same = replace(order, quantity=o.quantity, limit_price=o.limit_price,
                       stop_price=o.stop_price, transmit=o.transmit)
        if same != o:
            return _refuse(RefusalReason.MODIFICATION_INVALID,
                           "a modification may change only price or quantity")
    # --- position rules: long only, capped, never short ---
    pos, implied = ctx.position, ctx.implied_position
    if pos is None or not isinstance(pos, Decimal) or not pos.is_finite():
        return _refuse(RefusalReason.POSITION_UNKNOWN)
    if implied is None or not isinstance(implied, Decimal) or not implied.is_finite():
        return _refuse(RefusalReason.POSITION_UNKNOWN, "the session's own fills are unknown")
    if implied != pos:  # one of the two lags: refuse until they agree again
        return _refuse(RefusalReason.POSITION_DISAGREEMENT,
                       f"position read {pos} != {implied} implied by this session's own fills")
    # the more cautious of the two (equal here; kept so the rule survives a later relaxation)
    pos = max(pos, implied) if order.action == "BUY" else min(pos, implied)
    if caps.long_only and pos < 0:
        return _refuse(RefusalReason.SHORT_POSITION_PRESENT, str(pos))
    if order.action == "BUY":
        if order.parent_id or ctx.parent is not None:
            return _refuse(RefusalReason.CHILD_INVALID, "a child must be a protective SELL")
        if order.order_type != "LMT":
            return _refuse(RefusalReason.BUY_NOT_LIMIT, f"a BUY must be a limit order, not "
                           f"{order.order_type} (a stop becomes a market order when triggered)")
        projected = pos + _working_buys(ctx, order) + qty
        if projected > caps.max_net_position:
            return _refuse(RefusalReason.POSITION_CAP, f"{pos} held + working buys + {qty} > "
                           f"{caps.max_net_position}")
        return ALLOW
    # SELL
    parent = ctx.parent
    if ctx.parent is None and order.parent_id:
        parent_working = next((w for w in ctx.working if w.order_id == order.parent_id), None)
        if parent_working is not None and parent_working.action == "BUY":
            # an amendment of a child whose entry is still working: it may cover what is held
            # plus what its entry can still buy, net of other working sells
            capacity = max(pos, Decimal(0)) + parent_working.remaining - _other_working_sells(
                ctx, order)
            if qty > capacity:
                return _refuse(RefusalReason.SELL_EXCEEDS_POSITION,
                               f"child {qty} > held + working parent - working sells = {capacity}")
            return ALLOW
    if parent is not None:
        if (order.parent_id != parent.order_id or parent.action != "BUY"
                or parent.con_id != order.con_id or qty > parent.quantity):
            return _refuse(RefusalReason.CHILD_INVALID,
                           "a child must be a SELL no larger than its BUY parent in the contract")
        return ALLOW
    if pos <= 0:
        if order.order_type == "STP":
            return _refuse(RefusalReason.STOP_NOT_PROTECTIVE, "a stop must protect a bracketed "
                           "BUY or reduce a held long")
        return _refuse(RefusalReason.SHORT_SALE, "nothing long to sell")
    capacity = pos - _other_working_sells(ctx, order)
    if qty > capacity:
        return _refuse(RefusalReason.SELL_EXCEEDS_POSITION,
                       f"{qty} > {pos} held - working sells = {capacity}")
    return ALLOW
