"""SHADOW-01 regression tests: deterministic synthetic shadow runner, checkpoint/restart convergence,
duplicate-delivery idempotency, bounded runs and checkpoint validation.

Synthetic data only (invented tickers ZQX/VBN/KPL, invented prices and timestamps). No network, no
broker, no working data: every run uses a fresh pytest temporary directory.
"""
from __future__ import annotations

import itertools
import json
import os
import re
import socket
import sqlite3
import subprocess
import sys
from contextlib import closing
from datetime import datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from tradingbot.event_replay import PaperEventReplay
from tradingbot.models import MockPolicy
from tradingbot.shadow_runner import (
    CHECKPOINT_FILE,
    LEDGER_FILE,
    NON_PRODUCTION_LABEL,
    SHADOW_EPOCH,
    InterruptSpec,
    ShadowCheckpoint,
    ShadowDelivery,
    ShadowFeed,
    ShadowInterrupted,
    ShadowPost,
    ShadowRunner,
    ShadowStateError,
    policy_digest,
    shadow_policy,
    synthetic_feed,
)

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "run_shadow_synthetic.py"
FEED = synthetic_feed()
POLICY = shadow_policy()
TOTAL = len(FEED.deliveries)

# Straight run, per delivery index: (post key, duplicate return expected, statuses of the outcomes).
EXPECTED_DELIVERIES = [
    ("e01", False, ["SIMULATED"]),    # 0 fresh OPEN long
    ("e01", True, ["SIMULATED"]),     # 1 immediate duplicate
    ("e02", False, ["HOLD"]),         # 2 conditional add
    ("e03", False, ["SIMULATED"]),    # 3 REDUCE half
    ("e03", True, ["SIMULATED"]),     # 4 late duplicate
    ("e04", False, ["NO_TRADE"]),     # 5 commentary
    ("e05", False, ["SIMULATED"]),    # 6 CLOSE remaining
    ("e01", True, ["SIMULATED"]),     # 7 OPEN replayed after the close
    ("e06", False, ["RECONCILE"]),    # 8 past-tense exit report
    ("e07", False, ["SIMULATED"]),    # 9 fresh OPEN short
    ("e08", False, ["BLOCK"]),        # 10 MODIFY delivered 20 minutes late
    ("e08", True, ["BLOCK"]),         # 11 re-delivery of the blocked MODIFY
    ("e09", False, ["SIMULATED"]),    # 12 cover CLOSE
    ("e05-v2", False, ["HOLD"]),      # 13 edited revision
    ("e10", False, ["BLOCK"]),        # 14 off-allowlist OPEN
    ("e05", True, ["BLOCK"]),         # 15 tampered re-delivery (hash changed, same identity)
    ("e09", True, ["SIMULATED"]),     # 16 late duplicate of the cover
]
EXPECTED_ORDERS = [
    ("paper-e01-1-a1", "OPEN", "BUY", 4, "41.25"),
    ("paper-e03-1-a1", "REDUCE", "SELL", 2, "43.55"),
    ("paper-e05-1-a1", "CLOSE", "SELL", 2, "44.65"),
    ("paper-e07-1-a1", "OPEN", "SELL", 4, "120.45"),
    ("paper-e09-1-a1", "CLOSE", "BUY", 4, "118.45"),
]
UNIQUE_POSTS = 11
TAMPERED = {15}
DUPLICATES = {i for i, (_, dup, _) in enumerate(EXPECTED_DELIVERIES) if dup} - TAMPERED
FRESH = set(range(TOTAL)) - DUPLICATES - TAMPERED


def ledger_rows(workdir: Path) -> tuple[int, int]:
    """Independent read of the coordinator's tables: posts and recorded action rows."""
    with closing(sqlite3.connect(str(Path(workdir) / LEDGER_FILE))) as con:
        posts = con.execute("SELECT COUNT(*) FROM replay_posts").fetchone()[0]
        actions = con.execute("SELECT COUNT(*) FROM replay_actions").fetchone()[0]
    return posts, actions


def unique_posts_in(deliveries) -> int:
    return len({(FEED.post(d.post_key).source_id, FEED.post(d.post_key).revision) for d in deliveries})


