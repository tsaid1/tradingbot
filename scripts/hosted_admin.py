"""Operator, locally: the hosted reviewer site's database (docs/HOSTED_REVIEWER.md, "Operations").

    python scripts/hosted_admin.py init-db
    python scripts/hosted_admin.py sync-reviewers [--demo | --test-identity ALIAS LOGIN]
    python scripts/hosted_admin.py revoke ALIAS
    python scripts/hosted_admin.py restore-access ALIAS
    python scripts/hosted_admin.py invite ALIAS --kind continue|start
    python scripts/hosted_admin.py publish-bundle [--check-only] [--demo DIR]
    python scripts/hosted_admin.py import-log [--snapshots DIR]
    python scripts/hosted_admin.py parity
    python scripts/hosted_admin.py export --out FILE
    python scripts/hosted_admin.py restore --in FILE
    python scripts/hosted_admin.py pull-to-local
    python scripts/hosted_admin.py push-returns
    python scripts/hosted_admin.py status
    python scripts/hosted_admin.py test-report ALIAS
    python scripts/hosted_admin.py verify [--snapshots DIR]

The database is ``HOSTED_DATABASE_URL`` (``init-db`` prefers ``HOSTED_DATABASE_URL_UNPOOLED``);
the adjudication folder is ``--dir`` or ``TRADINGBOT_ADJUDICATION_DIR``. Never deployed. It
prints counts, ids, aliases, times, sizes and fingerprints only: never a sign-in address, a name,
or a word of a post or a reading; every refusal is a constant code. The owner's reviewers file is
only read. Exit 0 when all is well, 1 when a check fails (parity, verify), 2 when refused.
"""
from __future__ import annotations

import argparse
import dataclasses
import importlib
import json
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

import psycopg

from tradingbot.runtime import adjudication as A
from tradingbot.runtime import adjudication_rubric as R
from tradingbot.runtime import vocab_recommend as V
from tradingbot.runtime.vocab_review import PrivateDir, PrivatePathError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hosted"))
S = importlib.import_module("reviewer_web.store")  # the deployed layout: a top-level package
HOSTED, LEGACY, RETURN, sha256 = S.HOSTED, S.LEGACY, S.RETURN, S.sha256
HOSTED_BASE, RETURN_BASE = S.HOSTED_BASE, S.RETURN_BASE

URL_ENV, UNPOOLED_ENV = "HOSTED_DATABASE_URL", "HOSTED_DATABASE_URL_UNPOOLED"
EXPORT_SCHEMA = "tradingbot.hosted.export/1"
SNAPSHOTS, LOG, WHAT = "hosted_snapshots", "annotations.jsonl", "annotation log"
BUNDLE_FILES = ("manifest.json", "sequences.jsonl", "items.jsonl", "posts.jsonl")
# The keys of an item line and of a post line that the workspace reads: no other key of a line is
# published (``parity_problems`` fails if one that matters is ever missing here).
ITEM_KEYS = ("item_id", "kind", "sequence_id", "set", "post_ref", "slot", "order_in_sequence",
             "context_refs", "ai", "holds", "mapping")
POST_KEYS = ("post_ref", "published_at", "text", "media", "revision", "numbers", "reply_to",
             "quotes")
TEST_ALIAS = re.compile(r"^TEST-[0-9]{1,3}$")
APPROVER_SHOWN = "approver"  # all a reviewer learns of who returned a reading
ENVELOPE = ("schema", "seq", "prev_sha256")
EVENT_KEYS = ("alias", "is_test", "origin", "seq", "prev_sha256", "sha256", "line",
              "idempotency_key", "batch", "created_at")


def refused(code: str) -> A.AdjudicationError:
    return A.AdjudicationError(code)


def _store(*, unpooled: bool = False):
    url = (os.environ.get(UNPOOLED_ENV) if unpooled else None) or os.environ.get(URL_ENV)
    if not url:
        raise refused(f"SET_{URL_ENV}")
    return S.AdminStore(url)


def _base(args) -> Path:
    if not args.dir:
        raise refused(f"SET_{A.DIR_ENV}")
    return Path(args.dir).expanduser()


def _time(t: datetime | None) -> str:
    return t.astimezone(UTC).isoformat(timespec="seconds") if t else "-"


def _counts(values) -> str:
    """``A 2, B 1`` of the values, in order of first appearance; ``-`` when there are none."""
    seen: dict[str, int] = {}
    for v in values:
        seen[v] = seen.get(v, 0) + 1
    return ", ".join(f"{k} {n}" for k, n in seen.items()) or "-"


# ------------------------------------------------------------------ the part of the bundle


def _json_lines(data: bytes) -> list[tuple[str, dict]]:
    """(line, row) of a bundle file, split exactly as the loader splits it."""
    return [(line, json.loads(line)) for line in data.decode("utf-8").splitlines()
            if line.strip()]


def _jsonl(lines) -> bytes:
    return "".join(f"{line}\n" for line in lines).encode("utf-8")


def _dump(row: dict) -> str:
    return json.dumps(row, ensure_ascii=False, sort_keys=True)


