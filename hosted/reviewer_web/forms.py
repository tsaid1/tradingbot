"""The reviewer's workflow and form of the hosted site, as plain data for the templates.

Everything here is computed from the rubric (``adjudication_rubric``) and the core
(``adjudication``): which fields are asked, derived, pending or not applicable, the options
offered, the verdicts pre-filled from the comparison, the price table's rows, the blocking checks
and warnings. No rule is written a second time: this module asks the rubric and turns its answer
into what a page shows and what a save stores. It reproduces the reviewer part of the Streamlit
workspace (``dashboard/reviewer/adjudication_app.py``: ``_reviewer``, ``_form``, ``_inputs``,
``_price_table``, ``_extras``, ``_issues``, ``_saved_notice``) on a server that keeps no state
between requests: what a form holds is what the page sent, else what the stored record holds.

Pure: no Flask, no database. ``posted`` is anything with ``get`` and ``getlist`` (the request's
form), or None for a first render.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime

from tradingbot.runtime import adjudication as A
from tradingbot.runtime import adjudication_rubric as R

from . import wording as W

VALUE = W.VALUE
NOT_STATED_TEXT = W.POST_SILENT
UNANSWERED_TEXT = W.NOT_ANSWERED
NOT_READ_TEXT = "The system didn't read this number"
RADIO_UP_TO = 8  # options: radio buttons up to this many, a list box beyond
REPEAT_TABLE_CHOICES_FROM = 4  # kept for the record layer's tests; the table needs no repeat now
ROW_CHOICES = {"role": R.TABLE_ROLES, "exactness": R.ROW_EXACTNESS, "contract_month": R.ROW_MONTHS}
# The two questions asked after the reveal: field -> (the mapping value it is about, what the
# system proposes, the reviewer's own answer shown beside it and how it is introduced, the
# sentence when nothing was proposed).
PROPOSALS = {
    "reference_series_right": (
        "reference_series", "The system's price series for this post",
        ("quoted_series", "You answered that the quoted prices refer to"),
        "The system proposed no price series for this post, so there is nothing to judge."),
    "execution_instrument_right": (
        "proposed_execution_instrument", "The instrument the system would trade",
        ("traded_instrument", "You answered that the publisher traded"),
        "The system proposed no instrument for this post, so there is nothing to judge."),
}
ANSWER_TEXT = {"YES": "Yes", "NO": "No", "UNSURE": "Unsure"}
# Where one form stands for one reviewer (``step_state``), and how the workspace words it.
INDEPENDENT_DUE = "INDEPENDENT_DUE"  # no reading yet: the independent form
REVEAL_PENDING = "REVEAL_PENDING"  # independent reading saved; a form of the post still has none
AFTER_REVEAL_DUE = "AFTER_REVEAL_DUE"  # the system's reading may be shown; the second save is due
RETURNED = "RETURNED"  # the approver sent the reviewer's latest reading back
DONE = "DONE"  # a reading after the reveal is saved
STATUS_TEXT = {k: W.STATUS[v] for k, v in W.STEP_STATUS.items()}
STATUS_CODE = dict(W.STEP_STATUS)  # the page's state -> progress's code (for the badge)
# What a refusal of the core means for the reviewer, in a calm sentence.
REFUSALS = W.REFUSALS


def ident(text: str) -> str:
    """An element id, as the app's ``ident`` filter makes one."""
    return re.sub(r"[^A-Za-z0-9_-]", "-", str(text))



# ------------------------------------------------------------------ the workflow (resume, reveal)


def scoped(bundle: A.Bundle, session: A.Session) -> dict[str, dict]:
    """The items this reviewer may open, by id, in review order. Nothing else exists for them."""
    return {i["item_id"]: i for _, items in A.ordered(bundle, session.scope) for i in items}


@dataclass(frozen=True)
class Gate:
    """Where one form stands for one reviewer, from the stored records alone (``_reviewer``)."""

    ind: dict | None  # the latest independent reading
    after: dict | None  # the latest reading after the reveal
    waiting: tuple[str, ...]  # forms of the post still without an independent reading
    seen: bool  # a marker on ANY form of the post, or a record that is not a blind reading
    marked: bool  # the SYSTEM_SHOWN marker of this form and of every form of its post is stored
    returned: dict | None = None  # the approver's RETURN of ``after``, while it stands

    @property
    def open(self) -> bool:
        """The system's reading may be revealed (it is shown only once ``marked``)."""
        return not self.waiting or self.seen

    @property
    def state(self) -> str:
        if self.returned is not None:
            return RETURNED
        if self.after is not None:
            return DONE
        if self.open:
            return AFTER_REVEAL_DUE
        return REVEAL_PENDING if self.ind is not None else INDEPENDENT_DUE


def _latest(records, item_id, bundle, phase, login):
    found = [r for r in A._of(records, item_id, bundle.bundle_id, phase, login)
             if phase == A.SYSTEM_SHOWN or r.get("rubric") == A.RUBRIC]
    return found[-1] if found else None


def gate(bundle: A.Bundle, session: A.Session, item: dict, records: list[dict]) -> Gate:
    iid, me, bid = item["item_id"], session.login, bundle.bundle_id
    siblings = A.post_siblings(bundle, item, session.scope)
    new = A.rubric2(records)
    waiting = tuple(s for s in siblings if not A._of(new, s, bid, A.INDEPENDENT, me))
    # The system's reading of a post is shown for all its forms at once. So a form counts as
    # seen when ANY form of its post carries this reviewer's marker: a form that enters the
    # scope after its post was revealed (the scope was raised) can have no independent reading
    # any more (the core refuses one), and goes straight to the reading after the reveal,
    # stored as not blind.
    post = [i for i, x in bundle.items.items() if x["post_ref"] == item["post_ref"]]
    seen = any(A._of(records, s, bid, A.SYSTEM_SHOWN, me) for s in post) or any(
        r.get("rubric") != A.RUBRIC or r["phase"] != A.INDEPENDENT
        for r in A._of(records, iid, bid, login=me) if r["phase"] != A.SYSTEM_SHOWN)
    marked = all(A._of(records, s, bid, A.SYSTEM_SHOWN, me) for s in siblings)
    after = _latest(records, iid, bundle, A.AFTER_REVEAL, me)
    return Gate(_latest(records, iid, bundle, A.INDEPENDENT, me), after, waiting, seen, marked,
                returned(bundle, item, records, after))


