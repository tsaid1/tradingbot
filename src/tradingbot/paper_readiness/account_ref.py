"""Private exact account comparison with closed, opaque evidence. NON_AUTHORITATIVE.

``compare_account`` compares one expected account identifier with the managed-account list a session
reported, privately and exactly, and returns an ``AccountMatch`` that carries only a fixed status, a
fixed refusal code and, for an exact match only, a keyed opaque reference. Identifiers and the key
never appear in a result, its repr or an error.

This **prepares** E2 only. Nothing here is wired into ``ReadinessExpectation``, ``SessionAttestation``
or preflight, which still admit only ``SYNTH-`` identifiers, and E2 stays open until that integration
is separately reviewed. A real identifier is never presented as ``SYNTH-``. Nothing here attests a
PAPER environment, enforces key freshness, authenticates where an identifier came from, or promises
anonymity or cryptographic privacy. It reads no environment, clock, file, network or randomness: the
caller supplies the per-run key. See docs/ACCOUNT_REFERENCE.md.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from enum import Enum

__all__ = [
    "DIGEST_BYTES",
    "MAX_IDENTIFIER_BYTES",
    "MAX_KEY_BYTES",
    "MAX_MANAGED_ACCOUNTS",
    "MIN_KEY_BYTES",
    "REFERENCE_PREFIX",
    "AccountMatch",
    "MatchStatus",
    "RefusalCode",
    "compare_account",
]

MAX_IDENTIFIER_BYTES = 64  # UTF-8 bytes per identifier
MAX_MANAGED_ACCOUNTS = 16  # entries in the managed list
MIN_KEY_BYTES = 16
MAX_KEY_BYTES = 64         # blake2b's maximum key size
DIGEST_BYTES = 16
REFERENCE_PREFIX = "acctref-v1:"
_PERSON = b"tb-acctref-v1"  # blake2b domain separation (at most 16 bytes)
_REFERENCE_RE = re.compile(r"^acctref-v1:[0-9a-f]{32}$")


class MatchStatus(str, Enum):
    EXACT = "EXACT"          # exactly one valid managed identifier, byte-equal to the expected one
    MISMATCH = "MISMATCH"    # exactly one valid managed identifier, not equal
    AMBIGUOUS = "AMBIGUOUS"  # more than one managed identifier, duplicates included
    ABSENT = "ABSENT"        # no managed identifier
    REFUSED = "REFUSED"      # malformed input; never an ordinary mismatch


class RefusalCode(str, Enum):
    KEY_INVALID = "KEY_INVALID"
    EXPECTED_INVALID = "EXPECTED_INVALID"
    MANAGED_CONTAINER_INVALID = "MANAGED_CONTAINER_INVALID"
    MANAGED_TOO_MANY = "MANAGED_TOO_MANY"
    MANAGED_ENTRY_INVALID = "MANAGED_ENTRY_INVALID"


class _ConstructionError(ValueError):
    """A fixed message only: nothing passed to the constructor is echoed."""

    def __init__(self) -> None:
        super().__init__("AccountMatch fields are inconsistent or not of the closed types")


@dataclass(frozen=True, slots=True, repr=False)
class AccountMatch:
    """Closed result. Direct construction is validated, so it cannot carry an identifier either."""

    status: MatchStatus
    refusal: RefusalCode | None = None
    reference: str | None = None

    def __post_init__(self) -> None:
        status, refusal, reference = self.status, self.refusal, self.reference
        valid = (
            type(status) is MatchStatus
            and (refusal is None or type(refusal) is RefusalCode)
            and (reference is None or (type(reference) is str and _REFERENCE_RE.fullmatch(reference)))
            and (refusal is not None) == (status is MatchStatus.REFUSED)
            and (reference is not None) == (status is MatchStatus.EXACT)
        )
        if not valid:
            raise _ConstructionError

    def __repr__(self) -> str:
        refusal = self.refusal.value if self.refusal is not None else None
        return f"AccountMatch(status={self.status.value}, refusal={refusal}, reference={self.reference})"

    __str__ = __repr__


def _identifier_bytes(value: object) -> bytes | None:
    """The UTF-8 bytes of a valid identifier, or None.

    Valid means an exact ``str``, non-empty, strictly UTF-8 encodable (a lone surrogate is not),
    at most ``MAX_IDENTIFIER_BYTES`` bytes, and free of control characters (U+0000-U+001F and
    U+007F-U+009F). Spaces and every other character are kept as they are: nothing is trimmed,
    case-folded, normalized or split."""
    if type(value) is not str or not value or len(value) > MAX_IDENTIFIER_BYTES:
        return None
    try:
        encoded = value.encode("utf-8", "strict")
    except UnicodeEncodeError:
        return None
    if len(encoded) > MAX_IDENTIFIER_BYTES:
        return None
    if any(ord(ch) < 0x20 or 0x7F <= ord(ch) <= 0x9F for ch in value):
        return None
    return encoded


def _refused(code: RefusalCode) -> AccountMatch:
    return AccountMatch(MatchStatus.REFUSED, refusal=code)


def compare_account(expected: object, managed: object, key: object) -> AccountMatch:
    """Compare privately and exactly. Never raises for bad input; returns REFUSED instead.

    ``managed`` must be an exact ``list`` or ``tuple`` of identifiers, as the caller split them from
    the session's managed-accounts report. ``key`` must be exact ``bytes`` of 16 to 64 bytes, chosen
    by the caller for one run. Bounds are checked before any iteration, comparison or hashing."""
    if type(key) is not bytes or not MIN_KEY_BYTES <= len(key) <= MAX_KEY_BYTES:
        return _refused(RefusalCode.KEY_INVALID)
    expected_bytes = _identifier_bytes(expected)
    if expected_bytes is None:
        return _refused(RefusalCode.EXPECTED_INVALID)
    if type(managed) not in (list, tuple):
        return _refused(RefusalCode.MANAGED_CONTAINER_INVALID)
    if len(managed) > MAX_MANAGED_ACCOUNTS:
        return _refused(RefusalCode.MANAGED_TOO_MANY)
    entries = []
    for entry in managed:
        encoded = _identifier_bytes(entry)
        if encoded is None:
            return _refused(RefusalCode.MANAGED_ENTRY_INVALID)
        entries.append(encoded)
    if not entries:
        return AccountMatch(MatchStatus.ABSENT)
    if len(entries) > 1:
        return AccountMatch(MatchStatus.AMBIGUOUS)  # no deduplication: two equal entries are two
    if entries[0] != expected_bytes:
        return AccountMatch(MatchStatus.MISMATCH)
    digest = hashlib.blake2b(expected_bytes, digest_size=DIGEST_BYTES, key=key, person=_PERSON)
    return AccountMatch(MatchStatus.EXACT, reference=REFERENCE_PREFIX + digest.hexdigest())
