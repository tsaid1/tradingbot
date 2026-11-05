"""Audit one G0 live session locally. The JSON report contains aggregate metrics only."""

from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from statistics import median


def timestamp(value: str) -> datetime:
    stamp = datetime.fromisoformat(value)
    if stamp.tzinfo is None:
        raise ValueError("A poll timestamp is missing its time zone")
    return stamp


def distribution(values: list[float]) -> dict:
    ordered = sorted(values)

    def upper(percent: float):
        return round(ordered[math.ceil(len(ordered) * percent) - 1], 3) if ordered else None

    return {"n": len(ordered), "p50": round(median(ordered), 3) if ordered else None,
            "p95": upper(.95), "p99": upper(.99)}


def authorization(control: dict | None, target_id: str | None = None) -> str:
    """An empty page alone cannot distinguish authorization from an empty timeline."""
    if not control:
        return "UNMEASURED"
    count = control.get("n_posts")
    if type(count) is not int or count < 0:
        return "INCONCLUSIVE"
    if count:
        return "FAIL"
    errors = [e for e in control.get("errors") or [] if isinstance(e, dict)]
    if target_id:
        # Only the standalone capture retains the denied resource's identity.
        error_types = {e.get("type") for e in errors if e.get("resource_id") == target_id}
    else:
        error_types = {e.get("type") for e in errors}
        error_types.update(control.get("error_types") or [])
    if control.get("status") in (200, 403) and (
        "https://api.x.com/2/problems/not-authorized-for-resource" in error_types
        or "https://api.twitter.com/2/problems/not-authorized-for-resource" in error_types
    ):
        return "PASS"
    return "INCONCLUSIVE"


