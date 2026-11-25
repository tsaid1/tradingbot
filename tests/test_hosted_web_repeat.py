"""A second press of "Sign in" (production incident): the browser drops the first answer, which
carried the new session, when the second request starts; the second found the code used and was
refused, and the reviewer could not sign in. Now the browser that asked for the code may repeat
its own successful sign-in for a minute (the cookie its code request was answered with), and one
live session results; everything else is refused exactly as before. INVENTED data."""

from __future__ import annotations

import re
import threading

from hosted_web_site import FIRST, ORIGIN, Site, dsn, site, store  # noqa: F401 - fixtures

HEAD = {"Origin": ORIGIN, "Sec-Fetch-Site": "same-origin"}
BROWSER = "__Host-signin"


def post(client, path, data, browser=None):
    headers = dict(HEAD)
    if browser:
        headers["Cookie"] = f"{BROWSER}={browser}"
    return client.post(path, data=data, base_url=ORIGIN, headers=headers)


def ask(site, address):  # noqa: F811
    """A code request as a browser makes it: the code (from the outbox) and the browser value
    the answer set."""
    site.sql("DELETE FROM rate_limit WHERE bucket NOT LIKE 'wrong:%%'")
    answer = post(site.app.test_client(use_cookies=False), "/sign-in", {"address": address})
    browser = re.search(rf"{BROWSER}=([^;]+)", answer.headers["Set-Cookie"]).group(1)
    return re.search(r"\b(\d{6})\b", site.outbox.sent[-1][2]).group(1), browser


def sessions(site) -> list:  # noqa: F811
    return site.sql("SELECT reviewer_id FROM web_session")


def token_of(answer) -> str | None:
    found = re.search(r"__Host-session=([^;]+)", ",".join(answer.headers.getlist("Set-Cookie")))
    return found and found.group(1)


def test_a_second_press_signs_in_and_leaves_one_live_session(site):  # noqa: F811
    login = site.reviewers[FIRST].login_shown
    code, browser = ask(site, login)
    first = post(site.app.test_client(use_cookies=False), "/sign-in/verify",
                 {"address": login, "code": code}, browser)
    stamped = site.sql("SELECT first_sign_in_at FROM reviewer WHERE alias = %s", FIRST)
    second = post(site.app.test_client(use_cookies=False), "/sign-in/verify",
                  {"address": login, "code": code}, browser)
    assert [r.status_code for r in (first, second)] == [303, 303]
    assert first.headers["Location"] == second.headers["Location"] == "/"
    assert token_of(first) != token_of(second) and len(sessions(site)) == 1
    assert site.sql("SELECT first_sign_in_at FROM reviewer WHERE alias = %s", FIRST) == stamped
    assert site.sql("SELECT count(*) FROM rate_limit WHERE bucket LIKE 'wrong:%%'") == [(0,)]
    # The answer the browser kept works; the one it dropped was ended.
    kept, dropped = site.app.test_client(), site.app.test_client()
    kept.set_cookie("__Host-session", token_of(second), domain="reviewer.example.invalid")
    dropped.set_cookie("__Host-session", token_of(first), domain="reviewer.example.invalid")
    assert kept.get("/", base_url=ORIGIN).status_code == 200
    assert dropped.get("/", base_url=ORIGIN).status_code == 303
    # The sign-in ends the code request's cookie.
    assert f"{BROWSER}=;" in ",".join(second.headers.getlist("Set-Cookie"))


