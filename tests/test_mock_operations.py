"""OPS-01 regression tests: the mock operations view is read-only, synthetic and honest about unknowns.

Synthetic data only. Covers the read-model (`tradingbot.operations_view`) and the headless rendering of
`dashboard/pages/2_Mock_Operations.py` through `streamlit.testing.v1.AppTest`.
"""

from __future__ import annotations

import ast
import re
import sqlite3
import tempfile
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

import tradingbot
from tradingbot import operations_view as ov
from tradingbot.operations_view import MockLedgers

REPO = Path(__file__).resolve().parents[1]
PAGE = REPO / "dashboard" / "pages" / "2_Mock_Operations.py"
MODULE = REPO / "src" / "tradingbot" / "operations_view.py"
PROTECTION_STATUSES = {"UNKNOWN", "NOT_RECORDED", "RECORDED_MOCK_UNVERIFIED"}
SYNTHETIC_TEXTS = (
    "BUY MOCKA",
    "REDUCE 50%",
    "SHORT MOCKB",
    "CLOSE MOCKB",
    "We remain constructive",
)


def ledger_dump(ledgers: MockLedgers) -> dict[str, dict[str, list]]:
    """Logical content of every table in both ledgers, independent of SQLite sidecar files."""
    out: dict[str, dict[str, list]] = {}
    for path in (ledgers.journal_path, ledgers.replay_path):
        assert path is not None
        with closing(sqlite3.connect(str(path))) as c:
            tables = [
                r[0]
                for r in c.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
                )
            ]
            out[path.name] = {
                t: c.execute(f"SELECT * FROM {t} ORDER BY rowid").fetchall() for t in tables
            }
    return out


def ledger_files(ledgers: MockLedgers) -> set[str]:
    assert ledgers.journal_path is not None
    return {p.name for p in ledgers.journal_path.parent.iterdir()}


def non_sidecar(names: set[str]) -> set[str]:
    return {n for n in names if not n.endswith(("-wal", "-shm"))}


def page(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AppTest:
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))  # fixtures stay task-local
    return AppTest.from_file(str(PAGE), default_timeout=60)


def texts(at: AppTest) -> str:
    parts = [e.value for e in at.error] + [w.value for w in at.warning] + [i.value for i in at.info]
    parts += [m.value for m in at.markdown] + [c.value for c in at.caption]
    return "\n".join(parts)


@pytest.fixture
def populated(tmp_path: Path) -> MockLedgers:
    return ov.build_synthetic_ledgers(tmp_path / "populated", "populated")


# ----------------------------------------------------------------------------- provenance


def test_import_origin_is_this_checkout():
    assert Path(ov.__file__).resolve().is_relative_to(REPO / "src")
    assert Path(tradingbot.__file__).resolve().is_relative_to(REPO / "src")


