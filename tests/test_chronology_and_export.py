"""Availability-time chronology for edited revisions, and the development-export intake. Synthetic only."""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from tradingbot.evaluation import (
    LabeledAction, LabeledPost, coerce_reader_arg, evaluate, load_export, parse_reader_args, posts_from_adapter,
    verify_sha256,
)
from tradingbot.event_replay import PaperEventReplay
from tradingbot.models import Action, MockPolicy, Quote
from tradingbot.source_events import Provenance, SourcePost, coerce_source_posts
from tradingbot.tkl_interpreter import MessageClass, TklInterpreter, replay_posts

REPO = Path(__file__).resolve().parents[1]
FIXTURES = REPO / "fixtures" / "eval_synthetic"
T0 = datetime(2026, 10, 1, 14, 30, tzinfo=timezone.utc)
OPEN = "We are now long $TLT at $88.50. Stop: $86.00. Target: $95.00."
CLOSE = "Closing our $TLT long here at $92.00."


def post(sid, text, *, published=0.0, revision=1, revised=None, provenance=Provenance.SYNTHETIC, received=None):
    p = T0 + timedelta(hours=published)
    return SourcePost(source_id=sid, post_id=f"{sid}-v{revision}", revision=revision, author_id="S", verified_author=True,
                      text=text, text_complete=True, published_at=p,
                      revised_at=T0 + timedelta(hours=revised) if revised is not None else None,
                      received_at=T0 + timedelta(hours=received) if received is not None else p, provenance=provenance)


def quotes(p, now):
    mid = Decimal("88.50") if p.post_id == "a-v1" else Decimal("92.00")   # the open must sit inside the collar
    return {"TLT": Quote(symbol="TLT", bid=mid - Decimal("0.05"), ask=mid + Decimal("0.05"), observed_at=now)}


# ----------------------------------------------------------------------------- 1. availability time

def test_available_at_is_receipt_for_live_edit_time_for_revisions_else_publication():
    original = post("a", OPEN)
    edit = post("a", OPEN + " Edited.", revision=2, revised=2)
    live = post("l", OPEN, provenance=Provenance.LIVE_API_USER_CONTEXT, received=0.004)
    archived_edit_without_time = post("b", OPEN, revision=2)
    assert original.available_at == T0
    assert edit.available_at == T0 + timedelta(hours=2) and edit.published_at == T0
    assert live.available_at == live.received_at > live.published_at
    assert archived_edit_without_time.available_at == archived_edit_without_time.published_at   # fallback


def test_posts_between_original_and_edit_are_processed_before_the_edit(tmp_path):
    original = post("a", OPEN)                                   # T0
    between = post("b", CLOSE, published=1)                      # T0 + 1 h, references a
    edit = post("a", OPEN + " Edited.", revision=2, revised=2)   # available only at T0 + 2 h
    assert [p.post_id for p in coerce_source_posts([edit, between, original])] == ["a-v1", "b-v1", "a-v2"]

    seen_now = []

    def provider(p, now):
        seen_now.append((p.post_id, now))
        return quotes(p, now)

    coord = PaperEventReplay(tmp_path / "paper.sqlite3", MockPolicy(quantity=2))
    steps = replay_posts([edit, between, original], coord, TklInterpreter(), provider, lag_seconds=10)
    assert [s.post.post_id for s in steps] == ["a-v1", "b-v1", "a-v2"]
    assert [n for _, n in seen_now] == sorted(n for _, n in seen_now)                      # the clock never goes back
    assert seen_now[2] == ("a-v2", T0 + timedelta(hours=2, seconds=10))                   # edit clocked at its edit time
    assert steps[0].result.outcomes[0].status == "SIMULATED"
    assert steps[1].result.outcomes[0].status == "SIMULATED" and steps[1].result.outcomes[0].order.side == "SELL"
    assert steps[2].result.outcomes[0].reasons == ["SOURCE_EDIT_REQUIRES_REVIEW"]
    assert coord.ideas()[0]["status"] == "CLOSED"


def test_evaluation_scores_in_availability_order():
    original = post("a", OPEN)
    between = post("b", CLOSE, published=1)
    edit = post("a", OPEN, revision=2, revised=2)
    labels = {"b": LabeledPost(source_id="b", actions=[LabeledAction(message_class=MessageClass.CLOSE, action=Action.CLOSE,
                                                                     asset="TLT", links_to="a", executable=True)])}
    report = evaluate([edit, between, original], labels, dataset="unit")
    assert report.linkage["correct"] == 1 and report.missed_instructions == []


def test_live_posts_order_by_receipt_not_publication(tmp_path):
    # Published earlier but received later (slow exposure) must be processed after the one received first.
    slow = post("s", OPEN, provenance=Provenance.LIVE_API_USER_CONTEXT, published=0, received=0.02)
    fast = post("f", "We are now long $GLD at $280 with a stop at $270.", provenance=Provenance.LIVE_API_USER_CONTEXT,
                published=0.005, received=0.01)
    assert [p.post_id for p in coerce_source_posts([slow, fast])] == ["f-v1", "s-v1"]
    coord = PaperEventReplay(tmp_path / "paper.sqlite3", MockPolicy(quantity=1))
    steps = replay_posts([slow, fast], coord, TklInterpreter(), lambda p, now: {}, lag_seconds=0)
    assert [s.post.post_id for s in steps] == ["f-v1", "s-v1"]


# ----------------------------------------------------------------------------- 2. export intake

def _export(tmp_path, lines=None):
    export = tmp_path / "dev_export.jsonl"
    export.write_text((FIXTURES / "posts.jsonl").read_text() if lines is None else lines)
    return export


