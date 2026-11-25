"""The functions the hosted reviewer site shares with the local workspace (``bundle_from_bytes``,
``build_annotation``, ``build_system_shown``, ``encode_line``) and its Postgres store
(``hosted/reviewer_web/store.py``): reviewers, the published bundle and its images, one
reviewer's own records, the single chained append (one writer, idempotent), and event rows that
can never be updated or deleted. INVENTED demo data and a real embedded Postgres."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import hosted_pg
import pytest
from hosted_pg import (
    bundle_bytes,
    demo_reviewers,
    hosted_reveal,
    hosted_save,
    publish_demo,
    reading_kw,
    write_demo,
)

from tradingbot.runtime import adjudication as A
from tradingbot.runtime.vocab_recommend import GENESIS, encode_line

REVIEWER = A.Session("reviewer@example.invalid", "Reviewer Example", A.REVIEWER, A.FULL, False)
APPROVER = A.Session("owner@example.invalid", "Owner Example", A.APPROVER, A.FULL, False)
NOW = datetime(2026, 3, 9, 10, 30, tzinfo=UTC)
HOSTED_1 = 1_000_001
dsn, store = hosted_pg.dsn, hosted_pg.store  # fixtures


@pytest.fixture
def base(tmp_path) -> Path:
    return write_demo(tmp_path / "adjudication")


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def refused(code: str):
    return pytest.raises(A.AdjudicationError, match=f"^{code}$")


# ------------------------------------------------------------------ the shared core functions


@pytest.mark.parametrize("access", [A.FULL, A.RESTRICTED])
def test_bundle_from_bytes_is_load_bundle_without_the_folder(base, access):
    files, _media = bundle_bytes(base)
    posts = files[3] if access == A.FULL else None
    local = A.load_bundle(base / "bundle", access)
    assert local.root == base / "bundle"
    assert A.bundle_from_bytes(*files[:3], posts, access=access) == dataclasses.replace(
        local, root=None)


def test_bundle_from_bytes_refuses_with_the_loader_s_codes_but_checks_no_digest(base):
    files, _media = bundle_bytes(base)
    manifest = json.loads(files[0])
    rows = [json.loads(x) for x in files[2].splitlines()]
    bad = "".join(json.dumps({**r, "ai": {**r["ai"], "action": "BUY"}}) + "\n" for r in rows)
    for changed, access, code in (
            ({0: json.dumps({**manifest, "schema": "tradingbot.adjudication.bundle/9"})},
             A.FULL, "BUNDLE_SCHEMA"),
            ({0: json.dumps({**manifest, "bundle_id": "abc"})}, A.FULL, "BAD_BUNDLE_ID"),
            ({0: "{"}, A.RESTRICTED, "NOT_JSON:manifest"),
            ({2: bad}, A.RESTRICTED, "UNKNOWN_VALUE:action"),
            ({3: None}, A.FULL, "MISSING:posts.jsonl"),
            ({3: b""}, A.FULL, "UNKNOWN_POST_REF"),
            ({}, "EVERYTHING", "UNKNOWN_ACCESS")):
        given = [changed[n].encode() if isinstance(changed.get(n), str) else changed.get(n, f)
                 for n, f in enumerate(files)]
        with refused(code):
            A.bundle_from_bytes(*given, access=access)
    # The digest belongs to the folder: a part of a bundle keeps the ORIGINAL bundle_id.
    fewer = b"".join(files[2].splitlines(keepends=True)[:-1])
    part = A.bundle_from_bytes(files[0], files[1], fewer, files[3], access=A.FULL)
    assert part.bundle_id == manifest["bundle_id"] and len(part.items) == len(rows) - 1
    (base / "bundle" / "items.jsonl").write_bytes(fewer)
    with refused("BUNDLE_ID_MISMATCH"):
        A.load_bundle(base / "bundle", A.FULL)


def test_build_annotation_is_exactly_the_record_append_annotation_writes(base):
    b = A.load_bundle(base / "bundle", A.FULL)
    first = reading_kw(b, "i07", [], REVIEWER, phase=A.INDEPENDENT, confidence="MEDIUM")
    built = A.build_annotation([], b, REVIEWER, now=NOW, **first)
    assert not {"schema", "seq", "prev_sha256"} & set(built)
    stored = A.append_annotation(base, b, REVIEWER, now=NOW, **first)
    assert stored == {**built, "schema": A.ANNOTATION_SCHEMA, "seq": 1, "prev_sha256": GENESIS}
    assert A.annotations_path(base).read_bytes() == encode_line(stored) + b"\n"
    log = A.read_annotations(base).records
    after = reading_kw(b, "i07", log, REVIEWER, phase=A.AFTER_REVEAL, refers_to=1)
    built = A.build_annotation(log, b, REVIEWER, now=NOW, **after)
    stored = A.append_annotation(base, b, REVIEWER, now=NOW, **after)
    assert stored == {**built, "schema": A.ANNOTATION_SCHEMA, "seq": 2,
                      "prev_sha256": sha(encode_line(log[0]))}
    assert built["at"] == "2026-03-09T10:30:00+00:00" and built["refers_to"] == 1


def test_build_annotation_refuses_with_the_codes_of_append_annotation(base):
    b = A.load_bundle(base / "bundle", A.FULL)
    first = reading_kw(b, "i07", [], REVIEWER, phase=A.INDEPENDENT)
    A.append_annotation(base, b, REVIEWER, **first)
    log = A.read_annotations(base).records
    narrow = dataclasses.replace(REVIEWER, scope=1)
    blocked = {**first, "item_id": "i08", "reading": {}, "acknowledged_warnings": []}
    after = reading_kw(b, "i07", log, REVIEWER, phase=A.AFTER_REVEAL, refers_to=7)
    for session, kw, code in (
            (REVIEWER, first, "SYSTEM_READING_ALREADY_SEEN"),
            (REVIEWER, {**first, "item_id": "nope"}, "UNKNOWN_ITEM"),
            (narrow, {**first, "item_id": "i01"}, "OUTSIDE_SCOPE"),
            (REVIEWER, {**first, "phase": A.APPROVAL}, "ROLE_MAY_NOT_WRITE_PHASE"),
            (REVIEWER, {**first, "flags": ["NO_SUCH_FLAG"]}, "FLAGS_OR_CONFIDENCE"),
            (REVIEWER, after, "AFTER_REVEAL_REFERS_TO_OWN_INDEPENDENT"),
            (REVIEWER, blocked, None)):
        with pytest.raises(A.AdjudicationError) as pure:
            A.build_annotation(log, b, session, **kw)
        with pytest.raises(A.AdjudicationError) as local:
            A.append_annotation(base, b, session, **kw)
        assert (pure.value.code, pure.value.codes, pure.value.fields) == (
            local.value.code, local.value.codes, local.value.fields)
        assert pure.value.code == code or (code is None and pure.value.code.startswith(
            "BLOCKED:") and "REQUIRED_UNANSWERED" in pure.value.codes)
    assert len(A.read_annotations(base).records) == 1  # nothing more was written


def test_build_system_shown_gives_the_markers_mark_system_shown_writes(base):
    b = A.load_bundle(base / "bundle", A.FULL)
    assert [i["item_id"] for i in A.post_forms(b, b.items["s14a"])] == ["s14a", "s14b"]
    missing = A.build_system_shown([], b, REVIEWER, "s14b", now=NOW)
    assert [(m["item_id"], m["phase"], m["at"]) for m in missing] == [
        ("s14a", A.SYSTEM_SHOWN, NOW.isoformat()), ("s14b", A.SYSTEM_SHOWN, NOW.isoformat())]
    assert A.build_system_shown([], b, APPROVER, "s14a") == []  # writes no reading: no marker
    assert A.build_system_shown([], b, REVIEWER, "nope") == []
    one = {**missing[0], "seq": 1}
    assert [m["item_id"] for m in A.build_system_shown([one], b, REVIEWER, "s14a")] == ["s14b"]
    A.mark_system_shown(base, b, REVIEWER, "s14a")
    log = A.read_annotations(base).records
    assert [{**r, "at": NOW.isoformat()} for r in log] == [
        {**m, "schema": A.ANNOTATION_SCHEMA, "seq": n, "prev_sha256": r["prev_sha256"]}
        for n, (m, r) in enumerate(zip(missing, log, strict=True), 1)]
    assert A.build_system_shown(log, b, REVIEWER, "s14b") == []
    A.mark_system_shown(base, b, REVIEWER, "s14b")  # all exist: nothing more is written
    assert len(A.read_annotations(base).records) == 2
    other = dataclasses.replace(REVIEWER, login="remote@example.invalid")
    assert len(A.build_system_shown(log, b, other, "s14a")) == 2  # per login


# ------------------------------------------------------------------ the store: connections


def test_the_schema_applies_again_and_no_connection_prepares_a_statement(store, base):
    store.init_schema()
    store.init_schema()
    publish_demo(store, base)
    reviewer = demo_reviewers(store, base)["REMOTE-2"]
    hosted_save(store, reviewer, "i07", A.INDEPENDENT)
    with store.connection() as conn:
        assert conn.prepare_threshold is None
        assert conn.isolation_level.name == "READ_COMMITTED"
        for n in range(12):  # psycopg would prepare a repeated statement after five
            assert conn.execute("SELECT %s::int", (n,)).fetchone() == (n,)
        assert len(hosted_reveal(store, reviewer, "i07").records) == 1  # joins this connection
        hosted_save(store, reviewer, "i07", A.AFTER_REVEAL)
        assert store.records_of(reviewer)[-1]["phase"] == A.AFTER_REVEAL
        assert conn.execute("SELECT count(*) FROM pg_prepared_statements").fetchone() == (0,)


def test_a_connection_block_is_one_transaction(store, base):
    with pytest.raises(RuntimeError), store.connection():
        store.upsert_reviewer("REMOTE-1", "one@example.invalid")
        assert store.reviewer_by_login("one@example.invalid") is not None
        raise RuntimeError("the request failed")
    assert store.reviewer_by_login("one@example.invalid") is None  # rolled back
    with store.connection():
        store.upsert_reviewer("REMOTE-1", "one@example.invalid")
    assert store.reviewer_by_login("one@example.invalid").alias == "REMOTE-1"


# ------------------------------------------------------------------ reviewers


def test_reviewers_are_found_by_casefolded_login_and_a_revoked_one_says_so(store, base):
    rows = demo_reviewers(store, base)
    assert sorted(rows) == ["REMOTE-2", "REMOTE-3"]  # REMOTE-1: the approver's '@' entry, no row
    row = store.reviewer_by_login("Reviewer@EXAMPLE.invalid")
    assert row == rows["REMOTE-2"] == store.reviewer_by_id(row.id)
    assert dataclasses.asdict(row) == {
        "id": row.id, "alias": "REMOTE-2", "login": "reviewer@example.invalid",
        "login_shown": "reviewer@example.invalid", "name": "Reviewer Example",
        "role": A.REVIEWER, "access": A.FULL, "scope_sequences": 40, "is_test": False,
        "revoked": False}
    assert row.session() == A.Session("reviewer@example.invalid", "Reviewer Example", A.REVIEWER,
                                      A.FULL, True, 40)
    assert store.reviewer_by_login("mallory@example.invalid") is None
    assert store.reviewer_by_id(10_000) is None
    assert store.set_revoked("REMOTE-2", True) == 0
    assert store.reviewer_by_login("reviewer@example.invalid").revoked is True
    assert store.reviewer_by_id(row.id).revoked is True
    store.set_revoked("REMOTE-2", False)
    assert store.reviewer_by_id(row.id) == row
    with refused("UNKNOWN_ALIAS"):
        store.set_revoked("REMOTE-9", True)


def test_a_reviewer_row_is_one_person_and_follows_the_owner_s_file(store, base):
    rows = demo_reviewers(store, base)
    with refused("ALIAS_OR_LOGIN_HELD_BY_ANOTHER_ROW"):  # the alias would change hands
        store.upsert_reviewer("REMOTE-2", "someone.else@example.invalid")
    with refused("ALIAS_OR_LOGIN_HELD_BY_ANOTHER_ROW"):  # the sign-in under a second alias
        store.upsert_reviewer("REMOTE-7", "REVIEWER@example.invalid")
    again = store.upsert_reviewer("REMOTE-2", "Reviewer@Example.invalid", name="R. Example",
                                  scope_sequences=3)
    assert (again.id, again.login, again.login_shown, again.name, again.scope_sequences) == (
        rows["REMOTE-2"].id, "reviewer@example.invalid", "Reviewer@Example.invalid",
        "R. Example", 3)
    test = store.upsert_reviewer("TEST-1", "tester@example.invalid", is_test=True)
    assert test.is_test and [r.alias for r in store.reviewers()] == [
        "REMOTE-2", "REMOTE-3", "TEST-1"]
    # The owner's content switch decides what a remote session may see: off means RESTRICTED.
    closed = A.load_reviewers(write_demo(base.parent / "closed", remote_full_context=False)
                              / "reviewers.toml")
    assert {r.alias: r.access for r in store.sync_reviewers(closed)} == {
        "REMOTE-2": A.RESTRICTED, "REMOTE-3": A.RESTRICTED}
    assert len(store.reviewers()) == 3  # never deletes


# ------------------------------------------------------------------ the bundle and its images


def test_the_active_bundle_is_the_local_one_validated_and_cached(store, base):
    with refused("NO_ACTIVE_BUNDLE"):
        store.active_bundle()
    publish_demo(store, base)
    local = A.load_bundle(base / "bundle", A.FULL)
    served = store.active_bundle()
    assert served == dataclasses.replace(local, root=None) and store.bundle_is_demo() is True
    assert store.active_bundle() is served  # parsed once per process
    files, media = bundle_bytes(base)
    # A larger or smaller part of the same local bundle is published under the SAME bundle_id.
    fewer = b"".join(files[2].splitlines(keepends=True)[:-1])
    store.publish_bundle(files[0], files[1], fewer, files[3], media, is_demo=False,
                         source_sequences=7)
    assert len(store.active_bundle().items) == len(served.items) - 1
    assert store.bundle_is_demo() is False
    other = json.dumps({**json.loads(files[0]), "bundle_id": "ab" * 32}).encode()
    store.publish_bundle(other, *files[1:], media, is_demo=True, source_sequences=7)
    assert store.active_bundle().bundle_id == "ab" * 32
    with store.connection() as conn:
        assert conn.execute("SELECT bundle_id, source_sequences, scope_sequences FROM bundle"
                            " WHERE active").fetchall() == [("ab" * 32, 7, 7)]
        assert conn.execute("SELECT count(*) FROM bundle").fetchone() == (2,)


def test_an_image_is_served_only_for_a_post_of_the_active_bundle_that_lists_it(store, base):
    files, media = bundle_bytes(base)
    png = media["chart-p01.png"]
    for given, code in (({}, "MEDIA_NOT_AS_LISTED"),
                        ({**media, "other.png": png}, "MEDIA_NOT_AS_LISTED"),
                        ({"chart-p01.png": b"<svg/>"}, "MEDIA_NOT_AN_IMAGE"),
                        ({"chart-p01.png": png + bytes(4 * 1024 * 1024)}, "MEDIA_TOO_LARGE")):
        with refused(code):
            store.publish_bundle(*files, given, is_demo=True, source_sequences=1)
    with refused("NO_ACTIVE_BUNDLE"):  # a refused publication stored nothing
        store.media("p01", "chart-p01.png")
    publish_demo(store, base)
    assert store.media("p01", "chart-p01.png") == (png, "image/png")
    assert store.media("p02", "chart-p01.png") is None  # that post does not list it
    assert store.media("p01", "other.png") is None and store.media("nope", "x.png") is None
    with store.connection() as conn:
        assert conn.execute("SELECT name, post_ref, content_type, sha256 FROM media").fetchall() \
            == [("chart-p01.png", "p01", "image/png", sha(png))]
    from reviewer_web.store import image_type
    assert [image_type(d) for d in (b"\xff\xd8\xff\xe0", b"GIF89a..", b"RIFF\0\0\0\0WEBPVP8 ",
                                    b"RIFF\0\0\0\0WAVE", b"")] == [
        "image/jpeg", "image/gif", "image/webp", None, None]


# ------------------------------------------------------------------ records and the append


def test_a_reviewer_works_blind_first_through_the_store(store, base):
    bundle = publish_demo(store, base)
    reviewer = demo_reviewers(store, base)["REMOTE-2"]
    session = reviewer.session()
    assert store.records_of(reviewer) == []
    saved = hosted_save(store, reviewer, "i07", A.INDEPENDENT, confidence="MEDIUM")
    [ind] = saved.records
    assert (saved.duplicate, ind["seq"], ind["prev_sha256"], ind["schema"]) == (
        False, HOSTED_1, GENESIS, A.ANNOTATION_SCHEMA)
    assert ind["annotator"] == {"login": "reviewer@example.invalid", "name": "Reviewer Example",
                                "role": A.REVIEWER, "access": A.FULL}
    assert store.records_of(reviewer) == [ind]
    with refused("SYSTEM_READING_ALREADY_SEEN"):  # the same rule as the local log
        hosted_save(store, reviewer, "i07", A.INDEPENDENT)
    [marker] = hosted_reveal(store, reviewer, "i07").records
    assert (marker["phase"], marker["seq"], marker["prev_sha256"]) == (
        A.SYSTEM_SHOWN, HOSTED_1 + 1, sha(encode_line(ind)))
    assert hosted_reveal(store, reviewer, "i07") == type(saved)([], False)  # all exist
    [after] = hosted_save(store, reviewer, "i07", A.AFTER_REVEAL).records
    assert (after["refers_to"], after["blind"], after["seq"]) == (HOSTED_1, True, HOSTED_1 + 2)
    records = store.records_of(reviewer)
    assert records == [ind, marker, after]
    assert A.progress(bundle, records, session).status["i07"] == "DONE"
    assert A.latest_own_reading(records, "i07", bundle, session.login) == after
    with store.connection() as conn:
        rows = conn.execute("SELECT seq, prev_sha256, sha256, line, item_id, phase, at, origin,"
                            " batch, bundle_id FROM event ORDER BY seq").fetchall()
    assert [(r[0], r[1], r[2], bytes(r[3])) for r in rows] == [
        (rec["seq"], rec["prev_sha256"], sha(encode_line(rec)), encode_line(rec))
        for rec in records]
    assert [r[4:] for r in rows] == [
        (rec["item_id"], rec["phase"], datetime.fromisoformat(rec["at"]), "HOSTED", rec["seq"],
         bundle.bundle_id) for rec in records]


def test_the_first_hosted_line_continues_the_anchored_local_log(store, base):
    publish_demo(store, base)
    reviewer = demo_reviewers(store, base)["REMOTE-2"]
    store.set_anchor(12, "5a" * 32, "c3" * 32)
    assert store.anchor()["count"] == 12 and store.anchor()["file_sha256"] == "c3" * 32
    [first] = hosted_save(store, reviewer, "i07", A.INDEPENDENT).records
    assert (first["seq"], first["prev_sha256"]) == (HOSTED_1, "5a" * 32)
    with refused("ANCHOR_FIXED_BY_HOSTED_LINES"):  # that line names the head it continues
        store.set_anchor(13, "5b" * 32, "c3" * 32)
    store.set_anchor(12, "5a" * 32, "c4" * 32)
    assert store.anchor()["head"] == "5a" * 32


def test_a_save_of_several_lines_is_one_chained_batch_under_one_key(store, base):
    publish_demo(store, base)
    reviewer = demo_reviewers(store, base)["REMOTE-2"]
    bundle, session = store.active_bundle(), reviewer.session()
    calls = []

    def reveal(records):
        calls.append(len(records))
        return A.build_system_shown(records, bundle, session, "s14a", now=NOW)

    first = store.append(reviewer, reveal, idempotency_key="k-1")
    assert [(r["item_id"], r["seq"]) for r in first.records] == [
        ("s14a", HOSTED_1), ("s14b", HOSTED_1 + 1)] and not first.duplicate
    assert first.records[1]["prev_sha256"] == sha(encode_line(first.records[0]))
    again = store.append(reviewer, reveal, idempotency_key="k-1")
    assert again == type(first)(first.records, True) and calls == [0]  # make was not called
    assert [(e["seq"], e["idempotency_key"], e["batch"]) for e in store.events()] == [
        (HOSTED_1, "k-1", HOSTED_1), (HOSTED_1 + 1, None, HOSTED_1)]


def test_one_idempotency_key_stores_one_save_and_is_the_reviewer_s_own(store, base):
    publish_demo(store, base)
    rows = demo_reviewers(store, base)
    one, two = rows["REMOTE-2"], rows["REMOTE-3"]
    first = hosted_save(store, one, "i07", A.INDEPENDENT, key="k-1")
    again = hosted_save(store, one, "i07", A.INDEPENDENT, key="k-1")  # a second click, a retry
    assert again.duplicate and again.records == first.records and len(store.events()) == 1
    other = hosted_save(store, two, "i07", A.INDEPENDENT, key="k-1")  # another reviewer's key
    assert not other.duplicate and other.records[0]["seq"] == HOSTED_1 + 1
    assert other.records[0]["prev_sha256"] == sha(encode_line(first.records[0]))  # ONE chain
    assert store.records_of(one) == first.records and store.records_of(two) == other.records


def test_nothing_is_written_when_make_raises_returns_nothing_or_is_not_the_reviewer_s(
        store, base):
    bundle = publish_demo(store, base)
    rows = demo_reviewers(store, base)
    one, two = rows["REMOTE-2"], rows["REMOTE-3"]

    def fails(records):
        raise KeyError("a bug in the caller")

    with pytest.raises(KeyError):
        store.append(one, fails, idempotency_key="k-1")
    for nothing in (None, []):
        assert store.append(one, lambda records, n=nothing: n, idempotency_key="k-1").records == []
    kw = reading_kw(bundle, "i07", [], one.session(), phase=A.INDEPENDENT)
    body = A.build_annotation([], bundle, one.session(), **kw)
    with refused("RECORD_NOT_OF_THIS_REVIEWER_AND_BUNDLE"):
        store.append(two, lambda records: body)  # another reviewer's record
    with refused("RECORD_NOT_OF_THIS_REVIEWER_AND_BUNDLE"):  # the good first line goes with it
        store.append(one, lambda records: [body, {**body, "bundle_id": "ab" * 32}])
    assert store.events() == []
    assert not store.append(one, lambda records: body, idempotency_key="k-1").duplicate


def test_a_revoked_reviewer_cannot_append(store, base):
    publish_demo(store, base)
    reviewer = demo_reviewers(store, base)["REMOTE-2"]
    hosted_save(store, reviewer, "i07", A.INDEPENDENT, key="k-1")
    store.set_revoked("REMOTE-2", True)
    for key in ("k-1", "k-2", None):  # the row in hand was read before the revocation
        with refused("REVOKED"):
            store.append(reviewer, lambda records: pytest.fail("make was called"),
                         idempotency_key=key)
    assert len(store.events()) == 1 and len(store.records_of(reviewer)) == 1
    store.set_revoked("REMOTE-2", False)
    assert len(hosted_reveal(store, store.reviewer_by_id(reviewer.id), "i07").records) == 1


def test_records_of_gives_only_the_reviewer_s_own_of_the_active_bundle_legacy_first(
        store, base):
    bundle = publish_demo(store, base)
    rows = demo_reviewers(store, base)
    one, two = rows["REMOTE-2"], rows["REMOTE-3"]
    # A local log: both reviewers and (never imported) the approver.
    remote = dataclasses.replace(REVIEWER, login="remote@example.invalid")
    b = A.load_bundle(base / "bundle", A.FULL)
    for session, phase, refers_to in ((REVIEWER, A.INDEPENDENT, None),
                                      (remote, A.INDEPENDENT, None),
                                      (REVIEWER, A.AFTER_REVEAL, 1)):
        A.append_annotation(base, b, session, **reading_kw(
            b, "i07", A.read_annotations(base).records, session, phase=phase,
            refers_to=refers_to))
    A.append_annotation(base, b, APPROVER, item_id="i07", phase=A.APPROVAL, blind=False,
                        reading={}, refers_to=3, outcome="ACCEPT")
    lines = A.annotations_path(base).read_bytes().splitlines()
    from reviewer_web.store import EventLine
    store.insert_events([EventLine(one.id, "LEGACY", lines[0]),
                         EventLine(two.id, "LEGACY", lines[1]),
                         EventLine(one.id, "LEGACY", lines[2])])
    assert [e["line"] for e in store.events()] == lines[:3]  # byte for byte
    assert [(e["alias"], e["origin"], e["seq"], e["batch"]) for e in store.events()] == [
        ("REMOTE-2", "LEGACY", 1, 1), ("REMOTE-3", "LEGACY", 2, 2), ("REMOTE-2", "LEGACY", 3, 3)]
    hosted_save(store, two, "i07", A.AFTER_REVEAL)  # refers to the imported line 2
    hosted_save(store, one, "i08", A.INDEPENDENT)
    mine = store.records_of(one)
    assert [(r["seq"], r["phase"], r["item_id"]) for r in mine] == [
        (1, A.INDEPENDENT, "i07"), (3, A.AFTER_REVEAL, "i07"), (HOSTED_1 + 1, A.INDEPENDENT,
                                                                 "i08")]
    assert {r["annotator"]["login"] for r in mine} == {"reviewer@example.invalid"}
    theirs = store.records_of(two)
    assert [(r["seq"], r["refers_to"]) for r in theirs] == [(2, None), (HOSTED_1, 2)]
    assert A.progress(bundle, mine, one.session()).status["i07"] == "DONE"
    # Records of a bundle that is not the active one are not served.
    files, media = bundle_bytes(base)
    other = json.dumps({**json.loads(files[0]), "bundle_id": "ab" * 32}).encode()
    store.publish_bundle(other, *files[1:], media, is_demo=True, source_sequences=1)
    assert store.records_of(one) == [] and len(store.events()) == 5
    assert hosted_save(store, one, "i07", A.INDEPENDENT).records[0]["bundle_id"] == "ab" * 32


# ------------------------------------------------------------------ append-only, one writer


def test_an_event_row_can_never_be_updated_deleted_or_truncated(store, base):
    import psycopg

    publish_demo(store, base)
    reviewer = demo_reviewers(store, base)["REMOTE-2"]
    hosted_save(store, reviewer, "i07", A.INDEPENDENT)
    before = store.events()
    for statement in ("UPDATE event SET line = line || ' '", "UPDATE event SET at = now()",
                      "DELETE FROM event", "DELETE FROM event WHERE seq > 0",
                      "TRUNCATE event", "TRUNCATE event, chain_anchor"):
        with pytest.raises(psycopg.errors.RestrictViolation, match="EVENT_IS_APPEND_ONLY"), \
                store.connection() as conn:
            conn.execute(statement)
    assert store.events() == before and len(before) == 1


def chain_is_whole(events: list[dict], head: str = GENESIS) -> bool:
    hosted = [e for e in events if e["origin"] == "HOSTED"]
    seqs = [e["seq"] for e in hosted]
    prevs = [head] + [e["sha256"] for e in hosted[:-1]]
    return seqs == list(range(HOSTED_1, HOSTED_1 + len(hosted))) and all(
        e["prev_sha256"] == p == json.loads(e["line"])["prev_sha256"]
        and e["sha256"] == sha(e["line"]) and json.loads(e["line"])["seq"] == e["seq"]
        for e, p in zip(hosted, prevs, strict=True))


def test_two_reviewers_appending_at_once_write_one_unbroken_chain(store, base, dsn):
    from reviewer_web.store import Store

    bundle = publish_demo(store, base)
    rows = demo_reviewers(store, base)
    items = [i["item_id"] for _sid, group in A.ordered(bundle, 40) for i in group][:12]
    start = threading.Barrier(2)

    def work(reviewer):
        site, session = Store(dsn), reviewer.session()  # each web process has its own store
        start.wait()
        for item_id in items:
            site.append(reviewer, lambda records, i=item_id: A.build_system_shown(
                records, bundle, session, i))

    with ThreadPoolExecutor(2) as pool:
        for done in [pool.submit(work, r) for r in rows.values()]:
            done.result(60)  # raises what the thread raised
    events = store.events()
    assert chain_is_whole(events) and len(events) == 2 * len(
        {s for i in items for s in A.post_siblings(bundle, bundle.items[i], 40)})
    for reviewer in rows.values():
        mine = store.records_of(reviewer)
        assert {r["annotator"]["login"] for r in mine} == {reviewer.login_shown}
        assert len(mine) == len(events) // 2


def test_the_same_idempotency_key_from_two_connections_stores_one_event(store, base, dsn):
    from reviewer_web.store import Store

    bundle = publish_demo(store, base)
    reviewer = demo_reviewers(store, base)["REMOTE-2"]
    session = reviewer.session()
    kw = reading_kw(bundle, "i07", [], session, phase=A.INDEPENDENT)
    start, inside = threading.Barrier(2), threading.Event()

    def make(records):
        inside.set()
        threading.Event().wait(0.3)  # hold the lock while the other request arrives
        return A.build_annotation(records, bundle, session, **kw)

    def click():
        start.wait()
        return Store(dsn).append(reviewer, make, idempotency_key="k-1")

    with ThreadPoolExecutor(2) as pool:
        results = [done.result(60) for done in [pool.submit(click) for _ in range(2)]]
    assert inside.is_set() and sorted(r.duplicate for r in results) == [False, True]
    assert results[0].records == results[1].records and len(store.events()) == 1
    assert chain_is_whole(store.events())
