# IBKR read-only adapter (`tradingbot.ibkr`)

A thin adapter over the **official** Interactive Brokers TWS API for Python (`ibapi`,
`EClient`/`EWrapper`/`EReader`). This phase is **read-only and paper-only**. The adapter checks the
local paper environment, connects, checks the account identity, verifies
that the API is in read-only mode, then resolves a contract, reads the account summary and
positions, and samples a quote.

| Module | Imports `ibapi`? | Purpose |
|---|---|---|
| `config.py` | no | `PaperConnectionConfig`, loaded fail-closed from a private TOML file |
| `environment.py` | no | local preflight before any socket opens: TWS profile mode and listener owner |
| `identity.py` | no | identity-gate decision (managed accounts must be exactly the configured one) |
| `models.py` | no | result dataclasses, tick-type map, quote-quality assessment |
| `errors.py`, `redact.py` | no | typed exceptions, error-code classification, account masking |
| `check.py` | no (lazy) | step runner, verdict and report for the check script |
| `report_view.py` | no | load, validate and redact the latest report for the dashboard page |
| `discovery.py` | no (lazy) | query file, batch runner, catalog and table for contract discovery |
| `stream_check.py` | no (lazy) | held-quote-stream check behind `scripts/ibkr_quote_stream_check.py` |
| `session.py` | **yes** | `IbkrSession` over a deny-by-default `_ReadOnlyClient(EClient)` |

## 1. Install the SDK (not on PyPI, never committed)

The SDK is IBKR-licensed. Keep its source outside every git tree and install it non-editably:

```sh
uv pip install --python ~/.venvs/tradingbot/bin/python "<workspace>/private/vendor/twsapi-1052.1/pythonclient"
~/.venvs/tradingbot/bin/python -c "import ibapi; print(ibapi.__version__)"   # 1052.1
```

GitHub CI has no SDK, so the SDK tests skip there. `TRADINGBOT_BLOCK_IBAPI=1 python -m pytest`
reproduces that condition locally.

## 2. Private config

The default path is `~/.config/tradingbot/ibkr/paper.toml`. Use mode `0600` for the file and `0700`
for the directory. The file must be owned by you, must be a regular file and must not be a symlink.
Every key is required and none has a default:

```toml
mode = "paper"                                # must be exactly "paper"
host = "127.0.0.1"                            # loopback IP literal only (127.0.0.1 or ::1)
port = 7497                                   # allowlist: 7497 (paper TWS) or 4002 (paper Gateway)
client_id = 17                                # 1..2^31-1; 0 refused (TWS's manual-order client)
account = "DU0000000"                         # the one allowlisted paper account (placeholder)
tws_profile_ini = "/Users/<user>/Jts/jts.ini" # jts.ini of the TWS profile the owner starts
tws_os_user = "<user>"                        # macOS user that runs that TWS and owns that file
read_only_check = "config-read"               # the only legal value (see section 4)
```

Fill `tws_profile_ini` and `tws_os_user` in for each run. The code has no default for either. For
example, if TWS runs as macOS user `tariq`, use `/Users/tariq/Jts/jts.ini` and `tariq`.

Each config problem raises its own error type: `ConfigNotFound`, `ConfigIsSymlink`,
`ConfigNotRegularFile`, `ConfigWrongOwner`, `ConfigPermissionsTooOpen`, `ConfigInvalid`,
`NonLoopbackHost`, `PortNotAllowed`, `NonPaperAccount` or `ModeNotPaper`.

## 3. TWS settings the owner must set

- Log in to **Paper Trading** as `tws_os_user`.
- Global Configuration → API → Settings:
  - **Enable ActiveX and Socket Clients**: ON
  - **Read-Only API**: ON
  - **Allow connections from localhost only**: ON
  - **Socket port** equal to `port`
  - Master API client ID different from `client_id`
- No other client may use `client_id`. A live login for the same user can block paper market data
  (error 10197).
- TWS binds the API port on the wildcard address (`*.7497`) whatever the localhost-only setting
  is, so the bind address is not evidence either way.

## 4. Safety model: paper identity needs several independent checks to agree

The `connect()` order is: preflight → handshake → server version → identity gate → read-only check
→ READY. Any failure disconnects and raises. No data request is sent before READY.

1. **Profile mode (before any socket opens).** `tws_profile_ini` must be a regular file, must not
   be a symlink, and must be owned by `tws_os_user`. Its `[Logon]` section must say exactly
   `tradingMode=p`. If the file is missing or unreadable, the key is missing, the file cannot be
   parsed (duplicate keys included), or the value is anything other than `p`, the result is
   `PaperModeNotConfirmed`. No other line of the file is ever echoed.
