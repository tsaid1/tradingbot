"""Rubric version 2.2: the resolutions of the independent review (contract section 14), one test
per behaviour, on INVENTED readings and the invented demo bundle only. Finding numbers (M1 to M4,
S1 to S10, N...) are those of the review."""

from __future__ import annotations

import json
import random

import _review_modes as modes
import pytest
from test_adjudication_rubric import (
    APPROVER,
    OTHER_REVIEWER,
    REVIEWER,
    after_reading,
    answers_of,
    app,
    bundle,
    check,
    pick,
    records,
    save_after,
    save_independent,
    widgets,
)

from tradingbot.runtime import adjudication as A
from tradingbot.runtime import adjudication_rubric as R
from tradingbot.runtime import adjudication_scenarios as SC

NS, CD, NA = R.NOT_STATED, R.CANNOT_DETERMINE, R.NOT_APPLICABLE
local = modes.local  # fixture


@pytest.fixture
def base(tmp_path):
    from test_adjudication_rubric import DEMO
    return DEMO.write_demo(tmp_path / "adjudication")


def row(role, exactness="EXACT"):
    return {"role": role, "exactness": exactness, "contract_month": "", "month_named": ""}


def reading(levels=(), **kw) -> dict:
    """An invented instruction reading with the defaults every form shares."""
    out = {"explicit": "YES", "quoted_series": NS, "traded_instrument": NS,
           "instrument_as_read": "crude", "family": "CRUDE", "idea": "SAME_AS_PREVIOUS", **kw}
    out["levels"] = [{"row": f"n:{k}", **x} for k, x in enumerate(levels)]
    return out


def validate(r: dict, *, flags=(), notes="", kind=R.INSTRUCTION, forms=1, k=1):
    ctx = R.FormContext(kind=kind, form_index=k, forms_in_post=forms,
                        rows=tuple(x["row"] for x in r.get("levels", [])))
    return R.validate(r, ctx, flags=flags, confidence="HIGH", notes=notes)


OPEN_LONG = {"position_state": "OPEN", "position_side": "LONG"}


# ------------------------------------------------------------------ 14.1 (M1, N8)


M1_INPUTS = {
    # "Out of the crude long at 74.80; our 72 stop was never touched."
    "close-with-the-stop-in-force": reading(
        [row("HISTORICAL_FILL"), row("STOP")], timing="CURRENT", action="CLOSE", side="LONG",
        **OPEN_LONG, position_last_stop="72", effect="CLOSES"),
    # "Stopped out of crude: the 69.50 stop was hit, filled 69.40."
    "stop-out-first-report": reading(
        [row("STOP"), row("HISTORICAL_FILL")], timing="HISTORICAL", announcement="FIRST_REPORT",
        action="CLOSE", side="LONG", **OPEN_LONG, position_last_stop="69.5", effect="CLOSES"),
    # "Out of the crude long at 74.80, just short of our 75 target."
    "close-short-of-the-target": reading(
        [row("HISTORICAL_FILL"), row("TARGET")], timing="CURRENT", action="CLOSE", side="LONG",
        **OPEN_LONG, position_last_stop=NS, effect="CLOSES"),
    # "Moving the gold stop from 2,440 to breakeven" (no entry in the posts shown).
    "modify-to-a-level-by-reference": reading(
        [row("STOP")], family="GOLD", instrument_as_read="gold", timing="CURRENT",
        action="MODIFY", side="SHORT", position_state="OPEN", position_side="SHORT",
        position_last_stop="2440", amendment_field="STOP", amendment_to=CD, targets=NS,
        ambiguities=["LEVEL_BY_REFERENCE"]),
    # "Took half off at 5,080; stop on the rest below today's low (was 5,000)."
    "reduce-with-a-stop-by-reference": reading(
        [row("HISTORICAL_FILL"), row("STOP")], family="SPX", instrument_as_read="the S&P",
        timing="CURRENT", action="REDUCE", side="LONG", **OPEN_LONG, position_last_stop="5000",
        effect="REDUCES", stop_price=CD, targets=NS, ambiguities=["LEVEL_BY_REFERENCE"]),
}


@pytest.mark.parametrize("name", [*M1_INPUTS, "the-first-escalated"])
def test_m1_the_six_inputs_of_the_review_are_saved(name):
    if name == "the-first-escalated":
        checks = validate(M1_INPUTS["close-with-the-stop-in-force"],
                          flags=[R.FORM_CANNOT_EXPRESS], notes="spread")
    else:
        checks = validate(M1_INPUTS[name])
    assert checks.blocking == [], checks.blocking


