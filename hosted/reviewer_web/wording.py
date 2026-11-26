"""Every word a reviewer reads on the hosted site, in one place (DECIDED 40, section 6).

DISPLAY ONLY. Nothing here is a rule: every option still stores the rubric's own code
(``adjudication_rubric``), and no field, option, sentinel or validation rule changes. What changes
is what the page says: a short question with one decision in it, options in plain words, one short
example where help is needed, and messages that say what to do. Internal field names and option
codes stay off the reading flow; the Field reference page maps each wording to its field name and
code (``docs/HOSTED_REVIEWER_WORDING.md`` is generated from this module).

Four things are kept apart, and worded the same way everywhere:
  - NOT_ANSWERED: a state (nothing chosen yet), never an option;
  - the post doesn't say: the code NOT_STATED, where a field has it;
  - the evidence conflicts or doesn't settle it: CANNOT_DETERMINE (and UNKNOWN, UNCERTAIN,
    UNRESOLVED, which mean the same for their field);
  - this question doesn't apply: NOT_APPLICABLE, set by the form, never chosen.
"""

from __future__ import annotations

from tradingbot.runtime import adjudication_rubric as R

# ------------------------------------------------------------------ the four states

NOT_ANSWERED = "I haven't answered yet"
POST_SILENT = "The post doesn't say"
POSTS_SILENT = "The posts shown don't say"  # NOT_STATED where the earlier posts shown count too
UNSETTLED = "The evidence conflicts or doesn't settle it"
DOES_NOT_APPLY = "This question doesn't apply"
VALUE = "VALUE"  # the page's own code for "a value is typed" (forms.VALUE)

# ------------------------------------------------------------------ sections

SECTIONS = {
    "A": "The action",
    "B": "Market and instrument",
    "C": "The position",
    "D": "The entry",
    "E": "Stop and targets",
    "F": "The change",
    "G": "The trade idea",
    "T": "Every number in the post",
    "H": "The system's proposals",
    "I": "Anything unclear",
}
EXTRAS_TITLE = "Flags, confidence and notes"
POSITIONS_TITLE = "The position, as your answers leave it"

# ------------------------------------------------------------------ questions

QUESTIONS = {
    "component": "Does this form record one of the post's actions?",
    "timing": "When does this action happen?",
    "post_label": "What is the post, taken as a whole?",
    "post_label@INSTRUCTION": "What kind of action is this?",
    "announcement": "Is this post the first to tell subscribers about this action?",
    "follower_implication": "What this means for someone following the publisher",
    "action": "What does the publisher say they are doing?",
    "explicit": "Do the post's own words state this action?",
    "family": "Which market is this action in?",
    "instrument_as_read": "What does the post call the instrument?",
    "quoted_series": "What do the quoted prices refer to?",
    "traded_instrument": "What does the publisher say they traded?",
    "side": "Is the position this action concerns long or short?",
    "position_state": "Just before this action, was a position open in this market?",
    "position_side": "Just before this action, was that position long or short?",
    "position_last_stop": "Just before this action, what was the latest stop the publisher had given?",
    "effect": "What happened to the size or protection of the position?",
    "entry_price": "Which one number does the post give for this entry?",
    "entry_role": "What kind of number is that entry price?",
    "entry_exactness": "How exactly is the entry number given?",
    "stop_price": "What stop does this post give for the position after this action?",
    "targets": "What profit targets does this post give?",
    "amendment_field": "What does the change affect?",
    "amendment_to": "What is the new value?",
    "amendment_from": "What value does it replace?",
    "idea": "Which trade idea does this action belong to?",
    "reference_series_right": "Is this the price series the post uses?",
    "execution_instrument_right": "Would trading this instrument follow the idea in the post?",
    "ambiguities": "What in the publisher's message is unclear?",
    "additional_actions": "Does the post contain further actions that no form covers?",
    "flags": "Does this review need flagging?",
    "confidence": "How confident are you in this whole form?",
    "notes": "Notes",
    "role": "What is this number?",
    "exactness": "How exactly is it given?",
    "contract_month": "Does the post tie this number to a futures month?",
    "month_named": "Type the month or contract code exactly as the post writes it",
}

# One short example or hint, behind a disclosure ("Example"): reachable by keyboard and touch.
HELP = {
    "component": "Actions go on forms in the order they first appear: the first action on form 1, "
                 "the second on form 2. A form left over records no action.",
    "timing": "“Bought gold at 2,395.” happens now. “We bought gold this morning at 2,395.” "
              "happened earlier.",
    "post_label": "“Sold half our silver at 32.10.” is a trade made now: also list that action "
                  "below.",
    "announcement": "“Recap: last week we closed crude at 74.90”, after that close was "
                    "announced, is not the first report.",
    "action": "“Trimming half here.” closes part of a position.",
    "explicit": "“Closing this here.” states the action, even when the market comes from the "
                "earlier posts.",
    "family": "Take it from this post, or from the earlier posts shown when this post doesn't "
              "name it.",
    "instrument_as_read": "Copy the post's own words, such as “Dec gold”. Never add a ticker "
                          "yourself.",
    "quoted_series": "“Long December natural gas at 3.42” quotes a futures contract. Go by the "
                     "words and chart labels, never by how big the numbers are.",
    "traded_instrument": "A gold futures chart tells you where the prices came from. The post may "
                         "still leave the trade itself unspecified.",
    "side": "“Covered the gold short.” is short: the side of the position, not of the order.",
    "position_state": "If the posts shown simply don't show a position, choose “The evidence "
                      "conflicts or doesn't settle it” and flag the missing context; never No.",
    "position_side": "Covering a short means the position before was short.",
    "position_last_stop": "“Our 72 stop was never touched” restates the stop in force: 72.",
    "effect": "“Adding to our long” makes the position larger.",
    "entry_price": "“Will buy silver at 30.80 on a pullback” gives 30.80. Never copy a number "
                   "from a chart or an earlier post.",
    "entry_role": "“Long crude at 71.20, the order is working” is a price to trade at; “…, filled "
                  "on the open” is the price they got.",
    "entry_exactness": "A round number with no hedge is exact; “around 412” is approximate.",
    "stop_price": "“Stop stays 69.50” gives 69.50. Never carry a stop over from another post.",
    "targets": "Several targets: type them in the order stated, separated by commas.",
    "amendment_to": "“Raising the stop to 70” gives 70.",
    "amendment_from": "“Moving the target from 80 to 82” replaces 80.",
    "idea": "An idea is one position, from its opening to its final close.",
    "reference_series_right": "This checks a fact about the post: which prices its numbers are. "
                              "It approves nothing.",
    "execution_instrument_right": "Your view only. It approves nothing: whether the system may "
                                  "trade this instrument is the owner's separate decision.",
    "ambiguities": "Leave every box empty when nothing is unclear.",
    "additional_actions": "“Closed the crude long and bought gold”, with one form: list the gold "
                          "trade here.",
    "flags": "Flags are about this review; what is unclear in the post goes in the question "
             "above.",
    "notes": "Refer to a number by its row (“number 2”); don't paste the post.",
    "role": "“Crude bounced from 70 last week. Our stop goes to 70.”: the first 70 is a chart "
            "level, the second a stop.",
    "contract_month": "Only from what the posts show, never from the post's date or the price "
                      "level. If the post names the month, a box opens to type it.",
    "month_named": "For example DEC or Z6.",
}

