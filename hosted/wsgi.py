"""The platform's entry: the WSGI application, built from the environment (``reviewer_web.app``).
The project root on the platform holds this folder's contents and the runtime modules the app
imports (``tradingbot``), so both are importable from here."""

from reviewer_web.app import app  # noqa: F401