def test_the_fill_role_comes_before_stop_and_target_and_the_texts_say_why():
    order = R.TABLE_ROLES
    assert order.index("HISTORICAL_FILL") < order.index("STOP") < order.index("TARGET") < \
        order.index("INTENDED_ENTRY")
    assert [c for c, _ in R.BY_NAME["role"].values] == list(order)
    role = R.BY_NAME["role"].tooltip
    assert "stopped out at 2.84" in role and "69.50 STOP, 69.40 HISTORICAL_FILL" in role
    assert "the one replaced" in dict(R.BY_NAME["role"].values)["TARGET"]  # N8
    guide = " ".join(R.REVIEWER_GUIDE.split())
    assert "stopped out at 2.84" in guide and "69.50 STOP, 69.40 HISTORICAL_FILL" in guide


def test_table_stop_still_refuses_a_stop_nowhere_and_escalation_does_not_lift_it():
    r = reading([row("STOP")], timing="CURRENT", action="OPEN", side="LONG",
                position_state="FLAT", effect="OPENS", entry_price=NS, stop_price=NS,
                targets=NS)
    assert ("TABLE_STOP", "stop_price") in validate(r).blocking
    assert "TABLE_STOP" in validate(r, flags=[R.FORM_CANNOT_EXPRESS], notes="x").codes
    for f in ("stop_price", "position_last_stop", "amendment_from"):
        assert f in R.BLOCKING_CODES["TABLE_STOP"]


def test_table_target_on_an_exit_and_on_a_target_modify():
    reduce = reading([row("HISTORICAL_FILL"), row("TARGET")], timing="CURRENT", action="REDUCE",
                     side="LONG", **OPEN_LONG, position_last_stop=NS, effect="REDUCES",
                     stop_price=NS, targets=NS)
    assert validate(reduce).blocking == []
    modify = reading([row("TARGET"), row("TARGET")], timing="CURRENT", action="MODIFY",
                     side="LONG", **OPEN_LONG, position_last_stop=NS, stop_price=NS,
                     amendment_field="TARGET", amendment_from="75", amendment_to="78",
                     targets="78")
    assert validate(modify).blocking == []
    # The targets in force after a target MODIFY contain the new one (14.1).
    assert ("AMENDMENT", "targets") in validate({**modify, "targets": "75"}).blocking
    assert ("AMENDMENT", "targets") in validate({**modify, "targets": NS}).blocking
    opened = reading([row("TARGET")], timing="CURRENT", action="OPEN", side="LONG",
                     position_state="FLAT", effect="OPENS", entry_price=NS, stop_price=NS,
                     targets=NS)
    assert "TABLE_TARGET" in validate(opened).codes
    assert validate({**opened, "targets": CD, "ambiguities": ["LEVEL_BY_REFERENCE"]}).blocking \
        == []


def test_the_escalation_lifts_only_required_unanswered():
    r = reading(timing="CURRENT", action="OPEN", side="LONG", position_state="FLAT",
                effect="CLOSES")  # a contradiction between answered fields
    esc = validate(r, flags=[R.FORM_CANNOT_EXPRESS], notes="pairs trade")
    assert "REQUIRED_UNANSWERED" not in esc.codes and "ACTION_EFFECT" in esc.codes
    label_only = {"post_label": "COMMENTARY", "additional_actions": [
        {"timing": "CURRENT", "action": "OPEN", "family": "CRUDE", "side": "LONG"}]}
    ctx = R.FormContext(kind=R.LABEL_ONLY)
    assert "NOT_APPLICABLE_FIELD" in R.validate(label_only, ctx, flags=[R.FORM_CANNOT_EXPRESS],
                                                confidence="LOW", notes="x").codes
    actionable = {**label_only, "post_label": "CONDITIONAL"}
    assert "POST_LABEL_ACTIONS" in R.validate(actionable, ctx, flags=[R.FORM_CANNOT_EXPRESS],
                                              confidence="LOW", notes="x").codes


# ------------------------------------------------------------------ 14.2 (M2, M4, N4)


STOPPED_OUT = reading([row("HISTORICAL_FILL")], family="NATGAS", instrument_as_read="natgas",
                      timing="HISTORICAL", announcement="FIRST_REPORT", action="CLOSE",
                      side="LONG", **OPEN_LONG, position_last_stop=NS, effect="CLOSES")


def test_m2_effect_none_is_never_chosen_a_first_report_changes_the_position():
    assert validate(STOPPED_OUT).blocking == []
    assert ("ACTION_EFFECT", "effect") in validate({**STOPPED_OUT, "effect": "NONE"}).blocking
    unsure = {**STOPPED_OUT, "announcement": CD, "effect": "NONE",
              "ambiguities": ["TIMING_UNCLEAR"]}
    assert "ACTION_EFFECT" in validate(unsure).codes
    assert "NONE" not in R.offered("effect") and "NONE" in R.options("effect")
    text = R.BY_NAME["effect"].tooltip + dict(R.BY_NAME["effect"].values)["NONE"]
    assert "NONE with any action" not in text and "never chosen" in text
    # A repeat: the form sets NONE (and nothing else is accepted there).
    repeat = {**STOPPED_OUT, "announcement": "REPEAT", "position_state": "FLAT",
              "position_side": "", "position_last_stop": "", "effect": ""}
    done = R.complete(repeat, R.FormContext(rows=("n:0",)))
    assert done["effect"] == "NONE" and validate(repeat).blocking == []