2. **Listener owner (before any socket opens).** Every TCP socket that is LISTENing on `port` must
   belong to a process owned by `tws_os_user`. Otherwise the result is `ListenerNotConfirmed`, with
   reason `NO_LISTENER` or `WRONG_USER`. If netstat or ps cannot attribute the listener at all, the
   result is the distinct `ListenerOwnerUndeterminable`.
   - The check uses `netstat -anv -p tcp`, whose `process:pid` column shows the PID after the last
     colon, and `ps -o user= -p PID`.
   - On this macOS (26.6), an unprivileged user can see the PIDs of sockets owned by other users.
     This was verified on a TCP listener owned by `tradingbotpaper`, seen from `tariq`, and on UDP
     sockets owned by `root`, `_mdnsresponder` and `_netbios`.
   - Both preflight checks are point-in-time. Steps 3 to 5 still apply after them.
3. **Server version ≥ 219** (`MIN_SERVER_VERSION`, the lowest version the tests exercise; they also
   run at 226, the maximum for SDK 1052.1). A lower version raises `UnsupportedServerVersion`. The
   SDK has already sent the handshake and START_API at that point, but no data request.
4. **Identity gate.** The gate waits for `nextValidId` and `managedAccounts`.
   - **Every** `managedAccounts` value, from the first one on, must be exactly the configured
     account. A wrong value fails the gate permanently, including a live account followed by the
     paper account.
   - The value is compared again under the lock just before READY.
   - After READY, a wrong `managedAccounts` value or a foreign summary or position row poisons and
     disconnects the session.
   - The `D` prefix in config is defence in depth only. The expected account always comes from the
     config.
5. **Read-only API.** `read_only_check` has exactly one legal value, `"config-read"`. Any other
   value is a config error raised before any socket opens. The adapter fails closed whenever
   read-only cannot be verified.
   - After the gate and before READY, the session sends `reqConfigProtoBuf(ConfigRequest(reqId))`. The answer arrives
     on `EWrapper.configResponseProtoBuf(ConfigResponse)`.
     - Field path: `ConfigResponse.api` (`ApiConfig`) `.settings` (`ApiSettingsConfig`) with the
       fields `.readOnlyApi` (bool), `.socketPort` (int32) and `.allowLocalhostOnly` (bool).
     - These are proto3 `optional` fields with explicit presence, so `HasField` tells "unset" apart
       from `false`. An unset field is treated as absent, never as a value.
     - The check requires `readOnlyApi` present and True, `socketPort` present and equal to
       `port`, and `allowLocalhostOnly` present and True.
     - Each refusal has its own type (all subclass `ReadOnlyNotVerified`) and is followed by a
       clean disconnect:

       | Refusal | Error |
       |---|---|
       | server version below 219 | `ConfigReadUnsupported` |
       | no response in time | `ConfigReadTimeout` |
       | error callback for this request | `ConfigReadRejected` |
       | response `reqId` missing or different from the one sent | `ConfigReadUncorrelated` |
       | a field absent or wrong | `ReadOnlySettingRefused` |
       | a later config response (same or other `reqId`) whose `api` section differs from the first | `ConfigResponseChanged`; poisons the session |

       An identical repeat is ignored.
     - `updateConfigProtoBuf` is never used, and the deny-by-default client makes it unreachable.

Other protections:

- **Deny by default.** The session talks to TWS only through `_ReadOnlyClient(EClient)`.
  - Every public `EClient` method not in `ALLOWED_CLIENT_METHODS` raises `ReadOnlyViolation`. That
    covers orders, cancels of orders, global cancel, option exercise, `updateConfigProtoBuf`, FA
    replace, display groups, and any method a future SDK adds.
  - Outgoing message ids are filtered at the lowest shared choke point. `_ReadOnlyClient.conn`
    always wraps the SDK `Connection` in `_GuardedConnection`, and every SDK send path ends in
    `self.conn.sendMsg(frame)`: bound or unbound, base or subclass, text or protobuf. The guard
    allows the single initial `API\0` handshake. After that it passes only well-formed frames
    whose message id is in `ALLOWED_OUT_MESSAGES`. A test runs the reviewer's probe,
    `EClient.sendMsgProtoBuf(session._client, PLACE_ORDER/UPDATE_CONFIG, ...)`, and checks that
    nothing reaches the wire.
  - `ReadOnlyViolation` is a `BaseException`, so the SDK's `except Exception` send wrappers cannot
    swallow it.
  - An SDK test walks every public callable of `EClient`. Another test tries `placeOrder`,
    `cancelOrder`, `reqGlobalCancel` and `updateConfigProtoBuf` on every attribute of a connected
    session, two levels deep, and also as unbound base-class calls. Every attempt is refused and
    nothing reaches the transport. An AST test forbids those names anywhere in the package.
  - **No attribute or weak-reference path to an unfiltered send.** `_GuardedConnection` has
    `__slots__` without `__weakref__` (so it cannot be weak-referenced at all), exposes
    exactly the five methods the SDK uses (`connect`, `disconnect`, `isConnected`, `recvMsg`,
    `sendMsg`) and does not forward other attributes, so `conn.socket` and `conn.lock` no longer
    exist. It holds no reference to the SDK `Connection`. That object lives only in the
    module-private `session._RAW_CONNECTIONS`, a plain dict keyed by an opaque int token that
    the guard's `__del__` removes, and `_raw_call` never binds it to a local. An exception raised inside the transport leaves the
    guard with its traceback, context and cause cut, so a handed-out exception's frames cannot lead
    back to it either.
  - A permanent test (`test_object_graph_of_connected_session_reaches_no_unfiltered_send`) walks
    the object graph of a connected session, plus an exception the session raised, breadth-first
    to depth 16 with cycle protection. It follows container items, `__dict__` entries, `__slots__`,
    every non-dunder attribute and property, and `__self__`, `__func__`, `__closure__` cells,
    `__wrapped__`, `__traceback__`/`tb_next`/`tb_frame`/`f_locals`, `__context__`, `__cause__`,
    weak references and their `__callback__`, `weakref.getweakrefs(obj)` for every object, and
    the `__defaults__`, `__kwdefaults__` and `__closure__` of every function. It asserts that no fake connection or `socket.socket` is
    reachable. It then calls every reachable callable named like a send or write with
    PLACE_ORDER and UPDATE_CONFIG, in each argument shape the SDK's send primitives take, and
    asserts that not one byte reached the fake wire and that the session still reads. The merge
    review's probe (`weakref.getweakrefs(conn)[0].__callback__.__defaults__[0]()`) is a permanent
    test too.
    - It deliberately does not follow classes, modules, `__globals__`, `f_globals`, builtins or the
      caller's own stack (`f_back`), and it skips stdio streams and code objects.
  - **What remains, and is out of scope.** The guarantee is: no route by attribute or
    weak-reference traversal from any object the package hands out. Code that deliberately uses
    `gc`, module globals, imports the private module by name, uses `ctypes` or opens its own socket
    is out of scope for an in-process filter. The control for that class of risk during
    diagnostics is TWS's own Read-Only API setting, which the adapter verifies before any data
    request.
