"""Fail-closed readiness diagnostics over read-only observations (issue #16). NON-AUTHORITATIVE.

Each ``assess_*`` function inspects what a read returned and reports what is missing, ambiguous,
mismatched, stale, delayed, unentitled, incomplete or erroneous about it. Only a complete, consistent,
live, fresh, regular-session observation set earns INFO-only "control" findings, and even then the
report is a diagnostic for a human reviewer: it carries ``trading_permission: False`` and never
selects a contract, converts a price, sizes anything or alters ``MockPolicy``. The shared gateway in
``validator.py`` remains the only authorization path.

Diagnostics are safe by construction: details name codes, field names, counts and documented message
texts; they never carry a balance, a price, a raw broker payload or an account identifier in clear.
"""
from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from hashlib import sha256
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field

from tradingbot.models import MockPolicy, StrictModel
from tradingbot.operations_view import FUTURE_TOLERANCE_SECONDS, freshness
from tradingbot.paper_readiness.observations import (
    AUTHORITY,
    DELAYED_TICK_TYPES,
    RECORDS_VERSION,
    AccountSummaryObservation,
    ApiMessage,
    ContractObservation,
    MarketDataType,
    PositionLine,
    QuoteObservation,
    ReadinessExpectation,
    SessionAttestation,
)

NOTICE = (
    "Non-authoritative offline readiness diagnostic over a deterministic FAKE read-only transport. It "
    "reports missing, ambiguous, mismatched, stale, delayed, unentitled, incomplete or erroneous "
    "observations only. It grants no trading permission, selects no contract, sizes nothing and changes "
    "no policy; the deterministic gateway (validator.py) is unchanged and still decides every action. "
    "A CONTROL_COMPLETE outcome says the synthetic observation set was complete and consistent, not that "
    "any broker is reliable, connected or protected."
)
# Required accountSummary tags (documented tag names) beyond AccountType, which is held separately.
REQUIRED_ACCOUNT_TAGS = ("NetLiquidation", "TotalCashValue", "BuyingPower")
# Documented accountSummary tag names (account_summary reference: "AccountType,NetLiquidation,TotalCashValue,
# SettledCash,AccruedCash,BuyingPower,EquityWithLoanValue" plus SMA, InitMarginReq, MaintMarginReq,
# AvailableFunds, ExcessLiquidity, Cushion, Leverage, DayTradesRemaining). Only these names may ever be
# rendered into a diagnostic; any other key in an observation is counted and withheld, so an untrusted or
# sensitive key can never reach a report.
DOCUMENTED_ACCOUNT_TAGS = frozenset({
    "AccountType", "NetLiquidation", "TotalCashValue", "SettledCash", "AccruedCash", "BuyingPower",
    "EquityWithLoanValue", "SMA", "InitMarginReq", "MaintMarginReq", "AvailableFunds", "ExcessLiquidity",
    "Cushion", "Leverage", "DayTradesRemaining",
})
assert set(REQUIRED_ACCOUNT_TAGS) <= DOCUMENTED_ACCOUNT_TAGS
# Session-level codes that are informational: data farms connected / connectivity restored intact.
SESSION_OK_CODES = frozenset({2104, 2106, 2158, 1102})
DATA_FARM_OK_CODES = frozenset({2104, 2158})

# Documented liquidHours formats: "20090507:0700-1830,1830-2330;20090508:CLOSED" (TWS <= 969) and
# "20180323:0930-20180323:1600;20180326:0930-20180326:1600" (TWS 970+, closing date explicit).
HOURS_SEGMENT_RE = re.compile(r"^(\d{8}):(.+)$")
HOURS_RANGE_RE = re.compile(r"^(?:(\d{8}):)?(\d{4})-(?:(\d{8}):)?(\d{4})$")


class FindingKind(str, Enum):
    INFO = "INFO"                  # informational control; not permission
    MISSING = "MISSING"            # required observation or field absent
    AMBIGUOUS = "AMBIGUOUS"        # observations admit more than one reading
    MISMATCH = "MISMATCH"          # observation contradicts the expectation or itself
    OUT_OF_SCOPE = "OUT_OF_SCOPE"  # outside the approved regular-session / tradable scope
    STALE = "STALE"                # too old or frozen to describe the current market
    DELAYED = "DELAYED"            # delayed-feed evidence
    UNENTITLED = "UNENTITLED"      # market data entitlement absent or partial
    INCOMPLETE = "INCOMPLETE"      # stream ended early, disconnected or truncated
    ERROR = "ERROR"                # broker error or unclassified message
    REFUSED = "REFUSED"            # preflight refusal; nothing was dispatched


