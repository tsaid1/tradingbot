"""Shadow mode end to end: the whole runtime service on the REAL ibapi 1052.1 client, reader and
decoder over the in-repo fake TWS (``tests/_ibkr_fake_tws.py``). Orders and fills are simulated;
quotes are real held streams. No socket is opened, nothing under ``~/.config`` is read, and every
post and price is invented."""
from __future__ import annotations

import contextlib
import os
import stat
import sys
import time
from decimal import Decimal
from pathlib import Path

import pytest

pytest.importorskip("ibapi")

from _ibkr_fake_tws import (
    OTHER_PAPER,
    PAPER,
    FakeTws,
    contract_end_frame,
    contract_frame,
    error_frame,
    frame,
)
from ibapi.message import IN
from ibapi.protobuf.MarketDataType_pb2 import MarketDataType
from ibapi.protobuf.TickPrice_pb2 import TickPrice
from runtime_harness import (
    COMMENTARY,
    MAPPING,
    REQUIRED_EXECUTION,
    REQUIRED_POLICY,
    REQUIRED_SERVICE,
    START,  # Monday 10:00 New York: inside the TLT session
    RecordingNotifier,
    post,
    write_posts,
)
from test_ibkr_paper_disabled_by_default import READ_ONLY_OUT
from test_ibkr_quote_streams_sdk import Feed
from test_ibkr_session_sdk import CFG_DATA, stub_preflight, tws  # noqa: F401 - fixture

from tradingbot.execution.broker import BrokerEnvironment, broker_environment
from tradingbot.execution.shadow_broker import FILL_LABEL, ShadowBroker
from tradingbot.execution.sim_broker import SimClock
from tradingbot.ibkr import session as session_mod
from tradingbot.ibkr.errors import ListenerNotConfirmed
from tradingbot.ibkr.session import ALLOWED_OUT_MESSAGES, IbkrSession, _outgoing_msg_id
from tradingbot.runtime.app import build_service
from tradingbot.runtime.config import ConfigError, parse_config
from tradingbot.runtime.operator_actions import OperatorQueue
from tradingbot.runtime.service import BROKER_RECONNECT_PENDING

SHADOW_ACCOUNT = "SIM-SHADOW-0001"
# At the ask: since PR #70 an explicit entry is sent at the stated price, so a stated entry below
# the ask would rest. These tests are about the shadow lifecycle, not about resting entries.
EXPLICIT = "We are now long $TLT at 88.42. Stop 87.20."
ANCHORED = "We are now long $TLT, 88.42 now. Stop 87.20."
RAISE_STOP = "Raising our $TLT stop to 87.90."
CLOSE = "Closing the rest of our $TLT long at 88.40."
# Every message a shadow run may send: the read-only adapter's allowlist (market data, contract
# details, account and position reads, the config read). Nothing else may ever reach the wire.
SHADOW_ALLOWED = READ_ONLY_OUT
ORDER_MESSAGES = {"PLACE_ORDER", "CANCEL_ORDER", "REQ_GLOBAL_CANCEL", "EXERCISE_OPTIONS",
                  "UPDATE_CONFIG", "REPLACE_FA", "REQ_IDS", "REQ_OPEN_ORDERS",
                  "REQ_ALL_OPEN_ORDERS", "REQ_AUTO_OPEN_ORDERS", "REQ_EXECUTIONS",
                  "REQ_COMPLETED_ORDERS"}


# ------------------------------------------------------------------ harness
class Mono:
    """The adapter's monotonic clock (the session's ``_side_clock`` seam), moved with the
    service's SimClock so ages are deterministic."""

    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def paper_config_file(folder: Path) -> Path:
    d = folder / "private"
    d.mkdir(mode=0o700)
    p = d / "paper.toml"
    lines = [f"{k} = {v!r}" if isinstance(v, str) else f"{k} = {v}" for k, v in CFG_DATA.items()]
    p.write_text("\n".join(lines).replace("'", '"') + "\n")
    os.chmod(p, stat.S_IRUSR | stat.S_IWUSR)
    return p


