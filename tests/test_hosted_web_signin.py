"""The hosted reviewer site (``hosted/reviewer_web``): sign-in by a mailed one-time code, sessions,
the CSRF token and the origin check. A real Postgres (``hosted_pg``), the invented demo bundle and
its two invented reviewers; mail goes to an in-memory outbox and the clock is the test's."""

from __future__ import annotations

import hashlib
import logging
import re

from hosted_web_site import FIRST, ORIGIN, SECOND, dsn, site, store  # noqa: F401 - fixtures

NOBODY = "nobody@example.invalid"
COOKIE = "__Host-session"


def login_of(site, alias=FIRST) -> str:  # noqa: F811
    return site.reviewers[alias].login_shown


def ask(browser, address, **kw):
    return browser.post("/sign-in", {"address": address}, **kw)


def enter(browser, address, code, **kw):
    return browser.post("/sign-in/verify", {"address": address, "code": code}, **kw)


def signed_in(browser) -> bool:
    return browser.get("/").status_code == 200


def wrong(code: str) -> str:
    return f"{(int(code) + 1) % 10 ** 6:06d}"


def test_a_mailed_code_signs_in_and_only_hashes_are_stored(site):  # noqa: F811
    b, login = site.browser(), login_of(site)
    assert b.get("/").status_code == 303 and b.get("/").headers["Location"] == "/sign-in"
    page = ask(b, login.upper())  # the address is matched whatever its letter case
    assert page.status_code == 200
    [(to, subject, text)] = site.outbox.sent
    code = site.code()
    assert to == login and subject == "Your sign-in code" and code in text
    assert code not in page.get_data(as_text=True)
    answer = enter(b, login, code)
    assert answer.status_code == 303 and answer.headers["Location"] == "/"
    cookie = answer.headers["Set-Cookie"]
    for part in (f"{COOKIE}=", "Secure", "HttpOnly", "Path=/", "SameSite=Lax", "Max-Age=2592000"):
        assert part in cookie
    assert "Domain" not in cookie
    token = re.search(rf"{COOKIE}=([^;]+)", cookie).group(1)
    assert len(token) >= 43  # 256 random bits
    assert signed_in(b) and "Reviewer Example" in b.get("/").get_data(as_text=True)
    # The database holds the token's SHA-256 and the code's HMAC, never the token or the code.
    [(stored,)] = site.sql("SELECT token_sha256 FROM web_session")
    assert stored == hashlib.sha256(token.encode()).hexdigest()
    [(mac, used, tries)] = site.sql("SELECT code_hmac, used_at, attempts FROM login_code")
    assert re.fullmatch(r"[0-9a-f]{64}", mac) and code not in mac and used is not None
    assert tries == 1
    everything = repr(site.sql("SELECT * FROM web_session")) + repr(
        site.sql("SELECT * FROM login_code")) + repr(site.sql("SELECT * FROM rate_limit"))
    assert token not in everything and login.casefold() not in everything.casefold()
    # A signed-in browser does not see the sign-in pages again.
    assert b.get("/sign-in").status_code == 303


def test_the_answer_is_the_same_for_every_address_and_an_unlisted_one_gets_no_mail(site):  # noqa: F811
    listed, unlisted = site.browser(), site.browser()
    one, two = ask(listed, login_of(site)), ask(unlisted, NOBODY)
    assert len(site.outbox.sent) == 1 and site.outbox.sent[0][0] == login_of(site)
    assert one.status_code == two.status_code == 200
    assert one.get_data(as_text=True).replace(login_of(site), NOBODY) == two.get_data(as_text=True)
    assert dict(one.headers) == {**dict(two.headers), "Content-Length": one.headers[
        "Content-Length"]}
    assert site.sql("SELECT count(*) FROM login_code") == [(1,)]
    # Entering a code: the same refusal for an unlisted address as for a wrong code.
    refused, wrong_code = enter(unlisted, NOBODY, "123456"), enter(
        listed, login_of(site), wrong(site.code()))
    assert refused.status_code == wrong_code.status_code == 401
    assert refused.get_data(as_text=True) == wrong_code.get_data(as_text=True).replace(
        login_of(site), NOBODY)
    assert "Set-Cookie" not in refused.headers and "Set-Cookie" not in wrong_code.headers


def test_a_wrong_an_expired_a_used_and_a_replaced_code_are_refused(site):  # noqa: F811
    login = login_of(site)
    b = site.browser()
    ask(b, login)
    code = site.code()
    assert enter(b, login, wrong(code)).status_code == 401 and not signed_in(b)
    assert enter(b, login, "12345").status_code == 401  # not six digits
    assert enter(b, login, "").status_code == 401
    assert enter(b, login, f" {code[:3]} {code[3:]} ").status_code == 303  # spaces are ignored
    assert enter(site.browser(), login, code).status_code == 401  # used: works once
    # Expired: valid ten minutes.
    late, login2 = site.browser(), login_of(site, SECOND)
    ask(late, login2)
    code = site.code()
    site.clock.forward(minutes=10, seconds=1)
    assert enter(late, login2, code).status_code == 401 and not signed_in(late)
    # A new code replaces the earlier one.
    ask(late, login2)
    first = site.code()
    ask(late, login2)
    second = site.code()
    assert enter(late, login2, first).status_code == 401 or first == second
    assert enter(late, login2, second).status_code == 303 and signed_in(late)


