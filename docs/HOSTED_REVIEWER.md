# Hosted reviewer site

The remote reviewers' part of the adjudication workspace
([ADJUDICATION_WORKSPACE.md](ADJUDICATION_WORKSPACE.md)) as a hosted site: a web app
(`hosted/reviewer_web/`) on one Postgres database. The owner's own review, approval, freeze and
scoring stay local and unchanged. Every example below is invented.

## For reviewers

The site holds the same work as the adjudication workspace: you read a post with its earlier
posts and images, record your own reading, then see the system's reading and give your verdicts.
Nothing you save places, changes or approves a trade, a rule or a mapping. The reviewer guide and
the field reference are pages of the site ("Reviewer guide", "Field reference").

**Signing in.** Enter the address you were invited with. A message from "Trade review" brings a
six-digit code: it is valid for 10 minutes, works once and allows five tries; at most five codes
are sent to an address in an hour. The page answers the same for every address, so it never says
whether an address is invited. Signing in sets one cookie, which keeps you signed in on that
device for thirty days after your last visit; "Sign out" ends it. On another device you sign in
again. If your access is withdrawn, the site stops answering you at once.

**The workspace.** It shows how many ideas and forms of your scope are done, and every form in
review order with its status: open, independent reading saved (another form of the same post is
still open), reading after the reveal due, done, or "Returned to you". "Continue" opens the next
unfinished step (the rule is below). You may open any form of the list.

**A form** has three parts: 1. the source evidence (the post, its images, and the earlier posts
shown with it, oldest first), 2. your reading, 3. the system's reading.

1. *Independent reading.* Answer the fields. Which fields are asked depends on your answers: when
   you change one, the form is brought up to date by the server (without scripts in the browser,
   press "Refresh the form"). Above the Save button the page lists what Save still needs: the
   unanswered fields, each refusal with its code and field, and the warnings, which need the tick
   "I have checked these". Then "Save independent reading".
2. *The reveal.* The system's reading appears once EVERY form of the same post has your
   independent reading (a post may have two forms). From then on an independent reading of that
   post can no longer be saved.
3. *Reading after the reveal.* Your answers stand beside the system's. Under each compared field
   is the system's value and your verdict on it (CONFIRM, CORRECT or UNSURE), pre-filled from the
   comparison and editable. Change your own answer only for evidence you missed. Flags, confidence
   and notes are given anew at every save. Then "Save reading after reveal".

**The two questions after the reveal.** They stand in two blocks of their own, each showing what
the system proposes with your answer beside it (below it on a phone):