# ------------------------------------------------------------------ options

OPTIONS: dict[str, dict[str, str]] = {
    "component": {"ACTION_RECORDED_HERE": "Yes, this form records one action",
                  "NO_SEPARATE_ACTION": "No, the post has fewer actions than forms"},
    "timing": {"CURRENT": "Now, or just done",
               "HISTORICAL": "Earlier, as the post itself says (or an existing order did it)",
               "CONDITIONAL": "Later, only if something stated happens",
               "COMMENTARY": "There is no action: commentary only",
               "UNCERTAIN": UNSETTLED},
    "post_label": {"ACTIONABLE": "A trade made now",
                   "CONDITIONAL": "A plan that depends on something later",
                   "RETROSPECTIVE": "A report of a trade made earlier",
                   "COMMENTARY": "Commentary: no trading action",
                   "UNRESOLVED": UNSETTLED},
    "announcement": {"FIRST_REPORT": "Yes, this is the first report",
                     "REPEAT": "No, an earlier post shown already reported it",
                     R.CANNOT_DETERMINE: UNSETTLED},
    "follower_implication": {
        "INSTRUCTION_CANDIDATE": "A possible instruction, as it happens",
        "RECONCILE_EXIT": "The publisher is already out, or smaller",
        "RECONCILE_POSITION": "The position changed without a real-time announcement",
        "NO_NEW_INFORMATION": "Nothing new: a recap",
        "WAIT_FOR_CONDITION": "Wait for the condition",
        "NONE": "Nothing to do: commentary",
        R.CANNOT_DETERMINE: UNSETTLED},
    "action": {"OPEN": "Opening a new position",
               "ADD": "Adding to a position",
               "MODIFY": "Changing only the stop or a target",
               "REDUCE": "Closing part of a position",
               "CLOSE": "Closing the whole position",
               "NONE": "No trading action",
               R.CANNOT_DETERMINE: UNSETTLED},
    "explicit": {"YES": "Yes, the words say it", "NO": "No, I'm inferring it from context"},
    "family": {"OTHER": "A market not in this list", "UNKNOWN": UNSETTLED},
    "quoted_series": {"FUTURES": "A futures contract",
                      "SPOT_OR_INDEX": "A spot price or an index",
                      "ETF_OR_FUND": "A fund or ETF",
                      "STOCK": "A company's shares",
                      "OPTION": "An option's premium",
                      "OTHER": "Something else",
                      R.NOT_STATED: POST_SILENT,
                      R.CANNOT_DETERMINE: UNSETTLED},
    "traded_instrument": {"FUTURES": "Futures",
                          "ETF_OR_FUND": "A fund or ETF",
                          "STOCK": "Shares",
                          "OPTIONS": "Options",
                          "CFD_OR_SPOT": "A CFD or the spot market",
                          "OTHER": "Something else",
                          R.NOT_STATED: POST_SILENT,
                          R.CANNOT_DETERMINE: UNSETTLED},
    "side": {"LONG": "Long", "SHORT": "Short", R.CANNOT_DETERMINE: UNSETTLED},
    "position_state": {"OPEN": "Yes, a position was open",
                       "FLAT": "No, the posts show that none was open",
                       R.CANNOT_DETERMINE: UNSETTLED},
    "position_side": {"LONG": "Long", "SHORT": "Short", R.CANNOT_DETERMINE: UNSETTLED},
    "effect": {"OPENS": "Opened from nothing",
               "INCREASES": "Made larger",
               "REDUCES": "Made smaller, still open",
               "CLOSES": "Closed completely",
               "AMENDS_PROTECTION": "Same size; the stop or a target changed",
               "NONE": "No change",
               R.CANNOT_DETERMINE: UNSETTLED},
    "entry_role": {"INTENDED_ENTRY": "A price for subscribers to trade at",
                   "HISTORICAL_FILL": "The price the publisher says they got",
                   "CURRENT_MARKET": "Where the market is as they write",
                   "AVERAGE_OR_COST_BASIS": "The average price of the position",
                   "UNRESOLVED": UNSETTLED,
                   R.NOT_STATED: POST_SILENT},
    "entry_exactness": {"EXACT": "Exactly, with no hedge",
                        "APPROXIMATE": "Approximately (around, about, ~, a range)"},
    "amendment_field": {"STOP": "The stop", "TARGET": "A profit target",
                        "OTHER": "Something else"},
    "idea": {"NEW_IDEA": "A new idea, starting here",
             "SAME_AS_PREVIOUS": "The same idea as the earlier posts shown",
             "SEQUENCE:": "Another listed idea",
             "UNKNOWN": UNSETTLED},
    "reference_series_right": {"YES": "Yes", "NO": "No", "UNSURE": "Unsure"},
    "execution_instrument_right": {"YES": "Yes", "NO": "No", "UNSURE": "Unsure"},
    "ambiguities": {"ACTION_UNCLEAR": "Which action",
                    "SIDE_UNCLEAR": "Long or short",
                    "MARKET_UNCLEAR": "Which market",
                    "INSTRUMENT_UNCLEAR": "Which instrument",
                    "TIMING_UNCLEAR": "Whether it happens now, happened earlier or is a plan",
                    "POSITION_UNCLEAR": "Which position or idea",
                    "PRICE_ROLE_UNCLEAR": "What a number is",
                    "SEVERAL_CANDIDATE_LEVELS": "Several numbers could be the level",
                    "LEVEL_BY_REFERENCE": "A level given by reference (breakeven, “the lows”)",
                    "STOP_NOT_A_SIMPLE_LEVEL": "A stop that isn't a simple level (on a close, "
                                               "a mental stop)",
                    "SIZE_UNCLEAR": "How much",
                    "CONTRACT_MONTH_UNCLEAR": "Which contract month"},
    "flags": {"MISSING_CONTEXT": "An earlier post I need isn't shown",
              "AMBIGUOUS_PRICE_SEMANTICS": "What a number is can't be settled",
              "NEEDS_MORE_EVIDENCE": "This material can't settle it: the approver should look",
              "CHART_NEEDED": "The answer depends on a chart that is missing or unreadable",
              R.FORM_CANNOT_EXPRESS: "The form has no truthful way to record this (explain in "
                                     "the notes)"},
    "confidence": {"HIGH": "High: another careful reviewer would answer the same",
                   "MEDIUM": "Medium: one or two answers are debatable",
                   "LOW": "Low: substantial doubt"},
    "role": {"NOT_A_PRICE": "Not a price (an indicator, a percentage, a date, a size)",
             "OTHER_ACTION_OR_MARKET": "A price of another market, or of another action",
             "HISTORICAL_FILL": "A price a trade was actually done at",
             "STOP": "A stop",
             "TARGET": "A profit target",
             "INTENDED_ENTRY": "A price to enter at",
             "CURRENT_MARKET": "Where the market is",
             "AVERAGE_OR_COST_BASIS": "The position's average price",
             "OTHER_CONTRACT_MONTH": "A price of another contract month",
             "CHART_LEVEL": "A chart level (support, resistance, a prior high or low)",
             "UNRESOLVED": UNSETTLED},
    "exactness": {"EXACT": "Exactly", "APPROXIMATE": "Approximately"},
    "contract_month": {"FRONT": "Yes, the nearest (front) month",
                       "NEXT": "Yes, the month after it",
                       "NAMED": "Yes, a month the post names",
                       R.NOT_STATED: POSTS_SILENT},
}
# The further actions reuse the questions' own option words.
ADDITIONAL = {"timing": ("When", "timing"), "action": ("What", "action"),
              "family": ("Market", "family"), "side": ("Long or short", "side")}