def test_module_and_page_import_no_network_broker_or_environment_access():
    forbidden = {
        "requests",
        "urllib",
        "http",
        "socket",
        "openai",
        "dotenv",
        "httpx",
        "websocket",
        "ssl",
        "smtplib",
        "subprocess",
        "os",
        "post_interpreters",
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
    assert (
        "Journal(" not in page_source and "PaperEventReplay(" not in page_source
    )  # the page constructs no engine


# ----------------------------------------------------------------------------- read-model


def test_empty_synthetic_run_is_reported_as_empty_not_healthy(tmp_path):
    ledgers = ov.build_synthetic_ledgers(tmp_path, "empty")
    assert ledgers.journal_path is not None and ledgers.journal_path.is_relative_to(tmp_path)
    s = ov.build_snapshot(ledgers)
    assert s.is_empty and set(s.counts().values()) == {0}
    assert [a.engine for a in s.activity] == ["journal", "replay"]
    assert all(a.readable and a.rows_on_record == 0 and a.last_write_at is None for a in s.activity)
    assert s.reconciliation_complete and not s.discrepancies and len(s.checks_performed) >= 5


def test_populated_synthetic_run_reads_positions_fills_outcomes_and_discrepancies(populated):
    s = ov.build_snapshot(populated)
    open_ = {(p.engine, p.idea_id): p for p in s.open_positions}
    assert set(open_) == {("journal", "syn-j-open-1"), ("replay", "syn-r-1:open")}
    journal_pos = open_[("journal", "syn-j-open-1")]
    assert (journal_pos.symbol, journal_pos.side, journal_pos.quantity) == ("MOCKA", "LONG", 1)
    assert journal_pos.protection == "UNKNOWN" and journal_pos.stop_price is None
    replay_pos = open_[("replay", "syn-r-1:open")]
    assert (replay_pos.quantity, replay_pos.stop_price, replay_pos.target_price) == (2, "97", "110")
    assert replay_pos.protection == "RECORDED_MOCK_UNVERIFIED"
    closed = {p.idea_id: p for p in s.closed_ideas}
    assert closed["syn-r-4:open"].idea_status == "CLOSED" and closed["syn-r-4:open"].quantity == 0

    fills = {f.order_id: f for f in s.fills}
    assert len(fills) == 10 and all(f.status.startswith("SIMULATED") for f in s.fills)
    assert (
        fills["mock-syn-j-open-1"].side,
        fills["mock-syn-j-open-1"].quantity,
        fills["mock-syn-j-open-1"].price,
    ) == ("BUY", 2, "100.05")
    assert (
        fills["mock-syn-j-close-2"].action == "CLOSE" and fills["mock-syn-j-close-2"].side == "BUY"
    )
    assert (
        fills["paper-syn-r-2-1-tighten"].status == "SIMULATED_AMENDED"
        and fills["paper-syn-r-2-1-tighten"].quantity == 0
    )
    assert (
        fills["paper-syn-r-3-1-trim"].action == "REDUCE"
        and fills["paper-syn-r-3-1-trim"].quantity == 2
    )
    assert all(
        f.recorded_at is not None and "ledger write time" in f.recorded_at_basis for f in s.fills
    )

    status = {(v.engine, v.source_id, v.action_id): v.status for v in s.validations}
    assert status[("journal", "syn-j-stale", None)] == "BLOCK"
    assert status[("journal", "syn-j-commentary", None)] == "NO_TRADE"
    assert status[("journal", "syn-j-orphan-ref", None)] == "REVIEW"
    assert status[("journal", "syn-j-error", None)] == "ERROR"
    assert status[("journal", "syn-j-inflight", None)] == "RECEIVED"
    assert status[("replay", "syn-r-6", "report")] == "RECONCILE"
    assert status[("replay", "syn-r-7", "maybe")] == "HOLD"
    assert status[("replay", "syn-r-8", "open")] == "BLOCK"
    assert status[("replay", "syn-r-9", "open")] == "NO_TRADE"
    assert status[("replay", "syn-r-10", "open")] == "HOLD"
    assert status[("replay", "syn-r-11", "exit")] == "BLOCK"
    assert {v.status for v in s.validations if v.needs_review} == {
        "RECEIVED",
        "ERROR",
        "REVIEW",
        "HOLD",
        "RECONCILE",
    }
    assert {v.status for v in s.validations if not v.needs_review} == {
        "EXECUTE_MOCK",
        "NO_TRADE",
        "BLOCK",
        "SIMULATED",
    }
    # Duplicate submissions in the fixture recorded nothing extra.
    assert [v.source_id for v in s.validations].count("syn-j-open-1") == 1
    assert [v.source_id for v in s.validations].count("syn-r-3") == 1

    assert {(d.engine, d.code, d.severity) for d in s.discrepancies} == {
        ("journal", "JOURNAL_RESERVATION_INCOMPLETE", "WARNING"),
        ("replay", "REPLAY_HISTORICAL_REPORT_UNRECONCILED", "WARNING"),
    }
    assert s.reconciliation_complete and not s.is_empty
    assert all(
        a.readable and a.last_write_at is not None and a.seconds_before_as_of >= 0
        for a in s.activity
    )


def test_unknown_protection_and_stale_or_untimed_observations_are_never_fresh(populated):
    s = ov.build_snapshot(populated)
    assert s.positions and {p.protection for p in s.positions} <= PROTECTION_STATUSES
    for p in s.positions:
        assert p.protection_note == ov.PROTECTION_NOTES[p.protection]
        assert (
            "unknown" in p.protection_note
            or "none is recorded" in p.protection_note
            or "no broker-native" in p.protection_note
        )
    by_id = {v.source_id: v for v in s.validations if v.engine == "journal"}
    assert (
        by_id["syn-j-stale"].source_freshness == "STALE"
        and by_id["syn-j-stale"].source_age_seconds > s.signal_window_seconds
    )
    assert by_id["syn-j-open-1"].source_freshness == "FRESH"

    later = ov.build_snapshot(populated, now=datetime.now(timezone.utc) + timedelta(hours=1))
    assert {v.source_freshness for v in later.validations} == {"STALE"}
    earlier = ov.build_snapshot(populated, now=datetime.now(timezone.utc) - timedelta(hours=1))
    assert {v.source_freshness for v in earlier.validations} == {"FUTURE"}
    assert all(a.seconds_before_as_of < 0 for a in earlier.activity)

    assert ov.snapshot_freshness(s, now=s.as_of + timedelta(seconds=1)) == (1.0, "FRESH")
    assert (
        ov.snapshot_freshness(
            s, now=s.as_of + timedelta(seconds=ov.SNAPSHOT_STALE_AFTER_SECONDS + 1)
        )[1]
        == "STALE"
    )
    assert ov.snapshot_freshness(s, now=s.as_of - timedelta(seconds=30))[1] == "FUTURE"

    with (
        closing(sqlite3.connect(str(populated.journal_path))) as c,
        c,
    ):  # the TEST corrupts its own fixture
        c.execute(
            "UPDATE events SET published_at='2026-11-06T15:00:00' WHERE source_id='syn-j-commentary'"
        )
    untimed = {v.source_id: v for v in ov.build_snapshot(populated).validations}["syn-j-commentary"]
    assert untimed.source_freshness == "UNKNOWN" and untimed.source_age_seconds is None


def test_reconciliation_detects_injected_ledger_drift(populated):
    with closing(sqlite3.connect(str(populated.journal_path))) as c, c:
        c.execute("UPDATE positions SET quantity=5 WHERE idea_id='syn-j-open-1'")
        c.execute("INSERT INTO positions VALUES ('ghost-j', 'MOCKZ', 'EQUITY', 'LONG', 1)")
    with closing(sqlite3.connect(str(populated.replay_path))) as c, c:
        c.execute("UPDATE replay_ideas SET quantity=9 WHERE idea_id='syn-r-1:open'")
        c.execute("UPDATE replay_ideas SET status='OPEN' WHERE idea_id='syn-r-4:open'")
        c.execute(
            "INSERT INTO replay_ideas VALUES ('ghost-r', 'MOCKZ', 'ETF', 'LONG', 1, NULL, NULL, 'OPEN')"
        )
    s = ov.build_snapshot(populated)
    serious = {(d.engine, d.code, d.subject) for d in s.discrepancies if d.severity == "SERIOUS"}
    assert serious == {
        ("journal", "JOURNAL_POSITION_QUANTITY_MISMATCH", "syn-j-open-1"),
        ("journal", "JOURNAL_POSITION_WITHOUT_FILL", "ghost-j"),
        ("replay", "REPLAY_IDEA_QUANTITY_MISMATCH", "syn-r-1:open"),
        ("replay", "REPLAY_IDEA_STATUS_INCONSISTENT", "syn-r-4:open"),
        ("replay", "REPLAY_IDEA_WITHOUT_FILL", "ghost-r"),
    }
    ghost = next(p for p in s.positions if p.idea_id == "ghost-r")
    assert ghost.protection == "NOT_RECORDED" and ghost in s.open_positions
    assert s.counts()["serious_discrepancies"] == 5 and s.reconciliation_complete

    with closing(sqlite3.connect(str(populated.journal_path))) as c, c:
        c.execute("DELETE FROM positions WHERE idea_id='syn-j-open-1'")
    codes = {d.code for d in ov.build_snapshot(populated).discrepancies}
    assert (
        "JOURNAL_FILL_WITHOUT_POSITION" in codes
        and "JOURNAL_POSITION_QUANTITY_MISMATCH" not in codes
    )

    partial = ov.build_snapshot(populated, journal_event_limit=3)
    assert not partial.reconciliation_complete
    partial_codes = {d.code for d in partial.discrepancies}
    assert "JOURNAL_HISTORY_TRUNCATED" in partial_codes
    quantity_codes = {
        "JOURNAL_POSITION_QUANTITY_MISMATCH",
        "JOURNAL_FILL_WITHOUT_POSITION",
        "JOURNAL_POSITION_WITHOUT_FILL",
    }
    assert (
        not partial_codes & quantity_codes
    )  # not reconciled on partial history, instead of guessing
    assert (
        "JOURNAL_RESERVATION_INCOMPLETE" in partial_codes
    )  # per-row checks still run on what was read
    assert next(a for a in partial.activity if a.engine == "journal").history_truncated


def test_missing_or_foreign_ledger_files_are_reported_and_never_created(tmp_path, populated):
    missing = tmp_path / "does-not-exist.sqlite3"
    s = ov.build_snapshot(
        MockLedgers(journal_path=missing, replay_path=populated.journal_path, label="bad")
    )
    assert {(d.engine, d.code, d.severity) for d in s.discrepancies} == {
        ("journal", "LEDGER_FILE_MISSING", "SERIOUS"),
        ("replay", "LEDGER_UNREADABLE", "SERIOUS"),
    }
    assert not s.reconciliation_complete and not any(a.readable for a in s.activity)
    assert not missing.exists()  # a read-only view never creates a database
    assert s.is_empty and s.counts()["serious_discrepancies"] == 2


def test_view_connections_are_read_only_by_construction(populated):
    with ov.read_only_connection(populated.journal_path) as c:
        assert c.execute("SELECT COUNT(*) FROM positions").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            c.execute("DELETE FROM positions")
    with ov.read_only_connection(populated.replay_path) as c:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            c.execute("UPDATE replay_ideas SET quantity=0")
    source = MODULE.read_text()
    tree = ast.parse(source)
    connects = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and getattr(n.func, "attr", None) == "connect"
    ]
    assert len(connects) == 1 and {k.arg for k in connects[0].keywords} >= {"uri", "timeout"}
    assert "mode=ro" in ast.get_source_segment(source, connects[0])
    assert "executescript" not in source and "executemany" not in source
    # Every statement the module executes is a SELECT, apart from the one BEGIN that opens the read
    # transaction pinning a single snapshot per ledger (nothing else is ever sent to a ledger).
    statements = [
        n.args[0]
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and getattr(n.func, "attr", None) == "execute"
    ]
    sql = []
    for stmt in statements:
        assert isinstance(stmt, ast.Constant) and isinstance(stmt.value, str), ast.dump(stmt)[:80]
        sql.append(stmt.value)
    assert sql.count("BEGIN") == 1
    selects = [q for q in sql if q != "BEGIN"]
    assert len(selects) == 6, selects  # 3 journal reads, 3 replay reads
    assert all(re.match(r"\s*SELECT\b", q) for q in selects), selects


