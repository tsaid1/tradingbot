"""Operator, locally: the hosted reviewer site's database (docs/HOSTED_REVIEWER.md, "Operations").

    python scripts/hosted_admin.py init-db
    python scripts/hosted_admin.py sync-reviewers [--demo [--force] | --test-identity ALIAS LOGIN]
    python scripts/hosted_admin.py revoke ALIAS
    python scripts/hosted_admin.py restore-access ALIAS
    python scripts/hosted_admin.py invite ALIAS --kind continue|start
    python scripts/hosted_admin.py unlock ALIAS
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
    python scripts/hosted_admin.py make-site-role --out FILE [--role NAME]
    python scripts/hosted_admin.py make-site-role --grants-only [--role NAME]

The database is ``HOSTED_DATABASE_URL`` (``init-db`` prefers ``HOSTED_DATABASE_URL_UNPOOLED``);
the adjudication folder is ``--dir`` or ``TRADINGBOT_ADJUDICATION_DIR``. Never deployed. It
prints counts, ids, aliases, times, sizes and fingerprints only: never a sign-in address, a name,
or a word of a post or a reading; every refusal is a constant code. The owner's reviewers file is
only read. Exit 0 when all is well, 1 when a check fails (parity, verify), 2 when refused: a
refusal changes nothing, except that a ``pull-to-local`` which fails midway has appended the
lines before the failure (run it again: it appends the rest).
"""
from __future__ import annotations

import argparse
import dataclasses
import importlib
import json
import os
import re
import secrets
import sys
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo

from tradingbot.runtime import adjudication as A
from tradingbot.runtime import adjudication_rubric as R
from tradingbot.runtime import vocab_recommend as V
from tradingbot.runtime.vocab_review import PrivateDir, PrivatePathError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hosted"))
S = importlib.import_module("reviewer_web.store")  # the deployed layout: a top-level package
AUTH = importlib.import_module("reviewer_web.auth")  # the sign-in counters (invite, unlock)
HOSTED, LEGACY, RETURN, TEST, sha256 = S.HOSTED, S.LEGACY, S.RETURN, S.TEST, S.sha256
HOSTED_BASE, RETURN_BASE, TEST_BASE = S.HOSTED_BASE, S.RETURN_BASE, S.TEST_BASE

URL_ENV, UNPOOLED_ENV = "HOSTED_DATABASE_URL", "HOSTED_DATABASE_URL_UNPOOLED"
OWNER_ADDRESS_ENV = "HOSTED_OWNER_ADDRESS_FILE"  # names the private file of the owner's address
OWNER_REVIEWER = S.OWNER_REVIEWER
REPO = Path(__file__).resolve().parents[1]
SYNCED = ("dropbox", "cloudstorage", "icloud", "onedrive", "google drive", "mobile documents")
EXPORT_SCHEMA = "tradingbot.hosted.export/1"
SNAPSHOTS, LOG, WHAT = "hosted_snapshots", "annotations.jsonl", "annotation log"
BUNDLE_FILES = ("manifest.json", "sequences.jsonl", "items.jsonl", "posts.jsonl")
# What is published of a line: the keys the loader reads (``adjudication._item``, ``_ai``,
# ``_levels``, ``_holds``, ``_mapping``, ``_post``) and a page or a check of the site uses.
# Nothing else of a line leaves the Mac; ``parity_problems`` compares the loaded result with the
# full bundle's, so a key that matters and is missing here fails the publication.
ITEM_KEYS = ("item_id", "kind", "sequence_id", "post_ref", "slot", "order_in_sequence",
             "context_refs", "ai", "holds", "mapping")  # not ``set``: no page shows it
STUB_KEYS = ("item_id", "kind", "post_ref", "slot", "order_in_sequence")  # form numbering
AI_KEYS = ("post_label", "action", "family", "instrument_as_read", "side", "effect", "timing",
           "explicit", "ambiguities", "idea_ref")
AI_PARTS = {"entry": ("price", "role", "exactness"), "stop": ("price",),
            "amendment": ("field", "from", "to"),
            "position_context": ("state", "side", "last_stop")}
AI_LISTS = {"targets": ("price",),
            "levels": ("n", "price", "role", "exactness", "contract_month")}
HOLD_KEYS = ("status", "reasons")
MAPPING_KEYS = ("reference_series", "proposed_execution_instrument", "basis_treatment",
                "approved")
POST_KEYS = ("post_ref", "published_at", "text", "media", "revision", "numbers", "reply_to",
             "quotes")  # a form's own post
CONTEXT_KEYS = ("post_ref", "published_at", "text")  # a post shown as context: its text only
MANIFEST_KEYS = ("schema", "bundle_id", "families")
STUB_TIME = "1970-01-01T00:00:00+00:00"  # of a post that is named by its id only
TEST_ALIAS = re.compile(r"^TEST-[0-9]{1,3}$")
APPROVER_SHOWN = "approver"  # all a reviewer learns of who returned a reading
WITHDRAWN = "WITHDRAWN"  # the decision of a line that clears a pushed return (never RETURN)
MAX_LOCAL_LINES = 499_999  # a RETURN-stream seq is 2000000 + twice a local line's seq (+ 1)
ENVELOPE = ("schema", "seq", "prev_sha256")
EVENT_KEYS = ("alias", "is_test", "origin", "seq", "prev_sha256", "sha256", "line",
              "idempotency_key", "batch", "created_at")
# What the SITE's own database role may do, and nothing else. It owns nothing, so it can neither
# disable nor drop the trigger that makes ``event`` append-only.
SITE_ROLE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
SITE_READS = ("reviewer", "bundle", "media", "event", "chain_anchor", "mail_queue")
SITE_INSERTS = ("event",)
SITE_OWN = ("login_code", "web_session", "rate_limit", "sign_in_choice", "draft")  # the sign-in tables: every row operation
SITE_SETS = {"reviewer": ("first_sign_in_at", "invited_at"),
             # a try is counted and its start noted BEFORE the mail server hears of it
             "mail_queue": ("attempts", "sending_at", "result", "sent_at")}


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


def _keep(raw, keys):
    return {k: raw[k] for k in keys if k in raw} if isinstance(raw, dict) else raw


def _read_keys(row: dict) -> dict:
    """An item line with the keys the loader reads, at every level."""
    out = _keep(row, ITEM_KEYS)
    ai = out.get("ai")
    if isinstance(ai, dict):
        out["ai"] = {
            **_keep(ai, AI_KEYS),
            **{k: _keep(ai[k], keys) for k, keys in AI_PARTS.items() if k in ai},
            **{k: [_keep(x, keys) for x in ai[k]] if isinstance(ai[k], list) else ai[k]
               for k, keys in AI_LISTS.items() if k in ai}}
    if isinstance(out.get("holds"), dict):
        out["holds"] = {k: _keep(v, HOLD_KEYS) for k, v in out["holds"].items()}
    if "mapping" in out:
        out["mapping"] = _keep(out["mapping"], MAPPING_KEYS)
    return out


