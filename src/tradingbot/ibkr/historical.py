"""Historical price bars: request model, closed sets, pacing, bar parsing (SDK-free).

The session side (``IbkrSession.historical_bars``) sends one ``reqHistoricalData`` with
``keepUpToDate`` hard-wired to False and ``formatDate`` 2, and builds a ``HistoricalBarsResult``
from what this module parses. Everything here is pure and testable without the SDK.

Sources: the request fields, the bar fields and the date formats are from the installed SDK source
(ibapi 1052.1: ``EClient.reqHistoricalData``, ``client_utils.createHistoricalDataRequestProto``,
``decoder.processHistoricalDataMsgProtoBuf``, ``decoder_utils.decodeHistoricalDataBar``,
``common.BarData``). The pacing limits and the meaning of the broker's error codes are from IBKR's
public API documentation (``historical_limitations.html``, ``message_codes.html``); they are not in
the SDK and are unverified against a real TWS (docs/IBKR_ADAPTER.md, section 9).
"""
from __future__ import annotations

import math
import re
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, tzinfo
from decimal import Decimal
from typing import Any

# --- closed sets ---------------------------------------------------------------------------------
BAR_1_MIN, BAR_1_DAY = "1 min", "1 day"
# Bar size -> the durations allowed with it. IBKR's duration / bar-size table: 1 min bars are
# valid for 60 S .. 1 D (we allow 30 min .. 1 D), 1 day bars for 1 W .. 1 Y and beyond.
DURATIONS: dict[str, tuple[str, ...]] = {
    BAR_1_MIN: ("1800 S", "3600 S", "7200 S", "14400 S", "28800 S", "1 D"),
    BAR_1_DAY: ("1 W", "1 M", "3 M", "6 M", "1 Y"),
}
BAR_SIZES = tuple(DURATIONS)
# An index has no BID / ASK history: the caller may still ask, and the broker's refusal is
# reported as such (never worked around).
WHAT_TO_SHOW = ("TRADES", "MIDPOINT", "BID", "ASK")
SEC_TYPES = ("STK", "IND", "FUT", "CONTFUT", "CMDTY", "CASH")
FORMAT_DATE_EPOCH = 2  # SDK: "2 - dates are returned as a long integer ... seconds since 1970 GMT"
END_TIME_FORMAT = "%Y%m%d-%H:%M:%S"  # IBKR's UTC form, e.g. 20160127-23:59:59 (documentation)
MAX_HISTORICAL_BARS = 2000  # 1 D of 1 min bars is at most 1440; more is a malformed answer
DEFAULT_HISTORICAL_TIMEOUT = 60.0
MAX_HISTORICAL_TIMEOUT = 300.0
# The end time must lie between a fixed floor and now plus a small tolerance (clock skew), so a
# typo is refused instead of silently read by TWS as "up to now".
HIST_END_FLOOR = datetime(2010, 1, 1, tzinfo=UTC)
HIST_END_FUTURE_TOLERANCE_SECONDS = 300
# Bars must start inside the requested window: a sanity bound, not a gate, so it must never refuse
# legitimate data. IBKR may count durations in TRADING time (documented for D: "1 D" is the last
# trading session; possibly also for seconds), so a window can reach back across the daily
# maintenance break, a weekend or a long closure. Every duration therefore gets
# HIST_CALENDAR_SLACK_DAYS of calendar slack (10 days: longer than any market closure we expect);
# seconds durations also get one bar (60 s) for the first bar's alignment. A bar earlier than
# end - duration - slack fails the read; the upper bound (no bar at or after the end) is separate.
HIST_BAR_ALIGN_SECONDS = 60
HIST_CALENDAR_SLACK_DAYS = 10
_UNIT_DAYS = {"D": 1, "W": 7, "M": 31, "Y": 366}

# --- pacing (IBKR documentation, applied conservatively) ------------------------------------------
# Documented: no identical request within 15 s; no six or more requests for the same contract,
# exchange and tick type within 2 s; no more than 60 requests in any 10 minutes (BID_ASK counts
# twice; it is not in WHAT_TO_SHOW). Documented for bars of 30 s or less; we apply it to every bar
# size.
HIST_MIN_SPACING_SECONDS = 3.0  # between any two historical requests (at most 1 per 2 s)
HIST_WINDOW_SECONDS = 600.0  # the rolling window ...
HIST_MAX_PER_WINDOW = 50  # ... and the cap inside it (documented: 60)
HIST_IDENTICAL_INTERVAL_SECONDS = 60.0  # an identical request is refused this long (doc: 15 s)