def test_build_snapshot_never_mutates_or_adds_ledger_content(populated):
    before, files_before = ledger_dump(populated), ledger_files(populated)
    for _ in range(3):
        ov.build_snapshot(populated)
    assert ledger_dump(populated) == before
    assert non_sidecar(ledger_files(populated)) == non_sidecar(files_before)
    assert (
        populated.replay_path is not None and populated.replay_path.read_bytes()
    )  # still a plain file


def test_snapshot_never_carries_source_text(populated):
    blob = ov.build_snapshot(populated).model_dump_json()
    for text in SYNTHETIC_TEXTS:
        assert text not in blob
    assert "post_hash" not in blob


def test_policy_window_is_the_shared_mock_policy(populated):
    from tradingbot.models import MockPolicy

    assert ov.build_snapshot(populated).signal_window_seconds == MockPolicy().max_signal_age_seconds
    s = ov.build_snapshot(populated, policy=MockPolicy(max_signal_age_seconds=100000))
    assert {v.source_freshness for v in s.validations} == {"FRESH"}


# ----------------------------------------------------------------------------- headless UI


def test_page_renders_populated_run_headlessly(tmp_path, monkeypatch):
    at = page(tmp_path, monkeypatch).run()
    assert not at.exception
    assert at.title[0].value == "Mock Operations (read-only)"
    assert "READ-ONLY" in at.error[0].value and "NO ORDER SUBMISSION" in at.error[0].value
    assert [b.label for b in at.button] == ["Refresh view (read-only)"]
    assert not (
        at.number_input or at.text_input or at.text_area or at.selectbox or at.checkbox or at.slider
    )
    assert [r.value for r in at.radio] == ["Synthetic: populated run"]
    ledgers = MockLedgers.model_validate(at.session_state["mock_ops_ledgers"])
    assert ledgers.journal_path is not None and ledgers.journal_path.is_relative_to(tmp_path)
    metrics = {m.label: m.value for m in at.metric}
    assert metrics == {
        "Open mock positions": "2",
        "Fictional fills": "10",
        "Outcomes needing review": "6",
        "Discrepancies": "2",
    }
    positions, closed, fills, validations = [df.value for df in at.dataframe]
    assert set(positions["protection"]) == {"UNKNOWN", "RECORDED_MOCK_UNVERIFIED"}
    assert list(closed["idea_status"]) == ["CLOSED"]
    assert len(fills) == 10 and len(validations) == 21
    assert validations.loc[
        validations["source_id"] == "syn-j-stale", "source_freshness"
    ].tolist() == ["STALE"]
    shown = texts(at)
    assert "Protection is not verified for any position" in shown and "UNKNOWN" in shown
    assert "valuation and P&L are unknown" in shown
    assert (
        "WARNING JOURNAL_RESERVATION_INCOMPLETE" in shown
        and "WARNING REPLAY_HISTORICAL_REPORT_UNRECONCILED" in shown
    )
    assert "Snapshot as-of:" in shown and "last ledger write" in shown
    assert "No discrepancies were detected" not in shown
    for text in SYNTHETIC_TEXTS:
        assert text not in shown


