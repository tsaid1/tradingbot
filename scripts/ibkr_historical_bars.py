"""Read-only IBKR historical bars (CONNECTS to TWS; run only by the owner or the lead, in a bounded
session, with the paper TWS running).

    python scripts/ibkr_historical_bars.py --requests FILE --out PRIVATE_PATH [--config PATH]

Validates the request file (schema ``tradingbot.ibkr.historical_bars_requests/1``; see
``config/historical_bars.example.toml``) and the output path BEFORE the private config is read or
any socket opens (exit 2 on either). Then loads the private paper config exactly as
``ibkr_paper_check.py`` does, connects (preflight, identity gate and read-only verification all
apply), runs the requests one by one through ``IbkrSession.historical_bars`` (its pacing applies),
disconnects and writes ONE JSON report (schema ``tradingbot.ibkr.historical_bars_report/1``,
file 0600, masked account only) to ``--out``: a new file outside the repository. It prints only
counts and reason codes.

Exit 0 when every request ran (NO_DATA / NO_PERMISSION / TIMEOUT rows included); 1 when the batch
stopped (broker pacing violation, local pacing refusal, connection, identity, read-only or cleanup
failure); 2 on a bad request file or output path. See docs/IBKR_ADAPTER.md, section 9.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from tradingbot.ibkr.config import DEFAULT_CONFIG_PATH
from tradingbot.ibkr.historical_batch import (
    OutputPathRefused,
    RequestFileInvalid,
    check_output_path,
    exit_code,
    load_plan,
    run_batch,
    summarize,
    write_private_report,
)

REPO = Path(__file__).resolve().parents[1]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--requests", required=True, help="TOML request file")
    parser.add_argument("--out", required=True,
                        help="private report path: a new file outside the repository")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH),
                        help=f"private TOML config (default {DEFAULT_CONFIG_PATH})")
    args = parser.parse_args(argv)
    try:
        plan = load_plan(args.requests)
    except RequestFileInvalid as exc:
        print(f"request file refused: {exc}", file=sys.stderr)
        return 2
    try:
        out = check_output_path(args.out, REPO)
    except OutputPathRefused as exc:
        print(f"output path refused: {exc}", file=sys.stderr)
        return 2

    report = run_batch(plan, args.config)
    try:
        out = check_output_path(out, REPO)  # again: nothing may have appeared there meanwhile
        write_private_report(report, out)
    except (OutputPathRefused, OSError) as exc:
        print(f"report NOT written: {type(exc).__name__}", file=sys.stderr)
        print(summarize(report))
        return 1
    print(summarize(report))
    print("report written (0600)")
    return exit_code(report)


if __name__ == "__main__":
    sys.exit(main())
