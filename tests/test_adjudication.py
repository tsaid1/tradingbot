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

import _review_modes as modes
import pytest
from _review_modes import PUB, published, with_headers
from streamlit import config as st_config
from streamlit.runtime.context import StreamlitHeaders
from streamlit.testing.v1 import AppTest

from tradingbot.runtime import adjudication as A

REPO = Path(__file__).resolve().parents[1]
APP = REPO / "dashboard" / "reviewer" / "adjudication_app.py"
_spec = importlib.util.spec_from_file_location(
    "adjudication_demo_bundle", REPO / "scripts" / "adjudication_demo_bundle.py")
DEMO = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(DEMO)
REVIEWER = A.Session("reviewer@example.invalid", "Reviewer Example", A.REVIEWER, A.FULL, False)
APPROVER = A.Session("owner@example.invalid", "Owner", A.APPROVER, A.FULL, False)
local = modes.local  # fixture
POST_WORDS = ("starter long", "Zed Metals", "bond crowd")


@pytest.fixture
def base(tmp_path) -> Path:
    return DEMO.write_demo(tmp_path / "adjudication")


def bundle(base, access=A.FULL) -> A.Bundle:
    return A.load_bundle(base / "bundle", access)


def reseal(base: Path) -> None:
    """Recompute the manifest's bundle_id after an edit (the spec's recipe)."""
    path = base / "bundle" / "manifest.json"
    manifest = json.loads(path.read_text())
    if isinstance(manifest, dict) and "bundle_id" in manifest:
        manifest["bundle_id"] = A.bundle_digest(base / "bundle")
        path.write_text(json.dumps(manifest))


def rewrite(path: Path, edit) -> None:
    rows = [json.loads(x) for x in path.read_text().splitlines()]
    path.write_text("".join(json.dumps(r) + "\n" for r in edit(rows)))
    reseal(path.parents[1] if path.parent.name == "bundle" else path.parents[2])


def app(monkeypatch, base, login=None, *, proxy=None) -> AppTest:
    """LOCAL mode with the local identity ``login``; or PUBLISHED mode with the proxy's
    ``proxy`` login."""
    monkeypatch.setenv(A.DIR_ENV, str(base))
    monkeypatch.delenv(A.LOCAL_IDENTITY_ENV, raising=False)
    if proxy:
        published(monkeypatch)
        with_headers(monkeypatch, **{"Tailscale-User-Login": proxy})
    else:
        if login:
            monkeypatch.setenv(A.LOCAL_IDENTITY_ENV, login)
        with_headers(monkeypatch)
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


def records(base, *, markers: bool = False) -> list[dict]:
    """The annotation records (the SYSTEM_SHOWN markers only when asked)."""
    v = A.read_annotations(base)
    assert v.ok, v.problem
    return [r for r in v.records if markers or r["phase"] != A.SYSTEM_SHOWN]


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
    ("items.jsonl", lambda rs: [{**r, "ai": {**r["ai"], "entry": {
        "role": "ENTRY_AT_STATED_PRICE"}}} for r in rs], "UNKNOWN_VALUE:entry.role"),  # 1.1 value
    ("items.jsonl", lambda rs: [{**r, "ai": {**r["ai"], "levels": [{"n": 9, "price": 1,
                                                                    "role": "STOP"}]}}
                                for r in rs], "LEVEL_NOT_IN_NUMBERS"),
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


