"""Where the signed-in reviewer stands: the dashboard and the sidebar of the hosted site, as plain
data for the templates (DECIDED 40 section 2; DESIGN_UX section 3).

Everything is derived from the reviewer's OWN CONFIRMED saves (the stored records the store hands
over) and from the bundle's structure that a blind form already shows (the review order, the
posts and their times, the number of forms of a post). Nothing here reads the system's reading:
no value, no proposal, no market it chose, no count of its actions. So the dashboard and the
sidebar are the same, byte for byte, whatever the system read, until the reveal. A draft changes
nothing but its ``draft`` flag.

Pure: no Flask, no database. The resume rule is ``forms.next_step``; restricted to one sequence
it is ``sequence_next``.
"""

from __future__ import annotations

from tradingbot.runtime import adjudication as A
from tradingbot.runtime import adjudication_rubric as R

from . import forms as F
from . import wording as W

NOT_STARTED = "NOT_STARTED"
IN_PROGRESS = "IN_PROGRESS"
AWAITING_ASSESSMENT = "AWAITING_ASSESSMENT"
COMPLETE = "COMPLETE"
RETURNED = "RETURNED"
CODES = (NOT_STARTED, IN_PROGRESS, AWAITING_ASSESSMENT, COMPLETE, RETURNED)
# What remains to be done (``remaining_step``).
SAVE_YOUR_READING = "SAVE_YOUR_READING"
COMPLETE_OTHER_FORM = "COMPLETE_OTHER_FORM"
ASSESS_SYSTEM_ANSWERS = "ASSESS_SYSTEM_ANSWERS"
CORRECT_RETURNED = "CORRECT_RETURNED"
NEXT_FORM = "NEXT_FORM"
ALL_DONE = "ALL_DONE"
NO_SEQUENCE = "-"  # the id of the group of the forms of no sequence (in a URL too)

# The owner's phrases (DECIDED 40), in the words of ``wording`` (one source for every text).
TEXT = {
    **W.STATUS,
    SAVE_YOUR_READING: W.STEP_TEXT[SAVE_YOUR_READING],
    COMPLETE_OTHER_FORM: W.STEP_TEXT[COMPLETE_OTHER_FORM],
    ASSESS_SYSTEM_ANSWERS: W.STEP_TEXT[ASSESS_SYSTEM_ANSWERS],
    CORRECT_RETURNED: W.STEP_TEXT[CORRECT_RETURNED],
    NEXT_FORM: W.STEP_TEXT[NEXT_FORM],
    ALL_DONE: W.STEP_TEXT[ALL_DONE],
    "START": "Start",
    "CONTINUE": "Continue",
    "NO_SEQUENCE": W.NO_SEQUENCE,
}
_BY_STATE = {F.INDEPENDENT_DUE: NOT_STARTED, F.REVEAL_PENDING: IN_PROGRESS,
             F.AFTER_REVEAL_DUE: AWAITING_ASSESSMENT, F.DONE: COMPLETE, F.RETURNED: RETURNED}


def _place(bundle: A.Bundle, item_id: str) -> tuple[int, int]:
    """(k, n): "Form k of n" of the item's post (every form of the post counts, as the form's
    own header counts them)."""
    forms = A.post_forms(bundle, bundle.items[item_id])
    k = next((j for j, f in enumerate(forms, 1) if f["item_id"] == item_id), 1)
    return k, len(forms)


class _View:
    """The reviewer's gates of every form in scope, computed once."""

    def __init__(self, bundle: A.Bundle, session: A.Session, records: list[dict], drafts=()):
        self.bundle, self.session, self.records = bundle, session, records
        self.items = F.scoped(bundle, session)
        self.gates = {i: F.gate(bundle, session, item, records) for i, item in self.items.items()}
        self.drafts = set(drafts)

    def code(self, item_id: str) -> str:
        return _BY_STATE[self.gates[item_id].state]

    def waiting_for(self, item_id: str) -> dict | None:
        if self.gates[item_id].state != F.REVEAL_PENDING:
            return None
        other = F._sibling(self.bundle, self.session, self.records, self.items[item_id])
        k, n = _place(self.bundle, other)
        return {"item_id": other, "k": k, "n": n}

    def next_in(self, item_ids: list[str]) -> str | None:
        """The resume rule restricted to these forms: a returned one first, then the first that
        is not complete; for one whose post waits for another form, that form."""
        back = next((i for i in item_ids if self.code(i) == RETURNED), None)
        if back is not None:
            return back
        for i in item_ids:
            if self.code(i) == IN_PROGRESS:
                return self.waiting_for(i)["item_id"]
            if self.code(i) != COMPLETE:
                return i
        return None


