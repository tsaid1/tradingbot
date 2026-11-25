"""The hosted reviewer site's store: Postgres only, psycopg 3, synchronous.

``Store`` is all the web app may call: who a reviewer is, the active bundle and its images, ONE
reviewer's own records, and the single append. No call returns another reviewer's records.
``AdminStore`` adds the writes of the operator tool (``scripts/hosted_admin.py``) and of the test
fixtures; the web app never constructs it. Every refusal is ``AdjudicationError`` with a constant
code; every statement passes its values as parameters.

Connections: no pool (the app is a short-lived function behind a transaction-mode pooler). Each
call opens one connection, runs one transaction and closes it. Prepared statements are off
(``prepare_threshold=None``), the only lock is ``pg_advisory_xact_lock``, nothing depends on
session state. Inside ``with store.connection():`` every store call of that thread uses that one
connection and commits with the block (one connection per request).
"""
# Choices where the design contract left room (its names all work as written):
# * ``event.batch`` (an added column). One save may store several lines (the SYSTEM_SHOWN markers
#   of a post), but UNIQUE (reviewer_id, idempotency_key) allows the key on one row only: it is on
#   the FIRST line, and ``batch`` (that line's seq) ties the save's lines together.
# * No ``chain_anchor`` row (a demonstration database, a test): the first HOSTED line chains on
#   GENESIS, the head of an empty local log.
# * ``bundle.scope_sequences`` (an added column): how many sequences the published files carry
#   content for, so the operator tool can refuse a reviewer scope beyond it.
# * ``event.origin`` has a third value, RETURN: an approver's LOCAL decision to return a reading,
#   stored by the operator tool (``push-returns``) under the reviewer whose reading it returns,
#   so ``records_of`` serves it to that reviewer only. It is an APPROVAL record of the log's
#   shape (decision RETURN, the approver's notes; of the approver only the role), ``refers_to``
#   the seq the returned reading has HERE, so ``A.effective_approval`` / ``A._outcome`` give the
#   site what they give the local workspace, and the reviewer's next reading after the reveal
#   supersedes it. It is outside the hosted chain: seq 2000000 + its local line's seq (after
#   every record of the reviewer's own), ``prev_sha256`` the sha256 of the returned reading. The
#   site never writes one; ``pull-to-local`` never copies one back.
# * The parsed bundle is cached by (bundle_id, published_at), not bundle_id alone: a larger scope
#   of the same local bundle is published under the same bundle_id and must replace the cache.
# * ``append`` also refuses a record whose annotator is not the reviewer or whose bundle is not
#   the active one (RECORD_NOT_OF_THIS_REVIEWER_AND_BUNDLE): ``records_of`` relies on both.
# * A reviewer row is one person: ``upsert_reviewer`` refuses an alias that another sign-in holds.

from __future__ import annotations

import hashlib
import json
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

import psycopg

from tradingbot.runtime import adjudication as A
from tradingbot.runtime.vocab_recommend import GENESIS, encode_line

SCHEMA_SQL = Path(__file__).with_name("schema.sql")
LOCK_KEY = 7_203_001  # pg_advisory_xact_lock: one writer of ``event`` at a time
HOSTED_BASE = 1_000_000  # a HOSTED line's seq is HOSTED_BASE + n (n = 1, 2, ...)
RETURN_BASE = 2_000_000  # a RETURN line's seq is RETURN_BASE + the seq of its local line
LEGACY, HOSTED, RETURN = "LEGACY", "HOSTED", "RETURN"
MAX_MEDIA_BYTES = 4 * 1024 * 1024
_IMAGE_TYPES = dict(zip(A._MAGIC, ("image/png", "image/jpeg", "image/gif", "image/gif"),
                        strict=True))

_REVIEWER = ("SELECT id, alias, login, login_shown, name, role, access, scope_sequences, is_test,"
             " revoked_at IS NOT NULL FROM reviewer")
