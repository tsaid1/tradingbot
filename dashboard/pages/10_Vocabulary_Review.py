"""Vocabulary review (OWNER ONLY, local): the words that make the entry rule hold, as one
consolidated batch of proposals in four groups, for the owner to accept, regroup, reject or keep
holding, then apply in ONE action to the private production lexicon (with one backup and undo), or
write as a separate CANDIDATE lexicon for an offline replay experiment.

It shows the publisher's own words and sentences: only the owner may run it, on their own machine,
bound to localhost (it refuses otherwise). Nothing it shows is logged, cached, put in the URL or
written anywhere but the owner's private files (``tradingbot.runtime.vocab_review``).

The runtime config comes from ``TRADINGBOT_RUNTIME_CONFIG`` or ``~/.config/tradingbot/runtime.toml``;
it must set ``[interpreter] review_log_path`` and ``lexicon_path``. A proposals file is read from
``TRADINGBOT_VOCAB_PROPOSALS``, else beside the review log (``<log>.proposals.jsonl``); without one
the page builds a plain batch from the review log.
"""

from __future__ import annotations

import os
from pathlib import Path

import streamlit as st

from tradingbot.runtime import vocab_review as V
from tradingbot.runtime.config import DEFAULT_CONFIG_PATH, ConfigError, load_config

LOCAL_ADDRESSES = ("127.0.0.1", "localhost", "::1")
RESTART_NOTE = ("A new word takes effect only when the service (or a replay) is restarted: the "
                "running service keeps the lexicon it loaded at start.")
VERDICT_LABELS = {V.PENDING: "undecided", V.ACCEPT: "accept", V.REJECT: "reject (not now)",
                  V.KEEP_HOLDING: "keep holding (not offered again)"}
HOW_TO = """```toml
[interpreter]
lexicon_path = "/Users/you/tradingbot-private/lexicon.json"       # outside any git work tree, chmod 600
review_log_path = "/Users/you/tradingbot-private/review.jsonl"    # outside any git work tree
```"""


def _families(config) -> list[str]:
    try:
        from tradingbot.runtime.mapping import MappingPolicy

        return sorted(MappingPolicy.from_toml(config.paths.mapping_file).entries)
    except Exception:  # noqa: BLE001 - only the alias family picker depends on it
        return []


def _measured(m: dict[str, int] | None) -> str:
    if not m:
        return "not measured"
    return ", ".join(f"{k.replace('_', ' ')}: {v}" for k, v in sorted(m.items()))


def _show_result(r: V.BatchResult) -> None:
    show = st.success if r.status in ("WRITTEN", "RECORDED", "RESTORED") else (
        st.info if r.status in ("NOTHING", "NOTHING_TO_UNDO") else st.error)
    show(f"{r.status}: {r.message}")
    if r.added:
        st.markdown("\n".join(["| List | Words added |", "|---|---|"]
                              + [f"| {k} | {len(v)}: {', '.join(v)} |"
                                 for k, v in sorted(r.added.items())]))
    if r.kept_holding:
        st.markdown(f"Kept holding: {len(r.kept_holding)} ({', '.join(r.kept_holding)})")
    for word, reason in r.refused:
        st.error(f"Refused `{word}`: {reason}")
    if r.backup is not None:
        st.caption(f"Backup: {r.backup.name}")


