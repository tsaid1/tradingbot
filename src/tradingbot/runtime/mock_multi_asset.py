"""Mock multi-asset demonstrations through the real service path (invented data only).

One scenario per price-basis treatment, each on its own state directory, SimClock and strict
SimBroker, driven through ``RuntimeService.run_once`` exactly as the service runs: invented posts
-> TklInterpreter -> mapping policy (version 2) -> MappedQuote -> gateway -> coordinator -> broker
-> reconciliation. Every ticker, contract, multiplier, tick, level and date below is invented.

  index      $QIDX, quoted in index points, executed in the invented future QIF (x5) with
             REFERENCE_TRIGGER exits on the invented index QIDX and a BASIS_SHIFT backstop stop
  energy     $QOIL, the named month of the invented future QOF (x10), DIRECT; then a post naming
             a month inside its roll window (CONTRACT_IN_ROLL_WINDOW)
  metal      $QMTL, quoted as the invented spot QMS, executed in the invented future QMF (x10)
             with BASIS_SHIFT; the basis is recorded; then a post while the basis exceeds its cap
  option     $QSTK, a fully specified invented single-leg call (x100), DIRECT; then the same
             instruction without its strike (OPTION_TERMS_INCOMPLETE)
  unapproved the same posts with every family approved = false: everything HOLDs, nothing reaches
             the broker

Each lifecycle scenario shows an entry, a stop amendment, a partial reduction and a close, then
checks clean reconciliation and a flat account. ``run_all`` returns the results;
scripts/run_mock_multi_asset_demo.py prints them and exits non-zero on any unexplained discrepancy.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from tradingbot.execution.broker import ContractRef
from tradingbot.execution.sim_broker import SimBroker, SimClock
from tradingbot.models import AssetClass
from tradingbot.runtime.config import parse_config
from tradingbot.runtime.coverage import coverage_report
from tradingbot.runtime.mapping import MappingPolicy
from tradingbot.runtime.service import RuntimeService
from tradingbot.runtime.sources import ReplayFileSource
from tradingbot.runtime.store import RuntimeStore
from tradingbot.source_events import Provenance, SourcePost

START = datetime(2026, 6, 1, 14, 0, tzinfo=UTC)  # a Monday, inside every invented session
RECORDED = datetime(2026, 3, 2, 15, 0, tzinfo=UTC)
AUTHOR = "4242"

# ------------------------------------------------------------------ invented contracts

QIDX = ContractRef(symbol="QIDX", asset_class=AssetClass.INDEX, sec_type="IND",
                   exchange="SIMIDX", con_id=9101)
QIF_JUN = ContractRef(symbol="QIF", asset_class=AssetClass.FUTURE, sec_type="FUT",
                      exchange="SIMFUT", con_id=9102, expiry="20260619", local_symbol="QIFM6",
                      multiplier=Decimal(5), min_tick=Decimal("0.25"))
QIF_SEP = QIF_JUN.model_copy(update={"con_id": 9103, "expiry": "20260918",
                                     "local_symbol": "QIFU6"})
QOF_JUN = ContractRef(symbol="QOF", asset_class=AssetClass.FUTURE, sec_type="FUT",
                      exchange="SIMNRG", con_id=9201, expiry="20260605", local_symbol="QOFM6",
                      multiplier=Decimal(10), min_tick=Decimal("0.01"))
QOF_JUL = QOF_JUN.model_copy(update={"con_id": 9202, "expiry": "20260708",
                                     "local_symbol": "QOFN6"})
QMS = ContractRef(symbol="QMS", asset_class=AssetClass.COMMODITY, sec_type="CMDTY",
                  exchange="SIMSPOT", con_id=9301)
QMF_AUG = ContractRef(symbol="QMF", asset_class=AssetClass.FUTURE, sec_type="FUT",
                      exchange="SIMMTL", con_id=9302, expiry="20260827", local_symbol="QMFQ6",
                      multiplier=Decimal(10), min_tick=Decimal("0.10"))
QSTK_C105 = ContractRef(symbol="QSTK", asset_class=AssetClass.OPTION, sec_type="OPT",
                        exchange="SIMOPT", con_id=9401, expiry="20260918",
                        strike=Decimal(105), right="C", multiplier=Decimal(100),
                        min_tick=Decimal("0.01"))
CONTRACTS = (QIDX, QIF_JUN, QIF_SEP, QOF_JUN, QOF_JUL, QMS, QMF_AUG, QSTK_C105)

APPROVAL = 'approved = true\napproved_by = "demo-owner"\napproved_at = 2026-05-29T12:00:00Z'
SESSION = (  # doubled braces: MAPPING is formatted once more with the approval lines
    'session = {{ policy = "REGULAR_ONLY", timezone = "UTC", '
    'days = ["MON", "TUE", "WED", "THU", "FRI"], regular = ["13:00-21:00"] }}'
)
MAPPING = f"""
version = 2

