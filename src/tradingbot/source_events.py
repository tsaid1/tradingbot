"""Versioned source-event interface (schema 1.2) between the Source Integration Agent and the
interpreter / replay layer.

The Source Integration Agent (the G0 feed adapter) produces ``SourcePost`` objects; nothing
downstream reads the X API directly. Text travels in this object so the interpreter can see it,
but no ``SourcePost`` is ever persisted by this package: the replay coordinator stores only
``ReplaySource`` metadata and hashes. The mapping rules below come from the archive analysis in
``docs/research/G0_EVENT_SCHEMA.md``.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from enum import Enum
from typing import Literal

from pydantic import Field, model_validator

from tradingbot.event_models import ReplaySource
from tradingbot.models import SourceEvent, StrictModel

SNOWFLAKE_EPOCH_MS = 1288834974657  # X post IDs encode their creation time: (id >> 22) + epoch, ms


def snowflake_time(post_id: str) -> datetime:
    """Creation time encoded in an X post ID (UTC, millisecond resolution)."""
    return datetime.fromtimestamp(((int(post_id) >> 22) + SNOWFLAKE_EPOCH_MS) / 1000, tz=timezone.utc)


class Provenance(str, Enum):
    LIVE_API_USER_CONTEXT = "LIVE_API_USER_CONTEXT"        # live poll, subscriber token; received_at is real
    ARCHIVE_API_USER_CONTEXT = "ARCHIVE_API_USER_CONTEXT"  # backfill; received_at is archive time, not latency
    MANUAL = "MANUAL"                                      # operator-typed text (dashboard)
    SYNTHETIC = "SYNTHETIC"                                # tests and fixtures


PAID_PROVENANCE = frozenset({Provenance.LIVE_API_USER_CONTEXT, Provenance.ARCHIVE_API_USER_CONTEXT})


class MediaRef(StrictModel):
    media_key: str = Field(min_length=1)
    type: Literal["photo", "video", "animated_gif"] = "photo"
    url: str | None = None
    local_file: str | None = None
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class PostLink(StrictModel):
    relation: Literal["quoted", "replied_to", "retweeted"]
    post_id: str = Field(min_length=1)
    text_available: bool = False  # True when the adapter resolved the linked post's text as context


class SourcePost(StrictModel):
    """One version of one post, as delivered by the trusted adapter. Never produced by a model.

    ``source_id`` is the root of the edit chain (the logical post); ``post_id`` is this version.
    ``published_at`` is always the original publication; an edit carries its own ``revised_at``.
    """
    schema_version: Literal["1.2"] = "1.2"
    source_id: str = Field(min_length=1, max_length=128)
    post_id: str = Field(min_length=1, max_length=128)
    revision: int = Field(default=1, ge=1)
    author_id: str = Field(min_length=1)
    verified_author: bool = False
    text: str = Field(max_length=20000)
    text_complete: bool = False
    is_repost: bool = False
    published_at: datetime
    revised_at: datetime | None = None
    received_at: datetime
    provenance: Provenance
    conversation_id: str | None = None
    links: list[PostLink] = Field(default_factory=list)
    media: list[MediaRef] = Field(default_factory=list)

    @model_validator(mode="after")
    def _timestamps_are_aware(self):
        for name in ("published_at", "received_at", "revised_at"):
            value = getattr(self, name)
            if value is not None and value.tzinfo is None:
                raise ValueError(f"{name} must be timezone-aware")
        if self.revision == 1 and self.revised_at is not None:
            raise ValueError("revision 1 cannot carry revised_at")
        return self

    @property
    def text_sha256(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()

    @property
    def is_paid_content(self) -> bool:
        return self.provenance in PAID_PROVENANCE

    @property
    def is_live(self) -> bool:
        return self.provenance == Provenance.LIVE_API_USER_CONTEXT

    def to_replay_source(self) -> ReplaySource:
        """Trusted metadata for the replay coordinator. The hash covers the exact text bytes."""
        return ReplaySource(
            source_id=self.source_id, revision=self.revision, published_at=self.published_at,
            received_at=self.received_at, verified_author=self.verified_author,
            complete_text=self.text_complete, is_repost=self.is_repost, post_hash=self.text_sha256,
        )

    def to_legacy_source_event(self) -> SourceEvent:
        """Sprint 1 shape for the demo pipeline, which has no provenance fields of its own.

        Everything the Sprint 1.1 gateway would hold or block on provenance is therefore refused
        here, before the text can reach that pipeline: unverified authors, incomplete text,
        reposts and edits (the Sprint 1 journal has no revision concept)."""
        if not self.verified_author:
            raise ValueError("Refusing legacy conversion: author not verified")
        if not self.text_complete:
            raise ValueError("Refusing legacy conversion: text incomplete")
        if self.is_repost:
            raise ValueError("Refusing legacy conversion: repost is not the author's instruction")
        if self.revision != 1:
            raise ValueError("Refusing legacy conversion: edited post (the Sprint 1 journal has no revisions)")
        if not self.text:
            raise ValueError("Refusing legacy conversion: empty text")
        paid = self.is_paid_content
        return SourceEvent(
            source_id=self.source_id,
            source_type="protected_feed" if paid else "manual_demo",
            provider="x_api_v2_user_timeline" if paid else ("operator" if self.provenance == Provenance.MANUAL else "synthetic_test"),
            text=self.text, published_at=self.published_at,
        )

    @classmethod
    def from_g0_record(cls, record: dict, *, expected_author_id: str, provenance: Provenance,
                       received_at: datetime | None = None) -> SourcePost:
        """Map one G0 archive or poll record (see docs/research/G0_EVENT_SCHEMA.md).

        * identity: ``source_id`` is the first ID of the edit chain, ``revision`` the position of
          this version in it;
        * timestamps: for revision 1 ``published_at`` is the API ``created_at``; for later
          revisions ``created_at`` is the edit time, so ``published_at`` comes from the root ID's
          snowflake time and ``created_at`` becomes ``revised_at``;
        * completeness: a retweet is never complete (X truncates it); otherwise the post is complete
          when the long body was returned or the short text is not ellipsis-terminated.
        """
        post_id = str(record["id"])
        chain = [str(i) for i in (record.get("edit_history_ids") or [post_id])]
        root = chain[0]
        revision = chain.index(post_id) + 1 if post_id in chain else len(chain)
        created = _parse_time(record["created_at"])
        text = record.get("text") or ""
        short = record.get("short_text") if record.get("short_text") is not None else text
        referenced = record.get("referenced") or []
        is_repost = any(r.get("type") == "retweeted" for r in referenced) or text.startswith("RT @")
        complete = (not is_repost) and bool(record.get("has_note") or not short.rstrip().endswith("…"))
        files = {str(f.get("file", "")).split(".")[0]: f for f in (record.get("media_files") or []) if f.get("file")}
        media = [
            MediaRef(media_key=m["media_key"], type=m.get("type") or "photo", url=m.get("url"),
                     local_file=(files.get(m["media_key"]) or {}).get("file"))
            for m in (record.get("media") or []) if m.get("media_key")
        ]
        links = [PostLink(relation=r["type"], post_id=str(r["id"])) for r in referenced
                 if r.get("type") in ("quoted", "replied_to", "retweeted") and r.get("id")]
        observed = record.get("observed_at")
        received = received_at or (_parse_time(observed) if observed else created)
        return cls(
            source_id=root, post_id=post_id, revision=revision, author_id=str(record.get("author_id", "")),
            verified_author=str(record.get("author_id", "")) == expected_author_id,
            text=text, text_complete=complete, is_repost=is_repost,
            published_at=created if revision == 1 else snowflake_time(root),
            revised_at=created if revision > 1 else None,
            received_at=received, provenance=provenance, conversation_id=record.get("conversation_id"),
            links=links, media=media,
        )


def coerce_source_posts(items, *, expected_author_id: str | None = None,
                        provenance: Provenance = Provenance.ARCHIVE_API_USER_CONTEXT) -> list[SourcePost]:
    """Normalise what a Source Agent adapter yields into SourcePost objects.

    Accepted item shapes, in order of preference:
      * a SourcePost instance;
      * a SourcePost-shaped mapping (has ``schema_version`` or both ``source_id`` and ``post_id``);
      * a G0 archive/poll record (has ``id`` and ``created_at``), mapped with ``from_g0_record``,
        which needs ``expected_author_id``.
    Anything else raises TypeError. Output is sorted by (published_at, revision, post_id).
    """
    out: list[SourcePost] = []
    for item in items:
        if isinstance(item, SourcePost):
            out.append(item)
        elif isinstance(item, dict) and ("schema_version" in item or ("source_id" in item and "post_id" in item)):
            out.append(SourcePost.model_validate(item))
        elif isinstance(item, dict) and "id" in item and "created_at" in item:
            if expected_author_id is None:
                raise TypeError("G0 records need expected_author_id to verify the author")
            out.append(SourcePost.from_g0_record(item, expected_author_id=expected_author_id, provenance=provenance))
        else:
            raise TypeError(f"Unsupported source item: {type(item).__name__}")
    return sorted(out, key=lambda p: (p.published_at, p.revision, p.post_id))


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
