# Operations runbook (mock mode)

Three processes: the **service** (`scripts/run_service.py`), the **watchdog**
(`scripts/watchdog.py`, every 60 s) and the **dashboard** (`streamlit run dashboard/app.py`, pages
*Execution* and *Exceptions*). All read one runtime TOML (`TRADINGBOT_RUNTIME_CONFIG`, default
`~/.config/tradingbot/runtime.toml`). Only the simulated broker exists; no operator action below
sends an order.

## Start and stop

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
(`<state_dir>/alerts.jsonl`, shown on *Exceptions*) and, optionally, macOS.

| Alert | Meaning | Action |
|---|---|---|
| `ENTRY_UNKNOWN_OUTCOME` | An entry or add was sent and its outcome is unknown. It is never resent, and its contract is blocked. | Look the order up at the broker. If it does not appear on its own, settle it (below). |
| `UNPROTECTED_POSITION` | No working stop is confirmed for the position, or the check failed. | Check the stop at the broker. The alert clears only on evidence: a confirmed stop or a flat position. |
| `BROKER_FAILURE` | The broker is unreadable, or its state is not yet verified. New exposure is blocked. | Check TWS or the gateway. The service retries on its own. |
| `BROKER_RECONNECTED` | The session is back. The alert lists the fills and order ends adopted while it was down. | Read the list. If the resync waits on a discrepancy, resolve that first. |
| `BROKER_IDENTITY_MISMATCH` | The session came back under another account, or its identity check failed. The service stops touching the broker and engages the halt. | Connect the expected account, clear the lockout, then clear the halt. |
| `RECONCILIATION_DISCREPANCY`, `STUCK_INTENT` | The ledger and the broker disagree, or an intent has stayed unresolved. | Compare the two pages with the broker. |
| `LEDGER_REVERSAL` | An exit filled more than the position: the account went through flat. | Flatten the excess at the broker by hand, then CLEAR it. |
| `EXIT_REFUSED`, `HALTED_ACTION_REFUSED` | A publisher exit or amendment was not executed. | Check the position and its stop. Exit by hand if needed, then acknowledge. |
| `SETTLEMENT_UNVERIFIED` | A settlement was applied and its contract stays blocked until the evidence below has followed it. | None. It clears on its own once the evidence is in. |
| `SETTLEMENT_CONTRADICTED` | The broker shows a fill, a working order or a completed order for an intent you settled as `NOT_AT_BROKER` or `CANCELLED`. The fill is in the ledger as it happened, the contract is blocked and reconciliation is not clean. | Check the account at the broker: it may hold more than intended. Decide what to do with the extra position, then CLEAR it once reconciliation is otherwise clean. |
| `MAPPING_REVOKED_OPEN_POSITION` | A position is open in a family that has lost approval. It can be exited, never added to. | Decide whether to keep or exit the position. |
| `ROLL_DUE_FLATTEN_REQUIRED` | A futures position is inside its roll window. | Flatten or roll it by a new instruction. |
| `SOURCE_FAILURE`, `PIPELINE_FAILURE` | The source or the processing of a post has failed repeatedly. The cursor does not move. | See Source outage below. |
| `SOURCE_STALLED` | The cursor has not moved during the activity window. | Check the source account and token. |
| `HEARTBEAT_STALE` / `_RESTORED` | From the watchdog: the service has stopped, or is hung. | Check `launchctl list`, the service log and the lock holder. |
| `HALT_ENGAGED` / `HALT_CLEARED` | The halt file appeared or went away. | Confirm that you meant it. |

## Operator actions (CLI `scripts/operator.py`, or the *Exceptions* page)

Every action needs a note and the exact confirmation words. It is queued append-only in
`<state_dir>/operator.sqlite3` and applied by the process that holds the execution store: the
running service on its next cycle, or `operator.py apply` while the service is stopped. Results
show under `operator.py history` and on the page.

* `settle <ref> NOT_AT_BROKER | CANCELLED | FILLED --quantity N --price P`: declares the outcome
  of an intent whose outcome is unknown, once you have checked the broker. The ledger moves exactly
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
* `clear <id> LEDGER_REVERSAL | SETTLED_INTENT_CONTRADICTED`: applied only if a sync and
  reconciliation made at that moment show the contract clean (the record's own line excepted).
* `ack <id> <kind>`: closes a record that blocks nothing (`EXIT_NOT_NETTED`, `EXIT_WITHOUT_QUOTE`,
  `STALE_AFTER_UNKNOWN_OUTCOME`, `MODIFY_NOT_CONFIRMED`, `PROTECTIVE_LEVEL_NOT_LOOSENED`,
  `PARTIAL_GROUP`). `ack-signal <id> <kind>` closes a held signal.
* `clear-lockout <expected account>`: applied only once the broker shows the expected account
  again. The next cycle then runs the full resync.
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
