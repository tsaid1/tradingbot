"""The entry anchor and front-month rules after their review (findings 1-7): default HOLD.

A stated number may anchor an entry only with positive evidence, next to it, that it is the market
now; any relative contract wording holds; a month on the level line must be the order's month; a
reference-series anchor needs a basis cap; an anchored entry always needs a stop; the limits have
hard ceilings in code; a separate level line may name only this family. Invented data only.
"""

from __future__ import annotations

import json
import re
import tomllib
from decimal import Decimal

import pytest
from test_entry_anchor_policy import (
    ANCHOR_RULE,
    APPROVAL,
    LONG,
    MAP_NOW,
    RULE,
    SESSION,
    action,
    etf_entry,
    oil_action,
    oil_entry,
    policy,
    q_with,
    resolve,
    run_posts,
)
from test_entry_anchor_policy import outcomes as _outcomes
from test_multi_asset_gateway import act, gate, leg, position, terms

from tradingbot.event_models import ParsedPost, Timing, TradeAction
from tradingbot.execution.broker import ContractRef
from tradingbot.models import Action, AssetClass, MockPolicy
from tradingbot.runtime.mapping import AnchorLexicon, FamilyMapping, MappingPolicy

# ================================================================== 1. what makes a level an anchor

REVIEWER_POSTS = [
    "We are now long $QOIL. Stop 69.80. 71.60 was the high.",
    "We are now long $QOIL. Stop 69.80. 71.60 yesterday.",
    "We are now long $QOIL, 72 is resistance. Stop 69.80.",
    "We are now long $QOIL above 71.40. Stop 69.80.",
]


@pytest.mark.parametrize("post", REVIEWER_POSTS)
def test_reviewer_probe_a_number_that_is_not_the_market_never_trades(tmp_path, post):
    run = run_posts(tmp_path, "probe", [post])
    [(kind, status, reasons)] = _outcomes(run)
    assert kind == "OPEN" and status == "HOLD", reasons
    assert run.broker.submit_calls == 0
    run.close()


NOT_ANCHORS = [
    "We are now long $QETF, 50.10 was the high.",
    "We are now long $QETF, 50.10 yesterday.",
    "We are now long $QETF, 50.10 is resistance.",
    "We are now long $QETF above 50.10.",
    "We are now long $QETF, 50.10.",  # no present-market marker at all
    "We are now long $QETF, 50.10 now was the low.",
    "We are now long $QETF, now 50.10 from the open.",
    "We are now long $QETF, 50.10 now if it holds.",
    "We are now long $QETF, target 50.10 now.",
    "We are now long $QETF, 50.10 now versus last week.",
    "We are now long $QETF, 50.10 at the close.",
    "We are now long $QETF, 50.10 tomorrow.",
    "We are now long $QETF, currently above 50.10.",
    "We are now long $QETF, 50.10 once it breaks.",
    "We are now long $QETF, 50.10 support now.",
    "We are now long $QETF, 50.10 earlier.",
    "We are now long $QETF, now 50.10 or lower.",
    "We are now long $QETF, 50.10 now until the settlement.",
]


@pytest.mark.parametrize("clause", NOT_ANCHORS)
def test_a_level_without_present_market_evidence_is_not_an_anchor(tmp_path, clause):
    d, inst = resolve(policy(tmp_path, etf_entry()), action(), clause,
                      [("REFERENCE", "50.10"), ("STOP", "48.90")])
    assert d.status == "UNRESOLVED" and inst is None and d.entry_anchor is None
    # Without a marker: not current; with one, the context allow-list or the redundant
    # disqualifying-word check refuses it.
    assert d.reason.split(",")[0] in ("ENTRY_ANCHOR_NOT_CURRENT", "ENTRY_ANCHOR_CONTEXT_UNCLEAR")


ANCHORS = [
    ("We are now long $QETF, 50.10 now.", None),
    ("We are now long $QETF, now 50.10.", None),
    ("We are now long $QETF, now at 50.10.", None),
    ("We are now long $QETF, currently 50.10.", None),
    ("We are now long $QETF, currently at 50.10.", None),
    ("We are now long $QETF, trading at 50.10.", None),
    ("We are now long $QETF, here at 50.10.", None),  # owner's safeguard of 12 Nov 2026: see test_entry_rule_level_kind.py
    ("We are now long $QETF, 50.10 right now.", None),
    ("We are now long $QETF, 50.10 at the market.", None),
    ("Long $QETF, now 50.10.", None),
    (LONG, "We are now long $QETF.\n50.10 now."),
    (LONG, "We are now long $QETF.\n$QETF currently 50.10."),
    (LONG, "We are now long $QETF.\n$50.10 now.\nStop 48.90."),  # review S4 (12 Nov): "~" holds
    (LONG, "We are now long $QETF.\nQETF trading at 50.10 USD."),
]