def fast_session(cfg, cls=IbkrSession, preflight=stub_preflight, cap=None):
    s = cls(cfg, connect_timeout=2.0, request_timeout=2.0, quote_timeout=2.0,
            shutdown_timeout=3.0, preflight=preflight)
    if cap is not None:
        s.max_quote_streams = cap
    return s


class Shadow:
    def __init__(self, folder: Path, server: FakeTws, mono: Mono, posts=(), *, factory=None,
                 restart: bool = False, clock: SimClock | None = None,
                 interpreter: dict | None = None, allow_candidate_lexicon: bool = False):
        """``restart``: a new process on the same files (posts, state, private config)."""
        self.folder, self.tws, self.mono = folder, server, mono
        self.clock = clock or SimClock(START)
        self.feed = Feed(server)
        raw = {
            "mode": "shadow",
            "paths": {"state_dir": str(folder / "state"), "halt_file": str(folder / "HALT"),
                      "mapping_file": str(MAPPING)},
            "source": {"kind": "replay", "compression": 1.0,
                       "file": str(folder / "posts.jsonl") if restart
                       else str(write_posts(folder / "posts.jsonl", list(posts)))},
            "service": dict(REQUIRED_SERVICE),
            "policy": dict(REQUIRED_POLICY),
            "execution": {**REQUIRED_EXECUTION, "expected_account": SHADOW_ACCOUNT},
            "shadow": {"paper_config": str(folder / "private" / "paper.toml") if restart
                       else str(paper_config_file(folder))},
        }
        if interpreter:
            raw["interpreter"] = dict(interpreter)
        self.raw = raw
        self.config = parse_config(raw, base=folder)
        if not restart:
            OperatorQueue.initialise(self.config.operator_db)
        self.notifier = RecordingNotifier()
        self.service = build_service(self.config, clock=self.clock.now, notifier=self.notifier,
                                     session_factory=factory or fast_session,
                                     allow_candidate_lexicon=allow_candidate_lexicon)
        self.broker: ShadowBroker = self.service.broker

    @property
    def session(self):
        return self.broker._session

    def tick(self, idx: int = -1, *frames: bytes) -> None:
        self.feed.push(self.session, idx if idx >= 0 else len(self.feed.subs) - 1, *frames)

    def rid(self) -> int:
        return self.feed.rid(len(self.feed.subs) - 1)

    def live(self, bid: float, ask: float, *, mdt: int | None = 1) -> None:
        """A reported-live image (or a change) on both sides of the newest stream, now."""
        rid = self.rid()
        frames = [frame(IN.MARKET_DATA_TYPE, MarketDataType(reqId=rid, marketDataType=mdt))
                  ] if mdt is not None else []
        frames += [price(rid, 1, bid), price(rid, 2, ask)]
        self.tick(-1, *frames)

    def step(self, seconds: float = 1.0):
        report = self.service.run_once()
        self.clock.advance(seconds)
        self.mono.t += seconds
        return report

    def actions(self):
        return self.service.store.signal_actions()

    def events(self, name: str) -> list[dict]:
        return [e["detail"] for e in self.service.store.events() if e["event"] == name]

    def sim(self):
        return self.broker._sim

    def close(self) -> None:
        self.service.close()


def price(rid: int, tick: int, value: float, size: str = "100") -> bytes:
    return frame(IN.TICK_PRICE, TickPrice(reqId=rid, tickType=tick, price=value, size=size,
                                          attrMask=0))


def wire_names(server: FakeTws) -> set[str]:
    return {n for n, _ in server.requests}


