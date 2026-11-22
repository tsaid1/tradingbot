# Trading signal adjudication workspace

A stand-alone, loopback-only Streamlit app in which reviewers record what each trading post
really instructs, and an approver settles it, to make a versioned ground truth for evaluating the
interpreter. It implements the adjudication workspace specification version 1.3 (app side) and
the adjudication rubric version 2.3: the reviewer's fields, their questions, tooltips, values,
which fields apply and what Save checks are defined ONCE, in
`src/tradingbot/runtime/adjudication_rubric.py`, and rendered to
[ADJUDICATION_RUBRIC.md](ADJUDICATION_RUBRIC.md) (the authoritative rubric) and
[REVIEWER_GUIDE.md](REVIEWER_GUIDE.md) (the one-page guide the app shows) by
`scripts/adjudication_rubric_docs.py`; a test fails when the files and the code drift apart. It is the single system for vocabulary, post-level and
trading-idea review: the remote reviewer interface is this app behind Tailscale Serve, once the
owner activates that separately.
**It is not an execution path.** It has no broker, order, mapping-approval, lexicon or risk-policy
write path and imports none of those modules (tested in a subprocess). Its only write is the
annotation log. Publishing it through any proxy is **the owner's separate decision**; nothing
here enables remote access.

Code: `src/tradingbot/runtime/adjudication.py` (pure: bundle, log, versions),
`src/tradingbot/runtime/adjudication_rubric.py` (pure: the rubric),
`src/tradingbot/runtime/adjudication_scenarios.py` (the invented scenarios),
`dashboard/reviewer/adjudication_app.py` (the app) with its two static pages
`adjudication_guide.py` and `adjudication_fields.py`, `scripts/adjudication_freeze.py`,
`scripts/adjudication_demo_bundle.py`, `scripts/adjudication_rubric_docs.py`. It reuses the
reviewed vocabulary-reviewer parts ([VOCAB_REVIEWER.md](VOCAB_REVIEWER.md)): proxy-header identity,
the chained append-only log, private-path guards and the same server refusals.

## Files (private; outside the repository and outside synced folders)

`TRADINGBOT_ADJUDICATION_DIR` (normally `~/.local/share/tradingbot-private/adjudication/`):

| path | written by | content |
| --- | --- | --- |
| `bundle/` | the private export (data side) | `manifest.json`, `posts.jsonl` (the only publisher text), `media/`, `sequences.jsonl`, `items.jsonl` |
| `reviewers.toml` | the owner, by hand | who may sign in, role and access, `remote_full_context_authorized` |
| `annotations.jsonl` + `annotations.jsonl.head` | the app (only write) | schema `tradingbot.adjudication.annotation/1`, chained; the head file holds the line count and the last line's sha256 |
| `versions/ground_truth_v<N>.jsonl` + `.manifest.json` | the freeze command | resolved rubric-2 readings and their sha256 |
| `versions/ground_truth_v<N>.rubric1.jsonl` | the freeze command, only when there are rubric-1 rows | the rubric-1 rows (schema `tradingbot.adjudication.ground_truth/1`), never in the file above |

