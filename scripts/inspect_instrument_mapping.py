"""Offline instrument-mapping diagnostic CLI. NON-AUTHORITATIVE; synthetic records only.

Run against the bundled synthetic examples:
    python scripts/inspect_instrument_mapping.py --examples
    python scripts/inspect_instrument_mapping.py --examples --json

Run against your own synthetic record file (a JSON list, or an object with a "records" list; every
record must carry "synthetic": true):
    python scripts/inspect_instrument_mapping.py path/to/records.json [--json] [--out report.txt]

No network, broker, catalog or credential access. The report lists missing, ambiguous, mismatched,
invalid and unsupported mapping metadata; it never converts prices, picks contracts, sizes or
authorizes anything. Exit status 2 means the input could not be read; it is never a verdict.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from tradingbot.instrument_diagnostics import (
    diagnose_records,
    load_records,
    render_json,
    render_text,
)

REPO = Path(__file__).resolve().parents[1]
EXAMPLES = REPO / "fixtures" / "instrument_mapping_synthetic" / "records.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("records", nargs="?", type=Path, help="JSON file of synthetic mapping records")
    parser.add_argument("--examples", action="store_true", help=f"use the bundled synthetic examples ({EXAMPLES})")
    parser.add_argument("--json", action="store_true", help="machine-readable JSON instead of text")
    parser.add_argument("--summary", action="store_true", help="one text line per record (ignored with --json)")
    parser.add_argument("--out", type=Path, help="write the report to this file instead of stdout")
    args = parser.parse_args(argv)
    if args.records is None and not args.examples:
        parser.error("give a records file or --examples")
    if args.records is not None and args.examples:
        parser.error("give either a records file or --examples, not both")
    path = EXAMPLES if args.examples else args.records
    try:
        records = load_records(path)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    report = diagnose_records(records)
    text = render_json(report) if args.json else render_text(report, summary=args.summary)
    if args.out is not None:
        args.out.write_text(text, encoding="utf-8")
        print(f"wrote {args.out} ({report.record_count} records; NON-AUTHORITATIVE diagnostic)", file=sys.stderr)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
