"""SHADOW-02 regression tests: bounded synthetic multi-cycle soak and repeated fresh-process restarts.

Synthetic data only (the public SHADOW-01 fixture with invented tickers ZQX/VBN/KPL, repeated with
disjoint source identities). No network, no broker, no working data: every run uses a fresh pytest
temporary directory, and the soak processes are real child interpreters with their PYTHONPATH pinned
to this worktree's ``src``. The exhaustive single-run boundary matrix lives in test_shadow_runner.py
and is not duplicated here; these tests target cumulative, multi-process behaviour.
"""
from __future__ import annotations

import itertools
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
from contextlib import closing
from datetime import timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

import tradingbot
from tradingbot.shadow_runner import (
    CHECKPOINT_FILE,
    LEDGER_FILE,
    ShadowCheckpoint,
    ShadowRunner,
    ShadowRunReport,
    ShadowStateError,
    shadow_policy,
    synthetic_feed,
)
from tradingbot.shadow_soak import (
    DEFAULT_CYCLE_PERIOD_S,
    EXIT_INTERRUPTED,
    EXIT_OK,
    EXIT_PAUSED,
    MAX_CYCLES,
    MAX_RESTARTS,
    MAX_TOTAL_WALL_SECONDS,
    PROFILES,
    REPORT_SCHEMA,
    SOAK_LABEL,
    SOAK_REPORT_FILE,
    NetworkRefused,
    SegmentOutcome,
    SoakProfile,
    SoakReport,
    SoakStateError,
    base_span_s,
    build_soak_feed,
    cycle_mismatches,
    cycle_of,
    expected_cycle_ideas,
    expected_cycle_orders,
    expected_totals,
    import_origin,
    launch_subprocess,
    make_plan,
    measure_ledger,
    network_guard_active,
    no_network,
    prefix_unique_counts,
    run_soak,
    segment_argv,
)

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"
SCRIPT = REPO / "scripts" / "run_shadow_soak.py"
SHADOW_01_SCRIPT = REPO / "scripts" / "run_shadow_synthetic.py"
BASE = synthetic_feed()
POLICY = shadow_policy()
CI = PROFILES["ci"]
PERIOD = DEFAULT_CYCLE_PERIOD_S
FEED3 = build_soak_feed(3)

# Independent restatement of the SHADOW-01 specification (docs/SHADOW_01_SYNTHETIC_SHADOW_RUNNER.md),
# so the module's expectation constants are checked against a second source, not against themselves.
SPEC_DELIVERIES, SPEC_UNIQUE_POSTS, SPEC_IDEAS = 17, 11, 2
SPEC_ORDERS = 5
SPEC_OPEN_FILLS, SPEC_OPEN_QTY = 2, 4 + 4
SPEC_REDUCE_FILLS, SPEC_REDUCE_QTY = 1, 2
SPEC_CLOSE_FILLS, SPEC_CLOSE_QTY = 2, 2 + 4
SPEC_STATUSES = ["SIMULATED", "SIMULATED", "HOLD", "SIMULATED", "SIMULATED", "NO_TRADE", "SIMULATED", "SIMULATED",
                 "RECONCILE", "SIMULATED", "BLOCK", "BLOCK", "SIMULATED", "HOLD", "BLOCK", "BLOCK", "SIMULATED"]
SPEC_DUPLICATES = {1, 4, 7, 11, 16}
SPEC_TAMPERED = 15
SPEC_KPL_INDEX = 14
SPEC_FIRST_CYCLE_OUTCOMES = {"BLOCK": 2, "HOLD": 2, "NO_TRADE": 1, "RECONCILE": 1, "SIMULATED": 5}
EXIT_FOR_PLAN = {"pause": EXIT_PAUSED, "interrupt_before_commit": EXIT_INTERRUPTED,
                 "interrupt_after_commit": EXIT_INTERRUPTED, "complete": EXIT_OK}


def ledger_rows(workdir: Path) -> tuple[int, int, int]:
    with closing(sqlite3.connect(str(workdir / LEDGER_FILE))) as con:
        return tuple(con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                     for table in ("replay_posts", "replay_actions", "replay_ideas"))


def cli(script: Path, *args: str, tmpdir: Path | None = None, seed: str = "0") -> subprocess.CompletedProcess:
    """Run a documented command as a child process with the import origin pinned to this worktree."""
    env = {**os.environ, "PYTHONHASHSEED": seed,
           "PYTHONPATH": str(SRC) + os.pathsep + os.environ.get("PYTHONPATH", "")}
    if tmpdir is not None:
        env["TMPDIR"] = str(tmpdir)
    return subprocess.run([sys.executable, str(script), *args], capture_output=True, text=True, cwd=REPO,
                          env=env, check=False)