Every file is read only through the private-path guards: refused inside this repository, never
through a link, link count 1, mode 0600. The folders (the base, `bundle/`, `bundle/media/`,
`versions/`) must be mode 0700, or the app and the freeze refuse (`PRIVATE_FOLDER_NOT_0700`). A
base inside a cloud-synced folder shows a constant warning in LOCAL mode (the owner's policy
allows it) and is refused in PUBLISHED mode. The manifest's `bundle_id` is recomputed (sha256 over
one line `<POSIX path>\0<sha256 hex>\n` per file other than `manifest.json`, sorted by path) by a
FULL session and by the freeze, and a mismatch is refused (`BUNDLE_ID_MISMATCH`). The freeze
recomputes it at every run, from the files. A FULL session recomputes it (and runs every other
check of the loader) when it starts and again whenever anything in the bundle folder changes, not
at every run: the page runs again at every choice, and hashing a real bundle takes seconds,
during which the page would still show the state before the choice. What "changes" means: at
every run the page looks at every folder, file and link of the bundle without opening one (path,
kind and mode, link count, size, modification time, change time, inode). The system moves the
change time at every write, chmod, link or rename and a program cannot set it back, so a mode
other than 0600, a second link, a dot entry and an edit (even one that keeps the size and puts
the modification time back) are all refused at the next run, as before. What is no longer caught
during a session is a change that the file system itself does not record (writing to the disk
underneath it, or a system clock set back); the freeze still catches it. A change that only moves
the change time (a tool writing an extended attribute) costs one more validation. The validated
bundle is kept in that session's own state in between, never shared with another session, and
dropped whenever the page refuses the session or the session becomes RESTRICTED; a click on Save
after a change that fails validation writes nothing. A file named
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
* **What a blind form still reveals about the system** (by design; no code hides it): the number
  of forms per post ("Form k of m": m is the number of instruction items the system produced for
  the post, so it tells how many actions the system found); whether an item is an instruction or
  label-only (the sidebar's item label and the header "Label-only form" tell whether the system
  read the post as an instruction at all); and, in RESTRICTED access, the system's levels as the
  table's rows. Two bundle strings appear as option texts: market names (the `family` choice) and
  idea identifiers (`SEQUENCE:<id>` in `idea`); both are strictly pattern-checked at load and
  option texts are not rendered as Markdown. The market names also appear in the meanings
  beside the `family` control and above the further actions, which are plain text (`st.text`). Revealing one form marks every form of its post
  within the scope; a form of the same post outside the scope (in another sequence) is marked
  too, so it can no longer be read blind once the scope is later raised.
* **REVIEWER** writes INDEPENDENT and AFTER_REVEAL records. With *blind-first* on (the default,
  a sidebar switch) area 3 stays hidden until the reviewer has saved an INDEPENDENT reading on
  **every form of the post** (blind-first holds per post; an AFTER_REVEAL with `blind: true`
  before that is refused, `BLIND_FIRST_PER_POST`). Before the system's reading of an item is
  served to a reviewer, a SYSTEM_SHOWN marker is appended to the log for that item AND every
  other item of the same post (once per login and item); from then on that login cannot write an
  INDEPENDENT reading of any of them, in this or any later session, and a second INDEPENDENT is
  refused too. If a marker cannot be written, the system's reading is not shown. It is then
  shown beside the reviewer's form with a verdict on the SYSTEM's value per compared field
  ("CONFIRM: the system's value is right" / "CORRECT: the system's value is wrong, mine is right"
  / UNSURE, with a tooltip, pre-filled from the comparison and editable; NOT_APPLICABLE,
  automatic, where the field does not apply to the reviewer's reading), saved as AFTER_REVEAL
  (`blind: true`, referring to the independent record). With the switch off, the form starts
  **unanswered** with the system's values beside it (nothing of the system's reading fills an
  answer, in any mode) and only AFTER_REVEAL is written (`blind: false`).
