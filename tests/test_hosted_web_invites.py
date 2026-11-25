"""The hosted reviewer site (``hosted/reviewer_web``): the invitations the operator queues
(``scripts/hosted_admin.py invite``) and the site sends through its own mailer
(``POST /internal/send-queued``), because the mail password exists only where the site runs.
A real Postgres, the invented demo bundle and its two invented reviewers; mail goes to an
in-memory outbox."""

from __future__ import annotations

import json
import logging

import pytest
from hosted_web_site import FIRST, ORIGIN, SECOND, Site, dsn, site, store  # noqa: F401 - fixtures

from tradingbot.runtime import adjudication as A

PATH = "/internal/send-queued"
TOKEN = "an-invented-operator-token-0123456789abcdef"
URL = "https://review.example.invalid"
CONTINUE = "INVITE_CONTINUE", "The review site has moved: continue where you left off", (
    "The trade review has moved to a normal website. You no longer need Tailscale, and it is available at any hour.\n\nOpen: {URL}\nEnter this email address, and type the six-digit code that arrives in your inbox. That is the whole sign-in.\n\nEverything you saved is there. \"Continue\" takes you to your next unfinished step. If you had saved your own reading of a form and not yet answered the questions shown afterwards, that form opens at those questions with your own reading intact.\n\nOne change after you save your own reading: two questions now stand on their own, each beside what the system proposed. The first asks whether the system's price series is the one the post's numbers use. The second asks whether trading the system's proposed instrument would faithfully follow the publisher's idea. Answer Yes, No or Unsure.\n\nPlease stop using the old address. If the code does not arrive within two minutes, check spam, then tell the person who invited you.")  # the owner's text, as given
START = "INVITE_START", "Trade review: your access is ready", (
    "Your access to the trade review is ready on a normal website. You do not need Tailscale.\n\nOpen: {URL}\nEnter this email address, and type the six-digit code that arrives in your inbox. That is the whole sign-in.\n\n\"Continue\" opens your first form. For each one: read the post and its charts, record your own reading, and save it. Only then does the site show the system's reading and ask for your verdicts, including two questions on the system's proposed price series and trading instrument. The guide is linked at the top of every page.\n\nYou can stop at any time; saved work is kept, and \"Continue\" brings you back to the next unfinished step, from any device.\n\nIf the code does not arrive within two minutes, check spam, then tell the person who invited you.")  # the owner's text, as given


def bearer(token: str = TOKEN) -> dict:
    return {"Authorization": f"Bearer {token}"}


def call(app, headers=None, method: str = "POST", path: str = PATH, **kw):
    return app.test_client().open(path, method=method, base_url=ORIGIN, headers=headers or {},
                                  **kw)


def live(site, mailer=None, **config):  # noqa: F811
    """The site as production, with the operator's token and its own address configured."""
    return site.production(mailer, **{"OPERATOR_TOKEN": TOKEN, "SITE_URL": URL, **config})


def queue(site) -> list[tuple]:  # noqa: F811
    return [(m["alias"], m["kind"], m["sent_at"] is not None, m["result"], m["attempts"])
            for m in site.admin.queued_mail()]


def invited(site, alias):  # noqa: F811
    return next(r for r in site.admin.status() if r["alias"] == alias)["invited_at"]


def test_the_invitations_say_exactly_what_was_written():
    from reviewer_web import mail

    for kind, subject, text in (CONTINUE, START):
        assert mail.invitation(kind, URL) == (subject, text.replace("{URL}", URL))
        assert mail.INVITATIONS[kind] == (subject, text) and text.count("{URL}") == 1
    assert set(mail.INVITATIONS) == {"INVITE_CONTINUE", "INVITE_START"}


