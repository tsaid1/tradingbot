# Offline read-retirement accounting (NON_AUTHORITATIVE)

`src/tradingbot/paper_readiness/read_retirement.py` is a pure, bounded event model of read retirement for
one local connection. It is offline bookkeeping only.

- **No external effects:** it calls no SDK, client, socket or thread, runs no callable and sends nothing.
- **Values only:** its methods take fixed enum values only, and reports carry no account, position or price
  values.
- **What it proves:** at best, `local_quiescence_established`.
- **What it does not prove:**
  - broker-side retirement, which is `NOT_ESTABLISHED_BY_INSPECTED_SOURCES`
  - PAPER-READ-1 acceptance, which is always `NOT_ASSESSED`
  - any trading permission (`trading_permission: false`)
- **E3 is not resolved by this module**, and PAPER-READ-1 acceptance is unchanged. Any narrowing of
  "retirement proved" needs explicit owner acceptance through the established process.

It is not wired into the runner or `ReadOnlyTransport`. Those interfaces, the FAKE-only guard and
`FORBIDDEN_NAME_RE` are unchanged, and connected integration is separately gated.

## What the inspected official sources establish

The pages describe themselves as deprecated:
- [Account summary](https://interactivebrokers.github.io/tws-api/account_summary.html)
- [positions](https://interactivebrokers.github.io/tws-api/positions.html)
- [market data request](https://interactivebrokers.github.io/tws-api/md_request.html)
- [connection](https://interactivebrokers.github.io/tws-api/connection.html)
- [EClient](https://interactivebrokers.github.io/tws-api/classIBApi_1_1EClient.html)

| Read | Documented behaviour | Consequence here |
|---|---|---|
| Account summary | "creates a subscription"; changed values "every three minutes" | A returned retirement attempt is required after its END |
| Positions | `reqPositions()` takes no request id; updates continue after `positionEnd` | Requestless, so one positions read per connection, never reused |
| Quote snapshot | `tickSnapshotEnd` about 11 seconds later; not every field is guaranteed | END marks only that snapshot's end. Without END the read is incomplete. A retirement attempt is modelled intent only; early snapshot cancellation semantics are not established and nothing here says it is supported or authorized |
| Connection | Python's reader starts automatically, and callback processing runs separately in the client loop | A stopped reader alone cannot show that dispatch or the queue is quiet |

No cancellation acknowledgement is established by the inspected sources. That is the reason for
`NOT_ESTABLISHED_BY_INSPECTED_SOURCES`. It is not a claim that an acknowledgement is impossible.

## Model

| Event | Rule |
|---|---|
| `open_read(slot)` | Once per slot per connection. A second positions read is `POSITIONS_REUSED`; any other repeat is `READ_REOPENED` |
| `callback_received` → `callback_started` → `callback_finished` | Queued, then in flight (one consumer), then applied. After `close_admission` a received callback is a fault and is not queued |
| Applying a callback | After END it is `POST_END_EVENT`. After the read's retirement attempt is reserved it is `LATE_CALLBACK`. Faults are recorded, never silently excluded |
| `reserve_attempt` → `settle_attempt` | Reserved before any outcome, allowed from a requested read with or without END (for example after an error). It settles once with `RETURNED`, `FAILED` or `UNKNOWN`. It is never retried and never replaced |
| `reserve_disconnect` → `settle_disconnect` | Independent of read attempts, so a failed attempt never blocks it. Reserving a read attempt after it is a fault |
| `close_admission` | Terminal dispatch exclusion. Already queued callbacks may still drain |
| `component_stopped(CONSUMER / READER / SOCKET, outcome)` | Each is reported separately, once. `FAILED` and `UNKNOWN` are kept. A dispatch start or finish after the consumer is reported `STOPPED` is `DISPATCH_AFTER_CONSUMER_STOPPED`, and a receipt after the reader is reported `STOPPED` is `RECEIPT_AFTER_READER_STOPPED` |
| `seal()` | The settlement bound. Unsettled attempts and unreported components become `UNKNOWN`. Later events are `EVENT_AFTER_SEAL` |

`RETURNED` means only that a local call returned. A timeout or silence is never success.

`local_quiescence_established` is true only when all of these hold:
- the ledger is sealed, with no fault
- every opened read has its END and no error
- account summary and positions each have a `RETURNED` attempt, and any other reserved attempt (a snapshot
  attempt after its END) is also `RETURNED`; a snapshot with END and no reserved attempt needs none
- admission is closed, with nothing queued or in flight
- the consumer, reader and socket are each `STOPPED`
- the disconnect attempt is `RETURNED`

Events are capped at 4096 and recorded faults at 64. Overflow is counted and blocks quiescence.

## Tests

`tests/test_read_retirement.py` drives event sequences with invented values.

**Valid local control:** quiescence is established, and the broker side stays `NOT_ESTABLISHED`.

**Known E3 failures:**
- a reader stopped alone, and each missing component
- dispatch of a queued or in-flight callback after the consumer is reported stopped, and a receipt after
  the reader is reported stopped
- callbacks still queued or in flight at seal
- a callback after admission closes
- a failed attempt that is retained and not retried, with disconnect still independent
- an unsettled attempt that becomes `UNKNOWN` and can't settle later
- an outcome reported before its reservation
- a failed or unknown disconnect or socket
- an attempt after disconnect
- retirement from REQUESTED after an error, with the read still incomplete
- late and post-END callbacks
- positions reuse
- a snapshot without END, and a snapshot END alone
- a reserved snapshot attempt after END that stays unsettled, or settles `UNKNOWN` or `FAILED`

**Surface and bounds:** event bounds, fixed report fields, and a module surface with no forbidden names
or I/O imports.

## Limits

- This is bookkeeping over reported events. It cannot observe a real reader, consumer, socket or broker.
- An honest caller is assumed. A caller that reports `RETURNED` or `STOPPED` falsely is not detected.
- Only the three reads above are modelled. Contract details has no retirement step here.
- Python thread, queue and disconnect termination semantics are not established by the inspected sources.
  A connected bridge would need its own review.