- **Logging.** The `ibapi` logger is disabled, has `propagate = False` and a `NullHandler`. Without
  this, the SDK's field-count-mismatch path would log raw fields, account included, to stderr.
  Exception text is masked by `redact_accounts`. The report never contains the full account ID.
- **Errors and cleanup.**
  - Errors are routed by `reqId`. Farm and status codes are recorded as notices. Connection-loss
    codes fail outstanding requests at once. Quote entitlement codes give `UNAVAILABLE`.
  - Account summary and positions subscriptions are always cancelled, and an unended request is
    cancelled.
  - A failed cancel raises `CleanupError`. It is never reported as success.
  - The poisoned and connection state is re-checked under the lock immediately before every send.
  - `disconnect()` joins the handshake, `EClient.run` and `EReader` threads within a bound.
- **Quotes.** `quote()` makes up to two attempts, each with `reqMarketDataType(3)` first:
  1. One streaming sample (`reqMktData`, `snapshot=False`) for a bounded window (`stream_seconds`,
     8 s by default), always followed by `cancelMktData`. It ends early (`ended_early`) once bid,
     ask, last and the market-data type have all arrived and `stream_settle_seconds` (1 s) have
     passed with no new tick type.
  2. Only if the caller asks (`snapshot_fallback=True`; the script's
     `--quote-snapshot-fallback`) and the stream delivered no valid two-sided quote: one snapshot
     `reqMktData`, waiting up to `quote_timeout` (20 s).

  Why this order: in the first real paper run (type 3 requested) the snapshot delivered nothing
  at all in 20 s, with no tick, no market-data-type callback, no error and no snapshot end. The
  stream delivered a complete DELAYED two-sided quote (delayed bid, ask, last, sizes, last
  timestamp, open, high, low, close, volume; type 3; first tick after about 5.3 s), and
  `tickReqParams` reported `snapshotPermissions` 4.

  Nothing collected is ever discarded. A snapshot that does not end is returned with what arrived
  and flagged `SNAPSHOT_END_MISSING`. Each attempt records:
  - tick counts by tick-type name;
  - every `marketDataType` value delivered;
  - `tickReqParams` (including `snapshotPermissions`);
  - every error and notice carrying its reqId;
  - the first and last tick times, the window (`window_ms`), the time waited and `ended_early`;
  - `quote_age_seconds`: receipt time minus the exchange last-trade timestamp (tick 45/88), when
    TWS delivered one; otherwise `None` and the flag `QUOTE_AGE_UNKNOWN`. It dates the last trade
    only and includes any local clock offset. Nothing infers bid/ask freshness from it.

  The SDK decodes delayed tick types (66–76, 88) exactly like live ones, and the adapter maps
  delayed bid, ask, last, their sizes, close and the last timestamp. The verdict is:
  - `PASS`: a LIVE two-sided quote, by either route (see section 8 for what LIVE requires);
  - `PASS_WITH_WARNINGS`: only delayed or frozen two-sided data, or only partial data (the report
    says which);
  - `FAIL`: nothing at all arrived on either route.

  Further rules:
  - `LIVE` needs evidence: TWS reports type 1, or (type not reported) both bid and ask moved
    after the initial image. The full rule is the table in section 8.
  - `received_at` is the only time available for bid/ask. `last_trade_time` dates the last trade
    only.
- **Recovery.** Sessions are single-use. A new `IbkrSession` re-runs every check.
- **Orders (later).** Order code belongs in a separate, separately reviewed module. It must not be
  added to `session.py`.

