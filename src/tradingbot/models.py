"""Canonical signal, policy and mock-execution contracts.

Prices are passed as decimal *strings* by the model so money is never
silently rounded by JSON float serialization.
"""
from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from enum import Enum
from typing import Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field


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
    # Multi-asset mapping (runtime/mapping.py, version 2). Additive: a stock or ETF is unchanged.
    CFD = "CFD"
    COMMODITY = "COMMODITY"  # a spot commodity (IBKR secType CMDTY)
    INDEX = "INDEX"  # a reference series only (IBKR secType IND): quoted, never ordered


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

    @property
    def quote_key(self) -> str:
        """The key a gateway quote is filed under. A stock or ETF keeps its symbol (unchanged); a
        derivative adds its contract month, expiry, strike and right, so two months of one future
        (or two strikes of one option) never share a quote."""
        if self.asset_class in (AssetClass.EQUITY, AssetClass.ETF):
            return self.symbol
        month = self.futures_contract or self.expiry or "-"
        return ":".join(
            (self.asset_class.value, self.symbol, month, self.strike or "-", self.option_right or "-")
        )


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
    published_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class Quote(StrictModel):
    symbol: str
    bid: Decimal
    ask: Decimal
    observed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class MockPolicy(StrictModel):
    """The ONE mock risk-parameter contract, shared by every engine.

    Both the Sprint 1 journal (storage.py) and the Sprint 1.1 replay coordinator
    (event_replay.py) read their limits from here through validator.py, so no engine
    carries its own defaults. All values are illustrative DEMO limits, NOT real trading
    settings. Sizing lives here and in deterministic code only; the LLM-facing schemas
    (SignalCandidate, ParsedPost/TradeAction) carry no quantity, venue or account field.
    The Sprint 1.1 spellings are accepted as validation aliases for existing callers.
    """
    quantity: int = Field(
        default=1, ge=1, le=100, validation_alias=AliasChoices("quantity", "quantity_per_open"),
    )
    quantity_per_add: int = Field(default=1, ge=1, le=100)
    max_price_deviation_pct: Decimal = Field(
        default=Decimal("1.0"), ge=0, le=100,
        validation_alias=AliasChoices("max_price_deviation_pct", "max_deviation_pct"),
    )
    max_spread_pct: Decimal = Field(default=Decimal("1.0"), ge=0, le=100)
    max_notional_usd: Decimal = Field(default=Decimal(2000), gt=0)
    max_signal_age_seconds: int = Field(
        default=90, gt=0, validation_alias=AliasChoices("max_signal_age_seconds", "max_source_age_seconds"),
    )
    max_quote_age_seconds: int = Field(default=30, gt=0)
    # Execution-core exposure caps across follower ideas (execution/coordinator.py), checked before
    # every broker entry or add. None for the per-instrument cap means "same as max_notional_usd".
    max_instrument_exposure_usd: Decimal | None = Field(default=None, gt=0)
    max_aggregate_exposure_usd: Decimal = Field(default=Decimal(10000), gt=0)
    require_stop_on_open: bool = True
    # None = no symbol allowlist (the demo grammar accepts any well-formed symbol);
    # a set restricts fictional fills to those symbols.
    allowed_symbols: frozenset[str] | None = None
    paper_only: Literal[True] = True
    allow_stop_widening: Literal[False] = False


# ----------------------------------------------------------------------------- multi-asset mapping
#
# What the gateway needs to decide an action in a mapped family (runtime/mapping.py, version 2):
# the owner-approved mapping terms, the selected execution contract and the broker's quotes of
# both the execution instrument and the reference series. Built by the runtime from the mapping
# file and broker reads, never by a model; handed to ``validator.gate_action`` as a ``MappedQuote``.

BasisTreatment = Literal["DIRECT", "BASIS_SHIFT", "REFERENCE_TRIGGER", "UNSUPPORTED"]


class LegQuote(StrictModel):
    """One broker quote (execution instrument or reference series) as the broker reported it."""
    contract_key: str
    bid: Decimal | None = None
    ask: Decimal | None = None
    last: Decimal | None = None
    observed_at: datetime
    quality: str  # execution.broker.QuoteQuality value: LIVE | DELAYED | FROZEN | UNAVAILABLE


