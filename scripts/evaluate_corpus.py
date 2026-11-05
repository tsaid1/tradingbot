"""Evaluate the TKL interpreter against a labeled dataset and run golden replays. Mock-only.

Synthetic smoke run (no private data):
    python scripts/evaluate_corpus.py --synthetic --out data/eval/results_synthetic

Real development export from the Source Agent (private, git-ignored; never the holdout):
    python scripts/evaluate_corpus.py --export data/eval/dev_export.jsonl --manifest data/eval/dev_export.manifest.json \\
        --labels data/eval/dev_labels.jsonl --labels-sha256 <hex> --expected-author <account id> \\
        --golden data/eval/golden --out data/eval/results_dev
    # or straight from the Source Agent's reader (docs/research/source_corpus/source_reader.py):
    python scripts/evaluate_corpus.py --adapter docs.research.source_corpus.source_reader:historical_inputs \\
        --reader-arg path=private_samples/sprint12/development.jsonl --reader-arg expected_sha256=<export digest> \\
        --reader-arg expected_author_id=<account id> --reader-arg simulated_lag=12.4 \\
        --labels private_samples/sprint12/dev_labels.jsonl --labels-sha256 <hex> --expected-author <account id> \\
        --golden private_samples/sprint12/golden --out data/eval/results_dev --dataset-name dev-120

Integrity: an export needs a checksum (manifest `sha256` or --sha256) and the manifest split must be
"development"; the holdout is refused unless --allow-holdout (final evaluation only). Reports hold
aggregates, IDs and failure codes only; post text is never written or printed.
"""
from __future__ import annotations

import argparse
import importlib
import json
import sys
from pathlib import Path

from tradingbot.evaluation import (
    GoldenResult, evaluate, load_export, load_golden, load_labels, load_posts, parse_reader_args, posts_from_adapter,
    run_golden, verify_sha256,
)

REPO = Path(__file__).resolve().parents[1]
SYNTHETIC = REPO / "fixtures" / "eval_synthetic"


def _guard_holdout(paths: list[Path | None], allow: bool) -> None:
    for p in paths:
        if p is not None and "holdout" in str(p).lower() and not allow:
            sys.exit(f"Refusing {p}: the holdout is reserved for the final evaluation (pass --allow-holdout to override).")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--synthetic", action="store_true", help="use fixtures/eval_synthetic")
    ap.add_argument("--export", type=Path, help="Source Agent export: JSONL of SourcePost dicts or G0 records")
    ap.add_argument("--manifest", type=Path, help="export manifest JSON (split, post_count, sha256, labels_sha256)")
    ap.add_argument("--sha256", help="expected SHA-256 of the export file")
    ap.add_argument("--labels-sha256", help="expected SHA-256 of the labels file")
    ap.add_argument("--posts", type=Path, help="unverified JSONL (synthetic or local testing only)")
    ap.add_argument("--adapter", help="module:callable, the Source Agent's historical_inputs")
    ap.add_argument("--reader-arg", action="append", default=[], metavar="KEY=VALUE",
                    help="argument for the adapter, repeatable (ISO timestamps and integers are typed)")
    ap.add_argument("--start", help="shorthand for --reader-arg start=...")
    ap.add_argument("--end", help="shorthand for --reader-arg end=...")
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
    _guard_holdout([args.posts, args.export, args.manifest, args.labels, args.golden], args.allow_holdout)

    info: dict = {}
    try:
        if args.adapter:
            module, func = args.adapter.split(":")
            historical_inputs = getattr(importlib.import_module(module), func)
            reader_args = parse_reader_args(args.reader_arg)
            for key in ("start", "end"):
                if getattr(args, key):
                    reader_args[key] = parse_reader_args([f"{key}={getattr(args, key)}"])[key]
            if str(reader_args.get("split", "")).lower() == "holdout" and not args.allow_holdout:
                sys.exit("Refusing split=holdout: the holdout is reserved for the final evaluation.")
            _guard_holdout([v for v in reader_args.values() if isinstance(v, Path)], args.allow_holdout)
            posts = posts_from_adapter(historical_inputs, reader_args, expected_author_id=args.expected_author)
            info = {"adapter": args.adapter, "reader_args": {k: str(v) for k, v in reader_args.items()}}
        elif args.export:
            posts, info = load_export(args.export, manifest=args.manifest, sha256=args.sha256,
                                      expected_author_id=args.expected_author, allow_holdout=args.allow_holdout)
        elif args.posts:
            posts = load_posts(args.posts, expected_author_id=args.expected_author)
            info = {"export": args.posts.name, "sha256": None, "split": "unverified"}
        else:
            sys.exit("Give --synthetic, --export, --posts or --adapter")
        if not args.labels:
            sys.exit("--labels is required")
        if args.labels_sha256 or (args.manifest and info.get("split") != "synthetic"):
            expected_labels = args.labels_sha256
            if expected_labels is None and args.manifest:
                from tradingbot.evaluation import ExportManifest
                expected_labels = ExportManifest.model_validate_json(args.manifest.read_text()).labels_sha256
            info["labels_sha256"] = verify_sha256(args.labels, expected_labels)
    except (ValueError, TypeError) as exc:
        sys.exit(f"Refusing to evaluate: {exc}")
    labels = load_labels(args.labels)

    name = args.dataset_name or info.get("split") or (args.posts.name if args.posts else "adapter")
    report = evaluate(posts, labels, dataset=name)
    goldens: list[GoldenResult] = []
    if args.golden:
        by_id = {p.post_id: p for p in posts}
        args.out.mkdir(parents=True, exist_ok=True)
        for spec_path in sorted(args.golden.glob("golden_*.json")):
            spec = load_golden(spec_path)
            goldens.append(run_golden(spec, by_id, args.out / f"{spec_path.stem}.sqlite3"))
    report = report.model_copy(update={"golden": goldens})

    args.out.mkdir(parents=True, exist_ok=True)
    payload = json.loads(report.model_dump_json())
    payload["input"] = info
    (args.out / "report.json").write_text(json.dumps(payload, indent=1) + "\n")
    (args.out / "report.md").write_text(report.to_markdown())
    print(report.to_markdown())
    print(f"Input: {json.dumps(info)}")
    print(f"Written to {args.out}/report.json and report.md")
    return 0 if all(g.passed for g in goldens) else 1


if __name__ == "__main__":
    sys.exit(main())
