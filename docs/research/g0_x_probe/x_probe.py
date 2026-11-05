#!/usr/bin/env python3
"""G0 probe: can an approved follower read a protected X account through the official API?

Research tooling only (Agent B, see AGENTS.md). It contains no trading logic and never
touches a broker. It uses only the Python standard library.

What it does
  auth      OAuth 2.0 Authorization Code + PKCE as the subscriber; stores tokens locally.
  lookup    Resolve the target username to an ID and confirm protected=true.
  poll      Poll GET /2/users/{id}/tweets with since_id; record per-call and per-post metrics.
  controls  Bearer-token timeline call and user-token recent search (both expected to fail
            or return nothing for a protected account).
  report    Summarise a poll log against the G0 pass/fail criteria.
  backfill  Page backwards through the target's timeline (up to X's 3,200-post window) and
            store FULL posts locally for corpus building. Resumable. Optional chart download.
  corpus    Summarise a backfill corpus: posts per month, media, long bodies, quotes, replies.

What it stores
  poll stores NO post text by default: only a SHA-256 of the text and its length, plus IDs,
  timestamps, lag, media presence and edit-history counts. backfill is the exception: its
  purpose is a local evaluation corpus, so it stores full text and media under data/g0/corpus/.
  Research output lands under data/g0/ (git-ignored). OAuth tokens live in a separate local store.
"""
from __future__ import annotations

import argparse
import base64
import fcntl
import hashlib
import json
import math
import os
import pwd
import secrets
import stat
import statistics
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from contextlib import contextmanager
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from itertools import pairwise
from pathlib import Path

API = "https://api.x.com/2"
AUTH_URL = "https://x.com/i/oauth2/authorize"
TOKEN_URL = "https://api.x.com/2/oauth2/token"
SCOPES = ["tweet.read", "users.read", "offline.access"]
TWEET_FIELDS = (
    "created_at,edit_controls,edit_history_tweet_ids,referenced_tweets,attachments,"
    "conversation_id,in_reply_to_user_id,author_id,note_tweet"
)
# X is renaming tweet.* to post.* in its docs (2026). The live API has historically accepted
# tweet.fields; the endpoint reference now lists post.fields with note_post. The probe tries
# "tweet" naming first and falls back to "post" naming on a 400, recording which one worked.
FIELD_NAMING = {
    "tweet": {"param": "tweet.fields", "fields": TWEET_FIELDS},
    "post": {"param": "post.fields", "fields": TWEET_FIELDS.replace("note_tweet", "note_post")},
}
EXPANSIONS = "attachments.media_keys,referenced_tweets.id"
MEDIA_FIELDS = "media_key,type,url,preview_image_url,variants"
USER_FIELDS = "id,username,protected,created_at"

DATA_DIR = Path(os.environ.get("G0_DATA_DIR", "data/g0"))
# The account home comes from the OS. G0_DATA_DIR and shell environment overrides
# must never relocate credentials into a synced checkout.
TOKEN_FILE = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve() / ".config/tradingbot/g0/tokens.json"


# ----------------------------------------------------------------------------- pure helpers

