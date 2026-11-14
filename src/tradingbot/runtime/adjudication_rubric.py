"""The adjudication rubric, version 2: the ONE source of the reviewer's fields, their questions,
tooltips, permitted values and meanings, which fields apply, what the form derives, and what Save
checks (docs/ADJUDICATION_RUBRIC.md is rendered from ``RUBRIC``; a test fails when they drift).

Pure: no Streamlit, no I/O, no publisher content (every example is invented). The version-1 closed
sets of the bundle's ``ai`` block stay in ``adjudication.py``; this module holds the REVIEWER side.

A reading is a dict of field -> value. Values are strings, except ``ambiguities`` (a list of
codes), ``additional_actions`` (a list of ``{timing, action, family, side}``) and ``levels``
(the price table: ``[{row, role, exactness, contract_month, month_named}]``). Four answer states
are never interchangeable: ``""`` UNANSWERED, ``NOT_STATED``, ``CANNOT_DETERMINE`` and
``NOT_APPLICABLE`` (set by the form only).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

RUBRIC_VERSION = 2  # the records' "rubric" value; the text is version 2.3

UNANSWERED = ""
NOT_STATED = "NOT_STATED"
CANNOT_DETERMINE = "CANNOT_DETERMINE"
NOT_APPLICABLE = "NOT_APPLICABLE"
LEVELS = "levels"
INSTRUCTION, LABEL_ONLY = "INSTRUCTION", "LABEL_ONLY"
FORM_CANNOT_EXPRESS = "FORM_CANNOT_EXPRESS"
MAX_ADDITIONAL = 5
MAX_TEXT = 80
_PRICE = re.compile(r"^\d{1,9}(\.\d{1,8})?$")
_MONTH = re.compile(r"^[A-Z0-9]{1,12}$")

# ------------------------------------------------------------------ value sets (reviewer side)

COMPONENTS = ("ACTION_RECORDED_HERE", "NO_SEPARATE_ACTION")
POST_LABELS = ("ACTIONABLE", "CONDITIONAL", "RETROSPECTIVE", "COMMENTARY", "UNRESOLVED")
# Section 13.2: timing is ANSWERED per action; the label of the action follows from it.
TIMINGS = ("CURRENT", "HISTORICAL", "CONDITIONAL", "COMMENTARY", "UNCERTAIN")
LABEL_BY_TIMING = {"CURRENT": "ACTIONABLE", "HISTORICAL": "RETROSPECTIVE",
                   "CONDITIONAL": "CONDITIONAL", "COMMENTARY": "COMMENTARY",
                   "UNCERTAIN": "UNRESOLVED"}
# The whole post: the strongest label among its actions. UNRESOLVED ranks above COMMENTARY: an
# unresolved action is not "clearer" than commentary (section 13.2; see the report).
LABEL_STRENGTH = ("ACTIONABLE", "CONDITIONAL", "RETROSPECTIVE", "UNRESOLVED", "COMMENTARY")
# Section 13.4.
ANNOUNCEMENTS = ("FIRST_REPORT", "REPEAT", "CANNOT_DETERMINE")
FOLLOWER_IMPLICATIONS = ("INSTRUCTION_CANDIDATE", "RECONCILE_EXIT", "RECONCILE_POSITION",
                         "NO_NEW_INFORMATION", "WAIT_FOR_CONDITION", "NONE", "CANNOT_DETERMINE")
FOLLOWER_SENTENCE = ("This describes the publisher's message. It is not an order and "
                     "authorizes none.")
REAL_ACTIONS = ("OPEN", "ADD", "MODIFY", "REDUCE", "CLOSE")
ACTIONS = (*REAL_ACTIONS, "NONE", CANNOT_DETERMINE)
YES_NO = ("YES", "NO")
YES_NO_UNSURE = ("YES", "NO", "UNSURE")
QUOTED_SERIES = ("FUTURES", "SPOT_OR_INDEX", "ETF_OR_FUND", "STOCK", "OPTION", "OTHER",
                 NOT_STATED, CANNOT_DETERMINE)
TRADED_INSTRUMENTS = ("FUTURES", "ETF_OR_FUND", "STOCK", "OPTIONS", "CFD_OR_SPOT", "OTHER",
                      NOT_STATED, CANNOT_DETERMINE)
SIDES = ("LONG", "SHORT", CANNOT_DETERMINE)
POSITION_STATES = ("OPEN", "FLAT", CANNOT_DETERMINE)
POSITION_SIDES = ("LONG", "SHORT", CANNOT_DETERMINE)
EFFECTS = ("OPENS", "INCREASES", "REDUCES", "CLOSES", "AMENDS_PROTECTION", "NONE",
           CANNOT_DETERMINE)
# Section 3 D: the entry kinds, in the order that picks ``entry_price`` when several exist.
ENTRY_KINDS = ("INTENDED_ENTRY", "HISTORICAL_FILL", "CURRENT_MARKET", "AVERAGE_OR_COST_BASIS")
ENTRY_ROLES = (*ENTRY_KINDS, "UNRESOLVED")  # NOT_STATED: set by the form without a number
ENTRY_EXACTNESS = ("EXACT", "APPROXIMATE")
AMENDMENT_FIELDS = ("STOP", "TARGET", "OTHER")
IDEA_FIXED = ("NEW_IDEA", "SAME_AS_PREVIOUS", "UNKNOWN")  # or "SEQUENCE:<sequence_id>"
AMBIGUITIES = ("ACTION_UNCLEAR", "SIDE_UNCLEAR", "MARKET_UNCLEAR", "INSTRUMENT_UNCLEAR",
               "TIMING_UNCLEAR", "POSITION_UNCLEAR", "PRICE_ROLE_UNCLEAR",
               "SEVERAL_CANDIDATE_LEVELS", "LEVEL_BY_REFERENCE", "STOP_NOT_A_SIMPLE_LEVEL",
               "SIZE_UNCLEAR", "CONTRACT_MONTH_UNCLEAR")
FLAGS = ("MISSING_CONTEXT", "AMBIGUOUS_PRICE_SEMANTICS", "NEEDS_MORE_EVIDENCE", "CHART_NEEDED",
         FORM_CANNOT_EXPRESS)
CONFIDENCE = ("HIGH", "MEDIUM", "LOW")
# Section 6 (as amended by 14.1): the price table, roles in the order "the first that applies,
# top to bottom". HISTORICAL_FILL comes before STOP and TARGET: the price at which a trade was
# actually executed is a fill even when it was the stop or the target.
NON_PRICE_ROLES = ("NOT_A_PRICE", "OTHER_ACTION_OR_MARKET")
TABLE_ROLES = (*NON_PRICE_ROLES, "HISTORICAL_FILL", "STOP", "TARGET", "INTENDED_ENTRY",
               "CURRENT_MARKET", "AVERAGE_OR_COST_BASIS", "OTHER_CONTRACT_MONTH", "CHART_LEVEL",
               "UNRESOLVED")
ROW_EXACTNESS = ("EXACT", "APPROXIMATE")
ROW_MONTHS = ("FRONT", "NEXT", "NAMED", NOT_STATED)
ROW_KEYS = ("role", "exactness", "contract_month", "month_named")
MONTH_SERIES = ("FUTURES", CANNOT_DETERMINE)  # quoted series for which the month is asked
# Section 5: the compact record of an action no form covers.
ADDITIONAL_TIMINGS = ("CURRENT", "HISTORICAL", "CONDITIONAL", "UNCERTAIN")
ADDITIONAL_ACTIONS = (*REAL_ACTIONS, CANNOT_DETERMINE)
ADDITIONAL_KEYS = ("timing", "action", "family", "side")
ACTION_LABELS = ("ACTIONABLE", "CONDITIONAL", "RETROSPECTIVE")  # label-only: actions required
OPTIONAL_ACTION_LABELS = ("UNRESOLVED",)  # label-only: actions offered, optional (14.5)
# Section 8: verdicts on the SYSTEM's value (14.7: every option carries its meaning).
VERDICTS = ("CONFIRM", "CORRECT", "UNSURE")
VERDICT_NOT_APPLICABLE = NOT_APPLICABLE
VERDICT_TEXT = {"CONFIRM": "CONFIRM: the system's value is right",
                "CORRECT": "CORRECT: the system's value is wrong, mine is right",
                "UNSURE": "UNSURE"}
VERDICT_HELP = ("A verdict on the SYSTEM's value, never on yours. CONFIRM: the system's value "
                "is right. CORRECT: the system's value is wrong and your reading gives the right "
                "one. UNSURE: you cannot tell. Pre-filled from the comparison; editable.")
DECISION_TEXT = {"ACCEPT": "ACCEPT: the reviewer's reading is right as it stands",
                 "AMEND": "AMEND: some fields are wrong; I change only those",
                 "RETURN": "RETURN: send it back to the reviewer"}
DECISION_HELP = ("Your decision on the reviewer's reading. ACCEPT freezes it as it stands. AMEND "
                 "changes only the fields you correct (what follows from them is derived again, "
                 "and a field that changes kind must be answered again). RETURN sends it back "
                 "to the reviewer. Nothing here approves a trade, a rule or a mapping.")
ADDITIONAL_HELP = {
    "timing": "When this further action happens relative to the post (as the field 'timing').",
    "action": "What the publisher does in this further action (as the field 'action').",
    "family": "The market of this further action (as the field 'family').",
    "side": "The direction of the position this further action concerns (as the field 'side').",
}
INFERRED_NOTE = "inferred from context: not counted as executable"
# Values only the form sets (never offered to a reviewer or an approver; section 14.2).
FORM_ONLY = {"effect": ("NONE",)}

# Section 3 C: the allowed (action, effect) pairs.
ALLOWED_EFFECTS = {"OPEN": ("OPENS", "INCREASES"), "ADD": ("INCREASES", "OPENS"),
                   "MODIFY": ("AMENDS_PROTECTION",), "REDUCE": ("REDUCES", "CLOSES"),
                   "CLOSE": ("CLOSES", "REDUCES"), "NONE": ("NONE",)}

# The fields of a reading, in form order (groups A to I; the price table after G).
INSTRUCTION_FIELDS = (
    "component", "timing", "post_label", "announcement", "action", "explicit",
    "follower_implication",
    "family", "instrument_as_read", "quoted_series", "traded_instrument",
    "side", "position_state", "position_side", "position_last_stop", "effect",
    "entry_price", "entry_role", "entry_exactness",
    "stop_price", "targets",
    "amendment_field", "amendment_to", "amendment_from",
    "idea",
    "reference_series_right", "execution_instrument_right",
    "ambiguities", "additional_actions")
LABEL_ONLY_FIELDS = ("post_label", "ambiguities", "additional_actions")
LIST_FIELDS = ("ambiguities", "additional_actions")
# Fields that carry no verdict (section 8).
NOT_COMPARED = ("component", "announcement", "follower_implication", "quoted_series",
                "traded_instrument", "ambiguities", "additional_actions",
                "reference_series_right", "execution_instrument_right")
POSITION_FIELDS = ("position_state", "position_side", "position_last_stop")
MAPPING_FIELDS = ("reference_series_right", "execution_instrument_right")
# Free-value kinds: which sentinels a reviewer may choose beside a typed value.
PRICE_FIELDS = ("position_last_stop", "entry_price", "stop_price")
VALUE_SENTINELS = {
    "instrument_as_read": (NOT_STATED, CANNOT_DETERMINE),
    "position_last_stop": (NOT_STATED, CANNOT_DETERMINE),
    "entry_price": (NOT_STATED, CANNOT_DETERMINE),
    "stop_price": (NOT_STATED, CANNOT_DETERMINE),
    "targets": (NOT_STATED, CANNOT_DETERMINE),
    "amendment_to": (CANNOT_DETERMINE,),
    "amendment_from": (NOT_STATED, CANNOT_DETERMINE),
}


def fields_for(kind: str) -> tuple[str, ...]:
    return LABEL_ONLY_FIELDS if kind == LABEL_ONLY else INSTRUCTION_FIELDS


def compared_fields(kind: str, *, position_compared: bool = True) -> tuple[str, ...]:
    """The fields carrying a verdict. Section 13.1: on a form whose post has an earlier form in
    the same market the position fields are shown beside the system's but not compared."""
    return tuple(f for f in fields_for(kind) if f not in NOT_COMPARED and (
        position_compared or f not in POSITION_FIELDS))


