"""Operator confirmation of held signals: the PURE core (docs/OPERATOR_CONFIRMATION.md).

No I/O here. ``offer_for`` decides, from a recorded held decision, the offer it would produce or
why there is none; ``gate_confirmed`` runs the unchanged gateway on the action a confirmation
would release. Both carry only reason codes, the tier and typed fields: never post text (the
interpreter's ``evidence`` and ``reason`` are replaced by a hash and dropped).

A confirmation is an INPUT to the deterministic chain: it removes the listed interpretation
doubts and nothing else. The gateway (price, quote, stop, size, age, session), the coordinator
(account, environment, halt, reconnect, exposure) and the replay's own rules run again, unchanged,
when a confirmed offer is applied.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from tradingbot.event_models import ReplaySource, Timing, TradeAction
from tradingbot.event_replay import ConfirmationAuthority
from tradingbot.models import Action, GateDecision, MappedQuote, MockPolicy, Position, Quote
from tradingbot.runtime import confirmation_table as T
from tradingbot.runtime.mapping import (
    MAPPING_NOT_APPROVED,
    UNSUPPORTED_INSTRUMENT_MAPPING,
    FamilyMapping,
    MappingDecision,
    MappingPolicy,
    WaivedResolution,
)
from tradingbot.validator import gate_action, gate_source

SCHEMA = "tradingbot.confirmation_offer/1"
HELD_STATUSES = frozenset({"HOLD", "BLOCK", "REFUSED", "HALTED", "UNRESOLVED", "EXCEPTION",
                           "REJECTED"})
CONFIRMATION_EXPIRED = "CONFIRMATION_EXPIRED"

# Gateway refusals of the confirmed action that the market or the clock can still change within
# the offer's life: an offer is still made (the card shows them; apply re-checks). Default deny:
# any other refusal of the dry gate is static, and the hold is "not confirmable".
MARKET_DEPENDENT = frozenset({
    "QUOTE_UNAVAILABLE", "QUOTE_STALE_OR_FUTURE", "QUOTE_INSTRUMENT_MISMATCH", "INVALID_BID_ASK",
    "SPREAD_TOO_WIDE", "ENTRY_PRICE_COLLAR", "STOP_ALREADY_CROSSED_AT_ENTRY", "NOTIONAL_LIMIT",
    "OUTSIDE_PERMITTED_SESSION", "REFERENCE_SERIES_NOT_UPDATING", "REFERENCE_NOT_FRESH",
    "BASIS_QUOTE_UNUSABLE", "BASIS_EXCEEDS_CAP", "BACKSTOP_UNPRICEABLE",
    "BACKSTOP_ALREADY_CROSSED_AT_ENTRY", "PROTECTIVE_STOP_TOO_FAR_FROM_LIMIT",
    "ENTRY_ANCHOR_ADVERSE_MOVE", "ENTRY_ANCHOR_FAVOURABLE_MOVE", "ENTRY_ANCHOR_QUOTE_NOT_LIVE",
    "ENTRY_ANCHOR_QUOTE_SIDE_MISSING", "ENTRY_ANCHOR_QUOTE_OFF_TICK",
    "ENTRY_ANCHOR_QUOTE_LOCKED_OR_CROSSED", "ENTRY_ANCHOR_QUOTE_SIDE_TIME_UNKNOWN",
    "ENTRY_ANCHOR_QUOTE_SIDE_STALE", "ENTRY_ANCHOR_QUOTE_SIDE_FUTURE",
})


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     default=str).encode()).hexdigest()


def entry_terms_hash(entry: FamilyMapping) -> str:
    """The approved family entry exactly as loaded: any change to it (approval included) changes
    the hash and invalidates an offer bound to the old one."""
    return hashlib.sha256(entry.model_dump_json().encode()).hexdigest()


def lexicon_hash(policy: MappingPolicy) -> str:
    """The private lexicon's entry-anchor part as the mapping uses it (words, never shown)."""
    lex = policy.anchor_lexicon
    raw = lex.model_dump_json() if lex is not None else "null"
    return hashlib.sha256(raw.encode()).hexdigest()


