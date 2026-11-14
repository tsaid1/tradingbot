# Trading signal adjudication workspace

A stand-alone, loopback-only Streamlit app in which reviewers record what each trading post
really instructs, and an approver settles it, to make a versioned ground truth for evaluating the
interpreter. It implements the adjudication workspace specification version 1.2 (app side). It is the single system for vocabulary, post-level and
trading-idea review: the remote reviewer interface is this app behind Tailscale Serve, once the
owner activates that separately.
**It is not an execution path.** It has no broker, order, mapping-approval, lexicon or risk-policy
write path and imports none of those modules (tested in a subprocess). Its only write is the
annotation log. Publishing it through any proxy is **the owner's separate decision**; nothing
here enables remote access.

Code: `src/tradingbot/runtime/adjudication.py` (pure), `dashboard/reviewer/adjudication_app.py`
(the app), `scripts/adjudication_freeze.py`, `scripts/adjudication_demo_bundle.py`. It reuses the
reviewed vocabulary-reviewer parts ([VOCAB_REVIEWER.md](VOCAB_REVIEWER.md)): proxy-header identity,
the chained append-only log, private-path guards and the same server refusals.

## Files (private; outside the repository and outside synced folders)

`TRADINGBOT_ADJUDICATION_DIR` (normally `~/.local/share/tradingbot-private/adjudication/`):

| path | written by | content |
| --- | --- | --- |
| `bundle/` | the private export (data side) | `manifest.json`, `posts.jsonl` (the only publisher text), `media/`, `sequences.jsonl`, `items.jsonl` |
| `reviewers.toml` | the owner, by hand | who may sign in, role and access, `remote_full_context_authorized` |
| `annotations.jsonl` | the app (only write) | schema `tradingbot.adjudication.annotation/1`, chained |
| `versions/ground_truth_v<N>.jsonl` + `.manifest.json` | the freeze command | resolved readings and their sha256 |

Every file is read only through the private-path guards: refused inside this repository, never
through a link, link count 1, mode 0600 (directories 0700). A bundle with an unknown schema, a
value outside a closed set (`post_label`, `action`, `side`, `effect`, price `role`,
`exactness`, `contract_month`, `timing`, `holds.status`, families = `manifest.families` plus
OTHER and UNKNOWN), a broken `review_order`, a level pointing outside the post's `numbers`, a
media name that is not a plain image file name, or a reference to a post outside the bundle is
refused with a code, never a path or content.

**Price roles** (the owner's taxonomy, for every level and for `entry.role`): CURRENT_MARKET (a
current market observation), INTENDED_ENTRY (an explicit intended entry price), HISTORICAL_FILL,
AVERAGE_OR_COST_BASIS, STOP, TARGET, CHART_LEVEL (support or resistance), OTHER_CONTRACT_MONTH,
UNRESOLVED; in the reviewer's table also NOT_A_PRICE; for `entry.role` also NOT_STATED.
`exactness`: EXACT, APPROXIMATE (an approximate quotation is any role marked APPROXIMATE),
NOT_APPLICABLE. `contract_month`: FRONT, NEXT, NAMED, NOT_STATED, NOT_APPLICABLE.

```toml
# reviewers.toml (chmod 600)
remote_full_context_authorized = false   # true only after the publisher's permission
scope_sequences = 20                     # the first 20 balanced ideas; raise to cover all
[[reviewer]]
login = "you@example.com"
name = "Owner"
role = "APPROVER"        # REVIEWER or APPROVER
access = "FULL"          # FULL or RESTRICTED
```

## Who sees and does what

* **Not listed** in `reviewers.toml`, or no identity: "Not authorized" and nothing else.
* **FULL**: (1) the source evidence (the post, its images, the earlier posts of the idea and the
  recent context, in time order), (2) the reader's form, (3) the system's reading (`ai`, `holds`,
  `mapping`).
* **RESTRICTED**: never loads `posts.jsonl` or `media/` (no code path opens them; tested with the
  files unreadable): areas (2) and (3) only, plus the sanitized vocabulary page.
* **Through the proxy**: RESTRICTED whatever the entry says while
  `remote_full_context_authorized` is false.
* **REVIEWER** writes INDEPENDENT and AFTER_REVEAL records. With *blind-first* on (the default,
  a sidebar switch) area 3 stays hidden until the reviewer saves an INDEPENDENT reading; it is
  then shown beside the reader's form with a per-field verdict (CONFIRM / CORRECT / UNSURE)
  pre-filled from the comparison and editable, saved as AFTER_REVEAL (`blind: true`, referring to
  the independent record). With the switch off, the form starts from the system's reading and
  only AFTER_REVEAL is written (`blind: false`). The **price table** (instruction items) has one
  row per number of the post's `numbers` (FULL; shown as the token) or per level the system
  typed (RESTRICTED: no text), each with a role, an exactness and a contract month (always
  asked). Every row starts unanswered in a blind reading, never filled from the system; after
  reveal the system's answer stands beside each row with its own CONFIRM / CORRECT pre-fill (an
  unanswered row: UNSURE). A number the system typed no level for counts as NOT_A_PRICE in its
  reading. Any field, and any row, is corrected on its own: the record
  keeps every other field and its CONFIRM. An item whose system reading was shown can no longer
  receive an "independent" reading. Empty means "not stated", a first-class answer for prices,
  stop and targets. LABEL_ONLY items ask only for the post label, flags, confidence and notes.
