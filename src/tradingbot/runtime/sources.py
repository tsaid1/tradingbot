"""Source adapters: where posts come from, in publication order, behind a durable cursor.

``SourceAdapter.poll()`` returns the posts after the committed cursor. It does not move the cursor:
the service calls ``commit(post)`` once a post has been fully processed and journaled, in order. A
restart therefore re-polls exactly the posts that were not committed (nothing is skipped), and the
replay's once-only ledger turns the one post that may have been processed but not committed into a
recorded duplicate (no second order).

Two implementations:

* ``ReplayFileSource`` reads a JSONL file of ``SourcePost`` objects or G0 archive records (invented
  fixtures, or an approved historical replay). With a compression factor it releases posts in
  compressed real time and rebases their timestamps onto the service clock.
* ``XTimelineSource`` polls the publisher's X user timeline under the subscriber's user-context
  OAuth 2.0 token (ported from docs/research/g0_x_probe/x_probe.py). The HTTP call and the token
  store are injected; tests run against fakes and nothing here opens a socket unless the caller
  passes ``UrllibTransport``.
"""

from __future__ import annotations

import base64
import fcntl
import json
import math
import os
import re
import stat
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol, runtime_checkable

from pydantic import ValidationError

from tradingbot.runtime.store import RuntimeStore
from tradingbot.source_events import Provenance, SourcePost

Clock = Callable[[], datetime]


