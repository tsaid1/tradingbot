"""Behavioural no-order-path proof (review S1): a complete shadow lifecycle, including a reconnect,
in a SUBPROCESS that loads only the runtime's production entry points, under an import hook that
refuses (and records) any import of ``tradingbot.ibkr_orders``. The text and AST scans in
tests/test_ibkr_paper_disabled_by_default.py stay as a second layer.

The subprocess loads one file from tests/: ``_ibkr_fake_tws.py``, the fake transport (it imports
nothing from ``tradingbot``; asserted below). The session is built by the production factory;
only the two local preflight probes (the TWS profile file and the listener table) are stubbed,
as no TWS runs here. No socket is opened. Invented posts and prices."""
from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

pytest.importorskip("ibapi")

ROOT = Path(__file__).resolve().parents[1]

DRIVER = textwrap.dedent(r'''
    import importlib.abc, json, os, socket, stat, sys, time
    sys.path.insert(0, sys.argv[4])  # this checkout's src/ (the interpreter runs isolated, -I)
    ATTEMPTS = []
    CONFIRM = os.environ.get("SHADOW_PROBE_CONFIRMATION") == "1"  # operator confirmation on

    class OrderPackageGuard(importlib.abc.MetaPathFinder):
        def find_spec(self, name, path, target=None):
            if name == "tradingbot.ibkr_orders" or name.startswith("tradingbot.ibkr_orders."):
                ATTEMPTS.append(name)
                raise ImportError(f"order package import refused in shadow mode: {name}")
            return None

    sys.meta_path.insert(0, OrderPackageGuard())

    def no_network(*a, **k):
        raise AssertionError("network access attempted")

    socket.socket.connect = no_network
    socket.create_connection = no_network
    folder, tests = sys.argv[1], sys.argv[2]
    sys.path.insert(0, tests)
    import _ibkr_fake_tws as F  # the fake transport only
    import ibapi.client
    from ibapi.message import IN
    from ibapi.protobuf.MarketDataType_pb2 import MarketDataType
    from ibapi.protobuf.TickPrice_pb2 import TickPrice
    sys.path.remove(tests)

    tws = F.FakeTws()
    ibapi.client.Connection = tws.connection_factory
    subs = []
    tws.on["REQ_MKT_DATA"] = lambda s, c, r: subs.append((c, r.reqId))

    import tradingbot.ibkr.environment as env  # the two local probes: no TWS runs here
    env.confirm_paper_profile = lambda path, user: {"stub": "profile"}
    env.confirm_listener = lambda port, user: {"stub": "listener"}
    if os.environ.get("SHADOW_PROBE_INJECT") == "1":  # the reviewer's injection, for the proof
        import tradingbot.execution.shadow_broker as SB
        real = SB.default_session_factory

        def injected(cfg):
            __import__("tradingbot.ibkr_" + "orders.session")
            return real(cfg)

        SB.default_session_factory = injected

    from datetime import UTC, datetime, timedelta
    from tradingbot.execution.sim_broker import SimClock
    from tradingbot.runtime.app import build_service
    from tradingbot.runtime.config import parse_config
    from tradingbot.runtime.operator_actions import OperatorQueue

    d = os.path.join(folder, "private")
    os.makedirs(d, mode=0o700)
    paper = os.path.join(d, "paper.toml")
    with open(paper, "w") as f:
        f.write('mode = "paper"\nhost = "127.0.0.1"\nport = 7497\nclient_id = 7\n'
                f'account = "{F.PAPER}"\ntws_profile_ini = "/Users/someone/Jts/jts.ini"\n'
                'tws_os_user = "someone"\nread_only_check = "config-read"\n')
    os.chmod(paper, stat.S_IRUSR | stat.S_IWUSR)
    base = datetime(2026, 3, 2, 15, 0, tzinfo=UTC)
    posts = os.path.join(folder, "posts.jsonl")
    with open(posts, "w") as f:
        for sid, off, text in (("1", 0, "We are now long $TLT at 88.42. Stop 87.20."),  # at the ask
                               ("2", 120, "Raising our $TLT stop to 87.90."),
                               ("3", 240, "Closing the rest of our $TLT long at 88.40."),
                               *((("4", 360, "We are now long $TLT, ~88.41 now. Stop 87.20."),)
                                 if CONFIRM else ())):
            at = base + timedelta(seconds=off)
            f.write(json.dumps({
                "schema_version": "1.2", "source_id": sid, "post_id": sid, "revision": 1,
                "author_id": "4242", "verified_author": True, "text": text,
                "text_complete": True, "is_repost": False, "published_at": at.isoformat(),
                "revised_at": None, "received_at": (at + timedelta(seconds=2)).isoformat(),
                "provenance": "SYNTHETIC", "conversation_id": None, "links": [],
                "media": []}) + "\n")
    raw = {
        "mode": "shadow",
        "paths": {"state_dir": os.path.join(folder, "state"),
                  "halt_file": os.path.join(folder, "HALT"),
                  "mapping_file": sys.argv[3]},
        "source": {"kind": "replay", "file": posts, "compression": 1.0},
        "service": {"cadence_seconds": 5, "max_backoff_seconds": 10, "failure_alert_after": 3,
                    "stuck_after_seconds": 60},
        "policy": {"quantity": 1, "quantity_per_add": 1, "max_price_deviation_pct": "1.0",
                   "max_spread_pct": "1.0", "max_notional_usd": "2000",
                   "max_signal_age_seconds": 90, "max_quote_age_seconds": 30,
                   "max_instrument_exposure_usd": "2000", "max_aggregate_exposure_usd": "10000"},
        "execution": {"expected_account": "SIM-SHADOW-0001", "max_submit_attempts": 2,
                      "settle_seconds": 10, "exit_no_trace_lookups": 3,
                      "max_intent_age_seconds": 120, "max_exit_intent_age_seconds": 120,
                      "lookup_horizon_seconds": 21600, "confirm_attempts": 3,
                      "confirm_wait_seconds": 0},
        "shadow": {"paper_config": paper},
    }
    if CONFIRM:
        raw["operator_confirmation"] = {"enabled": True}
    config = parse_config(raw, base=__import__("pathlib").Path(folder))
    OperatorQueue.initialise(config.operator_db)
    clock = SimClock(datetime(2026, 6, 1, 14, 0, tzinfo=UTC))  # Monday, US regular session
    service = build_service(config, clock=clock.now)  # the production session factory
    broker = service.broker
    n = [0]

    def tick():
        """A real change on both sides of the newest stream, then wait until it is seen."""
        n[0] += 1
        conn, rid = subs[-1]
        bid, ask = 88.40 - (n[0] % 2) / 100, 88.42 - (n[0] % 2) / 100  # ask never above 88.42
        conn.push(F.frame(IN.MARKET_DATA_TYPE, MarketDataType(reqId=rid, marketDataType=1)))
        for t, v in ((1, bid), (2, ask)):
            conn.push(F.frame(IN.TICK_PRICE, TickPrice(reqId=rid, tickType=t, price=v,
                                                       size="100", attrMask=0)))
        stream = broker._streams.get(next(iter(broker._streams), None))
        deadline = time.monotonic() + 3
        while stream is not None and time.monotonic() < deadline:
            try:
                q = stream.latest()
            except Exception:
                break
            if (q.bid, q.ask) == (bid, ask):
                break
            time.sleep(0.01)

    dropped = False
    want = 4 if CONFIRM else 3
    confirmed = []
    for cycle in range(200):
        if subs and broker.connected():
            tick()
        service.run_once()
        clock.advance(5)
        if not dropped and len(service.store.signals()) >= 1:
            tws.drop()  # TWS restarts: the session must be reopened with every check
            dropped = True
            time.sleep(0.2)
        if CONFIRM:
            from tradingbot.execution.operator_catalog import offer_phrase
            for o in service.store.offers(states=("OFFERED",)):
                if o["id"] not in confirmed:
                    confirmed.append(o["id"])
                    OperatorQueue(config.operator_db).enqueue(
                        at=clock.now(), operator="owner", origin="test",
                        action="CONFIRM_SIGNAL", target=str(o["id"]), kind=o["hash"], note="",
                        confirmation=offer_phrase("CONFIRM_SIGNAL", o["id"], o["hash"]))
            if any(o["state"] == "OFFERED" for o in service.store.offers()):
                continue
        if len(service.store.signals()) >= want and broker.reconnects >= 1:
            for _ in range(2):
                tick(); service.run_once(); clock.advance(5)
            break
    events = service.store.events()
    fills = [e["detail"] for e in events if e["event"] == "SHADOW_SIMULATED_FILL"]
    service.close()
    print(json.dumps({
        "attempts": ATTEMPTS,
        "loaded": sorted(m for m in sys.modules if m.startswith("tradingbot.ibkr_orders")),
        "reconnects": broker.reconnects,
        "signals": len(service.store.signals()),
        "fills": [(f["side"], f["label"]) for f in fills],
        "positions": len(broker.positions()),
        "wire": sorted({name for name, _ in tws.requests}),
        "reconnected": any(e["event"] == "BROKER_RECONNECT_ATTEMPT" for e in events),
        "offers": [o["state"] for o in service.store.offers()],
    }))
''')
READ_ONLY = {"START_API", "REQ_CONTRACT_DATA", "CANCEL_CONTRACT_DATA", "REQ_ACCOUNT_SUMMARY",
             "CANCEL_ACCOUNT_SUMMARY", "REQ_POSITIONS", "CANCEL_POSITIONS", "REQ_MKT_DATA",
             "CANCEL_MKT_DATA", "REQ_MARKET_DATA_TYPE", "REQ_CONFIG", "REQ_SEC_DEF_OPT_PARAMS"}