def pkce_pair() -> tuple[str, str]:
    """Return (code_verifier, code_challenge) per RFC 7636, S256."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode("ascii")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def parse_x_time(value: str) -> datetime:
    """X returns ISO-8601 UTC like 2026-11-05T14:03:11.000Z."""
    return datetime.fromisoformat(value)


def lag_seconds(created_at: str, observed_at: datetime) -> float:
    return (observed_at - parse_x_time(created_at)).total_seconds()


def newest_id(current: str | None, ids: list[str]) -> str | None:
    """since_id must be the numerically largest post ID seen so far."""
    candidates = [int(i) for i in ids if i.isdigit()]
    if current and current.isdigit():
        candidates.append(int(current))
    return str(max(candidates)) if candidates else current


def redact_post(post: dict, media_by_key: dict[str, dict], observed_at: datetime,
                keep_text: bool = False) -> dict:
    """Reduce a post to metrics. Text is hashed unless keep_text is set."""
    short_text = post.get("text", "")
    note = post.get("note_tweet") or post.get("note_post") or {}
    text = note.get("text") or short_text
    media_keys = (post.get("attachments") or {}).get("media_keys", []) or []
    media = [media_by_key.get(k, {}) for k in media_keys]
    refs = post.get("referenced_tweets") or []
    record = {
        "id": post.get("id"),
        "author_id": post.get("author_id"),
        "created_at": post.get("created_at"),
        "lag_s": round(lag_seconds(post["created_at"], observed_at), 3) if post.get("created_at") else None,
        "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "text_len": len(text),
        "has_note": bool(note.get("text")),
        "short_text_len": len(short_text),
        "is_reply": bool(post.get("in_reply_to_user_id")) or any(r.get("type") == "replied_to" for r in refs),
        "ref_types": sorted({r.get("type", "") for r in refs}),
        "conversation_id": post.get("conversation_id"),
        "n_media_keys": len(media_keys),
        "media_types": [m.get("type") for m in media],
        "media_url_present": [bool(m.get("url") or m.get("preview_image_url") or m.get("variants")) for m in media],
        "n_edit_ids": len(post.get("edit_history_tweet_ids") or []),
        "edits_remaining": (post.get("edit_controls") or {}).get("edits_remaining"),
    }
    if keep_text:
        record["text"] = text
    return record


def percentile(values: list[float], pct: float) -> float:
    """Nearest-rank percentile (rounds up), so p95 is never below the median."""
    ordered = sorted(values)
    index = min(len(ordered) - 1, math.ceil(pct / 100 * len(ordered)) - 1)
    return round(ordered[max(0, index)], 1)


RATE_HEADER_PREFIXES = ("x-rate-limit-", "x-app-limit-", "x-user-limit-")
POST_READ_USD = 0.005  # pay-per-use list price per post read, deduplicated per UTC day


def rate_headers(headers: dict) -> dict:
    """Every X quota header (15-minute bucket plus 24-hour app/user caps), as ints where possible."""
    out: dict = {}
    for k, v in headers.items():
        if k.startswith(RATE_HEADER_PREFIXES):
            try:
                out[k] = int(v)
            except (TypeError, ValueError):
                out[k] = v
    return out


def parse_phases(spec: str) -> list[tuple[float, float]]:
    """'15:3600,5:1800' -> [(15.0, 3600.0), (5.0, 1800.0)]: poll interval seconds : phase duration seconds."""
    phases: list[tuple[float, float]] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        interval, duration = part.split(":")
        phases.append((float(interval), float(duration)))
    if not phases:
        raise ValueError("empty phase spec")
    return phases


def effective_interval(requested: float, limit_per_15min: int | None, headroom: float = 0.8) -> float:
    """The requested interval, or the slowest cadence the 15-minute quota allows with headroom if faster."""
    if not limit_per_15min or limit_per_15min <= 0:
        return requested
    floor = 900.0 / (headroom * limit_per_15min)
    return requested if requested >= floor else math.ceil(floor * 10) / 10


def enrich_lag(record: dict, created_at: str | None, received_at: datetime, prev_sent_at: datetime | None) -> dict:
    """Split publication-to-receipt lag into what the polling cadence can explain and what X itself took.

    lag_s            created_at -> response received: end-to-end, what a trading bot would see
    poll_window_s    previous call's send -> this call's receipt: the most the polling wait can account for
    x_delay_floor_s  the post existed this long before the previous call was sent yet was not returned then,
                     so X took at least this long to expose it (lower bound; 0 means nothing is provable)
    """
    if not created_at:
        return record
    created = parse_x_time(created_at)
    record["lag_s"] = round((received_at - created).total_seconds(), 3)
    if prev_sent_at is not None:
        record["poll_window_s"] = round((received_at - prev_sent_at).total_seconds(), 3)
        record["x_delay_floor_s"] = round(max(0.0, (prev_sent_at - created).total_seconds()), 3)
    return record


def pct_block(values: list[float]) -> dict | None:
    if not values:
        return None
    return {"n": len(values), "p50": percentile(values, 50), "p95": percentile(values, 95),
            "p99": percentile(values, 99), "max": round(max(values), 1), "min": round(min(values), 1)}


def looks_like_field_naming_error(body: dict) -> bool:
    """True when a 400 body complains about the tweet.fields parameter or the note_tweet field."""
    blob = json.dumps(body).lower()
    return any(k in blob for k in ("tweet.fields", "note_tweet", "tweet_fields"))


def summarize(records: list[dict], target_author_id: str | None = None) -> dict:
    """Aggregate a poll log (list of per-call dicts) into G0 metrics."""
    calls = [r for r in records if r.get("kind") == "call"]
    all_posts = [p for r in calls for p in r.get("posts", [])]
    posts = [p for p in all_posts if not p.get("duplicate")]
    statuses: dict[str, int] = {}
    for c in calls:
        statuses[str(c.get("status"))] = statuses.get(str(c.get("status")), 0) + 1
    lags = [p["lag_s"] for p in posts if p.get("lag_s") is not None]
    with_media = [p for p in posts if p.get("n_media_keys", 0) > 0]
    media_ok = sum(1 for p in with_media if p.get("media_url_present") and all(p["media_url_present"]))
    author_match = sum(1 for p in posts if target_author_id and p.get("author_id") == target_author_id)
    remaining = [c["rate_remaining"] for c in calls if isinstance(c.get("rate_remaining"), int)]
    unique_ids = {p["id"] for p in posts if p.get("id")}

    sent = sorted(parse_x_time(c["sent_at"]) for c in calls if c.get("sent_at"))
    gaps = [(b - a).total_seconds() for a, b in pairwise(sent)]
    elapsed = [c["elapsed_s"] for c in calls if isinstance(c.get("elapsed_s"), (int, float))]
    span = (sent[-1] - sent[0]).total_seconds() if len(sent) > 1 else 0.0

    by_interval: dict[str, dict] = {}
    for interval in sorted({c.get("interval_s") for c in calls if c.get("interval_s") is not None}):
        cs = [c for c in calls if c.get("interval_s") == interval]
        ps = [p for c in cs for p in c.get("posts", []) if not p.get("duplicate")]
        s = sorted(parse_x_time(c["sent_at"]) for c in cs if c.get("sent_at"))
        rem = [c["rate_remaining"] for c in cs if isinstance(c.get("rate_remaining"), int)]
        by_interval[str(interval)] = {
            "calls": len(cs), "new_posts": len(ps),
            "lag": pct_block([p["lag_s"] for p in ps if p.get("lag_s") is not None]),
            "gap": pct_block([(b - a).total_seconds() for a, b in pairwise(s)]),
            "elapsed": pct_block([c["elapsed_s"] for c in cs if isinstance(c.get("elapsed_s"), (int, float))]),
            "errors_4xx_5xx": sum(1 for c in cs if isinstance(c.get("status"), int) and c["status"] >= 400),
            "network_errors": sum(1 for c in cs if c.get("status") is None),
            "min_rate_remaining": min(rem) if rem else None,
        }

    rate_series: dict[str, list[int]] = {}
    for c in calls:
        for k, v in (c.get("rate") or {}).items():
            if isinstance(v, int):
                rate_series.setdefault(k, []).append(v)
    rate_summary = {k: {"min": min(v), "max": max(v)} for k, v in sorted(rate_series.items())}

    fetched = [m for p in posts for m in (p.get("media_fetch") or []) if isinstance(m, dict) and "status" in m]
    media_status: dict[str, int] = {}
    for m in fetched:
        media_status[str(m["status"])] = media_status.get(str(m["status"]), 0) + 1
    media = {"fetched": len(fetched), "status_counts": media_status,
             "elapsed": pct_block([m["elapsed_s"] for m in fetched if "elapsed_s" in m]),
             "media_lag": pct_block([m["media_lag_s"] for m in fetched if "media_lag_s" in m]),
             "bytes_total": sum(m.get("bytes", 0) for m in fetched)}

    bc = [c["bearer_control"] for c in calls if isinstance(c.get("bearer_control"), dict)]
    bc_status: dict[str, int] = {}
    for b in bc:
        bc_status[str(b.get("status"))] = bc_status.get(str(b.get("status")), 0) + 1
    bearer = {"calls": len(bc), "posts_returned": sum(b.get("n_posts", 0) for b in bc), "status_counts": bc_status,
              "error_types": sorted({t for b in bc for t in b.get("error_types", [])})}

    recon = next((r for r in records if r.get("kind") == "reconcile"), None)
    reconcile = None
    if recon:
        reconcile = {"status": recon.get("status"), "reported": recon.get("reported"), "polled": recon.get("polled"),
                     "missed": len(recon.get("missed_ids", [])), "vanished": len(recon.get("vanished_ids", [])),
                     "missed_ids": recon.get("missed_ids", []), "vanished_ids": recon.get("vanished_ids", [])}

    usage = [r for r in records if r.get("kind") == "usage"]
    before = next((u for u in usage if u.get("when") == "before"), None)
    after = next((u for u in usage if u.get("when") == "after"), None)
    cost = None
    if before and after and "project_usage" in before and "project_usage" in after:
        delta = after["project_usage"] - before["project_usage"]
        cost = {"reads_before": before["project_usage"], "reads_after": after["project_usage"],
                "delta_reads": delta, "usd_at_list_price": round(delta * POST_READ_USD, 4)}

    return {
        "calls": len(calls),
        "status_counts": statuses,
        "unique_posts": len(unique_ids),
        "duplicate_deliveries": len(all_posts) - len(posts),
        "edits_of_seen_posts": sum(1 for p in posts if p.get("edit_of_seen")),
        "posts_author_match": author_match,
        "lag_median_s": round(statistics.median(lags), 1) if lags else None,
        "lag_p95_s": percentile(lags, 95) if lags else None,
        "lag_p99_s": percentile(lags, 99) if lags else None,
        "lag_max_s": round(max(lags), 1) if lags else None,
        "lag_samples": len(lags),
        "poll_window": pct_block([p["poll_window_s"] for p in posts if p.get("poll_window_s") is not None]),
        "x_delay_floor": pct_block([p["x_delay_floor_s"] for p in posts if p.get("x_delay_floor_s") is not None]),
        "posts_with_media": len(with_media),
        "posts_with_media_url": media_ok,
        "posts_with_edit_history": sum(1 for p in posts if p.get("n_edit_ids", 0) > 1),
        "replies": sum(1 for p in posts if p.get("is_reply")),
        "min_rate_remaining": min(remaining) if remaining else None,
        "errors_4xx_5xx": sum(1 for c in calls if isinstance(c.get("status"), int) and c["status"] >= 400),
        "network_errors": sum(1 for c in calls if c.get("status") is None),
        "posts_with_long_body": sum(1 for p in posts if p.get("has_note")),
        "field_naming_used": sorted({c.get("field_naming") for c in calls if c.get("field_naming")}),
        "request_frequency": {"span_s": round(span, 1), "calls_per_min": round(60 * len(sent) / span, 2) if span else None,
                              "gap": pct_block(gaps), "elapsed": pct_block(elapsed)},
        "by_interval": by_interval,
        "rate_headers": rate_summary,
        "media_fetch": media,
        "bearer_control": bearer,
        "reconcile": reconcile,
        "cost": cost,
        "phases": [{k: r.get(k) for k in ("label", "requested_interval_s", "interval_s", "duration_s", "rate_limit_15min")}
                   for r in records if r.get("kind") == "phase"],
    }


def verdict(summary: dict, controls: dict | None) -> list[tuple[str, bool | None, str]]:
    """G0 pass/fail lines. None means not evaluable from the data given."""
    out: list[tuple[str, bool | None, str]] = []
    out.append(("Protected posts returned under user token",
                summary["posts_author_match"] > 0 if summary["unique_posts"] else None,
                f"{summary['posts_author_match']} posts by target author"))
    out.append(("Media URLs retrievable for posts with attachments",
                (summary["posts_with_media_url"] == summary["posts_with_media"]) if summary["posts_with_media"] else None,
                f"{summary['posts_with_media_url']}/{summary['posts_with_media']}"))
    out.append(("Median publication-to-receipt lag under 30 s",
                (summary["lag_median_s"] is not None and summary["lag_median_s"] < 30) if summary["lag_median_s"] is not None else None,
                (f"median {summary['lag_median_s']} s, p95 {summary['lag_p95_s']} s, p99 {summary.get('lag_p99_s')} s "
                 f"over {summary.get('lag_samples', 0)} posts")))
    xd = summary.get("x_delay_floor")
    out.append(("X-side exposure delay (lower bound, informational)", None,
                f"p50 {xd['p50']} s, p95 {xd['p95']} s, max {xd['max']} s" if xd else "no samples"))
    out.append(("No 4xx/5xx on timeline calls",
                summary["errors_4xx_5xx"] == 0 if summary["calls"] else None,
                f"{summary['errors_4xx_5xx']} errors in {summary['calls']} calls"))
    out.append(("No duplicate deliveries", summary.get("duplicate_deliveries", 0) == 0 if summary["unique_posts"] else None,
                f"{summary.get('duplicate_deliveries', 0)} duplicates, {summary.get('edits_of_seen_posts', 0)} edits of seen posts"))
    recon = summary.get("reconcile")
    if recon:
        out.append(("No posts missed by polling (end-of-run reconciliation)", recon["missed"] == 0,
                    (f"X reports {recon['reported']} posts since baseline; polling delivered {recon['polled']}; "
                     f"missed {recon['missed']}, vanished {recon['vanished']}")))
    bc = summary.get("bearer_control") or {}
    if bc.get("calls"):
        out.append(("In-run bearer-token control returns no protected posts", bc["posts_returned"] == 0,
                    (f"{bc['calls']} control calls, statuses {bc['status_counts']}, posts {bc['posts_returned']}, "
                     f"errors {bc['error_types']}")))
    cost = summary.get("cost")
    if cost:
        out.append(("Metered reads consistent with the per-day dedup rule (informational)", None,
                    (f"{cost['delta_reads']} billable reads during the run (${cost['usd_at_list_price']}) for "
                     f"{summary['unique_posts']} new posts")))
    if controls:
        bearer = controls.get("bearer_timeline", {})
        out.append(("Bearer-token control returns no protected posts",
                    bearer.get("n_posts", 0) == 0,
                    f"status {bearer.get('status')} posts {bearer.get('n_posts')}"))
        search = controls.get("user_search", {})
        out.append(("Recent search (informational, expected empty)", None,
                    f"status {search.get('status')} posts {search.get('n_posts')}"))
    return out


# ----------------------------------------------------------------------------- HTTP and auth

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # API credentials and token-exchange bodies stay at the original endpoint.


def http(method: str, url: str, *, headers: dict | None = None, data: bytes | None = None,
         timeout: float = 20.0) -> tuple[int, dict, bytes, float]:
    if (headers or data) and urllib.parse.urlsplit(url)[:2] != ("https", "api.x.com"):
        raise ValueError("Credentialed requests require the X API origin")
    req = urllib.request.Request(url, method=method, headers=headers or {}, data=data)
    t0 = time.monotonic()
    try:
        with urllib.request.build_opener(NoRedirect).open(req, timeout=timeout) as resp:
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read(), time.monotonic() - t0
    except urllib.error.HTTPError as exc:
        return exc.code, {k.lower(): v for k, v in exc.headers.items()}, exc.read(), time.monotonic() - t0


def client_config() -> tuple[str, str | None, str]:
    client_id = os.environ.get("X_CLIENT_ID")
    if not client_id:
        sys.exit("Set X_CLIENT_ID (from the X Developer Console app). Never commit it.")
    return client_id, os.environ.get("X_CLIENT_SECRET") or None, os.environ.get("X_REDIRECT_URI", "http://127.0.0.1:8765/callback")


def token_headers(client_id: str, client_secret: str | None) -> dict:
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    if client_secret:
        basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        headers["Authorization"] = f"Basic {basic}"
    return headers


@contextmanager
def credential_store():
    """Serialize auth/refresh and refuse an absent, redirected or shared local store."""
    directory = TOKEN_FILE.parent
    if (directory.resolve() != directory or directory.is_relative_to(DATA_DIR.resolve())
            or directory.is_relative_to(Path(__file__).resolve().parents[3])
            or any(part.lower().startswith("dropbox") or part.lower() == "cloudstorage"
                   for part in directory.parts)):
        sys.exit("OAuth storage must be outside the project and cloud storage, without symlinks.")
    try:
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        sys.exit("OAuth directory unavailable. Set up the private directory before running auth.")
    try:
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            sys.exit("OAuth directory must belong to the current user with mode 0700.")
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            info = TOKEN_FILE.lstat()
        except FileNotFoundError:
            pass  # A new authorization can create the file in this verified directory.
        else:
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600):
                sys.exit("OAuth token file must be a private, single-link regular file with mode 0600.")
        yield
    finally:
        os.close(fd)


def _save_tokens(payload: dict) -> None:
    """Called under the credential store lock after a successful token exchange."""
    if not isinstance(payload.get("access_token"), str) or not payload["access_token"]:
        sys.exit("Token endpoint omitted the access token.")
    try:
        expires_at = time.time() + float(payload.get("expires_in", 7200)) - 60
    except (ValueError, TypeError, OverflowError):
        sys.exit("Invalid token lifetime.")
    with tempfile.NamedTemporaryFile(dir=TOKEN_FILE.parent, mode="w", delete=False) as tmp:
        try:
            json.dump({**payload, "expires_at": expires_at}, tmp)
            tmp.flush()
            os.fsync(tmp.fileno())
            tmp.close()
            os.replace(tmp.name, TOKEN_FILE)
        finally:
            Path(tmp.name).unlink(missing_ok=True)


def _refresh_tokens(tokens: dict) -> dict:
    client_id, client_secret, _ = client_config()
    body = {"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"], "client_id": client_id}
    status, _, raw, _ = http("POST", TOKEN_URL, headers=token_headers(client_id, client_secret),
                             data=urllib.parse.urlencode(body).encode())
    if status != 200:
        sys.exit(f"Refresh failed (HTTP {status}).")
    new = json.loads(raw)
    new.setdefault("refresh_token", tokens["refresh_token"])
    _save_tokens(new)
    return new


def user_token() -> str:
    with credential_store():
        try:
            fd = os.open(TOKEN_FILE, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd) as f:
                tokens = json.load(f)
        except FileNotFoundError:
            sys.exit("No OAuth tokens in the private store. Run x_probe.py auth.")
        if time.time() >= tokens.get("expires_at", 0):
            if not tokens.get("refresh_token"):
                sys.exit("Refresh token missing. Run x_probe.py auth.")
            tokens = _refresh_tokens(tokens)
        return tokens["access_token"]


def cmd_auth(_: argparse.Namespace) -> None:
    with credential_store():
        _authorize()


def _authorize() -> None:
    client_id, client_secret, redirect_uri = client_config()
    verifier, challenge = pkce_pair()
    state = secrets.token_urlsafe(16)
    params = {
        "response_type": "code", "client_id": client_id, "redirect_uri": redirect_uri,
        "scope": " ".join(SCOPES), "state": state, "code_challenge": challenge, "code_challenge_method": "S256",
    }
    url = f"{AUTH_URL}?{urllib.parse.urlencode(params)}"
    parsed = urllib.parse.urlparse(redirect_uri)
    if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.path != "/callback" or parsed.username:
        sys.exit("Configure the OAuth redirect as http://127.0.0.1:<port>/callback.")
    captured: dict = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            callback = urllib.parse.urlsplit(self.path)
            if callback.path != parsed.path:
                self.send_error(404)
                return
            qs = urllib.parse.parse_qs(callback.query)
            captured.update({k: v[0] for k, v in qs.items()})
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Authorization received. You can close this tab.")

        def log_message(self, *args):  # silence
            return

    server = HTTPServer((parsed.hostname or "127.0.0.1", parsed.port or 80), Handler)
    try:
        server.timeout = 180
        print("Opening browser for X authorization. Log in as the SUBSCRIBER account that follows the target.")
        if not webbrowser.open(url):
            sys.exit("Could not open the OAuth authorization browser.")
        server.handle_request()
    finally:
        server.server_close()
    if captured.get("state") != state or "code" not in captured:
        sys.exit("Authorization failed or callback state mismatch.")
    body = {
        "grant_type": "authorization_code", "code": captured["code"], "client_id": client_id,
        "redirect_uri": redirect_uri, "code_verifier": verifier,
    }
    status, _, raw, _ = http("POST", TOKEN_URL, headers=token_headers(client_id, client_secret),
                             data=urllib.parse.urlencode(body).encode())
    if status != 200:
        sys.exit(f"Token exchange failed (HTTP {status}).")
    tokens = json.loads(raw)
    _save_tokens(tokens)
    print(f"Tokens stored in {TOKEN_FILE} (mode 600). "
          f"Refresh token present: {'refresh_token' in tokens}")


# ----------------------------------------------------------------------------- API commands

def api_get(path: str, params: dict, token: str) -> tuple[int, dict, dict, float]:
    url = f"{API}{path}?{urllib.parse.urlencode(params)}"
    status, headers, raw, elapsed = http("GET", url, headers={"Authorization": f"Bearer {token}"})
    try:
        body = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        body = {}
    return status, headers, body, elapsed


def log_errors(body: dict) -> list[dict]:
    """Keep the control evidence while discarding server-supplied descriptions and request echoes."""
    errors = []
    for error in body.get("errors") or []:
        safe = {"type": "unspecified"}
        if isinstance(error, dict):
            kind = error.get("type")
            if kind in ("https://api.x.com/2/problems/not-authorized-for-resource",
                        "https://api.twitter.com/2/problems/not-authorized-for-resource"):
                safe["type"] = kind
            resource = error.get("resource_id")
            if isinstance(resource, str) and resource.isascii() and resource.isdigit() and len(resource) <= 20:
                safe["resource_id"] = resource
        errors.append(safe)
    return errors


def rate_info(headers: dict) -> tuple[int | None, int | None]:
    def to_int(v):
        try:
            return int(v)
        except (TypeError, ValueError):
            return None
    return to_int(headers.get("x-rate-limit-remaining")), to_int(headers.get("x-rate-limit-reset"))


def cmd_lookup(args: argparse.Namespace) -> None:
    status, headers, body, _ = api_get(f"/users/by/username/{args.username}", {"user.fields": USER_FIELDS}, user_token())
    print(json.dumps({"status": status, "rate": rate_info(headers), "data": body.get("data"), "errors": log_errors(body)}, indent=2))
    if status == 200 and body.get("data"):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        (DATA_DIR / "target.json").write_text(json.dumps(body["data"], indent=2))
        print(f"Saved target to {DATA_DIR / 'target.json'}. protected={body['data'].get('protected')}")


def load_target() -> dict:
    path = DATA_DIR / "target.json"
    if not path.exists():
        sys.exit("No target. Run: x_probe.py lookup <username>")
    return json.loads(path.read_text())


def timeline_params(since_id: str | None, max_results: int, naming: str = "tweet") -> dict:
    scheme = FIELD_NAMING[naming]
    params = {
        "max_results": str(max(5, min(100, max_results))),
        scheme["param"]: scheme["fields"], "expansions": EXPANSIONS, "media.fields": MEDIA_FIELDS,
    }
    if since_id:
        params["since_id"] = since_id
    return params


def timeline_get(user_id: str, params_for: callable, token: str, naming: str) -> tuple[int, dict, dict, float, str]:
    """GET the user timeline; on a 400 about field naming, retry once with the other scheme."""
    status, headers, body, elapsed = api_get(f"/users/{user_id}/tweets", params_for(naming), token)
    if status == 400 and naming == "tweet" and looks_like_field_naming_error(body):
        naming = "post"
        print("400 on tweet.fields naming; retrying with post.fields naming")
        status, headers, body, elapsed = api_get(f"/users/{user_id}/tweets", params_for(naming), token)
    return status, headers, body, elapsed, naming


def sleep_for_rate_limit(reset: int | None) -> None:
    wait = max(1.0, (reset or time.time() + 60) - time.time() + 1)
    print(f"Rate limited; sleeping {wait:.0f}s until reset")
    time.sleep(min(wait, 900))


def usage_snapshot(bearer: str | None) -> dict | None:
    """GET /2/usage/tweets with the app-only bearer: X's own running count of billable post reads."""
    if not bearer:
        return None
    status, _, body, _ = api_get("/usage/tweets", {}, bearer)
    data = body.get("data") or {}
    snap: dict = {"at": datetime.now(UTC).isoformat(), "status": status}
    if status == 200 and data:
        snap.update({"project_usage": int(data.get("project_usage", 0)), "project_cap": int(data.get("project_cap", 0)),
                     "cap_reset_day": data.get("cap_reset_day")})
    else:
        snap["errors"] = log_errors(body)
    return snap