@pytest.mark.parametrize("clause,post", ANCHORS)
def test_a_level_with_an_attached_present_market_marker_is_an_anchor(tmp_path, clause, post):
    d, _ = resolve(policy(tmp_path, etf_entry()), action(), clause,
                   [("REFERENCE", "50.10"), ("STOP", "48.90")], post_text=post)
    assert d.status == "SUPPORTED", d.reason
    assert d.entry_anchor == "50.10"


def test_a_level_line_without_a_marker_is_not_an_anchor(tmp_path):
    for line in ("50.10 was the high.", "50.10 yesterday.", "50.10.", "50.10 Friday now."):
        d, _ = resolve(policy(tmp_path, etf_entry()), action(), LONG, [("REFERENCE", "50.10")],
                       post_text=f"{LONG}\n{line}")
        assert d.status == "UNRESOLVED" and d.reason.startswith("ENTRY_ANCHOR_NOT_CURRENT"), line


def test_a_private_marker_extends_the_list_and_nothing_else(tmp_path):
    clause = "We are now long $QETF, 50.10 on the tape."
    plain = policy(tmp_path, etf_entry())
    d, _ = resolve(plain, action(), clause, [("REFERENCE", "50.10")])
    assert d.reason.startswith("ENTRY_ANCHOR_NOT_CURRENT")
    extended = policy(tmp_path, etf_entry())
    extended.anchor_lexicon = AnchorLexicon(present_markers=["on the tape"])
    d, _ = resolve(extended, action(), clause, [("REFERENCE", "50.10")])
    assert d.status == "SUPPORTED" and d.entry_anchor == "50.10"
    # A private marker never overrides a disqualifying word.
    d, _ = resolve(extended, action(), "We are now long $QETF, 50.10 on the tape, was the high.",
                   [("REFERENCE", "50.10")])
    assert d.status == "UNRESOLVED"  # the whole sentence is the scope: "was the high" refuses
    d, _ = resolve(extended, action(), "We are now long $QETF, 50.10 high on the tape.",
                   [("REFERENCE", "50.10")])
    assert d.reason.startswith("ENTRY_ANCHOR_NOT_CURRENT")


@pytest.mark.parametrize("marker", ["the high", "above", "now 50", "", "x" * 41, "yesterday now"])
def test_a_private_marker_that_is_itself_a_qualifier_or_malformed_refuses_to_load(marker):
    with pytest.raises(ValueError):
        AnchorLexicon(present_markers=[marker])


def test_the_private_lexicon_file_carries_the_anchor_section_without_touching_the_interpreter(
        tmp_path):
    from tradingbot.runtime.service import load_lexicons
    f = tmp_path / "lex.json"
    f.write_text(json.dumps({"open_present": [r"\binvented\b"],
                             "entry_anchor": {"present_markers": ["on the tape"],
                                              "family_aliases": {"QETF": ["bond fund"]}}}))
    interp, anchor = load_lexicons(f)
    assert interp.open_present == [r"\binvented\b"]
    assert anchor.present_markers == ["on the tape"] and anchor.family_aliases == {
        "QETF": ["bond fund"]}
    f.write_text(json.dumps({"open_present": []}))
    _, anchor = load_lexicons(f)
    assert anchor == AnchorLexicon()


# ================================================================== 2. relative contract wording

RELATIVE = [
    "We are now long the $QOIL back month at 71.50.",
    "We are now long the $QOIL next month contract at 71.50.",
    "We are now long the $QOIL deferred contract at 71.50.",
    "We are now long the second $QOIL contract at 71.50.",
    "We are now long the following $QOIL month at 71.50.",
    "We are now long the far $QOIL contract at 71.50.",
    "We are now long the $QOIL spot month at 71.50.",
    "We are now long the $QOIL calendar at 71.50.",
    "We are now long the $QOIL spread at 71.50.",
    "We are now long $QOIL, rolling into the new contract at 71.50.",
    "We are now long the nearby $QOIL at 71.50.",
    "We are now long the $QOIL front month at 71.50.",
]


