# Paper readiness scaffold (S2-PAPER-01, offline, NON-AUTHORITATIVE)

`src/tradingbot/paper_readiness/`, `scripts/inspect_paper_readiness.py` and `tests/test_paper_readiness.py`
implement the approved **first step** of conditional IBKR Pro / IB Gateway paper readiness: an offline,
read-only capability scaffold behind a deterministic fake transport. Issue:
https://github.com/tsaid1/tradingbot/issues/16. Owner decision: Scope R, conditionally approved on
6 November 2026 ("fake transport first, then read-only paper capability checks only after my eligibility,
entitlements and locally configured paper environment are verified").

**This is not a broker connection and not a trading adapter.** No socket, HTTP, broker SDK/client,
credential loader, live endpoint or market-data request exists in this package. Every connected check
remains **BLOCKED**: none of the non-secret owner facts (residence, account type, paper/API availability,
data entitlements, reauthentication acceptability) has been provided. The scaffold refuses any transport
that is not its own fake, so it cannot be pointed at a gateway by configuration.

A `CONTROL_COMPLETE` outcome means one thing only: a *synthetic* observation set was complete and
consistent. It is not a claim that IBKR, IB Gateway, a paper account or any protection mechanism is
reliable, reachable or safe. Nothing here is trading permission; the deterministic gateway
(`validator.py`) is unchanged and still decides every action.

## What it does not do

- No order, cancel, replace, reset, settings, subscription or credential method exists on the transport
  surface. `transport.FORBIDDEN_NAME_RE` encodes that vocabulary and the tests assert it never appears.
- No socket, HTTP, SDK or environment access: the package imports only pure standard-library modules,
  pydantic and this project (asserted by AST in the tests), and a subprocess test asserts that importing
  it loads no network or broker module.
- No wall clock: `as_of` and every freshness or session judgement come from a `FakeClock`.
- No inference of PAPER from a port, an account-id prefix, a server version or anything else. The
  environment, the account and the Read-Only API setting must be stated explicitly, or the run is refused.
- No contract selection, proxy, alias, derivative, price conversion, sizing, policy change or
  authorization. `TargetContract` cannot even express a non-USD or non-STK instrument.
- No change to `models.py`, `validator.py`, `event_models.py`, `storage.py`, lifecycle dispatch or
  credentials. Readiness records are versioned locally (`RECORDS_VERSION = 1`) and are not adopted as
  trading contracts; the tests assert they do not exist in `tradingbot.models`.
- No real account identifier anywhere. Only `SYNTH-...` identifiers are admitted by schema, and a managed
  account without that marker is refused unseen.

## Running it

```bash
# From the repository root, with imports pinned to this checkout:
PYTHONPATH=src python scripts/inspect_paper_readiness.py --list
PYTHONPATH=src python scripts/inspect_paper_readiness.py --scenario tlt_regular_session_control
PYTHONPATH=src python scripts/inspect_paper_readiness.py --all --summary
PYTHONPATH=src python scripts/inspect_paper_readiness.py --all --json --out /tmp/readiness.json
PYTHONPATH=src python -m pytest tests/test_paper_readiness.py
```

The CLI prints the resolved `tradingbot` import origin on stderr so you can confirm it runs this checkout
and not another editable install. Exit status 0 means reports were produced, whatever they contain; 2 means
an unknown scenario name. The exit status is never a verdict.

From Python: build a `ReadinessExpectation` (synthetic account id, `TargetContract`), a `FakeScript`, a
`FakeClock` and a `FakeReadOnlyTransport`, then call
`run_readiness_check(transport, expectation, clock=clock, policy=None, label=None)` and render with
`render_text` / `render_json`. `scenarios.SCENARIOS` holds the bundled scripts.

## Read-only surface

| Method | Mirrors (documented TWS API) | Returns |
|---|---|---|
| `attest_session()` | connection completion, `managedAccounts`, session `error` codes, plus operator-attested settings | `SessionAttestation` |
| `request_contract_details(request_id, target)` | `reqContractDetails` -> `contractDetails`* -> `contractDetailsEnd` | correlated `ResponseEnvelope`s |
| `request_quote_snapshot(request_id, con_id)` | `reqMktData(snapshot=true)` -> `marketDataType`, aggregate ticks -> `tickSnapshotEnd` | correlated envelopes |
| `request_account_summary(request_id, account_id)` | `reqAccountSummary` -> `accountSummary`* -> `accountSummaryEnd` | correlated envelopes |
| `request_positions(request_id, account_id)` | `reqPositions` -> `position`* -> `positionEnd` | correlated envelopes |

`transport_kind` must be `"FAKE"`; the runner refuses anything else before attesting. Request ids are fixed
(`1001`..`1004`) so scripted and, later, logged responses correlate by construction.

## Order of operations

1. `TRANSPORT_NOT_FAKE` refusal if the transport kind is not FAKE (nothing attested, nothing dispatched).
2. `attest_session()`; then `preflight()` collects **every** applicable refusal: not connected, connectivity
   lost (1100 without 1101/1102), environment UNKNOWN / LIVE / mismatched, no managed account, account not
   in the managed list, managed account without the synthetic marker, more than one managed account,
   Read-Only API not attested. A refusal dispatches no read (`dispatched == []`, proven by the fake's log).
   A socket port, if attested, only produces the INFO note `SOCKET_PORT_NOT_EVIDENCE`.
3. Session capability findings (server version, connection time, data-farm status, other session codes).
4. Contract details. Only a **unique** row that matches symbol, secType, currency, routing exchange and
   primary listing with no missing identity field, whose stream **completed** with `contractDetailsEnd`,
   carried **no broker error** and produced **no non-INFO correlation finding** (stray id, conflicting
   duplicate, row after END, foreign kind, truncation) yields a conId; otherwise the quote is skipped
   (`QUOTE_SKIPPED_NO_CONTRACT_IDENTITY`). Exact duplicates and reordering are INFO and do not withhold it.
5. One quote snapshot for that conId; account summary; positions.
6. Correlation of every stream: stray request ids ignored (AMBIGUOUS), truncation at 64 (INCOMPLETE),
   reordering by sequence (INFO), exact duplicates dropped (INFO), conflicting duplicates (MISMATCH),
   anything after the first END ignored (AMBIGUOUS), foreign kinds dropped (AMBIGUOUS).
7. Outcome: `REFUSED_BEFORE_DISPATCH`, `NOT_READY` (any non-INFO finding) or `CONTROL_COMPLETE`. The
   headline is the most pressing kind in the order REFUSED > ERROR > INCOMPLETE > MISMATCH > AMBIGUOUS >
   UNENTITLED > DELAYED > STALE > OUT_OF_SCOPE > MISSING > INFO.

Every report carries `authority: NON_AUTHORITATIVE`, `trading_permission: false`, a redacted
`account_ref` (prefix plus 8 hex digits of a SHA-256), per-stage observation digests and the findings.
Details name codes, field names, counts and documented message texts; the tests assert that no account id,
balance, price or raw payload ever appears in a rendered report. Account-summary tag names are rendered only
when they belong to the documented allowlist (`DOCUMENTED_ACCOUNT_TAGS`); any other key is counted and
withheld, so an untrusted or sensitive key in an observation can never reach a report.

## Documented versus synthetic proof versus still unknown

Access note (6 November 2026): the current official TWS API documentation lives under
https://www.interactivebrokers.com/campus/ibkr-api-page/twsapi-doc/ and every `interactivebrokers.com`
page below returned **HTTP 403 to automated fetching**; `https://ibkrcampus.com/ibkr-api-page/trader-workstation-api/`
redirected (301) to the same protected domain. Verbatim quotes therefore come from the
`interactivebrokers.github.io/tws-api` reference, which states on every page "This documentation is now
deprecated. Please switch to the IBKR Campus for up-to-date information", and from two `ibkrguides.com`
pages that did load. Anything marked *(403, not re-verified)* is cited by URL only. A connected check
must re-verify every row against the current documentation before relying on it.

| Capability | Documented (source) | Synthetic proof in this scaffold | Still unknown until a connected check |
|---|---|---|---|
| Connection and client limit | `eConnect(host, port, clientId)`; "each TWS session can receive up to 32 different client applications simultaneously"; `nextValidId` "is commonly used to indicate that the connection is completed"; error 502 when the API is not enabled. https://interactivebrokers.github.io/tws-api/connection.html | `SessionAttestation.connected`, `server_version`, `connection_time`; refusal when not connected | actual serverVersion, handshake timing, whether the owner's gateway accepts the client id |
| Ports are configurable conventions | "a production account TWS session will be set for socket port 7496", "a paper account session will listen on socket port 7497", "They can be changed to any open socket port". https://interactivebrokers.github.io/tws-api/initial_setup.html (IB Gateway 4001/4002 are not stated on that page; they appear only as convention in docs/research/BROKER_API_COMPARISON.md) | `environment_unknown_port_7497`: port 7497 with no explicit claim is **refused**; the port only yields `SOCKET_PORT_NOT_EVIDENCE` | the owner's actual port configuration |
| Read-Only API setting | "By default, "Read Only" is enabled as an additional precautionary measure." "Information about orders is not available to the API when read-only mode is enabled." (same page) | `read_only_api_attested` must be `True` or preflight refuses | the setting is a TWS/Gateway configuration, not queryable through the API: the operator must attest it and it must be verified on the actual gateway |
| Managed accounts | "the TWS will automatically send a list of managed accounts once the connection is established"; comma-separated list via `managedAccounts`; `reqManagedAccts`. https://interactivebrokers.github.io/tws-api/managed_accounts.html | refusals for empty list, expected account absent, more than one account, unmarked identifier | real account-id format (the page does not describe it; the scaffold infers nothing from prefixes) |
| Contract details and ambiguity | `reqContractDetails` "takes as an argument a Contract object which may uniquely match one contract" and "can also take a Contract object which matches multiple contracts in IB's database. When there are multiple matches, they will each be returned individually"; `contractDetailsEnd` "marker will be triggered". https://interactivebrokers.github.io/tws-api/contract_details.html | unique row control; `contract_identity_ambiguous` (two rows -> no conId, no quote); `contract_not_found` (code 200); `contract_currency_mismatch`; END missing -> INCOMPLETE | TLT's actual conId, primary listing, local symbol and whether a SMART description resolves uniquely without `primaryExchange` |
| Liquid (regular) hours and time zone | `liquidHours`: "the liquid hours (regular trading hours) of the contract on the specified exchange"; formats "20090507:0700-1830,1830-2330;20090508:CLOSED" (TWS <= 969) and "20180323:0930-20180323:1600;20180326:0930-20180326:1600" (970+); `timeZoneId`: "The time zone for the trading hours of the product. For example, EST." https://interactivebrokers.github.io/tws-api/classIBApi_1_1ContractDetails.html | both formats parsed; unreadable or overnight-ambiguous ranges fail closed; unresolved zone fails closed; `outside_regular_session`; fake-clock boundary tests (09:30 inclusive, 16:00 exclusive) | the actual `timeZoneId` spelling IBKR sends (an abbreviation such as "EST" has no DST; the scaffold accepts only zones the standard library resolves) and the real holiday calendar |
| Quote snapshot | snapshot=true: "receive the currently available market data once before a tickSnapshotEnd event is sent 11 seconds later"; data "is not tick-by-tick but consists of aggregate snapshots taken several times per second". https://interactivebrokers.github.io/tws-api/md_request.html | one aggregate `QuoteObservation` plus `TICK_SNAPSHOT_END`; `QUOTE_OBSERVATIONS_MULTIPLE`; END missing -> INCOMPLETE | whether `tickSnapshotEnd` arrives after a 354 refusal; real tick cadence |
| Tick types | 0 bid size, 1 bid, 2 ask, 3 ask size, 4 last, 5 last size, 45 "Time of the last trade (in UNIX time)", 49 halted (-1 unavailable, 0 not halted "only returned for contracts in a TWS watchlist", 1 general halt, 2 volatility halt), 66/67/68/88 delayed bid/ask/last/last timestamp. https://interactivebrokers.github.io/tws-api/tick_types.html | bid/ask/size completeness, integrity (ask >= bid > 0), `QUOTE_UNTIMED`, delayed ticks -> `QUOTE_DELAYED`, halted 1/2 -> `INSTRUMENT_HALTED`, -1/None -> INFO `HALT_STATUS_UNAVAILABLE` | whether tick 45 and 49 are actually delivered for the owner's entitlement; tick 49 availability outside a watchlist |
| Market data type | 1 live; 2 frozen "the last data recorded at market close"; 3 delayed "15 - 20 minutes delayed"; 4 delayed frozen; `marketDataType` callback; "If live data is available a request for delayed data would be ignored by TWS." https://interactivebrokers.github.io/tws-api/market_data_type.html ; delayed page: "If live data is available, it will always be returned instead of delayed data." https://interactivebrokers.github.io/tws-api/delayed_data.html | LIVE control; DELAYED / DELAYED_FROZEN -> `QUOTE_DELAYED`; FROZEN -> `QUOTE_FROZEN` (STALE); UNKNOWN -> AMBIGUOUS; changing or inconsistent type -> AMBIGUOUS | which type the owner's paper username actually receives for TLT |
| Entitlement | "Market data subscriptions are done at a TWS user name level, not per account."; "By default, every user has a maxTicker Limit of 100 market data lines"; paper sharing "will take up to 24 hours until the market data sharing takes effect" and requires that "You are NOT logged in with your live user name at the same time on a different computer." https://interactivebrokers.github.io/tws-api/market_data.html ; codes 354 "Not subscribed to requested market data.", 10090 "Part of requested market data is not subscribed.", 10197 "No market data during competing session". https://interactivebrokers.github.io/tws-api/message_codes.html | `entitlement_unavailable` (354), 10090 and 10197 -> UNENTITLED | whether any entitlement exists or is shared to paper; no market-data request is made here even if public tokens were available |
| Account summary | "creates a subscription for the account data displayed in the TWS Account Summary window"; tags include "AccountType,NetLiquidation,TotalCashValue,...,BuyingPower"; delivered via `accountSummary` and `accountSummaryEnd`; "only two active summary subscriptions are allowed at a time!"; `cancelAccountSummary`. https://interactivebrokers.github.io/tws-api/account_summary.html | one USD summary control with values withheld; `account_summary_incomplete` (no END); id/currency mismatch; required tags missing; unparseable values reported by tag name only | real tag set, base currency, and that the summary request is cancelled after one snapshot |
| Positions | `reqPositions` -> `position(account, contract, pos, avgCost)` -> `positionEnd`; `cancelPositions`; "For futures, the exchange field will not be populated". https://interactivebrokers.github.io/tws-api/positions.html | complete flat snapshot control; `positions_disconnected` (1100 before END); foreign-account and incomplete lines | real position-line content and whether the subscription is cancelled after one snapshot |
| Connectivity and farm messages | 1100 "Connectivity between IB and the TWS has been lost.", 1101 "...restored- data lost.", 1102 "...restored- data maintained.", 2104 "Market data farm connection is OK", 2106 "A historical data farm is connected.", 2158 "Sec-def data farm connection is OK", 2110 "Connectivity between TWS and server is broken...", 200, 300, 321, 322, 504. https://interactivebrokers.github.io/tws-api/message_codes.html | preflight refusal on an unrestored 1100; 1101 -> INCOMPLETE; 2110 -> ERROR; any unlisted code -> `BROKER_MESSAGE_UNCLASSIFIED` (ERROR) | the message sequences the owner's gateway actually emits |
| Paper account nature | "Trades entered into a paper trading account will not actually execute on any exchange or settle at a clearing house."; "Stops and other complex order types are always simulated in paper trading"; "Trading permissions, market data subscriptions, base currency" match the live account; if the interface does not show a simulated account "you are trading in a production account and will be liable for all trades that fill"; "Only existing clients can request a paper account." https://www.ibkrguides.com/orgportal/aboutpapertradingaccount.htm | the scaffold places orders nowhere, so none of this is exercised; the explicit PAPER attestation is the only environment evidence | eligibility, whether a paper account exists, and all protection/fill behaviour (a later material-risk package) |
| Daily restart and weekly reauthentication | "This procedure will require manual authentication once a week", "each Sunday at 1:00 am ET"; token revocation and OS sleep/updates can prevent the restart. https://ibkrguides.com/traderworkstation/auto-restart-considerations.htm | not exercised (no session lifecycle exists offline) | whether the owner accepts weekly manual reauthentication; unattended survival |
| Requirements, paper limitations, reauthentication (current official pages) | https://www.interactivebrokers.com/docs/tws-api/doc/notes-limitations/requirements ; https://www.interactivebrokers.com/docs/tws-api/doc/notes-limitations/limitations/paper-trading ; https://www.interactivebrokers.com/docs/tws-api/doc/tws-settings/daily-weekly-reauthentication.md *(403, not re-verified)* | none | everything on those pages must be read by a human before any connected check |

## Finding codes

Preflight (kind REFUSED unless noted): `TRANSPORT_NOT_FAKE`, `SESSION_ATTESTATION_INVALID`,
`SESSION_NOT_CONNECTED`, `SESSION_CONNECTIVITY_LOST`, `ENVIRONMENT_UNKNOWN`, `ENVIRONMENT_LIVE_REFUSED`,
`ENVIRONMENT_MISMATCH`, `ACCOUNT_EVIDENCE_ABSENT`, `ACCOUNT_ID_NOT_SYNTHETIC`, `ACCOUNT_NOT_IN_MANAGED_LIST`,
`MANAGED_ACCOUNTS_NOT_SINGULAR`, `READ_ONLY_API_NOT_ATTESTED`; INFO `SOCKET_PORT_NOT_EVIDENCE`.

Session: `SERVER_VERSION_MISSING` (MISSING), `CONNECTION_TIME_MISSING` (MISSING), `CONNECTION_TIME_UNTIMED` /
`CONNECTION_TIME_INCONSISTENT` (MISMATCH), `CONNECTIVITY_RESTORED_DATA_LOST` (INCOMPLETE),
`SESSION_CONNECTIVITY_BROKEN` / `BROKER_MESSAGE_UNCLASSIFIED` (ERROR), INFO `DATA_FARM_STATUS_NOT_OBSERVED`,
INFO `SESSION_ATTESTATION_CONTROL`.

Contract (any MISMATCH, ERROR or INCOMPLETE finding here, or a missing END marker, withholds the conId and
therefore the quote): `CONTRACT_DETAILS_END_MISSING` (INCOMPLETE), `CONTRACT_OBSERVATION_MISSING` / `CONTRACT_FIELDS_MISSING`
(MISSING), `CONTRACT_IDENTITY_AMBIGUOUS` / `TIME_ZONE_UNRESOLVED` / `LIQUID_HOURS_UNPARSEABLE` (AMBIGUOUS),
`CONTRACT_SYMBOL_MISMATCH` / `CONTRACT_SEC_TYPE_MISMATCH` / `CONTRACT_CURRENCY_MISMATCH` /
`CONTRACT_EXCHANGE_MISMATCH` / `CONTRACT_PRIMARY_EXCHANGE_MISMATCH` / `CON_ID_MISMATCH` (MISMATCH),
`OUTSIDE_REGULAR_SESSION` (OUT_OF_SCOPE), `CONTRACT_NOT_FOUND` (ERROR, code 200), INFO `CON_ID_OBSERVED_NOT_PINNED`,
`REGULAR_SESSION_CONTROL`, `CONTRACT_IDENTITY_CONTROL`.

Quote: `QUOTE_SKIPPED_NO_CONTRACT_IDENTITY` / `TICK_SNAPSHOT_END_MISSING` (INCOMPLETE), `QUOTE_OBSERVATION_MISSING` /
`QUOTE_CON_ID_MISSING` / `QUOTE_BID_ASK_MISSING` / `QUOTE_SIZES_MISSING` / `QUOTE_UNTIMED` / `QUOTE_RECEIPT_UNTIMED`
(MISSING), `QUOTE_OBSERVATIONS_MULTIPLE` / `MARKET_DATA_TYPE_UNKNOWN` / `MARKET_DATA_TYPE_CHANGED` /
`MARKET_DATA_TYPE_INCONSISTENT` (AMBIGUOUS), `QUOTE_CON_ID_MISMATCH` / `QUOTE_BID_ASK_INVALID` /
`QUOTE_TIMESTAMP_FUTURE` / `QUOTE_RECEIPT_INCONSISTENT` (MISMATCH), `QUOTE_STALE` / `QUOTE_FROZEN` (STALE),
`QUOTE_DELAYED` (DELAYED), `ENTITLEMENT_UNAVAILABLE` / `ENTITLEMENT_PARTIAL` / `COMPETING_SESSION_NO_DATA`
(UNENTITLED), `INSTRUMENT_HALTED` (OUT_OF_SCOPE), INFO `HALT_STATUS_UNAVAILABLE` / `HALT_STATUS_CLEAR`,
`QUOTE_LIVE_CONTROL`. Freshness uses the shared `MockPolicy.max_quote_age_seconds` window and the gateway's
5 s future tolerance (via `operations_view.freshness`); the package carries no window of its own.

Account: `ACCOUNT_SUMMARY_END_MISSING` (INCOMPLETE), `ACCOUNT_SUMMARY_MISSING` / `ACCOUNT_TYPE_MISSING` /
`ACCOUNT_CURRENCY_MISSING` / `ACCOUNT_TAGS_MISSING` (MISSING), `ACCOUNT_SUMMARY_MULTIPLE` (AMBIGUOUS),
`ACCOUNT_ID_MISMATCH` / `ACCOUNT_CURRENCY_MISMATCH` / `ACCOUNT_VALUES_UNPARSEABLE` (MISMATCH; names only
documented tags and counts unrecognised ones), INFO `ACCOUNT_TAGS_UNRECOGNIZED_WITHHELD` (keys outside the
documented allowlist were counted, never printed), INFO `ACCOUNT_SNAPSHOT_CONTROL`.

Positions: `POSITIONS_END_MISSING` (INCOMPLETE), `POSITION_LINES_INCOMPLETE` (MISSING), `POSITION_ACCOUNT_MISMATCH`
(MISMATCH), INFO `POSITIONS_SNAPSHOT_CONTROL`.

Correlation: `UNCORRELATED_RESPONSE_IGNORED` / `RESPONSE_AFTER_END_IGNORED` / `UNEXPECTED_RESPONSE_KIND`
(AMBIGUOUS), `RESPONSE_LIMIT_EXCEEDED` (INCOMPLETE), `CONFLICTING_DUPLICATE_RESPONSE` (MISMATCH), INFO
`OUT_OF_ORDER_RESPONSES_REORDERED` / `DUPLICATE_RESPONSE_DROPPED`. Request-scoped broker messages classify as in
the table above; `CONNECTIVITY_LOST_MID_REQUEST` (1100) is INCOMPLETE.

## Bundled scenarios

| Scenario | Expected outcome | Proves |
|---|---|---|
| `tlt_regular_session_control` | CONTROL_COMPLETE | the positive deterministic fake-clock control: all six `*_CONTROL` INFO findings, four reads dispatched |
| `environment_unknown_port_7497` | REFUSED_BEFORE_DISPATCH | no inference from the documented paper default port |
| `environment_live` | REFUSED_BEFORE_DISPATCH | LIVE is refused |
| `account_mismatch`, `account_evidence_absent` | REFUSED_BEFORE_DISPATCH | mismatched / absent account evidence |
| `read_only_api_not_attested`, `session_disconnected` | REFUSED_BEFORE_DISPATCH | attestation and connectivity gates |
| `contract_identity_ambiguous`, `contract_not_found`, `contract_currency_mismatch` | NOT_READY | identity and USD basis; the quote is never requested |
| `outside_regular_session` | NOT_READY | regular-session applicability from observed liquid hours |
| `quote_stale`, `quote_delayed`, `quote_untimed`, `entitlement_unavailable` | NOT_READY | freshness, delayed feed, missing timestamp, entitlement |
| `account_summary_incomplete`, `positions_disconnected` | NOT_READY | incomplete and disconnected snapshots |
| `responses_duplicated`, `responses_out_of_order` | CONTROL_COMPLETE | bounded deterministic handling: same digest as the clean stream, anomalies recorded as INFO |
| `responses_uncorrelated` | NOT_READY | a stray request id makes the set ambiguous |

The test suite additionally covers a conflicting duplicate, a response after END, a foreign response kind, the
64-response bound, the pinned-conId mismatch, the fake-clock transitions complete -> stale -> out of session,
the non-fake transport refusal, the quote being withheld for a conflicting-duplicate / incomplete / stray /
post-END / broker-error contract stream, and a sensitive sentinel key in account-summary values that never
reaches any rendered report.

## Gate checklist for future read-only paper checks

None of the following is satisfied today, and this scaffold does not satisfy any of it. Connected read-only
checks may be *considered* only when every item is recorded by the owner or supervisor, and the owner's
non-secret facts are supplied together: residence country; individual/joint/entity account type; existing
IBKR paper/API availability; existing relevant data entitlements; whether periodic weekly/manual
reauthentication is acceptable.

1. **Eligibility and account** verified by the owner (residence, entity, account type), with the current
   requirements page read by a human.
2. **Paper account exists** and is the only account the API session manages; its identifier is held in the
   owner's local private store, never in chat, Git or any model context.
3. **API enabled and Read-Only API enabled** on the owner's TWS/IB Gateway, verified on the machine (the
   setting is not API-queryable). Order precautions and presets are irrelevant to read-only checks and must
   not be bypassed.
4. **Isolation**: a dedicated machine or OS user, a dedicated port chosen by the owner, no live username logged
   in elsewhere (market-data sharing rule), no credentials in the environment of any agent process.
5. **Entitlement**: whether live data is shared to the paper username is known before the first quote request;
   a delayed or frozen answer is a stop, not a fallback. No market-data request is made until this is known.
6. **Reauthentication**: the owner has accepted the weekly manual reauthentication and the daily restart, or
   the check is scheduled inside a freshly authenticated session.
7. **A connected transport** implementing exactly the five-method surface, with the fake's semantics
   (correlated envelopes, fixed request ids, stamped receipts, codes only, no message text retained), and
   `cancelAccountSummary` / `cancelPositions` / `cancelMktData` issued after each single snapshot. It must
   be introduced by a separate reviewed change; the runner's FAKE-only gate is lifted only in that change.
8. **Stop conditions**: unknown account/environment, stale/delayed data, ambiguous contract, inconsistent
   state, insufficient entitlement, any unclassified message code or any correlation anomaly beyond an exact
   duplicate stops the connected check. Offline work continues; nothing is retried blindly.
9. **Scope**: TLT direct identity, USD, regular session only. No proxy, derivative, sizing or risk-policy
   change. Paper order acceptance, protection, reset/correction/bust behaviour and recovery are a later
   material-risk package and remain fail-closed and unresolved.
10. **Record**: the connected report (redacted as here) is stored locally and summarised to the supervisor;
    raw payloads, balances and identifiers never leave the owner's machine.

## Relation to the gateway and shared contracts

`tests/test_paper_readiness.py` runs `gate_action` on a synthetic TLT instruction before and after every
scenario with the same `MockPolicy` instance and asserts identical decisions and an unchanged policy dump.
Readiness reports contain no APPROVE / EXECUTE / SIMULATED / ELIGIBLE / AUTHORIZED vocabulary and no quantity.
The scaffold *reuses* public code read-only: `models.StrictModel`, `Instrument`, `AssetClass`, `MockPolicy`
(quote window), `validator.SYMBOL_RE`, `instrument_diagnostics.AUTHORITY`, and `operations_view.freshness` /
`FUTURE_TOLERANCE_SECONDS`. It writes nothing back and adds no field to any shared model.

## Synthetic inputs only

`SYNTH-PAPER0001`, `SYNTH-PAPER0002`, conIds `900001`..`900003`, server version `176`, the `SYNTH-LISTING`
primary exchange, the liquid-hours strings, every price, size, balance and timestamp are invented. `TLT`,
`USD`, `STK` and `SMART` are the owner-approved target description and documented API vocabulary, not
observations. The real conId and primary listing of TLT are deliberately **not** encoded: they must be
observed by a connected `contractDetails` check before they may be pinned in a `TargetContract`.

## Limitations

- Offline only. Nothing here measures broker latency, reliability, fills, protection, restart survival or
  reauthentication; those are untested and must not be inferred from a complete control.
- The liquid-hours parser accepts only the two documented formats and zones the standard library resolves.
  IBKR's example `timeZoneId` "EST" is a fixed-offset zone without daylight saving; a connected check must
  confirm the spelling actually sent and treat an abbreviation as a stop until its semantics are confirmed.
- Halt status (tick 49) being unavailable is recorded as INFO because the documentation describes it as
  commonly unavailable; a connected gate must decide whether to require it.
- One aggregate quote stands for the 11-second snapshot window; tick-level cadence is not modelled.
- The documented "two active summary subscriptions" and "100 market data lines" limits are not enforced by
  the fake because it issues exactly one request per kind; a connected transport must enforce cancellation.
- The github.io reference is deprecated and the current pages were not fetchable by automation; quotes are
  faithful to the fetched text but a human must re-verify them against the current IBKR Campus documentation.
