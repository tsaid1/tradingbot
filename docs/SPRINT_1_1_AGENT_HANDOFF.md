# Agent coordination — Sprint 1.1

Branch suggestion: `feature/sprint1.1-event-replay`. This change touches only new files in `src/tradingbot/`, `tests/`, `scripts/` and `docs/`. Do not modify `docs/research/g0_x_probe/`, `data/g0/`, or existing starter interfaces in this PR.

## Agent A (event semantics)

- Own additive models + replay tests and private evaluation interface.
- Do not submit broker orders or configure live-trading credentials.
- Do not commit proprietary source material.
- Run complete `pytest` locally and CI.

## Agent B (G0)

- Continue test of authenticated feed freshness and the app-only bearer negative control. Never print raw keys.
- Export a **schema-only** sample of normalized G0 event data (keys and types, no protected text) so Agent A can write a source adapter without guessing archive structure.
- Report how full-text, media coverage, quoted/replied posts, edited posts and author identity are represented.
- Do not touch `src/tradingbot/`, `tests/`, `scripts/` while this PR is in flight.

## Product owner

- Review/merge PR only after CI and offline demo pass.
- Keep paid raw corpus under `data/g0/corpus/` and private labels under git-ignored paths.
- Authorize any future ingestion-to-paper linkage only after the source adapter's completeness and provenance controls are verified.