# A field with a typed value: the words of the choice "I'll type it", and of the box.
VALUE_CHOICE = {
    "instrument_as_read": ("It names it", "The post's words"),
    "position_last_stop": ("A stop was given", "The stop"),
    "entry_price": ("It gives a number", "The number"),
    "stop_price": ("It gives a stop", "The stop"),
    "targets": ("It gives targets", "The targets, separated by commas"),
    "amendment_to": ("It gives the new value", "The new value"),
    "amendment_from": ("It gives the old value", "The old value"),
}
SENTINEL = {R.NOT_STATED: POST_SILENT, R.CANNOT_DETERMINE: UNSETTLED}
# Where the rubric lets the earlier posts shown answer, "doesn't say" is about all of them.
SILENT_IN_POSTS_SHOWN = ("position_last_stop", "amendment_from", "contract_month")


def sentinel(f: str, code: str) -> str:
    if code == R.NOT_STATED and f in SILENT_IN_POSTS_SHOWN:
        return POSTS_SILENT
    return SENTINEL[code]


# The two proposals after the reveal: what each thing is, and a stored code in words.
PROPOSAL_IS = {
    "reference_series_right": ("The price series the system would compare the post's prices "
                               "with. The system records only this name for it."),
    "execution_instrument_right": "The instrument the system would trade to follow this idea.",
}
INSTRUMENT_WORDS = {"FUT": "futures contract", "FUTURES": "futures contract",
                    "ETF": "exchange-traded fund", "CFD": "CFD", "SPOT": "spot market",
                    "OPT": "options", "OPTIONS": "options", "STK": "shares", "STOCK": "shares"}


def proposal_words(f: str, value: str) -> str:
    """A proposed value in words: "FUT" is "A futures contract"; "TLT ETF" is "TLT, an
    exchange-traded fund". A name the site does not know stays as written."""
    parts = str(value).split()
    if f != "execution_instrument_right" or not parts:
        return str(value)
    known = [INSTRUMENT_WORDS.get(p.upper()) for p in parts]
    if len(parts) == 1 and known[0]:
        return ("A " if known[0][0] not in "aeiouAEIOU" or known[0] == "CFD" else "An ") \
            + known[0]
    if len(parts) == 2 and known[1] and not known[0]:
        art = "an" if known[1][0] in "aeiou" else "a"
        return f"{parts[0]}, {art} {known[1]}"
    return str(value)

VERDICT_QUESTION = "Is the system's answer right?"
VERDICTS = {"CONFIRM": "Yes, the system is right",
            "CORRECT": "No, my answer is right",
            "UNSURE": "Unsure"}
VERDICT_FIXED = "No judgment needed: this question doesn't apply to your reading."
VERDICT_HELP = ("Each judgment is on the SYSTEM's answer, never on yours. It starts from the "
                "comparison of the two answers; change it whenever you see otherwise. If the "
                "system points to something you missed, correct your own answer first.")

FURTHER_COUNT = "How many further actions?"


def further_count(n: int) -> str:
    return "None" if n == 0 else f"{n} more action" + ("" if n == 1 else "s")


# ------------------------------------------------------------------ why a question is not asked

