# Vocabulary reviewer (one trusted outside reviewer)

A separate, sanitized page on which ONE trusted reviewer may **recommend** a classification for
each proposed word. The reviewer cannot change the lexicon, the trading rules or the batch: the
owner sees the recommendations on the owner page (*Vocabulary Review*, see
[RUNTIME.md](RUNTIME.md), "Vocabulary review (owner only)") and still decides and applies every
word. Building and running the page locally enables no outside access: publishing it through an
identity-aware proxy is **the owner's separate decision**.

## What the reviewer sees and cannot do

* Per proposal, as plain text only (never Markdown or HTML): the word, its kind, the held-post and
  hold counts, the families and public reason codes, the proposed group and family, and the
  warning "seen with a price or a trading word; may be another market".
* Never: a meaning, an example sentence or any post text, measured counts, post, source or action
  ids, dates, paths, the review log, the lexicon, the config, the holdout or any dataset. A
  proposal whose word is not a short plain word (at most 4 parts, 48 characters, no markup
  characters) is withheld from the export, so a sentence-sized "token" never leaves.
* The reviewer chooses *recommend accept* (into one of the three groups that have a list, with the
  family when the group is market names), *recommend reject*, *recommend keep holding* or *needs
  context*, with an optional note of at most 500 characters, and submits per proposal. A later
  submit supersedes the earlier one; both stay in the log.
* The page has no file browser, upload, download, path input, lexicon, config, broker or order
  code. It imports only `streamlit` and `tradingbot.runtime.vocab_recommend` (which imports the
  path guards of `vocab_review`, `config` and `models`, never the service, mapping, execution,
  `ibkr`, `ibkr_orders`, storage or validator modules: tested in a subprocess). Errors show a
  constant, never a path or a traceback.
* It refuses to run unless bound to `127.0.0.1`, with usage statistics off, CORS and XSRF
  protection on, the Streamlit agent API off, static file serving off and
  `client.showErrorDetails = "none"`, and no `*` in `server.allowedHosts` or
  `server.corsAllowedOrigins`.

## Modes and identity

The same two modes as the adjudication workspace
([ADJUDICATION_WORKSPACE.md](ADJUDICATION_WORKSPACE.md), "Modes and identity"), configured, never
guessed from headers:

* **LOCAL** (default): identity from `TRADINGBOT_REVIEW_LOCAL_IDENTITY` (none: a read-only
  preview, "submissions are disabled"). A request with any forwarding, proxy or identity header
  is refused outright; `Host` and `Origin` must be this server's own loopback address and port.
* **PUBLISHED** (`TRADINGBOT_REVIEW_PUBLISHED_HOST=<host>`): identity only from the proxy's login
  header `TRADINGBOT_REVIEWER_LOGIN_HEADER` (default `Tailscale-User-Login`; name header
  `TRADINGBOT_REVIEWER_NAME_HEADER`, default `Tailscale-User-Name`); a request without it is not
  authorized. `Host` and `Origin` must be exactly `<host>` and `https://<host>`. It refuses to
  start with `TRADINGBOT_REVIEW_LOCAL_IDENTITY` set, or without `--server.allowedHosts <host>` and
  `--server.corsAllowedOrigins https://<host>`.

A header sent twice is refused. `TRADINGBOT_REVIEWER_ALLOWED_LOGINS=reviewer@example.com[,...]`:
any other login, and a request without a login, sees only "Not authorized"; **set it** when the
page is published. `TRADINGBOT_REVIEWER_IDENTITY` other than `tailscale` refuses everyone: the
seam for a Cloudflare Access variant, which would validate the signed `Cf-Access-Jwt-Assertion`
token (signature, audience, issuer, expiry) in `vocab_recommend.identity_from_headers`, not
implemented.

The header is trusted because only the proxy reaches the page. Anyone who can connect to the port
directly (another account on this Mac, a local process) can send any header: run it on a machine
(or account) only the owner uses. Use Tailscale **Serve** (tailnet only, sets the identity
headers), never **Funnel** (public, no identity).

## Commands (owner, locally)

