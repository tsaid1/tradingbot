"""Read-only shadow-run health and recovery visibility (OPS-02, issue #18).

What this module is
-------------------
A *read-model* over the artifacts that the merged SHADOW-01 synthetic shadow runner leaves behind:

* ``checkpoint.json`` (``shadow_runner.ShadowCheckpoint``): identity digests and the durable cursor;
* ``shadow_ledger.sqlite3`` (``event_replay.PaperEventReplay``): the mock ledger of record, read
  through ``operations_view`` exactly as OPS-01 reads it (SQLite ``mode=ro``);
* an optional report (``shadow_runner.ShadowRunReport``) as written by the documented command's
  ``--json`` option.

From those files alone it states: which run identity is on record, how far the cursor got, whether
completion, a pause or an unexplained interruption was *observed*, how old the observation is,
whether the runner's documented resume validation would accept the state, and what the existing
ledger reconciliation reports. Everything is derived from evidence; nothing is inferred beyond it.

What this module is not
-----------------------
Not a runner, controller, interpreter, validator, policy or journal. It never starts, stops, resumes
or recovers a run, never submits or amends an order, and never writes to a checkpoint, ledger or
report. The runner emits no heartbeat, so process liveness is always ``UNKNOWN``: a recently written
checkpoint shows recent progress, not a live process. An observed completion of the finite synthetic
feed is a mock-ledger fact, not account flatness, verified protection or paper acceptance, and no
state shown here authorizes anything.

Clocks
------
The checkpoint's ``virtual_now`` and every ledger timestamp are VIRTUAL event time (the feed's
synthetic clock, which the runner passes to the coordinator as ``now``). The only wall-clock evidence
is filesystem metadata (file modification times); it is labelled as an observed write, never as a
heartbeat, and the two clocks are never compared with each other.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from pydantic import ValidationError

from tradingbot.models import MockPolicy, StrictModel
from tradingbot.operations_view import (
    FUTURE_TOLERANCE_SECONDS,
    Discrepancy,
    FillRow,
    Freshness,
    MockLedgers,
    PositionRow,
    build_snapshot,
    freshness,
    read_only_connection,
)
from tradingbot.shadow_runner import (
    CHECKPOINT_FILE,
    CHECKPOINT_SCHEMA,
    LEDGER_FILE,
    NON_PRODUCTION_LABEL,
    InterruptSpec,
    ShadowCheckpoint,
    ShadowFeed,
    ShadowInterrupted,
    ShadowRunner,
    ShadowRunReport,
    policy_digest,
    shadow_policy,
    synthetic_feed,
)

HEALTH_LABEL = (
    "SYNTHETIC SHADOW RUN HEALTH - READ-ONLY OBSERVATION - NOT PRODUCTION - "
    "NOT A TRADING, RECOVERY OR ACCOUNT AUTHORIZATION"
)
REPORT_FILE = "shadow_report.json"
# Display threshold for "no recent checkpoint write". NOT a runner setting and NOT trading policy.
PROGRESS_STALE_AFTER_SECONDS = 300
WRITE_TIME_BASIS = "filesystem modification time (an observed file write; not a runner heartbeat)"
VIRTUAL_TIME_NOTE = (
    "virtual event time from the synthetic feed's clock; never compared with the wall clock"
)
LIVENESS_NOTE = (
    "The shadow runner emits no heartbeat, so whether a process is running cannot be observed from "
    "its artifacts. A recent checkpoint write shows recent progress only; an old one shows only that "
    "no progress was written since."
)
RESUMABILITY_NOTE = (
    "A read-only prediction from checks that mirror the runner's documented resume validation. The "
    "runner re-validates on resume; this view cannot resume, start, stop or recover anything."
)
FLATNESS_NOTE = (
    "Ideas marked CLOSED with quantity 0 are mock-ledger facts. They are not account flatness, not "
    "verified protection and not evidence about any broker account."
)
OBSERVATION_LIMITS = [
    "No heartbeat: the runner writes no liveness signal; process state is UNKNOWN in every case.",
    (
        "File write times are filesystem metadata, not runner timestamps; copying or restoring files "
        "changes them. They show when a file was last written, never whether a process is alive."
    ),
    (
        "virtual_now and all ledger timestamps are virtual event time; they are never aged against "
        "the wall clock, and the fake clock never moves them."
    ),
    (
        "COMPLETE means the finite synthetic feed was fully delivered to the mock ledger. It is not "
        "account flatness, verified protection, broker state or paper acceptance."
    ),
    (
        "Resumability is a read-only prediction of the runner's resume validation; this view cannot "
        "resume, start, stop or recover a run, and no state here authorizes any action."
    ),
    (
        "Ledger reconciliation reuses the OPS-01 read-model; 'no discrepancies' means only that its "
        "deterministic checks found nothing."
    ),
]

RunState = Literal["COMPLETE", "PAUSED", "INCOMPLETE", "CONTRADICTORY", "INVALID"]
Liveness = Literal["UNKNOWN"]
Resumability = Literal["RESUMABLE_BY_CONTRACT", "NOT_RESUMABLE", "NOTHING_TO_RESUME", "UNKNOWN"]
Severity = Literal["SERIOUS", "WARNING", "INFO"]
CheckResult = Literal["PASS", "FAIL", "SKIPPED"]
FileRole = Literal["checkpoint", "ledger", "report"]
ClockMode = Literal["real", "fresh", "stale", "future"]
PAUSE_STATUSES = frozenset({"DELIVERY_CAP_REACHED", "WALL_BUDGET_EXHAUSTED"})
SEVERITY_ORDER = {"SERIOUS": 0, "WARNING": 1, "INFO": 2}

Scenario = Literal[
    "completed",
    "paused_by_delivery_cap",
    "interrupted_after_commit",
    "interrupted_before_commit",
    "stale_report",
    "malformed_report",
    "missing_checkpoint",
    "malformed_checkpoint",
    "missing_ledger",
    "foreign_feed_identity",
    "cursor_beyond_feed",
    "checkpoint_ahead_of_ledger",
    "contradictory_report",
]
SCENARIO_DESCRIPTIONS: dict[str, str] = {
    "completed": "Straight run to the end of the finite feed; report written.",
    "paused_by_delivery_cap": "Run stopped by --max-deliveries 5; pause report written.",
    "interrupted_after_commit": (
        "Simulated crash after delivery 6 committed but before its checkpoint was written; no report."
    ),
    "interrupted_before_commit": "Simulated crash before delivery 6 was committed; no report.",
    "stale_report": "Paused at 5 with a report, then resumed to completion without a new report.",
    "malformed_report": "Completed run whose report file is not valid JSON.",
    "missing_checkpoint": "Completed run whose checkpoint.json was removed.",
    "malformed_checkpoint": "Paused run whose checkpoint.json is not valid JSON.",
    "missing_ledger": "Paused run whose ledger file was removed.",
    "foreign_feed_identity": "Paused run whose checkpoint carries another feed's digest.",
    "cursor_beyond_feed": "Completed run whose checkpoint cursor exceeds the feed length.",
    "checkpoint_ahead_of_ledger": "Paused run whose ledger was replaced by an empty database.",
    "contradictory_report": "Paused run whose report claims COMPLETE at the full cursor.",
}
SCENARIOS: tuple[str, ...] = tuple(SCENARIO_DESCRIPTIONS)


# ----------------------------------------------------------------------------- evidence contracts


class ShadowRunArtifacts(StrictModel):
    """Where one shadow run's working directory and (optional) report live."""

    label: str
    workdir: Path
    report_path: Path | None = None