# --- statuses and reason codes -------------------------------------------------------------------
COMPLETE, FAILED = "COMPLETE", "FAILED"
R_OK = "OK"  # end marker received, at least one bar
R_NO_BARS = "NO_BARS"  # end marker received, no bar (complete, empty)
R_NO_DATA = "NO_DATA"  # broker: 162 "... query returned no data ..."
R_NO_PERMISSION = "NO_PERMISSION"  # broker: no market-data permission / not subscribed
R_PACING_VIOLATION = "PACING_VIOLATION"  # broker: 162 "... pacing violation ..."; stops the batch
R_EXPIRED_CONTRACT = "EXPIRED_CONTRACT_UNAVAILABLE"  # broker: 166
R_CONTRACT_NOT_FOUND = "CONTRACT_NOT_FOUND"  # broker: 200
R_REQUEST_INVALID = "REQUEST_INVALID"  # broker: 321 / 322
R_HMDS_QUERY = "HMDS_QUERY_MESSAGE"  # broker: 165
R_HMDS_ERROR = "HMDS_ERROR"  # broker: any other 162
R_BROKER_ERROR = "BROKER_ERROR"  # any other request-scoped broker error
R_TIMEOUT = "TIMEOUT"  # no end marker in time; the request was cancelled
R_MALFORMED = "MALFORMED"  # a bar or the answer failed validation; the request was cancelled
R_LOCAL_PACING_CAP = "LOCAL_PACING_CAP"  # not sent: HIST_MAX_PER_WINDOW reached
R_LOCAL_IDENTICAL = "LOCAL_IDENTICAL_REQUEST"  # not sent: identical request too recently
R_LOCAL_PACING_LATCHED = "LOCAL_PACING_LATCHED"  # not sent: the broker reported a pacing violation
REASONS = (R_OK, R_NO_BARS, R_NO_DATA, R_NO_PERMISSION, R_PACING_VIOLATION, R_EXPIRED_CONTRACT,
           R_CONTRACT_NOT_FOUND, R_REQUEST_INVALID, R_HMDS_QUERY, R_HMDS_ERROR, R_BROKER_ERROR,
           R_TIMEOUT, R_MALFORMED, R_LOCAL_PACING_CAP, R_LOCAL_IDENTICAL, R_LOCAL_PACING_LATCHED)
LOCAL_PACING_REASONS = frozenset({R_LOCAL_PACING_CAP, R_LOCAL_IDENTICAL, R_LOCAL_PACING_LATCHED})
BATCH_STOP_REASONS = LOCAL_PACING_REASONS | {R_PACING_VIOLATION}

# Entitlement codes (documentation): 354 not subscribed, 10089 / 10090 / 10168 / 10186 / 10197.
NO_PERMISSION_CODES = frozenset({354, 10089, 10090, 10168, 10186, 10197})


class HistoricalRequestInvalid(ValueError):
    """The request is outside the closed sets or not fully specified. Raised before any send."""