def test_m2_an_approver_cannot_amend_a_first_report_to_effect_none(base):
    sc = next(s for s in SC.SCENARIOS if s["n"] == 10)
    b = bundle(base)
    [after] = save_after(base, b, sc, save_independent(base, b, sc, variants=False))
    with pytest.raises(A.AdjudicationError) as exc:
        A.append_annotation(base, b, APPROVER, item_id="s10a", phase=A.APPROVAL, blind=False,
                            reading={"effect": "NONE"}, refers_to=after["seq"], outcome="AMEND")
    assert "ACTION_EFFECT" in exc.value.codes


def test_a_historical_modify_amends_protection_unless_repeat():
    r = reading(timing="HISTORICAL", announcement="FIRST_REPORT", action="MODIFY", side="LONG",
                **OPEN_LONG, position_last_stop="68", amendment_field="STOP", amendment_to="70",
                targets=NS)
    ctx = R.FormContext()
    assert R.applicability(r, ctx)["effect"] == R.Status(R.DERIVED, "AMENDS_PROTECTION")
    assert R.applicability({**r, "announcement": "REPEAT"}, ctx)["effect"] == R.Status(
        R.DERIVED, "NONE")
    assert R.complete(r, ctx)["follower_implication"] == "RECONCILE_POSITION"


def test_m4_uncertain_asks_no_announcement_and_the_effect_as_for_any_action():
    r = reading(timing="UNCERTAIN", action="CLOSE", side="LONG", **OPEN_LONG,
                position_last_stop=NS, effect="CLOSES", ambiguities=["TIMING_UNCLEAR"])
    ctx = R.FormContext()
    st = R.applicability(r, ctx)
    assert st["announcement"] == R.Status(R.DERIVED, CD) and st["effect"].state == R.ASK
    done = R.complete(r, ctx)
    assert (done["announcement"], done["follower_implication"]) == (CD, CD)
    assert validate(r).blocking == []
    assert ("DERIVED_VALUE", "announcement") in validate(
        {**r, "announcement": "REPEAT"}).blocking  # the old dead end cannot be entered


def test_n4_uncertain_needs_an_action():
    r = {"timing": "UNCERTAIN", "action": "NONE", "ambiguities": ["TIMING_UNCLEAR"]}
    assert ("TIMING_ACTION", "action") in validate(r).blocking
    assert "TIMING_ACTION" not in validate({**r, "action": CD}, notes="unclear").codes


def test_m4_the_page_offers_no_announcement_under_uncertain_and_no_effect_none(
        monkeypatch, local, base):
    at = pick(app(monkeypatch, base, REVIEWER.login), "s11a")
    at.selectbox(key="aw_s11a_ind_timing").set_value("UNCERTAIN").run()
    at.selectbox(key="aw_s11a_ind_action").set_value("CLOSE").run()
    keys = {w.key for w in widgets(at)}
    assert "aw_s11a_ind_announcement" not in keys
    effect = at.selectbox(key="aw_s11a_ind_effect")
    assert not any(str(o).startswith("NONE") for o in effect.options)
    assert any("[announcement]: CANNOT_DETERMINE (set by the form)" in str(t.value)
               for t in at.text)
    assert not any("ACTION_EFFECT" in str(e.value) for e in at.error)


# ------------------------------------------------------------------ 14.3 (M3, S1)


def test_m3_an_escalated_reading_cannot_hold_a_pending_value(base):
    b = bundle(base)
    bad = {"timing": "HISTORICAL", "announcement": "", "action": "OPEN", "explicit": "YES",
           "family": "SPX", "follower_implication": "INSTRUCTION_CANDIDATE"}
    with pytest.raises(A.AdjudicationError) as exc:
        A.append_annotation(base, b, REVIEWER, item_id="s23a", phase=A.INDEPENDENT, blind=True,
                            reading=bad, flags=[R.FORM_CANNOT_EXPRESS], notes="spread",
                            confidence="LOW")
    assert "NOT_APPLICABLE_FIELD" in exc.value.codes
    assert "follower_implication" in exc.value.fields
    two = {"component": "", "timing": "HISTORICAL", "post_label": "ACTIONABLE",
           "announcement": "REPEAT", "follower_implication": "INSTRUCTION_CANDIDATE"}
    with pytest.raises(A.AdjudicationError, match="NOT_APPLICABLE_FIELD"):
        A.append_annotation(base, b, REVIEWER, item_id="s14a", phase=A.INDEPENDENT, blind=True,
                            reading=two, flags=[R.FORM_CANNOT_EXPRESS], notes="spread",
                            confidence="LOW")
    # Derived values are still checked when escalated.
    derived = {"timing": "CURRENT", "announcement": "REPEAT", "action": "OPEN",
               "explicit": "YES", "family": "SPX"}
    with pytest.raises(A.AdjudicationError, match="DERIVED_VALUE"):
        A.append_annotation(base, b, REVIEWER, item_id="s23a", phase=A.INDEPENDENT, blind=True,
                            reading=derived, flags=[R.FORM_CANNOT_EXPRESS], notes="spread",
                            confidence="LOW")


