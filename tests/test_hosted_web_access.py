"""The hosted reviewer site (``hosted/reviewer_web``): what the server enforces on every route.
A reviewer reaches only the forms and images of their own scope and only their own records; the
response headers; the pages that need no sign-in; the environments. A real Postgres, the invented
demo bundle, its two invented reviewers."""

from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path

import pytest
from hosted_pg import bundle_bytes, write_demo
from hosted_web_site import (  # noqa: F401 - fixtures
    FIRST,
    ORIGIN,
    SECOND,
    SECRET,
    Site,
    dsn,
    site,
    store,
)

from tradingbot.runtime import adjudication as A

HOSTED = Path(__file__).resolve().parents[1] / "hosted"
PNG = b"\x89PNG\r\n\x1a\n"
HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
    "X-Frame-Options": "DENY",
    "X-Robots-Tag": "noindex, nofollow",
}


def image(site, post="p01", name="chart-p01.png", bundle=None) -> str:  # noqa: F811
    return f"/media/{bundle or site.bundle.bundle_id}/{post}/{name}"


def test_a_reviewer_reaches_only_the_forms_of_their_scope(site):  # noqa: F811
    site.set_reviewer(FIRST, scope_sequences=1)  # the first idea in review order: seqB
    a, b = site.browser().sign_in(FIRST), site.browser().sign_in(SECOND)
    home = a.get("/").get_data(as_text=True)
    assert re.findall(r'href="/form/([^"]+)"', home) == ["i07", "i08"]
    assert "1 idea, 2 forms" in " ".join(home.split())
    assert a.get("/form/i07").status_code == 200
    data = {"_csrf": a.csrf(), "_phase": A.INDEPENDENT, "_key": "k" * 24, "confidence": "HIGH"}
    for item in ("i01", "s14a", "i09"):
        assert a.get(f"/form/{item}").status_code == 404
        assert a.post(f"/form/{item}/state", data).status_code == 404
        assert a.post(f"/form/{item}/save", data).status_code == 404
        assert b.get(f"/form/{item}").status_code == 200  # the other reviewer's scope is wider
    assert not site.records(FIRST)
    # An id in a URL is hostile until it is found in the scope.
    for item in ("nothing", "i07%00", "..%2F..%2Fetc%2Fpasswd", "i07/../i01", "I07", "x" * 300,
                 "%E2%80%AE", "i07;DROP TABLE event", "'--", "<script>"):
        answer = a.get(f"/form/{item}")
        assert answer.status_code == 404 and "<script>" not in answer.get_data(as_text=True)
        assert a.post(f"/form/{item}/save", data).status_code in (404, 405)
    # A 404 says nothing: the same page for a form outside the scope and for no form at all.
    assert a.get("/form/i01").get_data(as_text=True) == a.get("/form/nothing").get_data(
        as_text=True)


def test_a_reviewer_never_receives_anything_of_another_reviewers_records(site):  # noqa: F811
    a, b = site.browser().sign_in(FIRST), site.browser().sign_in(SECOND)
    reading_note = "ZQNOTE only the first reviewer wrote this"
    a.open("i07")
    from hosted_web_site import scenario_reading

    a.fill(scenario_reading(site.bundle, "i07")[0])
    a.save(notes=reading_note)
    a.save(notes=reading_note + ", again")
    a.review("s14a", after=False)
    assert len(site.records(FIRST)) == 4 and not site.records(SECOND)
    pages = [b.get("/"), b.get("/form/i07"), b.get("/form/s14a"), b.get("/form/s14b"),
             b.get("/continue"), b.get(f"/form/i07?saved={site.records(FIRST)[0]['seq']}")]
    for page in pages:
        text = page.get_data(as_text=True)
        assert "ZQNOTE" not in text and "Saved." not in text and "holds SHIPPED" not in text
        assert "independent reading saved" not in text and ">done<" not in text
    assert "0 of 45" in pages[0].get_data(as_text=True)
    assert pages[4].headers["Location"] == "/form/i07"
    # The second reviewer's own work starts blind, whatever the first has revealed.
    shown = b.open("i07")
    assert shown.value("_phase") == A.INDEPENDENT and shown.value("timing") == ""
    b.review("i07", after=False)
    assert [r["annotator"]["login"] for r in site.records(SECOND)] == [
        site.reviewers[SECOND].login_shown] * 2
    assert "ZQNOTE" not in b.shown.text
    # Nobody is somebody else by asking: a browser without a session gets the sign-in page.
    nobody = site.browser()
    for path in ("/", "/form/i07", "/continue", "/guide", "/fields"):
        answer = nobody.get(path)
        assert answer.status_code == 303 and answer.headers["Location"] == "/sign-in"
        assert answer.get_data() in (b"", answer.get_data()) and b"Zed" not in answer.get_data()
    assert nobody.post("/form/i07/save", {"_phase": A.INDEPENDENT}).status_code == 303
    assert nobody.post("/sign-out").status_code == 303