def assert_only_read_only_messages(server: FakeTws) -> None:
    names = wire_names(server)
    assert names <= SHADOW_ALLOWED, names - SHADOW_ALLOWED
    assert not names & ORDER_MESSAGES
    from ibapi.message import OUT
    order_ids = {int(getattr(OUT, n)) for n in ("PLACE_ORDER", "CANCEL_ORDER",
                                                "REQ_GLOBAL_CANCEL", "EXERCISE_OPTIONS")}
    for raw in server.wire:  # every byte string that reached the fake socket, after the handshake
        if raw.startswith(b"API\0"):
            continue
        mid = _outgoing_msg_id(raw, server.server_version)
        assert mid in ALLOWED_OUT_MESSAGES and mid not in order_ids, mid


@pytest.fixture
def mono(monkeypatch):
    m = Mono()
    monkeypatch.setattr(session_mod, "_side_clock", m)
    return m


@pytest.fixture
def shadow(tmp_path, tws, mono):  # noqa: F811
    made = []

    def make(posts=(), **kw):
        s = Shadow(tmp_path, tws, mono, posts, **kw)
        made.append(s)
        return s

    yield make
    for s in made:
        with contextlib.suppress(Exception):  # already closed by the test
            s.close()


def run_until_processed(sh: Shadow, n: int, *, before=None, cycles: int = 6) -> None:
    for _ in range(cycles):
        if before is not None:
            before()
        sh.step()
        if len(sh.service.store.signals()) >= n:
            return
    raise AssertionError(f"post {n} not processed: {sh.service.store.signals()}")


# ------------------------------------------------------------------ the lifecycles
def test_explicit_open_amend_and_close_all_simulated_at_real_quotes(shadow, tws):  # noqa: F811
    sh = shadow([post("1", 0, EXPLICIT), post("2", 30, RAISE_STOP), post("3", 60, CLOSE)])
    assert broker_environment(sh.broker) is BrokerEnvironment.SIM
    assert len(sh.feed.subs) == 1  # the TLT stream was opened at start (watched family)
    moves = iter(range(100000))

    def keep_fresh():  # real changes on both sides (a repeated image would age: review B2)
        i = next(moves) % 2
        sh.live(88.40, 88.42 + i / 100)
        sh.live(88.40 - i / 100, 88.42)
    run_until_processed(sh, 1, before=keep_fresh)
    [a] = [a for a in sh.actions() if a["action"] == "OPEN"]
    assert a["status"] == "EXECUTED", (a["status"], a["reasons"])
    fills = sh.events("SHADOW_SIMULATED_FILL")
    assert [(f["side"], f["price"], f["label"]) for f in fills] == [
        ("BUY", "88.42", "SIMULATED_AT_LIVE_QUOTE")]
    assert fills[0]["quote"]["label"] == "LIVE" and fills[0]["quote"]["reason"] == "REPORTED_LIVE"
    stops = [o for o in sh.sim().open_orders() if o.order_type.value == "STP"]
    assert [o.stop_price for o in stops] == [Decimal("87.20")]
    run_until_processed(sh, 2, before=keep_fresh, cycles=40)
    for _ in range(2):
        keep_fresh()
        sh.step()
    stops = [o for o in sh.sim().open_orders() if o.order_type.value == "STP"]
    assert [o.stop_price for o in stops] == [Decimal("87.90")]
    run_until_processed(sh, 3, before=keep_fresh, cycles=40)
    for _ in range(2):
        keep_fresh()
        sh.step()
    assert sh.sim().positions() == []
    sells = [f for f in sh.events("SHADOW_SIMULATED_FILL") if f["side"] == "SELL"]
    assert [Decimal(f["price"]) for f in sells] == [Decimal("88.40")]  # at the real bid
    hb = sh.service.store.get("heartbeat")
    assert hb["mode"] == "shadow" and hb["shadow"]["fills"] == FILL_LABEL
    start = sh.events("SERVICE_START")[0]
    assert start["mode"] == "shadow" and start["fills"] == FILL_LABEL
    decisions = sh.events("SHADOW_DECISION_QUOTES")
    assert decisions and all(q["label"] == "LIVE" for d in decisions for q in d["quotes"])
    sh.close()
    assert_only_read_only_messages(tws)


