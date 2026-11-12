"""Operator notifications: channels behind one small interface.

Messages carry identifiers, reason codes and counts only, never post text or credentials. A notifier
must not raise: a failed notification is never allowed to stop the service.

Channels are registered by name (``register_notifier``) and chosen in the config
(``[notify] channels``). Built in: ``log`` (JSON lines, the existing notifications log),
``alerts_file`` (a durable, size-rotated JSON-lines alerts file the dashboard reads) and ``macos``
(local notification centre). A further channel is one module in ``tradingbot.runtime.channels``
that defines a class with ``notify(kind, title, message)`` and registers it with
``@register_notifier("name")``; naming it in the config is then the only other change. No external
channel ships: none is approved.
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
# A publisher-driven exit or amendment refused because the emergency halt is engaged (review F12).
HALTED_ACTION_REFUSED = "HALTED_ACTION_REFUSED"
# Immediate alerts (operations controls).
HALT_CLEARED = "HALT_CLEARED"
ENTRY_UNKNOWN_OUTCOME = "ENTRY_UNKNOWN_OUTCOME"
REVERSAL = "LEDGER_REVERSAL"
EXIT_REFUSED = "EXIT_REFUSED"
EXECUTION_EXCEPTION = "EXECUTION_EXCEPTION"
SETTLEMENT_UNVERIFIED = "SETTLEMENT_UNVERIFIED"
# The broker contradicts an operator settlement (a fill or an order of an intent declared never
# filled): exposure the follower did not intend (review F1).
SETTLEMENT_CONTRADICTED = "SETTLEMENT_CONTRADICTED"
# The broker's executions for a FILLED settlement differ from the declaration (review F10).
SETTLEMENT_DIFFERS = "SETTLEMENT_DIFFERS_FROM_BROKER"
MAPPING_REVOKED_OPEN_POSITION = "MAPPING_REVOKED_OPEN_POSITION"
ROLL_DUE = "ROLL_DUE_FLATTEN_REQUIRED"
PIPELINE_FAILURE = "PIPELINE_FAILURE"
SOURCE_STALLED = "SOURCE_STALLED"
BROKER_RECONNECTED = "BROKER_RECONNECTED"
# Shadow mode: the quote-stream cap is full of contracts the simulation depends on (no stream for
# a new contract: its quote is unavailable and its entries hold).
SHADOW_STREAM_CAP_REACHED = "SHADOW_STREAM_CAP_REACHED"
BROKER_IDENTITY_MISMATCH = "BROKER_IDENTITY_MISMATCH"
HEARTBEAT_STALE = "HEARTBEAT_STALE"
HEARTBEAT_RESTORED = "HEARTBEAT_RESTORED"
OPERATOR_ACTION = "OPERATOR_ACTION"
# The operator queue is missing, lost or corrupt: operator actions are disabled (review F3).
OPERATOR_QUEUE_UNAVAILABLE = "OPERATOR_QUEUE_UNAVAILABLE"


@runtime_checkable
class Notifier(Protocol):
    def notify(self, kind: str, title: str, message: str) -> bool | None: ...


def deliver(notifier, kind: str, title: str, message: str) -> bool:
    """Notify and say whether it was accepted (review note c): False when the notifier raised or
    returned False; a notifier that returns nothing is taken as accepted unless it raised."""
    try:
        return notifier.notify(kind, title, message) is not False
    except Exception:  # noqa: BLE001 - a notification never stops the service
        return False


class LogFileNotifier:
    durable = True

    def __init__(self, path: str | Path, clock: Callable[[], datetime] | None = None):
        self.path = Path(path)
        self.clock = clock or (lambda: datetime.now(UTC))

    def notify(self, kind: str, title: str, message: str) -> bool:
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
            return False
        return True


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
    """Every channel, in order. Accepted (True) when at least one durable channel (``durable =
    True``: the log, the alerts file) accepted it; with no durable channel configured, any
    channel's acceptance counts."""

    def __init__(self, notifiers: Sequence[Notifier]):
        self.notifiers = list(notifiers)

    def notify(self, kind: str, title: str, message: str) -> bool:
        any_durable = any(getattr(n, "durable", False) for n in self.notifiers)
        accepted = False
        for n in self.notifiers:
            if deliver(n, kind, title, message) and (
                getattr(n, "durable", False) or not any_durable
            ):
                accepted = True
        return accepted


class AlertsFileNotifier:
    """A durable local alerts file (JSON lines) the dashboard reads. Rotated by size: at
    ``max_bytes`` the file moves to ``.1`` (older ones to ``.2`` ... ``.<keep>``)."""

    durable = True

    def __init__(self, path: str | Path, clock: Callable[[], datetime] | None = None, *,
                 max_bytes: int = 1_000_000, keep: int = 3):
        self.path = Path(path)
        self.clock = clock or (lambda: datetime.now(UTC))
        self.max_bytes = max_bytes
        self.keep = keep

    def _rotate(self) -> None:
        if not self.path.exists() or self.path.stat().st_size < self.max_bytes:
            return
        for n in range(self.keep - 1, 0, -1):
            older = self.path.with_name(f"{self.path.name}.{n}")
            if older.exists():
                older.replace(self.path.with_name(f"{self.path.name}.{n + 1}"))
        self.path.replace(self.path.with_name(f"{self.path.name}.1"))

    def notify(self, kind: str, title: str, message: str) -> bool:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._rotate()
            line = {"at": self.clock().isoformat(), "kind": kind, "title": title,
                    "message": message}
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(line, sort_keys=True) + "\n")
        except OSError:
            return False
        return True


def read_alerts_file(path: str | Path, limit: int = 200) -> list[dict]:
    """The newest ``limit`` lines of an alerts file, newest first (missing file: empty)."""
    p = Path(path)
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines()[-limit:]:
        with contextlib.suppress(ValueError):
            out.append(json.loads(line))
    return list(reversed(out))


# ----------------------------------------------------------------------------- channel registry


class ChannelContext:
    """What a channel factory may use: the configured files and the clock."""

    def __init__(self, *, log_file: Path, alerts_file: Path,
                 clock: Callable[[], datetime] | None = None):
        self.log_file = Path(log_file)
        self.alerts_file = Path(alerts_file)
        self.clock = clock


NOTIFIER_FACTORIES: dict[str, Callable[[ChannelContext], Notifier]] = {}


def register_notifier(name: str):
    def deco(factory):
        NOTIFIER_FACTORIES[name] = factory
        return factory

    return deco


register_notifier("log")(lambda ctx: LogFileNotifier(ctx.log_file, ctx.clock))
register_notifier("alerts_file")(lambda ctx: AlertsFileNotifier(ctx.alerts_file, ctx.clock))
register_notifier("macos")(lambda ctx: MacOSNotifier())


def _load_channel_modules() -> None:
    """Import every module of ``tradingbot.runtime.channels`` so each can register itself."""
    import importlib
    import pkgutil

    from tradingbot.runtime import channels

    for mod in pkgutil.iter_modules(channels.__path__):
        importlib.import_module(f"{channels.__name__}.{mod.name}")


def build_channels(names: Sequence[str], ctx: ChannelContext) -> FanoutNotifier:
    """The named channels, in order. An unknown name raises ValueError (the config is refused)."""
    _load_channel_modules()
    unknown = [n for n in names if n not in NOTIFIER_FACTORIES]
    if unknown:
        raise ValueError(f"unknown notification channel(s): {', '.join(unknown)}")
    return FanoutNotifier([NOTIFIER_FACTORIES[n](ctx) for n in dict.fromkeys(names)])