def test_an_image_is_served_only_to_a_reviewer_whose_form_shows_its_post(site):  # noqa: F811
    site.set_reviewer(SECOND, scope_sequences=1)  # seqB only: the post p01 is outside
    a, b, nobody = site.browser().sign_in(FIRST), site.browser().sign_in(SECOND), site.browser()
    page = a.get("/form/i01").get_data(as_text=True)
    assert f'<img src="{image(site)}"' in page
    served = a.get(image(site))
    assert served.status_code == 200 and served.get_data().startswith(PNG)
    assert served.headers["Content-Type"] == "image/png"
    assert served.headers["Cache-Control"] == "private, no-store"
    assert served.headers["X-Content-Type-Options"] == "nosniff"
    refused = [
        nobody.get(image(site)),  # signed out
        b.get(image(site)),  # signed in, the post outside the scope
        a.get(image(site, name="chart-p02.png")),  # a name the post does not list
        a.get(image(site, post="p02")),  # a post that does not list the name
        a.get(image(site, post="s11c")),  # a post shown as context only
        a.get(image(site, post="nothing")),
        a.get(image(site, bundle="0" * 64)),  # another bundle
        a.get(image(site, bundle="active")),
        a.get(image(site, name="..%2F..%2Fschema.sql")),
        a.get(image(site, name="chart-p01.png%00.html")),
        a.get(image(site) + "/more"),
    ]
    for answer in refused:
        assert answer.status_code == 404 and PNG not in answer.get_data()
        assert answer.headers["Cache-Control"] == "private, no-store"
    assert len({r.get_data() for r in refused[1:]}) == 1  # one page, whatever the reason
    assert a.post(image(site), {"_csrf": a.csrf()}).status_code == 405
    # Larger than the platform allows, or not an image type: not served.
    site.sql("UPDATE media SET content_type = 'text/html' WHERE name = 'chart-p01.png'")
    assert a.get(image(site)).status_code == 404
    site.sql("UPDATE media SET content_type = 'image/png', bytes = %s WHERE name = "
             "'chart-p01.png'", PNG + b"0" * (4 * 1024 * 1024))
    assert a.get(image(site)).status_code == 404


ROUTES = ("/", "/guide", "/fields", "/form/i07", "/form/nothing", "/continue", "/sign-in",
          "/healthz", "/robots.txt", "/favicon.ico", "/static/app.css", "/static/app.js",
          "/static/nothing.css",
          "/nothing", "/media/x/y/z")


