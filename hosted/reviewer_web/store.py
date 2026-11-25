"""The hosted reviewer site's store: Postgres only, psycopg 3, synchronous.

``Store`` is all the web app may call: who a reviewer is, THAT reviewer's bundle and its images,
that reviewer's own records, and the single append. No call returns another reviewer's records,
and none serves a reviewer anything of a bundle that is not theirs: the reviewers work on the
real bundle, a test identity on an invented one, in the same database.
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
# * A parsed bundle is cached by (bundle_id, published_at), not bundle_id alone: a larger scope
#   of the same local bundle is published under the same bundle_id and must replace the cache.
# * ``append`` also refuses a record whose annotator is not the reviewer or whose bundle is not
#   the reviewer's (RECORD_NOT_OF_THIS_REVIEWER_AND_BUNDLE): ``records_of`` relies on both.
# * A reviewer row is one person: ``upsert_reviewer`` refuses an alias that another sign-in holds.
# * The spelling of a sign-in is part of a reviewer's history: the core finds a reviewer's
#   records by ``annotator.login`` EXACTLY equal to the session's login (``login_shown``). So a
#   row that has events keeps its spelling (LOGIN_SPELLING_HAS_RECORDS), and no line of a
#   reviewer's own is stored under a row spelled otherwise (LINE_NOT_SPELLED_AS_ITS_ROW).
# * ``reviewer.bundle_id``: the one bundle a reviewer is served (``bundle_for``, ``is_demo``,
#   ``media(reviewer, ...)``, ``records_of``, ``append``). A test identity is served a bundle
#   only while that bundle is flagged ``is_demo`` (checked in every query, not only when it is
#   assigned). ``ReviewerRow.bundle_id`` (an added field, last, with a default) says which.
#   The names of the first contract remain: ``active_bundle()`` is the real active bundle, else
#   the invented one; ``bundle_is_demo()`` is True only while no real bundle is active;
#   ``media(post_ref, name)`` reads ``active_bundle()``.

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
MAIL_LOCK_KEY = 7_203_002  # pg_try_advisory_xact_lock: one sender of queued mail at a time
MAIL_KINDS = ("INVITE_CONTINUE", "INVITE_START")
MAIL_TRIES = 3  # of one queued message
MAIL_SENT = "SENT"
_IMAGE_TYPES = dict(zip(A._MAGIC, ("image/png", "image/jpeg", "image/gif", "image/gif"),
                        strict=True))

_REVIEWER = ("SELECT id, alias, login, login_shown, name, role, access, scope_sequences, is_test,"
             " revoked_at IS NOT NULL, bundle_id FROM reviewer")
# One bundle row; its four files travel only when this process does not hold that publication.
_BUNDLE = ("SELECT b.bundle_id, b.published_at, b.is_demo, f.manifest, f.sequences, f.items,"
           " f.posts FROM bundle b LEFT JOIN bundle f ON f.bundle_id = b.bundle_id"
           " AND (f.bundle_id, f.published_at) NOT IN"
           " (SELECT * FROM unnest(%s::text[], %s::timestamptz[]))")
# The bundle a reviewer is served: the assigned one; for a test identity only an invented one.
_THEIRS = ("(SELECT r.bundle_id FROM reviewer r JOIN bundle b ON b.bundle_id = r.bundle_id"
           " AND (b.is_demo OR NOT r.is_test) WHERE r.id = %s)")
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
    bundle_id: str | None = None  # the bundle this reviewer works on (None: not assigned yet)

    def session(self) -> A.Session:
        return A.Session(self.login_shown, self.name, self.role, self.access, True,
                         self.scope_sequences)


@dataclass(frozen=True)
class Appended:
    records: list[dict]  # the stored record(s), with schema, seq and prev_sha256
    duplicate: bool  # True: this idempotency key was already stored; nothing new was written


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def annotator_login(line: bytes) -> str | None:
    """The ``annotator.login`` of a line, exactly as spelled; None when it has none."""
    try:
        who = json.loads(line).get("annotator")
    except (ValueError, AttributeError):
        return None
    return who.get("login") if isinstance(who, dict) else None


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
        self._parsed: dict[str, tuple[datetime, A.Bundle]] = {}  # bundle_id: (published_at, it)

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

    def note_first_sign_in(self, reviewer_id: int) -> None:
        """The reviewer signed in: the time of the FIRST sign-in is kept, a later one changes
        nothing."""
        with self.connection() as conn:
            conn.execute("UPDATE reviewer SET first_sign_in_at = now() WHERE id = %s"
                         " AND first_sign_in_at IS NULL", (reviewer_id,))

    # -------------------------------------------------------------- a reviewer's bundle

    def _bundle(self, reviewer: ReviewerRow | None) -> tuple[A.Bundle, bool]:
        """(bundle, is_demo) in one query: the reviewer's bundle; without a reviewer, the real
        active bundle, else the invented one."""
        held = dict(self._parsed)
        known = [list(held), [published for published, _bundle in held.values()]]
        with self.connection() as conn:
            if reviewer is None:
                row = conn.execute(_BUNDLE + " WHERE b.active ORDER BY b.is_demo LIMIT 1",
                                   known).fetchone()
            else:
                row = conn.execute(_BUNDLE + " WHERE b.bundle_id = " + _THEIRS,
                                   [*known, reviewer.id]).fetchone()
        if row is None:
            raise A.AdjudicationError("NO_ACTIVE_BUNDLE" if reviewer is None
                                      else "NO_BUNDLE_ASSIGNED")
        if row[3] is not None:
            held[row[0]] = self._parsed[row[0]] = (
                row[1], A.bundle_from_bytes(*map(bytes, row[3:]), access=A.FULL))
        return held[row[0]][1], row[2]

    def bundle_for(self, reviewer: ReviewerRow) -> A.Bundle:
        """The bundle this reviewer works on, validated like a FULL local one (``root`` is
        None), parsed once per process and publication."""
        return self._bundle(reviewer)[0]

    def is_demo(self, reviewer: ReviewerRow) -> bool:
        """Whether this reviewer's bundle is invented demonstration data."""
        return self._bundle(reviewer)[1]

    def active_bundle(self) -> A.Bundle:
        """The real active bundle, else the invented one (the first contract's name; a page
        served to a reviewer uses ``bundle_for``)."""
        return self._bundle(None)[0]

    def bundle_is_demo(self) -> bool:
        """True only while no real bundle is active (a preview or test database)."""
        return self._bundle(None)[1]

    def media(self, *args) -> tuple[bytes, str] | None:
        """``media(reviewer, post_ref, name)``: (bytes, content type) of an image, only when
        that post of THAT REVIEWER'S bundle lists it. The first contract's
        ``media(post_ref, name)`` reads ``active_bundle()``."""
        reviewer, post_ref, name = args if len(args) == 3 else (None, *args)
        with self.connection() as conn:
            bundle = self._bundle(reviewer)[0]
            post = bundle.posts.get(post_ref)
            if post is None or name not in post["media"]:
                return None
            row = conn.execute("SELECT bytes, content_type FROM media WHERE bundle_id = %s"
                               " AND name = %s", (bundle.bundle_id, name)).fetchone()
        return (bytes(row[0]), row[1]) if row else None

    # -------------------------------------------------------------- queued mail

    def mail_next(self, skip=()) -> tuple[int, str, str] | None:
        """The oldest queued message that may be sent now, as (row, kind, the reviewer's
        address), or None: not sent yet, tries left, its reviewer not revoked, and not one of
        ``skip``. Call it inside ``connection()``: it takes the senders' lock for that
        transaction (None while another sender holds it), so no row is ever sent twice."""
        with self.connection() as conn:
            if not conn.execute("SELECT pg_try_advisory_xact_lock(%s)",
                                (MAIL_LOCK_KEY,)).fetchone()[0]:
                return None
            row = conn.execute(
                "SELECT q.id, q.kind, r.login_shown FROM mail_queue q JOIN reviewer r"
                " ON r.id = q.reviewer_id WHERE q.sent_at IS NULL AND q.attempts < %s"
                " AND r.revoked_at IS NULL AND NOT q.id = ANY(%s) ORDER BY q.id LIMIT 1",
                (MAIL_TRIES, list(skip))).fetchone()
        return tuple(row) if row else None

    def mail_done(self, row: int, result: str) -> None:
        """One try of a queued message: ``SENT`` (its time is kept, and the reviewer's
        ``invited_at`` while that is empty) or the constant code of the failure."""
        with self.connection() as conn:
            conn.execute(
                "UPDATE mail_queue SET attempts = attempts + 1, result = %s,"
                " sent_at = CASE WHEN %s THEN now() END WHERE id = %s",
                (result, result == MAIL_SENT, row))
            if result == MAIL_SENT:
                conn.execute(
                    "UPDATE reviewer SET invited_at = now() WHERE invited_at IS NULL"
                    " AND id = (SELECT reviewer_id FROM mail_queue WHERE id = %s)", (row,))

    def mail_left(self) -> int:
        """How many queued messages may still be sent."""
        with self.connection() as conn:
            return conn.execute(
                "SELECT count(*) FROM mail_queue q JOIN reviewer r ON r.id = q.reviewer_id"
                " WHERE q.sent_at IS NULL AND q.attempts < %s AND r.revoked_at IS NULL",
                (MAIL_TRIES,)).fetchone()[0]

    # -------------------------------------------------------------- one reviewer's records

    def records_of(self, reviewer: ReviewerRow) -> list[dict]:
        """ONLY this reviewer's records of this reviewer's bundle, as the log's dicts, in order:
        the imported local lines, the hosted ones, then the approver's returns of this reviewer's
        readings (APPROVAL records with decision RETURN; see the head of this file)."""
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT e.line FROM event e WHERE e.reviewer_id = %s AND e.bundle_id = " + _THEIRS
                + " ORDER BY e.seq", (reviewer.id, reviewer.id)).fetchall()
        return [json.loads(bytes(line)) for (line,) in rows]

    def append(self, reviewer: ReviewerRow, make, *,
               idempotency_key: str | None = None) -> Appended:
        """The site's only write of a reading. ``make(records)`` gets the reviewer's CURRENT
        records of the reviewer's bundle and returns one record body, a list of them, or None / []
        (nothing written), each WITHOUT schema, seq and prev_sha256; an exception from it writes
        nothing and propagates. A known ``idempotency_key`` returns what that save stored, with
        ``duplicate=True``, and does not call ``make``. A revoked reviewer: REVOKED.

        One writer at a time: the transaction first takes the advisory lock, and only then reads
        (READ COMMITTED: each statement sees what earlier writers committed)."""
        with self.connection() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK_KEY,))
            allowed, bundle_id, anchor, last_seq, last_sha = conn.execute(
                "SELECT (SELECT revoked_at IS NULL FROM reviewer WHERE id = %s), " + _THEIRS + ","
                " (SELECT snapshot_head_sha256 FROM chain_anchor WHERE id = 1),"
                " (SELECT max(seq) FROM event WHERE origin = 'HOSTED'),"
                " (SELECT sha256 FROM event WHERE origin = 'HOSTED' ORDER BY seq DESC LIMIT 1)",
                (reviewer.id, reviewer.id)).fetchone()
            if not allowed:
                raise A.AdjudicationError("REVOKED")
            if bundle_id is None:
                raise A.AdjudicationError("NO_BUNDLE_ASSIGNED")
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
                        scope_sequences: int = A.DEFAULT_SCOPE, is_test: bool = False,
                        bundle_id: str | None = None) -> ReviewerRow:
        """Insert the reviewer, or update the name, role, access, scope and bundle of the row
        that holds this alias AND this sign-in; its spelling too, but only while the row has no
        event (its records are found by that exact spelling). Anything else is refused: a row
        is one person, and its events stay theirs. Never deletes.

        ``bundle_id``: the bundle the reviewer works on; a test identity takes an invented one
        only (TEST_IDENTITY_NEEDS_A_DEMO_BUNDLE). None keeps the row's bundle; for a new row it
        is the invented active bundle (test identity), or the real active one, else the invented
        one (a preview or test database); with nothing published the row stays unassigned and is
        served nothing."""
        key = login.casefold()
        with self.connection() as conn:
            held = conn.execute(
                "SELECT alias, login, is_test, login_shown <> %s AND EXISTS (SELECT 1 FROM event e"
                " WHERE e.reviewer_id = reviewer.id) FROM reviewer WHERE alias = %s OR login = %s",
                (login, alias, key)).fetchall()
            if held and [h[:3] for h in held] != [(alias, key, is_test)]:
                raise A.AdjudicationError("ALIAS_OR_LOGIN_HELD_BY_ANOTHER_ROW")
            if held and held[0][3]:
                raise A.AdjudicationError("LOGIN_SPELLING_HAS_RECORDS")
            if bundle_id is None and not held:
                found = conn.execute(
                    "SELECT bundle_id FROM bundle WHERE active AND (is_demo OR NOT %s)"
                    " ORDER BY is_demo LIMIT 1", (is_test,)).fetchone()
                bundle_id = found and found[0]
            elif bundle_id is not None:
                demo = conn.execute("SELECT is_demo FROM bundle WHERE bundle_id = %s",
                                    (bundle_id,)).fetchone()
                if demo is None:
                    raise A.AdjudicationError("UNKNOWN_BUNDLE")
                if is_test and not demo[0]:
                    raise A.AdjudicationError("TEST_IDENTITY_NEEDS_A_DEMO_BUNDLE")
            if held:
                conn.execute(
                    "UPDATE reviewer SET login_shown = %s, name = %s, role = %s, access = %s,"
                    " scope_sequences = %s, bundle_id = COALESCE(%s, bundle_id) WHERE alias = %s",
                    (login, name, role, access, scope_sequences, bundle_id, alias))
            else:
                conn.execute(
                    "INSERT INTO reviewer (alias, login, login_shown, name, role, access,"
                    " scope_sequences, is_test, bundle_id)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (alias, key, login, name, role, access, scope_sequences, is_test, bundle_id))
            return self.reviewer_by_login(login)

    def sync_reviewers(self, reviewers: A.Reviewers, *,
                       bundle_id: str | None = None) -> list[ReviewerRow]:
        """The remote reviewers of the owner's reviewers file. An entry whose login contains
        ``@`` is REMOTE-<n>, n its place among those entries in file order (the alias the
        owner's other tools print); the REVIEWER ones are stored, with the file's scope, the
        access a remote session has by that file (RESTRICTED unless
        ``remote_full_context_authorized``) and ``bundle_id`` (as ``upsert_reviewer`` reads it).
        Never deletes."""
        out, n = [], 0
        with self.connection():
            for entry in reviewers.entries.values():
                n += "@" in entry.login
                if "@" in entry.login and entry.role == A.REVIEWER:
                    out.append(self.upsert_reviewer(
                        f"REMOTE-{n}", entry.login, name=entry.name, role=entry.role,
                        access=entry.access if reviewers.remote_full_context_authorized
                        else A.RESTRICTED, scope_sequences=reviewers.scope_sequences,
                        bundle_id=bundle_id))
        return out

    def reviewers(self) -> list[ReviewerRow]:
        with self.connection() as conn:
            return [ReviewerRow(*row)
                    for row in conn.execute(_REVIEWER + " ORDER BY id").fetchall()]

    def status(self) -> list[dict]:
        """Per reviewer row: alias, scope, bundle, the times the operator's ``status`` prints
        and the number of web sessions (the web app's ``web_session``)."""
        keys = ("alias", "is_test", "scope_sequences", "bundle_id", "invited_at",
                "first_sign_in_at", "revoked_at", "first_hosted_at", "sessions")
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT r.alias, r.is_test, r.scope_sequences, r.bundle_id, r.invited_at,"
                " r.first_sign_in_at, r.revoked_at, (SELECT min(e.at) FROM event e"
                " WHERE e.reviewer_id = r.id AND e.origin = 'HOSTED'), (SELECT count(*)"
                " FROM web_session w WHERE w.reviewer_id = r.id) FROM reviewer r ORDER BY r.id"
            ).fetchall()
        return [dict(zip(keys, row, strict=True)) for row in rows]

    def set_revoked(self, alias: str, revoked: bool) -> int:
        """Set or clear ``revoked_at`` and delete that reviewer's web sessions and sign-in codes
        not yet used (the web app's ``web_session`` and ``login_code``). Returns the sessions
        deleted."""
        with self.connection() as conn:
            row = conn.execute(
                "UPDATE reviewer SET revoked_at = CASE WHEN %s THEN COALESCE(revoked_at, now())"
                " END WHERE alias = %s RETURNING id", (revoked, alias)).fetchone()
            if row is None:
                raise A.AdjudicationError("UNKNOWN_ALIAS")
            conn.execute("DELETE FROM login_code WHERE reviewer_id = %s AND used_at IS NULL", row)
            return conn.execute("DELETE FROM web_session WHERE reviewer_id = %s", row).rowcount

    # -------------------------------------------------------------- queued mail

    def queue_mail(self, alias: str, kind: str) -> None:
        """Queue one invitation for a reviewer: the site sends it (the operator's machine holds
        no mail password). Only for a reviewer who is neither a test identity nor revoked, and
        only while no row of that kind is waiting for them."""
        if kind not in MAIL_KINDS:
            raise A.AdjudicationError("UNKNOWN_MAIL_KIND")
        with self.connection() as conn:
            row = conn.execute("SELECT id, is_test, revoked_at IS NOT NULL FROM reviewer"
                               " WHERE alias = %s FOR UPDATE", (alias,)).fetchone()
            if row is None:
                raise A.AdjudicationError("UNKNOWN_ALIAS")
            if row[1]:
                raise A.AdjudicationError("TEST_IDENTITY_IS_NOT_INVITED")
            if row[2]:
                raise A.AdjudicationError("REVOKED")
            if conn.execute("SELECT 1 FROM mail_queue WHERE reviewer_id = %s AND kind = %s"
                            " AND sent_at IS NULL AND attempts < %s",
                            (row[0], kind, MAIL_TRIES)).fetchone():
                raise A.AdjudicationError("ALREADY_QUEUED")
            conn.execute("INSERT INTO mail_queue (reviewer_id, kind) VALUES (%s, %s)",
                         (row[0], kind))

    def queued_mail(self) -> list[dict]:
        """Every queued message, oldest first: alias, kind, times, result, tries. No address."""
        keys = ("alias", "kind", "queued_at", "sent_at", "result", "attempts")
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT r.alias, q.kind, q.queued_at, q.sent_at, q.result, q.attempts"
                " FROM mail_queue q JOIN reviewer r ON r.id = q.reviewer_id ORDER BY q.id"
            ).fetchall()
        return [dict(zip(keys, row, strict=True)) for row in rows]

    # -------------------------------------------------------------- content

    def publish_bundle(self, manifest: bytes, sequences: bytes, items: bytes, posts: bytes,
                       media: dict[str, bytes], *, is_demo: bool, source_sequences: int,
                       scope_sequences: int | None = None) -> A.Bundle:
        """Validate the four files as the site will load them, store them with exactly the
        images their posts list (``media``: name -> bytes) and mark the bundle active, in one
        transaction. It becomes THE active bundle of its kind (real, or invented with
        ``is_demo``); the active bundle of the other kind stays. ``source_sequences``: the
        sequences of the local bundle; ``scope_sequences``: how many of them, in review order,
        these files carry content for (default: all). A bundle already stored under this id is
        replaced (a larger scope of the same local bundle keeps its bundle_id), but never as a
        real bundle while a test identity works on it."""
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
            if not is_demo and conn.execute(
                    "SELECT 1 FROM reviewer WHERE is_test AND bundle_id = %s LIMIT 1",
                    (bundle.bundle_id,)).fetchone():
                raise A.AdjudicationError("TEST_IDENTITIES_WORK_ON_THIS_BUNDLE")
            conn.execute("UPDATE bundle SET active = FALSE WHERE active AND is_demo = %s"
                         " AND bundle_id <> %s", (is_demo, bundle.bundle_id))
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

    def published(self, *, demo: bool) -> dict | None:
        """The active real bundle's row, or with ``demo`` the active invented one's, without
        its content: id, flags, scope and sizes."""
        keys = ("bundle_id", "is_demo", "published_at", "source_sequences", "scope_sequences",
                "bytes", "images", "image_bytes")
        with self.connection() as conn:
            row = conn.execute(
                "SELECT b.bundle_id, b.is_demo, b.published_at, b.source_sequences,"
                " b.scope_sequences, octet_length(b.manifest) + octet_length(b.sequences)"
                " + octet_length(b.items) + octet_length(b.posts), count(m.name),"
                " COALESCE(sum(octet_length(m.bytes)), 0)::bigint FROM bundle b"
                " LEFT JOIN media m ON m.bundle_id = b.bundle_id WHERE b.active"
                " AND b.is_demo = %s GROUP BY b.bundle_id", (demo,)).fetchone()
        return row and dict(zip(keys, row, strict=True))

    # -------------------------------------------------------------- events, verbatim

    def insert_events(self, lines: list[EventLine]) -> None:
        """Store lines exactly as given (the import's LEGACY lines; a restore's lines of every
        origin), under the writers' lock. A reviewer's own line (every origin but RETURN) is
        refused unless its ``annotator.login`` is EXACTLY its row's ``login_shown``: under any
        other spelling the site would not find it and the form would look open again."""
        with self.exclusive() as conn:
            spelled = dict(conn.execute("SELECT id, login_shown FROM reviewer WHERE id = ANY(%s)",
                                        ([e.reviewer_id for e in lines],)).fetchall())
            if any(e.origin != RETURN and annotator_login(e.line) != spelled.get(e.reviewer_id)
                   for e in lines):
                raise A.AdjudicationError("LINE_NOT_SPELLED_AS_ITS_ROW")
            if lines:
                conn.cursor().executemany(_INSERT_EVENT, [_event_row(e) for e in lines])

    def events(self) -> list[dict]:
        """EVERY event, in seq order, with its reviewer's alias and the row's spelling of the
        sign-in (operator tool only; the tool prints the alias, never the sign-in)."""
        keys = ("alias", "is_test", "origin", "seq", "prev_sha256", "sha256", "line",
                "idempotency_key", "batch", "created_at", "item_id", "phase", "at",
                "login_shown")
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT r.alias, r.is_test, e.origin, e.seq, e.prev_sha256, e.sha256, e.line,"
                " e.idempotency_key, e.batch, e.created_at, e.item_id, e.phase, e.at,"
                " r.login_shown FROM event e JOIN reviewer r ON r.id = e.reviewer_id"
                " ORDER BY e.seq").fetchall()
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
