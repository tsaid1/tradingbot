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
are sent to an address in an hour. If you ask twice, the two newest codes both work. After ten
wrong codes within 24 hours the sign-in of that address is locked for the rest of those 24
hours: tell the person who invited you, who can unlock it. The page answers the same for every
address, so it never says whether an address is invited. When one address opens two
workspaces (the owner's own review and the invented test forms), the page after the code asks
which to open: "Your review" or "Test forms (invented data)"; to open the other, sign out and
sign in again. Signing in sets one cookie, which keeps
you signed in on that device for thirty days after your last visit, and for at most ninety days
after signing in (then a new code is needed); "Sign out" ends it. On another device you sign in
again. If your access is withdrawn, the site stops answering you at once.

**Your sequences** (the dashboard). One row per sequence of your scope, in review order (the
posts of no sequence last): its idea, how many of its forms are complete out of how many, how
many remain, and one button: **Start** while nothing of it is saved, **Continue** after that
(it opens that sequence's next step by the rule below, a returned form first). A row opens to
its posts, in time order, and each post's forms ("Form 1 of 2" when a post has two), each with
its status: **Not started** (no reading saved; a kept draft is marked beside it), **In
progress** (your reading is saved and another form of the same post must be completed before
the system's answers are shown), **Awaiting your assessment**, **Complete**, or **Returned for
changes**. Everything is counted from your own saved readings only. The same grouping is the
sidebar of a form's page, with the sequence you are in open and the form you are on marked.
"Continue" at the top opens the next unfinished step of all your work.

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
twice. Every time the form is brought up to date, and when a save is refused, what you have
entered is kept as a DRAFT of that form ("Draft kept at HH:MM, not submitted"): it comes back
when you open the form again, on any device, and it ends when the form is saved. A draft is not
a submission: it changes no status, nobody else sees it, and it never reaches the approver.

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
If the approver takes a return back before you answer it, the mark and the notice go away and
nothing is asked of you.

## Security model

**Who gets in.** Only a listed reviewer who is not revoked, with the role REVIEWER and FULL
access, can sign in or stay signed in; this is checked on every request, so a revoked reviewer's
sessions stop working at once. A one-time code is six digits, valid ten minutes, single use, five
tries, compared in constant time. The newest two codes of a reviewer are valid at once, so a
request by somebody else does not end the code a reviewer was just sent; using one ends both.
Limits per hour: five codes per address, twenty code requests and thirty tries per network
address. Per day: ten wrong codes for an address lock its sign-in (no code is accepted or sent)
until 24 hours after the tenth-newest wrong code; the site logs
`SIGN_IN_TRIES_EXHAUSTED alias=<ALIAS>` once, `status` shows `sign-in: locked until <time>`, and
`hosted_admin.py unlock ALIAS` clears that counter and the hour's code requests of the address.
Every request is counted against its network address first; an address that is not shaped like
one (empty, too long, a control character) is never looked up and never makes the site contact
the mail server; entering a code takes the same least time and one database connection, whoever
asks. A session is a
256-bit random token in the cookie `__Host-session` (Secure, HttpOnly, SameSite=Lax, Path=/),
valid thirty days after the last use (the cookie is sent again whenever the stored expiry
moves, so the browser's copy slides with it) and at most ninety days after the sign-in.

**Pressing "Sign in" twice.** A browser drops the answer to a request when it sends the next
one, and the first answer carried the session. So the answer to a code request sets a cookie
`__Host-signin` (a random value, Secure, HttpOnly, SameSite=Lax, Path=/, eleven minutes; only
its SHA-256 is stored, beside the code it was answered with). A verify with the RIGHT code that
signed in at most 60 seconds ago, carrying the cookie of THAT code request, is answered like the
first: signed in again (for an address with two identities, the choice is offered again), and
the session or choice the first answer made, which never arrived, is ended, so one live session
results; the first sign-in's time stays and nothing is counted. The code stays single-use for
everyone else: without that cookie, with another request's, after 60 seconds, after a newer code
was asked for, or for a revoked, locked or not yet opened row, the answer is the same refusal as
before. A browser that already holds a session, or the choice of its identity, is sent on by a
verify (nothing counted). The cookie ends at sign-in and on "Use another address".

**What is stored for sign-in** (`login_code`, `web_session`, `rate_limit` in `schema.sql`): a
code only as HMAC-SHA256 under `SESSION_SECRET`, a session token only as SHA-256, a limit counter
under a keyed hash of the address or the network address. No address, code or token is stored in
these tables, written to a log, or put into a URL.

**Every request.** A request that changes something must come from a page of the site: an
`Origin` header, when sent, must be the site's own (`null` is refused); `Sec-Fetch-Site`, when
sent, must say `same-origin`; a request with neither is refused. The site's own origin is that
of the request itself (the scheme the platform's proxy reports in `X-Forwarded-Proto`, and the
`Host`), so one deployment answers under each of its host names, and no page or redirect holds
an absolute address. The site sends `Referrer-Policy: same-origin`: nothing goes to another
site, and a browser keeps the real `Origin` on a form it submits to the site. Once signed in, a
POST must also carry the session's CSRF token. A route behind sign-in first establishes the
reviewer, then that reviewer's own
bundle (`Store.bundle_for`), then the item: a form or an image is found only inside that
reviewer's scope of that bundle (`ordered(bundle, scope)`); every id from a URL is looked up,
never used. Anything else is a 404 that is the same page for every reason. The store has no call
that returns another reviewer's records, so no page can show them; beside a reviewer's own
records it hands over only the approver's RETURN of that reviewer's readings (of the approver
the role alone, no name and no address) and the line without content that takes one back. A
page shows a return only while it is the latest decision on the reviewer's latest reading.

**The address and the identity.** The address a code is mailed to (`reviewer.sign_in`, as
given in `reviewer.address`) is not the identity whose lines are written (`login_shown`, which
the records carry). For a remote reviewer the two are the same address. For the owner's own
reviewer identity, `OWNER-REVIEWER`, the identity is the local identity string exactly as the
owner's reviewers file and the local log spell it, and the address comes from a private file
(see "Operations"). One address may open at most one real identity and one test identity (a
unique index). After a right code such an address chooses which to open: the choice is stored
as `sign_in_choice` (only its token's SHA-256), made only when a right code was entered (it
holds the address's key row and its times, not the code), used once, valid
five minutes, held by the browser that entered the code in its own cookie `__Host-choice`; it
can name only an identity of that address that may sign in, and anything else uses it up and
opens nothing. A session belongs to one identity. The address's codes and counters are kept
under its key row (the lowest id of its rows): the ten-wrong-codes lock, the hourly quota and
`unlock ALIAS` (of either identity) act on the ADDRESS; `revoke ALIAS` ends that identity's
sessions and the address's open codes and choices (the other identity of the address simply
asks for a new code, and is then let in without a choice); the first sign-in is noted for the
identity opened. No hosted identity can approve, return or freeze: the store writes no phase
but a reviewer's own (`PHASE_NOT_WRITTEN_BY_THE_SITE`), and an APPROVER row cannot sign in.

**Before the import.** Until the local log is imported (no anchor), a reviewer's identity is
served one page ("Your workspace is not open yet ...") and the store refuses any of its lines
(`NO_ANCHOR`): such a line would continue no local log. A test identity works as before.

**A test identity beside the real reviewers.** A reviewer row flagged as a test identity reads a
bundle of invented forms in the same database, while the real reviewers read the real one. Every
route asks the store for the signed-in reviewer's bundle and never for "the" bundle, so a form,
an image or a record of one bundle is a 404 for a session of the other. Whoever reads a bundle
flagged as demonstration data sees the "Demonstration data" band on every page, in production
too. A test identity's saves are a stream of their own in the store (see "The events"): they go
through the same pages and the same checks, and never touch the reviewers' chain.

**Blind-first.** Before the reveal a response holds nothing of the system: not its reading of any
field, not its price rows or levels, not the holds, not the mapping (reference series, proposed
instrument, basis, approval), in the page, its attributes, the form fragment or a header. The
reveal is the existing per-post gate: every form of the post within the scope has the reviewer's
independent reading (or the system's reading was already shown to them). The SYSTEM_SHOWN markers
of every form of the post are then stored and committed FIRST, and only a page that reads them
back shows the system's reading; if they cannot be stored, nothing is shown. A reading after the
reveal is refused without the markers, and the core refuses an independent reading once a marker
or a reading exists. A form whose post was already revealed through another of its forms (a form
that enters the scope later, when the scope is raised) therefore has no independent step: its
page says so, and its reading is stored with `blind: false`. The site has no switch to turn blind-first off. What a blind form still tells
about the system is what it tells today, by design: the number of forms of a post, whether a form
is an instruction or label-only, and the market names and idea identifiers offered as choices.

**Saving.** One connection and one transaction per request; the page is built after the commit.
The server validates every save with the rubric (`build_annotation` through `Store.append`,
under the store's lock, against the reviewer's current records); the browser decides nothing. A
save carries a key made for that rendering of that form for that reviewer (a key sent to another
form, or made up, is refused). The store knows a save by a hash of that key and of what was
posted with it: the same POST again stores nothing and shows the saved state; the same page
posted with other answers is a new save and is checked as one.

**Images** are served only for the post of a form in the reviewer's scope, of the reviewer's
bundle, by a name that post lists, as PNG, JPEG, GIF or WEBP of at most 4 MB.

**Responses.** `Cache-Control: private, no-store` on everything but the stylesheet and the
script; a Content-Security-Policy of `default-src 'self'` (no inline script or style, no other
origin, no framing); `X-Content-Type-Options: nosniff`; `Referrer-Policy: same-origin`;
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

**The operator's sender.** `POST /internal/send-queued` is the one route without a session, and
so without the origin check and the CSRF token: it sends the invitations the operator queued
(see "Inviting a reviewer"), because the mail password exists only where the site runs. It reads
no parameter and no body. It answers only to `Authorization: Bearer <OPERATOR_TOKEN>`, compared
in constant time, where `OPERATOR_TOKEN` is an environment variable of at least 32 characters;
with the variable unset or too short, a wrong header or any other method (named or invented),
the answer is exactly that of a path that does not exist. So it is in `preview` and `test`, which never send mail, and
without `SITE_URL` (`SITE_URL_MISSING` is logged). `SITE_URL` (an `https://` address) is the
site's address as the invitations name it: the only absolute address the site ever writes; no
page and no redirect uses it, so the site works under each of its host names. The route's
answer is three counts (`sent`, `failed`, `left`); it never returns or logs an address. Each try
is counted, and its start recorded, in a committed transaction before the message is handed to
the mail server, and no transaction is open while the server is spoken to: a call that is
stopped between a send and its record can never send that message a second time.

**The database.** The site connects with `SITE_DATABASE_URL` when that variable is set (and
not empty), otherwise with `DATABASE_URL`; production does not start without
`SITE_DATABASE_URL` (`SITE_DATABASE_URL_MISSING`), and logs `SITE_ROLE_OWNS_EVENT` once at
start-up when the role it connects as owns the tables. The insert trigger refuses a LEGACY or
RETURN line from any role that does not own `event`. `SITE_DATABASE_URL` is for the connection of a
restricted role that may read and append but does not own the tables (so it cannot switch off
the rule that no event is changed or deleted), while the platform keeps managing `DATABASE_URL`
for the owning role. `hosted_admin.py make-site-role` creates that role and writes its address
to a file (see "The site's own database role"); a test runs every path of the site that writes
as that role.

**Environments** (`SITE_ENV`). `preview` and `test` serve only bundles flagged as demonstration
data: they do not start on any other, stop answering if one is published, and refuse a reviewer
whose bundle is not flagged. With none of the mail variables set they use synthetic mail: no
message leaves, and the code-entry page shows the code in the demonstration band. Synthetic mail
is therefore only for a preview that sits behind the platform's own login: whoever reaches it
can sign in as a listed address, and the page shows a code only for a listed one. Production
never uses it. A deployment the platform itself does not call production (`VERCEL_ENV` set and
not `production`) refuses to start as production (`PREVIEW_DEPLOYMENT_NEEDS_SITE_ENV`): a
preview that inherited the production variables must say `SITE_ENV=preview`, and then serves
invented data only.

**Known limits.** The time a code request takes is padded to a floor, and an address that gets no
code makes the same connection to the mail server as one that does (so a mail failure reads the
same for both); a sending time far above the floor could still be told apart. The limits per
network address rely on the platform's forwarding headers. Nothing prevents a signed-in reviewer
from copying what they are shown: confidentiality rests on the reviewer's undertaking, as today.

**Looking at the site locally.** `PYTHONPATH=src python scripts/hosted_dev_server.py` (outside
`hosted/`, the folder that is deployed: it is the only code with a way around the sign-in)
starts an embedded Postgres with the invented demo bundle and two invented reviewers on
`127.0.0.1:8600` and prints the one-time code in the terminal.

**The browser acceptance** (invented data only). One sequence (`scs01`) of four posts: s01 with
a chart, s05, s14 with two forms, s18; every form's system reading proposes a price series and
an instrument; the tester's reading of s01 has been returned by the approver.

```bash
# on this machine
PYTHONPATH=src .venv/bin/python scripts/hosted_dev_server.py --port 8612 --acceptance ACCEPT-1
# sign in as acceptance.tester@example.invalid (the code is printed in the terminal)

# on a database initialised as a preview, holding invented bundles only, no anchor of an
# import and no reviewer row but ACCEPT-n ones (refused otherwise: NOT_A_PREVIEW_DATABASE,
# NOT_A_DEMO_DATABASE, ANCHOR_EXISTS, OTHER_REVIEWER_ROWS); several testers may be seeded
export HOSTED_DATABASE_URL='postgresql://...'
PYTHONPATH=src python scripts/hosted_admin.py init-db --preview
PYTHONPATH=src python scripts/hosted_acceptance_seed.py seed --alias ACCEPT-1 \
    --login acceptance.tester@example.invalid
```

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
  1: a check failed (parity, verify); 2: refused. A refusal changes nothing, with one exception:
  a `pull-to-local` that fails midway has already appended the lines before the failure (run it
  again: it appends the rest).
* A reviewer is named by **alias**: an entry of `reviewers.toml` whose login contains `@` is
  `REMOTE-<n>`, n its place among those entries in file order (the alias the owner's other tools
  print). Only the REVIEWER entries get a row. The reviewers file is only ever read.
* The owner's own reviewer identity is `OWNER-REVIEWER`: the one REVIEWER entry of the file
  whose login has no `@` (or `--owner-entry N`, the N-th REVIEWER entry), with the sign-in
  address read from a private file: `--owner-address-file FILE` or the variable
  `HOSTED_OWNER_ADDRESS_FILE`. The file holds the address on one line, has mode 0600, and lies
  outside the repository and outside any synchronised folder (Dropbox, iCloud, OneDrive, Google
  Drive are refused). No command prints the address; it is stored in the database only.

### What the platform is told

`hosted/vercel.json` names the framework (`flask`) and bounds a request at 60 seconds. It is not
optional: a project that was not created from this folder has no framework setting, and without
one the platform builds no function, serves `public/` only and answers 404 for every page of the
app while reporting the deployment as ready. The entry is `hosted/wsgi.py` (`app`); the
dependencies are `hosted/requirements.txt`; the Python version is `hosted/.python-version`; the
static files are `hosted/public/static/`, served by the platform. A deployment is checked by
requesting `/healthz` (200, `ok`) and `/sign-in`, never by its "ready" state alone.

### Commands

| command | what it does |
| --- | --- |
| `init-db` | Applies `hosted/reviewer_web/schema.sql`. Every statement can be run again. |
| `sync-reviewers` | Writes the remote reviewers of `reviewers.toml` as rows, bound to the active REAL bundle: login, name, role, scope, and the access a remote session has by that file (FULL only while `remote_full_context_authorized = true`, else RESTRICTED, which the site does not serve). Never deletes: an entry that left the file keeps its row, so revoke it. Refused while no real bundle is published (`NO_REAL_BUNDLE_PUBLISHED`), when an alias would pass to another sign-in (`ALIAS_OR_LOGIN_HELD_BY_ANOTHER_ROW`), when the file spells differently (capitals included) a sign-in whose row already has lines (`LOGIN_SPELLING_HAS_RECORDS`, with the alias: put the earlier spelling back), or when the file's scope is larger than the published one (`SCOPE_NOT_PUBLISHED`: publish first). `--demo` binds them to the demonstration bundle instead (a preview database, tests); on a database that holds a real bundle that would move the reviewers from the real forms to the invented ones, so it is refused there (`REAL_BUNDLE_IS_PUBLISHED`) unless `--force` is given (run `sync-reviewers` without `--demo` to move them back). |
| `sync-reviewers --owner-address-file FILE` | Also writes `OWNER-REVIEWER` (above); without the file an existing row keeps its address, and a missing row is reported as not written. An APPROVER entry never gets a row. |
| `sync-reviewers --test-identity TEST-1 [LOGIN]` | Instead: writes (or updates) the test identity `TEST-<n>` for that sign-in (without LOGIN: the owner's address, from the address file), bound to the active DEMONSTRATION bundle with that bundle's scope (`NO_DEMO_BUNDLE_PUBLISHED` while there is none). The reviewers file is not read. |
| `revoke ALIAS` | Sets `revoked_at` and deletes that reviewer's web sessions and sign-in codes not yet used. From that moment sign-in, every page and every save are refused; the records stay. |
| `restore-access ALIAS` | Clears the revocation (the reviewer signs in again). |
| `unlock ALIAS` | Clears the sign-in counters of the reviewer's ADDRESS (of both identities when it opens two): the wrong codes of the last 24 hours (ten lock the sign-in; `status` then shows `sign-in: locked until <time>`) and the hour's code requests. |
| `invite ALIAS --kind continue\|start` | Queues one invitation for the SITE to send (the operator's machine holds no mail password): `continue` for a reviewer whose work moved here, `start` for a first access. Refused for a test identity (`TEST_IDENTITY_IS_NOT_INVITED`), a revoked reviewer (`REVOKED`), an unknown alias, and while a message of that kind already waits (`ALREADY_QUEUED`). |
| `publish-bundle` | Verifies the local bundle (`BUNDLE_ID_MISMATCH` otherwise), cuts the part the scope needs, checks parity, uploads and marks it THE active real bundle. Refused unless `remote_full_context_authorized = true` (`REMOTE_FULL_CONTEXT_NOT_AUTHORIZED`): the posts leave the Mac only by the owner's switch. `--check-only` cuts and checks without a database. `--demo DIR` publishes the invented demo folder `DIR` instead, as THE active demonstration bundle (and refuses any other bundle: `NOT_A_DEMO_BUNDLE`). The two kinds are active side by side: publishing one never deactivates the other. |
| `import-log [--accept-unlisted-lines N]` | Verifies the local log (chain and head), copies it and its head file into a snapshot folder, imports the lines of the reviewers who have a row (the remote reviewers and `OWNER-REVIEWER`; never the approver's), records the anchor, and prints how many local reviewer lines no row holds. Can be run again. |
| `parity [--accept-unlisted-lines N]` | Compares, per reviewer, the local log with the hosted store: the reviewer's records, the reveal history (the SYSTEM_SHOWN markers), the state of every form in scope, and the NEXT STEP as the web app's own functions compute it (`reviewer_web.forms.next_step` and `step_state`), once over the local log and once over what the site serves; then one line per identity for the owner: `<ALIAS>: completed n of m; next <form> <phase>`. PASS only when all agree. Local lines of a reviewer that no row holds fail it unless their count is named exactly with `--accept-unlisted-lines N`. |
| `export --out FILE` | Every event as JSONL (mode 0600, never over an existing file, never inside the repository): a header with the anchor, then per event its alias, origin, `seq`, hashes and exact line. |
| `restore --in FILE` | Loads an export into an EMPTY database and verifies every hash and the hosted chain. |
| `pull-to-local` | Appends the hosted saves the local log does not hold yet to the local log (`OWNER-REVIEWER`'s too; never a test identity's). Refused when an identity saved locally after the anchor while hosted saves of theirs are not home yet (`LOCAL_SAVE_AFTER_HOSTED_SAVES`). |
| `push-returns` | Stores the approver's standing local RETURN decisions for the reviewers whose readings they return. |
| `status` | The two bundles and the anchor; per reviewer: bundle, scope, invited, first sign-in, first hosted save, revoked, the lines by origin and phase, and each queued message (kind, queued, sent, result). Test identities are only counted. |
| `test-report ALIAS` | For a test identity only: its bundle, first sign-in, number of web sessions, each of its events (seq, time, phase, item id) and the state of every form that is not open. This is how a sign-in, a save and a resume on the production site are checked without touching a reviewer. |
| `make-site-role --out FILE` | Creates the database role the SITE connects as (default name `reviewer_site`), or gives it a new password, and writes its database address (the given address with user and password replaced) to FILE, mode 0600, never over an existing file. Nothing secret is printed. See "The site's own database role". |
| `make-site-role --grants-only` | Gives the existing role exactly the present rights again (a new table such as `draft`), its password unchanged. |
| `verify` | The hosted chain (every line against its hash, 1000001, 1000002, ... from the anchor), the test identities' stream as a chain of its own, every line under a row spelled as the line, the imported lines against the newest snapshot, in both directions, and the stored publication of this folder's bundle against a fresh cut of it (the four files and every image by sha256). |

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
  3000001 2026-03-09T07:21:10+00:00 INDEPENDENT i07
  3000002 2026-03-09T07:21:40+00:00 SYSTEM_SHOWN i07
TEST-1: records 2; forms 45: INDEPENDENT_SAVED+SHOWN 1, OPEN 44
  i07 INDEPENDENT_SAVED+SHOWN
```

A new real bundle (another `bundle_id`) does not move anyone by itself: after `publish-bundle`,
`sync-reviewers` binds the reviewers to it, and they are then served the records of that bundle
only.

### One spelling of a sign-in

The workspace finds a reviewer's history by the address in each record, compared character for
character with the address the reviewer signs in under. A line under any other spelling
(capitals included) is not found: the form looks open again, and a second "independent" reading
could be saved for a form whose system reading was already shown. So the spelling is kept in
step everywhere, and nothing changes it silently:

* `sync-reviewers` refuses to respell a row that has lines (`LOGIN_SPELLING_HAS_RECORDS`);
* `import-log` prints, per alias, the number of spellings of that sign-in among its local lines,
  and refuses unless there is one and it is the row's and the reviewers file's
  (`LOGIN_SPELLED_OTHERWISE`): such a log needs the owner's decision before any cutover;
* the store accepts no line of a reviewer under a row spelled otherwise
  (`LINE_NOT_SPELLED_AS_ITS_ROW`), and `restore` places every line by its own sign-in;
* `verify` fails on any line not spelled as its row, and `parity` fails when the local log holds
  lines of a remote reviewer that no row holds under that spelling (counts only).

### What is published

`publish-bundle` uploads the part of the local bundle that a page of a reviewer of the first
`scope_sequences` sequences (review order) shows, or a check of the site reads, and nothing
else, under the ORIGINAL `bundle_id` (the records carry it):

* the items the scope offers (those of no sequence only once the scope covers every sequence),
  each with the keys the loader reads, at every level of its line; not its `set`;
* the OWN post of each such item whole (text, numbers, links) and its images (at most 4 MB
  each: `MEDIA_TOO_LARGE`): the only images a page shows; a post shown as context with its time
  and text only; a post that is only named (the target of an own post's reply or quote) as a
  stub without text, time or images;
* every sequence id with its place in the review order, because the `idea` choices list them,
  and nothing else of a sequence outside the scope;
* an instruction item outside the scope that shares its post with an item inside it, as a stub:
  its place among the post's forms ("form k of m" counts it), and its market only when it
  comes before a form in scope (the comparison of the position fields reads the markets of the
  earlier forms of a post);
* a system reading that refers to an item left out refers to that item's sequence instead, which
  reads as the same idea association;
* of the manifest: its schema, the bundle id and the markets.

Before anything is uploaded it compares the full bundle with the part for every item in scope,
through the functions the workspace itself uses: the review order, the item's system fields,
`system_reading`, the forms of its post and the markets of the earlier ones, `post_siblings`,
`table_row_details`, `system_rows`, `form_context`, the ideas the `idea` field offers, the
evidence (the own post, and each context post's time and text), and the images: every image a
page shows must be in the publication with the sha256 of the local file, and no other image may
be. One difference: `parity with the full bundle: FAIL`, the check and the item id of each,
exit 1, nothing uploaded.

```text
bundle 5bee31e4acb2: scope 2 of 33 sequence(s); items 8 shown, 1 kept for form numbering, 37 left out; posts 8 with text and 1 named only, of 41; images 1, 73 bytes; files 8620 bytes
parity with the full bundle: PASS, 8 item(s) in scope, 0 difference(s)
images: all 1 that a page shows are in the publication with the sha256 of the local file; no other image is published
published and active: bundle 5bee31e4acb2, demonstration data (for test identities; the real bundle stays)
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
* **TEST**: a line of a test identity, written by the site: a stream of its own (`seq` 3000001,
  3000002, ..., chained among themselves from a fixed value). It never touches the reviewers'
  chain or the anchor, whenever it is written, and is never imported, pulled or pushed; the
  table refuses a TEST line of anyone else and any other line of a test identity.

The **anchor** is the local log's state that the reviewers' hosted chain continues. Every
`import-log` moves it to the log's present head, until the first HOSTED line of a reviewer
exists: from then on it is fixed, and `import-log` says so (`anchor: ...: FIXED ...`).

### The site's own database role

The role that owns the tables can disable or drop the trigger that makes `event` append-only.
The operator tool connects as that role; the SITE must not. `make-site-role` creates a second
login role that owns nothing and has exactly the rights the site's code uses:

* read `reviewer`, `bundle`, `media`, `event`, `chain_anchor`, `mail_queue`;
* add rows to `event` (never change or delete one; a LEGACY or RETURN line is refused to it);
* every row operation on the sign-in tables `login_code`, `web_session`, `rate_limit`,
  `sign_in_choice`, and on the drafts `draft`;
* set `reviewer.first_sign_in_at` and `reviewer.invited_at`, and `mail_queue.attempts`,
  `sending_at`, `result`, `sent_at`; no other column of either table.

```bash
python scripts/hosted_admin.py make-site-role --out ~/hosted-keys/site-address-1
```

Give the content of that file to the site as its `SITE_DATABASE_URL` (with the host of the
pooled address, if the site should use the pool), and keep `HOSTED_DATABASE_URL` (the owner's
role) on the Mac only; the platform may keep managing `DATABASE_URL`, which the site then does
not use. Run it again with another `--out` to change the password, and with `--grants-only`
after an `init-db` that added a table (the password stays). The statement carries the
password itself, over the direct TLS connection: the hosting provider takes a password in plain
text only (a statement carrying a SCRAM verifier is refused there when the transaction ends),
and stores its own verifier. The address file is written only after the database accepted the
change, so a refused run leaves no file. It works for an
operator that has CREATEROLE and is not a superuser (a second run alters only the password),
and after the schema was dropped and made again. It runs after `init-db` (the tables must
exist), over the direct connection when `HOSTED_DATABASE_URL_UNPOOLED` is set. It refuses a
role that is an administrator, may create roles or databases, inherits, cannot log in, owns
anything or is a member of another role (`NOT_A_SITE_ROLE`); `--grants-only` refuses a role
that does not exist (`NO_SITE_ROLE`).

### Rehearsal (on a preview database, invented data)

The whole cutover once, in its own order, on an invented folder shaped like the real one: two
remote reviewers (the first has finished three forms and is half-way through a fourth, the
second has nothing), the owner's local reviewer identity (two forms finished, form 1 of a
two-form post saved), the local approver (one reading accepted, one returned).

```bash
export PYTHONPATH="$PWD/src"
python scripts/hosted_rehearsal_folder.py ~/hosted-rehearsal      # prints the expected lines
export TRADINGBOT_ADJUDICATION_DIR=~/hosted-rehearsal
export HOSTED_OWNER_ADDRESS_FILE=~/hosted-keys/owner-address      # 0600, outside Dropbox
export HOSTED_DATABASE_URL='postgresql://...'                     # the PREVIEW database
python scripts/hosted_admin.py init-db --preview
python scripts/hosted_admin.py make-site-role --grants-only
python scripts/hosted_admin.py publish-bundle --demo ~/hosted-rehearsal
python scripts/hosted_admin.py sync-reviewers --demo
python scripts/hosted_admin.py sync-reviewers --test-identity TEST-1
python scripts/hosted_admin.py status
python scripts/hosted_admin.py import-log --cutover
python scripts/hosted_admin.py push-returns
python scripts/hosted_admin.py parity
python scripts/hosted_admin.py verify
```

`parity` must end with PASS (without `--accept-unlisted-lines`) and print, among its last
lines, exactly the three lines the folder script printed (`REMOTE-1: completed 3 of m; next
s02a RETURNED`, ...). A preview that held an earlier rehearsal is started again from an empty
schema: `import-log` refuses an anchor it did not write from this folder's log.

### Cutover

Steps 1 to 3 may run while the present site still serves, and be run again: no reviewer row
is open on the site before step 6 (each shows "Not open yet", and the store refuses its lines:
`NOT_IMPORTED`), so the owner may still review locally until step 4. **The site becomes usable
for the reviewers, the owner's "Your review" included, at step 6**, and is announced at step 8.

1. **The real folder and the production database, said explicitly** (a terminal that ran the
   rehearsal still holds its folder and the preview's address):

   ```bash
   export PYTHONPATH="$PWD/src"
   export TRADINGBOT_ADJUDICATION_DIR=<the REAL adjudication folder>
   export HOSTED_DATABASE_URL='<the PRODUCTION address>'
   export HOSTED_DATABASE_URL_UNPOOLED='<the PRODUCTION direct address>'
   export HOSTED_OWNER_ADDRESS_FILE=~/hosted-keys/owner-address
   ```

   Then `init-db` (never `--preview` on production), `make-site-role --grants-only` (when the
   role exists: the new tables), `publish-bundle --check-only`, `publish-bundle`,
   `sync-reviewers` (it fails without the owner's address file: `NO_OWNER_ADDRESS_FILE`), and
   `status`: compare its reviewer rows with the reviewers file (two `REMOTE-n` and
   `OWNER-REVIEWER`, each "closed until import-log --cutover runs"), `anchor: none`, and no
   other row but the test identity (otherwise start from an empty database).
2. `import-log`, then `parity` and `verify`: both PASS. This import snapshots the log and moves
   the anchor; it opens no row.
3. `publish-bundle --demo DIR`, `sync-reviewers --test-identity TEST-1` (the owner's address,
   from the same file). The owner signs in and, after the code, chooses **"Test forms
   (invented data)"** ("Your review" says "Not open yet" until step 6), works through an
   invented form; read `test-report TEST-1`. A test identity's lines are a stream of their own
   and leave the anchor alone.
4. **Pause the writers**: stop the present published site, so that no remote reviewer can save
   into the local log. The owner makes no reviewer save in the local workspace from here on.
5. **Verify** the local log: `python scripts/adjudication_freeze.py verify`.
6. **Snapshot, import and open**: `import-log --cutover`. It prints first which bundle and
   which database it writes (`cutover: real bundle into a database not marked preview`), and
   refuses a demonstration bundle into an unmarked database
   (`CUTOVER_DEMO_BUNDLE_ON_A_REAL_DATABASE`) and a real one into a preview
   (`CUTOVER_REAL_BUNDLE_ON_A_PREVIEW_DATABASE`). It verifies chain and head again, writes
   `<adjudication dir>/hosted_snapshots/log-<lines>-<sha256 prefix>/` (folder 0700, files 0600:
   the log, its head file, the sha256 of the log's bytes), imports only the lines that are new
   since step 2, moves the anchor to the log's present head (it must print
   `anchor: <lines> line(s), head <...>: moved to the log's present head`; anything else:
   stop), OPENS every reviewer row it considered, and marks them in the folder: from now on the
   local workspace refuses their reviewer saves (`REVIEWS_ON_THE_SITE`). A row written later
   stays closed until the next `import-log --cutover` has considered it.
7. **Returns and parity**: `push-returns` (a return the approver decided before the cutover
   and that still stands reaches its reviewer only by this command; until it is run, `parity`
   fails on that form), then `parity` PASS and `verify` PASS. Compare the per-alias lines and
   form states that `import-log` printed, and each reviewer's next step, with what you expect;
   the last lines of `parity` (`<ALIAS>: completed n of m; next <form> <phase>`, one per
   identity, `OWNER-REVIEWER` among them) are what to report to the owner.
8. Announce the hosted site to the reviewers (the invitations).

```text
local log: 8 line(s), head ba9b895aae2b, 10174 bytes, sha256 dcb3...4622
snapshot: log-00000008-dcb320867b5390e1
anchor: 8 line(s), head ba9b895aae2b: moved to the log's present head
imported 7 new line(s); 0 already imported
local lines of reviewers that no row holds: 0, of 0 sign-in spelling(s)
REMOTE-1: lines 6 (6 new); forms 45: DONE 1, INDEPENDENT_SAVED 1, INDEPENDENT_SAVED+SHOWN 1, OPEN 42
  i07 DONE
  i08 INDEPENDENT_SAVED
  i01 INDEPENDENT_SAVED+SHOWN
```

The states: OPEN; INDEPENDENT_SAVED; DONE (a reading after the reveal is saved); `+SHOWN` when
the system's reading was already shown and the reading after the reveal is still due.

Also refused, changing nothing: an empty local log (`EMPTY_LOCAL_LOG`; `--allow-empty-log`
on a preview only), an anchor that no import or restore of this folder's log wrote
(`ANCHOR_NOT_OF_THIS_LOG`), hosted lines of a row that is not an entry of the reviewers file
(`HOSTED_LINES_OF_ROWS_NOT_IN_THE_FILE`), a local line for a row that already holds hosted lines
(`LEGACY_AFTER_HOSTED_LINES`), and local reviewer lines no row holds unless
`--accept-unlisted-lines N` names exactly their count (`UNLISTED_LOCAL_LINES`). A reviewer row is
closed for saving (`NOT_IMPORTED`; the site says "Not open yet" and offers Sign out) until an
import has considered it, whenever it was written. `sync-reviewers` prints
`OWNER-REVIEWER: sign-in address CHANGED` (never the address) and, for a row with lines,
refuses without `--address-changed` (`ADDRESS_CHANGED_NEEDS_THE_FLAG`); `--without-owner` leaves
the owner's identity out on purpose; `--owner-entry N` refuses a remote reviewer's entry
(`OWNER_ENTRY_IS_A_REMOTE_REVIEWER_S`).

`import-log` refuses, and changes nothing, when the lines it imported earlier are no longer the
local log's (`IMPORTED_PREFIX_DIFFERS`), when the log fails verification
(`ANNOTATION_LOG_FAILS_VERIFICATION`), when a reviewer who has saved on the site has also
saved locally since (`LOCAL_SAVE_AFTER_HOSTED_SAVES`: two histories; see Rollback), and when a
reviewer saved on the site before any log was imported (`HOSTED_LINES_BEFORE_FIRST_IMPORT`: that
chain continues no log; start from an empty database).

### Day to day: hosted saves in the local log

Approval, freeze and scoring read the local log, and nothing in them knows of the site: a
freeze taken without a pull simply lacks the hosted saves. The owner now reviews on the site
(`OWNER-REVIEWER`) and approves locally: a reviewer save of the owner's in the local workspace
after the cutover makes two histories, and `pull-to-local` and `import-log` refuse until it is
resolved (`LOCAL_SAVE_AFTER_HOSTED_SAVES`). Before approval, freeze or scoring, always:

```bash
python scripts/hosted_admin.py pull-to-local
python scripts/hosted_admin.py parity
python scripts/adjudication_freeze.py verify
```

`pull-to-local` appends each hosted save the local log does not hold yet, through the log's own
chained append: the same record with its own time, `refers_to` pointing at the local line of the
record it means, and `hosted = {"seq", "sha256"}` naming the hosted line. It can be run again,
and two runs at once are safe: whether a line is already held is decided under the log's own
lock at each append, so nothing is appended twice. It never touches a line already there and
never pulls a test identity's line. It
refuses a folder whose log does not hold the anchored head at the anchored line count
(`LOCAL_LOG_IS_NOT_THE_ANCHORED_LOG`), any log but an empty one while nothing was imported
(`NO_ANCHOR_IMPORT_LOG_FIRST`), and a store that fails verification; `push-returns` refuses
the same. Between pulls `parity`
says FAIL with `hosted lines not pulled: n`; after a pull it must PASS.

```text
local bundle 5bee31e4acb2
REMOTE-1: bundle 5bee31e4acb2; records local 6, hosted 8: DIFFERENT; forms 45: 44 equal, 1 different; hosted lines not pulled: 2
  i08: local INDEPENDENT_SAVED, hosted DONE
  next step: local i08 AFTER_REVEAL_DUE, hosted i01 AFTER_REVEAL_DUE: DIFFERENT
  reveal history: local 2, hosted 3 marker(s): DIFFERENT
REMOTE-1: completed 2 of 45; next i01 AFTER_REVEAL_DUE (DOES NOT AGREE with the local log)
parity of the local log and the hosted store: FAIL, 1 reviewer(s)
```

`parity` also compares the site's state of EVERY form on both sides (a return that is missing
or stale on a form that is not the next step shows there), and fails when the local log holds
lines of a remote reviewer that no row holds; compare the number of reviewers it prints with
the reviewers file.

The next step is the form the site's "Continue" opens and where it stands (INDEPENDENT_DUE,
REVEAL_PENDING, AFTER_REVEAL_DUE, RETURNED). On the local side it is computed over the
reviewer's lines and the approver's decisions of the local log; so a RETURN that was decided
locally and not pushed yet also shows as a difference, until `push-returns`. Without the web
app's functions the command prints `NEXT_STEP_FUNCTION_MISSING` and fails.

### Two histories

A reviewer has two histories when they saved in the local workspace after the anchor while
hosted saves of theirs are not pulled yet. The final `import-log --cutover` prevents it at the
source: it writes `reviews_on_the_site.json` into the adjudication folder (mode 0600: the logins,
as the log spells them, of every imported reviewer, `OWNER-REVIEWER` among them; nothing else),
which `pull-to-local` keeps current (every row with hosted lines is added), and the local
workspace then refuses a REVIEWER save or marker of those logins (`Not saved
(REVIEWS_ON_THE_SITE).`). The approver's decisions are not touched, and `pull-to-local` appends
its copies as before. A folder without the file behaves as before. To let someone review locally
again (Rollback), delete the file after `revoke`.

A file that cannot be read as a list of logins (edited by hand, cut short) refuses EVERY
reviewer save in the local workspace (`REVIEWS_ON_THE_SITE_MARKER_UNREADABLE`; the approver's
decisions are not affected). Repair it with `hosted_admin.py pull-to-local`, which writes it
anew (counting the cutover as declared), or delete it to let the local workspace behave as
before the cutover.

If it happened anyway (a folder without the file), `pull-to-local` pulls everybody else, names
the identity (`<ALIAS>: NOT pulled: n hosted line(s) ... LOCAL_SAVE_AFTER_HOSTED_SAVES`) and
exits 1; `import-log` refuses. No command merges two histories, and no line can be taken back:
the identity's hosted lines stay in the store and on the site, its local lines are what
approval reads. Decide with the owner which history counts; the one way that keeps the hosted
saves is a new database (`init-db`, `publish-bundle`, `sync-reviewers`, `import-log`) after the
local lines of that identity were superseded by a rollback of that identity to the local site.

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
* `seq` 2000000 + twice the seq of the local approval line (so the same decision is stored
  once, in the local log's order), `prev_sha256` the sha256 of the returned reading,
  `local = {"seq", "sha256"}` naming the local line.

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
  REMOTE-1 i03: return 2000024 of the reading 1000003
cleared 0 pushed return(s) that no longer stand locally: -
```

It can be run again (nothing is stored twice). A RETURN of a reading the store does not hold
(saved locally and not imported) is counted as skipped. ACCEPT and AMEND are never pushed.
`verify` checks that every RETURN names a reading after the reveal of the same reviewer by its
seq and sha256; `export` and `restore` carry RETURN events like the others; `parity` compares the
reviewers' own records only.

No event can be taken back. When a pushed return no longer stands locally (the approver decided
again on that reading, or the item's latest decision now concerns another reviewer's reading)
and the site still shows it, `push-returns` stores ONE clearing line after it: the same shape
without content (`decision: "WITHDRAWN"`, no notes: nothing of what the approver decided), made
from the local decision that came after, at one seq above that decision's place. The site's
`effective_approval` then no longer gives the return, the form stands as done, and nothing asks
the reviewer for a save that would undo an acceptance. Stored once, like a return:

```text
cleared 1 pushed return(s) that no longer stand locally: REMOTE-1 1
  REMOTE-1 i07: line 2000021 clears the return 2000018
```

Only when there is no later local decision to make the line from (the reading was superseded
by a local save of the reviewer) it prints `NO_LONGER_STANDS_LOCALLY <alias> <item>` instead.

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
event and no anchor. A line is restored under the row whose sign-in is spelled exactly as the
line's annotator (a return: under the row of the reading it returns), never by alias. Refused:
`DATABASE_NOT_EMPTY`, `NO_ROW_FOR_A_LINE_S_SIGN_IN` (sync first, from the same file),
`ROW_IS_NOT_THE_SAME_KIND` (a test identity's lines under a reviewer's row, or the reverse),
`EXPORT_UNREADABLE`, `EXPORT_FAILS_VERIFICATION` (a line that does not match its hash, a gap in
the hosted chain, an anchor the first hosted line does not name). The restored site continues
the same chain.

### Rollback to the present site

1. Stop the hosted site, or `revoke` each alias (`OWNER-REVIEWER` too; a test identity may
   stay).
2. `pull-to-local`, then `parity` PASS and `adjudication_freeze.py verify` OK: the local log now
   holds every hosted save.
3. Start the present site again. The reviewers go on from the pulled state: a local reading
   after the reveal refers to a pulled independent reading like to any other line. The owner
   reviews locally again under the same local identity, whose pulled lines are its own.
   Drafts stay in the database and are never pulled: an answer kept only as a draft is not
   carried back.

To go back to the hosted site after reviewers have saved locally again, start from an EMPTY
database (`init-db`, `publish-bundle`, `sync-reviewers`, `import-log`): the whole local log,
pulled copies included, is then imported as the reviewers' local lines.

### Revoking a reviewer

`revoke REMOTE-2` takes effect at once: the site checks the row on every request and every save,
and the reviewer's sessions and open sign-in codes are deleted. Nothing of the reviewer's work is
removed.
`restore-access REMOTE-2` lifts it. Removing the entry from `reviewers.toml` does not revoke:
a reviewer who leaves is REVOKED and the entry stays. An entry removed while its row is not
revoked and holds hosted lines makes `import-log` and `pull-to-local` refuse
(`HOSTED_LINES_OF_ROWS_NOT_IN_THE_FILE`, after a line naming the alias and the remedy: put the
entry back); a revoked row's lines come home as anyone's.

### Inviting a reviewer

The site sends the invitation itself, through the mailer of the sign-in codes; the operator
only says who gets which one.

```bash
python scripts/hosted_admin.py invite REMOTE-2 --kind continue   # or: --kind start
curl -sS -X POST -H "Authorization: Bearer $OPERATOR_TOKEN" "$SITE_URL/internal/send-queued"
python scripts/hosted_admin.py status
```

1. `invite` writes one row of `mail_queue` (reviewer, kind, time). No address and no text is
   stored: the reviewer's row gives the address when the site sends, the kind gives the text
   (`mail.INVITATIONS`: the product owner's two messages, with the site's address filled in).
   Only a row that can sign in is invited: not revoked (`REVOKED`), not a test identity
   (`TEST_IDENTITY_IS_NOT_INVITED`), role REVIEWER with FULL access (`CANNOT_SIGN_IN`), a bundle
   assigned (`NO_BUNDLE_ASSIGNED`); and one message of a kind waits at a time
   (`ALREADY_QUEUED`).
2. The call to the site's sender sends every waiting message whose reviewer can still sign in,
   at most five a call, and answers `{"sent": n, "failed": m, "left": k}`. Call it again while
   `left` is not 0. A message that went out is never sent again; its time is kept, and the
   reviewer's `invited_at` the first time.
3. `status` shows per alias `mail INVITE_CONTINUE: queued ..., sent ..., result SENT`. A failure
   leaves `sent -` and a constant code (`MAIL_UNREACHABLE`: the mail server could not be reached
   or refused the site; `MAIL_FAILED`: it refused the message). A message is tried three times,
   once a call; after that it is given up, and `invite` may queue it again.
4. If the site is stopped between a send and its record (the platform's time limit), `status`
   says that sending began and its outcome was not recorded. Such a message may or may not have
   arrived, and the site never sends it again by itself: ask the reviewer, then `invite` again.

**When.** Queue and send the `continue` invitations only after step 7 of the cutover (the final
`import-log`, with `parity` and `verify` passing): the message tells reviewers to stop using the
old address, and a save on the hosted site before the final import would run ahead of it.

On the platform, production needs two more environment variables: `OPERATOR_TOKEN` (a random
secret of at least 32 characters; the same value on the operator's machine for the call above)
and `SITE_URL` (the site's `https://` address as reviewers should open it). The queue is no part
of `export` and `restore`. A preview never sends: there the sender does not exist.
