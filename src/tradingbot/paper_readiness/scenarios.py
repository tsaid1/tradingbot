"""Synthetic, fake-clock scenarios for the offline readiness scaffold (issue #16).

Every identifier, price, size, balance, conId, server version, listing label and timestamp here is
invented. ``TLT``/``USD``/``STK``/``SMART`` are the explicit owner-approved target description and
documented API vocabulary; the primary listing and the conId are placeholders that a connected
contractDetails check must observe before they may ever be pinned. Nothing here is a research snapshot
or a claim about the real instrument, broker or account.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from pydantic import Field

from tradingbot.models import AssetClass, Instrument, MockPolicy, StrictModel
from tradingbot.paper_readiness.diagnostics import ReadinessOutcome, ReadinessReport
from tradingbot.paper_readiness.observations import (
    AccountSummaryObservation,
    ApiMessage,
    ContractObservation,
    MarketDataType,
    PositionLine,
    QuoteObservation,
    ReadinessExpectation,
    ResponseEnvelope,
    ResponseKind,
    SessionAttestation,
    TargetContract,
)
from tradingbot.paper_readiness.runner import REQUEST_IDS, run_readiness_check
from tradingbot.paper_readiness.transport import FakeClock, FakeReadOnlyTransport, FakeScript

__all__ = [
    "CLOCK_START",
    "EXPECTATION",
    "OTHER_SYNTH_ACCOUNT",
    "SCENARIOS",
    "SYNTH_ACCOUNT",
    "SYNTH_CON_ID",
    "TLT_TARGET",
    "Scenario",
    "build_transport",
    "control_script",
    "run_scenario",
]

# 2026-11-06 15:30 UTC is 10:30 America/New_York: inside the synthetic regular-session window below.
CLOCK_START = datetime(2026, 11, 6, 15, 30, tzinfo=UTC)
SYNTH_ACCOUNT = "SYNTH-PAPER0001"
OTHER_SYNTH_ACCOUNT = "SYNTH-PAPER0002"
SYNTH_CON_ID = 900001            # invented; never the real conId
SYNTH_PRIMARY = "SYNTH-LISTING"  # placeholder for the primary listing a connected check must observe
LIQUID_HOURS = "20261106:0930-20261106:1600;20261109:0930-20261109:1600"
TIME_ZONE = "America/New_York"

TLT_TARGET = TargetContract(
    instrument=Instrument(asset_class=AssetClass.ETF, symbol="TLT", exchange="SMART"),
    primary_exchange=SYNTH_PRIMARY,
)
EXPECTATION = ReadinessExpectation(account_id=SYNTH_ACCOUNT, target=TLT_TARGET)
RID = REQUEST_IDS


class Scenario(StrictModel):
    name: str = Field(pattern=r"^[a-z0-9_]{1,64}$")
    description: str
    clock_start: datetime = CLOCK_START
    expectation: ReadinessExpectation = EXPECTATION
    script: FakeScript
    expected_outcome: ReadinessOutcome
    expected_codes: list[str] = Field(default_factory=list)


# ----------------------------------------------------------------------------- building blocks

def session(**overrides) -> SessionAttestation:
    base = {"synthetic": True, "connected": True, "environment_claim": "PAPER",
            "managed_accounts": [SYNTH_ACCOUNT], "read_only_api_attested": True, "server_version": 176,
            "connection_time": CLOCK_START - timedelta(seconds=60), "socket_port": 7497,
            "messages": [ApiMessage(code=2104), ApiMessage(code=2106), ApiMessage(code=2158)]}
    base.update(overrides)
    return SessionAttestation(**base)


def contract_row(**overrides) -> ContractObservation:
    base = {"con_id": SYNTH_CON_ID, "symbol": "TLT", "local_symbol": "TLT", "sec_type": "STK",
            "currency": "USD", "exchange": "SMART", "primary_exchange": SYNTH_PRIMARY,
            "long_name": "SYNTHETIC TLT PLACEHOLDER",
            "trading_hours": "20261106:0400-20261106:2000;20261109:0400-20261109:2000",
            "liquid_hours": LIQUID_HOURS, "time_zone_id": TIME_ZONE, "min_tick": "0.01"}
    base.update(overrides)
    return ContractObservation(**base)


def quote_row(clock_start: datetime = CLOCK_START, **overrides) -> QuoteObservation:
    base = {"con_id": SYNTH_CON_ID, "market_data_type": MarketDataType.LIVE, "bid": "100.00",
            "ask": "100.02", "last": "100.01", "bid_size": 300, "ask_size": 500,
            "last_timestamp": clock_start - timedelta(seconds=10),
            "tick_types_received": [0, 1, 2, 3, 4, 5, 45], "halted": -1}
    base.update(overrides)
    return QuoteObservation(**base)


def account_row(**overrides) -> AccountSummaryObservation:
    base = {"account_id": SYNTH_ACCOUNT, "account_type": "SYNTHETIC-PAPER", "currency": "USD",
            "values": {"NetLiquidation": "1000000.00", "TotalCashValue": "1000000.00",
                       "BuyingPower": "4000000.00"}}
    base.update(overrides)
    return AccountSummaryObservation(**base)


def env(method: str, sequence: int, kind: ResponseKind, request_id: int | None = None, **payload) -> ResponseEnvelope:
    return ResponseEnvelope(request_id=RID[method] if request_id is None else request_id, sequence=sequence,
                            kind=kind, **payload)


def control_script(clock_start: datetime = CLOCK_START, **session_overrides) -> FakeScript:
    """The positive control: one consistent TLT row, one live fresh snapshot, one USD summary, flat book."""
    return FakeScript(
        session=session(**session_overrides),
        contract_details=[env("request_contract_details", 1, ResponseKind.CONTRACT_DETAILS, contract=contract_row()),
                          env("request_contract_details", 2, ResponseKind.CONTRACT_DETAILS_END)],
        quote_snapshot=[env("request_quote_snapshot", 1, ResponseKind.MARKET_DATA_TYPE,
                            market_data_type=MarketDataType.LIVE),
                        env("request_quote_snapshot", 2, ResponseKind.QUOTE, quote=quote_row(clock_start)),
                        env("request_quote_snapshot", 3, ResponseKind.TICK_SNAPSHOT_END)],
        account_summary=[env("request_account_summary", 1, ResponseKind.ACCOUNT_SUMMARY, account_summary=account_row()),
                         env("request_account_summary", 2, ResponseKind.ACCOUNT_SUMMARY_END)],
        positions=[env("request_positions", 1, ResponseKind.POSITION_END)],
    )


def _with(script: FakeScript, **streams) -> FakeScript:
    return script.model_copy(update=streams)


def _doubled(stream: list[ResponseEnvelope]) -> list[ResponseEnvelope]:
    """Every envelope delivered twice in a row, exactly as a retransmitting stream would do."""
    return [e for e in stream for _ in range(2)]


def _scenario(name: str, description: str, script: FakeScript, outcome: ReadinessOutcome, *codes: str,
              clock_start: datetime = CLOCK_START) -> Scenario:
    return Scenario(name=name, description=description, clock_start=clock_start, script=script,
                    expected_outcome=outcome, expected_codes=list(codes))


REFUSED, NOT_READY, COMPLETE = (ReadinessOutcome.REFUSED_BEFORE_DISPATCH, ReadinessOutcome.NOT_READY,
                                ReadinessOutcome.CONTROL_COMPLETE)
CONTROL = control_script()
AFTER_CLOSE = datetime(2026, 11, 6, 21, 30, tzinfo=UTC)  # 16:30 America/New_York

_SCENARIOS = [
    _scenario("tlt_regular_session_control",
              "Positive control: PAPER session, single synthetic account, unique USD TLT row, live fresh "
              "snapshot inside liquid hours, complete USD summary, complete flat positions.",
              CONTROL, COMPLETE, "SESSION_ATTESTATION_CONTROL", "CONTRACT_IDENTITY_CONTROL",
              "REGULAR_SESSION_CONTROL", "QUOTE_LIVE_CONTROL", "ACCOUNT_SNAPSHOT_CONTROL",
              "POSITIONS_SNAPSHOT_CONTROL"),
    _scenario("environment_unknown_port_7497",
              "The session states no environment; the documented paper default port 7497 is attested and "
              "must not be used as evidence.",
              control_script(environment_claim="UNKNOWN", socket_port=7497), REFUSED,
              "ENVIRONMENT_UNKNOWN", "SOCKET_PORT_NOT_EVIDENCE"),
    _scenario("environment_live",
              "The session states LIVE.", control_script(environment_claim="LIVE"), REFUSED,
              "ENVIRONMENT_LIVE_REFUSED"),
    _scenario("account_mismatch",
              "PAPER session whose single managed account is a different synthetic account.",
              control_script(managed_accounts=[OTHER_SYNTH_ACCOUNT]), REFUSED, "ACCOUNT_NOT_IN_MANAGED_LIST"),
    _scenario("account_evidence_absent",
              "PAPER session with an empty managedAccounts list.",
              control_script(managed_accounts=[]), REFUSED, "ACCOUNT_EVIDENCE_ABSENT"),
    _scenario("read_only_api_not_attested",
              "The Read-Only API setting is not attested.",
              control_script(read_only_api_attested=None), REFUSED, "READ_ONLY_API_NOT_ATTESTED"),
    _scenario("session_disconnected",
              "The transport reports it is not connected and connectivity lost (1100).",
              control_script(connected=False, messages=[ApiMessage(code=1100)]), REFUSED,
              "SESSION_NOT_CONNECTED", "SESSION_CONNECTIVITY_LOST"),
    _scenario("contract_identity_ambiguous",
              "Two contractDetails rows answer the TLT description (different listings and conIds); no quote is "
              "requested.",
              _with(CONTROL, contract_details=[
                  env("request_contract_details", 1, ResponseKind.CONTRACT_DETAILS, contract=contract_row()),
                  env("request_contract_details", 2, ResponseKind.CONTRACT_DETAILS,
                      contract=contract_row(con_id=900002, primary_exchange="SYNTH-OTHER-LISTING")),
                  env("request_contract_details", 3, ResponseKind.CONTRACT_DETAILS_END)]),
              NOT_READY, "CONTRACT_IDENTITY_AMBIGUOUS", "QUOTE_SKIPPED_NO_CONTRACT_IDENTITY"),
    _scenario("contract_not_found",
              "Documented message 200 (no security definition) and no rows.",
              _with(CONTROL, contract_details=[
                  env("request_contract_details", 1, ResponseKind.ERROR, message=ApiMessage(code=200)),
                  env("request_contract_details", 2, ResponseKind.CONTRACT_DETAILS_END)]),
              NOT_READY, "CONTRACT_NOT_FOUND", "QUOTE_SKIPPED_NO_CONTRACT_IDENTITY"),
    _scenario("contract_currency_mismatch",
              "The single row is not USD; identity is inconsistent with the USD basis and no quote is requested.",
              _with(CONTROL, contract_details=[
                  env("request_contract_details", 1, ResponseKind.CONTRACT_DETAILS, contract=contract_row(currency="EUR")),
                  env("request_contract_details", 2, ResponseKind.CONTRACT_DETAILS_END)]),
              NOT_READY, "CONTRACT_CURRENCY_MISMATCH", "QUOTE_SKIPPED_NO_CONTRACT_IDENTITY"),
    _scenario("outside_regular_session",
              "Identical observations with the fake clock after the liquid-hours window.",
              control_script(AFTER_CLOSE), NOT_READY, "OUTSIDE_REGULAR_SESSION", clock_start=AFTER_CLOSE),
    _scenario("quote_stale",
              "The last-trade timestamp is older than the shared policy quote window.",
              _with(CONTROL, quote_snapshot=[
                  env("request_quote_snapshot", 1, ResponseKind.MARKET_DATA_TYPE, market_data_type=MarketDataType.LIVE),
                  env("request_quote_snapshot", 2, ResponseKind.QUOTE,
                      quote=quote_row(last_timestamp=CLOCK_START - timedelta(seconds=120))),
                  env("request_quote_snapshot", 3, ResponseKind.TICK_SNAPSHOT_END)]),
              NOT_READY, "QUOTE_STALE"),
    _scenario("quote_delayed",
              "marketDataType DELAYED with delayed tick types 66/67/68/88.",
              _with(CONTROL, quote_snapshot=[
                  env("request_quote_snapshot", 1, ResponseKind.MARKET_DATA_TYPE, market_data_type=MarketDataType.DELAYED),
                  env("request_quote_snapshot", 2, ResponseKind.QUOTE,
                      quote=quote_row(market_data_type=MarketDataType.DELAYED, tick_types_received=[66, 67, 68, 88])),
                  env("request_quote_snapshot", 3, ResponseKind.TICK_SNAPSHOT_END)]),
              NOT_READY, "QUOTE_DELAYED"),
    _scenario("quote_untimed",
              "A live snapshot without a last-trade timestamp.",
              _with(CONTROL, quote_snapshot=[
                  env("request_quote_snapshot", 1, ResponseKind.MARKET_DATA_TYPE, market_data_type=MarketDataType.LIVE),
                  env("request_quote_snapshot", 2, ResponseKind.QUOTE, quote=quote_row(last_timestamp=None)),
                  env("request_quote_snapshot", 3, ResponseKind.TICK_SNAPSHOT_END)]),
              NOT_READY, "QUOTE_UNTIMED"),
    _scenario("entitlement_unavailable",
              "Documented message 354 (not subscribed) and no ticks.",
              _with(CONTROL, quote_snapshot=[
                  env("request_quote_snapshot", 1, ResponseKind.ERROR, message=ApiMessage(code=354)),
                  env("request_quote_snapshot", 2, ResponseKind.TICK_SNAPSHOT_END)]),
              NOT_READY, "ENTITLEMENT_UNAVAILABLE"),
    _scenario("account_summary_incomplete",
              "The summary row arrives but accountSummaryEnd never does.",
              _with(CONTROL, account_summary=[
                  env("request_account_summary", 1, ResponseKind.ACCOUNT_SUMMARY, account_summary=account_row())]),
              NOT_READY, "ACCOUNT_SUMMARY_END_MISSING"),
    _scenario("positions_disconnected",
              "One position line, then connectivity lost (1100) before positionEnd.",
              _with(CONTROL, positions=[
                  env("request_positions", 1, ResponseKind.POSITION,
                      position=PositionLine(account_id=SYNTH_ACCOUNT, con_id=900003, symbol="ZZQ", sec_type="STK",
                                            currency="USD", position="5", avg_cost="50.00")),
                  env("request_positions", 2, ResponseKind.ERROR, message=ApiMessage(code=1100))]),
              NOT_READY, "CONNECTIVITY_LOST_MID_REQUEST", "POSITIONS_END_MISSING"),
    _scenario("responses_duplicated",
              "Every stream repeats envelopes verbatim; duplicates are dropped and the control still completes.",
              FakeScript(session=CONTROL.session, contract_details=_doubled(CONTROL.contract_details),
                         quote_snapshot=_doubled(CONTROL.quote_snapshot),
                         account_summary=_doubled(CONTROL.account_summary), positions=_doubled(CONTROL.positions)),
              COMPLETE, "DUPLICATE_RESPONSE_DROPPED", "CONTRACT_IDENTITY_CONTROL", "QUOTE_LIVE_CONTROL"),
    _scenario("responses_out_of_order",
              "END markers arrive first; streams are reordered by sequence and the control still completes.",
              FakeScript(session=CONTROL.session, contract_details=list(reversed(CONTROL.contract_details)),
                         quote_snapshot=list(reversed(CONTROL.quote_snapshot)),
                         account_summary=list(reversed(CONTROL.account_summary)), positions=CONTROL.positions),
              COMPLETE, "OUT_OF_ORDER_RESPONSES_REORDERED", "QUOTE_LIVE_CONTROL"),
    _scenario("responses_uncorrelated",
              "A quote for a foreign request id appears in the snapshot stream and is ignored; the set is ambiguous.",
              _with(CONTROL, quote_snapshot=CONTROL.quote_snapshot + [
                  env("request_quote_snapshot", 9, ResponseKind.QUOTE, request_id=4242, quote=quote_row(bid="1.00", ask="1.01"))]),
              NOT_READY, "UNCORRELATED_RESPONSE_IGNORED"),
]
SCENARIOS: dict[str, Scenario] = {s.name: s for s in _SCENARIOS}
assert len(SCENARIOS) == len(_SCENARIOS), "scenario names must be unique"


def build_transport(scenario: Scenario) -> tuple[FakeReadOnlyTransport, FakeClock]:
    clock = FakeClock(scenario.clock_start)
    return FakeReadOnlyTransport(scenario.script, clock), clock


def run_scenario(scenario: Scenario, policy: MockPolicy | None = None) -> ReadinessReport:
    transport, clock = build_transport(scenario)
    return run_readiness_check(transport, scenario.expectation, clock=clock, policy=policy, label=scenario.name)