def subset(base: Path, scope: int) -> tuple[A.Bundle, list[bytes], dict[str, bytes]]:
    """(the verified local bundle, the four files of its published part, that part's images).

    The part holds what a page of a reviewer of the first ``scope`` sequences shows or a check
    of the site reads, and nothing else:

    * the items ``ordered(bundle, scope)`` offers (those of no sequence only when the scope
      covers every sequence), with the keys the loader reads (``ITEM_KEYS`` and below);
    * the OWN post of each such item whole (text, numbers, links) and its images: the only
      images a page shows; a post shown as context with its time and text only; a post that is
      only named (the target of an own post's reply or quote) as a stub without text;
    * EVERY sequence id with its place in the review order (the ``idea`` choices and the scope
      rule read them), nothing else of a sequence outside the scope;
    * an instruction item outside the scope that shares its post with an item inside it, as a
      stub: its place among the post's forms ("form k of m" counts it), and its market only when
      it comes before a form in scope (``position_compared`` reads the earlier forms' markets);
    * a system reading that refers (``ai.idea_ref``) to an item left out refers to that item's
      sequence instead, which reads as the same idea association;
    * of the manifest: schema, the ORIGINAL ``bundle_id`` (the records carry it), the families.

    ``parity_problems`` proves that none of this changes what the reviewer sees."""
    root = base / "bundle"
    full = A.load_bundle(root, A.FULL)
    raw = [A._read(root, name) for name in BUNDLE_FILES]
    if A.bundle_from_bytes(*raw, access=A.FULL) != dataclasses.replace(full, root=None):
        raise refused("BUNDLE_CHANGED_WHILE_READ")
    shown = [i for _sid, items in A.ordered(full, scope) for i in items]
    ids = {i["item_id"] for i in shown}
    own = {i["post_ref"] for i in shown}
    context = {r for i in shown for r in i["context_refs"]} - own
    named = {full.posts[r][k] for r in own for k in ("reply_to", "quotes")
             if full.posts[r][k]} - own - context
    forms = {i["post_ref"] for i in shown if i["kind"] == "INSTRUCTION"}
    numbered = {i for i, x in full.items.items() if i not in ids
                and x["kind"] == "INSTRUCTION" and x["post_ref"] in forms}
    before = set()  # stubs that come before a form in scope: their market is read
    for item in shown:
        for form in A.post_forms(full, item):
            if form["item_id"] == item["item_id"]:
                break
            before.add(form["item_id"])
    in_scope = {s["sequence_id"] for s in full.sequences[:scope]}
    sequences = [_dump({"sequence_id": s["sequence_id"], "review_order": s["review_order"],
                        "post_refs": [r for r in s["post_refs"] if r in own]
                        if s["sequence_id"] in in_scope else []}) for s in full.sequences]
    items = []
    for _line, row in _json_lines(raw[2]):
        if row["item_id"] in ids:
            row = _read_keys(row)
            ref = (row.get("ai") or {}).get("idea_ref")
            gone = ref in full.items and ref not in ids | numbered
            if gone and full.items[ref]["sequence_id"] is not None:
                row["ai"] = {**row["ai"], "idea_ref": full.items[ref]["sequence_id"]}
            items.append(_dump(row))
        elif row["item_id"] in numbered:
            market = {"ai": {"family": (row.get("ai") or {}).get("family")}} \
                if row["item_id"] in before else {}
            items.append(_dump({**_keep(row, STUB_KEYS), **market}))
    posts = []
    for _line, row in _json_lines(raw[3]):
        ref = row.get("post_ref")
        if ref in own:
            posts.append(_dump(_keep(row, POST_KEYS)))
        elif ref in context:
            posts.append(_dump(_keep(row, CONTEXT_KEYS)))
        elif ref in named:
            posts.append(_dump({"post_ref": ref, "published_at": STUB_TIME, "text": ""}))
    media = {name: A.read_media(full, ref, name)
             for ref in sorted(own) for name in full.posts[ref]["media"]}
    manifest = json.dumps(_keep(full.manifest, MANIFEST_KEYS), sort_keys=True).encode("utf-8")
    return full, [manifest, _jsonl(sequences), _jsonl(items), _jsonl(posts)], media


def _shown(bundle: A.Bundle, item: dict, scope: int, image) -> dict:
    """What the site shows a reviewer of this scope for one item, or reads to check a save, per
    check, computed by the functions the workspace itself uses. ``image(post, name)``: the bytes
    of an image, or None."""
    post = bundle.posts[item["post_ref"]]
    forms = A.post_forms(bundle, item)
    place = next(n for n, f in enumerate(forms) if f["item_id"] == item["item_id"])
    others = [s["sequence_id"] for s in bundle.sequences
              if s["sequence_id"] != item["sequence_id"]]
    return {
        "item": {**{k: v for k, v in item.items() if k != "set"},
                 "ai": {k: v for k, v in item["ai"].items() if k != "idea_ref"}},
        "system_reading": A.system_reading(bundle, item),
        "post_forms": [(f["item_id"], f["kind"]) for f in forms],
        "earlier_markets": [f["ai"]["family"] for f in forms[:place]],
        "post_siblings": A.post_siblings(bundle, item, scope),
        "table_row_details": A.table_row_details(bundle, item),
        "system_rows": A.system_rows(bundle, item),
        "form_context": [A.form_context(bundle, item, revealed=r) for r in (False, True)],
        "idea_options": R.offered("idea", bundle.families, others),
        "evidence": (post, sorted((r, bundle.posts[r]["published_at"], bundle.posts[r]["text"])
                                  for r in set(item["context_refs"]) - {item["post_ref"]})),
        "images": [(name, data and sha256(data)) for name in post["media"]
                   for data in [image(item["post_ref"], name)]],
    }


def parity_problems(full: A.Bundle, part: A.Bundle, media: dict[str, bytes],
                    scope: int) -> list[tuple[str, str]]:
    """(check, item id) wherever the published part would show a reviewer of ``scope`` anything
    other than the full bundle does, an image included: every image a page shows must be in the
    publication with the sha256 of the local file. Empty: identical for every item in scope."""
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
                       lambda _ref, name: media.get(name))
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
    offered = [i for _sid, items in A.ordered(part, scope) for i in items]
    shown, texts = len(offered), sum(bool(p["text"]) for p in part.posts.values())
    images = {(i["post_ref"], name) for i in offered for name in full.posts[i["post_ref"]]["media"]}
    print(f"bundle {full.bundle_id[:12]}: scope {min(scope, len(full.sequences))} of "
          f"{len(full.sequences)} sequence(s); items {shown} shown, {len(part.items) - shown} "
          f"kept for form numbering, {len(full.items) - len(part.items)} left out; posts "
          f"{texts} with text and {len(part.posts) - texts} named only, of {len(full.posts)}; "
          f"images {len(media)}, {sum(map(len, media.values()))} bytes; files "
          f"{sum(map(len, files))} bytes")
    print(f"parity with the full bundle: {'FAIL' if problems else 'PASS'}, {shown} item(s) in "
          f"scope, {len(problems)} difference(s)")
    if not any(check == "images" for check, _item in problems):
        print(f"images: all {len(images)} that a page shows are in the publication with the "
              "sha256 of the local file; no other image is published"
              if {name for _ref, name in images} == set(media)
              else "images: MORE than the pages show would be published")
    for check, item_id in problems[:40]:
        print(f"  differs: {check} of {item_id}")
    if problems or {name for _ref, name in images} != set(media):
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


def cmd_init_db(args) -> int:
    store = _store(unpooled=True)
    store.init_schema()
    if args.preview:
        store.mark_preview()
        print("marked as a PREVIEW database (the acceptance seed may write here; never "
              "production)")
    with store.connection() as conn:
        tables = [name for (name,) in conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = current_schema()"
            " ORDER BY tablename").fetchall()]
    print(f"schema applied: {len(tables)} table(s): {', '.join(tables)}")
    return 0


def owner_entry(reviewers: A.Reviewers, place: int | None):
    """The owner's own REVIEWER entry of the reviewers file: the one REVIEWER entry whose login
    is a local identity (no ``@``), or with ``place`` the place-th REVIEWER entry in file order.
    None when the file has no such entry. An APPROVER entry is never one: approval, return and
    freeze have no hosted access."""
    listed = [e for e in reviewers.entries.values() if e.role == A.REVIEWER]
    if place is not None:
        if not 1 <= place <= len(listed):
            raise refused("NO_SUCH_REVIEWER_ENTRY")
        if "@" in listed[place - 1].login:  # a remote reviewer's entry: their REMOTE-n
            raise refused("OWNER_ENTRY_IS_A_REMOTE_REVIEWER_S")
        return listed[place - 1]
    local = [e for e in listed if "@" not in e.login]
    if len(local) > 1:
        raise refused("OWNER_REVIEWER_ENTRY_NOT_ONE")  # name it: --owner-entry N
    return local[0] if local else None


