# Tradingbot — Signal-to-Execution Lab (Sprint 1)

**A deliberately fail-closed, mock-only development foundation.**

This project converts synthetic trading signals (or user-provided analysis in optional AI mode)
into typed candidate instructions, runs deterministic validations, and records simulated orders.
It does **not** connect to X, does **not** contact a broker, and cannot place live orders.

## Quickstart (macOS / Linux)

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
cp .env.example .env
pytest
streamlit run dashboard/app.py
```

For free tests, select **Deterministic demo**. Paste:

```text
BUY ABC @ 100 STOP 95 TARGET 110
```

Use synthetic quote symbol `ABC`, bid `99.95`, ask `100.05`. A single fictional share
will be recorded as `SIMULATED_FILLED`. Re-run using the same source ID and it is rejected
as `DUPLICATE` before invoking an interpreter. The mock also supports:

```text
CLOSE ABC IDEA <original-source-id>
REDUCE 50% ABC IDEA <original-source-id>
```

The demo grammar is intentionally narrow and recognizes **neither arbitrary prose nor charts**.
That is a safety feature, not a claim of NLP quality.

### Optional LLM + chart mode

Set `OPENAI_API_KEY` and optionally `OPENAI_MODEL` in `.env` (not in source control).
Select **AI vision** in the dashboard and optionally attach a PNG/JPEG/WEBP chart.
It calls the OpenAI Responses API with Pydantic structured output (`store=False`).
API costs apply; avoid sending publisher material unless its use is permitted.
An LLM may hallucinate: output still passes the deterministic validator, and no
live broker is present in the codebase.

This is **not** a production risk engine. Important work remains: author authentication,
cryptographically verified source provenance, quote and broker state sync, instrument
resolution, conditional signal semantics, stop lifecycle, live order idempotency and
broker capability tests. No live trading should be enabled before these gates are met.

## Data and privacy

Local SQLite state lives under `data/`, which is ignored by Git. Never commit
subscription reports, protected posts, raw screenshots, API keys, or user account
information. The `fixtures/` directory contains **fictional synthetic material only**.
Check publisher terms and source/API permissions before protected-feed ingestion.

## Repository layout

- `src/tradingbot/models.py`: typed contracts
- `src/tradingbot/interpreters.py`: narrow demo parser; optional vision parser
- `src/tradingbot/validator.py`: deterministic safety preflight
- `src/tradingbot/storage.py`: durable source IDs, atomic fictional fills and idea positions
- `src/tradingbot/pipeline.py`: orchestration (dedupe before parsing)
- `dashboard/app.py`: signal console
- `tests/`: safety regression suite
- `docs/`: scope, architecture decisions, agent coordination

## Project rules

1. A publication or bullish thesis is **not** automatically an entry instruction.
2. Never guess option contract identifiers, futures expiry, or missing position references.
3. Unsupported asset classes/actions fail closed (`REVIEW`), not silently translated.
4. Model output can NEVER call broker APIs directly.
5. A failed or uncertain submission cannot be blindly retried.
6. The user's broker will eventually own approved trading parameters; API preset behavior
   needs independent verification before live deployment.
7. Do not enable live order submission in this branch or Sprint 1.
