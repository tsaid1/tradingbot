# Sprint 1.1 — Multi-action event contracts & offline TLT paper replay

**Status:** Offline implementation prototype. **Live execution is impossible** in this module. Source and broker integrations remain separate workstreams.

## Reason for the change

The private post review found: a single post can contain more than one action; current closes must be separated from retrospective exit reports; statements such as "we remain bullish" are not new entries; an entry can refer to levels in another post; a market quote may refer to an index or spot commodity, not an orderable contract. Truncated posts and reposts may not authorize execution.

The new modules are **additive** and retain the original Sprint 1 demo parser/dashboard/tests unchanged. They contain no subscriber post text or subscriber data.

## What was added

- `src/tradingbot/event_models.py`: `ReplaySource` (trusted metadata), `ParsedPost` (untrusted model output), `TradeAction` (zero-to-many ordered child actions), explicit `Timing` and `PriceBasis` enums, and paper-only results.
- `src/tradingbot/post_interpreters.py`: optional, **untested with the hosted API**, structured multi-action LLM extraction. It has no brokerage or X credentials.
- `src/tradingbot/event_replay.py`: deterministic mock replay using separate SQLite tables for posts, child action outcomes, and **persistent trading ideas**. It stores hashes and structured mock-state only, not paid post text.
- `tests/test_event_replay.py`: synthetic safety tests; no licensed examples.
- `scripts/run_synthetic_tlt.py`: fictional TLT old-idea/new-idea lifecycle.

## Local usage

```bash
python -m pip install -e '.[dev]'
pytest
python scripts/run_synthetic_tlt.py
```

## Replay contract

1. A **trusted adapter** constructs `ReplaySource`, including post ID, revision, verified author, full-text flag, repost flag, timestamps and SHA-256 source digest. The LLM cannot set these fields.
2. The parser produces `ParsedPost(actions=[...])`. Each `TradeAction` has a stable action ID and a `Timing` of `CURRENT`, `HISTORICAL`, `CONDITIONAL`, `COMMENTARY`, or `UNCERTAIN`.
3. Only **CURRENT, explicit, evidence-backed, unambiguous** actions enter deterministic paper preflight. Historical exits return `RECONCILE`, conditional intents return `HOLD`, and commentary returns `NO_TRADE`.
4. The MVP permits only allowlisted **directly orderable equities/ETFs** where source-market identity matches the orderable contract and the explicit reference price is for that same contract. There is **no** automatic SPX→SPY, spot gold→GLD, oil→futures, or any futures/option execution.
5. Every follow-up must reference an existing **idea ID**. A ticker alone cannot select an old or new position.
6. `atomic_group` links actions such as ADD + MODIFY; if any child fails, all changes in that group are held. Separate groups may succeed independently in the same post.
7. Simulated actions are committed under one SQLite write transaction with unique `(source_id, revision, action_id)` records. Exact duplicate source revisions return recorded outcomes and never submit a second fictional order. A digest change without a revision is blocked; revised posts require review rather than silently altering positions.
8. `ReplayPolicy` is a **fictional policy** for testing. It is not broker policy configuration, not a live risk gateway, and does not account for market impact, margin, live fills, gaps, commissions, or corporate actions.

## Safety boundaries

- `verified_author=True` in these synthetic fixtures is a test input; it is **not** cryptographic proof. A production ingestion adapter must validate the expected author ID and platform session independently.
- Local replay `now` overrides are for deterministic historical tests. A live service must use trusted wall-clock arrival timestamps and preserve source timestamp provenance.
- A chart may corroborate a signal, but source-price numbers are not blindly translated to different contracts.
- `require_stop_on_open=True` is a mock policy. **No real broker-native protection is created or tested here.**
- The model may misclassify a post or hallucinate an idea reference; this module checks references against stored ideas, but only a holdout evaluation can quantify misclassification risk.
- Do not commit protected X posts, attachments, OCR, text labels that reproduce paid content, extracted prices tied to paid posts, tokens, or subscriber records. Keep the private evaluation dataset outside Git. Confirm your rights to send paid content to external AI APIs before use.

## Next acceptance gates

**Sprint 1.2 (private corpus / evaluation):** Receive a safe JSON manifest of the G0 archive format, map source metadata into `ReplaySource`, resolve complete text, and independently label a stratified holdout. Evaluate action, timing, contract and idea-link accuracy. Do not equate 40 synthetic tests with proof of parser accuracy.

**Sprint 2 (broker POC):** Independently test live market-data provenance, contract qualification, position quantities, protective orders, order idempotency, partial fills, broker presets, and account-level limits in a broker paper environment before adding a broker adapter.

**G0 remains parallel:** live freshness and negative bearer-token control are pending. Source access alone does not certify runtime speed or legal authorization for all downstream uses.