class SourceError(RuntimeError):
    """The source could not be read this time. Nothing was consumed; retry after backoff."""

    def __init__(self, message: str, *, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class SourceAuthError(SourceError):
    """The credentials were refused (401/403) or could not be refreshed."""


class SourceRateLimited(SourceError):
    """The API quota is spent; ``retry_after`` says when the window resets."""


class SourceGap(SourceError):
    """The source cannot guarantee it returned every post since the cursor; nothing is passed on."""


@runtime_checkable
class SourceAdapter(Protocol):
    name: str
    # True when posts arrive in real time (decisions use the service clock); False for historical
    # replay (decisions use available_at + lag, exactly like tkl_interpreter.replay_posts).
    realtime: bool

    def poll(self) -> list[SourcePost]: ...

    def commit(self, post: SourcePost) -> None: ...

    def cursor(self) -> str | None: ...

    def next_poll_delay(self, cadence: float) -> float: ...


class _Pending:
    """Posts handed out by the last poll, committed strictly in order."""

    def __init__(self):
        self.items: list[tuple[SourcePost, dict]] = []

    def reset(self, items: list[tuple[SourcePost, dict]]) -> None:
        self.items = list(items)

    def pop(self, post: SourcePost) -> dict:
        if not self.items or self.items[0][0] is not post:
            raise ValueError("posts must be committed in the order poll() returned them")
        return self.items.pop(0)[1]


# ============================================================================= replay file


def parse_post_line(
    record: dict, *, expected_author_id: str | None, provenance: Provenance
) -> SourcePost:
    if "schema_version" in record or ("source_id" in record and "post_id" in record):
        return SourcePost.model_validate(record)
    if "id" in record and "created_at" in record:
        if expected_author_id is None:
            raise SourceError("G0 archive records need expected_author_id to verify the author")
        return SourcePost.from_g0_record(
            record, expected_author_id=expected_author_id, provenance=provenance
        )
    raise SourceError("unsupported record shape (neither SourcePost nor G0 archive record)")


class ReplayFileSource:
    """JSONL posts in file order. The cursor is the number of lines consumed; a malformed line stops
    the source (SourceError) rather than being skipped."""

    realtime: bool

    def __init__(
        self,
        path: str | Path,
        store: RuntimeStore,
        *,
        clock: Clock | None = None,
        compression: float | None = None,
        expected_author_id: str | None = None,
        provenance: Provenance = Provenance.SYNTHETIC,
        name: str = "replay",
    ):
        if compression is not None and compression <= 0:
            raise ValueError("compression must be positive")
        self.path = Path(path)
        self.store = store
        self.clock = clock or (lambda: datetime.now(UTC))
        self.compression = compression
        self.realtime = compression is not None
        self.expected_author_id = expected_author_id
        self.provenance = provenance
        self.name = name
        self._pending = _Pending()

    def _state(self) -> dict:
        return self.store.source_state(self.name)

    def cursor(self) -> str | None:
        return str(self._state().get("next_line", 0))

    def next_poll_delay(self, cadence: float) -> float:
        return cadence

    def _read(self, start: int) -> list[tuple[int, SourcePost]]:
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise SourceError(f"replay file unreadable: {type(exc).__name__}") from None
        out = []
        for index in range(start, len(lines)):
            line = lines[index].strip()
            if not line:
                continue
            try:
                post = parse_post_line(
                    json.loads(line),
                    expected_author_id=self.expected_author_id,
                    provenance=self.provenance,
                )
            except (json.JSONDecodeError, ValidationError, ValueError) as exc:
                raise SourceError(
                    f"replay file line {index + 1} is not a valid post ({type(exc).__name__})"
                ) from None
            out.append((index, post))
        return out

    def poll(self) -> list[SourcePost]:
        state = self._state()
        start = int(state.get("next_line", 0))
        items = self._read(start)
        now = self.clock()
        if self.compression is None:
            self._pending.reset([(p, {"next_line": i + 1}) for i, p in items])
            return [p for p, _ in self._pending.items]
        if items and "anchor" not in state:
            # The first post of the file defines time zero; the anchor survives restarts.
            state = {
                **state,
                "anchor": now.isoformat(),
                "origin": items[0][1].available_at.isoformat(),
            }
            self.store.put_source_state(self.name, state, now)
        released = []
        if items:
            anchor = datetime.fromisoformat(state["anchor"])
            origin = datetime.fromisoformat(state["origin"])

            def shift(t: datetime) -> datetime:
                return anchor + timedelta(seconds=(t - origin).total_seconds() / self.compression)

            for index, post in items:
                release = shift(post.available_at)
                if release > now:
                    break  # in order: nothing after a post that is not yet due
                update = {
                    "published_at": shift(post.published_at),
                    "revised_at": shift(post.revised_at) if post.revised_at else None,
                    "received_at": now,
                }
                rebased = SourcePost.model_validate({**post.model_dump(), **update})
                released.append((rebased, {**state, "next_line": index + 1}))
        self._pending.reset(released)
        return [p for p, _ in released]

    def commit(self, post: SourcePost) -> None:
        update = self._pending.pop(post)
        state = {**self._state(), **{k: v for k, v in update.items() if k == "next_line"}}
        self.store.put_source_state(self.name, state, self.clock())


# ============================================================================= X user timeline

API = "https://api.x.com/2"
TOKEN_URL = "https://api.x.com/2/oauth2/token"
TWEET_FIELDS = (
    "created_at,edit_controls,edit_history_tweet_ids,referenced_tweets,attachments,"
    "conversation_id,in_reply_to_user_id,author_id,note_tweet"
)
# X is renaming tweet.* to post.*; try "tweet" naming first and fall back on a 400 (as the probe).
FIELD_NAMING = {
    "tweet": {"param": "tweet.fields", "fields": TWEET_FIELDS},
    "post": {"param": "post.fields", "fields": TWEET_FIELDS.replace("note_tweet", "note_post")},
}
EXPANSIONS = "attachments.media_keys,referenced_tweets.id"
MEDIA_FIELDS = "media_key,type,url,preview_image_url,variants"
MEDIA_TYPES = frozenset({"photo", "video", "animated_gif"})
# A note-less body that ends in an ellipsis, optionally followed by X's own t.co link, was cut off.
TRUNCATED_RE = re.compile(r"…\s*(?:https://t\.co/\w+\s*)?$")


@dataclass
class HttpResponse:
    status: int
    headers: dict[str, str]
    body: bytes


class HttpTransport(Protocol):
    def __call__(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        data: bytes | None = None,
        timeout: float = 20.0,
    ) -> HttpResponse: ...


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # credentials and token-exchange bodies stay at the original endpoint


class UrllibTransport:
    """Standard-library HTTPS, as the probe: no redirects, credentials only to the X API origin."""

    def __call__(self, method, url, *, headers=None, data=None, timeout=20.0) -> HttpResponse:
        if (headers or data) and urllib.parse.urlsplit(url)[:2] != ("https", "api.x.com"):
            raise ValueError("credentialed requests require the X API origin")
        req = urllib.request.Request(url, method=method, headers=headers or {}, data=data)
        try:
            with urllib.request.build_opener(_NoRedirect).open(req, timeout=timeout) as resp:
                hdrs = {k.lower(): v for k, v in resp.headers.items()}
                return HttpResponse(resp.status, hdrs, resp.read())
        except urllib.error.HTTPError as exc:
            hdrs = {k.lower(): v for k, v in exc.headers.items()}
            return HttpResponse(exc.code, hdrs, exc.read())


class TokenStore(Protocol):
    def load(self) -> dict: ...

    def save(self, tokens: dict) -> None: ...

    @contextmanager
    def locked(self): ...


def _private_dir_problem(directory: Path) -> str | None:
    if directory.resolve() != directory:
        return "credential directory must be an absolute path without symlinks"
    if any(p.lower().startswith("dropbox") or p.lower() == "cloudstorage" for p in directory.parts):
        return "credential directory must be outside cloud storage"
    return None


def _check_private_file(path: Path) -> None:
    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_nlink != 1
        or stat.S_IMODE(info.st_mode) != 0o600
    ):
        raise SourceAuthError("credential file must be a private single-link regular file (0600)")


