"""Mapping policy version 2: fail-closed loading, approval, contract selection, option terms.

Invented families, symbols, months and dates only.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from tradingbot.event_models import ParsedPost, PriceBasis, Timing, TradeAction
from tradingbot.execution.broker import ContractRef
from tradingbot.models import Action, AssetClass, Instrument, PositionSide
from tradingbot.runtime.instruction_terms import extract_terms
from tradingbot.runtime.mapping import (
    MAPPING_NOT_APPROVED,
    UNSUPPORTED_INSTRUMENT_MAPPING,
    MappingPolicy,
)
from tradingbot.runtime.mock_multi_asset import mapping_toml

REPO = Path(__file__).resolve().parents[1]
SHIPPED = REPO / "config" / "instrument_mapping.toml"
NOW = datetime(2026, 6, 1, 14, 0, tzinfo=UTC)


def policy(tmp_path, text: str) -> MappingPolicy:
    p = tmp_path / "m.toml"
    p.write_text(text)
    return MappingPolicy.from_toml(p)


def demo(tmp_path, approved=True) -> MappingPolicy:
    return policy(tmp_path, mapping_toml(approved=approved))


def act(asset, action=Action.OPEN, *, idea=None, aid="a1") -> TradeAction:
    return TradeAction(
        action_id=aid, action=action, timing=Timing.CURRENT,
        instrument=Instrument(asset_class=AssetClass.EQUITY, symbol=asset),
        position_side=PositionSide.LONG, idea_reference=idea,
        price_basis=PriceBasis.TRADED_CONTRACT, source_market_symbol=asset,
        entry_reference="10" if action == Action.OPEN else None, stop_price=None,
        explicit_instruction=True, evidence=["invented"],
    )


def resolve(p: MappingPolicy, a: TradeAction, clause: str, held=None):
    return p.resolve(a, clause=clause, now=NOW, held=held)


# ------------------------------------------------------------------ shipped file


def test_shipped_file_is_version_2_with_only_the_bond_etf_approved():
    p = MappingPolicy.from_toml(SHIPPED)
    assert list(p.entries) == ["TLT"] and p.executable_assets == ["TLT"]
    e = p.entries["TLT"]
    assert e.approved and e.basis == "DIRECT" and e.legacy_equivalent
    assert e.approved_by and e.approved_at is not None


def test_shipped_templates_are_valid_unapproved_examples(tmp_path):
    text = SHIPPED.read_text()
    head, templates = text.split("# ------------------------------------------------------------------ templates")
    body = []
    for line in templates.splitlines()[1:]:
        if line.startswith("# [[mapping]]") or (body and line.startswith("# ") and "=" in line
                                                 and not line.startswith("# ---")):
            body.append(line[2:])
        elif line.startswith(("# [[", "# ---")):
            continue
    p = policy(tmp_path, head + "\n".join(body))
    by_status = {f: e.status_label for f, e in p.entries.items()}
    assert by_status == {"TLT": "APPROVED", "SPX": "NOT APPROVED", "WTI": "NOT APPROVED",
                         "GOLD": "NOT APPROVED", "EXETF": "NOT APPROVED",
                         "EXUND.OPT": "NOT APPROVED"}
    assert {e.basis for e in p.entries.values()} == {"DIRECT", "BASIS_SHIFT", "REFERENCE_TRIGGER"}
    assert "EXAMPLES ONLY" in templates and p.executable_assets == ["TLT"]


# ------------------------------------------------------------------ fail-closed loading

ENTRY = """version = 2
[[mapping]]
family = "QOIL"
basis = "DIRECT"
approved = false
reference = { sec_type = "FUT", symbol = "QOF", exchange = "SIMNRG", currency = "USD" }
execution = { sec_type = "FUT", symbol = "QOF", exchange = "SIMNRG", currency = "USD", multiplier = "10", min_tick = "0.01" }
contract_selection = { rule = "EXPLICIT_ONLY", min_business_days_to_roll = 6 }
contracts = [ { month = "202607", roll_date = 2026-07-06 } ]
sizing = { units_per_signal = 1, max_units = 2, max_notional = "5000" }
session = { policy = "REGULAR_ONLY", timezone = "UTC", days = ["MON"], regular = ["13:00-21:00"] }
roll = { business_days_before = 3, action = "FLATTEN" }
"""


def test_the_base_entry_loads(tmp_path):
    assert policy(tmp_path, ENTRY).entries["QOIL"].status_label == "NOT APPROVED"


@pytest.mark.parametrize(
    "change,match",
    [
        (('basis = "DIRECT"', 'basis = "DIRECT"\nsurprise = 1'), "surprise"),  # unknown key
        (("sizing = { units_per_signal = 1, max_units = 2, max_notional = \"5000\" }\n", ""),
         "sizing and session are required"),
        (("roll = { business_days_before = 3, action = \"FLATTEN\" }\n", ""),
         "contract_selection, roll and contracts"),
        (("approved = false", "approved = true"), "approved_by and approved_at"),
        (("approved = false", 'approved = false\napproved_by = "x"'), "approved = false"),
        (('symbol = "QOF", exchange = "SIMNRG", currency = "USD" }',
          'symbol = "QOG", exchange = "SIMNRG", currency = "USD" }'), "own prices"),
        (("min_business_days_to_roll = 6", "min_business_days_to_roll = 2"), "roll window"),
        (('basis = "DIRECT"', 'basis = "BASIS_SHIFT"'), "reference series other than"),
        (('regular = ["13:00-21:00"]', 'regular = ["21:00-13:00"]'), "forward"),
        (('timezone = "UTC"', 'timezone = "Mars/Olympus"'), "timezone"),
        (('{ month = "202607", roll_date = 2026-07-06 } ]',
          '{ month = "202607", roll_date = 2026-07-06 }, { month = "202607", roll_date = 2026-07-07 } ]'),
         "listed twice"),
        (("units_per_signal = 1, max_units = 2", "units_per_signal = 3, max_units = 2"),
         "exceeds max_units"),
        (('rule = "EXPLICIT_ONLY"', 'rule = "WHATEVER"'), "rule"),
    ],
)
def test_loading_refuses_any_malformed_or_inconsistent_entry(tmp_path, change, match):
    old, new = change
    assert old in ENTRY
    with pytest.raises(ValueError, match=match):
        policy(tmp_path, ENTRY.replace(old, new))


def test_two_entries_for_one_family_refuse(tmp_path):
    dup = ENTRY + ENTRY.split("version = 2\n", 1)[1]
    with pytest.raises(ValueError, match="duplicate"):
        policy(tmp_path, dup)


def test_only_usd_execution_may_be_approved(tmp_path):
    text = ENTRY.replace('currency = "USD"', 'currency = "EUR"').replace(
        "approved = false", 'approved = true\napproved_by = "o"\napproved_at = 2026-06-01T00:00:00Z')
    with pytest.raises(ValueError, match="USD"):
        policy(tmp_path, text)


def test_option_family_rules(tmp_path):
    text = mapping_toml(approved=True)
    shifted = text.replace('family = "QSTK.OPT"\nbasis = "DIRECT"',
                           'family = "QSTK.OPT"\nbasis = "BASIS_SHIFT"')
    with pytest.raises(ValueError, match="family QSTK.OPT"):  # an option trades on its premium
        policy(tmp_path, shifted)
    with pytest.raises(ValueError, match=r"<UNDERLYING>\.OPT"):
        policy(tmp_path, text.replace('family = "QSTK.OPT"', 'family = "QSTK"'))


def test_a_reference_trigger_backstop_needs_basis_rules(tmp_path):
    text = mapping_toml(approved=True).replace(
        'basis_shift = { max_abs_basis = "40", max_age_seconds = 5 }\n', "")
    with pytest.raises(ValueError, match="require_backstop"):
        policy(tmp_path, text)


def test_version_1_files_still_load(tmp_path):
    v1 = ("[[mapping]]\nsource_asset='TLT'\nbroker_symbol='TLT'\nbroker_asset_class='ETF'\n"
          "basis='DIRECT'\n")
    assert policy(tmp_path, v1).executable_assets == ["TLT"]


# ------------------------------------------------------------------ approval


def test_no_entry_is_unsupported_and_an_unapproved_entry_is_not_approved(tmp_path):
    p = demo(tmp_path, approved=False)
    parsed, decisions = p.apply(ParsedPost(actions=[act("QOIL"), act("QZZZ", aid="a2")]),
                                clauses={"a1": "long $QOIL Jul 2026 at 10"}, now=NOW)
    assert [d.status for d in decisions] == ["NOT_APPROVED", "UNSUPPORTED"]
    assert MAPPING_NOT_APPROVED in parsed.actions[0].ambiguities
    assert UNSUPPORTED_INSTRUMENT_MAPPING in parsed.actions[1].ambiguities
    assert parsed.actions[0].instrument.symbol == "QOIL"  # never given the execution contract


def test_an_option_instruction_never_falls_back_to_another_family(tmp_path):
    text = mapping_toml(approved=True).split('[[mapping]]\nfamily = "QSTK.OPT"')[0]
    p = policy(tmp_path, text)
    d, inst = resolve(p, act("QSTK"), "buying $QSTK 2026-09-18 105 call at 4.22")
    assert d.status == "UNSUPPORTED" and d.family == "QSTK.OPT" and inst is None


# ------------------------------------------------------------------ contract selection


def test_a_named_month_is_used_and_the_rule_picks_the_nearest_outside_the_roll_window(tmp_path):
    p = demo(tmp_path)
    d, inst = resolve(p, act("QOIL"), "long $QOIL Jul 2026 at 71.5")
    assert d.status == "SUPPORTED" and d.mapped and inst.futures_contract == "202607"
    assert inst.quote_key == "FUTURE:QOF:202607:-:-"
    # QIDX: June rolls on 12 June (9 business days away, >= 6): June; QMTL: its only month.
    _, inst = resolve(p, act("QIDX"), "long $QIDX at 5010")
    assert inst.futures_contract == "202606"
    late = datetime(2026, 6, 8, 14, tzinfo=UTC)  # 4 business days before 12 June: September
    _, inst = p.resolve(act("QIDX"), clause="long $QIDX at 5010", now=late)
    assert inst.futures_contract == "202609"


@pytest.mark.parametrize(
    "asset,clause,detail",
    [
        ("QOIL", "long $QOIL at 71.5", "CONTRACT_MONTH_NOT_STATED"),  # EXPLICIT_ONLY
        ("QOIL", "long $QOIL Dec 2026 at 71.5", "MONTH_NOT_LISTED:202612"),
        ("QOIL", "long $QOIL Jun 2026 and Jul 2026 at 71.5", "SEVERAL_MONTHS_NAMED"),
        ("QIDX", "long $QIDX Mar contract at 5010", "MONTH_03_MATCHES_0"),
    ],
)
def test_contract_selection_ambiguous_holds(tmp_path, asset, clause, detail):
    p = demo(tmp_path)
    parsed, [d] = p.apply(ParsedPost(actions=[act(asset)]), clauses={"a1": clause}, now=NOW)
    assert d.status == "UNRESOLVED"
    assert parsed.actions[0].ambiguities == ["CONTRACT_SELECTION_AMBIGUOUS", detail]


def test_a_follow_up_acts_on_its_idea_contract_and_never_another_month(tmp_path):
    p = demo(tmp_path)
    held = {"9:a1": ContractRef(symbol="QOF", asset_class=AssetClass.FUTURE, sec_type="FUT",
                                expiry="20260708", con_id=1)}
    close = act("QOIL", Action.CLOSE, idea="9:a1")
    d, inst = resolve(p, close, "closing our $QOIL long", held)
    assert d.status == "SUPPORTED" and inst.futures_contract == "202607"
    d, inst = resolve(p, close, "closing our $QOIL Jun 2026 long", held)
    assert d.status == "UNRESOLVED" and d.reason.startswith("IDEA_CONTRACT_MISMATCH")


# ------------------------------------------------------------------ options


def test_a_fully_specified_option_selects_its_contract(tmp_path):
    d, inst = resolve(demo(tmp_path), act("QSTK"), "Buying $QSTK 2026-09-18 105 call at 4.22")
    assert d.status == "SUPPORTED" and d.family == "QSTK.OPT"
    assert (inst.asset_class, inst.underlying, inst.expiry, inst.strike, inst.option_right) == (
        AssetClass.OPTION, "QSTK", "2026-09-18", "105", "CALL")


@pytest.mark.parametrize(
    "clause,missing",
    [
        ("Buying $QSTK 105 call at 4.22", ["EXPIRY"]),
        ("Buying $QSTK 2026-09-18 call at 4.22", ["STRIKE"]),
        ("Buying $QSTK 2026-09-18 105 strike option at 4.22", ["STRIKE", "RIGHT"]),
        ("Buying $QSTK Sep 2026 105 call at 4.22", ["EXPIRY"]),  # a month is not an expiry
        ("Buying $QSTK 2026-09-18 105 call and 110 put at 4.22", ["STRIKE", "RIGHT"]),
    ],
)
def test_an_option_with_any_identifying_field_missing_holds(tmp_path, clause, missing):
    parsed, [d] = demo(tmp_path).apply(ParsedPost(actions=[act("QSTK")]), clauses={"a1": clause},
                                       now=NOW)
    assert d.status == "UNRESOLVED"
    assert parsed.actions[0].ambiguities == ["OPTION_TERMS_INCOMPLETE",
                                             *(f"OPTION_{m}_NOT_EXPLICIT" for m in missing)]


def test_an_option_whose_underlying_is_not_the_family_holds(tmp_path):
    p = demo(tmp_path)
    other = act("QSTK").model_copy(update={"instrument": None})
    d, _ = resolve(p, other, "Buying $QSTK 2026-09-18 105 call at 4.22")
    assert d.status == "SUPPORTED"  # the underlying is the family's asset
    wrong = act("QSTK").model_copy(
        update={"instrument": Instrument(asset_class=AssetClass.EQUITY, symbol="QOTHER")})
    d, _ = resolve(p, wrong, "Buying $QSTK 2026-09-18 105 call at 4.22")
    assert d.status == "UNSUPPORTED" and d.reason == "INTERPRETED_INSTRUMENT_DIFFERS_FROM_MAPPING"


def test_terms_are_read_only_when_stated():
    t = extract_terms("Buying $QSTK 2026-09-18 105 call; the Dec contract; Jul 2026")
    assert t.months == ["202607"] and t.bare_months == [12] and t.strikes == [Decimal(105)]
    assert t.rights == ["CALL"] and [d.isoformat() for d in t.expiries] == ["2026-09-18"]
    nothing = extract_terms("Raising our stop to 70.40 on Dec 18")
    assert not nothing.names_a_month and not nothing.expiries and not nothing.option_language


# ------------------------------------------------------------------ terms for the gateway


def test_terms_flag_a_contract_whose_specification_differs_from_the_approved_entry(tmp_path):
    e = demo(tmp_path).entries["QOIL"]
    inst = Instrument(asset_class=AssetClass.FUTURE, symbol="QOF", futures_contract="202607")
    odd = ContractRef(symbol="QOF", asset_class=AssetClass.FUTURE, sec_type="FUT",
                      expiry="20260708", multiplier=Decimal(100), min_tick=Decimal("0.01"))
    t = e.terms(inst, odd, odd)
    assert t.problems == ["CONTRACT_SPEC_MISMATCH:MULTIPLIER:10!=100"]
    assert e.terms(inst, None, None).problems == ["CONTRACT_UNRESOLVED"]
    good = odd.model_copy(update={"multiplier": Decimal(10)})
    t = e.terms(inst, good, good)
    assert t.problems == [] and t.roll_date.isoformat() == "2026-07-06"
