"""The owner's vocabulary review module (``tradingbot.runtime.vocab_review``), invented text only.

* aggregating the private review log into candidates (token and kind, held posts, families,
  reason codes, the sentences to show), and the proposals file (``PROPOSAL_SCHEMA``) or a plain
  batch built from the log in the owner's four groups;
* one consolidated batch applied to the production lexicon through the real loader's validators:
  a refused word is reported with the loader's reason and left out; one atomic write, mode 0600,
  one timestamped backup, one undo; an applied batch is exactly the accepted proposals;
* the interpreter's own sections and every other key round-trip unchanged;
* a candidate (experimental) lexicon is a separate marked file the service refuses to load, and
  writing it never touches the production file;
* never a private file inside a git work tree or the repository.
"""

from __future__ import annotations

import json
import os
import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tradingbot.runtime import vocab_review as V

REPO = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 6, 1, 14, 0, tzinfo=UTC)

# An invented lexicon with the interpreter's own section, an unrelated key and every list of the
# entry-anchor section; all of it must survive a decision unchanged.
LEXICON = {
    "open_present": [r"\binvented entry verb\b"],
    "summary_headers": [r"^invented recap$"],
    "entry_anchor": {
        "present_markers": ["as we speak"],
        "family_aliases": {"TLT": ["long duration"]},
        "context_words": ["kindly"],
        "non_market_words": ["ophelia"],
    },
}


def lexicon(tmp_path: Path, content: dict | None = None) -> Path:
    folder = tmp_path / "private"
    folder.mkdir(exist_ok=True)
    p = folder / "lexicon.json"
    p.write_text(json.dumps(LEXICON if content is None else content, indent=2))
    p.chmod(0o600)
    return p


def entry(post: str, family: str, reason: str, tokens, sentences, at="2026-06-01T14:00:00+00:00"):
    return {"at": at, "post_id": post, "source_id": post, "revision": 1, "action_id": f"{post}:0",
            "family": family, "reason_code": reason.split(",")[0],
            "reason_detail": reason.split(",")[1:],
            "tokens": [{"token": t, "kind": k} for t, k in tokens],
            "sentences": [{"role": r, "text": s} for r, s in sentences]}


def write_log(tmp_path: Path, entries: list[dict], extra: str = "") -> Path:
    p = tmp_path / "private" / "review.jsonl"
    p.parent.mkdir(exist_ok=True)
    p.write_text("".join(json.dumps(e) + "\n" for e in entries) + extra)
    p.chmod(0o600)
    return p


ENTRIES = [
    entry("1", "TLT", "ENTRY_ANCHOR_CONTEXT_UNCLEAR,WORDS:1", [("zorbly", "UNKNOWN_WORD")],
          [("LEVEL", "We are now long $TLT zorbly, 88.42 now.")]),
    entry("2", "TLT", "ENTRY_ANCHOR_POST_UNRESOLVED_MARKET,MENTION:PROPER_NOUN",
          [("Quentin", "PROPER_NOUN")], [("MENTION", "Thanks to Quentin.")]),
    entry("3", "TLT", "ENTRY_ANCHOR_POST_UNRESOLVED_MARKET,MENTION:PROPER_NOUN",
          [("Quentin", "PROPER_NOUN")], [("MENTION", "Quentin agrees with us, says Quentin.")]),
    entry("3", "TLT", "ENTRY_ANCHOR_POST_UNRESOLVED_MARKET,MENTION:PROPER_NOUN",
          [("Quentin", "PROPER_NOUN")], [("MENTION", "Quentin agrees with us, says Quentin.")]),
    entry("4", "QOIL", "ENTRY_ANCHOR_CONTEXT_UNCLEAR,WORDS:1", [("zorbly", "UNKNOWN_WORD")],
          [("LEVEL", "Short $QOIL zorbly, 71.20 now.")]),
    entry("5", "QOIL", "ENTRY_ANCHOR_CONTEXT_UNCLEAR,WORDS:1", [("zorbly", "UNKNOWN_WORD")],
          [("LEVEL", "Short $QOIL zorbly, 71.25 now.")]),
]