def test_page_refresh_and_rerun_never_mutate_ledgers(tmp_path, monkeypatch):
    at = page(tmp_path, monkeypatch).run()
    ledgers = MockLedgers.model_validate(at.session_state["mock_ops_ledgers"])
    before, files_before = ledger_dump(ledgers), ledger_files(ledgers)
    first_as_of = next(m.value for m in at.markdown if "Snapshot as-of:" in m.value)
    at.button[0].click().run()
    at.run()
    at.button[0].click().run()
    assert not at.exception
    assert (
        MockLedgers.model_validate(at.session_state["mock_ops_ledgers"]) == ledgers
    )  # reused, not rebuilt
    assert ledger_dump(ledgers) == before
    assert non_sidecar(ledger_files(ledgers)) == non_sidecar(files_before)
    assert any("Snapshot as-of:" in m.value for m in at.markdown) and first_as_of


def test_page_renders_empty_run(tmp_path, monkeypatch):
    at = page(tmp_path, monkeypatch).run()
    at.radio[0].set_value("Synthetic: empty run").run()
    assert not at.exception
    infos = [i.value for i in at.info]
    assert any(i.startswith("Empty run:") for i in infos)
    assert "Nothing to reconcile: both ledgers are empty." in infos
    assert {m.value for m in at.metric} == {"0"}
    assert not at.dataframe
    assert "no ledger write recorded" in texts(at)
    assert at.session_state["mock_ops_scenario"] == "empty"


