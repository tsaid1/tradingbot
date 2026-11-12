"""Coverage of the publisher's instrument universe: per source asset family, whether it may trade.

Read-only. For every family that has a mapping entry, and every family the publisher actually
named in a signal, it reports the mapping status (APPROVED / NOT APPROVED / UNSUPPORTED), the
execution instrument and basis treatment, and counts of signals executed, held (by reason) and
exceptions. A family with no entry shows as UNSUPPORTED with what was held, so the universe the
bot does not trade is visible rather than silently ignored.
"""

from __future__ import annotations

from collections import Counter

from tradingbot.runtime.mapping import FamilyMapping, MappingEntry, MappingPolicy

EXECUTED = frozenset({"EXECUTED", "SIMULATED"})
NOT_COUNTED = frozenset({"NO_TRADE", "RECONCILE"})  # commentary, summaries, past-tense reports
EXCEPTION_STATUSES = frozenset({"EXCEPTION", "UNRESOLVED", "REJECTED", "REFUSED", "HALTED"})
# Gateway reasons that carry no information about why (they prefix the specific ones).
_GENERIC = frozenset({"SOURCE_AMBIGUITIES"})


def _family_of(row: dict) -> str | None:
    return row.get("mapping_family") or row.get("asset")


def _hold_reason(row: dict) -> str:
    reasons = [r for r in row.get("reasons") or [] if r not in _GENERIC]
    return reasons[0] if reasons else row.get("status") or "-"


def coverage_report(policy: MappingPolicy, actions: list[dict]) -> list[dict]:
    """One row per family, sorted: entries first (by family), then families seen but unmapped."""
    rows: dict[str, dict] = {}

    def row_for(family: str) -> dict:
        if family not in rows:
            entry = policy.entries.get(family)
            if isinstance(entry, FamilyMapping):
                status, execution, basis = entry.status_label, entry.execution_label(), entry.basis
                approved_by = entry.approved_by
            elif isinstance(entry, MappingEntry):  # version-1 file
                status = "APPROVED" if entry.executable else "UNSUPPORTED"
                execution = f"{entry.broker_symbol} {entry.broker_asset_class.value}"
                basis, approved_by = entry.basis.value, None
            else:
                status, execution, basis, approved_by = "UNSUPPORTED", "-", "-", None
            rows[family] = {
                "family": family,
                "mapping_status": status,
                "mapped": entry is not None,
                "execution": execution,
                "basis": basis,
                "approved_by": approved_by,
                "signals": 0,
                "executed": 0,
                "held": 0,
                "exceptions": 0,
                "held_by_reason": Counter(),
            }
        return rows[family]

    for family in policy.entries:
        row_for(family)
    for a in actions:
        family = _family_of(a)
        if not family or a.get("status") in NOT_COUNTED or a.get("action") in (None, "NO_TRADE"):
            continue
        r = row_for(family)
        r["signals"] += 1
        status = a.get("status")
        if status in EXECUTED:
            r["executed"] += 1
        elif status in EXCEPTION_STATUSES:
            r["exceptions"] += 1
            r["held_by_reason"][_hold_reason(a)] += 1
        else:
            r["held"] += 1
            r["held_by_reason"][_hold_reason(a)] += 1
    out = sorted(rows.values(), key=lambda r: (not r["mapped"], r["family"]))
    for r in out:
        r["held_by_reason"] = dict(sorted(r["held_by_reason"].items()))
    return out
