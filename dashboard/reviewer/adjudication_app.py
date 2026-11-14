"""Trading signal adjudication workspace: a stand-alone, loopback-only Streamlit app (not a page of
the owner dashboard) for establishing ground truth for complete trading ideas
(docs/ADJUDICATION_WORKSPACE.md). Two views: this workspace, and the sanitized vocabulary
recommendations page (``reviewer_app.py``) as its restricted vocabulary mode.

Per item, three areas kept apart: (1) SOURCE EVIDENCE (FULL access only), (2) THE READER'S READING,
(3) THE SYSTEM'S READING, hidden under blind-first until an INDEPENDENT reading is saved on
every form of the post. The form follows the rubric (``runtime/adjudication_rubric.py``, version
2.2); two static pages show the reviewer guide and the field reference. Every
publisher or bundle string is plain text (``st.text`` / ``st.code``); images only from the
bundle's ``media/`` by a name the post lists. The only write is the annotation log. No broker,
order, mapping, lexicon or policy code; no upload, download, file browser or path input.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import streamlit as st

from tradingbot.runtime import adjudication as A
from tradingbot.runtime import adjudication_rubric as R

VOCAB_PAGE = Path(__file__).with_name("reviewer_app.py")
GUIDE_PAGE = Path(__file__).with_name("adjudication_guide.py")
FIELDS_PAGE = Path(__file__).with_name("adjudication_fields.py")
NOT_STATED = "(not stated)"
NOT_READ_TEXT = "not read by the system"


def _refuse(message: str) -> None:
    st.error(message)
    st.stop()


def _code(exc: Exception) -> str:
    return getattr(exc, "code", None) or type(exc).__name__


# ------------------------------------------------------------------ areas


def _evidence(bundle: A.Bundle, item: dict) -> None:
    if bundle.posts is None:
        st.caption("Not available: restricted access never loads the source posts or images.")
        return
    posts = bundle.posts
    context = sorted({r for r in item["context_refs"] if r != item["post_ref"]},
                     key=lambda r: posts[r]["published_at"])
    if context:
        st.caption("Earlier posts of the idea and recent context, oldest first:")
        for r in context:
            st.text(f"{posts[r]['published_at']}  [{r}]\n{posts[r]['text']}")
    p = posts[item["post_ref"]]
    st.caption("The post:")
    links = "".join(f"  {k} {p[k]}" for k in ("reply_to", "quotes") if p[k])
    st.text(f"{p['published_at']}  [{p['post_ref']}]  revision {p['revision']}{links}")
    st.text(p["text"])
    for name in p["media"]:
        st.text(f"image: {name}")
        try:
            st.image(A.read_media(bundle, p["post_ref"], name))
        except (A.AdjudicationError, OSError, A.PrivatePathError) as exc:
            st.text(f"not shown ({_code(exc)})")


def _shown(item: dict, f: str, v) -> str:
    if f in item.get("withheld", ()):
        return f"(withheld: {A.WITHHELD})"
    if isinstance(v, list):
        return ", ".join(map(str, v)) or NOT_STATED
    return v or NOT_STATED


def _system(bundle: A.Bundle, item: dict) -> None:
    reading = A.system_reading(bundle, item)
    st.code("\n".join(f"{f}: {_shown(item, f, v)}" for f, v in reading.items()),
            language=None)
    if item["levels"]:
        st.code("\n".join(f"level {k} (number {'-' if lv['n'] is None else lv['n']}): "
                          f"{lv['price'] or '-'} " + " / ".join(lv[f] or "-" for f in A.LEVEL_KEYS)
                          for k, lv in enumerate(item["levels"])), language=None)
    unread = [k for k, v in A.system_rows(bundle, item).items() if v.get("not_read")]
    if unread:
        st.text(f"{NOT_READ_TEXT}: " + ", ".join(f"number {k.split(':')[1]}" for k in unread))
    if item["holds"]:
        st.code("\n".join(f"holds {k}: {v['status']} {', '.join(v['reasons'])}"
                          for k, v in item["holds"].items()), language=None)
    m = item["mapping"]
    if any(v not in (None, "") for v in m.values()):
        st.code("\n".join(f"mapping {k}: " + (f"(withheld: {A.WITHHELD})"
                                               if f"mapping.{k}" in item.get("withheld", ())
                                               else '-' if v in (None, '') else str(v))
                          for k, v in m.items()), language=None)


# ------------------------------------------------------------------ the reading form (rubric 2)


VALUE = "VALUE"
_STATE_TEXT = {VALUE: "value given", R.NOT_STATED: "not stated",
               R.CANNOT_DETERMINE: "cannot determine"}
_FAMILY_MEANING = {"OTHER": "a market outside the list", "UNKNOWN": "cannot determine"}


def _label(f: str) -> str:
    return f"{R.BY_NAME[f].question} [{f}]"


def _help(f: str) -> str:
    return R.BY_NAME[f].tooltip  # static rubric text only: never a bundle string


def _meaning(f: str, code: str) -> str:
    if code == "":
        return "(unanswered)"
    if f == "family":
        return f"{code}: {_FAMILY_MEANING.get(code, 'a market of the bundle')}"
    if f == "idea" and code.startswith("SEQUENCE:"):
        return f"{code}: another listed idea"
    meanings = dict(R.BY_NAME[f].values)
    return f"{code}: {meanings[code]}" if code in meanings else code


def _options(f: str, bundle: A.Bundle, item: dict) -> tuple[str, ...]:
    sequences = [s["sequence_id"] for s in bundle.sequences
                 if s["sequence_id"] != item["sequence_id"]]
    return R.offered(f, bundle.families, sequences)  # never a value only the form sets


def _key(prefix: str, f: str) -> str:
    return f"{prefix}_{f}"


def _peek(prefix: str, f: str, defaults: dict):
    """The current answer of a field: the widget's state when it exists, else the default (an
    answer the form derives or a NOT_APPLICABLE default counts as unanswered)."""
    default = defaults.get(f, [] if f in R.LIST_FIELDS else "")
    if default == R.NOT_APPLICABLE:
        default = ""
    ss = st.session_state
    if f == "additional_actions":
        n = ss.get(f"{prefix}_aa_n", len(default or []))
        return [{k: ss.get(f"{prefix}_aa_{j}_{k}", (default[j] if j < len(default or []) else
                                                    {}).get(k, ""))
                 for k in R.ADDITIONAL_KEYS} for j in range(n or 0)]
    if f in R.VALUE_SENTINELS:
        state = ss.get(f"{prefix}_{f}_state", (default if default in R.VALUE_SENTINELS[f] else
                                               VALUE if default else None))
        if state in R.VALUE_SENTINELS[f]:
            return state
        return ss.get(_key(prefix, f), default if default not in R.VALUE_SENTINELS[f] else "") \
            if state == VALUE else ""
    return ss.get(_key(prefix, f), default)


def _peek_rows(prefix: str, rows: list[dict], defaults: dict) -> list[dict]:
    given = {r["row"]: r for r in defaults.get(A.LEVELS, [])}
    out = []
    for d in rows:
        g = given.get(d["key"], {})
        out.append({"row": d["key"], **{
            c: st.session_state.get(f"{prefix}_lv_{d['key']}_{c}",
                                    "" if g.get(c) == R.NOT_APPLICABLE else g.get(c, ""))
            for c in R.ROW_KEYS}})
    return out


def _collect(prefix: str, item: dict, defaults: dict, details: list[dict]) -> dict:
    reading = {f: _peek(prefix, f, defaults) for f in R.fields_for(item["kind"])}
    if item["kind"] != "LABEL_ONLY":
        reading[A.LEVELS] = _peek_rows(prefix, details, defaults)
    return reading


def _widget(prefix: str, f: str, bundle: A.Bundle, item: dict, defaults: dict, col) -> None:
    """One answered field: a closed choice with meanings, or a value with its answer state."""
    current = _peek(prefix, f, defaults)
    if f == "component":
        col.radio(_label(f), R.COMPONENTS, key=_key(prefix, f), help=_help(f),
                  index=R.COMPONENTS.index(current) if current in R.COMPONENTS else None,
                  format_func=lambda v: _meaning(f, v))
    elif f in R.VALUE_SENTINELS:
        states = (VALUE, *R.VALUE_SENTINELS[f])
        state = current if current in R.VALUE_SENTINELS[f] else VALUE if current else None
        chosen = col.radio(_label(f), states, key=f"{prefix}_{f}_state", horizontal=True,
                           help=_help(f), index=states.index(state) if state else None,
                           format_func=_STATE_TEXT.get)
        if chosen == VALUE:
            col.text_input(f"{_label(f)}: the value", key=_key(prefix, f), help=_help(f),
                           value=current if current not in R.VALUE_SENTINELS[f] else "",
                           max_chars=A.MAX_TEXT)
    elif f == "ambiguities":
        col.multiselect(_label(f), R.AMBIGUITIES, key=_key(prefix, f), help=_help(f),
                        default=[c for c in current if c in R.AMBIGUITIES],
                        format_func=lambda v: _meaning(f, v))
    elif f == "additional_actions":
        _additional(prefix, bundle, defaults, col)
    else:
        options = ("", *_options(f, bundle, item))
        col.selectbox(_label(f), options, key=_key(prefix, f), help=_help(f),
                      index=options.index(current) if current in options else 0,
                      format_func=lambda v: _meaning(f, v))


def _additional(prefix: str, bundle: A.Bundle, defaults: dict, col) -> None:
    current = _peek(prefix, "additional_actions", defaults)
    n = col.selectbox(_label("additional_actions"), range(R.MAX_ADDITIONAL + 1),
                      key=f"{prefix}_aa_n", help=_help("additional_actions"),
                      index=min(len(current), R.MAX_ADDITIONAL),
                      format_func=lambda v: f"{v} further action(s)")
    allowed = {"timing": R.ADDITIONAL_TIMINGS, "action": R.ADDITIONAL_ACTIONS,
               "family": tuple(bundle.families), "side": R.SIDES}
    meaning_of = {"timing": "timing", "action": "action", "family": "family", "side": "side"}
    for j in range(n or 0):
        cols = col.columns(4)
        for c, k in zip(cols, R.ADDITIONAL_KEYS, strict=True):
            value = current[j][k] if j < len(current) else ""
            options = ("", *allowed[k])
            c.selectbox(f"further action {j + 1}: {k}", options, key=f"{prefix}_aa_{j}_{k}",
                        index=options.index(value) if value in options else 0,
                        help=R.ADDITIONAL_HELP[k],
                        format_func=lambda v, k=k: _meaning(meaning_of[k], v))


def _verdict(prefix: str, f: str, value, system: dict, item: dict, col, verdicts: dict) -> None:
    col.text(f"system: {_shown(item, f, system.get(f, ''))}")
    if value == R.NOT_APPLICABLE:
        col.text("verdict: NOT_APPLICABLE (does not apply to your reading)")
        verdicts[f] = R.VERDICT_NOT_APPLICABLE
        return
    pre = R.prefill_verdict(f, value, system.get(f, ""))
    tag = hashlib.sha256(repr(value).encode()).hexdigest()[:8]
    withheld = f in item.get("withheld", ())  # never pre-filled: left unanswered
    v = col.radio(f"verdict on the system's {f}", A.VERDICTS, horizontal=True,
                  index=None if withheld else A.VERDICTS.index(pre),
                  key=f"{prefix}_verdict_{f}_{tag}", help=R.VERDICT_HELP,
                  format_func=R.VERDICT_TEXT.get)
    if v is not None:
        verdicts[f] = v


def _inputs(prefix: str, bundle: A.Bundle, item: dict, defaults: dict, ctx,
            system: dict | None = None, compared=()) -> tuple[dict, dict]:
    """The reading form, field groups A to I: plain-English labels with the field name in
    brackets, a tooltip on every field, option texts with their meanings, fields that do not
    apply greyed with the reason, derived values read-only. With ``system`` (after the
    reveal): the system's value beside each field and a verdict on it, pre-filled from the
    comparison. Nothing of the system's reading fills an answer, in any mode."""
    details = A.table_row_details(bundle, item)
    status = R.applicability(R.complete(_collect(prefix, item, defaults, details), ctx), ctx)
    verdicts: dict = {}
    groups = [(g, t) for g, t in R.GROUPS if g != "T"]
    fields = R.fields_for(item["kind"])
    for g, title in groups:
        mine = [f for f in fields if R.BY_NAME[f].group == g]
        if g == "I":
            mine = [f for f in mine if f in ("ambiguities", "additional_actions")]
        if not mine:
            continue
        st.caption(f"{g}. {title}")
        states = [status.get(f, R.Status(R.NA)) for f in mine]
        if len({s.state for s in states}) == 1 and states[0].state in (R.NA, R.PENDING) and len(
                {s.reason for s in states}) == 1:
            st.caption(("Does not apply: " if states[0].state == R.NA else "Not yet: ")
                       + f"{states[0].reason}.")
            if system is not None:
                shown = [f for f in mine if f in A.SYSTEM_FIELDS]
                if shown:
                    st.text("system: " + "; ".join(f"{f} {_shown(item, f, system.get(f, ''))}"
                                                   for f in shown))
                if states[0].state == R.NA:  # the verdict follows: NOT_APPLICABLE
                    verdicts.update({f: R.VERDICT_NOT_APPLICABLE for f in mine if f in compared})
            if g == "G" and details:
                verdicts.update(_price_table(prefix, bundle, item, details, defaults, status,
                                             system))
            continue
        for f, s in zip(mine, states, strict=True):
            cols = st.columns([3, 2, 3]) if system is not None else [st.container()]
            if s.state == R.ASK:
                _widget(prefix, f, bundle, item, defaults, cols[0])
            elif s.state == R.DERIVED:
                shown = s.value if s.value not in ("", None) else "(unanswered)"
                cols[0].text(f"{_label(f)}: {shown} (set by the form)")
                if f == "follower_implication":
                    inferred = _peek(prefix, "explicit", defaults) == "NO"
                    cols[0].caption(R.FOLLOWER_SENTENCE + (f" ({R.INFERRED_NOTE}.)"
                                                           if inferred else ""))
            elif s.state == R.PENDING:
                cols[0].caption(f"{_label(f)}: {s.reason}.")
            else:
                cols[0].caption(f"{_label(f)}: does not apply ({s.reason}).")
            if system is not None and f in compared:
                value = R.NOT_APPLICABLE if s.state == R.NA else (
                    s.value if s.state == R.DERIVED else _peek(prefix, f, defaults))
                _verdict(prefix, f, value, system, item, _Two(cols[1], cols[2]), verdicts)
            elif system is not None and f in A.SYSTEM_FIELDS:
                cols[1].text(f"system (not compared): {_shown(item, f, system.get(f, ''))}")
        if g == "G" and details:
            verdicts.update(_price_table(prefix, bundle, item, details, defaults, status, system))
    reading = _collect(prefix, item, defaults, details)
    rows = verdicts.pop("__rows__", None)
    if system is not None and rows is not None:
        verdicts[A.LEVELS] = rows
    return reading, verdicts