@pytest.fixture(scope="module")
def straight(tmp_path_factory):
    """One in-process straight run of the 3-cycle feed: (workdir, report, records)."""
    workdir = tmp_path_factory.mktemp("straight") / "run"
    records = []
    report = ShadowRunner(workdir, FEED3, POLICY).run(on_delivery=records.append)
    return workdir, report, records


@pytest.fixture(scope="module")
def soak(tmp_path_factory):
    """One fast-profile soak with REAL child processes, shared by the convergence tests:
    (root, report, outcomes seen through the callback)."""
    root = tmp_path_factory.mktemp("soak") / "root"
    seen: list[SegmentOutcome] = []
    with no_network():
        report = run_soak(CI, root, on_segment=seen.append)
    return root, report, seen


# ----------------------------------------------------------------------------- 0. import origin

def test_tests_run_against_this_worktree_not_the_venv_editable_install():
    assert import_origin() == SRC
    assert Path(tradingbot.__file__).resolve().parent == SRC / "tradingbot"


# ----------------------------------------------------------------------------- 1. the multi-cycle feed

def test_feed_repeats_the_public_fixture_with_disjoint_identities_on_a_shifted_virtual_timeline():
    assert (len(FEED3.posts), len(FEED3.deliveries)) == (3 * SPEC_UNIQUE_POSTS, 3 * SPEC_DELIVERIES)
    assert len({p.source_id for p in FEED3.posts}) == 3 * len({p.source_id for p in BASE.posts})
    for cycle in range(3):
        shift = cycle * PERIOD
        posts = FEED3.posts[cycle * SPEC_UNIQUE_POSTS:(cycle + 1) * SPEC_UNIQUE_POSTS]
        for soak_post, base_post in zip(posts, BASE.posts, strict=True):
            assert soak_post.key == f"c{cycle:03d}-{base_post.key}"
            assert soak_post.source_id == f"c{cycle:03d}-{base_post.source_id}"
            assert (soak_post.text, soak_post.quotes, soak_post.revision) == (base_post.text, base_post.quotes, base_post.revision)
            assert soak_post.published_offset_s == base_post.published_offset_s + shift
            assert soak_post.revised_offset_s == (None if base_post.revised_offset_s is None
                                                  else base_post.revised_offset_s + shift)
        deliveries = FEED3.deliveries[cycle * SPEC_DELIVERIES:(cycle + 1) * SPEC_DELIVERIES]
        for soak_delivery, base_delivery in zip(deliveries, BASE.deliveries, strict=True):
            assert soak_delivery.post_key == f"c{cycle:03d}-{base_delivery.post_key}"
            assert soak_delivery.offset_s == base_delivery.offset_s + shift
            assert (soak_delivery.text_override, soak_delivery.note) == (base_delivery.text_override, base_delivery.note)
        assert deliveries[SPEC_TAMPERED].text_override is not None     # the tampered re-delivery survives in every cycle
    assert all(a.offset_s <= b.offset_s for a, b in itertools.pairwise(FEED3.deliveries))
    assert FEED3.epoch == BASE.epoch and FEED3.half_spread == BASE.half_spread
    assert FEED3.feed_id == f"shadow-02-soak-003c-{PERIOD}s-{BASE.feed_id}"
    assert [cycle_of(k) for k in ("c000-e01", "c017-e05-v2", "paper-c003-e01-1-a1", "e01")] == [0, 17, None, None]


def test_feed_identity_is_deterministic_and_distinguishes_cycle_counts_and_periods():
    assert build_soak_feed(3).digest() == FEED3.digest()
    two = build_soak_feed(2)
    assert two.feed_id != FEED3.feed_id and two.digest() != FEED3.digest() != BASE.digest()
    assert build_soak_feed(3, cycle_period_s=PERIOD + 60).digest() != FEED3.digest()
    one = build_soak_feed(1)
    assert len(one.deliveries) == SPEC_DELIVERIES and one.digest() != BASE.digest()   # identities differ, content does not
    assert [d.offset_s for d in one.deliveries] == [d.offset_s for d in BASE.deliveries]
    counts = prefix_unique_counts(one)
    assert counts[0] == 0 and counts[1] == counts[2] == 1 and counts[SPEC_DELIVERIES] == SPEC_UNIQUE_POSTS
    assert all(a <= b <= a + 1 for a, b in itertools.pairwise(counts))


def test_feed_rejects_overlapping_cycles_and_out_of_range_cycle_counts():
    span = base_span_s(BASE)
    assert span == 700 * 60 < PERIOD
    with pytest.raises(ValueError, match="never overlap"):
        build_soak_feed(2, cycle_period_s=span)
    assert len(build_soak_feed(2, cycle_period_s=span + 1).deliveries) == 2 * SPEC_DELIVERIES
    for cycles in (0, MAX_CYCLES + 1):
        with pytest.raises(ValueError, match="cycles"):
            build_soak_feed(cycles)