def test_a_code_allows_five_tries(site):  # noqa: F811
    login, b = login_of(site), site.browser()
    ask(b, login)
    code = site.code()
    for _ in range(5):
        assert enter(b, login, wrong(code)).status_code == 401
    assert enter(b, login, code).status_code == 401 and not signed_in(b)  # the sixth try
    other, login2 = site.browser(), login_of(site, SECOND)
    ask(other, login2)
    code = site.code()
    for _ in range(4):
        assert enter(other, login2, wrong(code)).status_code == 401
    assert enter(other, login2, code).status_code == 303  # the fifth try


def test_codes_and_tries_are_limited_per_hour(site):  # noqa: F811
    login, b = login_of(site), site.browser()
    pages = [ask(b, login).get_data(as_text=True) for _ in range(6)]
    assert len(site.outbox.sent) == 5 and len(set(pages)) == 1  # the sixth: no mail, same page
    site.clock.forward(minutes=61)
    ask(b, login)
    assert len(site.outbox.sent) == 6
    # Per network address: twenty code requests an hour, whatever the addresses.
    site.clock.forward(minutes=61)
    there = {"X-Real-Ip": "203.0.113.7"}
    for n in range(20):
        ask(b, f"someone{n}@example.invalid", headers=there)
    ask(b, login_of(site, SECOND), headers=there)
    assert len(site.outbox.sent) == 6
    ask(b, login_of(site, SECOND), headers={"X-Real-Ip": "203.0.113.8"})
    assert len(site.outbox.sent) == 7 and site.outbox.sent[-1][0] == login_of(site, SECOND)
    # Tries per network address: thirty an hour, then even the right code is refused there.
    code = site.code()
    for _ in range(30):
        enter(b, NOBODY, "000000", headers=there)
    assert enter(b, login_of(site, SECOND), code, headers=there).status_code == 401
    assert enter(b, login_of(site, SECOND), code,
                 headers={"X-Real-Ip": "203.0.113.9"}).status_code == 303
    # The limit rows hold keyed hashes only.
    buckets = [row[0] for row in site.sql("SELECT bucket FROM rate_limit")]
    assert buckets and all(re.fullmatch(r"(to|net|try):[0-9a-f]{64}", x) for x in buckets)


def test_a_revoked_reviewer_cannot_sign_in_and_an_existing_session_dies_at_once(site):  # noqa: F811
    login, b = login_of(site), site.browser().sign_in(FIRST)
    other = site.browser().sign_in(SECOND)
    assert signed_in(b)
    # Revoked in the reviewer table only: the session row is still there, and is refused.
    site.sql("UPDATE reviewer SET revoked_at = now() WHERE alias = %s", FIRST)
    assert site.sql("SELECT count(*) FROM web_session") == [(2,)]
    answer = b.get("/")
    assert answer.status_code == 303 and answer.headers["Location"] == "/sign-in"
    assert b.get("/form/i07").status_code == 303 and b.get("/guide").status_code == 303
    assert site.sql("SELECT count(*) FROM web_session") == [(1,)]  # deleted on sight
    sent = len(site.outbox.sent)
    assert ask(b, login).status_code == 200 and len(site.outbox.sent) == sent  # no code
    assert signed_in(other)  # nobody else is affected
    # The operator's revoke (which deletes the sessions itself) has the same effect.
    site.admin.set_revoked(SECOND, True)
    assert not signed_in(other)
    site.admin.set_revoked(SECOND, False)
    assert not signed_in(other)  # restored access needs a new sign-in
    other.sign_in(SECOND)
    assert signed_in(other)


def test_only_a_reviewer_with_full_access_can_sign_in_or_stay_signed_in(site):  # noqa: F811
    b = site.browser().sign_in(FIRST)
    site.set_reviewer(FIRST, role="APPROVER")
    assert not signed_in(b) and b.get("/form/i07").status_code == 303
    sent = len(site.outbox.sent)
    ask(b, login_of(site))
    assert len(site.outbox.sent) == sent
    other = site.browser().sign_in(SECOND)
    sent = len(site.outbox.sent)
    site.set_reviewer(SECOND, access="RESTRICTED")
    assert not signed_in(other)
    ask(other, login_of(site, SECOND))
    assert len(site.outbox.sent) == sent
    site.set_reviewer(SECOND, access="FULL")
    assert not signed_in(other)  # the old session was deleted
    other.sign_in(SECOND)
    assert signed_in(other)


