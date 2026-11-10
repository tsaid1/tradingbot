# Offline callback-driven readiness demonstration (SIMULATION)

`src/tradingbot/paper_readiness/callback_demo.py` shows the readiness check driven by callbacks instead of
preassembled contract and quote envelopes. It is a **simulation**. It is NON_AUTHORITATIVE, every report
carries `trading_permission: false`, and it is not paper-readiness authorization.

## Run it

```bash
PYTHONPATH=src python -m tradingbot.paper_readiness.callback_demo
```

It takes no arguments. The package admits only pure standard-library imports, so the module has no `sys`
or `argparse`. It prints all three scenarios. The exit status is 0 only when every scenario reaches its
expected outcome, and 1 otherwise. That checks the demonstration itself; it is never a verdict on any real
broker, account or readiness. For JSON from Python:

```python
from tradingbot.paper_readiness import callback_demo as demo
print(demo.render_demo_json([demo.run_demo(name) for name in demo.SCENARIOS]))
```

## What runs

1. Invented primitive values for documented IBKR callbacks are fed to the merged
   `IbkrCallbackNormalizer` (see [IBKR_CALLBACK_NORMALIZATION.md](IBKR_CALLBACK_NORMALIZATION.md)): one
   contract-details request and one quote-snapshot request.
2. The normalizers' `ResponseEnvelope` records go into the existing `FakeScript` and
   `FakeReadOnlyTransport`.
3. The unchanged `run_readiness_check` applies the existing preflight, correlation and fail-closed
   diagnostics, and produces the usual report.

**The account summary and positions are preassembled control envelopes**, invented in this module and
labelled as such in the text and JSON output. They are not produced from callbacks. This demonstration does
not integrate any account or position callback adapter, an installed SDK, a broker or a network. The session
attestation is the fake transport's own invented record, not evidence about any session.

All fixtures (instrument `DEMOETF`, conId, prices, sizes, the `SYNTH-DEMO0001` account and the fixed clock
2026-01-14 16:00 UTC) are invented here. No existing scenario fixture is reused.

## Scenarios

| Scenario | Callbacks | Expected outcome |
|---|---|---|
| `callback_control` | One contract row and `contractDetailsEnd`; `marketDataType` 1, bid, ask, sizes, last and tick 45, then `tickSnapshotEnd` | `CONTROL_COMPLETE` |
| `delayed_size_mixed_with_live` | As the control, plus delayed bid size tick 69 before `tickSnapshotEnd` (PR #25 review P2) | `NOT_READY`: `NON_LIVE_TICK_UNREPRESENTED` withholds the END, so no `QUOTE_LIVE_CONTROL` |
| `issue_with_repeated_end` | A valid row, `unsupported_callback("connectionClosed")`, then `contractDetailsEnd` twice (PR #25 review P1) | `NOT_READY`: `CONTRACT_DETAILS_END_MISSING`, no contract identity and no quote request |

## Limits

- Callback values are plain Python objects shaped like documented ones. No SDK compatibility is claimed.
- Only the contract and quote reads are callback-driven.
- The fake transport, runner and diagnostics are unchanged. This adds no connected path, and connected checks
  stay blocked as described in [PAPER_READINESS.md](PAPER_READINESS.md).
- There is no order, cancel, replace, reset, settings, subscription or credential interface.