# ----------------------------------------------------------------------------- 2. independent expectations

def test_expected_totals_for_one_cycle_restate_the_shadow_01_specification():
    one = expected_totals(1)
    assert (one.orders, one.open_fills, one.open_quantity) == (SPEC_ORDERS, SPEC_OPEN_FILLS, SPEC_OPEN_QTY)
    assert (one.reduce_fills, one.reduce_quantity) == (SPEC_REDUCE_FILLS, SPEC_REDUCE_QTY)
    assert (one.close_fills, one.close_quantity, one.amendments) == (SPEC_CLOSE_FILLS, SPEC_CLOSE_QTY, 0)
    assert (one.idea_rows, one.closed_ideas, one.open_position_quantity) == (SPEC_IDEAS, SPEC_IDEAS, 0)
    assert (one.post_rows, one.action_rows) == (SPEC_UNIQUE_POSTS, SPEC_UNIQUE_POSTS)
    assert one.outcome_counts == SPEC_FIRST_CYCLE_OUTCOMES
    assert one.reason_counts["INSTRUMENT_NOT_ALLOWLISTED"] == 1 and "SOURCE_AMBIGUITIES" not in one.reason_counts
    assert expected_cycle_orders(0) == [
        ("paper-c000-e01-1-a1", "c000-e01:a1", "OPEN", "BUY", 4, "41.25"),
        ("paper-c000-e03-1-a1", "c000-e01:a1", "REDUCE", "SELL", 2, "43.55"),
        ("paper-c000-e05-1-a1", "c000-e01:a1", "CLOSE", "SELL", 2, "44.65"),
        ("paper-c000-e07-1-a1", "c000-e07:a1", "OPEN", "SELL", 4, "120.45"),
        ("paper-c000-e09-1-a1", "c000-e07:a1", "CLOSE", "BUY", 4, "118.45"),
    ]
    assert expected_cycle_ideas(2) == [("c002-e01:a1", "ZQX", "LONG", "CLOSED", 0, "39.80", "45.00"),
                                       ("c002-e07:a1", "VBN", "SHORT", "CLOSED", 0, "124.00", "112.00")]


def test_expected_totals_scale_per_cycle_with_the_documented_cumulative_context_exception():
    four = expected_totals(4)
    assert (four.orders, four.open_fills, four.reduce_fills, four.close_fills) == (20, 8, 4, 8)
    assert (four.open_quantity, four.reduce_quantity, four.close_quantity) == (32, 8, 24)
    assert (four.idea_rows, four.closed_ideas, four.open_position_quantity, four.post_rows) == (8, 8, 0, 44)
    # positive fills, NO_TRADE and RECONCILE scale linearly; the KPL OPEN is BLOCK once, then HOLD
    assert four.outcome_counts == {"BLOCK": 2 + 3 * 1, "HOLD": 2 + 3 * 3, "NO_TRADE": 4, "RECONCILE": 4, "SIMULATED": 20}
    assert four.reason_counts["INSTRUMENT_NOT_ALLOWLISTED"] == 1
    assert four.reason_counts["SOURCE_AMBIGUITIES"] == four.reason_counts["EXISTING_OPEN_IDEA_SAME_SIDE_IN_CONTEXT"] == 3
    assert four.reason_counts["PAPER_OPEN"] == 8 and four.reason_counts["PAPER_REDUCE_ONLY"] == 12


# ----------------------------------------------------------------------------- 3. cumulative semantics (in-process)

def test_every_cycle_repeats_the_fixture_statuses_duplicates_and_virtual_times(straight):
    _, report, records = straight
    assert report.status == "COMPLETE" and len(records) == 3 * SPEC_DELIVERIES
    for cycle in range(3):
        cycle_records = records[cycle * SPEC_DELIVERIES:(cycle + 1) * SPEC_DELIVERIES]
        expected = list(SPEC_STATUSES)
        if cycle >= 1:
            expected[SPEC_KPL_INDEX] = "HOLD"
        assert [[o.status for o in r.result.outcomes] for r in cycle_records] == [[s] for s in expected], cycle
        assert [r.result.duplicate for r in cycle_records] == [i in SPEC_DUPLICATES | {SPEC_TAMPERED} for i in range(17)]
        tampered = cycle_records[SPEC_TAMPERED]
        assert tampered.result.outcomes[0].reasons == ["SOURCE_HASH_CHANGED_WITHOUT_REVISION"]
        assert [r.virtual_now for r in cycle_records] == \
            [BASE.virtual_time(d.offset_s) + timedelta(seconds=cycle * PERIOD) for d in BASE.deliveries]
        assert all(r.post.source_id.startswith(f"c{cycle:03d}-") for r in cycle_records)


