"""Runtime config (fails closed, mock only) and the instrument mapping policy. Invented data."""

from __future__ import annotations

import pytest
from runtime_harness import (
    MAPPING,
    REPO,
    REQUIRED_EXECUTION,
    REQUIRED_POLICY,
    REQUIRED_SERVICE,
    required_toml,
)

from tradingbot.event_models import ParsedPost, PriceBasis, Timing, TradeAction
from tradingbot.models import Action, AssetClass, Instrument, PositionSide
from tradingbot.runtime.config import ConfigError, load_config, parse_config
from tradingbot.runtime.mapping import (
    UNSUPPORTED_INSTRUMENT_MAPPING,
    BasisRelation,
    MappingEntry,
    MappingPolicy,
)
from tradingbot.validator import gate_action

MINIMAL = """
mode = "mock"
[paths]
state_dir = "state"
halt_file = "HALT"
mapping_file = "{mapping}"
[source]
kind = "replay"
file = "posts.jsonl"
""" + required_toml().replace("{", "{{").replace("}", "}}")


def write(tmp_path, text: str):
    p = tmp_path / "runtime.toml"
    p.write_text(text.format(mapping=MAPPING))
    return p


# ------------------------------------------------------------------------------------- config


def test_minimal_mock_config_loads_with_paths_relative_to_the_file(tmp_path):
    cfg = load_config(write(tmp_path, MINIMAL))
    assert cfg.mode == "mock"
    assert cfg.paths.state_dir == tmp_path / "state"
    assert cfg.source.file == tmp_path / "posts.jsonl"
    assert cfg.service.cadence_seconds == 5.0
    assert cfg.runtime_db.parent == cfg.paths.state_dir


@pytest.mark.parametrize("mode", ["paper", "live", "MOCK", ""])
def test_any_mode_other_than_mock_refuses_to_start(tmp_path, mode):
    with pytest.raises(ConfigError, match="only 'mock'"):
        load_config(write(tmp_path, MINIMAL.replace('mode = "mock"', f'mode = "{mode}"')))


def test_missing_mode_refuses(tmp_path):
    with pytest.raises(ConfigError, match="no 'mode'"):
        load_config(write(tmp_path, MINIMAL.replace('mode = "mock"', "")))


@pytest.mark.parametrize("table", ["[paths]", "[source]", "[service]", "[policy]", "[execution]"])
def test_missing_required_table_refuses(tmp_path, table):
    text = MINIMAL
    start = text.index(table)
    end = text.find("\n[", start + 1)
    text = text[:start] + (text[end + 1 :] if end != -1 else "")
    with pytest.raises(ConfigError):
        load_config(write(tmp_path, text))


@pytest.mark.parametrize(
    "edit,match",
    [
        (lambda t: "unknown_top = 1\n" + t, "unknown_top"),
        (lambda t: t.replace("[service]\n", "[service]\ncadence = 5\n"), "cadence"),  # misspelled
        (lambda t: t.replace("quantity = 2\n", "quantity = 0\n"), "quantity"),  # range
        (lambda t: t + "[notify]\nslack = true\n", "slack"),
        (lambda t: t.replace("[policy]\n", "[policy]\npaper_only = false\n"), "paper_only"),
    ],
)
def test_unknown_or_invalid_keys_refuse(tmp_path, edit, match):
    with pytest.raises(ConfigError, match=match):
        load_config(write(tmp_path, edit(MINIMAL)))


def test_missing_config_file_and_bad_toml_refuse(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "absent.toml")
    bad = tmp_path / "bad.toml"
    bad.write_text("mode = ")
    with pytest.raises(ConfigError, match="TOML"):
        load_config(bad)


def test_x_source_needs_numeric_author_and_credential_paths(tmp_path):
    raw = {
        "mode": "mock",
        "paths": {"state_dir": "s", "halt_file": "h", "mapping_file": str(MAPPING)},
        "service": dict(REQUIRED_SERVICE),
        "policy": dict(REQUIRED_POLICY),
        "execution": dict(REQUIRED_EXECUTION),
        "source": {
            "kind": "x_timeline",
            "author_id": "not-a-number",
            "token_file": "t",
            "client_file": "c",
        },
    }
    with pytest.raises(ConfigError):
        parse_config(raw, base=tmp_path)
    raw["source"]["author_id"] = "4242"
    cfg = parse_config(raw, base=tmp_path)
    assert cfg.source.token_file == tmp_path / "t"


def test_shipped_example_config_loads():
    cfg = load_config(REPO / "config" / "runtime.example.toml")
    assert cfg.mode == "mock" and cfg.source.kind == "replay"
    assert cfg.paths.mapping_file == REPO / "config" / "instrument_mapping.toml"


# ------------------------------------------------------------------------------------ mapping


def act(
    symbol: str | None,
    *,
    basis=PriceBasis.TRADED_CONTRACT,
    action=Action.OPEN,
    series: str | None = None,
    aid: str = "a1",
) -> TradeAction:
    inst = Instrument(asset_class=AssetClass.ETF, symbol=symbol) if symbol else None
    return TradeAction(
        action_id=aid,
        action=action,
        timing=Timing.CURRENT,
        instrument=inst,
        position_side=PositionSide.LONG,
        price_basis=basis,
        source_market_symbol=series or symbol,
        entry_reference="88.40",
        stop_price="87.20",
        explicit_instruction=True,
        evidence=["invented"],
    )


