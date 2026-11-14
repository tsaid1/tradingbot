"""Owner, locally: freeze the adjudication ground truth, or verify the annotation log and versions.

    python scripts/adjudication_freeze.py freeze [--dir DIR]
    python scripts/adjudication_freeze.py verify [--dir DIR]

DIR defaults to ``TRADINGBOT_ADJUDICATION_DIR``. ``freeze`` refuses when the annotation log fails
verification, then writes the next ``versions/ground_truth_v<N>.jsonl`` (never overwriting) and
``ground_truth_v<N>.manifest.json`` with its sha256: per item the approver's accepted or amended
reading, else the latest AFTER_REVEAL reading marked PROVISIONAL, with the INDEPENDENT reading
beside it. ``verify`` checks the log's chain and every version against its manifest. Both print
counts, codes and file names only. Exit 0 when all is well, 1 when a check fails, 2 when refused.
"""
from __future__ import annotations

import argparse
import os
import sys

from tradingbot.runtime import adjudication as A
from tradingbot.runtime.vocab_review import PrivatePathError


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("command", choices=("freeze", "verify"))
    ap.add_argument("--dir", default=os.environ.get(A.DIR_ENV))
    args = ap.parse_args(argv)
    if not args.dir:
        print(f"refused: give --dir or set {A.DIR_ENV}", file=sys.stderr)
        return 2
    try:
        if args.command == "freeze":
            path, m = A.freeze(args.dir)
            print(f"frozen {path.name}: {m['items']} item(s) {m['by_status']}, sha256 "
                  f"{m['sha256']}, from {m['annotation_log']['count']} annotation line(s)")
            return 0
        log = A.read_annotations(args.dir)
        print(f"annotation log: {'OK' if log.ok else 'FAILS ' + str(log.problem)}, "
              f"{log.count} line(s)")
        versions = A.verify_versions(args.dir)
        for name, ok in versions:
            print(f"{name}: {'OK' if ok else 'HASH MISMATCH'}")
        return 0 if log.ok and all(ok for _, ok in versions) else 1
    except A.AdjudicationError as exc:
        print(f"refused: {exc.code}", file=sys.stderr)
        return 2
    except (OSError, PrivatePathError) as exc:
        print(f"refused: {type(exc).__name__}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
