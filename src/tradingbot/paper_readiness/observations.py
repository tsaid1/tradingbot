"""Readiness-local, versioned observation records for the offline IBKR read-only scaffold (issue #16).

These records describe what a *read-only* broker session could report: a session/capability
attestation, contract metadata rows, one aggregate quote snapshot, an account-summary snapshot and
position lines. Their field names follow the documented TWS API callbacks (``managedAccounts``,
``contractDetails``, ``tickPrice``/``tickSize``/``marketDataType``/``tickSnapshotEnd``,
``accountSummary``/``accountSummaryEnd``, ``position``/``positionEnd``, ``error``) so that a future
connected adapter can populate them without inventing vocabulary. Nothing here is a trading
authorization contract: the shared ``models.py`` / ``validator.py`` contracts are unchanged and the
deterministic gateway still decides every action.

Every account identifier admitted by this scaffold must be an explicitly synthetic ``SYNTH-...``
value. Real account identifiers are refused by schema; this package has no credential loader and no
connected transport.
"""
from __future__ import annotations

import re
from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import Field, model_validator

from tradingbot.instrument_diagnostics import AUTHORITY
from tradingbot.models import AssetClass, Instrument, StrictModel
from tradingbot.validator import SYMBOL_RE

RECORDS_VERSION = 1
# Opt-in version 2: identity is held by a private per-run owner (private_account.py). Never mixed with 1.
RECORDS_VERSION_V2 = 2
__all__ = [
    "ACCOUNT_REF_PATTERN",
    "AUTHORITY",
    "DELAYED_TICK_TYPES",
    "END_KINDS",
    "MARKET_DATA_TYPE_CODES",
    "PAYLOAD_FIELD",
    "RECORDS_VERSION",
    "RECORDS_VERSION_V2",
    "STK_ASSET_CLASSES",
    "SYNTHETIC_ACCOUNT_RE",
    "AccountSummaryObservation",
    "ApiMessage",
    "ContractObservation",
    "Environment",
    "MarketDataType",
    "PositionLine",
    "QuoteObservation",
    "ReadinessExpectation",
    "ReadinessExpectationV2",
    "ResponseEnvelope",
    "ResponseKind",
    "SessionAttestation",
    "SessionAttestationV2",
    "TargetContract",
]

# Only explicitly synthetic account identifiers are admitted anywhere in this scaffold.
SYNTHETIC_ACCOUNT_RE = re.compile(r"^SYNTH-[A-Z0-9]{4,24}$")
# The fixed opaque reference format issued by account_ref.compare_account.
ACCOUNT_REF_PATTERN = r"^acctref-v1:[0-9a-f]{32}$"
# IBKR secType STK covers both equities and ETFs; the scaffold admits nothing else.
STK_ASSET_CLASSES = frozenset({AssetClass.EQUITY, AssetClass.ETF})

Environment = Literal["PAPER", "LIVE", "UNKNOWN"]


class MarketDataType(str, Enum):
    """Documented ``reqMarketDataType`` modes (1 live, 2 frozen, 3 delayed, 4 delayed-frozen)."""
    LIVE = "LIVE"
    FROZEN = "FROZEN"
    DELAYED = "DELAYED"
    DELAYED_FROZEN = "DELAYED_FROZEN"
    UNKNOWN = "UNKNOWN"


MARKET_DATA_TYPE_CODES = {1: MarketDataType.LIVE, 2: MarketDataType.FROZEN,
                          3: MarketDataType.DELAYED, 4: MarketDataType.DELAYED_FROZEN}
# Documented delayed tick types: 66 delayed bid, 67 delayed ask, 68 delayed last, 88 delayed last timestamp.
DELAYED_TICK_TYPES = frozenset({66, 67, 68, 88})


# ----------------------------------------------------------------------------- operator expectation