def test_shipped_mapping_has_exactly_one_executable_entry_tlt_direct():
    policy = MappingPolicy.from_toml(MAPPING)
    assert list(policy.entries) == ["TLT"]
    entry = policy.entries["TLT"]
    assert entry.basis == BasisRelation.DIRECT and entry.broker_symbol == "TLT"
    assert entry.broker_asset_class == AssetClass.ETF
    assert policy.executable_assets == ["TLT"]
    text = MAPPING.read_text()
    assert "# [[mapping]]" in text and "INDEX_LEVEL" in text  # the commented template


def test_direct_entry_supported_and_carries_the_mapped_contract():
    policy = MappingPolicy.from_toml(MAPPING)
    parsed, [d] = policy.apply(ParsedPost(actions=[act("TLT")]))
    assert d.status == "SUPPORTED" and d.basis == "DIRECT"
    assert parsed.actions[0].instrument.symbol == "TLT"
    assert parsed.actions[0].ambiguities == []


@pytest.mark.parametrize(
    "action,reason",
    [
        (act(None, basis=PriceBasis.SOURCE_SERIES, series="SPX"), "SOURCE_ASSET_NOT_MAPPED"),
        (act("QQQ"), "SOURCE_ASSET_NOT_MAPPED"),
        (act(None, basis=PriceBasis.UNKNOWN), "SOURCE_ASSET_UNRESOLVED"),
    ],
)
def test_unmapped_assets_are_marked_and_the_gateway_holds_them(action, reason):
    policy = MappingPolicy.from_toml(MAPPING)
    parsed, [d] = policy.apply(ParsedPost(actions=[action]))
    assert d.status == "UNSUPPORTED" and d.reason == reason
    marked = parsed.actions[0]
    assert UNSUPPORTED_INSTRUMENT_MAPPING in marked.ambiguities
    from datetime import UTC, datetime
    from decimal import Decimal

    from tradingbot.models import MockPolicy, Quote

    now = datetime(2026, 6, 1, 14, 0, tzinfo=UTC)
    quote = Quote(
        symbol=marked.instrument.symbol if marked.instrument else "X",
        bid=Decimal("88.40"),
        ask=Decimal("88.42"),
        observed_at=now,
    )
    gate = gate_action(marked, now, quote, MockPolicy(), None, now=now)
    assert gate.status == "HOLD" and UNSUPPORTED_INSTRUMENT_MAPPING in gate.reasons


@pytest.mark.parametrize("basis", [b for b in BasisRelation if b != BasisRelation.DIRECT])
def test_every_non_direct_basis_is_unsupported(basis):
    policy = MappingPolicy(
        [
            MappingEntry(
                source_asset="SPX",
                broker_symbol="SPY",
                broker_asset_class=AssetClass.ETF,
                basis=basis,
            )
        ]
    )
    _, [d] = policy.apply(
        ParsedPost(actions=[act(None, basis=PriceBasis.SOURCE_SERIES, series="SPX")])
    )
    assert d.status == "UNSUPPORTED" and d.reason == f"BASIS_{basis.value}_NOT_EXECUTABLE"


def test_direct_entry_cannot_rename_an_index_into_an_etf():
    with pytest.raises(ValueError, match="symbols must match"):
        MappingEntry(
            source_asset="SPX",
            broker_symbol="SPY",
            broker_asset_class=AssetClass.ETF,
            basis=BasisRelation.DIRECT,
        )


def test_duplicate_or_malformed_mapping_files_refuse(tmp_path):
    dup = tmp_path / "dup.toml"
    dup.write_text(
        "[[mapping]]\nsource_asset='TLT'\nbroker_symbol='TLT'\nbroker_asset_class='ETF'\n"
        "basis='DIRECT'\n" * 2
    )
    with pytest.raises(ValueError, match="duplicate"):
        MappingPolicy.from_toml(dup)
    bad = tmp_path / "bad.toml"
    bad.write_text("[[mapping]]\nsource_asset='TLT'\nbasis='SIDEWAYS'\n")
    with pytest.raises(ValueError):
        MappingPolicy.from_toml(bad)
    with pytest.raises(ValueError, match="not found"):
        MappingPolicy.from_toml(tmp_path / "absent.toml")


def test_commentary_is_not_subject_to_mapping():
    policy = MappingPolicy.from_toml(MAPPING)
    commentary = act(None, action=Action.NO_TRADE).model_copy(update={"timing": Timing.COMMENTARY})
    parsed, [d] = policy.apply(ParsedPost(actions=[commentary]))
    assert d.status == "NOT_APPLICABLE" and parsed.actions[0] == commentary


def test_interpreted_instrument_that_differs_from_the_mapping_is_unsupported():
    policy = MappingPolicy.from_toml(MAPPING)
    odd = act("TLTX").model_copy(update={"source_market_symbol": "TLT"})
    _, [d] = policy.apply(ParsedPost(actions=[odd]))
    assert d.status == "UNSUPPORTED" and d.reason == "INTERPRETED_INSTRUMENT_DIFFERS_FROM_MAPPING"
