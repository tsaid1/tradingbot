"""Source engineering contracts, all examples generated locally from fictional values."""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "docs/research"))
from source_corpus.archive import (
    development_rows,
    export,
    read_history,
    rows_from_capture,
    statistics,
)
from source_corpus.source_reader import historical_inputs

OPEN = "2030-01-01T12:00:00Z"
LATER = "2030-01-01T12:01:00Z"


def record(post_id="101", **overrides):
    return {
        "id": post_id, "author_id": "42", "created_at": OPEN,
        "conversation_id": post_id, "text": "Fictional long body Ω\nwith a second line.",
        "has_note": True, "short_text": "Fictional excerpt…", "referenced": [],
        "is_reply": False, "is_quote": False, "edit_history_ids": [post_id],
        "media": [], "observed_at": "2031-01-01T01:00:00+01:00", **overrides,
    }


@pytest.fixture
def store(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".gitignore").write_text("data/\nprivate_samples/\n")
    (tmp_path / "media").mkdir()
    lookup = tmp_path / "target.json"
    lookup.write_text(json.dumps({"id": "42", "protected": True, "username": "example"}))

    def load(*records):
        capture = tmp_path / "raw.jsonl"
        capture.write_bytes(b"".join(json.dumps(r).encode() + b"\n" for r in records))
        return rows_from_capture(capture, lookup, tmp_path / "media")
    return load


def test_export_round_trip_and_chronological_boundary(store, tmp_path):
    second = record("104", created_at=LATER, edit_history_ids=["103", "104"],
                    referenced=[{"type": "quoted", "id": "100"}], is_quote=True)
    earlier = record()
    rows = store(second, earlier)
    out = tmp_path / "private_samples" / "demo.jsonl"
    export(rows, out, tmp_path)
    assert list(read_history(out)) == rows
    when = datetime(2030, 1, 1, 12, tzinfo=UTC)
    assert list(read_history(out, through=when)) == rows[:1]
    assert rows[0]["text"] == earlier["text"] != earlier["short_text"]
    assert rows[0]["text_sha256"] == hashlib.sha256(earlier["text"].encode()).hexdigest()
    assert rows[0]["observed_at"] == earlier["observed_at"]
    assert rows[0]["provenance"]["capture_line"] == 2
    assert rows[1]["post_family_id"] == "103" and rows[1]["revision"] == 2
    assert rows[1]["edit_history_ids"] == ["103", "104"]
    assert rows[1]["references"] == second["referenced"]
    assert statistics(rows)["missing_reference_targets"] == {"quoted": 1}
    assert out.stat().st_mode & 0o777 == 0o400
    with pytest.raises(FileExistsError):
        export(rows[:1], out, tmp_path)
    assert list(read_history(out)) == rows


def test_source_completeness_is_separate_from_references_and_media(store):
    repost = record("102", has_note=False, text="RT @example: shortened…",
                    referenced=[{"type": "retweeted", "id": "99"}])
    uncertain = record("103", has_note=False, text="Truncated…", short_text="Truncated…")
    unavailable = record("104", is_reply=True, referenced=[{"type": "replied_to", "id": "99"}],
                         media=[{"media_key": "3_100", "type": "photo"}],
                         media_files=[{"status": 404}])
    rows = store(repost, uncertain, unavailable)
    assert [r["text_completeness"] for r in rows] == [
        "repost_wrapper", "truncated_api_text", "api_body"]
    assert rows[2]["media"][0]["local_status"] == "unavailable"


def test_local_media_hash_and_filename_association(store, tmp_path):
    data = b"Synthetic attachment"
    (tmp_path / "media" / "3_102.png").write_bytes(data)
    media = [{"media_key": "3_102", "type": "photo"}]
    receipt = [{"file": "3_102.png", "bytes": len(data), "status": 200}]
    r = store(record(media=media, media_files=receipt))[0]
    assert r["media"][0]["sha256"] == hashlib.sha256(data).hexdigest()
    assert r["media"][0]["download"] == receipt[0]
    receipt[0]["bytes"] += 1
    with pytest.raises(ValueError, match="byte count"):
        store(record(media=media, media_files=receipt))


@pytest.mark.parametrize("change", [
    {"id": 12}, {"author_id": "43"}, {"edit_history_ids": []},
    {"edit_history_ids": ["101", "101"]}, {"edit_history_ids": ["100"]},
    {"created_at": "2030-01-01T00:00:00"}, {"observed_at": "2029-01-01T00:00:00Z"},
    {"text": ""}, {"has_note": "false"}, {"is_quote": True},
    {"referenced": [{"type": "future_kind", "id": "100"}]},
    {"media": [{"media_key": "3_102", "type": "photo"}], "media_files": []},
    {"media": [{"media_key": "3_102", "type": "photo"}], "media_files": [{"status": 200}]},
    {"media": [{"media_key": "3_102", "type": "photo"}],
     "media_files": [{"file": "../3_102.png", "bytes": 1, "status": 200}]},
])
def test_corrupt_capture_rejected(store, change):
    with pytest.raises(ValueError):
        store(record(**change))


