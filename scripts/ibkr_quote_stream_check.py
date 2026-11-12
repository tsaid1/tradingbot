"""Read-only IBKR held-quote-stream check (CONNECTS to TWS; run only when the owner has started
the paper TWS and approved the run, and only after the independent review).

    python scripts/ibkr_quote_stream_check.py [--config PATH] [--report-dir PATH]
        [--contract symbol=TLT,sec_type=STK,exchange=SMART,currency=USD,primary_exchange=NASDAQ]...
        [--seconds 20] [--market-data-type 3] [--max-age 3]

Loads the private paper config exactly as ``ibkr_paper_check.py`` does and connects (preflight,
identity gate and read-only verification all apply). For each contract in turn (default: TLT;
``--contract`` takes the same fields as a ``[[contract]]`` entry of the discovery query file, and
may be repeated, at most 10): resolve it, open one held quote stream, hold it ``--seconds``
(default 20, maximum 120) polling the latest snapshot, then cancel it. Disconnects, prints per
contract the requested and reported type, tick classes, updates per side, per-side age at the end,
first post-image update time, largest age seen, the label and its reason, and the notices, and
writes ``ibkr_quote_stream_check_<UTC>.json`` (masked account only) to the check script's report
directory.

Exit 0 when every contract was processed and cleanup was clean; 1 on a config, connection,
identity, read-only or cleanup failure; 2 on bad arguments or no report directory (both detected
before any socket opens). See docs/IBKR_ADAPTER.md, section 8.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from tradingbot.ibkr.check import default_report_dir as _default_report_dir
from tradingbot.ibkr.check import write_report
from tradingbot.ibkr.config import DEFAULT_CONFIG_PATH
from tradingbot.ibkr.discovery import QueryFileInvalid
from tradingbot.ibkr.models import TLT_QUERY
from tradingbot.ibkr.stream_check import (
    DEFAULT_HOLD_SECONDS,
    DEFAULT_MAX_AGE_SECONDS,
    STREAM_CHECK_PREFIX,
    exit_code,
    parse_contract_arg,
    run_stream_check,
    summarize,
    validate_args,
)

REPO = Path(__file__).resolve().parents[1]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH),
                        help=f"private TOML config (default {DEFAULT_CONFIG_PATH})")
    parser.add_argument("--report-dir", default=None, help="directory for the JSON report")
    parser.add_argument("--contract", action="append", default=None,
                        help="contract query as key=value pairs (repeatable; default TLT)")
    parser.add_argument("--seconds", type=float, default=DEFAULT_HOLD_SECONDS,
                        help="hold time per stream (default 20, maximum 120)")
    parser.add_argument("--market-data-type", type=int, default=3,
                        help="reqMarketDataType for every stream (default 3)")
    parser.add_argument("--max-age", type=float, default=DEFAULT_MAX_AGE_SECONDS,
                        help="per-side age limit for the entry freshness check (default 3)")
    args = parser.parse_args(argv)

    try:
        queries = ([parse_contract_arg(c) for c in args.contract] if args.contract
                   else [TLT_QUERY])
        validate_args(queries, args.seconds, args.market_data_type, args.max_age)
    except QueryFileInvalid as exc:
        print(f"arguments refused: {exc}", file=sys.stderr)
        return 2
    report_dir = Path(args.report_dir) if args.report_dir else _default_report_dir(REPO)
    if report_dir is None:
        print("no report directory: pass --report-dir or set TRADINGBOT_IBKR_REPORT_DIR",
              file=sys.stderr)
        return 2

    report = run_stream_check(queries, args.config, hold_seconds=args.seconds,
                              market_data_type=args.market_data_type,
                              max_age_seconds=args.max_age)
    path = write_report(report, report_dir, prefix=STREAM_CHECK_PREFIX)
    print(summarize(report))
    print(f"report: {path}")
    return exit_code(report)


if __name__ == "__main__":
    sys.exit(main())