def test_every_response_carries_the_headers_and_nothing_is_kept_by_a_browser(site):  # noqa: F811
    signed, nobody = site.browser().sign_in(FIRST), site.browser()
    answers = [(path, who.get(path)) for who in (signed, nobody) for path in ROUTES]
    answers += [("/media", signed.get(image(site))),
                ("POST /sign-in", nobody.post("/sign-in", {"address": "x@example.invalid"})),
                ("POST verify", nobody.post("/sign-in/verify", {"address": "x", "code": "1"})),
                ("POST refused", signed.post("/sign-out", {}, origin="https://x.invalid")),
                ("POST state", signed.post("/form/i07/state", {"_csrf": signed.csrf(),
                                                               "_phase": A.INDEPENDENT})),
                ("POST fragment", signed.post("/form/i07/state", {
                    "_csrf": signed.csrf(), "_phase": A.INDEPENDENT},
                    headers={"X-Fragment": "form"})),
                ("POST save", signed.post("/form/i07/save", {
                    "_csrf": signed.csrf(), "_phase": A.INDEPENDENT,
                    "_key": site.page_key(FIRST, "i07")})),
                ("POST sign-out", signed.post("/sign-out", {"_csrf": signed.csrf()}))]
    assert {a.status_code for _, a in answers} >= {200, 303, 401, 403, 404, 422}
    for path, answer in answers:
        for name, value in HEADERS.items():
            assert answer.headers.get(name) == value, (path, name)
        policy = answer.headers["Content-Security-Policy"]
        assert "default-src 'self'" in policy and "frame-ancestors 'none'" in policy, path
        assert "unsafe" not in policy and "http" not in policy and "*" not in policy
        assert answer.headers["Strict-Transport-Security"].startswith("max-age=")
        static = path.startswith("/static/") and answer.status_code == 200
        assert answer.headers["Cache-Control"] == ("no-cache" if static else "private, no-store")
        assert "Server" not in answer.headers and "X-Powered-By" not in answer.headers
    # No page loads anything from elsewhere or carries a script or a style of its own.
    signed.sign_in(FIRST)
    for path in ("/", "/guide", "/fields", "/form/i07"):
        text = signed.get(path).get_data(as_text=True)
        assert re.findall(r'(?:src|href|action)="(?!/)[^"]*"', text) in ([], ['#notice'])
        assert "<style" not in text and " style=" not in text and "onclick" not in text
        assert re.findall(r"<script[^>]*>", text) == ['<script src="/static/app.js" defer>']
    assert nobody.get("/robots.txt").get_data(as_text=True) == "User-agent: *\nDisallow: /\n"
    for name in ("app.css", "app.js"):
        text = (HOSTED / "public" / "static" / name).read_text()
        assert "http://" not in text and "https://" not in text and "@import" not in text
        assert "url(" not in text


def test_the_public_folder_holds_the_stylesheet_and_the_script_and_nothing_else():
    files = sorted(p.relative_to(HOSTED / "public").as_posix()
                   for p in (HOSTED / "public").rglob("*") if p.is_file())
    assert files == ["static/app.css", "static/app.js"]
    assert (HOSTED / ".python-version").read_text().strip() == "3.12"
    assert "from reviewer_web.app import app" in (HOSTED / "wsgi.py").read_text()


def test_the_deployed_folder_holds_the_site_and_no_way_around_the_sign_in():
    """The review's finding 14: the development server, the only code with a sign-in bypass,
    sat inside the folder the deployment takes whole."""
    tracked = subprocess.run(["git", "ls-files", "hosted"], cwd=HOSTED.parent, check=True,
                             capture_output=True, text=True).stdout.split()
    code = sorted(f for f in tracked if f.endswith(".py"))
    assert code == ["hosted/reviewer_web/__init__.py", "hosted/reviewer_web/app.py",
                    "hosted/reviewer_web/auth.py", "hosted/reviewer_web/forms.py",
                    "hosted/reviewer_web/mail.py", "hosted/reviewer_web/store.py",
                    "hosted/wsgi.py"]
    for name in tracked:
        text = (HOSTED.parent / name).read_text()
        for word in ("auto-sign-in", "auto_sign_in", "dev_seed", "run_simple", "pgserver"):
            assert word not in text, (name, word)
    assert (HOSTED.parent / "scripts" / "hosted_dev_server.py").is_file()


def test_every_package_the_site_installs_is_pinned_to_the_version_the_tests_run_on():
    """The review's finding 14: psycopg was a range and Flask's own dependencies were not
    listed, so a new release reached production with the next deployment."""
    from importlib import metadata

    from packaging.markers import default_environment
    from packaging.requirements import Requirement
    from packaging.utils import canonicalize_name

    lines = [x for x in (HOSTED / "requirements.txt").read_text().splitlines()
             if x and not x.startswith("#")]
    pinned = {}
    for line in lines:
        found = re.fullmatch(r"([A-Za-z0-9_.-]+)(\[[a-z,]+\])?==([0-9][0-9A-Za-z.]*)", line)
        assert found, line  # a name and one exact version, nothing else
        pinned[canonicalize_name(found.group(1))] = found.group(3)
    assert len(pinned) == len(lines)
    for name, version in pinned.items():
        assert metadata.version(name) == version, name  # what the tests ran on
    # Nothing the three packages need is left to chance.
    needed, todo = set(), [("flask", set()), ("psycopg", {"binary"}), ("pydantic", set())]
    while todo:
        name, extras = todo.pop()
        name = canonicalize_name(name)
        if (name, frozenset(extras)) in needed:
            continue
        needed.add((name, frozenset(extras)))
        for line in metadata.distribution(name).requires or []:
            req = Requirement(line)
            if req.marker is None or any(req.marker.evaluate({**default_environment(), "extra": e})
                                         for e in (extras or {""})):
                todo.append((req.name, set(req.extras)))
    assert {name for name, _extras in needed} == set(pinned)