@pytest.mark.parametrize("clause", RELATIVE)
def test_relative_contract_wording_holds_even_with_an_explicit_entry_and_the_rule_off(
        tmp_path, clause):
    p = policy(tmp_path, oil_entry(rule=""))
    d, inst = resolve(p, oil_action(entry="71.50"), clause, [("ENTRY", "71.50")])
    assert d.status == "UNRESOLVED" and inst is None, d.reason
    assert d.reason.split(",")[0] == "CONTRACT_MONTH_UNRECOGNISED"
    assert any(r.startswith("RELATIVE_CONTRACT:") for r in d.reason.split(","))


@pytest.mark.parametrize("post", [
    "We are now long the $QOIL back month, 71.50 now. Stop 69.80.",
    "We are now long the $QOIL next month contract, 71.50 now. Stop 69.80.",
    "We are now long the $QOIL deferred contract, 71.50 now. Stop 69.80.",
])
def test_reviewer_probe_back_month_never_trades_the_front(tmp_path, post):
    run = run_posts(tmp_path, "back", [post])
    [(_, status, reasons)] = _outcomes(run)
    assert status == "HOLD", reasons
    assert {"CONTRACT_MONTH_UNRECOGNISED", "ENTRY_ANCHOR_CONTEXT_UNCLEAR"} & set(reasons), reasons
    assert run.broker.submit_calls == 0
    run.close()


def test_relative_wording_on_the_level_line_holds(tmp_path):
    d, _ = resolve(policy(tmp_path, oil_entry()), oil_action(), "We are now long $QOIL.",
                   [("REFERENCE", "71.50")], post_text="We are now long $QOIL.\n"
                                                        "Back month 71.50 now.")
    assert d.status == "UNRESOLVED", d.reason


def test_an_index_future_selected_nearest_holds_on_relative_wording_too(tmp_path):
    from test_entry_anchor_policy import service_mapping
    f = tmp_path / "m.toml"
    f.write_text(service_mapping(rule=False))
    p = MappingPolicy.from_toml(f)
    a = action("QIDX", entry="5010", stop="4980")
    d, _ = p.resolve(a, clause="We are now long the $QIDX back month at 5,010.", now=MAP_NOW)
    assert d.status == "UNRESOLVED" and d.reason.startswith("CONTRACT_MONTH_UNRECOGNISED")


# ================================================================== 3. a month on the level line


def test_reviewer_probe_a_level_line_ending_in_a_month_never_trades(tmp_path):
    run = run_posts(tmp_path, "aug", ["We are now long $QOIL. Stop 69.80. 71.60 Aug."])
    [(_, status, reasons)] = _outcomes(run)
    assert status == "HOLD" and run.broker.submit_calls == 0, reasons
    run.close()
    d, _ = resolve(policy(tmp_path, oil_entry()), oil_action(), "We are now long $QOIL.",
                   [("REFERENCE", "71.60")],
                   post_text="We are now long $QOIL. Stop 69.80. 71.60 Aug.")
    assert d.status == "UNRESOLVED", d.reason


@pytest.mark.parametrize("line", ["Aug 71.60 now.", "QOFQ6 71.60 now.", "Aug 2026 71.60 now."])
def test_reviewer_probe_a_level_line_for_another_month_holds(tmp_path, line):
    d, inst = resolve(policy(tmp_path, oil_entry()), oil_action(), "We are now long $QOIL.",
                      [("REFERENCE", "71.60")], post_text=f"We are now long $QOIL. Stop 69.80.\n"
                                                          f"{line}")
    assert d.status == "UNRESOLVED" and inst is None, d.reason
    # A line naming the family's own contract code is an explicit quotation and reaches the
    # contract check; a bare month line after "Stop 69.80." is held first by the owner's
    # safeguard (its context is a stop).
    assert d.reason.split(",")[0] in ("CONTRACT_MONTH_UNRECOGNISED",
                                      "ENTRY_ANCHOR_LEVEL_NOT_MARKET_QUOTE"), d.reason


