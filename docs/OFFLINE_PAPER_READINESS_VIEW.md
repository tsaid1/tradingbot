# Offline paper-readiness operator view (OPS-03, FAKE ONLY, NON-AUTHORITATIVE)

`dashboard/pages/4_Paper_Readiness.py` shows the redacted reports of the bundled synthetic scenarios from the
merged offline readiness scaffold (docs/PAPER_READINESS.md, issue
https://github.com/tsaid1/tradingbot/issues/16) inside the existing Streamlit app, so an operator can read a
synthetic control, a preflight refusal, an ambiguous identity, stale or delayed data and the still-unknown
prerequisites without the CLI. Issue: https://github.com/tsaid1/tradingbot/issues/22.

**This page is not a broker connection, not trading authorization and not readiness.** Every scenario is an
invented script replayed through the scaffold's FAKE transport under a FAKE clock. Connected capability is
unknown and `trading_permission` is `False` on every view. `CONTROL_COMPLETE` means only that a synthetic
observation set was complete and consistent.

## Running it

```bash
# From the repository root, imports pinned to this checkout. The page appears as "Paper Readiness" in the
# sidebar of the existing app:
PYTHONPATH=src streamlit run dashboard/app.py

# Or standalone, headless (open the server ROOT URL, never a /Paper_Readiness deep link):
PYTHONPATH=src streamlit run dashboard/pages/4_Paper_Readiness.py --server.headless true --server.port 8504

# Regression tests (headless AppTest; no browser or server needed):
PYTHONPATH=src python -m pytest tests/test_paper_readiness_page.py
```

The sidebar prints the resolved `tradingbot` import origin: confirm it is this checkout's `src/tradingbot`
and not another editable install before trusting what you read.

## Controls

Exactly two: the **Scenario** radio (the bundled synthetic scripts, labelled with their scripted outcome)
and **Re-run selected scenario (deterministic)**. There is no path, upload, account, credential, endpoint or
configuration field, no environment read, no network, no connect/order/cancel/replace/reset/start/resume
control and no configuration write. The tests assert the widget inventory, the page's imports and that a
render with every outbound connection primitive blocked leaves the filesystem untouched.

## Reading a report

- **Banner.** "OFFLINE / FAKE ONLY" at the top and in the sidebar on every render.
- **Scenario.** Name, description and the scripted expectation versus the actual outcome ("as scripted" or
  "UNEXPECTED"); the codes the script was written to exercise are checked for presence.
- **Synthetic outcome.** `REFUSED_BEFORE_DISPATCH` (preflight refused; no read dispatched; nothing observed),
  `NOT_READY` (at least one non-INFO finding) or `CONTROL_COMPLETE` (every finding INFO). The headline is the
  report's most pressing finding kind in its own order: REFUSED > ERROR > INCOMPLETE > MISMATCH > AMBIGUOUS >
  UNENTITLED > DELAYED > STALE > OUT_OF_SCOPE > MISSING > INFO. The redacted `account_ref` (prefix plus 8 hex
  digits of a SHA-256), the target symbol and the **FAKE-clock** `as_of` are shown; the wall clock is never read.
- **Deterministic replay.** Run counter per scenario and browser session plus the SHA-256 of the report's
  JSON rendering; a re-run must say "identical to the previous run". A difference is a defect, not evidence.
- **Stages.** One row per stage from the report's dispatch list and observation digests: which read was
  dispatched (request id), rows kept, whether the END marker arrived, the most pressing finding kind and the
  codes. A stage that was not dispatched or not assessed is shown as **unknown**, never as passed.
- **Findings.** Every `Finding` of the report by stage: kind, code, request id and the documented detail.
  INFO lines are informational controls, not permission.
- **Observation digests.** Stage, request id, rows kept, END seen and the SHA-256 of the kept envelopes, so a
  report can be tied to its inputs without carrying them.
- **Unknown and missing evidence.** Reads that were not dispatched, findings whose kind records absent,
  incomplete, ambiguous or unentitled evidence, and the static list of what stays unknown offline whatever the
  outcome.
- **Prerequisite checklist.** The ten gate items from docs/PAPER_READINESS.md, each marked **NOT RECORDED**.
  The page cannot attest, verify or configure any of them.

## What is never shown

No scenario script, observation payload, account identifier, balance, price, size or undocumented
account-summary key. The page only reads `ReadinessReport` fields (outcome, headline, counts, dispatched,
observation digests, findings, redacted `account_ref`, notice) plus the scenario name, description and
scripted expectation. It does not recompute or relax any readiness rule: the per-stage summary groups the
report's own findings with the report's own kind order. The tests run an invented sentinel scenario (sentinel
account, prices, balances, position and a sensitive account tag) through the page and assert that none of it
is rendered, and that a runner exception halts the page with the exception type only.

## Limits

- Offline only. Nothing here measures or implies broker reachability, latency, fills, protection, restart
  survival or reauthentication; a complete control proves none of it. Every connected capability listed under
  "Still unknown" remains unknown until the owner prerequisites are recorded and a separate reviewed change
  introduces a connected transport.
- `as_of` is the scenario's FAKE clock, not the current time; the browser does not auto-refresh.
- Streamlit multipage deep links break the embedded frontend's websocket; open the server root. In the AO
  Browser panel (6 November 2026) the page rendered fully, the re-run button responded to a click, and the
  scenario radio responded to keyboard navigation (focus the selected radio input, then ArrowDown/ArrowUp)
  but not to synthetic label clicks; the headless AppTest suite remains the interaction evidence.
- The page adds no module, contract, policy, transport, report field or journal; it changes no shared file.
