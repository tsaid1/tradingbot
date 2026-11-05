# Sprint 1.1 — Multi-action event contracts & offline TLT paper replay

**Status:** Offline implementation prototype. **Live execution is impossible** in this module. Source and broker integrations remain separate workstreams.

## Reason for the change

The private post review found: a single post can contain more than one action; current closes must be separated from retrospective exit reports; statements such as "we remain bullish" are not new entries; an entry can refer to levels in another post; a market quote may refer to an index or spot commodity, not an orderable contract. Truncated posts and reposts may not authorize execution.

The new modules are **additive** and retain the original Sprint 1 demo parser/dashboard/tests unchanged. They contain no subscriber post text or subscriber data.

## What was added

- `src/tradingbot/event_models.py`: `ReplaySource` (trusted metadata), `ParsedPost` (untrusted model output), `TradeAction` (zero-to-many ordered child actions), explicit `Timing` and `PriceBasis` enums, and paper-only results. `ReplayPolicy` is the shared `models.MockPolicy` (see ADR-009).
- `src/tradingbot/post_interpreters.py`: optional, **untested with the hosted API**, structured multi-action LLM extraction. It has no brokerage or X credentials.
- `src/tradingbot/event_replay.py`: deterministic replay **coordinator** using separate SQLite tables for posts, child action outcomes, and **persistent trading ideas**. It stores hashes and structured mock-state only, not paid post text. Every authorization decision comes from `validator.py`.
- `tests/test_event_replay.py`: synthetic safety tests; no licensed examples. `tests/test_gateway_equivalence.py`: proves both engines receive equivalent decisions, the policy is defined once, hydration is safe and connections are closed.
- `scripts/run_synthetic_tlt.py`: fictional TLT old-idea/new-idea lifecycle.

## Local usage

```bash
python -m pip install -e '.[dev]'
pytest
python scripts/run_synthetic_tlt.py
```

## One gateway, one policy (integration revision)

`src/tradingbot/validator.py` is the **sole deterministic authorization gateway**:

| Function | Role |
|---|---|
| `gate_source(source, now, is_edit=)` | Post-level provenance: edits/revisions → HOLD, unverified author → BLOCK, repost → NO_TRADE, truncated text → HOLD, timezone or receipt-order faults → BLOCK |
| `gate_action(action, published_at, quote, policy, position, now=, supported_actions=)` | Per-action preflight on the Sprint 1.1 `TradeAction` schema: timing, authority, instrument identity, allowlist, freshness, quote integrity, then OPEN / CLOSE / REDUCE / ADD / MODIFY rules. Returns a `GateDecision` (`APPROVE / HOLD / BLOCK / NO_TRADE / RECONCILE`) |
| `fill_quantity(action, policy, position, close_fraction)` | The only sizing function. Reads `MockPolicy` and ledger state; never model output |
| `validate(source, signal, quote, policy, position, now=)` | **Compatibility wrapper** for the Sprint 1 pipeline: lifts a `SignalCandidate` into a `TradeAction` (`as_trade_action`), calls `gate_action` with the journal's capability set (OPEN/CLOSE/REDUCE), and translates the result to the legacy `Decision` vocabulary (`EXECUTE_MOCK / REVIEW / BLOCK / NO_TRADE`) and reason names (`LEGACY_REASON_CODES`) |

Engines are coordinators: `storage.Journal` (Sprint 1) and `event_replay.PaperEventReplay` (Sprint 1.1) sequence calls, keep linked actions atomic, enforce once-only processing and apply fictional fills after an APPROVE. Neither contains a risk rule.

Risk parameters and defaults live only in `models.MockPolicy` (`quantity`, `quantity_per_add`, `max_price_deviation_pct`, `max_spread_pct`, `max_notional_usd`, `max_signal_age_seconds`, `max_quote_age_seconds`, `require_stop_on_open`, `allowed_symbols`, and the locked `paper_only=True` / `allow_stop_widening=False`). The Sprint 1.1 spellings (`quantity_per_open`, `max_deviation_pct`, `max_source_age_seconds`) are accepted as aliases. Shared defaults are the Sprint 1 values (1 unit, $2,000 notional); the Sprint 1.1 tests pin a 2-unit opening size explicitly. `allowed_symbols=None` means no allowlist (demo grammar); production adapters must set one.

