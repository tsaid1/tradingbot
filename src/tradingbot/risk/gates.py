"""The nine independent risk gates. Pure functions of their inputs; see docs/RISK_GATES.md.

``evaluate`` runs every gate, always, and returns a ``RiskReport`` listing each gate's result and
every failing gate's codes. No gate reads another gate's result, and nothing here skips a gate.
A gate whose inputs the caller does not hold at this stage (the gateway has no account snapshot
or portfolio) reports ``NOT_EVALUATED``, which is never a pass: the coordinator's last check
evaluates all nine with every input (``stage="EXECUTION"``).

With ``policy=None`` (no ``[risk]`` table) the fixed cash baseline applies: G1 refuses any
borrowing and any short sale, G2 any instrument but a stock or ETF, G9 is today's rule, and
G3-G8 record ``RISK_BASELINE_CASH_ONLY`` (the exposure the baseline admits, a long cash stock or
ETF, is bounded by the existing caps and family rules, which the gateway and coordinator apply
as on main).
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal

from tradingbot.risk.policy import (
    FUNDING_FOR_SEC_TYPE,
    MARGIN_FUNDING,
    FamilyRiskTerms,
    RiskPolicy,
    parse_amount_or_pct,
)

Status = Literal["PASS", "FAIL", "NOT_EVALUATED", "BASELINE"]
Stage = Literal["GATEWAY", "EXECUTION"]

BASELINE = "RISK_BASELINE_CASH_ONLY"
FAILED = "RISK_GATES_FAILED"
# G1
BORROWING_NOT_PERMITTED = "RISK_BORROWING_NOT_PERMITTED"
BORROWING_LIMIT = "RISK_BORROWING_LIMIT"
SHORT_SALE_NOT_PERMITTED = "RISK_SHORT_SALE_NOT_PERMITTED"
# G2
NOT_ELIGIBLE = "RISK_INSTRUMENT_NOT_ELIGIBLE"
FUNDING_MISSING = "RISK_FAMILY_FUNDING_MISSING"
FUNDING_INCONSISTENT = "RISK_FAMILY_FUNDING_INCONSISTENT"
# G3 .. G7
GROSS_NOTIONAL_LIMIT = "RISK_GROSS_NOTIONAL_LIMIT"
PER_IDEA_LIMIT = "RISK_PER_IDEA_LIMIT"
STOP_UNKNOWN = "RISK_STOP_UNKNOWN"
MULTIPLIER_UNKNOWN = "RISK_MULTIPLIER_UNKNOWN"
PORTFOLIO_LIMIT = "RISK_PORTFOLIO_LIMIT"
PORTFOLIO_UNBOUNDED = "RISK_PORTFOLIO_UNBOUNDED"
MARGIN_LIMIT = "RISK_MARGIN_UTILISATION_LIMIT"
MARGIN_FIGURE_MISSING = "RISK_MARGIN_FIGURE_MISSING"
MARGIN_FIGURE_STALE = "RISK_MARGIN_FIGURE_STALE"
EXCESS_LIQUIDITY = "RISK_EXCESS_LIQUIDITY_BUFFER"
# G8
STOP_RULE_MISSING = "RISK_FAMILY_STOP_RULE_MISSING"
PRICE_RULE_MISSING = "RISK_FAMILY_PRICE_RULE_MISSING"
STOP_TOO_FAR = "RISK_FAMILY_STOP_TOO_FAR"
PRICE_OUTSIDE_COLLAR = "RISK_FAMILY_PRICE_OUTSIDE_COLLAR"
# G9
ENVIRONMENT_NOT_AUTHORIZED = "RISK_ENVIRONMENT_NOT_AUTHORIZED"
# common
INPUT_MISSING = "RISK_INPUT_MISSING"
INPUT_STALE = "RISK_INPUT_STALE"
CURRENCY_MISMATCH = "RISK_CURRENCY_MISMATCH"

GATES = (
    ("G1", "BORROWING"), ("G2", "ELIGIBILITY"), ("G3", "GROSS_NOTIONAL"),
    ("G4", "RISK_PER_IDEA"), ("G5", "PORTFOLIO_RISK"), ("G6", "MARGIN_UTILISATION"),
    ("G7", "EXCESS_LIQUIDITY"), ("G8", "FAMILY_STOP_AND_PRICE"), ("G9", "ENVIRONMENT"),
)
ANCHOR_ENVIRONMENTS = frozenset({"SIM", "PAPER"})
FUTURE_TOLERANCE_SECONDS = 5


# ----------------------------------------------------------------------------- inputs

@dataclass(frozen=True)
class Proposal:
    """One OPEN or ADD as the order would be sent. Prices are in one series (the execution
    series, or both in the reference series when ``scale_ratio`` is 1); ``stop`` is the
    publisher's stop as read, None when unknown. ``multiplier`` is the resolved contract's;
    ``multiplier_confirmed`` says it equals the family's stated multiplier."""
    sec_type: str
    side: Literal["LONG", "SHORT"]
    units: int
    entry: Decimal | None
    stop: Decimal | None
    multiplier: Decimal | None
    currency: str = "USD"
    multiplier_confirmed: bool = True
    touch: Decimal | None = None  # the marketable side of the quote (G8 collar)
    anchored: bool = False  # entry_rule REFERENCE_AS_COLLAR (G9 baseline)
    action: Literal["OPEN", "ADD"] = "OPEN"
    held_risk: Decimal = Decimal(0)  # an ADD: the idea's existing dollar risk