def test_orders_and_positions_per_cycle_match_the_independent_expectation_with_coherent_references(straight):
    workdir, report, _ = straight
    assert [(o.order_id, o.idea_id, o.action, o.side, o.quantity, o.price) for o in report.ledger.orders] == \
        [order for cycle in range(3) for order in expected_cycle_orders(cycle)]
    assert all(cycle_of(o.order_id.removeprefix("paper-")) == cycle_of(o.idea_id) for o in report.ledger.orders)
    assert [(i.idea_id, i.symbol, i.position_side, i.status, i.quantity, i.stop_price, i.target_price)
            for i in report.ledger.ideas] == [idea for cycle in range(3) for idea in expected_cycle_ideas(cycle)]
    assert ledger_rows(workdir) == (3 * SPEC_UNIQUE_POSTS, 3 * SPEC_UNIQUE_POSTS, 3 * SPEC_IDEAS)
    measured = measure_ledger(workdir, 3)
    assert measured == expected_totals(3)
    assert (measured.orders, measured.open_quantity, measured.reduce_quantity, measured.close_quantity) == (15, 24, 6, 18)
    assert cycle_mismatches(workdir, 3) == []
    # the runner's own report agrees with the independent measurement (two routes, one ledger)
    assert report.ledger.outcome_counts == measured.outcome_counts
    assert report.ledger.reason_counts == measured.reason_counts


def test_kpl_open_is_blocked_in_the_first_cycle_and_held_as_ambiguous_afterwards_never_filled(straight):
    """Cumulative-context behaviour pinned, not hidden: the interpreter's publisher-book view keeps the
    first cycle's KPL long open (the publisher never exited it; the gateway only blocked the fill), so
    later identical entries are held as ambiguous before the allowlist is consulted. Existing policy;
    no order either way."""
    workdir, report, records = straight
    kpl = [r for r in records if r.post_key.endswith("-e10")]
    assert [o.status for r in kpl for o in r.result.outcomes] == ["BLOCK", "HOLD", "HOLD"]
    assert kpl[0].result.outcomes[0].reasons == ["INSTRUMENT_NOT_ALLOWLISTED"]
    for later in kpl[1:]:
        assert later.result.outcomes[0].reasons == ["SOURCE_AMBIGUITIES", "EXISTING_OPEN_IDEA_SAME_SIDE_IN_CONTEXT"]
        assert not later.result.duplicate                       # a genuinely new post, evaluated and recorded
    assert not any(o.symbol == "KPL" for o in report.ledger.orders)
    assert not any(i.symbol == "KPL" for i in report.ledger.ideas)
    with closing(sqlite3.connect(str(workdir / LEDGER_FILE))) as con:
        assert con.execute("SELECT COUNT(*) FROM replay_ideas WHERE symbol='KPL'").fetchone()[0] == 0


def test_measurement_is_read_only_and_detects_ledger_tampering_the_digest_route_would_miss(straight, tmp_path):
    workdir, _, _ = straight
    copy = tmp_path / "copy"
    copy.mkdir()
    shutil.copy(workdir / LEDGER_FILE, copy / LEDGER_FILE)
    assert measure_ledger(copy, 3) == expected_totals(3) and cycle_mismatches(copy, 3) == []
    with closing(sqlite3.connect(str(copy / LEDGER_FILE))) as con:
        con.execute("INSERT INTO replay_ideas VALUES ('zzz:a1','ZQX','EQUITY','LONG',4,NULL,NULL,'OPEN')")
        con.execute("UPDATE replay_ideas SET quantity=1, status='OPEN' WHERE idea_id='c001-e07:a1'")
        con.commit()
    tampered = measure_ledger(copy, 3)
    assert tampered != expected_totals(3)
    assert (tampered.idea_rows, tampered.closed_ideas, tampered.open_position_quantity) == (7, 5, 5)
    assert cycle_mismatches(copy, 3) == ["cycle 1: ideas differ from the specification",
                                         "unexpected ideas outside the planned cycles (None): 1"]
    # the measurement connection is read-only
    from tradingbot.shadow_soak import _readonly
    with closing(_readonly(copy / LEDGER_FILE)) as con, pytest.raises(sqlite3.OperationalError, match="readonly"):
        con.execute("DELETE FROM replay_ideas")


# ----------------------------------------------------------------------------- 4. the seeded plan

