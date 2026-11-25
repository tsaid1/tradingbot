"""The hosted reviewer site (``hosted/reviewer_web``): the reading form, blind-first with the
per-post reveal gate, saving, resuming, the two questions after the reveal and a returned
reading. Every test drives the pages as a browser does (``hosted_web_site.Browser``), on a real
Postgres with the invented demo bundle; the two forms of the demo's post s14 carry system values
that nothing else contains (``SYSTEM_MARKS``)."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest
from hosted_pg import hosted_reveal
from hosted_web_site import (  # noqa: F401 - fixtures
    FIRST,
    ORIGIN,
    SECOND,
    SYSTEM_MARKS,
    SYSTEM_WORDS,
    Shown,
    Site,
    dsn,
    scenario_reading,
    site,
    store,
    wanted,
)

from tradingbot.runtime import adjudication as A
from tradingbot.runtime import adjudication_rubric as R

NA = R.NOT_APPLICABLE
FRAGMENT = {"X-Fragment": "form"}


def phases(site, alias=FIRST, item=None) -> list[str]:  # noqa: F811
    return [r["phase"] for r in site.records(alias) if item is None or r["item_id"] == item]


def first_idea(base: Path, sequence_id: str) -> None:
    """Put one idea of the demo first in review order."""
    path = base / "bundle" / "sequences.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    old = next(r["review_order"] for r in rows if r["sequence_id"] == sequence_id)
    for r in rows:
        r["review_order"] = 1 if r["sequence_id"] == sequence_id else (
            r["review_order"] + 1 if r["review_order"] < old else r["review_order"])
    path.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows))


@pytest.fixture
def site14(tmp_path, store, dsn) -> Site:  # noqa: F811
    """The site with the two-form post s14 first in review order."""
    return Site(tmp_path, store, dsn, edit=lambda base: first_idea(base, "scs14"))


# ------------------------------------------------------------------ the form, as today


def test_the_form_stores_what_the_streamlit_form_stores(site):  # noqa: F811
    """The same invented item and answers as the Streamlit test of blind-first: the same record."""
    from test_adjudication import valid

    b = site.browser().sign_in(FIRST)
    shown = b.open("i07")
    assert "Hidden until you save an independent reading" in shown.text
    assert "holds SHIPPED" not in shown.text and "system-value" not in shown.text
    assert "Short Zed Metals around 412" in shown.text and "Form 1 of 1 for this post" in shown.text
    reading = valid(site.bundle, "i07", entry_exactness="EXACT",
                    levels=[("CURRENT_MARKET", "EXACT"), ("STOP", "EXACT")])
    b.fill(reading)
    saved = b.save(confidence="MEDIUM")
    [ind] = site.records(FIRST, markers=False)
    assert (ind["phase"], ind["blind"], ind["reading"]["action"]) == (A.INDEPENDENT, True, "OPEN")
    assert ind["rubric"] == 2 and ind["confidence"] == "MEDIUM" and ind["refers_to"] is None
    assert ind["reading"]["follower_implication"] == "INSTRUCTION_CANDIDATE"
    assert ind["reading"]["levels"][0] == {"row": "n:0", "role": "CURRENT_MARKET",
                                           "exactness": "EXACT", "contract_month": NA,
                                           "month_named": ""}
    assert ind["annotator"] == {"login": "reviewer@example.invalid", "name": "Reviewer Example",
                                "role": "REVIEWER", "access": "FULL"}
    # The stored reading is the one the core completes from those answers.
    item = site.bundle.items["i07"]
    ctx = A.form_context(site.bundle, item, revealed=False)
    assert ind["reading"] == R.complete(A.clean_reading(reading, item, site.bundle), ctx)
    # The reveal follows the save: the marker, then the system's reading beside the form.
    assert phases(site) == [A.INDEPENDENT, A.SYSTEM_SHOWN]
    assert "holds SHIPPED: HOLD" in saved.text and "Saved." in saved.text
    assert saved.value("side") == "SHORT" and saved.value("v.side") == "CONFIRM"
    assert saved.value("confidence") == "" and saved.value("notes") == ""  # given anew
    assert f"SAVED. Your independent reading of this form is record {ind['seq']}" in saved.text
    b.save()
    rec = site.records(FIRST)[-1]
    assert (rec["phase"], rec["blind"], rec["refers_to"]) == (A.AFTER_REVEAL, True, ind["seq"])
    assert rec["field_verdicts"]["action"] == "CONFIRM"
    assert rec["field_verdicts"]["entry_exactness"] == "CORRECT"  # EXACT against APPROXIMATE
    assert rec["field_verdicts"]["amendment_field"] == NA
    assert rec["field_verdicts"]["levels"] == {"n:0": "CORRECT", "n:1": "CONFIRM"}
    assert rec["reading"]["reference_series_right"] == NA  # this item has no proposal
    assert set(rec["field_verdicts"]) == {
        *A.compared_fields(site.bundle, item, rec["reading"]), "levels"}
    assert "SAVED. Your reading of this form after the reveal is record" in b.shown.text
    assert "The next save needs CONFIDENCE (confidence): confidence is required" in b.shown.text
    assert "Save refuses" not in b.shown.text


def test_fields_appear_and_go_with_the_answers_by_a_round_trip_to_the_server(site):  # noqa: F811
    b = site.browser().sign_in(FIRST)
    shown = b.open("i07")
    assert {"timing", "action", "explicit"} <= shown.names
    assert not {"family", "s.entry_price", "effect", "lv.n:0.role"} & shown.names
    assert "Not yet: choose the action first." in shown.text
    shown = b.set({"timing": "CURRENT"})
    assert "ACTIONABLE" in shown.text and "(set by the form)" in shown.text  # the derived label
    shown = b.set({"action": "OPEN"})
    assert {"family", "s.entry_price", "effect", "lv.n:0.role", "s.stop_price"} <= shown.names
    assert "entry_price" not in shown.names  # the value box comes with "value given"
    shown = b.set({"s.entry_price": "VALUE"})
    assert shown.value("entry_price") == ""
    shown = b.set({"entry_price": "412", "lv.n:0.role": "CURRENT_MARKET"})
    assert {"entry_role", "entry_exactness", "lv.n:0.exactness"} <= shown.names
    assert "lv.n:0.contract_month" not in shown.names  # waits for the quoted series
    shown = b.set({"quoted_series": "FUTURES"})
    assert "lv.n:0.contract_month" in shown.names
    shown = b.set({"lv.n:0.contract_month": "NAMED"})
    assert "lv.n:0.month_named" in shown.names
    # An answer that makes fields not apply takes their controls, and their values, away.
    shown = b.set({"action": "CLOSE"})
    assert not {"s.entry_price", "entry_price", "entry_role", "s.stop_price"} & shown.names
    assert "Does not apply: the entry applies to OPEN and ADD." in shown.text
    assert "does not apply (the stop and targets do not apply to CLOSE)." not in shown.text
    shown = b.set({"action": "OPEN"})
    assert shown.value("s.entry_price") == ""  # asked again: unanswered, nothing remembered
    shown = b.set({"timing": "COMMENTARY"})
    assert "action" not in shown.names and "Does not apply: the post states no trading action." \
        in shown.text
    assert not site.records(FIRST)  # a round trip saves nothing
    # A script asks for the form's body only.
    data = b.shown.data()
    part = b.post("/form/i07/state", data, headers=FRAGMENT)
    text = part.get_data(as_text=True)
    assert part.status_code == 200 and part.headers["X-Fragment"] == "form"
    assert "<html" not in text and 'name="_key"' in text and "Source evidence" not in text
    # No rule lives in the script: it names no field, option or code of the rubric.
    script = (Path(site.app.static_folder) / "app.js").read_text()
    words = {f.name for f in R.RUBRIC} | {c for f in R.RUBRIC for c, _ in f.values} \
        | set(R.BLOCKING_CODES) | set(R.WARNING_CODES) | set(A.VERDICTS)
    assert not [w for w in words if len(w) > 3 and w in script]


def test_save_lists_what_it_refuses_by_field_and_warnings_need_the_tick(site):  # noqa: F811
    from test_adjudication import valid

    b = site.browser().sign_in(FIRST)
    b.open("i07")
    b.fill(valid(site.bundle, "i07", stop_price=R.NOT_STATED,
                 levels=[("CURRENT_MARKET", "APPROXIMATE"), ("STOP", "EXACT")]))
    shown = b.shown
    assert not shown.can_save()
    for code, where in (("TABLE_STOP", "stop_price"), ("CONFIDENCE", "confidence")):
        assert f"Save refuses {code} ({where}): {R.BLOCKING_CODES[code]}" in shown.visible()
    refused = b.press_save()  # pressed all the same (a page without scripts, a stale page)
    assert refused.status == 422 and "Not saved (BLOCKED:TABLE_STOP,CONFIDENCE)" in refused.text
    assert refused.value("s.stop_price") == R.NOT_STATED  # the answers are still in the page
    assert not site.records(FIRST)
    shown = b.set({"s.stop_price": "VALUE", "confidence": "HIGH"})
    shown = b.set({"stop_price": "four twenty"})
    assert "Not a valid value (BAD_PRICE:stop_price)." in shown.text and not shown.can_save()
    shown = b.set({"stop_price": "420", "family": ""})
    assert "Required, unanswered: family" in shown.text
    assert f"Save refuses REQUIRED_UNANSWERED (family): {R.BLOCKING_CODES['REQUIRED_UNANSWERED']}" \
        in shown.visible()
    shown = b.set({"family": "OTHER"})
    assert shown.can_save() and "Save refuses" not in shown.text

    # A warning: the reading is saved only with the tick, and the tick is stored.
    b.open("i03")
    b.fill(valid(site.bundle, "i03", idea="NEW_IDEA"))
    shown = b.set({"confidence": "HIGH"})
    warning = f"Please check: NEW_IDEA_NOT_OPEN: {R.WARNING_CODES['NEW_IDEA_NOT_OPEN']}"
    assert warning in shown.visible() and "I have checked these" in shown.text
    assert not shown.can_save() and shown.value("ack") == []
    refused = b.press_save()
    assert refused.status == 422 and "Not saved (WARNINGS_NOT_ACKNOWLEDGED)" in refused.text
    assert not site.records(FIRST)
    shown = b.set({"ack": "NEW_IDEA_NOT_OPEN"})
    assert shown.can_save() and shown.value("ack") == ["NEW_IDEA_NOT_OPEN"]
    # The tick stands for the warnings it was given for: another list is checked again.
    shown = b.set({"s.stop_price": "VALUE"})
    shown = b.set({"stop_price": "99"})
    assert "STOP_SIDE" in shown.text and shown.value("ack") == [] and not shown.can_save()
    shown = b.set({"s.stop_price": R.NOT_STATED})
    assert shown.value("ack") == [] and "STOP_SIDE" not in shown.text
    b.set({"ack": "NEW_IDEA_NOT_OPEN"})
    assert "Saved." in b.press_save().text
    [rec] = site.records(FIRST, markers=False)
    assert rec["acknowledged_warnings"] == ["NEW_IDEA_NOT_OPEN"] and rec["item_id"] == "i03"


def test_a_label_only_form_asks_for_the_label_and_the_actions(site):  # noqa: F811
    b = site.browser().sign_in(FIRST)
    shown = b.open("i02")
    assert "Label-only form: the label of the whole post" in shown.text
    assert {"post_label", "ambiguities", "flags", "confidence", "notes"} <= shown.names
    assert not {"timing", "action", "aa.n", "lv.n:0.role"} & shown.names
    shown = b.set({"post_label": "ACTIONABLE", "confidence": "LOW"})
    assert "aa.n" in shown.names and "ADDITIONAL_ACTION_REQUIRED" in shown.text
    shown = b.set({"aa.n": "1"})
    b.set({"aa.0.timing": "CURRENT", "aa.0.action": "REDUCE", "aa.0.family": "TLT",
           "aa.0.side": "LONG"})
    assert b.shown.can_save()
    b.press_save()
    after = b.save(confidence="LOW")  # after the reveal: one verdict, on the label
    [ind, rec] = site.records(FIRST, markers=False)
    assert ind["reading"] == rec["reading"] == {
        "post_label": "ACTIONABLE", "ambiguities": [], "additional_actions": [
            {"timing": "CURRENT", "action": "REDUCE", "family": "TLT", "side": "LONG"}]}
    assert rec["field_verdicts"] == {"post_label": "CORRECT"}  # the system read COMMENTARY
    assert "Saved." in after.text


# ------------------------------------------------------------------ blind-first, per post


def test_nothing_of_the_system_leaves_the_server_before_every_form_of_the_post_is_saved(
        site, monkeypatch):  # noqa: F811
    from reviewer_web import forms as F

    b = site.browser().sign_in(FIRST)
    seen = [b.open("s14a").text, b.open("s14b").text]
    b.review("s14a", after=False)  # form 1 of 2 saved; form 2 not
    seen += [b.shown.text, b.open("s14a").text, b.open("s14b").text,
             b.get("/").get_data(as_text=True)]
    assert "Still without an independent reading" in seen[3] and "/form/s14b" in seen[3]
    b.open("s14b")
    seen.append(b.set({"component": "ACTION_RECORDED_HERE", "timing": "CURRENT"}).text)
    seen.append(b.post("/form/s14b/state", b.shown.data(), headers=FRAGMENT).get_data(
        as_text=True))
    # A request for the form after the reveal, sent anyway, for either form: refused, empty.
    for item in ("s14a", "s14b"):
        b.open("s14b")
        hostile = [(n, A.AFTER_REVEAL if n == "_phase" else site.page_key(FIRST, item)
                    if n == "_key" else v) for n, v in b.shown.data()]
        hostile += [("reference_series_right", "YES"), ("confidence", "HIGH")]
        answer = b.post(f"/form/{item}/save", hostile)
        assert answer.status_code == 422
        seen.append(answer.get_data(as_text=True))
        assert "Not saved (NOT_REVEALED)" in seen[-1]
        seen.append(b.post(f"/form/{item}/state", hostile, headers=FRAGMENT).get_data(
            as_text=True))
    assert phases(site) == [A.INDEPENDENT]
    for text in seen:
        for word in (*SYSTEM_WORDS, "TLT ETF", "NOT_DECIDED"):
            assert word not in text, word
    for response in (b.get("/form/s14a"), b.get("/form/s14b")):
        assert "ZQSYS" not in repr(sorted(response.headers.items()))

    # Saving form 2 opens the gate. The markers of BOTH forms are in the database, committed,
    # before the system's reading is read for a page.
    read_at = []

    def watched(real):
        def reading(bundle, item, *args, **kw):
            read_at.append([(r["item_id"], r["phase"]) for r in site.records(FIRST)
                            if r["phase"] == A.SYSTEM_SHOWN])
            return real(bundle, item, *args, **kw)
        return reading

    monkeypatch.setattr(A, "system_reading", watched(A.system_reading))
    monkeypatch.setattr(F, "system_block", watched(F.system_block))
    monkeypatch.setattr(A, "system_rows", watched(A.system_rows))
    b.review("s14b", after=False)
    assert read_at and all(marks == [("s14a", A.SYSTEM_SHOWN), ("s14b", A.SYSTEM_SHOWN)]
                           for marks in read_at)
    assert phases(site) == [A.INDEPENDENT, A.INDEPENDENT, A.SYSTEM_SHOWN, A.SYSTEM_SHOWN]
    for item, other in (("s14b", "s14a"), ("s14a", "s14b")):
        text = b.shown.text if item == "s14b" else b.open(item).text
        for value in SYSTEM_MARKS[item].values():
            assert value in text, value
        assert f"holds SHIPPED: HOLD {SYSTEM_MARKS[item]['hold']}" in text
        for value in SYSTEM_MARKS[other].values():
            assert value not in text  # one form's page carries that form's system values only
    # The other reviewer has saved nothing: the gate is theirs alone.
    other = site.browser().sign_in(SECOND)
    assert "ZQSYS" not in other.open("s14a").text + other.open("s14b").text
    assert not site.records(SECOND)


def test_nothing_of_the_system_is_shown_when_the_marker_cannot_be_stored(
        site, monkeypatch):  # noqa: F811
    b = site.browser().sign_in(FIRST)
    b.review("s14a", after=False)
    real = A.build_system_shown

    def refused(*_a, **_k):
        raise A.AdjudicationError("LOG_FAILS_VERIFICATION")

    monkeypatch.setattr(A, "build_system_shown", refused)
    b.review("s14b", after=False)
    for text in (b.shown.text, b.open("s14a").text, b.open("s14b").text):
        assert "The system's reading cannot be shown (LOG_FAILS_VERIFICATION)" in text
        assert not [w for w in SYSTEM_WORDS if w in text]
        assert "Your independent reading of this form is saved." in text
    assert not b.shown.has_form and phases(site) == [A.INDEPENDENT, A.INDEPENDENT]
    # The store failing (not the core) shows nothing either.
    append = site.store.append

    def broken(reviewer, make, **kw):
        if kw.get("idempotency_key") is None:
            raise OSError("the database went away")
        return append(reviewer, make, **kw)

    monkeypatch.setattr(A, "build_system_shown", real)
    monkeypatch.setattr(site.store, "append", broken)
    text = b.open("s14b").text
    assert "The system's reading cannot be shown (OSError)" in text and "went away" not in text
    assert not [w for w in SYSTEM_WORDS if w in text]
    monkeypatch.setattr(site.store, "append", append)
    assert SYSTEM_MARKS["s14b"]["reference"] in b.open("s14b").text  # stored now, so shown


def test_an_independent_reading_cannot_be_saved_once_the_system_was_shown(site):  # noqa: F811
    b = site.browser().sign_in(FIRST)
    reading, _form = scenario_reading(site.bundle, "i07")
    b.open("i07")
    b.fill(reading)
    b.set({"confidence": "HIGH"})
    independent = b.shown.data()  # the page as it stood before the save
    assert "Saved." in b.press_save().text
    before = site.records(FIRST)
    assert [r["phase"] for r in before] == [A.INDEPENDENT, A.SYSTEM_SHOWN]
    # The same answers again from the old page, with a new key: the core refuses, calmly.
    again = [(n, site.page_key(FIRST, "i07") if n == "_key" else v) for n, v in independent]
    answer = b.post("/form/i07/save", again)
    text = answer.get_data(as_text=True)
    assert answer.status_code == 422 and "Not saved (SYSTEM_READING_ALREADY_SEEN)" in text
    assert "An independent reading can no longer be saved for this form" in text
    assert "Save reading after reveal" in text  # the page shows where the form stands now
    assert site.records(FIRST) == before
    assert b.post("/form/i07/state", again, headers=FRAGMENT).status_code == 409
    # A form whose system reading was seen before any reading (a marker from the local site):
    # no independent form is offered, and the reading after the reveal is not blind.
    hosted_reveal(site.admin, site.reviewers[FIRST], "i08")
    shown = b.open("i08")
    assert shown.value("_phase") == A.AFTER_REVEAL and "holds SHIPPED" in shown.text
    assert shown.value("timing") == ""  # nothing of the system fills an answer
    attempt = [(n, A.INDEPENDENT if n == "_phase" else v) for n, v in shown.data()]
    refused = b.post("/form/i08/save", attempt)
    assert refused.status_code == 422 and "SYSTEM_READING_ALREADY_SEEN" in refused.get_data(
        as_text=True)
    reading, _form = scenario_reading(site.bundle, "i08")
    b.fill(reading)
    assert "Saved." in b.save().text
    rec = site.records(FIRST)[-1]
    assert (rec["item_id"], rec["phase"], rec["blind"], rec["refers_to"]) == (
        "i08", A.AFTER_REVEAL, False, None)


# ------------------------------------------------------------------ the two questions


def test_the_two_questions_after_the_reveal_stand_beside_the_systems_proposals(site):  # noqa: F811
    b = site.browser().sign_in(FIRST)
    b.open("s14a")
    blind = b.fill({"component": "ACTION_RECORDED_HERE", "timing": "CURRENT", "action": "CLOSE"})
    assert "Does not apply: asked only after the system's reading is revealed." in blind.plain
    assert "System's reference price series" not in blind.plain
    assert not set(R.MAPPING_FIELDS) & blind.names
    b.review("s14a", after=False)
    b.review("s14b", after=False)
    shown = b.open("s14a")
    marks, text = SYSTEM_MARKS["s14a"], shown.plain
    # Each proposal, then its question, then its own input: in that order, in its own block.
    order = ["System's reference price series", marks["reference"],
             "Is this the price series used for the numbers in the post?",
             'name="reference_series_right"', "System's proposed execution instrument",
             marks["execution"],
             "Would using this instrument faithfully follow the publisher",
             'name="execution_instrument_right"']
    places = [text.index(part) for part in order]
    assert places == sorted(places)
    for name in R.MAPPING_FIELDS:
        assert f'<div class="proposal" id="field-{name}">' in text
        assert shown._form.options[name] == ["YES", "NO", "UNSURE"]
        assert shown.value(name) == ""  # unanswered until the reviewer chooses
    assert text.count("not answered yet") == 2
    for label in (">Yes<", ">No<", ">Unsure<"):
        assert text.count(label) == 2
    # Save waits for both answers; the server refuses a save sent without them.
    shown = b.set({"confidence": "HIGH"})
    assert not shown.can_save()
    for name in R.MAPPING_FIELDS:
        assert f"REQUIRED_UNANSWERED ({name})" in shown.text
    refused = b.press_save()
    assert refused.status == 422 and "BLOCKED:REQUIRED_UNANSWERED" in refused.text
    assert A.AFTER_REVEAL not in phases(site)
    shown = b.set({"reference_series_right": "NO"})
    assert not shown.can_save() and shown.text.count("not answered yet") == 1
    assert "REQUIRED_UNANSWERED (reference_series_right)" not in shown.text
    shown = b.set({"execution_instrument_right": "UNSURE"})
    assert shown.can_save() and "not answered yet" not in shown.text
    assert "Saved." in b.press_save().text
    rec = site.records(FIRST)[-1]
    assert (rec["reading"]["reference_series_right"],
            rec["reading"]["execution_instrument_right"]) == ("NO", "UNSURE")
    assert "reference_series_right" not in rec["field_verdicts"]  # an answer, not a verdict
    # Resuming in a new session: the saved answers are chosen again.
    again = site.browser().sign_in(FIRST).open("s14a")
    assert again.value("reference_series_right") == "NO"
    assert again.value("execution_instrument_right") == "UNSURE"
    assert "not answered yet" not in again.text


def test_no_answer_is_asked_without_a_proposal_or_when_the_reading_has_no_action(site):  # noqa: F811
    b = site.browser().sign_in(FIRST)
    b.review("i07", after=False)  # the demo's i07: the system proposed nothing
    shown = b.shown
    assert "The system proposed no reference price series for this post, so no answer is " \
        "asked." in shown.plain
    assert "The system proposed no execution instrument for this post, so no answer is " \
        "asked." in shown.plain
    assert shown.text.count("none proposed") == 2 and not set(R.MAPPING_FIELDS) & shown.names
    assert shown.can_save() is False and "REQUIRED_UNANSWERED (reference" not in shown.text
    b.save()
    rec = site.records(FIRST)[-1]
    assert rec["reading"]["reference_series_right"] == NA
    assert rec["reading"]["execution_instrument_right"] == NA
    # A proposal exists, but the reviewer's reading records no trading action.
    b.review("s14a", after=False)
    b.review("s14b", after=False)
    shown = b.open("s14b")
    assert set(R.MAPPING_FIELDS) <= shown.names
    shown = b.set({"timing": "COMMENTARY"})
    assert not set(R.MAPPING_FIELDS) & shown.names
    assert shown.text.count("No answer is asked: the post states no trading action.") == 2
    assert SYSTEM_MARKS["s14b"]["reference"] in shown.text  # the proposal is still shown
    shown = b.set({"timing": "CURRENT"})  # asked again: the saved answers stand again
    assert shown.value("action") == "OPEN" and set(R.MAPPING_FIELDS) <= shown.names
    shown = b.set({"action": ""})
    assert shown.text.count("No answer is asked yet: choose the action first.") == 2
    shown = b.set({"component": "NO_SEPARATE_ACTION"})
    assert shown.text.count("No answer is asked: this form records no separate action.") == 2
    shown = b.set({"confidence": "HIGH"})
    assert shown.can_save()
    b.press_save()
    rec = site.records(FIRST)[-1]
    assert rec["item_id"] == "s14b" and rec["reading"]["execution_instrument_right"] == NA


# ------------------------------------------------------------------ after the reveal


def test_a_correction_after_the_reveal_derives_again_and_a_verdict_follows_its_answer(
        site):  # noqa: F811
    b = site.browser().sign_in(FIRST)
    b.review("i07", after=False)
    shown = b.shown
    assert shown.value("v.side") == "CONFIRM" and shown.value("v.entry_role") == "CONFIRM"
    assert "verdict: NOT_APPLICABLE (does not apply to your reading)" in shown.text
    # An edited verdict stays while its answer stays, and starts again when the answer changes.
    shown = b.set({"v.side": "UNSURE"})
    shown = b.set({"notes": "looked at the chart again"})
    assert shown.value("v.side") == "UNSURE"
    shown = b.set({"side": "LONG"})
    assert shown.value("v.side") == "CORRECT"
    shown = b.set({"side": "SHORT"})
    assert shown.value("v.side") == "CONFIRM"
    shown = b.set({"v.row.n:1": "UNSURE"})
    assert shown.value("v.row.n:1") == "UNSURE"
    # A corrected timing: the label and what follows are derived again, the announcement is
    # asked, and nothing of the earlier derivation blocks the save.
    shown = b.set({"timing": "HISTORICAL"})
    assert "announcement" in shown.names and shown.value("announcement") == ""
    shown = b.set({"announcement": "FIRST_REPORT", "confidence": "MEDIUM"})
    assert "DERIVED_VALUE" not in shown.text and shown.can_save(), shown.visible()[-900:]
    assert "Saved." in b.press_save().text
    rec = site.records(FIRST)[-1]
    assert rec["reading"]["post_label"] == "RETROSPECTIVE"
    assert rec["reading"]["follower_implication"] == "RECONCILE_POSITION"
    assert rec["field_verdicts"]["timing"] == "CORRECT"
    assert rec["field_verdicts"]["post_label"] == "CORRECT"
    assert rec["field_verdicts"]["levels"]["n:1"] == "UNSURE"
    assert rec["notes"] == "looked at the chart again"
    # A corrected action: the entry no longer applies, and its saved answers go with it.
    shown = b.open("i07")
    assert shown.value("timing") == "HISTORICAL" and shown.value("entry_price") == "412"
    b.set({"action": "CLOSE"})
    shown = b.set({"effect": "CLOSES", "position_state": "OPEN", "idea": "SAME_AS_PREVIOUS"})
    shown = b.set({"position_side": "SHORT", "s.position_last_stop": "VALUE"})
    shown = b.set({"position_last_stop": "420", "confidence": "LOW"})
    assert shown.can_save(), shown.visible()[-900:]
    b.press_save()
    rec = site.records(FIRST)[-1]
    assert rec["reading"]["action"] == "CLOSE" and rec["reading"]["entry_price"] == NA
    assert rec["field_verdicts"]["entry_price"] == NA
    assert rec["refers_to"] == site.records(FIRST)[0]["seq"]  # still the independent reading


# ------------------------------------------------------------------ saving, resuming


def test_the_same_key_twice_stores_one_record(site):  # noqa: F811
    b = site.browser().sign_in(FIRST)
    reading, _form = scenario_reading(site.bundle, "i07")
    b.open("i07")
    b.fill(reading)
    b.set({"confidence": "HIGH", "notes": "a double click"})
    data = b.shown.data()
    first, second = b.post("/form/i07/save", data), b.post("/form/i07/save", data)
    assert first.status_code == second.status_code == 303
    assert first.headers["Location"] == second.headers["Location"]
    [ind] = site.records(FIRST)
    assert first.headers["Location"] == f"/form/i07?saved={ind['seq']}"
    # The connection dropped after the save: the browser sends the same request again later,
    # even from another session. One record, and the saved page.
    later = site.browser().sign_in(FIRST)
    retry = [(n, later.csrf() if n == "_csrf" else v) for n, v in data]
    answer = later.post("/form/i07/save", retry)
    assert answer.status_code == 303 and answer.headers["Location"] == first.headers["Location"]
    page = later.get(answer.headers["Location"]).get_data(as_text=True)
    assert "Saved." in page and f"stored as record {ind['seq']}" in page
    assert [r["phase"] for r in site.records(FIRST)] == [A.INDEPENDENT, A.SYSTEM_SHOWN]
    # The reading after the reveal, the same way.
    later.open("i07")
    later.set({"confidence": "MEDIUM"})
    data = later.shown.data()
    answers = [later.post("/form/i07/save", data) for _ in range(3)]
    assert {a.status_code for a in answers} == {303}
    assert len({a.headers["Location"] for a in answers}) == 1
    assert [r["phase"] for r in site.records(FIRST)] == [
        A.INDEPENDENT, A.SYSTEM_SHOWN, A.AFTER_REVEAL]
    assert len(site.sql("SELECT 1 FROM event")) == 3
    # A key is the page's own: no key, a malformed or made-up one, the key of another form or
    # of another reviewer's page saves nothing.
    for key in ("", "short", "x" * 80, "has spaces in it 0123456789", "k" * 24,
                "k" * 24 + "." + "0" * 32, site.page_key(FIRST, "i08"),
                site.page_key(SECOND, "i07"), site.page_key(FIRST, "i07") + "0"):
        bad = [(n, key if n == "_key" else v) for n, v in data]
        assert later.post("/form/i07/save", bad).status_code == 400, key
    assert len(site.sql("SELECT 1 FROM event")) == 3
    # A confirmation names only the reviewer's own record of that form.
    assert "Saved." not in later.get("/form/i08?saved=" + str(ind["seq"])).get_data(as_text=True)
    assert "Saved." not in later.get("/form/i07?saved=999").get_data(as_text=True)
    other = site.browser().sign_in(SECOND)
    assert "Saved." not in other.get(first.headers["Location"]).get_data(as_text=True)


def test_the_same_page_posted_with_other_answers_is_another_save(site):  # noqa: F811
    """The review's finding 7: a second POST with the key of a stored save and a CHANGED body
    was answered "Saved." although the changed body was not stored; the same key sent to another
    form redirected there without a word."""
    b = site.browser().sign_in(FIRST)
    reading, _form = scenario_reading(site.bundle, "i07")
    b.open("i07")
    b.fill(reading)
    b.set({"confidence": "HIGH", "notes": "first body"})
    data = b.shown.data()
    first = b.post("/form/i07/save", data)
    assert first.status_code == 303
    changed = [(n, "LOW" if n == "confidence" else "second body, changed" if n == "notes" else v)
               for n, v in data]
    second = b.post("/form/i07/save", changed)
    # Another save: checked as one. An independent reading exists, so the core refuses it; the
    # page says "Not saved", never "Saved".
    text = second.get_data(as_text=True)
    assert second.status_code == 422 and "Not saved (SYSTEM_READING_ALREADY_SEEN)" in text
    assert "<strong>Saved.</strong>" not in text
    assert [(r["confidence"], r["notes"]) for r in site.records(FIRST, markers=False)] == [
        ("HIGH", "first body")]
    # The first POST again, unchanged, is still the same save.
    assert b.post("/form/i07/save", data).headers["Location"] == first.headers["Location"]
    # The same key on another form is not a key there: an error page, nothing stored.
    other = b.post("/form/i08/save", data)
    assert other.status_code == 400 and "Location" not in other.headers
    assert b"could not be read" in other.get_data()
    assert {r["item_id"] for r in site.records(FIRST)} == {"i07"}
    # After the reveal a changed body is a changed reading: stored as the new record it is.
    b.open("i07")
    b.set({"confidence": "MEDIUM", "notes": "after the reveal"})
    data = b.shown.data()
    assert b.post("/form/i07/save", data).status_code == 303
    changed = [(n, "after the reveal, corrected" if n == "notes" else v) for n, v in data]
    again = b.post("/form/i07/save", changed)
    assert again.status_code == 303
    assert b.post("/form/i07/save", changed).headers["Location"] == again.headers["Location"]
    assert [r["notes"] for r in site.records(FIRST) if r["phase"] == A.AFTER_REVEAL] == [
        "after the reveal", "after the reveal, corrected"]
    # What the store keeps is a hash: neither the page's key nor an answer.
    kept = [row[0] for row in site.sql("SELECT idempotency_key FROM event WHERE "
                                       "idempotency_key IS NOT NULL")]
    assert len(kept) == 3 and all(len(k) == 64 and "corrected" not in k for k in kept)
    assert dict(data)["_key"] not in kept


def test_two_reviewers_saving_at_the_same_moment_both_succeed_as_themselves(site):  # noqa: F811
    browsers = {alias: site.browser().sign_in(alias) for alias in (FIRST, SECOND)}
    reading, _form = scenario_reading(site.bundle, "i07")
    notes = {FIRST: "the first reviewer's note", SECOND: "the second reviewer's note"}

    def ready(phase_notes):
        out = {}
        for alias, b in browsers.items():
            b.open("i07")
            if b.shown.value("_phase") == A.INDEPENDENT:
                b.fill(reading)
            b.set({"confidence": "HIGH", "notes": phase_notes[alias]})
            assert b.shown.can_save()
            out[alias] = b.shown.data()
        return out

    def together(data):
        gate, answers = threading.Barrier(4), {}

        def press(alias, n):
            gate.wait()
            answers[alias, n] = browsers[alias].post("/form/i07/save", data[alias]).status_code

        threads = [threading.Thread(target=press, args=(alias, n))
                   for alias in browsers for n in (1, 2)]  # each reviewer double-clicks too
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        return answers

    assert set(together(ready(notes)).values()) == {303}
    for alias, b in browsers.items():
        assert "holds SHIPPED" in b.open("i07").text  # each reveal is that reviewer's own
    assert set(together(ready({a: n + ", after" for a, n in notes.items()})).values()) == {303}
    for alias in browsers:
        records = site.records(alias)
        assert [r["phase"] for r in records] == [A.INDEPENDENT, A.SYSTEM_SHOWN, A.AFTER_REVEAL]
        assert {r["annotator"]["login"] for r in records} == {site.reviewers[alias].login_shown}
        assert [r["notes"] for r in records] == [notes[alias], "", notes[alias] + ", after"]
        assert records[2]["refers_to"] == records[0]["seq"]
    seqs = [row[0] for row in site.sql("SELECT seq FROM event ORDER BY seq")]
    assert seqs == list(range(1_000_001, 1_000_007))  # one chain, no gap, six records
    for alias, other in ((FIRST, SECOND), (SECOND, FIRST)):
        pages = browsers[alias].open("i07").text + browsers[alias].get("/").get_data(as_text=True)
        assert notes[other] not in pages


def test_resuming_in_a_new_session_lands_on_the_next_unfinished_step(site14):
    from reviewer_web import forms as F

    site = site14  # noqa: F811
    session = site.reviewers[FIRST].session()

    def resume() -> str:
        """Where "Continue" leads in a new session: exactly what the pure function says."""
        answer = site.browser().sign_in(FIRST).get("/continue")
        assert answer.status_code == 303
        step = F.next_step(site.bundle, session, site.records(FIRST))
        assert answer.headers["Location"] == f"/form/{step}"
        return answer.headers["Location"]

    def state(item_id: str) -> str:
        """The form's state by the pure function, checked against what its page shows."""
        named = F.step_state(site.bundle, session, site.records(FIRST), item_id)
        shown = site.browser().sign_in(FIRST).open(item_id)
        assert shown.value("_phase") == {
            F.INDEPENDENT_DUE: A.INDEPENDENT, F.REVEAL_PENDING: None}.get(named, A.AFTER_REVEAL)
        return named

    assert resume() == "/form/s14a"  # nothing saved: the first form in review order
    assert (state("s14a"), state("s14b")) == (F.INDEPENDENT_DUE, F.INDEPENDENT_DUE)
    b = site.browser().sign_in(FIRST)
    b.review("s14a", after=False)
    assert "Next step:" in b.shown.text and "continue with s14b" in b.shown.visible()
    # Form 1 is saved, the post is not revealed: the step is the sibling without a reading.
    assert (state("s14a"), state("s14b")) == (F.REVEAL_PENDING, F.INDEPENDENT_DUE)
    assert resume() == "/form/s14b"
    home = site.browser().sign_in(FIRST).get("/").get_data(as_text=True)
    assert "In progress" in home and "/sequence/scs14" in home  # the dashboard (FLOW)
    b.review("s14b", after=False)
    assert "Next step: on this same form, below." in b.shown.visible()
    assert (state("s14a"), state("s14b")) == (F.AFTER_REVEAL_DUE, F.AFTER_REVEAL_DUE)
    assert "Awaiting your assessment" in b.get("/").get_data(as_text=True)
    # Both saved and revealed: the first form that is not done, in the state after the reveal,
    # with the independent answers intact.
    assert resume() == "/form/s14a"
    fresh = site.browser().sign_in(FIRST)
    shown = fresh.open("s14a")
    assert shown.value("_phase") == A.AFTER_REVEAL
    reading, _form = scenario_reading(site.bundle, "s14a")
    for name, value in wanted(reading).items():
        assert shown.value(name) == value, name
    assert shown.value("confidence") == "" and SYSTEM_MARKS["s14a"]["reference"] in shown.text
    fresh.set({"reference_series_right": "YES", "execution_instrument_right": "YES"})
    assert "continue with s14b" in fresh.save().visible()
    assert (state("s14a"), state("s14b")) == (F.DONE, F.AFTER_REVEAL_DUE)
    assert resume() == "/form/s14b"
    fresh.open("s14b")
    fresh.set({"reference_series_right": "YES", "execution_instrument_right": "YES"})
    done = fresh.save()
    assert "continue with i07" in done.visible()  # the post is done: on to the next idea
    assert resume() == "/form/i07"
    page = fresh.get("/").get_data(as_text=True)
    assert "1 of 33" in page and "2 of 45" in page  # ideas completed, forms done
    assert page.index("/form/s14a") < page.index("/form/s14b") < page.index("/form/i07")
    # The other reviewer starts at the beginning.
    assert site.browser().sign_in(SECOND).get("/continue").headers["Location"] == "/form/s14a"


