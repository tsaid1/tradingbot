"""Offline tests for the pure parts of the G0 probe. No network. Run from the repo root:

    python -m pytest docs/research/g0_x_probe -q
"""
import base64
import hashlib
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import x_probe


def test_pkce_pair_is_s256_of_verifier():
    verifier, challenge = x_probe.pkce_pair()
    assert 43 <= len(verifier) <= 128
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert challenge == expected
    assert "=" not in challenge and "=" not in verifier


def test_lag_from_x_timestamp():
    created = "2026-11-05T14:00:00.000Z"
    observed = datetime(2026, 11, 5, 14, 0, 12, 500000, tzinfo=UTC)
    assert x_probe.lag_seconds(created, observed) == 12.5


def test_newest_id_is_numeric_max_not_lexicographic():
    assert x_probe.newest_id(None, ["99", "100"]) == "100"
    assert x_probe.newest_id("150", ["99", "100"]) == "150"
    assert x_probe.newest_id(None, []) is None
    assert x_probe.newest_id("7", []) == "7"


def test_redact_post_hashes_text_by_default_and_reports_media():
    observed = datetime(2026, 11, 5, 14, 0, 30, tzinfo=UTC)
    post = {
        "id": "1", "author_id": "A", "created_at": "2026-11-05T14:00:00.000Z",
        "text": "BUY ABC @ 100", "attachments": {"media_keys": ["3_1", "3_2"]},
        "edit_history_tweet_ids": ["0", "1"], "edit_controls": {"edits_remaining": 4},
        "referenced_tweets": [{"type": "replied_to", "id": "0"}],
    }
    media = {"3_1": {"media_key": "3_1", "type": "photo", "url": "https://pbs/x.jpg"},
             "3_2": {"media_key": "3_2", "type": "video", "variants": [{"url": "https://v/x.mp4"}]}}
    rec = x_probe.redact_post(post, media, observed)
    assert "text" not in rec
    assert rec["text_sha256"] == hashlib.sha256(b"BUY ABC @ 100").hexdigest()
    assert rec["text_len"] == 13
    assert rec["lag_s"] == 30.0
    assert rec["is_reply"] is True
    assert rec["media_types"] == ["photo", "video"]
    assert rec["media_url_present"] == [True, True]
    assert rec["n_edit_ids"] == 2
    kept = x_probe.redact_post(post, media, observed, keep_text=True)
    assert kept["text"] == "BUY ABC @ 100"


def _call(status, posts, remaining=800):
    return {"kind": "call", "status": status, "rate_remaining": remaining, "posts": posts}


def test_summarize_and_verdict_pass_case():
    posts = [
        {"id": "1", "author_id": "T", "lag_s": 10.0, "n_media_keys": 1, "media_url_present": [True], "n_edit_ids": 1},
        {"id": "2", "author_id": "T", "lag_s": 20.0, "n_media_keys": 0, "media_url_present": [], "n_edit_ids": 2, "is_reply": True},
    ]
    records = [{"kind": "meta"}, _call(200, posts), _call(200, [])]
    s = x_probe.summarize(records, "T")
    assert s["calls"] == 2 and s["unique_posts"] == 2 and s["posts_author_match"] == 2
    assert s["lag_median_s"] == 15.0 and s["posts_with_media"] == 1 and s["posts_with_media_url"] == 1
    assert s["posts_with_edit_history"] == 1 and s["replies"] == 1 and s["errors_4xx_5xx"] == 0
    lines = x_probe.verdict(s, {"bearer_timeline": {"status": 401, "n_posts": 0}, "user_search": {"status": 200, "n_posts": 0}})
    results = {name: ok for name, ok, _ in lines}
    assert results["Protected posts returned under user token"] is True
    assert results["Media URLs retrievable for posts with attachments"] is True
    assert results["Median publication-to-receipt lag under 30 s"] is True
    assert results["No 4xx/5xx on timeline calls"] is True
    assert results["Bearer-token control returns no protected posts"] is True


