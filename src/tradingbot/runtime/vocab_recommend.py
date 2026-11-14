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
execution, ``ibkr``, ``ibkr_orders`` or storage modules (tests/test_vocab_recommend.py).
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
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
    if login is not None and not _IDENT.match(login):
        return Identity(refused="IDENTITY_UNREADABLE")
    allowed = {x.strip().casefold() for x in env.get(ALLOWED_ENV, "").split(",") if x.strip()}
    if allowed and (login is None or login.casefold() not in allowed):
        return Identity(refused="LOGIN_NOT_ALLOWED")
    return Identity(login, name if name and _IDENT.match(name) else "")


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


def verify_bytes(data: bytes) -> Verification:
    """Check every line: schema, sequence 1, 2, ... and the previous line's sha256."""
    if data and not data.endswith(b"\n"):
        return Verification(False, problem="LAST_LINE_INCOMPLETE")
    prev, records = GENESIS, []
    for n, line in enumerate(data.split(b"\n")[:-1] if data else [], 1):
        try:
            rec = json.loads(line)
        except ValueError:
            return Verification(False, n - 1, f"NOT_JSON at line {n}")
        if not isinstance(rec, dict) or rec.get("schema") != REC_SCHEMA or rec.get("seq") != n:
            return Verification(False, n - 1, f"SCHEMA_OR_SEQUENCE at line {n}")
        if rec.get("prev_sha256") != prev:
            return Verification(False, n - 1, f"CHAIN_BROKEN at line {n}")
        prev = _sha(line)
        records.append(rec)
    return Verification(True, len(records), None, records, prev)


def verify_log(path: str | Path) -> Verification | None:
    """The log's verification, None when there is no log."""
    data = _read_private(path, "recommendations log")
    return None if data is None else verify_bytes(data)


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
    note = (note or "").strip()
    if len(note) > MAX_NOTE or re.search(r"[\x00-\x08\x0b-\x1f\x7f]", note):
        raise RecommendationError("NOTE_TOO_LONG_OR_UNPRINTABLE")
    with PrivateDir(log_path, "recommendations log") as d:
        fd = d.open(d.name, os.O_RDWR | os.O_APPEND | os.O_CREAT)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            os.lseek(fd, 0, os.SEEK_SET)
            chunks = []
            while chunk := os.read(fd, 1 << 16):
                chunks.append(chunk)
            v = verify_bytes(b"".join(chunks))
            if not v.ok:
                raise RecommendationError("LOG_FAILS_VERIFICATION")
            rec = {"schema": REC_SCHEMA, "seq": v.count + 1,
                   "at": (now or datetime.now(UTC)).astimezone(UTC).isoformat(),
                   "reviewer": {"login": login, "name": name or ""}, "proposal_id": proposal,
                   "token": row["token"],
                   "recommendation": {"verdict": verdict, "group": group, "family": family},
                   "note": note, "prev_sha256": v.head}
            line = json.dumps(rec, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            os.write(fd, line.encode("utf-8") + b"\n")
            os.fsync(fd)
        finally:
            os.close(fd)  # releases the lock
    return rec


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
    note = f"; note: {rec['note']}" if rec.get("note") else ""
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
