"""The closed table of hold reasons an operator confirmation may resolve (docs/OPERATOR_CONFIRMATION.md).

Default deny: a (code, detail) pair that is not in ``CONFIRMABLE`` or ``REPORT_ONLY`` is never
stepped over by the mapping's waived evaluation, and only ``CONFIRMABLE`` may ever be offered. A
code added to the mapping, the gateway or the interpreter later is therefore never confirmable
until it is written here, reviewed and approved by the owner.

A pair is (reason code, detail key). The detail key is the reason's detail with its count or word
removed (``WORDS:3`` -> ``WORDS``, ``NOT_CURRENT_WORD:<w>`` -> ``NOT_CURRENT_WORD``), or a key of
the waiver site itself where the public detail is not specific enough (``OWN_CLAUSE:APPROXIMATION_
MARK``: the number's own clause holds nothing but an approximation mark). Interpreter ambiguities
have the empty detail key. String literals only: this module imports nothing, so the mapping can
import it; a test checks that every code named here exists in the code base.
"""

from __future__ import annotations

CONFIRMABLE_TIER = "CONFIRMABLE"
REPORT_ONLY_TIER = "REPORT_ONLY"

# Version 1, approved by the owner (lead's decision of 14 November 2026). One line of reasoning
# each: the level, the side and the stop are read and unique; only what the post MEANS is in doubt.
CONFIRMABLE: dict[tuple[str, str], str] = {
    ("ENTRY_ANCHOR_POST_UNRESOLVED_MARKET", "RULE:MENTION_WITH_CONTEXT"):
        "The post mentions a market name the bot does not know, next to a price or trading word.",
    ("ENTRY_ANCHOR_NOT_CURRENT", "NO_PRESENT_MARKER"):
        "The level is not marked as the current price.",
    ("ENTRY_ANCHOR_CONTEXT_UNCLEAR", "WORDS"):
        "A word in the level's sentence is not one the bot understands.",
    ("ENTRY_ANCHOR_CONTEXT_UNCLEAR", "OWN_CLAUSE"):
        "A word in the instruction's own sentences is not one the bot understands.",
    ("ENTRY_ANCHOR_INSTRUMENT_UNCLEAR", "OTHER_WORDS"):
        "The line holding the level has a word that is not this market's name.",
    ("ENTRY_ANCHOR_INSTRUMENT_UNCLEAR", "PRECEDING_SENTENCE"):
        "The sentence before the level has a word the bot does not understand.",
    ("ENTRY_ANCHOR_LEVEL_NOT_MARKET_QUOTE", "FOLLOWING_NOT_UNDERSTOOD"):
        "The sentence after the level is not understood.",
    ("ENTRY_ANCHOR_LEVEL_NOT_MARKET_QUOTE", "INSTRUCTION_NOT_UNDERSTOOD"):
        "A word of the instruction sentence is not understood.",
    ("ENTRY_ANCHOR_LEVEL_NOT_MARKET_QUOTE", "NEIGHBOUR_NOT_UNDERSTOOD"):
        "The sentence before the level is not understood.",
    ("ENTRY_ANCHOR_LEVEL_NOT_MARKET_QUOTE", "OWN_CLAUSE:APPROXIMATION_MARK"):
        "The level carries an approximation mark; the stated number is used as written, with no "
        "added tolerance.",
}

# Reported by the evaluation hook only ("would be offered if the owner allowed this tier"); never
# offered, never confirmable in version 1 (lead's decision of 14 November 2026).
REPORT_ONLY: dict[tuple[str, str], str] = {
    ("HEADLINE_POSITION_LINE_WITHOUT_ENTRY_VERB", ""):
        "The line may be a position summary rather than an entry.",
    ("POSITION_STATEMENT_OR_ENTRY", ""):
        "The sentence may state a held position rather than a new entry.",
    ("ENTRY_ANCHOR_POST_UNATTRIBUTED_ACTION", "RULE:UNATTRIBUTED_ACTION_SENTENCE"):
        "Another sentence holds an instruction word.",
    ("ENTRY_ANCHOR_NOT_CURRENT", "NOT_CURRENT_WORD"):
        "The level's clause holds a word that marks it as another time.",
    ("ENTRY_ANCHOR_LEVEL_NOT_MARKET_QUOTE", "LEVEL_KIND:ENTRY_OR_COST_BASIS"):
        "The level reads as an entry price or a cost basis.",
    ("ENTRY_ANCHOR_LEVEL_NOT_MARKET_QUOTE", "LEVEL_KIND:HISTORICAL"):
        "The level reads as a past level.",
    ("ENTRY_ANCHOR_CONTEXT_UNCLEAR", "QUESTION"):
        "The level's sentence is a question.",
}

TABLE: dict[tuple[str, str], str] = {**CONFIRMABLE, **REPORT_ONLY}

# Interpreter ambiguities (``TradeAction.ambiguities`` before any mapping mark) in the table.
INTERPRETER_CODES = frozenset(c for c, d in TABLE if d == "")


def tier_of(pair: tuple[str, str]) -> str | None:
    if pair in CONFIRMABLE:
        return CONFIRMABLE_TIER
    if pair in REPORT_ONLY:
        return REPORT_ONLY_TIER
    return None


def detail_key(detail: str) -> str:
    """``WORDS:3`` -> ``WORDS``; a detail without a count or word is its own key."""
    head, sep, _ = detail.partition(":")
    if sep and head in ("WORDS", "OTHER_WORDS", "NOT_CURRENT_WORD"):
        return head
    return detail


def check_pairs(pairs) -> frozenset[tuple[str, str]]:
    """The waiver's argument: only pairs of the closed table; anything else raises."""
    out = frozenset(pairs)
    bad = [p for p in out if not (isinstance(p, tuple) and len(p) == 2 and p in TABLE)]
    if bad:
        raise ValueError(f"not in the closed confirmation table: {sorted(map(str, bad))}")
    return out


WAIVE_CONFIRMABLE = frozenset(CONFIRMABLE)
WAIVE_ALL = frozenset(TABLE)
