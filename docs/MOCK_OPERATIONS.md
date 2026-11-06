# Mock Operations — read-only operational view (OPS-01)

**Status:** mock-only observability over the existing mock ledgers. **Nothing on this page can submit,
amend or cancel a trade, and viewing or refreshing it cannot change a ledger.** It adds no interpreter,
validator, policy, journal, contract migration or trading control. Issue: `tsaid1/tradingbot#9`.

## What it shows

`dashboard/pages/2_Mock_Operations.py` renders one `OperationsSnapshot` built by
`src/tradingbot/operations_view.py` from the two existing mock engines:

| Section | Source tables | Decoded with |
|---|---|---|
| As-of header | `events.created_at`, `replay_posts.processed_at` | — |
| Mock positions | `positions` (Sprint 1 journal), `replay_ideas` (Sprint 1.1 replay) | `Position`-shaped rows plus a protection status |
| Fictional orders and fills | `events.order_json`, `replay_actions.result_json` | `MockOrder`, `ActionOutcome` / `PaperOrder` |
| Validation outcomes | `events.status` + `decision_json`, `replay_actions.result_json` | `Decision`, `ActionOutcome` |
| Reconciliation | all of the above | deterministic checks listed on the page |

Every timestamp is explicit: the **snapshot as-of** (when the ledgers were read), each ledger's **last write**
with its basis column, and each outcome's **source timestamp age** against the shared `MockPolicy`
signal window. A last write is reported as **UNKNOWN** (never compared or aged) when any row's write
timestamp is missing or has no timezone; the page says how many rows are untimed. Source text is never
selected from the ledgers, so paid post text cannot reach the page.

## Read-only by construction

* All reads go through `operations_view.read_only_connection`, a SQLite `mode=ro` URI connection. SQLite
  itself refuses writes through it (`attempt to write a readonly database`), and the module contains no
  write statement and no engine construction on the read path. The page constructs no `Journal` or
  `PaperEventReplay`; it only reads the paths held in `st.session_state`.
* Each ledger is read inside **one deferred read transaction** (`BEGIN` … rollback on close), so every
  SELECT of a snapshot sees the same state. An engine committing between the fills read and the positions
  read cannot make the view compare old fills with new quantities: in WAL mode (the journal) the writer
  proceeds and the view keeps its snapshot; in rollback-journal mode (the replay ledger) the writer's commit
  waits for the view's milliseconds-long read. Tests drive both cases with a second connection.
* A corrupt ledger never aborts the view. A non-SQLite or malformed file is reported as `LEDGER_UNREADABLE`
  and the other ledger still renders. A stored decision, order or outcome record that does not decode with
  the engine's contract is reported as `JOURNAL_RECORD_UNDECODABLE` / `REPLAY_RECORD_UNDECODABLE` (error
  types only, never the payload), the affected row is marked `RECORD_UNDECODABLE` / `UNDECODABLE` for
  review, that ledger's fills are **not** reconciled, and reconciliation is marked incomplete.
* A missing or foreign ledger file is reported as a `SERIOUS` discrepancy (`LEDGER_FILE_MISSING`,
  `LEDGER_UNREADABLE`); the view never creates a database.
* The only control is **Refresh view (read-only)**, which re-reads the same files. There are no trade
  inputs, forms, or action buttons.
* `tests/test_mock_operations.py` verifies all of this: write attempts through the view's connection raise,
  the module's SQL strings are read-only, three snapshot builds and repeated page reruns (including refresh
  clicks) leave the logical content of both ledgers identical, and the page never touches a `data/`
  directory or `TRADINGBOT_DB`.

Reading a WAL-mode journal through a read-only connection may create SQLite's `-wal`/`-shm` sidecar files
next to the database; they are not ledger content and the tests exclude them from the file comparison.

## Synthetic inputs only

The page's default inputs are fictional ledgers written by the **existing** engines
(`SignalPipeline` + `Journal`, `PaperEventReplay`) into a fresh task-local temporary directory
(`operations_view.new_fixture_directory`, `tempfile.mkdtemp`). Two scenarios are offered in the sidebar:

