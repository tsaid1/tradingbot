"""The reviewers file's optional ``additional_items``: a short list of forms offered beyond the
first ``scope_sequences`` ideas, as ordinary forms. Absent or empty, nothing changes; listed, the
forms are offered, saved, revealed per post and counted apart. INVENTED demo bundle only."""

from __future__ import annotations

import re

import _review_modes as modes
import pytest
from streamlit.testing.v1 import AppTest
from test_adjudication import fill, review, save, valid
from test_adjudication_rubric import (
    APP,
    APPROVER,
    DEMO,
    REVIEWER,
    after_reading,
    answers_of,
    bundle,
    save_after,
    save_independent,
)

from tradingbot.runtime import adjudication as A
from tradingbot.runtime import adjudication_scenarios as SC

local = modes.local  # fixture
S14 = next(s for s in SC.SCENARIOS if s["n"] == 14)  # one post, two forms: s14a, s14b
HEADING = "Additional forms"


@pytest.fixture
def base(tmp_path):
    return DEMO.write_demo(tmp_path / "adjudication")


def configure(base, *, scope: int | None = None, additional=None, raw: str | None = None):
    """Edit the demo reviewers file: the scope, and an ``additional_items`` line (a list of ids,
    or ``raw`` TOML for the value)."""
    path = base / "reviewers.toml"
    text = path.read_text()
    if scope is not None:
        text = text.replace("scope_sequences = 40", f"scope_sequences = {scope}")
    if additional is not None or raw is not None:
        value = raw if raw is not None else "[" + ", ".join(f'"{i}"' for i in additional) + "]"
        text = text.replace("\n[[reviewer]]", f"additional_items = {value}\n\n[[reviewer]]", 1)
    path.write_text(text)
    return path


def session(role=A.REVIEWER, scope=1, additional=()):
    who = REVIEWER if role == A.REVIEWER else APPROVER
    return A.Session(who.login, who.name, role, A.FULL, False, scope, tuple(additional))


def run(monkeypatch, base, login, app=APP) -> AppTest:
    monkeypatch.setenv(A.DIR_ENV, str(base))
    monkeypatch.setenv(A.LOCAL_IDENTITY_ENV, login)
    modes.with_headers(monkeypatch)
    at = AppTest.from_file(str(app), default_timeout=120)
    at.run()
    assert not at.exception
    return at


def pick(at, item_id):
    return at.selectbox(key="aw_pick").set_value(item_id).run()


def texts(at) -> list[str]:
    return [str(e.value) for e in [*at.text, *at.code]]


def captions(at) -> list[str]:
    return [str(e.value) for e in at.caption]


def metric(at, label) -> list[str]:
    return [str(m.value) for m in at.metric if m.label == label]


def _walk(block):
    for child in block.children.values():
        yield child
        if hasattr(child, "children"):
            yield from _walk(child)


_ATTRS = ("key", "label", "options", "value", "help", "disabled", "placeholder", "body")
_CLOCK = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d+\+00:00|"
                    r"saved \d{4}-\d\d-\d\d at \d\d:\d\d UTC")


def page(at) -> list[tuple]:
    """Everything the page draws, in order: per element its kind and, where it has them, its
    key, label, options, value, tooltip, disabled state and text. This is what the "key absent =
    identical" comparison compares. A save's clock time (in the record and the notice) is
    replaced by ``<clock>``: two walks never save at the same instant."""
    out = []
    for el in _walk(at._tree):
        row = [type(el).__name__, getattr(el, "type", "")]
        for a in _ATTRS:
            try:
                v = getattr(el, a)
            except Exception:  # noqa: BLE001, S112 - an attribute this kind of element lacks
                continue
            if callable(v):
                continue
            if isinstance(v, list | tuple):
                v = tuple(map(repr, v))
            row.append((a, _CLOCK.sub("<clock>", repr(v))))
        out.append(tuple(row))
    return out


def stored(base) -> list[dict]:
    """The log's records without what a clock or the chain sets (``at``, ``prev_sha256``)."""
    v = A.read_annotations(base)
    assert v.ok, v.problem
    return [{k: x for k, x in r.items() if k not in ("at", "prev_sha256")} for r in v.records]


