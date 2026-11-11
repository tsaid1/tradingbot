# IBKR paper order mode (`tradingbot.ibkr_orders`)

**Status: built and tested offline only. No order of any kind is authorized.** Running it needs the
owner's explicit, separate authorization of one bounded experiment and an independent safety review
(AGENTS.md rule 1).

| Module | SDK? | Purpose |
|---|---|---|
| `caps.py` | no | `PAPER_TEST_1`: the experiment caps, as code constants |
| `config.py` | no | `load_order_config`: the read-only config plus the hand-written `orders` entry |
| `guard.py` | no | `order_guard(order, ctx) -> ALLOW / Refusal(reason)`: pure, no I/O |
| `counter.py` | no | durable, fsync'd, `flock`ed, hash-chained submission journal; `init_journal` |
| `mapping.py`, `timefmt.py`, `records.py` | no | status mapping, broker times, plain records and errors |
| `broker.py` | no | `IbkrPaperBroker`: the execution core's `Broker` protocol |
| `session.py` | **yes** | `PaperOrderSession(IbkrSession)`, `_OrderClient`, `_OrderGuardedConnection` |
| `experiment.py` | **yes** | the staged first experiment (`scripts/ibkr_paper_experiment.py`) |

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
   pins its conId, min tick, time zone and liquid hours. It then takes its position baseline:
   the executions known so far, then the position read.
4a. **Ways to construct an order-capable session.** Exactly one:
   `PaperOrderSession(load_order_config(path), <named timeouts>)`. The constructor takes no
   `preflight=`, `clock=` or `**kwargs`; `run_preflight` always runs and the clock is the wall
   clock. `PaperOrderConfig` objects not issued by `load_order_config` are refused, and the
   dict validator is private. A structural test asserts that no public callable in
   `ibkr_orders` accepts a parameter that replaces or skips the local checks, the identity gate,
   the config read, the guard, the counter or the clock. Offline tests reach the two local seams
   only through a test-only subclass in `tests/_ibkr_paper_testing.py`.
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
types LMT and STP only (MKT refused separately). **A BUY must be a limit order** (a buy stop
becomes a market order when triggered: `BUY_NOT_LIMIT`); a stop is allowed only as a SELL that
protects a bracketed BUY or reduces a held long (`STOP_NOT_PROTECTIVE` otherwise). TIF DAY. `outsideRth` false, and the clock must be
inside the broker-reported liquid hours. Prices must be on the min tick; an off-tick price is
refused, never rounded. A SELL is allowed only in two cases:

- as a protective child no larger than its BUY parent;
- to reduce a long, and never for more than the position less other working sells (one-cancels-
  other siblings count once; a PendingCancel or Inactive order still counts).

**Which position.** Two figures: (a) the broker's position read and (b) the position implied by
this session's own fills (the connect-time baseline plus every later fill; for an order this
session placed, the larger of its execution total and its `orderStatus` filled quantity, since
either can arrive first). The guard uses the more cautious of the two (the larger for a BUY, the
smaller for a SELL) and, when they differ, refuses everything with `POSITION_DISAGREEMENT` until
they agree again. A failed read is `POSITION_UNKNOWN`. If the read lagged at the baseline itself,
the disagreement persists for that session: restart it when flat and quiet.

**Which working orders.** Every row of the broker's open-orders answer whatever its status, plus
every order this session sent that is not final and not rejected even if no view shows it yet
(`SENT_NOT_VISIBLE`, an acknowledgement lost included), plus, after a restart, every journal
submission of an earlier session with no terminal evidence: a final status, a completed-orders row
(by order id or reference), executions covering its quantity, or absence from every lookup once
it is older than `JOURNAL_SETTLE_SECONDS` (120 s). So a second SELL is refused while an earlier
SELL is possibly live, and a second BUY likewise; a confirmed cancel frees it.

The cap is **6 submissions**, and every `placeOrder` counts (bracket children and amendments too).
Cancels do not count and stay possible after the cap, because they only reduce risk.

Refusal reasons (`RefusalReason`):

- budget: `EXPERIMENT_FINISHED`, `SUBMISSION_CAP` (a whole bracket must fit before anything is
  sent), `SUBMISSION_COUNT_UNKNOWN` (missing, mismatched or broken journal), and
  `SUBMISSION_COUNT_MISMATCH` (the broker shows more orders of the experiment than the journal);
- identity: `WRONG_ACCOUNT`, `WRONG_SYMBOL`, `WRONG_SEC_TYPE`, `WRONG_CURRENCY`,
  `WRONG_CONTRACT_ID`, `ORDER_REF_MISSING`;
