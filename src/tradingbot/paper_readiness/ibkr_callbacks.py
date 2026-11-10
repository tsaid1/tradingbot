"""SDK-free normalization of documented IBKR callback sequences into readiness envelopes.

NON-AUTHORITATIVE. ``IbkrCallbackNormalizer`` receives the primitive values of the documented TWS API
``EWrapper`` callbacks for one request (``contractDetails``/``contractDetailsEnd`` or ``marketDataType``,
``tickPrice``/``tickSize``/``tickString``/``tickGeneric``/``tickSnapshotEnd``, plus ``error``) and turns
them into the existing ``ResponseEnvelope`` records, which the existing ``correlate`` and ``assess_*``
diagnostics judge unchanged. It imports no SDK, opens no socket, performs no request and has no
order, cancel, modification, account or credential method. It was exercised only with invented
callback values, and no compatibility with an installed SDK is claimed.

Rules (see docs/IBKR_CALLBACK_NORMALIZATION.md):

* Every callback keeps the request id it arrived with. A callback for another id, a callback of a
  kind foreign to this request, and anything after the END marker all become ordinary envelopes,
  so ``correlate`` classifies them as it already does (uncorrelated, unexpected kind, after END).
* Quote ticks for this request are held as one pending aggregate until the explicit
  ``tickSnapshotEnd``. Only then is ``received_at`` stamped from the injected clock. A tick type
  that repeats with a different value starts a second aggregate, which the existing diagnostics
  report as ambiguous. Nothing is discarded.
* Anything that cannot be represented faithfully (a malformed or wrongly typed value, a tick type
  delivered through the wrong callback, an unsupported callback, a non-OK session message, or more
  callbacks than the bound) is recorded in ``issues``. It also withholds the END envelope, so the
  existing END-missing classification makes the stream INCOMPLETE. There is no fallback.
* Well-typed values that are simply invalid (a non-positive or non-finite price, an unknown market
  data type) are carried as values the existing checks already reject.
* No environment, account or read-only status is inferred from anything here. Message text and
  ``advancedOrderRejectJson`` are never retained.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Protocol

from tradingbot.paper_readiness.diagnostics import SESSION_OK_CODES
from tradingbot.paper_readiness.observations import (
    MARKET_DATA_TYPE_CODES,
    ApiMessage,
    ContractObservation,
    MarketDataType,
    QuoteObservation,
    ResponseEnvelope,
    ResponseKind,
)

__all__ = [
    "DOCUMENTED_TICK_CALLBACKS",
    "MAX_CALLBACKS",
    "MAX_ISSUES",
    "SESSION_REQUEST_ID",
    "CallbackIssue",
    "CallbackRequest",
    "IbkrCallbackNormalizer",
    "IssueCode",
]

# Bounds. Beyond MAX_CALLBACKS every further callback is counted and ignored, and the stream is
# incomplete. The existing correlation bound (64 envelopes) still applies after normalization.
MAX_CALLBACKS = 256
MAX_ISSUES = 32
MAX_SESSION_MESSAGES = 32
MAX_TICK_TYPES = 64
MAX_TICK_TYPE = 999
MAX_TEXT = 256
MAX_HOURS_TEXT = 4096
# ``error`` uses -1 for messages that answer no request.
SESSION_REQUEST_ID = -1

# Documented tick types and the callback each is delivered through (tick_types.html).
DOCUMENTED_TICK_CALLBACKS = {
    0: "tickSize", 1: "tickPrice", 2: "tickPrice", 3: "tickSize", 4: "tickPrice", 5: "tickSize",
    45: "tickString", 49: "tickGeneric", 66: "tickPrice", 67: "tickPrice", 68: "tickPrice",
    69: "tickSize", 70: "tickSize", 88: "tickString",
}
# The tick types that populate a QuoteObservation field. Others are recorded in
# ``tick_types_received`` only, which is how delayed ticks 66/67/68/88 reach the existing check.
_PRICE_FIELDS = {1: "bid", 2: "ask", 4: "last"}
_SIZE_FIELDS = {0: "bid_size", 3: "ask_size"}
_LAST_TIMESTAMP = 45
_HALTED = 49
_UNIX_SECONDS_RE = re.compile(r"^[0-9]{1,12}$")
_CALLBACK_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")


class CallbackRequest(str, Enum):
    CONTRACT_DETAILS = "CONTRACT_DETAILS"
    QUOTE_SNAPSHOT = "QUOTE_SNAPSHOT"


_END_KIND = {CallbackRequest.CONTRACT_DETAILS: ResponseKind.CONTRACT_DETAILS_END,
             CallbackRequest.QUOTE_SNAPSHOT: ResponseKind.TICK_SNAPSHOT_END}


class IssueCode(str, Enum):
    MALFORMED_CALLBACK = "MALFORMED_CALLBACK"
    TICK_TYPE_CALLBACK_MISMATCH = "TICK_TYPE_CALLBACK_MISMATCH"
    TICK_TYPE_LIMIT_EXCEEDED = "TICK_TYPE_LIMIT_EXCEEDED"
    UNSUPPORTED_CALLBACK = "UNSUPPORTED_CALLBACK"
    SESSION_MESSAGE_DURING_REQUEST = "SESSION_MESSAGE_DURING_REQUEST"
    CALLBACK_LIMIT_EXCEEDED = "CALLBACK_LIMIT_EXCEEDED"
    CLOCK_INVALID = "CLOCK_INVALID"


@dataclass(frozen=True)
class CallbackIssue:
    """Why the END envelope is withheld. Carries a fixed code, a callback name and a request id only."""
    code: IssueCode
    callback: str
    request_id: int | None


class Clock(Protocol):
    def now(self) -> datetime: ...


class _Malformed(Exception):
    pass


def _is_int(value: object) -> bool:
    return type(value) is int


def _number(value: object) -> Decimal:
    """A documented ``double`` as a Decimal, which may be non-finite. Anything else is malformed."""
    if type(value) not in (int, float):
        raise _Malformed
    try:
        return Decimal(repr(value))
    except (InvalidOperation, ValueError):
        raise _Malformed from None


def _price(value: object) -> str:
    """Finite prices keep their value. A non-finite one is carried as NaN, which the check rejects."""
    number = _number(value)
    return str(number) if number.is_finite() else "NaN"


def _size(value: object) -> int:
    """A documented ``decimal`` size (an int in older APIs). It must be a non-negative whole number."""
    if _is_int(value):
        number = Decimal(value)
    elif type(value) is Decimal:
        number = value
    else:
        raise _Malformed
    if not number.is_finite() or number < 0 or number != number.to_integral_value():
        raise _Malformed
    return int(number)


def _text(value: object, limit: int = MAX_TEXT) -> str | None:
    """An empty string is IBKR's unset default and becomes None. Other types are malformed."""
    if type(value) is not str or len(value) > limit:
        raise _Malformed
    return value or None


