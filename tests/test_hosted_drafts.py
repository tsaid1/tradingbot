"""Drafts and interface state of the hosted site (DESIGN_UX section 5): what a page last sent for
a form is kept (table ``draft``) by every round trip and by a refused save, loaded when the form
is opened again, and deleted in the transaction of the form's successful save. A draft is never
an event: it changes no status but its own flag, is never exported or pulled, and one of another
phase than the form's current one is dropped. Posted fields named ``ui.*`` are kept in a draft,
ignored by validation, never written to an event, and no part of what makes a save the same save.
INVENTED data on a real Postgres."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from hosted_web_site import FIRST, SECOND, dsn, scenario_reading, site, store  # noqa: F401

from tradingbot.runtime import adjudication as A


def drafts(site) -> list[tuple]:  # noqa: F811
    return site.sql("SELECT r.alias, d.item_id, d.phase FROM draft d JOIN reviewer r"
                    " ON r.id = d.reviewer_id ORDER BY r.alias, d.item_id")


def test_a_round_trip_keeps_a_draft_that_comes_back_and_ends_with_the_save(site):  # noqa: F811
    from reviewer_web import progress as P

    b = site.browser().sign_in(FIRST)
    reading, form = scenario_reading(site.bundle, "i07")
    b.open("i07")
    assert drafts(site) == []
    shown = b.set({"notes": "a draft note", "ui.later": "timing", "ui.active": "timing"})
    assert drafts(site) == [(FIRST, "i07", A.INDEPENDENT)]
    # The answer says that the draft was kept, and when (for the script: in a header too).
    kept = shown.response.headers["X-Draft-Kept"]
    assert kept == site.clock().isoformat(timespec="seconds") and "note" not in kept
    fragment = b.post("/form/i07/state", b.shown.data(), headers={"X-Fragment": "form"})
    assert fragment.headers["X-Fragment"] == "form" and fragment.headers["X-Draft-Kept"]
    # A new browser (another device) opens the form with the draft's answers in it.
    again = site.browser().sign_in(FIRST).open("i07")
    assert again.value("notes") == "a draft note"
    # The draft changes nothing of the form's status but its flag; nothing is an event.
    row = site.reviewers[FIRST]
    records = site.admin.records_of(row)
    status = P.form_status(site.bundle, row.session(), records, "i07", drafts={"i07"})
    assert (status["code"], status["draft"]) == ("NOT_STARTED", True)
    assert records == [] and not site.admin.events()
    home = b.get("/").get_data(as_text=True)
    assert "Draft kept, not submitted" in home and "Not started" in home
    # The other reviewer has no draft and sees none.
    other = site.browser().sign_in(SECOND)
    assert other.open("i07").value("notes") != "a draft note"
    assert "Draft kept" not in other.get("/").get_data(as_text=True)
    # A refused save keeps the draft too; the successful save ends it in its transaction.
    refused = b.press_save(follow=False)
    assert refused.status == 422 and drafts(site) == [(FIRST, "i07", A.INDEPENDENT)]
    b.open("i07")
    b.fill(reading)
    saved = b.save(confidence=form["confidence"], flags=form["flags"], notes=form["notes"])
    assert "Saved." in saved.text
    assert (FIRST, "i07", A.INDEPENDENT) not in drafts(site)
    # The stored reading holds nothing of the interface state.
    [rec] = [r for r in site.admin.records_of(row) if r["phase"] == A.INDEPENDENT]
    assert "ui." not in json.dumps(rec) and "later" not in json.dumps(rec["reading"])


def test_a_draft_of_a_phase_the_form_has_left_is_dropped(site):  # noqa: F811
    b = site.browser().sign_in(FIRST)
    b.open("i07")
    b.set({"notes": "a draft note"})
    row = site.reviewers[FIRST]
    # The phase moves on without this page: the reading is saved from another device.
    other = site.browser().sign_in(FIRST)
    reading, form = scenario_reading(site.bundle, "i07")
    other.open("i07")
    other.fill(reading)
    other.save(confidence=form["confidence"], flags=form["flags"], notes=form["notes"])
    site.admin.keep_draft(row, "i07", A.INDEPENDENT, [("_phase", A.INDEPENDENT)],
                          datetime(2026, 11, 25, 9, 0, tzinfo=UTC))  # a stale one, by hand
    shown = b.open("i07")
    assert shown.value("_phase") == A.AFTER_REVEAL and drafts(site) == []


def test_the_same_save_twice_is_one_event_whatever_the_interface_state_says(site):  # noqa: F811
    b = site.browser().sign_in(FIRST)
    reading, form = scenario_reading(site.bundle, "i08")
    b.open("i08")
    b.fill(reading)
    b.set({"confidence": form["confidence"], "ui.later": "", "ui.active": "family"})
    data = b.shown.data()
    first = b.post("/form/i08/save", data)
    marks = [(n, "timing" if n.startswith("ui.") else v) for n, v in data]
    second = b.post("/form/i08/save", marks)  # a retry after the marks changed
    third = b.post("/form/i08/save", data)
    assert [r.status_code for r in (first, second, third)] == [303, 303, 303]
    assert len({r.headers["Location"] for r in (first, second, third)}) == 1
    assert [r["phase"] for r in site.records(FIRST)] == [A.INDEPENDENT]


def test_a_draft_is_never_exported_or_given_to_another_reviewer(
        site, tmp_path, monkeypatch):  # noqa: F811
    b = site.browser().sign_in(FIRST)
    b.open("i07")
    secret = "ZQDRAFT a note that was never submitted"
    b.set({"notes": secret, "ui.later": "timing"})
    assert drafts(site)
    events = site.admin.events()
    assert not [e for e in events if secret.encode() in e["line"]]
    # The operator's export and status know no draft.
    from hosted_web_site import operator_tool

    cli = operator_tool()
    monkeypatch.setenv(cli.URL_ENV, site.dsn)
    out = tmp_path / "x" / "events.jsonl"
    out.parent.mkdir(mode=0o700)
    assert cli.main(["export", "--out", str(out)]) == 0
    assert secret not in out.read_text()
    assert secret not in site.browser().sign_in(SECOND).open("i07").text
