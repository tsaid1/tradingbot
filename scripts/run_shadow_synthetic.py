"""SHADOW-01 CLI: finite SYNTHETIC shadow run with checkpoint/restart. MOCK ONLY.

Replays the newly authored synthetic feed (``tradingbot.shadow_runner.synthetic_feed``) under a
virtual clock through the public interpreter -> validator -> Sprint 1.1 mock ledger chain, writes a
checkpoint after every committed delivery, and prints an aggregate report read back from the ledger.
No network, broker, credentials or working data are touched; a fresh directory is required.

Examples::

    python scripts/run_shadow_synthetic.py
    python scripts/run_shadow_synthetic.py --workdir /tmp/shadow-demo \
        --interrupt-at 6 --interrupt-phase after-commit
    python scripts/run_shadow_synthetic.py --workdir /tmp/shadow-demo --resume --json /tmp/shadow.json

Exit codes: 0 complete, 2 refused (occupied directory, invalid checkpoint, usage), 3 interrupted by
the injected crash (resume with ``--resume``), 4 paused by a bound (resume with ``--resume``).
"""
from __future__ import annotations

import argparse
import sys
import tempfile
import time
from pathlib import Path

from tradingbot.shadow_runner import (
    NON_PRODUCTION_LABEL,
    DeliveryRecord,
    InterruptSpec,
    ShadowInterrupted,
    ShadowRunner,
    ShadowRunReport,
    ShadowStateError,
    shadow_policy,
    synthetic_feed,
)

EXIT_OK, EXIT_REFUSED, EXIT_INTERRUPTED, EXIT_PAUSED = 0, 2, 3, 4


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--workdir", type=Path, default=None,
                        help="fresh (empty or absent) directory for the ledger and checkpoint; "
                             "default: a temporary directory removed at exit")
    parser.add_argument("--resume", action="store_true",
                        help="continue from the checkpoint in --workdir (validated against the feed and policy)")
    parser.add_argument("--interrupt-at", type=int, default=None, metavar="INDEX",
                        help="simulate a crash at this delivery index (requires --workdir)")
    parser.add_argument("--interrupt-phase", choices=["before-commit", "after-commit"], default="after-commit",
                        help="crash before the ledger transaction, or after it committed but before the "
                             "checkpoint was written (default)")
    parser.add_argument("--max-deliveries", type=int, default=None, help="stop after N deliveries this run")
    parser.add_argument("--max-seconds", type=float, default=60.0,
                        help="wall-clock budget for this run (a stop guard only, never a decision input)")
    parser.add_argument("--json", type=Path, default=None, metavar="PATH", help="write the report as JSON")
    parser.add_argument("--quiet", action="store_true", help="suppress the per-delivery trace")
    return parser


def trace(record: DeliveryRecord) -> None:
    outcomes = ", ".join(
        o.status + (f" {o.order.side} {o.order.quantity} @ {o.order.price}" if o.order and o.order.quantity else "")
        + ("" if o.status == "SIMULATED" else f" [{'/'.join(o.reasons[:2])}]")
        for o in record.result.outcomes
    ) or "(no actions)"
    kind = "DUPLICATE " if record.result.duplicate else ""
    print(f"  [{record.index:02d}] {record.virtual_now:%H:%M:%S} {record.post_key:<6} {kind}{outcomes}"
          f"  :: {record.note}")


def print_report(report: ShadowRunReport, elapsed: float) -> None:
    print()
    print(f"status: {report.status}  cursor: {report.cursor}/{report.deliveries_total}  "
          f"resumed_from: {report.resumed_from}  wall: {elapsed:.2f}s")
    s = report.this_run
    print(f"this run: processed={s.processed} fresh={s.fresh_results} duplicate_returns={s.duplicate_returns} "
          f"hash_mismatch_blocks={s.hash_mismatch_blocks}")
    print(f"ledger outcomes (latest attempt per post): {report.ledger.outcome_counts}")
    print(f"ledger reasons: {report.ledger.reason_counts}")
    print("ledger ideas:")
    for idea in report.ledger.ideas:
        print(f"  {idea.idea_id:<10} {idea.symbol:<5} {idea.position_side:<5} qty={idea.quantity} "
              f"stop={idea.stop_price} target={idea.target_price} {idea.status}")
    print(f"mock orders ({len(report.ledger.orders)}):")
    for order in report.ledger.orders:
        print(f"  {order.order_id:<20} {order.action:<6} {order.side:<4} {order.quantity} @ {order.price} {order.status}")
    print(f"convergence digest: {report.convergence_digest}")
    print(NON_PRODUCTION_LABEL)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if (args.resume or args.interrupt_at is not None) and args.workdir is None:
        parser.error("--resume and --interrupt-at require --workdir")
    feed, policy = synthetic_feed(), shadow_policy()
    interrupt = (InterruptSpec(args.interrupt_at, args.interrupt_phase.replace("-", "_"))
                 if args.interrupt_at is not None else None)
    print(NON_PRODUCTION_LABEL)
    print(f"feed {feed.feed_id} digest {feed.digest()} | {len(feed.posts)} synthetic post versions, "
          f"{len(feed.deliveries)} deliveries | virtual epoch {feed.epoch.isoformat()}")

    def execute(workdir: Path) -> int:
        try:
            runner = ShadowRunner(workdir, feed, policy, resume=args.resume)
        except ShadowStateError as exc:
            print(f"REFUSED: {exc}", file=sys.stderr)
            return EXIT_REFUSED
        print(f"workdir {workdir} | checkpoint cursor at start: {runner.cursor}")
        started = time.monotonic()
        try:
            report = runner.run(max_deliveries=args.max_deliveries, max_wall_seconds=args.max_seconds,
                                interrupt=interrupt, on_delivery=None if args.quiet else trace)
        except ShadowInterrupted as exc:
            print(f"INTERRUPTED (simulated crash): {exc}")
            print(f"resume with: --workdir {workdir} --resume")
            return EXIT_INTERRUPTED
        print_report(report, time.monotonic() - started)
        if args.json is not None:
            args.json.write_text(report.model_dump_json(indent=2), encoding="utf-8")
            print(f"report written to {args.json}")
        if report.status != "COMPLETE":
            print(f"paused ({report.status}); resume with: --workdir {workdir} --resume")
            return EXIT_PAUSED
        return EXIT_OK

    if args.workdir is None:
        with tempfile.TemporaryDirectory(prefix="tradingbot-shadow-") as folder:
            return execute(Path(folder))
    return execute(args.workdir)


if __name__ == "__main__":
    sys.exit(main())
