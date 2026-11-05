"""Read-only historical inputs for the core interpreter. No interpreter or engine is invoked."""
from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from tradingbot.event_models import ReplaySource
from tradingbot.models import SourceEvent
from tradingbot.source_events import MediaRef, PostLink, Provenance, SourcePost, snowflake_time

from .archive import identifier, instant, read_history

PLATFORM_EPOCH = datetime(2010, 11, 4, 1, 42, 54, 657000, tzinfo=UTC)


@dataclass(frozen=True)
class HistoricalInput:
    archive_record: dict
    replay_source: ReplaySource
    revised_at: datetime | None

    @property
    def text(self) -> str:
        return self.archive_record["text"]

    def as_source_post(self) -> SourcePost:
        """Project the validated revision into the core archive model, without resolving links."""
        row, src = self.archive_record, self.replay_source
        return SourcePost(
            source_id=src.source_id, post_id=row["post_id"], revision=src.revision,
            author_id=row["author"]["id"], verified_author=src.verified_author,
            text=self.text, text_complete=src.complete_text, is_repost=src.is_repost,
            published_at=snowflake_time(src.source_id) if self.revised_at else src.published_at,
            revised_at=self.revised_at, received_at=instant(row["observed_at"]),
            provenance=Provenance.ARCHIVE_API_USER_CONTEXT, conversation_id=row["conversation_id"],
            links=[PostLink(relation=r["type"], post_id=r["id"]) for r in row["references"]],
            media=[MediaRef(media_key=m["media_key"], type=m["type"], url=m.get("url"),
                            local_file=m["download"].get("file"), sha256=m.get("sha256"))
                   for m in row["media"]],
        )

    def as_source_event(self) -> SourceEvent:
        """The legacy input lacks revision, repost and completeness fields."""
        src = self.replay_source
        if src.revision != 1 or src.is_repost or not src.complete_text or not src.verified_author:
            raise ValueError("legacy SourceEvent cannot safely represent this record")
        return SourceEvent(source_id=src.source_id, source_type="protected_feed",
                           provider="x_api_v2_user_timeline", text=self.text,
                           published_at=src.published_at)


def historical_inputs(path: Path, *, expected_sha256: str, expected_author_id: str,
                      simulated_lag: timedelta, through: datetime | None = None
                      ) -> Iterator[HistoricalInput]:
    """Pin a trusted curator export and explicitly choose a nonnegative offline replay lag.

    through limits simulated availability. An edited body becomes visible at its revision's
    publication plus simulated lag, even when its family began much earlier.
    """
    identifier(expected_author_id)
    if simulated_lag < timedelta(0):
        raise ValueError("simulated lag must be nonnegative")
    if not expected_sha256:
        raise ValueError("expected archive digest is required")
    if through is not None and through.utcoffset() is None:
        raise ValueError("through needs an offset")
    for row in read_history(path, expected_sha256=expected_sha256, through=through):
        version_at = instant(row["published_at"])
        received = version_at + simulated_lag
        if through is not None and received > through:
            return
        author = row["author"]
        if author["id"] != expected_author_id or author["protected_at_lookup"] is not True:
            raise ValueError("unexpected protected-timeline author")
        if row["provenance"]["method"] != "authenticated_user_timeline_backfill":
            raise ValueError("unexpected source provenance")
        history = row["edit_history_ids"]
        if not history or (history[0], history[-1], len(history)) != (
            row["post_family_id"], row["post_id"], row["revision"]
        ):
            raise ValueError("invalid archived revision identity")
        revised = len(history) > 1
        if revised:
            original = PLATFORM_EPOCH + timedelta(milliseconds=int(identifier(history[0])) >> 22)
            original = original.replace(microsecond=0)
            if original > version_at:
                raise ValueError("edit predates original")
        else:
            original = version_at
        source = ReplaySource(
            source_id=history[0], revision=len(history), published_at=original,
            received_at=received, verified_author=True,
            complete_text=row["text_completeness"] == "api_body" and not row["is_repost"],
            is_repost=row["is_repost"], post_hash=row["text_sha256"],
        )
        yield HistoricalInput(row, source, version_at if revised else None)