def subset(base: Path, scope: int) -> tuple[A.Bundle, list[bytes], dict[str, bytes]]:
    """(the verified local bundle, the four files of its published part, that part's images).

    The part holds what a reviewer of the first ``scope`` sequences is shown, and nothing that
    reviewer is not shown:

    * the items ``ordered(bundle, scope)`` offers (those of no sequence only when the scope
      covers every sequence);
    * the posts those items, their context and their reply and quote chains need, and only the
      images those posts list; of an item's and a post's line only the keys the workspace reads
      (``ITEM_KEYS``, ``POST_KEYS``);
    * EVERY sequence id with its place in the review order (the ``idea`` choices and the scope
      rule read them); a sequence outside the scope keeps no post;
    * an instruction item outside the scope that shares its post with an item inside it, as a
      stub (its place among the post's forms and its market; no context, no system reading): the
      form number of that post ("form k of m") counts it;
    * a system reading that refers (``ai.idea_ref``) to an item left out refers to that item's
      sequence instead, which reads as the same idea association.

    The manifest is the local one, so the ORIGINAL ``bundle_id`` stays (the records carry it).
    ``parity_problems`` proves that none of this changes what the reviewer sees."""
    root = base / "bundle"
    full = A.load_bundle(root, A.FULL)
    raw = [A._read(root, name) for name in BUNDLE_FILES]
    if A.bundle_from_bytes(*raw, access=A.FULL) != dataclasses.replace(full, root=None):
        raise refused("BUNDLE_CHANGED_WHILE_READ")
    shown = {i["item_id"] for _sid, items in A.ordered(full, scope) for i in items}
    forms = {full.items[i]["post_ref"] for i in shown if full.items[i]["kind"] == "INSTRUCTION"}
    numbered = {i for i, x in full.items.items() if i not in shown
                and x["kind"] == "INSTRUCTION" and x["post_ref"] in forms}
    needed: set[str] = set()
    todo = [r for i in shown for r in (full.items[i]["post_ref"], *full.items[i]["context_refs"])]
    while todo:
        ref = todo.pop()
        if ref not in needed:
            needed.add(ref)
            todo += [full.posts[ref][k] for k in ("reply_to", "quotes") if full.posts[ref][k]]
    in_scope = {s["sequence_id"] for s in full.sequences[:scope]}
    sequences = [_dump({**s, "post_refs": [r for r in s["post_refs"] if r in needed]
                        if s["sequence_id"] in in_scope else []}) for s in full.sequences]
    items = []
    for _line, row in _json_lines(raw[2]):
        row = {k: row[k] for k in ITEM_KEYS if k in row}
        if row["item_id"] in shown:
            ref = (row.get("ai") or {}).get("idea_ref")
            gone = ref in full.items and ref not in shown | numbered
            if gone and full.items[ref]["sequence_id"] is not None:
                row["ai"] = {**row["ai"], "idea_ref": full.items[ref]["sequence_id"]}
            items.append(_dump(row))
        elif row["item_id"] in numbered:
            items.append(_dump({**row, "context_refs": [], "holds": {}, "mapping": {},
                                "ai": {"family": (row.get("ai") or {}).get("family")}}))
    posts = [_dump({k: row[k] for k in POST_KEYS if k in row})
             for _line, row in _json_lines(raw[3]) if row.get("post_ref") in needed]
    media = {name: A.read_media(full, ref, name)
             for ref in sorted(needed) for name in full.posts[ref]["media"]}
    return full, [raw[0], _jsonl(sequences), _jsonl(items), _jsonl(posts)], media


def _shown(bundle: A.Bundle, item: dict, scope: int, image) -> dict:
    """What the workspace shows a reviewer of this scope for one item, per check, computed by
    the functions the workspace itself uses."""
    posts = [bundle.posts[r] for r in sorted({item["post_ref"], *item["context_refs"]})]
    others = [s["sequence_id"] for s in bundle.sequences
              if s["sequence_id"] != item["sequence_id"]]
    return {
        "item": {**item, "ai": {k: v for k, v in item["ai"].items() if k != "idea_ref"}},
        "system_reading": A.system_reading(bundle, item),
        "post_forms": [(f["item_id"], f["kind"], f["slot"], f["order_in_sequence"],
                        f["ai"]["family"]) for f in A.post_forms(bundle, item)],
        "post_siblings": A.post_siblings(bundle, item, scope),
        "table_row_details": A.table_row_details(bundle, item),
        "system_rows": A.system_rows(bundle, item),
        "form_context": [A.form_context(bundle, item, revealed=r) for r in (False, True)],
        "idea_options": R.offered("idea", bundle.families, others),
        "evidence": [(p, [sha256(image(p["post_ref"], name)) for name in p["media"]])
                     for p in posts],
    }


def parity_problems(full: A.Bundle, part: A.Bundle, media: dict[str, bytes],
                    scope: int) -> list[tuple[str, str]]:
    """(check, item id) wherever the published part would show a reviewer of ``scope`` anything
    other than the full bundle does. Empty: identical for every item in scope."""
    def order(b: A.Bundle):
        return [(sid, [i["item_id"] for i in items]) for sid, items in A.ordered(b, scope)]

    if (full.bundle_id, full.families, order(full)) != (part.bundle_id, part.families,
                                                       order(part)):
        return [("ordered", "-")]
    out = []
    for _sid, items in A.ordered(full, scope):
        for item in items:
            a = _shown(full, item, scope, lambda ref, name: A.read_media(full, ref, name))
            b = _shown(part, part.items[item["item_id"]], scope,
                       lambda _ref, name: media.get(name, b""))
            out += [(check, item["item_id"]) for check in a if a[check] != b[check]]
    return out


def _scope_fits(published: dict | None, scope: int) -> bool:
    return published is None or scope <= published["scope_sequences"] \
        or published["scope_sequences"] >= published["source_sequences"]


