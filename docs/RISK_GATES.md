# Independent risk gates (design, phase 1: not implemented)

Status: DESIGN ONLY, for review. Nothing here grants paper or funded trading authority. No
number in this note is a proposed value: every limit is a field the owner fills.

## 1. The nine gates

Every OPEN and ADD (anything that creates or increases exposure) is evaluated by all nine gates.
REDUCE, CLOSE and a stop-tightening MODIFY are never refused by a risk gate (refusing an exit
would increase risk). Each gate is a pure function of its inputs with its own reason code.

| # | Gate | Lives in | Fields (owner supplies every value) | Reason codes |
|---|---|---|---|---|
| G1 | Margin borrowing | account `[risk.borrowing]` | `permitted` (bool), `max_borrowed` (USD, required if permitted), `short_sale_permitted` (bool) | `RISK_BORROWING_NOT_PERMITTED`, `RISK_BORROWING_LIMIT`, `RISK_SHORT_SALE_NOT_PERMITTED` |
| G2 | Futures / derivative eligibility | account `[risk.eligibility]` AND family `funding` | account: `futures`, `options`, `cfd` (bools); family: `funding = "CASH" \| "MARGIN_BORROWING" \| "FUTURES_MARGIN" \| "OPTION_PREMIUM"`, must agree with `execution.sec_type` | `RISK_INSTRUMENT_NOT_ELIGIBLE:<FUT\|OPT\|CFD>`, `RISK_FAMILY_FUNDING_MISSING`, `RISK_FAMILY_FUNDING_INCONSISTENT` |
| G3 | Gross notional / allocated equity | account `[risk]` | `allocated_equity` (USD), `max_gross_notional_ratio` | `RISK_GROSS_NOTIONAL_LIMIT` |
| G4 | Dollar risk per idea | family `risk` | `max_risk_per_idea` (USD) | `RISK_PER_IDEA_LIMIT`, `RISK_STOP_UNKNOWN`, `RISK_MULTIPLIER_UNKNOWN` |
| G5 | Aggregate portfolio risk | account `[risk]` | `max_portfolio_risk` (USD) | `RISK_PORTFOLIO_LIMIT`, `RISK_PORTFOLIO_UNBOUNDED` |
| G6 | Margin utilisation (futures performance margin) | account `[risk]` + family `margin` | account: `max_margin_utilisation` (fraction of equity); family: `margin = { initial, maintenance, currency, verified_at, max_age_days }` | `RISK_MARGIN_UTILISATION_LIMIT`, `RISK_MARGIN_FIGURE_MISSING`, `RISK_MARGIN_FIGURE_STALE` |
| G7 | Minimum excess-liquidity buffer | account `[risk]` | `min_excess_liquidity` (`"<USD>"` or `"<pct>%"` of effective equity) | `RISK_EXCESS_LIQUIDITY_BUFFER` |
| G8 | Family stop and price rules | family entry | `max_stop_distance` (exists), `entry_collar` (new, `"<price>"` or `"<pct>%"`), `entry_anchor` (exists, only with the entry rule) | `RISK_FAMILY_STOP_RULE_MISSING`, `RISK_FAMILY_PRICE_RULE_MISSING`, plus the existing `PROTECTIVE_STOP_TOO_FAR_FROM_*`, `ENTRY_PRICE_COLLAR`, `ENTRY_ANCHOR_*` |
| G9 | Paper versus funded | account `[risk]` + broker declaration | `environments` (subset of `SIM`, `PAPER`; `LIVE` is not a valid value); `funded_authorization` (ships absent; any value refuses to start in this change) | `RISK_ENVIRONMENT_NOT_AUTHORIZED:<SIM\|PAPER\|LIVE\|UNDECLARED>`, `RISK_FUNDED_NOT_AUTHORIZED` |

Common codes: `RISK_INPUT_MISSING:<gate>:<field>`, `RISK_INPUT_STALE:<gate>:<source>`,
`RISK_CURRENCY_MISMATCH` (no FX conversion; a non-account-currency contract refuses). The risk
table and the family risk fields are validated at start: unknown key, missing, zero, negative,
NaN, infinite or malformed value, or a partial table refuses to start (`ConfigError`, field paths
only). A per-decision missing input refuses that decision; nothing means "no limit".

