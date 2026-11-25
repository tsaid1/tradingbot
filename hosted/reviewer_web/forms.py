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
from dataclasses import dataclass, field

from tradingbot.runtime import adjudication as A
from tradingbot.runtime import adjudication_rubric as R

VALUE = "VALUE"
NOT_STATED_TEXT = "(not stated)"
UNANSWERED_TEXT = "(unanswered)"
NOT_READ_TEXT = "not read by the system"
STATE_TEXT = {VALUE: "value given", R.NOT_STATED: "not stated",
              R.CANNOT_DETERMINE: "cannot determine"}
FAMILY_MEANING = {"OTHER": "a market outside the list", "UNKNOWN": "cannot determine"}
REPEAT_TABLE_CHOICES_FROM = 4  # rows: from here the table's meanings are offered again below it
ROW_CHOICES = {"role": R.TABLE_ROLES, "exactness": R.ROW_EXACTNESS, "contract_month": R.ROW_MONTHS}
# The two questions asked after the reveal (the owner's wording): field -> (the mapping value it
# is about, what that value is called, the question, the sentence when nothing was proposed).
PROPOSALS = {
    "reference_series_right": (
        "reference_series", "System's reference price series",
        "Is this the price series used for the numbers in the post?",
        "The system proposed no reference price series for this post, so no answer is asked."),
    "execution_instrument_right": (
        "proposed_execution_instrument", "System's proposed execution instrument",
        "Would using this instrument faithfully follow the publisher's trade idea?",
        "The system proposed no execution instrument for this post, so no answer is asked."),
}
ANSWER_TEXT = {"YES": "Yes", "NO": "No", "UNSURE": "Unsure"}
# Where one form stands for one reviewer (``step_state``), and how the workspace words it.
INDEPENDENT_DUE = "INDEPENDENT_DUE"  # no reading yet: the independent form
REVEAL_PENDING = "REVEAL_PENDING"  # independent reading saved; a form of the post still has none
AFTER_REVEAL_DUE = "AFTER_REVEAL_DUE"  # the system's reading may be shown; the second save is due
RETURNED = "RETURNED"  # the approver sent the reviewer's latest reading back
DONE = "DONE"  # a reading after the reveal is saved
STATUS_TEXT = {INDEPENDENT_DUE: "open", REVEAL_PENDING: "independent reading saved",
               AFTER_REVEAL_DUE: "reading after the reveal due", RETURNED: "Returned to you",
               DONE: "done"}
# What a refusal of the core means for the reviewer, in a calm sentence (the code stays visible).
REFUSALS = {
    "SYSTEM_READING_ALREADY_SEEN": (
        "An independent reading can no longer be saved for this form: a reading of it is already "
        "stored, or the system's reading has been shown for this post. Nothing that was saved is "
        "lost; the form below shows where it stands now."),
    "NOT_REVEALED": (
        "A reading after the reveal cannot be saved yet: the system's reading has not been shown "
        "for this form."),
    "BLIND_FIRST_PER_POST": (
        "Every form of this post needs an independent reading before a reading after the reveal."),
    "WARNINGS_NOT_ACKNOWLEDGED": (
        "The warnings changed or were not ticked: check them and tick \"I have checked these\"."),
    "VERDICT_FOR_EVERY_COMPARED_FIELD": (
        "Every compared field and every price row needs a verdict on the system's value."),
    "REVOKED": "This access has been withdrawn.",
}


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
    login. A later save by the reviewer ends it (the approval then refers to an older record)."""
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


def saved_notice(ind: dict | None, after: dict | None,
                 returned: dict | None = None) -> tuple[str, str] | None:
    """What is stored of this reviewer's readings of this form, in words, shown directly above
    the confidence: (kind, text). After a save the form starts again with empty flags, confidence
    and notes: they belong to the next save, and a confidence is never carried over.
    ``returned``: the approver's RETURN of that very reading, when it stands; its note is shown
    as plain text, the approver's name and address never."""
    def saved(rec: dict) -> str:
        return (f"record {rec['seq']}, saved {rec['at'][:10]} at {rec['at'][11:16]} UTC with "
                f"confidence {rec['confidence'] or 'not recorded'}")

    anew = (" Flags, confidence and notes are given anew at every save: none is carried over "
            "from the last one.")
    if after is not None and returned is not None:
        note = A.clean_text(str(returned.get("notes") or ""))
        return "warning", (f"SAVED, and RETURNED by the approver. Your reading of this form after "
                           f"the reveal is {saved(after)}. The approver returned this reading."
                           + (f" The approver's note: \"{note}\"." if note
                              else " No note was given.")
                           + " Correct the answers above, give the flags, a confidence and the "
                           "notes of the corrected reading, and save." + anew)
    if after is not None:
        return "success", (f"SAVED. Your reading of this form after the reveal is {saved(after)}. "
                           "Nothing more is needed here: go on with \"Continue\". Save again only "
                           "to CHANGE the reading: correct the answers above, give the flags, a "
                           "confidence and the notes of the changed reading, and save; the newest "
                           "record counts." + anew)
    if ind is not None:
        return "info", (f"SAVED. Your independent reading of this form is {saved(ind)}. Second "
                        "step, on this same form: compare with the system's reading, check each "
                        "verdict, answer what the form still asks, choose a confidence for the "
                        "reading after the reveal, and save it." + anew)
    return None


