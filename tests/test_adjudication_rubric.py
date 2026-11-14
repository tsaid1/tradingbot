"""Rubric version 2.2 (``runtime/adjudication_rubric.py``) on the INVENTED demo bundle and the
invented scenarios (``runtime/adjudication_scenarios.py``): every scenario's expected reading
passes Save's checks, its contradictory variant is refused with the expected code, and saved,
resolved and frozen the ground_truth/2 row equals the expectation; plus per-post blind-first,
rubric-1 records, verdict rules, the approver's merged amendment, the docs drift and the form."""

from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path

import _review_modes as modes
import pytest
from _review_modes import with_headers
from streamlit.testing.v1 import AppTest

from tradingbot.runtime import adjudication as A
from tradingbot.runtime import adjudication_rubric as R
from tradingbot.runtime import adjudication_scenarios as SC
from tradingbot.runtime.vocab_recommend import append_chained

REPO = Path(__file__).resolve().parents[1]
APP = REPO / "dashboard" / "reviewer" / "adjudication_app.py"
_spec = importlib.util.spec_from_file_location(
    "adjudication_demo_bundle_r", REPO / "scripts" / "adjudication_demo_bundle.py")
DEMO = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(DEMO)
_dspec = importlib.util.spec_from_file_location(
    "adjudication_rubric_docs", REPO / "scripts" / "adjudication_rubric_docs.py")
DOCS = importlib.util.module_from_spec(_dspec)
_dspec.loader.exec_module(DOCS)
REVIEWER = A.Session("reviewer@example.invalid", "Reviewer Example", A.REVIEWER, A.FULL, False)
OTHER_REVIEWER = A.Session("remote@example.invalid", "Remote", A.REVIEWER, A.FULL, False)
APPROVER = A.Session("owner@example.invalid", "Owner", A.APPROVER, A.FULL, False)
local = modes.local  # fixture


@pytest.fixture
def base(tmp_path) -> Path:
    return DEMO.write_demo(tmp_path / "adjudication")


def bundle(base) -> A.Bundle:
    return A.load_bundle(base / "bundle", A.FULL)


def records(base, markers=True) -> list[dict]:
    v = A.read_annotations(base)
    assert v.ok, v.problem
    return [r for r in v.records if markers or r["phase"] != A.SYSTEM_SHOWN]


def answers_of(b, item, f, changes=None) -> dict:
    a = copy.deepcopy(f["answers"])
    rows = [copy.deepcopy(x) for x in f["rows"]]
    changes = dict(changes or {})
    for k, row in (changes.pop("rows", None) or {}).items():
        rows[k] = row
    for k in ("flags", "notes"):
        changes.pop(k, None)
    a.update(changes)
    if item["kind"] == "INSTRUCTION":
        a["levels"] = [{"row": key, **x} for (key, _), x in zip(A.table_rows(b, item), rows,
                                                                 strict=False)]
    return a


def extras(f, changes=None) -> dict:
    changes = changes or {}
    return {"flags": changes.get("flags", f["flags"]), "notes": changes.get("notes", f["notes"]),
            "confidence": f["confidence"]}


def check(base, b, item, reading, session, revealed, **kw):
    clean = A.clean_reading(reading, item, b)
    return A.check_reading(b, item, clean, revealed=revealed, records=records(base),
                           login=session.login, **kw)


def save_independent(base, b, sc, session=REVIEWER, variants=True) -> list[dict]:
    out = []
    for k, f in enumerate(sc["forms"]):
        item = b.items[SC.item_id(sc, k)]
        if variants and f["variant"]:
            v = f["variant"]
            reading = answers_of(b, item, f, v["changes"])
            ex = extras(f, v["changes"])
            _done, checks, _ctx = check(base, b, item, reading, session, False, **ex)
            assert v["code"] in checks.codes, (sc["n"], k, checks)
            with pytest.raises(A.AdjudicationError) as exc:
                A.append_annotation(base, b, session, item_id=item["item_id"],
                                    phase=A.INDEPENDENT, blind=True, reading=reading,
                                    acknowledged_warnings=checks.warnings, **ex)
            assert v["code"] in exc.value.codes
        reading = answers_of(b, item, f)
        _done, checks, _ctx = check(base, b, item, reading, session, False, **extras(f))
        assert checks.blocking == [], (sc["n"], k, checks.blocking)
        assert sorted(checks.warnings) == f["warnings"], (sc["n"], k)
        out.append(A.append_annotation(base, b, session, item_id=item["item_id"],
                                       phase=A.INDEPENDENT, blind=True, reading=reading,
                                       acknowledged_warnings=checks.warnings, **extras(f)))
    return out


