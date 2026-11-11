"""Operator notifications: a log file, and optionally a local macOS notification.

Messages carry identifiers, reason codes and counts only, never post text or credentials. A notifier
must not raise: a failed notification is never allowed to stop the service.
"""

from __future__ import annotations

import contextlib
import json
import subprocess
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, runtime_checkable

# The events the service notifies on.
RECONCILIATION_DISCREPANCY = "RECONCILIATION_DISCREPANCY"
UNPROTECTED_POSITION = "UNPROTECTED_POSITION"
HALT_ENGAGED = "HALT_ENGAGED"
SOURCE_FAILURE = "SOURCE_FAILURE"
BROKER_FAILURE = "BROKER_FAILURE"
STUCK_INTENT = "STUCK_INTENT"


@runtime_checkable
class Notifier(Protocol):
    def notify(self, kind: str, title: str, message: str) -> None: ...


class LogFileNotifier:
    def __init__(self, path: str | Path, clock: Callable[[], datetime] | None = None):
        self.path = Path(path)
        self.clock = clock or (lambda: datetime.now(UTC))

    def notify(self, kind: str, title: str, message: str) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            line = {
                "at": self.clock().isoformat(),
                "kind": kind,
                "title": title,
                "message": message,
            }
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(line, sort_keys=True) + "\n")
        except OSError:
            pass


def _applescript_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


class MacOSNotifier:
    """``osascript -e 'display notification ...'``. The runner is injectable for tests."""

    def __init__(self, runner: Callable[..., object] | None = None, platform: str = sys.platform):
        self.runner = runner or subprocess.run
        self.enabled = platform == "darwin"

    def notify(self, kind: str, title: str, message: str) -> None:
        if not self.enabled:
            return
        script = (
            f"display notification {_applescript_string(message[:240])} "
            f"with title {_applescript_string('TradingBot: ' + title)} "
            f"subtitle {_applescript_string(kind)}"
        )
        with contextlib.suppress(Exception):  # a notification never stops the service
            self.runner(["osascript", "-e", script], check=False, timeout=5, capture_output=True)


class FanoutNotifier:
    def __init__(self, notifiers: Sequence[Notifier]):
        self.notifiers = list(notifiers)

    def notify(self, kind: str, title: str, message: str) -> None:
        for n in self.notifiers:
            with contextlib.suppress(Exception):
                n.notify(kind, title, message)