class MarketTerms(StrictModel):
    """One mapping entry's terms, frozen for one decision, with the selected contract's rules."""
    family: str
    approved: bool
    basis: BasisTreatment
    execution_symbol: str
    execution_key: str  # Instrument.quote_key of the selected execution instrument
    execution_contract_key: str | None = None  # ContractRef.contract_key, once resolved
    asset_class: AssetClass
    multiplier: Decimal = Field(default=Decimal(1), gt=0)
    min_tick: Decimal = Field(default=Decimal("0.01"), gt=0)
    size_increment: int = Field(default=1, ge=1)
    units_per_signal: int = Field(ge=1)
    max_units: int = Field(ge=1)
    max_notional: Decimal = Field(gt=0)
    allow_short: bool = True
    # Session: weekdays (0 = Monday) and [start, end) minute windows in ``session_timezone``.
    session_timezone: str = "UTC"
    session_days: list[int] = Field(default_factory=lambda: [0, 1, 2, 3, 4])
    permitted_windows: list[tuple[int, int]] = Field(default_factory=list)
    # Roll: no new exposure within ``roll_business_days`` business days of ``roll_date``.
    roll_date: date | None = None
    roll_business_days: int | None = None
    holidays: list[date] = Field(default_factory=list)
    # Reference series and the basis rules (BASIS_SHIFT, and a REFERENCE_TRIGGER backstop).
    reference_contract_key: str | None = None
    reference_contract: str | None = None  # ContractRef JSON of the reference series
    reference_windows: list[tuple[int, int]] = Field(default_factory=list)
    max_abs_basis: Decimal | None = None
    max_basis_age_seconds: float | None = None
    require_backstop: bool = False
    released_order_type: Literal["MKT"] = "MKT"
    accepted_qualities: list[str] = Field(default_factory=lambda: ["LIVE"])
    # Problems found while building these terms (contract unresolved, specification differs).
    problems: list[str] = Field(default_factory=list)


class MappedQuote(Quote):
    """The gateway quote for an action in a mapped family: the execution instrument's quote (bid
    and ask are 0 when the broker had none; ``quality`` then says why), the reference series'
    quote and the terms. Filed under ``Instrument.quote_key``."""
    quality: str
    terms: MarketTerms
    reference: LegQuote | None = None


class ExecutionPlan(StrictModel):
    """What the gateway approved for a mapped action, in the series each price belongs to.

    ``execution_reference`` is the entry level in execution prices (the collar centre the
    execution core re-checks); ``stop`` and ``target`` are in ``protection_series``: EXECUTION
    prices for DIRECT and BASIS_SHIFT, REFERENCE levels for REFERENCE_TRIGGER (held as broker-side
    conditions). A BASIS_SHIFT conversion records the basis it used."""
    family: str
    basis: BasisTreatment
    execution_key: str
    quantity: int | None = None
    entry_reference: Decimal | None = None
    execution_reference: Decimal | None = None
    stop: Decimal | None = None
    target: Decimal | None = None
    protection_series: Literal["EXECUTION", "REFERENCE"] = "EXECUTION"
    reference_contract_key: str | None = None
    reference_contract: str | None = None
    backstop_stop: Decimal | None = None
    basis_value: Decimal | None = None
    basis_execution_mid: Decimal | None = None
    basis_reference_mid: Decimal | None = None
    basis_observed_at: datetime | None = None
    publisher_stop: Decimal | None = None
    publisher_target: Decimal | None = None


class GateDecision(StrictModel):
    """Canonical output of the deterministic authorization gateway (validator.py).

    APPROVE never means an order exists; an engine must still apply a fictional fill.
    The Sprint 1 pipeline sees this translated to the legacy Decision vocabulary.
    ``plan`` is set only on the approval of an action in a mapped family (multi-asset mapping).
    """
    status: Literal["APPROVE", "HOLD", "BLOCK", "NO_TRADE", "RECONCILE"]
    reasons: list[str] = Field(default_factory=list)
    plan: ExecutionPlan | None = None


class Decision(StrictModel):
    status: Literal["EXECUTE_MOCK", "NO_TRADE", "REVIEW", "BLOCK", "DUPLICATE"]
    reasons: list[str] = Field(default_factory=list)


class Position(StrictModel):
    idea_id: str
    symbol: str
    asset_class: AssetClass
    side: PositionSide
    quantity: int = Field(gt=0)
    # Protection levels known to the ledger (Sprint 1.1 ideas); None when the ledger does not track them.
    stop_price: str | None = None
    target_price: str | None = None
    # The idea's broker contract identity (ContractRef.contract_key) when the ledger knows it, so
    # two months of one future are never confused by a follow-up (multi-asset mapping).
    contract_key: str | None = None


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
