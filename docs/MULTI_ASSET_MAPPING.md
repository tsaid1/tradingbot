# Multi-asset mapping

The publisher names a market and quotes levels. Those levels usually belong to a **reference
series**: an index, a spot price, a futures month. The bot can only trade an **execution
instrument**: a future, an ETF, an option. The two prices differ by a basis that moves. The mapping
policy (`runtime/mapping.py`, data in `config/instrument_mapping.toml`, version 2) says, for each
source asset family, how the two relate. The one gateway (`validator.gate_action`) then decides
every action in the series each price belongs to. The rules are simple. A reference level is never
sent as an execution price. Nothing is substituted silently. Anything unresolved HOLDs.

## The model

Each `[[mapping]]` entry is one **family**: the asset code the interpreter emits, or
`<UNDERLYING>.OPT` for single-leg options on that underlying. An instruction that OPENS a position
and names options ("call", "put", "strike", "option", a compact `105C`, or a word that only names
options such as "straddle") only ever looks up the `.OPT` family; without complete terms it HOLDs,
and it never falls back to buying the stock. A follow-up (ADD, REDUCE, CLOSE, MODIFY) binds to its
idea: it uses the family recorded on the idea when it was opened (or the option family if the idea
holds an option), never the words in the follow-up, so "call it a day and close TLT" closes the
ETF. An entry states:

| Field | Meaning |
|---|---|
| `basis` | exactly one of `DIRECT`, `BASIS_SHIFT`, `REFERENCE_TRIGGER`, `UNSUPPORTED` |
| `approved`, `approved_by`, `approved_at` | `false` by default. An approval is the TOML boolean `true` (not `"yes"`, `1` or `"true"`) with a non-blank `approved_by` and an `approved_at` date-time with its UTC offset, not in the future |
| `same_series`, `same_series_reason`, `equivalent_roots` | DIRECT only: the reference and the execution future are one price series. Legal only between two futures in one currency, with one root or the two roots listed in `equivalent_roots` (a micro and its standard contract), and `contract_selection = EXPLICIT_ONLY`. Needs the reason |
| `reference` | the series the levels are quoted in: `sec_type` (`STK`, `FUT`, `OPT`, `CMDTY`, `CFD`, `IND`), `symbol`, `exchange`, `currency` |
| `execution` | the instrument traded: the same fields plus the expected `multiplier`, `min_tick` and `size_increment`. The broker's resolved contract must agree, or the action is held (`CONTRACT_SPEC_MISMATCH`) |
| `contract_selection`, `contracts` | futures only. `EXPLICIT_ONLY` (the instruction names the month) or `EXPLICIT_OR_NEAREST` (a named month, otherwise the nearest listed month with at least `min_business_days_to_roll` business days before its configured `roll_date`) |
| `sizing` | `units_per_signal` (contracts or shares per OPEN or ADD), `max_units` per idea, and `max_notional` per idea (price x units x multiplier, USD) |
| `session` | `REGULAR_ONLY` or `EXTENDED`, with a timezone, days and `HH:MM-HH:MM` windows |
| `roll` | `business_days_before` the roll date, and `action = "FLATTEN"` |
| `basis_shift` | `max_abs_basis`, `max_age_seconds` and `scale_ratio` (for BASIS_SHIFT, or for a backstop). `scale_ratio` must be declared and must be exactly `1`: any other ratio is UNSUPPORTED |
| `reference_trigger` | `released_order_type = "MKT"`, the `reference_hours` when the reference updates, and `require_backstop` |