**Definitions.**
* Dollar risk of one idea = |entry reference - stop| x multiplier x units, in account currency.
  Entry reference = the limit actually sent (anchored: the gateway limit; explicit: the stated
  entry snapped to tick). Stop = the publisher's stop as read, in the execution series. For
  REFERENCE_TRIGGER both levels are reference-series; the distance is taken there and is valid
  only when `basis_shift.scale_ratio = 1`, and the larger of it and the backstop distance counts;
  otherwise `RISK_STOP_UNKNOWN`. No stop, or a multiplier not stated in the family entry AND
  confirmed equal by the resolved contract (both default to 1 today), refuses. A stop tightened
  above entry counts as 0. This is distance to stop, not maximum loss: gaps and slippage exceed it.
* Portfolio risk = the same quantity over every open idea (ledger quantity, avg entry, current
  working stop), every working or unresolved entry (remaining quantity at its limit and planned
  stop) and the proposed one. A broker-only position, or one whose protection is UNKNOWN, has
  unbounded risk: `RISK_PORTFOLIO_UNBOUNDED`.
* Gross notional = sum of |quantity| x mark x multiplier over ledger and broker positions plus
  working entries, plus the proposal: exactly what `ExecutionCoordinator._exposure` computes today.
  Economic leverage = gross notional / effective equity, recorded on every decision (stock or
  future) whether or not G3 fails.
* Effective equity (constraint 6): min(`allocated_equity`, broker net liquidation). Both are
  recorded; net liquidation unreadable or stale refuses. Recommendation in Q4.
* Borrowing (G1, securities only) = max(0, purchase cost + cost of working buy entries - cash).
  Futures initial margin is NOT borrowing; it is G6. A short stock sale borrows the security: G1.
* Margin utilisation (G6) = (sum of initial margin per unit x units over open and working futures
  plus the proposal) / effective equity. Numerator = max(computed from the owner's figures,
  broker-reported initial margin when readable): a broker figure above ours can only tighten.
* Excess liquidity after the order (G7) = broker excess liquidity - proposed initial margin (or
  purchase cost for a cash buy) >= the buffer.

## 2. What exists today

| Control | Today | Where |
|---|---|---|
| Per-idea notional | `sizing.max_notional` (family), `max_notional_usd` (global), with multiplier | `validator._mapped_open`, `coordinator._entry_checks` |
| Instrument / aggregate exposure | `max_instrument_exposure_usd`, `max_aggregate_exposure_usd`: ABSOLUTE USD, not relative to equity | `coordinator._entry_checks`, `_exposure` |
| Units | `max_units`, `size_increment` | gateway |
| Stop distance | `max_stop_distance` per family, optional unless entry rule; ceiling 10% | gateway + `_entry_checks` |
| Price collar | `entry_anchor` limits per family (entry rule only); explicit entries use the GLOBAL `max_price_deviation_pct` for every family (conflicts with constraint 4) | gateway + `_entry_checks` |
| Long only | `long_only` per family (`SHORT_NOT_PERMITTED_FOR_FAMILY`) | gateway |
| Environment | runtime `mode` = mock/shadow only; paper ports only; `BrokerEnvironment` SIM/PAPER/LIVE, enforced only for anchored entries (`ENTRY_ANCHOR_PAPER_ONLY`); `entry_anchor.environment = "PAPER"`; `MockPolicy.paper_only: Literal[True]` | `execution/broker.py`, coordinator |
| Asset class | stock path: EQUITY/ETF only; shadow resolves STK only; mapped FUT/OPT need an approved entry (none approved) | gateway, `shadow_broker.py` |
| Dollar risk, portfolio risk, borrowing, margin, excess liquidity, allocated equity, funded key | NONE | |