def test_without_the_operators_token_the_sender_is_a_path_that_does_not_exist(
        site, caplog):  # noqa: F811
    caplog.set_level(logging.DEBUG)
    site.admin.queue_mail(FIRST, "INVITE_CONTINUE")
    app = live(site)
    unknown = call(app, path="/internal/nothing")
    assert unknown.status_code == 404

    def same_as_unknown(response) -> bool:
        return (response.status_code, response.get_data(), dict(response.headers)) == (
            404, unknown.get_data(), dict(unknown.headers))

    wrong = [None, {}, {"Authorization": ""}, {"Authorization": TOKEN},
             {"Authorization": "Bearer"}, {"Authorization": "Bearer "},
             bearer(TOKEN[:-1]), bearer(TOKEN + "x"), bearer(TOKEN.upper()), bearer("x" * 43),
             {"Authorization": f"bearer {TOKEN}"}, {"Authorization": f"Basic {TOKEN}"},
             {"Authorization": f"Bearer  {TOKEN}"}, {"Authorization": f"Bearer {TOKEN} "},
             {"X-Authorization": f"Bearer {TOKEN}"}, {"Cookie": f"Authorization=Bearer {TOKEN}"}]
    for headers in wrong:
        assert same_as_unknown(call(app, headers)), headers
    # Only a POST: any other method, named or invented, is the unknown path too, and nothing
    # tells them apart (the review: TRACE and PROPFIND answered 405 here and 404 there).
    for method in ("GET", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE", "PROPFIND", "LINK",
                   "CONNECT", "FOO"):
        answer = call(app, bearer(), method)
        missing = call(app, method=method, path="/internal/nothing")
        assert answer.status_code == missing.status_code == 404 and "Allow" not in answer.headers
        assert (answer.get_data(), dict(answer.headers)) == (
            missing.get_data(), dict(missing.headers)), method
    assert call(app, bearer(), "HEAD").status_code == 404
    # A header that is not text a token could be: the same answer, not a failure.
    for odd in ("Bearer " + "é" * 40, "Bearer " + "x" * 100000, "Bearer \x7f\x01"):
        assert same_as_unknown(call(app, {"Authorization": odd.encode().decode("latin-1")}))
    # No token configured, or one too short to be a secret: the right-looking header fails.
    for token in (None, "", "short", "x" * 31):
        for headers in (bearer(token or ""), {"Authorization": "Bearer "}, {}):
            assert same_as_unknown(call(live(site, OPERATOR_TOKEN=token), headers)), token
    # Preview and test never send mail: the route is not there, whatever the token.
    for env in ("preview", "test"):
        quiet = site.production(SITE_ENV=env, OPERATOR_TOKEN=TOKEN, SITE_URL=URL)
        answer = call(quiet, bearer())
        assert answer.status_code == 404 and answer.get_data() == call(
            quiet, path="/internal/nothing").get_data()
    # Without the site's own address there is nothing to write into the message.
    for address in (None, "", "review.example.invalid", "http://review.example.invalid"):
        assert same_as_unknown(call(live(site, SITE_URL=address), bearer())), address
    logged = [r.getMessage() for r in caplog.records]
    assert logged.count("SITE_URL_MISSING") == 4 and "example.invalid" not in "\n".join(logged)
    assert not site.outbox.sent and queue(site) == [(FIRST, "INVITE_CONTINUE", False, None, 0)]
    assert invited(site, FIRST) is None