def rewrite_checkpoint(path: Path, mutate) -> None:
    data = json.loads(path.read_text("utf-8"))
    mutate(data)
    path.write_text(json.dumps(data), encoding="utf-8")


def feed_with(**changes) -> ShadowFeed:
    """A validated variant of the shipped feed (model_copy would skip validation)."""
    data = FEED.model_dump()
    data.update(changes)
    return ShadowFeed.model_validate(data)


@pytest.fixture(scope="module")
def straight(tmp_path_factory):
    """One straight (uninterrupted) run shared by the convergence tests: (workdir, report, records)."""
    workdir = tmp_path_factory.mktemp("straight") / "run"
    records = []
    report = ShadowRunner(workdir, FEED, POLICY).run(on_delivery=records.append)
    return workdir, report, records


# ----------------------------------------------------------------------------- 1. the straight run

def test_straight_run_exercises_open_reduce_close_and_hold_reconcile_block_paths(straight):
    _, report, records = straight
    assert report.label == NON_PRODUCTION_LABEL and report.status == "COMPLETE"
    assert (report.resumed_from, report.cursor, report.deliveries_total) == (0, TOTAL, TOTAL)
    assert [(r.post_key, r.result.duplicate, [o.status for o in r.result.outcomes]) for r in records] \
        == EXPECTED_DELIVERIES
    assert [(o.order_id, o.action, o.side, o.quantity, o.price) for o in report.ledger.orders] == EXPECTED_ORDERS
    assert [(i.idea_id, i.symbol, i.position_side, i.status, i.quantity) for i in report.ledger.ideas] == [
        ("e01:a1", "ZQX", "LONG", "CLOSED", 0), ("e07:a1", "VBN", "SHORT", "CLOSED", 0)]
    assert report.ledger.outcome_counts == {"BLOCK": 2, "HOLD": 2, "NO_TRADE": 1, "RECONCILE": 1, "SIMULATED": 5}
    assert report.ledger.reason_counts == {
        "COMMENTARY_OR_SUMMARY": 1, "CONDITIONAL_TRIGGER_NOT_IMPLEMENTED": 1, "HISTORICAL_REPORT_NO_ORDER": 1,
        "INSTRUMENT_NOT_ALLOWLISTED": 1, "PAPER_OPEN": 2, "PAPER_REDUCE_ONLY": 3,
        "SOURCE_EDIT_REQUIRES_REVIEW": 1, "SOURCE_STALE_OR_FUTURE": 1,
    }
    assert len(report.ledger.posts) == UNIQUE_POSTS
    # the ledger-level MODIFY never happened: the stale delivery left the short's stop untouched
    assert report.ledger.ideas[1].stop_price == "124.00"


def test_virtual_clock_drives_every_delivery_and_never_the_wall_clock(straight):
    _, _, records = straight
    assert [r.virtual_now for r in records] == [FEED.virtual_time(d.offset_s) for d in FEED.deliveries]
    assert records[0].virtual_now == datetime.fromisoformat("2026-11-06T14:00:05+00:00")
    assert all(a.virtual_now <= b.virtual_now for a, b in itertools.pairwise(records))
    # the late MODIFY is stale by the virtual clock alone (20 minutes after a 6-hour-old publication)
    late = records[10]
    assert (late.virtual_now - late.post.published_at).total_seconds() == 20 * 60 + 5
    assert late.result.outcomes[0].reasons == ["SOURCE_STALE_OR_FUTURE"]


def test_two_fresh_runs_produce_byte_identical_reports(tmp_path, straight):
    _, first, _ = straight
    second = ShadowRunner(tmp_path / "again", FEED, POLICY).run()
    assert second.model_dump_json() == first.model_dump_json()
    assert first.feed_digest == FEED.digest() and first.policy_digest == policy_digest(POLICY)


# ----------------------------------------------------------------------------- 2. duplicates never fill

