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

What it stores
  By default NO post text: only a SHA-256 of the text and its length, plus IDs, timestamps,
  lag, media presence and edit-history counts. Pass --keep-text only for a short manual
  inspection and never commit the output. Everything lands under data/g0/ (git-ignored).
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import http.server
import json
import math
import os
import secrets
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime, timezone
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
TOKEN_FILE = DATA_DIR / "tokens.json"


# ----------------------------------------------------------------------------- pure helpers

def pkce_pair() -> tuple[str, str]:
    """Return (code_verifier, code_challenge) per RFC 7636, S256."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode("ascii")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def parse_x_time(value: str) -> datetime:
    """X returns ISO-8601 UTC like 2026-11-05T14:03:11.000Z."""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


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


def looks_like_field_naming_error(body: dict) -> bool:
    """True when a 400 body complains about the tweet.fields parameter or the note_tweet field."""
    blob = json.dumps(body).lower()
    return any(k in blob for k in ("tweet.fields", "note_tweet", "tweet_fields"))


def summarize(records: list[dict], target_author_id: str | None = None) -> dict:
    """Aggregate a poll log (list of per-call dicts) into G0 metrics."""
    calls = [r for r in records if r.get("kind") == "call"]
    posts = [p for r in calls for p in r.get("posts", [])]
    statuses: dict[str, int] = {}
    for c in calls:
        statuses[str(c.get("status"))] = statuses.get(str(c.get("status")), 0) + 1
    lags = [p["lag_s"] for p in posts if p.get("lag_s") is not None]
    with_media = [p for p in posts if p.get("n_media_keys", 0) > 0]
    media_ok = sum(1 for p in with_media if p.get("media_url_present") and all(p["media_url_present"]))
    author_match = sum(1 for p in posts if target_author_id and p.get("author_id") == target_author_id)
    remaining = [c["rate_remaining"] for c in calls if isinstance(c.get("rate_remaining"), int)]
    unique_ids = {p["id"] for p in posts if p.get("id")}
    return {
        "calls": len(calls),
        "status_counts": statuses,
        "unique_posts": len(unique_ids),
        "posts_author_match": author_match,
        "lag_median_s": round(statistics.median(lags), 1) if lags else None,
        "lag_p95_s": percentile(lags, 95) if lags else None,
        "posts_with_media": len(with_media),
        "posts_with_media_url": media_ok,
        "posts_with_edit_history": sum(1 for p in posts if p.get("n_edit_ids", 0) > 1),
        "replies": sum(1 for p in posts if p.get("is_reply")),
        "min_rate_remaining": min(remaining) if remaining else None,
        "errors_4xx_5xx": sum(1 for c in calls if isinstance(c.get("status"), int) and c["status"] >= 400),
        "posts_with_long_body": sum(1 for p in posts if p.get("has_note")),
        "field_naming_used": sorted({c.get("field_naming") for c in calls if c.get("field_naming")}),
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
                f"median {summary['lag_median_s']} s, p95 {summary['lag_p95_s']} s"))
    out.append(("No 4xx/5xx on timeline calls",
                summary["errors_4xx_5xx"] == 0 if summary["calls"] else None,
                f"{summary['errors_4xx_5xx']} errors in {summary['calls']} calls"))
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

def http(method: str, url: str, *, headers: dict | None = None, data: bytes | None = None,
         timeout: float = 20.0) -> tuple[int, dict, bytes, float]:
    req = urllib.request.Request(url, method=method, headers=headers or {}, data=data)
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
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


def save_tokens(payload: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    payload["expires_at"] = time.time() + float(payload.get("expires_in", 7200)) - 60
    TOKEN_FILE.write_text(json.dumps(payload, indent=2))
    os.chmod(TOKEN_FILE, 0o600)


def load_tokens() -> dict:
    if not TOKEN_FILE.exists():
        sys.exit("No tokens. Run: x_probe.py auth")
    return json.loads(TOKEN_FILE.read_text())


def refresh_tokens(tokens: dict) -> dict:
    client_id, client_secret, _ = client_config()
    body = {"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"], "client_id": client_id}
    status, _, raw, _ = http("POST", TOKEN_URL, headers=token_headers(client_id, client_secret),
                             data=urllib.parse.urlencode(body).encode())
    if status != 200:
        sys.exit(f"Refresh failed {status}: {raw[:300]!r}")
    new = json.loads(raw)
    new.setdefault("refresh_token", tokens["refresh_token"])
    save_tokens(new)
    return new


def user_token() -> str:
    tokens = load_tokens()
    if time.time() >= tokens.get("expires_at", 0):
        tokens = refresh_tokens(tokens)
    return tokens["access_token"]


def cmd_auth(_: argparse.Namespace) -> None:
    client_id, client_secret, redirect_uri = client_config()
    verifier, challenge = pkce_pair()
    state = secrets.token_urlsafe(16)
    params = {
        "response_type": "code", "client_id": client_id, "redirect_uri": redirect_uri,
        "scope": " ".join(SCOPES), "state": state, "code_challenge": challenge, "code_challenge_method": "S256",
    }
    url = f"{AUTH_URL}?{urllib.parse.urlencode(params)}"
    parsed = urllib.parse.urlparse(redirect_uri)
    captured: dict = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            captured.update({k: v[0] for k, v in qs.items()})
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Authorization received. You can close this tab.")

        def log_message(self, *args):  # silence
            return

    server = http.server.HTTPServer((parsed.hostname or "127.0.0.1", parsed.port or 80), Handler)
    print("Opening browser for X authorization. Log in as the SUBSCRIBER account that follows the target.")
    print(url)
    webbrowser.open(url)
    server.handle_request()
    server.server_close()
    if captured.get("state") != state or "code" not in captured:
        sys.exit(f"Authorization failed or state mismatch: {captured}")
    body = {
        "grant_type": "authorization_code", "code": captured["code"], "client_id": client_id,
        "redirect_uri": redirect_uri, "code_verifier": verifier,
    }
    status, _, raw, _ = http("POST", TOKEN_URL, headers=token_headers(client_id, client_secret),
                             data=urllib.parse.urlencode(body).encode())
    if status != 200:
        sys.exit(f"Token exchange failed {status}: {raw[:300]!r}")
    tokens = json.loads(raw)
    save_tokens(tokens)
    print(f"Tokens stored in {TOKEN_FILE} (mode 600). Scopes: {tokens.get('scope')}. "
          f"Refresh token present: {'refresh_token' in tokens}")


# ----------------------------------------------------------------------------- API commands

def api_get(path: str, params: dict, token: str) -> tuple[int, dict, dict, float]:
    url = f"{API}{path}?{urllib.parse.urlencode(params)}"
    status, headers, raw, elapsed = http("GET", url, headers={"Authorization": f"Bearer {token}"})
    try:
        body = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        body = {"raw": raw[:500].decode("utf-8", "replace")}
    return status, headers, body, elapsed


def rate_info(headers: dict) -> tuple[int | None, int | None]:
    def to_int(v):
        try:
            return int(v)
        except (TypeError, ValueError):
            return None
    return to_int(headers.get("x-rate-limit-remaining")), to_int(headers.get("x-rate-limit-reset"))


def cmd_lookup(args: argparse.Namespace) -> None:
    status, headers, body, _ = api_get(f"/users/by/username/{args.username}", {"user.fields": USER_FIELDS}, user_token())
    print(json.dumps({"status": status, "rate": rate_info(headers), "data": body.get("data"), "errors": body.get("errors")}, indent=2))
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


def cmd_poll(args: argparse.Namespace) -> None:
    target = load_target()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    log_path = DATA_DIR / f"poll_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.jsonl"
    print(f"Polling @{target.get('username')} (id {target['id']}) every {args.interval}s for {args.duration}s -> {log_path}")
    since_id: str | None = None
    naming = "tweet"
    deadline = time.monotonic() + args.duration
    with log_path.open("a") as log:
        log.write(json.dumps({"kind": "meta", "target": target, "interval_s": args.interval,
                              "keep_text": args.keep_text, "started": datetime.now(timezone.utc).isoformat()}) + "\n")
        while time.monotonic() < deadline:
            observed = datetime.now(timezone.utc)
            status, headers, body, elapsed = api_get(f"/users/{target['id']}/tweets",
                                                     timeline_params(since_id, args.max_results, naming), user_token())
            if status == 400 and naming == "tweet" and looks_like_field_naming_error(body):
                naming = "post"
                print("400 on tweet.fields naming; retrying with post.fields naming")
                status, headers, body, elapsed = api_get(f"/users/{target['id']}/tweets",
                                                         timeline_params(since_id, args.max_results, naming), user_token())
            remaining, reset = rate_info(headers)
            media_by_key = {m.get("media_key"): m for m in (body.get("includes") or {}).get("media", [])}
            posts = [redact_post(p, media_by_key, observed, args.keep_text) for p in body.get("data", [])]
            if args.fetch_media and posts and media_by_key:
                first = next((m for m in media_by_key.values() if m.get("url") or m.get("preview_image_url")), None)
                if first:
                    murl = first.get("url") or first.get("preview_image_url")
                    mstatus, mheaders, mraw, _ = http("GET", murl, headers={"Authorization": f"Bearer {user_token()}"})
                    posts[0]["media_fetch"] = {"status": mstatus, "bytes": len(mraw), "type": mheaders.get("content-type")}
            record = {
                "kind": "call", "observed_at": observed.isoformat(), "status": status, "elapsed_s": round(elapsed, 3),
                "field_naming": naming,
                "rate_remaining": remaining, "rate_reset": reset, "since_id": since_id,
                "n_posts": len(posts), "posts": posts, "errors": body.get("errors"),
                "result_count": (body.get("meta") or {}).get("result_count"),
            }
            log.write(json.dumps(record) + "\n")
            log.flush()
            since_id = newest_id(since_id, [p["id"] for p in posts if p.get("id")])
            print(f"{observed.strftime('%H:%M:%S')} status={status} posts={len(posts)} remaining={remaining} "
                  f"lag_med={statistics.median([p['lag_s'] for p in posts]) if posts else '-'}")
            if status == 429 and reset:
                wait = max(1.0, reset - time.time() + 1)
                print(f"Rate limited; sleeping {wait:.0f}s until reset")
                time.sleep(min(wait, 900))
            elif status in (401, 403):
                print("Auth or permission failure; stopping. This is a G0 FAIL signal if the token is the subscriber's.")
                break
            else:
                time.sleep(args.interval)
    print(f"Done. Summarise with: x_probe.py report {log_path}")


def cmd_controls(args: argparse.Namespace) -> None:
    target = load_target()
    results: dict = {}
    bearer = os.environ.get("X_BEARER_TOKEN")
    if bearer:
        status, headers, body, _ = api_get(f"/users/{target['id']}/tweets", timeline_params(None, 10), bearer)
        results["bearer_timeline"] = {"status": status, "n_posts": len(body.get("data", [])), "errors": body.get("errors"),
                                      "rate": rate_info(headers)}
    else:
        print("X_BEARER_TOKEN not set; skipping bearer control (set it from the app's Keys and Tokens page).")
    status, headers, body, _ = api_get("/tweets/search/recent",
                                       {"query": f"from:{target['username']}", "max_results": "10", "tweet.fields": "created_at,author_id"},
                                       user_token())
    results["user_search"] = {"status": status, "n_posts": len(body.get("data", [])), "errors": body.get("errors"),
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
    print(f"\nLong posts (over 280 chars) with full body present: {summary['posts_with_long_body']}; "
          f"field naming accepted by the API: {summary['field_naming_used'] or 'n/a'}")
    print("Also check manually: edits (n_edit_ids > 1), replies and threads, and whether the subscriber's follow was revoked.")


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
    p.set_defaults(fn=cmd_poll)
    sub.add_parser("controls", help="Bearer timeline and user search controls").set_defaults(fn=cmd_controls)
    p = sub.add_parser("report", help="Summarise a poll log")
    p.add_argument("log")
    p.set_defaults(fn=cmd_report)
    args = parser.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