def test_the_health_page_and_the_static_files_touch_neither_the_store_nor_a_session(
        site, monkeypatch):  # noqa: F811
    def gone(*_a, **_k):
        raise AssertionError("the store was used")

    for name in ("connection", "active_bundle", "records_of", "reviewer_by_id", "bundle_is_demo"):
        monkeypatch.setattr(site.store, name, gone)
    b = site.browser()
    health = b.get("/healthz")
    assert health.status_code == 200 and health.get_data() == b"ok\n"
    assert b.get("/robots.txt").status_code == 200 and b.get("/favicon.ico").status_code == 204
    assert b.get("/static/app.css").status_code == 200
    assert b.get("/healthz", headers={"Cookie": "__Host-session=anything"}).status_code == 200


def test_what_the_http_layer_declines_is_not_a_failure(site, caplog):  # noqa: F811
    """The review's finding 11: a range of a static file that cannot be satisfied was a 500."""
    caplog.set_level(logging.DEBUG)
    b = site.browser().sign_in(FIRST)
    answer = b.get("/static/app.css", headers={"Range": "bytes=999999-"})
    assert answer.status_code == 416 and b"could not be read" in answer.get_data()
    assert answer.headers["Cache-Control"] == "private, no-store"
    part = b.get("/static/app.css", headers={"Range": "bytes=0-9"})
    assert part.status_code == 206 and len(part.get_data()) == 10
    for path, method, status in (("/form/i07/", "GET", 404), ("/sign-out", "GET", 405),
                                 ("/form/i07/save", "GET", 405), ("/form/i07", "OPTIONS", 200),
                                 ("/static/", "GET", 404), ("/form/i07", "TRACE", 405),
                                 ("/static/%2e%2e/reviewer_web/schema.sql", "GET", 404)):
        assert b.client.open(path, method=method, base_url=ORIGIN).status_code == status, path
    assert not [r for r in caplog.records if "REQUEST_FAILED" in r.getMessage()]


def test_a_failure_shows_no_detail_and_logs_a_code_only(site, monkeypatch, caplog):  # noqa: F811
    caplog.set_level(logging.DEBUG)
    b = site.browser().sign_in(FIRST)

    def broken(_reviewer):
        raise RuntimeError("Short Zed Metals around 412 for reviewer@example.invalid")

    csrf = b.csrf()
    monkeypatch.setattr(site.store, "records_of", broken)
    for answer in (b.get("/"), b.get("/form/i07"),
                   b.post("/form/i07/state", {"_csrf": csrf, "_phase": A.INDEPENDENT})):
        text = answer.get_data(as_text=True)
        assert answer.status_code == 500 and "The page could not be completed." in text
        assert "Zed" not in text and "Traceback" not in text and "RuntimeError" not in text
        assert answer.headers["Cache-Control"] == "private, no-store"
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "REQUEST_FAILED endpoint=form error=RuntimeError at=test_hosted_web_access.py" in logged
    assert "Zed" not in logged and "example.invalid" not in logged
    assert not any(r.exc_info for r in caplog.records)  # no traceback either


