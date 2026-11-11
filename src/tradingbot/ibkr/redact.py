"""Account-ID masking. Full account IDs must never reach repr, logs, exceptions or reports."""
from __future__ import annotations

import re

# IBKR account IDs: one to three capital letters followed by digits (DU1234567, U1234567, F...).
# Matched regardless of what adjoins it (``aDU1234567``, ``DU1234567DU7654321``, ``_DU1234567``):
# this over-redacts some harmless tokens (e.g. ``T090000`` in a UTC stamp), deliberately.
_ACCOUNT_LIKE = re.compile(r"[A-Z]{1,3}\d{4,12}")


def mask_account(account: str) -> str:
    """First two and last two characters, e.g. ``DU…67``; anything shorter than 6 is fully hidden."""
    if not isinstance(account, str) or len(account) < 6:
        return "***"
    return f"{account[:2]}…{account[-2:]}"


def redact_accounts(text: str) -> str:
    """Mask every account-like token in free text (broker error strings, exception messages)."""
    return _ACCOUNT_LIKE.sub(lambda m: mask_account(m.group(0)), str(text))
