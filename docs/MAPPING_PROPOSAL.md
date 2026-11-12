# From a discovery report to a mapping proposal

`scripts/tb_mapping_proposal.py` turns the JSON catalog written by the read-only contract
discovery (`scripts/ibkr_contract_discovery.py`, schema `tradingbot.ibkr.contract_catalog/1`) into
the material the owner needs to approve a family (see `MULTI_ASSET_MAPPING.md`, "What the owner
approves per family"). It is offline: it reads two files, opens no socket and imports no `ibapi`.
**It approves nothing.** Every entry it writes is `approved = false`, and its policy file refuses
any `approved*` key.

    PYTHONPATH=src python scripts/tb_mapping_proposal.py contract_catalog_<UTC>.json --out DIR \
        [--policy config/mapping_proposal.toml] [--roll-days N] [--price FAMILY=P] \
        [--family FAMILY:TREATMENT:EXECUTION_QUERY[:REFERENCE_QUERY]]

Try it on the invented fixture: `fixtures/contract_discovery_invented/contract_catalog_INVENTED.json`.

## Inputs

* **The report.** Only what it states is used. A field it leaves empty is shown as `absent`. A row
  with status `NOT_FOUND`, `ERROR`, `TIMEOUT` or `SKIPPED`, or a query missing from the report,
  turns the family into an `UNSUPPORTED` entry (it HOLDs) that names the broker's code. No other
  vehicle is ever substituted.
* **The proposal policy** (`config/mapping_proposal.toml`, schema
  `tradingbot.mapping_proposal_policy/1`). For each family it lists its candidate rows (query names
  from `config/contract_discovery.toml`), the proposed execution and reference rows, the treatment
  (`DIRECT`, `BASIS_SHIFT` or `REFERENCE_TRIGGER`), an optional sentence on what the publisher's
  level is believed to mean, and an optional `[family.owner]` table holding owner inputs already
  decided (`sizing`, `session`, `roll`, `contract_selection`, `basis_shift`, `reference_trigger`,
  `same_series_reason`, `aliases`). The shipped file holds the lead's first choices, all
  unapproved. `--family` replaces a family's vehicle and treatment from the command line and drops
  owner inputs decided for a different vehicle or treatment.
* **`--roll-days N`** (or `[roll_rule] business_days_before_last_trade`, with optional
  `holidays`): the owner's roll-rule parameter. Without it no roll date is proposed.
* **`--price FAMILY=P`**: an illustrative price the owner supplies for the 1% risk line. No market
  price is ever assumed.

## Outputs (in DIR)

| File | Content |
|---|---|
| `contract_table.md` | per family, every candidate row: contract id, security type, exchange, currency, multiplier, minimum tick, size increment, trading class, contract month, last-trade date and time, time zone, trading and liquid hours, or a NOT_FOUND row with the broker's code. A truncated row and a report whose `overall` is not `COMPLETE` are flagged |
| `roll_calendar.md` | per futures execution vehicle and listed month: the last-trade date (and the report field it came from) and the proposed `roll_date` |
| `proposed_mapping.toml` | one `[[mapping]]` entry per family in the form `config/instrument_mapping.toml` accepts, `approved = false` |
| `approval_<FAMILY>.md` | one page per family: what a publisher level means under the treatment, what the bot would trade, USD per price point and per tick, USD for a 1% move at the owner's price, the broker facts used, and the numbered decisions the owner must make |

**Roll rule.** `roll_date` = the report's last-trade date minus N business days (Monday to Friday,
less the listed holidays). The contract month is the report's `contract_month` and is never
derived from the last-trade date (an energy contract's last trade falls in the month before it).
The report carries neither first-notice dates, the broker's close-out dates, nor whether a
contract settles physically. Every month is therefore flagged "first notice date not in report:
owner/broker confirmation needed". For a physically delivered energy or metals contract the
first-notice day or the broker's close-out can come before the proposed roll date, so the owner
must confirm each date before using it.

**Entries that cannot load yet.** An entry still waiting for an owner input carries
`OWNER_INPUT_REQUIRED` values and an `owner_input_required` list. An entry missing a contract
rule the report does not state (for example a multiplier) carries `BROKER_FACT_REQUIRED` and a
`broker_fact_missing` list. The mapping loader rejects both keys as unknown, so such an entry
cannot be loaded until the owner has filled it in and deleted them. The tests show that a
completed proposal loads through `MappingPolicy.from_toml` with every family `NOT APPROVED`, and
that an OPEN on each family HOLDs with `MAPPING_NOT_APPROVED`, both at the mapping stage and in
the gateway (`tests/test_mapping_proposal.py`). A stock quoted in its own price with no sizing
(TLT) needs no owner input and keeps the version-1 behaviour.

**Same-series proposals.** For a DIRECT `same_series` proposal (a micro future executing the
standard contract's levels) the sheet compares the two vehicles' last-trade dates month by month
and lists every difference (in the real catalog every micro oil and micro gas month stops trading
one business day before the full-size contract). The report shows listing facts only, never that
the two are priced as one series: that is an assertion the owner must make (`same_series_reason`).
Roll dates are always proposed from the execution vehicle's own last-trade dates.

**Warnings and refusals.** A report that is not `COMPLETE`, and a truncated execution or reference
row (the query's `limit` was reached; for the execution row the month list may be incomplete), are
warned on the sheet, in the roll calendar and in the entry's note. A proposed roll date on or before
the report date is UNUSABLE: the month is left out of `contracts` and named in the note. A roll
date closer to the report date than the owner's `min_business_days_to_roll` (when stated) is
flagged. Without a holiday list the business-day counts are Monday to Friday and every output says
"exchange holidays not applied". Dollar values need a positive, finite multiplier and tick and a
`price_magnifier` of 1; otherwise none are computed and the entry records a missing broker fact.
A share's multiplier of 1 is the loader's default, not a report fact; a fractional size increment
is an owner decision (the loader needs whole units). Duplicate query names in a report and
duplicate months in a row are refused, never first-wins.

**Output safety.** Policy keys must be bare TOML keys (`[A-Za-z0-9_-]+`) at every level and family
names must match the loader's pattern; every string is escaped. After rendering, the tool re-parses
`proposed_mapping.toml` and refuses (exit 3, nothing written) unless it holds exactly the proposed
families, each `approved = false` with no `approved_by`, `approved_at`, `entry_rule`, `entry_anchor`
or `level_contract_rule`. The output directory must be new or empty (exit 2 otherwise).

The proposal never writes `entry_rule`, `entry_anchor` or `level_contract_rule`. Those defaults
(`EXPLICIT_ENTRY_ONLY`, `EXPLICIT_MONTH_ONLY`) stay until the owner decides otherwise.