def test_two_presses_at_once_give_one_live_session(site):  # noqa: F811
    login = site.reviewers[FIRST].login_shown
    code, browser = ask(site, login)
    answers, start = [], threading.Barrier(2, timeout=30)

    def press():
        client = site.app.test_client(use_cookies=False)
        start.wait()
        answers.append(post(client, "/sign-in/verify", {"address": login, "code": code}, browser))

    threads = [threading.Thread(target=press) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(r.status_code for r in answers) == [303, 303]
    assert len(sessions(site)) == 1
    assert site.sql("SELECT count(*) FROM reviewer WHERE first_sign_in_at IS NOT NULL") == [(1,)]


def test_anything_else_is_refused_exactly_as_before(site):  # noqa: F811
    login = site.reviewers[FIRST].login_shown
    code, browser = ask(site, login)
    verify = {"address": login, "code": code}
    assert post(site.app.test_client(use_cookies=False), "/sign-in/verify", verify,
                browser).status_code == 303
    refused = []
    for who in (None, "x" * 43, "another-request-s-cookie-value-0000000000000"):
        refused.append(post(site.app.test_client(use_cookies=False), "/sign-in/verify", verify,
                            who))
    site.clock.forward(seconds=61)
    refused.append(post(site.app.test_client(use_cookies=False), "/sign-in/verify", verify,
                        browser))
    assert {r.status_code for r in refused} == {401} and len(sessions(site)) == 1
    # A newer code ("Send a new code") ends the repeat of the older one at once.
    site.clock.forward(seconds=-61)
    code2, browser2 = ask(site, login)
    assert post(site.app.test_client(use_cookies=False), "/sign-in/verify", verify,
                browser).status_code == 401
    del code2, browser2
    assert site.sql("SELECT count(*) FROM rate_limit WHERE bucket LIKE 'wrong:%%'")[0][0] == 1
    # (the older code against the newer valid one: a wrong code, counted as before)


def test_a_wrong_code_counts_and_a_revoked_row_repeats_nothing(site):  # noqa: F811
    login = site.reviewers[FIRST].login_shown
    code, browser = ask(site, login)
    wrong = f"{(int(code) + 1) % 10 ** 6:06d}"
    assert post(site.app.test_client(use_cookies=False), "/sign-in/verify",
                {"address": login, "code": wrong}, browser).status_code == 401
    assert site.sql("SELECT count(*) FROM rate_limit WHERE bucket LIKE 'wrong:%%'") == [(1,)]
    assert post(site.app.test_client(use_cookies=False), "/sign-in/verify",
                {"address": login, "code": code}, browser).status_code == 303
    site.admin.set_revoked(FIRST, True)
    assert post(site.app.test_client(use_cookies=False), "/sign-in/verify",
                {"address": login, "code": code}, browser).status_code == 401
    assert sessions(site) == []


def test_a_signed_in_browser_or_a_pending_choice_is_sent_on_and_nothing_counted(
        tmp_path, store, dsn):  # noqa: F811
    from test_hosted_identities import ADDRESS, Two

    two = Two(tmp_path, store, dsn)
    site = two.site  # noqa: F811
    site.app = two.app
    code, browser = ask(site, ADDRESS)
    client = site.app.test_client()
    client.set_cookie(BROWSER, browser, domain="reviewer.example.invalid")
    first = client.post("/sign-in/verify", data={"address": ADDRESS, "code": code},
                        base_url=ORIGIN, headers=HEAD)
    assert first.headers["Location"] == "/sign-in/choose"
    again = client.post("/sign-in/verify", data={"address": ADDRESS, "code": "000000"},
                        base_url=ORIGIN, headers=HEAD)  # the choice it holds is still to make
    assert again.headers["Location"] == "/sign-in/choose"
    # The first answer dropped (another tab without its choice): the repeat offers it again.
    fresh = site.app.test_client(use_cookies=False)
    repeat = post(fresh, "/sign-in/verify", {"address": ADDRESS, "code": code}, browser)
    assert repeat.headers["Location"] == "/sign-in/choose"
    assert site.sql("SELECT count(*) FROM sign_in_choice WHERE used_at IS NULL") == [(1,)]
    assert client.get("/sign-in/choose", base_url=ORIGIN).headers.get("Location") == "/sign-in"
    choice = re.search(r"__Host-choice=([^;]+)", ",".join(
        repeat.headers.getlist("Set-Cookie"))).group(1)
    chooser = site.app.test_client()
    chooser.set_cookie("__Host-choice", choice, domain="reviewer.example.invalid")
    assert chooser.post("/sign-in/choose", data={"identity": "test"}, base_url=ORIGIN,
                        headers=HEAD).headers["Location"] == "/"
    # Signed in now: a verify from that browser is sent to the site, nothing counted.
    late = chooser.post("/sign-in/verify", data={"address": ADDRESS, "code": "123456"},
                        base_url=ORIGIN, headers=HEAD)
    assert late.status_code == 303 and late.headers["Location"] == "/"
    assert site.sql("SELECT count(*) FROM rate_limit WHERE bucket LIKE 'wrong:%%'") == [(0,)]
    assert len(sessions(site)) == 1


def test_the_refused_page_is_the_same_for_listed_and_unlisted_and_the_floor_holds(
        site, monkeypatch):  # noqa: F811
    from reviewer_web import app as web

    slept = []
    monkeypatch.setattr(web.time, "sleep", slept.append)
    app = web.create_app(site.store, site.outbox, {**site.config, "SIGN_IN_FLOOR_SECONDS": 30})
    site.app = app
    login = site.reviewers[FIRST].login_shown
    code, browser = ask(site, login)
    slept.clear()
    assert post(app.test_client(use_cookies=False), "/sign-in/verify",
                {"address": login, "code": code}, browser).status_code == 303
    assert post(app.test_client(use_cookies=False), "/sign-in/verify",
                {"address": login, "code": code}, browser).status_code == 303
    assert len(slept) == 2 and all(29 < s <= 30 for s in slept)  # the repeat waits it out too
    pages = [post(app.test_client(use_cookies=False), "/sign-in/verify",
                  {"address": who, "code": code})
             for who in (login, "nobody.here@example.invalid")]
    assert [p.status_code for p in pages] == [401, 401]
    texts = [p.get_data(as_text=True) for p in pages]
    assert texts[0].replace(login, "X") == texts[1].replace("nobody.here@example.invalid", "X")
