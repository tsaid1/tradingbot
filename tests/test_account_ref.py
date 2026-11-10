"""Private exact account comparison. Invented identifiers and keys only.

Covers exact equality, zero/multiple/duplicate cardinality, whitespace/case/prefix/Unicode variants,
malformed types, bounds and UTF-8, key validity and variation, and leak canaries in results, errors
and direct construction.
"""
from __future__ import annotations

import pytest

from tradingbot.paper_readiness import account_ref as ar
from tradingbot.paper_readiness.account_ref import (
    AccountMatch,
    MatchStatus,
    RefusalCode,
    compare_account,
)

ID = "INVENTED7Q31"          # invented; shaped like nothing real
OTHER = "INVENTED7Q32"
KEY = b"k" * 32
KEY_2 = b"j" * 32
CANARY_ID = "CANARYIDENT9"
CANARY_KEY = b"CANARYKEYBYTES-0123456789"


class StrSub(str):
    pass


class ListSub(list):
    pass


class BytesSub(bytes):
    pass


# ----------------------------------------------------------------------------- exact equality and key

def test_exact_match_on_one_equal_identifier_gives_a_fixed_opaque_reference():
    result = compare_account(ID, [ID], KEY)
    assert result.status is MatchStatus.EXACT and result.refusal is None
    assert result.reference.startswith(ar.REFERENCE_PREFIX) and len(result.reference) == 11 + 32
    assert compare_account(ID, (ID,), KEY) == result  # tuple and list agree
    assert compare_account(ID, [ID], KEY) == result   # deterministic for one key


def test_the_reference_varies_with_the_key_and_the_identifier():
    a = compare_account(ID, [ID], KEY).reference
    assert compare_account(ID, [ID], KEY_2).reference != a
    assert compare_account(OTHER, [OTHER], KEY).reference != a


@pytest.mark.parametrize("key", [b"", b"k" * 15, b"k" * 65, "k" * 32, bytearray(b"k" * 32),
                                 memoryview(b"k" * 32), BytesSub(b"k" * 32), None, 32],
                         ids=["empty", "short", "long", "str", "bytearray", "memoryview", "subclass",
                              "none", "int"])
def test_an_invalid_key_is_refused_before_anything_is_compared(key):
    result = compare_account(ID, [ID], key)
    assert (result.status, result.refusal, result.reference) == (MatchStatus.REFUSED,
                                                                 RefusalCode.KEY_INVALID, None)


@pytest.mark.parametrize("size", [ar.MIN_KEY_BYTES, ar.MAX_KEY_BYTES])
def test_key_size_bounds_are_inclusive(size):
    assert compare_account(ID, [ID], b"k" * size).status is MatchStatus.EXACT


# ----------------------------------------------------------------------------- cardinality

@pytest.mark.parametrize(("managed", "status"), [
    ([], MatchStatus.ABSENT),
    ([ID, OTHER], MatchStatus.AMBIGUOUS),
    ([OTHER, ID], MatchStatus.AMBIGUOUS),
    ([ID, ID], MatchStatus.AMBIGUOUS),  # no deduplication
], ids=["zero", "multiple", "multiple-reversed", "duplicate"])
def test_zero_or_multiple_managed_identifiers_are_never_exact(managed, status):
    result = compare_account(ID, managed, KEY)
    assert result.status is status and result.reference is None and result.refusal is None


def test_too_many_managed_entries_are_refused_before_any_entry_is_examined():
    managed = [object()] * (ar.MAX_MANAGED_ACCOUNTS + 1)  # entries that would fail validation
    assert compare_account(ID, managed, KEY).refusal is RefusalCode.MANAGED_TOO_MANY


# ----------------------------------------------------------------------------- no normalization

@pytest.mark.parametrize("managed_id", [
    " " + ID, ID + " ", ID.lower(), ID[:-1], ID + "1", "PREFIX" + ID, "Ａ" + ID[1:],
], ids=["leading-space", "trailing-space", "case", "truncated", "extended", "prefixed", "fullwidth"])
def test_whitespace_case_prefix_and_lookalike_variants_are_mismatch(managed_id):
    result = compare_account(ID, [managed_id], KEY)
    assert result.status is MatchStatus.MISMATCH and result.reference is None


def test_unicode_is_compared_as_utf8_bytes_without_normalization():
    composed, decomposed = "INVÉNTED", "INVÉNTED"
    assert compare_account(composed, [composed], KEY).status is MatchStatus.EXACT
    assert compare_account(composed, [decomposed], KEY).status is MatchStatus.MISMATCH