## 5. Run the connected check (only with the owner's approval and paper TWS running)

```sh
cd <worktree>
PYTHONPATH="$PWD/src" ~/.venvs/tradingbot/bin/python scripts/ibkr_paper_check.py \
    [--config ~/.config/tradingbot/ibkr/paper.toml] [--report-dir <workspace>/private/ibkr_reports] \
    [--quote-snapshot-fallback]
```

The check runs these steps in order:

1. Preflight and connect: profile, listener, version, identity gate and read-only check.
2. Resolve TLT (STK/SMART/USD, primary NASDAQ).
3. Account summary, then positions, then the TLT quote (a streaming sample; a snapshot as a second
   attempt only with `--quote-snapshot-fallback`).
4. Disconnect, then reconnect once and disconnect again (recovery proof, with reader-thread join
   verified each time).

It writes `ibkr_paper_check_<UTC>.json` (file 0600, directory 0700) and prints a summary.

The report contains the masked account, the SDK version, the server version and connection time
reported by TWS, the preflight facts, the read-only status and fields, and per-step status with
timings. The verdict is one of:

- `PASS`: every step and cleanup passed, read-only was verified by the config read, and the quote
  step was PASS. The summary line always prints `read_only_check=...`.
- `PASS_WITH_WARNINGS`: everything passed, but the quote was delayed, frozen or partial.
- `FAIL`: anything else. This includes a quote step where nothing arrived on either route; its
  detail still carries both attempts' evidence. When no market data was delivered, the summary
  line includes `no market data delivered`.

The exit code is 0 for `PASS` and `PASS_WITH_WARNINGS`, otherwise 1.

The default report directory is `$TRADINGBOT_IBKR_REPORT_DIR`. If that is unset, it is
`private/ibkr_reports` in the parent or grandparent directory of the checkout.

## 6. Dashboard page: latest report (read-only)

`dashboard/pages/7_IBKR_Paper_Check.py` shows the newest `ibkr_paper_check_*.json` in the report
directory. The directory default is the script's (`tradingbot.ibkr.check.default_report_dir`:
`$TRADINGBOT_IBKR_REPORT_DIR`, else `private/ibkr_reports` in the workspace), and a sidebar text
input overrides it.

```sh
PYTHONPATH=src streamlit run dashboard/app.py        # then open "IBKR Paper Check"
PYTHONPATH=src python -m pytest tests/test_ibkr_paper_check_page.py
```

- It shows the verdict, the run time and the report's age, the pre-socket checks, the Read-Only
  API status and the three settings TWS reported, the masked account, the SDK and server versions,
  the TLT contract, the account summary, positions, and the quote. For the quote it shows the data
  type, quality, flags, last-trade time and age, and each attempt's evidence. It also shows every
  step with its status and duration, and the warnings.
- A red stale banner appears when the report finished longer ago than the sidebar threshold (60
  minutes by default).
- The page shows a file only if it is valid JSON of schema `tradingbot.ibkr.paper_check/1` with
  the structure the script writes (`tradingbot.ibkr.report_view.validate_report`). Anything else,
  including NaN, a naive timestamp, an unknown verdict or step status, or invalid UTF-8, gets one
  fixed message and nothing from the file.
- Every string, dict keys included, passes through `redact_accounts` before rendering. The
  redaction (`redact_accounts`, shared by the session, the report and the page) masks every
  account-shaped substring whatever adjoins it (`aDU1234567`, `DU1234567DU7654321`). It
  deliberately over-redacts some harmless tokens, such as `T090000` in a UTC file stamp.
- The page imports no SDK and no session, opens no socket, writes nothing, and has no button or
  other control that connects to the broker. Its tests use invented reports in a temporary
  directory.

## 7. Contract discovery (read-only)

`IbkrSession.discover_contracts(query, *, limit=50, timeout=None)` sends one `reqContractDetails`
(outgoing id `REQ_CONTRACT_DATA` = 9, already allowlisted) and returns **every** matching contract,
unlike `resolve_contract`, which insists on exactly one.

- `ContractQuery` gained optional fields (all default to unset): `last_trade_date_or_contract_month`
  (`YYYYMM` or `YYYYMMDD`), `multiplier`, `trading_class`, `local_symbol`, `strike`, `right`,
  `include_expired` (default false). A FUT query with no month returns every unexpired month.
- Each row is a `DiscoveredContract` taken from the 1052.1 `Contract` and `ContractDetails`
  objects: conId, symbol, local symbol, secType, exchange, primary exchange, currency, multiplier,
  `minTick`, `priceMagnifier`, trading class, `lastTradeDateOrContractMonth` (date part; the SDK
  splits a time part into `lastTradeTime`), `lastTradeDate`, `contractMonth`,
  `realExpirationDate`, strike, right, `underConId`, `underSymbol`, `underSecType`, `longName`,
  `marketName`, `stockType`, `timeZoneId`, `tradingHours`, `liquidHours`, `validExchanges`,
  `orderTypes`, `marketRuleIds`, and the size rules `minSize`, `sizeIncrement`,
  `suggestedSizeIncrement` (`None` when TWS leaves them unset). `query_match` says whether secType
  and currency equal the query's. Rows are sorted by last trade date or month, strike, right,
  conId.