def _con_id(value: object) -> int | None:
    if not _is_int(value) or value < 0:
        raise _Malformed
    return value or None  # 0 is the unset default


def _min_tick(value: object) -> str | None:
    number = _number(value)
    if not number.is_finite() or number < 0:
        raise _Malformed
    return str(number) if number > 0 else None


def _contract_row(details: object) -> ContractObservation:
    """One SDK-shaped ``ContractDetails`` (with its ``contract``) as a ContractObservation."""
    try:
        contract = details.contract
        values = {
            "con_id": _con_id(contract.conId),
            "symbol": _text(contract.symbol),
            "local_symbol": _text(contract.localSymbol),
            "sec_type": _text(contract.secType),
            "currency": _text(contract.currency),
            "exchange": _text(contract.exchange),
            "primary_exchange": _text(contract.primaryExchange),
            "long_name": _text(details.longName),
            "trading_hours": _text(details.tradingHours, MAX_HOURS_TEXT),
            "liquid_hours": _text(details.liquidHours, MAX_HOURS_TEXT),
            "time_zone_id": _text(details.timeZoneId),
            "min_tick": _min_tick(details.minTick),
        }
    except _Malformed:
        raise
    except Exception:  # noqa: BLE001 - a missing or failing attribute is a malformed payload
        raise _Malformed from None
    return ContractObservation(**values)