class _Two:
    """The system's value in one column, the verdict in the next."""

    def __init__(self, left, right):
        self.left, self.right = left, right

    def text(self, body):
        self.left.text(body)

    def radio(self, *a, **k):
        return self.right.radio(*a, **k)


def _price_table(prefix, bundle, item, details, defaults, status, system) -> dict:
    """One row per number of the post (FULL) or per typed level (RESTRICTED): the number as
    written, "k of m" for a repeated number and, FULL only, a few words around it (plain text);
    then role, exactness, contract month and month named as the rubric asks them."""
    st.caption("T. The price table: " + (
        "one row per number of the post, in text order" if bundle.posts is not None else
        "one row per level the system typed (restricted: no text)"))
    table = status.get(A.LEVELS, R.Status(R.NA))
    if table.state != R.ASK:
        for n, d in enumerate(details, 1):
            where = f"  ({d['k']} of {d['m']})" if d["m"] > 1 else ""
            st.text(f"row {n}: {d['label']}{where}" + (f"   ... {d['context']} ..."
                                                        if d["context"] else ""))
        st.caption(("Does not apply: " if table.state == R.NA else "Not yet: ")
                   + f"{table.reason}.")
        if system is not None and table.state == R.NA:
            return {"__rows__": {d["key"]: R.VERDICT_NOT_APPLICABLE for d in details}}
        return {}
    reading = R.complete(_collect(prefix, item, defaults, details), R.FormContext(
        rows=tuple(d["key"] for d in details)))
    rows = _peek_rows(prefix, details, defaults)
    sys_rows = A.system_rows(bundle, item) if system is not None else {}
    verdicts = {}
    allowed = {"role": R.TABLE_ROLES, "exactness": R.ROW_EXACTNESS,
               "contract_month": R.ROW_MONTHS}
    for n, (d, row) in enumerate(zip(details, rows, strict=True), 1):
        where = f"  ({d['k']} of {d['m']})" if d["m"] > 1 else ""
        st.text(f"row {n}: {d['label']}{where}" + (f"   ... {d['context']} ..."
                                                    if d["context"] else ""))
        cols = st.columns([2, 2, 2, 1] + ([2, 3] if system is not None else []))
        row_status = R.row_applicability(row, reading)
        for col, c in zip(cols[:4], R.ROW_KEYS, strict=True):
            s = row_status[c]
            label = f"{R.BY_NAME[c].question} [{c}], row {n}"
            if s.state != R.ASK:
                col.caption(f"{c}: " + ("not applicable" if s.state == R.NA else s.reason))
            elif c == "month_named":
                col.text_input(label, key=f"{prefix}_lv_{d['key']}_{c}", max_chars=12,
                               help=_help(c), value=row[c])
            else:
                options = ("", *allowed[c])
                col.selectbox(label, options, key=f"{prefix}_lv_{d['key']}_{c}", help=_help(c),
                              index=options.index(row[c]) if row[c] in options else 0,
                              format_func=lambda v, c=c: _meaning(c, v))
        if system is not None:
            sr = sys_rows[d["key"]]
            if sr.get("not_read"):
                cols[4].text(f"system: {NOT_READ_TEXT}")
                verdicts[d["key"]] = A.NOT_READ
                continue
            cols[4].text("system: " + " / ".join(sr[c] or "-" for c in A.LEVEL_KEYS))
            pre = R.prefill_row_verdict(reading[A.LEVELS][n - 1], sr)  # the completed row
            tag = hashlib.sha256("|".join(row[c] for c in R.ROW_KEYS).encode()).hexdigest()[:8]
            verdicts[d["key"]] = cols[5].radio(f"verdict, row {n}", A.VERDICTS, horizontal=True,
                                               index=A.VERDICTS.index(pre),
                                               key=f"{prefix}_lvv_{d['key']}_{tag}",
                                               help=R.VERDICT_HELP,
                                               format_func=R.VERDICT_TEXT.get)
    return {"__rows__": verdicts} if system is not None else {}


