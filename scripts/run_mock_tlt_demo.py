"""Run the synthetic, mock-only TLT lifecycle demonstration from the command line.

    python scripts/run_mock_tlt_demo.py                                    # temporary mock ledger, deleted afterwards
    python scripts/run_mock_tlt_demo.py --db data/demo_tlt_lifecycle.sqlite3   # persistent ledger (git-ignored)
    python scripts/run_mock_tlt_demo.py --db data/demo_tlt_lifecycle.sqlite3 --fresh   # empty that ledger first
    python scripts/run_mock_tlt_demo.py --json                             # machine-readable audit

Every post, level and date comes from fixtures/demo_tlt_lifecycle.json and is invented. The run goes
through the repository's own interpreter, deterministic gateway and paper coordinator; nothing is
bypassed and nothing leaves the machine. Exit status 0 when every step matched its expected outcome,
the replayed post was rejected as a duplicate and the publisher's stated book agrees with the mock
ledger; 1 otherwise (including a ledger that already contains the demo: use --fresh).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from tradingbot.demo_lifecycle import (
    MOCK_BANNER,
    DemoScript,
    DemoSession,
    fresh_database,
    load_script,
)


def _interpretation(entry) -> str:
    parts = []
    for i in entry.interpretations:
        levels = " ".join(f"{name} {value}" for name, value in (
            ("@", i.entry_reference), ("stop", i.stop_price), ("target", i.target_price), ("fraction", i.close_fraction)) if value)
        ref = f" -> {i.idea_reference}" if i.idea_reference else ""
        parts.append(f"{i.message_class}/{i.tense} {i.asset or '?'} {i.side or ''} {levels}{ref}".strip())
    return "; ".join(parts) or "no clause"


def _ledger(rows) -> str:
    return ", ".join(f"{r.idea_id} {r.status} {r.quantity} units stop {r.stop_price or '-'} target {r.target_price or '-'}"
                     for r in rows) or "empty"


def render(session: DemoSession, passed: bool) -> str:
    policy = session.policy
    lines = [f"=== {MOCK_BANNER} ===", f"Script: {session.script.golden.name}",
             (f"Policy: {policy.quantity} units per open, {policy.quantity_per_add} per add, mock notional cap "
              f"{policy.max_notional_usd} USD, stop required on open: {policy.require_stop_on_open}"),
             ""]
    for e in session.entries:
        prov = ("verified" if e.provenance.verified_author else "UNVERIFIED") + f" author, rev {e.provenance.revision}, " + \
               ("complete" if e.provenance.complete_text else "truncated")
        lines.append(f"[{e.step}] {e.source_id} as of {e.processed_as_of:%Y-%m-%d %H:%MZ} ({prov})")
        lines.append(f"     text:           {e.text}")
        lines.append(f"     interpretation: {_interpretation(e)}")
        for o in e.outcomes:
            lines.append(f"     gateway:        {o.status} {', '.join(o.reasons)}")
            for text in o.explanations:
                lines.append(f"                     - {text}")
            if o.reconciliation_note:
                lines.append(f"     reconciliation: {o.reconciliation_note}")
        lines.append(f"     result:         {e.headline}")
        lines.append(f"     ledger after:   {_ledger(e.ledger_after)}")
        lines.append(f"     as expected:    {'yes' if e.matches_expectation else 'NO'}")
        lines.append("")
    lines.append("Reconciliation (publisher's stated book vs mock ledger):")
    for r in session.reconciliation():
        lines.append(f"  {r.idea_id}: publisher {r.publisher_state} stop {r.publisher_stop} target {r.publisher_target} | "
                     f"ledger {r.ledger_state} {r.ledger_quantity} units stop {r.ledger_stop} target {r.ledger_target} -> "
                     f"{'agrees' if r.agrees else 'DISAGREES (operator check required)'}")
    s = session.summary()
    lines.append(f"Summary: mock fills {s['mock_fills']}, protection amendments {s['protection_amendments']}, holds {s['holds']}, "
                 f"blocks {s['blocks']}, no-trades {s['no_trades']}, reconciles {s['reconciles']}, "
                 f"duplicates rejected {s['duplicates_rejected']}; expectations met: {'yes' if s['expectations_met'] else 'NO'}")
    lines.append(f"RESULT: {'PASS' if passed else 'FAIL'} (mock only; nothing was sent anywhere)")
    return "\n".join(lines)


def run(db_path: Path, script: DemoScript, *, as_json: bool) -> int:
    session = DemoSession(db_path, script)
    already = session.ledger()
    session.run_all()
    session.replay()   # idempotency check: the last processed post is submitted a second time
    summary = session.summary()
    passed = bool(summary["expectations_met"] and summary["ledger_agrees_with_publisher"]
                  and summary["duplicates_rejected"] == 1)
    if as_json:
        payload = {"banner": MOCK_BANNER, "script": script.golden.name, "passed": passed, "summary": summary,
                   "entries": [e.model_dump(mode="json") for e in session.entries],
                   "reconciliation": [r.model_dump(mode="json") for r in session.reconciliation()]}
        print(json.dumps(payload, indent=1))
    else:
        if already:
            print(f"NOTE: {db_path} already held {len(already)} mock idea(s); every post is treated as already processed. "
                  "Pass --fresh for a clean run.")
        print(render(session, passed))
    return 0 if passed else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", type=Path, help="persistent mock ledger (SQLite); default: a temporary file")
    ap.add_argument("--fresh", action="store_true", help="delete the --db ledger before running")
    ap.add_argument("--script", type=Path, help="alternative DemoScript JSON (synthetic only)")
    ap.add_argument("--json", action="store_true", help="print the audit as JSON")
    args = ap.parse_args(argv)
    script = load_script(args.script) if args.script else load_script()
    if args.db is not None:
        if args.fresh:
            fresh_database(args.db)
        return run(args.db, script, as_json=args.json)
    with TemporaryDirectory(prefix="tradingbot-mock-tlt-") as folder:
        return run(Path(folder) / "demo.sqlite3", script, as_json=args.json)


if __name__ == "__main__":
    sys.exit(main())
