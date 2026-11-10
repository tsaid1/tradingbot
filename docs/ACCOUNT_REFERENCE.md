# Private exact account comparison (NON_AUTHORITATIVE)

`src/tradingbot/paper_readiness/account_ref.py` compares one expected account identifier with the
managed-account list a session reported, privately and exactly. It returns only closed, opaque evidence.

**This prepares E2 and does not clear it.**

- **Not wired in:** `ReadinessExpectation`, `SessionAttestation` and preflight are unchanged and still
  admit only `SYNTH-` identifiers. E2 stays open until separately reviewed schema and preflight integration.
- **No disguise:** a real identifier is never presented as `SYNTH-`.
- **No attestation:** nothing here attests a PAPER environment. A prefix, a port or a match never implies
  PAPER.

## API

```python
compare_account(expected, managed, key) -> AccountMatch
```

| Input | Must be |
|---|---|
| `expected` | One identifier |
| `managed` | An exact `list` or `tuple` of identifiers, at most 16, as the caller split them from the session's report |
| `key` | Exact `bytes`, 16–64 bytes, supplied by the caller for one run |

An identifier is valid when it meets all of these:

- an exact `str` (no subclass)
- non-empty
- strictly UTF-8 encodable (a lone surrogate is not)
- at most 64 UTF-8 bytes
- free of control characters (U+0000–U+001F, U+007F–U+009F)

Every other character, including spaces, is kept as it is. Nothing is trimmed, case-folded, Unicode
normalized, split or deduplicated. Comparison is equality of the UTF-8 bytes.

`AccountMatch` is frozen, with exactly three fields:

| Field | Values |
|---|---|
| `status` | `EXACT`, `MISMATCH`, `AMBIGUOUS`, `ABSENT` or `REFUSED` |
| `refusal` | A fixed `RefusalCode`, only when `REFUSED` |
| `reference` | Only when `EXACT`: `acctref-v1:` plus 32 hex characters |

The reference is a keyed `hashlib.blake2b` over the identifier's bytes, with a 16-byte digest and the
fixed domain `tb-acctref-v1`.

| Status | When |
|---|---|
| `EXACT` | Exactly one valid managed identifier, byte-equal to the expected one |
| `MISMATCH` | Exactly one valid managed identifier, not equal |
| `AMBIGUOUS` | More than one entry, including two equal ones |
| `ABSENT` | No entry |
| `REFUSED` | Malformed input: `KEY_INVALID`, `EXPECTED_INVALID`, `MANAGED_CONTAINER_INVALID`, `MANAGED_TOO_MANY` or `MANAGED_ENTRY_INVALID`. Never an ordinary mismatch, even beside an equal entry |

The checks run in a fixed order: key, expected identifier, container type, cardinality, then entries.
Each bound is checked before any iteration, comparison or hashing. `compare_account` never raises for bad
input.

Direct construction of `AccountMatch` is validated as well. A result can only carry the closed status and
code types, and a reference in the fixed format, consistent with its status. A failure raises a
`ValueError` with a fixed message. No identifier or key appears in a result, its `repr`, or an error.

## Limits

- **No freshness enforcement:** the caller supplies and owns the key. Reusing a key across runs makes
  references linkable across runs.
- **No anonymity or privacy guarantee:** with the key, anyone can test a guessed identifier against a
  reference.
- **No provenance:** nothing authenticates where an identifier or the managed list came from.
- **No timing claim:** comparison is ordinary equality, with no constant-time property.
- **No outside inputs:** it reads no environment, clock, file, network or randomness, and takes no SDK,
  broker or account input of its own.