def _extras(prefix: str, *, approval: bool = False) -> tuple[list, str, str]:
    flags = [] if approval else st.multiselect(
        _label("flags"), A.FLAGS, key=f"{prefix}_flags", help=_help("flags"),
        format_func=lambda v: _meaning("flags", v))
    confidence = "" if approval else st.radio(
        _label("confidence"), A.CONFIDENCE, index=None, horizontal=True,
        key=f"{prefix}_confidence", help=_help("confidence"),
        format_func=lambda v: _meaning("confidence", v))
    notes = st.text_area(f"{_label('notes')} (at most {A.MAX_NOTES} characters)",
                         max_chars=A.MAX_NOTES, key=f"{prefix}_notes", help=_help("notes"))
    return flags, confidence or "", notes


def _issues(prefix: str, checks: R.Checks) -> tuple[bool, list[str]]:
    """Above Save: the required fields still unanswered, the blocking codes and the warnings
    with the "I have checked these" tick. Returns (may save, acknowledged warnings)."""
    if checks.unanswered:
        st.text("Required, unanswered: " + ", ".join(checks.unanswered))
    for code, f in checks.blocking:
        st.error(f"Save refuses {code}" + (f" ({f})" if f else "") + ": "
                 + R.BLOCKING_CODES.get(code, ""))
    ack: list[str] = []
    if checks.warnings:
        st.warning("Please check: " + "; ".join(f"{c}: {R.WARNING_CODES[c]}"
                                                for c in checks.warnings))
        if st.checkbox("I have checked these", key=f"{prefix}_ack"):
            ack = list(checks.warnings)
    return not checks.blocking and len(ack) == len(checks.warnings), ack