def form_header(bundle: A.Bundle, item: dict) -> str:
    forms = A.post_forms(bundle, item)
    if item["kind"] != "INSTRUCTION":
        return "Label-only form: the label of the whole post"
    k = next(n for n, i in enumerate(forms, 1) if i["item_id"] == item["item_id"])
    return f"Form {k} of {len(forms)} for this post"


def overall(bundle: A.Bundle, item: dict, records: list[dict], login: str) -> str | None:
    """The label of the whole post, once every form of it has a saved reading."""
    if item["kind"] != "INSTRUCTION":
        return None
    saved = [A.latest_own_reading(records, f["item_id"], bundle, login)
             for f in A.post_forms(bundle, item)]
    if not all(saved):
        return None
    if any(R.FORM_CANNOT_EXPRESS in (r.get("flags") or []) for r in saved):
        return "Label of the whole post: not derived (a form of this post is escalated)"
    label = R.overall_label([r["reading"] for r in saved])
    return f"Label of the whole post (from your saved readings): {label or '-'}"


def describe(rec: dict) -> dict:
    """A stored reading as plain rows (shown while the post still waits for another form)."""
    rows = []
    for f, v in rec["reading"].items():
        if f == A.LEVELS:
            continue
        if f == "additional_actions":
            v = "; ".join("/".join(x.get(k, "") for k in R.ADDITIONAL_KEYS) for x in v or [])
        elif isinstance(v, list):
            v = ", ".join(map(str, v))
        rows.append((f, v or "-"))
    rows += [(f"price {r['row']}", " / ".join(r.get(c) or "-" for c in R.ROW_KEYS if c in r))
             for r in rec["reading"].get(A.LEVELS, [])]
    rows += [("flags", ", ".join(rec["flags"]) or "-"),
             ("acknowledged warnings", ", ".join(rec.get("acknowledged_warnings", [])) or "-"),
             ("notes", A.clean_text(str(rec["notes"] or "")) or "-")]
    return {"head": (f"record {rec['seq']}, {rec['phase']}, saved {rec['at'][:10]} at "
                     f"{rec['at'][11:16]} UTC, confidence {rec['confidence'] or '-'}"),
            "rows": rows}


# ------------------------------------------------------------------ the evidence and the system


