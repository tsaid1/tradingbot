"""Sign-in by an emailed one-time code, and sessions, for the hosted reviewer site.

* Only a listed reviewer who is not revoked, has the role REVIEWER and FULL access can sign in.
  Every refusal looks the same from outside: nothing says whether an address is listed.
* A code is six digits, valid ten minutes, single use, at most five tries. Only its HMAC-SHA256
  under the server's secret is stored. A new code replaces the earlier ones of that reviewer.
* A session is a 256-bit random token in a cookie; only its SHA-256 is stored. Thirty days,
  sliding. It is checked against the reviewer row on every request: a revoked reviewer's sessions
  stop working at once.
* Limits per hour: codes per address, codes per network address, tries per network address.
  The limit rows hold keyed hashes, never an address.

Tables: ``login_code``, ``web_session``, ``rate_limit`` (WEB's section of ``schema.sql``), reached
through ``Store.connection()``. Nothing here logs or returns a code except ``request_code``, whose
caller mails it and drops it.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta
from typing import NamedTuple

from psycopg.rows import tuple_row

from tradingbot.runtime import adjudication as A

from .mail import SITE_NAME

COOKIE = "__Host-session"
CODE_VALID = timedelta(minutes=10)
CODE_TRIES = 5
CODES_PER_ADDRESS = 5  # per hour
CODES_PER_NETWORK = 20  # per hour, per network address
TRIES_PER_NETWORK = 30  # per hour, per network address
SESSION_VALID = timedelta(days=30)
SESSION_REFRESH = timedelta(hours=1)  # the sliding expiry is written at most this often
HOUR = timedelta(hours=1)
LIMIT_LOCK = 7203  # pg_advisory_xact_lock(LIMIT_LOCK, <bucket>): one counter at a time
MAX_ADDRESS = 320
SUBJECT = f"{SITE_NAME}: your sign-in code"


class Asked(NamedTuple):
    """What a request for a code came to. ``allowed``: within the hourly limits. ``reviewer``
    and ``code``: set only when a code was stored for a reviewer who may sign in."""

    allowed: bool
    reviewer: object = None
    code: str | None = None


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


def request_code(store, secret: bytes, address: str, network: str, now: datetime) -> Asked:
    """A code is stored only for a reviewer who may sign in, within the limits; for everything
    else (not listed, revoked, another role, over a limit) there is none. The caller answers
    the same either way."""
    login = address.strip().casefold()
    if not login or len(login) > MAX_ADDRESS:
        return Asked(True)
    with store.connection() as conn:
        cur = conn.cursor(row_factory=tuple_row)
        cur.execute("DELETE FROM rate_limit WHERE at < %s", (now - 2 * HOUR,))
        cur.execute("DELETE FROM login_code WHERE expires_at < %s", (now - 24 * HOUR,))
        cur.execute("DELETE FROM web_session WHERE expires_at < %s", (now,))
        allowed = _allow(cur, "net:" + _mac(secret, "net", network), CODES_PER_NETWORK, now)
        allowed = _allow(cur, "to:" + _mac(secret, "to", login), CODES_PER_ADDRESS, now) \
            and allowed
    reviewer = store.reviewer_by_login(login)
    if not allowed or not eligible(reviewer):
        return Asked(allowed)
    code = f"{secrets.randbelow(10 ** 6):06d}"
    with store.connection() as conn:
        cur = conn.cursor(row_factory=tuple_row)
        cur.execute("UPDATE login_code SET used_at = %s WHERE reviewer_id = %s "
                    "AND used_at IS NULL", (now, reviewer.id))
        cur.execute("INSERT INTO login_code (reviewer_id, code_hmac, created_at, expires_at) "
                    "VALUES (%s, %s, %s, %s)",
                    (reviewer.id, _mac(secret, "code", str(reviewer.id), code), now,
                     now + CODE_VALID))
    return Asked(True, reviewer, code)


def verify_code(store, secret: bytes, address: str, code: str, network: str,
                now: datetime) -> str | None:
    """The token of a new session, or None. One refusal for every reason: not listed, revoked,
    no code, a wrong, expired or used code, too many tries."""
    login, code = address.strip().casefold(), "".join(code.split())
    with store.connection() as conn:
        allowed = _allow(conn.cursor(row_factory=tuple_row),
                         "try:" + _mac(secret, "net", network), TRIES_PER_NETWORK, now)
    reviewer = store.reviewer_by_login(login) if login and len(login) <= MAX_ADDRESS else None
    if not allowed or not eligible(reviewer) or not (code.isascii() and code.isdigit()
                                                     and len(code) == 6):
        hmac.compare_digest(_mac(secret, "code", "0", code[:16]), "0" * 64)
        return None
    with store.connection() as conn:
        cur = conn.cursor(row_factory=tuple_row)
        # Every try counts, right or wrong, and only the newest code of the reviewer is tried.
        cur.execute(
            "UPDATE login_code SET attempts = attempts + 1 WHERE id = (SELECT id FROM login_code "
            "WHERE reviewer_id = %s ORDER BY id DESC LIMIT 1) AND used_at IS NULL "
            "AND expires_at > %s AND attempts < %s RETURNING id, code_hmac",
            (reviewer.id, now, CODE_TRIES))
        row = cur.fetchone()
        if row is None or not hmac.compare_digest(
                row[1], _mac(secret, "code", str(reviewer.id), code)):
            return None
        cur.execute("UPDATE login_code SET used_at = %s WHERE id = %s AND used_at IS NULL",
                    (now, row[0]))
        if cur.rowcount != 1:
            return None
        token = secrets.token_urlsafe(32)  # 256 bits
        cur.execute("INSERT INTO web_session (token_sha256, reviewer_id, created_at, "
                    "last_seen_at, expires_at) VALUES (%s, %s, %s, %s, %s)",
                    (_sha(token), reviewer.id, now, now, now + SESSION_VALID))
    return token


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
