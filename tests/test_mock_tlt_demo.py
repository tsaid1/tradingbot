"""Track B: the synthetic, mock-only TLT lifecycle demonstration. Every post, level and date is invented.

Covers the acceptance flow end to end through the existing stack: synthetic input, interpretation with
provenance, the deterministic gateway, simulated fill, protected position, partial and complete close,
reconciliation, an operator-readable audit, idempotent replay and restart safety.
"""
from __future__ import annotations

import importlib.util
import json
import re
from datetime import timedelta
from itertools import pairwise
from pathlib import Path

import pytest

from tradingbot.demo_lifecycle import (
    DEFAULT_SCRIPT,
    REASON_EXPLANATIONS,
    STATUS_EXPLANATIONS,
    DemoSession,
    LedgerIdea,
    ReconciliationRow,
    explain,
    fresh_database,
    load_script,
)
from tradingbot.evaluation import run_golden
from tradingbot.source_events import Provenance

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src" / "tradingbot"
ORDER = ["d1", "d2", "d3", "d4", "d5", "d6", "d7", "d8", "d9"]


def session(tmp_path, name="demo.sqlite3") -> DemoSession:
    return DemoSession(tmp_path / name)


# ----------------------------------------------------------------------------- fixture integrity

def test_script_is_marked_synthetic_and_self_consistent():
    script = load_script(DEFAULT_SCRIPT)
    assert script.synthetic_only is True and "invented" in script.notice and "no broker" in script.notice.lower()
    posts = script.posts
    assert [p.source_id for p in posts] == ORDER == [s.source_id for s in script.golden.steps]
    assert all(p.provenance == Provenance.SYNTHETIC for p in posts)
    assert all(p.author_id.startswith("DEMO-") for p in posts)
    assert all(p.source_id in script.narration and script.narration[p.source_id] for p in posts)
    assert [p.source_id for p in posts if not p.verified_author] == ["d9"]
    assert all(p.revision == 1 and p.text_complete and not p.is_repost for p in posts)
    # an invented future window, strictly increasing, so the posts cannot coincide with real publications
    assert all((p.published_at.year, p.published_at.month) == (2026, 11) and p.published_at.day >= 16 for p in posts)
    assert all(a.published_at < b.published_at for a, b in pairwise(posts))
    assert set(script.golden.quotes) == set(ORDER) and all(q == {"TLT": q["TLT"]} for q in script.golden.quotes.values())
    assert script.golden.policy == {"quantity": 4, "quantity_per_add": 2}


def test_script_passes_the_existing_golden_runner_unchanged(tmp_path):
    result = run_golden(load_script().golden, {}, tmp_path / "golden.sqlite3")
    assert result.passed and result.steps == 9, result.mismatches


# ----------------------------------------------------------------------------- the lifecycle

