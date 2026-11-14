"""Write (or check) the two documents rendered from the rubric's code table.

    python scripts/adjudication_rubric_docs.py           # writes docs/ADJUDICATION_RUBRIC.md
                                                         #    and docs/REVIEWER_GUIDE.md
    python scripts/adjudication_rubric_docs.py --check   # exit 1 when a file has drifted

The ONE source is ``RUBRIC`` and ``REVIEWER_GUIDE`` in
``src/tradingbot/runtime/adjudication_rubric.py``; a test fails when the files differ from it.
"""
from __future__ import annotations

import sys
from pathlib import Path

from tradingbot.runtime import adjudication_rubric as R

DOCS = Path(__file__).resolve().parents[1] / "docs"
FILES = {"ADJUDICATION_RUBRIC.md": R.render_rubric_markdown,
         "REVIEWER_GUIDE.md": R.render_guide_markdown}


def main(argv: list[str] | None = None) -> int:
    check = "--check" in (sys.argv[1:] if argv is None else argv)
    drift = []
    for name, render in FILES.items():
        path, text = DOCS / name, render()
        if check:
            if not path.exists() or path.read_text() != text:
                drift.append(name)
        else:
            path.write_text(text)
            print(f"wrote docs/{name}")
    for name in drift:
        print(f"drifted: docs/{name} (run scripts/adjudication_rubric_docs.py)")
    return 1 if drift else 0


if __name__ == "__main__":
    sys.exit(main())
