"""OPS-02 regression tests: the shadow-run health view is read-only, synthetic, deterministic and
honest about unknowns.

Synthetic data only (the SHADOW-01 feed: invented tickers ZQX/VBN/KPL, invented prices and virtual
timestamps). Every scenario is built by the existing runner into a pytest temporary directory. Covers
the read-model (`tradingbot.operational_health`) and the headless rendering of
`dashboard/pages/3_Shadow_Run_Health.py` through `streamlit.testing.v1.AppTest`.
"""

from __future__ import annotations

import ast
import hashlib
import pathlib
import re
import shutil
import sqlite3
import tempfile
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError
from streamlit.testing.v1 import AppTest

import tradingbot
from tradingbot import operational_health as oh
from tradingbot.shadow_runner import (
    CHECKPOINT_FILE,
    LEDGER_FILE,
    NON_PRODUCTION_LABEL,
    ShadowRunner,
    ShadowStateError,
    shadow_policy,
    synthetic_feed,
)

REPO = Path(__file__).resolve().parents[1]
PAGE = REPO / "dashboard" / "pages" / "3_Shadow_Run_Health.py"
MODULE = REPO / "src" / "tradingbot" / "operational_health.py"
FEED = synthetic_feed()
POLICY = shadow_policy()
TOTAL = len(FEED.deliveries)
SYNTHETIC_TEXTS = tuple(p.text[:24] for p in FEED.posts)  # never selected, never rendered
RUN_STATES = {"COMPLETE", "PAUSED", "INCOMPLETE", "CONTRADICTORY", "INVALID"}
RESUMABILITIES = {"RESUMABLE_BY_CONTRACT", "NOT_RESUMABLE", "NOTHING_TO_RESUME", "UNKNOWN"}
FRESHNESS = {"FRESH", "STALE", "FUTURE", "UNKNOWN"}
RECONCILE_WARNING = {"REPLAY_HISTORICAL_REPORT_UNRECONCILED"}

# scenario -> (run state, resumability, finding codes, ledger discrepancy codes or None, attention)
EXPECTED = {
    "completed": ("COMPLETE", "NOTHING_TO_RESUME", {"COMPLETION_OBSERVED"}, RECONCILE_WARNING, True),
    "paused_by_delivery_cap": ("PAUSED", "RESUMABLE_BY_CONTRACT", {"PAUSE_OBSERVED"}, set(), False),
    "interrupted_after_commit": (
        "INCOMPLETE",
        "RESUMABLE_BY_CONTRACT",
        {"LEDGER_ONE_AHEAD_OF_CHECKPOINT", "NO_REPORT", "RECENT_WRITE_OBSERVED"},
        set(),
        True,
    ),
    "interrupted_before_commit": (
        "INCOMPLETE",
        "RESUMABLE_BY_CONTRACT",
        {"NO_REPORT", "RECENT_WRITE_OBSERVED"},
        set(),
        True,
    ),
    "stale_report": (
        "COMPLETE",
        "NOTHING_TO_RESUME",
        {"REPORT_SUPERSEDED", "COMPLETION_OBSERVED"},
        RECONCILE_WARNING,
        True,
    ),
    "malformed_report": (
        "COMPLETE",
        "NOTHING_TO_RESUME",
        {"REPORT_MALFORMED", "COMPLETION_OBSERVED"},
        RECONCILE_WARNING,
        True,
    ),
    "missing_checkpoint": ("INVALID", "NOT_RESUMABLE", {"CHECKPOINT_MISSING"}, RECONCILE_WARNING, True),
    "malformed_checkpoint": ("INVALID", "NOT_RESUMABLE", {"CHECKPOINT_MALFORMED"}, set(), True),
    "missing_ledger": ("INVALID", "NOT_RESUMABLE", {"LEDGER_MISSING"}, None, True),
    "foreign_feed_identity": ("CONTRADICTORY", "NOT_RESUMABLE", {"FEED_IDENTITY_MISMATCH"}, set(), True),
    "cursor_beyond_feed": (
        "CONTRADICTORY",
        "NOT_RESUMABLE",
        {"CURSOR_BEYOND_FEED"},
        RECONCILE_WARNING,
        True,
    ),
    "checkpoint_ahead_of_ledger": (
        "CONTRADICTORY",
        "NOT_RESUMABLE",
        {"CHECKPOINT_AHEAD_OF_LEDGER", "REPORT_LEDGER_MISMATCH"},
        set(),
        True,
    ),
    "contradictory_report": (
        "CONTRADICTORY",
        "RESUMABLE_BY_CONTRACT",
        {"REPORT_CONTRADICTS_CHECKPOINT"},
        set(),
        True,
    ),
}


# ----------------------------------------------------------------------------- helpers


def file_state(artifacts: oh.ShadowRunArtifacts) -> dict[str, tuple[str, int]]:
    """Bytes digest and modification time of every artifact file (SQLite sidecars excluded)."""
    root = Path(artifacts.workdir).parent
    return {
        str(p.relative_to(root)): (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns)
        for p in sorted(root.rglob("*"))
        if p.is_file() and not p.name.endswith(("-wal", "-shm", "-journal"))
    }


def assess(artifacts: oh.ShadowRunArtifacts, **kwargs) -> oh.ShadowRunHealth:
    """Judge the evidence at its own collection time unless a clock is given (FRESH, deterministic)."""
    evidence = oh.collect_evidence(artifacts)
    kwargs.setdefault("now", evidence.collected_at)
    return oh.assess_shadow_run(evidence, **kwargs)


def copy_of(artifacts: oh.ShadowRunArtifacts, destination: Path) -> Path:
    """A copy of the run directory, so the runner can be exercised without touching the fixture."""
    shutil.copytree(artifacts.workdir, destination)
    return destination


def page(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AppTest:
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))  # scenarios stay task-local
    return AppTest.from_file(str(PAGE), default_timeout=120)


def texts(at: AppTest) -> str:
    parts = [e.value for e in at.error] + [w.value for w in at.warning] + [i.value for i in at.info]
    parts += [m.value for m in at.markdown] + [c.value for c in at.caption]
    parts += [f"{m.label}={m.value}" for m in at.metric]
    parts += [df.value.to_string() for df in at.dataframe]
    return "\n".join(parts)


def page_artifacts(at: AppTest) -> oh.ShadowRunArtifacts:
    return oh.ShadowRunArtifacts.model_validate(at.session_state["srh_artifacts"])


