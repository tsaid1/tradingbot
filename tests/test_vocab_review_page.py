"""The owner's Vocabulary Review page (``dashboard/pages/10_Vocabulary_Review.py``), driven
headlessly through ``streamlit.testing.v1.AppTest`` on a review log the runtime service wrote for
invented posts. It shows the batch in the four groups, applies it in one action (one backup, one
undo), writes a candidate without touching production, refuses to run off localhost, and keeps
private text out of the URL, the caches and every log."""

from __future__ import annotations

import ast
import json
import tomllib
from pathlib import Path

import pytest
from runtime_harness import MAPPING, Runtime, post, required_toml
from streamlit import config as st_config
from streamlit.testing.v1 import AppTest
from test_execution_page import shown

from tradingbot.runtime import vocab_review as V

REPO = Path(__file__).resolve().parents[1]
PAGE = REPO / "dashboard" / "pages" / "10_Vocabulary_Review.py"
TEXTS = [
    "We are now long $TLT zorbly, 88.42 now. Stop 87.20.",
    "We are now long $TLT, 88.42 now. Stop 87.20. Thanks to Quentin.",
    "We are now long $TLT, 88.42 now. Stop 87.20. We like bonds here.",
]


def write_config(rt: Runtime, path: Path, *, interpreter: dict | None) -> Path:
    c = rt.config
    text = (f'mode = "mock"\n[paths]\nstate_dir = "{c.paths.state_dir}"\n'
            f'halt_file = "{c.paths.halt_file}"\nmapping_file = "{MAPPING}"\n'
            f'[source]\nkind = "replay"\nfile = "{c.source.file}"\n' + required_toml())
    if interpreter:
        text += "[interpreter]\n" + "".join(f'{k} = "{v}"\n' for k, v in interpreter.items())
    path.write_text(text)
    tomllib.loads(text)
    return path


@pytest.fixture
def local(monkeypatch):
    before = st_config.get_option("server.address")
    st_config.set_option("server.address", "127.0.0.1")
    yield
    st_config.set_option("server.address", before)


@pytest.fixture
def held(tmp_path):
    private = tmp_path / "owner-private"
    private.mkdir()
    log = private / "review.jsonl"
    lex = private / "lexicon.json"
    lex.write_text(json.dumps({"open_present": [], "entry_anchor": {}}))
    lex.chmod(0o600)
    (tmp_path / "run").mkdir()
    rt = Runtime(tmp_path / "run", [post(str(i), i * 30, t) for i, t in enumerate(TEXTS, 1)],
                 mapping_file=MAPPING,
                 interpreter={"review_log_path": str(log), "lexicon_path": str(lex)})
    rt.run(5, 30)
    rt.service.close()
    cfg = write_config(rt, tmp_path / "runtime.toml",
                       interpreter={"review_log_path": log, "lexicon_path": lex})
    return cfg, lex, log


def page(monkeypatch, cfg: Path) -> AppTest:
    monkeypatch.setenv("TRADINGBOT_RUNTIME_CONFIG", str(cfg))
    monkeypatch.delenv("TRADINGBOT_VOCAB_PROPOSALS", raising=False)
    at = AppTest.from_file(str(PAGE), default_timeout=60)
    at.run()
    assert not at.exception
    return at


def test_the_page_shows_the_batch_in_four_groups_with_its_evidence(monkeypatch, local, held):
    cfg, _lex, _log = held
    at = page(monkeypatch, cfg)
    text = shown(at)
    for needle in ("Vocabulary review (OWNER ONLY)", "privately, here on your machine only",
                   "restarted", *V.GROUPS.values(), "plain batch built from the review log",
                   "not measured", "zorbly", "Quentin", "Thanks to Quentin",
                   "Vocabulary holds logged 3", "Pending proposals 3"):
        assert needle in text, needle
    labels = [e.label for e in at.expander]
    assert any(lb.startswith("quentin:") for lb in labels)
    assert any(lb.startswith("bonds:") for lb in labels)


def test_the_batch_is_applied_in_one_action_and_undone(monkeypatch, local, held):
    cfg, lex, _log = held
    at = page(monkeypatch, cfg)
    keys = {e.label.split(":")[0]: n for n, e in enumerate(at.expander)}
    order = [k.key.rsplit("_", 1)[1] for k in at.radio if k.key.startswith("vr_verdict_")]
    by_token = {tok: order[n] for tok, n in keys.items()}
    at.radio(key=f"vr_verdict_{by_token['zorbly']}").set_value(V.ACCEPT).run()
    at.text_input(key=f"vr_meaning_{by_token['zorbly']}").input("invented filler word").run()
    at.radio(key=f"vr_verdict_{by_token['quentin']}").set_value(V.ACCEPT).run()
    at.radio(key=f"vr_verdict_{by_token['bonds']}").set_value(V.KEEP_HOLDING).run()
    at.button(key="vr_apply").click().run()
    assert "Not applied" in shown(at)  # the confirmation is required
    assert json.loads(lex.read_text()) == {"open_present": [], "entry_anchor": {}}
    at.checkbox(key="vr_confirm").check().run()
    at.button(key="vr_apply").click().run()
    text = shown(at)
    assert "WRITTEN" in text and "2 word(s) added, 1 kept holding, 0 refused" in text
    assert json.loads(lex.read_text())["entry_anchor"] == {
        "context_words": ["zorbly"], "non_market_words": ["quentin"]}
    assert len(V.backups(lex)) == 1
    meanings = {d["token"]: d["meaning"] for d in
                json.loads(V.decisions_path(lex).read_text())["applied"]}
    assert meanings["zorbly"] == "invented filler word"
    # Decided words are no longer proposed.
    assert "Pending proposals 0" in shown(page(monkeypatch, cfg))
    at = page(monkeypatch, cfg)
    at.button(key="vr_undo").click().run()
    assert "RESTORED" in shown(at)
    assert json.loads(lex.read_text()) == {"open_present": [], "entry_anchor": {}}


