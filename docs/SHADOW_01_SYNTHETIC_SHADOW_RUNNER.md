# SHADOW-01 — Deterministic synthetic shadow runner and restart proof

**Status:** offline operational evidence only. **MOCK ONLY.** This is not a claim of broker or
live-source reliability, nor sustained connected-paper acceptance. Closes
[issue #11](https://github.com/tsaid1/tradingbot/issues/11).

## What it demonstrates

A finite, newly authored synthetic event feed is replayed under a **virtual clock** through the
existing public chain, with a durable checkpoint after every committed delivery:

1. **Restart proof.** A straight run and a run interrupted at *any* delivery boundary and then
   resumed converge on the same mock ledger (ideas, orders, recorded outcomes) and the same
   aggregate outcome counts. The test does this exhaustively: every delivery index × both crash
   windows (before the ledger transaction; after it committed but before the checkpoint write).
2. **Idempotency proof.** Immediate, late and post-close duplicate deliveries, a re-delivery of a
   blocked post, and a tampered re-delivery (same identity, different bytes) add **no** mock order
   and **no** ledger row. Positive controls show the mechanism is real: a genuinely new post does
   fill, and the only thing separating a stale BLOCK from a SIMULATED amendment is the virtual
   delivery time.
3. **Path coverage.** Positive OPEN → REDUCE → CLOSE (long) and OPEN → CLOSE (short) fills, plus the
   existing HOLD (conditional, edited revision), RECONCILE (past-tense exit report), NO_TRADE
   (commentary) and BLOCK (stale delivery, off-allowlist symbol) paths.

## What is reused, and what is new

| Reused unchanged | Role |
|---|---|
| `tkl_interpreter.TklInterpreter` | synthetic `SourcePost` text → `TradeAction` candidates (deterministic grammar, no AI backend) |
| `validator.gate_source` / `gate_action` / `fill_quantity` | the only authorization and sizing, called inside the coordinator |
| `event_replay.PaperEventReplay` | the mock ledger of record (SQLite) with once-only protection per `(source_id, revision, post_hash)` |
| `models.MockPolicy` | the one mock policy contract (`quantity=4`, `allowed_symbols={ZQX, VBN}` for this run) |

| New (this issue) | Role |
|---|---|
| `src/tradingbot/shadow_runner.py` | feed/checkpoint/report contracts, virtual clock, delivery loop, interrupt injection, ledger-derived report |
| `scripts/run_shadow_synthetic.py` | documented command |
| `tests/test_shadow_runner.py` | deterministic regression suite |
| this document | run documentation |

No interpreter, policy, journal or trading adapter was created. No shared schema, validator,
storage, parser, dashboard or README file was edited. The Sprint 1 `storage.Journal` path was not
used because it reads the wall clock for freshness and cannot express RECONCILE; the Sprint 1.1
coordinator is the engine with an injectable `now` and the full HOLD / RECONCILE vocabulary.

## Run it

```bash
python -m pip install -e '.[dev]'

# 1. straight run in a temporary directory (removed when it completes; preserved, path printed, if it pauses)
python scripts/run_shadow_synthetic.py

# 2. simulate a crash after the 7th delivery committed but before its checkpoint was written
python scripts/run_shadow_synthetic.py --workdir /tmp/shadow-demo --interrupt-at 6 --interrupt-phase after-commit
#    -> exit 3, checkpoint cursor stays at 6, the ledger already holds delivery 6

# 3. resume; the re-delivered event is recognised as a duplicate and nothing fills twice
python scripts/run_shadow_synthetic.py --workdir /tmp/shadow-demo --resume --json /tmp/shadow-demo.json
#    -> exit 0; convergence digest identical to the straight run

# bounded runs pause and resume the same way
python scripts/run_shadow_synthetic.py --workdir /tmp/shadow-paced --max-deliveries 5     # exit 4
python scripts/run_shadow_synthetic.py --workdir /tmp/shadow-paced --resume              # exit 0

pytest tests/test_shadow_runner.py
```

Exit codes: `0` complete, `2` refused (occupied directory, invalid checkpoint, usage error),
`3` interrupted by the injected crash, `4` paused by a bound. `--max-seconds` (default 60) is a
wall-clock stop guard only; it is never a decision input. Without `--workdir` the script uses a
private temporary directory that is removed only when the run completes; a paused run keeps it and
prints the exact `--workdir … --resume` command, so bounded default-mode runs remain resumable.

The runner **refuses** a working directory that contains anything unless `--resume` is given, so
it can never read or write `data/`, an existing demo database, or any private path by accident.
Fresh runs use pytest temporary directories or an explicitly chosen empty directory.

## The synthetic feed (`synthetic_feed()`)

Virtual epoch `2026-11-06T14:00:00Z`; tickers ZQX, VBN, KPL and every price are invented. Quotes are
synthetic mids ± 0.05 stamped at the virtual delivery time. A timely delivery arrives 5 s after
publication.

| # | post | delivered at (virtual) | text (synthetic) | expected |
|---|---|---|---|---|
| 0 | e01 | +0:00:05 | We are now long $ZQX at $41.20. Stop: $39.80. Target: $45.00. | SIMULATED BUY 4 @ 41.25 |
| 1 | e01 | +0:00:08 | (same) | DUPLICATE, no fill |
| 2 | e02 | +1:00:05 | If $ZQX breaks above $43.00 we will add to the position. | HOLD `CONDITIONAL_TRIGGER_NOT_IMPLEMENTED` |
| 3 | e03 | +2:00:05 | Taking profits on half of our $ZQX long at $43.60. | SIMULATED SELL 2 @ 43.55 |
| 4 | e03 | +2:05:00 | (same, late) | DUPLICATE, no fill |
| 5 | e04 | +3:00:05 | We remain bullish on $ZQX and expect a move toward $45.00. | NO_TRADE `COMMENTARY_OR_SUMMARY` |
| 6 | e05 | +4:00:05 | Closing the remaining $ZQX long here at $44.70. | SIMULATED SELL 2 @ 44.65, idea CLOSED |
| 7 | e01 | +4:01:00 | (OPEN replayed after the close) | DUPLICATE, position not reopened |
| 8 | e06 | +4:05:05 | We have closed our $ZQX long at $44.70 for a +8.5% gain. | RECONCILE `HISTORICAL_REPORT_NO_ORDER` |
| 9 | e07 | +5:00:05 | We are now short $VBN at $120.50. Stop: $124.00. Target: $112.00. | SIMULATED SELL 4 @ 120.45 |
| 10 | e08 | +6:20:05 | Lowering our $VBN stop to $122.00. (published +6:00:00) | BLOCK `SOURCE_STALE_OR_FUTURE` |
| 11 | e08 | +6:21:00 | (re-delivery of the blocked post) | DUPLICATE, still BLOCK, not retried |
| 12 | e09 | +7:00:05 | Covering our $VBN short here at $118.40. | SIMULATED BUY 4 @ 118.45, idea CLOSED |
| 13 | e05 rev 2 | +8:20:05 | Closing the remaining $ZQX long here at $44.75. (edit) | HOLD `SOURCE_EDIT_REQUIRES_REVIEW` |
| 14 | e10 | +9:00:05 | We are now long $KPL at $18.30. Stop: $17.60. Target: $20.00. | BLOCK `INSTRUMENT_NOT_ALLOWLISTED` |
| 15 | e05 | +10:00:00 | …here at $44.71. (tampered bytes, same identity) | DUPLICATE, BLOCK `SOURCE_HASH_CHANGED_WITHOUT_REVISION` |
| 16 | e09 | +11:40:00 | (same, late) | DUPLICATE, no fill |

Final ledger: `e01:a1` ZQX LONG CLOSED qty 0 (stop 39.80), `e07:a1` VBN SHORT CLOSED qty 0 (stop
124.00: the stale MODIFY never applied). Five mock orders; eleven ledger rows for eleven unique post
versions; outcome counts `SIMULATED 5, HOLD 2, BLOCK 2, NO_TRADE 1, RECONCILE 1`. Observed
convergence digest on 2026-11-06: `52ebd151ebe46a736aa4a6c7231840d7d2c02d2cd67106894fa88ab4002d1833`
(the digest changes if the feed or the report contract changes; the tests compare runs, not this value).

## Checkpoint contract (`checkpoint.json`)

Strict JSON (`extra="forbid"`), written atomically (temp file + rename) **after** the coordinator's
SQLite transaction commits:

| field | validated on resume |
|---|---|
| `schema_version` | must be `shadow-checkpoint-1` |
| `mock_only` | must be `true` |
| `feed_id`, `feed_digest` | must match the feed in use (SHA-256 of its canonical JSON) |
| `policy_digest` | must match the mock policy in use (allowlist sorted before hashing) |
| `ledger_file` | must be `shadow_ledger.sqlite3`, which must exist |
| `cursor` | `0 ≤ cursor ≤ deliveries`; deliveries `[0, cursor)` are committed |
| `virtual_now` | delivery time of the last committed delivery (informational) |

On resume the runner also replays the committed prefix through the interpreter only, to rebuild the
point-in-time context, and requires every prefix post that produced actions to have a recorded
outcome. A checkpoint that is *ahead* of the ledger (for example the database was replaced) is
refused rather than skipping events. Malformed JSON, unknown fields, a different feed, a different
policy and an out-of-range cursor are all refused before anything is delivered.

## Crash-boundary claims (read carefully)

- **Claimed:** between any two deliveries, and inside the window after the ledger commit but before
  the checkpoint write, a crash followed by a resume yields the same final ledger and aggregate
  outcomes as an uninterrupted run. Verified for every boundary of the shipped feed.
- **Claimed:** an identical re-delivery has at most one ledger effect. This is the coordinator's
  `(source_id, revision, post_hash)` protection, not something the runner adds.
- **Not claimed:** exactly-once *processing*. Deliveries are processed **at least once**; after a
  post-commit crash the resumed run evaluates the same event again and counts it as a duplicate.
  `this_run` delivery statistics therefore legitimately differ between a straight and a resumed run;
  the ledger-derived report does not.
- **Not claimed:** anything about a crash *inside* the SQLite transaction beyond SQLite's own
  atomic-commit guarantee, about wall-clock freshness, live receipt latency, broker acknowledgement,
  partial fills, or a real feed. The virtual clock is a deterministic input, not time.
- **Known gateway behaviour surfaced, unchanged:** a stale delivery of a risk-reducing MODIFY is
  blocked and stays blocked on re-delivery (no blind retry); a past-tense exit report reconciles and
  never sells. Both are existing policy, not runner decisions.

## Tests (`tests/test_shadow_runner.py`)

- straight run: exact per-delivery statuses, five orders, two closed ideas, outcome and reason
  counts, virtual timestamps; two fresh runs are byte-identical;
- idempotency: duplicate / late / post-close / blocked / tampered re-deliveries add no orders or
  rows; duplicates return the recorded outcome verbatim; positive controls (a new post fills; an
  on-time MODIFY amends and changes the digest);
- restart proof: interrupt at every delivery index × both windows, then resume and compare the
  ledger, the digest, the row counts and the at-least-once duplicate accounting; delivery cap and a
  fake wall-clock budget pause and resume; resuming a completed run delivers nothing;
- refusals and validation: occupied directory, missing state, twelve checkpoint tampers, changed
  policy, changed feed, ledger behind the checkpoint, malformed JSON, strict feed contract;
- no network socket is opened during a run;
- the documented command: fresh run, simulated crash (exit 3), bounded resume (exit 4), final
  resume (exit 0) across separate processes with different `PYTHONHASHSEED`s, identical digests;
  a default-mode run paused by the delivery cap or by the wall-clock budget preserves its temporary
  directory and the advertised resume succeeds; a completed default-mode run removes it, and an
  explicit `--workdir` is never removed.

## Limits and follow-ups

- The feed is small and hand-authored; it is a regression specification, not a soak at scale.
- Hydration of truncated posts, reposts and unverified authors are covered by the existing
  coordinator tests and were not duplicated here.
- A real shadow deployment needs a trusted adapter for `SourcePost` provenance and real receipt
  times; this runner stamps `received_at` from the virtual delivery time.