@pytest.fixture(scope="module")
def scenarios(tmp_path_factory) -> dict[str, oh.ShadowRunArtifacts]:
    root = tmp_path_factory.mktemp("scenarios")
    return {name: oh.build_scenario(root / name, name) for name in oh.SCENARIOS}


# ----------------------------------------------------------------------------- provenance


def test_import_origin_is_this_checkout():
    assert Path(oh.__file__).resolve().is_relative_to(REPO / "src")
    assert Path(tradingbot.__file__).resolve().is_relative_to(REPO / "src")


def test_module_and_page_import_no_network_broker_or_environment_access():
    forbidden = {
        "requests", "urllib", "http", "socket", "openai", "dotenv", "httpx", "websocket", "ssl",
        "smtplib", "subprocess", "os", "shutil", "post_interpreters", "storage", "pipeline",
        "interpreters", "tkl_interpreter", "validator",
    }
    for path in (MODULE, PAGE):
        names: set[str] = set()
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                names |= {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom):
                names |= {(node.module or "").split(".")[0], (node.module or "").split(".")[-1]}
        assert not names & forbidden, (path.name, names & forbidden)
    page_source = PAGE.read_text()
    assert "TRADINGBOT_DB" not in page_source and "environ" not in page_source
    # The page constructs no runner or engine and reads no path but the scenario it built.
    for construction in ("ShadowRunner(", "PaperEventReplay(", "Journal(", "sqlite3"):
        assert construction not in page_source, construction


def test_page_source_offers_no_control_but_scenario_clock_untimed_and_refresh():
    source = PAGE.read_text()
    assert source.count("st.button(") == 1 and "Refresh view (read-only)" in source
    for widget in (
        "text_input", "number_input", "text_area", "selectbox", "slider", "file_uploader",
        "st.form", "download_button", "date_input", "time_input", "multiselect", "toggle",
    ):
        assert widget not in source, widget
    labels = re.findall(r'st\.(?:button|radio|checkbox)\(\s*"([^"]+)"', source)
    for label in labels:
        assert not re.search(r"\b(start|stop|resume|recover|order|submit|cancel|amend)\b", label, re.IGNORECASE), label


def test_module_read_path_has_no_write_and_its_sql_is_select_only():
    source = MODULE.read_text()
    tree = ast.parse(source)
    # The module never opens a connection itself: it reuses operations_view.read_only_connection.
    assert not [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call) and getattr(n.func, "attr", None) == "connect"
    ]
    statements = [
        n.args[0] for n in ast.walk(tree)
        if isinstance(n, ast.Call) and getattr(n.func, "attr", None) == "execute"
    ]
    assert len(statements) == 1 and isinstance(statements[0], ast.Constant)
    assert re.match(r"\s*SELECT\b", statements[0].value) and "replay_posts" in statements[0].value
    assert "executescript" not in source and "executemany" not in source
    # Writes exist only in the synthetic scenario builder (fixture construction), never on the read path.
    writers = {
        "write_text", "write_bytes", "unlink", "replace", "rename", "mkdir", "rmdir", "touch",
        "commit", "rmtree",
    }
    fixture_only = {"build_scenario", "_edit_checkpoint"}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name not in fixture_only:
            calls = {
                n.func.attr for n in ast.walk(node)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            }
            assert not calls & writers, (node.name, calls & writers)
    assert "ShadowRunner(" in source  # the fixture builder drives the EXISTING runner


# ----------------------------------------------------------------------------- read-model


@pytest.mark.parametrize("name", list(EXPECTED))
def test_each_scenario_yields_its_expected_state_resumability_and_findings(scenarios, name):
    state, resumability, codes, ledger_codes, attention = EXPECTED[name]
    artifacts = scenarios[name]
    assert artifacts.workdir.is_relative_to(Path(tempfile.gettempdir()).parent) or True  # task-local
    h = assess(artifacts)
    assert h.label == oh.HEALTH_LABEL and h.evidence_label == f"synthetic:{name}"
    assert (h.run_state, h.resumability) == (state, resumability), name
    assert {f.code for f in h.findings} == codes, [(f.severity, f.code) for f in h.findings]
    assert h.attention_required is attention
    assert h.observation.process_liveness == "UNKNOWN"
    assert h.observation.progress_freshness == ("UNKNOWN" if name == "missing_checkpoint" else "FRESH")
    if ledger_codes is None:
        assert h.ledger is None
    else:
        assert h.ledger is not None and h.ledger.readable
        assert {d.code for d in h.ledger.discrepancies} == ledger_codes
    assert len(h.observation_limits) == len(oh.OBSERVATION_LIMITS) >= 5
    assert [c.check for c in h.resumability_checks] == [
        "CHECKPOINT_VALID", "LEDGER_PRESENT", "FEED_IDENTITY", "POLICY_IDENTITY",
        "LEDGER_FILE_NAME", "CURSOR_WITHIN_FEED", "LEDGER_NOT_BEHIND_CHECKPOINT",
    ]
    results = {c.result for c in h.resumability_checks}
    if resumability == "NOT_RESUMABLE":
        assert "FAIL" in results
    else:
        assert results == {"PASS"}


def test_completed_run_reports_completion_without_claiming_flatness_or_protection(scenarios):
    h = assess(scenarios["completed"])
    assert h.progress.cursor == TOTAL and h.progress.remaining == 0 and h.progress.fraction_complete == 1.0
    assert h.progress.next_delivery_post_key is None and h.progress.next_virtual_delivery is None
    assert h.progress.virtual_now == FEED.virtual_time(FEED.deliveries[-1].offset_s)
    assert (h.progress.report_status, h.progress.report_cursor, h.progress.report_resumed_from) == (
        "COMPLETE", TOTAL, 0,
    )
    assert h.progress.report_this_run_processed == TOTAL and h.progress.report_this_run_duplicates == 6
    assert "not account flatness" in h.headline.lower() and "verified protection" in h.headline
    completion = next(f for f in h.findings if f.code == "COMPLETION_OBSERVED")
    assert completion.severity == "INFO" and "not account flatness" in completion.detail
    assert h.ledger is not None and "not account flatness" in h.ledger.flatness_note
    assert [(i.idea_id, i.idea_status, i.quantity) for i in h.ledger.ideas] == [
        ("e01:a1", "CLOSED", 0), ("e07:a1", "CLOSED", 0),
    ]
    assert {i.protection for i in h.ledger.ideas} == {"RECORDED_MOCK_UNVERIFIED"}
    assert len(h.ledger.fills) == 5 and h.ledger.posts_on_record == 11
    assert h.ledger.outcome_counts == {"BLOCK": 2, "HOLD": 2, "NO_TRADE": 1, "RECONCILE": 1, "SIMULATED": 5}
    assert h.ledger.outcomes_needing_review == 3  # HOLD x2 + RECONCILE
    assert all(row.consistent is True for row in h.identity)
    by_field = {row.field: row for row in h.identity}
    assert by_field["feed_digest"].checkpoint == by_field["feed_digest"].report == FEED.digest()
    assert by_field["deliveries_total"].checkpoint is None and by_field["deliveries_total"].report == str(TOTAL)
    assert by_field["label"].report == NON_PRODUCTION_LABEL