def test_a_reading_the_approver_returned_is_shown_to_its_reviewer_only(site):  # noqa: F811
    a, b = site.browser().sign_in(FIRST), site.browser().sign_in(SECOND)
    a.review("i07")
    a.review("i08")
    b.review("i08")
    assert a.get("/continue").headers["Location"] == "/form/i01"  # the first never started
    note = "Row 1 is a fill, not the market: please look at it again."
    # The operator tool stores of the approver the role only. Here the line names the approver
    # all the same: whatever a stored line says of them, no page shows it.
    returned = site.return_reading(FIRST, "i08", note, annotator={
        "login": "owner@example.invalid", "name": "Owner Example", "role": A.APPROVER,
        "access": A.FULL})
    shown = a.open("i08")
    after = [r for r in site.records(FIRST) if r["phase"] == A.AFTER_REVEAL][-1]
    assert f"SAVED, and RETURNED by the approver. Your reading of this form after the reveal " \
        f"is record {after['seq']}" in shown.text
    assert "The approver returned this reading." in shown.text and note in shown.visible()
    assert "owner@example.invalid" not in shown.text and "Owner Example" not in shown.text
    # The form is open again with the reviewer's latest answers.
    assert shown.value("_phase") == A.AFTER_REVEAL and shown.value("action") == "CLOSE"
    assert shown.value("lv.n:0.role") == "HISTORICAL_FILL"
    # The workspace says so, and "continue" goes there before the first form never started.
    home = a.get("/").get_data(as_text=True)
    assert "Returned for changes" in home and "The approver returned 1 reading to you." in " ".join(
        home.split())
    assert a.get("/continue").headers["Location"] == "/form/i08"
    assert "1 of 45" in home  # the reviewer's own saves; a returned form is not complete
    # The other reviewer, who read the same form, sees nothing of it.
    for text in (b.open("i08").text, b.get("/").get_data(as_text=True)):
        assert "RETURNED" not in text and "Returned for changes" not in text and note not in text
    assert b.get("/continue").headers["Location"] == "/form/i07"
    assert returned not in site.records(SECOND) and returned in site.records(FIRST)
    from reviewer_web import forms as F

    for alias, expected in ((FIRST, F.RETURNED), (SECOND, F.DONE)):
        session = site.reviewers[alias].session()
        assert F.step_state(site.bundle, session, site.records(alias), "i08") == expected
    assert F.next_step(site.bundle, site.reviewers[FIRST].session(), site.records(FIRST)) == "i08"
    # The approver's record is never taken for the reviewer's own.
    assert "Saved." not in a.get(f"/form/i08?saved={returned['seq']}").get_data(as_text=True)
    # The corrected reading is saved: the return no longer stands.
    a.open("i08")
    a.set({"lv.n:0.exactness": "APPROXIMATE"})
    done = a.save(confidence="MEDIUM", notes="corrected after the return")
    assert "Saved." in done.text and "RETURNED" not in done.text
    assert "SAVED. Your reading of this form after the reveal is record" in done.text
    home = a.get("/").get_data(as_text=True)
    assert "Returned for changes" not in home and "The approver returned" not in home
    assert a.get("/continue").headers["Location"] == "/form/i01"
    newest = [r for r in site.records(FIRST) if r["phase"] == A.AFTER_REVEAL][-1]
    assert newest["seq"] > after["seq"] and newest["notes"] == "corrected after the return"
    assert newest["reading"]["levels"][0]["exactness"] == "APPROXIMATE"
    assert [r["phase"] for r in site.records(FIRST)][-1] == A.APPROVAL  # kept, and overtaken


