"""Broker-backed replay: the Sprint 1.1 coordinator with real order execution behind the gateway.

``BrokerEventReplay`` is a drop-in ``PaperEventReplay``. Provenance, once-only processing, atomic
groups and every authorization decision stay exactly where they are (``gate_source`` /
``gate_action`` inside the base class). Only after a group is approved in full does
``_execute_group`` hand each action to the ``ExecutionCoordinator``; the replay's idea table is
then overwritten from the follower ledger, which moves only on broker fills. The default
``PaperEventReplay`` keeps its fictional fills unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from tradingbot.event_models import (
    ActionOutcome,
    ParsedPost,
    ReplayResult,
    ReplaySource,
    Timing,
    TradeAction,
)
from tradingbot.event_replay import Idea, PaperEventReplay
from tradingbot.execution.coordinator import OPPOSITE_IDEA_IN_SAME_CONTRACT, ExecutionCoordinator
from tradingbot.execution.store import LedgerPosition
from tradingbot.models import Action, ExecutionPlan, GateDecision, MappedQuote, Quote
from tradingbot.validator import ALL_ACTIONS, gate_action

PRIOR_INTENT = "INTENT_ALREADY_ON_RECORD"
# A member of an approved atomic group did not end EXECUTED: the rest of the group is held.
PARTIAL_GROUP = "PARTIAL_GROUP"


@dataclass
class LedgerIdea(Idea):
    """A ledger idea that also carries its contract identity, so the gateway can tell two months
    of one future apart in a follow-up (multi-asset mapping)."""

    contract_key: str | None = None
    # The working backstop and the publisher's own stop: an amendment is judged against both.
    backstop_price: str | None = None
    publisher_stop: str | None = None

    def as_position(self):
        p = super().as_position()
        if p is None:
            return None
        return p.model_copy(update={"contract_key": self.contract_key,
                                    "backstop_price": self.backstop_price,
                                    "publisher_stop": self.publisher_stop})


def _idea(p: LedgerPosition) -> Idea:
    return LedgerIdea(
        idea_id=p.idea_id,
        symbol=p.symbol,
        asset_class=p.asset_class,
        position_side=p.position_side,
        quantity=p.quantity,
        stop_price=p.stop_price,
        target_price=p.target_price,
        status=p.status,
        contract_key=p.contract_key,
        backstop_price=p.backstop_price,
        publisher_stop=p.publisher_stop,
    )


class BrokerEventReplay(PaperEventReplay):
    def __init__(self, db_path: str | Path, coordinator: ExecutionCoordinator):
        # One policy for gate and execution: the coordinator's.
        super().__init__(db_path, coordinator.policy)
        self.coordinator = coordinator
        # The gateway's execution plans for approved actions in mapped families, by action id.
        self._plans: dict[str, ExecutionPlan] = {}

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
        self._plans = {}
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
        self._plans.pop(a.action_id, None)  # action ids repeat across posts: never reuse a plan
        if self.coordinator.intent_for(source, a) is not None:
            return ActionOutcome(action_id=a.action_id, status="SIMULATED", reasons=[PRIOR_INTENT])
        quote = quotes.get(a.instrument.quote_key) if a.instrument is not None else None
        if isinstance(quote, MappedQuote):
            # The gateway is pure: asked with exactly the inputs the base class is about to use
            # (before its staged fill changes the idea), it returns the same decision, and with an
            # approval the plan the execution core must follow. The plan is kept only if the base
            # class's own call approves too.
            idea = ideas.get(a.idea_reference) if a.idea_reference else None
            position = idea.as_position() if idea is not None else None
            decided = gate_action(a, source.published_at, quote, self.policy, position, now=now,
                                  supported_actions=ALL_ACTIONS)
            if decided.status == "APPROVE" and decided.plan is not None:
                self._plans[a.action_id] = decided.plan
        if (a.action == Action.OPEN and a.timing == Timing.CURRENT and a.instrument is not None
                and a.position_side is not None):
            # Review F9: the broker nets one position per contract; never open against an open
            # idea on the other side (a reversal is a CLOSE, then the OPEN). Only a current
            # instruction can open anything: a retrospective report of an opposite entry is the
            # gateway's to RECONCILE, not a held signal.
            clash = self.coordinator.opposite_idea(a.instrument, a.position_side, ideas.values())
            if clash is not None:
                return ActionOutcome(
                    action_id=a.action_id,
                    status="HOLD",
                    reasons=[OPPOSITE_IDEA_IN_SAME_CONTRACT, f"OPEN_IDEA:{clash}"],
                )
        return super()._simulate(source, a, quotes, ideas, now)

    def _execute_group(self, source, group, outcomes, ideas, now) -> list[ActionOutcome]:
        """Execute the approved group in order. In a multi-action group, the first member that
        does not end EXECUTED stops it: every later member is HELD (never sent) and a PARTIAL_GROUP
        exception records the half-done group for a human (safety review M3)."""
        results = []
        stopped: tuple[TradeAction, str] | None = None
        for action, approved in zip(group, outcomes, strict=True):
            if stopped is not None:
                first, status = stopped
                results.append(
                    ActionOutcome(
                        action_id=action.action_id,
                        status="HOLD",
                        reasons=[PARTIAL_GROUP, f"GROUP_STOPPED_AT:{first.action_id}:{status}"],
                    )
                )
                continue
            gate = GateDecision(
                status="APPROVE",
                reasons=list(approved.reasons),
                plan=self._plans.get(action.action_id),
            )
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
            if len(group) > 1 and r.replay_status != "EXECUTED":
                stopped = (action, r.replay_status)
        if stopped is not None:
            self._record_partial_group(source, group, stopped, results)
        ledger = {p.idea_id: p for p in self.coordinator.ledger()}
        for action in group:
            if action.action == Action.OPEN:
                ideas.pop(f"{source.source_id}:{action.action_id}", None)  # drop the fictional fill
        for idea_id, p in ledger.items():
            ideas[idea_id] = _idea(p)
        return results

    def _record_partial_group(self, source, group, stopped, results) -> None:
        first, status = stopped
        store = self.coordinator.store
        at = self.coordinator.clock()
        held = [r.action_id for r in results if r.status == "HOLD" and PARTIAL_GROUP in r.reasons]
        ref = next((r.order_ref for r in results if r.action_id == first.action_id), None)
        done = [r.action_id for r in results if r.status == "EXECUTED"]
        detail = (
            f"atomic group of source {source.source_id} rev {source.revision}: "
            f"{first.action_id} ended {status}; executed {done or 'none'}; held {held}"
        )
        if store.raise_exception(
            at, PARTIAL_GROUP, detail, idea_id=first.idea_reference, order_ref=ref
        ):
            store.log(
                at,
                PARTIAL_GROUP,
                order_ref=ref,
                idea_id=first.idea_reference,
                group=[a.action_id for a in group],
                stopped_at=first.action_id,
                status=status,
                executed=done,
                held=held,
            )