- Results: `FOUND`; `NOT_FOUND` (error 200, or an empty end) and `ERROR` (any other request-scoped
  broker error) carry the broker's code and message and are **not** exceptions. If more than
  `limit` rows arrive (1..1000), the wait ends, `cancelContractData` is sent and the result is
  `truncated`. Identity, poison, connection, cleanup and timeout failures raise, exactly as for
  every other request (same `_request` path: READY and poison re-checked under the lock before the
  send, the same cleanup rules).

`IbkrSession.option_chain_params(underlying, *, fut_fop_exchange="", limit=100)` sends
`reqSecDefOptParams` (outgoing id `REQ_SEC_DEF_OPT_PARAMS` = 78, protobuf path from server version
212) and returns one `OptionChainParams` per exchange / trading class: exchange, underlying conId,
trading class, multiplier, sorted expirations and strikes (`securityDefinitionOptionParameter`,
then `securityDefinitionOptionParameterEnd`). It is a pure security-definition read: it takes only
the underlying's symbol, secType and conId, touches no account, order or setting, and the SDK
defines no cancel for it (so a timeout raises and nothing is cancelled). It is the only new id on
`ALLOWED_OUT_MESSAGES`; `reqSecDefOptParams` and `reqSecDefOptParamsProtoBuf` are the only new
entries on `ALLOWED_CLIENT_METHODS`. Every deny-by-default test (all other EClient methods, all
other ids, unbound base-class sends, object-graph walk) still runs over the larger allowlist.

### Run it (only with the owner's approval and paper TWS running)

```sh
cd <worktree>
PYTHONPATH="$PWD/src" ~/.venvs/tradingbot/bin/python scripts/ibkr_contract_discovery.py \
    [--config ~/.config/tradingbot/ibkr/paper.toml] [--report-dir <workspace>/private/ibkr_reports] \
    [--queries config/contract_discovery.toml] [--pace 1.0] [--limit 50]
```

- The query file (`config/contract_discovery.toml`, schema
  `tradingbot.ibkr.contract_discovery_queries/1`) is validated strictly **before** the private
  config is read or any socket opens; unknown keys or wrong types exit 2.
- The script loads the private config exactly as the check script does; preflight, identity gate
  and read-only verification all apply. It runs the queries one at a time with a fixed pause
  (`pace_seconds`, default 1 s) before every request but the first, then disconnects.
- `[[option_chain]]` entries first discover the underlying (it must match exactly one contract,
  otherwise the row is `NOT_FOUND` or `AMBIGUOUS_UNDERLYING` and no option request is sent).
- It writes `contract_catalog_<UTC>.json` (schema `tradingbot.ibkr.contract_catalog/1`, file 0600,
  directory 0700, masked account only) to the check script's report directory, and prints a table:
  query, status, count, and for futures the nearest three unexpired months (by UTC date) with
  conId, multiplier and minimum tick.
- `NOT_FOUND`, `ERROR` and `TIMEOUT` rows do not stop the batch. A connection, identity, poison,
  read-only, cleanup or redaction failure stops it (remaining rows `SKIPPED`), makes the catalog
  `FAILED` and the exit code 1. Otherwise the catalog is `COMPLETE` and the exit code 0.
- Bond contracts (`BOND`) arrive on a different callback and are not handled; bond ETFs are `STK`.

## 8. Held quote streams, the live / delayed / frozen rule, and entry freshness

The approved automatic-entry rule needs "live streaming only" quotes whose bid and ask are each no
older than 3 seconds. A bounded sample with one receipt time cannot show that, so the session can
hold a stream open and records evidence per side.

### Stream API (`IbkrSession`, read-only, no new outgoing message)

```python
stream = session.open_quote_stream(resolved_contract, market_data_type=3)  # -> QuoteStream
snap = stream.latest()        # QuoteSnapshot, route "held_stream"; any time, any thread
final = stream.close()        # cancelMktData; returns the final snapshot (idempotent)
session.quote_streams()       # the open streams
```

- It sends exactly what a bounded sample sends: `reqMarketDataType(type)`, `reqMktData(...,
  snapshot=False)`, and later `cancelMktData`. `ALLOWED_CLIENT_METHODS` and
  `ALLOWED_OUT_MESSAGES` are unchanged (a test asserts both sets exactly), and the object-graph
  test also runs with open and closed streams.
- `latest()` builds the snapshot under the session lock, the same lock every tick callback takes,
  so a snapshot never mixes two states (a deterministic test checks both that the lock is held
  while building and an invariant over thousands of concurrent ticks).
- Bounds: at most `max_quote_streams` (class attribute, default 4, hard cap `MAX_QUOTE_STREAMS` =
  10) open at once; another open raises `QuoteStreamLimit` and sends nothing.
