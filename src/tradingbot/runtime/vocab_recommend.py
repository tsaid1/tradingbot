"""The outside reviewer's material and record (docs/VOCAB_REVIEWER.md).

* ``export_rows`` / ``write_export``: the owner's SANITIZED export of the proposals the owner page
  shows. Each row keeps only ``EXPORT_FIELDS`` (an allow-list: any other field is dropped) and each
  value is checked (codes match a code pattern, the token must be a plain short word): no meaning,
  example, measured count, post or source id, date or path ever leaves.
* ``append_recommendation`` / ``verify_log``: the reviewer's append-only JSONL recommendations log
  beside the export, mode 0600, each line carrying the sha256 of the previous line, so an edited
  or removed line is detected. It is the ONLY file the reviewer page writes.
* ``identity_from_headers``: the reviewer's identity, only from the identity-aware proxy's header.
* ``record_owner_decisions``: the owner page's audit of each applied batch beside the lexicon.

Advice only: nothing here reads or writes the lexicon, and no recommendation changes the owner's
batch. This module (and the reviewer page) must not import the runtime service, mapping,
execution, ``ibkr``, order-capable or storage modules (tests/test_vocab_recommend.py).
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import unicodedata
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from tradingbot.runtime.vocab_review import (
    _FAMILY_RE,
    ACCEPT,
    GROUP_LIST,
    GROUPS,
    KEEP_HOLDING,
    MARKET_NAME,
    REJECT,
    PrivateDir,
    PrivatePathError,  # noqa: F401 - the reviewer page catches R.PrivatePathError
    proposal_id,
)

EXPORT_SCHEMA = "tradingbot.vocab_reviewer_export/1"
REC_SCHEMA = "tradingbot.vocab_recommendation/1"
AUDIT_SCHEMA = "tradingbot.vocab_reviewer_audit/1"
EXPORT_ENV = "TRADINGBOT_VOCAB_REVIEWER_EXPORT"
LOGIN_HEADER_ENV, NAME_HEADER_ENV = "TRADINGBOT_REVIEWER_LOGIN_HEADER", "TRADINGBOT_REVIEWER_NAME_HEADER"
ALLOWED_ENV, IDENTITY_ENV = "TRADINGBOT_REVIEWER_ALLOWED_LOGINS", "TRADINGBOT_REVIEWER_IDENTITY"
DEFAULT_LOGIN_HEADER, DEFAULT_NAME_HEADER = "Tailscale-User-Login", "Tailscale-User-Name"
# The two modes (docs/VOCAB_REVIEWER.md, "Modes"): LOCAL (default) or PUBLISHED at this host.
PUBLISHED_HOST_ENV, LOCAL_IDENTITY_ENV = ("TRADINGBOT_REVIEW_PUBLISHED_HOST",
                                          "TRADINGBOT_REVIEW_LOCAL_IDENTITY")
# Server options both reviewer apps require (refused otherwise): cross-origin and cross-site
# protection on, no agent API, no static serving, no error details to the browser.
SAFE_OPTIONS = {"server.enableCORS": True, "server.enableXsrfProtection": True,
                "server.enableAgentApi": False, "server.enableStaticServing": False,
                "client.showErrorDetails": "none"}
# Any of these on a LOCAL-mode request means it did not come straight from a local browser.
FORWARDING_HEADERS = ("X-Forwarded-For", "X-Forwarded-Host", "X-Forwarded-Proto",
                      "X-Forwarded-Port", "X-Forwarded-Server", "X-Forwarded-Prefix",
                      "X-Original-Forwarded-For", "Forwarded", "Via", "X-Real-Ip",
                      "True-Client-Ip", "X-Client-Ip", "Cf-Connecting-Ip",
                      "Cf-Access-Jwt-Assertion")
_HOST = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$")
# The allow-list of the export. Anything not named here never leaves the owner's machine.
EXPORT_FIELDS = ("id", "token", "kind", "posts", "holds", "families", "reasons",
                 "market_warning", "group", "family")
NEEDS_CONTEXT = "NEEDS_CONTEXT"
REC_VERDICTS = (ACCEPT, REJECT, KEEP_HOLDING, NEEDS_CONTEXT)
LIST_GROUPS = tuple(GROUP_LIST)  # an ACCEPT names one of the groups that has a list
MAX_NOTE = 500
GENESIS = "0" * 64
_CODE = re.compile(r"^[A-Z][A-Z0-9_:]{0,63}$")
_ID = re.compile(r"^[0-9a-f]{16}$")
# A plain word or short phrase: no markup characters, at most 4 parts and 48 characters. A longer
# "token" could be an excerpt of a post; it is withheld, never exported.
_WORD = re.compile(r"^[\w'’$#@&+./-]+( [\w'’$#@&+./-]+){0,3}$")
_IDENT = re.compile(r"^[^\x00-\x1f\x7f]{1,256}$")


class RecommendationError(ValueError):
    """A recommendation refused; ``code`` is a constant (never a path or a private word)."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