# ------------------------------------------------------------------ the reviewers file


@pytest.mark.parametrize("raw, expected", [
    (None, ()),
    ("[]", ()),
    ('["s14a", "I0123456789abcdef", "L0123456789abcdef"]',
     ("s14a", "I0123456789abcdef", "L0123456789abcdef")),
    ("[" + ", ".join(f'"x{k}"' for k in range(50)) + "]", tuple(f"x{k}" for k in range(50))),
])
def test_the_key_is_optional_and_carried(base, raw, expected):
    path = configure(base, raw=raw) if raw is not None else base / "reviewers.toml"
    reviewers = A.load_reviewers(path)
    assert reviewers.additional_items == expected
    assert reviewers.scope_sequences == 40 and len(reviewers.entries) == 3


@pytest.mark.parametrize("raw", [
    '["s14a", "s14a"]',  # duplicated
    '"s14a"',  # not a list
    "1",
    '{ a = "s14a" }',
    "[1]",  # wrong shape
    '[""]',
    '["bad id"]',
    '["-s14a"]',
    '["' + "x" * 65 + '"]',
    '[["s14a"]]',
    "[" + ", ".join(f'"x{k}"' for k in range(51)) + "]",  # more than 50
])
def test_a_malformed_list_makes_the_file_unreadable(base, raw):
    with pytest.raises(A.AdjudicationError, match="^REVIEWERS_FILE_UNREADABLE$"):
        A.load_reviewers(configure(base, raw=raw))


def test_existing_positional_constructions_keep_working():
    s = A.Session("a@example.invalid", "A", A.REVIEWER, A.FULL, False, 3)
    assert (s.scope, s.additional_items) == (3, ())
    r = A.Reviewers({}, False, 7)
    assert (r.scope_sequences, r.additional_items, r.remote_full_context_authorized) == (
        7, (), False)


# ------------------------------------------------------------------ an id not in the bundle


def test_an_unknown_id_fails_closed_in_the_runtime(base):
    b = bundle(base)
    bad = session(additional=("s14a", "zz99"))
    for call in (lambda: A.additional_forms(b, 1, bad.additional_items),
                 lambda: A.scope_ids(b, 1, bad.additional_items),
                 lambda: A.post_siblings(b, b.items["i07"], 1, bad.additional_items),
                 lambda: A.progress(b, [], bad),
                 lambda: A.mark_system_shown(base, b, bad, "i07"),
                 lambda: A.append_annotation(base, b, bad, item_id="i07", phase=A.INDEPENDENT,
                                             blind=True, reading={}, confidence="LOW")):
        with pytest.raises(A.AdjudicationError, match="^ADDITIONAL_ITEM_UNKNOWN$"):
            call()
    assert stored(base) == []


@pytest.mark.parametrize("login", [REVIEWER.login, APPROVER.login])
def test_an_unknown_id_refuses_the_session_before_any_form(monkeypatch, local, base, login):
    configure(base, scope=1, additional=["s14a", "zz99"])
    at = run(monkeypatch, base, login)
    assert [str(e.value) for e in at.error] == [
        "The reviewers file is refused (ADDITIONAL_ITEM_UNKNOWN)."]
    assert not at.selectbox and not at.button and not at.radio and not at.text_input
    assert "aw_bundle" not in at.session_state
    assert stored(base) == []


# ------------------------------------------------------------------ absent = as before