- order shape: `MARKET_ORDER`, `ORDER_TYPE_NOT_ALLOWED`, `TIF_NOT_ALLOWED`, `OUTSIDE_RTH_FLAG`,
  `BAD_SIDE`, `QUANTITY_INVALID`, `ORDER_QUANTITY_CAP`, `PRICE_INVALID`, `OFF_TICK_PRICE`,
  `TICK_UNKNOWN`, `NOTIONAL_CAP`, `BUY_NOT_LIMIT`, `STOP_NOT_PROTECTIVE`;
- clock and hours: `OUTSIDE_LIQUID_HOURS`, `LIQUID_HOURS_UNKNOWN`, `CLOCK_NOT_AWARE`;
- position: `POSITION_UNKNOWN`, `POSITION_DISAGREEMENT`, `SHORT_POSITION_PRESENT`, `POSITION_CAP`, `SHORT_SALE`,
  `SELL_EXCEEDS_POSITION`, `CHILD_INVALID`, `MODIFICATION_INVALID` (an amendment may change only
  price or quantity).

### The submission journal (`counter.py`)

`<state_dir>/ibkr_paper_orders/paper-test-1.submissions`, plus the head anchor
`paper-test-1.submissions.head`. One canonical-JSON object per line:

| Line | Fields |
|---|---|
| 0, header | `seq`=0, `type`="header", `version`=2, `experiment`, `account_masked`, `created_at`, `caps_digest` (SHA-256 of the code caps), `prev` (64 zeros), `hash` |
| n ≥ 1, one per `placeOrder` | `seq`=n, `type`="submission", `experiment`, `order_id`, `parent_id`, `order_ref` (the wire ref, with prefix), `kind` (SINGLE / PARENT / CHILD / MODIFY), `action`, `quantity`, `order_type`, `limit_price`, `stop_price`, `at`, `prev` (the previous line's `hash`), `hash` |

`hash` is the SHA-256 of the line's canonical JSON without `hash`. The head file holds the last
`{seq, hash}` and is rewritten atomically after each append (it may trail by one after a crash,
which is accepted). The used count is the number of submission lines.

- **The session never creates the journal.** The owner creates it once with
  `python scripts/ibkr_paper_init_journal.py` (offline: it opens no socket; it refuses if a journal
  exists). A missing journal, a header that does not match the experiment, the masked account and
  the caps digest, a break in the sequence or the chain, a torn line, or a journal that does not
  reach its head anchor (lines cut off the end) makes the count unknown: everything is refused
  with `SUBMISSION_COUNT_UNKNOWN`, never a fresh budget. The session still connects then, so
  cancels (and `abort`) stay possible.
- **Second source.** Every order of the experiment carries the prefix `paper-test-1:` in
  `orderRef` before the execution core's own reference; the session strips it in every lookup,
  so lookups by reference are unchanged. Before the first submission of every session (a restart
  is a new session) it counts the distinct orders with that prefix in open orders (every client),
  executions and completed orders, and uses the larger of that count and the journal's; if the
  broker shows more, it refuses (`SUBMISSION_COUNT_MISMATCH`).
- **What still cannot be prevented.** Someone who deletes the journal AND its head, initialises a
  new one, and does so after the broker's own history of the experiment has rolled off (the
  current day at IBKR) gets a fresh budget. Nothing in software can tell. The owner controls the
  state directory (`state_dir` in the private config); keep it outside Dropbox and do not delete
  it during the experiment.
- A second process cannot open the journal (`flock`). New order ids always come from
  `reqIds`/`nextValidId` and sit above every id in the journal.

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
  A rejection arrives through `error`. Final states (Filled, Cancelled, ApiCancelled, Rejected)
  are sticky: a late Submitted after Filled is ignored. **Inactive is not final at IBKR** (an
  inactive order may be rejected, or merely held and later active): it still counts as a working
  order for netting and the position cap, `cancel` acts on it, a later Submitted applies, and only
  an explicit rejection error makes it Rejected. It reaches the execution core as `UNKNOWN` with
  reason `IBKR_INACTIVE` (the core lists INACTIVE among its terminal statuses and records it as
  rejected), so the intent stays unresolved and protection unconfirmed. Filled quantity only grows. An execution is recorded once per execId.
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

## Authorizing and running `paper-test-1`: the staged runbook