* "System's reference price series: ..." with "Is this the price series used for the numbers in
  the post?" The reference series is the price series the system reads the post's numbers
  against. Answer Yes when the post's numbers are quoted in that series, No when they are quoted
  in another one (a fund's price against a futures price, for example), Unsure when you cannot
  tell. It is a check of a fact of the post.
* "System's proposed execution instrument: ..." with "Would using this instrument faithfully
  follow the publisher's trade idea?" This is your opinion on a proposal. It approves nothing:
  approving a mapping is the owner's separate decision.

Neither has an answer until you choose one, and Save waits for both. When no answer is asked the
block says why: the system proposed nothing for this post, or your reading records no trading
action. An answer you saved is shown chosen again when you return.

**Saving.** A save is confirmed at the top of the page ("Saved ... stored as record n") with the
next step, and the form says above the confidence what is stored. Every save is a new record;
nothing is overwritten, and the newest counts. A double click stores one record. If the
connection drops while saving, reload the page or send it again: the same save is never stored
twice. Answers you have not saved are lost when you leave the page.

**Resuming.** Everything shown comes from what is stored, on any device. The next unfinished step
is:

1. a reading the approver returned to you, if there is one (the first in review order);
2. otherwise the first form in review order that is not done;
3. when that form's independent reading is saved but another form of the same post has none yet,
   that other form;
4. a form whose independent reading is saved and whose post is revealed opens in the state after
   the reveal, with your independent answers in it.

The rule is one pure function, `forms.next_step(bundle, session, records)`, and the state a
form's page shows is another, `forms.step_state(bundle, session, records, item_id)`
(`INDEPENDENT_DUE`, `REVEAL_PENDING`, `AFTER_REVEAL_DUE`, `RETURNED`, `DONE`): the operator tool
can check a reviewer's resume point with them, without signing in as that reviewer.

**A returned reading.** When the approver returns your reading of a form, the workspace marks it
"Returned to you" and the form opens again with your latest answers. The notice above the
confidence says "RETURNED by the approver" and gives the approver's note. Correct the answers,
give flags, confidence and notes, and save: the corrected reading replaces the returned one.

## Security model

**Who gets in.** Only a listed reviewer who is not revoked, with the role REVIEWER and FULL
access, can sign in or stay signed in; this is checked on every request, so a revoked reviewer's
sessions stop working at once. A one-time code is six digits, valid ten minutes, single use, five
tries, compared in constant time; a new code replaces the earlier ones. Limits per hour: five
codes per address, twenty code requests and thirty tries per network address. A session is a
256-bit random token in the cookie `__Host-session` (Secure, HttpOnly, SameSite=Lax, Path=/),
valid thirty days after the last use.

**What is stored for sign-in** (`login_code`, `web_session`, `rate_limit` in `schema.sql`): a
code only as HMAC-SHA256 under `SESSION_SECRET`, a session token only as SHA-256, a limit counter
under a keyed hash of the address or the network address. No address, code or token is stored in
these tables, written to a log, or put into a URL.

**Every request.** A POST must come from a page of the site: `Sec-Fetch-Site` must say
`same-origin`, and an `Origin` sent with it must be the site's own or `null` (under
`Referrer-Policy: no-referrer` a browser sends `Origin: null` with a form it submits to its own
site); a browser too old to send `Sec-Fetch-Site` is believed only on an `Origin` that is the
site's, so its forms are refused (Chrome 76, Firefox 90 and Safari 16.4 or newer are needed).
Once signed in, a POST must also carry the session's CSRF token. A route behind sign-in first establishes the reviewer, then that reviewer's own
bundle (`Store.bundle_for`), then the item: a form or an image is found only inside that
reviewer's scope of that bundle (`ordered(bundle, scope)`); every id from a URL is looked up,
never used. Anything else is a 404 that is the same page for every reason. The store has no call
that returns another reviewer's records, so no page can show them; beside a reviewer's own
records it hands over only the approver's RETURN of that reviewer's readings.

**A test identity beside the real reviewers.** A reviewer row flagged as a test identity reads a
bundle of invented forms in the same database, while the real reviewers read the real one. Every
route asks the store for the signed-in reviewer's bundle and never for "the" bundle, so a form,
an image or a record of one bundle is a 404 for a session of the other. Whoever reads a bundle
flagged as demonstration data sees the "Demonstration data" band on every page, in production
too.

