# Trading signal adjudication workspace

A stand-alone, loopback-only Streamlit app in which reviewers record what each trading post
really instructs, and an approver settles it, to make a versioned ground truth for evaluating the
interpreter. It implements the adjudication workspace specification version 1.3 (app side). It is the single system for vocabulary, post-level and
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
| `annotations.jsonl` + `annotations.jsonl.head` | the app (only write) | schema `tradingbot.adjudication.annotation/1`, chained; the head file holds the line count and the last line's sha256 |
| `versions/ground_truth_v<N>.jsonl` + `.manifest.json` | the freeze command | resolved readings and their sha256 |

Every file is read only through the private-path guards: refused inside this repository, never
through a link, link count 1, mode 0600. The folders (the base, `bundle/`, `bundle/media/`,
`versions/`) must be mode 0700, or the app and the freeze refuse (`PRIVATE_FOLDER_NOT_0700`). A
base inside a cloud-synced folder shows a constant warning in LOCAL mode (the owner's policy
allows it) and is refused in PUBLISHED mode. The manifest's `bundle_id` is recomputed (sha256 over
one line `<POSIX path>\0<sha256 hex>\n` per file other than `manifest.json`, sorted by path) by a
FULL session and by the freeze, and a mismatch is refused (`BUNDLE_ID_MISMATCH`). A file named
exactly `.DS_Store` (macOS Finder), in any folder of the bundle, is ignored by the loader, the
media listing and this recipe; any other dot-file or dot-folder is refused (`DOT_FILE_IN_BUNDLE`).
A RESTRICTED
session cannot check it, because it never reads `posts.jsonl` or `media/`. A bundle with an unknown schema, a
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
* **PUBLISHED mode** (through the proxy): every session is RESTRICTED, whatever its entry says,
  while `remote_full_context_authorized` is false. RESTRICTED shows the free-text system fields
  (`instrument_as_read`, the amendment's from and to, the two mapping strings) only when each is a
  number, or exactly one family name of `manifest.families`, or exactly one of the instrument
  words FUT, FUTURE, ETF, INDEX, SPOT, CFD, OPT, OPTION, STOCK, FRONT, NEXT, DIRECT; anything else,
  whatever its letter case, is shown as `(withheld: WITHHELD_NOT_A_SYMBOL)`. A withheld field's
  verdict is never pre-filled: it stays unanswered and need not be given.
  In RESTRICTED blind mode the price-table rows are labelled with the system's typed prices (there
  is no text to show), so that reading is partly anchored by design.
* **REVIEWER** writes INDEPENDENT and AFTER_REVEAL records. With *blind-first* on (the default,
  a sidebar switch) area 3 stays hidden until the reviewer saves an INDEPENDENT reading. Before the
  system's reading of an item is served to a reviewer, a SYSTEM_SHOWN marker is appended to the
  log (once per login and item); from then on that login cannot write an INDEPENDENT reading of
  the item, in this or any later session, and a second INDEPENDENT is refused too. If the marker
  cannot be written, the system's reading is not shown. It is
  then shown beside the reader's form with a per-field verdict (CONFIRM / CORRECT / UNSURE)
  pre-filled from the comparison and editable, saved as AFTER_REVEAL (`blind: true`, referring to
  the independent record). With the switch off, the form starts from the system's reading and
  only AFTER_REVEAL is written (`blind: false`). The **price table** (instruction items) has one
  row per number of the post's `numbers` (FULL; shown as the token) or per level the system
  typed (RESTRICTED: no text), each with a role, an exactness and a contract month (always
  asked). Every row starts unanswered in a blind reading, never filled from the system; after
  reveal the system's answer stands beside each row with its own CONFIRM / CORRECT pre-fill (an
  unanswered row: UNSURE). A number the system typed no level for was **not read by the system**:
  shown as such, with the row verdict NOT_READ (never a CORRECT against a claim it did not make);
  the reviewer's own answer is recorded as usual. Any field, and any row, is corrected on its own:
  the record keeps every other field and its CONFIRM. Empty means "not stated", a first-class answer for prices,
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