class FileObservation(StrictModel):
    """What the filesystem showed about one artifact, without interpreting its content."""

    role: FileRole
    path: str
    present: bool
    size_bytes: int | None = None
    # Timezone-aware wall-clock write time from the filesystem; None means UNKNOWN (never guessed).
    written_at: datetime | None = None
    written_at_basis: str = WRITE_TIME_BASIS


class ShadowRunEvidence(StrictModel):
    """Raw observations of a shadow run: file metadata and the artifacts' bytes, uninterpreted.

    ``checkpoint_text`` and ``report_text`` are the files' contents as read; the health model never
    carries them, and a malformed artifact is reported by error type only.
    """

    label: str
    workdir: str
    collected_at: datetime  # wall clock when the files were examined
    checkpoint: FileObservation
    ledger: FileObservation
    report: FileObservation | None = None  # None: no report path was supplied
    checkpoint_text: str | None = None
    report_text: str | None = None

    def without_write_times(self) -> ShadowRunEvidence:
        """The same evidence with every file write time dropped (UNTIMED), for the fake clock."""
        drop = {"written_at": None}
        return self.model_copy(
            update={
                "checkpoint": self.checkpoint.model_copy(update=drop),
                "ledger": self.ledger.model_copy(update=drop),
                "report": self.report.model_copy(update=drop) if self.report else None,
            }
        )


# ----------------------------------------------------------------------------- health contracts


class IdentityField(StrictModel):
    field: str
    expected: str  # from the feed and mock policy in use
    checkpoint: str | None = None  # None: the checkpoint does not carry it or is unavailable
    report: str | None = None  # None: the report does not carry it or is unavailable
    consistent: bool | None  # None: no artifact provided a value to compare


class ProgressView(StrictModel):
    deliveries_total: int
    cursor: int | None = None  # None when no valid checkpoint was read
    remaining: int | None = None
    fraction_complete: float | None = None
    next_delivery_post_key: str | None = None
    virtual_now: datetime | None = None  # VIRTUAL: last committed delivery's time (checkpoint)
    next_virtual_delivery: datetime | None = None  # VIRTUAL: time of deliveries[cursor]
    virtual_time_note: str = VIRTUAL_TIME_NOTE
    report_status: str | None = None
    report_cursor: int | None = None
    report_resumed_from: int | None = None
    report_this_run_processed: int | None = None
    report_this_run_duplicates: int | None = None
    report_this_run_note: str = (
        "this_run counters belong to one invocation and legitimately differ between a straight and a "
        "resumed run; the ledger-derived report does not"
    )


class ObservationView(StrictModel):
    collected_at: datetime  # real wall clock when the files were examined
    as_of: datetime  # the clock the evidence is judged against (real or fake)
    clock_label: str
    checkpoint_written_at: datetime | None = None
    checkpoint_age_seconds: float | None = None
    progress_freshness: Freshness
    ledger_written_at: datetime | None = None
    ledger_age_seconds: float | None = None
    report_written_at: datetime | None = None
    report_age_seconds: float | None = None
    write_time_basis: str = WRITE_TIME_BASIS
    stale_after_seconds: int = PROGRESS_STALE_AFTER_SECONDS
    future_tolerance_seconds: int = FUTURE_TOLERANCE_SECONDS
    process_liveness: Liveness = "UNKNOWN"
    liveness_note: str = LIVENESS_NOTE


class ResumabilityCheck(StrictModel):
    check: str
    description: str
    result: CheckResult
    detail: str


class Finding(StrictModel):
    severity: Severity
    code: str
    subject: str
    detail: str