def test_sessions_follow_the_configured_mode_never_header_guessing(base):
    reviewers = A.load_reviewers(base / "reviewers.toml")
    pub = {A.PUBLISHED_HOST_ENV: PUB}
    proxied = [("Tailscale-User-Login", "remote@example.invalid"), ("Host", PUB),
               ("Origin", f"https://{PUB}")]
    s = A.resolve_session(StreamlitHeaders(proxied), pub, reviewers, 8620)
    assert (s.access, s.via_proxy) == (A.RESTRICTED, True)  # its entry says FULL
    reviewers.remote_full_context_authorized = True
    assert A.resolve_session(StreamlitHeaders(proxied), pub, reviewers, 8620).access == A.FULL
    reviewers.remote_full_context_authorized = False
    local = {A.LOCAL_IDENTITY_ENV: "reviewer@example.invalid"}
    loop = [("Host", "127.0.0.1:8620"), ("Origin", "http://127.0.0.1:8620")]
    s = A.resolve_session(StreamlitHeaders(loop), local, reviewers, 8620)
    assert (s.access, s.via_proxy) == (A.FULL, False)
    for h, env, code in (
            # LOCAL: any forwarding, proxy or identity header refuses the request outright.
            (loop + [("X-Real-Ip", "100.64.0.9")], local, "PROXY_HEADER_IN_LOCAL_MODE"),
            (loop + [("Via", "1.1 proxy")], local, "PROXY_HEADER_IN_LOCAL_MODE"),
            (loop + [("Tailscale-User-Login", "owner@example.invalid")], local,
             "PROXY_HEADER_IN_LOCAL_MODE"),
            ([("Host", "127.0.0.1:8620"), ("Origin", "http://localhost:9999")], local,
             "ORIGIN_NOT_THIS_SERVER"),  # another localhost port
            ([("Host", PUB)], local, "NOT_LOCAL"),
            (loop, {A.LOCAL_IDENTITY_ENV: "mallory@x"}, "NOT_LISTED"),
            # PUBLISHED: Host and Origin exactly the published host; identity only from the proxy.
            (proxied[1:], {**pub, A.LOCAL_IDENTITY_ENV: "owner@example.invalid"},
             "NO_PROXY_IDENTITY"),
            ([proxied[0], ("Host", "127.0.0.1:8620"), proxied[2]], pub,
             "HOST_OR_ORIGIN_NOT_PUBLISHED_HOST"),
            ([*proxied[:2], ("Origin", "http://localhost:9999")], pub,
             "HOST_OR_ORIGIN_NOT_PUBLISHED_HOST")):
        with pytest.raises(A.AdjudicationError, match=code):
            A.resolve_session(StreamlitHeaders(h), env, reviewers, 8620)


def test_published_mode_refuses_to_start_with_a_local_identity_or_a_wrong_configuration(
        monkeypatch, local, base):
    published(monkeypatch)
    monkeypatch.setenv(A.LOCAL_IDENTITY_ENV, "owner@example.invalid")
    assert A.server_problem(st_config.get_option, os.environ).startswith(
        A.LOCAL_IDENTITY_ENV)
    monkeypatch.delenv(A.LOCAL_IDENTITY_ENV)
    assert A.server_problem(st_config.get_option, os.environ) is None
    st_config.set_option("server.corsAllowedOrigins", ["*"])
    assert "'*'" in A.server_problem(st_config.get_option, os.environ)
    st_config.set_option("client.showErrorDetails", "full")
    assert "showErrorDetails" in A.server_problem(st_config.get_option, os.environ)
    monkeypatch.delenv(A.PUBLISHED_HOST_ENV)  # LOCAL mode with a proxy configured
    st_config.set_option("client.showErrorDetails", "none")
    st_config.set_option("server.corsAllowedOrigins", [f"https://{PUB}"])
    assert A.PUBLISHED_HOST_ENV in A.server_problem(st_config.get_option, os.environ)


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
    # The price table: one row per number of the post, shown as its token, nothing pre-filled.
    assert [at.selectbox(key=f"aw_i07_ind_lv_n:{k}_{f}").value for k in (0, 1)
            for f in A.LEVEL_KEYS] == [""] * 6
    assert "412" in plain(at) and "CURRENT_MARKET" not in text and "CHART_LEVEL" not in text
    at.selectbox(key="aw_i07_ind_lv_n:0_role").set_value("CURRENT_MARKET").run()
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
    assert ind["reading"]["levels"][0] == {"row": "n:0", "role": "CURRENT_MARKET",
                                           "exactness": "", "contract_month": ""}
    assert after["field_verdicts"]["levels"] == {"n:0": "CORRECT", "n:1": "UNSURE"}
    changed = {k for k in set(before) | set(tree(base)) if before.get(k) != tree(base).get(k)}
    assert changed == {"annotations.jsonl", "annotations.jsonl.head"}
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
    assert {f for f in system if rec["reading"][f] != system[f]} == {"stop_price"}
    assert rec["reading"]["stop_price"] == "88"
    assert {f for f, v in rec["field_verdicts"].items() if v != "CONFIRM"} == {
        "stop_price", "levels"}
    assert rec["field_verdicts"]["levels"] == {"n:0": "CONFIRM"}
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
                               field_verdicts={**A.prefill_verdicts(system, system,
                                                                    item["kind"]),
                                               "levels": {k: "UNSURE" for k, _ in
                                                          A.table_rows(b, item)}},
                               refers_to=ind["seq"])


