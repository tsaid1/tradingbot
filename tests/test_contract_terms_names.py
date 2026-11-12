"""Integration defects found by the historical service replay (classes reproduced on invented data).

1. The digits of an index's own name ("S&P 500") were read as an unread contract token, so a futures
   family for that index could never select a contract (CONTRACT_MONTH_UNRECOGNISED).
2. Any ordinary word that merely starts with a contract root's letters ("Stop" for a root STO,
   "closing" for CL) was read as an unread contract code.

Both now resolve; every near miss the review-F2 rule holds on still holds."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from tradingbot.event_models import PriceBasis, Timing, TradeAction
from tradingbot.models import Action, AssetClass, Instrument, PositionSide
from tradingbot.runtime.instruction_terms import code_shaped, extract_terms
from tradingbot.runtime.mapping import MappingPolicy
from tradingbot.runtime.mock_multi_asset import (
    MockMultiAssetRun,
    Scenario,
    Step,
    mapping_toml,
)

NOW = datetime(2026, 6, 1, 14, 0, tzinfo=UTC)


def policy(tmp_path) -> MappingPolicy:
    # The invented demo families, with the index family keyed as the interpreter's index code and
    # the invented energy future's root renamed to STO (a prefix of the word "stop").
    text = mapping_toml(approved=True).replace('family = "QIDX"', 'family = "SPX"')
    text = text.replace('symbol = "QOF"', 'symbol = "STO"')
    path = tmp_path / "m.toml"
    path.write_text(text)
    return MappingPolicy.from_toml(path)


def action(asset: str, entry: str, stop: str, target: str | None = None) -> TradeAction:
    return TradeAction(
        action_id="a1", action=Action.OPEN, timing=Timing.CURRENT,
        instrument=Instrument(asset_class=AssetClass.EQUITY, symbol=asset),
        position_side=PositionSide.LONG, price_basis=PriceBasis.SOURCE_SERIES,
        source_market_symbol=asset, entry_reference=entry, stop_price=stop, target_price=target,
        explicit_instruction=True, evidence=["invented"],
    )


SPX = action("SPX", "5010", "4980", "5070")


@pytest.mark.parametrize("clause", [
    "We are now long the S&P 500 at 5,010. Stop 4,980. Target 5,070.",
    "Long S&P500 here at 5010, stop 4980, target 5070.",
    "We are now long the s&p 500 at 5,010 (stop 4,980; target 5,070).",
])
def test_an_index_name_with_a_number_is_not_a_contract_term(tmp_path, clause):
    d, inst = policy(tmp_path).resolve(SPX, clause=clause, now=NOW)
    assert d.status == "SUPPORTED", d.reason
    assert inst.futures_contract == "202606"


@pytest.mark.parametrize("clause", [
    "We are now long the S&P 500 at 5,010 with 2% of the book. Stop 4,980.",
    "We are now long the S&P 500 09/26 at 5,010. Stop 4,980.",
    "We are now long the S&P 500 at 5,010, then 4,990. Stop 4,980.",
    "We are now long 3 S&P 500 contracts at 5,010. Stop 4,980.",
    "We are now long the S&P 5000 at 5,010. Stop 4,980.",
    "We are now long the S&P 500 sept at 5,010. Stop 4,980.",
])
def test_everything_else_beside_the_index_name_still_holds(tmp_path, clause):
    d, inst = policy(tmp_path).resolve(SPX, clause=clause, now=NOW)
    assert d.status == "UNRESOLVED" and inst is None, clause
    assert d.reason.split(",")[0] == "CONTRACT_MONTH_UNRECOGNISED"


OIL = action("QOIL", "71.50", "69.80")


@pytest.mark.parametrize("clause", [
    "We are now long $QOIL Jul 2026 at 71.50. Stop 69.80.",
    "We are now long $QOIL Jul 2026 at 71.50. STOP 69.80.",
    "Long $QOIL Jul 2026 at 71.50, stopping out at 69.80.",
])
def test_a_word_that_starts_with_the_root_is_not_a_contract_code(tmp_path, clause):
    d, inst = policy(tmp_path).resolve(OIL, clause=clause, now=NOW)
    assert d.status == "SUPPORTED", d.reason
    assert inst.futures_contract == "202607"


@pytest.mark.parametrize("clause", [
    "We are now long $QOIL stoq6 at 71.50. Stop 69.80.",
    "We are now long $QOIL STOQ at 71.50. Stop 69.80.",
    "We are now long $QOIL stodec at 71.50. Stop 69.80.",
    "We are now long $QOIL Jul 2026 STOX7Z at 71.50. Stop 69.80.",
])
def test_a_code_shaped_token_of_the_root_still_holds(tmp_path, clause):
    d, inst = policy(tmp_path).resolve(OIL, clause=clause, now=NOW)
    assert d.status == "UNRESOLVED" and inst is None, clause


def test_code_shaped():
    assert code_shaped("QIFU", "QIF") and code_shaped("QIFU6", "QIF") and code_shaped("QIFDEC", "QIF")
    assert code_shaped("QIFX7Z", "QIF")
    assert not code_shaped("CLOSE", "CL") and not code_shaped("CLOSING", "CL")
    assert not code_shaped("STOP", "STO") and not code_shaped("GOLDEN", "GOLD")
    assert not code_shaped("GOLD'S", "GOLD") and not code_shaped("QIF", "QIF")


def test_name_spans_cover_the_whole_index_name():
    t = extract_terms("Short the Nasdaq 100 and the Russell 2000 here")
    assert len(t.name_spans) == 2


def test_through_the_service_an_index_named_with_its_number_executes(tmp_path):
    """The real service path on invented posts: S&P 500 named in words, invented future QIF."""
    text = mapping_toml(approved=True).replace('family = "QIDX"', 'family = "SPX"')
    scenario = Scenario("index-name", "SPX", "REFERENCE_TRIGGER", [
        Step(0, "We are now long the S&P 500 at 5,010. Stop 4,980. Target 5,070.", "EXECUTED",
             action="OPEN"),
    ])
    run = MockMultiAssetRun(tmp_path, scenario, mapping_text=text)
    try:
        run.run()
        rows = [a for a in run.store.signal_actions() if a["action"] == "OPEN"]
        assert len(rows) == 1
        assert rows[0]["mapping_status"] == "SUPPORTED", rows[0]["mapping_reason"]
        assert rows[0]["status"] == "EXECUTED", rows[0]["reasons"]
        assert run.service.last_reconciliation.clean
    finally:
        run.close()