def cmd_publish_bundle(args) -> int:
    base = Path(args.demo).expanduser() if args.demo else _base(args)
    reviewers = A.load_reviewers(base / "reviewers.toml")
    A.check_folders(base, media=True)
    scope = reviewers.scope_sequences
    full, files, media = subset(base, scope)
    if bool(args.demo) != (full.manifest.get("product_commit") == "demo"):
        raise refused("NOT_A_DEMO_BUNDLE" if args.demo else "DEMO_BUNDLE_NEEDS_DEMO_FLAG")
    if not args.demo and not reviewers.remote_full_context_authorized:
        raise refused("REMOTE_FULL_CONTEXT_NOT_AUTHORIZED")  # the owner's switch: posts stay home
    part = A.bundle_from_bytes(*files, access=A.FULL)
    problems = parity_problems(full, part, media, scope)
    shown = sum(len(items) for _sid, items in A.ordered(part, scope))
    print(f"bundle {full.bundle_id[:12]}: scope {min(scope, len(full.sequences))} of "
          f"{len(full.sequences)} sequence(s); items {shown} shown, {len(part.items) - shown} "
          f"kept for form numbering, {len(full.items) - len(part.items)} left out; posts "
          f"{len(part.posts)} of {len(full.posts)}; images {len(media)}, "
          f"{sum(map(len, media.values()))} bytes; files {sum(map(len, files))} bytes")
    print(f"parity with the full bundle: {'FAIL' if problems else 'PASS'}, {shown} item(s) in "
          f"scope, {len(problems)} difference(s)")
    for check, item_id in problems[:40]:
        print(f"  differs: {check} of {item_id}")
    if problems:
        return 1
    if args.check_only:
        print("checked only: nothing uploaded")
        return 0
    store = _store()
    with store.exclusive():
        if scope < len(full.sequences) and any(
                r.bundle_id == full.bundle_id and r.scope_sequences > scope
                for r in store.reviewers()):
            raise refused("SCOPE_BELOW_A_REVIEWER")  # sync-reviewers first
        store.publish_bundle(*files, media, is_demo=bool(args.demo),
                             source_sequences=len(full.sequences), scope_sequences=scope)
    print(f"published and active: bundle {full.bundle_id[:12]}, "
          + ("demonstration data (for test identities; the real bundle stays)" if args.demo
             else "the real bundle (the demonstration bundle stays)"))
    return 0


# ------------------------------------------------------------------ reviewers


def cmd_init_db(_args) -> int:
    store = _store(unpooled=True)
    store.init_schema()
    with store.connection() as conn:
        tables = [name for (name,) in conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = current_schema()"
            " ORDER BY tablename").fetchall()]
    print(f"schema applied: {len(tables)} table(s): {', '.join(tables)}")
    return 0


def cmd_sync_reviewers(args) -> int:
    store = _store()
    if args.test_identity:  # a test identity works on the invented bundle, never the real one
        alias, login = args.test_identity
        if not TEST_ALIAS.match(alias):
            raise refused("TEST_ALIAS_IS_TEST_N")
        with store.exclusive():
            demo = store.published(demo=True)
            if demo is None:
                raise refused("NO_DEMO_BUNDLE_PUBLISHED")  # publish-bundle --demo DIR first
            rows = [store.upsert_reviewer(alias, login, name=alias, is_test=True,
                                          scope_sequences=demo["scope_sequences"],
                                          bundle_id=demo["bundle_id"])]
    else:
        reviewers = A.load_reviewers(_base(args) / "reviewers.toml")
        with store.exclusive():
            bundle = store.published(demo=args.demo)
            if bundle is None:  # publish-bundle first
                raise refused("NO_DEMO_BUNDLE_PUBLISHED" if args.demo
                              else "NO_REAL_BUNDLE_PUBLISHED")
            if not _scope_fits(bundle, reviewers.scope_sequences):
                raise refused("SCOPE_NOT_PUBLISHED")  # publish-bundle first
            rows = store.sync_reviewers(reviewers, bundle_id=bundle["bundle_id"])
    for r in rows:
        print(f"{r.alias}: id {r.id}, {r.role}, {r.access}, scope {r.scope_sequences}, bundle "
              f"{r.bundle_id[:12]}" + (" (demonstration data)" if args.demo or r.is_test else "")
              + (", test identity" if r.is_test else "") + (", REVOKED" if r.revoked else ""))
    print(f"{len(rows)} reviewer row(s) written; none deleted")
    return 0


def cmd_revoke(args) -> int:
    revoke = args.command == "revoke"
    sessions = _store().set_revoked(args.alias, revoke)
    print(f"{args.alias}: {'REVOKED' if revoke else 'access restored'}; {sessions} web "
          "session(s) deleted")
    return 0


def cmd_invite(args) -> int:
    """Queue an invitation: the SITE sends it (its sender is called with the operator's token),
    because the mail password exists only where the site runs."""
    kind = {"continue": "INVITE_CONTINUE", "start": "INVITE_START"}[args.kind]
    _store().queue_mail(args.alias, kind)
    print(f"{args.alias}: {kind} queued; the site sends it when its sender is called")
    return 0


# ------------------------------------------------------------------ the local log


def read_log(base: Path) -> tuple[bytes, bytes | None, V.Verification]:
    """(the log's bytes, its head file's bytes, its verification); refused unless chain and
    head verify. No log yet: empty."""
    path = A.annotations_path(base)
    data = V._read_private(path, WHAT) or b""
    head = V._read_private(V.head_path(path), WHAT + " head")
    log = V._with_head(V.verify_bytes(data, A.ANNOTATION_SCHEMA), head)
    if not log.ok:
        raise refused("ANNOTATION_LOG_FAILS_VERIFICATION")
    return data, head, log


def _lines(data: bytes) -> list[bytes]:
    return data.split(b"\n")[:-1]


def continues(lines: list[bytes], anchor: dict | None) -> bool:
    """Whether ``lines`` start with the log the hosted chain is anchored on."""
    count, head = (anchor["count"], anchor["head"]) if anchor else (0, V.GENESIS)
    return count <= len(lines) and (sha256(lines[count - 1]) if count else V.GENESIS) == head


def _login(rec: dict) -> str:
    login = (rec.get("annotator") or {}).get("login")
    return login.casefold() if isinstance(login, str) else ""


def _pulled(rec: dict) -> str | None:
    """The sha256 of the hosted line a local record is the pulled copy of, else None."""
    hosted = rec.get("hosted")
    return hosted.get("sha256") if isinstance(hosted, dict) else None


