"""The entry rule and text that is not plain ASCII (independent review on f9d7256): a second market
named with an accented or non-Latin letter was invisible to the competing-market check, and the
entry EXECUTED. Invisible characters, look-alike letters and digits outside ASCII must never
hide a word, a number or a sentence boundary: such a post holds. Through the whole mock service on
the shipped mapping. Invented text only.
"""

from __future__ import annotations

import tempfile
import unicodedata
from pathlib import Path

import pytest
from runtime_harness import MAPPING, Runtime, post

from tradingbot.runtime.entry_text import (
    ENTRY_TEXT_INVISIBLE_CHARACTER,
    ENTRY_TEXT_NUMBER_UNREADABLE,
    ascii_shadow,
    entry_text_problem,
)

ANCHORED = "We are now long $TLT, 88.42 now. Stop 87.20."
EXPLICIT = "We are now long $TLT at 88.42. Stop 87.20."
ZW = "\u200b"


def run(text):
    rt = Runtime(Path(tempfile.mkdtemp()), [post("1", 0, text)], mapping_file=MAPPING)
    rt.run(3, 30)
    opens = [a for a in rt.actions() if a["action"] == "OPEN"]
    sent = [o for o in rt.broker.all_orders() if o.parent_ref is None]
    rt.service.close()
    return opens, sent


def held(text):
    opens, sent = run(text)
    assert sent == [], (text, [(a["status"], a["mapping_reason"]) for a in opens])
    return opens


# ---------------------------------------------------------------- the clean posts still trade


@pytest.mark.parametrize("text", [ANCHORED, EXPLICIT])
def test_a_clean_ascii_post_is_unchanged(text):
    opens, sent = run(text)
    assert len(sent) == 1 and opens[0]["status"] == "EXECUTED"


# ---------------------------------------------------------------- the reviewer's post and variants


@pytest.mark.parametrize("text", [
    f"{ANCHORED} Zörbleflux sits near 12.40.",  # the reviewer's post: with a price
    f"{ANCHORED} Zörbleflux bought back.",  # with an instruction word
    f"{ANCHORED} Zörbleflux is bought here.",  # at the start of a sentence
    "We are now long $TLT and Zörbleflux, 88.42 now. Stop 87.20.",  # in the instruction sentence
    "Zörbleflux at 12.40. We are now long $TLT, 88.42 now. Stop 87.20.",  # neighbour, before
    "We are now long $TLT, 88.42 now. Zörbleflux at 12.40. Stop 87.20.",  # neighbour, after
    "We are now long $TLT, 88.42 now. Zörbleflux. Stop 87.20.",  # after the level, alone
    "We are now long $TLT, 88.42 nöw. Stop 87.20.",  # a look-alike of the present marker
    f"{ANCHORED} \u03a4L\u03a4 sits near 12.40.",  # the family's name with Greek capital taus
    f"{ANCHORED} \uff3aorbleflux sits near 12.40.",  # a full-width letter
    f"{ANCHORED} Zo\u0308rbleflux sits near 12.40.",  # a combining mark (decomposed)
])
def test_a_word_with_a_non_ascii_letter_is_an_unknown_word(text):
    for o in held(text):
        assert o["status"] in ("HOLD", "BLOCK")


@pytest.mark.parametrize("text", [
    f"{ANCHORED} Zörbleflux looks heavy.",  # a mention with no trading context in its sentence
    "Zörbleflux. We are now long $TLT, 88.42 now. Stop 87.20.",
    f"{EXPLICIT} Zörbleflux sits near 12.40.",  # explicit TLT entries get the checks too (13 Nov 2026)
])
def test_otherwise_a_non_ascii_word_is_held_no_less_than_its_ascii_twin(text):
    """Same policy as any unknown word: never laxer than the same post spelled in ASCII."""
    twin = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    _, sent = run(text)
    _, twin_sent = run(twin)
    assert len(sent) <= len(twin_sent)
    if not twin_sent:
        assert not sent


# ---------------------------------------------------------------- invisible characters


@pytest.mark.parametrize("text", [
    f"We are now long $T{ZW}LT, 88.42 now. Stop 87.20.",  # inside the ticker
    f"We are now long $TLT, 88.{ZW}42 now. Stop 87.20.",  # inside the level
    f"We are now long $TLT, 88.42 n{ZW}ow. Stop 87.20.",  # inside "now"
    f"We are now long $TLT, 88.42 now. St{ZW}op 87.20.",  # inside a stop word
    f"We are now long $TLT, 88.42 now.{ZW}Stop 87.20.",  # between sentences
    "We are now long $TLT, 88.42\u00a0now. Stop 87.20.",  # a no-break space
    "We are now long $TLT, 88.42 now. Stop 87.20.\u202e",  # a bidirectional control
    "We are now long $TLT, 88.42 now. Sto\u00adp 87.20.",  # a soft hyphen
    f"{EXPLICIT}\u200d",  # a joiner, on the explicit path
])
def test_an_invisible_character_holds_the_entry(text):
    for o in held(text):
        assert o["status"] == "HOLD"