[[mapping]]
family = "QIDX"
basis = "REFERENCE_TRIGGER"
{{approval}}
note = "Invented index family: index points; exits held as conditions on the index."
reference = {{{{ sec_type = "IND", symbol = "QIDX", exchange = "SIMIDX", currency = "USD" }}}}
execution = {{{{ sec_type = "FUT", symbol = "QIF", exchange = "SIMFUT", currency = "USD", multiplier = "5", min_tick = "0.25" }}}}
contract_selection = {{{{ rule = "EXPLICIT_OR_NEAREST", min_business_days_to_roll = 6 }}}}
contracts = [ {{{{ month = "202606", roll_date = 2026-06-12 }}}}, {{{{ month = "202609", roll_date = 2026-09-11 }}}} ]
sizing = {{{{ units_per_signal = 2, max_units = 4, max_notional = "60000" }}}}
{SESSION}
{{risk_FUT}}
roll = {{{{ business_days_before = 3, action = "FLATTEN" }}}}
reference_trigger = {{{{ released_order_type = "MKT", reference_hours = ["13:30-20:00"], require_backstop = true }}}}
basis_shift = {{{{ max_abs_basis = "40", max_age_seconds = 5, scale_ratio = "1" }}}}

[[mapping]]
family = "QOIL"
basis = "DIRECT"
{{approval}}
note = "Invented energy future quoted in its own named contract month."
reference = {{{{ sec_type = "FUT", symbol = "QOF", exchange = "SIMNRG", currency = "USD" }}}}
execution = {{{{ sec_type = "FUT", symbol = "QOF", exchange = "SIMNRG", currency = "USD", multiplier = "10", min_tick = "0.01" }}}}
contract_selection = {{{{ rule = "EXPLICIT_ONLY", min_business_days_to_roll = 6 }}}}
contracts = [ {{{{ month = "202606", roll_date = 2026-06-03 }}}}, {{{{ month = "202607", roll_date = 2026-07-06 }}}} ]
sizing = {{{{ units_per_signal = 2, max_units = 4, max_notional = "5000" }}}}
{SESSION}
{{risk_FUT}}
roll = {{{{ business_days_before = 3, action = "FLATTEN" }}}}

[[mapping]]
family = "QMTL"
basis = "BASIS_SHIFT"
{{approval}}
note = "Invented metal quoted as spot, executed in a future with the observed basis."
reference = {{{{ sec_type = "CMDTY", symbol = "QMS", exchange = "SIMSPOT", currency = "USD" }}}}
execution = {{{{ sec_type = "FUT", symbol = "QMF", exchange = "SIMMTL", currency = "USD", multiplier = "10", min_tick = "0.10" }}}}
contract_selection = {{{{ rule = "EXPLICIT_OR_NEAREST", min_business_days_to_roll = 6 }}}}
contracts = [ {{{{ month = "202608", roll_date = 2026-07-28 }}}} ]
sizing = {{{{ units_per_signal = 2, max_units = 4, max_notional = "50000" }}}}
{SESSION}
{{risk_FUT}}
roll = {{{{ business_days_before = 3, action = "FLATTEN" }}}}
basis_shift = {{{{ max_abs_basis = "20", max_age_seconds = 5, scale_ratio = "1" }}}}