class LedgerView(StrictModel):
    """The OPS-01 read-model of the shadow ledger, evaluated on the VIRTUAL clock."""

    path: str
    readable: bool
    virtual_as_of: datetime
    virtual_as_of_basis: str
    posts_on_record: int
    ideas: list[PositionRow]
    fills: list[FillRow]
    outcome_counts: dict[str, int]
    outcomes_needing_review: int
    discrepancies: list[Discrepancy]
    reconciliation_complete: bool
    checks_performed: list[str]
    flatness_note: str = FLATNESS_NOTE


class ShadowRunHealth(StrictModel):
    """Everything the page renders. Derived from ``ShadowRunEvidence`` plus one read of the ledger."""

    label: str = HEALTH_LABEL
    evidence_label: str
    workdir: str
    files: list[FileObservation]
    observation: ObservationView
    run_state: RunState
    headline: str
    attention_required: bool
    identity: list[IdentityField]
    progress: ProgressView
    resumability: Resumability
    resumability_checks: list[ResumabilityCheck]
    resumability_note: str = RESUMABILITY_NOTE
    findings: list[Finding]
    ledger: LedgerView | None = None  # None only when no ledger file exists
    observation_limits: list[str]

    def counts(self) -> dict[str, int]:
        ledger = self.ledger.discrepancies if self.ledger else []
        return {
            "serious_findings": sum(f.severity == "SERIOUS" for f in self.findings),
            "warning_findings": sum(f.severity == "WARNING" for f in self.findings),
            "info_findings": sum(f.severity == "INFO" for f in self.findings),
            "serious_ledger_discrepancies": sum(d.severity == "SERIOUS" for d in ledger),
            "warning_ledger_discrepancies": sum(d.severity == "WARNING" for d in ledger),
        }


# ----------------------------------------------------------------------------- evidence collection


def _observe_file(role: FileRole, path: Path) -> FileObservation:
    if not path.is_file():
        return FileObservation(role=role, path=str(path), present=False)
    stat = path.stat()
    return FileObservation(
        role=role,
        path=str(path),
        present=True,
        size_bytes=stat.st_size,
        written_at=datetime.fromtimestamp(stat.st_mtime, tz=UTC),
    )


def _read_text(observation: FileObservation) -> str | None:
    if not observation.present:
        return None
    return Path(observation.path).read_bytes().decode("utf-8", errors="replace")


def collect_evidence(
    artifacts: ShadowRunArtifacts, *, collected_at: datetime | None = None
) -> ShadowRunEvidence:
    """Stat and read the artifacts once. Reads bytes only; nothing is opened for writing."""
    workdir = Path(artifacts.workdir)
    checkpoint = _observe_file("checkpoint", workdir / CHECKPOINT_FILE)
    ledger = _observe_file("ledger", workdir / LEDGER_FILE)
    report = _observe_file("report", Path(artifacts.report_path)) if artifacts.report_path else None
    return ShadowRunEvidence(
        label=artifacts.label,
        workdir=str(workdir),
        collected_at=collected_at or datetime.now(UTC),
        checkpoint=checkpoint,
        ledger=ledger,
        report=report,
        checkpoint_text=_read_text(checkpoint),
        report_text=_read_text(report) if report else None,
    )


def fake_clock(evidence: ShadowRunEvidence, mode: ClockMode) -> tuple[datetime, str]:
    """A display-only as-of clock. ``real`` is the wall clock now; the fake modes are anchored on the
    observed checkpoint write (falling back to the ledger write, then to the collection time) so the
    same evidence can be judged as FRESH, STALE or FUTURE. Nothing on disk changes."""
    if mode == "real":
        return datetime.now(UTC), "real wall clock"
    anchor = evidence.checkpoint.written_at or evidence.ledger.written_at or evidence.collected_at
    offsets = {
        "fresh": (timedelta(seconds=30), "fake clock: 30 s after the last observed write"),
        "stale": (
            timedelta(seconds=PROGRESS_STALE_AFTER_SECONDS + 3600),
            "fake clock: 1 h 5 min after the last observed write",
        ),
        "future": (timedelta(hours=-1), "fake clock: 1 h before the last observed write"),
    }
    delta, label = offsets[mode]
    return anchor + delta, label


# ----------------------------------------------------------------------------- helpers


def _age_seconds(ts: datetime | None, now: datetime) -> float | None:
    if ts is None or ts.tzinfo is None or now.tzinfo is None:
        return None
    return (now - ts).total_seconds()


def _fmt_age(seconds: float | None) -> str:
    return "UNKNOWN" if seconds is None else f"{seconds:.0f} s"


def _parse(model, text: str | None) -> tuple[object | None, str | None]:
    """Parse an artifact with the runner's own strict contract. On failure return the error types
    only, never the payload."""
    if text is None:
        return None, None
    try:
        return model.model_validate_json(text), None
    except ValidationError as exc:
        kinds = sorted({str(e.get("type", "unknown")) for e in exc.errors()})
        return None, f"{len(exc.errors())} validation error(s): {', '.join(kinds)}"


def _ledger_identities(path: Path) -> set[tuple[str, int]] | None:
    """(source_id, revision) of every post row, through the OPS-01 read-only connection; None when
    the file is not a readable ledger (the OPS-01 snapshot reports that separately)."""
    try:
        with read_only_connection(path) as c:
            rows = c.execute("SELECT source_id, revision FROM replay_posts").fetchall()
    except sqlite3.DatabaseError:
        return None
    return {(r["source_id"], int(r["revision"])) for r in rows}


