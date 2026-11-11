"""Broker-backed replay: the Sprint 1.1 coordinator with real order execution behind the gateway.

``BrokerEventReplay`` is a drop-in ``PaperEventReplay``. Provenance, once-only processing, atomic
groups and every authorization decision stay exactly where they are (``gate_source`` /
``gate_action`` inside the base class). Only after a group is approved in full does
``_execute_group`` hand each action to the ``ExecutionCoordinator``; the replay's idea table is
then overwritten from the follower ledger, which moves only on broker fills. The default
``PaperEventReplay`` keeps its fictional fills unchanged.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from tradingbot.event_models import (
    ActionOutcome,
    ParsedPost,
    ReplayResult,
    ReplaySource,
    TradeAction,
)
from tradingbot.event_replay import Idea, PaperEventReplay
from tradingbot.execution.coordinator import ExecutionCoordinator
from tradingbot.execution.store import LedgerPosition
from tradingbot.models import Action, GateDecision, Quote

PRIOR_INTENT = "INTENT_ALREADY_ON_RECORD"


def _idea(p: LedgerPosition) -> Idea:
    return Idea(
        idea_id=p.idea_id,
        symbol=p.symbol,
        asset_class=p.asset_class,
        position_side=p.position_side,
        quantity=p.quantity,
        stop_price=p.stop_price,
        target_price=p.target_price,
        status=p.status,
    )


class BrokerEventReplay(PaperEventReplay):
    def __init__(self, db_path: str | Path, coordinator: ExecutionCoordinator):
        # One policy for gate and execution: the coordinator's.
        super().__init__(db_path, coordinator.policy)
        self.coordinator = coordinator

    def process(
        self,
        source: ReplaySource,
        parsed: ParsedPost,
        quotes: dict[str, Quote] | None = None,
        *,
        now: datetime | None = None,
    ) -> ReplayResult:
        # Before deciding anything: settle unknown outcomes and apply broker fills (e.g. a stop that
        # triggered), so the gateway sees the follower position as the broker actually holds it.
        self.coordinator.recover()
        return super().process(source, parsed, quotes, now=now)

    def _load_ideas(self, c):
        ideas = super()._load_ideas(c)
        for p in self.coordinator.ledger():
            ideas[p.idea_id] = _idea(p)
        return ideas

    def _simulate(self, source, a: TradeAction, quotes, ideas, now) -> ActionOutcome:
        # An intent for this exact (source, revision, action) exists only if an earlier pass was
        # approved and reached execution, then died before the replay committed (a crash mid-order).
        # Its gate decision is on the intent; the coordinator adopts it rather than ordering again.
        if self.coordinator.intent_for(source, a) is not None:
            return ActionOutcome(action_id=a.action_id, status="SIMULATED", reasons=[PRIOR_INTENT])
        return super()._simulate(source, a, quotes, ideas, now)

    def _execute_group(self, source, group, outcomes, ideas, now) -> list[ActionOutcome]:
        results = []
        for action, approved in zip(group, outcomes, strict=True):
            gate = GateDecision(status="APPROVE", reasons=list(approved.reasons))
            r = self.coordinator.execute(source, action, gate, now=now)
            reasons = [
                *r.gate_reasons,
                f"BROKER_{r.state.value}",
                f"PROTECTION_{r.protection}",
                *r.reasons,
            ]
            if PRIOR_INTENT in approved.reasons:
                reasons.append(PRIOR_INTENT)
            if r.duplicate:
                reasons.append("DUPLICATE_DELIVERY_NO_NEW_ORDER")
            results.append(
                ActionOutcome(
                    action_id=action.action_id,
                    status=r.replay_status,
                    reasons=reasons,
                    order_ref=r.order_ref,
                )
            )
        ledger = {p.idea_id: p for p in self.coordinator.ledger()}
        for action in group:
            if action.action == Action.OPEN:
                ideas.pop(f"{source.source_id}:{action.action_id}", None)  # drop the fictional fill
        for idea_id, p in ledger.items():
            ideas[idea_id] = _idea(p)
        return results
