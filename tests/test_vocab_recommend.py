"""The outside reviewer's sanitized export, the stand-alone reviewer page
(``dashboard/reviewer/reviewer_app.py``), its hash-chained recommendations log, and what the owner
page shows of it and records on apply. Invented words and sentences only."""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

import _review_modes as modes
import pytest
import test_vocab_review_page as owner
from _review_modes import published, with_headers
from streamlit.runtime.context import StreamlitHeaders
from streamlit.testing.v1 import AppTest
from test_vocab_review_page import confirm, ids, page, plain, proposals, shown, words

from tradingbot.runtime import vocab_recommend as R
from tradingbot.runtime import vocab_review as V

REPO = Path(__file__).resolve().parents[1]
APP = REPO / "dashboard" / "reviewer" / "reviewer_app.py"
RIA = "reviewer@example.invalid"
held, local = owner.held, modes.local  # fixtures
SECRET_SENTENCE = "The zubrowka vexels glint near ninety-two"


def invented() -> list[V.Proposal]:
    return [V.Proposal(token="vexels", group=V.MARKET_NAME, family="TLT", kind="MARKET_NOUN",
                       meaning="glimmerquark sense", posts=3, holds=4, families=["TLT"],
                       reasons=["ENTRY_ANCHOR_POST_UNRESOLVED_MARKET"],
                       measured={"holds_resolved": 3},
                       examples=[("MENTION", SECRET_SENTENCE)],
                       example_refs=[{"source_id": "77123", "action_id": "act-9"}]),
            V.Proposal(token="plinkish", group=V.NON_MARKET, kind="PROPER_NOUN", posts=1,
                       holds=1, reasons=["ENTRY_ANCHOR_CONTEXT_UNCLEAR"]),
            V.Proposal(token=SECRET_SENTENCE.lower() + " and more", group=V.AMBIGUOUS)]


@pytest.fixture
def export(tmp_path) -> Path:
    path = tmp_path / "reviewer-private" / "export.json"
    assert R.write_export(path, invented()) == (2, 1)  # the sentence-like "token" is withheld
    return path


def reviewer(monkeypatch, export: Path, **headers) -> AppTest:
    """The page; with a proxy identity header, in PUBLISHED mode (LOCAL mode refuses one)."""
    monkeypatch.setenv(R.EXPORT_ENV, str(export))
    for k in (R.ALLOWED_ENV, R.IDENTITY_ENV, R.LOGIN_HEADER_ENV, R.NAME_HEADER_ENV):
        monkeypatch.delenv(k, raising=False)
    if headers:
        published(monkeypatch)
    with_headers(monkeypatch, **headers)
    at = AppTest.from_file(str(APP), default_timeout=60)
    at.run()
    assert not at.exception
    return at


def tree(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*"))
            if p.is_file()}


def repo_files() -> set[str]:
    return {str(p) for p in REPO.rglob("*") if p.is_file() and "__pycache__" not in p.parts
            and ".pytest_cache" not in p.parts}


# ------------------------------------------------------------------ the export


def test_the_export_holds_only_the_allow_list_and_none_of_the_example_words(export):
    text = export.read_text()
    raw = json.loads(text)
    assert set(raw) == {"schema", "proposals"} and raw["schema"] == R.EXPORT_SCHEMA
    assert all(tuple(r) == R.EXPORT_FIELDS for r in raw["proposals"])
    assert [r["token"] for r in raw["proposals"]] == ["vexels", "plinkish"]
    for secret in ("zubrowka", "glint", "ninety", "glimmerquark", "77123", "act-9",
                   "holds_resolved", "MENTION", "meaning", "examples", "measured"):
        assert secret not in text, secret
    assert export.stat().st_mode & 0o777 == 0o600


def test_an_unknown_field_is_dropped_and_markup_is_withheld():
    row = {"id": "0123456789abcdef", "token": "plinkish", "group": V.NON_MARKET,
           "meaning": "x", "post_id": "9", "at": "2026-01-01", "path": "/tmp/x",
           "reasons": ["ENTRY_ANCHOR_CONTEXT_UNCLEAR", "<b>no</b>"]}
    assert tuple(R.sanitize_row(row)) == R.EXPORT_FIELDS
    assert R.sanitize_row(row)["reasons"] == ["ENTRY_ANCHOR_CONTEXT_UNCLEAR"]
    assert R.sanitize_row({**row, "token": "[x](https://example.invalid)"}) is None


