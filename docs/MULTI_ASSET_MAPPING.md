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
and names options ("call" or "put" as nouns, "strike", "option", a compact `105C`, or a word that
only names options such as "straddle") only ever looks up the `.OPT` family; without complete
terms it HOLDs, and it never falls back to buying the stock. Verb and idiom uses are not option
language: "put a stop at 88", "put on", "put in", "put it", "call it", "call me", "close call",
"calling" and "called" leave a stock instruction a stock instruction. A bare "calls" or "puts" with
no strike, expiry or "option" is unclear and HOLDs. A follow-up (ADD, REDUCE, CLOSE, MODIFY) binds to its
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
| `entry_rule`, `entry_anchor` | `EXPLICIT_ENTRY_ONLY` (default) or `REFERENCE_AS_COLLAR` with its required limits `max_adverse_move`, `max_favourable_move` (`"2.5"` in the anchor's own price units, or `"0.5%"` of it), `max_signal_age_seconds`, `anchor_scope`, `max_quote_age_seconds` and `environment = "PAPER"`. See "Entry rule" below |
| `level_contract_rule` | `EXPLICIT_MONTH_ONLY` (default) or `FRONT_MONTH`, for a DIRECT future whose levels are a futures series. See "Level contract rule" below |

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
with `CONTRACT_MONTH_UNRECOGNISED`. Shaped like a code means the root followed by a month code, a
month name or a digit; an ordinary word that merely starts with the root's letters ("stop" for a
root `STO`) is not one. The number in an index's own name ("S&P 500", "Nasdaq 100", "Russell 2000")
is part of the market's name and is skipped, like a recognised month. `EXPLICIT_OR_NEAREST` uses the nearest month only for a clause
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
option that is not DIRECT, or a file with a top-level `what_if` key. A `what_if` file is an offline
analysis aid (an invented mapping for a historical replay), never an owner's approval: only a caller
that passes `allow_what_if=True` may load it, `RuntimeService` accepts it only with
`allow_what_if_mapping=True` and a replay-file source, and `build_service` and the scripts never
pass either. A stock or ETF entry quoted in its own price, with no sizing and no
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

## Entry rule

By default (`entry_rule = "EXPLICIT_ENTRY_ONLY"`, the product decision of 5 November 2026) only
explicit execution language sets an entry price. A market level the publisher states when opening
("long here, the index 5,010 now") is a REFERENCE, so such an OPEN or ADD HOLDs
(`OPEN_ENTRY_PRICE_MISSING`, `ADD_ENTRY_REFERENCE_MISSING`).

**The owner's decision of 12 November 2026.** The shipped file turns the rule on for **TLT only**:
`anchor_scope = "SINGLE_MARKET_POST"`, `max_adverse_move = "0.3%"`, `max_favourable_move = "0.6%"`,
`max_signal_age_seconds = 90` (from the original publication), `max_quote_age_seconds = 3` (each side
of a live streaming quote), `environment = "PAPER"`, with the only exposure approved so far (1 share,
150 USD notional) and the US regular session (09:30 to 16:00 America/New_York, Monday to Friday). It
is for simulation, evaluation and shadow decisions on paper: it **does not authorize broker order
submission**, and the only broker on `main` is the simulated one. Every other family keeps
`EXPLICIT_ENTRY_ONLY`. The protective stop is mandatory and the order is always a limit, never a
market order.

**The owner's further decisions of 12 November 2026 (DECIDED 7), TLT only, paper and simulation
only (they do not authorize order submission):**

* `standalone_level_sentence = true`: a level may stand in its own sentence directly after the
  instruction, or after the instruction's own stop and target sentence (see *What the level is*).
* `max_stop_distance = "5%"`: a **family** rule (owner, 12 November 2026, extended the same day
  to explicit entries). It governs every OPEN and ADD of the family, explicit-entry and anchored
  alike; it is required whenever the family has the entry rule and optional otherwise (a family
  without it behaves exactly as before); strict "N%", code ceiling
  `validator.ANCHOR_MAX_STOP_DISTANCE_PCT_CEILING` = 10 %; written beside `entry_rule`, and refused
  inside `[entry_anchor]`. Only TLT sets it. It is an **outer rejection boundary**, not a stop
  placement the bot recommends, and it does **not** bound the loss actually realised in a gap or
  with slippage. The bot never synthesizes, moves or chooses a stop. An entry is refused, with
  one set of codes for both paths, when the stop is:
  - absent (`PROTECTIVE_STOP_MISSING`; for an ADD the idea's stop as it stands is the one
    checked, and an ADD to an idea without one holds);
  - on the wrong side of the entry (`STOP_DIRECTION_CONTRADICTION`);
  - not uniquely this action's (`PROTECTIVE_STOP_NOT_UNIQUE`, at the mapping stage: more than one
    value tagged STOP for the action `TWO_STOP_VALUES`; the action's own stop not among them
    `STOP_DIFFERS_FROM_ACTION`; the value stated by another trading action of the post,
    commentary aside, `STOP_SHARED_WITH_ANOTHER_ACTION`; and on the text, every number in a
    clause with a stop word anywhere in the post ("stop" in any hyphenated or qualified form,
    "stoploss", "stops", "sl", "protective"; not "stopped", which reports an exit; in a clause
    that also names a target, only the numbers between the stop word and the target word): more
    than one distinct value `SEVERAL_STOP_VALUES`, one value other than the action's stop
    `STOP_TEXT_DIFFERS`; a repeated identical value is not a second stop. So "Stop 87.20.
    Protective level 87.10." holds. "Protective stop 87.20." alone is read by the interpreter as
    commentary and attaches no stop, so such a post holds for a missing stop (the safe direction);
    the guard that the action's own protective stop still anchors lives at the mapping stage);
  - farther than the limit from the entry (`PROTECTIVE_STOP_TOO_FAR_FROM_ENTRY`: the explicit
    entry price, or an anchored entry's stated reference, in the publisher's series);
  - farther than the limit from the limit-entry price actually to be sent
    (`PROTECTIVE_STOP_TOO_FAR_FROM_LIMIT`, in the series the stop is held in): at the gateway for
    an anchored entry, and at the execution core before every submission, explicit and anchored,
    OPEN and ADD, recovery included. For an explicit entry the limit sent is the stated entry
    price itself (the collar only rejects), so the two checks coincide up to tick snapping: a stop
    4.9 % below an explicit entry trades under the default 1 % collar.
  Exactly at the limit is accepted (inclusive); farther is refused. A distance written as a stop
  ("Stop 1.20." on an 88 share price) is refused as too far.