def test_m3_the_whole_post_label_ignores_escalated_forms_and_the_freeze_leaves_it_null(base):
    labels = [{"component": "ACTION_RECORDED_HERE", "post_label": "ACTIONABLE"},
              {"component": "ACTION_RECORDED_HERE", "post_label": "RETROSPECTIVE"}]
    assert R.overall_label(labels) == "ACTIONABLE"
    assert R.overall_label(labels, [True, False]) == "RETROSPECTIVE"
    sc = next(s for s in SC.SCENARIOS if s["n"] == 14)
    b = bundle(base)
    esc = {**sc, "forms": [{**sc["forms"][0], "flags": [R.FORM_CANNOT_EXPRESS],
                            "notes": "invented escalation"}, sc["forms"][1]]}
    save_after(base, b, esc, save_independent(base, b, esc, variants=False))
    path, manifest = A.freeze(base)
    rows = [json.loads(x) for x in path.read_text().splitlines()]
    assert [(r["item_id"], r["escalated"], r["post_label_overall"]) for r in rows] == [
        ("s14a", True, None), ("s14b", False, None)]
    assert manifest["escalated"] == 1


def test_s1_an_amendment_resets_a_derived_value_that_becomes_asked(base):
    sc = next(s for s in SC.SCENARIOS if s["n"] == 1)
    b = bundle(base)
    [after] = save_after(base, b, sc, save_independent(base, b, sc, variants=False))
    with pytest.raises(A.AdjudicationError) as exc:
        A.append_annotation(base, b, APPROVER, item_id="s01a", phase=A.APPROVAL, blind=False,
                            reading={"timing": "HISTORICAL"}, refers_to=after["seq"],
                            outcome="AMEND")
    assert "REQUIRED_UNANSWERED" in exc.value.codes and "announcement" in exc.value.fields
    item = b.items["s01a"]
    ctx = A.form_context(b, item, revealed=True,
                         rows=[r["row"] for r in after["reading"]["levels"]])
    merged = A.merge_amendment(after["reading"], {"timing": "HISTORICAL"}, ctx)
    assert merged["announcement"] == "" and merged["follower_implication"] == ""
    A.append_annotation(base, b, APPROVER, item_id="s01a", phase=A.APPROVAL, blind=False,
                        reading={"timing": "HISTORICAL", "announcement": "FIRST_REPORT"},
                        refers_to=after["seq"], outcome="AMEND",
                        acknowledged_warnings=R.validate(A.merge_amendment(
                            after["reading"], {"timing": "HISTORICAL",
                                               "announcement": "FIRST_REPORT"}, ctx), ctx,
                            confidence="HIGH").warnings)
    [frozen] = [r for r in A.resolve(records(base), b) if r["item_id"] == "s01a"]
    assert (frozen["announcement"], frozen["follower_implication"]) == (
        "FIRST_REPORT", "RECONCILE_POSITION")


def test_s1_the_approver_page_never_preselects_a_reset_value(monkeypatch, local, base):
    sc = next(s for s in SC.SCENARIOS if s["n"] == 1)
    b = bundle(base)
    [after] = save_after(base, b, sc, save_independent(base, b, sc, variants=False))
    at = pick(app(monkeypatch, base, APPROVER.login), "s01a")
    prefix = f"aw_s01a_appr_{after['seq']}"
    radio = at.radio(key=f"{prefix}_decision")
    assert radio.help == R.DECISION_HELP
    assert any("AMEND: some fields are wrong" in str(o) for o in radio.options)
    radio.set_value("AMEND").run()
    at.selectbox(key=f"{prefix}_timing").set_value("HISTORICAL").run()
    assert at.selectbox(key=f"{prefix}_announcement").value == ""
    assert at.button(key=f"{prefix}_save").disabled
    assert any("REQUIRED_UNANSWERED (announcement)" in str(e.value) for e in at.error)


def test_the_freeze_recomputes_derived_values():
    r = reading(timing="HISTORICAL", announcement="FIRST_REPORT", action="CLOSE", side="LONG",
                **OPEN_LONG, position_last_stop=NS, effect="CLOSES",
                follower_implication="INSTRUCTION_CANDIDATE", post_label="ACTIONABLE")
    done = R.rederive(r, R.FormContext())
    assert (done["follower_implication"], done["post_label"]) == ("RECONCILE_EXIT",
                                                                  "RETROSPECTIVE")
    pending = {"timing": "HISTORICAL", "announcement": "", "action": "OPEN",
               "follower_implication": "INSTRUCTION_CANDIDATE"}
    assert R.rederive(pending, R.FormContext())["follower_implication"] == ""


