# Execution core

`src/tradingbot/execution/` turns gateway-approved actions into broker orders and keeps the follower
ledger in line with what the broker actually filled. It runs against `SimBroker` only. There is no
network code and no real broker in this package. A real Interactive Brokers paper adapter can be
added later by implementing the same `Broker` protocol, and that adapter needs its own safety
review (AGENTS.md rule 1).

## The model

```
SourcePost -> PaperEventReplay.process  (gate_source, gate_action, atomic groups, once-only)
                 | group approved in full
                 v
           _execute_group  --(BrokerEventReplay only)-->  ExecutionCoordinator.execute
                                                             | write-ahead intent (SQLite)
                                                             v
                                                           Broker  (SimBroker now, IBKR next)
                                                             | executions / open orders
                                                             v
                                       follower ledger  <- fills only  -> replay ideas overwritten
```

* **Authority stays in `validator.py`.** `execute()` raises `ExecutionRefused` unless it is given an
  APPROVE `GateDecision`, and it sizes every order with `validator.fill_quantity`. Its own checks are
  execution preconditions that the gateway cannot see. They can refuse an order but never approve
  one:
  * the broker's own quote: quality `LIVE` (configurable), not stale, not crossed, positive, and
    inside the policy collar `max_price_deviation_pct` around the action's entry reference. The
    entry is a limit order at the collar bound.
  * exposure caps: `MockPolicy.max_instrument_exposure_usd`, which defaults to `max_notional_usd`,
    and `max_aggregate_exposure_usd`. Per instrument the larger of the ledger's and the broker's
    position counts (a broker-only position counts too), valued at the usable quote's midpoint, at
    cost when there is none. Every entry or add that is not terminal (recorded, of unknown outcome,
    PreSubmitted or working) adds its full intended quantity at its limit, less what it has
    already filled into the ledger. Unreadable broker positions refuse the entry.
  * no open `LEDGER_REVERSAL` exception in the instrument (`INSTRUMENT_BLOCKED_LEDGER_REVERSAL`).
  * the account identity, when `ExecutionConfig.expected_account` is set.
  * the halt file, checked right before every submit or amend. It defaults to
    `~/.local/state/tradingbot/HALT` and can be overridden with `TRADINGBOT_HALT_FILE`.
* **One coordinator per store.** The coordinator takes an exclusive `flock` on
  `<execution store>.lock` for its whole life. A second coordinator on the same store, in this
  process or another, refuses to start with `ExecutionStoreLocked` (`run_service.py` exits 2), so
  no other process's `recover()` can resend this one's recorded intent. The OS releases the lock
  when the process dies.
* **Idempotency key.** `order_ref = "TB-" + sha256(source_id, revision, action_id)[:20]`. Protective
  children are `<ref>.STP` and `<ref>.TGT`. The broker stores the ref and returns it on every
  order, fill and open-order row (IBKR `Order.orderRef`).
* **Follower ledger** (`exec_positions`). It moves only in `ExecutionStore.apply_fill`, once per
  broker `exec_id`. Stop triggers, partial fills and fills from a recovered order all arrive through
  the same `sync()` path. The `stop_price` on an idea changes only after the amendment is seen at
  the broker. An exit fill larger than the position is recorded exactly as it happened; the ledger
  stops at zero, the idea becomes `REVERSED`, and a `LEDGER_REVERSAL` exception opens in the same
  transaction. `sync()`, `execute()` and `process()` keep working; new exposure in that instrument
  is refused until a human resolves the exception.
* **Protection is confirmed, not assumed.** After every fill, `sync()` sizes the idea's working
  stop and target to the *cover*: the ledger quantity less every non-terminal REDUCE / CLOSE
  (recorded, unknown outcome or working, less what it filled). It cancels them when the cover is
  zero or the idea is flat. It then reads `open_orders()`. Every applied fill resets the idea's
  protection to `UNKNOWN`. It becomes `CONFIRMED` only when a broker read that succeeded after
  the last fill shows a working (not PendingCancel) STP covering exactly the cover. A read that
  fails opens `PROTECTION_UNVERIFIED`. A read that shows no such stop opens `UNPROTECTED_POSITION`.
  Either way the outcome is `EXCEPTION`, not `EXECUTED`. `EXITING` means everything held is
  committed to working exits and no stop works beside them.