def _save(base, bundle, session, writable: bool, **kw) -> None:
    if not writable:
        st.error("Not saved: the annotation log fails verification.")
        return
    try:
        rec = A.append_annotation(base, bundle, session, **kw)
    except (A.AdjudicationError, OSError, A.PrivatePathError) as exc:
        st.error(f"Not saved ({_code(exc)}).")
        return
    st.session_state["aw_msg"] = f"Saved {rec['phase']} as line {rec['seq']}."
    st.rerun()


def _latest(records, item_id, bundle, phase, login=None):
    found = [r for r in A._of(records, item_id, bundle.bundle_id, phase, login)
             if phase == A.SYSTEM_SHOWN or r.get("rubric") == A.RUBRIC]
    return found[-1] if found else None


def _how_to_review() -> None:
    with st.expander("How to review"):
        for line in R.HOW_TO_REVIEW:
            st.text(line)


def _form_header(bundle, item) -> None:
    forms = A.post_forms(bundle, item)
    if item["kind"] == "INSTRUCTION":
        k = next(n for n, i in enumerate(forms, 1) if i["item_id"] == item["item_id"])
        st.text(f"Form {k} of {len(forms)} for this post")
    else:
        st.text("Label-only form: the label of the whole post")


def _positions(reading: dict, ctx) -> None:
    if ctx.kind == "LABEL_ONLY":
        return
    done = R.complete(reading, ctx)
    prev = R.previous_position(done, ctx)
    if prev is not None:
        st.text("Position after your reading of the previous action of this post in this "
                f"market: {R.describe_position(prev)}")
    st.text(f"Position AFTER this action: {R.describe_position(R.position_after(done))}")