# ------------------------------------------------------------------ the rubric table


@dataclass(frozen=True)
class Field:
    name: str
    group: str
    question: str
    tooltip: str
    values: tuple[tuple[str, str], ...] = ()  # (code, one-line meaning)
    kind: str = "choice"  # choice | family | idea | price | prices | text | codes | records | derived
    applies: str = ""
    examples: tuple[str, ...] = ()
    set_by_form: str = ""


GROUPS = (
    ("A", "What kind of statement"),
    ("B", "Market and instrument (source facts)"),
    ("C", "Direction, position BEFORE this action, effect"),
    ("D", "Entry (OPEN and ADD only)"),
    ("E", "Stop and targets (OPEN, ADD, MODIFY, REDUCE)"),
    ("F", "Amendment (MODIFY only)"),
    ("G", "Idea"),
    ("T", "The price table"),
    ("H", "The system's mapping (asked only after the reveal; advisory)"),
    ("I", "Ambiguities, flags, confidence, notes"),
)

_VALUE_STATES = ((NOT_STATED, "the publisher did not state it"),
                 (CANNOT_DETERMINE, ("it probably exists, but the evidence shown does not settle "
                                    "it (add an ambiguity or a flag)")))

# Section 13.3: no phrase has a fixed role; context decides.
PRICE_QUESTIONS = ("Ask of the number: is the publisher telling subscribers a price to transact "
                   "at (INTENDED_ENTRY)? Reporting the price they themselves got "
                   "(HISTORICAL_FILL)? Saying where the market is (CURRENT_MARKET)? Giving the "
                   "average of several entries (AVERAGE_OR_COST_BASIS)? Pointing at a level on "
                   "the chart (CHART_LEVEL)? Setting a stop or a target (STOP, TARGET)?")
CUE_WORDS = ("Cue words are indications only, never rules: \"bought at\", \"filled\", \"long "
             "from\" often report a fill; \"buy at\", \"limit\", \"bids\", \"the order is "
             "working\" often give an order price; \"trading at\", \"now at\", \"here at\" "
             "often describe the market. Context decides: order words, tense, what surrounds the "
             "number, the chart, the earlier posts shown, how the post speaks of its other "
             "numbers. If two readings remain: UNRESOLVED with AMBIGUOUS_PRICE_SEMANTICS, never "
             "a guess.")

