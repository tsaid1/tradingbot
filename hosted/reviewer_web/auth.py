"""Sign-in by an emailed one-time code, and sessions, for the hosted reviewer site.

* Only a listed reviewer who is not revoked, has the role REVIEWER and FULL access can sign in.
  Every refusal looks the same from outside: nothing says whether an address is listed.
* A code is six digits, valid ten minutes, single use, at most five tries. Only its HMAC-SHA256
  under the server's secret is stored. The newest TWO codes of a reviewer are valid (a request
  by somebody else does not end the code just delivered); using one ends both.
* Ten wrong codes for an address within 24 hours lock its sign-in: no code is accepted or sent
  until the window has passed or the operator unlocks it (``hosted_admin.py unlock``).
* A session is a 256-bit random token in a cookie; only its SHA-256 is stored. Thirty days,
  sliding. It is checked against the reviewer row on every request: a revoked reviewer's sessions
  stop working at once.
* Limits per hour: codes per address, codes per network address, tries per network address.
  Every request counts against its network address before anything else is looked at, and an
  address that is not shaped like one is never looked up and never reaches the mail server.
  The limit rows hold keyed hashes or a reviewer's row number, never an address.

Tables: ``login_code``, ``web_session``, ``rate_limit`` (WEB's section of ``schema.sql``), reached
through ``Store.connection()``. Nothing here logs or returns a code except ``request_code``, whose
caller mails it and drops it.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import unicodedata
from datetime import datetime, timedelta
from typing import NamedTuple

from psycopg.rows import tuple_row

from tradingbot.runtime import adjudication as A

from .mail import SITE_NAME

COOKIE = "__Host-session"
CODE_VALID = timedelta(minutes=10)
CODE_TRIES = 5
LIVE_CODES = 2  # the newest codes of a reviewer that are valid at once
WRONG_CODES_PER_DAY = 10  # then the address's sign-in is locked for the rest of the window
DAY = timedelta(hours=24)
CODES_PER_ADDRESS = 5  # per hour
CODES_PER_NETWORK = 20  # per hour, per network address
TRIES_PER_NETWORK = 30  # per hour, per network address
SESSION_VALID = timedelta(days=30)
SESSION_REFRESH = timedelta(hours=1)  # the sliding expiry is written at most this often
HOUR = timedelta(hours=1)
LIMIT_LOCK = 7203  # pg_advisory_xact_lock(LIMIT_LOCK, <bucket>): one counter at a time
MAX_ADDRESS = 320
SUBJECT = f"{SITE_NAME}: your sign-in code"


_ADDRESS = re.compile(r"[^@\s]{1,64}@[^@\s]{1,255}")


class Asked(NamedTuple):
    """What a request for a code came to. ``allowed``: an address shaped like one, within the
    hourly limits. ``reviewer``, ``code`` and ``code_id``: set only when a code was stored for
    a reviewer who may sign in."""

    allowed: bool
    reviewer: object = None
    code: str | None = None
    code_id: int | None = None


class Verified(NamedTuple):
    """What an entered code came to: the new session's ``token``, or None; ``exhausted``: the
    alias of the reviewer whose sign-in this wrong code has just locked (for one log line)."""

    token: str | None = None
    exhausted: str | None = None


def plausible(address: str) -> str | None:
    """The sign-in an address stands for (case-folded), or None when it is not shaped like an
    address: empty, too long, without one ``@``, or holding a space or a control character.
    Checked before anything is looked up, counted per address, or said to the mail server."""
    login = address.strip().casefold() if isinstance(address, str) else ""
    if len(login) > MAX_ADDRESS or not _ADDRESS.fullmatch(login) or any(
            unicodedata.category(c).startswith("C") for c in login):
        return None
    return login


def _mac(secret: bytes, *parts: str) -> str:
    return hmac.new(secret, "\0".join(parts).encode("utf-8"), hashlib.sha256).hexdigest()


def _sha(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def eligible(reviewer) -> bool:
    """A reviewer row that may use the site."""
    return reviewer is not None and not reviewer.revoked and reviewer.role == A.REVIEWER \
        and reviewer.access == A.FULL


def csrf_token(secret: bytes, token: str) -> str:
    """The session's CSRF token: bound to the session token, known only with the cookie."""
    return _mac(secret, "csrf", token)