def _prefix_identities(feed: ShadowFeed, cursor: int) -> set[tuple[str, int]]:
    out: set[tuple[str, int]] = set()
    for delivery in feed.deliveries[:cursor]:
        spec = feed.post(delivery.post_key)
        out.add((spec.source_id, spec.revision))
    return out


def _ledger_view(path: Path, virtual_as_of: datetime, basis: str, policy: MockPolicy) -> LedgerView:
    snapshot = build_snapshot(
        MockLedgers(replay_path=path, label="shadow ledger"), now=virtual_as_of, policy=policy
    )
    activity = snapshot.activity[0]
    return LedgerView(
        path=str(path),
        readable=activity.readable,
        virtual_as_of=virtual_as_of,
        virtual_as_of_basis=basis,
        posts_on_record=activity.rows_on_record,
        ideas=snapshot.positions,
        fills=snapshot.fills,
        outcome_counts=dict(sorted(Counter(v.status for v in snapshot.validations).items())),
        outcomes_needing_review=snapshot.counts()["needs_review"],
        discrepancies=snapshot.discrepancies,
        reconciliation_complete=snapshot.reconciliation_complete,
        checks_performed=snapshot.checks_performed,
    )


def _report_matches_ledger(report: ShadowRunReport, ledger: LedgerView) -> list[str]:
    """Names of the report sections that disagree with the read-only ledger view (empty = agree)."""
    differences: list[str] = []
    report_orders = {
        (o.order_id, o.action, o.side, o.quantity, o.price, o.status) for o in report.ledger.orders
    }
    ledger_orders = {
        (f.order_id, f.action, f.side, f.quantity, f.price, f.status) for f in ledger.fills
    }
    if report_orders != ledger_orders:
        differences.append(f"orders ({len(report_orders)} in report vs {len(ledger_orders)} in ledger)")
    report_ideas = {
        (i.idea_id, i.symbol, i.position_side, i.quantity, i.stop_price, i.target_price, i.status)
        for i in report.ledger.ideas
    }
    ledger_ideas = {
        (p.idea_id, p.symbol, p.side, p.quantity, p.stop_price, p.target_price, p.idea_status)
        for p in ledger.ideas
    }
    if report_ideas != ledger_ideas:
        differences.append(f"ideas ({len(report_ideas)} in report vs {len(ledger_ideas)} in ledger)")
    if report.ledger.outcome_counts != ledger.outcome_counts:
        differences.append("outcome counts")
    return differences


# ----------------------------------------------------------------------------- assessment