def test_a_return_the_approver_took_back_shows_nowhere_on_the_site(site):  # noqa: F811
    """When a pushed return no longer stands locally, the operator tool stores ONE line without
    content after it (decision WITHDRAWN; the RETURN stream is numbered 2000000 + twice a local
    line's seq, a clearing line one above). The pages go by the latest approval of the
    reviewer's latest reading: after a clearing line nothing says "returned", nothing of the
    return or of the later decision is shown, and "continue" asks for no save."""
    from reviewer_web import forms as F

    a = site.browser().sign_in(FIRST)
    a.review("i07")
    a.review("i08")
    note = "Row 1 is a fill, not the market: please look at it again."
    returned = site.return_reading(FIRST, "i08", note)
    assert returned["annotator"] == {"login": "approver", "name": "Approver",
                                     "role": A.APPROVER, "access": A.FULL}  # as the tool stores
    assert 2_000_000 < returned["seq"] < 3_000_000 and returned["seq"] % 2 == 0
    assert a.get("/continue").headers["Location"] == "/form/i08"
    assert "Returned for changes" in a.get("/").get_data(as_text=True)

    def state() -> str:
        return F.step_state(site.bundle, site.reviewers[FIRST].session(), site.records(FIRST),
                            "i08")

    def nothing_is_returned(*notes: str) -> None:
        home, shown = Shown(a.get("/")), a.open("i08")
        for page in (home, shown):
            assert "Returned for changes" not in page.text and "RETURNED" not in page.text
            assert "The approver returned" not in page.text and "WITHDRAWN" not in page.text
            assert not [n for n in (*notes, "never stored") if n in page.plain]
        assert "SAVED. Your reading of this form after the reveal is record" in shown.text
        assert "2 of 45" in home.text and state() == F.DONE
        assert a.get("/continue").headers["Location"] == "/form/i01"

    assert state() == F.RETURNED
    cleared = site.clear_return(FIRST, returned)
    assert cleared["seq"] > returned["seq"] and cleared["seq"] % 2 == 1
    assert (cleared["decision"], cleared["notes"], cleared["reading"], cleared["clears"]) == (
        "WITHDRAWN", "", {}, returned["seq"])
    assert site.records(FIRST)[-2:] == [returned, cleared]
    nothing_is_returned(note)
    # Neither line of the approver is ever taken for a save of the reviewer's own.
    for record in (returned, cleared):
        page = a.get(f"/form/i08?saved={record['seq']}").get_data(as_text=True)
        assert "Saved." not in page and "RETURNED" not in page
    # The other reviewer's pages never knew of either line.
    b = site.browser().sign_in(SECOND)
    assert "Returned" not in b.get("/").get_data(as_text=True)
    assert not [r for r in site.records(SECOND) if r["phase"] == A.APPROVAL]
    # The store keeps the local order: a return made AFTER the clearing line shows again,
    again = site.return_reading(FIRST, "i08", "Once more: row 1.")
    assert again["seq"] > cleared["seq"] and state() == F.RETURNED
    assert a.get("/continue").headers["Location"] == "/form/i08"
    shown = a.open("i08")
    assert "The approver returned this reading." in shown.text
    assert "Once more: row 1." in shown.visible() and note not in shown.visible()
    # and the reviewer's corrected save ends it, with all three lines among the records.
    a.set({"lv.n:0.exactness": "APPROXIMATE"})
    done = a.save(confidence="MEDIUM", notes="corrected after the second return")
    assert "Saved." in done.text and "RETURNED" not in done.text
    assert [r["decision"] for r in site.records(FIRST) if r["phase"] == A.APPROVAL] == [
        "RETURN", "WITHDRAWN", "RETURN"]
    newest = [r for r in site.records(FIRST) if r["phase"] == A.AFTER_REVEAL][-1]
    assert newest["notes"] == "corrected after the second return"
    nothing_is_returned(note, "Once more: row 1.")
    # A clearing line that comes when the reviewer has already answered changes nothing.
    site.clear_return(FIRST, again)
    nothing_is_returned(note, "Once more: row 1.")


