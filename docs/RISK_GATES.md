# Independent risk gates

`tradingbot.risk` adds nine independent, configurable risk gates to the order path. They grant no
paper or funded trading authority. Leveraged execution is off by default: with no `[risk]`
table, only a long cash stock or ETF can be traded, as before. No number here is a proposal: every
limit is a field the owner fills, and the shipped configuration and examples carry none.

## The gates

Every OPEN and ADD (anything that creates or increases exposure) is put to all nine gates. Each
is a pure function of its inputs (`tradingbot.risk.gates`) with its own reason codes. No gate reads
another gate's result, and nothing skips a gate. REDUCE, CLOSE and protective amendments never
pass through them: refusing an exit would increase risk.

| # | Gate | Owner's fields | Codes |
|---|---|---|---|
| G1 | Margin borrowing | `[risk.borrowing]` `permitted`, `max_borrowed` (required if permitted), `short_sale_permitted` | `RISK_BORROWING_NOT_PERMITTED`, `RISK_BORROWING_LIMIT`, `RISK_SHORT_SALE_NOT_PERMITTED` |
| G2 | Futures, options, CFD eligibility | `[risk.eligibility]` `futures`, `options`, `cfd`; family `funding` (`CASH`, `MARGIN_BORROWING`, `FUTURES_MARGIN`, `OPTION_PREMIUM`, `CFD_MARGIN`), which must fit `execution.sec_type` | `RISK_INSTRUMENT_NOT_ELIGIBLE:<sec_type>`, `RISK_FAMILY_FUNDING_MISSING`, `RISK_FAMILY_FUNDING_INCONSISTENT` |
| G3 | Gross notional over allocated equity | `allocated_equity`, `max_gross_notional_ratio` | `RISK_GROSS_NOTIONAL_LIMIT` |
| G4 | Dollar risk per idea | family `risk.max_risk_per_idea` | `RISK_PER_IDEA_LIMIT`, `RISK_STOP_UNKNOWN`, `RISK_MULTIPLIER_UNKNOWN` |
| G5 | Aggregate portfolio risk | `max_portfolio_risk` | `RISK_PORTFOLIO_LIMIT`, `RISK_PORTFOLIO_UNBOUNDED` |
| G6 | Margin utilisation (performance margin) | `max_margin_utilisation` (fraction of equity); family `margin = { initial, maintenance, currency, verified_at, max_age_days }` (per unit) | `RISK_MARGIN_UTILISATION_LIMIT`, `RISK_MARGIN_FIGURE_MISSING`, `RISK_MARGIN_FIGURE_STALE` |
| G7 | Minimum excess-liquidity buffer | `min_excess_liquidity` (`"<USD>"` or `"<pct>%"` of effective equity) | `RISK_EXCESS_LIQUIDITY_BUFFER` |
| G8 | Family stop and price rules | family `risk.max_stop_distance` (`"N%"`) and `entry_collar` (`"<price>"` or `"<pct>%"`) | `RISK_FAMILY_STOP_RULE_MISSING`, `RISK_FAMILY_PRICE_RULE_MISSING`, `RISK_FAMILY_STOP_TOO_FAR`, `RISK_FAMILY_PRICE_OUTSIDE_COLLAR` |
| G9 | Paper versus funded | `environments` (a subset of `SIM`, `PAPER`; `LIVE` is not a value) | `RISK_ENVIRONMENT_NOT_AUTHORIZED:<SIM\|PAPER\|LIVE\|UNDECLARED>` |

Common codes: `RISK_INPUT_MISSING:<gate>:<input>`, `RISK_INPUT_STALE:<gate>:ACCOUNT_VALUES`,
`RISK_CURRENCY_MISMATCH` (no FX conversion exists). A refusal reads `RISK_GATES_FAILED` followed
by every failing gate's codes in gate order.

The table also states `approved_by`, `approved_at`, `currency` (USD) and
`account_values_max_age_seconds`. It is complete or refused at start (`ConfigError`, field paths
only): an unknown key, a missing field, zero, a negative, NaN, infinite or malformed value, or a
partial table refuses. `funded_authorization` is refused: nothing in this build can reach a
funded account. The family fields are validated when the mapping loads (funding against the
security type, margin only for margin funding, a malformed collar or stop rule). The family stop
rule `risk.max_stop_distance` is the risk gate's own. It is separate from the family-level
`max_stop_distance` of the entry rule, which has a 10% ceiling and turns on the entry rule's
strict text checks. When a family states both, both apply. Neither is ever filled from another
family or from the global `[policy]` collar.

