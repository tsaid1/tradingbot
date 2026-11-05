# G0 probe: official X API read access to `@TKLSubscribers`

Runs the feasibility experiment described in `../X_PROTECTED_ACCOUNT_API_FEASIBILITY.md` section 6. Research tooling only. No trading code, no broker, standard library only.

**Budget:** one US market session, under $20 of X pay-per-use credit.
**Prerequisite:** the subscriber's X account is already an approved follower of `@TKLSubscribers`.

## 1. One-time setup (about 15 minutes)

1. Sign in to https://console.x.com with the **subscriber's** X account and create a Project and an App.
2. In the App's **User authentication settings**: enable OAuth 2.0, type **Web App** (or Native App), callback URI `http://127.0.0.1:8765/callback`, website URL anything. Save the **Client ID** (and Client Secret if one is issued).
3. Under **Keys and Tokens**, also copy the app **Bearer Token** (used only as a negative control).
4. Add a small pay-per-use credit balance.
5. Export the values in your shell. Never write them into the repo or `.env.example`:

```bash
export X_CLIENT_ID=...
export X_CLIENT_SECRET=...      # omit if the console did not issue one
export X_BEARER_TOKEN=...       # control only
```

All output goes to `data/g0/` which is git-ignored.

## 2. Run (from the repo root)

```bash
python docs/research/g0_x_probe/x_probe.py auth                     # browser login as the subscriber
python docs/research/g0_x_probe/x_probe.py lookup TKLSubscribers    # expects protected: true
python docs/research/g0_x_probe/x_probe.py controls                 # bearer timeline + user search
python docs/research/g0_x_probe/x_probe.py poll --phases 15:3600,5:1800,3:1800 --fetch-media \
    --start-at 2026-11-05T14:30:00Z --extend-until 2026-11-05T21:00:00Z --min-posts 3
python docs/research/g0_x_probe/x_probe.py report data/g0/poll_<timestamp>.jsonl --json data/g0/poll_summary.json
```

Run `poll` during a US market session so posts actually arrive. `--phases` takes `interval:duration` pairs in seconds and runs them back to back (the plain `--interval`/`--duration` pair is a single phase). Before each phase the probe reads the `x-rate-limit-limit` header from the previous call and slows the phase down if the requested cadence would use more than 80% of the 15-minute quota. `--start-at` sleeps until a UTC time; `--extend-until` keeps polling at `--extend-interval` after the schedule when fewer than `--min-posts` new posts by the target arrived, so an empty session is extended rather than reported as low latency. Post reads are billed at $0.005 each, deduplicated per UTC day, so cost is roughly the number of unique posts seen that day; empty calls are free.

The negative control runs inside the poll: whenever a call delivers a new post, the same request is repeated with the app-only bearer token taken from `X_BEARER_TOKEN` in the environment (never from a file in the repo), and only the status, post count and error type are logged. The token itself is never written anywhere.

Add `--keep-text` only for a short manual look at parsing-relevant content, and delete that log afterwards. TKL's terms restrict storage of their content; the default log keeps only hashes, lengths and IDs.

## 3. What gets recorded