def states(bundle: A.Bundle, records: list[dict], session: A.Session) -> dict[str, str]:
    """Per form in the reviewer's scope, in review order: OPEN, INDEPENDENT_SAVED or DONE as
    ``A.progress`` says, with ``+SHOWN`` once the system's reading was shown before the reading
    after the reveal is saved (the next step is then that reading, never a blind one)."""
    return {item_id: f"{s}+SHOWN" if s != "DONE" and A._of(
        records, item_id, bundle.bundle_id, A.SYSTEM_SHOWN, session.login) else s
            for item_id, s in A.progress(bundle, records, session).status.items()}


def _print_states(alias: str, lead: str, found: dict[str, str]) -> None:
    print(f"{alias}: {lead}; forms {len(found)}: {_counts(sorted(found.values()))}")
    for item_id, state in found.items():
        if state != "OPEN":
            print(f"  {item_id} {state}")


def snapshot(root: Path, data: bytes, head: bytes | None, count: int) -> Path:
    """A byte copy of the log and its head file in ``root/log-<lines>-<sha256 prefix>/`` (folder
    0700, files 0600), with the sha256 of the log's bytes beside them. The same log state gives
    the same folder; a folder of that name holding other bytes is refused."""
    folder = root / f"log-{count:08d}-{sha256(data)[:16]}"
    for path in (root, folder):
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        if os.stat(path).st_mode & 0o077:
            raise refused("SNAPSHOT_FOLDER_NOT_0700")
    files = {LOG: data, f"{LOG}.sha256": f"{sha256(data)}  {LOG}\n".encode()}
    if head is not None:
        files[f"{LOG}.head"] = head
    for name, content in files.items():
        held = V._read_private(folder / name, "log snapshot")
        if held is None:
            with PrivateDir(folder / name, "log snapshot", create=True) as d:
                d.create(d.name, content)
        elif held != content:
            raise refused("SNAPSHOT_DIFFERS")
    return folder


def cmd_import_log(args) -> int:
    base, store = _base(args), _store()
    data, head, log = read_log(base)
    lines = _lines(data)
    if log.count >= HOSTED_BASE:
        raise refused("LOCAL_LOG_TOO_LONG")
    local = A.load_bundle(base / "bundle", A.RESTRICTED)
    with store.exclusive():
        events, anchor = store.events(), store.anchor()
        legacy = {e["seq"]: e["line"] for e in events if e["origin"] == LEGACY}
        if not continues(lines, anchor) or any(
                seq > len(lines) or lines[seq - 1] != line for seq, line in legacy.items()):
            raise refused("IMPORTED_PREFIX_DIFFERS")
        rows = {r.login: r for r in store.reviewers() if not r.is_test}
        hosted = {e["sha256"] for e in events if e["origin"] == HOSTED}
        writers = {e["alias"] for e in events if e["origin"] == HOSTED}
        new = []
        for n, (line, rec) in enumerate(zip(lines, log.records, strict=True), 1):
            row = rows.get(_login(rec))
            if row is None or n in legacy or _pulled(rec) in hosted:
                continue  # not a remote reviewer's, already imported, or a hosted save pulled back
            if n > (anchor["count"] if anchor else 0) and row.alias in writers:
                raise refused("LOCAL_SAVE_AFTER_HOSTED_SAVES")  # two histories of one reviewer
            try:
                line.decode("utf-8")
            except UnicodeDecodeError:
                raise refused("LINE_NOT_UTF8") from None
            new.append(S.EventLine(row.id, LEGACY, line))
        folder = snapshot(Path(args.snapshots).expanduser() if args.snapshots
                          else base / SNAPSHOTS, data, head, log.count)
        store.insert_events(new)
        if not hosted:
            store.set_anchor(log.count, log.head, sha256(data))
        anchor, events = store.anchor(), store.events()
    print(f"local log: {log.count} line(s), head {log.head[:12]}, {len(data)} bytes, sha256 "
          f"{sha256(data)}")
    print(f"snapshot: {folder.name}")
    print(f"anchor: {anchor['count']} line(s), head {anchor['head'][:12]}"
          + (" (fixed: hosted lines continue it)" if hosted else "") if anchor
          else "anchor: none (the hosted lines continue an empty log)")
    print(f"imported {len(new)} new line(s); {len(legacy)} already imported")
    for row in rows.values():
        mine = [json.loads(e["line"]) for e in events
                if e["alias"] == row.alias and e["origin"] != RETURN]
        _print_states(row.alias, f"lines {len(mine)} ({sum(e.reviewer_id == row.id for e in new)}"
                      " new)", states(local, [r for r in mine if r["bundle_id"]
                                              == local.bundle_id], row.session()))
    return 0


# ------------------------------------------------------------------ parity, status, verify


def _bodies(records: list[dict]) -> list[dict]:
    """A reviewer's records without the envelope of the log that holds them (``schema``, ``seq``,
    ``prev_sha256``, a pulled copy's ``hosted``), ``refers_to`` as the place of the record it
    names: equal for the local log and the hosted store when they hold the same history."""
    place = {r["seq"]: n for n, r in enumerate(records)}
    return [{**{k: v for k, v in r.items() if k not in (*ENVELOPE, "hosted")},
             "refers_to": place.get(r.get("refers_to"), r.get("refers_to"))} for r in records]


def _web_steps():
    """(next_step, step_state) of the web app's own ``reviewer_web.forms``, else None."""
    try:
        forms = importlib.import_module("reviewer_web.forms")
        return forms.next_step, forms.step_state
    except (ImportError, AttributeError):
        return None