@dataclass(frozen=True)
class HistoricalContract:
    """A fully specified contract: ``con_id`` plus ``exchange``, or every identifying field that
    contract discovery returns. ``include_expired`` must be True for an expired future."""

    con_id: int = 0
    symbol: str = ""
    sec_type: str = ""
    exchange: str = ""
    currency: str = ""
    primary_exchange: str = ""
    last_trade_date_or_contract_month: str = ""
    multiplier: str = ""
    trading_class: str = ""
    local_symbol: str = ""
    include_expired: bool = False

    def __post_init__(self) -> None:
        for name in ("symbol", "sec_type", "exchange", "currency", "primary_exchange",
                     "last_trade_date_or_contract_month", "multiplier", "trading_class",
                     "local_symbol"):
            if not isinstance(getattr(self, name), str):
                raise HistoricalRequestInvalid(f"contract.{name} must be a string")
        if isinstance(self.con_id, bool) or not isinstance(self.con_id, int) or self.con_id < 0:
            raise HistoricalRequestInvalid("contract.con_id must be a non-negative int")
        if not isinstance(self.include_expired, bool):
            raise HistoricalRequestInvalid("contract.include_expired must be a bool")
        if not self.exchange:
            raise HistoricalRequestInvalid("contract.exchange is required")
        if self.sec_type and self.sec_type not in SEC_TYPES:
            raise HistoricalRequestInvalid(f"contract.sec_type must be one of {SEC_TYPES}")
        if self.include_expired and self.sec_type != "FUT":
            raise HistoricalRequestInvalid("include_expired needs sec_type FUT (given explicitly)")
        ltd = self.last_trade_date_or_contract_month
        if ltd and not re.fullmatch(r"\d{6}|\d{8}", ltd):
            raise HistoricalRequestInvalid("last_trade_date_or_contract_month: YYYYMM or YYYYMMDD")
        if self.con_id:
            return
        missing = [n for n in ("symbol", "sec_type", "currency") if not getattr(self, n)]
        if self.sec_type == "STK" and not self.primary_exchange:
            missing.append("primary_exchange")
        if self.sec_type == "FUT":
            missing += [n for n in ("last_trade_date_or_contract_month", "multiplier",
                                    "trading_class") if not getattr(self, n)]
        if missing:
            raise HistoricalRequestInvalid(
                f"contract not fully specified without con_id: missing {missing}")

    @classmethod
    def from_discovered(cls, d: Any, *, include_expired: bool = False) -> HistoricalContract:
        """From a ``DiscoveredContract`` (contract discovery's row)."""
        return cls(con_id=int(d.con_id), symbol=d.symbol, sec_type=d.sec_type,
                   exchange=d.exchange, currency=d.currency, primary_exchange=d.primary_exchange,
                   last_trade_date_or_contract_month=d.last_trade_date_or_contract_month,
                   multiplier=d.multiplier, trading_class=d.trading_class,
                   local_symbol=d.local_symbol, include_expired=include_expired)


def _now() -> datetime:  # a seam for tests only
    return datetime.now(UTC)


def _check_end(end: object) -> datetime:
    """Aware, whole seconds (also after conversion to UTC), unambiguous in its own zone, between
    ``HIST_END_FLOOR`` and now plus ``HIST_END_FUTURE_TOLERANCE_SECONDS``; returned in UTC."""
    if not isinstance(end, datetime):
        raise HistoricalRequestInvalid("end must be a datetime")
    if end.tzinfo is None or end.utcoffset() is None:
        raise HistoricalRequestInvalid("end must be timezone-aware (naive datetimes are refused)")
    if end.microsecond:
        raise HistoricalRequestInvalid("end must be a whole second")
    if end.replace(fold=0).utcoffset() != end.replace(fold=1).utcoffset():
        # a wall-clock time repeated (fall back) or skipped (spring forward) in that zone
        raise HistoricalRequestInvalid(
            f"end {end.replace(tzinfo=None).isoformat()} is ambiguous or does not exist in "
            f"{end.tzinfo}; give it in UTC or with a fixed offset")
    utc = end.astimezone(UTC)
    if utc.microsecond:  # an offset with sub-second precision: refused, never truncated
        raise HistoricalRequestInvalid("end must be a whole second in UTC (sub-second offset)")
    if utc < HIST_END_FLOOR:
        raise HistoricalRequestInvalid(f"end is before {HIST_END_FLOOR.date().isoformat()}")
    if utc > _now() + timedelta(seconds=HIST_END_FUTURE_TOLERANCE_SECONDS):
        raise HistoricalRequestInvalid("end is in the future")
    return utc


