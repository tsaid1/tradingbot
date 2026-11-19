"""After a save the reviewer's form is built again with flags, confidence and notes empty (they
belong to the next save). The page then listed "Save refuses CONFIDENCE ... confidence is
required" beside a disabled Save, with the confirmation out of sight at its top: a save that had
succeeded read as a save refused (a reviewer saved one reading twice, 2026-11-19). And on a real
bundle every choice made the page hash the whole bundle again, for seconds during which the
choice showed as made while the page still said it was missing.

Nothing here changes what Save requires, and no confidence is ever chosen for the reviewer.
INVENTED demo bundle only."""

from __future__ import annotations

import json
import os

import _review_modes as modes
import pytest
from test_adjudication import fill, save
from test_adjudication_rubric import (
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


def in_order(block):
    """Every element of the page, in the order it is drawn."""
    for child in block.children.values():
        yield child
        if hasattr(child, "children"):
            yield from in_order(child)


def notice(at) -> tuple[str, str]:
    """(kind of box, text) of the one "SAVED" notice of the form."""
    [found] = [(k, t) for k in ("success", "info", "warning") for t in shown(at, k)
               if t.startswith("SAVED")]
    return found


def posts_shown(at) -> bool:
    return any(SCENARIO["text"] in t for t in shown(at, "text"))


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
    kind, note = notice(at)
    assert kind == "info" and "Second step" in note  # not a green "done": one save is still due
    assert f"independent reading of this form is line {ind['seq']} of the log" in note
    assert f"saved {ind['at'][:10]} at {ind['at'][11:16]} UTC with confidence {first}." in note
    # It stands directly above the confidence, and stays true once a confidence is chosen.
    drawn = list(in_order(at.main))
    k = next(n for n, e in enumerate(drawn) if str(getattr(e, "value", "")) == note)
    assert getattr(drawn[k + 1], "key", None) == f"{after}_confidence"
    at.radio(key=f"{after}_confidence").set_value("LOW").run()
    assert notice(at) == (kind, note) and "is empty" not in note and "group H" not in note
    at.radio(key=f"{after}_confidence").set_value(None).run()
    assert not [t for t in shown(at, "error") if "Save refuses" in t]
    assert "The next save needs CONFIDENCE (confidence): confidence is required" in shown(
        at, "info")
    fill(at, after, after_reading(b, b.items[ITEM], ind["reading"]))
    save(at, after, second)
    [done] = [r for r in records(base) if r["phase"] == A.AFTER_REVEAL]
    assert (done["confidence"], done["refers_to"]) == (second, ind["seq"])
    assert f"Saved AFTER_REVEAL as line {done['seq']}." in shown(at, "success")  # the top, as before
    again = f"aw_{ITEM}_after_{done['seq']}"  # built again, under the new line's number
    assert at.radio(key=f"{again}_confidence").value is None  # never carried over
    assert at.button(key=f"{again}_save").disabled  # the same checks: a new save needs one
    kind, note = notice(at)
    assert kind == "success" and "Nothing more is needed here" in note
    assert f"reading of this form after the reveal is line {done['seq']} of the log" in note
    assert f"with confidence {second}." in note
    drawn = list(in_order(at.main))
    k = next(n for n, e in enumerate(drawn) if str(getattr(e, "value", "")) == note)
    assert getattr(drawn[k + 1], "key", None) == f"{again}_confidence"
    # A contradiction the reviewer introduces while changing the reading is still a refusal.
    at.selectbox(key=f"{again}_effect").set_value("CLOSES").run()
    assert [t for t in shown(at, "error") if t.startswith("Save refuses ACTION_EFFECT")]
    assert at.button(key=f"{again}_save").disabled
    at.selectbox(key=f"{again}_effect").set_value("OPENS").run()
    assert not [t for t in shown(at, "error") if "Save refuses" in t]
    assert not [t for t in shown(at, "error") if "Save refuses" in t]
    assert "The next save needs CONFIDENCE (confidence): confidence is required" in shown(
        at, "info")
    # A changed reading saves again only with a confidence chosen for it; the newest line counts.
    at.radio(key=f"{again}_confidence").set_value(first).run()
    assert not at.button(key=f"{again}_save").disabled
    at.button(key=f"{again}_save").click().run()
    last = [r for r in records(base) if r["phase"] == A.AFTER_REVEAL][-1]
    assert (last["confidence"], last["seq"] > done["seq"]) == (first, True)
    assert len([r for r in records(base) if r["phase"] != A.SYSTEM_SHOWN]) == 3
    # Three saved lines of ONE item are one ground-truth row, not three: the newest reading after
    # the reveal, with the blind reading it followed kept beside it, untouched.
    [row] = [r for r in A.resolve(records(base), b) if r["item_id"] == ITEM]
    assert row["source_seqs"] == {"independent": ind["seq"], "after_reveal": last["seq"],
                                  "approval": None}
    assert (row["status"], row["confidence"], row["blind"]) == ("PROVISIONAL", first, True)
    assert row["independent_reading"] == ind["reading"]
    [mine] = row["all_readings"]  # one entry per reviewer, whatever the number of saves
    assert mine["source_seqs"] == {"independent": ind["seq"], "after_reveal": last["seq"]}
    path, manifest = A.freeze(base)
    frozen = [json.loads(x) for x in path.read_text().splitlines()]
    assert [r["item_id"] for r in frozen].count(ITEM) == 1 and manifest["bundle_id"] == b.bundle_id
    assert [r["seq"] for r in records(base) if r["phase"] == A.INDEPENDENT] == [ind["seq"]]


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


def test_a_bundle_that_changed_under_a_session_refuses_the_save_and_the_freeze(
        monkeypatch, local, base):
    """The session holds a validated bundle and the form is ready to save. A file of the bundle
    then changes: the click on Save writes nothing, the page refuses, and the freeze (which
    hashes the whole bundle itself, every time) refuses too."""
    b = bundle(base)
    at = pick(app(monkeypatch, base, REVIEWER.login), ITEM)
    fill(at, IND, answers_of(b, b.items[ITEM], SCENARIO["forms"][0]))
    at.radio(key=f"{IND}_confidence").set_value("HIGH").run()
    assert not at.button(key=f"{IND}_save").disabled and "aw_bundle" in at.session_state
    posts = base / "bundle" / "posts.jsonl"
    posts.write_bytes(posts.read_bytes() + b"\n")
    posts.chmod(0o600)
    at.button(key=f"{IND}_save").click().run()
    assert records(base) == []  # nothing was saved against a bundle that no longer verifies
    assert shown(at, "error") == ["The adjudication bundle is refused (BUNDLE_ID_MISMATCH)."]
    assert "aw_bundle" not in at.session_state and not list(at.button)
    with pytest.raises(A.AdjudicationError, match="BUNDLE_ID_MISMATCH"):
        A.freeze(base)


def test_the_notice_says_returned_when_the_approver_sent_the_reading_back(
        monkeypatch, local, base):
    at, b = independent(monkeypatch, base, "HIGH")
    [ind] = [r for r in records(base) if r["phase"] == A.INDEPENDENT]
    after = f"aw_{ITEM}_after_{ind['seq']}"
    fill(at, after, after_reading(b, b.items[ITEM], ind["reading"]))
    save(at, after, "MEDIUM")
    [done] = [r for r in records(base) if r["phase"] == A.AFTER_REVEAL]
    back = A.append_annotation(base, b, APPROVER, item_id=ITEM, phase=A.APPROVAL, blind=False,
                               reading={}, refers_to=done["seq"], outcome="RETURN")
    at.run()
    kind, note = notice(at)
    assert kind == "warning" and note.startswith("SAVED, and RETURNED by the approver.")
    assert f"sent it back (line {back['seq']})" in note
    assert "Nothing more is needed" not in note and f"line {done['seq']} of the log" in note


def test_a_label_only_form_gets_the_same_notices_in_words_that_fit_it(monkeypatch, local, base):
    b = bundle(base)
    item = next(i for i in b.items.values() if i["kind"] == "LABEL_ONLY")
    prefix = f"aw_{item['item_id']}_ind"
    at = pick(app(monkeypatch, base, REVIEWER.login), item["item_id"])
    at.selectbox(key=f"{prefix}_post_label").set_value("COMMENTARY").run()
    save(at, prefix, "LOW")
    [ind] = [r for r in records(base) if r["phase"] == A.INDEPENDENT]
    kind, note = notice(at)
    assert kind == "info" and "with confidence LOW." in note and "group H" not in note
    save(at, f"aw_{item['item_id']}_after_{ind['seq']}", "HIGH")
    kind, note = notice(at)
    assert kind == "success" and "with confidence HIGH." in note
    assert [r["confidence"] for r in records(base, markers=False)] == ["LOW", "HIGH"]


def counted(monkeypatch) -> list[str]:
    """Every call of the bundle load, by the access it was asked for."""
    calls: list[str] = []
    load = A.load_bundle
    monkeypatch.setattr(A, "load_bundle", lambda path, access: calls.append(access) or load(
        path, access))
    return calls


def set_entry(base, **changes) -> None:
    """Edit the invented reviewer's entry of the reviewers file (what the owner may do while
    sessions are open); ``remove=True`` deletes it."""
    path = base / "reviewers.toml"
    entry = ('[[reviewer]]\nlogin = "reviewer@example.invalid"\nname = "Reviewer Example"\n'
             'role = "REVIEWER"\naccess = "FULL"\n')
    text = path.read_text()
    assert entry in text
    path.write_text(text.replace(entry, "" if changes.get("remove") else entry.replace(
        'access = "FULL"', f'access = "{changes["access"]}"')))


def test_a_session_that_loses_full_access_loses_the_bundle_it_held(monkeypatch, local, base):
    calls = counted(monkeypatch)
    at = pick(app(monkeypatch, base, REVIEWER.login), ITEM)
    assert posts_shown(at) and "aw_bundle" in at.session_state and calls == [A.FULL]
    set_entry(base, access="RESTRICTED")  # the owner lowers the access under the open session
    at.run()
    assert not posts_shown(at) and "aw_bundle" not in at.session_state
    assert calls == [A.FULL, A.RESTRICTED]
    assert any("RESTRICTED access" in t for t in shown(at, "text"))
    at.run()
    assert not posts_shown(at) and calls == [A.FULL, A.RESTRICTED, A.RESTRICTED]


@pytest.mark.parametrize("refusal", ["not listed", "folder"])
def test_a_session_that_is_refused_holds_no_bundle(monkeypatch, local, base, refusal):
    at = pick(app(monkeypatch, base, REVIEWER.login), ITEM)
    assert posts_shown(at) and "aw_bundle" in at.session_state
    if refusal == "not listed":
        set_entry(base, remove=True)
    else:
        base.chmod(0o755)
    try:
        at.run()
        assert len(shown(at, "error")) == 1 and not posts_shown(at)
        assert shown(at, "error")[0] == "Not authorized." or "folder is refused" in shown(
            at, "error")[0]
        assert "aw_bundle" not in at.session_state
    finally:
        base.chmod(0o700)


def test_two_sessions_each_validate_and_hold_their_own_bundle(monkeypatch, local, base):
    calls = counted(monkeypatch)
    one = app(monkeypatch, base, REVIEWER.login)
    two = app(monkeypatch, base, REVIEWER.login)
    assert calls == [A.FULL, A.FULL]
    assert one.session_state["aw_bundle"][1] is not two.session_state["aw_bundle"][1]


def _edit_in_place(path):
    """Change one byte, keep the size, put the modification time back."""
    info, data = path.stat(), path.read_bytes()
    k = data.index(b"long")
    path.write_bytes(data[:k] + b"lung" + data[k + 4:])
    os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))
    assert (path.stat().st_size, path.stat().st_mtime_ns) == (info.st_size, info.st_mtime_ns)


