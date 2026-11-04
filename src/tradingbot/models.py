"""Canonical signal, policy and mock-execution contracts.

Prices are passed as decimal *strings* by the model so money is never
silently rounded by JSON float serialization.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Action(str, Enum):
    NO_TRADE = "NO_TRADE"
    OPEN = "OPEN"
    ADD = "ADD"
    REDUCE = "REDUCE"
    CLOSE = "CLOSE"
    MODIFY = "MODIFY"


class AssetClass(str, Enum):
    EQUITY = "EQUITY"
    ETF = "ETF"
    OPTION = "OPTION"
    FUTURE = "FUTURE"


class PositionSide(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"


class Instrument(StrictModel):
    asset_class: AssetClass
    symbol: str = Field(min_length=1, max_length=40)
    underlying: str | None = None
    expiry: str | None = None  # ISO YYYY-MM-DD; mandatory for options
    strike: str | None = None  # decimal string
    option_right: Literal["CALL", "PUT"] | None = None
    exchange: str | None = None
    futures_contract: str | None = None


class SignalCandidate(StrictModel):
    action: Action = Action.NO_TRADE
    instrument: Instrument | None = None
    side: PositionSide | None = None
    entry_reference: str | None = None
    stop_price: str | None = None
    target_price: str | None = None
    close_fraction: str | None = None  # decimal between 0 and 1
    idea_reference: str | None = None   # prior source_id / idea_id
    conditional: bool = False
    explicit_instruction: bool = False
    evidence: list[str] = Field(default_factory=list)
    ambiguities: list[str] = Field(default_factory=list)
    reason: str = ""


class SourceEvent(StrictModel):
    source_id: str = Field(min_length=1, max_length=128)
    source_type: Literal["manual_demo", "public_feed", "protected_feed"] = "manual_demo"
    provider: str = "synthetic_test"
    text: str = Field(min_length=1, max_length=20000)
    published_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class Quote(StrictModel):
    symbol: str
    bid: Decimal
    ask: Decimal
    observed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class MockPolicy(StrictModel):
    # All values are illustrative DEMO limits, NOT real trading settings.
    quantity: int = Field(default=1, ge=1, le=100)
    max_price_deviation_pct: Decimal = Field(default=Decimal("1.0"), ge=0, le=100)
    max_spread_pct: Decimal = Field(default=Decimal("1.0"), ge=0, le=100)
    max_notional_usd: Decimal = Field(default=Decimal("2000"), gt=0)
    max_signal_age_seconds: int = Field(default=90, gt=0)
    max_quote_age_seconds: int = Field(default=30, gt=0)
    paper_only: Literal[True] = True


class Decision(StrictModel):
    status: Literal["EXECUTE_MOCK", "NO_TRADE", "REVIEW", "BLOCK", "DUPLICATE"]
    reasons: list[str] = Field(default_factory=list)


class Position(StrictModel):
    idea_id: str
    symbol: str
    asset_class: AssetClass
    side: PositionSide
    quantity: int = Field(gt=0)


class MockOrder(StrictModel):
    order_id: str
    source_id: str
    idea_id: str
    action: Action
    symbol: str
    asset_class: AssetClass
    side: Literal["BUY", "SELL"]
    quantity: int = Field(gt=0)
    mock_fill_price: str
    stop_price: str | None = None
    target_price: str | None = None
    status: Literal["SIMULATED_FILLED"] = "SIMULATED_FILLED"


class ProcessResult(StrictModel):
    source_id: str
    candidate: SignalCandidate | None = None
    decision: Decision
    order: MockOrder | None = None