def test_reviewer_and_approver_are_distinct(monkeypatch, local, base):
    after = review(base, "i01", action="OPEN")
    b = bundle(base)
    with pytest.raises(A.AdjudicationError, match="ROLE_MAY_NOT_WRITE_PHASE"):
        A.append_annotation(base, b, REVIEWER, item_id="i01", phase=A.APPROVAL, blind=False,
                            reading={}, refers_to=after["seq"], outcome="ACCEPT")
    promoted = A.Session(REVIEWER.login, "Reviewer Example", A.APPROVER, A.FULL, False)  # file edited later
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
                        reading={"stop_price": "87.1", "levels": [
                            {"row": "n:1", "role": "STOP", "exactness": "EXACT",
                             "contract_month": "NOT_APPLICABLE"}]}, refers_to=amended["seq"],
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
    assert [r["role"] for r in rows["i01"]["reading"]["levels"]] == ["", "STOP", ""]
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
c.set_option("client.showErrorDetails", "none")
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


def test_a_per_row_price_correction_is_saved_alone(monkeypatch, local, base):
    at = app(monkeypatch, base, "reviewer@example.invalid")
    at.toggle(key="aw_blind").set_value(False).run()
    at = pick(at, "i04")
    prefix = "aw_i04_after_0"
    assert at.selectbox(key=f"{prefix}_lv_n:0_role").value == "CURRENT_MARKET"  # blind off
    at.selectbox(key=f"{prefix}_lv_n:0_role").set_value("STOP").run()
    at.text_input(key=f"{prefix}_stop_price").set_value("87").run()  # keep the system's stop
    at.radio(key=f"{prefix}_confidence").set_value("HIGH").run()
    at.button(key=f"{prefix}_save").click().run()
    [rec] = records(base)
    system = A.system_reading(bundle(base), bundle(base).items["i04"])
    assert all(rec["reading"][f] == system[f] for f in system)
    assert rec["reading"]["levels"] == [{"row": "n:0", "role": "STOP", "exactness": "EXACT",
                                         "contract_month": "NOT_APPLICABLE"}]
    assert rec["field_verdicts"]["levels"] == {"n:0": "CORRECT"}
    assert all(v == "CONFIRM" for f, v in rec["field_verdicts"].items() if f != "levels")


def test_only_the_scope_is_offered(monkeypatch, local, base):
    path = base / "reviewers.toml"
    path.write_text(path.read_text().replace("scope_sequences = 20", "scope_sequences = 1"))
    at = app(monkeypatch, base, "reviewer@example.invalid")
    assert [o.split(":")[0] for o in at.selectbox(key="aw_pick").options] == ["seqB 1", "seqB 2"]
    assert "Scope: first 1 ideas (of 2)" in plain(at)
    scoped = A.Session(REVIEWER.login, "Reviewer Example", A.REVIEWER, A.FULL, False, 1)
    with pytest.raises(A.AdjudicationError, match="OUTSIDE_SCOPE"):
        A.append_annotation(base, bundle(base), scoped, item_id="i01", phase=A.INDEPENDENT,
                            blind=True, reading={}, confidence="LOW")


