"""The private review log (``[interpreter] review_log_path``, default off), invented text only.

* With the key unset nothing changes: no evidence is collected and every journal, alert, log and
  heartbeat file is identical to a run with the key set.
* The private evidence travels on the mapping decision in an attribute no serialisation reads; a
  journaled decision carrying it leaves the words and sentences nowhere but the private log.
* The log path must lie outside the repository and every git work tree; an existing file that is
  group- or world-readable is refused at start; a write failure raises an alert by code only and
  never stops the service.
* Each owner decision, applied through the review module, changes the outcome of an invented held
  post on a re-run of the mock service.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest
from runtime_harness import MAPPING, Runtime, post

from tradingbot.runtime import notify as N
from tradingbot.runtime import vocab_review as V
from tradingbot.runtime.config import ConfigError
from tradingbot.runtime.mapping import MappingPolicy
from tradingbot.runtime.service import load_lexicons

REPO = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 6, 1, 14, 0, tzinfo=UTC)

# Invented posts for the shipped TLT entry rule (SINGLE_MARKET_POST).
CLEAN = "We are now long $TLT, 88.42 now. Stop 87.20."
UNKNOWN_WORD = "We are now long $TLT zorbly, 88.42 now. Stop 87.20."  # lower-case, instruction
PERSON = "We are now long $TLT, 88.42 now. Stop 87.20. Thanks to Quentin."  # capitalised word
OTHER_MARKET = "We are now long $TLT, 88.42 now. Stop 87.20. Watching Zorbex too."
FAMILY_WORD = "We are now long $TLT, 88.42 now. Stop 87.20. We like bonds here."  # a market noun
PRIVATE = ("zorbly", "Quentin", "Zorbex", "Thanks to Quentin", "We are now long $TLT zorbly")


def private_dir(tmp_path: Path) -> Path:
    d = tmp_path / "owner-private"
    d.mkdir(exist_ok=True)
    return d


def run(folder: Path, texts: list[str], *, review_log: Path | None = None,
        lexicon: Path | None = None, cycles: int = 3) -> Runtime:
    folder.mkdir(parents=True, exist_ok=True)
    interpreter = {}
    if review_log is not None:
        interpreter["review_log_path"] = str(review_log)
    if lexicon is not None:
        interpreter["lexicon_path"] = str(lexicon)
    kw = {"interpreter": interpreter} if interpreter else {}
    rt = Runtime(folder, [post(str(i), i * 30, t) for i, t in enumerate(texts, 1)],
                 mapping_file=MAPPING, **kw)
    rt.run(cycles, 30)
    return rt


def opens(rt: Runtime) -> list[dict]:
    return [a for a in rt.actions() if a["action"] == "OPEN"]


def dump_db(path: Path) -> list:
    con = sqlite3.connect(path)
    try:
        out = []
        for (name,) in con.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY 1"):
            out.append((name, con.execute(f"SELECT * FROM {name}").fetchall()))
        return out
    finally:
        con.close()


def state_snapshot(rt: Runtime) -> dict[str, str]:
    """Every file of the state directory (SQLite files as their rows), its folder name masked."""
    out = {}
    for f in sorted(rt.config.paths.state_dir.iterdir()):
        if not f.is_file() or f.name.endswith(("-wal", "-shm", "-journal")):
            continue
        try:
            content = repr(dump_db(f))
        except sqlite3.DatabaseError:
            content = f.read_bytes().decode("utf-8", "replace")
        out[f.name] = content.replace(str(rt.folder), "<dir>")
    return out


def files_containing(root: Path, needles, skip: set[Path]) -> list[str]:
    hits = []
    for f in root.rglob("*"):
        if not f.is_file() or f.resolve() in skip:
            continue
        blob = f.read_bytes().lower()  # review N5: any case
        hits += [f"{f.name}: {n}" for n in needles if n.lower().encode() in blob]
    return hits


# ------------------------------------------------------------------ key unset: unchanged


def test_with_the_key_unset_no_evidence_is_collected(tmp_path):
    rt = run(tmp_path / "a", [UNKNOWN_WORD])
    assert rt.service.mapping.collect_vocab_evidence is False
    assert rt.service.review_log is None
    assert [a["status"] for a in opens(rt)] == ["HOLD"]
    rt.service.close()
    policy = MappingPolicy.from_toml(MAPPING)
    assert policy.collect_vocab_evidence is False


def test_the_key_changes_no_journal_alert_log_or_heartbeat_byte(tmp_path):
    texts = [CLEAN, UNKNOWN_WORD, PERSON, OTHER_MARKET]
    off = run(tmp_path / "off", texts, cycles=6)
    log = private_dir(tmp_path) / "review.jsonl"
    on = run(tmp_path / "on", texts, review_log=log, cycles=6)
    a, b = state_snapshot(off), state_snapshot(on)
    assert a.keys() == b.keys()
    for name in a:
        assert a[name] == b[name], name
    assert off.notifier.sent == on.notifier.sent
    assert len(log.read_text().splitlines()) == 3  # the three vocabulary holds
    off.service.close()
    on.service.close()


# ------------------------------------------------------------------ the evidence never serialises


def test_the_evidence_is_on_the_decision_but_in_no_serialisation(tmp_path):
    rt = run(tmp_path / "a", [UNKNOWN_WORD], review_log=private_dir(tmp_path) / "review.jsonl")
    policy = rt.service.mapping
    interpreted = rt.service.interpreter.interpret(post("9", 0, UNKNOWN_WORD))
    rt.service.close()
    from tradingbot.tkl_interpreter import group_linked_actions

    _, decisions = policy.apply(
        group_linked_actions(interpreted),
        clauses={c.action_id: c.clause for c in interpreted.classifications},
        now=NOW, post_text=UNKNOWN_WORD,
        levels={c.action_id: [(m.role.value, m.value) for m in c.prices]
                for c in interpreted.classifications})
    [d] = [d for d in decisions if d.status == "UNRESOLVED"]
    ev = d.vocab_evidence
    assert ("zorbly", "UNKNOWN_WORD") in ev.tokens
    assert any("zorbly" in text for _, text in ev.sentences)
    for text in (d.model_dump_json(), json.dumps(d.model_dump(mode="json")), repr(d), str(d),
                 repr(ev), str(ev), json.dumps(d.model_dump(mode="python"), default=str),
                 d.model_copy().model_dump_json(), json.dumps(type(d).model_json_schema())):
        assert "zorbly" not in text and "TLT zorbly" not in text, text
    assert "vocab_evidence" not in type(d).model_fields


def test_a_journaled_held_decision_leaves_the_words_only_in_the_private_log(tmp_path, caplog,
                                                                              capsys):
    caplog.set_level(logging.DEBUG)
    log = private_dir(tmp_path) / "review.jsonl"
    rt = run(tmp_path / "a", [UNKNOWN_WORD, PERSON, OTHER_MARKET], review_log=log)
    assert [a["status"] for a in opens(rt)] == ["HOLD"] * 3
    # The private log has the words and sentences.
    lines = [json.loads(x) for x in log.read_text().splitlines()]
    assert {t["token"] for x in lines for t in x["tokens"]} == {"zorbly", "Quentin", "Zorbex"}
    assert stat.S_IMODE(log.stat().st_mode) == 0o600
    # Nothing else does: journal rows, the exception register, alerts, logs, heartbeat, stdout.
    skip = {log.resolve(), rt.posts_file.resolve()}  # the private log and the invented input
    assert files_containing(tmp_path, PRIVATE, skip) == []
    out = capsys.readouterr()
    for needle in (n.lower() for n in PRIVATE):  # review N5: any case
        for blob in (caplog.text, out.out, out.err, json.dumps(rt.notifier.sent),
                     json.dumps(rt.actions(), default=str),
                     json.dumps(rt.store.events(), default=str)):
            assert needle not in blob.lower()
    # The journal still records the public reason code and kinds as before.
    reasons = {a["mapping_reason"] for a in opens(rt)}
    assert "ENTRY_ANCHOR_CONTEXT_UNCLEAR,WORDS:1" in reasons
    assert "ENTRY_ANCHOR_POST_UNRESOLVED_MARKET,MENTION:PROPER_NOUN" in reasons
    rt.service.close()


def test_the_log_line_names_the_post_the_family_the_reason_and_the_sentences(tmp_path):
    log = private_dir(tmp_path) / "review.jsonl"
    rt = run(tmp_path / "a", [UNKNOWN_WORD, CLEAN], review_log=log)
    [line] = [json.loads(x) for x in log.read_text().splitlines()]
    assert line["post_id"] == "1" and line["family"] == "TLT"
    assert line["reason_code"] == "ENTRY_ANCHOR_CONTEXT_UNCLEAR"
    assert line["reason_detail"] == ["WORDS:1"]
    assert line["tokens"] == [{"token": "zorbly", "kind": "UNKNOWN_WORD"}]
    assert {"role": "LEVEL", "text": "We are now long $TLT zorbly, 88.42 now"} in line["sentences"]
    assert line["at"].startswith("2026-")
    rt.service.close()


def test_a_neighbour_sentence_is_logged_only_when_words_could_resolve_it(tmp_path):
    log = private_dir(tmp_path) / "review.jsonl"
    texts = ["We are now long $TLT, 88.42 now. Stop 87.20 kindly.",  # words could fix it
             "We are now long $TLT, 88.42 now. The chart looks fine."]  # words cannot
    rt = run(tmp_path / "a", texts, review_log=log)
    reasons = sorted(a["mapping_reason"] for a in opens(rt))
    assert all("FOLLOWING_NOT_UNDERSTOOD" in r for r in reasons), reasons
    [line] = [json.loads(x) for x in log.read_text().splitlines()]
    assert line["post_id"] == "1"
    assert line["tokens"] == [{"token": "kindly", "kind": "NEIGHBOUR_WORD"}]
    assert {"role": "NEIGHBOUR", "text": "Stop 87.20 kindly"} in line["sentences"]
    rt.service.close()


# ------------------------------------------------------------------ path and permission refusals


def test_a_review_log_inside_the_repository_is_refused(tmp_path):
    with pytest.raises(ConfigError, match="repository"):
        run(tmp_path / "a", [], review_log=REPO / "state" / "review.jsonl", cycles=0)
    assert not (REPO / "state" / "review.jsonl").exists()


def test_a_review_log_inside_any_git_work_tree_is_refused(tmp_path):
    tree = tmp_path / "some-checkout"
    (tree / ".git").mkdir(parents=True)
    with pytest.raises(ConfigError, match="git work tree"):
        run(tmp_path / "a", [], review_log=tree / "logs" / "review.jsonl", cycles=0)


def test_a_group_or_world_readable_review_log_is_refused(tmp_path):
    log = private_dir(tmp_path) / "review.jsonl"
    log.write_text("")
    os.chmod(log, 0o644)
    with pytest.raises(ConfigError, match="group- or world-readable"):
        run(tmp_path / "a", [], review_log=log, cycles=0)


def test_a_write_failure_alerts_by_code_only_and_the_service_carries_on(tmp_path):
    log = private_dir(tmp_path) / "review.jsonl"
    rt = run(tmp_path / "a", [UNKNOWN_WORD], review_log=log, cycles=0)
    os.chmod(log, 0o644)  # someone widened it after start: the writer refuses to append
    rt.run(3, 30)
    assert [a["status"] for a in opens(rt)] == ["HOLD"]  # the post was still processed
    assert N.REVIEW_LOG_WRITE_FAILED in rt.notifier.kinds()
    [(_, title, message)] = [s for s in rt.notifier.sent if s[0] == N.REVIEW_LOG_WRITE_FAILED]
    assert message.startswith("REVIEW_LOG_WRITE_FAILED")
    for needle in (*PRIVATE, str(log)):
        assert needle not in title and needle not in message
    assert log.read_text() == ""
    assert rt.service.halted is False and rt.heartbeat()
    rt.service.close()


# ------------------------------------------------------------------ each decision, end to end


def owner_lexicon(tmp_path: Path) -> Path:
    lex = private_dir(tmp_path) / "lexicon.json"
    lex.write_text(json.dumps({"open_present": [], "entry_anchor": {}}))
    lex.chmod(0o600)
    return lex


def review_then_rerun(tmp_path: Path, text: str, verdict: str, group: str):
    """Hold, review the plain batch built from the log, apply the owner's verdict, re-run."""
    log = private_dir(tmp_path) / "review.jsonl"
    lex = owner_lexicon(tmp_path)
    first = run(tmp_path / "first", [text], review_log=log, lexicon=lex)
    assert [a["status"] for a in opens(first)] == ["HOLD"]
    first.service.close()
    entries, _ = V.read_review_log(log)
    [prop] = V.proposals_from_log(entries, lexicon_path=lex)
    assert prop.group == group  # the plain batch's proposed group
    r = V.apply_batch(lex, [V.BatchItem(prop.token, verdict, prop.group, prop.family)])
    assert r.status in ("WRITTEN", "RECORDED"), r.message
    cand = prop
    load_lexicons(lex)  # the real loader accepts the result
    second = run(tmp_path / "second", [text], review_log=log, lexicon=lex)
    statuses = [(a["status"], a["mapping_reason"]) for a in opens(second)]
    second.service.close()
    return statuses, lex, log, cand