def mapping_marks(decision: MappingDecision) -> list[str]:
    """The ambiguity marks ``MappingPolicy.apply`` adds for a decision (to tell the interpreter's
    own ambiguities from the mapping's on an action already through ``apply``)."""
    if decision.status == "UNSUPPORTED":
        return [UNSUPPORTED_INSTRUMENT_MAPPING, f"MAPPING_{decision.reason}"]
    if decision.status == "NOT_APPROVED":
        return [MAPPING_NOT_APPROVED, f"MAPPING_FAMILY:{decision.family}"]
    if decision.status == "UNRESOLVED":
        return decision.reason.split(",")
    return []


def _num(value: str | None) -> Decimal | None:
    try:
        v = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return v if v.is_finite() and v > 0 else None


# ----------------------------------------------------------------------------- inputs and results

@dataclass(frozen=True)
class HeldDecision:
    """A recorded held decision, typed fields only. ``actions``: every action of the post as the
    interpreter produced it (before or after ``MappingPolicy.apply``: the mapping's own marks are
    recognised). ``production``: the ordinary mapping decision of the held action; ``waived``:
    ``MappingPolicy.resolve_waived(action, waive=confirmation_table.WAIVE_ALL, ...)`` on the same
    inputs. ``quote``: what the gateway would be asked with at the decision (a ``MappedQuote``
    without anchors for a mapped family). ``entry``: the family's mapping entry, if any."""

    source_id: str
    post_id: str
    revision: int
    post_hash: str
    published_at: datetime
    received_at: datetime
    verified_author: bool
    text_complete: bool
    action_id: str
    actions: tuple[TradeAction, ...]
    production: MappingDecision
    waived: WaivedResolution | None
    outcome_status: str
    outcome_reasons: tuple[str, ...]
    entry: FamilyMapping | None
    policy: MockPolicy
    now: datetime
    quote: Quote | None = None
    is_repost: bool = False
    is_edit: bool = False
    duplicate: bool = False
    before_first_start: bool = False
    lexicon_hash: str = ""


