# G0 results: official X API read access to `@TKLSubscribers`

**Date run:** 2026-11-05 · **Run by:** Agent B with the product owner's subscriber account · **Status:** ACCESS PROVEN; freshness not yet measured
**Tooling:** `g0_x_probe/x_probe.py` (auth, lookup, controls, backfill). Raw output lives in git-ignored `data/g0/` and is paid content; nothing from it is reproduced here beyond counts.

## Outcome against the G0 criteria

| Criterion | Result | Evidence |
|---|---|---|
| Protected posts returned under the subscriber's user token | **PASS** | Backfill returned 3,189 posts from the user timeline endpoint; every post's author ID is the protected account's ID and no other. |
| Media retrievable | **PASS** | 993 posts carried media; 991 image files downloaded (two returned 404, consistent with deleted media). 150 MB of charts. |
| Full text of long posts | **PASS** | 1,367 posts carried the long-post body (`note_tweet`); longest body 1,588 characters. The `tweet.fields` naming was accepted; no fallback needed. |
| Thread and quote linkage | **PASS** | 1,601 posts reference a quoted post and 144 are replies, via `referenced_tweets`. 61 posts carry edit history. |
| Reliability | **PASS** | 33 pages, zero 4xx/5xx, zero rate-limit pauses. |
| Recent search under user token | As expected | `from:TKLSubscribers` recent search returned 0 posts: search does not expose protected content. |
| Bearer-token (app-only) control | NOT RUN | Bearer variable was missing from the local env file at run time. Low priority; rerun with `controls`. |
| Freshness (median lag under 30 s at a 15 s poll) | NOT MEASURED | Requires `poll` during a US market session. |

## Corpus obtained

- 3,189 posts, 17 June 2024 to 4 November 2026, single author. This is the endpoint's full 3,200-post window; nothing older is reachable through this route.
- Monthly volume ranges from about 70 to 260 posts, averaging about 110, so roughly four posts per trading day. The screenshot sample had suggested two; the API corpus is the better estimate.
- Stored at `data/g0/corpus/posts_20261105T102037Z.jsonl` with images under `data/g0/corpus/media/`. Git-ignored. Subject to TKL terms: personal evaluation only, no redistribution, delete on request.
- Approximate cost at pay-per-use pricing: about 3,200 post reads plus a handful of user lookups, roughly $16.

## Operational lessons

1. The OAuth 2.0 Client ID must be copied exactly; a single transcribed character (digit 1 versus lowercase l) produced X's generic "Something went wrong" authorization page with no further detail. The token endpoint's `invalid_client` error is the fast diagnostic.
2. Confidential-client app type with PKCE and callback `http://127.0.0.1:8765/callback` works for a local tool.
3. Media download is the slow part of a backfill (about 10 minutes for 991 images); posts alone take about a minute.
4. Access tokens last two hours; the stored refresh token (`offline.access`) renews them without a browser.

## What this changes

- Spec section 3 source path B (official X API) is **certified for access** for this account. Path A (publisher feed) is no longer required for feasibility, though it would still improve latency if offered.
- The parser evaluation corpus is now API text, not OCR: relabel `private_samples/tkl_100` from it and extend labels across the history.
- Remaining G0 item: run `poll` across one US session to measure publication-to-receipt lag and set the freshness TTL (spec D-05).