def after_reading(b, item, independent: dict) -> dict:
    """The reviewer's reading after the reveal: unchanged, plus the mapping questions."""
    if item["kind"] == "LABEL_ONLY":
        return dict(independent)
    reading = {**independent, "reference_series_right": "", "execution_instrument_right": ""}
    ctx = A.form_context(b, item, revealed=True)
    st = R.applicability(R.complete(reading, ctx), ctx)
    for f in R.MAPPING_FIELDS:
        if st[f].state == R.ASK:
            reading[f] = "YES"
    return reading


def save_after(base, b, sc, inds, session=REVIEWER) -> list[dict]:
    out = []
    for k, (f, ind) in enumerate(zip(sc["forms"], inds, strict=True)):
        item = b.items[SC.item_id(sc, k)]
        reading = after_reading(b, item, ind["reading"])
        done, checks, ctx = check(base, b, item, reading, session, True, **extras(f))
        assert not checks.blocking, (sc["n"], k, checks.blocking)
        system = A.system_reading(b, item)
        fields = A.compared_fields(b, item, done, ctx.earlier_positions)
        verdicts = A.prefill_verdicts(done, system, item["kind"], fields)
        if done.get("levels"):
            verdicts["levels"] = A.prefill_row_verdicts(done["levels"], A.system_rows(b, item))
        out.append(A.append_annotation(
            base, b, session, item_id=item["item_id"], phase=A.AFTER_REVEAL, blind=True,
            reading=reading, field_verdicts=verdicts, refers_to=ind["seq"],
            acknowledged_warnings=checks.warnings, **extras(f)))
    return out


# ------------------------------------------------------------------ the scenarios


@pytest.mark.parametrize("sc", SC.SCENARIOS, ids=lambda s: f"scenario-{s['n']}")
def test_each_scenario_passes_its_variant_is_refused_and_the_frozen_row_is_expected(base, sc):
    b = bundle(base)
    inds = save_independent(base, b, sc)
    afters = save_after(base, b, sc, inds)
    path, manifest = A.freeze(base)
    rows = {r["item_id"]: r for r in map(json.loads, path.read_text().splitlines())}
    assert manifest["schema"] == "tradingbot.adjudication.ground_truth/2:manifest"
    for k, f in enumerate(sc["forms"]):
        iid = SC.item_id(sc, k)
        row = rows[iid]
        assert (row["schema"], row["rubric"], row["status"]) == (A.GROUND_TRUTH_SCHEMA, 2,
                                                                 "PROVISIONAL")
        assert row["reading"] == afters[k]["reading"]
        assert row["independent_reading"] == inds[k]["reading"]
        assert row["reviewer"] == REVIEWER.login
        assert (row["form_index"], row["forms_in_post"]) == (
            k + 1, sum(x["kind"] == "INSTRUCTION" for x in sc["forms"]) or 1)
        assert row["acknowledged_warnings"] == f["warnings"]
        assert row["position_after"] == R.position_after(row["reading"])
        assert row["follower_implication"] == row["reading"].get("follower_implication")
        assert row["additional_actions"] == row["reading"].get("additional_actions", [])
        for key, want in f["expect"].items():
            if key in ("position_after", "escalated"):
                assert row[key] == want, (sc["n"], key)
            elif key == "position_compared":
                item = b.items[iid]
                assert not set(R.POSITION_FIELDS) & set(
                    afters[k]["field_verdicts"]), sc["n"]
                assert A.position_compared(b, item, row["reading"]) is want
            else:
                assert row["reading"][key] == want, (sc["n"], key)
        assert row["escalated"] is (R.FORM_CANNOT_EXPRESS in f["flags"])
        if "overall" in sc:
            assert row["post_label_overall"] == sc["overall"]
    assert len(SC.SCENARIOS) == 31  # 30 scenarios, scenario 7 with and without its context
    assert all(row["schema"] == A.GROUND_TRUTH_SCHEMA and "all_readings" in row
               for row in rows.values())


def test_scenario_29_records_three_timings_and_the_post_is_actionable(base):
    sc = next(s for s in SC.SCENARIOS if s["n"] == 29)
    b = bundle(base)
    save_after(base, b, sc, save_independent(base, b, sc, variants=False))
    rows = A.resolve(records(base), b)
    timings = [r["reading"]["timing"] for r in rows] + [
        x["timing"] for r in rows for x in r["additional_actions"]]
    assert timings == ["HISTORICAL", "CURRENT", "CONDITIONAL"]
    assert {r["post_label_overall"] for r in rows} == {"ACTIONABLE"}
    assert [r["reading"]["post_label"] for r in rows] == ["RETROSPECTIVE", "ACTIONABLE"]