def test_full_lifecycle_matches_every_expected_outcome(tmp_path):
    s = session(tmp_path)
    entries = s.run_all()
    assert [e.source_id for e in entries] == ORDER and [e.step for e in entries] == list(range(1, 10))
    assert all(e.matches_expectation and not e.duplicate for e in entries)
    by = {e.source_id: e for e in entries}

    # 1-5: synthetic signal -> interpretation with provenance -> gateway -> simulated fill -> protected position
    opened = by["d1"]
    assert opened.provenance.verified_author and opened.provenance.complete_text and opened.provenance.revision == 1
    assert opened.provenance.post_hash and len(opened.provenance.post_hash) == 64
    assert opened.quotes == {"TLT": {"bid": "99.95", "ask": "100.05"}}
    (clause,) = opened.interpretations
    assert (clause.message_class, clause.tense, clause.action, clause.timing) == ("OPEN", "PRESENT", "OPEN", "CURRENT")
    assert (clause.asset, clause.side, clause.entry_reference, clause.stop_price, clause.target_price) == ("TLT", "LONG", "100.00", "96.00", "108.00")
    assert clause.explicit_instruction and clause.ambiguities == []
    (outcome,) = opened.outcomes
    assert outcome.status == "SIMULATED" and outcome.reasons == ["PAPER_OPEN"] and outcome.explanations
    assert (outcome.order.side, outcome.order.quantity, outcome.order.price, outcome.order.status) == ("BUY", 4, "100.05", "SIMULATED_FILLED")
    assert opened.ledger_after == [LedgerIdea(idea_id="d1:a1", symbol="TLT", asset_class="ETF", position_side="LONG",
                                              quantity=4, stop_price="96.00", target_price="108.00", status="OPEN")]
    assert opened.headline.startswith("SIMULATED BUY 4 TLT @ 100.05")

    # add + stop raise in one sentence: one atomic group, fill plus amendment, stop tightened
    added = by["d2"]
    assert [i.message_class for i in added.interpretations] == ["ADD", "MODIFY"]
    assert {i.atomic_group for i in added.interpretations} == {"s0-TLT"}
    assert {i.idea_reference for i in added.interpretations} == {"d1:a1"}
    assert [o.order.status for o in added.outcomes] == ["SIMULATED_FILLED", "SIMULATED_AMENDED"]
    assert [o.reasons for o in added.outcomes] == [["PAPER_ADD"], ["PAPER_PROTECTION_AMENDMENT"]]
    assert (added.ledger_after[0].quantity, added.ledger_after[0].stop_price, added.ledger_after[0].target_price) == (6, "98.00", "108.00")
    assert "stop -> 98.00" in added.headline

    # understandable holds and no-trades that change nothing
    held = by["d3"]
    assert held.interpretations[0].message_class == "CONDITIONAL" and held.interpretations[0].timing == "CONDITIONAL"
    assert held.outcomes[0].status == "HOLD" and held.outcomes[0].reasons == ["CONDITIONAL_TRIGGER_NOT_IMPLEMENTED"]
    assert "never triggered automatically" in held.outcomes[0].explanations[0] and "HOLD" in held.headline
    commentary = by["d4"]
    assert commentary.interpretations[0].message_class == "COMMENTARY"
    assert commentary.outcomes[0].status == "NO_TRADE" and commentary.outcomes[0].reasons == ["COMMENTARY_OR_SUMMARY"]
    for sid in ("d3", "d4"):
        assert by[sid].ledger_after == added.ledger_after and all(o.order is None for o in by[sid].outcomes)

    # 6: partial, then complete close
    reduced = by["d5"]
    assert reduced.interpretations[0].close_fraction == "0.5" and reduced.interpretations[0].idea_reference == "d1:a1"
    assert (reduced.outcomes[0].order.side, reduced.outcomes[0].order.quantity, reduced.outcomes[0].order.price) == ("SELL", 3, "103.95")
    assert (reduced.ledger_after[0].quantity, reduced.ledger_after[0].status, reduced.ledger_after[0].stop_price) == (3, "OPEN", "98.00")
    closed = by["d6"]
    assert (closed.outcomes[0].order.side, closed.outcomes[0].order.quantity, closed.outcomes[0].order.price) == ("SELL", 3, "105.95")
    assert (closed.ledger_after[0].quantity, closed.ledger_after[0].status) == (0, "CLOSED")

    # 7: reconciliation of a past-tense confirmation: no order, ledger consistency reported
    confirmed = by["d7"]
    assert confirmed.interpretations[0].message_class == "RETROSPECTIVE" and confirmed.interpretations[0].timing == "HISTORICAL"
    assert confirmed.outcomes[0].status == "RECONCILE" and confirmed.outcomes[0].order is None
    assert confirmed.outcomes[0].reasons == ["HISTORICAL_REPORT_NO_ORDER"]
    assert confirmed.outcomes[0].reconciliation_note.startswith("Consistent")
    assert by["d8"].interpretations[0].message_class == "POSITION_SUMMARY" and by["d8"].outcomes[0].status == "NO_TRADE"

    # provenance failure: same wording, unverified author, blocked before any level is trusted
    blocked = by["d9"]
    assert not blocked.provenance.verified_author
    assert blocked.interpretations[0].message_class == "OPEN"          # interpreted, but
    assert blocked.outcomes[0].status == "BLOCK" and blocked.outcomes[0].reasons == ["SOURCE_NOT_AUTHENTICATED"]
    assert blocked.ledger_after == closed.ledger_after and "could not be verified" in blocked.headline

    assert s.summary() == {
        "steps_processed": 9, "steps_total": 9, "mock_fills": 4, "protection_amendments": 1, "holds": 1, "blocks": 1,
        "no_trades": 2, "reconciles": 1, "duplicates_rejected": 0, "expectations_met": True,
        "ledger_agrees_with_publisher": True,
    }
    assert s.is_complete and s.run_next() is None


