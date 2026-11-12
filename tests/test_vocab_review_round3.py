"""PR #71 review fixes (invented text and invented repositories only).

Blocker 1: the warning "seen with a price or a trading word; may be another market" belongs to the
WORD, not to a (word, kind) pair. "The same word" is ``vocab_review.word_key``: case-insensitive,
edge punctuation and a leading ``#``/``$``/``@`` removed, a possessive ``'s`` removed, whitespace
collapsed (what the anchor rule and the mention detector compare). A proposal is warned when any
of its parts (split on spaces, ``/``, ``.``, ``-``) is a warned word: a phrase or compound holding
it is warned too. A plural is a DIFFERENT word for the detector and is not warned. Every proposal
of a warned word is one proposal, forced to AMBIGUOUS, whatever its kind, group or source.

Blocker 2: "inside this program's repository" is decided by FILE IDENTITY (device and inode, as
``os.path.samefile``) on this machine's file system: case variants, Unicode normalisation variants
and symlinked parents of the repository, its work trees and its git directory are refused.
"""

from __future__ import annotations

import json
import os
import unicodedata
from pathlib import Path

import pytest
from runtime_harness import MAPPING, Runtime, post
from test_vocab_review_followup import WARNING, git, repo
from test_vocab_review_service import opens, owner_lexicon, private_dir

from tradingbot.runtime import vocab_review as V

MENTION = "We are now long $TLT, 88.42 now. Stop 87.20. Zorbleflux sits near 12.40."
ALSO = "We are now long $TLT zorbleflux, 88.42 now. Stop 87.20."  # same word, context unclear


def run(folder: Path, texts, log, lex):
    folder.mkdir()
    rt = Runtime(folder, [post(str(i), i * 30, t) for i, t in enumerate(texts, 1)],
                 mapping_file=MAPPING,
                 interpreter={"review_log_path": str(log), "lexicon_path": str(lex)})
    rt.run(5, 30)
    out = [(a["status"], a["mapping_reason"]) for a in opens(rt)]
    rt.service.close()
    return out


def entry(n, code, detail, token, kind):
    return {"source_id": str(n), "post_id": str(n), "revision": 1, "action_id": "a1",
            "family": "TLT", "reason_code": code, "reason_detail": detail,
            "tokens": [{"token": token, "kind": kind}],
            "sentences": [{"role": "MENTION", "text": f"invented {token} sentence"}]}


MENTION_HOLD = ("ENTRY_ANCHOR_POST_UNRESOLVED_MARKET",
                ["RULE:MENTION_WITH_CONTEXT", "MENTION:PROPER_NOUN"])
UNCLEAR = ("ENTRY_ANCHOR_CONTEXT_UNCLEAR", ["WORDS:1"])


# ------------------------------------------------------------------ blocker 1: one word, one warning


def test_a_warned_word_is_one_ambiguous_proposal_with_all_its_kinds(tmp_path):
    log = private_dir(tmp_path) / "review.jsonl"
    lex = owner_lexicon(tmp_path)
    statuses = run(tmp_path / "r1", [MENTION, ALSO], log, lex)
    assert [s for s, _ in statuses] == ["HOLD", "HOLD"]
    entries, _ = V.read_review_log(log)
    props = [p for p in V.proposals_from_log(entries, lexicon_path=lex)
             if p.token == "zorbleflux"]
    assert len(props) == 1  # never a second, unwarned route to the same word
    [p] = props
    assert p.group == V.AMBIGUOUS and p.market_warning
    assert p.kind == "PROPER_NOUN, UNKNOWN_WORD" and p.posts == 2
    # Accepting it as plain instruction wording is the owner's deliberate choice, warned.
    items = V.batch_items([p], {V.proposal_id(p): V.Verdict(V.ACCEPT, V.INSTRUCTION_PHRASE,
                                                            None, "")})
    assert V.final_table(items)[0]["warning"] == WARNING


@pytest.mark.parametrize("variant", ["Zorbleflux's", "#Zorbleflux", "ZORBLEFLUX", "zorbleflux."])
def test_the_same_word_in_another_form_is_merged_and_warned(variant):
    entries = [entry(1, *MENTION_HOLD, "Zorbleflux", "PROPER_NOUN"),
               entry(2, *UNCLEAR, variant, "UNKNOWN_WORD")]
    [p] = V.proposals_from_log(entries)
    assert p.token == "zorbleflux" and p.market_warning and p.group == V.AMBIGUOUS


