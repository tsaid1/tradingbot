# Offline account-summary and position callbacks (NON_AUTHORITATIVE)

`src/tradingbot/paper_readiness/account_position_callbacks.py` normalizes the documented account-summary and
position callbacks into the existing `ResponseEnvelope` records. The unchanged `correlate`,
`assess_account_summary` and `assess_positions` then judge them, through the unchanged FAKE-only runner.

It imports no SDK, connects to nothing, requests nothing and reads no account. It was exercised only with
invented values. **No installed-SDK or real-account compatibility is claimed, and nothing here infers or
attests PAPER.** Connected checks stay blocked as described in [PAPER_READINESS.md](PAPER_READINESS.md).

## Official interfaces

Both pages describe themselves as deprecated and point to IBKR Campus. The current Campus pages were not
re-checked for this module.

| Callback (documented signature) | Normalized as |
|---|---|
| `accountSummary(int reqId, string account, string tag, string value, string currency)` | Rows aggregate per account into one `AccountSummaryObservation`. The `AccountType` tag becomes `account_type`. Other tags go into `values`, and a non-empty currency becomes `currency` |
| `accountSummaryEnd(int reqId)` | `ACCOUNT_SUMMARY_END`, emitted only when `completed` |
| `position(string account, Contract contract, decimal pos, double avgCost)` | One `PositionLine`, taking `conId`, `symbol`, `secType` and `currency` from the contract |
| `positionEnd()` | `POSITION_END`, emitted only when `completed` |

Signatures are from https://interactivebrokers.github.io/tws-api/interfaceIBApi_1_1EWrapper.html.
Contract members (`ConId` int, `Symbol`, `SecType` and `Currency` string) are from
https://interactivebrokers.github.io/tws-api/classIBApi_1_1Contract.html.

Subscription behaviour is from https://interactivebrokers.github.io/tws-api/account_summary.html and
https://interactivebrokers.github.io/tws-api/positions.html:
- `reqPositions()` takes no request id.
- Position updates continue after `positionEnd`.

**Unverified:** the current Campus wording, real tag sets and currencies, Contract default values, and
whether the `AccountType` row carries a currency.

## Rules

- **Correlation.** Account-summary rows keep their broker `reqId`. A row or END for another id, and any row
  after END, stays a visible record, so the existing correlation reports `UNCORRELATED_RESPONSE_IGNORED` or
  `RESPONSE_AFTER_END_IGNORED`. Positions carry no broker request id: one `PositionSnapshotCallbacks` owns
  one local snapshot, and its id (the runner's 1004) is a local correlation id, never a broker one.
- **Identity.** Only exact `SYNTH-` identifiers are representable here. Any other account string is refused
  with `ACCOUNT_NOT_SYNTHETIC` and never stored, so it cannot reach a record, issue or report. Nothing is
  converted into a synthetic identifier, and no identity is trimmed or normalized. A foreign synthetic
  account reaches the existing `ACCOUNT_ID_MISMATCH`, `ACCOUNT_SUMMARY_MULTIPLE` or
  `POSITION_ACCOUNT_MISMATCH`.
- **Types:**
  - `reqId` and `conId` are exact non-negative `int`; a `conId` of 0 is unset.
  - Text fields are exact `str`, at most 64 characters, with no control characters.
  - `pos` is a finite `Decimal` or a whole `int` from older APIs. Fractional and negative values are kept
    exactly. A `float` is refused rather than rounded.
  - `avgCost` is a finite `float` or `int`.
  - `bool` is never accepted as a number.
  - A summary value is carried as its exact string, so the existing check rejects a non-decimal value.
- **Fail closed.** Each of these latches a fixed `AccountIssueCode` and withholds every END, including a
  repeated END:
  - a malformed value or contract
  - a duplicate tag or duplicate `(account, conId)`, even with the same value
  - conflicting non-empty summary currencies
  - an unsupported callback, including any error callback
  - exceeding 256 callbacks, 8 accounts, 64 tags per account or 64 position rows

  Rejected values are never stored or rendered.

## Limits

- Errors are not normalized here. Route them to `unsupported_callback`, which fails the stream closed.
- A real `reqAccountSummary` group or tag set, multi-account structures, and the `$LEDGER` currency tags are
  untested.
- Nothing is wired into a connected transport. Real accounts stay refused by the existing schema and
  preflight (E2, T2).
