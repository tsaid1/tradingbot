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
import sys
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
             "starter long", "bond crowd", "crude", "postgresql", "COMMENTARY", "NOT_STATED",
             "CURRENT_MARKET", "Approver")
HOSTED_1 = 1_000_001
TEST_1 = 3_000_001  # the first line of the test identities' own stream


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
    """The cutover on the demo data, as on a preview database (the remote reviewers work on the
    demonstration bundle); returns the reviewer rows by alias (TEST-1 included)."""
    half_way(base)
    run("init-db")
    run("publish-bundle", "--demo", base)
    run("sync-reviewers", "--demo")
    run("sync-reviewers", "--test-identity", "TEST-1", TESTER)
    run("import-log")
    return {r.alias: r for r in store.reviewers()}


def hosted_work(store, rows) -> None:
    """Saves made on the hosted site after the cutover: six lines of the reviewers' chain and
    one of the test identity, in its own stream."""
    one, two = rows["REMOTE-2"], rows["REMOTE-3"]
    hosted_reveal(store, one, "i08")  # 1000001
    hosted_save(store, one, "i08", A.AFTER_REVEAL)  # 1000002, refers to the local line 4
    hosted_save(store, one, "i03", A.INDEPENDENT)  # 1000003
    hosted_reveal(store, one, "i03")  # 1000004
    hosted_save(store, one, "i03", A.AFTER_REVEAL)  # 1000005, refers to the hosted 1000003
    hosted_save(store, two, "i08", A.INDEPENDENT)  # 1000006
    hosted_save(store, rows["TEST-1"], "i07", A.INDEPENDENT)  # 3000001: never pulled


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
        rows["i01"]["exporter_note"] = "a key the workspace never reads"
        rows["x01"] = {**rows["i07"], "item_id": "x01", "sequence_id": "scs01", "slot": 1,
                       "order_in_sequence": 2, "context_refs": ["s01"],
                       "ai": {**rows["i07"]["ai"], "idea_ref": "s01a"}}

    def posts(rows):
        rows["p05"]["quotes"] = "s02"
        rows["s02"]["reply_to"] = "s03"
        rows["s04"]["media"] = ["chart-s04.png"]
        rows["p01"]["source_handle"] = "a key the workspace never reads"

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
    assert "checked only: nothing uploaded" in out and store.published(demo=True) is None
    out = run("publish-bundle", "--demo", base)
    assert (f"bundle {full.bundle_id[:12]}: scope 2 of 33 sequence(s); items 8 shown, 1 kept for "
            "form numbering, 37 left out; posts 10 of 41; images 1,") in out
    assert "parity with the full bundle: PASS" in out and "published and active" in out
    part = store.active_bundle()
    assert part.bundle_id == full.bundle_id and store.bundle_is_demo() is True
    assert {k: store.published(demo=True)[k]
            for k in ("source_sequences", "scope_sequences", "images")} == {
        "source_sequences": 33, "scope_sequences": 2, "images": 1}
    assert store.published(demo=False) is None
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
        stored = b"".join(map(bytes, conn.execute("SELECT posts, items FROM bundle").fetchone()))
        assert conn.execute("SELECT name FROM media").fetchall() == [("chart-p01.png",)]
    assert b"never reads" not in stored and b"exporter_note" not in stored  # read keys only
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
    assert "differs: system_reading of i08" in out and store.published(demo=True) is None


def test_publish_bundle_refuses_real_content_without_the_owner_s_switch(run, store, base,
                                                                        tmp_path):
    assert "DEMO_BUNDLE_NEEDS_DEMO_FLAG" in run("publish-bundle", code=2)
    closed = write_demo(tmp_path / "closed", remote_full_context=False)
    reseal(closed, product_commit="abc1234")  # not the demo script's bundle any more
    assert "NOT_A_DEMO_BUNDLE" in run("publish-bundle", "--demo", closed, code=2)
    assert "REMOTE_FULL_CONTEXT_NOT_AUTHORIZED" in run("publish-bundle", "--dir", closed, code=2)
    assert store.published(demo=False) is None and store.published(demo=True) is None
    reseal(base, product_commit="abc1234")
    assert "published and active" in run("publish-bundle") and store.bundle_is_demo() is False
    assert store.published(demo=False)["bundle_id"] == A.bundle_digest(base / "bundle")
    (base / "bundle" / "items.jsonl").write_text("")  # the files no longer match the bundle_id
    assert "BUNDLE_ID_MISMATCH" in run("publish-bundle", code=2)


def test_the_published_scope_and_the_reviewers_scope_stay_consistent(run, store, base):
    extend(base, 2)
    run("publish-bundle", "--demo", base)
    assert "REMOTE-2: id 1, REVIEWER, FULL, scope 2, bundle " in run("sync-reviewers", "--demo")
    path = base / "reviewers.toml"
    path.write_text(path.read_text().replace("scope_sequences = 2", "scope_sequences = 3"))
    assert "SCOPE_NOT_PUBLISHED" in run("sync-reviewers", "--demo", code=2)  # publish first
    assert {r.scope_sequences for r in store.reviewers()} == {2}
    run("publish-bundle", "--demo", base)
    assert "scope 3" in run("sync-reviewers", "--demo")
    path.write_text(path.read_text().replace("scope_sequences = 3", "scope_sequences = 1"))
    assert "SCOPE_BELOW_A_REVIEWER" in run("publish-bundle", "--demo", base, code=2)
    assert store.published(demo=True)["scope_sequences"] == 3
    run("sync-reviewers", "--demo")  # lowering the reviewers first is always possible
    run("publish-bundle", "--demo", base)
    assert store.published(demo=True)["scope_sequences"] == 1


# ------------------------------------------------------------------ reviewers


def test_sync_reviewers_reads_the_owner_s_file_and_never_deletes(run, store, base):
    file = base / "reviewers.toml"
    before = (file.read_bytes(), file.stat().st_mtime_ns)
    out = run("init-db")  # the web app's sign-in tables live in the same schema file
    assert re.fullmatch(r"schema applied: \d+ table\(s\): [a-z_, ]+\n", out)
    assert {"bundle", "chain_anchor", "event", "media", "reviewer"} <= set(
        out.strip().split(": ")[2].split(", "))
    # A reviewer is bound to a bundle: nothing can be synced before one is published.
    assert run("sync-reviewers", code=2) == "refused: NO_REAL_BUNDLE_PUBLISHED\n"
    assert run("sync-reviewers", "--demo", code=2) == "refused: NO_DEMO_BUNDLE_PUBLISHED\n"
    assert run("sync-reviewers", "--test-identity", "TEST-1", TESTER, code=2) == \
        "refused: NO_DEMO_BUNDLE_PUBLISHED\n"
    run("publish-bundle", "--demo", base)
    assert run("sync-reviewers", code=2) == "refused: NO_REAL_BUNDLE_PUBLISHED\n"
    bundle = f"bundle {A.bundle_digest(base / 'bundle')[:12]} (demonstration data)"
    out = run("sync-reviewers", "--demo")
    assert out.splitlines() == [f"REMOTE-2: id 1, REVIEWER, FULL, scope 40, {bundle}",
                                f"REMOTE-3: id 2, REVIEWER, FULL, scope 40, {bundle}",
                                "2 reviewer row(s) written; none deleted"]
    assert store.reviewer_by_login(ONE.login).session() == ONE
    assert "TEST_ALIAS_IS_TEST_N" in run("sync-reviewers", "--test-identity", "REMOTE-9", TESTER,
                                         code=2)
    out = run("sync-reviewers", "--test-identity", "TEST-1", TESTER)
    assert out.splitlines() == [
        f"TEST-1: id 3, REVIEWER, FULL, scope 40, {bundle}, test identity",
        "1 reviewer row(s) written; none deleted"]
    assert store.reviewer_by_login(TESTER).is_test is True
    # An entry that left the file keeps its row (revoke it); the file itself is only read.
    file.write_text(file.read_text().split("[[reviewer]]\nlogin = \"remote@")[0])
    assert run("sync-reviewers", "--demo").splitlines()[-1] == \
        "1 reviewer row(s) written; none deleted"
    assert [r.alias for r in store.reviewers()] == ["REMOTE-2", "REMOTE-3", "TEST-1"]
    file.write_bytes(before[0])
    os.utime(file, ns=(before[1], before[1]))
    for command in (["sync-reviewers", "--demo"], ["publish-bundle", "--demo", base],
                    ["import-log"], ["parity"], ["status"], ["push-returns"]):
        run(*command)
    assert (file.read_bytes(), file.stat().st_mtime_ns) == before


