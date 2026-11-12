"""Turn a contract-discovery report into an UNAPPROVED mapping proposal (offline, no broker).

    python scripts/tb_mapping_proposal.py REPORT.json --out DIR
        [--policy config/mapping_proposal.toml] [--family FAMILY:TREATMENT:EXEC_QUERY[:REF_QUERY]]
        [--roll-days N] [--price FAMILY=P]

REPORT.json is the ``contract_catalog_<UTC>.json`` that ``scripts/ibkr_contract_discovery.py``
writes. Writes to DIR: ``contract_table.md``, ``roll_calendar.md``, ``proposed_mapping.toml``
(every entry ``approved = false``; entries still needing an owner input or a broker fact do not
load) and one ``approval_<FAMILY>.md`` per family. Reads only the two input files: no network, no
``ibapi``. ``--roll-days`` is the owner's roll-rule parameter (business days before the report's
last-trade date); ``--price`` an owner-supplied illustrative price for the 1% risk line (never a
market price). Exit 0 on success, 2 on a bad input or a non-empty DIR, 3 when the
rendered proposal fails its own re-parse check (nothing is written). See docs/MAPPING_PROPOSAL.md.
"""
from __future__ import annotations

import argparse
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

from tradingbot.runtime.mapping_proposal import (
    ProposalInputInvalid,
    ProposalUnsafe,
    build,
    load_policy,
    load_report,
    merge_cli,
    write_outputs,
)

REPO = Path(__file__).resolve().parents[1]
DEFAULT_POLICY = REPO / "config" / "mapping_proposal.toml"


def _prices(items: list[str]) -> dict[str, Decimal]:
    out = {}
    for item in items:
        fam, sep, value = item.partition("=")
        try:
            price = Decimal(value)
        except InvalidOperation:
            price = None
        if not sep or not fam or price is None or not price.is_finite() or price <= 0:
            raise ProposalInputInvalid(f"--price {item!r}: expected FAMILY=positive number")
        out[fam] = price
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("report", help="contract_catalog_<UTC>.json from ibkr_contract_discovery.py")
    ap.add_argument("--out", required=True, help="output directory")
    ap.add_argument("--policy", default=str(DEFAULT_POLICY),
                    help="proposal policy (default config/mapping_proposal.toml)")
    ap.add_argument("--family", action="append", default=[],
                    help="FAMILY:TREATMENT:EXECUTION_QUERY[:REFERENCE_QUERY] (repeatable; "
                         "replaces the policy's vehicle and treatment for that family)")
    ap.add_argument("--roll-days", type=int, default=None,
                    help="roll_date = last-trade date minus N business days (owner's parameter)")
    ap.add_argument("--price", action="append", default=[],
                    help="FAMILY=P: owner-supplied illustrative price for the 1%% risk line")
    args = ap.parse_args(argv)
    try:
        if args.roll_days is not None and args.roll_days < 0:
            raise ProposalInputInvalid("--roll-days must be >= 0")
        cat = load_report(args.report)
        policy = merge_cli(load_policy(args.policy), args.family, args.roll_days,
                           _prices(args.price))
        props = build(cat, policy)
        paths = write_outputs(props, cat, policy, args.out)
    except ProposalInputInvalid as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    except ProposalUnsafe as exc:
        print(f"UNSAFE, nothing written: {exc}", file=sys.stderr)
        return 3
    for p in props:
        state = ("UNSUPPORTED: " + p.unsupported_reason if p.unsupported_reason else
                 f"{p.plan.treatment}, {len(p.owner_inputs)} owner inputs, "
                 f"{len(p.broker_missing)} missing broker facts")
        print(f"  {p.plan.family:<10} approved=false  {state}")
    print(f"wrote {len(paths)} files to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
