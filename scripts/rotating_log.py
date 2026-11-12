"""Append stdin to a size-rotated log file (used by ops/run_tradingbot.sh under launchd).

    <command> 2>&1 | python scripts/rotating_log.py <log file> [--max-bytes N] [--keep K]

Lines are written as they arrive; at ``--max-bytes`` the file moves to ``.1`` (``.1`` to ``.2``,
and so on up to ``--keep``). The service's own output is identifiers and reason codes only.
"""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path")
    parser.add_argument("--max-bytes", type=int, default=5_000_000)
    parser.add_argument("--keep", type=int, default=5)
    args = parser.parse_args(argv)
    handler = logging.handlers.RotatingFileHandler(
        args.path, maxBytes=args.max_bytes, backupCount=args.keep, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    log = logging.getLogger("tradingbot.wrapper")
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    for line in sys.stdin:
        log.info(line.rstrip("\n"))
        handler.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