@dataclass(frozen=True)
class Exposure:
    """One open position or working entry already on the account (G3, G5, G6)."""
    sec_type: str
    units: int  # absolute
    mark: Decimal | None  # price per unit (quote mid, else cost)
    multiplier: Decimal
    entry: Decimal | None = None
    stop: Decimal | None = None
    protected: bool = True  # a working stop is known
    margin_initial: Decimal | None = None  # per unit, margin-funded only
    buy_cost: Decimal = Decimal(0)  # cash a working buy entry commits (G1)
    currency: str = "USD"


@dataclass(frozen=True)
class AccountSnapshot:
    """Account values as read (simulated account, or the read-only adapter's summary)."""
    read_at: datetime
    currency: str = "USD"
    net_liquidation: Decimal | None = None
    total_cash: Decimal | None = None
    excess_liquidity: Decimal | None = None
    init_margin: Decimal | None = None
    maint_margin: Decimal | None = None


@dataclass(frozen=True)
class GateResult:
    gate: str
    name: str
    status: Status
    codes: tuple[str, ...] = ()
    value: Decimal | None = None
    limit: Decimal | None = None


@dataclass(frozen=True)
class RiskReport:
    stage: Stage
    baseline: bool
    results: tuple[GateResult, ...]
    # Recorded on every decision, whether or not a gate fails.
    economic_leverage: Decimal | None = None  # gross notional / effective equity
    gross_notional: Decimal | None = None
    borrowing: Decimal | None = None  # conventional borrowing (G1)
    performance_margin: Decimal | None = None  # futures / CFD initial margin (G6)
    dollar_risk: Decimal | None = None  # this idea (G4)
    portfolio_risk: Decimal | None = None
    allocated_equity: Decimal | None = None
    net_liquidation: Decimal | None = None
    effective_equity: Decimal | None = None
    extra: dict = field(default_factory=dict)

    @property
    def failed(self) -> tuple[GateResult, ...]:
        return tuple(r for r in self.results if r.status == "FAIL")

    @property
    def passed(self) -> bool:
        """Every gate passed (or the baseline admits it). NOT_EVALUATED is never a pass."""
        return all(r.status in ("PASS", "BASELINE") for r in self.results)

    @property
    def refused(self) -> bool:
        return bool(self.failed)

    def reasons(self) -> list[str]:
        """[RISK_GATES_FAILED, every failing gate's codes in gate order], or []."""
        if not self.failed:
            return []
        out = [FAILED]
        for r in self.failed:
            out.extend(c for c in r.codes if c not in out)
        return out

    def record(self) -> dict:
        """Identifiers and numbers only, for the journal."""
        def s(v):
            return None if v is None else format(v, "f")
        return {
            "stage": self.stage, "baseline": self.baseline,
            "gates": {r.gate: {"status": r.status, "codes": list(r.codes), "value": s(r.value),
                               "limit": s(r.limit)} for r in self.results},
            "economic_leverage": s(self.economic_leverage), "gross_notional": s(self.gross_notional),
            "borrowing": s(self.borrowing), "performance_margin": s(self.performance_margin),
            "dollar_risk": s(self.dollar_risk), "portfolio_risk": s(self.portfolio_risk),
            "allocated_equity": s(self.allocated_equity), "net_liquidation": s(self.net_liquidation),
            "effective_equity": s(self.effective_equity),
        }


# ----------------------------------------------------------------------------- arithmetic