## What each gate measures

* **Dollar risk** of an idea = |entry - stop| x multiplier x units, in account currency. Entry is
  the limit actually sent. Stop is the publisher's stop as read, in the execution series. A
  REFERENCE_TRIGGER idea is measured to its execution-priced backstop. With no backstop it is
  measured in the reference series, from the stated entry (or anchor) to the stop, at the
  contract multiplier, taking one reference point as one execution point. The multiplier is the resolved contract's, and it must equal the
  multiplier the family entry states (a non-share family that does not state one refuses). An
  ADD counts the idea's existing units at their average price. A stop already beyond the entry in
  the favourable direction counts as 0. This is the distance to the stop, not the maximum loss:
  a gap or slippage can exceed it.
* **Portfolio risk** = the same quantity over every open position, every working entry and the
  proposal. A broker-only position, a position whose protection is not CONFIRMED, or one with no
  stop is unbounded (`RISK_PORTFOLIO_UNBOUNDED`).
* **Gross notional** = |quantity| x mark x multiplier over ledger and broker positions, working
  entries and the proposal. **Economic leverage** = gross notional / effective equity.
* **Effective equity** = min(`allocated_equity`, broker net liquidation). Both are recorded. If
  net liquidation is unreadable or stale, the gates that need it refuse.
* **Borrowing** (G1, securities only) = max(0, purchase cost + cash committed by working buy
  entries - total cash). A short stock sale borrows the security and is G1's too. Futures
  initial margin is not borrowing.
* **Margin utilisation** (G6) = (initial margin of open and working futures and CFDs, plus the
  proposal's) / effective equity. The account part is the larger of the owner's per-unit figures
  and the broker's reported initial margin, so a broker figure can only tighten it.
* **Excess liquidity after the order** (G7) = broker excess liquidity - the proposal's initial
  margin (a margin-funded order) or its cost (a cash purchase), compared with the buffer.

Every evaluation is journalled as `RISK_GATES`, numbers and codes only. The record carries each
gate's status, value and limit, plus economic leverage, gross notional, borrowing, performance
margin, dollar risk, portfolio risk, allocated equity, net liquidation and effective equity. It
is written whether or not a gate fails.

## Where they run

1. **Gateway** (`validator.gate_action`): after an OPEN or ADD in a mapped family is otherwise
   approved, it runs the gates whose inputs the gateway holds: G1's structural part (side, funding), G2, G4 (new units;
   the ledger's average price is the execution core's), G6's margin figure, and G8. Gates that
   need the account, the portfolio or the broker's declaration (G3, G5, G7, G9) report
   `NOT_EVALUATED`, which is never a pass. Any failure is a HOLD with every code. The gateway's
   unmapped stock path is not gated there. It serves the fictional Sprint 1 journal and paper
   replay, which never reach a broker, and it decides as on main. Any of its entries that reach
   a broker are gated by the execution core.
2. **Execution core** (`ExecutionCoordinator._entry_checks`, every entry, add and resend): all nine
   run with every input, and the limit actually sent. This is the deciding check. Its failures
   are added to any cap refusal of the same check.

## Data, sources and staleness

| Input | Mock | Shadow | Paper experiments |
|---|---|---|---|
| Positions, working entries, stops | ledger + SimBroker | ledger + SimBroker | not combinable with a table (below) |
| Account values | the simulated account: starting cash = `allocated_equity`, cash moved by fills, positions at their sim quotes, margin = the owner's per-unit figures | the same simulated account; the real paper account is never read for it, because its values say nothing about simulated positions | the paper broker offers none |
| Environment | SimBroker `SIM` | ShadowBroker `SIM` | the paper broker declares none |

Staleness: account values older than `account_values_max_age_seconds`, or more than 5 s in the
future, refuse. A margin figure older than `verified_at + max_age_days` refuses. A missing value
refuses and never counts as zero; a future with no owner margin figure has unknown margin.

The read-only adapter's account summary also requests `ExcessLiquidity`, `InitMarginReq` and
`MaintMarginReq`. This is only more tags on the same allow-listed `REQ_ACCOUNT_SUMMARY` message.
`ALLOWED_OUT_MESSAGES` and `ALLOWED_CLIENT_METHODS` are unchanged and pinned by a test. The
broker's own margin for a prospective order would need a what-if order, which is never sent.
IBKR refreshes these values every few minutes, so they serve as a cross-check only. No runtime
path gates on them today, since shadow gates on the simulated account.