def remaining_step(bundle: A.Bundle, session: A.Session, records: list[dict],
                   item_id: str | None = None, drafts=(), *, _view: _View | None = None) -> dict:
    """What remains for this form (or, without ``item_id``, for the reviewer): ``code`` and its
    details. After an independent submission of a form whose post has another form without a
    reading, COMPLETE_OTHER_FORM names that form (``item_id``, ``k``, ``n``)."""
    view = _view or _View(bundle, session, records, drafts)
    if item_id is not None and item_id in view.items:
        code = view.code(item_id)
        if code == NOT_STARTED:
            return {"code": SAVE_YOUR_READING, "item_id": item_id}
        if code == IN_PROGRESS:
            return {"code": COMPLETE_OTHER_FORM, **view.waiting_for(item_id)}
        if code == AWAITING_ASSESSMENT:
            return {"code": ASSESS_SYSTEM_ANSWERS, "item_id": item_id}
        if code == RETURNED:
            return {"code": CORRECT_RETURNED, "item_id": item_id}
    step = F.next_step(bundle, session, records)
    return {"code": NEXT_FORM, "item_id": step} if step else {"code": ALL_DONE}


def form_status(bundle: A.Bundle, session: A.Session, records: list[dict], item_id: str,
                drafts=(), *, _view: _View | None = None) -> dict:
    """One form's status for the signed-in reviewer: ``code`` (NOT_STARTED, IN_PROGRESS,
    AWAITING_ASSESSMENT, COMPLETE, RETURNED), ``waiting_for`` (the form of the same post that
    still needs a reading: ``item_id``, ``k``, ``n``), ``draft`` (a draft is kept; it changes
    nothing else) and ``step`` (the code of ``remaining_step``)."""
    view = _view or _View(bundle, session, records, drafts)
    return {"code": view.code(item_id), "text": TEXT[view.code(item_id)],
            "waiting_for": view.waiting_for(item_id), "draft": item_id in view.drafts,
            "step": remaining_step(bundle, session, records, item_id, _view=view)["code"]}


def _read_markets(view: _View, ids: list[str]) -> str:
    """The markets THIS reviewer read in these forms: the ``family`` answers of their own stored
    independent readings, in words ("TLT", "TLT, GOLD"); "" before they have saved one. Nothing
    of the system's reading: the dashboard stays the same whatever the system read."""
    found: list[str] = []
    for i in ids:
        rec = F._latest(view.records, i, view.bundle, A.INDEPENDENT, view.session.login)
        family = (rec or {}).get("reading", {}).get("family", "")
        if family and family != R.NOT_APPLICABLE and family not in found:
            found.append(family)
    return ", ".join(W.option("family", f) for f in found)


def _rows(view: _View, current: str | None) -> list[dict]:
    out = []
    number = 0
    for sid, items in A.ordered(view.bundle, view.session.scope):
        number += 1 if sid else 0
        ids = [i["item_id"] for i in items]
        codes = [view.code(i) for i in ids]
        done = codes.count(COMPLETE)
        status = NOT_STARTED if all(c == NOT_STARTED for c in codes) else (
            COMPLETE if done == len(ids) else IN_PROGRESS)
        posts: dict[str, list[str]] = {}
        for i in ids:
            posts.setdefault(view.items[i]["post_ref"], []).append(i)
        nxt = view.next_in(ids)
        begun = status != NOT_STARTED or any(i in view.drafts for i in ids)
        start_or_continue = "CONTINUE" if begun else "START"
        ordered_posts = sorted(posts.items(), key=lambda p: (
            view.bundle.posts[p[0]]["published_at"], p[0]))
        out.append({
            "id": sid or NO_SEQUENCE, "label": sid or TEXT["NO_SEQUENCE"], "status": status,
            "number": number if sid else None,
            "title": W.SEQUENCE.format(n=number) if sid else TEXT["NO_SEQUENCE"],
            "read": _read_markets(view, ids),
            "status_text": TEXT[status], "done": done, "total": len(ids),
            "remaining": len(ids) - done, "returned": codes.count(RETURNED),
            "attention": codes.count(AWAITING_ASSESSMENT), "next_item": nxt,
            # A kept draft is work begun: its button continues (the status stays what the
            # confirmed saves make it; the draft is a second fact beside it).
            "drafts": sum(i in view.drafts for i in ids),
            "button": None if nxt is None else start_or_continue,
            "button_text": None if nxt is None else TEXT[start_or_continue],
            "active": current in ids,
            "posts": [{"post_ref": ref, "when": view.bundle.posts[ref]["published_at"],
                       "number": p, "when_text": W.post_time(view.bundle.posts[ref][
                           "published_at"]),
                       "forms": [{"item_id": i, **dict(zip(("k", "n"), _place(view.bundle, i))),
                                  "status": view.code(i), "status_text": TEXT[view.code(i)],
                                  "draft": i in view.drafts, "current": i == current,
                                  "label": W.form_label(p, *_place(view.bundle, i))}
                                 for i in forms]}
                      for p, (ref, forms) in enumerate(ordered_posts, 1)]})
    return out