def _overall(records, bundle, item, login) -> None:
    if item["kind"] != "INSTRUCTION":
        return
    saved = [A.latest_own_reading(records, f["item_id"], bundle, login)
             for f in A.post_forms(bundle, item)]
    if all(saved) and any(R.FORM_CANNOT_EXPRESS in (r.get("flags") or []) for r in saved):
        st.text("Label of the whole post: not derived (a form of this post is escalated)")
    elif all(saved):
        label = R.overall_label([r["reading"] for r in saved])
        st.text(f"Label of the whole post (from your saved readings): {label or '-'}")


def _form(prefix, base, bundle, session, item, records, writable, *, defaults, revealed,
          system=None, phase, blind, refers_to=None) -> None:
    earlier = A.earlier_positions(records, bundle, item, session.login)
    ctx = A.form_context(bundle, item, revealed=revealed, earlier_positions=earlier)
    compared = ()
    if system is not None:
        guess = R.complete(_collect(prefix, item, defaults, A.table_row_details(bundle, item)),
                           ctx)
        compared = A.compared_fields(bundle, item, guess, earlier)
    reading, verdicts = _inputs(prefix, bundle, item, defaults, ctx, system, compared)
    _positions(reading, ctx)
    flags, confidence, notes = _extras(prefix)
    try:
        clean = A.clean_reading(reading, item, bundle)
    except A.AdjudicationError as exc:
        st.error(f"Not a valid value ({exc.code}).")
        return
    _done, checks, _ctx = A.check_reading(bundle, item, clean, revealed=revealed,
                                          records=records, login=session.login, flags=flags,
                                          confidence=confidence, notes=notes)
    ok, ack = _issues(prefix, checks)
    title = "Save independent reading" if phase == A.INDEPENDENT else \
        "Save reading after reveal"
    if st.button(title, key=f"{prefix}_save", type="primary", disabled=not ok):
        _save(base, bundle, session, writable, item_id=item["item_id"], phase=phase,
              blind=blind, reading=reading,
              field_verdicts=verdicts if phase == A.AFTER_REVEAL else None, flags=flags,
              confidence=confidence, notes=notes, refers_to=refers_to,
              acknowledged_warnings=ack)


