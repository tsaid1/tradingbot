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
