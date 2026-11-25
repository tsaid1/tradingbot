"""The hosted reviewer site: a Flask application over the store (docs/HOSTED_REVIEWER.md).

Two remote reviewers do here what they do in the Streamlit workspace: read a post with its
context and images, save an independent reading, then see the system's reading and save a reading
after the reveal. The server keeps nothing between requests: every page is computed from the
stored records, and every rule is the rubric's (``forms``).

Enforced on every route, before anything is read: a session (sign-in by an emailed one-time
code), a reviewer who is not revoked, role REVIEWER, access FULL, and an item inside that
reviewer's scope of that reviewer's OWN bundle (``store.bundle_for``: a test identity reads
invented forms beside the real reviewers, and neither ever gets a form, an image or a record of
the other's bundle). Anything else gets the sign-in page or a 404 without detail. The system's
reading, its mapping proposals, holds and price rows are put into a response only after the
SYSTEM_SHOWN markers are stored and read back. Logs carry constant codes and exception class
names: never post text, a reading, an address or a code.

One database connection and one transaction serve a request (``reviewer_route``): everything
is read and written inside it, it is committed, and only then is the page built from what was
read. So a marker or a save is durable before any page says so.

``create_app(store, mailer, config)`` builds the application; ``app`` (module level, built on
first use) is the one the platform serves, configured by the environment: ``DATABASE_URL``
(or ``SITE_DATABASE_URL``), ``SESSION_SECRET``, ``SITE_ENV``, the five SMTP variables of
``mail``, and for the operator's
queued invitations ``OPERATOR_TOKEN`` and ``SITE_URL`` (``from_environment``).
"""

from __future__ import annotations

import functools
import hmac
import inspect
import os
import re
import secrets
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from flask import Flask, Response, abort, g, jsonify, redirect, render_template, request

from tradingbot.runtime import adjudication as A
from tradingbot.runtime import adjudication_rubric as R

from . import auth, mail
from . import forms as F

STATIC = Path(__file__).resolve().parent.parent / "public" / "static"
SITE_ENVS = ("production", "preview", "test")
DEMO_ENVS = ("preview", "test")  # these show invented data only
PUBLIC = ("healthz", "robots", "favicon", "static")  # no session, no database
SIGN_IN = ("sign_in", "sign_in_code", "sign_in_verify")
OPERATOR = ("send_queued",)  # the one route without a session: its own token, or it is a 404
EVERY_METHOD = ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS")
MIN_OPERATOR_TOKEN = 32
MAIL_PER_CALL = 5
IMAGE_TYPES = ("image/png", "image/jpeg", "image/gif", "image/webp")
MAX_IMAGE_BYTES = 4 * 1024 * 1024  # the platform caps a response at 4.5 MB
MAX_REQUEST_BYTES = 512 * 1024
SESSION_SECONDS = int(auth.SESSION_VALID.total_seconds())
CSP = ("default-src 'self'; img-src 'self'; style-src 'self'; script-src 'self'; "
       "object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",  # nothing to another site; our own forms keep their Origin
    "X-Frame-Options": "DENY",
    "X-Robots-Tag": "noindex, nofollow",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
}
_KEY = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
_BUNDLE = re.compile(r"^[0-9a-f]{64}$")
MESSAGES = {
    400: "The request could not be read.",
    403: "The request was refused. Open the page again from the site and repeat it.",
    404: "Not found.",
    405: "Not found.",
    413: "The request is too large.",
    500: ("The page could not be completed. Reload it to send the same request again: a save is "
          "never stored twice."),
    503: "The site is not available.",
}


@dataclass(frozen=True)
class Who:
    """The signed-in reviewer of this request."""

    reviewer: object  # the store's ReviewerRow
    session: A.Session
    token: str
    demo: bool  # this reviewer's bundle is demonstration data: every page says so


def _code(exc: Exception) -> str:
    return getattr(exc, "code", None) or type(exc).__name__


