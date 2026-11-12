# TradingBot

Turns a publisher's trading posts into typed actions. A deterministic gateway validates every
action, and only an action it approves can reach a broker. Today that broker is a strict
**simulated broker**. An Interactive Brokers paper adapter is being prepared separately, and no
real order can be sent from `main`.

```
source (replay file | X timeline) -> TklInterpreter -> mapping policy -> gateway (validator.py)
   -> ExecutionCoordinator -> Broker (SimBroker) -> fills -> follower ledger -> reconciliation
                         runtime service: heartbeat, alerts, operator queue, dashboard
```

* **Gateway** (`validator.py`, `execution/replay.py`): every rule that can say no. Commentary,
  edits, unverified or stale posts, unmapped instruments and cap breaches are held with reason
  codes. Nothing that is held is traded.
* **Execution core** (`execution/`): write-ahead intents, idempotent order refs, an order of
  unknown outcome never resent on assumption, confirmed protective stops, exit netting, the halt
  switch, read-only reconciliation, and operator settlement. See `docs/EXECUTION_CORE.md`.
* **Multi-asset mapping** (`runtime/mapping.py`): only owner-approved families execute. See
  `docs/MULTI_ASSET_MAPPING.md`.
* **Runtime service** (`runtime/`, `scripts/run_service.py`): one restart-safe loop with immediate,
  de-duplicated alerts, a broker reconnect policy and a heartbeat watchdog. See `docs/RUNTIME.md`.
* **IBKR, read-only** (`ibkr/`, `scripts/ibkr_paper_check.py`): an operator-run identity and
  read check against a local paper session. It sends no orders. See `docs/IBKR_ADAPTER.md`.
* **Dashboard** (`streamlit run dashboard/app.py`): the *Execution* and *Exceptions* pages, among
  others. Its only controls are the halt and operator requests.

## Run it (mock)

```bash
python3 -m venv .venv && source .venv/bin/activate && pip install -e '.[dev]'
pytest
PYTHONPATH=src python scripts/run_mock_service_demo.py
PYTHONPATH=src python scripts/run_service.py --config config/runtime.example.toml --once
streamlit run dashboard/app.py
```

Operating it (start and stop, alerts, recovery, the daily checklist): `docs/OPERATIONS.md`.

## Rules

The safety rules are in `AGENTS.md`. In short:

* every order passes the one gateway;
* no paid source content and no credentials in this public repo;
* no order submission without the owner's explicit authorization of a specific experiment;
* independent review before any change to an order path;
* the funded account is out of scope.

Local state lives outside the repo (`~/.local/state/tradingbot/` by default). `fixtures/` holds
invented material only.
