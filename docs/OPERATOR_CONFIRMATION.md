# Operator confirmation of held signals

Owner decision: an optional, paper-only, off-by-default one-press confirmation for a signal held
only because its meaning is in doubt. A confirmation is an INPUT to the deterministic chain, never a
way around it: it removes the listed interpretation doubts and nothing else. It does not authorize
order submission to any real broker (`main` runs the simulated broker only).

```toml
[operator_confirmation]
enabled = false   # default. true is refused unless the broker declares SIM or PAPER
```

Version 1 offers only a CURRENT, explicit OPEN that is the only trading action of its post, with
one approved family, a side, a price reference (the stated level of an anchored entry, or the
explicit entry price) and the protective stop as read. Confirm or dismiss only: nothing the operator
enters becomes an order value.

## What happens

1. **Hold.** The post is interpreted, mapped and gated exactly as on `main`; the action holds.
2. **Pre-check** (`runtime/confirmation.py`, pure). Only with the switch on, for a held OPEN, the
   service asks the mapping once more with the table's holds waived
   (`MappingPolicy.apply_waived`, same inputs as `apply`). `offer_for` then requires: the post
   passes `gate_source` and is not a duplicate; one trading action; every doubt the waived run
   stepped over, and every interpreter ambiguity, in the closed table; the waived run SUPPORTED with
   one instrument in an approved family; side, stop and price reference present; a dry gateway run
   of the action a confirmation would release fails on nothing time cannot change (an allow-list of
   market-dependent codes, `MARKET_DEPENDENT`; anything else is "not confirmable"); not expired.
   Every doubt in the CONFIRMABLE tier gives an **offer**; anything else gives a journaled reason.
3. **Offer.** Stored in `confirmation_offers` (runtime store; the table exists only once the switch
   has been on), journaled `CONFIRMATION_OFFERED`, alerted `SIGNAL_CONFIRMABLE` on the existing
   channels, shown as a card on *Exceptions* and by `tb_operator.py offers`.
4. **Confirm.** One press on the card (or `tb_operator.py confirm <id> <hash8> --confirm "CONFIRM
   <id> <hash8>"`) queues `CONFIRM_SIGNAL` with the offer id and its full hash; the note is
   optional. The running service applies it at the start of its next cycle.
5. **Apply.** The offer is re-checked (state OFFERED, hash, tier, not expired, broker SIM or PAPER,
   no halt, the family entry and the lexicon unchanged by hash, same session date, no other revision
   on record), moved OFFERED -> CONFIRMED -> APPLYING, and the action is evaluated by
   `BrokerEventReplay.process_confirmed`: a new evaluation pass of the same post revision that runs
   `gate_source`, the gateway on a fresh quote and the execution core unchanged. The order ref is
   `order_ref_for(source_id, revision, action_id)`, so the core's own idempotency allows one order
   at most. The outcome ends the offer: EXECUTED (it reached the execution core), HELD (the gateway
   held it, e.g. the market moved) or REJECTED (refused, e.g. exposure, account, a stale ledger).

**An approximation mark adds no tolerance.** The stated number is the anchor as written; the
family's collar, move limits and quote rules apply unchanged against the fresh quote.

## The closed table (`runtime/confirmation_table.py`)

Default deny: a (code, detail) pair not written there is never stepped over, never offered.

| Tier | Code (detail key) | Plain words on the card |
|---|---|---|
| CONFIRMABLE | `ENTRY_ANCHOR_POST_UNRESOLVED_MARKET` (`RULE:MENTION_WITH_CONTEXT`) | an unknown market name next to a price or trading word |
| CONFIRMABLE | `ENTRY_ANCHOR_NOT_CURRENT` (`NO_PRESENT_MARKER`) | the level is not marked as the current price |
| CONFIRMABLE | `ENTRY_ANCHOR_CONTEXT_UNCLEAR` (`WORDS`, `OWN_CLAUSE`) | a word of the level's or the instruction's sentence is not understood |
| CONFIRMABLE | `ENTRY_ANCHOR_INSTRUMENT_UNCLEAR` (`OTHER_WORDS`, `PRECEDING_SENTENCE`) | whose level it is: a word of the level line or the sentence before |
| CONFIRMABLE | `ENTRY_ANCHOR_LEVEL_NOT_MARKET_QUOTE` (`FOLLOWING_`, `INSTRUCTION_`, `NEIGHBOUR_NOT_UNDERSTOOD`) | a neighbouring or instruction sentence is not understood |
| CONFIRMABLE | `ENTRY_ANCHOR_LEVEL_NOT_MARKET_QUOTE` (`OWN_CLAUSE:APPROXIMATION_MARK`) | the level carries "~" and nothing else |
| REPORT_ONLY | `HEADLINE_POSITION_LINE_WITHOUT_ENTRY_VERB`, `POSITION_STATEMENT_OR_ENTRY` | entry or position summary |
| REPORT_ONLY | `ENTRY_ANCHOR_POST_UNATTRIBUTED_ACTION` (`RULE:UNATTRIBUTED_ACTION_SENTENCE`) | another sentence holds an instruction word |
| REPORT_ONLY | `ENTRY_ANCHOR_NOT_CURRENT` (`NOT_CURRENT_WORD`), `..._LEVEL_NOT_MARKET_QUOTE` (`LEVEL_KIND:ENTRY_OR_COST_BASIS`, `LEVEL_KIND:HISTORICAL`), `..._CONTEXT_UNCLEAR` (`QUESTION`) | positive evidence the level is something else, or a question |