def main() -> None:
    st.set_page_config(page_title="Vocabulary review (owner)", page_icon="🔒", layout="wide")
    st.title("Vocabulary review (OWNER ONLY)")
    st.info(
        "What it is: the words that made the entry rule hold an entry, proposed as one batch of "
        "lexicon additions for you to approve.\n\n"
        "It shows the publisher's own text, privately, here on your machine only: never share "
        "this page, a screenshot of it or its files.\n\n"
        "Nothing changes until you apply the batch; you decide every word.")
    address = st.get_option("server.address")
    if address not in LOCAL_ADDRESSES:
        st.error("Refusing to show private text: this Streamlit server is not bound to localhost. "
                 "Start it with `--server.address 127.0.0.1`.")
        st.stop()
    if st.get_option("browser.gatherUsageStats"):
        st.warning("Streamlit usage statistics are on (they carry no page content). To turn them "
                   "off, start with `--browser.gatherUsageStats false`.")

    config_path = Path(os.environ.get("TRADINGBOT_RUNTIME_CONFIG", str(DEFAULT_CONFIG_PATH)))
    try:
        config = load_config(config_path)
    except ConfigError as exc:
        st.warning(f"No usable runtime config at `{config_path}`: {exc}")
        st.stop()
    lexicon = config.interpreter.lexicon_path
    log = config.interpreter.review_log_path
    if lexicon is None or log is None:
        st.warning("Set both private paths in the runtime config, then restart the service:")
        st.markdown(HOW_TO)
        st.stop()
    st.warning(RESTART_NOTE)

    entries, bad = V.read_review_log(log)
    env = os.environ.get("TRADINGBOT_VOCAB_PROPOSALS")
    prop_path = Path(env).expanduser() if env else V.default_proposals_path(log)
    if prop_path.exists():
        proposals, bad_props = V.read_proposals(prop_path, entries=entries, lexicon_path=lexicon)
        st.caption(f"Proposals from {prop_path.name} ({bad_props} unreadable line(s)).")
    else:
        proposals = V.proposals_from_log(entries, lexicon_path=lexicon)
        st.caption("No proposals file: a plain batch built from the review log by kind "
                   "(meaning empty, change not measured).")
    c1, c2, c3 = st.columns(3)
    c1.metric("Vocabulary holds logged", len(entries))
    c2.metric("Pending proposals", len(proposals))
    c3.metric("Unreadable log lines", bad)

    families = _families(config)
    items: list[V.BatchItem] = []
    for group, label in V.GROUPS.items():
        mine = [(i, p) for i, p in enumerate(proposals) if p.group == group]
        st.subheader(f"{label} ({len(mine)})")
        if not mine:
            st.caption("None proposed.")
        for i, p in mine:
            with st.expander(f"{p.token}: {p.posts} held post(s), {p.holds} hold(s)"):
                st.markdown(f"Kind: {p.kind or '-'}; families: {', '.join(p.families) or '-'}; "
                            f"reason codes: {', '.join(p.reasons) or '-'}")
                st.markdown(f"Measured change: {_measured(p.measured)}")
                for role, text in p.examples[:12]:
                    st.markdown(f"- *{role.lower() or 'sentence'}*: {text}")
                meaning = st.text_input("Meaning", value=p.meaning, key=f"vr_meaning_{i}")
                verdict = st.radio("Decision", list(VERDICT_LABELS), key=f"vr_verdict_{i}",
                                   format_func=VERDICT_LABELS.get, horizontal=True)
                chosen = st.selectbox("Group", list(V.GROUPS), key=f"vr_group_{i}",
                                      index=list(V.GROUPS).index(p.group),
                                      format_func=V.GROUPS.get)
                family = None
                if chosen == V.MARKET_NAME:
                    options = families or ([p.family] if p.family else [])
                    family = st.selectbox(
                        "Family it names", options, key=f"vr_family_{i}",
                        index=options.index(p.family) if p.family in options else 0)
                items.append(V.BatchItem(p.token, verdict, chosen, family, meaning))

    st.subheader("Batch")
    counts = {v: sum(1 for it in items if it.verdict == v) for v in VERDICT_LABELS}
    st.markdown(", ".join(f"{VERDICT_LABELS[v]}: {n}" for v, n in counts.items()))
    st.caption("Accepted words go to their group's list; an ambiguous expression must be moved to "
               "a group first. A word wrongly marked as not a market removes a hold: your judgement "
               "is the safeguard.")
    confirmed = st.checkbox("I have reviewed every accepted word", key="vr_confirm")
    b1, b2, b3 = st.columns(3)
    if b1.button("Apply batch to the production lexicon", key="vr_apply", type="primary"):
        if not confirmed:
            st.error("Not applied: tick the confirmation first.")
        else:
            st.session_state["vr_result"] = V.apply_batch(lexicon, items)
    if b2.button("Write a candidate lexicon (experiment)", key="vr_candidate"):
        out = V.default_candidate_path(lexicon)
        st.session_state["vr_result"] = V.write_candidate(lexicon, items, out)
    if b3.button(f"Undo last batch ({len(V.backups(lexicon))} backup(s))", key="vr_undo"):
        st.session_state["vr_result"] = V.undo_last(lexicon)
    result = st.session_state.get("vr_result")
    if result is not None:
        st.subheader("What was done")
        _show_result(result)
        if result.status == "WRITTEN" and result.path is not None and result.path != lexicon:
            st.markdown("Replay it offline (never the running service): set `lexicon_path` to "
                        f"`{result.path}` in a separate replay config, then run "
                        "`scripts/run_service.py --config <replay.toml> --source "
                        "replay:<posts.jsonl> --allow-candidate-lexicon`.")


try:
    main()
except Exception as exc:  # no traceback (which may quote private text) to the logs
    if type(exc).__name__ in ("StopException", "RerunException"):
        raise
    st.error(f"The review page stopped ({type(exc).__name__}); reload it.")