def test_anchored_open_fills_at_the_real_ask_and_the_stop_is_simulated(shadow, tws, mono):  # noqa: F811
    sh = shadow([post("1", 0, ANCHORED)])
    moves = iter(range(1000))

    def fresh():  # both sides change every second: per-side ages stay below 3 s
        i = next(moves) % 2
        sh.live(88.40, 88.42 + i / 100, mdt=1)
        sh.live(88.40 - i / 100, 88.42, mdt=1)

    run_until_processed(sh, 1, before=fresh)
    [a] = [a for a in sh.actions() if a["action"] == "OPEN"]
    assert a["status"] == "EXECUTED", (a["status"], a["reasons"])
    [entry] = [o for o in sh.sim().all_orders() if o.parent_ref is None]
    assert entry.order_type.value == "LMT" and entry.limit_price == Decimal("88.42")
    [fill] = sh.events("SHADOW_SIMULATED_FILL")
    assert (fill["side"], fill["price"], fill["quantity"]) == ("BUY", "88.42", 1)
    [decision] = sh.events("SHADOW_DECISION_QUOTES")
    ages = [(q["bid_age_seconds"], q["ask_age_seconds"]) for q in decision["quotes"]]
    assert ages and all(b is not None and a <= 3 and b <= 3 for b, a in ages)
    # The real market falls through the protective stop: the simulator fills it at the bid.
    sh.live(87.10, 87.12)
    sh.step()
    sells = [f for f in sh.events("SHADOW_SIMULATED_FILL") if f["side"] == "SELL"]
    assert [Decimal(f["price"]) for f in sells] == [Decimal("87.10")]
    assert sh.sim().positions() == []
    sh.close()
    assert_only_read_only_messages(tws)


# ------------------------------------------------------------------ the same anchored post HELD
def _held(sh: Shadow, before, cycles: int = 4) -> dict:
    run_until_processed(sh, 1, before=before, cycles=cycles)
    [a] = [a for a in sh.actions() if a["action"] == "OPEN"]
    assert a["status"] in ("HOLD", "BLOCK"), (a["status"], a["reasons"])  # never traded
    assert sh.sim().all_orders() == [] and sh.sim().submit_calls == 0
    return a


def test_held_on_delayed_quotes(shadow):
    sh = shadow([post("1", 0, ANCHORED)])

    def delayed():
        rid = sh.rid()
        sh.live(88.40, 88.42, mdt=3)
        sh.tick(-1, price(rid, 66, 88.40), price(rid, 67, 88.42))

    a = _held(sh, delayed)
    # Delayed ticks carry no live per-side time: the execution core sees the quote UNAVAILABLE
    # (review B2); the journal keeps the adapter's DELAYED label and the reason.
    assert a["reasons"] == ["QUOTE_UNAVAILABLE"], a["reasons"]
    [d] = sh.events("SHADOW_DECISION_QUOTES")
    assert {(q["label"], q["adapter_label"], q["reason"]) for q in d["quotes"]} == {
        ("UNAVAILABLE", "DELAYED", "REPORTED_DELAYED")}
    assert all("SIDE_TIME_UNKNOWN" in q["problems"] for q in d["quotes"])


def test_held_when_the_type_is_unreported_and_the_sides_do_not_change(shadow):
    sh = shadow([post("1", 0, ANCHORED)])
    a = _held(sh, lambda: sh.live(88.40, 88.42, mdt=None))  # the same image, repeated
    assert a["reasons"] == ["QUOTE_QUALITY_FROZEN"] or "QUALITY:FROZEN" in a["reasons"]
    reasons = {q["reason"] for d in sh.events("SHADOW_DECISION_QUOTES") for q in d["quotes"]}
    assert reasons == {"TYPE_UNREPORTED_NO_UPDATES_YET"}


