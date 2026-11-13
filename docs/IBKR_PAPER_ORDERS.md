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

## What is disabled by default, and what would have to be true for one order to be sent

An order frame is a PLACE_ORDER (place, bracket child, amendment: id 3) or a CANCEL_ORDER (id 4).
Global cancel (58), option exercise (21), configuration update (109) and financial-advisor writes
(18, 19) are refused on every path. On the shipped tree, with nothing written by hand, **no path
below can send one**.

| Way an order frame could reach the wire | Shipped default | What must ALL be true to send one | Tests that pin it |
|---|---|---|---|
| The runtime service (`scripts/run_service.py`, `tb_operator.py`, the watchdog, the dashboard) | No order path at all: `mode = "mock"` or `"shadow"` only (shadow reads real quotes through the read-only adapter; orders and fills stay simulated), the SimBroker | Impossible: the runtime accepts only those two simulated modes, has no key that selects a broker, never imports `tradingbot.ibkr_orders`, and `build_service` builds only `SimBroker` | `test_the_runtime_accepts_only_its_simulated_modes`, `test_the_runtime_config_has_no_key_that_selects_another_broker`, `test_the_runtime_builds_the_simulated_broker_only`, `test_runtime_execution_core_and_read_only_adapter_do_not_load_the_order_package` |
| The read-only adapter (`IbkrSession`, `ibkr_paper_check.py`, `ibkr_contract_discovery.py`, dashboard page 7) | Cannot: ids 3 and 4 are not in its deny-by-default allowlist, the order methods are denied | Impossible: the allowlist is exactly the 12 reviewed reads, unchanged by the merge | `test_read_only_allowlist_is_exactly_the_reviewed_read_set`, `test_read_only_allowlist_by_message_id_with_the_sdk`, `test_read_only_session_still_cannot_send_any_order_message` |
| Any other module importing the order package | None does | Only `tradingbot.ibkr_orders` and the two `scripts/ibkr_paper_*` scripts import it; no shipped file names it otherwise | `test_only_the_order_package_and_its_two_scripts_import_it`, `test_no_shipped_non_test_file_names_the_order_package_dynamically` |
| `scripts/ibkr_paper_init_journal.py` | Never sends | Offline: opens no socket | `test_init_script_is_offline_and_runs_once` |
| `scripts/ibkr_paper_experiment.py` without a stage | Does nothing | One explicit stage argument; no flag skips or repeats a stage | `test_the_script_does_nothing_without_one_explicit_stage`, `test_the_script_has_no_flag_to_skip_or_repeat` |
| `PaperOrderSession._send_place` (stage 2 bracket; stage 3 entry, stop, amendment, close) | Unreachable | 1. a private `~/.config/tradingbot/ibkr/paper_orders.toml` (mode 0600, owner-only) written by hand with `orders = "paper-test-1"` (the repo ships none and no template); 2. `state_dir` an existing private directory; 3. the journal created by hand with the init script; 4. paper port, loopback, a `D…` account, `tradingMode=p` and a listener owned by `tws_os_user`; 5. TWS's own `managedAccounts` exactly that account; 6. TWS reports Read-Only API **OFF**, localhost-only ON, the configured port; 7. the TLT contract pinned; 8. the stage's preconditions (the previous stage passed, this one never passed, the journal at the stage's starting count; stage 1 passed only on a LIVE or DELAYED quote with a bid and an ask, and stage 3 only after a LIVE one; stages 2 and 3 not after 15:00 New York and only with 60 minutes of today's liquid hours left, in a region time zone); 9. `order_guard` ALLOW on every cap, the clock (liquid hours in a region time zone only; a fixed-offset zone such as `EST` is refused) and the position cross-check; for the stage-3 entry, the price checked is the stated entry, the ask plus the owner's `stage3_entry_allowance_ticks` (0 to 5, private config only); 10. the journal line fsync'd; 11. a one-shot permit for exactly those bytes, re-checked at the wire | `test_no_orders_entry_no_state_dir_or_no_journal_means_no_order_capability`, `test_the_repo_ships_no_order_config_and_no_template_that_works_as_is`, `test_the_default_order_config_path_is_private_and_absent_by_default`, `test_order_session_needs_a_loaded_order_config`, `test_deleting_the_journal_never_gives_a_fresh_budget`, `test_order_mode_refuses_unless_read_only_is_explicitly_off`, `test_every_stage_refuses_without_its_predecessor`, `test_a_failed_stage_that_submitted_cannot_run_again`, `test_stage3_refuses_without_a_live_quote_in_stage1`, `test_stage3_refuses_when_the_journal_is_not_at_two`, `test_stage1_fails_and_stays_repeatable_without_a_live_or_delayed_quote`, `test_a_fixed_offset_time_zone_is_refused`, `test_the_stage3_entry_allowance_is_a_strict_integer_under_the_ceiling`, `test_the_repo_ships_no_stage3_entry_allowance_value`, the guard and wire tests below |
| `PaperOrderSession._send_cancel` (the core's cancels, stage 2, `abort`, a partial bracket's parent cancel) | Unreachable | Items 1, 2 and 4 to 7 above (a READY order session); an own, non-final order of this client; a one-shot permit. `abort` cancels a protective stop (or its parent) only when the position read and the experiment's own fills agree on flat. Never counted; works with the journal unknown | `test_abort_cancels_only_this_clients_orders_by_id`, `test_abort_never_cancels_the_stop_of_a_held_or_disputed_share`, `test_what_counts_toward_the_six` |
| Direct SDK calls (`placeOrder`, `cancelOrder`, `placeOrderProtoBuf`, `sendMsg`, a replayed frame, global cancel) | Refused | Impossible without a permit; the text-framed and global paths are denied | `test_denied_methods_raise_and_send_nothing`, `test_direct_sends_without_a_guard_permit_never_reach_the_wire`, `test_object_graph_of_order_session_reaches_no_unguarded_order_send` |
| The funded (live) account | Unreachable | Impossible: the config accepts only a `D…` paper id on a paper port, and TWS's own account list must be exactly that id (a live login reports `U…`) | `test_only_a_paper_account_id_can_be_configured`, `test_non_paper_accounts_refused_in_order_mode`, `test_live_ports_refused_in_order_mode`, `test_the_broker_account_list_must_be_exactly_the_paper_account` |
| Another program on the Mac while Read-Only API is OFF | The owner's standing TWS setting is Read-Only API ON | Out of scope for this code: the owner turns Read-Only OFF only for stages 1 to 3 and back ON afterwards | runbook below |

