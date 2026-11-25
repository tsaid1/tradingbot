"""The owner's two identities on one sign-in address (DECIDED 40, DESIGN_UX section 6), on INVENTED
data: the owner's REAL reviewer identity (the local owner-reviewer history, alias OWNER-REVIEWER,
whose lines carry the local identity string) and the synthetic TEST identity (the invented
bundle, the TEST stream). The address a code is mailed to is not the identity whose lines are
written; after a right code the address chooses which identity to open; a session belongs to one
identity; nothing of one is reachable from the other. And the operator tool carries the owner's
reviewer history like any reviewer's (import, parity, pull, returns), never the approver's."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest
from hosted_pg import REPO, hosted_reveal, hosted_save
from hosted_web_site import FIRST, ORIGIN, Site, dsn, store  # noqa: F401 - fixtures
from test_hosted_admin import (  # noqa: F401 - fixtures
    FORBIDDEN,
    OWNER,
    TESTER,
    base,
    cli,
    half_way,
    local_save,
)

from tradingbot.runtime import adjudication as A
from tradingbot.runtime import vocab_recommend as V

LOCAL_ID = "Owner.Reviewer"  # the owner's local identity string, as an invented reviewers file spells it
ADDRESS = "Owner.Mail@example.invalid"  # the owner's invented sign-in address
OWNER_R = A.Session(LOCAL_ID, "Owner Reviewer Example", A.REVIEWER, A.FULL, False, 40)
QUIET = (*FORBIDDEN, LOCAL_ID, LOCAL_ID.casefold(), "Owner Reviewer Example", "Owner.Mail",
         "owner.mail")


def owner_entry(base: Path) -> Path:  # noqa: F811
    """The invented reviewers file with the owner's own REVIEWER entry (a local identity, no
    ``@``) beside the approver and the two remote reviewers."""
    path = base / "reviewers.toml"
    path.write_text(path.read_text() + (
        f'\n[[reviewer]]\nlogin = "{LOCAL_ID}"\nname = "Owner Reviewer Example"\n'
        'role = "REVIEWER"\naccess = "FULL"\n'))
    return base


def address_file(directory: Path, text: str = ADDRESS, mode: int = 0o600) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "owner-address"
    path.write_text(text + "\n")
    os.chmod(path, mode)
    return path


@pytest.fixture
def run(cli, monkeypatch, capsys, dsn, base):  # noqa: F811
    """As ``test_hosted_admin.run``, and nothing printed may name the owner's identity string,
    name or address either."""
    monkeypatch.setenv(cli.URL_ENV, dsn)
    monkeypatch.delenv(cli.UNPOOLED_ENV, raising=False)
    monkeypatch.delenv(cli.OWNER_ADDRESS_ENV, raising=False)
    monkeypatch.setenv(A.DIR_ENV, str(base))
    owner_entry(base)

    def run(*argv, code: int = 0) -> str:
        capsys.readouterr()
        got = cli.main([str(a) for a in argv])
        printed = "".join(capsys.readouterr())
        assert not [w for w in (*QUIET, str(base.parent)) if w in printed], printed
        assert got == code, printed
        return printed

    return run


# ------------------------------------------------------------------ the site


class Two:
    """The site with the owner's two identities on one address: OWNER-REVIEWER on a stand-in for
    the real bundle (every id prefixed ``r``/``q``), TEST-1 on the invented one."""

    def __init__(self, tmp_path, admin, dsn):  # noqa: F811
        self.site = site = Site(tmp_path, admin, dsn)
        real = site.publish_real(tmp_path / "demo")
        self.owner = admin.upsert_reviewer("OWNER-REVIEWER", LOCAL_ID, name="Owner Reviewer "
                                           "Example", scope_sequences=40, bundle_id=real.bundle_id,
                                           address=ADDRESS)
        admin.stamp_imported([self.owner.id])  # its local history is imported (none)
        self.test = admin.upsert_reviewer("TEST-1", ADDRESS, name="TEST-1", scope_sequences=40,
                                          is_test=True, bundle_id=site.bundle.bundle_id)
        self.real = real
        self.app = site.production()  # a reviewer of the real bundle is served in production only

    def code_to(self, browser, address: str = ADDRESS) -> str:
        self.site.sql("DELETE FROM rate_limit WHERE bucket NOT LIKE 'wrong:%%'")
        sent = len(self.site.outbox.sent)
        assert browser.post("/sign-in", {"address": address}).status_code == 200
        assert len(self.site.outbox.sent) == sent + 1
        assert self.site.outbox.sent[-1][0] == ADDRESS  # the address, never the identity string
        return self.site.code()

    def open(self, which: str):
        """A new browser signed in to the identity ``which`` (``review`` or ``test``)."""
        b = self.site.browser(self.app)
        code = self.code_to(b)
        answer = b.post("/sign-in/verify", {"address": ADDRESS.upper(), "code": code})
        assert answer.status_code == 303 and answer.headers["Location"] == "/sign-in/choose"
        assert "__Host-session" not in answer.headers.get("Set-Cookie", "")
        assert b.post("/sign-in/choose", {"identity": which}).status_code == 303
        return b


@pytest.fixture
def two(tmp_path, store, dsn):  # noqa: F811
    return Two(tmp_path, store, dsn)


def sessions(two: Two) -> dict[str, int]:
    return dict(two.site.sql("SELECT r.alias, count(w.*) FROM reviewer r LEFT JOIN web_session w"
                             " ON w.reviewer_id = r.id GROUP BY r.alias"))


def test_after_a_right_code_the_address_chooses_and_the_choice_cannot_be_skipped_or_replayed(
        two):
    site = two.site
    b = site.browser(two.app)
    code = two.code_to(b)
    answer = b.post("/sign-in/verify", {"address": ADDRESS, "code": code})
    assert answer.headers["Location"] == "/sign-in/choose"
    assert b.get("/").status_code == 303  # no session before the choice
    page = b.get("/sign-in/choose").get_data(as_text=True)
    assert "Your review" in page and "Test forms (invented data)" in page
    assert ADDRESS not in page and LOCAL_ID not in page and "OWNER-REVIEWER" not in page
    # Another browser, without the choice's cookie, cannot make it; a made-up cookie neither.
    other = site.browser(two.app)
    assert other.post("/sign-in/choose", {"identity": "review"}).headers["Location"] == "/sign-in"
    other.client.set_cookie("__Host-choice", "x" * 43, domain="reviewer.example.invalid")
    assert other.post("/sign-in/choose", {"identity": "review"}).headers["Location"] == "/sign-in"
    # A choice of something the address does not open uses it up and opens nothing.
    assert b.post("/sign-in/choose", {"identity": "approver"}).headers["Location"] == "/sign-in"
    assert b.get("/").status_code == 303 and sum(sessions(two).values()) == 0
    # A new code, a right choice: one session, of that identity; the same choice again: nothing.
    code = two.code_to(b)
    b.post("/sign-in/verify", {"address": ADDRESS, "code": code})
    kept = b.client.get_cookie("__Host-choice", domain="reviewer.example.invalid").value
    assert b.post("/sign-in/choose", {"identity": "review"}).headers["Location"] == "/"
    assert b.get("/").status_code == 200 and sessions(two) == {
        "REMOTE-2": 0, "REMOTE-3": 0, "OWNER-REVIEWER": 1, "TEST-1": 0}
    replay = site.browser(two.app)
    replay.client.set_cookie("__Host-choice", kept, domain="reviewer.example.invalid")
    assert replay.post("/sign-in/choose", {"identity": "test"}).headers["Location"] == "/sign-in"
    assert sum(sessions(two).values()) == 1
    # The choice lives five minutes.
    late = site.browser(two.app)
    code = two.code_to(late)
    late.post("/sign-in/verify", {"address": ADDRESS, "code": code})
    site.clock.forward(minutes=6)
    assert late.post("/sign-in/choose", {"identity": "test"}).headers["Location"] == "/sign-in"
    assert sum(sessions(two).values()) == 1
    # The first sign-in is noted for the identity that was opened, not for the address.
    assert dict(site.sql("SELECT alias, first_sign_in_at IS NOT NULL FROM reviewer")) == {
        "REMOTE-2": False, "REMOTE-3": False, "OWNER-REVIEWER": True, "TEST-1": False}


def test_nothing_of_one_identity_is_reachable_from_the_other_s_session(two):
    site = two.site
    mine, test = two.open("review"), two.open("test")
    # Each saves; each line goes to its own stream.
    mine.review("ri07", like="i07", after=False)
    test.review("i07", after=False)
    events = {(e["alias"], e["origin"], e["seq"]) for e in site.admin.events()}
    assert ("OWNER-REVIEWER", "HOSTED", 1_000_001) in events
    assert ("TEST-1", "TEST", 3_000_001) in events
    owner_lines = [json.loads(e["line"]) for e in site.admin.events()
                   if e["alias"] == "OWNER-REVIEWER"]
    assert {r["annotator"]["login"] for r in owner_lines} == {LOCAL_ID}  # never the address
    # Pages, forms, images, records: of their own bundle only.
    home = mine.get("/").get_data(as_text=True)
    assert "/form/ri07" in home and not re.search(r"/form/i\d", home)
    assert "/form/i07" in test.get("/").get_data(as_text=True)
    assert "/form/r" not in test.get("/").get_data(as_text=True)
    for b, foreign in ((mine, "i07"), (test, "ri07")):
        assert b.get(f"/form/{foreign}").status_code == 404
        assert b.get(f"/form/{foreign}/state").status_code in (404, 405)
    assert mine.get(f"/media/{site.bundle.bundle_id}/p01/chart-p01.png").status_code == 404
    assert test.get(f"/media/{two.real.bundle_id}/qp01/real-chart-p01.png").status_code == 404
    assert test.get(f"/media/{site.bundle.bundle_id}/p01/chart-p01.png").status_code == 200
    assert mine.get(f"/media/{two.real.bundle_id}/qp01/real-chart-p01.png").status_code == 200
    # The owner's saved form shows as saved to the owner only; the test identity's form i07
    # shows its own independent reading, and the owner's form ri07 is not "done" for TEST-1.
    assert "Your independent reading of this form is saved" in mine.open("ri07").text or \
        mine.open("ri07").has_form
    owner_records = site.admin.records_of(two.owner)
    test_records = site.admin.records_of(two.test)
    assert {r["annotator"]["login"] for r in owner_records} == {LOCAL_ID}
    assert {r["annotator"]["login"] for r in test_records} == {ADDRESS}
    # A page key of one identity is no key for the other.
    key = site.page_key("TEST-1", "i08")
    mine.open("ri08")
    assert mine.post("/form/ri08/save", [*mine.shown.data(), ("_key", key)]).status_code in (
        400, 422)


def test_the_wrong_code_lock_and_unlock_act_on_the_address_and_revocation_on_one_identity(
        two, tmp_path, cli, monkeypatch, capsys, dsn):  # noqa: F811
    from reviewer_web import auth

    site = two.site
    owner_session = two.open("review")
    b = site.browser(two.app)
    for _ in range(2):
        code = two.code_to(b)
        for n in range(1, 6):
            wrong = f"{(int(code) + n) % 10 ** 6:06d}"
            assert b.post("/sign-in/verify", {"address": ADDRESS, "code": wrong}).status_code == 401
    key = min(two.owner.id, two.test.id)
    assert auth.locked_until(site.store, key, site.clock()) is not None
    sent = len(site.outbox.sent)
    site.sql("DELETE FROM rate_limit WHERE bucket LIKE 'to:%%' OR bucket LIKE 'net:%%'")
    b.post("/sign-in", {"address": ADDRESS})
    assert len(site.outbox.sent) == sent  # locked for both identities
    assert owner_session.get("/").status_code == 200  # an open session keeps working
    monkeypatch.setenv(cli.URL_ENV, dsn)
    capsys.readouterr()
    assert cli.main(["unlock", "TEST-1"]) == 0
    assert "opens 2 identities" in capsys.readouterr().out
    assert auth.locked_until(site.store, key, site.clock()) is None
    # Revoking the test identity ends its sessions, not the owner's; the address then opens
    # its one remaining identity without a choice.
    test_session = two.open("test")
    assert cli.main(["revoke", "TEST-1"]) == 0
    assert test_session.get("/").status_code == 303 and owner_session.get("/").status_code == 200
    c = site.browser(two.app)
    code = two.code_to(c)
    answer = c.post("/sign-in/verify", {"address": ADDRESS, "code": code})
    assert answer.headers["Location"] == "/" and "/form/ri07" in c.get("/").get_data(as_text=True)
    # An APPROVER row is never given hosted access: with the owner's row an approver, the
    # address opens nothing but what may sign in.
    assert cli.main(["restore-access", "TEST-1"]) == 0
    site.set_reviewer("OWNER-REVIEWER", role=A.APPROVER, bundle_id=two.real.bundle_id)
    assert owner_session.get("/").status_code == 303
    d = site.browser(two.app)
    code = two.code_to(d)
    assert d.post("/sign-in/verify", {"address": ADDRESS, "code": code}).headers[
        "Location"] == "/"  # straight to the test identity: there is nothing to choose
    assert "/form/i07" in d.get("/").get_data(as_text=True)


def test_no_hosted_identity_can_approve_return_or_freeze(two):
    site = two.site
    mine = two.open("review")
    mine.review("ri07", like="i07")
    mine.open("ri07")
    data = [(n, A.APPROVAL if n == "_phase" else v) for n, v in mine.shown.data()]
    assert mine.post("/form/ri07/save", [*data, ("outcome", "ACCEPT")]).status_code == 400
    owner = site.reviewers["OWNER-REVIEWER"]
    after = [r for r in site.admin.records_of(owner) if r["phase"] == A.AFTER_REVEAL][-1]
    with pytest.raises(A.AdjudicationError) as caught:
        site.store.append(owner, lambda _records: {**after, "phase": A.APPROVAL,
                                                   "decision": "RETURN"})
    assert caught.value.code == "PHASE_NOT_WRITTEN_BY_THE_SITE"
    for path in ("/approve", "/freeze", "/form/ri07/approve", "/form/ri07/return"):
        assert mine.get(path).status_code == 404


# ------------------------------------------------------------------ the operator tool


def test_sync_reviewers_writes_the_owner_s_reviewer_identity_from_the_private_address_file(
        run, store, base, tmp_path, monkeypatch, cli):  # noqa: F811
    run("init-db")
    run("publish-bundle", "--demo", base)
    out = run("sync-reviewers", "--demo", code=2)
    assert "OWNER-REVIEWER: not written: no sign-in address file" in out
    assert out.endswith("refused: NO_OWNER_ADDRESS_FILE\n")
    assert "OWNER-REVIEWER" not in {r.alias for r in store.reviewers()}
    secret = address_file(tmp_path / "keys")
    out = run("sync-reviewers", "--demo", "--owner-address-file", secret)
    assert re.search(r"^OWNER-REVIEWER: id \d+, REVIEWER, FULL, scope 40, bundle ", out, re.MULTILINE)
    owner = {r.alias: r for r in store.reviewers()}["OWNER-REVIEWER"]
    assert (owner.login_shown, owner.address, owner.role) == (LOCAL_ID, ADDRESS, A.REVIEWER)
    # The test identity on the same address: from the same file, nobody types it.
    monkeypatch.setenv(cli.OWNER_ADDRESS_ENV, str(secret))
    out = run("sync-reviewers", "--test-identity", "TEST-1")
    assert "its sign-in address opens 2 identities" in out
    test = {r.alias: r for r in store.reviewers()}["TEST-1"]
    assert (test.login_shown, test.sign_in, test.is_test) == (ADDRESS, ADDRESS.casefold(), True)
    # Without the file the row stays as it is (its address is kept); it is never deleted.
    monkeypatch.delenv(cli.OWNER_ADDRESS_ENV)
    run("sync-reviewers", "--demo")  # the row exists: it keeps its address
    assert {r.alias: r for r in store.reviewers()}["OWNER-REVIEWER"].address == ADDRESS
    # The approver never gets a row: no REVIEWER entry of the file is an approver's.
    assert {r.role for r in store.reviewers()} == {A.REVIEWER}
    assert run("sync-reviewers", "--demo", "--owner-entry", "9", code=2) == \
        "refused: NO_SUCH_REVIEWER_ENTRY\n"
    # The file: private, outside the repository and outside a synchronised folder, an address.
    for path, code in ((REPO / "owner-address", "OWNER_ADDRESS_FILE_INSIDE_THE_REPOSITORY"),
                       (tmp_path / "Dropbox" / "owner-address",
                        "OWNER_ADDRESS_FILE_IN_A_SYNCHRONISED_FOLDER"),
                       (address_file(tmp_path / "bad", "not an address"),
                        "OWNER_ADDRESS_FILE_NOT_AN_ADDRESS"),
                       (tmp_path / "none" / "owner-address", "NO_OWNER_ADDRESS_FILE")):
        assert run("sync-reviewers", "--demo", "--owner-address-file", path, code=2) == \
            f"refused: {code}\n"
    loose = address_file(tmp_path / "loose", mode=0o644)
    assert run("sync-reviewers", "--demo", "--owner-address-file", loose, code=2).startswith(
        "refused: ")
    # Another real identity on the owner's address is refused.
    with pytest.raises(A.AdjudicationError) as caught:
        store.upsert_reviewer("REMOTE-9", "another@example.invalid", address=ADDRESS,
                              scope_sequences=40)
    assert caught.value.code == "ADDRESS_HELD_BY_ANOTHER_IDENTITY"


def owner_cutover(run, store, base, tmp_path) -> dict:  # noqa: F811
    """The legacy log half way, with the owner's own reviewer history in it (an independent
    reading, the reveal and the reading after it of i07, and an independent reading of i08) and
    the approver's acceptance of that reading; then the cutover with the owner's identity and a
    test identity on the owner's address."""
    half_way(base)  # lines 1 to 8
    local_save(base, OWNER_R, "i07", A.INDEPENDENT)  # 9
    A.mark_system_shown(base, A.load_bundle(base / "bundle", A.FULL), OWNER_R, "i07")  # 10
    local_save(base, OWNER_R, "i07", A.AFTER_REVEAL)  # 11
    A.append_annotation(base, A.load_bundle(base / "bundle", A.FULL), OWNER, item_id="i07",
                        phase=A.APPROVAL, blind=False, reading={}, refers_to=11,
                        outcome="ACCEPT")  # 12: the approver's line never leaves the Mac
    local_save(base, OWNER_R, "i08", A.INDEPENDENT)  # 13
    secret = address_file(tmp_path / "keys")
    run("init-db", "--preview")  # a rehearsal on the invented folder
    run("publish-bundle", "--demo", base)
    run("sync-reviewers", "--demo", "--owner-address-file", secret)
    run("sync-reviewers", "--test-identity", "TEST-1", "--owner-address-file", secret)
    out = run("import-log")
    from test_hosted_admin import open_rows

    open_rows(store)
    assert "OWNER-REVIEWER: lines 4 (4 new); forms 45: DONE 1, INDEPENDENT_SAVED 1, OPEN 43" in out
    return {r.alias: r for r in store.reviewers()}


