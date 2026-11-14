"""Trading signal adjudication workspace: a stand-alone, loopback-only Streamlit app (not a page of
the owner dashboard) for establishing ground truth for complete trading ideas
(docs/ADJUDICATION_WORKSPACE.md). Two views: this workspace, and the sanitized vocabulary
recommendations page (``reviewer_app.py``) as its restricted vocabulary mode.

Per item, three areas kept apart: (1) SOURCE EVIDENCE (FULL access only), (2) THE READER'S READING,
(3) THE SYSTEM'S READING, hidden under blind-first until an INDEPENDENT reading is saved. Every
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

VOCAB_PAGE = Path(__file__).with_name("reviewer_app.py")
SAFE_OPTIONS = {"server.enableCORS": True, "server.enableXsrfProtection": True,
                "server.enableAgentApi": False, "server.enableStaticServing": False}
NOT_STATED = "(not stated)"


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


def _system(bundle: A.Bundle, item: dict) -> None:
    reading = A.system_reading(bundle, item)
    st.code("\n".join(f"{f}: {v or NOT_STATED}" for f, v in reading.items()
                      if f not in A.NOT_COMPARED), language=None)
    if item["levels"]:
        st.code("\n".join(f"level {k} (number {'-' if lv['n'] is None else lv['n']}): "
                          f"{lv['price'] or '-'} " + " / ".join(lv[f] or "-" for f in A.LEVEL_KEYS)
                          for k, lv in enumerate(item["levels"])), language=None)
    if item["holds"]:
        st.code("\n".join(f"holds {k}: {v['status']} {', '.join(v['reasons'])}"
                          for k, v in item["holds"].items()), language=None)
    m = item["mapping"]
    if any(v not in (None, "") for v in m.values()):
        st.code("\n".join(f"mapping {k}: {'-' if v in (None, '') else v}" for k, v in m.items()),
                language=None)


def _options(f: str, bundle: A.Bundle, item: dict) -> list[str] | None:
    kind = A.FIELDS[f]
    if isinstance(kind, tuple):
        return ["", *kind]
    if kind == "family":
        return ["", *bundle.families]
    if kind == "idea":
        return ["", *A.IDEA_FIXED, *(f"SEQUENCE:{s['sequence_id']}" for s in bundle.sequences
                                     if s["sequence_id"] != item["sequence_id"])]
    return None


def _inputs(prefix: str, bundle: A.Bundle, item: dict, defaults: dict,
            system: dict | None = None) -> tuple[dict, dict]:
    """The reading form; with ``system``, the system's value and a per-field verdict beside
    each field, pre-filled from the comparison (a new verdict widget when the value changes)."""
    reading, verdicts = {}, {}
    for f in A.fields_for(item["kind"]):
        cols = st.columns([3, 2, 3]) if system is not None else [st.container()]
        options, default = _options(f, bundle, item), defaults.get(f, "")
        if options is not None:
            reading[f] = cols[0].selectbox(
                f, options, key=f"{prefix}_{f}",
                index=options.index(default) if default in options else 0,
                format_func=lambda v: v or NOT_STATED)
        else:
            reading[f] = cols[0].text_input(f"{f} (empty = not stated)", value=default,
                                            key=f"{prefix}_{f}", max_chars=A.MAX_TEXT)
        if system is not None and f not in A.NOT_COMPARED:
            cols[1].text(f"system: {system.get(f) or NOT_STATED}")
            pre = A.prefill_verdicts({**system, f: reading[f]}, system, item["kind"])[f]
            tag = hashlib.sha256(reading[f].encode()).hexdigest()[:8]
            verdicts[f] = cols[2].radio(f"verdict on {f}", A.VERDICTS, horizontal=True,
                                        index=A.VERDICTS.index(pre),
                                        key=f"{prefix}_verdict_{f}_{tag}")
    rows = A.table_rows(bundle, item)
    if rows:
        reading[A.LEVELS], row_verdicts = _price_table(prefix, bundle, item, rows, defaults,
                                                       system)
        if system is not None:
            verdicts[A.LEVELS] = row_verdicts
    return reading, verdicts


def _price_table(prefix, bundle, item, rows, defaults, system) -> tuple[list, dict]:
    """One row per number of the post (FULL) or per typed level (RESTRICTED): role, exactness,
    contract month, unanswered unless the defaults say otherwise; after reveal, the system's
    answer and a per-row verdict beside it."""
    st.caption("Price table: " + ("one row per number of the post" if bundle.posts is not None
                                  else "one row per level the system typed (restricted: no "
                                       "text)") + ". Empty means unanswered.")
    given = {r["row"]: r for r in defaults.get(A.LEVELS, [])}
    sys_rows = A.system_rows(bundle, item) if system is not None else {}
    levels, verdicts = [], {}
    for k, (key, label) in enumerate(rows, 1):
        cols = st.columns([1, 2, 2, 2] + ([2, 3] if system is not None else []))
        cols[0].text(label)
        row = {"row": key}
        for col, f, allowed in zip(cols[1:4], A.LEVEL_KEYS,
                                   (A.TABLE_ROLES, A.EXACTNESS, A.CONTRACT_MONTHS), strict=True):
            options, default = ["", *allowed], given.get(key, {}).get(f, "")
            row[f] = col.selectbox(f"{f}, row {k}", options, key=f"{prefix}_lv_{key}_{f}",
                                   index=options.index(default) if default in options else 0,
                                   format_func=lambda v: v or "(unanswered)")
        levels.append(row)
        if system is not None:
            sr = sys_rows[key]
            cols[4].text("system: " + " / ".join(sr[f] or "-" for f in A.LEVEL_KEYS))
            pre = A.prefill_row_verdicts([row], sys_rows)[key]
            tag = hashlib.sha256("|".join(row[f] for f in A.LEVEL_KEYS).encode()).hexdigest()[:8]
            verdicts[key] = cols[5].radio(f"verdict, row {k}", A.VERDICTS, horizontal=True,
                                          index=A.VERDICTS.index(pre),
                                          key=f"{prefix}_lvv_{key}_{tag}")
    return levels, verdicts


def _with_rows(bundle, item, reading: dict) -> dict:
    """The system's reading with its price-table answers (the blind-off starting point)."""
    rows = A.system_rows(bundle, item)
    return {**reading, A.LEVELS: [{"row": k, **v} for k, v in rows.items()]} if rows else reading