def test_stepping_one_post_at_a_time_gives_the_same_audit_as_run_all(tmp_path):
    stepped = session(tmp_path, "stepped.sqlite3")
    seen = []
    while (entry := stepped.run_next()) is not None:
        seen.append(entry)
        assert stepped.steps_done == len(seen) and stepped.processed_posts[-1].source_id == entry.source_id
    whole = session(tmp_path, "whole.sqlite3").run_all()
    assert [(e.source_id, e.headline, e.ledger_after) for e in seen] == [(e.source_id, e.headline, e.ledger_after) for e in whole]


# ----------------------------------------------------------------------------- idempotency and restart

def test_replaying_a_processed_post_is_rejected_and_changes_nothing(tmp_path):
    s = session(tmp_path)
    s.run_all()
    before = s.ledger()
    replayed = s.replay("d6")
    assert replayed.duplicate and replayed.matches_expectation and replayed.expected_statuses == ["DUPLICATE"]
    assert [o.status for o in replayed.outcomes] == ["SIMULATED"]      # the recorded outcome, not a new fill
    assert replayed.headline.startswith("DUPLICATE") and replayed.step == 10
    assert s.ledger() == before
    last = s.replay()                                                   # default: the last processed post
    assert last.source_id == "d9" and last.duplicate and s.ledger() == before
    summary = s.summary()
    assert summary["duplicates_rejected"] == 2 and summary["mock_fills"] == 4 and summary["expectations_met"]
    with pytest.raises(ValueError):
        session(tmp_path, "empty.sqlite3").replay()


def test_restarting_on_the_same_ledger_never_fills_twice(tmp_path):
    first = session(tmp_path)
    first.run_all()
    ledger = first.ledger()
    second = session(tmp_path)   # a new process on the same database: empty interpreter context, full ledger
    entries = second.run_all()
    assert all(e.duplicate for e in entries) and not any(e.matches_expectation for e in entries)
    assert all(e.headline.startswith("DUPLICATE") for e in entries)
    assert second.ledger() == ledger and second.summary()["mock_fills"] == 0


# ----------------------------------------------------------------------------- reconciliation

def test_reconciliation_compares_the_publisher_book_with_the_mock_ledger(tmp_path):
    s = session(tmp_path)
    assert s.reconciliation() == []
    s.run_next()
    s.run_next()
    assert s.reconciliation() == [ReconciliationRow(
        idea_id="d1:a1", publisher_state="OPEN", publisher_stop="98.00", publisher_target="108.00",
        ledger_state="OPEN", ledger_quantity=6, ledger_stop="98.00", ledger_target="108.00", agrees=True)]
    s.run_all()
    (row,) = s.reconciliation()
    assert (row.publisher_state, row.ledger_state, row.ledger_quantity, row.agrees) == ("CLOSED", "CLOSED", 0, True)


def test_a_drift_between_publisher_book_and_ledger_is_reported_not_traded(tmp_path):
    s = session(tmp_path)
    s.run_next()
    view = s.interpreter.context.ideas["d1:a1"]
    view.closed_at = view.opened_at + timedelta(seconds=1)     # the publisher said "closed"; the ledger still holds 4 units
    s._last_now = view.closed_at + timedelta(hours=1)
    (row,) = s.reconciliation()
    assert (row.publisher_state, row.ledger_state, row.ledger_quantity, row.agrees) == ("CLOSED", "OPEN", 4, False)
    assert s.summary()["ledger_agrees_with_publisher"] is False
    assert s.ledger()[0].quantity == 4                            # nothing was sold on the strength of a report


def test_reconciliation_note_names_the_operator_task():
    open_row = LedgerIdea(idea_id="x:a1", symbol="TLT", asset_class="ETF", position_side="LONG", quantity=3, status="OPEN")
    closed_row = open_row.model_copy(update={"quantity": 0, "status": "CLOSED"})
    note = DemoSession._reconciliation_note
    assert "check the broker book" in note("TLT", [open_row]) and "x:a1 (3 units)" in note("TLT", [open_row])
    assert note("TLT", [closed_row]).startswith("Consistent")
    assert "never held" in note("TLT", [])
    assert "No orderable instrument" in note(None, [open_row])


# ----------------------------------------------------------------------------- operator-readable codes