def returned(bundle: A.Bundle, item: dict, records: list[dict], after: dict | None):
    """The approver's RETURN of this reviewer's latest reading after the reveal, while it stands
    (``_reviewer``). The store hands a reviewer the approver's RETURN records of their own
    readings beside their own records: this is the one place that reads a record of another
    login. A later save by the reviewer ends it (the approval then refers to an older record),
    and so does the line the operator tool stores when the approver took the return back: the
    latest approval of the item is then that line, whose decision is not RETURN."""
    appr = A.effective_approval(A.rubric2(records), item["item_id"], bundle) \
        if after is not None else None
    return appr if appr is not None and A._outcome(appr) == "RETURN" and appr[
        "refers_to"] == after["seq"] else None


def step_state(bundle: A.Bundle, session: A.Session, records: list[dict], item_id: str) -> str:
    """Where the form ``item_id`` of the reviewer's scope stands, from the stored records alone:
    INDEPENDENT_DUE (the independent form), REVEAL_PENDING (saved; a form of the same post still
    has no independent reading), AFTER_REVEAL_DUE (the form after the reveal), RETURNED (the
    approver sent the latest reading back: the form after the reveal, to correct) or DONE.
    Pure: the form's page shows exactly this state, and so does anything that checks a
    reviewer's resume point without signing in as them."""
    return gate(bundle, session, scoped(bundle, session)[item_id], records).state


def states(bundle: A.Bundle, session: A.Session, records: list[dict]) -> dict[str, str]:
    """``step_state`` of every form of the scope, in review order."""
    return {item_id: gate(bundle, session, item, records).state
            for item_id, item in scoped(bundle, session).items()}


def _sibling(bundle, session, records, item) -> str:
    """The form of ``item``'s post that still needs an independent reading (the first in review
    order)."""
    waiting = gate(bundle, session, item, records).waiting
    return next((i for i in scoped(bundle, session) if i in waiting), waiting[0])


def next_step(bundle: A.Bundle, session: A.Session, records: list[dict]) -> str | None:
    """The resume rule: the form "Continue" opens, or None when nothing is left. A reading the
    approver returned comes first (the approver is waiting): the first RETURNED form in review
    order. Otherwise the first form in review order that is not DONE; when that form is
    REVEAL_PENDING (its independent reading is saved and a form of the same post still has
    none), the step is that sibling. Pure: the stored records decide, nothing else."""
    state = states(bundle, session, records)
    back = next((i for i in state if state[i] == RETURNED), None)
    if back is not None:
        return back
    for item_id, item in scoped(bundle, session).items():
        if state[item_id] == REVEAL_PENDING:
            return _sibling(bundle, session, records, item)
        if state[item_id] != DONE:
            return item_id
    return None


def step_from(bundle: A.Bundle, session: A.Session, records: list[dict],
              item: dict) -> str | None:
    """The next step as one form's page names it: the form of the same post that still needs an
    independent reading, else this form while it is unfinished, else the resume rule."""
    state = step_state(bundle, session, records, item["item_id"])
    if state == REVEAL_PENDING:
        return _sibling(bundle, session, records, item)
    return item["item_id"] if state != DONE else next_step(bundle, session, records)


def names(bundle: A.Bundle, session: A.Session) -> dict[str, str]:
    """Each form's name in the workspace's list ("<idea> <n>: <kind>", as the Streamlit
    selector names it)."""
    return {item["item_id"]: f"{sid or 'no sequence'} {n}: {item['kind'].lower()}"
            for sid, items in A.ordered(bundle, session.scope)
            for n, item in enumerate(items, 1)}


def workspace(bundle: A.Bundle, session: A.Session, records: list[dict]) -> dict:
    """The workspace page: progress, and every form of the scope in review order with its
    status. Names are those of the Streamlit selector ("<idea> <n>: <kind>")."""
    prog = A.progress(bundle, records, session)
    status = states(bundle, session, records)
    name = names(bundle, session)
    groups = []
    for sid, items in A.ordered(bundle, session.scope):
        rows = []
        for item in items:
            forms = A.post_forms(bundle, item)
            k = next(j for j, f in enumerate(forms, 1) if f["item_id"] == item["item_id"])
            rows.append({"item_id": item["item_id"], "name": name[item["item_id"]],
                         "form": f"form {k} of {len(forms)}" if len(forms) > 1 else "",
                         "status": status[item["item_id"]],
                         "status_text": STATUS_TEXT[status[item["item_id"]]]})
        groups.append({"sequence": sid or "no sequence", "rows": rows})
    step = next_step(bundle, session, records)
    return {"progress": prog, "groups": groups, "forms": prog.total,
            "ideas": sum(g["sequence"] != "no sequence" for g in groups),
            "returned": sum(v == RETURNED for v in status.values()),
            "next": step, "next_name": name.get(step, "")}



def _when(at: str) -> tuple[str, str]:
    """(day, HH:MM) of a stored time, UTC as stored."""
    try:
        moment = datetime.fromisoformat(str(at))
    except ValueError:
        return str(at)[:10], str(at)[11:16]
    return f"{moment.day} {moment:%b %Y}", f"{moment:%H:%M}"


def receipt(rec: dict) -> str:
    """"Saved. Record 12, 25 Nov 2026 at 09:14 UTC." for a stored record."""
    day, hhmm = _when(rec["at"])
    return W.SAVED_RECEIPT.format(seq=rec["seq"], day=day, hhmm=hhmm)


def _record(rec: dict) -> str:
    """"record 12, 25 Nov 2026 at 09:14 UTC"."""
    day, hhmm = _when(rec["at"])
    return f"record {rec['seq']}, {day} at {hhmm} UTC"