def test_held_when_one_side_is_older_than_three_seconds(shadow, mono):
    sh = shadow([post("1", 0, ANCHORED)])
    sh.live(88.40, 88.42)
    moves = iter(range(1000))

    def bid_only():  # only the bid changes; the ask image ages past 3 s
        sh.tick(-1, price(sh.rid(), 1, 88.38 + (next(moves) % 2) / 100))
        mono.t += 1.0

    for _ in range(3):
        bid_only()
    a = _held(sh, bid_only)
    assert a["reasons"][:3] == ["ENTRY_ANCHOR_QUOTE_SIDE_STALE", "SIDE:ASK", "MAX_AGE:3"]


def test_held_when_the_stream_failed_and_the_stream_is_reopened(shadow):
    sh = shadow([post("1", 0, ANCHORED)])
    sh.live(88.40, 88.42)
    sh.tick(-1, error_frame(sh.rid(), 10090, "Part of requested market data is not subscribed"))
    first = len(sh.feed.subs)
    a = _held(sh, lambda: None, cycles=2)
    assert a["reasons"][0] in ("ENTRY_ANCHOR_QUOTE_NOT_LIVE", "QUOTE_UNAVAILABLE",
                               "ENTRY_ANCHOR_QUOTE_SIDE_MISSING"), a["reasons"]
    quotes = [q for d in sh.events("SHADOW_DECISION_QUOTES") for q in d["quotes"]]
    assert quotes[0]["label"] == "UNAVAILABLE" and "STREAM_FAILED" in quotes[0]["problems"]
    assert len(sh.feed.subs) > first  # the failed stream was cancelled and a new one opened
    assert "CANCEL_MKT_DATA" in wire_names(sh.tws)


def test_held_when_tws_loses_its_connectivity(shadow):
    sh = shadow([post("1", 0, ANCHORED)])
    sh.live(88.40, 88.42)
    sh.tick(-1, error_frame(-1, 1100, "Connectivity between IB and TWS has been lost."))
    a = _held(sh, lambda: None, cycles=2)
    quotes = [q for d in sh.events("SHADOW_DECISION_QUOTES") for q in d["quotes"]]
    assert quotes and all(q["label"] == "UNAVAILABLE" for q in quotes), quotes
    assert a["status"] in ("HOLD", "BLOCK")


def test_a_dropped_session_blocks_entries_alerts_and_reconnects_with_new_streams(shadow, tws):  # noqa: F811
    # The anchored post is published well after the outage (a post during it is not consumed).
    sh = shadow([post("0", 0, COMMENTARY), post("1", 400, ANCHORED)])
    sh.live(88.40, 88.42)
    old = sh.session
    tws.drop()  # TWS closes the socket (a restart)
    for _ in range(50):
        if not sh.broker.connected():
            break
        time.sleep(0.02)
    tws.refuse = True  # TWS not back yet: the reconnect fails
    report = sh.step()
    assert report.skipped == "broker_unavailable" and sh.service.store.signals() == []
    assert not sh.broker.connected()
    kinds = {e.kind for e in sh.service.coordinator.store.exceptions(open_only=True)}
    assert BROKER_RECONNECT_PENDING in kinds
    assert any(a["kind"] == "BROKER_FAILURE" for a in sh.service.store.alerts(open_only=True))
    tws.refuse = False
    for _ in range(20):  # the broker backoff, then a full reconnect with every check again
        sh.step(5.0)
        if sh.session is not old and sh.broker.connected():
            break
    assert sh.session is not old and sh.broker.reconnects == 1
    assert len(sh.feed.subs) >= 2  # the watched stream was reopened on the new session
    moves = iter(range(1000))

    def fresh():
        i = next(moves) % 2
        sh.live(88.40, 88.42 + i / 100)
        sh.live(88.40 - i / 100, 88.42)

    run_until_processed(sh, 2, before=fresh, cycles=400)
    [a] = [a for a in sh.actions() if a["action"] == "OPEN"]
    assert a["status"] == "EXECUTED", (a["status"], a["reasons"])
    assert any(e["event"] == "BROKER_RECONNECTED" for e in sh.service.store.events())
    sh.close()
    assert_only_read_only_messages(tws)


