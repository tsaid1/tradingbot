"""Result models and the pure quote-quality assessment (SDK-free)."""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum


@dataclass(frozen=True)
class ContractQuery:
    symbol: str
    sec_type: str = "STK"
    exchange: str = "SMART"
    currency: str = "USD"
    primary_exchange: str = ""


TLT_QUERY = ContractQuery(symbol="TLT", sec_type="STK", exchange="SMART", currency="USD",
                          primary_exchange="NASDAQ")


@dataclass(frozen=True)
class ResolvedContract:
    con_id: int
    symbol: str
    sec_type: str
    currency: str
    exchange: str
    primary_exchange: str
    local_symbol: str
    min_tick: float
    time_zone_id: str
    trading_hours: str
    liquid_hours: str


@dataclass(frozen=True)
class AccountValue:
    value: str
    currency: str


@dataclass(frozen=True)
class AccountSummary:
    account_masked: str
    values: dict[str, AccountValue]
    missing_tags: tuple[str, ...]


@dataclass(frozen=True)
class PositionRow:
    account_masked: str
    con_id: int
    symbol: str
    sec_type: str
    currency: str
    exchange: str
    quantity: Decimal
    avg_cost: float


class QuoteQuality(str, Enum):
    LIVE = "LIVE"
    DELAYED = "DELAYED"
    FROZEN = "FROZEN"
    UNAVAILABLE = "UNAVAILABLE"


# reqMarketDataType / marketDataType values (TWS API documentation).
MARKET_DATA_TYPE_NAMES = {1: "LIVE", 2: "FROZEN", 3: "DELAYED", 4: "DELAYED_FROZEN"}

# Tick-type IDs as in ibapi.ticktype.TickTypeEnum (asserted against the real SDK in the SDK tests).
# value: (field, is_delayed_tick_id)
PRICE_TICKS = {1: ("bid", False), 2: ("ask", False), 4: ("last", False),
               66: ("bid", True), 67: ("ask", True), 68: ("last", True)}
SIZE_TICKS = {0: ("bid", False), 3: ("ask", False), 5: ("last", False),
              69: ("bid", True), 70: ("ask", True), 71: ("last", True)}
LAST_TIMESTAMP_TICKS = {45: False, 88: True}  # tickString, unix seconds of the last trade
CLOSE_TICKS = {9: False, 75: True}  # CLOSE, DELAYED_CLOSE (prior close; never a bid/ask)
DELAYED_TICK_IDS = frozenset({66, 67, 68, 69, 70, 71, 72, 73, 74, 75, 76, 88, 90})


@dataclass(frozen=True)
class QuoteSnapshot:
    con_id: int
    symbol: str
    bid: float | None
    bid_size: Decimal | None
    ask: float | None
    ask_size: Decimal | None
    last: float | None
    last_size: Decimal | None
    market_data_type: int | None  # as delivered by the broker's marketDataType callback
    market_data_type_name: str
    requested_market_data_type: int
    received_at: str  # local UTC receipt time of the snapshot; the only time we have for bid/ask
    last_trade_time: str | None  # exchange last-trade time (tick 45/88); dates the LAST only
    quality: QuoteQuality
    flags: tuple[str, ...]
    notices: tuple[int, ...] = ()
    unavailable_code: int | None = None
    # --- evidence (kept even when the request did not end) ---
    route: str = "snapshot"  # "snapshot" or "stream"
    close: float | None = None
    snapshot_end_missing: bool = False
    waited_ms: int = 0
    tick_counts: dict[str, int] = field(default_factory=dict)  # by TickTypeEnum name
    market_data_types_delivered: tuple[int, ...] = ()
    tick_req_params: dict | None = None
    request_events: tuple[tuple[int, str], ...] = ()  # (code, message) for this reqId
    request_error: str | None = None
    first_tick_ms: int | None = None  # after the request was sent
    last_tick_ms: int | None = None

    @property
    def ticks_received(self) -> int:
        return sum(self.tick_counts.values())

    @property
    def has_price(self) -> bool:
        return any(x is not None and x > 0 for x in (self.bid, self.ask, self.last))

    @property
    def two_sided_valid(self) -> bool:
        bad = {"MISSING_BID", "MISSING_ASK", "NON_POSITIVE_BID", "NON_POSITIVE_ASK", "CROSSED"}
        return self.quality is not QuoteQuality.UNAVAILABLE and not bad.intersection(self.flags)


def _positive(x: float | None) -> bool:
    return x is not None and x > 0