def assess_shadow_run(
    evidence: ShadowRunEvidence,
    *,
    feed: ShadowFeed | None = None,
    policy: MockPolicy | None = None,
    now: datetime | None = None,
    clock_label: str = "real wall clock",
) -> ShadowRunHealth:
    """Judge the evidence against the feed and mock policy in use, as of ``now``.

    Deterministic for unchanged files and the same ``now``. The only I/O is the OPS-01 read-only
    SQLite read of the ledger; no file is created or written.
    """
    feed = feed or synthetic_feed()
    policy = policy or shadow_policy()
    now = now or datetime.now(UTC)
    total = len(feed.deliveries)
    expected_feed_digest = feed.digest()
    expected_policy_digest = policy_digest(policy)
    findings: list[Finding] = []
    checks: list[ResumabilityCheck] = []

    def finding(severity: Severity, code: str, subject: str, detail: str) -> None:
        findings.append(Finding(severity=severity, code=code, subject=subject, detail=detail))

    def check(code: str, description: str, result: CheckResult, detail: str) -> None:
        checks.append(
            ResumabilityCheck(check=code, description=description, result=result, detail=detail)
        )

    # --- parse the artifacts with the runner's own strict contracts -------------------------------
    checkpoint: ShadowCheckpoint | None = None
    if not evidence.checkpoint.present:
        finding("SERIOUS", "CHECKPOINT_MISSING", CHECKPOINT_FILE, "No checkpoint file was found.")
        check("CHECKPOINT_VALID", "checkpoint.json exists and parses as ShadowCheckpoint", "FAIL",
              "missing")
    else:
        parsed, error = _parse(ShadowCheckpoint, evidence.checkpoint_text)
        if parsed is None:
            finding("SERIOUS", "CHECKPOINT_MALFORMED", CHECKPOINT_FILE,
                    f"The checkpoint did not parse with the runner's contract ({error}); its content "
                    "is not shown.")
            check("CHECKPOINT_VALID", "checkpoint.json exists and parses as ShadowCheckpoint", "FAIL",
                  str(error))
        else:
            checkpoint = parsed
            check("CHECKPOINT_VALID", "checkpoint.json exists and parses as ShadowCheckpoint", "PASS",
                  f"schema {checkpoint.schema_version}, cursor {checkpoint.cursor}")

    report: ShadowRunReport | None = None
    if evidence.report is None or not evidence.report.present:
        finding("INFO", "NO_REPORT", REPORT_FILE,
                "No run report was supplied or found. A pause and an interruption cannot be told "
                "apart from the checkpoint alone.")
    else:
        parsed, error = _parse(ShadowRunReport, evidence.report_text)
        if parsed is None:
            finding("WARNING", "REPORT_MALFORMED", evidence.report.path,
                    f"The report did not parse with the runner's contract ({error}); its content is "
                    "not shown and the run state is taken from the checkpoint alone.")
        else:
            report = parsed

    if not evidence.ledger.present:
        finding("SERIOUS", "LEDGER_MISSING", LEDGER_FILE, "No ledger file was found.")
        check("LEDGER_PRESENT", "shadow_ledger.sqlite3 exists", "FAIL", "missing")
    else:
        check("LEDGER_PRESENT", "shadow_ledger.sqlite3 exists", "PASS",
              f"{evidence.ledger.size_bytes} bytes")

    # --- identity --------------------------------------------------------------------------------
    def identity(field: str, expected: str, cp: str | None, rp: str | None) -> IdentityField:
        values = [v for v in (cp, rp) if v is not None]
        return IdentityField(
            field=field, expected=expected, checkpoint=cp, report=rp,
            consistent=(all(v == expected for v in values) if values else None),
        )

    cp = checkpoint
    rp = report
    identity_rows = [
        identity("schema_version", CHECKPOINT_SCHEMA, cp.schema_version if cp else None, None),
        identity("mock_only", "true", "true" if cp else None, None),
        identity("feed_id", feed.feed_id, cp.feed_id if cp else None, rp.feed_id if rp else None),
        identity("feed_digest", expected_feed_digest, cp.feed_digest if cp else None,
                 rp.feed_digest if rp else None),
        identity("policy_digest", expected_policy_digest, cp.policy_digest if cp else None,
                 rp.policy_digest if rp else None),
        identity("ledger_file", LEDGER_FILE, cp.ledger_file if cp else None, None),
        identity("deliveries_total", str(total), None, str(rp.deliveries_total) if rp else None),
        identity("label", NON_PRODUCTION_LABEL, None, rp.label if rp else None),
    ]
    feed_identity_ok = policy_identity_ok = ledger_name_ok = cursor_ok = False
    if cp is not None:
        feed_identity_ok = cp.feed_id == feed.feed_id and cp.feed_digest == expected_feed_digest
        policy_identity_ok = cp.policy_digest == expected_policy_digest
        ledger_name_ok = cp.ledger_file == LEDGER_FILE
        cursor_ok = cp.cursor <= total
        if not feed_identity_ok:
            finding("SERIOUS", "FEED_IDENTITY_MISMATCH", CHECKPOINT_FILE,
                    f"The checkpoint names feed {cp.feed_id!r} with digest {cp.feed_digest[:12]}…; the "
                    f"feed in use is {feed.feed_id!r} with digest {expected_feed_digest[:12]}…. The "
                    "runner refuses to resume across feeds, and the cursor cannot be read against "
                    "this feed.")
        if not policy_identity_ok:
            finding("SERIOUS", "POLICY_IDENTITY_MISMATCH", CHECKPOINT_FILE,
                    "The checkpoint was written under a different mock policy digest; the runner "
                    "refuses to resume under another policy.")
        if not ledger_name_ok:
            finding("SERIOUS", "LEDGER_FILE_NAME_UNEXPECTED", CHECKPOINT_FILE,
                    f"The checkpoint names ledger file {cp.ledger_file!r}; the runner only accepts "
                    f"{LEDGER_FILE!r}.")
        if not cursor_ok:
            finding("SERIOUS", "CURSOR_BEYOND_FEED", CHECKPOINT_FILE,
                    f"The checkpoint cursor is {cp.cursor} but the feed has {total} deliveries; no "
                    "progress can be read from it.")
        check("FEED_IDENTITY", "checkpoint feed_id and feed_digest match the feed in use",
              "PASS" if feed_identity_ok else "FAIL", f"feed {cp.feed_id}")
        check("POLICY_IDENTITY", "checkpoint policy_digest matches the mock policy in use",
              "PASS" if policy_identity_ok else "FAIL", cp.policy_digest[:12] + "…")
        check("LEDGER_FILE_NAME", f"checkpoint names {LEDGER_FILE}",
              "PASS" if ledger_name_ok else "FAIL", repr(cp.ledger_file))
        check("CURSOR_WITHIN_FEED", f"0 <= cursor <= {total}",
              "PASS" if cursor_ok else "FAIL", f"cursor {cp.cursor}")
    else:
        for code, description in (
            ("FEED_IDENTITY", "checkpoint feed_id and feed_digest match the feed in use"),
            ("POLICY_IDENTITY", "checkpoint policy_digest matches the mock policy in use"),
            ("LEDGER_FILE_NAME", f"checkpoint names {LEDGER_FILE}"),
            ("CURSOR_WITHIN_FEED", f"0 <= cursor <= {total}"),
        ):
            check(code, description, "SKIPPED", "no valid checkpoint")

    # --- ledger view (virtual clock) and prefix consistency --------------------------------------
    ledger_view: LedgerView | None = None
    ledger_identities: set[tuple[str, int]] | None = None
    if evidence.ledger.present:
        if cp is not None and cp.virtual_now is not None:
            virtual_as_of, basis = cp.virtual_now, "checkpoint virtual_now (last committed delivery)"
        else:
            virtual_as_of, basis = feed.epoch, "feed epoch (no committed delivery or no checkpoint)"
        ledger_view = _ledger_view(Path(evidence.ledger.path), virtual_as_of, basis, policy)
        ledger_identities = _ledger_identities(Path(evidence.ledger.path))

    ledger_ahead_by_one = False
    prefix_description = "every post committed before the cursor has a ledger row (read-only proxy " \
                         "for the runner's recorded-outcome check)"
    if cp is None or not (feed_identity_ok and cursor_ok):
        check("LEDGER_NOT_BEHIND_CHECKPOINT", prefix_description, "SKIPPED",
              "cursor cannot be read against the feed in use")
    elif ledger_identities is None:
        check("LEDGER_NOT_BEHIND_CHECKPOINT", prefix_description, "SKIPPED",
              "ledger missing or unreadable")
    else:
        expected_prefix = _prefix_identities(feed, cp.cursor)
        missing = expected_prefix - ledger_identities
        extra = ledger_identities - expected_prefix
        if missing:
            finding("SERIOUS", "CHECKPOINT_AHEAD_OF_LEDGER", LEDGER_FILE,
                    f"The checkpoint cursor {cp.cursor} covers {len(expected_prefix)} unique post(s) but "
                    f"{len(missing)} of them have no ledger row; the ledger is behind the checkpoint "
                    "(for example replaced) and the runner refuses to resume rather than skip events.")
            check("LEDGER_NOT_BEHIND_CHECKPOINT", prefix_description, "FAIL",
                  f"{len(missing)} committed post(s) without a ledger row")
        else:
            check("LEDGER_NOT_BEHIND_CHECKPOINT", prefix_description, "PASS",
                  f"{len(expected_prefix)} committed post(s) all on record")
        if extra:
            next_identity = None
            if cp.cursor < total:
                spec = feed.post(feed.deliveries[cp.cursor].post_key)
                next_identity = (spec.source_id, spec.revision)
            if extra == {next_identity}:
                ledger_ahead_by_one = True
                finding("WARNING", "LEDGER_ONE_AHEAD_OF_CHECKPOINT", LEDGER_FILE,
                        f"The ledger holds delivery {cp.cursor} ({feed.deliveries[cp.cursor].post_key}), "
                        "which the checkpoint does not cover: consistent with an interruption after the "
                        "ledger commit and before the checkpoint write. On resume the runner re-delivers "
                        "it and the coordinator records a duplicate; nothing fills twice.")
            else:
                finding("SERIOUS", "LEDGER_CONTAINS_UNEXPECTED_POSTS", LEDGER_FILE,
                        f"The ledger holds {len(extra)} post(s) that the committed prefix of this feed "
                        "does not explain; the ledger may belong to another run.")

    # --- report versus checkpoint and ledger -----------------------------------------------------
    report_current = False
    if rp is not None:
        report_identity_ok = (
            rp.feed_id == feed.feed_id
            and rp.feed_digest == expected_feed_digest
            and rp.policy_digest == expected_policy_digest
            and rp.deliveries_total == total
        )
        if not report_identity_ok:
            finding("SERIOUS", "REPORT_IDENTITY_MISMATCH", REPORT_FILE,
                    "The report's feed, policy or delivery count does not match the feed and mock "
                    "policy in use; it describes another run.")
        if rp.label != NON_PRODUCTION_LABEL:
            finding("WARNING", "REPORT_LABEL_UNEXPECTED", REPORT_FILE,
                    "The report does not carry the runner's non-production label.")
        if cp is not None and report_identity_ok and cursor_ok:
            if rp.cursor > cp.cursor:
                finding("SERIOUS", "REPORT_CONTRADICTS_CHECKPOINT", REPORT_FILE,
                        f"The report claims cursor {rp.cursor} ({rp.status}) but the checkpoint cursor is "
                        f"{cp.cursor}; the two artifacts cannot both be current.")
            elif rp.cursor < cp.cursor:
                finding("WARNING", "REPORT_SUPERSEDED", REPORT_FILE,
                        f"The report ends at cursor {rp.cursor} ({rp.status}) but the checkpoint has "
                        f"advanced to {cp.cursor}; the report describes an earlier invocation and is not "
                        "compared with the ledger.")
            else:
                claims_complete = rp.status == "COMPLETE"
                at_end = cp.cursor == total
                if claims_complete != at_end:
                    finding("SERIOUS", "REPORT_CONTRADICTS_CHECKPOINT", REPORT_FILE,
                            f"The report status {rp.status} at cursor {rp.cursor} disagrees with a feed "
                            f"of {total} deliveries: {'a complete run must end at the last delivery' if claims_complete else 'a run paused at the last delivery is a completion'}.")
                else:
                    report_current = True
                    if ledger_view is not None and ledger_view.readable:
                        differences = _report_matches_ledger(rp, ledger_view)
                        if differences:
                            finding("SERIOUS", "REPORT_LEDGER_MISMATCH", REPORT_FILE,
                                    "The report's ledger summary disagrees with the read-only ledger "
                                    f"view: {'; '.join(differences)}. The report is not current for this "
                                    "ledger or the ledger changed after it was written.")
    # --- run state -------------------------------------------------------------------------------
    serious = {f.code for f in findings if f.severity == "SERIOUS"}
    invalid_codes = {"CHECKPOINT_MISSING", "CHECKPOINT_MALFORMED", "LEDGER_MISSING"}
    ledger_unreadable = ledger_view is not None and not ledger_view.readable
    if serious & invalid_codes or ledger_unreadable:
        run_state: RunState = "INVALID"
    elif serious:
        run_state = "CONTRADICTORY"
    elif cp is not None and cp.cursor == total:
        run_state = "COMPLETE"
    elif rp is not None and report_current and rp.status in PAUSE_STATUSES:
        run_state = "PAUSED"
    else:
        run_state = "INCOMPLETE"

    # --- observation (wall clock) ----------------------------------------------------------------
    cp_age = _age_seconds(evidence.checkpoint.written_at, now)
    progress_freshness = freshness(cp_age, PROGRESS_STALE_AFTER_SECONDS)
    observation = ObservationView(
        collected_at=evidence.collected_at,
        as_of=now,
        clock_label=clock_label,
        checkpoint_written_at=evidence.checkpoint.written_at,
        checkpoint_age_seconds=cp_age,
        progress_freshness=progress_freshness,
        ledger_written_at=evidence.ledger.written_at,
        ledger_age_seconds=_age_seconds(evidence.ledger.written_at, now),
        report_written_at=evidence.report.written_at if evidence.report else None,
        report_age_seconds=_age_seconds(evidence.report.written_at if evidence.report else None, now),
    )
    if evidence.checkpoint.present and progress_freshness == "UNKNOWN":
        finding("WARNING", "CHECKPOINT_WRITE_TIME_UNKNOWN", CHECKPOINT_FILE,
                "No write time is available for the checkpoint, so the age of the observation is "
                "UNKNOWN; nothing about recency can be concluded.")
    elif progress_freshness == "FUTURE":
        finding("WARNING", "CHECKPOINT_WRITE_IN_FUTURE", CHECKPOINT_FILE,
                f"The checkpoint's write time is {-cp_age:.0f} s after the as-of clock (beyond the "
                f"{FUTURE_TOLERANCE_SECONDS} s tolerance): clock skew or altered file metadata. Its age "
                "is not trusted.")
    elif progress_freshness == "STALE" and run_state in ("INCOMPLETE", "PAUSED"):
        # Expected for an explicit pause (INFO); unexplained for an incomplete run (WARNING).
        finding("WARNING" if run_state == "INCOMPLETE" else "INFO", "NO_RECENT_PROGRESS",
                CHECKPOINT_FILE,
                f"No checkpoint write for {cp_age:.0f} s (display threshold "
                f"{PROGRESS_STALE_AFTER_SECONDS} s) while the run is {run_state}. Interrupted, stalled "
                "or deliberately paused cannot be distinguished without a heartbeat.")
    elif progress_freshness == "FRESH" and run_state == "INCOMPLETE":
        finding("INFO", "RECENT_WRITE_OBSERVED", CHECKPOINT_FILE,
                f"The checkpoint was written {cp_age:.0f} s before the as-of clock. This shows recent "
                "progress only; whether a process is still running is UNKNOWN.")

    # --- progress --------------------------------------------------------------------------------
    progress = ProgressView(deliveries_total=total)
    if cp is not None and feed_identity_ok and cursor_ok:
        next_key = feed.deliveries[cp.cursor].post_key if cp.cursor < total else None
        progress = ProgressView(
            deliveries_total=total,
            cursor=cp.cursor,
            remaining=total - cp.cursor,
            fraction_complete=(cp.cursor / total) if total else None,
            next_delivery_post_key=next_key,
            virtual_now=cp.virtual_now,
            next_virtual_delivery=(
                feed.virtual_time(feed.deliveries[cp.cursor].offset_s) if next_key else None
            ),
        )
    if rp is not None:
        progress = progress.model_copy(update={
            "report_status": rp.status,
            "report_cursor": rp.cursor,
            "report_resumed_from": rp.resumed_from,
            "report_this_run_processed": rp.this_run.processed,
            "report_this_run_duplicates": rp.this_run.duplicate_returns + rp.this_run.hash_mismatch_blocks,
        })

    # --- resumability ----------------------------------------------------------------------------
    results = {c.result for c in checks}
    if "FAIL" in results:
        resumability: Resumability = "NOT_RESUMABLE"
    elif "SKIPPED" in results:
        resumability = "UNKNOWN"
    elif cp is not None and cp.cursor == total:
        resumability = "NOTHING_TO_RESUME"
    else:
        resumability = "RESUMABLE_BY_CONTRACT"

    # --- state-specific findings and headline ----------------------------------------------------
    if run_state == "COMPLETE":
        finding("INFO", "COMPLETION_OBSERVED", CHECKPOINT_FILE,
                f"The checkpoint cursor {cp.cursor}/{total} is at the end of the finite synthetic feed. "
                "This is a mock-ledger fact: not account flatness, verified protection or paper "
                "acceptance.")
        headline = (
            f"Completion observed: cursor {cp.cursor}/{total}; the finite synthetic feed was fully "
            "delivered to the mock ledger. Not account flatness, verified protection or paper acceptance."
        )
    elif run_state == "PAUSED":
        finding("INFO", "PAUSE_OBSERVED", REPORT_FILE,
                f"The report records {rp.status} at cursor {rp.cursor}/{total}, matching the checkpoint; "
                f"{total - cp.cursor} deliveries remain.")
        headline = (
            f"Pause observed: {rp.status} at cursor {cp.cursor}/{total} ({total - cp.cursor} remaining). "
            f"Resumability: {resumability}. Whether any process is running is UNKNOWN."
        )
    elif run_state == "INCOMPLETE":
        why = ("an earlier invocation's report does not cover it" if rp is not None
               else "no report explains why")
        headline = (
            f"Incomplete: cursor {cp.cursor}/{total} and {why}. Interrupted, crashed or still in progress "
            f"cannot be distinguished (no heartbeat). Resumability: {resumability}."
            + (" The ledger is one delivery ahead of the checkpoint." if ledger_ahead_by_one else "")
        )
    elif run_state == "CONTRADICTORY":
        headline = (
            f"Contradictory evidence ({', '.join(sorted(serious))}): the artifacts disagree with each "
            f"other or with the feed in use. No run state can be trusted. Resumability by the runner's "
            f"own checks: {resumability}; this view cannot resume anything."
        )
    else:
        codes = sorted(serious & invalid_codes) or ["LEDGER_UNREADABLE"]
        headline = (
            f"Invalid or missing evidence ({', '.join(codes)}): no run state can be read. "
            f"Resumability: {resumability}."
        )

    findings.sort(key=lambda f: SEVERITY_ORDER[f.severity])
    # Anything that is not an observed completion or pause, any non-informational finding, and any
    # ledger discrepancy or incomplete reconciliation needs a human look. INFO never raises attention.
    ledger_attention = ledger_view is not None and (
        bool(ledger_view.discrepancies) or not ledger_view.reconciliation_complete
    )
    attention = (
        run_state not in ("COMPLETE", "PAUSED")
        or any(f.severity != "INFO" for f in findings)
        or ledger_attention
    )
    files = [evidence.checkpoint, evidence.ledger] + ([evidence.report] if evidence.report else [])
    return ShadowRunHealth(
        evidence_label=evidence.label,
        workdir=evidence.workdir,
        files=files,
        observation=observation,
        run_state=run_state,
        headline=headline,
        attention_required=attention,
        identity=identity_rows,
        progress=progress,
        resumability=resumability,
        resumability_checks=checks,
        findings=findings,
        ledger=ledger_view,
        observation_limits=list(OBSERVATION_LIMITS),
    )