def evidence(bundle: A.Bundle, item: dict) -> dict:
    """The post, its images by name, and the earlier posts shown with it, oldest first. Publisher
    text: the templates render it as text only."""
    posts = bundle.posts
    refs = sorted({r for r in item["context_refs"] if r != item["post_ref"]},
                  key=lambda r: posts[r]["published_at"])
    p = posts[item["post_ref"]]
    return {"context": [{"ref": r, "at": posts[r]["published_at"], "text": posts[r]["text"]}
                        for r in refs],
            "post": {"ref": p["post_ref"], "at": p["published_at"], "text": p["text"],
                     "revision": p["revision"], "media": list(p["media"]),
                     "links": [(k, p[k]) for k in ("reply_to", "quotes") if p[k]]}}


def media_posts(bundle: A.Bundle, session: A.Session) -> set[str]:
    """The posts whose images a page of this reviewer shows: the post of each form in scope."""
    return {i["post_ref"] for i in scoped(bundle, session).values()}


def _shown(v) -> str:
    if isinstance(v, list):
        return ", ".join(map(str, v)) or NOT_STATED_TEXT
    return v or NOT_STATED_TEXT


def system_block(bundle: A.Bundle, item: dict) -> dict:
    """Area 3, the system's reading (``_system``). Built only after the markers are stored."""
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
    return {"fields": [(f, _shown(v)) for f, v in reading.items()], "levels": levels,
            "unread": unread, "holds": holds, "mapping": mapping}


# ------------------------------------------------------------------ options and their meanings


def _meaning(f: str, code: str) -> str:
    if f == "family":
        return FAMILY_MEANING.get(code, "a market of the bundle")
    if f == "idea" and code.startswith("SEQUENCE:"):
        return "another listed idea"
    return dict(R.BY_NAME[f].values).get(code, "")


def choices(f: str, options, chosen=()) -> list[dict]:
    """The complete meaning of every option a control offers, one option per row. The bundle's
    markets share one meaning and take one row, and so do the other listed ideas."""
    markets = [c for c in options if f == "family" and c and c not in FAMILY_MEANING]
    ideas = [c for c in options if f == "idea" and c.startswith("SEQUENCE:")]
    out = []
    for code in options:
        if code == "" or code in markets[1:] or code in ideas[1:]:
            continue
        if code in markets[:1]:
            out.append({"code": ", ".join(markets), "meaning": _meaning(f, code),
                        "chosen": any(c in chosen for c in markets)})
        elif code in ideas[:1]:
            out.append({"code": f"SEQUENCE:<idea> ({len(ideas)} in the list)",
                        "meaning": _meaning(f, code), "chosen": any(c in chosen for c in ideas)})
        else:
            out.append({"code": code, "meaning": _meaning(f, code), "chosen": code in chosen})
    return out


def _select(name: str, options, value: str, label: str) -> dict:
    return {"type": "select", "name": name, "label": label,
            "options": [(o, o or UNANSWERED_TEXT, o == value) for o in ("", *options)]}


