"""PR #71 should-fix items: private files after start (invented text, this machine's file system).

* A hard-linked private file (link count above 1) is refused: the review log at start and at every
  append, checked with ``fstat`` on the open descriptor; every other private write (lexicon,
  backups, decisions, candidate) at the moment of writing.
* No time-of-check / time-of-use gap: each private write opens the file relative to a descriptor
  of its checked folder with ``O_NOFOLLOW``, re-runs the private-path check on the resolved path,
  and verifies the folder is still the one it holds (device and inode), before and after opening.
  A folder swapped for a symlink into this program's repository between two writes is refused and
  nothing is written inside the repository.
* A refused append is a write failure like any other: an alert by code only, the service carries
  on, the hold itself is unaffected.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from runtime_harness import MAPPING, Runtime, post
from test_vocab_review_followup import repo
from test_vocab_review_service import UNKNOWN_WORD, opens

from tradingbot.runtime import notify as N
from tradingbot.runtime import vocab_review as V
from tradingbot.runtime.config import ConfigError

NOW_ITEM = V.BatchItem("zorbly", V.ACCEPT, V.INSTRUCTION_PHRASE)


@pytest.fixture
def code(tmp_path, monkeypatch):
    """An invented repository standing in for this program's own checkout."""
    c = repo(tmp_path / "code")
    (c / "sub").mkdir()
    monkeypatch.setattr(V, "REPO_ROOT", c)
    return c


def private(tmp_path: Path) -> Path:
    d = tmp_path / "owner-private"
    d.mkdir(mode=0o700)
    return d


def record(n: int) -> dict:
    return {"source_id": str(n), "post_id": str(n), "revision": 1, "action_id": "a1",
            "reason_code": "ENTRY_ANCHOR_CONTEXT_UNCLEAR", "reason_detail": ["WORDS:1"],
            "tokens": [{"token": "zorbly", "kind": "UNKNOWN_WORD"}], "sentences": []}


def nothing_private_in(folder: Path) -> bool:
    return all(b"zorbly" not in f.read_bytes() for f in folder.rglob("*") if f.is_file()
               and ".git" not in f.parts)


# ------------------------------------------------------------------ hard links


def test_a_hard_linked_review_log_is_refused_at_start(code, tmp_path):
    log = private(tmp_path) / "review.jsonl"
    log.write_text("")
    log.chmod(0o600)
    os.link(log, code / "sub" / "copy.jsonl")
    with pytest.raises(V.PrivatePathError, match="hard-linked"):
        V.ReviewLog(log)
    (tmp_path / "a").mkdir()
    with pytest.raises(ConfigError, match="hard-linked"):
        Runtime(tmp_path / "a", [], mapping_file=MAPPING,
                interpreter={"review_log_path": str(log)})


def test_a_review_log_hard_linked_after_start_is_refused_at_append(code, tmp_path):
    log = private(tmp_path) / "review.jsonl"
    w = V.ReviewLog(log)
    assert w.append(record(1))
    os.link(log, code / "sub" / "copy.jsonl")
    with pytest.raises(V.PrivatePathError, match="hard-linked"):
        w.append(record(2))
    assert len(log.read_text().splitlines()) == 1  # nothing more reached the linked file
    w.close()


def test_through_the_service_a_refused_append_alerts_by_code_and_the_hold_stands(code,
                                                                                tmp_path):
    log = private(tmp_path) / "review.jsonl"
    (tmp_path / "a").mkdir()
    rt = Runtime(tmp_path / "a", [post("1", 0, UNKNOWN_WORD)], mapping_file=MAPPING,
                 interpreter={"review_log_path": str(log)})
    os.link(log, code / "sub" / "copy.jsonl")
    rt.run(3, 30)
    assert [a["status"] for a in opens(rt)] == ["HOLD"]
    [(_, _title, message)] = [s for s in rt.notifier.sent if s[0] == N.REVIEW_LOG_WRITE_FAILED]
    assert message.startswith("REVIEW_LOG_WRITE_FAILED") and "zorbly" not in message
    assert nothing_private_in(code) and log.read_text() == ""
    rt.service.close()