- Every stream is cancelled on `close()`, on `disconnect()` (before the transport closes) and on
  session poison (after the poison is set, before the transport closes). On connection loss TWS
  drops the subscriptions and no cancel can be sent; each stream is closed
  (`CONNECTION_LOST`) and recorded in `cleanup_failures`, as for any request. A failed cancel is
  recorded and `close()` raises `CleanupError` (with the final snapshot as `result`). A closed
  stream's `latest()` raises `QuoteStreamClosed`; `stream.final` keeps the last snapshot and
  `stream.closed_reason` says why (`CLOSED`, `SESSION_DISCONNECTED`, `SESSION_POISONED`,
  `CONNECTION_LOST`, `OPEN_FAILED`).
- A request error for the stream's reqId, a connectivity interruption (1100/2110) or an internal
  callback error fails the stream: its label is `UNAVAILABLE` / `STREAM_FAILED` from then on, even
  after a 1102 "data maintained"; it must be closed and reopened, and until it is closed it still
  counts against `max_quote_streams`.
- **The market-data type is session-wide.** The SDK's `reqMarketDataType` carries no reqId, so the
  latest call applies to the whole session. Rule: one requested type per session at a time. While
  any stream is open, opening a stream, a bounded sample or a snapshot with another type raises
  `QuoteStreamConflict` before anything is sent, and nothing is cancelled either (nothing was
  requested). With no stream open, behaviour is as before. On a session that can place orders
  (`PaperOrderSession` subclasses `IbkrSession`), hold streams only with type 3, or not at all,
  so its own quote requests never conflict.
- Notices: an entitlement notice that TWS sends with reqId -1 (session-wide) is recorded in
  `session.notices` only and is **not** attributed to any stream. Whether TWS sends 2186 / 10090 /
  10167 with the request's reqId is for the stream check to confirm.

### Per-side evidence (additive `QuoteSnapshot` fields, safe defaults)

The counting lives in `models.SideEvidence` (pure, tested without the SDK):

- A **valid** price is finite and > 0; a valid size is finite and >= 0. TWS's "no quote yet"
  placeholders (price -1 or 0, a negative size), NaN and infinity are recorded as evidence (flags
  `BID_PLACEHOLDER_TICK`, `BID_NON_FINITE_TICK`, and the `ASK_` pair) and never count as image or
  change. An unset value (`UNSET_DOUBLE` / `UNSET_DECIMAL`) carries no information at all.
- **Initial image** of a side: its first valid price. Its size part is the first valid size at or
  after that price; a size alone never establishes the image, and sizes before it are ignored. The
  delayed or live tick id of every tick of the side is recorded either way (`*_tick_class`).
- **Change** (post-image update): a valid price or size of that side that differs from the
  previous valid value of the same kind. An identical repeat is not a change, a size-only change
  is. (The SDK turns each bid/ask price message into a price callback plus a size callback.)
- **Freshness**: `bid_at` / `ask_at` (UTC, ms) and `bid_at_monotonic` / `ask_at_monotonic` move
  **only** when the image is established or a value changes. An identical repeat never refreshes
  them. `bid_last_tick_monotonic` / `ask_last_tick_monotonic` record the latest tick of any kind,
  for diagnostics only, and are never used for freshness.
- `bid_updates` / `ask_updates`: changes since the image. `quality_reason`: the reason code.
  `observed_at_monotonic`: when the snapshot was built. `halted`: the latest HALTED tick (49)
  value (0 = not halted).

Reports written before these fields existed still load (defaults `None`, `0`, `""`) and can never
pass the entry check. The monotonic stamps mean nothing in another process.

### The classification (`models.classify_quote`, pure; first matching row wins)

Evaluated on every `latest()`, at the end of a bounded sample and for a snapshot, with the
requested type, the per-side change counts, each side's change age at that moment and every type
delivered so far.

| # | Observed facts | Quality | Reason |
|---|---|---|---|
| 1 | entitlement refusal for the request (354, 10089, 10168, 10186, 10197) | UNAVAILABLE | `NOT_ENTITLED` |
| 2 | the held stream failed (request error, connectivity, internal error) | UNAVAILABLE | `STREAM_FAILED` |
| 3 | no finite positive bid, ask or last | UNAVAILABLE | `NO_PRICE` |
| 4 | latest reported type 3 | DELAYED | `REPORTED_DELAYED` |
| 5 | latest reported type 4 | FROZEN (as before) | `REPORTED_DELAYED_FROZEN` |
| 6 | latest reported type 2 | FROZEN | `REPORTED_FROZEN` |
| 7 | any delayed tick id (66–76, 88, 90) on the subscription | DELAYED | `DELAYED_TICKS` |
| 8 | any reported type other than 1 in the history (2, 3, 4 or unknown, e.g. 7; sticky) | DELAYED (3/4 seen) or FROZEN | `REPORTED_TYPE_HISTORY_NOT_LIVE` |
| 9 | latest reported type outside 1..4 | FROZEN | `UNKNOWN_MARKET_DATA_TYPE` |
| 10 | reported type 1 but no live tick id | FROZEN | `REPORTED_LIVE_NO_LIVE_TICKS` |
| 11 | reported type 1 with live tick ids (the initial image counts) | LIVE\* | `REPORTED_LIVE` |
| 12 | type NOT reported, requested type not 1 or 3 | FROZEN | `TYPE_UNREPORTED_REQUESTED_NOT_ELIGIBLE` |
| 13 | type NOT reported, fewer than `UNREPORTED_MIN_CHANGES` (2) changes on a side | FROZEN | `TYPE_UNREPORTED_NO_UPDATES_YET` |
| 14 | type NOT reported, no PRICE change on a side since its image | FROZEN | `TYPE_UNREPORTED_NO_PRICE_CHANGE_YET` |
| 15 | type NOT reported, a side's latest change older than `UNREPORTED_LIVE_WINDOW_SECONDS` (5.0 s; or unknown or in the future) | FROZEN | `TYPE_UNREPORTED_UPDATES_TOO_OLD` |
| 16 | type NOT reported, a side's latest PRICE change older than the window (or unknown or in the future) | FROZEN | `TYPE_UNREPORTED_PRICE_CHANGE_TOO_OLD` |
| 17 | type NOT reported, otherwise | LIVE\* | `TYPE_UNREPORTED_UPDATES_SEEN` |