def test_publisher_text_is_shown_as_text_never_as_markup(tmp_path, store, dsn):  # noqa: F811
    hostile = '<script>alert("x")</script> <b onclick="x()">Short</b> Zed & Metals around 412'

    def edit(base: Path) -> None:
        path = base / "bundle" / "posts.jsonl"
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        for row in rows:
            if row["post_ref"] == "p07":
                row["text"] = hostile + ", stop 420."
        path.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows))

    site = Site(tmp_path, store, dsn, edit=edit)  # noqa: F811
    b = site.browser().sign_in(FIRST)
    for item in ("i07", "i08"):  # as the post itself, and as an earlier post of the idea
        text = b.open(item).text
        assert "&lt;script&gt;alert(" in text and "<script>alert(" not in text
        assert "<b onclick" not in text and "Zed &amp; Metals" in text
    b.open("i07")
    typed = '"><img src=x onerror=1>'
    shown = b.set({"timing": "CURRENT", "action": "OPEN", "notes": typed})
    shown = b.set({"s.instrument_as_read": "VALUE"})
    shown = b.set({"instrument_as_read": typed})
    assert "<img src=x" not in shown.text and shown.value("notes") == typed
    assert shown.value("instrument_as_read") == typed
    assert site.app.jinja_env.autoescape("form.html") is True
    templates = Path(site.app.root_path) / "templates"
    assert not [p.name for p in templates.glob("*.html") if "|safe" in p.read_text().replace(
        " ", "") or "autoescape" in p.read_text() or "Markup" in p.read_text()]