def test_a_refusal_is_shown_with_the_loaders_reason(monkeypatch, local, held):
    cfg, lex, _log = held
    at = page(monkeypatch, cfg)
    order = [k.key.rsplit("_", 1)[1] for k in at.radio if k.key.startswith("vr_verdict_")]
    labels = [e.label.split(":")[0] for e in at.expander]
    i = order[labels.index("bonds")]
    at.selectbox(key=f"vr_group_{i}").set_value(V.NON_MARKET).run()
    at.radio(key=f"vr_verdict_{i}").set_value(V.ACCEPT).run()
    at.checkbox(key="vr_confirm").check().run()
    before = lex.read_bytes()
    at.button(key="vr_apply").click().run()
    text = shown(at)
    assert "Refused `bonds`" in text and "market or instrument noun" in text
    assert lex.read_bytes() == before and V.backups(lex) == []


def test_a_candidate_is_written_beside_and_production_is_untouched(monkeypatch, local, held):
    cfg, lex, _log = held
    before = lex.read_bytes()
    at = page(monkeypatch, cfg)
    order = [k.key.rsplit("_", 1)[1] for k in at.radio if k.key.startswith("vr_verdict_")]
    for i in order:
        at.radio(key=f"vr_verdict_{i}").set_value(V.ACCEPT).run()
    at.button(key="vr_candidate").click().run()
    text = shown(at)
    assert "production lexicon is unchanged" in text and "--allow-candidate-lexicon" in text
    assert lex.read_bytes() == before
    assert V.CANDIDATE_KEY in json.loads(V.default_candidate_path(lex).read_text())


def test_a_proposals_file_is_used_with_its_measured_change(monkeypatch, local, held):
    cfg, _lex, log = held
    V.default_proposals_path(log).write_text(json.dumps({
        "schema": V.PROPOSAL_SCHEMA, "token": "zorbly", "group": V.INSTRUCTION_PHRASE,
        "meaning": "invented filler", "evidence": {"posts": 9, "holds": 11},
        "categories": {"families": ["TLT"], "reasons": ["ENTRY_ANCHOR_CONTEXT_UNCLEAR"]},
        "measured": {"holds_resolved": 9, "false_executable_introduced": 0},
        "examples": [{"source_id": "1"}]}) + "\n")
    text = shown(page(monkeypatch, cfg))
    assert "Proposals from review.proposals.jsonl" in text
    assert "holds resolved: 9" in text and "false executable introduced: 0" in text
    assert "Pending proposals 1" in text and "We are now long $TLT zorbly" in text


def test_the_page_refuses_to_show_private_text_off_localhost(monkeypatch, held):
    cfg, _lex, _log = held
    before = st_config.get_option("server.address")
    st_config.set_option("server.address", "0.0.0.0")
    try:
        text = shown(page(monkeypatch, cfg))
    finally:
        st_config.set_option("server.address", before)
    assert "not bound to localhost" in text
    assert "zorbly" not in text and "Quentin" not in text


def test_without_the_private_paths_it_shows_how_to_set_them(monkeypatch, local, tmp_path):
    (tmp_path / "run").mkdir()
    rt = Runtime(tmp_path / "run", [], mapping_file=MAPPING)
    rt.service.close()
    text = shown(page(monkeypatch, write_config(rt, tmp_path / "r.toml", interpreter=None)))
    assert "review_log_path" in text and "lexicon_path" in text
    assert "No usable runtime config" in shown(page(monkeypatch, tmp_path / "absent.toml"))


def test_the_page_never_logs_caches_or_puts_text_in_the_url():
    source = PAGE.read_text()
    for forbidden in ("query_params", "experimental_set_query_params", "cache_data",
                      "cache_resource", "logging", "print(", "st.exception", "st.write_stream",
                      ".submit(", "SimBroker", "sqlite3"):
        assert forbidden not in source, forbidden
    tree = ast.parse(source)
    imported = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    assert not any("execution" in m or "service" in m or "ibkr" in m for m in imported)


def test_no_other_page_reads_the_private_files():
    for page_file in (REPO / "dashboard").rglob("*.py"):
        if page_file == PAGE:
            continue
        text = page_file.read_text()
        assert "review_log" not in text and "vocab_review" not in text, page_file.name