RUBRIC: tuple[Field, ...] = (
    Field("component", "A", "Does this form record an action of the post?",
          "Asked only when the post has more than one form. Actions are recorded in the order "
          "they first appear in the post: the first on form 1, the second on form 2. A surplus "
          "form answers NO_SEPARATE_ACTION and nothing else except confidence and notes.",
          (("ACTION_RECORDED_HERE", "this form records one action of the post"),
           ("NO_SEPARATE_ACTION", "the post has fewer actions than forms; this form is surplus")),
          applies="INSTRUCTION forms of a post with more than one form; set to "
                  "ACTION_RECORDED_HERE by the form when the post has one form",
          examples=("A post with two forms and one action: form 2 is NO_SEPARATE_ACTION.",)),
    Field("timing", "A", "When does THIS action happen relative to the post?",
          "Answered per action. The post's own time words decide, not the time that has passed. "
          "CURRENT: the post presents the action as happening now or just done: present tense, "
          "\"now\", \"just\", \"here\", \"a few minutes ago\", or a plain past with no time "
          "placed on it (\"Bought gold at 2,395.\"). HISTORICAL: the post itself places the "
          "action at an earlier time than the post (\"this morning\", \"earlier today\", "
          "\"overnight\", \"yesterday\", \"last week\"), or the action was done by an existing "
          "order (a stop-out, a target hit). A plain past with no time word is CURRENT: "
          "\"Bought gold at 2,395.\" is CURRENT. Neither: UNCERTAIN with TIMING_UNCLEAR. "
          "\"Bought gold at 2,395 a few minutes ago.\" is CURRENT; \"We bought gold this morning "
          "at 2,395.\" is HISTORICAL (a first report: a position to reconcile, not an "
          "instruction). UNCERTAIN needs an action (no action is COMMENTARY). Timing says "
          "nothing about how directly the action is worded (that is 'explicit'). A post-level "
          "label never sets it.",
          (("CURRENT", "the post presents the action as happening now or just done"),
           ("HISTORICAL", ("the post places the action earlier than the post, or an existing "
                           "order did it")),
           ("CONDITIONAL", "it will happen only if or when something stated happens later"),
           ("COMMENTARY", "no action (the form sets action NONE)"),
           ("UNCERTAIN", ("the evidence does not settle it (add TIMING_UNCLEAR); needs an "
                          "action"))),
          applies="every instruction form that records an action",
          examples=("\"Bought gold at 2,395 a few minutes ago.\" CURRENT",
                    ("\"We bought gold this morning at 2,395.\" HISTORICAL, FIRST_REPORT: a "
                     "position to reconcile, not an instruction"),
                    "\"Bought gold at 2,395.\" CURRENT (a plain past with no time word)",
                    "\"Bought the S&P at 5,012 a few minutes ago.\" CURRENT (a fresh fill)",
                    "\"We were stopped out overnight at 2.84.\" HISTORICAL",
                    "\"If silver closes above 31.50 we go long.\" CONDITIONAL",
                    "\"Still long, stop unchanged at 70.\" COMMENTARY")),
    Field("post_label", "A", "Label of this action (instruction form) / of the whole post "
                             "(label-only form)",
          "On an instruction form it follows from the timing and is shown read-only: CURRENT "
          "gives ACTIONABLE, HISTORICAL gives RETROSPECTIVE, CONDITIONAL gives CONDITIONAL, "
          "COMMENTARY gives COMMENTARY, UNCERTAIN gives UNRESOLVED. A label-only form labels a "
          "whole post rather than one action (a post the system did not read as an "
          "instruction): there you answer the label of the WHOLE post, the strongest label "
          "among its actions, in this order: ACTIONABLE, CONDITIONAL, RETROSPECTIVE, "
          "UNRESOLVED, COMMENTARY. List the post's actions as further actions (required for "
          "ACTIONABLE, CONDITIONAL and RETROSPECTIVE, optional for UNRESOLVED); the label must "
          "equal the strongest label of the actions you list.",
          (("ACTIONABLE", "an action announced as the publisher acts (timing CURRENT)"),
           ("CONDITIONAL", "an intention that depends on something later"),
           ("RETROSPECTIVE", "a report of an action completed earlier or by an existing order"),
           ("COMMENTARY", ("no trading action: analysis, levels, views, updates that change "
                          "nothing")),
           ("UNRESOLVED", "the evidence does not settle it (add an ambiguity)")),
          applies="every form; answered on label-only forms only",
          set_by_form="on instruction forms, from the timing"),
    Field("announcement", "A", "Is this post the FIRST to tell subscribers of this action?",
          "Asked when the timing is HISTORICAL. FIRST_REPORT: this post is the first to tell "
          "subscribers: the publisher's position changes with it (the effect is answered and "
          "cannot be NONE). REPEAT: an earlier post shown already announced or reflected it (a "
          "recap); the form then sets the effect to NONE. Not compared with the system.",
          (("FIRST_REPORT", "the first report: the position changes with it"),
           ("REPEAT", "an earlier post shown already announced or reflected it"),
           (CANNOT_DETERMINE, "the posts shown do not settle it")),
          applies="timing HISTORICAL",
          set_by_form=("FIRST_REPORT for CURRENT; CANNOT_DETERMINE for UNCERTAIN; NOT_APPLICABLE "
                       "for CONDITIONAL and COMMENTARY"),
          examples=(("\"Recap: last week we closed crude at 74.90\" after the close was announced: "
                    "REPEAT"),)),
    Field("follower_implication", "A", "What this means for a follower",
          FOLLOWER_SENTENCE + " Derived, read-only. With explicit NO the line adds \""
          + INFERRED_NOTE + "\".",
          (("INSTRUCTION_CANDIDATE", "timing CURRENT"),
           ("RECONCILE_EXIT", ("HISTORICAL, FIRST_REPORT, CLOSE or REDUCE: the publisher is "
                              "already out (or smaller)")),
           ("RECONCILE_POSITION", ("HISTORICAL, FIRST_REPORT, OPEN, ADD or MODIFY: the position "
                                  "or protection changed without a real-time announcement")),
           ("NO_NEW_INFORMATION", "HISTORICAL, REPEAT"),
           ("WAIT_FOR_CONDITION", "CONDITIONAL"), ("NONE", "COMMENTARY"),
           (CANNOT_DETERMINE, "timing UNCERTAIN or announcement CANNOT_DETERMINE")),
          kind="derived", applies="every instruction form that records an action",
          set_by_form="from timing, announcement and action"),
    Field("action", "A", "What does the publisher announce?",
          "Taken from the publisher's WORDS. With CONDITIONAL and HISTORICAL timing the action "
          "is the one that would be, or was, done.",
          (("OPEN", "starts a position (going long, initiating, new short, bought)"),
           ("ADD", "increases an existing position on the same side"),
           ("MODIFY", "changes the stop or a target without changing the size"),
           ("REDUCE", "closes part of a position (trimming, half off)"),
           ("CLOSE", "exits the whole position, or reports that it was closed"),
           ("NONE", "no trading action (set by the form for timing COMMENTARY)"),
           (CANNOT_DETERMINE, "only with timing UNCERTAIN; notes required")),
          applies="every instruction form that records an action",
          examples=("\"Trimming half here.\" REDUCE", "\"Raising the stop to 70.\" MODIFY")),
    Field("explicit", "A", "Do the post's own words state the action?",
          "YES when the post's words state the action, in any wording or tense. NO when the "
          "reviewer infers it from context. Test: could a reader who sees only this post and "
          "its chart, knowing the publisher's usual vocabulary, tell that the publisher does or "
          "did this action? It has nothing to do with whether a price is given. An action with "
          "explicit NO is not counted as executable ground truth.",
          (("YES", "the words state the action (\"long\", \"out\", \"stopped\")"),
           ("NO", "inferred from context, not stated")),
          applies="OPEN, ADD, MODIFY, REDUCE, CLOSE, CANNOT_DETERMINE",
          examples=("\"Closing this here.\" YES (the market may come from context)",
                    "\"Nothing left on in crude after today.\" NO")),
    Field("family", "B", "Which market does the action concern?",
          "One of the markets listed in the form, OTHER (a market outside the list) or UNKNOWN "
          "(cannot determine). From this post, or from the earlier posts shown when this post "
          "does not name it.",
          (("<family>", "a market of the form's list"), ("OTHER", "a market outside the list"),
           ("UNKNOWN", "cannot determine")),
          kind="family", applies="OPEN, ADD, MODIFY, REDUCE, CLOSE"),
    Field("instrument_as_read", "B", "How does this post name the instrument?",
          "The publisher's own words for the instrument in THIS post, as short as possible. "
          "Never a ticker you supply.",
          (("<text>", "the post's own words, at most 80 characters"), *_VALUE_STATES),
          kind="text", applies="OPEN, ADD, MODIFY, REDUCE, CLOSE",
          examples=("\"Dec gold\"", "\"S&P futures\"", "NOT_STATED when the post names none")),
    Field("quoted_series", "B", "In which kind of price series are this action's numbers quoted?",
          "According to the post, its chart labels or the earlier posts shown. The size of a "
          "number is not evidence. A source fact, not a trading decision.",
          (("FUTURES", "a futures contract"), ("SPOT_OR_INDEX", "a spot price or an index"),
           ("ETF_OR_FUND", "an exchange-traded fund or other fund"), ("STOCK", "a share"),
           ("OPTION", "an option premium"), ("OTHER", "another kind of series"),
           (NOT_STATED, "no evidence, or no numbers"),
           (CANNOT_DETERMINE, "conflicting evidence")),
          applies="OPEN, ADD, MODIFY, REDUCE, CLOSE",
          examples=("\"Long December natural gas at 3.42\" FUTURES",)),
    Field("traded_instrument", "B", "What does the publisher SAY they traded?",
          "A chart of futures prices is not a statement that futures were traded: without words "
          "saying what was bought or sold, the answer is NOT_STATED. What TradingBot would trade "
          "is a separate policy decision.",
          (("FUTURES", "futures"), ("ETF_OR_FUND", "a fund"), ("STOCK", "shares"),
           ("OPTIONS", "options"), ("CFD_OR_SPOT", "a CFD or the spot market"),
           ("OTHER", "another instrument"), (NOT_STATED, "the post does not say"),
           (CANNOT_DETERMINE, "conflicting statements")),
          applies="OPEN, ADD, MODIFY, REDUCE, CLOSE",
          examples=("\"Bought the 5,000/5,100 call spread\" OPTIONS",)),
    Field("side", "C", "What is the direction of the POSITION the action concerns?",
          "Not the direction of the order: closing a long is a sale and its side is LONG; "
          "covering a short has side SHORT. From this post or the earlier posts shown.",
          (("LONG", "a long position"), ("SHORT", "a short position"),
           (CANNOT_DETERMINE, "the evidence does not settle it")),
          applies="OPEN, ADD, MODIFY, REDUCE, CLOSE",
          examples=("\"Covered the gold short.\" SHORT",)),
    Field("position_state", "C", "Immediately BEFORE this action, was a position open? (FLAT "
                                 "only when the posts establish that none existed)",
          "BEFORE this action: as established by the publisher's own account, the earlier "
          "posts shown AND the earlier actions of this same post, in text order (this post may "
          "restate it: \"our long from 70, stop was 68\"). FLAT: they establish that NO "
          "position exists before this action (it was closed, or this action begins the idea). "
          "CANNOT_DETERMINE: the posts shown do not settle it; \"the posts shown do not show a "
          "position\" is CANNOT_DETERMINE with the flag MISSING_CONTEXT, never FLAT. For a "
          "FIRST_REPORT of an exit the position before is still OPEN (the earlier posts did not "
          "know of the exit). For a REPEAT (a recap of an action an earlier post already "
          "announced) the position BEFORE this action is the position as the earlier posts "
          "shown already leave it, that is AFTER the earlier announcement (a recap of a close "
          "announced last week: FLAT). The position AFTER this action is shown separately.",
          (("OPEN", ("a position was open in this market before this action (for a "
                     "FIRST_REPORT of an exit: still OPEN)")),
           ("FLAT", ("the publisher's own account or the earlier posts shown establish that NO "
                     "position exists before this action (it was closed, or this action begins "
                     "the idea)")),
           (CANNOT_DETERMINE, ("the posts shown do not settle it; \"the posts shown do not show "
                               "a position\" is this, with the flag MISSING_CONTEXT, never "
                               "FLAT"))),
          applies="OPEN, ADD, MODIFY, REDUCE, CLOSE",
          examples=(("Form 2 of \"Closed the crude long ... we are now short crude\": FLAT "
                    "(after form 1's close)"),)),
    Field("position_side", "C", "BEFORE this action, on which side was that position?",
          "BEFORE this action (earlier posts shown and earlier actions of this post; for a "
          "REPEAT, as the earlier posts shown already leave it). NOT_APPLICABLE when the "
          "position before this action was FLAT (set by the form).",
          (("LONG", "long"), ("SHORT", "short"),
           (CANNOT_DETERMINE, "the posts shown do not settle it")),
          applies="when the position before this action was OPEN or CANNOT_DETERMINE"),
    Field("position_last_stop", "C", "BEFORE this action, what was the last stop "
                                     "communicated?",
          "The most recent stop the publisher had communicated for that position BEFORE this "
          "action (earlier posts shown and earlier actions of this post; for a REPEAT, as the "
          "earlier posts shown already leave it; this post may restate it: \"our 72 stop was "
          "never touched\"). 'stop_price' is the stop this post communicates; this is the stop "
          "that was in force before this action.",
          (("<number>", "the stop"), (NOT_STATED, ("open, but no stop communicated in the posts "
                                                  "shown")),
           (CANNOT_DETERMINE, "the posts shown do not settle it")),
          kind="price", applies="when the position before this action was OPEN or "
                                "CANNOT_DETERMINE"),
    Field("effect", "C", "How does this action change the position BEFORE this action?",
          "The action comes from the words; the effect from the position. For CONDITIONAL: the "
          "effect the action would have. Allowed pairs: OPEN with OPENS or INCREASES; ADD with "
          "INCREASES or OPENS; MODIFY with AMENDS_PROTECTION (set by the form; NONE from FLAT); "
          "REDUCE with REDUCES or CLOSES; CLOSE with CLOSES or REDUCES; CANNOT_DETERMINE with "
          "any action. NONE is never chosen: the form sets it only for announcement REPEAT (the "
          "position before already reflects it), for timing COMMENTARY, and for a MODIFY from "
          "FLAT (a change to an order that is not yet a position: \"moving our buy limit from "
          "70 to 69.50\"); a first report changes the position. Except for a CONDITIONAL "
          "action, the effect must be possible from the position before: from FLAT it cannot "
          "be INCREASES, REDUCES, CLOSES or AMENDS_PROTECTION; from a position open on the same "
          "side it cannot be OPENS. If the earlier posts shown do not show the position, the "
          "position before is CANNOT_DETERMINE with the flag MISSING_CONTEXT, not FLAT.",
          (("OPENS", "flat to a position"), ("INCREASES", "a larger position"),
           ("REDUCES", "smaller, still open"), ("CLOSES", "to flat"),
           ("AMENDS_PROTECTION", "size unchanged; stop or target changed"),
           ("NONE", ("no change: set by the form only, for a REPEAT, for COMMENTARY and for a "
                     "MODIFY from FLAT (never chosen)")),
           (CANNOT_DETERMINE, "context missing (flag MISSING_CONTEXT)")),
          applies="OPEN, ADD, MODIFY, REDUCE, CLOSE",
          set_by_form="NONE for announcement REPEAT, for action NONE and for a MODIFY from FLAT; "
                      "AMENDS_PROTECTION for any other MODIFY",
          examples=(("\"Adding to our long\" when the posts shown do not show the position: "
                     "position CANNOT_DETERMINE with MISSING_CONTEXT, effect INCREASES; ADD with "
                     "OPENS only when the posts shown establish that the position had been "
                     "closed"),
                    ("\"Moving our buy limit from 70 to 69.50\" with nothing filled yet: MODIFY "
                     "from FLAT, effect NONE (set by the form), warning ACTS_ON_FLAT"))),
    Field("entry_price", "D", "Which ONE number does the post tie to the entry of this action?",
          "Whatever kind of number it is; 'entry_role' says which kind. It is NOT by itself an "
          "order price. When several numbers relate to the entry, take the first that exists in "
          "this order: the instructed price, the reported fill, the market observation, the "
          "average. This order and the role order of the price table answer different "
          "questions: this one, which number is the entry reference; the table's, what kind of "
          "number a row is. Never copy a number from a chart or an earlier post into it. A level that "
          "must be confirmed first (\"if it CLOSES above 31.50\") is not the entry price.",
          (("<number>", "the entry number"), *_VALUE_STATES), kind="price",
          applies="OPEN, ADD",
          examples=("\"We are buying natural gas now.\" NOT_STATED",
                    "\"Will buy silver at 30.80 on a pullback.\" 30.80")),
    Field("entry_role", "D", "What kind of number is the entry price?",
          PRICE_QUESTIONS + " " + CUE_WORDS + " A freshly reported completed fill (\"bought at "
          "5,012 a few minutes ago\") can open a position (timing CURRENT) while its price stays "
          "HISTORICAL_FILL: a fact about the publisher's execution, never a limit instruction. "
          "Only INTENDED_ENTRY is a price instruction.",
          (("INTENDED_ENTRY", ("a price the publisher tells subscribers to transact at (only "
                              "this role is a price instruction)")),
           ("HISTORICAL_FILL", "the price the publisher says they themselves got"),
           ("CURRENT_MARKET", "where the market is as the publisher acts or writes"),
           ("AVERAGE_OR_COST_BASIS", "the average price of the position after several entries"),
           ("UNRESOLVED", ("the context leaves two readings (flag AMBIGUOUS_PRICE_SEMANTICS); "
                          "there is no default role")),
           (NOT_STATED, "set by the form when the entry price is NOT_STATED")),
          applies="OPEN, ADD, with an entry number or an entry price CANNOT_DETERMINE (the kind "
                  "of entry can be known when the level is not: \"bids in at yesterday's low\" "
                  "is INTENDED_ENTRY with LEVEL_BY_REFERENCE)",
          set_by_form="NOT_STATED when the entry price is NOT_STATED",
          examples=("\"Going long crude at 71.20.\" and nothing else: UNRESOLVED",
                    "\"Going long crude at 71.20, the order is working.\" INTENDED_ENTRY",
                    "\"Going long crude at 71.20, filled on the open.\" HISTORICAL_FILL")),
    Field("entry_exactness", "D", "How does the publisher express the entry number?",
          "How the number is expressed, never how close it is to the market and never your "
          "confidence. A round number without a hedge is EXACT.",
          (("EXACT", "a precise figure with no hedge"),
           ("APPROXIMATE", "hedged (around, about, near, ~), an end of a range, or shorthand"),
           (NOT_APPLICABLE, "set by the form without a number")),
          applies="OPEN, ADD, with an entry number",
          set_by_form="NOT_APPLICABLE without a number (NOT_STATED or CANNOT_DETERMINE)"),
    Field("stop_price", "E", "Which stop does THIS post communicate for the position after "
                             "this action?",
          "New, amended or restated (\"stop stays 69.50\"). NOT_STATED when this post "
          "communicates none: never carry one over from elsewhere. CANNOT_DETERMINE when a stop "
          "is referred to without a readable level (add LEVEL_BY_REFERENCE). For a MODIFY of "
          "the stop it is set by the form from 'amendment_to'.",
          (("<number>", "the stop"), *_VALUE_STATES), kind="price",
          applies="OPEN, ADD, MODIFY, REDUCE", set_by_form="for MODIFY of the STOP: amendment_to",
          examples=("\"Stop goes below yesterday's low.\" CANNOT_DETERMINE",)),
    Field("targets", "E", "Which profit targets does this post communicate?",
          "In the order stated, separated by commas. On a MODIFY of a target: the targets in "
          "force after the change (they must contain the new value when it is a number).",
          (("<numbers>", "the targets, comma-separated"), *_VALUE_STATES), kind="prices",
          applies="OPEN, ADD, MODIFY, REDUCE"),
    Field("amendment_field", "F", "What does the MODIFY change?",
          "A closed choice (version 1 asked for a typed code).",
          (("STOP", "the stop"), ("TARGET", "a profit target"), ("OTHER", "something else")),
          applies="MODIFY"),
    Field("amendment_to", "F", "What is the new value?",
          "For STOP or TARGET a number, or CANNOT_DETERMINE. \"Stop to breakeven\": the number "
          "only when the earlier posts shown state the publisher's own entry as a fill or cost "
          "basis, with the ambiguity LEVEL_BY_REFERENCE; otherwise CANNOT_DETERMINE with that "
          "ambiguity. For OTHER, a short text.",
          (("<number or text>", "the new value"),
           (CANNOT_DETERMINE, "no readable value (add an ambiguity)")),
          kind="text", applies="MODIFY"),
    Field("amendment_from", "F", "Which value does it replace?",
          "For STOP it is 'position_last_stop', set by the form. For TARGET or OTHER: as stated "
          "in this post or the earlier posts shown.",
          (("<number or text>", "the replaced value"), *_VALUE_STATES), kind="text",
          applies="MODIFY", set_by_form="for STOP: position_last_stop"),
    Field("idea", "G", "Which idea does this action belong to?",
          "An idea is one position from its opening to its final close. The other listed ideas "
          "are named by their identifier (SEQUENCE:<id>).",
          (("NEW_IDEA", "this action starts one (normally an OPEN from flat)"),
           ("SAME_AS_PREVIOUS", "it continues the idea of the earlier posts shown"),
           ("SEQUENCE:<id>", "it belongs to another listed idea; choose it only when sure"),
           ("UNKNOWN", "cannot tell")),
          kind="idea", applies="OPEN, ADD, MODIFY, REDUCE, CLOSE"),
    Field("role", "T", "What is this number?",
          "One row per number of the post, in text order (FULL access), or, in restricted "
          "access (no post text), one row per level the SYSTEM typed. Every occurrence is its "
          "own row. The FIRST role that applies, top to bottom. The price at which a trade was "
          "actually executed is HISTORICAL_FILL even when it was the stop or the target "
          "(\"stopped out at 2.84\", \"took profits at 75\"). A level the post names AS the "
          "stop or the target, without giving it as the price obtained, is STOP or TARGET "
          "(\"the 69.50 stop was hit, filled 69.40\": 69.50 STOP, 69.40 HISTORICAL_FILL). "
          "\"Stopped out at 2.84\" gives the price obtained (HISTORICAL_FILL); \"our 69.50 "
          "stop was hit\" names the stop level (STOP). Context still decides. The function wins over the description: a level used AS the "
          "stop is STOP; \"stop below support at 70\" leaves 70 a CHART_LEVEL and stop_price "
          "CANNOT_DETERMINE. A STOP row needs the stop in stop_price (a number or "
          "CANNOT_DETERMINE), position_last_stop or amendment_from. This role order and the "
          "order in which 'entry_price' is chosen answer different questions: this one, what "
          "kind of number a row is; that one, which number is the entry reference. "
          + PRICE_QUESTIONS + " " + CUE_WORDS,
          (("NOT_A_PRICE", ("not a price of any market: an indicator reading, a percentage, a "
                           "date, a size, a profit, the period of a moving average")),
           ("OTHER_ACTION_OR_MARKET", ("a price of another market, or of another action than "
                                      "this form records")),
           ("HISTORICAL_FILL", ("a price at which a trade was actually executed, even at the "
                                "stop or the target (\"closed at 74.90\", \"stopped out at "
                                "2.84\")")),
           ("STOP", ("a level named AS a stop of this position, not as the price obtained: "
                     "new, amended, restated, the one in force or the one replaced")),
           ("TARGET", ("a level named AS a profit target of this position, not as the price "
                       "obtained: new, restated, reached or the one replaced")),
           ("INTENDED_ENTRY", "a price the publisher instructs or plans to transact at"),
           ("CURRENT_MARKET", "where the market is (\"closing here, market 2,395\")"),
           ("AVERAGE_OR_COST_BASIS", "the average price of the position"),
           ("OTHER_CONTRACT_MONTH", ("a price of another contract month, given only as a "
                                    "reference")),
           ("CHART_LEVEL", ("support, resistance, a prior high or low, a moving-average value, "
                           "a trigger level")),
           ("UNRESOLVED", ("a price of this market whose role cannot be settled (flag "
                          "AMBIGUOUS_PRICE_SEMANTICS)"))),
          applies="OPEN, ADD, MODIFY, REDUCE, CLOSE",
          examples=(("\"Crude bounced from 70 last week. Our stop goes to 70.\" row 1 of 2 "
                    "CHART_LEVEL, row 2 of 2 STOP"),
                    "\"We were stopped out overnight at 2.84.\" 2.84 HISTORICAL_FILL",
                    ("\"Stopped out: the 69.50 stop was hit, filled 69.40.\" 69.50 STOP, 69.40 "
                     "HISTORICAL_FILL"),
                    "\"RSI at 31\" NOT_A_PRICE")),
    Field("exactness", "T", "How is this number expressed?",
          "How the publisher expresses it, never how close it is to the market.",
          (("EXACT", "a precise figure with no hedge (a round number without a hedge is EXACT)"),
           ("APPROXIMATE", ("hedged (around, about, near, roughly, area, zone, ~), an end of a "
                           "range, or rounded shorthand (4.5k)")),
           (NOT_APPLICABLE, "set by the form for NOT_A_PRICE and OTHER_ACTION_OR_MARKET")),
          applies="every row with a price role"),
    Field("contract_month", "T", "Which contract month is this number quoted for?",
          "Asked only when quoted_series is FUTURES or CANNOT_DETERMINE. Assign a month only on "
          "evidence available at the time of the post: never from the post's date, from the "
          "contracts listed today or from the price level. A stop, target or entry merely "
          "expressed in another month keeps its own role and says so here.",
          (("FRONT", "front, nearest or spot month"), ("NEXT", "next or second month"),
           ("NAMED", "a named month or contract code (type it in month_named)"),
           (NOT_STATED, "nothing in the material identifies the month"),
           (NOT_APPLICABLE, "set by the form when not asked")),
          applies="price rows, when quoted_series is FUTURES or CANNOT_DETERMINE"),
    Field("month_named", "T", "Which month or code is named?",
          "At most 12 letters and digits, such as DEC or Z6.",
          (("<code>", "the month or contract code as named"),), kind="text",
          applies="rows with contract_month NAMED"),
    Field("reference_series_right", "H", "Is the system's reference series the price series "
                                         "this post's numbers are quoted in?",
          "The system's mapping is its proposal of which price series and which instrument "
          "would follow this idea; the reference series is the price series it reads the "
          "numbers against. A check of a source fact. Not part of the independent reading.",
          (("YES", "yes"), ("NO", "no"), ("UNSURE", "cannot tell")),
          applies="after the reveal, when the system's mapping names a reference series"),
    Field("execution_instrument_right", "H", "Would the instrument the system PROPOSES be a "
                                             "faithful way to follow this idea?",
          "The system's mapping is its proposal of which instrument would follow this idea. "
          "Your opinion on a policy proposal. It approves nothing: approving a mapping is the "
          "owner's separate decision.",
          (("YES", "yes"), ("NO", "no"), ("UNSURE", "cannot tell")),
          applies="after the reveal, when the system's mapping proposes an instrument"),
    Field("ambiguities", "I", "What in the PUBLISHER'S message is unclear?",
          "A closed multiple choice; empty means none. Your own record: shown beside the "
          "system's codes after the reveal, with no verdict.",
          (("ACTION_UNCLEAR", "which action"), ("SIDE_UNCLEAR", "which direction"),
           ("MARKET_UNCLEAR", "which market"), ("INSTRUMENT_UNCLEAR", "which instrument"),
           ("TIMING_UNCLEAR", "announcement, report or plan"),
           ("POSITION_UNCLEAR", "which position or idea"),
           ("PRICE_ROLE_UNCLEAR", "what a number is"),
           ("SEVERAL_CANDIDATE_LEVELS", "several numbers could be the level"),
           ("LEVEL_BY_REFERENCE", "a level given by reference: breakeven, entry, \"the lows\""),
           ("STOP_NOT_A_SIMPLE_LEVEL", "a stop on a close, a mental stop"),
           ("SIZE_UNCLEAR", "how much"), ("CONTRACT_MONTH_UNCLEAR", "which contract month")),
          kind="codes", applies="every form"),
    Field("additional_actions", "I", "Which further actions does the post contain that no "
                                     "form covers?",
          "At most 5 compact records {timing, action, family, side}; details go in notes. "
          "Offered on the LAST form of a post, and on a label-only form whose label is "
          "ACTIONABLE, CONDITIONAL, RETROSPECTIVE (there at least one record is required, "
          "unless FORM_CANNOT_EXPRESS is set) or UNRESOLVED (optional; a record may have timing "
          "UNCERTAIN). On a label-only form the post label must equal the strongest label of "
          "the records, in the order ACTIONABLE, CONDITIONAL, RETROSPECTIVE, UNRESOLVED, "
          "COMMENTARY. Never merge two actions into one form.",
          (("{timing, action, family, side}", ("one record per action; timing CURRENT, "
                                              "HISTORICAL, CONDITIONAL or UNCERTAIN")),),
          kind="records", applies="the last form of a post; label-only forms labelled "
                                  "ACTIONABLE, CONDITIONAL, RETROSPECTIVE or UNRESOLVED",
          examples=(("\"Sold half our silver at 32.10.\" on a label-only form: ACTIONABLE plus "
                    "{CURRENT, REDUCE, SILVER, LONG}"),)),
    Field("flags", "I", "What does the REVIEW need?",
          "Flags are about the review, ambiguities about the message. FORM_CANNOT_EXPRESS "
          "lifts only the rule that every field be answered: leave unanswered what cannot be "
          "answered truthfully; every check between the answers you give still applies.",
          (("MISSING_CONTEXT", "an earlier post needed to answer is not among those shown"),
           ("AMBIGUOUS_PRICE_SEMANTICS", "a number's role cannot be settled (role UNRESOLVED)"),
           ("NEEDS_MORE_EVIDENCE", ("cannot be settled from the material at all; the approver "
                                   "should look")),
           ("CHART_NEEDED", ("the answer depends on a chart that is missing, unreadable or not "
                            "shown in this mode")),
           (FORM_CANNOT_EXPRESS, ("the form has no truthful way to record this case; notes "
                                 "required; excluded from scoring and listed for the owner"))),
          kind="codes", applies="every form"),
    Field("confidence", "I", "How confident are you in this whole form?",
          "Prefer CANNOT_DETERMINE and a flag to a low-confidence guess.",
          (("HIGH", ("another knowledgeable reviewer with the same evidence would give the same "
                    "answers")),
           ("MEDIUM", "one or two fields are debatable"), ("LOW", "substantial doubt")),
          applies="every form (required)"),
    Field("notes", "I", "Notes",
          "Free text. Refer to numbers by their row; do not paste the post.",
          (("<text>", "at most 2000 characters, stored as one plain line"),), kind="text",
          applies="every form; required with FORM_CANNOT_EXPRESS and with action "
                  "CANNOT_DETERMINE"),
)
BY_NAME = {f.name: f for f in RUBRIC}