def test_preview_and_test_show_invented_data_only_and_say_so(tmp_path, store, dsn):  # noqa: F811
    from reviewer_web import app as web
    from reviewer_web import mail
    from reviewer_web.store import Store

    site = Site(tmp_path, store, dsn)  # noqa: F811 - the demo bundle, flagged as such
    b = site.browser().sign_in(FIRST)
    for path in ("/", "/form/i07", "/guide"):
        assert "Demonstration data" in b.get(path).get_data(as_text=True)
    assert "Demonstration data" in site.browser().get("/sign-in").get_data(as_text=True)
    # The same content published WITHOUT the flag: the site answers no content route any more,
    # and an application for preview or test refuses to start on it.
    files, media = bundle_bytes(tmp_path / "demo")  # the site's own bundle: its reviewers' bundle
    store.publish_bundle(*files, media, is_demo=False, source_sequences=1)
    for path in ("/", "/form/i07", "/guide", f"/media/{site.bundle.bundle_id}/p01/chart-p01.png"):
        answer = b.get(path)
        assert answer.status_code == 503 and b"Zed" not in answer.get_data()
    for env in ("preview", "test"):
        with pytest.raises(RuntimeError, match="NOT_A_DEMO_BUNDLE"):
            web.create_app(Store(dsn), mail.Outbox(), {**site.config, "SITE_ENV": env})
    # Production serves the reviewer that bundle, which is theirs, and without the band: the
    # band follows the reader's bundle, not the environment (a real reviewer beside a test
    # identity: the next test).
    client = site.production().test_client()
    token = b.client.get_cookie("__Host-session", domain="reviewer.example.invalid").value
    client.set_cookie("__Host-session", token, domain="reviewer.example.invalid", secure=True)
    page = client.get("/form/i07", base_url=ORIGIN)
    assert page.status_code == 200 and b"Demonstration data" not in page.get_data()
    store.publish_bundle(*files, media, is_demo=True, source_sequences=1)
    assert b"Demonstration data" in client.get("/form/i07", base_url=ORIGIN).get_data()
    store.publish_bundle(*files, media, is_demo=False, source_sequences=1)
    # The configuration itself.
    for config, code in (({"SESSION_SECRET": "short"}, "SESSION_SECRET_TOO_SHORT"),
                         ({"SESSION_SECRET": ""}, "SESSION_SECRET_TOO_SHORT"),
                         ({"SITE_ENV": "staging"}, "UNKNOWN_SITE_ENV")):
        with pytest.raises(RuntimeError, match=code):
            web.create_app(Store(dsn), mail.Outbox(), {**site.config, **config})
    # The platform's application comes from the environment.
    store.publish_bundle(*files, media, is_demo=True, source_sequences=1)
    served = web.from_environment({"DATABASE_URL": dsn, "SESSION_SECRET": SECRET,
                                   "SITE_ENV": "preview"}).test_client()
    assert served.get("/healthz", base_url=ORIGIN).status_code == 200
    assert served.get("/", base_url=ORIGIN).headers["Location"] == "/sign-in"
    with pytest.raises(KeyError):
        web.from_environment({"SESSION_SECRET": SECRET, "SITE_ENV": "preview"})  # no database
    with pytest.raises(RuntimeError, match="MAIL_NOT_CONFIGURED"):
        web.from_environment({"DATABASE_URL": dsn, "SESSION_SECRET": SECRET})  # production