# ------------------------------------------------------------------ the sanitized export


def sanitize_row(raw: dict) -> dict | None:
    """One export row: exactly ``EXPORT_FIELDS``, each checked; None when the row is unusable."""
    token, pid = raw.get("token"), raw.get("id")
    if not (isinstance(token, str) and len(token) <= 48 and _WORD.match(token)
            and isinstance(pid, str) and _ID.match(pid) and raw.get("group") in GROUPS):
        return None
    family = raw.get("family")

    def codes(values, pattern):
        return sorted({v for v in values or () if isinstance(v, str) and pattern.match(v)})

    def count(v):
        return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else 0

    kind = raw.get("kind")
    row = {"id": pid, "token": token,
           "kind": kind if isinstance(kind, str) and _CODE.match(kind) else None,
           "posts": count(raw.get("posts")), "holds": count(raw.get("holds")),
           "families": codes(raw.get("families"), _FAMILY_RE),
           "reasons": codes(raw.get("reasons"), _CODE),
           "market_warning": raw.get("market_warning") is True, "group": raw["group"],
           "family": family if isinstance(family, str) and _FAMILY_RE.match(family) else None}
    return {k: row[k] for k in EXPORT_FIELDS}


def export_rows(proposals) -> tuple[list[dict], int]:
    """(rows, withheld): the sanitized rows of ``proposals`` (``vocab_review.Proposal``)."""
    rows = [sanitize_row({**asdict(p), "id": proposal_id(p)}) for p in proposals]
    return [r for r in rows if r is not None], sum(r is None for r in rows)


def write_export(path: str | Path, proposals) -> tuple[int, int]:
    """Write the export (one JSON file, mode 0600, private-path guards). (exported, withheld)."""
    rows, withheld = export_rows(proposals)
    data = json.dumps({"schema": EXPORT_SCHEMA, "proposals": rows}, ensure_ascii=False,
                      indent=1).encode("utf-8") + b"\n"
    with PrivateDir(path, "reviewer export", create=True) as d:
        d.write_atomic(data)
    return len(rows), withheld


def _read_private(path: str | Path, what: str) -> bytes | None:
    """A private file's bytes (no link, link count 1, mode 0600), None when it does not exist."""
    with PrivateDir(path, what) as d:
        try:
            fd = d.open(d.name, os.O_RDONLY)
        except FileNotFoundError:
            return None
        with os.fdopen(fd, "rb") as f:
            return f.read()


def read_export(path: str | Path) -> list[dict]:
    """The export's rows, each sanitized again (an unknown field is dropped)."""
    data = _read_private(path, "reviewer export")
    if data is None:
        raise RecommendationError("EXPORT_MISSING")
    try:
        raw = json.loads(data)
    except ValueError:
        raise RecommendationError("EXPORT_NOT_JSON") from None
    if not isinstance(raw, dict) or raw.get("schema") != EXPORT_SCHEMA:
        raise RecommendationError("EXPORT_SCHEMA")
    rows = [sanitize_row(r) for r in raw.get("proposals") or () if isinstance(r, dict)]
    return list({r["id"]: r for r in rows if r is not None}.values())


