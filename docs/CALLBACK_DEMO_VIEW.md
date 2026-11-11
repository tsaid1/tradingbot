# Callback Demo view (OFFLINE SIMULATION, NON_AUTHORITATIVE)

`dashboard/pages/5_Callback_Demo.py` shows the merged offline callback demonstration,
`src/tradingbot/paper_readiness/callback_demo.py` (docs/IBKR_CALLBACK_DEMO.md), as a dashboard page.
It is a simulation: invented callback values are replayed through the FAKE transport under a FAKE clock.
It has no trading authority, and every report stays `NON_AUTHORITATIVE` with
`trading_permission: false`.

## Run it

```bash
PYTHONPATH=src streamlit run dashboard/app.py                     # then choose "Callback Demo"
PYTHONPATH=src streamlit run dashboard/pages/5_Callback_Demo.py   # the page on its own
PYTHONPATH=src python -m pytest tests/test_callback_demo_page.py  # the page tests
```

## What it shows

The page has one input: a fixed choice among the demonstration's three invented scenarios.

| Scenario | Expected outcome |
|---|---|
| `callback_control` | `CONTROL_COMPLETE` |
| `delayed_size_mixed_with_live` | `NOT_READY` |
| `issue_with_repeated_end` | `NOT_READY` |

For the chosen scenario it shows:

- The expected and actual outcome, and whether they match.
- Callback ingestion: completion and issue codes for all four streams. Each stream's envelope source
  and lifecycle coverage come from the demonstration's `STREAM_SOURCES`:
  - contract details: `IbkrCallbackNormalizer`, called directly
  - quote snapshot: `IbkrCallbackNormalizer`
  - account summary: `AccountSummaryCallbacks`, with its broker-style synthetic request id kept
  - positions: `PositionSnapshotCallbacks`; one local snapshot window, whose id is local and never a
    broker request id

  The quote, account-summary and position callbacks reach their normalizers only in owned invented
  frames, through the decode handoff and the router's lifecycle-adapter consumer. Contract details are
  labelled outside the lifecycle model.
- Local read composition, from the demonstration's own reports:
  - one row per lifecycle read slot (`ACCOUNT_SUMMARY`, `POSITIONS`, `QUOTE_SNAPSHOT`), with its data
    callbacks, END seen, read complete and stop attempt. The continuing summary and positions reads end
    with the invented client's returned stop hook, never an SDK cancellation.
  - the aggregate's parts: lifecycle (with its ledger faults), framing, decode handoff (settled frames
    and failure), every stream, local components and `all_complete`. `all_complete` also requires
    `CONTROL_COMPLETE`, so in the bundled scenarios only `callback_control` shows it.
- The request sequence in dispatch order, with request id, stage, rows kept, END seen and envelope
  source. Any read that was not dispatched is listed as unknown, not passed.
- Every non-INFO finding by stage, with its code and documented detail.
- The report text from the existing `render_text`, and the JSON from the existing `render_demo_json`.

`CONTROL_COMPLETE` means only that the invented observation set was complete and consistent. It is not
paper readiness and never authorizes trading. Both `NOT_READY` scenarios are shown as errors, never as
success.

## Limits

- Every stream comes from **invented** callback values through the accepted offline normalizers. None is
  preassembled. This is not SDK, broker or paper-account proof.
- The demonstration's CLI-only cases, including its two account and position failure cases, are not
  on the page. The page keeps its three-scenario input.
- No input account or value record is shown. Only the redacted report is displayed: an account
  reference, codes, counts and digests.
- The page reads no file, environment variable, wall clock or network, and writes nothing.
- It has no free-form input and no broker, order, cancel, replace, reset, settings or credential path.
- The composition rows are local and `NON_AUTHORITATIVE`. They are never broker-side retirement,
  PAPER-READ-1 or trading permission.
- Shared contracts, the runner, the normalizers, the router, the decode handoff, the lifecycle adapter
  and the other dashboard pages are unchanged.
