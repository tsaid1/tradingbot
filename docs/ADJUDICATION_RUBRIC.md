# Adjudication rubric (version 2.3)

<!-- Rendered from RUBRIC in src/tradingbot/runtime/adjudication_rubric.py by scripts/adjudication_rubric_docs.py. Do not edit by hand. -->

The authoritative definition of every field a reviewer answers in the adjudication workspace ([ADJUDICATION_WORKSPACE.md](ADJUDICATION_WORKSPACE.md)). Every example is invented.

## Answer states

| Stored value | Meaning |
| --- | --- |
| `""` | UNANSWERED: not answered yet; Save is refused while a required field is unanswered |
| `NOT_STATED` | the publisher did not state it |
| `CANNOT_DETERMINE` | it probably exists, but the evidence shown does not settle it; needs an ambiguity or a flag |
| `NOT_APPLICABLE` | the field does not apply to this action or label; set by the form, never typed |

## A. What kind of statement

### `component`: Does this form record an action of the post?

Asked only when the post has more than one form. Actions are recorded in the order they first appear in the post: the first on form 1, the second on form 2. A surplus form answers NO_SEPARATE_ACTION and nothing else except confidence and notes.

Applies: INSTRUCTION forms of a post with more than one form; set to ACTION_RECORDED_HERE by the form when the post has one form.

- `ACTION_RECORDED_HERE`: this form records one action of the post
- `NO_SEPARATE_ACTION`: the post has fewer actions than forms; this form is surplus

Invented examples:

- A post with two forms and one action: form 2 is NO_SEPARATE_ACTION.

### `timing`: When does THIS action happen relative to the post?

Answered per action. The post's own time words decide, not the time that has passed. CURRENT: the post presents the action as happening now or just done: present tense, "now", "just", "here", "a few minutes ago", or a plain past with no time placed on it ("Bought gold at 2,395."). HISTORICAL: the post itself places the action at an earlier time than the post ("this morning", "earlier today", "overnight", "yesterday", "last week"), or the action was done by an existing order (a stop-out, a target hit). A plain past with no time word is CURRENT: "Bought gold at 2,395." is CURRENT. Neither: UNCERTAIN with TIMING_UNCLEAR. "Bought gold at 2,395 a few minutes ago." is CURRENT; "We bought gold this morning at 2,395." is HISTORICAL (a first report: a position to reconcile, not an instruction). UNCERTAIN needs an action (no action is COMMENTARY). Timing says nothing about how directly the action is worded (that is 'explicit'). A post-level label never sets it.

Applies: every instruction form that records an action.

- `CURRENT`: the post presents the action as happening now or just done
- `HISTORICAL`: the post places the action earlier than the post, or an existing order did it
- `CONDITIONAL`: it will happen only if or when something stated happens later
- `COMMENTARY`: no action (the form sets action NONE)
- `UNCERTAIN`: the evidence does not settle it (add TIMING_UNCLEAR); needs an action

Invented examples:

- "Bought gold at 2,395 a few minutes ago." CURRENT
- "We bought gold this morning at 2,395." HISTORICAL, FIRST_REPORT: a position to reconcile, not an instruction
- "Bought gold at 2,395." CURRENT (a plain past with no time word)
- "Bought the S&P at 5,012 a few minutes ago." CURRENT (a fresh fill)
- "We were stopped out overnight at 2.84." HISTORICAL
- "If silver closes above 31.50 we go long." CONDITIONAL
- "Still long, stop unchanged at 70." COMMENTARY

### `post_label`: Label of this action (instruction form) / of the whole post (label-only form)

On an instruction form it follows from the timing and is shown read-only: CURRENT gives ACTIONABLE, HISTORICAL gives RETROSPECTIVE, CONDITIONAL gives CONDITIONAL, COMMENTARY gives COMMENTARY, UNCERTAIN gives UNRESOLVED. A label-only form labels a whole post rather than one action (a post the system did not read as an instruction): there you answer the label of the WHOLE post, the strongest label among its actions, in this order: ACTIONABLE, CONDITIONAL, RETROSPECTIVE, UNRESOLVED, COMMENTARY. List the post's actions as further actions (required for ACTIONABLE, CONDITIONAL and RETROSPECTIVE, optional for UNRESOLVED); the label must equal the strongest label of the actions you list.

Applies: every form; answered on label-only forms only.

Set by the form: on instruction forms, from the timing.

- `ACTIONABLE`: an action announced as the publisher acts (timing CURRENT)
- `CONDITIONAL`: an intention that depends on something later
- `RETROSPECTIVE`: a report of an action completed earlier or by an existing order
- `COMMENTARY`: no trading action: analysis, levels, views, updates that change nothing
- `UNRESOLVED`: the evidence does not settle it (add an ambiguity)

### `announcement`: Is this post the FIRST to tell subscribers of this action?