**Blind-first.** Before the reveal a response holds nothing of the system: not its reading of any
field, not its price rows or levels, not the holds, not the mapping (reference series, proposed
instrument, basis, approval), in the page, its attributes, the form fragment or a header. The
reveal is the existing per-post gate: every form of the post within the scope has the reviewer's
independent reading (or the system's reading was already shown to them). The SYSTEM_SHOWN markers
of every form of the post are then stored and committed FIRST, and only a page that reads them
back shows the system's reading; if they cannot be stored, nothing is shown. A reading after the
reveal is refused without the markers, and the core refuses an independent reading once a marker
or a reading exists. The site has no switch to turn blind-first off. What a blind form still tells
about the system is what it tells today, by design: the number of forms of a post, whether a form
is an instruction or label-only, and the market names and idea identifiers offered as choices.

**Saving.** One connection and one transaction per request; the page is built after the commit.
The server validates every save with the rubric (`build_annotation` through `Store.append`,
under the store's lock, against the reviewer's current records); the browser decides nothing. A
save carries a key made for that rendering of the form: the same key again stores nothing and
shows the saved state.

**Images** are served only for the post of a form in the reviewer's scope, of the reviewer's
bundle, by a name that post lists, as PNG, JPEG, GIF or WEBP of at most 4 MB.

**Responses.** `Cache-Control: private, no-store` on everything but the stylesheet and the
script; a Content-Security-Policy of `default-src 'self'` (no inline script or style, no other
origin, no framing); `X-Content-Type-Options: nosniff`; `Referrer-Policy: no-referrer`;
`X-Frame-Options: DENY`; `X-Robots-Tag: noindex, nofollow`; `robots.txt` disallows everything.
The pages load one stylesheet and one script from the site itself (`hosted/public/static`, which
holds nothing else) and nothing from anywhere else. Publisher text is always rendered as text.
`/healthz` touches neither the database nor a session. A failure shows a constant sentence; a log
line carries a constant code, the route's name and an exception's class, never post text, a
reading, an address or a code.

**Mail.** `production` needs the five mail variables (`SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`,
`SMTP_PASSWORD`, `MAIL_FROM`) or does not start; port 465 is TLS from the first byte, any other
port STARTTLS. The message holds the site's name, the code and its validity, and no link. A
production page never carries a code, whatever the bundle and whoever signs in. When the mail
server cannot be reached, or does not let the site in, the page says "The code could not be
sent. Try again in a minute." for every address alike: an address that gets no code makes the
same connection and sends nothing. When the server refuses or loses the one message (which no
unlisted address could meet), the page stays the neutral one and `MAIL_FAILED` is logged:
saying so would tell that the address is listed. In both cases the undelivered code is ended.

**Environments** (`SITE_ENV`). `preview` and `test` serve only bundles flagged as demonstration
data: they do not start on any other, stop answering if one is published, and refuse a reviewer
whose bundle is not flagged. With none of the mail variables set they use synthetic mail: no
message leaves, and the code-entry page shows the code in the demonstration band. Synthetic mail
is therefore only for a preview that sits behind the platform's own login: whoever reaches it
can sign in as a listed address, and the page shows a code only for a listed one. Production
never uses it.

**Known limits.** The time a code request takes is padded to a floor, and an address that gets no
code makes the same connection to the mail server as one that does (so a mail failure reads the
same for both); a sending time far above the floor could still be told apart. The limits per
network address rely on the platform's forwarding headers. Nothing prevents a signed-in reviewer
from copying what they are shown: confidentiality rests on the reviewer's undertaking, as today.

**Looking at the site locally.** `PYTHONPATH=src python hosted/dev_server.py` (never deployed)
starts an embedded Postgres with the invented demo bundle and two invented reviewers on
`127.0.0.1:8600` and prints the one-time code in the terminal.

## Operations

The operator tool is `scripts/hosted_admin.py`. It runs on the owner's Mac, is never deployed, and
is the only thing that writes to the database besides the site itself.

```bash
export PYTHONPATH="$PWD/src"
export TRADINGBOT_ADJUDICATION_DIR=~/adjudication-demo
export HOSTED_DATABASE_URL='postgresql://...'
python scripts/hosted_admin.py status
```

* `HOSTED_DATABASE_URL`: the database (the pooled address is fine). `init-db` uses
  `HOSTED_DATABASE_URL_UNPOOLED` when it is set (schema changes go over the direct connection).
* `TRADINGBOT_ADJUDICATION_DIR` (or `--dir DIR`): the local adjudication folder.
* It needs `psycopg` (`uv pip install "psycopg[binary]"`); the tests also need `pgserver`.
* It prints counts, ids, aliases, times, sizes and fingerprints only: never a sign-in address, a
  name, or a word of a post or a reading. A refusal is one constant code (`refused: CODE`), a
  database error only its class (`refused: DATABASE_OperationalError`). Exit 0: all is well;
  1: a check failed (parity, verify); 2: refused, nothing changed.
* A reviewer is named by **alias**: an entry of `reviewers.toml` whose login contains `@` is
  `REMOTE-<n>`, n its place among those entries in file order (the alias the owner's other tools
  print). Only the REVIEWER entries get a row. The reviewers file is only ever read.

### Commands

| command | what it does |
| --- | --- |
| `init-db` | Applies `hosted/reviewer_web/schema.sql`. Every statement can be run again. |
| `sync-reviewers` | Writes the remote reviewers of `reviewers.toml` as rows, bound to the active REAL bundle: login, name, role, scope, and the access a remote session has by that file (FULL only while `remote_full_context_authorized = true`, else RESTRICTED, which the site does not serve). Never deletes: an entry that left the file keeps its row, so revoke it. Refused while no real bundle is published (`NO_REAL_BUNDLE_PUBLISHED`), when an alias would pass to another sign-in (`ALIAS_OR_LOGIN_HELD_BY_ANOTHER_ROW`) or the file's scope is larger than the published one (`SCOPE_NOT_PUBLISHED`: publish first). `--demo` binds them to the demonstration bundle instead (a preview database, tests). |
| `sync-reviewers --test-identity TEST-1 LOGIN` | Instead: writes (or updates) the test identity `TEST-<n>` for that sign-in, bound to the active DEMONSTRATION bundle with that bundle's scope (`NO_DEMO_BUNDLE_PUBLISHED` while there is none). The reviewers file is not read. |
| `revoke ALIAS` | Sets `revoked_at` and deletes that reviewer's web sessions and sign-in codes not yet used. From that moment sign-in, every page and every save are refused; the records stay. |
| `restore-access ALIAS` | Clears the revocation (the reviewer signs in again). |
| `publish-bundle` | Verifies the local bundle (`BUNDLE_ID_MISMATCH` otherwise), cuts the part the scope needs, checks parity, uploads and marks it THE active real bundle. Refused unless `remote_full_context_authorized = true` (`REMOTE_FULL_CONTEXT_NOT_AUTHORIZED`): the posts leave the Mac only by the owner's switch. `--check-only` cuts and checks without a database. `--demo DIR` publishes the invented demo folder `DIR` instead, as THE active demonstration bundle (and refuses any other bundle: `NOT_A_DEMO_BUNDLE`). The two kinds are active side by side: publishing one never deactivates the other. |
| `import-log` | Verifies the local log (chain and head), copies it and its head file into a snapshot folder, imports the remote reviewers' lines, records the anchor. Can be run again. |
| `parity` | Compares, per reviewer, the local log with the hosted store: the reviewer's records, the state of every form in scope, and the NEXT STEP as the web app's own functions compute it (`reviewer_web.forms.next_step` and `step_state`), once over the local log and once over what the site serves. PASS only when all agree. |
| `export --out FILE` | Every event as JSONL (mode 0600, never over an existing file, never inside the repository): a header with the anchor, then per event its alias, origin, `seq`, hashes and exact line. |
| `restore --in FILE` | Loads an export into an EMPTY database and verifies every hash and the hosted chain. |
| `pull-to-local` | Appends the hosted saves the local log does not hold yet to the local log. |
| `push-returns` | Stores the approver's standing local RETURN decisions for the reviewers whose readings they return. |
| `status` | The two bundles and the anchor; per reviewer: bundle, scope, invited, first sign-in, first hosted save, revoked, and the lines by origin and phase. Test identities are only counted. |
| `test-report ALIAS` | For a test identity only: its bundle, first sign-in, number of web sessions, each of its events (seq, time, phase, item id) and the state of every form that is not open. This is how a sign-in, a save and a resume on the production site are checked without touching a reviewer. |
| `verify` | The hosted chain (every line against its hash, 1000001, 1000002, ... from the anchor) and the imported lines against the newest snapshot, in both directions. |

### Real reviewers and test identities in one database

Every reviewer row is bound to ONE bundle (`reviewer.bundle_id`), and the site serves a reviewer
nothing else: forms, images, records and saves are of that bundle only. Two bundles are active
side by side, at most one of each kind:

* the **real** bundle (`publish-bundle`), on which the reviewers of `reviewers.toml` work
  (`sync-reviewers`);
* the **demonstration** bundle (`publish-bundle --demo DIR`, invented data), on which a test
  identity works (`sync-reviewers --test-identity TEST-1 LOGIN`).

A test identity is served a bundle only while that bundle is flagged as demonstration data: the
tool refuses to bind one to the real bundle (`TEST_IDENTITY_NEEDS_A_DEMO_BUNDLE`), the store
refuses to publish as real a bundle a test identity works on, and every query of the site checks
the flag again. `import-log`, `parity`, `pull-to-local`, `push-returns` and `status` act on the
reviewers only; a test identity's events stay in the store (`export` and `restore` keep them,
marked `is_test`) and never reach the local log.