def test_a_level_line_naming_the_orders_own_contract_is_accepted(tmp_path):
    d, inst = resolve(policy(tmp_path, oil_entry()), oil_action(), "We are now long $QOIL.",
                      [("REFERENCE", "71.60")],
                      post_text="We are now long $QOIL. Stop 69.80.\nQOFN6 71.60 now.")
    assert d.status == "SUPPORTED", d.reason
    assert inst.futures_contract == "202607" and d.entry_anchor == "71.60"
    # The clause names August and the line July: the line is not the order's contract.
    d, _ = resolve(policy(tmp_path, oil_entry()), oil_action(), "We are now long $QOIL Aug 2026.",
                   [("REFERENCE", "71.60")],
                   post_text="We are now long $QOIL Aug 2026. Stop 69.80.\nQOFN6 71.60 now.")
    assert d.status == "UNRESOLVED" and d.reason.startswith("CONTRACT_MONTH_UNRECOGNISED")


# ================================================================== 4. the basis cap


def test_reviewer_probe_reference_trigger_without_backstop_refuses_an_unbounded_limit():
    t = terms("REFERENCE_TRIGGER", **RULE)  # require_backstop false, cap 40
    g = gate(act(entry=None, stop="4980", target="5070"),
             q_with(t, "5010", bid="5400", ask="5400.25", reference=leg()))
    assert g.status == "HOLD" and g.plan is None, g.reasons
    assert g.reasons[0] == "BASIS_EXCEEDS_CAP"


@pytest.mark.parametrize("basis", ["BASIS_SHIFT", "REFERENCE_TRIGGER"])
def test_a_reference_series_anchor_without_a_basis_cap_holds(basis):
    t = terms(basis, **RULE).model_copy(update={"max_abs_basis": None})
    g = gate(act(entry=None, stop="4980", target="5070"), q_with(t, "5010", reference=leg()))
    assert g.status == "HOLD" and g.reasons[0] == "ENTRY_ANCHOR_BASIS_CAP_MISSING", g.reasons


def test_a_reference_trigger_anchored_add_is_bounded_too():
    t = terms("REFERENCE_TRIGGER", **{**RULE, "max_notional": Decimal(100000)})
    a = act(Action.ADD, entry=None, idea="9:a1")
    g = gate(a, q_with(t, "5010", bid="5400", ask="5400.25", reference=leg()), position(qty=1))
    assert g.status == "HOLD" and g.reasons[0] == "BASIS_EXCEEDS_CAP", g.reasons


IDX_RT = """
[[mapping]]
family = "QIDX"
basis = "REFERENCE_TRIGGER"
{approval}
reference = {{ sec_type = "IND", symbol = "QIDX", exchange = "SIMIDX", currency = "USD" }}
execution = {{ sec_type = "FUT", symbol = "QIF", exchange = "SIMFUT", currency = "USD", multiplier = "5", min_tick = "0.25" }}
contract_selection = {{ rule = "EXPLICIT_OR_NEAREST", min_business_days_to_roll = 6 }}
contracts = [ {{ month = "202609", roll_date = 2026-09-11 }} ]
sizing = {{ units_per_signal = 2, max_units = 4, max_notional = "60000" }}
{session}
roll = {{ business_days_before = 3, action = "FLATTEN" }}
reference_trigger = {{ released_order_type = "MKT", reference_hours = ["13:30-20:00"], require_backstop = false }}
entry_rule = "REFERENCE_AS_COLLAR"
entry_anchor = {{ max_adverse_move = "0.5%", max_favourable_move = "1%", max_signal_age_seconds = 60, anchor_scope = "SENTENCE_STRICT"{cap}, max_quote_age_seconds = 3, environment = "PAPER", max_stop_distance = "5%", standalone_level_sentence = true }}
"""


def load(text):
    return FamilyMapping.model_validate(tomllib.loads(text)["mapping"][0])


def test_the_loader_refuses_a_reference_trigger_anchor_without_a_basis_cap():
    with pytest.raises(ValueError, match="basis cap"):
        load(IDX_RT.format(approval=APPROVAL, session=SESSION, cap=""))
    entry = load(IDX_RT.format(approval=APPROVAL, session=SESSION,
                               cap=', max_abs_basis = "30", max_basis_age_seconds = 5'))
    inst = entry.select(action("QIDX", entry="5010"), "QIDX",
                        __import__("tradingbot.runtime.instruction_terms",
                                   fromlist=["x"]).extract_terms(""), MAP_NOW.date(), None)[0]
    c = ContractRef(symbol="QIF", asset_class=AssetClass.FUTURE, sec_type="FUT",
                    exchange="SIMFUT", con_id=3, expiry="20260918", multiplier=Decimal(5),
                    min_tick=Decimal("0.25"))
    r = ContractRef(symbol="QIDX", asset_class=AssetClass.INDEX, sec_type="IND",
                    exchange="SIMIDX", con_id=4)
    t = entry.terms(inst, c, r)
    assert t.anchor_max_abs_basis == Decimal(30) and t.anchor_max_basis_age_seconds == 5


