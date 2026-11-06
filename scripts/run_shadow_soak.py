"""SHADOW-02 CLI: bounded SYNTHETIC multi-cycle soak with repeated fresh-process restarts. MOCK ONLY.

Runs an uninterrupted reference process and a seeded chain of fresh OS processes (pause/resume,
before-commit and after-commit interruptions, each under a different ``PYTHONHASHSEED``) on ONE
persistent mock ledger. The feed is the public SHADOW-01 fixture repeated with disjoint source
identities on a coherent virtual timeline; both final ledgers are measured independently against
the specification. Offline synthetic evidence only: not broker, live-source, real-clock,
performance or production-readiness evidence.

Examples::

    python scripts/run_shadow_soak.py                                  # ci profile: 3 cycles, 51 deliveries, 5 restarts
    python scripts/run_shadow_soak.py --profile representative --json /tmp/soak.json
                                                                       # 64 cycles, 1088 deliveries, 24 restarts
    python scripts/run_shadow_soak.py --cycles 8 --restarts 6 --seed 7 --workdir /tmp/soak-demo

Exit codes: 0 PASS, 2 refused (occupied directory, unplannable bounds, usage), 5 FAIL (ledgers
diverge, or differ from the specification), 6 INCOMPLETE (a wall budget stopped the soak; the
preserved directories hold the paused state, which is never a PASS).
"""
from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

from tradingbot.shadow_soak import (
    DEFAULT_CYCLE_PERIOD_S,
    PROFILES,
    SOAK_LABEL,
    LedgerTotals,
    SegmentOutcome,
    SoakProfile,
    SoakReport,
    SoakStateError,
    build_soak_feed,
    make_plan,
    no_network,
    run_soak,
)

EXIT_PASS, EXIT_REFUSED, EXIT_FAIL, EXIT_INCOMPLETE = 0, 2, 5, 6
VERDICT_EXIT = {"PASS": EXIT_PASS, "FAIL": EXIT_FAIL, "INCOMPLETE": EXIT_INCOMPLETE}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--profile", choices=sorted(PROFILES), default="ci",
                        help="bounded profile to start from (default: ci); explicit options below override its fields")
    parser.add_argument("--cycles", type=int, default=None, help="number of fixture cycles (17 deliveries each)")
    parser.add_argument("--restarts", type=int, default=None, help="number of planned process restarts")
    parser.add_argument("--seed", type=int, default=None, help="plan seed: cut points, restart kinds, hash seeds")
    parser.add_argument("--cycle-period-s", type=int, default=None,
                        help=f"virtual seconds per cycle (default {DEFAULT_CYCLE_PERIOD_S}; must exceed the fixture span)")
    parser.add_argument("--max-seconds-per-process", type=float, default=None,
                        help="wall-clock budget of each process (a stop guard only, never a decision input)")
    parser.add_argument("--max-total-seconds", type=float, default=None, help="wall-clock budget of the whole soak")
    parser.add_argument("--workdir", type=Path, default=None,
                        help="fresh (absent or empty) root directory; default: a temporary directory that is removed "
                             "only on PASS and preserved otherwise")
    parser.add_argument("--json", type=Path, default=None, metavar="PATH", help="write the soak report as JSON")
    parser.add_argument("--quiet", action="store_true", help="suppress the per-process trace")
    return parser


def resolve_profile(args: argparse.Namespace) -> SoakProfile:
    overrides = {
        "cycles": args.cycles, "restarts": args.restarts, "seed": args.seed, "cycle_period_s": args.cycle_period_s,
        "max_wall_seconds_per_process": args.max_seconds_per_process, "max_total_wall_seconds": args.max_total_seconds,
    }
    data = {**PROFILES[args.profile].model_dump(), **{k: v for k, v in overrides.items() if v is not None}}
    return SoakProfile.model_validate(data)


def virtual_span(seconds: int) -> str:
    span = timedelta(seconds=seconds)
    return f"{span.days}d {timedelta(seconds=span.seconds)} ({seconds}s virtual)"


def trace(outcome: SegmentOutcome) -> None:
    verified = "" if outcome.window_verified is None else f" window_verified={outcome.window_verified}"
    print(f"  [{outcome.segment:02d}] {outcome.role:<9} {outcome.planned:<23} PYTHONHASHSEED={outcome.hash_seed:<10} "
          f"{outcome.status:<20} exit {outcome.exit_code}  cursor {outcome.resumed_from}->{outcome.cursor}  "
          f"processed={outcome.processed} fresh={outcome.fresh_results} duplicate={outcome.duplicate_returns} "
          f"tampered={outcome.hash_mismatch_blocks}  wall {outcome.wall_seconds:.2f}s"
          f"{verified}{'  ' + outcome.error if outcome.error else ''}")