def bearer_control_call(user_id: str, since_id: str | None, bearer: str, naming: str) -> dict:
    """Ask for the same new posts with the app-only token. Expected: no data, not-authorized-for-resource."""
    status, headers, body, elapsed = api_get(f"/users/{user_id}/tweets", timeline_params(since_id, 10, naming), bearer)
    errs = log_errors(body)
    return {"status": status, "n_posts": len(body.get("data") or []), "elapsed_s": round(elapsed, 3),
            "error_types": sorted({e.get("type", "") for e in errs if isinstance(e, dict)}), "rate": rate_headers(headers)}


def media_url(media: dict) -> str | None:
    url = media.get("url") or media.get("preview_image_url")
    if url and urllib.parse.urlsplit(url)[:2] in (("https", "pbs.twimg.com"), ("https", "video.twimg.com")):
        return url
    return None


def fetch_media_timed(media_items: list[dict], created_at: str | None) -> list[dict]:
    """GET every media URL of a post (bytes discarded); record status, size, elapsed and media lag."""
    out: list[dict] = []
    for m in media_items:
        url = media_url(m)
        if not url:
            out.append({"media_key": m.get("media_key"), "skipped": "no_trusted_url"})
            continue
        status, headers, raw, elapsed = http("GET", url)
        done = datetime.now(UTC)
        item = {"media_key": m.get("media_key"), "type": m.get("type"), "status": status, "bytes": len(raw),
                "elapsed_s": round(elapsed, 3), "content_type": (headers.get("content-type") or "").split(";")[0]}
        if created_at:
            item["media_lag_s"] = round((done - parse_x_time(created_at)).total_seconds(), 3)
        out.append(item)
    return out