Broker facts: the `Broker` protocol exposes account id, contract (with multiplier, tick,
increment), quotes, orders, fills and positions (signed quantity, avg cost). It exposes NO account
values. The read-only adapter `ibkr.session.account_summary()` reads, via the allow-listed
`REQ_ACCOUNT_SUMMARY`: `AccountType, NetLiquidation, TotalCashValue, BuyingPower, AvailableFunds`.
It does NOT read `ExcessLiquidity`, `InitMarginReq`, `MaintMarginReq` (or `FullInitMarginReq`,
`GrossPositionValue`). Adding them is a change to `ACCOUNT_SUMMARY_TAGS` only: the same message,
no new entry in `ALLOWED_OUT_MESSAGES` or `ALLOWED_CLIENT_METHODS` (a test pins both sets). The
broker's margin for a prospective order needs a what-if order (`placeOrder`); it is never sent.
IBKR refreshes account-summary values periodically (minutes, not seconds), so they are a
cross-check, not a live figure.

## 3. Evaluation and reporting

New pure module `tradingbot.risk.gates.evaluate(inputs, policy, family) -> RiskReport`: all nine
gates always run, in no meaningful order, each returning PASS or FAIL with value, limit and the
inputs' timestamps. The report fails if any gate fails and lists every failing gate's codes.
Called at two points with the same function:
1. **Gateway** (`_mapped_open`, mapped ADD, and the stock path open/add): gates whose inputs the
   gateway holds (G1 structural side and sec_type, G2, G4, G8, G9 policy side). Account and
   portfolio gates report `NOT_EVALUATED_AT_GATEWAY`, which is never a pass. A failure is a HOLD
   with `RISK_GATES_FAILED` and every code.
2. **Coordinator `_entry_checks`** (every entry, add and resend): all nine with the ledger, broker
   positions, account snapshot and the limit actually sent. This is the authoritative check; no
   order is submitted unless all nine PASS here. Its failures are appended to (not replaced by)
   any other refusal reason found in the same check.
Every evaluation is journalled as one `RISK_GATES` record (identifiers, numbers, codes; no text).

## 4. Data, sources and staleness

| Input | Mock | Shadow | Real paper session (experiments only) | Staleness rule |
|---|---|---|---|---|
| Multiplier, sec_type, currency | family entry + SimBroker contract | family + read-only contract details | family + contract details | per decision; mismatch refuses |
| Entry, stop | gateway plan | same | same | existing quote rules |
| Positions, working entries, stops | ledger + SimBroker | ledger + SimBroker (simulated) | ledger + broker | existing sync rule (stale ledger refuses) |
| Net liquidation, cash, excess liquidity, init/maint margin | simulated account in SimBroker (starting cash = invented test value; margin from the same owner figures) | the SIMULATED account: the real paper account's values do not reflect simulated positions and are recorded as evidence only, never gated on | read-only `account_summary` (extended tags), same paper account | owner `account_values_max_age_seconds`; missing tag or stale refuses |
| Per-unit margin | owner figure | owner figure | owner figure, cross-checked | `verified_at + max_age_days` past refuses |
| Environment | SimBroker `SIM` | ShadowBroker `SIM` | broker must declare `PAPER` | per decision; undeclared refuses |

New additive protocol `AccountValues.account_values() -> AccountSnapshot` (like
`ConditionalOrders`); a broker without it has no readable values: refuses whenever a gate needs one.

## 5. "Disabled by default", concretely

* Shipped config: no `[risk]` table; family entries gain no leveraged values; examples show field
  names with `OWNER_VALUE` placeholders, commented out. `funded_authorization` exists nowhere.
* No `[risk]` table = the **cash baseline** (a fixed policy in code, no numbers): G1 borrowing and
  short sales not permitted, G2 no FUT/OPT/CFD, G9 environments {SIM, PAPER} declared. G3-G7 in
  the baseline admit only the class for which they are structurally bounded by the existing caps
  (long cash STK/ETF: no margin, no borrowing) and record `RISK_BASELINE_CASH_ONLY`. See Q1.
* A `[risk]` table must be complete; leverage needs, separately, `borrowing.permitted = true` with
  a limit, or `eligibility.futures = true` AND a family `funding = "FUTURES_MARGIN"` with a fresh
  margin figure, AND every numeric limit. No single key enables anything.

