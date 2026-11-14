# Operator confirmation of held signals (DESIGN DRAFT, not implemented)

Owner decision: an optional, paper-only, off-by-default one-tap confirmation for a signal held only
for interpretation ambiguity. A confirmation is an input to the deterministic chain, never a way
around it. No order submission through it without separate authorization.

## 1. What is persisted today, and what must be added

A post is interpreted, mapped (`MappingPolicy.apply`), gated and committed in one pass
(`RuntimeService._handle`); the cursor then moves past it. What survives a held action:
`signal_actions` (action, timing, asset, side, mapping status/basis/reason, status, reasons, no
prices, no instrument), a `SIGNAL_<status>` row in `runtime_exceptions`, the outcome in
`replay_actions`, and the interpreter context (the publisher's book: a present-tense OPEN is recorded
as idea `<source_id>:<action_id>` with its stop and target even when the follower held it). The
typed `TradeAction`, the `MappingDecision` (selected instrument, candidate anchor) and the post text
are not kept. Two facts shape the design:

* **The mapping stops at the first failing check.** `entry_anchor_for`, `_resolve` and `resolve`
  return on the first problem, so a hold says nothing about the checks after it. To know whether
  confirming would leave another blocker, the pre-check must evaluate the action again with the
  confirmable problems *waived* (recorded and stepped over, at their own sites only) and see what
  the rest of the chain says.
* **Post text is gone after the pass.** The text-dependent mapping checks (stop uniqueness, cost
  basis, unexplained prices, contract selection) can be run only during the pass that has the text.

