# Reviewer guide

This page is self-contained: everything a reviewer needs to adjudicate a post is here. The field
reference (a separate page) lists every field and value.

## What adjudication is for

You read a publisher's trading posts and record, field by field, what each post really says: what
the publisher does, in which market, on which side, and what every number is. Your readings
become the ground truth against which the system's own reading is measured.

## Interpretation is not execution

Nothing you save places, changes or approves a trade, a rule or a mapping. The workspace has no
connection to a broker, to orders or to the system's settings. You record a reading; that is all.

## One form, one action

An instruction form records ONE action: one thing the publisher does, reports or says they will
do, to one position in one market. A post with several forms shows "Form k of m for this post".
Record the actions in the order they first appear in the post. Fewer actions than forms: each
surplus form answers "no separate action". More actions than forms: record the extras as
additional actions on the last form. Never merge two actions into one form. An opening with its
stop and target is ONE action; closing one position and opening another is TWO; the same action
in two markets is TWO; trimming and moving the stop of the rest is ONE (REDUCE with the new stop).

## The field groups

- A. What kind of statement. The TIMING of this action: CURRENT (the publisher acts as they
  announce it, in whatever tense), HISTORICAL (a report of something completed earlier, or done
  by an existing order such as a stop-out), CONDITIONAL (only if or when something happens),
  COMMENTARY (no action), UNCERTAIN. The label of this action follows from the timing (CURRENT
  is ACTIONABLE, HISTORICAL is RETROSPECTIVE). For a historical action: is this post the FIRST
  report of it, or a REPEAT of something an earlier post already told? Then the action (OPEN,
  ADD, MODIFY, REDUCE, CLOSE) and whether the post's own words state it. The label of the WHOLE
  post is the strongest label among its actions (a post may hold a historical exit, a fresh
  entry, a condition and commentary); it never changes an action's timing.
- B. Market and instrument: the market, the post's own words for the instrument, the kind of
  price series the numbers are quoted in, and what the publisher SAYS they traded.
- C. Direction and position: the side of the position the action concerns (closing a long has
  side LONG), the position immediately BEFORE THIS ACTION (from the earlier posts shown and the
  earlier actions of this same post, in text order), and how the action changes it. The form
  shows the resulting "Position AFTER this action" separately, and on a later form the position
  after your reading of the previous action in the same market.
- D. Entry (OPEN and ADD only): the one number tied to the entry, what kind of number it is, and
  how it is expressed.
- E. Stop and targets: the stop and targets THIS post communicates.
- F. Amendment (MODIFY only): what changes, to what, from what.
- G. Idea: does this action start an idea, continue it, or belong to another?
- H. The system's mapping: two opinions, asked only after the reveal.
- I. Ambiguities in the message, flags for the review, confidence and notes.

A field that does not apply disappears or is greyed with the reason. Values the form derives are
shown read-only, among them "What this means for a follower" (an instruction candidate, an exit
or a position to reconcile, no new information, wait for the condition, none). That line
describes the publisher's message. It is not an order and authorizes none.

## The price roles

Every number of the post is a row of the price table. Give each the FIRST role that applies, top
to bottom: NOT_A_PRICE (an indicator reading, a percentage, a date, a size, a profit);
OTHER_ACTION_OR_MARKET (a price of another market or of another action of the post); STOP;
TARGET; INTENDED_ENTRY; HISTORICAL_FILL; CURRENT_MARKET; AVERAGE_OR_COST_BASIS;
OTHER_CONTRACT_MONTH; CHART_LEVEL (support, resistance, a prior high or low, a trigger level);
UNRESOLVED.

No phrase has a fixed role: context decides. Ask of each number: is the publisher telling
subscribers a price to transact at (INTENDED_ENTRY)? Reporting the price they themselves got
(HISTORICAL_FILL)? Saying where the market is (CURRENT_MARKET)? Giving the average of several
entries (AVERAGE_OR_COST_BASIS)? Pointing at a level on the chart (CHART_LEVEL)? Setting a stop
or a target? Cue words such as "bought at", "limit", "the order is working" or "trading at" are
indications only. "Going long crude at 71.20" alone may be any of three: if the context leaves
two readings, answer UNRESOLVED with the flag AMBIGUOUS_PRICE_SEMANTICS; there is no default.
A freshly reported fill ("bought at 5,012 a few minutes ago") can open a position (timing
CURRENT) while its price stays HISTORICAL_FILL: a fact about the publisher's execution, never a
limit instruction. Only INTENDED_ENTRY is a price instruction. Exactness is how the number is
written: EXACT unless hedged ("around", "near", "~", a range, "4.5k").

When the same number occurs twice, the table shows "1 of 2" and "2 of 2" with a few words around
each occurrence, so you can tell them apart.

## Using the earlier posts

The earlier posts shown tell you the position before this post, the last stop, the market when
this post does not name it, and the idea. Use only the posts shown. If an answer needs a post
that is not shown, answer CANNOT_DETERMINE and set the flag MISSING_CONTEXT. Never work a
contract month out from the date, the price level or today's contracts.

## When the evidence does not settle something

Answer CANNOT_DETERMINE (UNCERTAIN for a timing, UNRESOLVED for a label or a price role) and
say why: at least one ambiguity or flag is required. Prefer CANNOT_DETERMINE and a flag to a low-confidence guess.
NOT_STATED means the publisher did not state it; CANNOT_DETERMINE means it probably exists but
the evidence does not settle it. If the form has no truthful way to record the case, set the
flag FORM_CANNOT_EXPRESS and explain in the notes.

## Saving, the reveal, resuming

1. Read the post and only the earlier posts shown.
2. Decide what the publisher does and what each number is, on your own.
3. Fill in the fields. Save lists anything unanswered or contradictory; warnings need the tick
   "I have checked these".
4. Save your independent reading. On a post with several forms, save every form first.
5. The system's reading then appears beside yours: confirm or correct each field, answer the two
   mapping questions, and save again. Change an answer only when the system's reading points to
   evidence you missed, not because it disagrees.

Every Save is a new line in the log: nothing is overwritten. An unsaved form is lost when you
leave the page. "Next item not yet adjudicated" takes you to where you stopped.

## Confidentiality

The material is paid subscriber content, shown to you for this review only. Do not copy it, take
screenshots, forward it, or paste it into any other tool or assistant. In notes, refer to
numbers by their row; do not paste the post. Your access is personal and can be withdrawn.
