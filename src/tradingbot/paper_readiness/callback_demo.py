"""Offline, callback-driven readiness demonstration. A SIMULATION, NON-AUTHORITATIVE.

Run it with ``python -m tradingbot.paper_readiness.callback_demo``. It takes no arguments: the package
admits only pure standard-library imports, so there is no ``sys`` or ``argparse`` here.

Each scenario feeds invented primitive values for documented IBKR callbacks into the merged
``IbkrCallbackNormalizer`` for the contract-details and quote-snapshot requests. The resulting
``ResponseEnvelope`` records go through the unchanged ``FakeReadOnlyTransport`` and readiness runner,
which produce the usual NON_AUTHORITATIVE report with ``trading_permission: false``.

The account summary and positions are **preassembled control envelopes**, invented here. They are not
produced from callbacks. No account or position callback adapter, installed SDK, broker connection or
network is involved, and nothing here authorizes paper readiness or trading.

Every fixture value is invented for this demonstration only, under one fixed ``FakeClock``.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from tradingbot.models import AssetClass, Instrument
from tradingbot.paper_readiness.diagnostics import ReadinessOutcome, ReadinessReport
from tradingbot.paper_readiness.ibkr_callbacks import CallbackRequest, IbkrCallbackNormalizer
from tradingbot.paper_readiness.observations import (
    AccountSummaryObservation,
    ApiMessage,
    ReadinessExpectation,
    ResponseEnvelope,
    ResponseKind,
    SessionAttestation,
    TargetContract,
)
from tradingbot.paper_readiness.runner import REQUEST_IDS, render_text, run_readiness_check
from tradingbot.paper_readiness.transport import FakeClock, FakeReadOnlyTransport, FakeScript

__all__ = [
    "DEMO_CLOCK",
    "DEMO_EXPECTATION",
    "PREASSEMBLED_STREAMS",
    "SCENARIOS",
    "SIMULATION_NOTICE",
    "DemoResult",
    "DemoScenario",
    "main",
    "render_demo_json",
    "render_demo_text",
    "run_demo",
]

SIMULATION_NOTICE = (
    "SIMULATION: contract and quote envelopes come from invented callback values through "
    "IbkrCallbackNormalizer; account summary and positions are PREASSEMBLED CONTROL envelopes, not "
    "callbacks. No SDK, broker, network or account is involved. Not paper-readiness authorization."
)
# The two streams this demonstration supplies as preassembled envelopes instead of callbacks.
PREASSEMBLED_STREAMS = ("account_summary", "positions")

# ----------------------------------------------------------------------------- invented fixtures

# 2026-01-14 16:00 UTC is 11:00 America/New_York (EST), inside the invented regular session below.
DEMO_CLOCK = datetime(2026, 1, 14, 16, 0, tzinfo=UTC)
_ACCOUNT = "SYNTH-DEMO0001"
_CON_ID = 700001
_SYMBOL = "DEMOETF"
_PRIMARY = "DEMO-LISTING"
_ZONE = "America/New_York"
_LIQUID_HOURS = "20260114:0930-20260114:1600;20260115:0930-20260115:1600"
_TRADING_HOURS = "20260114:0400-20260114:2000;20260115:0400-20260115:2000"
_LAST_TRADE = str(int((DEMO_CLOCK - timedelta(seconds=8)).timestamp()))
_CRID = REQUEST_IDS["request_contract_details"]
_QRID = REQUEST_IDS["request_quote_snapshot"]

DEMO_EXPECTATION = ReadinessExpectation(
    account_id=_ACCOUNT,
    target=TargetContract(instrument=Instrument(asset_class=AssetClass.ETF, symbol=_SYMBOL,
                                                exchange="SMART"),
                          primary_exchange=_PRIMARY),
)


class _Values:
    """A plain object shaped like a documented SDK value, holding only the given attributes."""

    def __init__(self, **values: object) -> None:
        self.__dict__.update(values)


class _NoAttrib:
    """Stands in for the documented TickAttrib argument, which the normalizer does not interpret."""


def _details() -> _Values:
    contract = _Values(conId=_CON_ID, symbol=_SYMBOL, localSymbol=_SYMBOL, secType="STK",
                       currency="USD", exchange="SMART", primaryExchange=_PRIMARY)
    return _Values(contract=contract, longName="INVENTED DEMO ETF", tradingHours=_TRADING_HOURS,
                   liquidHours=_LIQUID_HOURS, timeZoneId=_ZONE, minTick=0.01)


def _session() -> SessionAttestation:
    """The fake transport's own invented attestation. It is not evidence about any real session."""
    return SessionAttestation(
        synthetic=True, connected=True, environment_claim="PAPER", managed_accounts=[_ACCOUNT],
        read_only_api_attested=True, server_version=178,
        connection_time=DEMO_CLOCK - timedelta(seconds=120),
        messages=[ApiMessage(code=2104), ApiMessage(code=2158)],
    )


