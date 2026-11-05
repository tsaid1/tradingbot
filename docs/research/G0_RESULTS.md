# G0 results: official X API read access to `@TKLSubscribers`

**Date run:** 2026-11-05 · **Run by:** Agent B with the product owner's subscriber account · **Status:** G0 COMPLETE. Access proven, controls passed, freshness measured (median lag 12.4 s)
**Tooling:** `g0_x_probe/x_probe.py` (auth, lookup, controls, backfill, phased poll, report). Raw output lives in git-ignored `data/g0/` and is paid content; nothing from it is reproduced here beyond counts.

## Outcome against the G0 criteria

| Criterion | Result | Evidence |
|---|---|---|
| Protected posts returned under the subscriber's user token | **PASS** | Backfill returned 3,189 posts from the user timeline endpoint; every post's author ID is the protected account's ID and no other. |
| Media retrievable | **PASS** | 993 posts carried media; 991 image files downloaded (two returned 404, consistent with deleted media). 150 MB of charts. |
| Full text of long posts | **PASS** | 1,367 posts carried the long-post body (`note_tweet`); longest body 1,588 characters. The `tweet.fields` naming was accepted; no fallback needed. |
| Thread and quote linkage | **PASS** | 1,601 posts reference a quoted post and 144 are replies, via `referenced_tweets`. 61 posts carry edit history. |
| Reliability | **PASS** | 33 pages, zero 4xx/5xx, zero rate-limit pauses. |
| Recent search under user token | As expected | `from:TKLSubscribers` recent search returned 0 posts: search does not expose protected content. |
| Bearer-token (app-only) control | **PASS** | App-only token returns zero posts and an explicit `not-authorized-for-resource` error for the protected user ID. Protected content is gated to the approved follower's user context, as the Developer Policy requires. |
| Freshness (median lag under 30 s) | **PASS** | Live session 5 November 2026, 14:30 to 16:30 UTC: five posts, median publication-to-receipt lag 12.4 s, p95 and p99 13.4 s. See the freshness section. |
| Completeness of live delivery | **PASS** | 1,200 calls, zero errors, zero duplicates; end-of-run reconciliation: X reports 5 posts since the baseline, polling delivered 5, none missed, none vanished. |
| In-run bearer control | **PASS** | Each new post was immediately re-requested with the app-only token: 5 of 5 returned zero posts with `not-authorized-for-resource`. Token injected from the environment, never logged. |

## Corpus obtained

- 3,189 posts, 17 June 2024 to 4 November 2026, single author. This is the endpoint's full 3,200-post window; nothing older is reachable through this route.
- Monthly volume ranges from about 70 to 260 posts, averaging about 110, so roughly four posts per trading day. The screenshot sample had suggested two; the API corpus is the better estimate.
- Stored at `data/g0/corpus/posts_20261105T102037Z.jsonl` with images under `data/g0/corpus/media/`. Git-ignored. Subject to TKL terms: personal evaluation only, no redistribution, delete on request.
- Approximate cost at pay-per-use pricing: about 3,200 post reads plus a handful of user lookups, roughly $16.

## Freshness run (5 November 2026, 14:30 to 16:30 UTC)

Design: fixed-cadence polling of the user timeline with `since_id`, three phases (15 s for 60 min, 5 s for 30 min, 3 s for 30 min), every quota header recorded, timed download of every image, an app-only bearer control repeated whenever a post arrived, an end-of-run reconciliation, and X's own billable-read counter read before and after. The run would have extended at 15 s to the close had fewer than three posts arrived; five did.

