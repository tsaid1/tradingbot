# Shadow Run Health — read-only health and recovery visibility (OPS-02)

**Status:** read-only observation of the merged synthetic shadow runner's artifacts. **MOCK ONLY, NOT
PRODUCTION.** Nothing on this page starts, stops, resumes or recovers a run, submits or amends an order,
or changes a checkpoint, ledger or report, and no state it shows authorizes any trading, recovery or
account action. Issue: `tsaid1/tradingbot#18`. Depends on OPS-01 (`operations_view`, PR #14) and
SHADOW-01 (`shadow_runner`, PR #17), both reused unchanged.

## What it shows

`dashboard/pages/3_Shadow_Run_Health.py` renders one `ShadowRunHealth` built by
`src/tradingbot/operational_health.py` from the three artifacts a shadow run can leave behind:

| Artifact | Producer contract (reused) | Read how |
|---|---|---|
| `checkpoint.json` | `shadow_runner.ShadowCheckpoint` (strict, `extra="forbid"`) | bytes, parsed with the runner's own model |
| `shadow_ledger.sqlite3` | `event_replay.PaperEventReplay` tables | `operations_view.build_snapshot` through SQLite `mode=ro`, plus one `SELECT source_id, revision FROM replay_posts` through `operations_view.read_only_connection` |
| report (optional, `--json` output of `scripts/run_shadow_synthetic.py`) | `shadow_runner.ShadowRunReport` | bytes, parsed with the runner's own model |

| Section | Content |
|---|---|
| Observation | when the files were examined (real clock), the as-of clock they are judged against (real or fake), each file's presence, size and **filesystem write time**, progress freshness, and process liveness (**always UNKNOWN**) |
| Run state | `COMPLETE` / `PAUSED` / `INCOMPLETE` / `CONTRADICTORY` / `INVALID`, the checkpoint cursor, the resumability verdict, serious finding count and a one-line headline |
| Identity | schema version, mock-only flag, feed id and digest, policy digest, ledger file name, delivery count and report label: expected value (feed and mock policy in use) versus checkpoint versus report |
| Progress | cursor, remaining deliveries, next delivery key, **virtual** time of the last committed and next delivery, and the report's status, cursor and per-invocation counters |
| Resumability | the verdict and the seven read-only preconditions that mirror the runner's resume validation |
| Findings | every run-level observation with severity `SERIOUS` / `WARNING` / `INFO` and a code (table below) |
| Ledger reconciliation | the OPS-01 read-model of the shadow ledger **evaluated on the virtual clock**: ideas, mock orders, outcome counts, outcomes needing review and the existing discrepancy codes |
| Observation limits | the fixed list of what this view cannot know |

Two clocks are kept apart throughout. The checkpoint's `virtual_now` and every ledger timestamp
(`published_at`, `received_at`, `processed_at`) are **virtual event time**: the feed's synthetic clock,
which the runner passes to the coordinator as `now`. They are displayed as such and are never aged
against the wall clock. The only wall-clock evidence is the **filesystem modification time** of each
file, labelled "an observed file write; not a runner heartbeat". The ledger snapshot is evaluated at the
checkpoint's `virtual_now` (or the feed epoch when no checkpoint can be read), so its freshness columns
are virtual ages, internally consistent with the run.

## Vocabulary

**Run state** (an observation of the artifacts; this page never says "healthy" or "running"):

| State | Evidence |
|---|---|
| `COMPLETE` | a valid checkpoint whose cursor equals the feed's delivery count and no serious finding. A mock-ledger fact: **not account flatness, verified protection, broker state or paper acceptance**. |
| `PAUSED` | cursor short of the end **and** a current report (same cursor) with status `DELIVERY_CAP_REACHED` or `WALL_BUDGET_EXHAUSTED`. |
| `INCOMPLETE` | cursor short of the end with no current report. Interrupted, crashed or still in progress **cannot be distinguished**: the runner emits no heartbeat. |
| `CONTRADICTORY` | the artifacts parse but disagree with each other or with the feed and policy in use (identity, cursor, ledger prefix, report). |
| `INVALID` | the checkpoint is missing or malformed, or the ledger is missing or unreadable. |

**Process liveness** is `UNKNOWN` in every case. A fresh checkpoint write is reported as a recent write
(`RECENT_WRITE_OBSERVED`), never as a live process; an old one on an incomplete run is
`NO_RECENT_PROGRESS`, never "dead".

**Progress freshness** (`FRESH` / `STALE` / `FUTURE` / `UNKNOWN`): the checkpoint file's write time aged
at as-of against the display threshold `PROGRESS_STALE_AFTER_SECONDS` (300 s) with the gateway's 5 s
future tolerance (`operations_view.freshness`). A missing write time is `UNKNOWN`. Freshness never
changes the run state and is not a runner setting or trading policy.

**Resumability** (a prediction of the runner's own `resume=True` validation, from read-only checks):

| Verdict | Meaning |
|---|---|
| `RESUMABLE_BY_CONTRACT` | every precondition passes and deliveries remain. A prediction, not an action or an authorization. |
| `NOTHING_TO_RESUME` | every precondition passes and the cursor is at the end; a resume delivers nothing. |
| `NOT_RESUMABLE` | at least one precondition fails; the runner would refuse. |
| `UNKNOWN` | a precondition could not be evaluated (no valid checkpoint, unreadable ledger). |

Preconditions: `CHECKPOINT_VALID`, `LEDGER_PRESENT`, `FEED_IDENTITY`, `POLICY_IDENTITY`,
`LEDGER_FILE_NAME`, `CURSOR_WITHIN_FEED`, `LEDGER_NOT_BEHIND_CHECKPOINT`. The last mirrors the runner's
recorded-outcome check read-only, using the OPS-01 snapshot's validation rows (the latest-attempt
outcome records decoded with the engine's contract, exactly what `PaperEventReplay.outcomes` reads):
every unique post committed before the cursor must have a `replay_posts` row **and** decodable
latest-attempt outcome records. A missing row or an undecodable record fails the check; a post row with
no outcome record at all cannot be judged read-only (the runner accepts it only if the post produced no
actions, which would need the interpreter), so the check is `SKIPPED` and the verdict is `UNKNOWN`,
never a pass. `tests/test_operational_health.py` proves the prediction against the runner on copies:
refusal (`ShadowStateError`, or a pydantic `ValidationError` for an undecodable record) wherever the
view says `NOT_RESUMABLE` or `UNKNOWN` for the damaged copies, convergence to `COMPLETE` for the valid
positive controls.

**Findings** (`SERIOUS` rendered as errors, `WARNING` as warnings, `INFO` as info):

| Code | Severity | Meaning |
|---|---|---|
| `CHECKPOINT_MISSING`, `CHECKPOINT_MALFORMED`, `CHECKPOINT_UNREADABLE` | SERIOUS | no checkpoint; one that does not parse with the runner's contract (error types only; content never shown); or one that could not be examined or read (exception type only) |
| `LEDGER_MISSING`, `LEDGER_INACCESSIBLE` | SERIOUS | no ledger file, or a ledger file that could not be examined |
| `FEED_IDENTITY_MISMATCH`, `POLICY_IDENTITY_MISMATCH`, `LEDGER_FILE_NAME_UNEXPECTED` | SERIOUS | the checkpoint belongs to another feed, policy or ledger file; the cursor is not read against this feed |
| `CURSOR_BEYOND_FEED` | SERIOUS | cursor larger than the feed's delivery count |
| `CHECKPOINT_AHEAD_OF_LEDGER` | SERIOUS | a committed post has no ledger row (ledger replaced or lost); the runner refuses rather than skip events |
| `LEDGER_CONTAINS_UNEXPECTED_POSTS` | SERIOUS | ledger rows the committed prefix does not explain (another run's ledger) |
| `LEDGER_OUTCOMES_UNDECODABLE` | SERIOUS | a committed post's latest-attempt outcome record does not decode; the runner reads it on resume and fails |
| `LEDGER_OUTCOMES_NOT_RECORDED` | WARNING | a committed post has a ledger row but no outcome record; the runner refuses if the post produced actions, which cannot be established read-only, so resumability is `UNKNOWN` |
| `LEDGER_ONE_AHEAD_OF_CHECKPOINT` | WARNING | exactly the next delivery is in the ledger but not covered by the checkpoint: the post-commit, pre-checkpoint crash window. On resume the coordinator records a duplicate; nothing fills twice. |
| `REPORT_IDENTITY_MISMATCH`, `REPORT_CONTRADICTS_CHECKPOINT`, `REPORT_LEDGER_MISMATCH` | SERIOUS | the report describes another run, claims a cursor or completion the checkpoint does not support, or its ledger summary (orders, ideas, outcome counts) differs from the read-only ledger view |
| `REPORT_SUPERSEDED` | WARNING | the report ends before the checkpoint cursor: an earlier invocation's report; not compared with the ledger |
| `REPORT_MALFORMED`, `REPORT_UNREADABLE`, `REPORT_LABEL_UNEXPECTED` | WARNING | the optional report does not parse, could not be examined or read (exception type only), or lacks the runner's non-production label; the run state is then taken from the checkpoint alone |
| `CHECKPOINT_WRITE_TIME_UNKNOWN`, `CHECKPOINT_WRITE_IN_FUTURE` | WARNING | no write time available; or a write time beyond the as-of clock (clock skew or altered metadata) |
| `NO_RECENT_PROGRESS` | WARNING on `INCOMPLETE`, INFO on `PAUSED` | no checkpoint write within the display threshold |
| `RECENT_WRITE_OBSERVED` | INFO | a recent write on an incomplete run; liveness still UNKNOWN |
| `NO_REPORT` | INFO | no report supplied; pause and interruption cannot be told apart from the checkpoint alone |
| `COMPLETION_OBSERVED`, `PAUSE_OBSERVED` | INFO | the positive observations, each with its disclaimer |

**Attention required** is set unless the run is an observed completion or pause with only `INFO`
findings, no ledger discrepancy and complete reconciliation. Unknown or contradictory evidence can never
clear it. Ledger discrepancies keep their OPS-01 codes and meanings (see `docs/MOCK_OPERATIONS.md`);
the straight synthetic run itself carries `REPLAY_HISTORICAL_REPORT_UNRECONCILED` because post `e06`
reports a past exit that the ledger never executed.

## Read-only by construction

* Checkpoint and report are read as bytes. The ledger is read through `operations_view.build_snapshot`
  and `operations_view.read_only_connection` (SQLite `mode=ro`, one deferred read transaction). The
  module opens no connection of its own and its only SQL statement is a `SELECT`; the tests check this
  from the AST and verify that no function on the read path calls a write, unlink, rename or mkdir.
* Collecting evidence, assessing it under every clock mode, rendering the page, switching the clock,
  toggling UNTIMED and pressing **Refresh view (read-only)** leave every artifact byte-identical with an
  unchanged modification time (`file_state` in the tests hashes bytes and `st_mtime_ns`).
* The page constructs no `ShadowRunner`, `PaperEventReplay` or `Journal`, reads no environment variable
  and offers no path selector. Its only controls are the scenario radio, the fake-clock radio, the UNTIMED
  checkbox and the refresh button; the tests assert that no other widget exists and that no control label
  contains start, stop, resume, recover, order, submit, cancel or amend.
* A malformed checkpoint or report is reported by validation error type and count only; the tests plant
  markers in the malformed files and assert they reach neither the health model nor the page. Source text
  is never selected or rendered: the health model carries no `text`, `post_hash` or raw artifact field.
* The collector never raises on a file it cannot examine or read. A `PermissionError` or other `OSError`
  from `stat` or `read_bytes` is recorded on the file observation by exception type only (never the
  message, which may carry a path), and a file removed between observation and reading is reported as
  absent. An unreadable optional report leaves the checkpoint and ledger sections intact
  (`REPORT_UNREADABLE`); an unreadable checkpoint or ledger yields the truthful `INVALID` state
  (`CHECKPOINT_UNREADABLE`, `LEDGER_INACCESSIBLE`). The tests reproduce these with a monkeypatched
  `pathlib.Path` method for one file name, without changing any permission bits.

## Synthetic scenarios

Every scenario is a fresh run of the **existing** runner over `shadow_runner.synthetic_feed()` (tickers
ZQX/VBN/KPL and every price invented) into a task-local temporary directory
(`operational_health.new_scenario_directory`, `tempfile.mkdtemp`). Fault scenarios then damage only those
freshly written synthetic files. The page builds one scenario per selection and session and only reads it
afterwards; it never opens `data/`, an existing database, a private archive, a broker or the network.

| Scenario | Built how | Expected |
|---|---|---|
| completed | straight run, report written | `COMPLETE`, `NOTHING_TO_RESUME`, `COMPLETION_OBSERVED`; ledger warning `REPLAY_HISTORICAL_REPORT_UNRECONCILED` |
| paused by delivery cap | `max_deliveries=5`, report written | `PAUSED`, `RESUMABLE_BY_CONTRACT`, `PAUSE_OBSERVED` |
| interrupted after commit | `InterruptSpec(6, "after_commit")`, no report | `INCOMPLETE`, `LEDGER_ONE_AHEAD_OF_CHECKPOINT`, `NO_REPORT`, `RECENT_WRITE_OBSERVED` |
| interrupted before commit | `InterruptSpec(6, "before_commit")`, no report | `INCOMPLETE`, `NO_REPORT`, `RECENT_WRITE_OBSERVED` |
| stale report | paused at 5 with report, then resumed to completion | `COMPLETE`, `REPORT_SUPERSEDED` |
| malformed report | completed, report overwritten with non-JSON | `COMPLETE`, `REPORT_MALFORMED` |
| missing checkpoint | completed, `checkpoint.json` removed | `INVALID`, `NOT_RESUMABLE`, `CHECKPOINT_MISSING`; freshness `UNKNOWN` |
| malformed checkpoint | paused, `checkpoint.json` overwritten with non-JSON | `INVALID`, `CHECKPOINT_MALFORMED` |
| missing ledger | paused, ledger removed | `INVALID`, `LEDGER_MISSING`; no ledger section |
| foreign feed identity | paused, `feed_digest` rewritten | `CONTRADICTORY`, `FEED_IDENTITY_MISMATCH`; cursor UNKNOWN |
| cursor beyond feed | completed, `cursor` set to 18 | `CONTRADICTORY`, `CURSOR_BEYOND_FEED` |
| checkpoint ahead of ledger | paused, ledger replaced by an empty schema | `CONTRADICTORY`, `CHECKPOINT_AHEAD_OF_LEDGER`, `REPORT_LEDGER_MISMATCH` |
| contradictory report | paused, report rewritten to `COMPLETE` at 17 | `CONTRADICTORY` (checkpoint and ledger themselves still `RESUMABLE_BY_CONTRACT`), `REPORT_CONTRADICTS_CHECKPOINT` |

**Fake clock (display only).** The sidebar judges the same evidence as of the real wall clock, 30 s
after the last observed write (`FRESH`), 1 h 5 min after it (`STALE`), or 1 h before it (`FUTURE`), and
the UNTIMED checkbox drops every file write time (`UNKNOWN`). The clock moves only the as-of time:
virtual times, the run state and the files never change, and the tests assert exactly that.

Injected ledger drift (a `replay_ideas` quantity edited by the test) is not a sidebar scenario because the
read-model module contains no write statement; `tests/test_operational_health.py` performs it, in the
read-model and through the page, and checks that it surfaces as `SERIOUS` OPS-01 discrepancies plus
`REPORT_LEDGER_MISMATCH`: the cursor is still shown at the end of the feed, but the run state becomes
`CONTRADICTORY` because the report no longer matches the ledger, and attention is required.

## Running it

```bash
# From the repository root, with imports pinned to this checkout:
PYTHONPATH=src streamlit run dashboard/pages/3_Shadow_Run_Health.py --server.headless true
# or as a page of the multipage app:
PYTHONPATH=src streamlit run dashboard/app.py
PYTHONPATH=src python -m pytest tests/test_operational_health.py
```

The sidebar prints the resolved `tradingbot` import origin so you can confirm the page is running this
checkout's code and not another editable install. To observe a run you made with the documented command,
call the read-model directly (the page offers no path selector by design):

```python
from tradingbot.operational_health import ShadowRunArtifacts, observe_shadow_run
health = observe_shadow_run(ShadowRunArtifacts(label="demo", workdir="/tmp/shadow-demo",
                                               report_path="/tmp/shadow-demo.json"))
print(health.run_state, health.resumability, health.headline)
```

## Limitations

* **No heartbeat exists.** The runner records a cursor and virtual time, never a wall-clock liveness
  signal, and this issue does not add one. Liveness is therefore `UNKNOWN` in every state; adding a
  heartbeat would be a producer change requiring its own review.
* File write times are filesystem metadata. Copying, restoring or touching a file changes them; they are
  shown with their basis and never upgraded into a runner timestamp.
* The ledger-prefix check establishes decodable latest-attempt outcome records per committed post from
  the OPS-01 snapshot; it cannot tell a deleted outcome from a post that produced no actions without the
  interpreter, so that case is `UNKNOWN`. The runner's own validation remains authoritative on resume.
* The report-versus-ledger comparison uses the OPS-01 snapshot (orders, ideas, latest-attempt outcome
  counts); it does not recompute the runner's convergence digest.
* The view describes one run directory at a time against the shipped synthetic feed and mock policy;
  it does not discover runs, compare runs or watch a directory. The browser does not auto-refresh.
* Observed completion of the finite synthetic feed says nothing about account flatness, protection,
  broker acknowledgement, live sources or paper acceptance; the existing release and paper-trading gates
  are untouched.