The LLM-facing schemas (`SignalCandidate`, `ParsedPost`, `TradeAction`) carry no quantity, venue or account field, and reject extras.

## Replay contract

1. A **trusted adapter** constructs `ReplaySource`, including post ID, revision, verified author, full-text flag, repost flag, timestamps and SHA-256 source digest. The LLM cannot set these fields.
2. The parser produces `ParsedPost(actions=[...])`. Each `TradeAction` has a stable action ID and a `Timing` of `CURRENT`, `HISTORICAL`, `CONDITIONAL`, `COMMENTARY`, or `UNCERTAIN`.
3. Only **CURRENT, explicit, evidence-backed, unambiguous** actions pass `gate_action`. Historical exits return `RECONCILE`, conditional intents return `HOLD`, and commentary returns `NO_TRADE`.
4. The MVP permits only **directly orderable equities/ETFs** where source-market identity matches the orderable contract and the explicit reference price is for that same contract. There is **no** automatic SPX→SPY, spot gold→GLD, oil→futures, or any futures/option execution.
5. Every follow-up must reference an existing **idea ID**. A ticker alone cannot select an old or new position.
6. `atomic_group` links actions such as ADD + MODIFY; if any child fails, all changes in that group are held. Separate groups may succeed independently in the same post.
7. Simulated actions are committed under one SQLite write transaction with unique `(source_id, revision, attempt, action_id)` records. Exact duplicate source revisions return recorded outcomes and never submit a second fictional order. A digest change without a revision is blocked; revised posts require review rather than silently altering positions.
8. **Hydration of truncated posts.** A post received with `complete_text=False` only ever yields `HOLD SOURCE_TEXT_TRUNCATED`; it is recorded but authorizes nothing. When the adapter later supplies the complete text under the **same `source_id` and `revision`** (so a different digest), the coordinator evaluates it **once** as a new attempt, then seals the row as complete. After that, any further digest change is blocked. The hydrated pass still runs through `gate_source` (so an edit recorded meanwhile, or a revision other than 1, is held) and through the freshness rules (so a late completion is stale). A complete post can never be downgraded to truncated, and a second, different truncation of the same post is blocked.

## Safety boundaries

- `verified_author=True` in these synthetic fixtures is a test input; it is **not** cryptographic proof. A production ingestion adapter must validate the expected author ID and platform session independently.
- Local replay `now` overrides are for deterministic historical tests. A live service must use trusted wall-clock arrival timestamps and preserve source timestamp provenance.
- A chart may corroborate a signal, but source-price numbers are not blindly translated to different contracts.
- `require_stop_on_open=True` is a mock policy. **No real broker-native protection is created or tested here.**
- The model may misclassify a post or hallucinate an idea reference; the gateway checks references against stored ideas, but only a holdout evaluation can quantify misclassification risk.
- Do not commit protected X posts, attachments, OCR, text labels that reproduce paid content, extracted prices tied to paid posts, tokens, or subscriber records. Keep the private evaluation dataset outside Git. Confirm your rights to send paid content to external AI APIs before use.

## Known limits (deliberately unchanged in this revision)

- No aggregate exposure cap: each approved OPEN creates a separate idea; only the per-order notional cap applies.
- Exits are not privileged: a CLOSE with a stale quote is blocked and the paper position stays open.
- The Sprint 1 journal cannot fill ADD or MODIFY, so the compatibility wrapper keeps them under review there; the replay coordinator fills them.

## Next acceptance gates

**Sprint 1.2 (private corpus / evaluation):** Receive a safe JSON manifest of the G0 archive format, map source metadata into `ReplaySource`, resolve complete text, and independently label a stratified holdout. Evaluate action, timing, contract and idea-link accuracy. Do not equate synthetic tests with proof of parser accuracy.

**Sprint 2 (broker POC):** Independently test live market-data provenance, contract qualification, position quantities, protective orders, order idempotency, partial fills, broker presets, and account-level limits in a broker paper environment before adding a broker adapter.

**G0 remains parallel:** live freshness and negative bearer-token control are pending. Source access alone does not certify runtime speed or legal authorization for all downstream uses.
