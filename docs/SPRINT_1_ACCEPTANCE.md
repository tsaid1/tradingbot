# Sprint 1 acceptance criteria / current scope

## Shipped in starter

- [x] Pydantic trade candidate covering equities/ETFs, single-leg options and futures.
- [x] Deliberately limited rule-based parser, no API spend.
- [x] Optional LLM structured parsing with PNG/JPEG/WEBP chart input.
- [x] Fail-closed distinction between explicit commands and commentary.
- [x] Deterministic source-age, quote-age, instrument, spread, entry collar,
      stop/target sign, mock notional and duplicate checks.
- [x] SQLite source journal with durable `RECEIVED` reservations.
- [x] Atomic mock-only OPEN/REDUCE/CLOSE for **fictional equities/ETFs**.
- [x] Streamlit signal console with no live broker connection.
- [x] Offline automated tests and GitHub Actions CI.

## Not yet implemented

- [ ] Verified access to the protected @TKLSubscribers X account.
- [ ] TKL-authorized protected-content storage/processing terms.
- [ ] Rigorous labeled corpus of actual source posts and market updates.
- [ ] Deterministic evidence verification beyond model-provided snippets.
- [ ] Bounded second AI verification for ambiguous or contradictory content.
- [ ] Full idea state machine for ADD/MODIFY, partial fills and linked native protection.
- [ ] Exact option/futures contract lookup and actual quote feeds.
- [ ] One broker sandbox adapter, preset/conditional-order capability proof.
- [ ] Continuous ingestion, timestamp latency tracking and alerting.
- [ ] Any real-money trading. This MUST NOT be added without formal safety gates.

## Definition of done for Sprint 1

1. Tests pass locally and in CI.
2. Synthetic `OPEN` -> mock order -> idea position -> `REDUCE` -> `CLOSE` works.
3. Identical source ID does not cause a second mock order or another AI call.
4. Stale signal, stale quote, price collar violation, wrong ticker, missing idea,
   contrary stop, unsupported option/futures action all fail safely.
5. The UI presents the classifier response, specific validation reasons, and journal.
6. AI mode never receives brokerage credentials and has no execution tools.

The current implementation meets the structural requirements and offline tests,
NOT AI extraction quality: that awaits a manually labeled source dataset and live API tests.