def test_duplicate_late_replayed_and_tampered_redeliveries_add_no_orders_or_rows(straight):
    workdir, report, records = straight
    stats = report.this_run
    assert (stats.processed, stats.fresh_results, stats.duplicate_returns, stats.hash_mismatch_blocks) \
        == (TOTAL, len(FRESH), len(DUPLICATES), len(TAMPERED))
    assert ledger_rows(workdir) == (UNIQUE_POSTS, UNIQUE_POSTS)   # one row per unique post; duplicates appended nothing
    assert len(report.ledger.orders) == 5
    first_outcomes = {}
    for r in records:
        identity = (r.post.source_id, r.post.revision)
        if r.index in FRESH:
            first_outcomes[identity] = r.result.outcomes
        elif r.index in DUPLICATES:
            assert r.result.duplicate and r.result.outcomes == first_outcomes[identity]   # recorded, verbatim
    tampered = records[15]
    assert tampered.post.text != FEED.post("e05").text
    assert tampered.result.duplicate
    assert tampered.result.outcomes[0].reasons == ["SOURCE_HASH_CHANGED_WITHOUT_REVISION"]
    # the OPEN replayed after its position was closed did not reopen it
    assert records[7].result.duplicate and report.ledger.ideas[0].status == "CLOSED"
    # the re-delivered blocked MODIFY was not retried into a fill (no blind retry)
    assert records[11].result.duplicate and records[11].result.outcomes[0].status == "BLOCK"


def test_positive_control_a_genuinely_new_post_does_change_the_ledger(tmp_path):
    """The idempotency assertions above are not vacuous: a new identity with new content fills."""
    control = ShadowPost(key="ctl", source_id="ctl", published_offset_s=10 * 60,
                         text="We are now long $VBN at $118.00. Stop: $115.00. Target: $125.00.",
                         quotes={"VBN": "118.00"})
    deliveries = [*FEED.deliveries[:2], ShadowDelivery(post_key="ctl", offset_s=10 * 60 + 5, note="control")]
    feed = feed_with(posts=[*FEED.model_dump()["posts"], control.model_dump()],
                     deliveries=[d.model_dump() for d in deliveries])
    records = []
    report = ShadowRunner(tmp_path / "control", feed, POLICY).run(on_delivery=records.append)
    assert [r.result.duplicate for r in records] == [False, True, False]
    assert [i.idea_id for i in report.ledger.ideas] == ["ctl:a1", "e01:a1"]
    assert len(report.ledger.orders) == 2 and ledger_rows(tmp_path / "control") == (2, 2)


def test_positive_control_an_on_time_modify_is_amended_under_the_virtual_clock(tmp_path, straight):
    """Only the delivery offset changes; the stale BLOCK becomes a SIMULATED_AMENDED and the ledger differs."""
    _, baseline, _ = straight
    deliveries = [d.model_dump() for d in FEED.deliveries]
    assert deliveries[10]["post_key"] == "e08"
    deliveries[10]["offset_s"] = 360 * 60 + 5
    report = ShadowRunner(tmp_path / "on-time", feed_with(deliveries=deliveries), POLICY).run()
    e08 = next(p for p in report.ledger.posts if p.source_id == "e08")
    assert e08.statuses == ["SIMULATED"] and e08.reasons == [["PAPER_PROTECTION_AMENDMENT"]]
    amendment = next(o for o in report.ledger.orders if o.order_id == "paper-e08-1-a1")
    assert (amendment.action, amendment.side, amendment.quantity, amendment.status) == ("MODIFY", "NONE", 0, "SIMULATED_AMENDED")
    assert report.ledger.ideas[1].stop_price == "122.00" and report.ledger.ideas[1].status == "CLOSED"
    assert report.convergence_digest != baseline.convergence_digest


# ----------------------------------------------------------------------------- 3. restart proof

