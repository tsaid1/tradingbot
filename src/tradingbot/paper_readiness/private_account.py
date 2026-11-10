"""Private per-run account owner and a closed invented version 2 replay. NON_AUTHORITATIVE.

``AccountOwner`` holds one run's expected account identifier, managed-account snapshot and key
privately. It reuses ``account_ref.compare_account`` exactly. Only its own successful comparison issues
a ``Binding``, an immutable handle the owner later recognises by identity. A copied, forged,
serialized, foreign-owner or stale handle is refused. A public ``AccountMatch`` or a reference string
is never accepted as a binding.

``PrivateAccountReplay`` selects one fixed invented scenario. Its owned replay transport answers the
four reads from fixed data, refusing any handle its owner did not issue, and drives the accepted
account and position producers in owner mode. Rows are compared raw before projection. Nothing here
accepts a callable, client, transport kind or raw resolver, and nothing admits an external account or
session.

A binding or reference is a comparison label within one run. It is not authentication, PAPER evidence,
provenance or consent. Key freshness is the caller's responsibility. Object checks prevent accidental
cross-run pairing, not hostile introspection. See docs/PRIVATE_ACCOUNT_DATAFLOW.md.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from tradingbot.models import AssetClass, Instrument
from tradingbot.paper_readiness.account_position_callbacks import (
    AccountSummaryCallbacks,
    PositionSnapshotCallbacks,
)
from tradingbot.paper_readiness.account_ref import (
    MAX_MANAGED_ACCOUNTS,
    MatchStatus,
    RefusalCode,
    compare_account,
)
from tradingbot.paper_readiness.observations import (
    ApiMessage,
    ContractObservation,
    MarketDataType,
    QuoteObservation,
    ReadinessExpectationV2,
    ResponseEnvelope,
    ResponseKind,
    SessionAttestationV2,
    TargetContract,
)
from tradingbot.paper_readiness.transport import FakeClock

__all__ = [
    "REPLAY_CLOCK",
    "REPLAY_SCENARIOS",
    "REPLAY_TARGET",
    "AccountOwner",
    "Binding",
    "PrivateAccountReplay",
    "ReplayScenario",
]

_ISSUER = object()  # only this module issues bindings
_NO_COPY = "this object holds private run state and cannot be copied or serialized"


class Binding:
    """An owner-issued handle carrying only the opaque reference. Immutable once issued."""

    __slots__ = ("_reference",)

    def __init__(self, issuer: object, reference: str) -> None:
        if issuer is not _ISSUER:
            raise TypeError("a Binding is issued only by an AccountOwner")
        object.__setattr__(self, "_reference", reference)

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("a Binding is immutable")

    @property
    def reference(self) -> str:
        return self._reference

    def __repr__(self) -> str:
        return f"Binding({self._reference})"


class AccountOwner:
    """One run's private identity evidence. It validates and snapshots before it retains anything."""

    __slots__ = ("_binding", "_closed", "_expected", "_key", "_match")

    def __init__(self, expected: object, managed: object, key: object) -> None:
        # Snapshot a bounded list or tuple first, so later mutation of the caller's list changes nothing.
        snapshot = (tuple(managed) if type(managed) in (list, tuple) and len(managed) <= MAX_MANAGED_ACCOUNTS
                    else managed)
        match = compare_account(expected, snapshot, key)
        self._match = match
        retained = match.status is not MatchStatus.REFUSED
        self._expected = expected if retained else None
        self._key = key if retained else None
        self._binding = Binding(_ISSUER, match.reference) if match.status is MatchStatus.EXACT else None
        self._closed = False

    @property
    def status(self) -> MatchStatus:
        return self._match.status

    @property
    def refusal(self) -> RefusalCode | None:
        return self._match.refusal

    @property
    def binding(self) -> Binding | None:
        return self._binding

    def owns(self, handle: object) -> bool:
        """True only for this open owner's own issued binding, by exact type and object identity."""
        return (type(handle) is Binding and self._binding is not None and handle is self._binding
                and not self._closed)

    def close(self) -> None:
        """End the run. Its binding is stale from now on, and rows can no longer be projected."""
        self._closed = True

    def _project_row(self, raw: object) -> str | None:
        """Compare a raw row identity with the private expected one, then project it.

        A match projects to the binding's reference. A valid foreign identity projects to its own keyed
        reference, never the expected one. Malformed input, or a closed or unbound owner, gives None."""
        if self._closed or self._binding is None:
            return None
        match = compare_account(raw, [self._expected], self._key)
        if match.status is MatchStatus.EXACT:
            return match.reference
        if match.status is MatchStatus.MISMATCH:
            return compare_account(raw, [raw], self._key).reference
        return None

    def __repr__(self) -> str:
        return f"AccountOwner(status={self._match.status.value}, closed={self._closed})"

    def __reduce_ex__(self, protocol: object) -> object:
        raise TypeError(_NO_COPY)

    def __copy__(self) -> object:
        raise TypeError(_NO_COPY)

    def __deepcopy__(self, memo: object) -> object:
        raise TypeError(_NO_COPY)


