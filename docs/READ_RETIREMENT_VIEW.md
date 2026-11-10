# Read Retirement view (LOCAL BOOKKEEPING, SIMULATED, NON_AUTHORITATIVE)

`dashboard/pages/6_Read_Retirement.py` shows the offline read-retirement ledger,
`src/tradingbot/paper_readiness/read_retirement.py` (docs/READ_ONLY_RETIREMENT.md), as a dashboard page.

It replays a fixed invented event sequence through a fresh `RetirementLedger` each time you choose one.
It then shows that ledger's own `report()` without reinterpreting it.

## Run it

```bash
PYTHONPATH=src streamlit run dashboard/app.py                         # then choose "Read Retirement"
PYTHONPATH=src streamlit run dashboard/pages/6_Read_Retirement.py     # the page on its own
PYTHONPATH=src python -m pytest tests/test_read_retirement_page.py    # the page tests
```

## Sequences

| Sequence | Ledger result |
|---|---|
| `valid_local_cessation` | Local quiescence established, as a qualified control |
| `reader_stopped_with_outstanding_work` | Not established: admission open, callbacks queued and in flight, consumer and socket `UNKNOWN` |
| `failed_or_unknown_attempt` | Not established: one attempt `FAILED`, one reserved attempt `UNKNOWN` at the seal, and a fault code |

For the chosen sequence the page shows:

- local quiescence, admission, and the queued and in-flight callback counts;
- the consumer, reader and socket states and the disconnect attempt;
- each read slot with its END, errors, completeness, data callbacks and retirement attempt;
- every fault code;
- the listed event steps;
- the unmodified report as JSON.

Any state other than `STOPPED` or `RETURNED` is marked unresolved.

## Reading it

- The events are invented, and this is local-only bookkeeping.
- Even when local quiescence is established, broker-side retirement remains
  `NOT_ESTABLISHED_BY_INSPECTED_SOURCES`, PAPER-READ-1 remains `NOT_ASSESSED` and `trading_permission`
  is false.
- A locally quiescent sequence is never broker readiness or trading approval.
- The two unresolved sequences are shown as errors, never as success.

## Limits

- The page has one fixed input, the choice of sequence. There is no free-form account, path or value
  input and no file upload.
- It runs no SDK, client or callback.
- It reads no clock, environment variable, file or network, and writes nothing.
- It has no broker, order, cancel, replace, reset, settings or credential path.
- The ledger, the runner, the shared contracts and the existing dashboard pages are unchanged.
