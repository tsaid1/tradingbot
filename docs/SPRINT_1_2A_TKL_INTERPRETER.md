# Sprint 1.2A — TKL signal interpreter and versioned source events

**Status:** offline, deterministic, advisory. The interpreter authorizes nothing; every action it emits is decided by `validator.py` through the Sprint 1.1 replay coordinator, which also owns sizing and the journal. **Live execution is impossible** in this code. No subscriber text ships in the repository; all fixtures are invented.

## 1. Versioned source-event interface (`src/tradingbot/source_events.py`)

`SourcePost` (schema `1.2`) is what the Source Integration Agent produces from the G0 feed. It is the only object the interpreter reads. Rules come from `docs/research/G0_EVENT_SCHEMA.md`.

| Field | Meaning | Expected from the Source Agent |
|---|---|---|
| `source_id` | Root of the edit chain: the logical post | First element of `edit_history_ids`, never the current ID |
| `post_id`, `revision` | This version and its position in the chain (1 = original) | An edit is revision 2 under the **same** `source_id` |
| `published_at` | Original publication, UTC | API `created_at` for revision 1; snowflake time of the root ID otherwise |
| `revised_at` | The edit's own time (revision > 1) | API `created_at` of the edited version |
| `received_at` | Receipt by the poll, or archive time | Real receipt for live posts; archive time otherwise (not a latency) |
| `provenance` | `LIVE_API_USER_CONTEXT`, `ARCHIVE_API_USER_CONTEXT`, `MANUAL`, `SYNTHETIC` | Live vs backfill decides how replay sets `now` |
| `author_id`, `verified_author` | Author as returned; equality with the expected account **and** the pinned timeline endpoint | Not cryptographic proof; the adapter validates session and endpoint |
| `text`, `text_complete` | Full body (long-post field when present); completeness per the archive rule | Retweets are never complete; non-retweet ellipsis without a long body is truncated |
| `is_repost` | Retweet | From `referenced_tweets.type == retweeted` |
| `conversation_id`, `links` | Thread root and quoted / replied-to / retweeted post IDs with a `text_available` flag | Quoted text is **not** in the archive; fetch it or leave the flag false |
| `media` | Media keys, type, URL, local file, optional hash | Charts may carry the instruction; `text_complete` says nothing about them |

Helpers: `to_replay_source()` (trusted metadata, `post_hash` = SHA-256 of the exact text), `to_legacy_source_event()` (Sprint 1 demo pipeline, revision 1 only), `from_g0_record()` (the archive mapping, tested on synthetic records). The model forbids extra fields, so no sizing or routing can ride on a source event.

## 2. Interpreter (`src/tradingbot/tkl_interpreter.py`)

`TklInterpreter.interpret(post)` is pure: it reads the context as of `post.published_at` and returns an `InterpretedPost` with a `ParsedPost` (zero or more `TradeAction` children, unchanged Sprint 1.1 schema) and one `Classification` per clause. `observe()` advances the context afterwards; `ingest()` does both.

**Clauses.** Text is split into sentences, then a sentence is cut before a second action verb ("adding to our long at X **and** raising our stop to Y"). Bare level lines ("Stop: 86. Target: 95.") attach to the instruction before them.

**Classes and what they become.**

| Message class | Tense | `TradeAction` | Gateway result |
|---|---|---|---|
| OPEN, ADD, REDUCE, CLOSE, MODIFY | present | same action, `CURRENT` | preflight, may APPROVE |
| CONDITIONAL ("if", "should", "once", "on a break of", "will exit if") | future | underlying action, `CONDITIONAL` | HOLD |
| RETROSPECTIVE ("we have closed", "stopped out", "we took profits", "we are long X from Y") | past | reported action, `HISTORICAL` | RECONCILE, no order |
| CANCEL ("no longer looking to", "cancelling") | present | `NO_TRADE` | NO_TRADE; pending intent withdrawn in context |
| COMMENTARY ("we remain", "could", "expect", forecasts without an instruction) | present | `NO_TRADE`, `COMMENTARY` | NO_TRADE |
| POSITION_SUMMARY ("Current positions:", listed positions) | present | single `NO_TRADE`, `COMMENTARY` | NO_TRADE |

Tense is decided per clause from the verb form. A bare participle in the publisher's headline style ("Stop raised to 90") is a current instruction; "we have raised our stop" is a report.