def test_plain_instruction_wording_lets_the_held_post_trade(tmp_path):
    statuses, *_ = review_then_rerun(tmp_path, UNKNOWN_WORD, V.ACCEPT, V.INSTRUCTION_PHRASE)
    assert [s for s, _ in statuses] == ["EXECUTED"], statuses


def test_not_a_market_lets_the_held_post_trade(tmp_path):
    statuses, *_ = review_then_rerun(tmp_path, PERSON, V.ACCEPT, V.NON_MARKET)
    assert [s for s, _ in statuses] == ["EXECUTED"], statuses


def test_another_name_of_the_family_lets_the_held_post_trade(tmp_path):
    statuses, *_ = review_then_rerun(tmp_path, FAMILY_WORD, V.ACCEPT, V.MARKET_NAME)
    assert [s for s, _ in statuses] == ["EXECUTED"], statuses


def test_a_different_market_keeps_holding_and_is_not_offered_again(tmp_path):
    statuses, lex, log, _cand = review_then_rerun(tmp_path, OTHER_MARKET, V.KEEP_HOLDING,
                                                 V.NON_MARKET)
    assert [s for s, _ in statuses] == ["HOLD"]
    assert statuses[0][1] == "ENTRY_ANCHOR_POST_UNRESOLVED_MARKET,MENTION:PROPER_NOUN"
    assert json.loads(lex.read_text()) == {"open_present": [], "entry_anchor": {}}
    entries, _ = V.read_review_log(log)
    assert len(entries) == 1  # held twice, logged once (one record per hold)
    assert V.candidates(entries, lexicon_path=lex) == []
    assert V.proposals_from_log(entries, lexicon_path=lex) == []


