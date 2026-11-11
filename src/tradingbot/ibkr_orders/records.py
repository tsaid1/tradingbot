"""Plain records and errors shared by the order session and ``IbkrPaperBroker`` (SDK-free)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from tradingbot.execution.broker import OrderStatus
from tradingbot.ibkr.errors import IbkrError, ReadOnlySettingRefused, ReadOnlyViolation
from tradingbot.ibkr_orders.guard import Refusal


@dataclass(frozen=True)
class OrderSpec:
    """One order as the broker adapter asks the session to place it."""

    order_ref: str
    action: str  # BUY | SELL
    quantity: int
    order_type: str  # LMT | STP (anything else is refused by the guard)
    limit_price: Decimal | None = None
    stop_price: Decimal | None = None
    tif: str = "DAY"


@dataclass(frozen=True)
class OrderSnapshot:
    order_id: int
    client_id: int
    perm_id: int
    order_ref: str
    parent_id: int
    parent_ref: str | None
    con_id: int
    symbol: str
    sec_type: str
    currency: str
    action: str
    total_quantity: Decimal
    order_type: str
    limit_price: Decimal | None
    stop_price: Decimal | None
    tif: str
    status: OrderStatus
    raw_status: str
    filled: Decimal
    remaining: Decimal
    avg_fill_price: Decimal | None
    reason: str
    updated_at: datetime
    own: bool  # placed by this API client id


@dataclass(frozen=True)
class ExecRecord:
    exec_id: str
    order_id: int
    client_id: int
    perm_id: int
    order_ref: str
    con_id: int
    symbol: str
    sec_type: str
    currency: str
    side: str  # BUY | SELL, mapped from the broker's BOT | SLD; anything else kept raw
    shares: Decimal
    price: Decimal
    time: str  # the broker's execution time string, unparsed
    received_at: datetime


# --- errors --------------------------------------------------------------------------------------
class OrderModeViolation(ReadOnlyViolation):
    """An order-mode wire rule was broken (no permit, wrong id, text-framed order...). A
    BaseException like ReadOnlyViolation, so the SDK's ``except Exception`` cannot swallow it."""


class OrderModeSettingRefused(ReadOnlySettingRefused):
    """In order mode TWS must report Read-Only API explicitly OFF, localhost-only ON and the
    configured socket port."""


class ExperimentContractMismatch(IbkrError):
    pass


class OrderRefused(IbkrError):
    """The guard refused: NOTHING was sent for this request (definite outcome)."""

    def __init__(self, refusal: Refusal) -> None:
        super().__init__(f"order refused by the pre-trade guard: {refusal.reason.value}"
                         + (f" ({refusal.detail})" if refusal.detail else ""))
        self.refusal = refusal


class OrderOutcomeUnknown(IbkrError):
    """The request was (or may have been) sent and no acknowledgement arrived: never retried."""

    def __init__(self, message: str, *, orders: tuple = ()) -> None:
        super().__init__(message)
        self.orders = orders


class PartialBracket(OrderOutcomeUnknown):
    """Part of a bracket was accepted and part was not (rejected, unacknowledged or never sent).
    The session has asked the broker to cancel the parent; the outcome is unknown until looked up."""


class OrderNotWorking(IbkrError):
    """No working order of this client with that id."""


class AmendRejected(IbkrError):
    """The broker answered an amendment with an error; the original order stands unchanged."""


class CancelFailed(IbkrError):
    """The broker refused the cancel and the order is not terminal."""