def _invariant(r: dict, where: str) -> None:
    fi = r.get("follower_implication")
    if fi == "INSTRUCTION_CANDIDATE":
        assert r.get("timing") == "CURRENT", (where, r)
    if fi in ("RECONCILE_EXIT", "RECONCILE_POSITION"):
        assert (r.get("timing"), r.get("announcement")) == ("HISTORICAL", "FIRST_REPORT"), (
            where, r)


MUTABLE = {
    "timing": ("", *R.TIMINGS), "announcement": ("", *R.ANNOUNCEMENTS),
    "action": ("", *R.ACTIONS), "follower_implication": ("", *R.FOLLOWER_IMPLICATIONS),
    "post_label": ("", *R.POST_LABELS), "effect": ("", *R.EFFECTS), "explicit": ("", "YES"),
    "component": ("", *R.COMPONENTS),
}


def test_property_instruction_candidate_implies_current_on_every_path(base):
    """14.3: over the page's checks, append_annotation, the approver's merge and the freeze,
    INSTRUCTION_CANDIDATE implies CURRENT and RECONCILE_* imply HISTORICAL and FIRST_REPORT."""
    rnd = random.Random(20261115)
    b = bundle(base)
    sources = [(sc, k) for sc in SC.SCENARIOS for k, f in enumerate(sc["forms"])
               if sc["n"] in (1, 9, 10, 11, 14, 29) and f["kind"] == "INSTRUCTION"]
    saved = []
    for n in range(120):
        sc, k = rnd.choice(sources)
        f, item = sc["forms"][k], b.items[SC.item_id(sc, k)]
        r = answers_of(b, item, f)
        for field in rnd.sample(sorted(MUTABLE), rnd.randint(1, 4)):
            r[field] = rnd.choice(MUTABLE[field])
        r = after_reading(b, item, r)
        esc = rnd.random() < 0.5
        flags = [R.FORM_CANNOT_EXPRESS, "NEEDS_MORE_EVIDENCE"] if esc else ["NEEDS_MORE_EVIDENCE"]
        who = A.Session(f"r{n}@example.invalid", "R", A.REVIEWER, A.FULL, False)
        try:
            done, checks, ctx = check(base, b, item, r, who, True, flags=flags,
                                      confidence="LOW", notes="invented")
        except A.AdjudicationError:
            continue
        if checks.blocking:
            continue
        _invariant(done, "page")
        verdicts = A.prefill_verdicts(done, A.system_reading(b, item), item["kind"],
                                      A.compared_fields(b, item, done, ctx.earlier_positions))
        if done.get("levels"):
            verdicts["levels"] = A.prefill_row_verdicts(done["levels"], A.system_rows(b, item))
        rec = A.append_annotation(base, b, who, item_id=item["item_id"], phase=A.AFTER_REVEAL,
                                  blind=False, reading=r, field_verdicts=verdicts, flags=flags,
                                  confidence="LOW", notes="invented",
                                  acknowledged_warnings=checks.warnings)
        _invariant(rec["reading"], "append_annotation")
        saved.append((rec, item))
    assert len(saved) >= 15 and any(R.FORM_CANNOT_EXPRESS in rec["flags"] for rec, _ in saved)
    amended = 0
    for rec, item in saved:
        change = {f: rnd.choice([v for v in MUTABLE[f] if v]) for f in rnd.sample(
            ("timing", "announcement", "action", "effect"), rnd.randint(1, 2))}
        ctx = A.form_context(b, item, revealed=True,
                             rows=[x["row"] for x in rec["reading"].get("levels", [])])
        merged = A.merge_amendment(rec["reading"], change, ctx)
        checks = R.validate(merged, ctx, flags=rec["flags"], confidence="LOW", notes="x")
        if checks.blocking:
            continue
        _invariant(merged, "approver merge")
        A.append_annotation(base, b, APPROVER, item_id=item["item_id"], phase=A.APPROVAL,
                            blind=False, reading=change, refers_to=rec["seq"], outcome="AMEND",
                            acknowledged_warnings=checks.warnings)
        amended += 1
    path, _manifest = A.freeze(base)
    frozen = [json.loads(x) for x in path.read_text().splitlines()]
    assert frozen and amended
    for r in frozen:
        _invariant({**r["reading"], "follower_implication": r["follower_implication"]}, "freeze")
        for entry in r["all_readings"]:
            _invariant(entry["reading"], "freeze, all_readings")


# ------------------------------------------------------------------ 14.4 (S2, S3)


def test_s2_a_repeat_that_would_still_change_the_position_warns():
    recap = reading([row("HISTORICAL_FILL")], timing="HISTORICAL", announcement="REPEAT",
                    action="CLOSE", side="LONG", **OPEN_LONG, position_last_stop="69.5")
    checks = validate(recap)
    assert checks.blocking == [] and "REPEAT_POSITION" in checks.warnings
    flat = {**recap, "position_state": "FLAT", "position_side": "", "position_last_stop": ""}
    assert "REPEAT_POSITION" not in validate(flat).warnings
    reopen = {**flat, "action": "OPEN", "entry_price": NS, "stop_price": NS, "targets": NS}
    assert "REPEAT_POSITION" in validate(reopen).warnings
    for f in R.POSITION_FIELDS:
        assert "REPEAT" in R.BY_NAME[f].tooltip


