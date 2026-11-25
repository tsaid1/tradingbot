"""The hosted reviewer site (``hosted/reviewer_web``): sign-in by a mailed one-time code, sessions,
the CSRF token and the origin check. A real Postgres (``hosted_pg``), the invented demo bundle and
its two invented reviewers; mail goes to an in-memory outbox and the clock is the test's."""

from __future__ import annotations

import hashlib
import logging
import re

import pytest
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
    assert to == login and subject == "Trade review: your sign-in code"
    # The message: the site's name, the code, its validity. No link, nothing else.
    assert text == f"Trade review\n\nYour sign-in code is {code}. It is valid for 10 minutes.\n"
    assert code not in page.get_data(as_text=True) and code not in subject
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


def test_the_first_sign_in_is_noted_once(site):  # noqa: F811
    def first(alias):
        return next(r for r in site.admin.status() if r["alias"] == alias)["first_sign_in_at"]

    assert first(FIRST) is None and first(SECOND) is None
    b, login = site.browser(), login_of(site)
    ask(b, login)
    assert enter(b, login, wrong(site.code())).status_code == 401
    assert first(FIRST) is None  # a refused code is not a sign-in
    assert enter(b, login, site.code()).status_code == 303
    noted = first(FIRST)
    assert noted is not None and first(SECOND) is None
    site.sql("UPDATE reviewer SET first_sign_in_at = first_sign_in_at - interval '3 days' "
             "WHERE alias = %s", FIRST)
    earlier = first(FIRST)
    assert earlier < noted
    site.browser().sign_in(FIRST)  # another device, later: the first time stands
    b.post("/sign-out", {"_csrf": b.csrf()})
    b.sign_in(FIRST)
    assert first(FIRST) == earlier and first(SECOND) is None
    site.store.note_first_sign_in(site.reviewers[SECOND].id)
    site.store.note_first_sign_in(10 ** 9)  # nobody: nothing happens
    assert first(SECOND) is not None


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
    # The newest two codes are valid; a third request ends the oldest; using one ends both.
    codes = []
    for _ in range(3):
        ask(late, login2)
        codes.append(site.code())
    assert len(set(codes)) == 3, "three random codes happened to collide: run again"
    assert enter(late, login2, codes[0]).status_code == 401
    assert enter(late, login2, codes[1]).status_code == 303 and signed_in(late)
    assert enter(site.browser(), login2, codes[2]).status_code == 401  # ended with the other


def test_somebody_elses_request_does_not_end_a_delivered_code(site):  # noqa: F811
    """The review's finding 4, the lock-out: a stranger's request for a reviewer's address ended
    the code the reviewer had just been sent."""
    from reviewer_web import auth

    login = login_of(site)
    reviewer, stranger = site.browser(), site.browser()
    elsewhere = {"X-Real-Ip": "203.0.113.9"}
    ask(reviewer, login)
    code = site.code()
    ask(stranger, login.upper(), headers=elsewhere)
    assert enter(reviewer, login, code).status_code == 303 and signed_in(reviewer)
    # The hour's five requests per address are still counted, whoever makes them: the stranger
    # can use them up. The operator clears the counter (hosted_admin.py unlock).
    site.sql("DELETE FROM rate_limit")
    for _ in range(5):
        ask(stranger, login, headers=elsewhere)
    sent = len(site.outbox.sent)
    again = site.browser()
    ask(again, login)
    assert len(site.outbox.sent) == sent
    assert auth.unlock(site.store, site.reviewers[FIRST].id) == 5
    ask(again, login)
    assert len(site.outbox.sent) == sent + 1
    assert enter(again, login, site.code()).status_code == 303


