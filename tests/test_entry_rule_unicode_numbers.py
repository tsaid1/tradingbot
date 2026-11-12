"""Numbers with a non-ASCII separator, odd ASCII number forms, and Unicode dashes and slashes in the
second-market rules (review of PR #73). Through the whole mock service on the shipped mapping
(TLT, mock ask 88.42). Invented text only.

The number rule: any non-ASCII character directly touching an ASCII digit, or inside what would
otherwise be one number token (ASCII digits, full stops and commas), holds the entry
(ENTRY_TEXT_NUMBER_UNREADABLE), wherever the number is in the post. So do the odd ASCII forms that
would be read as a different number than written ("88. 42", "88 .42", "88..42", "88.42.1").
"""

from __future__ import annotations

import tempfile
from decimal import Decimal
from pathlib import Path

import pytest
from runtime_harness import MAPPING, Runtime, post

from tradingbot.runtime.entry_text import ENTRY_TEXT_NUMBER_UNREADABLE, entry_text_problem

FULL_STOP, MIDDLE_DOT, ARABIC_DECIMAL = "．", "·", "٫"
SEPARATORS = [FULL_STOP, MIDDLE_DOT, ARABIC_DECIMAL]
EXPLICIT = "We are now long $TLT at {level}. Stop {stop}."
ANCHORED = "We are now long $TLT, {level} now. Stop {stop}."


def run(text):
    rt = Runtime(Path(tempfile.mkdtemp()), [post("1", 0, text)], mapping_file=MAPPING)
    rt.run(3, 30)
    opens = [a for a in rt.actions() if a["action"] == "OPEN"]
    orders = rt.broker.all_orders()
    rt.service.close()
    return opens, orders


def held(text):
    opens, orders = run(text)
    assert orders == [], (text, [(a["status"], a["mapping_reason"]) for a in opens])
    assert all(a["status"] in ("HOLD", "BLOCK") for a in opens), opens
    return opens


def cases():
    for sep in SEPARATORS:
        for name, shape in (("explicit", EXPLICIT), ("anchored", ANCHORED)):
            yield f"{name}-level-{ord(sep):x}", shape.format(level=f"88{sep}42", stop="87.20")
            yield f"{name}-stop-{ord(sep):x}", shape.format(level="88.42", stop=f"87{sep}20")
            yield (f"{name}-second-stop-{ord(sep):x}",
                   shape.format(level="88.42", stop="87.20") + f" Stop 87{sep}10.")


@pytest.mark.parametrize("text", [t for _, t in cases()], ids=[i for i, _ in cases()])
def test_a_non_ascii_separator_in_a_number_holds(text):
    held(text)


@pytest.mark.parametrize("text", [
    EXPLICIT.format(level="€88.42", stop="87.20"),  # a currency sign touching the level
    EXPLICIT.format(level="88.42", stop="87.20") + " Fees fell 2％ today.",  # full-width %
    ANCHORED.format(level="88.42", stop="87.20") + " It cost £12 more.",  # the pound sign
    ANCHORED.format(level="88.42", stop="87.20") + " Up 3‰ on the day.",  # per mille
])
def test_a_non_ascii_currency_or_percent_sign_touching_a_digit_holds(text):
    held(text)


def test_the_rule_reads_the_text_not_a_list():
    assert entry_text_problem("at 88．42") == ENTRY_TEXT_NUMBER_UNREADABLE
    assert entry_text_problem("at 88,·,42") == ENTRY_TEXT_NUMBER_UNREADABLE  # inside a token
    assert entry_text_problem("at 12° today") == ENTRY_TEXT_NUMBER_UNREADABLE
    assert entry_text_problem("at 88.42 — calm") is None  # spaced away from the digits
    assert entry_text_problem("at 88.42. Stop 87.20.") is None


# ---------------------------------------------------------------- odd ASCII forms


@pytest.mark.parametrize("text", [
    EXPLICIT.format(level="88. 42", stop="87.20"),  # was sent as a buy limit of 88
    EXPLICIT.format(level="88 .42", stop="87.20"),  # was sent at 88
    EXPLICIT.format(level="88..42", stop="87.20"),  # was sent at 88
    EXPLICIT.format(level="88.42.1", stop="87.20"),  # was sent at 88.42
    EXPLICIT.format(level="88.42", stop="87. 20"),  # the stop was placed at 87
    EXPLICIT.format(level="88.42", stop="87..20"),  # the stop was placed at 87
    EXPLICIT.format(level="88.42", stop="87.20.1"),  # the stop was placed at 87.20
    ANCHORED.format(level="88. 42", stop="87.20"),
    ANCHORED.format(level="88.42.1", stop="87.20"),
])
def test_an_ascii_number_read_as_a_different_number_holds(text):
    held(text)