HEADLINE_ORDER = (FindingKind.REFUSED, FindingKind.ERROR, FindingKind.INCOMPLETE, FindingKind.MISMATCH,
                  FindingKind.AMBIGUOUS, FindingKind.UNENTITLED, FindingKind.DELAYED, FindingKind.STALE,
                  FindingKind.OUT_OF_SCOPE, FindingKind.MISSING, FindingKind.INFO)


class Stage(str, Enum):
    PREFLIGHT = "PREFLIGHT"
    SESSION = "SESSION"
    CONTRACT = "CONTRACT"
    QUOTE = "QUOTE"
    ACCOUNT = "ACCOUNT"
    POSITIONS = "POSITIONS"
    CORRELATION = "CORRELATION"


class Finding(StrictModel):
    code: str
    kind: FindingKind
    stage: Stage
    detail: str
    request_id: int | None = None


class ReadinessOutcome(str, Enum):
    REFUSED_BEFORE_DISPATCH = "REFUSED_BEFORE_DISPATCH"
    NOT_READY = "NOT_READY"
    CONTROL_COMPLETE = "CONTROL_COMPLETE"


class ObservationDigest(StrictModel):
    """Which read answered, how many correlated rows survived, whether its END marker arrived and a
    digest of the kept envelopes, so a report can be tied to its inputs without carrying them."""
    stage: Stage
    request_id: int
    rows: int
    end_seen: bool
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class ReadinessReport(StrictModel):
    report_kind: Literal["PAPER_READINESS_DIAGNOSTIC"] = "PAPER_READINESS_DIAGNOSTIC"
    authority: Literal["NON_AUTHORITATIVE"] = AUTHORITY
    records_version: int = RECORDS_VERSION
    notice: str = NOTICE
    trading_permission: Literal[False] = False
    label: str | None = None
    as_of: datetime
    account_ref: str
    target_symbol: str
    outcome: ReadinessOutcome
    headline: FindingKind
    counts: dict[str, int]
    dispatched: list[str]
    observations: list[ObservationDigest]
    findings: list[Finding]


# Documented message codes this scaffold classifies; everything else fails closed as unclassified.
REQUEST_CODES: dict[int, tuple[str, FindingKind, str]] = {
    200: ("CONTRACT_NOT_FOUND", FindingKind.ERROR, "No security definition has been found for the request."),
    300: ("REQUEST_ID_UNKNOWN_TO_BROKER", FindingKind.ERROR, "Can't find EId with ticker Id."),
    321: ("BROKER_REQUEST_VALIDATION_ERROR", FindingKind.ERROR,
          "Server error when validating an API client request."),
    322: ("BROKER_REQUEST_PROCESSING_ERROR", FindingKind.ERROR,
          "Server error when processing an API client request."),
    354: ("ENTITLEMENT_UNAVAILABLE", FindingKind.UNENTITLED, "Not subscribed to requested market data."),
    504: ("NOT_CONNECTED", FindingKind.ERROR, "Not connected."),
    1100: ("CONNECTIVITY_LOST_MID_REQUEST", FindingKind.INCOMPLETE,
           "Connectivity between IB and the TWS has been lost."),
    1101: ("CONNECTIVITY_RESTORED_DATA_LOST", FindingKind.INCOMPLETE,
           "Connectivity between IB and TWS has been restored- data lost."),
    2110: ("SESSION_CONNECTIVITY_BROKEN", FindingKind.ERROR,
           "Connectivity between TWS and server is broken. It will be restored automatically."),
    10090: ("ENTITLEMENT_PARTIAL", FindingKind.UNENTITLED, "Part of requested market data is not subscribed."),
    10197: ("COMPETING_SESSION_NO_DATA", FindingKind.UNENTITLED, "No market data during competing session"),
}


# ----------------------------------------------------------------------------- helpers