def _extras(prefix: str, *, approval: bool = False) -> tuple[list, str, str]:
    flags = [] if approval else st.multiselect("flags", A.FLAGS, key=f"{prefix}_flags")
    confidence = "" if approval else st.radio("confidence", A.CONFIDENCE, index=None,
                                              horizontal=True, key=f"{prefix}_confidence")
    notes = st.text_area(f"notes (at most {A.MAX_NOTES} characters)", max_chars=A.MAX_NOTES,
                         key=f"{prefix}_notes")
    return flags, confidence or "", notes


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
    found = A._of(records, item_id, bundle.bundle_id, phase, login)
    return found[-1] if found else None


def _reviewer(base, bundle, session, item, records, writable) -> None:
    iid, me = item["item_id"], session.login
    blind = st.session_state.get("aw_blind", True)
    ind = _latest(records, iid, bundle, A.INDEPENDENT, me)
    after = _latest(records, iid, bundle, A.AFTER_REVEAL, me)
    seen = st.session_state.setdefault("aw_seen_system", set())
    revealed = ind is not None or after is not None or not blind
    if revealed and ind is None:
        seen.add(iid)  # the system's reading was shown before any independent reading
    system = A.system_reading(bundle, item)
    with st.container(border=True):
        st.subheader("2. Your reading")
        if not revealed:
            st.caption("Blind-first: record your own reading; the system's reading is revealed "
                       "after you save it.")
            prefix = f"aw_{iid}_ind"
            reading, _ = _inputs(prefix, bundle, item, {})
            flags, confidence, notes = _extras(prefix)
            if st.button("Save independent reading", key=f"{prefix}_save", type="primary"):
                if iid in seen:
                    st.error("Not saved: you have seen the system's reading of this item.")
                else:
                    _save(base, bundle, session, writable, item_id=iid, phase=A.INDEPENDENT,
                          blind=True, reading=reading, flags=flags, confidence=confidence,
                          notes=notes)
        else:
            source = after or ind
            st.caption("Your reading beside the system's: correct any field on its own; each "
                       "verdict is pre-filled from the comparison and can be changed.")
            prefix = f"aw_{iid}_after_{source['seq'] if source else 0}"
            reading, verdicts = _inputs(prefix, bundle, item, source["reading"] if source
                                        else _with_rows(bundle, item, system), system)
            flags, confidence, notes = _extras(prefix)
            if st.button("Save reading after reveal", key=f"{prefix}_save", type="primary"):
                _save(base, bundle, session, writable, item_id=iid, phase=A.AFTER_REVEAL,
                      blind=ind is not None, reading=reading, field_verdicts=verdicts,
                      flags=flags, confidence=confidence, notes=notes,
                      refers_to=ind["seq"] if ind else None)
    with st.container(border=True):
        st.subheader("3. The system's reading")
        if revealed:
            _system(bundle, item)
        else:
            st.caption("Hidden until you save an independent reading (blind-first).")