* **Synthetic: empty run** — both schemas exist, no rows. The page says so explicitly ("Empty run", "Nothing
  to reconcile") and does not call the ledgers healthy.
* **Synthetic: populated run** — fictional symbols `MOCKA`–`MOCKE`: an open, a 50% reduction, a short that is
  closed, commentary (`NO_TRADE`), a stale source (`BLOCK`), an unknown idea reference (`REVIEW`), an
  interpreter error (`ERROR`), an incomplete reservation (`RECEIVED`), duplicate resubmissions (recorded
  nothing), and on the replay side an ETF open, an atomic ADD + MODIFY, a reduction, a short that is
  closed, a historical report (`RECONCILE`), a conditional (`HOLD`), an unauthenticated author (`BLOCK`),
  a repost (`NO_TRADE`), a truncated post (`HOLD`) and a stale-quote exit (`BLOCK`).

The fixture is built once per scenario and session; reruns and refreshes only read it. The page never opens
the repository's `data/` directory, an existing database, a broker or the network. The read-model itself
accepts any `MockLedgers` paths (see `MockLedgers.from_engines`), which the tests use to prove drift detection
on a corrupted copy; the page offers no selector for real ledgers.

## Vocabulary

**Protection** (per position; none of these means "protected"):

| Status | Meaning |
|---|---|
| `UNKNOWN` | The Sprint 1 journal does not track stop/target levels. |
| `NOT_RECORDED` | The replay ledger tracks levels but none is recorded for this idea. |
| `RECORDED_MOCK_UNVERIFIED` | A level exists in the mock ledger only; no broker-native protection exists or was verified. |

No market quote is stored in either ledger, so valuation and P&L are unknown and not displayed.

**Freshness** (`FRESH` / `STALE` / `FUTURE` / `UNKNOWN`): a source timestamp's age at as-of against
`MockPolicy.max_signal_age_seconds` (the gateway's window, with its 5 s future tolerance), or the
snapshot's age at render against the display threshold `SNAPSHOT_STALE_AFTER_SECONDS` (60 s). A timestamp
without a timezone is `UNKNOWN`. Freshness never changes a recorded outcome; it says what the source could
authorize *now*.

**Needs review**: outcomes with status `RECEIVED`, `ERROR`, `REVIEW`, `HOLD` or `RECONCILE` — nothing was
filled and nothing was finally rejected, so an operator must look.

**Discrepancies** (`SERIOUS` rendered as errors, `WARNING` as warnings):

| Code | Check |
|---|---|
| `LEDGER_FILE_MISSING`, `LEDGER_UNREADABLE` | A configured ledger is missing, is not a SQLite database, is malformed, or lacks the engine's tables. |
| `JOURNAL_RECORD_UNDECODABLE`, `REPLAY_RECORD_UNDECODABLE` | A stored decision/order/outcome record did not decode with the engine's contract; that ledger's fills are not reconciled. |
| `JOURNAL_RESERVATION_INCOMPLETE` | An event is still `RECEIVED`: reserved, never completed, and the duplicate guard will refuse a retry. |
| `JOURNAL_HISTORY_TRUNCATED` | More events exist than were read (`journal_event_limit`); fill totals were not reconciled. |
| `JOURNAL_POSITION_QUANTITY_MISMATCH`, `JOURNAL_FILL_WITHOUT_POSITION`, `JOURNAL_POSITION_WITHOUT_FILL` | Journal positions versus the signed sum of recorded fictional fills per idea. |
| `REPLAY_IDEA_QUANTITY_MISMATCH`, `REPLAY_FILL_WITHOUT_IDEA`, `REPLAY_IDEA_WITHOUT_FILL` | Replay ideas versus the signed sum of recorded paper fills (latest evaluation attempt per post). |
| `REPLAY_IDEA_STATUS_INCONSISTENT` | An `OPEN` idea with quantity 0, or a `CLOSED` idea with quantity > 0. |
| `REPLAY_HISTORICAL_REPORT_UNRECONCILED` | A `RECONCILE` outcome: the source reported a past action that the ledger did not execute. |

"No discrepancies" means only that these checks found nothing. The page states that this is not a health
certification: protection is unverified, no quotes exist and no broker state was compared.

## Running it

```bash
# From the repository root, with imports pinned to this checkout:
PYTHONPATH=src streamlit run dashboard/pages/2_Mock_Operations.py --server.headless true
# or as a page of the multipage app:
PYTHONPATH=src streamlit run dashboard/app.py
PYTHONPATH=src python -m pytest tests/test_mock_operations.py
```

The sidebar prints the resolved `tradingbot` import origin so you can confirm the page is running this
checkout's code and not another editable install.

## Limitations

* Observability only: no quotes, valuation, P&L, exposure netting across the two independent ledgers, or
  broker reconciliation (no broker exists in this repository).
* Replay outcomes are read from the latest evaluation attempt of each post revision, matching
  `PaperEventReplay.outcomes()`; earlier attempts of a truncated post only ever recorded `HOLD`.
* The journal read is capped at `JOURNAL_EVENT_LIMIT` (1000) events; beyond that the page reports
  `JOURNAL_HISTORY_TRUNCATED` instead of guessing. The journal's last write is computed from the rows read,
  which always include the newest ones.
* While the view holds its read transaction (milliseconds), a replay-ledger writer's commit waits; the
  engines' 10 s busy timeout covers this comfortably.
* The browser does not auto-refresh. The as-of timestamp is the truth; press **Refresh view (read-only)**.
* Synthetic fixtures are illustrative, not a corpus; they prove rendering and read-only behaviour, not
  interpreter accuracy.
