# Offline callback-driven readiness demonstration (SIMULATION)

`src/tradingbot/paper_readiness/callback_demo.py` shows the readiness check driven by callbacks for all
four readiness streams: contract details, quote snapshot, account summary and positions. No envelope is
preassembled. It is a **simulation**. It is NON_AUTHORITATIVE, every report carries
`trading_permission: false`, and it is not paper-readiness authorization.

Each run also reports local lifecycle cleanup next to readiness, because `CONTROL_COMPLETE` alone says
nothing about cleanup. See [Local lifecycle](#local-lifecycle).

## Run it

```bash
PYTHONPATH=src python -m tradingbot.paper_readiness.callback_demo
```

It takes no arguments. The package admits only pure standard-library imports, so the module has no `sys`
or `argparse`. It prints the three scenarios in `SCENARIOS`, then three CLI-only cases: `UNKNOWN_CLEANUP`
and the two `ACCOUNT_POSITION_FAILURES`. The exit status is 0 only when every case reaches both its
expected readiness outcome and its expected lifecycle completion, and 1 otherwise. A match means only that.
It checks the demonstration itself and is never a verdict on any real broker, account, retirement or
readiness. For JSON from Python:

```python
from tradingbot.paper_readiness import callback_demo as demo
names = [*demo.SCENARIOS, demo.UNKNOWN_CLEANUP.name, *(s.name for s in demo.ACCOUNT_POSITION_FAILURES)]
print(demo.render_demo_json([demo.run_demo(name) for name in names]))
```

`SCENARIOS` keeps its three entries, so the dashboard's Callback Demo selector is unchanged.

## What runs

1. Invented primitive values for documented IBKR callbacks are fed to the accepted offline normalizers,
   one per readiness stream:

   | Stream | Normalizer | Correlation |
   |---|---|---|
   | contract details | `IbkrCallbackNormalizer` ([IBKR_CALLBACK_NORMALIZATION.md](IBKR_CALLBACK_NORMALIZATION.md)), called directly | the contract-details request id |
   | quote snapshot | `IbkrCallbackNormalizer`, reached only through the lifecycle adapter's consumer | the quote request id |
   | account summary | `AccountSummaryCallbacks` ([ACCOUNT_POSITION_CALLBACKS.md](ACCOUNT_POSITION_CALLBACKS.md)), called directly | its broker request id, kept on every row and END |
   | positions | `PositionSnapshotCallbacks`, called directly | one local snapshot; its id (the runner's positions id) is local, never a broker request id |

2. The normalizers' `ResponseEnvelope` records go into the existing `FakeScript` and
   `FakeReadOnlyTransport`. No envelope is preassembled, on success or failure.
3. The unchanged `run_readiness_check` applies the existing preflight, correlation and fail-closed
   diagnostics, and produces the usual report.

The text and JSON output label each stream's source (`STREAM_SOURCES`) and its callback completion and
issue codes. The session attestation is the fake transport's own invented record, not evidence about any
session. No installed SDK, broker, account or network is involved.

All fixtures (instrument `DEMOETF`, conIds, prices, sizes, account-summary values, position quantities, the
`SYNTH-DEMO0001` account and the fixed clock 2026-01-14 16:00 UTC) are invented here. No existing scenario
fixture is reused. The control's positions are one fractional negative and one fractional positive
quantity, which the normalizer keeps exactly.

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

**Outside the lifecycle model.** The contract-details, account-summary and position callbacks reach their
normalizers directly, not through the adapter. The ledger has no read slot for contract details. It does
model account-summary and position reads, but this demonstration does not route them through it. The output
labels all three, and no lifecycle, retirement or cancellation coverage is claimed for them.

A completed lifecycle is local only. Every adapter and ledger report stays `NON_AUTHORITATIVE`, with
`trading_permission` false, broker-side retirement `NOT_ESTABLISHED_BY_INSPECTED_SOURCES` and PAPER-READ-1
`NOT_ASSESSED`.

## Scenarios

Every scenario uses the control's account-summary and position callbacks unless its row says otherwise:
`AccountType` and three monetary rows in USD, then `accountSummaryEnd`; two positions, then `positionEnd`.

| Scenario | Callbacks | Expected outcome | Local lifecycle |
|---|---|---|---|
| `callback_control` | One contract row and `contractDetailsEnd`; `marketDataType` 1, bid, ask, sizes, last and tick 45, then `tickSnapshotEnd` | `CONTROL_COMPLETE` | Completed: reader, socket and consumer each `STOPPED`; local quiescence established |
| `delayed_size_mixed_with_live` | As the control, plus delayed bid size tick 69 before `tickSnapshotEnd` (PR #25 review P2) | `NOT_READY`: `NON_LIVE_TICK_UNREPRESENTED` withholds the END, so no `QUOTE_LIVE_CONTROL` | Completed; cleanup is independent of readiness |
| `issue_with_repeated_end` | A valid row, `unsupported_callback("connectionClosed")`, then `contractDetailsEnd` twice (PR #25 review P1) | `NOT_READY`: `CONTRACT_DETAILS_END_MISSING`, no contract identity and no quote request | Completed; the contract read is outside the model |
| `control_with_unknown_socket_closure` (`UNKNOWN_CLEANUP`, CLI only) | As the control | `CONTROL_COMPLETE` | **Not completed**: socket `UNKNOWN` and `SOCKET_NOT_CLOSED` |
| `summary_monetary_currency_missing` (CLI only) | As the control, but `BuyingPower` arrives with an empty currency (PR #35 review P2) | `NOT_READY`: `ACCOUNT_CURRENCY_MISSING`, so no `ACCOUNT_SNAPSHOT_CONTROL` | Completed; the account read is outside the model |
| `malformed_position_then_repeated_end` (CLI only) | As the control, then an oversized position quantity and `positionEnd` twice (PR #35 review P2) | `NOT_READY`: `MALFORMED_CALLBACK` withholds every END, so `POSITIONS_END_MISSING` | Completed; the position read is outside the model |

In the counterpart, the disconnect returned, `CONNECTION_CLOSED` was recorded, the queue is empty and the
reader stopped. None of these clears the unknown socket closure, so readiness `CONTROL_COMPLETE` sits next
to an incomplete lifecycle.

## Limits

- Callback values are plain Python objects shaped like documented ones. No SDK compatibility is claimed.
- All four reads are callback-driven, but only the quote read passes through the lifecycle adapter. No read
  stop is exercised here, because the ended snapshot needs none; the lifecycle's own tests cover stops.
- Only exact `SYNTH-` account identities are representable, as in the accepted account normalizer. The two
  account and position failure cases are fixed; there is no free-form callback input.
- The lifecycle doubles run on one thread with a fixed schedule: every queued callback is delivered before
  shutdown. This adds no timing, tick, risk or retirement policy.
- The normalizers, the lifecycle adapter and ledger, and the shared contracts are unchanged. The dashboard
  page keeps its three-scenario input and only relabels stream sources.
- The fake transport, runner and diagnostics are unchanged. This adds no connected path, and connected checks
  stay blocked as described in [PAPER_READINESS.md](PAPER_READINESS.md).
- There is no order, cancel, replace, reset, settings, subscription or credential interface.