def web_sessions(store, *sessions: tuple[str, int]) -> None:
    """Rows (token letter, reviewer id) in the web app's ``web_session`` table."""
    with store.connection() as conn:
        for letter, reviewer_id in sessions:
            conn.execute("INSERT INTO web_session (token_sha256, reviewer_id, created_at,"
                         " last_seen_at, expires_at) VALUES (%s, %s, now(), now(), now())",
                         (letter * 64, reviewer_id))


def test_revoke_stops_access_at_once_and_restore_access_lifts_it(run, store, base):
    run("publish-bundle", "--demo", base)
    run("sync-reviewers", "--demo")
    web_sessions(store, ("a", 1), ("b", 1), ("c", 2))
    with store.connection() as conn:  # sign-in codes: one used, the others still open
        for letter, reviewer_id, used in (("d", 1, None), ("e", 1, "now"), ("f", 2, None)):
            conn.execute("INSERT INTO login_code (reviewer_id, code_hmac, created_at,"
                         " expires_at, used_at) VALUES (%s, %s, now(), now(), %s::timestamptz)",
                         (reviewer_id, letter * 64, used))
    assert run("revoke", "REMOTE-2") == "REMOTE-2: REVOKED; 2 web session(s) deleted\n"
    assert store.reviewer_by_login(ONE.login).revoked is True
    assert re.search(r"REMOTE-2: id 1, REVIEWER, FULL, scope 40, .*, REVOKED\n",
                     run("sync-reviewers", "--demo"))
    with store.connection() as conn:
        assert conn.execute("SELECT token_sha256 FROM web_session").fetchall() == [("c" * 64,)]
        assert conn.execute("SELECT left(code_hmac, 1) FROM login_code ORDER BY 1"
                            ).fetchall() == [("e",), ("f",)]  # the open code went too
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
    run("publish-bundle", "--demo", base)
    run("sync-reviewers", "--demo")
    log = A.annotations_path(base)
    data, lines = log.read_bytes(), log_lines(base)
    out = run("import-log")
    assert f"local log: 8 line(s), head {V._sha(lines[-1])[:12]}, {len(data)} bytes, sha256 " \
           f"{V._sha(data)}" in out
    assert "imported 7 new line(s); 0 already imported" in out
    assert out.splitlines()[:2] == [
        "REMOTE-2: 1 spelling(s) of its sign-in among its local lines",
        "REMOTE-3: 1 spelling(s) of its sign-in among its local lines"]
    assert out.split("REMOTE-2: lines ")[1].splitlines()[:4] == [
        "6 (6 new); forms 45: DONE 1, INDEPENDENT_SAVED 1, INDEPENDENT_SAVED+SHOWN 1, OPEN 42",
        "  i07 DONE", "  i08 INDEPENDENT_SAVED", "  i01 INDEPENDENT_SAVED+SHOWN"]
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
    # The approver's return is not on the site yet: the reviewer's next step differs there.
    assert "  next step: local i07 RETURNED, hosted i08 AFTER_REVEAL_DUE: DIFFERENT" in run(
        "parity", code=1)
    run("push-returns")
    assert "  next step: local i07 RETURNED, hosted i07 RETURNED: the same" in run("parity")
    assert "PASS" in run("verify")


def test_parity_compares_records_states_and_the_site_s_next_step(run, store, base,
                                                                monkeypatch):
    rows = cutover(run, store, base)
    bundle = store.active_bundle().bundle_id[:12]
    assert run("parity").splitlines() == [
        f"local bundle {bundle}",
        (f"REMOTE-2: bundle {bundle}; records local 6, hosted 6: the same; forms 45: 45 equal, "
         "0 different; hosted lines not pulled: 0"),
        "  next step: local i08 AFTER_REVEAL_DUE, hosted i08 AFTER_REVEAL_DUE: the same",
        (f"REMOTE-3: bundle {bundle}; records local 1, hosted 1: the same; forms 45: 45 equal, "
         "0 different; hosted lines not pulled: 0"),
        "  next step: local i07 AFTER_REVEAL_DUE, hosted i07 AFTER_REVEAL_DUE: the same",
        "parity of the local log and the hosted store: PASS, 2 reviewer(s)"]  # no test identity
    # The next step is the web app's own function: without it there is no parity.
    with monkeypatch.context() as without:
        without.setitem(sys.modules, "reviewer_web.forms", None)
        out = run("parity", code=1)
    assert out.startswith("NEXT_STEP_FUNCTION_MISSING") and "next step" not in out
    assert out.splitlines()[-1].endswith("FAIL, 2 reviewer(s)")
    # The site is ahead of the local log until the next pull.
    hosted_reveal(store, rows["REMOTE-2"], "i08")
    hosted_save(store, rows["REMOTE-2"], "i08", A.AFTER_REVEAL)
    out = run("parity", code=1)
    assert (f"REMOTE-2: bundle {bundle}; records local 6, hosted 8: DIFFERENT; forms 45: 44 "
            "equal, 1 different; hosted lines not pulled: 2") in out
    assert "  i08: local INDEPENDENT_SAVED, hosted DONE" in out
    assert "  next step: local i08 AFTER_REVEAL_DUE, hosted i01 AFTER_REVEAL_DUE: DIFFERENT" in out
    assert out.splitlines()[-1] == \
        "parity of the local log and the hosted store: FAIL, 2 reviewer(s)"
    run("pull-to-local")
    assert "  next step: local i01 AFTER_REVEAL_DUE, hosted i01 AFTER_REVEAL_DUE: the same" in run(
        "parity")