def _manifest(tmp_path, export, **over):
    data = {"split": "development", "post_count": 33, "sha256": hashlib.sha256(export.read_bytes()).hexdigest(),
            "source": "synthetic stand-in", "agent_version": "ignored extra key"}
    data.update(over)
    manifest = tmp_path / "dev_export.manifest.json"
    manifest.write_text(json.dumps(data))
    return manifest


def test_export_loads_with_matching_checksum_and_split(tmp_path):
    export = _export(tmp_path)
    manifest = _manifest(tmp_path, export)
    posts, info = load_export(export, manifest=manifest)
    assert len(posts) == 33 and info["split"] == "development" and info["posts"] == 33
    assert info["sha256"] == hashlib.sha256(export.read_bytes()).hexdigest()
    assert [p.post_id for p in posts] == [p.post_id for p in sorted(posts, key=lambda p: (p.available_at, p.revision, p.post_id))]


def test_export_refuses_bad_checksum_count_and_holdout(tmp_path):
    export = _export(tmp_path)
    with pytest.raises(ValueError, match="Checksum mismatch"):
        load_export(export, manifest=_manifest(tmp_path, export, sha256="0" * 64))
    with pytest.raises(ValueError, match="declares 120 posts"):
        load_export(export, manifest=_manifest(tmp_path, export, post_count=120))
    with pytest.raises(ValueError, match="holdout"):
        load_export(export, manifest=_manifest(tmp_path, export, split="holdout"))
    with pytest.raises(ValueError, match="checksum is required"):
        load_export(export)
    with pytest.raises(ValueError, match="disagrees"):
        load_export(export, manifest=_manifest(tmp_path, export), sha256="1" * 64)
    posts, _ = load_export(export, sha256=hashlib.sha256(export.read_bytes()).hexdigest())
    assert len(posts) == 33
    posts, info = load_export(export, manifest=_manifest(tmp_path, export, split="holdout"), allow_holdout=True)
    assert info["split"] == "holdout"


def test_verify_sha256_is_case_insensitive(tmp_path):
    f = tmp_path / "x"
    f.write_text("abc")
    digest = hashlib.sha256(b"abc").hexdigest()
    assert verify_sha256(f, digest.upper()) == digest


def test_reader_args_are_typed_and_passed_to_the_adapter():
    assert coerce_reader_arg("120") == 120 and coerce_reader_arg("development") == "development"
    assert coerce_reader_arg("2025-01-01T00:00:00Z") == datetime(2025, 1, 1, tzinfo=timezone.utc)
    args = parse_reader_args(["split=development", "start=2025-01-01T00:00:00Z", "limit=5"])
    assert args == {"split": "development", "start": datetime(2025, 1, 1, tzinfo=timezone.utc), "limit": 5}
    with pytest.raises(ValueError):
        parse_reader_args(["nonsense"])
    received = {}

    def historical_inputs(**kwargs):
        received.update(kwargs)
        yield post("z", OPEN)
        yield post("y", OPEN, published=-1)

    posts = posts_from_adapter(historical_inputs, args)
    assert received == args and [p.post_id for p in posts] == ["y-v1", "z-v1"]


def test_cli_export_path_with_manifest_and_label_checksum(tmp_path):
    export = _export(tmp_path)
    labels = tmp_path / "dev_labels.jsonl"
    labels.write_text((FIXTURES / "labels.jsonl").read_text())
    manifest = _manifest(tmp_path, export, labels_sha256=hashlib.sha256(labels.read_bytes()).hexdigest())
    out = tmp_path / "results"
    proc = subprocess.run([sys.executable, str(REPO / "scripts" / "evaluate_corpus.py"), "--export", str(export),
                           "--manifest", str(manifest), "--labels", str(labels), "--out", str(out)],
                          capture_output=True, text=True, cwd=REPO, check=False)
    assert proc.returncode == 0, proc.stderr[-800:]
    data = json.loads((out / "report.json").read_text())
    assert data["input"]["split"] == "development" and data["input"]["sha256"] == hashlib.sha256(export.read_bytes()).hexdigest()
    assert data["input"]["labels_sha256"] == hashlib.sha256(labels.read_bytes()).hexdigest() and data["dataset"] == "development"
    bad = subprocess.run([sys.executable, str(REPO / "scripts" / "evaluate_corpus.py"), "--export", str(export),
                          "--sha256", "f" * 64, "--labels", str(labels), "--out", str(out)],
                         capture_output=True, text=True, cwd=REPO, check=False)
    assert bad.returncode != 0 and "Checksum mismatch" in bad.stdout + bad.stderr
    tampered = subprocess.run([sys.executable, str(REPO / "scripts" / "evaluate_corpus.py"), "--export", str(export),
                               "--manifest", str(manifest), "--labels", str(labels), "--labels-sha256", "e" * 64, "--out", str(out)],
                              capture_output=True, text=True, cwd=REPO, check=False)
    assert tampered.returncode != 0 and "Checksum mismatch" in tampered.stdout + tampered.stderr


def test_cli_refuses_holdout_split_via_adapter_args(tmp_path):
    proc = subprocess.run([sys.executable, str(REPO / "scripts" / "evaluate_corpus.py"), "--adapter", "json:loads",
                           "--reader-arg", "split=holdout", "--labels", str(FIXTURES / "labels.jsonl"), "--out", str(tmp_path / "o")],
                          capture_output=True, text=True, cwd=REPO, check=False)
    assert proc.returncode != 0 and "holdout" in (proc.stdout + proc.stderr).lower()