def owner_address(args, *, required: bool) -> str | None:
    """The owner's sign-in address, from a private file (mode 0600) outside the repository and
    outside any synchronised folder: ``--owner-address-file`` or the variable
    HOSTED_OWNER_ADDRESS_FILE. Never printed; stored in the database only."""
    name = getattr(args, "owner_address_file", None) or os.environ.get(OWNER_ADDRESS_ENV)
    if not name:
        if required:
            raise refused("NO_OWNER_ADDRESS_FILE")
        return None
    path = Path(name).expanduser().absolute()
    if path.resolve().is_relative_to(REPO.resolve()):
        raise refused("OWNER_ADDRESS_FILE_INSIDE_THE_REPOSITORY")
    if any(word in part.casefold() for part in path.resolve().parts for word in SYNCED):
        raise refused("OWNER_ADDRESS_FILE_IN_A_SYNCHRONISED_FOLDER")
    try:
        data = V._read_private(path, "owner's address file")
    except FileNotFoundError:
        data = None  # not even its folder
    if data is None:
        raise refused("NO_OWNER_ADDRESS_FILE")
    try:
        address = data.decode("utf-8").strip()
    except UnicodeDecodeError:
        raise refused("OWNER_ADDRESS_FILE_NOT_AN_ADDRESS") from None
    if AUTH.plausible(address) is None:
        raise refused("OWNER_ADDRESS_FILE_NOT_AN_ADDRESS")
    return address


def cmd_sync_reviewers(args) -> int:
    store = _store()
    if args.test_identity:  # a test identity works on the invented bundle, never the real one
        if len(args.test_identity) > 2:
            raise refused("TEST_IDENTITY_IS_ALIAS_AND_ONE_ADDRESS")
        alias, login = (*args.test_identity, None)[:2]
        if login is None:  # the owner's own address, from the private file
            login = owner_address(args, required=True)
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
            if args.demo and not args.force and store.published(demo=False) is not None:
                # It would move the reviewers from the real forms to the invented ones.
                raise refused("REAL_BUNDLE_IS_PUBLISHED")
            if not _scope_fits(bundle, reviewers.scope_sequences):
                raise refused("SCOPE_NOT_PUBLISHED")  # publish-bundle first
            # A row with lines keeps its spelling: the site finds a reviewer's history by it.
            written = {e["alias"] for e in store.events()}
            respelled = [r.alias for r in store.reviewers() if r.alias in written
                         and r.login in reviewers.entries
                         and reviewers.entries[r.login].login != r.login_shown]
            for alias in respelled:
                print(f"{alias}: the file spells this sign-in otherwise than this row, which has "
                      "lines under its spelling; put the earlier spelling back in the file")
            if respelled:
                raise refused("LOGIN_SPELLING_HAS_RECORDS")
            owner = owner_entry(reviewers, args.owner_entry)
            address = owner_address(args, required=False)
            rows = store.sync_reviewers(reviewers, bundle_id=bundle["bundle_id"],
                                        skip=owner.login if owner else None)
            held = next((r for r in store.reviewers() if r.alias == OWNER_REVIEWER), None)
            if held is not None and address and held.address.casefold() != address.casefold():
                # The owner's codes would go elsewhere: said (never the address), and with
                # lines only on purpose.
                print(f"{OWNER_REVIEWER}: sign-in address CHANGED")
                if any(e["alias"] == OWNER_REVIEWER for e in store.events()) \
                        and not args.address_changed:
                    raise refused("ADDRESS_CHANGED_NEEDS_THE_FLAG")
            if owner is not None and (address or held):
                # The owner's REAL reviewer identity: its lines carry the local identity string
                # exactly as the reviewers file spells it; its codes go to the private address.
                rows.append(store.upsert_reviewer(
                    OWNER_REVIEWER, owner.login, name=owner.name, role=A.REVIEWER,
                    access=owner.access if reviewers.remote_full_context_authorized
                    else A.RESTRICTED, scope_sequences=reviewers.scope_sequences,
                    bundle_id=bundle["bundle_id"], address=address or held.address))
            elif owner is not None and not args.without_owner:
                print(f"{OWNER_REVIEWER}: not written: no sign-in address file (--owner-address-"
                      f"file FILE or {OWNER_ADDRESS_ENV}); --without-owner leaves it out on "
                      "purpose")
                raise refused("NO_OWNER_ADDRESS_FILE")
            elif owner is not None:
                print(f"{OWNER_REVIEWER}: left out on purpose (--without-owner): its local lines "
                      "stay unlisted until it is written and import-log runs")
    shared = {}
    for r in store.reviewers():
        shared[r.sign_in] = shared.get(r.sign_in, 0) + 1
    for r in rows:
        print(f"{r.alias}: id {r.id}, {r.role}, {r.access}, scope {r.scope_sequences}, bundle "
              f"{r.bundle_id[:12]}" + (" (demonstration data)" if args.demo or r.is_test else "")
              + (", test identity" if r.is_test else "") + (", REVOKED" if r.revoked else "")
              + (f"; its sign-in address opens {shared[r.sign_in]} identities"
                 if shared.get(r.sign_in, 0) > 1 else ""))
    print(f"{len(rows)} reviewer row(s) written; none deleted")
    for alias in store.closed_rows():
        print(f"{alias}: closed until import-log --cutover runs (no save is accepted before)")
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


def cmd_unlock(args) -> int:
    """Clear a reviewer's sign-in counters: the wrong codes of the day (ten lock the sign-in for
    24 hours) and the hour's code requests."""
    store = _store()
    with store.connection():
        row = next((r for r in store.reviewers() if r.alias == args.alias), None)
        if row is None:
            raise refused("UNKNOWN_ALIAS")
        key = AUTH.key_of(store, row)
        shared = len(store.identities_at(row.sign_in))
        cleared = AUTH.unlock(store, key)
    print(f"{args.alias}: sign-in unlocked; {cleared} counted event(s) cleared"
          + (f" (the counters belong to its sign-in address, which opens {shared} identities)"
             if shared > 1 else ""))
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


def own_anchor(store, lines: list[bytes], anchor: dict | None) -> None:
    """Refuse an anchor that no import (or restore) of THIS log wrote: one of the acceptance
    seed, of an earlier version, or of another folder's log, whose first ``count`` lines do not
    hash to the anchor's snapshot sha256 (ANCHOR_NOT_OF_THIS_LOG)."""
    if anchor is None:
        return
    prefix = b"".join(line + b"\n" for line in lines[:anchor["count"]])
    if store.anchor_writer() not in ("IMPORT", "RESTORE") or len(lines) < anchor["count"] \
            or sha256(prefix) != anchor["file_sha256"]:
        raise refused("ANCHOR_NOT_OF_THIS_LOG")


OWN_ALIAS = re.compile(r"^(REMOTE-[0-9]+|OWNER-REVIEWER)$")  # what sync-reviewers writes


def strangers(events: list[dict], rows, listed) -> int:
    """HOSTED lines of reviewer rows that are not entries of the reviewers file (an invented
    tester of a preview, a row of another folder): such a database is not this cutover's. A
    REVOKED row of the file's own kind (``REMOTE-n``, ``OWNER-REVIEWER``) is an offboarded
    reviewer: their lines are theirs and come home like anyone's. Each other row is named with
    its count and the remedy; returns the number of such lines."""
    found = 0
    for r in rows:
        if r.is_test or r.login in listed or (r.revoked and OWN_ALIAS.match(r.alias)):
            continue
        n = sum(e["origin"] == HOSTED and e["alias"] == r.alias for e in events)
        if n:
            found += n
            print(f"{r.alias}: {n} hosted line(s) of a row that is not an entry of the reviewers "
                  "file: put its entry back (a reviewer who leaves is revoked; the entry stays)"
                  + ("" if OWN_ALIAS.match(r.alias) else "; this row was not written from "
                     "this file: start from an empty database"))
    return found