def test_one_extra_local_line_makes_parity_fail(run, store, base):
    cutover(run, store, base)
    assert "PASS" in run("parity")
    A.mark_system_shown(base, A.load_bundle(base / "bundle", A.FULL), TWO, "i08")
    out = run("parity", code=1)  # one marker the site does not hold: no form changes its step
    assert "REMOTE-3: bundle " in out and "records local 2, hosted 1: DIFFERENT; forms 45: 44 " \
        "equal, 1 different" in out
    assert "  i08: local OPEN+SHOWN, hosted OPEN" in out
    assert "  next step: local i07 AFTER_REVEAL_DUE, hosted i07 AFTER_REVEAL_DUE: the same" in out
    assert "REMOTE-2: bundle " in out and "records local 6, hosted 6: the same" in out
    local_save(base, ONE, "i08", A.AFTER_REVEAL)  # and one reading: the next step moves too
    out = run("parity", code=1)
    assert "  next step: local i01 AFTER_REVEAL_DUE, hosted i08 AFTER_REVEAL_DUE: DIFFERENT" in out
    assert "imported 2 new line(s)" in run("import-log")  # no hosted save yet: both come over
    assert "PASS" in run("parity")


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
    assert run("import-log", code=2).endswith("refused: LOCAL_SAVE_AFTER_HOSTED_SAVES\n")


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
    assert "events: LEGACY 7, HOSTED 6, TEST 1; hosted chain on " in out and ": OK" in out
    assert "7 LEGACY line(s) compared with it" in out and "verify: PASS" in out
    # A hosted line that does not continue the chain (stored by other means than the site).
    last = json.loads(store.events()[-3]["line"])  # the first reviewer's latest hosted line
    stray = V.encode_line({**last, "seq": HOSTED_1 + 9})
    store.insert_events([cli.S.EventLine(rows["REMOTE-2"].id, "HOSTED", stray)])
    assert f"  HOSTED_CHAIN_BROKEN at {HOSTED_1 + 9}" in run("verify", code=1)
    # The test identities' stream is a chain of its own, checked apart.
    mine = json.loads(store.events()[-1]["line"])
    store.insert_events([cli.S.EventLine(rows["TEST-1"].id, "TEST", V.encode_line(
        {**mine, "seq": TEST_1 + 4}))])
    out = run("verify", code=1)
    assert f"  TEST_CHAIN_BROKEN at {TEST_1 + 4}" in out and out.count("CHAIN_BROKEN") == 2
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
    web_sessions(store, ("a", rows["REMOTE-2"].id), ("b", rows["TEST-1"].id))
    out_file = tmp_path / "exports" / "events.jsonl"
    out = run("export", "--out", out_file)
    data = out_file.read_bytes()
    assert b"a" * 64 not in data and b"token" not in data  # events and the anchor, nothing else
    assert f"exported 14 event(s): LEGACY 7, HOSTED 6, TEST 1; {len(data)} bytes, sha256 " \
           f"{V._sha(data)}" in out
    assert stat.S_IMODE(out_file.stat().st_mode) == 0o600
    assert run("export", "--out", out_file, code=2) == "refused: FileExistsError\n"
    header, *exported = map(json.loads, data.decode().splitlines())
    assert header["events"] == 14 and header["anchor"]["count"] == 8
    assert [(e["alias"], e["origin"], e["seq"]) for e in exported][7:] == [
        ("REMOTE-2", "HOSTED", HOSTED_1), ("REMOTE-2", "HOSTED", HOSTED_1 + 1),
        ("REMOTE-2", "HOSTED", HOSTED_1 + 2), ("REMOTE-2", "HOSTED", HOSTED_1 + 3),
        ("REMOTE-2", "HOSTED", HOSTED_1 + 4), ("REMOTE-3", "HOSTED", HOSTED_1 + 5),
        ("TEST-1", "TEST", TEST_1)]
    assert [e["is_test"] for e in exported] == [False] * 13 + [True]  # a test row is marked
    assert [e["line"].encode() for e in exported[:7]] == [
        log_lines(base)[n - 1] for n in (1, 2, 3, 4, 5, 7, 8)]
    assert run("restore", "--in", out_file, code=2) == "refused: DATABASE_NOT_EMPTY\n"
    # An EMPTY database: the schema, then the reviewers (an export names aliases only).
    events, anchor = store.events(), store.anchor()
    empty = hosted_pg.fresh_database()
    monkeypatch.setenv(cli.URL_ENV, empty)
    run("init-db")
    assert run("restore", "--in", out_file, code=2) == "refused: NO_ROW_FOR_A_LINE_S_SIGN_IN\n"
    run("publish-bundle", "--demo", base)
    run("sync-reviewers", "--demo")
    restored = cli.S.AdminStore(empty)
    restored.upsert_reviewer("TEST-1", TESTER)  # the alias of a test identity, as a reviewer
    assert run("restore", "--in", out_file, code=2) == "refused: ROW_IS_NOT_THE_SAME_KIND\n"
    with restored.connection() as conn:
        conn.execute("DELETE FROM reviewer WHERE alias = 'TEST-1'")
    run("sync-reviewers", "--test-identity", "TEST-1", TESTER)
    out = run("restore", "--in", out_file)
    assert "restored 14 event(s): LEGACY 7, HOSTED 6, TEST 1; every hash and the hosted chain " \
           f"verified; file sha256 {V._sha(data)}" in out
    assert restored.events() == events and restored.anchor() == anchor
    assert "verify: PASS" in run("verify")
    again = tmp_path / "exports" / "again.jsonl"
    run("export", "--out", again)
    assert again.read_bytes().splitlines()[1:] == data.splitlines()[1:]
    # Sign-in codes and web sessions are never exported or restored: everyone signs in again.
    with restored.connection() as conn:
        assert conn.execute("SELECT (SELECT count(*) FROM web_session),"
                            " (SELECT count(*) FROM login_code)").fetchone() == (0, 0)
    # The restored site goes on where the other stopped: the next line continues the chain.
    [nxt] = hosted_save(restored, restored.reviewer_by_login(TWO.login), "i08",
                        A.AFTER_REVEAL).records
    assert (nxt["seq"], nxt["prev_sha256"], nxt["refers_to"]) == (
        HOSTED_1 + 6, events[-2]["sha256"], HOSTED_1 + 5)  # the reviewers' chain, not the test's
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
    run("publish-bundle", "--demo", base)
    run("sync-reviewers", "--demo")
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
    assert run("status").splitlines() == [
        "real bundle: none published", "demonstration bundle: none published",
        "anchor: none (no log imported)", "test identities: 0 (see test-report ALIAS)"]
    rows = cutover(run, store, base)
    hosted_work(store, rows)
    with store.connection() as conn:  # what the web app records at the first sign-in
        conn.execute("UPDATE reviewer SET invited_at = '2026-03-08T09:00:00Z',"
                     " first_sign_in_at = '2026-03-09T07:15:00Z' WHERE alias = 'REMOTE-2'")
    run("revoke", "REMOTE-3")
    out = run("status").splitlines()
    bundle = store.active_bundle().bundle_id[:12]
    assert out[0] == "real bundle: none published"
    assert re.match(rf"demonstration bundle {bundle}: published \d{{4}}-\d\d-\d\dT\S+, scope 40 "
                    r"of 33 sequence\(s\), \d+ bytes, 1 image\(s\) \d+ bytes$", out[1])
    assert out[2].startswith("anchor: 8 line(s), head ")
    first = json.loads(store.events()[7]["line"])["at"][:19]
    assert out[3] == (f"REMOTE-2: bundle {bundle}, scope 40, invited 2026-03-08T09:00:00+00:00, "
                      "first sign-in 2026-03-09T07:15:00+00:00, first hosted save "
                      f"{first}+00:00, revoked -")
    assert out[4:6] == ["  LEGACY: INDEPENDENT 3, SYSTEM_SHOWN 2, AFTER_REVEAL 1",
                        "  HOSTED: SYSTEM_SHOWN 2, AFTER_REVEAL 2, INDEPENDENT 1"]
    assert re.fullmatch(rf"REMOTE-3: bundle {bundle}, scope 40, invited -, first sign-in -, "
                        r"first hosted save \d{4}-\S+, revoked \d{4}-\S+", out[6])
    assert out[7:] == ["  LEGACY: INDEPENDENT 1", "  HOSTED: INDEPENDENT 1",
                       "test identities: 1 (see test-report ALIAS)"]  # none of its lines here