* **APPROVER** sees the reviewers' records (and the independent readings) and writes APPROVAL:
  ACCEPT, AMEND (only the amended fields) or RETURN. Refused when the approver's login wrote any
  reading of that item, and a REVIEWER cannot write APPROVAL.
* Navigation: sequences in `review_order` (each with its label-only items). Only the first
  `scope_sequences` (default 20) are offered, shown as "Scope: first 20 ideas"; the 40 sampled
  label-only posts come once the scope covers all sequences. Progress ("sequences completed",
  "items done") counts within the scope; "Next item not yet adjudicated". A record for an item
  outside the scope is refused.
* The sidebar also offers the vocabulary recommendations page ([VOCAB_REVIEWER.md](VOCAB_REVIEWER.md)),
  which keeps its own checks and sanitized export.
* **What a remote reviewer cannot do**, by construction (the app has no such code and imports
  none of those modules): apply vocabulary or rules to production; change risk, mapping or
  execution policy, or account permissions; reach IBKR, an order function or an execution
  endpoint; see credentials, the filesystem (no file browser, upload, download or path input) or
  sealed holdout material; and, while `remote_full_context_authorized` is false, see any
  publisher text or image. Every publisher or bundle string is shown as plain text, never Markdown or HTML.
  Images are shown only from `media/`, by a name the post lists, at most 5 MB, PNG/JPEG/GIF/WEBP.

## Identity, and its limit

From the proxy header (`TRADINGBOT_REVIEWER_LOGIN_HEADER`, default `Tailscale-User-Login`). With
no proxy header at all (no `Tailscale-*`, `X-Forwarded-*`, `Forwarded` or `Cf-*` header) and a
loopback `Host`, from `TRADINGBOT_REVIEW_LOCAL_IDENTITY` (the owner working locally), which must
also be listed in `reviewers.toml`.

**Limit:** anyone who can reach the local port (another account or process on this Mac) can send
any header, including a forged `Tailscale-User-Login`, or none and so act as the local identity.
The header is only as trustworthy as the rule that nothing but the proxy reaches the port. Run it
on a machine and account only the owner uses; never set `TRADINGBOT_REVIEW_LOCAL_IDENTITY` on an
instance that a proxy publishes; use Tailscale Serve, never Funnel. The reviewer/approver
separation is by login: a person holding two listed logins can play both roles.

## Commands (owner, locally)

```bash
export PYTHONPATH="$PWD/src"
# Try it with INVENTED data (into an empty directory outside the repository):
python scripts/adjudication_demo_bundle.py ~/adjudication-demo
export TRADINGBOT_ADJUDICATION_DIR=~/adjudication-demo   # the real one: ~/.local/share/tradingbot-private/adjudication

# Start (loopback only). The demo lists reviewer@, remote@ (REVIEWER) and owner@ (APPROVER)
# @example.invalid; restart with another TRADINGBOT_REVIEW_LOCAL_IDENTITY to change role.
TRADINGBOT_REVIEW_LOCAL_IDENTITY=reviewer@example.invalid \
streamlit run dashboard/reviewer/adjudication_app.py --server.address 127.0.0.1 \
  --server.port 8502 --browser.gatherUsageStats false --server.headless true
# Stop: Ctrl-C in that terminal.

python scripts/adjudication_freeze.py freeze   # next versions/ground_truth_v<N>.jsonl + manifest
python scripts/adjudication_freeze.py verify   # the log's chain and every version's sha256
```

**Freeze** refuses when the log fails verification and never overwrites. For each item it records
the approver's accepted or amended reading (status APPROVED / AMENDED), otherwise the latest
AFTER_REVEAL reading (PROVISIONAL, also after a RETURN). It keeps the INDEPENDENT reading beside
it, so anchoring can be measured. Items with only an independent reading are not included.
Scoring uses frozen versions only (data side).

**The log** never changes: a later record supersedes. Each line carries the sha256 of the
previous line, so an edited or removed line is reported (`CHAIN_BROKEN at line n`). A failing
log disables saving and freezing. Removing the last lines leaves a valid shorter chain; each
version's manifest records the log's line count and head hash, so compare against those.

## Behind a proxy

As for the vocabulary page ([VOCAB_REVIEWER.md](VOCAB_REVIEWER.md), "Behind a reverse proxy"):
keep CORS and XSRF protection on (the app refuses otherwise), set `server.corsAllowedOrigins` and
`server.allowedHosts` to the proxy's name, forward the WebSocket, and have the proxy set and strip
the identity headers. Not verified without a real proxy.

## Not done here, or open

* `bundle_id` is recorded, not recomputed: the specification does not say exactly how "sha256
  over the other files" is formed, and a RESTRICTED session cannot read `posts.jsonl` to check it.
  The demo uses sha256 over the sorted (name, file sha256) lines.
* `position_context` is read as `{state: OPEN|FLAT, side, last_stop}`; the specification names
  the content but not the keys.
* The per-session "system reading was shown" guard against a fake independent reading covers
  one browser session plus any AFTER_REVEAL already logged. A reviewer who looks with the switch
  off and then saves an independent reading in a NEW session is not caught.