def mail_text(code: str) -> str:
    """The whole message: the site's name, the code, how long it is valid. No link."""
    return f"{SITE_NAME}\n\nYour sign-in code is {code}. It is valid for 10 minutes.\n"


def _allow(cur, bucket: str, limit: int, now: datetime) -> bool:
    """Count one event in ``bucket`` unless the last hour already holds ``limit`` of them."""
    cur.execute("SELECT pg_advisory_xact_lock(%s, hashtext(%s))", (LIMIT_LOCK, bucket))
    cur.execute("SELECT count(*) FROM rate_limit WHERE bucket = %s AND at > %s",
                (bucket, now - HOUR))
    if cur.fetchone()[0] >= limit:
        return False
    cur.execute("INSERT INTO rate_limit (bucket, at) VALUES (%s, %s)", (bucket, now))
    return True


def _wrong_codes(cur, reviewer_id: int, now: datetime) -> list[datetime]:
    """The times of the wrong codes entered for a reviewer in the last 24 hours, newest first."""
    cur.execute("SELECT at FROM rate_limit WHERE bucket = %s AND at > %s ORDER BY at DESC",
                (f"wrong:r{reviewer_id}", now - DAY))
    return [at for (at,) in cur.fetchall()]


def locked_until(store, reviewer_id: int, now: datetime) -> datetime | None:
    """When the sign-in of a reviewer opens again, or None while it is not locked: 24 hours
    after the tenth-newest wrong code of the window."""
    with store.connection() as conn:
        wrong = _wrong_codes(conn.cursor(row_factory=tuple_row), reviewer_id, now)
    return wrong[WRONG_CODES_PER_DAY - 1] + DAY if len(wrong) >= WRONG_CODES_PER_DAY else None


def unlock(store, reviewer_id: int) -> int:
    """The operator clears a reviewer's sign-in counters: the wrong codes of the day and the
    hour's code requests. Returns the rows cleared."""
    with store.connection() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM rate_limit WHERE bucket = ANY(%s)",
                    ([f"wrong:r{reviewer_id}", f"to:r{reviewer_id}"],))
        return cur.rowcount


def request_code(store, secret: bytes, address: str, network: str, now: datetime) -> Asked:
    """A code is stored only for a reviewer who may sign in, within the limits and not locked;
    for everything else there is none. The caller answers the same either way. One connection,
    whoever asks: the request is counted against its network address first, then the address's
    shape is checked, and only then is anything looked up."""
    login = plausible(address)
    with store.connection() as conn:
        cur = conn.cursor(row_factory=tuple_row)
        cur.execute("DELETE FROM rate_limit WHERE at < %s", (now - DAY - HOUR,))
        cur.execute("DELETE FROM login_code WHERE expires_at < %s", (now - DAY,))
        cur.execute("DELETE FROM web_session WHERE expires_at < %s", (now,))
        allowed = _allow(cur, "net:" + _mac(secret, "net", network), CODES_PER_NETWORK, now)
        if login is None:
            return Asked(False)  # not an address: nothing is looked up, no mail server is asked
        reviewer = store.reviewer_by_login(login)
        # A listed address is counted under its row (the operator can clear it by alias), any
        # other under a keyed hash.
        bucket = f"to:r{reviewer.id}" if reviewer is not None else "to:" + _mac(secret, "to", login)
        allowed = _allow(cur, bucket, CODES_PER_ADDRESS, now) and allowed
        if not allowed or not eligible(reviewer) or len(_wrong_codes(
                cur, reviewer.id, now)) >= WRONG_CODES_PER_DAY:
            return Asked(allowed)
        code = f"{secrets.randbelow(10 ** 6):06d}"
        # The code delivered last stays valid beside the new one; older ones end.
        cur.execute("UPDATE login_code SET used_at = %s WHERE reviewer_id = %s AND used_at IS NULL"
                    " AND id NOT IN (SELECT id FROM login_code WHERE reviewer_id = %s"
                    " ORDER BY id DESC LIMIT %s)", (now, reviewer.id, reviewer.id, LIVE_CODES - 1))
        cur.execute("INSERT INTO login_code (reviewer_id, code_hmac, created_at, expires_at) "
                    "VALUES (%s, %s, %s, %s) RETURNING id",
                    (reviewer.id, _mac(secret, "code", str(reviewer.id), code), now,
                     now + CODE_VALID))
        return Asked(True, reviewer, code, cur.fetchone()[0])