def anchored(lines: list[bytes], anchor: dict | None) -> None:
    """Refuse a folder whose log is not the log the hosted chain continues: it must hold the
    anchored head at the anchored line count. Without an anchor (nothing imported yet) only a
    log that holds nothing but its own pulled copies of hosted lines is that log (an empty log,
    or one a previous pull wrote)."""
    if anchor is None and any(_pulled(_record(line)) is None for line in lines):
        raise refused("NO_ANCHOR_IMPORT_LOG_FIRST")
    if not continues(lines, anchor):
        raise refused("LOCAL_LOG_IS_NOT_THE_ANCHORED_LOG")


def _spelled(rec: dict) -> str:
    """A record's ``annotator.login`` exactly as spelled ("" when it has none)."""
    login = (rec.get("annotator") or {}).get("login")
    return login if isinstance(login, str) else ""


def _login(rec: dict) -> str:
    return _spelled(rec).casefold()


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
    if log.count > MAX_LOCAL_LINES:
        raise refused("LOCAL_LOG_TOO_LONG")
    local = A.load_bundle(base / "bundle", A.RESTRICTED)
    listed = A.load_reviewers(base / "reviewers.toml").entries
    if not lines and not args.allow_empty_log:
        raise refused("EMPTY_LOCAL_LOG")  # a preview only: --allow-empty-log
    if args.cutover:  # say where it writes before it writes, and never the wrong pair
        demo, preview = local.manifest.get("product_commit") == "demo", store.is_preview()
        print(f"cutover: {'demonstration' if demo else 'real'} bundle into a database "
              f"{'marked preview' if preview else 'not marked preview'}")
        if demo and not preview:
            raise refused("CUTOVER_DEMO_BUNDLE_ON_A_REAL_DATABASE")
        if preview and not demo:
            raise refused("CUTOVER_REAL_BUNDLE_ON_A_PREVIEW_DATABASE")
    with store.exclusive():
        events, anchor = store.events(), store.anchor()
        if strangers(events, store.reviewers(), listed):
            raise refused("HOSTED_LINES_OF_ROWS_NOT_IN_THE_FILE")
        legacy = {e["seq"]: e["line"] for e in events if e["origin"] == LEGACY}
        # The reviewers' hosted lines (a test identity's are a stream of their own: TEST).
        hosted = {e["sha256"] for e in events if e["origin"] == HOSTED}
        if hosted and anchor is None:  # a chain that continues no log: start from an empty database
            raise refused("HOSTED_LINES_BEFORE_FIRST_IMPORT")
        if not continues(lines, anchor) or any(
                seq > len(lines) or lines[seq - 1] != line for seq, line in legacy.items()):
            raise refused("IMPORTED_PREFIX_DIFFERS")
        own_anchor(store, lines, anchor)
        rows = {r.login: r for r in store.reviewers() if not r.is_test}
        # One spelling per reviewer: the log's, the row's and the file's. The site finds a
        # reviewer's history by the exact spelling, so a line spelled otherwise would be lost.
        spellings = {key: {_spelled(r) for r in log.records if _login(r) == key} for key in rows}
        wrong = [row.alias for key, row in rows.items() if not spellings[key] <= {
            row.login_shown} or (key in listed and listed[key].login != row.login_shown)]
        for row in rows.values():
            print(f"{row.alias}: {len(spellings[row.login])} spelling(s) of its sign-in among "
                  "its local lines" + (": NOT the one spelling of its row and of the reviewers "
                                       "file" if row.alias in wrong else ""))
        if wrong:
            raise refused("LOGIN_SPELLED_OTHERWISE")
        writers = {e["alias"] for e in events if e["origin"] == HOSTED}
        new = []
        for n, (line, rec) in enumerate(zip(lines, log.records, strict=True), 1):
            row = rows.get(_login(rec))
            if row is None or n in legacy or _pulled(rec) in hosted:
                continue  # not a remote reviewer's, already imported, or a hosted save pulled back
            if row.alias in writers:  # a row that saved on the site: nothing goes behind it
                raise refused("LOCAL_SAVE_AFTER_HOSTED_SAVES" if n > (
                    anchor["count"] if anchor else 0) else "LEGACY_AFTER_HOSTED_LINES")
            try:
                line.decode("utf-8")
            except UnicodeDecodeError:
                raise refused("LINE_NOT_UTF8") from None
            new.append(S.EventLine(row.id, LEGACY, line))
        # Local reviewer lines that no row holds: only a count the operator names is accepted,
        # as by parity; otherwise nothing is imported (UNLISTED_LOCAL_LINES).
        unlisted_note, unlisted_fails = _unlisted_line(unlisted(log.records, rows.values()),
                                                       args.accept_unlisted_lines)
        if unlisted_fails:
            print(unlisted_note)
            raise refused("UNLISTED_LOCAL_LINES")
        folder = snapshot(Path(args.snapshots).expanduser() if args.snapshots
                          else base / SNAPSHOTS, data, head, log.count)
        store.insert_events(new)
        if args.cutover:  # the final import: only it opens the reviewer rows
            store.stamp_imported(row.id for row in rows.values())
        if not hosted:  # until a reviewer has saved on the site, the anchor follows the log
            store.set_anchor(log.count, log.head, sha256(data), writer="IMPORT")
        anchor, events = store.anchor(), store.events()
        on_site = write_reviews_on_the_site(base, events, store.reviewers(),
                                            set(store.closed_rows()), cutover=args.cutover)
    print(f"local log: {log.count} line(s), head {log.head[:12]}, {len(data)} bytes, sha256 "
          f"{sha256(data)}")
    print(f"snapshot: {folder.name}")
    print(f"anchor: {anchor['count']} line(s), head {anchor['head'][:12]}: "
          + (f"FIXED, the reviewers' hosted lines continue it (the local log has {log.count} "
             "line(s) now)" if hosted else "moved to the log's present head"))
    print(f"imported {len(new)} new line(s); {len(legacy)} already imported")
    print(f"reviewers who review on the site, marked in the adjudication folder: {on_site}"
          + (" (cutover: every imported reviewer)" if args.cutover else ""))
    print(unlisted_note)
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


def unlisted(records: list[dict], rows) -> list[str]:
    """The spelling of every local line of a reviewer (role REVIEWER) that no reviewer row holds
    under that exact spelling: a former reviewer no longer in the file, a sign-in spelled
    otherwise, or the owner's reviewer identity before its row is written. Counted, never
    printed."""
    held = {row.login_shown for row in rows if not row.is_test}
    return [_spelled(r) for r in records
            if (r.get("annotator") or {}).get("role") == A.REVIEWER and _spelled(r) not in held]