# ------------------------------------------------------------------ review fixes (C1, C2, S1-S6, n1-n4)


def test_c1_the_bundle_id_is_recomputed_and_a_mismatch_refused(base):
    assert bundle(base).bundle_id == A.bundle_digest(base / "bundle")  # the demo's id agrees
    posts = base / "bundle" / "posts.jsonl"
    posts.write_text(posts.read_text().replace("starter long", "starter lung"))
    with pytest.raises(A.AdjudicationError, match="BUNDLE_ID_MISMATCH"):
        bundle(base, A.FULL)
    with pytest.raises(A.AdjudicationError, match="BUNDLE_ID_MISMATCH"):
        A.freeze(base)
    assert bundle(base, A.RESTRICTED).posts is None  # RESTRICTED never reads the posts to check


def test_c2_a_number_the_system_did_not_read_is_its_own_state(monkeypatch, local, base):
    b = bundle(base)
    assert A.system_rows(b, b.items["i06"]) == {"n:0": {"role": "", "exactness": "",
                                                        "contract_month": "", "not_read": True}}
    at = app(monkeypatch, base, "reviewer@example.invalid")
    at.toggle(key="aw_blind").set_value(False).run()
    at = pick(at, "i06")
    assert "system: not read by the system" in plain(at)
    assert "not read by the system: number 0" in plain(at)
    prefix = "aw_i06_after_0"
    at.selectbox(key=f"{prefix}_lv_n:0_role").set_value("HISTORICAL_FILL").run()
    at.radio(key=f"{prefix}_confidence").set_value("LOW").run()
    at.button(key=f"{prefix}_save").click().run()
    [rec] = records(base)
    assert rec["field_verdicts"]["levels"] == {"n:0": A.NOT_READ}
    assert rec["reading"]["levels"][0]["role"] == "HISTORICAL_FILL"
    with pytest.raises(A.AdjudicationError, match="VERDICT_FOR_EVERY_COMPARED_FIELD"):
        A.append_annotation(base, b, REVIEWER, item_id="i06", phase=A.AFTER_REVEAL, blind=False,
                            reading=rec["reading"], confidence="LOW",
                            field_verdicts={**rec["field_verdicts"], "levels": {"n:0": "CORRECT"}})


def test_s1_blind_first_holds_across_sessions(monkeypatch, local, base):
    at = app(monkeypatch, base, "reviewer@example.invalid")
    at.toggle(key="aw_blind").set_value(False).run()
    pick(at, "i07")  # the system's reading is served: the marker is written first
    [marker] = [r for r in records(base, markers=True) if r["item_id"] == "i07"]
    assert marker["phase"] == A.SYSTEM_SHOWN and marker["annotator"]["login"] == REVIEWER.login
    again = pick(app(monkeypatch, base, "reviewer@example.invalid"), "i07")  # a reload
    assert again.session_state["aw_blind"] is True
    assert not [b for b in again.button if b.key == "aw_i07_ind_save"]  # no independent form
    with pytest.raises(A.AdjudicationError, match="SYSTEM_READING_ALREADY_SEEN"):
        A.append_annotation(base, bundle(base), REVIEWER, item_id="i07", phase=A.INDEPENDENT,
                            blind=True, reading={}, confidence="LOW")
    review(base, "i01")
    with pytest.raises(A.AdjudicationError, match="SYSTEM_READING_ALREADY_SEEN"):  # a second one
        A.append_annotation(base, bundle(base), REVIEWER, item_id="i01", phase=A.INDEPENDENT,
                            blind=True, reading={}, confidence="LOW")
    assert {r["item_id"] for r in A.resolve(records(base, markers=True), bundle(base))} == {"i01"}


