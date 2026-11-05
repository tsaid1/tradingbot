# Source to interpreter handoff

The producer owns the historical private archive. The core team owns the interpreter input and any mapping to trusted replay metadata. The archive CLI and reader have no core imports. The [core handoff](https://github.com/tsaid1/tradingbot/blob/84b93c8956e4c4106aa2e7caacc1fc906a98efb3/docs/research/G0_EVENT_SCHEMA.md) specifies the existing Sprint 1.1 consumer mappings.

## Historical input for core

| Item | Historical evidence and proposed consumer behavior |
|---|---|
| Identity | Preserve string platform IDs. Each captured revision has its own post ID. The first ID in its ordered edit-history chain identifies its family. Never present the second revision as a first delivery. |
| Author | The downloaded post's author ID matches the protected timeline lookup. A repost also identifies its original via a typed reference. Community membership or an original repost author's identity cannot be inferred from this lookup. |
| Clocks | Publication belongs to the captured revision. The backfill observation is the start of the page request. Live receipt is unknown. A deterministic replay clock must be explicitly synthetic and cannot serve as G0 latency evidence. |
| Body | UTF-8 API text verbatim, SHA-256 over those exact bytes, note body when captured. A repost wrapper or possible truncation is never promoted to a complete original. Missing quoted bodies are separate from completeness of the new author's own body. |
| References | Preserve conversation ID, typed quote/reply/repost IDs and ordered edit IDs. Resolve context from earlier available revisions only. Missing parents remain unresolved. |
| Media | Retain media key, type, URL, download outcome and verified local byte digest where available. No media, text or label enters a public artifact. |
| Provenance | Provider, source route, capture hash and per-record line number. Historical inputs are replay-only. No broker, venue, position, quantity or destination is assigned here. |

From the repo root, import `historical_inputs` from `docs.research.source_corpus.source_reader`. Supply the SHA-256 from a trusted export, the authorized timeline ID, an explicit simulated lag and the development file. The iterator never executes an interpreter or reads scoring labels.

```python
from datetime import timedelta
from docs.research.source_corpus.source_reader import historical_inputs

for item in historical_inputs(dev_path, expected_sha256=dev_digest,
                              expected_author_id=target_id, simulated_lag=timedelta(0)):
    text = item.text                 # full API body; apply source gates before interpretation
    metadata = item.replay_source    # tradingbot.event_models.ReplaySource
```

The example uses zero lag only as a deterministic simulation parameter. The revision's API timestamp controls availability, and the root's original publication controls freshness. For eligible revision-1 originals, `item.as_source_event()` returns `tradingbot.models.SourceEvent`. It refuses edits, uncertain bodies and reposts that the legacy shape cannot safely represent. The richer replay metadata and original archive row remain available on every item.

`post_hash` matches the UTF-8 text digest. A pinned archive checksum also covers identity, references, provenance and media metadata. A time cutoff on the interpreter reader includes simulated receipt lag. Never use the curator's full archive to tune or retrieve context; use the development file. The original, unedited publication time inferred from an edit chain uses the X snowflake epoch, truncated to whole seconds.
