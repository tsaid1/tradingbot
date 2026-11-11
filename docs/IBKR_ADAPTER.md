# IBKR read-only adapter (`tradingbot.ibkr`)

A thin adapter over the **official** Interactive Brokers TWS API for Python (`ibapi`,
`EClient`/`EWrapper`/`EReader`). This phase is **read-only and paper-only**. The adapter checks the
local paper environment, connects, checks the account identity, verifies
that the API is in read-only mode, then resolves a contract, reads the account summary and
positions, and takes a quote snapshot.

| Module | Imports `ibapi`? | Purpose |
|---|---|---|
| `config.py` | no | `PaperConnectionConfig`, loaded fail-closed from a private TOML file |
| `environment.py` | no | local preflight before any socket opens: TWS profile mode and listener owner |
| `identity.py` | no | identity-gate decision (managed accounts must be exactly the configured one) |
| `models.py` | no | result dataclasses, tick-type map, quote-quality assessment |
| `errors.py`, `redact.py` | no | typed exceptions, error-code classification, account masking |
| `check.py` | no (lazy) | step runner, verdict and report for the check script |
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
  - **No attribute path to an unfiltered send.** `_GuardedConnection` has `__slots__`, exposes
    exactly the five methods the SDK uses (`connect`, `disconnect`, `isConnected`, `recvMsg`,
    `sendMsg`) and does not forward other attributes, so `conn.socket` and `conn.lock` no longer
    exist. It holds no reference to the SDK `Connection`. That object lives only in the
    module-private `session._RAW_CONNECTIONS` (a `WeakKeyDictionary` keyed by the guard), and
    `_raw_call` never binds it to a local. An exception raised inside the transport leaves the
    guard with its traceback, context and cause cut, so a handed-out exception's frames cannot lead
    back to it either.
  - A permanent test (`test_object_graph_of_connected_session_reaches_no_unfiltered_send`) walks
    the object graph of a connected session, plus an exception the session raised, breadth-first
    to depth 16 with cycle protection. It follows container items, `__dict__` entries, `__slots__`,
    every non-dunder attribute and property, and `__self__`, `__func__`, `__closure__` cells,
    `__wrapped__`, `__defaults__`, `__traceback__`/`tb_next`/`tb_frame`/`f_locals`, `__context__`,
    `__cause__` and weak references. It asserts that no fake connection or `socket.socket` is
    reachable. It then calls every reachable callable named like a send or write with
    PLACE_ORDER and UPDATE_CONFIG, in each argument shape the SDK's send primitives take, and
    asserts that not one byte reached the fake wire and that the session still reads.
    - It deliberately does not follow classes, modules, `__globals__`, `f_globals`, builtins or the
      caller's own stack (`f_back`), and it skips stdio streams and code objects.
  - **What remains, and is out of scope for an in-process filter.** Code running in the same
    Python process can still:
    - open its own socket to the TWS port;
    - reach the raw connection or socket through module globals (`import`, a function's
      `__globals__`, `session._RAW_CONNECTIONS`), `gc.get_objects()`/`gc.get_referents()`,
      `sys._current_frames()` or the `socket` module.

    The filter stops mistakes and stray calls inside the adapter's own object graph, not
    deliberate in-process code. The real controls for that are TWS's own **Read-Only API**
    setting, which the adapter verifies on every connect (step 5 above), and process isolation
    (run nothing untrusted in the process that holds the session).
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
  1. A snapshot `reqMktData`, waiting up to 20 s.
  2. Only if the snapshot delivered no bid, ask or last price: one streaming sample
     (`snapshot=False`) for a bounded window (8 s by default, configurable), always followed by
     `cancelMktData`.

  Nothing collected is ever discarded. A snapshot that does not end is returned with what arrived
  and flagged `SNAPSHOT_END_MISSING`. Each attempt records:
  - tick counts by tick-type name;
  - every `marketDataType` value delivered;
  - `tickReqParams` (including `snapshotPermissions`);
  - every error and notice carrying its reqId;
  - the first and last tick times, and the time waited.

  The SDK decodes delayed tick types (66–76, 88) exactly like live ones, and the adapter maps
  delayed bid, ask, last, their sizes, close and the last timestamp. The verdict is:
  - `PASS`: a LIVE two-sided quote with a broker-reported type, by either route;
  - `PASS_WITH_WARNINGS`: only delayed or frozen two-sided data, or only partial data (the report
    says which);
  - `FAIL`: nothing at all arrived on either route.

  Further rules:
  - `LIVE` is reported only when TWS reports type 1.
  - `received_at` is the only time available for bid/ask. `last_trade_time` dates the last trade
    only.
- **Recovery.** Sessions are single-use. A new `IbkrSession` re-runs every check.
- **Orders (later).** Order code belongs in a separate, separately reviewed module. It must not be
  added to `session.py`.

## 5. Run the connected check (only with the owner's approval and paper TWS running)

```sh
cd <worktree>
PYTHONPATH="$PWD/src" ~/.venvs/tradingbot/bin/python scripts/ibkr_paper_check.py \
    [--config ~/.config/tradingbot/ibkr/paper.toml] [--report-dir <workspace>/private/ibkr_reports]
```

The check runs these steps in order:

1. Preflight and connect: profile, listener, version, identity gate and read-only check.
2. Resolve TLT (STK/SMART/USD, primary NASDAQ).
3. Account summary, then positions, then the TLT quote (snapshot, plus streaming fallback if
   needed).
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
