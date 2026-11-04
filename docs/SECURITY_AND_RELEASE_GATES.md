# Security and release gates

This starter is neither production trading software nor financial advice.

**P0 (current):** strict mock only; fake quotes, fake orders; credentials absent;
all private data excluded from Git. This is the only enabled mode.

**P1 (future):** broker paper adapter with explicit capability matrix, order/fill
reconciliation, safe retry rules, protected stops, accurate contract identifiers,
actual quotes, continuous test monitoring. No real orders.

**P2 (future):** written permission/confirmed lawful source ingestion, verified
real-time source provenance, API availability/latency, 300+ independently labeled
samples including updates, and zero unintended executions on safety suite.

**P3 (future):** independently reviewed live execution release with separate
credentials/accounts; hard notional/margin caps; broker-native protection; kill
switch; operational monitoring; emergency runbook; market session edge cases;
24h unattended recovery drills. P3 is outside this starter.

**Permission boundary:** an LLM receives only signal text/images (where authorized),
never brokerage/API credentials. External content is untrusted and must not be
allowed to alter policies or run arbitrary commands.