def _cause(exc: Exception) -> str:
    """What a log line may say of a mail failure: a class name, never a message (a message may
    quote an address or a code). ``mail.Unreachable`` carries the class name of what failed."""
    named = str(exc) if isinstance(exc, mail.Unreachable) else type(exc).__name__
    return named if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,60}", named) else type(exc).__name__


def create_app(store, mailer, config: dict) -> Flask:
    """``store``: the contract's ``Store``. ``mailer(to, subject, text)``, or None for synthetic
    mail (preview and test only: the code is shown on the page). ``config``:
    ``SESSION_SECRET`` (at least 32 characters), ``SITE_ENV`` (production | preview | test);
    for the operator's queued mail ``OPERATOR_TOKEN`` (at least 32 characters) and ``SITE_URL``
    (the address the invitations name); and for tests and the development server ``CLOCK`` (a
    function returning the time) and ``SIGN_IN_FLOOR_SECONDS``."""
    secret = config.get("SESSION_SECRET") or ""
    secret = secret.encode("utf-8") if isinstance(secret, str) else bytes(secret)
    site_env = config.get("SITE_ENV") or "production"
    if len(secret) < 32:
        raise RuntimeError("SESSION_SECRET_TOO_SHORT")
    if site_env not in SITE_ENVS:
        raise RuntimeError("UNKNOWN_SITE_ENV")
    demo_env = site_env in DEMO_ENVS  # preview and test serve invented data only
    if demo_env and not store.bundle_is_demo():
        raise RuntimeError("NOT_A_DEMO_BUNDLE")  # a preview never shows anything but invented data
    # No mailer: "synthetic mail". Nothing is sent and the code-entry page shows the code in the
    # demonstration band. Only in preview and test; production never uses it, whoever signs in.
    synthetic = mailer is None
    if synthetic and not demo_env:
        raise RuntimeError("MAIL_NOT_CONFIGURED")
    clock = config.get("CLOCK") or (lambda: datetime.now(UTC))
    operator_token = str(config.get("OPERATOR_TOKEN") or "").encode("utf-8")
    site_url = str(config.get("SITE_URL") or "").strip()
    floor = float(config.get("SIGN_IN_FLOOR_SECONDS", 2.0))

    app = Flask(__name__, static_folder=str(STATIC), static_url_path="/static")
    app.config.update(MAX_CONTENT_LENGTH=MAX_REQUEST_BYTES, MAX_FORM_MEMORY_SIZE=MAX_REQUEST_BYTES,
                      MAX_FORM_PARTS=5000)
    app.jinja_env.filters["ident"] = lambda s: re.sub(r"[^A-Za-z0-9_-]", "-", str(s))
    app.jinja_env.trim_blocks = app.jinja_env.lstrip_blocks = True

    # ------------------------------------------------------------- every request, every response

    def refuse(status: int):
        """The one page of a refusal: the same for everybody, whoever asks and whatever for."""
        return render_template("message.html", message=MESSAGES[status], status=status,
                               who=None, csrf="", demo=demo_env), status

    def scheme() -> str:
        """The scheme the browser used: behind the platform's proxy that is what
        ``X-Forwarded-Proto`` says, not the scheme of the hop that reaches the application."""
        forwarded = request.headers.get("X-Forwarded-Proto", "").split(",")[0].strip().lower()
        return forwarded if forwarded in ("https", "http") else request.scheme

    def same_origin() -> bool:
        """A request that changes something must come from a page of this site. An ``Origin``,
        when sent, must be this site's own (``null`` is not); ``Sec-Fetch-Site``, when sent,
        must say ``same-origin``; a request with neither is refused. The site's own origin is
        that of THIS request (its scheme and its ``Host``), so the same deployment answers
        under each of its host names and never takes one for the other."""
        site, origin = request.headers.get("Sec-Fetch-Site"), request.headers.get("Origin")
        if origin is None and site is None:
            return False
        if origin is not None and origin != f"{scheme()}://{request.host}":
            return False
        return site is None or site == "same-origin"

    def network() -> str:
        """The caller's network address, for the limits only (the platform sets these headers)."""
        forwarded = request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
        return request.headers.get("X-Real-Ip") or forwarded or request.remote_addr or "-"

    @app.before_request
    def guard():
        """Before any route: a POST from another origin is refused, and a signed-in browser
        does not see the sign-in pages."""
        g.who = None
        if request.endpoint is None or request.endpoint in (*PUBLIC, *OPERATOR):
            return None
        if request.method == "POST" and not same_origin():
            return refuse(403)
        if request.endpoint in SIGN_IN and auth.session_reviewer(
                store, request.cookies.get(auth.COOKIE, ""), clock()) is not None:
            return redirect("/", 303)
        return None

    def reads_demo(reviewer) -> bool | None:
        """Whether the reviewer's bundle is invented data; None: no bundle is assigned to them."""
        try:
            return store.is_demo(reviewer)
        except A.AdjudicationError:
            return None

    def admit():
        """Who is asking, or the refusal. Runs first in every reviewer route."""
        token = request.cookies.get(auth.COOKIE, "")
        reviewer = auth.session_reviewer(store, token, clock())
        if reviewer is None:
            if request.endpoint == "media":
                abort(404)
            return redirect("/sign-in", 303)
        if request.method == "POST" and not hmac.compare_digest(
                request.form.get("_csrf", "").encode("utf-8"),
                auth.csrf_token(secret, token).encode("utf-8")):
            return refuse(403)
        demo = reads_demo(reviewer)
        if demo is None:
            return refuse(503)  # no bundle is assigned to this reviewer: nothing is served
        if demo_env and not (demo and store.bundle_is_demo()):
            return refuse(503)  # a preview serves nobody whose bundle is not invented
        g.who = Who(reviewer, reviewer.session(), token, demo)
        return None

    def reviewer_route(view):
        """A route behind sign-in. One connection and one transaction for the whole request;
        the view returns a response, or a function that builds the page from what it read:
        that function runs after the commit, and touches the store no more."""
        @functools.wraps(view)
        def route(**kw):
            with store.connection():
                answer = admit()
                if answer is None:
                    answer = view(**kw)
            return answer() if inspect.isfunction(answer) else answer
        return route

    @app.after_request
    def headers(response):
        for name, value in HEADERS.items():
            response.headers[name] = value
        # Nothing behind sign-in, and no sign-in page, may be kept by a browser or a proxy.
        served = request.endpoint == "static" and response.status_code in (200, 304)
        response.headers["Cache-Control"] = "no-cache" if served else "private, no-store"
        if scheme() == "https":
            response.headers["Strict-Transport-Security"] = "max-age=63072000"
        return response

    @app.context_processor
    def common():
        who = g.get("who")
        return {"demo": who.demo if who else demo_env, "who": who,
                "site_name": mail.SITE_NAME, "synthetic": synthetic,
                "csrf": auth.csrf_token(secret, who.token) if who else ""}

    for status in (400, 403, 404, 405, 413):
        app.register_error_handler(status, lambda _exc, status=status: refuse(status))

    @app.errorhandler(Exception)
    def failed(exc):
        where = exc.__traceback__
        while where is not None and where.tb_next is not None:
            where = where.tb_next
        place = f"{Path(where.tb_frame.f_code.co_filename).name}:{where.tb_lineno}" if where \
            else "-"
        app.logger.error("REQUEST_FAILED endpoint=%s error=%s at=%s", request.endpoint,
                         type(exc).__name__, place)  # never the message: it may quote content
        return refuse(500)

    # ------------------------------------------------------------- public

    @app.get("/healthz")
    def healthz():
        return Response("ok\n", mimetype="text/plain")

    @app.get("/robots.txt")
    def robots():
        return Response("User-agent: *\nDisallow: /\n", mimetype="text/plain")

    @app.get("/favicon.ico")
    def favicon():
        return Response(status=204)  # browsers ask; the site has no image of its own

    # ------------------------------------------------------------- the operator's queued mail

    @app.route("/internal/send-queued", methods=EVERY_METHOD, provide_automatic_options=False)
    def send_queued():
        """Send the invitations the operator queued (``hosted_admin.py invite``): the mail
        password exists only here. No session, no parameters, no body: the operator's token in
        ``Authorization: Bearer``, or the answer is that of a path that does not exist (also in
        preview and test, which never send mail, and for any method but POST). At most five
        messages a call, one sender at a time, each try recorded as it happens. The answer is
        three counts; no address is returned or logged."""
        given = request.headers.get("Authorization", "").encode("utf-8")
        if request.method != "POST" or demo_env or len(operator_token) < MIN_OPERATOR_TOKEN \
                or not hmac.compare_digest(given, b"Bearer " + operator_token):
            return refuse(404)
        if not site_url.startswith("https://"):
            app.logger.error("SITE_URL_MISSING")
            return refuse(404)
        sent, failed, tried = 0, 0, []
        for _ in range(MAIL_PER_CALL):
            with store.connection():  # one message, one transaction: its result is kept
                due = store.mail_next(tried)
                if due is None:
                    break
                row, kind, to = due
                tried.append(row)
                try:
                    mailer(to, *mail.invitation(kind, site_url))
                    result = "SENT"
                except mail.Unreachable as exc:
                    result = "MAIL_UNREACHABLE"
                    app.logger.error("QUEUED_MAIL %s error=%s", result, _cause(exc))
                except Exception as exc:  # noqa: BLE001 - a constant code, no detail
                    result = "MAIL_FAILED"
                    app.logger.error("QUEUED_MAIL %s error=%s", result, _cause(exc))
                store.mail_done(row, result)
            sent, failed = sent + (result == "SENT"), failed + (result != "SENT")
        left = store.mail_left()
        app.logger.info("QUEUED_MAIL sent=%d failed=%d left=%d", sent, failed, left)
        return jsonify(sent=sent, failed=failed, left=left)

    # ------------------------------------------------------------- sign-in and sign-out

    @app.get("/sign-in")
    def sign_in():
        return render_template("sign_in.html")

    @app.post("/sign-in")
    def sign_in_code():
        """Answers the same for every address, listed or not, and takes about as long.

        The page never carries the code (synthetic mail apart, which production never uses).
        When the mail server cannot be reached the page says the code could not be sent, for
        every address alike: an address that gets no code asks the server too (``probe``) and
        sends nothing. A failure of the one message (which no unlisted address could meet) is
        logged and not shown: showing it would say that the address is listed. Either way the
        code that was not delivered is ended."""
        started = time.monotonic()
        address = request.form.get("address", "")[:auth.MAX_ADDRESS]
        if synthetic and not store.bundle_is_demo():
            return refuse(503)  # a code is shown on a page only beside invented data
        asked = auth.request_code(store, secret, address, network(), clock())
        if synthetic and asked.code is not None and not reads_demo(asked.reviewer):
            auth.cancel_codes(store, asked.reviewer, clock())  # never for a real bundle's reader
            asked = auth.Asked(True)
        unsent = False
        if not synthetic:
            try:
                if asked.code is not None:
                    mailer(asked.reviewer.login_shown, auth.SUBJECT, auth.mail_text(asked.code))
                elif asked.allowed and hasattr(mailer, "probe"):
                    mailer.probe()
            except mail.Unreachable as exc:
                app.logger.error("MAIL_UNREACHABLE error=%s", _cause(exc))
                unsent = True
            except Exception as exc:  # noqa: BLE001 - whatever failed: a constant code, no detail
                app.logger.error("MAIL_FAILED error=%s", _cause(exc))
            else:
                asked = asked._replace(code=None)  # delivered, or none was made: nothing to end
            if asked.code is not None:
                auth.cancel_codes(store, asked.reviewer, clock())
        time.sleep(max(0.0, floor - (time.monotonic() - started)))
        return render_template("sign_in_code.html", address=address, failed=False, unsent=unsent,
                               shown_code=asked.code if synthetic else None)

    @app.post("/sign-in/verify")
    def sign_in_verify():
        address = request.form.get("address", "")[:auth.MAX_ADDRESS]
        token = auth.verify_code(store, secret, address, request.form.get("code", "")[:32],
                                 network(), clock())
        if token is None:
            return render_template("sign_in_code.html", address=address, failed=True,
                                   unsent=False, shown_code=None), 401
        response = redirect("/", 303)
        response.set_cookie(auth.COOKIE, token, max_age=SESSION_SECONDS, secure=True,
                            httponly=True, samesite="Lax", path="/")
        return response

    @app.post("/sign-out")
    @reviewer_route
    def sign_out():
        auth.end_session(store, g.who.token)
        response = redirect("/sign-in", 303)
        response.delete_cookie(auth.COOKIE, secure=True, httponly=True, samesite="Lax", path="/")
        return response

    # ------------------------------------------------------------- the workspace

    @app.get("/")
    @reviewer_route
    def workspace():
        bundle = store.bundle_for(g.who.reviewer)
        records = store.records_of(g.who.reviewer)
        return lambda: render_template("workspace.html",
                                       **F.workspace(bundle, g.who.session, records))

    @app.get("/continue")
    @reviewer_route
    def resume():
        bundle = store.bundle_for(g.who.reviewer)
        step = F.next_step(bundle, g.who.session, store.records_of(g.who.reviewer))
        return redirect(f"/form/{step}" if step else "/", 303)

    @app.get("/guide")
    @reviewer_route
    def guide():
        return render_template("guide.html", blocks=F.guide_blocks(R.render_guide_markdown()),
                               how=R.HOW_TO_REVIEW)

    @app.get("/fields")
    @reviewer_route
    def fields():
        return render_template("fields.html", **F.field_reference())

    # ------------------------------------------------------------- one form

    def item_of(item_id: str):
        """This reviewer's bundle and the item, only when the item is in their scope of it."""
        bundle = store.bundle_for(g.who.reviewer)
        item = F.scoped(bundle, g.who.session).get(item_id)
        if item is None:
            abort(404)
        return bundle, item

    def reveal(bundle, item):
        """The reviewer's records, where the form stands, and a code when the reveal failed.
        Once every form of the post has an independent reading (or the system's reading was
        seen before), the SYSTEM_SHOWN markers are stored FIRST and read back: only a gate
        that is ``marked`` lets anything of the system into a response."""
        who = g.who
        records = store.records_of(who.reviewer)
        gate = F.gate(bundle, who.session, item, records)
        problem = None
        if gate.open and not gate.marked:
            def markers(current):
                if not F.gate(bundle, who.session, item, current).open:
                    return None
                return A.build_system_shown(current, bundle, who.session, item["item_id"])

            try:
                store.append(who.reviewer, markers)
            except Exception as exc:  # noqa: BLE001 - whatever failed, nothing is shown
                problem = _code(exc) if isinstance(exc, A.AdjudicationError) \
                    else type(exc).__name__
                app.logger.error("MARKER_NOT_STORED error=%s", problem)
                return records, gate, problem  # the gate as it was: not marked
            records = store.records_of(who.reviewer)
            gate = F.gate(bundle, who.session, item, records)
            if not gate.marked:
                problem = "MARKER_NOT_STORED"
        return records, gate, problem

    def build(bundle, item, records, gate, phase, posted):
        """The form of one phase. The system's reading is read here only for the phase after
        the reveal, and the callers allow that phase only for a gate that is marked."""
        after = phase == A.AFTER_REVEAL
        source = (gate.after or gate.ind) if after else None
        return F.build(bundle, item, records, g.who.session, phase=phase,
                       system=A.system_reading(bundle, item) if after else None,
                       defaults=source["reading"] if source else {}, posted=posted,
                       notice=F.saved_notice(gate.ind, gate.after, gate.returned)
                       if after else None)

    def phase_of(bundle, item, records, gate) -> str | None:
        """The form the page of ``item`` shows, by ``forms.step_state`` (the one function that
        says where a form stands): the independent form, no form while the post waits for
        another form's reading, else the form after the reveal. That one needs the markers:
        without them (they could not be stored) no form is shown."""
        state = F.step_state(bundle, g.who.session, records, item["item_id"])
        if state == F.INDEPENDENT_DUE:
            return A.INDEPENDENT
        return A.AFTER_REVEAL if state != F.REVEAL_PENDING and gate.marked else None

    def page(bundle, item, records, gate, problem, *, posted=None, refusal=None, saved=None,
             status=200):
        """The form page as it stands now. ``posted`` is used only when it belongs to the form
        the page shows (the same phase)."""
        who, item_id = g.who, item["item_id"]
        phase = phase_of(bundle, item, records, gate)
        revealed = phase == A.AFTER_REVEAL
        if posted is not None and posted.get("_phase") != phase:
            posted = None
        step = F.step_from(bundle, who.session, records, item)
        return render_template(
            "form.html", item_id=item_id, bundle_id=bundle.bundle_id,
            names=F.names(bundle, who.session),
            status=F.STATUS_TEXT[gate.state],
            returned=gate.returned is not None and revealed,
            not_blind=revealed and gate.ind is None,
            evidence=F.evidence(bundle, item), header=F.form_header(bundle, item),
            how=R.HOW_TO_REVIEW, verdict_help=R.VERDICT_HELP,
            overall=F.overall(bundle, item, records, who.session.login),
            form=build(bundle, item, records, gate, phase, posted) if phase else None,
            key=secrets.token_urlsafe(24), revealed=revealed,
            system=F.system_block(bundle, item) if revealed else None,
            described=F.describe(gate.ind) if gate.ind is not None and not revealed else None,
            waiting=[i for i in gate.waiting if i != item_id], problem=problem,
            refusal=refusal, refusal_text=F.REFUSALS.get((refusal or "").split(":")[0], ""),
            saved=saved, next_step=step), status

    def saved_record(bundle, records, item_id: str, seq: str):
        """The reviewer's own stored reading that a confirmation names, or None."""
        return next((r for r in A._of(records, item_id, bundle.bundle_id,
                                      login=g.who.session.login)
                     if str(r.get("seq")) == seq
                     and r["phase"] in (A.INDEPENDENT, A.AFTER_REVEAL)), None)

    @app.get("/form/<item_id>")
    @reviewer_route
    def form(item_id):
        bundle, item = item_of(item_id)
        records, gate, problem = reveal(bundle, item)
        saved = saved_record(bundle, records, item_id, request.args.get("saved", ""))
        return lambda: page(bundle, item, records, gate, problem, saved=saved)

    @app.post("/form/<item_id>/state")
    @reviewer_route
    def state(item_id):
        """The applicability round trip: the form again, for the answers the page holds now.
        Saves nothing. A script asks for the form's body only; without one the whole page."""
        bundle, item = item_of(item_id)
        records, gate, problem = reveal(bundle, item)
        phase = phase_of(bundle, item, records, gate)
        if request.headers.get("X-Fragment") != "form":
            return lambda: page(bundle, item, records, gate, problem, posted=request.form)
        if phase is None or request.form.get("_phase") != phase:
            return Response("", status=409)  # the form moved on: the script reloads the page

        def fragment():
            response = Response(render_template(
                "_form_body.html", item_id=item_id, key=secrets.token_urlsafe(24),
                form=build(bundle, item, records, gate, phase, request.form)),
                mimetype="text/html")
            response.headers["X-Fragment"] = "form"
            return response
        return fragment

    @app.post("/form/<item_id>/save")
    @reviewer_route
    def save(item_id):
        """ONE record per idempotency key: a second POST with the same key (a double click, a
        retry after a dropped connection) stores nothing and shows the saved state."""
        bundle, item = item_of(item_id)
        who, phase, key = g.who, request.form.get("_phase"), request.form.get("_key", "")
        if phase not in (A.INDEPENDENT, A.AFTER_REVEAL) or not _KEY.fullmatch(key):
            return refuse(400)

        def make(current):
            """Under the store's lock, with the reviewer's current records."""
            gate = F.gate(bundle, who.session, item, current)
            if phase == A.AFTER_REVEAL and phase_of(bundle, item, current, gate) != phase:
                raise A.AdjudicationError("NOT_REVEALED")
            filled = build(bundle, item, current, gate, phase, request.form)
            if filled.invalid:
                raise A.AdjudicationError(filled.invalid)
            after = phase == A.AFTER_REVEAL
            return A.build_annotation(
                current, bundle, who.session, item_id=item_id, phase=phase,
                blind=gate.ind is not None if after else True, reading=filled.reading,
                field_verdicts=filled.verdicts if after else None, flags=filled.flags,
                confidence=filled.confidence, notes=filled.notes,
                refers_to=gate.ind["seq"] if after and gate.ind is not None else None,
                acknowledged_warnings=filled.ack)

        try:
            done = store.append(who.reviewer, make, idempotency_key=key)
        except A.AdjudicationError as exc:
            if exc.code == "REVOKED":
                auth.end_session(store, who.token)
                return redirect("/sign-in", 303)
            app.logger.info("SAVE_REFUSED code=%s", exc.code.split(":")[0])
            records, gate, problem = reveal(bundle, item)
            code = exc.code
            return lambda: page(bundle, item, records, gate, problem, posted=request.form,
                                refusal=code, status=422)
        return lambda: redirect(f"/form/{item_id}?saved={done.records[0]['seq']}", 303)

    @app.get("/media/<bundle_id>/<post_ref>/<name>")
    @reviewer_route
    def media(bundle_id, post_ref, name):
        """An image of this reviewer's bundle, only for a post one of their forms shows and only
        by a name that post lists. Anything else (another bundle's image too): 404, no detail."""
        if not _BUNDLE.fullmatch(bundle_id):
            abort(404)
        bundle = store.bundle_for(g.who.reviewer)
        post = bundle.posts.get(post_ref) if bundle_id == bundle.bundle_id else None
        if post is None or post_ref not in F.media_posts(bundle, g.who.session) \
                or name not in post["media"]:
            abort(404)
        found = store.media(g.who.reviewer, post_ref, name)
        if found is None or found[1] not in IMAGE_TYPES or len(found[0]) > MAX_IMAGE_BYTES:
            abort(404)
        response = Response(found[0], mimetype=found[1])
        response.headers["Content-Disposition"] = "inline"
        return response

    return app


