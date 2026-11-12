"""The entry rule and text that is not plain ASCII (independent review on f9d7256).

The entry rule's word lists, regular expressions and tokenisers were written for ASCII text: a word
holding an accented or non-Latin letter was split into fragments or skipped, so "an unknown word
holds" silently did not apply to it, and a second market named that way was invisible. This module
holds the one definition the entry rule uses for such text. It only ever turns a pass into a hold:

* A WORD is a maximal run of letters of any script (``str.isalpha()``), with any combining marks
  attached to them. A word holding any character outside ASCII is never equal to an allow-listed,
  family, alias, marker or instruction word (stricter than an NFKC look-alike rule: a full-width or
  look-alike spelling of a listed word is unknown too). ``ascii_shadow`` replaces each such word,
  after NFC normalisation, by a distinct ASCII placeholder that no list holds, keeping its leading
  capital, so every existing check treats it as the unknown word it is (a capitalised one is a
  mention). The rule is decided on the text as written AND on the shadow; the stricter result wins.
* INVISIBLE AND ODD CHARACTERS (fail closed): any format character (zero-width space, joiner and
  non-joiner, word joiner, soft hyphen, byte-order mark, bidirectional controls), any control
  character other than tab and newline, any space other than U+0020 (no-break, narrow, ideographic
  and the other exotic spaces), line and paragraph separators, private-use and unassigned code
  points: the entry holds (``ENTRY_TEXT_INVISIBLE_CHARACTER``). Nothing is normalised away.
* NUMBERS (fail closed): any digit or numeral outside ASCII 0-9 (full-width, Arabic-Indic,
  superscripts, fractions, Roman numerals), and the Unicode minus or any non-ASCII dash next to a
  digit: the entry holds (``ENTRY_TEXT_NUMBER_UNREADABLE``). Such a number is never an anchor or a
  stop.
"""

from __future__ import annotations

import re
import unicodedata

ENTRY_TEXT_INVISIBLE_CHARACTER = "ENTRY_TEXT_INVISIBLE_CHARACTER"
ENTRY_TEXT_NUMBER_UNREADABLE = "ENTRY_TEXT_NUMBER_UNREADABLE"

_ALLOWED_CONTROLS = frozenset("\t\n\r")
_INVISIBLE_CATEGORIES = frozenset({"Cf", "Cc", "Zl", "Zp", "Co", "Cn", "Cs"})
_NUMBER_CATEGORIES = frozenset({"Nd", "Nl", "No"})
# Dashes and minus signs outside ASCII (a sign or a range next to a digit is not read).
_DASHES = frozenset("‐‑‒–—―−﹘﹣－")
_PLACEHOLDER_RE = re.compile(r"[Qq]zu[a-z]{3}x")


def _invisible(ch: str) -> bool:
    if ch.isascii():
        return unicodedata.category(ch) == "Cc" and ch not in _ALLOWED_CONTROLS
    cat = unicodedata.category(ch)
    return cat in _INVISIBLE_CATEGORIES or cat == "Zs"


def entry_text_problem(*texts: str | None) -> str | None:
    """The fail-closed hold for invisible characters and for numbers outside ASCII; None when the
    text has neither (pure ASCII text never has a problem here, bar a stray control character)."""
    for text in texts:
        if not text:
            continue
        if any(_invisible(ch) for ch in text):
            return ENTRY_TEXT_INVISIBLE_CHARACTER
    for text in texts:
        if not text or text.isascii():
            continue
        for i, ch in enumerate(text):
            if ch.isascii():
                continue
            if unicodedata.category(ch) in _NUMBER_CATEGORIES:
                return ENTRY_TEXT_NUMBER_UNREADABLE
            if ch in _DASHES and any(
                0 <= j < len(text) and text[j].isdigit() for j in (i - 1, i + 1)
            ):
                return ENTRY_TEXT_NUMBER_UNREADABLE
    return None


def _letter_or_mark(ch: str) -> bool:
    return ch.isalpha() or unicodedata.category(ch).startswith("M")


def entry_text_placeholder(index: int, capital: bool) -> str:
    code = ""
    for _ in range(3):
        index, r = divmod(index, 26)
        code = chr(97 + r) + code
    return ("Q" if capital else "q") + f"zu{code}x"


def ascii_shadow(text: str | None) -> tuple[str | None, dict[str, str]]:
    """The text (NFC) with every WORD that holds a non-ASCII character replaced by a distinct ASCII
    placeholder; returns (shadow, placeholder -> original word). Pure ASCII text is returned as
    it is, with no placeholders. Characters that are not letters (punctuation, symbols, emoji) are
    kept, so no word is created that was not there."""
    if not text or text.isascii():
        return text, {}
    text = unicodedata.normalize("NFC", text)
    out, originals, i, n = [], {}, 0, len(text)
    while i < n:
        if not _letter_or_mark(text[i]):
            out.append(text[i])
            i += 1
            continue
        j = i
        while j < n and _letter_or_mark(text[j]):
            j += 1
        word = text[i:j]
        if word.isascii() or not any(ch.isalpha() for ch in word):
            out.append(word)
        else:
            first = next(ch for ch in word if ch.isalpha())
            ph = entry_text_placeholder(len(originals), first.isupper())
            originals[ph] = word
            out.append(ph)
        i = j
    return "".join(out), originals


def restore(value: str, originals: dict[str, str]) -> str:
    """Put the original words back where placeholders stand (reason details, private evidence)."""
    if not originals or not value:
        return value
    return _PLACEHOLDER_RE.sub(lambda m: originals.get(m.group(0), m.group(0)), value)


def has_non_ascii_word(*texts: str | None) -> bool:
    return any(ascii_shadow(t)[1] for t in texts if t)