_INSERT_EVENT = (
    "INSERT INTO event (seq, prev_sha256, sha256, line, reviewer_id, bundle_id, item_id, phase,"
    " at, origin, idempotency_key, batch, created_at)"
    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, COALESCE(%s, now()))")


@dataclass(frozen=True)
class ReviewerRow:
    id: int
    alias: str
    login: str  # casefolded
    login_shown: str  # as the reviewers file spells it: what the reviewer's records carry
    name: str
    role: str
    access: str
    scope_sequences: int
    is_test: bool
    revoked: bool

    def session(self) -> A.Session:
        return A.Session(self.login_shown, self.name, self.role, self.access, True,
                         self.scope_sequences)


@dataclass(frozen=True)
class Appended:
    records: list[dict]  # the stored record(s), with schema, seq and prev_sha256
    duplicate: bool  # True: this idempotency key was already stored; nothing new was written


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def image_type(data: bytes) -> str | None:
    """The content type of an image the workspace serves (PNG, JPEG, GIF, WEBP), else None."""
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return next((t for magic, t in _IMAGE_TYPES.items() if data.startswith(magic)), None)


class EventLine(NamedTuple):
    """One line to store exactly as given (``AdminStore.insert_events``)."""
    reviewer_id: int
    origin: str
    line: bytes
    idempotency_key: str | None = None
    batch: int | None = None  # None: the line's own seq
    created_at: datetime | None = None  # None: now


def _event_row(e: EventLine) -> tuple:
    """The ``event`` row of one line: everything but its owner, origin and save is read from the
    line's own bytes."""
    rec = json.loads(e.line)
    return (rec["seq"], rec["prev_sha256"], sha256(e.line), e.line, e.reviewer_id,
            rec["bundle_id"], rec["item_id"], rec["phase"], datetime.fromisoformat(rec["at"]),
            e.origin, e.idempotency_key, rec["seq"] if e.batch is None else e.batch, e.created_at)


