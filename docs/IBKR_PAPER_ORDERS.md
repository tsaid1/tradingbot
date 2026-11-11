# IBKR paper order mode (`tradingbot.ibkr_orders`)

**Status: built and tested offline only. No order of any kind is authorized.** Running it needs the
owner's explicit, separate authorization of one bounded experiment and an independent safety review
(AGENTS.md rule 1).

| Module | SDK? | Purpose |
|---|---|---|
| `caps.py` | no | `PAPER_TEST_1`: the experiment caps, as code constants |
| `config.py` | no | `load_order_config`: the read-only config plus the hand-written `orders` entry |
| `guard.py` | no | `order_guard(order, ctx) -> ALLOW / Refusal(reason)`: pure, no I/O |
| `counter.py` | no | durable, fsync'd, `flock`ed submission journal |
| `mapping.py`, `timefmt.py`, `records.py` | no | status mapping, broker times, plain records and errors |
| `broker.py` | no | `IbkrPaperBroker`: the execution core's `Broker` protocol |
| `session.py` | **yes** | `PaperOrderSession(IbkrSession)`, `_OrderClient`, `_OrderGuardedConnection` |

The read-only `IbkrSession` is unchanged and still cannot send an order message. The order code
lives in this separate package; the read-only package's AST test still forbids order names there.

## Safety model

1. **Enabling.** Order mode exists only when `PaperOrderSession` is given a `PaperOrderConfig`
   loaded from the private file **and** that file says `orders = "paper-test-1"`, a key of
   `caps.EXPERIMENTS`. There is no default, no environment variable and no flag; the package
   contains no `os.environ`/`getenv` (AST test). The session also refuses a config whose caps
   object is not the one in the registry.
2. **Every read-only check still runs first**: config file safety, paper port (7497/4002), paper
   account, loopback host, jts.ini `tradingMode=p`, listener owned by `tws_os_user`, server version,
   the identity gate.
3. **Order-mode config read** (never a write): `readOnlyApi` present and **False**,
   `allowLocalhostOnly` present and True, `socketPort` equal to the configured port. Anything else
   raises `OrderModeSettingRefused` and disconnects before any order. A later differing config
   response poisons the session (inherited).
4. **One contract.** After READY the session resolves TLT/STK/SMART/USD (primary NASDAQ) itself and
   pins its conId, min tick, time zone and liquid hours.
5. **Path to the wire.** `_send_place` is the only path to a PLACE_ORDER frame. Under the session
   lock it runs `order_guard` immediately before the send, appends and fsyncs the submission to the
   journal, issues a one-shot permit for exactly those protobuf bytes, then calls the SDK. The
   guarded connection passes PLACE_ORDER and CANCEL_ORDER only when they are protobuf-framed and
   match an unused permit. It then re-checks the decoded order against the caps: account, conId,
   type, TIF, quantity, explicit `outsideRth=false` and `transmit`, no attached orders, and every
   other field equal to the SDK default. Replaying permitted bytes, or calling
   `placeOrderProtoBuf`, `sendMsgProtoBuf` or `sendMsg` directly, sends nothing (tests).
6. **Poisoning.** An `openOrder`, `execDetails` or `completedOrder` naming another account poisons
   and disconnects the session. `orderStatus` and `commissionAndFeesReport` carry no account. A
   status is applied only to this client id's own orders.
7. **Residual.** This is an in-process filter. With Read-Only API OFF it is the control: code
   that uses `gc`, module globals, `ctypes` or its own socket is out of scope (merge review). The
   live account is kept out by the independent paper-identity checks, not by this filter.

## The caps (`caps.py`, enforced in `guard.py`, re-checked at the wire)

TLT only, STK, USD, the session-resolved conId. Long only. At most 1 share per order and at most
1 share net, counting working buys. At most 150 USD per order (quantity × limit or stop). Order
types LMT and STP only (MKT refused separately). TIF DAY. `outsideRth` false, and the clock must be
inside the broker-reported liquid hours. Prices must be on the min tick; an off-tick price is
refused, never rounded. A SELL is allowed only in two cases:

