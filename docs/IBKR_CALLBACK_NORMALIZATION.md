# IBKR callback normalization (SDK-free, NON-AUTHORITATIVE)

`src/tradingbot/paper_readiness/ibkr_callbacks.py` turns the primitive values of documented TWS API
`EWrapper` callbacks for one contract-details or quote-snapshot request into the existing
`ResponseEnvelope` records. The unchanged `correlate` and `assess_*` diagnostics judge them, and the
unchanged FAKE-only runner can replay them through `FakeReadOnlyTransport`.

It imports no SDK, opens no socket, makes no request and has no order, cancel, modification, account or
credential method. It was exercised only with invented callback values. **No compatibility with an
installed SDK, a TWS build or IB Gateway is claimed.** Connected checks stay blocked as described in
[PAPER_READINESS.md](PAPER_READINESS.md).

## Use

```python
contract = IbkrCallbackNormalizer(1001, CallbackRequest.CONTRACT_DETAILS)
quote = IbkrCallbackNormalizer(1002, CallbackRequest.QUOTE_SNAPSHOT, con_id=<observed conId>, clock=<clock>)
# Route each callback that arrives while the request is outstanding to the method of the same name,
# and any other callback to unsupported_callback(name, reqId).
envelopes = contract.envelopes()   # ready for correlate(), or for FakeScript.contract_details
```

`envelopes()` can be read at any time and does not change state. `completed`, `issues`,
`issue_overflow`, `ignored_callbacks` and `session_messages` explain the result.

## Official interface mapping

