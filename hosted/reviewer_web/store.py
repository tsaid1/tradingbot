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
#   supersedes it. It is outside the hosted chain: seq 2000000 + twice its local line's seq
#   (after every record of the reviewer's own, in the local log's order), ``prev_sha256`` the
#   sha256 of the returned reading. A return that no longer stands locally is followed by ONE
#   clearing line of the same shape without content (decision WITHDRAWN, one seq above the place
#   of the local decision that came after), so that ``effective_approval`` no longer gives the
#   return. The site never writes either; ``pull-to-local`` never copies one back.
# * ``event.origin`` TEST: the lines of a test identity are a stream of their own (seq 3000000
#   + n, chained among themselves from TEST_GENESIS). A test identity may save before the real
#   log is imported: its lines never touch the reviewers' chain or its anchor, and no "is there
#   a hosted line yet" decision sees them. The table refuses a TEST line of anyone else and any
#   other line of a test identity.
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
RETURN_BASE = 2_000_000  # a RETURN line's seq: RETURN_BASE + twice its local line's seq (+ 1)
TEST_BASE = 3_000_000  # a TEST line's seq is TEST_BASE + n: the test identities' own stream
TEST_GENESIS = hashlib.sha256(b"tradingbot.hosted.test-stream/1").hexdigest()  # its first prev
LEGACY, HOSTED, RETURN, TEST = "LEGACY", "HOSTED", "RETURN", "TEST"
MAX_MEDIA_BYTES = 4 * 1024 * 1024
MAIL_KINDS = ("INVITE_CONTINUE", "INVITE_START")
MAIL_TRIES = 3  # of one queued message
MAIL_SENT = "SENT"
# A queued message that may be sent now: not sent, tries left, and no try whose outcome is
# unknown (sending began and nothing was recorded: that one is never sent again by itself).
_MAIL_DUE = ("q.sent_at IS NULL AND q.attempts < 3"
             " AND (q.sending_at IS NULL OR q.result IS NOT NULL)")
# Whom the site invites: only a row that can sign in and has something to read.
_MAIL_FOR = ("r.revoked_at IS NULL AND NOT r.is_test AND r.role = 'REVIEWER'"
             " AND r.access = 'FULL' AND r.bundle_id IS NOT NULL")
_IMAGE_TYPES = dict(zip(A._MAGIC, ("image/png", "image/jpeg", "image/gif", "image/gif"),
                        strict=True))

_REVIEWER = ("SELECT id, alias, login, login_shown, name, role, access, scope_sequences, is_test,"
             " revoked_at IS NOT NULL, bundle_id, sign_in, address FROM reviewer")
