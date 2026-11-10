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
  - quote snapshot: `IbkrCallbackNormalizer`, reached only through the local lifecycle adapter's consumer
  - account summary: `AccountSummaryCallbacks`, called directly, with its broker request id kept
  - positions: `PositionSnapshotCallbacks`, called directly; one local snapshot, whose id is local and
    never a broker request id

  Contract details, account summary and positions are labelled outside the lifecycle model.
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
- Shared contracts, the runner, the normalizers, the lifecycle adapter and the other dashboard pages are
  unchanged.
