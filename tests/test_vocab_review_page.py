"""The owner's Vocabulary Review page (``dashboard/pages/10_Vocabulary_Review.py``), driven
headlessly through ``streamlit.testing.v1.AppTest`` on a review log the runtime service wrote for
invented posts. It shows the batch in the four groups, applies it in one action (one backup, one
undo), writes a candidate without touching production, refuses to run off localhost or with
usage statistics on, and keeps private text out of the URL, the caches and every log.

Review fixes: widgets are keyed by a stable proposal id (never by position) and cleared after every
write; Apply is bound to the digest of the final table shown and refused when the files changed;
every private or file-supplied string is rendered as plain text, never Markdown or HTML; a
measured change from the proposals file is labelled as claimed, not verified."""

from __future__ import annotations

import ast
import json
import tomllib
from pathlib import Path

import pytest
from runtime_harness import MAPPING, Runtime, post, required_toml
from streamlit import config as st_config
from streamlit.testing.v1 import AppTest

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
def local():
    saved = {k: st_config.get_option(k) for k in ("server.address", "browser.gatherUsageStats")}
    st_config.set_option("server.address", "127.0.0.1")
    st_config.set_option("browser.gatherUsageStats", False)
    yield
    for k, v in saved.items():
        st_config.set_option(k, v)


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


MARKDOWN_KINDS = ("title", "subheader", "header", "error", "warning", "info", "success",
                  "markdown", "caption")


def rendered(at: AppTest) -> str:
    """Everything Streamlit renders as Markdown: element bodies, expander and widget labels."""
    parts = [str(e.value) for kind in MARKDOWN_KINDS for e in at.get(kind)]
    parts += [e.label for e in at.expander]
    parts += [w.label for kind in ("radio", "selectbox", "checkbox", "text_input", "button")
              for w in at.get(kind)]
    parts += [f"{m.label} {m.value}" for m in at.metric]
    return "\n".join(parts)


def plain(at: AppTest) -> str:
    """Everything rendered as plain text: st.text, st.code and the final table."""
    parts = [str(e.value) for e in at.text] + [str(e.value) for e in at.code]
    parts += [d.value.to_csv() for d in at.dataframe]
    return "\n".join(parts)


def shown(at: AppTest) -> str:
    return rendered(at) + "\n" + plain(at)


def page(monkeypatch, cfg: Path) -> AppTest:
    monkeypatch.setenv("TRADINGBOT_RUNTIME_CONFIG", str(cfg))
    monkeypatch.delenv("TRADINGBOT_VOCAB_PROPOSALS", raising=False)
    at = AppTest.from_file(str(PAGE), default_timeout=60)
    at.run()
    assert not at.exception
    return at


def ids(log: Path, lex: Path) -> dict[str, str]:
    props, *_ = V.load_batch(log, lex, V.default_proposals_path(log))
    return {p.token: V.proposal_id(p) for p in props}


def confirm(at: AppTest):
    [box] = [c for c in at.checkbox if c.key.startswith("vr_confirm_batch_")]
    return box


def proposals(log: Path, rows: list[dict]) -> Path:
    path = V.default_proposals_path(log)
    path.write_text("".join(json.dumps({"schema": V.PROPOSAL_SCHEMA,
                                        "evidence": {"posts": 1, "holds": 1}, **r}) + "\n"
                            for r in rows))
    return path


def words(*tokens, group=V.NON_MARKET) -> list[dict]:
    return [{"token": t, "group": group} for t in tokens]


def lexicon_words(lex: Path) -> dict:
    return json.loads(lex.read_text())["entry_anchor"]


# ------------------------------------------------------------------ the batch


def test_the_page_shows_the_batch_in_four_groups_with_its_evidence(monkeypatch, local, held):
    cfg, _lex, _log = held
    at = page(monkeypatch, cfg)
    text = shown(at)
    for needle in ("Vocabulary review (OWNER ONLY)", "privately, here on your machine only",
                   "restarted", *V.GROUPS.values(), "plain batch built from the review log",
                   "not measured", "zorbly", "quentin", "Thanks to Quentin",
                   "Vocabulary holds logged 3", "Pending proposals 3"):
        assert needle in text, needle
    # Private words are never in a Markdown-rendered label.
    assert "zorbly" not in rendered(at) and "quentin" not in rendered(at).lower()


def test_the_final_table_names_each_list_family_and_effect(monkeypatch, local, held):
    cfg, lex, log = held
    pid = ids(log, lex)
    at = page(monkeypatch, cfg)
    at.radio(key=f"vr_verdict_{pid['zorbly']}").set_value(V.ACCEPT).run()
    at.radio(key=f"vr_verdict_{pid['bonds']}").set_value(V.ACCEPT).run()
    at.radio(key=f"vr_verdict_{pid['quentin']}").set_value(V.KEEP_HOLDING).run()
    [table] = at.dataframe
    rows = {r["word"]: r for r in table.value.to_dict("records")}
    assert rows["zorbly"]["list"] == "entry_anchor.context_words"
    assert rows["bonds"]["list"] == "entry_anchor.family_aliases.TLT"
    assert rows["bonds"]["family"] == "TLT"
    assert rows["zorbly"]["effect"].startswith("removes a hold: posts containing this word may "
                                               "become executable")
    assert rows["quentin"]["effect"] == "keeps holding"
    assert "digest" in confirm(at).label