def test_a_session_slides_for_thirty_days_and_sign_out_ends_it(site):  # noqa: F811
    b = site.browser().sign_in(FIRST)
    token = b.client.get_cookie(COOKIE, domain="reviewer.example.invalid").value
    for _ in range(3):  # used every 29 days: it stays
        site.clock.forward(days=29)
        assert signed_in(b)
    site.clock.forward(days=30, seconds=1)
    assert not signed_in(b)
    b.sign_in(FIRST)
    assert signed_in(b)
    out = b.post("/sign-out", {"_csrf": b.csrf()})
    assert out.status_code == 303 and out.headers["Location"] == "/sign-in"
    assert f"{COOKIE}=;" in out.headers["Set-Cookie"]
    assert not signed_in(b) and site.sql("SELECT count(*) FROM web_session") == [(0,)]
    # Neither the expired nor the signed-out token works when a browser sends it again.
    thief = site.browser()
    thief.client.set_cookie(COOKIE, token, domain="reviewer.example.invalid", secure=True)
    assert not signed_in(thief)
    thief.client.set_cookie(COOKIE, "x" * 500, domain="reviewer.example.invalid", secure=True)
    assert not signed_in(thief)


def test_a_post_needs_the_sessions_csrf_token_and_the_sites_own_origin(site):  # noqa: F811
    b = site.browser().sign_in(FIRST)
    csrf = b.csrf()
    other_csrf = site.browser().sign_in(SECOND).csrf()
    assert re.fullmatch(r"[0-9a-f]{64}", csrf) and csrf != other_csrf
    for data in ({}, {"_csrf": ""}, {"_csrf": "0" * 64}, {"_csrf": other_csrf}):
        assert b.post("/sign-out", data).status_code == 403
        assert b.post("/form/i07/save", data).status_code == 403
        assert b.post("/form/i07/state", data).status_code == 403
    good = {"_csrf": csrf}
    refused = [
        b.post("/sign-out", good, origin="https://elsewhere.example.invalid"),
        b.post("/sign-out", good, origin=ORIGIN.replace("https", "http")),
        b.post("/sign-out", good, origin=ORIGIN + ".example.invalid"),
        b.post("/sign-out", good, origin=None),  # neither Origin nor Sec-Fetch-Site
        b.post("/sign-out", good, origin=None, headers={"Sec-Fetch-Site": "cross-site"}),
        b.post("/sign-out", good, origin=None, headers={"Sec-Fetch-Site": "same-site"}),
        b.post("/sign-out", good, headers={"Sec-Fetch-Site": "cross-site"}),
        b.post("/sign-out", good, origin="null"),
    ]
    assert [r.status_code for r in refused] == [403] * len(refused)
    assert signed_in(b) and not site.records(FIRST)
    # The sign-in forms have no session yet: the origin check alone guards them.
    sent = len(site.outbox.sent)
    stranger = site.browser()
    assert ask(stranger, login_of(site), origin="https://elsewhere.example.invalid").status_code \
        == 403
    assert ask(stranger, login_of(site), origin=None).status_code == 403
    assert enter(stranger, login_of(site), "123456", origin=None).status_code == 403
    assert len(site.outbox.sent) == sent
    # With only one of the two headers, a same-origin POST passes.
    assert b.post("/sign-out", good, origin=None,
                  headers={"Sec-Fetch-Site": "same-origin"}).status_code == 303


def test_no_response_and_no_log_line_carries_a_code_or_an_address(site, caplog):  # noqa: F811
    from reviewer_web import app as web

    caplog.set_level(logging.DEBUG)
    login, b = login_of(site), site.browser()
    seen = [ask(b, login)]
    code = site.code()
    seen += [enter(b, login, wrong(code)), enter(b, login, code)]
    seen += [b.get(path) for path in ("/", "/guide", "/fields", "/form/i07", "/continue",
                                      "/healthz", "/robots.txt", "/static/app.js")]
    seen.append(b.post("/sign-out", {"_csrf": b.csrf()}))

    def failing(_to, _subject, _text):
        raise RuntimeError(f"the server said no to {code}")  # a mailer's error may quote anything

    broken = web.create_app(site.store, failing, site.config).test_client()
    same = broken.post("/sign-in", data={"address": login}, base_url=ORIGIN,
                       headers={"Origin": ORIGIN})
    assert same.status_code == 200 and same.get_data(as_text=True) == seen[0].get_data(
        as_text=True)  # a failed mail answers the same
    seen.append(same)
    [(newest,)] = site.sql("SELECT code_hmac FROM login_code ORDER BY id DESC LIMIT 1")
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "MAIL_FAILED error=RuntimeError" in logged
    assert code not in logged and login.casefold() not in logged.casefold()
    assert newest not in logged
    for response in seen:
        assert code not in response.get_data(as_text=True)
        assert code not in repr(sorted(response.headers.items()))