Asked when the timing is HISTORICAL. FIRST_REPORT: this post is the first to tell subscribers: the publisher's position changes with it (the effect is answered and cannot be NONE). REPEAT: an earlier post shown already announced or reflected it (a recap); the form then sets the effect to NONE. Not compared with the system.

Applies: timing HISTORICAL.

Set by the form: FIRST_REPORT for CURRENT; CANNOT_DETERMINE for UNCERTAIN; NOT_APPLICABLE for CONDITIONAL and COMMENTARY.

- `FIRST_REPORT`: the first report: the position changes with it
- `REPEAT`: an earlier post shown already announced or reflected it
- `CANNOT_DETERMINE`: the posts shown do not settle it

Invented examples:

- "Recap: last week we closed crude at 74.90" after the close was announced: REPEAT

### `follower_implication`: What this means for a follower

This describes the publisher's message. It is not an order and authorizes none. Derived, read-only. With explicit NO the line adds "inferred from context: not counted as executable".

Applies: every instruction form that records an action.

Set by the form: from timing, announcement and action.

- `INSTRUCTION_CANDIDATE`: timing CURRENT
- `RECONCILE_EXIT`: HISTORICAL, FIRST_REPORT, CLOSE or REDUCE: the publisher is already out (or smaller)
- `RECONCILE_POSITION`: HISTORICAL, FIRST_REPORT, OPEN, ADD or MODIFY: the position or protection changed without a real-time announcement
- `NO_NEW_INFORMATION`: HISTORICAL, REPEAT
- `WAIT_FOR_CONDITION`: CONDITIONAL
- `NONE`: COMMENTARY
- `CANNOT_DETERMINE`: timing UNCERTAIN or announcement CANNOT_DETERMINE

### `action`: What does the publisher announce?

Taken from the publisher's WORDS. With CONDITIONAL and HISTORICAL timing the action is the one that would be, or was, done.

Applies: every instruction form that records an action.

- `OPEN`: starts a position (going long, initiating, new short, bought)
- `ADD`: increases an existing position on the same side
- `MODIFY`: changes the stop or a target without changing the size
- `REDUCE`: closes part of a position (trimming, half off)
- `CLOSE`: exits the whole position, or reports that it was closed
- `NONE`: no trading action (set by the form for timing COMMENTARY)
- `CANNOT_DETERMINE`: only with timing UNCERTAIN; notes required

Invented examples:

- "Trimming half here." REDUCE
- "Raising the stop to 70." MODIFY

### `explicit`: Do the post's own words state the action?

YES when the post's words state the action, in any wording or tense. NO when the reviewer infers it from context. Test: could a reader who sees only this post and its chart, knowing the publisher's usual vocabulary, tell that the publisher does or did this action? It has nothing to do with whether a price is given. An action with explicit NO is not counted as executable ground truth.

Applies: OPEN, ADD, MODIFY, REDUCE, CLOSE, CANNOT_DETERMINE.

- `YES`: the words state the action ("long", "out", "stopped")
- `NO`: inferred from context, not stated

Invented examples:

- "Closing this here." YES (the market may come from context)
- "Nothing left on in crude after today." NO

## B. Market and instrument (source facts)

### `family`: Which market does the action concern?

One of the markets listed in the form, OTHER (a market outside the list) or UNKNOWN (cannot determine). From this post, or from the earlier posts shown when this post does not name it.

Applies: OPEN, ADD, MODIFY, REDUCE, CLOSE.

- `<family>`: a market of the form's list
- `OTHER`: a market outside the list
- `UNKNOWN`: cannot determine

### `instrument_as_read`: How does this post name the instrument?

The publisher's own words for the instrument in THIS post, as short as possible. Never a ticker you supply.

Applies: OPEN, ADD, MODIFY, REDUCE, CLOSE.

- `<text>`: the post's own words, at most 80 characters
- `NOT_STATED`: the publisher did not state it
- `CANNOT_DETERMINE`: it probably exists, but the evidence shown does not settle it (add an ambiguity or a flag)

Invented examples:

- "Dec gold"
- "S&P futures"
- NOT_STATED when the post names none

### `quoted_series`: In which kind of price series are this action's numbers quoted?

According to the post, its chart labels or the earlier posts shown. The size of a number is not evidence. A source fact, not a trading decision.

Applies: OPEN, ADD, MODIFY, REDUCE, CLOSE.

- `FUTURES`: a futures contract
- `SPOT_OR_INDEX`: a spot price or an index
- `ETF_OR_FUND`: an exchange-traded fund or other fund
- `STOCK`: a share
- `OPTION`: an option premium
- `OTHER`: another kind of series
- `NOT_STATED`: no evidence, or no numbers
- `CANNOT_DETERMINE`: conflicting evidence

Invented examples:

- "Long December natural gas at 3.42" FUTURES

### `traded_instrument`: What does the publisher SAY they traded?

