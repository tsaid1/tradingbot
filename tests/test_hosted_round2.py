"""Regression tests of the second independent review (orchestration/reviews/
HOSTED_REVIEWER_review_round2.md, findings Q1 to Q4 and M1 to M4), each asserting the finding
itself on the reviewer's reproduction (``HOSTED_REVIEWER_review_round2_probes``). INVENTED data
on a real Postgres."""

from __future__ import annotations

import json

import pytest
from hosted_pg import hosted_reveal, hosted_save, reading_kw
from hosted_web_site import dsn, store  # noqa: F401 - fixtures
from test_hosted_admin import ONE, base, cli, half_way, local_save  # noqa: F401 - fixtures
from test_hosted_identities import OWNER_R, address_file, run  # noqa: F401 - fixtures

from tradingbot.runtime import adjudication as A
from tradingbot.runtime import vocab_recommend as V


def owner_history(base) -> None:  # noqa: F811
    """The legacy log half way (lines 1 to 8), then the owner's own reading of i07: independent
    (9), the reveal (10), the reading after it (11)."""
    half_way(base)
    local_save(base, OWNER_R, "i07", A.INDEPENDENT)
    A.mark_system_shown(base, A.load_bundle(base / "bundle", A.FULL), OWNER_R, "i07")
    local_save(base, OWNER_R, "i07", A.AFTER_REVEAL)


# ------------------------------------------------------------------ Q1


def test_q1_a_row_written_after_the_first_import_is_closed_until_import_log_considers_it(
        run, store, base, tmp_path):  # noqa: F811
    """Q1 (probe ``test_owner_row_written_after_the_first_import_serves_an_empty_history``): the
    owner's row written after the first import was open at once with an empty history; an
    independent reading of i07, whose system reading the owner had seen locally, was stored as
    blind, and a second import put the local lines behind it for good."""
    from reviewer_web import forms as F

    owner_history(base)
    run("init-db")
    run("publish-bundle", "--demo", base)
    # Without the owner's address file sync-reviewers fails: nothing is written.
    out = run("sync-reviewers", "--demo", code=2)
    assert "OWNER-REVIEWER: not written" in out and out.endswith("refused: NO_OWNER_ADDRESS_FILE\n")
    assert store.reviewers() == []
    out = run("sync-reviewers", "--demo", "--without-owner")
    assert "OWNER-REVIEWER: left out on purpose" in out
    assert "REMOTE-2: closed until import-log runs" in out
    run("import-log", "--accept-unlisted-lines", "3")
    secret = address_file(tmp_path / "keys")
    out = run("sync-reviewers", "--demo", "--owner-address-file", secret)
    assert "OWNER-REVIEWER: closed until import-log runs" in out
    assert "REMOTE-2: closed" not in out  # considered by the import
    owner = {r.alias: r for r in store.reviewers()}["OWNER-REVIEWER"]
    assert not store.is_open(owner)
    with pytest.raises(A.AdjudicationError, match="^NOT_IMPORTED$"):
        hosted_save(store, owner, "i07", A.INDEPENDENT)
    assert not [e for e in store.events() if e["alias"] == "OWNER-REVIEWER"]
    # The next import brings the owner's history first; then the site serves it.
    run("import-log")
    assert store.is_open(owner)
    bundle, session = store.bundle_for(owner), owner.session()
    assert F.step_state(bundle, session, store.records_of(owner), "i07") == F.DONE
    with pytest.raises(A.AdjudicationError, match="^SYSTEM_READING_ALREADY_SEEN$"):
        hosted_save(store, owner, "i07", A.INDEPENDENT)
    assert "PASS" in run("parity")


def test_q1_import_log_never_puts_a_local_line_behind_a_row_s_hosted_lines(
        run, store, base, tmp_path):  # noqa: F811
    """Q1, the second half: a database that already holds a HOSTED line of a row whose local
    lines were never imported (written under the old rule) is refused by import-log
    (LEGACY_AFTER_HOSTED_LINES) and nothing is imported behind it."""
    from reviewer_web.store import EventLine

    owner_history(base)
    run("init-db")
    run("publish-bundle", "--demo", base)
    run("sync-reviewers", "--demo", "--without-owner")
    run("import-log", "--accept-unlisted-lines", "3")
    run("sync-reviewers", "--demo", "--owner-address-file", address_file(tmp_path / "keys"))
    owner = {r.alias: r for r in store.reviewers()}["OWNER-REVIEWER"]
    b = store.bundle_for(owner)
    body = A.build_annotation([], b, owner.session(), **reading_kw(
        b, "i07", [], owner.session(), phase=A.INDEPENDENT))
    line = V.encode_line({**body, "schema": A.ANNOTATION_SCHEMA, "seq": 1_000_001,
                          "prev_sha256": store.anchor()["head"]})
    store.insert_events([EventLine(owner.id, "HOSTED", line)])
    before = store.events()
    assert run("import-log", code=2).endswith("refused: LEGACY_AFTER_HOSTED_LINES\n")
    assert store.events() == before
    assert json.loads(before[-1]["line"])["blind"] is True  # what the old rule let in