A family with no entry is marked `UNSUPPORTED_INSTRUMENT_MAPPING`. An entry that is not approved is
marked `MAPPING_NOT_APPROVED`. An approved entry that cannot select exactly one contract gets
`CONTRACT_SELECTION_AMBIGUOUS`, `CONTRACT_MONTH_UNRECOGNISED` (below), `OPTION_TERMS_INCOMPLETE`
(underlying, expiry as `YYYY-MM-DD`, strike and right must all be stated),
`OPTION_STRATEGY_NOT_SINGLE_LEG` (below) or `IDEA_CONTRACT_MISMATCH` (a follow-up that names a
different month than its idea's). Each of these marks makes the gateway HOLD, and the action never
reaches the broker.

**Contract months.** A month is read in these forms: a month name, full or abbreviated, with a
four- or two-digit year, with or without an apostrophe (`Sep 2026`, `Sept '26`, `Sep26`); a
numeric year and month (`2026-09`, `2026/09`, `09/2026`, `202609`); a futures month code with a
one- or two-digit year, with or without the contract's root (`QIFU6`, `U26`); a month name followed
by "contract" (`the Sep contract`). If the clause holds anything that looks like a month or a
contract code and it does not resolve to exactly one listed contract of the entry (a bare `Sep`, a
month or year that is not listed, a code with another root, `2026-13`), the action HOLDs with
`CONTRACT_MONTH_UNRECOGNISED`. The default is HOLD, not the nearest month: for a futures family,
after removing the recognised month forms and the numbers the interpreter already assigned to the
action (its entry, stop and target, and a reduction percentage), any token still left that carries
a digit (a percentage, a date, a time, a count, a second price, `09/26`, `U 26`), any month name, or
any token shaped like a contract code for the family's roots in any case (`qifu6`, `QIFU`) HOLDs
with `CONTRACT_MONTH_UNRECOGNISED`. `EXPLICIT_OR_NEAREST` uses the nearest month only for a clause
with no such leftover token at all. A follow-up on an open idea uses the idea's own contract. "May" and "march" count as months only in one of the forms
above. A month name with a two-digit number is read as a year: "Dec 18" is December 2018.

**Single-leg options only.** An option instruction executes only with exactly one underlying, one
expiry, one strike and one right. More than one strike (`100/105`, `105 and 110 calls`, two
strike-right pairs), more than one expiry, more than one right, or a strategy word (spread,
vertical, calendar, diagonal, straddle, strangle, condor, butterfly, collar, ratio, covered, combo,
iron fly, risk reversal) HOLDs with `OPTION_STRATEGY_NOT_SINGLE_LEG`. Single leg is the exception:
an option OPEN or ADD executes only if the WHOLE post, not just the action's clause, states exactly
one strike, one expiry and one right, with no strategy word and no selling, shorting, writing or
"against" language ("sell the 110 against it" in a later sentence HOLDs the whole post). An exit or
amendment on an open option idea is bound to that idea's contract.

**What approval governs.** Approval governs new exposure only. OPEN and ADD on an unapproved or
removed family HOLD (`MAPPING_NOT_APPROVED` or `UNSUPPORTED_INSTRUMENT_MAPPING`). Each idea records
the entry it was opened under (`exec_positions.mapping_terms`). If its family later loses approval
or its entry, REDUCE, CLOSE and protective amendments on that idea resolve from the recorded terms,
marked exits-only, so the position can always be closed; the gateway still refuses OPEN and ADD on
them and still holds a widened stop. Such a position shows as `OPEN_UNDER_REVOKED_MAPPING` in the
coverage report, the positions table and the Execution page.

At decision time the gateway also HOLDs for `OUTSIDE_PERMITTED_SESSION` (entries and exits; stop
amendments are allowed) and for `CONTRACT_IN_ROLL_WINDOW` (new exposure only; exits are allowed).

Loading fails closed. The service refuses to start on any of these: an unknown key, a missing
required field, two entries for one family, an approved entry without an approver, a non-USD
execution instrument marked approved (no FX conversion exists), DIRECT with a reference other than
the execution series or a stock family that is not its own ticker (unless `same_series = true`
with a reason), an index, spot or CFD reference with a stock or ETF execution under DIRECT,
BASIS_SHIFT or a backstop (their price scales differ), BASIS_SHIFT rules without
`scale_ratio = 1`, a selection rule that would pick a contract already in its roll window, or an
option that is not DIRECT. A stock or ETF entry quoted in its own price, with no sizing and no
session, keeps the version-1 behaviour exactly: policy sizing, the gateway's stock path, a plain
quote. Version-1 files still load.

## The three treatments and their risks

**DIRECT.** The levels are the execution instrument's own prices: the named futures month, the
ETF, the option premium. The gateway checks entry, stop and target against the execution quote,
and requires them on its tick. Levels the interpreter marked as belonging to another series
(`SOURCE_SERIES`) are refused, as on the stock path (`SOURCE_TO_BROKER_PRICE_BASIS_UNRESOLVED`),
unless the entry declares `same_series = true`. That declaration asserts that the futures contract
month the publisher quotes and the execution future's price are one and the same series (a micro
and its standard contract, same month), so its levels are execution prices and get every check
above in that one series. It may join only two futures in one currency with one root or an
`equivalent_roots` pair, selected `EXPLICIT_ONLY`; anything involving an index, a spot commodity,
a CFD, a stock or an ETF refuses to load (a stock or ETF quoted in its own price needs no flag). It is part of what the owner
approves, together with its stated reason, and the contract-month rules then apply in full: a level
is used only in the month the instruction names or the selection rule picks.
*Risk:* the publisher's "front month" may not be the month the
broker lets you hold near expiry, so name the month or use `EXPLICIT_ONLY`.

**BASIS_SHIFT.** At each decision (an entry, an add, an amendment), the basis is computed as the
execution midpoint minus the reference midpoint. Both quotes must be LIVE, each no older than
`max_age_seconds`, and taken within `max_age_seconds` of each other (the reference at its own
update time; see reference freshness below). The basis must be within
`max_abs_basis`. Otherwise the action HOLDs: `BASIS_QUOTE_UNUSABLE` with the leg and reason, or
`BASIS_EXCEEDS_CAP`. Levels are shifted by the basis and recorded on the intent. A shifted stop is
rounded to the tick on the tighter side and a shifted target to the tick nearer the entry, so
neither is loosened. The basis is additive only, so a ratio relationship (an ETF against an index)
cannot be a BASIS_SHIFT entry. *Risks:* the basis moves after entry, so the stop protects the
converted level, not the publisher's. A publisher stop that tightens in reference terms can widen
in execution terms when the basis has moved; the working stop is then kept (see protective levels
below).

