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
from typing import Literal, Protocol, runtime_checkable

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
    # A cancel was requested and the broker has not confirmed it: the order can still fill.
    PENDING_CANCEL = "PendingCancel"
    FILLED = "Filled"
    CANCELLED = "Cancelled"
    API_CANCELLED = "ApiCancelled"  # cancelled through the API before the exchange acknowledged it
    REJECTED = "Rejected"
    INACTIVE = "Inactive"
    UNKNOWN = "Unknown"


WORKING_STATUSES = frozenset(
    {
        OrderStatus.PENDING_SUBMIT,
        OrderStatus.PRE_SUBMITTED,
        OrderStatus.SUBMITTED,
        OrderStatus.PARTIALLY_FILLED,
        OrderStatus.PENDING_CANCEL,
    }
)
TERMINAL_STATUSES = frozenset(
    {
        OrderStatus.FILLED,
        OrderStatus.CANCELLED,
        OrderStatus.API_CANCELLED,
        OrderStatus.REJECTED,
        OrderStatus.INACTIVE,
    }
)


class ContractRef(StrictModel):
    """A broker-resolved, orderable contract. ``con_id`` is IBKR's contract id when known.

    The execution core identifies an instrument by ``contract_key`` (never by symbol alone): two
    months of one future, or a stock and a future sharing a symbol, are different instruments.
    The fields after ``con_id`` are optional with defaults that describe a stock, so a stock's
    behaviour is unchanged: notional = quantity x price x ``multiplier``; limit and stop prices
    must be multiples of ``min_tick`` and quantities multiples of ``size_increment``.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    symbol: str = Field(min_length=1, max_length=40)
    asset_class: AssetClass
    sec_type: str = "STK"
    exchange: str = "SMART"
    currency: str = "USD"
    primary_exchange: str | None = None
    con_id: int | None = None
    # Derivatives: expiry or contract month (IBKR lastTradeDateOrContractMonth, YYYYMM[DD]),
    # strike and right (options). None for a stock or ETF.
    expiry: str | None = Field(default=None, pattern=r"^\d{6}(\d{2})?$")
    strike: Decimal | None = Field(default=None, gt=0)
    right: Literal["C", "P"] | None = None
    local_symbol: str | None = None
    multiplier: Decimal = Field(default=Decimal(1), gt=0)
    min_tick: Decimal = Field(default=Decimal("0.01"), gt=0)
    size_increment: int = Field(default=1, ge=1)

    @property
    def contract_key(self) -> str:
        """Instrument identity: security type, symbol, expiry, strike, right and contract id."""
        strike = format(self.strike.normalize(), "f") if self.strike is not None else "-"
        parts = (self.sec_type, self.symbol, self.expiry or "-", strike, self.right or "-")
        return ":".join((*parts, str(self.con_id) if self.con_id is not None else "-"))

    def on_tick(self, price: Decimal) -> bool:
        return price > 0 and price % self.min_tick == 0

    def on_increment(self, quantity: int) -> bool:
        return quantity > 0 and quantity % self.size_increment == 0


class BrokerQuote(StrictModel):
    contract: ContractRef
    bid: Decimal | None = None
    ask: Decimal | None = None
    last: Decimal | None = None
    received_at: datetime
    quality: QuoteQuality


class ConditionDirection(str, Enum):
    ABOVE = "ABOVE"  # released when the reference trades at or above the trigger
    BELOW = "BELOW"  # released when the reference trades at or below the trigger


class PriceCondition(StrictModel):
    """A broker-side price condition on a REFERENCE series (IBKR PriceCondition: conId, exchange,
    isMore, price). The order it is attached to is held at the broker and released, as
    ``released_order_type`` in the order's own (execution) contract, once the reference's last
    price crosses ``trigger_price``. Additive (multi-asset mapping): an order without a condition
    behaves exactly as before. A broker that does not implement conditions must refuse such an
    order; the execution core checks ``supports_price_conditions`` before sending one."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    contract: ContractRef
    direction: ConditionDirection
    trigger_price: Decimal = Field(gt=0)
    released_order_type: OrderType = OrderType.MKT

    def met(self, last: Decimal | None) -> bool:
        if last is None:
            return False
        if self.direction == ConditionDirection.ABOVE:
            return last >= self.trigger_price
        return last <= self.trigger_price


class OrderIntent(StrictModel):
    """One order as the caller wants it placed. ``order_ref`` is the idempotency key: the caller
    derives it deterministically and the broker stores and echoes it (IBKR ``Order.orderRef``).
    ``condition`` (optional, additive) holds the order at the broker until a reference series
    crosses a level; ``order_type`` must then equal the condition's ``released_order_type``."""

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
    condition: PriceCondition | None = None

    @model_validator(mode="after")
    def _prices_match_type(self):
        if self.condition is not None and self.order_type != self.condition.released_order_type:
            raise ValueError("a conditional order's type must be its condition's released type")
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
    # The broker-side price condition the order is held on, as the broker reports it (additive).
    condition: PriceCondition | None = None
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
    """Net position per contract; quantity is signed (short < 0). ``avg_cost`` is the average price
    per unit of quantity, before the contract multiplier (IBKR reports avgCost multiplied for
    derivatives: the adapter divides it by the multiplier)."""

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
class ConditionalOrders(Protocol):
    """Optional broker capabilities for REFERENCE_TRIGGER families (multi-asset mapping). Kept out
    of ``Broker`` so an implementation without them still satisfies the protocol; the helpers
    below give each one a safe default (no conditions, no reference quote)."""

    def supports_price_conditions(self) -> bool: ...

    def reference_quote(self, contract: ContractRef) -> BrokerQuote: ...

    def modify_condition(self, order_ref: str, *, trigger_price: Decimal) -> BrokerOrder: ...


def supports_price_conditions(broker) -> bool:
    """True only when the broker says it places, reports and amends price conditions."""
    probe = getattr(broker, "supports_price_conditions", None)
    try:
        return bool(probe()) if callable(probe) else False
    except BrokerError:
        return False


def reference_quote(broker, contract: ContractRef, now: datetime) -> BrokerQuote:
    """The broker's quote of a reference series; UNAVAILABLE when it cannot supply one."""
    get = getattr(broker, "reference_quote", None)
    if not callable(get):
        return BrokerQuote(contract=contract, received_at=now, quality=QuoteQuality.UNAVAILABLE)
    try:
        return get(contract)
    except BrokerError:
        return BrokerQuote(contract=contract, received_at=now, quality=QuoteQuality.UNAVAILABLE)


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
