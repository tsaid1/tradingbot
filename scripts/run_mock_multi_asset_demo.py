#!/usr/bin/env python3
"""Mock multi-asset demonstration: one scenario per price-basis treatment, through the real
service path, on invented posts, contracts and quotes (tradingbot.runtime.mock_multi_asset).

    PYTHONPATH=src python scripts/run_mock_multi_asset_demo.py [--state-dir DIR]

Scenarios: an index family with REFERENCE_TRIGGER exits on an invented future; an energy future in
a named month (DIRECT) plus a roll-window refusal; a metal quoted as spot and executed in a future
(BASIS_SHIFT) with its basis recorded, plus a HOLD while the basis exceeds its cap; a fully
specified single-leg option plus the same instruction without its strike (HOLD); and every family
again with approved = false (all HOLD, nothing at the broker). Each lifecycle shows an entry, a stop
amendment, a partial reduction and a close. Exits non-zero on any unexpected outcome, any
unexplained reconciliation discrepancy, a duplicate order or an account left open.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

from tradingbot.runtime.mock_multi_asset import run_all


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--state-dir", help="where to keep the demo state (default: a temp dir)")
    args = parser.parse_args(argv)
    root = Path(args.state_dir or tempfile.mkdtemp(prefix="tradingbot-multi-asset-"))
    if root.exists() and any(root.iterdir()):
        print(f"{root} is not empty; use an empty --state-dir", file=sys.stderr)
        return 2
    print(f"Mock multi-asset demo; state in {root}")
    results = run_all(root)
    for r in results:
        s = r.scenario
        print(f"\n== {s.name}: family {s.family}, {s.treatment}")
        for x in r.steps:
            mark = "PASS" if x.ok else "FAIL"
            reasons = ", ".join(x.reasons)
            print(f"  [{mark}] {x.action or '-':<7} {x.status or '-':<9} {x.step.text}")
            print(f"         {reasons}")
        for b in r.basis_records:
            print(f"  recorded {b}")
        print(
            f"  broker orders {r.broker_orders}, duplicate refs {r.duplicate_refs}, "
            f"reconciliation clean={r.reconciliation_clean}, unexplained={len(r.unexplained)}, "
            f"flat={r.flat}"
        )
        for u in r.unexplained:
            print(f"  UNEXPLAINED {u}")
        for n in r.notes:
            print(f"  FAIL {n}")
        for c in r.coverage:
            held = ", ".join(f"{k} x{n}" for k, n in c["held_by_reason"].items()) or "-"
            print(
                f"  coverage {c['family']}: {c['mapping_status']}, {c['execution']}, "
                f"{c['basis']}; signals {c['signals']}, executed {c['executed']}, "
                f"held {c['held']} ({held}), exceptions {c['exceptions']}"
            )
    failed = [r.scenario.name for r in results if not r.ok]
    print("\nSummary")
    for r in results:
        print(f"  [{'PASS' if r.ok else 'FAIL'}] {r.scenario.name}")
    print(f"  {len(results) - len(failed)}/{len(results)} scenarios passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