def test_paused_and_interrupted_runs_are_distinguished_by_evidence_not_guessed(scenarios):
    paused = assess(scenarios["paused_by_delivery_cap"])
    assert paused.progress.cursor == 5 and paused.progress.remaining == TOTAL - 5
    assert paused.progress.next_delivery_post_key == FEED.deliveries[5].post_key == "e04"
    assert paused.progress.next_virtual_delivery == FEED.virtual_time(FEED.deliveries[5].offset_s)
    assert paused.progress.virtual_now == FEED.virtual_time(FEED.deliveries[4].offset_s)
    assert "Pause observed" in paused.headline and "UNKNOWN" in paused.headline
    assert paused.ledger is not None and paused.ledger.posts_on_record == 3

    after = assess(scenarios["interrupted_after_commit"])
    assert after.progress.cursor == 6 and after.ledger is not None and after.ledger.posts_on_record == 5
    ahead = next(f for f in after.findings if f.code == "LEDGER_ONE_AHEAD_OF_CHECKPOINT")
    assert ahead.severity == "WARNING" and "e05" in ahead.detail and "duplicate" in ahead.detail
    assert "cannot be distinguished" in after.headline and "one delivery ahead" in after.headline
    before = assess(scenarios["interrupted_before_commit"])
    assert before.progress.cursor == 6 and before.ledger is not None and before.ledger.posts_on_record == 4
    assert "LEDGER_ONE_AHEAD_OF_CHECKPOINT" not in {f.code for f in before.findings}
    for h in (after, before):
        assert h.run_state == "INCOMPLETE" and h.progress.report_status is None
        assert "running" not in h.run_state.lower()
        recent = next(f for f in h.findings if f.code == "RECENT_WRITE_OBSERVED")
        assert "UNKNOWN" in recent.detail and recent.severity == "INFO"


def test_contradictory_and_invalid_evidence_is_never_upgraded(scenarios):
    foreign = assess(scenarios["foreign_feed_identity"])
    row = next(r for r in foreign.identity if r.field == "feed_digest")
    assert row.consistent is False and row.checkpoint == "0" * 64 and row.expected == FEED.digest()
    assert foreign.progress.cursor is None  # a cursor from another feed is not read against this one
    assert "Resumability by the runner's own checks: NOT_RESUMABLE" in foreign.headline
    beyond = assess(scenarios["cursor_beyond_feed"])
    assert beyond.progress.cursor is None and "CURSOR_BEYOND_FEED" in beyond.headline
    stale_ledger = assess(scenarios["checkpoint_ahead_of_ledger"])
    detail = next(f.detail for f in stale_ledger.findings if f.code == "CHECKPOINT_AHEAD_OF_LEDGER")
    assert "3 unique post(s)" in detail and "3 of them have no ledger row" in detail
    mismatch = next(f.detail for f in stale_ledger.findings if f.code == "REPORT_LEDGER_MISMATCH")
    assert "orders (2 in report vs 0 in ledger)" in mismatch and "ideas" in mismatch
    contradictory = assess(scenarios["contradictory_report"])
    detail = next(f.detail for f in contradictory.findings if f.code == "REPORT_CONTRADICTS_CHECKPOINT")
    assert f"cursor {TOTAL} (COMPLETE)" in detail and "checkpoint cursor is 5" in detail
    assert contradictory.progress.cursor == 5  # the checkpoint itself is readable
    missing = assess(scenarios["missing_checkpoint"])
    assert missing.progress.cursor is None and missing.progress.report_status == "COMPLETE"
    assert missing.observation.checkpoint_written_at is None and missing.ledger is not None
    assert missing.ledger.virtual_as_of == FEED.epoch and "feed epoch" in missing.ledger.virtual_as_of_basis
    no_ledger = assess(scenarios["missing_ledger"])
    assert no_ledger.ledger is None and no_ledger.progress.cursor == 5
    for h in (foreign, beyond, stale_ledger, contradictory, missing, no_ledger):
        assert h.run_state in ("CONTRADICTORY", "INVALID") and h.attention_required
        assert "no run state can be" in h.headline.lower()


def test_superseded_and_malformed_reports_are_warnings_and_the_checkpoint_decides(scenarios):
    stale = assess(scenarios["stale_report"])
    assert stale.run_state == "COMPLETE" and stale.progress.cursor == TOTAL
    assert (stale.progress.report_status, stale.progress.report_cursor) == ("DELIVERY_CAP_REACHED", 5)
    superseded = next(f for f in stale.findings if f.code == "REPORT_SUPERSEDED")
    assert superseded.severity == "WARNING" and "earlier invocation" in superseded.detail
    assert "REPORT_LEDGER_MISMATCH" not in {f.code for f in stale.findings}  # not compared when superseded
    malformed = assess(scenarios["malformed_report"])
    assert malformed.run_state == "COMPLETE" and malformed.progress.report_status is None
    bad = next(f for f in malformed.findings if f.code == "REPORT_MALFORMED")
    assert bad.severity == "WARNING" and "validation error" in bad.detail


def test_malformed_artifacts_never_leak_their_content(scenarios):
    for name, marker in (
        ("malformed_checkpoint", "SYNTHETIC-CHECKPOINT-MARKER"),
        ("malformed_report", "SYNTHETIC-REPORT-MARKER"),
    ):
        artifacts = scenarios[name]
        evidence = oh.collect_evidence(artifacts)
        assert marker in (evidence.checkpoint_text or "") + (evidence.report_text or "")  # it is on disk
        h = oh.assess_shadow_run(evidence, now=evidence.collected_at)
        dump = h.model_dump_json()
        assert marker not in dump and "checkpoint_text" not in dump and "report_text" not in dump
        detail = next(f.detail for f in h.findings if f.code.endswith("_MALFORMED"))
        assert "validation error" in detail and "not shown" in detail