def saved_notice(ind: dict | None, after: dict | None,
                 returned: dict | None = None) -> tuple[str, str] | None:
    """What is stored of this reviewer's readings of this form, in words, shown directly above
    the confidence: (kind, text). After a save the form starts again with empty flags, confidence
    and notes: they belong to the next save, and a confidence is never carried over.
    ``returned``: the approver's RETURN of that very reading, when it stands; its note is shown
    as plain text, the approver's name and address never."""
    anew = " Flags, confidence and notes start empty for every save: none is carried over."
    if after is not None and returned is not None:
        note = A.clean_text(str(returned.get("notes") or ""))
        return "warning", ("The approver returned your assessment (" + _record(after)
                           + ")." + (f" The approver's note: “{note}”." if note
                                     else " No note was given.")
                           + " Correct your answers above, give the flags, a confidence and "
                           "notes for the corrected reading, and save the assessment again."
                           + anew)
    if after is not None:
        return "success", ("Your assessment is saved (" + _record(after) + "). Nothing "
                           "more is needed here. To change it, correct the answers and save "
                           "again: the newest assessment counts." + anew)
    if ind is not None:
        return "info", ("Your own reading is saved (" + _record(ind) + "). Now judge "
                        "each of the system's answers beside yours, answer what the form still "
                        "asks, choose a confidence and save the assessment." + anew)
    return None


def form_header(bundle: A.Bundle, item: dict) -> str:
    forms = A.post_forms(bundle, item)
    if item["kind"] != "INSTRUCTION":
        return W.LABEL_ONLY_FORM
    k = next(n for n, i in enumerate(forms, 1) if i["item_id"] == item["item_id"])
    return W.FORM_OF_POST.format(k=k, n=len(forms))


def overall(bundle: A.Bundle, item: dict, records: list[dict], login: str) -> str | None:
    """The label of the whole post, once every form of it has a saved reading."""
    if item["kind"] != "INSTRUCTION":
        return None
    saved = [A.latest_own_reading(records, f["item_id"], bundle, login)
             for f in A.post_forms(bundle, item)]
    if not all(saved):
        return None
    if any(R.FORM_CANNOT_EXPRESS in (r.get("flags") or []) for r in saved):
        return ("The post as a whole: no label, because a form of this post says the form "
                "can't record it.")
    label = R.overall_label([r["reading"] for r in saved])
    return ("The post as a whole, from your saved readings: "
            + (W.option("post_label", label) if label else W.NOT_ANSWERED) + ".")


def describe(rec: dict) -> dict:
    """A stored reading in words (shown while the post still waits for another form)."""
    rows = []
    reading = rec["reading"]
    kind = R.LABEL_ONLY if set(reading) <= set(R.LABEL_ONLY_FIELDS) else R.INSTRUCTION
    for f, v in reading.items():
        if f == A.LEVELS or v in (R.NOT_APPLICABLE, [], ""):
            continue
        if f == "additional_actions":
            v = "; ".join(", ".join(W.option(k, x.get(k, "")) for k in R.ADDITIONAL_KEYS)
                          for x in v or [])
        else:
            v = W.shown(f, v)
        rows.append((W.question(f, kind), v))
    for n, r in enumerate(reading.get(A.LEVELS, []), 1):
        if r.get("role") in ("", R.NOT_APPLICABLE):
            continue
        rows.append((f"Number {n}", " · ".join(
            W.option(c, r[c]) if c != "month_named" else r[c]
            for c in R.ROW_KEYS if r.get(c) not in (None, "", R.NOT_APPLICABLE))))
    rows += [(W.QUESTIONS["flags"], W.shown("flags", rec["flags"])),
             (W.QUESTIONS["confidence"], W.option("confidence", rec["confidence"])
              if rec["confidence"] else W.NOT_ANSWERED),
             (W.QUESTIONS["notes"], A.clean_text(str(rec["notes"] or "")) or "None")]
    return {"head": "Your own reading. " + receipt(rec), "rows": rows}


# ------------------------------------------------------------------ the evidence and the system


def _published(at: str) -> str:
    day, hhmm = _when(at)
    return f"{day}, {hhmm} UTC"


def evidence(bundle: A.Bundle, item: dict) -> dict:
    """The post, its images by name, and the earlier posts shown with it, oldest first, each as
    its own card ("Earlier post · 1 of 3"; "Post you're reviewing"). The forms of the post, by
    number only (``"Form 1 of 2"``: nothing of what the system read in them). Publisher text:
    the templates render it as text only. ``w``: the wording, for the templates."""
    posts = bundle.posts
    refs = sorted({r for r in item["context_refs"] if r != item["post_ref"]},
                  key=lambda r: posts[r]["published_at"])
    p = posts[item["post_ref"]]
    forms = A.post_forms(bundle, item)
    return {"context": [{"ref": r, "at": posts[r]["published_at"],
                         "when": _published(posts[r]["published_at"]),
                         "label": W.EARLIER_POST.format(k=k, n=len(refs)),
                         "text": posts[r]["text"]}
                        for k, r in enumerate(refs, 1)],
            "post": {"ref": p["post_ref"], "at": p["published_at"],
                     "when": _published(p["published_at"]), "text": p["text"],
                     "revision": p["revision"], "media": list(p["media"]),
                     "links": [(k, p[k]) for k in ("reply_to", "quotes") if p[k]]},
            "forms": [{"item_id": f["item_id"], "k": k, "n": len(forms),
                       "current": f["item_id"] == item["item_id"]}
                      for k, f in enumerate(forms, 1)] if item["kind"] == "INSTRUCTION" else [],
            "w": W}


def media_posts(bundle: A.Bundle, session: A.Session) -> set[str]:
    """The posts whose images a page of this reviewer shows: the post of each form in scope."""
    return {i["post_ref"] for i in scoped(bundle, session).values()}