class _Pending:
    """One quote aggregate in progress: the sequence of its first tick and its field values."""

    def __init__(self, sequence: int, fields: dict[str, object]) -> None:
        self.sequence = sequence
        self.fields = fields


class IbkrCallbackNormalizer:
    """Normalizes the callbacks of exactly one contract-details or quote-snapshot request.

    Construct it before the request is sent and route every callback that arrives while the request
    is outstanding to the method of the same name. Route any other callback to
    ``unsupported_callback``. ``envelopes()`` can be read at any time and does not change state.
    """

    def __init__(self, request_id: int, request: CallbackRequest, *, con_id: int | None = None,
                 clock: Clock | None = None) -> None:
        if not _is_int(request_id) or request_id < 0:
            raise ValueError("request_id must be a non-negative int")
        request = CallbackRequest(request)
        if request is CallbackRequest.QUOTE_SNAPSHOT:
            if not _is_int(con_id) or con_id <= 0 or clock is None:
                raise ValueError("a quote snapshot needs the requested conId and a clock")
        elif con_id is not None or clock is not None:
            raise ValueError("a contract-details request takes no conId or clock")
        self.request_id = request_id
        self.request = request
        self.con_id = con_id
        self._clock = clock
        self._callbacks = 0
        self._ignored = 0
        self._records: list[ResponseEnvelope] = []
        self._pending: list[_Pending] = []
        self._tick_types: set[int] = set()
        self._end_sequence: int | None = None
        self._received_at: datetime | None = None
        self._issues: list[CallbackIssue] = []
        self._issue_overflow = 0
        self._session_messages: list[ApiMessage] = []

    # ------------------------------------------------------------------ bookkeeping

    @property
    def issues(self) -> tuple[CallbackIssue, ...]:
        return tuple(self._issues)

    @property
    def issue_overflow(self) -> int:
        return self._issue_overflow

    @property
    def ignored_callbacks(self) -> int:
        return self._ignored

    @property
    def session_messages(self) -> tuple[ApiMessage, ...]:
        """Codes of ``error`` callbacks with id -1. Recorded only; nothing is inferred from them."""
        return tuple(self._session_messages)

    @property
    def completed(self) -> bool:
        """The explicit END callback arrived and every callback was represented faithfully."""
        return self._end_sequence is not None and not self._issues and not self._issue_overflow

    def _issue(self, code: IssueCode, callback: str, request_id: object = None) -> None:
        rid = request_id if _is_int(request_id) else None
        if len(self._issues) < MAX_ISSUES:
            self._issues.append(CallbackIssue(code, callback, rid))
        else:
            self._issue_overflow += 1

    def _admit(self, callback: str) -> int | None:
        """The sequence number for this callback, or None once the bound is exhausted."""
        if self._callbacks >= MAX_CALLBACKS:
            if not self._ignored:
                self._issue(IssueCode.CALLBACK_LIMIT_EXCEEDED, callback)
            self._ignored += 1
            return None
        self._callbacks += 1
        return self._callbacks

    def _request_id(self, callback: str, value: object) -> int | None:
        if not _is_int(value) or value < 0:
            self._issue(IssueCode.MALFORMED_CALLBACK, callback)
            return None
        return value

    def _record(self, request_id: int, sequence: int, kind: ResponseKind, **payload) -> None:
        self._records.append(ResponseEnvelope(request_id=request_id, sequence=sequence, kind=kind,
                                              **payload))

    def _owns_end(self, request_id: int, request: CallbackRequest) -> bool:
        return (request_id == self.request_id and self.request is request
                and self._end_sequence is None)

    # ------------------------------------------------------------------ contract details

    def contractDetails(self, reqId: int, contractDetails: object) -> None:
        sequence = self._admit("contractDetails")
        if sequence is None:
            return
        rid = self._request_id("contractDetails", reqId)
        if rid is None:
            return
        try:
            row = _contract_row(contractDetails)
        except _Malformed:
            self._issue(IssueCode.MALFORMED_CALLBACK, "contractDetails", rid)
            return
        self._record(rid, sequence, ResponseKind.CONTRACT_DETAILS, contract=row)

    def contractDetailsEnd(self, reqId: int) -> None:
        sequence = self._admit("contractDetailsEnd")
        if sequence is None:
            return
        rid = self._request_id("contractDetailsEnd", reqId)
        if rid is None:
            return
        if self._owns_end(rid, CallbackRequest.CONTRACT_DETAILS):
            self._end_sequence = sequence
        else:
            self._record(rid, sequence, ResponseKind.CONTRACT_DETAILS_END)

    # ------------------------------------------------------------------ quote snapshot

    def marketDataType(self, reqId: int, marketDataType: int) -> None:
        sequence = self._admit("marketDataType")
        if sequence is None:
            return
        rid = self._request_id("marketDataType", reqId)
        if rid is None:
            return
        if not _is_int(marketDataType):
            self._issue(IssueCode.MALFORMED_CALLBACK, "marketDataType", rid)
            return
        kind = MARKET_DATA_TYPE_CODES.get(marketDataType, MarketDataType.UNKNOWN)
        self._record(rid, sequence, ResponseKind.MARKET_DATA_TYPE, market_data_type=kind)

    def tickPrice(self, reqId: int, tickType: int, price: float, attrib: object) -> None:
        """``attrib`` (TickAttrib) is accepted and not interpreted."""
        self._tick("tickPrice", reqId, tickType, price)

    def tickSize(self, reqId: int, tickType: int, size: object) -> None:
        self._tick("tickSize", reqId, tickType, size)

    def tickString(self, reqId: int, tickType: int, value: str) -> None:
        self._tick("tickString", reqId, tickType, value)

    def tickGeneric(self, reqId: int, tickType: int, value: float) -> None:
        self._tick("tickGeneric", reqId, tickType, value)

    def _tick_field(self, callback: str, tick_type: int, value: object) -> tuple[str | None, object]:
        if callback == "tickPrice":
            _number(value)
            return _PRICE_FIELDS.get(tick_type), _price(value)
        if callback == "tickSize":
            return _SIZE_FIELDS.get(tick_type), _size(value)
        if callback == "tickString":
            _text(value)
            if tick_type != _LAST_TIMESTAMP:
                return None, None
            if not _UNIX_SECONDS_RE.fullmatch(value):
                raise _Malformed
            try:
                return "last_timestamp", datetime.fromtimestamp(int(value), UTC)
            except (OverflowError, OSError, ValueError):
                raise _Malformed from None
        number = _number(value)
        if tick_type != _HALTED:
            return None, None
        if number not in (-1, 0, 1, 2):
            raise _Malformed
        return "halted", int(number)

    def _tick(self, callback: str, reqId: object, tickType: object, value: object) -> None:
        sequence = self._admit(callback)
        if sequence is None:
            return
        rid = self._request_id(callback, reqId)
        if rid is None:
            return
        if not _is_int(tickType) or not 0 <= tickType <= MAX_TICK_TYPE:
            self._issue(IssueCode.MALFORMED_CALLBACK, callback, rid)
            return
        documented = DOCUMENTED_TICK_CALLBACKS.get(tickType)
        if documented is not None and documented != callback:
            self._issue(IssueCode.TICK_TYPE_CALLBACK_MISMATCH, callback, rid)
            return
        try:
            name, converted = self._tick_field(callback, tickType, value)
        except _Malformed:
            self._issue(IssueCode.MALFORMED_CALLBACK, callback, rid)
            return
        fields = {} if name is None else {name: converted}
        if (rid != self.request_id or self.request is not CallbackRequest.QUOTE_SNAPSHOT
                or self._end_sequence is not None):
            # Kept as its own record, so correlation classifies it as stray, foreign or after END.
            self._record(rid, sequence, ResponseKind.QUOTE,
                         quote=QuoteObservation(tick_types_received=[tickType], **fields))
            return
        if tickType not in self._tick_types and len(self._tick_types) >= MAX_TICK_TYPES:
            self._issue(IssueCode.TICK_TYPE_LIMIT_EXCEEDED, callback, rid)
            return
        self._tick_types.add(tickType)
        current = self._pending[-1] if self._pending else None
        if current is None:
            # Opened by any tick, so a snapshot of only recorded types (delayed ticks) still reaches
            # the existing checks through ``tick_types_received``.
            self._pending.append(_Pending(sequence, fields))
        elif name is None:
            return
        elif name not in current.fields:
            current.fields[name] = converted
        elif current.fields[name] != converted:
            # A conflicting value is never overwritten: a second aggregate makes the snapshot ambiguous.
            self._pending.append(_Pending(sequence, {**current.fields, name: converted}))

    def tickSnapshotEnd(self, reqId: int) -> None:
        sequence = self._admit("tickSnapshotEnd")
        if sequence is None:
            return
        rid = self._request_id("tickSnapshotEnd", reqId)
        if rid is None:
            return
        if not self._owns_end(rid, CallbackRequest.QUOTE_SNAPSHOT):
            self._record(rid, sequence, ResponseKind.TICK_SNAPSHOT_END)
            return
        self._end_sequence = sequence
        try:
            stamped = self._clock.now()
        except Exception:  # noqa: BLE001 - an unusable clock leaves the receipt unstamped
            stamped = None
        if isinstance(stamped, datetime):
            self._received_at = stamped
        else:
            self._issue(IssueCode.CLOCK_INVALID, "tickSnapshotEnd", rid)

    # ------------------------------------------------------------------ messages and the rest

    def error(self, reqId: int, errorCode: int, errorString: str,
              advancedOrderRejectJson: str = "") -> None:
        """The documented four-argument form. The text arguments are never read or retained."""
        sequence = self._admit("error")
        if sequence is None:
            return
        if not _is_int(errorCode) or errorCode < 0 or not _is_int(reqId):
            self._issue(IssueCode.MALFORMED_CALLBACK, "error", reqId)
            return
        if reqId == SESSION_REQUEST_ID:
            if len(self._session_messages) < MAX_SESSION_MESSAGES:
                self._session_messages.append(ApiMessage(code=errorCode))
            if errorCode not in SESSION_OK_CODES:
                self._issue(IssueCode.SESSION_MESSAGE_DURING_REQUEST, "error")
            return
        if reqId < 0:
            self._issue(IssueCode.MALFORMED_CALLBACK, "error")
            return
        self._record(reqId, sequence, ResponseKind.ERROR,
                     message=ApiMessage(code=errorCode, request_id=reqId))

    def unsupported_callback(self, name: str, reqId: int | None = None) -> None:
        """Any other callback while the request is outstanding. Recorded by name, never interpreted."""
        if self._admit("unsupported") is None:
            return
        label = name if type(name) is str and _CALLBACK_NAME_RE.fullmatch(name) else "INVALID_NAME"
        self._issue(IssueCode.UNSUPPORTED_CALLBACK, label, reqId)

    # ------------------------------------------------------------------ output

    def envelopes(self) -> list[ResponseEnvelope]:
        """Every record so far, in callback order. The END envelope appears only if ``completed``."""
        out = list(self._records)
        complete = self.completed
        tick_types = sorted(self._tick_types)
        for pending in self._pending:
            quote = QuoteObservation(con_id=self.con_id, tick_types_received=tick_types,
                                     received_at=self._received_at if complete else None,
                                     **pending.fields)
            out.append(ResponseEnvelope(request_id=self.request_id, sequence=pending.sequence,
                                        kind=ResponseKind.QUOTE, quote=quote))
        if complete:
            out.append(ResponseEnvelope(request_id=self.request_id, sequence=self._end_sequence,
                                        kind=_END_KIND[self.request]))
        return sorted(out, key=lambda e: e.sequence)
