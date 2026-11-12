"""Heartbeat watchdog (``scripts/watchdog.py``): alert when the service stops writing its heartbeat.

It reads ``<state_dir>/heartbeat.json`` and nothing else of the service's state, and never touches a
broker. De-duplication: one HEARTBEAT_STALE when the heartbeat goes missing or stale, again every
``[notify] realert_seconds`` while it stays so, and one HEARTBEAT_RESTORED when it is fresh again;
its own state is ``<state_dir>/watchdog.json``.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from tradingbot.runtime import notify as N
from tradingbot.runtime.app import build_notifier
from tradingbot.runtime.store import write_json_atomic


def check(config, now: datetime, *, max_age: float | None = None, notifier=None) -> dict:
    """One watchdog check. Returns {"stale": bool, "age_seconds": float | None, "notified": str}."""
    limit = max_age or config.heartbeat_stale_after
    notifier = notifier or build_notifier(config)
    state_file = config.paths.state_dir / "watchdog.json"
    try:
        state = json.loads(state_file.read_text())
    except (OSError, ValueError):
        state = {}
    age = None
    try:
        beat = json.loads(Path(config.heartbeat_file).read_text())
        age = (now - datetime.fromisoformat(beat["at"])).total_seconds()
    except (OSError, ValueError, KeyError):
        beat = None
    stale = age is None or age > limit
    notified = ""
    if stale:
        last = state.get("last_notified_at")
        due = last is None or (now - datetime.fromisoformat(last)).total_seconds() >= (
            config.notify.realert_seconds
        )
        if due:
            what = "missing" if beat is None else f"{age:.0f} s old (limit {limit:.0f} s)"
            title = "Service heartbeat stale" if not state.get("stale") else (
                "Still open: Service heartbeat stale")
            notifier.notify(N.HEARTBEAT_STALE, title,
                            f"heartbeat {what}: the service may be stopped or hung")
            state["last_notified_at"] = now.isoformat()
            notified = N.HEARTBEAT_STALE
        state.setdefault("stale_since", now.isoformat())
        state["stale"] = True
    elif state.get("stale"):
        notifier.notify(N.HEARTBEAT_RESTORED, "Service heartbeat restored",
                        f"heartbeat {age:.0f} s old again (stale since {state.get('stale_since')})")
        state = {"stale": False}
        notified = N.HEARTBEAT_RESTORED
    state["checked_at"] = now.isoformat()
    config.paths.state_dir.mkdir(parents=True, exist_ok=True)
    write_json_atomic(state_file, state)
    return {"stale": stale, "age_seconds": age, "limit_seconds": limit, "notified": notified}