* **Follow-ups.**
  * ADD is a plain limit entry. Its fill is covered by resizing the existing children.
  * REDUCE and CLOSE are marketable limits: the bid (sell) or ask (buy) of a usable broker quote,
    moved by `max_price_deviation_pct` and rounded to the cent. With no usable quote nothing is
    sent and `EXIT_WITHOUT_QUOTE` opens; the broker-side stop stays the protection. They are
    refused if they exceed the ledger or the broker position. Before one is sent, it is netted
    (next section).
  * MODIFY amends the working stop or target to an absolute price, then confirms the new price at
    the broker.

## Exit netting

The broker's reduce-only flag is never relied on. Before a REDUCE or CLOSE is sent:

1. Every working entry or add order of the idea is cancelled. The cancel is confirmed: the order
   has left `open_orders()` and appears terminal in `completed_orders()`. Otherwise a later fill
   could reopen the position unprotected.
2. A CLOSE also cancels, and confirms, every earlier resting REDUCE / CLOSE of the idea.
3. `sync()` applies anything that filled meanwhile. If the position changed, nothing is sent.
4. The protective children are resized to `position − other working exits − this exit`, or
   cancelled when that is zero. Each change is confirmed at the broker.
5. `sync()` runs again. A cancel or resize that lost the race with a fill changes the position, and
   then nothing is sent.
6. Account check, from broker truth: every working order on the exit side of the instrument
   (one-cancels-other siblings count once, at the larger remaining), plus exits of unknown
   outcome, plus this exit, must fit inside the broker position.

Any step that cannot be confirmed within `ExecutionConfig.confirm_attempts` broker reads ends the
intent `REFUSED` with `EXIT_NOT_NETTED` and an open exception. Nothing is sent, and the children
are sized back to the position. The same netting runs before a recovery resend of an exit. The
working exit quantity therefore never exceeds the position.

## Halt

While the halt file exists:

* Every `sync()` cancels every working entry and add order (risk-reducing) and immediately applies
  any fill that beat the cancel.
* Protective orders are still resized and cancelled to match the position. Sizing protection never
  adds exposure.
* Nothing that could create or increase exposure is submitted or amended: OPEN, ADD and recovery
  resends end `HALTED`.
* As before, REDUCE, CLOSE and MODIFY also end `HALTED`.

## Intent state machine

```
           record (write-ahead, one transaction with its order rows)
                 |
             RECORDED --halt file--> HALTED
                 | submit
     ack lost /  |  ack
     error       v
 UNRESOLVED <--+ WORKING --fills--> FILLED
     |         |    \--broker--> CANCELLED (may carry a partial fill) | REJECTED
     | lookup by order_ref (open_orders + executions + completed_orders)
     +-- found ------------------> adopt broker state (WORKING / FILLED / REJECTED / CANCELLED)
     +-- not yet settled --------> stays UNRESOLVED, looked up again later
     +-- older than horizon -----> stays UNRESOLVED + open UNKNOWN_OUTCOME_BEYOND_LOOKUP_HORIZON
     +-- positive no trace, -----> journal NO_TRACE_AT_BROKER, resubmit same ref (budget 2)
     |   exit, or entry/add that passes the entry checks again now
     +-- positive no trace, -----> REFUSED + open STALE_AFTER_UNKNOWN_OUTCOME exception
     |   entry/add that fails them (stale source, old intent, quote, collar, caps)
 definite submit error ---------> REJECTED (SUBMIT_ERROR), never resent
     +-- lookup failed / budget -> stays UNRESOLVED (reconcile: INTENT_STUCK_UNRESOLVED)
 REFUSED: precondition failed before anything was recorded for the broker, or a stale entry/add,
          or an exit that could not be netted (EXIT_NOT_NETTED) or priced (EXIT_WITHOUT_QUOTE)
 APPLIED: a MODIFY whose new prices were seen at the broker
 RECORDED --halt file--> HALTED, for every action
 RECORDED exit --netting not confirmed--> REFUSED (EXIT_NOT_NETTED), nothing sent
 found: entry gone from open orders and history, but its child is active --> FILLED
          + open ENTRY_FILL_NOT_IN_EXECUTIONS (the ledger cannot apply fills it cannot see)
```

