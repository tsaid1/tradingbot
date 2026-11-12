"""Result models and the pure quote-quality assessment (SDK-free)."""
from __future__ import annotations

import dataclasses
import math
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum


@dataclass(frozen=True)
class ContractQuery:
    symbol: str
    sec_type: str = "STK"
    exchange: str = "SMART"
    currency: str = "USD"
    primary_exchange: str = ""
    # --- optional, for futures, indices, commodities, CFDs and options (empty / None = unset) ---
    last_trade_date_or_contract_month: str = ""  # YYYYMM or YYYYMMDD, as the SDK field
    multiplier: str = ""
    trading_class: str = ""
    local_symbol: str = ""
    strike: float | None = None
    right: str = ""  # "C" / "P" (options)
    include_expired: bool = False


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
class DiscoveredContract:
    """One contract-details row, as ibapi 1052.1 ``Contract`` + ``ContractDetails`` provide it.
    ``None`` means the broker left the field unset (UNSET_DOUBLE / UNSET_DECIMAL)."""

    con_id: int
    symbol: str
    local_symbol: str
    sec_type: str
    exchange: str
    primary_exchange: str
    currency: str
    multiplier: str
    min_tick: float
    price_magnifier: int
    trading_class: str
    last_trade_date_or_contract_month: str  # Contract.lastTradeDateOrContractMonth (date part)
    last_trade_date: str  # Contract.lastTradeDate
    last_trade_time: str  # ContractDetails.lastTradeTime (time part split off by the SDK)
    contract_month: str  # ContractDetails.contractMonth
    real_expiration_date: str  # ContractDetails.realExpirationDate
    strike: float | None
    right: str
    under_con_id: int
    under_symbol: str
    under_sec_type: str
    long_name: str
    market_name: str
    stock_type: str
    time_zone_id: str
    trading_hours: str
    liquid_hours: str
    valid_exchanges: str
    order_types: str
    market_rule_ids: str
    min_size: Decimal | None
    size_increment: Decimal | None
    suggested_size_increment: Decimal | None
    query_match: bool  # secType and currency equal the query's


DISCOVERY_FOUND, DISCOVERY_NOT_FOUND, DISCOVERY_ERROR = "FOUND", "NOT_FOUND", "ERROR"
NO_SECURITY_DEFINITION = 200  # "No security definition has been found for the request"


@dataclass(frozen=True)
class DiscoveryResult:
    """All contracts matching one query. ``NOT_FOUND`` (error 200, or an empty end) and ``ERROR``
    (any other request-scoped broker error) are normal results, not exceptions. ``truncated``:
    more than ``limit`` rows arrived; the request was then cancelled and only ``limit`` kept."""

    query: ContractQuery
    status: str
    contracts: tuple[DiscoveredContract, ...]
    limit: int
    truncated: bool = False
    error_code: int | None = None
    error_message: str | None = None

    @property
    def count(self) -> int:
        return len(self.contracts)


@dataclass(frozen=True)
class OptionChainParams:
    """One ``securityDefinitionOptionParameter`` callback (one exchange / trading class)."""

    exchange: str
    underlying_con_id: int
    trading_class: str
    multiplier: str
    expirations: tuple[str, ...]  # sorted
    strikes: tuple[float, ...]  # sorted