So: (a) `MappingPolicy.resolve(..., waive=CONFIRMABLE)`, a second evaluation run only for a held
action and only when the feature is on; the production decision never runs with a waiver. (b) A
new `confirmation_offers` table in the runtime store, created only when the feature is on: the typed
proposal (no text), the waived codes, the pre-check result, the hash, the state and timestamps, and
the latest quote view (the service refreshes it each cycle for the card). (c) At apply time the
service rebuilds the typed action from the offer (the original ambiguity marks minus exactly the
confirmed codes, the waived run's instrument, the candidate anchor on the `MappedQuote`), re-checks
the family entry (still approved, terms hash unchanged) and the lexicon hash, reads a fresh quote
(`_gate_quotes`), and runs `BrokerEventReplay` as a new `attempt` of the same `(source_id,
revision)` (the column hydration already uses), re-running `gate_source` (an edit on record holds it)
and `gate_action`, then the coordinator. The order ref stays `order_ref_for(source_id, revision,
action_id)`, so the coordinator's own idempotency makes the action executable at most once.

## 2. Reason codes

Default deny: `CONFIRMABLE` is a closed table of (code, detail) pairs; anything else, including any
code added later, is not confirmable. The table is in `runtime/confirmation.py` with a test that
every code it names exists and every code in the never-list below is absent.

**Confirmable (interpretation only; the level, side and stop are read and unique).**

| Code (detail) | Why confirmable |
|---|---|
| `ENTRY_ANCHOR_POST_UNRESOLVED_MARKET` (`RULE:MENTION_WITH_CONTEXT`) | an unknown market word near the level: the owner can tell whether it is another market |
| `ENTRY_ANCHOR_NOT_CURRENT` (`NO_PRESENT_MARKER` only) | the level is not marked as the current price; whether it is the market now is judgement |
| `ENTRY_ANCHOR_CONTEXT_UNCLEAR` (`WORDS:<n>`, `OWN_CLAUSE`) | a word next to the level is not on the allow-list |
| `ENTRY_ANCHOR_INSTRUMENT_UNCLEAR` (`OTHER_WORDS:<n>`, `PRECEDING_SENTENCE`) | a level line holds an unknown word; whose level it is is judgement |
| `ENTRY_ANCHOR_LEVEL_NOT_MARKET_QUOTE` (`LEVEL_KIND:AMBIGUOUS` with `FOLLOWING_`, `INSTRUCTION_`, `NEIGHBOUR_NOT_UNDERSTOOD`, `CONTEXT_UNKNOWN`, or `OWN_CLAUSE` from an approximation mark) | a neighbouring or instruction sentence is not understood, or the number carries "~" |
| `ENTRY_ANCHOR_POST_UNATTRIBUTED_ACTION` (`RULE:UNATTRIBUTED_ACTION_SENTENCE`) | another sentence carries an instruction word; with one trading action in the post, whether it is a second instruction is judgement (doubtful, Q2) |
| `HEADLINE_POSITION_LINE_WITHOUT_ENTRY_VERB`, `POSITION_STATEMENT_OR_ENTRY` (interpreter) | entry or position summary? (doubtful, Q2) |

**Doubtful (owner decides, Q1/Q2):** `ENTRY_PRICE_IS_COST_BASIS` (recommend never: the stated price
would change meaning, not just be confirmed); `ENTRY_ANCHOR_NOT_CURRENT` with `NOT_CURRENT_WORD:<w>`
and `LEVEL_KIND:ENTRY_OR_COST_BASIS` or `HISTORICAL` (recommend never: the reader found positive
evidence that the level is something else); `ENTRY_ANCHOR_CONTEXT_UNCLEAR` (`QUESTION`) (recommend
never: a question is not an instruction).

**Never**, each because confirming would supply a value, choose among alternatives, or override a
deterministic or safety rule:
* mapping and contract: `UNSUPPORTED_INSTRUMENT_MAPPING`, `MAPPING_NOT_APPROVED`, `BASIS_UNSUPPORTED`,
  `SOURCE_ASSET_UNRESOLVED`/`_NOT_MAPPED`, `INTERPRETED_INSTRUMENT_DIFFERS_FROM_MAPPING`,
  `CONTRACT_SELECTION_AMBIGUOUS`, `CONTRACT_MONTH_UNRECOGNISED`, `FRONT_MONTH_*`, `OPTION_TERMS_INCOMPLETE`,
  `OPTION_STRATEGY_NOT_SINGLE_LEG`, `IDEA_CONTRACT_MISMATCH`, `MAPPING_LONG_ONLY`,
  `SHORT_NOT_PERMITTED_FOR_FAMILY`, `ENTRY_ANCHOR_RULE_INCOMPLETE`, `_LIMIT_ABOVE_CEILING`, `_BASIS_CAP_MISSING`;
* several candidates (v1 has no picking): `ENTRY_ANCHOR_SEVERAL_LEVELS`, `_SECOND_PRICE`,
  `_SOURCE_AMBIGUOUS`, `_INSTRUMENT_AMBIGUOUS`, `_UNEXPLAINED_PRICE`, `_REFERENCE_UNNAMED`,
  `_POST_SEVERAL_LEVELS`/`_MARKETS`/`_ACTIONS`, `CONTEXT_UNCLEAR` (`SECOND_NUMBER`), `SIDE_UNCLEAR`,
  `MULTIPLE_OPEN_IDEAS_IN_CONTEXT`, `EXISTING_OPEN_IDEA_SAME_SIDE_IN_CONTEXT` (a duplicate entry risk);
* unreadable or missing: `ENTRY_ANCHOR_UNREADABLE`, `_NOT_IN_POST`, `_NOT_STATED`, `ENTRY_TEXT_*`,
  `OPEN_ENTRY_PRICE_MISSING`, `ASSET_UNRESOLVED`, `INVALID_STOP_PRICE`, `NO_OPEN_IDEA_IN_CONTEXT`,
  `LEVEL_KIND:STOP_OR_TARGET`, `STANDALONE_LEVEL_NOT_ENABLED` (a family switch, not an ambiguity),
  `POST_UNRESOLVED_MARKET` (`RULE:ADDITIVE_ELLIPSIS`: the post may continue);
* stops: `PROTECTIVE_STOP_MISSING`, `PROTECTIVE_STOP_NOT_UNIQUE`, `PROTECTIVE_STOP_TOO_FAR_FROM_*`,
  `STOP_DIRECTION_CONTRADICTION`, `STOP_ALREADY_CROSSED_AT_ENTRY`;
* timing and authority: `TEMPORAL_INTENT_UNCERTAIN`, `CONDITIONAL_TRIGGER_NOT_IMPLEMENTED`,
  `FUTURE_INTENT_WITHOUT_EXPLICIT_CONDITION`, `NOT_EXPLICITLY_AUTHORITATIVE`;
* source: `SOURCE_EDIT_REQUIRES_REVIEW`, `SOURCE_NOT_AUTHENTICATED`, `SOURCE_TEXT_TRUNCATED`,
  `SOURCE_HASH_CHANGED_WITHOUT_REVISION`, `POST_BEFORE_FIRST_START`, duplicates, `SOURCE_STALE_OR_FUTURE`,
  `ENTRY_ANCHOR_SIGNAL_TOO_OLD`;
* market and risk: every `QUOTE_*` and `ENTRY_ANCHOR_QUOTE_*`, `INVALID_BID_ASK`, `SPREAD_TOO_WIDE`,
  `ENTRY_PRICE_COLLAR`, `ENTRY_ANCHOR_ADVERSE_MOVE`/`_FAVOURABLE_MOVE`, `NOTIONAL_LIMIT`,
  `UNIT_CAP_OR_INCREMENT`, the coordinator's exposure caps, `OPPOSITE_IDEA_IN_SAME_CONTRACT`;
* environment and state: `ENTRY_ANCHOR_PAPER_ONLY`, `BROKER_ACCOUNT_MISMATCH`, `BROKER_UNREADABLE`,
  `BROKER_RECONNECT_PENDING`, `HALTED` and the halt codes, the identity lockout.

## 3. When an offer exists (deterministic pre-check, repeated at apply)

Offered only if all hold: feature on, mode mock or shadow, broker declares SIM or PAPER; the post
passes `gate_source` and is not before first start or a duplicate; the post has exactly one trading
action and it is a CURRENT, explicit OPEN (ADD, exits and MODIFY are out of v1); every hold code on
it is in `CONFIRMABLE`; the waived mapping run is SUPPORTED with one instrument in an approved
family, a side, a price reference (the candidate anchor or the explicit entry) and the stop as read;
and a dry `gate_action` of the waived action on the decision's own quote fails on nothing that time
cannot change (stop direction and distance from the stated level, unit cap, side permitted). A
market-dependent failure at that moment (quote age, collar, adverse move) does not prevent the offer;
the card shows it. Otherwise the hold is shown as **not confirmable**, with its reasons.

## 4. Offer state machine

`OFFERED` -> `CONFIRMED` (request applied) -> `APPLYING` -> `EXECUTED` | `HELD` | `REJECTED`
(the gateway's or coordinator's outcome and reasons, recorded on the offer and the signal).
`OFFERED` -> `DISMISSED` (operator) | `EXPIRED` (signal age limit) | `INVALIDATED` with
`REVISION` (any other revision of the post), `LATER_INSTRUCTION` (a later action whose
`idea_reference` is the offer's idea, or any later trading action in the same family),
`HALT` (halt engaged), `MAPPING_CHANGED` / `LEXICON_CHANGED` (hash differs after a restart),
`FEATURE_DISABLED` (switched off). Every exit from `OFFERED` is final (one-shot). A request whose
hash differs from the offer's is refused `PROPOSAL_CHANGED`; one for a non-`OFFERED` offer is refused
`OFFER_NOT_OPEN`. An `APPLYING` offer found at start is never re-run: the coordinator's recovery
decides the intent if one exists; otherwise it ends `REJECTED` (`APPLY_INTERRUPTED`).

Binding hash: sha256 of the canonical JSON of source id, post id, revision, post hash, action id,
action, side, family and entry-terms hash, execution contract key, price reference, stop, target,
waived (code, detail) list, published time and expiry. Expiry: `published_at` + the action's existing
limit (anchored: the family's `max_signal_age_seconds`, 90 s for TLT; explicit: the policy's
`max_signal_age_seconds`), unchanged; the gateway enforces it again at apply.

## 5. Latency (90 s budget, measured from publication)

| Step | Time |
|---|---|
| X exposes the post (G0, median / p95) | 12.4 s / 13.4 s |
| Decision, offer, alert (end of the same cycle; macOS notification at once) | under 1 s |
| Owner notices, opens the card, decides | the remainder |
| Operator queue applied at the next cycle start (cycle = source poll interval: 5 s recommended, 1 s in the shadow example, longer under an X rate limit) | up to 5 s |
| **Left for the owner** | **about 70 s** |

The queue is fast enough; no new channel is needed. The real limit is the human: the feature is
useful only when the owner is at the screen with notifications on. An anchored entry also needs, at
apply, a LIVE quote with each side at most 3 s old and the market within 0.3% adverse / 0.6%
favourable of the stated level, so a slow confirmation often ends `HELD` with a move code. A longer
window for confirmed signals is a trading-behaviour change for the owner: it would need a new
per-family limit, raising the gateway ceiling `ANCHOR_MAX_SIGNAL_AGE_CEILING_SECONDS` (120 s), the
policy's source freshness and the coordinator's intent age, and an evaluation of the stale-level risk.
Not designed in.

## 6. Failure modes

| Failure | What happens |
|---|---|
| Queue missing or unreadable | nothing applies (`OPERATOR_QUEUE_UNAVAILABLE`); offers expire |
| Broker unreadable or reconnect pending | requests are not applied that cycle, or the coordinator refuses the OPEN; the offer ends `REJECTED` or expires |
| Halt engaged | every open offer `INVALIDATED HALT`; the coordinator refuses anyway |
| Service restart | offers persist; still valid only if not expired and the mapping and lexicon hashes match |
| `tb_operator.py apply` with the service stopped | confirm refused `CONFIRM_NEEDS_RUNNING_SERVICE` (no live quote path) |
| Stale card, double confirm (CLI and page) | `PROPOSAL_CHANGED`, `OFFER_NOT_OPEN` |
| Crash during apply | the same order ref; the coordinator adopts or refuses the intent; never a second order |
| Later post arrives after the confirm was applied | handled as for any open idea (the publisher's book already holds the idea), e.g. a CLOSE exits it |
| Broker declares LIVE or nothing | refused at start (exit 2) and at apply |

## 7. Surface, journal, alerts

Card per offer (Exceptions page): proposed action and side; source family, reference series,
execution instrument; price reference (stated level or explicit entry), basis treatment, the latest
quote with its age and quality, deviation from the level and whether the move limits pass now; stop;
target; uncertainty in plain words, one sentence per waived code; the hold reasons; time left; the
post's identity (post id, revision, published time). Never post text. Buttons Confirm and Dismiss
queue `CONFIRM_SIGNAL` / `DISMISS_SIGNAL` with the offer id and hash; CLI `tb_operator.py offers`,
`confirm <offer> <hash8>`, `dismiss <offer> <hash8>`. Journal events (`service_events`, identifiers,
codes, times, operator): `CONFIRMATION_OFFERED`, `CONFIRMATION_NOT_OFFERED` (only for holds carrying
at least one confirmable code), `OPERATOR_CONFIRMED_SIGNAL`, `OPERATOR_DISMISSED_SIGNAL`,
`CONFIRMATION_EXPIRED`, `CONFIRMATION_INVALIDATED`, `CONFIRMATION_RESULT` (status, reasons, order ref).
Alert `SIGNAL_CONFIRMABLE` (key `OFFER:<id>`) on the existing channels, resolved when the offer ends.

AGENTS.md rule 1: the capability adds no order path (the action still comes only from a gateway
approval through `ExecutionCoordinator`), but it is not "recording a broker fact" or "lifting a
block" as those words were meant. Proposed: "Operator tools only record what a human established at
the broker, lift a block, or, where `[operator_confirmation]` is enabled in a simulated or paper mode,
confirm the reading of a held signal whose only hold reasons are on the closed list in
docs/OPERATOR_CONFIRMATION.md; the confirmed action passes the gateway and the coordinator again,
unchanged, and nothing an operator enters becomes an order value." OPERATIONS.md ("no operator action
below sends an order") and the `operator_actions` docstring change to match. Rule 4 applies: the new
replay attempt and the service's apply step are order-path changes and need independent review.

Evaluation hook (pure, no I/O): `offer_for(HeldDecision) -> Offer | NoOffer(reason)` where
`HeldDecision` holds the typed action, the production and waived mapping decisions, the outcome,
the post-level facts and the dry gate; and `gate_confirmed(offer, quote, position, policy, now)`, the
gateway on the rebuilt action (no coordinator caps), for counting how many would pass.

## 8. Proof

* Off = main: unit test that no waiver run, table, event or alert exists; the mock scenarios and the
  shadow call trace replayed with the switch off give byte-identical journals and wire traces; the
  subprocess import blocker for `tradingbot.ibkr_orders` still passes with the switch on.
* SIM/PAPER gate: start refused for LIVE and undeclared brokers; apply refused if it changes.
* Chain unchanged: for every gateway and coordinator refusal (collar, quote age, stop distance, caps,
  age limit, halt, reconnect, account) a confirmed offer ends with that reason; a property test that
  a confirmed action is approved only if `gate_action` approves the rebuilt action on the same inputs.
* Closed table: every named code exists; the never-list is absent; an unknown code is refused; adding
  a code to the mapping does not make it confirmable.
* One proposal: posts with two actions, an ADD, an exit, two stops, or a confirmable plus a never
  code yield "not confirmable" with the right reason; the waived run finds blockers behind a
  confirmable first problem.
* Binding: revision, later instruction, halt, expiry, mapping or lexicon change, feature off all end
  the offer; wrong hash and second confirm refused; restart in `APPLYING` never re-runs.
* Journal and text: every transition journaled with operator and codes; a scan that no offer row,
  event, alert or card carries a word of an invented post.
* Hook: `offer_for` gives the same answer as the service on the invented fixtures.

## 9. Files and size (rough)

New `runtime/confirmation.py` (table, pre-check, hash, hook, about 400 lines); `runtime/mapping.py`
(waiver at the confirmable sites, about 80); `runtime/service.py` (offer, refresh, invalidate, apply,
about 200); `runtime/store.py` (offers table, about 80); `event_replay.py` and `execution/replay.py`
(the confirmed attempt, about 80); `operator_actions.py`, `operator_apply.py`, `execution/operator_catalog.py`,
`scripts/tb_operator.py` (about 120); `runtime/config.py`, `notify.py`, `alerts.py` (about 50);
`dashboard/pages/9_Exceptions.py` (card, about 200); docs (this file, RUNTIME, OPERATIONS, AGENTS);
tests about 1,000 lines. About 1,300 lines of production code.

## 10. Open questions for the owner

1. **Cost basis** (`ENTRY_PRICE_IS_COST_BASIS`): confirmable? Recommend never in v1.
2. **Entry or summary, and an unattributed instruction sentence**
   (`HEADLINE_POSITION_LINE_WITHOUT_ENTRY_VERB`, `POSITION_STATEMENT_OR_ENTRY`,
   `ENTRY_ANCHOR_POST_UNATTRIBUTED_ACTION`): confirmable? Recommend yes, only with one trading action.
3. **The 90 s window**: keep it for the evaluation? Recommend yes; count unanswered expiries first.
4. **One tap**: existing actions need a typed phrase and a note. Recommend a single press on the card
   (the request carries the hash; note optional), and the typed `confirm <offer> <hash8>` on the CLI.
5. **Text-dependent mapping checks**: run once at offer time and bound by hash (recommended), or keep
   the post text in memory until expiry so the whole mapping re-runs at apply? Recommend the former:
   no text is retained, and the checks depend only on the immutable revision, the mapping and the
   lexicon, all of which are re-checked by hash at apply.
