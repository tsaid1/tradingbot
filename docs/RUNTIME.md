# Runtime service (mock and shadow modes)

`src/tradingbot/runtime/` runs the pipeline as one restart-safe service:

```
source -> TklInterpreter -> mapping policy -> BrokerEventReplay.process -> SimBroker -> reconcile
          (+ private      (HOLD anything      (recover, gate_source,                    heartbeat,
           lexicon)        not DIRECT)          gate_action, coordinator)                 dashboard
```

Two modes are accepted, and in both every order, fill, position and the account are simulated by
the strict `SimBroker`: `mode = "mock"` (a static invented market) and `mode = "shadow"` (real
read-only IBKR paper quotes, see "Shadow mode" below). There is no route to an order-capable broker.

## Run it

```
PYTHONPATH=src python scripts/run_service.py --config ~/.config/tradingbot/runtime.toml
PYTHONPATH=src python scripts/run_service.py --config runtime.toml --once
PYTHONPATH=src python scripts/run_service.py --config runtime.toml --source replay:posts.jsonl
PYTHONPATH=src python scripts/run_mock_service_demo.py      # invented posts, kill + restart
PYTHONPATH=src streamlit run dashboard/app.py                # page "Execution"
```

Exit codes: 0 clean stop, 2 config refused, 1 unhandled fault. The dashboard reads the config named
by `TRADINGBOT_RUNTIME_CONFIG` (default `~/.config/tradingbot/runtime.toml`).

## Config

One TOML file; `config/runtime.example.toml` documents every key. Unknown keys, a missing required
table (`paths`, `source`, `service`, `policy`, `execution`) or any mode other than `mock` or
`shadow` refuse to start. **The config fails closed (review F4):** every size, cap, collar, freshness and timing value
the service uses must be written out, or the service refuses to start and names each missing key
(`config.REQUIRED_KEYS`). Nothing that sizes or bounds an order runs on a default. Relative paths
resolve against the config file.

* `[paths]`: `state_dir` (runtime, execution, replay and SimBroker SQLite files, `heartbeat.json`,
  `notifications.log`), `halt_file`, `mapping_file`.
* `[source]`: `kind = "replay"` (`file`, optional `compression`, `lag_seconds`,
  `expected_author_id` for G0 archive records) or `kind = "x_timeline"` (`author_id`, `token_file`,
  `client_file`; both private files, mode 0600 in a 0700 directory outside cloud storage).
* `[service]` (all required): `cadence_seconds`, `max_backoff_seconds`, `failure_alert_after`,
  `stuck_after_seconds`.
* `[policy]` (`MockPolicy`; required): `quantity`, `quantity_per_add`, `max_price_deviation_pct`,
  `max_spread_pct`, `max_notional_usd`, `max_signal_age_seconds`, `max_quote_age_seconds`,
  `max_instrument_exposure_usd`, `max_aggregate_exposure_usd`.
* `[execution]` (required): `expected_account` (the coordinator refuses any other account),
  `max_submit_attempts`, `settle_seconds`, `exit_no_trace_lookups` (at least 3),
  `max_intent_age_seconds`, `max_exit_intent_age_seconds`, `lookup_horizon_seconds`,
  `confirm_attempts`, `confirm_wait_seconds`.
* Optional: `[interpreter] lexicon_path, review_log_path` (see "Vocabulary review"), `[notify] channels, macos, realert_seconds,
  alerts_file`, `[service] source_stall_after_seconds, activity_window_utc,
  heartbeat_stale_after_seconds` (alerting only), `[service]
  lockout_clear_requires_full_reconnect` (default `true`, see `docs/OPERATIONS.md`), `[mock]
  account, quotes` (a static simulated market).

## Sources and the cursor