def audit(records: list[dict], controls: dict, min_posts: int = 3) -> dict:
    if min_posts < 1:
        raise ValueError("min_posts must be positive")
    by_kind = defaultdict(list)
    for record in records:
        by_kind[record["kind"]].append(record)
    if len(by_kind["meta"]) != 1 or len(by_kind["baseline"]) > 1 or len(by_kind["end"]) > 1:
        raise ValueError("Supply one poll session")
    meta = by_kind["meta"][0]
    target = meta["target"]
    if not target.get("id") or target.get("protected") is not True:
        raise ValueError("A protected target is required")
    baseline = next(iter(by_kind["baseline"]), {})
    base_ids = {p["id"] for p in baseline.get("posts", [])}
    good_baseline = bool(baseline.get("status") == 200 and not baseline.get("errors")
                         and baseline.get("since_id") and baseline["since_id"] in base_ids)
    end = next(iter(by_kind["end"]), {})
    finished = end.get("outcome") in ("done", "enough")

    seen = set(base_ids)
    samples = defaultdict(list)
    floors = defaultdict(list)
    media_lags = defaultdict(list)
    gaps = defaultdict(list)
    previous_phase = None
    first_sent = last_sent = last_receipt = None
    for phase in by_kind["phase"]:
        samples[float(phase["interval_s"])]  # include scheduled intervals with zero samples
    paired, bad_calls, wrong_author, duplicates, excluded = [], 0, 0, 0, 0
    for call in by_kind["call"]:
        cadence = float(call["interval_s"])
        phase = (call.get("phase"), cadence)
        if call.get("sent_at"):
            sent = timestamp(call["sent_at"])
            if last_sent is not None and phase == previous_phase:
                gaps[cadence].append((sent - last_sent).total_seconds())
            if first_sent is None:
                first_sent = sent
            last_sent, previous_phase = sent, phase
        if call.get("received_at"):
            last_receipt = timestamp(call["received_at"])
        good_call = call.get("status") == 200 and not call.get("errors")
        bad_calls += not good_call
        new_target = False
        for post in call.get("posts", []):
            pid = post.get("id")
            if not pid:
                excluded += 1
                continue
            if pid in seen or post.get("duplicate"):
                duplicates += 1
                continue
            seen.add(pid)
            if post.get("author_id") != target["id"]:
                wrong_author += 1
                continue
            new_target = True
            if (not good_baseline or not good_call or post.get("edit_of_seen")
                    or not post.get("created_at") or not call.get("received_at")):
                excluded += 1
                continue
            created = timestamp(post["created_at"])
            lag = (timestamp(call["received_at"]) - created).total_seconds()
            if lag < 0 or created < timestamp(baseline["sent_at"]):
                excluded += 1
                continue
            samples[cadence].append(lag)
            if post.get("x_delay_floor_s") is not None:
                floors[cadence].append(post["x_delay_floor_s"])
            for media in post.get("media_fetch", []):
                if media.get("status") == 200 and media.get("media_lag_s") is not None:
                    media_lags[cadence].append(media["media_lag_s"])
        if new_target:
            paired.append(authorization(call.get("bearer_control")))

    fresh = samples[15.0]
    freshness = "UNMEASURED"
    if fresh:
        freshness = "INCONCLUSIVE"
        if finished and good_baseline and len(fresh) >= min_posts:
            freshness = "PASS" if median(fresh) < 30 else "FAIL"
    paired_status = "UNMEASURED"
    if paired:
        paired_status = "FAIL" if "FAIL" in paired else (
            "PASS" if all(p == "PASS" for p in paired) else "INCONCLUSIVE"
        )
    recon = next(iter(by_kind["reconcile"]), {})
    recon_status = "UNMEASURED"
    if recon:
        # The original probe stops after 10 pages without retaining pagination state.
        complete = (finished and good_baseline and recon.get("status") == 200
                    and 0 < recon.get("pages", 0) < 10
                    and type(recon.get("reported")) is int
                    and type(recon.get("polled")) is int
                    and isinstance(recon.get("missed_ids"), list)
                    and isinstance(recon.get("vanished_ids"), list))
        recon_status = "INCONCLUSIVE"
        if complete:
            matched = (not recon["missed_ids"] and not recon["vanished_ids"]
                       and recon["reported"] == recon["polled"] == len(seen - base_ids))
            recon_status = "PASS" if matched else "FAIL"

    return {
        "run_finished": finished, "calls": len(by_kind["call"]), "call_errors": bad_calls,
        "baseline_ok": good_baseline, "wrong_author": wrong_author, "duplicates": duplicates,
        "excluded_target_samples_or_missing_ids": excluded,
        "measured_window": {"first_sent_at": first_sent.isoformat() if first_sent else None,
                            "last_received_at": last_receipt.isoformat() if last_receipt else None},
        "freshness_at_15s": freshness, "min_samples_at_15s": min_posts,
        "latency_by_effective_interval_s": {
            str(interval): {"publication_to_receipt_s": distribution(values),
                            "observed_send_gap_s": distribution(gaps[interval]),
                            "exposure_delay_floor_s": distribution(floors[interval]),
                            "publication_to_media_s": distribution(media_lags[interval])}
            for interval, values in sorted(samples.items())
        },
        "standalone_bearer": authorization(controls.get("bearer_timeline"), target["id"]),
        "calls_delivering_new_target_posts": len(paired),
        "paired_explicit_denials": paired.count("PASS"), "paired_bearer": paired_status,
        "reconciliation": recon_status, "missed": len(recon["missed_ids"]) if isinstance(recon.get("missed_ids"), list) else None,
        "vanished": len(recon["vanished_ids"]) if isinstance(recon.get("vanished_ids"), list) else None,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("poll", type=Path)
    parser.add_argument("controls", type=Path)
    args = parser.parse_args()
    with args.poll.open(encoding="utf-8") as stream:
        records = [json.loads(line) for line in stream if line.strip()]
    print(json.dumps(audit(records, json.loads(args.controls.read_text())), indent=2))


if __name__ == "__main__":
    main()
