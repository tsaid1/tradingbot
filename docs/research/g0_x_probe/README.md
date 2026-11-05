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
python docs/research/g0_x_probe/x_probe.py poll --interval 15 --duration 7200 --fetch-media
python docs/research/g0_x_probe/x_probe.py report data/g0/poll_<timestamp>.jsonl
```

Run `poll` during a US market session so posts actually arrive. `--duration 7200` is two hours; 480 calls at 15 s is well inside the 900-per-15-minute limit. Post reads are billed at $0.005 each, deduplicated per UTC day, so cost is roughly the number of unique posts seen that day.

Add `--keep-text` only for a short manual look at parsing-relevant content, and delete that log afterwards. TKL's terms restrict storage of their content; the default log keeps only hashes, lengths and IDs.

## 3. What gets recorded

Per call: UTC time, HTTP status, elapsed, `x-rate-limit-remaining` and reset, `since_id`, result count, any API errors.
Per post: ID, author ID, `created_at`, lag in seconds (receive time minus `created_at`), SHA-256 and length of text, reply flag, referenced-post types, media count and types, whether each media item carries a URL, number of edit-history IDs, edits remaining. With `--fetch-media`, the status and byte count of one media download per call.

## 4. Pass / fail (printed by `report`)

| Criterion | Pass |
|---|---|
| Protected posts returned under the user token | at least one post whose `author_id` equals the target's ID |
| Media URLs retrievable | every post with attachments has a URL for each media item; `--fetch-media` returns 200 |
| Freshness | median lag under 30 s at a 15 s poll |
| Reliability | no 4xx/5xx on timeline calls |
| Bearer control | app-only bearer token returns no posts (401, 403 or empty) |
| Search control | informational; expected empty |

A 401 or 403 on the user-token timeline is a **FAIL** for route B (official X API) and means the publisher-feed path (route A in the spec) is the only remaining option. Also watch whether the subscriber's follow is revoked during or after the run; that is a contractual signal, not a technical one.

## 5. Record the result

Write the `report` output and the key observations into `docs/research/G0_RESULTS.md`. Do not commit anything from `data/g0/`.

## Offline tests

```bash
python -m pytest docs/research/g0_x_probe -q
```

These cover PKCE, lag, `since_id` handling, redaction, summary and verdict logic. They are not in the CI `testpaths` (that is Agent A's `pyproject.toml`); run them locally.
