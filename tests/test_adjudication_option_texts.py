"""The form's choice controls list CODES only and the page gives the complete meaning of every
option beside them. A select control draws an option on one line and cuts a long text off instead
of wrapping it; a reviewer could not read the meanings of the price roles (2026-11-19).
Presentation only: the values, the widget keys and the saved records are what they were.
INVENTED demo bundle only."""

from __future__ import annotations

import ast
import re

import _review_modes as modes
import pytest
from test_adjudication import fill, save
from test_adjudication_rubric import (
    APP,
    DEMO,
    REVIEWER,
    answers_of,
    app,
    bundle,
    pick,
    records,
)

from tradingbot.runtime import adjudication as A
from tradingbot.runtime import adjudication_rubric as R
from tradingbot.runtime import adjudication_scenarios as SC

local = modes.local  # fixture
SCENARIO = next(s for s in SC.SCENARIOS if s["n"] == 1)  # an OPEN with three numbers
ITEM = SC.item_id(SCENARIO, 0)
IND = f"aw_{ITEM}_ind"


@pytest.fixture
def base(tmp_path):
    return DEMO.write_demo(tmp_path / "adjudication")


def filled(monkeypatch, base, further=1):
    """Scenario 1's form answered through its widgets, with ``further`` further actions asked."""
    b = bundle(base)
    at = pick(app(monkeypatch, base, REVIEWER.login), ITEM)
    fill(at, IND, answers_of(b, b.items[ITEM], SCENARIO["forms"][0]))
    if further:
        at.selectbox(key=f"{IND}_aa_n").set_value(further).run()
    return at


def choice_controls(at, prefix) -> list[tuple[str, object]]:
    """(rubric field, control) of every choice control of the form with that key prefix: the
    fields, the price-table rows, the further actions and the flags (not the count of further
    actions, whose options are numbers)."""
    out = []
    for w in (*at.selectbox, *at.multiselect):
        key = w.key or ""
        if not key.startswith(prefix + "_") or key == f"{prefix}_aa_n":
            continue
        name = key.removeprefix(prefix + "_")
        row = re.fullmatch(r"(?:lv_.+?_|aa_\d+_)(\w+)", name)  # a price row, a further action
        out.append((row.group(1) if row else name, w))
    return out


def option_texts(at, prefix) -> dict[str, list[str]]:
    return {w.key: [str(o) for o in w.options] for _, w in choice_controls(at, prefix)}


def lines(at) -> set[str]:
    return {line for t in at.text for line in str(t.value).splitlines()}


def test_every_choice_control_lists_codes_only_and_the_page_gives_each_meaning_in_full(
        monkeypatch, local, base):
    at = filled(monkeypatch, base)
    controls = choice_controls(at, IND)
    assert {f for f, _ in controls} >= {"timing", "action", "explicit", "family", "side", "idea",
                                         "position_state", "effect", "entry_role", "role",
                                         "exactness", "ambiguities", "flags"}
    assert {w.key for _, w in controls} >= {f"{IND}_lv_n:0_role", f"{IND}_aa_0_action"}
    b, shown = bundle(base), lines(at)
    for f, w in controls:
        meanings = dict(R.BY_NAME[f].values)
        for option in map(str, w.options):
            if option == "(unanswered)":
                continue
            assert ": " not in option and " " not in option, (f, option)  # the code alone
            if option in meanings:  # the complete meaning, as one line of plain text
                assert f"{option}: {meanings[option]}" in shown, (f, option)
            elif f == "family":
                assert option in b.families
            else:
                assert f == "idea" and option.startswith("SEQUENCE:"), (f, option)
    # The bundle's markets and the other listed ideas share one meaning and one line.
    markets = [m for m in b.families if m not in ("OTHER", "UNKNOWN")]
    assert f"{', '.join(markets)}: a market of the bundle" in shown
    assert (f"SEQUENCE:<idea> ({len(b.sequences) - 1} in the list): another listed idea"
            in shown)


