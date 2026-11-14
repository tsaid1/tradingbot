"""Owner, locally: write the SANITIZED export the outside reviewer page reads.

    python scripts/vocab_reviewer_export.py [--out PATH] [--config RUNTIME.toml]

Takes the proposals the owner page would show (same runtime config, review log, lexicon and
proposals file) and writes ONE JSON file with only the allow-listed fields
(``vocab_recommend.EXPORT_FIELDS``: word, kind, counts, public codes, proposed group and family).
A proposal whose word is not a short plain word is withheld. ``--out`` defaults to
``TRADINGBOT_VOCAB_REVIEWER_EXPORT``; the path is refused inside this repository. Prints counts
and the path only, never a word. Exit 0 on success, 2 when refused. See docs/VOCAB_REVIEWER.md.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from tradingbot.runtime import vocab_recommend as R
from tradingbot.runtime import vocab_review as V
from tradingbot.runtime.config import DEFAULT_CONFIG_PATH, ConfigError, load_config


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default=os.environ.get(R.EXPORT_ENV))
    ap.add_argument("--config", default=os.environ.get("TRADINGBOT_RUNTIME_CONFIG",
                                                       str(DEFAULT_CONFIG_PATH)))
    args = ap.parse_args(argv)
    if not args.out:
        print(f"refused: give --out or set {R.EXPORT_ENV}", file=sys.stderr)
        return 2
    try:
        config = load_config(Path(args.config))
    except ConfigError as exc:
        print(f"refused: no usable runtime config ({type(exc).__name__})", file=sys.stderr)
        return 2
    lexicon, log = config.interpreter.lexicon_path, config.interpreter.review_log_path
    if lexicon is None or log is None:
        print("refused: the runtime config sets no review_log_path or lexicon_path",
              file=sys.stderr)
        return 2
    env = os.environ.get("TRADINGBOT_VOCAB_PROPOSALS")
    prop_path = Path(env).expanduser() if env else V.default_proposals_path(log)
    proposals, *_ = V.load_batch(log, lexicon, prop_path)
    try:
        n, withheld = R.write_export(args.out, proposals)
    except (OSError, V.PrivatePathError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    print(f"exported {n} proposal(s), withheld {withheld} (not a short plain word), to "
          f"{Path(args.out).expanduser()}; recommendations log: "
          f"{R.recommendations_path(args.out)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
