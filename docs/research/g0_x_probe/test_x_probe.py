"""Offline tests for the pure parts of the G0 probe. No network. Run from the repo root:

    python -m pytest docs/research/g0_x_probe -q
"""
import base64
import hashlib
import sys
from datetime import datetime, timezone
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
    observed = datetime(2026, 11, 5, 14, 0, 12, 500000, tzinfo=timezone.utc)
    assert x_probe.lag_seconds(created, observed) == 12.5


def test_newest_id_is_numeric_max_not_lexicographic():
    assert x_probe.newest_id(None, ["99", "100"]) == "100"
    assert x_probe.newest_id("150", ["99", "100"]) == "150"
    assert x_probe.newest_id(None, []) is None
    assert x_probe.newest_id("7", []) == "7"


def test_redact_post_hashes_text_by_default_and_reports_media():
    observed = datetime(2026, 11, 5, 14, 0, 30, tzinfo=timezone.utc)
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
    observed = datetime(2026, 11, 5, 14, 0, 30, tzinfo=timezone.utc)
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