REASONS = {
    "a label-only form has no price table": "This form labels the whole post: it has no numbers "
                                            "to describe.",
    "answer the entry price first": "Answer the entry price first.",
    "answer the last stop before this action first": "Answer the latest stop before this "
                                                     "action first.",
    "answer the new value of the amendment first": "Answer the new value first.",
    "answer the quoted series first": "Answer what the quoted prices refer to first.",
    "answer the timing first": "Answer when the action happens first.",
    "answer the timing, the announcement and the action first": "Answer when it happens, "
                                                                "whether it is the first report "
                                                                "and the action first.",
    "answer what the MODIFY changes first": "Answer what the change affects first.",
    "answer whether this form records an action first": "Answer whether this form records an "
                                                        "action first.",
    "asked for a historical action only": "Asked only for an action that happened earlier.",
    "asked only after the system's reading is revealed": "Asked only after you have saved your "
                                                         "own reading.",
    "asked only for a futures series": "Asked only when the prices are of a futures contract.",
    "choose the action first": "Answer what the publisher is doing first.",
    "choose the role first": "Answer what this number is first.",
    "offered on a label-only form only when its label is ACTIONABLE, CONDITIONAL, RETROSPECTIVE "
    "or UNRESOLVED": "Asked only when the post contains a trade, a plan or a report, or can't "
                     "be settled.",
    "offered on the last form of the post only": "Asked on the last form of this post only.",
    "only with NAMED": "Only when the post names a month.",
    "the action cannot be determined (notes required)": "The action can't be settled: explain "
                                                         "in the notes.",
    "the amendment applies to MODIFY": "Only for a change of the stop or a target.",
    "the entry applies to OPEN and ADD": "Only for opening or adding to a position.",
    "the position before this action was FLAT": "No position was open before this action.",
    "the post has no numbers": "The post has no numbers.",
    "the post states no trading action": "The post states no trading action.",
    "the stop and targets do not apply to CLOSE": "Not asked when the whole position is closed.",
    "the system's mapping block is empty": "The system proposed nothing here.",
    "there is no entry number": "There is no entry number.",
    "there is no readable entry number": "There is no readable entry number.",
    "this form records no separate action": "This form records no separate action.",
    "not a price of this action": "Not a price of this action.",
}
SET_BY_FORM = "Set by the form from your answers"
# Which answer each value the form sets comes from ("set by the form from ...").
DERIVED_FROM = {
    "component": "because this post has one form",
    "post_label": "from when the action happens",
    "announcement": "from when the action happens",
    "follower_implication": "from when it happens, whether this is the first report, and the "
                            "action",
    "action": "because there is no action: commentary only",
    "effect": "from the action and the position before it (or because this post repeats an "
              "earlier report)",
    "entry_role": "because the post gives no entry number",
    "stop_price": "from the new value of the change",
    "amendment_from": "from the latest stop before this action",
}


# The answer a value the form sets follows from, to name it in words.
DERIVED_SOURCE = {"post_label": "timing", "announcement": "timing", "action": "timing",
                  "follower_implication": "timing", "entry_role": "entry_price",
                  "stop_price": "amendment_to", "amendment_from": "position_last_stop"}


def set_by_form(f: str, value: str = "", answers: dict | None = None) -> str:
    """Why the form set this value, in one clause a reviewer can follow."""
    answers = answers or {}
    if f == "announcement" and value == "FIRST_REPORT" and answers.get("timing") == "CURRENT":
        return "set by the form: the action happens now, so this post is its first report"
    if f == "announcement" and answers.get("timing") == "UNCERTAIN":
        return ("set by the form: when the action happens is not settled, so this cannot be "
                "settled either")
    if f == "effect" and value == "NONE" and answers.get("announcement") == "REPEAT":
        return "set by the form: this post repeats an earlier report, so nothing changes"
    if f == "effect" and value == "AMENDS_PROTECTION":
        return "set by the form: a change of the stop or a target keeps the size"
    if f == "component":
        return "set by the form: this post has one form, so this form records its action"
    source = DERIVED_SOURCE.get(f)
    if source and answers.get(source) not in (None, "", R.NOT_APPLICABLE):
        return (f"set by the form because you answered “{shown(source, answers[source])}” to "
                f"“{QUESTIONS[source]}”")
    return "set by the form " + DERIVED_FROM.get(f, "from your answers")


VERDICT_PRESET = ("Pre-set to “Yes, the system is right” because your answer is the same. "
                  "You can change it.")
# Every pre-set judgment says why (the rule is the rubric's own: same, different, or no answer).
VERDICT_PRESETS = {
    "CONFIRM": VERDICT_PRESET,
    "CORRECT": ("Pre-set to “No, my answer is right” because your answer differs from the "
                "system's. Compare both with the post, and change this if the system is right."),
    "UNSURE": ("Pre-set to “Unsure” because you gave no answer here, or said the evidence "
               "doesn't settle it. You can change it."),
}
VERDICT_PRESET_EMPTY = ("Pre-set to “Yes, the system is right”: the system read nothing here, "
                        "which counts as the same as your “{mine}”. You can change it.")
SYSTEM_NOTHING = "The system read nothing here"
ASSESSMENT_RETURNED = "Your assessment of {hhmm} UTC was returned for changes"

# The idea choices: the reviewer's own sequences by their dashboard number.
IDEA_OWN = "The same idea as the earlier posts shown ({seq}, this form's own)"
IDEA_LISTED = "Another idea of your list: {seq}"
IDEA_OUTSIDE = "An idea outside your list (number {k} of those)"


# What belongs on each form of a post with several forms (the rubric's own rule; nothing of
# what the system read).
FORMS_RULE = ("A post with several actions has one form per action, in the order the actions "
              "first appear in the post: the first action on form 1, the second on form 2. A "
              "form left over records no action.")

# ------------------------------------------------------------------ what blocks a submission

ANSWER_NEEDED = "This question needs an answer."
BLOCKING = {
    "REQUIRED_UNANSWERED": ANSWER_NEEDED,
    "TIMING_ACTION": "This action doesn't fit when it happens: commentary has no action; now, "
                     "earlier and later need a real action; “the evidence conflicts” needs "
                     "the timing “the evidence conflicts” too.",
    "POST_LABEL_ACTIONS": "The post's label must match the strongest of the actions you list "
                          "(a trade now, then a plan, then a report of an earlier trade).",
    "ACTION_EFFECT": "This change to the position doesn't fit the action you chose.",
    "EFFECT_POSITION": "This change isn't possible from the position before the action. If the "
                       "posts don't show that position, answer “The evidence conflicts or "
                       "doesn't settle it” there and flag the missing context.",
    "SIDE_POSITION": "Long or short must match the position before this action.",
    "ENTRY_FIELDS": "An entry number needs its kind, and how exactly it is given.",
    "TABLE_STOP": "A number you marked as a stop needs that stop in a stop question above.",
    "TABLE_TARGET": "A number you marked as a profit target needs the targets above.",
    "TABLE_ROW": "This number needs how exactly it is given and, where asked, its contract "
                 "month.",
    "AMENDMENT": "The change needs what it affects and its new value; a new target must also "
                 "be among the targets.",
    "CANNOT_DETERMINE_NEEDS_REASON": "An answer that the evidence doesn't settle needs a "
                                     "reason: tick what is unclear, or a flag.",
    "CANNOT_DETERMINE_NEEDS_NOTES": "When the action can't be settled, explain it in the notes.",
    "ESCALATION_NEEDS_NOTES": "When the form can't record this case, explain it in the notes.",
    "ADDITIONAL_ACTION_REQUIRED": "List at least one action of the post.",
    "CONFIDENCE": "Choose how confident you are.",
    "NOT_APPLICABLE_FIELD": "This answer doesn't fit your other answers: change it, or the "
                            "answer it depends on.",
    "DERIVED_VALUE": "This value follows from your other answers: change those instead.",
}
# Answering, rather than correcting: how the summary counts a problem.
UNANSWERED_CODES = ("REQUIRED_UNANSWERED", "CONFIDENCE", "ADDITIONAL_ACTION_REQUIRED",
                    "TABLE_ROW", "ENTRY_FIELDS", "AMENDMENT", "CANNOT_DETERMINE_NEEDS_REASON",
                    "CANNOT_DETERMINE_NEEDS_NOTES", "ESCALATION_NEEDS_NOTES")