def test_the_batch_is_applied_in_one_action_and_undone(monkeypatch, local, held):
    cfg, lex, log = held
    pid = ids(log, lex)
    at = page(monkeypatch, cfg)
    at.radio(key=f"vr_verdict_{pid['zorbly']}").set_value(V.ACCEPT).run()
    at.text_input(key=f"vr_meaning_{pid['zorbly']}").input("invented filler word").run()
    at.radio(key=f"vr_verdict_{pid['quentin']}").set_value(V.ACCEPT).run()
    at.radio(key=f"vr_verdict_{pid['bonds']}").set_value(V.KEEP_HOLDING).run()
    at.button(key="vr_apply").click().run()
    assert "Not applied" in shown(at)  # the confirmation is required
    assert lexicon_words(lex) == {}
    confirm(at).check().run()
    at.button(key="vr_apply").click().run()
    text = shown(at)
    assert "WRITTEN" in text and "2 word(s) added, 1 kept holding, 0 refused" in text
    assert lexicon_words(lex) == {"context_words": ["zorbly"], "non_market_words": ["quentin"]}
    assert len(V.backups(lex)) == 1
    meanings = {d["token"]: d["meaning"] for d in
                json.loads(V.decisions_path(lex).read_text())["applied"]}
    assert meanings["zorbly"] == "invented filler word"
    assert "Pending proposals 0" in shown(page(monkeypatch, cfg))
    at = page(monkeypatch, cfg)
    at.button(key="vr_undo").click().run()
    assert "Not undone" in shown(at)  # undo needs its own confirmation
    at.checkbox(key="vr_confirm_undo").check().run()
    at.button(key="vr_undo").click().run()
    assert "RESTORED" in shown(at)
    assert lexicon_words(lex) == {}


def test_after_a_write_the_next_proposal_inherits_nothing(monkeypatch, local, held):
    """The reviewer's scenario 1: the list shifts after an apply; a second Apply must not write
    a word the owner never accepted."""
    cfg, lex, log = held
    proposals(log, words("alphaword", "zorbleflux"))
    pid = ids(log, lex)
    at = page(monkeypatch, cfg)
    at.radio(key=f"vr_verdict_{pid['alphaword']}").set_value(V.ACCEPT).run()
    confirm(at).check().run()
    at.button(key="vr_apply").click().run()
    assert lexicon_words(lex) == {"non_market_words": ["alphaword"]}
    assert at.radio(key=f"vr_verdict_{pid['zorbleflux']}").value == V.PENDING
    assert not any(c.value for c in at.checkbox)  # every confirmation cleared
    at.button(key="vr_apply").click().run()
    assert lexicon_words(lex) == {"non_market_words": ["alphaword"]}


def test_a_new_proposal_appearing_before_the_click_never_gets_the_owners_choice(
        monkeypatch, local, held):
    """The reviewer's scenario 2."""
    cfg, lex, log = held
    proposals(log, words("betaword", "gammaword"))
    pid = ids(log, lex)
    at = page(monkeypatch, cfg)
    at.radio(key=f"vr_verdict_{pid['betaword']}").set_value(V.ACCEPT).run()
    proposals(log, words("quibblewump", "betaword", "gammaword"))
    confirm(at).check().run()
    at.button(key="vr_apply").click().run()
    assert lexicon_words(lex) == {"non_market_words": ["betaword"]}


def test_a_proposals_file_rewritten_between_render_and_click_is_refused(monkeypatch, local, held):
    cfg, lex, log = held
    proposals(log, words("betaword", "gammaword"))
    pid = ids(log, lex)
    at = page(monkeypatch, cfg)
    at.radio(key=f"vr_verdict_{pid['betaword']}").set_value(V.ACCEPT).run()
    confirm(at).check().run()
    proposals(log, words("betaword", group=V.INSTRUCTION_PHRASE) + words("gammaword"))
    before = lex.read_bytes()
    at.button(key="vr_apply").click().run()
    assert "changed since you reviewed it" in shown(at)
    assert lex.read_bytes() == before and V.backups(lex) == []


def test_a_refusal_is_shown_with_the_loaders_reason(monkeypatch, local, held):
    cfg, lex, log = held
    pid = ids(log, lex)
    at = page(monkeypatch, cfg)
    i = pid["bonds"]
    at.selectbox(key=f"vr_group_{i}").set_value(V.NON_MARKET).run()
    at.radio(key=f"vr_verdict_{i}").set_value(V.ACCEPT).run()
    confirm(at).check().run()
    before = lex.read_bytes()
    at.button(key="vr_apply").click().run()
    assert "bonds" in plain(at) and "market or instrument noun" in plain(at)
    assert "bonds" not in rendered(at)
    assert lex.read_bytes() == before and V.backups(lex) == []


