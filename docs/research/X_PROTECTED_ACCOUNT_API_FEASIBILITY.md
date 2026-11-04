# X protected-account read access: feasibility (G0 pre-study)

**Date:** 2026-11-05 · **Owner:** Agent B (research) · **Status:** DESK RESEARCH, G0 EXPERIMENT NOT YET RUN
**Scope:** official, authorized, machine-readable read access to posts (text, images, replies, edits) from the protected X account `@TKLSubscribers` by a paying TKL subscriber whose own X account is an approved follower. No scraping, no visibility bypass. Vendor documentation reviewed 2026-11-05; recheck at G0.

> Note for the spec: `Signal_to_Execution_Project_Specification_v0.1` section 3 analyses an X *Community*. The actual source (ADR-001) is a *protected account*. Community-search endpoints are irrelevant; the analysis below replaces spec section 3 for this source.

## 1. Verdict

**Feasible with conditions.** Confidence: medium-high on the technical path, medium on the contractual path.

- X API v2 documents a user-context path that is explicitly scoped to protected content. The `tweet.read` scope is defined as *"All the Tweets you can view, including Tweets from protected accounts."* and `users.read` as *"Any account you can view, including protected accounts."* [S1]
- `GET /2/users/:id/tweets` (user post timeline) accepts OAuth 2.0 user tokens with `tweet.read users.read`, returns media via `expansions=attachments.media_keys&media.fields=url,variants`, includes `edit_history_tweet_ids` by default, and is rate-limited at 900 requests / 15 min per user. [S2][S3][S4]
- Filtered stream and the Enterprise firehose are documented as **public posts only**; filtered stream is bearer-token only, so user context cannot even be supplied. They are not a route. [S5][S6][S7]
- No page on docs.x.com states per endpoint that protected posts are returned to approved followers. The scope text is the only explicit statement. **A one-day G0 experiment (section 6) is required before any build decision.**
- Pricing is pay-per-use since 6 Feb 2026: $0.005 per post read, deduplicated per 24-hour UTC day, 3M reads/month cap, no subscription. Expected cost for this source is a few dollars per month. [S8][S9]
- The main exposure is contractual, not technical: TKL's own terms describe the feed as a revocable "bonus perk" and restrict storage and reproduction (section 5).

## 2. Endpoint assessment

| Endpoint | Returns protected posts to an approved follower? | Auth | Media / edits | Rate limit (per user / per app) | Source |
|---|---|---|---|---|---|
| `GET /2/users/:id/tweets` user post timeline | **Implied yes** via `tweet.read` scope. Not stated per endpoint. UNVERIFIED until G0. | OAuth 2.0 user token (`tweet.read users.read`) or OAuth 1.0a user token. App-only bearer will not see protected content. | Yes. `attachments.media_keys`, `media.fields=url,preview_image_url,variants,type`; `edit_history_tweet_ids` default; `tweet.fields=edit_controls`. Includes replies unless `exclude=replies`. | 900 / 15 min user; 10,000 / 15 min app | [S2][S3][S4] |
| `GET /2/users/:id/timelines/reverse_chronological` home timeline | Plausible (followed protected accounts appear in a home timeline by product design). UNVERIFIED. | User context only. | Same fields. 3,200 posts / 7 days window. | 180 / 15 min user | [S10][S3] |
| `GET /2/lists/:id/tweets` posts from a private List containing the account | Plausible. UNVERIFIED. | OAuth 2.0 user (`list.read tweet.read users.read`). | Same fields. | 900 / 15 min | [S11] |
| `GET /2/tweets/search/recent` | docs.x.com makes no statement either way. Treat as public-only. Do not rely on it. | user or bearer | Same fields. 7-day window. | 300 / 15 min user | [S12] |
| `GET /2/tweets/search/stream` filtered stream | **No.** "filter a stream of public Posts". Spec security is BearerToken only. | bearer only | n/a | P99 delivery ≈ 4–5 s (public only) | [S5][S6][S13] |
| Enterprise firehose / resellers | **No.** "100% of public posts". Developer Policy also bars serving one user's token-derived content to another person, so no vendor can lawfully supply this feed. | n/a | n/a | custom | [S7][S14] |
| Mentions, bookmarks | Not a feed of the publisher's posts. | — | — | — | — |

Token lifetime: access tokens from the PKCE flow are valid for two hours; a refresh token is issued only with the `offline.access` scope. Unattended operation therefore needs `offline.access` and a refresh loop. [S1]

Edits: posts can be edited within 30 minutes of posting, up to 5 times; timeline endpoints return the most recent version; the edit chain is exposed through `edit_history_tweet_ids`. This maps directly onto the spec's `revision_key` requirement. [S4]