def test_summarize_and_verdict_fail_case():
    records = [_call(403, []), _call(403, [])]
    s = x_probe.summarize(records, "T")
    assert s["errors_4xx_5xx"] == 2 and s["unique_posts"] == 0
    lines = {name: ok for name, ok, _ in x_probe.verdict(s, {"bearer_timeline": {"status": 200, "n_posts": 5}})}
    assert lines["No 4xx/5xx on timeline calls"] is False
    assert lines["Protected posts returned under user token"] is None  # nothing returned, not evaluable
    assert lines["Bearer-token control returns no protected posts"] is False


def test_percentile_nearest_rank_never_below_median():
    assert x_probe.percentile([9.2, 22.0], 95) == 22.0
    assert x_probe.percentile([5.0], 95) == 5.0
    vals = [float(i) for i in range(1, 101)]
    assert x_probe.percentile(vals, 95) == 95.0
    assert x_probe.percentile(vals, 50) == 50.0


def test_timeline_params_clamps_and_sets_since_id():
    p = x_probe.timeline_params("42", 500)
    assert p["max_results"] == "100" and p["since_id"] == "42"
    assert "since_id" not in x_probe.timeline_params(None, 1)
    assert x_probe.timeline_params(None, 1)["max_results"] == "5"


def test_long_post_body_is_used_when_present():
    observed = datetime(2026, 11, 5, 14, 0, 30, tzinfo=UTC)
    long_body = "x" * 600
    post = {"id": "5", "author_id": "T", "created_at": "2026-11-05T14:00:00.000Z",
            "text": "x" * 280, "note_tweet": {"text": long_body}}
    rec = x_probe.redact_post(post, {}, observed)
    assert rec["has_note"] is True and rec["text_len"] == 600 and rec["short_text_len"] == 280
    assert rec["text_sha256"] == hashlib.sha256(long_body.encode()).hexdigest()
    post_new_naming = dict(post); del post_new_naming["note_tweet"]; post_new_naming["note_post"] = {"text": long_body}
    assert x_probe.redact_post(post_new_naming, {}, observed)["has_note"] is True


def test_field_naming_schemes_and_error_detection():
    old = x_probe.timeline_params(None, 10, "tweet")
    new = x_probe.timeline_params(None, 10, "post")
    assert "note_tweet" in old["tweet.fields"] and "post.fields" not in old
    assert "note_post" in new["post.fields"] and "tweet.fields" not in new
    assert x_probe.looks_like_field_naming_error({"errors": [{"message": "The query parameter [tweet.fields] is not one of [post.fields]"}]})
    assert not x_probe.looks_like_field_naming_error({"errors": [{"message": "Rate limit exceeded"}]})


def test_corpus_record_and_summary():
    observed = datetime(2026, 11, 5, 14, 0, 30, tzinfo=UTC)
    media = {"3_9": {"media_key": "3_9", "type": "photo", "url": "https://pbs/x.png"}}
    posts = [
        {"id": "1", "author_id": "T", "created_at": "2026-10-02T13:00:00.000Z", "text": "a",
         "attachments": {"media_keys": ["3_9"]}, "referenced_tweets": [{"type": "quoted", "id": "0"}],
         "edit_history_tweet_ids": ["1"]},
        {"id": "2", "author_id": "T", "created_at": "2026-11-03T13:00:00.000Z", "text": "b" * 280,
         "note_tweet": {"text": "b" * 500}, "edit_history_tweet_ids": ["2", "2b"],
         "in_reply_to_user_id": "T"},
    ]
    recs = [x_probe.corpus_record(p, media, observed) for p in posts]
    assert recs[0]["is_quote"] and recs[0]["media"][0]["url"] == "https://pbs/x.png"
    assert recs[1]["has_note"] and len(recs[1]["text"]) == 500 and recs[1]["is_reply"]
    s = x_probe.corpus_summary(recs)
    assert s["posts"] == 2 and s["by_month"] == {"2026-10": 1, "2026-11": 1}
    assert s["with_media"] == 1 and s["with_long_body"] == 1 and s["quotes"] == 1
    assert s["replies"] == 1 and s["edited"] == 1 and s["authors"] == ["T"]
    assert s["oldest"].startswith("2026-10-02") and s["newest"].startswith("2026-11-03")