def item(token, verdict=V.ACCEPT, group=V.INSTRUCTION_PHRASE, family=None, meaning=""):
    return V.BatchItem(token=token, verdict=verdict, group=group, family=family, meaning=meaning)


# ------------------------------------------------------------------ the review log and candidates


def test_candidates_are_grouped_by_word_and_kind_and_sorted_by_held_posts(tmp_path):
    log = write_log(tmp_path, ENTRIES, extra="not json\n")
    entries, unreadable = V.read_review_log(log)
    assert len(entries) == 6 and unreadable == 1
    zorbly, quentin = V.candidates(entries)
    assert (zorbly.token, zorbly.kind, zorbly.posts, zorbly.holds) == ("zorbly", "UNKNOWN_WORD", 3, 3)
    assert zorbly.families == ["QOIL", "TLT"]
    assert zorbly.reasons == ["ENTRY_ANCHOR_CONTEXT_UNCLEAR"]
    assert (quentin.token, quentin.posts, quentin.holds) == ("quentin", 2, 3)
    # Each sentence is kept once, with its role, for display.
    assert quentin.sentences == [("MENTION", "Thanks to Quentin."),
                                 ("MENTION", "Quentin agrees with us, says Quentin.")]


def test_a_word_already_in_the_lexicon_or_marked_a_different_market_is_not_offered(tmp_path):
    lex = lexicon(tmp_path, {"entry_anchor": {"context_words": ["zorbly"]}})
    entries, _ = V.read_review_log(write_log(tmp_path, ENTRIES))
    assert [c.token for c in V.candidates(entries, lexicon_path=lex)] == ["quentin"]
    r = V.apply_batch(lex, [item("Quentin", V.KEEP_HOLDING, V.AMBIGUOUS)], now=NOW)
    assert r.status == "RECORDED" and r.kept_holding == ["quentin"]
    assert V.candidates(entries, lexicon_path=lex) == []


def test_a_missing_review_log_reads_as_empty(tmp_path):
    assert V.read_review_log(tmp_path / "absent.jsonl") == ([], 0)


# ------------------------------------------------------------------ proposals


def proposal_line(**over) -> str:
    base = {"schema": V.PROPOSAL_SCHEMA, "token": "Zorbs", "group": V.MARKET_NAME,
            "family": "TLT", "meaning": "invented nickname of the bond fund", "kind": "MARKET_NOUN",
            "evidence": {"posts": 4, "holds": 5},
            "categories": {"families": ["TLT"], "reasons": ["ENTRY_ANCHOR_POST_UNRESOLVED_MARKET"]},
            "measured": {"holds_resolved": 4, "executable_gained": 3,
                         "false_executable_introduced": 0},
            "examples": [{"role": "MENTION", "text": "The zorbs look heavy."},
                         {"source_id": "7"}]}
    base.update(over)
    return json.dumps(base) + "\n"


def test_a_proposals_file_is_read_with_its_measured_change_and_example_references(tmp_path):
    log_entries = [entry("7", "TLT", "ENTRY_ANCHOR_POST_UNRESOLVED_MARKET,MENTION:MARKET_NOUN",
                         [("zorbs", "MARKET_NOUN")], [("MENTION", "Zorbs again: zorbs rally.")])]
    path = tmp_path / "proposals.jsonl"
    path.write_text(proposal_line()
                    + proposal_line(token="kindly", group=V.INSTRUCTION_PHRASE, measured=None)
                    + proposal_line(token="x", group="NOT_A_GROUP")
                    + proposal_line(token="y", schema="other/1") + "{broken\n")
    props, bad = V.read_proposals(path)
    assert bad == 3
    [zorbs, kindly] = props
    assert (zorbs.token, zorbs.group, zorbs.family, zorbs.posts, zorbs.holds) == \
        ("zorbs", V.MARKET_NAME, "TLT", 4, 5)
    assert zorbs.measured == {"holds_resolved": 4, "executable_gained": 3,
                              "false_executable_introduced": 0}
    assert zorbs.example_refs == [{"source_id": "7"}]  # a reference, resolved at display time
    assert V.example_sentences(zorbs, log_entries) == [("MENTION", "The zorbs look heavy."),
                                                       ("MENTION", "Zorbs again: zorbs rally.")]
    assert kindly.measured is None  # shown as "not measured"
    # A word already in the lexicon is not proposed again.
    lex = lexicon(tmp_path)  # holds "kindly" as a context word
    assert [p.token for p in V.read_proposals(path, lexicon_path=lex)[0]] == ["zorbs"]


