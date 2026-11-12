"""Typed exceptions and broker error-code classification (SDK-free)."""
from __future__ import annotations

from enum import Enum

from tradingbot.ibkr.redact import redact_accounts


class IbkrError(Exception):
    """Base class. Messages are passed through ``redact_accounts`` so no full account ID escapes."""

    def __init__(self, message: str) -> None:
        super().__init__(redact_accounts(message))


# --- configuration -------------------------------------------------------------------------------
class ConfigError(IbkrError):
    """Base for every fail-closed configuration refusal."""


class ConfigNotFound(ConfigError):
    pass


class ConfigIsSymlink(ConfigError):
    pass


class ConfigNotRegularFile(ConfigError):
    pass


class ConfigWrongOwner(ConfigError):
    pass


class ConfigPermissionsTooOpen(ConfigError):
    pass


class ConfigInvalid(ConfigError):
    """Unparseable TOML, missing/unknown keys or wrong value types."""


class NonLoopbackHost(ConfigError):
    pass


class PortNotAllowed(ConfigError):
    """Only the paper defaults (7497 TWS, 4002 Gateway) are allowed."""


class NonPaperAccount(ConfigError):
    pass


class ModeNotPaper(ConfigError):
    pass


# --- local environment preflight (before any socket is opened) ------------------------------------
class PaperModeNotConfirmed(IbkrError):
    """The TWS profile's jts.ini does not positively say paper mode (``[Logon] tradingMode=p``)."""

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


class ListenerNotConfirmed(IbkrError):
    """The process listening on host:port is not (provably) owned by the configured macOS user."""

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


class ListenerOwnerUndeterminable(ListenerNotConfirmed):
    """netstat/ps could not attribute the listener to a user (reason UNDETERMINABLE)."""

    def __init__(self, message: str) -> None:
        super().__init__(message, reason="UNDETERMINABLE")


# --- session -------------------------------------------------------------------------------------
class ReadOnlyViolation(BaseException):
    """A non-allowlisted EClient method or outgoing message was attempted.

    Deliberately a ``BaseException``: every ibapi request method wraps its send in
    ``except Exception`` and turns failures into an error callback, which would hide this.
    """


class UnsupportedServerVersion(IbkrError):
    pass


class ReadOnlyNotVerified(IbkrError):
    """Base: config-read mode could not positively verify the TWS API is read-only."""


class AccountIdentityError(IbkrError):
    """The broker's managedAccounts (or an account-scoped row) is not exactly the configured account."""

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


class IbkrTimeout(IbkrError):
    pass


class IbkrRequestError(IbkrError):
    """A broker error routed to a request (or to the connect phase)."""

    def __init__(self, code: int, message: str, req_id: int | None = None) -> None:
        super().__init__(f"IBKR error {code} (reqId {req_id}): {message}")
        self.code = code
        self.broker_message = redact_accounts(message)
        self.req_id = req_id


class ContractNotFound(IbkrError):
    pass


class AmbiguousContract(IbkrError):
    def __init__(self, message: str, *, count: int) -> None:
        super().__init__(message)
        self.count = count


class SessionStateError(IbkrError):
    """Method called in the wrong lifecycle state (e.g. a request before the identity gate passed)."""


class QuoteStreamLimit(SessionStateError):
    """Opening another held quote stream would exceed the session's stream cap. Nothing was sent."""


class QuoteStreamConflict(SessionStateError):
    """A quote request asked for a market-data type other than the open streams' type
    (``reqMarketDataType`` is session-wide). Nothing was sent."""


class QuoteStreamClosed(SessionStateError):
    """The held quote stream is closed (by close, disconnect, poison or connection loss)."""


class CleanupError(IbkrError):
    """Cancel/disconnect did not complete cleanly. Never folded into success."""

    def __init__(self, message: str, *, failures: list[str], result: object = None) -> None:
        super().__init__(f"{message}: {'; '.join(failures)}")
        self.failures = [redact_accounts(f) for f in failures]
        self.result = result


# --- config-read refusals (each followed by a clean disconnect) ----------------------------------
class ConfigReadUnsupported(ReadOnlyNotVerified):
    """TWS server version is below the one supporting reqConfigProtoBuf."""


class ConfigReadTimeout(ReadOnlyNotVerified, IbkrTimeout):
    """No configResponse arrived for our request in time."""


class ConfigReadRejected(ReadOnlyNotVerified):
    """TWS answered the config request with an error callback."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(f"config read rejected by TWS: {code} {message}")
        self.code = code


class ConfigReadUncorrelated(ReadOnlyNotVerified):
    """A configResponse whose reqId is missing or differs from the one we sent."""


class ConfigResponseChanged(ReadOnlyNotVerified):
    """A later config response differs from the verified first one: the session is poisoned."""


class ReadOnlySettingRefused(ReadOnlyNotVerified):
    """The response's readOnlyApi / socketPort / allowLocalhostOnly is absent or wrong."""

class IbkrConnectionError(IbkrError):
    """Socket/handshake failure or a connection lost while a request was outstanding."""


# --- broker error-code classification ------------------------------------------------------------
class ErrorClass(Enum):
    NOTICE = "NOTICE"  # informational (farm status etc.): logged, never fails a request
    CONNECTION = "CONNECTION"  # connection-level event
    MARKET_DATA_UNAVAILABLE = "MARKET_DATA_UNAVAILABLE"  # quote cannot be delivered (entitlement)
    ERROR = "ERROR"  # everything else: surfaced as IbkrRequestError


# Farm/status notices. 2100-2199 is IBKR's documented warning range; listed codes are the common ones.
NOTICE_CODES = frozenset({2100, 2103, 2104, 2105, 2106, 2107, 2108, 2119, 2150, 2157, 2158, 2168,
                          2169, 10167})
# 10167: "Requested market data is not subscribed. Displaying delayed market data." (data still flows)
CONNECTION_LOSS_CODES = frozenset({502, 504, 1100, 1300, 2110})
CONNECTION_RESTORED_CODES = frozenset({1101, 1102})
CONNECTION_CODES = CONNECTION_LOSS_CODES | CONNECTION_RESTORED_CODES
# Entitlement refusals for a quote request: reported as quality UNAVAILABLE, not raised.
MARKET_DATA_UNAVAILABLE_CODES = frozenset({354, 10089, 10168, 10186, 10197})


def classify_error(code: int) -> ErrorClass:
    if code in CONNECTION_CODES:
        return ErrorClass.CONNECTION
    if code in NOTICE_CODES or 2100 <= code <= 2199:
        return ErrorClass.NOTICE
    if code in MARKET_DATA_UNAVAILABLE_CODES:
        return ErrorClass.MARKET_DATA_UNAVAILABLE
    return ErrorClass.ERROR