def test_s1_nothing_of_the_system_is_shown_when_the_marker_cannot_be_written(
        monkeypatch, local, base):
    at = app(monkeypatch, base, "reviewer@example.invalid")
    base.chmod(0o500)  # the annotation log cannot be created
    try:
        at.toggle(key="aw_blind").set_value(False).run()
        text = rendered(at) + plain(at)
        assert "cannot be shown" in text and "holds SHIPPED" not in text and "system:" not in text
    finally:
        base.chmod(0o700)


def test_s2_private_folders_must_be_0700_and_a_synced_folder_is_warned_or_refused(
        monkeypatch, local, base):
    (base / "bundle" / "media").chmod(0o755)
    assert rendered(app(monkeypatch, base, "reviewer@example.invalid")).count(
        "PRIVATE_FOLDER_NOT_0700") == 1
    with pytest.raises(A.AdjudicationError, match="PRIVATE_FOLDER_NOT_0700"):
        A.freeze(base)
    (base / "bundle" / "media").chmod(0o700)
    monkeypatch.setattr(A, "synced_folder_class", lambda p: "CLOUD_STORAGE")
    at = app(monkeypatch, base, "reviewer@example.invalid")
    assert "PRIVATE_FOLDER_IN_SYNCED_FOLDER (CLOUD_STORAGE)" in rendered(at) and at.title
    at = app(monkeypatch, base, proxy="remote@example.invalid")
    assert "refused (PRIVATE_FOLDER_IN_SYNCED_FOLDER CLOUD_STORAGE)" in rendered(at)
    assert not at.title


def test_s5_both_apps_refuse_error_details(monkeypatch, local, base):
    st_config.set_option("client.showErrorDetails", "full")
    for path in (APP, APP.with_name("reviewer_app.py")):
        monkeypatch.setenv(A.DIR_ENV, str(base))
        with_headers(monkeypatch)
        at = AppTest.from_file(str(path), default_timeout=60)
        at.run()
        assert "client.showErrorDetails must be none" in rendered(at)


def test_n13_restricted_free_text_only_a_number_a_family_or_an_instrument_word(
        monkeypatch, local, base):
    rewrite(base / "bundle" / "items.jsonl", lambda rs: [
        {**r, "mapping": {**r["mapping"], "reference_series": "LONG ZED NEAR 412 STOP 420"}}
        if r["item_id"] == "i01" else r for r in rs])
    b = bundle(base, A.RESTRICTED)
    assert b.items["i07"]["withheld"] == ["instrument_as_read"]  # "Zed Metals"
    assert b.items["i01"]["ai"]["instrument_as_read"] == "TLT"  # a manifest family
    assert b.items["i01"]["ai"]["amendment_from"] == ""
    assert b.items["i04"]["ai"]["amendment_from"] == "87.2"  # a number
    assert set(b.items["i01"]["withheld"]) == {"mapping.reference_series",  # upper-case excerpt
                                                "mapping.proposed_execution_instrument"}  # 2 words
    at = pick(app(monkeypatch, base, proxy="remote@example.invalid"), "i07")
    at.toggle(key="aw_blind").set_value(False).run()
    assert f"instrument_as_read: (withheld: {A.WITHHELD})" in plain(at)
    assert "Zed" not in rendered(at) + plain(at)
    [radio] = [r for r in at.radio if r.key.startswith("aw_i07_after_0_verdict_instrument_as_read")]
    assert radio.value is None  # a withheld field's verdict is never pre-filled
    at.radio(key="aw_i07_after_0_confidence").set_value("LOW").run()
    at.button(key="aw_i07_after_0_save").click().run()
    [rec] = records(base)
    assert "instrument_as_read" not in rec["field_verdicts"] and rec["field_verdicts"]["side"]


