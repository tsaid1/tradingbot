# Instrument-mapping diagnostics (S2-MAP-01, offline, NON-AUTHORITATIVE)

`scripts/inspect_instrument_mapping.py` and `src/tradingbot/instrument_diagnostics.py` inspect
explicit **synthetic** mapping records and report what is missing, ambiguous, mismatched, invalid or
unsupported about the relationship between a quoted source price series (an index level, a spot
commodity level, a continuous futures series, a listed security quote) and the instrument the record
says it maps to. Issue: https://github.com/tsaid1/tradingbot/issues/10.

The report is a diagnostic for a human reviewer. It is not a gateway, not a policy, not a catalog and
not a mapping decision. Every value it inspects is whatever the record author typed.

## What it does not do

- No broker, network, catalog, credential or market-data access of any kind.
- No price conversion, FX conversion, unit conversion, basis or carry adjustment.
- No roll choice, no source-to-contract selection, no alias resolution between labels.
- No sizing, notional, order construction, execution eligibility or authorization.
- No change to `validator.py`, `models.py`, `event_models.py` or any versioned contract. It reads
  `AssetClass`, `Instrument`, `PriceBasis` and the gateway's symbol pattern; it writes nothing back.
- No adoption of a `ResolvedInstrument` or any resolution/quote/lifecycle contract; those remain gated
  on the architecture decision recorded in the broker plan review.
- No wall-clock: expiry is compared only to a record's own `as_of` date.

Output vocabulary is deliberately free of APPROVE / EXECUTE / ELIGIBLE. A record whose only finding is
the INFO control `DIRECT_IDENTITY_CONTROL` is a positive *informational* control: it says the source
label equals the instrument label and the level is stated as the instrument's own price. It is not
trading permission. The gateway still applies every provenance, freshness, quote, policy and
asset-class rule, and options and futures stay `ASSET_CLASS_PAPER_ADAPTER_UNSUPPORTED` whatever their
metadata says.

## Running it

```bash
# bundled synthetic examples, full text / one line per record / machine-readable JSON
python scripts/inspect_instrument_mapping.py --examples
python scripts/inspect_instrument_mapping.py --examples --summary
python scripts/inspect_instrument_mapping.py --examples --json

# your own synthetic record file (JSON list, or {"records": [...]}); optionally write to a file
python scripts/inspect_instrument_mapping.py path/to/records.json --json --out report.json
```

Exit status 0 means a report was produced, whatever it contains. Exit status 2 means the input could
not be read or validated (for example a record without `"synthetic": true`). The exit status is never a
verdict on the mapping.

From Python: `load_records(path)`, `diagnose_record(record)`, `diagnose_records(records)`,
`render_json(report)`, `render_text(report, summary=False)`.

## Input record

Records are strict (unknown fields are rejected). Every record must state `"synthetic": true`.

| Field | Meaning |
|---|---|
| `record_id` | `[A-Za-z0-9_-]{1,64}` |
| `synthetic` | must be `true`; unmarked input is refused |
| `description` | free text |
| `as_of` | ISO date used only to detect an already-expired contract; optional |
| `source.symbol` | the publisher-side label, e.g. `SPX`, `GOLD_SPOT`, `$TLT` (a leading cashtag is ignored for comparison; nothing else is normalised) |
| `source.kind` | `TRADED_SECURITY`, `DATED_CONTRACT`, `INDEX`, `SPOT`, `CONTINUOUS_FUTURES`, `CFD`, `UNKNOWN` |
| `source.currency`, `source.unit`, `source.quoted_level` | currency code, free unit label (e.g. `INDEX_POINT`, `USD_PER_BARREL`), decimal string |
| `instrument` | the existing `models.Instrument` (asset class, symbol, underlying, expiry, strike, option_right, exchange, futures_contract) |
| `price_basis` | the existing `event_models.PriceBasis`: `TRADED_CONTRACT`, `SOURCE_SERIES`, `UNKNOWN` |
| `contract` | record-supplied economics: `currency`, `unit`, `multiplier`, `tick_size`, `tick_value`, `settlement` (CASH/PHYSICAL), `settlement_time` (AM/PM), `exercise_style` (AMERICAN/EUROPEAN), `contract_month` (YYYY-MM) |

