"""Vocabulary review follow-up (invented text and invented repositories only).

1. Safer default: a word that held a post through the mention-with-context rule was seen next to a
   price or a trading word, so it may be another market. The plain batch proposes it as AMBIGUOUS
   (no list), and the page and the final table carry the warning "seen with a price or a trading
   word; may be another market", derived from the review log's evidence, never from a proposals
   file.
2. Path policy (Product Owner): a private file is refused inside this program's own repository
   (the main checkout and every linked worktree of it) and inside a bare repository; inside any
   OTHER git work tree it is allowed with a prominent warning (page banner, start-up event by
   class); when this program's repository cannot be determined, every git work tree is refused.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from runtime_harness import MAPPING, Runtime
from streamlit.testing.v1 import AppTest
from test_vocab_review_page import PAGE, confirm, local, plain, rendered, write_config  # noqa: F401
from test_vocab_review_service import opens, owner_lexicon, private_dir, run

from tradingbot.runtime import notify as N
from tradingbot.runtime import vocab_review as V

WARNING = "seen with a price or a trading word; may be another market"
NEAR_PRICE = "We are now long $TLT, 88.42 now. Stop 87.20. Zorbleflux sits near 12.40."


# ------------------------------------------------------------------ 1. the safer default


def held_near_price(tmp_path: Path) -> tuple[Path, Path]:
    log = private_dir(tmp_path) / "review.jsonl"
    lex = owner_lexicon(tmp_path)
    rt = run(tmp_path / "first", [NEAR_PRICE], review_log=log, lexicon=lex)
    [a] = opens(rt)
    rt.service.close()
    assert a["status"] == "HOLD" and "RULE:MENTION_WITH_CONTEXT" in a["mapping_reason"], a
    return log, lex


def test_a_word_seen_with_a_price_is_proposed_as_ambiguous_with_the_warning(tmp_path):
    log, lex = held_near_price(tmp_path)
    entries, _ = V.read_review_log(log)
    [prop] = V.proposals_from_log(entries, lexicon_path=lex)
    assert prop.token == "zorbleflux" and prop.kind == "PROPER_NOUN"
    assert prop.group == V.AMBIGUOUS and prop.family is None
    assert prop.market_warning
    # The untouched batch writes nothing for it, and accepting it unmoved is refused.
    before = lex.read_bytes()
    items = V.batch_items([prop], {})
    assert V.apply_batch(lex, items).status == "NOTHING" and lex.read_bytes() == before
    accepted = V.batch_items([prop], {V.proposal_id(prop): V.Verdict(V.ACCEPT, None, None, "")})
    r = V.apply_batch(lex, accepted)
    assert r.status == "NOTHING" and r.refused and lex.read_bytes() == before


def test_the_final_table_repeats_the_warning_when_the_owner_moves_the_word(tmp_path):
    log, lex = held_near_price(tmp_path)
    entries, _ = V.read_review_log(log)
    [prop] = V.proposals_from_log(entries, lexicon_path=lex)
    for group, family in ((V.NON_MARKET, None), (V.MARKET_NAME, "TLT")):
        items = V.batch_items([prop], {V.proposal_id(prop): V.Verdict(V.ACCEPT, group, family,
                                                                      "")})
        [row] = V.final_table(items)
        assert row["warning"] == WARNING


def test_a_mention_held_without_that_rule_carries_no_warning():
    entry = {"source_id": "1", "post_id": "1", "revision": 1, "action_id": "a1",
             "family": "TLT", "reason_code": "ENTRY_ANCHOR_CONTEXT_UNCLEAR",
             "reason_detail": ["WORDS:1"], "tokens": [{"token": "zorbly", "kind": "UNKNOWN_WORD"}],
             "sentences": [{"role": "LEVEL", "text": "Long $TLT zorbly, 88.42 now"}]}
    [prop] = V.proposals_from_log([entry])
    assert not prop.market_warning and prop.group == V.INSTRUCTION_PHRASE


def page_for(monkeypatch, tmp_path, log, lex) -> AppTest:
    (tmp_path / "cfg").mkdir()
    rt = Runtime(tmp_path / "cfg", [], mapping_file=MAPPING)
    rt.service.close()
    cfg = write_config(rt, tmp_path / "runtime.toml",
                       interpreter={"review_log_path": log, "lexicon_path": lex})
    monkeypatch.setenv("TRADINGBOT_RUNTIME_CONFIG", str(cfg))
    monkeypatch.delenv("TRADINGBOT_VOCAB_PROPOSALS", raising=False)
    at = AppTest.from_file(str(PAGE), default_timeout=60)
    at.run()
    assert not at.exception
    return at


def test_the_page_shows_the_warning_as_plain_text_and_the_untouched_batch_writes_nothing(
        monkeypatch, local, tmp_path):  # noqa: F811
    log, lex = held_near_price(tmp_path)
    at = page_for(monkeypatch, tmp_path, log, lex)
    assert WARNING in plain(at)
    pid = V.proposal_id(V.load_batch(log, lex, V.default_proposals_path(log))[0][0])
    assert at.selectbox(key=f"vr_group_{pid}").value == V.AMBIGUOUS
    before = lex.read_bytes()
    confirm(at).check().run()
    at.button(key="vr_apply").click().run()
    assert lex.read_bytes() == before
    # Moved to "not a market" and accepted: the final table repeats the warning.
    at.selectbox(key=f"vr_group_{pid}").set_value(V.NON_MARKET).run()
    at.radio(key=f"vr_verdict_{pid}").set_value(V.ACCEPT).run()
    [table] = at.dataframe
    assert table.value.to_dict("records")[0]["warning"] == WARNING


def test_a_proposals_file_cannot_remove_the_warning(monkeypatch, local, tmp_path):  # noqa: F811
    log, lex = held_near_price(tmp_path)
    V.default_proposals_path(log).write_text(json.dumps({
        "schema": V.PROPOSAL_SCHEMA, "token": "zorbleflux", "group": V.NON_MARKET,
        "meaning": "an invented person", "categories": {
            "families": ["TLT"], "reasons": ["ENTRY_ANCHOR_POST_UNRESOLVED_MARKET"]},
        "examples": [{"source_id": "1"}]}) + "\n")
    props, *_ = V.load_batch(log, lex, V.default_proposals_path(log))
    assert props[0].group == V.NON_MARKET and props[0].market_warning
    at = page_for(monkeypatch, tmp_path, log, lex)
    assert WARNING in plain(at)


# ------------------------------------------------------------------ 2. the path policy

GIT_ENV = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
           "GIT_AUTHOR_NAME": "invented", "GIT_AUTHOR_EMAIL": "invented@example.invalid",
           "GIT_COMMITTER_NAME": "invented", "GIT_COMMITTER_EMAIL": "invented@example.invalid"}


def git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, env=GIT_ENV)


def repo(path: Path) -> Path:
    path.mkdir(parents=True)
    git(path, "init", "-q")
    (path / "README").write_text("invented\n")
    git(path, "add", "README")
    git(path, "commit", "-qm", "invented")
    return path


@pytest.fixture
def code_repo(tmp_path, monkeypatch):
    """An invented repository standing in for this program's own checkout."""
    code = repo(tmp_path / "code")
    monkeypatch.setattr(V, "REPO_ROOT", code)
    return code


