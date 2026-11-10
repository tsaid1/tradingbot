# Private account dataflow, version 2 (closed invented replay, NON_AUTHORITATIVE)

Version 2 carries one run's managed identity through the whole readiness flow:

1. A private per-run owner holds the expected identifier, the managed-account snapshot and the key.
2. Only the owner's own T1 comparison issues a binding.
3. Requests carry the binding, never an identifier.
4. Callback rows are compared privately before projection.
5. The existing deterministic checks run unchanged.
6. The report carries only an opaque reference.

It runs only as a **closed invented replay** of fixed scenarios. It admits no external account or session,
and it accepts no callable, client, SDK, raw resolver or transport kind. No acceptance, retirement or
trading rule changes, and PAPER-READ-1 is unchanged.

## Version and API changes

Version 1 is unchanged. All 20 existing scenarios render byte-identical reports. Every v1 schema, default
export, SYNTH check, FAKE-only refusal and real-use refusal is retained.

| File | Change |
|---|---|
| `observations.py` | `RECORDS_VERSION_V2 = 2`, `ACCOUNT_REF_PATTERN`. `ReadinessExpectationV2` carries `account_ref` (the reference pattern) and never an identifier. `SessionAttestationV2` stays `synthetic: True` and has **no** managed-account list |
| `preflight.py` | `preflight_v2(expectation, session, account_status, binding_owned)` maps the owner's status onto the existing refusals (`ACCOUNT_EVIDENCE_ABSENT`, `ACCOUNT_NOT_IN_MANAGED_LIST`, `MANAGED_ACCOUNTS_NOT_SINGULAR`) and adds `ACCOUNT_EVIDENCE_REFUSED` and `BINDING_REFUSED`. The session checks are shared helpers in the original order |
| `runner.py` | `run_private_account_replay(replay)` accepts only a `PrivateAccountReplay` and emits `records_version: 2` with `account_ref` set to the reference, or `UNBOUND`. The four reads are one helper shared with v1. `run_readiness_check` now refuses any expectation that is not exactly a v1 `ReadinessExpectation` (`RECORDS_VERSION_MISMATCH`) |
| `account_position_callbacks.py` | An optional keyword-only `owner=` on both producers, default `None` (v1 unchanged). It requires an exact, open `AccountOwner` holding its own binding |
| `private_account.py` (new) | `AccountOwner`, `Binding`, `PrivateAccountReplay` and its fixed `REPLAY_SCENARIOS`, which are not exported from the package |

Versions never mix:
- `preflight_v2` refuses v1 records.
- The v1 runner refuses v2 expectations, and still refuses any non-v1 session.
- `run_private_account_replay` refuses anything but its replay.
- Nothing is converted from one version to the other.

## Rules

- **Owner.** A bounded list or tuple is snapshot before validation, so later mutation changes nothing. It is
  validated by `compare_account` before anything is retained. Malformed evidence retains no identifier or
  key. The owner cannot be copied or serialized, and its `repr` shows only status.
- **Binding.** It is issued only on the owner's own `EXACT`. It is immutable, carries only the reference,
  and is recognised by exact type and object identity while the owner is open. A copied, deep-copied,
  pickled, forged, foreign-owner or stale (after `close`) handle is refused. A reference string or a
  caller's `AccountMatch` is never a binding.
- **Closed dispatch.** The replay's scenario, clock and transport are read-only. Before preflight and before
  any transport method runs, the runner checks that the transport is the replay's own internally created
  one: exact type, bound to this replay, the same object. A replaced or foreign transport, of any kind, is
  refused as `REPLAY_TRANSPORT_REFUSED` without being called.
- **Requests.** The owned replay transport answers the account and position reads only for its owner's
  binding. Any other handle returns nothing and is counted, so the stream stays incomplete. Neither read
  needs a raw identifier, and there is no raw resolver.
- **Rows.** Each raw row identity is compared with the private expected identity before projection.
  - A match projects to the binding's reference.
  - A valid foreign identity projects to its own keyed reference, never the expected one, so the existing
    `ACCOUNT_ID_MISMATCH`, `ACCOUNT_SUMMARY_MULTIPLE` or `POSITION_ACCOUNT_MISMATCH` applies.
  - A malformed identity, or a closed owner, is `MALFORMED_CALLBACK` and withholds END.

  The accepted producer's handling of duplicates, missing currencies, numeric bounds, errors, sequence
  and END is reused unchanged.
- **Output.** The report carries the reference, `records_version: 2` and the existing findings. No
  identifier, key, balance or quantity appears in the report, a `repr` or an error.

## Scenarios

| Scenario | Outcome |
|---|---|
| `matching_singleton` | `CONTROL_COMPLETE` |
| `managed_absent`, `managed_foreign`, `managed_duplicate` | Refused before dispatch, `UNBOUND` |
| `foreign_summary_rows`, `foreign_position_row` | `NOT_READY`, account mismatch |
| `malformed_row_identity`, `duplicate_rows`, `error_in_stream` | `NOT_READY`, END withheld |
| `missing_end` | `NOT_READY`, END missing |
| `missing_currency`, `oversized_quantity` | `NOT_READY` (the PR #35 regressions) |

## Limits

- **Comparison labels only.** A binding or reference is a label within one run. It is not authentication,
  PAPER evidence, provenance or consent.
- **Caller-owned key.** Key freshness is the caller's responsibility.
- **No hostile-introspection secrecy.** Object checks prevent accidental cross-run pairing. They do not keep
  private state secret from code in the same process.
- **Invented only.** The identities are deliberately not `SYNTH-` shaped, but they are still invented. This
  is not authority to admit any external account, session or transport, and connected use remains a
  separate decision.