## Identity relations

| `identity` | Meaning |
|---|---|
| `DIRECT` | source label equals the instrument label |
| `DERIVATIVE_OF_SOURCE` | the instrument is an option/future and the source label equals its *stated* underlying |
| `DIFFERENT` | labels differ; the diagnostic does not resolve aliases (`WTI` vs `WTI_CONT` stays unverified) |
| `UNDETERMINED` | the instrument or the source label is missing |

## Finding kinds and headline

Each finding has a stable `code`, a `kind`, an optional dotted `field` and a `detail` sentence.
A record's `headline` is its most pressing kind in this order:
`INVALID` > `MISMATCH` > `MISSING` > `AMBIGUOUS` > `UNSUPPORTED` > `INFO`.
`runtime_scope` is `EQUITY_ETF_GATEWAY_CHECKS_STILL_APPLY`, `ASSET_CLASS_UNSUPPORTED_IN_CURRENT_RUNTIME`
or `UNDETERMINED` and is derived from the asset class alone.

### Codes

Identity and basis

| Code | Kind | Fires when |
|---|---|---|
| `INSTRUMENT_MISSING` / `SOURCE_SERIES_MISSING` | MISSING | a side of the comparison is absent |
| `PRICE_BASIS_UNKNOWN` | AMBIGUOUS | `price_basis` is `UNKNOWN`; no basis is assumed |
| `SOURCE_SERIES_KIND_UNKNOWN` | AMBIGUOUS | `source.kind` is `UNKNOWN` |
| `BLANK_FIELDS_TREATED_AS_ABSENT` | INFO | whitespace-only optional strings were present; they are reported as not stated (see below) |
| `INSTRUMENT_SYMBOL_BLANK` | MISSING | the instrument symbol is whitespace |
| `DIRECT_IDENTITY_CONTROL` | INFO | same label, orderable source kind matching the asset class, `TRADED_CONTRACT` basis |
| `SOURCE_IS_STATED_UNDERLYING` | INFO | the source label equals the derivative's stated underlying |
| `SOURCE_KIND_NOT_ORDERABLE` | MISMATCH | same label but the source is an index/spot/continuous/CFD series |
| `SOURCE_KIND_ASSET_CLASS_CONFLICT` | MISMATCH | same label but e.g. a `DATED_CONTRACT` quote for an `EQUITY` |
| `SOURCE_KIND_BASIS_CONFLICT` | MISMATCH | the source is the instrument's own quote yet the basis says `SOURCE_SERIES` |
| `PRICE_BASIS_CLAIM_CONFLICT` | MISMATCH | `TRADED_CONTRACT` is claimed for a non-orderable series or a different label |
| `PRICE_BASIS_NOT_TRADED_CONTRACT` | MISMATCH | basis is `SOURCE_SERIES`; the gateway holds this as `SOURCE_TO_BROKER_PRICE_BASIS_UNRESOLVED` |
| `SOURCE_SERIES_PROXY_MISMATCH` | MISMATCH | an index/spot/continuous/CFD/unknown series quoted against an equity or ETF |
| `SOURCE_INSTRUMENT_SYMBOL_MISMATCH` | MISMATCH | a different listed security is quoted |
| `SOURCE_UNDERLYING_LABEL_MISMATCH` | AMBIGUOUS | derivative whose stated underlying label differs from the source label |
| `SOURCE_UNDERLYING_UNCOMPARABLE` | AMBIGUOUS | derivative with no stated underlying and a different source label |
| `CONTINUOUS_SERIES_ROLL_UNRESOLVED` | AMBIGUOUS | a continuous futures series against a dated future; no roll is chosen |

Currency, unit and economics

