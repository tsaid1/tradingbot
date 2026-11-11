# Managed-accounts input (INT-2, offline, NON_AUTHORITATIVE)

`src/tradingbot/paper_readiness/managed_accounts_input.py` adds `ManagedAccountsInput`. It is constructed
explicitly for one exact `CallbackRouter` and receives only the converted `managedAccounts` callback. From
one bounded snapshot it builds the existing `AccountOwner`, then registers the router's account-summary
and positions reads in owner mode ([CALLBACK_ROUTER.md](CALLBACK_ROUTER.md), INT-1), once.

It is exercised only with invented identities. **It is not a runner, does not admit a real account or
session, and proves nothing about a source, a connection, PAPER or consent.** An `EXACT` status is a
comparison.

## Wiring

```python
router = CallbackRouter(client, lock, clock); router.start()
facade = ReadCallbackFacade(router)                 # every other callback, unchanged
accounts = ManagedAccountsInput(router, expected_identifier, per_run_key)
# explicit wiring: managedAccounts goes to `accounts`, never through the facade
accounts.managedAccounts(accounts_list)
accounts.register_reads(summary_request_id, positions_snapshot_id)
# ... callbacks through the facade, consume, router.shutdown(...)
accounts.close()                                    # after shutdown
accounts.report()                                   # fixed codes only
```

`ReadCallbackFacade.managedAccounts` is unchanged: it still refuses. Nothing replaces it automatically,
and there is no callback registry.

## Inputs

- **Construction:** `router` must be an exact `CallbackRouter`; nothing else is called. `expected` must be
  an exact `str` of 1 to 64 UTF-8 bytes, and `key` exact `bytes` of 16 to 64 bytes. These are checked
  before anything is retained. A violation raises with a fixed message and no value.
- **The callback:** exactly one positional exact `str` of at most 1040 characters (16 entries of 64 bytes
  plus separators). It is split on `,` exactly, with no trimming, case folding or deduplication. Entry
  count (at most 16) and entry validity (at most 64 UTF-8 bytes, no control characters) are judged by
  `AccountOwner`. The raw text is not retained.

## Outcomes

| `arrival` | When |
|---|---|
| `AWAITING` | No arrival yet |
| `MALFORMED` | Keywords, wrong arity, not an exact `str`, longer than 1040, empty, or an empty segment (including a trailing separator) |
| `EXACT`, `MISMATCH`, `AMBIGUOUS`, `REFUSED` | `AccountOwner`'s comparison of the parsed snapshot |

`ABSENT` is not reachable through this parser, because an empty list is `MALFORMED`. Whether the current
API sends a trailing separator is unverified; if it does, a real snapshot would be refused.

The first arrival's outcome is **sticky**: a malformed first arrival cannot later recover.

| `invalidation` (first one latched, never replaced) | When |
|---|---|
| `DUPLICATE` | A second `managedAccounts` arrival |
| `LATE` | An arrival after a normal `close()` |
| `SECOND_REGISTRATION` | `register_reads` called again |
| `REGISTRATION_INCOMPLETE` | The router's report did not show both reads newly open without a new fault |
| `REENTERED` | A call while another call of this component is running |

Every invalidating event closes the component's own owner, so undelivered rows can no longer be
projected (END is withheld). Once registration has been attempted, it also calls the router's public
`unsupported_callback("managedAccounts")`, a persistent router fault, even after END or router
completion. Records already produced stay as historical evidence. Before registration, an invalidation
only prohibits registration; the router is not touched.

## Registration

`register_reads` runs once. Without a current `EXACT` match it is `NOT_READY` and calls nothing. Otherwise
it calls `register_account_summary(..., owner=)` and `register_positions(..., owner=)`. Both return
`None`, so the result is read from the router's public report before and after:

| `registration` | Evidence |
|---|---|
| `REGISTERED` | Both slots newly open and no new router fault |
| `PARTIAL` | One slot newly open. It is kept: there is no rollback |
| `FAILED` | No slot newly open |

`PARTIAL` and `FAILED` invalidate. There is no retry and no fallback to no-owner registration.

## Completion and lifetime

`report().completed` is true only when the arrival is `EXACT`, nothing has invalidated, the registration
is `REGISTERED`, **and** the router's own report is `completed`. Neither alone is enough.

The caller keeps the component open through consumer delivery and shutdown, then calls `close()`. A normal
close closes only the owner this component created, and is not an invalidation. An arrival after it is
`LATE`, which is.

## Limits

- **One owning execution context.** Arrival, registration and close must be serialized; reentry is
  refused. Nothing here is thread-safe.
- **Invented identities only.** Admitting a real identifier, a real managed-accounts list, a session or a
  connection remains separately gated, as do read stops and PAPER-READ-1.
- **No secrecy.** Encapsulation guards against ordinary misuse, not hostile code in the same process. The
  component refuses copying and serialization, and its reports and repr carry fixed codes only.
