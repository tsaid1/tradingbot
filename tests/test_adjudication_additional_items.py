"""The reviewers file's optional ``additional_items``: a short list of forms offered beyond the
first ``scope_sequences`` ideas, as ordinary forms. Absent or empty, nothing changes; listed, the
forms are offered, saved, revealed per post and counted apart. INVENTED demo bundle only."""

from __future__ import annotations

import re

import _review_modes as modes
import pytest
from streamlit.testing.v1 import AppTest
from test_adjudication import VALID, fill, review, rewrite, save, valid
from test_adjudication_rubric import (
    APP,
    APPROVER,
    DEMO,
    REVIEWER,
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
        text = re.sub(r"additional_items = .*\n", "", text)  # a list changed: replaced
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
    '["s14a\\n"]',  # a trailing newline: the whole string must have the shape
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


PARTIAL = "The reviewers file is refused (ADDITIONAL_ITEM_PARTIAL_POST)."


def refused(at, message=PARTIAL) -> None:
    assert [str(e.value) for e in at.error] == [message]
    assert not at.selectbox and not at.button and not at.radio and not at.text_input
    assert "aw_bundle" not in at.session_state


def test_b1_one_form_of_a_two_form_post_refuses_the_session(monkeypatch, local, base):
    """The review's B1 start: ``["s14a"]`` alone (``s14b`` neither in the scope nor listed)."""
    configure(base, scope=1, additional=["s14a"])
    for login in (REVIEWER.login, APPROVER.login):
        refused(run(monkeypatch, base, login))
    b = bundle(base)
    half = session(additional=("s14a",))
    for call in (lambda: A.additional_forms(b, 1, ("s14a",)),
                 lambda: A.progress(b, [], half),
                 lambda: A.mark_system_shown(base, b, half, "s14a"),
                 lambda: A.append_annotation(
                     base, b, half, item_id="s14a", phase=A.INDEPENDENT, blind=True,
                     reading=answers_of(b, b.items["s14a"], S14["forms"][0]),
                     confidence="HIGH")):
        with pytest.raises(A.AdjudicationError, match="^ADDITIONAL_ITEM_PARTIAL_POST$"):
            call()
    assert stored(base) == []
    # Path A and B of the review: the owner then lists both. Nothing was revealed: both forms
    # start blind, and neither is shown before both independent readings are saved.
    configure(base, additional=["s14a", "s14b"])
    at = run(monkeypatch, base, REVIEWER.login)
    for iid in ("s14a", "s14b"):
        at = pick(at, iid)
        assert at.radio(key=f"aw_{iid}_ind_component").value is None
        assert not any(t.startswith("holds SHIPPED") for t in texts(at))
    assert markers(base) == []


def test_b1_a_list_changed_mid_review_refuses_and_writes_nothing(monkeypatch, local, base):
    """The variant: both listed, ``s14a`` saved, then the owner shrinks the list to
    ``["s14a"]`` between two requests: the next request is refused and nothing more of the
    post is written (no marker, no reading); the same through the proxy path."""
    configure(base, scope=1, additional=["s14a", "s14b"])
    b = bundle(base)
    at = _scenario_through_page(run(monkeypatch, base, REVIEWER.login), base, b, 0)
    before = stored(base)
    assert [r["phase"] for r in before] == [A.INDEPENDENT] and markers(base) == []
    configure(base, additional=["s14a"])
    at.run()
    refused(at)
    at = run(monkeypatch, base, REVIEWER.login)
    refused(at)
    shrunk = session(additional=("s14a",))
    with pytest.raises(A.AdjudicationError, match="^ADDITIONAL_ITEM_PARTIAL_POST$"):
        A.mark_system_shown(base, b, shrunk, "s14a")
    with pytest.raises(A.AdjudicationError, match="^ADDITIONAL_ITEM_PARTIAL_POST$"):
        save_after(base, b, {**S14, "forms": S14["forms"][:1]}, [before[0]], session=shrunk)
    assert stored(base) == before
    configure(base, additional=["s14b"])  # the other half: refused too
    refused(run(monkeypatch, base, REVIEWER.login))
    modes.published(monkeypatch)  # PUBLISHED mode: the proxy's login
    monkeypatch.delenv(A.LOCAL_IDENTITY_ENV, raising=False)
    modes.with_headers(monkeypatch, **{"Tailscale-User-Login": REVIEWER.login})
    at = AppTest.from_file(str(APP), default_timeout=120)
    at.run()
    refused(at)
    assert stored(base) == before


def _move(base, item_id, **fields) -> A.Bundle:
    """An invented variant of the demo bundle: one item's fields changed, resealed."""
    rewrite(base / "bundle" / "items.jsonl", lambda rows: [
        {**r, **fields} if r["item_id"] == item_id else r for r in rows])
    return bundle(base)


def partial(b, scope, additional) -> bool:
    try:
        A.additional_forms(b, scope, additional)
    except A.AdjudicationError as exc:
        assert exc.code == "ADDITIONAL_ITEM_PARTIAL_POST"
        return True
    return False


def test_a_form_of_a_post_partly_inside_the_scope_cannot_be_added(monkeypatch, local, base):
    """An invented bundle edit puts ``s14b`` in the next sequence: with ``s14a`` in the scope,
    listing ``s14b`` (alone or with ``s14a``) is refused; the in-scope ``s14a`` alone is a
    harmless entry."""
    b = _move(base, "s14b", sequence_id="scs15")
    order = [sid for sid, _ in A.ordered(b)]
    scope = order.index("scs14") + 1
    assert order[scope] == "scs15"
    assert partial(b, scope, ("s14b",)) and partial(b, scope, ("s14a", "s14b"))
    assert not partial(b, scope, ("s14a",)) and A.additional_forms(b, scope, ("s14a",)) == []
    assert not partial(b, scope - 1, ("s14a", "s14b"))  # both outside: the whole post listed
    configure(base, scope=scope, additional=["s14b"])
    refused(run(monkeypatch, base, REVIEWER.login))
    assert stored(base) == []


def test_sf1_a_sibling_outside_the_scope_of_a_revealed_form_cannot_be_added(
        monkeypatch, local, base):
    """The second review's SF1: the label-only ``i09`` (no sequence) moved onto ``i07``'s
    post ``p07``. ``i07`` is read and revealed at once (base behaviour: ``i09`` is not
    offered); the owner then lists ``i09``: refused, and nothing more is written."""
    b = _move(base, "i09", post_ref="p07")
    assert A.post_forms(b, b.items["i09"]) == [b.items["i09"]]
    configure(base, scope=1)
    at = pick(run(monkeypatch, base, REVIEWER.login), "i07")
    fill(at, "aw_i07_ind", valid(b, "i07"))
    save(at, "aw_i07_ind", "MEDIUM")
    assert markers(base) == ["i07"]  # revealed at once, as at the base
    before = stored(base)
    configure(base, additional=["i09"])
    at.run()
    refused(at)
    refused(run(monkeypatch, base, REVIEWER.login))
    listed = session(additional=("i09",))
    for call in (lambda: A.mark_system_shown(base, b, listed, "i07"),
                 lambda: A.append_annotation(base, b, listed, item_id="i09",
                                             phase=A.INDEPENDENT, blind=True,
                                             reading=valid(b, "i09"), confidence="LOW")):
        with pytest.raises(A.AdjudicationError, match="^ADDITIONAL_ITEM_PARTIAL_POST$"):
            call()
    assert stored(base) == before and "i09" not in markers(base)
    configure(base, additional=["i07"])  # an in-scope id alone: harmless, not doubled
    at = run(monkeypatch, base, REVIEWER.login)
    assert not at.error and metric(at, HEADING) == []


def test_the_rule_covers_every_kind_every_listed_id_and_every_scope(base):
    """The label-only ``i09`` (no sequence) moved onto post ``s14``: the post is ``s14a``,
    ``s14b`` and ``i09``. Every one must be listed, whatever its kind or sequence, whichever
    listed id is the partial one, at any scope that leaves the post outside."""
    b = _move(base, "i09", post_ref="s14")
    whole = ("s14a", "s14b", "i09")
    for scope in (1, 2, 5, 16):
        assert not partial(b, scope, whole)
        assert [i["item_id"] for i in A.additional_forms(b, scope, whole)] == [
            "s14a", "s14b", "i09"]
        assert partial(b, scope, ("s14a", "s14b"))  # the label-only, no-sequence one missing
        assert partial(b, scope, ("i09",))  # a label-only listed id: its post missing
        assert partial(b, scope, ("s22a", "s22b", "s14a", "s14b"))  # not the first listed id
        assert partial(b, scope, ("s22a", "s22b", "i09", "s01a"))
        assert not partial(b, scope, ("s22a", "s22b", *whole))
    plain = bundle(DEMO.write_demo(base.parent / "plain"))
    for scope in (2, 5, 16):  # the demo as written: two-form posts at scopes other than 1
        assert partial(plain, scope, ("s14a",)) and partial(plain, scope, ("s30b",))
        assert partial(plain, scope, ("s22a", "s22b", "s14b"))
        assert not partial(plain, scope, ("s14a", "s14b", "s22a", "s22b"))


def _forge_marker(base, b, item_id) -> None:
    """A SYSTEM_SHOWN marker for ONE form, as a log written while only that form of the post
    was offered would hold it (the state B1 left behind)."""
    def make(_v):
        return {"at": "2026-11-20T10:00:00+00:00", "bundle_id": b.bundle_id, "item_id": item_id,
                "annotator": {"login": REVIEWER.login, "name": REVIEWER.name,
                              "role": A.REVIEWER, "access": A.FULL},
                "phase": A.SYSTEM_SHOWN, "blind": False, "reading": {}, "field_verdicts": {},
                "flags": [], "confidence": "", "notes": "", "refers_to": None,
                "decision": None, "rubric": A.RUBRIC, "acknowledged_warnings": []}
    A.append_chained(A.annotations_path(base), "annotation log", A.ANNOTATION_SCHEMA, make)


def test_the_save_check_holds_blind_first_over_listed_siblings(base):
    """Through ``append_annotation`` directly, with both forms of ``s14`` listed: no reading
    after the reveal of one form before the other's independent reading, and no
    "independent" reading of a form whose listed sibling's system reading was shown."""
    b = bundle(base)
    both = session(additional=("s14a", "s14b"))
    [ind] = save_independent(base, b, {**S14, "forms": S14["forms"][:1]}, session=both,
                             variants=False)
    with pytest.raises(A.AdjudicationError, match="^BLIND_FIRST_PER_POST$"):
        save_after(base, b, {**S14, "forms": S14["forms"][:1]}, [ind], session=both)
    _forge_marker(base, b, "s14a")
    f = S14["forms"][1]
    with pytest.raises(A.AdjudicationError, match="^SYSTEM_READING_ALREADY_SEEN$"):
        A.append_annotation(base, b, both, item_id="s14b", phase=A.INDEPENDENT, blind=True,
                            reading=answers_of(b, b.items["s14b"], f), flags=f["flags"],
                            notes=f["notes"], confidence=f["confidence"])
    assert [r["phase"] for r in stored(base)] == [A.INDEPENDENT, A.SYSTEM_SHOWN]


def test_a_held_selector_text_keeps_its_form_when_the_list_grows(monkeypatch, local, base):
    """An additional form is named by its place in its own sequence, never by its place in
    the list: ``i03`` is "seqA 3", alone or after ``i01`` joins the list before it."""
    configure(base, scope=1, additional=["i03"])
    at = pick(run(monkeypatch, base, REVIEWER.login), "i03")
    box = at.selectbox(key="aw_pick")
    assert list(box.options) == ["seqB 1: instruction", "seqB 2: instruction",
                                 "seqA 3: instruction"]
    configure(base, additional=["i03", "i01"])
    at.run()
    box = at.selectbox(key="aw_pick")
    assert list(box.options) == ["seqB 1: instruction", "seqB 2: instruction",
                                 "seqA 1: instruction", "seqA 3: instruction"]
    assert box.value == "i03" and box.options[box.index] == "seqA 3: instruction"
    assert "seqA 3: instruction, open" in texts(at)


def test_a_label_only_additional_form(monkeypatch, local, base):
    configure(base, scope=1, additional=["i02"])
    b = bundle(base)
    at = run(monkeypatch, base, REVIEWER.login)
    assert "seqA 2: label_only" in list(at.selectbox(key="aw_pick").options)
    assert f"{HEADING}\nseqA 2: label_only, open" in texts(at)
    assert metric(at, HEADING) == ["0 of 1"]
    at = pick(at, "i02")
    assert "Label-only form: the label of the whole post" in texts(at)
    fill(at, "aw_i02_ind", valid(b, "i02"))
    save(at, "aw_i02_ind", "MEDIUM")
    [ind] = [r for r in stored(base) if r["phase"] == A.INDEPENDENT]
    assert ind["item_id"] == "i02"
    p = A.progress(b, A.read_annotations(base).records, session(additional=("i02",)))
    assert (p.additional_done, p.additional_total, p.status["i02"]) == (
        0, 1, "INDEPENDENT_SAVED")
    save(at, f"aw_i02_after_{ind['seq']}")
    assert [r["phase"] for r in stored(base) if r["item_id"] == "i02"][-1] == A.AFTER_REVEAL
    assert metric(at, HEADING) == ["1 of 1"] and metric(at, "Items done") == ["0 of 2"]
    assert f"{HEADING}\nseqA 2: label_only, done" in texts(at)
    assert VALID["i02"]["post_label"] == "COMMENTARY"


def test_the_key_below_a_reviewer_entry_is_refused_not_ignored(base):
    path = base / "reviewers.toml"
    text = path.read_text()
    for value in ('["s14a"]', '["zz99"]', "[]"):
        path.write_text(text + f"additional_items = {value}\n")
        with pytest.raises(A.AdjudicationError, match="^REVIEWERS_FILE_ENTRY$"):
            A.load_reviewers(path)
    path.write_text(text + "scope_sequences = 3\n")  # any other key: loaded exactly as before
    assert A.load_reviewers(path).scope_sequences == 40
