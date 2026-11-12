"""Create the submission journal for the paper order experiment ONCE (offline: opens no socket).

    python scripts/ibkr_paper_init_journal.py [--config PATH]

Reads the private order config (same file-safety rules as every other read), then creates
``<state_dir>/ibkr_paper_orders/<experiment>.submissions`` with its header (experiment name,
masked account, creation time, digest of the code-defined caps) and its head anchor. Refuses if a
journal already exists. The order session never creates the journal; without it nothing can be
placed (SUBMISSION_COUNT_UNKNOWN). See docs/IBKR_PAPER_ORDERS.md.
"""
from __future__ import annotations

import argparse
import sys

from tradingbot.ibkr_orders.config import DEFAULT_ORDER_CONFIG_PATH, load_order_config
from tradingbot.ibkr_orders.counter import JournalExists, init_journal


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default=str(DEFAULT_ORDER_CONFIG_PATH),
                        help=f"private order config (default {DEFAULT_ORDER_CONFIG_PATH})")
    args = parser.parse_args(argv)
    cfg = load_order_config(args.config)
    try:
        path = init_journal(cfg.state_dir, cfg.experiment, cfg.connection.masked_account())
    except JournalExists as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    print(f"journal created for {cfg.experiment.name} "
          f"({cfg.connection.masked_account()}): {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