def test_ten_wrong_codes_in_a_day_lock_the_address_and_the_log_says_so_once(
        site, caplog):  # noqa: F811
    """The review's finding 4, the guessing: 25 guesses an hour per address, for ever, and
    nothing recorded. Now ten wrong codes in 24 hours end it for the rest of the window."""
    from reviewer_web import auth

    caplog.set_level(logging.DEBUG)
    login, row = login_of(site), site.reviewers[FIRST]
    b = site.browser()

    def exhausted() -> list[str]:
        return [r.getMessage() for r in caplog.records if "EXHAUSTED" in r.getMessage()]

    # A wrong code while no code is valid is no guess at one: it is not counted.
    assert enter(b, login, "000000").status_code == 401
    assert auth.locked_until(site.store, row.id, site.clock()) is None
    wrong_tries = 0
    for _hour in range(3):  # the reviewer's probe: one computer, hour after hour
        for _ in range(5):
            sent = len(site.outbox.sent)
            ask(b, login)
            if len(site.outbox.sent) == sent:
                continue
            for n in range(1, 7):
                assert enter(b, login, f"{(int(site.code()) + n) % 10 ** 6:06d}").status_code == 401
        wrong_tries = site.sql("SELECT count(*) FROM rate_limit WHERE bucket = %s",
                               f"wrong:r{row.id}")[0][0]
        site.clock.forward(minutes=61)
    assert wrong_tries == 10  # not 75: after the tenth nothing more was compared or sent
    assert exhausted() == [f"SIGN_IN_TRIES_EXHAUSTED alias={FIRST}"]  # once, the alias only
    assert login not in "\n".join(r.getMessage() for r in caplog.records)
    assert len(site.outbox.sent) == 2  # two codes went out before the lock, none after
    # Locked: no code is sent and none is accepted, not even the right one.
    until = auth.locked_until(site.store, row.id, site.clock())
    assert until is not None and until > site.clock()
    site.sql("DELETE FROM rate_limit WHERE bucket LIKE 'to:%%' OR bucket LIKE 'net:%%'")
    page = ask(b, login)
    assert len(site.outbox.sent) == 2
    assert page.get_data(as_text=True).replace(login, NOBODY) == ask(
        site.browser(), NOBODY).get_data(as_text=True)  # and the page says nothing of it
    site.sql("UPDATE login_code SET used_at = NULL, expires_at = %s, attempts = 0 WHERE id = "
             "(SELECT max(id) FROM login_code)", site.clock() + auth.CODE_VALID)
    assert enter(b, login, site.code()).status_code == 401 and not signed_in(b)
    assert exhausted() == [f"SIGN_IN_TRIES_EXHAUSTED alias={FIRST}"]
    # The other reviewer is not affected.
    other = site.browser().sign_in(SECOND)
    assert signed_in(other)
    # The window passes: the sign-in opens again by itself.
    site.clock.now = until + auth.HOUR
    assert auth.locked_until(site.store, row.id, site.clock()) is None
    site.sql("DELETE FROM rate_limit WHERE bucket LIKE 'to:%%' OR bucket LIKE 'net:%%'")
    ask(b, login)
    assert len(site.outbox.sent) == 4 and enter(b, login, site.code()).status_code == 303
    # Or the operator unlocks it.
    for n in range(10):
        site.sql("INSERT INTO rate_limit (bucket, at) VALUES (%s, %s)", f"wrong:r{row.id}",
                 site.clock())
    assert auth.locked_until(site.store, row.id, site.clock()) is not None
    assert auth.unlock(site.store, row.id) >= 10
    assert auth.locked_until(site.store, row.id, site.clock()) is None


def test_what_is_not_an_address_costs_one_count_and_never_reaches_the_mail_server(
        site):  # noqa: F811
    """The review's finding 11: an empty or over-long address was answered before any limit and
    still made the site sign in to the mail server; a NUL character was a 500."""
    b = site.browser()
    normal = ask(site.browser(), NOBODY, headers={"X-Real-Ip": "203.0.113.1"})
    site.sql("DELETE FROM rate_limit")
    probes, sent = site.outbox.probes, len(site.outbox.sent)
    odd = ["", "   ", "ß" * 300, "x" * 400 + "@example.invalid", "no-at-sign", "a@b@c",
           "two words@example.invalid", "a\x00b@example.invalid", "\x7f@example.invalid",
           "tab\there@example.invalid", "@example.invalid", "someone@", "a\u202eb@example.invalid"]
    for address in odd:
        page = ask(b, address)
        assert page.status_code == 200, repr(address)
        # the neutral page, with nothing of what was typed in it
        assert page.get_data(as_text=True) == normal.get_data(as_text=True).replace(
            f'value="{NOBODY}"', 'value=""'), repr(address)
        refused = enter(b, address, "123456")
        assert refused.status_code == 401 and 'name="address" value=""' in refused.get_data(
            as_text=True)
    for code in ("12\x0034", "\x00" * 6, "１２３４５６", "12345\n6", "-12345", "1e5000"):
        assert enter(b, login_of(site), code).status_code == 401, repr(code)
    assert site.outbox.probes == probes and len(site.outbox.sent) == sent
    # Every one of them was counted against the network address it came from, and only there.
    buckets = [row[0][:4] for row in site.sql("SELECT bucket FROM rate_limit")]
    assert buckets.count("net:") == len(odd) and set(buckets) == {"net:", "try:"}
    # So such requests run into the network's hourly limit like any others.
    for _ in range(30):
        ask(b, "")
    assert site.sql("SELECT count(*) FROM rate_limit WHERE bucket LIKE 'net:%%'") == [(20,)]
    ask(b, login_of(site))
    assert len(site.outbox.sent) == sent and site.outbox.probes == probes