def test_a_test_identity_reads_invented_forms_beside_a_real_reviewer_in_production(
        tmp_path, store, dsn):  # noqa: F811
    """One database, production: the reviewer works on the real bundle (here an invented
    stand-in for it), the owner's test identity on the demonstration bundle. Neither ever gets
    a form, an image or a record of the other's."""
    from hosted_web_site import SYSTEM_MARKS

    site = Site(tmp_path, store, dsn)  # noqa: F811 - the demonstration bundle, with its marks
    demo = site.bundle
    real = site.publish_real(write_demo(tmp_path / "real"))
    assert real.bundle_id != demo.bundle_id and not set(real.items) & set(demo.items)
    site.set_reviewer(FIRST, bundle_id=real.bundle_id)  # the reviewer: the real bundle
    store.upsert_reviewer("TEST-1", "tester@example.invalid", name="Test Identity",
                          scope_sequences=40, is_test=True)  # takes the invented bundle
    assert site.reviewers["TEST-1"].bundle_id == demo.bundle_id
    live = site.production()
    assert b"Demonstration data" not in site.browser(live).get("/sign-in").get_data()
    # The test identity signs in by mail like everybody: production never shows a code.
    tester = site.browser(live)
    asked = tester.post("/sign-in", {"address": "tester@example.invalid"})
    code = site.code()
    whole = asked.get_data(as_text=True) + repr(sorted(asked.headers.items()))
    assert site.outbox.sent[-1][0] == "tester@example.invalid" and code not in whole
    assert "your code is" not in whole and "Demonstration" not in whole
    assert tester.post("/sign-in/verify", {"address": "tester@example.invalid",
                                           "code": code}).status_code == 303
    reviewer = site.browser(live).sign_in(FIRST)

    def forms(browser) -> list[str]:
        return re.findall(r'href="/form/([^"]+)"', browser.get("/").get_data(as_text=True))

    assert forms(tester) == list(F_scoped(demo)) and forms(reviewer) == list(F_scoped(real))
    assert forms(reviewer)[0] == "ri07" and forms(tester)[0] == "i07"
    # The band: wherever the reader's bundle is invented, and only there.
    for path in ("/", "/form/i07", "/guide", "/fields"):
        assert b"Demonstration data" in tester.get(path).get_data(), path
    for path in ("/", "/form/ri07", "/guide", "/fields"):
        assert b"Demonstration data" not in reviewer.get(path).get_data(), path
    # A form of the other bundle does not exist, to read or to write.
    for browser, theirs, other in ((reviewer, "ri07", "i07"), (tester, "i07", "ri07")):
        data = {"_csrf": browser.csrf(), "_phase": A.INDEPENDENT, "_key": "k" * 24}
        assert browser.get(f"/form/{theirs}").status_code == 200
        assert browser.get(f"/form/{other}").status_code == 404
        assert browser.post(f"/form/{other}/state", data).status_code == 404
        assert browser.post(f"/form/{other}/save", data).status_code == 404
    # An image of the other bundle is not served, by any of its names.
    real_image = f"/media/{real.bundle_id}/qp01/real-chart-p01.png"
    demo_image = f"/media/{demo.bundle_id}/p01/chart-p01.png"
    assert reviewer.get(real_image).get_data().startswith(PNG)
    assert tester.get(demo_image).get_data().startswith(PNG)
    refused = [reviewer.get(demo_image), tester.get(real_image),
               reviewer.get(f"/media/{real.bundle_id}/p01/chart-p01.png"),
               reviewer.get(f"/media/{real.bundle_id}/qp01/chart-p01.png"),
               reviewer.get(f"/media/{demo.bundle_id}/qp01/real-chart-p01.png"),
               tester.get(f"/media/{demo.bundle_id}/qp01/real-chart-p01.png"),
               tester.get(f"/media/{demo.bundle_id}/p01/real-chart-p01.png"),
               tester.get(f"/media/{real.bundle_id}/p01/chart-p01.png")]
    assert [r.status_code for r in refused] == [404] * 8
    assert not [r for r in refused if PNG in r.get_data()]
    # Each one's records are of their own bundle, and stay their own.
    tester.site.bundle = demo
    tester.open("i07")
    tester.review("s14a", after=False)
    tester.review("s14b", after=False)  # the invented system's reading is revealed to the tester
    assert SYSTEM_MARKS["s14b"]["reference"] in tester.shown.text
    reviewer.review("ri07", like="i07")
    assert {r["bundle_id"] for r in site.records("TEST-1")} == {demo.bundle_id}
    assert {r["bundle_id"] for r in site.records(FIRST)} == {real.bundle_id}
    assert {r["item_id"] for r in site.records(FIRST)} == {"ri07"}
    pages = [reviewer.get("/"), reviewer.get("/form/rs14a"), reviewer.get("/form/rs14b"),
             reviewer.get("/form/ri07")]
    for page in pages:
        assert b"ZQSYS" not in page.get_data() and b"Demonstration" not in page.get_data()
    assert "1 of 45" in pages[0].get_data(as_text=True)
    assert "0 of 45" in tester.get("/").get_data(as_text=True)
    assert reviewer.get("/continue").headers["Location"] == "/form/ri08"
    assert tester.get("/continue").headers["Location"] == "/form/i07"
    # A test identity is served an invented bundle or nothing.
    with pytest.raises(A.AdjudicationError, match="TEST_IDENTITY_NEEDS_A_DEMO_BUNDLE"):
        site.set_reviewer("TEST-1", bundle_id=real.bundle_id)
    site.sql("UPDATE bundle SET is_demo = FALSE, active = FALSE WHERE bundle_id = %s",
             demo.bundle_id)  # no longer flagged as invented
    assert tester.get("/").status_code == 503 and tester.get(demo_image).status_code == 503
    assert reviewer.get("/").status_code == 200


def F_scoped(bundle) -> dict:
    from reviewer_web import forms as F

    return F.scoped(bundle, A.Session("x", "x", A.REVIEWER, A.FULL, True, 40))