def export_families(rows: list[dict]) -> list[str]:
    return sorted({f for r in rows for f in r["families"] + ([r["family"]] if r["family"] else [])})


def reviewer_text(row: dict) -> str:
    """A row as plain text (the page shows it with st.text: nothing is interpreted)."""
    return "\n".join([
        f"word: {row['token']}",
        f"proposed group: {GROUPS[row['group']]}"
        + (f" (family {row['family']})" if row["family"] else ""),
        f"kind: {row['kind'] or '-'}",
        f"evidence: {row['posts']} held post(s), {row['holds']} hold(s)",
        f"families: {', '.join(row['families']) or '-'}",
        f"reason codes: {', '.join(row['reasons']) or '-'}",
    ] + (["warning: seen with a price or a trading word; may be another market"]
         if row["market_warning"] else []))


# ------------------------------------------------------------------ identity


@dataclass(frozen=True)
class Identity:
    login: str | None = None  # None: no identity header (read-only preview)
    name: str = ""
    refused: str | None = None  # a constant: the request sees only "not authorized"
    via_proxy: bool = False  # PUBLISHED mode: every session, by configuration


def clean_text(text: str) -> str:
    """Free text as one plain line: control, format (including direction-override) and other
    invisible characters, tabs and line breaks become spaces (on save and on display)."""
    return " ".join("".join(" " if unicodedata.category(c).startswith("C") else c
                            for c in text).split())


def _printable_identity(v: str) -> bool:
    return bool(_IDENT.match(v)) and clean_text(v) == v


def identity_from_headers(headers, env=os.environ) -> Identity:
    """The reviewer, ONLY from the identity-aware proxy's request headers (``st.context.headers``).
    A header sent twice is refused. With ``TRADINGBOT_REVIEWER_ALLOWED_LOGINS`` set, any other
    login, and a request with no login, is refused."""
    mode = env.get(IDENTITY_ENV, "tailscale")
    if mode != "tailscale":
        # SEAM (not implemented): a Cloudflare Access variant would validate the signed
        # Cf-Access-Jwt-Assertion token here (signature against the team's published keys,
        # audience, issuer, expiry) and take the login from its verified claims, never from a
        # plain header. Until then any other mode refuses everyone.
        return Identity(refused="IDENTITY_MODE_NOT_IMPLEMENTED")

    def one(name: str) -> str | None | bool:
        values = headers.get_all(name) if hasattr(headers, "get_all") else (
            [headers[name]] if name in headers else [])
        if len(values) > 1:
            return False
        return values[0].strip() if values and values[0].strip() else None

    login = one(env.get(LOGIN_HEADER_ENV) or DEFAULT_LOGIN_HEADER)
    name = one(env.get(NAME_HEADER_ENV) or DEFAULT_NAME_HEADER)
    if login is False or name is False:
        return Identity(refused="DUPLICATE_IDENTITY_HEADER")
    if login is not None and not _printable_identity(login):
        return Identity(refused="IDENTITY_UNREADABLE")
    allowed = {x.strip().casefold() for x in env.get(ALLOWED_ENV, "").split(",") if x.strip()}
    if allowed and (login is None or login.casefold() not in allowed):
        return Identity(refused="LOGIN_NOT_ALLOWED")
    return Identity(login, name if name and _printable_identity(name) else "")


def _loopback_origins(port: int) -> tuple[str, ...]:
    return (f"http://127.0.0.1:{port}", f"http://localhost:{port}")