Nothing here may run without the owner's explicit authorization of this experiment and the
independent safety review. Every stage is one command, writes one JSON evidence file (masked
account only) to `<state_dir>/ibkr_paper_orders/paper-test-1.evidence/` and stops. A stage refuses
unless the previous stage's evidence exists and passed; a passed stage never repeats; a stage that
submitted never repeats (the journal count must equal the count the stage starts from). There is
no flag to skip or repeat. All stages go through the same session, guard and counter.

**Submission arithmetic:** stage 2 = 2 (BUY LMT parent + SELL STP child) and stage 3 = 4 (bracket
entry 2, one stop amendment 1, closing limit 1): 2 + 4 = 6 = the cap (asserted in a test).

**Once, before stage 0** (owner, by hand):

1. Close any live TWS. Start the paper TWS as `tws_os_user`, logged in to the paper account.
2. `mkdir -m 700 <state_dir>` (outside Dropbox).
3. Write `~/.config/tradingbot/ibkr/paper_orders.toml`, mode 0600: every key of `paper.toml` plus
   `orders = "paper-test-1"` and `state_dir = "<state_dir>"`.
4. `python scripts/ibkr_paper_init_journal.py` (offline). It prints the journal path.
5. Plan the day: start stage 3 no later than 14:30 ET; the experiment ends flat the same day.

| Stage | Owner does first | Command | Passes when | Count |
|---|---|---|---|---|
| 0 | TWS API settings: **Read-Only API ON**, localhost only ON, socket port = `port` | `python scripts/ibkr_paper_experiment.py stage0-refusal` | order mode refuses (`readOnlyApi=True`) and no order frame was sent | 0 |
| 1 | Turn **Read-Only API OFF**. Nothing else. | `python scripts/ibkr_paper_experiment.py stage1-lookups` | open orders, executions, completed orders and positions all complete; no order frame; the evidence lists which fields the rows carry (order id, `orderRef`, perm id; the account is checked on every row) and the quote quality | 0 |
| 2 | Check stage 1 evidence. Watch TWS. | `python scripts/ibkr_paper_experiment.py stage2-unfillable-bracket` | one bracket, BUY LMT at 90% of the bid with its SELL STP 1.00 lower, never filled; the parent was not answered before the child was sent; both seen in the read-back; both cancelled, gone from open orders and in completed orders | 2 |
| 3 | Check stage 2 evidence in full (especially `parent_answered_before_child_sent` and the callback times) and the TWS Audit Trail. | `python scripts/ibkr_paper_experiment.py stage3-lifecycle` | bracket entry at the ask filled, stop confirmed working, one stop raise (+0.10), stop cancelled and confirmed, closing limit filled, reconciliation clean, 6 used | 4 |
| any | — | `python scripts/ibkr_paper_experiment.py abort` | every working order of this client cancelled by its own order id; reports what remains and the position | 0 |

After stage 3 (or on any failure): turn Read-Only API back ON, remove the `orders` line, and keep
the evidence directory and journal. If stage 2 reports a fill, run `abort` and close the share by
hand (below); stage 3 then never runs.

## Emergency stop

- **Not known to cancel anything (unverified):** disconnecting the client, killing the script, or
  turning Read-Only API back ON. Working orders may stay live in TWS after any of these. The
  halt file only affects a coordinator that is still running (stage 3): it stops new entries and
  amendments and cancels working entries, not the stop or a placed close.
- **The certain stop:** in the paper Trader Workstation window, cancel every working TLT order of
  the experiment (Orders / Activity panel, right-click, Cancel), then close any TLT position by
  hand (SELL 1 TLT, a limit at the bid), and confirm in the Portfolio that TLT is 0.
- **The software path:** `python scripts/ibkr_paper_experiment.py abort` cancels every working
  order of this client by its own order id (never a global cancel) and reports what remains. It
  works even when the journal is unknown. It does not close a position.
- **The close.** The protective stop is DAY: it expires at 16:00 ET and a share held overnight is
  unprotected; nothing can be placed after 16:00 ET. The experiment must end flat the same day.
  **If the closing limit has not filled by 15:30 ET** (30 minutes before the close): run `abort`,
  then in TWS sell the share by hand with a limit at the bid (lower it a tick at a time until it
  fills), confirm the position is 0 before 16:00 ET, and record what you did beside the evidence.

## Execution-core change it needs

`ExecutionConfig.protective_tif` (default GTC). Stage 3 sets DAY; the DAY-only cap refuses GTC
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
- Positions that lag executions (the guard refuses on a disagreement; the lag is measured in
  stage 3).
- Execution corrections (a new execId for the same fill) would count twice in the session's own
  figure; the result is a disagreement, so a refusal.
