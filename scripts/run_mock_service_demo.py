#!/usr/bin/env python3
"""Mock demonstration of the runtime service on invented posts, with a kill and a restart.

    PYTHONPATH=src python scripts/run_mock_service_demo.py [--state-dir DIR]

The sequence (fixtures/runtime/mock_service_posts.jsonl, invented wording, ticker TLT and the S&P
500 series): an entry with stop, an add, commentary, a duplicate delivery of the add, an edited
revision of the entry, a stop amendment, a short in an index-quoted asset (unmapped: must HOLD), a
partial reduction and a close. Posts are released in compressed real time (60x) and stamped with
the wall clock, so the latency table shows measured stage times.

The service is killed in the middle of the add: the simulated broker accepts the order and the
process dies before the replay commits (SimulatedCrash, a BaseException that nothing catches). A
fresh service on the same files then recovers. A second, clean restart follows later. The demo
exits non-zero unless there are zero duplicate orders, zero unexplained reconciliation
discrepancies, the index-quoted post is held for UNSUPPORTED_INSTRUMENT_MAPPING and the follower
ends flat with no working order.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

from tradingbot.execution.sim_broker import SimulatedCrash
from tradingbot.runtime.app import build_service
from tradingbot.runtime.config import load_config
from tradingbot.runtime.lifecycle import latency_table, load
from tradingbot.runtime.mapping import UNSUPPORTED_INSTRUMENT_MAPPING

REPO = Path(__file__).resolve().parents[1]
FIXTURE = REPO / "fixtures" / "runtime" / "mock_service_posts.jsonl"

CONFIG = """
mode = "mock"
[paths]
state_dir = "state"
halt_file = "HALT"
mapping_file = "{mapping}"
[source]
kind = "replay"
file = "{fixture}"
compression = 60.0
[service]
cadence_seconds = 0.1
max_backoff_seconds = 2.0
[policy]
quantity = 2
quantity_per_add = 2
[mock]
quotes = {{ TLT = {{ bid = "88.40", ask = "88.42" }} }}
"""


def _fmt(v) -> str:
    return "-" if v is None else f"{v:.1f}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--state-dir", help="where to keep the demo state (default: a temp dir)")
    parser.add_argument("--timeout", type=float, default=60.0)
    args = parser.parse_args(argv)
    root = Path(args.state_dir or tempfile.mkdtemp(prefix="tradingbot-mock-demo-"))
    root.mkdir(parents=True, exist_ok=True)
    if (root / "state").exists():
        print(f"{root}/state already exists; use an empty --state-dir", file=sys.stderr)
        return 2
    cfg_path = root / "runtime.toml"
    cfg_path.write_text(
        CONFIG.format(mapping=REPO / "config" / "instrument_mapping.toml", fixture=FIXTURE)
    )
    config = load_config(cfg_path)
    total_lines = sum(1 for line in FIXTURE.read_text().splitlines() if line.strip())
    deadline = time.monotonic() + args.timeout
    print(f"Mock runtime demo; state in {root}")

    service = build_service(config)
    phase = "first run"
    armed = crashed = restarted_clean = False
    while time.monotonic() < deadline:
        try:
            service.run_once()
        except SimulatedCrash:
            crashed = True
            print("  KILLED: the broker accepted the add, then the process died before commit")
            del service
            service = build_service(config)  # a new process on the same files
            phase = "after kill"
            print("  RESTARTED: new service, same state files")
            continue
        n = len(service.store.signals())
        if not armed and n == 1:
            service.broker.script_next_submit("CRASH_AFTER_PLACE")
            armed = True
        if crashed and not restarted_clean and n >= 6:
            del service
            service = build_service(config)
            restarted_clean = True
            phase = "after clean restart"
            print("  RESTARTED (clean, between posts)")
        if int(service.source.cursor() or 0) >= total_lines:
            service.run_once()  # one more cycle: reconcile and heartbeat after the last post
            break
        time.sleep(0.05)
    else:
        print(f"timed out in phase '{phase}'", file=sys.stderr)
        return 1

    view = load(config)
    broker_orders = service.broker.all_orders()
    per_ref = Counter(o.order_ref for o in broker_orders)
    duplicates = {ref: n for ref, n in per_ref.items() if n > 1}
    rec = view["reconciliation"] or {}
    unexplained = [d for d in rec.get("discrepancies", []) if d.get("explained_by") is None]

    print("\nSignals (source -> interpretation -> mapping -> gate/execution)")
    print(
        f"  {'src':>5} {'rev':>3} {'dup':>3} {'action':>8} {'asset':>6} {'mapping':>13} "
        f"{'status':>9}  reasons"
    )
    for row in view["lifecycle"]:
        print(
            f"  {row['source_id']:>5} {row['revision']:>3} {'yes' if row['duplicate'] else '':>3} "
            f"{row['action'] or '-':>8} {row['asset'] or '-':>6} {row['mapping'] or '-':>13} "
            f"{row['status']:>9}  {', '.join(row['reasons'])}"
        )

    print("\nBroker orders by ref (each ref must appear once)")
    for o in broker_orders:
        print(
            f"  {o.order_ref:<28} {o.side.value:<4} {o.order_type.value:<3} qty {o.quantity:<2} "
            f"{o.status.value:<10} filled {o.filled_quantity}"
        )

    print("\nHeld signals and exceptions")
    for e in view["runtime_exceptions"]:
        print(
            f"  {e['kind']:<14} source {e['source_id']} rev {e['revision']} "
            f"{e['action_id']}: {', '.join(e['reasons'])}"
        )

    print("\nLatency (ms, measured from journal stamps)")
    cols = (
        "publish_to_receive",
        "receive_to_interpret",
        "interpret_to_gate",
        "gate_to_submit",
        "submit_to_ack",
        "ack_to_fill",
        "end_to_end",
    )
    print(f"  {'src':>5} {'action':>7} " + " ".join(f"{c:>20}" for c in cols))
    for r in latency_table(view["lifecycle"]):
        note = "  (order placed before the kill; adopted after restart)" if r["recovered"] else ""
        print(
            f"  {r['source_id']:>5} {r['action']:>7} "
            + " ".join(f"{_fmt(r[c]):>20}" for c in cols)
            + note
        )

    held_index = [
        r
        for r in view["lifecycle"]
        if r["asset"] == "SPX"
        and r["status"] == "HOLD"
        and UNSUPPORTED_INSTRUMENT_MAPPING in r["reasons"]
    ]
    flat = all(p["follower"] == 0 and p["broker"] == 0 for p in view["positions"])
    checks = {
        "service was killed mid-order and restarted": crashed,
        "zero duplicate orders at the broker": not duplicates,
        "zero unexplained reconciliation discrepancies": rec.get("clean") is True
        and not unexplained,
        "index-quoted post held (UNSUPPORTED_INSTRUMENT_MAPPING)": bool(held_index),
        "duplicate delivery recorded, no new order": any(r["duplicate"] for r in view["lifecycle"]),
        "edited revision held for review": any(
            r["revision"] > 1 and "SOURCE_EDIT_REQUIRES_REVIEW" in r["reasons"]
            for r in view["lifecycle"]
        ),
        "follower and broker flat, no working order": flat and not view["working_orders"],
    }
    print("\nSummary")
    print(
        f"  broker orders {len(broker_orders)}, distinct refs {len(per_ref)}, "
        f"duplicate refs {len(duplicates)}"
    )
    print(f"  reconciliation clean={rec.get('clean')} unexplained={len(unexplained)}")
    print(f"  held exceptions {len(view['runtime_exceptions'])}")
    for name, ok in checks.items():
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