# Section 4, as rendered in the field reference.
APPLICABILITY = (
    ("A label, action, explicit", "yes", "yes", "yes", "yes", "yes", "label only"),
    ("B market and instrument", "yes", "yes", "yes", "yes", "yes", "no"),
    ("C side, position before, effect", "yes", "yes", "yes", "yes", "yes", "no"),
    ("D entry", "yes", "yes", "no", "no", "no", "no"),
    ("E stop, targets", "yes", "yes", "yes", "yes", "no", "no"),
    ("F amendment", "no", "no", "yes", "no", "no", "no"),
    ("G idea", "yes", "yes", "yes", "yes", "yes", "no"),
    ("price table", "yes", "yes", "yes", "yes", "yes", "no"),
    ("H mapping verdicts (after reveal)", "yes", "yes", "yes", "yes", "yes", "no"),
    ("I ambiguities, flags, confidence, notes", "yes", "yes", "yes", "yes", "yes", "yes"),
)
APPLICABILITY_HEAD = ("Field group", "OPEN", "ADD", "MODIFY", "REDUCE", "CLOSE",
                      "NONE (commentary)")

BLOCKING_CODES = {
    "REQUIRED_UNANSWERED": "every applicable field must be answered",
    "TIMING_ACTION": "timing COMMENTARY needs action NONE; CURRENT, HISTORICAL and CONDITIONAL "
                     "need a real action; UNCERTAIN needs a real action or CANNOT_DETERMINE; "
                     "CANNOT_DETERMINE as action only with UNCERTAIN",
    "POST_LABEL_ACTIONS": "on a label-only form the post label must equal the strongest label "
                          "of the listed actions",
    "ACTION_EFFECT": "the action and the effect are not an allowed pair (effect NONE is set by "
                     "the form only, for a REPEAT, for COMMENTARY and for a MODIFY from FLAT)",
    "EFFECT_POSITION": "the effect is impossible from the position before this action: from "
                       "FLAT not INCREASES, REDUCES, CLOSES or AMENDS_PROTECTION; from a "
                       "position open on the same side not OPENS (not checked for a CONDITIONAL "
                       "action). If the earlier posts shown do not show the position, answer "
                       "CANNOT_DETERMINE with the flag MISSING_CONTEXT, not FLAT.",
    "SIDE_POSITION": "for ADD, MODIFY, REDUCE and CLOSE, side and position_side must be equal",
    "ENTRY_FIELDS": "an entry number needs an entry role and EXACT or APPROXIMATE; an entry "
                    "price CANNOT_DETERMINE needs an entry role; NOT_STATED needs neither",
    "TABLE_STOP": "a row with role STOP needs the stop in one of three fields: stop_price (a "
                  "number or CANNOT_DETERMINE), position_last_stop (a number) or "
                  "amendment_from (a number)",
    "TABLE_TARGET": "a row with role TARGET needs targets (numbers or CANNOT_DETERMINE), or the "
                    "action CLOSE or REDUCE, or a MODIFY of a target with a number in "
                    "amendment_from or amendment_to",
    "TABLE_ROW": "a price role needs an exactness and, when asked, a contract month; NAMED needs "
                 "month_named",
    "AMENDMENT": "MODIFY needs amendment_field and amendment_to (a number or CANNOT_DETERMINE "
                 "for STOP and TARGET); on a MODIFY of a target with a number, targets must "
                 "contain it",
    "CANNOT_DETERMINE_NEEDS_REASON": "a CANNOT_DETERMINE, UNCERTAIN or UNRESOLVED answer needs "
                                     "at least one ambiguity or flag",
    "CANNOT_DETERMINE_NEEDS_NOTES": "action CANNOT_DETERMINE needs notes",
    "ESCALATION_NEEDS_NOTES": "FORM_CANNOT_EXPRESS needs notes",
    "ADDITIONAL_ACTION_REQUIRED": "a label-only form labelled ACTIONABLE, CONDITIONAL or "
                                  "RETROSPECTIVE needs at least one additional action",
    "CONFIDENCE": "confidence is required",
    "NOT_APPLICABLE_FIELD": "a field that does not apply, or that waits on an unanswered "
                            "answer, carries a value; or NOT_APPLICABLE was given for a field "
                            "that applies",
    "DERIVED_VALUE": "a value the form derives (the action's label, the announcement for "
                     "CURRENT, the follower implication, component) was given differently",
}
WARNING_CODES = {
    "ENTRY_NOT_IN_TABLE": "an entry number is given but no row carries its role",
    "STOP_NOT_IN_TABLE": "a stop number is given but no row is STOP",
    "TARGET_NOT_IN_TABLE": "target numbers are given but no row is TARGET",
    "REPEAT_POSITION": "a REPEAT whose position before does not already reflect the action (a "
                       "repeat OPEN, ADD, REDUCE or MODIFY with FLAT before; a repeat CLOSE "
                       "with OPEN before; a repeat OPEN or ADD with a position open on the "
                       "other side): for a REPEAT the position before is the one after the "
                       "earlier announcement",
    "ENTRY_ROLE_ORDER": "a row carries an entry kind that ranks before entry_role",
    "STOP_SIDE": "a long's stop above its entry, or a short's below",
    "ACTS_ON_FLAT": "ADD, REDUCE or CLOSE on a FLAT position with an effect other than NONE "
                    "(for example ADD with OPENS), or a MODIFY on a FLAT position other than a "
                    "REPEAT (a change to an order that is not yet a position: effect NONE)",
    "OPEN_ON_OPEN": "OPEN with a position already open on the other side (a reversal is two "
                    "actions)",
    "NEW_IDEA_NOT_OPEN": "NEW_IDEA with an action other than OPEN",
    "POSITION_CHAIN": "the position before this action differs from the position after your "
                      "reading of the previous action of this post in this market",
}


