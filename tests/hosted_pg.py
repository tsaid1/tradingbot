"""A real Postgres for the hosted reviewer site's tests (``hosted/reviewer_web``): one embedded
server per test process (``pgserver``), a fresh database per test. Import the fixtures by name::

    from hosted_pg import dsn, store  # noqa: F401 - fixtures

A test that uses them is skipped, with the reason, when ``pgserver`` or ``psycopg`` is missing
(``uv pip install pgserver "psycopg[binary]"``). Invented data only: the helpers below publish the
demo bundle (``scripts/adjudication_demo_bundle.py``) and build readings of its nine first items.
"""

from __future__ import annotations

import importlib.util
import itertools
import sys
import tempfile
from functools import cache
from pathlib import Path

import pytest

from tradingbot.runtime import adjudication as A
from tradingbot.runtime import adjudication_rubric as R

REPO = Path(__file__).resolve().parents[1]
if str(REPO / "hosted") not in sys.path:  # the deployed layout: ``reviewer_web`` is top-level
    sys.path.insert(0, str(REPO / "hosted"))
_spec = importlib.util.spec_from_file_location(
    "adjudication_demo_bundle_hosted", REPO / "scripts" / "adjudication_demo_bundle.py")
DEMO = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(DEMO)
NEEDS = 'the hosted store needs a real Postgres: uv pip install pgserver "psycopg[binary]"'
BUNDLE_FILES = ("manifest.json", "sequences.jsonl", "items.jsonl", "posts.jsonl")
_names = (f"t{n}" for n in itertools.count(1))


@cache
def _server():
    pytest.importorskip("psycopg", reason=NEEDS)
    pgserver = pytest.importorskip("pgserver", reason=NEEDS)
    # Stopped and deleted when the process ends. A short path: the server listens on a socket.
    return pgserver.get_server(tempfile.mkdtemp(prefix="hosted-pg-"), cleanup_mode="delete")


def fresh_database() -> str:
    """The address of a new, EMPTY database (no schema yet) on the embedded server."""
    server, name = _server(), next(_names)
    import psycopg
    from psycopg import sql

    with psycopg.connect(server.get_uri(), autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    return server.get_uri(name)


@pytest.fixture
def dsn() -> str:
    """A fresh, EMPTY database (no schema yet) on the embedded server."""
    return fresh_database()


@pytest.fixture
def store(dsn):
    """An ``AdminStore`` on a fresh database with the schema applied. The web app's own object
    is ``Store(dsn)`` on the same database."""
    from reviewer_web.store import AdminStore

    admin = AdminStore(dsn)
    admin.init_schema()
    return admin


# ------------------------------------------------------------------ invented content


def write_demo(directory: Path, *, remote_full_context: bool = True) -> Path:
    """The invented demo folder (bundle and reviewers file). A hosted reviewer is FULL only when
    the owner's file authorizes remote full context, so the demo file says so here."""
    base = DEMO.write_demo(directory)
    if remote_full_context:
        path = base / "reviewers.toml"
        path.write_text(path.read_text().replace(
            "remote_full_context_authorized = false", "remote_full_context_authorized = true"))
    return base


def bundle_bytes(base: Path) -> tuple[list[bytes], dict[str, bytes]]:
    """(the four files' bytes, {image name: bytes}) of the folder's bundle."""
    root = base / "bundle"
    return ([(root / name).read_bytes() for name in BUNDLE_FILES],
            {p.name: p.read_bytes() for p in sorted((root / "media").iterdir())})


def publish_demo(store, base: Path) -> A.Bundle:
    """Publish the WHOLE demo bundle, flagged as demonstration data, and make it active."""
    files, media = bundle_bytes(base)
    return store.publish_bundle(*files, media, is_demo=True,
                                source_sequences=len(files[1].splitlines()))


def demo_reviewers(store, base: Path) -> dict[str, object]:
    """The demo file's remote reviewers as rows, by alias (REMOTE-2 and REMOTE-3: the demo's
    approver has an ``@`` login too and takes REMOTE-1 by the alias rule, without a row)."""
    return {r.alias: r for r in store.sync_reviewers(A.load_reviewers(base / "reviewers.toml"))}


def reading_kw(bundle: A.Bundle, item_id: str, records: list[dict], session: A.Session, *,
               phase: str, refers_to: int | None = None, confidence: str = "HIGH",
               **changes) -> dict:
    """Keyword arguments for ``A.build_annotation`` / ``A.append_annotation``: a reading of one
    of the demo's nine first items that Save accepts (``changes`` replace answers): INDEPENDENT,
    or AFTER_REVEAL with every verdict pre-filled from the comparison with the system's."""
    from test_adjudication import valid  # the invented readings of those items

    item = bundle.items[item_id]
    reading, verdicts = valid(bundle, item_id, **changes), None
    if phase == A.AFTER_REVEAL:
        ctx = A.form_context(bundle, item, revealed=True)
        if item["kind"] == "INSTRUCTION":
            reading.update(reference_series_right="", execution_instrument_right="")
            state = R.applicability(R.complete(reading, ctx), ctx)
            reading.update({f: "YES" for f in R.MAPPING_FIELDS if state[f].state == R.ASK})
        done = R.complete(A.clean_reading(reading, item, bundle), ctx)
        verdicts = A.prefill_verdicts(done, A.system_reading(bundle, item), item["kind"],
                                      A.compared_fields(bundle, item, done))
        if done.get("levels"):
            verdicts["levels"] = A.prefill_row_verdicts(done["levels"],
                                                        A.system_rows(bundle, item))
    _done, checks, _ctx = A.check_reading(
        bundle, item, A.clean_reading(reading, item, bundle), revealed=phase == A.AFTER_REVEAL,
        records=records, login=session.login, confidence=confidence)
    assert not checks.blocking, checks.blocking
    return {"item_id": item_id, "phase": phase, "blind": True, "reading": reading,
            "field_verdicts": verdicts, "refers_to": refers_to, "confidence": confidence,
            "acknowledged_warnings": checks.warnings}


def hosted_save(store, reviewer, item_id: str, phase: str, *, key: str | None = None, **kw):
    """One reading saved through the store as the web app saves it: validated by
    ``A.build_annotation`` under the store's lock. Returns the ``Appended``."""
    bundle, session = store.active_bundle(), reviewer.session()
    if phase == A.AFTER_REVEAL and "refers_to" not in kw:
        kw["refers_to"] = A.latest_own_reading(
            [r for r in store.records_of(reviewer) if r["phase"] == A.INDEPENDENT], item_id,
            bundle, session.login)["seq"]
    args = reading_kw(bundle, item_id, store.records_of(reviewer), session, phase=phase, **kw)
    return store.append(reviewer, lambda records: A.build_annotation(
        records, bundle, session, **args), idempotency_key=key)


def hosted_reveal(store, reviewer, item_id: str):
    """The SYSTEM_SHOWN markers of the item's post, stored before the system's reading is shown."""
    bundle, session = store.active_bundle(), reviewer.session()
    return store.append(reviewer, lambda records: A.build_system_shown(
        records, bundle, session, item_id))