def test_a_measured_change_must_be_public_counts(tmp_path):
    path = tmp_path / "proposals.jsonl"
    path.write_text(proposal_line(measured={"holds_resolved": "four"})
                    + proposal_line(token="b", measured={"note": True}))
    assert [p.measured for p in V.read_proposals(path)[0]] == [None, None]


def test_a_plain_batch_is_built_from_the_review_log_by_kind():
    log_entries = [
        *ENTRIES,
        entry("6", "TLT", "ENTRY_ANCHOR_POST_UNRESOLVED_MARKET,MENTION:MARKET_NOUN",
              [("bonds", "MARKET_NOUN")], [("MENTION", "We like bonds.")]),
        entry("7", "TLT", "ENTRY_ANCHOR_INSTRUMENT_UNCLEAR,PRECEDING_SENTENCE",
              [("blorp", "UNKNOWN_WORD")], [("PRECEDING", "Blorp blorp."), ("LEVEL", "88.4 now.")]),
        entry("8", "TLT", "ENTRY_ANCHOR_LEVEL_NOT_MARKET_QUOTE,LEVEL_KIND:AMBIGUOUS,"
              "FOLLOWING_NOT_UNDERSTOOD", [("zap", "NEIGHBOUR_WORD")],
              [("NEIGHBOUR", "Stop zap 87.2"), ("LEVEL", "Long $TLT, 88.4 now")]),
    ]
    by = {p.token: p for p in V.proposals_from_log(log_entries)}
    # A word that held a post as a market mention may be another market: always ambiguous,
    # with the warning (follow-up of PR #68; these lines predate the rule detail).
    assert by["quentin"].group == V.AMBIGUOUS and by["quentin"].market_warning
    assert (by["bonds"].group, by["bonds"].family) == (V.AMBIGUOUS, None)
    assert by["bonds"].market_warning and not by["zorbly"].market_warning
    assert by["zorbly"].group == V.INSTRUCTION_PHRASE  # unknown word of the instruction sentence
    assert by["blorp"].group == V.AMBIGUOUS and by["zap"].group == V.AMBIGUOUS
    assert all(p.meaning == "" and p.measured is None for p in by.values())
    assert by["zorbly"].families == ["QOIL", "TLT"] and by["zorbly"].posts == 3


# ------------------------------------------------------------------ the batch