def test_the_loader_refuses_a_second_cap_beside_the_basis_shift_cap():
    text = etf_entry(rule=ANCHOR_RULE.replace("max_signal_age_seconds = 60",
                                              'max_signal_age_seconds = 60, max_abs_basis = "1"'))
    with pytest.raises(ValueError, match="basis cap"):
        load(text)


# ================================================================== 5. a stop is mandatory


def test_an_anchored_open_needs_a_stop_even_when_the_policy_does_not():
    lax = MockPolicy(max_notional_usd=Decimal(100000), require_stop_on_open=False)
    g = gate(act(entry=None, stop=None), q_with(terms(**RULE), "5025"), policy=lax)
    assert g.status == "HOLD" and g.reasons == ["PROTECTIVE_STOP_MISSING"]


def test_an_anchored_add_needs_the_idea_to_have_a_stop():
    t = terms(**{**RULE, "max_notional": Decimal(100000)})
    g = gate(act(Action.ADD, entry=None, idea="9:a1"), q_with(t, "5025"),
             position(qty=1, stop=None))
    assert g.status == "HOLD" and g.reasons == ["PROTECTIVE_STOP_MISSING"]


def test_the_service_refuses_the_rule_with_a_policy_that_allows_an_open_without_a_stop(
        tmp_path, monkeypatch):
    from tradingbot.runtime import mock_multi_asset
    monkeypatch.setitem(mock_multi_asset.POLICY, "require_stop_on_open", False)
    with pytest.raises(ValueError, match="require_stop_on_open"):
        run_posts(tmp_path, "lax", ["We are now long $QOIL, 71.50 now. Stop 69.80."])


# ================================================================== 6. hard ceilings


@pytest.mark.parametrize("change,message", [
    (('"0.5%"', '"100%"'), "ceiling"),
    (('"0.5%"', '"1.5%"'), "ceiling"),
    (('"1%"', '"500%"'), "ceiling"),
    (('"1%"', '"2.5%"'), "ceiling"),
    (('"0.5%"', '"99999"'), "ceiling"),
    (('"1%"', '"99999"'), "ceiling"),
    (("max_signal_age_seconds = 60", "max_signal_age_seconds = 121"), "ceiling"),
])
def test_the_loader_refuses_limits_above_the_ceilings(change, message):
    with pytest.raises(ValueError, match=message):
        load(etf_entry(rule=ANCHOR_RULE.replace(*change)))


def test_limits_at_the_ceilings_load():
    rule = ('entry_rule = "REFERENCE_AS_COLLAR"\nentry_anchor = { max_adverse_move = "1%", '
            'max_favourable_move = "2%", max_signal_age_seconds = 120, '
            'anchor_scope = "SENTENCE_STRICT", max_quote_age_seconds = 3, environment = "PAPER", max_stop_distance = "5%" }')
    assert load(etf_entry(rule=rule)).entry_anchor.max_signal_age_seconds == 120
    # An absolute move within 1% of the highest price the family's sizing can buy (5000 / 10).
    assert load(etf_entry(rule=ANCHOR_RULE.replace('"0.5%"', '"5"'))).entry_anchor


@pytest.mark.parametrize("kw", [{"anchor_max_adverse_move": "5%"},
                                {"anchor_max_favourable_move": "50%"},
                                {"anchor_max_adverse_move": "400"},
                                {"anchor_max_signal_age_seconds": 3600}])
def test_the_gateway_refuses_terms_above_the_ceilings(kw):
    g = gate(act(entry=None), q_with(terms(**{**RULE, **kw}), "5025"))
    assert g.status == "HOLD" and g.reasons[0] == "ENTRY_ANCHOR_LIMIT_ABOVE_CEILING", g.reasons


def test_the_gateway_refuses_limits_wider_than_the_policy_collar():
    tight = MockPolicy(max_notional_usd=Decimal(100000), max_price_deviation_pct=Decimal("0.1"))
    g = gate(act(entry=None), q_with(terms(**RULE), "5025"), policy=tight)
    assert g.status == "HOLD" and g.reasons[0] == "ENTRY_ANCHOR_LIMIT_ABOVE_CEILING"