def test_entering_a_code_costs_the_same_whoever_asks(site, monkeypatch):  # noqa: F811
    """The review's finding 11: /sign-in/verify had no least time and opened one more database
    connection for a listed address than for an unlisted one."""
    import psycopg
    from reviewer_web import app as web

    opened, slept = [], []
    connect = psycopg.connect
    monkeypatch.setattr(psycopg, "connect", lambda *a, **k: opened.append(1) or connect(*a, **k))
    monkeypatch.setattr(web.time, "sleep", slept.append)
    app = web.create_app(site.store, site.outbox, {**site.config, "SIGN_IN_FLOOR_SECONDS": 30})
    counts = {}
    for who, address in (("listed", login_of(site)), ("unlisted", NOBODY), ("odd", "nobody")):
        b = site.browser(app)
        for step in ("ask", "000000", "abcdef"):
            opened.clear()
            slept.clear()
            if step == "ask":
                ask(b, address)
            else:
                enter(b, address, step)
            counts[who, step] = len(opened)
            assert len(slept) == 1 and 29 < slept[0] <= 30, (who, step)  # the same least time
    assert set(counts.values()) == {1}, counts  # one connection, whoever asks and whatever for
    # The right code too waits out the floor.
    b = site.browser(app)
    ask(b, login_of(site))
    slept.clear()
    assert enter(b, login_of(site), site.code()).status_code == 303 and len(slept) == 1


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
    # The limit rows hold keyed hashes or a reviewer's row number, never an address.
    buckets = [row[0] for row in site.sql("SELECT bucket FROM rate_limit")]
    assert buckets and all(re.fullmatch(r"(to|net|try):[0-9a-f]{64}|(to|wrong):r\d+", x)
                           for x in buckets)
    assert {x.split(":")[0] for x in buckets} == {"net", "to", "try"}


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

    def sign_out(origin=None, fetch_site=None, **extra):
        headers = {k: v for k, v in (("Origin", origin), ("Sec-Fetch-Site", fetch_site)) if v}
        return b.post("/sign-out", good, origin=None, headers={**headers, **extra}).status_code

    refused = [
        sign_out(),  # neither header
        sign_out("null", "same-origin"),  # "null" is never the site's own origin
        sign_out("null"),
        sign_out("https://elsewhere.example.invalid", "same-origin"),  # the two disagree
        sign_out("https://elsewhere.example.invalid", "cross-site"),
        sign_out("https://elsewhere.example.invalid"),
        sign_out(ORIGIN.replace("https", "http"), "same-origin"),  # another scheme
        sign_out(ORIGIN + ".example.invalid", "same-origin"),
        sign_out(ORIGIN + ":8443", "same-origin"),  # another port
        sign_out(ORIGIN + "/", "same-origin"),
        sign_out(fetch_site="cross-site"),
        sign_out(fetch_site="same-site"),
        sign_out(fetch_site="none"),
        sign_out(ORIGIN, "cross-site"),
        sign_out(ORIGIN, "same-site"),
    ]
    assert refused == [403] * len(refused)
    assert signed_in(b) and not site.records(FIRST)
    # The sign-in forms have no session yet: the origin check alone guards them.
    sent = len(site.outbox.sent)
    stranger = site.browser()
    assert ask(stranger, login_of(site), origin="https://elsewhere.example.invalid").status_code \
        == 403
    assert ask(stranger, login_of(site), origin="null").status_code == 403
    assert ask(stranger, login_of(site), origin=None).status_code == 403
    assert enter(stranger, login_of(site), "123456", origin=None).status_code == 403
    assert len(site.outbox.sent) == sent
    # What a browser sends from a page of the site passes: both headers, the Origin alone (an
    # older browser), or Sec-Fetch-Site alone.
    for origin, fetch_site in ((ORIGIN, "same-origin"), (ORIGIN, None), (None, "same-origin")):
        if not signed_in(b):
            b.sign_in(FIRST)
            good["_csrf"] = b.csrf()
        assert sign_out(origin, fetch_site) == 303