def _shown(v) -> str:
    if isinstance(v, list):
        return ", ".join(map(str, v)) or NOT_STATED_TEXT
    return v or NOT_STATED_TEXT


def _row_words(row: dict) -> str:
    return " · ".join(W.option(c, row[c]) for c in A.LEVEL_KEYS
                      if row.get(c) not in (None, "", R.NOT_APPLICABLE)) or W.NOT_ANSWERED


def system_block(bundle: A.Bundle, item: dict) -> dict:
    """Area 3, the system's reading (``_system``). Built only after the markers are stored.
    ``fields``: (question, answer in words); the codes as the system wrote them stay in
    ``levels``, ``holds`` and ``mapping`` (shown under "Technical detail")."""
    reading = A.system_reading(bundle, item)
    levels = [f"level {k} (number {'-' if lv['n'] is None else lv['n']}): {lv['price'] or '-'} "
              + " / ".join(lv[f] or "-" for f in A.LEVEL_KEYS)
              for k, lv in enumerate(item["levels"])]
    unread = [k.split(":")[1] for k, v in A.system_rows(bundle, item).items()
              if v.get("not_read")]
    holds = [f"holds {k}: {v['status']} {', '.join(v['reasons'])}".rstrip()
             for k, v in item["holds"].items()]
    m = item["mapping"]
    mapping = [(k, "-" if v in (None, "") else str(v)) for k, v in m.items()] if any(
        v not in (None, "") for v in m.values()) else []
    return {"fields": [(W.question(f, item["kind"]), _system_words(f, v))
                       for f, v in reading.items()],
            "levels": levels, "unread": unread, "holds": holds, "mapping": mapping}


def _system_words(f: str, v) -> str:
    if f == "ambiguities":
        codes = [x.strip() for x in v.split(",")] if isinstance(v, str) else list(v or [])
        return "; ".join(W.OPTIONS["ambiguities"].get(x, x) for x in codes if x) or \
            "Nothing marked unclear"
    if v in (None, "", []):
        return "No answer"
    return W.shown(f, v)


# ------------------------------------------------------------------ options and their meanings


def _meaning(f: str, code: str) -> str:
    """An option in words (``wording.option``)."""
    return W.option(f, code)


def choices(f: str, options, chosen=()) -> list[dict]:
    """Every option a control offers, in words, one per row (the Field reference shows codes)."""
    return [{"code": c, "meaning": W.option(f, c), "chosen": c in chosen} for c in options
            if c != ""]


def _select(name: str, options, value: str, label: str, words: str = "") -> dict:
    """A list box. ``words``: the field whose option words are shown."""
    return {"type": "select", "name": name, "label": label,
            "options": [(o, W.option(words, o) if words and o else o or UNANSWERED_TEXT,
                         o == value) for o in ("", *options)]}