def test_an_applied_batch_is_exactly_the_accepted_proposals_in_one_change(tmp_path):
    lex = lexicon(tmp_path)
    before = json.loads(lex.read_text())
    r = V.apply_batch(lex, [
        item("Zorbs", V.ACCEPT, V.MARKET_NAME, "TLT", meaning="nickname"),
        item("Quentin", V.ACCEPT, V.NON_MARKET),
        item("zorbly", V.ACCEPT, V.INSTRUCTION_PHRASE),
        item("blorp", V.REJECT, V.INSTRUCTION_PHRASE),
        item("glim", V.PENDING, V.INSTRUCTION_PHRASE),
        item("Zorbex", V.KEEP_HOLDING, V.AMBIGUOUS),
    ], now=NOW)
    assert r.status == "WRITTEN", r.message
    assert r.added == {"family_aliases.TLT": ["zorbs"], "non_market_words": ["quentin"],
                       "context_words": ["zorbly"]}
    assert r.kept_holding == ["zorbex"] and r.refused == []
    expected = json.loads(json.dumps(before))
    expected["entry_anchor"]["family_aliases"]["TLT"].append("zorbs")
    expected["entry_anchor"]["non_market_words"].append("quentin")
    expected["entry_anchor"]["context_words"].append("zorbly")
    assert json.loads(lex.read_text()) == expected  # every other key unchanged
    assert len(V.backups(lex)) == 1
    from tradingbot.runtime.service import load_lexicons
    interp, anchor = load_lexicons(lex)
    assert interp.open_present == LEXICON["open_present"]
    assert anchor.family_aliases["TLT"] == ["long duration", "zorbs"]
    # The meanings and the keep-holding word are kept privately beside the lexicon.
    decisions = json.loads(V.decisions_path(lex).read_text())
    assert {d["token"]: d["meaning"] for d in decisions["applied"]}["zorbs"] == "nickname"
    assert V.decided_words(lex) == {"zorbex"}
    assert stat.S_IMODE(V.decisions_path(lex).stat().st_mode) == 0o600


def test_applying_nothing_leaves_the_production_lexicon_byte_identical(tmp_path):
    lex = lexicon(tmp_path)
    before = lex.read_bytes()
    r = V.apply_batch(lex, [item("zorbly", V.PENDING), item("blorp", V.REJECT)], now=NOW)
    assert r.status == "NOTHING" and lex.read_bytes() == before
    assert V.backups(lex) == [] and not V.decisions_path(lex).exists()
    r = V.apply_batch(lex, [item("Kindly")], now=NOW)  # already present
    assert r.status == "NOTHING" and r.unchanged == ["kindly"] and lex.read_bytes() == before


def test_keep_holding_alone_changes_nothing_in_the_lexicon(tmp_path):
    lex = lexicon(tmp_path)
    before = lex.read_bytes()
    r = V.apply_batch(lex, [item("Zorbex", V.KEEP_HOLDING, V.AMBIGUOUS)], now=NOW)
    assert r.status == "RECORDED" and lex.read_bytes() == before and V.backups(lex) == []
    assert V.decisions_path(lex).parent == lex.parent
    assert V.decided_words(lex) == {"zorbex"}


@pytest.mark.parametrize(("group", "token", "family", "fragment"), [
    (V.NON_MARKET, "bonds", None, "market or instrument noun"),  # a market noun
    (V.NON_MARKET, "two words", None, "one to 1 words"),
    (V.INSTRUCTION_PHRASE, "above", None, "disqualifies a level"),  # a condition word
    (V.MARKET_NAME, "now", "TLT", "present-market marker"),
    (V.INSTRUCTION_PHRASE, "zorb42", None, "words of letters"),
    (V.MARKET_NAME, "zorbs", None, "needs the family"),
    (V.AMBIGUOUS, "blorp", None, "list chosen by the owner"),
])
def test_a_refused_word_shows_the_loaders_reason_and_the_rest_of_the_batch_is_written(
        tmp_path, group, token, family, fragment):
    lex = lexicon(tmp_path)
    before = lex.read_bytes()
    r = V.apply_batch(lex, [item(token, V.ACCEPT, group, family)], now=NOW)
    assert r.status == "NOTHING" and lex.read_bytes() == before
    [(word, reason)] = r.refused
    assert word == token and fragment in reason, reason
    assert V.backups(lex) == []
    assert sorted(p.name for p in lex.parent.iterdir()) == ["lexicon.json"]  # no temp file left
    r = V.apply_batch(lex, [item(token, V.ACCEPT, group, family), item("zorbly")], now=NOW)
    assert r.status == "WRITTEN" and r.added == {"context_words": ["zorbly"]}
    assert len(r.refused) == 1 and token not in json.loads(lex.read_text())["entry_anchor"].get(
        "non_market_words", [])