def _describe(rec: dict) -> str:
    who = rec["annotator"]
    lines = [(f"line {rec['seq']}, {rec['phase']} by {who['login']} at {rec['at']}"
              f" (blind {rec['blind']}, confidence {rec['confidence'] or '-'})")]
    lines += [f"{f}: {v or '-'}" + (f"   [{rec['field_verdicts'][f]}]"
                                    if f in rec["field_verdicts"] else "")
              for f, v in rec["reading"].items() if f != A.LEVELS]
    row_verdicts = rec["field_verdicts"].get(A.LEVELS) or {}
    lines += [f"price {r['row']}: " + " / ".join(r[f] or "-" for f in A.LEVEL_KEYS)
              + (f"   [{row_verdicts[r['row']]}]" if r["row"] in row_verdicts else "")
              for r in rec["reading"].get(A.LEVELS, [])]
    lines += [f"flags: {', '.join(rec['flags']) or '-'}", f"notes: {rec['notes'] or '-'}"]
    return "\n".join(lines)


def _approver(base, bundle, session, item, records, writable) -> None:
    iid = item["item_id"]
    with st.container(border=True):
        st.subheader("2. The reviewers' readings")
        afters = A._of(records, iid, bundle.bundle_id, A.AFTER_REVEAL)
        if not afters:
            st.caption("No reviewer has saved a reading of this item yet.")
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
                                key=f"{prefix}_decision")
            amended = {}
            if decision == "AMEND":
                st.caption("Amend only the fields that are wrong:")
                values, _ = _inputs(prefix, bundle, item, rec["reading"])
                old_rows = {r["row"]: r for r in rec["reading"].get(A.LEVELS, [])}
                amended = {f: v for f, v in values.items()
                           if f != A.LEVELS and v != rec["reading"].get(f, "")}
                changed = [r for r in values.get(A.LEVELS, []) if r != old_rows.get(r["row"])]
                if changed:
                    amended[A.LEVELS] = changed
            _flags, _conf, notes = _extras(prefix, approval=True)
            if st.button("Save decision", key=f"{prefix}_save", type="primary",
                         disabled=bool(own) or decision is None):
                _save(base, bundle, session, writable, item_id=iid, phase=A.APPROVAL,
                      blind=False, reading=amended, notes=notes, refers_to=chosen,
                      outcome=decision)
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
        st.error("The annotation log fails verification: saving is disabled.")
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

        def _next():
            start = ids.index(st.session_state.get("aw_pick", ids[0]))
            for iid in ids[start + 1:] + ids[:start + 1]:
                if prog.status[iid] in ("OPEN", "INDEPENDENT_SAVED", "AWAITING", "RETURNED"):
                    st.session_state["aw_pick"] = iid
                    return

        st.button("Next item not yet adjudicated", on_click=_next, key="aw_next")
        pick = st.selectbox("Item (by sequence, in time order)", ids, key="aw_pick",
                            format_func=labels.get)
    item = bundle.items[pick]
    st.subheader(labels[pick])
    with st.container(border=True):
        st.subheader("1. Source evidence")
        _evidence(bundle, item)
    (_reviewer if session.role == A.REVIEWER else _approver)(base, bundle, session, item,
                                                              records, log.ok)


def main() -> None:
    st.set_page_config(page_title="Adjudication", layout="wide")
    if st.get_option("server.address") != "127.0.0.1":
        _refuse("Refusing to run: this server is not bound to 127.0.0.1.")
    if st.get_option("browser.gatherUsageStats"):
        _refuse("Refusing to run while Streamlit usage statistics are on.")
    for option, safe in SAFE_OPTIONS.items():
        if st.get_option(option) != safe:
            _refuse(f"Refusing to run with {option} = {not safe} (it must be {safe}).")
    configured = os.environ.get(A.DIR_ENV)
    if not configured:
        _refuse("No adjudication material is configured.")
    base = Path(configured).expanduser()
    try:
        reviewers = A.load_reviewers(base / "reviewers.toml")
        session = A.resolve_session(st.context.headers, os.environ, reviewers,
                                    st.get_option("server.port"))
    except (A.AdjudicationError, OSError, A.PrivatePathError):
        _refuse("Not authorized.")
    try:
        bundle = A.load_bundle(base / "bundle", session.access)
    except (A.AdjudicationError, OSError, A.PrivatePathError) as exc:
        _refuse(f"The adjudication bundle is refused ({_code(exc)}).")

    def adjudication() -> None:
        _workspace(base, bundle, session)

    st.navigation([st.Page(adjudication, title="Adjudication", default=True),
                   st.Page(VOCAB_PAGE, title="Vocabulary recommendations",
                           url_path="vocabulary")]).run()


try:
    main()
except Exception as exc:  # no traceback (which may quote content or a path) to the page or logs
    if type(exc).__name__ in ("StopException", "RerunException"):
        raise
    st.error(f"The workspace stopped ({type(exc).__name__}); reload it.")