def test_the_export_is_refused_inside_the_repository():
    with pytest.raises(V.PrivatePathError):
        R.write_export(REPO / "reviewer-export.json", invented())
    assert not (REPO / "reviewer-export.json").exists()


# ------------------------------------------------------------------ isolation


FORBIDDEN = ("tradingbot.runtime.service", "tradingbot.runtime.mapping", "tradingbot.execution",
             "tradingbot.ibkr", "tradingbot.ibkr_orders", "tradingbot.storage",
             "tradingbot.validator", "ibapi")


def test_the_reviewer_page_imports_no_service_mapping_execution_broker_or_storage(export):
    script = f"""
import sys
from streamlit import config as c
from streamlit.testing.v1 import AppTest
from streamlit.runtime.context import ContextProxy, StreamlitHeaders
c.set_option("server.address", "127.0.0.1"); c.set_option("browser.gatherUsageStats", False)
c.set_option("client.showErrorDetails", "none")
port = c.get_option("server.port")
ContextProxy.headers = property(lambda s: StreamlitHeaders([("Host", f"127.0.0.1:{{port}}")]))
at = AppTest.from_file({str(APP)!r}, default_timeout=60); at.run()
assert not at.exception and "plinkish" in "".join(str(t.value) for t in at.text)
print(sorted(m for m in sys.modules if m.startswith({FORBIDDEN!r})))
"""
    env = {**os.environ, R.EXPORT_ENV: str(export), "PYTHONPATH": str(REPO / "src")}
    out = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True,
                         text=True, timeout=120, check=False)
    assert out.returncode == 0, out.stderr[-2000:]
    assert out.stdout.strip().splitlines()[-1] == "[]"
    imported = {n.module for n in ast.walk(ast.parse(APP.read_text()))
                if isinstance(n, ast.ImportFrom)} | {
        a.name for n in ast.walk(ast.parse(APP.read_text())) if isinstance(n, ast.Import)
        for a in n.names}
    assert imported == {"__future__", "os", "pathlib", "streamlit", "tradingbot.runtime"}
    source = APP.read_text()
    for forbidden in ("file_uploader", "download_button", "unsafe_allow_html", "st.markdown",
                      "st.write", "query_params", "cache_data", "cache_resource", "logging",
                      "print("):
        assert forbidden not in source, forbidden
    assert not (APP.parent / "pages").exists()


# ------------------------------------------------------------------ identity


def test_without_an_identity_header_it_is_a_read_only_preview(monkeypatch, local, export):
    at = reviewer(monkeypatch, export)
    assert "Preview only" in shown(at) and "submissions are disabled" in shown(at)
    assert "vexels" in plain(at) and "vexels" not in "".join(str(e.value) for e in at.markdown)
    assert all(b.disabled for b in at.button)
    assert not R.recommendations_path(export).exists()


def test_a_login_outside_the_allow_list_sees_only_not_authorized(monkeypatch, local, export):
    monkeypatch.setenv(R.ALLOWED_ENV, RIA)
    published(monkeypatch)
    for headers in ({"Tailscale-User-Login": "mallory@example.invalid"}, {}):
        with_headers(monkeypatch, **headers)
        at = AppTest.from_file(str(APP), default_timeout=60)
        at.run()
        assert [e.value for e in at.error] == ["Not authorized."]
        assert "vexels" not in shown(at) and not at.title and not at.button


def test_identity_refusals():
    hdr = StreamlitHeaders([("Tailscale-User-Login", RIA), ("tailscale-user-login", "x@y")])
    assert R.identity_from_headers(hdr, {}).refused == "DUPLICATE_IDENTITY_HEADER"
    assert R.identity_from_headers(StreamlitHeaders([]), {R.IDENTITY_ENV: "cloudflare"}
                                   ).refused == "IDENTITY_MODE_NOT_IMPLEMENTED"
    custom = StreamlitHeaders([("X-Proxy-User", RIA)])
    assert R.identity_from_headers(custom, {R.LOGIN_HEADER_ENV: "X-Proxy-User"}).login == RIA