def test_backfill_pages_until_no_token_and_is_resumable(tmp_path, monkeypatch, capsys):
    import argparse
    import json
    monkeypatch.setattr(x_probe, "DATA_DIR", tmp_path)
    (tmp_path / "target.json").write_text(json.dumps({"id": "T", "username": "demo"}))
    monkeypatch.setattr(x_probe, "user_token", lambda: "tok")
    monkeypatch.setattr(x_probe.time, "sleep", lambda s: None)
    pages = {
        None: ({"data": [{"id": "30", "author_id": "T", "created_at": "2026-11-03T00:00:00.000Z", "text": "c"},
                         {"id": "20", "author_id": "T", "created_at": "2026-11-02T00:00:00.000Z", "text": "b"}],
                "meta": {"next_token": "p2"}}),
        "p2": ({"data": [{"id": "10", "author_id": "T", "created_at": "2026-11-01T00:00:00.000Z", "text": "a"}],
                "meta": {}}),
    }
    seen = []

    def fake_timeline_get(user_id, params_for, token, naming):
        params = params_for(naming)
        seen.append(params.get("pagination_token"))
        assert params["max_results"] == "100" and params.get("start_time") == "2026-01-01T00:00:00Z"
        body = pages[params.get("pagination_token")]
        return 200, {"x-rate-limit-remaining": "899"}, body, 0.1, naming

    monkeypatch.setattr(x_probe, "timeline_get", fake_timeline_get)
    args = argparse.Namespace(start_time="2026-01-01T00:00:00Z", end_time=None, until_id=None, max_posts=3200,
                              pause=0, pagination_token=None, resume=False, download_media=False)
    x_probe.cmd_backfill(args)
    assert seen == [None, "p2"]
    files = list((tmp_path / "corpus").glob("posts_*.jsonl"))
    assert len(files) == 1
    recs = [json.loads(l) for l in files[0].read_text().splitlines()]
    assert [r["id"] for r in recs] == ["30", "20", "10"]
    state = json.loads((tmp_path / "corpus" / "backfill_state.json").read_text())
    assert state["fetched"] == 3 and state["next_token"] is None
    out = capsys.readouterr().out
    assert "Reached the end" in out and '"posts": 3' in out
    # --max-posts stops early and leaves a resumable token
    seen.clear()
    for f in files: f.unlink()
    args2 = argparse.Namespace(**{**vars(args), "max_posts": 2})
    x_probe.cmd_backfill(args2)
    state = json.loads((tmp_path / "corpus" / "backfill_state.json").read_text())
    assert state["fetched"] == 2 and state["next_token"] == "p2"
    args3 = argparse.Namespace(**{**vars(args), "resume": True})
    x_probe.cmd_backfill(args3)
    state = json.loads((tmp_path / "corpus" / "backfill_state.json").read_text())
    assert state["fetched"] == 3 and state["next_token"] is None and seen[-1] == "p2"