def test_the_post_label_overall_waits_for_every_form(base):
    sc = next(s for s in SC.SCENARIOS if s["n"] == 14)
    b = bundle(base)
    inds = save_independent(base, b, sc, variants=False)
    save_after(base, b, sc, inds[:1] + inds[1:])  # both forms
    assert {r["post_label_overall"] for r in A.resolve(records(base), b)} == {"ACTIONABLE"}
    rows = A.resolve([r for r in records(base) if r["item_id"] != "s14b"], b)
    assert [r["post_label_overall"] for r in rows] == [None]


def test_scenario_30_chain_warns_and_the_later_form_is_not_compared_on_position(base):
    sc = next(s for s in SC.SCENARIOS if s["n"] == 30)
    b = bundle(base)
    inds = save_independent(base, b, sc, variants=False)
    item = b.items["s30b"]
    wrong = answers_of(b, item, sc["forms"][1], {"position_state": "OPEN",
                                                 "position_side": "LONG",
                                                 "position_last_stop": "69.5"})
    _done, checks, ctx = check(base, b, item, wrong, REVIEWER, True, confidence="HIGH")
    assert "POSITION_CHAIN" in checks.warnings and ctx.earlier_positions[0][1]["state"] == "FLAT"
    fields = A.compared_fields(b, item, inds[1]["reading"], ctx.earlier_positions)
    assert not set(R.POSITION_FIELDS) & set(fields)
    assert set(R.POSITION_FIELDS) <= set(A.compared_fields(
        b, b.items["s30a"], inds[0]["reading"]))


# ------------------------------------------------------------------ blind-first per post


def test_blind_first_holds_per_post(base):
    sc = next(s for s in SC.SCENARIOS if s["n"] == 14)
    b = bundle(base)
    first = save_independent(base, b, {**sc, "forms": sc["forms"][:1]}, variants=False)[0]
    item = b.items["s14a"]
    reading = after_reading(b, item, first["reading"])
    with pytest.raises(A.AdjudicationError, match="BLIND_FIRST_PER_POST"):
        A.append_annotation(base, b, REVIEWER, item_id="s14a", phase=A.AFTER_REVEAL,
                            blind=True, reading=reading, field_verdicts={},
                            refers_to=first["seq"], confidence="HIGH")
    # Revealing one form of a post marks every form of it: no later independent reading.
    A.mark_system_shown(base, b, OTHER_REVIEWER, "s14a")
    marked = {r["item_id"] for r in records(base) if r["phase"] == A.SYSTEM_SHOWN}
    assert marked == {"s14a", "s14b"}
    assert all(r["rubric"] == 2 for r in records(base))
    with pytest.raises(A.AdjudicationError, match="SYSTEM_READING_ALREADY_SEEN"):
        A.append_annotation(base, b, OTHER_REVIEWER, item_id="s14b", phase=A.INDEPENDENT,
                            blind=True, reading=answers_of(b, b.items["s14b"], sc["forms"][1]),
                            confidence="HIGH")


# ------------------------------------------------------------------ records, versions, approver


