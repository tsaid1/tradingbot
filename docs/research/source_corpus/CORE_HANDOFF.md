# Source to interpreter handoff

The producer owns the historical private archive. The core team owns the interpreter input and any mapping to trusted replay metadata. The archive CLI and reader have no core imports. This note records the boundary for interface review.

## Proposed SourceEvent semantics for review

| Item | Historical evidence and proposed consumer behavior |
|---|---|
| Identity | Preserve string platform IDs. Each captured revision has its own post ID. The first ID in its ordered edit-history chain identifies its family. Never present the second revision as a first delivery. |
| Author | The downloaded post's author ID matches the protected timeline lookup. A repost also identifies its original via a typed reference. Community membership or an original repost author's identity cannot be inferred from this lookup. |
| Clocks | Publication belongs to the captured revision. The backfill observation is the start of the page request. Live receipt is unknown. A deterministic replay clock must be explicitly synthetic and cannot serve as G0 latency evidence. |
| Body | UTF-8 API text verbatim, SHA-256 over those exact bytes, note body when captured. A repost wrapper or possible truncation is never promoted to a complete original. Missing quoted bodies are separate from completeness of the new author's own body. |
| References | Preserve conversation ID, typed quote/reply/repost IDs and ordered edit IDs. Resolve context from earlier available revisions only. Missing parents remain unresolved. |
| Media | Retain media key, type, URL, download outcome and verified local byte digest where available. No media, text or label enters a public artifact. |
| Provenance | Provider, source route, capture hash and per-record line number. Historical inputs are replay-only. No broker, venue, position, quantity or destination is assigned here. |

Before connecting an interpreter, agree its version, import path, required field names, digest/identity scheme and read-only dev input selection. Use the sealed development export for evaluation work. The curator's full archive includes the holdout.