def cmd_parity(args) -> int:
    base, store = _base(args), _store()
    _data, _head, log = read_log(base)
    local = A.load_bundle(base / "bundle", A.RESTRICTED)
    with store.connection():
        events = store.events()
        theirs = {r.alias: (r, store.bundle_for(r), store.records_of(r))
                  for r in store.reviewers() if not r.is_test}
    held = {_pulled(r) for r in log.records}
    steps = _web_steps()
    failed = steps is None
    if steps is None:  # the web app's resume rule is part of what must agree
        print("NEXT_STEP_FUNCTION_MISSING: reviewer_web.forms gives no next_step and step_state")
    print(f"local bundle {local.bundle_id[:12]}")
    for alias, (row, served, stored) in theirs.items():
        session = row.session()
        mine = [r for r in log.records if _login(r) == row.login
                and r.get("bundle_id") == local.bundle_id]
        hosted = [r for r in stored if _login(r) == row.login]  # the reviewer's own
        a, b = states(local, mine, session), states(served, hosted, session)
        same = _bodies(mine) == _bodies(hosted) and served.bundle_id == local.bundle_id
        differing = [i for i in dict.fromkeys([*a, *b]) if a.get(i) != b.get(i)]
        waiting = sum(e["alias"] == alias and e["origin"] == HOSTED and e["sha256"] not in held
                      for e in events)
        print(f"{alias}: bundle {served.bundle_id[:12]}; records local {len(mine)}, hosted "
              f"{len(hosted)}: {'the same' if same else 'DIFFERENT'}; forms {len(b)}: "
              f"{len(b) - len(differing)} equal, {len(differing)} different; hosted lines not "
              f"pulled: {waiting}")
        for item_id in differing:
            print(f"  {item_id}: local {a.get(item_id, '-')}, hosted {b.get(item_id, '-')}")
        failed = failed or not same or bool(differing)
        if steps is None:
            continue
        # The next step as the site computes it: over what the local log holds for this
        # reviewer (their lines and the approver's decisions) and over what the site serves.
        at_home = [r for r in log.records if r.get("bundle_id") == local.bundle_id
                   and (_login(r) == row.login or r.get("phase") == A.APPROVAL)]
        resume = []
        for bundle, records in ((local, at_home), (served, stored)):
            item_id = steps[0](bundle, session, records)
            resume.append((item_id or "-", steps[1](bundle, session, records, item_id)
                           if item_id else "nothing due"))
        print(f"  next step: local {' '.join(resume[0])}, hosted {' '.join(resume[1])}: "
              f"{'the same' if resume[0] == resume[1] else 'DIFFERENT'}")
        failed = failed or resume[0] != resume[1]
    print(f"parity of the local log and the hosted store: {'FAIL' if failed else 'PASS'}, "
          f"{len(theirs)} reviewer(s)")
    return int(failed)


def _print_bundle(published: dict | None, kind: str) -> None:
    if published is None:
        print(f"{kind} bundle: none published")
        return
    print(f"{kind} bundle {published['bundle_id'][:12]}: published "
          f"{_time(published['published_at'])}, scope {published['scope_sequences']} of "
          f"{published['source_sequences']} sequence(s), {published['bytes']} bytes, "
          f"{published['images']} image(s) {published['image_bytes']} bytes")


def cmd_status(_args) -> int:
    store = _store()
    with store.connection():
        real, demo = store.published(demo=False), store.published(demo=True)
        anchor, events, rows = store.anchor(), store.events(), store.status()
        mail = store.queued_mail()
    _print_bundle(real, "real")
    _print_bundle(demo, "demonstration")
    print(f"anchor: {anchor['count']} line(s), head {anchor['head'][:12]}, imported "
          f"{_time(anchor['imported_at'])}" if anchor else "anchor: none (no log imported)")
    for r in rows:
        if r["is_test"]:
            continue  # see test-report
        mine = [e for e in events if e["alias"] == r["alias"]]
        print(f"{r['alias']}: bundle {(r['bundle_id'] or '-')[:12]}, scope "
              f"{r['scope_sequences']}, invited {_time(r['invited_at'])}, first sign-in "
              f"{_time(r['first_sign_in_at'])}, first hosted save {_time(r['first_hosted_at'])}, "
              f"revoked {_time(r['revoked_at'])}")
        for origin in dict.fromkeys(e["origin"] for e in mine):
            print(f"  {origin}: " + _counts(e["phase"] for e in mine if e["origin"] == origin))
        for m in (m for m in mail if m["alias"] == r["alias"]):
            print(f"  mail {m['kind']}: queued {_time(m['queued_at'])}, sent "
                  f"{_time(m['sent_at'])}, result {m['result'] or '-'}")
    print(f"test identities: {sum(r['is_test'] for r in rows)} (see test-report ALIAS)")
    return 0


def cmd_test_report(args) -> int:
    """A test identity's sign-in, saves and resume point: how a production sign-in is checked."""
    store = _store()
    with store.connection():
        row = next((r for r in store.reviewers() if r.alias == args.alias), None)
        if row is None or not row.is_test:
            raise refused("UNKNOWN_ALIAS" if row is None else "NOT_A_TEST_IDENTITY")
        status = next(r for r in store.status() if r["alias"] == row.alias)
        events = [e for e in store.events() if e["alias"] == row.alias]
        bundle, records = store.bundle_for(row), store.records_of(row)
    print(f"{row.alias} (test identity): bundle {bundle.bundle_id[:12]} (demonstration data), "
          f"scope {row.scope_sequences}, first sign-in {_time(status['first_sign_in_at'])}, web "
          f"sessions {status['sessions']}, revoked {_time(status['revoked_at'])}")
    print(f"events: {len(events)}")
    for e in events:
        print(f"  {e['seq']} {_time(e['at'])} {e['phase']} {e['item_id']}")
    _print_states(row.alias, f"records {len(records)}", states(bundle, records, row.session()))
    return 0


def _record(line: bytes) -> dict:
    try:
        rec = json.loads(line)
    except ValueError:
        return {}
    return rec if isinstance(rec, dict) else {}


