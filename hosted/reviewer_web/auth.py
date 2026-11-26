"""Sign-in by an emailed one-time code, and sessions, for the hosted reviewer site.

* Only a listed reviewer who is not revoked, has the role REVIEWER and FULL access can sign in.
  Every refusal looks the same from outside: nothing says whether an address is listed.
* A code is six digits, valid ten minutes, single use, at most five tries. Only its HMAC-SHA256
  under the server's secret is stored. The newest TWO codes of a reviewer are valid (a request
  by somebody else does not end the code just delivered); using one ends both.
* Ten wrong codes for an address within 24 hours lock its sign-in: no code is accepted or sent
  until the window has passed or the operator unlocks it (``hosted_admin.py unlock``).
* A session is a 256-bit random token in a cookie; only its SHA-256 is stored. Thirty days
  after its last use (the cookie is sent again whenever the stored expiry moves), and at most
  ninety days after the sign-in: then a new code is needed. It is checked against the reviewer
  row on every request: a revoked reviewer's sessions stop working at once.
* One address may open TWO identities (the owner's real reviewer identity and a test identity).
  Its codes and its counters belong to the ADDRESS (kept under its key row: the lowest id of
  its rows), so the wrong-code lock, the hourly quota and ``unlock`` act on the address. After a
  right code such an address chooses which identity to open (``choices``, ``choose``): a
  choice is made only by a right code and holds the address's key row and its times (not the
  code), single use, five minutes, held by the browser in a
  cookie of its own. A session belongs to exactly one identity.
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
CHOICE_COOKIE = "__Host-choice"
BROWSER_COOKIE = "__Host-signin"  # set with a code request; names that request's browser
BROWSER_VALID = timedelta(minutes=11)  # the cookie's life: the code's ten minutes and a little
REPEAT_WINDOW = timedelta(seconds=60)  # a used code may be repeated by its own browser this long
CHOICE_VALID = timedelta(minutes=5)
REAL, TEST = "review", "test"  # what a choice names: the address's real identity, or its test one
CODE_VALID = timedelta(minutes=10)
CODE_TRIES = 5
LIVE_CODES = 2  # the newest codes of a reviewer that are valid at once
WRONG_CODES_PER_DAY = 10  # then the address's sign-in is locked for the rest of the window
DAY = timedelta(hours=24)
CODES_PER_ADDRESS = 5  # per hour
CODES_PER_NETWORK = 20  # per hour, per network address
TRIES_PER_NETWORK = 30  # per hour, per network address
SESSION_VALID = timedelta(days=30)  # after the last use
SESSION_LIMIT = timedelta(days=90)  # after the sign-in, however it is used
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
    identities: tuple = ()  # every identity of the address that may sign in


class Verified(NamedTuple):
    """What an entered code came to: the new session's ``token``, or None; ``exhausted``: the
    alias of the reviewer whose sign-in this wrong code has just locked (for one log line)."""

    token: str | None = None
    exhausted: str | None = None
    choice: str | None = None  # the token of the choice between two identities, instead


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


class Address(NamedTuple):
    """The identities an address opens: ``key`` is the row its codes and counters are kept
    under (None: no row has this address), ``open`` the identities that may sign in now."""

    key: object = None
    open: tuple = ()


def address_of(store, login: str | None) -> Address:
    rows = store.identities_at(login) if login is not None else []
    if not rows:
        return Address()
    return Address(min(rows, key=lambda r: r.id), tuple(r for r in rows if eligible(r)))


def key_of(store, reviewer) -> int:
    """The key row of a reviewer's address: where its codes and counters are kept."""
    return min(r.id for r in store.identities_at(reviewer.sign_in or reviewer.login))


def kind(reviewer) -> str:
    return TEST if reviewer.is_test else REAL


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


def request_code(store, secret: bytes, address: str, network: str, now: datetime,
                 browser: str = "") -> Asked:
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
        found = address_of(store, login)
        key = found.key
        # A listed address is counted under its key row (the operator can clear it by alias),
        # any other under a keyed hash.
        bucket = f"to:r{key.id}" if key is not None else "to:" + _mac(secret, "to", login)
        allowed = _allow(cur, bucket, CODES_PER_ADDRESS, now) and allowed
        if not allowed or not found.open or len(_wrong_codes(
                cur, key.id, now)) >= WRONG_CODES_PER_DAY:
            return Asked(allowed)
        code = f"{secrets.randbelow(10 ** 6):06d}"
        # The code delivered last stays valid beside the new one; older ones end.
        cur.execute("UPDATE login_code SET used_at = %s WHERE reviewer_id = %s AND used_at IS NULL"
                    " AND id NOT IN (SELECT id FROM login_code WHERE reviewer_id = %s"
                    " ORDER BY id DESC LIMIT %s)", (now, key.id, key.id, LIVE_CODES - 1))
        cur.execute("INSERT INTO login_code (reviewer_id, code_hmac, created_at, expires_at,"
                    " browser_sha256) VALUES (%s, %s, %s, %s, %s) RETURNING id",
                    (key.id, _mac(secret, "code", str(key.id), code), now, now + CODE_VALID,
                     _sha(browser) if browser else None))
        return Asked(True, found.open[0], code, cur.fetchone()[0], found.open)