## Modes and identity

The mode is configured, never guessed from headers. Both apps (this one and the vocabulary page)
refuse to run unless bound to `127.0.0.1`, with usage statistics off, CORS and XSRF protection on,
the agent API and static serving off, `client.showErrorDetails = "none"`, and no `*` in
`server.allowedHosts` or `server.corsAllowedOrigins`.

* **LOCAL** (default): identity from `TRADINGBOT_REVIEW_LOCAL_IDENTITY`, which must be listed in
  `reviewers.toml`. A request carrying ANY forwarding, proxy or identity header (`X-Forwarded-*`,
  `Forwarded`, `Via`, `X-Real-IP`, `True-Client-IP`, `Cf-*`, `Tailscale-*`, the configured login
  and name headers) is refused outright. `Host` must be this server's `127.0.0.1:<port>` or
  `localhost:<port>`, and `Origin`, when sent, exactly `http://127.0.0.1:<port>` or
  `http://localhost:<port>` (no other localhost port). `allowedHosts` and `corsAllowedOrigins` may
  name only loopback.
* **PUBLISHED** (`TRADINGBOT_REVIEW_PUBLISHED_HOST=<host>`): refuses to start when
  `TRADINGBOT_REVIEW_LOCAL_IDENTITY` is set, or unless `--server.allowedHosts <host>` and
  `--server.corsAllowedOrigins https://<host>` are given. Identity comes only from the proxy
  header (default `Tailscale-User-Login`). Every session counts as proxied by configuration, so it
  is RESTRICTED while `remote_full_context_authorized` is false. `Host` must be exactly `<host>`
  and `Origin` exactly `https://<host>`.

**The remaining limit:** the proxy header is trusted because only the proxy reaches the port.
Another account or process on this Mac can still connect to `127.0.0.1:<port>` and forge
`Host`, `Origin` and the identity header. Run it on a machine and account only the owner uses,
and use Tailscale Serve, never Funnel. The reviewer/approver separation is by login: a person
holding two listed logins can play both roles.

## Commands (owner, locally)

```bash
export PYTHONPATH="$PWD/src"
# Try it with INVENTED data (into an empty directory outside the repository):
python scripts/adjudication_demo_bundle.py ~/adjudication-demo
export TRADINGBOT_ADJUDICATION_DIR=~/adjudication-demo   # the real one: ~/.local/share/tradingbot-private/adjudication

# Start (loopback only). The demo lists reviewer@, remote@ (REVIEWER) and owner@ (APPROVER)
# @example.invalid; restart with another TRADINGBOT_REVIEW_LOCAL_IDENTITY to change role.
# LOCAL mode:
TRADINGBOT_REVIEW_LOCAL_IDENTITY=reviewer@example.invalid \
streamlit run dashboard/reviewer/adjudication_app.py --server.address 127.0.0.1 \
  --server.port 8502 --browser.gatherUsageStats false --server.headless true \
  --client.showErrorDetails none
# PUBLISHED mode (behind Tailscale Serve; the owner's separate decision to activate):
TRADINGBOT_REVIEW_PUBLISHED_HOST=adjudication.<tailnet>.ts.net \
streamlit run dashboard/reviewer/adjudication_app.py --server.address 127.0.0.1 \
  --server.port 8502 --browser.gatherUsageStats false --server.headless true \
  --client.showErrorDetails none \
  --server.allowedHosts adjudication.<tailnet>.ts.net \
  --server.corsAllowedOrigins https://adjudication.<tailnet>.ts.net
# Stop: Ctrl-C in that terminal.

python scripts/adjudication_freeze.py freeze   # next versions/ground_truth_v<N>.jsonl + manifest
python scripts/adjudication_freeze.py verify   # the log's chain and every version's sha256
```

