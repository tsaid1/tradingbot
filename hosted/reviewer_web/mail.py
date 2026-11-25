"""Mail for the hosted reviewer site: one function, ``send(to, subject, text)``.

Production sends through SMTP, configured by the environment: ``SMTP_HOST``, ``SMTP_PORT`` (465:
TLS from the first byte; anything else: STARTTLS, required), ``SMTP_USER``, ``SMTP_PASSWORD``,
``MAIL_FROM``. Tests and the development server pass an ``Outbox`` instead. Nothing here logs: a
message carries a one-time code.
"""

from __future__ import annotations

import os
import smtplib
import ssl
from email.message import EmailMessage

TIMEOUT_SECONDS = 10


def send(to: str, subject: str, text: str, env=os.environ) -> None:
    """Send one plain-text message. Raises on any failure (the caller logs a constant code)."""
    message = EmailMessage()
    message["From"], message["To"], message["Subject"] = env["MAIL_FROM"], to, subject
    message.set_content(text)
    host, port = env["SMTP_HOST"], int(env.get("SMTP_PORT") or 587)
    context = ssl.create_default_context()
    if port == 465:
        server = smtplib.SMTP_SSL(host, port, timeout=TIMEOUT_SECONDS, context=context)
    else:
        server = smtplib.SMTP(host, port, timeout=TIMEOUT_SECONDS)
    with server:
        if port != 465:
            server.starttls(context=context)  # raises when the server does not offer it
        if env.get("SMTP_USER"):
            server.login(env["SMTP_USER"], env.get("SMTP_PASSWORD", ""))
        server.send_message(message)


class Outbox:
    """An in-memory mailer: ``outbox(to, subject, text)`` keeps the message in ``sent``."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []

    def __call__(self, to: str, subject: str, text: str) -> None:
        self.sent.append((to, subject, text))