def cancel_code(store, code_id: int, now: datetime) -> None:
    """End one code: a code that was never delivered cannot be tried. (A code delivered before
    it stays valid.)"""
    with store.connection() as conn:
        conn.cursor().execute("UPDATE login_code SET used_at = %s WHERE id = %s "
                              "AND used_at IS NULL", (now, code_id))


def verify_code(store, secret: bytes, address: str, code: str, network: str,
                now: datetime, browser: str = "") -> Verified:
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
        found = address_of(store, login)
        key = found.key
        if not allowed or not digits or not found.open or len(_wrong_codes(
                cur, key.id, now)) >= WRONG_CODES_PER_DAY:
            hmac.compare_digest(_mac(secret, "code", "0", code[:16]), "0" * 64)
            return Verified()
        # Every try counts on each valid code, right or wrong: the newest two of the address.
        cur.execute(
            "UPDATE login_code SET attempts = attempts + 1 WHERE id IN (SELECT id FROM login_code"
            " WHERE reviewer_id = %s ORDER BY id DESC LIMIT %s) AND used_at IS NULL"
            " AND expires_at > %s AND attempts < %s RETURNING id, code_hmac",
            (key.id, LIVE_CODES, now, CODE_TRIES))
        valid = cur.fetchall()
        entered = _mac(secret, "code", str(key.id), code)
        right = [hmac.compare_digest(stored, entered) for _id, stored in valid]  # each one
        if not any(right):
            if not valid:
                # A code that signed in a moment ago: its own browser may repeat that sign-in
                # (the first answer was dropped); any wrong code here is counted as any other.
                return _repeat(cur, store, secret, found, code, browser, now) or Verified()
            cur.execute("INSERT INTO rate_limit (bucket, at) VALUES (%s, %s)",
                        (f"wrong:r{key.id}", now))
            exhausted = len(_wrong_codes(cur, key.id, now)) == WRONG_CODES_PER_DAY
            return Verified(None, "+".join(r.alias for r in found.open) if exhausted else None)
        cur.execute("UPDATE login_code SET used_at = %s WHERE reviewer_id = %s "
                    "AND used_at IS NULL", (now, key.id))  # using one ends both
        used = next(i for (i, _stored), ok in zip(valid, right, strict=True) if ok)
        # The repeat belongs to the browser of THIS verify, and only when it is the browser
        # that asked for the code: otherwise nobody may ever repeat this code.
        cur.execute("UPDATE login_code SET signed_in_at = %s, browser_sha256 = CASE WHEN"
                    " browser_sha256 = %s THEN browser_sha256 END WHERE id = %s",
                    (now, _sha(browser) if browser else "-", used))
        return _signed_in(cur, store, found, used, now)


def _signed_in(cur, store, found: Address, code_id: int, now: datetime) -> Verified:
    """A new session (or, for an address with two identities, a new choice) made by this code.
    The sessions one code made all stay live until one of them is used (``session_reviewer``
    then ends the others): whichever answer the browser kept, it works."""
    token = secrets.token_urlsafe(32)  # 256 bits
    if len(found.open) > 1:  # which identity to open: chosen next (only after this right code)
        cur.execute("DELETE FROM sign_in_choice WHERE expires_at < %s", (now - DAY,))
        cur.execute("INSERT INTO sign_in_choice (token_sha256, sign_in_key, created_at,"
                    " expires_at) VALUES (%s, %s, %s, %s)",
                    (_sha(token), found.key.id, now, now + CHOICE_VALID))
        return Verified(choice=token)
    _open_session(cur, store, token, found.open[0].id, now, from_code=code_id)
    return Verified(token)


def _repeat(cur, store, secret: bytes, found: Address, code: str, browser: str,
            now: datetime) -> Verified | None:
    """While the address's newest code signed in no more than REPEAT_WINDOW ago: a WRONG code
    is an ordinary wrong code (counted against the address; the per-network tries were counted
    before) and ends the repeat of that code for good. The RIGHT code (the same constant-time
    comparison) signs in again only when the browser carries the cookie recorded at the
    first success, which is there only when that success came from the browser that asked
    for the code, and every identity of the address is open. Anything else: None (the same
    refusal as before). Nothing earlier is ended here."""
    cur.execute("SELECT id, code_hmac, browser_sha256 FROM login_code WHERE reviewer_id = %s"
                " AND id = (SELECT max(id) FROM login_code WHERE reviewer_id = %s)"
                " AND signed_in_at > %s FOR UPDATE",
                (found.key.id, found.key.id, now - REPEAT_WINDOW))
    row = cur.fetchone()
    if row is None:
        return None
    entered = _mac(secret, "code", str(found.key.id), code)
    if not hmac.compare_digest(row[1], entered):
        cur.execute("UPDATE login_code SET browser_sha256 = NULL WHERE id = %s", (row[0],))
        cur.execute("INSERT INTO rate_limit (bucket, at) VALUES (%s, %s)",
                    (f"wrong:r{found.key.id}", now))
        exhausted = len(_wrong_codes(cur, found.key.id, now)) == WRONG_CODES_PER_DAY
        return Verified(None, "+".join(r.alias for r in found.open) if exhausted else None)
    if not browser or len(browser) > 128 or row[2] is None or not hmac.compare_digest(
            row[2], _sha(browser)) or not all(store.is_open(r) for r in found.open):
        return None
    return _signed_in(cur, store, found, row[0], now)