def cancel_code(store, code_id: int, now: datetime) -> None:
    """End one code: a code that was never delivered cannot be tried. (A code delivered before
    it stays valid.)"""
    with store.connection() as conn:
        conn.cursor().execute("UPDATE login_code SET used_at = %s WHERE id = %s "
                              "AND used_at IS NULL", (now, code_id))


def verify_code(store, secret: bytes, address: str, code: str, network: str,
                now: datetime) -> Verified:
    """A new session's token, or none. One refusal for every reason: not an address, not
    listed, revoked, locked, no code, a wrong, expired or used code, too many tries. One
    connection, whoever asks. A wrong code that was compared with a valid one counts toward the
    day's limit of the address."""
    login = plausible(address)
    code = "".join(code.split()) if isinstance(code, str) else ""
    digits = code.isascii() and code.isdigit() and len(code) == 6
    with store.connection() as conn:
        cur = conn.cursor(row_factory=tuple_row)
        allowed = _allow(cur, "try:" + _mac(secret, "net", network), TRIES_PER_NETWORK, now)
        reviewer = store.reviewer_by_login(login) if login is not None else None
        if not allowed or not digits or not eligible(reviewer) or len(_wrong_codes(
                cur, reviewer.id, now)) >= WRONG_CODES_PER_DAY:
            hmac.compare_digest(_mac(secret, "code", "0", code[:16]), "0" * 64)
            return Verified()
        # Every try counts on each valid code, right or wrong: the newest two of the reviewer.
        cur.execute(
            "UPDATE login_code SET attempts = attempts + 1 WHERE id IN (SELECT id FROM login_code"
            " WHERE reviewer_id = %s ORDER BY id DESC LIMIT %s) AND used_at IS NULL"
            " AND expires_at > %s AND attempts < %s RETURNING id, code_hmac",
            (reviewer.id, LIVE_CODES, now, CODE_TRIES))
        valid = cur.fetchall()
        entered = _mac(secret, "code", str(reviewer.id), code)
        right = [hmac.compare_digest(stored, entered) for _id, stored in valid]  # each one
        if not any(right):
            if not valid:
                return Verified()  # nothing it could have been: not a guess at a code
            cur.execute("INSERT INTO rate_limit (bucket, at) VALUES (%s, %s)",
                        (f"wrong:r{reviewer.id}", now))
            exhausted = len(_wrong_codes(cur, reviewer.id, now)) == WRONG_CODES_PER_DAY
            return Verified(None, reviewer.alias if exhausted else None)
        cur.execute("UPDATE login_code SET used_at = %s WHERE reviewer_id = %s "
                    "AND used_at IS NULL", (now, reviewer.id))  # using one ends both
        token = secrets.token_urlsafe(32)  # 256 bits
        cur.execute("INSERT INTO web_session (token_sha256, reviewer_id, created_at, "
                    "last_seen_at, expires_at) VALUES (%s, %s, %s, %s, %s)",
                    (_sha(token), reviewer.id, now, now, now + SESSION_VALID))
        store.note_first_sign_in(reviewer.id)  # in the same transaction as the session
    return Verified(token)


def session_reviewer(store, token: str, now: datetime):
    """The reviewer of a session cookie, or None: no such session, expired, or a reviewer who may
    no longer use the site (their session is deleted)."""
    if not token or len(token) > 128:
        return None
    with store.connection() as conn:
        cur = conn.cursor(row_factory=tuple_row)
        cur.execute("SELECT reviewer_id, last_seen_at, expires_at FROM web_session "
                    "WHERE token_sha256 = %s", (_sha(token),))
        row = cur.fetchone()
        if row is None or row[2] <= now:
            return None
        if now - row[1] >= SESSION_REFRESH:
            cur.execute("UPDATE web_session SET last_seen_at = %s, expires_at = %s "
                        "WHERE token_sha256 = %s", (now, now + SESSION_VALID, _sha(token)))
    reviewer = store.reviewer_by_id(row[0])
    if not eligible(reviewer):
        end_session(store, token)
        return None
    return reviewer


def end_session(store, token: str) -> None:
    if token and len(token) <= 128:
        with store.connection() as conn:
            conn.cursor().execute("DELETE FROM web_session WHERE token_sha256 = %s",
                                  (_sha(token),))