def dollar_risk(entry: Decimal | None, stop: Decimal | None, multiplier: Decimal | None,
                units: int, long: bool | None = None) -> Decimal | None:
    """|entry - stop| x multiplier x units; None when any input is unknown. With ``long`` given,
    a stop already beyond the entry in the favourable direction counts as 0."""
    if entry is None or stop is None or multiplier is None or entry <= 0 or stop <= 0 \
            or multiplier <= 0 or units < 0:
        return None
    if long is True and stop >= entry or long is False and stop <= entry:
        return Decimal(0)
    return abs(entry - stop) * multiplier * units


def _num(v) -> Decimal | None:
    return v if isinstance(v, Decimal) and v.is_finite() else None


class _Ctx:
    def __init__(self, policy, family, proposal, account, portfolio, environment, now, stage):
        self.policy: RiskPolicy | None = policy
        self.family: FamilyRiskTerms = family
        self.p: Proposal = proposal
        self.account: AccountSnapshot | None = account
        self.portfolio: Sequence[Exposure] | None = portfolio
        self.environment: str | None = environment
        self.now: datetime = now
        self.stage: Stage = stage
        self.long = proposal.side == "LONG"
        self.cost = (proposal.entry * proposal.units * proposal.multiplier
                     if proposal.entry is not None and proposal.multiplier is not None else None)

    # -- shared reads
    def account_problem(self, gate: str, *fields: str) -> list[str]:
        """Codes when the snapshot (or one of ``fields``) is missing or stale."""
        a = self.account
        if a is None:
            return [f"{INPUT_MISSING}:{gate}:ACCOUNT_VALUES"]
        age = (self.now - a.read_at).total_seconds() if a.read_at.tzinfo else None
        limit = self.policy.account_values_max_age_seconds if self.policy else None
        if age is None or age < -FUTURE_TOLERANCE_SECONDS or (limit is not None and age > limit):
            return [f"{INPUT_STALE}:{gate}:ACCOUNT_VALUES"]
        if self.policy is not None and a.currency != self.policy.currency:
            return [CURRENCY_MISMATCH]
        return [f"{INPUT_MISSING}:{gate}:{f.upper()}" for f in fields
                if _num(getattr(a, f)) is None]

    def effective_equity(self) -> Decimal | None:
        """min(allocated equity, net liquidation); without a policy, net liquidation."""
        nl = _num(self.account.net_liquidation) if self.account else None
        if self.policy is None:
            return nl
        if nl is None:
            return None
        return min(self.policy.allocated_equity, nl)

    def money_problem(self) -> list[str]:
        if self.policy is not None and self.p.currency != self.policy.currency:
            return [CURRENCY_MISMATCH]
        return []

    def portfolio_missing(self, gate: str) -> list[str]:
        return [] if self.portfolio is not None else [f"{INPUT_MISSING}:{gate}:PORTFOLIO"]

    def gross_notional(self) -> Decimal | None:
        if self.portfolio is None or self.cost is None:
            return None
        total = abs(self.cost)
        for e in self.portfolio:
            if e.mark is None:
                return None
            total += abs(e.mark * e.units * e.multiplier)
        return total

    def own_margin(self) -> tuple[Decimal, list[str]]:
        """The proposal's own initial margin (0 unless margin-funded), and its problems."""
        if not (self.family.funding in MARGIN_FUNDING or self.p.sec_type in ("FUT", "CFD")):
            return Decimal(0), []
        m = self.family.margin
        if m is None:
            return Decimal(0), [MARGIN_FIGURE_MISSING]
        if m.stale(self.now.date()):
            return Decimal(0), [MARGIN_FIGURE_STALE]
        return m.initial * self.p.units, []

    def margin_numerator(self) -> tuple[Decimal | None, Decimal, list[str]]:
        """(account margin after the order, the proposal's own margin, problems)."""
        own, problems = self.own_margin()
        if self.portfolio is None:
            return None, own, problems
        computed = Decimal(0)
        for e in self.portfolio:
            if e.sec_type in ("FUT", "CFD"):
                if e.margin_initial is None:
                    problems.append(MARGIN_FIGURE_MISSING)
                    continue
                computed += e.margin_initial * e.units
        broker = _num(self.account.init_margin) if self.account else None
        existing = max(computed, broker) if broker is not None else computed
        return existing + own, own, problems


def _result(gate: str, codes: list[str], *, value=None, limit=None,
            status: Status | None = None) -> GateResult:
    name = dict(GATES)[gate]
    if status is None:
        status = "FAIL" if codes else "PASS"
    return GateResult(gate, name, status, tuple(dict.fromkeys(codes)), value, limit)