```bash
export PYTHONPATH="$PWD/src"
export TRADINGBOT_VOCAB_REVIEWER_EXPORT="$HOME/tradingbot-reviewer/export.json"  # private dir, not synced

# 1. Export the proposals the owner page shows (same runtime config and proposals file):
python scripts/vocab_reviewer_export.py            # or --out PATH; prints counts and paths only

# 2. Start the reviewer page, LOCAL mode:
TRADINGBOT_REVIEW_LOCAL_IDENTITY="reviewer@example.com" \
streamlit run dashboard/reviewer/reviewer_app.py --server.address 127.0.0.1 \
  --server.port 8502 --browser.gatherUsageStats false --server.headless true \
  --client.showErrorDetails none
#    or PUBLISHED mode (behind Tailscale Serve; the owner's separate decision):
TRADINGBOT_REVIEW_PUBLISHED_HOST=reviewer.<tailnet>.ts.net \
TRADINGBOT_REVIEWER_ALLOWED_LOGINS="reviewer@example.com" \
streamlit run dashboard/reviewer/reviewer_app.py --server.address 127.0.0.1 \
  --server.port 8502 --browser.gatherUsageStats false --server.headless true \
  --client.showErrorDetails none --server.allowedHosts reviewer.<tailnet>.ts.net \
  --server.corsAllowedOrigins https://reviewer.<tailnet>.ts.net

# 3. Stop it: Ctrl-C in that terminal (or kill the process listening on 127.0.0.1:8502).

# Verify the recommendations log:
python -c "import os; from tradingbot.runtime.vocab_recommend import recommendations_path as p, \
verify_log as v; r = v(p(os.environ['TRADINGBOT_VOCAB_REVIEWER_EXPORT'])); print(r.ok, r.count, r.problem)"
```

The export, the log and the owner's audit are refused inside this repository (the existing
private-path guards) and written mode 0600. Re-export after the proposals change; a
recommendation whose proposal id no longer matches is simply not shown on the owner page.

## The recommendations log

`<export stem>.recommendations.jsonl` beside the export (for `export.json`:
`export.recommendations.jsonl`), schema `tradingbot.vocab_recommendation/1`, append-only, one line
per submit: `seq`, UTC `at`, `reviewer` (`login`, `name`), `proposal_id`, `token` (from the
export, never from the request), `recommendation` (`verdict`, `group`, `family`), `note` and
`prev_sha256` (the sha256 of the previous line's bytes; 64 zeros for the first). `verify_log`
checks the schema, the sequence and the chain (an edit of line n is reported at line n+1, for
example `CHAIN_BROKEN at line 2`), and the head file `<log>.head` (line count and the last line's
sha256, replaced atomically after every append), so an edit or removal of the LAST line is
reported too (`HEAD_MISMATCH`). The chain has no key: whoever can write both files can rewrite
them consistently. A log that fails verification blocks new submits and is not shown to the
owner. Notes are stored and shown as one plain line (control, line-break and direction-override
characters become spaces). It is the
reviewer page's only write: with the log unwritable a submit fails closed and nothing else is
written (tested).

## On the owner page

With `TRADINGBOT_VOCAB_REVIEWER_EXPORT` set and the log present, each proposal shows one
plain-text line per reviewer with the latest recommendation, note and time. The verdict controls
are never pre-set by it. *Apply batch* then appends one line (`tradingbot.vocab_reviewer_audit/1`)
to `<lexicon stem>.reviewer-audit.jsonl` beside the lexicon: the time, the result and backup, the
log head, and per decided word the owner's decision (verdict, group, family) and the
recommendations shown with it (login, verdict, group, family, log line). Without the log the
owner page is exactly as before (tested).

## Behind a reverse proxy on another host name

The page is reached at, say, `https://reviewer.<tailnet>.ts.net` while Streamlit listens on
`127.0.0.1:8502`. Needed:

* The proxy must forward WebSocket upgrades on `/_stcore/stream` (Tailscale Serve does) and must
  set (and strip any client-sent copy of) the identity headers on that WebSocket request: the page
  reads them from it.
* Keep `server.enableCORS` and `server.enableXsrfProtection` **true** (the page refuses otherwise:
  a page the reviewer visits must not drive this one with the reviewer's identity). PUBLISHED
  mode requires `--server.corsAllowedOrigins https://reviewer.<tailnet>.ts.net` and
  `--server.allowedHosts reviewer.<tailnet>.ts.net` (DNS-rebinding protection), and the page
  itself requires `Host` and `Origin` to be exactly that host: Streamlit alone would also accept
  any `localhost` origin.

Verified locally on 127.0.0.1 (Streamlit 1.67, no proxy): a WebSocket from a foreign origin is
refused (403); with `server.corsAllowedOrigins` set, that origin is accepted and others still
refused; with `server.allowedHosts` set, a WebSocket with a listed Host is accepted
and one with another Host refused (403). **Not verified without the proxy:** that
Tailscale Serve forwards the WebSocket, which Host and Origin it sends, that it sets and strips
`Tailscale-User-Login`/`-Name` on the WebSocket request, and the page end to end in a browser
through it (the identity handling is tested headlessly with the header injected).