\* An entitlement notice for the request (`ENTITLEMENT_NOTICE_CODES`: 2186, 10090, 10167) turns a
would-be LIVE into DELAYED / `ENTITLEMENT_NOTICE`.

- **Bad news is sticky per subscription.** Notices, delayed tick ids and every reported type
  accumulate for the subscription. Once a delayed id, a reported 2/3/4 or an entitlement notice has
  been seen, the subscription can never be LIVE again (reported 1 → 3 → 1 stays DELAYED). Close it
  and open a new one. The same holds for any reported value other than 1, including an unknown
  one: 1 → 7 → 1 stays FROZEN.
- **A 10090 notice fails the stream.** 10090 ("part of requested market data is not subscribed")
  is an error for the request in `classify_error`, not a farm notice, so the stream becomes
  `UNAVAILABLE` / `STREAM_FAILED`, which is stricter than the DELAYED the notice rule would give.
- **Validity flags** are separate from the label: `MISSING_BID/ASK`, `NON_POSITIVE_BID/ASK`,
  `NON_FINITE_BID/ASK/LAST`, `CROSSED` (bid > ask) and `LOCKED` (bid = ask). A non-finite bid, ask
  or last makes `two_sided_valid` false. `HALTED` is flagged and does not change the label; any
  non-zero HALTED value counts as halted, including -1 ("status not available").
- `assess_quote` keeps its signature and is the same function with no update counts, notices or
  requested type, so for it an unreported type is never LIVE.

**Why an unreported type needs recent changes on both sides.** TWS does not always send
`marketDataType` for real-time data: on 2026-11-12 a spot-gold stream delivered 31 bid and 35 ask
updates in 12 s with plain tick ids and no type, and was labelled FROZEN, while an index CFD in
the same run reported type 1. A frozen feed, however, also uses plain tick ids. It delivers one
fresh-looking image at subscription time, sometimes after placeholders, and may re-send it. So
"it arrived just now" proves nothing, and neither does one old change. Rows 12 to 15 require:
- a request that could deliver live data (type 1 or 3);
- at least two genuine value changes on each side since its first valid value, at least one of
  them a PRICE change (a size flicker 100 → 200 → 100 under unchanged prices is not enough);
- each side's latest change AND its latest price change within the last 5 seconds at the moment
  of the snapshot.

A size change still moves a side's freshness time on every path. A price that is not a multiple
of the contract's minimum tick (`ResolvedContract.min_tick`, which every stream and sample
carries) is flagged `BID_OFF_TICK_TICK` / `ASK_OFF_TICK_TICK` and is neither image nor change.
Prices on a coarser increment are multiples of the minimum tick, so they still count. When the
minimum tick is unknown (missing, zero or not finite), the stream does no tick check, and the
gateway's own off-tick refusal applies.
A side that carries either flag is never usable for an automatic entry (`entry_quote_check` reasons
`BID_OFF_TICK` / `ASK_OFF_TICK`), on the reported and the unreported path alike, until the stream is
reopened: the off-tick price may be the one the snapshot shows.

**Accepted consequence:** a thin or quiet market that is genuinely live but reports no type may
never qualify, and an entry on it HOLDs. US stocks on this account answer 2186 with delayed ids
(type 3), and futures answer 354 for a live request and 10167 with delayed data for a delayed
request. Rows 1, 4, 7 and the notice rule keep them DELAYED or UNAVAILABLE.

**Changes in existing output.** Compared with `main`:
- A bounded sample or snapshot with no reported type, requested type 1 or 3, and two recent
  changes on each side (one of them a recent price change) is now `LIVE` (it was `FROZEN`), and `assess_quote_attempts` counts it as
  a LIVE two-sided quote (`PASS`).
- Every other unreported quote stays `FROZEN`, now with one of five distinct reasons.
- Flags gained `LOCKED`, `NON_FINITE_*`, the placeholder, non-finite and off-tick tick flags,
  `HALTED`,
  `EARLIER_TYPE_NOT_LIVE` and, only when it changes the label, `ENTITLEMENT_NOTICE`.

