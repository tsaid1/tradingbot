"""The operator tool of the hosted reviewer site (``scripts/hosted_admin.py``) on INVENTED data and
a real embedded Postgres: publishing the part of a bundle a scope needs (and the parity check that
proves it shows the same), reviewers and revocation, the verbatim import of the local log, parity,
hosted saves pulled back into a local log the existing code accepts, export and restore, verify,
and that no command prints an address, a name or a word of a post or a reading."""

from __future__ import annotations

import dataclasses
import hashlib
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
        rows["i01"]["ai"]["rationale"] = "a key the workspace never reads"
        rows["i01"]["ai"]["entry"]["quote"] = "a key the workspace never reads"
        rows["i01"]["mapping"]["memo"] = "a key the workspace never reads"
        rows["i01"]["holds"]["SHIPPED"]["trace"] = "a key the workspace never reads"
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
            "form numbering, 37 left out; posts 8 with text and 1 named only, of 41; images 1,"
            ) in out
    assert "parity with the full bundle: PASS" in out and "published and active" in out
    assert ("images: all 1 that a page shows are in the publication with the sha256 of the "
            "local file; no other image is published") in out
    part = store.active_bundle()
    assert part.bundle_id == full.bundle_id and store.bundle_is_demo() is True
    assert {k: store.published(demo=True)[k]
            for k in ("source_sequences", "scope_sequences", "images")} == {
        "source_sequences": 33, "scope_sequences": 2, "images": 1}
    assert store.published(demo=False) is None
    # The items the scope offers, the second form of a post they share, and nothing else.
    assert [i["item_id"] for _s, items in A.ordered(part, 2) for i in items] == IN_SCOPE
    assert set(part.items) == {*IN_SCOPE, "x01"}
    with store.connection() as conn:
        files = list(map(bytes, conn.execute(
            "SELECT manifest, sequences, items, posts FROM bundle").fetchone()))
        assert conn.execute("SELECT name FROM media").fetchall() == [("chart-p01.png",)]
    stored = b"".join(files)
    # The other form of the shared post: its place among the forms, nothing else (it comes
    # after the form in scope, so not even its market is read).
    [stub] = [r for r in map(json.loads, files[2].decode().splitlines())
              if r["item_id"] == "x01"]
    assert stub == {"item_id": "x01", "kind": "INSTRUCTION", "post_ref": "p07", "slot": 1,
                    "order_in_sequence": 2}
    # The forms' own posts whole; the post an own post quotes by its id only; nothing of the
    # post THAT one replies to, nor of the stub form's context.
    assert set(part.posts) == {"p01", "p02", "p03", "p04", "p05", "p06", "p07", "p08", "s02"}
    assert part.posts["s02"] == {**part.posts["s02"], "text": "", "media": [], "numbers": [],
                                 "reply_to": None, "published_at": "1970-01-01T00:00:00+00:00"}
    assert b"never reads" not in stored and b"exporter_note" not in stored  # read keys only
    for ref in ("s01", "s02", "s03"):
        assert json.dumps(full.posts[ref]["text"])[1:-1].encode() not in stored
    # Of the manifest and of the ideas: what the loader and the ``idea`` choices need.
    assert sorted(json.loads(files[0])) == ["bundle_id", "families", "schema"]
    assert {tuple(sorted(json.loads(x))) for x in files[1].decode().splitlines()} == {
        ("post_refs", "review_order", "sequence_id")}
    assert b"DESIGN" not in stored and b"CHECK" not in stored  # which set an idea is in
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
    # And by the web app's own page functions, for every form in scope.
    from reviewer_web import forms

    session = dataclasses.replace(ONE, scope=2)
    assert forms.workspace(part, session, []) == forms.workspace(full, session, [])
    assert forms.media_posts(part, session) == forms.media_posts(full, session)
    for item_id in IN_SCOPE:
        mine, theirs = part.items[item_id], full.items[item_id]
        for page in (forms.evidence, forms.system_block, forms.form_header):
            assert page(part, mine) == page(full, theirs), (page.__name__, item_id)
        for system in (None, A.system_reading(full, theirs)):
            built = [forms.build(b, i, [], session, system=system, defaults={},
                                 phase=A.AFTER_REVEAL if system else A.INDEPENDENT)
                     for b, i in ((part, mine), (full, theirs))]
            assert built[0] == built[1], item_id


def test_a_scope_that_covers_every_sequence_publishes_the_items_of_no_sequence_too(
        run, store, base):
    extend(base, 40)
    full = A.load_bundle(base / "bundle", A.FULL)
    out = run("publish-bundle", "--demo", base)
    assert "scope 33 of 33 sequence(s); items 46 shown, 0 kept for form numbering, 0 left out; " \
           "posts 41 with text and 0 named only, of 41; images 2," in out and "PASS" in out
    part = store.active_bundle()
    assert list(part.items) == list(full.items) and "i09" in part.items
    assert [(s, [i["item_id"] for i in g]) for s, g in A.ordered(part, 40)] == [
        (s, [i["item_id"] for i in g]) for s, g in A.ordered(full, 40)]
    assert part.items == {k: {**v, "set": None} for k, v in full.items.items()}  # as loaded


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
    assert problems({}, images={"chart-p01.png": DEMO._png() + b"."}) == {("images", "i01")}
    assert problems({}, images={}) == {("images", "i01")}  # an image a page shows is missing
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
                                "2 reviewer row(s) written; none deleted",
                                "REMOTE-2: closed until import-log runs (no save is accepted "
                                "before)",
                                "REMOTE-3: closed until import-log runs (no save is accepted "
                                "before)"]
    assert store.reviewer_by_login(ONE.login).session() == ONE
    assert "TEST_ALIAS_IS_TEST_N" in run("sync-reviewers", "--test-identity", "REMOTE-9", TESTER,
                                         code=2)
    out = run("sync-reviewers", "--test-identity", "TEST-1", TESTER)
    assert out.splitlines() == [
        f"TEST-1: id 3, REVIEWER, FULL, scope 40, {bundle}, test identity",
        "1 reviewer row(s) written; none deleted",
        "REMOTE-2: closed until import-log runs (no save is accepted before)",
        "REMOTE-3: closed until import-log runs (no save is accepted before)"]
    assert store.reviewer_by_login(TESTER).is_test is True
    # An entry that left the file keeps its row (revoke it); the file itself is only read.
    file.write_text(file.read_text().split("[[reviewer]]\nlogin = \"remote@")[0])
    assert "1 reviewer row(s) written; none deleted" in run("sync-reviewers", "--demo")
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
        "  reveal history: local 2, hosted 2 marker(s): the same",
        "  the site's state of each form: 45 equal, 0 different",
        (f"REMOTE-3: bundle {bundle}; records local 1, hosted 1: the same; forms 45: 45 equal, "
         "0 different; hosted lines not pulled: 0"),
        "  next step: local i07 AFTER_REVEAL_DUE, hosted i07 AFTER_REVEAL_DUE: the same",
        "  reveal history: local 0, hosted 0 marker(s): the same",
        "  the site's state of each form: 45 equal, 0 different",
        "REMOTE-2: completed 1 of 45; next i08 AFTER_REVEAL_DUE",
        "REMOTE-3: completed 0 of 45; next i07 AFTER_REVEAL_DUE",
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