- as a protective child no larger than its BUY parent;
- to reduce a long, and never for more than the position less other working sells (one-cancels-
  other siblings count once; a PendingCancel still counts).

The cap is **6 submissions**, and every `placeOrder` counts (bracket children and amendments too).
Cancels do not count and stay possible after the cap, because they only reduce risk.

Refusal reasons (`RefusalReason`):

- budget: `EXPERIMENT_FINISHED`, `SUBMISSION_CAP` (a whole bracket must fit before anything is
  sent), `SUBMISSION_COUNT_UNKNOWN`;
- identity: `WRONG_ACCOUNT`, `WRONG_SYMBOL`, `WRONG_SEC_TYPE`, `WRONG_CURRENCY`,
  `WRONG_CONTRACT_ID`, `ORDER_REF_MISSING`;
- order shape: `MARKET_ORDER`, `ORDER_TYPE_NOT_ALLOWED`, `TIF_NOT_ALLOWED`, `OUTSIDE_RTH_FLAG`,
  `BAD_SIDE`, `QUANTITY_INVALID`, `ORDER_QUANTITY_CAP`, `PRICE_INVALID`, `OFF_TICK_PRICE`,
  `TICK_UNKNOWN`, `NOTIONAL_CAP`;
- clock and hours: `OUTSIDE_LIQUID_HOURS`, `LIQUID_HOURS_UNKNOWN`, `CLOCK_NOT_AWARE`;
- position: `POSITION_UNKNOWN`, `SHORT_POSITION_PRESENT`, `POSITION_CAP`, `SHORT_SALE`,
  `SELL_EXCEEDS_POSITION`, `CHILD_INVALID`, `MODIFICATION_INVALID` (an amendment may change only
  price or quantity).

The **counter** is `<state_dir>/ibkr_paper_orders/paper-test-1.submissions`, one JSON line per
placement, written and fsync'd before the wire. A torn, edited or out-of-sequence line makes the
count unknown, and everything is then refused. A second process cannot open it (`flock`). New
order ids always come from `reqIds`/`nextValidId` and sit above every id in the journal.

## Broker mapping (`IbkrPaperBroker`)

- **`submit`.** The parent goes with `transmit=False` and the last child with `transmit=True`,
  children with `parentId` (an OCA group when there are two). It returns snapshots only once every
  order of the bracket has an acknowledgement. Errors map as follows:
  - a guard refusal, or any failure before anything was sent: `BrokerError` (definite; the
    coordinator never resends);
  - no acknowledgement in `ack_timeout`, or a send failure (SDK code 512): `AckLost`, never retried;
  - a broker rejection (error callback before any status): `REJECTED` snapshots.
  Whether anything was sent is decided from the journal count, not from the exception type.
- **Partial bracket.** After the last order is transmitted, the session waits for every id of the
  bracket. It declares a partial bracket when the parent was acknowledged (or a child rejected) and
  a child is rejected or unacknowledged, or when a refusal or exception stopped the bracket after
  the parent was sent. It then sends a cancel for the parent and raises `PartialBracket`, a subclass
  of `AckLost`. The coordinator journals `ACK_LOST`, never `ACK`, and adopts what the lookup
  finds: a cancelled entry, or a filled entry with a missing stop, which gives
  `UNPROTECTED_POSITION`.
- **`modify`.** Re-places under the original id (counts as a submission). It succeeds only when an
  `openOrder` echo shows the new values. An error gives `BrokerError` (the original stands); a
  timeout gives `AckLost`.
- **`cancel`** goes through `cancelOrder` for this client's order. The status is PendingCancel until
  error 202 or a Cancelled status arrives. Errors 161, 10147 and 10148 on a non-terminal order give
  `BrokerError`.
- **Statuses.** The statuses are PendingSubmit, ApiPending, PreSubmitted, Submitted (PartiallyFilled
  when filled > 0 and remaining > 0), PendingCancel, Filled, Cancelled, ApiCancelled and Inactive.
  A rejection arrives through `error`. Terminal states are sticky: a late Submitted after Filled is
  ignored. Filled quantity only grows. An execution is recorded once per execId.