class TargetContract(StrictModel):
    """The explicit instrument the operator expects to observe. Direct identity only: a USD STK
    (equity/ETF) with a stated routing exchange and a stated primary listing. No proxy, derivative,
    alias or conversion is admitted; ``con_id`` may be pinned only after a connected check learned it."""
    instrument: Instrument
    sec_type: Literal["STK"] = "STK"
    currency: Literal["USD"] = "USD"
    primary_exchange: str = Field(min_length=1, max_length=40)
    con_id: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _direct_identity_stk_only(self):
        inst = self.instrument
        if inst.asset_class not in STK_ASSET_CLASSES:
            raise ValueError("TargetContract admits EQUITY/ETF (IBKR secType STK) only; no derivatives")
        if not SYMBOL_RE.fullmatch(inst.symbol):
            raise ValueError("TargetContract symbol must match the gateway symbol pattern")
        if inst.exchange is None or not inst.exchange.strip():
            raise ValueError("TargetContract must state the routing exchange explicitly")
        if not self.primary_exchange.strip():
            raise ValueError("TargetContract must state the primary listing explicitly")
        extras = [n for n in ("underlying", "expiry", "strike", "option_right", "futures_contract")
                  if getattr(inst, n) is not None]
        if extras:
            raise ValueError(f"TargetContract carries derivative fields {extras}; direct identity only")
        return self


class ReadinessExpectation(StrictModel):
    """What the operator explicitly expects of the session. The scaffold can express only a PAPER
    environment, a synthetic account id and the regular session; anything else is unrepresentable."""
    environment: Literal["PAPER"] = "PAPER"
    account_id: str = Field(pattern=SYNTHETIC_ACCOUNT_RE.pattern)
    session_scope: Literal["REGULAR"] = "REGULAR"
    target: TargetContract


# ----------------------------------------------------------------------------- session attestation

class ApiMessage(StrictModel):
    """A documented TWS API message *code* (``error`` callback). The message text is never retained,
    so no broker payload can reach a diagnostic. ``request_id`` is None for session-level messages."""
    code: int = Field(ge=0)
    request_id: int | None = None


class SessionAttestation(StrictModel):
    """What a transport reports about its own session plus operator-attested settings.

    ``environment_claim`` is an explicit statement, never derived from ``socket_port``: the documented
    default ports (7496/7497 TWS, 4001/4002 IB Gateway) are user-configurable conventions, so a port is
    recorded for diagnostics only. ``read_only_api_attested`` mirrors the TWS/IB Gateway "Read-Only API"
    setting, which is not queryable through the API and must therefore be attested by the operator.
    ``managed_accounts`` mirrors the ``managedAccounts`` callback. ``messages`` are session-level codes
    such as 2104/2158 (data farms OK) or 1100 (connectivity lost).
    """
    synthetic: Literal[True]
    connected: bool
    environment_claim: Environment = "UNKNOWN"
    managed_accounts: list[str] = Field(default_factory=list)
    read_only_api_attested: bool | None = None
    server_version: int | None = Field(default=None, gt=0)
    connection_time: datetime | None = None
    socket_port: int | None = Field(default=None, ge=1, le=65535)
    messages: list[ApiMessage] = Field(default_factory=list)


class ReadinessExpectationV2(StrictModel):
    """Version 2 expectation. It carries only the opaque reference of an owner-issued binding, never an
    account identifier. The reference is a label, not authority: the version 2 preflight also requires
    the issuing owner to confirm its own binding. PAPER and the regular session are still the only
    expressible scope."""
    records_version: Literal[2] = 2
    environment: Literal["PAPER"] = "PAPER"
    account_ref: str = Field(pattern=ACCOUNT_REF_PATTERN)
    session_scope: Literal["REGULAR"] = "REGULAR"
    target: TargetContract


class SessionAttestationV2(StrictModel):
    """Version 2 session attestation. Managed accounts are not carried here: they stay with the private
    owner, which reports only its comparison status. ``synthetic`` remains ``True``: every version 2
    session is an invented replay, and no external session is admitted."""
    records_version: Literal[2] = 2
    synthetic: Literal[True]
    connected: bool
    environment_claim: Environment = "UNKNOWN"
    read_only_api_attested: bool | None = None
    server_version: int | None = Field(default=None, gt=0)
    connection_time: datetime | None = None
    socket_port: int | None = Field(default=None, ge=1, le=65535)
    messages: list[ApiMessage] = Field(default_factory=list)


# ----------------------------------------------------------------------------- read-only observations

class ContractObservation(StrictModel):
    """One ``contractDetails`` row. All values are whatever the (fake) transport delivered."""
    con_id: int | None = Field(default=None, gt=0)
    symbol: str | None = None
    local_symbol: str | None = None
    sec_type: str | None = None
    currency: str | None = None
    exchange: str | None = None
    primary_exchange: str | None = None
    long_name: str | None = None
    trading_hours: str | None = None
    liquid_hours: str | None = None
    time_zone_id: str | None = None
    min_tick: str | None = None  # decimal string