# ----------------------------------------------------------------------------- the closed invented replay

# 2026-02-11 16:00 UTC is 11:00 America/New_York (EST), inside the invented session below.
REPLAY_CLOCK = datetime(2026, 2, 11, 16, 0, tzinfo=UTC)
_EXPECTED = "INVENTEDACCT01"  # invented identities, deliberately not SYNTH-shaped
_FOREIGN = "INVENTEDACCT02"
_CON_ID = 810001
REPLAY_TARGET = TargetContract(
    instrument=Instrument(asset_class=AssetClass.ETF, symbol="PRIVETF", exchange="SMART"),
    primary_exchange="INVENTED-LISTING",
)
_SUMMARY = (("AccountType", "INVENTED-TYPE", ""), ("NetLiquidation", "180000.00", "USD"),
            ("TotalCashValue", "150000.00", "USD"), ("BuyingPower", "360000.00", "USD"))
_ERROR = "error"


@dataclass(frozen=True)
class ReplayScenario:
    """One fixed invented scenario: managed identities, summary and position callbacks, END counts."""
    name: str
    managed: tuple[str, ...]
    summary: tuple[tuple, ...]
    summary_ends: int
    positions: tuple[tuple, ...]
    position_ends: int


def _summary_for(account: str, rows=_SUMMARY) -> tuple[tuple, ...]:
    return tuple((account, *row) for row in rows)


_ROW = (_EXPECTED, 820001, Decimal("-15.25"), 98.5)
REPLAY_SCENARIOS = {s.name: s for s in (
    ReplayScenario("matching_singleton", (_EXPECTED,), _summary_for(_EXPECTED), 1, (_ROW,), 1),
    ReplayScenario("managed_absent", (), _summary_for(_EXPECTED), 1, (_ROW,), 1),
    ReplayScenario("managed_foreign", (_FOREIGN,), _summary_for(_EXPECTED), 1, (_ROW,), 1),
    ReplayScenario("managed_duplicate", (_EXPECTED, _EXPECTED), _summary_for(_EXPECTED), 1, (_ROW,), 1),
    ReplayScenario("foreign_summary_rows", (_EXPECTED,), _summary_for(_FOREIGN), 1, (_ROW,), 1),
    ReplayScenario("foreign_position_row", (_EXPECTED,), _summary_for(_EXPECTED), 1,
                   (_ROW, (_FOREIGN, 820002, Decimal(1), 1.0)), 1),
    ReplayScenario("malformed_row_identity", (_EXPECTED,), (*_summary_for(_EXPECTED), ("BAD\nID",
                   "SettledCash", "1.00", "USD")), 1, (_ROW,), 1),
    ReplayScenario("duplicate_rows", (_EXPECTED,), (*_summary_for(_EXPECTED), (_EXPECTED, "NetLiquidation",
                   "180000.00", "USD")), 1, (_ROW,), 1),
    ReplayScenario("missing_end", (_EXPECTED,), _summary_for(_EXPECTED), 1, (_ROW,), 0),
    ReplayScenario("error_in_stream", (_EXPECTED,), (*_summary_for(_EXPECTED), (_ERROR,)), 2, (_ROW,), 1),
    ReplayScenario("missing_currency", (_EXPECTED,), _summary_for(_EXPECTED, (
        _SUMMARY[0], _SUMMARY[1], _SUMMARY[2], ("BuyingPower", "360000.00", ""))), 1, (_ROW,), 1),
    ReplayScenario("oversized_quantity", (_EXPECTED,), _summary_for(_EXPECTED), 1,
                   ((_EXPECTED, 820001, 10**5000, 1.0),), 2),
)}