def drive(monkeypatch, base, app=APP, *, short=False) -> list:
    """A fixed walk through the page (a reviewer, then an approver) on the demo bundle: what
    every step draws. With the same reviewers file and log, the walk must draw the same.
    ``short``: the first page of each role and one form outside the first idea only."""
    shots = []
    b = bundle(base)
    at = run(monkeypatch, base, REVIEWER.login, app)
    shots.append(page(at))
    if short:
        shots.append(page(pick(at, "s14b")))
        at = run(monkeypatch, base, APPROVER.login, app)
        return [*shots, page(at), page(pick(at, "s14b"))]
    at = pick(at, "i07")
    shots.append(page(at))
    fill(at, "aw_i07_ind", valid(b, "i07"))
    shots.append(page(at))
    save(at, "aw_i07_ind", "MEDIUM")
    shots.append(page(at))
    ind = [r for r in A.read_annotations(base).records if r["phase"] == A.INDEPENDENT][-1]
    save(at, f"aw_i07_after_{ind['seq']}")
    shots.append(page(at))
    at.button(key="aw_next").click().run()
    shots.append(page(at))
    at.toggle(key="aw_blind").set_value(False).run()
    at = pick(at, "i08")
    shots.append(page(at))
    at = run(monkeypatch, base, APPROVER.login, app)
    shots.append(page(at))
    at = pick(at, "i07")
    shots.append(page(at))
    for decision in ("AMEND", "RETURN", "ACCEPT"):
        at.radio(key=next(w.key for w in at.radio if w.key.endswith("_decision"))).set_value(
            decision).run()
        shots.append(page(at))
    at.button(key=next(w.key for w in at.button if w.key.endswith("_save"))).click().run()
    shots.append(page(at))
    at.button(key="aw_next").click().run()
    shots.append(page(at))
    return shots


@pytest.mark.parametrize("scope, steps", [(1, 14), (40, 4)])
def test_absent_empty_or_already_in_scope_draws_and_saves_the_same(
        monkeypatch, local, tmp_path, scope, steps):
    """The same walk with the key absent, with ``additional_items = []`` and with a list that
    names only forms already in the scope: the same widgets (kind, key, label, options, value,
    tooltip) and strings at every step, and the same records."""
    runs = []
    for k, additional in enumerate((None, [], ["i07", "i08"])):
        base = DEMO.write_demo(tmp_path / f"run{k}")
        configure(base, scope=scope, additional=additional)
        runs.append((drive(monkeypatch, base, short=scope != 1), stored(base)))
    (shots, recs), *others = runs
    assert len(shots) == steps and sum(map(len, shots)) > 50 * steps
    assert scope != 1 or any(r["phase"] == A.APPROVAL for r in recs)
    assert not any(HEADING in str(row) for shot in shots for row in shot)
    for other_shots, other_recs in others:
        for n, (a, b) in enumerate(zip(shots, other_shots, strict=True)):
            assert a == b, n
        assert other_recs == recs


# ------------------------------------------------------------------ the forms offered


def test_listed_forms_are_offered_saved_and_not_doubled(base):
    b = bundle(base)
    plain, extra = session(), session(additional=("s14a", "s14b", "i07"))
    assert [i["item_id"] for i in A.additional_forms(b, 1, extra.additional_items)] == [
        "s14a", "s14b"]  # i07 is in the scope already: not doubled
    assert A.scope_ids(b, 1, extra.additional_items) == {"i07", "i08", "s14a", "s14b"}
    reading = answers_of(b, b.items["s14a"], S14["forms"][0])
    kw = {"item_id": "s14a", "phase": A.INDEPENDENT, "blind": True, "reading": reading,
          "confidence": S14["forms"][0]["confidence"], "flags": S14["forms"][0]["flags"],
          "notes": S14["forms"][0]["notes"]}
    with pytest.raises(A.AdjudicationError, match="OUTSIDE_SCOPE"):
        A.append_annotation(base, b, plain, **kw)
    rec = A.append_annotation(base, b, extra, **kw)
    assert rec["item_id"] == "s14a" and rec["phase"] == A.INDEPENDENT
    with pytest.raises(A.AdjudicationError, match="OUTSIDE_SCOPE"):  # neither scope nor list
        A.append_annotation(base, b, extra, item_id="i01", phase=A.INDEPENDENT, blind=True,
                            reading=valid(b, "i01"), confidence="LOW")
    p = A.progress(b, A.read_annotations(base).records, extra)
    assert (p.total, p.additional_total) == (2, 2) and list(p.status) == [
        "i07", "i08", "s14a", "s14b"]


