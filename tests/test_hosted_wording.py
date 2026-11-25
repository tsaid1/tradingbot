"""The words of the hosted reviewer site (``hosted/reviewer_web/wording.py``): every question,
option, section, status and message a reviewer reads. New wording is DISPLAY ONLY: the same
answers entered through the page store byte-identical records, for every form of the demo
(``tests/hosted_wording_readings.json`` holds what the page of the base commit 537e155 stored)."""

from __future__ import annotations

import ast
import html
import json
import os
import re
from pathlib import Path

from hosted_pg import fresh_database
from hosted_web_site import (  # noqa: F401 - fixtures
    FIRST,
    SYSTEM_MARKS,
    SYSTEM_WORDS,
    Site,
    dsn,
    mark_system,
    scenario_reading,
    site,
    store,
)
from reviewer_web import forms as F
from reviewer_web import wording as W

from tradingbot.runtime import adjudication as A
from tradingbot.runtime import adjudication_rubric as R

HERE = Path(__file__).resolve().parent
READINGS = HERE / "hosted_wording_readings.json"
KEPT = ("reading", "field_verdicts", "flags", "confidence", "notes", "acknowledged_warnings",
        "blind", "phase")


def _order(site) -> list[str]:  # noqa: F811
    from reviewer_web import forms as F

    return list(F.scoped(site.bundle, site.reviewers[FIRST].session()))


def stored_by_the_page(site, pages: list | None = None) -> dict:  # noqa: F811
    """Every form of the demo answered through the pages: first every independent reading (in
    review order), then every reading after the reveal (the mapping questions answered YES, the
    verdicts as the page pre-fills them). The stored records, by "item phase"."""
    b = site.browser().sign_in(FIRST)
    order = _order(site)
    for item_id in order:
        if pages is not None:
            pages.append((item_id, "blind", b.open(item_id).text))
        b.review(item_id, after=False)
        if pages is not None:
            pages.append((item_id, "blind, saved", b.shown.text))
    for item_id in order:
        _reading, form = scenario_reading(site.bundle, item_id)
        shown = b.open(item_id)
        if pages is not None:
            pages.append((item_id, "after the reveal", shown.text))
        assert shown.value("_phase") == A.AFTER_REVEAL, item_id
        answers = {n: "YES" for n in R.MAPPING_FIELDS if n in shown.names}
        if answers:
            b.set(answers)
        done = b.save(confidence=form["confidence"], flags=form["flags"], notes=form["notes"])
        assert done.status == 200 and "Saved." in done.text, (item_id, done.visible()[:600])
    out = {}
    for r in site.records(FIRST, markers=False):
        out[f"{r['item_id']} {r['phase']}"] = {k: r.get(k) for k in KEPT}
    return out


def test_the_same_answers_store_the_same_records_as_the_base_commits_page(site):  # noqa: F811
    pages: list = []
    got = stored_by_the_page(site, pages)
    if os.environ.get("HOSTED_WORDING_WRITE"):  # run once on the base commit, never after
        READINGS.write_text(json.dumps(got, indent=1, sort_keys=True, ensure_ascii=False) + "\n")
    want = json.loads(READINGS.read_text())
    assert len(want) == 2 * len(_order(site))
    assert json.dumps(got, sort_keys=True) == json.dumps(want, sort_keys=True)
    # The same pages, read as a reviewer reads them: no field name or option code in the
    # reading flow, blind or after the reveal (the technical detail and the publisher's own
    # words aside).
    families = set(site.bundle.families) - {"OTHER", "UNKNOWN"}
    for item_id, when, text in pages:
        found = codes_in_reading_flow(text) - families
        assert not found, (item_id, when, sorted(found))


# ------------------------------------------------------------------ the reading flow