def test_auth_flow_exchanges_code_without_a_real_browser(tmp_path, monkeypatch, capsys):
    """Drives cmd_auth end to end with a fake browser that hits the local callback."""
    import argparse
    import json
    import threading
    import urllib.request
    monkeypatch.setattr(x_probe, "DATA_DIR", tmp_path)
    monkeypatch.setattr(x_probe, "TOKEN_FILE", tmp_path / "tokens.json")
    monkeypatch.setenv("X_CLIENT_ID", "cid")
    monkeypatch.setenv("X_REDIRECT_URI", "http://127.0.0.1:8799/callback")
    monkeypatch.delenv("X_CLIENT_SECRET", raising=False)

    def fake_open(url):
        state = dict(p.split("=") for p in url.split("?", 1)[1].split("&"))["state"]
        def hit():
            urllib.request.urlopen(f"http://127.0.0.1:8799/callback?code=abc&state={state}", timeout=5).read()
        threading.Timer(0.2, hit).start()
        return True

    monkeypatch.setattr(x_probe.webbrowser, "open", fake_open)
    posted = {}

    def fake_http(method, url, *, headers=None, data=None, timeout=20.0):
        posted.update({"url": url, "body": data.decode()})
        return 200, {}, json.dumps({"access_token": "A", "refresh_token": "R", "expires_in": 7200, "scope": "tweet.read"}).encode(), 0.1

    monkeypatch.setattr(x_probe, "http", fake_http)
    x_probe.cmd_auth(argparse.Namespace())
    assert posted["url"] == x_probe.TOKEN_URL and "code=abc" in posted["body"] and "code_verifier=" in posted["body"]
    saved = json.loads((tmp_path / "tokens.json").read_text())
    assert saved["refresh_token"] == "R" and saved["expires_at"] > 0
    assert "Refresh token present: True" in capsys.readouterr().out


def test_parse_phases_and_effective_interval():
    assert x_probe.parse_phases("15:3600, 5:1800,3:1800") == [(15.0, 3600.0), (5.0, 1800.0), (3.0, 1800.0)]
    assert x_probe.effective_interval(15, 900) == 15
    assert x_probe.effective_interval(3, 900) == 3          # 300 calls per 15 min, inside 80% of 900
    assert x_probe.effective_interval(3, 180) == 6.3        # 900 / (0.8 * 180) = 6.25 -> 6.3
    assert x_probe.effective_interval(5, None) == 5         # unknown limit: trust the request


def test_enrich_lag_splits_polling_wait_from_x_side_delay():
    created = "2026-11-05T14:00:00.000Z"
    prev_sent = datetime(2026, 11, 5, 14, 0, 5, tzinfo=UTC)      # post existed 5 s before this call, not returned
    received = datetime(2026, 11, 5, 14, 0, 20, 500000, tzinfo=UTC)
    rec = x_probe.enrich_lag({}, created, received, prev_sent)
    assert rec["lag_s"] == 20.5 and rec["poll_window_s"] == 15.5 and rec["x_delay_floor_s"] == 5.0
    fresh = x_probe.enrich_lag({}, "2026-11-05T14:00:10.000Z", received, prev_sent)
    assert fresh["x_delay_floor_s"] == 0.0                               # created after the previous call: not provable
    assert x_probe.enrich_lag({}, None, received, prev_sent) == {}