def chain_problems(events: list[dict], anchor: dict | None) -> list[str]:
    """Constant codes, each with a line's seq: every line against its row (hash, seq, previous
    hash, schema); the HOSTED lines as ONE chain that starts on the anchor's head and counts
    1000001, 1000002, ...; and every RETURN as the return of a reading after the reveal that
    the same reviewer has in the store (its seq, and its sha256 as the RETURN's previous hash)."""
    out = []
    rows = {e["seq"]: (e, _record(e["line"])) for e in events}
    prev, due = anchor["head"] if anchor else V.GENESIS, HOSTED_BASE + 1
    for e, rec in rows.values():
        if sha256(e["line"]) != e["sha256"] or (rec.get("seq"), rec.get("prev_sha256"), rec.get(
                "schema")) != (e["seq"], e["prev_sha256"], A.ANNOTATION_SCHEMA):
            out.append(f"LINE_DIFFERS_FROM_ITS_ROW at {e['seq']}")
        if e["origin"] == HOSTED:
            if (e["seq"], e["prev_sha256"]) != (due, prev):
                out.append(f"HOSTED_CHAIN_BROKEN at {e['seq']}")
            prev, due = e["sha256"], e["seq"] + 1
        elif e["origin"] == RETURN:
            local = rec.get("local") if isinstance(rec.get("local"), dict) else {}
            reviewed, reading = rows.get(rec.get("refers_to"), ({}, {}))
            if (e["seq"] - RETURN_BASE, rec.get("phase"), rec.get("decision")) != (
                    local.get("seq"), A.APPROVAL, "RETURN") or (
                    reviewed.get("alias"), reviewed.get("sha256"), reading.get("phase")) != (
                    e["alias"], e["prev_sha256"], A.AFTER_REVEAL):
                out.append(f"RETURN_DOES_NOT_NAME_ITS_READING at {e['seq']}")
    return out


def cmd_verify(args) -> int:
    store = _store()
    with store.connection():
        events, anchor = store.events(), store.anchor()
        logins = {r.login for r in store.reviewers() if not r.is_test}
    problems = chain_problems(events, anchor)
    legacy = {e["seq"]: e["line"] for e in events if e["origin"] == LEGACY}
    hosted = {e["sha256"] for e in events if e["origin"] == HOSTED}
    print(f"events: {_counts(e['origin'] for e in events)}; hosted chain on "
          f"{(anchor['head'] if anchor else V.GENESIS)[:12]}: "
          f"{'OK' if not problems else 'FAILS'}")
    root = Path(args.snapshots).expanduser() if args.snapshots else _base(args) / SNAPSHOTS
    folders = sorted(root.glob("log-*")) if root.is_dir() else []
    if not folders:
        problems.append("NO_SNAPSHOT")
    else:
        data = V._read_private(folders[-1] / LOG, "log snapshot") or b""
        log = V._with_head(V.verify_bytes(data, A.ANNOTATION_SCHEMA),
                           V._read_private(folders[-1] / f"{LOG}.head", "log snapshot"))
        lines = _lines(data)
        if not log.ok or folders[-1].name != f"log-{log.count:08d}-{sha256(data)[:16]}":
            problems.append("SNAPSHOT_FAILS_VERIFICATION")
        elif not continues(lines, anchor) or (anchor and anchor["count"] == log.count
                                              and anchor["file_sha256"] != sha256(data)):
            problems.append("SNAPSHOT_IS_NOT_THE_ANCHORED_LOG")
        else:
            problems += [f"LEGACY_LINE_DIFFERS at {seq}" for seq, line in legacy.items()
                         if seq > len(lines) or lines[seq - 1] != line]
            problems += [f"LEGACY_LINE_MISSING at {n}" for n, rec in enumerate(log.records, 1)
                         if _login(rec) in logins and n not in legacy
                         and _pulled(rec) not in hosted]
        print(f"snapshot {folders[-1].name}: {len(lines)} line(s); {len(legacy)} LEGACY line(s) "
              "compared with it")
    for problem in problems[:40]:
        print(f"  {problem}")
    print(f"verify: {'FAIL' if problems else 'PASS'}, {len(problems)} problem(s)")
    return int(bool(problems))


# ------------------------------------------------------------------ export, restore


def cmd_export(args) -> int:
    store = _store()
    with store.connection():
        events, anchor = store.events(), store.anchor()
    header = {"schema": EXPORT_SCHEMA, "exported_at": _time(datetime.now(UTC)),
              "events": len(events),
              "anchor": anchor and {**anchor, "imported_at": anchor["imported_at"].isoformat()}}
    rows = [{**{k: e[k] for k in EVENT_KEYS}, "line": e["line"].decode("utf-8"),
             "created_at": e["created_at"].isoformat()} for e in events]
    data = _jsonl(_dump(r) for r in [header, *rows])
    with PrivateDir(args.out, "hosted export", create=True) as d:
        d.create(d.name, data)  # never over an existing file
    print(f"exported {len(events)} event(s): {_counts(e['origin'] for e in events)}; "
          f"{len(data)} bytes, sha256 {sha256(data)}")
    return 0