def totals_line(name: str, t: LedgerTotals | None) -> str:
    if t is None:
        return f"  {name:<9} (not measured)"
    return (f"  {name:<9} orders={t.orders} (open {t.open_fills}x qty {t.open_quantity}, reduce {t.reduce_fills}x qty "
            f"{t.reduce_quantity}, close {t.close_fills}x qty {t.close_quantity}, amendments {t.amendments}) "
            f"ideas={t.idea_rows} closed={t.closed_ideas} open_qty={t.open_position_quantity} "
            f"rows posts={t.post_rows} actions={t.action_rows} outcomes={t.outcome_counts}")


def print_report(report: SoakReport) -> None:
    print()
    print(f"processes launched: {report.processes_launched}  restarts verified: {report.restarts_executed}/"
          f"{report.restarts_planned}  distinct PYTHONHASHSEEDs: {report.distinct_hash_seeds}")
    print(f"reference digest: {report.reference_digest}")
    print(f"chain digest:     {report.chain_digest}")
    print(f"digests converge: {report.digests_converge}  ledgers equal: {report.ledgers_equal}")
    print("expected (specification-derived) vs measured (read-only SQLite):")
    print(totals_line("expected", report.expected))
    print(totals_line("reference", report.actual_reference))
    print(totals_line("chain", report.actual_chain))
    if report.cycle_mismatches:
        print(f"per-cycle mismatches ({len(report.cycle_mismatches)}):")
        for item in report.cycle_mismatches[:20]:
            print(f"  - {item}")
    w = report.wall
    print(f"wall time (measured, includes process start-up): reference {w.reference_s:.2f}s, chain {w.chain_s:.2f}s, "
          f"total {w.total_s:.2f}s; budgets {w.per_process_budget_s:g}s per process, {w.total_budget_s:g}s total")
    print(f"virtual time (deterministic input, not elapsed time): {report.feed.virtual_epoch.isoformat()} -> "
          f"{report.feed.virtual_end.isoformat()} = {virtual_span(report.feed.virtual_span_s)}")
    print(f"verdict: {report.verdict}")
    for finding in report.findings:
        print(f"  - {finding}")
    print(SOAK_LABEL)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        profile = resolve_profile(args)
        feed = build_soak_feed(profile.cycles, cycle_period_s=profile.cycle_period_s)
        plan = make_plan(profile, len(feed.deliveries))
    except (ValueError, SoakStateError) as exc:
        parser.error(str(exc))
    print(SOAK_LABEL)
    print(f"profile {profile.name}: cycles={profile.cycles} restarts={profile.restarts} seed={profile.seed} "
          f"cycle_period_s={profile.cycle_period_s} per-process budget {profile.max_wall_seconds_per_process:g}s, "
          f"total budget {profile.max_total_wall_seconds:g}s")
    print(f"feed {feed.feed_id} digest {feed.digest()} | {len(feed.posts)} synthetic post versions, "
          f"{len(feed.deliveries)} deliveries | virtual epoch {feed.epoch.isoformat()}, span "
          f"{virtual_span(feed.deliveries[-1].offset_s)}")
    kinds = {kind: sum(1 for s in plan.steps if s.planned == kind)
             for kind in ("pause", "interrupt_before_commit", "interrupt_after_commit")}
    print(f"plan: {len(plan.steps)} restarts {kinds} at cuts {[s.cut_index for s in plan.steps]}; "
          f"{len(plan.steps) + 2} fresh processes, each with its own PYTHONHASHSEED")

    def execute(root: Path) -> int:
        print(f"root {root}")
        try:
            with no_network():
                report = run_soak(profile, root, on_segment=None if args.quiet else trace)
        except SoakStateError as exc:
            print(f"REFUSED: {exc}", file=sys.stderr)
            return EXIT_REFUSED
        print_report(report)
        if args.json is not None:
            args.json.write_text(report.model_dump_json(indent=2), encoding="utf-8")
            print(f"report written to {args.json}")
        return VERDICT_EXIT[report.verdict]

    if args.workdir is not None:
        return execute(args.workdir)
    # Default mode: a private temporary directory, removed only on PASS. A FAIL or INCOMPLETE soak
    # keeps its reference and chain directories (checkpoint + ledger) for inspection.
    folder = Path(tempfile.mkdtemp(prefix="tradingbot-soak-"))
    code = execute(folder)
    if code == EXIT_PASS:
        shutil.rmtree(folder, ignore_errors=True)
        print(f"temporary root removed: {folder}")
    else:
        print(f"temporary root preserved for inspection: {folder}")
    return code


if __name__ == "__main__":
    sys.exit(main())