# ------------------------------------------------------------------ the approver's returns

RETURN_1 = 2_000_001
NOTE = "The stop was raised in the post before this one."
NEUTRAL = {"login": "approver", "name": "Approver", "role": A.APPROVER, "access": A.FULL}


def standing_return(records: list[dict], bundle: A.Bundle, login: str, item_id: str):
    """The approver's RETURN of this reviewer's latest reading of the item, when it stands: what
    the local workspace computes from the log for its "returned" notice (``_reviewer``), here
    from any list of records."""
    new = A.rubric2(records)
    own = A._of(new, item_id, bundle.bundle_id, A.AFTER_REVEAL, login)
    appr = A.effective_approval(new, item_id, bundle) if own else None
    return appr if appr is not None and A._outcome(appr) == "RETURN" and appr[
        "refers_to"] == own[-1]["seq"] else None


def hosted_return(store, row, item_id: str):
    return standing_return(store.records_of(row), store.bundle_for(row), row.login_shown,
                           item_id)


def decide(base: Path, item_id: str, refers_to: int, outcome: str, notes: str = "") -> dict:
    return A.append_annotation(base, A.load_bundle(base / "bundle", A.FULL), OWNER,
                               item_id=item_id, phase=A.APPROVAL, blind=False, reading={},
                               refers_to=refers_to, outcome=outcome, notes=notes)


def test_a_local_return_reaches_its_reviewer_and_a_corrected_save_supersedes_it(
        run, store, base):
    rows = cutover(run, store, base)
    one, two = rows["REMOTE-2"], rows["REMOTE-3"]
    bundle = store.active_bundle()
    # The reviewer works on the site, the saves come home, the approver returns two readings
    # with a note: one made on the site, one imported from the local log.
    hosted_save(store, one, "i03", A.INDEPENDENT)  # 1000001
    hosted_reveal(store, one, "i03")  # 1000002
    hosted_save(store, one, "i03", A.AFTER_REVEAL)  # 1000003; local line 11 after the pull
    run("pull-to-local")
    before = {alias: store.records_of(row) for alias, row in rows.items()}
    assert "pushed 0 new return(s): -; 0 already stored; 0 skipped" in run("push-returns")
    local = decide(base, "i03", 11, "RETURN", NOTE)  # line 12
    decide(base, "i07", 3, "RETURN", "Look at the entry again.")  # line 13, of the local line 3
    assert "  next step: local i07 RETURNED, hosted i08 AFTER_REVEAL_DUE: DIFFERENT" in run(
        "parity", code=1)  # the approver is waiting, and the site does not say so yet
    out = run("push-returns")
    assert NOTE not in out and out.splitlines() == [
        ("returns that stand in the local log: 2; 0 of a reading that was not made by a hosted "
         "reviewer of this bundle"),
        ("pushed 2 new return(s): REMOTE-2 2; 0 already stored; 0 skipped (the returned reading "
         "is not in the hosted store)"),
        f"  REMOTE-2 i03: return {RETURN_1 + 11} of the reading {HOSTED_1 + 2}",
        f"  REMOTE-2 i07: return {RETURN_1 + 12} of the reading 3"]
    # The site computes "returned" with the workspace's own functions, as the local log does.
    got = hosted_return(store, one, "i03")
    assert got == {**local, "seq": RETURN_1 + 11, "refers_to": HOSTED_1 + 2, "annotator": NEUTRAL,
                   "prev_sha256": got["prev_sha256"], "local": got["local"]}
    assert (got["decision"], got["notes"], got["at"]) == ("RETURN", NOTE, local["at"])
    log = A.read_annotations(base).records
    at_home = standing_return(log, bundle, ONE.login, "i03")
    assert (at_home["seq"], at_home["refers_to"], at_home["notes"]) == (12, 11, NOTE)
    assert hosted_return(store, one, "i07")["refers_to"] == 3
    # Only that reviewer gets it, nothing of anyone's own records changed, and no line of the
    # store names the approver.
    mine = store.records_of(one)
    assert [r for r in mine if r["phase"] != A.APPROVAL] == before["REMOTE-2"]
    assert [r["seq"] for r in mine][-2:] == [RETURN_1 + 11, RETURN_1 + 12]
    assert store.records_of(two) == before["REMOTE-3"]
    assert store.records_of(rows["TEST-1"]) == before["TEST-1"] == []
    returns = [e for e in store.events() if e["origin"] == "RETURN"]
    assert [(e["seq"], e["alias"], e["phase"]) for e in returns] == [
        (RETURN_1 + 11, "REMOTE-2", A.APPROVAL), (RETURN_1 + 12, "REMOTE-2", A.APPROVAL)]
    assert not any(word.encode() in e["line"] for e in returns
                   for word in (OWNER.login, OWNER.name, "owner"))
    assert got["prev_sha256"] == V._sha(V.encode_line(mine[-3]))  # the reading it returns
    assert got["local"] == {"seq": 12, "sha256": V._sha(log_lines(base)[11])}
    assert A.progress(bundle, mine, one.session()).status["i03"] == "DONE"
    # Again: nothing new. The store verifies, parity ignores the returns, status counts them.
    events = store.events()
    assert "pushed 0 new return(s): -; 2 already stored; 0 skipped" in run("push-returns")
    assert store.events() == events
    assert "events: LEGACY 7, HOSTED 3, RETURN 2" in run("verify")
    assert "  next step: local i07 RETURNED, hosted i07 RETURNED: the same" in run("parity")
    assert "  RETURN: APPROVAL 2" in run("status")
    # The reviewer saves a corrected reading on the site: it chains on normally, and the return
    # no longer stands (the other one still does).
    [fixed] = hosted_save(store, one, "i03", A.AFTER_REVEAL, confidence="MEDIUM").records
    assert (fixed["seq"], fixed["prev_sha256"], fixed["refers_to"]) == (
        HOSTED_1 + 3, events[9]["sha256"], HOSTED_1)
    assert hosted_return(store, one, "i03") is None
    assert hosted_return(store, one, "i07") is not None
    # Home again: only the corrected reading is pulled, never a return; the approver decides.
    assert "pulled 1 hosted line(s) into the local log: REMOTE-2 1; 3 already held" in run(
        "pull-to-local")
    log = A.read_annotations(base)
    assert log.ok and log.count == 14 and "approver" not in A.annotations_path(base).read_text()
    assert FREEZE.main(["verify", "--dir", str(base)]) == 0
    b = A.load_bundle(base / "bundle", A.FULL)
    assert standing_return(log.records, b, ONE.login, "i03") is None
    assert A.progress(b, log.records, OWNER).status["i03"] == "AWAITING"
    decide(base, "i03", 14, "ACCEPT")
    assert A.progress(b, A.read_annotations(base).records, OWNER).status["i03"] == "DECIDED"
    out = run("push-returns")  # an ACCEPT is never pushed
    assert "returns that stand in the local log: 1;" in out
    assert "pushed 0 new return(s): -; 1 already stored; 0 skipped" in out
    assert "PASS" in run("verify") and "PASS" in run("parity")