@dataclass(frozen=True)
class OptionChainResult:
    underlying_symbol: str
    underlying_sec_type: str
    underlying_con_id: int
    fut_fop_exchange: str
    status: str  # FOUND | NOT_FOUND | ERROR
    params: tuple[OptionChainParams, ...]
    limit: int
    truncated: bool = False
    error_code: int | None = None
    error_message: str | None = None


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
    """One quote attempt and its evidence.

    ``quote_age_seconds``: local receipt time (``received_at``) minus the exchange last-trade
    timestamp (tick 45/88), in seconds, when TWS delivered that timestamp; otherwise ``None`` and
    the flag ``QUOTE_AGE_UNKNOWN``. It describes the LAST TRADE ONLY. It says nothing about how
    fresh the bid or ask is (TWS sends no time for those), and nothing here infers bid/ask
    freshness from it. It also includes any local clock offset.
    """

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
    quote_age_seconds: float | None = None  # receipt minus last-trade time; LAST TRADE ONLY
    ended_early: bool = False  # stream ended once complete and settled, before its window
    window_ms: int = 0  # the bounded wait for this attempt
    # --- per-side evidence (additive; defaults keep older JSON reports loadable) ---------------
    # ``*_at``: local UTC receipt time of the latest tick that confirmed that side (its price or
    # its size tick); ``*_at_monotonic``: the same instant on this process's monotonic clock
    # (comparable only with ``time.monotonic()`` in the same process). ``*_updates``: ticks on
    # that side after its initial image that changed a value (see ``classify_quote``).
    # ``*_tick_class``: LIVE / DELAYED / MIXED tick ids seen on that side (None: no tick).
    bid_at: str | None = None
    ask_at: str | None = None
    bid_at_monotonic: float | None = None
    ask_at_monotonic: float | None = None
    bid_updates: int = 0
    ask_updates: int = 0
    bid_tick_class: str | None = None
    ask_tick_class: str | None = None
    quality_reason: str = ""  # one of QUOTE_REASONS ("" in reports written before it existed)
    observed_at_monotonic: float | None = None  # monotonic time this snapshot was built

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


TICK_CLASS_LIVE, TICK_CLASS_DELAYED, TICK_CLASS_MIXED = "LIVE", "DELAYED", "MIXED"

# Request-scoped notices that say the delivered data is delayed or only partly entitled. They are
# recorded on the snapshot and can only make the label worse (a would-be LIVE becomes DELAYED).
# 10167: "not subscribed, displaying delayed market data"; 2186: delayed data instead of live
# (US stocks on the paper account); 10090: "part of requested market data is not subscribed".
# Entitlement REFUSALS (354, 10089, 10168, 10186, 10197) arrive as ``unavailable_code`` instead.
ENTITLEMENT_NOTICE_CODES = frozenset({2186, 10090, 10167})

# The closed list of classification reasons (``QuoteClassification.reason``).
REASON_NOT_ENTITLED = "NOT_ENTITLED"  # UNAVAILABLE: an entitlement refusal for this request
REASON_STREAM_FAILED = "STREAM_FAILED"  # UNAVAILABLE: the held stream failed (error, loss, poison)
REASON_NO_PRICE = "NO_PRICE"  # UNAVAILABLE: no positive bid, ask or last
REASON_REPORTED_DELAYED = "REPORTED_DELAYED"  # DELAYED: type 3
REASON_REPORTED_DELAYED_FROZEN = "REPORTED_DELAYED_FROZEN"  # FROZEN: type 4
REASON_REPORTED_FROZEN = "REPORTED_FROZEN"  # FROZEN: type 2
REASON_DELAYED_TICKS = "DELAYED_TICKS"  # DELAYED: a delayed tick id with type 1, unreported or unknown
REASON_UNKNOWN_TYPE = "UNKNOWN_MARKET_DATA_TYPE"  # FROZEN: a type value outside 1..4
REASON_REPORTED_LIVE_NO_LIVE_TICKS = "REPORTED_LIVE_NO_LIVE_TICKS"  # FROZEN: type 1, no live tick
REASON_TYPE_UNREPORTED_NO_UPDATES_YET = "TYPE_UNREPORTED_NO_UPDATES_YET"  # FROZEN
REASON_ENTITLEMENT_NOTICE = "ENTITLEMENT_NOTICE"  # DELAYED: would be LIVE, but a notice says not
REASON_REPORTED_LIVE = "REPORTED_LIVE"  # LIVE: type 1 and live ticks (the initial image counts)
REASON_TYPE_UNREPORTED_UPDATES_SEEN = "TYPE_UNREPORTED_UPDATES_SEEN"  # LIVE: both sides moved
QUOTE_REASONS = (
    REASON_NOT_ENTITLED, REASON_STREAM_FAILED, REASON_NO_PRICE, REASON_REPORTED_DELAYED,
    REASON_REPORTED_DELAYED_FROZEN, REASON_REPORTED_FROZEN, REASON_DELAYED_TICKS,
    REASON_UNKNOWN_TYPE, REASON_REPORTED_LIVE_NO_LIVE_TICKS,
    REASON_TYPE_UNREPORTED_NO_UPDATES_YET, REASON_ENTITLEMENT_NOTICE, REASON_REPORTED_LIVE,
    REASON_TYPE_UNREPORTED_UPDATES_SEEN)