def test_inside_this_programs_main_checkout_is_refused(code_repo):
    with pytest.raises(V.PrivatePathError, match="this program's repository"):
        V.check_private_path(code_repo / "notes" / "review.jsonl", "review log")


def test_inside_a_linked_worktree_of_the_same_repository_is_refused(code_repo, tmp_path):
    git(code_repo, "worktree", "add", "-q", str(tmp_path / "code-wt"))
    with pytest.raises(V.PrivatePathError, match="this program's repository"):
        V.check_private_path(tmp_path / "code-wt" / "x" / "review.jsonl", "review log")
    # And the other way round: the program running from the linked worktree.
    V.REPO_ROOT = tmp_path / "code-wt"
    with pytest.raises(V.PrivatePathError, match="this program's repository"):
        V.check_private_path(code_repo / "x" / "review.jsonl", "review log")


def test_inside_another_repository_is_allowed_with_a_warning(code_repo, tmp_path):
    other = repo(tmp_path / "workspace")
    target = other / "private" / "review.jsonl"
    assert V.check_private_path(target, "review log") == target.resolve()
    assert V.git_tree_class(target) == "OTHER_GIT_WORK_TREE"
    assert V.git_tree_class(tmp_path / "plain" / "review.jsonl") is None


def test_inside_a_git_directory_is_still_refused(code_repo, tmp_path):
    other = repo(tmp_path / "workspace")
    with pytest.raises(V.PrivatePathError, match="git"):
        V.check_private_path(other / ".git" / "review.jsonl", "review log")