def test_a_call_with_the_token_sends_each_queued_invitation_once(site, caplog):  # noqa: F811
    caplog.set_level(logging.DEBUG)
    first, second = site.reviewers[FIRST], site.reviewers[SECOND]
    third = site.admin.upsert_reviewer("REMOTE-4", "third@example.invalid", name="Third Example",
                                       scope_sequences=40)
    site.admin.queue_mail(FIRST, "INVITE_CONTINUE")
    site.admin.queue_mail(SECOND, "INVITE_START")
    site.admin.queue_mail("REMOTE-4", "INVITE_START")
    site.admin.set_revoked("REMOTE-4", True)  # revoked after the row was queued
    site.sql("UPDATE reviewer SET invited_at = '2026-03-08T09:00:00Z' WHERE alias = %s", SECOND)
    earlier = invited(site, SECOND)
    app = live(site)
    # No session, no origin, no CSRF token; a query string and a body change nothing.
    answer = call(app, bearer(), path=PATH + "?limit=100&alias=REMOTE-4&to=x@example.invalid",
                  data=json.dumps({"to": "x@example.invalid", "kind": "INVITE_START"}),
                  content_type="application/json")
    assert answer.status_code == 200 and answer.get_json() == {"sent": 2, "failed": 0, "left": 0}
    assert answer.headers["Cache-Control"] == "private, no-store"
    assert answer.headers["Content-Type"] == "application/json"
    assert site.outbox.sent == [
        (first.login_shown, CONTINUE[1], CONTINUE[2].replace("{URL}", URL)),
        (second.login_shown, START[1], START[2].replace("{URL}", URL))]
    assert queue(site) == [(FIRST, "INVITE_CONTINUE", True, "SENT", 1),
                           (SECOND, "INVITE_START", True, "SENT", 1),
                           ("REMOTE-4", "INVITE_START", False, None, 0)]
    # The invitation is stamped on the reviewer's row once; an earlier stamp stands.
    assert invited(site, FIRST) is not None and invited(site, SECOND) == earlier
    assert invited(site, "REMOTE-4") is None
    # A second call sends nothing.
    again = call(app, bearer())
    assert again.get_json() == {"sent": 0, "failed": 0, "left": 0} and len(site.outbox.sent) == 2
    # Access restored: the row that waited goes out at the next call; nothing is sent twice.
    site.admin.set_revoked("REMOTE-4", False)
    assert call(app, bearer()).get_json() == {"sent": 1, "failed": 0, "left": 0}
    assert site.outbox.sent[-1][0] == third.login_shown and len(site.outbox.sent) == 3
    # Neither an answer nor a log line names anybody.
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "QUEUED_MAIL sent=2 failed=0 left=0" in logged
    for text in (logged, answer.get_data(as_text=True), repr(sorted(answer.headers.items()))):
        assert "@" not in text and "example.invalid" not in text and "Example" not in text
    # The pages behind sign-in still need a session: the token opens nothing else.
    for path in ("/", "/form/i07", "/guide"):
        assert call(app, bearer(), "GET", path).status_code == 303
    assert call(app, bearer(), path="/sign-out").status_code == 403


def test_the_sites_configured_address_is_written_into_the_invitation_and_nowhere_else(
        site):  # noqa: F811
    """One deployment answers under two host names: no page and no redirect may name a host.
    ``SITE_URL`` is for the invitation alone."""
    app = live(site)
    b = site.browser(app).sign_in(FIRST)
    b.review("i07", after=False)
    seen = [b.shown.response, b.get("/"), b.get("/continue"), b.get("/guide"), b.get("/fields"),
            b.get("/form/i01"), b.get("/sign-in"), site.browser(app).get("/sign-in"),
            b.post("/sign-out", {"_csrf": b.csrf()})]
    for response in seen:
        text = response.get_data(as_text=True) + repr(sorted(response.headers.items()))
        assert "review.example.invalid" not in text and "://" not in text
    site.admin.queue_mail(FIRST, "INVITE_START")
    assert call(app, bearer()).get_json() == {"sent": 1, "failed": 0, "left": 0}
    assert f"Open: {URL}\n" in site.outbox.sent[-1][2]


def test_a_message_that_fails_is_tried_three_times_and_five_go_out_per_call(
        site, caplog):  # noqa: F811
    from reviewer_web import mail

    caplog.set_level(logging.DEBUG)

    class Failing:
        def __init__(self, error):
            self.error, self.calls = error, 0

        def __call__(self, to, _subject, _text):
            self.calls += 1
            raise self.error(f"no delivery to {to}")

    site.admin.queue_mail(FIRST, "INVITE_CONTINUE")
    down = Failing(mail.Unreachable)
    app = live(site, down)
    assert [call(app, bearer()).get_json() for _ in range(4)] == [
        {"sent": 0, "failed": 1, "left": 1}, {"sent": 0, "failed": 1, "left": 1},
        {"sent": 0, "failed": 1, "left": 0}, {"sent": 0, "failed": 0, "left": 0}]
    assert down.calls == 3 and queue(site) == [(FIRST, "INVITE_CONTINUE", False,
                                                "MAIL_UNREACHABLE", 3)]
    assert invited(site, FIRST) is None
    # Given up: the operator may queue it again; a message that then goes out is stamped.
    with pytest.raises(A.AdjudicationError, match="ALREADY_QUEUED"):
        site.admin.queue_mail(FIRST, "INVITE_START")
        site.admin.queue_mail(FIRST, "INVITE_START")
    site.admin.queue_mail(FIRST, "INVITE_CONTINUE")
    broken = Failing(RuntimeError)
    assert call(live(site, broken), bearer()).get_json() == {"sent": 0, "failed": 2, "left": 2}
    assert broken.calls == 2  # each waiting row once per call
    assert call(live(site), bearer()).get_json() == {"sent": 2, "failed": 0, "left": 0}
    assert queue(site)[1:] == [(FIRST, "INVITE_START", True, "SENT", 2),
                               (FIRST, "INVITE_CONTINUE", True, "SENT", 2)]
    assert invited(site, FIRST) is not None
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "QUEUED_MAIL MAIL_UNREACHABLE error=Unreachable" in logged
    assert "QUEUED_MAIL MAIL_FAILED error=RuntimeError" in logged
    assert "@" not in logged and "no delivery" not in logged
    # At most five messages a call, oldest first.
    for n in range(7):
        site.admin.upsert_reviewer(f"REMOTE-{10 + n}", f"more{n}@example.invalid",
                                   scope_sequences=40)
        site.admin.queue_mail(f"REMOTE-{10 + n}", "INVITE_START")
    before = len(site.outbox.sent)
    assert call(live(site), bearer()).get_json() == {"sent": 5, "failed": 0, "left": 2}
    assert [to for to, _s, _t in site.outbox.sent[before:]] == [
        f"more{n}@example.invalid" for n in range(5)]
    assert call(live(site), bearer()).get_json() == {"sent": 2, "failed": 0, "left": 0}
    assert len(site.outbox.sent) == before + 7