@dataclass(frozen=True)
class QuoteClassification:
    quality: QuoteQuality
    reason: str  # one of QUOTE_REASONS
    flags: tuple[str, ...]


def classify_quote(*, bid: float | None, ask: float | None, last: float | None,
                   market_data_type: int | None, delayed_ticks_seen: bool,
                   live_ticks_seen: bool, bid_updates: int = 0, ask_updates: int = 0,
                   notices: tuple[int, ...] | list[int] = (),
                   unavailable_code: int | None = None,
                   stream_failed: bool = False) -> QuoteClassification:
    """THE quote classification: observed facts -> label, reason, flags. Pure; fails closed.

    Rules, first match wins:

    ====  ===========================================================  ============  ===========
    step  facts                                                        quality       reason
    ====  ===========================================================  ============  ===========
    1     entitlement refusal (``unavailable_code``)                   UNAVAILABLE   NOT_ENTITLED
    2     the held stream failed (``stream_failed``)                   UNAVAILABLE   STREAM_FAILED
    3     no positive bid, ask or last                                 UNAVAILABLE   NO_PRICE
    4     reported type 3                                              DELAYED       REPORTED_DELAYED
    5     reported type 4                                              FROZEN        REPORTED_DELAYED_FROZEN
    6     reported type 2                                              FROZEN        REPORTED_FROZEN
    7     any delayed tick id (type 1, unreported or unknown)          DELAYED       DELAYED_TICKS
    8     reported type outside 1..4                                   FROZEN        UNKNOWN_MARKET_DATA_TYPE
    9     reported type 1, no live tick                                FROZEN        REPORTED_LIVE_NO_LIVE_TICKS
    10    reported type 1, live ticks (the initial image counts)       LIVE*         REPORTED_LIVE
    11    type NOT reported, bid AND ask each >= 1 post-image update   LIVE*         TYPE_UNREPORTED_UPDATES_SEEN
    12    type NOT reported, otherwise                                 FROZEN        TYPE_UNREPORTED_NO_UPDATES_YET
    ====  ===========================================================  ============  ===========

    ``*``: an entitlement notice for the request (``ENTITLEMENT_NOTICE_CODES``) turns a would-be
    LIVE into DELAYED / ENTITLEMENT_NOTICE. Nothing here ever makes a label better.

    Initial image: after subscribing, the FIRST price tick and the FIRST size tick of each side
    (bid, ask), live or delayed id alike, with a set value. A post-image update is any later tick
    of that side whose value differs from the previous value of the same kind (price or size), so
    a frozen feed that repeats its image is still "no updates". Why an unreported type needs
    them: TWS does not always send ``marketDataType`` for real-time data, and a frozen feed also
    sends plain (non-delayed) tick ids, but only one image at subscription time; "arrived just
    now" is no evidence of a moving market. Usability of the two sides is separate (flags below,
    and ``entry_quote_check``): the label says what kind of data it is, not whether it can trade.
    Timestamps play no part: a last-trade time never makes bid/ask "fresh".
    """
    flags: list[str] = []
    for name, value in (("BID", bid), ("ASK", ask)):
        if value is None:
            flags.append(f"MISSING_{name}")
        elif value <= 0:
            flags.append(f"NON_POSITIVE_{name}")
    if last is not None and last <= 0:
        flags.append("NON_POSITIVE_LAST")
    if _positive(bid) and _positive(ask):
        if bid > ask:
            flags.append("CROSSED")
        elif bid == ask:
            flags.append("LOCKED")
    if market_data_type is None:
        flags.append("MARKET_DATA_TYPE_NOT_REPORTED")
    if delayed_ticks_seen and live_ticks_seen:
        flags.append("MIXED_TICK_TYPES")
    if unavailable_code is not None:
        flags.append("NOT_ENTITLED")
    notice = any(int(c) in ENTITLEMENT_NOTICE_CODES for c in notices)

    def out(quality: QuoteQuality, reason: str) -> QuoteClassification:
        return QuoteClassification(quality, reason, tuple(flags))

    if unavailable_code is not None:
        return out(QuoteQuality.UNAVAILABLE, REASON_NOT_ENTITLED)
    if stream_failed:
        flags.append("STREAM_FAILED")
        return out(QuoteQuality.UNAVAILABLE, REASON_STREAM_FAILED)
    if not (_positive(bid) or _positive(ask) or _positive(last)):
        return out(QuoteQuality.UNAVAILABLE, REASON_NO_PRICE)
    if market_data_type == 3:
        return out(QuoteQuality.DELAYED, REASON_REPORTED_DELAYED)
    if market_data_type == 4:
        return out(QuoteQuality.FROZEN, REASON_REPORTED_DELAYED_FROZEN)
    if market_data_type == 2:
        return out(QuoteQuality.FROZEN, REASON_REPORTED_FROZEN)
    known = market_data_type in (None, 1)
    if not known:
        flags.append("UNKNOWN_MARKET_DATA_TYPE")
    if delayed_ticks_seen:
        if market_data_type == 1:
            flags.append("TYPE_TICK_CONFLICT")
        return out(QuoteQuality.DELAYED, REASON_DELAYED_TICKS)
    if not known:
        return out(QuoteQuality.FROZEN, REASON_UNKNOWN_TYPE)
    if market_data_type == 1:
        if not live_ticks_seen:
            return out(QuoteQuality.FROZEN, REASON_REPORTED_LIVE_NO_LIVE_TICKS)
        reason = REASON_REPORTED_LIVE
    else:
        if bid_updates < 1 or ask_updates < 1:
            flags.append(REASON_TYPE_UNREPORTED_NO_UPDATES_YET)
            return out(QuoteQuality.FROZEN, REASON_TYPE_UNREPORTED_NO_UPDATES_YET)
        reason = REASON_TYPE_UNREPORTED_UPDATES_SEEN
    if notice:  # the codes themselves are on the snapshot (``notices``)
        flags.append("ENTITLEMENT_NOTICE")
        return out(QuoteQuality.DELAYED, REASON_ENTITLEMENT_NOTICE)
    return out(QuoteQuality.LIVE, reason)


