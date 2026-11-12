"""Review fixes for the owner's vocabulary review (invented text only).

* The batch is bound to what the owner reviewed: a digest of the final table and the production
  lexicon; ``apply_reviewed`` recomputes it from the current files and refuses a change.
* S4: undo refuses when the lexicon is not the file this tool wrote (a hand edit), naming the
  backup.
* S5: bare repositories are refused; cloud-synced folders are classified, warned on the page and
  alerted at start by path class only.
* S6: a lexicon the loader refuses never puts its words on stderr, stdout, a log or an alert.
* S7: a candidate replay never shares a state directory with production.
* N1: a candidate is only ever a ``*.candidate.json`` file of its own.
* N4: a keep-holding word that could not be recorded is reported as failed.
* N6: nothing under ``src/`` pickles, deep-copies, copies or compares mapping decisions (they
  carry the private evidence).
"""

from __future__ import annotations

import ast
import json
import logging
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from runtime_harness import MAPPING, Runtime, post
from test_vocab_review_service import UNKNOWN_WORD, opens, owner_lexicon, private_dir

from tradingbot.runtime import notify as N
from tradingbot.runtime import vocab_review as V
from tradingbot.runtime.config import ConfigError

REPO = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 6, 1, 14, 0, tzinfo=UTC)


def item(token, verdict=V.ACCEPT, group=V.INSTRUCTION_PHRASE, family=None, meaning=""):
    return V.BatchItem(token=token, verdict=verdict, group=group, family=family, meaning=meaning)


def contains(blob: str | bytes, word: str) -> bool:
    """Case-insensitive (review N5): the word in any case."""
    text = blob.decode("utf-8", "replace") if isinstance(blob, bytes) else blob
    return word.lower() in text.lower()


def proposals_file(log: Path, tokens: list[tuple[str, str]]) -> Path:
    path = V.default_proposals_path(log)
    path.write_text("".join(json.dumps({"schema": V.PROPOSAL_SCHEMA, "token": t, "group": g,
                                        "evidence": {"posts": 1, "holds": 1}}) + "\n"
                            for t, g in tokens))
    return path


# ------------------------------------------------------------------ the reviewed batch


def test_the_digest_binds_the_final_table_and_the_production_lexicon(tmp_path):
    lex = owner_lexicon(tmp_path)
    a = V.batch_digest([item("zorbly"), item("blorp", V.PENDING)], lex)
    assert a == V.batch_digest([item("zorbly")], lex)  # undecided words are not in the table
    assert a != V.batch_digest([item("zorbly", meaning="filler")], lex)
    assert a != V.batch_digest([item("zorbly", group=V.NON_MARKET)], lex)
    assert a != V.batch_digest([item("zorbly"), item("glim", V.KEEP_HOLDING)], lex)
    lex.write_text(json.dumps({"entry_anchor": {"context_words": ["kindly"]}}))
    assert a != V.batch_digest([item("zorbly")], lex)


def test_apply_reviewed_recomputes_the_batch_and_refuses_a_change(tmp_path):
    lex = owner_lexicon(tmp_path)
    log = private_dir(tmp_path) / "review.jsonl"
    path = proposals_file(log, [("betaword", V.NON_MARKET), ("gammaword", V.NON_MARKET)])
    props, *_ = V.load_batch(log, lex, path)
    verdicts = {V.proposal_id(p): V.Verdict(V.ACCEPT, p.group, p.family, "")
                for p in props if p.token == "betaword"}
    digest = V.batch_digest(V.batch_items(props, verdicts), lex)
    # A new proposal appears: the reviewed batch is unchanged and only betaword is written.
    proposals_file(log, [("quibblewump", V.NON_MARKET), ("betaword", V.NON_MARKET),
                         ("gammaword", V.NON_MARKET)])
    r = V.apply_reviewed(lex, log, path, verdicts, digest)
    assert r.status == "WRITTEN" and r.added == {"non_market_words": ["betaword"]}
    # The accepted proposal changes group in the file: the batch changed, nothing written.
    V.undo_last(lex)
    before = lex.read_bytes()
    proposals_file(log, [("betaword", V.INSTRUCTION_PHRASE)])
    r = V.apply_reviewed(lex, log, path, verdicts, digest)
    assert r.status == "REFUSED" and "changed since you reviewed it" in r.message
    assert lex.read_bytes() == before