CODES = ({f for f in R.INSTRUCTION_FIELDS if "_" in f} | {"post_label", "month_named",
                                                          "contract_month", "stop_price"}
         | {c for f in R.RUBRIC for c, _ in f.values if not c.startswith(("<", "{"))}
         | set(R.BLOCKING_CODES) | set(R.WARNING_CODES) | set(A.VERDICTS) | set(A.FLAGS)
         | {R.NOT_APPLICABLE, R.NOT_STATED, R.CANNOT_DETERMINE, "VALUE", "INDEPENDENT",
            "AFTER_REVEAL", "BLOCKED", "REQUIRED_UNANSWERED"})


def codes_in_reading_flow(page: str) -> set[str]:
    """The field names and option codes a reviewer would READ on the page: the text of the
    answers column (not attributes, not the publisher's words quoted beside a number, not the
    technical detail of the system's reading behind its own disclosure)."""
    start = page.find('<div class="answers">')
    body = page[start:page.find('<div class="dock"', start)] if start >= 0 else ""
    body = re.sub(r'<details class="technical">.*?</details>', " ", body, flags=re.DOTALL)
    body = re.sub(r'<span class="context">.*?</span>', " ", body, flags=re.DOTALL)
    text = html.unescape(re.sub(r"<[^>]+>", " ", body))
    words = set(re.findall(r"\b[A-Za-z]+(?:_[A-Za-z]+)+\b|\b[A-Z]{2,}\b", text))
    return words & CODES


# ------------------------------------------------------------------ the wording itself


def _families(site) -> tuple[str, ...]:  # noqa: F811
    return tuple(site.bundle.families)


def test_every_question_and_option_the_form_can_show_has_its_words():
    for f in W.asked_fields():
        assert W.QUESTIONS.get(f), f
    families, sequences = ("GOLD", "CRUDE", "OTHER", "UNKNOWN"), ("scs01", "scs02")
    for f in W.asked_fields():
        if f in R.VALUE_SENTINELS:
            assert f in W.VALUE_CHOICE, f
            assert all(s in W.SENTINEL for s in R.VALUE_SENTINELS[f]), f
            continue
        if f in ("notes", "month_named"):
            continue
        codes = R.options(f, families, sequences) if f not in R.ROW_KEYS else \
            F.ROW_CHOICES[f]
        if f in ("flags", "confidence", "ambiguities"):
            codes = {"flags": A.FLAGS, "confidence": A.CONFIDENCE,
                     "ambiguities": R.AMBIGUITIES}[f]
        for c in codes:
            words = W.option(f, c)
            if f == "family" and c not in ("OTHER", "UNKNOWN"):
                assert words == c  # a market keeps its own name
            elif f == "idea" and c.startswith("SEQUENCE:"):
                assert words == "Another listed idea: " + c.split(":")[1]
            else:
                assert c in W.OPTIONS[f] and words and words != c, (f, c)
    # The further actions, the judgments, the derived fields, every rubric reason.
    for k in R.ADDITIONAL_KEYS:
        assert all(W.option(W.ADDITIONAL[k][1], c) != c
                   for c in {"timing": R.ADDITIONAL_TIMINGS, "action": R.ADDITIONAL_ACTIONS,
                             "side": R.SIDES, "family": ("OTHER",)}[k])
    assert set(W.VERDICTS) == set(A.VERDICTS)
    for code in R.FOLLOWER_IMPLICATIONS:
        assert W.option("follower_implication", code) != code
    assert set(W.BLOCKING) == set(R.BLOCKING_CODES) and set(W.WARNINGS) == set(R.WARNING_CODES)
    assert set(W.SECTIONS) == {g for g, _ in R.GROUPS}
    assert rubric_reasons() <= set(W.REASONS), rubric_reasons() - set(W.REASONS)
    assert set(F.STATUS_TEXT) == {F.INDEPENDENT_DUE, F.REVEAL_PENDING, F.AFTER_REVEAL_DUE,
                                  F.RETURNED, F.DONE}


def rubric_reasons() -> set[str]:
    """Every reason the rubric gives for a question it does not ask (read from its source)."""
    tree = ast.parse(Path(R.__file__).read_text())
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            out |= {k.value.value for k in node.keywords
                    if k.arg == "reason" and isinstance(k.value, ast.Constant)}
            if getattr(node.func, "id", "") in ("na", "pending") and len(node.args) > 1 and \
                    isinstance(node.args[1], ast.Constant):
                out.add(node.args[1].value)
    return out


