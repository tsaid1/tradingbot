# G0 archive: event schema and mapping to the Sprint 1.1 contracts

**Purpose:** the schema-only handoff requested in `docs/SPRINT_1_1_AGENT_HANDOFF.md` so the Sprint 1.2 source adapter can be written without guessing the archive structure. Keys, types and semantics only. **No post text, no media, no subscriber data appears in this file.** The archive itself is paid publisher content held locally under the git-ignored `data/g0/` and must never be committed.

Figures below are measured on the local backfill corpus of 2026-11-05 (3,189 posts by one author, June 2024 to November 2026).

## 1. Where the data lives (local, git-ignored)

| Path | Content | Produced by |
|---|---|---|
| `data/g0/corpus/posts_<UTC timestamp>.jsonl` | One **full** post record per line (schema in section 2) | `x_probe.py backfill` |
| `data/g0/corpus/media/<media_key>.<ext>` | Chart images referenced by `media[].media_key` | `x_probe.py backfill --download-media` |
| `data/g0/poll_<UTC timestamp>.jsonl` | Live-poll log: per-call quota/timing records and **hashed** posts (`text_sha256`, lengths, lag fields). Not a text source. | `x_probe.py poll` |

Records come from `GET /2/users/1237233159005179904/tweets` under the approved subscriber's OAuth 2.0 user token with `tweet.fields=created_at,edit_controls,edit_history_tweet_ids,referenced_tweets,attachments,conversation_id,in_reply_to_user_id,author_id,note_tweet`, `expansions=attachments.media_keys,referenced_tweets.id`, `media.fields=media_key,type,url,preview_image_url,variants`. The endpoint path pins the author, so every record's `author_id` is the target's.

## 2. Archive record (`posts_*.jsonl`)

| Key | Type | From X API | Semantics |
|---|---|---|---|
| `id` | str | `data[].id` | Post ID (snowflake). For an edited post this is the **latest version's** ID. |
| `author_id` | str | `data[].author_id` | Always `1237233159005179904` in the corpus (3,189 of 3,189). |
| `created_at` | str, ISO-8601 UTC, `.000Z` | `data[].created_at` | Whole-second resolution. Equals the snowflake time of `id` within 1 s on every record. **For an edited post it is the time of the edit**, not of the original (verified on all 61 edited posts: `created_at` matches the current ID's snowflake time and trails the first ID's by 6 s to 60 min). |
| `conversation_id` | str | `data[].conversation_id` | Equals `id` for thread roots (3,041); 148 records are inside a thread. |
| `text` | str | `note_tweet.text` when present, else `text` | **Full body** as the probe resolved it. Max 1,588 characters in the corpus. |
| `has_note` | bool | presence of `note_tweet` | True on 1,367 posts (long bodies obtained). |
| `short_text` | str | `data[].text` | X's 280-weighted-character field (raw length up to 336 because URLs count as 23). Never truncated with an ellipsis on long posts in this corpus; `text` is the field to parse. |
| `referenced` | list of `{type, id}` | `referenced_tweets` | `type` in `quoted` (1,601 posts), `retweeted` (291), `replied_to` (144). Only the referenced IDs are stored; **quoted or retweeted text is not in the archive**. |
| `is_reply` | bool | `in_reply_to_user_id` or `replied_to` | 144 author replies within own threads. |
| `is_quote` | bool | `quoted` reference | 1,601 quote posts; the author's own text is in `text`, the quoted content is only referenced. |
| `edit_history_ids` | list[str], oldest first | `edit_history_tweet_ids` | Present on every record. Length 1 for unedited posts (first element equals `id`); length 2 on all 61 edited posts (1.9%), with `id` as the last element. No chain longer than 2 was observed. |
| `media` | list of `{media_key, type, url}` | `includes.media` | 993 items, all `type == "photo"`, every item has a `url`. |
| `observed_at` | str, ISO-8601 UTC | probe clock | When the probe received the response. For backfill records this is 1 to 132 days after `created_at`; it is an archive timestamp, **not** a latency measurement. |
| `media_files` | list of `{status, file?, bytes?}` | probe download | 991 files saved (`status` 200); 2 items returned 404 at download time. |

Schema-only sample (synthetic values, shapes only):

```json
{"id": "2000000000000000001", "author_id": "1237233159005179904",
 "created_at": "2026-01-02T14:31:07.000Z", "conversation_id": "2000000000000000001",
 "text": "<full body, up to ~1,600 chars>", "has_note": true, "short_text": "<X text field, <= 336 chars>",
 "referenced": [{"type": "quoted", "id": "1999999999999999999"}], "is_reply": false, "is_quote": true,
 "edit_history_ids": ["2000000000000000001"],
 "media": [{"media_key": "3_2000000000000000002", "type": "photo", "url": "https://pbs.twimg.com/media/<key>.png"}],
 "observed_at": "2026-11-05T10:21:03.412000+00:00",
 "media_files": [{"status": 200, "file": "3_2000000000000000002.png", "bytes": 123456}]}
```