A RECORDED, UNRESOLVED or WORKING exit counts against the idea's cover (see Protection). A
`RECOVERY_LOOKUP` row is journaled when an intent's state or attempt count changes, not on every
cycle.

A restart leaves an intent in `RECORDED`, because nobody can tell whether the request left the
process. `recover()` treats it exactly like `UNRESOLVED`.

## The never-resend rule

An order whose outcome is unknown is never sent again on assumption. The coordinator first looks the
order up at the broker by `order_ref` in `open_orders()`, `executions()` and `completed_orders()`
(IBKR `reqCompletedOrders`):

* If it finds the order, it adopts it (`ADOPTED_FROM_BROKER`). An order the broker rejected or
  cancelled is visible only in completed orders; it is adopted as `REJECTED` / `CANCELLED` and not
  resent.
* Only when all three show no trace of the ref does it resubmit with the same ref. The journal
  records which refs were checked (`NO_TRACE_AT_BROKER`). An entry or add is resubmitted only if
  its source post still passes `validator.source_is_fresh` at that moment (the publication time is
  stored on the intent). Otherwise it ends `REFUSED` with reason and open exception
  `STALE_AFTER_UNKNOWN_OUTCOME`. A reduce-only exit is resubmitted regardless of age.
* "No trace" is concluded only once `ExecutionConfig.settle_seconds` (default 10 s) have passed
  since the request: before that the intent stays `UNRESOLVED` (`LOOKUP_NOT_SETTLED`) and is looked
  up again on a later `recover()`, because open orders can lag a fresh submission.
* Before any resend of an entry or add, the entry checks run again at that moment: source
  freshness, `max_intent_age_seconds` (default 120 s), broker-quote sanity, the collar, the exposure
  caps, and the halt in `_submit`. Any failure ends it `REFUSED` with reasons
  `[STALE_AFTER_UNKNOWN_OUTCOME, <specific reason>]` and an open exception; nothing is sent. The
  redelivery path (`PRIOR_INTENT`) goes through the same `_resolve`.
* An unknown outcome older than `lookup_horizon_seconds` (default 6 h; IBKR executions cover the
  current day) can never be proven absent: it stays `UNRESOLVED` with an open
  `UNKNOWN_OUTCOME_BEYOND_LOOKUP_HORIZON` exception for a human, and is never resent.
* A definite submit error (a `BrokerError` that is not `AckLost`) is a known outcome: the intent
  ends `REJECTED` with reason `SUBMIT_ERROR` and is never resent.
* A lookup that fails leaves the intent `UNRESOLVED`.

Duplicate delivery of a post revision is stopped twice:

1. The replay's once-only ledger stops it first.
2. If a crash happened after the broker accepted the order but before the replay committed, the
   existing intent stops it. In that case `BrokerEventReplay` marks the action `INTENT_ALREADY_ON_RECORD`
   and the coordinator returns that intent's outcome. It never places a second order.

`HALTED` and `REFUSED` intents are terminal. A redelivered post does not retry them.

## Integration seam

`PaperEventReplay._execute_group(...)` is the seam. It is a no-op in the base class and is called
only for a group whose every action the gateway approved. The default mock path is unchanged.
`execution.replay.BrokerEventReplay` overrides three things:

* `_execute_group`, to send each action to the coordinator, in order. In a multi-action group, the
  first member that does not end `EXECUTED` stops the group. Every later member is `HOLD`
  (`PARTIAL_GROUP`) and never sent, and a `PARTIAL_GROUP` exception is opened;
