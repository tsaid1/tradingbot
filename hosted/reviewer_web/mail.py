"""Mail for the hosted reviewer site: one function, ``send(to, subject, text)``.

Production sends through SMTP, configured by five environment variables, all required:
``SMTP_HOST``, ``SMTP_PORT`` (465: TLS from the first byte; any other port: STARTTLS, required),
``SMTP_USER``, ``SMTP_PASSWORD``, ``MAIL_FROM``. A message is plain text, from
``Trade review <MAIL_FROM>``, with its own ``Date`` and ``Message-ID``. Tests and the development
server pass an ``Outbox`` instead. Nothing here logs: a message carries a one-time code.
"""

from __future__ import annotations

import os
import smtplib
import ssl
from datetime import UTC, datetime
from email.message import EmailMessage
from email.utils import format_datetime, formataddr, make_msgid

SITE_NAME = "Trade review"
VARIABLES = ("SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD", "MAIL_FROM")
TIMEOUT_SECONDS = 15
IMPLICIT_TLS_PORT = 465


def configured(env=os.environ) -> bool | None:
    """True: all five variables are set. False: none is. None: some are (a mistake)."""
    given = [bool(env.get(name)) for name in VARIABLES]
    return True if all(given) else False if not any(given) else None


# The two invitations the site sends for the operator (``/internal/send-queued``). Their words
# are the product owner's; only ``{URL}`` (the site's address) is filled in. No name is used.
INVITATIONS = {
    "INVITE_CONTINUE": (
        "The review site has moved: continue where you left off",
        ("The trade review has moved to a normal website. You no longer need Tailscale, and it is "
         "available at any hour.\n\n"
         "Open: {URL}\n"
         "Enter this email address, and type the six-digit code that arrives in your inbox. That "
         "is the whole sign-in.\n\n"
         "Everything you saved is there. \"Continue\" takes you to your next unfinished step. If "
         "you had saved your own reading of a form and not yet answered the questions shown "
         "afterwards, that form opens at those questions with your own reading intact.\n\n"
         "One change after you save your own reading: two questions now stand on their own, each "
         "beside what the system proposed. The first asks whether the system's price series is "
         "the one the post's numbers use. The second asks whether trading the system's proposed "
         "instrument would faithfully follow the publisher's idea. Answer Yes, No or Unsure.\n\n"
         "Please stop using the old address. If the code does not arrive within two minutes, "
         "check spam, then tell the person who invited you.")),
    "INVITE_START": (
        "Trade review: your access is ready",
        ("Your access to the trade review is ready on a normal website. You do not need "
         "Tailscale.\n\n"
         "Open: {URL}\n"
         "Enter this email address, and type the six-digit code that arrives in your inbox. That "
         "is the whole sign-in.\n\n"
         "\"Continue\" opens your first form. For each one: read the post and its charts, record "
         "your own reading, and save it. Only then does the site show the system's reading and "
         "ask for your verdicts, including two questions on the system's proposed price series "
         "and trading instrument. The guide is linked at the top of every page.\n\n"
         "You can stop at any time; saved work is kept, and \"Continue\" brings you back to the "
         "next unfinished step, from any device.\n\n"
         "If the code does not arrive within two minutes, check spam, then tell the person who "
         "invited you.")),
}


def invitation(kind: str, url: str) -> tuple[str, str]:
    """(subject, text) of an invitation, with the site's address in it."""
    subject, text = INVITATIONS[kind]
    return subject, text.replace("{URL}", url)


class Unreachable(Exception):
    """The mail server could not be reached or would not let the site in: no message, to
    anybody, can go out now. Carries the class name of what failed, nothing else."""


def _connect(env) -> smtplib.SMTP:
    """A signed-in connection: TLS before anything is said that matters. Any failure up to the
    sign-in is ``Unreachable``: it would meet every message alike."""
    host, port = env["SMTP_HOST"], int(env["SMTP_PORT"])
    context = ssl.create_default_context()
    server = None
    try:
        if port == IMPLICIT_TLS_PORT:
            server = smtplib.SMTP_SSL(host, port, timeout=TIMEOUT_SECONDS, context=context)
        else:
            server = smtplib.SMTP(host, port, timeout=TIMEOUT_SECONDS)
            server.starttls(context=context)  # raises when the server does not offer it
        server.login(env["SMTP_USER"], env["SMTP_PASSWORD"])
    except Exception as exc:  # noqa: BLE001 - whatever it was: its class name, no detail
        if server is not None:
            server.close()
        raise Unreachable(type(exc).__name__) from None
    return server


def send(to: str, subject: str, text: str, env=os.environ) -> None:
    """Send one plain-text message. Raises on any failure (the caller logs a constant code):
    ``Unreachable`` before the server accepted the site, anything else for this message."""
    sender = env["MAIL_FROM"]
    message = EmailMessage()
    message["From"] = formataddr((SITE_NAME, sender))
    message["To"], message["Subject"] = to, subject
    message["Date"] = format_datetime(datetime.now(UTC))
    message["Message-ID"] = make_msgid(domain=sender.rpartition("@")[2] or None)
    message.set_content(text)
    with _connect(env) as server:
        server.send_message(message)


def probe(env=os.environ) -> None:
    """Reach the mail server and sign in, sending nothing. Raises ``Unreachable`` when no
    message could be sent now. The sign-in page asks this for an address that gets no code, so
    that its answer does not depend on whether the address is listed when the mail server is
    down."""
    with _connect(env) as server:
        server.noop()


class Smtp:
    """The production mailer: ``send`` and ``probe`` bound to one environment."""

    def __init__(self, env=os.environ):
        self.env = env

    def __call__(self, to: str, subject: str, text: str) -> None:
        send(to, subject, text, self.env)

    def probe(self) -> None:
        probe(self.env)


class Outbox:
    """An in-memory mailer: ``outbox(to, subject, text)`` keeps the message in ``sent``."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []
        self.probes = 0

    def __call__(self, to: str, subject: str, text: str) -> None:
        self.sent.append((to, subject, text))

    def probe(self) -> None:
        self.probes += 1