OWNER_REVIEWER = "OWNER-REVIEWER"  # the alias of the owner's own reviewer identity
# The phases the site writes: never an approval (approval, return and freeze stay local).
SITE_PHASES = (A.INDEPENDENT, A.AFTER_REVEAL, A.SYSTEM_SHOWN)
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
    sign_in: str | None = None  # casefolded: the address whose codes open this identity
    address: str | None = None  # as given: where this identity's codes and invitations go

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

    def identities_at(self, sign_in: str) -> list[ReviewerRow]:
        """Every identity whose codes go to this address (casefolded match), revoked ones too:
        the real one first, then the test identity. At most one of each kind."""
        with self.connection() as conn:
            rows = conn.execute(_REVIEWER + " WHERE sign_in = %s ORDER BY is_test, id",
                                (sign_in.casefold(),)).fetchall()
        return [ReviewerRow(*row) for row in rows]

    def role_owns_events(self) -> bool:
        """Whether the role this store connects as owns ``event`` (or has its rights): such a
        role could switch off the rule that no event is changed. The site must not be it."""
        with self.connection() as conn:
            row = conn.execute("SELECT pg_has_role(current_user, c.relowner, 'USAGE')"
                               " FROM pg_class c WHERE c.oid = to_regclass('event')").fetchone()
        return bool(row and row[0])

    def is_open(self, reviewer: ReviewerRow) -> bool:
        """Whether this identity may work here: a test identity always; a reviewer once an
        ``import-log`` has considered THAT row (``imported_at``) and the anchor exists, so that
        its local history is on the site before any hosted line continues it. A row written
        after an import is closed until the next one."""
        if reviewer.is_test:
            return True
        with self.connection() as conn:
            return conn.execute(
                "SELECT EXISTS (SELECT 1 FROM chain_anchor WHERE id = 1) AND EXISTS (SELECT 1"
                " FROM reviewer WHERE id = %s AND imported_at IS NOT NULL)",
                (reviewer.id,)).fetchone()[0]

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

    def mail_claim(self, skip=()) -> tuple[int, str, str] | None:
        """Take the oldest queued message that may be sent now, as (row, kind, the reviewer's
        address), or None. Taking it COUNTS the try and notes that sending begins, committed
        before the mail server hears of it: if the site is stopped between the send and its
        record, the row is left with an unknown outcome and is never sent again by itself. A
        message is for a reviewer who can sign in (not revoked, REVIEWER, FULL, not a test
        identity, a bundle assigned). ``skip``: rows already tried in this call."""
        with self.connection() as conn:
            row = conn.execute(
                "UPDATE mail_queue SET attempts = attempts + 1, sending_at = now(), result = NULL"
                " WHERE id = (SELECT q.id FROM mail_queue q JOIN reviewer r"
                " ON r.id = q.reviewer_id WHERE " + _MAIL_DUE + " AND " + _MAIL_FOR
                + " AND NOT q.id = ANY(%s) ORDER BY q.id LIMIT 1 FOR UPDATE OF q SKIP LOCKED)"
                " RETURNING id, kind, (SELECT address FROM reviewer"
                " WHERE id = mail_queue.reviewer_id)", (list(skip),)).fetchone()
        return tuple(row) if row else None

    def mail_done(self, row: int, result: str) -> None:
        """The outcome of the try just made: ``SENT`` (its time is kept, and the reviewer's
        ``invited_at`` while that is empty) or the constant code of the failure."""
        with self.connection() as conn:
            conn.execute(
                "UPDATE mail_queue SET result = %s, sent_at = CASE WHEN %s THEN now() END"
                " WHERE id = %s", (result, result == MAIL_SENT, row))
            if result == MAIL_SENT:
                conn.execute(
                    "UPDATE reviewer SET invited_at = now() WHERE invited_at IS NULL"
                    " AND id = (SELECT reviewer_id FROM mail_queue WHERE id = %s)", (row,))

    def mail_left(self) -> int:
        """How many queued messages may still be sent."""
        with self.connection() as conn:
            return conn.execute(
                "SELECT count(*) FROM mail_queue q JOIN reviewer r ON r.id = q.reviewer_id"
                " WHERE " + _MAIL_DUE + " AND " + _MAIL_FOR).fetchone()[0]

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

    # -------------------------------------------------------------- one reviewer's drafts

    def keep_draft(self, reviewer: ReviewerRow, item_id: str, phase: str, fields,
                   now: datetime) -> None:
        """Keep what the page sent for this form in this phase (the latest only). A draft is
        never an event."""
        with self.connection() as conn:
            conn.execute(
                "INSERT INTO draft (reviewer_id, bundle_id, item_id, phase, fields, saved_at)"
                " SELECT %s, " + _THEIRS + ", %s, %s, %s, %s"
                " ON CONFLICT (reviewer_id, bundle_id, item_id) DO UPDATE SET"
                " phase = EXCLUDED.phase, fields = EXCLUDED.fields, saved_at = EXCLUDED.saved_at",
                (reviewer.id, reviewer.id, item_id, phase,
                 json.dumps([[k, v] for k, v in fields], ensure_ascii=False), now))

    def draft_of(self, reviewer: ReviewerRow, item_id: str) -> dict | None:
        """This reviewer's draft of this form of their bundle: phase, fields, saved_at."""
        with self.connection() as conn:
            row = conn.execute("SELECT phase, fields, saved_at FROM draft WHERE reviewer_id = %s"
                               " AND bundle_id = " + _THEIRS + " AND item_id = %s",
                               (reviewer.id, reviewer.id, item_id)).fetchone()
        return row and {"phase": row[0], "fields": [tuple(f) for f in row[1]],
                        "saved_at": row[2]}

    def draft_items(self, reviewer: ReviewerRow) -> dict[str, str]:
        """{form: the phase of its draft} of this reviewer's bundle."""
        with self.connection() as conn:
            rows = conn.execute("SELECT item_id, phase FROM draft WHERE reviewer_id = %s"
                                " AND bundle_id = " + _THEIRS, (reviewer.id, reviewer.id))
            return dict(rows.fetchall())

    def drop_draft(self, reviewer: ReviewerRow, item_id: str) -> None:
        with self.connection() as conn:
            conn.execute("DELETE FROM draft WHERE reviewer_id = %s AND item_id = %s",
                         (reviewer.id, item_id))

    def append(self, reviewer: ReviewerRow, make, *,
               idempotency_key: str | None = None) -> Appended:
        """The site's only write of a reading. ``make(records)`` gets the reviewer's CURRENT
        records of the reviewer's bundle and returns one record body, a list of them, or None / []
        (nothing written), each WITHOUT schema, seq and prev_sha256; an exception from it writes
        nothing and propagates. A known ``idempotency_key`` returns what that save stored, with
        ``duplicate=True``, and does not call ``make``. A revoked reviewer: REVOKED.

        A reviewer's line continues the HOSTED chain (from the anchor on the local log); a test
        identity's line continues the TEST stream, which never touches that chain.

        One writer at a time: the transaction first takes the advisory lock, and only then reads
        (READ COMMITTED: each statement sees what earlier writers committed)."""
        with self.connection() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK_KEY,))
            allowed, test, bundle_id, anchor, last_seq, last_sha, imported = conn.execute(
                "SELECT r.revoked_at IS NULL, r.is_test, " + _THEIRS + ","
                " (SELECT snapshot_head_sha256 FROM chain_anchor WHERE id = 1), t.seq, t.sha256,"
                " r.imported_at IS NOT NULL"
                " FROM reviewer r LEFT JOIN LATERAL (SELECT e.seq, e.sha256 FROM event e"
                " WHERE e.origin = CASE WHEN r.is_test THEN 'TEST' ELSE 'HOSTED' END"
                " ORDER BY e.seq DESC LIMIT 1) t ON TRUE WHERE r.id = %s",
                (reviewer.id, reviewer.id)).fetchone() or (None,) * 7
            if not allowed:
                raise A.AdjudicationError("REVOKED")
            if bundle_id is None:
                raise A.AdjudicationError("NO_BUNDLE_ASSIGNED")
            if not test and anchor is None:
                # Nothing is imported yet: a reviewer's line now would continue no local log, and
                # no command could bring it home. A test identity's stream needs no anchor.
                raise A.AdjudicationError("NO_ANCHOR")
            if not test and not imported:
                # Written after the last import: its local lines may not be on the site yet.
                raise A.AdjudicationError("NOT_IMPORTED")
            mine = conn.execute(
                "SELECT line, bundle_id, idempotency_key, batch, seq FROM event"
                " WHERE reviewer_id = %s ORDER BY seq", (reviewer.id,)).fetchall()
            if idempotency_key is not None:
                first = next((r[4] for r in mine if r[2] == idempotency_key), None)
                if first is not None:
                    return Appended([json.loads(bytes(r[0])) for r in mine if r[3] == first], True)
            made = make([json.loads(bytes(r[0])) for r in mine if r[1] == bundle_id])
            bodies = [made] if isinstance(made, dict) else list(made or [])
            if test:
                origin, seq, prev = TEST, last_seq or TEST_BASE, last_sha or TEST_GENESIS
            else:
                origin, seq, prev = HOSTED, last_seq or HOSTED_BASE, last_sha or anchor or GENESIS
            records, rows = [], []
            for body in bodies:
                if (body.get("annotator") or {}).get("login") != reviewer.login_shown \
                        or body.get("bundle_id") != bundle_id:
                    raise A.AdjudicationError("RECORD_NOT_OF_THIS_REVIEWER_AND_BUNDLE")
                if body.get("phase") not in SITE_PHASES or (body.get("annotator") or {}).get(
                        "role") != A.REVIEWER:
                    raise A.AdjudicationError("PHASE_NOT_WRITTEN_BY_THE_SITE")
                seq += 1
                rec = {**body, "schema": A.ANNOTATION_SCHEMA, "seq": seq, "prev_sha256": prev}
                line = encode_line(rec)
                rows.append(_event_row(EventLine(
                    reviewer.id, origin, line, None if records else idempotency_key,
                    seq - len(records))))
                prev = sha256(line)
                records.append(rec)
            if rows:
                conn.cursor().executemany(_INSERT_EVENT, rows)
                # A reading saved: its form's draft ends in the same transaction.
                saved = [r["item_id"] for r in records
                         if r["phase"] in (A.INDEPENDENT, A.AFTER_REVEAL)]
                if saved:
                    conn.execute("DELETE FROM draft WHERE reviewer_id = %s AND item_id = ANY(%s)",
                                 (reviewer.id, saved))
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
                        bundle_id: str | None = None, address: str | None = None) -> ReviewerRow:
        """Insert the reviewer, or update the name, role, access, scope and bundle of the row
        that holds this alias AND this sign-in; its spelling too, but only while the row has no
        event (its records are found by that exact spelling). Anything else is refused: a row
        is one person, and its events stay theirs. Never deletes.

        ``bundle_id``: the bundle the reviewer works on; a test identity takes an invented one
        only (TEST_IDENTITY_NEEDS_A_DEMO_BUNDLE). None keeps the row's bundle; for a new row it
        is the invented active bundle (test identity), or the real active one, else the invented
        one (a preview or test database); with nothing published the row stays unassigned and is
        served nothing.

        ``address``: where this identity's codes go, when it is not the login itself (the
        owner's reviewer identity, whose login is the local identity string). An address holds
        at most one identity of each kind, a real one and a test identity
        (ADDRESS_HELD_BY_ANOTHER_IDENTITY)."""
        key = login.casefold()
        address = (address if address is not None else login).strip()
        with self.connection() as conn:
            held = conn.execute(
                "SELECT alias, login, is_test, login_shown <> %s AND EXISTS (SELECT 1 FROM event e"
                " WHERE e.reviewer_id = reviewer.id) FROM reviewer WHERE alias = %s OR login = %s",
                (login, alias, key)).fetchall()
            if held and [h[:3] for h in held] != [(alias, key, is_test)]:
                raise A.AdjudicationError("ALIAS_OR_LOGIN_HELD_BY_ANOTHER_ROW")
            if held and held[0][3]:
                raise A.AdjudicationError("LOGIN_SPELLING_HAS_RECORDS")
            if conn.execute("SELECT 1 FROM reviewer WHERE sign_in = %s AND is_test = %s"
                            " AND alias <> %s", (address.casefold(), is_test, alias)).fetchone():
                raise A.AdjudicationError("ADDRESS_HELD_BY_ANOTHER_IDENTITY")
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
                    " scope_sequences = %s, bundle_id = COALESCE(%s, bundle_id), sign_in = %s,"
                    " address = %s WHERE alias = %s",
                    (login, name, role, access, scope_sequences, bundle_id, address.casefold(),
                     address, alias))
            else:
                conn.execute(
                    "INSERT INTO reviewer (alias, login, login_shown, name, role, access,"
                    " scope_sequences, is_test, bundle_id, sign_in, address)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (alias, key, login, name, role, access, scope_sequences, is_test, bundle_id,
                     address.casefold(), address))
            return self.reviewer_by_login(login)

    def sync_reviewers(self, reviewers: A.Reviewers, *,
                       bundle_id: str | None = None,
                       skip: str | None = None) -> list[ReviewerRow]:
        """The remote reviewers of the owner's reviewers file. An entry whose login contains
        ``@`` is REMOTE-<n>, n its place among those entries in file order (the alias the
        owner's other tools print); the REVIEWER ones are stored, with the file's scope, the
        access a remote session has by that file (RESTRICTED unless
        ``remote_full_context_authorized``) and ``bundle_id`` (as ``upsert_reviewer`` reads it).
        Never deletes. ``skip``: the login of the owner's own reviewer entry (its row is
        OWNER-REVIEWER, written by the operator tool with the owner's address)."""
        out, n = [], 0
        with self.connection():
            for entry in reviewers.entries.values():
                n += "@" in entry.login
                if "@" in entry.login and entry.role == A.REVIEWER and entry.login != skip:
                    out.append(self.upsert_reviewer(
                        f"REMOTE-{n}", entry.login, name=entry.name, role=entry.role,
                        access=entry.access if reviewers.remote_full_context_authorized
                        else A.RESTRICTED, scope_sequences=reviewers.scope_sequences,
                        bundle_id=bundle_id))
        return out

    def stamp_imported(self, ids) -> None:
        """``import-log`` considered these rows: from now on they are open for saving."""
        with self.connection() as conn:
            conn.execute("UPDATE reviewer SET imported_at = COALESCE(imported_at, now())"
                         " WHERE id = ANY(%s) AND NOT is_test", (list(ids),))

    def closed_rows(self) -> list[str]:
        """The aliases of the reviewer rows (not test identities) that no import has
        considered: closed for saving until ``import-log`` runs."""
        with self.connection() as conn:
            return [a for (a,) in conn.execute(
                "SELECT alias FROM reviewer WHERE NOT is_test AND imported_at IS NULL"
                " ORDER BY id").fetchall()]

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
            # The codes and open choices of the ADDRESS end (they are kept under its key row);
            # the other identity of that address simply asks for a new code. Sessions end for
            # this identity only.
            same = ("(SELECT o.id FROM reviewer o JOIN reviewer r ON r.sign_in = o.sign_in"
                    " WHERE r.id = %s)")
            conn.execute("DELETE FROM login_code WHERE used_at IS NULL AND reviewer_id IN "
                         + same, row)
            conn.execute("DELETE FROM sign_in_choice WHERE sign_in_key IN " + same, row)
            return conn.execute("DELETE FROM web_session WHERE reviewer_id = %s", row).rowcount

    # -------------------------------------------------------------- queued mail

    def queue_mail(self, alias: str, kind: str) -> None:
        """Queue one invitation for a reviewer: the site sends it (the operator's machine holds
        no mail password). Only for a row that can sign in and has a bundle (what the sign-in
        itself requires), and only while no message of that kind waits for them."""
        if kind not in MAIL_KINDS:
            raise A.AdjudicationError("UNKNOWN_MAIL_KIND")
        with self.connection() as conn:
            row = conn.execute(
                "SELECT id, is_test, revoked_at IS NOT NULL, role = 'REVIEWER' AND access = 'FULL',"
                " bundle_id IS NOT NULL FROM reviewer WHERE alias = %s FOR UPDATE",
                (alias,)).fetchone()
            if row is None:
                raise A.AdjudicationError("UNKNOWN_ALIAS")
            for refused, code in ((row[1], "TEST_IDENTITY_IS_NOT_INVITED"), (row[2], "REVOKED"),
                                  (not row[3], "CANNOT_SIGN_IN"),
                                  (not row[4], "NO_BUNDLE_ASSIGNED")):
                if refused:
                    raise A.AdjudicationError(code)
            if conn.execute("SELECT 1 FROM mail_queue q WHERE q.reviewer_id = %s AND q.kind = %s"
                            " AND " + _MAIL_DUE, (row[0], kind)).fetchone():
                raise A.AdjudicationError("ALREADY_QUEUED")
            conn.execute("INSERT INTO mail_queue (reviewer_id, kind) VALUES (%s, %s)",
                         (row[0], kind))

    def queued_mail(self) -> list[dict]:
        """Every queued message, oldest first: alias, kind, times, result, tries. No address.
        ``sending_at`` with neither ``sent_at`` nor ``result``: a try whose outcome is unknown."""
        keys = ("alias", "kind", "queued_at", "sent_at", "result", "attempts", "sending_at")
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT r.alias, q.kind, q.queued_at, q.sent_at, q.result, q.attempts,"
                " q.sending_at FROM mail_queue q JOIN reviewer r ON r.id = q.reviewer_id"
                " ORDER BY q.id").fetchall()
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

    def published_files(self, bundle_id: str) -> tuple[list[str], dict[str, str]]:
        """(the sha256 of the four stored files, {image name: sha256 of its stored bytes}) of a
        stored bundle: what ``verify`` compares with a fresh cut of the local bundle."""
        with self.connection() as conn:
            row = conn.execute("SELECT manifest, sequences, items, posts FROM bundle"
                               " WHERE bundle_id = %s", (bundle_id,)).fetchone()
            images = conn.execute("SELECT name, bytes FROM media WHERE bundle_id = %s",
                                  (bundle_id,)).fetchall()
        return ([sha256(bytes(x)) for x in row] if row else [],
                {name: sha256(bytes(data)) for name, data in images})

    # -------------------------------------------------------------- events, verbatim

    def insert_events(self, lines: list[EventLine]) -> None:
        """Store lines exactly as given (the import's LEGACY lines; a restore's lines of every
        origin), under the writers' lock. A reviewer's own line (every origin but RETURN) is
        refused unless its ``annotator.login`` is EXACTLY its row's ``login_shown``: under any
        other spelling the site would not find it and the form would look open again. A test
        identity's lines are TEST lines, and no one else's are."""
        with self.exclusive() as conn:
            rows = {r[0]: r[1:] for r in conn.execute(
                "SELECT id, login_shown, is_test FROM reviewer WHERE id = ANY(%s)",
                ([e.reviewer_id for e in lines],)).fetchall()}
            if any((e.origin == TEST) != rows.get(e.reviewer_id, (None, None))[1] for e in lines):
                raise A.AdjudicationError("LINE_NOT_IN_ITS_ROW_S_STREAM")
            if any(e.origin != RETURN and annotator_login(e.line) != rows[e.reviewer_id][0]
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