@pytest.mark.parametrize("which", ["lexicon", "decisions", "candidate"])
def test_every_other_private_write_refuses_a_hard_linked_file(code, tmp_path, which):
    folder = private(tmp_path)
    lex = folder / "lexicon.json"
    lex.write_text(json.dumps({"entry_anchor": {}}))
    lex.chmod(0o600)
    target = {"lexicon": lex, "decisions": V.decisions_path(lex),
              "candidate": V.default_candidate_path(lex)}[which]
    if not target.exists():
        target.write_text(json.dumps({V.CANDIDATE_KEY: {}} if which == "candidate" else {}))
        target.chmod(0o600)
    os.link(target, code / "sub" / f"{which}.json")
    before = (code / "sub" / f"{which}.json").read_bytes()
    if which == "candidate":
        r = V.write_candidate(lex, [NOW_ITEM], target)
    elif which == "decisions":
        r = V.apply_batch(lex, [V.BatchItem("zorbex", V.KEEP_HOLDING, V.AMBIGUOUS)])
    else:
        r = V.apply_batch(lex, [NOW_ITEM])
    assert r.status == "REFUSED", r
    assert (code / "sub" / f"{which}.json").read_bytes() == before
    assert nothing_private_in(code)


# ------------------------------------------------------------------ symlink swap after start


def swap_for_symlink(folder: Path, into: Path) -> Path:
    moved = folder.with_name(folder.name + "-moved")
    folder.rename(moved)
    folder.symlink_to(into)
    return moved


def test_a_folder_swapped_for_a_symlink_into_the_repository_is_refused(code, tmp_path):
    folder = private(tmp_path)
    log = folder / "review.jsonl"
    w = V.ReviewLog(log)
    assert w.append(record(1))
    moved = swap_for_symlink(folder, code / "sub")
    with pytest.raises(V.PrivatePathError):
        w.append(record(2))
    assert not (code / "sub" / "review.jsonl").exists() and nothing_private_in(code)
    assert len((moved / "review.jsonl").read_text().splitlines()) == 1
    w.close()


def test_through_the_service_a_swapped_folder_alerts_and_writes_nothing_in_the_repo(code,
                                                                                   tmp_path):
    folder = private(tmp_path)
    log = folder / "review.jsonl"
    (tmp_path / "a").mkdir()
    rt = Runtime(tmp_path / "a", [post("1", 0, UNKNOWN_WORD)], mapping_file=MAPPING,
                 interpreter={"review_log_path": str(log)})
    swap_for_symlink(folder, code / "sub")
    rt.run(3, 30)
    assert [a["status"] for a in opens(rt)] == ["HOLD"]
    assert N.REVIEW_LOG_WRITE_FAILED in rt.notifier.kinds()
    assert nothing_private_in(code)
    rt.service.close()


def test_a_lexicon_folder_swapped_during_a_batch_is_refused(code, tmp_path, monkeypatch):
    folder = private(tmp_path)
    lex = folder / "lexicon.json"
    lex.write_text(json.dumps({"entry_anchor": {}}))
    lex.chmod(0o600)
    real = V.loader_problem
    swapped = []

    def swap_during(path, **kw):
        out = real(path, **kw)
        if not swapped:  # after the first validation, before the write
            swapped.append(swap_for_symlink(folder, code / "sub"))
        return out

    monkeypatch.setattr(V, "loader_problem", swap_during)
    r = V.apply_batch(lex, [NOW_ITEM])
    assert r.status == "REFUSED"
    assert nothing_private_in(code)
    assert json.loads((swapped[0] / "lexicon.json").read_text()) == {"entry_anchor": {}}


# ------------------------------------------------------------------ the normal case


def test_the_normal_case_is_unaffected(code, tmp_path):
    folder = private(tmp_path)
    log = folder / "review.jsonl"
    w = V.ReviewLog(log)
    for n in range(5):
        assert w.append(record(n))
    assert len(log.read_text().splitlines()) == 5
    w.close()
    w = V.ReviewLog(log)  # accepted again at the next start
    assert not w.append(record(1)) and w.append(record(9))
    w.close()
    lex = folder / "lexicon.json"
    lex.write_text(json.dumps({"entry_anchor": {}}))
    lex.chmod(0o600)
    assert V.apply_batch(lex, [NOW_ITEM, V.BatchItem("zorbex", V.KEEP_HOLDING,
                                                     V.AMBIGUOUS)]).status == "WRITTEN"
    assert V.write_candidate(lex, [NOW_ITEM], V.default_candidate_path(lex)).status == "WRITTEN"
    assert V.undo_last(lex).status == "RESTORED"
    assert V.truncate_review_log(log, keep_last=2) == 4
    for f in folder.iterdir():
        assert os.stat(f).st_nlink == 1 and oct(os.stat(f).st_mode & 0o777) == "0o600", f.name
