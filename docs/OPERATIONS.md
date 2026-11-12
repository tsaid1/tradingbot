# Operations runbook (mock mode)

Three processes: the **service** (`scripts/run_service.py`), the **watchdog**
(`scripts/tb_watchdog.py`, every 60 s) and the **dashboard** (`streamlit run dashboard/app.py`, pages
*Execution* and *Exceptions*). All read one runtime TOML (`TRADINGBOT_RUNTIME_CONFIG`, default
`~/.config/tradingbot/runtime.toml`). Only the simulated broker exists; no operator action below
sends an order.

## Start and stop

* Once, before the first start: `PYTHONPATH=src python scripts/tb_operator.py --config <toml> init`
  creates the operator queue (`<state_dir>/operator.sqlite3`). Nothing else creates it: the
  service, the CLI and the dashboard only open an existing queue. Without it the service runs, but
  operator actions are disabled and `OPERATOR_QUEUE_UNAVAILABLE` stays open.
* By hand: `PYTHONPATH=src python scripts/run_service.py --config <toml>`; Ctrl-C stops it cleanly.
* Under launchd: copy `ops/launchd/com.example.tradingbot.{service,watchdog}.plist.template` to
  `~/Library/LaunchAgents/` without `.template`, replace every `__PLACEHOLDER__` with an absolute
  path (keep the state and log directories outside cloud storage), then
  `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.example.tradingbot.service.plist`
  (same for the watchdog). Stop: `launchctl bootout gui/$(id -u)/com.example.tradingbot.service`.
  The service restarts only after a non-zero exit. Logs: `~/Library/Logs/tradingbot/*.log`,
  rotated at 5 MB, 5 kept.
* A second service on the same state directory refuses to start (exit 2): the store is locked.

## Halt and clear

* Engage: the dashboard button, or `touch <paths.halt_file>`. While it exists, nothing that adds
  exposure is sent or amended, working entries are cancelled, stops still follow the position, and
  publisher exits are refused (`HALTED_ACTION_REFUSED`; the broker stop stays the protection).
* Clear: type `RESUME` on either page, or `rm <paths.halt_file>`. The service resumes next cycle.
* The service engages the halt itself on an identity lockout. Clear the lockout first, then the halt.

## Alerts and what to do

Each alert notifies once when it opens, again every `[notify] realert_seconds` (default 1 h) while
it stays open, and closes when its cause is gone. Channels: the notifications log, the alerts file
(`<state_dir>/alerts.jsonl`, shown on *Exceptions*) and, optionally, macOS. An alert counts as
notified only once a durable channel (the log or the alerts file) has accepted it; if none did, it
stays open and un-notified and is sent again on the next cycle, so a failed channel never loses
an alert.

