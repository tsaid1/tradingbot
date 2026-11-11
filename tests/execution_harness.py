"""Shared, invented-data harness for the execution-core tests (no broker, no network)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256
from pathlib import Path

from tradingbot.event_models import ParsedPost, PriceBasis, ReplaySource, Timing, TradeAction
from tradingbot.execution.coordinator import ExecutionConfig, ExecutionCoordinator
from tradingbot.execution.replay import BrokerEventReplay
from tradingbot.execution.sim_broker import SimBroker, SimClock
from tradingbot.models import (
    Action,
    AssetClass,
    GateDecision,
    Instrument,
    MockPolicy,
    PositionSide,
    Quote,
)

START = datetime(2026, 3, 2, 15, 0, tzinfo=UTC)
SYMBOL = "ZZQ"  # invented ticker
INSTRUMENT = Instrument(asset_class=AssetClass.EQUITY, symbol=SYMBOL)
APPROVE = GateDecision(status="APPROVE", reasons=["TEST_APPROVED"])


def source(source_id: str, revision: int = 1, at: datetime = START) -> ReplaySource:
    return ReplaySource(
        source_id=source_id,
        revision=revision,
        published_at=at,
        received_at=at,
        verified_author=True,
        complete_text=True,
        post_hash=sha256(f"invented:{source_id}:{revision}".encode()).hexdigest(),
    )


def action(
    action_id: str,
    act: Action,
    *,
    idea: str | None = None,
    entry: str | None = None,
    stop: str | None = None,
    target: str | None = None,
    fraction: str | None = None,
    side: PositionSide = PositionSide.LONG,
    symbol: str = SYMBOL,
    group: str | None = None,
) -> TradeAction:
    return TradeAction(
        action_id=action_id,
        action=act,
        timing=Timing.CURRENT,
        instrument=Instrument(asset_class=AssetClass.EQUITY, symbol=symbol),
        position_side=side,
        idea_reference=idea,
        atomic_group=group,
        price_basis=PriceBasis.TRADED_CONTRACT,
        source_market_symbol=symbol,
        entry_reference=entry,
        stop_price=stop,
        target_price=target,
        close_fraction=fraction,
        explicit_instruction=True,
        evidence=["invented test instruction"],
    )


def open_action(action_id: str = "open", **kw) -> TradeAction:
    kw.setdefault("entry", "50")
    kw.setdefault("stop", "48")
    kw.setdefault("target", "55")
    return action(action_id, Action.OPEN, **kw)


class Harness:
    """A SimBroker, a coordinator and (optionally) a broker-backed replay, sharing one SimClock."""

    def __init__(
        self, folder: Path, *, policy: MockPolicy | None = None, strict: bool = True, **config
    ):
        self.folder = Path(folder)
        self.strict = strict
        self.clock = SimClock(START)
        self.policy = policy or MockPolicy(
            quantity=4, quantity_per_add=2, max_notional_usd=Decimal(1000)
        )
        self.config = ExecutionConfig(halt_file=self.folder / "HALT", **config)
        self.restart()
        self.broker.set_quote(SYMBOL, "49.98", "50.02")

    def restart(self) -> None:
        """New process: fresh objects on the same SQLite files (in-memory scripts are lost). The old
        process is gone, so its store lock is released first, as the OS would on exit."""
        if getattr(self, "coord", None) is not None:
            self.coord.close()
        self.broker = SimBroker(
            self.folder / "broker.sqlite3", clock=self.clock.now, strict=self.strict
        )
        self.coord = ExecutionCoordinator(
            self.broker,
            self.folder / "exec.sqlite3",
            self.policy,
            self.config,
            clock=self.clock.now,
        )
        self.replay = BrokerEventReplay(self.folder / "replay.sqlite3", self.coord)

    def halt(self) -> None:
        self.config.halt_file.write_text("halt\n")

    def unhalt(self) -> None:
        self.config.halt_file.unlink(missing_ok=True)

    def execute(self, source_id: str, act: TradeAction, revision: int = 1):
        return self.coord.execute(
            source(source_id, revision, self.clock.now()), act, APPROVE, now=self.clock.now()
        )

    def quotes(self, symbol: str = SYMBOL) -> dict[str, Quote]:
        q = self.broker.quote(
            self.broker.resolve(Instrument(asset_class=AssetClass.EQUITY, symbol=symbol))
        )
        return {symbol: Quote(symbol=symbol, bid=q.bid, ask=q.ask, observed_at=q.received_at)}

    def post(self, source_id: str, *actions: TradeAction):
        return self.replay.process(
            source(source_id, at=self.clock.now()),
            ParsedPost(actions=list(actions)),
            self.quotes(),
            now=self.clock.now(),
        )

    def events(self, order_ref: str | None = None) -> list[str]:
        return [e.event for e in self.coord.store.journal(order_ref)]

    def working(self) -> dict[str, object]:
        return {o.order_ref: o for o in self.broker.open_orders()}