def _run(tmp_path: Path, inject: bool = False,
         confirmation: bool = False) -> subprocess.CompletedProcess:
    script = tmp_path / "driver.py"
    script.write_text(DRIVER)
    work = tmp_path / "run"
    work.mkdir()
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "HOME": str(tmp_path),
           "SHADOW_PROBE_INJECT": "1" if inject else "0",
           "SHADOW_PROBE_CONFIRMATION": "1" if confirmation else "0"}
    env.pop("TRADINGBOT_BLOCK_IBAPI", None)
    return subprocess.run(
        [sys.executable, "-I", str(script), str(work), str(ROOT / "tests"),
         str(ROOT / "config" / "instrument_mapping.toml"), str(ROOT / "src")],
        env=env, capture_output=True, text=True, check=False, timeout=300, cwd=tmp_path)


def test_the_fake_transport_imports_nothing_from_tradingbot():
    tree = ast.parse((ROOT / "tests" / "_ibkr_fake_tws.py").read_text())
    mods = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    mods |= {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    assert not {m for m in mods if m.startswith("tradingbot")}


def test_a_full_shadow_lifecycle_with_a_reconnect_never_imports_the_order_package(tmp_path):
    r = _run(tmp_path)
    assert r.returncode == 0, r.stderr[-3000:]
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert out["attempts"] == [] and out["loaded"] == []
    assert out["reconnects"] >= 1 and out["reconnected"] and out["signals"] == 3
    assert ["BUY", "SIMULATED_AT_LIVE_QUOTE"] in out["fills"]
    assert ["SELL", "SIMULATED_AT_LIVE_QUOTE"] in out["fills"] and out["positions"] == 0
    assert set(out["wire"]) <= READ_ONLY, out["wire"]


def test_the_proof_fails_when_the_session_factory_imports_the_order_package(tmp_path):
    """The reviewer's injection (a dynamic import in the session factory) is caught."""
    r = _run(tmp_path, inject=True)
    failed = r.returncode != 0
    if not failed:
        out = json.loads(r.stdout.strip().splitlines()[-1])
        failed = bool(out["attempts"] or out["loaded"])
    assert failed and "tradingbot.ibkr_orders" in (r.stdout + r.stderr)


def test_with_operator_confirmation_on_a_confirmed_entry_never_imports_the_order_package(
        tmp_path):
    """Operator confirmation (docs/OPERATOR_CONFIRMATION.md) switched on: the same lifecycle
    plus a held anchored entry the owner confirms, released through the gateway and simulated at
    a live quote. Still no import of the order package, and only read-only messages on the wire."""
    r = _run(tmp_path, confirmation=True)
    assert r.returncode == 0, r.stderr[-3000:]
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert out["attempts"] == [] and out["loaded"] == []
    assert out["offers"] == ["EXECUTED"] and out["signals"] == 4
    assert out["fills"].count(["BUY", "SIMULATED_AT_LIVE_QUOTE"]) == 2
    assert set(out["wire"]) <= READ_ONLY, out["wire"]
