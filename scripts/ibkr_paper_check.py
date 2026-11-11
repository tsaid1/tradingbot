"""Read-only IBKR paper connectivity check (CONNECTS to TWS; run only when the owner has started
the paper TWS and approved the run).

    python scripts/ibkr_paper_check.py [--config PATH] [--report-dir PATH]

Steps: load config -> connect + identity gate -> resolve TLT -> account summary -> positions ->
TLT quote snapshot -> disconnect -> reconnect once + disconnect (recovery proof). Writes a JSON report
(masked account only) and prints a summary. Exit 0 only for PASS or PASS_WITH_WARNINGS (every step,
including cleanup, passed; warnings such as an owner-attested read-only API need human review).

Default report dir: $TRADINGBOT_IBKR_REPORT_DIR, else ``private/ibkr_reports`` in the workspace that
contains this checkout (the parent or grandparent of the repo root). See docs/IBKR_ADAPTER.md.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from tradingbot.ibkr.check import run_check, summarize, write_report
from tradingbot.ibkr.config import DEFAULT_CONFIG_PATH


def default_report_dir() -> Path | None:
    env = os.environ.get("TRADINGBOT_IBKR_REPORT_DIR")
    if env:
        return Path(env)
    repo = Path(__file__).resolve().parents[1]
    for base in (repo.parent, repo.parent.parent):
        if base != Path(base.anchor) and (base / "private").is_dir():
            return base / "private" / "ibkr_reports"
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH),
                        help=f"private TOML config (default {DEFAULT_CONFIG_PATH})")
    parser.add_argument("--report-dir", default=None, help="directory for the JSON report")
    args = parser.parse_args(argv)

    report_dir = Path(args.report_dir) if args.report_dir else default_report_dir()
    if report_dir is None:
        print("no report directory: pass --report-dir or set TRADINGBOT_IBKR_REPORT_DIR",
              file=sys.stderr)
        return 2

    report = run_check(args.config)
    path = write_report(report, report_dir)
    print(summarize(report))
    print(f"report: {path}")
    return 0 if report["overall"] in ("PASS", "PASS_WITH_WARNINGS") else 1


if __name__ == "__main__":
    sys.exit(main())