@pytest.mark.parametrize("phase", ["before_commit", "after_commit"])
@pytest.mark.parametrize("index", range(TOTAL))
def test_interrupt_at_every_delivery_boundary_then_resume_converges(tmp_path, straight, index, phase):
    straight_dir, baseline, _ = straight
    work = tmp_path / "interrupted"
    with pytest.raises(ShadowInterrupted) as info:
        ShadowRunner(work, FEED, POLICY).run(interrupt=InterruptSpec(index, phase))
    assert (info.value.delivery_index, info.value.phase, info.value.checkpoint_cursor) == (index, phase, index)
    # The checkpoint never runs ahead of a commit ...
    assert ShadowCheckpoint.model_validate_json((work / CHECKPOINT_FILE).read_text()).cursor == index
    # ... and the interruption window is real: after_commit leaves the ledger one delivery ahead of it.
    committed = unique_posts_in(FEED.deliveries[:index + (1 if phase == "after_commit" else 0)])
    assert ledger_rows(work)[0] == committed

    records = []
    report = ShadowRunner(work, FEED, POLICY, resume=True).run(on_delivery=records.append)
    assert (report.status, report.resumed_from, report.cursor) == ("COMPLETE", index, TOTAL)
    assert report.ledger == baseline.ledger
    assert report.convergence_digest == baseline.convergence_digest
    assert ledger_rows(work) == ledger_rows(straight_dir) == (UNIQUE_POSTS, UNIQUE_POSTS)
    # At-least-once accounting: the re-delivered event is a duplicate exactly when its first delivery committed.
    redelivered_fresh = phase == "after_commit" and index in FRESH
    assert records[0].result.duplicate == (redelivered_fresh or index not in FRESH)
    assert report.this_run.duplicate_returns == len(DUPLICATES & set(range(index, TOTAL))) + int(redelivered_fresh)
    assert report.this_run.hash_mismatch_blocks == len(TAMPERED & set(range(index, TOTAL)))
    assert report.this_run.processed == TOTAL - index


def test_delivery_cap_and_wall_budget_pause_the_run_and_resume_converges(tmp_path, straight):
    _, baseline, _ = straight
    work = tmp_path / "bounded"
    capped = ShadowRunner(work, FEED, POLICY).run(max_deliveries=5)
    assert (capped.status, capped.cursor, capped.this_run.processed) == ("DELIVERY_CAP_REACHED", 5, 5)
    assert ShadowCheckpoint.model_validate_json((work / CHECKPOINT_FILE).read_text()).cursor == 5
    fake_wall = itertools.chain([0.0, 0.5, 1.0], itertools.repeat(100.0))   # budget 10 s: two deliveries fit
    budgeted = ShadowRunner(work, FEED, POLICY, resume=True, wall_clock=lambda: next(fake_wall)).run(max_wall_seconds=10)
    assert (budgeted.status, budgeted.resumed_from, budgeted.cursor, budgeted.this_run.processed) \
        == ("WALL_BUDGET_EXHAUSTED", 5, 7, 2)
    final = ShadowRunner(work, FEED, POLICY, resume=True).run()
    assert (final.status, final.resumed_from, final.cursor) == ("COMPLETE", 7, TOTAL)
    assert final.ledger == baseline.ledger and final.convergence_digest == baseline.convergence_digest
    # a cap met exactly at the end of the finite feed is a completion, and bounds must be positive
    assert ShadowRunner(tmp_path / "exact", FEED, POLICY).run(max_deliveries=TOTAL).status == "COMPLETE"
    with pytest.raises(ValueError):
        ShadowRunner(tmp_path / "zero", FEED, POLICY).run(max_deliveries=0)
    with pytest.raises(ValueError):
        ShadowRunner(tmp_path / "negative", FEED, POLICY).run(max_wall_seconds=0)


def test_resuming_a_completed_run_delivers_nothing_and_keeps_the_ledger(straight):
    workdir, baseline, _ = straight
    again = ShadowRunner(workdir, FEED, POLICY, resume=True).run()
    assert (again.status, again.resumed_from, again.cursor, again.this_run.processed) == ("COMPLETE", TOTAL, TOTAL, 0)
    assert again.ledger == baseline.ledger


# ----------------------------------------------------------------------------- 4. refusals and validation

