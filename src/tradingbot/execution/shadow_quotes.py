"""Shadow mode: the pure mapping from the read-only adapter's quote to the execution quote.

``map_snapshot(snapshot, contract, at=...)`` turns a ``tradingbot.ibkr.models.QuoteSnapshot`` (a
held stream's ``latest()``) into the ``BrokerQuote`` the gateway and the execution core decide on,
plus the public evidence (codes and numbers only) the journal records. SDK-free.

**Per-side times (the conversion).** Ages are measured ONLY on the adapter's monotonic clock:
``age = snapshot.observed_at_monotonic - snapshot.<side>_at_monotonic`` (the side's initial image
or latest value change; an identical repeat never refreshes it). The only wall time used is
``at``, a time on the SERVICE clock that the caller guarantees is no later than any ``now`` the
gateway or the execution core will measure this quote at. Then ``<side>_at = at - age``, the age
rounded UP to the microsecond, so the gateway's ``now - <side>_at`` is at least the adapter's age
and never smaller. The adapter's own wall stamps (``bid_at`` / ``ask_at`` strings) are never read,
so no wall-clock skew between the adapter, TWS and the service can make a side look fresher.

**Stricter than the adapter, never looser.** The gateway's anchored-entry rule
(``validator.anchor_quote_problem``) must refuse every quote ``models.entry_quote_check`` refuses:

* quality maps one to one (LIVE, DELAYED, FROZEN, UNAVAILABLE); anything else is UNAVAILABLE;
* a LIVE quote whose market-data type was NOT reported is re-checked at the read: if a side's
  latest change or latest price change is older than ``UNREPORTED_LIVE_WINDOW_SECONDS`` (or
  unknown) it is FROZEN (the gateway cannot see the price-change stamps);
* a side is absent (``None``) when its CURRENT value is missing, not finite or not positive (a
  placeholder), when the stream flagged an off-tick price on it (sticky until reopened), or when
  the series is halted (any non-zero HALTED value, NaN and -1 included);
* a side's time is absent when its ticks were not live ids only, when the observation time or the
  side's stamp is unknown, or when the stamp is after the observation (the gateway would tolerate
  0.5 s in the future; the adapter tolerates none).

**The quote's own age (review B2).** ``received_at`` is the OLDER side's time (the read time
minus the larger side age), so the policy's quote-age limit measures what the adapter measured: a
hung feed that still reports LIVE is stale. When either side's time is unknown, the quote the
gateway and the execution core see is UNAVAILABLE, with no sides and no side times (code
``SIDE_TIME_UNKNOWN``): no entry, add or exit is priced on it. The evidence keeps the adapter's
label. ``map_for_simulator`` gives the simulator the same prices and label (after the
unreported-evidence downgrade) without the time requirement, so a protective order is still
simulated on a delayed quote, and labelled as such.

**Opening orders (review B1).** ``opening_problem(quote, at, limit)``: an order that is not
reduce-only may be matched only on a LIVE quote with both sides present, bid below ask, and each
side no older than ``limit`` at ``at``, measured from the mapped side times.

A sticky ``*_PLACEHOLDER_TICK`` / ``*_NON_FINITE_TICK`` flag alone does not remove a side whose
current value is valid: TWS sends -1 / 0 before the first quote, the adapter never counts such a
tick as image or change, and a valid current value is necessarily the side's evidence price.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_CEILING, Decimal

from tradingbot.execution.broker import BrokerQuote, ContractRef, QuoteQuality
from tradingbot.ibkr.models import UNREPORTED_LIVE_WINDOW_SECONDS, QuoteSnapshot

TICK_CLASS_LIVE = "LIVE"
# The closed list of mapping problem codes recorded on the evidence.
SHADOW_QUOTE_PROBLEMS = (
    "QUALITY_UNKNOWN", "OBSERVED_TIME_UNKNOWN", "HALTED", "UNREPORTED_EVIDENCE_EXPIRED",
    "BID_MISSING", "ASK_MISSING", "BID_NOT_FINITE", "ASK_NOT_FINITE", "BID_NON_POSITIVE",
    "ASK_NON_POSITIVE", "BID_OFF_TICK", "ASK_OFF_TICK", "BID_TICKS_NOT_LIVE", "ASK_TICKS_NOT_LIVE",
    "BID_TIME_UNKNOWN", "ASK_TIME_UNKNOWN", "BID_FROM_FUTURE", "ASK_FROM_FUTURE",
    "SIDE_TIME_UNKNOWN",
    # set by the shadow broker, not by the mapping
    "NO_STREAM_YET", "STREAM_FAILED", "STREAM_CLOSED", "SESSION_NOT_READY", "CONTRACT_UNKNOWN",
    "STREAM_UNAVAILABLE", "STREAM_CAP_REACHED",
)
# Why an opening (not reduce-only) order may not be matched on a quote.
OPENING_PROBLEMS = (
    "SHADOW_OPENING_QUOTE_NOT_LIVE", "SHADOW_OPENING_QUOTE_LOCKED_OR_CROSSED",
    "SHADOW_OPENING_QUOTE_SIDE_STALE", "SHADOW_OPENING_LIMIT_UNKNOWN",
)
_QUALITIES = {q.value: q for q in QuoteQuality}


def _finite(x: object) -> bool:
    return not isinstance(x, bool) and isinstance(x, (int, float)) and math.isfinite(x)


@dataclass(frozen=True)
class QuoteEvidence:
    """What a shadow decision was made on: public codes and numbers only."""

    contract_key: str
    label: str  # the execution quality the gateway saw
    adapter_label: str | None  # the adapter's own label
    simulator_label: str  # the label of the quote the simulator matched on
    reason: str  # the adapter's classification reason, or the broker's own code
    market_data_type: int | None
    requested_market_data_type: int | None
    bid_age_seconds: float | None
    ask_age_seconds: float | None
    problems: tuple[str, ...]
    read_at: str

    def as_dict(self) -> dict:
        return {"contract_key": self.contract_key, "label": self.label,
                "adapter_label": self.adapter_label, "simulator_label": self.simulator_label,
                "reason": self.reason,
                "market_data_type": self.market_data_type,
                "requested_market_data_type": self.requested_market_data_type,
                "bid_age_seconds": self.bid_age_seconds, "ask_age_seconds": self.ask_age_seconds,
                "problems": list(self.problems), "read_at": self.read_at}


def unavailable_quote(contract: ContractRef, at: datetime) -> BrokerQuote:
    return BrokerQuote(contract=contract, received_at=at, quality=QuoteQuality.UNAVAILABLE)


def unavailable_evidence(contract: ContractRef, at: datetime, code: str,
                         reason: str | None = None) -> QuoteEvidence:
    return QuoteEvidence(contract_key=contract.contract_key, label="UNAVAILABLE",
                         adapter_label=None, simulator_label="UNAVAILABLE", reason=reason or code, market_data_type=None,
                         requested_market_data_type=None, bid_age_seconds=None,
                         ask_age_seconds=None, problems=(code,), read_at=at.isoformat())


def _ceil_age(age: float) -> timedelta:
    """``age`` seconds rounded UP to the microsecond, exactly (Decimal of a float is exact)."""
    micros = (Decimal(age) * 1_000_000).to_integral_value(rounding=ROUND_CEILING)
    return timedelta(microseconds=int(micros))


def _updated_at(text: str | None, at: datetime) -> datetime | None:
    if not text:
        return None
    try:
        t = datetime.fromisoformat(text)
    except ValueError:
        return None
    if t.tzinfo is None:
        return None
    return min(t, at)


def map_snapshot(snapshot: QuoteSnapshot, contract: ContractRef, *,
                 at: datetime) -> tuple[BrokerQuote, QuoteEvidence]:
    """The execution quote for ``contract`` from one adapter snapshot (see the module doc)."""
    quote, _, evidence = map_all(snapshot, contract, at=at)
    return quote, evidence


def map_for_simulator(snapshot: QuoteSnapshot, contract: ContractRef, *,
                      at: datetime) -> BrokerQuote:
    """The quote the simulator matches on: real prices and their honest label."""
    return map_all(snapshot, contract, at=at)[1]


def opening_problem(quote: BrokerQuote, at: datetime, limit: float | None) -> str | None:
    """Why an opening order may not be matched on ``quote`` (read at ``at``), or None."""
    if isinstance(limit, bool) or not _finite(limit) or limit <= 0:
        return "SHADOW_OPENING_LIMIT_UNKNOWN"
    if quote.quality is not QuoteQuality.LIVE or quote.bid is None or quote.ask is None \
            or quote.bid_at is None or quote.ask_at is None:
        return "SHADOW_OPENING_QUOTE_NOT_LIVE"
    if quote.bid >= quote.ask:
        return "SHADOW_OPENING_QUOTE_LOCKED_OR_CROSSED"
    if max((at - quote.bid_at).total_seconds(), (at - quote.ask_at).total_seconds()) > limit:
        return "SHADOW_OPENING_QUOTE_SIDE_STALE"
    return None


def map_all(snapshot: QuoteSnapshot, contract: ContractRef, *,
            at: datetime) -> tuple[BrokerQuote, BrokerQuote, QuoteEvidence]:
    """(the execution core's quote, the simulator's quote, the evidence)."""
    if at.tzinfo is None:
        raise ValueError("at must be an aware datetime on the service clock")
    problems: list[str] = []
    raw = getattr(snapshot.quality, "value", snapshot.quality)
    quality = _QUALITIES.get(raw) if isinstance(raw, str) else None
    if quality is None:
        quality = QuoteQuality.UNAVAILABLE
        problems.append("QUALITY_UNKNOWN")
    mono = snapshot.observed_at_monotonic
    if not _finite(mono):
        mono = None
        problems.append("OBSERVED_TIME_UNKNOWN")
    halted = snapshot.halted is not None and snapshot.halted != 0  # NaN != 0: halted
    if halted:
        problems.append("HALTED")
    flags = set(snapshot.flags or ())

    sides: dict[str, tuple[Decimal | None, datetime | None, float | None]] = {}
    for side, value, cls, stamp in (
            ("BID", snapshot.bid, snapshot.bid_tick_class, snapshot.bid_at_monotonic),
            ("ASK", snapshot.ask, snapshot.ask_tick_class, snapshot.ask_at_monotonic)):
        price: Decimal | None = None
        if value is None:
            problems.append(f"{side}_MISSING")
        elif not _finite(value):
            problems.append(f"{side}_NOT_FINITE")
        elif not value > 0:
            problems.append(f"{side}_NON_POSITIVE")
        elif f"{side}_OFF_TICK_TICK" in flags:
            problems.append(f"{side}_OFF_TICK")
        elif not halted:
            price = Decimal(str(float(value)))
        side_at: datetime | None = None
        age: float | None = None
        if mono is not None and _finite(stamp):
            age = mono - stamp
        if price is not None:
            if cls != TICK_CLASS_LIVE:
                problems.append(f"{side}_TICKS_NOT_LIVE")
            elif age is None:
                problems.append(f"{side}_TIME_UNKNOWN")
            elif age < 0:
                problems.append(f"{side}_FROM_FUTURE")
            else:
                side_at = at - _ceil_age(age)
        sides[side] = (price, side_at, None if age is None else round(age, 3))

    if quality is QuoteQuality.LIVE and snapshot.market_data_type is None:
        stamps = (snapshot.bid_at_monotonic, snapshot.ask_at_monotonic,
                  snapshot.bid_price_change_monotonic, snapshot.ask_price_change_monotonic)
        if mono is None or not all(_finite(t) and mono - t <= UNREPORTED_LIVE_WINDOW_SECONDS
                                   for t in stamps):
            quality = QuoteQuality.FROZEN
            problems.append("UNREPORTED_EVIDENCE_EXPIRED")

    last = snapshot.last
    last = Decimal(str(float(last))) if _finite(last) and last > 0 else None
    updated = _updated_at(snapshot.last_trade_time, at)
    simulator = BrokerQuote(
        contract=contract, bid=sides["BID"][0], ask=sides["ASK"][0], last=last, received_at=at,
        quality=quality, updated_at=updated)
    bid_at, ask_at = sides["BID"][1], sides["ASK"][1]
    if quality is not QuoteQuality.UNAVAILABLE and (bid_at is None or ask_at is None):
        # Review B2: no quote is shown fresher than the adapter measured it; a quote without both
        # side times is not one the execution core may price anything on.
        problems.append("SIDE_TIME_UNKNOWN")
        quote = BrokerQuote(contract=contract, received_at=at, quality=QuoteQuality.UNAVAILABLE)
    elif quality is QuoteQuality.UNAVAILABLE:
        quote = BrokerQuote(contract=contract, received_at=at, quality=quality)
    else:
        quote = BrokerQuote(
            contract=contract, bid=sides["BID"][0], ask=sides["ASK"][0], last=last,
            received_at=min(bid_at, ask_at),  # the older side: the quote's own age
            quality=quality, updated_at=updated, bid_at=bid_at, ask_at=ask_at)
    evidence = QuoteEvidence(
        contract_key=contract.contract_key, label=quote.quality.value,
        adapter_label=raw if isinstance(raw, str) else None,
        simulator_label=simulator.quality.value,
        reason=snapshot.quality_reason or "", market_data_type=snapshot.market_data_type,
        requested_market_data_type=snapshot.requested_market_data_type,
        bid_age_seconds=sides["BID"][2], ask_age_seconds=sides["ASK"][2],
        problems=tuple(dict.fromkeys(problems)), read_at=at.isoformat())
    return quote, simulator, evidence