def test_a_reviewer_without_a_bundle_is_served_nothing(store, dsn):  # noqa: F811
    from reviewer_web import app as web
    from reviewer_web import mail
    from reviewer_web.store import Store

    row = store.upsert_reviewer("REMOTE-1", "early@example.invalid", name="Early Example")
    assert row.bundle_id is None  # listed before anything was published
    outbox = mail.Outbox()
    app = web.create_app(Store(dsn), outbox, {"SESSION_SECRET": SECRET, "SITE_ENV": "production",
                                              "SIGN_IN_FLOOR_SECONDS": 0})
    client = app.test_client()
    kw = {"base_url": ORIGIN, "headers": {"Origin": ORIGIN}}
    client.post("/sign-in", data={"address": row.login}, **kw)
    code = re.search(r"\b\d{6}\b", outbox.sent[-1][2]).group(0)
    assert client.post("/sign-in/verify", data={"address": row.login, "code": code},
                       **kw).status_code == 303
    for path in ("/", "/continue", "/form/i07", "/guide", "/media/" + "0" * 64 + "/p01/x.png"):
        answer = client.get(path, base_url=ORIGIN)
        assert answer.status_code == 503 and b"The site is not available." in answer.get_data()


def test_a_deployment_the_platform_does_not_call_production_never_starts_as_production(
        tmp_path, store, dsn):  # noqa: F811
    """The review's finding 3: a preview deployment that inherited the production variables
    (the database, the secret, the mail) and no SITE_ENV started as production on the real
    database."""
    from hosted_pg import publish_as_real
    from reviewer_web import app as web

    site = Site(tmp_path, store, dsn)  # noqa: F811
    real = publish_as_real(store, tmp_path / "demo")  # a stand-in for the real bundle, active
    site.set_reviewer(FIRST, bundle_id=real.bundle_id)
    env = {"DATABASE_URL": dsn, "SESSION_SECRET": SECRET, "SMTP_HOST": "smtp.example.invalid",
           "SMTP_PORT": "465", "SMTP_USER": "u", "SMTP_PASSWORD": "p",
           "MAIL_FROM": "site@example.invalid", "OPERATOR_TOKEN": "t" * 40,
           "SITE_URL": "https://review.example.invalid"}
    for platform in ("preview", "development", "staging"):
        for site_env in ({}, {"SITE_ENV": ""}, {"SITE_ENV": "production"}):
            with pytest.raises(RuntimeError, match="^PREVIEW_DEPLOYMENT_NEEDS_SITE_ENV$"):
                web.from_environment({**env, **site_env, "VERCEL_ENV": platform})
    # Said to be a preview, it serves invented data only: here a real bundle is active.
    with pytest.raises(RuntimeError, match="^NOT_A_DEMO_BUNDLE$"):
        web.from_environment({**env, "VERCEL_ENV": "preview", "SITE_ENV": "preview"})
    # The production deployment, and a site that is not on the platform at all, start.
    for platform in ({"VERCEL_ENV": "production"}, {"VERCEL_ENV": ""}, {}):
        app = web.from_environment({**env, **platform})
        page = app.test_client().get("/sign-in", base_url=ORIGIN)
        assert page.status_code == 200 and b"Demonstration data" not in page.get_data()


def test_the_site_connects_with_its_own_database_address_when_one_is_given(
        tmp_path, store, dsn, monkeypatch):  # noqa: F811
    """SITE_DATABASE_URL (the connection of a restricted role) comes before DATABASE_URL (the
    owning role's, which the platform manages); an empty value counts as not set."""
    from reviewer_web import app as web
    from reviewer_web import store as S

    Site(tmp_path, store, dsn)
    used = []

    class Recording(S.Store):
        def __init__(self, address):
            used.append(address)
            super().__init__(dsn)  # the test's database, whatever address was asked for

    monkeypatch.setattr(S, "Store", Recording)
    env = {"SESSION_SECRET": SECRET, "SITE_ENV": "test"}
    owner, site_role = "postgresql://owner@db.example.invalid/x", \
        "postgresql://site@db.example.invalid/x"
    web.from_environment({**env, "DATABASE_URL": owner, "SITE_DATABASE_URL": site_role})
    web.from_environment({**env, "DATABASE_URL": owner, "SITE_DATABASE_URL": ""})
    web.from_environment({**env, "DATABASE_URL": owner})
    web.from_environment({**env, "SITE_DATABASE_URL": site_role})
    assert used == [site_role, owner, owner, site_role]
    for nothing in ({}, {"SITE_DATABASE_URL": ""}):
        with pytest.raises(KeyError):
            web.from_environment({**env, **nothing})