def _reviewer(base, bundle, session, item, records, writable) -> None:
    iid, me, bid = item["item_id"], session.login, bundle.bundle_id
    blind = st.session_state.get("aw_blind", True)
    ind = _latest(records, iid, bundle, A.INDEPENDENT, me)
    after = _latest(records, iid, bundle, A.AFTER_REVEAL, me)
    siblings = A.post_siblings(bundle, item, session.scope)
    new = A.rubric2(records)
    every_form_saved = all(A._of(new, s, bid, A.INDEPENDENT, me) for s in siblings)
    seen = _latest(records, iid, bundle, A.SYSTEM_SHOWN, me) is not None or any(
        r.get("rubric") != A.RUBRIC or r["phase"] != A.INDEPENDENT
        for r in A._of(records, iid, bid, login=me) if r["phase"] != A.SYSTEM_SHOWN)
    revealed = every_form_saved or seen or not blind
    if revealed and writable:
        try:  # durable, BEFORE anything of the system's reading is served (every form)
            A.mark_system_shown(base, bundle, session, iid)
        except (A.AdjudicationError, OSError, A.PrivatePathError) as exc:
            st.error(f"The system's reading cannot be shown ({_code(exc)}): the annotation log "
                     "could not record that it was shown.")
            revealed = False
            writable = False
    elif revealed:
        revealed = False  # the log fails verification: nothing of the system is shown
    with st.container(border=True):
        st.subheader("2. Your reading")
        _form_header(bundle, item)
        _how_to_review()
        _overall(records, bundle, item, me)
        if not revealed and ind is not None:
            st.caption("Your independent reading of this form is saved. The system's reading "
                       "appears once every form of this post has an independent reading.")
            st.code(_describe(ind), language=None)
        elif not revealed:
            st.caption("Blind-first: record your own reading; the system's reading is revealed "
                       "after you save it on every form of this post.")
            _form(f"aw_{iid}_ind", base, bundle, session, item, records, writable,
                  defaults={}, revealed=False, phase=A.INDEPENDENT, blind=True)
        else:
            source = after or ind
            st.caption("Your reading beside the system's: correct an answer only when the "
                       "system's reading points to evidence you missed; each verdict is on the "
                       "system's value, pre-filled from the comparison and editable.")
            _form(f"aw_{iid}_after_{source['seq'] if source else 0}", base, bundle, session,
                  item, records, writable, defaults=source["reading"] if source else {},
                  revealed=True, system=A.system_reading(bundle, item), phase=A.AFTER_REVEAL,
                  blind=ind is not None, refers_to=ind["seq"] if ind else None)
    with st.container(border=True):
        st.subheader("3. The system's reading")
        if revealed:
            _system(bundle, item)
        else:
            st.caption("Hidden until you save an independent reading on every form of this "
                       "post (blind-first), or while the annotation log cannot record that it "
                       "was shown.")


def _describe(rec: dict) -> str:
    who = rec["annotator"]
    lines = [(f"line {rec['seq']}, {rec['phase']} by {who['login']} at {rec['at']}"
              f" (rubric {rec.get('rubric', 1)}, blind {rec['blind']}, confidence "
              f"{rec['confidence'] or '-'})")]
    for f, v in rec["reading"].items():
        if f == A.LEVELS:
            continue
        if f == "additional_actions":
            v = "; ".join("/".join(x.get(k, "") for k in R.ADDITIONAL_KEYS) for x in v or [])
        elif isinstance(v, list):
            v = ", ".join(map(str, v))
        lines.append(f"{f}: {v or '-'}" + (f"   [{rec['field_verdicts'][f]}]"
                                           if f in rec["field_verdicts"] else ""))
    row_verdicts = rec["field_verdicts"].get(A.LEVELS) or {}
    lines += [f"price {r['row']}: " + " / ".join(r.get(f) or "-" for f in R.ROW_KEYS
                                                 if f in r)
              + (f"   [{row_verdicts[r['row']]}]" if r["row"] in row_verdicts else "")
              for r in rec["reading"].get(A.LEVELS, [])]
    lines += [f"flags: {', '.join(rec['flags']) or '-'}",
              f"acknowledged warnings: {', '.join(rec.get('acknowledged_warnings', [])) or '-'}",
              f"notes: {A.clean_text(str(rec['notes'] or '')) or '-'}"]
    return "\n".join(lines)


def _amendments(source: dict, values: dict, ctx, item: dict, bundle: A.Bundle) -> dict:
    """The approver's amended fields: the ASKED fields (in the merged reading) whose value
    differs from the reviewed reading, and the changed price-table rows."""
    old_rows = {r["row"]: r for r in source.get(A.LEVELS, [])}
    amended = {f: v for f, v in values.items()
               if f != A.LEVELS and v != source.get(f, "")
               and not (v in ("", []) and source.get(f) in (R.NOT_APPLICABLE, []))}
    changed = [r for r in values.get(A.LEVELS, [])
               if any(r[c] != old_rows.get(r["row"], {}).get(c, "") and not (
                   r[c] == "" and old_rows.get(r["row"], {}).get(c) in (
                       R.NOT_APPLICABLE, "")) for c in R.ROW_KEYS)]
    if changed:
        amended[A.LEVELS] = changed
    merged = A.merge_amendment(source, A.clean_reading(amended, item, bundle, partial=True), ctx)
    st_ = R.applicability(merged, ctx)
    return {f: v for f, v in amended.items()
            if f == A.LEVELS or st_.get(f, R.Status(R.NA)).state == R.ASK}