def test_only_a_reviewer_who_can_use_the_site_is_invited_and_once_per_kind(site):  # noqa: F811
    site.admin.upsert_reviewer("TEST-1", "tester@example.invalid", scope_sequences=40,
                               is_test=True)
    refusals = (("REMOTE-9", "INVITE_START", "UNKNOWN_ALIAS"),
                ("TEST-1", "INVITE_START", "TEST_IDENTITY_IS_NOT_INVITED"),
                (FIRST, "INVITE_AGAIN", "UNKNOWN_MAIL_KIND"))
    for alias, kind, code in refusals:
        with pytest.raises(A.AdjudicationError, match=f"^{code}$"):
            site.admin.queue_mail(alias, kind)
    site.admin.set_revoked(SECOND, True)
    with pytest.raises(A.AdjudicationError, match="^REVOKED$"):
        site.admin.queue_mail(SECOND, "INVITE_START")
    site.admin.queue_mail(FIRST, "INVITE_CONTINUE")
    with pytest.raises(A.AdjudicationError, match="^ALREADY_QUEUED$"):
        site.admin.queue_mail(FIRST, "INVITE_CONTINUE")
    site.admin.queue_mail(FIRST, "INVITE_START")  # the other kind is its own row
    assert queue(site) == [(FIRST, "INVITE_CONTINUE", False, None, 0),
                           (FIRST, "INVITE_START", False, None, 0)]
    # The table itself holds no address and no text, and allows one waiting row of a kind.
    columns = [c for (c,) in site.sql("SELECT column_name FROM information_schema.columns "
                                      "WHERE table_name = 'mail_queue' ORDER BY ordinal_position")]
    assert columns == ["id", "reviewer_id", "kind", "queued_at", "sent_at", "result", "attempts",
                       "sending_at"]
    import psycopg

    with pytest.raises(psycopg.errors.UniqueViolation):
        site.sql("INSERT INTO mail_queue (reviewer_id, kind) VALUES (%s, 'INVITE_START')",
                 site.reviewers[FIRST].id)
    with pytest.raises(psycopg.errors.CheckViolation):
        site.sql("INSERT INTO mail_queue (reviewer_id, kind) VALUES (%s, 'NEWSLETTER')",
                 site.reviewers[FIRST].id)