## 3. Mapping to the merged contracts (`main` at e022602)

Python import path consumed: `from tradingbot.event_models import ReplaySource, ParsedPost, TradeAction` and `from tradingbot.models import SourceEvent, Quote` (package `tradingbot-signal-lab` 0.1.0, src layout, `pip install -e .`). Agent B's tooling only reads these contracts; the adapter itself is Agent A's module.

### `ReplaySource` (trusted metadata; the LLM never sets these)

| Field | Rule | Why |
|---|---|---|
| `source_id` | `edit_history_ids[0]` (the original post ID), **not** `id` | The logical post identity must survive edits. An edit creates a new post ID; if `id` were used, every edit would look like a fresh post and could execute again. |
| `revision` | `edit_history_ids.index(id) + 1` (1 = original, 2 = first edit) | Matches the engine: revision 1 may execute; any other revision, or a second revision of a known post, is held for review. |
| `published_at` | Snowflake time of `edit_history_ids[0]` (equals revision 1's `created_at` within 1 s) | The freshness rule must measure from the original publication. The edited version's own `created_at` is the edit time; carry it in the adapter manifest as `revised_at` (not a `ReplaySource` field today). |
| `received_at` | `observed_at` for live-poll records | For backfill records `observed_at` is the archive time, so offline replay must set `now` explicitly (below) and treat `received_at` as provenance only. |
| `verified_author` | `author_id == "1237233159005179904"` **and** the record came from that user's timeline endpoint under the subscriber token | Belt and braces: the endpoint pins the author, the field check guards against adapter bugs. Not cryptographic proof. |
| `complete_text` | `not is_repost and (has_note or not short_text.rstrip().endswith("…"))` | In this corpus every ellipsis-terminated, note-less record is a retweet (286 of 286); zero non-retweet truncation. So all 2,898 non-retweet posts are complete. Says nothing about images. |
| `is_repost` | any `referenced[].type == "retweeted"` (equivalently `text` starts with `RT @`) | 291 retweets. X truncates retweet text at about 140 characters; the full text is in the referenced post, which is someone else's instruction, so NO_TRADE. |
| `post_hash` | `sha256(text.encode("utf-8")).hexdigest()` over the exact full body, no normalisation | Identifies the bytes the parser saw. Media are not hashed; their keys are listed separately. Equal hashes under the same revision are duplicates; a changed hash under the same revision is blocked unless it is the one-time completion of a truncated post. |

### `SourceEvent` (Sprint 1 pipeline, if the feed is ever pointed at it)

`source_id` as above, `source_type="protected_feed"`, `provider="x_api_v2_user_timeline"`, `text` = full body, `published_at` = original publication. The Sprint 1 journal keys on `source_id` alone and has no revision concept, so edits must not be fed to it.

### Historical timestamp semantics for offline replay

- `published_at` is the X server timestamp of the original publication, UTC, whole seconds.
- `received_at` on archive records is meaningless for latency. Replay must pass `now = published_at + lag`, where `lag` is the measured publication-to-receipt figure from the G0 freshness run (pending; see `G0_RESULTS.md`), and synthesize quotes with `observed_at <= now`. Using the archive's `observed_at` as `now` would make every post stale.
- For revision 2 records `created_at` is the edit time; the engine holds them regardless, so this only matters for reporting.

### Context the adapter must still resolve

- **Quoted posts (1,601, half the corpus).** The archive holds only the quoted post's ID. The author's own words are the authorized text; the quoted content is context. The adapter should fetch it (public quoted posts are readable with the app bearer token) or record its absence as an ambiguity.
- **Threads (144 replies).** Group by `conversation_id`; a reply may reference levels in the root post.
- **Images (993 photos, 991 on disk).** A chart may carry the instruction; `complete_text` does not cover it.
- **Edits (61).** Single edits only, 6 s to 60 min after the original, median 38 s; 30 were long posts and 26 carried media. Emit revision 2 under the same `source_id`.

## 4. Proposed contract additions for Sprint 1.2 (for Agent A to decide)

1. `ReplaySource.revised_at: datetime | None` to carry the edit time of revision >= 2.
2. `ReplaySource.schema_version: Literal["1.1"]` to match `ParsedPost`.
3. A `provenance` enum on `ReplaySource` (`API_USER_CONTEXT_PROTECTED` for this route) as proposed in `X_PROTECTED_ACCOUNT_API_FEASIBILITY.md`.
4. `media_complete: bool` or a media-key list, so the gateway can hold text-only parses of chart-bearing posts when policy requires it.
