"""Alerts: every state that needs a human notifies at once, once, and again on a schedule.

One alert per *key* (a condition and its subject, e.g. ``UNKNOWN_ENTRY:<order_ref>``) in the runtime
store's ``alerts`` table. De-duplication rule:

* the cycle that first sees a condition opens its alert and notifies in that cycle;
* while it stays open it is not notified again until ``realert_seconds`` have passed since the
  last notification, then again every ``realert_seconds`` ("Still open: ...");
* when the condition is gone the alert is resolved (some kinds notify the clearing too); if it
  comes back later it is a new occurrence and notifies at once.

``event`` is for one-off facts (a reconnect report, an operator action): notified once and kept
in the alert history already resolved.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from tradingbot.runtime.notify import Notifier
from tradingbot.runtime.store import RuntimeStore


class Alerter:
    def __init__(self, store: RuntimeStore, notifier: Notifier, *,
                 realert_seconds: float, clock: Callable[[], datetime]):
        self.store = store
        self.notifier = notifier
        self.realert_seconds = realert_seconds
        self.clock = clock

    def raise_(self, key: str, kind: str, title: str, message: str,
               now: datetime | None = None) -> bool:
        """Open (or keep open) the alert ``key`` and notify when due. True when notified."""
        now = now or self.clock()
        new = self.store.open_alert(key, kind, message, now)
        if not self.store.alert_notify_due(key, now, self.realert_seconds):
            return False
        self.notifier.notify(kind, title if new else f"Still open: {title}", message)
        return True

    def sweep(self, prefix: str, keep: set[str], now: datetime | None = None, *,
              cleared_kind: str | None = None, cleared_title: str = "") -> list[str]:
        """Resolve every open alert under ``prefix`` not in ``keep``; optionally notify each."""
        now = now or self.clock()
        gone = self.store.resolve_alerts(now, keep=keep, prefix=prefix)
        if cleared_kind:
            for key in gone:
                self.notifier.notify(cleared_kind, cleared_title, f"cleared: {key}")
        return gone

    def event(self, key: str, kind: str, title: str, message: str,
              now: datetime | None = None) -> None:
        """A one-off fact: notified once, kept in the history already resolved."""
        now = now or self.clock()
        self.store.open_alert(key, kind, message, now)
        self.store.alert_notify_due(key, now, self.realert_seconds)
        self.store.resolve_alerts(now, keep=set(), prefix=key)
        self.notifier.notify(kind, title, message)