def test_the_reviewer_page_refuses_off_loopback(monkeypatch, local, export):
    from streamlit import config as st_config

    st_config.set_option("server.address", "0.0.0.0")
    text = shown(reviewer(monkeypatch, export))
    assert "not bound to 127.0.0.1" in text and "vexels" not in text


# ------------------------------------------------------------------ the log


def submit(at: AppTest, pid: str, verdict: str, *, group=None, family=None, note="") -> AppTest:
    at.radio(key=f"rv_verdict_{pid}").set_value(verdict).run()
    if group:
        at.selectbox(key=f"rv_group_{pid}").set_value(group).run()
    if family:
        at.selectbox(key=f"rv_family_{pid}").set_value(family).run()
    at.text_area(key=f"rv_note_{pid}").input(note).run()
    return at.button(key=f"rv_submit_{pid}").click().run()


def test_a_submit_writes_exactly_one_chained_line_and_a_later_one_supersedes(
        monkeypatch, local, export):
    pid = R.read_export(export)[0]["id"]
    before = tree(export.parent)
    at = reviewer(monkeypatch, export, **{"Tailscale-User-Login": RIA,
                                          "Tailscale-User-Name": "Reviewer Example"})
    at = submit(at, pid, "ACCEPT", group=V.MARKET_NAME, family="TLT", note="looks like a nick")
    assert "Recorded as line 1" in shown(at)
    log = R.recommendations_path(export)
    assert set(tree(export.parent)) == set(before) | {log.name, R.head_path(log).name}
    assert log.stat().st_mode & 0o777 == 0o600
    [line] = log.read_text().splitlines()
    rec = json.loads(line)
    assert rec["seq"] == 1 and rec["prev_sha256"] == R.GENESIS and rec["proposal_id"] == pid
    assert rec["reviewer"] == {"login": RIA, "name": "Reviewer Example"} and rec["token"] == "vexels"
    assert rec["recommendation"] == {"verdict": "ACCEPT", "group": V.MARKET_NAME,
                                     "family": "TLT"} and rec["note"] == "looks like a nick"
    at = submit(at, pid, R.NEEDS_CONTEXT)
    checked = R.verify_log(log)
    assert checked.ok and checked.count == 2
    assert R.latest(checked.records)[pid][RIA]["recommendation"]["verdict"] == R.NEEDS_CONTEXT
    assert "Your latest recommendation" in plain(at.run())


def test_verify_detects_an_edited_or_removed_line(export):
    log, rows = R.recommendations_path(export), R.read_export(export)
    for v in ("REJECT", "KEEP_HOLDING", "NEEDS_CONTEXT"):
        R.append_recommendation(log, rows, login=RIA, name="", proposal=rows[1]["id"], verdict=v)
    assert R.verify_log(log).ok
    lines = log.read_bytes().splitlines(keepends=True)
    log.write_bytes(lines[0].replace(b"REJECT", b"ACCEPT") + b"".join(lines[1:]))
    assert R.verify_log(log).problem == "CHAIN_BROKEN at line 2"
    log.write_bytes(lines[0] + lines[2])
    assert not R.verify_log(log).ok
    with pytest.raises(R.RecommendationError, match="LOG_FAILS_VERIFICATION"):
        R.append_recommendation(log, rows, login=RIA, name="", proposal=rows[1]["id"],
                                verdict="REJECT")


def test_invalid_recommendations_are_refused(export):
    log, rows = R.recommendations_path(export), R.read_export(export)
    pid = rows[0]["id"]
    for kw, code in (({"proposal": "f" * 16, "verdict": "REJECT"}, "UNKNOWN_PROPOSAL"),
                     ({"proposal": pid, "verdict": "APPLY"}, "UNKNOWN_VERDICT"),
                     ({"proposal": pid, "verdict": "ACCEPT", "group": V.AMBIGUOUS},
                      "ACCEPT_NEEDS_A_GROUP"),
                     ({"proposal": pid, "verdict": "ACCEPT", "group": V.MARKET_NAME,
                       "family": "QQQ"}, "ACCEPT_NEEDS_A_FAMILY"),
                     ({"proposal": pid, "verdict": "REJECT", "note": "x" * 501},
                      "NOTE_TOO_LONG_OR_UNPRINTABLE")):
        with pytest.raises(R.RecommendationError, match=code):
            R.append_recommendation(log, rows, login=RIA, name="", **kw)
    assert not log.exists()