def assess_quote(*, bid: float | None, ask: float | None, last: float | None,
                 market_data_type: int | None, delayed_ticks_seen: bool,
                 live_ticks_seen: bool, unavailable_code: int | None = None,
                 ) -> tuple[QuoteQuality, tuple[str, ...]]:
    """Label the quote from what the broker actually delivered.

    * No positive bid, ask or last, or an entitlement refusal -> UNAVAILABLE.
    * Otherwise the broker-reported market-data type decides: 1 LIVE, 3 DELAYED, 2/4 FROZEN.
    * If the broker never reported a type, delayed tick IDs mean DELAYED; plain tick IDs are also
      used for frozen data, so they are labelled FROZEN. LIVE is never claimed without the broker
      saying so. Timestamps play no part: a last-trade time never makes bid/ask "fresh".
    """
    flags: list[str] = []
    for name, value in (("BID", bid), ("ASK", ask)):
        if value is None:
            flags.append(f"MISSING_{name}")
        elif value <= 0:
            flags.append(f"NON_POSITIVE_{name}")
    if last is not None and last <= 0:
        flags.append("NON_POSITIVE_LAST")
    if _positive(bid) and _positive(ask) and bid > ask:
        flags.append("CROSSED")
    if market_data_type is None:
        flags.append("MARKET_DATA_TYPE_NOT_REPORTED")
    if delayed_ticks_seen and live_ticks_seen:
        flags.append("MIXED_TICK_TYPES")
    if unavailable_code is not None:
        flags.append("NOT_ENTITLED")

    if unavailable_code is not None or not (_positive(bid) or _positive(ask) or _positive(last)):
        return QuoteQuality.UNAVAILABLE, tuple(flags)
    if market_data_type == 1:
        if delayed_ticks_seen:
            flags.append("TYPE_TICK_CONFLICT")
            return QuoteQuality.DELAYED, tuple(flags)
        return QuoteQuality.LIVE, tuple(flags)
    if market_data_type == 3:
        return QuoteQuality.DELAYED, tuple(flags)
    if market_data_type in (2, 4):
        return QuoteQuality.FROZEN, tuple(flags)
    if market_data_type is not None:
        flags.append("UNKNOWN_MARKET_DATA_TYPE")
    if delayed_ticks_seen:
        return QuoteQuality.DELAYED, tuple(flags)
    return QuoteQuality.FROZEN, tuple(flags)


def to_jsonable(obj: object) -> object:
    """Dataclass/enum/Decimal -> plain JSON types."""
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: to_jsonable(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, dict):
        return {str(k): to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_jsonable(v) for v in obj]
    return obj



QUOTE_PASS, QUOTE_WARN, QUOTE_FAIL = "PASS", "PASS_WITH_WARNINGS", "FAIL"


@dataclass(frozen=True)
class QuoteReport:
    """All quote attempts (snapshot, then at most one streaming sample) and the step verdict."""
    attempts: tuple[QuoteSnapshot, ...]
    status: str
    reason: str

    @property
    def best(self) -> QuoteSnapshot:
        return max(self.attempts, key=lambda q: (q.two_sided_valid, q.has_price,
                                                 q.ticks_received))


def assess_quote_attempts(attempts: tuple[QuoteSnapshot, ...]) -> tuple[str, str]:
    """PASS: a LIVE two-sided quote with a broker-reported market-data type, by either route.
    PASS_WITH_WARNINGS: only delayed/frozen two-sided data, or only partial data (says which).
    FAIL: nothing at all arrived on any route."""
    two_sided = [q for q in attempts if q.two_sided_valid and q.market_data_type is not None]
    for q in two_sided:
        if q.quality is QuoteQuality.LIVE:
            return QUOTE_PASS, f"live two-sided quote via {q.route}"
    if two_sided:
        q = two_sided[0]
        return QUOTE_WARN, (f"only {q.market_data_type_name} two-sided data (quality "
                            f"{q.quality.value}) via {q.route}")
    if any(q.ticks_received for q in attempts):
        parts = []
        for q in attempts:
            got = [n for n, v in (("bid", q.bid), ("ask", q.ask), ("last", q.last),
                                  ("close", q.close)) if v is not None and v > 0]
            parts.append(f"{q.route}: {', '.join(got) or 'no price'}; "
                         f"type {q.market_data_type_name}; flags {list(q.flags)}")
        return QUOTE_WARN, "partial data only (" + " | ".join(parts) + ")"
    routes = ", ".join(f"{q.route} {q.waited_ms} ms" for q in attempts)
    return QUOTE_FAIL, f"no market data delivered ({routes})"
