"""The operator tool of the hosted reviewer site (``scripts/hosted_admin.py``) on INVENTED data and
a real embedded Postgres: publishing the part of a bundle a scope needs (and the parity check that
proves it shows the same), reviewers and revocation, the verbatim import of the local log, parity,
hosted saves pulled back into a local log the existing code accepts, export and restore, verify,
and that no command prints an address, a name or a word of a post or a reading."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import stat
from pathlib import Path

import hosted_pg
import pytest
from hosted_pg import DEMO, REPO, hosted_reveal, hosted_save, reading_kw, write_demo

from tradingbot.runtime import adjudication as A
from tradingbot.runtime import vocab_recommend as V


def _script(name: str):
    spec = importlib.util.spec_from_file_location(name, REPO / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


dsn, store = hosted_pg.dsn, hosted_pg.store  # fixtures
FREEZE = _script("adjudication_freeze")
ONE = A.Session("reviewer@example.invalid", "Reviewer Example", A.REVIEWER, A.FULL, True, 40)
TWO = A.Session("remote@example.invalid", "Remote Reviewer Example", A.REVIEWER, A.FULL, True, 40)
OWNER = A.Session("owner@example.invalid", "Owner Example", A.APPROVER, A.FULL, False, 40)
TESTER = "tester@example.invalid"
# Nothing the tool prints may hold a sign-in, a name, or a word of a post or of a reading.
FORBIDDEN = ("@", "example.invalid", "Reviewer Example", "Owner Example", "Zed Metals", "TLT",
             "starter long", "bond crowd", "crude", "postgresql", "COMMENTARY", "LONG")
HOSTED_1 = 1_000_001


@pytest.fixture(scope="module")
def cli():
    pytest.importorskip("psycopg", reason=hosted_pg.NEEDS)
    return _script("hosted_admin")


@pytest.fixture
def base(tmp_path) -> Path:
    return write_demo(tmp_path / "adjudication")


@pytest.fixture
def run(cli, monkeypatch, capsys, dsn, base):
    """Run one subcommand against the test database and the demo folder; returns what it
    printed. EVERY call checks the exit code and that the output names no one and quotes
    nothing."""
    monkeypatch.setenv(cli.URL_ENV, dsn)
    monkeypatch.delenv(cli.UNPOOLED_ENV, raising=False)
    monkeypatch.setenv(A.DIR_ENV, str(base))

    def run(*argv, code: int = 0) -> str:
        capsys.readouterr()
        got = cli.main([str(a) for a in argv])
        printed = "".join(capsys.readouterr())
        assert not [w for w in (*FORBIDDEN, str(base.parent)) if w in printed]
        assert got == code, printed
        return printed

    return run


def local_save(base: Path, session: A.Session, item_id: str, phase: str, **kw) -> dict:
    b = A.load_bundle(base / "bundle", A.FULL)
    records = A.read_annotations(base).records
    if phase == A.AFTER_REVEAL and "refers_to" not in kw:
        kw["refers_to"] = A._of(records, item_id, b.bundle_id, A.INDEPENDENT,
                                session.login)[-1]["seq"]
    return A.append_annotation(base, b, session, **reading_kw(
        b, item_id, records, session, phase=phase, **kw))


def half_way(base: Path) -> None:
    """A local log as the legacy site leaves it: the first remote reviewer has finished one form,
    saved another blind and has seen the system's reading of a third; the second has begun; the
    approver (whose lines never leave the Mac) has accepted one reading."""
    b = A.load_bundle(base / "bundle", A.FULL)
    local_save(base, ONE, "i07", A.INDEPENDENT)  # line 1
    A.mark_system_shown(base, b, ONE, "i07")  # 2
    local_save(base, ONE, "i07", A.AFTER_REVEAL)  # 3
    local_save(base, ONE, "i08", A.INDEPENDENT)  # 4
    local_save(base, TWO, "i07", A.INDEPENDENT)  # 5
    A.append_annotation(base, b, OWNER, item_id="i07", phase=A.APPROVAL, blind=False,
                        reading={}, refers_to=3, outcome="ACCEPT")  # 6
    local_save(base, ONE, "i01", A.INDEPENDENT)  # 7
    A.mark_system_shown(base, b, ONE, "i01")  # 8


def cutover(run, store, base) -> dict:
    """The cutover on the demo data; returns the reviewer rows by alias (TEST-1 included)."""
    half_way(base)
    run("init-db")
    run("sync-reviewers", "--test-identity", "TEST-1", TESTER)
    run("publish-bundle", "--demo", base)
    run("import-log")
    return {r.alias: r for r in store.reviewers()}


def hosted_work(store, rows) -> None:
    """Saves made on the hosted site after the cutover (seven hosted lines)."""
    one, two = rows["REMOTE-2"], rows["REMOTE-3"]
    hosted_reveal(store, one, "i08")  # 1000001
    hosted_save(store, one, "i08", A.AFTER_REVEAL)  # 1000002, refers to the local line 4
    hosted_save(store, one, "i03", A.INDEPENDENT)  # 1000003
    hosted_reveal(store, one, "i03")  # 1000004
    hosted_save(store, one, "i03", A.AFTER_REVEAL)  # 1000005, refers to the hosted 1000003
    hosted_save(store, two, "i08", A.INDEPENDENT)  # 1000006
    hosted_save(store, rows["TEST-1"], "i07", A.INDEPENDENT)  # 1000007: never pulled


def log_lines(base: Path) -> list[bytes]:
    return A.annotations_path(base).read_bytes().splitlines()


# ------------------------------------------------------------------ the published part


def edit(path: Path, change) -> None:
    rows = {r.get("item_id") or r.get("post_ref"): r
            for r in map(json.loads, path.read_text().splitlines())}
    change(rows)
    path.write_text("".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n"
                            for r in rows.values()))


def reseal(base: Path, **manifest_changes) -> None:
    path = base / "bundle" / "manifest.json"
    manifest = {**json.loads(path.read_text()), **manifest_changes}
    path.write_text(json.dumps(manifest))
    manifest["bundle_id"] = A.bundle_digest(base / "bundle")
    path.write_text(json.dumps(manifest))


def extend(base: Path, scope: int) -> None:
    """The demo bundle with what a real one has and the shipped demo lacks: system readings that
    refer to items OUTSIDE the scope (another idea's, and one of no sequence), a second form of a
    post that belongs to another idea, a quote chain into posts outside the scope, and an image
    only an outside post lists. ``scope``: the reviewers file's ``scope_sequences``."""
    def items(rows):
        rows["i08"]["ai"]["idea_ref"] = "s01a"
        rows["i05"]["ai"]["idea_ref"] = "i09"
        rows["x01"] = {**rows["i07"], "item_id": "x01", "sequence_id": "scs01", "slot": 1,
                       "order_in_sequence": 2, "context_refs": ["s01"],
                       "ai": {**rows["i07"]["ai"], "idea_ref": "s01a"}}

    def posts(rows):
        rows["p05"]["quotes"] = "s02"
        rows["s02"]["reply_to"] = "s03"
        rows["s04"]["media"] = ["chart-s04.png"]

    edit(base / "bundle" / "items.jsonl", items)
    edit(base / "bundle" / "posts.jsonl", posts)
    fd = os.open(base / "bundle" / "media" / "chart-s04.png", os.O_WRONLY | os.O_CREAT, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(DEMO._png() + b"another invented chart")
    path = base / "reviewers.toml"
    path.write_text(path.read_text().replace("scope_sequences = 40",
                                             f"scope_sequences = {scope}"))
    reseal(base)


IN_SCOPE = ["i07", "i08", "i01", "i02", "i03", "i04", "i05", "i06"]  # seqB, then seqA


def test_publish_bundle_uploads_only_what_the_scope_needs_and_proves_parity(run, store, base):
    extend(base, 2)
    full = A.load_bundle(base / "bundle", A.FULL)
    assert A.system_reading(full, full.items["i08"])["idea"] == "SEQUENCE:scs01"
    assert [f["item_id"] for f in A.post_forms(full, full.items["i07"])] == ["i07", "x01"]
    out = run("publish-bundle", "--demo", base, "--check-only")
    assert "parity with the full bundle: PASS, 8 item(s) in scope, 0 difference(s)" in out
    assert "checked only: nothing uploaded" in out and store.published() is None
    out = run("publish-bundle", "--demo", base)
    assert (f"bundle {full.bundle_id[:12]}: scope 2 of 33 sequence(s); items 8 shown, 1 kept for "
            "form numbering, 37 left out; posts 10 of 41; images 1,") in out
    assert "parity with the full bundle: PASS" in out and "published and active" in out
    part = store.active_bundle()
    assert part.bundle_id == full.bundle_id and store.bundle_is_demo() is True
    assert {k: store.published()[k] for k in ("source_sequences", "scope_sequences", "images")} \
        == {"source_sequences": 33, "scope_sequences": 2, "images": 1}
    # The items the scope offers, the second form of a post they share, and nothing else.
    assert [i["item_id"] for _s, items in A.ordered(part, 2) for i in items] == IN_SCOPE
    assert set(part.items) == {*IN_SCOPE, "x01"}
    stub = part.items["x01"]  # its place among the forms and its market, nothing else
    assert (stub["post_ref"], stub["slot"], stub["sequence_id"], stub["ai"]["family"]) == (
        "p07", 1, "scs01", "OTHER")
    assert (stub["context_refs"], stub["levels"], stub["holds"], stub["ai"]["action"],
            stub["ai"]["instrument_as_read"]) == ([], [], {}, "", "")
    # Only the posts those items, their context and their quote chain need; one image.
    assert set(part.posts) == {"p01", "p02", "p03", "p04", "p05", "p06", "p07", "p08", "s02",
                               "s03"}
    with store.connection() as conn:
        stored = bytes(conn.execute("SELECT posts FROM bundle").fetchone()[0])
        assert conn.execute("SELECT name FROM media").fetchall() == [("chart-p01.png",)]
    assert json.dumps(full.posts["s01"]["text"])[1:-1].encode() not in stored
    assert json.dumps(full.posts["s03"]["text"])[1:-1].encode() in stored
    assert store.media("p01", "chart-p01.png") == (
        (base / "bundle" / "media" / "chart-p01.png").read_bytes(), "image/png")
    # Every idea keeps its id and its place; one outside the scope keeps no post.
    assert [s["sequence_id"] for s in part.sequences] == [s["sequence_id"]
                                                         for s in full.sequences]
    assert [s["post_refs"] for s in part.sequences] == [
        ["p07", "p08"], ["p01", "p02", "p03", "p04", "p05", "p06"], *([[]] * 31)]
    # What a reviewer sees is the same: the idea association of a reference that left, the
    # number of forms of a shared post, the ideas offered.
    assert A.system_reading(part, part.items["i08"])["idea"] == "SEQUENCE:scs01"
    assert A.system_reading(part, part.items["i05"])["idea"] == "UNKNOWN" == A.system_reading(
        full, full.items["i05"])["idea"]
    assert A.form_context(part, part.items["i07"], revealed=True) == A.form_context(
        full, full.items["i07"], revealed=True)
    assert A.form_context(part, part.items["i07"], revealed=True).forms_in_post == 2


def test_a_scope_that_covers_every_sequence_publishes_the_items_of_no_sequence_too(
        run, store, base):
    extend(base, 40)
    full = A.load_bundle(base / "bundle", A.FULL)
    out = run("publish-bundle", "--demo", base)
    assert "scope 33 of 33 sequence(s); items 46 shown, 0 kept for form numbering, 0 left out; " \
           "posts 41 of 41; images 2," in out and "PASS" in out
    part = store.active_bundle()
    assert list(part.items) == list(full.items) and "i09" in part.items
    assert [(s, [i["item_id"] for i in g]) for s, g in A.ordered(part, 40)] == [
        (s, [i["item_id"] for i in g]) for s, g in A.ordered(full, 40)]
    assert part.items == full.items  # nothing rewritten, nothing stripped


def test_a_wrong_part_fails_parity_and_is_not_uploaded(cli, run, store, base, monkeypatch):
    extend(base, 2)
    full, files, media = cli.subset(base, 2)
    assert cli.parity_problems(full, A.bundle_from_bytes(*files, access=A.FULL), media, 2) == []

    def wrong(changes: dict) -> list[bytes]:
        """The part with ``changes[n]`` applied to the rows (by id) of its file ``n``."""
        given = list(files)
        for n, change in changes.items():
            rows = {r.get("item_id") or r.get("post_ref") or r.get("sequence_id"): r
                    for r in map(json.loads, files[n].decode().splitlines())}
            change(rows)
            given[n] = "".join(json.dumps(r) + "\n" for r in rows.values()).encode()
        return given

    def problems(changes: dict, images=None) -> set:
        return set(cli.parity_problems(
            full, A.bundle_from_bytes(*wrong(changes), access=A.FULL),
            media if images is None else images, 2))

    def keep_the_reference(rows):
        rows["i08"]["ai"]["idea_ref"] = "s01a"  # the item it names is not in the part

    def without_the_other_form(rows):
        del rows["x01"]

    def only_the_scope_s_ideas(rows):
        for sid in [s for s in rows if s not in ("seqA", "seqB")]:
            del rows[sid]

    assert problems({2: keep_the_reference}) == {("system_reading", "i08")}
    assert problems({2: without_the_other_form}) == {("post_forms", "i07"),
                                                     ("form_context", "i07")}
    assert problems({2: lambda rows: rows["i01"]["mapping"].update(reference_series="SPX")}) \
        == {("item", "i01")}
    assert problems({2: lambda rows: rows.pop("i06")}) == {("ordered", "-")}
    assert problems({3: lambda rows: rows["p02"].update(text="Another invented sentence.")}) \
        == {("evidence", "i02"), ("evidence", "i03"), ("evidence", "i04")}
    assert problems({3: lambda rows: rows["p04"].update(text="Raising the stop to 88.00 now.")}) \
        >= {("table_row_details", "i04"), ("evidence", "i04"), ("evidence", "i05")}
    assert problems({}, images={"chart-p01.png": DEMO._png() + b"."}) == {
        ("evidence", i) for i in ("i01", "i02", "i03", "i04", "i05")}
    # Without the ideas outside the scope the ``idea`` choices shrink for every form.
    assert problems({1: only_the_scope_s_ideas, 2: without_the_other_form}) >= {
        ("idea_options", i) for i in IN_SCOPE} | {("system_reading", "i08")}
    # The command: FAIL, exit 1, nothing uploaded.
    monkeypatch.setattr(cli, "subset", lambda _base, _scope: (
        full, wrong({2: keep_the_reference}), media))
    out = run("publish-bundle", "--demo", base, code=1)
    assert "parity with the full bundle: FAIL, 8 item(s) in scope, 1 difference(s)" in out
    assert "differs: system_reading of i08" in out and store.published() is None


def test_publish_bundle_refuses_real_content_without_the_owner_s_switch(run, store, base,
                                                                        tmp_path):
    assert "DEMO_BUNDLE_NEEDS_DEMO_FLAG" in run("publish-bundle", code=2)
    closed = write_demo(tmp_path / "closed", remote_full_context=False)
    reseal(closed, product_commit="abc1234")  # not the demo script's bundle any more
    assert "NOT_A_DEMO_BUNDLE" in run("publish-bundle", "--demo", closed, code=2)
    assert "REMOTE_FULL_CONTEXT_NOT_AUTHORIZED" in run("publish-bundle", "--dir", closed, code=2)
    assert store.published() is None
    reseal(base, product_commit="abc1234")
    assert "published and active" in run("publish-bundle") and store.bundle_is_demo() is False
    (base / "bundle" / "items.jsonl").write_text("")  # the files no longer match the bundle_id
    assert "BUNDLE_ID_MISMATCH" in run("publish-bundle", code=2)


def test_the_published_scope_and_the_reviewers_scope_stay_consistent(run, store, base):
    extend(base, 2)
    run("publish-bundle", "--demo", base)
    assert "REMOTE-2: id 1, REVIEWER, FULL, scope 2" in run("sync-reviewers")
    path = base / "reviewers.toml"
    path.write_text(path.read_text().replace("scope_sequences = 2", "scope_sequences = 3"))
    assert "SCOPE_NOT_PUBLISHED" in run("sync-reviewers", code=2)  # publish the larger part first
    assert {r.scope_sequences for r in store.reviewers()} == {2}
    run("publish-bundle", "--demo", base)
    assert "scope 3" in run("sync-reviewers")
    path.write_text(path.read_text().replace("scope_sequences = 3", "scope_sequences = 1"))
    assert "SCOPE_BELOW_A_REVIEWER" in run("publish-bundle", "--demo", base, code=2)
    assert store.published()["scope_sequences"] == 3
    run("sync-reviewers")  # lowering the reviewers first is always possible
    run("publish-bundle", "--demo", base)
    assert store.published()["scope_sequences"] == 1


# ------------------------------------------------------------------ reviewers


def test_sync_reviewers_reads_the_owner_s_file_and_never_deletes(run, store, base):
    file = base / "reviewers.toml"
    before = (file.read_bytes(), file.stat().st_mtime_ns)
    out = run("init-db")
    assert out.startswith("schema applied: 5 table(s): bundle, chain_anchor, event, media, ")
    out = run("sync-reviewers")
    assert out.splitlines() == ["REMOTE-2: id 1, REVIEWER, FULL, scope 40",
                                "REMOTE-3: id 2, REVIEWER, FULL, scope 40",
                                "2 reviewer row(s) written; none deleted"]
    assert store.reviewer_by_login(ONE.login).session() == ONE
    assert "TEST_ALIAS_IS_TEST_N" in run("sync-reviewers", "--test-identity", "REMOTE-9", TESTER,
                                         code=2)
    out = run("sync-reviewers", "--test-identity", "TEST-1", TESTER)
    assert "TEST-1: id 3, REVIEWER, FULL, scope 40, test identity" in out
    assert store.reviewer_by_login(TESTER).is_test is True
    # An entry that left the file keeps its row (revoke it); the file itself is only read.
    file.write_text(file.read_text().split("[[reviewer]]\nlogin = \"remote@")[0])
    assert run("sync-reviewers").splitlines()[-1] == "1 reviewer row(s) written; none deleted"
    assert [r.alias for r in store.reviewers()] == ["REMOTE-2", "REMOTE-3", "TEST-1"]
    file.write_bytes(before[0])
    os.utime(file, ns=(before[1], before[1]))
    for command in (["sync-reviewers"], ["publish-bundle", "--demo", base], ["import-log"],
                    ["parity"], ["status"]):
        run(*command)
    assert (file.read_bytes(), file.stat().st_mtime_ns) == before


def test_revoke_stops_access_at_once_and_restore_access_lifts_it(run, store, base):
    run("sync-reviewers")
    with store.connection() as conn:  # the web app's table, as its part of the schema names it
        conn.execute("CREATE TABLE web_session (token_sha256 TEXT PRIMARY KEY,"
                     " reviewer_id BIGINT NOT NULL REFERENCES reviewer (id))")
        conn.execute("INSERT INTO web_session VALUES ('a', 1), ('b', 1), ('c', 2)")
    assert run("revoke", "REMOTE-2") == "REMOTE-2: REVOKED; 2 web session(s) deleted\n"
    assert store.reviewer_by_login(ONE.login).revoked is True
    assert "REMOTE-2: id 1, REVIEWER, FULL, scope 40, REVOKED" in run("sync-reviewers")
    with store.connection() as conn:
        assert conn.execute("SELECT token_sha256 FROM web_session").fetchall() == [("c",)]
    assert "revoked 2" in run("status").replace("revoked -", "")
    assert run("restore-access", "REMOTE-2") == \
        "REMOTE-2: access restored; 0 web session(s) deleted\n"
    assert store.reviewer_by_login(ONE.login).revoked is False
    assert "UNKNOWN_ALIAS" in run("revoke", "REMOTE-9", code=2)


def test_the_database_address_comes_from_the_environment_and_is_never_printed(
        cli, run, monkeypatch, dsn):
    monkeypatch.setenv(cli.UNPOOLED_ENV, dsn)  # schema changes go over the direct connection
    monkeypatch.setenv(cli.URL_ENV, "postgresql://nobody@127.0.0.1:1/none?connect_timeout=1")
    assert "schema applied" in run("init-db")
    assert run("status", code=2) == "refused: DATABASE_OperationalError\n"
    monkeypatch.delenv(cli.URL_ENV)
    assert run("status", code=2) == "refused: SET_HOSTED_DATABASE_URL\n"
    monkeypatch.delenv(A.DIR_ENV)
    monkeypatch.setenv(cli.URL_ENV, dsn)
    assert run("import-log", code=2) == f"refused: SET_{A.DIR_ENV}\n"


# ------------------------------------------------------------------ the cutover


def test_import_log_is_verbatim_verifiable_and_idempotent(run, store, base):
    half_way(base)
    run("sync-reviewers")
    run("publish-bundle", "--demo", base)
    log = A.annotations_path(base)
    data, lines = log.read_bytes(), log_lines(base)
    out = run("import-log")
    assert f"local log: 8 line(s), head {V._sha(lines[-1])[:12]}, {len(data)} bytes, sha256 " \
           f"{V._sha(data)}" in out
    assert "imported 7 new line(s); 0 already imported" in out
    assert out.split("REMOTE-2: ")[1].splitlines()[:4] == [
        ("lines 6 (6 new); forms 45: DONE 1, INDEPENDENT_SAVED 1, INDEPENDENT_SAVED+SHOWN 1, "
         "OPEN 42"), "  i07 DONE", "  i08 INDEPENDENT_SAVED", "  i01 INDEPENDENT_SAVED+SHOWN"]
    assert "REMOTE-3: lines 1 (1 new); forms 45: INDEPENDENT_SAVED 1, OPEN 44" in out
    # LEGACY events: the remote reviewers' lines, byte for byte, under their own seq. The
    # approver's line 6 never leaves the Mac.
    events = store.events()
    assert [(e["seq"], e["alias"], e["origin"], e["line"]) for e in events] == [
        (n, "REMOTE-3" if n == 5 else "REMOTE-2", "LEGACY", lines[n - 1])
        for n in (1, 2, 3, 4, 5, 7, 8)]
    assert all(e["sha256"] == V._sha(e["line"]) for e in events)
    assert all(ONE.login.encode() not in e["line"] for e in events if e["alias"] == "REMOTE-3")
    # The anchor: the log's state, and the sha256 of the bytes that were copied.
    anchor = store.anchor()
    assert (anchor["count"], anchor["head"], anchor["file_sha256"]) == (
        8, V._sha(lines[-1]), V._sha(data))
    folder = base / "hosted_snapshots" / f"log-00000008-{V._sha(data)[:16]}"
    assert f"snapshot: {folder.name}" in out
    assert (folder / "annotations.jsonl").read_bytes() == data
    assert (folder / "annotations.jsonl.head").read_bytes() == V.head_path(log).read_bytes()
    assert (folder / "annotations.jsonl.sha256").read_text().split() == [
        V._sha(data), "annotations.jsonl"]
    assert [stat.S_IMODE(p.stat().st_mode) for p in (folder.parent, folder,
                                                     *sorted(folder.iterdir()))] == [
        0o700, 0o700, 0o600, 0o600, 0o600]
    assert "parity of the local log and the hosted store: PASS, 2 reviewer(s)" in run("parity")
    assert "verify: PASS, 0 problem(s)" in run("verify")
    # Again: nothing new. After new local lines: only those, and the anchor follows the log.
    assert "imported 0 new line(s); 7 already imported" in run("import-log")
    assert store.events() == events and store.anchor()["count"] == 8
    local_save(base, TWO, "i07", A.AFTER_REVEAL)  # the legacy site is still serving
    A.append_annotation(base, A.load_bundle(base / "bundle", A.FULL), OWNER, item_id="i07",
                        phase=A.APPROVAL, blind=False, reading={}, refers_to=3, outcome="RETURN")
    out = run("import-log")
    assert "imported 1 new line(s); 7 already imported" in out
    assert "REMOTE-3: lines 2 (1 new); forms 45: DONE 1, OPEN 44" in out
    assert [e["seq"] for e in store.events()] == [1, 2, 3, 4, 5, 7, 8, 9]
    assert (store.anchor()["count"], store.anchor()["head"]) == (10, V._sha(log_lines(base)[-1]))
    assert len(list((base / "hosted_snapshots").iterdir())) == 2
    assert "PASS" in run("parity") and "PASS" in run("verify")


def test_parity_names_each_reviewer_s_resume_point_and_fails_on_a_difference(run, store, base):
    rows = cutover(run, store, base)
    out = run("parity")
    assert "bundle: local " in out and ": the same" in out
    assert ("REMOTE-2: records local 6, hosted 6: the same; forms 45: 45 equal, 0 different; "
            "hosted lines not pulled: 0; resumes at i08 INDEPENDENT_SAVED") in out
    assert "REMOTE-3: records local 1, hosted 1: the same" in out
    assert "resumes at i07 INDEPENDENT_SAVED" in out and "TEST-1" not in out
    hosted_reveal(store, rows["REMOTE-2"], "i08")
    hosted_save(store, rows["REMOTE-2"], "i08", A.AFTER_REVEAL)
    out = run("parity", code=1)  # the site is ahead of the local log until the next pull
    assert ("REMOTE-2: records local 6, hosted 8: DIFFERENT; forms 45: 44 equal, 1 different; "
            "hosted lines not pulled: 2; resumes at i01 INDEPENDENT_SAVED+SHOWN") in out
    assert "  i08: local INDEPENDENT_SAVED, hosted DONE" in out
    assert out.splitlines()[-1] == \
        "parity of the local log and the hosted store: FAIL, 2 reviewer(s)"
    run("pull-to-local")
    assert "resumes at i01 INDEPENDENT_SAVED+SHOWN" in run("parity")


def test_import_log_refuses_a_log_that_is_not_the_imported_one(run, store, base, tmp_path):
    cutover(run, store, base)
    log = A.annotations_path(base)
    whole, head = log.read_bytes(), V.head_path(log).read_bytes()
    lines = whole.splitlines(keepends=True)
    # The last imported line removed, with a head file that matches the shorter log.
    log.write_bytes(b"".join(lines[:-1]))
    V.head_path(log).write_text(json.dumps({"count": 7, "sha256": V._sha(lines[6][:-1])}))
    assert A.read_annotations(base).ok
    assert run("import-log", code=2) == "refused: IMPORTED_PREFIX_DIFFERS\n"
    assert run("pull-to-local", code=2) == "refused: LOCAL_LOG_IS_NOT_THE_ANCHORED_LOG\n"
    log.write_bytes(whole)
    assert run("import-log", code=2) == "refused: ANNOTATION_LOG_FAILS_VERIFICATION\n"
    V.head_path(log).write_bytes(head)
    assert "imported 0 new line(s)" in run("import-log")
    # Another folder's log (the same bundle and reviewers, other saves).
    other = write_demo(tmp_path / "other")
    local_save(other, TWO, "i08", A.INDEPENDENT)
    assert run("import-log", "--dir", other, code=2) == "refused: IMPORTED_PREFIX_DIFFERS\n"
    assert run("pull-to-local", "--dir", other, code=2) == \
        "refused: LOCAL_LOG_IS_NOT_THE_ANCHORED_LOG\n"
    assert len(store.events()) == 7


# ------------------------------------------------------------------ hosted saves, back home


def test_hosted_saves_come_home_into_a_log_the_local_code_accepts(run, store, base):
    rows = cutover(run, store, base)
    hosted_work(store, rows)
    hosted = {e["seq"]: e for e in store.events() if e["origin"] == "HOSTED"}
    before = log_lines(base)
    out = run("pull-to-local")
    assert "pulled 6 hosted line(s) into the local log: REMOTE-2 5, REMOTE-3 1; 0 already held" \
        in out and "annotation log: OK, 14 line(s)" in out
    lines = log_lines(base)
    assert lines[:8] == before  # the log only grew
    log = A.read_annotations(base)
    assert log.ok and log.count == 14
    pulled = log.records[8:]
    assert [(r["seq"], r["hosted"]["seq"], r["phase"], r["item_id"], r["refers_to"])
            for r in pulled] == [
        (9, HOSTED_1, A.SYSTEM_SHOWN, "i08", None),
        (10, HOSTED_1 + 1, A.AFTER_REVEAL, "i08", 4),  # the local line it always meant
        (11, HOSTED_1 + 2, A.INDEPENDENT, "i03", None),
        (12, HOSTED_1 + 3, A.SYSTEM_SHOWN, "i03", None),
        (13, HOSTED_1 + 4, A.AFTER_REVEAL, "i03", 11),  # 1000003 is line 11 here
        (14, HOSTED_1 + 5, A.INDEPENDENT, "i08", None)]
    for r in pulled:  # the same record: its own time, reading and annotator; plus where it was
        source = json.loads(hosted[r["hosted"]["seq"]]["line"])
        assert r["hosted"] == {"seq": source["seq"], "sha256": hosted[source["seq"]]["sha256"]}
        assert {k: v for k, v in r.items() if k not in ("seq", "prev_sha256", "hosted",
                                                        "refers_to")} == {
            k: v for k, v in source.items() if k not in ("seq", "prev_sha256", "refers_to")}
    assert TESTER not in A.annotations_path(base).read_text()  # a test identity stays hosted
    # The existing functions see one consistent history.
    b = A.load_bundle(base / "bundle", A.FULL)
    status = A.progress(b, log.records, ONE).status
    assert (status["i07"], status["i08"], status["i03"], status["i01"]) == (
        "DONE", "DONE", "DONE", "INDEPENDENT_SAVED")
    assert A.latest_own_reading(log.records, "i03", b, ONE.login)["seq"] == 13
    assert A.progress(b, log.records, TWO).status["i08"] == "INDEPENDENT_SAVED"
    assert "PASS" in run("parity") and "PASS" in run("verify")
    # Again: nothing is appended twice. More hosted work: only that.
    assert "pulled 0 hosted line(s) into the local log: -; 6 already held" in run(
        "pull-to-local")
    assert log_lines(base) == lines
    hosted_save(store, rows["REMOTE-3"], "i07", A.AFTER_REVEAL)  # refers to the local line 5
    assert "pulled 1 hosted line(s) into the local log: REMOTE-3 1; 6 already held" in run(
        "pull-to-local")
    assert A.read_annotations(base).records[-1]["refers_to"] == 5
    assert "imported 0 new line(s); 7 already imported" in run("import-log")  # copies are known
    # Local work goes on: the approver decides on a pulled reading, the legacy site could
    # resume (a local reading after the reveal that refers to a pulled independent one).
    A.append_annotation(base, b, OWNER, item_id="i08", phase=A.APPROVAL, blind=False,
                        reading={}, refers_to=10, outcome="ACCEPT")
    later = local_save(base, TWO, "i08", A.AFTER_REVEAL)
    assert (later["seq"], later["refers_to"], later["blind"]) == (17, 14, True)
    assert FREEZE.main(["verify", "--dir", str(base)]) == 0
    path, manifest = A.freeze(base)
    frozen = {r["item_id"]: r for r in map(json.loads, path.read_text().splitlines())}
    assert manifest["annotation_log"]["count"] == 17
    assert (frozen["i08"]["status"], frozen["i08"]["reviewer"], frozen["i08"]["source_seqs"]) == (
        "APPROVED", ONE.login, {"independent": 4, "after_reveal": 10, "approval": 16})
    assert (frozen["i03"]["status"], frozen["i03"]["source_seqs"]["independent"]) == (
        "PROVISIONAL", 11)
    assert frozen["i03"]["independent_reading"] is not None
    assert FREEZE.main(["verify", "--dir", str(base)]) == 0
    # A reviewer with saves on the site AND later local saves has two histories: the import
    # refuses to mix them (start again from an empty database, or pull and keep the site).
    assert run("import-log", code=2) == "refused: LOCAL_SAVE_AFTER_HOSTED_SAVES\n"


def test_pull_to_local_refuses_a_store_that_fails_verification(run, store, base):
    rows = cutover(run, store, base)
    hosted_work(store, rows)
    with store.connection() as conn:  # what the trigger forbids, done by a database owner
        conn.execute("ALTER TABLE event DISABLE TRIGGER USER")
        conn.execute("UPDATE event SET line = convert_to(replace(convert_from(line, 'UTF8'),"
                     " 'HIGH', 'LOW'), 'UTF8') WHERE seq = %s", (HOSTED_1 + 1,))
        conn.execute("UPDATE chain_anchor SET snapshot_file_sha256 = repeat('0', 64)")
    before = log_lines(base)
    assert run("pull-to-local", code=2) == "refused: HOSTED_STORE_FAILS_VERIFICATION\n"
    assert log_lines(base) == before
    out = run("verify", code=1)
    assert "hosted chain on " in out and ": FAILS" in out
    assert f"  LINE_DIFFERS_FROM_ITS_ROW at {HOSTED_1 + 1}" in out
    assert "  SNAPSHOT_IS_NOT_THE_ANCHORED_LOG" in out
    assert out.splitlines()[-1] == "verify: FAIL, 2 problem(s)"


def test_verify_checks_the_chain_the_anchor_and_the_imported_lines(cli, run, store, base):
    rows = cutover(run, store, base)
    hosted_work(store, rows)
    out = run("verify")
    assert "events: LEGACY 7, HOSTED 7; hosted chain on " in out and ": OK" in out
    assert "7 LEGACY line(s) compared with it" in out and "verify: PASS" in out
    # A hosted line that does not continue the chain (stored by other means than the site).
    last = json.loads(store.events()[-1]["line"])
    stray = V.encode_line({**last, "seq": HOSTED_1 + 9})
    store.insert_events([cli.S.EventLine(rows["REMOTE-2"].id, "HOSTED", stray)])
    assert f"  HOSTED_CHAIN_BROKEN at {HOSTED_1 + 9}" in run("verify", code=1)
    # A snapshot that was edited, and none at all.
    [folder] = (base / "hosted_snapshots").iterdir()
    copy = folder / "annotations.jsonl"
    copy.write_bytes(copy.read_bytes().replace(b'"HIGH"', b'"LOW"', 1))
    assert "  SNAPSHOT_FAILS_VERIFICATION" in run("verify", code=1)
    assert "  NO_SNAPSHOT" in run("verify", "--snapshots", base / "nowhere", code=1)


# ------------------------------------------------------------------ export, restore


def test_an_export_restored_into_an_empty_database_is_identical(cli, run, store, base,
                                                                monkeypatch, tmp_path):
    rows = cutover(run, store, base)
    hosted_work(store, rows)
    out_file = tmp_path / "exports" / "events.jsonl"
    out = run("export", "--out", out_file)
    data = out_file.read_bytes()
    assert f"exported 14 event(s): LEGACY 7, HOSTED 7; {len(data)} bytes, sha256 " \
           f"{V._sha(data)}" in out
    assert stat.S_IMODE(out_file.stat().st_mode) == 0o600
    assert run("export", "--out", out_file, code=2) == "refused: FileExistsError\n"
    header, *exported = map(json.loads, data.decode().splitlines())
    assert header["events"] == 14 and header["anchor"]["count"] == 8
    assert [(e["alias"], e["origin"], e["seq"]) for e in exported][7:] == [
        ("REMOTE-2", "HOSTED", HOSTED_1), ("REMOTE-2", "HOSTED", HOSTED_1 + 1),
        ("REMOTE-2", "HOSTED", HOSTED_1 + 2), ("REMOTE-2", "HOSTED", HOSTED_1 + 3),
        ("REMOTE-2", "HOSTED", HOSTED_1 + 4), ("REMOTE-3", "HOSTED", HOSTED_1 + 5),
        ("TEST-1", "HOSTED", HOSTED_1 + 6)]
    assert [e["line"].encode() for e in exported[:7]] == [
        log_lines(base)[n - 1] for n in (1, 2, 3, 4, 5, 7, 8)]
    assert run("restore", "--in", out_file, code=2) == "refused: DATABASE_NOT_EMPTY\n"
    # An EMPTY database: the schema, then the reviewers (an export names aliases only).
    events, anchor = store.events(), store.anchor()
    empty = hosted_pg.fresh_database()
    monkeypatch.setenv(cli.URL_ENV, empty)
    run("init-db")
    assert run("restore", "--in", out_file, code=2) == "refused: UNKNOWN_ALIAS\n"
    run("sync-reviewers", "--test-identity", "TEST-1", TESTER)
    out = run("restore", "--in", out_file)
    assert "restored 14 event(s): LEGACY 7, HOSTED 7; every hash and the hosted chain " \
           f"verified; file sha256 {V._sha(data)}" in out
    restored = cli.S.AdminStore(empty)
    assert restored.events() == events and restored.anchor() == anchor
    assert "verify: PASS" in run("verify")
    again = tmp_path / "exports" / "again.jsonl"
    run("export", "--out", again)
    assert again.read_bytes().splitlines()[1:] == data.splitlines()[1:]
    # The restored site goes on where the other stopped: the next line continues the chain.
    run("publish-bundle", "--demo", base)
    [nxt] = hosted_save(restored, restored.reviewer_by_login(TWO.login), "i08",
                        A.AFTER_REVEAL).records
    assert (nxt["seq"], nxt["prev_sha256"], nxt["refers_to"]) == (
        HOSTED_1 + 7, events[-1]["sha256"], HOSTED_1 + 5)
    assert "verify: PASS" in run("verify")
    run("pull-to-local")
    assert A.read_annotations(base).count == 8 + 7


def test_restore_refuses_an_export_that_fails_verification(cli, run, store, base, monkeypatch,
                                                           tmp_path):
    rows = cutover(run, store, base)
    hosted_work(store, rows)
    run("export", "--out", tmp_path / "x" / "good.jsonl")
    good = (tmp_path / "x" / "good.jsonl").read_text().splitlines()
    empty = hosted_pg.fresh_database()
    monkeypatch.setenv(cli.URL_ENV, empty)
    run("init-db")
    run("sync-reviewers", "--test-identity", "TEST-1", TESTER)

    def restore(name: str, lines: list[str]) -> str:
        path = tmp_path / "x" / name
        path.write_text("".join(f"{x}\n" for x in lines))
        path.chmod(0o600)
        return run("restore", "--in", path, code=2)

    edited = good[3].replace("MEDIUM", "LOW").replace("HIGH", "LOW")
    assert edited != good[3]
    for name, lines, code in (
            ("edited", [*good[:3], edited, *good[4:]], "EXPORT_FAILS_VERIFICATION"),
            ("gap", [*good[:9], *good[10:]], "EXPORT_UNREADABLE"),  # the count says 14
            ("gap2", [json.dumps({**json.loads(good[0]), "events": 13}), *good[1:9], *good[10:]],
             "EXPORT_FAILS_VERIFICATION"),  # a hosted line is missing from the chain
            ("anchor", [json.dumps({**json.loads(good[0]), "anchor": None}), *good[1:]],
             "EXPORT_FAILS_VERIFICATION"),  # the first hosted line names another head
            ("empty", [], "EXPORT_UNREADABLE"),
            ("other", ["{}"], "EXPORT_UNREADABLE")):
        assert restore(name, lines) == f"refused: {code}\n", name
    assert run("restore", "--in", tmp_path / "x" / "nowhere.jsonl", code=2) == \
        "refused: EXPORT_UNREADABLE\n"
    assert cli.S.AdminStore(empty).events() == []


# ------------------------------------------------------------------ status


def test_status_gives_times_and_counts_by_phase_per_alias(run, store, base):
    assert run("status").splitlines() == ["bundle: none published",
                                          "anchor: none (no log imported)"]
    rows = cutover(run, store, base)
    hosted_work(store, rows)
    with store.connection() as conn:  # what the web app records at the first sign-in
        conn.execute("UPDATE reviewer SET invited_at = '2026-03-08T09:00:00Z',"
                     " first_sign_in_at = '2026-03-09T07:15:00Z' WHERE alias = 'REMOTE-2'")
    run("revoke", "REMOTE-3")
    out = run("status").splitlines()
    assert re.match(r"bundle [0-9a-f]{12}: demonstration data, published \d{4}-\d\d-\d\dT",
                    out[0])
    assert "scope 40 of 33 sequence(s)" in out[0] and "1 image(s)" in out[0]
    assert out[1].startswith("anchor: 8 line(s), head ")
    first = json.loads(store.events()[7]["line"])["at"][:19]
    assert out[2] == ("REMOTE-2: scope 40, invited 2026-03-08T09:00:00+00:00, first sign-in "
                      f"2026-03-09T07:15:00+00:00, first hosted save {first}+00:00, revoked -")
    assert out[3:5] == ["  LEGACY: INDEPENDENT 3, SYSTEM_SHOWN 2, AFTER_REVEAL 1",
                        "  HOSTED: SYSTEM_SHOWN 2, AFTER_REVEAL 2, INDEPENDENT 1"]
    assert re.fullmatch(r"REMOTE-3: scope 40, invited -, first sign-in -, first hosted save "
                        r"\d{4}-\S+, revoked \d{4}-\S+", out[5])
    assert out[6:8] == ["  LEGACY: INDEPENDENT 1", "  HOSTED: INDEPENDENT 1"]
    assert out[8].startswith("TEST-1 (test identity): scope 40,")
    assert out[9:] == ["  HOSTED: INDEPENDENT 1"]