def test_the_owner_s_reviewer_history_is_imported_verbatim_compared_pulled_and_approved_locally(
        run, store, base, tmp_path):  # noqa: F811
    rows = owner_cutover(run, store, base, tmp_path)
    owner, test = rows["OWNER-REVIEWER"], rows["TEST-1"]
    lines = A.annotations_path(base).read_bytes().splitlines()
    imported = {e["seq"]: e["line"] for e in store.events() if e["alias"] == "OWNER-REVIEWER"}
    assert imported == {n: lines[n - 1] for n in (9, 10, 11, 13)} | {}  # verbatim, no approval
    assert 12 not in {e["seq"] for e in store.events()}
    out = run("parity")
    assert "OWNER-REVIEWER: completed 1 of 45; next i08 AFTER_REVEAL_DUE" in out
    assert out.splitlines()[-1] == "parity of the local log and the hosted store: PASS, 3 " \
        "reviewer(s)"
    assert "verify: PASS" in run("verify")
    status = run("status")
    assert "OWNER-REVIEWER: bundle " in status and "sign-in address shared by 2 identities" in status
    anchor = store.anchor()
    # The owner reviews on the site; the test identity tests beside it.
    hosted_reveal(store, owner, "i08")
    hosted_save(store, owner, "i08", A.AFTER_REVEAL)
    hosted_save(store, test, "i07", A.INDEPENDENT)
    hosted_reveal(store, test, "i07")
    events = store.events()
    assert [(e["seq"], e["alias"]) for e in events if e["origin"] == "HOSTED"] == [
        (1_000_001, "OWNER-REVIEWER"), (1_000_002, "OWNER-REVIEWER")]
    assert {e["alias"] for e in events if e["origin"] == "TEST"} == {"TEST-1"}
    assert store.anchor() == anchor
    out = run("parity", code=1)
    assert "OWNER-REVIEWER: bundle" in out and "hosted lines not pulled: 2" in out
    assert "TEST-1" not in out
    assert run("pull-to-local").startswith(
        "pulled 2 hosted line(s) into the local log: OWNER-REVIEWER 2; 0 already held")
    log = A.read_annotations(base)
    assert ADDRESS not in A.annotations_path(base).read_text()  # no line of the test identity
    assert "PASS" in run("parity")
    # The approver decides locally on the pulled reading; the existing tools accept the log.
    pulled = [r for r in log.records if r["annotator"]["login"] == LOCAL_ID
              and r["phase"] == A.AFTER_REVEAL][-1]
    b = A.load_bundle(base / "bundle", A.FULL)
    A.append_annotation(base, b, OWNER, item_id="i08", phase=A.APPROVAL, blind=False,
                        reading={}, refers_to=pulled["seq"], outcome="RETURN", notes="again")
    assert A.read_annotations(base).ok
    out = run("push-returns")
    assert "OWNER-REVIEWER i08: return " in out
    assert "  next step: local i08 RETURNED, hosted i08 RETURNED: the same" in run("parity")
    # Export: each identity's lines under its own alias; the test identity's marked as such.
    exported = tmp_path / "out" / "events.jsonl"
    exported.parent.mkdir(mode=0o700)
    run("export", "--out", exported)
    rows_out = [json.loads(x) for x in exported.read_text().splitlines()[1:]]
    assert {(r["alias"], r["is_test"]) for r in rows_out if r["origin"] == "TEST"} == {
        ("TEST-1", True)}
    assert all(json.loads(r["line"])["annotator"]["login"] == LOCAL_ID for r in rows_out
               if r["alias"] == "OWNER-REVIEWER" and r["origin"] != "RETURN")


