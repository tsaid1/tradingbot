"""The staged paper order experiments. CONNECTS to the paper TWS and PLACES PAPER ORDERS. The
private order config names the experiment (``orders = ...``); its stages are fixed in code. Run
only with the owner's explicit authorization of that experiment, one stage at a time, following
docs/IBKR_PAPER_ORDERS.md ("Staged runbook"; paper-test-2: its own section).

paper-test-1 (over; it can never place again):

    python scripts/ibkr_paper_experiment.py stage0-refusal            # Read-Only ON; 0 orders
    python scripts/ibkr_paper_experiment.py stage1-lookups            # Read-Only OFF; 0 orders
    python scripts/ibkr_paper_experiment.py stage2-unfillable-bracket # 2 submissions
    python scripts/ibkr_paper_experiment.py stage3-lifecycle          # 4 or 6 on a pass, at most 7
                                                                      # (9 total)
    python scripts/ibkr_paper_experiment.py abort                     # cancels this client's
                                                                      # working orders by id

Each stage refuses unless the previous stage passed, never repeats a passed stage, writes one
JSON evidence file (masked account only) beside the journal and stops.

paper-test-2 (10 submissions at most; NOT authorized):

    python scripts/ibkr_paper_experiment.py stage0-refusal             # Read-Only ON; 0 orders
    python scripts/ibkr_paper_experiment.py stage1-lookups             # Read-Only OFF; 0 orders
    python scripts/ibkr_paper_experiment.py stage2-single-unfillable   # 1 submission
    python scripts/ibkr_paper_experiment.py stage3-unfillable-bracket  # 2 submissions
    python scripts/ibkr_paper_experiment.py stage4-lifecycle           # at most 7
    python scripts/ibkr_paper_experiment.py abort

A stage name that is not a stage of the configured experiment is refused before connecting. There is no flag to skip
or repeat a stage. Exit 0 only when the stage passed.
"""
from __future__ import annotations

import argparse
import sys

from tradingbot.ibkr_orders.config import DEFAULT_ORDER_CONFIG_PATH
from tradingbot.ibkr_orders.experiment import ABORT, PLANS, StageRefused, run_stage

ALL_STAGES = tuple(dict.fromkeys(x for p in PLANS.values() for x in p.stages))

# Text only. CI has no ibapi, so the order-path tests are skipped there.
SUITE_REMINDER = (
    "REQUIRED before stage 2 and again before stage 3: run the full test suite locally WITH the "
    "SDK (ibapi installed, TRADINGBOT_BLOCK_IBAPI unset) at the exact commit you will run, and "
    "record the commit and the passed/skipped counts in the evidence directory "
    "(docs/IBKR_PAPER_ORDERS.md, staged runbook).")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("stage", choices=[*ALL_STAGES, ABORT])
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
    if args.stage == "stage1-lookups":
        print(SUITE_REMINDER)
    return 0 if evidence.get("passed") else 1


if __name__ == "__main__":
    sys.exit(main())