def test_every_gateway_and_interpreter_code_has_a_plain_language_explanation():
    validator = (SRC / "validator.py").read_text()
    replay = (SRC / "event_replay.py").read_text()
    interpreter = (SRC / "tkl_interpreter.py").read_text()
    codes = set(re.findall(r'_gate\("(?:APPROVE|HOLD|BLOCK|NO_TRADE|RECONCILE)",\s*"([A-Z_]+)"', validator))
    codes |= set(re.findall(r'reasons=\[\s*"([A-Z_]+)"', replay))
    codes |= set(re.findall(r'ambiguities\.append\("([A-Z_]+)"\)', interpreter))
    codes |= set(re.findall(r'return [^\n]*?, "([A-Z_]+)"\n', interpreter))
    assert {"PAPER_OPEN", "SOURCE_NOT_AUTHENTICATED", "ATOMIC_GROUP_REJECTED", "NO_OPEN_IDEA_IN_CONTEXT",
            "MULTIPLE_TICKERS_IN_CLAUSE"} <= codes and len(codes) >= 60
    assert sorted(c for c in codes if c not in REASON_EXPLANATIONS) == []
    assert all(text.strip() for text in REASON_EXPLANATIONS.values())
    assert set(STATUS_EXPLANATIONS) == {"SIMULATED", "HOLD", "BLOCK", "NO_TRADE", "RECONCILE", "DUPLICATE"}
    assert explain("HOLD") == STATUS_EXPLANATIONS["HOLD"]
    assert "SOME_FUTURE_CODE" in explain("SOME_FUTURE_CODE")       # unknown codes are still shown, never hidden


def test_fresh_database_removes_the_ledger_and_its_sidecars(tmp_path):
    base = tmp_path / "ledger.sqlite3"
    for name in ("ledger.sqlite3", "ledger.sqlite3-wal", "ledger.sqlite3-shm"):
        (tmp_path / name).write_bytes(b"x")
    fresh_database(base)
    assert not any(tmp_path.iterdir())
    fresh_database(base)                                          # idempotent on an empty directory


# ----------------------------------------------------------------------------- command line

def _cli():
    spec = importlib.util.spec_from_file_location("run_mock_tlt_demo", REPO / "scripts" / "run_mock_tlt_demo.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cli_runs_the_demo_and_reports_pass(tmp_path, capsys):
    cli = _cli()
    assert cli.main([]) == 0                                       # temporary ledger
    out = capsys.readouterr().out
    assert "MOCK / SYNTHETIC ONLY" in out and "RESULT: PASS" in out and "DUPLICATE" in out
    assert "SIMULATED BUY 4 TLT @ 100.05" in out and "Consistent: the mock ledger already shows TLT closed" in out
    assert "as expected:    yes" in out and "as expected:    NO" not in out
    db = tmp_path / "cli.sqlite3"
    assert cli.main(["--db", str(db)]) == 0 and db.exists()
    capsys.readouterr()
    assert cli.main(["--db", str(db)]) == 1                        # same ledger again: everything is a duplicate
    assert "already held" in capsys.readouterr().out
    assert cli.main(["--db", str(db), "--fresh"]) == 0
    capsys.readouterr()
    assert cli.main(["--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["passed"] and len(payload["entries"]) == 10 and payload["entries"][-1]["duplicate"]
    assert payload["summary"]["mock_fills"] == 4 and payload["reconciliation"][0]["agrees"]
    assert "MOCK" in payload["banner"]


# ----------------------------------------------------------------------------- the Streamlit page

def test_streamlit_page_drives_the_lifecycle_headlessly(tmp_path, monkeypatch):
    from streamlit.testing.v1 import AppTest

    monkeypatch.setenv("TRADINGBOT_DEMO_DB", str(tmp_path / "ui.sqlite3"))
    at = AppTest.from_file(str(REPO / "dashboard" / "pages" / "1_Mock_TLT_Lifecycle.py"), default_timeout=120)
    at.run()
    assert not at.exception
    assert any("MOCK / SYNTHETIC ONLY" in e.value for e in at.error)
    assert at.session_state["demo"].steps_done == 0

    at.button(key="run_next").click().run()
    assert not at.exception and at.session_state["demo"].steps_done == 1
    assert at.session_state["demo"].ledger()[0].quantity == 4

    at.button(key="run_all").click().run()
    assert not at.exception
    demo = at.session_state["demo"]
    assert demo.steps_done == 9 and demo.is_complete and demo.summary()["expectations_met"]
    assert any("Reconciliation" in h.value for h in at.subheader)

    at.button(key="replay").click().run()
    assert not at.exception
    assert at.session_state["demo"].entries[-1].duplicate and at.session_state["demo"].summary()["duplicates_rejected"] == 1

    at.button(key="reset").click().run()
    assert not at.exception
    assert at.session_state["demo"].steps_done == 0 and at.session_state["demo"].ledger() == []
