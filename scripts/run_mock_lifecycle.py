"""End-to-end execution-core demonstration on invented data, against the simulated broker.

Run: PYTHONPATH=src python scripts/run_mock_lifecycle.py

Posts flow through the existing replay coordinator and deterministic gateway, then through the
ExecutionCoordinator into SimBroker: an entry with a protective stop, an add, a stop amendment, a
partial reduction and a full close; then the failure drills (duplicate delivery, a lost
acknowledgement recovered by lookup, a process restart mid-order, a rejected order, a halt). Every
state lives in a temporary folder; the halt switch used is inside it. No network, no real broker,
invented tickers. Exits non-zero if reconciliation reports any unexplained discrepancy.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory

from tradingbot.event_models import ParsedPost, PriceBasis, ReplaySource, Timing, TradeAction
from tradingbot.execution.coordinator import ExecutionConfig, ExecutionCoordinator
from tradingbot.execution.replay import BrokerEventReplay
from tradingbot.execution.sim_broker import SimBroker, SimClock, SimulatedCrash
from tradingbot.models import Action, AssetClass, Instrument, MockPolicy, PositionSide, Quote

START = datetime(2026, 3, 2, 14, 30, tzinfo=UTC)
POLICY = MockPolicy(quantity=4, quantity_per_add=2)
PRICES = {
    "ZZQ": ("49.98", "50.02"),
    "QQZ": ("19.99", "20.01"),
    "ZQZ": ("29.98", "30.02"),
    "QZQ": ("9.99", "10.01"),
    "ZZZ": ("39.98", "40.02"),
}
ENTRY = {
    "ZZQ": ("50", "48", "55"),
    "QQZ": ("20", "19", "22"),
    "ZQZ": ("30", "28.50", "33"),
    "QZQ": ("10", "9.50", "11"),
    "ZZZ": ("40", "38", "44"),
}


class Demo:
    def __init__(self, folder: Path):
        self.folder = folder
        self.clock = SimClock(START)
        self.config = ExecutionConfig(halt_file=folder / "HALT")
        self.rows: list[list[str]] = []
        self.unexplained = 0
        self.start()
        for symbol, (bid, ask) in PRICES.items():
            self.broker.set_quote(symbol, bid, ask)

    def start(self) -> None:
        """(Re)start the process: new objects over the same SQLite files. The old process's store
        lock is released first, as the OS would on its exit."""
        if getattr(self, "coord", None) is not None:
            self.coord.close()
        self.broker = SimBroker(self.folder / "broker.sqlite3", clock=self.clock.now)
        self.coord = ExecutionCoordinator(
            self.broker,
            self.folder / "execution.sqlite3",
            POLICY,
            self.config,
            clock=self.clock.now,
        )
        self.replay = BrokerEventReplay(self.folder / "replay.sqlite3", self.coord)

    def tick(self) -> None:
        self.clock.advance(5)
        for symbol in PRICES:
            q = self.broker.quote(
                self.broker.resolve(Instrument(asset_class=AssetClass.EQUITY, symbol=symbol))
            )
            self.broker.set_quote(symbol, q.bid, q.ask)  # refresh the timestamp

    def action(
        self, action_id: str, act: Action, symbol: str, idea: str | None = None, **kw
    ) -> TradeAction:
        if act == Action.OPEN:
            entry, stop, target = ENTRY[symbol]
            kw = {"entry_reference": entry, "stop_price": stop, "target_price": target, **kw}
        return TradeAction(
            action_id=action_id,
            action=act,
            timing=Timing.CURRENT,
            instrument=Instrument(asset_class=AssetClass.EQUITY, symbol=symbol),
            position_side=PositionSide.LONG,
            idea_reference=idea,
            price_basis=PriceBasis.TRADED_CONTRACT,
            source_market_symbol=symbol,
            explicit_instruction=True,
            evidence=["invented demo instruction"],
            **kw,
        )

    def post(self, step: str, source_id: str, act: TradeAction):
        now = self.clock.now()
        src = ReplaySource(
            source_id=source_id,
            published_at=now,
            received_at=now,
            verified_author=True,
            complete_text=True,
            post_hash=sha256(f"invented:{source_id}".encode()).hexdigest(),
        )
        symbol = act.instrument.symbol
        q = self.broker.quote(self.broker.resolve(act.instrument))
        quotes = {symbol: Quote(symbol=symbol, bid=q.bid, ask=q.ask, observed_at=q.received_at)}
        result = self.replay.process(src, ParsedPost(actions=[act]), quotes, now=now)
        self.record(step, source_id, act, result)
        self.tick()
        return result

    def record(self, step, source_id, act, result) -> None:
        o = result.outcomes[0]
        idea = act.idea_reference or f"{source_id}:{act.action_id}"
        gate = next((r for r in o.reasons if r.startswith(("PAPER_", "INTENT_"))), o.status)
        fills = self.coord.store.fills(o.order_ref) if o.order_ref else []
        qty = sum(f["quantity"] for f in fills)
        fill_txt = f"{qty}@{fills[-1]['price']}" if fills else "-"
        if act.action == Action.MODIFY and o.status == "EXECUTED":
            fill_txt = "amend"
        pos = self.coord.store.position(idea)
        working = {w.order_ref: w for w in self.broker.open_orders()}
        prot = []
        if pos is not None:
            for ref, tag in ((pos.stop_ref, "STP"), (pos.target_ref, "TGT")):
                w = working.get(ref)
                if w is not None:
                    prot.append(f"{tag} {w.remaining}@{w.stop_price or w.limit_price}")
        report = self.coord.reconcile()
        self.unexplained += len(report.blocking)
        recon = "clean" if report.clean else ",".join(d.kind.value for d in report.blocking)
        broker_state = next(
            (r[len("BROKER_") :] for r in o.reasons if r.startswith("BROKER_")), "-"
        )
        self.rows.append(
            [
                step,
                source_id,
                act.action.value,
                act.instrument.symbol,
                gate,
                o.order_ref or "-",
                f"{o.status}/{broker_state}",
                fill_txt,
                " ".join(prot) or "-",
                f"{pos.quantity if pos else 0}",
                recon,
            ]
        )

    def table(self) -> str:
        head = [
            "step",
            "post",
            "action",
            "sym",
            "gate",
            "order_ref",
            "outcome/broker",
            "fills",
            "protective orders",
            "held",
            "reconcile",
        ]
        rows = [head, *self.rows]
        widths = [max(len(r[i]) for r in rows) for i in range(len(head))]
        line = lambda r: "  ".join(c.ljust(w) for c, w in zip(r, widths, strict=True))
        return "\n".join([line(head), line(["-" * w for w in widths]), *map(line, self.rows)])

    def journal(self, order_ref: str) -> str:
        return " -> ".join(e.event for e in self.coord.store.journal(order_ref))


def main() -> int:
    with TemporaryDirectory(prefix="tradingbot-exec-") as folder:
        d = Demo(Path(folder))
        a = d.action
        idea = "lc-open:open"
        # ---- lifecycle
        d.post("entry+stop", "lc-open", a("open", Action.OPEN, "ZZQ"))
        d.post("add", "lc-add", a("add", Action.ADD, "ZZQ", idea, entry_reference="50"))
        d.post("stop amend", "lc-stop", a("raise", Action.MODIFY, "ZZQ", idea, stop_price="49"))
        d.post(
            "partial reduce", "lc-trim", a("trim", Action.REDUCE, "ZZQ", idea, close_fraction="0.5")
        )
        d.post("full close", "lc-close", a("close", Action.CLOSE, "ZZQ", idea))
        # ---- drills
        dup = d.post("duplicate", "lc-open", a("open", Action.OPEN, "ZZQ"))
        d.broker.script_next_submit("LOST_ACK_PLACED")
        lost = d.post("lost ack", "dr-lost", a("open", Action.OPEN, "QQZ"))
        d.broker.script_next_submit("CRASH_AFTER_PLACE")
        try:
            d.post("crash", "dr-crash", a("open", Action.OPEN, "ZQZ"))
        except SimulatedCrash:
            d.rows.append(
                [
                    "crash",
                    "dr-crash",
                    "OPEN",
                    "ZQZ",
                    "PAPER_OPEN",
                    "-",
                    "SimulatedCrash",
                    "(at broker)",
                    "-",
                    "-",
                    "(process died)",
                ]
            )
        d.start()  # restart: new process, same files
        restarted = d.post("restart+redeliver", "dr-crash", a("open", Action.OPEN, "ZQZ"))
        d.broker.script_next_submit("REJECT", reason="INVENTED_REJECT_REASON")
        d.post("rejected", "dr-reject", a("open", Action.OPEN, "QZQ"))
        d.config.halt_file.write_text("halt\n")
        d.post("halt", "dr-halt", a("open", Action.OPEN, "ZZZ"))
        d.config.halt_file.unlink()

        print("Execution core: mock lifecycle on invented data (SimBroker, no network)\n")
        print(d.table())
        print("\nJournal excerpts")
        print(f"  lost ack : {d.journal(lost.outcomes[0].order_ref)}")
        print(f"  restart  : {d.journal(restarted.outcomes[0].order_ref)}")
        print(
            f"  duplicate: {dup.duplicate=}, broker orders under ref "
            f"{len(d.broker.orders_with_ref(dup.outcomes[0].order_ref))}"
        )
        d.coord.sync()
        final = d.coord.reconcile()
        print("\nFinal reconciliation")
        print(
            f"  account {final.account}: ledger ideas open {final.ledger_ideas_open}, "
            f"broker positions "
            f"{final.broker_positions}, working orders {final.broker_open_orders}"
        )
        print(f"  discrepancies {len(final.discrepancies)}, unexplained {len(final.unexplained)}")
        for item in final.discrepancies:
            print(
                f"   - {item.kind.value} {item.symbol} {item.detail} "
                f"explained_by={item.explained_by}"
            )
        bad = d.unexplained + len(final.blocking)
        print(f"\nRESULT: {'OK' if bad == 0 else 'FAIL'} ({bad} unexplained discrepancies)")
        return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
