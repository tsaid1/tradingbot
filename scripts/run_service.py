#!/usr/bin/env python3
"""Run the TradingBot runtime service. Every order, fill and position is simulated in both modes:
``mode = "mock"`` (an invented market) or ``mode = "shadow"`` (real read-only IBKR paper quotes;
nothing is ever sent to a broker but the read-only adapter's market-data and read requests).

    PYTHONPATH=src python scripts/run_service.py --config path/to/runtime.toml
    PYTHONPATH=src python scripts/run_service.py --config runtime.toml --once
    PYTHONPATH=src python scripts/run_service.py --config runtime.toml --source replay:posts.jsonl
    PYTHONPATH=src python scripts/run_service.py --config replay.toml --source replay:posts.jsonl \
        --allow-candidate-lexicon      # an offline replay with a CANDIDATE lexicon (docs/RUNTIME.md)

Exit codes: 0 after a clean stop (or one cycle with --once), 2 when the config is refused,
1 on an unhandled fault. See docs/RUNTIME.md.
"""

from __future__ import annotations

import argparse
import json
import signal
import sys

from tradingbot.execution.coordinator import ExecutionStoreLocked
from tradingbot.runtime.app import build_service, with_replay_source
from tradingbot.runtime.config import DEFAULT_CONFIG_PATH, ConfigError, load_config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG_PATH),
        help=f"runtime TOML (default {DEFAULT_CONFIG_PATH})",
    )
    parser.add_argument("--once", action="store_true", help="run one cycle and exit")
    parser.add_argument("--source", help="override the source: replay:<file.jsonl>")
    parser.add_argument("--allow-candidate-lexicon", action="store_true",
                        help="offline replay only: accept a CANDIDATE lexicon (unapproved "
                             "vocabulary proposals) as [interpreter] lexicon_path")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        if args.source:
            kind, _, value = args.source.partition(":")
            if kind != "replay" or not value:
                raise ConfigError("--source must be replay:<file>")
            config = with_replay_source(config, value)
        service = build_service(config, allow_candidate_lexicon=args.allow_candidate_lexicon)
    except (ConfigError, ExecutionStoreLocked) as exc:
        print(f"refusing to start: {exc}", file=sys.stderr)
        return 2

    stopping = {"flag": False}

    def stop(signum, _frame):
        stopping["flag"] = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        if args.once:
            report = service.run_once()
            print(
                json.dumps(
                    {
                        "posts_processed": report.posts_processed,
                        "held_actions": report.held_actions,
                        "duplicates": report.duplicates,
                        "skipped": report.skipped,
                        "faults": report.faults,
                        "reconciliation_clean": report.reconciliation_clean,
                        "unexplained": report.unexplained,
                        "halted": report.halted,
                    },
                    indent=2,
                )
            )
        else:
            what = ("simulated fills at read-only real quotes" if config.mode == "shadow"
                    else "simulated broker")
            print(f"service running ({config.mode} mode: {what}); heartbeat at "
                  f"{config.heartbeat_file}", flush=True)
            service.run_forever(stop=lambda: stopping["flag"])
    except Exception as exc:  # noqa: BLE001 - an unhandled fault exits non-zero
        print(f"unhandled fault: {type(exc).__name__}", file=sys.stderr)
        return 1
    finally:
        if config.mode == "shadow":  # every quote stream closed, the session disconnected
            service.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
