"""An INVENTED adjudication folder shaped like the real one at the cutover, for the operator's
rehearsal of the whole cutover on a preview database (docs/HOSTED_REVIEWER.md, "Rehearsal").

    PYTHONPATH=src python scripts/hosted_rehearsal_folder.py DIR

DIR (new, empty, outside the repository) receives the demo bundle (it stays demonstration data:
a preview serves nothing else), a reviewers file with the local approver, two remote reviewers
and the owner's local reviewer identity (FULL, remote full context authorised, scope 40), and a
local log in which:

* the first remote reviewer (REMOTE-1) has finished three forms and is half-way through a
  fourth (independent reading saved, the reveal recorded, no reading after it);
* the owner's reviewer identity (OWNER-REVIEWER) has finished two forms and has saved form 1 of
  a two-form post (the reveal waits for form 2);
* the approver has accepted one reading of REMOTE-1 and returned another, with a note;
* the second remote reviewer (REMOTE-2) has nothing.

It prints, per identity, what ``parity`` must then say after ``push-returns``:
``<ALIAS>: completed n of m; next <form> <phase>``. Every word, address and price is invented.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "hosted"))

REVIEWERS = """remote_full_context_authorized = true
scope_sequences = 40

[[reviewer]]
login = "Approver.Local"
name = "Approver Example"
role = "APPROVER"
access = "FULL"

[[reviewer]]
login = "reviewer@example.invalid"
name = "Reviewer Example"
role = "REVIEWER"
access = "FULL"

[[reviewer]]
login = "remote@example.invalid"
name = "Remote Reviewer Example"
role = "REVIEWER"
access = "FULL"

[[reviewer]]
login = "Owner.Reviewer"
name = "Owner Reviewer Example"
role = "REVIEWER"
access = "FULL"
"""
NOTE = "Please look at the stop again."


def _module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write(directory: Path) -> dict[str, str]:
    """Write the folder; return {alias: the line parity must print for it}."""
    from reviewer_web import forms as F

    from tradingbot.runtime import adjudication as A

    demo = _module("adjudication_demo_bundle_rehearsal",
                   REPO / "scripts" / "adjudication_demo_bundle.py")
    seeding = _module("hosted_acceptance_seed_rehearsal",
                      REPO / "scripts" / "hosted_acceptance_seed.py")
    base = demo.write_demo(directory)
    (base / "reviewers.toml").write_text(REVIEWERS)  # keeps the file's mode, 0600
    reviewers = A.load_reviewers(base / "reviewers.toml")
    bundle = A.load_bundle(base / "bundle", A.FULL)

    def session(login: str) -> A.Session:
        e = reviewers.entries[login.casefold()]
        return A.Session(e.login, e.name, e.role, e.access, False, reviewers.scope_sequences)

    one, owner, approver = (session("reviewer@example.invalid"), session("Owner.Reviewer"),
                            session("Approver.Local"))

    def save(who: A.Session, item_id: str, *, after: bool) -> dict:
        records = A.read_annotations(base).records
        reading, verdicts, form = seeding._reading(bundle, item_id, after=after)
        item = bundle.items[item_id]
        _done, checks, _ctx = A.check_reading(
            bundle, item, A.clean_reading(reading, item, bundle), revealed=after,
            records=records, login=who.login, flags=form["flags"],
            confidence=form["confidence"], notes=form["notes"])
        ind = A.latest_own_reading([r for r in records if r["phase"] == A.INDEPENDENT],
                                   item_id, bundle, who.login)
        return A.append_annotation(
            base, bundle, who, item_id=item_id,
            phase=A.AFTER_REVEAL if after else A.INDEPENDENT, blind=True, reading=reading,
            field_verdicts=verdicts, flags=form["flags"], confidence=form["confidence"],
            notes=form["notes"], refers_to=ind["seq"] if after else None,
            acknowledged_warnings=checks.warnings)

    def finish(who: A.Session, item_id: str) -> dict:
        save(who, item_id, after=False)
        A.mark_system_shown(base, bundle, who, item_id)
        return save(who, item_id, after=True)

    accepted = finish(one, "s01a")
    returned = finish(one, "s02a")
    finish(one, "s03a")
    save(one, "s04a", after=False)
    A.mark_system_shown(base, bundle, one, "s04a")  # half-way: no reading after the reveal
    finish(owner, "s05a")
    finish(owner, "s06a")
    save(owner, "s14a", after=False)  # form 1 of 2: the reveal waits for s14b
    for reading, outcome in ((accepted, "ACCEPT"), (returned, "RETURN")):
        A.append_annotation(base, bundle, approver, item_id=reading["item_id"],
                            phase=A.APPROVAL, blind=False, reading={},
                            refers_to=reading["seq"], outcome=outcome,
                            notes=NOTE if outcome == "RETURN" else "")
    records = A.read_annotations(base).records
    expected = {}
    for alias, login in (("REMOTE-1", "reviewer@example.invalid"),
                         ("REMOTE-2", "remote@example.invalid"),
                         ("OWNER-REVIEWER", "Owner.Reviewer")):
        who = session(login)
        own = [r for r in records if (r.get("annotator") or {}).get("login") == who.login]
        home = own + [r for r in records if r.get("phase") == A.APPROVAL]
        done = A.progress(bundle, own, who)
        step = F.next_step(bundle, who, home)
        state = F.step_state(bundle, who, home, step) if step else "nothing due"
        expected[alias] = f"{alias}: completed {done.done} of {done.total}; next {step} {state}"
    return expected


def main(argv=None) -> int:
    args = (argv if argv is not None else sys.argv[1:])
    if len(args) != 1:
        print("usage: hosted_rehearsal_folder.py DIR", file=sys.stderr)
        return 2
    for line in write(Path(args[0]).expanduser()).values():
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