def test_n15_a_finder_ds_store_is_ignored_and_no_other_dot_file_tolerated(base):
    for folder in (base / "bundle", base / "bundle" / "media"):
        (folder / ".DS_Store").write_bytes(b"\x00\x01finder")
        (folder / ".DS_Store").chmod(0o644)
    assert bundle(base, A.FULL).bundle_id == A.bundle_digest(base / "bundle")
    (base / "bundle" / "media" / ".hidden").write_bytes(b"x")
    with pytest.raises(A.AdjudicationError, match="DOT_FILE_IN_BUNDLE"):
        bundle(base, A.FULL)


def _repair_cli():
    spec = importlib.util.spec_from_file_location("adjudication_freeze_r",
                                                  REPO / "scripts" / "adjudication_freeze.py")
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    return cli


def test_n14_repair_head_repairs_exactly_the_two_situations(base, capsys):
    from tradingbot.runtime.vocab_recommend import _sha, head_path

    cli = _repair_cli()
    review(base, "i01")
    log, head = A.annotations_path(base), head_path(A.annotations_path(base))
    lines = log.read_bytes().splitlines()
    # (i) a crash between line 2 and its head: the head still names line 1
    head.write_text(json.dumps({"count": 1, "sha256": _sha(lines[0])}))
    assert A.read_annotations(base).problem.startswith("HEAD_MISMATCH")
    assert cli.main(["repair-head", "--dir", str(base)]) == 0  # without --yes: nothing written
    assert not A.read_annotations(base).ok
    assert cli.main(["repair-head", "--dir", str(base), "--yes"]) == 0
    out = capsys.readouterr().out
    assert "CRASH_BETWEEN_LINE_AND_HEAD: last line 2, phase AFTER_REVEAL, item i01" in out
    assert A.read_annotations(base).ok
    # (ii) no head file (a log from before head files, or a deleted head)
    head.unlink()
    assert cli.main(["repair-head", "--dir", str(base), "--yes"]) == 0
    assert "NO_HEAD_FILE: last line 2" in capsys.readouterr().out and A.read_annotations(base).ok
    good, good_head = log.read_bytes(), head.read_bytes()

    def refused(data: bytes, head_bytes: bytes | None, code: str):
        log.write_bytes(data)
        if head_bytes is None:
            head.unlink(missing_ok=True)
        else:
            head.write_bytes(head_bytes)
            head.chmod(0o600)
        assert cli.main(["repair-head", "--dir", str(base), "--yes"]) == 2
        assert code in capsys.readouterr().err
        assert log.read_bytes() == data  # never a line removed or rewritten

    refused(lines[0] + b"\n", good_head, "HEAD_NOT_REPAIRABLE")  # truncated log
    refused(lines[0] + b"\n" + lines[1].replace(b'"HIGH"', b'"LOW"') + b"\n", good_head,
            "HEAD_NOT_REPAIRABLE")  # an edited last line
    refused(lines[0].replace(b'"MEDIUM"', b'"LOW"') + b"\n" + lines[1] + b"\n", None,
            "CHAIN_BROKEN_NOT_REPAIRED")  # an edited earlier line
    refused(good, json.dumps({"count": 3, "sha256": "f" * 64}).encode(),
            "HEAD_NOT_REPAIRABLE")  # a head pointing past the end


def test_n14_repair_head_for_the_recommendations_log(tmp_path, monkeypatch, capsys):
    from tradingbot.runtime import vocab_recommend as R

    export = tmp_path / "rv" / "export.json"
    R.write_export(export, [])
    rows = [{"id": "0123456789abcdef", "token": "plinkish", "kind": None, "posts": 1,
             "holds": 1, "families": [], "reasons": [], "market_warning": False,
             "group": "NON_MARKET", "family": None}]
    log = R.recommendations_path(export)
    R.append_recommendation(log, rows, login="reviewer@example.invalid", name="",
                            proposal=rows[0]["id"], verdict="REJECT")
    R.head_path(log).unlink()
    assert R.verify_log(log).problem == "HEAD_FILE_MISSING"
    monkeypatch.setenv(R.EXPORT_ENV, str(export))
    assert _repair_cli().main(["repair-head", "--recommendations", "--yes"]) == 0
    assert "NO_HEAD_FILE: last line 1, phase RECOMMENDATION, item 0123456789abcdef" in \
        capsys.readouterr().out
    assert R.verify_log(log).ok