@pytest.mark.parametrize("state,effect,action,blocked", [
    ("FLAT", "INCREASES", "OPEN", True), ("FLAT", "REDUCES", "REDUCE", True),
    ("FLAT", "CLOSES", "CLOSE", True), ("FLAT", "OPENS", "ADD", False),
    ("OPEN", "OPENS", "OPEN", True), ("OPEN", "OPENS", "ADD", True),
    ("OPEN", "INCREASES", "OPEN", False), (CD, "INCREASES", "ADD", False),
])
def test_s3_effect_position(state, effect, action, blocked):
    r = reading(timing="CURRENT", action=action, side="LONG", position_state=state,
                effect=effect, stop_price=NS, targets=NS)
    if state != "FLAT":
        r.update(position_side="LONG", position_last_stop=NS)
    if action in ("OPEN", "ADD"):
        r["entry_price"] = NS
    if state == CD:
        r["ambiguities"] = ["POSITION_UNCLEAR"]
    assert ("EFFECT_POSITION" in validate(r).codes) is blocked


def test_s3_a_modify_on_flat_is_refused_and_recorded_with_an_unknown_position():
    r = reading(timing="CURRENT", action="MODIFY", side="LONG", position_state="FLAT",
                amendment_field="STOP", amendment_to="70", targets=NS)
    assert "EFFECT_POSITION" in validate(r).codes
    unknown = {**r, "position_state": CD, "position_side": "LONG", "position_last_stop": CD}
    assert "EFFECT_POSITION" not in validate(unknown, flags=["MISSING_CONTEXT"]).codes


def test_s3_position_after_never_carries_not_applicable_as_an_open_stop():
    pa = R.position_after
    flat = {"action": "ADD", "side": "LONG", "position_state": "FLAT", "timing": "CURRENT",
            "effect": "INCREASES", "stop_price": NS}
    assert pa(flat) == {"state": "OPEN", "side": "LONG", "stop": NS}
    close = {**flat, "action": "CLOSE", "effect": "REDUCES", "stop_price": NA}
    assert pa(close)["stop"] == NS


# ------------------------------------------------------------------ 14.5 (S4, S5, N1)


def test_s4_an_entry_by_reference_keeps_its_role():
    bids = reading([row("CHART_LEVEL")], timing="CURRENT", action="OPEN", side="LONG",
                   position_state="FLAT", effect="OPENS", entry_price=CD,
                   entry_role="INTENDED_ENTRY", stop_price=NS, targets=NS,
                   ambiguities=["LEVEL_BY_REFERENCE"])
    ctx = R.FormContext(rows=("n:0",))
    st = R.applicability(bids, ctx)
    assert st["entry_role"].state == R.ASK and st["entry_exactness"].state == R.NA
    assert validate(bids).blocking == []
    assert R.complete(bids, ctx)["entry_exactness"] == NA
    assert ("REQUIRED_UNANSWERED", "entry_role") in validate({**bids, "entry_role": ""}).blocking
    assert ("ENTRY_FIELDS", "entry_role") in validate({**bids, "entry_role": NS}).blocking
    stated = R.applicability({**bids, "entry_price": NS}, ctx)
    assert stated["entry_role"] == R.Status(R.DERIVED, NS)


def test_s5_a_label_only_post_with_an_uncertain_action():
    ctx = R.FormContext(kind=R.LABEL_ONLY)
    extra = [{"timing": "UNCERTAIN", "action": "CLOSE", "family": "CRUDE", "side": "LONG"}]
    r = {"post_label": "UNRESOLVED", "ambiguities": ["TIMING_UNCLEAR"], "additional_actions": extra}
    assert R.validate(r, ctx, confidence="LOW").blocking == []
    assert R.validate({**r, "additional_actions": []}, ctx, confidence="LOW").blocking == []
    assert "POST_LABEL_ACTIONS" in R.validate({**r, "post_label": "ACTIONABLE"}, ctx,
                                              confidence="LOW").codes
    tip = R.BY_NAME["post_label"].tooltip
    assert "ACTIONABLE, CONDITIONAL, RETROSPECTIVE, UNRESOLVED, COMMENTARY" in tip
    assert R.LABEL_STRENGTH == ("ACTIONABLE", "CONDITIONAL", "RETROSPECTIVE", "UNRESOLVED",
                                "COMMENTARY")


# ------------------------------------------------------------------ 14.6 (S7)