- **Quotes** come from the stream-first `quote()` with the session's own quality; DELAYED and FROZEN
  are never upgraded.
- **Positions, orders and fills** carry the conId. The protocol's optional contract fields (broker
  contract id, multiplier, min tick, size increment) are passed when the model defines them. A read
  that fails raises `BrokerError`; it never returns an empty list.

**Lookup horizons** (from the SDK source and docstrings; not verified against a real TWS):

| Lookup | Call | Coverage | Lag |
|---|---|---|---|
| Open orders | `reqAllOpenOrders` | every client and TWS, working orders only | can lag a fresh placement (the session adds its own still-working orders, reason `LOCAL_STATE_NOT_IN_SNAPSHOT`) |
| Executions | `reqExecutions`, account filter | "the past 24 hours" / current day only (SDK docstring); older ones need the TWS Trade Log | — |
| Completed orders | `reqCompletedOrders(apiOnly=True)` | API orders TWS still holds, in practice the current session or day | lag not documented |

## Authorizing, running and stopping `paper-test-1`

**To authorize** (owner, by hand, only after the review):

1. In paper TWS (user `tws_os_user`), set Read-Only API **OFF**, localhost only ON and the socket
   port to `port`.
2. `mkdir -m 700 <state_dir>`.
3. Write `~/.config/tradingbot/ibkr/paper_orders.toml` with mode 0600. It holds every key of
   `paper.toml` plus `orders = "paper-test-1"` and `state_dir = "<state_dir>"`.

**To run:** there is deliberately no runner script in this change. The authorized run needs a small
script, reviewed with it. That script connects `PaperOrderSession(load_order_config())` and wraps it
in `IbkrPaperBroker`. It drives `ExecutionCoordinator` with `protective_tif=DAY`, a `MockPolicy`
with `quantity=1` and `max_notional_usd=150`, `expected_account`, and a quote quality the paper
data actually delivers.

**To stop:**

- Create the halt file: no entry or amendment is sent, and working entries are cancelled.
- Remove the `orders` line, so the next session refuses order mode.
- Disconnect.
- Turn Read-Only API back ON: the real control. Order mode then refuses to connect.
- Cancel anything still working in TWS.

The experiment is finished at 6 journal lines. Nothing more can be placed, even after a restart.

**Planned sequence: 4 of 6 submissions.**

| Step | Submissions | Count |
|---|---|---|
| Bracket entry: BUY 1 LMT, parent, `transmit=False` | 1 | 1 |
| Its protective SELL 1 STP, DAY, `transmit=True` | 1 | 2 |
| One stop amendment, same order id | 1 | 3 |
| Close | 1 | 4 |

At the close, the coordinator's exit netting first **cancels the stop** and confirms it is gone
from open orders and present in completed orders. The cancel is not a submission. Only then does
it send the closing SELL 1 LMT (guard: 1 held, 0 working sells). Two submissions stay spare. The
offline lifecycle test runs exactly this sequence and asserts 4.

Note: a DAY stop expires at the close. If the position is not closed in the same session, it is
unprotected overnight.

## Execution-core change it needs

`ExecutionConfig.protective_tif` (default GTC, so behaviour is unchanged). The coordinator
hard-coded GTC on protective children and on their recovery rebuild. The DAY-only cap refuses GTC
(`TIF_NOT_ALLOWED`).

## Not modelled relative to the real broker

- Whether TWS honours an explicit protobuf `transmit=false`. The SDK omits a false bool, and TWS's
  default for an absent field is undocumented.
- Whether untransmitted orders appear in open orders.
- How a real child rejection affects its held parent.
- Simulated stops that report PreSubmitted while active. The coordinator's "child active ⇒ entry
  filled" inference treats PreSubmitted as inactive.
- Execution corrections (execId suffix revisions).
- Commission arriving before or after its execution.
- `openOrder` rows without an account. These are treated as foreign and poison the session.
- Order-id gaps from other clients.
- Real error codes and text for rejections, beyond 201, 202, 161, 10147 and 321.
- Real visibility lag.
- Margin and what-if.
- Market-data entitlement on the order path.
- Price-management algorithms.
- Positions that lag executions.