# ------------------------------------------------------------------ start-up refusals
def _refused(tmp_path, tws, mono, match, **kw):  # noqa: F811
    with pytest.raises(ConfigError, match=match):
        Shadow(tmp_path, tws, mono, **kw)
    assert not [c for c in tws.connections if c.isConnected()]  # nothing left connected


def test_start_refused_on_an_identity_mismatch(tmp_path, tws, mono):  # noqa: F811
    tws.managed_accounts = OTHER_PAPER
    _refused(tmp_path, tws, mono, "AccountIdentityError")


def test_start_refused_when_read_only_is_not_verified(tmp_path, tws, mono):  # noqa: F811
    tws.api_settings = {**tws.api_settings, "readOnlyApi": False}
    _refused(tmp_path, tws, mono, "ReadOnlySettingRefused")


def test_start_refused_when_no_listener_is_found(tmp_path, tws, mono):  # noqa: F811
    def no_listener(cfg):
        raise ListenerNotConfirmed("nothing listens on the port", reason="NO_LISTENER")

    _refused(tmp_path, tws, mono, "ListenerNotConfirmed",
             factory=lambda cfg: fast_session(cfg, preflight=no_listener))
    assert tws.connections == []  # refused before any socket


def test_start_refused_for_any_session_class_but_the_read_only_one(tmp_path, tws, mono):  # noqa: F811
    class AnotherSession(IbkrSession):
        pass

    _refused(tmp_path, tws, mono, "only the read-only IbkrSession",
             factory=lambda cfg: fast_session(cfg, cls=AnotherSession))
    assert tws.connections == []


def test_start_refused_when_the_paper_config_is_unsafe(tmp_path, tws, mono, monkeypatch):  # noqa: F811
    original = paper_config_file

    def loose(folder):
        p = original(folder)
        os.chmod(p, 0o644)
        return p

    monkeypatch.setattr(sys.modules[__name__], "paper_config_file", loose)
    _refused(tmp_path, tws, mono, "ConfigPermissionsTooOpen")


# ------------------------------------------------------------------ streams
def _multi_symbol(server: FakeTws) -> None:
    def handler(s, c, r):
        sym = r.contract.symbol
        c.push(contract_frame(r.reqId, con_id=1000 + sum(map(ord, sym)), symbol=sym,
                              sec_type="STK", exchange="SMART", min_tick="0.01"))
        c.push(contract_end_frame(r.reqId))

    server.on["REQ_CONTRACT_DATA"] = handler


def test_streams_are_reused_and_the_least_recently_read_is_closed_at_the_cap(shadow, tws):  # noqa: F811
    from tradingbot.models import AssetClass, Instrument

    _multi_symbol(tws)
    sh = shadow(factory=lambda cfg: fast_session(cfg, cap=2))
    b = sh.broker
    assert len(sh.feed.subs) == 1  # TLT, opened at start
    qa, qb = (b.resolve(Instrument(asset_class=AssetClass.ETF, symbol=s)) for s in ("QAA", "QBB"))
    first = b.quote(qa)  # a contract with no stream: opened, and unavailable for this read
    assert first.quality.value == "UNAVAILABLE"
    assert "NO_STREAM_YET" in b.last_evidence[qa.contract_key].problems
    assert len(sh.feed.subs) == 2 and len(sh.session.quote_streams()) == 2
    b.quote(qa)
    assert len(sh.feed.subs) == 2  # reused: no new subscription
    tlt_rid = sh.feed.rid(0)
    b.quote(qb)  # the cap is 2: the least recently read stream (TLT) is closed first
    cancelled = [r.reqId for n, r in tws.requests if n == "CANCEL_MKT_DATA"]
    assert cancelled == [tlt_rid] and len(sh.session.quote_streams()) == 2
    assert [s["contract_key"].split(":")[1] for s in b.status()["streams"]] == ["QAA", "QBB"]