def test_refuses_existing_data_and_missing_state(tmp_path):
    occupied = tmp_path / "occupied"
    occupied.mkdir()
    (occupied / "someone_elses.sqlite3").write_bytes(b"")
    with pytest.raises(ShadowStateError, match="non-empty"):
        ShadowRunner(occupied, FEED, POLICY)
    assert [p.name for p in occupied.iterdir()] == ["someone_elses.sqlite3"]   # nothing was created or touched
    with pytest.raises(ShadowStateError, match="no checkpoint"):
        ShadowRunner(tmp_path / "void", FEED, POLICY, resume=True)
    done = tmp_path / "done"
    ShadowRunner(done, FEED, POLICY).run()
    with pytest.raises(ShadowStateError, match="non-empty"):
        ShadowRunner(done, FEED, POLICY)                      # a finished run is never silently restarted
    (done / LEDGER_FILE).unlink()
    with pytest.raises(ShadowStateError, match="no ledger"):
        ShadowRunner(done, FEED, POLICY, resume=True)
    not_a_dir = tmp_path / "file"
    not_a_dir.write_text("x")
    with pytest.raises(ShadowStateError, match="not a directory"):
        ShadowRunner(not_a_dir, FEED, POLICY)


@pytest.mark.parametrize("name, mutate, message", [
    ("cursor_beyond_feed", lambda d: d.__setitem__("cursor", TOTAL + 1), "exceeds"),
    ("negative_cursor", lambda d: d.__setitem__("cursor", -1), "invalid checkpoint"),
    ("cursor_not_int", lambda d: d.__setitem__("cursor", "5"), "invalid checkpoint"),
    ("feed_digest", lambda d: d.__setitem__("feed_digest", "0" * 64), "different feed"),
    ("feed_digest_malformed", lambda d: d.__setitem__("feed_digest", "xyz"), "invalid checkpoint"),
    ("feed_id", lambda d: d.__setitem__("feed_id", "another-feed"), "different feed"),
    ("policy_digest", lambda d: d.__setitem__("policy_digest", "f" * 64), "different mock policy"),
    ("schema_version", lambda d: d.__setitem__("schema_version", "shadow-checkpoint-0"), "invalid checkpoint"),
    ("not_mock_only", lambda d: d.__setitem__("mock_only", False), "invalid checkpoint"),
    ("extra_field", lambda d: d.__setitem__("live_routing", True), "invalid checkpoint"),
    ("missing_field", lambda d: d.pop("cursor"), "invalid checkpoint"),
    ("ledger_file", lambda d: d.__setitem__("ledger_file", "../elsewhere.sqlite3"), "unexpected ledger file"),
])
def test_checkpoint_inputs_are_validated_before_any_resume(tmp_path, name, mutate, message):
    work = tmp_path / "run"
    ShadowRunner(work, FEED, POLICY).run(max_deliveries=5)
    rewrite_checkpoint(work / CHECKPOINT_FILE, mutate)
    with pytest.raises(ShadowStateError, match=message):
        ShadowRunner(work, FEED, POLICY, resume=True)


def test_malformed_checkpoint_changed_policy_changed_feed_and_lost_ledger_are_refused(tmp_path):
    work = tmp_path / "run"
    ShadowRunner(work, FEED, POLICY).run(max_deliveries=5)
    with pytest.raises(ShadowStateError, match="different mock policy"):
        ShadowRunner(work, FEED, MockPolicy(quantity=5, allowed_symbols=frozenset({"ZQX", "VBN"})), resume=True)
    with pytest.raises(ShadowStateError, match="different feed"):
        ShadowRunner(work, feed_with(half_spread="0.06"), POLICY, resume=True)
    # the ledger replaced by an empty database: the checkpoint cursor is ahead of what was committed
    (work / LEDGER_FILE).unlink()
    PaperEventReplay(work / LEDGER_FILE, POLICY)
    with pytest.raises(ShadowStateError, match="ahead of the ledger"):
        ShadowRunner(work, FEED, POLICY, resume=True)
    (work / CHECKPOINT_FILE).write_text("{not json", encoding="utf-8")
    with pytest.raises(ShadowStateError, match="invalid checkpoint"):
        ShadowRunner(work, FEED, POLICY, resume=True)