def test_the_sites_own_origin_is_that_of_the_request_behind_a_proxy_and_under_two_names(
        site):  # noqa: F811
    """The platform's proxy reaches the application over plain http and says which scheme the
    browser used; one deployment answers under two host names (its own and the platform's)."""
    login, other = login_of(site), "review.other.example.invalid"

    def post(path, data, host, origin, proto="https", client=None):
        client = client or site.app.test_client()
        return client.post(path, data=data, base_url=f"http://{host}", headers={
            "Origin": origin, "Sec-Fetch-Site": "same-origin", "X-Forwarded-Proto": proto})

    host = ORIGIN.removeprefix("https://")
    # Behind the proxy: the browser's https origin is the site's own, the hop's http is not.
    assert post("/sign-in", {"address": login}, host, ORIGIN).status_code == 200
    assert post("/sign-in", {"address": login}, host, f"http://{host}").status_code == 403
    assert post("/sign-in", {"address": login}, host, ORIGIN, proto="http").status_code == 403
    assert post("/sign-in", {"address": login}, host, f"http://{host}", proto="http").status_code \
        == 200  # a plain http site (the development server) is its own origin too
    # Under the second name the site is its own origin there, and neither name passes for the
    # other.
    assert post("/sign-in", {"address": login}, other, f"https://{other}").status_code == 200
    assert post("/sign-in", {"address": login}, other, ORIGIN).status_code == 403
    assert post("/sign-in", {"address": login}, host, f"https://{other}").status_code == 403
    # A whole visit under the second name: sign-in, the workspace, a form, a round trip, a
    # save. No page and no redirect names a host: every address is relative.
    site.sql("DELETE FROM rate_limit")
    client = site.app.test_client()
    post("/sign-in", {"address": login}, other, f"https://{other}", client=client)
    answer = post("/sign-in/verify", {"address": login, "code": site.code()}, other,
                  f"https://{other}", client=client)
    assert answer.status_code == 303 and answer.headers["Location"] == "/"
    assert "Strict-Transport-Security" in answer.headers
    kw = {"base_url": f"http://{other}", "headers": {"X-Forwarded-Proto": "https"}}
    home = client.get("/", **kw)
    assert home.status_code == 200
    csrf = re.search(r'name="_csrf" value="([0-9a-f]{64})"', home.get_data(as_text=True)).group(1)
    assert client.get("/continue", **kw).headers["Location"] == "/form/i07"
    page = client.get("/form/i02", **kw).get_data(as_text=True)
    key = re.search(r'name="_key" value="([^"]+)"', page).group(1)
    data = {"_csrf": csrf, "_phase": "INDEPENDENT", "_key": key, "_on": "post_label",
            "post_label": "COMMENTARY", "confidence": "HIGH"}
    state = post("/form/i02/state", data, other, f"https://{other}", client=client)
    assert state.status_code == 200 and "Save independent reading" in state.get_data(as_text=True)
    saved = post("/form/i02/save", data, other, f"https://{other}", client=client)
    assert saved.status_code == 303 and saved.headers["Location"].startswith("/form/i02?saved=")
    assert post("/form/i02/save", data, other, ORIGIN, client=client).status_code == 403
    pages = [home, state, saved, client.get(saved.headers["Location"], **kw),
             client.get("/guide", **kw), client.get("/fields", **kw)]
    for response in pages:
        text = response.get_data(as_text=True) + repr(sorted(response.headers.items()))
        assert "example.invalid" not in text.replace(login, "") and "://" not in text
    # The session of one name is not a session under the other (the cookie is the host's own).
    assert site.app.test_client().get("/", base_url=ORIGIN).status_code == 303
    assert [r["phase"] for r in site.records(FIRST)] == ["INDEPENDENT", "SYSTEM_SHOWN"]


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

    def failing(_to, _subject, text):
        raise RuntimeError(f"the server said no to {text}")  # an error may quote anything

    broken = web.create_app(site.store, failing, site.config).test_client()
    same = broken.post("/sign-in", data={"address": login}, base_url=ORIGIN,
                       headers={"Origin": ORIGIN})
    assert same.status_code == 200 and same.get_data(as_text=True) == seen[0].get_data(
        as_text=True)  # a message that failed answers like one that went out
    seen.append(same)
    [(newest,)] = site.sql("SELECT code_hmac FROM login_code ORDER BY id DESC LIMIT 1")
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "MAIL_FAILED error=RuntimeError" in logged
    assert code not in logged and login.casefold() not in logged.casefold()
    assert newest not in logged and "sign-in code is" not in logged
    for response in seen:
        assert code not in response.get_data(as_text=True)
        assert code not in repr(sorted(response.headers.items()))
        assert not re.search(r"code is\s*(<[^>]+>)?\d{6}", response.get_data(as_text=True))
    # A healthy mail server is asked for an unlisted address too, and nothing is sent to it.
    probes, sent = site.outbox.probes, len(site.outbox.sent)
    ask(site.browser(), NOBODY)
    assert site.outbox.probes == probes + 1 and len(site.outbox.sent) == sent


