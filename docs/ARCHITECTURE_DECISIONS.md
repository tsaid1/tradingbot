# Architecture decisions (living ADR)

## ADR-001 — Protected source is an X account, not a Community

Current working source: `@TKLSubscribers`, a protected X account. Access has been
requested but not approved as of initial sprint. Do not rely on public X streaming
for protected content. Read/poll eligibility, author provenance and permissions
are feasibility gates; do not browser-scrape or bypass visibility controls.

## ADR-002 — Paper first; never guess from a chart

General newsletters are often *views* with targets/stops, rather than immediate
orders. Image annotations do not independently authorize trades. Explicit commands
require verified contract and source before execution.

## ADR-003 — Conservative modular monolith

Python + Pydantic + SQLite + Streamlit. No Redis, SQS, Kubernetes or AWS yet.

## ADR-004 — Trading integration is always a downstream policy enforcement layer

AI extraction cannot choose unrestricted trade size or access a broker. Broker-native
risk presets must be independently checked per API endpoint, asset and order type.

## ADR-005 — Costs and milestones

Development cash ceiling $500, personal AI-assisted development, 2–3-week initial
prototype target. Treat developer time, paid feed, actual quotes and subscriptions
separately. Do not commit source-provider protected material to a public repo.

## ADR-006 — Sprint 1 asset scope

Equities/ETFs, single-leg options and futures are represented in schemas.
Only **fictional equity/ETF orders** are applied in the first mock engine.
Options and futures are non-executable until individually validated and paper-tested.

## ADR-007 — Suspension policy

Performance thresholds generate notifications in later sprints. Technical integrity
failures MUST automatically block unsafe new submissions (not discretionary strategy halt).

## ADR-008 — Publication-to-execution metrics

Eventual target: ordinary publication-to-broker acknowledgement ~10–15 seconds.
Source delay is not controllable; measure publication, receipt, parse, preflight,
submission, ack, first fill and protection confirmation independently.

## ADR-009 — One authorization gateway, one policy contract

`src/tradingbot/validator.py` is the sole deterministic authorization gateway.
`gate_source()` applies provenance rules to a whole post and `gate_action()` applies
per-action preflight to the Sprint 1.1 `TradeAction` schema; `validate()` is a
compatibility wrapper that lifts a Sprint 1 `SignalCandidate` into that schema and
translates the canonical result (APPROVE / HOLD / BLOCK / NO_TRADE / RECONCILE) to the
legacy `Decision` vocabulary. Engines (`storage.Journal`, `event_replay.PaperEventReplay`)
are coordinators: they sequence calls, keep linked actions atomic, enforce once-only
processing and apply fictional fills sized by `fill_quantity()`. Risk parameters and
defaults live only in `models.MockPolicy`; `ReplayPolicy` is the same class. No LLM-facing
schema carries a quantity, venue or account. Equivalence of both paths is a regression test.
