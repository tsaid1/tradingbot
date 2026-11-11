# Decode handoff (SDK-free, offline, NON_AUTHORITATIVE)

`src/tradingbot/paper_readiness/decode_handoff.py` adds `DecodeHandoff`. It connects owned frame output to
synchronous decoding and to a router's real consumer settlement, keeping each frame owned until that
settlement is proven.

It composes accepted pieces, all unchanged:
- an internally owned `FrameAccumulator` ([READ_FRAMING.md](READ_FRAMING.md))
- one exact `CallbackRouter` ([CALLBACK_ROUTER.md](CALLBACK_ROUTER.md)), reached through the accepted
  `ReadCallbackFacade` ([IBKR_READ_FACADE.md](IBKR_READ_FACADE.md))
- a closed invented decoder, `InventedDecoder`

It imports no SDK, parses no wire format, opens no socket and starts no thread. **It is not a decoder, a
reader or a runner, and it proves no SDK compatibility.** Numeric message-id and protobuf selection, and
any real SDK decoder, stay absent.

## Use

```python
router = CallbackRouter(client, lock, clock); router.start(); router.register_...(...)
decoder = InventedDecoder(server_version, {payload: (Step(StepKind.CALL, Entry.TICK_PRICE, args), ...)})
handoff = DecodeHandoff(router, decoder, server_version=server_version)
handoff.feed(chunk)          # opaque bytes, the accumulator's own bounds
handoff.process_available()  # one frame at a time, until none is queued or one does not settle
handoff.finish()
router.shutdown(join_timeout)  # the caller's, through the router's public seam
handoff.report()
```

## The invented decoder

`InventedDecoder(server_version, scripts)` maps whole payloads (`bytes`, at most 4096) to fixed scripts:
at most 256 scripts of at most 128 steps. A step is one of:

- `CALL` with a fixed `Entry`, a router entry reached through the facade (contract details, quote ticks
  and snapshot end, account summary, positions, the 1052-order `error`, or `UNSUPPORTED`)
- `RAISE`, the decoder raising at that point
- `RETAIN_SINK`, the decoder keeping the frame's sink so it can call it after returning
- `CLEAR_VERSION`, the decoder's version being cleared, as a reset would

An unknown payload returns without any callback. There is no parsing, no field splitting, no dynamic
method name and no user callable.

## One frame at a time

`process_one` takes the next steps in order:

1. **Refuse foreign work.** Queued or fetched router work that did not come from this handoff is
   `FOREIGN_QUEUED_WORK`. Nothing is taken.
2. **Reserve, then take.** Ownership is reserved before `take()`, keeping the frame's sequence and length.
3. **Check version provenance.** The decoder's current version must equal the version pinned at
   construction; otherwise `VERSION_MISMATCH`, and the frame is not decoded.
4. **Decode synchronously** into a fresh frame-scoped sink. The sink forwards each callback to one fixed
   facade entry and is closed when decoding returns.
5. **Check delivery.** A return with no callback, a decoder exception (also after callbacks), or any new
   router fault or adapter failure fails the frame.
6. **Settle.** Every record decoding admitted is dispatched with the router's own `consume_one`. Afterwards
   the router must show no queued record, no outstanding fetch, nothing in flight, and no new fault or
   adapter failure. Delivery is never inferred from a callback count or an empty queue alone.
7. **Release** the frame as `SETTLED`.

Contract details are routed directly by the router and admit no ledger record. Reaching a normalizer is not
stream completion: a settled handoff means its callbacks were dispatched, while whether a stream completed
is the normalizers' and router's verdict.

## Failures

The first failure latches, and the failing frame stays **held** (`held_sequence`). Processing then stops:
later frames stay in the accumulator, which reports them at finish. The failure persists after END and
after router completion.

| `failure` | Cause |
|---|---|
| `RETURNED_WITHOUT_DELIVERY` | Decoding returned with no callback |
| `DECODER_RAISED` | The decoder raised, before or after callbacks; admitted records are not settled |
| `VERSION_MISMATCH` | The decoder's version differs from the pinned provenance |
| `ROUTER_REFUSED` | A new router fault or adapter failure, such as a call form the facade refuses |
| `UNSUPPORTED_CALLBACK` | An `UNSUPPORTED` callback, such as an unconverted raw callback |
| `REFUSED_CALLBACK` | A sink call that is not an `Entry` |
| `CALLBACK_OVERFLOW` | More than 64 callbacks for one frame |
| `SETTLEMENT_INCOMPLETE` | Admitted records were not all dispatched, or work remained |
| `FOREIGN_QUEUED_WORK` | Router work not from this handoff was pending |
| `TAKE_FAILED` | The accumulator had a queued frame but `take()` returned none |
| `LATE_CALLBACK` | A sink called after its frame's decoding returned |
| `REENTERED` | A call while another call of this handoff ran |

Unsupported, refused and late callbacks also reach the router as its persistent `unsupported_callback`
refusal, so its own completion fails too. After a failure, the frame's remaining callbacks are not
forwarded.

## Completion

`report().completed` is true only with all of these:
- clean, finished framing (`COMPLETE`)
- every handoff `SETTLED`, none held and no record dropped
- no failure
- the router's own `completed`

None alone is enough.

## Limits

- **One owning execution context.** Nothing is thread-safe. Reentry is refused.
- **Invented only.** No real decoder, wire selection, SDK object, socket or connection. Real SDK binding
  and reader ownership remain separately gated.
- **Local only.** `completed` is never broker-side retirement, PAPER-READ-1 or trading permission.
- **Payload-free.** Reports, reprs and errors carry fixed codes and counts only; the handoff cannot be
  serialized.
