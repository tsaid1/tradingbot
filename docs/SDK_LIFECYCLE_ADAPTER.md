# SDK-free lifecycle adapter (NON_AUTHORITATIVE)

`src/tradingbot/paper_readiness/sdk_lifecycle.py` adds `LifecycleAdapter`, a single-use adapter. It drives
the merged `RetirementLedger` ([READ_ONLY_RETIREMENT.md](READ_ONLY_RETIREMENT.md)) from observations of
one local connection, and it doesn't duplicate the ledger.

The boundary is fixed and mock-only, and today it is implemented only by invented test doubles. The
adapter imports no SDK, opens no socket and starts no thread. **Real SDK integration is a later,
separately reviewed task.** Nothing here claims SDK compatibility, broker-side retirement
(`NOT_ESTABLISHED_BY_INSPECTED_SOURCES`), PAPER-READ-1 acceptance (`NOT_ASSESSED`) or trading permission.
The runner, `ReadOnlyTransport`, the FAKE-only guard and `FORBIDDEN_NAME_RE` are unchanged.

## Boundary

| Boundary item | Use |
|---|---|
| `connect()` | Returns `connection`, `reader` and `queue`. The adapter retains all three immediately |
| `stop_read(slot)` | One modelled retirement attempt for an open read |
| `disconnect()` | Local disconnect |
| `dispatch(slot, kind)` | The synchronous callback for one consumed item |
| `connection.is_connected()`, `connection.socket_closed()` | Connection state before a stop, and socket cessation |
| `reader.join(timeout)`, `reader.is_alive()`, `reader.partial_bytes()` | Reader cessation and unread bytes it holds (`None` if unknown) |
| `queue.get_nowait()`, `queue.size()` | The adapter-owned consumer's input |

The adapter takes its lock from the caller, a context manager. The package admits only pure
standard-library imports, so it cannot create a thread lock itself. The lock is never held while a hook
or observation runs. Every such call counts as pending until it returns.

The reader reports each enqueue through `on_enqueued`. Callbacks outside the consumer flow
(`CONNECT_ACK`, `CONNECTION_CLOSED`, `ERROR`, `READ`) arrive through `on_direct_callback`.

## Shutdown, once

1. **Reads.** Each open account-summary or positions read, and a snapshot without END, gets its attempt
   reserved in the ledger first. Then the adapter checks the connection.
   - **Disconnected:** the hook is not called, and the attempt is `UNKNOWN`.
   - **Connected:** `stop_read` runs. Its result is recorded:
     - `RETURNED` means only that the call returned.
     - A raise is `FAILED`.
     - An `ERROR` direct callback during the hook makes it `UNKNOWN`.
2. **Disconnect.** This runs independently of step 1's results.
3. **Admission.** Admission closes. Later enqueues are ledger faults.
4. **Drain.** The adapter-owned consumer drains the queue, up to 1024 items. A raising `dispatch` stops
   the consumer and leaves its callback in flight. An item the ledger never admitted is not dispatched.
5. **Observe.** Each resource is observed through the retained references, even if the boundary has
   since reset its own:
   - the reader, joined with a timeout that bounds only that wait
   - the socket
   - the consumer, which must not be busy and must see an empty queue
6. **Seal.** The ledger is sealed.

`completed` is true only when every one of these holds:

- the adapter is closed with no pending call
- there is no adapter failure
- the reader is not alive and holds zero partial bytes
- the socket is observed closed
- the consumer is idle on an empty queue
- the ledger reports local quiescence

**These never count as completion:** a `CONNECTION_CLOSED` callback, a returned stop, an empty queue or a
disconnected state. Failures are fixed codes. Exception text is never kept.

Single use means a second `start` or `shutdown` is refused, there are no reads after shutdown, and there
is no retry or recovery. The ledger refuses a second positions read on the connection.

## Tests

`tests/test_sdk_lifecycle.py` uses invented doubles throughout.

- **Returning control:** it completes only after the reader, socket and consumer are observed stopped,
  with the lock never held during a hook.
- **Paired hazards:**
  - a blocked stop hook, which stays pending and resumes to completion, run on a real thread
  - raising stop, disconnect, dispatch and connect hooks
  - a stop while disconnected
  - a direct read callback, and an error during a stop
  - an item enqueued without admission
  - a late enqueue after admission closes, and data after END
  - a snapshot without END
  - references erased by a reset, with the reader alive and with it stopped
  - unresolved and unknown partial bytes
  - single use and positions reuse

## Limits

- **No hard guarantee:** there is no total-time or termination guarantee. Synchronous hooks can block
  indefinitely. A blocked call stays pending, and the adapter stays incomplete.
- **Honest doubles assumed:** the adapter observes only what the boundary reports. A real reader's
  framing and buffer completeness are not established, and partial-byte reporting is part of the
  boundary contract, not proved.
- **Coverage:** only account summary, positions and the quote snapshot are modelled. Snapshot retirement
  is intent only.
- **Concurrency:** a real threaded integration needs a real lock and its own review.