def test_apply_reviewed_refuses_after_a_hand_edit_of_the_lexicon(tmp_path):
    lex = owner_lexicon(tmp_path)
    log = private_dir(tmp_path) / "review.jsonl"
    path = proposals_file(log, [("betaword", V.NON_MARKET)])
    props, *_ = V.load_batch(log, lex, path)
    verdicts = {V.proposal_id(props[0]): V.Verdict(V.ACCEPT, V.NON_MARKET, None, "")}
    digest = V.batch_digest(V.batch_items(props, verdicts), lex)
    lex.write_text(json.dumps({"open_present": [], "entry_anchor": {"context_words": ["kindly"]}}))
    r = V.apply_reviewed(lex, log, path, verdicts, digest)
    assert r.status == "REFUSED" and "changed since you reviewed it" in r.message


def test_a_proposal_id_is_stable_and_names_its_identity():
    p = V.Proposal(token="zorbs", group=V.MARKET_NAME, family="TLT", kind="MARKET_NOUN")
    q = V.Proposal(token="zorbs", group=V.MARKET_NAME, family="TLT", kind="MARKET_NOUN", posts=9)
    assert V.proposal_id(p) == V.proposal_id(q)
    assert V.proposal_id(p) != V.proposal_id(V.Proposal(token="zorbs", group=V.NON_MARKET))
    assert "zorbs" not in V.proposal_id(p) and len(V.proposal_id(p)) >= 12


def test_a_proposal_with_a_malformed_family_is_unreadable(tmp_path):
    path = tmp_path / "p.jsonl"
    path.write_text(json.dumps({"schema": V.PROPOSAL_SCHEMA, "token": "x", "group": V.MARKET_NAME,
                                "family": "../../etc"}) + "\n")
    assert V.read_proposals(path) == ([], 1)


# ------------------------------------------------------------------ S4 undo after a hand edit


def test_undo_refuses_when_the_lexicon_was_edited_by_hand_and_names_the_backup(tmp_path):
    lex = owner_lexicon(tmp_path)
    r = V.apply_batch(lex, [item("zorbly")], now=NOW)
    data = json.loads(lex.read_text())
    data["entry_anchor"]["present_markers"] = ["as we speak"]
    lex.write_text(json.dumps(data))
    edited = lex.read_bytes()
    u = V.undo_last(lex)
    assert u.status == "REFUSED" and r.backup.name in u.message and "by hand" in u.message
    assert lex.read_bytes() == edited and r.backup.exists()


def test_undo_refuses_without_a_record_of_the_write(tmp_path):
    lex = owner_lexicon(tmp_path)
    V.apply_batch(lex, [item("zorbly")], now=NOW)
    V.decisions_path(lex).unlink()
    assert V.undo_last(lex).status == "REFUSED"


# ------------------------------------------------------------------ S5 bare repos, synced folders


def test_a_bare_repository_is_refused(tmp_path):
    bare = tmp_path / "store.git"
    (bare / "objects").mkdir(parents=True)
    (bare / "refs").mkdir()
    (bare / "HEAD").write_text("ref: refs/heads/main\n")
    with pytest.raises(V.PrivatePathError, match="bare"):
        V.check_private_path(bare / "hooks" / "review.jsonl", "review log")
    with pytest.raises(V.PrivatePathError, match="git"):
        V.check_private_path(tmp_path / "w" / ".git" / "review.jsonl", "review log")


@pytest.mark.parametrize(("rel", "cls"), [
    ("Library/CloudStorage/Dropbox-Invented/x/review.jsonl", "CLOUD_STORAGE"),
    ("Library/Mobile Documents/com~apple~CloudDocs/x/review.jsonl", "ICLOUD_DRIVE"),
    ("Dropbox/x/review.jsonl", "DROPBOX"),
    ("OneDrive - Invented/x/review.jsonl", "ONEDRIVE"),
    ("Google Drive/x/review.jsonl", "GOOGLE_DRIVE"),
    ("Documents/x/review.jsonl", "POSSIBLY_ICLOUD_DESKTOP_DOCUMENTS"),
])
def test_cloud_synced_folders_are_classified(rel, cls):
    assert V.synced_folder_class(Path.home() / rel) == cls


def test_a_local_folder_is_not_synced(tmp_path):
    assert V.synced_folder_class(tmp_path / "review.jsonl") is None