def test_plan_is_seed_deterministic_strictly_increasing_and_covers_every_restart_kind():
    plan = make_plan(CI, 51)
    assert plan == make_plan(CI, 51) and plan.deliveries == 51
    cuts = [s.cut_index for s in plan.steps]
    assert len(cuts) == CI.restarts and cuts == sorted(set(cuts)) and 1 <= cuts[0] and cuts[-1] <= 50
    assert {s.planned for s in plan.steps} == {"pause", "interrupt_before_commit", "interrupt_after_commit"}
    assert [s.segment for s in plan.steps] == list(range(1, CI.restarts + 1))
    seeds = [plan.reference_hash_seed, plan.final_hash_seed, *(s.hash_seed for s in plan.steps)]
    assert len(set(seeds)) == CI.restarts + 2 and all(1 <= s <= 2**32 - 1 for s in seeds)
    other = make_plan(CI.model_copy(update={"seed": CI.seed + 1}), 51)
    assert [s.cut_index for s in other.steps] != cuts or other.reference_hash_seed != plan.reference_hash_seed
    assert make_plan(CI.model_copy(update={"restarts": 0}), 51).steps == []
    with pytest.raises(SoakStateError, match="restarts need at least"):
        make_plan(CI.model_copy(update={"restarts": 51}), 51)
    assert make_plan(CI.model_copy(update={"restarts": 50}), 51).steps[-1].cut_index == 50


def test_profile_contract_is_strict_and_bounded():
    assert (CI.cycles, CI.restarts) == (3, 5)
    representative = PROFILES["representative"]
    assert (representative.cycles, representative.restarts) == (64, 24)
    assert representative.cycles * SPEC_DELIVERIES >= 1000
    assert representative.max_total_wall_seconds <= MAX_TOTAL_WALL_SECONDS
    with pytest.raises(ValidationError):
        SoakProfile(name="x", cycles=1, restarts=0, seed=1, broker="live")
    for bad in ({"cycles": 0}, {"cycles": MAX_CYCLES + 1}, {"restarts": MAX_RESTARTS + 1}, {"seed": -1},
                {"max_total_wall_seconds": MAX_TOTAL_WALL_SECONDS + 1}, {"max_wall_seconds_per_process": 0},
                {"name": "Bad Name"}):
        with pytest.raises(ValidationError):
            SoakProfile(**{"name": "x", "cycles": 1, "restarts": 0, "seed": 1, **bad})


# ----------------------------------------------------------------------------- 5. the soak: fresh processes, one ledger

def test_fast_profile_passes_with_fresh_processes_pauses_and_both_crash_windows(soak):
    _, report, seen = soak
    assert report.verdict == "PASS" and report.findings == [], report.findings
    assert report.processes_launched == len(report.segments) == len(seen) == CI.restarts + 2
    assert seen == report.segments                                     # the callback saw the verified outcomes
    plan = report.plan
    assert [s.planned for s in report.segments] == ["complete", *(st.planned for st in plan.steps), "complete"]
    assert [s.role for s in report.segments] == ["reference", *(["chain"] * (CI.restarts + 1))]
    assert [s.exit_code for s in report.segments] == [EXIT_FOR_PLAN[s.planned] for s in report.segments]
    assert [s.status for s in report.segments[1:-1]] == \
        ["DELIVERY_CAP_REACHED" if st.planned == "pause" else "INTERRUPTED" for st in plan.steps]
    assert all(s.import_origin_verified and s.hash_seed_confirmed and s.network_guard for s in report.segments)
    assert [s.hash_seed for s in report.segments] == [plan.reference_hash_seed, *(st.hash_seed for st in plan.steps),
                                                      plan.final_hash_seed]
    assert report.distinct_hash_seeds == CI.restarts + 2
    assert all(s.window_verified for s in report.segments[1:-1]) and report.restarts_executed == CI.restarts
    chain = report.segments[1:]
    assert chain[0].resumed_from == 0 and all(b.resumed_from == a.cursor for a, b in itertools.pairwise(chain))
    assert [s.cursor for s in chain] == [*(st.cut_index for st in plan.steps), 51]
    assert chain[-1].status == "COMPLETE" and report.segments[0].status == "COMPLETE"
    assert report.digests_converge and report.ledgers_equal
    assert report.reference_digest == report.chain_digest == report.segments[0].convergence_digest
    assert report.actual_reference == report.actual_chain == report.expected == expected_totals(3)
    assert report.cycle_mismatches == []
    # at-least-once accounting: the chain re-evaluates a delivery after every after-commit crash
    after_commit = sum(1 for st in plan.steps if st.planned == "interrupt_after_commit")
    assert sum(s.processed for s in chain) == 51 + after_commit
    assert sum(s.duplicate_returns + s.hash_mismatch_blocks for s in chain) == 3 * 6 + after_commit
    assert report.wall.total_s > 0 and report.wall.total_s >= report.wall.reference_s + report.wall.chain_s - 0.01
    assert (report.feed.deliveries, report.feed.posts, report.feed.virtual_span_s) == (51, 33, 2 * PERIOD + 700 * 60)
    assert (report.feed.duplicate_deliveries, report.feed.tampered_deliveries) == (15, 3)
    assert report.feed.base_feed_digest == BASE.digest() and report.feed.feed_digest == FEED3.digest()