def test_page_uses_supplied_ledgers_and_shows_drift_as_errors(tmp_path, monkeypatch):
    supplied = ov.build_synthetic_ledgers(tmp_path / "supplied", "populated")
    with closing(sqlite3.connect(str(supplied.journal_path))) as c, c:
        c.execute("UPDATE positions SET quantity=7 WHERE idea_id='syn-j-open-1'")
    before = ledger_dump(supplied)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TRADINGBOT_DB", str(tmp_path / "forbidden.sqlite3"))
    at = page(tmp_path, monkeypatch)
    at.session_state["mock_ops_scenario"] = "populated"
    at.session_state["mock_ops_ledgers"] = supplied.model_dump(mode="json")
    at.run()
    assert not at.exception
    assert MockLedgers.model_validate(at.session_state["mock_ops_ledgers"]) == supplied
    assert ledger_dump(supplied) == before
    assert not (tmp_path / "data").exists() and not (tmp_path / "forbidden.sqlite3").exists()
    errors = [e.value for e in at.error]
    assert any(e.startswith("SERIOUS JOURNAL_POSITION_QUANTITY_MISMATCH") for e in errors)
    assert "No discrepancies were detected" not in texts(at)
    assert {m.label: m.value for m in at.metric}["Discrepancies"] == "3"


