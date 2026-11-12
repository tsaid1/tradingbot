# Runtime service (mock mode)

`src/tradingbot/runtime/` runs the pipeline as one restart-safe service:

```
source -> TklInterpreter -> mapping policy -> BrokerEventReplay.process -> SimBroker -> reconcile
          (+ private      (HOLD anything      (recover, gate_source,                    heartbeat,
           lexicon)        not DIRECT)          gate_action, coordinator)                 dashboard
```

Only `mode = "mock"` (the simulated broker) is accepted. There is no route to a real broker.

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
table (`paths`, `source`, `service`, `policy`, `execution`) or any mode other than `mock` refuse to
start. **The config fails closed (review F4):** every size, cap, collar, freshness and timing value
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

## Vocabulary review (owner only)

The entry rule fails closed: an entry holds when its post holds a word the public lists do not
know (`ENTRY_ANCHOR_POST_UNRESOLVED_MARKET`, `ENTRY_ANCHOR_CONTEXT_UNCLEAR`,
`ENTRY_ANCHOR_INSTRUMENT_UNCLEAR`, or a level whose instruction or neighbour sentence is not
understood). The journal records only the reason code and the mention kinds, and only the owner may
see the publisher's words. Dashboard page *Vocabulary Review* (`10_Vocabulary_Review.py`) shows
them to the owner, locally, as one batch of proposed lexicon additions.

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
instruction phrase; anything else: ambiguous). One line:

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
--allow-candidate-lexicon` (refused with any live source). The candidate replay needs a **new
state directory** (no heartbeat, no journaled event or signal) or one an earlier candidate run
marked (`CANDIDATE_RUN`); it marks it, and a production run on a marked directory is refused, so
experimental holds and trades never enter production's journal. Production eligibility therefore
changes only through *Apply batch* (or the owner editing the file by hand).

**Privacy rules.** Every private file (review log, proposals, lexicon, backups, decisions,
candidate) lies outside the repository, every git work tree it can detect (a `.git` entry in an
ancestor) and every bare repository; the service refuses to start otherwise, and refuses a group-
or world-readable review log. A work tree whose git directory lives elsewhere without a `.git`
entry (`core.worktree`) cannot be detected. A cloud-synced folder (`~/Library/CloudStorage`,
iCloud Drive, `~/Dropbox`, OneDrive, Google Drive, possibly iCloud Desktop and Documents) is
allowed by the owner's policy but warned: a banner on the page and, when the review log is on, a
start-up alert `PRIVATE_PATH_IN_SYNCED_FOLDER` naming the file kind and path class only. A
non-synced private directory is recommended for the review log, proposals, candidates and
backups; credentials never belong in a synced folder. A lexicon the loader refuses stops the
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