def _amend_defaults(source: dict, amended: dict, ctx, item: dict, bundle: A.Bundle) -> dict:
    """The approver's form defaults: the reviewed reading, with every field the amendment
    resets (14.3) left unanswered so that no widget preselects it."""
    merged = A.merge_amendment(source, A.clean_reading(amended, item, bundle, partial=True), ctx)
    reset = A.reset_by_amendment(source, merged, amended, ctx)
    return {**source, **{f: [] if f in R.LIST_FIELDS else "" for f in reset}}


def _approver(base, bundle, session, item, records, writable) -> None:
    iid = item["item_id"]
    with st.container(border=True):
        st.subheader("2. The reviewers' readings")
        _form_header(bundle, item)
        afters = [r for r in A._of(records, iid, bundle.bundle_id, A.AFTER_REVEAL)
                  if r.get("rubric") == A.RUBRIC]
        if not afters:
            st.caption("No reviewer has saved a rubric-2 reading of this item yet.")
        else:
            seqs = [r["seq"] for r in afters]
            chosen = st.selectbox("reviewed record", seqs[::-1], key=f"aw_{iid}_pick",
                                  format_func=lambda q: f"line {q}")
            rec = next(r for r in afters if r["seq"] == chosen)
            st.code(_describe(rec), language=None)
            ind = next((r for r in records if r["seq"] == rec["refers_to"]), None)
            if ind is not None:
                st.caption("The independent reading it followed:")
                st.code(_describe(ind), language=None)
            last = _latest(records, iid, bundle, A.APPROVAL)
            if last is not None:
                st.text(f"Latest decision: {last['decision']} (line {last['seq']}, "
                        f"{last['annotator']['login']})")
            own = A._of([r for r in records if r["phase"] != A.APPROVAL], iid,
                        bundle.bundle_id, login=session.login)
            if own:
                st.error("You wrote a reading of this item: its approval needs another login.")
            prefix = f"aw_{iid}_appr_{chosen}"
            decision = st.radio("decision", A.OUTCOMES, index=None, horizontal=True,
                                key=f"{prefix}_decision", help=R.DECISION_HELP,
                                format_func=R.DECISION_TEXT.get)
            amended, ok, ack = {}, True, []
            if decision == "AMEND":
                st.caption("Amend only the fields that are wrong (what follows from them is "
                           "derived again; a field that changes kind must be answered again):")
                rows = [r["row"] for r in rec["reading"].get(A.LEVELS, [])]
                ctx = A.form_context(bundle, item, revealed=True, rows=rows)
                details = A.table_row_details(bundle, item)
                try:  # 14.3: a field the amendment resets is never preselected
                    defaults = _amend_defaults(rec["reading"], _amendments(
                        rec["reading"], _collect(prefix, item, rec["reading"], details), ctx,
                        item, bundle), ctx, item, bundle)
                except A.AdjudicationError:
                    defaults = rec["reading"]
                values, _ = _inputs(prefix, bundle, item, defaults, ctx)
                try:
                    amended = _amendments(rec["reading"], values, ctx, item, bundle)
                    merged = A.merge_amendment(rec["reading"], A.clean_reading(
                        amended, item, bundle, partial=True), ctx)
                    checks = R.validate(merged, ctx, flags=rec["flags"],
                                        confidence=rec["confidence"], notes=rec["notes"] or "-")
                    ok, ack = _issues(prefix, checks)
                    _positions(merged, ctx)
                except A.AdjudicationError as exc:
                    st.error(f"Not a valid value ({exc.code}).")
                    ok = False
            _flags, _conf, notes = _extras(prefix, approval=True)
            if st.button("Save decision", key=f"{prefix}_save", type="primary",
                         disabled=bool(own) or decision is None or not ok):
                _save(base, bundle, session, writable, item_id=iid, phase=A.APPROVAL,
                      blind=False, reading=amended, notes=notes, refers_to=chosen,
                      outcome=decision, acknowledged_warnings=ack)
    with st.container(border=True):
        st.subheader("3. The system's reading")
        _system(bundle, item)


