"""SDK-free normalization of IBKR account-summary and position callbacks. NON-AUTHORITATIVE.

An injected-value adapter for the offline readiness scaffold. A future read-only transport would
forward the documented EWrapper callbacks ``accountSummary``/``accountSummaryEnd`` and
``position``/``positionEnd`` to these collectors. They turn one bounded snapshot into the existing
``ResponseEnvelope`` records, which ``runner.correlate`` and
``diagnostics.assess_account_summary``/``assess_positions`` already judge.

This module imports no broker SDK, opens no socket, dispatches no request, holds no credential and
infers no session readiness. It is tested with invented synthetic accounts and events only, and it
proves nothing about an installed SDK or a connected session. Interface notes and sources are in
docs/IBKR_ACCOUNT_CALLBACK_NORMALIZATION.md.

Every rule fails closed:

* Values are kept verbatim. Decimal strings are not parsed or rewritten here, so the existing
  assessments stay their single judge. Only the average cost, which no assessment reads, is checked.
* What the envelope records can represent faithfully is emitted as it arrived. A foreign request id
  keeps its own id, another account keeps its own observation, a missing END stays missing, and
  anything after END stays after it. Correlation and assessment then report each as a non-success.
* What they cannot represent faithfully makes the snapshot ``UNSUPPORTED``, with no envelopes at
  all: conflicting duplicates, an argument of another type, an oversized text, a non-positive conId,
  a non-finite average cost, a non-synthetic account, or more callbacks than the per-request bound.
* ``position`` and ``positionEnd`` carry no request id. A positions collector therefore labels its
  envelopes with a *local* request id and is valid only while it is the one active positions
  snapshot. It never claims broker correlation, and it cannot detect a second concurrent
  positions subscription.

Findings name fixed codes, counts and documented tag names only, never an account or a value.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from tradingbot.paper_readiness.diagnostics import (
    DOCUMENTED_ACCOUNT_TAGS,
    Finding,
    FindingKind,
    Stage,
)
from tradingbot.paper_readiness.observations import (
    SYNTHETIC_ACCOUNT_RE,
    AccountSummaryObservation,
    PositionLine,
    ResponseEnvelope,
    ResponseKind,
)
from tradingbot.paper_readiness.runner import MAX_RESPONSES_PER_REQUEST

__all__ = [
    "ACCOUNT_TYPE_TAG",
    "MAX_CALLBACKS_PER_SNAPSHOT",
    "MAX_TEXT_CHARS",
    "AccountSummaryCollector",
    "CallbackSnapshot",
    "Correlation",
    "PositionSnapshotCollector",
    "SnapshotStatus",
]

# The existing per-request response bound, applied to callbacks. END and later callbacks count too.
MAX_CALLBACKS_PER_SNAPSHOT = MAX_RESPONSES_PER_REQUEST
# A finite bound on every text argument and on each decimal rendered as text.
MAX_TEXT_CHARS = 256
# Held separately in ``AccountSummaryObservation.account_type``, as the existing records expect.
ACCOUNT_TYPE_TAG = "AccountType"


class SnapshotStatus(str, Enum):
    """What the collector received. Not a readiness judgement: correlation and assessment decide."""

    END_RECEIVED = "END_RECEIVED"
    END_NOT_RECEIVED = "END_NOT_RECEIVED"
    UNSUPPORTED = "UNSUPPORTED"  # nothing was normalized and no envelope is returned


class Correlation(str, Enum):
    BROKER_REQUEST_ID = "BROKER_REQUEST_ID"  # each envelope carries the reqId its callback named
    LOCAL_SINGLE_ACTIVE_SNAPSHOT = "LOCAL_SINGLE_ACTIVE_SNAPSHOT"  # the callback has no reqId


@dataclass(frozen=True)
class CallbackSnapshot:
    stage: Stage
    request_id: int
    correlation: Correlation
    status: SnapshotStatus
    envelopes: tuple[ResponseEnvelope, ...]
    findings: tuple[Finding, ...]


_UNSUPPORTED = {
    "CALLBACK_LIMIT_EXCEEDED": (
        FindingKind.INCOMPLETE,
        f"More than {MAX_CALLBACKS_PER_SNAPSHOT} callbacks arrived for one snapshot.",
    ),
    "CALLBACK_ARGUMENT_UNSUPPORTED": (
        FindingKind.MISMATCH,
        (f"A callback argument was not of its documented type, or a text exceeded "
         f"{MAX_TEXT_CHARS} characters."),
    ),
    "CALLBACK_ACCOUNT_NOT_SYNTHETIC": (
        FindingKind.MISMATCH,
        "A callback named an account that is not explicitly synthetic. It was not retained.",
    ),
    "CALLBACK_CONTRACT_ID_UNSUPPORTED": (
        FindingKind.MISMATCH,
        "A position named no positive integer conId.",
    ),
    "CALLBACK_AVERAGE_COST_NOT_FINITE": (
        FindingKind.MISMATCH,
        "A position's average cost was not a finite number.",
    ),
    "CALLBACK_CURRENCY_CONFLICT": (
        FindingKind.MISMATCH,
        "One account's summary rows named more than one currency.",
    ),
    "CALLBACK_POSITION_CONFLICT": (
        FindingKind.MISMATCH,
        "One position identity (account and conId) arrived twice with different content.",
    ),
}
_MISSING = object()


def _request_id(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _text(value: object) -> str | None:
    return value if type(value) is str and len(value) <= MAX_TEXT_CHARS else None


def _number_text(value: object) -> str | None:
    """An int or Decimal as its exact text. A float is refused: the documented type is decimal."""
    if type(value) is int or type(value) is Decimal:
        return _text(str(value))
    return None


class _Collector:
    _stage: Stage
    correlation: Correlation

    def __init__(self, request_id: int) -> None:
        if _request_id(request_id) is None:
            raise ValueError("request_id must be a non-negative int")
        self.request_id = request_id
        self._events: list[tuple] = []
        self._end_seen = False
        self._callbacks = 0
        self._refusals: list[str] = []

    def _admit(self) -> bool:
        """Count every callback against the bound. After a refusal nothing more is stored."""
        self._callbacks += 1
        if self._callbacks > MAX_CALLBACKS_PER_SNAPSHOT:
            self._refuse("CALLBACK_LIMIT_EXCEEDED")
        return not self._refusals

    def _refuse(self, code: str) -> None:
        if code not in self._refusals:
            self._refusals.append(code)

    def _finding(self, code: str, kind: FindingKind, detail: str) -> Finding:
        return Finding(code=code, kind=kind, stage=self._stage, detail=detail,
                       request_id=self.request_id)

    def _result(self, status: SnapshotStatus, envelopes: list[ResponseEnvelope],
                findings: list[Finding]) -> CallbackSnapshot:
        return CallbackSnapshot(stage=self._stage, request_id=self.request_id,
                                correlation=self.correlation, status=status,
                                envelopes=tuple(envelopes), findings=tuple(findings))

    def _unsupported(self, codes: list[str],
                     extra: list[Finding] | None = None) -> CallbackSnapshot:
        """A pure view: building a snapshot never changes what the collector accepts next."""
        findings = [self._finding(code, *_UNSUPPORTED[code]) for code in codes]
        findings.extend(extra or [])
        findings.append(self._finding("CALLBACK_SNAPSHOT_UNSUPPORTED", FindingKind.MISMATCH,
                                      "Nothing was normalized and no envelope was returned."))
        return self._result(SnapshotStatus.UNSUPPORTED, [], findings)

    def _status(self) -> SnapshotStatus:
        return SnapshotStatus.END_RECEIVED if self._end_seen else SnapshotStatus.END_NOT_RECEIVED

    def _duplicates(self, count: int) -> list[Finding]:
        if not count:
            return []
        return [self._finding("CALLBACK_DUPLICATE_DROPPED", FindingKind.INFO,
                              f"{count} exact duplicate callback(s) were dropped.")]


class AccountSummaryCollector(_Collector):
    """The ``accountSummary``/``accountSummaryEnd`` callbacks of one ``reqAccountSummary`` request.

    Rows aggregate into one ``AccountSummaryObservation`` per request id, account and side of this
    request's END marker, placed at the arrival of the group's first row. Each envelope keeps the
    request id its callback named, so a foreign id is never relabelled.
    """

    _stage = Stage.ACCOUNT
    correlation = Correlation.BROKER_REQUEST_ID

    def accountSummary(self, reqId: object, account: object, tag: object, value: object,
                       currency: object) -> None:
        if not self._admit():
            return
        request_id = _request_id(reqId)
        texts = [_text(account), _text(tag), _text(value), _text(currency)]
        if request_id is None or any(item is None for item in texts):
            self._refuse("CALLBACK_ARGUMENT_UNSUPPORTED")
            return
        if not SYNTHETIC_ACCOUNT_RE.fullmatch(account):
            self._refuse("CALLBACK_ACCOUNT_NOT_SYNTHETIC")
            return
        self._events.append(("ROW", request_id, account, tag, value, currency, self._end_seen))

    def accountSummaryEnd(self, reqId: object) -> None:
        if not self._admit():
            return
        request_id = _request_id(reqId)
        if request_id is None:
            self._refuse("CALLBACK_ARGUMENT_UNSUPPORTED")
            return
        self._events.append(("END", request_id))
        if request_id == self.request_id:
            self._end_seen = True

    def snapshot(self) -> CallbackSnapshot:
        if self._refusals:
            return self._unsupported(self._refusals)
        groups: dict[tuple, dict[str, tuple[str, str]]] = {}
        order: list[tuple] = []
        duplicates = 0
        conflicts: set[str] = set()
        for event in self._events:
            if event[0] == "END":
                order.append(event)
                continue
            _, request_id, account, tag, value, currency, after_end = event
            key = (request_id, account, after_end)
            if key not in groups:
                groups[key] = {}
                order.append(("ROWS", key))
            prior = groups[key].get(tag)
            if prior is None:
                groups[key][tag] = (value, currency)
            elif prior == (value, currency):
                duplicates += 1
            else:
                conflicts.add(tag)
        currency_conflict = any(len(_currencies(rows)) > 1 for rows in groups.values())
        if conflicts or currency_conflict:
            codes = ["CALLBACK_CURRENCY_CONFLICT"] if currency_conflict else []
            return self._unsupported(codes, self._tag_conflict(conflicts))
        envelopes = []
        for sequence, item in enumerate(order):
            if item[0] == "END":
                envelopes.append(ResponseEnvelope(request_id=item[1], sequence=sequence,
                                                  kind=ResponseKind.ACCOUNT_SUMMARY_END))
                continue
            request_id, account, _after_end = item[1]
            rows = groups[item[1]]
            currencies = _currencies(rows)
            observation = AccountSummaryObservation(
                account_id=account,
                account_type=rows[ACCOUNT_TYPE_TAG][0] if ACCOUNT_TYPE_TAG in rows else None,
                currency=currencies[0] if currencies else None,
                values={tag: value for tag, (value, _c) in rows.items() if tag != ACCOUNT_TYPE_TAG},
            )
            envelopes.append(ResponseEnvelope(request_id=request_id, sequence=sequence,
                                              kind=ResponseKind.ACCOUNT_SUMMARY,
                                              account_summary=observation))
        return self._result(self._status(), envelopes, self._duplicates(duplicates))

    def _tag_conflict(self, tags: set[str]) -> list[Finding]:
        if not tags:
            return []
        named = sorted(tags & DOCUMENTED_ACCOUNT_TAGS)
        withheld = len(tags - DOCUMENTED_ACCOUNT_TAGS)
        parts = [f"documented tag(s) {', '.join(named)}"] if named else []
        if withheld:
            parts.append(f"{withheld} undocumented tag(s) (names withheld)")
        return [self._finding("CALLBACK_TAG_CONFLICT", FindingKind.MISMATCH,
                              "One account's tag arrived twice with a different value or currency: "
                              f"{'; '.join(parts)}.")]


def _currencies(rows: dict[str, tuple[str, str]]) -> list[str]:
    return sorted({currency for _value, currency in rows.values() if currency})


class PositionSnapshotCollector(_Collector):
    """The ``position``/``positionEnd`` callbacks of ONE active ``reqPositions`` snapshot.

    These callbacks carry no request id, so ``request_id`` is a local label for this one snapshot,
    not a broker correlation. Use exactly one collector while exactly one positions subscription is
    active. Later updates are emitted after the END envelope, where correlation reports them. Each
    callback becomes one ``PositionLine`` envelope in arrival order.
    """

    _stage = Stage.POSITIONS
    correlation = Correlation.LOCAL_SINGLE_ACTIVE_SNAPSHOT

    def position(self, account: object, contract: object, position: object,
                 avgCost: object) -> None:
        if not self._admit():
            return
        account_text = _text(account)
        con_id = getattr(contract, "conId", _MISSING)
        fields = ("symbol", "secType", "currency")
        texts = [_text(getattr(contract, name, _MISSING)) for name in fields]
        amount = _number_text(position)
        if account_text is None or any(item is None for item in texts) or amount is None:
            self._refuse("CALLBACK_ARGUMENT_UNSUPPORTED")
            return
        if not SYNTHETIC_ACCOUNT_RE.fullmatch(account_text):
            self._refuse("CALLBACK_ACCOUNT_NOT_SYNTHETIC")
            return
        if type(con_id) is not int or con_id <= 0:
            self._refuse("CALLBACK_CONTRACT_ID_UNSUPPORTED")
            return
        cost = self._average_cost(avgCost)
        if cost is None:
            return
        symbol, sec_type, currency = texts
        line = PositionLine(account_id=account_text, con_id=con_id, symbol=symbol,
                            sec_type=sec_type, currency=currency, position=amount, avg_cost=cost)
        self._events.append(("POSITION", line, self._end_seen))

    def positionEnd(self) -> None:
        if not self._admit():
            return
        self._events.append(("END",))
        self._end_seen = True

    def _average_cost(self, value: object) -> str | None:
        if type(value) is float:
            finite, text = Decimal(value).is_finite(), repr(value)  # exact; NaN and inf kept
        elif type(value) is Decimal:
            finite, text = value.is_finite(), str(value)
        elif type(value) is int:
            finite, text = True, str(value)
        else:
            self._refuse("CALLBACK_ARGUMENT_UNSUPPORTED")
            return None
        if not finite:
            self._refuse("CALLBACK_AVERAGE_COST_NOT_FINITE")
            return None
        if _text(text) is None:
            self._refuse("CALLBACK_ARGUMENT_UNSUPPORTED")
            return None
        return text

    def snapshot(self) -> CallbackSnapshot:
        if self._refusals:
            return self._unsupported(self._refusals)
        envelopes: list[ResponseEnvelope] = []
        seen: dict[tuple[str, int], PositionLine] = {}
        duplicates = 0
        conflict = False
        for event in self._events:
            if event[0] == "END":
                envelopes.append(ResponseEnvelope(request_id=self.request_id,
                                                  sequence=len(envelopes),
                                                  kind=ResponseKind.POSITION_END))
                continue
            _, line, after_end = event
            if not after_end:
                key = (line.account_id, line.con_id)
                prior = seen.get(key)
                if prior is not None:
                    if prior == line:
                        duplicates += 1
                    else:
                        conflict = True
                    continue
                seen[key] = line
            envelopes.append(ResponseEnvelope(request_id=self.request_id,
                                              sequence=len(envelopes),
                                              kind=ResponseKind.POSITION, position=line))
        if conflict:
            return self._unsupported(["CALLBACK_POSITION_CONFLICT"])
        return self._result(self._status(), envelopes, self._duplicates(duplicates))