def _open_session(cur, store, token: str, reviewer_id: int, now: datetime, *,
                  from_code: int | None = None) -> None:
    cur.execute("INSERT INTO web_session (token_sha256, reviewer_id, created_at, "
                "last_seen_at, expires_at, from_code) VALUES (%s, %s, %s, %s, %s, %s)",
                (_sha(token), reviewer_id, now, now, now + SESSION_VALID, from_code))
    store.note_first_sign_in(reviewer_id)  # in the same transaction as the session


def _choice_key(cur, token: str, now: datetime) -> int | None:
    if not token or len(token) > 128:
        return None
    cur.execute("SELECT sign_in_key FROM sign_in_choice WHERE token_sha256 = %s"
                " AND used_at IS NULL AND expires_at > %s", (_sha(token), now))
    row = cur.fetchone()
    return row[0] if row else None


def choices(store, token: str, now: datetime) -> tuple | None:
    """The identities a pending choice may open, in order (the real one first), or None: no such
    choice, used, expired, or fewer than two identities left that may sign in."""
    if not token:
        return None  # no choice cookie: nothing to look up
    with store.connection() as conn:
        key = _choice_key(conn.cursor(row_factory=tuple_row), token, now)
        if key is None:
            return None
        row = store.reviewer_by_id(key)
        found = address_of(store, row.sign_in) if row is not None else Address()
    return found.open if len(found.open) > 1 else None


def choose(store, token: str, which: str, now: datetime) -> str | None:
    """Open the identity ``which`` (REAL or TEST) of a pending choice: a new session's token, or
    None. The choice is used up whatever happens; it names an identity of ITS address only."""
    with store.connection() as conn:
        cur = conn.cursor(row_factory=tuple_row)
        if not token or len(token) > 128:
            return None
        cur.execute("UPDATE sign_in_choice SET used_at = %s WHERE token_sha256 = %s"
                    " AND used_at IS NULL AND expires_at > %s RETURNING sign_in_key",
                    (now, _sha(token), now))
        row = cur.fetchone()
        if row is None:
            return None
        key = store.reviewer_by_id(row[0])
        chosen = [r for r in address_of(store, key.sign_in).open if kind(r) == which]
        if len(chosen) != 1:
            return None
        # The other choices a repeat of the same sign-in made end with this one.
        cur.execute("DELETE FROM sign_in_choice WHERE sign_in_key = %s AND used_at IS NULL",
                    (row[0],))
        session = secrets.token_urlsafe(32)
        _open_session(cur, store, session, chosen[0].id, now)
    return session


class Signed(NamedTuple):
    """A session that stands: its reviewer, and, when its stored expiry was just moved, the
    seconds its cookie should now live (the browser's copy slides with the stored one)."""

    reviewer: object
    cookie_seconds: int | None = None


def session_reviewer(store, token: str, now: datetime) -> Signed | None:
    """The reviewer of a session cookie, or None: no such session, not used for thirty days,
    older than ninety days, or a reviewer who may no longer use the site (their session is
    deleted)."""
    if not token or len(token) > 128:
        return None
    seconds = None
    with store.connection() as conn:
        cur = conn.cursor(row_factory=tuple_row)
        cur.execute("SELECT reviewer_id, last_seen_at, expires_at, created_at, from_code"
                    " FROM web_session WHERE token_sha256 = %s", (_sha(token),))
        row = cur.fetchone()
        if row is None or row[2] <= now or row[3] + SESSION_LIMIT <= now:
            return None
        if row[4] is not None:  # the first use of a session a code made: its others end
            cur.execute("DELETE FROM web_session WHERE from_code = %s AND token_sha256 <> %s",
                        (row[4], _sha(token)))
            cur.execute("UPDATE web_session SET from_code = NULL WHERE token_sha256 = %s",
                        (_sha(token),))
        if now - row[1] >= SESSION_REFRESH:
            expires = min(now + SESSION_VALID, row[3] + SESSION_LIMIT)
            cur.execute("UPDATE web_session SET last_seen_at = %s, expires_at = %s "
                        "WHERE token_sha256 = %s", (now, expires, _sha(token)))
            seconds = int((expires - now).total_seconds())
    reviewer = store.reviewer_by_id(row[0])
    if not eligible(reviewer):
        end_session(store, token)
        return None
    return Signed(reviewer, seconds)


def end_session(store, token: str) -> None:
    if token and len(token) <= 128:
        with store.connection() as conn:
            conn.cursor().execute("DELETE FROM web_session WHERE token_sha256 = %s",
                                  (_sha(token),))
