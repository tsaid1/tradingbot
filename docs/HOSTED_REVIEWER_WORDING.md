# The hosted reviewer site: the wording (generated)

Generated from `hosted/reviewer_web/wording.py` by `tests/test_hosted_wording.py`; do not edit by hand. Every option still stores the rubric's own code: the wording is display only (DECIDED 40, section 6).

The four states, worded the same way everywhere:

- not answered yet (a state, never an option): "I haven't answered yet"
- `NOT_STATED`: "The post doesn't say"
- `CANNOT_DETERMINE` (and `UNKNOWN`, `UNCERTAIN`, `UNRESOLVED`): "The evidence conflicts or doesn't settle it"
- `NOT_APPLICABLE` (set by the form, never chosen): "This question doesn't apply"

| Field | Question as shown | Option as shown | Stored value | The wording before |
|---|---|---|---|---|
| `component` | Does this form record one of the post's actions? | | | Does this form record an action of the post? |
| | | Yes, this form records one action | `ACTION_RECORDED_HERE` | ACTION_RECORDED_HERE: this form records one action of the post |
| | | No, the post has fewer actions than forms | `NO_SEPARATE_ACTION` | NO_SEPARATE_ACTION: the post has fewer actions than forms; this form is surplus |
| `timing` | When does this action happen? | | | When does THIS action happen relative to the post? |
| | | Now, or just done | `CURRENT` | CURRENT: the post presents the action as happening now or just done |
| | | Earlier, as the post itself says (or an existing order did it) | `HISTORICAL` | HISTORICAL: the post places the action earlier than the post, or an existing order did it |
| | | Later, only if something stated happens | `CONDITIONAL` | CONDITIONAL: it will happen only if or when something stated happens later |
| | | There is no action: commentary only | `COMMENTARY` | COMMENTARY: no action (the form sets action NONE) |
| | | The evidence conflicts or doesn't settle it | `UNCERTAIN` | UNCERTAIN: the evidence does not settle it (add TIMING_UNCLEAR); needs an action |
| `post_label` | What is the post, taken as a whole? (instruction form: "What kind of action is this?") | | | Label of this action (instruction form) / of the whole post (label-only form) |
| | | A trade made now | `ACTIONABLE` | ACTIONABLE: an action announced as the publisher acts (timing CURRENT) |
| | | A plan that depends on something later | `CONDITIONAL` | CONDITIONAL: an intention that depends on something later |
| | | A report of a trade made earlier | `RETROSPECTIVE` | RETROSPECTIVE: a report of an action completed earlier or by an existing order |
| | | Commentary: no trading action | `COMMENTARY` | COMMENTARY: no trading action: analysis, levels, views, updates that change nothing |
| | | The evidence conflicts or doesn't settle it | `UNRESOLVED` | UNRESOLVED: the evidence does not settle it (add an ambiguity) |
| `announcement` | Is this post the first to tell subscribers about this action? | | | Is this post the FIRST to tell subscribers of this action? |
| | | Yes, this is the first report | `FIRST_REPORT` | FIRST_REPORT: the first report: the position changes with it |
| | | No, an earlier post shown already reported it | `REPEAT` | REPEAT: an earlier post shown already announced or reflected it |
| | | The evidence conflicts or doesn't settle it | `CANNOT_DETERMINE` | CANNOT_DETERMINE: the posts shown do not settle it |
| `action` | What does the publisher say they are doing? | | | What does the publisher announce? |
| | | Opening a new position | `OPEN` | OPEN: starts a position (going long, initiating, new short, bought) |
| | | Adding to a position | `ADD` | ADD: increases an existing position on the same side |
| | | Changing only the stop or a target | `MODIFY` | MODIFY: changes the stop or a target without changing the size |
| | | Closing part of a position | `REDUCE` | REDUCE: closes part of a position (trimming, half off) |
| | | Closing the whole position | `CLOSE` | CLOSE: exits the whole position, or reports that it was closed |
| | | No trading action | `NONE` | NONE: no trading action (set by the form for timing COMMENTARY) |
| | | The evidence conflicts or doesn't settle it | `CANNOT_DETERMINE` | CANNOT_DETERMINE: only with timing UNCERTAIN; notes required |
| `explicit` | Do the post's own words state this action? | | | Do the post's own words state the action? |
| | | Yes, the words say it | `YES` | YES: the words state the action ("long", "out", "stopped") |
| | | No, I'm inferring it from context | `NO` | NO: inferred from context, not stated |
| `follower_implication` | What this means for someone following the publisher | | | What this means for a follower |
| | | A possible instruction, as it happens | `INSTRUCTION_CANDIDATE` | INSTRUCTION_CANDIDATE: timing CURRENT |
| | | The publisher is already out, or smaller | `RECONCILE_EXIT` | RECONCILE_EXIT: HISTORICAL, FIRST_REPORT, CLOSE or REDUCE: the publisher is already out (or smaller) |
| | | The position changed without a real-time announcement | `RECONCILE_POSITION` | RECONCILE_POSITION: HISTORICAL, FIRST_REPORT, OPEN, ADD or MODIFY: the position or protection changed without a real-time announcement |
| | | Nothing new: a recap | `NO_NEW_INFORMATION` | NO_NEW_INFORMATION: HISTORICAL, REPEAT |
| | | Wait for the condition | `WAIT_FOR_CONDITION` | WAIT_FOR_CONDITION: CONDITIONAL |
| | | Nothing to do: commentary | `NONE` | NONE: COMMENTARY |
| | | The evidence conflicts or doesn't settle it | `CANNOT_DETERMINE` | CANNOT_DETERMINE: timing UNCERTAIN or announcement CANNOT_DETERMINE |
| `family` | Which market is this action in? | | | Which market does the action concern? |
| | | the market's own name | the market's code | `<family>`: a market of the form's list |
| | | A market not in this list | `OTHER` | OTHER: a market outside the list |
| | | The evidence conflicts or doesn't settle it | `UNKNOWN` | UNKNOWN: cannot determine |
| `instrument_as_read` | What does the post call the instrument? | | | How does this post name the instrument? |
| | | It names it: a box "The post's words" | the typed value | value given |
| | | The post doesn't say | `NOT_STATED` | not stated |
| | | The evidence conflicts or doesn't settle it | `CANNOT_DETERMINE` | cannot determine |
| `quoted_series` | What do the quoted prices refer to? | | | In which kind of price series are this action's numbers quoted? |
| | | A futures contract | `FUTURES` | FUTURES: a futures contract |
| | | A spot price or an index | `SPOT_OR_INDEX` | SPOT_OR_INDEX: a spot price or an index |
| | | A fund or ETF | `ETF_OR_FUND` | ETF_OR_FUND: an exchange-traded fund or other fund |
| | | A company's shares | `STOCK` | STOCK: a share |
| | | An option's premium | `OPTION` | OPTION: an option premium |
| | | Something else | `OTHER` | OTHER: another kind of series |
| | | The post doesn't say | `NOT_STATED` | NOT_STATED: no evidence, or no numbers |
| | | The evidence conflicts or doesn't settle it | `CANNOT_DETERMINE` | CANNOT_DETERMINE: conflicting evidence |
| `traded_instrument` | What does the publisher say they traded? | | | What does the publisher SAY they traded? |
| | | Futures | `FUTURES` | FUTURES: futures |
| | | A fund or ETF | `ETF_OR_FUND` | ETF_OR_FUND: a fund |
| | | Shares | `STOCK` | STOCK: shares |
| | | Options | `OPTIONS` | OPTIONS: options |
| | | A CFD or the spot market | `CFD_OR_SPOT` | CFD_OR_SPOT: a CFD or the spot market |
| | | Something else | `OTHER` | OTHER: another instrument |
| | | The post doesn't say | `NOT_STATED` | NOT_STATED: the post does not say |
| | | The evidence conflicts or doesn't settle it | `CANNOT_DETERMINE` | CANNOT_DETERMINE: conflicting statements |
| `side` | Is the position this action concerns long or short? | | | What is the direction of the POSITION the action concerns? |
| | | Long | `LONG` | LONG: a long position |
| | | Short | `SHORT` | SHORT: a short position |
| | | The evidence conflicts or doesn't settle it | `CANNOT_DETERMINE` | CANNOT_DETERMINE: the evidence does not settle it |
| `position_state` | Just before this action, was a position open in this market? | | | Immediately BEFORE this action, was a position open? (FLAT only when the posts establish that none existed) |
| | | Yes, a position was open | `OPEN` | OPEN: a position was open in this market before this action (for a FIRST_REPORT of an exit: still OPEN) |
| | | No, the posts show that none was open | `FLAT` | FLAT: the publisher's own account or the earlier posts shown establish that NO position exists before this action (it was closed, or this action begins the idea) |
| | | The evidence conflicts or doesn't settle it | `CANNOT_DETERMINE` | CANNOT_DETERMINE: the posts shown do not settle it; "the posts shown do not show a position" is this, with the flag MISSING_CONTEXT, never FLAT |
| `position_side` | Just before this action, was that position long or short? | | | BEFORE this action, on which side was that position? |
| | | Long | `LONG` | LONG: long |
| | | Short | `SHORT` | SHORT: short |
| | | The evidence conflicts or doesn't settle it | `CANNOT_DETERMINE` | CANNOT_DETERMINE: the posts shown do not settle it |
| `position_last_stop` | Just before this action, what was the latest stop the publisher had given? | | | BEFORE this action, what was the last stop communicated? |
| | | A stop was given: a box "The stop" | the typed value | value given |
| | | The post doesn't say | `NOT_STATED` | not stated |
| | | The evidence conflicts or doesn't settle it | `CANNOT_DETERMINE` | cannot determine |
| `effect` | What happened to the size or protection of the position? | | | How does this action change the position BEFORE this action? |
| | | Opened from nothing | `OPENS` | OPENS: flat to a position |
| | | Made larger | `INCREASES` | INCREASES: a larger position |
| | | Made smaller, still open | `REDUCES` | REDUCES: smaller, still open |
| | | Closed completely | `CLOSES` | CLOSES: to flat |
| | | Same size; the stop or a target changed | `AMENDS_PROTECTION` | AMENDS_PROTECTION: size unchanged; stop or target changed |
| | | No change | `NONE` | NONE: no change: set by the form only, for a REPEAT, for COMMENTARY and for a MODIFY from FLAT (never chosen) |
| | | The evidence conflicts or doesn't settle it | `CANNOT_DETERMINE` | CANNOT_DETERMINE: context missing (flag MISSING_CONTEXT) |
| `entry_price` | Which one number does the post give for this entry? | | | Which ONE number does the post tie to the entry of this action? |
| | | It gives a number: a box "The number" | the typed value | value given |
| | | The post doesn't say | `NOT_STATED` | not stated |
| | | The evidence conflicts or doesn't settle it | `CANNOT_DETERMINE` | cannot determine |
| `entry_role` | What kind of number is that entry price? | | | What kind of number is the entry price? |
| | | A price for subscribers to trade at | `INTENDED_ENTRY` | INTENDED_ENTRY: a price the publisher tells subscribers to transact at (only this role is a price instruction) |
| | | The price the publisher says they got | `HISTORICAL_FILL` | HISTORICAL_FILL: the price the publisher says they themselves got |
| | | Where the market is as they write | `CURRENT_MARKET` | CURRENT_MARKET: where the market is as the publisher acts or writes |
| | | The average price of the position | `AVERAGE_OR_COST_BASIS` | AVERAGE_OR_COST_BASIS: the average price of the position after several entries |
| | | The evidence conflicts or doesn't settle it | `UNRESOLVED` | UNRESOLVED: the context leaves two readings (flag AMBIGUOUS_PRICE_SEMANTICS); there is no default role |
| | | The post doesn't say | `NOT_STATED` | NOT_STATED: set by the form when the entry price is NOT_STATED |
| `entry_exactness` | How exactly is the entry number given? | | | How does the publisher express the entry number? |
| | | Exactly, with no hedge | `EXACT` | EXACT: a precise figure with no hedge |
| | | Approximately (around, about, ~, a range) | `APPROXIMATE` | APPROXIMATE: hedged (around, about, near, ~), an end of a range, or shorthand |
| `stop_price` | What stop does this post give for the position after this action? | | | Which stop does THIS post communicate for the position after this action? |
| | | It gives a stop: a box "The stop" | the typed value | value given |
| | | The post doesn't say | `NOT_STATED` | not stated |
| | | The evidence conflicts or doesn't settle it | `CANNOT_DETERMINE` | cannot determine |
| `targets` | What profit targets does this post give? | | | Which profit targets does this post communicate? |
| | | It gives targets: a box "The targets, separated by commas" | the typed value | value given |
| | | The post doesn't say | `NOT_STATED` | not stated |
| | | The evidence conflicts or doesn't settle it | `CANNOT_DETERMINE` | cannot determine |
| `amendment_field` | What does the change affect? | | | What does the MODIFY change? |
| | | The stop | `STOP` | STOP: the stop |
| | | A profit target | `TARGET` | TARGET: a profit target |
| | | Something else | `OTHER` | OTHER: something else |
| `amendment_to` | What is the new value? | | | What is the new value? |
| | | It gives the new value: a box "The new value" | the typed value | value given |
| | | The evidence conflicts or doesn't settle it | `CANNOT_DETERMINE` | cannot determine |
| `amendment_from` | What value does it replace? | | | Which value does it replace? |
| | | It gives the old value: a box "The old value" | the typed value | value given |
| | | The post doesn't say | `NOT_STATED` | not stated |
| | | The evidence conflicts or doesn't settle it | `CANNOT_DETERMINE` | cannot determine |
| `idea` | Which trade idea does this action belong to? | | | Which idea does this action belong to? |
| | | A new idea, starting here | `NEW_IDEA` | NEW_IDEA: this action starts one (normally an OPEN from flat) |
| | | The same idea as the earlier posts shown | `SAME_AS_PREVIOUS` | SAME_AS_PREVIOUS: it continues the idea of the earlier posts shown |
| | | Another listed idea: `<id>` | `SEQUENCE:<id>` | SEQUENCE:<id>: it belongs to another listed idea; choose it only when sure |
| | | The evidence conflicts or doesn't settle it | `UNKNOWN` | UNKNOWN: cannot tell |
| `reference_series_right` | Is this the price series the post uses? | | | Is the system's reference series the price series this post's numbers are quoted in? |
| | | Yes | `YES` | YES: yes |
| | | No | `NO` | NO: no |
| | | Unsure | `UNSURE` | UNSURE: cannot tell |
| `execution_instrument_right` | Would trading this instrument follow the idea in the post? | | | Would the instrument the system PROPOSES be a faithful way to follow this idea? |
| | | Yes | `YES` | YES: yes |
| | | No | `NO` | NO: no |
| | | Unsure | `UNSURE` | UNSURE: cannot tell |
| `ambiguities` | What in the publisher's message is unclear? | | | What in the PUBLISHER'S message is unclear? |
| | | Which action | `ACTION_UNCLEAR` | ACTION_UNCLEAR: which action |
| | | Long or short | `SIDE_UNCLEAR` | SIDE_UNCLEAR: which direction |
| | | Which market | `MARKET_UNCLEAR` | MARKET_UNCLEAR: which market |
| | | Which instrument | `INSTRUMENT_UNCLEAR` | INSTRUMENT_UNCLEAR: which instrument |
| | | Whether it happens now, happened earlier or is a plan | `TIMING_UNCLEAR` | TIMING_UNCLEAR: announcement, report or plan |
| | | Which position or idea | `POSITION_UNCLEAR` | POSITION_UNCLEAR: which position or idea |
| | | What a number is | `PRICE_ROLE_UNCLEAR` | PRICE_ROLE_UNCLEAR: what a number is |
| | | Several numbers could be the level | `SEVERAL_CANDIDATE_LEVELS` | SEVERAL_CANDIDATE_LEVELS: several numbers could be the level |
| | | A level given by reference (breakeven, “the lows”) | `LEVEL_BY_REFERENCE` | LEVEL_BY_REFERENCE: a level given by reference: breakeven, entry, "the lows" |
| | | A stop that isn't a simple level (on a close, a mental stop) | `STOP_NOT_A_SIMPLE_LEVEL` | STOP_NOT_A_SIMPLE_LEVEL: a stop on a close, a mental stop |
| | | How much | `SIZE_UNCLEAR` | SIZE_UNCLEAR: how much |
| | | Which contract month | `CONTRACT_MONTH_UNCLEAR` | CONTRACT_MONTH_UNCLEAR: which contract month |
| `additional_actions` | Does the post contain further actions that no form covers? | | | Which further actions does the post contain that no form covers? |
| `flags` | Does this review need flagging? | | | What does the REVIEW need? |
| | | An earlier post I need isn't shown | `MISSING_CONTEXT` | MISSING_CONTEXT: an earlier post needed to answer is not among those shown |
| | | What a number is can't be settled | `AMBIGUOUS_PRICE_SEMANTICS` | AMBIGUOUS_PRICE_SEMANTICS: a number's role cannot be settled (role UNRESOLVED) |
| | | This material can't settle it: the approver should look | `NEEDS_MORE_EVIDENCE` | NEEDS_MORE_EVIDENCE: cannot be settled from the material at all; the approver should look |
| | | The answer depends on a chart that is missing or unreadable | `CHART_NEEDED` | CHART_NEEDED: the answer depends on a chart that is missing, unreadable or not shown in this mode |
| | | The form has no truthful way to record this (explain in the notes) | `FORM_CANNOT_EXPRESS` | FORM_CANNOT_EXPRESS: the form has no truthful way to record this case; notes required; excluded from scoring and listed for the owner |
| `confidence` | How confident are you in this whole form? | | | How confident are you in this whole form? |
| | | High: another careful reviewer would answer the same | `HIGH` | HIGH: another knowledgeable reviewer with the same evidence would give the same answers |
| | | Medium: one or two answers are debatable | `MEDIUM` | MEDIUM: one or two fields are debatable |
| | | Low: substantial doubt | `LOW` | LOW: substantial doubt |
| `notes` | Notes | | | Notes |
| `role` | What is this number? | | | What is this number? |
| | | Not a price (an indicator, a percentage, a date, a size) | `NOT_A_PRICE` | NOT_A_PRICE: not a price of any market: an indicator reading, a percentage, a date, a size, a profit, the period of a moving average |
| | | A price of another market, or of another action | `OTHER_ACTION_OR_MARKET` | OTHER_ACTION_OR_MARKET: a price of another market, or of another action than this form records |
| | | A price a trade was actually done at | `HISTORICAL_FILL` | HISTORICAL_FILL: a price at which a trade was actually executed, even at the stop or the target ("closed at 74.90", "stopped out at 2.84") |
| | | A stop | `STOP` | STOP: a level named AS a stop of this position, not as the price obtained: new, amended, restated, the one in force or the one replaced |
| | | A profit target | `TARGET` | TARGET: a level named AS a profit target of this position, not as the price obtained: new, restated, reached or the one replaced |
| | | A price to enter at | `INTENDED_ENTRY` | INTENDED_ENTRY: a price the publisher instructs or plans to transact at |
| | | Where the market is | `CURRENT_MARKET` | CURRENT_MARKET: where the market is ("closing here, market 2,395") |
| | | The position's average price | `AVERAGE_OR_COST_BASIS` | AVERAGE_OR_COST_BASIS: the average price of the position |
| | | A price of another contract month | `OTHER_CONTRACT_MONTH` | OTHER_CONTRACT_MONTH: a price of another contract month, given only as a reference |
| | | A chart level (support, resistance, a prior high or low) | `CHART_LEVEL` | CHART_LEVEL: support, resistance, a prior high or low, a moving-average value, a trigger level |
| | | The evidence conflicts or doesn't settle it | `UNRESOLVED` | UNRESOLVED: a price of this market whose role cannot be settled (flag AMBIGUOUS_PRICE_SEMANTICS) |
| `exactness` | How exactly is it given? | | | How is this number expressed? |
| | | Exactly | `EXACT` | EXACT: a precise figure with no hedge (a round number without a hedge is EXACT) |
| | | Approximately | `APPROXIMATE` | APPROXIMATE: hedged (around, about, near, roughly, area, zone, ~), an end of a range, or rounded shorthand (4.5k) |
| `contract_month` | Which contract month is it for? | | | Which contract month is this number quoted for? |
| | | The front (nearest) month | `FRONT` | FRONT: front, nearest or spot month |
| | | The next month | `NEXT` | NEXT: next or second month |
| | | A month the post names | `NAMED` | NAMED: a named month or contract code (type it in month_named) |
| | | The post doesn't say | `NOT_STATED` | NOT_STATED: nothing in the material identifies the month |
| `month_named` | Which month or contract code is named? | | | Which month or code is named? |