| Code | Kind | Fires when |
|---|---|---|
| `SOURCE_CURRENCY_MISSING`, `SOURCE_UNIT_MISSING`, `TRADED_CURRENCY_MISSING`, `TRADED_UNIT_MISSING`, `CONTRACT_SPEC_MISSING` | MISSING | the named field or the whole contract block is absent |
| `CURRENCY_CODE_INVALID` | INVALID | not three upper-case letters |
| `CURRENCY_MISMATCH` / `UNIT_MISMATCH` | MISMATCH | both sides stated and different; nothing is converted |
| `SOURCE_LEVEL_INVALID`, `MULTIPLIER_INVALID`, `TICK_SIZE_INVALID`, `TICK_VALUE_INVALID` | INVALID | not a positive finite decimal, or outside the supported numeric range (see below) |
| `MULTIPLIER_MISSING` (derivatives), `TICK_SIZE_MISSING` (futures) | MISSING | required economics absent |
| `TICK_VALUE_INCONSISTENT` | MISMATCH | stated tick value is not multiplier x tick size |
| `QUOTED_LEVEL_OFF_TICK_GRID` | AMBIGUOUS | a level claimed as the contract's own price is not a multiple of the stated tick |

Contract specificity

| Code | Kind | Fires when |
|---|---|---|
| `SYMBOL_FORMAT_INVALID` | INVALID | equity/ETF symbol fails the gateway pattern (the gateway blocks it as `INVALID_SYMBOL`) |
| `SYMBOL_SUFFIX_CONVENTION_AMBIGUOUS` | AMBIGUOUS | share-class separator whose spelling varies by venue/vendor |
| `LISTING_VENUE_UNSPECIFIED` | AMBIGUOUS | equity/ETF/option without an exchange |
| `DERIVATIVE_FIELDS_ON_CASH_INSTRUMENT` / `OPTION_FIELDS_ON_FUTURE` | MISMATCH | identifiers contradict the asset class |
| `UNDERLYING_MISSING` | MISSING | derivative without an underlying |
| `OPTION_EXPIRY_MISSING`, `OPTION_STRIKE_MISSING`, `OPTION_RIGHT_MISSING`, `OPTION_EXERCISE_STYLE_MISSING`, `OPTION_SETTLEMENT_MISSING` | MISSING | single-leg option identifiers absent |
| `OPTION_STRIKE_INVALID`, `EXPIRY_FORMAT_INVALID`, `AS_OF_DATE_INVALID` | INVALID | malformed values; a strike is also subject to the numeric range |
| `OPTION_SETTLEMENT_TIME_UNSPECIFIED` | AMBIGUOUS | cash-settled option without AM/PM; those are distinct series |
| `CONTRACT_EXPIRED_AT_AS_OF` | INVALID | expiry precedes the record's `as_of` |
| `FUTURES_VENUE_MISSING`, `FUTURES_EXPIRY_MISSING`, `FUTURES_CONTRACT_CODE_MISSING`, `FUTURES_CONTRACT_MONTH_MISSING`, `FUTURES_SETTLEMENT_MISSING` | MISSING | dated-contract identifiers absent; a product root alone is not a contract |
| `FUTURES_CONTRACT_CODE_FORMAT_UNRECOGNIZED` | AMBIGUOUS | code is not root + month letter + 1-2 digit year; no venue format is assumed |
| `FUTURES_YEAR_DIGIT_AMBIGUOUS` | AMBIGUOUS | one-digit year, which repeats every decade |
| `FUTURES_CONTRACT_MONTH_FORMAT_INVALID` | INVALID | `contract_month` is not `YYYY-MM` |
| `FUTURES_CONTRACT_MONTH_CONFLICT` | MISMATCH | the code's month letter/year disagree with `contract_month` |

Runtime

| Code | Kind | Fires when |
|---|---|---|
| `RUNTIME_ASSET_CLASS_UNSUPPORTED` | UNSUPPORTED | the instrument is an option or future; the gateway holds it as `ASSET_CLASS_PAPER_ADAPTER_UNSUPPORTED` however complete the metadata |

## Numeric bounds and blank values

**Numbers.** `quoted_level`, `multiplier`, `tick_size`, `tick_value` and `strike` must be positive finite
decimals with at most 28 significant digits and a magnitude from 1e-12 up to but excluding 1e16. Anything
else is reported as INVALID for its own field *before* any arithmetic, so one extreme record (for example a
level of `1e30` against a `0.01` tick, or a `1e999999` multiplier) becomes a finding and never an
exception that would abort the batch. The only arithmetic the module performs (multiplier x tick for
`TICK_VALUE_INCONSISTENT`, level mod tick for `QUOTED_LEVEL_OFF_TICK_GRID`) runs inside an explicit
64-digit `decimal` local context in which every in-range operation is exact; the host process's ambient
decimal context is never changed. No price is computed, converted or sized.

