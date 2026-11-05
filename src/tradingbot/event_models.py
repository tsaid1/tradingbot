"""Versioned contracts for zero-to-many actions extracted from a single source post.

The classifier is deliberately separate from source authentication and execution.
Source identity, completeness, timestamps, revisions and repost status must come
from the ingest adapter, never from model output. Paid post text is NOT stored here.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Literal

from pydantic import Field, model_validator

from tradingbot.models import Action, Instrument, PositionSide, StrictModel


class Timing(str, Enum):
    CURRENT = "CURRENT"
    HISTORICAL = "HISTORICAL"
    CONDITIONAL = "CONDITIONAL"
    COMMENTARY = "COMMENTARY"
    UNCERTAIN = "UNCERTAIN"


class PriceBasis(str, Enum):
    # Refers to bid/ask of the actual orderable instrument, e.g. TLT.
    TRADED_CONTRACT = "TRADED_CONTRACT"
    # Refers to spot/index/CFD level that cannot be sent to a different security.
    SOURCE_SERIES = "SOURCE_SERIES"
    UNKNOWN = "UNKNOWN"


class TradeAction(StrictModel):
    action_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,48}$")
    action: Action
    timing: Timing
    instrument: Instrument | None = None
    position_side: PositionSide | None = None
    idea_reference: str | None = None
    atomic_group: str | None = None
    price_basis: PriceBasis = PriceBasis.UNKNOWN
    source_market_symbol: str | None = None
    entry_reference: str | None = None
    stop_price: str | None = None
    target_price: str | None = None
    close_fraction: str | None = None
    explicit_instruction: bool = False
    evidence: list[str] = Field(default_factory=list)
    ambiguities: list[str] = Field(default_factory=list)
    reason: str = ""


class ParsedPost(StrictModel):
    """Model-generated semantic content; has no authority over source provenance."""
    schema_version: Literal["1.1"] = "1.1"
    actions: list[TradeAction] = Field(default_factory=list, max_length=25)
    summary: str = ""

    @model_validator(mode="after")
    def unique_child_ids(self):
        ids = [a.action_id for a in self.actions]
        if len(ids) != len(set(ids)):
            raise ValueError("Duplicate action_id in post")
        return self


class ReplaySource(StrictModel):
    """Trusted metadata supplied by source adapter, not by the LLM.

    No source text is retained by this contract. The opaque post_hash identifies
    source bytes held in a private, ignored local dataset.
    """
    source_id: str = Field(min_length=1, max_length=128)
    revision: int = Field(default=1, ge=1)
    published_at: datetime
    received_at: datetime
    verified_author: bool = False
    complete_text: bool = False
    is_repost: bool = False
    post_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class ReplayPolicy(StrictModel):
    """Fictional paper-only limits, never a substitute for broker policy."""
    paper_only: Literal[True] = True
    allowed_symbols: set[str] = Field(default_factory=lambda: {"TLT"})
    quantity_per_open: int = Field(default=2, ge=1, le=100)
    quantity_per_add: int = Field(default=1, ge=1, le=100)
    max_notional_usd: Decimal = Field(default=Decimal("20000"), gt=0)
    max_deviation_pct: Decimal = Field(default=Decimal("1.0"), ge=0, le=20)
    max_spread_pct: Decimal = Field(default=Decimal("1.0"), ge=0, le=20)
    max_source_age_seconds: int = Field(default=90, ge=1)
    max_quote_age_seconds: int = Field(default=30, ge=1)
    require_stop_on_open: bool = True
    allow_stop_widening: Literal[False] = False


class PaperOrder(StrictModel):
    order_id: str
    idea_id: str
    action_id: str
    action: Action
    symbol: str
    side: Literal["BUY", "SELL", "NONE"]
    quantity: int = Field(ge=0)
    price: str | None = None
    status: Literal["SIMULATED_FILLED", "SIMULATED_AMENDED"]


class ActionOutcome(StrictModel):
    action_id: str
    status: Literal["SIMULATED", "HOLD", "BLOCK", "NO_TRADE", "RECONCILE"]
    reasons: list[str] = Field(default_factory=list)
    order: PaperOrder | None = None


class ReplayResult(StrictModel):
    source_id: str
    revision: int
    duplicate: bool = False
    outcomes: list[ActionOutcome] = Field(default_factory=list)