`poll()` returns posts after the committed cursor and does not move it; the service commits each
post only after it is processed and journaled. A restart re-polls exactly the uncommitted posts.
A post processed but not committed before a crash is replayed as a recorded duplicate: no second
order. `XTimelineSource` starts with a baseline (posts that existed before the first run are never
acted on; an empty first page still sets a baseline time, used as `start_time`, never id "0"),
reads every page since `since_id` (more than `max_pages` raises `SourceGap`), marks a
note-less ellipsis-terminated body incomplete, maps edits to revision N of the original post,
marks posts by any other author unverified, and backs off on 429 until the reset. The service also
records its first-start time and never acts on a live post published before it
(`POST_BEFORE_FIRST_START`: consumed and journaled by identity, not processed).

## Mapping policy

`config/instrument_mapping.toml` (version 2) maps each source asset family to a reference series,
an execution instrument and one price-basis treatment (`DIRECT`, `BASIS_SHIFT`,
`REFERENCE_TRIGGER`, `UNSUPPORTED`), with an owner approval. Only an approved entry executes. One
ships approved: TLT, quoted in its own price, with the version-1 behaviour unchanged. An unmapped
family is held with `UNSUPPORTED_INSTRUMENT_MAPPING`, an unapproved one with `MAPPING_NOT_APPROVED`,
and an unselectable contract with `CONTRACT_SELECTION_AMBIGUOUS`, `CONTRACT_MONTH_UNRECOGNISED`,
`OPTION_TERMS_INCOMPLETE` or `OPTION_STRATEGY_NOT_SINGLE_LEG`. Approval governs new exposure only:
an open idea whose family loses approval can still be reduced, closed and protected on the terms
recorded when it was opened (`OPEN_UNDER_REVOKED_MAPPING` on the Execution page). An index or spot
level is never sent as the price of another instrument. The Execution page lists each family's
coverage. See `docs/MULTI_ASSET_MAPPING.md`.

## What is held, and why

Every action whose outcome is `HOLD`, `BLOCK`, `REFUSED`, `HALTED`, `UNRESOLVED`, `EXCEPTION` or
`REJECTED` becomes a `SIGNAL_<status>` exception with the gateway's or the coordinator's reason
codes: unsupported mapping, edits (`SOURCE_EDIT_REQUIRES_REVIEW`), unverified or truncated
posts, stale sources or quotes, collar or exposure breaches, the halt, unknown broker outcomes.
Commentary (`NO_TRADE`) and recaps (`RECONCILE`) are journaled, not held. Nothing held is traded.

## Failures, halt, notifications

A broker that cannot be read stops the cycle before the source is touched, so no signal is consumed
during an outage. The probe reads the account, open orders, executions, completed orders and
positions: every read the coordinator depends on. A failed sync inside recovery fails the cycle
too. Source, broker and processing failures back off exponentially, bounded by
`max_backoff_seconds` (the exponent is capped, so a long outage cannot overflow). They never move
the cursor. Unknown order outcomes are looked up again every cycle; an entry or add of unknown
outcome is never resent (see `docs/EXECUTION_CORE.md`). While the halt file exists, nothing that could add exposure is
submitted or amended, and posts are still journaled (`HALTED`). Working entry and add orders are
cancelled, and protective orders are still resized to the position. A second service on the same
state directory refuses to start: the execution store is locked.

**Alerts** (`runtime/alerts.py`): at the end of every cycle, also when the broker is down, the
service derives every state that needs a human and alerts in that cycle: an entry of unknown
outcome, an unprotected or unverified position, the broker unreadable, a reconciliation
discrepancy, a reversal, a refused exit, every other open execution exception, the halt engaged or
cleared, a revoked mapping with an open position, a roll due, repeated source or pipeline failure,
a stalled cursor, a broker identity lockout. One alert per key; notified when it opens, again every
`realert_seconds` while open, resolved when its cause is gone. Channels are registered by name
(`log`, `alerts_file`, `macos`); a new one is a module in `tradingbot/runtime/channels/` and a
config entry. `scripts/tb_watchdog.py` alerts on a stale heartbeat.