def test_report_is_versioned_strict_sanitized_and_round_trips(soak):
    root, report, _ = soak
    text = (root / SOAK_REPORT_FILE).read_text("utf-8")
    assert SoakReport.model_validate_json(text) == report
    assert report.schema_version == REPORT_SCHEMA and report.mock_only is True and report.label == SOAK_LABEL
    assert str(root) not in text and "/Users/" not in text and "/tmp/" not in text    # no host paths in the report
    data = json.loads(text)
    assert {"profile", "feed", "plan", "segments", "expected", "actual_reference", "actual_chain", "wall", "verdict"} <= set(data)
    assert data["feed"]["source_identity_scheme"] == "c{cycle:03d}-{base_key}"
    with pytest.raises(ValidationError):
        SoakReport.model_validate({**data, "live_routing": True})
    with pytest.raises(ValidationError):
        SoakReport.model_validate({**data, "mock_only": False})
    segment_reports = sorted(p.name for p in (root / "segments").iterdir())
    assert segment_reports == ["000-reference.json", *(f"{s.segment:03d}-chain.json" for s in report.segments[1:]
                                                       if s.status != "INTERRUPTED")]
    reference = ShadowRunReport.model_validate_json((root / "segments" / "000-reference.json").read_text())
    final = ShadowRunReport.model_validate_json((root / "segments" / segment_reports[-1]).read_text())
    assert reference.ledger == final.ledger and reference.this_run != final.this_run


def test_chain_state_is_preserved_and_a_mismatched_resume_is_refused(soak):
    root, report, _ = soak
    chain = root / "chain"
    assert sorted(p.name for p in chain.iterdir()) == [CHECKPOINT_FILE, LEDGER_FILE]
    assert ShadowCheckpoint.model_validate_json((chain / CHECKPOINT_FILE).read_text()).cursor == 51
    assert ledger_rows(chain) == ledger_rows(root / "reference") == (33, 33, 6)
    again = ShadowRunner(chain, FEED3, POLICY, resume=True).run()
    assert (again.status, again.this_run.processed, again.convergence_digest) == ("COMPLETE", 0, report.chain_digest)
    with pytest.raises(ShadowStateError, match="non-empty"):
        ShadowRunner(chain, FEED3, POLICY)
    with pytest.raises(ShadowStateError, match="different feed"):
        ShadowRunner(chain, build_soak_feed(2), POLICY, resume=True)
    # the SHADOW-01 command cannot resume a soak ledger either: its feed does not match the checkpoint
    refused = cli(SHADOW_01_SCRIPT, "--workdir", str(chain), "--resume")
    assert refused.returncode == 2 and "REFUSED" in refused.stderr and "different feed" in refused.stderr
    assert ledger_rows(chain) == (33, 33, 6)


def test_soak_refuses_an_occupied_root_or_an_unplannable_profile_without_touching_anything(tmp_path):
    occupied = tmp_path / "occupied"
    occupied.mkdir()
    (occupied / "ledger.sqlite3").write_bytes(b"not ours")
    with pytest.raises(SoakStateError, match="non-empty"):
        run_soak(CI, occupied)
    assert [p.name for p in occupied.iterdir()] == ["ledger.sqlite3"]
    assert (occupied / "ledger.sqlite3").read_bytes() == b"not ours"
    not_a_dir = tmp_path / "file"
    not_a_dir.write_text("x")
    with pytest.raises(SoakStateError, match="not a directory"):
        run_soak(CI, not_a_dir)
    with pytest.raises(SoakStateError, match="restarts need at least"):
        run_soak(CI.model_copy(update={"restarts": 60}), tmp_path / "never")
    assert not (tmp_path / "never").exists()