class FileTokenStore:
    """OAuth tokens in a private local file, with the probe's protections: the directory must be
    owned by the user with mode 0700, outside cloud storage and without symlinks; the file must be
    a single-link regular file with mode 0600. Reads and refreshes are serialized with flock.
    Token values never appear in an exception message or a log."""

    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser()

    @contextmanager
    def locked(self):
        directory = self.path.parent
        problem = _private_dir_problem(directory)
        if problem:
            raise SourceAuthError(problem)
        try:
            fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except OSError:
            raise SourceAuthError("credential directory unavailable") from None
        try:
            info = os.fstat(fd)
            if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
                raise SourceAuthError("credential directory must be private (0700)")
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def load(self) -> dict:
        try:
            _check_private_file(self.path)
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, encoding="utf-8") as f:
                tokens = json.load(f)
        except FileNotFoundError:
            raise SourceAuthError("no OAuth tokens in the private store") from None
        except (OSError, json.JSONDecodeError):
            raise SourceAuthError("OAuth token store unreadable") from None
        if not isinstance(tokens, dict) or not tokens.get("access_token"):
            raise SourceAuthError("OAuth token store holds no access token")
        return tokens

    def save(self, tokens: dict) -> None:
        with tempfile.NamedTemporaryFile(
            dir=self.path.parent, mode="w", delete=False, encoding="utf-8"
        ) as tmp:
            try:
                os.chmod(tmp.name, 0o600)
                json.dump(tokens, tmp)
                tmp.flush()
                os.fsync(tmp.fileno())
                tmp.close()
                os.replace(tmp.name, self.path)
            finally:
                Path(tmp.name).unlink(missing_ok=True)


@dataclass(frozen=True)
class ClientCredentials:
    client_id: str
    client_secret: str | None = None

    def __repr__(self) -> str:  # never print the secret
        return "ClientCredentials(<redacted>)"


def load_client_credentials(path: str | Path) -> ClientCredentials:
    """The X app's client id (and optional secret) from a private JSON file named in config."""
    p = Path(path).expanduser()
    problem = _private_dir_problem(p.parent)
    if problem:
        raise SourceAuthError(problem)
    try:
        _check_private_file(p)
        raw = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SourceAuthError("client credentials file not found") from None
    except (OSError, json.JSONDecodeError):
        raise SourceAuthError("client credentials file unreadable") from None
    if not isinstance(raw, dict) or not isinstance(raw.get("client_id"), str):
        raise SourceAuthError("client credentials file has no client_id")
    return ClientCredentials(raw["client_id"], raw.get("client_secret") or None)


def newest_id(current: str | None, ids: list[str]) -> str | None:
    """since_id must be the numerically largest post id seen so far."""
    values = [int(i) for i in ids if i.isdigit()]
    if current and current.isdigit():
        values.append(int(current))
    return str(max(values)) if values else current