def server_problem(get_option, env=os.environ) -> str | None:
    """Why this server may not serve a reviewer page (a message without content), or None.
    Bound to 127.0.0.1, usage statistics off, ``SAFE_OPTIONS``, no ``*`` in the host or origin
    allow-lists. PUBLISHED mode: a valid host name, no local identity, and that host in
    ``server.allowedHosts`` and its https origin in ``server.corsAllowedOrigins``. LOCAL mode:
    neither list may name anything but loopback (a proxy configuration needs PUBLISHED mode)."""
    if get_option("server.address") != "127.0.0.1":
        return "this server is not bound to 127.0.0.1 (start it with --server.address 127.0.0.1)"
    if get_option("browser.gatherUsageStats"):
        return "Streamlit usage statistics are on (start it with --browser.gatherUsageStats false)"
    for option, safe in SAFE_OPTIONS.items():
        if get_option(option) != safe:
            return f"{option} must be {safe}"
    hosts = list(get_option("server.allowedHosts") or [])
    origins = list(get_option("server.corsAllowedOrigins") or [])
    if "*" in hosts or "*" in origins:
        return "a '*' entry in server.allowedHosts or server.corsAllowedOrigins"
    published = env.get(PUBLISHED_HOST_ENV, "")
    if published:
        if not _HOST.match(published):
            return f"{PUBLISHED_HOST_ENV} is not a plain host name"
        if env.get(LOCAL_IDENTITY_ENV):
            return f"{LOCAL_IDENTITY_ENV} is set in PUBLISHED mode"
        if published not in hosts or f"https://{published}" not in origins:
            return ("PUBLISHED mode needs --server.allowedHosts <host> and "
                    "--server.corsAllowedOrigins https://<host>")
        return None
    port = get_option("server.port")
    if any(h not in ("127.0.0.1", "localhost") for h in hosts) or any(
            o not in _loopback_origins(port) for o in origins):
        return f"a proxy host is configured: set {PUBLISHED_HOST_ENV} (PUBLISHED mode)"
    return None


def request_identity(headers, env, port: int) -> Identity:
    """The identity of one request, by the configured mode, never by guessing from headers.

    PUBLISHED (``TRADINGBOT_REVIEW_PUBLISHED_HOST`` set): ``Host`` must be that host and
    ``Origin`` its https origin, exactly; the login comes ONLY from the proxy header, and the
    session is proxied by configuration. LOCAL (default): a request carrying any forwarding,
    proxy or identity header is refused outright; ``Host`` must be this server's loopback address
    and port and ``Origin``, when sent, exactly its loopback origin; the login is
    ``TRADINGBOT_REVIEW_LOCAL_IDENTITY`` (None: no identity)."""
    def values(name):
        return headers.get_all(name) if hasattr(headers, "get_all") else (
            [headers[name]] if name in headers else [])

    host, origin = values("Host"), values("Origin")
    if len(host) > 1 or len(origin) > 1:
        return Identity(refused="DUPLICATE_HOST_OR_ORIGIN")
    host, origin = (host or [""])[0], (origin or [None])[0]
    published = env.get(PUBLISHED_HOST_ENV, "")
    if published:
        if host != published or origin != f"https://{published}":
            return Identity(refused="HOST_OR_ORIGIN_NOT_PUBLISHED_HOST")
        ident = identity_from_headers(headers, env)
        if ident.refused or ident.login is None:
            return Identity(refused=ident.refused or "NO_PROXY_IDENTITY")
        return Identity(ident.login, ident.name, via_proxy=True)
    names = {k.casefold() for k in headers}
    identity_headers = {(env.get(LOGIN_HEADER_ENV) or DEFAULT_LOGIN_HEADER).casefold(),
                        (env.get(NAME_HEADER_ENV) or DEFAULT_NAME_HEADER).casefold()}
    if names & ({h.casefold() for h in FORWARDING_HEADERS} | identity_headers) or any(
            n.startswith(("tailscale-", "cf-")) for n in names):
        return Identity(refused="PROXY_HEADER_IN_LOCAL_MODE")
    if host not in (f"127.0.0.1:{port}", f"localhost:{port}"):
        return Identity(refused="NOT_LOCAL")
    if origin is not None and origin not in _loopback_origins(port):
        return Identity(refused="ORIGIN_NOT_THIS_SERVER")
    login = env.get(LOCAL_IDENTITY_ENV, "").strip()
    allowed = {x.strip().casefold() for x in env.get(ALLOWED_ENV, "").split(",") if x.strip()}
    if login and not _printable_identity(login):
        return Identity(refused="IDENTITY_UNREADABLE")
    if allowed and login.casefold() not in allowed:
        return Identity(refused="LOGIN_NOT_ALLOWED")
    return Identity(login or None)


