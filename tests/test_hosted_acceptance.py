"""The invented data of the browser acceptance (``scripts/hosted_acceptance_seed.py``; DESIGN_UX
section 7): one sequence of several posts with a post of two forms, a chart, proposals of both a
price series and an instrument, and a returned reading of the named invented reviewer, put into
place on a database of invented bundles only."""

from __future__ import annotations

import importlib.util

import hosted_pg
import pytest
from hosted_pg import REPO, dsn, store  # noqa: F401 - fixtures

from tradingbot.runtime import adjudication as A


@pytest.fixture(scope="module")
def seeding():
    spec = importlib.util.spec_from_file_location("hosted_acceptance_seed_test",
                                                  REPO / "scripts" / "hosted_acceptance_seed.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_acceptance_sequence_holds_what_the_tester_must_meet(seeding, tmp_path):
    base = seeding.write(tmp_path / "acceptance")
    bundle = A.load_bundle(base / "bundle", A.FULL)
    sid, items = A.ordered(bundle, 40)[0]
    assert sid == seeding.SEQUENCE
    posts = [i["post_ref"] for i in items]
    assert list(dict.fromkeys(posts)) == list(seeding.POSTS)
    assert posts.count("s14") == 2  # one post with two forms (two actions)
    assert bundle.posts["s01"]["media"] == [seeding.CHART]  # one post with a chart
    assert A.read_media(bundle, "s01", seeding.CHART).startswith(b"\x89PNG")
    for item in items:  # every form proposes BOTH a price series and an instrument
        assert item["mapping"]["reference_series"] and \
            item["mapping"]["proposed_execution_instrument"]
    times = [bundle.posts[p]["published_at"] for p in seeding.POSTS]
    assert times == sorted(times)


def test_seed_puts_the_named_reviewer_into_the_acceptance_state(seeding, store, dsn, tmp_path):  # noqa: F811
    from hosted_web_site import ORIGIN
    from reviewer_web import app as web
    from reviewer_web import forms as F
    from reviewer_web import mail
    from reviewer_web import progress as P
    from reviewer_web.store import Store

    store.mark_preview()  # init-db --preview
    found = seeding.seed(store, seeding.write(tmp_path / "a"), "ACCEPT-1")
    assert found["returned"] == "s01a" and found["forms"][0] == "s01a"
    row = {r.alias: r for r in store.reviewers()}["ACCEPT-1"]
    assert not row.is_test and row.login_shown == seeding.LOGIN
    bundle, records = store.bundle_for(row), store.records_of(row)
    assert [r["phase"] for r in records] == [A.INDEPENDENT, A.SYSTEM_SHOWN, A.AFTER_REVEAL,
                                             A.APPROVAL]
    assert records[-1]["decision"] == "RETURN" and records[-1]["notes"] == seeding.NOTE
    assert F.next_step(bundle, row.session(), records) == "s01a"
    dash = P.dashboard(bundle, row.session(), records)
    first = dash["sequences"][0]
    assert (first["id"], first["returned"], first["next_item"], first["total"]) == (
        "scs01", 1, "s01a", 5)
    # The site serves it: the tester signs in and lands on the returned form.
    outbox = mail.Outbox()
    app = web.create_app(Store(dsn), outbox, {"SESSION_SECRET": "x" * 40, "SITE_ENV": "test",
                                              "SIGN_IN_FLOOR_SECONDS": 0})
    client = app.test_client()
    head = {"Origin": ORIGIN, "Sec-Fetch-Site": "same-origin"}
    client.post("/sign-in", data={"address": seeding.LOGIN}, base_url=ORIGIN, headers=head)
    code = outbox.sent[-1][2].split("code is ")[1][:6]
    client.post("/sign-in/verify", data={"address": seeding.LOGIN, "code": code},
                base_url=ORIGIN, headers=head)
    assert client.get("/continue", base_url=ORIGIN).headers["Location"] == "/form/s01a"
    page = client.get("/form/s01a", base_url=ORIGIN).get_data(as_text=True)
    assert f"/media/{bundle.bundle_id}/s01/{seeding.CHART}" in page
    assert "Returned for changes" in client.get("/", base_url=ORIGIN).get_data(as_text=True)
    # Once only, and never on a database that holds a bundle that is not invented.
    with pytest.raises(A.AdjudicationError, match="^ALREADY_SEEDED$"):
        seeding.seed(store, seeding.write(tmp_path / "b"), "ACCEPT-1")


def test_seed_refuses_a_database_that_holds_a_real_bundle(seeding, store, tmp_path):  # noqa: F811
    base = hosted_pg.write_demo(tmp_path / "demo")
    store.mark_preview()
    hosted_pg.publish_as_real(store, base)
    before = (store.events(), [r.alias for r in store.reviewers()], store.anchor())
    with pytest.raises(A.AdjudicationError, match="^NOT_A_DEMO_DATABASE$"):
        seeding.seed(store, seeding.write(tmp_path / "a"), "ACCEPT-1")
    assert (store.events(), [r.alias for r in store.reviewers()], store.anchor()) == before
    assert store.published(demo=True) is None


def test_the_command_prints_ids_and_counts_only(seeding, dsn, monkeypatch, capsys):  # noqa: F811
    from reviewer_web.store import AdminStore

    AdminStore(dsn).init_schema()
    AdminStore(dsn).mark_preview()
    monkeypatch.setenv("HOSTED_DATABASE_URL", dsn)
    assert seeding.main(["seed", "--alias", "ACCEPT-2"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("ACCEPT-2: bundle ") and "@" not in out and "returned: s01a" in out
    assert seeding.main(["seed", "--alias", "ACCEPT-2"]) == 2
    assert capsys.readouterr().err == "refused: ALREADY_SEEDED\n"


def test_several_testers_are_seeded_on_one_preview(seeding, store, tmp_path):  # noqa: F811
    """Each seeded tester's returned reading has its own RETURN line (a second seed failed on
    the preview with a duplicate seq); and each form of the sequence shows every earlier post
    of it (post 3 closes the long that post 2 added to)."""
    from reviewer_web import forms as F

    store.mark_preview()
    for alias in ("ACCEPT-1", "ACCEPT-2"):
        seeding.seed(store, seeding.write(tmp_path / alias), alias)
    returns = [(e["alias"], e["seq"]) for e in store.events() if e["origin"] == "RETURN"]
    assert [a for a, _seq in returns] == ["ACCEPT-1", "ACCEPT-2"] and len(set(returns)) == 2
    for row in store.reviewers():
        records = store.records_of(row)
        assert F.step_state(store.bundle_for(row), row.session(), records, "s01a") == F.RETURNED
    bundle = store.bundle_for(store.reviewers()[0])
    for item in ("s01a", "s05a", "s14a", "s14b", "s18a"):
        post = bundle.items[item]["post_ref"]
        assert bundle.items[item]["context_refs"] == list(
            seeding.POSTS[:seeding.POSTS.index(post)]), item