# ------------------------------------------------------------------ the form's context


@dataclass(frozen=True)
class FormContext:
    """What the rules need besides the reading: the item's kind, its place among the post's
    instruction forms, whether the system's reading is revealed, whether the system's mapping
    block names a reference series / proposes an instrument, the table's row keys, and the
    reviewer's saved earlier actions of this post as ``(family, position_after)`` in form
    order (section 13.1)."""

    kind: str = INSTRUCTION
    form_index: int = 1
    forms_in_post: int = 1
    revealed: bool = False
    mapping: tuple[bool, bool] = (False, False)
    rows: tuple[str, ...] = ()
    earlier_positions: tuple[tuple[str, dict | None], ...] = ()

    @property
    def is_last(self) -> bool:
        return self.form_index >= self.forms_in_post


ASK, DERIVED, NA, PENDING = "ASK", "DERIVED", "NA", "PENDING"


@dataclass(frozen=True)
class Status:
    state: str  # ASK | DERIVED | NA | PENDING
    value: object = None  # DERIVED: the value
    reason: str = ""  # NA / PENDING: why


def is_price(v) -> bool:
    return isinstance(v, str) and bool(_PRICE.match(v))


def norm_price(v: str) -> str:
    try:
        text = format(Decimal(v.replace(",", "")).normalize(), "f")
    except (InvalidOperation, AttributeError):
        return v
    return text


def _prices(v) -> list[str]:
    return [x.strip() for x in v.split(",") if x.strip()] if isinstance(v, str) else []


def are_prices(v) -> bool:
    xs = _prices(v)
    return bool(xs) and all(is_price(x) for x in xs)


def _effective_action(r: dict) -> str:
    return "NONE" if r.get("timing") == "COMMENTARY" and r.get("action") in ("", None) \
        else r.get("action", "")


def follower_implication(r: dict) -> str:
    """Section 13.4, from timing, announcement and action; "" while they are unanswered."""
    timing, ann, action = r.get("timing", ""), r.get("announcement", ""), _effective_action(r)
    if timing == "CURRENT":
        return "INSTRUCTION_CANDIDATE"
    if timing == "CONDITIONAL":
        return "WAIT_FOR_CONDITION"
    if timing == "COMMENTARY":
        return "NONE"
    if timing == "UNCERTAIN":
        return CANNOT_DETERMINE
    if timing != "HISTORICAL" or ann == "":
        return ""
    if ann == CANNOT_DETERMINE:
        return CANNOT_DETERMINE
    if ann == "REPEAT":
        return "NO_NEW_INFORMATION"
    if action in ("CLOSE", "REDUCE"):
        return "RECONCILE_EXIT"
    if action in ("OPEN", "ADD", "MODIFY"):
        return "RECONCILE_POSITION"
    return "" if action == "" else CANNOT_DETERMINE


def strongest_label(labels) -> str | None:
    found = [x for x in labels if x in LABEL_STRENGTH]
    return min(found, key=LABEL_STRENGTH.index) if found else None


def overall_label(readings: list[dict], escalated=None) -> str | None:
    """The label of the WHOLE post (section 13.2): the strongest label among the actions the
    reviewer recorded for it (its instruction forms and their ``additional_actions``). Escalated
    forms (``escalated``: one bool per reading) are ignored (14.3); the freeze leaves the whole
    post's label null when any form is escalated."""
    labels = []
    for k, r in enumerate(readings):
        if escalated is not None and escalated[k]:
            continue
        if r.get("component") != "NO_SEPARATE_ACTION":
            labels.append(r.get("post_label"))
        labels += [LABEL_BY_TIMING.get(x.get("timing")) for x in r.get("additional_actions") or []
                   if isinstance(x, dict)]
    return strongest_label(labels)


def applicability(r: dict, ctx: FormContext) -> dict[str, Status]:
    """Section 4 (as amended by 13): per field of the reading (and ``levels`` for the table as
    a whole) whether it is asked, derived (with the value), not applicable (with the reason) or
    pending (an earlier answer it depends on is still unanswered)."""
    s: dict[str, Status] = {}
    ask = Status(ASK)
    if ctx.kind == LABEL_ONLY:
        label = r.get("post_label", "")
        s.update(post_label=ask, ambiguities=ask)
        s["additional_actions"] = ask if label in (*ACTION_LABELS, *OPTIONAL_ACTION_LABELS) \
            else Status(NA, reason="offered on a label-only form only when its label is "
                                   "ACTIONABLE, CONDITIONAL, RETROSPECTIVE or UNRESOLVED")
        s[LEVELS] = Status(NA, reason="a label-only form has no price table")
        return s

    def na(fields, reason):
        for f in fields:
            s.setdefault(f, Status(NA, reason=reason))

    def pending(fields, reason):
        for f in fields:
            s.setdefault(f, Status(PENDING, reason=reason))

    every = (*INSTRUCTION_FIELDS, LEVELS)
    if ctx.forms_in_post > 1:
        s["component"] = ask
        comp = r.get("component", "")
    else:
        s["component"] = Status(DERIVED, "ACTION_RECORDED_HERE")
        comp = "ACTION_RECORDED_HERE"
    if comp == "NO_SEPARATE_ACTION":
        na(every, "this form records no separate action")
        return s
    if comp == "":
        pending(every, "answer whether this form records an action first")
        return s
    timing = r.get("timing", "")
    s["timing"] = ask
    s["post_label"] = Status(DERIVED, LABEL_BY_TIMING[timing]) if timing in LABEL_BY_TIMING \
        else Status(PENDING, reason="answer the timing first")
    if timing == "HISTORICAL":
        s["announcement"] = ask
    elif timing == "CURRENT":
        s["announcement"] = Status(DERIVED, "FIRST_REPORT")
    elif timing == "UNCERTAIN":  # 14.2: not asked; the effect is asked as for any action
        s["announcement"] = Status(DERIVED, CANNOT_DETERMINE)
    elif timing in ("CONDITIONAL", "COMMENTARY"):
        s["announcement"] = Status(NA, reason="asked for a historical action only")
    else:
        s["announcement"] = Status(PENDING, reason="answer the timing first")
    s["action"] = Status(DERIVED, "NONE") if timing == "COMMENTARY" else ask
    fi = follower_implication(r)
    s["follower_implication"] = Status(DERIVED, fi) if fi else Status(
        PENDING, reason="answer the timing, the announcement and the action first")
    action = _effective_action(r)
    s["ambiguities"] = ask
    s["additional_actions"] = ask if ctx.is_last else Status(
        NA, reason="offered on the last form of the post only")
    b_to_g = ("family", "instrument_as_read", "quoted_series", "traded_instrument", "side",
              "position_state", "position_side", "position_last_stop", "effect", "entry_price",
              "entry_role", "entry_exactness", "stop_price", "targets", "amendment_field",
              "amendment_to", "amendment_from", "idea", LEVELS)
    if action == "":
        s["explicit"] = ask
        pending((*b_to_g, *MAPPING_FIELDS), "choose the action first")
        return s
    if action == "NONE":
        s["effect"] = Status(DERIVED, "NONE")
        na(("explicit", *b_to_g, *MAPPING_FIELDS), "the post states no trading action")
        return s
    s["explicit"] = ask
    _mapping(s, ctx)
    if action == CANNOT_DETERMINE:
        na(b_to_g, "the action cannot be determined (notes required)")
        return s
    for f in ("family", "instrument_as_read", "quoted_series", "traded_instrument", "side",
              "position_state", "idea"):
        s[f] = ask
    if r.get("position_state") == "FLAT":
        na(("position_side", "position_last_stop"), "the position before this action was FLAT")
    else:
        s["position_side"] = s["position_last_stop"] = ask
    if r.get("announcement") == "REPEAT" and timing == "HISTORICAL":
        s["effect"] = Status(DERIVED, "NONE")
    elif action == "MODIFY" and r.get("position_state") == "FLAT":
        # 15.1: a change to an order that is not yet a position; the position is unchanged.
        s["effect"] = Status(DERIVED, "NONE")
    elif action == "MODIFY":  # 14.2: a HISTORICAL MODIFY too, unless REPEAT
        s["effect"] = Status(DERIVED, "AMENDS_PROTECTION")
    else:
        s["effect"] = ask
    if action in ("OPEN", "ADD"):
        s["entry_price"] = ask
        price = r.get("entry_price", "")
        if price == "":
            pending(("entry_role", "entry_exactness"), "answer the entry price first")
        elif price == NOT_STATED:
            s["entry_role"] = Status(DERIVED, NOT_STATED)
            s["entry_exactness"] = Status(NA, reason="there is no entry number")
        elif price == CANNOT_DETERMINE:  # 14.5: the kind of entry can be known, not the level
            s["entry_role"] = ask
            s["entry_exactness"] = Status(NA, reason="there is no readable entry number")
        else:
            s["entry_role"] = s["entry_exactness"] = ask
    else:
        na(("entry_price", "entry_role", "entry_exactness"), "the entry applies to OPEN and ADD")
    if action == "CLOSE":
        na(("stop_price", "targets"), "the stop and targets do not apply to CLOSE")
    else:
        s["targets"] = ask
        if action == "MODIFY" and r.get("amendment_field") == "STOP":
            to = r.get("amendment_to", "")
            s["stop_price"] = Status(DERIVED, to) if to else Status(
                PENDING, reason="answer the new value of the amendment first")
        else:
            s["stop_price"] = ask
    if action == "MODIFY":
        s["amendment_field"] = ask
        fld = r.get("amendment_field", "")
        if fld == "":
            pending(("amendment_to", "amendment_from"), "answer what the MODIFY changes first")
        else:
            s["amendment_to"] = ask
            if fld == "STOP":
                last = r.get("position_last_stop", "")
                if r.get("position_state") == "FLAT":
                    last = NOT_APPLICABLE
                s["amendment_from"] = Status(DERIVED, last) if last else Status(
                    PENDING, reason="answer the last stop before this action first")
            else:
                s["amendment_from"] = ask
    else:
        na(("amendment_field", "amendment_to", "amendment_from"), "the amendment applies to "
                                                                  "MODIFY")
    s[LEVELS] = ask if ctx.rows else Status(NA, reason="the post has no numbers")
    return s