* `long_only = true` (a family switch, default false): an OPEN or ADD on the short side HOLDs at
  the mapping stage (`MAPPING_LONG_ONLY`), explicit and anchored alike. TLT entries are limit
  orders; exits are marketable limits; the protective stop child is a stop order (it becomes a
  market order at the broker when triggered), as for every family.
* Approximate levels ("~N now") still hold.

`entry_rule = "REFERENCE_AS_COLLAR"`, approved per family with its three limits (no defaults),
changes one thing. A **current** OPEN or ADD (timing CURRENT: never a retrospective report, a
conditional, commentary or an uncertain one) that states **exactly one** market level for its
instrument and **no** explicit entry price enters with a **limit order at the marketable side of the
live execution quote** (the ask for a long, the bid for a short, snapped to the tick never past it),
never a market order. The stated level is the **anchor**. An explicit entry price or range is
decided exactly as before; the rule never touches it.

*How the stated level travels.* The interpreter classifies each price in a clause with a role
(`PriceMention`: ENTRY, STOP, TARGET, EXIT, TRIGGER, REFERENCE). The typed action carries only
ENTRY (`entry_reference`), STOP and TARGET; a REFERENCE level is not on it. The service passes each
action's classified mentions, as (role, value) pairs, to `MappingPolicy.apply(levels=...)`, beside
the clause text and post text it already passes for contract months and option legs. Only an entry with the rule on reads them
(`FamilyMapping.entry_anchor_for`). The level becomes the anchor only if all of these hold, and
otherwise the action HOLDs at the mapping stage with the reason shown:

| Condition | Otherwise |
|---|---|
| exactly one distinct REFERENCE value for the action | `ENTRY_ANCHOR_SEVERAL_LEVELS` |
| no other classified price (trigger, exit) beside the stop and target | `ENTRY_ANCHOR_SECOND_PRICE` |
| the value is a number | `ENTRY_ANCHOR_UNREADABLE` |
| no other action of the post for another instrument shares the clause or states the same value | `ENTRY_ANCHOR_INSTRUMENT_AMBIGUOUS` |
| the value is written somewhere: the action's own clause, else another clause of the post (a commentary line quoting the market), else a line of the post that is no action's clause (a level line such as "41.20 now.", which the interpreter attaches to the instruction it follows) | `ENTRY_ANCHOR_NOT_IN_POST` |
| ...and in exactly one such place | `ENTRY_ANCHOR_SOURCE_AMBIGUOUS` |
| neither the clause nor that source names another cashtag | `ENTRY_ANCHOR_INSTRUMENT_AMBIGUOUS` |
| the value is the market **now** (see *What makes a level an anchor* below) | `ENTRY_ANCHOR_NOT_CURRENT` |
| a separate level line or attached quote names only this family | `ENTRY_ANCHOR_INSTRUMENT_UNCLEAR` |
| for a future, the clause and the source pass the contract checks of the *Level contract rule*, and a month the source names is the order's month | `CONTRACT_MONTH_UNRECOGNISED` |
| the level is an explicit contemporaneous market quotation (see *What the level is* below) | `ENTRY_ANCHOR_LEVEL_NOT_MARKET_QUOTE` |
| after the contract is selected, neither the clause nor the source holds another number once months, an index's own name, the anchor, the stop, the target and a reduction percentage are read | `ENTRY_ANCHOR_UNEXPLAINED_PRICE` |

### What makes a level an anchor

Default HOLD: a number is the market's level at the time of the post only with **positive
evidence** next to it. A level without it is simply not an anchor, and the open holds as it does
with the rule off (`ENTRY_ANCHOR_NOT_CURRENT`; `OPEN_ENTRY_PRICE_MISSING` when no level was
tagged at all). The checks run on the number's **own clause**: the part of the clause or line it
is written in between punctuation (`,` `;` `.` `:` `!` `?`, a line break, a spaced dash).

1. **A present-market marker is attached to it**, from a closed public list
   (`instruction_terms.PRESENT_MARKERS`): `now`, `right now`, `currently`, `here at`, `trading at`,
   `at the market`. Attached means written directly after the number ("41.20 now") or directly
   before it ("now 41.20", "trading at 41.20", also with "at" between: "currently at 41.20").
   "We are now long $X, 41.20" has a "now", but not attached to the number: not an anchor.