@pytest.mark.parametrize("login", [REVIEWER.login, APPROVER.login])
def test_the_page_lists_them_under_one_neutral_heading(monkeypatch, local, base, login):
    configure(base, scope=1, additional=["s14b", "i07", "s14a"])
    at = run(monkeypatch, base, login)
    assert list(at.selectbox(key="aw_pick").options) == [
        "seqB 1: instruction", "seqB 2: instruction", "scs14 1: instruction",
        "scs14 2: instruction"]
    assert metric(at, "Items done") == ["0 of 2"] and metric(at, "Sequences completed") == [
        "0 of 1"]
    assert metric(at, HEADING) == ["0 of 2"]
    status = "open" if login == REVIEWER.login else "not_reviewed"
    assert f"{HEADING}\nscs14 1: instruction, {status}\nscs14 2: instruction, {status}" in \
        texts(at)
    assert "Scope: first 1 ideas (of 33)" in texts(at)
    at = pick(at, "s14a")
    assert f"scs14 1: instruction, {status}" in texts(at)
    shown = "\n".join(texts(at) + captions(at) + [str(e.value) for e in at.subheader])
    assert "addition" not in shown.replace(HEADING, "").lower().replace(
        "additional_actions", "")  # nothing says why; the form is an ordinary one
    assert "Form 1 of 2 for this post" in texts(at)


def _scenario_through_page(at, base, b, k):
    iid = SC.item_id(S14, k)
    at = pick(at, iid)
    f = S14["forms"][k]
    fill(at, f"aw_{iid}_ind", answers_of(b, b.items[iid], f))
    if f["flags"]:
        at.multiselect(key=f"aw_{iid}_ind_flags").set_value(f["flags"]).run()
    if f["notes"]:
        at.text_area(key=f"aw_{iid}_ind_notes").set_value(f["notes"]).run()
    return save(at, f"aw_{iid}_ind", f["confidence"])


def markers(base) -> list[str]:
    return [r["item_id"] for r in A.read_annotations(base).records
            if r["phase"] == A.SYSTEM_SHOWN]


def test_blind_first_holds_across_two_additional_forms_of_one_post(monkeypatch, local, base):
    configure(base, scope=1, additional=["s14a", "s14b"])
    b = bundle(base)
    at = run(monkeypatch, base, REVIEWER.login)
    system_line = "holds SHIPPED: NOT_DECIDED "
    at = _scenario_through_page(at, base, b, 0)
    assert "Saved INDEPENDENT as line 1." in [str(e.value) for e in at.success]
    assert any(c.startswith("Hidden until you save an independent reading") for c in
               captions(at))
    assert not any(t.startswith(system_line) for t in texts(at)) and markers(base) == []
    at = pick(at, "s14b")
    assert not any(t.startswith(system_line) for t in texts(at)) and markers(base) == []
    assert at.radio(key="aw_s14b_ind_component").value is None  # its own blind form
    at = _scenario_through_page(at, base, b, 1)
    assert any(t.startswith(system_line) for t in texts(at))  # now both are saved
    assert sorted(markers(base)) == ["s14a", "s14b"]  # once per form, as within the scope
    at = pick(at, "s14a")
    assert any(t.startswith(system_line) for t in texts(at))
    assert sorted(markers(base)) == ["s14a", "s14b"]  # not written again


def test_progress_counts_the_list_apart(base):
    b = bundle(base)
    s = session(additional=("s14a", "s14b"))
    base_line = A.progress(b, [], session())

    def now():
        p = A.progress(b, A.read_annotations(base).records, s)
        first_n = A.progress(b, A.read_annotations(base).records, session())
        assert (p.total, p.done, p.sequences, p.sequences_done) == (
            first_n.total, first_n.done, first_n.sequences, first_n.sequences_done)
        assert {k: v for k, v in p.status.items() if k in first_n.status} == first_n.status
        return p.additional_done, p.additional_total, p.status["s14a"], p.status["s14b"]

    assert (base_line.total, base_line.sequences) == (2, 1)
    assert now() == (0, 2, "OPEN", "OPEN")
    inds = save_independent(base, b, S14, variants=False)
    assert now() == (0, 2, "INDEPENDENT_SAVED", "INDEPENDENT_SAVED")
    save_after(base, b, {**S14, "forms": S14["forms"][:1]}, inds[:1])
    assert now() == (1, 2, "DONE", "INDEPENDENT_SAVED")
    save_after(base, b, S14, inds)  # both (a second reading of s14a: the newest counts)
    assert now() == (2, 2, "DONE", "DONE")
    review(base, "i07")
    p = A.progress(b, A.read_annotations(base).records, s)
    assert (p.done, p.total, p.sequences_done, p.additional_done) == (1, 2, 0, 2)
    approver = session(A.APPROVER, additional=("s14a", "s14b"))
    p = A.progress(b, A.read_annotations(base).records, approver)
    assert (p.additional_done, p.status["s14a"]) == (0, "AWAITING")