**Broker session:** a failed probe marks the session lost (`BROKER_RECONNECT_PENDING` refuses
every OPEN and ADD, protection becomes UNKNOWN). Reconnection is retried on the broker backoff and
re-runs the adapter's identity checks; a session under another account locks the service out of
the broker and engages the halt. The block lifts only after a full sync and a reconciliation with
nothing unexplained; what changed meanwhile is adopted from broker evidence and reported
(`BROKER_RECONNECTED`).

**Operator actions** (`scripts/tb_operator.py`, dashboard page *Exceptions*) are queued and applied
at the start of the next cycle; see `docs/OPERATIONS.md`. Every publisher-driven REDUCE, CLOSE or MODIFY refused because the halt is engaged sends
`HALTED_ACTION_REFUSED` naming the idea, action and order ref (identifiers and reason codes only). Post text is never stored; the journal keeps hashes, identities, reason codes and
timestamps for every stage (published, received, interpreted, gate decided, submitted,
acknowledged, filled).

## Shadow mode

`mode = "shadow"` runs the whole product (source, interpreter, mapping policy, gateway, execution
core, reconciliation, alerts, dashboard) on **real read-only quotes** with **simulated fills**.

**What it is.** The broker is `tradingbot.execution.shadow_broker.ShadowBroker`. Orders, fills,
positions, open orders, executions and the account are the strict `SimBroker`, unchanged, on a
simulated account (`SIM-SHADOW-0001`; it declares `BrokerEnvironment.SIM`). Contract resolution
(stocks and ETFs) and quotes come from ONE read-only `IbkrSession`
(`docs/IBKR_ADAPTER.md`): preflight, identity gate, Read-Only verified from TWS's own settings,
the deny-by-default outgoing allowlist. Quotes are held streams, market-data type 3 (live when
entitled, honestly labelled delayed otherwise). The simulator fills working orders against each
real quote with its existing rules.

**What it is not.** It sends no order, amendment or cancel to any broker: the only messages a
shadow run can send are the read-only adapter's (market data, contract details, account and
position reads, the config read; `tests/test_shadow_mode_sdk.py` asserts this on the wire for full
lifecycles). It does not use `tradingbot.ibkr_orders` and is not a second market-data source. It
proves nothing about broker-side order handling (acks, rejections, real fills, queue position).

**The quote mapping** (`execution/shadow_quotes.py`, pure). Quality maps one to one (anything
unknown is UNAVAILABLE). Each side's age is measured only on the adapter's monotonic clock (its
image or latest value change, never a repeat) and subtracted from a service-clock anchor, rounded
up to the microsecond; the adapter's wall stamps are never read. The anchor is never later than
any `now` a decision uses (the service sets it at each cycle start and to each post's decision
time), so the gateway measures at least the adapter's age. A side that is missing, a placeholder,
not finite or flagged off-tick, and both sides of a halted series, arrive absent; a side with
non-live ticks or an observation-time problem arrives without a time; an unreported-type LIVE
quote whose evidence has expired at the read arrives FROZEN. A property test checks that the
gateway's anchored rule refuses every quote `entry_quote_check` refuses. The quote's own
`received_at` is its OLDER side's time (the read time minus the larger side age), so the policy's
`max_quote_age_seconds` measures what the adapter measured: a hung feed that still reports LIVE
is stale (review B2). When either side's time is unknown (delayed ticks, a missing side, a halted
series, an unknown observation time), the quote the gateway and the execution core see is
UNAVAILABLE with no sides (code `SIDE_TIME_UNKNOWN`): no entry, add or exit is priced on it; the
journal keeps the adapter's own label (for example DELAYED) and reason.

