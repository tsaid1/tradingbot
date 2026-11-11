"""Source adapters: the replay file and the X user-timeline poller (against a fake HTTP layer).

No network: the X adapter gets a scripted transport and an in-memory token store. Every id, author,
token and text is invented.
"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from runtime_harness import ADD, ENTRY, post, write_posts

from tradingbot.execution.sim_broker import SimClock
from tradingbot.runtime.sources import (
    FileTokenStore,
    HttpResponse,
    ReplayFileSource,
    SourceAuthError,
    SourceError,
    SourceGap,
    SourceRateLimited,
    XTimelineSource,
    effective_interval,
    load_client_credentials,
)
from tradingbot.runtime.store import RuntimeStore
from tradingbot.source_events import Provenance

AUTHOR = "1000000000000000042"  # invented numeric id
T0 = datetime(2026, 6, 1, 14, 0, tzinfo=UTC)


@pytest.fixture
def store(tmp_path):
    return RuntimeStore(tmp_path / "runtime.sqlite3")


# ================================================================================ replay file


def test_replay_returns_posts_in_order_and_moves_the_cursor_only_on_commit(tmp_path, store):
    f = write_posts(tmp_path / "p.jsonl", [post("1", 0, ENTRY), post("2", 60, ADD)])
    src = ReplayFileSource(f, store)
    first = src.poll()
    assert [p.post_id for p in first] == ["1", "2"] and not src.realtime
    assert src.cursor() == "0"
    assert [p.post_id for p in src.poll()] == ["1", "2"]  # nothing consumed yet
    second = src.poll()
    with pytest.raises(ValueError, match="in the order"):
        src.commit(second[1])
    src.commit(second[0])
    assert src.cursor() == "1"
    again = ReplayFileSource(f, store)  # a restart: same store, new object
    assert [p.post_id for p in again.poll()] == ["2"]


def test_replay_picks_up_appended_lines(tmp_path, store):
    f = write_posts(tmp_path / "p.jsonl", [post("1", 0, ENTRY)])
    src = ReplayFileSource(f, store)
    src.commit(src.poll()[0])
    assert src.poll() == []
    with f.open("a") as fh:
        fh.write(post("2", 60, ADD).model_dump_json() + "\n")
    assert [p.post_id for p in src.poll()] == ["2"]


def test_malformed_line_stops_the_source_without_skipping(tmp_path, store):
    f = tmp_path / "p.jsonl"
    f.write_text(post("1", 0, ENTRY).model_dump_json() + "\n{not json\n")
    src = ReplayFileSource(f, store)
    with pytest.raises(SourceError, match="line 2"):
        src.poll()
    assert src.cursor() == "0"


def test_compressed_replay_releases_on_schedule_and_rebases_timestamps(tmp_path, store):
    clock = SimClock(T0)
    edit = post("3", 0, ENTRY + " Edited.", source_id="1", revision=2, revised=90)
    f = write_posts(tmp_path / "p.jsonl", [post("1", 0, ENTRY), post("2", 60, ADD), edit])
    src = ReplayFileSource(f, store, clock=clock.now, compression=2.0)
    assert src.realtime
    [p1] = src.poll()
    assert p1.published_at == T0 and p1.received_at == T0
    src.commit(p1)
    clock.advance(29)
    assert src.poll() == []  # due at T0 + 60 / 2
    clock.advance(1)
    [p2] = src.poll()
    assert p2.published_at == T0 + timedelta(seconds=30) and p2.received_at == clock.now()
    src.commit(p2)
    restarted = ReplayFileSource(f, store, clock=clock.now, compression=2.0)
    clock.advance(15)
    [p3] = restarted.poll()  # the anchor survived the restart
    assert p3.revision == 2 and p3.published_at == T0
    assert p3.revised_at == T0 + timedelta(seconds=45)


def test_replay_reads_g0_archive_records_with_an_explicit_author_check(tmp_path, store):
    rec = {
        "id": "1950000000000000001",
        "author_id": AUTHOR,
        "created_at": "2026-03-02T15:00:00.000Z",
        "text": ENTRY,
        "has_note": False,
        "short_text": ENTRY,
        "referenced": [],
        "edit_history_ids": ["1950000000000000001"],
        "media": [],
    }
    other = {
        **rec,
        "id": "1950000000000000002",
        "author_id": "77",
        "edit_history_ids": ["1950000000000000002"],
    }
    f = tmp_path / "g0.jsonl"
    f.write_text(json.dumps(rec) + "\n" + json.dumps(other) + "\n")
    with pytest.raises(SourceError, match="expected_author_id"):
        ReplayFileSource(f, store).poll()
    posts = ReplayFileSource(
        f, store, expected_author_id=AUTHOR, provenance=Provenance.ARCHIVE_API_USER_CONTEXT
    ).poll()
    assert [p.verified_author for p in posts] == [True, False]
    assert posts[0].provenance == Provenance.ARCHIVE_API_USER_CONTEXT


# ================================================================================ X timeline


class MemoryTokens:
    def __init__(self, tokens: dict):
        self.tokens = dict(tokens)
        self.saved: list[dict] = []

    @contextmanager
    def locked(self):
        yield

    def load(self) -> dict:
        return dict(self.tokens)

    def save(self, tokens: dict) -> None:
        self.tokens = dict(tokens)
        self.saved.append(dict(tokens))


class FakeX:
    """Scripted HTTP: each call pops the next (status, body, headers) response for its kind."""

    def __init__(self):
        self.calls: list[tuple[str, str, dict]] = []
        self.timeline: list[tuple[int, dict, dict]] = []
        self.token: list[tuple[int, dict]] = []

    def __call__(self, method, url, *, headers=None, data=None, timeout=20.0):
        self.calls.append((method, url, dict(headers or {})))
        if "oauth2/token" in url:
            status, body = self.token.pop(0)
            return HttpResponse(status, {}, json.dumps(body).encode())
        if isinstance(self.timeline[0], Exception):
            raise self.timeline.pop(0)
        status, body, hdrs = self.timeline.pop(0)
        return HttpResponse(status, hdrs, json.dumps(body).encode())

    def timeline_urls(self) -> list[str]:
        return [u for _, u, _ in self.calls if "/tweets" in u]


def raw(
    pid: str,
    text: str,
    *,
    author: str = AUTHOR,
    note: str | None = None,
    edits: list[str] | None = None,
    refs=None,
    media_keys=None,
    created="2026-06-01T14:00:00Z",
):
    d = {
        "id": pid,
        "author_id": author,
        "created_at": created,
        "text": text,
        "edit_history_tweet_ids": edits or [pid],
        "conversation_id": pid,
    }
    if note:
        d["note_tweet"] = {"text": note}
    if refs:
        d["referenced_tweets"] = refs
    if media_keys:
        d["attachments"] = {"media_keys": media_keys}
    return d


RATE = {"x-rate-limit-limit": "900", "x-rate-limit-remaining": "899", "x-rate-limit-reset": "0"}


def make(store, fake, *, tokens=None, now=1_000_000.0):
    clock = SimClock(T0)
    wall = {"t": now}
    src = XTimelineSource(
        author_id=AUTHOR,
        token_store=tokens
        or MemoryTokens(
            {
                "access_token": "invented-access",
                "refresh_token": "invented-refresh",
                "expires_at": now + 3600,
            }
        ),
        client=load_client_credentials_stub(),
        transport=fake,
        store=store,
        clock=clock.now,
        wall_time=lambda: wall["t"],
    )
    return src, wall


def load_client_credentials_stub():
    from tradingbot.runtime.sources import ClientCredentials

    return ClientCredentials("invented-client", "invented-secret")


def baseline(fake, newest="100"):
    fake.timeline.append((200, {"data": [{"id": newest}, {"id": "99"}]}, RATE))


def test_first_poll_is_a_baseline_that_never_passes_old_posts_on(store):
    fake = FakeX()
    baseline(fake, "100")
    src, _ = make(store, fake)
    assert src.poll() == [] and src.cursor() == "100"
    assert "since_id" not in fake.timeline_urls()[0]


def test_poll_uses_since_id_returns_publication_order_and_commit_moves_the_cursor(store):
    fake = FakeX()
    baseline(fake)
    src, _ = make(store, fake)
    src.poll()
    fake.timeline.append((200, {"data": [raw("102", ADD), raw("101", ENTRY)]}, RATE))
    posts = src.poll()
    assert "since_id=100" in fake.timeline_urls()[-1]
    assert [p.post_id for p in posts] == ["101", "102"]
    assert all(
        p.provenance == Provenance.LIVE_API_USER_CONTEXT and p.verified_author for p in posts
    )
    src.commit(posts[0])
    assert src.cursor() == "101"
    restarted, _ = make(store, fake)  # new process, same store
    fake.timeline.append((200, {"data": [raw("102", ADD)]}, RATE))
    assert [p.post_id for p in restarted.poll()] == ["102"]
    assert "since_id=101" in fake.timeline_urls()[-1]


def test_long_post_uses_the_note_body_and_a_truncated_body_is_never_complete(store):
    fake = FakeX()
    baseline(fake)
    src, _ = make(store, fake)
    src.poll()
    long_body = ENTRY + " " + "Invented detail. " * 30
    fake.timeline.append(
        (
            200,
            {
                "data": [
                    raw("101", long_body[:270] + "… https://t.co/abc123", note=long_body),
                    raw("102", long_body[:270] + "… https://t.co/def456"),  # note missing: cut off
                    raw("103", "Short and complete."),
                ]
            },
            RATE,
        )
    )
    p1, p2, p3 = src.poll()
    assert p1.text == long_body and p1.text_complete
    assert not p2.text_complete and src.truncated_posts == 1
    assert p3.text_complete


def test_edit_becomes_a_revision_of_the_original_post(store):
    fake = FakeX()
    baseline(fake)
    src, _ = make(store, fake)
    src.poll()
    fake.timeline.append(
        (
            200,
            {
                "data": [
                    raw(
                        "105",
                        ENTRY + " Edited.",
                        edits=["101", "105"],
                        created="2026-06-01T14:00:40Z",
                    ),
                ]
            },
            RATE,
        )
    )
    [p] = src.poll()
    assert (p.source_id, p.post_id, p.revision) == ("101", "105", 2)
    assert p.revised_at == datetime(2026, 6, 1, 14, 0, 40, tzinfo=UTC)
    assert p.to_replay_source().revision == 2  # the gateway holds every revision > 1


def test_media_and_quote_reply_references_are_kept(store):
    fake = FakeX()
    baseline(fake)
    src, _ = make(store, fake)
    src.poll()
    fake.timeline.append(
        (
            200,
            {
                "data": [
                    raw(
                        "101",
                        ENTRY,
                        media_keys=["3_1"],
                        refs=[{"type": "quoted", "id": "90"}, {"type": "replied_to", "id": "91"}],
                    )
                ],
                "includes": {
                    "media": [
                        {
                            "media_key": "3_1",
                            "type": "photo",
                            "url": "https://pbs.twimg.com/media/invented.png",
                        }
                    ]
                },
            },
            RATE,
        )
    )
    [p] = src.poll()
    assert [m.media_key for m in p.media] == ["3_1"]
    assert {(link.relation, link.post_id) for link in p.links} == {
        ("quoted", "90"),
        ("replied_to", "91"),
    }


def test_post_by_another_author_is_passed_on_unverified(store):
    fake = FakeX()
    baseline(fake)
    src, _ = make(store, fake)
    src.poll()
    fake.timeline.append((200, {"data": [raw("101", ENTRY, author="777")]}, RATE))
    [p] = src.poll()
    assert not p.verified_author and src.author_mismatches == 1


def test_rate_limit_blocks_until_reset_without_calling_the_api(store):
    fake = FakeX()
    baseline(fake)
    src, wall = make(store, fake)
    src.poll()
    reset = int(wall["t"]) + 120
    fake.timeline.append(
        (429, {}, {"x-rate-limit-remaining": "0", "x-rate-limit-reset": str(reset)})
    )
    with pytest.raises(SourceRateLimited) as exc:
        src.poll()
    assert 119 <= exc.value.retry_after <= 122
    calls = len(fake.calls)
    with pytest.raises(SourceRateLimited):
        src.poll()
    assert len(fake.calls) == calls
    wall["t"] = reset + 1
    fake.timeline.append((200, {"data": []}, RATE))
    assert src.poll() == [] and src.cursor() == "100"


def test_cadence_slows_to_what_the_quota_allows():
    assert effective_interval(5.0, None) == 5.0
    assert effective_interval(5.0, 900) == 5.0
    assert effective_interval(5.0, 180) == 6.3  # 900 / (0.8 * 180) = 6.25, rounded up


def test_expired_token_is_refreshed_before_the_call_and_saved(store):
    fake = FakeX()
    tokens = MemoryTokens(
        {"access_token": "old", "refresh_token": "invented-refresh", "expires_at": 0}
    )
    fake.token.append((200, {"access_token": "new-access", "expires_in": 7200}))
    baseline(fake)
    src, _ = make(store, fake, tokens=tokens)
    src.poll()
    assert tokens.tokens["access_token"] == "new-access"
    assert tokens.tokens["refresh_token"] == "invented-refresh"  # kept when not rotated
    auth = next(h for _, u, h in fake.calls if "oauth2/token" in u)["Authorization"]
    assert auth.startswith("Basic ")
    assert fake.calls[-1][2]["Authorization"] == "Bearer new-access"


def test_a_401_refreshes_once_and_retries(store):
    fake = FakeX()
    baseline(fake)
    src, _ = make(store, fake)
    src.poll()
    fake.timeline.append((401, {}, {}))
    fake.token.append((200, {"access_token": "rotated", "expires_in": 7200}))
    fake.timeline.append((200, {"data": [raw("101", ENTRY)]}, RATE))
    assert [p.post_id for p in src.poll()] == ["101"]


@pytest.mark.parametrize("status", [401, 403])
def test_refused_access_raises_an_auth_error_without_leaking_tokens(store, status):
    fake = FakeX()
    baseline(fake)
    src, _ = make(store, fake)
    src.poll()
    fake.timeline.append((status, {}, {}))
    if status == 401:
        fake.token.append((400, {"error": "invalid_grant"}))
    with pytest.raises(SourceAuthError) as exc:
        src.poll()
    assert "invented-access" not in str(exc.value) and "invented-refresh" not in str(exc.value)
    assert src.cursor() == "100"


def test_network_error_and_server_error_are_retryable_source_errors(store):
    fake = FakeX()
    baseline(fake)
    src, _ = make(store, fake)
    src.poll()
    fake.timeline.append(OSError("invented network failure"))
    with pytest.raises(SourceError, match="network"):
        src.poll()
    fake.timeline.append((503, {}, {}))
    with pytest.raises(SourceError, match="503"):
        src.poll()
    assert src.cursor() == "100"


def test_pagination_reads_every_page_and_refuses_a_partial_backlog(store):
    fake = FakeX()
    baseline(fake)
    src, _ = make(store, fake)
    src.max_pages = 2
    src.poll()
    fake.timeline.append((200, {"data": [raw("104", ADD)], "meta": {"next_token": "n1"}}, RATE))
    fake.timeline.append((200, {"data": [raw("101", ENTRY)]}, RATE))
    assert [p.post_id for p in src.poll()] == ["101", "104"]
    assert "pagination_token=n1" in fake.timeline_urls()[-1]
    fake.timeline += [
        (200, {"data": [raw("110", ADD)], "meta": {"next_token": "a"}}, RATE),
        (200, {"data": [raw("109", ADD)], "meta": {"next_token": "b"}}, RATE),
    ]
    with pytest.raises(SourceGap):
        src.poll()
    assert src.cursor() == "100"


def test_field_naming_falls_back_to_post_fields_on_a_400(store):
    fake = FakeX()
    fake.timeline.append((400, {"errors": [{"message": "invalid tweet.fields"}]}, {}))
    baseline(fake)
    src, _ = make(store, fake)
    src.poll()
    assert "tweet.fields" in fake.timeline_urls()[0] and "post.fields" in fake.timeline_urls()[1]
    assert src.naming == "post"


def test_nothing_at_or_before_the_cursor_is_delivered_twice(store):
    fake = FakeX()
    baseline(fake)
    src, _ = make(store, fake)
    src.poll()
    fake.timeline.append(
        (200, {"data": [raw("100", ENTRY), raw("101", ADD), raw("101", ADD)]}, RATE)
    )
    assert [p.post_id for p in src.poll()] == ["101"]


# ------------------------------------------------------------------------- credential files


def private_dir(tmp_path: Path) -> Path:
    d = tmp_path / "cred"
    d.mkdir(mode=0o700)
    os.chmod(d, 0o700)
    return d.resolve()


def test_file_token_store_requires_private_permissions(tmp_path):
    d = private_dir(tmp_path)
    f = d / "tokens.json"
    f.write_text(json.dumps({"access_token": "invented", "expires_at": 1}))
    os.chmod(f, 0o644)
    store = FileTokenStore(f)
    with store.locked(), pytest.raises(SourceAuthError, match="0600"):
        store.load()
    os.chmod(f, 0o600)
    with store.locked():
        assert store.load()["access_token"] == "invented"
        store.save({"access_token": "rotated"})
    assert oct(f.stat().st_mode & 0o777) == "0o600"
    os.chmod(d, 0o755)
    with pytest.raises(SourceAuthError, match="0700"), store.locked():
        pass


def test_client_credentials_are_read_from_a_private_file_and_never_printed(tmp_path):
    d = private_dir(tmp_path)
    f = d / "client.json"
    f.write_text(json.dumps({"client_id": "invented-id", "client_secret": "invented-secret"}))
    os.chmod(f, 0o600)
    creds = load_client_credentials(f)
    assert creds.client_id == "invented-id"
    assert "invented-secret" not in repr(creds) and "invented-id" not in repr(creds)
    f.write_text("{}")
    with pytest.raises(SourceAuthError, match="client_id"):
        load_client_credentials(f)


def test_credentials_in_cloud_storage_are_refused(tmp_path):
    d = tmp_path / "Dropbox-Invented" / "cred"
    d.mkdir(parents=True)
    with pytest.raises(SourceAuthError, match="cloud"):
        load_client_credentials(d.resolve() / "client.json")