@dataclass(frozen=True)
class HistoricalBarsRequest:
    """One bounded, non-streaming historical-bars read. There is deliberately no keep-up-to-date
    field: the session always sends ``keepUpToDate=False``."""

    contract: HistoricalContract
    end: datetime  # timezone-aware; stored converted to UTC
    duration: str
    bar_size: str
    what_to_show: str
    regular_hours_only: bool

    def __post_init__(self) -> None:
        if not isinstance(self.contract, HistoricalContract):
            raise HistoricalRequestInvalid("contract must be a HistoricalContract")
        object.__setattr__(self, "end", _check_end(self.end))
        if self.bar_size not in DURATIONS:
            raise HistoricalRequestInvalid(f"bar_size must be one of {BAR_SIZES}")
        if self.duration not in DURATIONS[self.bar_size]:
            raise HistoricalRequestInvalid(
                f"duration for {self.bar_size!r} bars must be one of {DURATIONS[self.bar_size]}")
        if self.what_to_show not in WHAT_TO_SHOW:
            raise HistoricalRequestInvalid(f"what_to_show must be one of {WHAT_TO_SHOW}")
        if not isinstance(self.regular_hours_only, bool):
            raise HistoricalRequestInvalid("regular_hours_only must be a bool")

    @property
    def end_date_time(self) -> str:
        """The ``endDateTime`` string sent: UTC, ``yyyymmdd-hh:mm:ss``."""
        return self.end.strftime(END_TIME_FORMAT)

    @property
    def intraday(self) -> bool:
        return self.bar_size != BAR_1_DAY

    def window_start(self) -> datetime:
        """Earliest start a bar of this request may have (see HIST_CALENDAR_SLACK_DAYS)."""
        n, unit = self.duration.split()
        if unit == "S":
            return self.end - timedelta(days=HIST_CALENDAR_SLACK_DAYS,
                                        seconds=int(n) + HIST_BAR_ALIGN_SECONDS)
        return self.end - timedelta(days=int(n) * _UNIT_DAYS[unit] + HIST_CALENDAR_SLACK_DAYS)

    def pacing_key(self) -> tuple:
        """Canonical: the same contract written two ways is the same request. With a conId the
        conId alone identifies it; otherwise the identifying fields, case-folded, multiplier as a
        number, local symbol ignored. (conId versus fields cannot be matched without a broker
        lookup; the spacing and the cap still apply.)"""
        k = self.contract
        if k.con_id:
            ident: tuple = ("conId", k.con_id)
        else:
            try:
                mult = format(Decimal(k.multiplier).normalize(), "f") if k.multiplier else ""
            except Exception:  # noqa: BLE001 - not a number: keep as written
                mult = k.multiplier
            ident = ("fields", k.symbol.upper(), k.sec_type.upper(), k.exchange.upper(),
                     k.currency.upper(), k.primary_exchange.upper(),
                     k.last_trade_date_or_contract_month, mult, k.trading_class.upper())
        return (ident, self.end, self.duration, self.bar_size, self.what_to_show,
                self.regular_hours_only)


@dataclass(frozen=True)
class HistoricalBar:
    """One bar. Intraday bars carry ``start_utc`` (aware UTC; from epoch seconds). Daily bars
    carry ``session_date`` (``YYYY-MM-DD``, the trading session's date as TWS gives it; IBKR
    documents only ``yyyyMMdd`` for day bars, so no instant is invented) and ``start_utc`` None."""

    start_utc: datetime | None
    session_date: str | None
    open: float
    high: float
    low: float
    close: float
    volume: Decimal | None = None  # None where the broker gives none (unset or -1)
    wap: Decimal | None = None
    bar_count: int | None = None


class BarMalformed(ValueError):
    """A bar that fails validation; the whole read fails (``MALFORMED``)."""


def parse_bar_time(raw: object, *, intraday: bool) -> tuple[datetime | None, str | None]:
    """``formatDate`` 2: intraday bars are epoch seconds (digits only); day bars are
    ``yyyyMMdd``. Anything else (a formatted local time, which is ambiguous across a DST change,
    an empty string, a sign, spaces) is refused."""
    if not isinstance(raw, str):
        raise BarMalformed("bar date is not a string")
    if intraday:
        if not re.fullmatch(r"[0-9]{9,11}", raw):
            raise BarMalformed(f"intraday bar time {raw[:32]!r} is not epoch seconds")
        try:
            return datetime.fromtimestamp(int(raw), UTC), None
        except (OverflowError, OSError, ValueError) as exc:
            raise BarMalformed(f"bar time {raw!r} out of range") from exc
    if not re.fullmatch(r"[0-9]{8}", raw):
        raise BarMalformed(f"day bar date {raw[:32]!r} is not yyyyMMdd")
    try:
        d = datetime.strptime(raw, "%Y%m%d").date()
    except ValueError as exc:
        raise BarMalformed(f"day bar date {raw!r} is not a calendar date") from exc
    return None, d.isoformat()


def _optional_decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        d = Decimal(value)
    except Exception as exc:
        raise BarMalformed("unreadable volume / WAP") from exc
    if not d.is_finite():
        raise BarMalformed("non-finite volume / WAP")
    return None if d < 0 else d