class _ReplayTransport:
    """The owned replay transport: the same four read names, fixed invented answers, owner-checked
    handles. A handle the owner did not issue returns nothing, so the stream stays incomplete."""

    def __init__(self, replay: PrivateAccountReplay) -> None:
        self._replay = replay

    def request_contract_details(self, request_id: int, target: TargetContract) -> list[ResponseEnvelope]:
        row = ContractObservation(con_id=_CON_ID, symbol="PRIVETF", local_symbol="PRIVETF", sec_type="STK",
                                  currency="USD", exchange="SMART", primary_exchange="INVENTED-LISTING",
                                  long_name="INVENTED PRIVATE ETF",
                                  trading_hours="20260211:0400-20260211:2000",
                                  liquid_hours="20260211:0930-20260211:1600", time_zone_id="America/New_York",
                                  min_tick="0.01")
        return [ResponseEnvelope(request_id=request_id, sequence=1, kind=ResponseKind.CONTRACT_DETAILS,
                                 contract=row),
                ResponseEnvelope(request_id=request_id, sequence=2, kind=ResponseKind.CONTRACT_DETAILS_END)]

    def request_quote_snapshot(self, request_id: int, con_id: int) -> list[ResponseEnvelope]:
        quote = QuoteObservation(con_id=con_id, market_data_type=MarketDataType.LIVE, bid="40.00", ask="40.02",
                                 last="40.01", bid_size=200, ask_size=400,
                                 last_timestamp=REPLAY_CLOCK - timedelta(seconds=10), received_at=REPLAY_CLOCK,
                                 tick_types_received=[0, 1, 2, 3, 4, 45], halted=-1)
        return [ResponseEnvelope(request_id=request_id, sequence=1, kind=ResponseKind.MARKET_DATA_TYPE,
                                 market_data_type=MarketDataType.LIVE),
                ResponseEnvelope(request_id=request_id, sequence=2, kind=ResponseKind.QUOTE, quote=quote),
                ResponseEnvelope(request_id=request_id, sequence=3, kind=ResponseKind.TICK_SNAPSHOT_END)]

    def request_account_summary(self, request_id: int, binding: object) -> list[ResponseEnvelope]:
        owner = self._replay._owner
        if not owner.owns(binding):
            self._replay._handle_refusals += 1
            return []
        stream = AccountSummaryCallbacks(request_id, owner=owner)
        for row in self._replay.scenario.summary:
            if row == (_ERROR,):
                stream.unsupported_callback(_ERROR)
            else:
                stream.accountSummary(request_id, *row)
        for _ in range(self._replay.scenario.summary_ends):
            stream.accountSummaryEnd(request_id)
        return stream.envelopes()

    def request_positions(self, request_id: int, binding: object) -> list[ResponseEnvelope]:
        owner = self._replay._owner
        if not owner.owns(binding):
            self._replay._handle_refusals += 1
            return []
        stream = PositionSnapshotCallbacks(request_id, owner=owner)
        for account, con_id, quantity, cost in self._replay.scenario.positions:
            contract = _Contract(con_id)
            stream.position(account, contract, quantity, cost)
        for _ in range(self._replay.scenario.position_ends):
            stream.positionEnd()
        return stream.envelopes()


class _Contract:
    """The documented Contract fields a position carries, invented."""

    def __init__(self, con_id: int) -> None:
        self.conId, self.symbol, self.secType, self.currency = con_id, "INVPOS", "STK", "USD"


class PrivateAccountReplay:
    """One single-use run of a fixed invented scenario. The caller supplies only the scenario name and
    the per-run key; everything else is fixed here."""

    __slots__ = ("_clock", "_handle_refusals", "_owner", "_scenario", "_transport", "_used")

    def __init__(self, scenario: str, key: bytes) -> None:
        if type(scenario) is not str or scenario not in REPLAY_SCENARIOS:
            raise ValueError("unknown replay scenario")
        self._scenario = REPLAY_SCENARIOS[scenario]
        self._owner = AccountOwner(_EXPECTED, self._scenario.managed, key)
        self._clock = FakeClock(REPLAY_CLOCK)
        self._transport = _ReplayTransport(self)
        self._used = False
        self._handle_refusals = 0

    # Read-only: the scenario, clock and transport are fixed at construction and cannot be reassigned.
    @property
    def scenario(self) -> ReplayScenario:
        return self._scenario

    @property
    def clock(self) -> FakeClock:
        return self._clock

    @property
    def transport(self) -> _ReplayTransport:
        return self._transport

    def owns_transport(self, transport: object) -> bool:
        """True only for this replay's own internally created transport, checked without calling it."""
        return (type(transport) is _ReplayTransport and transport is self._transport
                and transport._replay is self)

    @property
    def account_status(self) -> MatchStatus:
        return self._owner.status

    @property
    def binding(self) -> Binding | None:
        return self._owner.binding

    @property
    def handle_refusals(self) -> int:
        return self._handle_refusals

    def binding_owned(self, handle: object) -> bool:
        return self._owner.owns(handle)

    def expectation(self) -> ReadinessExpectationV2 | None:
        binding = self._owner.binding
        return None if binding is None else ReadinessExpectationV2(account_ref=binding.reference,
                                                                   target=REPLAY_TARGET)

    def attest_session(self) -> SessionAttestationV2:
        return SessionAttestationV2(synthetic=True, connected=True, environment_claim="PAPER",
                                    read_only_api_attested=True, server_version=178,
                                    connection_time=REPLAY_CLOCK - timedelta(seconds=90),
                                    messages=[ApiMessage(code=2104), ApiMessage(code=2158)])

    def begin(self) -> bool:
        """Single use: True the first time only."""
        if self._used:
            return False
        self._used = True
        return True

    def close(self) -> None:
        self._owner.close()

    def __repr__(self) -> str:
        return f"PrivateAccountReplay(scenario={self._scenario.name}, status={self._owner.status.value})"

    def __reduce_ex__(self, protocol: object) -> object:
        raise TypeError(_NO_COPY)

    def __copy__(self) -> object:
        raise TypeError(_NO_COPY)

    def __deepcopy__(self, memo: object) -> object:
        raise TypeError(_NO_COPY)