# ------------------------------------------------------------------ the recommendations log


def recommendations_path(export_path: str | Path) -> Path:
    p = Path(export_path).expanduser()
    return p.with_name(f"{p.stem}.recommendations.jsonl")


@dataclass
class Verification:
    ok: bool
    count: int = 0
    problem: str | None = None  # a constant and a line number
    records: list[dict] = field(default_factory=list)
    head: str = GENESIS  # sha256 of the last line


def _sha(line: bytes) -> str:
    return hashlib.sha256(line).hexdigest()


def verify_bytes(data: bytes, schema: str = REC_SCHEMA) -> Verification:
    """Check every line: schema, sequence 1, 2, ... and the previous line's sha256."""
    if data and not data.endswith(b"\n"):
        return Verification(False, problem="LAST_LINE_INCOMPLETE")
    prev, records = GENESIS, []
    for n, line in enumerate(data.split(b"\n")[:-1] if data else [], 1):
        try:
            rec = json.loads(line)
        except ValueError:
            return Verification(False, n - 1, f"NOT_JSON at line {n}")
        if not isinstance(rec, dict) or rec.get("schema") != schema or rec.get("seq") != n:
            return Verification(False, n - 1, f"SCHEMA_OR_SEQUENCE at line {n}")
        if rec.get("prev_sha256") != prev:
            return Verification(False, n - 1, f"CHAIN_BROKEN at line {n}")
        prev = _sha(line)
        records.append(rec)
    return Verification(True, len(records), None, records, prev)


def head_path(log_path: str | Path) -> Path:
    p = Path(log_path).expanduser()
    return p.with_name(f"{p.name}.head")


def _with_head(v: Verification, head: bytes | None) -> Verification:
    """The chain checked against the head file (line count and sha256 of the last line, written
    after every append): an edited or removed LAST line is detected too."""
    if not v.ok:
        return v
    if head is None:
        return v if v.count == 0 else Verification(False, v.count, "HEAD_FILE_MISSING")
    try:
        h = json.loads(head)
    except ValueError:
        return Verification(False, v.count, "HEAD_FILE_UNREADABLE")
    if not isinstance(h, dict) or h.get("count") != v.count or h.get("sha256") != v.head:
        return Verification(False, v.count, "HEAD_MISMATCH (last line edited or removed)")
    return v


def verify_log(path: str | Path, schema: str = REC_SCHEMA,
               what: str = "recommendations log") -> Verification | None:
    """The log's verification (chain and head file), None when there is no log."""
    data = _read_private(path, what)
    if data is None:
        return None
    return _with_head(verify_bytes(data, schema), _read_private(head_path(path), what + " head"))