def test_an_unreadable_or_missing_lexicon_is_refused_and_left_alone(tmp_path):
    lex = lexicon(tmp_path)
    lex.write_text("{not json")
    assert V.apply_batch(lex, [item("zorbly")], now=NOW).status == "REFUSED"
    assert lex.read_text() == "{not json"
    r = V.apply_batch(tmp_path / "absent.json", [item("zorbly")], now=NOW)
    assert r.status == "REFUSED" and "does not exist" in r.message


def test_the_write_is_atomic_keeps_mode_0600_and_one_timestamped_backup(tmp_path):
    lex = lexicon(tmp_path)
    before = lex.read_bytes()
    r = V.apply_batch(lex, [item("zorbly"), item("glim")], now=NOW)
    assert r.status == "WRITTEN"
    assert stat.S_IMODE(lex.stat().st_mode) == 0o600
    [backup] = V.backups(lex)
    assert backup.parent == lex.parent and "20260601T140000" in backup.name
    assert backup.read_bytes() == before and r.backup == backup
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600


def test_a_failed_rename_leaves_the_lexicon_untouched_and_no_temp_file(tmp_path, monkeypatch):
    lex = lexicon(tmp_path)
    before = lex.read_bytes()

    def boom(*_a, **_k):
        raise OSError("invented rename failure")

    monkeypatch.setattr(V.os, "replace", boom)
    r = V.apply_batch(lex, [item("zorbly")], now=NOW)
    assert r.status == "REFUSED"
    assert lex.read_bytes() == before and V.backups(lex) == []
    assert not [p for p in lex.parent.iterdir() if ".tmp" in p.name]


def test_undo_restores_the_whole_batch_step_by_step(tmp_path):
    lex = lexicon(tmp_path)
    original = lex.read_bytes()
    V.apply_batch(lex, [item("zorbly"), item("Zorbex", V.KEEP_HOLDING, V.AMBIGUOUS)], now=NOW)
    middle = lex.read_bytes()
    V.apply_batch(lex, [item("Quentin", V.ACCEPT, V.NON_MARKET)], now=NOW.replace(second=5))
    assert len(V.backups(lex)) == 2
    r = V.undo_last(lex)
    assert r.status == "RESTORED" and lex.read_bytes() == middle
    assert stat.S_IMODE(lex.stat().st_mode) == 0o600
    assert V.undo_last(lex).status == "RESTORED" and lex.read_bytes() == original
    assert V.decided_words(lex) == set()  # that batch's keep-holding word is undone too
    assert V.undo_last(lex).status == "NOTHING_TO_UNDO"


# ------------------------------------------------------------------ experiments, never production


def test_a_candidate_is_a_separate_marked_file_and_production_is_untouched(tmp_path):
    lex = lexicon(tmp_path)
    before = lex.read_bytes()
    out = V.default_candidate_path(lex)
    r = V.write_candidate(lex, [item("zorbly"), item("blorp", V.REJECT)], out, now=NOW)
    assert r.status == "WRITTEN", r.message
    assert lex.read_bytes() == before and V.backups(lex) == []
    raw = json.loads(out.read_text())
    assert raw[V.CANDIDATE_KEY]["added"] == 1
    assert raw["entry_anchor"]["context_words"] == ["kindly", "zorbly"]
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    from tradingbot.runtime.service import load_lexicons
    with pytest.raises(V.CandidateLexiconRefused, match="CANDIDATE"):
        load_lexicons(out)
    _, anchor = load_lexicons(out, allow_candidate=True)  # an offline replay's explicit opt-in
    assert "zorbly" in anchor.context_words
    # A candidate never becomes production through the batch, and never overwrites it.
    assert V.apply_batch(out, [item("glim")], now=NOW).status == "REFUSED"
    assert V.write_candidate(lex, [item("glim")], lex, now=NOW).status == "REFUSED"
    assert lex.read_bytes() == before


# ------------------------------------------------------------------ never inside a work tree


