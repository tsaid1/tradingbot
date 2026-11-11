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
table (`paths`, `source`, `service`, `policy`) or any mode other than `mock` refuse to start.
Relative paths resolve against the config file.

* `[paths]`: `state_dir` (runtime, execution, replay and SimBroker SQLite files, `heartbeat.json`,
  `notifications.log`), `halt_file`, `mapping_file`.
* `[source]`: `kind = "replay"` (`file`, optional `compression`, `lag_seconds`,
  `expected_author_id` for G0 archive records) or `kind = "x_timeline"` (`author_id`, `token_file`,
  `client_file`; both private files, mode 0600 in a 0700 directory outside cloud storage).
* `[service]`: `cadence_seconds` (5), `max_backoff_seconds`, `failure_alert_after`,
  `stuck_after_seconds`. `[policy]`: `MockPolicy`. `[execution]`, `[interpreter] lexicon_path`,
  `[notify] macos`, `[mock] account, quotes` (a static simulated market).

## Sources and the cursor

`poll()` returns posts after the committed cursor and does not move it; the service commits each
post only after it is processed and journaled. A restart re-polls exactly the uncommitted posts.
A post processed but not committed before a crash is replayed as a recorded duplicate: no second
order. `XTimelineSource` starts with a baseline (posts that existed before the first run are never
acted on), reads every page since `since_id` (more than `max_pages` raises `SourceGap`), marks a
note-less ellipsis-terminated body incomplete, maps edits to revision N of the original post,
marks posts by any other author unverified, and backs off on 429 until the reset.

## Mapping policy

`config/instrument_mapping.toml` names, per source asset, the broker contract and the price-basis
relationship. Only `DIRECT` (levels quoted in the traded instrument's own price, same symbol)
executes; one entry ships: TLT. Unmapped assets and every other basis (`INDEX_LEVEL`,
`SPOT_LEVEL`, `FUTURES_LEVEL`, `PROXY`, `UNKNOWN`) get the ambiguity
`UNSUPPORTED_INSTRUMENT_MAPPING`, so the gateway HOLDs them. An index or spot level is never treated
as an ETF or futures price.

## What is held, and why

Every action whose outcome is `HOLD`, `BLOCK`, `REFUSED`, `HALTED`, `UNRESOLVED`, `EXCEPTION` or
`REJECTED` becomes a `SIGNAL_<status>` exception with the gateway's or the coordinator's reason
codes: unsupported mapping, edits (`SOURCE_EDIT_REQUIRES_REVIEW`), unverified or truncated
posts, stale sources or quotes, collar or exposure breaches, the halt, unknown broker outcomes.
Commentary (`NO_TRADE`) and recaps (`RECONCILE`) are journaled, not held. Nothing held is traded.

## Failures, halt, notifications

A broker that cannot be read stops the cycle before the source is touched, so no signal is consumed
during an outage. Source, broker and processing failures back off exponentially (bounded) and never
move the cursor. Unknown order outcomes are looked up again every cycle (see
`docs/EXECUTION_CORE.md`). While the halt file exists nothing is submitted or amended; posts are
still journaled (`HALTED`). Notifications (log file, optional macOS): reconciliation discrepancy,
unprotected position, halt engaged, repeated source or broker failure, stuck intent; each once per
occurrence. Post text is never stored; the journal keeps hashes, identities, reason codes and
timestamps for every stage (published, received, interpreted, gate decided, submitted,
acknowledged, filled).