def _tag(text: str) -> str:
    """What a verdict was chosen for: a changed answer starts its verdict from the comparison."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]


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
    positions: list[str] = field(default_factory=list)
    overall: str | None = None
    flag_options: list[dict] = field(default_factory=list)
    confidence_options: list[tuple] = field(default_factory=list)
    notice: tuple[str, str] | None = None
    invalid: str | None = None  # a value outside its closed set or shape: nothing can be checked
    unanswered: list[str] = field(default_factory=list)
    blocking: list[tuple[str, str]] = field(default_factory=list)  # (refuses | next, sentence)
    warnings: list[tuple[str, str]] = field(default_factory=list)  # (code, meaning)
    ack_value: str = ""
    may_save: bool = False
    save_title: str = ""
    # What a save stores (``build_annotation`` checks all of it again).
    reading: dict = field(default_factory=dict)
    verdicts: dict = field(default_factory=dict)
    flags: list[str] = field(default_factory=list)
    confidence: str = ""
    notes: str = ""
    ack: list[str] = field(default_factory=list)


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
    form = Form(item["item_id"], phase, form_header(bundle, item), notice=notice, reading=raw,
                save_title="Save independent reading" if phase == A.INDEPENDENT
                else "Save reading after reveal")
    verdicts = form.verdicts
    given = posted if posted is not None else {}

    def verdict(name: str, tag: str, pre: str) -> dict:
        chosen = given.get(f"v.{name}") if given.get(f"vt.{name}") == tag else None
        chosen = chosen if chosen in A.VERDICTS else pre
        return {"name": f"v.{name}", "tag_name": f"vt.{name}", "tag": tag, "chosen": chosen,
                "options": [(c, R.VERDICT_TEXT[c], c == chosen) for c in A.VERDICTS]}

    def control(f: str) -> dict:
        value = raw[f]
        if f == "component":
            return {"type": "radio", "name": f, "options": [
                (c, f"{c}: {_meaning(f, c)}", c == value) for c in R.COMPONENTS]}
        if f in R.VALUE_SENTINELS:
            state = held.state(f)
            return {"type": "value", "name": f, "state_name": f"s.{f}", "typed": state == VALUE,
                    "value": value if state == VALUE else "", "maxlength": A.MAX_TEXT,
                    "options": [(s, STATE_TEXT[s], s == state)
                                for s in (VALUE, *R.VALUE_SENTINELS[f])]}
        if f == "ambiguities":
            return {"type": "checks", "name": f, "options": [
                {"code": c, "meaning": _meaning(f, c), "checked": c in value}
                for c in R.AMBIGUITIES]}
        if f == "additional_actions":
            allowed = {"timing": R.ADDITIONAL_TIMINGS, "action": R.ADDITIONAL_ACTIONS,
                       "family": tuple(bundle.families), "side": R.SIDES}
            return {"type": "additional", "name": f, "count": len(value),
                    "counts": [(n, f"{n} further action(s)", n == len(value))
                               for n in range(R.MAX_ADDITIONAL + 1)],
                    "meanings": [(k, choices(k, allowed[k])) for k in R.ADDITIONAL_KEYS],
                    "rows": [[{**_select(f"aa.{j}.{k}", allowed[k], x[k],
                                         f"further action {j + 1}: {k}"),
                               "help": R.ADDITIONAL_HELP[k]} for k in R.ADDITIONAL_KEYS]
                             for j, x in enumerate(value)]}
        options = R.offered(f, bundle.families, sequences)  # never a value only the form sets
        return {**_select(f, options, value, ""), "meanings": choices(f, options, (value,))}

    def entry(f: str, s: R.Status) -> dict:
        e = {"name": f, "question": R.BY_NAME[f].question, "help": R.BY_NAME[f].tooltip,
             "state": s.state}
        if s.state == R.ASK:
            e["control"] = control(f)
        elif s.state == R.DERIVED:
            e["derived"] = s.value if s.value not in ("", None) else UNANSWERED_TEXT
            if f == "follower_implication":
                e["sentence"] = R.FOLLOWER_SENTENCE + (
                    f" ({R.INFERRED_NOTE}.)" if raw.get("explicit") == "NO" else "")
        elif s.state == R.PENDING:
            e["note"] = f"{s.reason}."
        else:
            e["note"] = f"does not apply ({s.reason})."
        if revealed and f in compared:
            value = R.NOT_APPLICABLE if s.state == R.NA else (
                s.value if s.state == R.DERIVED else raw[f])
            e["system"] = _shown(system.get(f, ""))
            if value == R.NOT_APPLICABLE:
                verdicts[f] = R.VERDICT_NOT_APPLICABLE
                e["verdict_fixed"] = "NOT_APPLICABLE (does not apply to your reading)"
            else:
                e["verdict"] = verdict(f, _tag(repr(value)),
                                       R.prefill_verdict(f, value, system.get(f, "")))
                verdicts[f] = e["verdict"]["chosen"]
        elif revealed and f in A.SYSTEM_FIELDS:
            e["system"], e["not_compared"] = _shown(system.get(f, "")), True
        return e

    def proposals() -> dict:
        entries = []
        for f, (key, title, question, none) in PROPOSALS.items():
            s, value = status.get(f, R.Status(R.NA)), item["mapping"].get(key) or ""
            e = {"name": f, "title": title, "value": str(value), "question": question,
                 "help": R.BY_NAME[f].tooltip, "asked": s.state == R.ASK}
            if s.state == R.ASK:
                e["options"] = [(c, ANSWER_TEXT[c], raw[f] == c) for c in R.offered(f)]
                e["answered"] = raw[f] in R.offered(f)
            elif not value:
                e["why"] = none
            elif s.state == R.PENDING:
                e["why"] = f"No answer is asked yet: {s.reason}."
            else:
                e["why"] = f"No answer is asked: {s.reason}."
            entries.append(e)
        return {"kind": "proposals", "entries": entries}

    def table() -> dict:
        state = status.get(A.LEVELS, R.Status(R.NA))
        block = {"kind": "table", "asked": state.state == R.ASK, "rows": [], "caption": (
            "one row per number of the post, in text order" if bundle.posts is not None else
            "one row per level the system typed (restricted: no text)")}
        for n, d in enumerate(details, 1):
            block["rows"].append({"n": n, "key": d["key"], "label": d["label"],
                                  "context": d["context"],
                                  "where": f"({d['k']} of {d['m']})" if d["m"] > 1 else ""})
        if state.state != R.ASK:
            block["note"] = ("Does not apply: " if state.state == R.NA else "Not yet: ") \
                + f"{state.reason}."
            if revealed and state.state == R.NA:
                verdicts[A.LEVELS] = {d["key"]: R.VERDICT_NOT_APPLICABLE for d in details}
            return block
        block["meanings"] = [(c, R.BY_NAME[c].question, choices(c, options))
                             for c, options in ROW_CHOICES.items()]
        block["repeat"] = len(details) >= REPEAT_TABLE_CHOICES_FROM
        sys_rows = A.system_rows(bundle, item) if revealed else {}
        row_verdicts = {}
        for shown, row, completed in zip(block["rows"], raw[A.LEVELS], done[A.LEVELS],
                                         strict=True):
            n, key = shown["n"], shown["key"]
            row_status = R.row_applicability(row, done)
            shown["cells"] = []
            for c in R.ROW_KEYS:
                s, name = row_status[c], f"lv.{key}.{c}"
                label = f"{R.BY_NAME[c].question} [{c}], row {n}"
                if s.state != R.ASK:
                    shown["cells"].append({"column": c, "note": "not applicable"
                                           if s.state == R.NA else s.reason})
                elif c == "month_named":
                    shown["cells"].append({"column": c, "type": "text", "name": name,
                                           "label": label, "value": row[c], "maxlength": 12})
                else:
                    shown["cells"].append({"column": c, **_select(name, ROW_CHOICES[c], row[c],
                                                                  label)})
            shown["chosen"] = [(c, row[c], _meaning(c, row[c])) for c in ROW_CHOICES
                               if row_status[c].state == R.ASK and row[c] in ROW_CHOICES[c]]
            if not revealed:
                continue
            sr = sys_rows[key]
            if sr.get("not_read"):
                shown["system"], row_verdicts[key] = NOT_READ_TEXT, A.NOT_READ
                continue
            shown["system"] = " / ".join(sr[c] or "-" for c in A.LEVEL_KEYS)
            shown["verdict"] = verdict(f"row.{key}", _tag("|".join(row[c] for c in R.ROW_KEYS)),
                                       R.prefill_row_verdict(completed, sr))
            row_verdicts[key] = shown["verdict"]["chosen"]
        if revealed:
            verdicts[A.LEVELS] = row_verdicts
        return block

    fields = R.fields_for(item["kind"])
    for g, title in R.GROUPS:
        mine = [f for f in fields if R.BY_NAME[f].group == g
                and (g != "I" or f in ("ambiguities", "additional_actions"))]
        if g == "T" or not mine:
            continue
        states = [status.get(f, R.Status(R.NA)) for f in mine]
        block = {"kind": "group", "code": g, "title": title, "fields": []}
        if g == "H" and revealed:
            block["proposals"] = proposals()["entries"]
        elif len({s.state for s in states}) == 1 and states[0].state in (R.NA, R.PENDING) \
                and len({s.reason for s in states}) == 1:
            block["collapsed"] = ("Does not apply: " if states[0].state == R.NA else "Not yet: ") \
                + f"{states[0].reason}."
            if revealed:
                shown = [f for f in mine if f in A.SYSTEM_FIELDS]
                block["system"] = "; ".join(f"{f} {_shown(system.get(f, ''))}" for f in shown)
                if states[0].state == R.NA:  # the verdict follows: NOT_APPLICABLE
                    verdicts.update({f: R.VERDICT_NOT_APPLICABLE for f in mine if f in compared})
        else:
            block["fields"] = [entry(f, s) for f, s in zip(mine, states, strict=True)]
        form.blocks.append(block)
        if g == "G" and details:
            form.blocks.append(table())

    if ctx.kind != "LABEL_ONLY":
        prev = R.previous_position(done, ctx)
        if prev is not None:
            form.positions.append("Position after your reading of the previous action of this "
                                  f"post in this market: {R.describe_position(prev)}")
        form.positions.append(
            f"Position AFTER this action: {R.describe_position(R.position_after(done))}")
    form.overall = overall(bundle, item, records, session.login)

    form.flags = [x for x in given.getlist("flags")] if posted is not None else []
    form.confidence = given.get("confidence", "") if posted is not None else ""
    form.notes = given.get("notes", "") if posted is not None else ""
    form.flag_options = [{"code": c, "meaning": _meaning("flags", c), "checked": c in form.flags}
                         for c in A.FLAGS]
    form.confidence_options = [(c, f"{c}: {_meaning('confidence', c)}", c == form.confidence)
                               for c in A.CONFIDENCE]
    try:
        clean = A.clean_reading(raw, item, bundle)
    except A.AdjudicationError as exc:
        form.invalid = exc.code
        return form
    _done, checks, _ctx = A.check_reading(
        bundle, item, clean, revealed=revealed, records=records, login=session.login,
        flags=form.flags, confidence=form.confidence, notes=form.notes)
    form.unanswered = list(checks.unanswered)
    for code, f in checks.blocking:
        what = code + (f" ({f})" if f else "") + ": " + R.BLOCKING_CODES.get(code, "")
        if notice and code in ("CONFIDENCE", "REQUIRED_UNANSWERED"):
            form.blocking.append(("next", f"The next save needs {what}"))
        else:
            form.blocking.append(("refuses", f"Save refuses {what}"))
    form.warnings = [(c, R.WARNING_CODES[c]) for c in checks.warnings]
    form.ack_value = ",".join(checks.warnings)
    # The tick stands for the warnings it was given for: a changed list is checked again.
    if checks.warnings and given.get("ack") == form.ack_value:
        form.ack = list(checks.warnings)
    form.may_save = not checks.blocking and len(form.ack) == len(checks.warnings)
    return form


def field_reference() -> dict:
    """The "Field reference" page, from the rubric's table (as ``adjudication_fields.py``)."""
    groups = [{"code": code, "title": title, "fields": [f for f in R.RUBRIC if f.group == code]}
              for code, title in R.GROUPS]
    return {"groups": groups, "applicability": R.applicability_text(),
            "blocking": list(R.BLOCKING_CODES.items()), "warnings": list(R.WARNING_CODES.items())}


def guide_blocks(text: str) -> list[tuple[str, str]]:
    """(kind, text) of the reviewer guide: ``title``, ``heading`` or ``para`` (a bullet or a
    paragraph, joined), as the workspace's guide page reads it. Rendered as text."""
    out: list[tuple[str, str]] = []
    para: list[str] = []

    def flush():
        if para:
            out.append(("para", " ".join(para)))
            para.clear()

    for line in text.splitlines():
        if line.startswith("# "):
            flush()
            out.append(("title", line[2:]))
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