def from_environment(env=os.environ) -> Flask:
    """The application the platform serves. Mail: all five SMTP variables, or none of them.
    With none, ``preview`` and ``test`` show the code on the page beside the demonstration data
    (no mail leaves); ``production`` refuses to start.

    The platform names the kind of deployment itself (``VERCEL_ENV``). A deployment it does not
    call production never starts as production, whatever variables it inherited: a preview of
    any branch would otherwise serve, and write to, the real database.

    The database is ``SITE_DATABASE_URL`` when that is set (the connection of a role that may
    read and append but does not own the tables), else ``DATABASE_URL``."""
    from .store import Store

    site_env = env.get("SITE_ENV") or "production"
    if site_env == "production" and (env.get("VERCEL_ENV") or "production") != "production":
        raise RuntimeError("PREVIEW_DEPLOYMENT_NEEDS_SITE_ENV")
    with_mail = mail.configured(env)
    if with_mail is None or (not with_mail and site_env not in DEMO_ENVS):
        raise RuntimeError("MAIL_NOT_CONFIGURED")
    database = env.get("SITE_DATABASE_URL") or env["DATABASE_URL"]
    return create_app(Store(database), mail.Smtp(env) if with_mail else None,
                      {"SESSION_SECRET": env.get("SESSION_SECRET", ""), "SITE_ENV": site_env,
                       "OPERATOR_TOKEN": env.get("OPERATOR_TOKEN", ""),
                       "SITE_URL": env.get("SITE_URL", "")})


def __getattr__(name: str):
    """``from reviewer_web.app import app``: built from the environment on first use, so that
    importing ``create_app`` (tests, the development server) needs no environment."""
    if name == "app":
        globals()["app"] = from_environment()
        return globals()["app"]
    raise AttributeError(name)
