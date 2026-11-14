"""The adjudication workspace (``runtime/adjudication.py``, ``dashboard/reviewer/adjudication_app.py``)
on the INVENTED demo bundle (``scripts/adjudication_demo_bundle.py``): validator refusals, the
restricted loader, proxy sessions, blind-first, single-field corrections, distinct approver, the
chained log, the freeze order, import isolation and the single write path."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from streamlit import config as st_config
from streamlit.runtime.context import ContextProxy, StreamlitHeaders
from streamlit.testing.v1 import AppTest

from tradingbot.runtime import adjudication as A

REPO = Path(__file__).resolve().parents[1]
APP = REPO / "dashboard" / "reviewer" / "adjudication_app.py"
_spec = importlib.util.spec_from_file_location(
    "adjudication_demo_bundle", REPO / "scripts" / "adjudication_demo_bundle.py")
DEMO = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(DEMO)
REVIEWER = A.Session("reviewer@example.invalid", "Ria", A.REVIEWER, A.FULL, False)
APPROVER = A.Session("owner@example.invalid", "Owner", A.APPROVER, A.FULL, False)
POST_WORDS = ("starter long", "Zed Metals", "bond crowd")


@pytest.fixture
def local():
    keys = ("server.address", "browser.gatherUsageStats")
    saved = {k: st_config.get_option(k) for k in keys}
    st_config.set_option("server.address", "127.0.0.1")
    st_config.set_option("browser.gatherUsageStats", False)
    yield
    for k, v in saved.items():
        st_config.set_option(k, v)


@pytest.fixture
def base(tmp_path) -> Path:
    return DEMO.write_demo(tmp_path / "adjudication")


def bundle(base, access=A.FULL) -> A.Bundle:
    return A.load_bundle(base / "bundle", access)


def rewrite(path: Path, edit) -> None:
    rows = [json.loads(x) for x in path.read_text().splitlines()]
    path.write_text("".join(json.dumps(r) + "\n" for r in edit(rows)))


def headers(monkeypatch, **h):
    monkeypatch.setattr(ContextProxy, "headers",
                        property(lambda self: StreamlitHeaders(list(h.items()))))


def app(monkeypatch, base, login=None, *, proxy=None) -> AppTest:
    monkeypatch.setenv(A.DIR_ENV, str(base))
    monkeypatch.delenv(A.LOCAL_IDENTITY_ENV, raising=False)
    if login:
        monkeypatch.setenv(A.LOCAL_IDENTITY_ENV, login)
    h = {"Host": f"127.0.0.1:{st_config.get_option('server.port')}"}
    if proxy:
        h["Tailscale-User-Login"] = proxy
    headers(monkeypatch, **h)
    at = AppTest.from_file(str(APP), default_timeout=60)
    at.run()
    assert not at.exception
    return at


def rendered(at) -> str:
    kinds = ("title", "subheader", "header", "error", "warning", "info", "success",
             "markdown", "caption")
    return "\n".join(str(e.value) for k in kinds for e in at.get(k))


def plain(at) -> str:
    return "\n".join([str(e.value) for e in at.text] + [str(e.value) for e in at.code])


def tree(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*"))
            if p.is_file()}


def records(base) -> list[dict]:
    v = A.read_annotations(base)
    assert v.ok
    return v.records


# ------------------------------------------------------------------ the bundle


@pytest.mark.parametrize("file,edit,code", [
    ("manifest.json", None, "BUNDLE_SCHEMA"),
    ("items.jsonl", lambda rs: [{**r, "ai": {**r["ai"], "action": "BUY"}} for r in rs],
     "UNKNOWN_VALUE:action"),
    ("items.jsonl", lambda rs: [{**r, "ai": {**r["ai"], "timing": "NOW"}} for r in rs],
     "UNKNOWN_VALUE:timing"),
    ("items.jsonl", lambda rs: [{**r, "ai": {**r["ai"], "family": "QQQ"}} for r in rs],
     "UNKNOWN_VALUE:family"),
    ("posts.jsonl", lambda rs: [{**r, "media": ["../secret.png"]} for r in rs],
     "BAD_MEDIA_NAME"),
    ("sequences.jsonl", lambda rs: [{**r, "review_order": 1} for r in rs], "BAD_REVIEW_ORDER"),
])
def test_the_validator_refuses_with_a_code_never_a_path_or_content(base, file, edit, code):
    path = base / "bundle" / file
    if edit is None:
        path.write_text(json.dumps({"schema": "tradingbot.adjudication.bundle/9"}))
    else:
        rewrite(path, edit)
    with pytest.raises(A.AdjudicationError) as exc:
        bundle(base)
    assert exc.value.code == code and str(base) not in str(exc.value)
    assert not any(w in str(exc.value) for w in POST_WORDS)


def test_restricted_never_opens_the_posts_or_the_media(base, monkeypatch):
    (base / "bundle" / "posts.jsonl").chmod(0)
    (base / "bundle" / "media").chmod(0)
    opened = []
    real = os.open
    monkeypatch.setattr(os, "open", lambda p, *a, **k: opened.append(str(p)) or real(p, *a, **k))
    try:
        b = bundle(base, A.RESTRICTED)
        assert b.posts is None and len(b.items) == 9
        assert not any("posts" in p or "media" in p or p.endswith(".png") for p in opened)
        with pytest.raises(A.AdjudicationError, match="RESTRICTED"):
            A.read_media(b, "p01", "chart-p01.png")
        with pytest.raises(PermissionError):
            bundle(base, A.FULL)
    finally:
        (base / "bundle" / "posts.jsonl").chmod(0o600)
        (base / "bundle" / "media").chmod(0o700)


def test_images_only_by_a_listed_name_within_the_cap(base, monkeypatch):
    b = bundle(base)
    assert A.read_media(b, "p01", "chart-p01.png").startswith(b"\x89PNG")
    for ref, name in (("p01", "other.png"), ("p02", "chart-p01.png")):
        with pytest.raises(A.AdjudicationError, match="MEDIA_NOT_LISTED"):
            A.read_media(b, ref, name)
    monkeypatch.setattr(A, "MAX_MEDIA_BYTES", 10)
    with pytest.raises(A.AdjudicationError, match="MEDIA_TOO_LARGE"):
        A.read_media(b, "p01", "chart-p01.png")


# ------------------------------------------------------------------ sessions


def test_a_proxy_session_is_restricted_while_the_switch_is_false(base):
    reviewers = A.load_reviewers(base / "reviewers.toml")
    h = StreamlitHeaders([("Tailscale-User-Login", "remote@example.invalid"),
                          ("Host", "adj.example.ts.net")])
    s = A.resolve_session(h, {}, reviewers, 8502)
    assert (s.access, s.via_proxy) == (A.RESTRICTED, True)  # its entry says FULL
    reviewers.remote_full_context_authorized = True
    assert A.resolve_session(h, {}, reviewers, 8502).access == A.FULL
    local = {A.LOCAL_IDENTITY_ENV: "reviewer@example.invalid"}
    assert A.resolve_session(StreamlitHeaders([("Host", "127.0.0.1:8502")]), local, reviewers,
                             8502).access == A.FULL
    for h, env, code in (
            ([("Host", "adj.example.ts.net")], local, "NOT_LOCAL"),
            ([("Host", "127.0.0.1:8502"), ("X-Forwarded-For", "100.64.0.9")], local,
             "PROXY_WITHOUT_IDENTITY"),
            ([("Host", "127.0.0.1:8502")], {A.LOCAL_IDENTITY_ENV: "mallory@x"}, "NOT_LISTED"),
            ([("Tailscale-User-Login", "mallory@x")], {}, "NOT_LISTED")):
        with pytest.raises(A.AdjudicationError, match=code):
            A.resolve_session(StreamlitHeaders(h), env, reviewers, 8502)


def test_the_app_serves_a_proxy_session_restricted_without_reading_the_posts(
        monkeypatch, local, base):
    (base / "bundle" / "posts.jsonl").chmod(0)
    try:
        at = app(monkeypatch, base, proxy="remote@example.invalid")
        text = rendered(at) + plain(at)
        assert "RESTRICTED access, through the access proxy" in text
        assert "never loads the source posts" in text
        assert not any(w in text for w in POST_WORDS)
        assert [e.value for e in app(monkeypatch, base, proxy="mallory@x").error] == [
            "Not authorized."]
    finally:
        (base / "bundle" / "posts.jsonl").chmod(0o600)


# ------------------------------------------------------------------ the workflow


def pick(at, item_id):
    return at.selectbox(key="aw_pick").set_value(item_id).run()


def test_blind_first_hides_the_system_until_an_independent_reading_and_writes_only_the_log(
        monkeypatch, local, base):
    before, repo_before = tree(base), {str(p) for p in (REPO / "src").rglob("*.py")}
    at = pick(app(monkeypatch, base, "reviewer@example.invalid"), "i07")
    text = rendered(at) + plain(at)
    assert "Hidden until you save an independent reading" in text
    assert "holds SHIPPED" not in text and "system:" not in text
    assert "Short Zed Metals around 412" in plain(at) and "Zed Metals" not in rendered(at)
    at.selectbox(key="aw_i07_ind_action").set_value("OPEN").run()
    at.radio(key="aw_i07_ind_confidence").set_value("MEDIUM").run()
    at.button(key="aw_i07_ind_save").click().run()
    [ind] = records(base)
    assert (ind["phase"], ind["blind"], ind["reading"]["action"]) == (A.INDEPENDENT, True, "OPEN")
    assert "holds SHIPPED: HOLD" in plain(at) and "system: SHORT" in plain(at)
    prefix = f"aw_i07_after_{ind['seq']}"
    at.radio(key=f"{prefix}_confidence").set_value("HIGH").run()
    at.button(key=f"{prefix}_save").click().run()
    after = records(base)[-1]
    assert (after["phase"], after["blind"], after["refers_to"]) == (A.AFTER_REVEAL, True, 1)
    assert after["field_verdicts"]["action"] == "CONFIRM"
    assert after["field_verdicts"]["side"] == "CORRECT"  # the reviewer left it not stated
    changed = {k for k in set(before) | set(tree(base)) if before.get(k) != tree(base).get(k)}
    assert changed == {"annotations.jsonl"}
    assert {str(p) for p in (REPO / "src").rglob("*.py")} == repo_before


def test_a_single_field_correction_leaves_every_other_field_confirmed(monkeypatch, local, base):
    at = app(monkeypatch, base, "reviewer@example.invalid")
    at.toggle(key="aw_blind").set_value(False).run()
    at = pick(at, "i04")
    prefix = "aw_i04_after_0"
    at.text_input(key=f"{prefix}_stop_price").set_value("88.00").run()
    at.radio(key=f"{prefix}_confidence").set_value("HIGH").run()
    at.button(key=f"{prefix}_save").click().run()
    [rec] = records(base)
    system = A.system_reading(bundle(base), bundle(base).items["i04"])
    assert (rec["phase"], rec["blind"], rec["refers_to"]) == (A.AFTER_REVEAL, False, None)
    assert {f for f, v in rec["reading"].items() if v != system[f]} == {"stop_price"}
    assert rec["reading"]["stop_price"] == "88"
    assert {f for f, v in rec["field_verdicts"].items() if v != "CONFIRM"} == {"stop_price"}
    assert rec["field_verdicts"]["stop_price"] == "CORRECT"
    # Toggling blind-first back on cannot produce an "independent" reading of a seen item.
    with pytest.raises(A.AdjudicationError, match="SYSTEM_READING_ALREADY_SEEN"):
        A.append_annotation(base, bundle(base), REVIEWER, item_id="i04", phase=A.INDEPENDENT,
                            blind=True, reading={}, confidence="LOW")


def test_a_label_only_item_asks_for_the_label_flags_confidence_and_notes_only(
        monkeypatch, local, base):
    at = pick(app(monkeypatch, base, "reviewer@example.invalid"), "i09")
    reading = [w.key for w in at.selectbox if w.key.startswith("aw_i09_ind_")]
    assert reading == ["aw_i09_ind_post_label"] and not [
        w for w in at.text_input if w.key.startswith("aw_i09")]


def review(base, item_id, **reading) -> dict:
    b = bundle(base)
    ind = A.append_annotation(base, b, REVIEWER, item_id=item_id, phase=A.INDEPENDENT,
                              blind=True, reading=reading, confidence="MEDIUM")
    item, system = b.items[item_id], A.system_reading(b, b.items[item_id])
    return A.append_annotation(base, b, REVIEWER, item_id=item_id, phase=A.AFTER_REVEAL,
                               blind=True, reading=system, confidence="HIGH",
                               field_verdicts=A.prefill_verdicts(system, system, item["kind"]),
                               refers_to=ind["seq"])


def test_reviewer_and_approver_are_distinct(monkeypatch, local, base):
    after = review(base, "i01", action="OPEN")
    b = bundle(base)
    with pytest.raises(A.AdjudicationError, match="ROLE_MAY_NOT_WRITE_PHASE"):
        A.append_annotation(base, b, REVIEWER, item_id="i01", phase=A.APPROVAL, blind=False,
                            reading={}, refers_to=after["seq"], outcome="ACCEPT")
    promoted = A.Session(REVIEWER.login, "Ria", A.APPROVER, A.FULL, False)  # file edited later
    with pytest.raises(A.AdjudicationError, match="APPROVER_IS_REVIEWER"):
        A.append_annotation(base, b, promoted, item_id="i01", phase=A.APPROVAL, blind=False,
                            reading={}, refers_to=after["seq"], outcome="ACCEPT")
    at = pick(app(monkeypatch, base, "owner@example.invalid"), "i01")
    assert "line 2, AFTER_REVEAL by reviewer@example.invalid" in plain(at)
    prefix = f"aw_i01_appr_{after['seq']}"
    at.radio(key=f"{prefix}_decision").set_value("ACCEPT").run()
    at.button(key=f"{prefix}_save").click().run()
    last = records(base)[-1]
    assert (last["phase"], last["decision"], last["refers_to"]) == (A.APPROVAL, "ACCEPT", 2)
    assert last["annotator"]["login"] == "owner@example.invalid"


def test_the_log_is_chained_and_an_edit_disables_saving_and_freezing(monkeypatch, local, base):
    review(base, "i01", action="OPEN")
    log = A.annotations_path(base)
    lines = log.read_bytes().splitlines(keepends=True)
    log.write_bytes(lines[0].replace(b'"MEDIUM"', b'"LOW"') + lines[1])
    assert A.read_annotations(base).problem == "CHAIN_BROKEN at line 2"
    with pytest.raises(A.AdjudicationError, match="ANNOTATION_LOG_FAILS_VERIFICATION"):
        A.freeze(base)
    assert "fails verification" in rendered(app(monkeypatch, base, "reviewer@example.invalid"))


def test_freeze_resolves_approver_then_provisional_and_keeps_the_independent_reading(base):
    b = bundle(base)
    amended = review(base, "i01", action="ADD")  # independent differs from the final reading
    A.append_annotation(base, b, APPROVER, item_id="i01", phase=A.APPROVAL, blind=False,
                        reading={"stop_price": "87.1"}, refers_to=amended["seq"],
                        outcome="AMEND")
    provisional = review(base, "i03")
    returned = review(base, "i05")
    A.append_annotation(base, b, APPROVER, item_id="i05", phase=A.APPROVAL, blind=False,
                        reading={}, refers_to=returned["seq"], outcome="RETURN")
    A.append_annotation(base, b, REVIEWER, item_id="i07", phase=A.INDEPENDENT, blind=True,
                        reading={}, confidence="LOW")  # independent only: not resolved
    before = tree(base)
    path, manifest = A.freeze(base)
    rows = {r["item_id"]: r for r in map(json.loads, path.read_text().splitlines())}
    assert set(rows) == {"i01", "i03", "i05"} and path.name == "ground_truth_v1.jsonl"
    assert rows["i01"]["status"] == "AMENDED" and rows["i01"]["reading"]["stop_price"] == "87.1"
    assert rows["i01"]["reading"]["action"] == "OPEN"
    assert rows["i01"]["independent_reading"]["action"] == "ADD"
    assert rows["i03"]["status"] == "PROVISIONAL"
    assert rows["i03"]["source_seqs"]["after_reveal"] == provisional["seq"]
    assert rows["i05"]["status"] == "PROVISIONAL" and rows["i05"]["decision"] == "RETURN"
    new = set(tree(base)) - set(before)
    assert new == {"versions/ground_truth_v1.jsonl", "versions/ground_truth_v1.manifest.json"}
    assert all(tree(base)[k] == v for k, v in before.items())
    assert manifest["by_status"] == {"APPROVED": 0, "AMENDED": 1, "PROVISIONAL": 2}
    assert A.freeze(base)[0].name == "ground_truth_v2.jsonl"
    assert A.verify_versions(base) == [("ground_truth_v1.manifest.json", True),
                                       ("ground_truth_v2.manifest.json", True)]
    path.write_text(path.read_text().replace("PROVISIONAL", "APPROVED"))
    assert A.verify_versions(base)[0] == ("ground_truth_v1.manifest.json", False)


def test_the_workspace_imports_no_service_mapping_execution_broker_or_storage(base):
    forbidden = ("tradingbot.runtime.service", "tradingbot.runtime.mapping",
                 "tradingbot.execution", "tradingbot.ibkr", "tradingbot.ibkr_orders",
                 "tradingbot.storage", "tradingbot.validator", "ibapi")
    script = f"""