class QuoteObservation(StrictModel):
    """One aggregate quote snapshot (``reqMktData`` with snapshot=true delivers aggregate ticks and a
    ``tickSnapshotEnd``). ``last_timestamp`` is the broker's tick 45/88; ``received_at`` is stamped by
    the transport's clock on delivery. Prices are decimal strings; nothing is converted or sized."""
    con_id: int | None = Field(default=None, gt=0)
    market_data_type: MarketDataType = MarketDataType.UNKNOWN
    bid: str | None = None
    ask: str | None = None
    last: str | None = None
    bid_size: int | None = Field(default=None, ge=0)
    ask_size: int | None = Field(default=None, ge=0)
    last_timestamp: datetime | None = None
    received_at: datetime | None = None
    tick_types_received: list[int] = Field(default_factory=list)
    halted: int | None = Field(default=None, ge=-1, le=2)  # documented tick 49: -1 unavailable, 0, 1, 2


class AccountSummaryObservation(StrictModel):
    """Aggregate of ``accountSummary`` rows for one account. ``values`` (tag -> decimal string) are
    held for completeness checks only and are NEVER rendered into a diagnostic."""
    account_id: str
    account_type: str | None = None
    currency: str | None = None
    values: dict[str, str] = Field(default_factory=dict)


class PositionLine(StrictModel):
    """One ``position`` callback row: account, contract identity, signed position, average cost."""
    account_id: str
    con_id: int | None = Field(default=None, gt=0)
    symbol: str | None = None
    sec_type: str | None = None
    currency: str | None = None
    position: str | None = None  # decimal string
    avg_cost: str | None = None


# ----------------------------------------------------------------------------- response correlation

class ResponseKind(str, Enum):
    CONTRACT_DETAILS = "CONTRACT_DETAILS"
    CONTRACT_DETAILS_END = "CONTRACT_DETAILS_END"
    MARKET_DATA_TYPE = "MARKET_DATA_TYPE"
    QUOTE = "QUOTE"
    TICK_SNAPSHOT_END = "TICK_SNAPSHOT_END"
    ACCOUNT_SUMMARY = "ACCOUNT_SUMMARY"
    ACCOUNT_SUMMARY_END = "ACCOUNT_SUMMARY_END"
    POSITION = "POSITION"
    POSITION_END = "POSITION_END"
    ERROR = "ERROR"


END_KINDS = frozenset({ResponseKind.CONTRACT_DETAILS_END, ResponseKind.TICK_SNAPSHOT_END,
                       ResponseKind.ACCOUNT_SUMMARY_END, ResponseKind.POSITION_END})
PAYLOAD_FIELD = {
    ResponseKind.CONTRACT_DETAILS: "contract", ResponseKind.MARKET_DATA_TYPE: "market_data_type",
    ResponseKind.QUOTE: "quote", ResponseKind.ACCOUNT_SUMMARY: "account_summary",
    ResponseKind.POSITION: "position", ResponseKind.ERROR: "message",
}


class ResponseEnvelope(StrictModel):
    """One correlated response: the request id it answers, its delivery sequence and exactly the
    payload its kind requires (none for END markers). Duplicates and out-of-order delivery are
    expressed by repeating or permuting sequences; the runner handles both deterministically."""
    request_id: int = Field(ge=0)
    sequence: int = Field(ge=0)
    kind: ResponseKind
    contract: ContractObservation | None = None
    market_data_type: MarketDataType | None = None
    quote: QuoteObservation | None = None
    account_summary: AccountSummaryObservation | None = None
    position: PositionLine | None = None
    message: ApiMessage | None = None

    @model_validator(mode="after")
    def _payload_matches_kind(self):
        expected = PAYLOAD_FIELD.get(self.kind)
        for name in PAYLOAD_FIELD.values():
            value = getattr(self, name)
            if name == expected and value is None:
                raise ValueError(f"{self.kind.value} envelope requires a '{name}' payload")
            if name != expected and value is not None:
                raise ValueError(f"{self.kind.value} envelope must not carry a '{name}' payload")
        return self