def test_odd_ascii_forms_that_are_read_as_written():
    # A whole number before a sentence end is read as written: a buy limit of 88 (it rests).
    _, orders = run(EXPLICIT.format(level="88", stop="87.20"))
    [entry] = [o for o in orders if o.parent_ref is None]
    assert entry.limit_price == Decimal(88)
    # "1.20" is read as 1.20 (a stop 98.6 % away: held); "1,20" is no number (no stop: held).
    assert "PROTECTIVE_STOP_TOO_FAR_FROM_ENTRY" in held(
        EXPLICIT.format(level="88.42", stop="1.20"))[0]["reasons"]
    assert "PROTECTIVE_STOP_MISSING" in held(EXPLICIT.format(level="88.42", stop="1,20"))[0][
        "reasons"]


def test_a_clean_ascii_post_is_unchanged():
    for shape in (EXPLICIT, ANCHORED):
        opens, orders = run(shape.format(level="88.42", stop="87.20"))
        assert opens[0]["status"] == "EXECUTED" and len(orders) == 2


# ---------------------------------------------------------------- dashes and slashes


@pytest.mark.parametrize("text", [
    "We are now long $TLT, 88.42 now. Stop 87.20. Qorvex‐long as well.",  # hyphen U+2010
    "We are now long $TLT, 88.42 now. Stop 87.20. Qorvex‑long as well.",  # non-breaking
    "We are now long $TLT, 88.42 now. Stop 87.20. Long⁄short Qorvex.",  # fraction slash
    "We are now long $TLT, 88.42 now. Stop 87.20. Long∕short Qorvex.",  # division slash
    "We are now long $TLT, 88.42 now. Stop 87.20. Long／short Qorvex.",  # full-width solidus
])
def test_a_unicode_dash_or_slash_splits_like_its_ascii_form(text):
    twin = text
    for ch in "‐‑":
        twin = twin.replace(ch, "-")
    for ch in "⁄∕／":
        twin = twin.replace(ch, "/")
    held(twin)  # the ASCII form holds today
    held(text)


# ---------------------------------------------------------------- second fix round (PR #73)


@pytest.mark.parametrize("text", [
    "We are now long $TLT. Stop 87.20. 88.42 now.",  # the stand-alone level shape on one line
    "We are now long $TLT at 88.42. Stop 87.20. 2 targets ahead.",
    "We are now long $TLT at 88.42. Stop 87.20. Target 89.50. 3 weeks left.",
])
def test_a_full_stop_after_a_decimal_number_is_a_sentence_end(text):
    """Read exactly as before this PR (11cc2d3): sent at the ask."""
    opens, orders = run(text)
    assert opens[0]["status"] == "EXECUTED", opens
    [entry] = [o for o in orders if o.parent_ref is None]
    assert entry.limit_price == Decimal("88.42")


def test_an_integer_then_a_full_stop_then_a_digit_still_holds():
    """Accepted collateral: "87. 88.42" cannot be told from a broken decimal."""
    assert held("We are now long $TLT. Stop 87. 88.42 now.")[0]["mapping_reason"].startswith(
        ENTRY_TEXT_NUMBER_UNREADABLE)


@pytest.mark.parametrize("level", ["88 . 42", "88 , 42", "88 ,42", "88 .42", "88. 42"])
def test_a_separator_with_spaces_inside_a_number_holds(level):
    held(EXPLICIT.format(level=level, stop="87.20"))


@pytest.mark.parametrize("text", [
    EXPLICIT.format(level="88 42", stop="87.20"),  # was sent as a buy limit of 88
    EXPLICIT.format(level="88.42k", stop="87.20"),  # was sent at 88.42
    EXPLICIT.format(level="88.42x", stop="87.20"),
    EXPLICIT.format(level="88.42", stop="87.20k"),
    "We are now long $TLT at 88.42. Stop 87.20 5 below.",
])
def test_a_read_price_glued_to_a_letter_or_followed_by_another_number_holds(text):
    held(text)


@pytest.mark.parametrize("text", [
    "We are now long $TLT at 88.42, stop 87.20.",
    "We are now long $TLT at 88.42. Stop 87.20. The index sat at 4,512.25 and volume 1,250 lots.",
    "We are now long $TLT at 88.42. Stop 87.20. Nov 12 2026 was quiet.",
    "We are now long $TLT at 88.42. Stop 87.20. Eyes on the 10Y and the 20yr.",
    "We are now long $TLT at 88.42. Stop 87.20. It sits above the 200DMA into 3pm.",
    "We are now long $TLT at 88.42. Stop 87.20. The S&P 500 is calm.",
])
def test_ordinary_numbers_that_are_not_the_actions_prices_still_send(text):
    opens, _ = run(text)
    assert opens[0]["status"] == "EXECUTED", opens


def test_a_comma_list_of_prices_reads_as_before():
    # "88.42, 87.20 is the stop": the second number is not read as the stop (held, as before).
    assert "PROTECTIVE_STOP_MISSING" in held(
        "We are now long $TLT at 88.42, 87.20 is the stop.")[0]["reasons"]
