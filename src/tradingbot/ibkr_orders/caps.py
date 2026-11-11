"""Experiment caps for the paper order mode (SDK-free).

The caps are code constants on purpose: changing any of them is a code change that goes through
review. The private config can only NAME an experiment (``orders = "paper-test-1"``); it cannot
define or widen one. An experiment name that is not a key of ``EXPERIMENTS`` refuses order mode.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from types import MappingProxyType


@dataclass(frozen=True)
class ExperimentCaps:
    name: str
    symbol: str  # the only symbol; its contract ID is the one the session itself resolves
    sec_type: str
    currency: str
    max_order_quantity: int  # per order
    max_net_position: int  # long, in shares, counting working buys
    max_order_notional: Decimal  # quantity x limit (LMT) or stop (STP) price, in `currency`
    max_submissions: int  # every placeOrder: entries, bracket children and amendments
    order_types: frozenset[str]  # IBKR orderType strings
    tifs: frozenset[str]
    long_only: bool = True
    outside_rth_allowed: bool = False
    primary_exchange: str = ""  # used only to resolve the one contract (SMART routing)

    @property
    def ref_prefix(self) -> str:
        """Every order of the experiment carries this before the execution core's own reference
        in ``orderRef``, so the broker's own history can be counted for the experiment."""
        return f"{self.name}:"


PAPER_TEST_1 = ExperimentCaps(
    name="paper-test-1",
    symbol="TLT",
    sec_type="STK",
    currency="USD",
    max_order_quantity=1,
    max_net_position=1,
    max_order_notional=Decimal(150),
    max_submissions=6,
    order_types=frozenset({"LMT", "STP"}),
    tifs=frozenset({"DAY"}),
    long_only=True,
    outside_rth_allowed=False,
    primary_exchange="NASDAQ",
)

# Read-only mapping: the registry cannot be extended at run time through this name.
EXPERIMENTS = MappingProxyType({PAPER_TEST_1.name: PAPER_TEST_1})
