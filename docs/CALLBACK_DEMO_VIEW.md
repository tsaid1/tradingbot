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
- Callback ingestion: completion and issue codes for the contract and quote streams.
- The request sequence in dispatch order, with request id, stage, rows kept, END seen and envelope
  source. Any read that was not dispatched is listed as unknown, not passed.
- Every non-INFO finding by stage, with its code and documented detail.
- The report text from the existing `render_text`, and the JSON from the existing `render_demo_json`.

`CONTROL_COMPLETE` means only that the invented observation set was complete and consistent. It is not
paper readiness and never authorizes trading. Both `NOT_READY` scenarios are shown as errors, never as
success.

## Limits

- The account summary and positions are **PREASSEMBLED** invented control envelopes, not callbacks. They
  are not callback, SDK, broker or paper-account proof.
- No input account or value record is shown. Only the redacted report is displayed: an account
  reference, codes, counts and digests.
- The page reads no file, environment variable, wall clock or network, and writes nothing.
- It has no free-form input and no broker, order, cancel, replace, reset, settings or credential path.
- Shared contracts, the runner, the demonstration and the existing dashboard pages are unchanged.