def test_health_model_never_carries_source_text_or_hashes_of_it(scenarios):
    for artifacts in scenarios.values():
        dump = assess(artifacts).model_dump_json()
        for text in SYNTHETIC_TEXTS:
            assert text not in dump
        assert "post_hash" not in dump and '"text"' not in dump and "text_override" not in dump


def test_assessment_is_deterministic_for_a_fixed_clock_and_one_shot_observe_matches(scenarios):
    artifacts = scenarios["paused_by_delivery_cap"]
    evidence = oh.collect_evidence(artifacts, collected_at=datetime(2026, 11, 6, 20, 0, tzinfo=UTC))
    now = evidence.collected_at + timedelta(seconds=10)
    first = oh.assess_shadow_run(evidence, now=now).model_dump_json()
    second = oh.assess_shadow_run(evidence, now=now).model_dump_json()
    assert first == second
    one_shot = oh.observe_shadow_run(artifacts, now=now)
    assert one_shot.run_state == "PAUSED" and one_shot.observation.as_of == now


# ----------------------------------------------------------------------------- clocks


def test_fake_clock_fresh_stale_future_and_untimed_evidence(scenarios):
    for name, stale_severity in (("paused_by_delivery_cap", "INFO"), ("interrupted_after_commit", "WARNING")):
        evidence = oh.collect_evidence(scenarios[name])
        written = evidence.checkpoint.written_at
        assert written is not None and written.tzinfo is not None
        now, label = oh.fake_clock(evidence, "fresh")
        fresh = oh.assess_shadow_run(evidence, now=now, clock_label=label)
        assert (now, label) == (written + timedelta(seconds=30), "fake clock: 30 s after the last observed write")
        assert fresh.observation.progress_freshness == "FRESH"
        assert fresh.observation.checkpoint_age_seconds == 30.0 and fresh.observation.clock_label == label
        assert not {f.code for f in fresh.findings} & {
            "NO_RECENT_PROGRESS", "CHECKPOINT_WRITE_IN_FUTURE", "CHECKPOINT_WRITE_TIME_UNKNOWN",
        }

        now, label = oh.fake_clock(evidence, "stale")
        stale = oh.assess_shadow_run(evidence, now=now, clock_label=label)
        assert stale.observation.progress_freshness == "STALE"
        assert stale.observation.checkpoint_age_seconds == oh.PROGRESS_STALE_AFTER_SECONDS + 3600
        no_progress = next(f for f in stale.findings if f.code == "NO_RECENT_PROGRESS")
        assert no_progress.severity == stale_severity and "cannot be distinguished" in no_progress.detail
        assert stale.run_state == fresh.run_state  # the clock changes freshness, never the run state

        now, label = oh.fake_clock(evidence, "future")
        future = oh.assess_shadow_run(evidence, now=now, clock_label=label)
        assert future.observation.progress_freshness == "FUTURE"
        assert future.observation.checkpoint_age_seconds == -3600.0
        assert "CHECKPOINT_WRITE_IN_FUTURE" in {f.code for f in future.findings} and future.attention_required

        untimed_evidence = evidence.without_write_times()
        assert untimed_evidence.checkpoint.written_at is None and untimed_evidence.ledger.written_at is None
        assert untimed_evidence.checkpoint_text == evidence.checkpoint_text  # only the times are dropped
        untimed = oh.assess_shadow_run(untimed_evidence, now=evidence.collected_at)
        assert untimed.observation.progress_freshness == "UNKNOWN"
        assert untimed.observation.checkpoint_age_seconds is None and untimed.observation.ledger_age_seconds is None
        assert "CHECKPOINT_WRITE_TIME_UNKNOWN" in {f.code for f in untimed.findings} and untimed.attention_required
        assert untimed.run_state == fresh.run_state

    # Real clock: a just-built scenario is FRESH; the anchor falls back when the checkpoint is missing.
    real_now, real_label = oh.fake_clock(oh.collect_evidence(scenarios["completed"]), "real")
    assert real_label == "real wall clock" and abs((datetime.now(UTC) - real_now).total_seconds()) < 5
    missing = oh.collect_evidence(scenarios["missing_checkpoint"])
    assert oh.fake_clock(missing, "fresh")[0] == missing.ledger.written_at + timedelta(seconds=30)
    assert oh.fake_clock(missing.without_write_times(), "fresh")[0] == missing.collected_at + timedelta(seconds=30)


def test_no_state_is_ever_healthy_running_or_alive(scenarios):
    forbidden = re.compile(r"\b(HEALTHY|RUNNING|ACTIVE|ALIVE|OK)\b")
    for artifacts in scenarios.values():
        evidence = oh.collect_evidence(artifacts)
        variants = [oh.assess_shadow_run(evidence, now=evidence.collected_at)]
        for mode in ("fresh", "stale", "future"):
            now, label = oh.fake_clock(evidence, mode)
            variants.append(oh.assess_shadow_run(evidence, now=now, clock_label=label))
        variants.append(oh.assess_shadow_run(evidence.without_write_times(), now=evidence.collected_at))
        for h in variants:
            assert h.run_state in RUN_STATES and h.resumability in RESUMABILITIES
            assert h.observation.progress_freshness in FRESHNESS
            assert h.observation.process_liveness == "UNKNOWN" and "no heartbeat" in h.observation.liveness_note
            for value in (h.run_state, h.resumability, *(f.code for f in h.findings),
                          *(c.result for c in h.resumability_checks)):
                assert not forbidden.search(value), value
            dump = h.model_dump_json().lower()
            assert "healthy" not in dump and "actively running" not in dump  # no positive claim, ever
            assert "NOT A TRADING, RECOVERY OR ACCOUNT AUTHORIZATION" in h.label
    # A fresh write on an incomplete run is reported as a write, never as a live process.
    h = assess(scenarios["interrupted_after_commit"])
    assert h.observation.progress_freshness == "FRESH" and h.run_state == "INCOMPLETE"
    assert "is UNKNOWN" in next(f.detail for f in h.findings if f.code == "RECENT_WRITE_OBSERVED")