def test_no_two_options_of_one_question_share_their_words():
    for f, options in W.OPTIONS.items():
        words = list(options.values())
        assert len(words) == len(set(words)), f
    for f in W.VALUE_CHOICE:
        shown = [W.VALUE_CHOICE[f][0], *(W.SENTINEL[s] for s in R.VALUE_SENTINELS[f])]
        assert len(shown) == len(set(shown)), f
    # The four states keep their own words, the same everywhere.
    four = (W.NOT_ANSWERED, W.POST_SILENT, W.UNSETTLED, W.DOES_NOT_APPLY)
    assert len(set(four)) == 4
    for f, options in W.OPTIONS.items():
        assert W.NOT_ANSWERED not in options.values() and W.DOES_NOT_APPLY not in \
            options.values(), f  # states, never options
        if R.NOT_STATED in options:
            # "doesn't say" is about this post, or about all the posts shown where the rubric
            # lets the earlier posts answer (the position before, its stop, the month).
            assert options[R.NOT_STATED] == (W.POSTS_SILENT if f in W.SILENT_IN_POSTS_SHOWN
                                             else W.POST_SILENT), f
        if R.CANNOT_DETERMINE in options:
            assert options[R.CANNOT_DETERMINE] == W.UNSETTLED, f


def test_the_owners_wording_is_used_as_given():
    assert W.QUESTIONS["effect"] == "What happened to the size or protection of the position?"
    assert W.QUESTIONS["quoted_series"] == "What do the quoted prices refer to?"
    assert W.QUESTIONS["traded_instrument"] == "What does the publisher say they traded?"
    assert W.HELP["traded_instrument"] == (
        "A gold futures chart tells you where the prices came from. The post may still leave "
        "the trade itself unspecified.")
    assert W.QUESTIONS["reference_series_right"] == "Is this the price series the post uses?"
    assert W.QUESTIONS["execution_instrument_right"] == (
        "Would trading this instrument follow the idea in the post?")
    assert W.refusal_summary(2, 0) == ("Your form hasn't been submitted. Two questions need an "
                                       "answer.")
    assert W.refusal_summary(1, 2) == ("Your form hasn't been submitted. One question needs an "
                                       "answer and two answers need a correction.")
    assert W.SAVED_READING_REVIEW == "Your reading is saved. Review the system's answers"
    assert W.SAVED_REVIEW_COMPLETE == "Review complete. Continue to the next form."
    assert W.EARLIER_POST.format(k=1, n=3) == "Earlier post · 1 of 3"
    assert W.FORM_OF_POST.format(k=1, n=2) == "Form 1 of 2 for this post"
    assert (W.POST_UNDER_REVIEW, W.VIEW_POST, W.BACK_TO_QUESTION, W.FOR_LATER) == (
        "Post you're reviewing", "View post", "Back to my question", "For later")
    assert W.state_draft("09:14") == "Draft kept at 09:14 UTC, not submitted"


def test_the_wording_table_of_the_documentation_is_generated_from_the_module():
    doc = HERE.parent / "docs" / "HOSTED_REVIEWER_WORDING.md"
    if os.environ.get("HOSTED_WORDING_WRITE_DOC"):
        doc.write_text(W.wording_table_markdown())
    assert doc.read_text() == W.wording_table_markdown()


# ------------------------------------------------------------------ blind first, byte for byte