def build_bar(raw_date: object, open_: object, high: object, low: object, close: object, *,
              volume: object, wap: object, bar_count: object, request: HistoricalBarsRequest,
              previous: HistoricalBar | None) -> HistoricalBar:
    """Validate one bar against the request and the bar before it (strictly increasing time)."""
    start, session_date = parse_bar_time(raw_date, intraday=request.intraday)
    prices = []
    for name, v in (("open", open_), ("high", high), ("low", low), ("close", close)):
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
            raise BarMalformed(f"bar {name} is not a finite number")
        prices.append(float(v))
    o, h, lo, c = prices
    if min(prices) <= 0:
        # zero, -1 (an unset sentinel) and any negative price fail the read; by decision this
        # also fails closed on a genuinely negative futures price
        raise BarMalformed("bar prices must be positive")
    if not (lo <= min(o, c) and h >= max(o, c) and lo <= h):
        raise BarMalformed("bar high / low do not bound open and close")
    window = request.window_start()
    if start is not None and start < window:
        raise BarMalformed("bar starts before the requested window")
    if session_date is not None and date.fromisoformat(session_date) < window.date():
        raise BarMalformed("day bar dated before the requested window")
    if start is not None and start >= request.end:
        raise BarMalformed("bar starts at or after the requested end")
    # a day bar is dated by its session (which may close after the UTC date of the end)
    if (session_date is not None
            and date.fromisoformat(session_date) > request.end.date() + timedelta(days=1)):
        raise BarMalformed("day bar dated after the requested end")
    if previous is not None:
        key_new = start if start is not None else session_date
        key_old = previous.start_utc if previous.start_utc is not None else previous.session_date
        if key_new <= key_old:  # type: ignore[operator]
            raise BarMalformed("bar times are not strictly increasing")
    count: int | None = None
    if bar_count is not None:
        if isinstance(bar_count, bool) or not isinstance(bar_count, int):
            raise BarMalformed("bar count is not an int")
        count = bar_count if bar_count >= 0 else None
    return HistoricalBar(start_utc=start, session_date=session_date, open=o, high=h, low=lo,
                         close=c, volume=_optional_decimal(volume), wap=_optional_decimal(wap),
                         bar_count=count)


@dataclass(frozen=True)
class HistoricalBarsResult:
    """Immutable outcome. ``bars`` is non-empty only when ``complete`` (the end marker arrived)
    and ``reason`` is ``OK``: a failed read never carries bars."""

    request: HistoricalBarsRequest
    status: str  # COMPLETE | FAILED
    reason: str  # one of REASONS
    complete: bool
    bars: tuple[HistoricalBar, ...] = ()
    count: int = 0
    error_code: int | None = None
    error_message: str | None = None  # redacted by the session
    detail: str | None = None  # local reason detail (malformed, pacing); no broker text
    end_marker_start: str | None = None  # historicalDataEnd's start / end strings, as received
    end_marker_end: str | None = None
    req_id: int | None = None
    sent: bool = False
    cancel_sent: bool = False
    notices: tuple[int, ...] = ()
    waited_ms: int = 0
    bars_discarded: int = 0  # bars received before a failure, not kept

    def __post_init__(self) -> None:
        if self.reason not in REASONS:
            raise ValueError(f"unknown reason {self.reason!r}")
        if self.count != len(self.bars):
            raise ValueError("count must equal len(bars)")
        ok = self.reason in (R_OK, R_NO_BARS)
        if ok != (self.status == COMPLETE) or (ok and not self.complete):
            raise ValueError("status / reason / complete are inconsistent")
        if self.bars and self.reason != R_OK:
            raise ValueError("a failed or empty read carries no bars")

    @property
    def stops_batch(self) -> bool:
        return self.reason in BATCH_STOP_REASONS


def failed(request: HistoricalBarsRequest, reason: str, **kw: Any) -> HistoricalBarsResult:
    return HistoricalBarsResult(request=request, status=FAILED, reason=reason, complete=False,
                                **kw)


def classify_broker_error(code: int, message: str) -> str:
    """Reason code for a request-scoped broker error (codes and texts per IBKR documentation;
    162 carries several distinct meanings, told apart only by its message)."""
    text = (message or "").lower()
    if code == 162:
        if "pacing violation" in text:
            return R_PACING_VIOLATION
        if "permission" in text or "not subscribed" in text:
            return R_NO_PERMISSION
        if "no data" in text:
            return R_NO_DATA
        return R_HMDS_ERROR
    if code in NO_PERMISSION_CODES:
        return R_NO_PERMISSION
    if code == 165:
        return R_HMDS_QUERY
    if code == 166:
        return R_EXPIRED_CONTRACT
    if code == 200:
        return R_CONTRACT_NOT_FOUND
    if code in (321, 322):
        return R_REQUEST_INVALID
    return R_BROKER_ERROR


