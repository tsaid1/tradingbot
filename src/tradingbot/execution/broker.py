"""The broker abstraction the execution core is written against.

Shaped so Interactive Brokers maps onto it directly: a resolved contract (conId), a quote with a
market-data quality (IBKR market data types 1-4), orders identified by a caller-supplied
``orderRef`` that the broker echoes on every order, execution and open-order row, brackets
submitted atomically (parent ``transmit=False``, the last child ``transmit=True``), and IBKR's
order-status vocabulary.

Nothing here sizes, authorizes or decides. Every ``OrderIntent`` is built by the coordinator from an
action the deterministic gateway (``validator.py``) approved.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Protocol, runtime_checkable

from pydantic import ConfigDict, Field, model_validator

from tradingbot.models import AssetClass, Instrument, StrictModel


class QuoteQuality(str, Enum):
    LIVE = "LIVE"  # IBKR market data type 1
    DELAYED = "DELAYED"  # type 3 / 4
    FROZEN = "FROZEN"  # type 2: last value at the close
    UNAVAILABLE = "UNAVAILABLE"  # no subscription, no data, or the request failed


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    MKT = "MKT"
    LMT = "LMT"
    STP = "STP"


class TimeInForce(str, Enum):
    DAY = "DAY"
    GTC = "GTC"


class OrderStatus(str, Enum):
    """IBKR order states, plus UNKNOWN for "we cannot tell" (never inferred as success)."""

    PENDING_SUBMIT = "PendingSubmit"
    PRE_SUBMITTED = "PreSubmitted"  # accepted, held (e.g. a bracket child awaiting its parent)
    SUBMITTED = "Submitted"
    PARTIALLY_FILLED = "PartiallyFilled"
    FILLED = "Filled"
    CANCELLED = "Cancelled"
    REJECTED = "Rejected"
    INACTIVE = "Inactive"
    UNKNOWN = "Unknown"


WORKING_STATUSES = frozenset(
    {
        OrderStatus.PENDING_SUBMIT,
        OrderStatus.PRE_SUBMITTED,
        OrderStatus.SUBMITTED,
        OrderStatus.PARTIALLY_FILLED,
    }
)
TERMINAL_STATUSES = frozenset(
    {
        OrderStatus.FILLED,
        OrderStatus.CANCELLED,
        OrderStatus.REJECTED,
        OrderStatus.INACTIVE,
    }
)


class ContractRef(StrictModel):
    """A broker-resolved, orderable contract. ``con_id`` is IBKR's contract id when known."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    symbol: str = Field(min_length=1, max_length=40)
    asset_class: AssetClass
    sec_type: str = "STK"
    exchange: str = "SMART"
    currency: str = "USD"
    primary_exchange: str | None = None
    con_id: int | None = None


class BrokerQuote(StrictModel):
    contract: ContractRef
    bid: Decimal | None = None
    ask: Decimal | None = None
    last: Decimal | None = None
    received_at: datetime
    quality: QuoteQuality


class OrderIntent(StrictModel):
    """One order as the caller wants it placed. ``order_ref`` is the idempotency key: the caller
    derives it deterministically and the broker stores and echoes it (IBKR ``Order.orderRef``)."""

    order_ref: str = Field(min_length=1, max_length=64)
    contract: ContractRef
    side: OrderSide
    quantity: int = Field(gt=0)
    order_type: OrderType
    limit_price: Decimal | None = None
    stop_price: Decimal | None = None
    tif: TimeInForce = TimeInForce.DAY
    # Set on a protective child: the order_ref of the entry it is attached to (IBKR parentId).
    parent_ref: str | None = None
    # May only reduce an existing position, never open or reverse one.
    reduce_only: bool = False

    @model_validator(mode="after")
    def _prices_match_type(self):
        if self.order_type == OrderType.LMT and (self.limit_price is None or self.limit_price <= 0):
            raise ValueError("LMT order needs a positive limit_price")
        if self.order_type == OrderType.STP and (self.stop_price is None or self.stop_price <= 0):
            raise ValueError("STP order needs a positive stop_price")
        if self.order_type == OrderType.MKT and (self.limit_price or self.stop_price):
            raise ValueError("MKT order carries no price")
        return self


class BrokerOrder(StrictModel):
    """The broker's view of one order (IBKR openOrder + orderStatus)."""

    broker_order_id: str
    order_ref: str
    parent_ref: str | None = None
    contract: ContractRef
    side: OrderSide
    quantity: int = Field(ge=0)
    order_type: OrderType
    limit_price: Decimal | None = None
    stop_price: Decimal | None = None
    tif: TimeInForce = TimeInForce.DAY
    reduce_only: bool = False
    status: OrderStatus
    filled_quantity: int = Field(default=0, ge=0)
    avg_fill_price: Decimal | None = None
    reason: str = ""
    updated_at: datetime

    @property
    def remaining(self) -> int:
        return max(self.quantity - self.filled_quantity, 0)

    @property
    def working(self) -> bool:
        return self.status in WORKING_STATUSES


class Fill(StrictModel):
    """One execution (IBKR execDetails). ``exec_id`` is unique per execution."""

    exec_id: str
    broker_order_id: str
    order_ref: str
    contract: ContractRef
    side: OrderSide
    quantity: int = Field(gt=0)
    price: Decimal
    executed_at: datetime


class BrokerPosition(StrictModel):
    """Net position per contract; quantity is signed (short < 0)."""

    account: str
    contract: ContractRef
    quantity: int
    avg_cost: Decimal | None = None


class BrokerError(RuntimeError):
    """The broker call failed. Whether it had any effect is stated by the subclass."""


class AckLost(BrokerError):
    """The request may or may not have reached the broker; its outcome is unknown.
    The caller must look the order up by ``order_ref`` before doing anything else."""


class OrderNotFound(BrokerError):
    """No working order with that order_ref (it is unknown or already terminal)."""


@runtime_checkable
class Broker(Protocol):
    def account_id(self) -> str: ...

    def resolve(self, instrument: Instrument) -> ContractRef: ...

    def quote(self, contract: ContractRef) -> BrokerQuote: ...

    def submit(self, order: OrderIntent, children: Sequence[OrderIntent] = ()) -> list[BrokerOrder]:
        """Place ``order`` and its protective ``children`` as one atomic bracket. Returns the broker
        view of every order placed, entry first. Raises AckLost when the outcome is unknown."""
        ...

    def modify(
        self,
        order_ref: str,
        *,
        limit_price: Decimal | None = None,
        stop_price: Decimal | None = None,
        quantity: int | None = None,
    ) -> BrokerOrder:
        """Amend a working order's price or size. Raises OrderNotFound if it is not working."""
        ...

    def cancel(self, order_ref: str) -> BrokerOrder: ...

    def open_orders(self) -> list[BrokerOrder]:
        """Every working order on the account (IBKR reqOpenOrders / reqAllOpenOrders)."""
        ...

    def executions(self, since: datetime | None = None) -> list[Fill]: ...

    def completed_orders(self, since: datetime | None = None) -> list[BrokerOrder]:
        """Every order that reached a terminal state (Filled, Cancelled, Rejected, Inactive) and was
        last updated at or after ``since``, with its ``order_ref`` and final status (IBKR
        reqCompletedOrders). This is the only place a rejected or cancelled order is visible once it
        has left ``open_orders()``."""
        ...

    def positions(self) -> list[BrokerPosition]: ...