def test_wall_budget_pauses_are_reported_incomplete_never_pass(tmp_path):
    tiny = SoakProfile(name="tiny", cycles=1, restarts=1, seed=3, max_wall_seconds_per_process=1e-9)
    report = run_soak(tiny, tmp_path / "per-process")
    assert report.verdict == "INCOMPLETE" and report.processes_launched == 1
    assert report.segments[0].status == "WALL_BUDGET_EXHAUSTED" and report.segments[0].exit_code == EXIT_PAUSED
    assert report.findings == [("INCOMPLETE: reference paused on its wall budget at cursor "
                                f"{report.segments[0].cursor} (bounded pause, not a PASS)")]
    assert report.actual_reference is None and not report.digests_converge and report.restarts_executed == 0
    preserved = tmp_path / "per-process" / "reference"
    assert 0 <= ShadowCheckpoint.model_validate_json((preserved / CHECKPOINT_FILE).read_text()).cursor < 17
    assert (tmp_path / "per-process" / SOAK_REPORT_FILE).is_file()
    # the total budget stops the chain between processes
    fake = itertools.chain([0.0], itertools.repeat(10_000.0))
    small = SoakProfile(name="total", cycles=1, restarts=1, seed=3, max_total_wall_seconds=900)
    report = run_soak(small, tmp_path / "total", wall_clock=lambda: next(fake))
    assert report.verdict == "INCOMPLETE" and report.processes_launched == 1
    assert report.segments[0].status == "COMPLETE"
    assert report.findings == ["INCOMPLETE: total wall budget exhausted before chain segment 1"]


def test_tampered_chain_ledger_fails_even_though_the_runner_digests_agree(tmp_path):
    """Digest equality alone is insufficient: the runner digests are computed before the tamper and
    still match, the independent read-only measurement catches it and the verdict is FAIL."""
    def tampering(request):
        outcome = launch_subprocess(request)
        if request.spec.role == "chain" and request.spec.planned == "complete":
            with closing(sqlite3.connect(str(request.workdir / LEDGER_FILE))) as con:
                con.execute("INSERT INTO replay_ideas VALUES ('zzz:a1','ZQX','EQUITY','LONG',4,NULL,NULL,'OPEN')")
                con.commit()
        return outcome

    small = SoakProfile(name="tamper", cycles=1, restarts=1, seed=5)
    report = run_soak(small, tmp_path / "root", launcher=tampering)
    assert report.verdict == "FAIL" and report.processes_launched == 3
    assert report.digests_converge and report.ledgers_equal
    assert report.actual_reference == report.expected and report.actual_chain != report.expected
    assert (report.actual_chain.idea_rows, report.actual_chain.open_position_quantity) == (3, 4)
    assert report.cycle_mismatches == ["chain: unexpected ideas outside the planned cycles (None): 1"]
    assert report.findings == ["FAIL: chain ledger totals differ from the specification-derived expectation",
                               "FAIL: 1 per-cycle mismatch(es), see cycle_mismatches"]


def test_segment_process_refuses_a_wrong_import_origin_or_feed_digest_before_touching_its_workdir(tmp_path):
    from tradingbot.shadow_soak import SegmentRequest, SegmentSpec
    spec = SegmentSpec(segment=0, role="reference", planned="complete", resume=False, hash_seed=7, max_wall_seconds=30)
    feed = build_soak_feed(1)

    def child(request) -> tuple[int, SegmentOutcome]:
        env = {**os.environ, "PYTHONPATH": str(SRC), "PYTHONHASHSEED": "7"}
        proc = subprocess.run([sys.executable, "-m", "tradingbot.shadow_soak", *segment_argv(request)],
                              capture_output=True, text=True, env=env, check=False)
        return proc.returncode, SegmentOutcome.model_validate_json(proc.stdout.strip().splitlines()[-1])

    wrong_origin = SegmentRequest(spec=spec, workdir=tmp_path / "a", report_path=tmp_path / "a.json", cycles=1,
                                  cycle_period_s=PERIOD, feed_digest=feed.digest(), import_origin=tmp_path / "elsewhere")
    code, outcome = child(wrong_origin)
    assert (code, outcome.status, outcome.import_origin_verified) == (2, "REFUSED", False)
    assert "import origin mismatch" in outcome.error and not (tmp_path / "a").exists()
    wrong_digest = SegmentRequest(spec=spec, workdir=tmp_path / "b", report_path=tmp_path / "b.json", cycles=1,
                                  cycle_period_s=PERIOD, feed_digest="0" * 64, import_origin=SRC)
    code, outcome = child(wrong_digest)
    assert (code, outcome.status, outcome.import_origin_verified) == (2, "REFUSED", True)
    assert "feed digest mismatch" in outcome.error and not (tmp_path / "b").exists()
    good = SegmentRequest(spec=spec, workdir=tmp_path / "c", report_path=tmp_path / "c.json", cycles=1,
                          cycle_period_s=PERIOD, feed_digest=feed.digest(), import_origin=SRC)
    code, outcome = child(good)
    assert (code, outcome.status, outcome.cursor, outcome.hash_seed_confirmed, outcome.network_guard) == \
        (0, "COMPLETE", 17, True, True)
    assert ShadowRunReport.model_validate_json((tmp_path / "c.json").read_text()).convergence_digest == outcome.convergence_digest