def _unlisted_line(found: list[str], accepted: int | None) -> tuple[str, bool]:
    """(the line that says how many local reviewer lines no row holds, whether that fails)."""
    n, who = len(found), len(set(found))
    if accepted is not None and accepted == n:
        return (f"local lines of reviewers that no row holds: {n}, of {who} sign-in "
                f"spelling(s): accepted as named (--accept-unlisted-lines {accepted})"), False
    if accepted is not None:
        return (f"local lines of reviewers that no row holds: {n}, of {who} sign-in "
                f"spelling(s): NOT the {accepted} named"), True
    return (f"local lines of reviewers that no row holds: {n}, of {who} sign-in spelling(s)"
            + (": give them a row, or accept exactly that many with --accept-unlisted-lines "
               f"{n}" if n else "")), bool(n)


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
    # A reviewer without a row, lines under another spelling of a sign-in: only a count the
    # operator names explicitly is accepted (re-check R5).
    strays = unlisted(log.records, [row for row, _served, _stored in theirs.values()])
    if strays or args.accept_unlisted_lines is not None:
        line, fails = _unlisted_line(strays, args.accept_unlisted_lines)
        print(line)
        failed = failed or fails
    summary = []
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
        shown = [sum(r.get("phase") == A.SYSTEM_SHOWN for r in side) for side in (mine, hosted)]
        print(f"  reveal history: local {shown[0]}, hosted {shown[1]} marker(s): "
              f"{'the same' if shown[0] == shown[1] and same else 'DIFFERENT'}")
        prog = A.progress(served, hosted, session)
        summary.append(f"{alias}: completed {prog.done} of {prog.total}; next "
                       f"{' '.join(resume[1])}"
                       + ("" if same and resume[0] == resume[1] and not differing
                          else " (DOES NOT AGREE with the local log)"))
        if served.bundle_id != local.bundle_id:
            continue
        # Where EVERY form stands as the site says it, on both sides: a return that is missing
        # or stale on a form that is not the next step shows here.
        sides = [{i: steps[1](bundle, session, records, i) for i in b}
                 for bundle, records in ((local, at_home), (served, stored))]
        apart = [i for i in b if sides[0][i] != sides[1][i]]
        print(f"  the site's state of each form: {len(b) - len(apart)} equal, {len(apart)} "
              "different")
        for item_id in apart:
            print(f"  {item_id}: site state local {sides[0][item_id]}, hosted "
                  f"{sides[1][item_id]}")
        failed = failed or bool(apart)
    for line in summary:  # one line per identity, for the report to the owner
        print(line)
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
        locks = {r.alias: AUTH.locked_until(store, AUTH.key_of(store, r), datetime.now(UTC))
                 for r in store.reviewers()}
        shared = {r.alias: len(store.identities_at(r.sign_in)) for r in store.reviewers()}
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
              f"revoked {_time(r['revoked_at'])}"
              + (f", sign-in address shared by {shared[r['alias']]} identities"
                 if shared[r["alias"]] > 1 else ""))
        for origin in dict.fromkeys(e["origin"] for e in mine):
            print(f"  {origin}: " + _counts(e["phase"] for e in mine if e["origin"] == origin))
        for m in (m for m in mail if m["alias"] == r["alias"]):
            unknown = m["sending_at"] and not m["sent_at"] and not m["result"]
            print(f"  mail {m['kind']}: queued {_time(m['queued_at'])}, sent "
                  f"{_time(m['sent_at'])}, result {m['result'] or '-'}" + (
                      f" (sending began {_time(m['sending_at'])} and its outcome was not "
                      "recorded: it is not sent again by itself; ask the reviewer, then invite "
                      "again)" if unknown else ""))
        if locks[r["alias"]]:  # ten wrong codes within 24 hours
            print(f"  sign-in: locked until {_time(locks[r['alias']])} (unlock lifts it)")
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
    1000001, 1000002, ...; the TEST lines (test identities, and only theirs) as a chain of their
    own from TEST_GENESIS, 3000001, 3000002, ...; and every RETURN as the return of a reading
    after the reveal that the same reviewer has in the store (its seq, and its sha256 as the
    RETURN's previous hash)."""
    out = []
    rows = {e["seq"]: (e, _record(e["line"])) for e in events}
    prev, due = anchor["head"] if anchor else V.GENESIS, HOSTED_BASE + 1
    test_prev, test_due = S.TEST_GENESIS, TEST_BASE + 1
    for e, rec in rows.values():
        if sha256(e["line"]) != e["sha256"] or (rec.get("seq"), rec.get("prev_sha256"), rec.get(
                "schema")) != (e["seq"], e["prev_sha256"], A.ANNOTATION_SCHEMA):
            out.append(f"LINE_DIFFERS_FROM_ITS_ROW at {e['seq']}")
        if (e["origin"] == TEST) != e["is_test"]:
            out.append(f"LINE_NOT_IN_ITS_ROW_S_STREAM at {e['seq']}")
        if e["origin"] == HOSTED:
            if (e["seq"], e["prev_sha256"]) != (due, prev):
                out.append(f"HOSTED_CHAIN_BROKEN at {e['seq']}")
            prev, due = e["sha256"], e["seq"] + 1
        elif e["origin"] == TEST:
            if (e["seq"], e["prev_sha256"]) != (test_due, test_prev):
                out.append(f"TEST_CHAIN_BROKEN at {e['seq']}")
            test_prev, test_due = e["sha256"], e["seq"] + 1
        elif e["origin"] == RETURN:
            local = rec.get("local") if isinstance(rec.get("local"), dict) else {}
            reviewed, reading = rows.get(rec.get("refers_to"), ({}, {}))
            clears = rec.get("decision") == WITHDRAWN  # a clearing line: an odd seq, no notes
            if (e["seq"] - RETURN_BASE, rec.get("phase"), rec.get("decision") in (
                    "RETURN", WITHDRAWN), bool(clears and rec.get("notes"))) != (
                    2 * local.get("seq", 0) + clears, A.APPROVAL, True, False) or (
                    reviewed.get("alias"), reviewed.get("sha256"), reading.get("phase")) != (
                    e["alias"], e["prev_sha256"], A.AFTER_REVEAL):
                out.append(f"RETURN_DOES_NOT_NAME_ITS_READING at {e['seq']}")
    return out


def published_problems(store, base: Path) -> list[str]:
    """Re-check R6: what is stored of the published part, compared again with a fresh cut of the
    local bundle at the stored scope: the sha256 of the four files and of every image, both
    ways. The active bundle of this folder (real, else the demonstration one) is compared; a
    real bundle of another folder is a problem. Counts only."""
    local_id = A.load_bundle(base / "bundle", A.RESTRICTED).bundle_id
    real, demo = store.published(demo=False), store.published(demo=True)
    mine = next((p for p in (real, demo) if p and p["bundle_id"] == local_id), None)
    if mine is None:
        if real is not None:
            print("published files: the active real bundle is not this folder's: NOT compared")
            return ["PUBLISHED_BUNDLE_IS_NOT_THIS_FOLDER_S"]
        print("published files: no active bundle is this folder's: not compared")
        return []
    _full, files, media = subset(base, mine["scope_sequences"])
    stored_files, stored_media = store.published_files(local_id)
    files_differ = sum(a != b for a, b in zip([sha256(f) for f in files], stored_files,
                                              strict=False)) + abs(len(files) - len(stored_files))
    names = set(media) | set(stored_media)
    images_differ = sum(sha256(media[n]) != stored_media.get(n) if n in media else True
                        for n in names)
    print(f"published files of bundle {local_id[:12]} against a fresh cut of the local bundle: "
          f"files {len(files) - files_differ} of {len(files)} equal, images "
          f"{len(names) - images_differ} of {len(names)} equal: "
          f"{'PASS' if not files_differ and not images_differ else 'FAIL'}")
    return ["PUBLISHED_FILE_DIFFERS"] * bool(files_differ) + \
        ["PUBLISHED_IMAGE_DIFFERS"] * bool(images_differ)


def cmd_verify(args) -> int:
    store = _store()
    with store.connection():
        events, anchor = store.events(), store.anchor()
        logins = {r.login for r in store.reviewers() if not r.is_test}
    problems = chain_problems(events, anchor)
    problems += [f"LINE_NOT_SPELLED_AS_ITS_ROW at {e['seq']}" for e in events
                 if e["origin"] != RETURN and S.annotator_login(e["line"]) != e["login_shown"]]
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
    problems += published_problems(store, _base(args))
    for problem in problems[:40]:
        print(f"  {problem}")
    print(f"verify: {'FAIL' if problems else 'PASS'}, {len(problems)} problem(s)")
    return int(bool(problems))


# ------------------------------------------------------------------ export, restore


def cmd_export(args) -> int:
    store = _store()
    with store.connection():
        events, anchor = store.events(), store.anchor()
        closed = set(store.closed_rows())
        # Which rows an import has considered (open for saving), by a hash of their spelling:
        # a restore opens the same rows, and no address is written into the header.
        imported = sorted(sha256(r.login_shown.encode("utf-8")) for r in store.reviewers()
                          if not r.is_test and r.alias not in closed)
    header = {"schema": EXPORT_SCHEMA, "exported_at": _time(datetime.now(UTC)),
              "events": len(events), "imported": imported,
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
        # A line goes to the row whose sign-in is spelled exactly as the line's annotator (a
        # return: to the row of the reading it returns). Never by alias: aliases follow the
        # order of the reviewers file, which may have changed.
        spelled = {r.login_shown: r for r in store.reviewers()}
        owner: dict[int, object] = {}
        for e in events:
            row = owner.get(_record(e["line"]).get("refers_to")) if e["origin"] == RETURN \
                else spelled.get(S.annotator_login(e["line"]))
            if row is None:
                raise refused("NO_ROW_FOR_A_LINE_S_SIGN_IN")  # sync-reviewers first
            if row.is_test != e["is_test"]:
                raise refused("ROW_IS_NOT_THE_SAME_KIND")  # a test identity's lines stay a test's
            owner[e["seq"]] = row
        if anchor:
            store.set_anchor(anchor["count"], anchor["head"], anchor["file_sha256"],
                             anchor["imported_at"], writer="RESTORE")
        store.insert_events([S.EventLine(owner[e["seq"]].id, e["origin"], e["line"],
                                         e["idempotency_key"], e["batch"], e["created_at"])
                             for e in events])
        opened = set(header.get("imported") or ())
        store.stamp_imported(r.id for r in store.reviewers()
                             if sha256(r.login_shown.encode("utf-8")) in opened)
        keys = [k for k in EVENT_KEYS if k != "alias"]
        if [{k: e[k] for k in keys} for e in store.events()] != [
                {k: e[k] for k in keys} for e in events] or store.anchor() != anchor:
            raise refused("RESTORE_DIFFERS")  # rolled back
    print(f"restored {len(events)} event(s): {_counts(e['origin'] for e in events)}; every "
          f"hash and the hosted chain verified; file sha256 {sha256(data)}")
    return 0


# ------------------------------------------------------------------ hosted saves, back home


class _Held(Exception):
    """The local log already holds this hosted line."""


def write_reviews_on_the_site(base: Path, events: list[dict], rows, closed, *,
                              cutover: bool) -> int:
    """The marker the local workspace reads (``adjudication.REVIEWS_ON_THE_SITE_FILE``): the
    logins, as the log spells them, of the reviewers who review on the site, so that a REVIEWER
    save of theirs in the local workspace is refused (REVIEWS_ON_THE_SITE). Those are the rows
    with HOSTED lines, and once the cutover was declared (``import-log --cutover``, remembered in
    the file) every reviewer row an import has opened. Nothing else is in it. Returns the
    number of logins."""
    path = base / A.REVIEWS_ON_THE_SITE_FILE
    held = V._read_private(path, "reviews-on-the-site marker")
    was = json.loads(held) if held else {}
    cutover = cutover or bool(was.get("cutover"))
    writers = {e["alias"] for e in events if e["origin"] == HOSTED}
    logins = sorted({r.login_shown for r in rows if not r.is_test and (
        r.alias in writers or (cutover and r.alias not in closed))} | set(was.get("logins") or ()))
    data = (json.dumps({"schema": A.REVIEWS_ON_THE_SITE_SCHEMA, "cutover": cutover,
                        "logins": logins}, ensure_ascii=False, sort_keys=True) + "\n").encode()
    if held != data:
        with PrivateDir(path, "reviews-on-the-site marker", create=True) as d:
            d.write_atomic(data)
    return len(logins)


def cmd_pull_to_local(args) -> int:
    base, store = _base(args), _store()
    data, _head, log = read_log(base)
    with store.connection():
        events, anchor = store.events(), store.anchor()
        logins = {r.alias: r.login for r in store.reviewers() if not r.is_test}
    anchored(_lines(data), anchor)
    if chain_problems(events, anchor):
        raise refused("HOSTED_STORE_FAILS_VERIFICATION")
    own_anchor(store, _lines(data), anchor)
    listed = A.load_reviewers(base / "reviewers.toml").entries
    with store.connection():
        if strangers(events, store.reviewers(), listed):
            raise refused("HOSTED_LINES_OF_ROWS_NOT_IN_THE_FILE")
    hosted = [e for e in events if e["origin"] == HOSTED]  # never a TEST or RETURN line
    known = {e["sha256"] for e in hosted}
    held_here = {_pulled(r) for r in log.records}
    imported = {e["seq"] for e in events if e["origin"] == LEGACY}
    # A reviewer (the owner's reviewer identity too) who saved LOCALLY after the anchor while
    # hosted saves of theirs are not home yet has two histories: THAT identity's lines are not
    # appended (everybody else's are). A local line the store already holds as LEGACY (imported
    # beyond the anchor before the reviewer saved on the site) is one history, not two.
    since = anchor["count"] if anchor else 0
    blocked = {}
    for alias, login in logins.items():
        waiting = sum(e["alias"] == alias and e["sha256"] not in held_here for e in hosted)
        if waiting and any(_login(r) == login and _pulled(r) is None and n not in imported
                           for n, r in enumerate(log.records[since:], since + 1)):
            blocked[alias] = waiting
    hosted = [e for e in hosted if e["alias"] not in blocked]
    pulled, held = [], 0
    for e in hosted:
        def make(v: V.Verification, e=e) -> dict:
            # Decided HERE, from the log as it is under its lock: since this command read the
            # log another pull may have appended this very line (two runs at once).
            local = {r["hosted"]["seq"]: r["seq"] for r in v.records if _pulled(r) in known}
            if e["seq"] in local:
                raise _Held
            body = {k: x for k, x in json.loads(e["line"]).items() if k not in ENVELOPE}
            if isinstance(body.get("refers_to"), int) and body["refers_to"] > HOSTED_BASE:
                if body["refers_to"] not in local:
                    raise refused("REFERS_TO_A_LINE_NOT_PULLED")
                body["refers_to"] = local[body["refers_to"]]  # the local line of that record
            return {**body, "hosted": {"seq": e["seq"], "sha256": e["sha256"]}}

        try:
            V.append_chained(A.annotations_path(base), WHAT, A.ANNOTATION_SCHEMA, make)
        except _Held:
            held += 1
        else:
            pulled.append(e["alias"])
    after = A.read_annotations(base)
    print(f"pulled {len(pulled)} hosted line(s) into the local log: {_counts(pulled)}; "
          f"{held} already held")
    for alias, waiting in blocked.items():
        print(f"{alias}: NOT pulled: {waiting} hosted line(s); this identity also saved in the "
              "local workspace after the anchor (LOCAL_SAVE_AFTER_HOSTED_SAVES): two histories, "
              "see \"Two histories\" in docs/HOSTED_REVIEWER.md")
    print(f"annotation log: {'OK' if after.ok else 'FAILS ' + str(after.problem)}, "
          f"{after.count} line(s), head {after.head[:12]}")
    with store.connection():
        on_site = write_reviews_on_the_site(base, store.events(), store.reviewers(),
                                            set(store.closed_rows()), cutover=False)
    print(f"reviewers who review on the site, marked in the adjudication folder: {on_site}")
    return 0 if after.ok and not blocked else 1


# ------------------------------------------------------------------ the approver's returns


def return_record(approval: dict, line_sha256: str, reviewed: dict, *,
                  clears: int | None = None) -> dict:
    """The record the hosted store keeps of a local RETURN, for the reviewer whose reading it
    returns: an APPROVAL record of the log's shape, so that ``A.effective_approval`` and
    ``A._outcome`` read it as they read the local one. ``reviewed``: the hosted event of the
    returned reading. It carries the decision, the approver's notes and time, the hosted ``seq``
    of the returned reading (``refers_to``) and that reading's sha256 (``prev_sha256``: it is
    outside the hosted chain); of the approver only the role; and the local line it was made
    from (``local``). Its seq is 2000000 + twice the local line's seq, so the store's order is
    the local log's.

    With ``clears`` (the seq of a pushed return that no longer stands locally): the line that
    clears it, made from the local approval that came after: the same shape WITHOUT content
    (no notes, decision WITHDRAWN, which says nothing of what the approver decided), one seq
    above that approval's place. It is then the latest approval of the item on the site, so
    ``effective_approval`` no longer gives the return."""
    return {"schema": A.ANNOTATION_SCHEMA,
            "seq": RETURN_BASE + 2 * approval["seq"] + (clears is not None),
            "prev_sha256": reviewed["sha256"], "at": approval["at"],
            "bundle_id": approval["bundle_id"], "item_id": approval["item_id"],
            "annotator": {"login": APPROVER_SHOWN, "name": APPROVER_SHOWN.capitalize(),
                          "role": A.APPROVER, "access": A.FULL},
            "phase": A.APPROVAL, "blind": False, "reading": {}, "field_verdicts": {},
            "flags": [], "confidence": "",
            "notes": "" if clears is not None else approval.get("notes") or "",
            "refers_to": reviewed["seq"],
            "decision": WITHDRAWN if clears is not None else "RETURN", "rubric": A.RUBRIC,
            "acknowledged_warnings": [],
            "local": {"seq": approval["seq"], "sha256": line_sha256},
            **({} if clears is None else {"clears": clears})}


def cmd_push_returns(args) -> int:
    base, store = _base(args), _store()
    data, _head, log = read_log(base)
    lines = _lines(data)
    if log.count > MAX_LOCAL_LINES:
        raise refused("LOCAL_LOG_TOO_LONG")
    local = A.load_bundle(base / "bundle", A.RESTRICTED)
    new = A.rubric2(log.records)
    standing = [a for a in (A.effective_approval(new, i, local) for i in local.items)
                if a is not None and A._outcome(a) == "RETURN"]
    with store.exclusive():
        events, anchor = store.events(), store.anchor()
        anchored(lines, anchor)
        if chain_problems(events, anchor):
            raise refused("HOSTED_STORE_FAILS_VERIFICATION")
        own_anchor(store, lines, anchor)
        rows = {r.login: r for r in store.reviewers()
                if not r.is_test and r.bundle_id == local.bundle_id}  # who is served that bundle
        ids = {r.alias: r.id for r in rows.values()}
        by_seq = {e["seq"]: e for e in events}
        by_sha = {e["sha256"]: e for e in events}

        def push(row_id: int, record: dict) -> bool:
            """Store the line unless the store holds it; True when it was stored now."""
            line = V.encode_line(record)
            held = by_seq.get(record["seq"])
            if held is not None and held["sha256"] != sha256(line):
                raise refused("PUSHED_RETURN_DIFFERS")
            if held is None:
                store.insert_events([S.EventLine(row_id, RETURN, line)])
            return held is None

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
            record = return_record(approval, sha256(lines[approval["seq"] - 1]), reviewed)
            if push(row.id, record):
                pushed.append((row.alias, approval["item_id"], record["seq"], reviewed["seq"]))
            else:
                known += 1
        # A pushed return that no longer stands locally (the approver decided again on that
        # reading, or another decision on the item came after) but still shows on the site: no
        # line of the store can be taken back, so ONE clearing line follows it.
        stands = {a["seq"] for a in standing}
        cleared, stale = [], []
        for e in [e for e in events if e["origin"] == RETURN and e["alias"] in ids]:
            rec = json.loads(e["line"])
            if rec.get("decision") != "RETURN" or rec["local"]["seq"] in stands:
                continue
            theirs = [r for r in (json.loads(x["line"]) for x in store.events()
                                  if x["alias"] == e["alias"])
                      if r["bundle_id"] == rec["bundle_id"]]
            showing = A.effective_approval(A.rubric2(theirs), rec["item_id"], local)
            if showing is None or showing["seq"] != e["seq"]:
                continue  # the site no longer shows it (answered, or cleared before)
            later = [r for r in new if r["phase"] == A.APPROVAL and r["seq"] > rec["local"]["seq"]
                     and (r["item_id"], r["bundle_id"]) == (rec["item_id"], rec["bundle_id"])]
            if not later:  # nothing to derive a clearing line from
                stale.append((e["alias"], rec["item_id"], e["seq"]))
                continue
            record = return_record(later[-1], sha256(lines[later[-1]["seq"] - 1]),
                                   by_seq[rec["refers_to"]], clears=e["seq"])
            if push(ids[e["alias"]], record):
                cleared.append((e["alias"], rec["item_id"], record["seq"], e["seq"]))
    print(f"returns that stand in the local log: {len(standing)}; {elsewhere} of a reading that "
          "was not made by a hosted reviewer of this bundle")
    print(f"pushed {len(pushed)} new return(s): {_counts(p[0] for p in pushed)}; {known} already "
          f"stored; {skipped} skipped (the returned reading is not in the hosted store)")
    for alias, item_id, seq, reviewed_seq in pushed:
        print(f"  {alias} {item_id}: return {seq} of the reading {reviewed_seq}")
    print(f"cleared {len(cleared)} pushed return(s) that no longer stand locally: "
          f"{_counts(c[0] for c in cleared)}")
    for alias, item_id, seq, return_seq in cleared:
        print(f"  {alias} {item_id}: line {seq} clears the return {return_seq}")
    for alias, item_id, seq in stale:
        print(f"  NO_LONGER_STANDS_LOCALLY {alias} {item_id}: return {seq} still shows on the "
              "site until that reviewer saves again (no later local decision to clear it with)")
    return 0


# ------------------------------------------------------------------ the site's own role


def _site_url(url: str, role: str, password: str) -> str:
    """The database address with the user and the password replaced."""
    if "://" not in url:
        return make_conninfo(url, user=role, password=password)
    parts = urlsplit(url)
    host = parts.netloc.rpartition("@")[2]
    return urlunsplit(parts._replace(
        netloc=f"{quote(role, safe='')}:{quote(password, safe='')}@{host}"))


def scram_verifier(password: str, salt: bytes | None = None, iterations: int = 4096) -> str:
    """The SCRAM-SHA-256 verifier PostgreSQL stores for ``password`` with ``salt`` (RFC 5802,
    7677). NOT sent to the server: the hosting provider accepts a password in plain text only
    ("Hashed passwords are not supported"; a statement carrying a verifier fails there when the
    transaction ends). Kept to prove that the verifier a server stored belongs to the password
    in the address file. (The password is URL-safe ASCII, which SASLprep leaves as it is.)"""
    import base64
    import hashlib
    import hmac

    salt = salt if salt is not None else secrets.token_bytes(16)
    salted = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    client = hmac.new(salted, b"Client Key", hashlib.sha256).digest()
    server = hmac.new(salted, b"Server Key", hashlib.sha256).digest()

    def b64(data: bytes) -> str:
        return base64.b64encode(data).decode("ascii")

    return (f"SCRAM-SHA-256${iterations}:{b64(salt)}${b64(hashlib.sha256(client).digest())}"
            f":{b64(server)}")


# What a site role must not have; checked, never re-stated: an operator who is not a superuser
# may not name SUPERUSER, REPLICATION or BYPASSRLS in an ALTER ROLE at all, even to deny them.
_ROLE_FACTS = ("SELECT r.rolsuper OR r.rolcreaterole OR r.rolcreatedb OR r.rolbypassrls"
               " OR r.rolreplication OR r.rolinherit OR NOT r.rolcanlogin"
               " OR r.rolname = current_user"
               " OR EXISTS (SELECT 1 FROM pg_class c WHERE c.relowner = r.oid)"
               " OR EXISTS (SELECT 1 FROM pg_namespace n WHERE n.nspowner = r.oid)"
               " OR EXISTS (SELECT 1 FROM pg_auth_members m WHERE m.member = r.oid)"
               " FROM pg_roles r WHERE r.rolname = %s")


def cmd_make_site_role(args) -> int:
    """Create the login role the SITE connects as, give it a new password, or (``--grants-only``)
    give an existing one exactly the present rights again without touching its password: the
    rights the site's code uses and no other, no ownership. A new address goes to a file (mode
    0600), written only once the database has accepted the change; nothing secret is printed.
    The statement carries the password itself, over the direct TLS connection: the hosting
    provider takes no hashed password (seen there: a verifier is refused when the transaction
    ends). It works for an operator that has CREATEROLE and is not a superuser (the hosting
    provider's), and after the schema was dropped and made again."""
    url = os.environ.get(URL_ENV)
    if not SITE_ROLE.match(args.role):
        raise refused("BAD_ROLE_NAME")
    if not url:
        raise refused(f"SET_{URL_ENV}")
    if not args.grants_only and not args.out:
        raise refused("OUT_FILE_NEEDED")  # or --grants-only
    store, role = _store(unpooled=True), sql.Identifier(args.role)
    with store.exclusive() as conn:
        found = conn.execute(_ROLE_FACTS, (args.role,)).fetchone()
        if found and found[0]:  # an administrator, an owner, a member of another role
            raise refused("NOT_A_SITE_ROLE")
        if args.grants_only and not found:
            raise refused("NO_SITE_ROLE")  # make it first: make-site-role --out FILE
        password = None
        if not args.grants_only:
            password = secrets.token_urlsafe(32)
            secret = sql.Literal(password)
            if found:  # only the password: what an operator without superuser may alter
                conn.execute(sql.SQL("ALTER ROLE {} PASSWORD {}").format(role, secret))
            else:
                conn.execute(sql.SQL(
                    "CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT"
                    " NOREPLICATION NOBYPASSRLS PASSWORD {}").format(role, secret))
        schema, database = (sql.Identifier(x) for x in conn.execute(
            "SELECT current_schema(), current_database()").fetchone())
        for statement in ("REVOKE ALL ON ALL TABLES IN SCHEMA {s} FROM {r}",
                          "REVOKE ALL ON ALL SEQUENCES IN SCHEMA {s} FROM {r}",
                          "REVOKE ALL ON SCHEMA {s} FROM {r}",
                          "GRANT CONNECT ON DATABASE {d} TO {r}",
                          "GRANT USAGE ON SCHEMA {s} TO {r}"):
            conn.execute(sql.SQL(statement).format(s=schema, d=database, r=role))

        def grant(what: str, table: str, columns=()) -> None:
            listed = sql.SQL(" ({})").format(sql.SQL(", ").join(map(sql.Identifier, columns))) \
                if columns else sql.SQL("")
            conn.execute(sql.SQL("GRANT {}{} ON {} TO {}").format(
                sql.SQL(what), listed, sql.Identifier(table), role))

        for table in (*SITE_READS, *SITE_OWN):
            grant("SELECT", table)
        for table in (*SITE_INSERTS, *SITE_OWN):
            grant("INSERT", table)
            for sequence in conn.execute(  # the sequences its columns draw from, if any
                    "SELECT n.nspname, q.relname FROM pg_depend d JOIN pg_class q"
                    " ON q.oid = d.objid AND q.relkind = 'S' JOIN pg_namespace n"
                    " ON n.oid = q.relnamespace WHERE d.refobjid = %s::regclass"
                    " AND d.deptype IN ('a', 'i')", (table,)).fetchall():
                conn.execute(sql.SQL("GRANT USAGE ON SEQUENCE {} TO {}").format(
                    sql.Identifier(*sequence), role))
        for table in SITE_OWN:
            grant("UPDATE, DELETE", table)
        for table, columns in SITE_SETS.items():
            grant("UPDATE", table, columns)
    if password is not None:  # only now: the database has accepted the role and its password
        with PrivateDir(args.out, "site database address", create=True) as d:
            d.create(d.name, (_site_url(url, args.role, password) + "\n").encode("utf-8"))
    done = "rights given again, password unchanged" if args.grants_only else (
        "password changed" if found else "created")
    print(f"site role {args.role}: {done}; it owns "
          f"nothing and may: read {', '.join(SITE_READS)}; add to {', '.join(SITE_INSERTS)}; "
          f"use {', '.join(SITE_OWN)}; set "
          + "; ".join(f"{t}.({', '.join(c)})" for t, c in SITE_SETS.items()))
    if password is not None:
        print(f"its database address is in {Path(args.out).name} (mode 0600): the site's "
              "SITE_DATABASE_URL. Nothing of it is printed.")
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

    accept = {"type": int, "metavar": "N", "dest": "accept_unlisted_lines",
              "help": "accept exactly N local reviewer lines that no row holds (the number a "
                      "run without it printed); any other number fails"}
    command("init-db", cmd_init_db, "apply schema.sql (can be run again)").add_argument(
        "--preview", action="store_true",
        help="mark this database as a preview (never production): the acceptance seed's")
    p = command("sync-reviewers", cmd_sync_reviewers,
                "the reviewers file's remote reviewers as REMOTE-<n> rows on the real bundle")
    p.add_argument("--demo", action="store_true",
                   help="bind them to the demonstration bundle instead (a preview database)")
    p.add_argument("--force", action="store_true",
                   help="with --demo: although a real bundle is published in this database")
    p.add_argument("--test-identity", nargs="+", metavar="ALIAS [LOGIN]",
                   help="instead: write the test row TEST-<n> for this sign-in (without LOGIN: "
                        "the owner's address, from the address file), bound to the "
                        "demonstration bundle")
    p.add_argument("--owner-address-file", metavar="FILE",
                   help=f"the private file (0600) holding the owner's sign-in address (default: "
                        f"{OWNER_ADDRESS_ENV})")
    p.add_argument("--address-changed", action="store_true",
                   help="the owner's address file names another address on purpose")
    p.add_argument("--without-owner", action="store_true",
                   help="leave the owner's reviewer identity out on purpose (no address file)")
    p.add_argument("--owner-entry", type=int, metavar="N",
                   help="the owner's reviewer entry is the N-th REVIEWER entry of the file "
                        "(default: the one REVIEWER entry without '@')")
    for name, text in (("revoke", "stop a reviewer's access at once"),
                       ("restore-access", "lift a revocation")):
        command(name, cmd_revoke, text).add_argument("alias")
    command("unlock", cmd_unlock,
            "clear a reviewer's sign-in counters (wrong codes, code requests)").add_argument(
        "alias")
    p = command("invite", cmd_invite, "queue an invitation for the site to send")
    p.add_argument("alias")
    p.add_argument("--kind", required=True, choices=("continue", "start"),
                   help="continue: the site has moved; start: a first access")
    p = command("publish-bundle", cmd_publish_bundle,
                "publish the part of the local bundle the scope needs, after a parity check")
    p.add_argument("--check-only", action="store_true", help="cut and check; upload nothing")
    p.add_argument("--demo", metavar="DIR", help="publish the invented demo folder DIR instead")
    p = command("import-log", cmd_import_log,
                "snapshot the local log and import the remote reviewers' lines")
    p.add_argument("--snapshots", metavar="DIR", help=f"snapshot root (default: <dir>/{SNAPSHOTS})")
    p.add_argument("--accept-unlisted-lines", **accept)
    p.add_argument("--allow-empty-log", action="store_true",
                   help="import an EMPTY local log (a preview only)")
    p.add_argument("--cutover", action="store_true",
                   help="the final import: it alone opens the reviewer rows on the site; from "
                        "now on the local workspace refuses their reviewer saves")
    command("parity", cmd_parity, "compare the local log's state with the hosted store's"
            ).add_argument("--accept-unlisted-lines", **accept)
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
    p = command("make-site-role", cmd_make_site_role,
                "create the site's own database role (or change its password): no ownership")
    p.add_argument("--out", metavar="FILE",
                   help="where the new database address is written (mode 0600)")
    p.add_argument("--grants-only", action="store_true",
                   help="give the existing role the present rights again; its password stays")
    p.add_argument("--role", default="reviewer_site", help="the role's name")
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