class Store:
    def __init__(self, dsn: str):
        self._dsn = dsn
        self._open = threading.local()
        self._cached: tuple[tuple, A.Bundle] | None = None  # ((bundle_id, published_at), bundle)

    @contextmanager
    def connection(self):
        """A psycopg connection in ONE transaction: committed when the block ends, rolled back
        when it raises, then closed. Store calls made inside the block (same thread) join it."""
        conn = getattr(self._open, "conn", None)
        if conn is not None:
            yield conn
            return
        with psycopg.connect(self._dsn, prepare_threshold=None) as conn:
            conn.isolation_level = psycopg.IsolationLevel.READ_COMMITTED  # see ``append``
            self._open.conn = conn
            try:
                yield conn
            finally:
                self._open.conn = None

    # -------------------------------------------------------------- reviewers

    def reviewer_by_login(self, login: str) -> ReviewerRow | None:
        """Casefolded match. A revoked row is returned, with ``revoked=True``."""
        with self.connection() as conn:
            row = conn.execute(_REVIEWER + " WHERE login = %s", (login.casefold(),)).fetchone()
        return ReviewerRow(*row) if row else None

    def reviewer_by_id(self, reviewer_id: int) -> ReviewerRow | None:
        with self.connection() as conn:
            row = conn.execute(_REVIEWER + " WHERE id = %s", (reviewer_id,)).fetchone()
        return ReviewerRow(*row) if row else None

    # -------------------------------------------------------------- the active bundle

    def _active(self) -> tuple[A.Bundle, bool]:
        """(bundle, is_demo) in one query: the four files travel only when the cache is stale."""
        cached = self._cached
        with self.connection() as conn:
            row = conn.execute(
                "SELECT b.bundle_id, b.published_at, b.is_demo, f.manifest, f.sequences, f.items,"
                " f.posts FROM bundle b LEFT JOIN bundle f ON f.bundle_id = b.bundle_id"
                " AND NOT (f.bundle_id = %s AND f.published_at = %s) WHERE b.active",
                cached[0] if cached else ("", None)).fetchone()
        if row is None:
            raise A.AdjudicationError("NO_ACTIVE_BUNDLE")
        if row[3] is not None:
            cached = self._cached = (
                row[:2], A.bundle_from_bytes(*map(bytes, row[3:]), access=A.FULL))
        return cached[1], row[2]

    def active_bundle(self) -> A.Bundle:
        """The published bundle, validated like a FULL local one (``root`` is None)."""
        return self._active()[0]

    def bundle_is_demo(self) -> bool:
        return self._active()[1]

    def media(self, post_ref: str, name: str) -> tuple[bytes, str] | None:
        """(bytes, content type) of an image, only when that post of the ACTIVE bundle lists it."""
        with self.connection() as conn:
            bundle = self.active_bundle()
            post = bundle.posts.get(post_ref)
            if post is None or name not in post["media"]:
                return None
            row = conn.execute("SELECT bytes, content_type FROM media WHERE bundle_id = %s"
                               " AND name = %s", (bundle.bundle_id, name)).fetchone()
        return (bytes(row[0]), row[1]) if row else None

    # -------------------------------------------------------------- one reviewer's records

    def records_of(self, reviewer: ReviewerRow) -> list[dict]:
        """ONLY this reviewer's records of the active bundle, as the log's dicts, in order: the
        imported local lines, the hosted ones, then the approver's returns of this reviewer's
        readings (APPROVAL records with decision RETURN; see the head of this file)."""
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT e.line FROM event e JOIN bundle b ON b.bundle_id = e.bundle_id AND b.active"
                " WHERE e.reviewer_id = %s ORDER BY e.seq", (reviewer.id,)).fetchall()
        return [json.loads(bytes(line)) for (line,) in rows]

    def append(self, reviewer: ReviewerRow, make, *,
               idempotency_key: str | None = None) -> Appended:
        """The site's only write of a reading. ``make(records)`` gets the reviewer's CURRENT
        records of the active bundle and returns one record body, a list of them, or None / []
        (nothing written), each WITHOUT schema, seq and prev_sha256; an exception from it writes
        nothing and propagates. A known ``idempotency_key`` returns what that save stored, with
        ``duplicate=True``, and does not call ``make``. A revoked reviewer: REVOKED.

        One writer at a time: the transaction first takes the advisory lock, and only then reads
        (READ COMMITTED: each statement sees what earlier writers committed)."""
        with self.connection() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK_KEY,))
            allowed, bundle_id, anchor, last_seq, last_sha = conn.execute(
                "SELECT (SELECT revoked_at IS NULL FROM reviewer WHERE id = %s),"
                " (SELECT bundle_id FROM bundle WHERE active),"
                " (SELECT snapshot_head_sha256 FROM chain_anchor WHERE id = 1),"
                " (SELECT max(seq) FROM event WHERE origin = 'HOSTED'),"
                " (SELECT sha256 FROM event WHERE origin = 'HOSTED' ORDER BY seq DESC LIMIT 1)",
                (reviewer.id,)).fetchone()
            if not allowed:
                raise A.AdjudicationError("REVOKED")
            if bundle_id is None:
                raise A.AdjudicationError("NO_ACTIVE_BUNDLE")
            mine = conn.execute(
                "SELECT line, bundle_id, idempotency_key, batch, seq FROM event"
                " WHERE reviewer_id = %s ORDER BY seq", (reviewer.id,)).fetchall()
            if idempotency_key is not None:
                first = next((r[4] for r in mine if r[2] == idempotency_key), None)
                if first is not None:
                    return Appended([json.loads(bytes(r[0])) for r in mine if r[3] == first], True)
            made = make([json.loads(bytes(r[0])) for r in mine if r[1] == bundle_id])
            bodies = [made] if isinstance(made, dict) else list(made or [])
            seq, prev = last_seq or HOSTED_BASE, last_sha or anchor or GENESIS
            records, rows = [], []
            for body in bodies:
                if (body.get("annotator") or {}).get("login") != reviewer.login_shown \
                        or body.get("bundle_id") != bundle_id:
                    raise A.AdjudicationError("RECORD_NOT_OF_THIS_REVIEWER_AND_BUNDLE")
                seq += 1
                rec = {**body, "schema": A.ANNOTATION_SCHEMA, "seq": seq, "prev_sha256": prev}
                line = encode_line(rec)
                rows.append(_event_row(EventLine(
                    reviewer.id, HOSTED, line, None if records else idempotency_key,
                    seq - len(records))))
                prev = sha256(line)
                records.append(rec)
            if rows:
                conn.cursor().executemany(_INSERT_EVENT, rows)
        return Appended(records, False)