def test_a_price_row_gives_the_complete_meaning_of_what_it_holds(monkeypatch, local, base):
    at = filled(monkeypatch, base, further=0)
    role, exact = dict(R.BY_NAME["role"].values), dict(R.BY_NAME["exactness"].values)
    texts = [str(t.value) for t in at.text]
    assert (f"role CURRENT_MARKET: {role['CURRENT_MARKET']}\n"
            f"exactness APPROXIMATE: {exact['APPROXIMATE']}") in texts
    assert f"role STOP: {role['STOP']}\nexactness EXACT: {exact['EXACT']}" in texts
    at.selectbox(key=f"{IND}_lv_n:1_role").set_value("CHART_LEVEL").run()
    texts = [str(t.value) for t in at.text]
    assert any(t.startswith(f"role CHART_LEVEL: {role['CHART_LEVEL']}") for t in texts)
    assert not any(t.startswith("role STOP: ") for t in texts)  # it follows the choice
    # The table's meanings stand above its rows, and again below a table of four rows or more.
    table = [e.label for e in at.expander if "price-table" in e.label]
    assert table == ["What the price-table choices mean, in full"]
    long = next(s for s in SC.SCENARIOS if len(s["forms"][0]["rows"]) >= 4)
    at = pick(at, SC.item_id(long, 0))
    b = bundle(base)
    prefix = f"aw_{SC.item_id(long, 0)}_ind"
    fill(at, prefix, answers_of(b, b.items[SC.item_id(long, 0)], long["forms"][0]))
    assert [e.label for e in at.expander if "price-table" in e.label] == [
        "What the price-table choices mean, in full", "The price-table choices again, in full"]


def test_option_texts_never_change_and_the_saved_choices_stay_chosen(monkeypatch, local, base):
    """The browser holds a chosen option by its TEXT (see the item selector's test): an option
    text that changed under it would drop the choice. The texts of a control depend on its field
    alone: not on a choice, not on a save, not on the phase."""
    at = filled(monkeypatch, base)
    before = option_texts(at, IND)
    at.selectbox(key=f"{IND}_lv_n:1_role").set_value("CHART_LEVEL").run()
    at.multiselect(key=f"{IND}_ambiguities").set_value(["PRICE_ROLE_UNCLEAR"]).run()
    during = option_texts(at, IND)
    assert all(during[k] == v for k, v in before.items() if k in during)
    assert at.selectbox(key=f"{IND}_timing").value == "CURRENT"  # the other choices are kept
    assert at.selectbox(key=f"{IND}_lv_n:0_role").value == "CURRENT_MARKET"
    at.selectbox(key=f"{IND}_lv_n:1_role").set_value("STOP").run()
    at.multiselect(key=f"{IND}_ambiguities").set_value([]).run()
    at.selectbox(key=f"{IND}_aa_n").set_value(0).run()
    assert {k: v for k, v in option_texts(at, IND).items()} == {
        k: v for k, v in before.items() if "_aa_" not in k}
    save(at, IND)
    [ind] = [r for r in records(base) if r["phase"] == A.INDEPENDENT]
    assert ind["reading"]["timing"] == "CURRENT"  # the stored value is the code, as before
    assert [r["role"] for r in ind["reading"]["levels"]] == ["CURRENT_MARKET", "STOP", "TARGET"]
    after = f"aw_{ITEM}_after_{ind['seq']}"  # the reveal: the same texts, the same choices
    revealed = option_texts(at, after)
    twins = {k.replace(IND, after, 1): v for k, v in before.items()}
    assert len([k for k in twins if k in revealed]) >= 12
    assert all(revealed[k] == v for k, v in twins.items() if k in revealed)
    assert at.selectbox(key=f"{after}_timing").value == "CURRENT"
    assert at.selectbox(key=f"{after}_side").value == "LONG"
    assert at.selectbox(key=f"{after}_lv_n:1_role").value == "STOP"
    assert any(str(t.value).startswith("role STOP: ") for t in at.text)


def test_every_code_fits_the_fixed_width_of_its_control(monkeypatch, local, base):
    """A choice control has a fixed width, so that its codes are whole at any window width or
    zoom. Measured in Chrome (2026-11-19): a control keeps 50 pixels for its padding and arrow,
    a chosen chip of a multiple choice 120, and a capital letter of a code takes up to 8. A
    longer code than these needs a wider control: widen it, then look at it in a browser."""
    tree = ast.parse(APP.read_text())
    px = {t.targets[0].id: ast.literal_eval(t.value) for t in tree.body
          if isinstance(t, ast.Assign) and isinstance(t.targets[0], ast.Name)
          and t.targets[0].id in ("CONTROL_PX", "ROW_PX", "FURTHER_PX")}
    at = filled(monkeypatch, base)
    checked = 0
    for f, w in choice_controls(at, IND):
        key = w.key.removeprefix(IND + "_")
        chips = w in list(at.multiselect)
        width = px["ROW_PX"][f] if key.startswith("lv_") else (
            px["FURTHER_PX"][f] if key.startswith("aa_") else px["CONTROL_PX"])
        room = (width - (120 if chips else 50)) // 8
        for option in map(str, w.options):
            assert len(option) <= room, (w.key, option, room)
            checked += 1
    assert checked > 100
    for c, codes in (("role", R.TABLE_ROLES), ("exactness", R.ROW_EXACTNESS),
                     ("contract_month", R.ROW_MONTHS)):  # contract_month is not asked above
        assert max(map(len, codes)) <= (px["ROW_PX"][c] - 50) // 8, c