# ----------------------------------------------------------------------------- review round 1


class _Interposed:
    """Test-only connection wrapper: runs a callback with each statement before it executes."""

    def __init__(self, conn, before):
        self._conn, self._before = conn, before

    def execute(self, sql, *args):
        self._before(sql)
        return self._conn.execute(sql, *args)

    def __getattr__(self, name):
        return getattr(self._conn, name)


def _interpose(monkeypatch, before):
    real = ov.read_only_connection

    @contextmanager
    def hooked(path):
        with real(path) as conn:
            yield _Interposed(conn, before)

    monkeypatch.setattr(ov, "read_only_connection", hooked)
    return real


def test_journal_commit_between_reads_does_not_cause_false_drift(populated, monkeypatch):
    """One read transaction per ledger: a WAL writer committing after the fills were read and before
    the positions are read leaves the snapshot internally consistent (no fills/positions mismatch)."""
    from tradingbot.interpreters import DemoInterpreter
    from tradingbot.models import MockPolicy, Quote, SourceEvent
    from tradingbot.pipeline import SignalPipeline
    from tradingbot.storage import Journal

    pipe = SignalPipeline(
        Journal(populated.journal_path), DemoInterpreter(), MockPolicy(quantity=2)
    )
    committed: list[str] = []

    def race_writer(sql: str) -> None:
        if "FROM positions" in sql:
            now = datetime.now(timezone.utc)
            result = pipe.process(
                SourceEvent(
                    source_id="race-open", text="BUY MOCKX @ 10 STOP 9 TARGET 12", published_at=now
                ),
                Quote(symbol="MOCKX", bid=Decimal("9.99"), ask=Decimal("10.01"), observed_at=now),
            )
            committed.append(result.decision.status)

    real = _interpose(monkeypatch, race_writer)
    racy = ov.build_snapshot(populated)
    monkeypatch.setattr(ov, "read_only_connection", real)

    assert committed == ["EXECUTE_MOCK"]  # the engine really committed while the view was reading
    assert not [d for d in racy.discrepancies if d.severity == "SERIOUS"]
    seen = (
        {p.idea_id for p in racy.positions}
        | {f.idea_id for f in racy.fills}
        | {v.source_id for v in racy.validations}
    )
    assert "race-open" not in seen  # the whole snapshot predates the commit
    after = ov.build_snapshot(populated)
    assert "race-open" in {p.idea_id for p in after.open_positions}
    assert "race-open" in {f.idea_id for f in after.fills}
    assert not [d for d in after.discrepancies if d.severity == "SERIOUS"]