def test_duplicates_empty_capture_and_cross_family_collisions_rejected(store):
    with pytest.raises(ValueError, match="duplicate post ID"):
        store(record(), record())
    with pytest.raises(ValueError, match="empty capture"):
        store()
    with pytest.raises(ValueError, match="conflicting revision"):
        store(record(), record("102", edit_history_ids=["100", "101", "102"]))


def test_export_refuses_public_paths_and_escape(store, tmp_path):
    rows = store(record())
    with pytest.raises(ValueError):
        export(rows, tmp_path / "public.jsonl", tmp_path)
    (tmp_path / "data").mkdir()
    (tmp_path / "public").mkdir()
    (tmp_path / "data" / "link").symlink_to(tmp_path / "public", target_is_directory=True)
    with pytest.raises(ValueError):
        export(rows, tmp_path / "data/link/leak.jsonl", tmp_path)
    assert not (tmp_path / "public/leak.jsonl").exists()


def test_reader_rejects_tampering_and_unsorted_input(store, tmp_path):
    a, b = store(record(), record("102"))
    path = tmp_path / "synthetic"
    for values, reason in (([b, a], "out-of-order"), ([a, a], "out-of-order"),
                           ([dict(a, text="changed")], "digest"),
                           ([dict(a, schema_version="unknown")], "version")):
        path.write_text("".join(json.dumps(r) + "\n" for r in values))
        with pytest.raises(ValueError, match=reason):
            list(read_history(path))


def test_reader_split_requires_exact_partition_and_positive_dev_clearance(store, tmp_path):
    rows = store(record("1"), record("2"), record("3"))
    manifest = tmp_path / "split.jsonl"

    def write(items):
        manifest.write_text("".join(
            json.dumps({"post_id": i, "disposition": d}) + "\n" for i, d in items
        ))

    splits = [("1", "HOLDOUT"), ("2", "DEVELOPMENT"), ("3", "UNASSIGNED")]
    write(splits)
    assert development_rows(rows, manifest) == rows[1:2]
    for broken in (splits[:2], [*splits, splits[0]], [*splits, ("4", "DEVELOPMENT")],
                   [*splits[:2], ("3", "DEVELOPMENT?")]):
        write(broken)
        with pytest.raises(ValueError):
            development_rows(rows, manifest)


def test_interpreter_mapping_eligibility_and_as_of_revision(store, tmp_path):
    def snowflake(when):
        millis = int(datetime.fromisoformat(when).timestamp()) * 1000
        return str((millis - 1288834974657) << 22)

    root, edited_id = snowflake(OPEN), snowflake(LATER)
    rows = store(record(root), record(edited_id, created_at=LATER,
                                      edit_history_ids=[root, edited_id]))
    path = tmp_path / "data" / "test.jsonl"
    export(rows, path, tmp_path)
    args = {"expected_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "expected_author_id": "42", "simulated_lag": timedelta(seconds=2)}
    before_edit = list(historical_inputs(path, through=datetime.fromisoformat(LATER), **args))
    assert len(before_edit) == 1
    a, b = list(historical_inputs(path, **args))
    assert a.as_source_event().source_id == root
    assert a.as_source_event().text == rows[0]["text"]
    assert a.as_source_event().source_type == "protected_feed"
    assert a.replay_source.published_at == b.replay_source.published_at == datetime.fromisoformat(OPEN)
    assert b.replay_source.source_id == root and b.replay_source.revision == 2
    assert b.revised_at == datetime.fromisoformat(LATER)
    assert b.replay_source.received_at == b.revised_at + timedelta(seconds=2)
    with pytest.raises(ValueError, match="legacy"):
        b.as_source_event()
    for overrides, reason in (({"expected_author_id": "666"}, "author"),
                              ({"expected_sha256": "f" * 64}, "digest"),
                              ({"simulated_lag": timedelta(seconds=-1)}, "nonnegative")):
        with pytest.raises(ValueError, match=reason):
            list(historical_inputs(path, **(args | overrides)))


def test_reposts_and_truncation_never_exposed_to_legacy_event(store, tmp_path):
    rows = store(record("102", referenced=[{"id": "99", "type": "retweeted"}]),
                 record("103", has_note=False, short_text="Cut…", text="Cut…"),
                 record("104", has_note=False, short_text="A full thought... https://example.invalid",
                        text="A full thought... https://example.invalid"))
    path = tmp_path / "data" / "inputs.jsonl"
    export(rows, path, tmp_path)
    events = list(historical_inputs(path, expected_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                                    simulated_lag=timedelta(0), expected_author_id="42"))
    for e in events[:2]:
        assert not e.replay_source.complete_text
        with pytest.raises(ValueError, match="legacy"):
            e.as_source_event()
    assert events[2].as_source_event().source_id == "104"