```bash
python scripts/hosted_admin.py publish-bundle --demo ~/adjudication-demo
python scripts/hosted_admin.py sync-reviewers --test-identity TEST-1 LOGIN
# ... sign in as that identity on the site, save a form, sign in again ...
python scripts/hosted_admin.py test-report TEST-1
```

```text
TEST-1 (test identity): bundle 5bee31e4acb2 (demonstration data), scope 40, first sign-in 2026-03-09T07:15:00+00:00, web sessions 1, revoked -
events: 2
  1000004 2026-03-09T07:21:10+00:00 INDEPENDENT i07
  1000005 2026-03-09T07:21:40+00:00 SYSTEM_SHOWN i07
TEST-1: records 2; forms 45: INDEPENDENT_SAVED+SHOWN 1, OPEN 44
  i07 INDEPENDENT_SAVED+SHOWN
```

A new real bundle (another `bundle_id`) does not move anyone by itself: after `publish-bundle`,
`sync-reviewers` binds the reviewers to it, and they are then served the records of that bundle
only.

### What is published

`publish-bundle` uploads the part of the local bundle that the first `scope_sequences` sequences
(review order) need, under the ORIGINAL `bundle_id` (the records carry it):

* the items the scope offers; the items of no sequence only once the scope covers every
  sequence;