def test_checkpoint_file_is_strict_and_atomic(tmp_path):
    work = tmp_path / "run"
    runner = ShadowRunner(work, FEED, POLICY)
    assert runner.read_checkpoint().cursor == 0 and runner.read_checkpoint().virtual_now is None
    runner.run(max_deliveries=3)
    checkpoint = runner.read_checkpoint()
    assert checkpoint.cursor == 3 and checkpoint.virtual_now == FEED.virtual_time(FEED.deliveries[2].offset_s)
    assert checkpoint.mock_only is True and checkpoint.ledger_file == LEDGER_FILE
    assert sorted(p.name for p in work.iterdir()) == [CHECKPOINT_FILE, LEDGER_FILE]   # no temp file left behind
    with pytest.raises(ValidationError):
        ShadowCheckpoint(feed_id="x", feed_digest="0" * 64, policy_digest="0" * 64, ledger_file=LEDGER_FILE,
                         cursor=1, unexpected=True)


def test_feed_contract_rejects_inconsistent_synthetic_inputs():
    post = ShadowPost(key="p", source_id="p", published_offset_s=100, text="synthetic", quotes={"ZQX": "10"})

    def feed(deliveries, posts=(post,), epoch=SHADOW_EPOCH):
        return ShadowFeed(feed_id="t", epoch=epoch, posts=list(posts), deliveries=deliveries)

    ok = feed([ShadowDelivery(post_key="p", offset_s=100)])
    assert ok.digest() == ShadowFeed.model_validate(ok.model_dump()).digest()
    assert ok.digest() == ShadowFeed.model_validate_json(ok.model_dump_json()).digest()
    with pytest.raises(ValidationError, match="unknown post"):
        feed([ShadowDelivery(post_key="q", offset_s=100)])
    with pytest.raises(ValidationError, match="precedes the availability"):
        feed([ShadowDelivery(post_key="p", offset_s=99)])
    with pytest.raises(ValidationError, match="back in virtual time"):
        feed([ShadowDelivery(post_key="p", offset_s=200), ShadowDelivery(post_key="p", offset_s=150)])
    with pytest.raises(ValidationError, match="timezone"):
        feed([ShadowDelivery(post_key="p", offset_s=100)], epoch=datetime(2026, 1, 1))
    with pytest.raises(ValidationError, match="unique"):
        feed([ShadowDelivery(post_key="p", offset_s=100)], posts=(post, post))
    with pytest.raises(ValidationError, match="revised_offset_s"):
        ShadowPost(key="p2", source_id="p", revision=2, published_offset_s=0, text="synthetic")
    with pytest.raises(ValidationError, match="revised_offset_s"):
        ShadowPost(key="p3", source_id="p", published_offset_s=0, revised_offset_s=5, text="synthetic")
    with pytest.raises(ValidationError, match="decimal"):
        ShadowPost(key="p4", source_id="p4", published_offset_s=0, text="synthetic", quotes={"ZQX": "ten"})
    with pytest.raises(ValidationError, match="positive"):
        ShadowPost(key="p5", source_id="p5", published_offset_s=0, text="synthetic", quotes={"ZQX": "-1"})
    with pytest.raises(ValidationError):
        ShadowDelivery(post_key="p", offset_s=100, broker="x")


def test_no_network_socket_is_ever_opened(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("the shadow runner must not open network sockets")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    assert ShadowRunner(tmp_path / "offline", FEED, POLICY).run().status == "COMPLETE"


# ----------------------------------------------------------------------------- 5. the documented command

def cli(*args: str, seed: str = "0", tmpdir: Path | None = None) -> subprocess.CompletedProcess:
    """Run the documented command. ``tmpdir`` redirects the script's default temporary directory
    (via TMPDIR) under pytest's tmp_path so anything it preserves is cleaned up with the test."""
    env = {**os.environ, "PYTHONHASHSEED": seed,
           "PYTHONPATH": str(REPO / "src") + os.pathsep + os.environ.get("PYTHONPATH", "")}
    if tmpdir is not None:
        env["TMPDIR"] = str(tmpdir)
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True, cwd=REPO,
                          env=env, check=False)