**REFERENCE_TRIGGER.** The entry is a limit order priced from the execution instrument's own
quote. The publisher's entry level is checked only against the reference quote (the collar). The
stop and target stay in reference levels. They are placed in the bracket as conditional market
exits in the execution contract, each released when the reference's last price crosses its level
(a long's stop BELOW, its target ABOVE). With `require_backstop`, a stop order in the execution
contract, priced as BASIS_SHIFT, works beside them in the same one-cancels-other group.
Protection counts as CONFIRMED only when the broker shows that conditional exit working: right
contract, reference contract, direction, trigger level, side and quantity, plus the backstop when
it is required. Reconciliation checks the same things. An amendment moves the trigger
(`modify_condition`) and re-prices the backstop. A broker that does not report
`supports_price_conditions()` is never sent such a bracket (`CONDITIONAL_ORDERS_UNSUPPORTED_BY_BROKER`).

**What a reference trigger cannot do.** A condition fires only when its reference updates. An
index that publishes only during its cash session cannot trigger overnight, at weekends or on its
holidays, even though the future keeps trading. For those hours the backstop is the only
protection, and the two stops can disagree by however much the basis moves. Entries outside
`reference_hours` are held (`REFERENCE_SERIES_NOT_UPDATING`). A released market exit fills at
whatever the execution market offers, with no limit. A GTC condition that did not execute on its
trigger day must trigger again on a later day.

**UNSUPPORTED** always HOLDs. It documents why a family is not traded.

## Protective levels are never loosened

An amendment in which the publisher's own stop loosens follows the gateway's rule for widened stops,
unchanged: it HOLDs (`STOP_WIDENING_REQUIRES_POLICY_REVIEW`). An amendment that tightens the
publisher's stop may still convert, after the basis moved, to a broker-side level that is looser
than the one working: a BASIS_SHIFT stop, or a REFERENCE_TRIGGER backstop. For every treatment the
tighter of the working and the newly computed level is kept. The plan records each kept level
(`protective_levels_kept`, on the intent), the execution core raises a
`PROTECTIVE_LEVEL_NOT_LOOSENED` exception, and protection is confirmed against what is actually
kept. The ledger keeps the publisher's own stop (`publisher_stop`) so the next amendment can tell a
tightening from a loosening; a BASIS_SHIFT idea recorded before that field existed HOLDs as before.

## Reference freshness

A reference quote (BASIS_SHIFT, a REFERENCE_TRIGGER entry, amendment or add, and a backstop) is
judged by when its series last updated, never only by when the bot received it. The broker's own
update time (`BrokerQuote.updated_at`) is used when it gives one. Without it, the receipt time
counts only once the service has seen the value change, so the first sighting of a value, or a
value that never moves, is no evidence. A frozen value, an unknown update time, a crossed quote
(bid above ask), or a quote outside the series' declared `reference_hours` HOLDs every action that
needs it: `REFERENCE_QUOTE_NOT_FRESH` with `REFERENCE_QUOTE_STALE`, `_UPDATE_TIME_UNKNOWN`,
`_CROSSED` or `_OUTSIDE_HOURS` (inside `BASIS_QUOTE_UNUSABLE` for a basis). A REFERENCE_TRIGGER ADD
outside the reference hours HOLDs as an OPEN does (`REFERENCE_SERIES_NOT_UPDATING`). REDUCE and
CLOSE need no reference quote.

## Rolls

A follower position in a contract inside its roll window is flagged every cycle as
`ROLL_DUE_FLATTEN_REQUIRED`, with an alert and a notification. The service never sends an order
the gateway did not approve from a publisher action. Flattening, or rolling through a new
instruction, is therefore a human decision. Holding into a delivery window is prevented by that
alert and by the gateway's refusal of new exposure. It is not automated.

## What the owner approves per family

Approve the following for each family:

* Which reference series the publisher's levels mean.
* The execution instrument, with its multiplier, tick and size increment as the broker reports them.
* The basis treatment. For BASIS_SHIFT also the basis cap and quote age. For REFERENCE_TRIGGER
  also the reference hours and whether a backstop is required.
* The contract months and their roll dates, with the business-day margin.
* The units per signal and the caps.
* The trading session.

Then set `approved = true` (the TOML boolean) with `approved_by` and `approved_at`. The shipped file approves only the
bond ETF. Its other entries are commented templates with placeholder values.

## To verify against the real broker (not done here)

* Whether a price condition can sit on a bracket child or an OCA member, and how the parent's
  `transmit` sequence treats it. IBKR documents conditional submission for LMT, MKT and a few other
  types, not for STP.
* Which market-data subscription the condition's reference contract needs (for example an index
  feed), and whether the broker can evaluate a condition without it. Also which data the
  `reference_quote` request needs.
* Trading permissions per instrument (futures, options, CFDs, spot metals), which depend on
  residency and entity.
* The futures close-out and delivery dates per contract, to set each `roll_date`.
* `tradingHours` and `liquidHours` per contract, to set each session.
* That `reqContractDetails` returns the multiplier, `minTick` and contract month the entry expects.

Run the demonstration: `PYTHONPATH=src python scripts/run_mock_multi_asset_demo.py`.