CHANGES = {
    "a file made readable by others": lambda b: (b / "posts.jsonl").chmod(0o644),
    "the manifest made readable by others": lambda b: (b / "manifest.json").chmod(0o644),
    "a second link to a file": lambda b: os.link(b / "posts.jsonl", b.parent / "second-link"),
    "an empty dot-folder": lambda b: (b / ".hidden").mkdir(),
    "an edit in place, same size, time put back": lambda b: _edit_in_place(b / "posts.jsonl"),
    "a file added to the images": lambda b: (b / "media" / "extra.png").write_bytes(b"x"),
}


@pytest.mark.parametrize("change", CHANGES)
def test_what_the_loader_refused_at_every_run_is_still_refused_at_the_next_run(
        monkeypatch, local, base, change):
    """Each of these was refused at the very next run when every run loaded the bundle. With the
    bundle held by the session it still is: the stamp sees the change, the load refuses."""
    at = pick(app(monkeypatch, base, REVIEWER.login), ITEM)
    assert posts_shown(at) and "aw_bundle" in at.session_state
    CHANGES[change](base / "bundle")
    try:
        at.run()
        [error] = shown(at, "error")
        assert error.startswith("The adjudication bundle is refused ("), error
        assert not posts_shown(at) and "aw_bundle" not in at.session_state
    finally:
        (base / "bundle" / "posts.jsonl").chmod(0o600)
        (base / "bundle" / "manifest.json").chmod(0o600)