REPORT_ONLY is never offered: only the evaluation hook reports it ("would be offered if the owner
allowed this tier", `NoOffer.reason = TIER_NOT_ALLOWED`, `would_be`), so the owner decides with
counts. Never (not in the table): every mapping, contract, option, stop, collar, quote, exposure,
age, environment, halt, reconnect, source (edited, unverified, truncated, before first start,
duplicate) and unreadable-number code, `ENTRY_PRICE_IS_COST_BASIS`, several levels or markets or
actions, `SIDE_UNCLEAR`, `SECOND_NUMBER`, `LEVEL_KIND:STOP_OR_TARGET`, `STANDALONE_LEVEL_NOT_ENABLED`,
`RULE:ADDITIVE_ELLIPSIS`, and `CONTEXT_UNKNOWN` (moved from the design draft: once stepped over, the
later checks cannot run, so it is never waived). A test checks every code named exists, the
never-list is absent and an unknown pair raises.

## The waiver's safeguards (`runtime/mapping.py`)

1. `resolve_waived(action, *, waive, ...)` / `apply_waived(parsed, action_id, *, waive, ...)` take
   `waive` keyword-only and accept only pairs of the closed table; anything else raises.
2. The production decision never passes one: `resolve` and `apply` have no such argument. With the
   switch off the waived evaluation is never run (test patches it to raise).
3. The result is a `WaivedResolution`, never a `MappingDecision`; `ensure_production` refuses it
   on the ordinary gate path (`_gate_quotes`). The replay's ordinary `process` treats the post as a
   duplicate; `process_confirmed` requires a `ConfirmationAuthority`, which `confirmation.authorize`
   issues only for an APPLYING offer of the CONFIRMABLE tier, intact and unexpired, and which
   permits exactly that post revision and that action.
4. Each check steps over only its own hold, at its own site, records it and returns to the chain
   with the inputs unchanged; the checks after it run as on `main`. `INSTRUCTION_NOT_UNDERSTOOD` is
   stepped over only when every instruction sentence fails on unknown words alone (never a
   question or a second number).

## Binding, expiry, state machine

The hash binds the post identity and revision, its sha256, the typed proposal (with the
interpreter's evidence replaced by its hash), the family, execution instrument, basis, price
reference, stop, target, the doubts and tier, the publication, receipt and expiry times, the entry
terms hash, the lexicon hash and the session date. Expiry is the existing signal age limit, from
publication (anchored: the family's `max_signal_age_seconds`, 90 s for TLT, and the policy's, the
smaller; explicit: the policy's), unchanged; `gate_confirmed` and the apply step refuse at or after
it.

`OFFERED` -> `CONFIRMED` -> `APPLYING` -> `EXECUTED` | `HELD` | `REJECTED`; `OFFERED` -> `DISMISSED`
| `EXPIRED` (journaled `unanswered = true` when no one answered) | `INVALIDATED` (`REVISION`,
`LATER_INSTRUCTION`: a later post's trading action on the offer's idea or family, `HALT`,
`MAPPING_CHANGED`, `LEXICON_CHANGED`, `DATE_CHANGED`, `ENVIRONMENT_NOT_SIM_OR_PAPER`,
`FEATURE_DISABLED`, `OFFER_RECORD_CORRUPT`). Every exit is final. A request with another hash is
refused `PROPOSAL_CHANGED`; one for an offer no longer open `OFFER_NOT_OPEN:<state>`. An offer
found CONFIRMED or APPLYING at start is never evaluated again: the execution core's recovery
decides an intent that exists (`RECOVERED_BY_EXECUTION_CORE`), otherwise `APPLY_INTERRUPTED`.

**Clock- and date-dependent checks.** The mapping reads the date only for futures contract
selection (named months, nearest month, front month, the roll margin; codes
`CONTRACT_SELECTION_AMBIGUOUS`, `CONTRACT_MONTH_UNRECOGNISED`, `FRONT_MONTH_*`, all never-listed).
The selected contract is in the hash and the apply step refuses another session date
(`DATE_CHANGED`), so the selection is the one the same inputs give. Everything else that depends on
time (source freshness, the anchor age, the session window, the roll window, quote ages) is in the
gateway or the execution core and runs again at apply. TLT is a stock: no contract date at all.

## Latency (90 s budget, from publication)

X exposes a post in 12.4 s (median; p95 13.4 s, G0). Decision, offer and alert take under 1 s in
the same cycle. The queue is applied at the next cycle start: up to one cycle (5 s recommended for
X, 1 s in the shadow example). That leaves the owner about 70 s. An anchored entry also needs, at
apply, a LIVE quote with each side at most 3 s old and the market within 0.3% adverse / 0.6%
favourable of the stated level; a slow confirmation often ends HELD with a move code. A longer
window for confirmed signals would be a trading-behaviour change for the owner: a new limit, a raised
gateway ceiling (`ANCHOR_MAX_SIGNAL_AGE_CEILING_SECONDS`, 120 s), the policy's source freshness and
the core's intent age. Not built.

## Journal and privacy

Events (`service_events`): `CONFIRMATION_OFFERED`, `CONFIRMATION_NOT_OFFERED` (holds carrying a
table doubt; reason, detail, tier), `OPERATOR_CONFIRMED_SIGNAL`, `OPERATOR_DISMISSED_SIGNAL` (with
the operator, request id and origin), `CONFIRMATION_EXPIRED`, `CONFIRMATION_INVALIDATED`,
`CONFIRMATION_RESULT` (status, reasons, order ref), `CONFIRMATION_PRECHECK_FAILED` (exception
type only; no offer). Identifiers, codes, numbers and times only: no post text is stored anywhere
(test scans every table, the alerts file, the notification log and the card for an invented post's
words).

## Evaluation hook (pure)

```python
from tradingbot.runtime import confirmation as C, confirmation_table as T
waived = mapping.apply_waived(parsed, action_id, waive=T.WAIVE_ALL, **same_kwargs_as_apply)
out = C.offer_for(C.HeldDecision(..., production=decision, waived=waived, quote=..., now=...))
gate = C.gate_confirmed(out, fresh_quote, policy, now=later)   # for an Offer (or out.would_be)
```

`offer_for` returns an `Offer` (CONFIRMABLE) or a `NoOffer(reason, detail, tier,
confirmable_doubts, would_be)`. `gate_confirmed` is the gateway only (no execution-core caps).

## Proof (tests)

| Invariant | Test |
|---|---|
| Off equals main | `test_operator_confirmation_service`: no waived run, table, event or alert with the switch off; the mock scenarios and the shadow call trace replayed with the switch off match `main` (experiment, PR notes) |
| No order package | `test_shadow_no_order_import_sdk`: the subprocess import blocker with the switch on, through a confirmed entry |
| SIM or PAPER only | refused at start for LIVE and undeclared brokers, and at apply |
| The chain still refuses | adverse move, a quote that stopped updating, exposure cap, stale ledger (reconnect pending), account check, halt, expiry, each with its own reason |
| One-shot and binding | second press, stale hash, dismiss then confirm, revision, later instruction, mapping change on restart, switch off |
| Restart in APPLYING | never evaluated again |
| Closed table, safeguards | `test_operator_confirmation_core`: codes exist, never-list absent, unknown pair raises, keyword-only, no waiver in `resolve`/`apply`, `ensure_production`, later checks run behind a waived hold |
| No tolerance | the approximation's anchor is the stated number; 0.31% away holds `ENTRY_ANCHOR_ADVERSE_MOVE` |
| No post text | every row, event, alert, log line and card |

AGENTS.md rule 1 is worded for this capability. The apply step and `process_confirmed` are order-path
changes (rule 4): they need the independent review before merge.