def safe_user_token(attempts: int = 5) -> str | None:
    """user_token() exits on a failed refresh; during a long poll we retry instead of dying."""
    for i in range(attempts):
        try:
            return user_token()
        except SystemExit as exc:
            print(f"Token refresh problem ({exc}); retry {i + 1}/{attempts}")
            time.sleep(min(60, 5 * (i + 1)))
    return None


def wait_until(start_at: str | None) -> None:
    if not start_at:
        return
    target = parse_x_time(start_at)
    delay = (target - datetime.now(UTC)).total_seconds()
    if delay > 0:
        print(f"Waiting {delay:.0f}s until {target.isoformat()}", flush=True)
        time.sleep(delay)


def reconcile(user_id: str, baseline_since_id: str | None, naming: str, seen: dict) -> dict:
    """Re-read everything X now reports since the baseline and diff it against what polling delivered."""
    token = safe_user_token()
    reported: dict[str, str | None] = {}
    pagination: str | None = None
    pages = 0
    status: int | None = None
    if token is not None:
        while True:
            def params_for(nm: str, _tok: str | None = pagination) -> dict:
                p = timeline_params(baseline_since_id, 100, nm)
                if _tok:
                    p["pagination_token"] = _tok
                return p
            status, _, body, _, naming = timeline_get(user_id, params_for, token, naming)
            pages += 1
            for p in body.get("data", []):
                reported[p["id"]] = p.get("created_at")
            pagination = (body.get("meta") or {}).get("next_token")
            if status != 200 or not pagination or pages >= 10:
                break
    polled = {i for i in seen if baseline_since_id is None or (i.isdigit() and int(i) > int(baseline_since_id))}
    return {"status": status, "pages": pages, "reported": len(reported), "polled": len(polled),
            "missed_ids": sorted(set(reported) - polled), "vanished_ids": sorted(polled - set(reported)),
            "at": datetime.now(UTC).isoformat()}