def test_production_never_shows_its_own_code_whatever_happens_to_the_mail(
        tmp_path, store, dsn, monkeypatch, caplog):  # noqa: F811
    import smtplib

    from reviewer_web import app as web
    from reviewer_web import auth, mail

    caplog.set_level(logging.DEBUG)
    site = Site(tmp_path, store, dsn)  # noqa: F811
    login, made = login_of(site), []
    real = auth.secrets.randbelow

    def recorded(n):
        made.append(f"{real(n):06d}")
        return int(made[-1])

    monkeypatch.setattr(auth.secrets, "randbelow", recorded)

    class Mailer:
        """A mail server in one of its states: fine, unreachable, or failing this message."""

        def __init__(self, at_connect=None, at_send=None):
            self.at_connect, self.at_send, self.sent = at_connect, at_send, []

        def __call__(self, to, _subject, text):
            if self.at_connect:
                raise mail.Unreachable(type(self.at_connect).__name__)
            if self.at_send:
                raise self.at_send
            self.sent.append((to, text))

        def probe(self):
            if self.at_connect:
                raise mail.Unreachable(type(self.at_connect).__name__)

    def production(mailer):
        return web.create_app(site.store, mailer, {**site.config, "SITE_ENV": "production"})

    def ask_live(app, address):
        site.sql("DELETE FROM rate_limit")
        return app.test_client().post("/sign-in", data={"address": address}, base_url=ORIGIN,
                                      headers={"Origin": ORIGIN})

    def whole(response) -> str:
        return response.get_data(as_text=True) + repr(sorted(response.headers.items()))

    fine = production(Mailer())
    neutral = ask_live(fine, NOBODY).get_data(as_text=True)
    assert "a message with a six-digit code is on its way" in " ".join(neutral.split())
    cases = [(Mailer(at_connect=TimeoutError("timed out")), True),
             (Mailer(at_connect=ConnectionRefusedError()), True),
             (Mailer(at_send=smtplib.SMTPDataError(451, b"try later 123456")), False),
             (Mailer(at_send=TimeoutError("timed out")), False),
             (Mailer(at_send=smtplib.SMTPRecipientsRefused({})), False),
             (Mailer(), False)]
    for mailer, says_so in cases:
        app, before = production(mailer), len(made)
        listed, unlisted = ask_live(app, login), ask_live(app, NOBODY)
        assert len(made) == before + 1  # one code, for the listed address only
        code = made[-1]
        for response in (listed, unlisted):
            assert response.status_code == 200
            assert code not in whole(response)  # body and every header
            assert "Set-Cookie" not in response.headers and "Location" not in response.headers
            assert "Demonstration" not in response.get_data(as_text=True)
            assert ("The code could not be sent. Try again in a minute." in response.get_data(
                as_text=True)) is says_so
        # The same page for a listed and an unlisted address, whatever happened to the mail;
        assert listed.get_data(as_text=True).replace(login, NOBODY) == unlisted.get_data(
            as_text=True)
        # and without the sentence it is the page of a message that went out.
        if not says_so:
            assert unlisted.get_data(as_text=True) == neutral
        # A code that was not delivered is ended: it cannot be tried afterwards.
        delivered = bool(mailer.sent)
        answer = app.test_client().post(
            "/sign-in/verify", data={"address": login, "code": code}, base_url=ORIGIN,
            headers={"Origin": ORIGIN})
        assert answer.status_code == (303 if delivered else 401)
        assert code not in whole(answer)
        assert site.sql("SELECT count(*) FROM login_code WHERE used_at IS NULL") == [(0,)]
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "MAIL_UNREACHABLE error=TimeoutError" in logged
    assert "MAIL_FAILED error=SMTPDataError" in logged
    assert not [code for code in made if code in logged] and login not in logged
    assert "try later" not in logged