## Judgments of the system's answers (after the reveal)

Question: "Is the system's answer right?" (before: "verdict on the system's <field>").

| Option as shown | Stored value | The wording before |
|---|---|---|
| Yes, the system is right | `CONFIRM` | CONFIRM: the system's value is right |
| No, my answer is right | `CORRECT` | CORRECT: the system's value is wrong, mine is right |
| Unsure | `UNSURE` | UNSURE |
| No judgment needed: this question doesn't apply to your reading. | `NOT_APPLICABLE` | NOT_APPLICABLE (does not apply to your reading) |

## Sections

| Group | Title as shown | The title before |
|---|---|---|
| A | The action | What kind of statement |
| B | Market and instrument | Market and instrument (source facts) |
| C | The position | Direction, position BEFORE this action, effect |
| D | The entry | Entry (OPEN and ADD only) |
| E | Stop and targets | Stop and targets (OPEN, ADD, MODIFY, REDUCE) |
| F | The change | Amendment (MODIFY only) |
| G | The trade idea | Idea |
| T | Every number in the post | The price table |
| H | The system's proposals | The system's mapping (asked only after the reveal; advisory) |
| I | Anything unclear | Ambiguities, flags, confidence, notes |

## What blocks a submission

| Code | Message as shown | The message before |
|---|---|---|
| `REQUIRED_UNANSWERED` | This question needs an answer. | every applicable field must be answered |
| `TIMING_ACTION` | This action doesn't fit when it happens: commentary has no action; now, earlier and later need a real action; “the evidence conflicts” needs the timing “the evidence conflicts” too. | timing COMMENTARY needs action NONE; CURRENT, HISTORICAL and CONDITIONAL need a real action; UNCERTAIN needs a real action or CANNOT_DETERMINE; CANNOT_DETERMINE as action only with UNCERTAIN |
| `POST_LABEL_ACTIONS` | The post's label must match the strongest of the actions you list (a trade now, then a plan, then a report of an earlier trade). | on a label-only form the post label must equal the strongest label of the listed actions |
| `ACTION_EFFECT` | This change to the position doesn't fit the action you chose. | the action and the effect are not an allowed pair (effect NONE is set by the form only, for a REPEAT, for COMMENTARY and for a MODIFY from FLAT) |
| `EFFECT_POSITION` | This change isn't possible from the position before the action. If the posts don't show that position, answer “The evidence conflicts or doesn't settle it” there and flag the missing context. | the effect is impossible from the position before this action: from FLAT not INCREASES, REDUCES, CLOSES or AMENDS_PROTECTION; from a position open on the same side not OPENS (not checked for a CONDITIONAL action). If the earlier posts shown do not show the position, answer CANNOT_DETERMINE with the flag MISSING_CONTEXT, not FLAT. |
| `SIDE_POSITION` | Long or short must match the position before this action. | for ADD, MODIFY, REDUCE and CLOSE, side and position_side must be equal |
| `ENTRY_FIELDS` | An entry number needs its kind, and how exactly it is given. | an entry number needs an entry role and EXACT or APPROXIMATE; an entry price CANNOT_DETERMINE needs an entry role; NOT_STATED needs neither |
| `TABLE_STOP` | A number you marked as a stop needs that stop in a stop question above. | a row with role STOP needs the stop in one of three fields: stop_price (a number or CANNOT_DETERMINE), position_last_stop (a number) or amendment_from (a number) |
| `TABLE_TARGET` | A number you marked as a profit target needs the targets above. | a row with role TARGET needs targets (numbers or CANNOT_DETERMINE), or the action CLOSE or REDUCE, or a MODIFY of a target with a number in amendment_from or amendment_to |
| `TABLE_ROW` | This number needs how exactly it is given and, where asked, its contract month. | a price role needs an exactness and, when asked, a contract month; NAMED needs month_named |
| `AMENDMENT` | The change needs what it affects and its new value; a new target must also be among the targets. | MODIFY needs amendment_field and amendment_to (a number or CANNOT_DETERMINE for STOP and TARGET); on a MODIFY of a target with a number, targets must contain it |
| `CANNOT_DETERMINE_NEEDS_REASON` | An answer that the evidence doesn't settle needs a reason: tick what is unclear, or a flag. | a CANNOT_DETERMINE, UNCERTAIN or UNRESOLVED answer needs at least one ambiguity or flag |
| `CANNOT_DETERMINE_NEEDS_NOTES` | When the action can't be settled, explain it in the notes. | action CANNOT_DETERMINE needs notes |
| `ESCALATION_NEEDS_NOTES` | When the form can't record this case, explain it in the notes. | FORM_CANNOT_EXPRESS needs notes |
| `ADDITIONAL_ACTION_REQUIRED` | List at least one action of the post. | a label-only form labelled ACTIONABLE, CONDITIONAL or RETROSPECTIVE needs at least one additional action |
| `CONFIDENCE` | Choose how confident you are. | confidence is required |
| `NOT_APPLICABLE_FIELD` | This answer doesn't fit your other answers: change it, or the answer it depends on. | a field that does not apply, or that waits on an unanswered answer, carries a value; or NOT_APPLICABLE was given for a field that applies |
| `DERIVED_VALUE` | This value follows from your other answers: change those instead. | a value the form derives (the action's label, the announcement for CURRENT, the follower implication, component) was given differently |

## Points to check (warnings)

| Code | Message as shown | The message before |
|---|---|---|
| `ENTRY_NOT_IN_TABLE` | You gave an entry number, but no number below has that kind. | an entry number is given but no row carries its role |
| `STOP_NOT_IN_TABLE` | You gave a stop, but no number below is marked as a stop. | a stop number is given but no row is STOP |
| `TARGET_NOT_IN_TABLE` | You gave targets, but no number below is marked as a target. | target numbers are given but no row is TARGET |
| `REPEAT_POSITION` | For a recap, the position before should already reflect the action the earlier post announced. | a REPEAT whose position before does not already reflect the action (a repeat OPEN, ADD, REDUCE or MODIFY with FLAT before; a repeat CLOSE with OPEN before; a repeat OPEN or ADD with a position open on the other side): for a REPEAT the position before is the one after the earlier announcement |
| `ENTRY_ROLE_ORDER` | A number below has an entry kind that comes before the entry kind you chose. | a row carries an entry kind that ranks before entry_role |
| `STOP_SIDE` | The stop is on the wrong side of the entry: a long's stop above it, or a short's below. | a long's stop above its entry, or a short's below |
| `ACTS_ON_FLAT` | This acts on a position that wasn't open. | ADD, REDUCE or CLOSE on a FLAT position with an effect other than NONE (for example ADD with OPENS), or a MODIFY on a FLAT position other than a REPEAT (a change to an order that is not yet a position: effect NONE) |
| `OPEN_ON_OPEN` | This opens a position while one is open on the other side (a reversal is two actions). | OPEN with a position already open on the other side (a reversal is two actions) |
| `NEW_IDEA_NOT_OPEN` | A new idea normally starts by opening a position. | NEW_IDEA with an action other than OPEN |
| `POSITION_CHAIN` | The position before this action differs from where your reading of the previous action of this post left it. | the position before this action differs from the position after your reading of the previous action of this post in this market |

## Why a question is not asked

| The rubric's reason | As shown |
|---|---|
| a label-only form has no price table | This form labels the whole post: it has no numbers to describe. |
| answer the entry price first | Answer the entry price first. |
| answer the last stop before this action first | Answer the latest stop before this action first. |
| answer the new value of the amendment first | Answer the new value first. |
| answer the quoted series first | Answer what the quoted prices refer to first. |
| answer the timing first | Answer when the action happens first. |
| answer the timing, the announcement and the action first | Answer when it happens, whether it is the first report and the action first. |
| answer what the MODIFY changes first | Answer what the change affects first. |
| answer whether this form records an action first | Answer whether this form records an action first. |
| asked for a historical action only | Asked only for an action that happened earlier. |
| asked only after the system's reading is revealed | Asked only after you have saved your own reading. |
| asked only for a futures series | Asked only when the prices are of a futures contract. |
| choose the action first | Answer what the publisher is doing first. |
| choose the role first | Answer what this number is first. |
| offered on a label-only form only when its label is ACTIONABLE, CONDITIONAL, RETROSPECTIVE or UNRESOLVED | Asked only when the post contains a trade, a plan or a report, or can't be settled. |
| offered on the last form of the post only | Asked on the last form of this post only. |
| only with NAMED | Only when the post names a month. |
| the action cannot be determined (notes required) | The action can't be settled: explain in the notes. |
| the amendment applies to MODIFY | Only for a change of the stop or a target. |
| the entry applies to OPEN and ADD | Only for opening or adding to a position. |
| the position before this action was FLAT | No position was open before this action. |
| the post has no numbers | The post has no numbers. |
| the post states no trading action | The post states no trading action. |
| the stop and targets do not apply to CLOSE | Not asked when the whole position is closed. |
| the system's mapping block is empty | The system proposed nothing here. |
| there is no entry number | There is no entry number. |
| there is no readable entry number | There is no readable entry number. |
| this form records no separate action | This form records no separate action. |
| not a price of this action | Not a price of this action. |

## Statuses

| Code | As shown |
|---|---|
| `NOT_STARTED` | Not started |
| `IN_PROGRESS` | In progress |
| `AWAITING_ASSESSMENT` | Awaiting your assessment |
| `COMPLETE` | Complete |
| `RETURNED` | Returned for changes |