def test_s7_current_or_historical_is_decided_by_the_posts_time_words():
    tip = R.BY_NAME["timing"].tooltip
    guide = " ".join(R.REVIEWER_GUIDE.split())
    for text in (tip, guide):
        assert "Bought gold at 2,395." in text and "We bought gold this morning at 2,395." in text
        assert "this morning" in text and "overnight" in text
    assert "a few minutes ago" in tip and "HISTORICAL (a first report" in tip
    morning = reading(timing="HISTORICAL", announcement="FIRST_REPORT", action="OPEN",
                      side="LONG", position_state="FLAT", effect="OPENS", entry_price="2395",
                      entry_role="HISTORICAL_FILL", entry_exactness="EXACT", stop_price=NS,
                      targets=NS)
    assert R.complete(morning, R.FormContext())["follower_implication"] == "RECONCILE_POSITION"


# ------------------------------------------------------------------ 14.7 (S6, S8, N3, N7)


def test_s8_the_guide_is_one_page_and_defines_its_terms():
    guide = R.REVIEWER_GUIDE
    assert len(guide.split()) <= R.REVIEWER_GUIDE_MAX_WORDS == 700
    flat = " ".join(guide.split())
    for words in ("Field reference", "not an order", "ONE action", "label-only form labels",
                  "further actions", "BEFORE THIS ACTION", "post's own time words",
                  "Only INTENDED_ENTRY", "earlier posts shown", "CANNOT_DETERMINE",
                  "FORM_CANNOT_EXPRESS", "independent reading", "CONFIRM (it is right)",
                  "CORRECT (it is wrong, yours is right)", "Next item not yet adjudicated",
                  "Confidentiality", "mapping (the system's proposal",
                  "NOT_READ (the system typed no level there)", "restricted access"):
        assert words in flat, words
    for undefined in ("sequence", "bundle", "reference series", "disappears"):
        assert undefined not in flat.casefold(), undefined
    assert any("greyed" in line for line in R.HOW_TO_REVIEW)
    assert not any("disappears" in line for line in R.HOW_TO_REVIEW)
    texts = " ".join(f.tooltip + " ".join(m for _, m in f.values) for f in R.RUBRIC)
    assert "bundle" not in texts and "this sequence" not in texts
    rubric_doc = R.render_rubric_markdown()
    assert "`NOT_READ`" in rubric_doc and "the system's value is wrong, mine is right" in \
        rubric_doc


def test_s6_n7_verdicts_carry_their_meaning_and_further_actions_have_tooltips(
        monkeypatch, local, base):
    assert R.VERDICT_TEXT["CORRECT"] == "CORRECT: the system's value is wrong, mine is right"
    at = pick(app(monkeypatch, base, REVIEWER.login), "s01a")
    at.toggle(key="aw_blind").set_value(False).run()
    radios = [r for r in at.radio if r.key and "_verdict_" in r.key]
    assert radios
    for r in radios:
        assert r.help == R.VERDICT_HELP
        assert "CONFIRM: the system's value is right" in [str(o) for o in r.options]
    at.toggle(key="aw_blind").set_value(True).run()
    at = pick(at, "s22b")
    at.radio(key="aw_s22b_ind_component").set_value("ACTION_RECORDED_HERE").run()
    at.selectbox(key="aw_s22b_ind_aa_n").set_value(1).run()
    for k in R.ADDITIONAL_KEYS:
        assert at.selectbox(key=f"aw_s22b_ind_aa_0_{k}").help == R.ADDITIONAL_HELP[k]


def test_n3_the_follower_line_says_an_inferred_action_is_not_executable(
        monkeypatch, local, base):
    at = pick(app(monkeypatch, base, REVIEWER.login), "s20a")
    for f, v in (("timing", "HISTORICAL"), ("announcement", "FIRST_REPORT"),
                 ("action", "CLOSE"), ("explicit", "NO")):
        at.selectbox(key=f"aw_s20a_ind_{f}").set_value(v).run()
    captions = [str(c.value) for c in at.caption]
    assert any(R.FOLLOWER_SENTENCE in c and R.INFERRED_NOTE in c for c in captions)
    at.selectbox(key="aw_s20a_ind_explicit").set_value("YES").run()
    assert not any(R.INFERRED_NOTE in str(c.value) for c in at.caption)


# ------------------------------------------------------------------ 14.8 (S9, N5)


def test_s9_rubric_1_rows_go_to_a_sibling_file_with_the_version_1_schema(base):
    from tradingbot.runtime.vocab_recommend import append_chained
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
    sc = next(s for s in SC.SCENARIOS if s["n"] == 1)
    save_after(base, b, sc, save_independent(base, b, sc, variants=False))
    path, manifest = A.freeze(base)
    main = [json.loads(x) for x in path.read_text().splitlines()]
    assert [(r["item_id"], r["schema"], r["rubric"]) for r in main] == [
        ("s01a", A.GROUND_TRUTH_SCHEMA, 2)]
    sibling = path.with_name("ground_truth_v1.rubric1.jsonl")
    assert manifest["rubric1"]["file"] == sibling.name
    old_rows = [json.loads(x) for x in sibling.read_text().splitlines()]
    assert [(r["item_id"], r["schema"], r["rubric"]) for r in old_rows] == [
        ("s02a", "tradingbot.adjudication.ground_truth/1", 1)]
    assert manifest["by_rubric"] == {"1": 1, "2": 1} and manifest["rubric1"]["items"] == 1
    assert A.verify_versions(base) == [("ground_truth_v1.manifest.json", True)]
    sibling.write_text(sibling.read_text().replace("LOW", "HIGH"))
    assert A.verify_versions(base) == [("ground_truth_v1.manifest.json", False)]
    assert A.freeze(base)[0].name == "ground_truth_v2.jsonl"