def test_a_local_reviewer_save_of_the_owner_after_hosted_saves_is_refused(
        run, store, base, tmp_path):  # noqa: F811
    rows = owner_cutover(run, store, base, tmp_path)
    owner = rows["OWNER-REVIEWER"]
    hosted_reveal(store, owner, "i08")  # a hosted line of the owner's, not pulled yet
    local_save(base, OWNER_R, "i01", A.INDEPENDENT)  # and a local save by habit
    assert run("import-log", code=2).endswith("refused: LOCAL_SAVE_AFTER_HOSTED_SAVES\n")
    assert "OWNER-REVIEWER: NOT pulled: 1 hosted line(s)" in run("pull-to-local", code=1)
    assert len(A.read_annotations(base).records) == 14  # nothing appended


def test_the_test_identity_s_lines_never_reach_the_chain_the_anchor_or_a_pull(
        run, store, base, tmp_path):  # noqa: F811
    rows = owner_cutover(run, store, base, tmp_path)
    anchor = store.anchor()
    for item in ("i07", "i08"):
        hosted_save(store, rows["TEST-1"], item, A.INDEPENDENT)
    assert not [e for e in store.events() if e["origin"] == "HOSTED"]
    assert store.anchor() == anchor
    assert run("pull-to-local").startswith("pulled 0 hosted line(s)")
    assert "TEST-1" not in run("parity")
    assert "verify: PASS" in run("verify")
    before = V.verify_bytes(A.annotations_path(base).read_bytes(), A.ANNOTATION_SCHEMA).count
    assert before == 13


def test_before_the_first_import_a_reviewer_is_told_calmly_and_the_test_identity_works(two):
    """Re-check R1 on the pages: with no local log imported (no anchor) a reviewer's identity is
    served one calm page and can store nothing; the test identity tests production as before."""
    from reviewer_web import app as web

    site = two.site
    site.sql("DELETE FROM chain_anchor")
    mine, test = two.open("review"), two.open("test")
    page = mine.get("/")
    assert page.status_code == 503 and web.NOT_OPEN in page.get_data(as_text=True)
    assert mine.get("/form/ri07").status_code == 503
    test.review("i07", after=False)
    assert {e["origin"] for e in site.admin.events()} == {"TEST"}
