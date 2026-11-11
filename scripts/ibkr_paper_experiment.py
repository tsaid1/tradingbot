"""The staged paper order experiment ``paper-test-1``. CONNECTS to the paper TWS and (stages 2
and 3) PLACES PAPER ORDERS. Run only with the owner's explicit authorization of this experiment,
one stage at a time, following docs/IBKR_PAPER_ORDERS.md ("Staged runbook").

    python scripts/ibkr_paper_experiment.py stage0-refusal            # Read-Only ON; 0 orders
    python scripts/ibkr_paper_experiment.py stage1-lookups            # Read-Only OFF; 0 orders
    python scripts/ibkr_paper_experiment.py stage2-unfillable-bracket # 2 submissions
    python scripts/ibkr_paper_experiment.py stage3-lifecycle          # 4 submissions (6 total)
    python scripts/ibkr_paper_experiment.py abort                     # cancels this client's
                                                                      # working orders by id

Each stage refuses unless the previous stage passed, never repeats a passed stage, writes one
JSON evidence file (masked account only) beside the journal and stops. There is no flag to skip
or repeat a stage. Exit 0 only when the stage passed.
"""
from __future__ import annotations

import argparse
import sys

from tradingbot.ibkr_orders.config import DEFAULT_ORDER_CONFIG_PATH
from tradingbot.ibkr_orders.experiment import ABORT, STAGES, StageRefused, run_stage


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("stage", choices=[*STAGES, ABORT])
    parser.add_argument("--config", default=str(DEFAULT_ORDER_CONFIG_PATH),
                        help=f"private order config (default {DEFAULT_ORDER_CONFIG_PATH})")
    args = parser.parse_args(argv)
    try:
        evidence, path = run_stage(args.stage, args.config)
    except StageRefused as exc:
        print(f"refused (nothing was sent): {exc}", file=sys.stderr)
        return 2
    verdict = "PASSED" if evidence.get("passed") else "FAILED"
    print(f"{args.stage}: {verdict}" + (f" - {evidence['why_failed']}"
                                        if evidence.get("why_failed") else "")
          + (f" - {evidence['error']}" if evidence.get("error") else ""))
    print(f"submissions: {evidence.get('submissions_before')} -> "
          f"{evidence.get('submissions_after')}")
    print(f"evidence: {path}")
    return 0 if evidence.get("passed") else 1


if __name__ == "__main__":
    sys.exit(main())