def effective_interval(
    requested: float, limit_per_15min: int | None, headroom: float = 0.8
) -> float:
    """The requested interval, or the slowest cadence the 15-minute quota allows with headroom."""
    if not limit_per_15min or limit_per_15min <= 0:
        return requested
    floor = 900.0 / (headroom * limit_per_15min)
    return requested if requested >= floor else math.ceil(floor * 10) / 10


def _int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def to_archive_record(raw: dict, media_by_key: dict[str, dict], observed_at: datetime) -> dict:
    """One API post as a G0 archive record (docs/research/G0_EVENT_SCHEMA.md, section 2)."""
    note = raw.get("note_tweet") or raw.get("note_post") or {}
    keys = (raw.get("attachments") or {}).get("media_keys", []) or []
    refs = raw.get("referenced_tweets") or []
    media = []
    for k in keys:
        m = media_by_key.get(k, {"media_key": k})
        if m.get("type", "photo") in MEDIA_TYPES:
            media.append(
                {
                    "media_key": k,
                    "type": m.get("type", "photo"),
                    "url": m.get("url") or m.get("preview_image_url"),
                }
            )
    return {
        "id": str(raw.get("id")),
        "author_id": str(raw.get("author_id") or "UNKNOWN"),
        "created_at": raw.get("created_at"),
        "conversation_id": raw.get("conversation_id"),
        "text": note.get("text") or raw.get("text", ""),
        "has_note": bool(note.get("text")),
        "short_text": raw.get("text", ""),
        "referenced": [{"type": r.get("type"), "id": r.get("id")} for r in refs],
        "edit_history_ids": raw.get("edit_history_tweet_ids") or [str(raw.get("id"))],
        "media": media,
        "observed_at": observed_at.isoformat(),
    }