def _mapping(s: dict, ctx: FormContext) -> None:
    for f, present in zip(MAPPING_FIELDS, ctx.mapping, strict=True):
        if not ctx.revealed:
            s[f] = Status(NA, reason="asked only after the system's reading is revealed")
        elif not present:
            s[f] = Status(NA, reason="the system's mapping block is empty")
        else:
            s[f] = Status(ASK)


def row_applicability(row: dict, reading: dict) -> dict[str, Status]:
    """Per column of one price-table row (the table itself applies)."""
    role = row.get("role", "")
    out = {"role": Status(ASK)}
    if role in NON_PRICE_ROLES:
        why = "not a price of this action"
        out.update(exactness=Status(NA, reason=why), contract_month=Status(NA, reason=why),
                   month_named=Status(NA, reason=why))
        return out
    if role == "":
        out.update(exactness=Status(PENDING, reason="choose the role first"),
                   contract_month=Status(PENDING, reason="choose the role first"),
                   month_named=Status(PENDING, reason="choose the role first"))
        return out
    out["exactness"] = Status(ASK)
    series = reading.get("quoted_series", "")
    if series in MONTH_SERIES:
        out["contract_month"] = Status(ASK)
    elif series == "":
        out["contract_month"] = Status(PENDING, reason="answer the quoted series first")
    else:
        out["contract_month"] = Status(NA, reason="asked only for a futures series")
    out["month_named"] = Status(ASK) if row.get("contract_month") == "NAMED" else Status(
        NA, reason="only with NAMED")
    return out


def _blank(f: str):
    return [] if f in LIST_FIELDS else ""


def _na(f: str):
    return [] if f in LIST_FIELDS else NOT_APPLICABLE


def complete(reading: dict, ctx: FormContext) -> dict:
    """The reading with every derived value and every NOT_APPLICABLE filled in where it is still
    unanswered (a contradicting value is left in place for ``validate`` to refuse)."""
    r = {f: reading.get(f, _blank(f)) for f in fields_for(ctx.kind)}
    if ctx.kind != LABEL_ONLY:
        given = {x.get("row"): x for x in reading.get(LEVELS, []) or [] if isinstance(x, dict)}
        r[LEVELS] = [{"row": k, **{c: str(given.get(k, {}).get(c, "") or "") for c in ROW_KEYS}}
                     for k in ctx.rows]
    for _ in range(6):  # a derived value can make another field apply or not: to a fixed point
        before = repr(r)
        st = applicability(r, ctx)
        for f, s in st.items():
            if f == LEVELS:
                continue
            if s.state == DERIVED and r[f] in ("", s.value):
                r[f] = s.value
            elif s.state == NA and r[f] in ("", []):
                r[f] = _na(f)
        if ctx.kind != LABEL_ONLY:
            table = st.get(LEVELS, Status(NA))
            for row in r[LEVELS]:
                if table.state == NA:
                    for c in ROW_KEYS:
                        if row[c] == "":
                            row[c] = NOT_APPLICABLE if c != "month_named" else ""
                    continue
                for c, s in row_applicability(row, r).items():
                    if s.state == NA and row[c] == "":
                        row[c] = NOT_APPLICABLE if c != "month_named" else ""
        if repr(r) == before:
            break
    return r


def rederive(reading: dict, ctx: FormContext) -> dict:
    """14.3: the reading with every derived value recomputed (whatever was stored), every field
    waiting on an unanswered answer blank unless NOT_APPLICABLE, and every field that does not
    apply NOT_APPLICABLE. A reading that passed ``validate`` is unchanged. The freeze applies
    it, so a frozen row never carries a derived value its own answers contradict."""
    r = complete(reading, ctx)
    for _ in range(8):
        before = repr(r)
        for f, s in applicability(r, ctx).items():
            if f == LEVELS:
                continue
            if s.state == DERIVED:
                r[f] = s.value
            elif s.state == PENDING and r[f] not in ("", [], NOT_APPLICABLE):
                r[f] = _blank(f)
            elif s.state == NA:
                r[f] = _na(f)
        if repr(r) == before:
            break
    return r


def position_before(r: dict) -> dict:
    out = {"state": r.get("position_state", ""), "side": r.get("position_side", ""),
           "stop": r.get("position_last_stop", "")}
    if out["state"] == "FLAT":
        out.update(side=NOT_APPLICABLE, stop=NOT_APPLICABLE)
    return out


def position_after(r: dict) -> dict | None:
    """The derived "Position AFTER this action" (section 3 C, 13.1), or None when the form
    records no action whose position is asked (no separate action, NONE, CANNOT_DETERMINE, a
    label-only form, or the answers it needs are still missing)."""
    action, effect = r.get("action", ""), r.get("effect", "")
    if r.get("component") == "NO_SEPARATE_ACTION" or action not in REAL_ACTIONS or effect in (
            "", NOT_APPLICABLE) or r.get("position_state", "") == "":
        return None
    before = position_before(r)
    if r.get("timing") == "CONDITIONAL" or effect == "NONE":
        return _open_stop(before)
    stop = r.get("stop_price", "")
    if effect == "OPENS":
        return _open_stop({"state": "OPEN", "side": r.get("side", ""), "stop": stop})
    if effect in ("INCREASES", "REDUCES", "AMENDS_PROTECTION"):
        return _open_stop({"state": "OPEN", "side": r.get("side", ""),
                           "stop": stop if is_price(stop) else before["stop"]})
    if effect == "CLOSES":
        return {"state": "FLAT", "side": NOT_APPLICABLE, "stop": NOT_APPLICABLE}
    return {"state": CANNOT_DETERMINE, "side": CANNOT_DETERMINE, "stop": CANNOT_DETERMINE}


def _open_stop(p: dict) -> dict:
    """14.4: an OPEN position never carries NOT_APPLICABLE as its stop (NOT_STATED)."""
    if p.get("state") == "OPEN" and p.get("stop") == NOT_APPLICABLE:
        return {**p, "stop": NOT_STATED}
    return p


def describe_position(p: dict | None) -> str:
    if p is None:
        return "not applicable"
    if p["state"] == "OPEN":
        return f"OPEN {p['side'] or '(side unanswered)'}, stop {p['stop'] or '(unanswered)'}"
    return p["state"] or "(unanswered)"


def previous_position(r: dict, ctx: FormContext) -> dict | None:
    """The position after the reviewer's reading of the previous action of this post in this
    market, when one is saved (section 13.1)."""
    fam = r.get("family", "")
    found = [p for f, p in ctx.earlier_positions if fam and f == fam and p is not None]
    return found[-1] if found else None


def _same_position(a: dict, b: dict) -> bool:
    return all(same("position_last_stop" if k == "stop" else "x", a.get(k, ""), b.get(k, ""))
               or a.get(k, "") == b.get(k, "") for k in ("state", "side", "stop"))


# ------------------------------------------------------------------ what Save checks


@dataclass
class Checks:
    blocking: list[tuple[str, str]] = field(default_factory=list)  # (code, field)
    warnings: list[str] = field(default_factory=list)
    unanswered: list[str] = field(default_factory=list)

    @property
    def codes(self) -> list[str]:
        return list(dict.fromkeys(c for c, _ in self.blocking))


_DERIVED_CODE = {"action": "TIMING_ACTION", "effect": "ACTION_EFFECT",
                 "entry_role": "ENTRY_FIELDS", "stop_price": "AMENDMENT",
                 "amendment_from": "AMENDMENT"}
_NA_CODE = {"entry_role": "ENTRY_FIELDS", "entry_exactness": "ENTRY_FIELDS"}


def _cd(v) -> bool:
    return v in (CANNOT_DETERMINE, "UNRESOLVED", "UNCERTAIN")


