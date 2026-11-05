# Sprint 1.2 — historical source and corpus engineering

This offline exporter validates a protected timeline backfill and writes a chronological, read-only archive. The source record schema is in `archive.schema.json`. See `CORE_HANDOFF.md` before connecting an interpreter. Nothing here submits orders or calls a provider.

## Local use

Run from the repository root with Python 3.11 or later. Set `CAPTURE` to the local posts JSONL, `TARGET` to the protected timeline lookup, and `MEDIA` to the capture's media directory. These files stay private.

```sh
python docs/research/source_corpus/archive.py "$CAPTURE" --target "$TARGET" --media "$MEDIA" --expected-count 3189
python docs/research/source_corpus/archive.py "$CAPTURE" --target "$TARGET" --media "$MEDIA" --expected-count 3189 --full-archive --out private_samples/sprint12/archive.jsonl
python docs/research/source_corpus/archive.py "$CAPTURE" --target "$TARGET" --media "$MEDIA" --split-manifest private_samples/sprint12/holdout/split_manifest.jsonl --out private_samples/sprint12/development.jsonl
python -m pytest
```

Use a new output filename for each immutable export. The CLI validates the full capture before creating the file, refuses tracked or public destinations and never overwrites a predecessor. The standard-library `read_history(path, through=aware_datetime)` iterator only yields captured revisions at or before the specified publication time.

## Historical capture

| Check | Observed |
|---|---:|
| Records / distinct IDs / target authors | 3,189 / 3,189 / 1 |
| Publication window (UTC) | 2024-06-17 to 2026-11-04 |
| Long-form API bodies / edited records | 1,367 / 61 |
| Complete author bodies / flagged author truncations / repost wrappers | 2,898 / 0 / 291 |
| Photo references / locally verified files / recorded 404s | 993 / 991 / 2 |
| Quote / reply references | 1,601 / 144 |
| Absent quote / reply / repost targets | 421 / 133 / 287 |
| Private screenshot labels matched to unique API revisions | 100 / 100 |
| Chronological golden sequences / distinct captured revisions | 4 / 38 |
| Separately annotated holdout posts / source-action candidates | 365 / 78 |

The full archive is for the corpus curator. The private split assigns 120 development posts, 365 scoring posts, 97 holdout-context posts, 527 quarantined posts and 2,080 unassigned posts. Give the interpreter only the development export during tuning. Apply the same allowlist to context retrieval. Holdout answers and corrections stay outside the public branch.

## Limits

- An authorized backfill records the captured revision's publication time and a historical page-request timestamp. The latter cannot measure live delivery. The G0 probe separately measures live delivery and app-only authorization.
- Earlier text versions of the 61 edited records are unavailable. Reposts can contain only wrapper excerpts. The core handoff defines a note-less short field ending with a literal Unicode ellipsis as potentially truncated. None of the original-author posts in this capture meets that condition.
- An absent parent, quote or media file remains absent. A protected-author timeline establishes the timeline identity at collection; it carries no independent community-membership assertion.
- The golden sequences express source intent as of publication. The holdout has a single annotation pass independent of interpreter output. Broker fills, strategy performance and parser accuracy require separate evidence.