def _preassembled_account_streams() -> dict[str, list[ResponseEnvelope]]:
    """PREASSEMBLED CONTROL envelopes for the two reads this demonstration does not drive by callback."""
    summary = AccountSummaryObservation(
        account_id=_ACCOUNT, account_type="INVENTED-DEMO", currency="USD",
        values={"NetLiquidation": "250000.00", "TotalCashValue": "250000.00",
                "BuyingPower": "500000.00"},
    )
    account_rid = REQUEST_IDS["request_account_summary"]
    positions_rid = REQUEST_IDS["request_positions"]
    return {
        "account_summary": [
            ResponseEnvelope(request_id=account_rid, sequence=1, kind=ResponseKind.ACCOUNT_SUMMARY,
                             account_summary=summary),
            ResponseEnvelope(request_id=account_rid, sequence=2, kind=ResponseKind.ACCOUNT_SUMMARY_END),
        ],
        "positions": [ResponseEnvelope(request_id=positions_rid, sequence=1,
                                       kind=ResponseKind.POSITION_END)],
    }


# ----------------------------------------------------------------------------- callback sequences

def _contract_callbacks(n: IbkrCallbackNormalizer) -> None:
    n.contractDetails(_CRID, _details())
    n.contractDetailsEnd(_CRID)


def _live_ticks(n: IbkrCallbackNormalizer) -> None:
    attrib = _NoAttrib()
    n.marketDataType(_QRID, 1)
    n.tickPrice(_QRID, 1, 48.25, attrib)
    n.tickSize(_QRID, 0, Decimal(700))
    n.tickPrice(_QRID, 2, 48.27, attrib)
    n.tickSize(_QRID, 3, Decimal(900))
    n.tickPrice(_QRID, 4, 48.26, attrib)
    n.tickString(_QRID, 45, _LAST_TRADE)


def _control(contract: IbkrCallbackNormalizer, quote: IbkrCallbackNormalizer) -> None:
    _contract_callbacks(contract)
    _live_ticks(quote)
    quote.tickSnapshotEnd(_QRID)


def _delayed_size_mixed_with_live(contract: IbkrCallbackNormalizer,
                                  quote: IbkrCallbackNormalizer) -> None:
    """PR #25 review P2: delayed bid size (tick 69) among otherwise live ticks."""
    _contract_callbacks(contract)
    _live_ticks(quote)
    quote.tickSize(_QRID, 69, Decimal(700))
    quote.tickSnapshotEnd(_QRID)


def _issue_with_repeated_end(contract: IbkrCallbackNormalizer, quote: IbkrCallbackNormalizer) -> None:
    """PR #25 review P1: a valid row, an unsupported callback, then contractDetailsEnd twice."""
    contract.contractDetails(_CRID, _details())
    contract.unsupported_callback("connectionClosed")
    contract.contractDetailsEnd(_CRID)
    contract.contractDetailsEnd(_CRID)
    _live_ticks(quote)
    quote.tickSnapshotEnd(_QRID)


@dataclass(frozen=True)
class DemoScenario:
    name: str
    description: str
    expected_outcome: ReadinessOutcome
    feed: object


