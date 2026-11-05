"""End-to-end compatibility with the Source Agent's `historical_inputs()` adapter.

The adapter itself lives with the Source Agent and is not in this repository yet. This test pins
the contract from the consuming side with a synthetic stand-in that yields every item shape the
adapter may reasonably produce, and runs the result through SourcePost -> interpreter -> gateway ->
coordinator. The Source Agent can import `historical_inputs_contract` and run its real adapter
through `check_historical_inputs` to prove compatibility without sharing any paid text.
"""
from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from tradingbot.event_replay import PaperEventReplay
from tradingbot.models import MockPolicy, Quote
from tradingbot.source_events import SNOWFLAKE_EPOCH_MS, Provenance, SourcePost, coerce_source_posts
from tradingbot.tkl_interpreter import MessageClass, TklInterpreter, replay_posts

AUTHOR = "1237233159005179904"
T0 = datetime(2026, 11, 5, 14, 30, tzinfo=timezone.utc)
ARCHIVE_TIME = "2026-11-06T10:00:00+00:00"


def _snowflake(moment: datetime) -> str:
    return str((int(moment.timestamp() * 1000) - SNOWFLAKE_EPOCH_MS) << 22)


def historical_inputs(start: datetime, end: datetime) -> Iterable[SourcePost | dict]:
    """Stand-in for the Source Agent adapter. Contract: yields, for posts published in [start, end),
    items that are SourcePost instances, SourcePost-shaped dicts (schema 1.2) or raw G0 archive
    records; provenance ARCHIVE_API_USER_CONTEXT; any order (the consumer sorts). All text below is
    invented."""
    t1, t2, t3 = T0, T0 + timedelta(hours=5), T0 + timedelta(hours=9)
    root3 = _snowflake(t3)
    edit3 = _snowflake(t3 + timedelta(seconds=40))
    items = [
        # a raw G0 record (the archive's native shape)
        {"id": _snowflake(t1), "author_id": AUTHOR, "created_at": t1.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
         "conversation_id": _snowflake(t1), "text": "We are now long $TLT at $88.50. Stop: $86.00. Target: $95.00.",
         "has_note": False, "short_text": "We are now long $TLT at $88.50. Stop: $86.00. Target: $95.00.",
         "referenced": [], "is_reply": False, "is_quote": False, "edit_history_ids": [_snowflake(t1)],
         "media": [], "observed_at": ARCHIVE_TIME, "media_files": []},
        # a SourcePost-shaped dict (schema 1.2), published later but yielded first on purpose
        {"schema_version": "1.2", "source_id": "half", "post_id": "half", "revision": 1, "author_id": AUTHOR,
         "verified_author": True, "text": "Taking profits on half of our $TLT long at $92.00.", "text_complete": True,
         "is_repost": False, "published_at": t2.isoformat(), "received_at": ARCHIVE_TIME,
         "provenance": "ARCHIVE_API_USER_CONTEXT", "links": [], "media": []},
        # an edited G0 record: revision 2 of a close, which must hold rather than execute
        {"id": edit3, "author_id": AUTHOR, "created_at": (t3 + timedelta(seconds=40)).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
         "conversation_id": root3, "text": "Closing the remaining $TLT long here at $94.10.", "has_note": False,
         "short_text": "Closing the remaining $TLT long here at $94.10.", "referenced": [], "is_reply": False,
         "is_quote": False, "edit_history_ids": [root3, edit3], "media": [], "observed_at": ARCHIVE_TIME,
         "media_files": []},
        # a SourcePost instance: a retweet, never an instruction
        SourcePost(source_id="rt", post_id="rt", author_id=AUTHOR, verified_author=True,
                   text="RT @someone: synthetic…", text_complete=False, is_repost=True,
                   published_at=T0 + timedelta(hours=1), received_at=datetime.fromisoformat(ARCHIVE_TIME),
                   provenance=Provenance.ARCHIVE_API_USER_CONTEXT),
    ]
    for item in items:
        published = item.published_at if isinstance(item, SourcePost) else (
            datetime.fromisoformat(item["published_at"]) if "published_at" in item
            else datetime.fromisoformat(item["created_at"].replace("Z", "+00:00")))
        if start <= published < end:
            yield item