def cmd_poll(args: argparse.Namespace) -> None:
    target = load_target()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    phases = parse_phases(args.phases) if args.phases else [(args.interval, args.duration)]
    bearer = os.environ.get("X_BEARER_TOKEN")  # read from the environment only; never written to disk or logs
    wait_until(args.start_at)
    log_path = DATA_DIR / f"poll_{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.jsonl"
    print(f"Polling @{target.get('username')} (id {target['id']}) phases={phases} -> {log_path}", flush=True)
    naming = "tweet"
    seen: dict[str, str] = {}  # post id -> first receipt time
    new_target_posts = 0
    prev_sent: datetime | None = None
    since_id: str | None = None
    last_limit: int | None = None
    log = log_path.open("a")

    def write(rec: dict) -> None:
        log.write(json.dumps(rec) + "\n")
        log.flush()

    def run_phase(requested: float, duration: float, label: str, stop_when_posts: int | None = None) -> str:
        nonlocal naming, since_id, prev_sent, last_limit, new_target_posts
        interval = effective_interval(requested, last_limit)
        write({"kind": "phase", "label": label, "requested_interval_s": requested, "interval_s": interval,
               "duration_s": duration, "rate_limit_15min": last_limit, "started": datetime.now(UTC).isoformat()})
        if interval != requested:
            print(f"Phase {label}: {requested}s requested; the 15-minute quota ({last_limit}) only supports {interval}s "
                  f"with 20% headroom", flush=True)
        print(f"Phase {label}: interval {interval}s for {duration:.0f}s", flush=True)
        deadline = time.monotonic() + duration
        next_send = time.monotonic()
        calls = 0
        failures = 0
        while time.monotonic() < deadline:
            if stop_when_posts and new_target_posts >= stop_when_posts:
                return "enough"
            token = safe_user_token()
            if token is None:
                write({"kind": "call", "phase": label, "interval_s": interval, "status": None, "error": "no_token",
                       "sent_at": datetime.now(UTC).isoformat(), "n_posts": 0, "posts": []})
                return "no_token"
            sent = datetime.now(UTC)
            try:
                status, headers, body, elapsed, naming = timeline_get(
                    target["id"], lambda nm, _s=since_id: timeline_params(_s, args.max_results, nm), token, naming)
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                received = datetime.now(UTC)
                write({"kind": "call", "phase": label, "interval_s": interval, "sent_at": sent.isoformat(),
                       "received_at": received.isoformat(), "status": None, "error": type(exc).__name__,
                       "since_id": since_id, "n_posts": 0, "posts": []})
                failures += 1
                print(f"{sent.strftime('%H:%M:%S')} network error: {type(exc).__name__}", flush=True)
                if failures >= 10:
                    print("Ten consecutive network failures; ending phase", flush=True)
                    return "network"
                next_send = max(next_send + interval, time.monotonic())
                time.sleep(max(0.0, next_send - time.monotonic()))
                continue
            failures = 0
            received = datetime.now(UTC)
            rh = rate_headers(headers)
            last_limit = rh.get("x-rate-limit-limit", last_limit)
            media_by_key = {m.get("media_key"): m for m in (body.get("includes") or {}).get("media", [])}
            posts: list[dict] = []
            n_new = 0
            for raw_post in body.get("data", []):
                rec = redact_post(raw_post, media_by_key, received, args.keep_text)
                enrich_lag(rec, raw_post.get("created_at"), received, prev_sent)
                pid = rec.get("id")
                rec["duplicate"] = pid in seen
                rec["edit_of_seen"] = any(e in seen for e in (raw_post.get("edit_history_tweet_ids") or []) if e != pid)
                if not rec["duplicate"]:
                    seen[pid] = received.isoformat()
                    n_new += 1
                    if rec.get("author_id") == target["id"]:
                        new_target_posts += 1
                    if args.fetch_media and rec.get("n_media_keys"):
                        keys = (raw_post.get("attachments") or {}).get("media_keys", []) or []
                        rec["media_fetch"] = fetch_media_timed([media_by_key.get(k, {}) for k in keys],
                                                               raw_post.get("created_at"))
                posts.append(rec)
            record = {
                "kind": "call", "phase": label, "interval_s": interval, "sent_at": sent.isoformat(),
                "received_at": received.isoformat(), "observed_at": sent.isoformat(), "status": status,
                "elapsed_s": round(elapsed, 3), "field_naming": naming, "rate": rh,
                "rate_remaining": rh.get("x-rate-limit-remaining"), "rate_reset": rh.get("x-rate-limit-reset"),
                "since_id": since_id, "n_posts": len(posts), "n_new": n_new, "posts": posts, "errors": log_errors(body),
                "result_count": (body.get("meta") or {}).get("result_count"),
            }
            if n_new and bearer:
                record["bearer_control"] = bearer_control_call(target["id"], since_id, bearer, naming)
            write(record)
            prev_sent = sent
            since_id = newest_id(since_id, [p["id"] for p in posts if p.get("id")])
            calls += 1
            if posts or calls % max(1, int(300 / max(interval, 1))) == 1:
                lags = [p["lag_s"] for p in posts if p.get("lag_s") is not None and not p.get("duplicate")]
                print(f"{received.strftime('%H:%M:%S')} [{label} {interval}s] status={status} posts={len(posts)} new={n_new} "
                      f"remaining={rh.get('x-rate-limit-remaining')} lag={','.join(str(round(v, 1)) for v in lags) or '-'}",
                      flush=True)
            if status == 429:
                sleep_for_rate_limit(rh.get("x-rate-limit-reset"))
                next_send = time.monotonic()
            elif status in (401, 403):
                print("Auth or permission failure; stopping. This is a G0 FAIL signal if the token is the subscriber's.", flush=True)
                return "denied"
            else:
                next_send = max(next_send + interval, time.monotonic() - interval)
                time.sleep(max(0.0, next_send - time.monotonic()))
        return "done"

    try:
        write({"kind": "meta", "target": target, "phases": phases, "interval_s": phases[0][0], "keep_text": args.keep_text,
               "fetch_media": args.fetch_media, "bearer_control": bool(bearer), "max_results": args.max_results,
               "started": datetime.now(UTC).isoformat()})
        usage0 = usage_snapshot(bearer)
        if usage0:
            write({"kind": "usage", "when": "before", **usage0})
        # Baseline: learn the newest existing post ID so every later call asks only for newer posts.
        # Baseline posts are not lag samples (they are old) and are read again free under the daily dedup.
        token = safe_user_token()
        if token is None:
            sys.exit("Could not obtain a user token")
        sent = datetime.now(UTC)
        status, headers, body, elapsed, naming = timeline_get(target["id"], lambda nm: timeline_params(None, 5, nm), token, naming)
        received = datetime.now(UTC)
        base_posts = body.get("data", [])
        since_id = newest_id(None, [p["id"] for p in base_posts if p.get("id")])
        baseline_since_id = since_id
        last_limit = rate_headers(headers).get("x-rate-limit-limit")
        for p in base_posts:
            seen[p["id"]] = received.isoformat()
        write({"kind": "baseline", "sent_at": sent.isoformat(), "received_at": received.isoformat(), "status": status,
               "elapsed_s": round(elapsed, 3), "field_naming": naming, "rate": rate_headers(headers), "since_id": since_id,
               "posts": [{"id": p.get("id"), "created_at": p.get("created_at")} for p in base_posts], "errors": log_errors(body)})
        print(f"Baseline: status={status} newest_id={since_id} rate_limit_15min={last_limit}", flush=True)
        if status in (401, 403):
            sys.exit("Baseline call denied; check the subscriber token")
        prev_sent = sent

        outcome = "done"
        for i, (interval, duration) in enumerate(phases, 1):
            outcome = run_phase(interval, duration, f"p{i}")
            if outcome in ("denied", "no_token"):
                break
        if outcome not in ("denied", "no_token") and args.extend_until and new_target_posts < args.min_posts:
            remaining = (parse_x_time(args.extend_until) - datetime.now(UTC)).total_seconds()
            if remaining > 0:
                print(f"Only {new_target_posts} new target posts after the schedule; extending at {args.extend_interval}s "
                      f"until {args.extend_until} or {args.min_posts} posts", flush=True)
                outcome = run_phase(args.extend_interval, remaining, "extend", stop_when_posts=args.min_posts)
        if outcome not in ("denied", "no_token"):
            write({"kind": "reconcile", **reconcile(target["id"], baseline_since_id, naming, seen)})
        usage1 = usage_snapshot(bearer)
        if usage1:
            write({"kind": "usage", "when": "after", **usage1})
        write({"kind": "end", "outcome": outcome, "new_target_posts": new_target_posts,
               "finished": datetime.now(UTC).isoformat()})
    finally:
        log.close()
    print(f"Done ({outcome}). Summarise with: x_probe.py report {log_path}", flush=True)