def test_n5_every_reviewers_reading_reaches_the_frozen_version(base):
    sc = next(s for s in SC.SCENARIOS if s["n"] == 1)
    b = bundle(base)
    first = save_after(base, b, sc, save_independent(base, b, sc, variants=False))[0]
    second = save_after(base, b, sc, save_independent(base, b, sc, OTHER_REVIEWER, False),
                        OTHER_REVIEWER)[0]
    A.append_annotation(base, b, APPROVER, item_id="s01a", phase=A.APPROVAL, blind=False,
                        reading={}, refers_to=first["seq"], outcome="ACCEPT")
    [row_] = [r for r in A.resolve(records(base), b) if r["item_id"] == "s01a"]
    assert row_["reviewer"] == REVIEWER.login and row_["status"] == "APPROVED"
    entries = row_["all_readings"]
    assert [e["reviewer"] for e in entries] == [REVIEWER.login, OTHER_REVIEWER.login]
    assert [e["source_seqs"]["after_reveal"] for e in entries] == [first["seq"], second["seq"]]
    assert entries[1]["reading"] == second["reading"] and entries[1]["blind"] is True
    assert entries[0]["independent_reading"] == row_["independent_reading"]
    assert set(entries[0]) == {"reviewer", "reading", "independent_reading", "blind",
                               "source_seqs"}


# ------------------------------------------------------------------ 14.9 (N6)


def test_n6_not_in_table_warnings_are_not_raised_for_a_level_by_reference_or_a_chart():
    r = reading(timing="CURRENT", action="OPEN", side="LONG", position_state="FLAT",
                effect="OPENS", entry_price="70", entry_role="INTENDED_ENTRY",
                entry_exactness="EXACT", stop_price="68", targets="75")
    codes = {"ENTRY_NOT_IN_TABLE", "STOP_NOT_IN_TABLE", "TARGET_NOT_IN_TABLE"}
    assert codes <= set(validate(r).warnings)
    assert not codes & set(validate({**r, "ambiguities": ["LEVEL_BY_REFERENCE"]}).warnings)
    assert not codes & set(validate(r, flags=["CHART_NEEDED"]).warnings)


# ------------------------------------------------------------------ 14.10: blind-first, page


SERVES = {A: ("system_reading", "system_rows", "prefill_verdicts", "prefill_row_verdicts",
              "compared_fields", "position_compared"),
          R: ("prefill_verdict", "prefill_row_verdict", "prefill_verdicts")}


def test_the_page_holds_blind_first_per_post_and_marks_before_serving(
        monkeypatch, local, base):
    """Spies on every function that serves the system's reading: none runs before every form
    of the post is saved; each call finds the SYSTEM_SHOWN marker of both forms in the log."""
    calls: list[tuple[str, set]] = []

    def marked() -> set:
        v = A.read_annotations(base)
        return {r["item_id"] for r in v.records if r["phase"] == A.SYSTEM_SHOWN}

    for mod, names in SERVES.items():
        for name in names:
            real = getattr(mod, name)

            def spy(*a, _real=real, _name=name, **k):
                calls.append((_name, marked()))
                return _real(*a, **k)

            monkeypatch.setattr(mod, name, spy)
    sc = next(s for s in SC.SCENARIOS if s["n"] == 22)
    b = bundle(base)
    at = app(monkeypatch, base, REVIEWER.login)
    calls.clear()  # the first item of the list (another post) is blind too
    save_independent(base, b, {**sc, "forms": sc["forms"][:1]}, variants=False)
    calls.clear()
    at = pick(at, "s22a")
    assert calls == [] and marked() == set()
    assert any("Your independent reading of this form is saved" in str(c.value)
               for c in at.caption)
    at = pick(at, "s22b")
    at.radio(key="aw_s22b_ind_component").set_value("NO_SEPARATE_ACTION").run()
    at.radio(key="aw_s22b_ind_confidence").set_value("HIGH").run()
    assert calls == [] and marked() == set()
    at.button(key="aw_s22b_ind_save").click().run()
    assert not at.exception
    assert calls, "the system's reading is served once every form is saved"
    assert all({"s22a", "s22b"} <= seen for _name, seen in calls), calls
    assert {"system_reading", "system_rows"} <= {name for name, _ in calls}
