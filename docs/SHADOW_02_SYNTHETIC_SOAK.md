# SHADOW-02 — Bounded synthetic soak and repeated-process-restart proof

**Status:** offline synthetic operational evidence only. **MOCK ONLY.** This is not broker, live-source,
real-clock, performance, uptime or production-readiness evidence, and not connected-paper acceptance.
Closes [issue #20](https://github.com/tsaid1/tradingbot/issues/20).

[SHADOW-01](SHADOW_01_SYNTHETIC_SHADOW_RUNNER.md) proves every single-run boundary of a 17-delivery
synthetic fixture and lists "small, hand-authored feed; not a soak at scale" as its limit. SHADOW-02
closes exactly that gap through the **unchanged** SHADOW-01 runner: a longer cumulative run and
repeated restarts across **fresh operating-system processes** on one persistent mock ledger.

## What it demonstrates

1. **Cumulative determinism at scale.** The public SHADOW-01 fixture is repeated for a bounded number of
   cycles with disjoint source identities on one coherent virtual timeline (representative profile:
   64 cycles = 704 synthetic post versions, 1088 deliveries, 32 virtual days). Every cycle's
   OPEN → REDUCE → CLOSE (long) and OPEN → CLOSE (short) fills land once; every duplicate, late,
   post-close, blocked and tampered re-delivery of every cycle adds **no** order and **no** row.
2. **Repeated-process-restart proof.** One uninterrupted *reference* process and a *chain* of fresh
   processes sharing one ledger (seeded pause/resume, before-commit and after-commit interruptions,
   each process under a different `PYTHONHASHSEED`) converge on the same ledger-derived orders,
   positions, outcomes, counts and digest. Per-process delivery statistics differ honestly
   (at-least-once processing, as SHADOW-01 documents); the ledger does not.
3. **Independent expectations.** Both final ledgers are measured through a **read-only SQLite
   connection**, not through the runner's report, and compared with totals derived from the
   SHADOW-01 specification (per cycle: 2 OPEN fills of 4, 1 REDUCE of 2, 2 CLOSE fills of 2 and 4,
   2 ideas CLOSED at quantity 0, 11 post rows, 11 action rows). Digest equality alone cannot carry
   a PASS; a test proves a tampered ledger fails even while the runner digests still agree.

## What is reused, and what is new

| Reused unchanged | Role |
|---|---|
| `shadow_runner.ShadowRunner`, `ShadowFeed`, checkpoint and report contracts | delivery loop, virtual clock, atomic checkpoint, interrupt injection, ledger-derived report |
| `shadow_runner.synthetic_feed()` / `shadow_policy()` | the public fixture and the one mock policy (`quantity=4`, allowlist ZQX/VBN) |
| `tkl_interpreter.TklInterpreter`, `validator.gate_source` / `gate_action` / `fill_quantity`, `event_replay.PaperEventReplay` | interpretation, authorization, sizing and the once-only mock ledger, all inside the runner |
| `event_models.ActionOutcome` | the contract used to read recorded outcomes back from SQLite |

| New (this issue) | Role |
|---|---|
| `src/tradingbot/shadow_soak.py` | multi-cycle feed builder, seeded restart plan, specification-derived expectations, read-only measurement, fresh-process launcher, versioned report and verdict |
| `scripts/run_shadow_soak.py` | documented command |
| `tests/test_shadow_soak.py` | regression suite (fast CI profile with real child processes; opt-in representative profile) |
| this document | run documentation and evidence |

No interpreter, validator, policy, journal, parsing rule or trading adapter was created. No shared
module, schema, dashboard, README, dependency or CI file was edited.

## The multi-cycle feed (`build_soak_feed`)

Cycle `c` copies every post and delivery of the SHADOW-01 fixture verbatim, prefixes every post key
and source id with `c{c:03d}-` (scheme `c{cycle:03d}-{base_key}`) and shifts every publication,
revision and delivery offset by `c × 43 200 s` (12 virtual hours; the fixture spans 42 000 s, and a
period that does not exceed the span is refused). Text, quotes, revisions, the late/post-close
duplicates and the tampered `text_override` are unchanged, so parser semantics, references,
duplicate/tampered identities and the existing policy are untouched. The coordinator's once-only
key `(source_id, revision, post_hash)` therefore sees a genuinely new post in every cycle, while
inside a cycle the five duplicates and the tampered re-delivery are recognised exactly as before.

| profile | cycles | post versions | deliveries | duplicate / tampered deliveries | virtual span | feed id |
|---|---|---|---|---|---|---|
| `ci` | 3 | 33 | 51 | 15 / 3 | 1 d 11:40:00 | `shadow-02-soak-003c-43200s-shadow-01-synthetic` |
| `representative` | 64 | 704 | 1088 | 320 / 64 | 31 d 23:40:00 | `shadow-02-soak-064c-43200s-shadow-01-synthetic` |

Feed digests on 2026-11-06: `ci` `288a6175bbfffa24207f430a719f99d7df73c22ec049d24054b7ef2e12942624`,
`representative` `eef42fa3e7dde57b1aabec5fd4ff28b615cc66a72d1fcb7ec1706005ed9af0b1` (they change if the
fixture or the feed contract changes; tests compare runs, not these values).

### Cumulative-context behaviour surfaced (existing policy, unchanged)

The only per-cycle difference the soak surfaces is the off-allowlist `$KPL` OPEN (fixture post e10):

- cycle 0: `BLOCK` `INSTRUMENT_NOT_ALLOWLISTED` (exactly as in SHADOW-01);
- every later cycle: `HOLD` `SOURCE_AMBIGUITIES` / `EXISTING_OPEN_IDEA_SAME_SIDE_IN_CONTEXT`.

The interpreter's point-in-time context is the *publisher's* book: the publisher announced the KPL
long and never exited it (the gateway blocked the fill, the publisher did not close), so the identical
entry sentence in the next cycle carries an "existing open idea" ambiguity, and the validator holds
on ambiguities before it reaches the allowlist. Both outcomes place no order; fills, positions and row
counts are unaffected. This is the pre-existing interpreter + validator behaviour on a cumulative
context, not a runner or soak decision, and it is pinned by a dedicated test rather than hidden
in the expectation table. SHADOW-01's single run could not expose it because its one KPL post is the
first.

## The seeded plan and the processes

`make_plan(profile, deliveries)` derives from `profile.seed` alone (deterministic under any
`PYTHONHASHSEED`): strictly increasing cut points in `[1, deliveries − 1]`, the three restart kinds
assigned round-robin and shuffled (every kind appears once there are three or more restarts), and a
distinct `PYTHONHASHSEED` for every process (reference, each chain segment, final segment).

| kind | the process ... | child exit | orchestrator verifies afterwards |
|---|---|---|---|
| `pause` | stops on `--max-deliveries` so the checkpoint cursor equals the cut | 4 | status `DELIVERY_CAP_REACHED`, cursor = cut, ledger post rows = unique posts in `deliveries[:cut]` |
| `interrupt_before_commit` | raises the injected crash before the ledger transaction at the cut | 3 | status `INTERRUPTED`, checkpoint cursor = cut, post rows = unique posts in `deliveries[:cut]` |
| `interrupt_after_commit` | raises after the transaction committed, before the checkpoint write | 3 | status `INTERRUPTED`, checkpoint cursor = cut, post rows = unique posts in `deliveries[:cut + 1]` (the window is real) |
| final / reference | runs to the end of the finite feed | 0 | status `COMPLETE`, cursor = deliveries, resumed from the last cut |

Each segment is a **fresh interpreter** (`python -m tradingbot.shadow_soak segment …`) started with
`PYTHONPATH` pinned to the orchestrator's own import origin and `PYTHONHASHSEED` from the plan. Its
wall budget (`--max-seconds`) is the per-process bound **capped by what remains of the total budget**
at launch time, and it is echoed back as `wall_budget_s`; once the total budget is exhausted nothing
further is launched. The
child refuses (exit 2, nothing touched) unless it imported `tradingbot` from exactly that origin and
rebuilt a feed with exactly the expected digest; it reports whether its hash seed matched and whether
the no-network guard was active. The guard (`no_network`) replaces the socket constructors with a
refusing stub for the whole segment and for the orchestrator; it is a defence-in-depth check that
no code path reaches for the network, not a sandbox.

## Verdict

- **PASS** only if: every process ran, verified its import origin, hash seed and guard; every planned
  restart was verified as in the table above; the reference and final chain segments completed the
  whole feed; the hash seeds were all distinct; the two convergence digests and the two ledger
  summaries are equal; both read-only measurements equal the specification-derived totals; every
  cycle holds exactly its two closed ideas and its five orders with idea references inside the same
  cycle; and no wall budget intervened.
- **INCOMPLETE** if a per-process or total wall budget stopped the soak, or if the total deadline had
  passed when the final segment and the measurement were re-checked (a bounded pause, or a late
  finish, is never a PASS); the preserved directories hold the paused state and the work done.
- **FAIL** for any divergence, refusal, error or mismatch.

## Report contract (`soak_report.json`, schema `shadow-soak-report-1`)

Strict JSON (`extra="forbid"`, `mock_only` must be `true`); no host paths. Main fields:

| field | content |
|---|---|
| `profile` | name, cycles, restarts, seed, cycle period, per-process and total wall budgets |
| `feed` | feed id and digest, base feed id and digest, policy digest, posts, deliveries, duplicate and tampered delivery counts, identity scheme, virtual epoch / end / span |
| `plan` | cut points, kinds and hash seed per restart, reference and final hash seeds |
| `segments` | one entry per process: role, planned kind, hash seed and whether it was confirmed, import origin and guard verification, exit code, status, resumed-from and end cursors, processed / fresh / duplicate / tampered counters, convergence digest, `wall_budget_s` (the bound handed to the process: per-process cap and remaining total), wall seconds, `window_verified` |
| `expected`, `actual_reference`, `actual_chain` | `LedgerTotals`: rows, ideas, closed ideas, open position quantity, orders by action with quantities, outcome and reason counts |
| `reference_digest`, `chain_digest`, `digests_converge`, `ledgers_equal`, `cycle_mismatches` | convergence evidence |
| `wall` | measured reference, chain and total wall seconds plus the budgets; **separate from virtual time** |
| `verdict`, `findings` | `PASS` / `FAIL` / `INCOMPLETE` with the reasons |

Per-process `ShadowRunReport`s are kept under `segments/`; the reference and chain working directories
contain only the standard SHADOW-01 `checkpoint.json` and `shadow_ledger.sqlite3`.

## Run it

```bash
python -m pip install -e '.[dev]'

python scripts/run_shadow_soak.py                                     # ci profile: 3 cycles, 51 deliveries, 5 restarts, 7 processes
python scripts/run_shadow_soak.py --profile representative --json /tmp/soak.json
                                                                      # 64 cycles, 1088 deliveries, 24 restarts, 26 processes
python scripts/run_shadow_soak.py --cycles 8 --restarts 6 --seed 7 --workdir /tmp/soak-demo

pytest tests/test_shadow_soak.py                                      # fast profile with real child processes
SHADOW_SOAK_PROFILE=representative pytest tests/test_shadow_soak.py -k representative   # opt-in, bounded local run
```

Exit codes: `0` PASS, `2` refused (occupied directory, unplannable bounds, usage), `5` FAIL,
`6` INCOMPLETE. Bounds: cycles ≤ 256, restarts ≤ 256 and ≤ deliveries − 1, per-process wall budget
≤ 600 s, total wall budget ≤ 1800 s. Without `--workdir` the command uses a private temporary root
that is removed only on PASS; a FAIL or INCOMPLETE soak preserves it and prints the path. An explicit
`--workdir` is never removed and must be absent or empty: the soak never reads `data/`, an existing
ledger or any other host path, and never scans for one. A soak ledger cannot be resumed by the
SHADOW-01 command or by a soak with a different cycle count: the checkpoint's feed id and digest do
not match and the runner refuses before anything is delivered.

## Representative evidence (2026-11-06, developer machine, this branch after the total-budget correction)

| run | processes | restarts verified | deliveries | convergence digest (reference = chain) | wall (measured) | virtual span | verdict |
|---|---|---|---|---|---|---|---|
| `representative`, seed 20261106 | 26 | 24 / 24 (8 pause, 8 before-commit, 8 after-commit) | 1088 | `3bc86358f124e77611eab184d4273832f4e78e8f1cafcaaec0bf72bad291bef0` | 6.04 s | 31 d 23:40:00 | PASS |
| `representative`, seed 7 | 26 | 24 / 24 (8 / 8 / 8, different cuts) | 1088 | `3bc86358f124e77611eab184d4273832f4e78e8f1cafcaaec0bf72bad291bef0` | 5.96 s | 31 d 23:40:00 | PASS |
| `ci`, seed 20261106 | 7 | 5 / 5 (2 / 2 / 1) | 51 | `1333a5edebee1d261dd7ae3c09f0dacf719f489f929e023924fe6030cfc42ad2` | 0.62 s | 1 d 11:40:00 | PASS |
| SHADOW-01 single run (before, runner unchanged) | 1 | — | 17 | `52ebd151ebe46a736aa4a6c7231840d7d2c02d2cd67106894fa88ab4002d1833` | 0.01 s | 11:40:00 | COMPLETE |

Measured totals of both 64-cycle ledgers, equal to the specification-derived expectation: 320 orders
(128 OPEN fills, quantity 512; 64 REDUCE, quantity 128; 128 CLOSE, quantity 384; 0 amendments),
128 ideas all CLOSED at quantity 0, 704 post rows and 704 action rows for 1088 deliveries, outcome
counts `SIMULATED 320, HOLD 191, BLOCK 65, NO_TRADE 64, RECONCILE 64` (BLOCK 65 = 64 stale MODIFY
deliveries + the cycle-0 KPL block; HOLD 191 = 128 conditional-add and edited-revision holds + 63 KPL
holds from cycles 1–63, the behaviour above). Wall time includes 26 interpreter start-ups and is reported
only to show the run is bounded; it is **not** a throughput, latency or uptime figure. Every process in
these runs received a budget of 120 s (the per-process bound; the 900 s total was never close to
exhaustion), recorded per segment as `wall_budget_s` in the report.

## Tests (`tests/test_shadow_soak.py`)

- import origin: the test process itself runs this worktree's `src`, not the editable install;
- feed: verbatim repetition with disjoint identities and shifted virtual times, deterministic digests
  that distinguish cycle counts and periods, refusal of overlapping periods;
- expectations: the one-cycle totals restate the SHADOW-01 numbers from a second source; scaling
  with the documented KPL exception;
- cumulative semantics in-process (3 cycles): per-cycle statuses, duplicate flags, tampered blocks and
  virtual times; orders and positions per cycle with coherent references; read-only measurement
  equals the expectation; the KPL BLOCK-then-HOLD behaviour pinned; a tampered ledger copy is detected
  and the measurement connection cannot write;
- plan: seed-determinism, strictly increasing cuts, all three kinds, distinct hash seeds, bounds;
- the soak with real child processes (fast profile): PASS, exit codes per kind, chained cursors on one
  ledger, verified crash windows, distinct confirmed hash seeds, at-least-once accounting, versioned
  sanitized report round trip, preserved chain state, refusal of mismatched resumes (different cycle
  count, and the SHADOW-01 command), occupied root and unplannable profile refused without touching
  anything, per-process and total wall budgets → INCOMPLETE, total budget controls with an injected
  clock (every launch capped by the remaining total and visible in the report; a final segment that
  finishes past the deadline is INCOMPLETE with ledgers, checkpoints and reports preserved; exhaustion
  refuses the next launch; a tiny total budget pauses the reference itself), tampered chain ledger →
  FAIL while the runner digests still agree, child refusals for a wrong import origin or feed digest,
  no-network guard;
- the documented command: PASS with temporary root removed, explicit root preserved and refused when
  occupied, INCOMPLETE exit 6 with the root preserved, usage errors refused before any directory exists;
- opt-in representative profile (skipped unless `SHADOW_SOAK_PROFILE=representative`).

## Limits and non-claims

- Synthetic, offline, mock-only. Nothing about a real feed, broker acknowledgement, partial fills,
  wall-clock freshness, live receipt latency or sustained connected-paper operation is shown.
- The restart chain simulates process death at delivery boundaries through the runner's injected
  interruptions; it does not kill processes with signals, pull power or test SQLite durability beyond
  SQLite's own atomic commit.
- The feed is the SHADOW-01 fixture repeated; it adds breadth over time and identities, not new
  message shapes. Hydration, reposts and unverified authors remain covered by the coordinator tests.
- Wall time is a bound and a measurement on one machine; no throughput, latency or uptime claim.
- The no-network guard is an in-process check, not an operating-system sandbox.
- The KPL cumulative-context behaviour is reported, not judged: whether an unexited blocked idea
  should keep shadowing later entries in the publisher-book view is a product/interpreter question
  outside this issue's scope.