class XTimelineSource:
    """GET /2/users/{author_id}/tweets with ``since_id``, under the subscriber's user token.

    * First run (no cursor): a baseline call learns the newest existing post id; posts that existed
      before the service started are never acted on.
    * Pagination: every page since the cursor is read before anything is passed on; more than
      ``max_pages`` raises ``SourceGap`` (never a silent skip).
    * Long posts: the body comes from ``note_tweet`` (or ``note_post``). A note-less body that ends
      in an ellipsis is passed on with ``text_complete = False``; the gateway holds it.
    * Edits: ``edit_history_tweet_ids`` makes an edit revision N of the original ``source_id``.
    * Author: ``verified_author`` is true only when the post's ``author_id`` equals the configured
      id (the endpoint pins it as well); anything else is passed on unverified and the gateway
      blocks it.
    * Rate limits: a 429 blocks polling until the reported reset; the cadence slows to what the
      15-minute quota allows with 20% headroom.
    """

    name = "x_timeline"
    realtime = True

    def __init__(
        self,
        *,
        author_id: str,
        token_store: TokenStore,
        client: ClientCredentials,
        transport: HttpTransport,
        store: RuntimeStore,
        clock: Clock | None = None,
        max_results: int = 100,
        max_pages: int = 10,
        wall_time: Callable[[], float] = time.time,
    ):
        if not author_id.isdigit():
            raise ValueError("author_id must be the numeric X user id")
        self.author_id = author_id
        self.token_store = token_store
        self.client = client
        self.transport = transport
        self.store = store
        self.clock = clock or (lambda: datetime.now(UTC))
        self.max_results = max(5, min(100, max_results))
        self.max_pages = max_pages
        self.wall_time = wall_time
        self._pending = _Pending()
        self.naming = "tweet"
        self.rate_limit: int | None = None
        self.rate_remaining: int | None = None
        self.rate_reset: int | None = None
        self.blocked_until: float | None = None
        self.author_mismatches = 0
        self.truncated_posts = 0

    # ------------------------------------------------------------------ cursor

    def cursor(self) -> str | None:
        return self.store.source_state(self.name).get("since_id")

    def next_poll_delay(self, cadence: float) -> float:
        delay = effective_interval(cadence, self.rate_limit)
        if self.rate_remaining == 0 and self.rate_reset:
            delay = max(delay, self.rate_reset - self.wall_time() + 1)
        return delay

    def commit(self, post: SourcePost) -> None:
        self._pending.pop(post)
        state = self.store.source_state(self.name)
        state["since_id"] = newest_id(state.get("since_id"), [post.post_id])
        self.store.put_source_state(self.name, state, self.clock())

    # ------------------------------------------------------------------ auth

    def _token_request(self, body: dict) -> dict:
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        if self.client.client_secret:
            pair = f"{self.client.client_id}:{self.client.client_secret}".encode()
            headers["Authorization"] = "Basic " + base64.b64encode(pair).decode()
        try:
            resp = self.transport(
                "POST", TOKEN_URL, headers=headers, data=urllib.parse.urlencode(body).encode()
            )
        except (OSError, urllib.error.URLError) as exc:
            raise SourceError(f"token refresh network error: {type(exc).__name__}") from None
        if resp.status != 200:
            raise SourceAuthError(f"token refresh refused (HTTP {resp.status})")
        try:
            payload = json.loads(resp.body)
        except json.JSONDecodeError:
            raise SourceAuthError("token endpoint returned no JSON") from None
        if not isinstance(payload, dict) or not payload.get("access_token"):
            raise SourceAuthError("token endpoint omitted the access token")
        return payload

    def _refresh(self, tokens: dict) -> dict:
        if not tokens.get("refresh_token"):
            raise SourceAuthError("refresh token missing; re-authorize")
        payload = self._token_request(
            {
                "grant_type": "refresh_token",
                "refresh_token": tokens["refresh_token"],
                "client_id": self.client.client_id,
            }
        )
        payload.setdefault("refresh_token", tokens["refresh_token"])
        try:
            lifetime = float(payload.get("expires_in", 7200))
        except (TypeError, ValueError):
            raise SourceAuthError("token endpoint returned an invalid lifetime") from None
        payload["expires_at"] = self.wall_time() + lifetime - 60
        self.token_store.save(payload)
        return payload

    def _access_token(self, *, force_refresh: bool = False) -> str:
        with self.token_store.locked():
            tokens = self.token_store.load()
            if force_refresh or self.wall_time() >= float(tokens.get("expires_at", 0)):
                tokens = self._refresh(tokens)
            return tokens["access_token"]

    # ------------------------------------------------------------------ HTTP

    def _params(
        self,
        since_id: str | None,
        max_results: int,
        pagination: str | None,
        start_time: datetime | None = None,
    ) -> dict:
        scheme = FIELD_NAMING[self.naming]
        params = {
            "max_results": str(max_results),
            scheme["param"]: scheme["fields"],
            "expansions": EXPANSIONS,
            "media.fields": MEDIA_FIELDS,
        }
        if since_id:
            params["since_id"] = since_id
        if start_time is not None:
            params["start_time"] = start_time.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        if pagination:
            params["pagination_token"] = pagination
        return params

    def _get(self, params_for: Callable[[], dict], token: str) -> tuple[int, dict, dict]:
        def once() -> tuple[int, dict, dict]:
            url = f"{API}/users/{self.author_id}/tweets?{urllib.parse.urlencode(params_for())}"
            try:
                resp = self.transport("GET", url, headers={"Authorization": f"Bearer {token}"})
            except (OSError, urllib.error.URLError, TimeoutError) as exc:
                raise SourceError(f"network error: {type(exc).__name__}") from None
            try:
                body = json.loads(resp.body) if resp.body else {}
            except json.JSONDecodeError:
                body = {}
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, body

        status, headers, body = once()
        if status == 400 and self.naming == "tweet" and _field_naming_error(body):
            self.naming = "post"
            status, headers, body = once()
        return status, headers, body

    def _rate(self, headers: dict) -> None:
        self.rate_limit = _int(headers.get("x-rate-limit-limit")) or self.rate_limit
        self.rate_remaining = _int(headers.get("x-rate-limit-remaining"))
        self.rate_reset = _int(headers.get("x-rate-limit-reset"))

    def _check(self, status: int, headers: dict) -> None:
        self._rate(headers)
        if status == 429:
            reset = self.rate_reset or (self.wall_time() + 60)
            self.blocked_until = reset
            raise SourceRateLimited(
                "rate limited (HTTP 429)", retry_after=max(1.0, reset - self.wall_time() + 1)
            )
        if status in (401, 403):
            raise SourceAuthError(f"timeline access refused (HTTP {status})")
        if status != 200:
            raise SourceError(f"timeline call failed (HTTP {status})")

    # ------------------------------------------------------------------ poll

    def poll(self) -> list[SourcePost]:
        if self.blocked_until is not None and self.wall_time() < self.blocked_until:
            raise SourceRateLimited(
                "rate limit window not yet reset",
                retry_after=self.blocked_until - self.wall_time() + 1,
            )
        self.blocked_until = None
        token = self._access_token()
        state = self.store.source_state(self.name)
        since_id = self.cursor()
        if since_id == "0":
            since_id = None  # a store written before round 2 (F7): the empty baseline marker
        if since_id is None and not state.get("baseline_at"):
            self._baseline(token)
            self._pending.reset([])
            return []
        # Review F7: after an empty baseline there is no id to read from; the baseline time bounds
        # the read instead, so nothing that existed before the first run is ever passed on.
        floor = datetime.fromisoformat(state["baseline_at"]) if since_id is None else None
        raw_posts: list[dict] = []
        media_by_key: dict[str, dict] = {}
        pagination: str | None = None
        refreshed = False
        pages = 0
        while True:
            status, headers, body = self._get(
                lambda p=pagination: self._params(since_id, self.max_results, p, floor), token
            )
            if status == 401 and not refreshed:
                refreshed = True  # an access token revoked early: refresh once, then retry
                token = self._access_token(force_refresh=True)
                continue
            self._check(status, headers)
            pages += 1
            raw_posts.extend(body.get("data") or [])
            for m in (body.get("includes") or {}).get("media", []) or []:
                if m.get("media_key"):
                    media_by_key[m["media_key"]] = m
            pagination = (body.get("meta") or {}).get("next_token")
            if not pagination:
                break
            if pages >= self.max_pages:
                raise SourceGap(
                    f"more than {self.max_pages} pages of new posts since the cursor; refusing to "
                    "pass on a partial backlog"
                )
        received = self.clock()
        posts: dict[str, SourcePost] = {}
        for raw in raw_posts:
            pid = str(raw.get("id", ""))
            if not pid.isdigit() or (since_id and int(pid) <= int(since_id)) or pid in posts:
                continue  # defensive: never re-deliver at or before the cursor, never twice
            post = self._to_post(raw, media_by_key, received)
            if floor is not None and post.published_at < floor:
                continue  # existed before the baseline
            posts[pid] = post
        ordered = sorted(posts.values(), key=lambda p: int(p.post_id))
        self._pending.reset([(p, {}) for p in ordered])
        return list(ordered)

    def _baseline(self, token: str) -> None:
        status, headers, body = self._get(lambda: self._params(None, 5, None), token)
        self._check(status, headers)
        ids = [str(p.get("id")) for p in body.get("data") or [] if p.get("id")]
        state = self.store.source_state(self.name)
        # An account with no posts yet has no id to start from: the baseline time is the floor
        # (review F7; "0" used to let every older post through to the freshness rule).
        state["since_id"] = newest_id(None, ids)
        state["baseline_at"] = self.clock().isoformat()
        self.store.put_source_state(self.name, state, self.clock())

    def _to_post(self, raw: dict, media_by_key: dict, received: datetime) -> SourcePost:
        record = to_archive_record(raw, media_by_key, received)
        try:
            post = SourcePost.from_g0_record(
                record,
                expected_author_id=self.author_id,
                provenance=Provenance.LIVE_API_USER_CONTEXT,
                received_at=received,
            )
        except (ValidationError, ValueError, KeyError, TypeError):
            raise SourceError(f"post {record['id']} could not be mapped") from None
        if not post.verified_author:
            self.author_mismatches += 1
        if (
            post.text_complete
            and not record["has_note"]
            and TRUNCATED_RE.search(record["short_text"])
        ):
            post = post.model_copy(update={"text_complete": False})
        if not post.text_complete:
            self.truncated_posts += 1
        return post


def _field_naming_error(body: dict) -> bool:
    blob = json.dumps(body).lower()
    return any(k in blob for k in ("tweet.fields", "note_tweet", "tweet_fields"))
