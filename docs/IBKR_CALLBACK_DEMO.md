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
or `argparse`. It prints the three scenarios in `SCENARIOS`, then seven CLI-only cases: `UNKNOWN_CLEANUP`,
the two `ACCOUNT_POSITION_FAILURES` and the four `READER_FRAMING_CASES`. The exit status is 0 only when
every case reaches its expected readiness outcome, lifecycle completion and framing outcome, and 1
otherwise. A match means only that. It checks the demonstration itself and is never a verdict on any real
broker, account, retirement or readiness. For JSON from Python:

```python
from tradingbot.paper_readiness import callback_demo as demo
extra = [demo.UNKNOWN_CLEANUP, *demo.ACCOUNT_POSITION_FAILURES, *demo.READER_FRAMING_CASES]
names = [*demo.SCENARIOS, *(s.name for s in extra)]
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

## Reader framing

The quote reader double's buffer is the accepted `FrameAccumulator` ([READ_FRAMING.md](READ_FRAMING.md)),
with its default limits.

1. Each case gives the reader fixed invented opaque byte chunks in the accumulator's documented frame shape:
   a four-byte length, then the payload. The control's chunks hold three complete frames, one of them
   zero-length, and split headers and a payload across chunk boundaries.
2. Normal processing feeds each chunk, then hands off with `take()` every frame that chunk completed. The
   reader double holds handed-off frames unprocessed. Nothing decodes a payload. The bytes are not SDK
   messages, do not carry the quote callbacks and do not affect callback routing.
3. When the adapter joins the reader, the reader only calls `finish()`. Nothing is drained, taken,
   discarded or decoded at shutdown.
4. The adapter's reader observation reads `partial_bytes()`, which returns the accumulator's observed
   `bytes_retained`. A retained partial frame therefore makes the lifecycle reader `FAILED` with
   `PARTIAL_BYTES_UNRESOLVED`. No byte count is invented.
5. The accumulator's own report goes unmodified into `DemoResult.framing` and the JSON `framing_report`.
   Only enum members are written as their values.

**Two reports, one aggregate.** The lifecycle model has no state for a refusal without retained bytes or
for untaken frames, so those appear only in the framing report. `DemoResult.local_components_complete`
(JSON `local_components.both_complete`) is true only when the lifecycle completed **and** the framing
outcome is `COMPLETE`. It is local and `NON_AUTHORITATIVE`. It never changes the readiness outcome, and
it is never PAPER-READ-1, broker-side retirement or trading permission. A handed-off frame is local buffer
accounting only (`downstream_retirement` stays `NOT_ESTABLISHED`); it is not consumer processing or
retirement.

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

Every scenario above uses the control's reader bytes, so its framing outcome is `COMPLETE`. The four
reader-framing cases (`READER_FRAMING_CASES`, CLI only) keep every callback of the control, so readiness
stays `CONTROL_COMPLETE`. Each differs from the control only in the reader's bytes, or in whether the
reader hands off its final frames:

| Case | Reader input | Framing outcome | Local lifecycle | Both local components |
|---|---|---|---|---|
| `framing_partial_header` | The control's chunks, then two header bytes of a further frame | `PARTIAL_HEADER`, 2 bytes retained | **Not completed**: reader `FAILED`, `PARTIAL_BYTES_UNRESOLVED` | No |
| `framing_partial_payload` | The control's chunks, then a further frame's header and six payload bytes | `PARTIAL_PAYLOAD`, 10 bytes retained | **Not completed**: reader `FAILED`, `PARTIAL_BYTES_UNRESOLVED` | No |
| `framing_declared_length_refused` | The control's chunks, then a header declaring a length beyond the payload limit | `REFUSED` (`DECLARED_LENGTH_TOO_LARGE`), its 4 header bytes retained | **Not completed**: reader `FAILED`, `PARTIAL_BYTES_UNRESOLVED` | No |
| `framing_untaken_frames` | The control's chunks; the frames the last chunk completes are never handed off | `UNTAKEN_FRAMES`, nothing retained | Completed: the lifecycle model cannot express untaken frames | No |

The unknown-socket counterpart keeps its incomplete lifecycle, with framing `COMPLETE`, so it is not
complete on both components either.

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
- The reader bytes are fixed invented opaque controls, unrelated to the callbacks. This is not a reader of
  any real socket or SDK stream. It does not resolve which component owns a real reader buffer, and it
  makes no protocol, decoding or SDK-admission claim. The accumulator's limits are its documented
  defaults, chosen as model policy.
- The normalizers, the accumulator, the lifecycle adapter and ledger, and the shared contracts are
  unchanged. The dashboard page and its three-scenario input are unchanged; its JSON view now also carries
  the framing report.
- The fake transport, runner and diagnostics are unchanged. This adds no connected path, and connected checks
  stay blocked as described in [PAPER_READINESS.md](PAPER_READINESS.md).
- There is no order, cancel, replace, reset, settings, subscription or credential interface.