Signatures come from the legacy reference
https://interactivebrokers.github.io/tws-api/interfaceIBApi_1_1EWrapper.html, which describes itself as
deprecated and points to IBKR Campus. The current pages (https://www.interactivebrokers.com/docs/tws-api/doc/,
reached from https://ibkrcampus.com/campus/ibkr-api-page/twsapi-doc/) returned **HTTP 403 to automated
fetching** on 2026-11-10 and were not verified.

| Callback (documented signature) | Normalized as |
|---|---|
| `contractDetails(int reqId, ContractDetails contractDetails)` | One `CONTRACT_DETAILS` envelope. `contract.conId`, `symbol`, `localSymbol`, `secType`, `currency`, `exchange`, `primaryExchange`, plus `longName`, `tradingHours`, `liquidHours`, `timeZoneId` and `minTick`. `0` and `""` are unset defaults and become missing |
| `contractDetailsEnd(int reqId)` | The `CONTRACT_DETAILS_END` envelope, emitted only when `completed` |
| `marketDataType(int reqId, int marketDataType)` | A `MARKET_DATA_TYPE` envelope: 1 live, 2 frozen, 3 delayed, 4 delayed frozen, anything else unknown |
| `tickPrice(int tickerId, int field, double price, TickAttrib attribs)` | Tick 1 bid, 2 ask, 4 last. `attribs` is accepted and not interpreted |
| `tickSize(int tickerId, int field, decimal size)` | Tick 0 bid size, 3 ask size. An `int` (older APIs) or a whole, non-negative `Decimal` |
| `tickString(int tickerId, int field, string value)` | Tick 45, "Time of the last trade (in UNIX time)", becomes `last_timestamp` (UTC) |
| `tickGeneric(int tickerId, int field, double value)` | Tick 49, halted: -1, 0, 1 or 2 |
| `tickSnapshotEnd(int tickerId)` | Stamps `received_at` from the injected clock and emits `TICK_SNAPSHOT_END`, only when `completed` |
| `error(int id, int errorCode, string errorMsg, string advancedOrderRejectJson)` | An `ERROR` envelope with the code only. Id -1 is session-level and recorded in `session_messages`. Neither text argument is read or retained |

Tick ids and their delivery callbacks are from https://interactivebrokers.github.io/tws-api/tick_types.html.
`DOCUMENTED_TICK_CALLBACKS` holds the whole documented table (99 ids):

- **Populates a field:** 0, 1, 2, 3, 4, 45 and 49.
- **Recorded only:** other documented live types (for example 5, 6, 7, 9, 14 and 57) go into
  `tick_types_received`.
- **Delayed, caught by the existing check:** 66, 67, 68 and 88 are recorded, so they reach
  `QUOTE_DELAYED`. They are never mapped onto live fields.
- **Other delayed and frozen types withhold completion.** The unchanged existing delayed set does not
  contain them, so recording them would let a live control pass. These are 69 and 70 (delayed bid and ask
  size), 71–76 and 80–83 (other "Delayed ..." types) and 97 ("ETF Nav Frozen Last").
- **Other cases withhold completion too:** an undocumented id, and a documented id delivered through a
  different callback (including option-computation and EFP ids).

Snapshot behaviour is from https://interactivebrokers.github.io/tws-api/md_request.html: the snapshot is
followed by `tickSnapshotEnd`, "values may not be returned for all tick types", and "no generic ticks can
be specified". Market data types are from https://interactivebrokers.github.io/tws-api/market_data_type.html.
The remaining official references, and their limits, are in [PAPER_READINESS.md](PAPER_READINESS.md).

## Rules

- **Request ids are kept, never rewritten.** A callback for another id becomes an envelope with that id.
  A callback foreign to this request, or one arriving after the END marker, becomes an ordinary envelope
  too. `correlate` then reports `UNCORRELATED_RESPONSE_IGNORED`, `UNEXPECTED_RESPONSE_KIND` or
  `RESPONSE_AFTER_END_IGNORED` as it already does.
- **Held until explicit completion.** Ticks for this request form one pending aggregate. Before
  `tickSnapshotEnd` it is still returned, but with no receipt time and no END envelope. Nothing is discarded.
- **No END while incomplete.** While `completed` is false, `envelopes()` contains no END for this request,
  including the END of a repeated `contractDetailsEnd` or `tickSnapshotEnd` callback. Once complete, a
  repeated END stays visible and `correlate` reports it as `RESPONSE_AFTER_END_IGNORED`.
- **Conflicts are never resolved.** A tick type repeated with another value starts a second aggregate
  (`QUOTE_OBSERVATIONS_MULTIPLE`). Several contract rows, identical or not, stay several rows
  (`CONTRACT_IDENTITY_AMBIGUOUS`).
- **Invalid but well-typed values are carried.** A non-finite, zero, negative or crossed price, or an
  unknown market data type, reaches the existing integrity, live and delayed checks, which reject it.
- **Unrepresentable input withholds completion.** Any of these is recorded in `issues` and withholds
  the END envelope, so the existing END-missing classification makes the stream INCOMPLETE. There is
  no fallback:
  - a wrong type
  - an out-of-range tick type
  - a misrouted tick
  - an unparseable timestamp or halt value
  - an unsupported callback
  - a session message outside the documented OK codes (2104, 2106, 2158, 1102)
  - an unusable clock
  - more than 256 callbacks
- **Bounded.** 256 callbacks, 64 distinct tick types, 32 issues and 32 session messages. The existing
  correlation bound of 64 envelopes per request still applies.
- **Nothing is inferred.** No environment, account, read-only or connection status is derived from a
  callback, a market data type or a port. The quote's `con_id` is the conId the request was made for.
  IBKR ticks carry only the request id, so the tie to the contract is by request id alone.

## Requirement coverage

`tests/test_ibkr_callbacks.py`:

| Requirement | Result |
|---|---|
| Documented contract and live snapshot sequences | `CONTROL_COMPLETE` through the unchanged FAKE runner, with every control code. The same input yields byte-identical output |
| Ambiguous or conflicting contract identity | `CONTRACT_IDENTITY_AMBIGUOUS`, the mismatch codes or `CONTRACT_FIELDS_MISSING`, and no quote is requested |
| Wrong request id | `UNCORRELATED_RESPONSE_IGNORED`. Another id's END never completes this request |
| Missing END marker | `CONTRACT_DETAILS_END_MISSING`, or `TICK_SNAPSHOT_END_MISSING` with `QUOTE_RECEIPT_UNTIMED`. Ticks are preserved |
| Malformed or unsupported tick payload | An issue, END withheld, `TICK_SNAPSHOT_END_MISSING`, `NOT_READY`. A repeated END cannot complete it |
| Delayed or frozen data | `QUOTE_DELAYED`, `QUOTE_FROZEN`, `MARKET_DATA_TYPE_UNKNOWN` or `MARKET_DATA_TYPE_CHANGED`. Any other delayed or frozen tick withholds completion. Never `QUOTE_LIVE_CONTROL` |
| Missing or stale timestamps | `QUOTE_UNTIMED`, `QUOTE_STALE` or `QUOTE_TIMESTAMP_FUTURE` |
| Unknown callbacks | Recorded by a validated name, bounded, and they withhold completion |

## Limits

- Exercised only with invented values in plain Python objects shaped like the documented ones. Real SDK
  types, versions and callback ordering are not verified.
- Newer API versions may add `error` arguments (for example an error time). Only the legacy four-argument
  form is implemented, and a bridge for another form must route it explicitly. The `error(Exception)` and
  `error(string)` overloads are API-internal, so route them to `unsupported_callback`.
- `TickAttrib` fields, including pre-open, are not interpreted.
- A real snapshot can legitimately update a tick within its window. Here that is reported as ambiguous
  rather than resolved.
- Callbacks outside the request window (for example connection-time `nextValidId`) must not be routed to
  a normalizer. Any routed unsupported callback withholds completion.
- `FakeReadOnlyTransport` stamps `received_at` on any quote without one. An incomplete normalized
  snapshot still has no END marker there, so it remains INCOMPLETE.
- The adapter is not connected to any runner path beyond the existing FAKE transport. A connected
  transport remains out of scope and needs the owner prerequisites in PAPER_READINESS.md.