def test_independent_and_reviewer_survive_a_revision_and_an_approver_amendment(base):
    sc = next(s for s in SC.SCENARIOS if s["n"] == 1)
    b = bundle(base)
    [ind] = save_independent(base, b, sc, variants=False)
    [after] = save_after(base, b, sc, [ind])
    item = b.items["s01a"]
    revised = {**after["reading"], "entry_exactness": "EXACT", "levels": [
        {**after["reading"]["levels"][0], "exactness": "EXACT"}, *after["reading"]["levels"][1:]]}
    done, _checks, _ctx = check(base, b, item, revised, REVIEWER, True, confidence="HIGH")
    verdicts = A.prefill_verdicts(done, A.system_reading(b, item), "INSTRUCTION",
                                  A.compared_fields(b, item, done))
    verdicts["levels"] = A.prefill_row_verdicts(done["levels"], A.system_rows(b, item))
    second = A.append_annotation(base, b, REVIEWER, item_id="s01a", phase=A.AFTER_REVEAL,
                                 blind=True, reading=revised, field_verdicts=verdicts,
                                 refers_to=ind["seq"], confidence="HIGH")
    # The approver amends the action to a CLOSE: what follows is derived again and checked.
    with pytest.raises(A.AdjudicationError) as exc:
        A.append_annotation(base, b, APPROVER, item_id="s01a", phase=A.APPROVAL, blind=False,
                            reading={"effect": "CLOSES"}, refers_to=second["seq"],
                            outcome="AMEND")
    assert "ACTION_EFFECT" in exc.value.codes  # checked on the MERGED reading
    A.append_annotation(base, b, APPROVER, item_id="s01a", phase=A.APPROVAL, blind=False,
                        reading={"stop_price": "69.4"}, refers_to=second["seq"],
                        outcome="AMEND")
    [row] = [r for r in A.resolve(records(base), b) if r["item_id"] == "s01a"]
    assert row["status"] == "AMENDED" and row["reading"]["stop_price"] == "69.4"
    assert row["reading"]["entry_exactness"] == "EXACT"
    assert row["independent_reading"] == ind["reading"]
    assert row["independent_reading"]["entry_exactness"] == "APPROXIMATE"
    assert (row["reviewer"], row["approver"]) == (REVIEWER.login, APPROVER.login)
    assert row["position_after"] == {"state": "OPEN", "side": "LONG", "stop": "69.4"}


def test_rubric_1_records_still_verify_are_never_rewritten_and_count_apart(base):
    b = bundle(base)
    old = {"bundle_id": b.bundle_id, "item_id": "s02a",
           "annotator": {"login": REVIEWER.login, "name": "", "role": "REVIEWER",
                         "access": "FULL"}, "blind": True, "field_verdicts": {}, "flags": [],
           "confidence": "LOW", "notes": "", "decision": None, "at": "2026-11-13T00:00:00+00:00"}
    log = A.annotations_path(base)
    append_chained(log, "annotation log", A.ANNOTATION_SCHEMA, lambda v: {
        **old, "phase": A.INDEPENDENT, "reading": {"action": "OPEN"}, "refers_to": None})
    append_chained(log, "annotation log", A.ANNOTATION_SCHEMA, lambda v: {
        **old, "phase": A.AFTER_REVEAL, "reading": {"action": "OPEN"}, "refers_to": 1})
    before = log.read_bytes()
    [row] = A.resolve(records(base), b)
    assert (row["rubric"], row["position_after"], row["escalated"]) == (1, None, False)
    assert A.progress(b, records(base), REVIEWER).status["s02a"] == "OPEN"
    with pytest.raises(A.AdjudicationError, match="RUBRIC_1_RECORD_NOT_APPROVABLE"):
        A.append_annotation(base, b, APPROVER, item_id="s02a", phase=A.APPROVAL, blind=False,
                            reading={}, refers_to=2, outcome="ACCEPT")
    _path, manifest = A.freeze(base)
    assert manifest["by_rubric"] == {"1": 1, "2": 0}
    assert log.read_bytes() == before and A.read_annotations(base).ok


def test_warnings_must_be_acknowledged_exactly(base):
    sc = next(s for s in SC.SCENARIOS if s["n"] == 7)
    b = bundle(base)
    item = b.items["s07a"]
    # Without LEVEL_BY_REFERENCE the stop number not in the table is a warning (14.9).
    reading = answers_of(b, item, sc["forms"][0], {"ambiguities": []})
    for ack, code in (((), "WARNINGS_NOT_ACKNOWLEDGED"),
                      (("STOP_NOT_IN_TABLE", "STOP_SIDE"), "WARNINGS_NOT_ACKNOWLEDGED"),
                      (("NOT_A_WARNING",), "BAD_VALUE:acknowledged_warnings")):
        with pytest.raises(A.AdjudicationError, match=code):
            A.append_annotation(base, b, REVIEWER, item_id="s07a", phase=A.INDEPENDENT,
                                blind=True, reading=reading, confidence="HIGH",
                                acknowledged_warnings=ack)
    rec = A.append_annotation(base, b, REVIEWER, item_id="s07a", phase=A.INDEPENDENT,
                              blind=True, reading=reading, confidence="HIGH",
                              acknowledged_warnings=["STOP_NOT_IN_TABLE"])
    assert rec["acknowledged_warnings"] == ["STOP_NOT_IN_TABLE"] and rec["rubric"] == 2


# ------------------------------------------------------------------ pure rules