# ----------------------------------------------------------------------------- malformed input

@pytest.mark.parametrize("expected", [
    "", 123, None, b"INVENTED", StrSub(ID), "\ud800", "INV\nENTED", "INV\x00", "INV\x85",
    "A" * (ar.MAX_IDENTIFIER_BYTES + 1), "é" * 33,
], ids=["empty", "int", "none", "bytes", "str-subclass", "lone-surrogate", "newline", "nul", "c1-control",
        "too-long", "too-many-utf8-bytes"])
def test_a_malformed_expected_identifier_is_refused_not_mismatched(expected):
    result = compare_account(expected, [ID], KEY)
    assert result.status is MatchStatus.REFUSED and result.refusal is RefusalCode.EXPECTED_INVALID


@pytest.mark.parametrize("managed", [
    {ID}, ID, (x for x in [ID]), ListSub([ID]), None, {ID: 1},
], ids=["set", "str", "generator", "list-subclass", "none", "dict"])
def test_a_managed_container_of_the_wrong_type_is_refused(managed):
    assert compare_account(ID, managed, KEY).refusal is RefusalCode.MANAGED_CONTAINER_INVALID


@pytest.mark.parametrize("managed", [
    [ID, ""], [ID, 7], [StrSub(ID)], ["\udc80"], [ID + "\t"], ["A" * (ar.MAX_IDENTIFIER_BYTES + 1)],
], ids=["empty-entry", "int-entry", "subclass-entry", "surrogate-entry", "control-entry", "long-entry"])
def test_a_malformed_managed_entry_is_refused_even_beside_an_equal_one(managed):
    assert compare_account(ID, managed, KEY).refusal is RefusalCode.MANAGED_ENTRY_INVALID


@pytest.mark.parametrize("size", [1, ar.MAX_IDENTIFIER_BYTES])
def test_identifier_byte_bounds_are_inclusive(size):
    value = "A" * size
    assert compare_account(value, [value], KEY).status is MatchStatus.EXACT


# ----------------------------------------------------------------------------- leak canaries

@pytest.mark.parametrize(("expected", "managed", "key"), [
    (CANARY_ID, [CANARY_ID], CANARY_KEY),
    (CANARY_ID, [OTHER], CANARY_KEY),
    (CANARY_ID, [CANARY_ID, CANARY_ID], CANARY_KEY),
    (CANARY_ID + "\n", [CANARY_ID], CANARY_KEY),
    (CANARY_ID, [CANARY_ID], b"short"),
], ids=["exact", "mismatch", "ambiguous", "refused-expected", "refused-key"])
def test_no_identifier_or_key_reaches_a_result(expected, managed, key):
    result = compare_account(expected, managed, key)
    rendered = repr(result) + str(result) + repr(result.status) + repr(result.refusal)
    assert CANARY_ID not in rendered and "CANARYKEY" not in rendered
    assert set(AccountMatch.__slots__) == {"status", "refusal", "reference"}


@pytest.mark.parametrize("fields", [
    {"status": MatchStatus.EXACT, "reference": CANARY_ID},
    {"status": MatchStatus.EXACT, "reference": ar.REFERENCE_PREFIX + CANARY_ID},
    {"status": MatchStatus.EXACT},
    {"status": MatchStatus.MISMATCH, "reference": ar.REFERENCE_PREFIX + "0" * 32},
    {"status": MatchStatus.REFUSED},
    {"status": MatchStatus.MISMATCH, "refusal": RefusalCode.KEY_INVALID},
    {"status": CANARY_ID},
    {"status": "EXACT", "reference": ar.REFERENCE_PREFIX + "0" * 32},
    {"status": MatchStatus.REFUSED, "refusal": CANARY_ID},
], ids=["raw-reference", "prefixed-raw", "exact-without-reference", "reference-on-mismatch",
        "refused-without-code", "code-without-refusal", "raw-status", "string-status", "raw-refusal"])
def test_direct_construction_is_validated_and_never_echoes_its_inputs(fields):
    with pytest.raises(ValueError) as caught:
        AccountMatch(**fields)
    assert CANARY_ID not in str(caught.value) and CANARY_ID not in repr(caught.value)


def test_a_valid_direct_construction_and_immutability():
    reference = ar.REFERENCE_PREFIX + "a" * 32
    result = AccountMatch(MatchStatus.EXACT, reference=reference)
    assert result.reference == reference
    with pytest.raises(AttributeError):
        result.status = MatchStatus.MISMATCH  # type: ignore[misc]
