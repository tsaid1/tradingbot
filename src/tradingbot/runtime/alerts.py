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

Delivery (review note c): an alert counts as notified only once a durable channel has accepted it
(``deliver``); until then it stays open and un-notified, ``raise_`` retries it on the next cycle
and ``retry_undelivered`` retries one-off events, so a failed channel never loses an alert.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from tradingbot.runtime.notify import Notifier, deliver
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
        self.store.open_alert(key, kind, message, now)
        if not self.store.alert_notify_due(key, now, self.realert_seconds):
            return False
        first = not self._notified(key)
        if not deliver(self.notifier, kind, title if first else f"Still open: {title}", message):
            return False  # stays open and due: retried next cycle
        self.store.mark_alert_notified(key, now)
        return True

    def _notified(self, key: str) -> bool:
        return any(a["key"] == key and a["notified"] for a in self.store.alerts(open_only=True))

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
        if deliver(self.notifier, kind, title, message):
            self.store.mark_alert_notified(key, now)
            self.store.resolve_alerts(now, keep=set(), prefix=key)
        # Otherwise it stays open and un-notified until retry_undelivered delivers it.

    def retry_undelivered(self, now: datetime | None = None) -> list[str]:
        """Deliver every one-off event (``EVENT:`` key) still open because no channel accepted it.
        Returns the keys delivered now."""
        now = now or self.clock()
        done = []
        for a in self.store.alerts(open_only=True):
            if not a["key"].startswith("EVENT:") or a["notified"]:
                continue
            title = a["kind"].replace("_", " ").capitalize() + " (delivery retried)"
            if deliver(self.notifier, a["kind"], title, a["detail"]):
                self.store.mark_alert_notified(a["key"], now)
                self.store.resolve_alerts(now, keep=set(), prefix=a["key"])
                done.append(a["key"])
        return done
