"""Identity-gate decision (SDK-free): the broker's managedAccounts must be exactly one configured account.

The expected account always comes from the private config, never from anything the broker returned.
"""
from __future__ import annotations

from tradingbot.ibkr.errors import AccountIdentityError
from tradingbot.ibkr.redact import mask_account


def parse_managed_accounts(accounts_list: str) -> list[str]:
    """TWS sends a comma-separated list, sometimes with a trailing comma."""
    return [a.strip() for a in (accounts_list or "").split(",") if a.strip()]


def check_managed_accounts(accounts_list: str | None, expected: str) -> None:
    """Raise ``AccountIdentityError`` unless the list is exactly ``[expected]``."""
    if accounts_list is None:
        raise AccountIdentityError("managedAccounts was never received", reason="TIMEOUT")
    accounts = parse_managed_accounts(accounts_list)
    if not accounts:
        raise AccountIdentityError("managedAccounts is empty", reason="EMPTY")
    if len(accounts) != 1:
        masked = ", ".join(mask_account(a) for a in accounts)
        raise AccountIdentityError(
            f"managedAccounts has {len(accounts)} entries ({masked}); exactly one is allowed",
            reason="MULTIPLE")
    if accounts[0] != expected:
        raise AccountIdentityError(
            f"managedAccounts {mask_account(accounts[0])} does not match the configured account "
            f"{mask_account(expected)}", reason="MISMATCH")


def check_row_account(account: str, expected: str, where: str) -> None:
    """Every account-scoped row must carry the configured account, or the session is poisoned."""
    if account != expected:
        raise AccountIdentityError(
            f"{where} row for foreign account {mask_account(account)} (expected "
            f"{mask_account(expected)}); session poisoned", reason="FOREIGN_ROW")