def test_shutdown_closes_every_stream_and_disconnects(shadow, tws):  # noqa: F811
    sh = shadow()
    session = sh.session
    rid = sh.feed.rid(0)
    sh.step()
    sh.close()
    assert [r.reqId for n, r in tws.requests if n == "CANCEL_MKT_DATA"] == [rid]
    assert session.state.value == "DISCONNECTED" and session.threads_alive() == []
    assert sh.events("SHADOW_SESSION_CLOSED") == [{"cleanup_failures": []}]
    assert_only_read_only_messages(tws)


def test_the_paper_account_is_never_the_runtime_account(shadow):
    sh = shadow()
    assert sh.broker.account_id() == SHADOW_ACCOUNT != PAPER
    ident = sh.broker.session_identity()
    assert ident["account"] == SHADOW_ACCOUNT and PAPER not in str(ident)
    hb = sh.service.store.get("heartbeat")
    assert hb is None or PAPER not in str(hb)
    sh.step()
    assert PAPER not in str(sh.service.store.get("heartbeat"))
    assert PAPER not in str(sh.service.store.events())


def test_the_shadow_broker_cannot_be_given_an_invented_market(shadow):
    sh = shadow()
    for name in ("set_quote", "fill", "script_next_submit", "expire_day_orders"):
        assert not hasattr(sh.broker, name), name


def test_quotes_published_to_the_simulator_never_outlive_the_real_quote(shadow):
    """A stream that stops being usable publishes UNAVAILABLE: no fill on an older quote."""
    sh = shadow()
    sh.live(88.40, 88.42)
    contract = next(iter(sh.broker._by_query.values()))
    assert sh.broker.quote(contract).quality.value == "LIVE"
    sh.tick(-1, error_frame(sh.rid(), 10090, "Part of requested market data is not subscribed"))
    assert sh.broker.quote(contract).quality.value == "UNAVAILABLE"
    with sh.sim()._tx() as c:
        row = sh.sim()._quote_for(c, contract)
    assert row["quality"] == "UNAVAILABLE" and row["bid"] is None and row["ask"] is None


# ------------------------------------------------------------------ the Execution page
def _toml(raw: dict) -> str:
    def v(x):
        return f'"{x}"' if isinstance(x, str) else str(x).lower() if isinstance(x, bool) else x

    lines = [f"mode = {v(raw['mode'])}"]
    for table, values in raw.items():
        if isinstance(values, dict):
            lines.append(f"[{table}]")
            lines += [f"{k} = {v(x)}" for k, x in values.items()]
    return "\n".join(lines) + "\n"


def test_the_execution_page_labels_everything_simulated_and_shows_the_quotes(
        shadow, tmp_path, monkeypatch):
    import re

    from streamlit.testing.v1 import AppTest
    from test_execution_page import PAGE, shown

    sh = shadow([post("1", 0, ANCHORED)])
    moves = iter(range(1000))

    def fresh():
        i = next(moves) % 2
        sh.live(88.40, 88.42 + i / 100)
        sh.live(88.40 - i / 100, 88.42)

    run_until_processed(sh, 1, before=fresh)
    fresh()
    sh.step()
    cfg = tmp_path / "runtime.toml"
    cfg.write_text(_toml(sh.raw))
    monkeypatch.setenv("TRADINGBOT_RUNTIME_CONFIG", str(cfg))
    at = AppTest.from_file(str(PAGE), default_timeout=60)
    at.run()
    assert not at.exception
    text = shown(at)
    for needle in ("SHADOW MODE", "SIMULATED at real quotes", "Quotes each decision was made on",
                   "REPORTED_LIVE", "Read-only quote session** READY", "Simulated fills",
                   "88.42", "SIMULATED_AT_LIVE_QUOTE", "simulated broker"):
        assert needle in text, needle
    assert "MOCK" not in text and PAPER not in text
    # "broker" appears only as the simulated broker or to say nothing reached any broker.
    rest = re.sub(r"simulated broker|any broker|only broker connection|BROKER_[A-Z_]+", "",
                  text, flags=re.IGNORECASE)
    assert "broker" not in rest.lower(), [ln for ln in rest.splitlines() if "broker" in ln.lower()]