## 3. Pricing and access tiers (as of 2026-11-05)

| Tier | Terms | Source |
|---|---|---|
| Pay-per-use (default since 6 Feb 2026) | Posts read $0.005 each, user lookup $0.010 each. "All resources are deduplicated within a 24-hour UTC day window" (stated as a soft guarantee). Capped at 3 million post reads per monthly billing cycle. "No contracts, subscriptions, or minimum spend." Owned Reads ($0.001) apply only when `{id}` is the app owner's own account, so they do not apply to reading `@TKLSubscribers`. | [S8] |
| Legacy Free | Discontinued for new sign-ups 6 Feb 2026. | [S9] |
| Legacy Basic / Pro | Changelog says they "stay available" to existing subscribers. Third-party reports of forced migration to pay-per-use in mid-2026: UNVERIFIED. The pricing page no longer mentions them. | [S9] |
| Enterprise | Custom pricing; public posts only; irrelevant to this source. | [S7] |

Rate limits and billing are separate. [S3]

Cost model for this source: cost ≈ unique posts returned per day × $0.005, because of the daily dedup. Polling with `since_id` keeps responses small. A publisher posting 20–50 times a day costs well under $10 per month.

## 4. Freshness and polling budget

- There is no documented freshness SLA for timeline endpoints. Effective lag = poll interval + X-side propagation (unknown, measure in G0).
- 900 / 15 min on the user timeline allows one call per second; a 10–15 s poll is comfortably within limits and leaves headroom for backfill and the home-timeline or List control paths.
- Filtered stream's documented P99 of 4–5 s applies to public posts only and is not available here. The spec's latency assumptions in section 17.2 should be re-based on measured poll lag.

## 5. Terms and policy clauses that matter

**X Terms of Service** (dated 9 October 2026; x.com/en/tos could not be fetched directly, wording taken from a third-party capture, UNVERIFIED verbatim) [S15]
- Prohibits accessing the service "by any means (automated or otherwise) other than through our currently available, published interfaces" and "crawling or scraping the Services in any form, for any purpose without our prior written consent".
- The official API is a published interface, so API polling is not scraping. Browser automation would be.

**X Developer Agreement** (updated 27 April 2026) [S16]
- No selling, sublicensing, redistributing or creating derivative works of X Content; no use to train or fine-tune models.
- "will not attempt to exceed or circumvent limitations on access, calls, and use of the X API."
- Pay-per-use is "designed for hobbyists, commercial prototyping, initial development"; use beyond that scope requires Enterprise. A single-user personal integration sits inside this language; a multi-user product would not.
- If content "gains protected status, or is otherwise ... modified, or removed", licensees must make reasonable efforts to delete or modify stored copies.
- No clause addresses financial or trading use.

**X Developer Policy** (no date shown) [S14], verified verbatim 2026-11-05
- "You must respect the protected and blocked status of all X Content."
- "A protected account's content is only available to people who have been approved by the owner to follow that account."
- "You may not serve content obtained using one person's authentication token to a different person".
- "If you store X Content offline, you must keep it up to date with the current state of that content on X."
- Redistribution outside the API is limited to Post IDs and User IDs.

**TKL terms and conditions** [S17], verified verbatim 2026-11-05
- "Access to The Kobeissi Letter's private Twitter feed (@TKLSubscribers) is considered a bonus perk which we reserve the right to remove members from should they display behavior which is damaging to the community." Removal "is at the discretion of TKL Inc. and no refunds will be permitted should it occur."
- "No part of this publication or its contents may be further transmitted, or otherwise reproduced, stored, disseminated, transferred, or used, in any form or by any means, except as permitted with prior written permission."
- "No license is granted to the user except for the user's personal use."
- No clause mentions automation, bots or software.

**TKL disclaimer** [S18]: contents may not be "copied, downloaded, stored in a retrieval system"; the letter is "not intended to be personalized recommendations to buy, hold or sell investments."

Reading: storing posts in a bot database and acting on them automatically is not expressly prohibited, but "stored ... except as permitted with prior written permission" and "personal use" give TKL grounds to revoke the follow. This is a product-owner decision (spec D-07). Recommended posture: single subscriber, personal account, no redistribution, minimal retention (IDs, hashes, ephemeral text), and written confirmation from TKL that personal automated processing is acceptable.

## 6. G0 feasibility experiment (ready to run)

Prerequisites: the subscriber's X account is already an approved follower of `@TKLSubscribers`; a developer app in the X Developer Console with OAuth 2.0 enabled and a small pay-per-use credit top-up (budget: under $20 for the day).