[[mapping]]
family = "QSTK.OPT"
basis = "DIRECT"
{{approval}}
note = "Invented single-leg options on QSTK, quoted in their own premium."
reference = {{{{ sec_type = "OPT", symbol = "QSTK", exchange = "SIMOPT", currency = "USD" }}}}
execution = {{{{ sec_type = "OPT", symbol = "QSTK", exchange = "SIMOPT", currency = "USD", multiplier = "100", min_tick = "0.01" }}}}
sizing = {{{{ units_per_signal = 2, max_units = 4, max_notional = "2000" }}}}
{SESSION}
{{risk_OPT}}
"""


# Risk gates (docs/RISK_GATES.md). EVERY VALUE BELOW IS INVENTED DEMONSTRATION DATA, deliberately
# wide so that each scenario decides exactly as it did before the gates existed; none is a
# proposal, an owner's value or a contract specification. Without the [risk] table (``risk=False``)
# the cash baseline refuses every one of these futures and options entries (G2).
INVENTED_RISK_TABLE = {
    "approved_by": "invented-demo-owner", "approved_at": datetime(2026, 5, 29, 12, tzinfo=UTC),
    "currency": "USD", "environments": ["SIM"],
    "allocated_equity": "100000000", "max_gross_notional_ratio": "1000",
    "max_portfolio_risk": "100000000", "max_margin_utilisation": "1",
    "min_excess_liquidity": "1", "account_values_max_age_seconds": 86400,
    "borrowing": {"permitted": False, "short_sale_permitted": False},
    "eligibility": {"futures": True, "options": True, "cfd": False},
}
INVENTED_FAMILY_RISK = {
    "FUT": ('funding = "FUTURES_MARGIN"\nrisk = {{ max_risk_per_idea = "100000000", max_stop_distance = "100%" }}\n'
            'margin = {{ initial = "1", maintenance = "1", currency = "USD", '
            'verified_at = 2026-05-29, max_age_days = 3650 }}\n'
            'entry_collar = "100%"'),
    "OPT": ('funding = "OPTION_PREMIUM"\nrisk = {{ max_risk_per_idea = "100000000", max_stop_distance = "100%" }}\n'
            'entry_collar = "100%"'),
}


def mapping_toml(*, approved: bool) -> str:
    return MAPPING.format(approval=APPROVAL if approved else "approved = false",
                          risk_FUT=INVENTED_FAMILY_RISK["FUT"].format(),
                          risk_OPT=INVENTED_FAMILY_RISK["OPT"].format())


# ------------------------------------------------------------------ quotes

Quotes = dict[str, tuple[str, str, str | None]]  # contract_key -> (bid, ask, last)
STATIC: dict[ContractRef, tuple[str, str, str | None]] = {
    QIDX: ("5009.5", "5010.5", "5010"),
    QIF_JUN: ("5024.75", "5025.25", None),
    QIF_SEP: ("5040.00", "5040.50", None),
    QOF_JUN: ("70.10", "70.12", None),
    QOF_JUL: ("71.50", "71.52", None),
    QMS: ("1849.9", "1850.1", None),
    QMF_AUG: ("1861.9", "1862.1", None),  # basis +12 against the spot midpoint
    QSTK_C105: ("4.20", "4.22", None),
}


def static_quotes(_elapsed: float) -> dict[ContractRef, tuple[str, str, str | None]]:
    return STATIC


def metal_quotes(elapsed: float) -> dict[ContractRef, tuple[str, str, str | None]]:
    """After the metal lifecycle, the future moves away from spot: basis +30, over its cap."""
    if elapsed >= 230:
        return {**STATIC, QMF_AUG: ("1879.9", "1880.1", None)}
    return STATIC


# ------------------------------------------------------------------ scenarios


@dataclass
class Step:
    offset: float
    text: str
    expect: str  # EXECUTED or HOLD
    reason: str | None = None  # a reason the outcome must carry
    action: str | None = None  # the interpreted action, when stated


@dataclass
class Scenario:
    name: str
    family: str
    treatment: str
    steps: list[Step]
    quotes: Callable[[float], dict] = static_quotes
    contracts: list[ContractRef] = field(default_factory=list)


def lifecycle(ticker: str, entry: str, stop: str, target: str, new_stop: str,
              reduce_at: str, *, qualifier: str = "") -> list[Step]:
    q = f" {qualifier}" if qualifier else ""
    return [
        Step(0, f"We are now long ${ticker}{q} at {entry}. Stop {stop}. Target {target}.",
             "EXECUTED", action="OPEN"),
        Step(60, f"Raising our ${ticker} stop to {new_stop}.", "EXECUTED", action="MODIFY"),
        Step(120, f"Selling half of our ${ticker} long at {reduce_at}.", "EXECUTED",
             action="REDUCE"),
        Step(180, f"Closing the rest of our ${ticker} long.", "EXECUTED", action="CLOSE"),
    ]


def scenarios() -> list[Scenario]:
    option = [
        Step(0, "Buying $QSTK 2026-09-18 105 call at 4.22. Stop 3.40. Target 6.00.", "EXECUTED",
             action="OPEN"),
        Step(60, "Raising our $QSTK stop to 3.80 on the call.", "EXECUTED", action="MODIFY"),
        Step(120, "Selling half of our $QSTK calls at 4.30.", "EXECUTED", action="REDUCE"),
        Step(180, "Closing our $QSTK calls.", "EXECUTED", action="CLOSE"),
        Step(240, "Buying $QSTK 2026-09-18 call at 4.22. Stop 3.40.", "HOLD",
             "OPTION_TERMS_INCOMPLETE", action="OPEN"),
    ]
    return [
        Scenario("index", "QIDX", "REFERENCE_TRIGGER",
                 lifecycle("QIDX", "5,010", "4,980", "5,070", "4,995", "5,030")),
        Scenario("energy", "QOIL", "DIRECT",
                 # The entry at the ask: an explicit limit is the stated price itself (owner,
                 # 12 Nov 2026); a stated price below the ask rests.
                 [*lifecycle("QOIL", "71.52", "69.80", "75.00", "70.40", "71.80",
                             qualifier="Jul 2026"),
                  Step(240, "We are now long $QOIL Jun 2026 at 70.10. Stop 68.90.", "HOLD",
                       "CONTRACT_IN_ROLL_WINDOW", action="OPEN")]),
        Scenario("metal", "QMTL", "BASIS_SHIFT",
                 # 1,850.1 converts to 1862.1, the execution ask (basis +12).
                 [*lifecycle("QMTL", "1,850.1", "1,835", "1,880", "1,842", "1,851"),
                  Step(240, "We are now long $QMTL at 1,850. Stop 1,835.", "HOLD",
                       "BASIS_EXCEEDS_CAP", action="OPEN")],
                 quotes=metal_quotes),
        Scenario("option", "QSTK.OPT", "DIRECT", option),
    ]


def unapproved(base: list[Scenario]) -> list[Scenario]:
    return [
        Scenario(f"{s.name} (approved = false)", s.family, s.treatment,
                 [Step(x.offset, x.text, "HOLD", "MAPPING_NOT_APPROVED", x.action)
                  for x in s.steps], s.quotes)
        for s in base
    ]


# ------------------------------------------------------------------ running

POLICY = {
    "quantity": 2,
    "quantity_per_add": 2,
    "max_price_deviation_pct": "1.0",
    "max_spread_pct": "1.0",
    "max_notional_usd": "100000",
    "max_signal_age_seconds": 90,
    "max_quote_age_seconds": 30,
    "max_instrument_exposure_usd": "100000",
    "max_aggregate_exposure_usd": "400000",
}
SERVICE = {"cadence_seconds": 5, "max_backoff_seconds": 60, "failure_alert_after": 3,
           "stuck_after_seconds": 60}
EXECUTION = {
    "expected_account": "SIM-PAPER-0001",
    "max_submit_attempts": 2,
    "settle_seconds": 10,
    "exit_no_trace_lookups": 3,
    "max_intent_age_seconds": 120,
    "max_exit_intent_age_seconds": 120,
    "lookup_horizon_seconds": 21600,
    "confirm_attempts": 3,
    "confirm_wait_seconds": 0,
}


def post(pid: str, offset: float, text: str) -> SourcePost:
    published = RECORDED + timedelta(seconds=offset)
    return SourcePost(
        source_id=pid, post_id=pid, revision=1, author_id=AUTHOR, verified_author=True,
        text=text, text_complete=True, published_at=published,
        received_at=published + timedelta(seconds=2), provenance=Provenance.SYNTHETIC,
    )


@dataclass
class StepResult:
    step: Step
    action: str | None
    status: str | None
    reasons: list[str]
    order_ref: str | None
    ok: bool


@dataclass
class ScenarioResult:
    scenario: Scenario
    steps: list[StepResult]
    reconciliation_clean: bool
    unexplained: list[str]
    flat: bool
    broker_orders: int
    duplicate_refs: int
    basis_records: list[str]
    coverage: list[dict]
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return (
            all(s.ok for s in self.steps)
            and self.reconciliation_clean
            and not self.unexplained
            and self.flat
            and self.duplicate_refs == 0
            and not self.notes
        )


class MockMultiAssetRun:
    """One scenario's runtime on temp files (the shape of tests/runtime_harness.Runtime)."""

    def __init__(self, folder: Path, scenario: Scenario, *, approved: bool = True,
                 source_offset: int = 0, mapping_text: str | None = None, risk: bool = True):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.scenario = scenario
        self.clock = SimClock(START)
        self.posts = [
            post(str(9000 + source_offset + i), s.offset, s.text)
            for i, s in enumerate(scenario.steps)
        ]
        posts_file = self.folder / "posts.jsonl"
        posts_file.write_text("".join(p.model_dump_json() + "\n" for p in self.posts))
        self.mapping_file = self.folder / "instrument_mapping.toml"
        self.mapping_file.write_text(mapping_text or mapping_toml(approved=approved))
        raw = {
            "mode": "mock",
            "paths": {"state_dir": str(self.folder / "state"),
                      "halt_file": str(self.folder / "HALT"),
                      "mapping_file": str(self.mapping_file)},
            "source": {"kind": "replay", "file": str(posts_file), "compression": 1.0},
            "service": dict(SERVICE),
            "policy": dict(POLICY),
            "execution": dict(EXECUTION),
        }
        if risk:
            raw["risk"] = dict(INVENTED_RISK_TABLE)
        self.config = parse_config(raw, base=self.folder)
        self.store = RuntimeStore(self.config.runtime_db)
        self.broker = SimBroker(self.config.broker_db, clock=self.clock.now)
        for c in CONTRACTS:
            self.broker.register_contract(c)
        self.source = ReplayFileSource(self.config.source.file, self.store, clock=self.clock.now,
                                       compression=1.0)
        self.service = RuntimeService(self.config, source=self.source, broker=self.broker,
                                      clock=self.clock.now, store=self.store,
                                      notifier=_Quiet())

    def publish_quotes(self) -> None:
        elapsed = (self.clock.now() - START).total_seconds()
        for contract, (bid, ask, last) in self.scenario.quotes(elapsed).items():
            self.broker.set_quote(contract.symbol, bid, ask, last=last, contract=contract,
                                  received_at=self.clock.now())

    def step(self, seconds: float = 5.0) -> None:
        self.publish_quotes()
        self.service.run_once()
        self.clock.advance(seconds)

    def run(self) -> None:
        last = max(s.offset for s in self.scenario.steps)
        while (self.clock.now() - START).total_seconds() <= last + 30:
            self.step()

    def close(self) -> None:
        self.service.close()


