"""Shadow broker: SIMULATED orders and fills at REAL read-only quotes. No order capability.

``ShadowBroker`` satisfies ``broker.Broker`` by composition:

* **Simulated** (the strict ``SimBroker``, unchanged): orders, fills, positions, open orders,
  executions, completed orders and the account (a simulated ``SIM-`` account). It declares
  ``BrokerEnvironment.SIM``. Nothing is ever sent to a broker for any of these.
* **Real, read-only**: contract resolution and quotes, from ONE ``tradingbot.ibkr.session.
  IbkrSession`` (preflight, identity gate and Read-Only verified from TWS's own settings, a
  deny-by-default outgoing allowlist). Quotes come from held streams (``open_quote_stream``,
  market-data type 3: live when entitled, honestly labelled delayed otherwise), mapped by
  ``shadow_quotes.map_snapshot``.

**Streams.** One stream per contract, opened on demand and reused, at most the session's
``max_quote_streams`` at once; when the cap is reached the least recently read stream is closed
first. A contract with no stream yet does NOT block: the stream is opened and the read returns
whatever has arrived at that instant (normally nothing, so the quote is UNAVAILABLE and an entry
decided on it HOLDs); later reads see the stream's evidence. ``prewarm`` opens the streams of the
watched instruments at start and after a reconnect, so a post does not meet a cold stream. A
failed stream (request error, connectivity interruption) or a closed one is dropped: that read is
UNAVAILABLE, a new stream is opened at once (no wait) and a dropped watched stream is reopened
at the next cycle's refresh (the adapter's bad news is sticky per
subscription, so a reopened stream must earn LIVE again from its own evidence).

**Nothing invented or carried forward.** Every quote read is published to the simulator (so its
matching engine fills working orders at the real quote with its existing rules); an unusable
quote is published as UNAVAILABLE with no sides, so the simulator never fills on an older one.
Before a submit, amendment or cancel, the contract's quote is read again and published.

**Time anchor.** ``set_reference_time(t)``: every quote read until the next call is stamped as if
read at ``min(t, clock())``. The service sets it at the start of each cycle and to each post's
decision time, so no gateway or execution-core ``now`` is earlier than a quote's stamp and the age
it measures is at least the adapter's own monotonic age (see ``shadow_quotes``).

**Session loss.** ``connected()`` is False once the session is not READY, is poisoned, or a held
stream was closed by connection loss, poison or disconnect. The runtime service then applies its
existing policy: BROKER_RECONNECT_PENDING (every OPEN and ADD refused), alerts, ``reconnect()``
on its backoff. ``reconnect()`` opens a NEW session (sessions are single-use) with every check
again; an identity, paper-environment or read-only failure raises ``BrokerIdentityError`` (the
service locks itself out and engages the halt), anything else ``BrokerDisconnected``.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from tradingbot.execution.broker import (
    BrokerDisconnected,
    BrokerEnvironment,
    BrokerError,
    BrokerIdentityError,
    BrokerOrder,
    BrokerPosition,
    BrokerQuote,
    ContractRef,
    Fill,
    OrderIntent,
    QuoteQuality,
)
from tradingbot.execution.shadow_quotes import (
    QuoteEvidence,
    map_snapshot,
    unavailable_evidence,
    unavailable_quote,
)
from tradingbot.execution.sim_broker import SimBroker
from tradingbot.ibkr.config import PaperConnectionConfig
from tradingbot.ibkr.config import load_config as load_paper_config
from tradingbot.ibkr.errors import (
    AccountIdentityError,
    CleanupError,
    IbkrError,
    ListenerNotConfirmed,
    PaperModeNotConfirmed,
    QuoteStreamClosed,
    ReadOnlyNotVerified,
    ReadOnlyViolation,
    UnsupportedServerVersion,
)
from tradingbot.ibkr.errors import ConfigError as PaperConfigError
from tradingbot.ibkr.models import REASON_STREAM_FAILED, ContractQuery, ResolvedContract
from tradingbot.ibkr.redact import redact_accounts
from tradingbot.models import AssetClass, Instrument

SHADOW_MARKET_DATA_TYPE = 3  # live when entitled, delayed (and labelled so) otherwise
SHADOW_SESSION = "SHADOW_SIMULATED_FILLS"
FILL_LABEL = "SIMULATED_AT_REAL_QUOTE"
# Stream close reasons that mean the session itself is gone.
SESSION_LOST_REASONS = frozenset({"CONNECTION_LOST", "SESSION_POISONED", "SESSION_DISCONNECTED"})
SHADOW_ASSET_CLASSES = (AssetClass.EQUITY, AssetClass.ETF)


class ShadowStartRefused(BrokerError):
    """The read-only session could not be opened as the approved paper environment."""


def _safe(exc: BaseException) -> str:
    return redact_accounts(f"{type(exc).__name__}: {exc}")[:300]


def _identity_failure(exc: BaseException) -> bool:
    """Failures that mean "not positively the approved paper environment" (lockout), as opposed to
    "not reachable right now" (retry). TWS not running at all (no listener) is the latter."""
    if isinstance(exc, ListenerNotConfirmed):
        return getattr(exc, "reason", None) != "NO_LISTENER"
    return isinstance(exc, (AccountIdentityError, ReadOnlyNotVerified, PaperModeNotConfirmed,
                            UnsupportedServerVersion, PaperConfigError, ReadOnlyViolation))


def default_session_factory(config: PaperConnectionConfig) -> Any:
    from tradingbot.ibkr.session import IbkrSession  # imports ibapi

    return IbkrSession(config)


def open_read_only_session(config: PaperConnectionConfig,
                           factory: Callable[[PaperConnectionConfig], Any] | None = None) -> Any:
    """A connected READ-ONLY session: exactly ``IbkrSession`` (never a subclass, so never an
    order-capable session class), connected through every check, Read-Only VERIFIED."""
    from tradingbot.ibkr.session import IbkrSession  # imports ibapi

    session = (factory or default_session_factory)(config)
    if type(session) is not IbkrSession:
        raise ReadOnlyNotVerified(
            f"shadow mode accepts only the read-only IbkrSession, not {type(session).__name__}")
    session.connect()  # preflight -> handshake -> version -> identity gate -> read-only -> READY
    if session.read_only_status != "VERIFIED" or session.state.value != "READY":
        try:
            session.disconnect()
        finally:
            raise ReadOnlyNotVerified("the session did not verify Read-Only before READY")
    return session


class ShadowBroker:
    """See the module docstring. ``connect_session`` returns a NEW connected read-only session;
    it is called by ``reconnect``."""

    def __init__(self, sim: SimBroker, session: Any, *,
                 connect_session: Callable[[], Any], clock: Callable[[], datetime] | None = None,
                 watch: Sequence[Instrument] = ()):
        if not sim.strict:
            raise ValueError("the shadow broker simulates with the strict SimBroker only")
        self._sim = sim
        self._session = session
        self._connect_session = connect_session
        self.clock = clock or (lambda: datetime.now(UTC))
        self.watch = tuple(watch)
        self._streams: OrderedDict[str, Any] = OrderedDict()  # contract_key -> QuoteStream (LRU)
        self._resolved: dict[str, ResolvedContract] = {}
        self._by_query: dict[tuple[str, str, str], ContractRef] = {}
        self._reference: datetime | None = None
        self._lost = False
        self._quote_account = session.masked_account
        self.reconnects = 0
        self.cleanup_failures: list[str] = []
        self.last_evidence: dict[str, QuoteEvidence] = {}
        self._reads: list[QuoteEvidence] = []
        self._fills: list[dict] = []
        self._seen_execs = {f.exec_id for f in sim.executions()}

    # ------------------------------------------------------------------ shadow-specific controls
    def set_reference_time(self, at: datetime | None) -> None:
        self._reference = at

    def take_quote_log(self) -> list[dict]:
        out, self._reads = [e.as_dict() for e in self._reads], []
        return out

    def take_fill_log(self) -> list[dict]:
        out, self._fills = self._fills, []
        return out

    def status(self) -> dict:
        s = self._session
        return {
            "fills": FILL_LABEL,
            "quote_session": "LOST" if self._lost or s is None
            else ("POISONED" if s.poisoned else s.state.value),
            "quote_account": self._quote_account,
            "market_data_type_requested": SHADOW_MARKET_DATA_TYPE,
            "stream_cap": getattr(s, "max_quote_streams", None),
            "reconnects": self.reconnects,
            "streams": [
                {"contract_key": k,
                 "label": self.last_evidence[k].label if k in self.last_evidence else None,
                 "reason": self.last_evidence[k].reason if k in self.last_evidence else None}
                for k in self._streams],
        }

    def prewarm(self) -> list[str]:
        """Resolve each watched instrument and open its stream. Returns the problems."""
        problems = []
        for instrument in self.watch:
            try:
                contract = self.resolve(instrument)
            except BrokerError as exc:
                problems.append(f"{instrument.symbol}: {_safe(exc)}")
                continue
            if self._ensure_stream(contract.contract_key) is None:
                problems.append(f"{instrument.symbol}: stream not opened")
        return problems

    def close(self) -> list[str]:
        """Close every held stream, then disconnect. Returns the cleanup failures."""
        failures: list[str] = []
        for key in list(self._streams):
            failures += self._close_stream(key)
        s, self._session = self._session, None
        if s is not None:
            try:
                s.disconnect()
            except CleanupError as exc:
                failures += list(exc.failures) or [_safe(exc)]
            except IbkrError as exc:
                failures.append(_safe(exc))
        self.cleanup_failures += failures
        return failures

    # ------------------------------------------------------------------ session (additive)
    def connected(self) -> bool:
        s = self._session
        if self._lost or s is None or s.poisoned or s.state.value != "READY":
            return False
        for stream in self._streams.values():
            if stream.closed and stream.closed_reason in SESSION_LOST_REASONS:
                self._lost = True
                return False
        return True

    def reconnect(self) -> None:
        self.close()
        try:
            session = self._connect_session()
        except BaseException as exc:
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            if _identity_failure(exc):
                raise BrokerIdentityError(f"shadow quote session refused: {_safe(exc)}") from None
            raise BrokerDisconnected(f"shadow quote session not reopened: {_safe(exc)}") from None
        self._session, self._lost = session, False
        self._streams.clear()
        self.reconnects += 1
        self.prewarm()

    def session_identity(self) -> dict[str, str]:
        ident = self._sim.session_identity()
        return {"account": ident["account"], "session": SHADOW_SESSION,
                "quote_session": f"IBKR_PAPER_READ_ONLY:{self._quote_account}"}

    def environment(self) -> BrokerEnvironment:
        return BrokerEnvironment.SIM

    # ------------------------------------------------------------------ real, read-only
    def resolve(self, instrument: Instrument) -> ContractRef:
        if instrument.asset_class not in SHADOW_ASSET_CLASSES:
            raise BrokerError(f"shadow mode resolves stocks and ETFs only, not "
                              f"{instrument.asset_class.value}")
        exchange = instrument.exchange or "SMART"
        key = (instrument.symbol, exchange, instrument.asset_class.value)
        if key in self._by_query:
            return self._by_query[key]
        rc = self._resolve_query(ContractQuery(symbol=instrument.symbol, sec_type="STK",
                                               exchange=exchange, currency="USD"))
        tick = Decimal(str(rc.min_tick))
        if not tick.is_finite() or tick <= 0:
            raise BrokerError(f"shadow: {instrument.symbol} has no usable minimum tick")
        contract = ContractRef(
            symbol=rc.symbol, asset_class=instrument.asset_class, sec_type="STK",
            exchange=rc.exchange or exchange, currency=rc.currency,
            primary_exchange=rc.primary_exchange or None, con_id=rc.con_id,
            local_symbol=rc.local_symbol or None, min_tick=tick)
        self._resolved[contract.contract_key] = rc
        self._by_query[key] = contract
        return contract

    def _resolve_query(self, query: ContractQuery) -> ResolvedContract:
        if not self.connected():
            raise BrokerError("shadow: the read-only session is not ready")
        try:
            return self._session.resolve_contract(query)
        except IbkrError as exc:
            raise BrokerError(f"shadow: contract not resolved: {_safe(exc)}") from None

    def _resolved_for(self, contract: ContractRef) -> ResolvedContract | None:
        rc = self._resolved.get(contract.contract_key)
        if rc is not None or contract.sec_type != "STK" or contract.con_id is None:
            return rc
        try:  # a contract recorded before this process started: resolve it again, same conId
            rc = self._resolve_query(ContractQuery(
                symbol=contract.symbol, sec_type="STK", exchange=contract.exchange,
                currency=contract.currency, primary_exchange=contract.primary_exchange or ""))
        except BrokerError:
            return None
        if rc.con_id != contract.con_id:
            return None
        self._resolved[contract.contract_key] = rc
        return rc

    def _ensure_stream(self, key: str) -> Any | None:
        stream = self._streams.get(key)
        if stream is not None:
            self._streams.move_to_end(key)
            return stream
        rc = self._resolved.get(key)
        if rc is None:
            return None
        cap = self._session.max_quote_streams
        while len(self._streams) >= cap:
            self._close_stream(next(iter(self._streams)))  # least recently read first
        try:
            stream = self._session.open_quote_stream(rc, market_data_type=SHADOW_MARKET_DATA_TYPE)
        except IbkrError:
            return None
        self._streams[key] = stream
        return stream

    def _close_stream(self, key: str) -> list[str]:
        stream = self._streams.pop(key, None)
        if stream is None or stream.closed:
            return []
        try:
            stream.close()
        except CleanupError as exc:
            failures = list(exc.failures) or [_safe(exc)]
            self.cleanup_failures += failures
            return failures
        except IbkrError as exc:
            self.cleanup_failures.append(_safe(exc))
            return [_safe(exc)]
        return []

    def _anchor(self) -> datetime:
        now = self.clock()
        return now if self._reference is None else min(now, self._reference)

    def _read(self, contract: ContractRef) -> tuple[BrokerQuote, QuoteEvidence]:
        at = self._anchor()  # taken BEFORE the snapshot: any lag only ages the sides
        if not self.connected():
            return unavailable_quote(contract, at), unavailable_evidence(
                contract, at, "SESSION_NOT_READY")
        if self._resolved_for(contract) is None:
            return unavailable_quote(contract, at), unavailable_evidence(
                contract, at, "CONTRACT_UNKNOWN")
        key = contract.contract_key
        fresh = key not in self._streams
        stream = self._ensure_stream(key)
        if stream is None:
            return unavailable_quote(contract, at), unavailable_evidence(
                contract, at, "STREAM_UNAVAILABLE")
        try:
            snapshot = stream.latest()
        except QuoteStreamClosed:
            self._streams.pop(key, None)
            reason = stream.closed_reason or "CLOSED"
            if reason in SESSION_LOST_REASONS:
                self._lost = True
            return unavailable_quote(contract, at), unavailable_evidence(
                contract, at, "STREAM_CLOSED", reason)
        quote, evidence = map_snapshot(snapshot, contract, at=at)
        if snapshot.quality_reason == REASON_STREAM_FAILED:
            self._close_stream(key)
            self._ensure_stream(key)  # a new subscription earns its own evidence from now on
            evidence = QuoteEvidence(**{**evidence.__dict__, "problems": (
                *evidence.problems, "STREAM_FAILED")})
        elif fresh and quote.quality is QuoteQuality.UNAVAILABLE:
            evidence = QuoteEvidence(**{**evidence.__dict__, "problems": (
                *evidence.problems, "NO_STREAM_YET")})
        return quote, evidence

    def quote(self, contract: ContractRef) -> BrokerQuote:
        self._sim.account_id()  # the simulated session must be up as well
        quote, evidence = self._read(contract)
        self._reads.append(evidence)
        self.last_evidence[contract.contract_key] = evidence
        self._publish(quote)
        self._note_fills(evidence)
        return quote

    def reference_quote(self, contract: ContractRef) -> BrokerQuote:
        return self.quote(contract)

    def _publish(self, q: BrokerQuote) -> None:
        """To the simulator's matching engine: the real quote, or UNAVAILABLE with no sides."""
        usable = q.bid is not None and q.ask is not None and q.quality != QuoteQuality.UNAVAILABLE
        self._sim.set_quote(
            q.contract.symbol, q.bid if usable else None, q.ask if usable else None,
            last=q.last if usable else None,
            quality=q.quality if usable else QuoteQuality.UNAVAILABLE,
            received_at=q.received_at, contract=q.contract, updated_at=q.updated_at,
            bid_at=q.bid_at, ask_at=q.ask_at)

    def _note_fills(self, evidence: QuoteEvidence | None = None) -> None:
        for f in self._sim.executions():
            if f.exec_id in self._seen_execs:
                continue
            self._seen_execs.add(f.exec_id)
            ev = self.last_evidence.get(f.contract.contract_key) or evidence
            self._fills.append({
                "label": FILL_LABEL, "exec_id": f.exec_id, "order_ref": f.order_ref,
                "contract_key": f.contract.contract_key, "side": f.side.value,
                "quantity": f.quantity, "price": str(f.price),
                "executed_at": f.executed_at.isoformat(),
                "quote": ev.as_dict() if ev is not None else None})

    def refresh_quotes(self) -> int:
        """Read (and publish) the quote of every contract with a working order, a price
        condition or a position, so simulated stops and targets see the real market."""
        contracts: dict[str, ContractRef] = {}
        for o in self._sim.open_orders():
            contracts.setdefault(o.contract.contract_key, o.contract)
            if o.condition is not None:
                contracts.setdefault(o.condition.contract.contract_key, o.condition.contract)
        for p in self._sim.positions():
            contracts.setdefault(p.contract.contract_key, p.contract)
        for c in contracts.values():
            self.quote(c)
        if self.connected():  # a watched stream that was dropped is reopened (no read)
            for contract in self._by_query.values():
                if contract.contract_key not in self._streams and any(
                        (i.symbol, i.exchange or "SMART") == (contract.symbol, contract.exchange)
                        for i in self.watch):
                    self._ensure_stream(contract.contract_key)
        return len(contracts)

    def _refresh_ref(self, order_ref: str) -> None:
        orders = self._sim.orders_with_ref(order_ref)
        if orders:
            self.quote(orders[-1].contract)

    # ------------------------------------------------------------------ simulated (SimBroker)
    def account_id(self) -> str:
        return self._sim.account_id()

    def submit(self, order: OrderIntent, children: Sequence[OrderIntent] = ()) -> list[BrokerOrder]:
        self.quote(order.contract)
        out = self._sim.submit(order, children)
        self._note_fills()
        return out

    def modify(self, order_ref: str, *, limit_price: Decimal | None = None,
               stop_price: Decimal | None = None, quantity: int | None = None) -> BrokerOrder:
        self._refresh_ref(order_ref)
        out = self._sim.modify(order_ref, limit_price=limit_price, stop_price=stop_price,
                               quantity=quantity)
        self._note_fills()
        return out

    def cancel(self, order_ref: str) -> BrokerOrder:
        self._refresh_ref(order_ref)
        out = self._sim.cancel(order_ref)
        self._note_fills()
        return out

    def supports_price_conditions(self) -> bool:
        return self._sim.supports_price_conditions()

    def modify_condition(self, order_ref: str, *, trigger_price: Decimal) -> BrokerOrder:
        self._refresh_ref(order_ref)
        out = self._sim.modify_condition(order_ref, trigger_price=trigger_price)
        self._note_fills()
        return out

    def open_orders(self) -> list[BrokerOrder]:
        return self._sim.open_orders()

    def executions(self, since: datetime | None = None) -> list[Fill]:
        return self._sim.executions(since)

    def completed_orders(self, since: datetime | None = None) -> list[BrokerOrder]:
        return self._sim.completed_orders(since)

    def positions(self) -> list[BrokerPosition]:
        return self._sim.positions()


def build_shadow_broker(*, sim_db: str | Path, account: str, paper_config: str | Path,
                        clock: Callable[[], datetime] | None = None,
                        watch: Sequence[Instrument] = (),
                        session_factory: Callable[[PaperConnectionConfig], Any] | None = None,
                        ) -> ShadowBroker:
    """Load the private paper config exactly as the paper check does, connect (every check), and
    build the broker. Raises ``ShadowStartRefused`` on any failure; nothing is left connected."""
    if not account.startswith("SIM-"):
        raise ShadowStartRefused("the shadow account must be a simulated SIM- account")
    try:
        cfg = load_paper_config(paper_config)

        def connect() -> Any:
            return open_read_only_session(cfg, session_factory)

        session = connect()
    except BaseException as exc:
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        raise ShadowStartRefused(f"read-only paper session refused: {_safe(exc)}") from None
    broker = ShadowBroker(SimBroker(sim_db, account=account, clock=clock), session,
                          connect_session=connect, clock=clock, watch=watch)
    problems = broker.prewarm()
    if problems:
        broker.close()
        raise ShadowStartRefused("watched instrument(s) not streamable: " + "; ".join(problems))
    return broker
