# Callback router (SDK-free, NON_AUTHORITATIVE)

`src/tradingbot/paper_readiness/callback_router.py` adds `CallbackRouter`. It owns the association between
a callback's payload and the lifecycle adapter's consumer, and routes each invented EWrapper-named callback
to the accepted normalizer that owns its request. It composes the merged `LifecycleAdapter`,
`IbkrCallbackNormalizer` and the account and position producers unchanged.

It imports no SDK, opens no socket and starts no thread. **Real SDK integration is a later, separately
reviewed task.** `completed` is local only: it is never broker-side retirement
(`NOT_ESTABLISHED_BY_INSPECTED_SOURCES`), PAPER-READ-1 acceptance or trading permission. The adapter,
ledger, normalizers, runner, transport, guards, demo, exports and schemas are unchanged.

## Use

```python
router = CallbackRouter(client, lock, clock)  # client: connect / stop_read / disconnect only
router.start()
router.register_contract(rid); router.register_quote(rid, con_id)
router.register_account_summary(rid); router.register_positions(snapshot_id)
# Deliver callbacks by their EWrapper names, consume, then:
router.shutdown(join_timeout)
router.report()  # RouterReport: fixed fault codes and counts, never a payload
```

The router builds the one adapter itself. It wraps the caller's client double so that the queue and
`dispatch` belong to the router, and only fixed attribute names are read from what `connect` returns.

## Payload ownership

Quote, account-summary and position callbacks are lifecycle reads. Each is copied into an immutable record
with a unique id. Retention and admission happen as one step: the record is queued only if the adapter's
public report shows it was admitted, so a refused admission retains nothing dispatchable.

The adapter's consumer fetches through the router's queue one record at a time. A second fetch while one
is outstanding is refused (`FETCH_WHILE_OUTSTANDING`). The boundary's `dispatch` delivers only that
fetched record, once. Equal `(slot, kind)` values therefore cannot let one DATA payload stand in for
another. These are refused without any normalizer delivery:

- a dispatch with nothing fetched (`DISPATCH_WITHOUT_FETCH`)
- a dispatch for another record (`DISPATCH_MISMATCH`)
- a reentrant dispatch, or a dispatch during an arrival
- an arrival while another arrival or a dispatch is in progress (`ARRIVAL_REENTERED`). One guard covers
  the whole arrival, including snapshotting and direct routes, so a nested call from a contract getter or
  from the clock during dispatch is refused
- an arrival after closure (`ARRIVAL_AFTER_CLOSE`)

A fetched record left undispatched stays outstanding, so cleanup cannot pass. The router has no public
`dispatch` or queue, and it cannot be serialized.

**Retention.** Only exact `int` (at most 128 bits), `float`, `None`, `str` (at most 4096 characters) and
`Decimal` are retained. A finite `Decimal` may have at most 40 coefficient digits and an exponent of
magnitude at most 40. Both are read from `as_tuple()` without expanding the value, and accepted digits count
toward the byte budget. A position's contract fields are snapshot into a frozen value, so a later
mutation changes nothing. Anything else is retained as a sentinel the normalizer rejects, so the malformed
fault survives. Text arguments of errors and the tick attribute are never retained.

**Bounds:**
- at most 256 queued records and 64 KiB of estimated retained data, refused as `QUEUE_OVERFLOW` before
  admission
- at most 64 faults, with the rest counted
- one registration per request, at distinct ids
- one positions window, which never reopens

## Routing

| Callback | Route |
|---|---|
| `contractDetails`, `contractDetailsEnd` | Contract normalizer, **directly, outside lifecycle accounting**. Contract details have no ledger slot and never borrow the quote slot |
| `marketDataType`, `tick*`, `tickSnapshotEnd` | Quote snapshot slot, through the FIFO |
| `accountSummary`, `accountSummaryEnd` | Account-summary slot, through the FIFO |
| `position`, `positionEnd` | The single positions window, through the FIFO |
| `error_1052` for the quote or contract id | That stream's `error_1052` (quote through the FIFO) |
| `error_1052` for the account id | A fixed `unsupported_callback("error")` plus a lifecycle ERROR. Account and position streams have no `error_1052` |
| `error_1052` with id -1 | Every stream, directly. Only a well-formed call (4 or 5 arguments, the exact integer id -1, an exact integer time and an exact non-negative integer code; the same fields the contract and quote normalizers require) with an OK code is exempt. A non-OK code (`SESSION_ERROR`) or a malformed call (`SESSION_ERROR_MALFORMED`) fails every stream, account and positions included |
| `error_1052` for another valid id | The contract stream, as a stray with its original id (`UNKNOWN_REQUEST_ID`) |
| `error_1052` with an id that is not a valid request id | Latches both `error_1052` streams (`UNKNOWN_REQUEST_ID`) |
| A quote or account callback for another id | Its own stream, as a stray with its original id, outside lifecycle accounting (`UNKNOWN_REQUEST_ID`) |
| `unsupported_callback(name, reqId)` | Always an `UNSUPPORTED_CALLBACK` router fault, so completion fails even when no stream has registered yet. Also routed to its owner, or to every registered stream when the id is not registered. Excluded protobuf variants use this same fixed refusal. The name is never retained |
| `connectAck()`, `connectionClosed()` | The adapter's direct-callback record (`CONNECT_ACK`, `CONNECTION_CLOSED`), under the same arrival guard. Accepted during the connect and disconnect hooks; refused after closure or when nested |

**Stop window.** While the adapter's stop hook runs, an `error_1052` arriving synchronously inside it is also reported to the adapter as a direct `ERROR`, which settles that attempt `UNKNOWN` (`ERROR_DURING_STOP`). This applies only to an error for the stopped read's own request, or to a session error that is not an exempt well-formed OK code. Positions have no request id, so only session errors apply to them. Errors for another request and informational session codes do not taint the stop. The normal routing above is unchanged, and errors outside a stop hook are never reported as direct errors.

Nothing is dropped silently or relabelled. Evidence of duplicates, post-END input, missing currencies and
oversized quantities is preserved by the unchanged normalizers. No version is inferred: a legacy-order
error sent as `error_1052` is indistinguishable and is not detected.

## Limits

- **Local and single-threaded.** Arrival is supported from one thread, and admission and retention are
  indivisible only in that model. Concurrent arrival is not supported.
- **No secrecy.** Encapsulation guards against ordinary misuse, not hostile code in the same process.
- **Contract details** are outside lifecycle accounting, so local cleanup covers only the three lifecycle
  reads, never all streams.
- **Direct callbacks are observations only.** `connectAck` and `connectionClosed` never establish socket
  closure, session validity or broker acknowledgement. The socket is observed separately at shutdown.
- **Not connected.** It is exercised only with invented doubles. Connected use, SDK import and real-use
  admission remain separately gated.