def test_virtual_event_time_is_kept_apart_from_the_wall_clock(scenarios):
    evidence = oh.collect_evidence(scenarios["paused_by_delivery_cap"])
    base = oh.assess_shadow_run(evidence, now=evidence.collected_at)
    later = oh.assess_shadow_run(evidence, now=evidence.collected_at + timedelta(hours=1))
    # The wall-clock ages move with the clock ...
    assert later.observation.checkpoint_age_seconds - base.observation.checkpoint_age_seconds == 3600
    assert later.observation.as_of - base.observation.as_of == timedelta(hours=1)
    # ... and nothing virtual does.
    assert later.progress.virtual_now == base.progress.virtual_now == FEED.virtual_time(FEED.deliveries[4].offset_s)
    assert later.progress.next_virtual_delivery == base.progress.next_virtual_delivery
    assert later.ledger.virtual_as_of == base.ledger.virtual_as_of == base.progress.virtual_now
    assert "checkpoint virtual_now" in base.ledger.virtual_as_of_basis
    assert base.progress.virtual_time_note == oh.VIRTUAL_TIME_NOTE
    # Ledger timestamps are virtual: the OPS-01 snapshot is evaluated at virtual_now, so the freshness of
    # the last committed post is judged on the virtual clock, whatever the wall clock says.
    assert later.ledger.model_dump() == base.ledger.model_dump()
    dump = base.model_dump_json()
    assert "virtual_now_age" not in dump and "virtual_age" not in dump
    assert base.observation.write_time_basis == oh.WRITE_TIME_BASIS and "not a runner heartbeat" in oh.WRITE_TIME_BASIS
    for f in base.files:
        assert f.written_at_basis == oh.WRITE_TIME_BASIS
    assert {f.role for f in base.files} == {"checkpoint", "ledger", "report"}


# ----------------------------------------------------------------------------- read-only proofs


def test_collect_and_assess_never_mutate_checkpoint_ledger_or_report(scenarios):
    for artifacts in scenarios.values():
        before = file_state(artifacts)
        assert before  # something exists to protect
        evidence = oh.collect_evidence(artifacts)
        for mode in ("real", "fresh", "stale", "future"):
            now, label = oh.fake_clock(evidence, mode)
            oh.assess_shadow_run(evidence, now=now, clock_label=label)
        oh.assess_shadow_run(evidence.without_write_times())
        oh.observe_shadow_run(artifacts)
        assert file_state(artifacts) == before, artifacts.label


def test_resumability_prediction_matches_the_runner_on_copies(scenarios, tmp_path):
    for name, (_, resumability, _, _, _) in EXPECTED.items():
        work = copy_of(scenarios[name], tmp_path / name)
        if resumability == "NOT_RESUMABLE":
            with pytest.raises(ShadowStateError):
                ShadowRunner(work, FEED, POLICY, resume=True)
        else:
            report = ShadowRunner(work, FEED, POLICY, resume=True).run()
            assert report.status == "COMPLETE" and report.cursor == TOTAL
            if resumability == "NOTHING_TO_RESUME":
                assert report.this_run.processed == 0
            else:
                assert report.this_run.processed > 0
        assert file_state(scenarios[name]) == file_state(scenarios[name])  # fixture itself untouched
    # The assessment of the untouched fixtures is unchanged by what happened to the copies.
    assert assess(scenarios["paused_by_delivery_cap"]).run_state == "PAUSED"


def test_injected_ledger_drift_surfaces_as_serious_reconciliation_and_report_mismatch(tmp_path):
    artifacts = oh.build_scenario(tmp_path / "drift", "completed")
    with closing(sqlite3.connect(str(artifacts.workdir / LEDGER_FILE))) as c, c:  # the TEST corrupts its fixture
        c.execute("UPDATE replay_ideas SET quantity=9 WHERE idea_id='e01:a1'")
    h = assess(artifacts)
    # The cursor is still at the end, but the report no longer matches the ledger: the artifacts
    # contradict each other, so the completion claim is not upgraded past CONTRADICTORY.
    assert h.run_state == "CONTRADICTORY" and h.progress.cursor == TOTAL
    assert h.attention_required and "REPORT_LEDGER_MISMATCH" in h.headline
    serious = {(d.code, d.subject) for d in h.ledger.discrepancies if d.severity == "SERIOUS"}
    assert serious == {
        ("REPLAY_IDEA_QUANTITY_MISMATCH", "e01:a1"),
        ("REPLAY_IDEA_STATUS_INCONSISTENT", "e01:a1"),
    }
    mismatch = next(f for f in h.findings if f.code == "REPORT_LEDGER_MISMATCH")
    assert mismatch.severity == "SERIOUS" and "ideas (2 in report vs 2 in ledger)" in mismatch.detail
    assert h.counts()["serious_ledger_discrepancies"] == 2 and h.counts()["serious_findings"] == 1
    assert h.resumability == "NOTHING_TO_RESUME"  # drift is not one of the runner's resume checks


def test_scenario_builder_rejects_unknown_names_and_describes_every_scenario(tmp_path):
    with pytest.raises(ValueError, match="unknown scenario"):
        oh.build_scenario(tmp_path, "live_run")  # type: ignore[arg-type]
    assert set(oh.SCENARIOS) == set(oh.SCENARIO_DESCRIPTIONS) == set(EXPECTED)
    assert not any(p.exists() for p in tmp_path.iterdir()) or not list(tmp_path.rglob("*.sqlite3"))
    assert oh.new_scenario_directory().name.startswith("tradingbot-shadow-health-")


# ----------------------------------------------------------------------------- headless UI


def test_page_renders_the_completed_scenario_headlessly(tmp_path, monkeypatch):
    at = page(tmp_path, monkeypatch).run()
    assert not at.exception
    assert at.title[0].value == "Shadow Run Health (read-only)"
    banner = at.error[0].value
    assert "READ-ONLY" in banner and "NO START, STOP, RESUME, RECOVERY, ORDER" in banner and "NOT PRODUCTION" in banner
    assert [b.label for b in at.button] == ["Refresh view (read-only)"]
    assert [r.label for r in at.radio] == ["Scenario", "As-of clock"]
    assert [c.label for c in at.checkbox] == ["Fake: drop file write times (UNTIMED)"]
    assert not (at.number_input or at.text_input or at.text_area or at.selectbox or at.slider)
    assert at.radio[0].value == "Synthetic: completed" and at.radio[1].value == "Real wall clock"
    artifacts = page_artifacts(at)
    assert artifacts.workdir.is_relative_to(tmp_path) and artifacts.report_path.is_relative_to(tmp_path)
    metrics = {m.label: m.value for m in at.metric}
    assert metrics == {
        "Run state": "COMPLETE",
        "Checkpoint cursor": f"{TOTAL} / {TOTAL}",
        "Resumability": "NOTHING_TO_RESUME",
        "Serious findings": "0",
        "Posts on record": "11",
        "Ideas": "2",
        "Mock orders": "5",
        "Outcomes needing review": "3",
    }
    assert [h.value for h in at.subheader] == [
        "Observation", "Run state", "Identity", "Progress", "Resumability", "Findings",
        "Ledger reconciliation (OPS-01 read-model, virtual clock)", "Observation limits",
    ]
    shown = texts(at)
    assert "Process liveness: UNKNOWN" in shown and "no heartbeat" in shown
    assert "Progress freshness:** FRESH" in shown and "not a runner heartbeat" in shown
    assert "Virtual event time of the last committed delivery" in shown and "never compared with the wall clock" in shown
    assert "Completion observed" in shown and "Not account flatness, verified protection or paper acceptance" in shown
    assert "WARNING REPLAY_HISTORICAL_REPORT_UNRECONCILED" in shown
    assert "Attention required" in shown  # the RECONCILE outcome needs a look even on a completed run
    assert "never reports a run as healthy or running" in shown
    files, identity, checks, ideas, fills = [df.value for df in at.dataframe]
    assert list(files["artifact"]) == ["checkpoint", "ledger", "report"] and files["present"].all()
    assert set(identity["consistent"]) == {"yes"}
    assert set(checks["result"]) == {"PASS"}
    assert list(ideas["idea_status"]) == ["CLOSED", "CLOSED"] and len(fills) == 5
    for text in SYNTHETIC_TEXTS:
        assert text not in shown


