"""Where a reviewer stands (``hosted/reviewer_web/progress.py``): the dashboard of one row per
sequence, the sidebar, each form's plain-language status, the step that remains after a save,
and Start / Continue per sequence. Derived from the reviewer's own confirmed saves only, and
blind: three databases that differ only in the system's reading give a reviewer who has not
passed the reveal byte-identical dashboards, sidebars and Start / Continue answers. INVENTED
data on a real Postgres."""

from __future__ import annotations

from pathlib import Path

import pytest
from hosted_web_site import FIRST, SECOND, Site, dsn, site, store  # noqa: F401 - fixtures

from tradingbot.runtime import adjudication as A

FIRST_POST = "scs14"  # the demo idea whose post s14 has two forms (s14a, s14b)


def assess(browser, item_id: str) -> None:
    """The reading after the reveal, with both mapping questions answered."""
    browser.open(item_id)
    browser.set({"reference_series_right": "YES", "execution_instrument_right": "YES"})
    assert "Saved." in browser.save().text


def first(base: Path, sequence_id: str = FIRST_POST) -> None:
    from test_hosted_web_forms import first_idea

    first_idea(base, sequence_id)


@pytest.fixture
def site14(tmp_path, store, dsn) -> Site:  # noqa: F811
    return Site(tmp_path, store, dsn, edit=first)


def model(site, alias=FIRST, item=None):  # noqa: F811
    from reviewer_web import progress as P

    row = site.reviewers[alias]
    records = site.admin.records_of(row)
    return P, row.session(), records


def test_each_form_has_a_status_from_the_reviewer_s_own_saves(site14):
    P, session, records = model(site14)
    b = site14.bundle
    assert P.form_status(b, session, records, "s14a") == {
        "code": "NOT_STARTED", "text": "Not started", "waiting_for": None, "draft": False,
        "step": "SAVE_YOUR_READING"}
    browser = site14.browser().sign_in(FIRST)
    browser.review("s14a", after=False)
    P, session, records = model(site14)
    found = P.form_status(b, session, records, "s14a")
    assert found["code"] == "IN_PROGRESS" and found["text"] == "In progress"
    assert found["waiting_for"] == {"item_id": "s14b", "k": 2, "n": 2}
    step = P.remaining_step(b, session, records, "s14a")
    assert step == {"code": "COMPLETE_OTHER_FORM", "item_id": "s14b", "k": 2, "n": 2}
    assert "another form of this post must be completed before the system's answers are " \
        "shown" in P.TEXT[step["code"]].casefold()
    # The page after the independent save names that step (the ``step`` context key).
    page = browser.open("s14a")
    assert page.status == 200
    browser.review("s14b", after=False)
    P, session, records = model(site14)
    assert [P.form_status(b, session, records, i)["code"] for i in ("s14a", "s14b")] == [
        "AWAITING_ASSESSMENT"] * 2
    assert P.remaining_step(b, session, records, "s14a")["code"] == "ASSESS_SYSTEM_ANSWERS"
    assess(browser, "s14a")  # after the reveal too
    P, session, records = model(site14)
    assert P.form_status(b, session, records, "s14a")["code"] == "COMPLETE"
    assert P.remaining_step(b, session, records, "s14a") == {"code": "NEXT_FORM",
                                                            "item_id": "s14b"}
    # Another reviewer's saves change nothing of this reviewer's statuses.
    P, other, theirs = model(site14, SECOND)
    assert {P.form_status(b, other, theirs, i)["code"] for i in ("s14a", "s14b")} == {
        "NOT_STARTED"}