def test_an_approver_decides_an_additional_form(monkeypatch, local, base):
    configure(base, scope=1, additional=["s14a", "s14b"])
    b = bundle(base)
    inds = save_independent(base, b, S14, variants=False)
    save_after(base, b, S14, inds)
    at = pick(run(monkeypatch, base, APPROVER.login), "s14a")
    assert metric(at, HEADING) == ["0 of 2"]
    prefix = next(w.key for w in at.radio if w.key.endswith("_decision"))[:-len("_decision")]
    at.radio(key=f"{prefix}_decision").set_value("ACCEPT").run()
    at.button(key=f"{prefix}_save").click().run()
    assert not [str(e.value) for e in at.error]
    last = A.read_annotations(base).records[-1]
    assert (last["phase"], last["item_id"], last["decision"]) == (A.APPROVAL, "s14a", "ACCEPT")
    assert metric(at, HEADING) == ["1 of 2"] and metric(at, "Items done") == ["0 of 2"]


def test_next_reaches_the_list_after_the_scope_and_never_leaves_the_union(
        monkeypatch, local, base):
    configure(base, scope=1, additional=["s14a", "s14b"])
    union = {"i07", "i08", "s14a", "s14b"}
    at = run(monkeypatch, base, REVIEWER.login)
    seen = []
    for _ in range(6):
        at.button(key="aw_next").click().run()
        seen.append(at.selectbox(key="aw_pick").value)
    assert set(seen) <= union and seen[:4] == ["i08", "s14a", "s14b", "i07"]
    review(base, "i07")
    review(base, "i08")
    at = pick(run(monkeypatch, base, REVIEWER.login), "i08")
    at.button(key="aw_next").click().run()
    assert at.selectbox(key="aw_pick").value == "s14a"  # the scope is finished: the list
    b = bundle(base)
    save_after(base, b, S14, save_independent(base, b, S14, variants=False))
    at = pick(run(monkeypatch, base, REVIEWER.login), "s14a")
    at.button(key="aw_next").click().run()
    assert at.selectbox(key="aw_pick").value == "s14a"  # nothing open: it stays
    assert metric(at, HEADING) == ["2 of 2"] and metric(at, "Items done") == ["2 of 2"]


def test_a_later_form_whose_earlier_form_is_not_offered(monkeypatch, local, base):
    """``s14b`` listed alone: its earlier form ``s14a`` is neither in the scope nor in the list.
    The page shows its own evidence and context; no earlier position is assumed (there is
    none), its reveal follows its own independent reading, and only ``s14b`` is marked, as for
    a form whose sibling lies outside the scope."""
    configure(base, scope=1, additional=["s14b"])
    b = bundle(base)
    assert A.earlier_positions([], b, b.items["s14b"], REVIEWER.login) == []
    at = pick(run(monkeypatch, base, REVIEWER.login), "s14b")
    assert "Form 2 of 2 for this post" in texts(at)
    s01 = next(s for s in SC.SCENARIOS if s["n"] == 1)
    assert any(s01["text"] in t for t in texts(at)) and any(S14["text"] in t for t in texts(at))
    assert not any(t.startswith("Position after your reading of the previous action")
                   for t in texts(at))
    at = _scenario_through_page(at, base, b, 1)
    assert any(t.startswith("holds SHIPPED: NOT_DECIDED ") for t in texts(at))
    assert markers(base) == ["s14b"]
    assert not any(t.startswith("Label of the whole post") for t in texts(at))
    ind = A.read_annotations(base).records[0]
    _done, checks, ctx = A.check_reading(
        b, b.items["s14b"], A.clean_reading(after_reading(b, b.items["s14b"], ind["reading"]),
                                            b.items["s14b"], b),
        revealed=True, records=A.read_annotations(base).records, login=REVIEWER.login,
        confidence="HIGH")
    assert ctx.earlier_positions == () and "POSITION_CHAIN" not in checks.warnings