# ---------------------------------------------------------------- numbers


@pytest.mark.parametrize("text", [
    "We are now long $TLT, ８８.４２ now. Stop 87.20.",  # full-width digits in the level
    "We are now long $TLT, 88.42 now. Stop ٨٧.٢٠.",  # Arabic-Indic digits in the stop
    "We are now long $TLT at 88.42. Stop ８７.２０.",  # the explicit path
    f"{ANCHORED} Last week it was \u221212.40 on the spread.",  # the Unicode minus
    "We are now long $TLT, 88.42 now. Stop 87\u00a0020.",  # a no-break thousands separator
    # A decimal comma is plain ASCII: already never read as the level or the stop (pinned).
    "We are now long $TLT, 88,42 now. Stop 87.20.",
    "We are now long $TLT at 88,42. Stop 87.20.",
    "We are now long $TLT, 88.42 now. Stop 87,20.",
    "We are now long $TLT at 88.42. Stop 87,20.",
])
def test_a_number_outside_ascii_holds_the_entry(text):
    for o in held(text):
        assert o["status"] == "HOLD"


# ---------------------------------------------------------------- commentary


def test_accented_commentary_far_from_the_level_holds_no_more_than_its_ascii_twin():
    accented, _ = run(f"{ANCHORED} Bonds look firm. The café was quiet.")
    twin, _ = run(f"{ANCHORED} Bonds look firm. The cafe was quiet.")
    assert [a["status"] for a in accented] == [a["status"] for a in twin] == ["EXECUTED"]


# ---------------------------------------------------------------- the helpers


def test_the_helpers():
    assert entry_text_problem("plain ascii 88.42.") is None
    assert entry_text_problem(f"a{ZW}b") == ENTRY_TEXT_INVISIBLE_CHARACTER
    assert entry_text_problem("at ８８") == ENTRY_TEXT_NUMBER_UNREADABLE
    assert entry_text_problem("café 88.42") is None  # a letter, not a number or an invisible
    shadow, originals = ascii_shadow("Zörbleflux and café, ΤLΤ's 12.40 — ok")
    assert shadow.endswith("12.40 - ok")  # the em dash is folded to an ASCII hyphen
    assert "Zörbleflux" not in shadow and "café" not in shadow
    words = sorted(originals.values())
    assert words == sorted(["Zörbleflux", "café", "ΤLΤ"])
    for placeholder, word in originals.items():
        assert placeholder[0].isupper() == word[0].isupper() and placeholder.isascii()
    assert ascii_shadow("plain") == ("plain", {})


# ---------------------------------------------------------------- the owner's vocabulary review


def test_a_non_ascii_unknown_word_is_offered_to_the_owner_as_written(tmp_path):
    import json

    from test_vocab_review_service import private_dir
    from test_vocab_review_service import run as review_run

    log = private_dir(tmp_path) / "review.jsonl"
    rt = review_run(tmp_path / "a", ["We are now long $TLT zörbly, 88.42 now. Stop 87.20."],
                    review_log=log)
    [line] = [json.loads(x) for x in log.read_text().splitlines()]
    assert line["tokens"] == [{"token": "zörbly", "kind": "UNKNOWN_WORD"}]
    assert {"role": "LEVEL", "text": "We are now long $TLT zörbly, 88.42 now"} in line["sentences"]
    assert not any("qzu" in json.dumps(x).lower() for x in (line["tokens"], line["sentences"]))
    rt.service.close()


def test_an_owner_accepted_non_ascii_word_still_never_lets_the_entry_trade(tmp_path):
    from test_vocab_review_service import opens, owner_lexicon, private_dir
    from test_vocab_review_service import run as review_run

    from tradingbot.runtime import vocab_review as V

    text = "We are now long $TLT zörbly, 88.42 now. Stop 87.20."
    log = private_dir(tmp_path) / "review.jsonl"
    lex = owner_lexicon(tmp_path)
    first = review_run(tmp_path / "first", [text], review_log=log, lexicon=lex)
    first.service.close()
    entries, _ = V.read_review_log(log)
    [prop] = V.proposals_from_log(entries, lexicon_path=lex)
    V.apply_batch(lex, [V.BatchItem(prop.token, V.ACCEPT, prop.group, prop.family)])
    second = review_run(tmp_path / "second", [text], review_log=log, lexicon=lex)
    assert [a["status"] for a in opens(second)] == ["HOLD"]
    second.service.close()
