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
or `argparse`. It prints the three scenarios in `SCENARIOS`, then eight CLI-only cases: `UNKNOWN_CLEANUP`,
the two `ACCOUNT_POSITION_FAILURES`, the four `READER_FRAMING_CASES` and the one `HANDOFF_FAILURE_CASES`.
The exit status is 0 only when every case reaches its expected readiness outcome, lifecycle completion,
framing outcome and handoff failure, and 1 otherwise. A match means only that. It checks the
demonstration itself and is never a verdict on any real broker, account, retirement or readiness. For JSON from Python:

```python
from tradingbot.paper_readiness import callback_demo as demo
extra = [demo.UNKNOWN_CLEANUP, *demo.ACCOUNT_POSITION_FAILURES, *demo.READER_FRAMING_CASES,
         *demo.HANDOFF_FAILURE_CASES]
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
   | quote snapshot | The router's own `IbkrCallbackNormalizer` | the quote request id |
   | account summary | The router's own `AccountSummaryCallbacks` ([ACCOUNT_POSITION_CALLBACKS.md](ACCOUNT_POSITION_CALLBACKS.md)) | its broker-style synthetic request id, kept on every row and END |
   | positions | The router's own `PositionSnapshotCallbacks` | the single local snapshot window; its id (the runner's positions id) is local, never a broker request id |

   The three read streams' normalizers are created by the router's existing registrations, without an
   owner. Their callbacks reach them only through owned frames, `DecodeHandoff` and the router's consumer,
   and their envelopes and evidence are read back through the router (`CallbackRouter.stream_status()`).
   The demonstration builds no read normalizer of its own. No `managedAccounts` input or account owner is
   involved.

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

Each run composes one `CallbackRouter` ([CALLBACK_ROUTER.md](CALLBACK_ROUTER.md)), whose `LifecycleAdapter`
and `RetirementLedger` ([SDK_LIFECYCLE_ADAPTER.md](SDK_LIFECYCLE_ADAPTER.md),
[READ_ONLY_RETIREMENT.md](READ_ONLY_RETIREMENT.md)) account for the quote, account-summary and position
reads. It uses invented SDK-free doubles for the client, connection, reader and lock; the router supplies
the queue and dispatch.

1. The router starts and registers the `ACCOUNT_SUMMARY`, `POSITIONS` and `QUOTE_SNAPSHOT` reads.
2. Their callbacks reach it only through owned frames and the decode handoff (below), and only the
   router's consumer delivers them to the normalizers.
3. `shutdown` disconnects, closes admission and drains. It then observes the reader, the socket and the
   consumer separately. The continuing account-summary and position reads end with the invented client's
   **returned** stop hook (the existing mock hooks), never an SDK cancellation. No stop attempt is made for
   a snapshot whose END was delivered.
4. The router's adapter report, with its ledger report, goes unmodified into `DemoResult.lifecycle` and the
   JSON `lifecycle_report`. The router report is in `DemoResult.router`.

In the control the ledger shows three slots: `ACCOUNT_SUMMARY` (4 data callbacks, END, stop `RETURNED`),
`POSITIONS` (2, END, `RETURNED`) and `QUOTE_SNAPSHOT` (7, END, `NOT_RESERVED`).

**Outside the lifecycle model.** The contract-details callbacks reach their normalizer directly, not
through the router, and the ledger has no read slot for them. The output labels them, and no lifecycle,
retirement or cancellation coverage is claimed for them.

A completed lifecycle is local only. Every adapter and ledger report stays `NON_AUTHORITATIVE`, with
`trading_permission` false, broker-side retirement `NOT_ESTABLISHED_BY_INSPECTED_SOURCES` and PAPER-READ-1
`NOT_ASSESSED`.

## Frames and the decode handoff

Each quote, account-summary and position callback travels in **one owned invented frame**. Its payload is a whole invented key, not an SDK
message: the closed `InventedDecoder` ([DECODE_HANDOFF.md](DECODE_HANDOFF.md)) maps it to exactly that
callback. No message id, protobuf or wire-format selection is modelled.

1. The frames' bytes arrive fragmented in 13-byte chunks, so headers and payloads cross chunk boundaries.
   The control has sixteen frames, in this order: four summary rows and `accountSummaryEnd`, two
   positions and `positionEnd`, then `marketDataType`, six ticks and `tickSnapshotEnd`. The quote END is
   the final frame.
2. Each chunk goes into the accepted `DecodeHandoff`'s own `FrameAccumulator`
   ([READ_FRAMING.md](READ_FRAMING.md)), at its default limits.
3. After each chunk, every complete frame is reserved, taken, decoded into the `ReadCallbackFacade`, and
   settled through the router's own consumer before it is released. A frame whose delivery is not proven
   stays held, and no later frame is taken.
4. When the router joins the reader double, the reader only calls the handoff's `finish()`. Its
   `partial_bytes()` reads the handoff framing report's `bytes_retained`, so a retained partial frame makes
   the lifecycle reader `FAILED` with `PARTIAL_BYTES_UNRESOLVED`. No byte count is invented.
5. The handoff's framing report goes unmodified into `DemoResult.framing` and the JSON `framing_report`; its
   handoff report into `DemoResult.handoff`, summarized in `decode_handoff` with its per-frame records.

The three read streams' completion and issue codes come from the router's own normalizers through
`CallbackRouter.stream_status()`. They are normalizer evidence, not a delivery or lifecycle verdict.

**The aggregate.** Each part must hold on its own, so no part can stand in for another:

| Field | True only when |
|---|---|
| `streams_completed` | every stream's own normalizer completed, so one stream's success never clears another's failure |
| `local_components_complete` | the lifecycle completed, the framing outcome is `COMPLETE`, the handoff completed (every frame settled and the router's own completion) **and** `streams_completed` |
| `aggregate_complete` (JSON `local_components.all_complete`) | `local_components_complete` **and** the unchanged readiness outcome is `CONTROL_COMPLETE` |

All are local and `NON_AUTHORITATIVE`. They never change the readiness outcome, and are never PAPER-READ-1,
broker-side retirement or trading permission. The text output also prints the ledger's faults beside the
lifecycle's failures.

**Changed from the previous demonstration (quote-only delivery).** The account-summary and position callbacks
were previously fed to directly built normalizers, outside the lifecycle model. Now they share the quote's
one owning path. Readiness outcomes and findings are unchanged for every case, and the envelopes reaching the
runner equal those of feeding the same callbacks directly (tested for every case). The local counts change:

- Frames: 16 in the control (was 8); 17 with the delayed size tick and 18 for the malformed position case.
- Ledger slots: three (was one), with returned stop attempts for the summary and positions reads.
- `malformed_position_then_repeated_end`: the repeated `positionEnd` is now a `POST_END_EVENT` ledger
  fault, so its lifecycle is **not completed** (it was completed when the read was outside the model).
- `all_complete`: now requires every stream and `CONTROL_COMPLETE`. `delayed_size_mixed_with_live`,
  `issue_with_repeated_end` and `summary_monetary_currency_missing` are no longer `all_complete`. Only
  `callback_control` is; the currency case still completes all its local components.
- The handoff-failure case holds frame 16, with 15 of 16 settled (was frame 8, 7 of 8).

## Scenarios

Every scenario uses the control's account-summary and position callbacks unless its row says otherwise:
`AccountType` and three monetary rows in USD, then `accountSummaryEnd`; two positions, then `positionEnd`.

| Scenario | Callbacks | Expected outcome | Local lifecycle |
|---|---|---|---|
| `callback_control` | One contract row and `contractDetailsEnd`; `marketDataType` 1, bid, ask, sizes, last and tick 45, then `tickSnapshotEnd` | `CONTROL_COMPLETE` | Completed: reader, socket and consumer each `STOPPED`; local quiescence established |
| `delayed_size_mixed_with_live` | As the control, plus delayed bid size tick 69 before `tickSnapshotEnd` (PR #25 review P2) | `NOT_READY`: `NON_LIVE_TICK_UNREPRESENTED` withholds the END, so no `QUOTE_LIVE_CONTROL` | Completed; cleanup is independent of readiness |
| `issue_with_repeated_end` | A valid row, `unsupported_callback("connectionClosed")`, then `contractDetailsEnd` twice (PR #25 review P1) | `NOT_READY`: `CONTRACT_DETAILS_END_MISSING`, no contract identity and no quote request | Completed; the contract read is outside the model, and the failed contract stream keeps the aggregate incomplete |
| `control_with_unknown_socket_closure` (`UNKNOWN_CLEANUP`, CLI only) | As the control | `CONTROL_COMPLETE` | **Not completed**: socket `UNKNOWN` and `SOCKET_NOT_CLOSED` |
| `summary_monetary_currency_missing` (CLI only) | As the control, but `BuyingPower` arrives with an empty currency (PR #35 review P2) | `NOT_READY`: `ACCOUNT_CURRENCY_MISSING`, so no `ACCOUNT_SNAPSHOT_CONTROL` | Completed: every row is delivered and settled and the normalizer completes; readiness alone keeps `all_complete` false |
| `malformed_position_then_repeated_end` (CLI only) | As the control, then an oversized position quantity and `positionEnd` twice (PR #35 review P2) | `NOT_READY`: `MALFORMED_CALLBACK` withholds every END, so `POSITIONS_END_MISSING` | **Not completed**: all 18 frames settle, but the repeated END is a `POST_END_EVENT` ledger fault. The failure stays after the earlier END, and the completed summary and quote streams do not clear it |

Every scenario above carries its read callbacks in owned frames with no trailing bytes, so its framing
outcome is `COMPLETE` and every frame settles. The four reader-framing cases (`READER_FRAMING_CASES`, CLI
only) keep every callback of the control, all delivered, so readiness stays `CONTROL_COMPLETE`. Each differs
from the control only in trailing bytes after its sixteen read frames, or in whether the final chunk's frame
is handed to decoding:

| Case | Reader input | Framing outcome | Local lifecycle | Both local components |
|---|---|---|---|---|
| `framing_partial_header` | The control's frames, then two header bytes of a further frame | `PARTIAL_HEADER`, 2 bytes retained | **Not completed**: reader `FAILED`, `PARTIAL_BYTES_UNRESOLVED` | No |
| `framing_partial_payload` | The control's frames, then a further frame's header and six payload bytes | `PARTIAL_PAYLOAD`, 10 bytes retained | **Not completed**: reader `FAILED`, `PARTIAL_BYTES_UNRESOLVED` | No |
| `framing_declared_length_refused` | The control's frames, then a header declaring a length beyond the payload limit | `REFUSED` (`DECLARED_LENGTH_TOO_LARGE`), its 4 header bytes retained | **Not completed**: reader `FAILED`, `PARTIAL_BYTES_UNRESOLVED` | No |
| `framing_untaken_frames` | The control's frames, then one further complete frame in a final chunk that is never handed to decoding | `UNTAKEN_FRAMES`, nothing retained | Completed: the lifecycle model cannot express untaken frames | No |

The unknown-socket counterpart keeps its incomplete lifecycle, with framing `COMPLETE`, so its aggregate is
incomplete too.

The handoff-failure case (`HANDOFF_FAILURE_CASES`, CLI only) shows a failure **after a complete frame but
before its settlement**:

| Case | What happens | Readiness | Local lifecycle and handoff |
|---|---|---|---|
| `handoff_decoder_raised_after_snapshot_end` | The control's frames; the invented decoder raises right after delivering `tickSnapshotEnd`, so that final frame's admitted END is never settled by the handoff | `CONTROL_COMPLETE`: shutdown later drains the admitted END and the stream completes | **Not completed**: handoff `DECODER_RAISED`, frame 16 held (1 admitted, 0 dispatched), 15 of 16 settled; the END arrived after the stop attempt, so the ledger records `LATE_CALLBACK` and the quote read is not complete. The completed summary and positions reads do not clear it, and the failure persists after END |

In the counterpart, the disconnect returned, `CONNECTION_CLOSED` was recorded, the queue is empty and the
reader stopped. None of these clears the unknown socket closure, so readiness `CONTROL_COMPLETE` sits next
to an incomplete lifecycle.

## Limits

- Callback values are plain Python objects shaped like documented ones. No SDK compatibility is claimed.
- All four reads are callback-driven. The quote, account-summary and position reads pass through the
  lifecycle adapter; contract details do not. The only stops are the invented client's existing mock hooks
  for the continuing summary and positions reads. No SDK cancellation exists here, and a returned stop is
  not broker-side retirement.
- Only exact `SYNTH-` account identities are representable, as in the accepted account normalizer. The two
  account and position failure cases are fixed; there is no free-form callback input.
- The doubles run on one thread with a fixed schedule: each frame is decoded and settled as its chunk
  completes it. This adds no timing, tick, risk or retirement policy.
- The frames are invented: whole invented keys mapped by a closed decoder to fixed callbacks. This is not a
  reader of any real socket or SDK stream, and it makes no protocol, message-id, protobuf, decoding or
  SDK-admission claim. The accumulator's limits are its documented defaults, chosen as model policy.
- The normalizers, the accumulator, the decode handoff, the router, the lifecycle adapter and ledger, and
  the shared contracts are unchanged. The dashboard page keeps its three-scenario input; it now labels the
  three read streams' actual coverage and shows the ledger slots and the aggregate's parts
  ([CALLBACK_DEMO_VIEW.md](CALLBACK_DEMO_VIEW.md)).
- The fake transport, runner and diagnostics are unchanged. This adds no connected path, and connected checks
  stay blocked as described in [PAPER_READINESS.md](PAPER_READINESS.md).
- There is no order, cancel, replace, reset, settings, subscription or credential interface.