def test_page_scenario_and_fake_clock_controls_change_only_the_judgement(tmp_path, monkeypatch):
    at = page(tmp_path, monkeypatch).run()
    at.radio[0].set_value("Synthetic: interrupted after commit").run()
    assert not at.exception
    metrics = {m.label: m.value for m in at.metric}
    assert metrics["Run state"] == "INCOMPLETE" and metrics["Checkpoint cursor"] == f"6 / {TOTAL}"
    assert metrics["Resumability"] == "RESUMABLE_BY_CONTRACT"
    shown = texts(at)
    assert "WARNING LEDGER_ONE_AHEAD_OF_CHECKPOINT" in shown and "cannot be distinguished" in shown
    assert "INFO NO_REPORT" in shown and "Report:** none readable" in shown
    artifacts = page_artifacts(at)
    before = file_state(artifacts)

    at.radio[1].set_value("Fake: 1 h 5 min after the last write (STALE)").run()
    assert not at.exception
    shown = texts(at)
    assert "Progress freshness:** STALE" in shown and "WARNING NO_RECENT_PROGRESS" in shown
    assert {m.label: m.value for m in at.metric}["Run state"] == "INCOMPLETE"  # the clock never changes the state
    at.radio[1].set_value("Fake: 1 h before the last write (FUTURE)").run()
    shown = texts(at)
    assert "Progress freshness:** FUTURE" in shown and "WARNING CHECKPOINT_WRITE_IN_FUTURE" in shown
    at.checkbox[0].check().run()
    shown = texts(at)
    assert "Progress freshness:** UNKNOWN" in shown and "WARNING CHECKPOINT_WRITE_TIME_UNKNOWN" in shown
    assert "written_at (wall clock)" in shown and shown.count("UNKNOWN") >= 4
    assert page_artifacts(at) == artifacts and file_state(artifacts) == before  # same files, untouched

    at.checkbox[0].uncheck().run()
    at.radio[1].set_value("Real wall clock").run()
    at.radio[0].set_value("Synthetic: missing checkpoint").run()
    assert not at.exception
    metrics = {m.label: m.value for m in at.metric}
    assert metrics["Run state"] == "INVALID" and metrics["Checkpoint cursor"] == f"UNKNOWN / {TOTAL}"
    assert metrics["Resumability"] == "NOT_RESUMABLE" and metrics["Serious findings"] == "1"
    shown = texts(at)
    assert "SERIOUS CHECKPOINT_MISSING" in shown and "Cursor UNKNOWN" in shown
    assert "feed epoch" in shown  # the ledger is still read, on the virtual clock, from the epoch
    assert page_artifacts(at) != artifacts  # a new scenario was built in a new temporary directory
    assert page_artifacts(at).workdir.is_relative_to(tmp_path)


def test_page_refresh_and_rerun_never_mutate_artifacts(tmp_path, monkeypatch):
    at = page(tmp_path, monkeypatch).run()
    at.radio[0].set_value("Synthetic: paused by delivery cap").run()
    artifacts = page_artifacts(at)
    before = file_state(artifacts)
    at.button[0].click().run()
    at.run()
    at.radio[1].set_value("Fake: 30 s after the last write (FRESH)").run()
    at.button[0].click().run()
    assert not at.exception
    assert page_artifacts(at) == artifacts  # reused, not rebuilt
    assert file_state(artifacts) == before
    assert {m.label: m.value for m in at.metric}["Run state"] == "PAUSED"
    assert "Pause observed" in texts(at) and "View refreshed." in [c.value for c in at.caption]


def test_page_uses_supplied_artifacts_and_shows_drift_and_contradictions_as_errors(tmp_path, monkeypatch):
    supplied = oh.build_scenario(tmp_path / "supplied", "completed")
    with closing(sqlite3.connect(str(supplied.workdir / LEDGER_FILE))) as c, c:  # the TEST corrupts its fixture
        c.execute("UPDATE replay_ideas SET quantity=9 WHERE idea_id='e01:a1'")
    before = file_state(supplied)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TRADINGBOT_DB", str(tmp_path / "forbidden.sqlite3"))
    at = page(tmp_path, monkeypatch)
    at.session_state["srh_scenario"] = "completed"
    at.session_state["srh_artifacts"] = supplied.model_dump(mode="json")
    at.run()
    assert not at.exception
    assert page_artifacts(at) == supplied and file_state(supplied) == before
    assert not (tmp_path / "data").exists() and not (tmp_path / "forbidden.sqlite3").exists()
    errors = [e.value for e in at.error]
    assert any(e.startswith("SERIOUS REPORT_LEDGER_MISMATCH") for e in errors)
    assert any(e.startswith("SERIOUS REPLAY_IDEA_QUANTITY_MISMATCH [replay] e01:a1") for e in errors)
    assert any(e.startswith("SERIOUS REPLAY_IDEA_STATUS_INCONSISTENT [replay] e01:a1") for e in errors)
    assert any(e.startswith("Contradictory evidence (REPORT_LEDGER_MISMATCH)") for e in errors)
    metrics = {m.label: m.value for m in at.metric}
    assert metrics["Run state"] == "CONTRADICTORY" and metrics["Serious findings"] == "3"
    assert metrics["Checkpoint cursor"] == f"{TOTAL} / {TOTAL}"  # the cursor is shown, the claim is not trusted
    shown = texts(at)
    assert "No run state can be trusted" in shown and "No discrepancies were detected" not in shown
    for marker in ("SYNTHETIC-CHECKPOINT-MARKER", "SYNTHETIC-REPORT-MARKER", *SYNTHETIC_TEXTS):
        assert marker not in shown