* `_load_ideas`, so the gateway sees the follower ledger;
* `process`, to run `recover()` first, so a stop that fired between posts is already applied.

`SignalPipeline` (Sprint 1) is not wired. It supports neither ADD nor MODIFY nor revisions.

## Reconciliation

`reconcile.reconcile(store, broker, now=...)` is read-only. It returns these kinds of discrepancy:

* `UNEXPLAINED_BROKER_POSITION`
* `LEDGER_POSITION_MISSING_AT_BROKER`
* `QUANTITY_MISMATCH`
* `POSITION_WITHOUT_PROTECTIVE_STOP`
* `ORPHAN_WORKING_ORDER` (unknown ref, a child of a flat idea, an oversized child, or a working
  order under a halted, refused or rejected intent)
* `UNAPPLIED_EXECUTION`
* `INTENT_STUCK_UNRESOLVED`
* `BROKER_UNREADABLE`
* `WORKING_ENTRY_ON_CLOSED_IDEA` (an entry or add still working while its idea is closed,
  reversed, or has a CLOSE in flight)
* `PROTECTIVE_STOP_INVALID` (the stop is not a STP, is on the wrong side, or its price is not
  below the average entry for a long, or above it for a short)
* `PARTIAL_GROUP` (an open `PARTIAL_GROUP` exception)

The stop is expected to cover the idea's cover, which is its quantity less the working exits. A
position fully committed to working exits is reported `POSITION_WITHOUT_PROTECTIVE_STOP`.

A discrepancy counts as explained when an open exception record covers it. `report.clean` is
`False` while any discrepancy is unexplained, and also while a safety discrepancy exists even if it
is explained: `POSITION_WITHOUT_PROTECTIVE_STOP`, `PROTECTIVE_STOP_INVALID`,
`WORKING_ENTRY_ON_CLOSED_IDEA` or `PARTIAL_GROUP`. These are listed in `report.blocking`.

## The simulated broker

`SimBroker` runs in strict mode by default, and the coordinator, replay, service and reconciliation
tests, the mock service and both demos all use it. In strict mode:

* there is no reduce-only clamp, so an oversized exit really reverses the position;
* children keep their submitted quantity, and only a sibling's complete fill cancels them;
* `executions()` covers the current UTC day;
* `cancel` and `modify` pass through a pending state. They take effect after `pending_seconds`, at
  the next broker interaction. Until then the order can still fill; `script_next_cancel` /
  `script_next_modify("FILL_FIRST")` make the fill win;
* `open_orders_lag_seconds` can hide fresh orders.

`strict=False` keeps the old, kinder model for the SimBroker's own unit tests.

## Plugging in a real broker

Implement `broker.Broker`:

* `resolve` maps to `reqContractDetails`, which gives the conId.
* `quote` maps to `reqMktData`, with the market-data type mapped to `QuoteQuality`.
* `submit` places the parent with `transmit=False` and the last child with `transmit=True`. The
  children use `parentId` and share an OCA group. `orderRef` is set on all of them.
* Exits are plain LMT orders. The coordinator nets them itself and does not depend on any
  broker-side reduce-only behaviour.
* `modify` re-places the order under the same orderId, which the adapter finds through its open
  orders by `orderRef`.
* `cancel` maps to `cancelOrder`.
* `open_orders` maps to `reqAllOpenOrders`.
* `executions` maps to `reqExecutions`.
* `completed_orders` maps to `reqCompletedOrders`.
* `positions` maps to `reqPositions`.

Raise `AckLost` whenever the outcome of a request is unknown, for example on a timeout or a
disconnect after sending. Return `UNKNOWN` rather than guessing a status. The coordinator, ledger,
recovery and reconciliation then need no change. `completed_orders` maps to `reqCompletedOrders`
(apiOnly). One known gap for that adapter: `open_orders()` can lag a fresh submission, so the adapter
should settle before answering.

Run the demonstration with `PYTHONPATH=src python scripts/run_mock_lifecycle.py`.