SCENARIOS = {
    s.name: s for s in (
        DemoScenario("callback_control", "Documented contract and live snapshot callbacks.",
                     ReadinessOutcome.CONTROL_COMPLETE, _control),
        DemoScenario("delayed_size_mixed_with_live",
                     "Delayed bid size tick 69 mixed into live ticks (PR #25 review P2).",
                     ReadinessOutcome.NOT_READY, _delayed_size_mixed_with_live),
        DemoScenario("issue_with_repeated_end",
                     "Unsupported callback, then contractDetailsEnd twice (PR #25 review P1).",
                     ReadinessOutcome.NOT_READY, _issue_with_repeated_end),
    )
}


# ----------------------------------------------------------------------------- running and rendering

@dataclass(frozen=True)
class DemoResult:
    scenario: DemoScenario
    report: ReadinessReport
    contract_completed: bool
    contract_issues: tuple[str, ...]
    quote_completed: bool
    quote_issues: tuple[str, ...]

    @property
    def matches_expectation(self) -> bool:
        return self.report.outcome == self.scenario.expected_outcome


def run_demo(name: str) -> DemoResult:
    """Run one named scenario from fresh normalizers under the fixed clock."""
    scenario = SCENARIOS[name]
    clock = FakeClock(DEMO_CLOCK)
    contract = IbkrCallbackNormalizer(_CRID, CallbackRequest.CONTRACT_DETAILS)
    quote = IbkrCallbackNormalizer(_QRID, CallbackRequest.QUOTE_SNAPSHOT, con_id=_CON_ID, clock=clock)
    scenario.feed(contract, quote)
    script = FakeScript(session=_session(), contract_details=contract.envelopes(),
                        quote_snapshot=quote.envelopes(), **_preassembled_account_streams())
    report = run_readiness_check(FakeReadOnlyTransport(script, clock), DEMO_EXPECTATION, clock=clock,
                                 label=f"callback-demo:{name}")
    return DemoResult(
        scenario=scenario, report=report,
        contract_completed=contract.completed, contract_issues=tuple(i.code.value for i in contract.issues),
        quote_completed=quote.completed, quote_issues=tuple(i.code.value for i in quote.issues),
    )


def _summary(result: DemoResult) -> dict:
    return {
        "scenario": result.scenario.name,
        "description": result.scenario.description,
        "expected_outcome": result.scenario.expected_outcome.value,
        "outcome": result.report.outcome.value,
        "matches_expectation": result.matches_expectation,
        "callback_ingestion": {
            "contract_details": {"completed": result.contract_completed,
                                 "issues": list(result.contract_issues)},
            "quote_snapshot": {"completed": result.quote_completed, "issues": list(result.quote_issues)},
        },
        "preassembled_control_streams": list(PREASSEMBLED_STREAMS),
    }


def render_demo_json(results: list[DemoResult]) -> str:
    payload = {"notice": SIMULATION_NOTICE,
               "scenarios": [{**_summary(r), "report": r.report.model_dump(mode="json")} for r in results]}
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def render_demo_text(results: list[DemoResult]) -> str:
    lines = [SIMULATION_NOTICE, ""]
    for r in results:
        s = _summary(r)
        ingestion = s["callback_ingestion"]
        lines.append(f"== {r.scenario.name}: {r.scenario.description}")
        lines.append(f"   expected={s['expected_outcome']} outcome={s['outcome']} "
                     f"matches_expectation={s['matches_expectation']}")
        for stream, state in ingestion.items():
            issues = ", ".join(state["issues"]) or "none"
            lines.append(f"   {stream}: completed={state['completed']} issues={issues}")
        lines.append(f"   preassembled control streams: {', '.join(PREASSEMBLED_STREAMS)}")
        lines.append(render_text(r.report, summary=True).rstrip("\n"))
        lines.append("")
    matched = sum(r.matches_expectation for r in results)
    lines.append(f"{matched}/{len(results)} scenarios matched their expected outcome. "
                 "NON_AUTHORITATIVE; trading_permission=False.")
    return "\n".join(lines) + "\n"


def main() -> int:
    """Print every scenario. Exit 0 only if each outcome equals its expected outcome; never a verdict
    on any real broker, account or readiness."""
    results = [run_demo(name) for name in SCENARIOS]
    print(render_demo_text(results), end="")
    return 0 if all(r.matches_expectation for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
