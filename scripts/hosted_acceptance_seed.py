"""The browser acceptance of the hosted site (DECIDED 40 "Acceptance"; DESIGN_UX section 7) on
INVENTED data: one sequence of several posts in which one post has TWO forms, one post has a
chart, every form's system reading proposes BOTH a price series and an instrument, and one
reading of the tester has been RETURNED by the approver. Never deployed; never for a database
that holds anything but invented bundles.

    PYTHONPATH=src python scripts/hosted_acceptance_seed.py write DIR
    PYTHONPATH=src python scripts/hosted_acceptance_seed.py seed --alias ACCEPT-1 [--login ADDRESS]

``write DIR`` writes the acceptance folder (the demo bundle with its crude-oil story gathered into
ONE sequence, ``scs01``, first in review order: posts s01, s05, s14 (two forms), s18; the
invented chart on s01). ``seed`` publishes that folder as the active DEMONSTRATION bundle of the
database ``HOSTED_DATABASE_URL`` (refused, with nothing changed, when that database holds a bundle
that is not demonstration data: NOT_A_DEMO_DATABASE), writes the invented reviewer ALIAS (a
reviewer row, not a test identity: a return reaches a reviewer only), records an empty local log
as imported when no log is, and stores for that reviewer: the independent reading of s01's
form s01a, the reveal, the reading after it (both mapping questions answered), and the
approver's RETURN of that reading with an invented note, in the shape ``push-returns`` stores. The tester then works
through the rest with the interface alone. The development server does the same with
``scripts/hosted_dev_server.py --acceptance ACCEPT-1``. Prints ids and counts only.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "hosted"))  # the deployed layout: ``reviewer_web`` is top-level

SEQUENCE = "scs01"  # the acceptance sequence: the crude story of the demo's scenarios
POSTS = ("s01", "s05", "s14", "s18")  # in time order; s14 has two forms; s01 has the chart
CHART = "chart-s01.png"
RETURNED = "s01a"  # the form of s01: the tester's reading that the approver returned
NOTE = "Please look again at the stop: is 69.50 the stop of this trade?"
LOGIN = "acceptance.tester@example.invalid"


def _module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write(directory: Path) -> Path:
    """The acceptance folder: the invented demo folder, its crude story in one sequence."""
    from tradingbot.runtime import adjudication as A

    demo = _module("adjudication_demo_bundle_acceptance",
                   REPO / "scripts" / "adjudication_demo_bundle.py")
    base = demo.write_demo(directory)
    reviewers = base / "reviewers.toml"
    reviewers.write_text(reviewers.read_text().replace(
        "remote_full_context_authorized = false", "remote_full_context_authorized = true"))
    root = base / "bundle"

    def rows(name):
        return [json.loads(line) for line in (root / name).read_text().splitlines()]

    def put(name, data):
        (root / name).write_text("".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n"
                                         for r in data))

    items, posts, sequences = rows("items.jsonl"), rows("posts.jsonl"), rows("sequences.jsonl")
    for item in items:
        if item["post_ref"] in POSTS:
            item["sequence_id"] = SEQUENCE
            # Every earlier post of the sequence is shown with a form (post 3 closes the long
            # that post 2 added to).
            item["context_refs"] = list(POSTS[:POSTS.index(item["post_ref"])])
    moved = {f"sc{p}" for p in POSTS} - {SEQUENCE}
    order = 1
    kept = []
    for seq in sorted(sequences, key=lambda s: s["review_order"]):
        if seq["sequence_id"] in moved:
            continue
        if seq["sequence_id"] == SEQUENCE:
            seq["post_refs"] = list(POSTS)
        kept.append(seq)
    first = next(s for s in kept if s["sequence_id"] == SEQUENCE)
    kept = [first] + [s for s in kept if s is not first]
    for order, seq in enumerate(kept, 1):
        seq["review_order"] = order
    for item in items:  # an idea that named a merged sequence names the one it joined
        ai = item.get("ai") or {}
        if ai.get("idea_ref") in moved:
            ai["idea_ref"] = SEQUENCE
    for post in posts:
        if post["post_ref"] == "s01":
            post["media"] = [CHART]
    (root / "media" / CHART).write_bytes((root / "media" / "chart-p01.png").read_bytes())
    os.chmod(root / "media" / CHART, 0o600)
    put("items.jsonl", items), put("posts.jsonl", posts), put("sequences.jsonl", kept)
    manifest = json.loads((root / "manifest.json").read_text())
    manifest["bundle_id"] = A.bundle_digest(root)
    (root / "manifest.json").write_text(json.dumps(manifest, indent=1) + "\n")
    A.load_bundle(root, A.FULL)  # the loader's own checks
    return base


def _reading(bundle, item_id: str, *, after: bool):
    """(reading, verdicts, form) of a scenario item: its expected answers; after the reveal
    with both mapping questions answered YES and every verdict pre-filled from the comparison."""
    from tradingbot.runtime import adjudication as A
    from tradingbot.runtime import adjudication_rubric as R
    from tradingbot.runtime import adjudication_scenarios as SC

    form = next(sc["forms"][k] for sc in SC.SCENARIOS for k in range(len(sc["forms"]))
                if SC.item_id(sc, k) == item_id)
    item, reading = bundle.items[item_id], dict(form["answers"])
    if form["kind"] == "INSTRUCTION":
        reading[A.LEVELS] = [{"row": key, **row} for (key, _), row in zip(
            A.table_rows(bundle, item), form["rows"], strict=False)]
    if not after:
        return reading, None, form
    ctx = A.form_context(bundle, item, revealed=True)
    state = R.applicability(R.complete(A.clean_reading(reading, item, bundle), ctx), ctx)
    reading.update({f: "YES" for f in R.MAPPING_FIELDS if state[f].state == R.ASK})
    done = R.complete(A.clean_reading(reading, item, bundle), ctx)
    verdicts = A.prefill_verdicts(done, A.system_reading(bundle, item), item["kind"],
                                  A.compared_fields(bundle, item, done))
    if done.get(A.LEVELS):
        verdicts[A.LEVELS] = A.prefill_row_verdicts(done[A.LEVELS], A.system_rows(bundle, item))
    return reading, verdicts, form


def _save(store, reviewer, item_id: str, *, after: bool) -> dict:
    from tradingbot.runtime import adjudication as A

    bundle, session = store.bundle_for(reviewer), reviewer.session()
    reading, verdicts, form = _reading(bundle, item_id, after=after)

    def make(records):
        item = bundle.items[item_id]
        _done, checks, _ctx = A.check_reading(
            bundle, item, A.clean_reading(reading, item, bundle), revealed=after,
            records=records, login=session.login, flags=form["flags"],
            confidence=form["confidence"], notes=form["notes"])
        ind = A.latest_own_reading([r for r in records if r["phase"] == A.INDEPENDENT],
                                   item_id, bundle, session.login)
        return A.build_annotation(
            records, bundle, session, item_id=item_id,
            phase=A.AFTER_REVEAL if after else A.INDEPENDENT, blind=True, reading=reading,
            field_verdicts=verdicts, flags=form["flags"], confidence=form["confidence"],
            notes=form["notes"], refers_to=ind["seq"] if after else None,
            acknowledged_warnings=checks.warnings)

    return store.append(reviewer, make).records[-1]


def login_of(alias: str) -> str:
    """The invented sign-in of a tester: ACCEPT-1's is LOGIN, ACCEPT-n's carries n."""
    n = alias.rsplit("-", 1)[-1]
    return LOGIN if n == "1" else LOGIN.replace("@", f"-{n}@")


def seed(admin, base: Path, alias: str, login: str | None = None) -> dict:
    """Publish the acceptance folder as the active demonstration bundle and put ``alias`` into
    the acceptance state. Refused on a database that holds a bundle that is not invented."""
    from reviewer_web.store import EventLine, sha256

    from tradingbot.runtime import adjudication as A
    from tradingbot.runtime.vocab_recommend import GENESIS, encode_line

    if not re.fullmatch(r"ACCEPT-[0-9]{1,3}", alias):
        raise A.AdjudicationError("SEED_ALIAS_IS_ACCEPT_N")
    with admin.connection() as conn:
        # Only on a database initialised as a preview (init-db --preview), of invented bundles
        # only, before any real import, holding no reviewer row but the seed's own.
        if not admin.is_preview():
            raise A.AdjudicationError("NOT_A_PREVIEW_DATABASE")
        if conn.execute("SELECT 1 FROM bundle WHERE NOT is_demo LIMIT 1").fetchone():
            raise A.AdjudicationError("NOT_A_DEMO_DATABASE")
        if admin.anchor() is not None and admin.anchor_writer() != "SEED":
            raise A.AdjudicationError("ANCHOR_EXISTS")
        if [r.alias for r in admin.reviewers() if not re.fullmatch(r"ACCEPT-[0-9]{1,3}", r.alias)]:
            raise A.AdjudicationError("OTHER_REVIEWER_ROWS")
    root = base / "bundle"
    files = [(root / n).read_bytes()
             for n in ("manifest.json", "sequences.jsonl", "items.jsonl", "posts.jsonl")]
    media = {p.name: p.read_bytes() for p in sorted((root / "media").iterdir())}
    with admin.connection():
        bundle = admin.publish_bundle(*files, media, is_demo=True,
                                      source_sequences=len(files[1].splitlines()))
        row = admin.upsert_reviewer(alias, login or login_of(alias), name="Acceptance Tester", scope_sequences=40,
                                    bundle_id=bundle.bundle_id)
        if any(e["alias"] == alias for e in admin.events()):
            raise A.AdjudicationError("ALREADY_SEEDED")
        if admin.anchor() is None:  # an empty local log, imported: reviewers may save
            admin.set_anchor(0, GENESIS, sha256(b""), writer="SEED")
        admin.stamp_imported([row.id])
    _save(admin, row, RETURNED, after=False)
    admin.append(row, lambda records: A.build_system_shown(records, bundle, row.session(),
                                                           RETURNED))
    reading = _save(admin, row, RETURNED, after=True)
    tool = _module("hosted_admin_acceptance", REPO / "scripts" / "hosted_admin.py")
    reviewed = next(e for e in admin.events() if e["seq"] == reading["seq"])
    # Each tester's return has its own place in the RETURN stream (as the approver's own line
    # of an invented local log would): after every return already stored.
    held = [json.loads(e["line"])["local"]["seq"] for e in admin.events()
            if e["origin"] == "RETURN"]
    approval = {"seq": max(held, default=0) + 1, "at": reading["at"],
                "bundle_id": bundle.bundle_id, "item_id": RETURNED, "notes": NOTE}
    record = tool.return_record(approval, "0" * 64, reviewed)
    admin.insert_events([EventLine(row.id, "RETURN", encode_line(record))])
    return {"alias": alias, "bundle": bundle.bundle_id, "sequence": SEQUENCE,
            "forms": [i for i, x in bundle.items.items() if x["sequence_id"] == SEQUENCE],
            "returned": RETURNED}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="command", required=True)
    sub.add_parser("write").add_argument("dir")
    p = sub.add_parser("seed")
    p.add_argument("--alias", required=True)
    p.add_argument("--login", help="default: acceptance.tester@example.invalid for ACCEPT-1, "
                                   "acceptance.tester-<n>@example.invalid for ACCEPT-n")
    args = ap.parse_args(argv)
    if args.command == "write":
        base = write(Path(args.dir).expanduser())
        print(f"acceptance folder written: sequence {SEQUENCE}, posts {len(POSTS)}")
        return 0 if base else 1
    url = os.environ.get("HOSTED_DATABASE_URL")
    if not url:
        print("refused: SET_HOSTED_DATABASE_URL", file=sys.stderr)
        return 2
    from reviewer_web.store import AdminStore

    from tradingbot.runtime import adjudication as A

    work = Path(tempfile.mkdtemp(prefix="hosted-acceptance-"))
    try:
        found = seed(AdminStore(url), write(work / "acceptance"), args.alias, args.login)
    except A.AdjudicationError as exc:
        print(f"refused: {exc.code}", file=sys.stderr)
        return 2
    print(f"{found['alias']}: bundle {found['bundle'][:12]} (demonstration data); sequence "
          f"{found['sequence']}: forms {', '.join(found['forms'])}; returned: {found['returned']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