WARNINGS = {
    "ENTRY_NOT_IN_TABLE": "You gave an entry number, but no number below has that kind.",
    "STOP_NOT_IN_TABLE": "You gave a stop, but no number below is marked as a stop.",
    "TARGET_NOT_IN_TABLE": "You gave targets, but no number below is marked as a target.",
    "REPEAT_POSITION": "For a recap, the position before should already reflect the action "
                       "the earlier post announced.",
    "ENTRY_ROLE_ORDER": "A number below has an entry kind that comes before the entry kind you "
                        "chose.",
    "STOP_SIDE": "The stop is on the wrong side of the entry: a long's stop above it, or a "
                 "short's below.",
    "ACTS_ON_FLAT": "This acts on a position that wasn't open.",
    "OPEN_ON_OPEN": "This opens a position while one is open on the other side (a reversal "
                    "is two actions).",
    "NEW_IDEA_NOT_OPEN": "A new idea normally starts by opening a position.",
    "POSITION_CHAIN": "The position before this action differs from where your reading of the "
                      "previous action of this post left it.",
}
WARNINGS_TITLE = "Please check before you submit"
WARNINGS_TICK = "I have checked these"

# ------------------------------------------------------------------ the page's own messages

SUMMARY_NOT_SUBMITTED = "Your form hasn't been submitted."
GO_TO_QUESTION = "Go to question"
NUMBERS = ("No", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine", "Ten")


def counted(n: int, one: str, many: str) -> str:
    """"Two questions need" with the real count, as a sentence starts."""
    word = NUMBERS[n] if n < len(NUMBERS) else str(n)
    return f"{word} {one if n == 1 else many}"


def refusal_summary(answers: int, corrections: int) -> str:
    """"Your form hasn't been submitted. Two questions need an answer." (and the corrections)."""
    parts = []
    if answers:
        parts.append(counted(answers, "question needs an answer", "questions need an answer"))
    if corrections:
        part = counted(corrections, "answer needs a correction", "answers need a correction")
        parts.append(part if not parts else part[0].lower() + part[1:])
    if not parts:
        return SUMMARY_NOT_SUBMITTED
    return f"{SUMMARY_NOT_SUBMITTED} {' and '.join(parts)}."


REFUSALS = {
    "SYSTEM_READING_ALREADY_SEEN": (
        "Your own reading can no longer be saved for this form: one is already stored, or the "
        "system's answers have been shown for this post. Nothing saved is lost; the page shows "
        "where the form stands now."),
    "NOT_REVEALED": ("The assessment can't be saved yet: the system's answers have not been "
                     "shown for this form."),
    "BLIND_FIRST_PER_POST": ("Every form of this post needs your own reading before the "
                             "system's answers can be assessed."),
    "WARNINGS_NOT_ACKNOWLEDGED": ("The points to check changed, or were not ticked: read them "
                                  "and tick “I have checked these”."),
    "VERDICT_FOR_EVERY_COMPARED_FIELD": ("Every system answer and every number needs your "
                                         "judgment."),
    "BLOCKED": "",
    "REVOKED": "This access has been withdrawn.",
    "NO_ANCHOR": ("Nothing was stored: the site is not yet open for saving your review. You "
                  "don't need to do anything now. Try again later, and if this keeps happening, "
                  "tell the person who invited you."),
}
REFUSAL_OTHER = "The server could not accept these answers. Check them and submit again."

SAVE = {"INDEPENDENT": "Save independent reading", "AFTER_REVEAL": "Save assessment"}
SAVE_HINT = {"INDEPENDENT": "Saves your own reading. The system's answers are shown only "
                            "after this.",
             "AFTER_REVEAL": "Saves your judgments of the system's answers, with any "
                             "corrections to your reading."}

# The three states of what is on the page.
STATE_EDITING = "Edits not yet kept"
STATE_UNSUBMITTED = "Not submitted yet"


def state_draft(hhmm: str) -> str:
    return f"Draft kept at {hhmm} UTC, not submitted"


def state_submitted(hhmm: str) -> str:
    return f"Submitted at {hhmm} UTC"


def day_time(at: str) -> str:
    """"25 Nov 2026 at 09:14 UTC" for a stored time (UTC as stored)."""
    from datetime import datetime
    try:
        moment = datetime.fromisoformat(str(at))
    except ValueError:
        return f"{str(at)[:10]} at {str(at)[11:16]} UTC"
    return f"{moment.day} {moment:%b %Y} at {moment:%H:%M} UTC"


# After a confirmed save (DECIDED 40 section 4: the owner's words).
SAVED_READING_REVIEW = "Your reading is saved. Review the system's answers"
SAVED_READING_OTHER_FORM = "Your reading is saved. Complete form {k} of {n} for this post next"
SAVED_READING_OTHER_FORM_DETAIL = ("The system's answers are shown once every form of this post "
                                   "has your own reading.")
SAVED_REVIEW_COMPLETE = "Review complete. Continue to the next form."
SAVED_ALL_DONE = "Review complete. Every form in your list is done."
SAVED_RECEIPT = "Saved. Record {seq}, {day} at {hhmm} UTC."
REVIEW_COMPLETE_HERE = "Review complete. Your assessment of this form is saved."
COMPLETE_AGAIN = "This form is complete: your reading and your assessment are saved."
COMPLETE_AGAIN_DETAIL = ("Nothing more is needed here. Save again only to correct your "
                         "assessment: the newest one counts.")
NEXT_ACTION = {"review": "Review the system's answers", "other": "Go to form {k} of {n}",
               "next": "Continue to the next form", "home": "Back to the dashboard"}

# The dashboard and the sidebar.
SEQUENCE = "Sequence {n}"
NO_SEQUENCE = "Single posts outside any sequence"
YOU_READ = "You read"


def hhmm(at) -> str:
    """HH:MM (UTC) of a stored time: a datetime or its ISO text."""
    from datetime import UTC, datetime
    if isinstance(at, datetime):
        return f"{(at.astimezone(UTC) if at.tzinfo else at):%H:%M}"
    try:
        return f"{datetime.fromisoformat(str(at)).astimezone(UTC):%H:%M}"
    except ValueError:
        return str(at)[11:16]


def post_time(at: str) -> str:
    """"3 Mar 2026, 10:15 UTC" for a post's time."""
    from datetime import datetime
    try:
        moment = datetime.fromisoformat(str(at))
    except ValueError:
        return str(at)
    return f"{moment.day} {moment:%b %Y}, {moment:%H:%M} UTC"


def form_label(post: int, k: int, n: int) -> str:
    """"Post 2 · Form 1 of 2" (the form count only when the post has more than one)."""
    return f"Post {post}" + (f" · Form {k} of {n}" if n > 1 else "")


def place(sequence: str, post: int, k: int, n: int) -> str:
    return f"{sequence}, post {post}" + (f", form {k} of {n}" if n > 1 else "")


def next_button(code: str, where: str) -> str:
    """The one primary button of the dashboard: what it opens, in plain words."""
    verb = {"RETURNED": "Correct the returned reading", "AWAITING_ASSESSMENT":
            "Assess the system's answers", "NOT_STARTED": "Start"}.get(code, "Continue")
    return f"{verb}: {where}" if where else verb


# What remains after a save (progress.remaining_step's codes), in the owner's words.
STEP_TEXT = {
    "SAVE_YOUR_READING": "Save your own reading of this form.",
    "COMPLETE_OTHER_FORM": ("Your reading is saved. Another form of this post must be completed "
                            "before the system's answers are shown: form {k} of {n}."),
    "ASSESS_SYSTEM_ANSWERS": "Your reading is saved. Review the system's answers.",
    "CORRECT_RETURNED": ("This reading was returned for changes. Correct it and save the "
                         "assessment again."),
    "NEXT_FORM": "Review complete. Continue to the next form.",
    "ALL_DONE": "Review complete. Every form assigned to you is done.",
}

# Form statuses (progress's codes; FLOW's model, these words).
STATUS = {"NOT_STARTED": "Not started", "IN_PROGRESS": "In progress",
          "AWAITING_ASSESSMENT": "Awaiting your assessment", "COMPLETE": "Complete",
          "RETURNED": "Returned for changes"}
# The page's own states (forms.step_state) in the same words.
STEP_STATUS = {"INDEPENDENT_DUE": "NOT_STARTED", "REVEAL_PENDING": "IN_PROGRESS",
               "AFTER_REVEAL_DUE": "AWAITING_ASSESSMENT", "DONE": "COMPLETE",
               "RETURNED": "RETURNED"}

# The posts.
EARLIER_POST = "Earlier post · {k} of {n}"
POST_UNDER_REVIEW = "Post you're reviewing"
FORM_OF_POST = "Form {k} of {n} for this post"
LABEL_ONLY_FORM = "One form for the whole post"
VIEW_POST = "View post"
BACK_TO_QUESTION = "Back to my question"
FOR_LATER = "For later"
FOR_LATER_LIST = "Marked for later"
FOR_LATER_NOTE = "A mark is only a reminder: a question marked for later still needs its answer."

HOW_TO_REVIEW = (
    "Read the post you're reviewing and the earlier posts shown with it, nothing else.",
    ("Answer every question on your own. Where the evidence doesn't settle something, say so "
    "(“The evidence conflicts or doesn't settle it”) and tick what is unclear: never guess."),
    ("Save your own reading. When every form of the post has one, the system's answers appear "
    "beside yours: judge each one, and save the assessment."),
)


# ------------------------------------------------------------------ lookups


def question(f: str, kind: str = R.INSTRUCTION) -> str:
    if f == "post_label" and kind == R.INSTRUCTION:
        return QUESTIONS["post_label@INSTRUCTION"]
    return QUESTIONS[f]


def option(f: str, code: str) -> str:
    """The words of one option. A market keeps its own name; another listed idea is named."""
    if code == "":
        return NOT_ANSWERED
    if code == R.NOT_APPLICABLE:
        return DOES_NOT_APPLY
    if f == "family" and code not in OPTIONS["family"]:
        return code
    if f == "idea" and code.startswith("SEQUENCE:"):
        return OPTIONS["idea"]["SEQUENCE:"]  # the form names each one (forms.idea_names)
    if f == "post_label" and code not in OPTIONS[f]:
        return code
    if f in VALUE_CHOICE:
        return sentinel(f, code) if code in SENTINEL else code
    if code == R.NOT_STATED and f in SILENT_IN_POSTS_SHOWN:
        return POSTS_SILENT
    return OPTIONS.get(f, {}).get(code, code)


def shown(f: str, value) -> str:
    """A stored or proposed value of ``f`` in words (a typed value as typed)."""
    if isinstance(value, list):
        if f == "ambiguities" or f == "flags":
            return "; ".join(OPTIONS[f].get(v, str(v)) for v in value) or "Nothing ticked"
        return ", ".join(map(str, value)) or POST_SILENT
    value = "" if value is None else str(value)
    if value in ("", R.NOT_APPLICABLE):
        return option(f, value)
    if f in VALUE_CHOICE or f not in OPTIONS and f != "family":
        return sentinel(f, value) if value in SENTINEL else value
    words = option(f, value)
    return humane(words) if words == value and f != "family" else words


def humane(code: str) -> str:
    """A code no option of the field has (a value only the system writes, such as NONE), as
    words: "NONE" becomes "None"."""
    if code.isupper() or "_" in code:
        return code.replace("_", " ").capitalize()
    return code


def reason(text: str) -> str:
    return REASONS.get(text, text[:1].upper() + text[1:] + ".")


def describe_position(p: dict | None) -> str:
    """The rubric's position (``position_after``) in words."""
    if p is None:
        return DOES_NOT_APPLY
    state = p.get("state", "")
    if state == "OPEN":
        side = {"LONG": "long", "SHORT": "short"}.get(p.get("side", ""), "")
        stop = p.get("stop", "")
        stop_text = ("no stop given" if stop == R.NOT_STATED else
                     "stop not settled" if stop == R.CANNOT_DETERMINE else
                     "stop not answered yet" if not stop else f"stop {stop}")
        return (f"A {side} position is open" if side else
                "A position is open (long or short not settled)" if p.get("side") ==
                R.CANNOT_DETERMINE else "A position is open (long or short not answered yet)") \
            + f", {stop_text}."
    if state == "FLAT":
        return "No position is open."
    if state == R.CANNOT_DETERMINE:
        return "Not settled by the evidence."
    return "Not answered yet."


POSITION_BEFORE = "Where your reading of the previous action of this post left this market"
POSITION_AFTER = "After this action"


# ------------------------------------------------------------------ the table of the documentation


def _before_options(f: str) -> list[tuple[str, str]]:
    """(code, what the page showed for it before): ``CODE: meaning`` from the rubric."""
    field = R.BY_NAME.get(f)
    return [(c, f"{c}: {m}") for c, m in (field.values if field else ())]


def asked_fields() -> list[str]:
    """Every field the form can show, in form order, then the price table's columns."""
    return [*R.INSTRUCTION_FIELDS, "flags", "confidence", "notes", *R.ROW_KEYS]


def wording_table_markdown() -> str:
    lines = [
        "# The hosted reviewer site: the wording (generated)",
        "",
        ("Generated from `hosted/reviewer_web/wording.py` by `tests/test_hosted_wording.py`; do "
        "not edit by hand. Every option still stores the rubric's own code: the wording is "
        "display only (DECIDED 40, section 6)."),
        "",
        "The four states, worded the same way everywhere:",
        "",
        f"- not answered yet (a state, never an option): \"{NOT_ANSWERED}\"",
        f"- `NOT_STATED`: \"{POST_SILENT}\"",
        f"- `CANNOT_DETERMINE` (and `UNKNOWN`, `UNCERTAIN`, `UNRESOLVED`): \"{UNSETTLED}\"",
        f"- `NOT_APPLICABLE` (set by the form, never chosen): \"{DOES_NOT_APPLY}\"",
        "",
        "| Field | Question as shown | Option as shown | Stored value | The wording before |",
        "|---|---|---|---|---|",
    ]

    def cell(text: str) -> str:
        return text.replace("|", "\\|")

    for f in asked_fields():
        before_q = R.BY_NAME[f].question
        q = QUESTIONS[f] + (f" (instruction form: \"{QUESTIONS['post_label@INSTRUCTION']}\")"
                            if f == "post_label" else "")
        lines.append(f"| `{f}` | {cell(q)} | | | {cell(before_q)} |")
        if f in VALUE_CHOICE:
            lines.append(f"| | | {cell(VALUE_CHOICE[f][0])}: a box \"{cell(VALUE_CHOICE[f][1])}\" "
                         f"| the typed value | value given |")
            for c in R.VALUE_SENTINELS[f]:
                lines.append(f"| | | {cell(sentinel(f, c))} | `{c}` | "
                             f"{'not stated' if c == R.NOT_STATED else 'cannot determine'} |")
            continue
        before = dict(_before_options(f))
        codes = list(OPTIONS.get(f, {}))
        if f == "family":
            codes = ["<market>", *codes]
        for c in codes:
            if f == "family" and c == "<market>":
                lines.append("| | | the market's own name | the market's code | "
                             "`<family>`: a market of the form's list |")
                continue
            if f == "idea" and c == "SEQUENCE:":
                lines.append(f"| | | {OPTIONS['idea']['SEQUENCE:']}: `<id>` | `SEQUENCE:<id>` | "
                             f"{cell(before.get('SEQUENCE:<id>', ''))} |")
                continue
            lines.append(f"| | | {cell(OPTIONS[f][c])} | `{c}` | {cell(before.get(c, c))} |")
    lines += ["", "## Judgments of the system's answers (after the reveal)", "",
              f"Question: \"{VERDICT_QUESTION}\" (before: \"verdict on the system's <field>\").",
              "", "| Option as shown | Stored value | The wording before |", "|---|---|---|"]
    lines += [f"| {VERDICTS[c]} | `{c}` | {cell(R.VERDICT_TEXT[c])} |" for c in R.VERDICTS]
    lines += [(f"| {VERDICT_FIXED} | `{R.NOT_APPLICABLE}` | NOT_APPLICABLE (does not apply to your "
              "reading) |")]
    lines += ["", "## Sections", "", "| Group | Title as shown | The title before |", "|---|---|---|"]
    lines += [f"| {g} | {SECTIONS[g]} | {cell(t)} |" for g, t in R.GROUPS]
    lines += ["", "## What blocks a submission", "", "| Code | Message as shown | The message before |",
              "|---|---|---|"]
    lines += [f"| `{c}` | {cell(BLOCKING[c])} | {cell(m)} |" for c, m in R.BLOCKING_CODES.items()]
    lines += ["", "## Points to check (warnings)", "", ("| Code | Message as shown | The message "
              "before |"), "|---|---|---|"]
    lines += [f"| `{c}` | {cell(WARNINGS[c])} | {cell(m)} |" for c, m in R.WARNING_CODES.items()]
    lines += ["", "## Why a question is not asked", "", "| The rubric's reason | As shown |",
              "|---|---|"]
    lines += [f"| {cell(k)} | {cell(v)} |" for k, v in REASONS.items()]
    lines += ["", "## Statuses", "", "| Code | As shown |", "|---|---|"]
    lines += [f"| `{c}` | {t} |" for c, t in STATUS.items()]
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------ the reviewer guide and the reference

# The guide as the site shows it: the rubric's guide (REVIEWER_GUIDE) in the form's own words.
GUIDE = """# Reviewer guide

## Purpose and confidentiality

You read a publisher's trading posts and record what each one says. Your readings are the ground
truth the system is measured against. Nothing you save places, changes or approves a trade, a
rule, or the system's proposal of an instrument to trade. "What this means for someone following
the publisher" is not an order.

The posts are paid subscriber content, shown to you for this review only. Do not copy them, take
screenshots, forward them, or paste them into any other tool or assistant. Your access is personal
and can be withdrawn.

## Forms and questions

A form records ONE action: one thing the publisher does, reports or plans, on one position in one
market. A post with several actions has one form per action, in the order the actions first
appear: the first on form 1, the second on form 2. Closing one position and opening another are
two actions. A form left over answers "No, the post has fewer actions than forms"; an action no
form covers goes under "further actions" on the last form. "One form for the whole post" labels a
post the system did not read as an instruction: say what the post is as a whole (a trade made now,
then a plan, then a report of an earlier trade, then commentary) and list its actions as further
actions.

The questions come in groups: the action; market and instrument; the position just before this
action (from the earlier posts shown and the earlier actions of this post) and what happened to
it; the entry; stop and targets; the change; the trade idea; every number in the post; after your
reading is saved, the system's proposals; anything unclear. A question appears only once the
answer it depends on is given ("Answer … first"), and one that does not apply says so. "Example"
and "More about this question" open a short help in place; the Field reference lists every
question and every choice.

## When it happens, and the numbers

The post's own time words decide when the action happens. "Now, or just done": "a few minutes
ago", or a plain past with no time word ("Bought gold at 2,395."). "Earlier, as the post itself
says": "this morning", "overnight", or an existing order did it (stopped out). "Later, only if
something stated happens": a plan. If none fits, say the evidence doesn't settle it. An earlier
action is either first reported by this post, or an earlier post shown already reported it (then
the position before is the one after that report). "We bought gold this morning at 2,395." is an
earlier action first reported here: a position to catch up with, not an instruction.

Each number of the post is a row of "Every number in the post". Give it the first kind that
applies, in the order of the list. Context decides, never a phrase alone. A price a trade was
actually done at is "A price a trade was actually done at", even at the stop ("stopped out at
2.84"); a level named as the stop is "A stop" ("the 69.50 stop was hit, filled 69.40": 69.50 a
stop, 69.40 a price a trade was done at). Only "A price to enter at" tells subscribers a price to
trade at. If two readings remain, say the evidence doesn't settle it and flag "What a number is
can't be settled": there is no default.

## Earlier posts and doubt

Use only the earlier posts shown. Answer that no position was open only when those posts, or this
post's own words, show that none existed. When they do not settle it, say the evidence doesn't
settle it and flag "An earlier post I need isn't shown", never "no position". "The post doesn't
say" (or "The posts shown don't say") means the publisher did not state it. Whenever the evidence
does not settle an answer, say so and tick what is unclear or a flag. Never guess. If the form has
no truthful way to record a case, flag it, explain in the notes, and leave unanswered only what
cannot be answered truthfully.

## Saving, the system's answers, coming back

Your answers are kept as a draft while you work ("Draft kept at …, not submitted"); they are
submitted only when you save. If something is missing or contradicts another answer, the save is
refused, every answer stays on the page, and "Go to question" takes you to each point; points to
check need the tick "I have checked these". Once every form of a post has your own reading, the
system's answers appear beside yours. Judge each one: "Yes, the system is right", "No, my answer
is right" or "Unsure"; the form pre-sets a judgment from the comparison and says why. Change your
own answer only for evidence you missed, never because the system disagrees. Every save is kept;
nothing is overwritten. "Continue" on the dashboard takes you to your next step.
"""

# Codes in the rubric's texts (the Field reference's explanations), in the form's words. One
# choice per code, by the field where a reviewer meets it first.
_PLAIN_FIELDS = ("timing", "action", "announcement", "post_label", "follower_implication",
                 "position_state", "effect", "entry_role", "entry_exactness", "role",
                 "contract_month", "amendment_field", "idea", "component", "side",
                 "quoted_series", "traded_instrument", "ambiguities", "flags", "confidence",
                 "explicit")


def plain(text: str) -> str:
    """A text of the rubric with its codes and field names in the form's own words."""
    import re

    words: dict[str, str] = {}
    for f in reversed(_PLAIN_FIELDS):
        for code, label in OPTIONS.get(f, {}).items():
            if code and code.replace("_", "").isalpha() and code.isupper():
                words[code] = label
    words.update({R.NOT_STATED: POST_SILENT, R.CANNOT_DETERMINE: UNSETTLED,
                  R.NOT_APPLICABLE: DOES_NOT_APPLY, "NOT_READ": "not read by the system",
                  "UNCERTAIN": UNSETTLED, "UNRESOLVED": UNSETTLED, "UNKNOWN": UNSETTLED,
                  "CONFIRM": VERDICTS["CONFIRM"], "CORRECT": VERDICTS["CORRECT"],
                  "UNSURE": "Unsure", "YES": "Yes", "NO": "No", "VALUE": "a typed value"})
    names = {f: QUESTIONS[f] for f in QUESTIONS if "@" not in f}

    def code(m):
        token = m.group(0)
        return f"“{words[token]}”" if token in words else token

    def name(m):
        token = m.group(1) or m.group(2)
        return f"“{names[token]}”" if token in names else m.group(0)

    text = re.sub(r"'([a-z_]+)'|\b([a-z]+_[a-z_]+)\b", name, text)
    return re.sub(r"\b[A-Z][A-Z_]{1,}[A-Z]\b", code, text)


def applicability_table() -> tuple[list[str], list[tuple[str, list[str]]]]:
    """The rubric's "which questions apply" table, in words: (headings, rows)."""
    head = ["Questions", *(OPTIONS["action"][c] for c in R.APPLICABILITY_HEAD[1:6]),
            "No action (commentary)"]
    groups = {"A": SECTIONS["A"], "B": SECTIONS["B"], "C": SECTIONS["C"], "D": SECTIONS["D"],
              "E": SECTIONS["E"], "F": SECTIONS["F"], "G": SECTIONS["G"], "p": SECTIONS["T"],
              "H": SECTIONS["H"] + " (after your reading)", "I": SECTIONS["I"] + ", flags, "
              "confidence, notes"}
    cells = {"yes": "Asked", "no": "—", "label only": "Only on a form for the whole post"}
    rows = [(groups.get(r[0][0], r[0]), [cells.get(c, c) for c in r[1:]])
            for r in R.APPLICABILITY]
    return head, rows
