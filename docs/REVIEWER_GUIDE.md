# Reviewer guide

## Purpose and Confidentiality

You read a publisher's trading posts and record what each one says. Your readings are the ground
truth the system is measured against. Nothing you save places, changes or approves a trade, a rule
or a mapping (the system's proposal of which instrument would follow an idea). "What this means
for a follower" describes the publisher's message; it is not an order.

The posts are paid subscriber content, shown to you for this review only. Do not copy them, take
screenshots, forward them, or paste them into any other tool or assistant. Your access is personal
and can be withdrawn.

## Forms and fields

An instruction form records ONE action: one thing the publisher does, reports or plans, on one
position in one market. Record a post's actions in the order they appear, one per form. Closing
one position and opening another are two actions. With fewer actions than forms, a surplus form
answers "no separate action"; extra actions go in "further actions" on the last form. A label-only
form labels a whole post the system did not read as an instruction: list the actions you find as
further actions; the post takes the strongest of their labels (ACTIONABLE, CONDITIONAL,
RETROSPECTIVE, UNRESOLVED, COMMENTARY).

Field groups: timing and action; market and instrument; side, the position BEFORE THIS ACTION
(earlier posts shown, earlier actions of this post) and effect; entry; stop and targets;
amendment; idea (one position, opening to final close); the system's mapping, after the reveal;
ambiguities, flags, confidence, notes. A field that does not apply is greyed. Tooltips and the
Field reference page explain every value.

## Timing and prices

The post's own time words decide the timing. CURRENT: happening now or just done ("a few minutes
ago"); a plain past with no time word is CURRENT too ("Bought gold at 2,395."). HISTORICAL: the
post places the action earlier ("this morning", "overnight"), or an existing order did it (a
stop-out). CONDITIONAL: only if something happens later. Otherwise UNCERTAIN. A historical action
is a FIRST_REPORT (this post first tells subscribers) or a REPEAT (an earlier post shown announced
it; the position before is then the one after that announcement). "We bought gold this morning at
2,395." is a historical first report: a position to reconcile, not an instruction.

Each number of the post is a row of the price table (in restricted access: each level the system
typed). Give it the first role that applies, from the top of the list. Context decides, never a
phrase alone. A price actually obtained is HISTORICAL_FILL, even at the stop
("stopped out at 2.84"); a level named as the stop is STOP ("the 69.50 stop was hit, filled
69.40": 69.50 STOP, 69.40 HISTORICAL_FILL). Only INTENDED_ENTRY, a price to transact at, is
an instruction. If two readings remain, answer UNRESOLVED with the flag AMBIGUOUS_PRICE_SEMANTICS;
there is no default.

## Earlier posts and doubt

Use only the earlier posts shown. The position before is FLAT when those posts, or the post's own
words (a new position), establish that none existed. When they do not settle it, answer
CANNOT_DETERMINE with the flag MISSING_CONTEXT, never FLAT. NOT_STATED means the publisher did not
state it. When the evidence does not settle an answer, say so (CANNOT_DETERMINE; UNCERTAIN for
a timing; UNRESOLVED for a label or a role) and add an ambiguity or a flag. Never guess. If the
form cannot record a case truthfully, set FORM_CANNOT_EXPRESS, explain in the notes and leave the
rest unanswered.

## Saving, the reveal, resuming

Save lists anything unanswered or contradictory; warnings need the tick "I have checked these". An
unsaved form is lost when you leave the page. Once every form of a post has your independent
reading, the system's reading appears beside yours. Each compared field (shown beside the
system's value) then gets a verdict on the SYSTEM's value: CONFIRM (it is right), CORRECT (it is
wrong, yours is right) or UNSURE. A row shown as NOT_READ (the system typed no level there) needs
none. Change your own answer only for evidence you missed, not because the system disagrees.
Every Save is a new line; nothing is overwritten. "Next item not yet adjudicated" resumes.
