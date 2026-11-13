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
    collar is a rejection boundary only (owner, 12 November 2026): an explicit entry or add is a
    limit at the publisher's stated entry price itself, snapped to the tick never past it (a buy
    limit down, a sell limit up), so a buy whose ask is above the stated price rests at it; it is
    never lifted to the collar. An entry stated as a range reaches the action as its first entry
    value (the lower bound of "88.38-88.42"), so the buy limit is never above the stated maximum.
    An anchored entry sends the gateway's limit, the fresh executable quote side inside the
    owner's anchor limits.
  * **The life of an entry order** (lead's decision, 12 November 2026): an unfilled entry never
    outlives the validity of its signal, and never outlives the publisher's own exit. An entry or
    add is sent at the stated price or the anchored quote and may rest. Every normal `sync()`
    (each service cycle, and the first after a restart) cancels, and confirms through the same
    cancel-and-confirm path as the halt and the pre-exit netting, any OPEN or ADD still working
    once its source post is older than `max_intent_age_seconds` (for an anchored entry the
    smaller of that and the anchor's `max_signal_age_seconds`), measured from the post's
    publication with the service clock; at exactly the limit it is still valid. It ends
    `CANCELLED` with `ENTRY_EXPIRED_UNFILLED`, journaled. A partial fill keeps its position and
    its protection sized to what filled; only the remainder is cancelled. It is never resent or
    re-priced. An unconfirmed cancel opens `ENTRY_REMAINDER_NOT_CANCELLED` (the instrument is
    blocked, the alert fires) and is retried at the next sync. A follow-up on an idea whose entry
    is still working and wholly unfilled (the ledger holds nothing yet, so the gateway alone would
    hold it with `REFERENCED_ACTIVE_IDEA_NOT_FOUND`) is seen by the broker-backed replay before the
    gateway: the entry is cancelled and confirmed first; a CLOSE or REDUCE is then recorded
    `CANCELLED` with `ENTRY_CANCELLED_BEFORE_FILL` (nothing else is sent); any other follow-up (a
    stop or target change, an add) is held (`ENTRY_CANCELLED_BEFORE_FILL`,
    `FOLLOW_UP_ON_UNFILLED_ENTRY`); a resting bracket is never amended. If the entry filled before
    its cancel took effect, the follow-up is gated on what is held, as before; after a partial fill
    a CLOSE cancels the remainder and closes what is held (pre-exit netting). Real-broker binding:
    before any resting entry is possible at a real broker, the expiry must also be enforced
    broker-side (the order module today sends day orders); that is outside this change.
  * exposure caps: `MockPolicy.max_instrument_exposure_usd`, which defaults to `max_notional_usd`,
    and `max_aggregate_exposure_usd`. Per contract the larger of the ledger's and the broker's
    position counts (a broker-only position counts too), valued at quantity x the usable quote's
    midpoint x the contract multiplier, at cost when there is none. Every entry or add that is not
    terminal (recorded, of unknown outcome, PreSubmitted or working) adds its full intended
    quantity at its limit x multiplier, less what it has already filled into the ledger. The
    per-idea `max_notional_usd` is enforced again with the multiplier (`NOTIONAL_LIMIT`), which
    the gateway cannot see. Unreadable broker positions refuse the entry.
  * no open exception in the instrument of a blocking kind (`INSTRUMENT_BLOCKED_<KIND>`):
    `LEDGER_REVERSAL`, `ENTRY_REMAINDER_NOT_CANCELLED`, `UNPROTECTED_POSITION`,
    `PROTECTION_UNVERIFIED`, `ENTRY_STATE_UNKNOWN`, `OPERATOR_SETTLEMENT_UNVERIFIED`; and no entry
    or add of unknown outcome in the instrument (`UNRESOLVED_ENTRY_IN_INSTRUMENT`).
  * no open `BROKER_STATE_UNKNOWN` (see Broker reads below) or `BROKER_RECONNECT_PENDING` (the
    runtime service's session policy): OPEN and ADD refuse with `BROKER_UNREADABLE`.
  * one net position per contract: an OPEN on the opposite side of an open or pending idea in the
    same contract is held by `BrokerEventReplay` (`HOLD`, `OPPOSITE_IDEA_IN_SAME_CONTRACT`) and
    refused by the coordinator. A publisher reversal is a CLOSE followed by an OPEN.
  * the contract's tick and size rules (see Instruments).
  * the account identity, when `ExecutionConfig.expected_account` is set (the runtime service
    requires it).
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
* **Broker reads (review F1).** `sync()` reads executions, open orders, completed orders and
  positions. If any read fails (there, in a lookup, a protection check or an exposure check), every
  open position's protection becomes `UNKNOWN`, a `BROKER_STATE_UNKNOWN` exception opens, and
  OPEN / ADD are refused until a full sync succeeds, which resolves it. `recover()` raises
  `BrokerUnreadable` instead of swallowing a failed sync. The service's broker probe exercises all
  four reads before it touches the source.
* **Protection is confirmed, not assumed.** After every fill, `sync()` sizes the idea's working
  stop and target to the *cover*: the ledger quantity less every non-terminal REDUCE / CLOSE
  (recorded, unknown outcome or working, less what it filled). It cancels them when the cover is
  zero or the idea is flat. Every applied fill resets the idea's protection to `UNKNOWN`. It
  becomes `CONFIRMED` only when (1) a successful `open_orders()` read shows a working (not
  PendingCancel) STP on the protective side covering exactly the cover at exactly the ledger's
  stop price, and (2) a successful `executions()` read made after it shows no fill on the idea's
  orders that the ledger has not applied. A PreSubmitted stop counts only once its parent's own
  fills are applied. A read that fails opens `PROTECTION_UNVERIFIED`. A stop that is missing,
  short, on the wrong side or at another price (`STOP_PRICE_MISMATCH` in the detail) opens
  `UNPROTECTED_POSITION`. Either way the outcome is `EXCEPTION`, not `EXECUTED`, and new exposure
  in the instrument is blocked. `EXITING` means everything held is committed to working exits and
  no stop works beside them.
* **Cover follows the broker's position, asymmetrically (review N1).** Upward, a protective
  order is never placed or sized above what the broker shows held in the contract (each idea's
  share of the broker's position on its side, less the working exits), so cover grows only once
  the ledger and the broker both show it; when the positions cannot be read nothing is sized up.
  Downward, cover is never reduced below the ledger, or cancelled, on a position read alone: only
  as far as qualifying executions explain (review P1: exit side for the idea, in the contract,
  not applied by the ledger, executed by the broker's own timestamp after the idea's last fill or
  settlement, never attributed before, each attributed once to one idea and journaled
  `EXECUTION_ATTRIBUTED`, and together capped at the ledger/broker gap), or to the lower position
  once `exit_no_trace_lookups` consecutive successful reads spread over
  `settle_seconds` all show it (a failed read restarts that run, and is never a quantity). Until
  then the working order stays as it is. Whenever the broker's position differs from the ledger,
  in either direction, `UNPROTECTED_POSITION` opens with `BROKER_POSITION_DIFFERS`, it is alerted,
  and new exposure in the contract is blocked. A broker holding more than the ledger is covered
  only for the ledger's quantity; the excess is reconciliation's unexplained `QUANTITY_MISMATCH` /
  `UNEXPLAINED_BROKER_POSITION` (a stop the system invented for it would be an order no one
  approved). The trade-off: an oversized stop risks a small reversal if it triggers before the
  discrepancy is resolved; a missing stop risks an unbounded loss on what is really held; so cover
  is reduced only on corroborated evidence, and an operator is alerted immediately in either case.
* **Stop-outs (review F2).** Once a protective stop has filled for an idea, or the idea is closed,
  every working remainder of its entry and add orders is cancelled and the cancel confirmed
  (`ENTRY_REMAINDER_CANCELLED`). An unconfirmed cancel opens `ENTRY_REMAINDER_NOT_CANCELLED`, which
  blocks new exposure in the instrument. Further entry fills resize protection to the full position
  before it can be `CONFIRMED`; a resize that cannot be confirmed leaves `UNPROTECTED_POSITION`.
* **An entry's fill is never inferred from a child.** At IBKR a bracket child is PreSubmitted before
  its parent fills and may stay PreSubmitted afterwards (stops held broker-side). An entry's state is
  decided only from its own row in open or completed orders and its own executions. When those are
  missing or incomplete the intent is `UNRESOLVED` with an `ENTRY_STATE_UNKNOWN` exception and new
  exposure in the instrument is blocked; it is never adopted FILLED or CANCELLED on inference. An
  order that leaves the working set takes its final state from its completed-orders row.
* **Protective time in force.** `ExecutionConfig.protective_tif` (default `GTC`) sets the TIF of
  stop and target children. With `DAY` the stop expires at the session end and reconciliation then
  reports the open position `POSITION_WITHOUT_PROTECTIVE_STOP` until a human acts.
* **Follow-ups.**
  * ADD is a plain limit entry. Its fill is covered by resizing the existing children.
  * REDUCE and CLOSE are marketable limits: the bid (sell) or ask (buy) of a usable broker quote,
    moved by `max_price_deviation_pct` and snapped to the contract tick inside the collar (exits
    are unchanged by the entry-side correction of 12 November 2026). With no usable quote nothing is
    sent and `EXIT_WITHOUT_QUOTE` opens; the broker-side stop stays the protection. They are
    refused if they exceed the ledger or the broker position. Before one is sent, it is netted
    (next section). Two options, off by default and used by the paper experiment's stage 3
    (docs/IBKR_PAPER_ORDERS.md, "Stage 3: the close"): `exit_price_policy="TOUCH"` prices the
    exit at the touch itself (a sell at the bid snapped up, a buy at the ask snapped down), and
    `exit_validity_seconds` bounds its life: `sync` cancels an exit still working after that long,
    confirms the cancel, ends it `EXIT_UNFILLED_CANCELLED` and places the stop the netting cancelled
    again (once, a new order `<entry>.STP<n>`); an unconfirmed cancel opens
    `EXIT_CANCEL_UNCONFIRMED` and places nothing, and an exit of unknown outcome is never resent
    (`EXIT_OUTCOME_UNKNOWN`).
  * MODIFY amends the working stop or target to an absolute price, then confirms the new price at
    the broker.

## Instruments

One execution core serves stocks, ETFs, futures and options; asset-specific behaviour belongs in
policies, not in separate engines. An instrument is identified by `ContractRef.contract_key`
(security type, symbol, expiry or contract month, strike, right and broker contract id), never by
symbol alone: two months of one future are two instruments, and a stock and a future sharing a
symbol never net. Intents, ledger positions and order rows persist the contract and its key;
positions, caps, netting, exit checks, reconciliation and the journal use it.

`ContractRef` carries optional fields whose defaults describe a stock, so a stock behaves exactly
as before: `multiplier` 1, `min_tick` 0.01, `size_increment` 1, `expiry` / `strike` / `right` /
`local_symbol` None. Notional and exposure are quantity x price x multiplier. Before an order is
built, stop and target prices must be multiples of `min_tick` (`STOP_PRICE_OFF_TICK`,
`TARGET_PRICE_OFF_TICK`) and quantities multiples of `size_increment` (`QUANTITY_OFF_INCREMENT`).
Those are refused, never rounded, so a protective stop is never loosened. Entry and exit limits
are snapped, a buy limit down and a sell limit up (an entry never past its stated price, an exit
always inside the collar). Sessions, rollover,
margin and option chains are not implemented beyond what the multi-asset mapping states.
`BrokerPosition.avg_cost` is per unit, before the multiplier. A future, option, CFD or spot
commodity reaches the coordinator only through an approved version-2 mapping entry. The gateway
then approves with an `ExecutionPlan`, and the coordinator follows it: units per signal, the entry
level in execution prices, and stop and target in their own series. A REFERENCE_TRIGGER plan
produces conditional exits (`OrderIntent.condition`) and an optional backstop (role `BACKSTOP`,
ref `<entry>.BKS`). A follow-up executes only in its idea's own contract. See
`docs/MULTI_ASSET_MAPPING.md`.

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
     +-- submitted entry/add, ---> stays UNRESOLVED, looked up every cycle; after the settle
     |   no trace                  + open UNKNOWN_OUTCOME_ENTRY_NOT_RESENT, instrument blocked
     +-- submitted exit, no -----> journal NO_TRACE_AT_BROKER, resubmit same ref (budget 2),
     |   trace in N lookups        re-priced first if older than max_exit_intent_age_seconds
     |   over the settle
     +-- never submitted --------> sent once, if an entry/add passes the entry checks again now;
     |   (attempts 0)              else REFUSED + open STALE_AFTER_UNKNOWN_OUTCOME
     +-- entry's own row/fills --> stays UNRESOLVED + open ENTRY_STATE_UNKNOWN
         missing or incomplete
 definite submit error ---------> REJECTED (SUBMIT_ERROR), never resent
     +-- lookup failed / budget -> stays UNRESOLVED (reconcile: INTENT_STUCK_UNRESOLVED)
 REFUSED: precondition failed before anything was recorded for the broker, or a stale entry/add,
          or an exit that could not be netted (EXIT_NOT_NETTED) or priced (EXIT_WITHOUT_QUOTE)
 APPLIED: a MODIFY whose new prices were seen at the broker
 RECORDED --halt file--> HALTED, for every action
 RECORDED exit --netting not confirmed--> REFUSED (EXIT_NOT_NETTED), nothing sent
 WORKING --left open orders, no completed row, not fully filled--> UNRESOLVED + ENTRY_STATE_UNKNOWN
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
* **A submitted entry or add of unknown outcome is never resent automatically (review F5).**
  Duplicate prevention must not depend on a fixed wait being longer than the broker's visibility
  lag. The intent stays `UNRESOLVED`, is looked up on every cycle and adopted if it appears. Once
  `settle_seconds` have passed it opens `UNKNOWN_OUTCOME_ENTRY_NOT_RESENT` for a human, and new
  exposure in the instrument is refused (`UNRESOLVED_ENTRY_IN_INSTRUMENT`) while it is unresolved.
  If it appears later it is adopted and the exception is resolved (`UNKNOWN_OUTCOME_SETTLED`).
* **An exit** is resubmitted with the same ref only after `settle_seconds` and at least
  `exit_no_trace_lookups` (minimum 3) consecutive lookups, spread over at least `settle_seconds`,
  in which open orders, executions and completed orders all show no trace (`NO_TRACE_LOOKUP`,
  then `NO_TRACE_AT_BROKER`). A failed lookup restarts the count. An exit older than
  `max_exit_intent_age_seconds` is re-priced from a fresh quote first (`EXIT_REPRICED`); with no
  usable quote it ends `REFUSED` with an open `EXIT_WITHOUT_QUOTE`. It is never sent at a stale
  price. The safety of an exit resend still depends on the broker showing a new order within
  the settle period: the IBKR adapter must confirm visibility.
* **An intent recorded but never submitted** (the process died between the write-ahead record and
  the request, `attempts` 0) is sent once its checks pass. An entry or add must still pass
  `validator.source_is_fresh` (the publication time is stored on the intent); otherwise it ends
  `REFUSED` with reason and open exception `STALE_AFTER_UNKNOWN_OUTCOME`.
* Before such a send of an entry or add, the entry checks run again at that moment: source
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

## Operator settlement

`execution/settlement.py` lets a human end what the coordinator never ends by itself (an entry or
add of unknown outcome, above all one beyond the lookup horizon). `settle_intent` declares one
intent `NOT_AT_BROKER`, `CANCELLED` or `FILLED` (quantity, average price) after the operator
checked the broker. Its own call only reads and records: it sends, amends and cancels nothing,
not even protective upkeep, which the next normal sync does. It reads first
(`sync(record_only=True)`), then refuses a declaration the broker
contradicts (the order or its fills visible, a fill outside the limit, off tick or increment, or a
resulting follower position the broker's cannot equal, allowing only for other intents of unknown
outcome in the contract). A declared fill goes through `apply_fill` (`exec_id = OPERATOR:<request>`)
so protection must then be confirmed at the broker like any fill. The settlement opens
`OPERATOR_SETTLEMENT_UNVERIFIED`, a blocking kind that only `lift_verified_blocks` resolves, never
in the call that records the settlement, after a full successful sync and a reconciliation with no
blocking discrepancy in that contract. After `NOT_AT_BROKER` or `CANCELLED` it also needs the
exit-resend evidence rule after the settlement (settle period, `exit_no_trace_lookups` no-trace
lookups spread over it). Broker evidence for such an intent later opens
`SETTLED_INTENT_CONTRADICTED` (blocking, a safety discrepancy in reconciliation, CLEAR-class;
cleared only once the contradicting order is terminal at the broker and the contract reconciles
clean).
`clear_exception` (a `LEDGER_REVERSAL`, verified the same way) and `acknowledge_exception`
(records that block nothing) complete the set; `operator_catalog.py` classes every exception kind.
Every action, applied or refused, is journaled with its before and after state.

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
* `PROTECTIVE_STOP_INVALID` (the stop is not a STP, is on the wrong side, or, when a quote is
  available, is not below the bid for a long or above the ask for a short; break-even and
  profit-locking stops are valid)
* `STOP_PRICE_MISMATCH` (the working stop's price differs from the ledger's confirmed stop)
* `PARTIAL_GROUP` (an open `PARTIAL_GROUP` exception)

Quantities are compared per contract (`Discrepancy.contract_key`), never per symbol.

The stop is expected to cover the idea's cover, which is its quantity less the working exits. A
position fully committed to working exits is reported `POSITION_WITHOUT_PROTECTIVE_STOP`.

A discrepancy counts as explained when an open exception record covers it. `report.clean` is
`False` while any discrepancy is unexplained, and also while a safety discrepancy exists even if it
is explained: `POSITION_WITHOUT_PROTECTIVE_STOP`, `PROTECTIVE_STOP_INVALID`,
`STOP_PRICE_MISMATCH`, `WORKING_ENTRY_ON_CLOSED_IDEA` or `PARTIAL_GROUP`. These are listed in `report.blocking`.

## The simulated broker

`SimBroker` runs in strict mode by default, and the coordinator, replay, service and reconciliation
tests, the mock service and both demos all use it. In strict mode:

* there is no reduce-only clamp, so an oversized exit really reverses the position;
* children keep their submitted quantity, and only a sibling's complete fill cancels them;
* `executions()` covers the current UTC day;
* `cancel` and `modify` pass through a pending state. They take effect after `pending_seconds`, at
  the next broker interaction. Until then the order can still fill; `script_next_cancel` /
  `script_next_modify("FILL_FIRST")` make the fill win;
* `open_orders_lag_seconds` can hide fresh orders;
* bracket children are PreSubmitted until the parent fills; with `children_stay_presubmitted` they
  stay PreSubmitted while working afterwards (and still trigger);
* positions and quotes are keyed by contract; `register_contract` makes a derivative resolvable;
  orders off the contract's tick or size increment are refused; `expire_day_orders()` ends a
  session.

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
should settle before answering. Entries are never resent after an unknown outcome; exits are, after
the lookups described above. Fill `ContractRef.multiplier`, `min_tick` (IBKR `minTick`),
`size_increment`, `expiry` (`lastTradeDateOrContractMonth`), `strike`, `right` and `con_id` from
`reqContractDetails`, and report `BrokerPosition.avg_cost` per unit (IBKR multiplies it).

Price conditions are optional (`broker.ConditionalOrders`). A broker that offers them implements
`supports_price_conditions()` (True), `reference_quote(contract)` (a quote of an index, spot or
other reference contract) and `modify_condition(order_ref, trigger_price=...)`. It also echoes
`BrokerOrder.condition` on open orders. Without these, REFERENCE_TRIGGER entries are refused
before submission, reference quotes read as UNAVAILABLE, and protection is never confirmed for such
a position. `PriceCondition` maps to IBKR `PriceCondition` (conId, exchange, isMore, price) on an
order whose type is the condition's `released_order_type`.

Run the demonstration with `PYTHONPATH=src python scripts/run_mock_lifecycle.py`.
