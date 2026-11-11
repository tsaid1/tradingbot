# Paper Connection view (saved report, read-only, NON_AUTHORITATIVE)

`dashboard/pages/7_Paper_Connection.py` shows **one saved result** of the read-only paper diagnostic
(the official-SDK diagnostic owned separately; see its own documentation) as a dashboard page. It is a
viewer only: it connects to nothing, imports no SDK, starts nothing and writes nothing. The existing
offline Paper Readiness page (`4_Paper_Readiness.py`) is unchanged.

## Run it

```bash
PYTHONPATH=src streamlit run dashboard/app.py                         # then choose "Paper Connection"
PYTHONPATH=src streamlit run dashboard/pages/7_Paper_Connection.py    # the page on its own
PYTHONPATH=src python -m pytest tests/test_paper_connection_page.py   # the page tests
```

## What it shows

By default the page shows **NO REPORT**. Its only input is one upload of a saved JSON file, held in memory
for the session and never saved.

The upload is checked before anything is shown:
- **Size:** at most 64 KiB, checked against the declared size before reading and again after a bounded read.
  Larger files are refused before parsing, and no digest is computed for them. The uploader's own
  transport limit is 1 MB, the smallest Streamlit allows; that is not the acceptance cap.
- **Parsing:** strict UTF-8 JSON, with no duplicate keys or `NaN`/`Infinity`, at most 16 levels deep and
  20 000 values, and a top-level object.
- **Closed shape:** every top-level field must belong to the diagnostic's accepted result shape (head
  `f018e00`). Rendered fields must have their exact type and a value from a closed vocabulary:
  - stops and phases
  - run-failure codes, with only a builtin exception class name or `NON_BUILTIN_EXCEPTION` as suffix
  - cleanup codes
  - stream issue codes from the accepted normalizers
  - the managed-account statuses
  - market data types
  - the fixed last-trade source text
  - the reader partial-bytes values
  - the 15 named checks
- **Required:** `authority` is `NON_AUTHORITATIVE`, `trading_permission` is `false`, `plan_sha256` is 64 hex
  characters and `verdict` is `PASS` or `FAIL`.
- **Narrowing only:** a `PASS` is displayed only if the same document has stop `COMPLETE`, no run failure
  and all 15 checks true. Otherwise the file is refused as `INCONSISTENT_PASS`. A `FAIL` is never upgraded,
  and nothing is recalculated.

A file that fails any check is **refused whole** with one fixed code, such as `UNRECOGNIZED_FIELD`,
`UNRECOGNIZED_FAILURE_CODE` or `QUOTE_TIMING_INVALID`. The page shows nothing from that file except its
SHA-256 digest; field names, values and parser messages are never echoed.

For an accepted file the page shows only these fixed fields:

| Section | Fields |
|---|---|
| Reported result | verdict, stop and phase, exactly as reported; the input SHA-256 and size, `plan_sha256` and `run_id` |
| Identity match | arrival, registration, invalidation, invalidating events, closed, and whether rows match the expected keyed reference. Never a raw account or key |
| TLT and read streams | for contract details, quote, account summary and positions: registered, completed, issue codes, issue overflow; the reported TLT conId and delivered market data types |
| Quote timing | last-trade timestamp, age, max age and within-max-age; local receipt time; **bid/ask timestamp freshness UNKNOWN** |
| Checks, failures, cleanup | the 15 reported checks, run-failure codes, cleanup codes |
| Stated unknowns | reader partial bytes, local lifecycle completion, reader alive after join, broker-side retirement **NOT ESTABLISHED**, `trading_permission` false |

A value the report lacks is shown as **UNKNOWN**, never as zero or as passing.

The diagnostic also writes sections that can carry payloads:
- `envelopes`, with account-summary values and keyed row references
- `findings` detail text
- `router_report`, `requests_sent`, `callbacks` and `marks_s`
- `server_version`, `endpoint`, `limits` and `operator_attestation`

The page recognises these sections and **never displays them**; it lists only their names.

## Meaning

- A reported `PASS` is evidence somebody reported. It is **not authentication and not live verification**.
- The upload's origin is **not independently verified**. Compare the shown SHA-256 digest with the digest of
  the reviewed result.
- Nothing here is paper readiness, trading authority, protection or broker-side retirement. Every report
  stays `NON_AUTHORITATIVE` with `trading_permission` false.

## Limits

- **Tested only on invented reports** in the accepted shape. No real diagnostic result has been produced
  or viewed. The tests' "PASS" document is invented to show how a reported PASS is labelled.
- The closed vocabulary follows the diagnostic's accepted output at head `f018e00`. A later shape change
  will be refused (`UNRECOGNIZED_FIELD` or a field-specific code) until this page is reviewed and updated.
- A real result larger than 64 KiB is refused, for example one with many position rows. Raising the cap
  needs review.
- The page cannot tell an edited or fabricated file from a genuine one; only the digest comparison can.
- Nothing is persisted; reloading the page clears the upload.