# ================================================================== 7. whose level is it


@pytest.mark.parametrize("line", ["Brent 50.10 now.", "Other fund 50.10 now.",
                                  "Gold is 50.10 now.", "QBOND 50.10 now.",
                                  "The fund is at 50.10 now."])
def test_a_level_line_naming_anything_but_this_family_is_refused(tmp_path, line):
    d, _ = resolve(policy(tmp_path, etf_entry()), action(), LONG, [("REFERENCE", "50.10")],
                   post_text=f"{LONG}\n{line}")
    assert d.status == "UNRESOLVED", d.reason
    assert d.reason.split(",")[0] in ("ENTRY_ANCHOR_INSTRUMENT_UNCLEAR",
                                      "ENTRY_ANCHOR_CONTEXT_UNCLEAR")


def test_reviewer_probe_a_plain_word_market_on_the_level_line(tmp_path):
    d, _ = resolve(policy(tmp_path, oil_entry()), oil_action(), "We are now long $QOIL.",
                   [("REFERENCE", "71.45")],
                   post_text="We are now long $QOIL. Stop 69.80.\nBrent 71.45 now.")
    assert d.status == "UNRESOLVED"
    assert d.reason.split(",")[0] in ("ENTRY_ANCHOR_INSTRUMENT_UNCLEAR",
                                      "ENTRY_ANCHOR_CONTEXT_UNCLEAR")


def test_an_alias_from_the_mapping_entry_or_the_private_lexicon_names_this_family(tmp_path):
    post = f"{LONG}\nBond fund 50.10 now."
    d, _ = resolve(policy(tmp_path, etf_entry(extra='aliases = ["bond fund"]')), action(), LONG,
                   [("REFERENCE", "50.10")], post_text=post)
    assert d.status == "SUPPORTED" and d.entry_anchor == "50.10", d.reason
    p = policy(tmp_path, etf_entry())
    p.anchor_lexicon = AnchorLexicon(family_aliases={"QETF": ["bond fund"]})
    d, _ = resolve(p, action(), LONG, [("REFERENCE", "50.10")], post_text=post)
    assert d.status == "SUPPORTED", d.reason
    p.anchor_lexicon = AnchorLexicon(family_aliases={"QOTHER": ["bond fund"]})
    d, _ = resolve(p, action(), LONG, [("REFERENCE", "50.10")], post_text=post)
    assert d.reason.split(",")[0] in ("ENTRY_ANCHOR_INSTRUMENT_UNCLEAR",
                                      "ENTRY_ANCHOR_CONTEXT_UNCLEAR")


@pytest.mark.parametrize("alias", ["high", "now", "", "x y z w", "fund 2"])
def test_an_alias_that_is_a_qualifier_marker_or_malformed_refuses_to_load(alias):
    with pytest.raises(ValueError):
        load(etf_entry(extra=f'aliases = ["{alias}"]'))


def test_an_attached_commentary_quote_must_name_only_this_family(tmp_path):
    p = policy(tmp_path, etf_entry())
    note = TradeAction(action_id="a2", action=Action.NO_TRADE, timing=Timing.COMMENTARY,
                       source_market_symbol="QETF", evidence=[])
    parsed = ParsedPost(actions=[action(), note])
    for quote, ok in (("$QETF trading at 50.10.", True), ("The fund is at 50.10 now.", False)):
        _, [d, _] = p.apply(parsed, clauses={"a1": LONG, "a2": quote}, now=MAP_NOW,
                            post_text=f"{LONG} {quote}",
                            levels={"a1": [("REFERENCE", "50.10")],
                                    "a2": [("REFERENCE", "50.10")]})
        assert (d.status == "SUPPORTED") is ok, (quote, d.reason)


# ================================================================== the shipped file is unchanged


def test_the_ceilings_are_code_constants():
    from tradingbot import validator
    assert validator.ANCHOR_MAX_ADVERSE_PCT_CEILING == Decimal(1)
    assert validator.ANCHOR_MAX_FAVOURABLE_PCT_CEILING == Decimal(2)
    assert validator.ANCHOR_MAX_SIGNAL_AGE_CEILING_SECONDS == 120
    assert re.search(r"ANCHOR_MAX_ADVERSE_PCT_CEILING", (
        __import__("pathlib").Path(validator.__file__).read_text()))
