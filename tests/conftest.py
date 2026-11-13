"""Test-suite switches.

``TRADINGBOT_BLOCK_IBAPI=1`` makes ``import ibapi`` fail, reproducing GitHub CI (where the IBKR SDK is
not installed) on a machine that has it: SDK tests skip via ``pytest.importorskip`` and the pure-logic
tests must still pass.

``TRADINGBOT_TEST_HOST_NOW=<ISO time with offset>`` makes every wall clock the code under test and
the test modules read start at that moment (``tests/_clock.py``), as if the machine's clock said
so: it proves the order-module tests do not depend on when they run.
"""
from __future__ import annotations

import importlib.abc
import os
import sys


class _BlockIbapi(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname == "ibapi" or fullname.startswith("ibapi."):
            raise ModuleNotFoundError(f"ibapi blocked by TRADINGBOT_BLOCK_IBAPI ({fullname})",
                                      name=fullname)


if os.environ.get("TRADINGBOT_BLOCK_IBAPI") == "1":
    for _name in [n for n in sys.modules if n == "ibapi" or n.startswith("ibapi.")]:
        del sys.modules[_name]
    sys.meta_path.insert(0, _BlockIbapi())


def pytest_collection_finish(session):
    moment = os.environ.get("TRADINGBOT_TEST_HOST_NOW")
    if moment:
        from datetime import datetime

        import _clock

        _clock.install()
        _clock.set_now(datetime.fromisoformat(moment))