def test_summarize_reports_p99_duplicates_intervals_reconcile_and_cost():
    def call(sent, interval, posts, elapsed=0.4, rate=None, bearer=None):
        rec = {"kind": "call", "status": 200, "sent_at": sent, "interval_s": interval, "elapsed_s": elapsed,
               "rate": rate or {"x-rate-limit-limit": 900, "x-rate-limit-remaining": 890, "x-app-limit-24hour-remaining": 9000},
               "rate_remaining": 890, "posts": posts}
        if bearer:
            rec["bearer_control"] = bearer
        return rec
    p1 = {"id": "11", "author_id": "T", "lag_s": 8.0, "poll_window_s": 15.4, "x_delay_floor_s": 0.0, "n_media_keys": 1,
          "media_url_present": [True], "media_fetch": [{"status": 200, "bytes": 1000, "elapsed_s": 0.3, "media_lag_s": 9.1}]}
    p1_dup = dict(p1, duplicate=True)
    p2 = {"id": "12", "author_id": "T", "lag_s": 4.0, "poll_window_s": 5.3, "x_delay_floor_s": 2.0, "n_media_keys": 0, "media_url_present": []}
    records = [
        {"kind": "meta", "target": {"id": "T"}},
        {"kind": "usage", "when": "before", "project_usage": 3189},
        {"kind": "phase", "label": "p1", "requested_interval_s": 15, "interval_s": 15, "duration_s": 60, "rate_limit_15min": 900},
        call("2026-11-05T14:30:00+00:00", 15, []),
        call("2026-11-05T14:30:15+00:00", 15, [p1], bearer={"status": 200, "n_posts": 0, "error_types": ["x/not-authorized-for-resource"]}),
        call("2026-11-05T14:30:30+00:00", 15, [p1_dup]),
        call("2026-11-05T14:31:00+00:00", 5, [p2], elapsed=0.6),
        call("2026-11-05T14:31:05+00:00", 5, []),
        {"kind": "reconcile", "status": 200, "reported": 3, "polled": 2, "missed_ids": ["13"], "vanished_ids": []},
        {"kind": "usage", "when": "after", "project_usage": 3191},
    ]
    s = x_probe.summarize(records, "T")
    assert s["unique_posts"] == 2 and s["duplicate_deliveries"] == 1 and s["lag_samples"] == 2
    assert s["lag_median_s"] == 6.0 and s["lag_p95_s"] == 8.0 and s["lag_p99_s"] == 8.0 and s["lag_max_s"] == 8.0
    assert s["x_delay_floor"]["max"] == 2.0 and s["poll_window"]["p50"] == 5.3
    assert set(s["by_interval"]) == {"15", "5"}
    assert s["by_interval"]["15"]["calls"] == 3 and s["by_interval"]["15"]["new_posts"] == 1
    assert s["by_interval"]["15"]["gap"]["p50"] == 15.0 and s["by_interval"]["5"]["gap"]["p50"] == 5.0
    assert s["request_frequency"]["span_s"] == 65.0 and s["request_frequency"]["elapsed"]["max"] == 0.6
    assert s["rate_headers"]["x-app-limit-24hour-remaining"] == {"min": 9000, "max": 9000}
    assert s["media_fetch"]["fetched"] == 1 and s["media_fetch"]["media_lag"]["p50"] == 9.1
    assert s["bearer_control"] == {"calls": 1, "posts_returned": 0, "status_counts": {"200": 1},
                                   "error_types": ["x/not-authorized-for-resource"]}
    assert s["reconcile"]["missed"] == 1 and s["reconcile"]["missed_ids"] == ["13"]
    assert s["cost"] == {"reads_before": 3189, "reads_after": 3191, "delta_reads": 2, "usd_at_list_price": 0.01}
    assert s["phases"][0]["label"] == "p1"
    lines = {name: ok for name, ok, _ in x_probe.verdict(s, None)}
    assert lines["No duplicate deliveries"] is False
    assert lines["No posts missed by polling (end-of-run reconciliation)"] is False
    assert lines["In-run bearer-token control returns no protected posts"] is True
    assert lines["Median publication-to-receipt lag under 30 s"] is True


