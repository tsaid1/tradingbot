"""Shared test helpers for the two reviewer apps: the server options a page run needs, LOCAL or
PUBLISHED mode, and request headers as a browser (LOCAL) or the proxy (PUBLISHED) would send."""

from __future__ import annotations

import pytest
from streamlit import config as st_config
from streamlit.runtime.context import ContextProxy, StreamlitHeaders

from tradingbot.runtime import vocab_recommend as R

PUB = "adj.example.ts.net"
_KEYS = ("server.address", "browser.gatherUsageStats", "client.showErrorDetails",
         "server.allowedHosts", "server.corsAllowedOrigins")


@pytest.fixture
def local(monkeypatch):
    """LOCAL mode, with the options both apps require; everything restored afterwards."""
    saved = {k: st_config.get_option(k) for k in _KEYS}
    st_config.set_option("server.address", "127.0.0.1")
    st_config.set_option("browser.gatherUsageStats", False)
    st_config.set_option("client.showErrorDetails", "none")
    for k in (R.PUBLISHED_HOST_ENV, R.LOCAL_IDENTITY_ENV):
        monkeypatch.delenv(k, raising=False)
    yield
    for k, v in saved.items():
        st_config.set_option(k, v)


def published(monkeypatch) -> None:
    """Switch the running test into PUBLISHED mode at ``PUB`` (needs the ``local`` fixture)."""
    monkeypatch.setenv(R.PUBLISHED_HOST_ENV, PUB)
    monkeypatch.delenv(R.LOCAL_IDENTITY_ENV, raising=False)
    st_config.set_option("server.allowedHosts", [PUB])
    st_config.set_option("server.corsAllowedOrigins", [f"https://{PUB}"])


def base_headers() -> dict[str, str]:
    import os

    if os.environ.get(R.PUBLISHED_HOST_ENV):
        return {"Host": PUB, "Origin": f"https://{PUB}"}
    port = st_config.get_option("server.port")
    return {"Host": f"127.0.0.1:{port}", "Origin": f"http://127.0.0.1:{port}"}


def with_headers(monkeypatch, **extra) -> None:
    """The request headers the page sees: the mode's Host and Origin, plus ``extra``."""
    items = list({**base_headers(), **extra}.items())
    monkeypatch.setattr(ContextProxy, "headers",
                        property(lambda self: StreamlitHeaders(items)))