## Disabled by default: the cash baseline

With no `[risk]` table (the shipped configuration):

* G1 refuses a short stock sale and a `MARGIN_BORROWING` family.
* G2 refuses anything but a stock or ETF.
* G9 is today's rule: an anchored entry needs a broker that declares SIM or PAPER, and a broker
  declaring LIVE is refused for every entry. An undeclared broker may still take an explicit
  entry, as on main.
* G3 to G8 record `RISK_BASELINE_CASH_ONLY`. The only exposure the baseline admits is a long
  cash stock or ETF, which the existing caps and family rules bound as before.
* The simulated account keeps no values. The portfolio is not built and no extra quote is read,
  so the shadow call trace is unchanged.

A `[risk]` table switches every numeric gate on together. Leverage then still needs its own keys:
`borrowing.permitted` with a limit, or `eligibility.futures` plus a family with
`FUTURES_MARGIN` funding and a fresh margin figure, plus every limit. No single key enables it.

**The paper experiments** (`tradingbot.ibkr_orders`, unchanged by this change) declare no
environment and offer no account values. Under a `[risk]` table every entry there would be
refused (G9 `UNDECLARED`, and the account gates). A table cannot be combined with them until a
separately reviewed change.

## Behaviour that differs from main

* **Short stock sales sent to a broker are refused by default** (G1). Under a `[risk]` table, an
  unmapped stock is gated with the terms of the family named by its symbol, or refused when
  none exists. The fictional Sprint 1 journal and paper replay (no broker) are unchanged.
* **Futures, options and CFDs are refused** without a `[risk]` table (G2). On main they needed only
  an approved mapping entry. The multi-asset mock scenarios carry an invented table
  (`runtime/mock_multi_asset.INVENTED_RISK_TABLE`, deliberately wide) and decide exactly as before.
  Run with `risk=False`, each is refused with `RISK_INSTRUMENT_NOT_ELIGIBLE`.
* **A broker declaring LIVE** is refused for an explicit entry too (on main only anchored entries
  were).
* Under a `[risk]` table, a broker-only position (or one whose protection is not confirmed)
  makes portfolio risk unbounded and refuses new exposure until it is settled.
* New journal rows `RISK_GATES` appear for every entry and add that reaches the last check. The
  journal's autoincrement counter moves with them.
* The execution core reads the broker's environment declaration once more per entry (G9).
* The read-only account summary (used by the `ibkr.check` tool and the paper-readiness checks;
  no runtime path) requests three more tags.

Test fixtures changed for this, each with an INVENTED table (`tests/_risk_fixture.py`), no
expectation relaxed:

* the gateway's futures terms (`test_multi_asset_gateway.terms`);
* the anchored-entry service mapping;
* the futures contracts harness;
* the short-sale tests (coordinator short entry, publisher reversal, sell limit, two cover-cap
  short mirrors; one of these sets the gates aside because its old broker-only buy would make
  portfolio risk unbounded);
* the fake TWS (three invented account values).

Two paper-only tests now expect a refusal. The LIVE broker's explicit entry is refused, and a
second PAPER answer covers the extra G9 read before the last-step check.

## Proofs

* `tests/test_risk_gates.py`: each gate's boundary (passes at the limit, fails just past it,
  reports only its own code), all nine failing at once, the baseline, config refusals, staleness,
  family isolation (TLT's 5% and 0.3% / 0.6% never appear in another family's decision), the
  what-if hook.
* `tests/test_risk_gates_wiring.py`: the coordinator (baseline unchanged and recorded, short
  refused, LIVE refused, exits exempt with every gate failing), the gateway, the simulated
  account, shadow reading nothing real, config, the policy digest, the shipped configs, the pinned
  allow-lists, and the multi-asset scenarios with and without the table.
* Off-equals-main: the shipped cash TLT path is identical on main and on this branch with no
  `[risk]` table. That covers the mock service demo, the mock lifecycle and the shadow call trace
  (every SQLite row and log line compared, `RISK_GATES` rows excepted). Each multi-asset scenario
  decides identically to main with its invented table, and is refused by G2 without it.

## Reporting hook

`tradingbot.risk.what_if(risk_table, family, *, side, entry_reference, stop, units, touch=None,
account=None, portfolio=(), environment="SIM", now=None) -> RiskReport` returns the nine results
for a hypothetical table and one typed proposal. It is pure: no broker, no store, no network.
With no `account`, it assumes a flat account at the table's allocated equity.