@dataclass(frozen=True)
class Offer:
    """One fully formed proposal. ``proposal`` is the action exactly as the gateway will see it
    when the offer is applied (only the confirmed doubts removed; evidence replaced by its hash)."""

    tier: str
    source_id: str
    post_id: str
    revision: int
    post_hash: str
    action_id: str
    idea_id: str
    proposal: TradeAction
    family: str
    reference_series: str | None
    execution_instrument: str
    basis: str
    price_reference: str
    price_reference_kind: str  # STATED_LEVEL (an anchored entry) or EXPLICIT_ENTRY
    stop: str
    target: str | None
    confirms: tuple[tuple[str, str], ...]  # the (code, detail key) pairs a confirmation resolves
    confirms_detail: tuple[tuple[str, ...], ...]  # the full problem of each, codes only
    uncertainty: tuple[str, ...]  # plain words, one per pair (from the closed table)
    hold_reasons: tuple[str, ...]
    published_at: datetime
    received_at: datetime
    expires_at: datetime
    entry_terms_hash: str
    lexicon_hash: str
    session_date: str
    gate_now_status: str
    gate_now_reasons: tuple[str, ...]
    hash: str = ""

    def binding(self) -> dict:
        """Every field the hash binds: the post identity and revision, the typed proposal, the
        family terms and lexicon, the doubts, the time limits."""
        return {
            "schema": SCHEMA, "source_id": self.source_id, "post_id": self.post_id,
            "revision": self.revision, "post_hash": self.post_hash, "action_id": self.action_id,
            "proposal": self.proposal.model_dump(mode="json"), "family": self.family,
            "execution_instrument": self.execution_instrument, "basis": self.basis,
            "price_reference": self.price_reference, "kind": self.price_reference_kind,
            "stop": self.stop, "target": self.target,
            "confirms": [list(p) for p in self.confirms], "tier": self.tier,
            "published_at": self.published_at.isoformat(),
            "received_at": self.received_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "entry_terms_hash": self.entry_terms_hash, "lexicon_hash": self.lexicon_hash,
            "session_date": self.session_date,
        }

    @property
    def short_hash(self) -> str:
        return self.hash[:8]

    def to_record(self) -> dict:
        """The typed record kept in the runtime store (codes, numbers, identities; no text)."""
        return {
            **self.binding(), "idea_id": self.idea_id, "reference_series": self.reference_series,
            "confirms_detail": [list(p) for p in self.confirms_detail],
            "uncertainty": list(self.uncertainty), "hold_reasons": list(self.hold_reasons),
            "gate_now_status": self.gate_now_status,
            "gate_now_reasons": list(self.gate_now_reasons), "hash": self.hash,
        }

    @classmethod
    def from_record(cls, r: dict) -> Offer:
        offer = cls(
            tier=r["tier"], source_id=r["source_id"], post_id=r["post_id"],
            revision=int(r["revision"]), post_hash=r["post_hash"], action_id=r["action_id"],
            idea_id=r["idea_id"], proposal=TradeAction.model_validate(r["proposal"]),
            family=r["family"], reference_series=r.get("reference_series"),
            execution_instrument=r["execution_instrument"], basis=r["basis"],
            price_reference=r["price_reference"], price_reference_kind=r["kind"],
            stop=r["stop"], target=r.get("target"),
            confirms=tuple(tuple(p) for p in r["confirms"]),
            confirms_detail=tuple(tuple(p) for p in r.get("confirms_detail", ())),
            uncertainty=tuple(r.get("uncertainty", ())),
            hold_reasons=tuple(r.get("hold_reasons", ())),
            published_at=datetime.fromisoformat(r["published_at"]),
            received_at=datetime.fromisoformat(r["received_at"]),
            expires_at=datetime.fromisoformat(r["expires_at"]),
            entry_terms_hash=r["entry_terms_hash"], lexicon_hash=r["lexicon_hash"],
            session_date=r["session_date"], gate_now_status=r.get("gate_now_status", ""),
            gate_now_reasons=tuple(r.get("gate_now_reasons", ())), hash=r["hash"])
        if offer_hash(offer) != offer.hash:
            raise ValueError("offer record does not match its hash")
        return offer


@dataclass(frozen=True)
class NoOffer:
    """Why a held decision produces no offer. ``tier``: REPORT_ONLY when every doubt is in the
    reporting-only tier (``would_be``: the offer it would be if the owner allowed that tier; the
    service never offers it). ``confirmable_doubts``: the table pairs found, when any were."""

    reason: str
    detail: tuple[str, ...] = ()
    tier: str | None = None
    confirmable_doubts: tuple[tuple[str, str], ...] = ()
    would_be: Offer | None = field(default=None, compare=False)


def offer_hash(offer: Offer) -> str:
    return _digest(offer.binding())


# ----------------------------------------------------------------------------- the decision