**Simulated fills (review B1).** A real quote is in the simulator's store only for the instant of
one read: it is published, the simulator matches, the read's own action (a submit, amendment or
cancel) runs on it, and the store is set back to UNAVAILABLE; nothing can later match on an older
quote. An OPENING order (not reduce-only: an entry or an add) is matched only on a LIVE quote with
both sides present, bid below ask, and each side no older than the limit it was approved under
(the anchored rule's `max_quote_age_seconds` for an anchored entry, the policy's
`max_quote_age_seconds` for an explicit one). The shadow submit checks its own read first; if it
fails, the simulated broker rejects the whole bracket with a public code
(`SHADOW_OPENING_QUOTE_NOT_LIVE`, `..._SIDE_STALE`, `..._LOCKED_OR_CROSSED`, `..._LIMIT_UNKNOWN`;
journaled `SHADOW_OPENING_ORDER_REJECTED`): nothing rests. A resting opening order is cancelled
(`SHADOW_OPENING_ORDER_CANCELLED`, with the code and the quote) as soon as a read of its contract
fails that test: the simulation cannot know what a real resting order would have done on data it
does not have. This sits beside the execution core's own rules for a resting entry (it expires
once its post is older than its validity, and a publisher CLOSE or REDUCE cancels it,
docs/EXECUTION_CORE.md): whichever acts first cancels the order, the other finds nothing working,
and the journal shows one reason per order; the stop child is cancelled with its parent. The
invariant is enforced where a read is published: before a read that fails the opening test is
published, every resting opening order of the contract is cancelled in the simulator, the one
the core is cancelling in that same call included (its journal row is left to the core, and the
core's cancel then finds it cancelled, a confirmed cancel). If a cancel is refused, the read is not
published at all, the refusal is journaled (`SHADOW_OPENING_CANCEL_REFUSED`, with the code) and the
alert of the same name stays open until a later read qualifies the order, a later read cancels it,
or the order has ended. The alert is kept in memory only: after a restart it is gone, and it comes
back only if the next read that does not qualify the order is refused again. The journal row
survives the restart. Reduce-only orders (stops, closes, take-profits) keep the simulator's rules on any
real quote, and every simulated fill's label says what it was computed from:
`SIMULATED_AT_LIVE_QUOTE`, `SIMULATED_AT_DELAYED_QUOTE` or `SIMULATED_AT_FROZEN_QUOTE`.

**Streams.** One per contract, opened on demand and reused, at most the session's
`max_quote_streams` (4); at the cap the least recently read stream is closed first, but never
the stream of a contract with a working simulated order, a price condition or a position (review
S2). When every stream is held by such a contract, the new contract gets no stream: its quote is
UNAVAILABLE (`STREAM_CAP_REACHED`), its entries hold, and the alert `SHADOW_STREAM_CAP_REACHED`
stays open until a stream is free. A contract
with no stream yet does not block the loop: its stream is opened and that read returns what has
arrived (normally nothing: UNAVAILABLE, journaled `NO_STREAM_YET`), so a decision on it holds.
The approved stock and ETF families of the mapping (TLT) are opened at start and after every
reconnect, so a post normally meets a warm stream. A failed stream (request error, 1100
connectivity loss) is cancelled and reopened at once; the new subscription must earn LIVE from
its own evidence. Every unusable read is published to the simulator as UNAVAILABLE with no sides:
nothing is filled on an older quote.

**Configure and start.**

```toml
mode = "shadow"
[shadow]
paper_config = "~/.config/tradingbot/ibkr/paper.toml"   # loaded exactly as the paper check does
account = "SIM-SHADOW-0001"                             # optional; the simulated account
[execution]
expected_account = "SIM-SHADOW-0001"                    # must equal it
```

`config/runtime.shadow.example.toml` is complete (invented posts). Shadow mode refuses to start
without `[shadow] paper_config`, with a `[mock]` table (no invented market), with a historical
replay (a replay needs `compression`: decisions are taken on today's market), with any other
`expected_account`, on a state directory that mock mode used (and mock mode refuses a shadow one),
and whenever the session cannot be positively identified as the approved paper environment:
config file unsafe, profile not paper, listener absent or owned by another user, server too old,
identity mismatch, Read-Only not verified, the SDK missing, or the watched families not
streamable. Exit code 2, nothing left connected (the simulator is opened before the session, and the session is closed on any later start-up failure; review S4).

```
PYTHONPATH=src python scripts/run_service.py --config <private>/runtime.shadow.toml
```

**Start before the first post.** A post that arrives before the watched family's quote stream has
delivered its first price is blocked with `QUOTE_UNAVAILABLE` (for a mapped family, an earlier gate
such as its session window or roll window can name a different code first). The stream is opened at start and
needs a moment to deliver, so start the service before the session's first expected post, and
check the heartbeat's `shadow` block (each open stream with its label and reason) shows a usable
quote before relying on it.

**What the owner sees.** The heartbeat carries `mode = "shadow"` and a `shadow` block (quote
session state, masked paper account, requested market-data type, open streams with their label and
reason, reconnects, `fills = SIMULATED_AT_REAL_QUOTE`). The journal's `SERVICE_START` carries the
mode; every post records `SHADOW_DECISION_QUOTES` (for each quote read during its decision: label,
adapter reason, market-data type, per-side ages, problem codes); every simulated fill records
`SHADOW_SIMULATED_FILL` with its label and the quote it was computed from, journaled the moment the
fill is noted. The quote's evidence is written to the simulator's own file just before the quote
is published, so a fill that a crash left unjournaled is rebuilt at the next start from the
simulator's executions and that evidence (marked `recovered`; review S3). The Execution page shows a shadow
banner, labels positions and orders `SIMULATED at real quotes`, shows each fill's label
(`SIMULATED_AT_LIVE_QUOTE`, `..._DELAYED_...`, `..._FROZEN_...`), and lists the quotes each decision
was made on. The paper account appears only masked.

**Delayed or quiet quotes.** Anchored entries need a LIVE quote with each side changed within
`max_quote_age_seconds` (3 s for TLT). A delayed (this account's US stocks answer 2186 with delayed
ids), frozen, unreported-and-unchanging, one-sided-stale, failed or unavailable quote HOLDs or
BLOCKs the entry (`QUOTE_QUALITY_DELAYED`, `ENTRY_ANCHOR_QUOTE_NOT_LIVE`,
`ENTRY_ANCHOR_QUOTE_SIDE_STALE`, `QUOTE_UNAVAILABLE`, ...). Explicit-price entries follow the
gateway's quality rules as in mock mode. A simulated stop is checked against every real quote read,
including a delayed one, exactly as the simulator does today, and its fill is labelled
`SIMULATED_AT_DELAYED_QUOTE`; a simulated entry never fills on one.

**Session loss.** A connectivity interruption (1100) fails the streams: quotes UNAVAILABLE, entries
held, streams reopened once TWS answers again. A dropped socket, poison or disconnect marks the
broker disconnected: the existing policy applies (`BROKER_RECONNECT_PENDING`, every OPEN and ADD
refused, the broker alert, no post consumed), and `reconnect()` opens a NEW session with every
check again on the broker backoff; an identity, paper-environment or read-only failure locks the
service out and engages the halt. Clean shutdown closes every stream and disconnects
(`SHADOW_SESSION_CLOSED`).

**What remains to verify against a real TWS.** Whether TLT's live quote is entitled at all on this
account (if it stays DELAYED, every anchored entry holds, by design); how often each side changes
in a quiet session against the 3 s limit; how soon a new stream earns LIVE; whether TWS sends -1/0
placeholders before the first quote (a placeholder never counts; the current value must be valid);
how 2186 / 10090 / 10167 attach to a stream; how a TWS restart surfaces (socket close vs 1100) and
how long reconnect takes; contract resolution for TLT with `exchange = "SMART"` and no primary
exchange returning exactly one contract.

## Vocabulary review (owner only)

The entry rule fails closed: an entry holds when its post holds a word the public lists do not
know (`ENTRY_ANCHOR_POST_UNRESOLVED_MARKET`, `ENTRY_ANCHOR_CONTEXT_UNCLEAR`,
`ENTRY_ANCHOR_INSTRUMENT_UNCLEAR`, or a level whose instruction or neighbour sentence is not
understood). The journal records only the reason code and the mention kinds, and only the owner may
see the publisher's words. Dashboard page *Vocabulary Review* (`10_Vocabulary_Review.py`) shows
them to the owner, locally, as one batch of proposed lexicon additions.

**Outside reviewer (optional).** A separate, sanitized page lets ONE trusted reviewer recommend
classifications (words, kinds, counts and public codes only; no sentence, meaning or id), shown
read-only under each proposal here; the owner still decides and applies every word, and each
apply records the recommendations it was shown with. See [VOCAB_REVIEWER.md](VOCAB_REVIEWER.md).
Without its recommendations log this page is unchanged.

**Config** (both optional; the review log is off by default):

```toml
[interpreter]
lexicon_path = "/Users/you/tradingbot-private/lexicon.json"     # the PRODUCTION private lexicon
review_log_path = "/Users/you/tradingbot-private/review.jsonl"  # the PRIVATE review log
```

With `review_log_path` set, every held action whose mapping decision is a vocabulary hold appends
one JSON line: time, post and action ids, family, the public reason code and detail, and privately
the words (with their kind: `UNKNOWN_WORD`, `PROPER_NOUN`, `MARKET_NOUN`, `INSTRUMENT_PHRASE`,
`TICKER_SHAPED`, `NEIGHBOUR_WORD`, `LEVEL_LINE_WORD`) and the sentences involved (role `LEVEL`,
`INSTRUCTION`, `NEIGHBOUR`, `PRECEDING`, `MENTION`, `LEVEL_LINE`). A neighbour sentence is logged
only when context words alone could make it a protective-levels sentence. One record per (post,
revision, action, reason code and detail): a replay or a restart that sees the same hold again
does not append it again. The review log is the ONE file holding the publisher's sentences;
proposals, candidates, the decisions file and backups hold words and public codes only. To bound
it, stop the service and run
`python -c "from tradingbot.runtime.vocab_review import truncate_review_log as t; t('<path>', 2000)"`
(keeps the last 2,000 lines). Unset, nothing is collected and every journal, alert and heartbeat
byte is as before.

**Proposals** (`tradingbot.vocab_proposal/1`, JSON lines, private). The page reads
`TRADINGBOT_VOCAB_PROPOSALS`, else `<review log>.proposals.jsonl` beside the log; without one it
builds a plain batch from the log by kind (proper noun: non-market word; market noun or instrument
phrase seen in posts of one family: a name of that family; unknown word of an instruction sentence:
instruction phrase; anything else: ambiguous). **Safer default:** a word that held a post through
the mention-with-context rule (`ENTRY_ANCHOR_POST_UNRESOLVED_MARKET`, `RULE:MENTION_WITH_CONTEXT`)
was seen next to a price or a trading word, so it may be another market: whatever its kind, it is
proposed as ambiguous (no list) and the page and the final table show, as plain text, "seen with a
price or a trading word; may be another market". The warning comes from the review log's
evidence, never from a proposals file: a file may suggest another group, but a warned word is
always pre-selected as ambiguous, and the final table repeats the warning beside the word when the
owner accepts it into a list. The warning belongs to the **word**, not to a (word, kind) pair: the
plain batch shows ONE proposal per word with all its kinds, so a warned word is never also offered,
unwarned, under another kind. "The same word" (`vocab_review.word_key`) is what the anchor rule
and the mention detector compare: case-insensitive, edge punctuation and a leading `#`, `$` or `@`
removed, a possessive `'s` removed. A phrase or compound one of whose parts (split on spaces, `/`,
`.`, `-`) is a warned word is warned too. A plural is a different word: the detector does not fold
it, so the plural in the lexicon does not remove the original's hold. The warning flag is part of
the batch digest. One line:

```json
{"schema": "tradingbot.vocab_proposal/1", "token": "zorbs", "group": "MARKET_NAME",
 "family": "TLT", "meaning": "invented nickname", "kind": "MARKET_NOUN",
 "evidence": {"posts": 4, "holds": 5},
 "categories": {"families": ["TLT"], "reasons": ["ENTRY_ANCHOR_POST_UNRESOLVED_MARKET"]},
 "measured": {"holds_resolved": 4, "executable_gained": 3, "false_executable_introduced": 0},
 "examples": [{"role": "MENTION", "text": "..."}, {"source_id": "7", "action_id": "a1"}]}
```

`group` is one of `MARKET_NAME` (market names and instrument aliases: `family_aliases[family]`),
`NON_MARKET` (non-market words mistaken for instruments: `non_market_words`), `INSTRUCTION_PHRASE`
(publisher-specific instruction phrases: `context_words`) or `AMBIGUOUS` (no list until the owner
picks one). `family` must be a family code. `measured` holds public counts only, from a local
evaluation, and the page labels them "claimed by the proposals file; not verified by this page";
anything else, or `null`, is shown as "not measured". An example should be a reference into the
review log (`{"source_id", "action_id"}`), resolved when the page renders; inline `text` is
displayed but never copied into another file. A plain batch built from the log emits references
only. At most five example sentences are loaded per proposal. Words already in the lexicon or kept
holding are not proposed again.

**The batch.** For each proposal the owner edits the meaning, may change the group (and family), and
chooses accept, reject (not now), keep holding (recorded, not offered again) or leaves it undecided.
The page shows a **final table** of the batch (each word, the exact list it is written to and its
family, and its effect: "removes a hold: posts containing this word may become executable" or
"keeps holding") with a **digest** of the table and of the production lexicon's bytes; the
confirmation names that digest. *Apply batch* recomputes the batch from the current files at click
time and refuses when the digest differs ("the batch changed since you reviewed it"). Widgets are
keyed by a stable id of each proposal (word, kind, group, family), never by position, and every
verdict and confirmation is cleared after any write (apply, undo, candidate). *Undo* and *Write a
candidate* need their own confirmation.

*Apply batch* is one change of the production lexicon: each accepted word is checked by the
service's own loader (`service.load_lexicons`; a refused word is listed with the loader's reason and
left out), the whole result is checked again, then written atomically (temporary file and rename),
mode 0600, with one timestamped backup beside it (`lexicon.json.bak-<UTC time>`). *Undo* restores the
latest backup and drops that batch's records; it refuses when the lexicon is not the file the tool
last wrote (its sha256 is recorded per write), so a hand edit is never silently discarded: the
message names the backup to restore by hand. Only the `entry_anchor` lists change; every other
key, including the interpreter's own sections, keeps its content and meaning, but the file is
re-serialised (JSON, two-space indent), so its formatting may change. Keep-holding words, the
applied words, the owner's meanings and the sha256 of each write are kept in
`<lexicon>.review-decisions.json` beside the lexicon; a keep-holding word that could not be
recorded is reported as failed. **A new word
takes effect only when the service (or a replay) is restarted.**

**Experiment versus production.** *Write a candidate* writes the production lexicon plus the accepted
proposals, validated by the same loader, to a separate file (`<lexicon>.candidate.json`) marked
`candidate_lexicon`; the production lexicon is only read. A candidate is only ever written to a
`*.candidate.json` path, never over a file without the marker, the production lexicon, a backup,
the decisions file or the review log. The service refuses to start with a
candidate as its `lexicon_path`; only an offline replay may load one, explicitly:
`scripts/run_service.py --config <replay.toml> --source replay:<posts.jsonl>
--allow-candidate-lexicon` (refused with any live source, and in shadow mode whatever the source). The candidate replay needs a **new
state directory** (no heartbeat, no journaled event or signal) or one an earlier candidate run
marked (`CANDIDATE_RUN`); it marks it, and a production run on a marked directory is refused, so
experimental holds and trades never enter production's journal. Production eligibility therefore
changes only through *Apply batch* (or the owner editing the file by hand).

**Privacy rules.** Every private file (review log, proposals, lexicon, backups, decisions,
candidate) lies outside **this program's repository** (the checkout it runs from, its main
checkout and every linked worktree of it, compared by their common git directory), outside any
`.git` directory and outside any bare repository; the service refuses to start otherwise, and
refuses a group- or world-readable review log. "Inside" is decided by **file identity** (device
and inode of every existing enclosing directory, compared with this program's checkout, its main
checkout, every linked worktree recorded in the common git directory and the git directory
itself), never by comparing strings, so a case or Unicode-normalisation variant of a directory
name or a symlinked parent cannot pass on a file system that folds them (macOS does); string checks
remain as an extra refusal, and any error while checking refuses. A repository nested inside
this program's main checkout is therefore refused too, also when the service runs from a linked
worktree. When this program's repository cannot be determined (an installed package, no git
checkout, a git directory that does not exist), every git work tree is refused (fail closed); so is
another work tree whose own git directory cannot be found. After start nothing is trusted either:
every private write (review log appends, lexicon, backups, decisions, candidate) opens its file
relative to a held descriptor of its checked folder, never through a link (`O_NOFOLLOW`), re-runs
the path check and requires the folder to be the same directory (device and inode) before and
after opening, and refuses a file whose link count is above 1 (a hard link elsewhere, checked with
`fstat` on the open file). A refused review-log append raises `REVIEW_LOG_WRITE_FAILED` by code
only; the service and the hold are unaffected.
A path inside **any other** git work tree is allowed (the Product Owner's decision: the owner's
workspace is itself a private repository) and warned: a banner on the page saying the file could
be committed to that repository by mistake and that the tool cannot prevent it, and, when the
review log is on, a start-up alert `PRIVATE_PATH_IN_OTHER_GIT_TREE <file> OTHER_GIT_WORK_TREE`
(no path). A work tree whose git directory lives elsewhere without a `.git` entry
(`core.worktree`) cannot be detected. A cloud-synced folder (`~/Library/CloudStorage`,
iCloud Drive, `~/Dropbox`, OneDrive, Google Drive, possibly iCloud Desktop and Documents) is
allowed by the owner's policy but warned: a banner on the page and, when the review log is on, a
start-up alert `PRIVATE_PATH_IN_SYNCED_FOLDER` naming the file kind and path class only. A
non-synced private directory is recommended for the review log, proposals, candidates and
backups; credentials never belong in a synced folder. A path can carry both warnings.