def assess_quote(*, bid: float | None, ask: float | None, last: float | None,
                 market_data_type: int | None, delayed_ticks_seen: bool,
                 live_ticks_seen: bool, unavailable_code: int | None = None,
                 ) -> tuple[QuoteQuality, tuple[str, ...]]:
    """``classify_quote`` without per-side update counts or notices (kept for existing callers):
    with no post-image updates known, an unreported type is never LIVE."""
    c = classify_quote(bid=bid, ask=ask, last=last, market_data_type=market_data_type,
                       delayed_ticks_seen=delayed_ticks_seen, live_ticks_seen=live_ticks_seen,
                       unavailable_code=unavailable_code)
    return c.quality, c.flags


# --- freshness for an automatic entry ---------------------------------------------------------------
# The closed list of reasons ``entry_quote_check`` can return.
ENTRY_QUOTE_REASONS = (
    "INVALID_MAX_AGE", "INVALID_NOW", "NOT_LIVE", "BID_MISSING", "ASK_MISSING",
    "BID_NON_POSITIVE", "ASK_NON_POSITIVE", "LOCKED", "CROSSED", "BID_TICKS_NOT_LIVE",
    "ASK_TICKS_NOT_LIVE", "BID_TIME_UNKNOWN", "ASK_TIME_UNKNOWN", "BID_FROM_FUTURE",
    "ASK_FROM_FUTURE", "BID_STALE", "ASK_STALE")


@dataclass(frozen=True)
class EntryQuoteCheck:
    usable: bool
    reasons: tuple[str, ...]  # empty iff usable; members of ENTRY_QUOTE_REASONS
    bid_age_seconds: float | None
    ask_age_seconds: float | None