MEDIA_EXT = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "image/gif": ".gif"}


def corpus_record(post: dict, media_by_key: dict[str, dict], observed_at: datetime) -> dict:
    """Full local record for corpus building (paid content: local only)."""
    note = post.get("note_tweet") or post.get("note_post") or {}
    media_keys = (post.get("attachments") or {}).get("media_keys", []) or []
    refs = post.get("referenced_tweets") or []
    return {
        "id": post.get("id"), "author_id": post.get("author_id"), "created_at": post.get("created_at"),
        "conversation_id": post.get("conversation_id"),
        "text": note.get("text") or post.get("text", ""), "has_note": bool(note.get("text")),
        "short_text": post.get("text", ""),
        "referenced": [{"type": r.get("type"), "id": r.get("id")} for r in refs],
        "is_reply": bool(post.get("in_reply_to_user_id")) or any(r.get("type") == "replied_to" for r in refs),
        "is_quote": any(r.get("type") == "quoted" for r in refs),
        "edit_history_ids": post.get("edit_history_tweet_ids") or [],
        "media": [media_by_key.get(k, {"media_key": k}) for k in media_keys],
        "observed_at": observed_at.isoformat(),
    }


def corpus_summary(records: list[dict]) -> dict:
    by_month: dict[str, int] = {}
    for r in records:
        month = (r.get("created_at") or "")[:7] or "unknown"
        by_month[month] = by_month.get(month, 0) + 1
    ids = {r.get("id") for r in records if r.get("id")}
    created = sorted(r["created_at"] for r in records if r.get("created_at"))
    return {
        "posts": len(records), "unique_ids": len(ids),
        "oldest": created[0] if created else None, "newest": created[-1] if created else None,
        "by_month": dict(sorted(by_month.items())),
        "with_media": sum(1 for r in records if r.get("media")),
        "with_long_body": sum(1 for r in records if r.get("has_note")),
        "quotes": sum(1 for r in records if r.get("is_quote")),
        "replies": sum(1 for r in records if r.get("is_reply")),
        "edited": sum(1 for r in records if len(r.get("edit_history_ids") or []) > 1),
        "authors": sorted({r.get("author_id") for r in records if r.get("author_id")}),
    }