def test_a_plural_is_a_different_word_and_does_not_open_a_route(tmp_path):
    entries = [entry(1, *MENTION_HOLD, "Zorbleflux", "PROPER_NOUN"),
               entry(2, *UNCLEAR, "zorblefluxes", "UNKNOWN_WORD")]
    by = {p.token: p for p in V.proposals_from_log(entries)}
    assert by["zorbleflux"].market_warning and not by["zorblefluxes"].market_warning
    # The detector does not fold plurals: the plural as a context word leaves the mention held.
    log = private_dir(tmp_path) / "review.jsonl"
    lex = owner_lexicon(tmp_path)
    lex.write_text(json.dumps({"entry_anchor": {"context_words": ["zorblefluxes"]}}))
    [(status, reason)] = run(tmp_path / "r", [MENTION], log, lex)
    assert status == "HOLD" and "RULE:MENTION_WITH_CONTEXT" in reason


def test_a_phrase_or_compound_holding_a_warned_word_is_warned(tmp_path):
    log = private_dir(tmp_path) / "review.jsonl"
    lex = owner_lexicon(tmp_path)
    log.write_text(json.dumps(entry(1, *MENTION_HOLD, "Zorbleflux", "PROPER_NOUN")) + "\n")
    log.chmod(0o600)
    path = V.default_proposals_path(log)
    path.write_text("".join(json.dumps({
        "schema": V.PROPOSAL_SCHEMA, "token": t, "group": V.INSTRUCTION_PHRASE,
        "categories": {"reasons": ["ENTRY_ANCHOR_CONTEXT_UNCLEAR"]}}) + "\n"
        for t in ("kindly zorbleflux", "zorbleflux-wise", "glim")))
    props = {p.token: p for p in V.load_batch(log, lex, path)[0]}
    assert props["kindly zorbleflux"].market_warning
    assert props["zorbleflux-wise"].market_warning
    assert not props["glim"].market_warning


def test_a_proposals_file_cannot_preselect_a_list_for_a_warned_word(tmp_path):
    log = private_dir(tmp_path) / "review.jsonl"
    lex = owner_lexicon(tmp_path)
    log.write_text(json.dumps(entry(1, *MENTION_HOLD, "Zorbleflux", "PROPER_NOUN")) + "\n")
    log.chmod(0o600)
    path = V.default_proposals_path(log)
    path.write_text(json.dumps({"schema": V.PROPOSAL_SCHEMA, "token": "ZorbleFLUX",
                                "group": V.NON_MARKET, "kind": "UNKNOWN_WORD",
                                "categories": {"reasons": ["ENTRY_ANCHOR_CONTEXT_UNCLEAR"]}})
                    + "\n")
    [p] = V.load_batch(log, lex, path)[0]
    assert (p.token, p.group, p.market_warning) == ("zorbleflux", V.AMBIGUOUS, True)


def test_the_digest_covers_the_warning(tmp_path):
    lex = owner_lexicon(tmp_path)
    plain = V.BatchItem("zorbleflux", V.ACCEPT, V.NON_MARKET, None, "", False)
    warned = V.BatchItem("zorbleflux", V.ACCEPT, V.NON_MARKET, None, "", True)
    assert V.batch_digest([plain], lex) != V.batch_digest([warned], lex)


# ------------------------------------------------------------------ blocker 2: file identity

def folds_case(folder: Path) -> bool:
    probe = folder / "CaseProbe"
    probe.mkdir()
    try:
        return (folder / "caseprobe").exists()
    finally:
        probe.rmdir()


@pytest.fixture
def pub(tmp_path, monkeypatch):
    code = repo(tmp_path / "pub")
    (code / "vendor").mkdir()
    monkeypatch.setattr(V, "REPO_ROOT", code)
    return code


def case_variant(path: Path, part: str) -> Path:
    return Path(str(path).replace(f"/{part}", f"/{part.upper()}"))


