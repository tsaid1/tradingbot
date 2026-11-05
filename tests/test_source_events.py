"""SourcePost (schema 1.2): the versioned interface from the Source Integration Agent. Synthetic data only."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256

import pytest
from pydantic import ValidationError

from tradingbot.source_events import SNOWFLAKE_EPOCH_MS, Provenance, SourcePost, snowflake_time

AUTHOR = "1237233159005179904"
ROOT_TIME = datetime(2026, 1, 2, 14, 31, 7, tzinfo=timezone.utc)


def _snowflake_for(moment: datetime) -> str:
    return str((int(moment.timestamp() * 1000) - SNOWFLAKE_EPOCH_MS) << 22)


def _record(**overrides):
    root = _snowflake_for(ROOT_TIME)
    rec = {"id": root, "author_id": AUTHOR, "created_at": "2026-01-02T14:31:07.000Z", "conversation_id": root,
           "text": "SYNTHETIC body", "has_note": False, "short_text": "SYNTHETIC body", "referenced": [],
           "is_reply": False, "is_quote": False, "edit_history_ids": [root], "media": [],
           "observed_at": "2026-11-05T10:21:03+00:00", "media_files": []}
    rec.update(overrides)
    return rec


def _from(rec, provenance=Provenance.ARCHIVE_API_USER_CONTEXT, expected=AUTHOR):
    return SourcePost.from_g0_record(rec, expected_author_id=expected, provenance=provenance)


def test_snowflake_time_round_trips():
    assert abs((snowflake_time(_snowflake_for(ROOT_TIME)) - ROOT_TIME).total_seconds()) < 0.001


def test_unedited_record_maps_to_revision_one():
    rec = _record()
    p = _from(rec)
    assert p.schema_version == "1.2" and p.source_id == p.post_id == rec["id"]
    assert p.revision == 1 and p.revised_at is None and p.published_at == ROOT_TIME
    assert p.received_at == datetime(2026, 11, 5, 10, 21, 3, tzinfo=timezone.utc)
    assert p.verified_author and p.text_complete and not p.is_repost
    assert p.is_paid_content and not p.is_live
    assert p.text_sha256 == sha256(b"SYNTHETIC body").hexdigest()
    rs = p.to_replay_source()
    assert rs.source_id == rec["id"] and rs.revision == 1 and rs.post_hash == p.text_sha256
    assert rs.verified_author and rs.complete_text and not rs.is_repost


def test_edited_record_keeps_root_identity_and_original_publication_time():
    edit_time = ROOT_TIME + timedelta(seconds=38)
    root, edit = _snowflake_for(ROOT_TIME), _snowflake_for(edit_time)
    rec = _record(id=edit, created_at=edit_time.strftime("%Y-%m-%dT%H:%M:%S.000Z"), edit_history_ids=[root, edit])
    p = _from(rec)
    assert p.source_id == root and p.post_id == edit and p.revision == 2
    assert abs((p.published_at - ROOT_TIME).total_seconds()) < 0.001   # not the edit time
    assert p.revised_at == edit_time
    assert p.to_replay_source().revision == 2
    with pytest.raises(ValueError):
        p.to_legacy_source_event()   # the Sprint 1 journal has no revision concept


def test_retweet_is_a_repost_and_never_complete():
    rec = _record(text="RT @someone: synthetic truncated…", short_text="RT @someone: synthetic truncated…",
                  referenced=[{"type": "retweeted", "id": "1"}])
    p = _from(rec)
    assert p.is_repost and not p.text_complete
    assert p.links[0].relation == "retweeted" and p.links[0].post_id == "1"
    assert p.to_replay_source().is_repost and not p.to_replay_source().complete_text


def test_completeness_rule():
    long_body = _from(_record(text="x" * 600, has_note=True, short_text="x" * 279 + "…"))
    assert long_body.text_complete
    truncated = _from(_record(text="synthetic cut…", has_note=False, short_text="synthetic cut…"))
    assert not truncated.text_complete
    assert not truncated.to_replay_source().complete_text


def test_media_links_and_conversation_are_carried():
    rec = _record(media=[{"media_key": "3_1", "type": "photo", "url": "https://example.invalid/3_1.png"}],
                  media_files=[{"status": 200, "file": "3_1.png", "bytes": 5}],
                  referenced=[{"type": "quoted", "id": "9"}, {"type": "replied_to", "id": "8"}],
                  conversation_id="8", is_reply=True, is_quote=True)
    p = _from(rec)
    assert p.media[0].media_key == "3_1" and p.media[0].local_file == "3_1.png" and p.media[0].url.endswith(".png")
    assert [(link.relation, link.post_id) for link in p.links] == [("quoted", "9"), ("replied_to", "8")]
    assert all(not link.text_available for link in p.links) and p.conversation_id == "8"


def test_author_verification_and_legacy_lift():
    other = _from(_record(), expected="999")
    assert not other.verified_author and not other.to_replay_source().verified_author
    archive = _from(_record()).to_legacy_source_event()
    assert archive.source_type == "protected_feed" and archive.provider == "x_api_v2_user_timeline"
    synthetic = _from(_record(), provenance=Provenance.SYNTHETIC).to_legacy_source_event()
    assert synthetic.source_type == "manual_demo" and synthetic.provider == "synthetic_test"
    live = _from(_record(), provenance=Provenance.LIVE_API_USER_CONTEXT)
    assert live.is_live and live.is_paid_content
    with pytest.raises(ValueError):
        _from(_record(text="", short_text="")).to_legacy_source_event()


def test_timestamps_must_be_aware_and_revision_one_has_no_revised_at():
    base = {"source_id": "s", "post_id": "s", "author_id": "A", "text": "t", "published_at": ROOT_TIME,
            "received_at": ROOT_TIME, "provenance": Provenance.SYNTHETIC}
    with pytest.raises(ValidationError):
        SourcePost(**{**base, "published_at": ROOT_TIME.replace(tzinfo=None)})
    with pytest.raises(ValidationError):
        SourcePost(**{**base, "revised_at": ROOT_TIME})
    with pytest.raises(ValidationError):
        SourcePost(**{**base, "quantity": 5})   # no sizing on a source event
    assert SourcePost(**{**base, "revision": 2, "revised_at": ROOT_TIME}).revision == 2
