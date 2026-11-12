#!/usr/bin/env python3
"""Heartbeat watchdog: alert when the runtime service stops writing its heartbeat.

    PYTHONPATH=src python scripts/watchdog.py --config runtime.toml [--max-age 300]

Run it on a schedule (the launchd template runs it every 60 s). It reads
``<state_dir>/heartbeat.json`` and nothing else of the service's state; it never touches a broker.
A missing or stale heartbeat raises HEARTBEAT_STALE through the configured notification channels,
once, then again every ``[notify] realert_seconds`` while it stays stale; a fresh heartbeat after
an alert sends HEARTBEAT_RESTORED. Its own de-duplication state is ``<state_dir>/watchdog.json``.

Exit codes: 0 heartbeat fresh, 1 stale or missing, 2 config refused.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime

from tradingbot.runtime.config import DEFAULT_CONFIG_PATH, ConfigError, load_config
from tradingbot.runtime.watchdog import check


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    parser.add_argument("--max-age", type=float, default=None,
                        help="seconds (default: service.heartbeat_stale_after_seconds)")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        result = check(config, datetime.now(UTC), max_age=args.max_age)
    except ConfigError as exc:
        print(f"refusing: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result))
    return 1 if result["stale"] else 0


if __name__ == "__main__":
    sys.exit(main())
