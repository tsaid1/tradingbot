"""Vocabulary review (OWNER ONLY, local): the words that make the entry rule hold, as one
consolidated batch of proposals in four groups, for the owner to accept, regroup, reject or keep
holding, then apply in ONE action to the private production lexicon (with one backup and undo), or
write as a separate CANDIDATE lexicon for an offline replay experiment.

It shows the publisher's own words and sentences: only the owner may run it, on their own machine,
bound to localhost with usage statistics off (it refuses otherwise). Every private or file-supplied
string is rendered as plain text (``st.text``, ``st.code``, the final table), never as Markdown or
HTML. Widgets are keyed by a stable proposal id and cleared after every write; Apply is bound to the
digest of the final table shown and refused when the files changed since. Session state holds
verdicts, ids and digests, never a sentence: sentences are loaded from the review log at render
time. Nothing is logged, cached or put in the URL (``tradingbot.runtime.vocab_review``).

The runtime config comes from ``TRADINGBOT_RUNTIME_CONFIG`` or ``~/.config/tradingbot/runtime.toml``;
it must set ``[interpreter] review_log_path`` and ``lexicon_path``. A proposals file is read from
``TRADINGBOT_VOCAB_PROPOSALS``, else beside the review log (``<log>.proposals.jsonl``); without one
the page builds a plain batch from the review log.

When the outside reviewer's recommendations log exists (beside ``TRADINGBOT_VOCAB_REVIEWER_EXPORT``,
docs/VOCAB_REVIEWER.md), each proposal also shows each reviewer's latest recommendation as one
plain-text line, read only: the verdict controls are never pre-set by it. Applying a batch then
appends, beside the lexicon, the owner's decision per word with the recommendations shown.
"""

from __future__ import annotations

import os
from pathlib import Path

import pandas as pd
import streamlit as st

from tradingbot.runtime import vocab_recommend as R
from tradingbot.runtime import vocab_review as V
from tradingbot.runtime.config import DEFAULT_CONFIG_PATH, ConfigError, load_config

LOCAL_ADDRESSES = ("127.0.0.1", "localhost", "::1")
RESTART_NOTE = ("A new word takes effect only when the service (or a replay) is restarted: the "
                "running service keeps the lexicon it loaded at start.")
VERDICT_LABELS = {V.PENDING: "undecided", V.ACCEPT: "accept", V.REJECT: "reject (not now)",
                  V.KEEP_HOLDING: "keep holding (not offered again)"}
# Every widget whose state must not survive a write (keyed by proposal id or by digest).
RESET_PREFIXES = ("vr_verdict_", "vr_group_", "vr_family_", "vr_meaning_", "vr_confirm")
HOW_TO = """[interpreter]
lexicon_path = "/Users/you/tradingbot-private/lexicon.json"       # outside any git work tree, chmod 600
review_log_path = "/Users/you/tradingbot-private/review.jsonl"    # outside any git work tree"""
STATUS = {"WRITTEN": st.success, "RECORDED": st.success, "RESTORED": st.success,
          "NOTHING": st.info, "NOTHING_TO_UNDO": st.info}


def _families(config) -> list[str]:
    try:
        from tradingbot.runtime.mapping import MappingPolicy

        return sorted(MappingPolicy.from_toml(config.paths.mapping_file).entries)
    except Exception:  # noqa: BLE001 - only the alias family picker depends on it
        return []


def _details(p: V.Proposal) -> str:
    """A proposal's details as plain text (shown with st.code: nothing is interpreted)."""
    return "\n".join([
        f"word: {p.token}",
        f"proposed group: {p.group}" + (f" (family {p.family})" if p.family else ""),
        f"kind: {p.kind or '-'}",
        f"evidence: {p.posts} held post(s), {p.holds} hold(s)",
        f"families: {', '.join(p.families) or '-'}",
        f"reason codes: {', '.join(p.reasons) or '-'}",
    ])


