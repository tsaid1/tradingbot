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
* Optional: `[interpreter] lexicon_path`, `[notify] channels, macos, realert_seconds,
  alerts_file`, `[service] source_stall_after_seconds, activity_window_utc,
  heartbeat_stale_after_seconds` (alerting only), `[mock] account, quotes` (a static simulated
  market).

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
config entry. `scripts/watchdog.py` alerts on a stale heartbeat.

**Broker session:** a failed probe marks the session lost (`BROKER_RECONNECT_PENDING` refuses
every OPEN and ADD, protection becomes UNKNOWN). Reconnection is retried on the broker backoff and
re-runs the adapter's identity checks; a session under another account locks the service out of
the broker and engages the halt. The block lifts only after a full sync and a reconciliation with
nothing unexplained; what changed meanwhile is adopted from broker evidence and reported
(`BROKER_RECONNECTED`).

**Operator actions** (`scripts/operator.py`, dashboard page *Exceptions*) are queued and applied
at the start of the next cycle; see `docs/OPERATIONS.md`. Every publisher-driven REDUCE, CLOSE or MODIFY refused because the halt is engaged sends
`HALTED_ACTION_REFUSED` naming the idea, action and order ref (identifiers and reason codes only). Post text is never stored; the journal keeps hashes, identities, reason codes and
timestamps for every stage (published, received, interpreted, gate decided, submitted,
acknowledged, filled).
