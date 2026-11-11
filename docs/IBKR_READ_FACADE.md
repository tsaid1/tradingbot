# Read-callback facade (SDK-free, NON_AUTHORITATIVE)

`src/tradingbot/ibkr_read_facade.py` adds `ReadCallbackFacade`. It is a plain Python object over one accepted
`CallbackRouter` ([CALLBACK_ROUTER.md](CALLBACK_ROUTER.md)) for converted callbacks of the existing read-only
streams:
- contract details
- the quote snapshot
- the account summary
- the positions snapshot
- the two direct notifications

It extends the contract-only facade ([IBKR_CONTRACT_FACADE.md](IBKR_CONTRACT_FACADE.md)) by **composition**.
Contract details and the 1052 `error` go through the accepted `ContractCallbackFacade` unchanged.

It imports no SDK, subclasses nothing, opens no socket and starts no thread. **It is not a complete callback
interface, an SDK wrapper or a connected adapter, and it establishes no SDK compatibility.** Binding a real
wrapper, decoder or protobuf variant to it is a later, separately approved step.

## Use

```python
router = CallbackRouter(client, lock, clock)
router.start()
router.register_quote(quote_id, con_id)  # registration stays with the router
facade = ReadCallbackFacade(router)      # an exact CallbackRouter only
facade.tickPrice(quote_id, tick_type, price, attrib)
facade.tickSnapshotEnd(quote_id)
while router.consume_one():              # delivery stays with the router's consumer
    pass
router.shutdown(join_timeout)
```

## Surface

Every forwarded call reaches the same-named router entry exactly once, with its arguments unchanged.

| Facade call | Forwarded to |
|---|---|
| `contractDetails(reqId, contractDetails)`, `contractDetailsEnd(reqId)` | The accepted `ContractCallbackFacade`, then the router |
| `error(reqId, errorTime, errorCode, errorString[, advancedOrderRejectJson])` | The accepted `ContractCallbackFacade`, then `router.error_1052(*args)`, positional only |
| `marketDataType(reqId, marketDataType)` | `router.marketDataType` |
| `tickPrice(reqId, tickType, price, attrib)` | `router.tickPrice`, which drops `attrib` |
| `tickSize`, `tickString`, `tickGeneric(reqId, tickType, value)` | The same-named router entries |
| `tickSnapshotEnd(reqId)` | `router.tickSnapshotEnd` |
| `accountSummary(reqId, account, tag, value, currency)`, `accountSummaryEnd(reqId)` | The same-named router entries |
| `position(account, contract, pos, avgCost)`, `positionEnd()` | The same-named router entries |
| `connectAck()`, `connectionClosed()` | The same-named router entries |
| `managedAccounts(accountsList)` | Always `router.unsupported_callback("managedAccounts")`; the value is never read |

**Refused forms.** These become `router.unsupported_callback(<the entry's fixed name>)`:
- any keyword argument
- any argument count other than the documented one (`error`: any keyword)

The router records a persistent `UNSUPPORTED_CALLBACK` fault that blocks completion, even before any stream
registers, and every registered stream refuses it too. No request id or value is taken from a refused call, a
refused entry is never also called, and nothing raises.

There are no other methods: no `__getattr__`, reflection, dynamic dispatch, superclass or logging. An
uncovered name, such as `nextValidId` or a protobuf variant, is not intercepted. It is simply absent, and
how a future binding treats it is undecided here. **Protobuf callbacks are never treated as converted
callbacks.**

## What the router and normalizers still decide

The facade validates nothing itself. All of these stay exactly as the accepted router and normalizers define
them:
- value retention and contract snapshots
- malformed-value refusal (never coerced)
- request-id attribution and foreign-id strays
- END and post-END handling
- the 1052 arity, type, session and informational-code rules
- account errors as fixed refusals
- direct-notification refusals when nested in an arrival or after closure

## Ownership

- The facade holds only its router and the composed contract facade. It retains no callback or payload.
- It never touches the router's queue, consumer, registration, shutdown or clock.
- Contract details stay **outside lifecycle accounting**, and positions are **one local snapshot** whose id
  is local, never a broker request id.
- `connectAck` and `connectionClosed` are adapter observations only: never broker acknowledgement, session
  validity or socket closure.
- The router's single-threaded model applies unchanged.

## Verification

`tests/test_ibkr_read_facade.py` composes the actual facade, router and normalizers. It compares every case
with the same calls made directly on a second router, and the four-stream control also with normalizers fed
directly. Covered:
- each stream alone
- malformed values per stream
- foreign ids and post-END input
- quote, account, session and foreign 1052 errors
- every entry's extra, missing and keyword forms
- refusals before registration
- `managedAccounts`
- notifications from hooks, nested in an arrival, and after closure
- the fixed surface
- an import probe that loads no SDK module

## Limits

- **Not an SDK binding.** No SDK object, wrapper class, decoder or client is imported, constructed or read.
  Whether real SDK objects and calls fit these shapes is unestablished, and needs separate approval and
  exact-pin evidence. Passing tests here prove no SDK compatibility.
- **Fixed coverage.** Only the callbacks listed above are covered. Unverified auxiliary callbacks, such as
  `nextValidId`, have no entry here. A future binding needs a reviewed decision to refuse or support each
  one.
- **Not connected.** It is exercised only with invented objects and invented client doubles, on one thread.
- `completed` is local only: never PAPER-READ-1, broker-side retirement or trading permission.