def _not_evaluated(gate: str) -> GateResult:
    return _result(gate, [], status="NOT_EVALUATED")


def _baseline(gate: str) -> GateResult:
    return _result(gate, [BASELINE], status="BASELINE")


# ----------------------------------------------------------------------------- the gates

def g1_borrowing(c: _Ctx) -> tuple[GateResult, Decimal | None]:
    p, fam = c.p, c.family
    codes: list[str] = []
    rules = c.policy.borrowing if c.policy else None
    if not c.long and p.sec_type in ("STK", "OPT", "CMDTY") and not (
            rules and rules.short_sale_permitted):
        codes.append(SHORT_SALE_NOT_PERMITTED)
    if fam.funding == "MARGIN_BORROWING" and not (rules and rules.permitted):
        codes.append(BORROWING_NOT_PERMITTED)
    if c.policy is None:
        return (_result("G1", codes) if codes else _baseline("G1")), None
    if p.sec_type in ("FUT", "CFD") or not c.long:
        # Performance margin is not borrowing (G6); a short sale is decided above.
        return _result("G1", codes, value=Decimal(0)), Decimal(0)
    if c.stage == "GATEWAY":
        return (_result("G1", codes) if codes else _not_evaluated("G1")), None
    codes += c.money_problem() + c.portfolio_missing("G1") + c.account_problem("G1", "total_cash")
    if c.cost is None:
        codes.append(f"{INPUT_MISSING}:G1:COST")
    if codes:
        return _result("G1", codes), None
    committed = sum((e.buy_cost for e in c.portfolio), Decimal(0))
    borrowed = max(Decimal(0), c.cost + committed - c.account.total_cash)
    if borrowed > 0 and not rules.permitted:
        codes.append(BORROWING_NOT_PERMITTED)
    elif rules.permitted and borrowed > rules.max_borrowed:
        codes.append(BORROWING_LIMIT)
    return _result("G1", codes, value=borrowed, limit=rules.max_borrowed or Decimal(0)), borrowed


def g2_eligibility(c: _Ctx) -> GateResult:
    sec, fam = c.p.sec_type, c.family
    codes: list[str] = []
    if c.policy is None:
        if sec != "STK" or fam.funding not in (None, "CASH", "MARGIN_BORROWING"):
            codes.append(f"{NOT_ELIGIBLE}:{sec}")
        return _result("G2", codes) if codes else _baseline("G2")
    e = c.policy.eligibility
    allowed = {"STK": True, "FUT": e.futures, "OPT": e.options, "CFD": e.cfd}.get(sec, False)
    if not allowed:
        codes.append(f"{NOT_ELIGIBLE}:{sec}")
    if fam.funding is None:
        codes.append(FUNDING_MISSING)
    elif fam.funding not in FUNDING_FOR_SEC_TYPE.get(sec, frozenset()):
        codes.append(FUNDING_INCONSISTENT)
    return _result("G2", codes)


def g3_gross_notional(c: _Ctx) -> tuple[GateResult, Decimal | None, Decimal | None]:
    gross = c.gross_notional()
    equity = c.effective_equity()
    leverage = gross / equity if gross is not None and equity and equity > 0 else None
    if c.policy is None:
        return _baseline("G3"), gross, leverage
    if c.stage == "GATEWAY":
        return _not_evaluated("G3"), gross, leverage
    codes = c.money_problem() + c.portfolio_missing("G3") \
        + c.account_problem("G3", "net_liquidation")
    if not codes and gross is None:
        codes.append(f"{INPUT_MISSING}:G3:MARK")
    if not codes and (equity is None or equity <= 0):
        codes.append(GROSS_NOTIONAL_LIMIT)
    if not codes and leverage > c.policy.max_gross_notional_ratio:
        codes.append(GROSS_NOTIONAL_LIMIT)
    return (_result("G3", codes, value=leverage, limit=c.policy.max_gross_notional_ratio),
            gross, leverage)


def _idea_risk(c: _Ctx) -> tuple[Decimal | None, list[str]]:
    p = c.p
    codes = []
    if p.stop is None:
        codes.append(STOP_UNKNOWN)
    if p.multiplier is None or not p.multiplier_confirmed or (
            c.family.multiplier is not None and p.multiplier != c.family.multiplier) or (
            c.family.multiplier is None and p.sec_type != "STK"):
        codes.append(MULTIPLIER_UNKNOWN)
    risk = dollar_risk(p.entry, p.stop, p.multiplier, p.units, c.long)
    if risk is None and not codes:
        codes.append(STOP_UNKNOWN)
    return (risk if not codes else None), codes