def repair_head(log_path: str | Path, schema: str, what: str, *, confirm: bool = False) -> dict:
    """Repair the head file in exactly two situations, under the log's lock; anything else is
    refused with a code. Never removes or rewrites a log line, never repairs a broken chain.

    (i) ``CRASH_BETWEEN_LINE_AND_HEAD``: the head matches line n-1 and line n chains onto it.
    (ii) ``NO_HEAD_FILE``: no head file, and the whole log verifies as a chain from line 1.

    Returns ``{situation, seq, phase, item}`` of the last line (no content); writes the head
    for it only with ``confirm``."""
    with PrivateDir(log_path, what) as d:
        try:
            fd = d.open(d.name, os.O_RDONLY)
        except FileNotFoundError:
            raise RecommendationError("NO_LOG") from None
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            chunks = []
            while chunk := os.read(fd, 1 << 16):
                chunks.append(chunk)
            data = b"".join(chunks)
            v = verify_bytes(data, schema)
            if not v.ok:
                raise RecommendationError("CHAIN_BROKEN_NOT_REPAIRED")
            if v.count == 0:
                raise RecommendationError("NOTHING_TO_REPAIR")
            head = _read_private(head_path(log_path), what + " head")
            if head is None:
                situation = "NO_HEAD_FILE"
            else:
                try:
                    h = json.loads(head)
                except ValueError:
                    raise RecommendationError("HEAD_NOT_REPAIRABLE") from None
                lines = data.split(b"\n")[:-1]
                if not isinstance(h, dict):
                    raise RecommendationError("HEAD_NOT_REPAIRABLE")
                if h.get("count") == v.count and h.get("sha256") == v.head:
                    raise RecommendationError("HEAD_ALREADY_MATCHES")
                previous = GENESIS if v.count == 1 else _sha(lines[-2])
                if h.get("count") != v.count - 1 or h.get("sha256") != previous:
                    raise RecommendationError("HEAD_NOT_REPAIRABLE")
                situation = "CRASH_BETWEEN_LINE_AND_HEAD"
            last = v.records[-1]
            out = {"situation": situation, "seq": last.get("seq"),
                   "phase": last.get("phase", "RECOMMENDATION"),
                   "item": last.get("item_id") or last.get("proposal_id")}
            if confirm:
                with PrivateDir(head_path(log_path), what + " head") as h_dir:
                    h_dir.write_atomic(json.dumps({"count": v.count, "sha256": v.head}).encode())
        finally:
            os.close(fd)
    return out


def append_chained(log_path: str | Path, what: str, schema: str, make) -> dict:
    """Append ONE line to a chained log under an exclusive lock: ``make(verification)`` returns
    the record (without ``schema``, ``seq``, ``prev_sha256``) or raises to write nothing. Refused
    when the log fails verification or its path fails the private-path guards."""
    with PrivateDir(log_path, what) as d:
        fd = d.open(d.name, os.O_RDWR | os.O_APPEND | os.O_CREAT)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            os.lseek(fd, 0, os.SEEK_SET)
            chunks = []
            while chunk := os.read(fd, 1 << 16):
                chunks.append(chunk)
            v = _with_head(verify_bytes(b"".join(chunks), schema),
                           _read_private(head_path(log_path), what + " head"))
            if not v.ok:
                raise RecommendationError("LOG_FAILS_VERIFICATION")
            rec = {**make(v), "schema": schema, "seq": v.count + 1, "prev_sha256": v.head}
            line = json.dumps(rec, ensure_ascii=False, sort_keys=True,
                              separators=(",", ":")).encode("utf-8")
            os.write(fd, line + b"\n")
            os.fsync(fd)
            # Still under the lock. A crash between the two writes leaves a head that fails
            # verification: the log then refuses appends until the owner checks it.
            with PrivateDir(head_path(log_path), what + " head") as h:
                h.write_atomic(json.dumps({"count": rec["seq"], "sha256": _sha(line)}).encode())
        finally:
            os.close(fd)  # releases the lock
    return rec