Recommended layout: one private folder, mode 0700, outside this program's repository, ideally not
synced and not in any git work tree, holding all of the owner's review files:

```
~/tradingbot-private/            (chmod 700)
  lexicon.json                   [interpreter] lexicon_path (chmod 600)
  lexicon.json.bak-<UTC time>    backups, one per applied batch
  lexicon.review-decisions.json  keep-holding words, applied words, meanings, write hashes
  lexicon.candidate.json         a candidate for an offline replay only
  review.jsonl                   [interpreter] review_log_path (the only file with sentences)
  review.proposals.jsonl         proposals (optional)
```

A lexicon the loader refuses stops the
service with a `ConfigError` naming the error type, field paths and count only, never a value. Files are created with mode 0600. The
private words ride on the mapping decision as a pydantic private attribute that no serialisation
reads, so they never reach the journal, logs, alerts, the heartbeat, the exception register or
another page. (`pickle`, `copy.deepcopy`, `model_copy` and `==` on a decision do carry or compare
the private evidence; nothing under `src/` does any of these to a decision, and a test scans for
it.) A failed write raises `REVIEW_LOG_WRITE_FAILED` by code only and never stops the service. The
page refuses to run unless Streamlit is bound to localhost with usage statistics off, renders every
private or file-supplied string as plain text (never Markdown, HTML, links or images), keeps no
sentence in its session state, caches nothing and puts nothing in the URL. Binding to localhost
does not protect against other user accounts on the same Mac:

```
PYTHONPATH=src streamlit run dashboard/app.py --server.address 127.0.0.1 --browser.gatherUsageStats false
```

**The honest limit.** The owner's judgement is the safeguard. A word wrongly marked "not a market"
or added as a family name removes a hold that protected against a second market; a word wrongly
added as instruction wording widens what an anchor sentence may say. Nothing is accepted
automatically, and no validator is relaxed: the loader still refuses a disqualifying word, a market
noun as a non-market word and a marker word in an alias.
