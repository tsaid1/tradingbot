"""The operator-confirmation card (dashboard page *Exceptions*, ``tb_operator.py offers``).

Read-only: typed fields of the offers table and the post's identity, never post text. Every
string here is built from codes, numbers, identities and the closed table's plain words.
"""

from __future__ import annotations

from datetime import datetime

from tradingbot.runtime.store import RuntimeStore

WHAT_IT_DOES = ("Confirming removes only the doubts listed below. The price, quote, stop, size "
                "and age checks run again when it is applied, on a fresh quote, and can still "
                "refuse it.")


def card(row: dict, now: datetime) -> dict:
    """One offer as the card shows it (labels to plain strings)."""
    r = row["record"] or {}
    proposal = r.get("proposal") or {}
    view = row.get("quote_view") or {}
    quote = view.get("quote") or {}
    expires = datetime.fromisoformat(row["expires_at"])
    left = max(0, int((expires - now).total_seconds()))
    stated = r.get("kind") == "STATED_LEVEL"
    return {
        "offer": f"{row['id']} ({row['hash'][:8]})",
        "state": row["state"] + (f" ({row['state_reason']})" if row.get("state_reason") else ""),
        "what a confirmation does": WHAT_IT_DOES,
        "proposed action": f"{proposal.get('action')} {proposal.get('position_side')}",
        "instrument": (f"family {r.get('family')}; reference series "
                       f"{r.get('reference_series') or '-'}; execution "
                       f"{r.get('execution_instrument')}"),
        "price reference": (f"{'stated level' if stated else 'explicit entry'} "
                            f"{r.get('price_reference')}; basis {r.get('basis')}"),
        "current quote": (f"bid {quote.get('bid')} ask {quote.get('ask')} "
                          f"({quote.get('quality') or '-'}), {quote.get('age_seconds')} s old, "
                          f"deviation {quote.get('deviation_pct')}% at {view.get('at')}"
                          if quote else "not read yet"),
        "would pass now": " ".join(view.get("gate_now") or [r.get("gate_now_status") or "-"]),
        "stop": str(r.get("stop")),
        "target": str(r.get("target") or "-"),
        "uncertainty": list(r.get("uncertainty") or ()),
        "reason for the hold": ", ".join(r.get("hold_reasons") or ()),
        "time left": f"{left} s (expires {row['expires_at']})",
        "post": (f"source {row['source_id']} post {r.get('post_id')} revision "
                 f"{row['revision']} action {row['action_id']}, published "
                 f"{r.get('published_at')}"),
        "result": row.get("result"),
    }


def load_offers(store: RuntimeStore, *, open_only: bool = False) -> list[dict]:
    rows = store.offers()
    return [r for r in rows if r["state"] == "OFFERED"] if open_only else rows