def cmd_restore(args) -> int:
    data = V._read_private(args.infile, "hosted export")
    try:
        header, *rows = [json.loads(x) for x in (data or b"").decode("utf-8").splitlines()]
        events = [{**{k: r[k] for k in EVENT_KEYS}, "line": r["line"].encode("utf-8"),
                   "created_at": datetime.fromisoformat(r["created_at"])} for r in rows]
        anchor = header["anchor"] and {**header["anchor"], "imported_at": datetime.fromisoformat(
            header["anchor"]["imported_at"])}
        if header["schema"] != EXPORT_SCHEMA or header["events"] != len(events):
            raise ValueError
    except (ValueError, KeyError, TypeError, AttributeError):
        raise refused("EXPORT_UNREADABLE") from None
    if chain_problems(events, anchor) or [e["seq"] for e in events] != sorted(
            {e["seq"] for e in events}):
        raise refused("EXPORT_FAILS_VERIFICATION")
    store = _store()
    with store.exclusive():
        if store.events() or store.anchor():
            raise refused("DATABASE_NOT_EMPTY")
        rows = {r.alias: r for r in store.reviewers()}
        if not {e["alias"] for e in events} <= set(rows):
            raise refused("UNKNOWN_ALIAS")  # sync-reviewers first
        if any(rows[e["alias"]].is_test != e["is_test"] for e in events):
            raise refused("ALIAS_IS_NOT_THE_SAME_KIND")  # a test identity's lines stay a test's
        if anchor:
            store.set_anchor(anchor["count"], anchor["head"], anchor["file_sha256"],
                             anchor["imported_at"])
        store.insert_events([S.EventLine(rows[e["alias"]].id, e["origin"], e["line"],
                                         e["idempotency_key"], e["batch"], e["created_at"])
                             for e in events])
        stored = [{k: e[k] for k in EVENT_KEYS} for e in store.events()]
        if stored != events or store.anchor() != anchor:
            raise refused("RESTORE_DIFFERS")  # rolled back
    print(f"restored {len(events)} event(s): {_counts(e['origin'] for e in events)}; every "
          f"hash and the hosted chain verified; file sha256 {sha256(data)}")
    return 0


# ------------------------------------------------------------------ hosted saves, back home


def cmd_pull_to_local(args) -> int:
    base, store = _base(args), _store()
    data, _head, log = read_log(base)
    with store.connection():
        events, anchor = store.events(), store.anchor()
    if not continues(_lines(data), anchor):
        raise refused("LOCAL_LOG_IS_NOT_THE_ANCHORED_LOG")
    if chain_problems(events, anchor):
        raise refused("HOSTED_STORE_FAILS_VERIFICATION")
    held = {_pulled(r): r["seq"] for r in log.records if _pulled(r)}
    local: dict[int, int] = {}  # a hosted line's seq -> the seq of its copy in the local log
    pulled = []
    for e in events:
        if e["origin"] != HOSTED or e["is_test"]:
            continue
        if e["sha256"] in held:
            local[e["seq"]] = held[e["sha256"]]
            continue
        rec = json.loads(e["line"])
        body = {k: v for k, v in rec.items() if k not in ENVELOPE}
        if isinstance(body.get("refers_to"), int) and body["refers_to"] > HOSTED_BASE:
            if body["refers_to"] not in local:
                raise refused("REFERS_TO_A_LINE_NOT_PULLED")
            body["refers_to"] = local[body["refers_to"]]
        body["hosted"] = {"seq": e["seq"], "sha256": e["sha256"]}
        local[e["seq"]] = V.append_chained(A.annotations_path(base), WHAT, A.ANNOTATION_SCHEMA,
                                           lambda _v, body=body: body)["seq"]
        pulled.append(e["alias"])
    after = A.read_annotations(base)
    print(f"pulled {len(pulled)} hosted line(s) into the local log: {_counts(pulled)}; "
          f"{len(held)} already held")
    print(f"annotation log: {'OK' if after.ok else 'FAILS ' + str(after.problem)}, "
          f"{after.count} line(s), head {after.head[:12]}")
    return 0 if after.ok else 1


# ------------------------------------------------------------------ the approver's returns


def return_record(approval: dict, line_sha256: str, reviewed: dict) -> dict:
    """The record the hosted store keeps of a local RETURN, for the reviewer whose reading it
    returns: an APPROVAL record of the log's shape, so that ``A.effective_approval`` and
    ``A._outcome`` read it as they read the local one. ``reviewed``: the hosted event of the
    returned reading. It carries the decision, the approver's notes and time, the hosted ``seq``
    of the returned reading (``refers_to``) and that reading's sha256 (``prev_sha256``: it is
    outside the hosted chain); of the approver only the role; and the local line it was made
    from (``local``)."""
    return {"schema": A.ANNOTATION_SCHEMA, "seq": RETURN_BASE + approval["seq"],
            "prev_sha256": reviewed["sha256"], "at": approval["at"],
            "bundle_id": approval["bundle_id"], "item_id": approval["item_id"],
            "annotator": {"login": APPROVER_SHOWN, "name": APPROVER_SHOWN.capitalize(),
                          "role": A.APPROVER, "access": A.FULL},
            "phase": A.APPROVAL, "blind": False, "reading": {}, "field_verdicts": {},
            "flags": [], "confidence": "", "notes": approval.get("notes") or "",
            "refers_to": reviewed["seq"], "decision": "RETURN", "rubric": A.RUBRIC,
            "acknowledged_warnings": [],
            "local": {"seq": approval["seq"], "sha256": line_sha256}}