def test_answer_states_and_verdict_rules():
    assert R.prefill_verdict("stop_price", R.NOT_APPLICABLE, "87") == "NOT_APPLICABLE"
    assert R.prefill_verdict("stop_price", R.NOT_STATED, "") == "CONFIRM"
    assert R.prefill_verdict("side", R.NOT_STATED, "NONE") == "CONFIRM"
    assert R.prefill_verdict("stop_price", R.CANNOT_DETERMINE, "87") == "UNSURE"
    assert R.prefill_verdict("stop_price", "87.0", "87") == "CONFIRM"
    assert R.prefill_verdict("stop_price", R.NOT_STATED, "87") == "CORRECT"
    assert R.prefill_row_verdict({"role": R.NOT_APPLICABLE}, {"not_read": True}) == \
        "NOT_APPLICABLE"
    assert R.prefill_row_verdict({"role": "STOP"}, {"not_read": True}) == R.NOT_READ
    for f in ("component", "quoted_series", "traded_instrument", "ambiguities",
              "additional_actions", "announcement", "follower_implication",
              *R.MAPPING_FIELDS):
        assert f not in R.compared_fields("INSTRUCTION")
    # Nothing NOT_APPLICABLE is ever typed into a field that applies.
    checks = R.validate({"timing": "CURRENT", "action": "OPEN", "side": R.NOT_APPLICABLE},
                        R.FormContext(), confidence="LOW")
    assert ("NOT_APPLICABLE_FIELD", "side") in checks.blocking


def test_follower_implication_and_position_after():
    fi = R.follower_implication
    assert fi({"timing": "CURRENT"}) == "INSTRUCTION_CANDIDATE"
    assert fi({"timing": "HISTORICAL", "announcement": "FIRST_REPORT",
               "action": "REDUCE"}) == "RECONCILE_EXIT"
    assert fi({"timing": "HISTORICAL", "announcement": "FIRST_REPORT",
               "action": "MODIFY"}) == "RECONCILE_POSITION"
    assert fi({"timing": "HISTORICAL", "announcement": "REPEAT"}) == "NO_NEW_INFORMATION"
    assert fi({"timing": "HISTORICAL", "announcement": R.CANNOT_DETERMINE}) == R.CANNOT_DETERMINE
    assert fi({"timing": "UNCERTAIN"}) == R.CANNOT_DETERMINE
    assert fi({"timing": "CONDITIONAL"}) == "WAIT_FOR_CONDITION"
    assert fi({"timing": "COMMENTARY"}) == "NONE"
    assert "not an order" in R.FOLLOWER_SENTENCE
    pa = R.position_after
    base = {"action": "ADD", "side": "LONG", "position_state": "OPEN", "position_side": "LONG",
            "position_last_stop": "68", "timing": "CURRENT"}
    assert pa({**base, "effect": "INCREASES", "stop_price": R.NOT_STATED})["stop"] == "68"
    assert pa({**base, "effect": "CANNOT_DETERMINE"})["state"] == R.CANNOT_DETERMINE
    assert pa({**base, "effect": "INCREASES", "timing": "CONDITIONAL"})["stop"] == "68"


# ------------------------------------------------------------------ the documents


def test_the_rubric_and_guide_documents_are_rendered_from_the_code():
    assert (REPO / "docs" / "ADJUDICATION_RUBRIC.md").read_text() == R.render_rubric_markdown()
    assert (REPO / "docs" / "REVIEWER_GUIDE.md").read_text() == R.render_guide_markdown()
    assert DOCS.main(["--check"]) == 0
    text = R.render_rubric_markdown()
    for f in R.RUBRIC:
        assert f"`{f.name}`" in text and f.tooltip in text
        for code, meaning in f.values:
            assert f"`{code}`: {meaning}" in text
    guide = " ".join(R.render_guide_markdown().split())
    for words in ("not an order", "do not copy", "screenshots", "nothing is overwritten",
                  "Next item not yet adjudicated", "BEFORE THIS ACTION", "no default"):
        assert words.casefold() in guide.casefold(), words


def test_every_field_has_a_question_a_tooltip_and_meanings():
    names = {f.name for f in R.RUBRIC}
    assert set(R.INSTRUCTION_FIELDS) <= names and set(R.ROW_KEYS) <= names
    for f in R.RUBRIC:
        assert f.question and f.tooltip and f.values and all(m for _, m in f.values)
    for f in ("position_state", "position_side", "position_last_stop"):
        assert "BEFORE this action" in R.BY_NAME[f].question + R.BY_NAME[f].tooltip


# ------------------------------------------------------------------ the form