def ret(line: int) -> int:
    """The store's seq of the return made from that local line (its clearing line: one above)."""
    return 2_000_000 + 2 * line


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
        f"  REMOTE-2 i03: return {ret(12)} of the reading {HOSTED_1 + 2}",
        f"  REMOTE-2 i07: return {ret(13)} of the reading 3",
        "cleared 0 pushed return(s) that no longer stand locally: -"]
    # The site computes "returned" with the workspace's own functions, as the local log does.
    got = hosted_return(store, one, "i03")
    assert got == {**local, "seq": ret(12), "refers_to": HOSTED_1 + 2, "annotator": NEUTRAL,
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
    assert [r["seq"] for r in mine][-2:] == [ret(12), ret(13)]
    assert store.records_of(two) == before["REMOTE-3"]
    assert store.records_of(rows["TEST-1"]) == before["TEST-1"] == []
    returns = [e for e in store.events() if e["origin"] == "RETURN"]
    assert [(e["seq"], e["alias"], e["phase"]) for e in returns] == [
        (ret(12), "REMOTE-2", A.APPROVAL), (ret(13), "REMOTE-2", A.APPROVAL)]
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
    assert f"  REMOTE-3 i07: return {ret(10)} of the reading 9" in out
    assert hosted_return(store, rows["REMOTE-3"], "i07")["notes"] == NOTE
    assert hosted_return(store, rows["REMOTE-2"], "i07") is None  # the other reviewer's reading
    assert [r["phase"] for r in store.records_of(rows["REMOTE-2"])].count(A.APPROVAL) == 0


def test_a_return_the_approver_took_back_is_cleared_on_the_site(run, store, base):
    """Probe ``test_stale_return_undoes_a_later_accept`` (finding 6): the approver returned a
    reading, pushed it, then accepted that same reading; the site went on saying "Returned to
    you", the reviewer saved again, and after the next pull the acceptance no longer counted."""
    from reviewer_web import forms

    rows = cutover(run, store, base)
    one, two = rows["REMOTE-2"], rows["REMOTE-3"]

    def site_state(row) -> str:
        return forms.step_state(store.bundle_for(row), row.session(), store.records_of(row),
                                "i07")

    decide(base, "i07", 3, "RETURN", NOTE)  # line 9
    assert "pushed 1 new return(s): REMOTE-2 1" in run("push-returns")
    assert site_state(one) == forms.RETURNED
    accepted = decide(base, "i07", 3, "ACCEPT", "a note of the approver's own")  # line 10
    out = run("push-returns")
    assert out.splitlines()[-2:] == [
        "cleared 1 pushed return(s) that no longer stand locally: REMOTE-2 1",
        f"  REMOTE-2 i07: line {ret(10) + 1} clears the return {ret(9)}"]
    # ONE line without content: nothing of what the approver decided, no note.
    clear = store.records_of(one)[-1]
    assert clear == {
        "schema": A.ANNOTATION_SCHEMA, "seq": ret(10) + 1, "prev_sha256": clear["prev_sha256"],
        "at": accepted["at"], "bundle_id": accepted["bundle_id"], "item_id": "i07",
        "annotator": NEUTRAL, "phase": A.APPROVAL, "blind": False, "reading": {},
        "field_verdicts": {}, "flags": [], "confidence": "", "notes": "", "refers_to": 3,
        "decision": "WITHDRAWN", "rubric": A.RUBRIC, "acknowledged_warnings": [],
        "local": {"seq": 10, "sha256": V._sha(log_lines(base)[9])}, "clears": ret(9)}
    assert hosted_return(store, one, "i07") is None and site_state(one) == forms.DONE
    events = store.events()
    assert "cleared 0 pushed return(s)" in run("push-returns") and store.events() == events
    assert "PASS" in run("verify") and "PASS" in run("parity")
    # Nothing asks the reviewer to save again, so the acceptance stands.
    assert "pulled 0 hosted line(s)" in run("pull-to-local")
    b = A.load_bundle(base / "bundle", A.RESTRICTED)
    records = A.read_annotations(base).records
    assert A.effective_approval(A.rubric2(records), "i07", b)["decision"] == "ACCEPT"
    assert next(r for r in A.resolve(records, b) if r["item_id"] == "i07")["status"] == "APPROVED"
    # The store keeps the local order: a return of that reading AFTER the clearing line shows.
    decide(base, "i07", 3, "RETURN", NOTE)  # line 11
    assert f"  REMOTE-2 i07: return {ret(11)} of the reading 3" in run("push-returns")
    assert hosted_return(store, one, "i07")["seq"] == ret(11) and site_state(one) == \
        forms.RETURNED
    # Superseded by a decision on ANOTHER reviewer's reading of the item: cleared too, and the
    # other reviewer gets theirs (two lines made from one local line, two seqs).
    local_save(base, TWO, "i07", A.AFTER_REVEAL)  # line 12
    run("import-log")
    decide(base, "i07", 12, "RETURN", NOTE)  # line 13
    out = run("push-returns")
    assert f"  REMOTE-3 i07: return {ret(13)} of the reading 12" in out
    assert f"  REMOTE-2 i07: line {ret(13) + 1} clears the return {ret(11)}" in out
    assert (site_state(one), site_state(two)) == (forms.DONE, forms.RETURNED)
    assert "events: LEGACY 8, RETURN 5" in run("verify") and "PASS" in run("parity")


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
        stray = V.encode_line({**record, "seq": record["seq"] + 2 * n, "refers_to": refers_to,
                               "prev_sha256": prev,
                               "local": {**record["local"], "seq": record["local"]["seq"] + n}})
        restored.insert_events([cli.S.EventLine(one.id, "RETURN", stray)])
    # ... or a seq that is not its local line's place, or a clearing line that carries a note.
    for seq, extra in ((record["seq"] + 9, {}), (record["seq"] + 21, {
            "decision": "WITHDRAWN", "notes": "a note", "local": {"seq": 19, "sha256": "0" * 64}})):
        restored.insert_events([cli.S.EventLine(one.id, "RETURN", V.encode_line(
            {**record, "seq": seq, **extra}))])
    out = run("verify", code=1)
    assert [x for x in out.splitlines() if x.startswith("  ")] == [
        f"  RETURN_DOES_NOT_NAME_ITS_READING at {record['seq'] + n}" for n in (2, 4, 6, 9, 21)]
    # The site never writes a return, and a return is never in the hosted chain.
    assert restored.records_of(one)[-1]["seq"] == record["seq"] + 21
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
    row, _kind, _to = store.mail_claim(())
    store.mail_done(row, cli.S.MAIL_SENT)
    for _ in range(3):  # tried three times, failed three times: given up
        row, _kind, _to = store.mail_claim(())
        store.mail_done(row, "MAIL_UNREACHABLE")
    after = run("status")
    assert re.search(r"REMOTE-2: [^\n]*invited \d{4}-\S+,[^\n]*\n"
                     r"  mail INVITE_CONTINUE: queued \d{4}-\S+, sent \d{4}-\S+, result SENT\n"
                     r"  mail INVITE_START: queued \d{4}-\S+, sent -, result MAIL_UNREACHABLE\n",
                     after)
    assert store.mail_claim(()) is None and store.mail_left() == 0
    # A message that was given up may be queued again; one that waits may not.
    run("invite", "REMOTE-2", "--kind", "start")
    assert run("invite", "REMOTE-2", "--kind", "start", code=2) == "refused: ALREADY_QUEUED\n"
    # A try whose outcome was never recorded (the site was stopped between the send and its
    # record): status says so, the site does not send it again, the operator may queue again.
    store.mail_claim(())
    unknown = run("status")
    assert re.search(r"  mail INVITE_START: queued \S+, sent -, result - \(sending began "
                     r"\d{4}-\S+ and its outcome was not recorded: it is not sent again by "
                     r"itself; ask the reviewer, then invite again\)\n", unknown)
    assert store.mail_claim(()) is None and store.mail_left() == 0
    run("invite", "REMOTE-2", "--kind", "start")
    # Nobody who cannot sign in is invited.
    with store.connection() as conn:
        conn.execute("UPDATE reviewer SET access = 'RESTRICTED' WHERE alias = 'REMOTE-2'")
    assert run("invite", "REMOTE-2", "--kind", "continue", code=2) == "refused: CANNOT_SIGN_IN\n"
    # The queue is no part of an export.
    out = tmp_path / "export.jsonl"
    run("export", "--out", out)
    assert "INVITE" not in out.read_text() and "mail" not in out.read_text()


def test_unlock_clears_a_reviewers_sign_in_counters_and_status_shows_a_lock(run, store, base):
    """Ten wrong codes within 24 hours lock an address's sign-in (tests/test_hosted_web_signin.py);
    the operator sees it in ``status`` and lifts it with ``unlock``."""
    run("publish-bundle", "--demo", base)
    run("sync-reviewers", "--demo")
    one = store.reviewer_by_login(ONE.login)
    assert "sign-in: locked" not in run("status")
    with store.connection() as conn:  # what the site records: nine wrong codes, then the tenth
        for n in range(9):
            conn.execute("INSERT INTO rate_limit (bucket, at) VALUES (%s, now() - %s * interval"
                         " '1 minute')", (f"wrong:r{one.id}", 30 - n))
        conn.execute("INSERT INTO rate_limit (bucket, at) VALUES (%s, now())", (f"to:r{one.id}",))
    assert "sign-in: locked" not in run("status")  # nine: not locked
    with store.connection() as conn:
        conn.execute("INSERT INTO rate_limit (bucket, at) VALUES (%s, now())",
                     (f"wrong:r{one.id}",))
    out = run("status")
    assert re.search(r"REMOTE-2: [^\n]*\n(  [^\n]*\n)*  sign-in: locked until \d{4}-\d\d-\d\dT\S+ "
                     r"\(unlock lifts it\)\n", out)
    assert "sign-in: locked" not in out.split("REMOTE-3:")[1]
    assert run("unlock", "REMOTE-2") == "REMOTE-2: sign-in unlocked; 11 counted event(s) cleared\n"
    assert "sign-in: locked" not in run("status")
    assert run("unlock", "REMOTE-2") == "REMOTE-2: sign-in unlocked; 0 counted event(s) cleared\n"
    assert run("unlock", "REMOTE-9", code=2) == "refused: UNKNOWN_ALIAS\n"


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
    assert ("local lines of reviewers that no row holds: 3, of 1 sign-in spelling(s)") in out \
        and out.splitlines()[-1].endswith("FAIL, 2 reviewer(s)")
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
    """Re-check R1 (probe ``test_r11::test_real_save_before_any_import_and_the_empty_log_case``):
    a reviewer's saves before the first ``import-log`` left lines that ``pull-to-local``,
    ``push-returns`` and ``import-log`` then refused for good. Now the store refuses such a
    save (NO_ANCHOR; the site says calmly that the workspace is not open yet), and a database
    that holds such lines anyway (made before this rule, or restored from one) can still bring
    them home: a log that holds only its own pulled copies is the log they continue."""
    from reviewer_web.store import EventLine

    run("init-db")
    run("publish-bundle", "--demo", base)
    run("sync-reviewers", "--demo")
    one = store.reviewer_by_login(ONE.login)
    with pytest.raises(A.AdjudicationError, match="^NO_ANCHOR$"):
        hosted_save(store, one, "i07", A.INDEPENDENT)
    assert store.events() == [] and store.anchor() is None
    # Lines of the old rule: two saves of a reviewer before any import, one after a pull.
    b = store.bundle_for(one)
    body = A.build_annotation([], b, one.session(), **reading_kw(b, "i07", [], one.session(),
                                                                 phase=A.INDEPENDENT))
    first = {**body, "schema": A.ANNOTATION_SCHEMA, "seq": HOSTED_1, "prev_sha256": V.GENESIS}
    store.insert_events([EventLine(one.id, "HOSTED", V.encode_line(first))])
    # A folder with a log of its own: not the (empty) log this chain continues.
    other = write_demo(tmp_path / "other")
    local_save(other, TWO, "i08", A.INDEPENDENT)
    for command in ("pull-to-local", "push-returns"):
        assert run(command, "--dir", other, code=2) == "refused: NO_ANCHOR_IMPORT_LOG_FIRST\n"
    assert len(log_lines(other)) == 1 and store.anchor() is None
    assert "pulled 1 hosted line(s) into the local log: REMOTE-2 1; 0 already held" in run(
        "pull-to-local")
    [marker] = A.build_system_shown([first], b, one.session(), "i07")
    second = {**marker, "schema": A.ANNOTATION_SCHEMA, "seq": HOSTED_1 + 1,
              "prev_sha256": hashlib.sha256(V.encode_line(first)).hexdigest()}
    store.insert_events([EventLine(one.id, "HOSTED", V.encode_line(second))])
    # The second pull: the log holds only its own pulled copy, so it is still that log.
    assert "pulled 1 hosted line(s) into the local log: REMOTE-2 1; 1 already held" in run(
        "pull-to-local")
    assert [r["hosted"]["seq"] for r in A.read_annotations(base).records] == [
        HOSTED_1, HOSTED_1 + 1]
    assert "returns that stand in the local log: 0" in run("push-returns")


def test_two_pulls_at_once_append_every_hosted_line_once(cli, run, store, base, monkeypatch,
                                                         capsys):
    """Probe ``test_two_pulls_at_once_append_twice`` (finding 9): both runs read the log before
    either appended, and six hosted lines became twelve."""
    import threading

    rows = cutover(run, store, base)
    hosted_work(store, rows)
    both_have_read, read_log = threading.Barrier(2, timeout=30), cli.read_log

    def read_then_wait(folder):
        got = read_log(folder)
        both_have_read.wait()
        return got

    monkeypatch.setattr(cli, "read_log", read_then_wait)
    codes = []
    threads = [threading.Thread(target=lambda: codes.append(cli.main(["pull-to-local"])))
               for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    monkeypatch.setattr(cli, "read_log", read_log)
    printed = capsys.readouterr().out
    log = A.read_annotations(base)
    assert codes == [0, 0] and log.ok and log.count == 8 + 6
    assert sorted(r["hosted"]["seq"] for r in log.records[8:]) == [
        HOSTED_1 + n for n in range(6)]  # each exactly once, whichever run wrote it
    counts = [int(x) for x in re.findall(r"pulled (\d+) hosted line", printed)]
    assert len(counts) == 2 and sum(counts) == 6
    assert [r["refers_to"] for r in log.records if r.get("hosted", {}).get("seq")
            == HOSTED_1 + 4] == [next(r["seq"] for r in log.records if r.get(
                "hosted", {}).get("seq") == HOSTED_1 + 2)]  # still the local line it means
    assert "PASS" in run("parity") and "pulled 0 hosted line(s)" in run("pull-to-local")


# ------------------------------------------------------------------ only the material needed
# (The independent review's finding 8.)


def test_the_published_part_holds_nothing_that_no_page_shows(cli, run, store, base):
    """Probes ``test_published_part_holds_more_than_is_shown`` and
    ``test_an_image_of_a_post_no_page_shows_is_uploaded``: the text and the images of posts that
    are only named, the images of context posts, the set of every idea, the system's market of a
    stub form and the manifest's counts were published."""
    extend(base, 2)

    def posts(rows):  # the post that is only named has an image; so has a post shown as context
        rows["s02"]["media"] = ["chart-s04.png"]

    def items(rows):  # a post of an idea outside the scope, shown as context of a form in scope
        rows["i08"]["context_refs"].append("s04")
        rows["x01"]["slot"], rows["i07"]["slot"] = 0, 1  # the other form now comes FIRST

    edit(base / "bundle" / "posts.jsonl", posts)
    edit(base / "bundle" / "items.jsonl", items)
    reseal(base)
    full, files, media = cli.subset(base, 2)
    part = A.bundle_from_bytes(*files, access=A.FULL)
    assert cli.parity_problems(full, part, media, 2) == []
    offered = [i for _s, group in A.ordered(part, 2) for i in group]
    own = {i["post_ref"] for i in offered}
    evidence = own | {r for i in offered for r in i["context_refs"]}
    # A post no page shows: named by its id only, without text, time, numbers or images.
    assert sorted(set(part.posts) - evidence) == ["s02"] and part.posts["s02"]["text"] == ""
    # A post shown as context: its time and text, no image, no numbers, no links.
    assert part.posts["s04"] == {
        **full.posts["s04"], "media": [], "numbers": [], "revision": None}
    assert full.posts["s04"]["media"] == ["chart-s04.png"] and full.posts["s04"]["numbers"]
    # Images: exactly those of the forms' own posts, which is all the image route serves.
    assert sorted(media) == sorted({n for r in own for n in part.posts[r]["media"]}) == [
        "chart-p01.png"]
    raw = {r["item_id"]: r for r in map(json.loads, files[2].decode().splitlines())}
    # The other form of a shared post comes before the form in scope here: its market is read
    # (``A.position_compared``), and nothing else of it is published.
    assert raw["x01"] == {"item_id": "x01", "kind": "INSTRUCTION", "post_ref": "p07", "slot": 0,
                          "order_in_sequence": 2, "ai": {"family": "OTHER"}}
    without = dict(raw, x01={k: v for k, v in raw["x01"].items() if k != "ai"})
    wrong = [files[0], files[1],
             "".join(json.dumps(r) + "\n" for r in without.values()).encode(), files[3]]
    assert cli.parity_problems(full, A.bundle_from_bytes(*wrong, access=A.FULL), media, 2) == [
        ("earlier_markets", "i07")]
    # Of an item in scope: the keys the loader reads, at every level.
    assert set(raw["i01"]) <= set(cli.ITEM_KEYS) and set(raw["i01"]["ai"]) <= {
        *cli.AI_KEYS, *cli.AI_PARTS, *cli.AI_LISTS}
    assert set(raw["i01"]["ai"]["entry"]) == {"price", "role", "exactness"}
    assert set(raw["i01"]["mapping"]) == set(cli.MAPPING_KEYS)
    assert set(raw["i01"]["holds"]["SHIPPED"]) == {"status", "reasons"}
    assert b"never reads" not in b"".join(files)
    assert {s["set"] for s in part.sequences} == {None} and len(part.sequences) == 33
    assert sorted(part.manifest) == ["bundle_id", "families", "schema"]
    # The command says how many images it proved, and uploads no other.
    out = run("publish-bundle", "--demo", base)
    assert "posts 9 with text and 1 named only, of 41; images 1," in out
    assert ("images: all 1 that a page shows are in the publication with the sha256 of the "
            "local file; no other image is published") in out
    with store.connection() as conn:
        assert conn.execute("SELECT name FROM media").fetchall() == [("chart-p01.png",)]


# ------------------------------------------------------------------ what parity did not say
# (The independent review's finding 13.)


def test_parity_compares_the_site_s_state_of_every_form_not_only_the_next_step(run, store,
                                                                              base):
    rows = cutover(run, store, base)
    one = rows["REMOTE-2"]
    hosted_save(store, one, "i03", A.INDEPENDENT)
    hosted_reveal(store, one, "i03")
    hosted_save(store, one, "i03", A.AFTER_REVEAL)  # the local line 11 after the pull
    run("pull-to-local")
    decide(base, "i07", 3, "RETURN", NOTE)
    run("push-returns")
    assert "PASS" in run("parity")
    decide(base, "i03", 11, "RETURN", NOTE)  # a second return, not pushed yet
    out = run("parity", code=1)  # the next step is the same on both sides: the first return
    assert "  next step: local i07 RETURNED, hosted i07 RETURNED: the same" in out
    assert "  the site's state of each form: 44 equal, 1 different" in out
    assert "  i03: site state local RETURNED, hosted DONE" in out
    run("push-returns")
    assert "  the site's state of each form: 45 equal, 0 different" in run("parity")
    decide(base, "i03", 11, "ACCEPT")  # taken back locally: stale on the site until the push
    out = run("parity", code=1)
    assert "  i03: site state local DONE, hosted RETURNED" in out
    run("push-returns")
    assert "PASS" in run("parity")


def test_sync_reviewers_demo_is_refused_beside_a_real_bundle_unless_forced(run, store, base,
                                                                         tmp_path):
    demo = write_demo(tmp_path / "demo")
    edit(base / "bundle" / "posts.jsonl",
         lambda rows: rows["p09"].update(text="Back on Wednesday, after the holiday."))
    reseal(base, product_commit="abc1234")  # ``base`` stands in for the real folder
    real_id, demo_id = (A.bundle_digest(d / "bundle") for d in (base, demo))
    run("publish-bundle")
    run("publish-bundle", "--demo", demo)
    run("sync-reviewers")
    # It would silently move the reviewers from the real forms to the invented ones.
    assert run("sync-reviewers", "--demo", code=2) == "refused: REAL_BUNDLE_IS_PUBLISHED\n"
    assert {r.bundle_id for r in store.reviewers()} == {real_id}
    assert "(demonstration data)" in run("sync-reviewers", "--demo", "--force")
    assert {r.bundle_id for r in store.reviewers()} == {demo_id}
    run("sync-reviewers")  # and back
    assert {r.bundle_id for r in store.reviewers()} == {real_id}


# ------------------------------------------------------------------ the site's own database role
# (The independent review's finding 12.)


def test_the_site_s_role_does_what_the_site_does_and_cannot_touch_the_append_only_rule(
        cli, run, store, dsn, tmp_path):
    """Probe ``test_what_defeats_the_append_only_trigger``: the role that owns ``event`` can
    disable or drop its trigger, so the site must not connect as that role."""
    import psycopg
    from hosted_web_site import FIRST, ORIGIN, SECOND, Site
    from psycopg.conninfo import conninfo_to_dict

    site = Site(tmp_path / "site", store, dsn)  # the invented bundle and its two reviewers
    role = f"site_{conninfo_to_dict(dsn)['dbname']}"  # a role is of the whole server: one a test
    address = tmp_path / "keys" / "site-address"
    out = run("make-site-role", "--role", role, "--out", address)
    url = address.read_text().strip()
    secret = conninfo_to_dict(url)["password"]
    assert out.startswith(f"site role {role}: created; it owns nothing and may: read reviewer, ")
    assert len(secret) >= 40 and secret not in out and "://" not in out
    assert stat.S_IMODE(address.stat().st_mode) == 0o600
    assert {**conninfo_to_dict(url), "password": ""} == {
        **conninfo_to_dict(dsn), "user": role, "password": ""}
    # The site through that role: sign-in, a save, the reveal, the second save, an image, and
    # an invitation sent by the site's own sender.
    site.dsn = url
    token, operator = "an-invented-operator-token-0123456789abcdef", None
    app = site.production(OPERATOR_TOKEN=token, SITE_URL="https://review.example.invalid")
    browser = site.browser(app).sign_in(FIRST)
    browser.review("i07")
    assert [r["phase"] for r in site.records(FIRST)] == [A.INDEPENDENT, A.SYSTEM_SHOWN,
                                                         A.AFTER_REVEAL]
    image = browser.get(f"/media/{site.bundle.bundle_id}/p01/chart-p01.png")
    assert image.status_code == 200 and image.get_data().startswith(b"\x89PNG")
    store.queue_mail(SECOND, "INVITE_START")
    operator = app.test_client().post("/internal/send-queued", base_url=ORIGIN,
                                      headers={"Authorization": f"Bearer {token}"})
    assert operator.status_code == 200
    assert [(m["alias"], m["result"]) for m in store.queued_mail()] == [(SECOND, "SENT")]
    times = {r["alias"]: (r["first_sign_in_at"], r["invited_at"]) for r in store.status()}
    assert times[FIRST][0] is not None and times[SECOND][1] is not None

    def as_site(statement: str):
        with psycopg.connect(url) as conn:
            return conn.execute(statement)

    # ... and nothing else: not the append-only rule, not a row it has no business with.
    for statement in (
            "UPDATE event SET item_id = 'x'", "DELETE FROM event", "TRUNCATE event",
            "TRUNCATE reviewer CASCADE", "ALTER TABLE event DISABLE TRIGGER USER",
            "ALTER TABLE event DISABLE TRIGGER ALL",
            "DROP TRIGGER event_no_update_or_delete ON event",
            "DROP TRIGGER event_no_truncate ON event", "DROP TABLE event",
            "ALTER TABLE event OWNER TO CURRENT_USER", "SET session_replication_role = replica",
            ("CREATE OR REPLACE FUNCTION event_is_append_only() RETURNS trigger LANGUAGE plpgsql"
             " AS $$ BEGIN RETURN NEW; END $$"),
            "SET ROLE postgres", "CREATE TABLE elsewhere (x int)",
            "UPDATE reviewer SET revoked_at = NULL", "UPDATE reviewer SET alias = 'x'",
            "UPDATE reviewer SET bundle_id = bundle_id", "DELETE FROM reviewer",
            ("INSERT INTO reviewer (alias, login, login_shown, role, access, scope_sequences)"
             " VALUES ('x', 'x', 'x', 'REVIEWER', 'FULL', 1)"),
            "UPDATE bundle SET active = FALSE", "DELETE FROM bundle", "DELETE FROM media",
            "UPDATE media SET bytes = ''", "UPDATE chain_anchor SET snapshot_count = 0",
            "INSERT INTO chain_anchor VALUES (1, 0, repeat('0', 64), repeat('0', 64))",
            "INSERT INTO mail_queue (reviewer_id, kind) VALUES (1, 'INVITE_START')",
            "UPDATE mail_queue SET kind = 'INVITE_CONTINUE'", "DELETE FROM mail_queue"):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            as_site(statement)
    assert as_site("SELECT count(*) FROM event").fetchone() == (3,) == (len(store.events()),)
    assert as_site("SELECT count(*) FROM pg_trigger WHERE tgrelid = 'event'::regclass"
                   " AND NOT tgisinternal AND tgenabled = 'O'").fetchone() == (3,)
    with pytest.raises(psycopg.errors.RestrictViolation):  # what it MAY insert is still checked
        as_site("INSERT INTO event (seq, prev_sha256, sha256, line, reviewer_id, bundle_id,"
                " item_id, phase, at, origin, batch) SELECT 3000009, prev_sha256, repeat('a', 64),"
                " line, reviewer_id, bundle_id, item_id, phase, at, 'TEST', 3000009 FROM event"
                " LIMIT 1")
    # Again: a new password, the same rights; the administrator's own role is never touched.
    with store.connection() as conn:
        before = conn.execute("SELECT rolpassword FROM pg_authid WHERE rolname = %s",
                              (role,)).fetchone()[0]
    assert run("make-site-role", "--role", role, "--out", address, code=2) == \
        "refused: FileExistsError\n"  # never over an address file that exists
    out = run("make-site-role", "--role", role, "--out", tmp_path / "keys" / "second")
    assert out.startswith(f"site role {role}: password changed;")
    again = conninfo_to_dict((tmp_path / "keys" / "second").read_text().strip())["password"]
    with store.connection() as conn:
        after = conn.execute("SELECT rolpassword FROM pg_authid WHERE rolname = %s",
                             (role,)).fetchone()[0]
    assert again != secret and after != before and after.startswith("SCRAM-SHA-256$")
    assert site.browser(app).sign_in(SECOND).get("/").status_code == 200  # still what it needs
    for name, code in (("postgres", "NOT_A_SITE_ROLE"), ("Bad-Name", "BAD_ROLE_NAME"),
                       ("x; DROP TABLE event", "BAD_ROLE_NAME")):
        assert run("make-site-role", "--role", name, "--out", tmp_path / "keys" / "third",
                   code=2) == f"refused: {code}\n"
    assert not (tmp_path / "keys" / "third").exists()


# ------------------------------------------------------------------ the web app's own paths
# What the SITE's code does, seen from the operator's side: as the role ``make-site-role``
# creates, as a test identity, and on the part of a bundle that ``publish-bundle`` uploads.


def test_the_site_s_role_covers_every_write_of_the_sign_in_the_save_and_the_sender(
        run, store, dsn, tmp_path, caplog):
    """A right the site's code uses and ``make-site-role`` does not grant is a failure in
    production and nowhere else (every other test connects as the owner). So each path of the
    web app that writes runs here as that role: a code that could not be mailed, a wrong and a
    right one; ten wrong codes and the operator's unlock; a save, the same save again, the
    reveal, by a reviewer and by a test identity; the cookie's renewal, the end of a session,
    sign-out; an invitation that fails and is sent at the next call."""
    import logging

    import psycopg
    from hosted_web_site import FIRST, ORIGIN, SECOND, Site, scenario_reading
    from psycopg.conninfo import conninfo_to_dict
    from reviewer_web import auth, mail
    from reviewer_web.store import Store

    caplog.set_level(logging.DEBUG)
    site = Site(tmp_path / "site", store, dsn)  # the invented bundle and its two reviewers
    store.upsert_reviewer("TEST-1", TESTER, name="Test Identity", scope_sequences=40,
                          is_test=True)
    role, address = f"site_{conninfo_to_dict(dsn)['dbname']}", tmp_path / "keys" / "site-address"
    out = run("make-site-role", "--role", role, "--out", address)
    assert "set reviewer.(first_sign_in_at, invited_at); mail_queue.(attempts, sending_at, " \
           "result, sent_at)\n" in out
    site.dsn = address.read_text().strip()
    with Store(site.dsn).connection() as conn:
        assert conn.execute("SELECT current_user").fetchone() == (role,)

    class Mailer:
        """The outbox, or a mail server that cannot be reached."""

        down = False

        def __call__(self, to, subject, text):
            if self.down:
                raise mail.Unreachable("no connection")
            site.outbox(to, subject, text)

    mailer, token = Mailer(), "an-invented-operator-token-0123456789abcdef"
    app = site.production(mailer, OPERATOR_TOKEN=token, SITE_URL="https://review.example.invalid")
    rows = site.reviewers

    def ask(browser, alias):
        return browser.post("/sign-in", {"address": rows[alias].login_shown})

    def enter(browser, alias, code, plus: int = 0):
        return browser.post("/sign-in/verify", {"address": rows[alias].login_shown,
                                                "code": f"{(int(code) + plus) % 10 ** 6:06d}"})

    def counted(bucket: str) -> int:
        return site.sql("SELECT count(*) FROM rate_limit WHERE bucket = %s", bucket)[0][0]

    # A code that could not be mailed is ended; a wrong code is counted; of two codes asked
    # for, the older still signs in, and the first sign-in is noted.
    one, mailer.down = site.browser(app), True
    assert "The code could not be sent." in ask(one, FIRST).get_data(as_text=True)
    assert site.sql("SELECT count(*) FROM login_code WHERE used_at IS NULL") == [(0,)]
    mailer.down = False
    assert ask(one, FIRST).status_code == 200
    code = site.code()
    assert enter(one, FIRST, code, plus=1).status_code == 401
    assert ask(one, FIRST).status_code == 200 and site.code() != code
    assert enter(one, FIRST, code).status_code == 303 and one.get("/").status_code == 200
    assert counted(f"wrong:r{rows[FIRST].id}") == 1
    assert site.sql("SELECT count(*), count(used_at) FROM login_code") == [(3, 3)]
    assert site.sql("SELECT first_sign_in_at IS NOT NULL FROM reviewer ORDER BY id") == [
        (True,), (False,), (False,)]
    # Ten wrong codes lock the other reviewer's sign-in; the operator sees it and lifts it.
    two = site.browser(app)
    for _ in range(2):
        assert ask(two, SECOND).status_code == 200
        for n in range(1, 6):
            assert enter(two, SECOND, site.code(), plus=n).status_code == 401
    assert counted(f"wrong:r{rows[SECOND].id}") == 10
    assert auth.locked_until(store, rows[SECOND].id, site.clock()) is not None
    sent = len(site.outbox.sent)
    assert ask(two, SECOND).status_code == 200 and len(site.outbox.sent) == sent  # locked
    assert "  sign-in: locked until " in run("status")
    assert run("unlock", SECOND).startswith(f"{SECOND}: sign-in unlocked; ")
    assert ask(two, SECOND).status_code == 200 and len(site.outbox.sent) == sent + 1
    assert enter(two, SECOND, site.code()).status_code == 303 and two.get("/").status_code == 200
    # A whole form; then a save sent twice: one record.
    one.review("i07")
    reading, _form = scenario_reading(site.bundle, "i08")
    one.open("i08")
    one.fill(reading)
    one.set({"confidence": "HIGH", "notes": "sent twice", "ui.later": "timing"})
    assert site.sql("SELECT item_id FROM draft") == [("i08",)]  # kept by the round trips
    answers = [one.post("/form/i08/save", one.shown.data()) for _ in range(2)]
    assert [a.status_code for a in answers] == [303, 303]
    assert site.sql("SELECT count(*) FROM draft") == [(0,)]  # ended with the save
    assert {a.headers["Location"] for a in answers} == {"/form/i08?saved=1000004"}
    # The test identity, on the same site as the same role: a stream of its own.
    expiry = "SELECT expires_at FROM web_session WHERE reviewer_id = %s"
    [(stored,)] = site.sql(expiry, rows[FIRST].id)
    site.clock.forward(hours=2)
    tester = site.browser(app)
    assert ask(tester, "TEST-1").status_code == 200
    assert enter(tester, "TEST-1", site.code()).status_code == 303
    tester.review("i07")
    assert [(e["alias"], e["origin"], e["seq"]) for e in store.events()] == [
        *[(FIRST, "HOSTED", 1_000_001 + n) for n in range(4)],
        *[("TEST-1", "TEST", TEST_1 + n) for n in range(3)]]
    # The cookie is sent again when the stored expiry moves; sign-out; ninety days end a session,
    # and the next request for a code clears what has run out.
    renewed = one.get("/")
    assert renewed.status_code == 200 and "Max-Age=2592000" in renewed.headers["Set-Cookie"]
    assert site.sql(expiry, rows[FIRST].id) == [(site.clock() + auth.SESSION_VALID,)] != [
        (stored,)]
    assert two.post("/sign-out", {"_csrf": two.csrf()}).status_code == 303
    assert site.sql("SELECT count(*) FROM web_session") == [(2,)]
    site.clock.forward(days=91)
    ended = one.get("/")
    assert (ended.status_code, ended.headers["Location"]) == (303, "/sign-in")
    assert ask(one, FIRST).status_code == 200
    assert enter(one, FIRST, site.code()).status_code == 303 and one.get("/").status_code == 200
    assert site.sql("SELECT (SELECT count(*) FROM web_session), (SELECT count(*) FROM login_code),"
                    " (SELECT count(*) FROM rate_limit WHERE at < %s)", site.clock()) == [
        (1, 1, 0)]
    # The sender: a try is counted before the mail server hears of it, a failure keeps its
    # code, and the next call sends.
    store.queue_mail(FIRST, "INVITE_CONTINUE")
    store.queue_mail(SECOND, "INVITE_START")

    def send() -> dict:
        answer = app.test_client().post("/internal/send-queued", base_url=ORIGIN,
                                        headers={"Authorization": f"Bearer {token}"})
        assert answer.status_code == 200
        return answer.get_json()

    mailer.down = True
    assert send() == {"sent": 0, "failed": 2, "left": 2}
    assert [(m["result"], m["attempts"], m["sending_at"] is not None, m["sent_at"])
            for m in store.queued_mail()] == [("MAIL_UNREACHABLE", 1, True, None)] * 2
    mailer.down = False
    assert send() == {"sent": 2, "failed": 0, "left": 0}
    assert [(m["alias"], m["result"], m["attempts"], m["sent_at"] is not None)
            for m in store.queued_mail()] == [(FIRST, "SENT", 2, True), (SECOND, "SENT", 2, True)]
    assert site.sql("SELECT invited_at IS NOT NULL FROM reviewer ORDER BY id") == [
        (True,), (True,), (False,)]
    # Nothing the site did was refused to it ...
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "REQUEST_FAILED" not in logged and "InsufficientPrivilege" not in logged
    assert "MAIL_UNREACHABLE" in logged and f"SIGN_IN_TRIES_EXHAUSTED alias={SECOND}" in logged

    # ... and of the rows it writes, nothing but the columns its code sets.
    def as_site(statement: str):
        with psycopg.connect(site.dsn) as conn:
            return conn.execute(statement)

    for statement in (
            "UPDATE mail_queue SET queued_at = now()", "UPDATE mail_queue SET reviewer_id = 1",
            "UPDATE mail_queue SET kind = kind", "UPDATE reviewer SET is_test = TRUE",
            "UPDATE reviewer SET access = 'FULL'", "UPDATE reviewer SET scope_sequences = 40",
            "UPDATE reviewer SET login = login, login_shown = login_shown",
            "ALTER TABLE mail_queue DROP COLUMN sending_at", "DROP INDEX mail_queue_one_to_send",
            "TRUNCATE rate_limit", "DROP TABLE web_session"):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            as_site(statement)
    assert as_site("UPDATE mail_queue SET sending_at = sending_at").rowcount == 2


def test_a_test_identity_s_saves_through_the_site_are_a_stream_of_their_own(
        run, store, dsn, tmp_path):
    """The web app's save path beside the TEST stream: a test identity's saves through the
    pages are numbered 3000001, ... and chained among themselves, whenever a reviewer saves in
    between; the same POST again finds the save by its key (a hash of the page's key and of
    what was posted) and stores nothing; ``test-report`` shows the lines and the states the
    pages show."""
    from hosted_web_site import FIRST, Site, scenario_reading
    from reviewer_web import app as web
    from reviewer_web import forms
    from reviewer_web.store import TEST_GENESIS

    site = Site(tmp_path / "site", store, dsn)  # the invented bundle and its two reviewers
    store.upsert_reviewer("TEST-1", TESTER, name="Test Identity", scope_sequences=40,
                          is_test=True)
    app = site.production()
    reviewer, tester = (site.browser(app).sign_in(alias) for alias in (FIRST, "TEST-1"))
    reviewer.review("i07")  # 1000001, 1000002, 1000003
    reading, _form = scenario_reading(site.bundle, "i07")
    tester.open("i07")
    tester.fill(reading)
    tester.set({"confidence": "HIGH", "notes": "a double click"})
    data = tester.shown.data()
    answers = [tester.post("/form/i07/save", data) for _ in range(2)]
    assert [a.status_code for a in answers] == [303, 303]
    assert {a.headers["Location"] for a in answers} == {f"/form/i07?saved={TEST_1}"}
    saved = tester.get(f"/form/i07?saved={TEST_1}").get_data(as_text=True)  # and the reveal
    assert "Saved." in saved and f"stored as record {TEST_1}" in saved
    assert "Demonstration data" in saved
    reviewer.review("i08")  # 1000004, 1000005, 1000006: between the test identity's lines
    tester.open("i07")
    assert "Saved." in tester.save(confidence="MEDIUM").text
    again = site.browser(app).sign_in("TEST-1")  # the same POST once more, from a new session
    retry = [(n, again.csrf() if n == "_csrf" else v) for n, v in data]
    assert again.post("/form/i07/save", retry).headers["Location"] == f"/form/i07?saved={TEST_1}"
    events = {alias: [e for e in store.events() if e["alias"] == alias]
              for alias in (FIRST, "TEST-1")}
    mine = events["TEST-1"]
    assert [(e["origin"], e["seq"], e["phase"]) for e in mine] == [
        ("TEST", TEST_1, A.INDEPENDENT), ("TEST", TEST_1 + 1, A.SYSTEM_SHOWN),
        ("TEST", TEST_1 + 2, A.AFTER_REVEAL)]
    assert [e["prev_sha256"] for e in mine] == [TEST_GENESIS, mine[0]["sha256"],
                                                mine[1]["sha256"]]
    key = next(v for n, v in data if n == "_key")
    # (the session's token and the interface state, ``ui.*``, are no part of what was saved)
    assert mine[0]["idempotency_key"] == web.save_key(
        key, [x for x in data if x[0] != "_csrf" and not x[0].startswith("ui.")])
    assert any(x[0].startswith("ui.") for x in data)  # the page does post such fields
    assert [(e["origin"], e["seq"]) for e in events[FIRST]] == [
        ("HOSTED", 1_000_001 + n) for n in range(6)]
    assert [e["prev_sha256"] for e in events[FIRST]] == [
        V.GENESIS, *[e["sha256"] for e in events[FIRST][:-1]]]  # no log imported here
    # The operator's report of the test identity, and the page's own state of that form.
    out = run("test-report", "TEST-1").splitlines()
    assert re.fullmatch(r"TEST-1 \(test identity\): bundle [0-9a-f]{12} \(demonstration data\), "
                        r"scope 40, first sign-in \d{4}-\S+, web sessions 2, revoked -", out[0])
    assert out[1] == "events: 3"
    assert [line.split()[0::2] for line in out[2:5]] == [
        [str(TEST_1), "INDEPENDENT"], [str(TEST_1 + 1), "SYSTEM_SHOWN"],
        [str(TEST_1 + 2), "AFTER_REVEAL"]]
    assert out[5:] == ["TEST-1: records 3; forms 45: DONE 1, OPEN 44", "  i07 DONE"]
    row = site.reviewers["TEST-1"]
    assert forms.step_state(site.bundle, row.session(), store.records_of(row), "i07") == forms.DONE
    out = run("verify", code=1)  # both chains hold; all it misses is an imported log
    assert out.startswith("events: HOSTED 6, TEST 3; hosted chain on ") and \
        "CHAIN_BROKEN" not in out
    assert out.splitlines()[1:] == [
        "published files: no active bundle is this folder's: not compared", "  NO_SNAPSHOT",
        "verify: FAIL, 1 problem(s)"]
    out = run("status")
    assert "TEST-1" not in out and out.splitlines()[-1] == \
        "test identities: 1 (see test-report ALIAS)"


def test_every_page_of_the_site_is_the_same_on_the_published_part_as_on_the_whole_bundle(
        run, store, dsn, tmp_path, caplog):
    """``publish-bundle`` uploads a part: posts that are only named as stubs, a form outside the
    scope as its place among its post's forms, items and sequences without their set, three
    keys of the manifest. Its parity check compares what the core's functions give. Here the
    pages themselves are compared, as a browser receives them from the web app: blind, with the
    reading after the reveal due, and done; first on the part and then on the whole bundle."""
    import logging

    from hosted_web_site import FIRST, SECOND, Site

    caplog.set_level(logging.DEBUG)
    site = Site(tmp_path / "site", store, dsn, mark=False, edit=lambda folder: extend(folder, 2))
    base, bundle_id = tmp_path / "site" / "demo", site.bundle.bundle_id
    whole = set(site.bundle.items)
    paths = ["/", "/continue", "/guide", "/fields", *[f"/form/{i}" for i in IN_SCOPE],
             "/form/x01", "/form/i09", "/form/s01a",  # outside the scope: not found, on both
             *[f"/media/{bundle_id}/{ref}/{name}" for ref, post in sorted(site.bundle.posts.items())
               for name in post["media"]]]
    assert len(paths) == 17 and f"/media/{bundle_id}/s04/chart-s04.png" in paths

    def received(browser) -> dict:
        """Every page as that browser receives it: status, where it sends the browser, body
        (a form's key is made anew for every rendering)."""
        out = {}
        for path in paths:
            r = browser.get(path)
            out[path] = (r.status_code, r.headers.get("Location"), re.sub(
                rb'(name="_key" value=")[^"]+', rb"\1", r.get_data()))
        return out

    def same(before: dict, after: dict) -> None:
        assert [p for p in paths if before[p] != after[p]] == []
        assert [before[p][0] for p in paths] == [200, 303, 200, 200, *[200] * 8, *[404] * 3,
                                                 200, 404]

    def served() -> set:
        return set(store.bundle_for(site.reviewers[FIRST]).items)

    a, b = site.browser().sign_in(FIRST), site.browser().sign_in(SECOND)
    # Blind, nothing saved: the whole bundle, then the part.
    assert served() == whole
    blind = received(a)
    assert "PASS" in run("publish-bundle", "--demo", base)
    assert served() == {*IN_SCOPE, "x01"}
    same(blind, received(a))
    # On the part: one reviewer finishes every form of the scope; the other saves every
    # independent reading and opens each form, so the reading after the reveal is due.
    for item_id in IN_SCOPE:
        more = {"component": "ACTION_RECORDED_HERE"} if item_id == "i07" else {}  # form 1 of 2
        a.review(item_id, answers=more)
        b.review(item_id, after=False, answers=more)
        assert b.open(item_id).value("_phase") == A.AFTER_REVEAL
    assert a.get("/continue").headers["Location"] == "/"  # nothing is left
    done, due = received(a), received(b)
    assert [r["phase"] for r in store.records_of(site.reviewers[SECOND])] == [
        A.INDEPENDENT, A.SYSTEM_SHOWN] * 8
    # ... and the same pages on the whole bundle.
    hosted_pg.publish_demo(store, base)
    assert served() == whole
    same(done, received(a))
    same(due, received(b))
    assert due["/form/i07"] != done["/form/i07"] != blind["/form/i07"] != due["/form/i07"]
    assert b"SEQUENCE:scs01" in due["/form/i08"][2]  # the idea of a reference that left
    assert "REQUEST_FAILED" not in "\n".join(r.getMessage() for r in caplog.records)


# ------------------------------------------------------------------ an operator who is no superuser


def operator_database(name: str) -> tuple[str, str]:
    """(the superuser's address, the operator's address) of a new database OWNED by a new login
    role that has CREATEROLE and is NOT a superuser, as the hosting provider gives its operator.
    The embedded server trusts every connection, so the role is used, not its password."""
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    server = hosted_pg._server()
    with psycopg.connect(server.get_uri(), autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE ROLE {} LOGIN CREATEROLE NOSUPERUSER PASSWORD 'op'").format(
            sql.Identifier(f"op_{name}")))
        conn.execute(sql.SQL("CREATE DATABASE {} OWNER {}").format(
            sql.Identifier(name), sql.Identifier(f"op_{name}")))
    admin = server.get_uri(name)
    parts = conninfo_to_dict(admin)
    return admin, make_conninfo(**{**parts, "user": f"op_{name}", "password": "op"})


def test_make_site_role_works_for_an_operator_who_is_not_a_superuser(
        cli, monkeypatch, capsys, base, tmp_path):
    """Re-check R4 and the lead's preview database: an operator with CREATEROLE but no superuser
    may not name SUPERUSER, REPLICATION or BYPASSRLS in an ALTER ROLE, so the second run failed
    (InsufficientPrivilege). And the password travelled in clear inside the statement, and a new
    grant meant a new password. Here the WHOLE tool runs as such an operator: init-db (twice),
    the bundle and the reviewers, make-site-role a first and a second time and --grants-only,
    after the schema was dropped and made again, and an export restored into a second such
    database."""
    import psycopg
    from psycopg.conninfo import conninfo_to_dict

    name = f"nosu{os.getpid()}"
    admin, operator = operator_database(name)
    monkeypatch.setenv(cli.URL_ENV, operator)
    monkeypatch.delenv(cli.UNPOOLED_ENV, raising=False)
    monkeypatch.setenv(A.DIR_ENV, str(base))
    sent: list[str] = []
    real_execute = psycopg.Connection.execute

    def spy(self, query, params=None, **kw):
        sent.append(query.as_string(self) if hasattr(query, "as_string") else str(query))
        return real_execute(self, query, params, **kw)

    monkeypatch.setattr(psycopg.Connection, "execute", spy)

    def run(*argv, code=0) -> str:
        capsys.readouterr()
        got = cli.main([str(a) for a in argv])
        printed = "".join(capsys.readouterr())
        assert got == code, printed
        return printed

    with psycopg.connect(operator) as conn:
        assert conn.execute("SELECT rolsuper FROM pg_roles WHERE rolname = current_user"
                            ).fetchone() == (False,)
    for _ in range(2):
        run("init-db")
    half_way(base)
    run("publish-bundle", "--demo", base)
    run("sync-reviewers", "--demo")
    run("import-log")
    role = f"site_{name}"

    def stored_verifier() -> str:
        with psycopg.connect(admin) as conn:
            return conn.execute("SELECT rolpassword FROM pg_authid WHERE rolname = %s",
                                (role,)).fetchone()[0]

    def password_of(path: Path) -> str:
        return conninfo_to_dict(path.read_text().strip())["password"]

    assert "created" in run("make-site-role", "--role", role, "--out", tmp_path / "k" / "a1")
    first = stored_verifier()
    assert "password changed" in run("make-site-role", "--role", role, "--out",
                                     tmp_path / "k" / "a2")
    second = stored_verifier()
    assert first != second and second.startswith("SCRAM-SHA-256$4096:")
    for path, verifier in ((tmp_path / "k" / "a1", first), (tmp_path / "k" / "a2", second)):
        salt = __import__("base64").b64decode(verifier.split("$")[1].split(":")[1])
        assert cli.scram_verifier(password_of(path), salt) == verifier  # its own password's
        # The hosting provider takes a password in plain text only ("Hashed passwords are not
        # supported"): it travels in the ONE statement that sets it, and in no other.
        assert [q.split()[:2] for q in sent if password_of(path) in q] in (
            [["CREATE", "ROLE"]], [["ALTER", "ROLE"]])
    out = run("make-site-role", "--role", role, "--grants-only")
    assert "rights given again, password unchanged" in out and stored_verifier() == second
    assert not (tmp_path / "k" / "a3").exists()
    # The site's role may read and add HOSTED lines; a LEGACY or a RETURN line is the operator's.
    site = conninfo_to_dict(admin) | {"user": role}
    with psycopg.connect(**site) as conn:
        assert conn.execute("SELECT count(*) FROM event").fetchone()[0] > 0
        line = conn.execute("SELECT line, reviewer_id FROM event ORDER BY seq LIMIT 1").fetchone()
    rec = json.loads(bytes(line[0]))
    for origin, seq in (("LEGACY", 400), ("RETURN", 2_000_999)):
        moved = V.encode_line({**rec, "seq": seq})
        with pytest.raises(psycopg.errors.InsufficientPrivilege), psycopg.connect(**site) as conn:
            conn.execute(
                "INSERT INTO event (seq, prev_sha256, sha256, line, reviewer_id, bundle_id,"
                " item_id, phase, at, origin, batch) VALUES (%s, %s, %s, %s, %s, %s, %s, %s,"
                " %s, %s, %s)", (seq, rec["prev_sha256"], hashlib.sha256(moved).hexdigest(),
                                 moved, line[1], rec["bundle_id"], rec["item_id"], rec["phase"],
                                 rec["at"], origin, seq))
    # Export, and restore into a second database of such an operator.
    (tmp_path / "x").mkdir(mode=0o700)
    run("export", "--out", tmp_path / "x" / "events.jsonl")
    _admin2, operator2 = operator_database(name + "b")
    monkeypatch.setenv(cli.URL_ENV, operator2)
    run("init-db")
    run("publish-bundle", "--demo", base)
    run("sync-reviewers", "--demo")
    assert run("restore", "--in", tmp_path / "x" / "events.jsonl").startswith("restored ")
    assert "verify: PASS" in run("verify")
    monkeypatch.setenv(cli.URL_ENV, operator)
    # The preview database is reset by dropping the schema and making it again.
    with psycopg.connect(operator, autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")
    run("init-db")
    assert "rights given again" in run("make-site-role", "--role", role, "--grants-only")
    assert "password changed" in run("make-site-role", "--role", role, "--out",
                                     tmp_path / "k" / "a4")
    # A refusal by the facts of the role, never by re-stating them.
    assert run("make-site-role", "--role", f"op_{name}", "--grants-only", code=2) == \
        "refused: NOT_A_SITE_ROLE\n"
    assert run("make-site-role", "--role", "nobody_here", "--grants-only", code=2) == \
        "refused: NO_SITE_ROLE\n"
    assert run("make-site-role", "--role", role, code=2) == "refused: OUT_FILE_NEEDED\n"


def test_local_lines_of_a_reviewer_without_a_row_are_accepted_only_by_their_count(
        cli, run, store, base):
    """Re-check R5 (probe ``test_r15::test_a_reviewer_of_the_local_log_without_a_row...``): a
    former remote reviewer's line in the local log, nobody of that address in the file, made
    ``parity`` fail for good, while the cutover needs PASS. Now ``import-log`` and ``parity``
    print the count (no address), and ``parity`` accepts exactly the number the operator names
    with ``--accept-unlisted-lines``; any other number fails."""
    b = A.load_bundle(base / "bundle", A.FULL)
    former = A.Session("former@example.invalid", "Former Example", A.REVIEWER, A.FULL, True, 40)
    A.append_annotation(base, b, former, **reading_kw(b, "i07", [], former, phase=A.INDEPENDENT))
    run("init-db")
    run("publish-bundle", "--demo", base)
    run("sync-reviewers", "--demo")
    assert "local lines of reviewers that no row holds: 1, of 1 sign-in spelling(s): give them " \
        "a row, or accept exactly that many with --accept-unlisted-lines 1" in run("import-log")
    assert run("parity", code=1).splitlines()[-1].endswith("FAIL, 2 reviewer(s)")
    for wrong in ("0", "2"):
        out = run("parity", "--accept-unlisted-lines", wrong, code=1)
        assert f"NOT the {wrong} named" in out
    out = run("parity", "--accept-unlisted-lines", "1")
    assert "1, of 1 sign-in spelling(s): accepted as named (--accept-unlisted-lines 1)" in out
    assert out.splitlines()[-1].endswith("PASS, 2 reviewer(s)")


def test_verify_compares_the_published_files_with_a_fresh_cut_of_the_local_bundle(
        run, store, base, dsn):
    """Re-check R6 (probe ``test_r15::test_nothing_compares_the_published_files...``): the stored
    posts and images were changed with the owning role, and ``verify`` still said PASS."""
    import psycopg

    cutover(run, store, base)
    out = run("verify")
    assert "files 4 of 4 equal, images 1 of 1 equal: PASS" in out and "verify: PASS" in out
    with psycopg.connect(dsn) as conn:  # the owning role, or anyone holding its address
        conn.execute("UPDATE media SET bytes = bytes || 'x'")
    out = run("verify", code=1)
    assert "images 0 of 1 equal: FAIL" in out and "PUBLISHED_IMAGE_DIFFERS" in out
    with psycopg.connect(dsn) as conn:
        conn.execute("UPDATE bundle SET posts = convert_to(replace(convert_from(posts, 'UTF8'),"
                     " 'a', 'e'), 'UTF8')")
    out = run("verify", code=1)
    assert "files 3 of 4 equal" in out and "PUBLISHED_FILE_DIFFERS" in out
    run("publish-bundle", "--demo", base)  # published again: equal again
    assert "verify: PASS" in run("verify")