def append_recommendation(log_path: str | Path, rows: list[dict], *, login: str, name: str,
                          proposal: str, verdict: str, group: str | None = None,
                          family: str | None = None, note: str = "",
                          now: datetime | None = None) -> dict:
    """Append ONE recommendation (the reviewer page's only write). The token comes from the
    export, never from the request. Refused (nothing written) on any invalid input, a log that
    fails verification, or a log path that fails the private-path guards."""
    row = next((r for r in rows if r["id"] == proposal), None)
    if row is None:
        raise RecommendationError("UNKNOWN_PROPOSAL")
    if not (isinstance(login, str) and _IDENT.match(login)):
        raise RecommendationError("NO_REVIEWER_IDENTITY")
    if verdict not in REC_VERDICTS:
        raise RecommendationError("UNKNOWN_VERDICT")
    if verdict == ACCEPT:
        if group not in LIST_GROUPS:
            raise RecommendationError("ACCEPT_NEEDS_A_GROUP")
        if group == MARKET_NAME and family not in export_families(rows):
            raise RecommendationError("ACCEPT_NEEDS_A_FAMILY")
        family = family if group == MARKET_NAME else None
    else:
        group = family = None
    note = clean_text(note or "")
    if len(note) > MAX_NOTE:
        raise RecommendationError("NOTE_TOO_LONG_OR_UNPRINTABLE")
    return append_chained(log_path, "recommendations log", REC_SCHEMA, lambda _v: {
        "at": (now or datetime.now(UTC)).astimezone(UTC).isoformat(),
        "reviewer": {"login": login, "name": name or ""}, "proposal_id": proposal,
        "token": row["token"],
        "recommendation": {"verdict": verdict, "group": group, "family": family},
        "note": note})


def latest(records: list[dict]) -> dict[str, dict[str, dict]]:
    """{proposal id: {reviewer login: latest record}}: a later line supersedes an earlier one."""
    out: dict[str, dict[str, dict]] = {}
    for r in records:
        out.setdefault(str(r.get("proposal_id")), {})[str((r.get("reviewer") or {})
                                                          .get("login"))] = r
    return out


def describe(rec: dict) -> str:
    """One recommendation as one plain-text line (owner page; shown with st.text)."""
    r, who = rec.get("recommendation") or {}, rec.get("reviewer") or {}
    what = str(r.get("verdict"))
    if r.get("group"):
        what += f" into {GROUPS.get(r['group'], r['group'])}"
        what += f" (family {r['family']})" if r.get("family") else ""
    note = f"; note: {clean_text(str(rec['note']))}" if rec.get("note") else ""
    name = f" ({who['name']})" if who.get("name") else ""
    return (f"Reviewer {who.get('login')}{name} recommends {what}, at {rec.get('at')} "
            f"(line {rec.get('seq')}){note}")


# ------------------------------------------------------------------ the owner's audit


def audit_path(lexicon_path: str | Path) -> Path:
    p = Path(lexicon_path).expanduser()
    return p.with_name(f"{p.stem}.reviewer-audit.jsonl")


def record_owner_decisions(lexicon_path, decided: list[tuple[str, object]], status: str,
                           backup: str | None, shown: dict[str, list[dict]], log_head: dict,
                           now: datetime | None = None) -> None:
    """Append one line for an applied batch: per word, the owner's decision and the reviewer
    recommendations the page showed with it (login, verdict, group, family, log line). Also the
    log's head (count and last line's sha256) so a later truncation of the log is detectable."""
    at = (now or datetime.now(UTC)).astimezone(UTC).isoformat()
    line = {"schema": AUDIT_SCHEMA, "at": at, "result": status, "backup": backup,
            "recommendations_log_head": log_head,
            "decisions": [{"token": item.token, "proposal_id": pid, "decision": item.verdict,
                           "group": item.group, "family": item.family,
                           "shown_recommendations": shown.get(pid, [])}
                          for pid, item in decided]}
    with PrivateDir(audit_path(lexicon_path), "reviewer audit") as d:
        fd = d.open(d.name, os.O_WRONLY | os.O_APPEND | os.O_CREAT)
        try:
            os.write(fd, (json.dumps(line, ensure_ascii=False, sort_keys=True) + "\n").encode())
            os.fsync(fd)
        finally:
            os.close(fd)


def shown_summary(recs: dict[str, dict]) -> list[dict]:
    """What the owner page shows of one proposal's recommendations, for the audit (no note)."""
    return [{"login": login, "verdict": (r.get("recommendation") or {}).get("verdict"),
             "group": (r.get("recommendation") or {}).get("group"),
             "family": (r.get("recommendation") or {}).get("family"), "seq": r.get("seq")}
            for login, r in sorted(recs.items())]