def test_n17_the_app_names_the_repair_command_for_a_log_without_head(monkeypatch, local, base):
    review(base, "i01")
    from tradingbot.runtime.vocab_recommend import head_path

    head_path(A.annotations_path(base)).unlink()
    text = rendered(app(monkeypatch, base, "reviewer@example.invalid"))
    assert "HEAD_FILE_MISSING" in text and "scripts/adjudication_freeze.py repair-head" in text


def test_n1_an_edit_of_the_last_line_is_detected(base):
    review(base, "i01")
    log = A.annotations_path(base)
    lines = log.read_bytes().splitlines(keepends=True)
    log.write_bytes(b"".join(lines[:-1]) + lines[-1].replace(b'"HIGH"', b'"LOW"'))
    assert A.read_annotations(base).problem.startswith("HEAD_MISMATCH")
    log.write_bytes(b"".join(lines[:-1]))  # the last line removed
    assert A.read_annotations(base).problem.startswith("HEAD_MISMATCH")
    log.write_bytes(b"".join(lines))
    assert A.read_annotations(base).ok
    from tradingbot.runtime.vocab_recommend import head_path

    head_path(log).unlink()
    assert A.read_annotations(base).problem == "HEAD_FILE_MISSING"


def test_n2_notes_lose_control_line_break_and_direction_characters(base):
    b = bundle(base)
    rec = A.append_annotation(base, b, REVIEWER, item_id="i09", phase=A.INDEPENDENT, blind=True,
                              reading={}, confidence="LOW",
                              notes="fine\nline 9, APPROVAL by owner\u202egnp.\tend")
    assert rec["notes"] == "fine line 9, APPROVAL by owner gnp. end"


def test_n3_a_reviewer_revision_after_approval_returns_the_item_to_provisional(base):
    after = review(base, "i03")
    b = bundle(base)
    A.append_annotation(base, b, APPROVER, item_id="i03", phase=A.APPROVAL, blind=False,
                        reading={}, refers_to=after["seq"], outcome="ACCEPT")
    assert A.resolve(records(base), b)[0]["status"] == "APPROVED"
    system = A.system_reading(b, b.items["i03"])
    A.append_annotation(base, b, REVIEWER, item_id="i03", phase=A.AFTER_REVEAL, blind=True,
                        reading={**system, "stop_price": "87"}, confidence="HIGH",
                        refers_to=after["refers_to"],
                        field_verdicts={**A.prefill_verdicts(system, system, "INSTRUCTION"),
                                        "levels": {"n:0": "UNSURE"}})
    [row] = A.resolve(records(base), b)
    assert row["status"] == "PROVISIONAL" and row["reading"]["stop_price"] == "87"
    assert A.progress(b, records(base), APPROVER).status["i03"] == "AWAITING"


def test_n4_the_demo_refuses_inside_the_repository_before_creating_anything():
    target = REPO / "demo-adjudication-n4"
    with pytest.raises(SystemExit, match="inside this program's repository"):
        DEMO.write_demo(target)
    assert not target.exists()


def test_resuming_finds_the_next_open_item_from_the_log_alone(monkeypatch, local, base):
    review(base, "i07")  # the first item in review order is done; nothing else
    at = app(monkeypatch, base, "reviewer@example.invalid")  # a fresh session (a restart)
    at.button(key="aw_next").click().run()
    assert at.selectbox(key="aw_pick").value == "i08"
    assert "Items done" in [m.label for m in at.metric]
    assert [m.value for m in at.metric if m.label == "Items done"] == ["1 of 9"]
