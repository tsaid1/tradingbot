"""Read-only IBKR contract discovery (CONNECTS to TWS; run only when the owner has started the
paper TWS and approved the run).

    python scripts/ibkr_contract_discovery.py [--config PATH] [--report-dir PATH]
        [--queries config/contract_discovery.toml] [--pace SECONDS] [--limit N]

Loads the private paper config exactly as ``ibkr_paper_check.py`` does, connects (preflight,
identity gate and read-only verification all apply), runs every query in the query file
sequentially with a fixed pause between requests (contract details; option-chain parameters for
``[[option_chain]]`` entries), disconnects, writes ``contract_catalog_<UTC>.json`` (schema
``tradingbot.ibkr.contract_catalog/1``, masked account only) to the check script's report
directory and prints a compact table.

Exit 0 when the batch completed (NOT_FOUND / ERROR / TIMEOUT rows included); 1 on a config,
connection, identity, read-only or cleanup failure; 2 on a bad query file or no report directory
(both detected before any socket opens). See docs/IBKR_ADAPTER.md, section 7.
"""
from __future__ import annotations

import argparse
import dataclasses
import sys
from pathlib import Path

from tradingbot.ibkr.check import default_report_dir as _default_report_dir
from tradingbot.ibkr.check import write_report
from tradingbot.ibkr.config import DEFAULT_CONFIG_PATH
from tradingbot.ibkr.discovery import (
    CATALOG_PREFIX,
    QueryFileInvalid,
    exit_code,
    load_plan,
    run_discovery,
    summarize,
)

REPO = Path(__file__).resolve().parents[1]
DEFAULT_QUERIES = REPO / "config" / "contract_discovery.toml"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH),
                        help=f"private TOML config (default {DEFAULT_CONFIG_PATH})")
    parser.add_argument("--report-dir", default=None, help="directory for the JSON catalog")
    parser.add_argument("--queries", default=str(DEFAULT_QUERIES),
                        help="query list (default config/contract_discovery.toml)")
    parser.add_argument("--pace", type=float, default=None,
                        help="seconds between requests (overrides the query file)")
    parser.add_argument("--limit", type=int, default=None,
                        help="max contracts kept per query (overrides the query file)")
    args = parser.parse_args(argv)

    try:
        plan = load_plan(args.queries)
        if args.pace is not None:
            if not 0 <= args.pace <= 60:
                raise QueryFileInvalid("--pace must be in 0..60")
            plan = dataclasses.replace(plan, pace_seconds=args.pace)
        if args.limit is not None:
            if not 1 <= args.limit <= 1000:
                raise QueryFileInvalid("--limit must be in 1..1000")
            plan = dataclasses.replace(plan, limit=args.limit)
    except QueryFileInvalid as exc:
        print(f"query file refused: {exc}", file=sys.stderr)
        return 2
    report_dir = Path(args.report_dir) if args.report_dir else _default_report_dir(REPO)
    if report_dir is None:
        print("no report directory: pass --report-dir or set TRADINGBOT_IBKR_REPORT_DIR",
              file=sys.stderr)
        return 2

    catalog = run_discovery(plan, args.config)
    path = write_report(catalog, report_dir, prefix=CATALOG_PREFIX)
    print(summarize(catalog))
    print(f"catalog: {path}")
    return exit_code(catalog)


if __name__ == "__main__":
    sys.exit(main())