def test_a_lexicon_inside_this_programs_repository_is_never_written(tmp_path, monkeypatch):
    tree = tmp_path / "tree"
    (tree / ".git").mkdir(parents=True)
    (tree / "sub").mkdir()
    monkeypatch.setattr(V, "REPO_ROOT", tree)  # an invented checkout of this program
    lex = tree / "sub" / "lexicon.json"
    lex.write_text(json.dumps(LEXICON))
    before = lex.read_bytes()
    r = V.apply_batch(lex, [item("zorbly")], now=NOW)
    assert r.status == "REFUSED" and "this program's repository" in r.message
    assert V.write_candidate(lex, [item("zorbly")], tmp_path / "c.json", now=NOW).status \
        == "REFUSED"
    assert V.write_candidate(lexicon(tmp_path), [item("zorbly")], tree / "c.json",
                             now=NOW).status == "REFUSED"
    assert lex.read_bytes() == before and V.backups(lex) == []


def test_private_paths_inside_the_repository_are_refused_and_other_trees_warned(tmp_path,
                                                                                 monkeypatch):
    with pytest.raises(V.PrivatePathError, match="repository"):
        V.check_private_path(REPO / "state" / "review.jsonl", "review log")
    tree = tmp_path / "w"
    (tree / ".git").mkdir(parents=True)  # ANOTHER repository's work tree
    target = tree / "deep" / "review.jsonl"
    assert V.check_private_path(target, "review log") == target.resolve()  # allowed, warned
    assert V.git_tree_class(target) == "OTHER_GIT_WORK_TREE"
    broken = tmp_path / "w3"
    broken.mkdir()
    (broken / ".git").write_text("gitdir: /elsewhere\n")  # its git directory cannot be found
    with pytest.raises(V.PrivatePathError, match="cannot be read"):  # any doubt refuses
        V.check_private_path(broken / "deep" / "review.jsonl", "review log")
    monkeypatch.setattr(V, "REPO_ROOT", tree)  # the program runs from that same work tree
    with pytest.raises(V.PrivatePathError, match="this program's repository"):
        V.check_private_path(target, "review log")
    monkeypatch.setattr(V, "REPO_ROOT", tmp_path / "w2")
    (tmp_path / "w2").mkdir()
    (tmp_path / "w2" / ".git").write_text("gitdir: /elsewhere\n")  # its own git dir is missing
    with pytest.raises(V.PrivatePathError, match="cannot be determined"):
        V.check_private_path(target, "review log")
    ok = V.check_private_path(tmp_path / "private" / "review.jsonl", "review log")
    assert ok == (tmp_path / "private" / "review.jsonl").resolve()


def test_the_review_log_file_is_created_0600_and_a_readable_one_is_refused(tmp_path):
    p = tmp_path / "private" / "review.jsonl"
    p.parent.mkdir()
    log = V.ReviewLog(p)
    log.append({"post_id": "1"})
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    assert json.loads(p.read_text()) == {"post_id": "1"}
    os.chmod(p, 0o640)
    with pytest.raises(V.PrivatePathError, match="group- or world-readable"):
        V.ReviewLog(p)
    with pytest.raises(V.PrivatePathError, match="group- or world-readable"):
        log.append({"post_id": "2"})
    link = tmp_path / "private" / "link.jsonl"
    link.symlink_to(p)
    with pytest.raises(V.PrivatePathError):
        V.ReviewLog(link)


def test_the_module_never_prints_or_logs(tmp_path, capsys, caplog):
    caplog.set_level("DEBUG")
    lex = lexicon(tmp_path)
    entries, _ = V.read_review_log(write_log(tmp_path, ENTRIES))
    V.candidates(entries, lexicon_path=lex)
    V.proposals_from_log(entries, lexicon_path=lex)
    V.apply_batch(lex, [item("zorbly"), item("bonds", V.ACCEPT, V.NON_MARKET)], now=NOW)
    V.write_candidate(lex, [item("glim")], V.default_candidate_path(lex), now=NOW)
    V.undo_last(lex)
    out = capsys.readouterr()
    assert out.out == "" and out.err == "" and caplog.text == ""
