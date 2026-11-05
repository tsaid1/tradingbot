"""End-to-end compatibility with the Source Agent's real reader and exporter.

A synthetic G0-style capture goes through ``docs/research/source_corpus/archive.py`` (Agent 2's
exporter), the resulting archive export is read back with ``source_reader.historical_inputs`` (Agent
2's reader, pinned by SHA-256, with an explicit simulated lag), normalised with
``coerce_source_posts`` and replayed through interpreter, gateway and coordinator. No paid text.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from tradingbot.event_replay import PaperEventReplay
from tradingbot.evaluation import LabeledAction, LabeledPost, evaluate, parse_reader_args, posts_from_adapter
from tradingbot.models import Action, MockPolicy, Quote
from tradingbot.source_events import SNOWFLAKE_EPOCH_MS, Provenance, SourcePost, coerce_source_posts
from tradingbot.tkl_interpreter import MessageClass, TklInterpreter, replay_posts

source_corpus = pytest.importorskip("docs.research.source_corpus.source_reader")
archive = pytest.importorskip("docs.research.source_corpus.archive")

AUTHOR = "1237233159005179904"
T0 = datetime(2026, 11, 5, 14, 30, tzinfo=timezone.utc)
OBSERVED = "2026-11-06T10:00:00+00:00"


def _snowflake(moment: datetime) -> str:
    return str((int(moment.timestamp() * 1000) - SNOWFLAKE_EPOCH_MS) << 22)


def _capture_row(post_id, text, published, *, chain=None, referenced=(), has_note=False):
    return {"id": post_id, "author_id": AUTHOR, "created_at": published.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "conversation_id": post_id, "text": text, "has_note": has_note, "short_text": text if not has_note else text[:40] + "…",
            "referenced": list(referenced), "is_reply": any(r["type"] == "replied_to" for r in referenced),
            "is_quote": any(r["type"] == "quoted" for r in referenced), "edit_history_ids": chain or [post_id],
            "media": [], "observed_at": OBSERVED, "media_files": []}


@pytest.fixture
def development_export(tmp_path):
    """A development-style export produced by Agent 2's own exporter from a synthetic capture."""
    t1, t2, t3 = T0, T0 + timedelta(hours=5), T0 + timedelta(hours=9)
    open_id, half_id, root3 = _snowflake(t1), _snowflake(t2), _snowflake(t3)
    edit3 = _snowflake(t3 + timedelta(seconds=40))
    rt_id = _snowflake(T0 + timedelta(hours=1))
    rows = [
        _capture_row(open_id, "We are now long $TLT at $88.50. Stop: $86.00. Target: $95.00.", t1),
        _capture_row(rt_id, "RT @someone: synthetic…", T0 + timedelta(hours=1), referenced=[{"type": "retweeted", "id": "7"}]),
        _capture_row(half_id, "Taking profits on half of our $TLT long at $92.00.", t2),
        # an edited close: the original version is not in the capture (as in the real archive),
        # so the chain names the root and the archive carries only the revised body
        _capture_row(edit3, "Closing the remaining $TLT long here at $94.10.", t3 + timedelta(seconds=40), chain=[root3, edit3]),
    ]
    capture = tmp_path / "capture.jsonl"
    capture.write_text("".join(json.dumps(r) + "\n" for r in rows))
    target = tmp_path / "target.json"
    target.write_text(json.dumps({"id": AUTHOR, "name": "SYNTHETIC", "protected": True, "username": "synthetic"}))
    media = tmp_path / "media"
    media.mkdir()
    archive_rows = archive.rows_from_capture(capture, target, media)
    export = tmp_path / "development.jsonl"
    export.write_bytes(b"".join(json.dumps(r, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n" for r in archive_rows))
    return export, hashlib.sha256(export.read_bytes()).hexdigest(), {"open": open_id, "rt": rt_id, "half": half_id, "root3": root3, "edit3": edit3}


def test_real_reader_output_coerces_into_source_posts(development_export):
    export, digest, ids = development_export
    items = list(source_corpus.historical_inputs(export, expected_sha256=digest, expected_author_id=AUTHOR,
                                                 simulated_lag=timedelta(seconds=12.4)))
    assert len(items) == 4 and all(hasattr(i, "as_source_post") for i in items)
    posts = coerce_source_posts(items, expected_author_id=AUTHOR)
    assert [p.source_id for p in posts] == [ids["open"], ids["rt"], ids["half"], ids["root3"]]
    opened, rt, half, edited = posts
    assert opened.revision == 1 and opened.verified_author and opened.text_complete and opened.provenance == Provenance.ARCHIVE_API_USER_CONTEXT
    assert rt.is_repost and not rt.text_complete
    assert edited.revision == 2 and edited.post_id == ids["edit3"] and edited.revised_at == T0 + timedelta(hours=9, seconds=40)
    assert abs((edited.published_at - (T0 + timedelta(hours=9))).total_seconds()) < 0.001   # root snowflake time
    assert edited.available_at == edited.revised_at
    assert all(p.received_at == datetime.fromisoformat(OBSERVED) for p in posts)              # archive clock, not latency
    assert all(p.to_replay_source().post_hash == p.text_sha256 for p in posts)


def test_real_reader_pins_the_digest_and_requires_a_lag(development_export):
    export, digest, _ = development_export
    with pytest.raises(ValueError):
        list(source_corpus.historical_inputs(export, expected_sha256="0" * 64, expected_author_id=AUTHOR, simulated_lag=timedelta(0)))
    with pytest.raises(ValueError):
        list(source_corpus.historical_inputs(export, expected_sha256=digest, expected_author_id=AUTHOR, simulated_lag=timedelta(seconds=-1)))
    with pytest.raises(ValueError):
        list(source_corpus.historical_inputs(export, expected_sha256=digest, expected_author_id="999", simulated_lag=timedelta(0)))


def test_cli_reader_args_match_the_real_signature(development_export):
    export, digest, ids = development_export
    args = parse_reader_args([f"path={export}", f"expected_sha256={digest}", f"expected_author_id={AUTHOR}",
                              "simulated_lag=12.4", "through=2026-11-06T00:00:00Z"])
    assert isinstance(args["path"], Path) and args["simulated_lag"] == timedelta(seconds=12.4)
    assert args["expected_author_id"] == AUTHOR and args["expected_sha256"] == digest
    assert args["through"] == datetime(2026, 11, 6, tzinfo=timezone.utc)
    posts = posts_from_adapter(source_corpus.historical_inputs, args, expected_author_id=AUTHOR)
    assert [p.source_id for p in posts] == [ids["open"], ids["rt"], ids["half"], ids["root3"]]


def test_real_export_replays_end_to_end_through_gateway_and_coordinator(development_export, tmp_path):
    export, digest, ids = development_export
    posts = coerce_source_posts(source_corpus.historical_inputs(export, expected_sha256=digest, expected_author_id=AUTHOR,
                                                                simulated_lag=timedelta(seconds=12.4)))
    coord = PaperEventReplay(tmp_path / "paper.sqlite3", MockPolicy(quantity=4))
    mids = {ids["open"]: "88.50", ids["half"]: "92.00", ids["root3"]: "94.10"}

    def quotes(p, now):
        mid = Decimal(mids.get(p.source_id, "90.00"))
        return {"TLT": Quote(symbol="TLT", bid=mid - Decimal("0.05"), ask=mid + Decimal("0.05"), observed_at=now)}

    steps = replay_posts(posts, coord, TklInterpreter(), quotes, lag_seconds=12.4)
    by_id = {s.post.source_id: s for s in steps}
    assert by_id[ids["open"]].result.outcomes[0].status == "SIMULATED"
    assert by_id[ids["rt"]].result.outcomes == [] or all(o.status == "NO_TRADE" for o in by_id[ids["rt"]].result.outcomes)
    assert by_id[ids["half"]].result.outcomes[0].status == "SIMULATED" and by_id[ids["half"]].result.outcomes[0].order.quantity == 2
    assert by_id[ids["root3"]].result.outcomes[0].reasons == ["SOURCE_EDIT_REQUIRES_REVIEW"]   # the edited close holds
    assert coord.ideas()[0]["quantity"] == 2 and coord.ideas()[0]["status"] == "OPEN"
    labels = {ids["half"]: LabeledPost(source_id=ids["half"], actions=[LabeledAction(
        message_class=MessageClass.REDUCE, action=Action.REDUCE, asset="TLT", close_fraction="0.5", links_to=ids["open"], executable=True)])}
    report = evaluate(posts, labels, dataset="compat")
    assert report.linkage["correct"] == 1 and report.false_executable == [] and report.missed_instructions == []


def test_coercion_rejects_unknown_shapes_and_unauthenticated_records():
    with pytest.raises(TypeError):
        coerce_source_posts([42])
    with pytest.raises(TypeError):
        coerce_source_posts([{"id": "1", "created_at": "2026-01-01T00:00:00.000Z"}])
    other = coerce_source_posts([{"id": "1", "created_at": "2026-01-01T00:00:00.000Z", "author_id": "999",
                                  "text": "x", "edit_history_ids": ["1"]}], expected_author_id=AUTHOR)[0]
    assert not other.verified_author

    class Bad:
        def as_source_post(self):
            return "not a post"

    with pytest.raises(TypeError):
        coerce_source_posts([Bad()])
    assert coerce_source_posts([]) == []