def test_a_form_that_enters_the_scope_after_its_post_was_revealed_can_be_completed(
        tmp_path, store, dsn):  # noqa: F811
    """The review's finding 5. Scope 2; the forms i07 and x01 (of another idea) share one post
    and x01 is outside the scope. The reviewer finishes i07; the scope is raised. x01 offered
    the independent form, which the core can no longer accept: a form nobody could complete."""
    from reviewer_web import forms as F
    from test_hosted_admin import extend

    site = Site(tmp_path, store, dsn, mark=False, edit=lambda base: extend(base, 2))  # noqa: F811
    b = site.browser().sign_in(FIRST)
    assert b.get("/form/x01").status_code == 404  # outside the scope of 2
    reading = {"component": "ACTION_RECORDED_HERE", **scenario_reading(site.bundle, "i07")[0]}
    b.open("i07")
    b.fill(reading)
    assert "Saved." in b.save().text and "already shown to you" not in b.shown.text
    assert "Saved." in b.save().text
    assert [(r["item_id"], r["phase"], r["blind"]) for r in site.records(FIRST)] == [
        ("i07", A.INDEPENDENT, True), ("i07", A.SYSTEM_SHOWN, False), ("i07", A.AFTER_REVEAL, True)]
    site.set_reviewer(FIRST, scope_sequences=3)  # the scope is raised: x01 enters
    session = site.reviewers[FIRST].session()

    def state(item_id):
        return F.step_state(site.bundle, session, site.records(FIRST), item_id)

    # The post was shown: x01 is due after the reveal, by the function and on the page.
    assert state("x01") == F.AFTER_REVEAL_DUE and state("i07") == F.DONE
    b = site.browser().sign_in(FIRST)
    shown = b.open("x01")
    assert shown.value("_phase") == A.AFTER_REVEAL and "holds SHIPPED" in shown.text
    assert "The system's reading of this post was already shown to you" in shown.plain
    assert "reading after the reveal due" in shown.text
    assert shown.value("component") == "" and "timing" not in shown.names  # nothing is filled
    assert ("x01", A.SYSTEM_SHOWN) in [(r["item_id"], r["phase"]) for r in site.records(FIRST)]
    # An independent reading is what the core refuses; the page never offers that form.
    attempt = [(n, A.INDEPENDENT if n == "_phase" else v) for n, v in shown.data()]
    refused = b.post("/form/x01/save", attempt)
    assert refused.status_code == 422 and b"SYSTEM_READING_ALREADY_SEEN" in refused.get_data()
    assert b'name="_phase" value="AFTER_REVEAL"' in refused.get_data()
    # The reading is saved, as one given after seeing the system's.
    b.open("x01")
    b.fill(reading)
    done = b.save()
    assert "Saved." in done.text and "Not saved" not in done.text
    rec = site.records(FIRST)[-1]
    assert (rec["item_id"], rec["phase"], rec["blind"], rec["refers_to"]) == (
        "x01", A.AFTER_REVEAL, False, None)
    assert state("x01") == F.DONE
    # "Continue" never leads to a form that cannot be saved: whatever it names is a form whose
    # page offers the phase the core accepts.
    step = F.next_step(site.bundle, session, site.records(FIRST))
    assert step == "i08" and b.get("/continue").headers["Location"] == "/form/i08"
    for item_id in F.scoped(site.bundle, session):
        offered = site.browser().sign_in(FIRST).open(item_id).value("_phase")
        assert offered == {F.INDEPENDENT_DUE: A.INDEPENDENT, F.REVEAL_PENDING: None}.get(
            state(item_id), A.AFTER_REVEAL), item_id