def g4_per_idea(c: _Ctx) -> tuple[GateResult, Decimal | None]:
    risk, codes = _idea_risk(c)
    total = risk + c.p.held_risk if risk is not None else None
    if c.policy is None:
        return _baseline("G4"), total
    limit = c.family.max_risk_per_idea
    codes = c.money_problem() + codes
    if limit is None:
        codes.append(f"{INPUT_MISSING}:G4:MAX_RISK_PER_IDEA")
    if not codes and total > limit:
        codes.append(PER_IDEA_LIMIT)
    return _result("G4", codes, value=total, limit=limit), total


def g5_portfolio(c: _Ctx) -> tuple[GateResult, Decimal | None]:
    risk, _ = _idea_risk(c)
    total = None
    unbounded = False
    if c.portfolio is not None and risk is not None:
        total = risk
        for e in c.portfolio:
            r = dollar_risk(e.entry, e.stop, e.multiplier, e.units)
            if r is None or not e.protected:
                unbounded = True
                break
            total += r
    if unbounded:
        total = None
    if c.policy is None:
        return _baseline("G5"), total
    if c.stage == "GATEWAY":
        return _not_evaluated("G5"), total
    codes = c.money_problem() + c.portfolio_missing("G5")
    if not codes and unbounded:
        codes.append(PORTFOLIO_UNBOUNDED)
    elif not codes and risk is None:
        codes.append(STOP_UNKNOWN)
    elif not codes and total > c.policy.max_portfolio_risk:
        codes.append(PORTFOLIO_LIMIT)
    return _result("G5", codes, value=total, limit=c.policy.max_portfolio_risk), total


def g6_margin(c: _Ctx) -> tuple[GateResult, Decimal | None]:
    numerator, _own, problems = c.margin_numerator()
    if c.policy is None:
        return _baseline("G6"), numerator
    if c.stage == "GATEWAY":
        return (_result("G6", problems) if problems else _not_evaluated("G6")), numerator
    codes = c.money_problem() + problems + c.portfolio_missing("G6") \
        + c.account_problem("G6", "net_liquidation", "init_margin")
    equity = c.effective_equity()
    utilisation = numerator / equity if numerator is not None and equity and equity > 0 else None
    if not codes and (utilisation is None or utilisation > c.policy.max_margin_utilisation):
        codes.append(MARGIN_LIMIT)
    return (_result("G6", codes, value=utilisation, limit=c.policy.max_margin_utilisation),
            numerator)


def g7_excess_liquidity(c: _Ctx) -> GateResult:
    if c.policy is None:
        return _baseline("G7")
    if c.stage == "GATEWAY":
        return _not_evaluated("G7")
    own, problems = c.own_margin()
    codes = c.money_problem() + problems + c.account_problem("G7", "excess_liquidity",
                                                             "net_liquidation")
    if c.cost is None:
        codes.append(f"{INPUT_MISSING}:G7:COST")
    if codes:
        return _result("G7", codes)
    margin_funded = c.family.funding in MARGIN_FUNDING or c.p.sec_type in ("FUT", "CFD")
    use = own if margin_funded else (c.cost if c.long else Decimal(0))
    after = c.account.excess_liquidity - use
    spec, pct = parse_amount_or_pct(c.policy.min_excess_liquidity)
    equity = c.effective_equity()
    buffer = (equity * spec / 100) if pct else spec
    if equity is None or after < buffer:
        codes.append(EXCESS_LIQUIDITY)
    return _result("G7", codes, value=after, limit=buffer)


def g8_family_rules(c: _Ctx) -> GateResult:
    if c.policy is None:
        return _baseline("G8")
    fam, p = c.family, c.p
    codes = []
    stop_pct = parse_amount_or_pct(fam.max_stop_distance)
    collar = parse_amount_or_pct(fam.entry_collar)
    if stop_pct is None or not stop_pct[1]:
        codes.append(STOP_RULE_MISSING)
    if collar is None:
        codes.append(PRICE_RULE_MISSING)
    if p.stop is None:
        codes.append(STOP_UNKNOWN)
    if codes:
        return _result("G8", codes)
    if p.entry is None or p.entry <= 0:
        return _result("G8", [f"{INPUT_MISSING}:G8:ENTRY"])
    if abs(p.entry - p.stop) / p.entry * 100 > stop_pct[0]:
        codes.append(STOP_TOO_FAR)
    if p.touch is None:
        codes.append(f"{INPUT_MISSING}:G8:QUOTE")
    else:
        move = abs(p.touch - p.entry)
        bound = p.entry * collar[0] / 100 if collar[1] else collar[0]
        if move > bound:
            codes.append(PRICE_OUTSIDE_COLLAR)
    return _result("G8", codes)