def test_replay_writer_cannot_tear_the_view_snapshot(populated, monkeypatch):
    """The replay ledger uses a rollback journal: a commit attempted between the view's reads waits for
    the view's read lock instead of being interleaved, so no false drift is possible there either."""
    attempts: list[str] = []

    def contending_writer(sql: str) -> None:
        if "FROM replay_ideas" in sql:
            with closing(sqlite3.connect(str(populated.replay_path), timeout=0.2)) as w:
                try:
                    w.execute("UPDATE replay_ideas SET quantity=99 WHERE idea_id='syn-r-1:open'")
                    w.commit()
                    attempts.append("committed")
                except sqlite3.OperationalError as exc:
                    attempts.append(str(exc))
                    w.rollback()

    real = _interpose(monkeypatch, contending_writer)
    racy = ov.build_snapshot(populated)
    monkeypatch.setattr(ov, "read_only_connection", real)

    assert len(attempts) == 1 and "locked" in attempts[0]
    assert not [d for d in racy.discrepancies if d.severity == "SERIOUS"]
    assert next(p for p in racy.open_positions if p.idea_id == "syn-r-1:open").quantity == 2
    # Once the view released its read transaction the same write succeeds, and the next snapshot sees it.
    with closing(sqlite3.connect(str(populated.replay_path))) as w, w:
        w.execute("UPDATE replay_ideas SET quantity=99 WHERE idea_id='syn-r-1:open'")
    assert "REPLAY_IDEA_QUANTITY_MISMATCH" in {
        d.code for d in ov.build_snapshot(populated).discrepancies
    }


def test_corrupt_ledger_files_and_undecodable_records_are_reported_not_fatal(
    tmp_path, monkeypatch, populated
):
    not_a_db = tmp_path / "not-a-database.sqlite3"
    not_a_db.write_text("plain text, not SQLite\n" * 40)
    s = ov.build_snapshot(MockLedgers(journal_path=not_a_db, replay_path=populated.replay_path))
    assert {(d.engine, d.code) for d in s.discrepancies if d.severity == "SERIOUS"} == {
        ("journal", "LEDGER_UNREADABLE")
    }
    assert not s.reconciliation_complete
    assert {p.idea_id for p in s.open_positions} == {
        "syn-r-1:open"
    }  # the healthy ledger still renders
    s = ov.build_snapshot(MockLedgers(journal_path=populated.journal_path, replay_path=not_a_db))
    assert {(d.engine, d.code) for d in s.discrepancies if d.severity == "SERIOUS"} == {
        ("replay", "LEDGER_UNREADABLE")
    }
    assert {p.idea_id for p in s.open_positions} == {"syn-j-open-1"}

    with closing(sqlite3.connect(str(populated.journal_path))) as c, c:
        c.execute(
            "UPDATE events SET order_json='{\"garbage\": \"PAYLOAD-MARKER\"}' WHERE source_id='syn-j-open-1'"
        )
        c.execute(
            "UPDATE events SET decision_json='not json PAYLOAD-MARKER' WHERE source_id='syn-j-commentary'"
        )
    with closing(sqlite3.connect(str(populated.replay_path))) as c, c:
        c.execute(
            "UPDATE replay_actions SET result_json='not json PAYLOAD-MARKER' WHERE source_id='syn-r-1'"
        )
    s = ov.build_snapshot(populated)
    serious = {(d.engine, d.code, d.subject) for d in s.discrepancies if d.severity == "SERIOUS"}
    assert serious == {
        ("journal", "JOURNAL_RECORD_UNDECODABLE", "syn-j-open-1"),
        ("journal", "JOURNAL_RECORD_UNDECODABLE", "syn-j-commentary"),
        ("replay", "REPLAY_RECORD_UNDECODABLE", "syn-r-1 r1 / open"),
    }
    assert "PAYLOAD-MARKER" not in s.model_dump_json()  # diagnostics never carry the raw payload
    assert not s.reconciliation_complete
    assert {a.engine: a.undecodable_rows for a in s.activity} == {"journal": 2, "replay": 1}
    quantity_codes = {
        "JOURNAL_POSITION_QUANTITY_MISMATCH",
        "JOURNAL_FILL_WITHOUT_POSITION",
        "JOURNAL_POSITION_WITHOUT_FILL",
        "REPLAY_IDEA_QUANTITY_MISMATCH",
        "REPLAY_FILL_WITHOUT_IDEA",
        "REPLAY_IDEA_WITHOUT_FILL",
    }
    assert (
        not {d.code for d in s.discrepancies} & quantity_codes
    )  # partial history is not reconciled as complete
    rows = {(v.engine, v.source_id, v.action_id): v for v in s.validations}
    assert rows[("journal", "syn-j-open-1", None)].needs_review
    assert "RECORD_UNDECODABLE" in rows[("journal", "syn-j-open-1", None)].reasons
    assert rows[("journal", "syn-j-commentary", None)].reasons == ["RECORD_UNDECODABLE"]
    assert (
        rows[("replay", "syn-r-1", "open")].status == "UNDECODABLE"
        and rows[("replay", "syn-r-1", "open")].needs_review
    )
    assert "paper-syn-r-1-1-open" not in {f.order_id for f in s.fills}

    at = page(tmp_path, monkeypatch)
    at.session_state["mock_ops_scenario"] = "populated"
    at.session_state["mock_ops_ledgers"] = populated.model_dump(mode="json")
    at.run()
    assert not at.exception
    errors = [e.value for e in at.error]
    assert any(e.startswith("SERIOUS JOURNAL_RECORD_UNDECODABLE") for e in errors)
    assert any(e.startswith("SERIOUS REPLAY_RECORD_UNDECODABLE") for e in errors)
    assert any("Reconciliation is INCOMPLETE" in e for e in errors)
    assert "PAYLOAD-MARKER" not in texts(at)