def offer_for(held: HeldDecision) -> Offer | NoOffer:
    """Pure: the offer a recorded held decision would produce, or why there is none. Repeated at
    apply time by the caller on what it then knows (expiry, hashes, state)."""
    a = next((x for x in held.actions if x.action_id == held.action_id), None)
    if a is None:
        return NoOffer("ACTION_UNKNOWN")
    if held.duplicate:
        return NoOffer("DUPLICATE")
    if held.before_first_start:
        return NoOffer("POST_BEFORE_FIRST_START")
    source = ReplaySource(source_id=held.source_id, revision=held.revision,
                          published_at=held.published_at, received_at=held.received_at,
                          verified_author=held.verified_author, complete_text=held.text_complete,
                          is_repost=held.is_repost, post_hash=held.post_hash)
    blocked = gate_source(source, held.now, is_edit=held.is_edit)
    if blocked is not None:
        return NoOffer("SOURCE_NOT_ELIGIBLE", tuple(blocked.reasons))
    if held.outcome_status not in HELD_STATUSES:
        return NoOffer("NOT_HELD", (held.outcome_status,))
    trading = [x for x in held.actions if x.action != Action.NO_TRADE]
    if len(trading) != 1 or trading[0].action_id != held.action_id:
        return NoOffer("NOT_SINGLE_TRADING_ACTION", (f"TRADING_ACTIONS:{len(trading)}",))
    if a.action != Action.OPEN or a.timing != Timing.CURRENT or not a.explicit_instruction \
            or not a.evidence or a.idea_reference is not None:
        return NoOffer("NOT_A_CURRENT_EXPLICIT_OPEN", (a.action.value, a.timing.value))
    prod = held.production
    if prod.status in ("UNSUPPORTED", "NOT_APPROVED", "NOT_APPLICABLE"):
        return NoOffer("MAPPING_NOT_CONFIRMABLE", tuple(mapping_marks(prod)) or (prod.status,))
    w = held.waived
    if w is None or not isinstance(w, WaivedResolution) or w.action_id != held.action_id:
        return NoOffer("WAIVED_EVALUATION_MISSING")
    if w.status != "SUPPORTED" or w.instrument is None:
        # A hold the table does not cover, met before or after a confirmable one.
        return NoOffer("NOT_CONFIRMABLE", tuple(w.reason.split(",")) if w.reason else (w.status,),
                       confirmable_doubts=tuple(p for p in w.pairs if p in T.CONFIRMABLE))
    marks = set(mapping_marks(prod))
    own = [x for x in a.ambiguities if x not in marks]
    unknown = [x for x in own if x not in T.INTERPRETER_CODES]
    if unknown:
        return NoOffer("NOT_CONFIRMABLE", tuple(unknown))
    pairs = [*w.pairs, *((x, "") for x in dict.fromkeys(own))]
    if not pairs:
        # The mapping and the interpreter found nothing to confirm: the hold is the gateway's or
        # the execution core's (quote, collar, halt, ...), never an operator's to lift.
        return NoOffer("HOLD_NOT_FROM_INTERPRETATION", tuple(held.outcome_reasons))
    tiers = {T.tier_of(p) for p in pairs}
    if None in tiers:  # cannot happen: resolve_waived accepts table pairs only
        return NoOffer("NOT_CONFIRMABLE", tuple(f"{c}:{k}" for c, k in pairs))
    tier = T.CONFIRMABLE_TIER if tiers == {T.CONFIRMABLE_TIER} else T.REPORT_ONLY_TIER
    entry = held.entry
    if entry is None or entry.family != w.family or not entry.approved or not entry.executable:
        return NoOffer("MAPPING_NOT_APPROVED", (w.family or "-",))
    if a.position_side is None:
        return NoOffer("SIDE_MISSING")
    if _num(a.stop_price) is None:
        return NoOffer("STOP_MISSING")
    anchored = a.entry_reference is None
    reference = w.entry_anchor if anchored else a.entry_reference
    if _num(reference) is None:
        return NoOffer("PRICE_REFERENCE_MISSING")
    limit = float(held.policy.max_signal_age_seconds)
    if anchored and entry.entry_anchor is not None:
        limit = min(limit, float(entry.entry_anchor.max_signal_age_seconds))
    expires = held.published_at + timedelta(seconds=limit)
    if held.now >= expires:
        return NoOffer(CONFIRMATION_EXPIRED)
    proposal = a.model_copy(update={
        "instrument": w.instrument, "ambiguities": [],
        # The interpreter's evidence is the clause itself (post text): only its hash is kept.
        "evidence": ["sha256:" + hashlib.sha256("\x1f".join(a.evidence).encode()).hexdigest()],
        "reason": "",
    })
    tz = entry.session.timezone if entry.session else "UTC"
    offer = Offer(
        tier=tier, source_id=held.source_id, post_id=held.post_id, revision=held.revision,
        post_hash=held.post_hash, action_id=held.action_id,
        idea_id=f"{held.source_id}:{held.action_id}", proposal=proposal, family=entry.family,
        reference_series=entry.reference.symbol if entry.reference else None,
        execution_instrument=w.instrument.quote_key, basis=str(entry.basis),
        price_reference=str(reference),
        price_reference_kind="STATED_LEVEL" if anchored else "EXPLICIT_ENTRY",
        stop=str(a.stop_price), target=a.target_price,
        confirms=tuple(pairs),
        confirms_detail=tuple(p for _, _, p in w.waived) + tuple((x,) for x in own),
        uncertainty=tuple(T.TABLE[p] for p in pairs),
        hold_reasons=tuple(held.outcome_reasons), published_at=held.published_at,
        received_at=held.received_at, expires_at=expires, entry_terms_hash=entry_terms_hash(entry),
        lexicon_hash=held.lexicon_hash,
        session_date=held.now.astimezone(ZoneInfo(tz)).date().isoformat(),
        gate_now_status="", gate_now_reasons=())
    gate = gate_confirmed(offer, held.quote, held.policy, now=held.now)
    if gate.status != "APPROVE" and not set(gate.reasons[:1]) <= MARKET_DEPENDENT:
        return NoOffer("STATIC_BLOCKER", tuple(gate.reasons), confirmable_doubts=tuple(
            p for p in pairs if p in T.CONFIRMABLE))
    offer = _with_hash(offer, gate)
    if tier != T.CONFIRMABLE_TIER:
        return NoOffer("TIER_NOT_ALLOWED", tuple(f"{c}:{k}" for c, k in pairs
                                                  if (c, k) not in T.CONFIRMABLE),
                       tier=tier, would_be=offer)
    return offer