# ------------------------------------------------------------------ experiment versus production


def test_a_candidate_lexicon_is_refused_by_the_service(tmp_path):
    lex = owner_lexicon(tmp_path)
    cand = V.default_candidate_path(lex)
    assert V.write_candidate(lex, [V.BatchItem("zorbly", V.ACCEPT, V.INSTRUCTION_PHRASE)],
                             cand).status == "WRITTEN"
    with pytest.raises(ConfigError, match="CANDIDATE"):
        run(tmp_path / "a", [UNKNOWN_WORD], lexicon=cand, cycles=0)
    from tradingbot.runtime.app import build_service
    rt = run(tmp_path / "b", [], lexicon=lex, cycles=0)
    rt.service.close()
    with pytest.raises(ConfigError, match="CANDIDATE"):
        build_service(rt.config.model_copy(update={"interpreter": rt.config.interpreter.model_copy(
            update={"lexicon_path": cand})}))


def test_only_an_offline_replay_may_opt_in_to_a_candidate_and_production_stays_held(tmp_path):
    lex = owner_lexicon(tmp_path)
    before = lex.read_bytes()
    cand = V.default_candidate_path(lex)
    V.write_candidate(lex, [V.BatchItem("zorbly", V.ACCEPT, V.INSTRUCTION_PHRASE)], cand)
    # The experiment: an offline replay loads the candidate explicitly and the post trades.
    folder = tmp_path / "experiment"
    folder.mkdir()
    rt = Runtime(folder, [post("1", 0, UNKNOWN_WORD)], mapping_file=MAPPING,
                 interpreter={"lexicon_path": str(cand)}, allow_candidate_lexicon=True)
    rt.run(3, 30)
    assert [a["status"] for a in opens(rt)] == ["EXECUTED"]
    rt.service.close()
    # Production is untouched and still holds the same post.
    assert lex.read_bytes() == before
    prod = run(tmp_path / "production", [UNKNOWN_WORD], lexicon=lex)
    assert [a["status"] for a in opens(prod)] == ["HOLD"]
    prod.service.close()
    # A live source may never opt in.
    from tradingbot.runtime.service import RuntimeService

    class LiveSource:
        name = "invented-live"

    with pytest.raises(ConfigError, match="replay-file source"):
        RuntimeService(prod.config, source=LiveSource(), broker=prod.broker,
                       allow_candidate_lexicon=True)