def test_mail_goes_out_over_tls_by_the_port_and_carries_the_code_and_nothing_else(monkeypatch):
    import smtplib

    from reviewer_web import auth, mail

    calls = []

    class Server:
        def __init__(self, host, port, timeout=None, context=None):
            calls.append(("connect", type(self).__name__, host, port, timeout, context is not None))

        def starttls(self, context=None):
            calls.append(("starttls", context is not None))

        def login(self, user, password):
            calls.append(("login", user, password))

        def send_message(self, message):
            calls.append(("send", message))

        def noop(self):
            calls.append(("noop",))

        def close(self):
            calls.append(("close",))

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            calls.append(("quit",))

    class Tls(Server):
        pass

    monkeypatch.setattr(smtplib, "SMTP", Server)
    monkeypatch.setattr(smtplib, "SMTP_SSL", Tls)
    env = {"SMTP_HOST": "mail.example.invalid", "SMTP_PORT": "465", "SMTP_USER": "sender",
           "SMTP_PASSWORD": "an-invented-key", "MAIL_FROM": "review@example.invalid"}
    mail.Smtp(env)("reviewer@example.invalid", auth.SUBJECT, auth.mail_text("123456"))
    assert calls[0] == ("connect", "Tls", "mail.example.invalid", 465, 15, True)  # implicit TLS
    assert calls[1] == ("login", "sender", "an-invented-key") and calls[-1] == ("quit",)
    message = calls[2][1]
    assert message["From"] == "Trade review <review@example.invalid>"
    assert message["To"] == "reviewer@example.invalid"
    assert message["Subject"] == "Trade review: your sign-in code"
    assert message["Date"].endswith("+0000") and re.fullmatch(
        r"<[^@<>\s]+@example\.invalid>", message["Message-ID"])
    assert message.get_content_type() == "text/plain" and not message.is_multipart()
    body = message.get_content()
    assert body == "Trade review\n\nYour sign-in code is 123456. It is valid for 10 minutes.\n"
    assert "http" not in body and "://" not in body and "example.invalid" not in body
    # Any other port: STARTTLS before the sign-in, or nothing is sent.
    calls.clear()
    mail.Smtp({**env, "SMTP_PORT": "587"}).probe()
    assert [c[0] for c in calls] == ["connect", "starttls", "login", "noop", "quit"]
    assert calls[0][1] == "Server" and calls[0][3] == 587

    class NoTls(Server):
        def starttls(self, context=None):
            raise smtplib.SMTPNotSupportedError("STARTTLS extension not supported by server.")

    monkeypatch.setattr(smtplib, "SMTP", NoTls)
    calls.clear()
    with pytest.raises(mail.Unreachable, match="^SMTPNotSupportedError$"):
        mail.send("reviewer@example.invalid", "s", "t", {**env, "SMTP_PORT": "587"})
    assert [c[0] for c in calls] == ["connect", "close"]  # no sign-in, no message in the clear

    class Refused(Server):
        def __init__(self, *a, **k):
            raise ConnectionRefusedError("an-invented-key must not travel with the error")

    monkeypatch.setattr(smtplib, "SMTP_SSL", Refused)
    with pytest.raises(mail.Unreachable) as failure:
        mail.Smtp(env).probe()
    assert failure.value.args == ("ConnectionRefusedError",) and failure.value.__cause__ is None
    assert mail.configured(env) is True and mail.configured({}) is False
    assert mail.configured({**env, "SMTP_PASSWORD": ""}) is None