def test_a_bare_repository_made_by_git_is_refused(code_repo, tmp_path):
    bare = tmp_path / "store.git"
    git(tmp_path, "init", "-q", "--bare", str(bare))
    with pytest.raises(V.PrivatePathError, match="bare"):
        V.check_private_path(bare / "info" / "review.jsonl", "review log")


def test_when_this_programs_repository_is_unknown_every_git_tree_is_refused(tmp_path,
                                                                            monkeypatch):
    other = repo(tmp_path / "workspace")
    monkeypatch.setattr(V, "REPO_ROOT", tmp_path / "installed-package")  # no git checkout
    with pytest.raises(V.PrivatePathError, match="cannot be determined"):
        V.check_private_path(other / "private" / "review.jsonl", "review log")
    plain_dir = tmp_path / "plain" / "review.jsonl"
    assert V.check_private_path(plain_dir, "review log") == plain_dir.resolve()


def service_events(rt: Runtime, kind: str) -> list[str]:
    return [m for k, _, m in rt.notifier.sent if k == kind]


def test_a_review_log_in_another_repository_starts_with_an_event_by_class(code_repo, tmp_path):
    other = repo(tmp_path / "workspace")
    (other / "private").mkdir()
    log = other / "private" / "review.jsonl"
    (tmp_path / "a").mkdir()
    rt = Runtime(tmp_path / "a", [], mapping_file=MAPPING,
                 interpreter={"review_log_path": str(log)})
    rt.step()
    assert service_events(rt, N.PRIVATE_PATH_IN_OTHER_GIT_TREE) == [
        "PRIVATE_PATH_IN_OTHER_GIT_TREE review_log OTHER_GIT_WORK_TREE"]
    assert str(other) not in json.dumps(rt.notifier.sent)
    rt.service.close()


def test_another_repository_in_a_synced_folder_gets_both_warnings(code_repo, tmp_path,
                                                                  monkeypatch):
    other = repo(tmp_path / "workspace")
    (other / "private").mkdir()
    log = other / "private" / "review.jsonl"
    monkeypatch.setattr(V, "synced_folder_class", lambda p: "CLOUD_STORAGE")
    (tmp_path / "a").mkdir()
    rt = Runtime(tmp_path / "a", [], mapping_file=MAPPING,
                 interpreter={"review_log_path": str(log)})
    rt.step()
    assert service_events(rt, N.PRIVATE_PATH_IN_OTHER_GIT_TREE)
    assert service_events(rt, N.PRIVATE_PATH_IN_SYNCED_FOLDER)
    rt.service.close()


def test_the_page_banner_says_the_file_could_be_committed(monkeypatch, local, code_repo,  # noqa: F811
                                                          tmp_path):
    other = repo(tmp_path / "workspace")
    (other / "private").mkdir()
    log = other / "private" / "review.jsonl"
    lex = other / "private" / "lexicon.json"
    lex.write_text(json.dumps({"entry_anchor": {}}))
    lex.chmod(0o600)
    text = rendered(page_for(monkeypatch, tmp_path, log, lex))
    assert "PRIVATE_PATH_IN_OTHER_GIT_TREE" in text
    assert "could be committed to that repository by mistake" in text
    assert "cannot prevent it" in text