def test_an_unwritable_log_fails_closed_and_nothing_else_is_written(
        monkeypatch, local, export, tmp_path):
    pid = R.read_export(export)[0]["id"]
    at = reviewer(monkeypatch, export, **{"Tailscale-User-Login": RIA})
    repo_before = repo_files()
    before = tree(tmp_path)
    export.parent.chmod(0o500)  # the log cannot be created
    try:
        at = submit(at, pid, "REJECT")
        assert "Not recorded" in shown(at) and "Recorded as" not in shown(at)
        assert tree(tmp_path) == before
    finally:
        export.parent.chmod(0o700)
    log = R.recommendations_path(export)
    R.append_recommendation(log, R.read_export(export), login=RIA, name="", proposal=pid,
                            verdict="REJECT")
    log.chmod(0o400)  # the log exists but is read-only
    before = tree(tmp_path)
    at = submit(at, pid, "KEEP_HOLDING")
    assert "Not recorded" in shown(at) and tree(tmp_path) == before
    assert repo_files() == repo_before  # nothing written into the code repository either


# ------------------------------------------------------------------ the owner page


def owner_export(log: Path, lex: Path, tmp_path: Path) -> Path:
    props, *_ = V.load_batch(log, lex, V.default_proposals_path(log))
    path = tmp_path / "reviewer-private" / "export.json"
    R.write_export(path, props)
    return path


def test_without_a_recommendations_log_the_owner_page_is_unchanged(
        monkeypatch, local, held, tmp_path):
    cfg, lex, log = held
    proposals(log, words("alphaword", "zorbleflux"))
    monkeypatch.delenv(R.EXPORT_ENV, raising=False)
    baseline = page(monkeypatch, cfg)
    monkeypatch.setenv(R.EXPORT_ENV, str(owner_export(log, lex, tmp_path)))  # no log yet
    at = page(monkeypatch, cfg)
    assert shown(at) == shown(baseline) and "vr_recs_shown" not in at.session_state
    pid = ids(log, lex)["alphaword"]
    at.radio(key=f"vr_verdict_{pid}").set_value(V.ACCEPT).run()
    confirm(at).check().run()
    at.button(key="vr_apply").click().run()
    assert V.backups(lex) and not R.audit_path(lex).exists()


def test_the_owner_page_shows_the_latest_recommendation_and_records_it_on_apply(
        monkeypatch, local, held, tmp_path):
    cfg, lex, log = held
    proposals(log, words("alphaword", "zorbleflux"))
    export = owner_export(log, lex, tmp_path)
    pid = ids(log, lex)
    rows = R.read_export(export)
    rlog = R.recommendations_path(export)
    R.append_recommendation(rlog, rows, login=RIA, name="Reviewer Example", proposal=pid["alphaword"],
                            verdict="REJECT", note="first thought")
    R.append_recommendation(rlog, rows, login=RIA, name="Reviewer Example", proposal=pid["alphaword"],
                            verdict="ACCEPT", group=V.NON_MARKET, note="a filler word")
    monkeypatch.setenv(R.EXPORT_ENV, str(export))
    at = page(monkeypatch, cfg)
    text = plain(at)
    assert f"Reviewer {RIA} (Reviewer Example) recommends ACCEPT into" in text and "a filler word" in text
    assert "first thought" not in text and "a filler word" not in "".join(
        str(e.value) for e in at.markdown)
    assert all(r.value == V.PENDING for r in at.radio)  # never pre-selected
    at.radio(key=f"vr_verdict_{pid['alphaword']}").set_value(V.ACCEPT).run()
    at.radio(key=f"vr_verdict_{pid['zorbleflux']}").set_value(V.REJECT).run()
    confirm(at).check().run()
    at.button(key="vr_apply").click().run()
    [line] = R.audit_path(lex).read_text().splitlines()
    audit = json.loads(line)
    assert audit["result"] == "WRITTEN" and audit["backup"] == V.backups(lex)[0].name
    assert audit["recommendations_log_head"]["count"] == 2
    by = {d["token"]: d for d in audit["decisions"]}
    assert by["alphaword"]["decision"] == V.ACCEPT and by["zorbleflux"]["decision"] == V.REJECT
    assert by["alphaword"]["shown_recommendations"] == [
        {"login": RIA, "verdict": "ACCEPT", "group": V.NON_MARKET, "family": None, "seq": 2}]
    assert by["zorbleflux"]["shown_recommendations"] == [] and audit["at"]
    assert R.audit_path(lex).stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("option,unsafe", [("server.enableCORS", False),
                                           ("server.enableXsrfProtection", False),
                                           ("server.enableAgentApi", True),
                                           ("server.enableStaticServing", True)])
