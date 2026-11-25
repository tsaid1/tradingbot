"""Look at the hosted reviewer site on INVENTED data, on this machine. Never deployed.

    PYTHONPATH=src .venv/bin/python scripts/hosted_dev_server.py [--port 8600]
        [--dev-auto-sign-in REMOTE-2] [--dev-seed s14a --dev-seed s14b] [--acceptance ACCEPT-1]

Starts an embedded Postgres (``pgserver``) in a temporary folder, applies the schema, publishes
the invented demo bundle (``scripts/adjudication_demo_bundle.py``) flagged as demonstration data
with its two invented reviewers (aliases REMOTE-2 and REMOTE-3), and serves the application on
``127.0.0.1``. No mail is sent: the one-time code is printed in this terminal. Everything is
deleted when the process ends.

Sign in as ``reviewer@example.invalid`` or ``remote@example.invalid`` (Chrome and Firefox keep
the Secure session cookie on ``http://127.0.0.1``; Safari does not).

``--dev-auto-sign-in ALIAS`` exists ONLY here, for a headless browser taking screenshots: every
request but those to ``/sign-in`` is served as that reviewer, without a cookie. The application
has no such switch, and this script lives outside ``hosted/`` (the folder that is deployed). ``--dev-seed ITEM`` stores that reviewer's independent reading of a scenario
item of the demo (its expected answers) before serving, so the page after the reveal can be seen
at once: seed every form of a post (``s14a`` and ``s14b`` are the two forms of one post).
``--acceptance ALIAS`` serves the browser acceptance's invented data instead
(``scripts/hosted_acceptance_seed.py``): one sequence of four posts, a chart, a post with two
forms, and the reading of s01 of the invented reviewer ALIAS (sign-in
``acceptance.tester@example.invalid``) returned by the approver.
"""

from __future__ import annotations

import argparse
import atexit
import importlib.util
import shutil
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "hosted"))  # the deployed layout: ``reviewer_web`` is top-level
SECRET = "dev-server-secret-not-a-secret-0123456789abcdef"


def _demo_module():
    spec = importlib.util.spec_from_file_location(
        "adjudication_demo_bundle_dev", REPO / "scripts" / "adjudication_demo_bundle.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def publish(admin, base: Path):
    """The demo bundle and the demo file's two remote reviewers, into the database."""
    from tradingbot.runtime import adjudication as A

    root = base / "bundle"
    files = [(root / name).read_bytes()
             for name in ("manifest.json", "sequences.jsonl", "items.jsonl", "posts.jsonl")]
    media = {p.name: p.read_bytes() for p in sorted((root / "media").iterdir())}
    admin.publish_bundle(*files, media, is_demo=True, source_sequences=len(files[1].splitlines()))
    reviewers = A.load_reviewers(base / "reviewers.toml")
    reviewers.remote_full_context_authorized = True  # a hosted reviewer reads the posts
    return {r.alias: r for r in admin.sync_reviewers(reviewers)}


def seed(store, reviewer, item_id: str) -> None:
    """The independent reading of one scenario item: the scenario's expected answers."""
    from tradingbot.runtime import adjudication as A
    from tradingbot.runtime import adjudication_scenarios as SC

    bundle, session = store.bundle_for(reviewer), reviewer.session()
    form = next((sc["forms"][k] for sc in SC.SCENARIOS for k in range(len(sc["forms"]))
                 if SC.item_id(sc, k) == item_id), None)
    if form is None:
        raise SystemExit(f"--dev-seed: {item_id} is not a scenario item of the demo")
    item, reading = bundle.items[item_id], dict(form["answers"])
    if form["kind"] == "INSTRUCTION":
        reading[A.LEVELS] = [{"row": key, **row} for (key, _), row in zip(
            A.table_rows(bundle, item), form["rows"], strict=False)]

    def make(records):
        _done, checks, _ctx = A.check_reading(
            bundle, item, A.clean_reading(reading, item, bundle), revealed=False, records=records,
            login=session.login, flags=form["flags"], confidence=form["confidence"],
            notes=form["notes"])
        return A.build_annotation(
            records, bundle, session, item_id=item_id, phase=A.INDEPENDENT, blind=True,
            reading=reading, flags=form["flags"], confidence=form["confidence"],
            notes=form["notes"], acknowledged_warnings=checks.warnings)

    store.append(reviewer, make)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--port", type=int, default=8600)
    parser.add_argument("--dev-auto-sign-in", metavar="ALIAS")
    parser.add_argument("--dev-seed", metavar="ITEM", action="append", default=[])
    parser.add_argument("--acceptance", metavar="ALIAS")
    args = parser.parse_args()

    import pgserver
    from reviewer_web import app as web
    from reviewer_web import auth
    from reviewer_web.store import AdminStore, Store
    from werkzeug.serving import run_simple

    # Two temporary folders, both removed when the process ends (a short path for the server:
    # it listens on a socket there).
    server = pgserver.get_server(tempfile.mkdtemp(prefix="hosted-dev-pg-"), cleanup_mode="delete")
    work = Path(tempfile.mkdtemp(prefix="hosted-dev-"))
    atexit.register(shutil.rmtree, work, ignore_errors=True)
    dsn = server.get_uri()
    admin = AdminStore(dsn)
    admin.init_schema()
    if args.acceptance:
        spec = importlib.util.spec_from_file_location(
            "hosted_acceptance_seed_dev", REPO / "scripts" / "hosted_acceptance_seed.py")
        seeding = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(seeding)
        seeding.seed(admin, seeding.write(work / "acceptance"), args.acceptance)
        reviewers = {r.alias: r for r in admin.reviewers()}
    else:
        reviewers = publish(admin, _demo_module().write_demo(work / "demo"))
        # An empty local log, imported: the invented reviewers may save.
        from reviewer_web.store import sha256

        from tradingbot.runtime.vocab_recommend import GENESIS

        admin.set_anchor(0, GENESIS, sha256(b""))
    store = Store(dsn)

    def mailer(to: str, _subject: str, text: str) -> None:
        print(f"\n--- a message for {to} (not sent) ---\n{text}", flush=True)

    application = web.create_app(store, mailer, {
        "SESSION_SECRET": SECRET, "SITE_ENV": "test",
        "SIGN_IN_FLOOR_SECONDS": 0})
    served = application
    alias = args.dev_auto_sign_in
    if alias or args.dev_seed:
        reviewer = reviewers.get(alias or "REMOTE-2")
        if reviewer is None:
            raise SystemExit(f"--dev-auto-sign-in: one of {', '.join(sorted(reviewers))}")
        for item_id in args.dev_seed:
            seed(store, reviewer, item_id)
    if alias:
        now = datetime.now(UTC)
        asked = auth.request_code(store, SECRET.encode(), reviewer.login, "dev", now)
        token = auth.verify_code(store, SECRET.encode(), reviewer.login, asked.code, "dev",
                                 now).token

        def served(environ, start_response):
            if not environ.get("PATH_INFO", "").startswith("/sign-in"):
                environ["HTTP_COOKIE"] = f"{auth.COOKIE}={token}"
            return application(environ, start_response)

    print(f"Invented demonstration data only. Reviewers: "
          f"{', '.join(r.login_shown for r in reviewers.values())}", flush=True)
    print(f"http://127.0.0.1:{args.port}/" + (f"  (every request is {alias})" if alias else ""),
          flush=True)
    run_simple("127.0.0.1", args.port, served, threaded=True)


if __name__ == "__main__":
    sys.exit(main())
