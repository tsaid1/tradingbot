# Agent development rules

Canonical repository: https://github.com/tsaid1/tradingbot

1. No agent may add live broker order submission without a new, explicit safety review.
2. Never commit user-subscription content or API credentials to this **public** repo.
3. Work on separate branches and PRs. Avoid unrelated formatting/refactors.
4. Treat schemas in `src/tradingbot/models.py` as versioned contracts.
5. No LLM-created field directly bypasses deterministic `validator.py`.
6. Add regression tests for every new parsing rule and execution lifecycle feature.
7. Record external API feasibility findings, exact documentation URLs and limitations.
8. Do not use unauthorized scraping to read protected X content.

## Suggested parallel assignments

**Agent A (core, this starter)**: contracts, demo parser, optional vision extraction,
validation, mock journal, tests, local UI. Ownership: `src/tradingbot/`,
`dashboard/`, `tests/`.

**Agent B (research and isolated implementation)**: official X protected-post
availability proof, commercial/publisher terms diligence; compare IBKR vs tastytrade
(or a suitable alternative), including native presets for API orders and support for
ETF/stock, options and futures. Ownership: `docs/research/` ONLY until a review.

**Product owner:** selects broker, approves ingestion terms, merges PRs, authorizes
real-money permission changes only after paper-trading gate.

## Secondary agent prompt

"Research official authorized read access for the protected X account
@TKLSubscribers without attempting to bypass access, and independently score
IBKR/tastytrade API order types, native broker preset support, paper accounts,
options/futures availability and authentication. Produce concise, source-linked
findings in docs/research/ as a PR. Do not touch the core parser, risk gateway,
GUI or any live execution route. Do not use or publish paid source content."