def test_a_synced_private_path_alerts_at_start_by_class_only(tmp_path, monkeypatch):
    log = private_dir(tmp_path) / "review.jsonl"
    monkeypatch.setattr(V, "synced_folder_class",
                        lambda p: "CLOUD_STORAGE" if Path(p).name == "review.jsonl" else None)
    (tmp_path / "a").mkdir()
    rt = Runtime(tmp_path / "a", [], mapping_file=MAPPING,
                 interpreter={"review_log_path": str(log)})
    rt.step()
    sent = [s for s in rt.notifier.sent if s[0] == N.PRIVATE_PATH_IN_SYNCED_FOLDER]
    assert sent and sent[0][2] == "PRIVATE_PATH_IN_SYNCED_FOLDER review_log CLOUD_STORAGE"
    assert str(log) not in json.dumps(rt.notifier.sent)
    rt.service.close()


def test_without_the_review_log_a_synced_lexicon_raises_nothing_new(tmp_path, monkeypatch):
    monkeypatch.setattr(V, "synced_folder_class", lambda p: "CLOUD_STORAGE")
    lex = owner_lexicon(tmp_path)
    (tmp_path / "a").mkdir()
    rt = Runtime(tmp_path / "a", [], mapping_file=MAPPING,
                 interpreter={"lexicon_path": str(lex)})
    rt.step()
    assert N.PRIVATE_PATH_IN_SYNCED_FOLDER not in rt.notifier.kinds()
    rt.service.close()


# ------------------------------------------------------------------ S6 loader errors carry no words


BAD_MARKER = "zorbleflux yesterday"  # an invented marker holding a disqualifying word


def bad_lexicon(tmp_path: Path) -> Path:
    lex = private_dir(tmp_path) / "bad.json"
    lex.write_text(json.dumps({"entry_anchor": {"present_markers": [BAD_MARKER]}}))
    lex.chmod(0o600)
    return lex


def test_a_refused_lexicon_raises_a_config_error_without_its_words(tmp_path):
    from tradingbot.runtime.service import load_lexicons

    with pytest.raises(ConfigError) as exc:
        load_lexicons(bad_lexicon(tmp_path))
    text = str(exc.value) + repr(exc.value)
    assert not contains(text, "zorbleflux") and "input_value" not in text
    assert "entry_anchor.present_markers" in text and "1 error" in text
    assert exc.value.__cause__ is None and exc.value.__suppress_context__


def test_the_service_entry_script_prints_no_word_of_a_refused_lexicon(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    lex = bad_lexicon(tmp_path)
    (tmp_path / "a").mkdir()
    with pytest.raises(ConfigError):
        Runtime(tmp_path / "a", [], mapping_file=MAPPING, interpreter={"lexicon_path": str(lex)})
    assert not contains(caplog.text, "zorbleflux")
    from test_vocab_review_page import write_config

    (tmp_path / "b").mkdir()
    rt = Runtime(tmp_path / "b", [], mapping_file=MAPPING)
    rt.service.close()
    cfg = write_config(rt, tmp_path / "runtime.toml", interpreter={"lexicon_path": lex})
    proc = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "run_service.py"), "--config", str(cfg), "--once"],
        capture_output=True, text=True, env={**os.environ, "PYTHONPATH": str(REPO / "src")},
        timeout=120, check=False)
    assert proc.returncode == 2, proc.stderr
    assert "refusing to start" in proc.stderr
    for blob in (proc.stdout, proc.stderr):
        assert not contains(blob, "zorbleflux") and "input_value" not in blob
    for f in (tmp_path / "b").rglob("*"):
        if f.is_file():
            assert not contains(f.read_bytes(), "zorbleflux"), f.name


def test_the_owners_page_still_gets_the_loaders_reason(tmp_path):
    assert "disqualifies a level" in (V.loader_problem(bad_lexicon(tmp_path)) or "")


# ------------------------------------------------------------------ S7 candidate state directories


def candidate(tmp_path: Path) -> Path:
    lex = owner_lexicon(tmp_path)
    cand = V.default_candidate_path(lex)
    assert V.write_candidate(lex, [item("zorbly")], cand).status == "WRITTEN"
    return cand


def test_a_candidate_replay_refuses_a_state_directory_production_used(tmp_path):
    cand = candidate(tmp_path)
    folder = tmp_path / "shared"
    folder.mkdir()
    prod = Runtime(folder, [post("1", 0, UNKNOWN_WORD)], mapping_file=MAPPING)
    prod.run(2, 30)
    prod.service.close()
    with pytest.raises(ConfigError, match="state directory"):
        Runtime(folder, [post("1", 0, UNKNOWN_WORD)], mapping_file=MAPPING,
                interpreter={"lexicon_path": str(cand)}, allow_candidate_lexicon=True)