Order execution stays separate from merging: merging this code changes nothing above. One order
needs every hand-written item in the `_send_place` row **and** the owner running one stage command.

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

**Which position.** Two figures: (a) the broker's position read and (b) the experiment's own
position, derived from the broker's executions that belong to the experiment (an `orderRef` with
the experiment prefix, or this client's order ids in the journal or placed in this session):
today's, read with `reqExecutions` at connect and again before the first submission of every
session, plus every execution received live; for an order this session placed, the larger of its
execution total and its `orderStatus` filled quantity, since either can arrive first. It never
starts from the position read, so a read that lags across a restart is caught. It assumes the
account holds no TLT outside the experiment (otherwise every order is refused). The guard uses the more cautious of the two (the larger for a BUY, the
smaller for a SELL) and, when they differ, refuses everything with `POSITION_DISAGREEMENT` until
they agree again. A failed read is `POSITION_UNKNOWN`. The comparison made at connect is kept as
`position_check`.

**Exits never remove protection for a refused close.** Before the binding cancels or shrinks a
protective SELL stop (the execution core does this before sending a close), it runs the guard on
a closing SELL LMT for what is held (`dry_check_exit`). The price is the binding's own
**estimate** of the core's exit limit: the core prices the close first; the binding then takes a
**new** quote and prices it the same way. By default that is the bid less the policy collar,
snapped up to the tick (`exit_collar_pct` = `MockPolicy.max_price_deviation_pct`); stage 3 uses
the owner's touch price instead (`exit_at_touch=True` with the core's `exit_price_policy="TOUCH"`:
the fresh bid itself, snapped up). It is never the stop price, but it is not the core's number
either, and the core sends its own limit. The
other inputs are those of the send (fresh position, own fills, working orders without the stop,
the count), with the stop assumed gone, and the liquid-hours rule is judged at now +
`EXIT_HOURS_MARGIN_SECONDS` (60 s, `session.py`), so no stop is cancelled when the close could land
outside liquid hours. A binding with neither a collar nor the touch, or a quote that is missing,
malformed or (strict quotes) UNAVAILABLE, cancels
or shrinks no stop while anything is held. If that is refused, nothing is cancelled and the core sees a
cancel failure (`EXIT_WOULD_BE_REFUSED`), so the exit is not netted and the stop stays. If the
real close is nevertheless refused or fails (nothing sent) after the stop was cancelled, the
binding raises `ProtectionLost` ("POSITION_OPEN_PROTECTION_ABSENT", a `BrokerError`). The core
records it as a submit error with that text, and its next `sync` raises `UNPROTECTED_POSITION`;
stage 3 calls `sync` at once and stops.

**Residual.** The dry check cannot see the future. If the market falls between the core pricing the
exit and the send, the stop is already cancelled and the closing limit (priced off the earlier
bid) may rest unfilled: one share is then held with no stop while that limit works. Stage 3
bounds that: the core cancels the limit after `CLOSE_WAIT_SECONDS` (10 s), confirms the cancel at
the broker and places the stop again (see "Stage 3: the close" below).

