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
  superscripts, fractions, Roman numerals); any non-ASCII character directly touching an ASCII
  digit or inside what would otherwise be one number token (ASCII digits, full stops and commas):
  a separator (full-width full stop, middle dot, Arabic decimal separator), a minus or dash, and
  collaterally a currency, percent or degree sign; the ASCII forms that the rule would read as
  a different number than written ("88. 42" after an integer, "88 .42", "88 , 42", "88..42",
  "88.42.1"; a full stop after a decimal part is a sentence end); and a price READ for the action
  directly followed by spaces and a digit or by a letter ("88 42", "88.42k"): the entry holds
  (``ENTRY_TEXT_NUMBER_UNREADABLE``), wherever the number is in the post. Such a number is never
  an anchor, an entry, a stop or a second stop.
* DASHES AND SLASHES: in the shadow text every Unicode dash (category Pd, the minus sign) becomes
  an ASCII hyphen and every slash-like character (fraction, division, full-width solidus) an ASCII
  slash, so the second-market rules split "word\u2010word" and "word\u2044word" as they split
  the ASCII forms. A dash touching a digit has already held by the number rule.
"""

from __future__ import annotations

import contextlib
import re
import unicodedata
from decimal import Decimal, InvalidOperation

ENTRY_TEXT_INVISIBLE_CHARACTER = "ENTRY_TEXT_INVISIBLE_CHARACTER"
ENTRY_TEXT_NUMBER_UNREADABLE = "ENTRY_TEXT_NUMBER_UNREADABLE"

_ALLOWED_CONTROLS = frozenset("\t\n\r")
_INVISIBLE_CATEGORIES = frozenset({"Cf", "Cc", "Zl", "Zp", "Co", "Cn", "Cs"})
_NUMBER_CATEGORIES = frozenset({"Nd", "Nl", "No"})
# Dashes and minus signs outside ASCII (a sign or a range next to a digit is not read).
_DASHES = frozenset("‐‑‒–—―−﹘﹣－")
_PLACEHOLDER_RE = re.compile(r"[Qq]zu[a-z]{3}x")
_SLASHES = frozenset("\u2044\u2215\uff0f\u29f8")
# A number token: ASCII digits, full stops and commas, and any non-ASCII character that is not a
# letter or a space between them. One holding a non-ASCII character is not read.
_NUMBER_TOKEN_RE = re.compile(r"[0-9.,\u0080-\U0010ffff]*[0-9][0-9.,\u0080-\U0010ffff]*")
# ASCII forms read as a different number than written:
# * an INTEGER, a full stop, spaces, a digit ("88. 42"; also "Stop 87. 88.42", accepted
#   collateral); after a number that already has a decimal part ("87.20. 88.42") the full stop
#   is a sentence end and is read as before;
# * a digit, spaces, a full stop or comma, optional spaces, a digit ("88 .42", "88 . 42",
#   "88 , 42", "88 ,42");
# * a doubled separator ("88..42", "87..") and a second decimal point ("88.42.1").
_ODD_ASCII_NUMBER_RE = re.compile(
    r"(?<![\d.])\d+\.[ \t]+\d|\d[ \t]+[.,][ \t]*\d|\d[.,]{2,}|\d\.\d+\.\d")
# One ASCII number as written (thousands commas, a decimal part), and what follows it.
_READ_NUMBER_RE = re.compile(r"(?<![\d.,])\$?(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?")


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
        if not text:
            continue
        if _ODD_ASCII_NUMBER_RE.search(text):
            return ENTRY_TEXT_NUMBER_UNREADABLE
        if text.isascii():
            continue
        for i, ch in enumerate(text):
            if ch.isascii():
                continue
            if unicodedata.category(ch) in _NUMBER_CATEGORIES:
                return ENTRY_TEXT_NUMBER_UNREADABLE
            if any(0 <= j < len(text) and text[j] in "0123456789" for j in (i - 1, i + 1)):
                return ENTRY_TEXT_NUMBER_UNREADABLE  # touching an ASCII digit
        for m in _NUMBER_TOKEN_RE.finditer(text):
            token = m.group(0)
            if not token.isascii() and any(
                not ch.isascii() and not ch.isalpha() and not ch.isspace() for ch in token
            ):
                return ENTRY_TEXT_NUMBER_UNREADABLE
    return None


def read_price_problem(prices, *texts: str | None) -> str | None:
    """A price the rule actually READ for this action (its entry or reference level, its stop, its
    target) written in the post as a number token directly followed by spaces and another digit
    ("88 42" read as 88) or directly followed by an ASCII letter ("88.42k"): the number as read may
    not be the number as written, so the entry holds. Any other number never triggers this
    ("Nov 12 2026", "S&P 500", "10Y", "200DMA", "3pm")."""
    wanted = set()
    for p in prices:
        with contextlib.suppress(InvalidOperation, TypeError, ValueError):
            if p is not None and str(p).strip():
                wanted.add(Decimal(str(p).replace(",", "").lstrip("$")))
    if not wanted:
        return None
    for text in texts:
        for m in _READ_NUMBER_RE.finditer(text or ""):
            value = Decimal(m.group(1).replace(",", "") + (m.group(2) or ""))
            if value not in wanted:
                continue
            rest = text[m.end():]
            if re.match(r"[ \t]+\d|[A-Za-z]", rest):
                return ENTRY_TEXT_NUMBER_UNREADABLE
    return None


# A sentence end for the cost-basis rule: ". ! ?" or a line break (not ";"), a full stop not
# before a digit and not ending the abbreviations "avg." and "av.".
_COST_SENTENCE_END_RE = re.compile(r"[!?\n]|(?<![Aa][Vv][Gg])(?<![Aa][Vv])\.(?!\d)")
_WORD_RE = re.compile(r"[^\W\d_]+")
_GLUED_AVG_RE = re.compile(r"\b(avg|av)\.(?=\d)", re.IGNORECASE)
_POSITION_FROM_RE = re.compile(r"\b(?:from|since)\s+\$?$", re.IGNORECASE)


def _cost_sentences(text: str) -> list[tuple[int, int]]:
    spans, start = [], 0
    for m in [*_COST_SENTENCE_END_RE.finditer(text), None]:
        end = m.start() if m else len(text)
        spans.append((start, end))
        start = m.end() if m else end
    return spans


def _numbers(text: str) -> list[tuple[int, int, Decimal]]:
    return [(m.start(), m.end(), Decimal(m.group(1).replace(",", "") + (m.group(2) or "")))
            for m in _READ_NUMBER_RE.finditer(text)]


def entry_is_cost_basis(entry, words, phrases, own_levels, *texts: str | None) -> bool:
    """The owner's rule of 13 November 2026 (and the PR #75 review): is the action's stated entry
    price, as written in the post (every place the value is written), possibly an average or cost
    basis rather than a fresh entry? A sentence ends at ". ! ?" or a line break, never at ";".
    (i) a cost-basis word or phrase anywhere in the entry's sentence, before or after the number;
    (ii) one in the directly following sentence when that sentence carries no number of its own
    ("That is our average."); (iii) the post holds a cost-basis expression anywhere and the
    entry's sentence holds another price that is not one of the action's own stop or target
    levels (``own_levels``): which of several fills is the fresh entry cannot be told; (iv)
    "from" or "since" directly before the price ("long from 88.42"); (v) a word holding a
    non-ASCII letter in the entry's sentence or the following one: unreadable for this purpose
    (a look-alike "avg" never passes)."""
    try:
        value = Decimal(str(entry).replace(",", "").lstrip("$"))
    except (InvalidOperation, TypeError, ValueError):
        return False
    own = set()
    for p in own_levels:
        with contextlib.suppress(InvalidOperation, TypeError, ValueError):
            if p is not None and str(p).strip():
                own.add(Decimal(str(p).replace(",", "").lstrip("$")))

    def cost(t: str) -> bool:
        return bool({w.lower() for w in _WORD_RE.findall(t)} & words or phrases.search(t))

    def non_ascii_word(t: str) -> bool:
        return any(not w.isascii() for w in _WORD_RE.findall(t))

    for raw in texts:
        if not raw:
            continue
        text = _GLUED_AVG_RE.sub(r"\1. ", raw)
        spans = _cost_sentences(text)
        anywhere = cost(text)
        for start, end, v in _numbers(text):
            if v != value:
                continue
            k = next(i for i, (a, b) in enumerate(spans) if a <= start <= b)
            sentence = text[spans[k][0]:spans[k][1]]
            following = text[spans[k + 1][0]:spans[k + 1][1]] if k + 1 < len(spans) else ""
            if cost(sentence) or non_ascii_word(sentence) or non_ascii_word(following):
                return True
            if following.strip() and cost(following) and not _numbers(following):
                return True
            if _POSITION_FROM_RE.search(text[spans[k][0]:start]):
                return True
            if anywhere and any(x not in own and x != value
                                for _, _, x in _numbers(sentence)):
                return True
    return False


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
    text = "".join(
        "-" if ch in _DASHES or (not ch.isascii() and unicodedata.category(ch) == "Pd")
        else "/" if ch in _SLASHES else ch
        for ch in unicodedata.normalize("NFC", text))
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
            # A letter of a script without case (Japanese, Arabic, Hebrew, Thai, Chinese) may
            # begin a name: its placeholder is capitalised, so it is read as a possible name.
            ph = entry_text_placeholder(len(originals), first.isupper() or not first.islower())
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
    """True when the shadow text differs from the text: a non-ASCII word, dash or slash."""
    return any(ascii_shadow(t)[0] != t for t in texts if t)
