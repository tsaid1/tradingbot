"""Vocabulary REVIEWER page: a stand-alone Streamlit app for ONE trusted outside reviewer, meant to
be reached only through an identity-aware proxy (docs/VOCAB_REVIEWER.md). It is not a page of the
owner dashboard and includes none of its pages.

It reads ONLY the owner's sanitized export (``TRADINGBOT_VOCAB_REVIEWER_EXPORT``: words, kinds,
counts and public codes; never a sentence, meaning, post id, date or path) and the recommendations
log beside it, and writes ONLY that log. The reviewer recommends; the owner decides on the owner
page. No file browser, upload, download or path input; no lexicon, config, broker or order code.
Every value from the export is shown as plain text (``st.text``), never Markdown or HTML, and no
error shows a path or a traceback.

Identity by the configured mode (``vocab_recommend.request_identity``): PUBLISHED, only from
the proxy's request header (default ``Tailscale-User-Login``); LOCAL, from
``TRADINGBOT_REVIEW_LOCAL_IDENTITY`` and any proxy header is refused. Without an identity the page
is a read-only preview; with ``TRADINGBOT_REVIEWER_ALLOWED_LOGINS`` set, any other login (or none)
sees only "Not authorized".
"""

from __future__ import annotations

import os
from pathlib import Path

import streamlit as st

from tradingbot.runtime import vocab_recommend as R

VERDICT_LABELS = {"ACCEPT": "recommend accept", "REJECT": "recommend reject (not now)",
                  "KEEP_HOLDING": "recommend keep holding", R.NEEDS_CONTEXT: "needs context"}
GROUP_LABELS = {g: R.GROUPS[g] for g in R.LIST_GROUPS}

def _refuse(message: str) -> None:
    st.error(message)
    st.stop()


def _proposal(n: int, row: dict, families: list[str], who: R.Identity, mine: dict,
              rows: list[dict], log: Path, can_submit: bool) -> None:
    pid = row["id"]
    st.subheader(f"Proposal {n}")
    st.text(R.reviewer_text(row))
    if pid in mine:
        st.text("Your latest recommendation: " + R.describe(mine[pid]))
    verdict = st.radio("Your recommendation", list(VERDICT_LABELS), index=None,
                       key=f"rv_verdict_{pid}", format_func=VERDICT_LABELS.get, horizontal=True)
    group = family = None
    if verdict == "ACCEPT":
        groups = list(GROUP_LABELS)
        group = st.selectbox("Into the group", groups, key=f"rv_group_{pid}",
                             index=groups.index(row["group"]) if row["group"] in groups else 0,
                             format_func=GROUP_LABELS.get)
        if group == R.MARKET_NAME:
            if not families:
                st.caption("No family to choose: the export names none.")
            else:
                family = st.selectbox("Family it names", families, key=f"rv_family_{pid}",
                                      index=families.index(row["family"])
                                      if row["family"] in families else 0)
    note = st.text_area(f"Note (optional, at most {R.MAX_NOTE} characters)",
                        max_chars=R.MAX_NOTE, key=f"rv_note_{pid}")
    if st.button("Submit recommendation", key=f"rv_submit_{pid}", disabled=not can_submit):
        if verdict is None:
            st.error("Not recorded: choose a recommendation first.")
            return
        try:
            rec = R.append_recommendation(log, rows, login=who.login, name=who.name,
                                          proposal=pid, verdict=verdict, group=group,
                                          family=family, note=note)
        except R.RecommendationError as exc:
            st.error(f"Not recorded ({exc.code}).")
        except (OSError, R.PrivatePathError) as exc:
            st.error(f"Not recorded: the recommendations log could not be written "
                     f"({type(exc).__name__}).")
        else:
            st.success(f"Recorded as line {rec['seq']}. A later submit supersedes it; the owner "
                       "decides.")


def main() -> None:
    st.set_page_config(page_title="Vocabulary reviewer", layout="wide")
    problem = R.server_problem(st.get_option, os.environ)
    if problem:
        _refuse(f"Refusing to run: {problem}.")
    who = R.request_identity(st.context.headers, os.environ, st.get_option("server.port"))
    if who.refused:
        _refuse("Not authorized.")
    st.title("Vocabulary reviewer")
    configured = os.environ.get(R.EXPORT_ENV)
    if not configured:
        _refuse("No review material is configured.")
    export = Path(configured).expanduser()
    log = R.recommendations_path(export)
    try:
        rows = R.read_export(export)
        checked = R.verify_log(log)
    except R.RecommendationError as exc:
        _refuse(f"The review material could not be read ({exc.code}).")
    except (OSError, R.PrivatePathError) as exc:
        _refuse(f"The review material could not be read ({type(exc).__name__}).")
    can_submit = who.login is not None
    if checked is not None and not checked.ok:
        if str(checked.problem).startswith("HEAD_"):
            st.error("The recommendations log's head file does not match: the owner runs "
                     "python scripts/adjudication_freeze.py repair-head --recommendations "
                     "(then --yes).")
        st.error("The recommendations log fails verification: submissions are disabled until "
                 "the owner checks it.")
        can_submit = False
    if who.login is None:
        st.info("Preview only: no reviewer identity, so submissions are disabled.")
    else:
        st.text(f"Reviewer ({'from the access proxy' if who.via_proxy else 'local'}): "
                f"{who.login}"
                + (f" ({who.name})" if who.name else ""))
    st.info("You recommend a classification for each proposed word. Your recommendations are "
            "advice: they change nothing. The owner reviews them and decides the final batch.")
    records = checked.records if checked is not None and checked.ok else []
    mine = {pid: by[who.login] for pid, by in R.latest(records).items() if who.login in by}
    families = R.export_families(rows)
    st.caption(f"{len(rows)} proposal(s).")
    for n, row in enumerate(rows, 1):
        _proposal(n, row, families, who, mine, rows, log, can_submit)


try:
    main()
except Exception as exc:  # no traceback (which may quote a path) to the reviewer or the logs
    if type(exc).__name__ in ("StopException", "RerunException"):
        raise
    st.error(f"The reviewer page stopped ({type(exc).__name__}); reload it.")
