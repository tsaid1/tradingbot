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


class LivePortRejected(ConfigError):
    pass


class NonPaperAccount(ConfigError):
    pass


class ModeNotPaper(ConfigError):
    pass


# --- session -------------------------------------------------------------------------------------
class IbkrConnectionError(IbkrError):
    """Socket/handshake failure or a connection lost while a request was outstanding."""


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


class CleanupError(IbkrError):
    """Cancel/disconnect did not complete cleanly. Never folded into success."""

    def __init__(self, message: str, *, failures: list[str], result: object = None) -> None:
        super().__init__(f"{message}: {'; '.join(failures)}")
        self.failures = [redact_accounts(f) for f in failures]
        self.result = result


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