**Blank strings.** A whitespace-only optional string (unit, currency, venue, underlying, expiry, strike,
contract code, contract month, level, as_of) is treated as *not stated* within the diagnostic: the
corresponding MISSING / AMBIGUOUS finding fires exactly as if the field were absent, and an INFO
`BLANK_FIELDS_TREATED_AS_ABSENT` lists the fields concerned. A whitespace-only instrument symbol is
`INSTRUMENT_SYMBOL_BLANK`. The shared `Instrument` contract is not changed and still accepts such values;
only this tool's reading of them is defined.

## Relation to the gateway

The diagnostic mirrors, and never replaces, the identity rules the gateway already enforces:
`PriceBasis` other than `TRADED_CONTRACT` holds as `SOURCE_TO_BROKER_PRICE_BASIS_UNRESOLVED`, a source
symbol that is not the instrument holds as `SOURCE_TO_BROKER_MAPPING_UNAPPROVED`, and options/futures
hold as `ASSET_CLASS_PAPER_ADAPTER_UNSUPPORTED`. `tests/test_instrument_diagnostics.py` runs the gateway
before and after diagnosing the same records and asserts identical decisions; the direct equity control
is an INFO finding here while only the gateway produces its own decision from its own inputs.

## Determinism and the golden report

Records are reported in input order, findings in rule order, JSON keys sorted; no timestamps, paths or
environment values appear. `fixtures/instrument_mapping_synthetic/expected_report.json` is the committed
report for `records.json` and the suite asserts byte equality, in-process and through the CLI. Current
`RULES_VERSION` is 2 (1 -> 2 added the numeric bounds and blank-string handling after review). After a
deliberate rule change, bump `RULES_VERSION`, regenerate with

```bash
python scripts/inspect_instrument_mapping.py --examples --json --out fixtures/instrument_mapping_synthetic/expected_report.json
```

and review the diff as part of the change.

## Synthetic inputs only

Every symbol, venue, multiplier, tick, expiry, level and currency in the fixtures and tests is invented.
`SPX`, `GOLD_SPOT`, `WTI_SPOT`, `WTI_CONT` and `HENRY_HUB` are used as *source-series labels* so that the
index, gold, crude and natural-gas examples the issue asks for are recognisable; the contracts they are
paired with (`IDXFZ26`, `AUFG27`, `CRDG27`, `NGSH27`, `SPX-SYN-...`) and their economics are fictional.
Nothing here asserts a publisher mapping, a real contract specification or a research snapshot, and
no runtime market specification is hard-coded: the module carries no catalog, only rules.

## Remaining decisions (not implementation gaps)

The diagnostic is complete for its scope: the acceptance boundaries in issue #10 are covered by the
fixture and the regression suite. The following remain open decisions for the product owner and are
*not* resolved, inferred or defaulted by this code:

- **Broker and account.** Which broker, account type, residency/eligibility, product approvals,
  market-data entitlements and API access apply. The diagnostic is broker-neutral and names none.
- **Source-to-instrument mapping policy.** Whether any index/spot/continuous series may ever stand in
  for a listed proxy or a dated contract, who approves each mapping, and how basis, roll and currency
  are handled. Today every such record is a `MISMATCH`/`AMBIGUOUS` and the gateway holds it.
- **Derivative contracts.** `ResolvedInstrument`, quote and lifecycle contracts, Decimal economics,
  session/expiry rules and versioning remain behind the architecture gate in the broker plan review;
  complete option/futures metadata here still reports `RUNTIME_ASSET_CLASS_UNSUPPORTED`.
- **Data rights and protection.** Real quote sources, their age budget, protective-order semantics,
  partial-fill behaviour and recovery are paper-adapter questions outside this offline tool.

When one of these is decided, the record schema or rule set here may be extended under a new
`RULES_VERSION`; the extension still must not select, convert, size or authorize.