def _workspace(base: Path, bundle: A.Bundle, session: A.Session) -> None:
    st.title("Trading signal adjudication")
    st.text(f"{session.login} ({session.name}): {session.role}, {session.access} access, "
            + ("through the access proxy" if session.via_proxy else "local"))
    if session.access == A.RESTRICTED:
        st.info("Restricted access: the source posts, images and context are not loaded.")
    log = A.read_annotations(base)
    if not log.ok:
        st.error(f"The annotation log fails verification ({log.problem}): saving is "
                 "disabled." + (" If it is only the head file (a log without one, or a crash "
                                "between a line and its head), the owner runs: python "
                                "scripts/adjudication_freeze.py repair-head (then --yes)."
                                if str(log.problem).startswith("HEAD_") else ""))
    records = log.records if log.ok else []
    if msg := st.session_state.pop("aw_msg", None):
        st.success(msg)
    scoped = A.ordered(bundle, session.scope)
    order = [(sid, i) for sid, items in scoped for i in items]
    covers_all = session.scope is None or session.scope >= len(bundle.sequences)
    st.text(f"Scope: all {len(bundle.sequences)} ideas and the sampled posts" if covers_all
            else f"Scope: first {session.scope} ideas (of {len(bundle.sequences)})")
    prog = A.progress(bundle, records, session)
    labels = {}
    for sid, items in scoped:
        for n, i in enumerate(items, 1):
            labels[i["item_id"]] = (f"{sid or 'no sequence'} {n}: {i['kind'].lower()}, "
                                    f"{prog.status[i['item_id']].lower()}")
    with st.sidebar:
        st.metric("Sequences completed", f"{prog.sequences_done} of {prog.sequences}")
        st.metric("Items done", f"{prog.done} of {prog.total}")
        if session.role == A.REVIEWER:
            st.toggle("Blind-first (hide the system's reading until you save yours)",
                      value=True, key="aw_blind")
        ids = [i["item_id"] for _, i in order]

        def _next():  # a callback runs outside the page's error handling: nothing may escape
            try:
                current = st.session_state.get("aw_pick")
                start = ids.index(current) if current in ids else -1
                for iid in ids[start + 1:] + ids[:start + 1]:
                    if prog.status[iid] in ("OPEN", "INDEPENDENT_SAVED", "AWAITING",
                                            "RETURNED"):
                        st.session_state["aw_pick"] = iid
                        return
            except Exception:  # noqa: BLE001
                st.session_state["aw_msg"] = "Could not move to the next item."

        st.button("Next item not yet adjudicated", on_click=_next, key="aw_next")
        pick = st.selectbox("Item (by sequence, in time order)", ids, key="aw_pick",
                            format_func=labels.get)
    item = bundle.items[pick]
    st.text(labels[pick])
    with st.container(border=True):
        st.subheader("1. Source evidence")
        _evidence(bundle, item)
    (_reviewer if session.role == A.REVIEWER else _approver)(base, bundle, session, item,
                                                              records, log.ok)


def main() -> None:
    st.set_page_config(page_title="Adjudication", layout="wide")
    problem = A.server_problem(st.get_option, os.environ)
    if problem:
        _refuse(f"Refusing to run: {problem}.")
    configured = os.environ.get(A.DIR_ENV)
    if not configured:
        _refuse("No adjudication material is configured.")
    base = Path(configured).expanduser()
    published = bool(os.environ.get(A.PUBLISHED_HOST_ENV))
    try:
        synced = A.check_folders(base, media=False)
    except (A.AdjudicationError, OSError) as exc:
        _refuse(f"The adjudication folder is refused ({_code(exc)}).")
    if synced and published:
        _refuse(f"The adjudication folder is refused (PRIVATE_FOLDER_IN_SYNCED_FOLDER {synced}).")
    try:
        reviewers = A.load_reviewers(base / "reviewers.toml")
        session = A.resolve_session(st.context.headers, os.environ, reviewers,
                                    st.get_option("server.port"))
    except (A.AdjudicationError, OSError, A.PrivatePathError):
        _refuse("Not authorized.")
    try:
        if session.access == A.FULL:
            A.check_folders(base, media=True)
        bundle = A.load_bundle(base / "bundle", session.access)
    except (A.AdjudicationError, OSError, A.PrivatePathError) as exc:
        _refuse(f"The adjudication bundle is refused ({_code(exc)}).")
    if synced:
        st.warning(f"PRIVATE_FOLDER_IN_SYNCED_FOLDER ({synced}): the bundle and the judgments "
                   "are copied to that provider. Allowed locally by the owner's policy; refused "
                   "in published mode.")

    def adjudication() -> None:
        _workspace(base, bundle, session)

    st.navigation([st.Page(adjudication, title="Adjudication", default=True),
                   st.Page(GUIDE_PAGE, title="Reviewer guide", url_path="guide"),
                   st.Page(FIELDS_PAGE, title="Field reference", url_path="fields"),
                   st.Page(VOCAB_PAGE, title="Vocabulary recommendations",
                           url_path="vocabulary")]).run()


try:
    main()
except Exception as exc:  # no traceback (which may quote content or a path) to the page or logs
    if type(exc).__name__ in ("StopException", "RerunException"):
        raise
    st.error(f"The workspace stopped ({type(exc).__name__}); reload it.")
