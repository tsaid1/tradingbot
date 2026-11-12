"""The operator's vocabulary: which human action resolves each exception kind, and the typed
confirmation each action needs. Plain strings, no imports, so the dashboard and the CLI can use it
without touching the execution core.

Every exception kind the execution core opens falls in exactly one class (a test keeps this list
complete):

* SETTLE: the outcome of an intent is unknown and only the operator, having checked the broker,
  can declare it (``execution.settlement.settle_intent``).
* CLEAR: an operator may close it, but only when a full broker sync and reconciliation made at
  that moment show the contract clean (the human fixes the account at the broker first).
* ACKNOWLEDGE: a record that needs a human to read it; it blocks no exposure, so acknowledging
  it changes nothing at the broker or in the ledger.
* EVIDENCE: never operator-clearable. Only broker evidence resolves it (a confirmed stop, a
  confirmed cancel, a successful sync, a clean reconciliation). Letting a human declare protection
  or broker state would let a typed word create unprotected exposure.
"""

from __future__ import annotations

SETTLE = "SETTLE"
CLEAR = "CLEAR"
ACKNOWLEDGE = "ACKNOWLEDGE"
EVIDENCE = "EVIDENCE"

SETTLEMENT_OUTCOMES = ("NOT_AT_BROKER", "FILLED", "CANCELLED")

EXCEPTION_ACTIONS: dict[str, tuple[str, str]] = {
    # kind: (class, what the operator does)
    "UNKNOWN_OUTCOME_ENTRY_NOT_RESENT": (
        SETTLE, "check the order at the broker, then settle its intent"),
    "UNKNOWN_OUTCOME_BEYOND_LOOKUP_HORIZON": (
        SETTLE, "check the order at the broker, then settle its intent"),
    "ENTRY_STATE_UNKNOWN": (SETTLE, "check the entry's fills at the broker, then settle its intent"),
    "LEDGER_REVERSAL": (
        CLEAR, "flatten the excess at the broker by hand, then clear (verified by reconciliation)"),
    "EXIT_NOT_NETTED": (ACKNOWLEDGE, "review the position and its stop; acknowledge"),
    "EXIT_WITHOUT_QUOTE": (ACKNOWLEDGE, "review the position and its stop; acknowledge"),
    "STALE_AFTER_UNKNOWN_OUTCOME": (ACKNOWLEDGE, "nothing was sent; acknowledge"),
    "MODIFY_NOT_CONFIRMED": (ACKNOWLEDGE, "compare the working stop with the ledger; acknowledge"),
    "PROTECTIVE_LEVEL_NOT_LOOSENED": (ACKNOWLEDGE, "review the kept level; acknowledge"),
    "PARTIAL_GROUP": (ACKNOWLEDGE, "review the half-executed group; acknowledge"),
    "UNPROTECTED_POSITION": (
        EVIDENCE, "restore the stop or exit the position; clears when the stop is confirmed"),
    "PROTECTION_UNVERIFIED": (EVIDENCE, "clears on the next successful broker check"),
    "ENTRY_REMAINDER_NOT_CANCELLED": (
        EVIDENCE, "cancel the remainder at the broker; clears when the cancel is seen"),
    "BROKER_STATE_UNKNOWN": (EVIDENCE, "restore the broker connection; clears on a full sync"),
    "BROKER_RECONNECT_PENDING": (
        EVIDENCE, "restore the session; clears after a full sync and reconciliation"),
    "OPERATOR_SETTLEMENT_UNVERIFIED": (
        EVIDENCE, "clears when a full sync and reconciliation show the contract clean"),
}
UNRESOLVED_INTENT_ACTION = (SETTLE, "check the order at the broker, then settle the intent")
SIGNAL_ACTION = (ACKNOWLEDGE, "review the held signal; acknowledge")


def action_for(kind: str) -> tuple[str, str]:
    """The operator action class and instruction for an exception kind (unknown kinds: EVIDENCE,
    never operator-clearable, which fails closed)."""
    if kind.startswith("SIGNAL_"):
        return SIGNAL_ACTION
    return EXCEPTION_ACTIONS.get(kind, (EVIDENCE, "no operator action; investigate"))


def settle_phrase(order_ref: str, outcome: str) -> str:
    return f"SETTLE {order_ref} {outcome}"


def clear_phrase(exc_id: int, kind: str) -> str:
    return f"CLEAR {exc_id} {kind}"


def acknowledge_phrase(exc_id: int, kind: str) -> str:
    return f"ACKNOWLEDGE {exc_id} {kind}"


def acknowledge_signal_phrase(exc_id: int, kind: str) -> str:
    return f"ACKNOWLEDGE SIGNAL {exc_id} {kind}"


def lockout_phrase(account: str) -> str:
    return f"CLEAR LOCKOUT {account}"