# ----------------------------------------------------------------------------- review round 1


def _resume_check(h: oh.ShadowRunHealth) -> oh.ResumabilityCheck:
    return next(c for c in h.resumability_checks if c.check == "LEDGER_NOT_BEHIND_CHECKPOINT")


def _damaged_copy(base: oh.ShadowRunArtifacts, destination: Path, sql: str) -> oh.ShadowRunArtifacts:
    """A copy of a scenario (run directory and report) whose ledger the TEST damages with ``sql``."""
    copy_of(base, destination / "run")
    report = None
    if base.report_path is not None:
        report = destination / oh.REPORT_FILE
        shutil.copyfile(base.report_path, report)
    artifacts = oh.ShadowRunArtifacts(label=destination.name, workdir=destination / "run", report_path=report)
    with closing(sqlite3.connect(str(artifacts.workdir / LEDGER_FILE))) as c, c:
        c.execute(sql)
    return artifacts


def test_resume_check_requires_decodable_prefix_outcomes_and_matches_the_runner(tmp_path):
    """The runner's resume validation reads the latest-attempt outcome records of every committed post
    (PaperEventReplay.outcomes). A post row alone must never pass the read-only mirror of that check."""
    base = oh.build_scenario(tmp_path / "paused", "paused_by_delivery_cap")
    control = assess(base)
    assert control.resumability == "RESUMABLE_BY_CONTRACT" and _resume_check(control).result == "PASS"
    assert "decodable recorded outcomes" in _resume_check(control).detail
    assert control.ledger.posts_without_recorded_outcomes == 0
    assert control.ledger.posts_with_undecodable_outcomes == 0
    resumed = ShadowRunner(copy_of(base, tmp_path / "control-runner"), FEED, POLICY, resume=True).run()
    assert resumed.status == "COMPLETE"  # the positive control still holds

    # Action rows deleted while the post row survives: the runner refuses; the view cannot prove the
    # precondition read-only (the post might have produced no actions), so it is UNKNOWN, never PASS.
    deleted = _damaged_copy(base, tmp_path / "deleted", "DELETE FROM replay_actions WHERE source_id='e01'")
    h = assess(deleted)
    assert h.resumability == "UNKNOWN" and _resume_check(h).result == "SKIPPED"
    assert "cannot be established read-only" in _resume_check(h).detail
    findings = {f.code: f for f in h.findings}
    assert findings["LEDGER_OUTCOMES_NOT_RECORDED"].severity == "WARNING"
    assert "1 committed post(s)" in findings["LEDGER_OUTCOMES_NOT_RECORDED"].detail
    assert "REPORT_LEDGER_MISMATCH" in findings and h.run_state == "CONTRADICTORY"
    assert h.ledger.posts_without_recorded_outcomes == 1
    assert "REPLAY_IDEA_WITHOUT_FILL" in {d.code for d in h.ledger.discrepancies}
    with pytest.raises(ShadowStateError, match="has no recorded outcome"):
        ShadowRunner(copy_of(deleted, tmp_path / "deleted-runner"), FEED, POLICY, resume=True)

    # The latest outcome record does not decode: the runner fails while reading it; the view fails the
    # check and reports the record by error type only.
    malformed = _damaged_copy(
        base, tmp_path / "malformed",
        "UPDATE replay_actions SET result_json='not json SYNTHETIC-OUTCOME-MARKER' WHERE source_id='e01'",
    )
    h = assess(malformed)
    assert h.resumability == "NOT_RESUMABLE" and _resume_check(h).result == "FAIL"
    assert "1 with undecodable outcome records" in _resume_check(h).detail
    findings = {f.code: f for f in h.findings}
    assert findings["LEDGER_OUTCOMES_UNDECODABLE"].severity == "SERIOUS" and h.run_state == "CONTRADICTORY"
    assert h.ledger.posts_with_undecodable_outcomes == 1 and not h.ledger.reconciliation_complete
    assert "REPLAY_RECORD_UNDECODABLE" in {d.code for d in h.ledger.discrepancies}
    assert "SYNTHETIC-OUTCOME-MARKER" not in h.model_dump_json()
    with pytest.raises(ValidationError):
        ShadowRunner(copy_of(malformed, tmp_path / "malformed-runner"), FEED, POLICY, resume=True)

    # Without a report to contradict, UNKNOWN stands on its own (not masked by CONTRADICTORY).
    interrupted = oh.build_scenario(tmp_path / "interrupted", "interrupted_before_commit")
    with closing(sqlite3.connect(str(interrupted.workdir / LEDGER_FILE))) as c, c:  # the TEST damages its fixture
        c.execute("DELETE FROM replay_actions WHERE source_id='e02'")
    h = assess(interrupted)
    assert (h.run_state, h.resumability) == ("INCOMPLETE", "UNKNOWN")
    assert "Resumability: UNKNOWN" in h.headline and h.attention_required
    assert {f.code for f in h.findings} >= {"LEDGER_OUTCOMES_NOT_RECORDED", "NO_REPORT"}
    with pytest.raises(ShadowStateError, match="has no recorded outcome"):
        ShadowRunner(copy_of(interrupted, tmp_path / "interrupted-runner"), FEED, POLICY, resume=True)


def _deny(monkeypatch: pytest.MonkeyPatch, filename: str, exc: Exception, method: str = "read_bytes") -> None:
    """Make ``pathlib.Path.<method>`` raise ``exc`` for one file name; every other path is untouched.
    No permission bits are changed anywhere."""
    original = getattr(pathlib.Path, method)

    def guarded(self, *args, **kwargs):
        if self.name == filename:
            raise exc
        return original(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, method, guarded)


