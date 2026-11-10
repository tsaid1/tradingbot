# IBKR account-summary and position callback normalization (NON-AUTHORITATIVE)

`src/tradingbot/paper_readiness/ibkr_account_callbacks.py` turns one bounded snapshot of the
documented `accountSummary`/`accountSummaryEnd` and `position`/`positionEnd` callbacks into the
existing `ResponseEnvelope` records. The unchanged `runner.correlate` and
`diagnostics.assess_account_summary`/`assess_positions` then judge those records.

It is an injected-value adapter only. A future read-only transport would forward the callbacks' values
to two collectors. The module imports no broker SDK, opens no socket, dispatches no request and holds no
credential. It infers no session readiness, and its findings grant no permission. The tests use
invented `SYNTH-` accounts and invented events, so they show nothing about an installed SDK, a
connected session or a real account.

## Interface

| Collector | Callbacks | Correlation |
|---|---|---|
| `AccountSummaryCollector(request_id)` | `accountSummary(reqId, account, tag, value, currency)`, `accountSummaryEnd(reqId)` | `BROKER_REQUEST_ID`: each envelope keeps the `reqId` its callback named |
| `PositionSnapshotCollector(request_id)` | `position(account, contract, position, avgCost)`, `positionEnd()` | `LOCAL_SINGLE_ACTIVE_SNAPSHOT`: the callbacks carry no request id |

`snapshot()` returns a `CallbackSnapshot` with a status, the envelopes and the collector's own findings.
It is a pure view. The statuses are:

- `END_RECEIVED`: the END for this snapshot arrived.
- `END_NOT_RECEIVED`: it did not.
- `UNSUPPORTED`: nothing was normalized and no envelope is returned.

None of these is a readiness verdict, which stays with correlation and assessment.

**Account summary.** Rows group by request id, account and side of this request's END marker. Each
group becomes one `AccountSummaryObservation`. The `AccountType` value goes to `account_type`, and
every other tag goes to `values` verbatim. The single non-empty row currency becomes the observation's
`currency`.

**Positions.** Each `position` callback becomes one `PositionLine`. `conId`, `symbol`, `secType` and
`currency` come from the contract's attributes. The position is kept as exact decimal text, and so is
the average cost.

## Fail-closed rules

| Input | Handling | Judgement |
|---|---|---|
| Complete USD summary, position or empty book, with END | Emitted | Existing assessments, unchanged |
| Row or END with another request id | Kept under that id, never relabelled | `UNCORRELATED_RESPONSE_IGNORED` |
| Rows or lines for another account | Their own observation or line | `ACCOUNT_SUMMARY_MULTIPLE`, `ACCOUNT_ID_MISMATCH` or `POSITION_ACCOUNT_MISMATCH` |
| Malformed or non-finite summary value or position | Kept verbatim | `ACCOUNT_VALUES_UNPARSEABLE` or `POSITION_LINES_INCOMPLETE` |
| No END | Never fabricated | `*_END_MISSING` |
| Anything after END, including a second END | Emitted after the END envelope | `RESPONSE_AFTER_END_IGNORED` |
| Exact duplicate summary row, or exact duplicate position before END | Dropped once | INFO `CALLBACK_DUPLICATE_DROPPED` |
| Same tag twice with another value or currency, or one account's rows in two currencies | No envelopes | `UNSUPPORTED`: `CALLBACK_TAG_CONFLICT` or `CALLBACK_CURRENCY_CONFLICT` |
| Same account and conId twice with other content before END | No envelopes | `UNSUPPORTED`: `CALLBACK_POSITION_CONFLICT` |
| Non-finite average cost | No envelopes | `UNSUPPORTED`: `CALLBACK_AVERAGE_COST_NOT_FINITE` |
| conId not a positive int; another argument type; text over 256 characters | No envelopes | `UNSUPPORTED` |
| Account not matching `SYNTH-…` | Not retained; no envelopes | `UNSUPPORTED`: `CALLBACK_ACCOUNT_NOT_SYNTHETIC` |
| More than 64 callbacks, counting END and later ones | No envelopes | `UNSUPPORTED`: `CALLBACK_LIMIT_EXCEEDED` |

What the envelope records can represent faithfully is emitted, and the existing code reports it as a
non-success. What they cannot represent is refused whole, so a conflict is never overwritten. A refusal
is permanent for that snapshot: no later callback, and no repeated END, can turn it into a complete
one. The bound is the existing `MAX_RESPONSES_PER_REQUEST`.

Only the average cost is checked here, because no assessment reads it. A float position is refused,
because the documented position is a decimal. A conId of 0 is refused rather than read as "unset",
because the Contract page states no default. Findings carry codes, counts and documented tag names
only, never an account identifier, a value or an undocumented tag name.

## The positions correlation limit

`position` and `positionEnd` have no request id, and `reqPositions` covers every position in every
associated account. The collector's `request_id` is therefore only a local label for one snapshot. It
is valid while exactly one positions subscription is active, and no broker correlation is claimed. The
collector cannot detect a second concurrent subscription.

`positionEnd` ends only the initial batch, and later updates are emitted after it as non-success. A
future transport must therefore stop the subscription at `positionEnd`. `reqPositionsMulti` has a
request id but is not used here.

## Sources

All accessed 2026-11-10. These are IBKR's GitHub-hosted TWS API reference pages, read through a
summarising fetch tool and not pinned by bytes. The newer IBKR Campus pages and any SDK source or
installed SDK were not consulted. The pages give C#-style types (`string`, `decimal`, `double`). That
the Python callbacks pass `str`, `Decimal` and `float` is an assumption here, not a verified fact.

| Source | Used for |
|---|---|
| [EWrapper reference](https://interactivebrokers.github.io/tws-api/interfaceIBApi_1_1EWrapper.html) | Signatures `accountSummary(int reqId, string account, string tag, string value, string currency)`, `accountSummaryEnd(int reqId)`, `position(string account, Contract contract, decimal pos, double avgCost)`, `positionEnd()`. The fetch covered about 100,000 of 105,702 characters |
| [Account summary](https://interactivebrokers.github.io/tws-api/account_summary.html) | `reqAccountSummary(reqId, group, tags)`; changed values re-sent every three minutes; `cancelAccountSummary`; only two active summary subscriptions; `$LEDGER` currency variants |
| [Positions](https://interactivebrokers.github.io/tws-api/positions.html) | No request id; all associated accounts; `positionEnd` after the initial batch only; later updates; `cancelPositions`; unavailable for more than 50 subaccounts; `reqPositionsMulti` has a request id |
| [Contract](https://interactivebrokers.github.io/tws-api/classIBApi_1_1Contract.html) | `ConId` is "the unique IB contract identifier"; no default is stated |

## Still required before any connected use

None of this is permitted now:

- A reviewed read-only transport that issues the requests and stops each stream at its END, using
  `cancelAccountSummary` and `cancelPositions`.
- Error-callback handling for these requests.
- An approach to the three-minute summary updates.
- Single-account scope, without `$LEDGER` tags.
- Every owner prerequisite in docs/PAPER_READINESS.md.

Stream cancellation is a future transport prerequisite, not permission to connect. The shared contracts,
the runner, the diagnostics and the package exports are unchanged.

Tests: `tests/test_ibkr_account_callbacks.py`, 41 synthetic cases.
