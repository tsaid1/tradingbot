"""The layout of the adjudication form: the page is read by three levels of heading, every
control has the control width, and after the reveal a verdict follows under the system's value
with its three choices on one line (2026-11-22). Presentation only: the values, the widget keys,
the labels, the options and the saved records are what they were. INVENTED demo bundle only."""

from __future__ import annotations

import tomllib

import _review_modes as modes
import pytest
from streamlit.delta_generator import DeltaGenerator
from test_adjudication import fill, save
from test_adjudication_option_texts import app_code
from test_adjudication_rubric import (
    APP,
    APPROVER,
    DEMO,
    REVIEWER,
    after_reading,
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
STEPS = ["1. Source evidence", "2. Your reading", "3. The system's reading"]
TABLE = "T. The price table: one row per number of the post, in text order"
GROUPS = [TABLE if g == "T" else f"{g}. {title}" for g, title in R.GROUPS]
SCALE = APP.parent / ".streamlit" / "config.toml"


@pytest.fixture
def base(tmp_path):
    return DEMO.write_demo(tmp_path / "adjudication")


@pytest.fixture
def widths(monkeypatch) -> dict:
    """key -> the width a text input or a select control was given, as the page asks for it."""
    seen: dict = {}
    for name in ("text_input", "selectbox"):
        original = getattr(DeltaGenerator, name)

        def spy(self, *args, _original=original, **kwargs):
            seen[kwargs.get("key")] = kwargs.get("width")
            return _original(self, *args, **kwargs)

        monkeypatch.setattr(DeltaGenerator, name, spy)
    return seen


def answered(monkeypatch, base):
    b = bundle(base)
    at = pick(app(monkeypatch, base, REVIEWER.login), ITEM)
    fill(at, IND, answers_of(b, b.items[ITEM], SCENARIO["forms"][0]))
    return at


def headings(at) -> tuple[list[str], list[str], list[str]]:
    return ([str(h.value) for h in at.title], [str(h.value) for h in at.header],
            [str(h.value) for h in at.subheader])


def test_the_page_is_read_by_three_levels_of_heading(monkeypatch, local, base):
    """The title, the three steps of an item, and the field groups of the form: a group title
    is a heading, not a grey caption under which the questions weigh more than their title."""
    at = answered(monkeypatch, base)
    assert headings(at) == (["Trading signal adjudication"], STEPS, GROUPS)
    assert not any(str(c.value).startswith(tuple(f"{g}. " for g, _ in R.GROUPS))
                   for c in at.caption)
    at = save(at, IND)  # the reveal: the same headings, in the same order
    assert headings(at) == (["Trading signal adjudication"], STEPS, GROUPS)
    assert not any(str(c.value).startswith(tuple(f"{g}. " for g, _ in R.GROUPS))
                   for c in at.caption)


def test_the_approver_reads_the_same_three_steps(monkeypatch, local, base):
    at, b = save(answered(monkeypatch, base), IND), bundle(base)
    [ind] = [r for r in records(base) if r["phase"] == A.INDEPENDENT]
    after = f"aw_{ITEM}_after_{ind['seq']}"
    fill(at, after, after_reading(b, b.items[ITEM], ind["reading"]))
    save(at, after)
    assert [r["phase"] for r in records(base, markers=False)] == [A.INDEPENDENT, A.AFTER_REVEAL]
    seen = pick(app(monkeypatch, base, APPROVER.login), ITEM)
    steps = ["1. Source evidence", "2. The reviewers' readings", "3. The system's reading"]
    assert headings(seen) == (["Trading signal adjudication"], steps, [])  # no form yet
    seen.radio(key=f"aw_{ITEM}_appr_{records(base)[-1]['seq']}_decision").set_value("AMEND").run()
    assert headings(seen) == (["Trading signal adjudication"], steps, GROUPS)  # the amendment form
    assert not any(str(c.value).startswith(tuple(f"{g}. " for g, _ in R.GROUPS))
                   for c in seen.caption)


def test_a_typed_value_and_the_count_of_further_actions_have_the_control_width(
        monkeypatch, local, base, widths):
    """A typed value (a number, an instrument) and the count of further actions were as wide as
    the page; they have the width of every other control of the form. The price table's own
    narrower controls keep theirs."""
    code = app_code("CONTROL_PX", "ROW_PX")
    at = answered(monkeypatch, base)
    typed = [f for f in R.VALUE_SENTINELS if any(t.key == f"{IND}_{f}" for t in at.text_input)]
    assert {"entry_price", "stop_price", "targets", "instrument_as_read"} <= set(typed)
    assert {widths[f"{IND}_{f}"] for f in typed} == {code["CONTROL_PX"]}
    assert widths[f"{IND}_aa_n"] == code["CONTROL_PX"]
    assert widths[f"{IND}_timing"] == code["CONTROL_PX"]  # as the lists beside it
    assert widths[f"{IND}_lv_n:0_role"] == code["ROW_PX"]["role"]


def test_after_the_reveal_a_verdict_follows_under_the_systems_value(monkeypatch, local, base):
    """The system's value is one line and the verdict follows under it at the form's width: no
    narrow column beside the form breaks its three choices over two lines."""
    at = save(answered(monkeypatch, base), IND)
    verdicts = [r for r in at.radio if "_verdict_" in (r.key or "") or "_lvv_" in (r.key or "")]
    assert len(verdicts) > 10 and len(at.columns) == 0
    assert all(r.proto.horizontal and list(r.options) == [R.VERDICT_TEXT[v] for v in A.VERDICTS]
               for r in verdicts)
    systems = [str(t.value) for t in at.text if str(t.value).startswith("system: ")]
    assert len(systems) >= len(verdicts)
    assert "st.columns" not in APP.read_text()


def test_the_heading_scale_beside_the_page_holds_sizes_only():
    """Streamlit reads this file for every instance of the page, the published one included: it
    may size the three levels of heading and nothing else (never a server option)."""
    scale = tomllib.loads(SCALE.read_text())
    assert set(scale) == {"theme"}
    assert set(scale["theme"]) == {"headingFontSizes", "headingFontWeights"}
    sizes = [float(s.removesuffix("rem")) for s in scale["theme"]["headingFontSizes"]]
    assert len(sizes) == 3 and sizes == sorted(sizes, reverse=True) and 1 < sizes[2] < sizes[0] < 2
    assert all(isinstance(w, int) and 500 <= w <= 700 for w in scale["theme"]["headingFontWeights"])
