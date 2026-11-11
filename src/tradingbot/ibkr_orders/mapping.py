"""IBKR order-status vocabulary -> the execution core's ``OrderStatus`` (SDK-free).

Status strings as documented in ibapi 1052.1 ``EWrapper.orderStatus``: PendingSubmit, PendingCancel
(both normally set by the API client itself), PreSubmitted, Submitted, Cancelled, Filled, Inactive;
plus ApiPending and ApiCancelled from the TWS API documentation. Rejection is not a status: it
arrives through the ``error`` callback for the order id (see ``classify_order_error``).
"""
from __future__ import annotations

from decimal import Decimal

from tradingbot.execution.broker import TERMINAL_STATUSES, OrderStatus

_BY_NAME = {
    "ApiPending": OrderStatus.PENDING_SUBMIT,
    "PendingSubmit": OrderStatus.PENDING_SUBMIT,
    "PreSubmitted": OrderStatus.PRE_SUBMITTED,
    "Submitted": OrderStatus.SUBMITTED,
    "PendingCancel": OrderStatus.PENDING_CANCEL,
    "Filled": OrderStatus.FILLED,
    "Cancelled": OrderStatus.CANCELLED,
    "ApiCancelled": OrderStatus.API_CANCELLED,
    "Inactive": OrderStatus.INACTIVE,
}


def map_status(status: str | None, filled: Decimal | None = None,
               remaining: Decimal | None = None) -> OrderStatus:
    """Unknown strings map to UNKNOWN (never guessed). A working status with a fill and something
    remaining is PARTIALLY_FILLED."""
    mapped = _BY_NAME.get(status or "", OrderStatus.UNKNOWN)
    if (mapped in (OrderStatus.SUBMITTED, OrderStatus.PRE_SUBMITTED) and filled is not None
            and remaining is not None and filled > 0 and remaining > 0):
        return OrderStatus.PARTIALLY_FILLED
    return mapped


def merge_status(current: OrderStatus | None, new: OrderStatus, *, filled: Decimal,
                 total: Decimal) -> OrderStatus:
    """Tolerate duplicate and out-of-order callbacks: a terminal status is sticky (a late
    'Submitted' after 'Filled' is ignored), except that FILLED replaces another terminal state
    when everything is in fact filled. UNKNOWN never overwrites a known state."""
    if current is None:
        return new
    if new == OrderStatus.UNKNOWN:
        return current
    if current in TERMINAL_STATUSES:
        if new == OrderStatus.FILLED and current != OrderStatus.FILLED and filled >= total > 0:
            return OrderStatus.FILLED
        return current
    return new


# --- the error callback for an order id -----------------------------------------------------------
ORDER_CANCELLED_CODE = 202  # "Order Canceled - reason: ..." (the confirmation of a cancel)
CANCEL_FAILED_CODES = frozenset({161, 10147, 10148})  # not cancellable / not found / cannot cancel
SEND_FAILED_CODES = frozenset({512})  # ibapi FAIL_SEND_ORDER: the SDK's send raised (outcome unknown)
ORDER_NOTICE_CODES = frozenset({399, 10167})  # order warnings; the order may well be working


def classify_order_error(code: int) -> str:
    """NOTICE | CANCELLED | CANCEL_FAILED | SEND_FAILED | REJECTED."""
    if code in ORDER_NOTICE_CODES or 2100 <= code <= 2199:
        return "NOTICE"
    if code == ORDER_CANCELLED_CODE:
        return "CANCELLED"
    if code in CANCEL_FAILED_CODES:
        return "CANCEL_FAILED"
    if code in SEND_FAILED_CODES:
        return "SEND_FAILED"
    return "REJECTED"