def test_untimed_ledger_activity_is_unknown_in_read_model_and_page(
    tmp_path, monkeypatch, populated
):
    with closing(sqlite3.connect(str(populated.journal_path))) as c, c:
        c.execute(
            "UPDATE events SET created_at='2099-01-01T00:00:00' WHERE source_id='syn-j-inflight'"
        )  # naive, newest
    with closing(sqlite3.connect(str(populated.replay_path))) as c, c:
        c.execute(
            "UPDATE replay_posts SET processed_at='2026-11-06T15:00:00' WHERE source_id='syn-r-5'"
        )  # mixed
    s = ov.build_snapshot(populated)  # must not raise on aware/naive comparison
    act = {a.engine: a for a in s.activity}
    for engine in ("journal", "replay"):
        assert act[engine].readable and act[engine].last_write_at is None
        assert act[engine].seconds_before_as_of is None and act[engine].untimed_rows == 1
    assert s.reconciliation_complete and len(s.open_positions) == 2
    assert ov._last_write([]) == (None, 0)
    assert ov._last_write([None, datetime(2026, 1, 1)]) == (None, 2)
    aware = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert ov._last_write([aware, aware + timedelta(seconds=5)]) == (
        aware + timedelta(seconds=5),
        0,
    )

    at = page(tmp_path, monkeypatch)
    at.session_state["mock_ops_scenario"] = "populated"
    at.session_state["mock_ops_ledgers"] = populated.model_dump(mode="json")
    at.run()
    assert not at.exception
    assert [h.value for h in at.subheader] == [
        "As-of",
        "Mock positions",
        "Fictional orders and fills",
        "Validation outcomes",
        "Reconciliation",
    ]
    unknown = [w.value for w in at.warning if "last ledger write UNKNOWN" in w.value]
    assert len(unknown) == 2 and all("without a timezone" in w for w in unknown)
    assert {m.label: m.value for m in at.metric}["Open mock positions"] == "2"
