# Offline callback-driven readiness demonstration (SIMULATION)

`src/tradingbot/paper_readiness/callback_demo.py` shows the readiness check driven by callbacks instead of
preassembled contract and quote envelopes. It is a **simulation**. It is NON_AUTHORITATIVE, every report
carries `trading_permission: false`, and it is not paper-readiness authorization.

Each run also reports local lifecycle cleanup next to readiness, because `CONTROL_COMPLETE` alone says
nothing about cleanup. See [Local lifecycle](#local-lifecycle).

## Run it

```bash
PYTHONPATH=src python -m tradingbot.paper_readiness.callback_demo
```

It takes no arguments. The package admits only pure standard-library imports, so the module has no `sys`
or `argparse`. It prints the three scenarios in `SCENARIOS`, then the CLI-only counterpart
`UNKNOWN_CLEANUP`. The exit status is 0 only when every scenario reaches both its expected readiness
outcome and its expected lifecycle completion, and 1 otherwise. A match means only that. It checks the
demonstration itself and is never a verdict on any real broker, account, retirement or readiness. For JSON
from Python:

```python
from tradingbot.paper_readiness import callback_demo as demo
names = [*demo.SCENARIOS, demo.UNKNOWN_CLEANUP.name]
print(demo.render_demo_json([demo.run_demo(name) for name in names]))
```

`SCENARIOS` keeps its three entries, so the dashboard's Callback Demo selector is unchanged.

## What runs

1. Invented primitive values for documented IBKR callbacks are fed to the merged
   `IbkrCallbackNormalizer` (see [IBKR_CALLBACK_NORMALIZATION.md](IBKR_CALLBACK_NORMALIZATION.md)): one
   contract-details request and one quote-snapshot request.
2. The normalizers' `ResponseEnvelope` records go into the existing `FakeScript` and
   `FakeReadOnlyTransport`.
3. The unchanged `run_readiness_check` applies the existing preflight, correlation and fail-closed
   diagnostics, and produces the usual report.

**The account summary and positions are preassembled control envelopes**, invented in this module and
labelled as such in the text and JSON output. They are not produced from callbacks. This demonstration does
not integrate any account or position callback adapter, an installed SDK, a broker or a network. The session
attestation is the fake transport's own invented record, not evidence about any session.

All fixtures (instrument `DEMOETF`, conId, prices, sizes, the `SYNTH-DEMO0001` account and the fixed clock
2026-01-14 16:00 UTC) are invented here. No existing scenario fixture is reused.

## Local lifecycle

Each run composes one merged `LifecycleAdapter` and its `RetirementLedger`
([SDK_LIFECYCLE_ADAPTER.md](SDK_LIFECYCLE_ADAPTER.md), [READ_ONLY_RETIREMENT.md](READ_ONLY_RETIREMENT.md)).
It uses invented SDK-free doubles for the client, connection, reader, queue and lock.

1. The adapter opens the `QUOTE_SNAPSHOT` read.
2. A reader double queues each invented quote callback. Only the adapter's consumer takes it from the
   queue and delivers it to the quote normalizer, so the readiness quote envelopes come only from callbacks
   the adapter dispatched.
3. `shutdown` disconnects, closes admission and drains. It then observes the reader, the socket and the
   consumer separately. The adapter makes no stop attempt for a snapshot whose END it delivered.
4. The adapter's own `report()`, with its ledger report, goes unmodified into `DemoResult.lifecycle` and
   the JSON `lifecycle_report`. Text and JSON summaries read their fields from it.

**Outside the lifecycle model.** The contract-details callbacks reach their normalizer directly, because
the ledger has no read slot for them. The account summary and positions are preassembled controls, not
callbacks. The output labels all three, and no lifecycle coverage is claimed for them.

A completed lifecycle is local only. Every adapter and ledger report stays `NON_AUTHORITATIVE`, with
`trading_permission` false, broker-side retirement `NOT_ESTABLISHED_BY_INSPECTED_SOURCES` and PAPER-READ-1
`NOT_ASSESSED`.

## Scenarios

| Scenario | Callbacks | Expected outcome | Local lifecycle |
|---|---|---|---|
| `callback_control` | One contract row and `contractDetailsEnd`; `marketDataType` 1, bid, ask, sizes, last and tick 45, then `tickSnapshotEnd` | `CONTROL_COMPLETE` | Completed: reader, socket and consumer each `STOPPED`; local quiescence established |
| `delayed_size_mixed_with_live` | As the control, plus delayed bid size tick 69 before `tickSnapshotEnd` (PR #25 review P2) | `NOT_READY`: `NON_LIVE_TICK_UNREPRESENTED` withholds the END, so no `QUOTE_LIVE_CONTROL` | Completed; cleanup is independent of readiness |
| `issue_with_repeated_end` | A valid row, `unsupported_callback("connectionClosed")`, then `contractDetailsEnd` twice (PR #25 review P1) | `NOT_READY`: `CONTRACT_DETAILS_END_MISSING`, no contract identity and no quote request | Completed; the contract read is outside the model |
| `control_with_unknown_socket_closure` (`UNKNOWN_CLEANUP`, CLI only) | As the control | `CONTROL_COMPLETE` | **Not completed**: socket `UNKNOWN` and `SOCKET_NOT_CLOSED` |

In the counterpart, the disconnect returned, `CONNECTION_CLOSED` was recorded, the queue is empty and the
reader stopped. None of these clears the unknown socket closure, so readiness `CONTROL_COMPLETE` sits next
to an incomplete lifecycle.

## Limits

- Callback values are plain Python objects shaped like documented ones. No SDK compatibility is claimed.
- Only the contract and quote reads are callback-driven. Only the quote read passes through the lifecycle
  adapter. No read stop is exercised here, because the ended snapshot needs none; the lifecycle's own tests
  cover stops.
- The lifecycle doubles run on one thread with a fixed schedule: every queued callback is delivered before
  shutdown. This adds no timing, tick, risk or retirement policy.
- The lifecycle adapter and ledger, the dashboard page and its inputs, and the shared contracts are
  unchanged.
- The fake transport, runner and diagnostics are unchanged. This adds no connected path, and connected checks
  stay blocked as described in [PAPER_READINESS.md](PAPER_READINESS.md).
- There is no order, cancel, replace, reset, settings, subscription or credential interface.