def _tag(text: str) -> str:
    """What a verdict was chosen for: a changed answer starts its verdict from the comparison."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]


# ------------------------------------------------------------------ what the form holds

# ------------------------------------------------------------------ what the form holds


class _Held:
    """The answer each control holds: what the page sent for a control it had, else what the
    stored reading holds (``_peek``). Only an ANSWER of the stored reading counts: a value the
    form had derived there, or NOT_APPLICABLE, never answers a question that a correction now
    asks. A field that is not asked holds nothing: its value is the form's (derived or
    NOT_APPLICABLE), as on the independent form."""

    def __init__(self, posted, defaults: dict, item: dict, details: list[dict],
                 ctx: R.FormContext):
        self.posted, self.defaults, self.details = posted, defaults, details
        self.fields = R.fields_for(item["kind"])
        self.table = item["kind"] != "LABEL_ONLY"
        self.on = set(posted.getlist("_on")) if posted is not None else set()
        stored = R.applicability(defaults, ctx) if defaults else {}
        self.answers = {f for f in self.fields if stored.get(f, R.Status(R.NA)).state == R.ASK}

    def _default(self, f: str):
        d = self.defaults.get(f, [] if f in R.LIST_FIELDS else "")
        if f not in self.answers or d == R.NOT_APPLICABLE:
            return [] if f in R.LIST_FIELDS else ""
        return d

    def state(self, f: str) -> str | None:
        """Of a field with a typed value: VALUE, one of its sentinels, or None (unanswered)."""
        sentinels = R.VALUE_SENTINELS[f]
        if f in self.on:
            s = self.posted.get(f"s.{f}", "")
            return s if s in (VALUE, *sentinels) else None
        d = self._default(f)
        return d if d in sentinels else VALUE if d else None

    def value(self, f: str):
        d, live = self._default(f), f in self.on
        if f == "additional_actions":
            d = [x for x in d or [] if isinstance(x, dict)]
            n = len(d)
            if live:
                given = self.posted.get("aa.n", "")
                n = int(given) if given.isascii() and given.isdigit() and len(given) < 3 else 0
            return [{k: self.posted.get(f"aa.{j}.{k}", "") if live and self.posted.get(
                        f"aa.{j}.{k}") is not None else (d[j] if j < len(d) else {}).get(k, "")
                     for k in R.ADDITIONAL_KEYS} for j in range(min(n, R.MAX_ADDITIONAL))]
        if f in R.VALUE_SENTINELS:
            state = self.state(f)
            if state == VALUE:
                return self.posted.get(f, "") if live else d
            return state or ""
        if f == "ambiguities":
            return list(self.posted.getlist(f)) if live else list(d or [])
        return self.posted.get(f, "") if live else d

    def cell(self, key: str, c: str) -> str:
        if self.posted is not None and self.posted.get(f"lv.{key}.{c}") is not None:
            return self.posted.get(f"lv.{key}.{c}")
        given = next((r for r in self.defaults.get(A.LEVELS, []) if r.get("row") == key), {})
        return "" if given.get(c) == R.NOT_APPLICABLE else given.get(c, "")

    def settle(self, ctx: R.FormContext) -> dict:
        """The reading the form holds: every asked control's answer, nothing for a field or a
        table cell that is not asked. What is asked depends on the answers, so to a fixed point."""
        asked = cells = None
        raw: dict = {}
        for _ in range(8):
            raw = {f: self.value(f) if asked is None or f in asked else
                   ([] if f in R.LIST_FIELDS else "") for f in self.fields}
            if self.table:
                raw[A.LEVELS] = [{"row": d["key"], **{
                    c: self.cell(d["key"], c) if cells is None or (d["key"], c) in cells else ""
                    for c in R.ROW_KEYS}} for d in self.details]
            done = R.complete(raw, ctx)
            status = R.applicability(done, ctx)
            now = {f for f in self.fields if status.get(f, R.Status(R.NA)).state == R.ASK}
            now_cells = set()
            if self.table and status.get(A.LEVELS, R.Status(R.NA)).state == R.ASK:
                now_cells = {(row["row"], c) for row in raw[A.LEVELS]
                             for c, s in R.row_applicability(row, done).items()
                             if s.state == R.ASK}
            if (now, now_cells) == (asked, cells):
                break
            asked, cells = now, now_cells
        return raw



# ------------------------------------------------------------------ the form


@dataclass
class Form:
    """One form as a page shows it, and what a save of it stores."""

    item_id: str
    phase: str
    header: str
    blocks: list[dict] = field(default_factory=list)
    positions: list[tuple[str, str]] = field(default_factory=list)
    overall: str | None = None
    flag_options: list[dict] = field(default_factory=list)
    confidence_options: list[tuple] = field(default_factory=list)
    notice: tuple[str, str] | None = None
    invalid: str | None = None  # a value outside its closed set or shape: nothing can be checked
    unanswered: list[str] = field(default_factory=list)
    # Every blocking problem: {code, field, row, message, anchor, label, answer}. ``anchor``:
    # the id of the element that holds the control to correct ("Go to question").
    problems: list[dict] = field(default_factory=list)
    errors: dict[str, list[str]] = field(default_factory=dict)  # anchor -> messages shown there
    warnings: list[tuple[str, str]] = field(default_factory=list)  # (code, meaning)
    ack_value: str = ""
    may_save: bool = False
    save_title: str = ""
    save_hint: str = ""
    tried: bool = False  # a submission was refused: the corrections are shown at each question
    summary: str = ""
    # Interface state (posted fields ``ui.*``): kept through round trips, never stored.
    later: list[tuple[str, str]] = field(default_factory=list)  # (anchor, question) For later
    last: str = ""  # the anchor of the last question the reviewer was at
    next_anchor: str = ""  # the next applicable unanswered question after ``last``
    stored: dict = field(default_factory=dict)  # HH:MM (UTC) of each stored save, or ""
    w: object = W  # the wording, for the templates
    # What a save stores (``build_annotation`` checks all of it again).
    reading: dict = field(default_factory=dict)
    verdicts: dict = field(default_factory=dict)
    flags: list[str] = field(default_factory=list)
    confidence: str = ""
    notes: str = ""
    ack: list[str] = field(default_factory=list)

    @property
    def blocking(self) -> list[tuple[str, str]]:
        return [(p["code"], p["message"]) for p in self.problems]

    def later_names(self) -> list[str]:
        return [a for a, _ in self.later]


def _invalid_message(code: str) -> str:
    kind = code.split(":")[0]
    if kind == "BAD_PRICE":
        return ("This can't be stored as a price: type digits with an optional decimal point, "
                "such as 412.5, without a currency sign or thousands separator.")
    if kind == "BAD_PRICES":
        return ("These can't be stored as prices: type numbers separated by commas, such as "
                "412, 418.5.")
    if kind == "BAD_MONTH":
        return "Type the month or contract code in letters and digits only, such as DEC or Z6."
    return "This value can't be stored: choose one of the options, or shorten the text."


def build(bundle: A.Bundle, item: dict, records: list[dict], session: A.Session, *, phase: str,
          system: dict | None, defaults: dict, posted=None,
          notice: tuple[str, str] | None = None) -> Form:
    """The form of ``item`` for this reviewer. ``system`` is the system's reading and is given
    ONLY after the reveal (then every compared field shows the system's value and carries a
    verdict, and the two mapping questions are asked); before it nothing of the system is read
    here. ``defaults``: the stored reading the form starts from. ``posted``: what the page sent."""
    revealed = system is not None
    earlier = A.earlier_positions(records, bundle, item, session.login)
    ctx = A.form_context(bundle, item, revealed=revealed, earlier_positions=earlier)
    details = A.table_row_details(bundle, item)
    held = _Held(posted, defaults, item, details, ctx)
    raw = held.settle(ctx)
    done = R.complete(raw, ctx)
    status = R.applicability(done, ctx)
    compared = A.compared_fields(bundle, item, done, earlier) if revealed else ()
    sequences = [s["sequence_id"] for s in bundle.sequences
                 if s["sequence_id"] != item["sequence_id"]]
    kind = item["kind"]
    form = Form(item["item_id"], phase, form_header(bundle, item), notice=notice, reading=raw,
                save_title=W.SAVE[phase], save_hint=W.SAVE_HINT[phase])
    verdicts = form.verdicts
    given = posted if posted is not None else {}
    # The reviewer's own independent answers, beside a corrected one after the reveal.
    first = _latest(records, item["item_id"], bundle, A.INDEPENDENT, session.login) \
        if revealed else None
    original = first["reading"] if first is not None else {}
    anchors: dict[str, str] = {}  # field or row key -> the id of the element showing it
    order: list[tuple[str, bool]] = []  # (anchor, answered) of every asked control, in page order

    def verdict(name: str, tag: str, pre: str) -> dict:
        chosen = given.get(f"v.{name}") if given.get(f"vt.{name}") == tag else None
        chosen = chosen if chosen in A.VERDICTS else pre
        return {"name": f"v.{name}", "tag_name": f"vt.{name}", "tag": tag, "chosen": chosen,
                "options": [(c, W.VERDICTS[c], c == chosen) for c in A.VERDICTS]}

    def control(f: str) -> dict:
        value = raw[f]
        if f == "component":
            return {"type": "radio", "name": f, "options": [
                (c, W.option(f, c), c == value) for c in R.COMPONENTS]}
        if f in R.VALUE_SENTINELS:
            state = held.state(f)
            choice, box = W.VALUE_CHOICE[f]
            return {"type": "value", "name": f, "state_name": f"s.{f}", "typed": state == VALUE,
                    "value": value if state == VALUE else "", "maxlength": A.MAX_TEXT,
                    "box": box, "options": [(s, choice if s == VALUE else W.SENTINEL[s],
                                             s == state)
                                            for s in (VALUE, *R.VALUE_SENTINELS[f])]}
        if f == "ambiguities":
            return {"type": "checks", "name": f, "options": [
                {"code": c, "meaning": W.option(f, c), "checked": c in value}
                for c in R.AMBIGUITIES]}
        if f == "additional_actions":
            allowed = {"timing": R.ADDITIONAL_TIMINGS, "action": R.ADDITIONAL_ACTIONS,
                       "family": tuple(bundle.families), "side": R.SIDES}
            return {"type": "additional", "name": f, "count": len(value),
                    "counts": [(n, W.further_count(n), n == len(value))
                               for n in range(R.MAX_ADDITIONAL + 1)],
                    "rows": [[{**_select(f"aa.{j}.{k}", allowed[k], x[k],
                                         f"Further action {j + 1}: {W.ADDITIONAL[k][0]}",
                                         W.ADDITIONAL[k][1]),
                               "short": W.ADDITIONAL[k][0]} for k in R.ADDITIONAL_KEYS]
                             for j, x in enumerate(value)]}
        options = R.offered(f, bundle.families, sequences)  # never a value only the form sets
        if len(options) <= RADIO_UP_TO:
            return {"type": "radio", "name": f, "options": [
                (c, W.option(f, c), c == value) for c in options]}
        return _select(f, options, value, "", f)

    def answered(f: str) -> bool:
        v = raw[f]
        if f in R.VALUE_SENTINELS:
            return held.state(f) is not None and (held.state(f) != VALUE or bool(v))
        return f in R.LIST_FIELDS or bool(v)

    def entry(f: str, s: R.Status) -> dict:
        e = {"name": f, "anchor": f"field-{ident(f)}", "question": W.question(f, kind),
             "help": W.HELP.get(f, ""), "state": s.state}
        anchors[f] = e["anchor"]
        if s.state == R.ASK:
            e["control"] = control(f)
            order.append((e["anchor"], answered(f)))
        elif s.state == R.DERIVED:
            e["derived"] = W.shown(f, s.value) if s.value not in ("", None) else UNANSWERED_TEXT
            if f == "follower_implication":
                e["sentence"] = R.FOLLOWER_SENTENCE + (
                    " Inferred from context: not counted as something to act on."
                    if raw.get("explicit") == "NO" else "")
        elif s.state == R.PENDING:
            e["note"] = W.reason(s.reason)
        else:
            e["note"] = W.reason(s.reason)
        if revealed and f in compared:
            value = R.NOT_APPLICABLE if s.state == R.NA else (
                s.value if s.state == R.DERIVED else raw[f])
            e["system"] = _system_words(f, system.get(f, ""))
            if value == R.NOT_APPLICABLE:
                verdicts[f] = R.VERDICT_NOT_APPLICABLE
                e["verdict_fixed"] = W.VERDICT_FIXED
            else:
                e["verdict"] = verdict(f, _tag(repr(value)),
                                       R.prefill_verdict(f, value, system.get(f, "")))
                verdicts[f] = e["verdict"]["chosen"]
            if f in original and original[f] != value and original[f] not in (
                    "", R.NOT_APPLICABLE, []):
                e["original"] = W.shown(f, original[f])
        elif revealed and f in A.SYSTEM_FIELDS:
            e["system"], e["not_compared"] = _system_words(f, system.get(f, "")), True
        return e

    def proposals() -> list[dict]:
        entries = []
        for f, (key, title, (mine, said), none) in PROPOSALS.items():
            s, value = status.get(f, R.Status(R.NA)), item["mapping"].get(key) or ""
            e = {"name": f, "anchor": f"field-{ident(f)}", "title": title, "value": str(value),
                 "question": W.question(f), "help": W.HELP.get(f, ""),
                 "asked": s.state == R.ASK}
            anchors[f] = e["anchor"]
            if raw.get(mine) not in (None, "", R.NOT_APPLICABLE):
                e["mine"] = (said, W.shown(mine, raw[mine]))
            if s.state == R.ASK:
                e["options"] = [(c, W.option(f, c), raw[f] == c) for c in R.offered(f)]
                e["answered"] = raw[f] in R.offered(f)
                order.append((e["anchor"], e["answered"]))
            elif not value:
                e["why"] = none
            elif s.state == R.PENDING:
                e["why"] = "Nothing to judge yet: " + W.reason(s.reason)[:1].lower() + \
                    W.reason(s.reason)[1:]
            else:
                e["why"] = "Nothing to judge: " + W.reason(s.reason)[:1].lower() + \
                    W.reason(s.reason)[1:]
            entries.append(e)
        return entries

    def table() -> dict:
        state = status.get(A.LEVELS, R.Status(R.NA))
        block = {"kind": "table", "code": "T", "title": W.SECTIONS["T"],
                 "anchor": "sec-T", "asked": state.state == R.ASK, "rows": [],
                 "caption": ("One row per number of the post, in the order of the text." if
                             bundle.posts is not None else
                             "One row per level the system typed (this access shows no text).")}
        anchors[A.LEVELS] = block["anchor"]
        for n, d in enumerate(details, 1):
            block["rows"].append({"n": n, "key": d["key"], "label": d["label"],
                                  "anchor": f"row-{ident(d['key'])}", "context": d["context"],
                                  "where": f"(occurrence {d['k']} of {d['m']})"
                                  if d["m"] > 1 else ""})
            anchors[f"row:{d['key']}"] = block["rows"][-1]["anchor"]
        if state.state != R.ASK:
            block["note"] = W.reason(state.reason)
            if revealed and state.state == R.NA:
                verdicts[A.LEVELS] = {d["key"]: R.VERDICT_NOT_APPLICABLE for d in details}
            return block
        sys_rows = A.system_rows(bundle, item) if revealed else {}
        row_verdicts = {}
        for shown, row, completed in zip(block["rows"], raw[A.LEVELS], done[A.LEVELS],
                                         strict=True):
            n, key = shown["n"], shown["key"]
            row_status = R.row_applicability(row, done)
            shown["cells"] = []
            for c in R.ROW_KEYS:
                s, name = row_status[c], f"lv.{key}.{c}"
                label = f"Number {n} ({shown['label']}): {W.QUESTIONS[c]}"
                if s.state != R.ASK:
                    shown["cells"].append({"column": c, "short": W.QUESTIONS[c],
                                           "note": W.DOES_NOT_APPLY if s.state == R.NA
                                           else W.reason(s.reason)})
                elif c == "month_named":
                    shown["cells"].append({"column": c, "short": W.QUESTIONS[c], "type": "text",
                                           "name": name, "label": label, "value": row[c],
                                           "maxlength": 12})
                else:
                    shown["cells"].append({"column": c, "short": W.QUESTIONS[c],
                                           **_select(name, ROW_CHOICES[c], row[c], label, c)})
            order.append((shown["anchor"], all(
                row[c] for c in R.ROW_KEYS if row_status[c].state == R.ASK)))
            if not revealed:
                continue
            sr = sys_rows[key]
            if sr.get("not_read"):
                shown["system"], row_verdicts[key] = NOT_READ_TEXT, A.NOT_READ
                continue
            shown["system"] = _row_words(sr)
            shown["verdict"] = verdict(f"row.{key}", _tag("|".join(row[c] for c in R.ROW_KEYS)),
                                       R.prefill_row_verdict(completed, sr))
            row_verdicts[key] = shown["verdict"]["chosen"]
        if revealed:
            verdicts[A.LEVELS] = row_verdicts
        return block

    fields = R.fields_for(kind)
    for g, _title in R.GROUPS:
        mine = [f for f in fields if R.BY_NAME[f].group == g
                and (g != "I" or f in ("ambiguities", "additional_actions"))]
        if g == "T" or not mine:
            continue
        states = [status.get(f, R.Status(R.NA)) for f in mine]
        block = {"kind": "group", "code": g, "title": W.SECTIONS[g], "anchor": f"sec-{g}",
                 "fields": []}
        if g == "H" and revealed:
            block["proposals"] = proposals()
        elif len({s.state for s in states}) == 1 and states[0].state in (R.NA, R.PENDING) \
                and len({s.reason for s in states}) == 1:
            block["collapsed"] = W.reason(states[0].reason)
            for f in mine:
                anchors[f] = block["anchor"]
            if revealed:
                shown = [f for f in mine if f in A.SYSTEM_FIELDS]
                block["system"] = [(W.question(f, kind), _system_words(f, system.get(f, "")))
                                   for f in shown]
                if states[0].state == R.NA:  # the verdict follows: NOT_APPLICABLE
                    verdicts.update({f: R.VERDICT_NOT_APPLICABLE for f in mine if f in compared})
        else:
            block["fields"] = [entry(f, s) for f, s in zip(mine, states, strict=True)]
        if g == "H" and not revealed:
            continue  # asked only after the reveal: nothing to show before it
        form.blocks.append(block)
        if g == "G" and details:
            form.blocks.append(table())

    if ctx.kind != "LABEL_ONLY":
        prev = R.previous_position(done, ctx)
        if prev is not None:
            form.positions.append((W.POSITION_BEFORE, W.describe_position(prev)))
        form.positions.append((W.POSITION_AFTER, W.describe_position(R.position_after(done))))
    form.overall = overall(bundle, item, records, session.login)

    form.flags = [x for x in given.getlist("flags")] if posted is not None else []
    form.confidence = given.get("confidence", "") if posted is not None else ""
    form.notes = given.get("notes", "") if posted is not None else ""
    form.flag_options = [{"code": c, "meaning": W.option("flags", c), "checked": c in form.flags}
                         for c in A.FLAGS]
    form.confidence_options = [(c, W.option("confidence", c), c == form.confidence)
                               for c in A.CONFIDENCE]
    for f in ("flags", "confidence", "notes"):
        anchors[f] = f"field-{f}"
    order.append(("field-confidence", bool(form.confidence)))
    ind = _latest(records, item["item_id"], bundle, A.INDEPENDENT, session.login)
    aft = _latest(records, item["item_id"], bundle, A.AFTER_REVEAL, session.login)
    form.stored = {"reading": _when(ind["at"])[1] if ind else "",
                   "assessment": _when(aft["at"])[1] if aft else ""}
    _interface(form, posted, anchors, order)
    try:
        clean = A.clean_reading(raw, item, bundle)
    except A.AdjudicationError as exc:
        form.invalid = exc.code
        where = exc.code.split(":", 1)[1] if ":" in exc.code else ""
        anchor = anchors.get(where, "issues")
        form.problems.append({"code": exc.code, "field": where, "row": "", "anchor": anchor,
                              "message": _invalid_message(exc.code), "answer": False,
                              "label": W.QUESTIONS.get(where, "An answer")})
        form.errors.setdefault(anchor, []).append(_invalid_message(exc.code))
        form.summary = W.refusal_summary(0, 1)
        form.tried = True  # a value that can't be stored is shown at once
        return form
    _done, checks, _ctx = A.check_reading(
        bundle, item, clean, revealed=revealed, records=records, login=session.login,
        flags=form.flags, confidence=form.confidence, notes=form.notes)
    form.unanswered = list(checks.unanswered)
    for code, f in checks.blocking:
        _problem(form, code, f, raw, details, anchors, kind)
    form.warnings = [(c, W.WARNINGS[c]) for c in checks.warnings]
    form.ack_value = ",".join(checks.warnings)
    # The tick stands for the warnings it was given for: a changed list is checked again.
    if checks.warnings and given.get("ack") == form.ack_value:
        form.ack = list(checks.warnings)
    if checks.warnings and not form.ack:
        form.problems.append({"code": "WARNINGS", "field": "ack", "row": "", "anchor": "warnings",
                              "message": "Read the points to check, then tick “"
                              + W.WARNINGS_TICK + "”.", "answer": True,
                              "label": W.WARNINGS_TITLE})
    form.may_save = not checks.blocking and len(form.ack) == len(checks.warnings)
    answers = len({p["anchor"] for p in form.problems if p["answer"]})
    corrections = len({p["anchor"] for p in form.problems if not p["answer"]} -
                      {p["anchor"] for p in form.problems if p["answer"]})
    form.summary = W.refusal_summary(answers, corrections)
    for p in form.problems:  # shown at the question once a submission was refused
        form.errors.setdefault(p["anchor"], [])
        if p["message"] not in form.errors[p["anchor"]]:
            form.errors[p["anchor"]].append(p["message"])
    return form


def _problem(form: Form, code: str, f: str, raw: dict, details: list[dict],
             anchors: dict[str, str], kind: str) -> None:
    """One blocking problem of the rubric's checks, with the element to go to."""
    row = ""
    if f.startswith("levels row "):
        k = int(f.rsplit(" ", 1)[1])
        row = details[k - 1]["key"] if 0 < k <= len(details) else ""
        anchor = anchors.get(f"row:{row}", anchors.get(A.LEVELS, "issues"))
        label = f"Number {k} ({details[k - 1]['label']})" if row else W.SECTIONS["T"]
    elif code == "CANNOT_DETERMINE_NEEDS_REASON":
        target = "ambiguities" if "ambiguities" in anchors else "flags"
        anchor, label = anchors[target], W.question(target, kind)
    elif f == A.LEVELS:
        anchor, label = anchors.get(A.LEVELS, "issues"), W.SECTIONS["T"]
    else:
        anchor = anchors.get(f, "issues")
        label = W.question(f, kind) if f in W.QUESTIONS else "Your answers"
    message = W.BLOCKING.get(code, R.BLOCKING_CODES.get(code, code))
    if any(p["anchor"] == anchor and p["message"] == message for p in form.problems):
        return
    form.problems.append({"code": code, "field": f, "row": row, "anchor": anchor,
                          "message": message, "label": label,
                          "answer": code in W.UNANSWERED_CODES})