def download_media(media: dict, dest_dir: Path) -> dict:
    url = media_url(media)
    if not url:
        return {"skipped": "no_trusted_url"}
    key = media.get("media_key", "media")
    existing = next(dest_dir.glob(f"{key}.*"), None)
    if existing:
        return {"file": existing.name, "cached": True}
    status, headers, raw, _ = http("GET", url)
    if status != 200:
        return {"status": status}
    ext = MEDIA_EXT.get((headers.get("content-type") or "").split(";")[0].strip(), ".bin")
    path = dest_dir / f"{key}{ext}"
    path.write_bytes(raw)
    return {"file": path.name, "bytes": len(raw), "status": status}


def cmd_backfill(args: argparse.Namespace) -> None:
    target = load_target()
    corpus_dir = DATA_DIR / "corpus"
    media_dir = corpus_dir / "media"
    corpus_dir.mkdir(parents=True, exist_ok=True)
    state_path = corpus_dir / "backfill_state.json"
    state = json.loads(state_path.read_text()) if (args.resume and state_path.exists()) else {}
    next_token = args.pagination_token or state.get("next_token")
    out_path = Path(state["out"]) if state.get("out") else corpus_dir / f"posts_{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.jsonl"
    fetched = state.get("fetched", 0)
    naming = state.get("naming", "tweet")
    print(f"Backfilling @{target.get('username')} (id {target['id']}) -> {out_path}"
          + (" resuming from saved cursor" if next_token else "")
          + f"; window start={args.start_time or '-'} end={args.end_time or '-'} max_posts={args.max_posts}")
    print("NOTE: this stores full post text (paid content) locally under data/g0/. Never commit it.")

    def params_for(nm: str) -> dict:
        params = timeline_params(None, 100, nm)
        if args.start_time:
            params["start_time"] = args.start_time
        if args.end_time:
            params["end_time"] = args.end_time
        if args.until_id:
            params["until_id"] = args.until_id
        if next_token:
            params["pagination_token"] = next_token
        return params

    pages = 0
    with out_path.open("a") as out:
        while True:
            observed = datetime.now(UTC)
            status, headers, body, _elapsed, naming = timeline_get(target["id"], params_for, user_token(), naming)
            remaining, reset = rate_info(headers)
            if status == 429:
                sleep_for_rate_limit(reset)
                continue
            if status != 200:
                print(f"Stopped: HTTP {status}")
                break
            media_by_key = {m.get("media_key"): m for m in (body.get("includes") or {}).get("media", [])}
            posts = body.get("data", [])
            for post in posts:
                rec = corpus_record(post, media_by_key, observed)
                if args.download_media and rec["media"]:
                    media_dir.mkdir(parents=True, exist_ok=True)
                    rec["media_files"] = [download_media(m, media_dir) for m in rec["media"]]
                out.write(json.dumps(rec) + "\n")
            out.flush()
            fetched += len(posts)
            pages += 1
            next_token = (body.get("meta") or {}).get("next_token")
            state_path.write_text(json.dumps({"next_token": next_token, "fetched": fetched, "out": str(out_path),
                                              "naming": naming, "updated": observed.isoformat()}))
            oldest = min((p.get("created_at", "") for p in posts), default="-")
            print(f"page {pages}: {len(posts)} posts (oldest {oldest}) total {fetched} remaining={remaining} naming={naming}")
            if not next_token:
                print("Reached the end of the available timeline window.")
                break
            if fetched >= args.max_posts:
                print(f"Reached --max-posts {args.max_posts}; rerun with --resume to continue.")
                break
            time.sleep(args.pause)
    records = [json.loads(line) for line in out_path.read_text().splitlines() if line.strip()]
    print(json.dumps(corpus_summary(records), indent=2))
    print(f"Corpus: {out_path}. Summarise again with: x_probe.py corpus {out_path}")