def test_a_change_to_an_image_or_during_the_load_validates_again(monkeypatch, local, base):
    calls = counted(monkeypatch)
    at = app(monkeypatch, base, REVIEWER.login)
    image = next((base / "bundle" / "media").iterdir())
    info = image.stat()
    os.utime(image, ns=(info.st_atime_ns, info.st_mtime_ns + 1_000_000_000))
    at.run()
    assert calls == [A.FULL, A.FULL] and not at.exception  # the images are covered
    at.run()
    assert calls == [A.FULL, A.FULL]
    # A file changes WHILE a load runs: the stamp was taken before it, so the next run loads again.
    load, posts = A.load_bundle, base / "bundle" / "posts.jsonl"

    def load_then_touch(path, access):
        out = load(path, access)
        now = posts.stat()
        os.utime(posts, ns=(now.st_atime_ns, now.st_mtime_ns + 1_000_000_000))
        return out

    monkeypatch.setattr(A, "load_bundle", load_then_touch)
    os.utime(image, ns=(info.st_atime_ns, info.st_mtime_ns + 2_000_000_000))
    at.run()
    assert len(calls) == 3
    monkeypatch.setattr(A, "load_bundle", load)
    at.run()
    assert len(calls) == 4  # the change made during the load was not missed
    at.run()
    assert len(calls) == 4


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