def test_a_candidate_state_directory_is_marked_and_never_becomes_production(tmp_path):
    cand = candidate(tmp_path)
    folder = tmp_path / "experiment"
    folder.mkdir()
    rt = Runtime(folder, [post("1", 0, UNKNOWN_WORD)], mapping_file=MAPPING,
                 interpreter={"lexicon_path": str(cand)}, allow_candidate_lexicon=True)
    rt.run(3, 30)
    assert [a["status"] for a in opens(rt)] == ["EXECUTED"]
    assert (rt.config.paths.state_dir / V.CANDIDATE_RUN_MARKER).exists()
    rt.restart()  # an earlier candidate run's directory is accepted again
    rt.service.close()
    with pytest.raises(ConfigError, match="candidate"):
        Runtime(folder, [], mapping_file=MAPPING)  # a production run there is refused


# ------------------------------------------------------------------ N1 candidate targets


def test_a_candidate_is_only_written_to_its_own_candidate_file(tmp_path):
    lex = owner_lexicon(tmp_path)
    folder = lex.parent
    log = folder / "review.jsonl"
    log.write_text("")
    other = folder / "notes.candidate.json"
    other.write_text("{}")  # an existing file without the candidate marker
    for target in (log, V.decisions_path(lex), folder / "x.json", other, lex,
                   folder / f"{lex.name}.bak-20260601T140000000000Z"):
        r = V.write_candidate(lex, [item("zorbly")], target, now=NOW)
        assert r.status == "REFUSED", target.name
    assert other.read_text() == "{}" and log.read_text() == ""
    cand = V.default_candidate_path(lex)
    assert V.write_candidate(lex, [item("zorbly")], cand).status == "WRITTEN"
    assert V.write_candidate(lex, [item("glim")], cand).status == "WRITTEN"  # its own: replaced


# ------------------------------------------------------------------ N4 keep-holding failures


def test_a_keep_holding_word_that_could_not_be_recorded_is_reported_as_failed(tmp_path):
    lex = owner_lexicon(tmp_path)
    V.decisions_path(lex).mkdir()  # the decisions file cannot be written
    r = V.apply_batch(lex, [item("zorbex", V.KEEP_HOLDING, V.AMBIGUOUS)], now=NOW)
    assert r.status == "REFUSED" and r.kept_holding == [] and "not recorded" in r.message
    r = V.apply_batch(lex, [item("zorbly"), item("zorbex", V.KEEP_HOLDING, V.AMBIGUOUS)],
                      now=NOW)
    assert r.status == "WRITTEN" and r.kept_holding == [] and "not recorded" in r.message


# ------------------------------------------------------------------ N6 decisions are never copied


def _src_of(node, source: str) -> str:
    return (ast.get_source_segment(source, node) or "").lower()


def test_nothing_under_src_pickles_copies_or_compares_mapping_decisions():
    problems = []
    for f in (REPO / "src").rglob("*.py"):
        source = f.read_text()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [a.name for a in node.names] + [getattr(node, "module", "") or ""]
                if any(n.split(".")[0] in ("pickle", "dill", "shelve", "marshal") for n in names):
                    problems.append(f"{f.name}: imports a pickler")
            if isinstance(node, ast.Call):
                fn = _src_of(node.func, source)
                args = " ".join(_src_of(a, source) for a in node.args)
                if (fn.endswith(("deepcopy", "copy.copy", "model_copy", "dumps"))
                        and ("decision" in args or "decision" in fn.rsplit(".", 1)[0])):
                    problems.append(f"{f.name}:{node.lineno}: {fn}")
            if isinstance(node, ast.Compare) and any(
                    isinstance(op, (ast.Eq, ast.NotEq, ast.In, ast.NotIn)) for op in node.ops):
                for operand in (node.left, *node.comparators):
                    if isinstance(operand, (ast.Name, ast.Subscript)) and \
                            "decision" in _src_of(operand, source):
                        problems.append(f"{f.name}:{node.lineno}: compares a decision")
    assert problems == []


# ------------------------------------------------------------------ owner policy a to c


