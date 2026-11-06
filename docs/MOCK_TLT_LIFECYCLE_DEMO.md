# Mock TLT lifecycle demonstration (Track B)

**Status:** runnable, synthetic-only, mock-only. The demonstration drives the repository's existing
interpreter, deterministic gateway and paper coordinator through a complete TLT lifecycle and shows
every decision in an operator-readable audit. It adds no authorization rule, no sizing, no persistence
of its own and no broker connectivity. **Live execution is impossible** in this code.

Every post, price level, date and expected outcome lives in `fixtures/demo_tlt_lifecycle.json` and is
invented (dates sit in a future window so they cannot coincide with a real publication). No publisher
content, development corpus, lexicon or holdout material is used.

## Run it

```bash
python -m pip install -e '.[dev]'
python scripts/run_mock_tlt_demo.py                         # text audit, temporary mock ledger, exit 0 on PASS
python scripts/run_mock_tlt_demo.py --json                  # the same audit as JSON
streamlit run dashboard/app.py                              # then open "Mock TLT Lifecycle" in the sidebar
streamlit run dashboard/pages/1_Mock_TLT_Lifecycle.py       # or the page on its own
pytest tests/test_mock_tlt_demo.py                          # the regression tests for this flow
```

The Streamlit page keeps its mock ledger in `data/demo_tlt_lifecycle.sqlite3` (git-ignored; override with
`TRADINGBOT_DEMO_DB`). Use **Run next step** to walk the lifecycle one post at a time, **Run all remaining
steps** for the whole sequence, **Replay last processed post** for the idempotency check, and **Reset demo**
for an empty ledger. The CLI uses a temporary ledger unless `--db` is given (`--fresh` empties it first).

## What the flow shows

| Acceptance step | Demo post | What happens | Where it is decided |
|---|---|---|---|
| 1. Synthetic TLT signal input | `d1` "We are now long $TLT at $100.00. Stop: $96.00. Target: $108.00." | A `SourcePost` (schema 1.2, provenance `SYNTHETIC`) | `source_events.SourcePost` |
| 2. Structured interpretation with provenance | `d1` | One `Classification` (OPEN / PRESENT, entry 100.00, stop 96.00, target 108.00) and one `TradeAction`; the trusted `ReplaySource` (verified author, revision 1, complete text, text hash) travels alongside and is never produced by the interpreter | `tkl_interpreter.TklInterpreter`, `SourcePost.to_replay_source()` |
| 3. Deterministic validation gateway | every post | `gate_source` then `gate_action`; the result is one of APPROVE / HOLD / BLOCK / NO_TRADE / RECONCILE with reason codes | `validator.py` (unchanged) |
| 4. Simulated order and fill | `d1` | `SIMULATED BUY 4 TLT @ 100.05`, sized by `MockPolicy`, never by the post | `event_replay.PaperEventReplay` |
| 5. Protected position | `d1`, `d2` | The ledger idea carries stop 96.00 / target 108.00; `d2` adds 2 units and tightens the stop to 98.00 in **one atomic group** (both apply or neither) | `replay_ideas` table, `validator._gate_follow_up` |
| 6. Partial, then complete close | `d5`, `d6` | Half (3 of 6 units) sold at the bid, then the remaining 3; the idea is CLOSED with 0 units | `validator.fill_quantity`, coordinator |
| 7. Reconciliation | `d7` | A past-tense confirmation returns RECONCILE and no order; the audit states whether the mock ledger is consistent with the report. The reconciliation panel compares the publisher's stated book (the interpreter's point-in-time context) with the mock ledger, idea by idea; a disagreement is an operator task, never an order | `demo_lifecycle.DemoSession.reconciliation()` |
| 8. Audit and operational interface | all | Mock banner, per-step provenance, interpretation, gateway decision with a plain-language explanation for every code, fill, ledger after the step, expected-versus-actual tick, session summary | `dashboard/pages/1_Mock_TLT_Lifecycle.py`, `scripts/run_mock_tlt_demo.py` |

Understandable failures and holds in the same run: `d3` (a conditional exit) is **HOLD
CONDITIONAL_TRIGGER_NOT_IMPLEMENTED**, `d4` (commentary with a price) and `d8` (a position summary) are
**NO_TRADE**, and `d9` (the entry wording again, from an author that could not be verified) is **BLOCK
SOURCE_NOT_AUTHENTICATED**. None of them changes the ledger.

Idempotency: replaying any processed post returns the recorded outcome as **DUPLICATE** and leaves the
ledger unchanged; restarting the demo on an existing ledger treats every post as already processed, so
nothing can fill twice.

## What is reused and what was added

Reused unchanged: `models.py` (contracts and `MockPolicy`), `validator.py` (the only gateway),
`event_replay.py` (paper coordinator and ledger), `tkl_interpreter.py` (interpreter, point-in-time
context, `replay_posts`, `group_linked_actions`), `source_events.py`, and the `GoldenReplay` fixture
format from `evaluation.py` (the demo script embeds one, and `run_golden` verifies it in the tests).

Added: `fixtures/demo_tlt_lifecycle.json` (the invented script with narration),
`src/tradingbot/demo_lifecycle.py` (session glue, audit records, reconciliation view, plain-language
explanations for every gateway and interpreter code), `scripts/run_mock_tlt_demo.py` (CLI),
`dashboard/pages/1_Mock_TLT_Lifecycle.py` (page of the existing dashboard), `tests/test_mock_tlt_demo.py`.

## Mock boundary

- Quotes are typed into the fixture (mid ± 0.05) and stamped at the replay clock; they are not market data.
- Fills, positions and protection levels exist only in the SQLite mock ledger. There is no broker-native
  stop or target, no account, no credentials and no network call anywhere in this flow.
- `verified_author=True` in the fixture is a test input, not cryptographic proof; the production adapter
  owns authorship verification.
- The policy values shown on the page are the fixture's illustrative demo limits, displayed read-only.

## Limitations

- The grammar is the deterministic one validated on synthetic phrasings; the demo proves the lifecycle
  mechanics, not interpreter accuracy on real posts.
- Conditional instructions are held by design; there is no trigger engine.
- The reconciliation view compares two local views (publisher book as interpreted, mock ledger). A broker
  book does not exist yet, so a real reconciliation against broker positions remains Sprint 2 work.