def test_a_case_variant_of_the_programs_checkout_is_refused(pub, tmp_path):
    if not folds_case(tmp_path):
        pytest.skip("this file system does not fold case")
    for target in (case_variant(pub, "pub") / "zz.jsonl",
                   case_variant(pub, "pub") / "new-dir" / "zz.jsonl",
                   case_variant(pub / "vendor", "vendor") / "zz.jsonl"):
        with pytest.raises(V.PrivatePathError, match="this program's repository"):
            V.check_private_path(target, "review log")
    with pytest.raises(V.PrivatePathError):
        V.check_private_path(Path(str(pub) + "/.GIT") / "zz.jsonl", "review log")


def test_a_case_variant_of_a_linked_worktree_is_refused(pub, tmp_path, monkeypatch):
    if not folds_case(tmp_path):
        pytest.skip("this file system does not fold case")
    git(pub, "worktree", "add", "-q", str(tmp_path / "wt-linked"))
    monkeypatch.setattr(V, "REPO_ROOT", tmp_path / "wt-linked")  # run from the linked worktree
    for target in (case_variant(pub, "pub") / "zz.jsonl",
                   case_variant(tmp_path / "wt-linked", "wt-linked") / "zz.jsonl"):
        with pytest.raises(V.PrivatePathError, match="this program's repository"):
            V.check_private_path(target, "review log")


def test_a_nested_repository_inside_the_checkout_is_refused_from_a_linked_worktree(pub,
                                                                                    tmp_path,
                                                                                    monkeypatch):
    nested = pub / "vendor" / "other"
    nested.mkdir()
    git(nested, "init", "-q")
    git(pub, "worktree", "add", "-q", str(tmp_path / "wt-linked"))
    monkeypatch.setattr(V, "REPO_ROOT", tmp_path / "wt-linked")
    with pytest.raises(V.PrivatePathError, match="this program's repository"):
        V.check_private_path(nested / "zz.jsonl", "review log")


def test_a_symlinked_parent_into_the_checkout_is_refused(pub, tmp_path):
    link = tmp_path / "ln"
    link.symlink_to(pub / "vendor")
    with pytest.raises(V.PrivatePathError, match="this program's repository"):
        V.check_private_path(link / "zz.jsonl", "review log")


def test_a_unicode_normalisation_variant_of_the_checkout_is_refused(tmp_path, monkeypatch):
    nfc = unicodedata.normalize("NFC", "café-repo")
    nfd = unicodedata.normalize("NFD", nfc)
    assert nfc != nfd
    code = repo(tmp_path / nfc)
    if not (tmp_path / nfd).exists():
        pytest.skip("this file system does not fold Unicode normalisation")
    monkeypatch.setattr(V, "REPO_ROOT", code)
    with pytest.raises(V.PrivatePathError, match="this program's repository"):
        V.check_private_path(tmp_path / nfd / "zz.jsonl", "review log")


def test_a_genuinely_different_repository_is_still_allowed_with_the_warning(pub, tmp_path):
    other = repo(tmp_path / "workspace")
    target = other / "private" / "zz.jsonl"
    assert V.check_private_path(target, "review log") == target.resolve()
    assert V.git_tree_class(target) == "OTHER_GIT_WORK_TREE"
    if folds_case(tmp_path):
        upper = case_variant(other, "workspace") / "private" / "zz.jsonl"
        V.check_private_path(upper, "review log")  # a case variant of ANOTHER repo: allowed


def test_any_doubt_is_refused(pub, tmp_path, monkeypatch):
    other = repo(tmp_path / "workspace")

    def broken(*_a, **_k):
        raise OSError("invented stat failure")

    monkeypatch.setattr(V.os, "stat", broken)
    with pytest.raises(V.PrivatePathError):
        V.check_private_path(other / "zz.jsonl", "review log")


def test_the_real_checkout_is_refused_through_a_case_variant(tmp_path):
    """This program's own checkout on this machine (no file is read or written there)."""
    root = V.REPO_ROOT
    if not folds_case(tmp_path):
        pytest.skip("this file system does not fold case")
    variant = Path(str(root).replace(root.name, root.name.upper())) / "zz-invented.jsonl"
    with pytest.raises(V.PrivatePathError):
        V.check_private_path(variant, "review log")
    assert not variant.exists()
    assert os.path.samefile(root, Path(str(root).replace(root.name, root.name.upper())))
