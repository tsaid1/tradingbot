"""Owner, locally: freeze the adjudication ground truth, or verify the annotation log and versions.

    python scripts/adjudication_freeze.py freeze [--dir DIR]
    python scripts/adjudication_freeze.py verify [--dir DIR]
    python scripts/adjudication_freeze.py repair-head [--dir DIR | --recommendations] [--yes]

DIR defaults to ``TRADINGBOT_ADJUDICATION_DIR``. ``freeze`` refuses when the annotation log fails
verification, then writes the next ``versions/ground_truth_v<N>.jsonl`` (never overwriting) and
``ground_truth_v<N>.manifest.json`` with its sha256: per item the approver's accepted or amended
reading, else the latest AFTER_REVEAL reading marked PROVISIONAL, with the INDEPENDENT reading
beside it. ``verify`` checks the log's chain and every version against its manifest. Both print
counts, codes and file names only. ``repair-head`` repairs the head file of the annotation log
(or, with ``--recommendations``, of the recommendations log beside
``TRADINGBOT_VOCAB_REVIEWER_EXPORT``) in exactly two situations: a crash between a line and its
head (the head matches line n-1 and line n chains onto it), or no head file with a whole valid
chain. It prints the last line's sequence, phase and item id, never content, and writes only with
``--yes``; anything else is refused with a code and nothing is changed. Exit 0 when all is well,
1 when a check fails, 2 when refused.
"""
from __future__ import annotations

import argparse
import os
import sys

from tradingbot.runtime import adjudication as A
from tradingbot.runtime import vocab_recommend as R
from tradingbot.runtime.vocab_review import PrivatePathError


def _repair(log, schema: str, what: str, yes: bool) -> int:
    try:
        r = R.repair_head(log, schema, what, confirm=yes)
    except R.RecommendationError as exc:
        print(f"refused: {exc.code}; nothing changed", file=sys.stderr)
        return 2
    except (OSError, PrivatePathError) as exc:
        print(f"refused: {type(exc).__name__}; nothing changed", file=sys.stderr)
        return 2
    print(f"{r['situation']}: last line {r['seq']}, phase {r['phase']}, item {r['item']}: "
          + ("head written" if yes else "nothing written; run again with --yes to write the head"))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("command", choices=("freeze", "verify", "repair-head"))
    ap.add_argument("--dir", default=os.environ.get(A.DIR_ENV))
    ap.add_argument("--recommendations", action="store_true",
                    help="repair-head: the vocabulary recommendations log instead")
    ap.add_argument("--yes", action="store_true", help="repair-head: write the repaired head")
    args = ap.parse_args(argv)
    if args.command == "repair-head" and args.recommendations:
        export = os.environ.get(R.EXPORT_ENV)
        if not export:
            print(f"refused: set {R.EXPORT_ENV}", file=sys.stderr)
            return 2
        return _repair(R.recommendations_path(export), R.REC_SCHEMA, "recommendations log",
                       args.yes)
    if not args.dir:
        print(f"refused: give --dir or set {A.DIR_ENV}", file=sys.stderr)
        return 2
    try:
        if args.command == "repair-head":
            return _repair(A.annotations_path(args.dir), A.ANNOTATION_SCHEMA, "annotation log",
                           args.yes)
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