def validate(reading: dict, ctx: FormContext, *, flags=(), confidence: str = "",
             notes: str = "", required=()) -> Checks:
    """Section 7 (as amended by 13) over a reading (``complete`` is applied first): blocking
    codes with the field they concern, warning codes, and the applicable fields still
    unanswered. ``required``: fields that must be answered even under FORM_CANNOT_EXPRESS (15.2:
    the fields an approver's amendment reset)."""
    r = complete(reading, ctx)
    st = applicability(r, ctx)
    out = Checks()
    escalated = FORM_CANNOT_EXPRESS in flags

    def block(code, f=""):
        if (code, f) not in out.blocking:
            out.blocking.append((code, f))

    for f in fields_for(ctx.kind):
        s, v = st.get(f, Status(NA)), r[f]
        if s.state == ASK:
            if v == NOT_APPLICABLE:
                block("NOT_APPLICABLE_FIELD", f)
            elif v == "" and f not in LIST_FIELDS:
                out.unanswered.append(f)
        elif s.state == DERIVED and v not in ("", s.value):
            block(_DERIVED_CODE.get(f, "DERIVED_VALUE"), f)
        elif s.state == NA and v not in ("", [], NOT_APPLICABLE):
            block(_NA_CODE.get(f, "NOT_APPLICABLE_FIELD"), f)
        elif s.state == PENDING and v not in ("", [], NOT_APPLICABLE):
            # 14.3: a field waiting on an unanswered answer holds no value, escalated or not.
            block("NOT_APPLICABLE_FIELD", f)
    # The price table.
    table = st.get(LEVELS, Status(NA))
    rows = r.get(LEVELS, []) if ctx.kind != LABEL_ONLY else []
    for k, row in enumerate(rows, 1):
        if table.state != ASK:
            if any(row[c] not in ("", NOT_APPLICABLE) for c in ROW_KEYS):
                block("NOT_APPLICABLE_FIELD", LEVELS)
            continue
        if row["role"] in ("", NOT_APPLICABLE):
            out.unanswered.append(f"levels row {k}")
            continue
        for c, s in row_applicability(row, r).items():
            if c == "role":
                continue
            v = row[c]
            if s.state == ASK and v in ("", NOT_APPLICABLE) or s.state == NA and v not in ("", NOT_APPLICABLE) or s.state == ASK and c == "month_named" and not _MONTH.match(v):
                block("TABLE_ROW", f"levels row {k}")
    for f in out.unanswered:  # 15.2: escalation never lifts it for a field reset by an amendment
        if not escalated or f in required:
            block("REQUIRED_UNANSWERED", f)
    # TIMING_ACTION.
    timing, action = r.get("timing", ""), r.get("action", "")
    if timing and action and NOT_APPLICABLE not in (timing, action) and _timing_action_wrong(
            timing, action):
        block("TIMING_ACTION", "action")
    # ACTION_EFFECT (14.2: NONE only where the form sets it, which the derived check covers).
    effect = r.get("effect", "")
    effect_asked = st.get("effect", Status(NA)).state == ASK
    if effect_asked and action in ALLOWED_EFFECTS and effect not in (
            "", NOT_APPLICABLE, CANNOT_DETERMINE) and effect not in ALLOWED_EFFECTS[action]:
        block("ACTION_EFFECT", "effect")
    # EFFECT_POSITION (14.4; 15.1: not for a CONDITIONAL action, which has not happened).
    state, side_ = r.get("position_state", ""), r.get("side", "")
    if action in REAL_ACTIONS and timing != "CONDITIONAL" and (
            (state == "FLAT" and effect in ("INCREASES", "REDUCES", "CLOSES",
                                            "AMENDS_PROTECTION")) or
            (state == "OPEN" and effect == "OPENS" and side_ in ("LONG", "SHORT")
             and r.get("position_side") == side_)):
        block("EFFECT_POSITION", "effect")
    # SIDE_POSITION.
    side, pside = r.get("side", ""), r.get("position_side", "")
    if action in ("ADD", "MODIFY", "REDUCE", "CLOSE") and side in ("LONG", "SHORT") and \
            pside in ("LONG", "SHORT") and side != pside:
        block("SIDE_POSITION", "side")
    # ENTRY_FIELDS.
    if st.get("entry_price", Status(NA)).state == ASK and is_price(r.get("entry_price", "")) \
            and (r["entry_role"] == NOT_STATED or r["entry_exactness"] == NOT_APPLICABLE):
        block("ENTRY_FIELDS", "entry_role")
    if st.get("entry_price", Status(NA)).state == ASK and r.get("entry_price") == \
            CANNOT_DETERMINE and r.get("entry_role") == NOT_STATED:
        block("ENTRY_FIELDS", "entry_role")  # 14.5: the kind of entry is asked
    # TABLE_STOP / TABLE_TARGET (14.1).
    if table.state == ASK:
        roles = [row["role"] for row in rows]
        if "STOP" in roles and not _stop_referred(r):
            block("TABLE_STOP", "stop_price")
        if "TARGET" in roles and not _target_referred(r):
            block("TABLE_TARGET", "targets")
    # AMENDMENT.
    if action == "MODIFY" and st.get("amendment_field", Status(NA)).state == ASK:
        fld, to, frm = (r.get("amendment_field", ""), r.get("amendment_to", ""),
                        r.get("amendment_from", ""))
        if not escalated and (not fld or not to):
            block("AMENDMENT", "amendment_to" if fld else "amendment_field")
        if fld in ("STOP", "TARGET") and to and not (is_price(to) or to == CANNOT_DETERMINE):
            block("AMENDMENT", "amendment_to")
        if fld == "TARGET" and frm and not (is_price(frm) or frm in (NOT_STATED,
                                                                     CANNOT_DETERMINE)):
            block("AMENDMENT", "amendment_from")
        if fld == "TARGET" and is_price(to) and norm_price(to) not in [
                norm_price(x) for x in _prices(r.get("targets", "")) if is_price(x)]:
            block("AMENDMENT", "targets")  # 14.1: the targets in force contain the new one
    # The additional actions.
    extras = r.get("additional_actions") or []
    if st.get("additional_actions", Status(NA)).state == ASK:
        for x in extras:
            if any(x.get(k, "") == "" for k in ADDITIONAL_KEYS) and not escalated:
                block("REQUIRED_UNANSWERED", "additional_actions")
            if x.get("timing") and x.get("action") and _timing_action_wrong(
                    x["timing"], x["action"]):
                block("TIMING_ACTION", "additional_actions")
        if ctx.kind == LABEL_ONLY:
            if not extras and not escalated and r.get("post_label") in ACTION_LABELS:
                block("ADDITIONAL_ACTION_REQUIRED", "additional_actions")
            elif strongest_label(LABEL_BY_TIMING.get(x.get("timing")) for x in extras) not in (
                    None, r.get("post_label")):
                block("POST_LABEL_ACTIONS", "post_label")  # escalated too (14.1)
    # CANNOT_DETERMINE / UNCERTAIN / UNRESOLVED need a reason.
    undetermined = [f for f in fields_for(ctx.kind) if f not in LIST_FIELDS and _cd(r[f])]
    undetermined += [LEVELS for row in rows if _cd(row["role"])]
    undetermined += ["additional_actions" for x in extras
                     if any(_cd(x.get(k, "")) for k in ADDITIONAL_KEYS)]
    if undetermined and not (r.get("ambiguities") or list(flags)):
        block("CANNOT_DETERMINE_NEEDS_REASON", undetermined[0])
    if action == CANNOT_DETERMINE and not notes.strip():
        block("CANNOT_DETERMINE_NEEDS_NOTES", "notes")
    if escalated and not notes.strip():
        block("ESCALATION_NEEDS_NOTES", "notes")
    if not confidence:
        block("CONFIDENCE", "confidence")
    out.warnings = _warnings(r, st, rows, table, ctx, flags)
    return out


def _stop_referred(r: dict) -> bool:
    """14.1: a STOP row is accepted when the reading carries the stop somewhere."""
    return is_price(r.get("stop_price", "")) or r.get("stop_price") == CANNOT_DETERMINE or \
        is_price(r.get("position_last_stop", "")) or is_price(r.get("amendment_from", ""))


def _target_referred(r: dict) -> bool:
    """14.1: a TARGET row is accepted when targets carry one, on an exit (a target reached or
    spoken of), or on a MODIFY of a target with a number replaced or set."""
    return are_prices(r.get("targets", "")) or r.get("targets") == CANNOT_DETERMINE or \
        r.get("action") in ("CLOSE", "REDUCE") or (
            r.get("action") == "MODIFY" and r.get("amendment_field") == "TARGET" and (
                is_price(r.get("amendment_from", "")) or is_price(r.get("amendment_to", ""))))


def _timing_action_wrong(timing: str, action: str) -> bool:
    if timing == "COMMENTARY":
        return action != "NONE"
    if timing in ("CURRENT", "HISTORICAL", "CONDITIONAL"):
        return action not in REAL_ACTIONS
    if timing == "UNCERTAIN":  # 14.2 (N4): no action is COMMENTARY
        return action not in (*REAL_ACTIONS, CANNOT_DETERMINE)
    return False


NOT_IN_TABLE_EXEMPT = ("LEVEL_BY_REFERENCE", "CHART_NEEDED")  # 14.9


def _warnings(r: dict, st: dict, rows: list, table: Status, ctx: FormContext,
              flags=()) -> list[str]:
    out = []
    action = r.get("action", "")
    if action not in REAL_ACTIONS:
        return out
    roles = [row["role"] for row in rows] if table.state == ASK else []
    entry_asked = st.get("entry_price", Status(NA)).state == ASK
    # 14.9: a level given by reference, or one on a chart, is not in the text by nature.
    exempt = bool(set(NOT_IN_TABLE_EXEMPT) & {*(r.get("ambiguities") or []), *flags})
    if not exempt and entry_asked and is_price(r.get("entry_price", "")) and r.get(
            "entry_role") in ENTRY_ROLES and r["entry_role"] not in roles:
        out.append("ENTRY_NOT_IN_TABLE")
    if not exempt and is_price(r.get("stop_price", "")) and "STOP" not in roles:
        out.append("STOP_NOT_IN_TABLE")
    if not exempt and are_prices(r.get("targets", "")) and "TARGET" not in roles:
        out.append("TARGET_NOT_IN_TABLE")
    if entry_asked and r.get("entry_role") in ENTRY_KINDS:
        rank = ENTRY_KINDS.index(r["entry_role"])
        if any(x in ENTRY_KINDS and ENTRY_KINDS.index(x) < rank for x in roles):
            out.append("ENTRY_ROLE_ORDER")
    entry, stop, side = r.get("entry_price", ""), r.get("stop_price", ""), r.get("side", "")
    if entry_asked and is_price(entry) and is_price(stop) and (
            (side == "LONG" and Decimal(stop) > Decimal(entry)) or
            (side == "SHORT" and Decimal(stop) < Decimal(entry))):
        out.append("STOP_SIDE")
    repeat = r.get("announcement") == "REPEAT" and r.get("timing") == "HISTORICAL"
    state, pside = r.get("position_state"), r.get("position_side")
    if action in ("ADD", "REDUCE", "CLOSE") and state == "FLAT" \
            and r.get("effect") not in ("NONE", "", NOT_APPLICABLE):
        out.append("ACTS_ON_FLAT")
    elif action == "MODIFY" and state == "FLAT" and not repeat:
        out.append("ACTS_ON_FLAT")  # 15.1: effect NONE set by the form; the warning stays
    if action == "OPEN" and r.get("position_state") == "OPEN" and side in ("LONG", "SHORT") \
            and r.get("position_side") in ("LONG", "SHORT") and r["position_side"] != side:
        out.append("OPEN_ON_OPEN")
    if r.get("idea") == "NEW_IDEA" and action != "OPEN":
        out.append("NEW_IDEA_NOT_OPEN")
    if repeat and (  # 15.3: the position before a REPEAT already reflects the action
            (action in ("OPEN", "ADD", "REDUCE", "MODIFY") and state == "FLAT") or
            (action == "CLOSE" and state == "OPEN") or
            (action in ("OPEN", "ADD") and state == "OPEN" and side in ("LONG", "SHORT")
             and pside in ("LONG", "SHORT") and pside != side)):
        out.append("REPEAT_POSITION")
    prev = previous_position(r, ctx)
    if prev is not None and r.get("position_state") and not _same_position(
            position_before(r), prev):
        out.append("POSITION_CHAIN")
    return out


# ------------------------------------------------------------------ the comparison (section 8)


def _norm(f: str, v) -> str:
    if not isinstance(v, str):
        return repr(v)
    if f in PRICE_FIELDS and is_price(norm_price(v)):
        return norm_price(v)
    if f == "targets" and v not in (NOT_STATED, CANNOT_DETERMINE, NOT_APPLICABLE):
        return ",".join(norm_price(x) for x in _prices(v))
    if f in ("amendment_to", "amendment_from") and is_price(norm_price(v)):
        return norm_price(v)
    return v.strip().casefold()


def same(f: str, mine, system) -> bool:
    """The reviewer's value equals the system's; the system's empty or NONE equals the
    reviewer's NOT_STATED or NOT_APPLICABLE (the system cannot tell them apart)."""
    if mine in (NOT_STATED, NOT_APPLICABLE) and system in ("", "NONE", None):
        return True
    if f == "contract_month" and mine == NOT_APPLICABLE and system == NOT_STATED:
        return True  # a month not asked (no futures series) against the system's "not stated"
    return _norm(f, mine) == _norm(f, system or "")


def prefill_verdict(f: str, mine, system) -> str:
    if mine == NOT_APPLICABLE:
        return VERDICT_NOT_APPLICABLE
    if mine in ("", CANNOT_DETERMINE):
        return "UNSURE"
    return "CONFIRM" if same(f, mine, system) else "CORRECT"


def prefill_verdicts(reading: dict, system: dict, kind: str) -> dict[str, str]:
    return {f: prefill_verdict(f, reading.get(f, ""), system.get(f, ""))
            for f in compared_fields(kind)}


NOT_READ = "NOT_READ"


def prefill_row_verdict(row: dict, sys_row: dict) -> str:
    if row.get("role") == NOT_APPLICABLE:
        return VERDICT_NOT_APPLICABLE
    if sys_row.get("not_read"):
        return NOT_READ
    if not row.get("role"):
        return "UNSURE"
    if row["role"] == sys_row.get("role") == "NOT_A_PRICE":
        return "CONFIRM"
    ok = all(same(c, row.get(c, ""), sys_row.get(c, "")) for c in ("role", "exactness",
                                                                     "contract_month"))
    return "CONFIRM" if ok else "CORRECT"


def expected_row_verdict(row: dict, sys_row: dict) -> str | None:
    """The verdict a row MUST carry (NOT_APPLICABLE or NOT_READ), or None: any of VERDICTS."""
    if row.get("role") == NOT_APPLICABLE:
        return VERDICT_NOT_APPLICABLE
    if sys_row.get("not_read"):
        return NOT_READ
    return None


# ------------------------------------------------------------------ allowed values (cleaning)