def test_poll_end_to_end_with_fake_clock(tmp_path, monkeypatch, capsys):
    """Baseline, three phase calls at a fixed cadence, a duplicate, media timing, bearer control, reconcile, usage."""
    import argparse
    import json
    monkeypatch.setattr(x_probe, "DATA_DIR", tmp_path)
    (tmp_path / "target.json").write_text(json.dumps({"id": "T", "username": "demo"}))
    monkeypatch.setattr(x_probe, "user_token", lambda: "tok")
    monkeypatch.setenv("X_BEARER_TOKEN", "app-only")
    clock = [1000.0]
    monkeypatch.setattr(x_probe.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(x_probe.time, "sleep", lambda s: clock.__setitem__(0, clock[0] + s))
    now = datetime.now(UTC)
    fresh = (now - __import__("datetime").timedelta(seconds=3)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    old = "2026-11-01T00:00:00.000Z"
    timeline_calls = []

    def fake_timeline_get(user_id, params_for, token, naming):
        params = params_for(naming)
        timeline_calls.append(params)
        headers = {"x-rate-limit-limit": "900", "x-rate-limit-remaining": str(900 - len(timeline_calls)), "x-app-limit-24hour-limit": "100000"}
        if params.get("since_id") is None:                       # baseline
            return 200, headers, {"data": [{"id": "100", "author_id": "T", "created_at": old, "text": "old"}]}, 0.2, naming
        if params["max_results"] == "100" and params["since_id"] == "100" and len(timeline_calls) >= 5:   # reconcile
            return 200, headers, {"data": [{"id": "101", "created_at": fresh}, {"id": "102", "created_at": fresh}], "meta": {}}, 0.2, naming
        n = len(timeline_calls)
        if n == 2:
            return 200, headers, {"data": [], "meta": {"result_count": 0}}, 0.3, naming
        post = {"id": "101", "author_id": "T", "created_at": fresh, "text": "BUY", "attachments": {"media_keys": ["3_1"]},
                "edit_history_tweet_ids": ["101"]}
        body = {"data": [post], "includes": {"media": [{"media_key": "3_1", "type": "photo", "url": "https://pbs/x.png"}]},
                "meta": {"result_count": 1}}
        return 200, headers, body, 0.3, naming                 # call 3: new post; call 4: same post again (duplicate)

    monkeypatch.setattr(x_probe, "timeline_get", fake_timeline_get)
    api_calls = []

    def fake_api_get(path, params, token):
        api_calls.append((path, token))
        if path == "/usage/tweets":
            return 200, {}, {"data": {"project_usage": str(3189 + len(api_calls)), "project_cap": "3000000", "cap_reset_day": 5}}, 0.1
        return 200, {"x-rate-limit-remaining": "9998"}, {"errors": [{"type": "x/not-authorized-for-resource"}]}, 0.1

    monkeypatch.setattr(x_probe, "api_get", fake_api_get)
    monkeypatch.setattr(x_probe, "http", lambda m, url, **kw: (200, {"content-type": "image/png"}, b"\x89PNG" * 10, 0.05))
    args = argparse.Namespace(interval=15.0, duration=0, max_results=100, fetch_media=True, keep_text=False,
                              phases="15:45", start_at=None, extend_until=None, extend_interval=15.0, min_posts=3)
    x_probe.cmd_poll(args)
    log = next(tmp_path.glob("poll_*.jsonl"))
    recs = [json.loads(l) for l in log.read_text().splitlines()]
    kinds = [r["kind"] for r in recs]
    assert kinds == ["meta", "usage", "baseline", "phase", "call", "call", "call", "reconcile", "usage", "end"]
    calls = [r for r in recs if r["kind"] == "call"]
    assert [c["since_id"] for c in calls] == ["100", "100", "101"]
    assert [c["n_new"] for c in calls] == [0, 1, 0] and calls[2]["posts"][0]["duplicate"] is True
    new_post = calls[1]["posts"][0]
    # wall clock does not advance in this test, so the post "existed" before the previous call: floor == lag minus epsilon
    assert 2.5 <= new_post["lag_s"] < 10 and 0 <= new_post["x_delay_floor_s"] <= new_post["lag_s"] and "poll_window_s" in new_post
    assert new_post["media_fetch"][0]["status"] == 200 and new_post["media_fetch"][0]["bytes"] == 40
    assert calls[1]["bearer_control"]["error_types"] == ["x/not-authorized-for-resource"]
    assert "bearer_control" not in calls[2]                       # duplicates do not trigger a control call
    assert all(t == "app-only" for _, t in api_calls) and "tok" not in json.dumps(recs) and "app-only" not in json.dumps(recs)
    recon = recs[-3]
    assert recon["reported"] == 2 and recon["polled"] == 1 and recon["missed_ids"] == ["102"] and recon["vanished_ids"] == []
    assert recs[-2]["when"] == "after" and recs[-1]["new_target_posts"] == 1
    s = x_probe.summarize(recs, "T")
    assert s["cost"]["delta_reads"] == 2 and s["duplicate_deliveries"] == 1 and s["reconcile"]["missed"] == 1
    out = capsys.readouterr().out
    assert "Baseline: status=200 newest_id=100 rate_limit_15min=900" in out and "Done (done)" in out