def redact_account(account_id: str) -> str:
    """Prefix plus a short digest: enough to correlate, never the identifier itself."""
    return f"{account_id[:6]}...{sha256(account_id.encode('utf-8')).hexdigest()[:8]}"


def _decimal(value: str | None) -> Decimal | None:
    if value is None:
        return None
    try:
        result = Decimal(value)
    except (InvalidOperation, ValueError, TypeError):
        return None
    return result if result.is_finite() else None


def _label(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip().upper()
    return text or None


def _f(code: str, kind: FindingKind, stage: Stage, detail: str, request_id: int | None = None) -> Finding:
    return Finding(code=code, kind=kind, stage=stage, detail=detail, request_id=request_id)


def _has_problem(findings: list[Finding], stage: Stage) -> bool:
    return any(f.stage == stage and f.kind != FindingKind.INFO for f in findings)


def classify_messages(messages: list[ApiMessage], stage: Stage, request_id: int | None) -> list[Finding]:
    """Documented codes map to named findings; unknown codes fail closed as unclassified errors."""
    found: list[Finding] = []
    for m in messages:
        if m.code in SESSION_OK_CODES:
            continue
        entry = REQUEST_CODES.get(m.code)
        if entry is None:
            found.append(_f("BROKER_MESSAGE_UNCLASSIFIED", FindingKind.ERROR, stage,
                            f"Broker message code {m.code} is not classified by this scaffold; treated as not "
                            "ready.", request_id))
        else:
            code, kind, text = entry
            found.append(_f(code, kind, stage, f"Broker message {m.code}: {text}", request_id))
    return found


def parse_liquid_hours(text: str, tz: ZoneInfo) -> list[tuple[datetime, datetime]] | None:
    """Windows from a documented liquidHours string, or None when any part is unreadable. A range whose
    end does not follow its start (the pre-970 overnight ambiguity) is unreadable: nothing is guessed."""
    windows: list[tuple[datetime, datetime]] = []
    for segment in text.split(";"):
        segment = segment.strip()
        if not segment:
            continue
        match = HOURS_SEGMENT_RE.fullmatch(segment)
        if match is None:
            return None
        day, rest = match.groups()
        if rest == "CLOSED":
            continue
        for rng in rest.split(","):
            r = HOURS_RANGE_RE.fullmatch(rng.strip())
            if r is None:
                return None
            start_day, start_time, end_day, end_time = r.groups()
            try:
                start = datetime.strptime(f"{start_day or day}{start_time}", "%Y%m%d%H%M").replace(tzinfo=tz)
                end = datetime.strptime(f"{end_day or day}{end_time}", "%Y%m%d%H%M").replace(tzinfo=tz)
            except ValueError:
                return None
            if end <= start:
                return None
            windows.append((start, end))
    return windows


# ----------------------------------------------------------------------------- stage assessments

def assess_session(session: SessionAttestation, *, now: datetime) -> list[Finding]:
    """Capability facts of an already-admitted session (preflight has passed)."""
    s = Stage.SESSION
    found = classify_messages([m for m in session.messages if m.code != 1100], s, None)
    if session.server_version is None:
        found.append(_f("SERVER_VERSION_MISSING", FindingKind.MISSING, s,
                        "The session does not report a server version; API capability cannot be pinned."))
    if session.connection_time is None:
        found.append(_f("CONNECTION_TIME_MISSING", FindingKind.MISSING, s,
                        "The session does not report its connection time."))
    elif session.connection_time.tzinfo is None:
        found.append(_f("CONNECTION_TIME_UNTIMED", FindingKind.MISMATCH, s,
                        "The connection time carries no timezone and cannot be compared with the clock."))
    elif (session.connection_time - now).total_seconds() > FUTURE_TOLERANCE_SECONDS:
        found.append(_f("CONNECTION_TIME_INCONSISTENT", FindingKind.MISMATCH, s,
                        "The connection time lies in the future relative to the clock."))
    if not any(m.code in DATA_FARM_OK_CODES for m in session.messages):
        found.append(_f("DATA_FARM_STATUS_NOT_OBSERVED", FindingKind.INFO, s,
                        "No market-data (2104) or security-definition (2158) farm status message was observed; "
                        "recorded for the connected-check gate, not a refusal."))
    if not _has_problem(found, s):
        found.append(_f("SESSION_ATTESTATION_CONTROL", FindingKind.INFO, s,
                        "PAPER environment, single synthetic account, Read-Only API, server version and "
                        "connection time were all explicitly attested. Informational control only."))
    return found


def _session_scope(row: ContractObservation, now: datetime, request_id: int) -> list[Finding]:
    s = Stage.CONTRACT
    if row.liquid_hours is None or row.time_zone_id is None:
        return []  # already reported as missing fields
    try:
        tz = ZoneInfo(row.time_zone_id)
    except (ZoneInfoNotFoundError, ValueError, KeyError, OSError):
        return [_f("TIME_ZONE_UNRESOLVED", FindingKind.AMBIGUOUS, s,
                   "The contract's timeZoneId could not be resolved to a known zone; the session schedule "
                   "cannot be placed in time.", request_id)]
    windows = parse_liquid_hours(row.liquid_hours, tz)
    if windows is None:
        return [_f("LIQUID_HOURS_UNPARSEABLE", FindingKind.AMBIGUOUS, s,
                   "The liquidHours string is not in a documented format (or a range is ambiguous); no "
                   "session is assumed.", request_id)]
    if any(start <= now < end for start, end in windows):
        return [_f("REGULAR_SESSION_CONTROL", FindingKind.INFO, s,
                   "The clock falls inside an observed liquid-hours (regular session) window. Informational "
                   "control only.", request_id)]
    return [_f("OUTSIDE_REGULAR_SESSION", FindingKind.OUT_OF_SCOPE, s,
               "The clock is outside every observed liquid-hours window; the approved scope is the regular "
               "session only.", request_id)]


def assess_contract(expectation: ReadinessExpectation, rows: list[ContractObservation], end_seen: bool,
                    errors: list[ApiMessage], *, now: datetime, request_id: int,
                    ) -> tuple[list[Finding], int | None]:
    """Identity, USD basis and regular-session checks. Returns the con_id a quote may be requested for,
    or None when identity is absent, ambiguous or inconsistent, when the stream did not complete
    (no contractDetailsEnd) or when a broker error or connectivity loss accompanied it. The caller adds
    the correlation condition: a stream with any non-INFO correlation finding establishes nothing."""
    s = Stage.CONTRACT
    found = classify_messages(errors, s, request_id)
    if not end_seen:
        found.append(_f("CONTRACT_DETAILS_END_MISSING", FindingKind.INCOMPLETE, s,
                        "No contractDetailsEnd marker arrived; the set of matching contracts is not known to be "
                        "complete.", request_id))
    if not rows:
        if not any(m.code == 200 for m in errors):
            found.append(_f("CONTRACT_OBSERVATION_MISSING", FindingKind.MISSING, s,
                            "No contractDetails row answered the description.", request_id))
        return found, None
    if len(rows) > 1:
        found.append(_f("CONTRACT_IDENTITY_AMBIGUOUS", FindingKind.AMBIGUOUS, s,
                        f"{len(rows)} contractDetails rows answered one description; identity is not unique "
                        "and no row is chosen.", request_id))
        return found, None

    row, target = rows[0], expectation.target
    identity_fields = ("con_id", "symbol", "sec_type", "currency", "exchange", "primary_exchange")
    missing = [n for n in identity_fields + ("liquid_hours", "time_zone_id") if getattr(row, n) is None]
    if missing:
        found.append(_f("CONTRACT_FIELDS_MISSING", FindingKind.MISSING, s,
                        f"The contractDetails row lacks: {', '.join(missing)}.", request_id))
    comparisons = (
        ("CONTRACT_SYMBOL_MISMATCH", row.symbol, target.instrument.symbol, "symbol"),
        ("CONTRACT_SEC_TYPE_MISMATCH", row.sec_type, target.sec_type, "secType"),
        ("CONTRACT_CURRENCY_MISMATCH", row.currency, target.currency, "currency (USD basis)"),
        ("CONTRACT_EXCHANGE_MISMATCH", row.exchange, target.instrument.exchange, "routing exchange"),
        ("CONTRACT_PRIMARY_EXCHANGE_MISMATCH", row.primary_exchange, target.primary_exchange, "primary listing"),
    )
    for code, observed, expected, what in comparisons:
        if observed is not None and _label(observed) != _label(expected):
            found.append(_f(code, FindingKind.MISMATCH, s,
                            f"The observed {what} differs from the explicit expectation.", request_id))
    if row.con_id is not None:
        if target.con_id is None:
            found.append(_f("CON_ID_OBSERVED_NOT_PINNED", FindingKind.INFO, s,
                            "A conId was observed but the expectation pins none; it may be pinned only after a "
                            "connected check has learned it.", request_id))
        elif target.con_id != row.con_id:
            found.append(_f("CON_ID_MISMATCH", FindingKind.MISMATCH, s,
                            "The observed conId differs from the pinned expectation.", request_id))
    found.extend(_session_scope(row, now, request_id))

    identity_consistent = (
        end_seen
        and all(getattr(row, n) is not None for n in identity_fields)
        and not any(f.stage == s and f.kind in (FindingKind.MISMATCH, FindingKind.ERROR, FindingKind.INCOMPLETE)
                    for f in found)
    )
    if identity_consistent:
        found.append(_f("CONTRACT_IDENTITY_CONTROL", FindingKind.INFO, s,
                        "Exactly one contractDetails row matched the explicit USD STK description on every "
                        "identity field and the stream completed with contractDetailsEnd. Informational control "
                        "only; no contract was selected for trading.", request_id))
    return found, (row.con_id if identity_consistent else None)


def assess_quote(expectation: ReadinessExpectation, quotes: list[QuoteObservation],
                 data_types: list[MarketDataType], end_seen: bool, errors: list[ApiMessage], con_id: int,
                 *, now: datetime, policy: MockPolicy, request_id: int) -> list[Finding]:
    """Entitlement, live-versus-delayed, completeness, integrity and freshness of one snapshot."""
    s = Stage.QUOTE
    found = classify_messages(errors, s, request_id)
    if not end_seen:
        found.append(_f("TICK_SNAPSHOT_END_MISSING", FindingKind.INCOMPLETE, s,
                        "No tickSnapshotEnd marker arrived; the snapshot is not known to be complete.",
                        request_id))
    if len(set(data_types)) > 1:
        found.append(_f("MARKET_DATA_TYPE_CHANGED", FindingKind.AMBIGUOUS, s,
                        "More than one marketDataType was reported during the snapshot.", request_id))
    if not quotes:
        if not _has_problem(found, s):
            found.append(_f("QUOTE_OBSERVATION_MISSING", FindingKind.MISSING, s,
                            "No quote ticks were delivered for the snapshot.", request_id))
        return found
    if len(quotes) > 1:
        found.append(_f("QUOTE_OBSERVATIONS_MULTIPLE", FindingKind.AMBIGUOUS, s,
                        f"{len(quotes)} aggregate quotes answered one snapshot request.", request_id))
        return found
    q = quotes[0]

    mdt = data_types[-1] if data_types else q.market_data_type
    if data_types and q.market_data_type != MarketDataType.UNKNOWN and q.market_data_type != data_types[-1]:
        found.append(_f("MARKET_DATA_TYPE_INCONSISTENT", FindingKind.AMBIGUOUS, s,
                        "The marketDataType callback and the quote's own data type disagree.", request_id))
    delayed_ticks = sorted(DELAYED_TICK_TYPES & set(q.tick_types_received))
    if mdt == MarketDataType.UNKNOWN:
        found.append(_f("MARKET_DATA_TYPE_UNKNOWN", FindingKind.AMBIGUOUS, s,
                        "No marketDataType was reported; live versus delayed cannot be established.", request_id))
    elif mdt in (MarketDataType.DELAYED, MarketDataType.DELAYED_FROZEN) or delayed_ticks:
        found.append(_f("QUOTE_DELAYED", FindingKind.DELAYED, s,
                        f"Delayed-feed evidence: marketDataType {mdt.value}"
                        + (f", delayed tick types {delayed_ticks}" if delayed_ticks else "") + ".", request_id))
    elif mdt == MarketDataType.FROZEN:
        found.append(_f("QUOTE_FROZEN", FindingKind.STALE, s,
                        "marketDataType FROZEN: the last data recorded at market close, not a current market.",
                        request_id))

    if q.con_id is None:
        found.append(_f("QUOTE_CON_ID_MISSING", FindingKind.MISSING, s,
                        "The quote carries no conId; it cannot be tied to the matched contract.", request_id))
    elif q.con_id != con_id:
        found.append(_f("QUOTE_CON_ID_MISMATCH", FindingKind.MISMATCH, s,
                        "The quote's conId is not the matched contract's conId.", request_id))

    bid, ask = _decimal(q.bid), _decimal(q.ask)
    if q.bid is None or q.ask is None:
        found.append(_f("QUOTE_BID_ASK_MISSING", FindingKind.MISSING, s,
                        "Bid and/or ask price ticks (types 1/2) are absent.", request_id))
    elif bid is None or ask is None or bid <= 0 or ask <= 0 or ask < bid:
        found.append(_f("QUOTE_BID_ASK_INVALID", FindingKind.MISMATCH, s,
                        "Bid/ask are not positive decimals with ask >= bid (the gateway would block this as "
                        "INVALID_BID_ASK).", request_id))
    if q.bid_size is None or q.ask_size is None:
        found.append(_f("QUOTE_SIZES_MISSING", FindingKind.MISSING, s,
                        "Bid and/or ask size ticks (types 0/3) are absent.", request_id))

    if q.last_timestamp is None or q.last_timestamp.tzinfo is None:
        found.append(_f("QUOTE_UNTIMED", FindingKind.MISSING, s,
                        "No timezone-aware last-trade timestamp (tick 45) was delivered; freshness cannot be "
                        "judged.", request_id))
    else:
        state = freshness((now - q.last_timestamp).total_seconds(), policy.max_quote_age_seconds)
        if state == "STALE":
            found.append(_f("QUOTE_STALE", FindingKind.STALE, s,
                            "The last-trade timestamp is older than the shared policy quote window.", request_id))
        elif state == "FUTURE":
            found.append(_f("QUOTE_TIMESTAMP_FUTURE", FindingKind.MISMATCH, s,
                            "The last-trade timestamp lies in the future relative to the clock.", request_id))
    if q.received_at is None or q.received_at.tzinfo is None:
        found.append(_f("QUOTE_RECEIPT_UNTIMED", FindingKind.MISSING, s,
                        "The transport did not stamp a timezone-aware receipt time.", request_id))
    elif (q.received_at - now).total_seconds() > FUTURE_TOLERANCE_SECONDS:
        found.append(_f("QUOTE_RECEIPT_INCONSISTENT", FindingKind.MISMATCH, s,
                        "The receipt time lies in the future relative to the clock.", request_id))

    if q.halted in (1, 2):
        found.append(_f("INSTRUMENT_HALTED", FindingKind.OUT_OF_SCOPE, s,
                        "Tick 49 reports a halt; no readiness during a halt.", request_id))
    else:
        found.append(_f("HALT_STATUS_UNAVAILABLE" if q.halted in (None, -1) else "HALT_STATUS_CLEAR",
                        FindingKind.INFO, s,
                        "Halt status (tick 49) is unavailable, which the documentation describes as common; "
                        "recorded for the connected-check gate." if q.halted in (None, -1)
                        else "Tick 49 reports no halt.", request_id))
    if not _has_problem(found, s):
        found.append(_f("QUOTE_LIVE_CONTROL", FindingKind.INFO, s,
                        "One complete, live, timezone-aware snapshot inside the shared quote window for the "
                        "matched conId. Informational control only; no price was converted or used.", request_id))
    return found


def assess_account_summary(expectation: ReadinessExpectation, rows: list[AccountSummaryObservation],
                           end_seen: bool, errors: list[ApiMessage], *, request_id: int) -> list[Finding]:
    """Completeness and USD basis of the read-only account snapshot. Values are never rendered."""
    s = Stage.ACCOUNT
    found = classify_messages(errors, s, request_id)
    if not end_seen:
        found.append(_f("ACCOUNT_SUMMARY_END_MISSING", FindingKind.INCOMPLETE, s,
                        "No accountSummaryEnd marker arrived; the snapshot is not known to be complete.",
                        request_id))
    if not rows:
        found.append(_f("ACCOUNT_SUMMARY_MISSING", FindingKind.MISSING, s,
                        "No accountSummary rows were delivered.", request_id))
        return found
    if len(rows) > 1:
        found.append(_f("ACCOUNT_SUMMARY_MULTIPLE", FindingKind.AMBIGUOUS, s,
                        f"{len(rows)} account summaries answered one request.", request_id))
        return found
    row = rows[0]
    if row.account_id != expectation.account_id:
        found.append(_f("ACCOUNT_ID_MISMATCH", FindingKind.MISMATCH, s,
                        f"The summary names an account other than the expected {redact_account(expectation.account_id)}.",
                        request_id))
    if row.account_type is None:
        found.append(_f("ACCOUNT_TYPE_MISSING", FindingKind.MISSING, s,
                        "The AccountType tag is absent.", request_id))
    if row.currency is None:
        found.append(_f("ACCOUNT_CURRENCY_MISSING", FindingKind.MISSING, s,
                        "The summary states no currency.", request_id))
    elif _label(row.currency) != expectation.target.currency:
        found.append(_f("ACCOUNT_CURRENCY_MISMATCH", FindingKind.MISMATCH, s,
                        "The summary currency is not the expected USD basis.", request_id))
    missing_tags = [t for t in REQUIRED_ACCOUNT_TAGS if t not in row.values]
    if missing_tags:
        found.append(_f("ACCOUNT_TAGS_MISSING", FindingKind.MISSING, s,
                        f"Required tags absent: {', '.join(missing_tags)}.", request_id))
    unrecognised = [t for t in row.values if t not in DOCUMENTED_ACCOUNT_TAGS]
    if unrecognised:
        found.append(_f("ACCOUNT_TAGS_UNRECOGNIZED_WITHHELD", FindingKind.INFO, s,
                        f"{len(unrecognised)} tag name(s) outside the documented accountSummary allowlist were "
                        "withheld from diagnostics.", request_id))
    bad_known = sorted(t for t, v in row.values.items() if _decimal(v) is None and t in DOCUMENTED_ACCOUNT_TAGS)
    bad_unknown = sum(1 for t, v in row.values.items() if _decimal(v) is None and t not in DOCUMENTED_ACCOUNT_TAGS)
    if bad_known or bad_unknown:
        parts = []
        if bad_known:
            parts.append(f"documented tags {', '.join(bad_known)}")
        if bad_unknown:
            parts.append(f"{bad_unknown} unrecognised tag(s) (names withheld)")
        found.append(_f("ACCOUNT_VALUES_UNPARSEABLE", FindingKind.MISMATCH, s,
                        f"Values that are not finite decimals: {'; '.join(parts)}.", request_id))
    if not _has_problem(found, s):
        found.append(_f("ACCOUNT_SNAPSHOT_CONTROL", FindingKind.INFO, s,
                        "One complete USD account summary for the expected synthetic account. Values are "
                        "withheld from diagnostics. Informational control only.", request_id))
    return found


def assess_positions(expectation: ReadinessExpectation, lines: list[PositionLine], end_seen: bool,
                     errors: list[ApiMessage], *, request_id: int) -> list[Finding]:
    s = Stage.POSITIONS
    found = classify_messages(errors, s, request_id)
    if not end_seen:
        found.append(_f("POSITIONS_END_MISSING", FindingKind.INCOMPLETE, s,
                        "No positionEnd marker arrived; the position set is not known to be complete.",
                        request_id))
    foreign = sum(line.account_id != expectation.account_id for line in lines)
    if foreign:
        found.append(_f("POSITION_ACCOUNT_MISMATCH", FindingKind.MISMATCH, s,
                        f"{foreign} position line(s) belong to an account other than the expected one.",
                        request_id))
    incomplete = sum(line.con_id is None or _decimal(line.position) is None for line in lines)
    if incomplete:
        found.append(_f("POSITION_LINES_INCOMPLETE", FindingKind.MISSING, s,
                        f"{incomplete} position line(s) lack a conId or a decimal position.", request_id))
    if not _has_problem(found, s):
        found.append(_f("POSITIONS_SNAPSHOT_CONTROL", FindingKind.INFO, s,
                        f"Complete position snapshot with {len(lines)} line(s) for the expected synthetic account. "
                        "Informational control only.", request_id))
    return found