def test_a_call_stopped_between_the_send_and_its_record_does_not_send_again(
        site, monkeypatch):  # noqa: F811
    """The review, on 24f78dd: the mailer ran before the try was recorded, in one transaction;
    a failure in between left the row unsent with no try counted, and the next call sent the
    invitation a second time."""
    from reviewer_web import store as S

    site.admin.queue_mail(FIRST, "INVITE_CONTINUE")
    app = live(site)
    done = S.Store.mail_done
    stopped = []

    def flaky(self, row, result):
        if not stopped:
            stopped.append(row)
            raise OSError("the function was stopped after the send")
        return done(self, row, result)

    monkeypatch.setattr(S.Store, "mail_done", flaky)
    first = call(app, bearer())
    assert first.status_code == 500 and len(site.outbox.sent) == 1  # it went out, unrecorded
    # The try was counted before the send: the row waits for the operator, not for a call.
    [row] = site.admin.queued_mail()
    assert (row["attempts"], row["sent_at"], row["result"]) == (1, None, None)
    assert row["sending_at"] is not None
    for _ in range(3):
        assert call(app, bearer()).get_json() == {"sent": 0, "failed": 0, "left": 0}
    assert len(site.outbox.sent) == 1
    # The operator, having asked the reviewer, queues it again deliberately.
    site.admin.queue_mail(FIRST, "INVITE_CONTINUE")
    assert call(app, bearer()).get_json() == {"sent": 1, "failed": 0, "left": 0}
    assert len(site.outbox.sent) == 2


def test_no_transaction_is_open_while_the_mail_server_is_spoken_to(site):  # noqa: F811
    import psycopg

    site.admin.queue_mail(FIRST, "INVITE_START")
    seen = {}

    def mailer(_to, _subject, _text):
        with psycopg.connect(site.dsn) as conn:
            seen["idle in transaction"] = conn.execute(
                "SELECT count(*) FROM pg_stat_activity WHERE state = 'idle in transaction'"
            ).fetchone()[0]
            seen["counted before the send"] = conn.execute(
                "SELECT attempts, sending_at IS NOT NULL, result FROM mail_queue").fetchone()

    assert call(live(site, mailer), bearer()).get_json() == {"sent": 1, "failed": 0, "left": 0}
    assert seen == {"idle in transaction": 0, "counted before the send": (1, True, None)}


def test_nobody_who_cannot_sign_in_is_invited(site):  # noqa: F811
    """The review, on 24f78dd: an invitation was queued and sent for a row that cannot use the
    site; that reviewer followed it and never received a code."""
    site.set_reviewer(SECOND, access=A.RESTRICTED)
    with pytest.raises(A.AdjudicationError, match="^CANNOT_SIGN_IN$"):
        site.admin.queue_mail(SECOND, "INVITE_START")
    site.set_reviewer(SECOND, access=A.FULL, role=A.APPROVER)
    with pytest.raises(A.AdjudicationError, match="^CANNOT_SIGN_IN$"):
        site.admin.queue_mail(SECOND, "INVITE_START")
    site.sql("INSERT INTO reviewer (alias, login, login_shown, name, role, access, "
             "scope_sequences) VALUES ('REMOTE-7', 'nobundle@example.invalid', "
             "'nobundle@example.invalid', '', 'REVIEWER', 'FULL', 40)")
    with pytest.raises(A.AdjudicationError, match="^NO_BUNDLE_ASSIGNED$"):
        site.admin.queue_mail("REMOTE-7", "INVITE_START")
    assert queue(site) == []
    # Queued while they could sign in, changed before the site sends: the message waits.
    site.set_reviewer(SECOND, access=A.FULL, role=A.REVIEWER)
    site.admin.queue_mail(SECOND, "INVITE_START")
    site.admin.queue_mail(FIRST, "INVITE_START")
    app = live(site)
    for change in ({"access": A.RESTRICTED}, {"role": A.APPROVER}):
        site.set_reviewer(SECOND, access=A.FULL, role=A.REVIEWER)
        site.set_reviewer(SECOND, **change)
        answer = call(app, bearer()).get_json()
        assert answer["left"] == 0 and site.reviewers[SECOND].login_shown not in [
            to for to, _s, _t in site.outbox.sent]
    assert [to for to, _s, _t in site.outbox.sent] == [site.reviewers[FIRST].login_shown]
    site.set_reviewer(SECOND, access=A.FULL, role=A.REVIEWER)
    assert call(app, bearer()).get_json() == {"sent": 1, "failed": 0, "left": 0}
    # The token is the whole authority of this route: a request that carries it is served
    # whatever its Origin says (a page cannot attach an Authorization header across sites).
    site.admin.queue_mail(FIRST, "INVITE_CONTINUE")
    crossing = {**bearer(), "Origin": "https://elsewhere.example.invalid",
                "Sec-Fetch-Site": "cross-site"}
    assert call(app, crossing).get_json() == {"sent": 1, "failed": 0, "left": 0}