1. OAuth 2.0 Authorization Code + PKCE, scopes `tweet.read users.read offline.access` (add `list.read` for the List control). Log in as the subscriber. Store access and refresh tokens in a local secrets store, never in the repo.
2. `GET /2/users/by/username/TKLSubscribers?user.fields=protected,id` with the user token. Record `id` and confirm `protected=true`.
3. Poll `GET /2/users/{id}/tweets?max_results=100&tweet.fields=created_at,edit_controls,edit_history_tweet_ids,referenced_tweets,attachments,conversation_id,in_reply_to_user_id,author_id&expansions=attachments.media_keys,referenced_tweets.id&media.fields=url,preview_image_url,variants,type` with `since_id`, every 15 s for at least 2 hours during a US market session.
4. Controls: (a) the same request with the app bearer token, expect no protected data (401/403 or empty); (b) `GET /2/tweets/search/recent?query=from:TKLSubscribers` with the user token, record whether anything returns; (c) optional home-timeline and private-List polls to compare arrival times.
5. Record per call: HTTP status, `x-rate-limit-remaining` and `x-rate-limit-reset`, post IDs, `created_at` versus local receive time (lag), presence of `includes.media[].url`, changes in `edit_history_tweet_ids` after any edit, reply and thread linkage, and credit spend per hour from the console.
6. Download one image from a returned `media.url` with the user token to confirm media is retrievable without a browser session.

**Pass:** user-token calls return posts whose `author_id` equals the protected account's ID, with media URLs and edit IDs; the bearer control returns none; median lag under 30 s; spend consistent with the dedup rule. All known test-window posts accounted for.
**Fail:** empty data or 403 under the user token; media URLs not retrievable; or the follow is revoked after querying (watch for removal during the run).

Record results in `docs/research/G0_RESULTS.md` with raw response samples redacted of post text.

## 7. Implications for the source adapter contract (spec section 6.1)

- `origin_platform = X`, `provenance_method = API_COMMUNITY_FIELD` does not fit. Propose a new enum value `API_USER_CONTEXT_PROTECTED` with eligibility = (`author_id` == allowlisted protected account ID) AND (`user.protected == true` at observation) AND (token principal == the approved subscriber account).
- `revision_key` = latest ID in `edit_history_tweet_ids`; each new ID is a new immutable revision.
- `license_tag` = `TKL_PERSONAL_NONREDISTRIBUTABLE`; retention policy must honour the Developer Policy "keep it up to date" and Developer Agreement deletion duties.
- Freshness TTL must be set from measured poll lag, not from the filtered-stream figure.

## 8. Unknowns and risks

- UNVERIFIED: per-endpoint protected-post behaviour; search and List behaviour; Basic/Pro migration dates; help.x.com wording (page returned 403).
- Single-account dependence: if the subscriber's follow is revoked, the source is gone with no alternative. TKL offers no email, RSS, API, webhook or chat channel for intraday updates; the protected X account is the only one. [S19]
- Developer Agreement scope language for pay-per-use could be read against a live trading integration; the personal, single-user posture is the strongest position.
- Soft guarantees on dedup and the 3M monthly cap.

## Sources

- [S1] https://docs.x.com/resources/fundamentals/authentication/oauth-2-0/authorization-code (scopes table, token lifetime; verified verbatim 2026-11-05)
- [S2] https://docs.x.com/x-api/posts/user-posts-timeline-by-user-id
- [S3] https://docs.x.com/x-api/fundamentals/rate-limits
- [S4] https://docs.x.com/x-api/fundamentals/edit-posts
- [S5] https://docs.x.com/x-api/webhooks/stream/
- [S6] https://docs.x.com/x-api/stream/stream-filtered-posts.md
- [S7] https://docs.x.com/enterprise-api/getting-started/about-x-api
- [S8] https://docs.x.com/x-api/getting-started/pricing (verified verbatim 2026-11-05)
- [S9] https://docs.x.com/changelog
- [S10] https://docs.x.com/x-api/posts/user-home-timeline-by-user-id
- [S11] https://docs.x.com/x-api/posts/list-posts-timeline-by-list-id
- [S12] https://docs.x.com/x-api/posts/recent-search
- [S13] https://docs.x.com/x-api/posts/filtered-stream/introduction
- [S14] https://docs.x.com/developer-terms/policy (verified verbatim 2026-11-05)
- [S15] https://x.com/en/tos (not fetchable from this environment; quotes via https://conductatlas.com/platform/x/x-terms-of-service/anti-scraping-and-acceptable-use-restrictions/ , UNVERIFIED verbatim)
- [S16] https://docs.x.com/developer-terms/agreement
- [S17] https://thekobeissiletter.com/legal/terms-and-conditions (verified verbatim 2026-11-05)
- [S18] https://thekobeissiletter.com/legal/disclaimer
- [S19] https://thekobeissiletter.com/pricing
