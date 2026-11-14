# Agent development rules

Canonical repository: https://github.com/tsaid1/tradingbot (**public**).

## Safety rules (never relaxed by any task)

1. **One gateway.** Every order comes from an action that the deterministic gateway
   (`validator.py`) approved, through `ExecutionCoordinator`. No other path may create or increase
   exposure. Operator tools only record what a human established at the broker, lift a block, or,
   where `[operator_confirmation]` is enabled in a simulated or paper mode, confirm the reading of
   a held signal whose only hold reasons are on the closed list in docs/OPERATOR_CONFIRMATION.md;
   the confirmed action passes the gateway and the coordinator again, unchanged, and nothing an
   operator enters becomes an order value. LLM or interpreter output never bypasses the gateway.
2. **No paid source content and no credentials in this repo.** Do not commit post text,
   screenshots, subscriber material, tokens, account numbers or broker licences. Tests and
   fixtures use invented data only. Logs, journals and notifications carry identifiers and reason
   codes, never post text.
3. **No order submission without the owner's explicit authorization of a specific experiment.**
   `main` runs the simulated broker only: `mode = "mock"` (invented market) or `mode = "shadow"` (simulated orders and fills at quotes from the read-only adapter `tradingbot.ibkr`, which cannot send orders). An order-capable module (`tradingbot.ibkr_orders`,
   `scripts/ibkr_paper_*`) defines two capped paper experiments, `paper-test-1` and
   `paper-test-2`; each is disabled by default and may be run only with the Product Owner's
   explicit authorization of that experiment (docs/IBKR_PAPER_ORDERS.md).
4. **Independent review before any change to an order path.** That covers the coordinator, the
   broker protocol and adapters, reconciliation, settlement and the runtime service's broker
   handling. Protocol changes are additive.
5. **The funded account is out of scope.** Paper only, and only after rule 3.
6. Treat `models.py` and `event_models.py` as versioned contracts. Add a regression test for
   every parsing rule and every execution lifecycle change.
7. Read the publisher only through authorized access. No scraping of protected content.

## How work is organised

* Each task runs on its own branch and worktree, and lands as its own PR. Avoid unrelated
  refactors and reformatting.
* The core engineering agent owns `src/`, `dashboard/`, `tests/`, `scripts/`, `ops/` and `docs/`.
* The research agent owns `docs/research/` until a review accepts its work.
* Reviewers are independent of the author and record their findings under `orchestration/reviews/`
  (outside the repo).
* The product owner merges PRs, approves mappings and experiments, and owns every broker-side
  change.

Architecture: `README.md`, `docs/RUNTIME.md`, `docs/EXECUTION_CORE.md`. Operations:
`docs/OPERATIONS.md`.
