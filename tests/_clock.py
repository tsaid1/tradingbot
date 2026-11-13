"""TEST-ONLY wall clock control (no third-party time library).

``ShiftedDatetime`` is a ``datetime`` subclass whose ``now()`` is the real time plus ``offset``:
time keeps flowing (durations, monotonic waits and the adapter's monotonic stamps stay real), but
every wall-clock reading through a module's ``datetime`` name lands at a chosen moment. ``install``
points the ``datetime`` name of every loaded ``tradingbot`` module and every test module at it.

Two users:

* the conftest switch ``TRADINGBOT_TEST_HOST_NOW=<ISO time>`` (whole session): "the machine's clock
  reads this moment", to prove the order-module tests do not depend on when they run;
* the experiment tests' fixture (``pin``), which puts every wall clock the experiment, the guard,
  the session, the execution core and the fake TWS read at its own invented moment.
"""
from __future__ import annotations

import sys
from datetime import UTC, timedelta
from datetime import datetime as _real

_SYSTEM = _real  # the original class


class _Meta(type(_real)):
    def __instancecheck__(cls, obj):  # a real datetime is still a datetime
        return isinstance(obj, _SYSTEM)

    def __subclasscheck__(cls, sub):
        return issubclass(sub, _SYSTEM)


class ShiftedDatetime(_real, metaclass=_Meta):
    offset = timedelta(0)

    @classmethod
    def now(cls, tz=None):
        return _SYSTEM.now(tz) + cls.offset

    @classmethod
    def utcnow(cls):  # pragma: no cover - not used by the code under test
        return _SYSTEM.now(UTC).replace(tzinfo=None) + cls.offset


# modules the code under test may import lazily: loaded first so that install() reaches them
_EAGER = ("tradingbot.execution.reconcile", "tradingbot.execution.settlement",
          "tradingbot.execution.coordinator", "tradingbot.execution.store",
          "tradingbot.ibkr_orders.experiment", "tradingbot.ibkr_orders.session",
          "tradingbot.ibkr_orders.counter", "tradingbot.ibkr_orders.broker",
          "tradingbot.ibkr.session")


def _targets():
    for name, mod in list(sys.modules.items()):
        if mod is None:
            continue
        if not name.startswith(("tradingbot", "test_", "_ibkr", "execution_harness",
                                "runtime_harness")):
            continue
        if getattr(mod, "datetime", None) in (_SYSTEM, ShiftedDatetime):
            yield mod


def install() -> None:
    """Point every loaded tradingbot / test module's ``datetime`` name at ``ShiftedDatetime``."""
    import importlib
    for name in _EAGER:
        try:
            importlib.import_module(name)
        except ImportError:  # the SDK-backed modules under TRADINGBOT_BLOCK_IBAPI=1
            pass
    for mod in _targets():
        mod.datetime = ShiftedDatetime


def set_now(moment: _real | None) -> None:
    """Wall clocks read ``moment`` from now on (and keep running from there); None: real time."""
    ShiftedDatetime.offset = timedelta(0) if moment is None else moment - _SYSTEM.now(UTC)