import sys
from streamlit import config as c
from streamlit.runtime.context import ContextProxy, StreamlitHeaders
from streamlit.testing.v1 import AppTest
c.set_option("server.address", "127.0.0.1"); c.set_option("browser.gatherUsageStats", False)
port = c.get_option("server.port")
ContextProxy.headers = property(lambda s: StreamlitHeaders([("Host", f"127.0.0.1:{{port}}")]))
at = AppTest.from_file({str(APP)!r}, default_timeout=60); at.run()
assert not at.exception and any("Zed Metals" in str(t.value) for t in at.text)
print(sorted(m for m in sys.modules if m.startswith({forbidden!r})))
"""
    env = {**os.environ, A.DIR_ENV: str(base), A.LOCAL_IDENTITY_ENV: "reviewer@example.invalid",
           "PYTHONPATH": str(REPO / "src")}
    out = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True,
                         text=True, timeout=120, check=False)
    assert out.returncode == 0, out.stderr[-2000:]
    assert out.stdout.strip().splitlines()[-1] == "[]"
    source = APP.read_text()
    for word in ("file_uploader", "download_button", "unsafe_allow_html", "st.markdown",
                 "st.write", "query_params", "cache_data", "logging", "print("):
        assert word not in source, word


def test_the_freeze_command_and_verify(base, capsys):
    spec = importlib.util.spec_from_file_location("adjudication_freeze",
                                                  REPO / "scripts" / "adjudication_freeze.py")
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    review(base, "i03")
    assert cli.main(["freeze", "--dir", str(base)]) == 0
    assert cli.main(["verify", "--dir", str(base)]) == 0
    out = capsys.readouterr().out
    assert "ground_truth_v1" in out and "annotation log: OK, 2 line(s)" in out
    assert not any(w in out for w in POST_WORDS)


def test_the_vocabulary_page_is_reachable_as_the_restricted_vocabulary_mode(
        monkeypatch, local, base):
    monkeypatch.delenv("TRADINGBOT_VOCAB_REVIEWER_EXPORT", raising=False)
    at = app(monkeypatch, base, "reviewer@example.invalid")
    at.switch_page(str(APP.with_name("reviewer_app.py"))).run()
    assert not at.exception
    assert "No review material is configured." in rendered(at)  # the vocabulary page's own check