class HistoricalPacer:
    """Local, conservative pacing for historical requests (shared by every session in the
    process by default, since IBKR paces per connection over time, not per session object).

    ``admit`` waits out the minimum spacing (at most ``HIST_MIN_SPACING_SECONDS``), and refuses
    (returns a reason, records nothing) when the rolling cap is reached, when an identical
    request was admitted within ``HIST_IDENTICAL_INTERVAL_SECONDS``, or after ``latch`` (a
    broker-reported pacing violation; there is no way to un-latch).

    Scope: one process. Separate script runs do not share a pacer (each run is its own unit);
    the operator spaces runs. ``IbkrSession(historical_pacer=...)`` exists for tests only."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep,
                 min_spacing: float = HIST_MIN_SPACING_SECONDS,
                 window: float = HIST_WINDOW_SECONDS, max_per_window: int = HIST_MAX_PER_WINDOW,
                 identical_interval: float = HIST_IDENTICAL_INTERVAL_SECONDS) -> None:
        self._clock, self._sleep = clock, sleep
        self.min_spacing, self.window = min_spacing, window
        self.max_per_window, self.identical_interval = max_per_window, identical_interval
        self._lock = threading.Lock()
        self._sent: deque[float] = deque()
        self._keys: dict[tuple, float] = {}
        self._latched: str | None = None
        self.waited_seconds = 0.0

    @property
    def latched(self) -> str | None:
        """Why the pacer is latched (None if not). Read-only: nothing un-latches it."""
        return self._latched

    def latch(self, why: str) -> None:
        with self._lock:
            self._latched = self._latched or why

    def admit(self, key: tuple) -> str | None:
        with self._lock:
            if self._latched is not None:
                return R_LOCAL_PACING_LATCHED
            now = self._clock()
            while self._sent and now - self._sent[0] >= self.window:
                self._sent.popleft()
            self._keys = {k: t for k, t in self._keys.items()
                          if now - t < self.identical_interval}
            if len(self._sent) >= self.max_per_window:
                return R_LOCAL_PACING_CAP
            if key in self._keys:
                return R_LOCAL_IDENTICAL
            if self._sent:
                wait = self._sent[-1] + self.min_spacing - now
                if wait > 0:
                    self._sleep(wait)
                    self.waited_seconds += wait
                    now = max(self._clock(), self._sent[-1] + self.min_spacing)
            self._sent.append(now)
            self._keys[key] = now
            return None

    def sent_in_window(self) -> int:
        with self._lock:
            now = self._clock()
            return sum(1 for t in self._sent if now - t < self.window)


DEFAULT_HISTORICAL_PACER = HistoricalPacer()


def bar_to_jsonable(bar: HistoricalBar) -> dict:
    return {"start_utc": None if bar.start_utc is None else bar.start_utc.isoformat(),
            "session_date": bar.session_date, "open": bar.open, "high": bar.high, "low": bar.low,
            "close": bar.close, "volume": None if bar.volume is None else str(bar.volume),
            "wap": None if bar.wap is None else str(bar.wap), "bar_count": bar.bar_count}


def request_to_jsonable(req: HistoricalBarsRequest) -> dict:
    c = req.contract
    return {"contract": {f: getattr(c, f) for f in c.__dataclass_fields__},
            "end_utc": req.end.isoformat(), "end_date_time": req.end_date_time,
            "duration": req.duration, "bar_size": req.bar_size,
            "what_to_show": req.what_to_show, "regular_hours_only": req.regular_hours_only,
            "keep_up_to_date": False, "format_date": FORMAT_DATE_EPOCH}


def result_to_jsonable(r: HistoricalBarsResult) -> dict:
    return {"request": request_to_jsonable(r.request), "status": r.status, "reason": r.reason,
            "complete": r.complete, "count": r.count, "error_code": r.error_code,
            "error_message": r.error_message, "detail": r.detail,
            "end_marker_start": r.end_marker_start, "end_marker_end": r.end_marker_end,
            "req_id": r.req_id, "sent": r.sent, "cancel_sent": r.cancel_sent,
            "notices": list(r.notices), "waited_ms": r.waited_ms,
            "bars_discarded": r.bars_discarded, "bars": [bar_to_jsonable(b) for b in r.bars]}


def zone(name: str) -> tzinfo:
    """IANA zone by name (for request files that give a local time plus a zone)."""
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise HistoricalRequestInvalid(f"unknown time zone {name!r}") from exc

