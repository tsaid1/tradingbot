"""Offline IBKR read-only readiness scaffold CLI (issue #16). NON-AUTHORITATIVE; synthetic scenarios only.

List the bundled fake-clock scenarios, run one, or run them all:
    python scripts/inspect_paper_readiness.py --list
    python scripts/inspect_paper_readiness.py --scenario tlt_regular_session_control [--json]
    python scripts/inspect_paper_readiness.py --all [--json | --summary] [--out report.txt]

Every scenario replays a scripted FAKE transport under a FAKE clock: no network, broker, SDK, credential,
live endpoint or market-data access exists in this path. The report lists refused, missing, ambiguous,
mismatched, stale, delayed, unentitled, incomplete and erroneous observations; it never grants trading
permission, selects a contract, sizes or changes policy. Exit status 2 means an unknown scenario name;
the exit status is never a verdict.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import tradingbot
from tradingbot.paper_readiness.runner import render_json, render_text
from tradingbot.paper_readiness.scenarios import SCENARIOS, run_scenario


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--list", action="store_true", help="list the bundled synthetic scenarios")
    group.add_argument("--scenario", metavar="NAME", help="run one bundled scenario")
    group.add_argument("--all", action="store_true", help="run every bundled scenario")
    parser.add_argument("--json", action="store_true", help="machine-readable JSON instead of text")
    parser.add_argument("--summary", action="store_true", help="one text line per scenario (ignored with --json)")
    parser.add_argument("--out", type=Path, help="write the output to this file instead of stdout")
    args = parser.parse_args(argv)
    print(f"tradingbot import origin: {Path(tradingbot.__file__).resolve().parent}", file=sys.stderr)

    if args.list:
        lines = [f"{name:<34} expected={s.expected_outcome.value:<24} {s.description}"
                 for name, s in SCENARIOS.items()]
        text = "\n".join(lines) + "\n"
    else:
        if args.all:
            selected = list(SCENARIOS.values())
        elif args.scenario in SCENARIOS:
            selected = [SCENARIOS[args.scenario]]
        else:
            print(f"error: unknown scenario '{args.scenario}'; use --list", file=sys.stderr)
            return 2
        reports = [(s, run_scenario(s)) for s in selected]
        if args.json:
            payloads = [json.loads(render_json(r)) for _, r in reports]
            text = json.dumps(payloads[0] if args.scenario else payloads, indent=2, sort_keys=True) + "\n"
        else:
            parts = []
            for s, r in reports:
                verdict = "as scripted" if r.outcome == s.expected_outcome else "UNEXPECTED"
                parts.append(render_text(r, summary=args.summary)
                             + ("" if args.summary else
                                f"scenario expectation: {s.expected_outcome.value} -> actual {r.outcome.value} "
                                f"({verdict})\n\n"))
            text = "".join(parts)
    if args.out is not None:
        args.out.write_text(text, encoding="utf-8")
        print(f"wrote {args.out} (NON-AUTHORITATIVE synthetic readiness diagnostic)", file=sys.stderr)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
