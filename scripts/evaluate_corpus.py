"""Evaluate the TKL interpreter against a labeled dataset and run golden replays. Mock-only.

Synthetic smoke run (no private data):
    python scripts/evaluate_corpus.py --synthetic --out data/eval/results_synthetic

Real development dataset (private, git-ignored; never the holdout):
    python scripts/evaluate_corpus.py --posts data/eval/dev_posts.jsonl --labels data/eval/dev_labels.jsonl \\
        --expected-author <account id> --golden data/eval/golden --out data/eval/results_dev
    python scripts/evaluate_corpus.py --adapter my_adapter:historical_inputs --start 2025-01-01T00:00:00Z \\
        --end 2026-11-01T00:00:00Z --labels data/eval/dev_labels.jsonl --expected-author <account id>

Reports hold aggregates, IDs and failure codes only; post text is never written or printed.
Paths containing "holdout" are refused unless --allow-holdout is given (final evaluation only).
"""
from __future__ import annotations

import argparse
import importlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from tradingbot.evaluation import GoldenResult, evaluate, load_golden, load_labels, load_posts, run_golden
from tradingbot.source_events import coerce_source_posts

REPO = Path(__file__).resolve().parents[1]
SYNTHETIC = REPO / "fixtures" / "eval_synthetic"


def _guard_holdout(paths: list[Path | None], allow: bool) -> None:
    for p in paths:
        if p is not None and "holdout" in str(p).lower() and not allow:
            sys.exit(f"Refusing {p}: the holdout is reserved for the final evaluation (pass --allow-holdout to override).")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--synthetic", action="store_true", help="use fixtures/eval_synthetic")
    ap.add_argument("--posts", type=Path, help="JSONL of SourcePost dicts or G0 records")
    ap.add_argument("--adapter", help="module:callable returning historical_inputs(start, end)")
    ap.add_argument("--start", help="ISO-8601 UTC start for --adapter")
    ap.add_argument("--end", help="ISO-8601 UTC end for --adapter")
    ap.add_argument("--labels", type=Path, help="JSONL of LabeledPost")
    ap.add_argument("--expected-author", help="account id used to verify authorship of G0 records")
    ap.add_argument("--golden", type=Path, help="directory of golden replay JSON files")
    ap.add_argument("--out", type=Path, default=Path("data/eval/results"), help="report directory (git-ignored)")
    ap.add_argument("--dataset-name", default=None)
    ap.add_argument("--allow-holdout", action="store_true")
    args = ap.parse_args(argv)

    if args.synthetic:
        args.posts = args.posts or SYNTHETIC / "posts.jsonl"
        args.labels = args.labels or SYNTHETIC / "labels.jsonl"
        args.golden = args.golden or SYNTHETIC
        args.dataset_name = args.dataset_name or "synthetic fixtures"
    _guard_holdout([args.posts, args.labels, args.golden], args.allow_holdout)

    if args.adapter:
        module, func = args.adapter.split(":")
        historical_inputs = getattr(importlib.import_module(module), func)
        start = datetime.fromisoformat(args.start.replace("Z", "+00:00")) if args.start else datetime(2000, 1, 1, tzinfo=timezone.utc)
        end = datetime.fromisoformat(args.end.replace("Z", "+00:00")) if args.end else datetime.now(timezone.utc)
        posts = coerce_source_posts(historical_inputs(start, end), expected_author_id=args.expected_author)
    elif args.posts:
        posts = load_posts(args.posts, expected_author_id=args.expected_author)
    else:
        sys.exit("Give --synthetic, --posts or --adapter")
    if not args.labels:
        sys.exit("--labels is required")
    labels = load_labels(args.labels)

    report = evaluate(posts, labels, dataset=args.dataset_name or args.posts.name if args.posts else "adapter")
    goldens: list[GoldenResult] = []
    if args.golden:
        by_id = {p.source_id: p for p in posts}
        args.out.mkdir(parents=True, exist_ok=True)
        for spec_path in sorted(args.golden.glob("golden_*.json")):
            spec = load_golden(spec_path)
            goldens.append(run_golden(spec, by_id, args.out / f"{spec_path.stem}.sqlite3"))
    report = report.model_copy(update={"golden": goldens})

    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "report.json").write_text(json.dumps(json.loads(report.model_dump_json()), indent=1) + "\n")
    (args.out / "report.md").write_text(report.to_markdown())
    print(report.to_markdown())
    print(f"Written to {args.out}/report.json and report.md")
    return 0 if all(g.passed for g in goldens) else 1


if __name__ == "__main__":
    sys.exit(main())