def observe_shadow_run(
    artifacts: ShadowRunArtifacts,
    *,
    feed: ShadowFeed | None = None,
    policy: MockPolicy | None = None,
    now: datetime | None = None,
) -> ShadowRunHealth:
    """Collect and assess in one step, judged on the real wall clock unless ``now`` is given."""
    return assess_shadow_run(collect_evidence(artifacts), feed=feed, policy=policy, now=now)


# ----------------------------------------------------------------------------- synthetic scenarios


def new_scenario_directory() -> Path:
    """A fresh task-local temporary directory; never the repository's data/ directory."""
    return Path(tempfile.mkdtemp(prefix="tradingbot-shadow-health-"))


def _edit_checkpoint(path: Path, mutate: Callable[[dict], None]) -> None:
    """Fixture-only fault injection on a freshly built synthetic checkpoint."""
    data = json.loads(path.read_text("utf-8"))
    mutate(data)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def build_scenario(
    directory: str | Path,
    scenario: Scenario,
    *,
    feed: ShadowFeed | None = None,
    policy: MockPolicy | None = None,
) -> ShadowRunArtifacts:
    """Drive the EXISTING shadow runner over the synthetic feed into ``directory`` and, for the fault
    scenarios, damage the freshly written synthetic artifacts the way the named fault would.

    Everything is newly authored and synthetic; the runner refuses a non-empty working directory,
    so this can never touch existing data. The caller owns ``directory`` (use a fresh temp dir).
    """
    if scenario not in SCENARIO_DESCRIPTIONS:
        raise ValueError(f"unknown scenario {scenario!r}")
    feed = feed or synthetic_feed()
    policy = policy or shadow_policy()
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    workdir = directory / "run"
    report_path = directory / REPORT_FILE
    total = len(feed.deliveries)

    def fresh(**kwargs) -> ShadowRunReport:
        return ShadowRunner(workdir, feed, policy).run(**kwargs)

    def write_report(report: ShadowRunReport) -> None:
        report_path.write_text(report.model_dump_json(indent=2), encoding="utf-8")

    def interrupt(phase: str) -> None:
        try:
            fresh(interrupt=InterruptSpec(6, phase))  # type: ignore[arg-type]
        except ShadowInterrupted:
            pass

    has_report = True
    if scenario == "completed":
        write_report(fresh())
    elif scenario == "paused_by_delivery_cap":
        write_report(fresh(max_deliveries=5))
    elif scenario == "interrupted_after_commit":
        interrupt("after_commit")
        has_report = False
    elif scenario == "interrupted_before_commit":
        interrupt("before_commit")
        has_report = False
    elif scenario == "stale_report":
        write_report(fresh(max_deliveries=5))
        ShadowRunner(workdir, feed, policy, resume=True).run()
    elif scenario == "malformed_report":
        write_report(fresh())
        report_path.write_text("not json SYNTHETIC-REPORT-MARKER", encoding="utf-8")
    elif scenario == "missing_checkpoint":
        write_report(fresh())
        (workdir / CHECKPOINT_FILE).unlink()
    elif scenario == "malformed_checkpoint":
        write_report(fresh(max_deliveries=5))
        (workdir / CHECKPOINT_FILE).write_text("{not json SYNTHETIC-CHECKPOINT-MARKER", encoding="utf-8")
    elif scenario == "missing_ledger":
        write_report(fresh(max_deliveries=5))
        (workdir / LEDGER_FILE).unlink()
    elif scenario == "foreign_feed_identity":
        write_report(fresh(max_deliveries=5))
        _edit_checkpoint(workdir / CHECKPOINT_FILE, lambda d: d.__setitem__("feed_digest", "0" * 64))
    elif scenario == "cursor_beyond_feed":
        write_report(fresh())
        _edit_checkpoint(workdir / CHECKPOINT_FILE, lambda d: d.__setitem__("cursor", total + 1))
    elif scenario == "checkpoint_ahead_of_ledger":
        write_report(fresh(max_deliveries=5))
        # An empty ledger with the engine's schema, as the runner itself creates it at cursor 0.
        empty = directory / "empty"
        ShadowRunner(empty, feed, policy)
        (empty / LEDGER_FILE).replace(workdir / LEDGER_FILE)
    elif scenario == "contradictory_report":
        report = fresh(max_deliveries=5)
        write_report(report.model_copy(update={"cursor": total, "status": "COMPLETE"}))
    return ShadowRunArtifacts(
        label=f"synthetic:{scenario}", workdir=workdir, report_path=report_path if has_report else None
    )