def _with_hash(offer: Offer, gate: GateDecision) -> Offer:
    out = replace(offer, gate_now_status=gate.status, gate_now_reasons=tuple(gate.reasons))
    return replace(out, hash=offer_hash(out))


# ----------------------------------------------------------------------------- the gateway

def anchored_quote(offer: Offer, quote: Quote | None) -> Quote | None:
    """The gateway quote with the offer's stated level as this action's anchor (an anchored entry
    only), exactly as the service hands an accepted anchor to the gateway."""
    if offer.price_reference_kind != "STATED_LEVEL" or not isinstance(quote, MappedQuote):
        return quote
    return quote.model_copy(update={"entry_anchors": {
        **quote.entry_anchors, offer.action_id: Decimal(offer.price_reference)}})


def gate_confirmed(offer: Offer, quote: Quote | None, policy: MockPolicy, *, now: datetime,
                   position: Position | None = None) -> GateDecision:
    """Pure: the unchanged gateway (``validator.gate_action``) on the action a confirmation would
    release, at ``now`` with ``quote``. Never past the offer's expiry. The stated level is the
    anchor as written: a confirmation adds no tolerance to any collar or move limit. The execution
    core's checks (account, environment, halt, exposure) are not part of this function."""
    if now >= offer.expires_at:
        return GateDecision(status="HOLD", reasons=[CONFIRMATION_EXPIRED])
    return gate_action(offer.proposal, offer.published_at, anchored_quote(offer, quote), policy,
                       position, now=now)


# ----------------------------------------------------------------------------- the apply step

def source_of(offer: Offer) -> ReplaySource:
    """The post revision the offer is bound to, as the replay records it (``offer_for`` offers
    only a verified, complete, original post)."""
    return ReplaySource(source_id=offer.source_id, revision=offer.revision,
                        published_at=offer.published_at, received_at=offer.received_at,
                        verified_author=True, complete_text=True, post_hash=offer.post_hash)


def authorize(offer: Offer, *, state: str, now: datetime) -> ConfirmationAuthority:
    """The one capability that lets the replay evaluate a confirmed action: only for an offer in
    state APPLYING (confirmed by the operator), of the confirmable tier, intact (its record matches
    its hash) and not expired."""
    if state != "APPLYING" or offer.tier != T.CONFIRMABLE_TIER or offer_hash(offer) != offer.hash \
            or now >= offer.expires_at:
        raise PermissionError("no confirmed, unexpired, intact offer")
    return ConfirmationAuthority(offer_hash=offer.hash, source_id=offer.source_id,
                                 revision=offer.revision, post_hash=offer.post_hash,
                                 action=offer.proposal, expires_at=offer.expires_at)