def check_historical_inputs(items, *, expected_author_id: str) -> list[SourcePost]:
    """What the consumer requires of the adapter's output. Importable by the Source Agent."""
    posts = coerce_source_posts(items, expected_author_id=expected_author_id)
    for p in posts:
        assert p.schema_version == "1.2"
        assert p.provenance in (Provenance.ARCHIVE_API_USER_CONTEXT, Provenance.LIVE_API_USER_CONTEXT)
        assert p.published_at.tzinfo is not None and p.received_at.tzinfo is not None
        assert (p.revision == 1) == (p.revised_at is None)
        assert p.verified_author == (p.author_id == expected_author_id)
        assert p.to_replay_source().post_hash == p.text_sha256
    assert [(p.published_at, p.revision, p.post_id) for p in posts] == sorted((p.published_at, p.revision, p.post_id) for p in posts)
    return posts


def test_adapter_output_coerces_sorts_and_maps_provenance():
    posts = check_historical_inputs(historical_inputs(T0 - timedelta(days=1), T0 + timedelta(days=1)),
                                    expected_author_id=AUTHOR)
    assert [p.source_id for p in posts][1:] == ["rt", "half", _snowflake(T0 + timedelta(hours=9))]
    first, rt, half, edited = posts
    assert first.revision == 1 and first.text_complete and first.verified_author and not first.is_live
    assert rt.is_repost and not rt.text_complete
    assert half.provenance == Provenance.ARCHIVE_API_USER_CONTEXT and half.received_at == datetime.fromisoformat(ARCHIVE_TIME)
    assert edited.revision == 2 and edited.revised_at is not None and edited.published_at == T0 + timedelta(hours=9)
    assert all(not p.is_live for p in posts)


def test_adapter_output_replays_end_to_end_through_gateway_and_coordinator(tmp_path):
    posts = check_historical_inputs(historical_inputs(T0 - timedelta(days=1), T0 + timedelta(days=1)),
                                    expected_author_id=AUTHOR)
    coord = PaperEventReplay(tmp_path / "paper.sqlite3", MockPolicy(quantity=4))
    mids = {"88.50": 0, "92.00": 5, "94.10": 9}

    def quotes(p, now):
        hours = round((p.published_at - T0).total_seconds() / 3600)
        mid = next((m for m, h in mids.items() if h == hours), "90.00")
        return {"TLT": Quote(symbol="TLT", bid=Decimal(mid) - Decimal("0.05"), ask=Decimal(mid) + Decimal("0.05"),
                             observed_at=now)}

    steps = replay_posts(posts, coord, TklInterpreter(), quotes, lag_seconds=13.3)
    by_id = {s.post.source_id: s for s in steps}
    assert by_id[_snowflake(T0)].result.outcomes[0].status == "SIMULATED"            # open 4
    assert by_id["rt"].result.outcomes == [] or all(o.status == "NO_TRADE" for o in by_id["rt"].result.outcomes)
    assert by_id["half"].result.outcomes[0].status == "SIMULATED"                     # reduce 2
    edited = by_id[_snowflake(T0 + timedelta(hours=9))]
    assert edited.result.outcomes[0].status == "HOLD" and edited.result.outcomes[0].reasons == ["SOURCE_EDIT_REQUIRES_REVIEW"]
    assert coord.ideas()[0]["quantity"] == 2 and coord.ideas()[0]["status"] == "OPEN"   # the edited close did not fill
    assert by_id["half"].interpreted.classifications[0].message_class == MessageClass.REDUCE
    # archive receipt times are not latencies: the replay clock was publication + lag
    assert all(s.result.outcomes == [] or s.result.outcomes[0].status != "BLOCK" for s in steps)


def test_coercion_rejects_unknown_shapes_and_unauthenticated_records():
    with pytest.raises(TypeError):
        coerce_source_posts([42])
    with pytest.raises(TypeError):
        coerce_source_posts([{"id": "1", "created_at": "2026-01-01T00:00:00.000Z"}])   # needs expected_author_id
    other = coerce_source_posts([{"id": "1", "created_at": "2026-01-01T00:00:00.000Z", "author_id": "999",
                                  "text": "x", "edit_history_ids": ["1"]}], expected_author_id=AUTHOR)[0]
    assert not other.verified_author