def test_no_network_guard_refuses_sockets_and_the_runner_completes_under_it(tmp_path):
    assert not network_guard_active()
    with no_network():
        assert network_guard_active()
        for attempt in (lambda: socket.socket(), lambda: socket.create_connection(("127.0.0.1", 9)),
                        lambda: socket.getaddrinfo("localhost", 80), lambda: socket.socketpair()):
            with pytest.raises(NetworkRefused):
                attempt()
        report = ShadowRunner(tmp_path / "offline", build_soak_feed(2), POLICY).run()
        assert report.status == "COMPLETE" and measure_ledger(tmp_path / "offline", 2) == expected_totals(2)
    assert not network_guard_active()
    with closing(socket.socket()) as restored:
        assert restored.fileno() >= 0                      # the originals are restored after the block


# ----------------------------------------------------------------------------- 6. the documented command

def test_cli_ci_profile_passes_writes_the_report_and_removes_its_temporary_root(tmp_path):
    result = cli(SCRIPT, "--json", str(tmp_path / "soak.json"), tmpdir=tmp_path, seed="11")
    assert result.returncode == 0, result.stderr[-800:]
    out = result.stdout
    assert SOAK_LABEL in out and "verdict: PASS" in out and "profile ci: cycles=3 restarts=5" in out
    assert "wall time (measured" in out and "virtual time (deterministic input" in out and "1d 11:40:00" in out
    assert "[00] reference complete" in out and "DELIVERY_CAP_REACHED" in out and "INTERRUPTED" in out
    assert "digests converge: True  ledgers equal: True" in out
    report = SoakReport.model_validate_json((tmp_path / "soak.json").read_text())
    assert report.verdict == "PASS" and report.processes_launched == 7 and report.restarts_executed == 5
    removed = Path(out.split("temporary root removed: ")[1].split()[0])
    assert not removed.exists() and removed.is_relative_to(tmp_path)


def test_cli_explicit_workdir_is_preserved_and_an_occupied_one_is_refused(tmp_path):
    chosen = tmp_path / "chosen"
    result = cli(SCRIPT, "--quiet", "--workdir", str(chosen), "--cycles", "1", "--restarts", "2", "--seed", "9")
    assert result.returncode == 0, result.stderr[-800:]
    assert "temporary root" not in result.stdout and "verdict: PASS" in result.stdout
    assert sorted(p.name for p in chosen.iterdir()) == ["chain", "reference", "segments", SOAK_REPORT_FILE]
    refused = cli(SCRIPT, "--quiet", "--workdir", str(chosen))
    assert refused.returncode == 2 and "REFUSED" in refused.stderr and "non-empty" in refused.stderr
    assert sorted(p.name for p in chosen.iterdir()) == ["chain", "reference", "segments", SOAK_REPORT_FILE]
    assert SoakReport.model_validate_json((chosen / SOAK_REPORT_FILE).read_text()).profile.cycles == 1


def test_cli_incomplete_soak_preserves_its_temporary_root_and_exits_6(tmp_path):
    result = cli(SCRIPT, "--cycles", "1", "--restarts", "1", "--max-seconds-per-process", "1e-9", tmpdir=tmp_path)
    assert result.returncode == 6, result.stderr[-800:]
    assert "verdict: INCOMPLETE" in result.stdout and "bounded pause, not a PASS" in result.stdout
    kept = Path(result.stdout.split("temporary root preserved for inspection: ")[1].split()[0])
    assert kept.is_dir() and kept.is_relative_to(tmp_path)
    assert (kept / "reference" / CHECKPOINT_FILE).is_file() and (kept / SOAK_REPORT_FILE).is_file()


def test_cli_usage_errors_are_refused_before_any_directory_is_created(tmp_path):
    for args, message in ((("--cycles", "2", "--restarts", "40"), "restarts need at least"),
                          (("--cycle-period-s", "10"), "never overlap"),
                          (("--cycles", "0"), "cycles"),
                          (("--max-total-seconds", "99999"), "max_total_wall_seconds")):
        result = cli(SCRIPT, *args, "--workdir", str(tmp_path / "never"))
        assert result.returncode == 2 and message in result.stderr, (args, result.stderr[-300:])
        assert not (tmp_path / "never").exists()


# ----------------------------------------------------------------------------- 7. representative profile (opt-in)

@pytest.mark.skipif(os.environ.get("SHADOW_SOAK_PROFILE") != "representative",
                    reason="bounded local profile (64 cycles, 1088 deliveries, 24 restarts); set SHADOW_SOAK_PROFILE=representative")
def test_representative_profile_passes_locally(tmp_path):
    report = run_soak(PROFILES["representative"], tmp_path / "root")
    assert report.verdict == "PASS", report.findings
    assert report.feed.deliveries >= 1000 and report.restarts_executed == 24 and report.processes_launched == 26
    assert report.actual_reference == report.actual_chain == expected_totals(64)