def g9_environment(c: _Ctx) -> GateResult:
    env = c.environment
    label = env or "UNDECLARED"
    if c.policy is None:
        # Today's rule: an anchored entry needs SIM or PAPER; LIVE is refused for every entry.
        bad = env == "LIVE" or (c.p.anchored and env not in ANCHOR_ENVIRONMENTS)
        return _result("G9", [f"{ENVIRONMENT_NOT_AUTHORIZED}:{label}"]) if bad \
            else _baseline("G9")
    ok = env is not None and env in c.policy.environments
    return _result("G9", [] if ok else [f"{ENVIRONMENT_NOT_AUTHORIZED}:{label}"])


def evaluate(policy: RiskPolicy | None, family: FamilyRiskTerms, proposal: Proposal, *,
             account: AccountSnapshot | None = None,
             portfolio: Sequence[Exposure] | None = None,
             environment: str | None = None, now: datetime | None = None,
             stage: Stage = "EXECUTION") -> RiskReport:
    """Run all nine gates. ``environment`` is the broker's declaration (SIM, PAPER, LIVE, None)."""
    now = now or datetime.now(UTC)
    c = _Ctx(policy, family, proposal, account, portfolio, environment, now, stage)
    r1, borrowed = g1_borrowing(c)
    r2 = g2_eligibility(c)
    r3, gross, leverage = g3_gross_notional(c)
    r4, idea_risk = g4_per_idea(c)
    r5, portfolio_risk = g5_portfolio(c)
    r6, margin = g6_margin(c)
    r7 = g7_excess_liquidity(c)
    r8 = g8_family_rules(c)
    r9 = g9_environment(c)
    nl = _num(account.net_liquidation) if account else None
    return RiskReport(
        stage=stage, baseline=policy is None, results=(r1, r2, r3, r4, r5, r6, r7, r8, r9),
        economic_leverage=leverage, gross_notional=gross, borrowing=borrowed,
        performance_margin=margin, dollar_risk=idea_risk, portfolio_risk=portfolio_risk,
        allocated_equity=policy.allocated_equity if policy else None, net_liquidation=nl,
        effective_equity=c.effective_equity())


# ----------------------------------------------------------------------------- reporting hook

def what_if(risk_table: dict | RiskPolicy, family, *, side: Literal["LONG", "SHORT"],
            entry_reference: Decimal, stop: Decimal | None, units: int,
            touch: Decimal | None = None, account: AccountSnapshot | None = None,
            portfolio: Sequence[Exposure] = (), environment: str = "SIM",
            now: datetime | None = None) -> RiskReport:
    """The nine gate results for a hypothetical ``[risk]`` table and one typed proposal, for an
    owner's what-if count. ``family`` is a ``FamilyMapping`` or ``FamilyRiskTerms``; its stated
    multiplier is the contract's. With no ``account``, a flat account is assumed whose net
    liquidation, cash and excess liquidity equal the table's allocated equity and whose margin
    is zero (derived from the table, no other value). ``touch`` defaults to the entry reference.
    Pure: no broker, no store, no network."""
    policy = risk_table if isinstance(risk_table, RiskPolicy) else RiskPolicy.from_table(risk_table)
    terms = family if isinstance(family, FamilyRiskTerms) else FamilyRiskTerms.of(family)
    now = now or datetime.now(UTC)
    if account is None:
        eq = policy.allocated_equity
        account = AccountSnapshot(read_at=now, currency=policy.currency, net_liquidation=eq,
                                  total_cash=eq, excess_liquidity=eq, init_margin=Decimal(0),
                                  maint_margin=Decimal(0))
    proposal = Proposal(sec_type=terms.sec_type, side=side, units=units, entry=entry_reference,
                        stop=stop, multiplier=terms.multiplier,
                        touch=entry_reference if touch is None else touch)
    return evaluate(policy, terms, proposal, account=account, portfolio=list(portfolio),
                    environment=environment, now=now, stage="EXECUTION")