def test_a_return_of_a_reading_the_store_does_not_hold_is_counted_and_skipped(run, store, base):
    rows = cutover(run, store, base)
    # The legacy site still serves the second reviewer, who has no save on the hosted site.
    local_save(base, TWO, "i07", A.AFTER_REVEAL)  # line 9: not imported yet
    decide(base, "i07", 9, "RETURN", NOTE)  # line 10
    out = run("push-returns")
    assert "returns that stand in the local log: 1;" in out
    assert "pushed 0 new return(s): -; 0 already stored; 1 skipped" in out
    assert [e["origin"] for e in store.events()] == ["LEGACY"] * 7
    assert "imported 1 new line(s)" in run("import-log")
    out = run("push-returns")
    assert f"  REMOTE-3 i07: return {RETURN_1 + 9} of the reading 9" in out
    assert hosted_return(store, rows["REMOTE-3"], "i07")["notes"] == NOTE
    assert hosted_return(store, rows["REMOTE-2"], "i07") is None  # the other reviewer's reading
    assert [r["phase"] for r in store.records_of(rows["REMOTE-2"])].count(A.APPROVAL) == 0


def test_a_pushed_return_that_the_approver_took_back_is_reported(run, store, base):
    rows = cutover(run, store, base)
    one = rows["REMOTE-2"]
    decide(base, "i07", 3, "RETURN", NOTE)  # line 9
    assert "pushed 1 new return(s): REMOTE-2 1" in run("push-returns")
    decide(base, "i07", 3, "ACCEPT")  # line 10: decided again, without a new reading
    assert standing_return(A.read_annotations(base).records, store.bundle_for(one), ONE.login,
                           "i07") is None
    out = run("push-returns")  # no line of the store can be taken back: the operator is told
    assert "returns that stand in the local log: 0;" in out
    assert out.splitlines()[-1] == (f"  NO_LONGER_STANDS_LOCALLY REMOTE-2 i07: return "
                                    f"{RETURN_1 + 8} still shows on the site until that reviewer "
                                    "saves again")
    assert hosted_return(store, one, "i07") is not None
    hosted_reveal(store, one, "i07")
    hosted_save(store, one, "i07", A.AFTER_REVEAL)  # the reviewer's next save clears it
    assert hosted_return(store, one, "i07") is None
    assert "NO_LONGER_STANDS_LOCALLY" not in run("push-returns")


def test_returns_survive_export_and_restore_and_verify_checks_what_they_name(
        cli, run, store, base, monkeypatch, tmp_path):
    rows = cutover(run, store, base)
    one = rows["REMOTE-2"]
    decide(base, "i07", 3, "RETURN", NOTE)
    run("push-returns")
    run("export", "--out", tmp_path / "x" / "events.jsonl")
    events = store.events()
    monkeypatch.setenv(cli.URL_ENV, hosted_pg.fresh_database())
    run("init-db")
    run("publish-bundle", "--demo", base)
    run("sync-reviewers", "--demo")
    assert "restored 8 event(s): LEGACY 7, RETURN 1" in run(
        "restore", "--in", tmp_path / "x" / "events.jsonl")
    restored = cli.S.AdminStore(os.environ[cli.URL_ENV])
    assert restored.events() == events
    assert hosted_return(restored, restored.reviewer_by_login(ONE.login), "i07")["notes"] == NOTE
    assert "verify: PASS" in run("verify")
    # A return that names a reading of another reviewer, or no reading after the reveal.
    record = json.loads(events[-1]["line"])
    for n, (refers_to, prev) in enumerate(((5, events[4]["sha256"]), (1, events[0]["sha256"]),
                                           (3, "0" * 64)), 1):
        stray = V.encode_line({**record, "seq": record["seq"] + n, "refers_to": refers_to,
                               "prev_sha256": prev,
                               "local": {**record["local"], "seq": record["local"]["seq"] + n}})
        restored.insert_events([cli.S.EventLine(one.id, "RETURN", stray)])
    out = run("verify", code=1)
    assert [x for x in out.splitlines() if x.startswith("  ")] == [
        f"  RETURN_DOES_NOT_NAME_ITS_READING at {record['seq'] + n}" for n in (1, 2, 3)]
    # The site never writes a return, and a return is never in the hosted chain.
    assert restored.records_of(one)[-1]["seq"] == record["seq"] + 3
    [nxt] = hosted_reveal(restored, restored.reviewer_by_login(ONE.login), "i08").records
    assert (nxt["seq"], nxt["prev_sha256"]) == (HOSTED_1, restored.anchor()["head"])
    # Another folder's log is not the log this store continues.
    assert run("push-returns", "--dir", write_demo(tmp_path / "other"), code=2) == \
        "refused: LOCAL_LOG_IS_NOT_THE_ANCHORED_LOG\n"


# ------------------------------------------------------------------ a test identity in production