def test_a_save_carries_the_rows_spelling_of_the_sign_in_however_it_was_typed(site):  # noqa: F811
    """The core finds a reviewer's records by the address in each record, compared exactly with
    the session's; the store refuses a line spelled otherwise than its row. So whatever capitals
    a reviewer types at sign-in, the lines the site builds carry the row's spelling, and the
    reviewer finds their work again under any other."""
    from reviewer_web import forms as F

    row = site.reviewers[FIRST]
    spelled = row.login_shown.title()  # the row as the reviewers file might spell it
    assert spelled not in (row.login_shown, row.login_shown.upper())
    site.admin.upsert_reviewer(FIRST, spelled, name=row.name, scope_sequences=row.scope_sequences)
    assert site.reviewers[FIRST].login_shown == spelled

    def sign_in(typed: str):
        b = site.browser()
        site.sql("DELETE FROM rate_limit")
        assert b.post("/sign-in", {"address": typed}).status_code == 200
        assert site.outbox.sent[-1][0] == spelled  # mail goes to the row's spelling
        assert b.post("/sign-in/verify", {"address": typed,
                                          "code": site.code()}).status_code == 303
        return b

    a = sign_in(spelled.lower())
    a.review("i07")
    lines = [e for e in site.admin.events() if e["alias"] == FIRST]
    assert [json.loads(e["line"])["annotator"]["login"] for e in lines] == [spelled] * 3
    assert {e["login_shown"] for e in lines} == {spelled}
    assert F.step_state(site.bundle, site.reviewers[FIRST].session(), site.records(FIRST),
                        "i07") == F.DONE
    # Typed in capitals on another device: the same history, the same next step.
    again = sign_in(spelled.upper())
    assert "1 of 45" in again.get("/").get_data(as_text=True)
    assert again.get("/continue").headers["Location"] == "/form/i08"
    assert "SAVED. Your reading of this form after the reveal is record" in again.open("i07").text
    again.review("i08")
    assert {r["annotator"]["login"] for r in site.records(FIRST)} == {spelled}
    # From now on the row keeps that spelling: its lines are found by it.
    with pytest.raises(A.AdjudicationError, match="LOGIN_SPELLING_HAS_RECORDS"):
        site.admin.upsert_reviewer(FIRST, spelled.lower(), name=row.name,
                                   scope_sequences=row.scope_sequences)