def options(f: str, families=(), sequences=()) -> tuple[str, ...]:
    """The values a reviewer may choose for a closed field (without the form's NOT_APPLICABLE)."""
    return {
        "component": COMPONENTS, "post_label": POST_LABELS, "action": ACTIONS,
        "explicit": YES_NO, "family": tuple(families),
        "quoted_series": QUOTED_SERIES, "traded_instrument": TRADED_INSTRUMENTS,
        "side": SIDES, "position_state": POSITION_STATES, "position_side": POSITION_SIDES,
        "effect": EFFECTS, "entry_role": ENTRY_ROLES, "entry_exactness": ENTRY_EXACTNESS,
        "amendment_field": AMENDMENT_FIELDS,
        "idea": (*IDEA_FIXED, *(f"SEQUENCE:{s}" for s in sequences)),
        "reference_series_right": YES_NO_UNSURE, "execution_instrument_right": YES_NO_UNSURE,
        "timing": TIMINGS, "announcement": ANNOUNCEMENTS,
        "follower_implication": FOLLOWER_IMPLICATIONS,
    }.get(f, ())


def offered(f: str, families=(), sequences=()) -> tuple[str, ...]:
    """The values a widget offers: ``options`` without the values only the form sets."""
    return tuple(v for v in options(f, families, sequences) if v not in FORM_ONLY.get(f, ()))


def value_problem(f: str, v, families=(), sequences=()) -> str | None:
    """A code when ``v`` is not a storable value of field ``f`` (closed sets, shapes), else
    None. Whether it fits the rest of the reading is ``validate``'s job."""
    if f in LIST_FIELDS:
        return None  # checked by the record layer's own cleaning
    if not isinstance(v, str):
        return f"BAD_VALUE:{f}"
    if v in ("", NOT_APPLICABLE):
        return None
    if f == "entry_role" and v == NOT_STATED:
        return None
    if f in ("instrument_as_read", "amendment_to", "amendment_from"):
        return None if len(v) <= MAX_TEXT else f"BAD_VALUE:{f}"
    if f in PRICE_FIELDS:
        return None if is_price(v) or v in VALUE_SENTINELS[f] else f"BAD_PRICE:{f}"
    if f == "targets":
        return None if v in VALUE_SENTINELS[f] or are_prices(v) else f"BAD_PRICE:{f}"
    allowed = options(f, families, sequences)
    return None if v in allowed else f"UNKNOWN_VALUE:{f}"


def clean_month(v) -> str:
    return v.strip().upper() if isinstance(v, str) else v


# ------------------------------------------------------------------ the guide and the docs


REVIEWER_GUIDE = """# Reviewer guide

## Purpose and Confidentiality

Your readings are the ground truth the system is measured against (details: the Field reference
page). Nothing you save places, changes or approves a trade, a rule or a mapping (the system's
proposal of which instrument would follow an idea). "What this means for a follower" is not an
order.

This is paid content, for this review only: do not copy it, take screenshots, forward it or paste
it elsewhere.

## Forms and fields

An instruction form records ONE action (done, reported or planned) on one position in one market.
Record a post's actions in text order, one per form; a surplus form answers "no separate action";
extra actions go in "further actions" on the last form. A label-only form labels a whole post the
system did not read as an instruction: the strongest label (ACTIONABLE, CONDITIONAL, RETROSPECTIVE,
UNRESOLVED, COMMENTARY) of the further actions you list.

Groups: A timing, announcement, action, explicit wording; B market, instrument; C side, the
position BEFORE THIS ACTION (earlier posts shown, earlier actions of this post), effect; D entry; E
stop, targets; F amendment; G idea (one position, opening to final close); H opinions on the
system's mapping, after the reveal; I ambiguities, flags, confidence, notes. Fields that do not
apply are greyed.

## Timing and prices

The post's own time words decide. CURRENT: now or just done ("a few minutes ago"); a plain past
with no time word is CURRENT too: "Bought gold at 2,395." HISTORICAL: the post places it earlier
("this morning", "overnight"), or an existing order did it (a stop-out). CONDITIONAL: only if
something happens later. Otherwise UNCERTAIN. A historical action is a FIRST_REPORT (this post
first tells subscribers) or a REPEAT (an earlier post shown announced it; the position before is
the one after that announcement). "We bought gold this morning at 2,395." is a HISTORICAL
FIRST_REPORT: a position to reconcile, not an instruction.

Each number is a row (in restricted access: each level the system typed); give it the first role
that applies, top to bottom; context decides, never a phrase alone. An executed price is
HISTORICAL_FILL even at the stop ("stopped out at 2.84"); a level named as the stop is STOP ("the
69.50 stop was hit, filled 69.40": 69.50 STOP, 69.40 HISTORICAL_FILL). Only INTENDED_ENTRY, a price
to transact at, is an instruction. Two readings left: UNRESOLVED with AMBIGUOUS_PRICE_SEMANTICS;
there is no default.

## Earlier posts and doubt

Use only the earlier posts shown. What needs a post not shown is CANNOT_DETERMINE with the flag
MISSING_CONTEXT; so is a position they do not establish, never FLAT (FLAT: they establish that none
existed). Unsure: CANNOT_DETERMINE (UNCERTAIN for a timing, UNRESOLVED for a label or role) with an
ambiguity or flag; never guess. NOT_STATED: the publisher did not state it. If the form cannot
record the case truthfully, set FORM_CANNOT_EXPRESS, explain in the notes and leave the rest
unanswered.

## Saving, the reveal, resuming

Warnings need the tick "I have checked these". Once every form of the post has your independent
reading, the system's reading appears. Each compared field (shown beside the system's value) then
gets a verdict on the SYSTEM's value: CONFIRM (it is right), CORRECT (it is wrong, yours is right)
or UNSURE. A row shown as NOT_READ (the system typed no level there) needs none. Change an answer
only for evidence you missed. Every Save is a new line: nothing is overwritten. "Next item not yet
adjudicated" resumes.
"""

REVIEWER_GUIDE_MAX_WORDS = 580  # 15.5: ONE printed page (a test enforces it)
REVIEWER_GUIDE_MAX_HEADINGS = 6

HOW_TO_REVIEW = (
    "1. Read the post and only the earlier posts shown.",
    "2. Decide what the publisher does and what each number is, on your own.",
    "3. Fill in the fields; a field that does not apply is greyed, with the reason.",
    ("4. Where the evidence does not settle something, answer CANNOT_DETERMINE and say why; never "
    "guess."),
    "5. Save your independent reading.",
    ("6. Once the last form of the post is saved (a post may have several forms), the system's "
     "reading appears: give each field a verdict on the SYSTEM's value (CONFIRM: it is right; "
     "CORRECT: it is wrong, yours is right; UNSURE), and save again."),
)


def applicability_text() -> str:
    widths = [max(len(r[i]) for r in (APPLICABILITY_HEAD, *APPLICABILITY))
              for i in range(len(APPLICABILITY_HEAD))]
    return "\n".join("  ".join(c.ljust(w) for c, w in zip(row, widths, strict=True)).rstrip()
                     for row in (APPLICABILITY_HEAD, *APPLICABILITY))


def render_rubric_markdown() -> str:
    """docs/ADJUDICATION_RUBRIC.md, rendered from ``RUBRIC`` (scripts/adjudication_rubric_docs.py
    writes it; a test fails when the file and the table drift apart)."""
    out = ["# Adjudication rubric (version 2.3)", "",
           ("<!-- Rendered from RUBRIC in src/tradingbot/runtime/adjudication_rubric.py by "
           "scripts/adjudication_rubric_docs.py. Do not edit by hand. -->"), "",
           ("The authoritative definition of every field a reviewer answers in the adjudication "
           "workspace ([ADJUDICATION_WORKSPACE.md](ADJUDICATION_WORKSPACE.md)). Every example is "
           "invented."), "",
           "## Answer states", "",
           "| Stored value | Meaning |", "| --- | --- |",
           ("| `\"\"` | UNANSWERED: not answered yet; Save is refused while a required field is "
           "unanswered |"),
           "| `NOT_STATED` | the publisher did not state it |",
           ("| `CANNOT_DETERMINE` | it probably exists, but the evidence shown does not settle it; "
           "needs an ambiguity or a flag |"),
           ("| `NOT_APPLICABLE` | the field does not apply to this action or label; set by the "
           "form, never typed |"), ""]
    for code, title in GROUPS:
        out += [f"## {code}. {title}", ""]
        for f in (x for x in RUBRIC if x.group == code):
            out += [f"### `{f.name}`: {f.question}", "", f.tooltip, ""]
            if f.applies:
                out += [f"Applies: {f.applies}.", ""]
            if f.set_by_form:
                out += [f"Set by the form: {f.set_by_form}.", ""]
            out += [f"- `{c}`: {m}" for c, m in f.values] + [""]
            if f.examples:
                out += ["Invented examples:", ""] + [f"- {e}" for e in f.examples] + [""]
    out += ["## Which fields apply", "", "| " + " | ".join(APPLICABILITY_HEAD) + " |",
            "| " + " | ".join("---" for _ in APPLICABILITY_HEAD) + " |"]
    out += ["| " + " | ".join(r) + " |" for r in APPLICABILITY] + [
        "", ("A field that does not apply is not shown (or greyed with the reason) and is stored "
        "`NOT_APPLICABLE` (lists: `[]`). With action NONE the price table is not asked. With "
        "action CANNOT_DETERMINE groups B to G and the table are not asked and notes are "
        "required. CONDITIONAL and RETROSPECTIVE use the row of their action."), "",
        "## What Save refuses (blocking)", ""]
    out += [f"- `{c}`: {m}" for c, m in BLOCKING_CODES.items()]
    out += ["", ("With the flag FORM_CANNOT_EXPRESS (notes required) only `REQUIRED_UNANSWERED` "
            "is lifted: leave unanswered what cannot be answered truthfully. Every check between "
            "the answers given still applies, the derived values are still checked, and a field "
            "waiting on an unanswered answer may hold only `\"\"` or `NOT_APPLICABLE`."), "",
            ("## Warnings (saving needs the tick \"I have checked these\"; stored as "
             "`acknowledged_warnings`)"), ""]
    out += [f"- `{c}`: {m}" for c, m in WARNING_CODES.items()]
    out += ["", ("`ENTRY_NOT_IN_TABLE`, `STOP_NOT_IN_TABLE` and `TARGET_NOT_IN_TABLE` are not "
                 "raised when the reading carries the ambiguity `LEVEL_BY_REFERENCE` or the flag "
                 "`CHART_NEEDED` (the number is not in the text by nature)."), "",
            "## Verdicts after the reveal", "",
            "Each compared field carries a verdict on the SYSTEM's value, never on yours:", ""]
    out += [f"- `{c}`: {VERDICT_TEXT[c].partition(': ')[2] or 'you cannot tell'}"
            for c in VERDICTS]
    out += ["", "A field `NOT_APPLICABLE` in your reading gets `NOT_APPLICABLE`. A price row the "
            "system typed no level for gets `NOT_READ` (not read by the system: it made no claim "
            "there, so there is nothing to judge). Not compared: "
            + ", ".join(f"`{f}`" for f in NOT_COMPARED) + ". The system's empty or NONE equals "
            "your NOT_STATED or NOT_APPLICABLE; your CANNOT_DETERMINE pre-fills UNSURE. Nothing "
            "of the system's reading ever fills one of your answers.", "",
            "## The approver's decision", ""]
    out += [f"- `{c}`: {t.partition(': ')[2]}" for c, t in DECISION_TEXT.items()]
    out += ["", ("An amendment re-derives what follows from it; every field it does not amend "
                 "whose kind (asked, derived, not applicable) changes, or that was derived and is "
                 "now asked, is reset to unanswered and must be answered by the approver. "
                 "Amending `action`, `timing` or `announcement` also resets `effect` when it is "
                 "asked and not amended. A field the amendment reset (or the approver blanked) "
                 "must be answered even when the reviewer's record carries FORM_CANNOT_EXPRESS: "
                 "the escalation lifts `REQUIRED_UNANSWERED` only for the other fields."), ""]
    return "\n".join(out)


def render_guide_markdown() -> str:
    return REVIEWER_GUIDE