### Entry freshness (`models.entry_quote_check`, pure; not wired into execution)

`entry_quote_check(snapshot, now_monotonic=..., max_age_seconds=3.0) -> EntryQuoteCheck(usable,
reasons, bid_age_seconds, ask_age_seconds)`. Usable only if the label is `LIVE`; bid and ask are
present, finite and positive and bid < ask; no HALTED tick with a non-zero value; each side's
tick class is `LIVE`; and each side's freshness time (its image or its latest change, never a mere
repeat) is no older than the limit by the monotonic clock (an age equal to the limit passes) and
not later than `now`. So even with reported type 1, a side that has not changed for longer than
the limit is `*_STALE`. A quote that is LIVE with no reported type is re-checked at the check's
own `now`: if either side's latest change or latest price change is then older than
`UNREPORTED_LIVE_WINDOW_SECONDS`, the reason is `UNREPORTED_EVIDENCE_EXPIRED`, whatever
`max_age_seconds` the caller passed. Every failing condition is listed, from the closed list
`ENTRY_QUOTE_REASONS`: `INVALID_MAX_AGE`, `INVALID_NOW`, `NOT_LIVE`, `BID_MISSING`,
`ASK_MISSING`, `BID_NON_POSITIVE`, `ASK_NON_POSITIVE`, `LOCKED`, `CROSSED`,
`BID_TICKS_NOT_LIVE`, `ASK_TICKS_NOT_LIVE`, `BID_TIME_UNKNOWN`, `ASK_TIME_UNKNOWN`,
`BID_FROM_FUTURE`, `ASK_FROM_FUTURE`, `BID_STALE`, `ASK_STALE`, `BID_NOT_FINITE`,
`ASK_NOT_FINITE`, `HALTED`, `UNREPORTED_EVIDENCE_EXPIRED`. Wall-clock times are never used
for age. The runtime's shadow mode (docs/RUNTIME.md, "Shadow mode") maps held-stream snapshots
into execution quotes so the gateway's anchored-entry rule refuses everything this check refuses
(`execution/shadow_quotes.py`, property-tested); it uses the session exactly as documented here.

### Run the stream check (only after the independent review, with the owner's approval and paper TWS running)

```sh
cd <worktree>
PYTHONPATH="$PWD/src" ~/.venvs/tradingbot/bin/python scripts/ibkr_quote_stream_check.py \
    [--config ~/.config/tradingbot/ibkr/paper.toml] [--report-dir <workspace>/private/ibkr_reports] \
    [--contract symbol=TLT,sec_type=STK,exchange=SMART,currency=USD,primary_exchange=NASDAQ]... \
    [--seconds 20] [--market-data-type 1|3] [--max-age 3]
```

- Arguments are validated before the private config is read or any socket opens (exit 2):
  `--contract` takes the `[[contract]]` fields of the discovery query file as `key=value` pairs
  (repeatable, at most 10; default TLT), `--seconds` 1..120 (default 20), `--market-data-type`
  1 or 3 only (default 3, one type for the whole run; 2 and 4 can never be LIVE), `--max-age` (0, 60] (default 3).
- It loads the private config exactly as the paper check does; preflight, identity gate and
  read-only verification all apply. For each contract in turn it resolves the contract, opens one
  stream, polls `latest()` every 0.25 s for the hold, then closes it; then it disconnects.
- Per contract it prints and records: requested and reported type, tick classes per side, updates
  per side, per-side age at the end, how long after opening each side first moved, the largest
  per-side age seen while holding, how many polls passed the entry check, final label and reason,
  entry reasons at the end, and the notices.
- It writes `ibkr_quote_stream_check_<UTC>.json` (schema `tradingbot.ibkr.quote_stream_check/1`,
  file 0600, directory 0700, masked account only) to the paper check's report directory. Exit 0
  when every contract was processed and cleanup was clean, otherwise 1.

### What only a real TWS can confirm

- Whether a quiet but live market updates each side often enough for a 3-second limit (the
  "largest age while held" and "polls usable" columns), per instrument and time of day.
- How long after subscribing the second change and the first price change arrive on each side
  (until then an unreported-type stream is `TYPE_UNREPORTED_NO_UPDATES_YET` or
  `TYPE_UNREPORTED_NO_PRICE_CHANGE_YET`), and whether quiet unreported markets keep changing
  prices within the 5-second window.
- Which placeholders TWS really sends before the first quote (-1 / 0 are assumed), whether a
  frozen or closed market ever sends a changed value, and whether `marketDataType` arrives late on
  a stream (the label then moves to rule 11, or to rule 6/4 and stays not live).
- What a second `reqMarketDataType` does to an already open subscription (the adapter avoids the
  question by refusing a conflicting type while streams are open).
- How 2186 / 10090 / 10167 are attached to the request in practice (by reqId, or only as a
  session-wide notice with reqId -1, which the adapter records in `session.notices` but cannot
  attach to a stream).
