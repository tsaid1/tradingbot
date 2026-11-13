"""Experiment caps for the paper order mode (SDK-free).

The caps are code constants on purpose: changing any of them is a code change that goes through
review. The private config can only NAME an experiment (``orders = "paper-test-1"`` or
``"paper-test-2"``); it cannot
define or widen one. An experiment name that is not a key of ``EXPERIMENTS`` refuses order mode.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
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
    # 2 (stage 2) + 7 (stage 3 at worst: entry, stop, one amendment, and two close attempts of a
    # closing limit plus the stop placed again after it was cancelled unfilled); see experiment.py
    max_submissions=9,
    order_types=frozenset({"LMT", "STP"}),
    tifs=frozenset({"DAY"}),
    long_only=True,
    outside_rth_allowed=False,
    primary_exchange="NASDAQ",
)

# paper-test-2 (YOUR CALL 17; NOT authorized): every rule of paper-test-1 except the cap:
# 1 (stage 2, one unfillable order) + 2 (stage 3, the unfillable bracket) + 7 (stage 4) = 10.
# Its own journal and evidence (both are named after the experiment). See experiment.py.
PAPER_TEST_2 = replace(PAPER_TEST_1, name="paper-test-2", max_submissions=10)

# Hard ceiling of the owner's OPTIONAL stage-3 entry allowance (``stage3_entry_allowance_ticks``
# in the private order config): stage 3 states its entry as the ask plus at most this many min
# ticks. A code constant on purpose; the config can choose 0..this, never more.
MAX_STAGE3_ENTRY_ALLOWANCE_TICKS = 5

# Read-only mapping: the registry cannot be extended at run time through this name.
EXPERIMENTS = MappingProxyType({PAPER_TEST_1.name: PAPER_TEST_1, PAPER_TEST_2.name: PAPER_TEST_2})