A chart of futures prices is not a statement that futures were traded: without words saying what was bought or sold, the answer is NOT_STATED. What TradingBot would trade is a separate policy decision.

Applies: OPEN, ADD, MODIFY, REDUCE, CLOSE.

- `FUTURES`: futures
- `ETF_OR_FUND`: a fund
- `STOCK`: shares
- `OPTIONS`: options
- `CFD_OR_SPOT`: a CFD or the spot market
- `OTHER`: another instrument
- `NOT_STATED`: the post does not say
- `CANNOT_DETERMINE`: conflicting statements

Invented examples:

- "Bought the 5,000/5,100 call spread" OPTIONS

## C. Direction, position BEFORE this action, effect

### `side`: What is the direction of the POSITION the action concerns?

Not the direction of the order: closing a long is a sale and its side is LONG; covering a short has side SHORT. From this post or the earlier posts shown.

Applies: OPEN, ADD, MODIFY, REDUCE, CLOSE.

- `LONG`: a long position
- `SHORT`: a short position
- `CANNOT_DETERMINE`: the evidence does not settle it

Invented examples:

- "Covered the gold short." SHORT

### `position_state`: Immediately BEFORE this action, was a position open? (FLAT only when the posts establish that none existed)

BEFORE this action: as established by the publisher's own account, the earlier posts shown AND the earlier actions of this same post, in text order (this post may restate it: "our long from 70, stop was 68"). FLAT: they establish that NO position exists before this action (it was closed, or this action begins the idea). CANNOT_DETERMINE: the posts shown do not settle it; "the posts shown do not show a position" is CANNOT_DETERMINE with the flag MISSING_CONTEXT, never FLAT. For a FIRST_REPORT of an exit the position before is still OPEN (the earlier posts did not know of the exit). For a REPEAT (a recap of an action an earlier post already announced) the position BEFORE this action is the position as the earlier posts shown already leave it, that is AFTER the earlier announcement (a recap of a close announced last week: FLAT). The position AFTER this action is shown separately.

Applies: OPEN, ADD, MODIFY, REDUCE, CLOSE.

- `OPEN`: a position was open in this market before this action (for a FIRST_REPORT of an exit: still OPEN)
- `FLAT`: the publisher's own account or the earlier posts shown establish that NO position exists before this action (it was closed, or this action begins the idea)
- `CANNOT_DETERMINE`: the posts shown do not settle it; "the posts shown do not show a position" is this, with the flag MISSING_CONTEXT, never FLAT

Invented examples:

- Form 2 of "Closed the crude long ... we are now short crude": FLAT (after form 1's close)

### `position_side`: BEFORE this action, on which side was that position?

BEFORE this action (earlier posts shown and earlier actions of this post; for a REPEAT, as the earlier posts shown already leave it). NOT_APPLICABLE when the position before this action was FLAT (set by the form).

Applies: when the position before this action was OPEN or CANNOT_DETERMINE.

- `LONG`: long
- `SHORT`: short
- `CANNOT_DETERMINE`: the posts shown do not settle it

### `position_last_stop`: BEFORE this action, what was the last stop communicated?

The most recent stop the publisher had communicated for that position BEFORE this action (earlier posts shown and earlier actions of this post; for a REPEAT, as the earlier posts shown already leave it; this post may restate it: "our 72 stop was never touched"). 'stop_price' is the stop this post communicates; this is the stop that was in force before this action.

Applies: when the position before this action was OPEN or CANNOT_DETERMINE.

- `<number>`: the stop
- `NOT_STATED`: open, but no stop communicated in the posts shown
- `CANNOT_DETERMINE`: the posts shown do not settle it

### `effect`: How does this action change the position BEFORE this action?

The action comes from the words; the effect from the position. For CONDITIONAL: the effect the action would have. Allowed pairs: OPEN with OPENS or INCREASES; ADD with INCREASES or OPENS; MODIFY with AMENDS_PROTECTION (set by the form; NONE from FLAT); REDUCE with REDUCES or CLOSES; CLOSE with CLOSES or REDUCES; CANNOT_DETERMINE with any action. NONE is never chosen: the form sets it only for announcement REPEAT (the position before already reflects it), for timing COMMENTARY, and for a MODIFY from FLAT (a change to an order that is not yet a position: "moving our buy limit from 70 to 69.50"); a first report changes the position. Except for a CONDITIONAL action, the effect must be possible from the position before: from FLAT it cannot be INCREASES, REDUCES, CLOSES or AMENDS_PROTECTION; from a position open on the same side it cannot be OPENS. If the earlier posts shown do not show the position, the position before is CANNOT_DETERMINE with the flag MISSING_CONTEXT, not FLAT.

Applies: OPEN, ADD, MODIFY, REDUCE, CLOSE.

Set by the form: NONE for announcement REPEAT, for action NONE and for a MODIFY from FLAT; AMENDS_PROTECTION for any other MODIFY.

- `OPENS`: flat to a position
- `INCREASES`: a larger position
- `REDUCES`: smaller, still open
- `CLOSES`: to flat
- `AMENDS_PROTECTION`: size unchanged; stop or target changed
- `NONE`: no change: set by the form only, for a REPEAT, for COMMENTARY and for a MODIFY from FLAT (never chosen)
- `CANNOT_DETERMINE`: context missing (flag MISSING_CONTEXT)

Invented examples:

- "Adding to our long" when the posts shown do not show the position: position CANNOT_DETERMINE with MISSING_CONTEXT, effect INCREASES; ADD with OPENS only when the posts shown establish that the position had been closed
- "Moving our buy limit from 70 to 69.50" with nothing filled yet: MODIFY from FLAT, effect NONE (set by the form), warning ACTS_ON_FLAT

## D. Entry (OPEN and ADD only)

### `entry_price`: Which ONE number does the post tie to the entry of this action?

Whatever kind of number it is; 'entry_role' says which kind. It is NOT by itself an order price. When several numbers relate to the entry, take the first that exists in this order: the instructed price, the reported fill, the market observation, the average. This order and the role order of the price table answer different questions: this one, which number is the entry reference; the table's, what kind of number a row is. Never copy a number from a chart or an earlier post into it. A level that must be confirmed first ("if it CLOSES above 31.50") is not the entry price.

Applies: OPEN, ADD.

- `<number>`: the entry number
- `NOT_STATED`: the publisher did not state it
- `CANNOT_DETERMINE`: it probably exists, but the evidence shown does not settle it (add an ambiguity or a flag)

Invented examples:

- "We are buying natural gas now." NOT_STATED
- "Will buy silver at 30.80 on a pullback." 30.80

### `entry_role`: What kind of number is the entry price?

Ask of the number: is the publisher telling subscribers a price to transact at (INTENDED_ENTRY)? Reporting the price they themselves got (HISTORICAL_FILL)? Saying where the market is (CURRENT_MARKET)? Giving the average of several entries (AVERAGE_OR_COST_BASIS)? Pointing at a level on the chart (CHART_LEVEL)? Setting a stop or a target (STOP, TARGET)? Cue words are indications only, never rules: "bought at", "filled", "long from" often report a fill; "buy at", "limit", "bids", "the order is working" often give an order price; "trading at", "now at", "here at" often describe the market. Context decides: order words, tense, what surrounds the number, the chart, the earlier posts shown, how the post speaks of its other numbers. If two readings remain: UNRESOLVED with AMBIGUOUS_PRICE_SEMANTICS, never a guess. A freshly reported completed fill ("bought at 5,012 a few minutes ago") can open a position (timing CURRENT) while its price stays HISTORICAL_FILL: a fact about the publisher's execution, never a limit instruction. Only INTENDED_ENTRY is a price instruction.

Applies: OPEN, ADD, with an entry number or an entry price CANNOT_DETERMINE (the kind of entry can be known when the level is not: "bids in at yesterday's low" is INTENDED_ENTRY with LEVEL_BY_REFERENCE).

Set by the form: NOT_STATED when the entry price is NOT_STATED.

- `INTENDED_ENTRY`: a price the publisher tells subscribers to transact at (only this role is a price instruction)
- `HISTORICAL_FILL`: the price the publisher says they themselves got
- `CURRENT_MARKET`: where the market is as the publisher acts or writes
- `AVERAGE_OR_COST_BASIS`: the average price of the position after several entries
- `UNRESOLVED`: the context leaves two readings (flag AMBIGUOUS_PRICE_SEMANTICS); there is no default role
- `NOT_STATED`: set by the form when the entry price is NOT_STATED

Invented examples:

- "Going long crude at 71.20." and nothing else: UNRESOLVED
- "Going long crude at 71.20, the order is working." INTENDED_ENTRY
- "Going long crude at 71.20, filled on the open." HISTORICAL_FILL

### `entry_exactness`: How does the publisher express the entry number?

How the number is expressed, never how close it is to the market and never your confidence. A round number without a hedge is EXACT.

Applies: OPEN, ADD, with an entry number.

Set by the form: NOT_APPLICABLE without a number (NOT_STATED or CANNOT_DETERMINE).

- `EXACT`: a precise figure with no hedge
- `APPROXIMATE`: hedged (around, about, near, ~), an end of a range, or shorthand
- `NOT_APPLICABLE`: set by the form without a number

## E. Stop and targets (OPEN, ADD, MODIFY, REDUCE)

### `stop_price`: Which stop does THIS post communicate for the position after this action?

New, amended or restated ("stop stays 69.50"). NOT_STATED when this post communicates none: never carry one over from elsewhere. CANNOT_DETERMINE when a stop is referred to without a readable level (add LEVEL_BY_REFERENCE). For a MODIFY of the stop it is set by the form from 'amendment_to'.

Applies: OPEN, ADD, MODIFY, REDUCE.

Set by the form: for MODIFY of the STOP: amendment_to.

- `<number>`: the stop
- `NOT_STATED`: the publisher did not state it
- `CANNOT_DETERMINE`: it probably exists, but the evidence shown does not settle it (add an ambiguity or a flag)

Invented examples:

- "Stop goes below yesterday's low." CANNOT_DETERMINE

### `targets`: Which profit targets does this post communicate?

In the order stated, separated by commas. On a MODIFY of a target: the targets in force after the change (they must contain the new value when it is a number).

Applies: OPEN, ADD, MODIFY, REDUCE.

- `<numbers>`: the targets, comma-separated
- `NOT_STATED`: the publisher did not state it
- `CANNOT_DETERMINE`: it probably exists, but the evidence shown does not settle it (add an ambiguity or a flag)

## F. Amendment (MODIFY only)

### `amendment_field`: What does the MODIFY change?

A closed choice (version 1 asked for a typed code).

Applies: MODIFY.

- `STOP`: the stop
- `TARGET`: a profit target
- `OTHER`: something else

### `amendment_to`: What is the new value?

For STOP or TARGET a number, or CANNOT_DETERMINE. "Stop to breakeven": the number only when the earlier posts shown state the publisher's own entry as a fill or cost basis, with the ambiguity LEVEL_BY_REFERENCE; otherwise CANNOT_DETERMINE with that ambiguity. For OTHER, a short text.

Applies: MODIFY.

- `<number or text>`: the new value
- `CANNOT_DETERMINE`: no readable value (add an ambiguity)

### `amendment_from`: Which value does it replace?

For STOP it is 'position_last_stop', set by the form. For TARGET or OTHER: as stated in this post or the earlier posts shown.

Applies: MODIFY.

Set by the form: for STOP: position_last_stop.

- `<number or text>`: the replaced value
- `NOT_STATED`: the publisher did not state it
- `CANNOT_DETERMINE`: it probably exists, but the evidence shown does not settle it (add an ambiguity or a flag)

## G. Idea

### `idea`: Which idea does this action belong to?

An idea is one position from its opening to its final close. The other listed ideas are named by their identifier (SEQUENCE:<id>).

Applies: OPEN, ADD, MODIFY, REDUCE, CLOSE.

- `NEW_IDEA`: this action starts one (normally an OPEN from flat)
- `SAME_AS_PREVIOUS`: it continues the idea of the earlier posts shown
- `SEQUENCE:<id>`: it belongs to another listed idea; choose it only when sure
- `UNKNOWN`: cannot tell

## T. The price table

### `role`: What is this number?

One row per number of the post, in text order (FULL access), or, in restricted access (no post text), one row per level the SYSTEM typed. Every occurrence is its own row. The FIRST role that applies, top to bottom. The price at which a trade was actually executed is HISTORICAL_FILL even when it was the stop or the target ("stopped out at 2.84", "took profits at 75"). A level the post names AS the stop or the target, without giving it as the price obtained, is STOP or TARGET ("the 69.50 stop was hit, filled 69.40": 69.50 STOP, 69.40 HISTORICAL_FILL). "Stopped out at 2.84" gives the price obtained (HISTORICAL_FILL); "our 69.50 stop was hit" names the stop level (STOP). Context still decides. The function wins over the description: a level used AS the stop is STOP; "stop below support at 70" leaves 70 a CHART_LEVEL and stop_price CANNOT_DETERMINE. A STOP row needs the stop in stop_price (a number or CANNOT_DETERMINE), position_last_stop or amendment_from. This role order and the order in which 'entry_price' is chosen answer different questions: this one, what kind of number a row is; that one, which number is the entry reference. Ask of the number: is the publisher telling subscribers a price to transact at (INTENDED_ENTRY)? Reporting the price they themselves got (HISTORICAL_FILL)? Saying where the market is (CURRENT_MARKET)? Giving the average of several entries (AVERAGE_OR_COST_BASIS)? Pointing at a level on the chart (CHART_LEVEL)? Setting a stop or a target (STOP, TARGET)? Cue words are indications only, never rules: "bought at", "filled", "long from" often report a fill; "buy at", "limit", "bids", "the order is working" often give an order price; "trading at", "now at", "here at" often describe the market. Context decides: order words, tense, what surrounds the number, the chart, the earlier posts shown, how the post speaks of its other numbers. If two readings remain: UNRESOLVED with AMBIGUOUS_PRICE_SEMANTICS, never a guess.

Applies: OPEN, ADD, MODIFY, REDUCE, CLOSE.

- `NOT_A_PRICE`: not a price of any market: an indicator reading, a percentage, a date, a size, a profit, the period of a moving average
- `OTHER_ACTION_OR_MARKET`: a price of another market, or of another action than this form records
- `HISTORICAL_FILL`: a price at which a trade was actually executed, even at the stop or the target ("closed at 74.90", "stopped out at 2.84")
- `STOP`: a level named AS a stop of this position, not as the price obtained: new, amended, restated, the one in force or the one replaced
- `TARGET`: a level named AS a profit target of this position, not as the price obtained: new, restated, reached or the one replaced
- `INTENDED_ENTRY`: a price the publisher instructs or plans to transact at
- `CURRENT_MARKET`: where the market is ("closing here, market 2,395")
- `AVERAGE_OR_COST_BASIS`: the average price of the position
- `OTHER_CONTRACT_MONTH`: a price of another contract month, given only as a reference
- `CHART_LEVEL`: support, resistance, a prior high or low, a moving-average value, a trigger level
- `UNRESOLVED`: a price of this market whose role cannot be settled (flag AMBIGUOUS_PRICE_SEMANTICS)

Invented examples:

- "Crude bounced from 70 last week. Our stop goes to 70." row 1 of 2 CHART_LEVEL, row 2 of 2 STOP
- "We were stopped out overnight at 2.84." 2.84 HISTORICAL_FILL
- "Stopped out: the 69.50 stop was hit, filled 69.40." 69.50 STOP, 69.40 HISTORICAL_FILL
- "RSI at 31" NOT_A_PRICE

### `exactness`: How is this number expressed?

How the publisher expresses it, never how close it is to the market.

Applies: every row with a price role.

- `EXACT`: a precise figure with no hedge (a round number without a hedge is EXACT)
- `APPROXIMATE`: hedged (around, about, near, roughly, area, zone, ~), an end of a range, or rounded shorthand (4.5k)
- `NOT_APPLICABLE`: set by the form for NOT_A_PRICE and OTHER_ACTION_OR_MARKET

### `contract_month`: Which contract month is this number quoted for?

Asked only when quoted_series is FUTURES or CANNOT_DETERMINE. Assign a month only on evidence available at the time of the post: never from the post's date, from the contracts listed today or from the price level. A stop, target or entry merely expressed in another month keeps its own role and says so here.

Applies: price rows, when quoted_series is FUTURES or CANNOT_DETERMINE.

- `FRONT`: front, nearest or spot month
- `NEXT`: next or second month
- `NAMED`: a named month or contract code (type it in month_named)
- `NOT_STATED`: nothing in the material identifies the month
- `NOT_APPLICABLE`: set by the form when not asked

### `month_named`: Which month or code is named?

At most 12 letters and digits, such as DEC or Z6.

Applies: rows with contract_month NAMED.

- `<code>`: the month or contract code as named

## H. The system's mapping (asked only after the reveal; advisory)

### `reference_series_right`: Is the system's reference series the price series this post's numbers are quoted in?

The system's mapping is its proposal of which price series and which instrument would follow this idea; the reference series is the price series it reads the numbers against. A check of a source fact. Not part of the independent reading.

Applies: after the reveal, when the system's mapping names a reference series.

- `YES`: yes
- `NO`: no
- `UNSURE`: cannot tell

### `execution_instrument_right`: Would the instrument the system PROPOSES be a faithful way to follow this idea?

The system's mapping is its proposal of which instrument would follow this idea. Your opinion on a policy proposal. It approves nothing: approving a mapping is the owner's separate decision.

Applies: after the reveal, when the system's mapping proposes an instrument.

- `YES`: yes
- `NO`: no
- `UNSURE`: cannot tell

## I. Ambiguities, flags, confidence, notes

### `ambiguities`: What in the PUBLISHER'S message is unclear?

A closed multiple choice; empty means none. Your own record: shown beside the system's codes after the reveal, with no verdict.

Applies: every form.

- `ACTION_UNCLEAR`: which action
- `SIDE_UNCLEAR`: which direction
- `MARKET_UNCLEAR`: which market
- `INSTRUMENT_UNCLEAR`: which instrument
- `TIMING_UNCLEAR`: announcement, report or plan
- `POSITION_UNCLEAR`: which position or idea
- `PRICE_ROLE_UNCLEAR`: what a number is
- `SEVERAL_CANDIDATE_LEVELS`: several numbers could be the level
- `LEVEL_BY_REFERENCE`: a level given by reference: breakeven, entry, "the lows"
- `STOP_NOT_A_SIMPLE_LEVEL`: a stop on a close, a mental stop
- `SIZE_UNCLEAR`: how much
- `CONTRACT_MONTH_UNCLEAR`: which contract month

### `additional_actions`: Which further actions does the post contain that no form covers?

At most 5 compact records {timing, action, family, side}; details go in notes. Offered on the LAST form of a post, and on a label-only form whose label is ACTIONABLE, CONDITIONAL, RETROSPECTIVE (there at least one record is required, unless FORM_CANNOT_EXPRESS is set) or UNRESOLVED (optional; a record may have timing UNCERTAIN). On a label-only form the post label must equal the strongest label of the records, in the order ACTIONABLE, CONDITIONAL, RETROSPECTIVE, UNRESOLVED, COMMENTARY. Never merge two actions into one form.

Applies: the last form of a post; label-only forms labelled ACTIONABLE, CONDITIONAL, RETROSPECTIVE or UNRESOLVED.

- `{timing, action, family, side}`: one record per action; timing CURRENT, HISTORICAL, CONDITIONAL or UNCERTAIN

Invented examples:

- "Sold half our silver at 32.10." on a label-only form: ACTIONABLE plus {CURRENT, REDUCE, SILVER, LONG}

### `flags`: What does the REVIEW need?

Flags are about the review, ambiguities about the message. FORM_CANNOT_EXPRESS lifts only the rule that every field be answered: leave unanswered what cannot be answered truthfully; every check between the answers you give still applies.

Applies: every form.

- `MISSING_CONTEXT`: an earlier post needed to answer is not among those shown
- `AMBIGUOUS_PRICE_SEMANTICS`: a number's role cannot be settled (role UNRESOLVED)
- `NEEDS_MORE_EVIDENCE`: cannot be settled from the material at all; the approver should look
- `CHART_NEEDED`: the answer depends on a chart that is missing, unreadable or not shown in this mode
- `FORM_CANNOT_EXPRESS`: the form has no truthful way to record this case; notes required; excluded from scoring and listed for the owner

### `confidence`: How confident are you in this whole form?

Prefer CANNOT_DETERMINE and a flag to a low-confidence guess.

Applies: every form (required).

- `HIGH`: another knowledgeable reviewer with the same evidence would give the same answers
- `MEDIUM`: one or two fields are debatable
- `LOW`: substantial doubt

### `notes`: Notes

Free text. Refer to numbers by their row; do not paste the post.

Applies: every form; required with FORM_CANNOT_EXPRESS and with action CANNOT_DETERMINE.

- `<text>`: at most 2000 characters, stored as one plain line

## Which fields apply

| Field group | OPEN | ADD | MODIFY | REDUCE | CLOSE | NONE (commentary) |
| --- | --- | --- | --- | --- | --- | --- |
| A label, action, explicit | yes | yes | yes | yes | yes | label only |
| B market and instrument | yes | yes | yes | yes | yes | no |
| C side, position before, effect | yes | yes | yes | yes | yes | no |
| D entry | yes | yes | no | no | no | no |
| E stop, targets | yes | yes | yes | yes | no | no |
| F amendment | no | no | yes | no | no | no |
| G idea | yes | yes | yes | yes | yes | no |
| price table | yes | yes | yes | yes | yes | no |
| H mapping verdicts (after reveal) | yes | yes | yes | yes | yes | no |
| I ambiguities, flags, confidence, notes | yes | yes | yes | yes | yes | yes |

A field that does not apply is not shown (or greyed with the reason) and is stored `NOT_APPLICABLE` (lists: `[]`). With action NONE the price table is not asked. With action CANNOT_DETERMINE groups B to G and the table are not asked and notes are required. CONDITIONAL and RETROSPECTIVE use the row of their action.

## What Save refuses (blocking)

- `REQUIRED_UNANSWERED`: every applicable field must be answered
- `TIMING_ACTION`: timing COMMENTARY needs action NONE; CURRENT, HISTORICAL and CONDITIONAL need a real action; UNCERTAIN needs a real action or CANNOT_DETERMINE; CANNOT_DETERMINE as action only with UNCERTAIN
- `POST_LABEL_ACTIONS`: on a label-only form the post label must equal the strongest label of the listed actions
- `ACTION_EFFECT`: the action and the effect are not an allowed pair (effect NONE is set by the form only, for a REPEAT, for COMMENTARY and for a MODIFY from FLAT)
- `EFFECT_POSITION`: the effect is impossible from the position before this action: from FLAT not INCREASES, REDUCES, CLOSES or AMENDS_PROTECTION; from a position open on the same side not OPENS (not checked for a CONDITIONAL action). If the earlier posts shown do not show the position, answer CANNOT_DETERMINE with the flag MISSING_CONTEXT, not FLAT.
- `SIDE_POSITION`: for ADD, MODIFY, REDUCE and CLOSE, side and position_side must be equal
- `ENTRY_FIELDS`: an entry number needs an entry role and EXACT or APPROXIMATE; an entry price CANNOT_DETERMINE needs an entry role; NOT_STATED needs neither
- `TABLE_STOP`: a row with role STOP needs the stop in one of three fields: stop_price (a number or CANNOT_DETERMINE), position_last_stop (a number) or amendment_from (a number)
- `TABLE_TARGET`: a row with role TARGET needs targets (numbers or CANNOT_DETERMINE), or the action CLOSE or REDUCE, or a MODIFY of a target with a number in amendment_from or amendment_to
- `TABLE_ROW`: a price role needs an exactness and, when asked, a contract month; NAMED needs month_named
- `AMENDMENT`: MODIFY needs amendment_field and amendment_to (a number or CANNOT_DETERMINE for STOP and TARGET); on a MODIFY of a target with a number, targets must contain it
- `CANNOT_DETERMINE_NEEDS_REASON`: a CANNOT_DETERMINE, UNCERTAIN or UNRESOLVED answer needs at least one ambiguity or flag
- `CANNOT_DETERMINE_NEEDS_NOTES`: action CANNOT_DETERMINE needs notes
- `ESCALATION_NEEDS_NOTES`: FORM_CANNOT_EXPRESS needs notes
- `ADDITIONAL_ACTION_REQUIRED`: a label-only form labelled ACTIONABLE, CONDITIONAL or RETROSPECTIVE needs at least one additional action
- `CONFIDENCE`: confidence is required
- `NOT_APPLICABLE_FIELD`: a field that does not apply, or that waits on an unanswered answer, carries a value; or NOT_APPLICABLE was given for a field that applies
- `DERIVED_VALUE`: a value the form derives (the action's label, the announcement for CURRENT, the follower implication, component) was given differently

With the flag FORM_CANNOT_EXPRESS (notes required) only `REQUIRED_UNANSWERED` is lifted: leave unanswered what cannot be answered truthfully. Every check between the answers given still applies, the derived values are still checked, and a field waiting on an unanswered answer may hold only `""` or `NOT_APPLICABLE`.

## Warnings (saving needs the tick "I have checked these"; stored as `acknowledged_warnings`)

- `ENTRY_NOT_IN_TABLE`: an entry number is given but no row carries its role
- `STOP_NOT_IN_TABLE`: a stop number is given but no row is STOP
- `TARGET_NOT_IN_TABLE`: target numbers are given but no row is TARGET
- `REPEAT_POSITION`: a REPEAT whose position before does not already reflect the action (a repeat OPEN, ADD, REDUCE or MODIFY with FLAT before; a repeat CLOSE with OPEN before; a repeat OPEN or ADD with a position open on the other side): for a REPEAT the position before is the one after the earlier announcement
- `ENTRY_ROLE_ORDER`: a row carries an entry kind that ranks before entry_role
- `STOP_SIDE`: a long's stop above its entry, or a short's below
- `ACTS_ON_FLAT`: ADD, REDUCE or CLOSE on a FLAT position with an effect other than NONE (for example ADD with OPENS), or a MODIFY on a FLAT position other than a REPEAT (a change to an order that is not yet a position: effect NONE)
- `OPEN_ON_OPEN`: OPEN with a position already open on the other side (a reversal is two actions)
- `NEW_IDEA_NOT_OPEN`: NEW_IDEA with an action other than OPEN
- `POSITION_CHAIN`: the position before this action differs from the position after your reading of the previous action of this post in this market

`ENTRY_NOT_IN_TABLE`, `STOP_NOT_IN_TABLE` and `TARGET_NOT_IN_TABLE` are not raised when the reading carries the ambiguity `LEVEL_BY_REFERENCE` or the flag `CHART_NEEDED` (the number is not in the text by nature).

## Verdicts after the reveal

Each compared field carries a verdict on the SYSTEM's value, never on yours:

- `CONFIRM`: the system's value is right
- `CORRECT`: the system's value is wrong, mine is right
- `UNSURE`: you cannot tell

A field `NOT_APPLICABLE` in your reading gets `NOT_APPLICABLE`. A price row the system typed no level for gets `NOT_READ` (not read by the system: it made no claim there, so there is nothing to judge). Not compared: `component`, `announcement`, `follower_implication`, `quoted_series`, `traded_instrument`, `ambiguities`, `additional_actions`, `reference_series_right`, `execution_instrument_right`. The system's empty or NONE equals your NOT_STATED or NOT_APPLICABLE; your CANNOT_DETERMINE pre-fills UNSURE. Nothing of the system's reading ever fills one of your answers.

## The approver's decision

- `ACCEPT`: the reviewer's reading is right as it stands
- `AMEND`: some fields are wrong; I change only those
- `RETURN`: send it back to the reviewer

An amendment re-derives what follows from it; every field it does not amend whose kind (asked, derived, not applicable) changes, or that was derived and is now asked, is reset to unanswered and must be answered by the approver. Amending `action`, `timing` or `announcement` also resets `effect` when it is asked and not amended. A field the amendment reset (or the approver blanked) must be answered even when the reviewer's record carries FORM_CANNOT_EXPRESS: the escalation lifts `REQUIRED_UNANSWERED` only for the other fields.