def _show_result(r: V.BatchResult) -> None:
    STATUS.get(r.status, st.error)(f"Result: {r.status}")
    st.text(r.message)
    if r.added:
        st.dataframe(pd.DataFrame([{"list": f"entry_anchor.{k}", "words added": len(v),
                                    "words": ", ".join(v)} for k, v in sorted(r.added.items())]),
                     hide_index=True)
    if r.kept_holding:
        st.text(f"Kept holding: {len(r.kept_holding)} ({', '.join(r.kept_holding)})")
    if r.refused:
        st.error("Refused words, with the lexicon loader's reasons:")
        st.code("\n".join(f"{w}: {reason}" for w, reason in r.refused), language=None)
    if r.backup is not None:
        st.text(f"Backup: {r.backup.name}")
    if r.status == "WRITTEN" and r.path is not None and r.path.name.endswith(V.CANDIDATE_SUFFIX):
        st.caption("Replay it offline, never with the running service: set lexicon_path to the "
                   "candidate in a separate replay config with a new state_dir, then run:")
        st.code(f"lexicon_path = \"{r.path}\"\nscripts/run_service.py --config <replay.toml> "
                "--source replay:<posts.jsonl> --allow-candidate-lexicon", language=None)


def _recommendations() -> tuple[dict, dict] | None:
    """({proposal id: {login: latest record}}, log head) when the reviewer's recommendations log
    exists, else None (the page is then exactly as without a reviewer)."""
    configured = os.environ.get(R.EXPORT_ENV)
    if not configured:
        return None
    path = R.recommendations_path(Path(configured).expanduser())
    if not path.exists():
        return None
    try:
        checked = R.verify_log(path)
    except (OSError, V.PrivatePathError) as exc:
        st.error(f"The reviewer's recommendations could not be read ({type(exc).__name__}); "
                 "none shown.")
        return {}, {"count": None, "sha256": None}
    if checked is None or not checked.ok:
        st.error("The reviewer's recommendations log fails verification ("
                 f"{checked.problem if checked else 'missing'}): none shown.")
        return {}, {"count": None, "sha256": None}
    st.caption("Reviewer recommendations are shown under each proposal: advice only, your "
               "decisions are not pre-set.")
    return R.latest(checked.records), {"count": checked.count, "sha256": checked.head}


def _audit(lexicon, proposals, verdicts, result, shown, head) -> None:
    """Record the owner's decisions with the recommendations they were shown with."""
    if result.status == "REFUSED":
        return
    decided = [(V.proposal_id(p), i) for p, i in zip(proposals, V.batch_items(proposals, verdicts),
                                                      strict=True) if i.verdict != V.PENDING]
    if not decided:
        return
    try:
        R.record_owner_decisions(lexicon, decided, result.status,
                                 result.backup.name if result.backup else None, shown, head)
    except (OSError, V.PrivatePathError) as exc:
        result.message += f" The reviewer audit could not be written ({type(exc).__name__})."


def _reset_selections() -> None:
    for key in list(st.session_state):
        if isinstance(key, str) and key.startswith(RESET_PREFIXES):
            del st.session_state[key]


def _done(result: V.BatchResult) -> None:
    """After any write: keep the result (words and codes only, no sentence), clear every verdict
    and confirmation, and render again from the files."""
    st.session_state["vr_result"] = result
    st.session_state["vr_reset"] = True
    st.rerun()