def test_the_dashboard_has_one_row_per_sequence_and_start_or_continue_opens_its_next_step(
        site14):
    from reviewer_web import forms as F

    P, session, records = model(site14)
    dash = P.dashboard(site14.bundle, session, records)
    order = [sid or P.NO_SEQUENCE for sid, _items in A.ordered(site14.bundle, session.scope)]
    assert [r["id"] for r in dash["sequences"]] == order and order[-1] == P.NO_SEQUENCE
    row = dash["sequences"][0]
    assert (row["id"], row["status"], row["done"], row["total"], row["remaining"],
            row["button"], row["next_item"]) == (FIRST_POST, "NOT_STARTED", 0, 2, 2, "START",
                                                 "s14a")
    assert [[(f["item_id"], f["k"], f["n"]) for f in p["forms"]] for p in row["posts"]] == [
        [("s14a", 1, 2), ("s14b", 2, 2)]]
    b = site14.browser().sign_in(FIRST)
    home = b.get("/").get_data(as_text=True)
    assert 'href="/sequence/scs14">Start' in home and "<details>" in home
    assert b.get("/sequence/scs14").headers["Location"] == "/form/s14a"
    b.review("s14a", after=False)
    # Continue opens the form of the post that still needs a reading.
    assert b.get("/sequence/scs14").headers["Location"] == "/form/s14b"
    P, session, records = model(site14)
    row = P.dashboard(site14.bundle, session, records)["sequences"][0]
    assert (row["status"], row["button"], row["done"]) == ("IN_PROGRESS", "CONTINUE", 0)
    home = b.get("/").get_data(as_text=True)
    assert 'href="/sequence/scs14">Continue' in home and "In progress" in home
    b.review("s14b", after=False)
    assess(b, "s14a")
    assess(b, "s14b")
    P, session, records = model(site14)
    dash = P.dashboard(site14.bundle, session, records)
    assert (dash["sequences"][0]["status"], dash["sequences"][0]["button"]) == ("COMPLETE", None)
    assert b.get("/sequence/scs14").headers["Location"] == "/"
    assert dash["totals"]["done"] == 2 and dash["next"] == F.next_step(site14.bundle, session,
                                                                     records)
    # A sequence the reviewer does not have is no sequence at all.
    for unknown in ("scs99", "x" * 300, "%2e%2e"):
        assert b.get(f"/sequence/{unknown}").status_code == 404


def test_a_returned_form_comes_first_in_its_sequence(site):  # noqa: F811
    from reviewer_web import progress as P

    a = site.browser().sign_in(FIRST)
    a.review("i07")
    a.review("i08")
    site.return_reading(FIRST, "i08", "look again")
    _p, session, records = model(site)
    dash = P.dashboard(site.bundle, session, records)
    row = next(r for r in dash["sequences"] if any(
        f["item_id"] == "i08" for p in r["posts"] for f in p["forms"]))
    assert row["returned"] == 1 and row["next_item"] == "i08"
    assert a.get(f"/sequence/{row['id']}").headers["Location"] == "/form/i08"
    assert P.form_status(site.bundle, session, records, "i08")["text"] == "Returned for changes"
    assert P.remaining_step(site.bundle, session, records, "i08")["code"] == "CORRECT_RETURNED"


def test_the_sidebar_expands_the_active_sequence_and_marks_the_current_form(site14):
    from flask import render_template

    P, session, records = model(site14)
    nav = P.sidebar(site14.bundle, session, records, "s14b")
    assert nav["active"] == FIRST_POST and nav["current"] == "s14b"
    assert [f["current"] for f in nav["sequences"][0]["posts"][0]["forms"]] == [False, True]
    assert not any(f["current"] for r in nav["sequences"][1:] for p in r["posts"]
                   for f in p["forms"])
    with site14.app.test_request_context():
        html = render_template("_sidebar.html", nav=nav)
    assert html.count('aria-current="page"') == 1
    assert '<a href="/form/s14b" aria-current="page">' in html
    # The active sequence only: the panel itself opens on a wide screen (the script), and stays
    # one line above the page on a phone.
    assert html.count(" open>") == 1
    assert "Form 2 of 2" in html and "<script" not in html
    # The form page carries the model as ``nav``, ``status`` and ``step``.
    b = site14.browser().sign_in(FIRST)
    assert b.open("s14b").status == 200


# Blind first, byte for byte, over the dashboard, the sidebar and every other surface: the
# reviewer's three-database walk in tests/test_hosted_blind.py (round 2, M3).


def test_the_form_page_carries_nav_status_and_step_after_an_independent_save(site14):
    """The context keys of the form page (DESIGN_UX section 5): after the first form of a
    two-form post is saved, ``step`` says another form must be completed before the system's
    answers are shown, and names it."""
    from flask import template_rendered

    shown = []

    def note(_app, template, context, **_kw):
        if template.name == "form.html":
            shown.append({k: context.get(k) for k in ("nav", "status", "step", "draft")})

    b = site14.browser().sign_in(FIRST)
    with template_rendered.connected_to(note, site14.app):
        b.review("s14a", after=False)
    last = shown[-1]
    assert last["step"] == {"code": "COMPLETE_OTHER_FORM", "item_id": "s14b", "k": 2, "n": 2}
    assert last["status"]["code"] == "IN_PROGRESS" and str(last["status"])  # prints as before
    assert last["nav"]["current"] == "s14a" and last["nav"]["active"] == FIRST_POST
    assert last["draft"] is None