# ---------------------------------------------------------------------------------------------
# The operator's side. Nothing below is used by the web app.
# ---------------------------------------------------------------------------------------------


class AdminStore(Store):
    """The writes of the local operator tool and of the test fixtures, and the reads that span
    reviewers (export, verification, status). Never constructed by the web app."""

    @contextmanager
    def exclusive(self):
        """``connection()`` holding the writers' lock from here to its end: no save lands in
        between."""
        with self.connection() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK_KEY,))
            yield conn

    def init_schema(self) -> None:
        """Apply ``schema.sql`` (every statement can be run again)."""
        with self.exclusive() as conn:
            conn.execute(SCHEMA_SQL.read_text(encoding="utf-8"))

    # -------------------------------------------------------------- reviewers

    def upsert_reviewer(self, alias: str, login: str, *, name: str = "",
                        role: str = A.REVIEWER, access: str = A.FULL,
                        scope_sequences: int = A.DEFAULT_SCOPE,
                        is_test: bool = False) -> ReviewerRow:
        """Insert the reviewer, or update the spelling, name, role, access and scope of the row
        that holds this alias AND this sign-in. Anything else is refused: a row is one person,
        and its events stay theirs. Never deletes."""
        key = login.casefold()
        with self.connection() as conn:
            held = conn.execute("SELECT alias, login, is_test FROM reviewer WHERE alias = %s"
                                " OR login = %s", (alias, key)).fetchall()
            if held and held != [(alias, key, is_test)]:
                raise A.AdjudicationError("ALIAS_OR_LOGIN_HELD_BY_ANOTHER_ROW")
            if held:
                conn.execute(
                    "UPDATE reviewer SET login_shown = %s, name = %s, role = %s, access = %s,"
                    " scope_sequences = %s WHERE alias = %s",
                    (login, name, role, access, scope_sequences, alias))
            else:
                conn.execute(
                    "INSERT INTO reviewer (alias, login, login_shown, name, role, access,"
                    " scope_sequences, is_test) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                    (alias, key, login, name, role, access, scope_sequences, is_test))
            return self.reviewer_by_login(login)

    def sync_reviewers(self, reviewers: A.Reviewers) -> list[ReviewerRow]:
        """The remote reviewers of the owner's reviewers file. An entry whose login contains
        ``@`` is REMOTE-<n>, n its place among those entries in file order (the alias the
        owner's other tools print); the REVIEWER ones are stored, with the file's scope and the
        access a remote session has by that file (RESTRICTED unless
        ``remote_full_context_authorized``). Never deletes."""
        out, n = [], 0
        with self.connection():
            for entry in reviewers.entries.values():
                n += "@" in entry.login
                if "@" in entry.login and entry.role == A.REVIEWER:
                    out.append(self.upsert_reviewer(
                        f"REMOTE-{n}", entry.login, name=entry.name, role=entry.role,
                        access=entry.access if reviewers.remote_full_context_authorized
                        else A.RESTRICTED, scope_sequences=reviewers.scope_sequences))
        return out

    def reviewers(self) -> list[ReviewerRow]:
        with self.connection() as conn:
            return [ReviewerRow(*row)
                    for row in conn.execute(_REVIEWER + " ORDER BY id").fetchall()]

    def status(self) -> list[dict]:
        """Per reviewer row: alias, scope and the times the operator's ``status`` prints."""
        keys = ("alias", "is_test", "scope_sequences", "invited_at", "first_sign_in_at",
                "revoked_at", "first_hosted_at")
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT r.alias, r.is_test, r.scope_sequences, r.invited_at, r.first_sign_in_at,"
                " r.revoked_at, (SELECT min(e.at) FROM event e WHERE e.reviewer_id = r.id"
                " AND e.origin = 'HOSTED') FROM reviewer r ORDER BY r.id").fetchall()
        return [dict(zip(keys, row, strict=True)) for row in rows]

    def set_revoked(self, alias: str, revoked: bool) -> int:
        """Set or clear ``revoked_at`` and delete that reviewer's web sessions (WEB's table
        ``web_session``, by ``reviewer_id``, once it exists). Returns the sessions deleted."""
        with self.connection() as conn:
            row = conn.execute(
                "UPDATE reviewer SET revoked_at = CASE WHEN %s THEN COALESCE(revoked_at, now())"
                " END WHERE alias = %s RETURNING id", (revoked, alias)).fetchone()
            if row is None:
                raise A.AdjudicationError("UNKNOWN_ALIAS")
            if conn.execute("SELECT to_regclass('web_session')").fetchone()[0] is None:
                return 0
            return conn.execute("DELETE FROM web_session WHERE reviewer_id = %s",
                                (row[0],)).rowcount

    # -------------------------------------------------------------- content

    def publish_bundle(self, manifest: bytes, sequences: bytes, items: bytes, posts: bytes,
                       media: dict[str, bytes], *, is_demo: bool, source_sequences: int,
                       scope_sequences: int | None = None) -> A.Bundle:
        """Validate the four files as the site will load them, store them with exactly the
        images their posts list (``media``: name -> bytes) and mark the bundle active, in one
        transaction. ``source_sequences``: the sequences of the local bundle; ``scope_sequences``:
        how many of them, in review order, these files carry content for (default: all). A
        bundle already stored under this id is replaced (a larger scope of the same local bundle
        keeps its bundle_id)."""
        bundle = A.bundle_from_bytes(manifest, sequences, items, posts, access=A.FULL)
        listed: dict[str, str] = {}
        for ref, post in bundle.posts.items():
            for name in post["media"]:
                listed.setdefault(name, ref)
        if set(listed) != set(media):
            raise A.AdjudicationError("MEDIA_NOT_AS_LISTED")
        rows = []
        for name, ref in listed.items():
            if len(media[name]) > MAX_MEDIA_BYTES:
                raise A.AdjudicationError("MEDIA_TOO_LARGE")
            kind = image_type(media[name])
            if kind is None:
                raise A.AdjudicationError("MEDIA_NOT_AN_IMAGE")
            rows.append((bundle.bundle_id, name, ref, kind, media[name], sha256(media[name])))
        with self.connection() as conn:
            conn.execute("UPDATE bundle SET active = FALSE WHERE active")
            conn.execute(
                "INSERT INTO bundle (bundle_id, manifest, sequences, items, posts, is_demo,"
                " source_sequences, scope_sequences, published_at, active)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, clock_timestamp(), TRUE)"
                " ON CONFLICT (bundle_id) DO UPDATE SET manifest = EXCLUDED.manifest,"
                " sequences = EXCLUDED.sequences, items = EXCLUDED.items, posts = EXCLUDED.posts,"
                " is_demo = EXCLUDED.is_demo, source_sequences = EXCLUDED.source_sequences,"
                " scope_sequences = EXCLUDED.scope_sequences,"
                " published_at = EXCLUDED.published_at, active = TRUE",
                (bundle.bundle_id, manifest, sequences, items, posts, is_demo, source_sequences,
                 source_sequences if scope_sequences is None else scope_sequences))
            conn.execute("DELETE FROM media WHERE bundle_id = %s", (bundle.bundle_id,))
            if rows:
                conn.cursor().executemany(
                    "INSERT INTO media (bundle_id, name, post_ref, content_type, bytes, sha256)"
                    " VALUES (%s, %s, %s, %s, %s, %s)", rows)
        return bundle

    def published(self) -> dict | None:
        """The active bundle's row without its content: id, flags, scope and sizes."""
        keys = ("bundle_id", "is_demo", "published_at", "source_sequences", "scope_sequences",
                "bytes", "images", "image_bytes")
        with self.connection() as conn:
            row = conn.execute(
                "SELECT b.bundle_id, b.is_demo, b.published_at, b.source_sequences,"
                " b.scope_sequences, octet_length(b.manifest) + octet_length(b.sequences)"
                " + octet_length(b.items) + octet_length(b.posts), count(m.name),"
                " COALESCE(sum(octet_length(m.bytes)), 0)::bigint FROM bundle b"
                " LEFT JOIN media m ON m.bundle_id = b.bundle_id WHERE b.active"
                " GROUP BY b.bundle_id").fetchone()
        return row and dict(zip(keys, row, strict=True))

    # -------------------------------------------------------------- events, verbatim

    def insert_events(self, lines: list[EventLine]) -> None:
        """Store lines exactly as given (the import's LEGACY lines; a restore's lines of every
        origin), under the writers' lock. Nothing is checked here but the table's own rules."""
        with self.exclusive() as conn:
            if lines:
                conn.cursor().executemany(_INSERT_EVENT, [_event_row(e) for e in lines])

    def events(self) -> list[dict]:
        """EVERY event, in seq order, with its reviewer's alias (operator tool only)."""
        keys = ("alias", "is_test", "origin", "seq", "prev_sha256", "sha256", "line",
                "idempotency_key", "batch", "created_at", "item_id", "phase", "at")
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT r.alias, r.is_test, e.origin, e.seq, e.prev_sha256, e.sha256, e.line,"
                " e.idempotency_key, e.batch, e.created_at, e.item_id, e.phase, e.at FROM event e"
                " JOIN reviewer r ON r.id = e.reviewer_id ORDER BY e.seq").fetchall()
        return [{**dict(zip(keys, row, strict=True)), "line": bytes(row[6])} for row in rows]

    def anchor(self) -> dict | None:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT snapshot_count, snapshot_head_sha256, snapshot_file_sha256, imported_at"
                " FROM chain_anchor WHERE id = 1").fetchone()
        return row and dict(zip(("count", "head", "file_sha256", "imported_at"), row,
                                strict=True))

    def set_anchor(self, count: int, head: str, file_sha256: str,
                   imported_at: datetime | None = None) -> None:
        """Record the local log's state the hosted chain continues. Once a HOSTED line exists
        its head is fixed (the first HOSTED line names it): a different head is refused."""
        with self.exclusive() as conn:
            fixed, hosted = conn.execute(
                "SELECT (SELECT snapshot_head_sha256 FROM chain_anchor WHERE id = 1),"
                " EXISTS (SELECT 1 FROM event WHERE origin = 'HOSTED')").fetchone()
            if hosted and (fixed or GENESIS) != head:
                raise A.AdjudicationError("ANCHOR_FIXED_BY_HOSTED_LINES")
            conn.execute(
                "INSERT INTO chain_anchor (id, snapshot_count, snapshot_head_sha256,"
                " snapshot_file_sha256, imported_at) VALUES (1, %s, %s, %s, COALESCE(%s, now()))"
                " ON CONFLICT (id) DO UPDATE SET snapshot_count = EXCLUDED.snapshot_count,"
                " snapshot_head_sha256 = EXCLUDED.snapshot_head_sha256,"
                " snapshot_file_sha256 = EXCLUDED.snapshot_file_sha256,"
                " imported_at = EXCLUDED.imported_at", (count, head, file_sha256, imported_at))