def test_a_test_identity_works_on_invented_forms_beside_the_reviewers_on_the_real_bundle(
        run, store, base, tmp_path):
    # ``base`` stands in for the real folder (other content than the demo script writes, and
    # not flagged as its bundle); ``demo`` is the invented folder for test identities.
    demo = write_demo(tmp_path / "demo")
    edit(base / "bundle" / "posts.jsonl",
         lambda rows: rows["p09"].update(text="Back on Wednesday, after the holiday."))
    chart = base / "bundle" / "media" / "chart-p01.png"
    chart.write_bytes(chart.read_bytes() + b"another invented chart")
    reseal(base, product_commit="abc1234")
    real_id, demo_id = (A.bundle_digest(d / "bundle") for d in (base, demo))
    half_way(base)
    run("init-db")
    assert "the real bundle (the demonstration bundle stays)" in run("publish-bundle")
    assert "demonstration data (for test identities; the real bundle stays)" in run(
        "publish-bundle", "--demo", demo)
    assert f"REMOTE-2: id 1, REVIEWER, FULL, scope 40, bundle {real_id[:12]}\n" in run(
        "sync-reviewers")
    assert run("sync-reviewers", "--test-identity", "TEST-1", TESTER).startswith(
        f"TEST-1: id 3, REVIEWER, FULL, scope 40, bundle {demo_id[:12]} (demonstration data), "
        "test identity\n")
    rows = {r.alias: r for r in store.reviewers()}
    one, tester = rows["REMOTE-2"], rows["TEST-1"]
    assert real_id != demo_id and [r.bundle_id for r in rows.values()] == [
        real_id, real_id, demo_id]
    # Each is served their own bundle: forms, posts, images.
    assert (store.bundle_for(one).bundle_id, store.is_demo(one)) == (real_id, False)
    assert (store.bundle_for(tester).bundle_id, store.is_demo(tester)) == (demo_id, True)
    assert store.bundle_for(one).posts["p09"]["text"] != store.bundle_for(
        tester).posts["p09"]["text"]
    assert store.media(one, "p01", "chart-p01.png") == (chart.read_bytes(), "image/png")
    assert store.media(tester, "p01", "chart-p01.png") == (
        (demo / "bundle" / "media" / "chart-p01.png").read_bytes(), "image/png")
    # The cutover and the work: the reviewer on the real forms, the test identity on invented ones.
    assert "imported 7 new line(s)" in run("import-log")
    hosted_reveal(store, one, "i08")  # 1000001
    hosted_save(store, one, "i08", A.AFTER_REVEAL)  # 1000002
    hosted_save(store, tester, "i07", A.INDEPENDENT)  # 3000001: the test identity's stream
    hosted_reveal(store, tester, "i07")  # 3000002
    assert [r["bundle_id"] for r in store.records_of(tester)] == [demo_id] * 2
    assert {r["bundle_id"] for r in store.records_of(one)} == {real_id}
    assert len(store.records_of(one)) == 8  # the imported six and the two hosted, no one else's
    # The report of the test identity: sign-in, sessions, events, form states. Never an address.
    web_sessions(store, ("a", tester.id), ("b", one.id))
    with store.connection() as conn:
        conn.execute("UPDATE reviewer SET first_sign_in_at = '2026-03-09T07:15:00Z'"
                     " WHERE alias = 'TEST-1'")
    out = run("test-report", "TEST-1").splitlines()
    assert out[0] == (f"TEST-1 (test identity): bundle {demo_id[:12]} (demonstration data), "
                      "scope 40, first sign-in 2026-03-09T07:15:00+00:00, web sessions 1, "
                      "revoked -")
    assert out[1] == "events: 2"
    assert re.fullmatch(rf"  {TEST_1} \d{{4}}-\S+ INDEPENDENT i07", out[2])
    assert re.fullmatch(rf"  {TEST_1 + 1} \d{{4}}-\S+ SYSTEM_SHOWN i07", out[3])
    assert out[4:] == ["TEST-1: records 2; forms 45: INDEPENDENT_SAVED+SHOWN 1, OPEN 44",
                       "  i07 INDEPENDENT_SAVED+SHOWN"]
    assert run("test-report", "REMOTE-2", code=2) == "refused: NOT_A_TEST_IDENTITY\n"
    assert run("test-report", "TEST-9", code=2) == "refused: UNKNOWN_ALIAS\n"
    # The reviewers' commands leave the test identity alone: its events are never pulled.
    out = run("parity", code=1)
    assert "TEST-1" not in out and "hosted lines not pulled: 2" in out
    assert "pulled 2 hosted line(s) into the local log: REMOTE-2 2; 0 already held" in run(
        "pull-to-local")
    text = A.annotations_path(base).read_text()
    assert TESTER not in text and demo_id not in text
    out = run("parity")
    assert "TEST-1" not in out and out.splitlines()[-1].endswith("PASS, 2 reviewer(s)")
    out = run("status")
    assert out.startswith(f"real bundle {real_id[:12]}: published ")
    assert f"\ndemonstration bundle {demo_id[:12]}: published " in out
    assert out.count("TEST-1") == 0 and out.splitlines()[-1] == \
        "test identities: 1 (see test-report ALIAS)"
    assert "PASS" in run("verify") and "imported 0 new line(s)" in run("import-log")
    # Nothing moves a test identity onto real content, and publishing again keeps both bundles.
    with pytest.raises(A.AdjudicationError, match="TEST_IDENTITY_NEEDS_A_DEMO_BUNDLE"):
        store.upsert_reviewer("TEST-1", TESTER, is_test=True, bundle_id=real_id)
    run("publish-bundle", "--demo", demo)
    run("publish-bundle")
    assert (store.published(demo=False)["bundle_id"], store.published(demo=True)["bundle_id"]) \
        == (real_id, demo_id)
    assert store.bundle_for(tester).bundle_id == demo_id


# ------------------------------------------------------------------ invitations


def test_invite_queues_a_message_for_the_site_to_send_and_status_says_how_it_went(
        cli, run, store, base, tmp_path, capsys):
    """The operator's machine holds no mail password: ``invite`` only queues, the site sends
    (tests/test_hosted_web_invites.py), and ``status`` shows what became of each message."""
    run("publish-bundle", "--demo", base)
    run("sync-reviewers", "--demo")
    run("sync-reviewers", "--test-identity", "TEST-1", TESTER)
    assert run("invite", "REMOTE-2", "--kind", "continue") == (
        "REMOTE-2: INVITE_CONTINUE queued; the site sends it when its sender is called\n")
    assert run("invite", "REMOTE-2", "--kind", "continue", code=2) == "refused: ALREADY_QUEUED\n"
    run("invite", "REMOTE-2", "--kind", "start")  # the other kind is a message of its own
    assert run("invite", "TEST-1", "--kind", "start", code=2) == \
        "refused: TEST_IDENTITY_IS_NOT_INVITED\n"
    assert run("invite", "REMOTE-9", "--kind", "start", code=2) == "refused: UNKNOWN_ALIAS\n"
    run("revoke", "REMOTE-3")
    assert run("invite", "REMOTE-3", "--kind", "start", code=2) == "refused: REVOKED\n"
    with pytest.raises(SystemExit):  # the two kinds are the command line's only choices
        cli.main(["invite", "REMOTE-2", "--kind", "newsletter"])
    capsys.readouterr()
    waiting = run("status")
    assert re.search(r"REMOTE-2: [^\n]*invited -,[^\n]*\n"
                     r"  mail INVITE_CONTINUE: queued \d{4}-\S+, sent -, result -\n"
                     r"  mail INVITE_START: queued \d{4}-\S+, sent -, result -\n", waiting)
    assert "mail" not in waiting.split("REMOTE-3:")[1]
    # What the site's sender records: one message went out, the other could not be sent.
    for result in (cli.S.MAIL_SENT, "MAIL_UNREACHABLE"):
        with store.connection():
            row, _kind, _to = store.mail_next(())
            store.mail_done(row, result)
            if result != cli.S.MAIL_SENT:  # tried three times: given up
                store.mail_done(row, result)
                store.mail_done(row, result)
    after = run("status")
    assert re.search(r"REMOTE-2: [^\n]*invited \d{4}-\S+,[^\n]*\n"
                     r"  mail INVITE_CONTINUE: queued \d{4}-\S+, sent \d{4}-\S+, result SENT\n"
                     r"  mail INVITE_START: queued \d{4}-\S+, sent -, result MAIL_UNREACHABLE\n",
                     after)
    with store.connection():
        assert store.mail_next(()) is None and store.mail_left() == 0
    # A message that was given up may be queued again; one that waits may not.
    run("invite", "REMOTE-2", "--kind", "start")
    assert run("invite", "REMOTE-2", "--kind", "start", code=2) == "refused: ALREADY_QUEUED\n"
    # The queue is no part of an export.
    out = tmp_path / "export.jsonl"
    run("export", "--out", out)
    assert "INVITE" not in out.read_text() and "mail" not in out.read_text()