def test_cli_fresh_run_simulated_crash_and_resume_converge_across_processes(tmp_path):
    fresh = cli("--json", str(tmp_path / "fresh.json"), seed="1")
    assert fresh.returncode == 0, fresh.stderr[-800:]
    assert NON_PRODUCTION_LABEL in fresh.stdout and "status: COMPLETE  cursor: 17/17" in fresh.stdout
    assert "mock orders (5)" in fresh.stdout and "[01] 14:00:08 e01    DUPLICATE" in fresh.stdout
    work = tmp_path / "work"
    crashed = cli("--workdir", str(work), "--interrupt-at", "6", "--interrupt-phase", "after-commit", seed="2")
    assert crashed.returncode == 3 and "INTERRUPTED (simulated crash)" in crashed.stdout
    assert ShadowCheckpoint.model_validate_json((work / CHECKPOINT_FILE).read_text()).cursor == 6
    paused = cli("--workdir", str(work), "--resume", "--max-deliveries", "3", seed="3")
    assert paused.returncode == 4 and "paused (DELIVERY_CAP_REACHED)" in paused.stdout
    resumed = cli("--workdir", str(work), "--resume", "--json", str(tmp_path / "resumed.json"), seed="4")
    assert resumed.returncode == 0, resumed.stderr[-800:]
    a = json.loads((tmp_path / "fresh.json").read_text())
    b = json.loads((tmp_path / "resumed.json").read_text())
    for key in ("feed_digest", "policy_digest", "convergence_digest", "ledger", "deliveries_total"):
        assert a[key] == b[key], key   # identical across processes and hash seeds
    assert (a["resumed_from"], b["resumed_from"], b["cursor"]) == (0, 9, TOTAL)
    assert a["this_run"]["duplicate_returns"] == len(DUPLICATES)
    assert b["this_run"]["duplicate_returns"] == len(DUPLICATES & set(range(9, TOTAL)))


def test_cli_paused_run_without_workdir_preserves_state_and_its_advertised_resume_succeeds(tmp_path, straight):
    _, baseline, _ = straight
    paused = cli("--max-deliveries", "5", tmpdir=tmp_path)
    assert paused.returncode == 4, paused.stderr[-800:]
    assert "paused (DELIVERY_CAP_REACHED)" in paused.stdout
    advertised = re.search(r"resume with: --workdir (\S+) --resume", paused.stdout)
    assert advertised is not None
    preserved = Path(advertised.group(1))
    assert f"temporary workdir preserved for --resume: {preserved}" in paused.stdout
    assert preserved.is_dir() and preserved.is_relative_to(tmp_path)
    assert sorted(p.name for p in preserved.iterdir()) == [CHECKPOINT_FILE, LEDGER_FILE]
    assert ShadowCheckpoint.model_validate_json((preserved / CHECKPOINT_FILE).read_text()).cursor == 5
    resumed = cli("--workdir", str(preserved), "--resume", "--json", str(tmp_path / "resumed.json"), tmpdir=tmp_path)
    assert resumed.returncode == 0, resumed.stderr[-800:]
    report = json.loads((tmp_path / "resumed.json").read_text())
    assert (report["status"], report["resumed_from"], report["cursor"]) == ("COMPLETE", 5, TOTAL)
    assert report["convergence_digest"] == baseline.convergence_digest
    # a default-mode run that completes leaves nothing behind
    completed = cli("--quiet", tmpdir=tmp_path)
    assert completed.returncode == 0, completed.stderr[-800:]
    removed = re.search(r"temporary workdir removed: (\S+)", completed.stdout)
    assert removed is not None and not Path(removed.group(1)).exists()


def test_cli_refuses_an_occupied_workdir_and_requires_workdir_for_resume(tmp_path):
    occupied = tmp_path / "occupied"
    occupied.mkdir()
    (occupied / "state.sqlite3").write_bytes(b"")
    refused = cli("--workdir", str(occupied))
    assert refused.returncode == 2 and "REFUSED" in refused.stderr and "non-empty" in refused.stderr
    assert [p.name for p in occupied.iterdir()] == ["state.sqlite3"]
    usage = cli("--resume")
    assert usage.returncode == 2 and "--workdir" in usage.stderr
    usage = cli("--interrupt-at", "3")
    assert usage.returncode == 2 and "--workdir" in usage.stderr