def _other_marks(base: Path) -> None:
    """Another system reading of the post s14: other values everywhere the system has one."""
    mark_system(base)
    path = base / "bundle" / "items.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    for row in rows:
        if row["item_id"] in SYSTEM_MARKS:
            row["ai"]["instrument_as_read"] = "ZQOTHER instrument"
            row["ai"]["side"] = "LONG" if row["ai"].get("side") == "SHORT" else "SHORT"
            row["mapping"] = {"reference_series": "ZQOTHER-REF", "approved": False,
                              "proposed_execution_instrument": "", "basis_treatment": "X"}
    path.write_text("".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n"
                            for r in rows))
    manifest = base / "bundle" / "manifest.json"
    data = json.loads(manifest.read_text())
    data["bundle_id"] = A.bundle_digest(base / "bundle")
    manifest.write_text(json.dumps(data, indent=1) + "\n")


def _blind_pages(site) -> list[str]:  # noqa: F811
    """Every response a reviewer gets for the post s14 before both its forms have a reading:
    the forms opened, filled through round trips (and as a script asks for them), marked "For
    later", a refused submission, the first form saved, the dashboard."""
    b = site.browser().sign_in(FIRST)
    seen = [b.open("s14a").text, b.open("s14b").text]
    b.open("s14a")
    reading, form = scenario_reading(site.bundle, "s14a")
    seen.append(b.set({"ui.later": "field-timing", "ui.last": "field-action"}).text)
    seen.append(b.press_save().text)  # refused: nothing answered yet
    b.fill(reading)
    seen.append(b.shown.text)
    seen.append(b.post("/form/s14a/state", b.shown.data(), headers={"X-Fragment": "form"})
                .get_data(as_text=True))
    seen.append(b.save(confidence=form["confidence"], flags=form["flags"],
                       notes=form["notes"]).text)
    seen += [b.open("s14a").text, b.open("s14b").text, b.get("/").get_data(as_text=True)]
    b.open("s14b")
    seen.append(b.set({"component": "ACTION_RECORDED_HERE", "ui.later": "field-side"}).text)
    seen.append(b.press_save().text)
    return seen


def _same(text: str, site) -> str:  # noqa: F811
    """A response without what differs between two sessions or two bundles by construction:
    the session's token, the page's key, the bundle's id (a digest of the whole bundle), the
    time a record was stored."""
    text = text.replace(site.bundle.bundle_id, "<bundle>")
    text = re.sub(r'name="_csrf" value="[0-9a-f]{64}"', 'name="_csrf" value="<csrf>"', text)
    # When a record was stored (the store's own clock: the three sites save minutes apart).
    text = re.sub(r"\b\d{1,2} [A-Z][a-z]{2} \d{4} at \d{2}:\d{2} UTC", "<when>", text)
    text = re.sub(r"\b\d{4}-\d{2}-\d{2}( at |T)\d{2}:\d{2}", "<when>", text)
    text = re.sub(r"\b\d{2}:\d{2} UTC", "<when>", text)
    return re.sub(r'name="_key" value="[^"]+"', 'name="_key" value="<key>"', text)


def test_blind_responses_are_the_same_bytes_whatever_the_system_read(tmp_path, store, dsn):  # noqa: F811
    """Three databases that differ ONLY in the system's reading (the demo's, the marked one,
    another one): before every form of the post has a reading, every response of every new
    surface (cards, form counts, marks for later, the refusal summary, the saved message) is the
    same, byte for byte."""
    from reviewer_web.store import AdminStore

    sites = [Site(tmp_path / "marked", store, dsn)]
    for name, kw in (("plain", {"mark": False}), ("other", {"mark": False,
                                                            "edit": _other_marks})):
        other = fresh_database()
        admin = AdminStore(other)
        admin.init_schema()
        sites.append(Site(tmp_path / name, admin, other, **kw))
    assert len({s.bundle.bundle_id for s in sites}) == 3
    pages = [[_same(t, s) for t in _blind_pages(s)] for s in sites]
    for k, (a, b, c) in enumerate(zip(*pages, strict=True)):
        if not a == b == c:
            import difflib
            other = b if a != b else c
            raise AssertionError((k, "".join(list(difflib.unified_diff(
                a.splitlines(True), other.splitlines(True), n=0))[:12])))
        for word in (*SYSTEM_WORDS, "ZQOTHER"):
            assert word not in a, (k, word)
