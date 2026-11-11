"""TEST-ONLY seams for the order session (requires the real ``ibapi``).

The production ``PaperOrderSession`` takes no argument that replaces or skips its local checks or
its clock. Offline tests reach those two seams through this subclass, which lives in the tests
package only: it stubs the local profile/listener check (there is no paper TWS here) and can pin
the clock. Everything else (identity gate, order-mode config read, guard, counter) is the
production code, unchanged.
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from tradingbot.ibkr_orders.session import PaperOrderSession

FAST = {"connect_timeout": 2.0, "request_timeout": 2.0, "quote_timeout": 2.0,
        "stream_seconds": 1.0, "stream_settle_seconds": 0.05, "shutdown_timeout": 3.0,
        "ack_timeout": 0.4, "cancel_wait": 1.0}


def stub_preflight(cfg) -> dict:
    return {"stub": True}


class OfflineTestSession(PaperOrderSession):
    test_clock: Callable[[], datetime] | None = None

    def __init__(self, config, **timeouts) -> None:
        super().__init__(config, **{**FAST, **timeouts})
        self._preflight = stub_preflight

    def _now(self) -> datetime:
        return self.test_clock() if self.test_clock is not None else super()._now()