* **The form** (rubric 2.3): "Form k of m for this post" (m = the post's instruction items), a
  "How to review" expander, then field groups A to I, each field labelled with a plain-English
  question and its name in brackets, with a tooltip. A select control (a list or a multiple
  choice) lists the option CODES only, at a width of its own that fits its longest option, and
  the complete meaning of every option is plain text beside the control (under it in a narrow
  window): a select control draws an option on one line and cuts a long text off instead of
  wrapping it, so no meaning is ever inside a list. A radio keeps the meanings in its option
  texts, which wrap. The price table and the further actions give the meanings of their options
  once, above their rows (the table again below a table of four rows or more), and each price
  row shows the complete meaning of what it holds. Typing in a list narrows it by code. After the
  reveal the system's value and the verdict follow under each answer: the value on one line, the
  verdict under it at the form's width, its three choices on one line in a window of ordinary
  width. The page is read by three levels of heading: its title, the three steps of an item
  (source evidence, the reading, the system's reading) and the field groups A to I with the
  price table; their sizes are in `dashboard/reviewer/.streamlit/config.toml`, which Streamlit
  reads for every instance of the page and which may hold the heading sizes and nothing else.
  Every control of the form has the control width, whatever it holds: a list, a typed value, the
  count of further actions.
  A field that does not apply to the reviewer's own timing and action is greyed with the
  reason (and stored NOT_APPLICABLE); values the form derives (the label of the action from its
  timing, the announcement FIRST_REPORT for CURRENT and CANNOT_DETERMINE for UNCERTAIN,
  `follower_implication` (with "inferred from context: not counted as executable" when `explicit`
  is NO), the effect NONE for a REPEAT and for a MODIFY from FLAT (a change to an order that is
  not yet a position; the warning ACTS_ON_FLAT still shows) and AMENDS_PROTECTION for any other
  MODIFY, the stop of a stop
  MODIFY, `amendment_from` for STOP, the NOT_STATED entry role when the entry price is NOT_STATED)
  are shown read-only. Effect NONE is never offered: only the form sets it. Price and text fields carry an answer-state choice (value
  given | not stated | cannot determine). The **price table** (instruction items) has one row per
  number of the post's `numbers` (FULL; shown as the token, with "k of m" for a repeated number
  and a few words either side of that occurrence, as plain text) or per level the system typed
  (RESTRICTED: no text): role, exactness, contract month (only for a futures quoted series) and
  `month_named` (only with NAMED). A number the system typed no level for was **not read by the
  system**: shown as such, row verdict NOT_READ. The lines "Position after your reading of the
  previous action of this post in this market", "Position AFTER this action" and, once every form
  of the post is saved, "Label of the whole post" are shown read-only. The mapping questions
  appear only after the reveal. Above Save: the required fields still unanswered and every
  blocking code; warnings need the tick "I have checked these". The same checks run in
  `append_annotation` (and on an approver's merged amendment), so a record that the form would
  refuse is refused by the log too. LABEL_ONLY items ask for the label of the whole post,
  ambiguities, the further actions (required for ACTIONABLE, CONDITIONAL, RETROSPECTIVE; optional
  for UNRESOLVED), flags, confidence and notes.
* **Two static pages** in the navigation, in both access modes: "Reviewer guide" (the text of
  REVIEWER_GUIDE.md) and "Field reference" (rendered from the rubric's table). They read nothing
  but the rubric module.
* **APPROVER** sees the reviewers' records (and the independent readings) and writes APPROVAL
  (each option with its meaning and a tooltip): ACCEPT, AMEND (only the amended fields; what
  follows from them is derived again; every field not amended whose kind (asked, derived, not
  applicable) changes through the amendment, or that was derived and is now asked, is reset to
  unanswered and never preselected, so Save waits until the approver answers it; amending
  `action`, `timing` or `announcement` also resets `effect` when it is asked and not amended; a
  field the amendment reset (or the approver blanked) must be answered even when the reviewer's
  record carries FORM_CANNOT_EXPRESS, which lifts REQUIRED_UNANSWERED only for the other fields;
  a reset field answered again counts as amended even with its earlier value; Save's checks run
  on the merged reading, its warnings acknowledged in the approval) or RETURN. A
  rubric-1 record cannot be approved (`RUBRIC_1_RECORD_NOT_APPROVABLE`). Refused when the approver's login wrote any
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
python scripts/adjudication_freeze.py verify   # the log's chain, every version's sha256, counts
```

**Freeze** refuses when the log fails verification, a folder is not 0700 or the `bundle_id` does
not match, and never overwrites. For each item it records the approver's accepted or amended
reading (status APPROVED / AMENDED), but only while that approval refers to its reviewer's LATEST
AFTER_REVEAL of the item; otherwise (no approval, a RETURN, or a reviewer revision after the
approval) the latest AFTER_REVEAL reading (PROVISIONAL). SYSTEM_SHOWN markers are ignored. It keeps
the INDEPENDENT reading beside it, so anchoring can be measured. Items with only an independent
reading are not included. Rubric-2 records are resolved first; an item with only rubric-1 records
gets a row marked `rubric: 1`, written to the sibling file `ground_truth_v<N>.rubric1.jsonl`
(schema `tradingbot.adjudication.ground_truth/1`), never to the ground_truth/2 file. The freeze
recomputes every derived value of a rubric-2 reading from its own answers (a stored value that
contradicts them never reaches a version). Scoring uses frozen versions only (data side).

### Records (`annotations.jsonl`, envelope schema unchanged)

Every line keeps the envelope `tradingbot.adjudication.annotation/1` (so an existing log and its
head keep verifying) and the keys of version 1 (`seq`, `prev_sha256`, `at`, `bundle_id`,
`item_id`, `annotator`, `phase`, `blind`, `reading`, `field_verdicts`, `flags`, `confidence`,
`notes`, `refers_to`, `decision`), plus, on every line written since rubric 2 (markers too):
`rubric: 2` and `acknowledged_warnings` (the sorted warning codes the writer ticked; they must be
exactly the warnings of the reading). A line without `rubric` is rubric 1: never edited or
converted, still verified and listed, not counted as adjudicated under rubric 2 (progress shows
the item open), and it still counts as "seen" for blind-first. An INDEPENDENT or AFTER_REVEAL
`reading` is the COMPLETED rubric-2 reading (every derived value and NOT_APPLICABLE filled in);
an APPROVAL's `reading` holds the amended fields only.

### The frozen version (`versions/ground_truth_v<N>.jsonl`, schema `tradingbot.adjudication.ground_truth/2`)

One JSON object per line (keys sorted, UTF-8), one line per resolved item, in bundle order:

| key | value |
| --- | --- |
| `schema` | `"tradingbot.adjudication.ground_truth/2"` |
| `rubric` | `2` (rubric-1 rows are in the sibling file, with schema `.../ground_truth/1`: count them, do not score them) |
| `bundle_id`, `item_id`, `kind` (`INSTRUCTION` / `LABEL_ONLY`), `set` (`DESIGN` / `CHECK`), `sequence_id`, `post_ref` | from the bundle |
| `form_index`, `forms_in_post` | this item's form number k and m among the post's INSTRUCTION items (slot order); `1, 1` for a label-only item |
| `status` | `APPROVED`, `AMENDED` (approver's merged amendment) or `PROVISIONAL` |
| `decision` | the latest effective approval's `ACCEPT` / `AMEND` / `RETURN`, or null |
| `reading` | the resolved reading: every field of the rubric (below), `levels` included |
| `independent_reading` | the INDEPENDENT reading the source AFTER_REVEAL followed, or null (blind-first off) |
| `field_verdicts` | of the source AFTER_REVEAL: field -> `CONFIRM` / `CORRECT` / `UNSURE` / `NOT_APPLICABLE`; `levels` -> {row -> the same or `NOT_READ`} |
| `blind` | whether the source AFTER_REVEAL followed an independent reading |
| `position_after` | `{state, side, stop}` derived from `reading`, or null (no separate action, action NONE or CANNOT_DETERMINE, label-only); `state` `OPEN` / `FLAT` / `CANNOT_DETERMINE`; with FLAT `side` and `stop` are `NOT_APPLICABLE`; with OPEN `stop` is a number string, `NOT_STATED` or `CANNOT_DETERMINE`, never `NOT_APPLICABLE` |
| `additional_actions` | `reading.additional_actions`: a list of `{timing, action, family, side}` (timing `CURRENT` / `HISTORICAL` / `CONDITIONAL` / `UNCERTAIN`; action `OPEN` / `ADD` / `MODIFY` / `REDUCE` / `CLOSE` / `CANNOT_DETERMINE`; side `LONG` / `SHORT` / `CANNOT_DETERMINE`) |
| `announcement` | `reading.announcement`: `FIRST_REPORT` / `REPEAT` / `CANNOT_DETERMINE` / `NOT_APPLICABLE` (null for rubric 1 and label-only) |
| `follower_implication` | `reading.follower_implication` (recomputed by the freeze; `INSTRUCTION_CANDIDATE` only with timing CURRENT, `RECONCILE_EXIT` and `RECONCILE_POSITION` only with HISTORICAL and FIRST_REPORT): `INSTRUCTION_CANDIDATE` / `RECONCILE_EXIT` / `RECONCILE_POSITION` / `NO_NEW_INFORMATION` / `WAIT_FOR_CONDITION` / `NONE` / `CANNOT_DETERMINE` / `NOT_APPLICABLE` (null for rubric 1 and label-only). It describes the publisher's message; it is not an order and authorizes none |
| `post_label_overall` | the label of the WHOLE post: the strongest of the post's action labels and its additional actions' labels, in the order ACTIONABLE, CONDITIONAL, RETROSPECTIVE, UNRESOLVED, COMMENTARY; set only once EVERY form of the post has a rubric-2 row and none of them is escalated (else null); a label-only row: its own `post_label` (null when escalated) |
| `acknowledged_warnings` | the warning codes acknowledged for the resolved reading (the approval's for AMENDED, else the source's) |
| `escalated` | true when the source record carries the flag `FORM_CANNOT_EXPRESS` (count, do not score). An escalated row MAY contain unanswered `""` fields (the escalation lifts only `REQUIRED_UNANSWERED`). It may NOT contain a value in a field that waits on an unanswered answer (such a field is `""` or `NOT_APPLICABLE`), a derived value other than the form's (`post_label`, `announcement` for CURRENT or UNCERTAIN, `follower_implication`, `component` on a single-form post, the effect of a REPEAT or a MODIFY), or any combination Save refuses between answered fields. Every row of a post with an escalated form has `post_label_overall` null. The notes are not frozen: the escalation list for the owner is read from the log |
| `validation_codes` | (rubric 2.3) the blocking codes the rubric's validation finds on the resolved `reading` after the freeze has recomputed its derived values (with the source's flags, confidence and notes; for AMENDED, the fields the amendment reset must be answered), as a list in the order found; `[]` when the reading is clean. The freeze refuses nothing and repairs nothing beyond the derived values. A consumer counts a row with a non-empty list apart and does not score it |
| `all_readings` | one entry per reviewer login that saved an AFTER_REVEAL reading of the item (its latest), in log order, the resolved reading's reviewer included: `{reviewer, reading, independent_reading, blind, source_seqs: {independent, after_reveal}}`; `reading` is that reviewer's own (never the approver's amendment). It adds no rule for choosing the ground truth; it makes agreement between reviewers measurable |
| `flags`, `confidence` | of the source AFTER_REVEAL |
| `reviewer`, `approver` | logins (approver null without an effective approval) |
| `source_seqs` | `{independent, after_reveal, approval}` log sequence numbers |

The `reading` of an INSTRUCTION row has these keys (values are strings unless said otherwise):
`component` (`ACTION_RECORDED_HERE` / `NO_SEPARATE_ACTION`; on a NO_SEPARATE_ACTION form every
other field is `NOT_APPLICABLE` and `levels` rows too), `timing` (`CURRENT` / `HISTORICAL` /
`CONDITIONAL` / `COMMENTARY` / `UNCERTAIN`, answered), `post_label` (the label of THIS action,
derived from the timing), `announcement`, `action` (`OPEN` / `ADD` / `MODIFY` / `REDUCE` /
`CLOSE` / `NONE` / `CANNOT_DETERMINE`), `explicit` (`YES` / `NO`), `follower_implication`,
`family`, `instrument_as_read` (text), `quoted_series`, `traded_instrument`, `side` (`LONG` /
`SHORT` / `CANNOT_DETERMINE`), `position_state` (`OPEN` / `FLAT` / `CANNOT_DETERMINE`, BEFORE THIS
ACTION: earlier posts shown and earlier actions of the post), `position_side`,
`position_last_stop` (number), `effect`, `entry_price` (number), `entry_role`, `entry_exactness`,
`stop_price` (number), `targets` (numbers joined by `", "`), `amendment_field` (`STOP` / `TARGET`
/ `OTHER`), `amendment_to`, `amendment_from`, `idea` (`NEW_IDEA` / `SAME_AS_PREVIOUS` /
`SEQUENCE:<id>` / `UNKNOWN`), `reference_series_right`, `execution_instrument_right` (`YES` /
`NO` / `UNSURE`), `ambiguities` (a list of codes), `additional_actions` (a list, above), `levels`
(a list, one per row in table order: `{row, role, exactness, contract_month, month_named}`;
`row` is `n:<index into the post's numbers>` (FULL) or `level:<index into the system's levels>`
(RESTRICTED); `role` one of NOT_A_PRICE, OTHER_ACTION_OR_MARKET, HISTORICAL_FILL, STOP, TARGET,
INTENDED_ENTRY, CURRENT_MARKET, AVERAGE_OR_COST_BASIS, OTHER_CONTRACT_MONTH, CHART_LEVEL,
UNRESOLVED (the reviewer's order, "the first that applies"), or `NOT_APPLICABLE` when the table does not apply; `month_named` up to 12 upper-case
letters and digits, else `""`). A label-only row's `reading` has `post_label` (the whole post),
`ambiguities` and `additional_actions`.

Value conventions: numbers are plain decimal strings without thousands separators or trailing
zeros (`"2410"`, `"71.2"`); the four answer states are never interchangeable: `""` unanswered
(only in escalated rows), `NOT_STATED` (the publisher did not state it), `CANNOT_DETERMINE` (it
probably exists, the evidence does not settle it; always with an ambiguity or a flag),
`NOT_APPLICABLE` (does not apply; set by the form; lists use `[]`). `action = NONE`,
`effect = NONE`, family `UNKNOWN` / `OTHER`, price role `UNRESOLVED` keep their own meanings.
Executable ground truth (contract 13.4): timing `CURRENT`, a real action and `explicit = YES`,
nothing else. On a form whose post has an earlier form in the same market the position fields
carry no verdict (the system's position block is before the post).

The manifest (`ground_truth_v<N>.manifest.json`, schema `...ground_truth/2:manifest`) adds
`rubric: 2`, `by_rubric` (`{"1": n, "2": n}`), `escalated` (a count), `with_validation_codes`
(the number of /2 rows whose `validation_codes` is not empty) and `rubric1` (`{file, schema,
sha256, items}`; `file` and `sha256` null when there is no rubric-1 row) to the version-1 keys;
`items`, `by_status` and `escalated` count both files. `verify` checks the sibling file's sha256
too and, for a rubric-2 manifest, recomputes every count (`items`, `by_status`, `by_rubric`,
`escalated`, `with_validation_codes`, `rubric1.items`) from the two files: a version whose
manifest disagrees with its files fails. The manifest itself is not hashed. A rubric-1 row keeps the row keys above (with the /2-only values null or empty) and
has no `all_readings`.

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
  `annotations.jsonl` (fsynced) and replaces the head file, and the page says "Saved ... as line n"
  at its top. A reviewer's form is then built again (its key holds the saved line's number): the
  answers are those just saved, while flags, confidence and notes are EMPTY, because they belong
  to the next save and a confidence is never carried from one save to the next. Directly above
  the confidence the form therefore says what the log holds ("SAVED. Your reading ... is line n
  of the log, saved <date> at <time> UTC with confidence X"), and what comes next: the second step
  after an independent reading; nothing more after the reading after the reveal; or that the
  approver RETURNED that reading. Above Save, the two codes that only say the rebuilt form is not
  answered yet (CONFIDENCE, REQUIRED_UNANSWERED) are worded "The next save needs ...", not "Save
  refuses ...": nothing that was saved was refused. A contradiction between answers stays "Save
  refuses ...". What Save requires is the same in both wordings.
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
* The scorer for `ground_truth/2` is private tooling (data side); this page is its contract.