def cmd_push_returns(args) -> int:
    base, store = _base(args), _store()
    data, _head, log = read_log(base)
    lines = _lines(data)
    local = A.load_bundle(base / "bundle", A.RESTRICTED)
    new = A.rubric2(log.records)
    standing = [a for a in (A.effective_approval(new, i, local) for i in local.items)
                if a is not None and A._outcome(a) == "RETURN"]
    with store.exclusive():
        events, anchor = store.events(), store.anchor()
        if not continues(lines, anchor):
            raise refused("LOCAL_LOG_IS_NOT_THE_ANCHORED_LOG")
        if chain_problems(events, anchor):
            raise refused("HOSTED_STORE_FAILS_VERIFICATION")
        rows = {r.login: r for r in store.reviewers()
                if not r.is_test and r.bundle_id == local.bundle_id}  # who is served that bundle
        by_seq = {e["seq"]: e for e in events}
        by_sha = {e["sha256"]: e for e in events}
        pushed, known, skipped, elsewhere = [], 0, 0, 0
        for approval in standing:
            source = log.records[approval["refers_to"] - 1]  # the reading it returns
            row = rows.get(_login(source))
            if row is None:
                elsewhere += 1  # a local reviewer's reading: nothing to push
                continue
            # The same reading in the store: the pulled copy's hosted line, else the imported one.
            reviewed = by_sha.get(_pulled(source) or sha256(lines[source["seq"] - 1]))
            if reviewed is None or reviewed["alias"] != row.alias or reviewed["origin"] not in (
                    LEGACY, HOSTED):
                skipped += 1
                continue
            line = V.encode_line(return_record(approval, sha256(lines[approval["seq"] - 1]),
                                               reviewed))
            held = by_seq.get(RETURN_BASE + approval["seq"])
            if held is not None and held["sha256"] != sha256(line):
                raise refused("PUSHED_RETURN_DIFFERS")
            if held is not None:
                known += 1
                continue
            store.insert_events([S.EventLine(row.id, RETURN, line)])
            pushed.append((row.alias, approval["item_id"], RETURN_BASE + approval["seq"],
                           reviewed["seq"]))
        events = store.events()
    print(f"returns that stand in the local log: {len(standing)}; {elsewhere} of a reading that "
          "was not made by a hosted reviewer of this bundle")
    print(f"pushed {len(pushed)} new return(s): {_counts(p[0] for p in pushed)}; {known} already "
          f"stored; {skipped} skipped (the returned reading is not in the hosted store)")
    for alias, item_id, seq, reviewed_seq in pushed:
        print(f"  {alias} {item_id}: return {seq} of the reading {reviewed_seq}")
    # A pushed return that no longer stands locally (the approver decided again) but still shows
    # on the site: no line of the store can be taken back; the reviewer's next save clears it.
    stands = {a["seq"] for a in standing}
    for e in events:
        if e["origin"] != RETURN:
            continue
        rec = json.loads(e["line"])
        theirs = [json.loads(x["line"]) for x in events if x["alias"] == e["alias"]
                  and json.loads(x["line"])["bundle_id"] == rec["bundle_id"]]
        shown = A.effective_approval(A.rubric2(theirs), rec["item_id"], local)
        if rec["local"]["seq"] not in stands and shown is not None and shown["seq"] == e["seq"]:
            print(f"  NO_LONGER_STANDS_LOCALLY {e['alias']} {rec['item_id']}: return {e['seq']} "
                  "still shows on the site until that reviewer saves again")
    return 0


# ------------------------------------------------------------------ the command line


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--dir", default=os.environ.get(A.DIR_ENV),
                        help=f"the adjudication folder (default: {A.DIR_ENV})")
    sub = ap.add_subparsers(dest="command", required=True)

    def command(name: str, run, text: str) -> argparse.ArgumentParser:
        p = sub.add_parser(name, parents=[common], help=text)
        p.set_defaults(run=run)
        return p

    command("init-db", cmd_init_db, "apply schema.sql (can be run again)")
    p = command("sync-reviewers", cmd_sync_reviewers,
                "the reviewers file's remote reviewers as REMOTE-<n> rows on the real bundle")
    p.add_argument("--demo", action="store_true",
                   help="bind them to the demonstration bundle instead (a preview database)")
    p.add_argument("--test-identity", nargs=2, metavar=("ALIAS", "LOGIN"),
                   help="instead: write the test row TEST-<n> for this sign-in, bound to the "
                        "demonstration bundle")
    for name, text in (("revoke", "stop a reviewer's access at once"),
                       ("restore-access", "lift a revocation")):
        command(name, cmd_revoke, text).add_argument("alias")
    p = command("invite", cmd_invite, "queue an invitation for the site to send")
    p.add_argument("alias")
    p.add_argument("--kind", required=True, choices=("continue", "start"),
                   help="continue: the site has moved; start: a first access")
    p = command("publish-bundle", cmd_publish_bundle,
                "publish the part of the local bundle the scope needs, after a parity check")
    p.add_argument("--check-only", action="store_true", help="cut and check; upload nothing")
    p.add_argument("--demo", metavar="DIR", help="publish the invented demo folder DIR instead")
    command("import-log", cmd_import_log,
            "snapshot the local log and import the remote reviewers' lines").add_argument(
        "--snapshots", metavar="DIR", help=f"snapshot root (default: <dir>/{SNAPSHOTS})")
    command("parity", cmd_parity, "compare the local log's state with the hosted store's")
    command("export", cmd_export, "every event as JSONL").add_argument(
        "--out", required=True, metavar="FILE")
    command("restore", cmd_restore, "load an export into an EMPTY database").add_argument(
        "--in", dest="infile", required=True, metavar="FILE")
    command("pull-to-local", cmd_pull_to_local,
            "append the hosted saves the local log does not hold yet")
    command("push-returns", cmd_push_returns,
            "store the approver's standing local returns for the reviewers they concern")
    command("status", cmd_status, "the bundles, and per reviewer: times, counts by phase")
    command("test-report", cmd_test_report,
            "a test identity's sign-in, events and form states").add_argument("alias")
    command("verify", cmd_verify,
            "the hosted chain, and the imported lines against the snapshot").add_argument(
        "--snapshots", metavar="DIR", help=f"snapshot root (default: <dir>/{SNAPSHOTS})")
    args = ap.parse_args(argv)
    try:
        return args.run(args)
    except (A.AdjudicationError, V.RecommendationError) as exc:
        print(f"refused: {exc.code}", file=sys.stderr)
    except (OSError, PrivatePathError) as exc:
        print(f"refused: {type(exc).__name__}", file=sys.stderr)
    except psycopg.Error as exc:  # never the message: it may name the host or hold content
        print(f"refused: DATABASE_{type(exc).__name__}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
