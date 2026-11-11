# Contract-details callback facade (SDK-free, NON_AUTHORITATIVE)

`src/tradingbot/ibkr_contract_facade.py` adds `ContractCallbackFacade`. It is a deliberately narrow, plain
Python object over one accepted `CallbackRouter` ([CALLBACK_ROUTER.md](CALLBACK_ROUTER.md)). It exists for
one unit: an invented contract-details object, through the router, to its END, with the same envelopes and
reports as calling the router directly.

It imports no SDK, subclasses nothing, opens no socket and starts no thread. **It is not a complete
callback interface or a connected adapter, and it establishes no SDK compatibility.** Binding any real
SDK object or wrapper to it is a later, separately approved step.

## Use

```python
router = CallbackRouter(client, lock, clock)
router.start()
router.register_contract(request_id)
facade = ContractCallbackFacade(router)  # an exact CallbackRouter only
facade.contractDetails(request_id, details)
facade.contractDetailsEnd(request_id)
facade.error(request_id, error_time, error_code, error_string)  # the SDK 1052 order
```

## Surface

| Facade call | Forwarded to | Refused form |
|---|---|---|
| `contractDetails(reqId, contractDetails)` | `router.contractDetails`, unchanged | Any keyword, or not exactly two positional arguments |
| `contractDetailsEnd(reqId)` | `router.contractDetailsEnd`, unchanged | Any keyword, or not exactly one positional argument |
| `error(reqId, errorTime, errorCode, errorString[, advancedOrderRejectJson])` | `router.error_1052(*args)`, unchanged, with `errorTime` kept before `errorCode` | Any keyword argument |

Each forwarded call is made exactly once. A refused form becomes `router.unsupported_callback(<fixed
name>)`. The router records that as a persistent `UNSUPPORTED_CALLBACK` fault that blocks completion, even
before the contract stream registers, and every registered stream refuses it too. No request id is taken
from a malformed call, and nothing raises.

There are no other methods: no `__getattr__`, reflection or dynamic dispatch, no superclass, and no
protobuf or other callback surface.

## What the router and normalizer still decide

The facade validates nothing itself. All of these stay exactly as the accepted router and contract
normalizer define them:
- the fixed attribute snapshot, and refusal of malformed rows (a missing or mistyped field is refused,
  never coerced)
- request-id attribution: a foreign id is a recorded stray (`UNKNOWN_REQUEST_ID`) with its own id, and a
  mistyped id is refused by the normalizer
- END handling, including repeated END and details after END
- the 1052 arity and type rules, session (-1) OK-code exemption, session and malformed session faults,
  and unknown-id errors

`error` is always the 1052 order. A legacy-order call cannot be detected and is not guessed at. The text
and JSON arguments are never read, logged or retained.

## Ownership

- The facade holds only its router reference. It retains no callback or payload, and never touches the
  router's queue, consumer, registration or shutdown.
- Contract details stay **outside lifecycle accounting**. They take no ledger slot, so contract details
  alone never establish local quiescence or router completion. A run completes only through at least one
  lifecycle read, which the facade does not provide.
- The router's single-threaded model applies unchanged.

## Limits

- **Not an SDK binding.** No SDK object, wrapper class, decoder or client is imported, constructed or
  read. Whether real SDK objects and calls fit these shapes is unestablished, and needs separate
  approval and exact-pin evidence. Passing tests here prove no SDK compatibility.
- **Three callbacks only.** It covers converted contract-details callbacks and the 1052 error. Every other
  callback, including protobuf variants, is outside it.
- **Not connected.** It is exercised only with invented objects and the router's invented client double.
- `completed` is local only: never PAPER-READ-1, broker-side retirement or trading permission.
