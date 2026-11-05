"""Offline, lossless-text export of an authenticated timeline backfill. No network or engine imports."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import subprocess
import tempfile
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

ARCHIVE_VERSION = "g0.archive.v1"
REFERENCE_TYPES = {"quoted", "replied_to", "retweeted"}


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def instant(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.utcoffset() is None:
        raise ValueError("timestamp needs an offset")
    return dt.astimezone(UTC)


def identifier(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]+", value):
        raise ValueError("expected a decimal ID string")
    return value


def validated_media(row: dict, media_root: Path) -> list[dict]:
    media = row["media"]
    downloads = row.get("media_files")
    if downloads is not None and len(media) != len(downloads):
        raise ValueError("media/download count mismatch")
    results = []
    keys = set()
    for index, item in enumerate(media):
        key = item["media_key"]
        if not isinstance(item.get("type"), str) or not item["type"]:
            raise ValueError("missing media type")
        if not isinstance(key, str) or not re.fullmatch(r"[0-9]+_[0-9]+", key) or key in keys:
            raise ValueError("invalid or duplicate media key")
        keys.add(key)
        receipt = downloads[index] if downloads is not None else {}
        filename = receipt.get("file")
        result = dict(item)
        result["download"] = dict(receipt)
        if filename is not None:
            if (not isinstance(filename, str) or Path(filename).name != filename
                    or Path(filename).stem != key):
                raise ValueError("invalid media filename or key mismatch")
            file = media_root / filename
            if file.is_symlink() or not file.is_file():
                raise ValueError("missing or unsafe media file")
            raw = file.read_bytes()
            if not raw or len(raw) != receipt.get("bytes", len(raw)):
                raise ValueError("media byte count mismatch")
            if receipt.get("status", 200) != 200:
                raise ValueError("failed download has a local filename")
            result.update(local_status="verified", sha256=sha256(raw), bytes=len(raw))
        elif receipt.get("status") == 200:
            raise ValueError("successful download missing filename")
        else:
            result["local_status"] = "unavailable" if receipt else "not_requested"
        results.append(result)
    return results


def normalize(row: dict, target: dict, digest: str, line: int, media_root: Path) -> dict:
    post_id = identifier(row["id"])
    author_id = identifier(row["author_id"])
    if not isinstance(target.get("username"), str) or not target["username"].strip():
        raise ValueError("timeline lookup lacks a username")
    if target.get("protected") is not True or identifier(target["id"]) != author_id:
        raise ValueError("post author differs from protected timeline target")
    history = [identifier(x) for x in row["edit_history_ids"]]
    if not history or history[-1] != post_id or len(set(history)) != len(history):
        raise ValueError("invalid revision chain")
    published, observed = row["created_at"], row["observed_at"]
    if instant(observed) < instant(published):
        raise ValueError("backfill observation predates publication")
    body = row["text"]
    if not isinstance(body, str) or not body.strip():
        raise ValueError("authoritative text missing")
    if not isinstance(row["short_text"], str):
        raise TypeError("missing API short field")
    if any(type(row[k]) is not bool for k in ("has_note", "is_quote", "is_reply")):
        raise ValueError("invalid source metadata flags")
    refs = row["referenced"]
    for ref in refs:
        identifier(ref["id"])
        if ref["type"] not in REFERENCE_TYPES:
            raise ValueError("unknown reference type")
    types = {ref["type"] for ref in refs}
    if row["is_quote"] != ("quoted" in types) or ("replied_to" in types and not row["is_reply"]):
        raise ValueError("reference flags disagree")
    repost = "retweeted" in types
    if repost:
        completeness = "repost_wrapper"
    elif not row["has_note"] and row["short_text"].rstrip().endswith("…"):
        completeness = "truncated_api_text"
    else:
        completeness = "api_body"
    return {
        "schema_version": ARCHIVE_VERSION,
        "post_id": post_id,
        "author": {"id": author_id, "username": target["username"], "protected_at_lookup": True},
        "published_at": published,
        "observed_at": observed,
        "conversation_id": identifier(row["conversation_id"]),
        "edit_history_ids": history,
        "post_family_id": history[0],
        "revision": len(history),
        "references": refs,
        "is_quote": row["is_quote"],
        "is_reply": row["is_reply"],
        "is_repost": repost,
        "text": body,
        "text_sha256": sha256(body.encode("utf-8")),
        "text_field": "note" if row["has_note"] else "text",
        "text_completeness": completeness,
        "media": validated_media(row, media_root),
        "provenance": {
            "platform": "x",
            "method": "authenticated_user_timeline_backfill",
            "capture_sha256": digest,
            "capture_line": line,
            "observation_clock": "page_request_start",
        },
    }


def rows_from_capture(capture: Path, target_path: Path, media_root: Path) -> list[dict]:
    raw = capture.read_bytes()
    digest = sha256(raw)
    target = json.loads(target_path.read_bytes())
    rows, ids, families = [], set(), {}
    for line, data in enumerate(raw.splitlines(), 1):
        try:
            row = normalize(json.loads(data), target, digest, line, media_root)
            if row["post_id"] in ids:
                raise ValueError("duplicate post ID")
            for version in row["edit_history_ids"]:
                family = families.setdefault(version, row["post_family_id"])
                if family != row["post_family_id"]:
                    raise ValueError("conflicting revision families")
        except (KeyError, TypeError, ValueError, OSError) as exc:
            raise ValueError(f"invalid source record at line {line}: {exc}") from exc
        ids.add(row["post_id"])
        rows.append(row)
    if not rows:
        raise ValueError("empty capture")
    return sorted(rows, key=lambda r: (instant(r["published_at"]), int(r["post_id"])))


def statistics(rows: list[dict]) -> dict:
    ids = {row["post_id"] for row in rows}
    refs = Counter()
    missing = Counter()
    media = Counter()
    completeness = Counter()
    for row in rows:
        completeness[row["text_completeness"]] += 1
        for ref in row["references"]:
            refs[ref["type"]] += 1
            if ref["id"] not in ids:
                missing[ref["type"]] += 1
        media.update(item["local_status"] for item in row["media"])
    return {
        "records": len(rows), "unique_ids": len(ids),
        "authors": len({r["author"]["id"] for r in rows}),
        "oldest": rows[0]["published_at"], "newest": rows[-1]["published_at"],
        "note_bodies": sum(r["text_field"] == "note" for r in rows),
        "edited": sum(r["revision"] > 1 for r in rows),
        "completeness": dict(completeness), "media": dict(media),
        "references": dict(refs), "missing_reference_targets": dict(missing),
    }


def development_rows(rows: list[dict], manifest: Path) -> list[dict]:
    """Allow only explicitly cleared development IDs; never read the scoring labels."""
    dispositions = {}
    allowed = {"DEVELOPMENT", "HOLDOUT", "HOLDOUT_CONTEXT_ONLY", "QUARANTINE", "UNASSIGNED"}
    with manifest.open(encoding="utf-8") as file:
        for line in file:
            item = json.loads(line)
            post_id = identifier(item["post_id"])
            if post_id in dispositions or item["disposition"] not in allowed:
                raise ValueError("duplicate or invalid split entry")
            dispositions[post_id] = item["disposition"]
    if dispositions.keys() != {r["post_id"] for r in rows}:
        raise ValueError("split manifest and capture must cover exactly the same post IDs")
    return [r for r in rows if dispositions[r["post_id"]] == "DEVELOPMENT"]


def private_destination(path: Path, repo: Path) -> Path:
    # Resolve symlinks before running Git. Only permit new, untracked files under the
    # project's existing ignored private roots. Never replace a capture, even on rerun.
    resolved = path.resolve()
    rel = resolved.relative_to(repo.resolve())
    if len(rel.parts) < 2 or rel.parts[0] not in {"private_samples", "data"}:
        raise ValueError("export must be in the repository's ignored private directory")
    ignored = subprocess.run(["git", "check-ignore", "-q", "--", str(rel)], cwd=repo, check=False)
    if ignored.returncode != 0 or path.is_symlink():
        raise ValueError("destination must be git-ignored and untracked")
    return resolved


def export(rows: list[dict], dest: Path, repo: Path) -> None:
    dest = private_destination(dest, repo)
    dest.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=dest.parent, delete=False) as stage:
        tmp = Path(stage.name)
        try:
            for row in rows:
                data = json.dumps(row, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                stage.write(data + b"\n")
            stage.flush()
            os.fsync(stage.fileno())
            tmp.chmod(0o400)
            os.link(tmp, dest)  # atomic publication, fail if a concurrent writer already won
        finally:
            tmp.unlink(missing_ok=True)


def read_history(path: Path, *, through: datetime | None = None, expected_sha256: str | None = None):
    """Read a chronological export up to and including an optional aware publication time."""
    if through is not None and through.utcoffset() is None:
        raise ValueError("through needs an offset")
    previous = None
    if expected_sha256 is not None:
        raw = path.read_bytes()
        if sha256(raw) != expected_sha256:
            raise ValueError("archive digest mismatch")
        source = io.BytesIO(raw)
    else:
        source = path.open("rb")
    with source as file:
        for line in file:
            row = json.loads(line)
            if row["schema_version"] != ARCHIVE_VERSION:
                raise ValueError("unsupported archive version")
            key = instant(row["published_at"]), int(identifier(row["post_id"]))
            if previous is not None and key <= previous:
                raise ValueError("duplicate or out-of-order event")
            if through is not None and key[0] > through:
                return
            if sha256(row["text"].encode("utf-8")) != row["text_sha256"]:
                raise ValueError("body digest mismatch")
            previous = key
            yield row


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture", type=Path)
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--media", type=Path, required=True)
    parser.add_argument("--out", type=Path, help="new ignored file (omit for audit only)")
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument("--full-archive", action="store_true", help="curator access, all splits")
    scope.add_argument("--split-manifest", type=Path, help="export only DEVELOPMENT records")
    parser.add_argument("--expected-count", type=int)
    args = parser.parse_args()
    rows = rows_from_capture(args.capture, args.target, args.media)
    if args.expected_count is not None and len(rows) != args.expected_count:
        parser.error("unexpected record count")
    summary = statistics(rows)
    if args.out is not None:
        if not args.full_archive and args.split_manifest is None:
            parser.error("choose --full-archive or --split-manifest before writing")
        selected = development_rows(rows, args.split_manifest) if args.split_manifest else rows
        export(selected, args.out, Path(__file__).resolve().parents[3])
        summary["exported_records"] = len(selected)
        summary["export_sha256"] = sha256(args.out.read_bytes())
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
