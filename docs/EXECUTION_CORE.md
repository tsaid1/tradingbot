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
    and `max_aggregate_exposure_usd`. Both are counted across ideas at cost, plus the unfilled part
    of working entries.
  * the account identity, when `ExecutionConfig.expected_account` is set.
  * the halt file, checked right before every submit or amend. It defaults to
    `~/.local/state/tradingbot/HALT` and can be overridden with `TRADINGBOT_HALT_FILE`.
* **Idempotency key.** `order_ref = "TB-" + sha256(source_id, revision, action_id)[:20]`. Protective
  children are `<ref>.STP` and `<ref>.TGT`. The broker stores the ref and returns it on every
  order, fill and open-order row (IBKR `Order.orderRef`).
* **Follower ledger** (`exec_positions`). It moves only in `ExecutionStore.apply_fill`, once per
  broker `exec_id`. Stop triggers, partial fills and fills from a recovered order all arrive through
  the same `sync()` path. The `stop_price` on an idea changes only after the amendment is seen at
  the broker.
* **Protection is confirmed, not assumed.** After every fill, `sync()` resizes the idea's working
  stop and target to the ledger quantity, or cancels them when the idea is flat. It then reads
  `open_orders()`. If no working STP covers exactly the held quantity, it opens an
  `UNPROTECTED_POSITION` exception record and the outcome becomes `EXCEPTION`, not `EXECUTED`.
* **Follow-ups.**
  * ADD is a plain limit entry. Its fill grows the existing children.
  * REDUCE and CLOSE are reduce-only market orders. They are refused if they exceed either the
    ledger or the broker position, so they can never open or reverse a position. Their fills shrink
    or cancel the children.
  * MODIFY amends the working stop or target to an absolute price, then confirms the new price at
    the broker.

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
     +-- positive no trace, -----> journal NO_TRACE_AT_BROKER, resubmit same ref (budget 2)
     |   exit, or entry/add whose source is still fresh
     +-- positive no trace, -----> REFUSED + open STALE_AFTER_UNKNOWN_OUTCOME exception
     |   entry/add whose source is stale
     +-- lookup failed / budget -> stays UNRESOLVED (reconcile: INTENT_STUCK_UNRESOLVED)
 REFUSED: precondition failed before anything was recorded for the broker, or a stale entry/add
 APPLIED: a MODIFY whose new prices were seen at the broker
```

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

* `_execute_group`, to send each action to the coordinator;
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

A discrepancy counts as explained when an open exception record covers it.

## Plugging in a real broker

Implement `broker.Broker`:

* `resolve` maps to `reqContractDetails`, which gives the conId.
* `quote` maps to `reqMktData`, with the market-data type mapped to `QuoteQuality`.
* `submit` places the parent with `transmit=False` and the last child with `transmit=True`. The
  children use `parentId` and share an OCA group. `orderRef` is set on all of them.
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