**Which working orders.** Every row of the broker's open-orders answer whatever its status, plus
every order this session sent that is not final and not rejected even if no view shows it yet
(`SENT_NOT_VISIBLE`, an acknowledgement lost included), plus, after a restart, every journal
submission of an earlier session with no terminal evidence: a final status, a completed-orders row
(by order id or reference), executions covering its quantity, or absence from every lookup once
it is older than `JOURNAL_SETTLE_SECONDS` (120 s). So a second SELL is refused while an earlier
SELL is possibly live, and a second BUY likewise; a confirmed cancel frees it.

The cap is **9 submissions** (`caps.py`, `max_submissions`): every PLACE_ORDER frame counts, and
nothing else.

| Event | Counts | Why |
|---|---|---|
| Entry (BUY LMT), alone or as a bracket parent | 1 | a PLACE_ORDER frame |
| Protective child (SELL STP) of a bracket | 1 | its own PLACE_ORDER frame |
| Amendment (stop raise, price or quantity change) | 1 | re-placed under the same id |
| Closing SELL LMT | 1 | a PLACE_ORDER frame |
| The protective SELL STP placed again after an unfilled closing limit was cancelled (stage 3) | 1 | a NEW order (new id, new ref `<entry>.STP<n>`, no parent), its own PLACE_ORDER frame; not an amendment |
| An order the broker rejects, or never acknowledges | 1 | it was sent; it is never resent |
| Cancel (by the core, by stage 2, by `abort`, a partial bracket's parent cancel) | 0 | a CANCEL_ORDER frame only reduces risk; possible after the cap |
| A guard refusal, or any failure before the send | 0 | nothing was sent |

The journal line is fsync'd **before** the frame is sent, so a crash after the append counts the
order even if it never left. A bracket is checked whole: if it does not fit the rest of the budget,
nothing of it is sent (`SUBMISSION_CAP`); once 9 are used, every placement is
`EXPERIMENT_FINISHED`.

| Stage | Submissions | Starts from | Ends at |
|---|---|---|---|
| 0 `stage0-refusal` | 0 | 0 | 0 |
| 1 `stage1-lookups` | 0 | 0 | 0 |
| 2 `stage2-unfillable-bracket` | 2 (BUY LMT parent + SELL STP child) | 0 | 2 |
| 3 `stage3-lifecycle` | at most 7: entry + stop = 2, one stop raise = 1, then per close attempt (at most 2) the closing limit = 1 and, if it did not fill, the stop placed again = 1; every cancel = 0. A pass uses 4 (first close fills) or 6 (second close fills) | 2 | 6 or 8 on a pass; at most 9 |
| `abort` | 0 (cancels only) | any | unchanged |

Pinned by `test_what_counts_toward_the_six`, `test_a_partial_bracket_counts_both_orders_and_its_parent_cancel_counts_nothing`,
`test_a_bracket_that_does_not_fit_the_remaining_budget_sends_nothing`,
`test_full_lifecycle_within_the_six_submission_cap` (2, 3, 4),
`test_stages_0_to_3_then_nothing_more` (stage 3 steps 4, 5, 6),
`test_the_first_close_does_not_fill_and_the_second_does` (8),
`test_neither_close_fills_the_share_is_held_and_the_stop_confirmed` (9),
`test_an_unfilled_close_is_cancelled_and_the_stop_placed_again_as_one_new_order`,
`test_stage_names_and_per_stage_budget_are_the_runbooks` and
`test_submission_arithmetic_is_exactly_the_cap`.

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
- **What still cannot be prevented.** Replacing the journal and its head file together (or
  deleting both and initialising again) resets the budget. The broker-side count catches that
  only while the day's orders of the experiment are still visible in the broker's lookups (the
  same day at IBKR); after that it is silent, and nothing in software can tell. The owner controls the
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
- **Price conditions** (main's optional `ConditionalOrders`): `supports_price_conditions()` is
  False, so the core sends no reference-triggered order here, and `submit` refuses an
  `OrderIntent` with a `condition` (`PRICE_CONDITION_NOT_SUPPORTED`, nothing sent). There is no
  `reconnect` (`ReconnectableBroker`): a dropped session is a new process and a new session.
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
  are never upgraded. With `strict_quotes=True` (stage 3) the attempt is mapped by the shadow
  mode's own `execution.shadow_quotes.map_snapshot`: each side's age is measured on the adapter's
  monotonic clock (its initial image or latest value change), `bid_at` / `ask_at` are that age
  subtracted (rounded up) from a wall time taken before the read, or from the caller's
  `set_reference_time` when earlier (stage 3 sets it immediately before each core call, so the
  core's own `now` is never earlier), and `received_at` is the OLDER side. A side without a live
  tick time, a missing, placeholder or off-tick side, or a halt makes the side absent or the whole
  quote UNAVAILABLE, so the quote is never fresher or more live than the adapter judged it.
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

**Submission arithmetic:** stage 2 = 2 (BUY LMT parent + SELL STP child) and stage 3 at most 7
(bracket entry 2, one stop amendment 1, then two close attempts of a closing limit 1 plus the
stop placed again 1): 2 + 7 = 9 = the cap (asserted in a test). A clean stage 3 ends at exactly 6
(the first close fills) or 8 (the second does); `STAGE3_PASSED` accepts nothing else.

**Once, before stage 0** (owner, by hand):

1. Close any live TWS. Start the paper TWS as `tws_os_user`, logged in to the paper account.
2. `mkdir -m 700 <state_dir>` (outside Dropbox).
3. Write `~/.config/tradingbot/ibkr/paper_orders.toml`, mode 0600: every key of `paper.toml` plus
   `orders = "paper-test-1"` and `state_dir = "<state_dir>"`. Optionally, the owner's stage-3
   price choice `stage3_entry_allowance_ticks = N` (an integer 0 to 5; the ceiling is
   `MAX_STAGE3_ENTRY_ALLOWANCE_TICKS` in `caps.py`; absent means 0). The repo ships no value.
4. `python scripts/ibkr_paper_init_journal.py` (offline). It prints the journal path.
5. Plan the day: start stage 3 no later than 14:30 ET; the experiment ends flat the same day.
   The script itself refuses to START stage 2 or stage 3 at or after 15:00 New York time
   (`LATEST_START_NEW_YORK`, `experiment.py`): stage 3 holds a share, and stage 2 can leave one
   (an unexpected fill) that must be closed by hand before 16:00. After connecting and before
   any order, stages 2 and 3 also refuse unless TODAY's broker-reported liquid hours (the
   contract's `liquidHours`, the data the guard uses) last at least 60 more minutes
   (`MIN_MINUTES_TO_CLOSE`): an early-close day such as a 13:00 close is caught. If today's end
   cannot be determined (missing or unparseable hours, an unknown time zone, now outside them),
   the stage refuses. The stricter of the two rules applies.
6. **Required before stage 2 and again before stage 3:** run the full test suite locally WITH the
   SDK (`ibapi` installed, `TRADINGBOT_BLOCK_IBAPI` unset) at the exact commit you will run:
   `PYTHONPATH="$PWD/src" python -m pytest tests docs/research/g0_x_probe`. CI has no `ibapi`, so
   it skips every order-path test. Record the commit hash and the passed / skipped counts in a
   file beside the evidence (`<state_dir>/ibkr_paper_orders/paper-test-1.evidence/`). Stage 1
   prints this reminder.

| Stage | Owner does first | Command | Passes when | Count |
|---|---|---|---|---|
| 0 | TWS API settings: **Read-Only API ON**, localhost only ON, socket port = `port` | `python scripts/ibkr_paper_experiment.py stage0-refusal` | order mode refuses (`readOnlyApi=True`) and no order frame was sent | 0 |
| 1 | Turn **Read-Only API OFF**. Nothing else. | `python scripts/ibkr_paper_experiment.py stage1-lookups` | open orders, executions, completed orders and positions all complete; no order frame; the evidence lists which fields the rows carry (order id, `orderRef`, perm id; the account is checked on every row) and the quote labels. **Passes only when** the adapter labels a quote LIVE or DELAYED with a bid and an ask: up to `STAGE1_QUOTE_SAMPLES` (3) bounded samples in a row, each label and reason recorded, passing on the first usable one. Otherwise `STAGE1_QUOTE_NOT_USABLE` ("no order was sent; run stage 1 again"): stage 1 stays repeatable, stage 2 stays blocked | 0 |
| 2 | Check stage 1 evidence. Watch TWS. | `python scripts/ibkr_paper_experiment.py stage2-unfillable-bracket` | one bracket, BUY LMT at 90% of the bid with its SELL STP 1.00 lower, never filled; the parent was not answered before the child was sent; both seen in the read-back; both cancelled, gone from open orders and in completed orders | 2 |
| 3 | Check stage 2 evidence in full and **confirm in the TWS Audit Trail that the parent was held (not transmitted) until the child arrived**. The evidence field `parent_answered_before_child_sent` is one-sided: the child follows the parent by milliseconds, so False proves nothing. | `python scripts/ibkr_paper_experiment.py stage3-lifecycle` | Stage 1 must have recorded a **LIVE** quote (DELAYED no longer qualifies for stage 3). A LIVE quote with both sides at most 3 s old (read again, reads only, for up to 20 s if not); bracket entry filled, a BUY LMT at the **stated entry = the ask + `stage3_entry_allowance_ticks` min ticks** (0 when absent: exactly the ask, e.g. 80.73 for an ask of 80.73; with 3: 80.76). The core sends the stated price itself (the collar only rejects; it adds nothing), every cap (150 USD notional included) applies to it, and the evidence records `entry_price` {ask, allowance_ticks, tick, stated_entry}. With 0, an ask that ticks up before the order is live may leave the entry unfilled (`STAGE3_ENTRY_NOT_FILLED`, 4 of 9 spent); stop confirmed working, one stop raise (+0.10), then the bounded close ("Stage 3: the close" below) filled on attempt 1 or 2, reconciliation clean, exactly 6 or 8 used | 4 or 6 (at most 7) |
| any | — | `python scripts/ibkr_paper_experiment.py abort` | re-reads the position and the experiment's own fills first. Only when they agree on flat: every working order of this client cancelled by its own order id. Otherwise (held, disagreeing or unreadable): no protective stop and no parent of one is cancelled, only this client's other working orders; result `ABORT_STOP_LEFT_WORKING` with the TWS procedure | 0 |

### Stage 3: the close (owner's decision, 13 November 2026)

"For the closing sell limit, use the fresh executable bid as the initial price rather than
automatically applying a 1% downward haircut. If it doesn't fill, use bounded cancellation and
broker-state reconciliation. Preserve protection until flat. Never submit another exit while the
previous order's outcome is unknown."

Wiring (`experiment.py` `_stage3`): `ExecutionConfig(exit_price_policy="TOUCH",
exit_validity_seconds=CLOSE_WAIT_SECONDS, accepted_quote_qualities={LIVE})`,
`MockPolicy(max_quote_age_seconds=3)`, `IbkrPaperBroker(exit_at_touch=True, strict_quotes=True)`.

At most `STAGE3_CLOSE_ATTEMPTS` (2) attempts. Each attempt, in order:

1. **Quote, reads only.** A LIVE quote with a positive bid and ask (bid not above ask) and both
   sides at most 3 s old, measured from the read's reference time. A read that is not usable is
   repeated for up to `QUOTE_RETRY_SECONDS` (20 s). None: no closing limit is sent, the stop is
   left as it is, `STAGE3_CLOSE_NO_QUOTE`. (The entry has the same rule; `STAGE3_NO_USABLE_QUOTE`,
   nothing sent, stage 3 may run again.)
2. **The core's CLOSE.** It reads its own quote (again LIVE, older side at most 3 s) and prices a
   SELL 1 LMT DAY **at the bid** (snapped up to the tick, never below the bid read). It records
   the intent, then cancels the protective stop (after the binding's dry check of that limit) and
   confirms the cancel at the broker (gone from open orders, Cancelled in completed orders), then
   re-reads (fills, position, working orders, the account-wide exit check), and only then sends
   the closing limit (one submission). Any step that cannot be confirmed sends nothing
   (`STAGE3_CLOSE_REFUSED`; if the stop was already cancelled, `STAGE3_PROTECTION_LOST`). A send
   whose acknowledgement is lost ends at once `STAGE3_CLOSE_OUTCOME_UNKNOWN`.
3. **Wait, reads only.** `sync` every second. Filled: reconcile and finish. Once the limit has
   worked `CLOSE_WAIT_SECONDS` (10 s) since it was sent, the core's `sync` cancels it and confirms
   the cancel (`EXIT_EXPIRY_CANCEL_REQUESTED`, `EXIT_EXPIRED_CANCEL_CONFIRMED`; up to 10 reads 1 s
   apart). A fill that beat the cancel is applied: flat, nothing more is placed.
4. **Cancelled unfilled.** In the same `sync` the intent ends CANCELLED with
   `EXIT_UNFILLED_CANCELLED` and the core places the protective stop **again**: a NEW SELL 1 STP
   DAY, no parent, at the ledger's (raised) stop price, under the ref `<entry ref>.STP<n>` that
   becomes the idea's stop (one submission, once per exit, never resent). It is placed only when
   reads made then show the old stop cancelled, no other exit of the idea live, and the broker
   position less every working sell covers it. `_maintain_protection` then confirms it like any
   stop. The script waits (reads only, up to `PROTECTION_SETTLE_SECONDS`, 15 s) until the core
   reports it CONFIRMED **and** the open-orders read shows it working. Only then may the next
   attempt start. Not confirmed: `STAGE3_PROTECTION_NOT_RESTORED` (the share may be unprotected).
5. **Cancel not confirmed or outcome unknown** (the limit still working after
   `CLOSE_WAIT_SECONDS` + `CLOSE_SETTLE_SECONDS`, or not visible without a completed-orders row):
   nothing more is sent, no stop is placed again (the limit still counts as a live exit),
   `EXIT_CANCEL_UNCONFIRMED` stays open and `STAGE3_CLOSE_OUTCOME_UNKNOWN` is reported. The core
   asks the cancel again at any later `sync` and resolves the intent only on broker evidence; in
   this mode it never resends an exit of unknown outcome (`EXIT_OUTCOME_UNKNOWN`), and a new CLOSE
   is refused (`EXIT_NOT_NETTED`) while an earlier one is recorded, unresolved or not confirmed
   cancelled.

After the last unfilled attempt the share is held with the stop placed again and confirmed:
`STAGE3_CLOSE_NOT_FILLED`, whose message states the stop's status as read.

The evidence records, per attempt (`close_attempts`): every quote read before it (bid, ask, each
side's age, label, problems, usable), the core's own reads during the CLOSE (`core_quote_reads`,
the first is the one the limit was priced on), the limit sent, the wait, the outcome and reasons,
whether the cancel was requested and confirmed, the protection, stop ref and stop status before
the next attempt, and the submissions used before and after.

**When the share has no working stop.** Only from the stop's cancel until the closing limit
fills, or until the stop placed again is acknowledged after the limit's confirmed cancel. By
construction that is at most: the stop's cancel and its confirmation (up to `cancel_wait` plus 10
reads 1 s apart), two syncs and the account-wide check, the closing limit's acknowledgement (up to
`ack_timeout`, 5 s), `CLOSE_WAIT_SECONDS` (10 s) plus the next one-second poll, the limit's cancel
and confirmation (as above), and the reads and acknowledgement of the stop placed again: about
10 s plus a few seconds when the broker answers promptly, under a minute when every confirmation
read is needed. Twice at most. It is **unbounded** (a human acts) only on an abnormal ending:
`STAGE3_CLOSE_OUTCOME_UNKNOWN`, `STAGE3_PROTECTION_NOT_RESTORED`, `STAGE3_PROTECTION_LOST`, a
`STAGE3_CLOSE_REFUSED` after the stop's cancel, or a process or TWS failure inside that window.

After stage 3 (or on any failure): turn Read-Only API back ON, remove the `orders` line, and keep
the evidence directory and journal. If stage 2 reports a fill, follow its printed TWS procedure (below):
sell only if the TWS window shows 1, and run `abort` only once TWS shows 0; stage 3 then never
runs.

**If stage 2 fills** (result `STAGE2_UNEXPECTED_FILL`): stage 2 cancels the parent first and
cancels the child only when the parent is confirmed cancelled with nothing filled. After any
fill it sends no cancel for the protective SELL STP, records what is held and blocks stage 3. The
evidence (`held.stop_status`, `held.stop_working`) and the message report the child's actual
state: the broker may have cancelled it with its parent. The stop counts as working ONLY when its
status is Submitted or PreSubmitted AND it is visible in a completed open-orders read; Inactive,
PendingCancel, an unknown status or a failed read are "NOT working". Then follow the printed
TWS procedure (below): never `abort` while TWS shows 1 and a working stop.

**What the script prints on an abnormal ending, and what the owner does.** Every message is
`<RESULT>: <cause>. Now: <state>. <procedure>`, built from reads only. Before printing, the reads
are retried for up to 15 s (`POSITION_SETTLE_SECONDS`) until the broker's position read agrees
with the experiment's own fills; stage 3 likewise retries a reconciliation that is not clean for
up to 15 s (`RECONCILE_SETTLE_SECONDS`) before failing, so a read that merely lags the closing
fill ends as a PASS. No retry sends an order frame or uses budget.

The **state** line is one of:
- read and fills agree: "this script reads a TLT position of N (the broker's read and the
  experiment's own fills agree)";
- they disagree: "the broker's position read R and the experiment's own fills F disagree: do not
  sell on this script's numbers" (no position is stated as held);
- unreadable: "the broker's position could not be read: do not sell on this script's numbers".
It then says whether a protective stop of this client is working (status) or not ("if TWS shows
1, the share is UNPROTECTED"), whether a closing limit is resting, or that the orders could not
be read.

The **procedure** is the same for every ending that is not a confirmed flat:

> In the TWS window: (1) open the Trades panel: if it already shows a TLT sale of this experiment
> today, skip step (3); (2) cancel a resting TLT closing limit only if you can see it working
> there, never the stop; (3) Look at the TLT position in the TWS window. Only if it shows 1: sell
> 1 there with a limit at the bid (lower it a tick at a time until it fills) and wait until it
> shows 0. If it shows 0: do NOT sell. (4) After a manual sale, run abort at once: the protective
> stop is still working until then and must not trigger on a share you no longer hold. Abort
> cancels the stop only when this script itself reads the account flat; if it reports the stop
> left working while TWS shows 0, cancel that TLT stop in the TWS window yourself at once. Never
> cancel the stop while TWS shows 1.

A manual sale is not an execution of the experiment, so after one the experiment's own fills
still say 1 while the position read says 0: abort then reads a disagreement and leaves the stop
working (`ABORT_STOP_LEFT_WORKING`), and the owner cancels that stop in TWS by hand, as step (4)
says.

When read and fills agree on 0, it instead says "run abort to cancel this client's N working
order(s), then confirm in TWS that the TLT position is 0 and no TLT order is working", or, with
nothing working, "nothing to do; confirm in TWS ...". No message contains an unconditional sell
(test: every ending x every state).

| Result | Cause printed | The owner does |
|---|---|---|
| `STAGE2_UNEXPECTED_FILL` | the entry filled; its stop is working (status, in open orders) or NOT working (status, in or not in open orders); stage 3 blocked | the TWS procedure |
| `STAGE2_PARENT_NOT_CONFIRMED_CANCELLED` | the parent was not confirmed cancelled; its child was left alone | the TWS procedure, or abort if the reads agree on flat |
| `STAGE2_FAILED` | the read-back, cancels or completed orders did not show what stage 2 needs | abort if the reads agree on flat with orders working; otherwise the TWS procedure |
| `STAGE3_ENTRY_NOT_FILLED` | the entry did not fill in time | normally flat with the bracket working: abort (it re-reads first: if the entry filled meanwhile, it leaves the stop working); otherwise the TWS procedure |
| `STAGE3_PROTECTION_UNCONFIRMED` | the entry filled; the stop was not confirmed within 15 s | the TWS procedure |
| `STAGE3_PROTECTION_LOST` | POSITION OPEN, PROTECTION ABSENT: the close was not sent after the stop was cancelled | the TWS procedure |
| `STAGE3_CLOSE_REFUSED` | the close was REFUSED (or REJECTED / HALTED), with the core's reasons, e.g. `EXIT_WITHOUT_QUOTE` | the TWS procedure (the stop normally still works: sell only if TWS shows 1, abort after) |
| `STAGE3_CLOSE_NOT_FILLED` | the closing limit did not fill in 2 attempts; each was cancelled (confirmed) and the protective stop placed again: "a protective stop is working (status) as read" | the TWS procedure (the share is held with its stop: sell only if TWS shows 1, abort right after) |
| `STAGE3_CLOSE_OUTCOME_UNKNOWN` | the closing limit (ref, limit) MAY STILL BE WORKING OR MAY HAVE FILLED: its cancel was not confirmed or its acknowledgement was lost; nothing more was sent and no stop was placed again, so a share still held may be UNPROTECTED | the TWS procedure (step 2 cancels the closing limit only if TWS shows it working) |
| `STAGE3_PROTECTION_NOT_RESTORED` | SHARE POSSIBLY UNPROTECTED: the closing limit was cancelled unfilled (confirmed) but the stop placed again was not confirmed working | the TWS procedure at once |
| `STAGE3_CLOSE_NO_QUOTE` | before a close attempt no LIVE quote with both sides at most 3 s old in 20 s of reads; no closing limit sent, the stop left as it was | the TWS procedure |
| `STAGE3_NO_USABLE_QUOTE` | before the entry no such quote; nothing was sent | nothing in TWS; stage 3 may run again (the count is still 2) |
| `STAGE3_ENTRY_REFUSED` | the core refused the entry (its reasons, e.g. `BROKER_QUOTE_STALE`); nothing was sent | nothing in TWS; stage 3 may run again |
| `STAGE3_NOT_CLEAN` | after the 15 s retry, reconciliation still not clean, or the count is not exactly 6 (first close filled) or 8 (second close filled) | the TWS procedure; with read and fills disagreeing, do not sell on the script's numbers; keep the evidence |
| `ABORT_STOP_LEFT_WORKING` | abort does not read the account flat; it left the protective stop(s) working and cancelled only the other orders | the TWS procedure; if TWS shows TLT 0, cancel that stop in TWS yourself at once; if it shows 1, leave it |
| `STAGE1_QUOTE_NOT_USABLE` | no sample labelled LIVE or DELAYED with a bid and an ask (labels and last reason) | nothing to do in TWS (no order was sent); run stage 1 again |

## Emergency stop

- **The TWS window is the source of truth.** Check the TLT position and the working TLT orders
  there before any manual order or cancel. Sell only if TWS shows 1; cancel only orders you can
  see working in TWS. Never act on this script's numbers when they disagree with TWS.
- **Not known to cancel anything (unverified):** disconnecting the client, killing the script, or
  turning Read-Only API back ON. Working orders may stay live in TWS after any of these. The
  halt file only affects a coordinator that is still running (stage 3): it stops new entries and
  amendments and cancels working entries, not the stop or a placed close.
- **The certain stop:** in the paper Trader Workstation window, cancel every working TLT order of
  the experiment that you can see working there (Orders / Activity panel, right-click, Cancel),
  then look at the TLT position in TWS: only if it shows 1, sell 1 with a limit at the bid; if it
  shows 0, do not sell. Confirm in the Portfolio that TLT is 0.
- **The software path:** `python scripts/ibkr_paper_experiment.py abort` re-reads the position
  and the experiment's own fills first. When they agree on flat it cancels every working order of
  this client by its own order id (never a global cancel). Otherwise it cancels no protective stop
  (nor the parent of one), only the other orders, and reports `ABORT_STOP_LEFT_WORKING`. It works
  even when the journal is unknown. It does not close a position: only if the TWS window shows 1
  TLT, sell it there (a limit at the bid), then run `abort` at once; if it then reports the stop
  left working while TWS shows 0, cancel that stop in TWS yourself.
- **The close.** The protective stop is DAY: it expires at 16:00 ET and a share held overnight is
  unprotected; nothing can be placed after 16:00 ET. The experiment must end flat the same day.
  **If the share is not closed by 15:30 ET** (the script reports the close within about two
  minutes, normally `STAGE3_CLOSE_NOT_FILLED` with the stop working): in TWS cancel any resting
  closing limit you can see working, then look at the TLT position there: only if it shows 1,
  sell 1 with a limit at the bid (lower it a tick at a time until it fills); if it shows 0, do
  not sell. Confirm 0 before 16:00 ET, run `abort`, and record what you did beside the evidence.

## Execution-core change it needs

`ExecutionConfig.protective_tif` (default GTC). Stage 3 sets DAY; the DAY-only cap refuses GTC
(`TIF_NOT_ALLOWED`).

For the owner's exit decision, two additive options whose defaults leave every other mode
unchanged (`tests/test_exit_order_life.py`):

- `exit_price_policy`: `"COLLAR"` (default: a SELL exit at the bid less the policy collar) or
  `"TOUCH"` (a SELL exit at the bid snapped up, a BUY exit at the ask snapped down).
- `exit_validity_seconds` (default None): an exit still working that long after it was sent is
  cancelled by `sync` with broker confirmation; confirmed unfilled it ends CANCELLED with
  `EXIT_UNFILLED_CANCELLED` and the stop the exit netting cancelled is placed again, once, under a
  new ref (`store.set_stop_ref`); unconfirmed it opens `EXIT_CANCEL_UNCONFIRMED` and nothing is
  placed. In this mode an exit of unknown outcome is never resent (`EXIT_OUTCOME_UNKNOWN`).

## Stage 3 close: known limits

- **No protection while a closing limit works.** The caps allow no stop beside a closing limit
  for the one share (two working sells could both fill), and there is no one-cancels-other link
  between a standalone stop and a closing limit here (that would be new session, guard and wire
  support). So for up to `CLOSE_WAIT_SECONDS` plus the confirmations, per attempt, the share's
  only working order is the closing limit (see "When the share has no working stop").
- **The bid is fresh when priced, not when sent.** The core prices the limit on a quote whose
  older side is at most 3 s old at its decision, then cancels and confirms the stop, re-reads and
  sends: the limit reaches the broker some seconds after that quote (the stop's cancel
  confirmation plus the reads; the binding's dry check takes one more bounded quote sample). It is
  not re-priced after the netting; if the bid has fallen meanwhile the limit rests and is
  cancelled after 10 s.
- **A bounded quote sample is never instant.** Each read opens a stream and ends about 1 s
  (`stream_settle_seconds`) after the last new tick type, so a side that did not change during the
  sample is already about 1 s old when observed. The 3 s limit leaves about 2 s of margin; a quiet
  book retries (reads only) or ends `STAGE3_CLOSE_NO_QUOTE`.
- **Only the plain stop is placed again**: an idea with a target, a backstop or a reference-series
  stop is left unprotected for a human (not the case in stage 3). A stop placed again whose
  acknowledgement is lost, or that the broker rejects, is never resent.
- **No automatic recovery after a crash.** Stage 3 cannot be repeated once it submitted, and no
  script runs the coordinator's `recover` on its store. After a TWS disconnect or a process death
  during the close, the orders at TWS stay as they were (DAY): the owner follows the printed
  message, or the TWS procedure and `abort`. `abort` re-reads first: with a share held it cancels
  only the closing limit and leaves any stop; it never places a stop.
- **The stop placed again is DAY** like every order here: a share held at 16:00 ET is unprotected
  overnight. The experiment must end flat the same day.

## Not modelled relative to the real broker

- Whether TWS honours an explicit protobuf `transmit=false`. The SDK omits a false bool, and TWS's
  default for an absent field is undocumented.
- Whether untransmitted orders appear in open orders.
- How a real child rejection affects its held parent.
- Stops that report PreSubmitted while active. The coordinator never infers an entry fill from
  its child (`ENTRY_STATE_UNKNOWN`: at IBKR a child is PreSubmitted before the parent fills and
  may stay so after). A PreSubmitted stop is confirmed as protection only once the parent's own
  fills are applied (`coordinator.py` `_maintain_protection`, `reconcile.py`); until then the
  position reads UNPROTECTED.
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