def app(monkeypatch, base, login) -> AppTest:
    monkeypatch.setenv(A.DIR_ENV, str(base))
    monkeypatch.setenv(A.LOCAL_IDENTITY_ENV, login)
    with_headers(monkeypatch)
    at = AppTest.from_file(str(APP), default_timeout=60)
    at.run()
    assert not at.exception
    return at


def pick(at, item_id):
    return at.selectbox(key="aw_pick").set_value(item_id).run()


def widgets(at):
    return [*at.selectbox, *at.radio, *at.text_input, *at.multiselect, *at.checkbox]


def test_the_form_labels_tooltips_options_and_no_publisher_text_in_them(
        monkeypatch, local, base):
    at = pick(app(monkeypatch, base, REVIEWER.login), "s14a")
    texts = [str(t.value) for t in at.text]
    assert "Form 1 of 2 for this post" in texts
    at.radio(key="aw_s14a_ind_component").set_value("ACTION_RECORDED_HERE").run()
    at.selectbox(key="aw_s14a_ind_timing").set_value("CURRENT").run()
    at.selectbox(key="aw_s14a_ind_action").set_value("CLOSE").run()
    labelled = [w for w in widgets(at) if w.key and w.key.startswith("aw_s14a_ind_")
                and not w.key.startswith("aw_s14a_ind_lv")]
    assert labelled
    for w in labelled:
        field = w.key.removeprefix("aw_s14a_ind_").removesuffix("_state")
        if field in R.BY_NAME:
            assert f"[{field}]" in w.label and w.help, field
    post = bundle(base).posts["s14"]["text"]
    tokens = post.split()
    phrases = [" ".join(tokens[i:i + 3]) for i in range(len(tokens) - 2)]  # publisher text
    assert "Closed the crude" in phrases
    for w in widgets(at):
        shown = " ".join([str(w.label), str(w.help or ""), *map(str, getattr(w, "options",
                                                                             []) or [])])
        assert not any(ph in shown for ph in phrases), (w.key, shown)
    assert not any(ph in str(e.value) for e in (*at.markdown, *at.caption, *at.error,
                                                *at.warning) for ph in phrases)
    assert any(post in str(t.value) for t in at.text)  # shown, as plain text
    # The price table: "k of m" and the context of each occurrence as plain text.
    at = pick(at, "s18a")
    assert any("1 of 2" in t and "bounced from [70]" in t for t in map(str, (
        x.value for x in at.text)))


def test_the_form_hides_what_does_not_apply_and_lists_what_save_needs(monkeypatch, local, base):
    at = pick(app(monkeypatch, base, REVIEWER.login), "s25a")
    keys = {w.key for w in widgets(at)}
    assert "aw_s25a_ind_timing" in keys and "aw_s25a_ind_entry_price" not in keys
    assert any("REQUIRED_UNANSWERED" in str(e.value) for e in [*at.text, *at.error])
    assert at.button(key="aw_s25a_ind_save").disabled
    at.selectbox(key="aw_s25a_ind_timing").set_value("COMMENTARY").run()
    keys = {w.key for w in widgets(at)}
    assert "aw_s25a_ind_action" not in keys and "aw_s25a_ind_family" not in keys
    assert not [k for k in keys if k and k.startswith("aw_s25a_ind_lv")]
    assert any("[action]: NONE" in str(t.value) for t in at.text)
    at.radio(key="aw_s25a_ind_confidence").set_value("HIGH").run()
    at.button(key="aw_s25a_ind_save").click().run()
    [rec] = [r for r in records(base) if r["phase"] == A.INDEPENDENT]
    assert rec["reading"]["action"] == "NONE" and rec["reading"]["entry_price"] == "NOT_APPLICABLE"
    # After the reveal: the mapping questions appear only now; nothing pre-filled from the system.
    assert "aw_s25a_after_" + str(rec["seq"]) + "_timing" in {w.key for w in widgets(at)}


def test_the_guide_and_the_field_reference_pages_in_both_access_modes(
        monkeypatch, local, base):
    at = app(monkeypatch, base, REVIEWER.login)
    at.switch_page(str(APP.with_name("adjudication_guide.py"))).run()
    assert not at.exception
    assert any("Confidentiality" in str(h.value) for h in at.subheader)
    at.switch_page(str(APP.with_name("adjudication_fields.py"))).run()
    assert not at.exception
    assert any("timing" in str(h.value) for h in at.subheader)
    assert any("NONE (commentary)" in str(c.value) for c in at.code)