2. **Its whole sentence is entirely understood** (the final rule; `ENTRY_ANCHOR_CONTEXT_UNCLEAR`).
   There is no window: the scope is the entire sentence that holds the number. Only a full stop
   (not a decimal point), an exclamation mark, a question mark, a line break or the end of the
   post ends a sentence; commas, colons, semicolons, dashes, slashes and brackets do not, and
   nothing resets the scope. Every alphabetic token in the sentence must be a present-market
   marker (public or private), a connector (`CONNECTOR_WORDS`: at, is, are, around, near, here,
   the, a, it, @), a generic function word (`FUNCTION_WORDS`: we, i, are, am, is, and, our, my, a,
   the, this, new, position, positions, trade; no preposition that could make the number a goal,
   a condition, a comparison or another thing's price, such as "for", "to", "on", "of", "with" or
   "in"), a currency or unit, a
   name or alias of this family, the action's own words (long, buy, buying or short, shorting,
   sell, selling; add, adding for an ADD; stop and target when the action has them), or a
   private `context_words` entry. One unknown word anywhere refuses the anchor ("cost basis long
   $X 41.20 now", "long $X avg cost, 41.20 now", "goal: 41.20 now"). A sentence ending in a
   question mark never supplies an anchor (`QUESTION`), nor does one holding another number than
   the action's own stop, target, reduction or contract month (`SECOND_NUMBER`). For a family
   whose anchor is compared in a reference series (BASIS_SHIFT, REFERENCE_TRIGGER) the names are
   the reference series' only: "QIF 5,025 now" for an index family is never compared with the
   index. A level sentence without the action's own words (a standalone "41.20 now.") also needs
   the sentence before it to be entirely on the allow-list (`ENTRY_ANCHOR_INSTRUMENT_UNCLEAR`,
   `PRECEDING_SENTENCE`). A second marked number elsewhere in the post that belongs to no other
   action holds (`ENTRY_ANCHOR_SEVERAL_LEVELS`). Phrasings that pass this rule and *What the
   level is* below include "We are now long $X, 41.20 now.", "I am buying $X, currently 41.20.",
   "New position: long $X, trading at 41.20.", "We are now long $X, 41.20 now, stop 40.10.",
   "Short $X, 41.20 right now, stop 42.30.", and "We are now long $X." followed by "41.20 now.".
   "We are long $X here at 41.20." passes this rule but holds there (rule A).
3. **Nothing in its own clause makes it something else** (a second, redundant check) (`NOT_CURRENT_WORDS`): a time other than
   now (yesterday, earlier, last, next, was, will, a weekday, overnight, ...), a chart, session or
   order level (high, low, open, close, settlement, resistance, support, target, objective, stop,
   level, entry, exit, ...), a condition or a level to cross (from, to, above, below, over, under,
   through, break, if, when, once, until, unless), a range or a comparison (between, range, vs,
   than, higher, lower, up, down, or, `<`, `>`). A marker never overrides them. Every place the
   number is written must pass.
4. **A separate level line or attached quote** (not the action's own clause) may hold only: the
   number and other numbers (which `ENTRY_ANCHOR_UNEXPLAINED_PRICE` then checks), the markers, the
   connector "at", a currency or unit (`UNIT_WORDS`: usd, $, points, ticks, bbl, oz, ...), and a
   name of **this** family: its code, its reference and execution symbols (with or without `$`),
   and its `aliases` (one to three words, in the mapping entry). Any other word means the bot does
   not know whose level it is (`ENTRY_ANCHOR_INSTRUMENT_UNCLEAR`): "Brent 71.45 now." is never the
   anchor of an oil family that does not list "brent". For a future, a month or contract code on
   the line is left to the contract check instead (rule 3 of the *Level contract rule*).

### What the level is

The owner's ambiguity safeguard (12 November 2026). Rules 1 to 4 decide whether a number is written
as the market now; they do not decide what the number *is*. Every word of "Our position is 71.60
now." is on the allow-list, yet 71.60 may be the position's average cost. So every candidate level is
also classified into one closed kind (`AnchorLevelKind`, recorded on the decision as
`anchor_level_kind`, beside `anchor_attribution`):

| Kind | Meaning |
|---|---|
| `MARKET_QUOTE` | an explicit contemporaneous market quotation: the only kind that may anchor |
| `ENTRY_OR_COST_BASIS` | possibly an entry price or an average cost basis |
| `STOP_OR_TARGET` | possibly a stop or a target |
| `HISTORICAL` | a historical or retrospective level |
| `AMBIGUOUS` | any other numerical reference, or one the bot cannot classify |

Any kind other than `MARKET_QUOTE` HOLDs with `ENTRY_ANCHOR_LEVEL_NOT_MARKET_QUOTE`,
`LEVEL_KIND:<kind>` and what decided it. The existing checks feed the classification: a level refused
by them records the kind their words show (a stop or target word `STOP_OR_TARGET`, a cost-basis or
holder word `ENTRY_OR_COST_BASIS`, a time word `HISTORICAL`, anything else, a question or a second
number `AMBIGUOUS`). None of them is loosened. Two tightenings apply in **every** anchor scope:

* **A. The number's own clause is a pure market quotation** (`OWN_CLAUSE`). The clause, delimited
  as in rule 3, may hold only the number, its attached marker, the connectors `at`, `is`, `are`,
  `around`, `near`, `@` ("here" only inside "here at"), a currency or unit, and a name or alias of
  this family. A holder, position, trade, side or add word there (we, I, our, my, position, trade,
  long, buy, short, sell, add, ...) makes the number possibly an entry price or a cost basis
  (`ENTRY_OR_COST_BASIS`): "Our position is 71.60 now." and "We are long $X here at 41.20." hold (a
  "long at N" statement reads as an entry price). A pronoun subject (it, this, that, they) or any
  other word makes it some other thing's level (`AMBIGUOUS`): "It is 41.20 now." holds. So do
  quotation marks of any kind in the clause and an approximation or sign mark on the number (`~`,
  `≈`, `±`, `<`, `>`, `+`, `-`): they are not interpreted.
  "We are now long $X, 41.20 now.", "I am buying $X, currently 41.20.", "New position: long $X,
  trading at 41.20." and "We are now long $X." followed by "41.20 now." still anchor, subject to B.
* **B. Understood neighbours** (final review round, 12 November 2026: paths removed, not patterns
  added). An *understood neighbour* of the level's sentence is one of: the boundary of the post;
  one of this action's own instruction sentences (a sentence of the action's own clause that names
  this family or carries the action's own side or add word); or a protective-levels sentence (one
  stating only the action's own stop and target, defined below). Nothing else is understood.
  - **R1, every anchored level, every scope, key on or off:** the sentence immediately AFTER the
    level's sentence must be an understood neighbour (a protective-levels sentence counts here
    whatever the key); otherwise HOLD (`AMBIGUOUS`, `FOLLOWING_NOT_UNDERSTOOD`). "... 41.20 now.
    Stop 40.10." passes; "... 41.20 now. That is our average cost." and "... 41.20 now. The chart
    looks constructive." hold.
  - **An elliptical level never anchors by default** (fallback after review F1, 12 November
    2026). A level written on its own ("41.20 now.", or a later clause of a sentence that is not
    the instruction) whose clause does not name this family HOLDs with `AMBIGUOUS`,
    `STANDALONE_LEVEL_NOT_ENABLED`, unless the owner sets `standalone_level_sentence = true`
    (strict boolean, default false, shipped false for every family). The reason: the instruction
    sentence can name another market in plain lower-case words ("long $X and spy", "like the long
    end"), and the level on its own line could be that market's price. A level written inside the
    instruction sentence, or naming this family, is decided as before.
  - **With `standalone_level_sentence = true`**, an elliptical level may anchor only if (a) the
    earlier clauses of its sentence, else the sentence BEFORE it, are the action's own instruction
    sentence or a protective-levels sentence (`NEIGHBOUR_NOT_UNDERSTOOD` otherwise, also when the
    level opens the post); (b) R1 holds; and (c) every one of the action's own instruction
    sentences passes the same whole-sentence allow-list a level's own sentence must pass (not a
    question, every word allow-listed, no other number), so the level is accepted only where it
    would have been accepted inside the instruction sentence (`INSTRUCTION_NOT_UNDERSTOOD`
    otherwise: "long $X and spy", "long $X, bought last week"). The old key name
    `level_after_own_protective_levels` is refused with a message naming the new one.
  - The level- and cost-word deny-list (`CONTEXT_WORD:<w>`) stays as a second check that can only
    add a hold: on the neighbour before an elliptical level and on the clauses before a level in
    the instruction sentence ("We are now long $X, stop 40.10, 41.20 now." holds).
  - Rule A and the whole-sentence checks of `SENTENCE_STRICT` and `ACTION_BLOCK` stay on top, so
    no scope is looser than `SINGLE_MARKET_POST`. A level that names this family ("$X 41.20 now.")
    needs no preceding neighbour, but R1 still applies. A level sentence that cannot be located in
    the post holds (`CONTEXT_UNKNOWN`).

* **Protective-levels sentence** (used by R1 always, and before an elliptical level only with
  `standalone_level_sentence = true`). It satisfies all of: every alphabetic token is on a small closed public list
  (`instruction_terms.PROTECTIVE_WORDS`: stop, stops, stop-loss, stoploss, protective, target,
  targets, targeting, objective, with, a, an, the, and, at, is, are, our, my, set, of, price, level,
  loss) or the private `context_words`; it holds no time, condition or cost-basis word and no month;
  it holds at least one number and every number is exactly the action's own stop or target; the stop
  is on the protective side of the level (below it for a long, above for a short) and a target on the
  profit side; it is not a question. Before an elliptical level (switch on) the attribution is then
  `AFTER_OWN_PROTECTIVE_LEVELS`. "Stop 40.10. 41.20 now." is accepted with the switch on; "We
  raised our stop.", "Stop moved to 40.10.", another value, a
  stop on the wrong side, an extra number, "today", "if needed", an unknown word or a question still
  hold, and so does such a sentence that is not the immediate neighbour. The switch lives in
  `[entry_anchor]`, so it cannot exist without the rule.

This is one classification function with a closed result (`FamilyMapping._level_kind`); there is no
scoring, no weight and no window.

*Anchor scope* (`entry_anchor.anchor_scope`, required with the rule, no default: how much textual
certainty the owner requires). All values apply rules 1 to 3 to the marked number's own sentence
unchanged; there is no windowed option (a window's start and end could be gamed inside one
sentence). They differ only in how a standalone level sentence is attributed ("41.20 now." on its own):

* `"SENTENCE_STRICT"` trusts only words: the sentence immediately before the level sentence must be
  entirely on the allow-list (`ENTRY_ANCHOR_INSTRUMENT_UNCLEAR`, `PRECEDING_SENTENCE` otherwise).
* `"ACTION_BLOCK"` also trusts the interpreter's delimitation: the level sentence is attributed
  when the sentence immediately before it is one of this action's own clause sentences
  (`Classification.clause`, the text the service passes as the action's clause), with nothing in
  between; otherwise it falls back to the SENTENCE_STRICT test. The typed action exposes only that
  clause. Which sentence a stop or target came from is not exposed (`PriceMention` carries a role
  and a value, no sentence index), so a stop or target sentence counts as part of the block only
  when it is itself entirely allow-listed ("Stop 40.10." is; "Stop below 40.10." is not). To
  attribute more, the interpreter would have to expose the sentence span of the action's block.
  Since 12 November 2026 a level sentence right after a stop or target sentence holds in every
  scope (*What the level is*, rule B): the stop sentence still attributes, but the level may be
  the stop.

* `"SINGLE_MARKET_POST"` trusts the post as a whole. The level's own sentence is checked exactly
  as above; nothing is asked of its neighbours. Instead the anchor is accepted only if all hold:
  (a) exactly one sentence of the post carries a marked number (`ENTRY_ANCHOR_POST_SEVERAL_LEVELS`);
  (b) every action of the post is this family's, no action carries an unresolved-instrument flag
  (`ASSET_UNRESOLVED`, `MIXED_SERIES_AND_TICKER`, `MULTIPLE_TICKERS_IN_CLAUSE`), and the post holds
  no cashtag but this family's and no code, series symbol or alias of any other family the mapping
  file or the private lexicon (`family_aliases`) knows (`ENTRY_ANCHOR_POST_SEVERAL_MARKETS`); (c)
  this is the post's only action besides commentary, and no action is conditional, retrospective
  or uncertain (`ENTRY_ANCHOR_POST_SEVERAL_ACTIONS`); (d) for a reference-series family the level's
  sentence does not name the execution instrument (`ENTRY_ANCHOR_REFERENCE_UNNAMED`). Every gateway
  protection is unchanged.

  **Competing actionable instruments** (part of (b); the owner's decision of 12 November 2026,
  DECIDED 7: "An unknown capitalized word alone is insufficient to establish a second market";
  "If the post may contain multiple actionable instruments ..., HOLD"). Two closed tests, one
  function (`FamilyMapping._competing_instrument`):
  - *Mention with context* (`ENTRY_ANCHOR_POST_UNRESOLVED_MARKET`, `RULE:MENTION_WITH_CONTEXT`,
    then the mention kinds): a mention the bot cannot tie to this family (the detectors below)
    holds only when its own sentence also holds an instruction, stop or target word
    (`instruction_terms.ACTION_SEMANTIC_WORDS`) or a price-like level (`price_like_level`: a number
    written as a price, with a decimal, a thousands separator or a currency sign, a number with a
    present-market marker, or a number after "at"; not a percentage, a year, a date or a count).
    Plain commentary ("Qorvex reported earnings.", "NFA.") no longer holds.
  - *Unattributed action sentence* (`ENTRY_ANCHOR_POST_UNATTRIBUTED_ACTION`,
    `RULE:UNATTRIBUTED_ACTION_SENTENCE`): any sentence outside this action's own clause sentences
    that holds an instruction word (`INSTRUCTION_WORDS`: the side words long, buy, buying, short,
    shorting, sell, selling; add, adding; reduce, reducing, trim, trimming; exit, exiting, cover,
    covering) holds, whatever it names ("We are buying spy as well.", "Also long the ten-year.").
    A past form ("bought") and "close" (a chart level for the anchor rule) are not instruction
    words here; stop and target words are not either, so the action's own stop sentence never
    fires it. Hyphenated and slashed tokens are split first ("Qorvex-long", "Long/short"). What
    this costs, pinned in tests and not fixed: "The long end is bid.", "In short, we like it.",
    "Enjoy the long weekend.", "Add to that the CPI print.", "Buy the dip, as always.", "Exit plan:
    below the stop.", "Long story short", "Do not sell in panic.", "long-term", "Long-duration".
  - Narrow additions for missed second instruments (review of PR #70): a sentence-initial unknown
    capitalised word counts as a mention when its sentence has a trading context; with a mention
    in the same sentence, past forms of the instruction words (bought, added, sold, shorted,
    trimmed, reduced, exited, covered), close / closing / closed and the acquisition verbs
    entering, accumulating, picking up, grabbing, loading count as context (alone they do not);
    and the sentence directly after an own instruction sentence that holds a mention and "too",
    "also", "as well", "same" or "likewise" holds (`RULE:ADDITIVE_ELLIPSIS`). Residual, pinned: a
    lowercase unknown market with another verb ("Get qorvex.", "Load qorvex.") is not detected; a
    sentence that opens with an ordinary capitalised word outside the understood vocabulary and
    holds a context word counts as a mention (over-blocking). The owner accepted these residuals for now (12 November 2026: "Accept
    the documented residual cases for now. Do not open a broad parser redesign to address rare
    hypothetical phrasing."). A post whose commentary the interpreter itself reads as
    another family's action still holds on the action rule (`ACTION_FOR_ANOTHER_FAMILY`).
  The mention detectors are closed public lists of generic words (`instruction_terms`), never a publisher's:
  `TICKER_SHAPED` (2 to 5 capital letters, optionally with a futures month and year, or any
  all-capitals word of six or more letters wherever it stands, that are not
  this family's names, its own contract codes, a word the anchor rule already understands written
  in capitals, such as "NOW" or "STOP", or a non-instrument acronym such as "CPI", "FOMC", "RSI");
  `MARKET_NOUN` (a generic market or asset noun: energy, metals, rates and bonds, equity indices,
  currencies, crypto, agricultural commodities, volatility, "equities", ...; any case, also as a
  hashtag) that is not a name or alias of this family (a `#WORD` is read as `WORD`, and a token
  is split on `/` and `.`, so "EUR/USD" and "BRK.B" are seen part by part); `INSTRUMENT_PHRASE` (futures, calls, puts,
  options, shares, stock, ETF, index, contract, yield, spread) without a name of this family
  directly before it ("$X shares" resolves, "the bond ETF" and "Zorb futures" do not; for a future,
  its own month or contract code also resolves); `PROPER_NOUN` (a capitalised word that is not
  sentence-initial, not this family's, not understood by the anchor rule and not on the small
  public list of weekdays, months, institutions and holidays). A publisher's non-market proper
  nouns (a person, a place) may be added privately as `entry_anchor.non_market_words` in the
  lexicon file: one word of letters each, never a disqualifying word, a market noun or an
  instrument noun; loading refuses anything else. A cashtag that is not this family's is check (b)
  as before.

The decision records how the anchor was attributed (`anchor_attribution`: `OWN_SENTENCE`,
`AFTER_OWN_INSTRUCTION`, `AFTER_ALLOW_LISTED_SENTENCE`, `FIRST_SENTENCE`, `SINGLE_MARKET_POST`) and
what the level was classified as (`anchor_level_kind`).

*Private extension.* A publisher's own present-market phrasing, and further names of a family,
are never added to the public lists. They go in the optional `entry_anchor` section of the private
lexicon file (`[interpreter] lexicon_path`, git-ignored):
`{"entry_anchor": {"present_markers": ["..."], "family_aliases": {"<FAMILY>": ["..."]},
"context_words": ["..."], "non_market_words": ["..."]}}` (context words: a publisher's plain
instruction phrasing; non-market words: see check (b)). The
interpreter never sees that section. A marker (one to four words) or an alias (one to three) is
letters only and may not contain any disqualifying word; an alias may not contain a marker word.
Loading refuses anything else. The extension can only turn a held level into an anchor where the
publisher's wording is positive evidence; it cannot override rules 2 and 3.

*What this rule cannot know.* When a post discusses several markets, a correctly marked current
level in a clean sentence may still belong to a different market than the instruction: the words
around it are all understood, but the publisher's intent is not. The protections that bound that
case are the live-quote collar (`max_adverse_move`, `max_favourable_move`, within the code
ceilings), the signal-age limit, the mandatory protective stop and the size caps (`max_units`,
`max_notional`). The interpreter must also still tag the right number REFERENCE.
Under `SINGLE_MARKET_POST` specifically, after the unresolved-reference check and the neighbour
allow-list (B): a market named only by a lower-case common word outside the public lists ("the
widget fund"), or by an unknown word at the start of a sentence ("Zorbex is heavy."), in a sentence
that is **not** the level's neighbour, in an otherwise understood post, is still invisible, so a
marked level that really belongs to it could be attributed to the post's family. (Directly before an
elliptical level, or directly after any anchored level, such a sentence holds: it is not an
understood neighbour.) This is not named-entity recognition and
there is no dictionary. The protections that bound that case are the same: the collar against the
live quote, the signal-age limit, the mandatory stop and the size caps; and the vocabulary can be
extended privately (`entry_anchor.family_aliases`). The check also fails closed the other way: a
capitalised ordinary word the rule does not understand ("We like STRONG setups"), or a family's
plain-word name that is not one of its aliases ("bonds" in a TLT post), holds the post until the
owner adds an alias or a non-market word. Classification residual: the level kind is read from
words only. A level the interpreter misread, written as a pure quotation with a marker, is still a
`MARKET_QUOTE`.

No stated level at all leaves the action as today (`OPEN_ENTRY_PRICE_MISSING`, with
`ENTRY_ANCHOR_NOT_STATED`). For a futures family the accepted anchor is the one extra level the
contract-month leftover rule counts as read; without the rule, or for any other number, that rule is
unchanged. The accepted anchor is recorded on the mapping decision (`entry_anchor`), handed to the
gateway on the `MappedQuote` (`entry_anchors`, by action id), and the gateway decides:

* the limits are within the hard ceilings below and the policy's own entry collar, as a
  percentage of the anchor (`ENTRY_ANCHOR_LIMIT_ABOVE_CEILING`);
* the source post is no older than `max_signal_age_seconds` (`ENTRY_ANCHOR_SIGNAL_TOO_OLD`), on top
  of the policy's own signal age;
* the anchor is compared **in its own series**: with the live reference quote for BASIS_SHIFT and
  REFERENCE_TRIGGER (which must be LIVE and fresh by the reference-freshness rules; it HOLDs
  otherwise), with the execution quote for DIRECT and a same-series future (whose quote checks
  already refuse a delayed, frozen, stale or absent quote);
* **live streaming quotes only, each side fresh** (owner, 12 November 2026). The execution quote,
  and the reference quote where the anchor is compared in a reference series, must be `LIVE` (a
  delayed or frozen quote is never eligible, whatever `accepted_qualities` says:
  `ENTRY_ANCHOR_QUOTE_NOT_LIVE`), with a bid and an ask both present and positive
  (`ENTRY_ANCHOR_QUOTE_SIDE_MISSING`), on the contract's tick for the execution quote
  (`ENTRY_ANCHOR_QUOTE_OFF_TICK`), the bid strictly below the ask (`ENTRY_ANCHOR_QUOTE_LOCKED_OR_CROSSED`),
  and each side's own update time (`BrokerQuote.bid_at`, `ask_at`, additive) known
  (`ENTRY_ANCHOR_QUOTE_SIDE_TIME_UNKNOWN`), no older than `max_quote_age_seconds`
  (`ENTRY_ANCHOR_QUOTE_SIDE_STALE`) and not more than 0.5 s in the future
  (`ANCHOR_QUOTE_FUTURE_TOLERANCE_SECONDS`, its own small bound; review S1)
  (`ENTRY_ANCHOR_QUOTE_SIDE_FUTURE`). The detail names the side (`SIDE:BID`, `SIDE:ASK`) and the leg
  (`LEG:EXECUTION`, `LEG:REFERENCE`). A broker that cannot report per-side times can never anchor.
  The simulated broker stamps both sides (its receipt time unless a test says otherwise); in shadow
  mode the per-side times come from the read-only adapter's held streams, converted so the gateway
  measures at least the adapter's own monotonic age (docs/RUNTIME.md, "Shadow mode"). The execution core applies the same
  rule again to the quote it re-reads just before sending the anchored limit;
* **the basis cap** (review F4): when the anchor is compared in a reference series (BASIS_SHIFT,
  REFERENCE_TRIGGER) it bounds the reference price only, so the absolute difference between the
  live execution and reference mids, from two fresh, near-simultaneous quotes, must be within the
  family's cap: `[basis_shift] max_abs_basis` (and `max_age_seconds`) where the family has one,
  otherwise `entry_anchor.max_abs_basis` and `max_basis_age_seconds` (a REFERENCE_TRIGGER without
  a backstop). It HOLDs when the cap is exceeded (`BASIS_EXCEEDS_CAP`), the basis cannot be
  computed (`BASIS_QUOTE_UNUSABLE`), or no cap reached the gateway
  (`ENTRY_ANCHOR_BASIS_CAP_MISSING`). The loader refuses the rule on such a family without a cap,
  a second cap beside `[basis_shift]`'s, and an anchor cap on a DIRECT family;
* the live side the entry trades against may be worse than the anchor (above it for a long) by at
  most `max_adverse_move` (`ENTRY_ANCHOR_ADVERSE_MOVE`: do not chase) and better by at most
  `max_favourable_move` (`ENTRY_ANCHOR_FAVOURABLE_MOVE`: the market moved so far the signal may be
  stale or misread);
* a protective stop is **always** required, whatever `require_stop_on_open` says
  (`PROTECTIVE_STOP_MISSING`); an anchored ADD needs its idea to have a stop. The service refuses to
  start with the rule on any family and `policy.require_stop_on_open = false`. Its side is checked
  against the anchor in the publisher's series, and it must not already be crossed by the live market in the series it will be held in
  (converted by the basis for BASIS_SHIFT, in reference levels for REFERENCE_TRIGGER, with the
  backstop as before);
* the policy's percentage collar between the entry level and the quote is replaced by the two
  limits above; sizing, caps, session and roll window are unchanged.

The approved plan records `entry_rule`, `entry_anchor`, `anchor_series`, `anchor_live`,
`entry_limit` and `anchor_max_quote_age_seconds`; the execution core sends exactly that limit,
after re-checking that a fresh quote still lies inside the policy collar around it and passes the
per-side rule above. An anchored entry whose first submission has an
unknown outcome is never re-sent (as for every entry). An anchored intent that was recorded but
never submitted (the process stopped in between) is recovered under its own rule (review S2): the
plan's anchored limit, the per-side quote rule and the anchor's signal age
(`anchor_max_signal_age_seconds` on the plan, `ENTRY_ANCHOR_SIGNAL_TOO_OLD`); otherwise it ends
`REFUSED` with `STALE_AFTER_UNKNOWN_OUTCOME` and the reason. A mapped plan's recovery is checked
against its execution reference, as at the first attempt. Every anchor limit the gateway and the
execution core read must be a finite positive number (nan, infinity, zero or a boolean holds with
`ENTRY_ANCHOR_RULE_INCOMPLETE`), and the loader accepts only strict numbers for them (no string,
no boolean, no nan or infinity; review N1).

*Hard ceilings.* Constants in `validator.py`, raised only by a code change and its review:

| Limit | Ceiling |
|---|---|
| `max_adverse_move` | `ANCHOR_MAX_ADVERSE_PCT_CEILING` = 1 % of the anchor |
| `max_favourable_move` | `ANCHOR_MAX_FAVOURABLE_PCT_CEILING` = 2 % of the anchor |
| `max_signal_age_seconds` | `ANCHOR_MAX_SIGNAL_AGE_CEILING_SECONDS` = 120 s |
| `max_quote_age_seconds` | `ANCHOR_MAX_QUOTE_AGE_CEILING_SECONDS` = 5 s |

The loader refuses a percentage above its ceiling, an age above 120 s, and an absolute move above
its ceiling percentage of the highest entry price the family's sizing can buy
(`max_notional / (units_per_signal x multiplier)`). The gateway refuses either move above its
ceiling, or above the policy's `max_price_deviation_pct`, as a percentage of the actual anchor.

*Paper and simulation only; impossible against the funded account.* Three independent locks:

1. The mapping file must state `environment = "PAPER"` in `[entry_anchor]`; it is required and
   "PAPER" is its only accepted value, so no value can mean a funded account.
2. The runtime refuses to start with the rule on any family in a runtime mode outside the
   allow-list `mapping.ANCHOR_RULE_MODES` (`"mock"` and `"shadow"`: both simulate every order and
   fill; shadow at read-only real quotes), so a future mode never inherits the rule silently.
3. A broker declares what it trades through an optional `environment()` (`BrokerEnvironment`: `SIM`,
   `PAPER`, `LIVE`; additive, outside the `Broker` protocol). The simulated broker declares `SIM`.
   The execution core refuses an anchored entry unless the broker positively declares `SIM` or
   `PAPER`: a `LIVE` broker, one without the declaration or one that answers anything else is
   refused with `ENTRY_ANCHOR_PAPER_ONLY` (`ENVIRONMENT:<value or UNDECLARED>`) and an open
   `ENTRY_ANCHOR_PAPER_ONLY` exception. It checks before the intent is recorded and again at the
   last step before submission (an intent refused there ends `REFUSED`, its orders `NotSubmitted`).
   A declaration that raises any exception counts as undeclared (review S3). Entries with an
   explicit price are unaffected. A future order adapter for a real paper account must derive the
   declaration from the managed account, not assert it.

*What the owner accepts.* Buying at the market some seconds after the post, with the slippage from
the publisher's level bounded by the two limits; that a misread level would be used as an anchor
(the checks above refuse a level without positive present-market evidence and every ambiguity
listed, but not a single level the interpreter misread that also carries a marker); and that the limits are in the anchor's series, so for a basis-shifted family the
execution price can differ from the anchor by the basis.

### Text that is not plain ASCII

The entry rule's lists and tokenisers were written for ASCII text, so a word holding an accented
or non-Latin letter could split into fragments or vanish ("an unknown word holds" did not apply to
it; an independent review found a second market named that way invisible). For an OPEN or ADD in
a family with an entry rule (`REFERENCE_AS_COLLAR` or a family stop distance), a SUPPORTED decision
is checked once more (`tradingbot.runtime.entry_text`); the check can only turn it into a hold:

* *A word* is a maximal run of letters of any script (`str.isalpha()`) with any combining marks
  attached. A word holding any non-ASCII character is never equal to an allow-listed, family,
  alias, marker or instruction word, look-alikes included (Greek capital taus, full-width letters,
  a combining mark): the text is normalised (NFC) and every such word is replaced by a distinct
  ASCII placeholder that no list holds, keeping its leading capital, and the decision is taken
  again on that shadow text. The stricter decision wins; the owner's review log receives the word
  as written. So "Zörbleflux sits near 12.40." is a capitalised unknown word with a trading context:
  `ENTRY_ANCHOR_POST_UNRESOLVED_MARKET`. Accented commentary with no trading context holds no
  more than its ASCII spelling would.
* *Invisible and odd characters*, fail closed: any format character (zero-width space, joiner,
  non-joiner, word joiner, soft hyphen, byte-order mark, bidirectional controls), any control
  character but tab and newline, any space but U+0020 (no-break and other exotic spaces), line and
  paragraph separators, private-use or unassigned code points anywhere in the post:
  `ENTRY_TEXT_INVISIBLE_CHARACTER`. Nothing is normalised away, so nothing can be joined into a
  word that was not there. (A post with an emoji built with a joiner holds too.)
* *Numbers*, fail closed: any digit or numeral outside ASCII 0-9 (full-width, Arabic-Indic,
  superscripts, fractions), or the Unicode minus or a non-ASCII dash next to a digit:
  `ENTRY_TEXT_NUMBER_UNREADABLE`. Such a number is never an anchor or a stop.

Pure ASCII text is decided exactly as before.

## Level contract rule

A DIRECT future whose levels are a futures series (its own, or `same_series`) is selected
`EXPLICIT_ONLY`: a level is used only in the month the instruction names. The publisher often
quotes an energy level without a month, so such an instruction HOLDs
(`CONTRACT_SELECTION_AMBIGUOUS`). `level_contract_rule = "FRONT_MONTH"` declares that a level
stated without a month refers to the **front contract** of the entry's own list: the first listed
contract whose roll date has not been reached. Execution is in that same month (or its
`equivalent_roots` micro). A month the instruction names always wins; a follow-up keeps its idea's
contract; the other same-series constraints stay.

`FRONT_MONTH` applies only when the instruction says **nothing at all** about which contract:

1. **Relative or descriptive wording holds** (`CONTRACT_MONTH_UNRECOGNISED`, `RELATIVE_CONTRACT:<word>`)
   for every futures family and every OPEN or ADD, whatever the selection rule and with or without
   an entry price or the entry rule: back, next, following, second (2nd), deferred, far, nearby,
   front, spot month, calendar, spread, roll (rolls, rolled, rolling, rollover), in the clause or on
   the level line the anchor is taken from. "the back month at 71.50" is never the front.
   (`front` holds too: the publisher's front may not be the list's.)
2. **The leftover-token rule** is unchanged: anything month-like or contract-code-like left in the
   clause after its recognised months and levels holds.
3. **The level line** an anchor comes from passes the same contract-month and leftover-token checks
   as the clause, and a month or contract code it names must resolve to the same contract the
   order uses (`LEVEL_LINE_MONTH`); "QOFQ6 71.60 now." never anchors a July order.

It also HOLDs when the front is fewer than
`min_business_days_to_roll` business days from its roll date (`FRONT_MONTH_IN_ROLL_WINDOW`: the
publisher may be quoting either month, and the next one is never assumed), and when the front
cannot be determined uniquely (`FRONT_MONTH_AMBIGUOUS`: months not listed in order, roll dates not
strictly increasing, or no listed contract before its roll date). *Risk:* near a roll the
publisher's "oil" may already be the next month while the list's front is still the old one; the
roll margin is the only protection, so set it no shorter than the period in which the market's own
front-month convention changes.

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
update time (`BrokerQuote.updated_at`) is the only evidence of freshness. When the broker supplies
no update time for a reference series, every action that needs that reference HOLDs; a value seen
to change between two decisions is not credited. A frozen value, an unknown update time, a crossed quote
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
* The entry rule. `REFERENCE_AS_COLLAR` only with its anchor scope, its adverse and favourable
  move limits and its signal age, chosen for the family within the code ceilings (1 %, 2 %, 120 s); for a
  reference-series family without `[basis_shift]`, also its basis cap; and the family's plain-word
  `aliases` a level line may name it by.
* For a DIRECT future, whether a level without a month means the front contract (`FRONT_MONTH`).

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

Once a read-only discovery report exists, `scripts/tb_mapping_proposal.py` turns it into a
contract table, a proposed roll calendar, unapproved `[[mapping]]` entries and a one-page approval
sheet per family (`MAPPING_PROPOSAL.md`).

Run the demonstration: `PYTHONPATH=src python scripts/run_mock_multi_asset_demo.py`.