def cmd_corpus(args: argparse.Namespace) -> None:
    records = [json.loads(line) for line in Path(args.corpus).read_text().splitlines() if line.strip()]
    print(json.dumps(corpus_summary(records), indent=2))


def cmd_controls(args: argparse.Namespace) -> None:
    target = load_target()
    results: dict = {}
    bearer = os.environ.get("X_BEARER_TOKEN")
    if bearer:
        status, headers, body, _ = api_get(f"/users/{target['id']}/tweets", timeline_params(None, 10), bearer)
        results["bearer_timeline"] = {"status": status, "n_posts": len(body.get("data", [])), "errors": log_errors(body),
                                      "rate": rate_info(headers)}
    else:
        print("X_BEARER_TOKEN not set; skipping bearer control (set it from the app's Keys and Tokens page).")
    status, headers, body, _ = api_get("/tweets/search/recent",
                                       {"query": f"from:{target['username']}", "max_results": "10", "tweet.fields": "created_at,author_id"},
                                       user_token())
    results["user_search"] = {"status": status, "n_posts": len(body.get("data", [])), "errors": log_errors(body),
                              "rate": rate_info(headers)}
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "controls.json").write_text(json.dumps(results, indent=2))
    print(json.dumps(results, indent=2))


def cmd_report(args: argparse.Namespace) -> None:
    records = [json.loads(line) for line in Path(args.log).read_text().splitlines() if line.strip()]
    meta = next((r for r in records if r.get("kind") == "meta"), {})
    target_id = (meta.get("target") or {}).get("id")
    summary = summarize(records, target_id)
    controls_path = DATA_DIR / "controls.json"
    controls = json.loads(controls_path.read_text()) if controls_path.exists() else None
    print(json.dumps(summary, indent=2))
    print("\nG0 criteria")
    for name, ok, detail in verdict(summary, controls):
        flag = "PASS" if ok else ("FAIL" if ok is False else "n/a ")
        print(f"  [{flag}] {name}: {detail}")
    if summary["by_interval"]:
        print("\nBy polling interval")
        print(f"  {'interval':>8} {'calls':>6} {'posts':>6} {'lag p50':>8} {'p95':>6} {'p99':>6} {'gap p50':>8} {'gap p95':>8} "
              f"{'req p50':>8} {'req p95':>8} {'min rem':>8}")
        for interval, b in summary["by_interval"].items():
            lag = b["lag"] or {}
            gap = b["gap"] or {}
            el = b["elapsed"] or {}
            print(f"  {interval:>8} {b['calls']:>6} {b['new_posts']:>6} {lag.get('p50', '-')!s:>8} {lag.get('p95', '-')!s:>6} "
                  f"{lag.get('p99', '-')!s:>6} {gap.get('p50', '-')!s:>8} {gap.get('p95', '-')!s:>8} "
                  f"{el.get('p50', '-')!s:>8} {el.get('p95', '-')!s:>8} {b['min_rate_remaining']!s:>8}")
    print(f"\nLong posts (over 280 chars) with full body present: {summary['posts_with_long_body']}; "
          f"field naming accepted by the API: {summary['field_naming_used'] or 'n/a'}")
    print("Also check manually: edits (n_edit_ids > 1), replies and threads, and whether the subscriber's follow was revoked.")
    if args.json:
        Path(args.json).write_text(json.dumps(summary, indent=2))
        print(f"Summary written to {args.json}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("auth", help="OAuth 2.0 PKCE login as the subscriber").set_defaults(fn=cmd_auth)
    p = sub.add_parser("lookup", help="Resolve target username; confirm protected")
    p.add_argument("username")
    p.set_defaults(fn=cmd_lookup)
    p = sub.add_parser("poll", help="Poll the target's post timeline")
    p.add_argument("--interval", type=float, default=15.0, help="seconds between calls (default 15)")
    p.add_argument("--duration", type=float, default=7200.0, help="total seconds to run (default 7200)")
    p.add_argument("--max-results", type=int, default=100)
    p.add_argument("--fetch-media", action="store_true", help="GET the first media URL per call and record status/bytes")
    p.add_argument("--keep-text", action="store_true", help="store post text (manual inspection only; never commit)")
    p.add_argument("--phases", help="interval:duration pairs, e.g. 15:3600,5:1800,3:1800 (overrides --interval/--duration)")
    p.add_argument("--start-at", help="ISO-8601 UTC time to begin, e.g. 2026-11-05T14:30:00Z (sleeps until then)")
    p.add_argument("--extend-until", help="if fewer than --min-posts new target posts arrived, keep polling until this UTC time")
    p.add_argument("--extend-interval", type=float, default=15.0, help="interval for the extension phase (default 15)")
    p.add_argument("--min-posts", type=int, default=3, help="new target posts that make the extension unnecessary (default 3)")
    p.set_defaults(fn=cmd_poll)
    sub.add_parser("controls", help="Bearer timeline and user search controls").set_defaults(fn=cmd_controls)
    p = sub.add_parser("backfill", help="Page backwards through the timeline and store full posts locally")
    p.add_argument("--start-time", help="ISO-8601 UTC lower bound, e.g. 2025-01-01T00:00:00Z")
    p.add_argument("--end-time", help="ISO-8601 UTC upper bound")
    p.add_argument("--until-id", help="only posts older than this post ID")
    p.add_argument("--max-posts", type=int, default=3200, help="stop after this many (API window is 3,200)")
    p.add_argument("--pause", type=float, default=1.0, help="seconds between pages (default 1)")
    p.add_argument("--pagination-token", help="start from this token (from a previous run)")
    p.add_argument("--resume", action="store_true", help="continue from data/g0/corpus/backfill_state.json")
    p.add_argument("--download-media", action="store_true", help="save chart images under data/g0/corpus/media/")
    p.set_defaults(fn=cmd_backfill)
    p = sub.add_parser("corpus", help="Summarise a backfill corpus file")
    p.add_argument("corpus")
    p.set_defaults(fn=cmd_corpus)
    p = sub.add_parser("report", help="Summarise a poll log")
    p.add_argument("log")
    p.add_argument("--json", help="also write the summary dict to this path")
    p.set_defaults(fn=cmd_report)
    args = parser.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
