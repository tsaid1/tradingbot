"""Mock-only, synthetic-only TLT lifecycle demonstration through the existing stack.

Orchestration glue and presentation only. The module loads an invented script
(``fixtures/demo_tlt_lifecycle.json``), feeds each post through the existing path exactly as
``tkl_interpreter.replay_posts`` does (TklInterpreter -> validator gateway inside
PaperEventReplay -> fictional fill), and records one audit entry per step together with the mock
ledger after it. It adds no authorization rule, no sizing, no persistence of its own and no broker
connectivity. Every status and reason code an operator sees comes from the gateway or the
coordinator; this module only attaches plain-language explanations to them.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Literal

from pydantic import Field

from tradingbot.evaluation import GoldenReplay, GoldenStep
from tradingbot.event_models import PaperOrder, ReplaySource
from tradingbot.event_replay import PaperEventReplay
from tradingbot.models import MockPolicy, Quote, StrictModel
from tradingbot.source_events import SourcePost
from tradingbot.tkl_interpreter import TklInterpreter, group_linked_actions, replay_posts

REPO = Path(__file__).resolve().parents[2]
DEFAULT_SCRIPT = REPO / "fixtures" / "demo_tlt_lifecycle.json"
MOCK_BANNER = ("MOCK / SYNTHETIC ONLY. No broker, no live market data, no real orders. "
               "Every post, price level and date in this demonstration is invented.")

# Plain-language meaning of every status the gateway and coordinator can return, for operators.
STATUS_EXPLANATIONS: dict[str, str] = {
    "SIMULATED": "Approved by the deterministic gateway and filled in the mock ledger. No broker was contacted.",
    "HOLD": "Not executed. The gateway needs a human decision or information it does not have; nothing changed.",
    "BLOCK": "Rejected by a hard safety rule; nothing changed.",
    "NO_TRADE": "Not an instruction (commentary, summary or cancellation); there is nothing to execute.",
    "RECONCILE": "A report about the past. No order; the operator compares it with the mock ledger instead.",
    "DUPLICATE": "This post was already processed. The recorded outcome is returned and no second fill can happen.",
}

# Plain-language meaning of every reason code in validator.py, event_replay.py and the interpreter's
# ambiguity codes. tests/test_mock_tlt_demo.py checks that no code can reach an operator unexplained.
REASON_EXPLANATIONS: dict[str, str] = {
    # post-level provenance (gate_source)
    "SOURCE_EDIT_REQUIRES_REVIEW": "The post was edited; a revised instruction is held for human review.",
    "SOURCE_NOT_AUTHENTICATED": "The author could not be verified, so nothing in the post is trusted.",
    "REPOST_NOT_AUTHOR_INSTRUCTION": "A repost is someone else's words, not the author's instruction.",
    "SOURCE_TEXT_TRUNCATED": "The text is incomplete; the full post is needed before anything can execute.",
    "SOURCE_TIMEZONE_MISSING": "A timestamp has no timezone, so freshness cannot be judged.",
    "SOURCE_RECEIPT_BEFORE_PUBLICATION": "The post was received before it was published; the timestamps are untrustworthy.",
    # temporal intent and authority (gate_action)
    "HISTORICAL_REPORT_NO_ORDER": "The clause reports something that already happened; reports never create orders.",
    "CONDITIONAL_TRIGGER_NOT_IMPLEMENTED": "The instruction depends on a condition; conditions are never triggered automatically.",
    "COMMENTARY_OR_SUMMARY": "Commentary, an outlook or a position summary is not an instruction.",
    "TEMPORAL_INTENT_UNCERTAIN": "It is unclear whether the clause is a current instruction.",
    "NOT_EXPLICITLY_AUTHORITATIVE": "No explicit, evidence-backed instruction was found.",
    "SOURCE_AMBIGUITIES": "The interpreter flagged ambiguities (listed next); the gateway holds rather than guesses.",
    # instrument identity
    "ORDERABLE_INSTRUMENT_MISSING": "The level refers to an index, spot or futures series, not to an orderable product.",
    "SOURCE_TO_BROKER_PRICE_BASIS_UNRESOLVED": "The quoted price is not known to belong to the orderable contract.",
    "SOURCE_PRICE_INSTRUMENT_IDENTITY_UNVERIFIED": "The post does not say which market the price belongs to.",
    "SOURCE_TO_BROKER_MAPPING_UNAPPROVED": "Mapping the quoted market to a tradable product is not approved.",
    "ASSET_CLASS_PAPER_ADAPTER_UNSUPPORTED": "Only equities and ETFs can be filled in the mock ledger.",
    "INVALID_SYMBOL": "The symbol is malformed.",
    "INSTRUMENT_NOT_ALLOWLISTED": "The symbol is outside the policy's allowlist.",
    "ACTION_UNSUPPORTED": "This engine cannot fill that kind of action.",
    # freshness and quote integrity
    "SOURCE_STALE_OR_FUTURE": "The post is older than the policy allows, or dated in the future.",
    "QUOTE_UNAVAILABLE": "No synthetic quote was supplied for the instrument.",
    "QUOTE_INSTRUMENT_MISMATCH": "The quote is for a different instrument.",
    "QUOTE_STALE_OR_FUTURE": "The quote is older than the policy allows, or dated in the future.",
    "INVALID_BID_ASK": "The quote's bid and ask are inconsistent.",
    "SPREAD_TOO_WIDE": "The bid/ask spread exceeds the policy limit.",
    # opening rules
    "OPEN_SHOULD_NOT_REFERENCE_EXISTING_IDEA": "A new entry must not point at an existing idea.",
    "DIRECTION_MISSING": "Long or short was not stated.",
    "OPEN_ENTRY_PRICE_MISSING": "No explicit entry price for the orderable contract was stated.",
    "ENTRY_PRICE_COLLAR": "The market has moved too far from the stated entry price.",
    "INVALID_STOP_PRICE": "The stop level is not a valid price.",
    "INVALID_TARGET_PRICE": "The target level is not a valid price.",
    "PROTECTIVE_STOP_MISSING": "Policy requires a protective stop on every entry.",
    "STOP_DIRECTION_CONTRADICTION": "The stop is on the wrong side of the entry for this direction.",
    "STOP_ALREADY_CROSSED_AT_ENTRY": "The market is already beyond the stop.",
    "TARGET_DIRECTION_CONTRADICTION": "The target is on the wrong side of the entry for this direction.",
    "NOTIONAL_LIMIT": "The fictional order would exceed the mock notional cap.",
    "PAPER_OPEN": "Mock entry approved; sized by policy, never by the post.",
    # follow-up rules
    "REFERENCED_ACTIVE_IDEA_NOT_FOUND": "The follow-up does not point at an open position in the mock ledger.",
    "IDEA_INSTRUMENT_MISMATCH": "The referenced position is in a different instrument.",
    "IDEA_SIDE_MISMATCH": "The referenced position is on the other side.",
    "REDUCTION_FRACTION_UNDEFINED": "The size of the partial exit was not stated.",
    "NON_INTEGER_REDUCTION": "The partial exit does not divide the position into whole units.",
    "PAPER_REDUCE_ONLY": "Mock reduce-only exit approved.",
    "EXISTING_STOP_ALREADY_CROSSED": "The existing stop has already been crossed; adding is unsafe.",
    "ADD_ENTRY_REFERENCE_MISSING": "No explicit price was stated for the addition.",
    "PAPER_ADD": "Mock addition approved; sized by policy.",
    "NO_FIELDS_TO_MODIFY": "The modification names neither a stop nor a target.",
    "STOP_WIDENING_REQUIRES_POLICY_REVIEW": "Loosening a stop needs a policy review; only tightening is automatic.",
    "STOP_ON_WRONG_SIDE_OF_MARKET": "The new stop would be on the wrong side of the market.",
    "TARGET_ON_WRONG_SIDE_OF_MARKET": "The new target would be on the wrong side of the market.",
    "PAPER_PROTECTION_AMENDMENT": "Protection levels amended in the mock ledger only; no broker order exists.",
    "UNKNOWN_ACTION": "The action is not one the gateway knows.",
    # coordinator
    "IDEA_ALREADY_EXISTS": "An idea with this identity already exists in the mock ledger.",
    "ATOMIC_GROUP_REJECTED": "A linked action failed, so none of the linked actions was applied.",
    "SOURCE_HASH_CHANGED_WITHOUT_REVISION": "The text changed without a new revision; blocked as tampering.",
    # interpreter ambiguities (attached after SOURCE_AMBIGUITIES)
    "MULTIPLE_TICKERS_IN_CLAUSE": "More than one ticker in the clause; the instrument is unclear.",
    "MIXED_SERIES_AND_TICKER": "A market series and a ticker are both named; the price basis is unclear.",
    "MULTIPLE_SERIES_IN_CLAUSE": "More than one market series in the clause.",
    "ASSET_UNRESOLVED": "No instrument or market could be identified.",
    "NO_OPEN_IDEA_IN_CONTEXT": "The publisher's book shows no open position to apply this to.",
    "MULTIPLE_OPEN_IDEAS_IN_CONTEXT": "Several open positions could be meant.",
    "EXISTING_OPEN_IDEA_SAME_SIDE_IN_CONTEXT": "A position on the same side is already open.",
    "SIDE_UNCLEAR": "Long or short is unclear.",
    "HEADLINE_POSITION_LINE_WITHOUT_ENTRY_VERB": "A bare position line could be a summary rather than an entry.",
    "POSITION_STATEMENT_OR_ENTRY": "'We are long' may describe an existing position rather than a new entry.",
    "FUTURE_INTENT_WITHOUT_EXPLICIT_CONDITION": "A future intention without a stated condition.",
}


def explain(code: str) -> str:
    """Plain-language text for a status or reason code; never returns an empty string."""
    return STATUS_EXPLANATIONS.get(code) or REASON_EXPLANATIONS.get(code) or f"Gateway code {code} (no description recorded)."


# ----------------------------------------------------------------------------- script and views

class DemoScript(StrictModel):
    """The invented lifecycle: a GoldenReplay (the evaluation runner's own format) plus narration."""
    synthetic_only: Literal[True]
    notice: str
    narration: dict[str, str] = Field(default_factory=dict)
    golden: GoldenReplay

    @property
    def posts(self) -> list[SourcePost]:
        return sorted(self.golden.posts, key=lambda p: (p.available_at, p.revision, p.post_id))

    def expected(self, source_id: str) -> GoldenStep | None:
        return next((s for s in self.golden.steps if s.source_id == source_id), None)


def load_script(path: str | Path = DEFAULT_SCRIPT) -> DemoScript:
    return DemoScript.model_validate_json(Path(path).read_text(encoding="utf-8"))


class LedgerIdea(StrictModel):
    """One row of the coordinator's mock ledger (replay_ideas)."""
    idea_id: str
    symbol: str
    asset_class: str
    position_side: str
    quantity: int
    stop_price: str | None = None
    target_price: str | None = None
    status: str


class InterpretationView(StrictModel):
    """One clause as the interpreter read it, with the TradeAction it produced."""
    action_id: str
    message_class: str
    tense: str
    action: str
    timing: str
    asset: str | None = None
    side: str | None = None
    entry_reference: str | None = None
    stop_price: str | None = None
    target_price: str | None = None
    close_fraction: str | None = None
    idea_reference: str | None = None
    atomic_group: str | None = None
    explicit_instruction: bool
    ambiguities: list[str] = Field(default_factory=list)
    clause: str


class OutcomeView(StrictModel):
    action_id: str
    status: str
    reasons: list[str]
    explanations: list[str]
    order: PaperOrder | None = None
    headline: str
    reconciliation_note: str | None = None


class AuditEntry(StrictModel):
    step: int
    source_id: str
    post_id: str
    published_at: datetime
    processed_as_of: datetime
    text: str
    narration: str
    provenance: ReplaySource
    quotes: dict[str, dict[str, str]]
    interpretations: list[InterpretationView]
    outcomes: list[OutcomeView]
    duplicate: bool
    ledger_after: list[LedgerIdea]
    expected_statuses: list[str]
    expected_orders: list[dict]
    matches_expectation: bool
    headline: str


class ReconciliationRow(StrictModel):
    """The publisher's stated book (interpreter context) against the mock ledger, per idea."""
    idea_id: str
    publisher_state: str | None = None   # OPEN / CLOSED as the publisher described it; None if unknown there
    publisher_stop: str | None = None
    publisher_target: str | None = None
    ledger_state: str | None = None      # OPEN / CLOSED in the mock ledger; None if the ledger never filled it
    ledger_quantity: int | None = None
    ledger_stop: str | None = None
    ledger_target: str | None = None
    agrees: bool


# ----------------------------------------------------------------------------- session

def fresh_database(path: str | Path) -> None:
    """Remove a mock ledger file (and SQLite sidecars) so a session starts empty."""
    base = Path(path)
    for candidate in (base, base.with_name(base.name + "-wal"), base.with_name(base.name + "-shm"),
                      base.with_name(base.name + "-journal")):
        if candidate.exists():
            candidate.unlink()


class DemoSession:
    """Steps through the script one post at a time, through the existing replay path."""

    def __init__(self, db_path: str | Path, script: DemoScript | None = None):
        self.script = script or load_script()
        self.db_path = Path(db_path)
        self.coordinator = PaperEventReplay(self.db_path, MockPolicy(**self.script.golden.policy))
        self.interpreter = TklInterpreter()
        self.entries: list[AuditEntry] = []
        self._posts = self.script.posts
        self._cursor = 0
        self._last_now: datetime | None = None
        self._last_quotes: dict[str, Quote] = {}
        # Reconciliation watermark: the latest replay clock this session has seen. It never moves
        # backwards, so a duplicate replay of an earlier post cannot rewind the comparison.
        self._as_of: datetime | None = None

    # ------------------------------------------------------------------ state

    @property
    def policy(self) -> MockPolicy:
        return self.coordinator.policy

    @property
    def total_steps(self) -> int:
        return len(self._posts)

    @property
    def steps_done(self) -> int:
        return self._cursor

    @property
    def is_complete(self) -> bool:
        return self._cursor >= len(self._posts)

    @property
    def processed_posts(self) -> list[SourcePost]:
        return self._posts[:self._cursor]

    def ledger(self) -> list[LedgerIdea]:
        return [LedgerIdea.model_validate(row) for row in self.coordinator.ideas()]

    # ------------------------------------------------------------------ actions

    def run_next(self) -> AuditEntry | None:
        if self.is_complete:
            return None
        post = self._posts[self._cursor]
        entry = self._process(post, replay=False)
        # Advance only after the coordinator committed and the audit entry exists: a failure (for
        # example a SQLite lock timeout) leaves the post unprocessed and the next call retries it.
        self._cursor += 1
        self.entries.append(entry)
        return entry

    def run_all(self) -> list[AuditEntry]:
        done: list[AuditEntry] = []
        while (entry := self.run_next()) is not None:
            done.append(entry)
        return done

    def replay(self, source_id: str | None = None) -> AuditEntry:
        """Re-submit a post that was already processed: the idempotency check. The coordinator must
        answer with the recorded outcome and the mock ledger must not change."""
        processed = self.processed_posts
        if not processed:
            raise ValueError("Nothing has been processed yet")
        post = processed[-1] if source_id is None else next(p for p in processed if p.source_id == source_id)
        entry = self._process(post, replay=True)
        self.entries.append(entry)
        return entry

    # ------------------------------------------------------------------ internals

    def _quotes_for(self, post: SourcePost, now: datetime) -> dict[str, Quote]:
        """Synthetic operator quotes from the script (mid +/- 0.05), stamped at the replay clock."""
        mids = self.script.golden.quotes.get(post.post_id) or self.script.golden.quotes.get(post.source_id, {})
        quotes = {symbol: Quote(symbol=symbol, bid=Decimal(mid) - Decimal("0.05"), ask=Decimal(mid) + Decimal("0.05"),
                                observed_at=now) for symbol, mid in mids.items()}
        self._last_now, self._last_quotes = now, quotes
        return quotes

    def _process(self, post: SourcePost, *, replay: bool) -> AuditEntry:
        before = self.ledger()
        step = replay_posts([post], self.coordinator, self.interpreter, self._quotes_for,
                            lag_seconds=self.script.golden.lag_seconds)[0]
        after = self.ledger()
        assert self._last_now is not None
        now = self._last_now
        self._as_of = now if self._as_of is None else max(self._as_of, now)
        grouped = {a.action_id: a for a in group_linked_actions(step.interpreted).actions}
        interpretations = [
            InterpretationView(
                action_id=c.action_id, message_class=c.message_class.value, tense=c.tense.value,
                action=grouped[c.action_id].action.value, timing=grouped[c.action_id].timing.value,
                asset=c.asset, side=c.side.value if c.side else None,
                entry_reference=grouped[c.action_id].entry_reference, stop_price=grouped[c.action_id].stop_price,
                target_price=grouped[c.action_id].target_price, close_fraction=grouped[c.action_id].close_fraction,
                idea_reference=c.idea_reference, atomic_group=grouped[c.action_id].atomic_group,
                explicit_instruction=grouped[c.action_id].explicit_instruction,
                ambiguities=list(c.ambiguities), clause=c.clause)
            for c in step.interpreted.classifications
        ]
        outcomes = []
        for outcome in step.result.outcomes:
            action = grouped.get(outcome.action_id)
            symbol = action.instrument.symbol if action is not None and action.instrument is not None else None
            note = self._reconciliation_note(symbol, after) if outcome.status == "RECONCILE" else None
            outcomes.append(OutcomeView(
                action_id=outcome.action_id, status=outcome.status, reasons=list(outcome.reasons),
                explanations=[explain(r) for r in outcome.reasons], order=outcome.order,
                headline=_outcome_headline(outcome.status, outcome.reasons, outcome.order, action.stop_price if action else None,
                                           action.target_price if action else None),
                reconciliation_note=note))
        expected = self.script.expected(post.source_id)
        if replay:
            expected_statuses, expected_orders = ["DUPLICATE"], []
            matches = step.result.duplicate and before == after
            headline = "DUPLICATE: already processed; recorded outcome returned, mock ledger unchanged" if matches \
                else "UNEXPECTED: the replay was not recognised as a duplicate"
        else:
            expected_statuses = expected.statuses if expected else []
            expected_orders = expected.orders if expected else []
            statuses = [o.status for o in step.result.outcomes]
            orders = [{"side": o.order.side, "quantity": o.order.quantity} for o in step.result.outcomes if o.order]
            matches = expected is not None and statuses == expected_statuses and orders == expected_orders \
                and not step.result.duplicate
            if step.result.duplicate:
                # A restart on an existing ledger: the coordinator recognised the post; nothing filled again.
                headline = "DUPLICATE: this post was already in the mock ledger; recorded outcome returned, no new fill"
            else:
                headline = " | ".join(o.headline for o in outcomes) or "No clause produced an action"
        return AuditEntry(
            step=len(self.entries) + 1, source_id=post.source_id, post_id=post.post_id, published_at=post.published_at,
            processed_as_of=now, text=post.text, narration=self.script.narration.get(post.source_id, ""),
            provenance=post.to_replay_source(),
            quotes={s: {"bid": str(q.bid), "ask": str(q.ask)} for s, q in self._last_quotes.items()},
            interpretations=interpretations, outcomes=outcomes, duplicate=step.result.duplicate,
            ledger_after=after, expected_statuses=expected_statuses, expected_orders=expected_orders,
            matches_expectation=matches, headline=headline)

    @staticmethod
    def _reconciliation_note(symbol: str | None, ledger: list[LedgerIdea]) -> str:
        if symbol is None:
            return "No orderable instrument named; nothing to compare with the mock ledger."
        open_ideas = [i for i in ledger if i.symbol == symbol and i.status == "OPEN" and i.quantity > 0]
        if open_ideas:
            units = ", ".join(f"{i.idea_id} ({i.quantity} units)" for i in open_ideas)
            return (f"Mock ledger still holds {symbol} OPEN: {units}. The report and the ledger disagree; "
                    "an operator must check the broker book (a stop may have fired) before any exit.")
        closed = [i.idea_id for i in ledger if i.symbol == symbol]
        if closed:
            return f"Consistent: the mock ledger already shows {symbol} closed ({', '.join(closed)}). Nothing to do."
        return f"The mock ledger never held {symbol}; the report describes a position the follower did not take."

    # ------------------------------------------------------------------ reporting

    def reconciliation(self) -> list[ReconciliationRow]:
        """Publisher's stated book (as the interpreter understood it, as of the last processed post)
        against the mock ledger. A disagreement is an operator task, never an automatic order."""
        as_of = self._as_of or datetime.now(timezone.utc)
        ledger = {i.idea_id: i for i in self.ledger()}
        views = self.interpreter.context.ideas
        rows: list[ReconciliationRow] = []
        for idea_id in sorted(set(ledger) | set(views)):
            view, row = views.get(idea_id), ledger.get(idea_id)
            publisher_state = None if view is None else ("OPEN" if view.is_open_as_of(as_of) else "CLOSED")
            ledger_state = None if row is None else ("OPEN" if row.status == "OPEN" and row.quantity > 0 else "CLOSED")
            agrees = (view is not None and row is not None and publisher_state == ledger_state
                      and _decimal_equal(view.stop_price, row.stop_price)
                      and _decimal_equal(view.target_price, row.target_price))
            rows.append(ReconciliationRow(
                idea_id=idea_id, publisher_state=publisher_state,
                publisher_stop=view.stop_price if view else None, publisher_target=view.target_price if view else None,
                ledger_state=ledger_state, ledger_quantity=row.quantity if row else None,
                ledger_stop=row.stop_price if row else None, ledger_target=row.target_price if row else None,
                agrees=agrees))
        return rows

    def summary(self) -> dict:
        fresh = [e for e in self.entries if not e.duplicate]
        outcomes = [o for e in fresh for o in e.outcomes]
        return {
            "steps_processed": self.steps_done, "steps_total": self.total_steps,
            "mock_fills": sum(1 for o in outcomes if o.order is not None and o.order.quantity > 0),
            "protection_amendments": sum(1 for o in outcomes if o.order is not None and o.order.status == "SIMULATED_AMENDED"),
            "holds": sum(1 for o in outcomes if o.status == "HOLD"),
            "blocks": sum(1 for o in outcomes if o.status == "BLOCK"),
            "no_trades": sum(1 for o in outcomes if o.status == "NO_TRADE"),
            "reconciles": sum(1 for o in outcomes if o.status == "RECONCILE"),
            "duplicates_rejected": sum(1 for e in self.entries if e.duplicate),
            "expectations_met": all(e.matches_expectation for e in self.entries),
            "ledger_agrees_with_publisher": all(r.agrees for r in self.reconciliation()),
        }


# ----------------------------------------------------------------------------- helpers

def _decimal_equal(a: str | None, b: str | None) -> bool:
    if a is None or b is None:
        return a is b
    try:
        return Decimal(a) == Decimal(b)
    except (InvalidOperation, ValueError):
        return a == b


def _outcome_headline(status: str, reasons: list[str], order: PaperOrder | None,
                      stop: str | None, target: str | None) -> str:
    if status == "SIMULATED" and order is not None:
        if order.status == "SIMULATED_AMENDED":
            levels = ", ".join(f"{name} -> {value}" for name, value in (("stop", stop), ("target", target)) if value)
            return f"SIMULATED protection amendment ({levels or 'no level'}) in the mock ledger only"
        return f"SIMULATED {order.side} {order.quantity} {order.symbol} @ {order.price} (mock fill, idea {order.idea_id})"
    first = reasons[0] if reasons else status
    return f"{status}: {first}. {explain(first)}"