def _named(rows: list[dict], item_id: str | None) -> dict:
    """Where a form stands in the rows, in words: "Sequence 3, post 2, form 1 of 2"."""
    for row in rows:
        for post in row["posts"]:
            for f in post["forms"]:
                if f["item_id"] == item_id:
                    return {"place": W.place(row["title"], post["number"], f["k"], f["n"]),
                            "code": f["status"], "draft": f["draft"]}
    return {"place": "", "code": "", "draft": False}


def _places(rows: list[dict]) -> dict[str, str]:
    """Every form's place in words, by item ("Sequence 1, post 2"): never its id."""
    return {f["item_id"]: W.place(row["title"], post["number"], f["k"], f["n"])
            for row in rows for post in row["posts"] for f in post["forms"]}


def dashboard(bundle: A.Bundle, session: A.Session, records: list[dict], drafts=()) -> dict:
    """One row per assigned sequence, in review order (the forms of no sequence last): its idea
    as the selector names it, its status, completed forms of total, remaining, the forms
    returned and awaiting an assessment, the form its button opens (``next_item``) and the
    button (START while nothing is stored, else CONTINUE; None when complete); expanded, its
    posts in time order and each post's forms. ``next``: the global resume rule."""
    view = _View(bundle, session, records, drafts)
    rows = _rows(view, None)
    codes = [view.code(i) for i in view.items]
    nxt = F.next_step(bundle, session, records)
    named = _named(rows, nxt)
    return {"sequences": rows, "next": nxt, "next_place": named["place"],
            "next_text": W.next_button("IN_PROGRESS" if named["draft"] else named["code"],
                                       named["place"]) if nxt else "",
            "places": _places(rows),
            "totals": {
        "done": codes.count(COMPLETE), "total": len(codes),
        "remaining": len(codes) - codes.count(COMPLETE), "returned": codes.count(RETURNED),
        "attention": codes.count(AWAITING_ASSESSMENT),
        "sequences": sum(r["id"] != NO_SEQUENCE for r in rows),
        "sequences_done": sum(r["id"] != NO_SEQUENCE and r["status"] == COMPLETE for r in rows)}}


def sidebar(bundle: A.Bundle, session: A.Session, records: list[dict], item_id: str | None,
            drafts=()) -> dict:
    """The dashboard's rows, with the sequence of ``item_id`` (else of the next step) ``active``
    (expanded) and ``item_id`` marked ``current``."""
    view = _View(bundle, session, records, drafts)
    current = item_id if item_id in view.items else None
    rows = _rows(view, current or F.next_step(bundle, session, records))
    if current is None:
        for row in rows:
            for post in row["posts"]:
                for form in post["forms"]:
                    form["current"] = False
    active = next((r["id"] for r in rows if r["active"]), None)
    return {"sequences": rows, "active": active, "current": current,
            "place": _named(rows, current)["place"] if current else "", "places": _places(rows)}


def sequence_next(bundle: A.Bundle, session: A.Session, records: list[dict],
                  sequence_id: str) -> str | None | bool:
    """The form a sequence's Start or Continue opens (the resume rule restricted to it: a
    returned form first); None when it is complete; False when the reviewer has no such
    sequence."""
    view = _View(bundle, session, records)
    for sid, items in A.ordered(bundle, session.scope):
        if (sid or NO_SEQUENCE) == sequence_id:
            return view.next_in([i["item_id"] for i in items])
    return False
