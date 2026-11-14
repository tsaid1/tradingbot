"""Operator confirmation in the running service (docs/OPERATOR_CONFIRMATION.md).

Created by ``RuntimeService`` only when ``[operator_confirmation] enabled = true``; with the switch
off nothing here runs and nothing it owns exists. It offers, refreshes, invalidates and applies
offers; the pure decisions are in ``runtime.confirmation``.

The apply step is an ORDER PATH (AGENTS.md rule 4): a confirmed offer's action is evaluated again
by ``BrokerEventReplay.process_confirmed``, which runs ``gate_source``, the gateway and the
execution core unchanged, at that moment, on a fresh quote. Nothing the operator enters becomes an
order value: a request carries only the offer id and its hash. Journal, alerts and the offers table
carry identifiers, codes, numbers and times, never post text.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from tradingbot.event_models import ParsedPost
from tradingbot.execution import settlement
from tradingbot.execution.broker import (
    ANCHOR_ENVIRONMENTS,
    BrokerError,
    QuoteQuality,
    broker_environment,
)
from tradingbot.execution.coordinator import order_ref_for
from tradingbot.execution.operator_catalog import offer_phrase
from tradingbot.models import Action, MockPolicy, Quote
from tradingbot.runtime import confirmation as C
from tradingbot.runtime import confirmation_table as T
from tradingbot.runtime import notify as N
from tradingbot.runtime import operator_actions as Q
from tradingbot.runtime.config import ConfigError
from tradingbot.runtime.mapping import FamilyMapping, MappingDecision

OFFERED, CONFIRMED, APPLYING = "OFFERED", "CONFIRMED", "APPLYING"
EXECUTED, HELD, REJECTED = "EXECUTED", "HELD", "REJECTED"
DISMISSED, EXPIRED, INVALIDATED = "DISMISSED", "EXPIRED", "INVALIDATED"
OPEN_STATES = (OFFERED,)
ENVIRONMENT_REFUSED = ("operator confirmation is paper and simulation only: the broker must "
                       "declare SIM or PAPER (mock and shadow modes only)")


def environment_ok(broker) -> bool:
    return broker_environment(broker) in ANCHOR_ENVIRONMENTS


class ConfirmationDesk:
    def __init__(self, service):
        self.svc = service
        if service.config.mode not in ("mock", "shadow") or not environment_ok(service.broker):
            raise ConfigError(ENVIRONMENT_REFUSED)
        self.store = service.store
        self.store.ensure_offers()
        self.lexicon_hash = C.lexicon_hash(service.mapping)
        self._recover_at_start(service.clock())

    # ------------------------------------------------------------------ helpers

    @property
    def policy(self) -> MockPolicy:
        return self.svc.config.policy

    def _event(self, at: datetime, event: str, **detail) -> None:
        self.store.event(at, event, **detail)

    def _end(self, row: dict, state: str, reason: str, at: datetime, **extra) -> bool:
        if not self.store.set_offer_state(row["id"], expect=(row["state"],), state=state, at=at,
                                          reason=reason):
            return False
        event = {EXPIRED: "CONFIRMATION_EXPIRED", INVALIDATED: "CONFIRMATION_INVALIDATED"}.get(
            state, f"CONFIRMATION_{state}")
        self._event(at, event, offer_id=row["id"], hash=row["hash"][:8],
                    source_id=row["source_id"], revision=row["revision"],
                    action_id=row["action_id"], reason=reason, **extra)
        return True

    def _quotes(self, offer: C.Offer, entry: FamilyMapping, now: datetime) -> dict[str, Quote]:
        """The gateway's quote for the offer's instrument, read now, exactly as ``_gate_quotes``
        builds it for an approved action (the stated level as the action's anchor)."""
        instrument = offer.proposal.instrument
        if not entry.legacy_equivalent:
            q = self.svc._mapped_quote(entry, instrument, now)
            return {instrument.quote_key: C.anchored_quote(offer, q)}
        try:
            q = self.svc.broker.quote(self.svc.broker.resolve(instrument))
        except BrokerError:
            return {}
        if q.quality == QuoteQuality.UNAVAILABLE or q.bid is None or q.ask is None:
            return {}
        return {instrument.symbol: Quote(symbol=instrument.symbol, bid=q.bid, ask=q.ask,
                                         observed_at=q.received_at)}

    def _offer(self, row: dict) -> C.Offer | None:
        try:
            return C.Offer.from_record(row["record"])
        except (ValueError, KeyError, TypeError):
            return None

    # ------------------------------------------------------------------ start

    def _recover_at_start(self, now: datetime) -> None:
        """An offer left CONFIRMED or APPLYING by a crash is NEVER evaluated again: the execution
        core's own recovery decides an intent that exists; otherwise it ends REJECTED. An open
        offer bound to a mapping entry or lexicon that changed with the restart ends."""
        for row in self.store.offers(states=(CONFIRMED, APPLYING)):
            ref = order_ref_for(row["source_id"], row["revision"], row["action_id"])
            intent = self.svc.coordinator.intent(ref)
            if intent is not None:
                self.store.set_offer_state(
                    row["id"], expect=(row["state"],), state=EXECUTED, at=now,
                    reason="RECOVERED_BY_EXECUTION_CORE",
                    result={"status": intent.state.value, "order_ref": ref})
                self._event(now, "CONFIRMATION_RESULT", offer_id=row["id"],
                            hash=row["hash"][:8], status=intent.state.value, order_ref=ref,
                            reasons=["RECOVERED_BY_EXECUTION_CORE"])
            else:
                self._end(row, REJECTED, "APPLY_INTERRUPTED", now)
        for row in self.store.offers(states=OPEN_STATES):
            offer = self._offer(row)
            problem = self._binding_problem(offer, now) if offer else "OFFER_RECORD_CORRUPT"
            if problem:
                self._end(row, INVALIDATED if problem != C.CONFIRMATION_EXPIRED else EXPIRED,
                          problem, now)

    def _binding_problem(self, offer: C.Offer, now: datetime) -> str | None:
        """What no longer holds of the offer's binding, or None."""
        if now >= offer.expires_at:
            return C.CONFIRMATION_EXPIRED
        entry = self.svc.mapping.entries.get(offer.family)
        if not isinstance(entry, FamilyMapping) or not entry.approved \
                or C.entry_terms_hash(entry) != offer.entry_terms_hash:
            return "MAPPING_CHANGED"
        if C.lexicon_hash(self.svc.mapping) != offer.lexicon_hash:
            return "LEXICON_CHANGED"
        tz = entry.session.timezone if entry.session else "UTC"
        if now.astimezone(ZoneInfo(tz)).date().isoformat() != offer.session_date:
            # The contract selection read the date: never applied on another day's selection.
            return "DATE_CHANGED"
        if self.svc.replay.has_other_revision(offer.source_id, offer.revision):
            return "REVISION"
        return None

    # ------------------------------------------------------------------ after each post

    def after_post(self, post, linked: ParsedPost, apply_kw: dict, mapped: ParsedPost,
                   decisions: list[MappingDecision], result, decision_now: datetime) -> None:
        """Invalidate what this post supersedes, then offer what it holds (never raises: a
        pre-check failure is journaled by type and makes no offer)."""
        try:
            self._supersede(post, mapped, decision_now)
            if not result.duplicate:
                self._offer_holds(post, linked, apply_kw, decisions, result, decision_now)
        except Exception as exc:  # noqa: BLE001 - fail closed: no offer, the pipeline carries on
            self._event(decision_now, "CONFIRMATION_PRECHECK_FAILED", source_id=post.source_id,
                        revision=post.revision, error=type(exc).__name__)

    def _supersede(self, post, mapped: ParsedPost, now: datetime) -> None:
        trading = [a for a in mapped.actions if a.action != Action.NO_TRADE]
        for row in self.store.offers(states=OPEN_STATES):
            if post.source_id == row["source_id"]:
                if post.revision != row["revision"]:
                    self._end(row, INVALIDATED, "REVISION", now)
                continue
            asset = row["family"].removesuffix(".OPT")
            if any(a.idea_reference == row["idea_id"] or a.source_market_symbol == asset
                   for a in trading):
                self._end(row, INVALIDATED, "LATER_INSTRUCTION", now,
                          by_source_id=post.source_id)

    def _offer_holds(self, post, linked, apply_kw, decisions, result, now) -> None:
        by_decision = {d.action_id: d for d in decisions}
        for o in result.outcomes:
            if o.status not in C.HELD_STATUSES or o.action_id not in by_decision:
                continue
            a = next((x for x in linked.actions if x.action_id == o.action_id), None)
            if a is None or a.action != Action.OPEN:
                continue
            waived = self.svc.mapping.apply_waived(linked, a.action_id, waive=T.WAIVE_ALL,
                                                   **apply_kw)
            entry = self.svc.mapping.entries.get(waived.family or "")
            entry = entry if isinstance(entry, FamilyMapping) else None
            quote = None
            if waived.status == "SUPPORTED" and waived.instrument is not None and entry \
                    and not entry.legacy_equivalent:
                quote = self.svc._mapped_quote(entry, waived.instrument, now)
            held = C.HeldDecision(
                source_id=post.source_id, post_id=post.post_id, revision=post.revision,
                post_hash=post.text_sha256, published_at=post.published_at,
                received_at=post.received_at, verified_author=post.verified_author,
                text_complete=post.text_complete, action_id=a.action_id,
                actions=tuple(linked.actions), production=by_decision[a.action_id],
                waived=waived, outcome_status=o.status, outcome_reasons=tuple(o.reasons),
                entry=entry, policy=self.policy, now=now, quote=quote,
                is_repost=getattr(post, "is_repost", False),
                is_edit=self.svc.replay.has_other_revision(post.source_id, post.revision),
                lexicon_hash=self.lexicon_hash)
            out = C.offer_for(held)
            if isinstance(out, C.Offer):
                offer_id = self.store.add_offer(out.to_record(), now)
                if offer_id is not None:
                    self._event(now, "CONFIRMATION_OFFERED", offer_id=offer_id,
                                hash=out.short_hash, source_id=out.source_id,
                                post_id=out.post_id, revision=out.revision,
                                action_id=out.action_id, family=out.family,
                                confirms=[f"{c}:{k}" if k else c for c, k in out.confirms],
                                expires_at=out.expires_at.isoformat(),
                                gate_now=[out.gate_now_status, *out.gate_now_reasons])
            elif out.tier or out.confirmable_doubts or waived.pairs:
                self._event(now, "CONFIRMATION_NOT_OFFERED", source_id=post.source_id,
                            revision=post.revision, action_id=a.action_id, reason=out.reason,
                            detail=list(out.detail), tier=out.tier)

    # ------------------------------------------------------------------ each cycle

    def tick(self, now: datetime, *, read_quotes: bool) -> None:
        """Expiry (journaled as unanswered), the halt, and the card's live view; then the alert
        of every open offer."""
        halted = self.svc.halted
        keep: set[str] = set()
        rows = self.store.offers(states=OPEN_STATES)
        held = {(e["source_id"], e["revision"], e["action_id"])
                for e in self.store.exceptions(open_only=True)
                if e["kind"].startswith("SIGNAL_")} if rows else set()
        for row in rows:
            offer = self._offer(row)
            if offer is None:
                self._end(row, INVALIDATED, "OFFER_RECORD_CORRUPT", now)
            elif (row["source_id"], row["revision"], row["action_id"]) not in held:
                # The held signal was acknowledged (ack-signal): its offer ends with it.
                self._end(row, INVALIDATED, "SIGNAL_ACKNOWLEDGED", now)
            elif halted:
                self._end(row, INVALIDATED, "HALT", now)
            elif now >= offer.expires_at:
                self._end(row, EXPIRED, C.CONFIRMATION_EXPIRED, now, unanswered=True)
            else:
                if read_quotes:
                    self._refresh(row, offer, now)
                key = f"OFFER:{row['id']}"
                keep.add(key)
                left = int((offer.expires_at - now).total_seconds())
                self.svc.alerter.raise_(
                    key, N.SIGNAL_CONFIRMABLE, "Held signal can be confirmed",
                    f"offer {row['id']} {offer.short_hash}: {offer.proposal.action.value} "
                    f"{offer.proposal.position_side.value} {offer.family} at "
                    f"{offer.price_reference} stop {offer.stop}; doubts "
                    f"{', '.join(c for c, _ in offer.confirms)}; {left} s left", now)
        self.svc.alerter.sweep("OFFER:", keep, now)

    def _refresh(self, row: dict, offer: C.Offer, now: datetime) -> None:
        entry = self.svc.mapping.entries.get(offer.family)
        if not isinstance(entry, FamilyMapping):
            return
        try:
            quotes = self._quotes(offer, entry, now)
        except BrokerError:
            return
        q = next(iter(quotes.values()), None)
        view: dict = {"at": now.isoformat(), "quote": None}
        if q is not None:
            sides = [t for t in (getattr(q, "bid_at", None), getattr(q, "ask_at", None)) if t]
            oldest = min(sides) if sides else q.observed_at
            touch = q.ask if offer.proposal.position_side.value == "LONG" else q.bid
            ref = C._num(offer.price_reference)
            view["quote"] = {
                "bid": str(q.bid), "ask": str(q.ask), "quality": getattr(q, "quality", None),
                "age_seconds": round((now - oldest).total_seconds(), 3),
                "deviation_pct": (str(round((touch - ref) / ref * 100, 4))
                                  if ref and touch else None)}
        gate = C.gate_confirmed(offer, q, self.policy, now=now)
        view["gate_now"] = [gate.status, *gate.reasons]
        self.store.set_offer_quote(row["id"], view, now)

    # ------------------------------------------------------------------ operator requests

    def handle(self, req: dict) -> settlement.OperatorResult:
        """CONFIRM_SIGNAL or DISMISS_SIGNAL, at the start of a cycle (``apply_pending``)."""
        now = self.svc.clock()
        action, target, full = req["action"], str(req["target"]), req["kind"] or ""
        reasons: list[str] = []
        row = self.store.offer(int(target)) if target.isdigit() else None
        if req["confirmation"].strip() != offer_phrase(action, int(target or 0), full):
            reasons.append("CONFIRMATION_MISMATCH")
        if row is None:
            reasons.append("OFFER_UNKNOWN")
        else:
            if row["state"] != OFFERED:
                reasons.append(f"OFFER_NOT_OPEN:{row['state']}")
            if row["hash"] != full:
                reasons.append("PROPOSAL_CHANGED")
        before = {"state": row["state"] if row else None}
        if reasons:
            return settlement.OperatorResult(status="REFUSED", reasons=reasons, before=before,
                                             after=before)
        who = {"operator": req["operator"], "request_id": req["uid"], "origin": req["origin"]}
        if action == Q.DISMISS_SIGNAL:
            if not self.store.set_offer_state(row["id"], expect=(OFFERED,), state=DISMISSED,
                                              at=now, reason="OPERATOR_DISMISSED",
                                              operator=req["operator"], request_uid=req["uid"]):
                return settlement.OperatorResult(status="REFUSED", reasons=["OFFER_NOT_OPEN"])
            self._event(now, "OPERATOR_DISMISSED_SIGNAL", offer_id=row["id"],
                        hash=row["hash"][:8], **who)
            return settlement.OperatorResult(status="APPLIED", before=before,
                                             after={"state": DISMISSED})
        return self._confirm(row, req, now, before, who)

    def _confirm(self, row, req, now, before, who) -> settlement.OperatorResult:
        offer = self._offer(row)
        problem = "OFFER_RECORD_CORRUPT" if offer is None else None
        if problem is None and offer.tier != T.CONFIRMABLE_TIER:
            problem = "TIER_NOT_ALLOWED"
        if problem is None and not environment_ok(self.svc.broker):
            problem = "ENVIRONMENT_NOT_SIM_OR_PAPER"
        if problem is None and self.svc.halted:
            problem = "HALT"
        if problem is None:
            problem = self._binding_problem(offer, now)
        if problem:
            self._end(row, EXPIRED if problem == C.CONFIRMATION_EXPIRED else INVALIDATED,
                      problem, now, **who)
            return settlement.OperatorResult(status="REFUSED", reasons=[problem], before=before,
                                             after={"state": self.store.offer(row["id"])["state"]})
        if not self.store.set_offer_state(row["id"], expect=(OFFERED,), state=CONFIRMED, at=now,
                                          operator=req["operator"], request_uid=req["uid"]):
            return settlement.OperatorResult(status="REFUSED", reasons=["OFFER_NOT_OPEN"])
        self._event(now, "OPERATOR_CONFIRMED_SIGNAL", offer_id=row["id"], hash=offer.short_hash,
                    source_id=offer.source_id, revision=offer.revision,
                    action_id=offer.action_id,
                    confirms=[f"{c}:{k}" if k else c for c, k in offer.confirms], **who)
        self.store.set_offer_state(row["id"], expect=(CONFIRMED,), state=APPLYING, at=now)
        state, outcome = self._apply(row["id"], offer, now)
        return settlement.OperatorResult(status="APPLIED", before=before,
                                         after={"state": state, **outcome})

    def _apply(self, offer_id: int, offer: C.Offer, now: datetime) -> tuple[str, dict]:
        """The confirmed action through the unchanged chain, once (one-shot either way)."""
        svc = self.svc
        entry = svc.mapping.entries[offer.family]
        try:
            authority = C.authorize(offer, state=APPLYING, now=now)
            if svc.shadow:
                svc.broker.set_reference_time(now)
                svc.broker.take_quote_log()
            quotes = self._quotes(offer, entry, now)
            svc.replay.gate_stamps = {}
            result = svc.replay.process_confirmed(C.source_of(offer), offer.proposal, quotes,
                                                  now=now, authority=authority)
            [o] = result.outcomes
        except Exception as exc:  # noqa: BLE001 - recorded; never retried
            outcome = {"status": "EXCEPTION", "reasons": [f"APPLY_FAILED:{type(exc).__name__}"],
                       "order_ref": None}
            self.store.set_offer_state(offer_id, expect=(APPLYING,), state=REJECTED, at=now,
                                       reason=outcome["reasons"][0], result=outcome)
            self._event(now, "CONFIRMATION_RESULT", offer_id=offer_id, hash=offer.short_hash,
                        **outcome)
            return REJECTED, outcome
        if o.status not in C.HELD_STATUSES or o.status == "UNRESOLVED":
            state = EXECUTED
        else:
            state = HELD if o.status == "HOLD" else REJECTED
        outcome = {"status": o.status, "reasons": list(o.reasons), "order_ref": o.order_ref}
        self.store.set_offer_state(offer_id, expect=(APPLYING,), state=state, at=now,
                                   reason=o.status, result=outcome)
        decision = MappingDecision(action_id=offer.action_id, status="SUPPORTED",
                                   source_asset=offer.proposal.source_market_symbol,
                                   family=offer.family, basis=offer.basis)
        svc._record_idea_mappings(offer.source_id, ParsedPost(actions=[offer.proposal]),
                                  [decision])
        self._event(now, "CONFIRMATION_RESULT", offer_id=offer_id, hash=offer.short_hash,
                    source_id=offer.source_id, revision=offer.revision,
                    action_id=offer.action_id, **outcome)
        if svc.shadow:
            self._event(now, "SHADOW_DECISION_QUOTES", source_id=offer.source_id,
                        revision=offer.revision, post_id=offer.post_id, confirmed=True,
                        decisions=[{"action_id": o.action_id, "status": o.status,
                                    "order_ref": o.order_ref}],
                        quotes=svc.broker.take_quote_log())
        if state == EXECUTED:
            for e in self.store.exceptions(open_only=True):
                if e["kind"].startswith("SIGNAL_") and e["source_id"] == offer.source_id \
                        and e["revision"] == offer.revision and e["action_id"] == offer.action_id:
                    self.store.resolve_exception(
                        e["id"], now, f"released by operator confirmation, offer {offer_id}")
        return state, outcome


def disable_open_offers(store, now: datetime) -> None:
    """The switch is off: an offer left open by an earlier run ends (FEATURE_DISABLED). Only a
    state directory that once ran with the switch on has the table; nothing else is touched."""
    if not store.has_offers():
        return
    for row in store.offers(states=(OFFERED, CONFIRMED)):
        if store.set_offer_state(row["id"], expect=(row["state"],), state=INVALIDATED, at=now,
                                 reason="FEATURE_DISABLED"):
            store.event(now, "CONFIRMATION_INVALIDATED", offer_id=row["id"],
                        hash=row["hash"][:8], reason="FEATURE_DISABLED")
