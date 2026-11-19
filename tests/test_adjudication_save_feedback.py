"""After a save the reviewer's form is built again with flags, confidence and notes empty (they
belong to the next save). The page then listed "Save refuses CONFIDENCE ... confidence is
required" beside a disabled Save, with the confirmation out of sight at its top: a save that had
succeeded read as a save refused (a reviewer saved one reading twice, 2026-11-19). And on a real
bundle every choice made the page hash the whole bundle again, for seconds during which the
choice showed as made while the page still said it was missing.

Nothing here changes what Save requires, and no confidence is ever chosen for the reviewer.
INVENTED demo bundle only."""

from __future__ import annotations

import os

import _review_modes as modes
import pytest
from test_adjudication import fill, save
from test_adjudication_rubric import (
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
from tradingbot.runtime import adjudication_scenarios as SC

local = modes.local  # fixture
SCENARIO = next(s for s in SC.SCENARIOS if s["n"] == 1)
ITEM = SC.item_id(SCENARIO, 0)
IND = f"aw_{ITEM}_ind"


@pytest.fixture
def base(tmp_path):
    return DEMO.write_demo(tmp_path / "adjudication")


def shown(at, kind) -> list[str]:
    return [str(e.value) for e in getattr(at, kind)]


def independent(monkeypatch, base, confidence):
    """Scenario 1 answered through the form and saved as the independent reading."""
    b = bundle(base)
    at = pick(app(monkeypatch, base, REVIEWER.login), ITEM)
    fill(at, IND, answers_of(b, b.items[ITEM], SCENARIO["forms"][0]))
    return save(at, IND, confidence), b


def test_a_first_save_is_still_refused_without_a_confidence(monkeypatch, local, base):
    b = bundle(base)
    at = pick(app(monkeypatch, base, REVIEWER.login), ITEM)
    fill(at, IND, answers_of(b, b.items[ITEM], SCENARIO["forms"][0]))
    assert at.radio(key=f"{IND}_confidence").value is None  # none is chosen for the reviewer
    assert "Save refuses CONFIDENCE (confidence): confidence is required" in shown(at, "error")
    assert at.button(key=f"{IND}_save").disabled
    assert not [t for t in shown(at, "success") if t.startswith("SAVED")]
    assert not [t for t in shown(at, "info") if "The next save needs" in t]
    assert records(base) == []


@pytest.mark.parametrize("first, second", [("HIGH", "LOW"), ("MEDIUM", "HIGH"), ("LOW", "MEDIUM")])
def test_each_confidence_is_saved_as_chosen_and_the_page_says_so_beside_the_confidence(
        monkeypatch, local, base, first, second):
    at, b = independent(monkeypatch, base, first)
    [ind] = [r for r in records(base) if r["phase"] == A.INDEPENDENT]
    assert ind["confidence"] == first  # the exact value chosen
    after = f"aw_{ITEM}_after_{ind['seq']}"
    # The reveal form: built anew. Its confidence is empty, Save waits for it, and the page says
    # that the independent reading IS saved and that this is the second step: not a refusal.
    assert at.radio(key=f"{after}_confidence").value is None
    assert at.button(key=f"{after}_save").disabled
    [note] = [t for t in shown(at, "success") if t.startswith("SAVED")]
    assert f"independent reading of this form is line {ind['seq']} of the log" in note
    assert f"with confidence {first}." in note and ind["at"][11:16] in note
    assert not [t for t in shown(at, "error") if "Save refuses" in t]
    assert "The next save needs CONFIDENCE (confidence): confidence is required" in shown(
        at, "info")
    fill(at, after, after_reading(b, b.items[ITEM], ind["reading"]))
    save(at, after, second)
    [done] = [r for r in records(base) if r["phase"] == A.AFTER_REVEAL]
    assert (done["confidence"], done["refers_to"]) == (second, ind["seq"])
    again = f"aw_{ITEM}_after_{done['seq']}"  # built again, under the new line's number
    assert at.radio(key=f"{again}_confidence").value is None  # never carried over
    assert at.button(key=f"{again}_save").disabled  # the same checks: a new save needs one
    [note] = [t for t in shown(at, "success") if t.startswith("SAVED")]
    assert f"reading of this form after the reveal is line {done['seq']} of the log" in note
    assert f"with confidence {second}." in note and "This form is done" in note
    assert not [t for t in shown(at, "error") if "Save refuses" in t]
    assert "The next save needs CONFIDENCE (confidence): confidence is required" in shown(
        at, "info")
    assert f"Saved AFTER_REVEAL as line {done['seq']}." in shown(at, "success")  # the top, as before
    # A changed reading saves again only with a confidence chosen for it; the newest line counts.
    at.radio(key=f"{again}_confidence").set_value(first).run()
    assert not at.button(key=f"{again}_save").disabled
    at.button(key=f"{again}_save").click().run()
    last = [r for r in records(base) if r["phase"] == A.AFTER_REVEAL][-1]
    assert (last["confidence"], last["seq"] > done["seq"]) == (first, True)
    assert len([r for r in records(base) if r["phase"] != A.SYSTEM_SHOWN]) == 3


def test_a_full_session_validates_the_bundle_once_and_again_when_a_file_changes(
        monkeypatch, local, base):
    """Every run of the page used to read and hash the whole bundle. Counted here: the loads and
    the hashes, over the runs of one session."""
    calls = {"load": 0, "digest": 0}
    load, digest = A.load_bundle, A.bundle_digest

    def counted_load(path, access):
        calls["load"] += 1
        return load(path, access)

    def counted_digest(root):
        calls["digest"] += 1
        return digest(root)

    monkeypatch.setattr(A, "load_bundle", counted_load)
    monkeypatch.setattr(A, "bundle_digest", counted_digest)
    at = app(monkeypatch, base, REVIEWER.login)
    assert calls == {"load": 1, "digest": 1}
    at = pick(at, ITEM)
    at.selectbox(key=f"{IND}_timing").set_value("CURRENT").run()
    at.selectbox(key=f"{IND}_action").set_value("OPEN").run()
    assert calls == {"load": 1, "digest": 1}  # three more runs, no further load
    assert at.selectbox(key=f"{IND}_action").value == "OPEN"
    # A file of the bundle changes on disk (here: its modification time): validated again.
    posts = base / "bundle" / "posts.jsonl"
    info = posts.stat()
    os.utime(posts, ns=(info.st_atime_ns, info.st_mtime_ns + 1_000_000_000))
    at.run()
    assert calls == {"load": 2, "digest": 2} and not at.exception
    at.run()
    assert calls == {"load": 2, "digest": 2}
    # A changed file that no longer matches the manifest is refused, and nothing is kept.
    posts.write_bytes(posts.read_bytes() + b"\n")
    posts.chmod(0o600)
    at.run()
    assert shown(at, "error") == ["The adjudication bundle is refused (BUNDLE_ID_MISMATCH)."]
    assert "aw_bundle" not in at.session_state
    at.run()
    assert shown(at, "error") == ["The adjudication bundle is refused (BUNDLE_ID_MISMATCH)."]
    assert calls["load"] == 4  # refused again at every run: no bundle is held


def test_a_restricted_session_loads_at_every_run_and_holds_nothing(monkeypatch, local, base):
    calls = []
    load = A.load_bundle
    monkeypatch.setattr(A, "load_bundle", lambda path, access: calls.append(access) or load(
        path, access))
    path = base / "reviewers.toml"
    path.write_text(path.read_text().replace(
        'login = "reviewer@example.invalid"\nname = "Reviewer Example"\nrole = "REVIEWER"\n'
        'access = "FULL"', 'login = "reviewer@example.invalid"\nname = "Reviewer Example"\n'
        'role = "REVIEWER"\naccess = "RESTRICTED"'))
    at = app(monkeypatch, base, REVIEWER.login)
    at.run()
    assert calls == [A.RESTRICTED, A.RESTRICTED] and "aw_bundle" not in at.session_state
    assert any("RESTRICTED access" in t for t in shown(at, "text"))