def test_a_plain_batch_refers_to_the_log_and_no_other_file_holds_a_sentence(tmp_path):
    lex = owner_lexicon(tmp_path)
    log_entries = [
        {"source_id": str(n), "post_id": str(n), "revision": 1, "action_id": "a1",
         "family": "TLT", "reason_code": "ENTRY_ANCHOR_CONTEXT_UNCLEAR",
         "reason_detail": ["WORDS:1"], "tokens": [{"token": "zorbly", "kind": "UNKNOWN_WORD"}],
         "sentences": [{"role": "LEVEL", "text": f"Sentenceonly long $TLT zorbly {n}"}]}
        for n in range(8)]
    [prop] = V.proposals_from_log(log_entries)
    assert prop.examples == [] and len(prop.example_refs) == V.MAX_EXAMPLES
    assert prop.example_refs[0] == {"source_id": "0", "action_id": "a1"}
    shown = V.example_sentences(prop, log_entries)
    assert len(shown) == V.MAX_EXAMPLES and all("Sentenceonly" in t for _, t in shown)
    [cand] = V.candidates(log_entries)
    assert len(cand.sentences) == V.MAX_EXAMPLES
    item_ = V.BatchItem(prop.token, V.ACCEPT, prop.group, prop.family, "a gloss")
    V.apply_batch(lex, [item_, V.BatchItem("zorbex", V.KEEP_HOLDING, V.AMBIGUOUS)], now=NOW)
    V.write_candidate(lex, [V.BatchItem("glim", V.ACCEPT, V.INSTRUCTION_PHRASE)],
                      V.default_candidate_path(lex), now=NOW)
    for f in lex.parent.iterdir():
        assert not contains(f.read_bytes(), "sentenceonly"), f.name


def test_the_same_hold_is_never_logged_twice(tmp_path):
    log = private_dir(tmp_path) / "review.jsonl"
    for folder in ("first", "replay-again"):
        (tmp_path / folder).mkdir()
        rt = Runtime(tmp_path / folder, [post("1", 0, UNKNOWN_WORD)], mapping_file=MAPPING,
                     interpreter={"review_log_path": str(log)})
        rt.run(3, 30)
        rt.service.close()
    assert len(log.read_text().splitlines()) == 1
    w = V.ReviewLog(log)
    record = json.loads(log.read_text())
    assert w.append(record) is False and w.append({**record, "revision": 2}) is True


def test_the_owner_can_truncate_the_review_log(tmp_path):
    log = private_dir(tmp_path) / "review.jsonl"
    w = V.ReviewLog(log)
    for n in range(10):
        w.append({"source_id": str(n), "action_id": "a1", "reason_code": "X", "tokens": []})
    assert V.truncate_review_log(log, keep_last=3) == 7
    assert [json.loads(x)["source_id"] for x in log.read_text().splitlines()] == ["7", "8", "9"]
    assert oct(log.stat().st_mode & 0o777) == "0o600"
    assert V.truncate_review_log(log, keep_last=3) == 0


# ------------------------------------------------------------------ S6 follow-up: unknown keys


@pytest.mark.parametrize("content", [
    {"zorbleflux": []},  # an unknown top-level key
    {"entry_anchor": {"zorbleflux": 1}},  # an unknown key in the entry-anchor section
    {"entry_anchor": {"family_aliases": {"TLT": {"zorbleflux": 1}}}},  # one level deeper
    {"entry_anchor": {"family_aliases": {"Zorbleflux": ["now"]}}},  # a private family key
    {"entry_anchor": {"family_aliases": {"Zorbleflux": "not a list"}}},
], ids=["top", "section", "deeper", "family-key", "family-key-type"])
def test_an_unknown_or_private_key_never_appears_in_a_lexicon_error(tmp_path, content):
    from test_vocab_review_page import write_config

    from tradingbot.runtime.service import load_lexicons

    lex = private_dir(tmp_path) / "bad.json"
    lex.write_text(json.dumps(content))
    lex.chmod(0o600)
    with pytest.raises(ConfigError) as exc:
        load_lexicons(lex)
    assert not contains(str(exc.value), "zorbleflux") and "input_value" not in str(exc.value)
    (tmp_path / "b").mkdir()
    rt = Runtime(tmp_path / "b", [], mapping_file=MAPPING)
    rt.service.close()
    cfg = write_config(rt, tmp_path / "runtime.toml", interpreter={"lexicon_path": lex})
    proc = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "run_service.py"), "--config", str(cfg), "--once"],
        capture_output=True, text=True, env={**os.environ, "PYTHONPATH": str(REPO / "src")},
        timeout=120, check=False)
    assert proc.returncode == 2 and "refusing to start" in proc.stderr
    for blob in (proc.stdout, proc.stderr):
        assert not contains(blob, "zorbleflux"), blob
    for f in (tmp_path / "b").rglob("*"):  # logs, the alert file, the journal
        if f.is_file():
            assert not contains(f.read_bytes(), "zorbleflux"), f.name
