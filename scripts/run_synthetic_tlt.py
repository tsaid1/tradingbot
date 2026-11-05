"""Offline paper-only TLT lifecycle demonstration using fictional prices.

Run: python scripts/run_synthetic_tlt.py
No network access, keys, real charts, broker or proprietary content needed.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory

from tradingbot.event_models import (
    ParsedPost, PriceBasis, ReplaySource, Timing, TradeAction,
)
from tradingbot.event_replay import PaperEventReplay
from tradingbot.models import Action, AssetClass, Instrument, PositionSide, Quote

STAMP = datetime(2026, 11, 5, 14, 30, tzinfo=timezone.utc)
TLT = Instrument(asset_class=AssetClass.ETF, symbol="TLT")


def src(source_id: str) -> ReplaySource:
    return ReplaySource(
        source_id=source_id, published_at=STAMP, received_at=STAMP,
        post_hash=sha256(f"fictional:{source_id}".encode()).hexdigest(),
        verified_author=True, complete_text=True,
    )


def a(action_id: str, act: Action, *, idea: str | None = None,
      timing: Timing = Timing.CURRENT, opening: bool = False) -> TradeAction:
    return TradeAction(
        action_id=action_id, action=act, timing=timing,
        instrument=TLT, position_side=PositionSide.LONG,
        idea_reference=idea, price_basis=PriceBasis.TRADED_CONTRACT,
        source_market_symbol="TLT", entry_reference="100" if opening else None,
        stop_price="95" if opening else None,
        target_price="110" if opening else None,
        explicit_instruction=True, evidence=["Fictional synthetic instruction"],
    )


def main():
    with TemporaryDirectory(prefix="tradingbot-paper-") as folder:
        book = PaperEventReplay(Path(folder) / "paper.db")
        quotes = {"TLT": Quote(symbol="TLT", bid=Decimal("99.95"),
                               ask=Decimal("100.05"), observed_at=STAMP)}
        sequence = [
            ("old-open", [a("open", Action.OPEN, opening=True)]),
            ("old-close", [a("close", Action.CLOSE, idea="old-open:open")]),
            ("new-open", [a("open", Action.OPEN, opening=True)]),
            # A report of the old stop is a reconciliation event, not a SELL.
            ("old-report", [a("report", Action.CLOSE, idea="old-open:open",
                              timing=Timing.HISTORICAL)]),
            # A current instruction must reference the new idea explicitly.
            ("new-close", [a("close", Action.CLOSE, idea="new-open:open")]),
        ]
        for sid, actions in sequence:
            result = book.process(src(sid), ParsedPost(actions=actions), quotes, now=STAMP)
            print(sid, [(o.status, o.order.side if o.order else "NO_ORDER")
                        for o in result.outcomes])
        print("FINAL IDEAS:", [(i["idea_id"], i["status"], i["quantity"])
                               for i in book.ideas()])
        duplicate = book.process(src("new-close"), ParsedPost(actions=[]), quotes, now=STAMP)
        print("DUPLICATE CLOSE BLOCKED:", duplicate.duplicate)


if __name__ == "__main__":
    main()