def test_the_reviewer_page_refuses_unsafe_server_options(monkeypatch, local, export, option,
                                                         unsafe):
    from streamlit import config as st_config

    saved = st_config.get_option(option)
    st_config.set_option(option, unsafe)
    try:
        text = shown(reviewer(monkeypatch, export, **{"Tailscale-User-Login": RIA}))
    finally:
        st_config.set_option(option, saved)
    assert f"Refusing to run: {option}" in text and "vexels" not in text


def test_the_export_command_writes_the_owner_pages_proposals_and_refuses_the_repo(
        monkeypatch, held, tmp_path, capsys):
    import importlib.util

    cfg, lex, log = held
    proposals(log, words("alphaword", "zorbleflux"))
    spec = importlib.util.spec_from_file_location(
        "vocab_reviewer_export", REPO / "scripts" / "vocab_reviewer_export.py")
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    monkeypatch.delenv("TRADINGBOT_VOCAB_PROPOSALS", raising=False)
    out = tmp_path / "reviewer-private" / "export.json"
    assert cli.main(["--out", str(out), "--config", str(cfg)]) == 0
    assert {r["id"] for r in R.read_export(out)} == set(ids(log, lex).values())
    assert "alphaword" not in capsys.readouterr().out
    assert cli.main(["--out", str(REPO / "x-export.json"), "--config", str(cfg)]) == 2
    assert not (REPO / "x-export.json").exists()


def test_the_vocabulary_page_follows_the_modes(monkeypatch, local, export):
    monkeypatch.setenv(R.EXPORT_ENV, str(export))
    with_headers(monkeypatch, **{"Tailscale-User-Login": RIA})  # LOCAL mode: refused outright
    at = AppTest.from_file(str(APP), default_timeout=60)
    at.run()
    assert [e.value for e in at.error] == ["Not authorized."]
    monkeypatch.setenv(R.LOCAL_IDENTITY_ENV, RIA)  # LOCAL mode: the owner's local identity
    with_headers(monkeypatch)
    at = AppTest.from_file(str(APP), default_timeout=60)
    at.run()
    assert f"Reviewer (local): {RIA}" in "".join(str(t.value) for t in at.text)
    pid = R.read_export(export)[0]["id"]
    at = submit(at, pid, "REJECT", note="first\nsecond \u202ethird")
    [line] = R.recommendations_path(export).read_text().splitlines()
    assert json.loads(line)["note"] == "first second third"
    published(monkeypatch)  # PUBLISHED mode with a local identity: refuses to start
    monkeypatch.setenv(R.LOCAL_IDENTITY_ENV, RIA)
    at = AppTest.from_file(str(APP), default_timeout=60)
    at.run()
    assert R.LOCAL_IDENTITY_ENV in "".join(str(e.value) for e in at.error)


def test_an_edit_of_the_last_recommendation_is_detected(export):
    log, rows = R.recommendations_path(export), R.read_export(export)
    for v in ("REJECT", "KEEP_HOLDING"):
        R.append_recommendation(log, rows, login=RIA, name="", proposal=rows[1]["id"], verdict=v)
    lines = log.read_bytes().splitlines(keepends=True)
    log.write_bytes(lines[0] + lines[1].replace(b"KEEP_HOLDING", b"REJECT"))
    assert R.verify_log(log).problem.startswith("HEAD_MISMATCH")