def _interface(form: Form, posted, anchors: dict[str, str], order: list) -> None:
    """The interface's own state, from the posted ``ui.*`` fields: the marks "For later", the
    last question the reviewer was at, a refused submission. Only ids this form has are kept.
    Never stored, never checked: navigation only."""
    known = {a: a for a in anchors.values()}
    labels = {}
    for b in form.blocks:
        for e in b.get("fields", []):
            labels.setdefault(e["anchor"], e["question"])
        for e in b.get("proposals", []):
            labels.setdefault(e["anchor"], e["question"])
        for r in b.get("rows", []):
            labels.setdefault(r["anchor"], f"Number {r['n']} ({r['label']})")
    labels.update({"field-flags": W.QUESTIONS["flags"],
                   "field-confidence": W.QUESTIONS["confidence"], "field-notes": "Notes"})
    marked = list(dict.fromkeys(posted.getlist("ui.later"))) if posted is not None else []
    form.later = [(a, labels[a]) for a in marked if a in known and a in labels]
    last = posted.get("ui.last", "") if posted is not None else ""
    form.last = last if last in known else ""
    form.tried = bool(posted is not None and posted.get("ui.tried") == "1")
    ids = [a for a, _ in order]
    start = ids.index(form.last) + 1 if form.last in ids else 0
    todo = [a for a, ok in order[start:] if not ok] or [a for a, ok in order if not ok]
    form.next_anchor = todo[0] if todo else "save"