## 6. Failure modes considered

Multiplier defaulting to 1 on a future (refuse unless stated and confirmed); a stop synthesized
or converted silently (never; REFERENCE_TRIGGER rule above); stale owner margin figure; IBKR's
delayed account values (cross-check only, max() numerator); shadow gating on the real paper
account (forbidden); broker-only or unprotected position (unbounded portfolio risk); partial risk
table (refuse start); FX (refuse); a risk failure blocking an exit (exits exempt); resend after an
unknown outcome (re-evaluated in `_entry_checks`); an undeclared environment (refuse); the global
collar silently becoming a family rule (under a risk table each family needs its own `entry_collar`;
the global collar stays as an outer bound and never fills the family value).

## 7. Tests and the off-equals-main experiment

* Per gate: passes at the limit, fails just past it, reports exactly its own code when alone.
* All nine failing at once: nine codes, any order of construction; a passing gate never hides a
  failing one; the gateway subset never yields a coordinator pass.
* Config: each field missing / zero / negative / NaN / inf / malformed, partial table, unknown key,
  `LIVE` in environments, any `funded_authorization` value: start refused.
* Data: stale margin figure, stale or missing account tag, undeclared environment, missing stop,
  missing or mismatched multiplier, currency mismatch, broker-only position: each refuses.
* Family isolation: a second family with no stop or price rule refuses under a risk table; TLT's
  5% and 0.3%/0.6% never appear in another family's decision.
* Exits: REDUCE/CLOSE/tightening MODIFY pass with every gate failing.
* Read-only adapter: `ALLOWED_OUT_MESSAGES` and `ALLOWED_CLIENT_METHODS` equal their main values.
* Off-equals-main: run the shipped mock scenarios (`mock_multi_asset`, `run_mock_lifecycle`), the
  paper-readiness scenarios and the shadow call trace on main and on the branch with no `[risk]`
  table; decisions, reason codes, intents, orders, fills and the shadow call trace must be
  identical, `RISK_GATES` records excepted (asserted all PASS / baseline). Any expected
  difference (short stock, Q2) is listed in advance by scenario id.

## 8. Files and size (estimate)

New `src/tradingbot/risk/{__init__,policy,gates}.py` (~550). Additive: `models.py` (RiskReport,
AccountSnapshot, ~60), `validator.py` (~50), `execution/coordinator.py` (~150),
`execution/broker.py` (protocol, ~40), `execution/sim_broker.py` (simulated account, ~100),
`execution/shadow_broker.py` (~15), `ibkr/session.py` (tags, ~5),
`ibkr_orders/broker.py` (`environment() -> PAPER`, ~10; order module), `runtime/mapping.py` (family
`funding`, `risk`, `margin`, `entry_collar`, ~120), `runtime/config.py` (`[risk]`, ~80),
`runtime/service.py` (~30), docs and commented examples. Tests ~1,000 lines. Roughly 1,200
production lines; order-path change: independent safety review.

## 9. Open questions for the owner (each with a recommendation)

1. **No risk table: cash baseline or strict?** Strict (G3-G7 always need values) means TLT holds
   until you set values, so "off" is not main. *Recommend the cash baseline*: it admits only long
   cash ETF/stock, which is what you have approved, and adding a `[risk]` table switches every
   numeric gate on together.
2. **Short stock sales count as borrowing (G1), refused by default?** This changes the mock
   scenarios that short a stock (TLT is long-only; unaffected). *Recommend yes.*
3. **Dollar risk per idea: per family or one account figure?** *Recommend per family, required*
   (you asked to see capital needs per family first); G5 stays one account figure.
4. **Allocated equity versus net liquidation.** *Recommend min(stated, net liquidation)*, both
   recorded; it never sizes up on a larger account and shrinks automatically after losses.
5. **Extend the read-only account summary by `ExcessLiquidity`, `InitMarginReq`,
   `MaintMarginReq`** (same allow-listed message), and use the larger of your per-contract
   figure and the broker's? *Recommend yes to both.*
