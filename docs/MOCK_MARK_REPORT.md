# Mock mark report (MARK-1, read-only, NON_AUTHORITATIVE)

`src/tradingbot/mark_report.py` adds `build_mark_report(snapshot, marks, policy)`. It values each open
fictional position in an existing `operations_view.OperationsSnapshot` against quotes the caller supplies
as invented marks. It fills the valuation the Mock Operations view does not show (see
[MOCK_OPERATIONS.md](MOCK_OPERATIONS.md): "valuation and P&L are unknown").

It is a pure function. It reads no ledger and writes nothing. It reads no wall clock, changes no input,
and never touches the global Decimal context. **It is not advice, a trigger, an alert, an order,
monitoring or trading permission.** Page integration (MARK-2) is a separate later step.

## Use

```python
snapshot = operations_view.build_snapshot(ledgers, now=as_of, policy=policy)
report = build_mark_report(snapshot, {"MOCKD": invented_quote}, policy)
```

## Marks

- Every mark is labelled `SUPPLIED_INVENTED`. That is the caller's statement of input scope, **not
  verified provenance**. The report never relabels a ledger or a quote as established evidence.
- Freshness is judged at the snapshot's own timezone-aware `as_of`. It uses the existing
  `operations_view.freshness` with `policy.max_quote_age_seconds`, including its five-second future
  tolerance. An age at the window is `FRESH`; past it, `STALE`. Up to five seconds ahead is `FRESH`;
  beyond that, `FUTURE`.
- A missing mark is `UNKNOWN`, and so is every mark if `as_of` has no timezone. A malformed mark is
  `INVALID`. That covers:
  - something other than a `Quote`
  - another symbol
  - a non-finite or unbounded price
  - a bid or ask at or below zero, or a crossed quote
  - a time without a timezone
- Only a `FRESH` mark values anything. Nothing falls back to zero, a last-known value or the mid.

## Exposure

Exposure is units times the executable side of the mark: the bid for a long position, the ask for a
short one. It is in the instrument's own price units. Signed exposure is positive for long and negative
for short, and gross exposure is its magnitude. Only equities and ETFs are valued; other asset classes
are `UNSUPPORTED`.

**No currency, multiplier, cross-symbol or cross-engine total is computed.** The report carries counts
only. The two engines stay separate, as in the operations view.

Exposure is `UNKNOWN` without a fresh mark, for a duplicate position key, for an invalid side or unit
count, or when a discrepancy names the position or any `SERIOUS` discrepancy exists for its engine.

## Average cost and unrealized P&L

The basis comes from the position's own fills, joined by `(engine, idea_id)`:
- Entries (`OPEN`, `ADD`) set the average, weighted by units.
- Exits (`REDUCE`, `CLOSE`) remove units at the existing average, never at the exit price.
- Amendments (`MODIFY` rows that are `SIMULATED_AMENDED`, with zero units, no price and side `NONE`) are
  not fills.
- Long P&L is `(bid − average) × units`; short P&L is `(average − ask) × units`.

The basis is `UNKNOWN` whenever the evidence does not support it:

| Reason | When |
|---|---|
| `RECONCILIATION_INCOMPLETE` | The snapshot's history is unreadable, truncated or undecodable. Completeness alone is not discrepancy-free |
| `ENGINE_DISCREPANCY_PRESENT` | The engine has any discrepancy |
| `DUPLICATE_ORDER_ID` | An order id repeats within the engine |
| `FILL_SYMBOL_MISMATCH`, `FILL_DIRECTION_MISMATCH` | A fill names another symbol, or moves the wrong way for the position's side |
| `FILL_RECORD_INVALID`, `AMENDMENT_RECORD_INVALID` | A fill status or amendment does not match its action |
| `FILL_QUANTITY_INVALID`, `FILL_PRICE_INVALID` | Units are not positive and bounded, or any fill's price, exits included, is not plain positive decimal text (at most 15 integer and 10 fractional digits). Exit prices are validated but never enter the average |
| `OPEN_FILL_MISSING`, `REPEATED_OPEN_FILL`, `CLOSE_FILL_ON_OPEN_POSITION` | The lifecycle is inconsistent |
| `FILL_QUANTITY_DISAGREES_WITH_POSITION` | Entries minus exits do not equal the position's units |
| `ORDER_DEPENDENT_BASIS_UNPROVEN` | See below |

**Fill order is not authenticated chronology.** The snapshot orders fills by ledger-write time and row
order, and carries no execution ordinal. When entries at different prices occur together with an exit,
the average depends on whether any entry followed an exit, so the basis is `UNKNOWN`. Two cases are
order-invariant and supported: entries with no exit, and entries all at one price. No ordinal is
invented and no identifier is sorted.

## Recorded levels

The stop and target distance is the level minus the executable mark, in price units. It is
informational only: **no level is monitored and no protection is active.** An unrecorded level is
reported as such. An invalid level, or one without a valued mark, has an unknown distance. The
snapshot's `protection` status and note are carried unchanged.

## Arithmetic

One local Decimal context: 34 significant digits, half-even rounding, with invalid-operation, overflow
and division-by-zero traps. Recorded prices and mark prices are bounded before use. Only the
average-cost division can round; every other value is exact for bounded inputs.

## Limits

- **Read-only.** Inputs and ledgers are unchanged. Verified by file hashes and model dumps in the tests.
- **Invented marks only.** No real quote, feed or broker is read.
- **Partial basis.** Order-dependent histories stay `UNKNOWN` until a ledger records an authenticated
  execution order. That would be a separate contract change, not made here.
- `status: COMPLETE` means only that reconciliation was complete and `as_of` timezone-aware, not that
  any value is known. It is never PAPER-READ-1, retirement or trading permission.
