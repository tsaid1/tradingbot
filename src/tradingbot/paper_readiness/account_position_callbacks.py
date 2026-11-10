"""Offline normalization of account-summary and position callbacks. NON_AUTHORITATIVE.

``AccountSummaryCallbacks`` and ``PositionSnapshotCallbacks`` turn the primitive values of the
documented TWS API callbacks ``accountSummary``/``accountSummaryEnd`` and ``position``/``positionEnd``
into the existing ``ResponseEnvelope`` records, which the unchanged ``correlate``,
``assess_account_summary`` and ``assess_positions`` judge as they already do. Nothing here imports an
SDK, connects, requests anything or reads an account. It was exercised only with invented values, and
no installed-SDK or real-account compatibility is claimed.

Only explicitly synthetic ``SYNTH-...`` account identities are representable in this offline scope.
Any other account value is refused with a fixed code and never stored, so no real identifier reaches a
record, issue or report, and nothing is converted into a synthetic one. Nothing here infers PAPER or
provenance. See docs/ACCOUNT_POSITION_CALLBACKS.md.

Rules:

* account-summary rows keep their broker request id; another id or anything after END stays a
  visible record, so correlation classifies it as it already does
* positions carry no broker request id. One ``PositionSnapshotCallbacks`` owns one local snapshot,
  and its id is a local correlation id, never a broker one
* anything that cannot be represented faithfully (a malformed or non-synthetic value, a duplicate or
  conflicting row, an unsupported callback, more than the bounds) is recorded in ``issues`` and
  withholds every END envelope, including a repeated END, so the stream stays INCOMPLETE
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum

from tradingbot.paper_readiness.diagnostics import REQUIRED_ACCOUNT_TAGS
from tradingbot.paper_readiness.observations import (
    SYNTHETIC_ACCOUNT_RE,
    AccountSummaryObservation,
    PositionLine,
    ResponseEnvelope,
    ResponseKind,
)

__all__ = [
    "MAX_ACCOUNTS",
    "MAX_CALLBACKS",
    "MAX_ISSUES",
    "MAX_POSITION_ROWS",
    "MAX_TAGS_PER_ACCOUNT",
    "AccountIssue",
    "AccountIssueCode",
    "AccountSummaryCallbacks",
    "PositionSnapshotCallbacks",
]

MAX_CALLBACKS = 256
MAX_ISSUES = 32
MAX_ACCOUNTS = 8
MAX_TAGS_PER_ACCOUNT = 64
MAX_POSITION_ROWS = 64
MAX_TEXT = 64
MAX_ID = 2**31 - 1            # documented ids are 32-bit ints
MAX_QUANTITY_DIGITS = 30      # bounded before any int or Decimal is converted to text
ACCOUNT_TYPE_TAG = "AccountType"
_TAG_RE = re.compile(r"^[A-Za-z$][A-Za-z0-9$._-]{0,63}$")
_CALLBACK_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")


class AccountIssueCode(str, Enum):
    MALFORMED_CALLBACK = "MALFORMED_CALLBACK"
    ACCOUNT_NOT_SYNTHETIC = "ACCOUNT_NOT_SYNTHETIC"
    DUPLICATE_ROW = "DUPLICATE_ROW"
    CONFLICTING_CURRENCY = "CONFLICTING_CURRENCY"
    ROW_LIMIT_EXCEEDED = "ROW_LIMIT_EXCEEDED"
    CALLBACK_LIMIT_EXCEEDED = "CALLBACK_LIMIT_EXCEEDED"
    UNSUPPORTED_CALLBACK = "UNSUPPORTED_CALLBACK"


@dataclass(frozen=True)
class AccountIssue:
    """Why END is withheld: a fixed code and callback name only, never a value."""
    code: AccountIssueCode
    callback: str


class _Malformed(Exception):
    pass


def _is_int(value: object) -> bool:
    return type(value) is int


def _is_id(value: object) -> bool:
    """An exact non-negative int within the documented 32-bit range, checked without conversion."""
    return _is_int(value) and 0 <= value <= MAX_ID


def _text(value: object, *, empty_is_none: bool = True) -> str | None:
    if type(value) is not str or len(value) > MAX_TEXT:
        raise _Malformed
    if any(ord(ch) < 0x20 or 0x7F <= ord(ch) <= 0x9F for ch in value):
        raise _Malformed
    return (value or None) if empty_is_none else value


def _synthetic_account(value: object) -> str | None:
    """The account if it is an exact synthetic identifier; None if it is any other string."""
    if type(value) is not str or len(value) > MAX_TEXT:
        raise _Malformed
    return value if SYNTHETIC_ACCOUNT_RE.fullmatch(value) else None


class _Stream:
    """Bookkeeping shared by both streams: bounded callbacks, closed issues, END withholding."""

    _end_kind: ResponseKind

    def __init__(self, request_id: int) -> None:
        if not _is_id(request_id):
            raise ValueError("request_id must be a non-negative 32-bit int")
        self.request_id = request_id
        self._callbacks = 0
        self._ignored = 0
        self._issues: list[AccountIssue] = []
        self._issue_overflow = 0
        self._records: list[ResponseEnvelope] = []
        self._end_sequence: int | None = None

    @property
    def issues(self) -> tuple[AccountIssue, ...]:
        return tuple(self._issues)

    @property
    def issue_overflow(self) -> int:
        return self._issue_overflow

    @property
    def ignored_callbacks(self) -> int:
        return self._ignored

    @property
    def completed(self) -> bool:
        """The explicit END arrived and every callback was represented faithfully."""
        return self._end_sequence is not None and not self._issues and not self._issue_overflow

    def _issue(self, code: AccountIssueCode, callback: str) -> None:
        if len(self._issues) < MAX_ISSUES:
            self._issues.append(AccountIssue(code, callback))
        else:
            self._issue_overflow += 1

    def _admit(self, callback: str) -> int | None:
        if self._callbacks >= MAX_CALLBACKS:
            if not self._ignored:
                self._issue(AccountIssueCode.CALLBACK_LIMIT_EXCEEDED, callback)
            self._ignored += 1
            return None
        self._callbacks += 1
        return self._callbacks

    def unsupported_callback(self, name: str) -> None:
        """Any other callback while the stream is outstanding, including errors. Never interpreted."""
        if self._admit("unsupported") is None:
            return
        label = name if type(name) is str and _CALLBACK_NAME_RE.fullmatch(name) else "INVALID_NAME"
        self._issue(AccountIssueCode.UNSUPPORTED_CALLBACK, label)

    def _own(self) -> list[ResponseEnvelope]:
        raise NotImplementedError

    def envelopes(self) -> list[ResponseEnvelope]:
        """Every record so far in callback order. No END for this stream appears unless ``completed``."""
        complete = self.completed
        out = [e for e in self._records
               if complete or not (e.request_id == self.request_id and e.kind == self._end_kind)]
        out.extend(self._own())
        if complete:
            out.append(ResponseEnvelope(request_id=self.request_id, sequence=self._end_sequence,
                                        kind=self._end_kind))
        return sorted(out, key=lambda e: e.sequence)


class _SummaryAccount:
    def __init__(self, sequence: int) -> None:
        self.sequence = sequence
        self.account_type: str | None = None
        self.currencies: set[str] = set()
        self.currency_missing = False  # a required monetary row arrived without its currency
        self.values: dict[str, str] = {}
        self.tags: set[str] = set()


class AccountSummaryCallbacks(_Stream):
    """One ``reqAccountSummary`` request, correlated by its broker request id."""

    _end_kind = ResponseKind.ACCOUNT_SUMMARY_END

    def __init__(self, request_id: int) -> None:
        super().__init__(request_id)
        self._accounts: dict[str, _SummaryAccount] = {}

    def accountSummary(self, reqId: int, account: str, tag: str, value: str,
                       currency: str) -> None:
        sequence = self._admit("accountSummary")
        if sequence is None:
            return
        try:
            if not _is_id(reqId):
                raise _Malformed
            synthetic = _synthetic_account(account)
            if type(tag) is not str or not _TAG_RE.fullmatch(tag):
                raise _Malformed
            row_value = _text(value, empty_is_none=False)
            row_currency = _text(currency)
        except _Malformed:
            self._issue(AccountIssueCode.MALFORMED_CALLBACK, "accountSummary")
            return
        if synthetic is None:
            self._issue(AccountIssueCode.ACCOUNT_NOT_SYNTHETIC, "accountSummary")
            return
        if reqId != self.request_id or self._end_sequence is not None:
            # Visible on its own, so correlation reports it as stray or after END.
            row = AccountSummaryObservation(account_id=synthetic, values={tag: row_value})
            self._records.append(ResponseEnvelope(request_id=reqId, sequence=sequence,
                                                  kind=ResponseKind.ACCOUNT_SUMMARY, account_summary=row))
            return
        state = self._accounts.get(synthetic)
        if state is None:
            if len(self._accounts) >= MAX_ACCOUNTS:
                self._issue(AccountIssueCode.ROW_LIMIT_EXCEEDED, "accountSummary")
                return
            state = self._accounts[synthetic] = _SummaryAccount(sequence)
        if tag in state.tags:
            self._issue(AccountIssueCode.DUPLICATE_ROW, "accountSummary")
            return
        if len(state.tags) >= MAX_TAGS_PER_ACCOUNT:
            self._issue(AccountIssueCode.ROW_LIMIT_EXCEEDED, "accountSummary")
            return
        state.tags.add(tag)
        if tag == ACCOUNT_TYPE_TAG:
            state.account_type = row_value or None
            return
        state.values[tag] = row_value
        if row_currency is None and tag in REQUIRED_ACCOUNT_TAGS:
            # Each row's currency describes only its own value; one row cannot vouch for another.
            state.currency_missing = True
        if row_currency is not None:
            state.currencies.add(row_currency)
            if len(state.currencies) > 1:
                self._issue(AccountIssueCode.CONFLICTING_CURRENCY, "accountSummary")

    def accountSummaryEnd(self, reqId: int) -> None:
        sequence = self._admit("accountSummaryEnd")
        if sequence is None:
            return
        if not _is_id(reqId):
            self._issue(AccountIssueCode.MALFORMED_CALLBACK, "accountSummaryEnd")
            return
        if reqId == self.request_id and self._end_sequence is None:
            self._end_sequence = sequence
        else:
            self._records.append(ResponseEnvelope(request_id=reqId, sequence=sequence,
                                                  kind=ResponseKind.ACCOUNT_SUMMARY_END))

    def _own(self) -> list[ResponseEnvelope]:
        out = []
        for account, state in self._accounts.items():
            known = len(state.currencies) == 1 and not state.currency_missing
            currency = next(iter(state.currencies)) if known else None
            row = AccountSummaryObservation(account_id=account, account_type=state.account_type,
                                            currency=currency, values=dict(state.values))
            out.append(ResponseEnvelope(request_id=self.request_id, sequence=state.sequence,
                                        kind=ResponseKind.ACCOUNT_SUMMARY, account_summary=row))
        return out


def _position_quantity(value: object) -> str:
    """A documented ``decimal`` (or a whole ``int`` from older APIs); fractional and negative kept.

    Magnitude is bounded before any conversion to text, so an oversized value is malformed and can
    never raise out of the callback."""
    if _is_int(value):
        if abs(value) >= 10**MAX_QUANTITY_DIGITS:
            raise _Malformed
    elif type(value) is Decimal and value.is_finite():
        if value and value.adjusted() >= MAX_QUANTITY_DIGITS:
            raise _Malformed
    else:
        raise _Malformed
    try:
        return str(value)
    except (ValueError, OverflowError):
        raise _Malformed from None


def _average_cost(value: object) -> str:
    """A documented ``double``; non-finite values and every non-number are malformed."""
    if type(value) not in (int, float):
        raise _Malformed
    try:
        number = Decimal(repr(value))
    except (InvalidOperation, ValueError):
        raise _Malformed from None
    if not number.is_finite():
        raise _Malformed
    return str(number)


def _con_id(value: object) -> int | None:
    if not _is_id(value):
        raise _Malformed
    return value or None  # 0 is the unset default


class PositionSnapshotCallbacks(_Stream):
    """One local positions snapshot. ``position``/``positionEnd`` carry no broker request id, so every
    position routed here belongs to this snapshot, and ``snapshot_id`` is a local id only."""

    _end_kind = ResponseKind.POSITION_END

    def __init__(self, snapshot_id: int) -> None:
        super().__init__(snapshot_id)
        self._rows: set[tuple[str, int]] = set()
        self._count = 0

    def position(self, account: str, contract: object, pos: object, avgCost: object) -> None:
        sequence = self._admit("position")
        if sequence is None:
            return
        try:
            synthetic = _synthetic_account(account)
            try:
                con_id = _con_id(contract.conId)
                symbol = _text(contract.symbol)
                sec_type = _text(contract.secType)
                currency = _text(contract.currency)
            except _Malformed:
                raise
            except Exception:  # noqa: BLE001 - a missing or failing attribute is a malformed contract
                raise _Malformed from None
            quantity = _position_quantity(pos)
            cost = _average_cost(avgCost)
        except _Malformed:
            self._issue(AccountIssueCode.MALFORMED_CALLBACK, "position")
            return
        if synthetic is None:
            self._issue(AccountIssueCode.ACCOUNT_NOT_SYNTHETIC, "position")
            return
        if self._end_sequence is None:
            if self._count >= MAX_POSITION_ROWS:
                self._issue(AccountIssueCode.ROW_LIMIT_EXCEEDED, "position")
                return
            if con_id is not None:
                key = (synthetic, con_id)
                if key in self._rows:
                    self._issue(AccountIssueCode.DUPLICATE_ROW, "position")
                    return
                self._rows.add(key)
            self._count += 1
        line = PositionLine(account_id=synthetic, con_id=con_id, symbol=symbol, sec_type=sec_type,
                            currency=currency, position=quantity, avg_cost=cost)
        # A row after END is kept as its own record, so correlation reports it as after END.
        self._records.append(ResponseEnvelope(request_id=self.request_id, sequence=sequence,
                                              kind=ResponseKind.POSITION, position=line))

    def positionEnd(self) -> None:
        sequence = self._admit("positionEnd")
        if sequence is None:
            return
        if self._end_sequence is None:
            self._end_sequence = sequence
        else:
            self._records.append(ResponseEnvelope(request_id=self.request_id, sequence=sequence,
                                                  kind=ResponseKind.POSITION_END))

    def _own(self) -> list[ResponseEnvelope]:
        return []