* the posts those items, their context and their reply and quote chains need, and only the
  images those posts list (at most 4 MB each: `MEDIA_TOO_LARGE`);
* every sequence id with its place in the review order, because the `idea` choices list them;
  a sequence outside the scope keeps no post;
* an instruction item outside the scope that shares its post with an item inside it, as a stub
  (its place among the post's forms and its market; no context, no system reading): "form k of
  m" of that post counts it;
* a system reading that refers to an item left out refers to that item's sequence instead, which
  reads as the same idea association.

Before anything is uploaded it compares the full bundle with the part for every item in scope,
through the functions the workspace itself uses: the review order, the item's system fields,
`system_reading`, `post_forms`, `post_siblings`, `table_row_details`, `system_rows`,
`form_context`, the ideas the `idea` field offers, and the evidence (the posts shown and the
sha256 of each image). One difference: `parity with the full bundle: FAIL`, the check and the item
id of each, exit 1, nothing uploaded.

```text
bundle 5bee31e4acb2: scope 2 of 33 sequence(s); items 8 shown, 1 kept for form numbering, 37 left out; posts 10 of 41; images 1, 73 bytes; files 9120 bytes
parity with the full bundle: PASS, 8 item(s) in scope, 0 difference(s)
published and active: bundle 5bee31e4acb2, demonstration data
```

To raise the scope: raise `scope_sequences` in `reviewers.toml`, then `publish-bundle`, then
`sync-reviewers` (in that order; the other order is refused). To lower it: `sync-reviewers`
first.

### The events

Every annotation line is one row of `event`. A trigger refuses every UPDATE, DELETE and TRUNCATE
of it: a later line supersedes, as in the local log.

* **LEGACY**: a remote reviewer's own line of the local log, byte for byte, under its local `seq`.
  The owner's lines never leave the Mac.
* **HOSTED**: written by the site. `seq` 1000001, 1000002, ...; each line carries the sha256 of
  the previous hosted line, and the first one the head of the local log at the cutover
  (`chain_anchor`). A line hashes the same as a local one (`vocab_recommend.encode_line`).
* **RETURN**: the approver's local decision to return a reading, stored by `push-returns` for
  the reviewer it concerns (below). Outside the hosted chain; the site never writes one.

### Cutover

Rehearse steps 1 to 3 while the present site still serves: every command can be run again.

1. `init-db`, `publish-bundle --check-only`, `publish-bundle`, `sync-reviewers`, `status`.
2. `import-log`, then `parity` and `verify`: both PASS.
3. `publish-bundle --demo DIR`, `sync-reviewers --test-identity TEST-1 LOGIN`: sign in as that
   test identity, work through an invented form, and read `test-report TEST-1`.
4. **Pause the writers**: stop the present published site, so that no remote reviewer can save
   into the local log. Do not save locally yourself during the next steps.
5. **Verify** the local log: `python scripts/adjudication_freeze.py verify`.
6. **Snapshot and import**: `import-log`. It verifies chain and head again, writes
   `<adjudication dir>/hosted_snapshots/log-<lines>-<sha256 prefix>/` (folder 0700, files 0600:
   the log, its head file, the sha256 of the log's bytes), imports only the lines that are new
   since the rehearsal, and moves the anchor to the log's present head.
7. **Parity**: `parity` PASS, `verify` PASS. Compare the per-alias lines and form states that
   `import-log` printed, and each reviewer's next step, with what you expect.
8. Open the hosted site to the reviewers.

```text
local log: 8 line(s), head ba9b895aae2b, 10174 bytes, sha256 dcb3...4622
snapshot: log-00000008-dcb320867b5390e1
anchor: 8 line(s), head ba9b895aae2b
imported 7 new line(s); 0 already imported
REMOTE-1: lines 6 (6 new); forms 45: DONE 1, INDEPENDENT_SAVED 1, INDEPENDENT_SAVED+SHOWN 1, OPEN 42
  i07 DONE
  i08 INDEPENDENT_SAVED
  i01 INDEPENDENT_SAVED+SHOWN
```

The states: OPEN; INDEPENDENT_SAVED; DONE (a reading after the reveal is saved); `+SHOWN` when
the system's reading was already shown and the reading after the reveal is still due.

`import-log` refuses, and changes nothing, when the lines it imported earlier are no longer the
local log's (`IMPORTED_PREFIX_DIFFERS`), when the log fails verification
(`ANNOTATION_LOG_FAILS_VERIFICATION`), and when a reviewer who has saved on the site has also
saved locally since (`LOCAL_SAVE_AFTER_HOSTED_SAVES`: two histories; see Rollback).

### Day to day: hosted saves in the local log

Approval, freeze and scoring read the local log. Before them:

```bash
python scripts/hosted_admin.py pull-to-local
python scripts/hosted_admin.py parity
python scripts/adjudication_freeze.py verify
```

`pull-to-local` appends each hosted save the local log does not hold yet, through the log's own
chained append: the same record with its own time, `refers_to` pointing at the local line of the
record it means, and `hosted = {"seq", "sha256"}` naming the hosted line. It can be run again
(nothing is appended twice), never touches a line already there, and skips test identities. It
refuses a folder whose log is not the one the site continues
(`LOCAL_LOG_IS_NOT_THE_ANCHORED_LOG`) and a store that fails verification. Between pulls `parity`
says FAIL with `hosted lines not pulled: n`; after a pull it must PASS.

```text
local bundle 5bee31e4acb2
REMOTE-1: bundle 5bee31e4acb2; records local 6, hosted 8: DIFFERENT; forms 45: 44 equal, 1 different; hosted lines not pulled: 2
  i08: local INDEPENDENT_SAVED, hosted DONE
  next step: local i08 AFTER_REVEAL_DUE, hosted i01 AFTER_REVEAL_DUE: DIFFERENT
parity of the local log and the hosted store: FAIL, 1 reviewer(s)
```

The next step is the form the site's "Continue" opens and where it stands (INDEPENDENT_DUE,
REVEAL_PENDING, AFTER_REVEAL_DUE, RETURNED). On the local side it is computed over the
reviewer's lines and the approver's decisions of the local log; so a RETURN that was decided
locally and not pushed yet also shows as a difference, until `push-returns`. Without the web
app's functions the command prints `NEXT_STEP_FUNCTION_MISSING` and fails.

### Returned readings

The approver decides locally. A RETURN reaches the reviewer through the operator:

```bash
python scripts/hosted_admin.py pull-to-local
# ... the approver returns readings in the local workspace ...
python scripts/hosted_admin.py push-returns
```

`push-returns` takes each RETURN that stands in the local log (the item's latest approval, still
referring to its reviewer's latest reading after the reveal: `effective_approval`), finds the
returned reading in the store (an imported line by its bytes, a pulled copy by its `hosted`
field) and stores one RETURN event under that reviewer:

* an APPROVAL record of the log's shape with `decision: "RETURN"`, the approver's notes and
  time, and `refers_to` the `seq` the returned reading has in the store; of the approver only
  the role (`annotator` is `approver` / `Approver`, never a sign-in or a name);
* `seq` 2000000 + the seq of the local approval line (so the same decision is stored once),
  `prev_sha256` the sha256 of the returned reading, `local = {"seq", "sha256"}` naming the local
  line.

The site serves it to that reviewer only, after the reviewer's own records, and computes
"returned" with the workspace's own functions (`effective_approval`, `_outcome`), so the form
reopens after the reveal with the reviewer's latest answers and the approver's notes. Nothing of
the reviewer's records changes. The reviewer's next reading after the reveal chains on as any
other save and supersedes the return, exactly as locally; the next `pull-to-local` brings that
reading home (a RETURN event is never written back into the local log) and the approver decides
again.

```text
returns that stand in the local log: 2; 0 of a reading that was not made by a hosted reviewer
pushed 1 new return(s): REMOTE-1 1; 1 already stored; 0 skipped (the returned reading is not in the hosted store)
  REMOTE-1 i03: return 2000012 of the reading 1000003
```

It can be run again (nothing is stored twice). A RETURN of a reading the store does not hold
(saved locally and not imported) is counted as skipped. ACCEPT and AMEND are never pushed.
`verify` checks that every RETURN names a reading after the reveal of the same reviewer by its
seq and sha256; `export` and `restore` carry RETURN events like the others; `parity` compares the
reviewers' own records only.

One limit: no event can be taken back. If the approver decides again on the same reading
(ACCEPT or AMEND after a RETURN) before the reviewer has answered, the site still shows the
return until that reviewer saves again; `push-returns` then prints
`NO_LONGER_STANDS_LOCALLY <alias> <item>`. The same holds for the return of one reviewer's
reading once the approver's latest decision on that item concerns another reviewer's reading.

### Backup: export and restore

```bash
python scripts/hosted_admin.py export --out ~/hosted-backups/events-1.jsonl
```

An export holds every reading and the sign-ins inside its lines: keep it like the adjudication
folder. It holds the events (each with its alias and whether that is a test identity) and the
anchor, and nothing else: sign-in codes, web sessions and rate limits are never exported or
restored, so after a restore everyone signs in again; bundles and reviewer rows are written
again by their own commands. To restore into a new database: `init-db`, `publish-bundle` (and
`publish-bundle --demo DIR`), `sync-reviewers` (the same reviewers file gives the same aliases;
the test identities again by `--test-identity`), `restore --in FILE`, `verify`. "Empty" means no
event and no anchor. Refused: `DATABASE_NOT_EMPTY`, `UNKNOWN_ALIAS` (sync first),
`ALIAS_IS_NOT_THE_SAME_KIND` (a test identity's lines under a reviewer's alias, or the reverse),
`EXPORT_UNREADABLE`, `EXPORT_FAILS_VERIFICATION` (a line that does not match its hash, a gap in
the hosted chain, an anchor the first hosted line does not name). The restored site continues
the same chain.

### Rollback to the present site

1. Stop the hosted site, or `revoke` each alias.
2. `pull-to-local`, then `parity` PASS and `adjudication_freeze.py verify` OK: the local log now
   holds every hosted save.
3. Start the present site again. The reviewers go on from the pulled state: a local reading
   after the reveal refers to a pulled independent reading like to any other line.

To go back to the hosted site after reviewers have saved locally again, start from an EMPTY
database (`init-db`, `publish-bundle`, `sync-reviewers`, `import-log`): the whole local log,
pulled copies included, is then imported as the reviewers' local lines.

### Revoking a reviewer

`revoke REMOTE-2` takes effect at once: the site checks the row on every request and every save,
and the reviewer's sessions and open sign-in codes are deleted. Nothing of the reviewer's work is
removed.
`restore-access REMOTE-2` lifts it. Removing the entry from `reviewers.toml` does not revoke.
