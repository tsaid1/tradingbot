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

from . import forms as F

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

# The owner's phrases (DECIDED 40); INTERFACE has the last word on every text a reviewer reads.
TEXT = {
    NOT_STARTED: "Not started",
    IN_PROGRESS: "In progress",
    AWAITING_ASSESSMENT: "Awaiting your assessment",
    COMPLETE: "Complete",
    RETURNED: "Returned for changes",
    SAVE_YOUR_READING: "Save your own reading of this form.",
    COMPLETE_OTHER_FORM: ("Your reading is saved. Another form of this post must be completed "
                          "before the system's answers are shown: form {k} of {n}."),
    ASSESS_SYSTEM_ANSWERS: "Your reading is saved. Review the system's answers.",
    CORRECT_RETURNED: "This reading was returned for changes. Correct it and save it again.",
    NEXT_FORM: "Review complete. Continue to the next form.",
    ALL_DONE: "Review complete. Every form assigned to you is done.",
    "START": "Start",
    "CONTINUE": "Continue",
    "NO_SEQUENCE": "Posts of no sequence",
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


def _rows(view: _View, current: str | None) -> list[dict]:
    out = []
    for sid, items in A.ordered(view.bundle, view.session.scope):
        ids = [i["item_id"] for i in items]
        codes = [view.code(i) for i in ids]
        done = codes.count(COMPLETE)
        status = NOT_STARTED if all(c == NOT_STARTED for c in codes) else (
            COMPLETE if done == len(ids) else IN_PROGRESS)
        posts: dict[str, list[str]] = {}
        for i in ids:
            posts.setdefault(view.items[i]["post_ref"], []).append(i)
        nxt = view.next_in(ids)
        out.append({
            "id": sid or NO_SEQUENCE, "label": sid or TEXT["NO_SEQUENCE"], "status": status,
            "status_text": TEXT[status], "done": done, "total": len(ids),
            "remaining": len(ids) - done, "returned": codes.count(RETURNED),
            "attention": codes.count(AWAITING_ASSESSMENT), "next_item": nxt,
            "button": None if nxt is None else "START" if status == NOT_STARTED else "CONTINUE",
            "button_text": None if nxt is None else TEXT["START" if status == NOT_STARTED
                                                         else "CONTINUE"],
            "active": current in ids,
            "posts": [{"post_ref": ref, "when": view.bundle.posts[ref]["published_at"],
                       "forms": [{"item_id": i, **dict(zip(("k", "n"), _place(view.bundle, i))),
                                  "status": view.code(i), "status_text": TEXT[view.code(i)],
                                  "draft": i in view.drafts, "current": i == current}
                                 for i in forms]}
                      for ref, forms in sorted(posts.items(), key=lambda p: (
                          view.bundle.posts[p[0]]["published_at"], p[0]))]})
    return out


def dashboard(bundle: A.Bundle, session: A.Session, records: list[dict], drafts=()) -> dict:
    """One row per assigned sequence, in review order (the forms of no sequence last): its idea
    as the selector names it, its status, completed forms of total, remaining, the forms
    returned and awaiting an assessment, the form its button opens (``next_item``) and the
    button (START while nothing is stored, else CONTINUE; None when complete); expanded, its
    posts in time order and each post's forms. ``next``: the global resume rule."""
    view = _View(bundle, session, records, drafts)
    rows = _rows(view, None)
    codes = [view.code(i) for i in view.items]
    return {"sequences": rows, "next": F.next_step(bundle, session, records), "totals": {
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
    return {"sequences": rows, "active": active, "current": current}


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