Run: a `baseline` record (newest existing post ID, used as the first `since_id`; its posts are not lag samples), a `phase` record per interval (requested and effective interval, quota seen), a `reconcile` record at the end (every post X reports since the baseline versus what polling delivered: `missed_ids`, `vanished_ids`), and `usage` records before and after from `GET /2/usage/tweets` (X's own billable-read counter, read with the bearer token) so the incremental cost is metered rather than estimated.

Per call: send and receive time (UTC), HTTP status, elapsed, every `x-rate-limit-*`, `x-app-limit-*` and `x-user-limit-*` header, `since_id`, result count, number of new posts, any API errors, and the bearer-control result when new posts arrived. Calls are sent on a fixed cadence (next send = previous send + interval), so request frequency is the interval, not interval plus request time.

Per post: ID, author ID, `created_at`, `lag_s` (response received minus `created_at`), `poll_window_s` (previous send to this receipt: the most the polling wait can explain), `x_delay_floor_s` (how long the post already existed when the previous call was sent and did not return it: a lower bound on X's own exposure delay), `duplicate` and `edit_of_seen` flags, SHA-256 and length of text, reply flag, referenced-post types, media count and types, whether each media item carries a URL, number of edit-history IDs, edits remaining. With `--fetch-media`, every media item of each new post is downloaded once (bytes discarded) and its status, size, elapsed and `media_lag_s` (media received minus `created_at`) are recorded.

`report` prints P50/P95/P99 lag, the polling-wait and X-side decomposition, duplicates, reconciliation, request frequency and quota headers per interval, media timing, bearer-control results and the metered cost; `--json` writes the same summary to a file. Note that `created_at` has one-second resolution, so individual lags carry about plus or minus one second of quantisation.

Long posts: most publisher posts exceed 280 characters, so the probe requests the long-post field (`note_tweet`, falling back to `post.fields` / `note_post` if the API rejects the older naming) and hashes the full body. The report shows how many posts carried a long body and which parameter naming the API accepted. A G0 run where long bodies never appear is a fail for signal completeness.

## 4. Pass / fail (printed by `report`)

| Criterion | Pass |
|---|---|
| Protected posts returned under the user token | at least one post whose `author_id` equals the target's ID |
| Media URLs retrievable | every post with attachments has a URL for each media item; `--fetch-media` returns 200 |
| Freshness | median lag under 30 s at a 15 s poll; P95/P99 and the X-side floor reported alongside |
| Reliability | no 4xx/5xx on timeline calls |
| Completeness | no duplicate deliveries; end-of-run reconciliation finds no missed posts |
| Bearer control | app-only bearer token returns no posts (401, 403 or empty), both standalone (`controls`) and in-run |
| Cost | metered reads during the run consistent with the daily dedup rule (informational) |
| Search control | informational; expected empty |

A 401 or 403 on the user-token timeline is a **FAIL** for route B (official X API) and means the publisher-feed path (route A in the spec) is the only remaining option. Also watch whether the subscriber's follow is revoked during or after the run; that is a contractual signal, not a technical one.

## 5. Build the evaluation corpus (backfill)

The same endpoint pages backwards through the account's most recent 3,200 posts, 100 per call, so one session yields years of history with full text, timestamps, media URLs, quote links and edit history:

```bash
python docs/research/g0_x_probe/x_probe.py backfill --start-time 2025-01-01T00:00:00Z --download-media
python docs/research/g0_x_probe/x_probe.py corpus data/g0/corpus/posts_<timestamp>.jsonl
```

- Output: `data/g0/corpus/posts_<timestamp>.jsonl`, one full post per line, plus chart images under `data/g0/corpus/media/` when `--download-media` is set. **This is paid publisher content stored locally for personal evaluation only. It is git-ignored; never commit or share it.**
- Cost: about $0.005 per post read, so the full 3,200-post window is roughly $16. 32 pages at the default one-second pause takes under a minute.
- Interrupted or capped runs resume with `--resume`; `--max-posts` and `--until-id` bound a run; `--start-time` / `--end-time` take ISO-8601 UTC.
- The `corpus` summary reports posts per month, how many carried media, long bodies, quotes, replies and edits, and the author IDs seen (expect exactly one: the target).

Run `backfill` after `lookup` and before or after `poll`; it is the quickest confirmation that protected posts come through at all.

## 6. Record the result

Write the `report` output and the key observations into `docs/research/G0_RESULTS.md`. Do not commit anything from `data/g0/`.

## Offline tests

```bash
python -m pytest docs/research/g0_x_probe -q
```

These cover PKCE, lag and its decomposition, `since_id` handling, redaction, phase parsing and quota-aware intervals, summary and verdict logic, backfill paging, the auth flow and an end-to-end poll against a fake clock and fake API. They are not in the CI `testpaths` (that is Agent A's `pyproject.toml`); run them locally.