| Measure | Result |
|---|---|
| Live posts | 5 (3 with long bodies, 1 with a chart) |
| Publication-to-receipt lag, p50 / p95 / p99 / max | 12.4 s / 13.4 s / 13.4 s / 13.4 s |
| Lag by cadence | 15 s: 13.3 s and 11.5 s; 5 s: 12.4 s; 3 s: 13.4 s and 10.4 s |
| Polling window (previous send to receipt) | p50 5.3 s, max 15.4 s |
| X-side exposure delay, lower bound | p50 7.1 s, max 10.1 s (0 for the 15 s posts, where it cannot be proven) |
| Calls, errors | 1,200 timeline calls, 0 errors, 0 rate-limit pauses |
| Request time | p50 0.3 s, p95 0.4 s, max 1.4 s |
| Cadence held | per phase exactly 15.0 s, 5.0 s, 3.0 s between sends |
| Quota headers | `x-rate-limit-limit` 900 per 15 min on the timeline; minimum remaining 600 during the 3 s phase (300 calls per window); the app-only bearer bucket showed 10,000 |
| Duplicates, edits | 0, 0 |
| Reconciliation | X reports 5 posts since baseline; polling delivered 5; missed 0; vanished 0 |
| Media | one 280 KB image, fetched in 0.80 s, URL present on first delivery; media lag 12.3 s |
| In-run bearer control | 5 of 5 calls: status 200, zero posts, `not-authorized-for-resource` |
| Metered cost | project usage 3,189 before, 3,194 after: 5 billable reads, $0.025 at list price; the 1,200 empty calls, the baseline read and the reconciliation re-read cost nothing (daily dedup confirmed) |

**Interpretation.**

1. The median of 12.4 s passes the G0 threshold of 30 s.
2. **Polling cadence is not the bottleneck.** At 3 s and 5 s cadence the lag stayed between 10.4 s and 13.4 s while the polling window was only 3 to 5 s, so X took at least 7 to 10 s to expose each post after its own timestamp. At 15 s cadence the lag was no worse (11.5 s and 13.3 s) and cannot be decomposed. The route therefore has a floor of roughly 10 to 13 s that no polling rate removes; going faster than 5 s buys at most a second or two.
3. **Recommended cadence: 5 s** (180 calls per 15-minute window against a quota of 900; cost unaffected because empty calls are free). 3 s is affordable but gains nothing measurable.
4. **Implication for ADR-008 (10 to 15 s publication to broker acknowledgement).** The source alone consumes 10 to 13 s on this route, before parsing, preflight and submission. The end-to-end objective needs re-basing to roughly receipt plus 3 s, or about 15 s, unless the publisher feed (path A) proves faster. The signal TTL for D-05 is unaffected: a 90 s source-age limit still passes with 75 s to spare.
5. **Caveats.** Five posts in one session; X stamps posts at whole seconds, so each lag carries about plus or minus one second; the local clock is assumed NTP-synchronised. Two or three more sessions at 5 s would confirm the floor for a few cents each.

## Operational lessons

1. The OAuth 2.0 Client ID must be copied exactly; a single transcribed character (digit 1 versus lowercase l) produced X's generic "Something went wrong" authorization page with no further detail. The token endpoint's `invalid_client` error is the fast diagnostic.
2. Confidential-client app type with PKCE and callback `http://127.0.0.1:8765/callback` works for a local tool.
3. Media download is the slow part of a backfill (about 10 minutes for 991 images); posts alone take about a minute.
4. Access tokens last two hours; the stored refresh token (`offline.access`) renews them without a browser. The two-hour poll crossed one refresh without incident.
5. `GET /2/usage/tweets` with the app-only bearer token returns the project's billable-read counter, which makes incremental cost measurable rather than estimated.

## What this changes

- Spec section 3 source path B (official X API) is **certified for access** for this account. Path A (publisher feed) is no longer required for feasibility, though it would still improve latency if offered.
- The parser evaluation corpus is now API text, not OCR: relabel `private_samples/tkl_100` from it and extend labels across the history.
- G0 is complete. Poll at 5 s in production; treat 10 to 13 s as the source floor; re-base the end-to-end latency objective (ADR-008) around it; keep the 90 s source-age limit (D-05).
- Optional follow-up: two or three more sessions at 5 s to tighten the estimate of the X-side floor.
