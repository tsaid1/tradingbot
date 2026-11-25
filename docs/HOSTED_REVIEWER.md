# Hosted reviewer site

The remote reviewers' part of the adjudication workspace
([ADJUDICATION_WORKSPACE.md](ADJUDICATION_WORKSPACE.md)) as a hosted site: a web app
(`hosted/reviewer_web/`) on one Postgres database. The owner's own review, approval, freeze and
scoring stay local and unchanged. Every example below is invented.

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
| `sync-reviewers` | Writes the remote reviewers of `reviewers.toml` as rows: login, name, role, scope, and the access a remote session has by that file (FULL only while `remote_full_context_authorized = true`, else RESTRICTED, which the site does not serve). Never deletes: an entry that left the file keeps its row, so revoke it. Refused when an alias would pass to another sign-in (`ALIAS_OR_LOGIN_HELD_BY_ANOTHER_ROW`) or the file's scope is larger than the published one (`SCOPE_NOT_PUBLISHED`: publish first). `--test-identity TEST-1 LOGIN` also writes a test row: it works like a reviewer, and its saves are never pulled into the local log. |
| `revoke ALIAS` | Sets `revoked_at` and deletes that reviewer's web sessions. From that moment sign-in, every page and every save are refused; the records stay. |
| `restore-access ALIAS` | Clears the revocation (the reviewer signs in again). |
| `publish-bundle` | Verifies the local bundle (`BUNDLE_ID_MISMATCH` otherwise), cuts the part the scope needs, checks parity, uploads and marks it active. Refused unless `remote_full_context_authorized = true` (`REMOTE_FULL_CONTEXT_NOT_AUTHORIZED`): the posts leave the Mac only by the owner's switch. `--check-only` cuts and checks without a database. `--demo DIR` publishes the invented demo folder `DIR` instead, flagged as demonstration data (and refuses any other bundle: `NOT_A_DEMO_BUNDLE`). |
| `import-log` | Verifies the local log (chain and head), copies it and its head file into a snapshot folder, imports the remote reviewers' lines, records the anchor. Can be run again. |
| `parity` | Compares, per alias, the local log with the hosted store: the reviewer's records and the state of every form in scope. PASS or FAIL, and where each reviewer resumes. |
| `export --out FILE` | Every event as JSONL (mode 0600, never over an existing file, never inside the repository): a header with the anchor, then per event its alias, origin, `seq`, hashes and exact line. |
| `restore --in FILE` | Loads an export into an EMPTY database and verifies every hash and the hosted chain. |
| `pull-to-local` | Appends the hosted saves the local log does not hold yet to the local log. |
| `push-returns` | Stores the approver's standing local RETURN decisions for the reviewers whose readings they return. |
| `status` | The published bundle and the anchor; per alias: scope, invited, first sign-in, first hosted save, revoked, and the lines by origin and phase. |
| `verify` | The hosted chain (every line against its hash, 1000001, 1000002, ... from the anchor) and the imported lines against the newest snapshot, in both directions. |

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
3. Sign in as a test identity (`sync-reviewers --test-identity TEST-1 LOGIN`) and work through a
   form.
4. **Pause the writers**: stop the present published site, so that no remote reviewer can save
   into the local log. Do not save locally yourself during the next steps.
5. **Verify** the local log: `python scripts/adjudication_freeze.py verify`.
6. **Snapshot and import**: `import-log`. It verifies chain and head again, writes
   `<adjudication dir>/hosted_snapshots/log-<lines>-<sha256 prefix>/` (folder 0700, files 0600:
   the log, its head file, the sha256 of the log's bytes), imports only the lines that are new
   since the rehearsal, and moves the anchor to the log's present head.
7. **Parity**: `parity` PASS, `verify` PASS. Compare the per-alias lines and form states that
   `import-log` printed with what you expect.
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
folder. To restore into a new, EMPTY database: `init-db`, `sync-reviewers` (the same reviewers
file gives the same aliases), `restore --in FILE`, `publish-bundle`, `verify`. Refused:
`DATABASE_NOT_EMPTY`, `UNKNOWN_ALIAS` (sync first), `EXPORT_UNREADABLE`,
`EXPORT_FAILS_VERIFICATION` (a line that does not match its hash, a gap in the hosted chain, an
anchor the first hosted line does not name). The restored site continues the same chain.

### Rollback to the present site

1. Stop the hosted site, or `revoke` each alias.
2. `pull-to-local`, then `parity` PASS and `adjudication_freeze.py verify` OK: the local log now
   holds every hosted save.
3. Start the present site again. The reviewers go on from the pulled state: a local reading
   after the reveal refers to a pulled independent reading like to any other line.

To go back to the hosted site after reviewers have saved locally again, start from an EMPTY
database (`init-db`, `sync-reviewers`, `publish-bundle`, `import-log`): the whole local log,
pulled copies included, is then imported as the reviewers' local lines.

### Revoking a reviewer

`revoke REMOTE-2` takes effect at once: the site checks the row on every request and every save,
and the reviewer's sessions are deleted. Nothing of the reviewer's work is removed.
`restore-access REMOTE-2` lifts it. Removing the entry from `reviewers.toml` does not revoke.