def _finite_number(x: object) -> bool:
    return not isinstance(x, bool) and isinstance(x, (int, float)) and math.isfinite(x)


def entry_quote_check(snapshot: QuoteSnapshot, *, now_monotonic: float,
                      max_age_seconds: float) -> EntryQuoteCheck:
    """Is this quote usable for an AUTOMATIC entry? Pure; every failing condition is listed.

    Usable only if: the label is LIVE; bid and ask are both present and positive; bid < ask
    (locked and crossed refused); each side's ticks were live ids only; and each side's last
    confirming tick (``*_at_monotonic``) is no older than ``max_age_seconds`` at
    ``now_monotonic`` (same process's ``time.monotonic()``) and not later than it. Wall-clock
    times are never used for age."""
    reasons: list[str] = []
    if not _finite_number(max_age_seconds) or max_age_seconds <= 0:
        reasons.append("INVALID_MAX_AGE")
    if not _finite_number(now_monotonic):
        reasons.append("INVALID_NOW")
    if snapshot.quality is not QuoteQuality.LIVE:
        reasons.append("NOT_LIVE")
    for side, value in (("BID", snapshot.bid), ("ASK", snapshot.ask)):
        if value is None:
            reasons.append(f"{side}_MISSING")
        elif not value > 0:
            reasons.append(f"{side}_NON_POSITIVE")
    if _positive(snapshot.bid) and _positive(snapshot.ask):
        if snapshot.bid == snapshot.ask:
            reasons.append("LOCKED")
        elif snapshot.bid > snapshot.ask:
            reasons.append("CROSSED")
    ages: dict[str, float | None] = {}
    for side, cls, at in (("BID", snapshot.bid_tick_class, snapshot.bid_at_monotonic),
                          ("ASK", snapshot.ask_tick_class, snapshot.ask_at_monotonic)):
        if cls != TICK_CLASS_LIVE:
            reasons.append(f"{side}_TICKS_NOT_LIVE")
        ages[side] = None
        if not _finite_number(at):
            reasons.append(f"{side}_TIME_UNKNOWN")
            continue
        if not _finite_number(now_monotonic):
            continue
        age = now_monotonic - at
        ages[side] = round(age, 3)
        if age < 0:
            reasons.append(f"{side}_FROM_FUTURE")
        elif _finite_number(max_age_seconds) and age > max_age_seconds:
            reasons.append(f"{side}_STALE")
    return EntryQuoteCheck(usable=not reasons, reasons=tuple(reasons),
                           bid_age_seconds=ages["BID"], ask_age_seconds=ages["ASK"])


def quote_age_seconds(received_at: datetime, last_trade_epoch: int | None) -> float | None:
    """Receipt time minus the exchange last-trade timestamp (unix seconds), or ``None`` when no
    timestamp was delivered. Dates the last trade only, never the bid or ask."""
    if last_trade_epoch is None:
        return None
    return round((received_at - datetime.fromtimestamp(last_trade_epoch, UTC)).total_seconds(), 3)


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
    """All quote attempts (a streaming sample, then at most one snapshot if the caller asked for
    it) and the step verdict."""
    attempts: tuple[QuoteSnapshot, ...]
    status: str
    reason: str

    @property
    def best(self) -> QuoteSnapshot:
        return max(self.attempts, key=lambda q: (q.two_sided_valid, q.has_price,
                                                 q.ticks_received))


def assess_quote_attempts(attempts: tuple[QuoteSnapshot, ...]) -> tuple[str, str]:
    """PASS: a LIVE two-sided quote, by either route (LIVE needs a broker-reported type 1, or an
    unreported type with post-image updates on both sides; see ``classify_quote``).
    PASS_WITH_WARNINGS: only delayed/frozen two-sided data, or only partial data (says which).
    FAIL: nothing at all arrived on any route."""
    # An unreported type counts only when the classification found it LIVE (both sides moved
    # after the initial image); an unreported, unconfirmed quote stays out, as before.
    two_sided = [q for q in attempts if q.two_sided_valid
                 and (q.market_data_type is not None or q.quality is QuoteQuality.LIVE)]
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