**Price roles.** ENTRY, STOP, TARGET, EXIT, TRIGGER, REFERENCE, PNL_PERCENT, assigned from the words before each number. Only ENTRY, STOP, TARGET and the reduction fraction reach the `TradeAction`; the rest are evaluation detail.

**Instruments.** An explicitly named ticker (`$TLT`, or a known ETF symbol) becomes an `Instrument` with `TRADED_CONTRACT` price basis. Index, spot and futures names (S&P 500, gold, natural gas, oil, Bitcoin, ...) become a source series (`SPX`, `GOLD`, `NATGAS`, `WTI`, `BTC`, ...) with **no** instrument, so the gateway holds them. The interpreter never maps a series to an ETF. A clause naming both a series and a ticker is ambiguous.

**Point-in-time context.** `PointInTimeContext` is the interpreter's view of the publisher's book, keyed by idea IDs that match the coordinator's (`source_id:action_id`). Every lookup is filtered by `as_of = post.published_at`: an idea opened later, or closed later, is invisible, so a post is always interpreted against the state that existed when it was published, whatever has been ingested since. Only first revisions of complete, original posts change the context. References resolve to exactly one open idea; none or several is an ambiguity. A second open on the same side of an asset already open is flagged.

**Linked actions.** Actions from the same sentence on the same asset form one atomic group (ADD + MODIFY, CLOSE + OPEN reversal), so the coordinator applies all or none.

**Replay runner.** `replay_posts(posts, coordinator, interpreter, quotes_for, lag_seconds=)` sorts by publication time, interprets, authorizes and fills through `PaperEventReplay.process`, then advances the context. `now` is the real receipt time for live posts and `published_at + lag_seconds` otherwise, because archive receipt times are not latencies.

**AI backend.** `TklInterpreter(backend=..., external_processing_permitted=False)` accepts any `StructuredBackend` (for example `post_interpreters.OpenAIPostInterpreter`). Paid provenance (`LIVE_*`, `ARCHIVE_*`) raises `PermissionError` unless the caller passes `external_processing_permitted=True`, which the product owner may do only after the publisher's usage permission (spec D-07) is recorded. Synthetic and manual posts may use the backend. The deterministic path is the default and the only one exercised by the scenarios.

## 3. Scenarios (`tests/test_tkl_scenarios.py`, all synthetic)

- **TLT lifecycle:** open with levels on separate lines → add plus stop raise in one post (atomic) → half off → close the rest → past-tense confirmation (RECONCILE, no order) → position summary (no order) → replay of the close is a duplicate. Same results when the posts are fed out of order.
- **S&P reversal:** at index level every leg holds (`ORDERABLE_INSTRUMENT_MISSING`) while still being classified CLOSE + OPEN; with `$SPY` both legs fill atomically, and a reversal whose open leg is invalid changes nothing.
- **Staged partial exits:** quarter, then half of the remainder, then a third that is not an integer number of units (held), then the rest; the retrospective summary reconciles.
- **Natural-gas conditional exit:** position statement reconciles, the condition holds, the cancellation withdraws it, a present-tense exit on a non-orderable series holds, the past-tense exit reconciles. No order anywhere.
- **Provenance:** truncated, reposted and unverified posts fail closed; a truncated post completed later executes exactly once; archive posts replay at publication time plus the measured lag.

## 4. Unresolved risks

1. **Publisher exits are usually announced in the past tense.** Under the tense rule they reconcile rather than execute, so a follower would never close on "we have closed". A product decision is needed: treat same-session past-tense exit confirmations as current closes, or operate exits from reconciliation alerts.
2. **Most instructions are quoted at index or spot level.** They hold by design until the product owner approves a source-to-contract mapping policy.
3. **The grammar is synthetic-validated only.** Accuracy on the real archive needs the Sprint 1.2 holdout labelled independently; a phrasing outside the lexicon degrades to COMMENTARY (fail closed), never to a wrong order, but recall is unknown.
4. **Quoted posts, threads and charts** are context the interpreter does not see; the Source Agent must resolve quoted text or flag it, and chart-only instructions remain invisible.
5. **The context is the interpreter's belief, not the broker's book.** It resolves references for the publisher's positions even when the coordinator held the open; those follow-ups then hold at the gateway (`REFERENCED_ACTIVE_IDEA_NOT_FOUND`), which is correct but means the two views can drift and need reconciliation reporting.