def test_a_candidate_needs_its_confirmation_and_leaves_production_untouched(
        monkeypatch, local, held):
    cfg, lex, log = held
    before = lex.read_bytes()
    at = page(monkeypatch, cfg)
    for i in ids(log, lex).values():
        at.radio(key=f"vr_verdict_{i}").set_value(V.ACCEPT).run()
    at.button(key="vr_candidate").click().run()
    assert not V.default_candidate_path(lex).exists() and "Not written" in shown(at)
    at.checkbox(key="vr_confirm_candidate").check().run()
    at.button(key="vr_candidate").click().run()
    text = shown(at)
    assert "production lexicon is unchanged" in text and "--allow-candidate-lexicon" in text
    assert lex.read_bytes() == before
    assert V.CANDIDATE_KEY in json.loads(V.default_candidate_path(lex).read_text())
    assert all(r.value == V.PENDING for r in at.radio if r.key.startswith("vr_verdict_"))


def test_a_proposals_file_is_used_and_its_measured_change_is_labelled_claimed(
        monkeypatch, local, held):
    cfg, _lex, log = held
    proposals(log, [{"token": "zorbly", "group": V.INSTRUCTION_PHRASE,
                     "meaning": "invented filler", "evidence": {"posts": 9, "holds": 11},
                     "categories": {"families": ["TLT"],
                                    "reasons": ["ENTRY_ANCHOR_CONTEXT_UNCLEAR"]},
                     "measured": {"holds_resolved": 9, "false_executable_introduced": 0},
                     "examples": [{"source_id": "1"}]}])
    at = page(monkeypatch, cfg)
    text = shown(at)
    assert "Proposals from review.proposals.jsonl" in text
    assert "claimed by the proposals file; not verified by this page" in text
    assert "holds_resolved: 9" in plain(at) and "false_executable_introduced: 0" in plain(at)
    assert "Pending proposals 1" in text and "We are now long $TLT zorbly" in plain(at)


def test_a_family_outside_the_mapping_is_shown_as_the_file_states_it(monkeypatch, local, held):
    cfg, lex, log = held
    proposals(log, [{"token": "zetaword", "group": V.MARKET_NAME, "family": "QQQZ"}])
    at = page(monkeypatch, cfg)
    assert at.selectbox(key=f"vr_family_{ids(log, lex)['zetaword']}").value == "QQQZ"
    assert "not in the mapping file" in shown(at)


PAYLOADS = ("![img](https://example.invalid/p.png?q=zorbleflux)",
            "[click](https://example.invalid/link)", "<b>bold-html</b>",
            "<script>alert('x')</script>", "<img src=https://example.invalid/i.png>")


def test_file_supplied_text_is_never_rendered_as_markdown_or_html(monkeypatch, local, held):
    cfg, _lex, log = held
    rows = []
    for n, payload in enumerate(PAYLOADS):
        rows.append({"token": f"tok{n} {payload}", "group": V.NON_MARKET, "meaning": payload,
                     "kind": payload,
                     "categories": {"families": [payload], "reasons": [payload]},
                     "measured": {payload: 1},
                     "examples": [{"role": payload, "text": f"sentence {payload}"}]})
    proposals(log, rows)
    at = page(monkeypatch, cfg)
    md = rendered(at)
    for needle in ("example.invalid", "<b>", "<script>", "<img", "bold-html"):
        assert needle not in md, needle
    text = plain(at)
    for payload in PAYLOADS:
        assert payload in text  # shown literally
    source = PAGE.read_text()
    assert "unsafe_allow_html" not in source and "st.html" not in source


# ------------------------------------------------------------------ refusals and privacy


def test_the_page_refuses_to_show_private_text_off_localhost(monkeypatch, local, held):
    cfg, _lex, _log = held
    st_config.set_option("server.address", "0.0.0.0")
    text = shown(page(monkeypatch, cfg))
    assert "not bound to localhost" in text
    assert "zorbly" not in text.lower() and "quentin" not in text.lower()


def test_the_page_refuses_while_usage_statistics_are_on(monkeypatch, local, held):
    cfg, _lex, _log = held
    st_config.set_option("browser.gatherUsageStats", True)
    text = shown(page(monkeypatch, cfg))
    assert "usage statistics" in text and "--browser.gatherUsageStats false" in text
    assert "zorbly" not in text.lower() and "quentin" not in text.lower()


def test_a_synced_private_path_is_named_by_class_in_a_banner(monkeypatch, local, held):
    cfg, _lex, _log = held
    monkeypatch.setattr(V, "synced_folder_class", lambda p: "CLOUD_STORAGE")
    text = shown(page(monkeypatch, cfg))
    assert "PRIVATE_PATH_IN_SYNCED_FOLDER" in text and "CLOUD_STORAGE" in text
    assert "non-synced" in text


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
                      "unsafe_allow_html", ".submit(", "SimBroker", "sqlite3"):
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
