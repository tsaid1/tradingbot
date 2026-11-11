"""The source-series-to-broker-contract mapping policy.

A publisher quotes levels in some series: an ETF's own price, an index, a spot rate, a futures
contract. An order can only be placed in a broker contract. This module decides, from an explicit
data file, which source assets may reach the broker at all, and as which contract.

Rules
  * Each entry names the source asset, the broker instrument and the price-basis relationship.
  * Only basis ``DIRECT`` (the publisher's levels are the traded instrument's own price) may
    execute. A ``DIRECT`` entry must name the same symbol on both sides: the gateway checks that
    the quoted level belongs to the orderable contract, and this policy never renames one into
    another.
  * Anything unmapped, or mapped with any other basis (index, spot or futures levels that are not
    the traded instrument's price), is marked ``UNSUPPORTED_INSTRUMENT_MAPPING``. The marker is
    added as an ambiguity, so the one gateway (``validator.gate_action``) HOLDs the action; it
    never reaches the broker. No index or spot level is ever treated as interchangeable with an
    ETF or futures price.
  * The policy only ever restricts. It cannot approve anything, and it never changes a price basis,
    a price, a side or a timing.
"""

from __future__ import annotations

import tomllib
from enum import Enum
from pathlib import Path
from typing import Literal

from pydantic import Field, ValidationError, model_validator

from tradingbot.event_models import ParsedPost, TradeAction
from tradingbot.models import Action, AssetClass, Instrument, StrictModel

UNSUPPORTED_INSTRUMENT_MAPPING = "UNSUPPORTED_INSTRUMENT_MAPPING"


class BasisRelation(str, Enum):
    DIRECT = "DIRECT"  # levels are quoted in the traded instrument's own price
    INDEX_LEVEL = "INDEX_LEVEL"  # levels are index points (e.g. an equity index), not the ETF price
    SPOT_LEVEL = "SPOT_LEVEL"  # levels are a spot rate or commodity price
    FUTURES_LEVEL = "FUTURES_LEVEL"  # levels are a futures contract's price
    PROXY = "PROXY"  # a related instrument whose price differs (ratio, tracking error, roll)
    UNKNOWN = "UNKNOWN"


class MappingEntry(StrictModel):
    source_asset: str = Field(min_length=1, max_length=40)
    broker_symbol: str = Field(min_length=1, max_length=40)
    broker_asset_class: AssetClass
    broker_exchange: str | None = None
    basis: BasisRelation
    note: str = ""

    @model_validator(mode="after")
    def _direct_is_same_symbol(self):
        if self.basis == BasisRelation.DIRECT and self.source_asset != self.broker_symbol:
            raise ValueError(
                f"DIRECT entry {self.source_asset!r} -> {self.broker_symbol!r}: a direct basis "
                "means the publisher quotes the traded instrument itself, so the symbols must match"
            )
        return self

    @property
    def executable(self) -> bool:
        return self.basis == BasisRelation.DIRECT

    def instrument(self) -> Instrument:
        return Instrument(
            asset_class=self.broker_asset_class,
            symbol=self.broker_symbol,
            exchange=self.broker_exchange,
        )


class MappingDecision(StrictModel):
    action_id: str
    status: Literal["SUPPORTED", "UNSUPPORTED", "NOT_APPLICABLE"]
    source_asset: str | None = None
    broker_symbol: str | None = None
    basis: str | None = None
    reason: str = ""


class MappingFile(StrictModel):
    version: Literal[1] = 1
    mapping: list[MappingEntry] = Field(default_factory=list)


class MappingPolicy:
    def __init__(self, entries: list[MappingEntry]):
        seen: dict[str, MappingEntry] = {}
        for e in entries:
            if e.source_asset in seen:
                raise ValueError(f"duplicate mapping for source asset {e.source_asset!r}")
            seen[e.source_asset] = e
        self.entries = seen

    @classmethod
    def from_toml(cls, path: str | Path) -> MappingPolicy:
        p = Path(path)
        try:
            raw = tomllib.loads(p.read_text(encoding="utf-8"))
            return cls(MappingFile.model_validate(raw).mapping)
        except FileNotFoundError:
            raise ValueError(f"instrument mapping file not found: {p}") from None
        except (tomllib.TOMLDecodeError, ValidationError) as exc:
            raise ValueError(f"invalid instrument mapping file {p}: {exc}") from None

    @property
    def executable_assets(self) -> list[str]:
        return sorted(a for a, e in self.entries.items() if e.executable)

    def decide(self, action: TradeAction) -> MappingDecision:
        if action.action == Action.NO_TRADE:
            return MappingDecision(action_id=action.action_id, status="NOT_APPLICABLE")
        asset = action.source_market_symbol or (
            action.instrument.symbol if action.instrument is not None else None
        )
        if asset is None:
            return MappingDecision(
                action_id=action.action_id, status="UNSUPPORTED", reason="SOURCE_ASSET_UNRESOLVED"
            )
        entry = self.entries.get(asset)
        if entry is None:
            return MappingDecision(
                action_id=action.action_id,
                status="UNSUPPORTED",
                source_asset=asset,
                reason="SOURCE_ASSET_NOT_MAPPED",
            )
        if not entry.executable:
            return MappingDecision(
                action_id=action.action_id,
                status="UNSUPPORTED",
                source_asset=asset,
                broker_symbol=entry.broker_symbol,
                basis=entry.basis.value,
                reason=f"BASIS_{entry.basis.value}_NOT_EXECUTABLE",
            )
        if action.instrument is not None and action.instrument.symbol != entry.broker_symbol:
            return MappingDecision(
                action_id=action.action_id,
                status="UNSUPPORTED",
                source_asset=asset,
                broker_symbol=entry.broker_symbol,
                basis=entry.basis.value,
                reason="INTERPRETED_INSTRUMENT_DIFFERS_FROM_MAPPING",
            )
        return MappingDecision(
            action_id=action.action_id,
            status="SUPPORTED",
            source_asset=asset,
            broker_symbol=entry.broker_symbol,
            basis=entry.basis.value,
        )

    def apply(self, parsed: ParsedPost) -> tuple[ParsedPost, list[MappingDecision]]:
        """Return the post with every unsupported action marked for HOLD, and one decision per
        action. A supported action gets the mapped broker contract (same symbol)."""
        actions: list[TradeAction] = []
        decisions: list[MappingDecision] = []
        for a in parsed.actions:
            d = self.decide(a)
            decisions.append(d)
            if d.status == "UNSUPPORTED":
                marks = [UNSUPPORTED_INSTRUMENT_MAPPING, f"MAPPING_{d.reason}"]
                a = a.model_copy(
                    update={
                        "ambiguities": [
                            *a.ambiguities,
                            *(m for m in marks if m not in a.ambiguities),
                        ]
                    }
                )
            elif d.status == "SUPPORTED":
                a = a.model_copy(update={"instrument": self.entries[d.source_asset].instrument()})
            actions.append(a)
        return ParsedPost(actions=actions, summary=parsed.summary), decisions