def field_reference() -> dict:
    """The "Field reference" page: each question as the form words it, the field name and
    the stored code of every option (the rubric's table, as ``adjudication_fields.py``)."""
    groups = [{"code": code, "title": W.SECTIONS[code], "before": title,
               "fields": [{"field": f, "asked": W.question(f.name, R.LABEL_ONLY)
                           if f.name in W.QUESTIONS else f.question,
                           "options": [(W.shown(f.name, c) if not c.startswith("<") else "",
                                        c, m) for c, m in f.values]}
                          for f in R.RUBRIC if f.group == code]}
              for code, title in R.GROUPS]
    return {"groups": groups, "applicability": R.applicability_text(),
            "blocking": [(c, W.BLOCKING[c], m) for c, m in R.BLOCKING_CODES.items()],
            "warnings": [(c, W.WARNINGS[c], m) for c, m in R.WARNING_CODES.items()],
            "w": W}


def guide_blocks(text: str) -> list[tuple[str, str]]:
    """(kind, text) of the reviewer guide: ``title``, ``heading`` or ``para`` (a bullet or a
    paragraph, joined), as the workspace's guide page reads it. Rendered as text. The site's
    own "How to review", in the form's words, comes first."""
    out: list[tuple[str, str]] = [("heading", "How to review"),
                                  *(("para", line) for line in W.HOW_TO_REVIEW)]
    para: list[str] = []

    def flush():
        if para:
            out.append(("para", " ".join(para)))
            para.clear()

    for line in text.splitlines():
        if line.startswith("# "):
            flush()
            out.insert(0, ("title", line[2:]))
        elif line.startswith("## "):
            flush()
            out.append(("heading", line[3:]))
        elif not line.strip():
            flush()
        elif line.startswith(("- ", "1.", "2.", "3.", "4.", "5.", "6.")):
            flush()
            para.append(line.strip())
        else:
            para.append(line.strip())
    flush()
    return out