# ------------------------------------------------------------------ one spelling of a sign-in
# The core finds a reviewer's records by ``annotator.login`` EXACTLY equal to the session's login.
# (The independent review's finding 1; its probes are named in each test.)

RESPELLED = A.Session("Reviewer@Example.invalid", ONE.name, A.REVIEWER, A.FULL, True, 40)


def test_a_sign_in_with_lines_keeps_its_spelling(run, store, base):
    """Probe ``test_respelled_login_hides_history``: a respelled row hid the reviewer's history
    and let them save a second "independent" reading of a form they had seen revealed."""
    from reviewer_web import forms

    rows = cutover(run, store, base)
    file = base / "reviewers.toml"
    file.write_text(file.read_text().replace('"reviewer@example', '"Reviewer@Example'))
    out = run("sync-reviewers", "--demo", code=2)
    assert out.splitlines() == [
        ("REMOTE-2: the file spells this sign-in otherwise than this row, which has lines under "
         "its spelling; put the earlier spelling back in the file"),
        "refused: LOGIN_SPELLING_HAS_RECORDS"]
    with pytest.raises(A.AdjudicationError, match="^LOGIN_SPELLING_HAS_RECORDS$"):
        store.upsert_reviewer("REMOTE-2", RESPELLED.login, name=ONE.name, scope_sequences=40)
    one = store.reviewer_by_login(ONE.login)
    assert one == rows["REMOTE-2"] and one.session() == ONE  # nothing was written
    # The history is still found, by the site's own functions and by the core.
    assert forms.step_state(store.bundle_for(one), one.session(), store.records_of(one),
                            "i07") == forms.DONE
    with pytest.raises(A.AdjudicationError, match="SYSTEM_READING_ALREADY_SEEN"):
        hosted_save(store, one, "i07", A.INDEPENDENT)
    # The import refuses too while the file, the row and the log do not spell it alike.
    out = run("import-log", code=2)
    assert ("REMOTE-2: 1 spelling(s) of its sign-in among its local lines: NOT the one spelling "
            "of its row and of the reviewers file") in out
    assert out.endswith("refused: LOGIN_SPELLED_OTHERWISE\n")
    # A row whose spelling was changed by other means than the tool: verify says which lines.
    assert "PASS" in run("verify")
    with store.connection() as conn:
        conn.execute("UPDATE reviewer SET login_shown = %s WHERE alias = 'REMOTE-2'",
                     (RESPELLED.login,))
    out = run("verify", code=1)
    assert [x for x in out.splitlines() if x.startswith("  ")] == [
        f"  LINE_NOT_SPELLED_AS_ITS_ROW at {n}" for n in (1, 2, 3, 4, 7, 8)]


def test_local_lines_spelled_otherwise_are_refused_and_parity_fails(cli, run, store, base):
    """Probe ``test_legacy_lines_spelled_otherwise_are_invisible_and_parity_passes``: lines the
    legacy site wrote while the owner's file spelled the address otherwise were imported, never
    served, and ``parity`` and ``verify`` passed."""
    b = A.load_bundle(base / "bundle", A.FULL)
    A.append_annotation(base, b, RESPELLED, **reading_kw(b, "i07", [], RESPELLED,
                                                         phase=A.INDEPENDENT))
    A.mark_system_shown(base, b, RESPELLED, "i07")
    A.append_annotation(base, b, RESPELLED, **reading_kw(
        b, "i07", A.read_annotations(base).records, RESPELLED, phase=A.AFTER_REVEAL,
        refers_to=1))
    local_save(base, TWO, "i07", A.INDEPENDENT)
    run("init-db")
    run("publish-bundle", "--demo", base)
    run("sync-reviewers", "--demo")
    out = run("import-log", code=2)
    assert out.splitlines() == [
        ("REMOTE-2: 1 spelling(s) of its sign-in among its local lines: NOT the one spelling of "
         "its row and of the reviewers file"),
        "REMOTE-3: 1 spelling(s) of its sign-in among its local lines",
        "refused: LOGIN_SPELLED_OTHERWISE"]
    assert store.events() == [] and store.anchor() is None  # nothing imported, no anchor
    assert not (base / "hosted_snapshots").exists()
    local_save(base, ONE, "i08", A.INDEPENDENT)  # the file's spelling, beside the other
    assert "REMOTE-2: 2 spelling(s) of its sign-in" in run("import-log", code=2)
    out = run("parity", code=1)
    assert ("local lines of remote reviewers that NO row holds under that spelling: 3, of 1 "
            "sign-in spelling(s)") in out and out.splitlines()[-1].endswith("FAIL, 2 reviewer(s)")
    # Whoever asks: no line is stored under a row spelled otherwise.
    row = store.reviewer_by_login(ONE.login)
    with pytest.raises(A.AdjudicationError, match="^LINE_NOT_SPELLED_AS_ITS_ROW$"):
        store.insert_events([cli.S.EventLine(row.id, "LEGACY", log_lines(base)[0])])
    assert store.events() == []


def test_restore_follows_the_sign_in_not_the_alias(cli, run, store, base, monkeypatch,
                                                   tmp_path):
    """Probe ``test_restore_under_swapped_aliases``: restored under a reviewers file that lists
    the two remote reviewers in the other order, every line landed under the other person."""
    from reviewer_web import forms

    rows = cutover(run, store, base)
    hosted_work(store, rows)
    decide(base, "i07", 3, "RETURN", NOTE)
    run("push-returns")
    served = {row.login: store.records_of(row) for row in rows.values()}
    run("export", "--out", tmp_path / "x" / "events.jsonl")
    head, owner, first, second = (base / "reviewers.toml").read_text().split("[[reviewer]]")
    other = write_demo(tmp_path / "other")
    (other / "reviewers.toml").write_text("[[reviewer]]".join(
        [head, owner, second.rstrip("\n") + "\n\n", first]))
    monkeypatch.setenv(cli.URL_ENV, hosted_pg.fresh_database())
    run("init-db")
    run("publish-bundle", "--demo", other)
    run("sync-reviewers", "--demo", "--dir", other)
    run("sync-reviewers", "--test-identity", "TEST-1", TESTER)
    new = cli.S.AdminStore(os.environ[cli.URL_ENV])
    assert [(r.alias, r.login) for r in new.reviewers()] == [
        ("REMOTE-2", TWO.login), ("REMOTE-3", ONE.login), ("TEST-1", TESTER)]  # swapped
    assert "restored 15 event(s): LEGACY 7, HOSTED 6, RETURN 1, TEST 1" in run(
        "restore", "--in", tmp_path / "x" / "events.jsonl")
    for row in new.reviewers():  # each person has exactly their own history again
        assert new.records_of(row) == served[row.login]
    one = new.reviewer_by_login(ONE.login)
    state = forms.states(new.bundle_for(one), one.session(), new.records_of(one))
    assert (one.alias, state["i07"], state["i08"], state["i03"]) == (
        "REMOTE-3", forms.RETURNED, forms.DONE, forms.DONE)
    assert "verify: PASS" in run("verify")
    # A line whose sign-in no row spells that way is not restored under anyone.
    monkeypatch.setenv(cli.URL_ENV, hosted_pg.fresh_database())
    run("init-db")
    run("publish-bundle", "--demo", other)
    run("sync-reviewers", "--test-identity", "TEST-1", TESTER)
    cli.S.AdminStore(os.environ[cli.URL_ENV]).upsert_reviewer("REMOTE-2", RESPELLED.login)
    assert run("restore", "--in", tmp_path / "x" / "events.jsonl", code=2) == \
        "refused: NO_ROW_FOR_A_LINE_S_SIGN_IN\n"


