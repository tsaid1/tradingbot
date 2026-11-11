# IBKR read-only adapter (`tradingbot.ibkr`)

A thin adapter over the **official** Interactive Brokers TWS API for Python (`ibapi`,
`EClient`/`EWrapper`/`EReader`). This phase is **read-only and paper-only**: it connects, checks the
account identity, resolves a contract, reads the account summary and positions, and takes a quote
snapshot. There is no order code in this package. A test scans it for order-affecting API names.

| Module | Imports `ibapi`? | Purpose |
|---|---|---|
| `config.py` | no | `PaperConnectionConfig`, loaded fail-closed from a private TOML file |
| `identity.py` | no | identity-gate decision (managed accounts must be exactly the configured one) |
| `models.py` | no | result dataclasses, tick-type map, quote-quality assessment |
| `errors.py`, `redact.py` | no | typed exceptions, error-code classification, account masking |
| `check.py` | no (lazy) | step runner, PASS/FAIL verdict and report for the check script |
| `session.py` | **yes** | `IbkrSession`: owns one `EClient` + `EWrapper`, synchronous requests with timeouts |

## 1. Install the SDK (not on PyPI, never committed)

The SDK is IBKR-licensed. Keep its source outside every git tree and install it into the venv as a
regular (non-editable) package:

```sh
uv pip install --python ~/.venvs/tradingbot/bin/python "<workspace>/private/vendor/twsapi-1052.1/pythonclient"
~/.venvs/tradingbot/bin/python -c "import ibapi; print(ibapi.__version__)"   # 1052.1
```

GitHub CI does not have the SDK. The SDK tests skip there and the pure-logic tests still run. To
reproduce that locally, run `TRADINGBOT_BLOCK_IBAPI=1 python -m pytest` (`tests/conftest.py` then
blocks `import ibapi`).

## 2. Private config

The default path is `~/.config/tradingbot/ibkr/paper.toml`. Set the file to mode `0600` and the
directory to `0700`. The file must be owned by you, must be a regular file and must not be a
symlink:

```toml
host = "127.0.0.1"     # loopback IP literal only (127.0.0.1 or ::1)
port = 7497            # paper TWS; 7496 and 4001 (live defaults) are refused
client_id = 17         # 1..2^31-1; 0 is refused (TWS's special manual-order client)
account = "DU0000000"  # the one allowlisted paper account (placeholder shown)
mode = "paper"         # must be exactly "paper"
```

Each problem raises its own error type: `ConfigNotFound`, `ConfigIsSymlink`,
`ConfigNotRegularFile`, `ConfigWrongOwner`, `ConfigPermissionsTooOpen`, `ConfigInvalid`,
`NonLoopbackHost`, `LivePortRejected`, `NonPaperAccount` or `ModeNotPaper`.

The rule that the account must start with `D` is **defence in depth only**. The **primary control**
is the identity gate: the broker's own `managedAccounts` must be exactly the configured account.

## 3. TWS settings the owner must set (paper TWS, macOS user `tradingbotpaper`)

- Log in to **Paper Trading** mode. The profile of macOS user `tariq` is in LIVE mode, so do not
  use it.
- Global Configuration → API → Settings:
  - **Enable ActiveX and Socket Clients**: ON
  - **Read-Only API**: ON for this phase
  - **Socket port**: `7497`
  - **Allow connections from localhost only**: ON
  - Master API client ID: not equal to the `client_id` above
- Make sure no other session uses the same `client_id`. A competing live login for the same user
  can also block paper market data (error 10197).

## 4. Safety model

- **Identity gate.** `connect()` waits for `nextValidId` and `managedAccounts`. If the list is not
  exactly `[configured account]`, the session disconnects and raises `AccountIdentityError`. This
  covers a different account, more than one account, an empty list and a timeout. No data request
  is sent before the gate passes. The expected account always comes from the config, never from
  broker rows.
- **Poisoning.** If any later account-scoped row (a summary row, a position row or a repeated
  `managedAccounts`) names a different account, the session is poisoned, disconnected, and the
  request raises `AccountIdentityError`.
- **Masking.** The full account ID never appears in a `repr`, an exception, the report or a log
  line. Exception text passes through `redact_accounts`. The session sets the `ibapi` logger to
  WARNING because the SDK logs raw messages at INFO/DEBUG. The check script also refuses to write a
  report that contains the configured ID.
- **Errors.** Errors are routed by `reqId`. Farm and status codes (2104, 2106, 2107, 2108, 2158,
  2119, other 2100–2199 codes, and 10167 "displaying delayed data") are recorded as notices.
  Connection codes (502, 504, 1100, 1101, 1102, 1300, 2110) are recorded as connection events, and
  the loss codes fail any outstanding request at once. Entitlement refusals for a quote (354, 10089,
  10168, 10186, 10197) produce quality `UNAVAILABLE`. Anything else raises
  `IbkrRequestError(code, message)`.
- **Cleanup.** Account summary and positions subscriptions are always cancelled. A contract request
  or snapshot that has not ended is cancelled. A timeout raises `IbkrTimeout` after the cancel is
  sent. TWS does not acknowledge cancels, so "clean" means the connection was up and the SDK
  reported no send error. A cancel that fails raises `CleanupError`, or is attached as a note to the
  primary error. It is never reported as success. `disconnect()` closes the socket, joins the
  handshake, `EClient.run` and `EReader` threads within a bound, and raises `CleanupError` if any of
  them is still alive.
- **Quotes.** The session sends `reqMarketDataType(3)` and then `reqMktData(snapshot=True)`. Type 3
  means live data when the account is entitled, otherwise delayed data. The result reports the type
  the broker actually delivered and a quality of `LIVE`, `DELAYED`, `FROZEN` or `UNAVAILABLE`, with
  flags such as `MISSING_BID`, `NON_POSITIVE_ASK`, `CROSSED` and `MARKET_DATA_TYPE_NOT_REPORTED`.
  `LIVE` is never claimed unless the broker reported type 1. `received_at` is the local receipt
  time, and it is the only time available for bid/ask. `last_trade_time` (tick 45/88) dates the
  last trade only, so it never makes bid/ask "fresh".
- **Recovery.** A session is single-use. After a disconnect or a dropped connection, create a new
  `IbkrSession`. Its `connect()` runs the identity gate again.
- **Seam for orders (later).** Order code belongs in a separate module that is given a READY session
  explicitly. Do not add it to `session.py`.

## 5. Run the connected check (only with the owner's approval and paper TWS running)

```sh
cd <worktree>
PYTHONPATH="$PWD/src" ~/.venvs/tradingbot/bin/python scripts/ibkr_paper_check.py \
    [--config ~/.config/tradingbot/ibkr/paper.toml] [--report-dir <workspace>/private/ibkr_reports]
```

The check runs these steps in order: load config, connect and identity gate, resolve TLT
(STK/SMART/USD, primary NASDAQ), account summary, positions, TLT snapshot, disconnect, then one
reconnect and disconnect to prove recovery. It writes
`ibkr_paper_check_<UTC>.json` (mode 0600, directory 0700) and prints a summary.

The report contains the masked account, the SDK version, the server version, the connection time
reported by TWS, notices and per-step status with timings. Overall `PASS` requires every step,
cleanup included, to pass. A delayed or unavailable quote does not fail the run, but it is listed
under `warnings`. The exit code is 0 only on PASS.

The default report directory is `$TRADINGBOT_IBKR_REPORT_DIR`. If that is unset, it is
`private/ibkr_reports` in the parent or grandparent directory of the checkout.