def test_synthetic_mail_shows_the_code_only_in_a_preview_of_invented_data(tmp_path, store, dsn):  # noqa: F811
    from hosted_pg import bundle_bytes, write_demo
    from reviewer_web import app as web
    from reviewer_web.store import Store

    site = Site(tmp_path, store, dsn)  # noqa: F811
    login = login_of(site)
    env = {"DATABASE_URL": dsn, "SESSION_SECRET": SECRET}
    smtp = {"SMTP_HOST": "mail.example.invalid", "SMTP_PORT": "465", "SMTP_USER": "sender",
            "SMTP_PASSWORD": "an-invented-key", "MAIL_FROM": "review@example.invalid"}

    def post(app, path, data):
        return app.test_client().post(path, data=data, base_url=ORIGIN, headers={"Origin": ORIGIN})

    # Preview (and test) without any mail variable: no mail leaves, the code stands in the band.
    for site_env in ("preview", "test"):
        app = web.from_environment({**env, "SITE_ENV": site_env})
        page = post(app, "/sign-in", {"address": login}).get_data(as_text=True)
        shown = re.search(r'<div class="demo-band".*?Demonstration: your code is <strong>(\d{6})'
                          r"</strong>; no email was sent\..*?</div>", page, re.DOTALL)
        assert shown, page[:900]
        client = app.test_client()
        answer = client.post("/sign-in/verify", data={"address": login, "code": shown.group(1)},
                             base_url=ORIGIN, headers={"Origin": ORIGIN})
        assert answer.status_code == 303
        assert client.get("/", base_url=ORIGIN).status_code == 200
        other = post(app, "/sign-in", {"address": NOBODY}).get_data(as_text=True)
        assert "your code is" not in other and not re.search(r"\b\d{6}\b", other)
        assert "Demonstration: no email is sent from this site." in other
    assert not site.outbox.sent
    # Production never shows a code: without the five variables it does not start,
    for missing in (*smtp, None):
        given = {k: v for k, v in smtp.items() if k != missing} if missing else {}
        with pytest.raises(RuntimeError, match="MAIL_NOT_CONFIGURED"):
            web.from_environment({**env, **given, "SITE_ENV": "production"})
    with pytest.raises(RuntimeError, match="MAIL_NOT_CONFIGURED"):
        web.create_app(Store(dsn), None, {**site.config, "SITE_ENV": "production"})
    with pytest.raises(RuntimeError, match="MAIL_NOT_CONFIGURED"):
        web.from_environment({**env, "SMTP_HOST": "mail.example.invalid", "SITE_ENV": "preview"})
    # and with them it mails, and its pages carry no code and no band.
    live = web.create_app(Store(dsn), site.outbox, {**site.config, "SITE_ENV": "production"})
    page = post(live, "/sign-in", {"address": login}).get_data(as_text=True)
    assert len(site.outbox.sent) == 1 and site.code() not in page
    assert "Demonstration" not in page and "your code is" not in page
    assert isinstance(web.from_environment({**env, **smtp, "SITE_ENV": "production"}), web.Flask)
    # A preview whose bundle is not flagged as demonstration data: no code is made or shown,
    # and such a preview does not start.
    app = web.from_environment({**env, "SITE_ENV": "preview"})
    files, media = bundle_bytes(write_demo(tmp_path / "again"))
    store.publish_bundle(*files, media, is_demo=False, source_sequences=1)
    codes = site.sql("SELECT count(*) FROM login_code")
    refused = post(app, "/sign-in", {"address": login})
    assert refused.status_code == 503 and not re.search(rb"\b\d{6}\b", refused.get_data())
    assert site.sql("SELECT count(*) FROM login_code") == codes
    with pytest.raises(RuntimeError, match="NOT_A_DEMO_BUNDLE"):
        web.from_environment({**env, "SITE_ENV": "preview"})