# ------------------------------------------------------------------ the test identities' stream
# (The independent review's finding 2.)


def test_a_test_identity_saves_in_a_stream_of_its_own_and_never_fixes_the_anchor(
        cli, run, store, base, tmp_path):
    """Probes ``test_anchor_is_stuck_once_a_test_identity_saved`` and
    ``test_no_anchor_when_a_hosted_line_comes_first``: a save of the test identity fixed the
    anchor at the rehearsal's head, or (before the first import) left the store without one, and
    ``pull-to-local`` then wrote into any folder's log."""
    half_way(base)
    run("init-db")
    run("publish-bundle", "--demo", base)
    run("sync-reviewers", "--demo")
    run("sync-reviewers", "--test-identity", "TEST-1", TESTER)
    rows = {r.alias: r for r in store.reviewers()}
    # The owner signs in as the test identity and saves BEFORE anything is imported.
    [first] = hosted_save(store, rows["TEST-1"], "i07", A.INDEPENDENT).records
    assert (first["seq"], first["prev_sha256"]) == (TEST_1, cli.S.TEST_GENESIS)
    assert "anchor: 8 line(s), head " in run("import-log")  # the rehearsal
    [second] = hosted_reveal(store, rows["TEST-1"], "i07").records
    local_save(base, TWO, "i07", A.AFTER_REVEAL)  # the legacy site is still serving
    local_save(base, ONE, "i08", A.AFTER_REVEAL)
    out = run("import-log")  # the cutover: the anchor is the log's state NOW
    head = V._sha(log_lines(base)[-1])
    assert f"anchor: 10 line(s), head {head[:12]}: moved to the log's present head" in out
    assert (store.anchor()["count"], store.anchor()["head"]) == (10, head)
    assert "events: LEGACY 9, TEST 2; hosted chain on " in run("verify")
    out = run("parity")
    assert "TEST-1" not in out and out.splitlines()[-1].endswith("PASS, 2 reviewer(s)")
    # A reviewer's first save continues the anchored log, and only that fixes the anchor.
    [mine] = hosted_save(store, rows["REMOTE-2"], "i03", A.INDEPENDENT).records
    assert (mine["seq"], mine["prev_sha256"]) == (HOSTED_1, head)
    [third] = hosted_save(store, rows["TEST-1"], "i08", A.INDEPENDENT).records
    assert (second["seq"], third["seq"], third["prev_sha256"]) == (
        TEST_1 + 1, TEST_1 + 2, V._sha(V.encode_line(second)))  # its own chain, uninterrupted
    decide(base, "i07", 3, "ACCEPT")  # the local log goes on: line 11
    assert (f"anchor: 10 line(s), head {head[:12]}: FIXED, the reviewers' hosted lines continue "
            "it (the local log has 11 line(s) now)") in run("import-log")
    # Another folder's log is not the anchored log, for the pull and for the returns.
    other = write_demo(tmp_path / "other")
    local_save(other, TWO, "i08", A.INDEPENDENT)
    for command in ("pull-to-local", "push-returns"):
        assert run(command, "--dir", other, code=2) == \
            "refused: LOCAL_LOG_IS_NOT_THE_ANCHORED_LOG\n"
    assert len(log_lines(other)) == 1
    # Home comes the reviewer's line, never a test identity's; its stream verifies apart.
    assert "pulled 1 hosted line(s) into the local log: REMOTE-2 1; 0 already held" in run(
        "pull-to-local")
    assert TESTER not in A.annotations_path(base).read_text()
    assert "events: LEGACY 9, HOSTED 1, TEST 3; hosted chain on " in run("verify")
    assert "PASS" in run("parity")
    # Whoever inserts: a test identity's line is a TEST line, and no one else's is.
    for row, origin in ((rows["TEST-1"], "HOSTED"), (rows["REMOTE-2"], "TEST")):
        line = V.encode_line({**(third if row.is_test else mine), "seq": 1_999_000 if origin
                              == "HOSTED" else 3_999_000})
        with pytest.raises(A.AdjudicationError, match="^LINE_NOT_IN_ITS_ROW_S_STREAM$"):
            store.insert_events([cli.S.EventLine(row.id, origin, line)])
        import psycopg
        with pytest.raises(psycopg.errors.RestrictViolation,
                           match="EVENT_NOT_IN_ITS_REVIEWER_S_STREAM"), \
                store.connection() as conn:
            conn.execute(
                "INSERT INTO event (seq, prev_sha256, sha256, line, reviewer_id, bundle_id,"
                " item_id, phase, at, origin, batch) SELECT %s, prev_sha256, %s, %s, %s,"
                " bundle_id, item_id, phase, at, %s, %s FROM event LIMIT 1",
                (json.loads(line)["seq"], V._sha(line), line, row.id, origin,
                 json.loads(line)["seq"]))


def test_a_reviewer_s_hosted_line_before_any_import_is_never_built_upon(run, store, base,
                                                                        tmp_path):
    run("init-db")
    run("publish-bundle", "--demo", base)
    run("sync-reviewers", "--demo")
    one = store.reviewer_by_login(ONE.login)
    [line] = hosted_save(store, one, "i07", A.INDEPENDENT).records  # no log was ever imported
    assert (line["seq"], line["prev_sha256"]) == (HOSTED_1, V.GENESIS) and store.anchor() is None
    # A folder with a log: not the (empty) log this chain continues. Nothing is written.
    other = write_demo(tmp_path / "other")
    local_save(other, TWO, "i08", A.INDEPENDENT)
    for command in ("pull-to-local", "push-returns"):
        assert run(command, "--dir", other, code=2) == "refused: NO_ANCHOR_IMPORT_LOG_FIRST\n"
    assert run("import-log", "--dir", other, code=2) == \
        "refused: HOSTED_LINES_BEFORE_FIRST_IMPORT\n"
    assert len(log_lines(other)) == 1 and len(store.events()) == 1 and store.anchor() is None
    # An empty log IS that log (a preview's saves can be taken home).
    assert "pulled 1 hosted line(s) into the local log: REMOTE-2 1; 0 already held" in run(
        "pull-to-local")
    assert A.read_annotations(base).records[0]["hosted"]["seq"] == HOSTED_1