**Freeze** refuses when the log fails verification, a folder is not 0700 or the `bundle_id` does
not match, and never overwrites. For each item it records the approver's accepted or amended
reading (status APPROVED / AMENDED), but only while that approval refers to its reviewer's LATEST
AFTER_REVEAL of the item; otherwise (no approval, a RETURN, or a reviewer revision after the
approval) the latest AFTER_REVEAL reading (PROVISIONAL). SYSTEM_SHOWN markers are ignored. It keeps the INDEPENDENT reading beside
it, so anchoring can be measured. Items with only an independent reading are not included.
Scoring uses frozen versions only (data side).

**The log** never changes: a later record supersedes. Each line carries the sha256 of the
previous line (an edit of line n is reported at line n+1, `CHAIN_BROKEN`), and after every append
the head file is replaced atomically with the line count and the last line's sha256, so an edit
or removal of the LAST line is reported too (`HEAD_MISMATCH`). A failing log disables saving,
revealing and freezing. The chain has no key: whoever can write both files can rewrite them
consistently; each frozen version's manifest records the head, for comparison later.

**Repairing the head file.** A failed head write (a crash between the line and the head, a full
disk, a permission change) leaves `HEAD_MISMATCH`, and a log without a head file (written before
head files existed, or the head deleted) gives `HEAD_FILE_MISSING`; both lock saving, revealing
and freezing, and the page names this command:

```bash
python scripts/adjudication_freeze.py repair-head                  # annotation log: shows, writes nothing
python scripts/adjudication_freeze.py repair-head --yes            # writes the head
python scripts/adjudication_freeze.py repair-head --recommendations --yes   # the vocabulary log
```

It repairs only (i) a head that matches line n-1 with line n chaining onto it, and (ii) no head
file with the whole log a valid chain from line 1; it prints the last line's sequence, phase and
item id (never content). Anything else (a truncated log, an edited line, a head pointing past the
end, a broken chain) is refused with a code, and it never removes or rewrites a log line. Do not
edit the head file by hand. Notes are stored and shown as one plain line: control,
line-break and direction-override characters become spaces.

**One limit of the repair.** A head file protects the last line only while the head file exists. If the head file AND the last line(s) are both gone, what remains is a valid, shorter
chain, and situation (ii) accepts it. That is why the command prints the sequence number and item of the line it would treat as the last: compare it with the last save the page
confirmed ("Saved ... as line n") before you pass `--yes`.

## Saving and resuming

* A judgment is durable only when its **Save** button is clicked ("Save independent reading",
  "Save reading after reveal", "Save decision"): that click appends one line to
  `annotations.jsonl` (fsynced) and replaces the head file, and the page says "Saved ... as line n".
  Nothing is saved while you choose values.
* Revealing the system's reading also writes one line (the SYSTEM_SHOWN marker), before it is shown.
* If the browser, the tab or the app closes mid-item, every value chosen since the last Save is
  lost: the widgets live only in that browser session. Records already saved are kept, and a
  reveal stays recorded (that item can no longer get an independent reading by that login).
* On restart nothing else is needed: progress, "sequences completed", "items done" and each item's
  status come from the log alone, and **Next item not yet adjudicated** moves to the next item in
  review order whose status is still open (reviewer: OPEN or INDEPENDENT_SAVED; approver: AWAITING
  or RETURNED). Tested with a fresh session.

## Behind a proxy

PUBLISHED mode above. The proxy must forward the WebSocket (`/_stcore/stream`), send `Host` and
`Origin` as the published host, and set (and strip any client copy of) the identity headers on the
WebSocket request. Not verified without the real Tailscale Serve: that it does all this, which
`Host` it forwards, and what a tagged or shared device receives.

## Not done here, or open

* `position_context` is read as `{state: OPEN|FLAT, side, last_stop}`; the specification names
  the content but not the keys.