| Alert | Meaning | Action |
|---|---|---|
| `ENTRY_UNKNOWN_OUTCOME` | An entry or add was sent and its outcome is unknown. It is never resent, and its contract is blocked. | Look the order up at the broker. If it does not appear on its own, settle it (below). |
| `UNPROTECTED_POSITION` | No working stop is confirmed for the position, or the check failed. With `BROKER_POSITION_DIFFERS` in the detail, the broker holds a different quantity than the ledger: the stop and target are never sized above what the broker holds, and are reduced below the ledger only on corroborated evidence (an exit fill visible in executions made after the idea's last fill or settlement and not used before, or the lower position read consistently over the settle period), never on one read; the contract is blocked. | Check the stop at the broker. If the broker holds less than a settlement declared, correct it with `settle <ref> FILLED` naming the true total; if it holds more, deal with the excess by hand. The alert clears only on evidence: a confirmed stop or a flat position. |
| `BROKER_FAILURE` | The broker is unreadable, or its state is not yet verified. New exposure is blocked. | Check TWS or the gateway. The service retries on its own. |
| `BROKER_RECONNECTED` | The session is back. The alert lists the fills and order ends adopted while it was down. | Read the list. If the resync waits on a discrepancy, resolve that first. |
| `BROKER_IDENTITY_MISMATCH` | The session came back under another account, or its identity check failed. The service stops touching the broker and engages the halt. | Connect the expected account, clear the lockout, then clear the halt. |
| `RECONCILIATION_DISCREPANCY`, `STUCK_INTENT` | The ledger and the broker disagree, or an intent has stayed unresolved. | Compare the two pages with the broker. |
| `LEDGER_REVERSAL` | An exit filled more than the position: the account went through flat. | Flatten the excess at the broker by hand, then CLEAR it. |
| `EXIT_REFUSED`, `HALTED_ACTION_REFUSED` | A publisher exit or amendment was not executed. | Check the position and its stop. Exit by hand if needed, then acknowledge. |
| `SETTLEMENT_UNVERIFIED` | A settlement was applied and its contract stays blocked until the evidence below has followed it. | None. It clears on its own once the evidence is in. |
| `SETTLEMENT_CONTRADICTED` | The broker shows a fill, a working order or a completed order for an intent you settled as `NOT_AT_BROKER` or `CANCELLED`. The fill is in the ledger as it happened, the contract is blocked and reconciliation is not clean. | Check the account at the broker: it may hold more than intended. Decide what to do with the extra position and make sure the order has ended at the broker, then CLEAR it once reconciliation is otherwise clean. |
| `MAPPING_REVOKED_OPEN_POSITION` | A position is open in a family that has lost approval. It can be exited, never added to. | Decide whether to keep or exit the position. |
| `ROLL_DUE_FLATTEN_REQUIRED` | A futures position is inside its roll window. | Flatten or roll it by a new instruction. |
| `SOURCE_FAILURE`, `PIPELINE_FAILURE` | The source or the processing of a post has failed repeatedly. The cursor does not move. | See Source outage below. |
| `SOURCE_STALLED` | The cursor has not moved during the activity window. | Check the source account and token. |
| `OPERATOR_QUEUE_UNAVAILABLE` | The operator queue is not initialised (`QUEUE_NOT_INITIALISED`), missing although the execution journal shows it was used, or present but lacking a request the execution journal records a result for, for example swapped for an empty valid file (`QUEUE_LOST`, checked at start-up and every cycle), or unreadable or corrupt (`QUEUE_UNREADABLE`). No operator request is applied; trading, exits, protection upkeep and the heartbeat carry on. | Not initialised: run `tb_operator.py init`. Lost, swapped or corrupt: restore the file from where it went; if that is impossible, move the file present aside and run `tb_operator.py init --replace-lost` (the new queue records the lost request ids it replaces, so it is accepted). Request ids are globally unique, so a new queue's requests can never be matched to old results. |
| `REVIEW_LOG_WRITE_FAILED` | The owner's private vocabulary review log (`[interpreter] review_log_path`) could not be appended: it became group- or world-readable, was replaced by something other than a regular file, or the disk refused the write. The alert carries the code and the exception type only. Trading, the journal and the heartbeat carry on; only review lines are lost. | Fix the file (`chmod 600`, restore a regular file, free space); the alert resolves on the next successful write. See `docs/RUNTIME.md`, "Vocabulary review". |
| `PRIVATE_PATH_IN_SYNCED_FOLDER` | At start, with the review log on: the review log or the private lexicon lies in a cloud-synced folder (the message names the file kind and the path class, such as `CLOUD_STORAGE`, never the path or any content). Allowed by the owner's policy; the file is copied to that provider. | Nothing required. To avoid the copy, move the review log, proposals, candidates and backups to a non-synced private directory (mode 0700) and update the config. |
| `PRIVATE_PATH_IN_OTHER_GIT_TREE` | At start, with the review log on: the review log or the private lexicon lies inside a git work tree that is not this program's repository (the message names the file kind and the class `OTHER_GIT_WORK_TREE`, never the path). Allowed by the Product Owner's decision; the file could be committed to that repository by mistake and the tool cannot prevent it. | Make sure that repository never commits the file (an ignore rule, or keep it outside the tracked folders), or move the private files to a private folder outside any git work tree (docs/RUNTIME.md, recommended layout). |
| `HEARTBEAT_STALE` / `_RESTORED` | From the watchdog: the service has stopped, or is hung. | Check `launchctl list`, the service log and the lock holder. |
| `HALT_ENGAGED` / `HALT_CLEARED` | The halt file appeared or went away. | Confirm that you meant it. |

## Operator actions (CLI `scripts/tb_operator.py`, or the *Exceptions* page)

Every action needs a note and the exact confirmation words. It is queued append-only in
`<state_dir>/operator.sqlite3` and applied by the process that holds the execution store: the
running service on its next cycle, or `tb_operator.py apply` while the service is stopped. Results
show under `tb_operator.py history` and on the page. Each request has a globally unique id (`uid`);
the execution journal records the result under that id with its target and outcome, so a request
is applied exactly once and never answered with another request's result. Your note is kept in
the queue and, with its hash, in the local execution journal, so your reasoning survives the loss
of the queue; write what you checked, never a credential or a full account number.

* `settle <ref> NOT_AT_BROKER | CANCELLED | FILLED --quantity N --price P`: declares the outcome
  of an intent whose outcome is unknown, once you have checked the broker. Applying it reads the
  broker and records the declaration; it sends, amends and cancels nothing. Protective upkeep
  (sizing or cancelling the stop to the declared position, cancels under the halt) happens in the
  service's next normal cycle, as after any broker fill. The confirmation is
  `SETTLE <ref> <outcome>`; for `FILLED` it also names what you declare,
  `SETTLE <ref> FILLED <N> @ <P>` (the price exactly as you typed it), so a mistyped size or price
  is not confirmed by habit. The ledger moves exactly
  as a broker fill would move it. A settlement is refused if the broker contradicts it: the order or
  its fills are visible, the price breaks the limit, the quantity is off, or the resulting position
  cannot match the broker's. A declared fill with no confirmed stop opens `UNPROTECTED_POSITION`.
  The contract stays blocked (`OPERATOR_SETTLEMENT_UNVERIFIED`) afterwards, never lifted in the
  call that records the settlement. After `FILLED` it lifts on the next cycle whose sync and
  reconciliation show the contract clean. After `NOT_AT_BROKER` or `CANCELLED` it lifts only when
  the rule the core uses before resending an exit has been met *after* the settlement: the settle
  period (`[execution] settle_seconds`) has passed, at least `exit_no_trace_lookups` consecutive
  lookups spread over that period found no trace of the order in open orders, executions or
  completed orders (a cancelled, unfilled row confirms a `CANCELLED`), and the contract then
  reconciles clean. With the defaults that is about one settle period and three cycles. The
  reason: the broker's open-orders view can lag, so "I don't see it" on one read is not proof.
  If the order shows up later anyway, that is `SETTLED_INTENT_CONTRADICTED` (above), never a
  normal adoption; the intent's journal shows your declaration and the evidence that
  contradicted it.
  A `FILLED` declaration is recorded as declared, not as broker evidence: when the broker's own
  executions for that order arrive they replace it, so the ledger never holds both. While they
  cover only part of what you declared, the declared rest still counts and the contract stays
  blocked; correct it with a new `settle <ref> FILLED` naming the true total (never below what the
  broker shows). If the broker's quantity or average price differs from your declaration,
  `SETTLEMENT_DIFFERS_FROM_BROKER` opens and alerts (blocking only for a quantity); CLEAR it once
  reconciliation is clean.
* `clear <id> LEDGER_REVERSAL | SETTLED_INTENT_CONTRADICTED | PARTIAL_GROUP`: applied only if a
  sync and reconciliation made at that moment show the contract clean (the record's own line
  excepted). A `SETTLED_INTENT_CONTRADICTED` also needs the contradicting order ended at the
  broker: while it is working, or no terminal completed-orders row shows it, the clear is refused
  with `CONTRADICTING_ORDER_NOT_TERMINAL:<ref>:<state>`; cancel it at the broker first. `PARTIAL_GROUP` is here, not under `ack`: while it is open reconciliation is not
  clean, so closing it can lift other blocks and needs the same evidence.
* `ack <id> <kind>`: closes a record that blocks nothing (`EXIT_NOT_NETTED`, `EXIT_WITHOUT_QUOTE`,
  `STALE_AFTER_UNKNOWN_OUTCOME`, `MODIFY_NOT_CONFIRMED`, `PROTECTIVE_LEVEL_NOT_LOOSENED`).
  `ack-signal <id> <kind>` closes a held signal.
* `clear-lockout <expected account>`: forces a full reconnect through the adapter, so every
  identity check it has runs again, even if the session looks connected. It is applied only if
  that reconnect succeeds with the expected account and the same session identity the service
  last accepted (account and paper or live; a session that changed kind is never accepted by a
  typed word). Otherwise it is refused with the reason (`IDENTITY_CHECK_FAILED`,
  `BROKER_STILL_SHOWS_ANOTHER_ACCOUNT`, `SESSION_IDENTITY_NOT_ACCEPTED`, `BROKER_UNREADABLE`,
  `FULL_RECONNECT_UNAVAILABLE`) and the lockout stays. Once applied, the next cycle runs the full
  resync. The halt stays engaged. The journal (`IDENTITY_LOCKOUT_CLEARED`) states what was done:
  `mode` `FULL_RECONNECT` (the adapter reconnected, or this process opened its session after the
  lockout began) or `IDENTITY_REREAD_ONLY` (an adapter that cannot reconnect, cleared on an
  identity read alone). With `[service] lockout_clear_requires_full_reconnect = true` (the
  default) the latter is refused with `FULL_RECONNECT_UNAVAILABLE`: such an adapter stays locked
  out until the service is restarted, after which the clear is accepted on the new session.
* Never operator-clearable, because only evidence resolves them: `UNPROTECTED_POSITION`,
  `PROTECTION_UNVERIFIED`, `ENTRY_REMAINDER_NOT_CANCELLED`, `BROKER_STATE_UNKNOWN`,
  `BROKER_RECONNECT_PENDING`, `OPERATOR_SETTLEMENT_UNVERIFIED`.

## Recovery

* **Crash:** launchd restarts the service. On start it looks up every intent of unknown outcome. An
  intent that was recorded but never sent is sent only if it is still fresh, otherwise it is
  REFUSED. A post processed but not committed replays as a duplicate, so no second order is placed.
  Check the *Exceptions* page.
* **Reboot:** the same. Then confirm a fresh heartbeat, the broker session `CONNECTED` and a clean
  reconciliation.
* **Broker restart** (TWS restarts daily): the probe fails, `BROKER_FAILURE` fires and every OPEN
  and ADD is refused. The service reconnects on a bounded backoff (cadence × 2ⁿ, capped at
  `max_backoff_seconds`) and re-runs the adapter's identity checks. A full sync and a reconciliation
  with nothing unexplained must succeed before new exposure is allowed. Fills, cancels and expired
  DAY orders from the gap are adopted from broker evidence and reported in `BROKER_RECONNECTED`.
  If the session drops again while the resync is still waiting, that is a new outage: it is
  reported (`BROKER_FAILURE`, `BROKER_SESSION_LOST`), the session goes back to `DISCONNECTED`,
  exposure stays blocked, and the next reconnect reports everything adopted since the last report.
* **Source outage:** nothing is consumed and the cursor stays put. Once the source is back, posts
  run in order, and stale ones are held by the freshness gate.
* **A post that always fails:** `PIPELINE_FAILURE` repeats and every later post waits behind it,
  exits included. Engage the halt, read `POST_PROCESSING_FAILED` in the service events (identifiers
  only), fix the cause, restart. Never skip the post by editing the cursor.

## Daily checklist

1. The heartbeat is fresh and the session is `CONNECTED` (*Exceptions*, Status).
2. Reconciliation is clean and every position shows `CONFIRMED` (*Execution*).
3. There is no open alert, or each has an owner.
4. There is no unresolved intent. Any older than the settle period: check the broker and settle it.
5. After the TWS restart, `BROKER_RECONNECTED` has appeared and its list matches the broker.
6. The halt is in the state you intend.

## Never

* Never delete, edit or copy the execution store, the replay store, the runtime store (the
  cursor) or the operator queue, and never run two copies of a state directory.
* Never edit the source cursor to skip a post.
* Never settle an intent without having looked at the broker yourself.
* Never place an order at the broker by hand under a `TB-` ref.
* Never clear a halt or a lockout you do not understand.
* Never put credentials, tokens or source text in the repository or in a config committed to it.