class _Quiet:
    def __init__(self):
        self.sent: list[tuple[str, str, str]] = []

    def notify(self, kind: str, title: str, message: str) -> None:
        self.sent.append((kind, title, message))


def run_scenario(folder: Path, scenario: Scenario, *, approved: bool = True,
                 source_offset: int = 0, risk: bool = True) -> ScenarioResult:
    run = MockMultiAssetRun(folder, scenario, approved=approved, source_offset=source_offset,
                            risk=risk)
    try:
        run.run()
        return _result(run)
    finally:
        run.close()


def _result(run: MockMultiAssetRun) -> ScenarioResult:
    actions = run.store.signal_actions()
    signals = {s["seq"]: s for s in run.store.signals()}
    by_source: dict[str, list[dict]] = {}
    for a in actions:
        by_source.setdefault(signals[a["seq"]]["source_id"], []).append(a)
    steps = []
    for p, step in zip(run.posts, run.scenario.steps, strict=True):
        rows = [a for a in by_source.get(p.source_id, []) if a["action"] not in (None, "NO_TRADE")]
        a = rows[0] if len(rows) == 1 else None
        status = a["status"] if a else None
        reasons = a["reasons"] if a else []
        ok = (
            a is not None
            and status == step.expect
            and (step.reason is None or any(step.reason in r for r in reasons))
            and (step.action is None or a["action"] == step.action)
            and (step.expect != "HOLD" or a["order_ref"] is None)
        )
        steps.append(StepResult(step, a["action"] if a else None, status, reasons,
                                a["order_ref"] if a else None, ok))
    rec = run.service.last_reconciliation
    orders = run.broker.all_orders()
    refs = [o.order_ref for o in orders]
    flat = not run.broker.positions() and not run.broker.open_orders() and all(
        p.quantity == 0 for p in run.service.coordinator.ledger()
    )
    basis = []
    for i in run.service.coordinator.store.intents():
        if i.plan:
            plan = json.loads(i.plan)
            if plan.get("basis_value") is not None:
                basis.append(
                    f"{i.action.value}: basis {plan['basis_value']} (execution mid "
                    f"{plan['basis_execution_mid']} - reference mid {plan['basis_reference_mid']})"
                    + (f", backstop {plan['backstop_stop']}" if plan.get("backstop_stop") else "")
                    + (f", stop {plan['publisher_stop']} -> {plan['stop']}"
                       if plan.get("protection_series") == "EXECUTION" and plan.get("stop")
                       else "")
                )
    notes = []
    approved_family = any(s.expect == "EXECUTED" for s in run.scenario.steps)
    if not approved_family and run.broker.submit_calls:
        notes.append(f"unapproved family reached the broker: {run.broker.submit_calls} submits")
    policy = MappingPolicy.from_toml(run.mapping_file)
    return ScenarioResult(
        scenario=run.scenario,
        steps=steps,
        reconciliation_clean=bool(rec and rec.clean),
        unexplained=[d.kind.value + ": " + d.detail for d in (rec.blocking if rec else [])],
        flat=flat,
        broker_orders=len(orders),
        duplicate_refs=len(refs) - len(set(refs)),
        basis_records=basis,
        coverage=[c for c in coverage_report(policy, actions) if c["signals"]],
        notes=notes,
    )


def run_all(root: Path, *, risk: bool = True) -> list[ScenarioResult]:
    """Every scenario; ``risk=False`` runs them without the invented [risk] table."""
    base = scenarios()
    out = []
    for i, s in enumerate(base):
        out.append(run_scenario(root / s.name, s, source_offset=100 * i, risk=risk))
    for i, s in enumerate(unapproved(base)):
        out.append(run_scenario(root / f"{base[i].name}-unapproved", s, approved=False,
                                source_offset=1000 + 100 * i, risk=risk))
    return out