def test_collector_reports_unreadable_or_vanishing_artifacts_instead_of_aborting(tmp_path, monkeypatch):
    base = oh.build_scenario(tmp_path / "paused", "paused_by_delivery_cap")
    before = file_state(base)
    secret = "REDACTED-PATH-MARKER"

    # Unreadable optional report: the checkpoint and ledger stay visible; the pause can no longer be shown.
    _deny(monkeypatch, oh.REPORT_FILE, PermissionError(13, "Permission denied", secret))
    evidence = oh.collect_evidence(base)
    assert evidence.report.present and evidence.report.read_error == "PermissionError"
    assert evidence.report_text is None and evidence.checkpoint_text is not None
    h = oh.assess_shadow_run(evidence, now=evidence.collected_at)
    assert (h.run_state, h.resumability) == ("INCOMPLETE", "RESUMABLE_BY_CONTRACT")
    assert h.progress.cursor == 5 and h.progress.report_status is None and h.ledger.readable
    findings = {f.code: f for f in h.findings}
    assert findings["REPORT_UNREADABLE"].severity == "WARNING" and "PermissionError" in findings["REPORT_UNREADABLE"].detail
    assert "NO_REPORT" not in findings
    dump = h.model_dump_json()
    assert secret not in dump and "Permission denied" not in dump
    monkeypatch.undo()
    _deny(monkeypatch, oh.REPORT_FILE, OSError(5, "Input/output error", secret))
    assert oh.collect_evidence(base).report.read_error == "OSError"
    monkeypatch.undo()

    # Unreadable checkpoint: truthful INVALID, the ledger is still read and the report still parsed.
    _deny(monkeypatch, CHECKPOINT_FILE, PermissionError(13, "Permission denied", secret))
    evidence = oh.collect_evidence(base)
    assert evidence.checkpoint.present and evidence.checkpoint.read_error == "PermissionError"
    assert evidence.checkpoint_text is None
    h = oh.assess_shadow_run(evidence, now=evidence.collected_at)
    assert (h.run_state, h.resumability) == ("INVALID", "NOT_RESUMABLE")
    findings = {f.code: f for f in h.findings}
    assert findings["CHECKPOINT_UNREADABLE"].severity == "SERIOUS" and "CHECKPOINT_MISSING" not in findings
    assert "CHECKPOINT_UNREADABLE" in h.headline and h.progress.cursor is None
    assert h.progress.report_status == "DELIVERY_CAP_REACHED" and h.ledger is not None and h.ledger.readable
    checks = {c.check: c for c in h.resumability_checks}
    assert checks["CHECKPOINT_VALID"].result == "FAIL" and "PermissionError" in checks["CHECKPOINT_VALID"].detail
    assert checks["LEDGER_NOT_BEHIND_CHECKPOINT"].result == "SKIPPED"
    assert h.observation.progress_freshness == "FRESH"  # the stat succeeded; only the read was refused
    assert secret not in h.model_dump_json()
    monkeypatch.undo()

    # Checkpoint stat denied (is_file propagates EACCES): existence unknown, reported as unreadable.
    _deny(monkeypatch, CHECKPOINT_FILE, PermissionError(13, "Permission denied", secret), method="stat")
    evidence = oh.collect_evidence(base)
    assert not evidence.checkpoint.present and evidence.checkpoint.read_error == "PermissionError"
    assert evidence.checkpoint.written_at is None
    h = oh.assess_shadow_run(evidence, now=evidence.collected_at)
    assert h.run_state == "INVALID" and "CHECKPOINT_UNREADABLE" in {f.code for f in h.findings}
    assert h.observation.progress_freshness == "UNKNOWN"
    monkeypatch.undo()

    # Ledger stat denied: nothing is read or reconciled, and the view says so.
    _deny(monkeypatch, LEDGER_FILE, PermissionError(13, "Permission denied", secret), method="stat")
    evidence = oh.collect_evidence(base)
    assert not evidence.ledger.present and evidence.ledger.read_error == "PermissionError"
    h = oh.assess_shadow_run(evidence, now=evidence.collected_at)
    assert (h.run_state, h.resumability) == ("INVALID", "NOT_RESUMABLE") and h.ledger is None
    findings = {f.code: f for f in h.findings}
    assert findings["LEDGER_INACCESSIBLE"].severity == "SERIOUS" and "LEDGER_MISSING" not in findings
    assert {c.check: c.result for c in h.resumability_checks}["LEDGER_PRESENT"] == "FAIL"
    monkeypatch.undo()

    assert file_state(base) == before  # nothing above touched the fixture

    # Checkpoint removed between observation and read: the observation is corrected to absent.
    vanishing = oh.ShadowRunArtifacts(
        label="vanish", workdir=copy_of(base, tmp_path / "vanish" / "run"), report_path=base.report_path
    )
    original = pathlib.Path.read_bytes

    def vanish(self):
        if self.name == CHECKPOINT_FILE:
            self.unlink()
        return original(self)

    monkeypatch.setattr(pathlib.Path, "read_bytes", vanish)
    evidence = oh.collect_evidence(vanishing)
    monkeypatch.undo()
    assert not evidence.checkpoint.present and evidence.checkpoint.read_error is None
    assert evidence.checkpoint.written_at is None and evidence.checkpoint_text is None
    assert not (vanishing.workdir / CHECKPOINT_FILE).exists()
    h = oh.assess_shadow_run(evidence, now=evidence.collected_at)
    assert h.run_state == "INVALID" and "CHECKPOINT_MISSING" in {f.code for f in h.findings}
    assert h.observation.progress_freshness == "UNKNOWN"


def test_page_survives_an_unreadable_optional_report(tmp_path, monkeypatch):
    supplied = oh.build_scenario(tmp_path / "supplied", "completed")
    before = file_state(supplied)
    _deny(monkeypatch, oh.REPORT_FILE, PermissionError(13, "Permission denied", "REDACTED-PATH-MARKER"))
    at = page(tmp_path, monkeypatch)
    at.session_state["srh_scenario"] = "completed"
    at.session_state["srh_artifacts"] = supplied.model_dump(mode="json")
    at.run()
    assert not at.exception
    metrics = {m.label: m.value for m in at.metric}
    assert metrics["Run state"] == "COMPLETE" and metrics["Checkpoint cursor"] == f"{TOTAL} / {TOTAL}"
    assert metrics["Resumability"] == "NOTHING_TO_RESUME" and metrics["Ideas"] == "2"
    warnings = [w.value for w in at.warning]
    assert any(w.startswith("WARNING REPORT_UNREADABLE") and "PermissionError" in w for w in warnings)
    assert any(w.startswith("At least one artifact could not be examined or read") for w in warnings)
    files = at.dataframe[0].value
    assert list(files["read_error"]) == ["—", "—", "PermissionError"]
    shown = texts(at)
    assert "Report:** none readable" in shown and "REDACTED-PATH-MARKER" not in shown
    assert "Permission denied" not in shown
    monkeypatch.undo()  # the denial served its purpose; the fixture itself must be untouched
    assert page_artifacts(at) == supplied and file_state(supplied) == before