def main() -> None:
    st.set_page_config(page_title="Vocabulary review (owner)", page_icon="🔒", layout="wide")
    st.title("Vocabulary review (OWNER ONLY)")
    st.info(
        "What it is: the words that made the entry rule hold an entry, proposed as one batch of "
        "lexicon additions for you to approve.\n\n"
        "It shows the publisher's own text, privately, here on your machine only: never share "
        "this page, a screenshot of it or its files.\n\n"
        "Nothing changes until you apply the batch; you decide every word.")
    if st.session_state.pop("vr_reset", False):
        _reset_selections()
    if st.get_option("server.address") not in LOCAL_ADDRESSES:
        st.error("Refusing to show private text: this Streamlit server is not bound to localhost. "
                 "Start it with --server.address 127.0.0.1 (another account on this Mac can "
                 "still reach localhost).")
        st.stop()
    if st.get_option("browser.gatherUsageStats"):
        st.error("Refusing to show private text while Streamlit usage statistics are on. Start "
                 "it with --browser.gatherUsageStats false.")
        st.stop()

    config_path = Path(os.environ.get("TRADINGBOT_RUNTIME_CONFIG", str(DEFAULT_CONFIG_PATH)))
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        st.warning("No usable runtime config at the configured path:")
        st.text(f"{config_path}: {exc}")
        st.stop()
    lexicon = config.interpreter.lexicon_path
    log = config.interpreter.review_log_path
    if lexicon is None or log is None:
        st.warning("Set both private paths (review_log_path and lexicon_path) in the runtime "
                   "config, then restart the service:")
        st.code(HOW_TO, language="toml")
        st.stop()
    env = os.environ.get("TRADINGBOT_VOCAB_PROPOSALS")
    prop_path = Path(env).expanduser() if env else V.default_proposals_path(log)
    candidate_path = V.default_candidate_path(lexicon)
    for label, path in (("private lexicon", lexicon), ("review log", log),
                        ("proposals file", prop_path), ("candidate lexicon", candidate_path)):
        if V.git_tree_class(path) is not None:
            st.error(f"PRIVATE_PATH_IN_OTHER_GIT_TREE: the {label} is inside another git work "
                     "tree (not this program's repository). It could be committed to that "
                     "repository by mistake, and this tool cannot prevent it: keep it out of that "
                     "repository's commits (for example with an ignore rule).")
        cls = V.synced_folder_class(path)
        if cls is not None:
            st.error(f"PRIVATE_PATH_IN_SYNCED_FOLDER: the {label} is in a cloud-synced folder "
                     f"({cls}): it is copied to that provider. Allowed by the owner's policy; a "
                     "non-synced private directory is recommended for the review log, proposals, "
                     "candidates and backups.")
    st.warning(RESTART_NOTE)

    proposals, entries, source, bad = V.load_batch(log, lexicon, prop_path)
    if source == "file":
        st.caption("Proposals from the proposals file:")
        st.text(f"Proposals from {prop_path.name}")
    else:
        st.caption("No proposals file: a plain batch built from the review log by kind "
                   "(meaning empty, change not measured).")
    c1, c2, c3 = st.columns(3)
    c1.metric("Vocabulary holds logged", len(entries))
    c2.metric("Pending proposals", len(proposals))
    c3.metric("Unreadable lines", bad)

    families = _families(config)
    recs = _recommendations()
    verdicts: dict[str, V.Verdict] = {}
    n = 0
    for group, label in V.GROUPS.items():
        mine = [p for p in proposals if p.group == group]
        st.subheader(f"{label} ({len(mine)})")
        if not mine:
            st.caption("None proposed.")
        for p in mine:
            n += 1
            pid = V.proposal_id(p)
            with st.expander(f"Proposal {n}: {p.posts} held post(s), {p.holds} hold(s)"):
                st.code(_details(p), language=None)
                for rec in (recs[0].get(pid, {}) if recs else {}).values():
                    st.text(R.describe(rec))
                if p.market_warning:
                    st.text(f"Warning: {V.MAY_BE_MARKET}.")
                if p.measured:
                    st.caption("Measured change (claimed by the proposals file; not verified by "
                               "this page):")
                    st.code("\n".join(f"{k}: {v}" for k, v in sorted(p.measured.items())),
                            language=None)
                else:
                    st.caption("Measured change: not measured")
                examples = V.example_sentences(p, entries)
                if examples:
                    st.caption(f"Example sentences (at most {V.MAX_EXAMPLES}):")
                    st.code("\n".join(f"{role.lower() or 'sentence'}: {text}"
                                      for role, text in examples), language=None)
                meaning = st.text_input("Meaning", value=p.meaning, key=f"vr_meaning_{pid}")
                verdict = st.radio("Decision", list(VERDICT_LABELS), key=f"vr_verdict_{pid}",
                                   format_func=VERDICT_LABELS.get, horizontal=True)
                chosen = st.selectbox("Group", list(V.GROUPS), key=f"vr_group_{pid}",
                                      index=list(V.GROUPS).index(p.group),
                                      format_func=V.GROUPS.get)
                family = None
                if chosen == V.MARKET_NAME:
                    options = ([p.family] if p.family else []) + [
                        f for f in families if f != p.family]
                    if p.family and p.family not in families:
                        st.caption("The proposed family is not in the mapping file.")
                    if options:
                        family = st.selectbox("Family it names", options, key=f"vr_family_{pid}")
                    else:
                        st.caption("No family to choose: the mapping file has none.")
                verdicts[pid] = V.Verdict(verdict, chosen, family, meaning)

    items = V.batch_items(proposals, verdicts)
    digest = V.batch_digest(items, lexicon)
    short = digest[:16]
    st.subheader("Final batch")
    table = V.final_table(items)
    if table:
        st.dataframe(pd.DataFrame(table), hide_index=True)
    else:
        st.caption("Nothing accepted or kept holding yet.")
    st.text(f"Batch digest: {short}")
    st.caption("A word wrongly accepted removes a hold that protected against a second market or "
               "an unclear instruction: your judgement is the safeguard.")
    confirmed = st.checkbox(f"I have reviewed the final batch table above (digest {short})",
                            key=f"vr_confirm_batch_{short}")
    confirm_candidate = st.checkbox("Write the accepted words to a separate candidate file "
                                    "(experiment only)", key="vr_confirm_candidate")
    confirm_undo = st.checkbox(f"Undo the last batch ({len(V.backups(lexicon))} backup(s))",
                               key="vr_confirm_undo")
    previous = st.session_state.get("vr_shown_digest")
    st.session_state["vr_shown_digest"] = digest
    if recs is not None:  # logins, verdicts and line numbers only: what this render showed
        shown = st.session_state.get("vr_recs_shown")
        st.session_state["vr_recs_shown"] = {
            V.proposal_id(p): R.shown_summary(recs[0].get(V.proposal_id(p), {}))
            for p in proposals}
    b1, b2, b3 = st.columns(3)
    if b1.button("Apply batch to the production lexicon", key="vr_apply", type="primary"):
        if previous != digest:
            _show_result(V.BatchResult("REFUSED", "the batch changed since you reviewed it; "
                                                  "nothing written. Review the final table "
                                                  "again."))
        elif not confirmed:
            st.error("Not applied: tick the confirmation of the final table first.")
        else:
            result = V.apply_reviewed(lexicon, log, prop_path, verdicts, previous)
            if recs is not None:
                _audit(lexicon, proposals, verdicts, result,
                       shown if shown is not None else st.session_state["vr_recs_shown"], recs[1])
            _done(result)
    if b2.button("Write a candidate lexicon (experiment)", key="vr_candidate"):
        if not confirm_candidate:
            st.error("Not written: tick the candidate confirmation first.")
        else:
            _done(V.write_candidate(lexicon, items, candidate_path))
    if b3.button("Undo last batch", key="vr_undo"):
        if not confirm_undo:
            st.error("Not undone: tick the undo confirmation first.")
        else:
            _done(V.undo_last(lexicon))
    result = st.session_state.get("vr_result")
    if result is not None:
        st.subheader("What was done")
        _show_result(result)


try:
    main()
except Exception as exc:  # no traceback (which may quote private text) to the logs
    if type(exc).__name__ in ("StopException", "RerunException"):
        raise
    st.error(f"The review page stopped ({type(exc).__name__}); reload it.")
