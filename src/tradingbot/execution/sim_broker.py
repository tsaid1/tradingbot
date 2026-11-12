"""A deterministic simulated broker with durable state (SQLite), for the mock lifecycle and tests.

It implements ``broker.Broker`` the way an IBKR paper account behaves in the cases that matter to
the execution core: brackets whose children wait (PreSubmitted) until the parent fills,
one-cancels-other children, stop and limit triggers on later quotes, and order refs echoed on every
row. ``strict=False`` keeps the older, kinder model (children sized to the parent's fills,
reduce-only orders clamped so they never reverse a position, instant cancels and modifies, full
execution history) for the SimBroker's own unit tests only.

Failure behaviours are scripted by the test (``script_next_submit`` / ``script_next_modify`` /
``script_next_cancel``); the script lives in memory, everything the broker "knows" lives in SQLite
and survives a restart. No network, no real broker, invented data only.

``strict=True`` (the default) removes the kindnesses an Interactive Brokers account does not offer (safety review
"SimBroker kinder than IBKR"): no reduce-only clamp (an oversized exit really reverses the
position), protective children keep the quantity they were submitted with (they neither grow with
the parent's fills nor shrink when a sibling partly fills; a sibling's complete fill still cancels
them, one-cancels-other; they are visible as ``PreSubmitted`` before the parent fills and, with
``children_stay_presubmitted``, stay ``PreSubmitted`` after it while working, as a stop held and
simulated broker-side does at IBKR), ``executions()`` covers the current (UTC) day only, and cancels and
modifies pass through a pending state (``PendingCancel`` / an unapplied amendment) that takes effect
only after ``pending_seconds`` and at the next broker interaction, during which the order can still
fill (``script_next_cancel("FILL_FIRST")`` / ``script_next_modify("FILL_FIRST")`` make a fill win
the race deterministically). ``open_orders_lag_seconds`` can additionally hide fresh orders.
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections import deque
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import Enum
from pathlib import Path

from tradingbot.execution.broker import (
    TERMINAL_STATUSES,
    WORKING_STATUSES,
    AckLost,
    BrokerDisconnected,
    BrokerError,
    BrokerIdentityError,
    BrokerOrder,
    BrokerPosition,
    BrokerQuote,
    ContractRef,
    Fill,
    OrderIntent,
    OrderNotFound,
    OrderSide,
    OrderStatus,
    OrderType,
    PriceCondition,
    QuoteQuality,
    TimeInForce,
)
from tradingbot.models import AssetClass, Instrument

SCHEMA = """
CREATE TABLE IF NOT EXISTS sim_orders (
    broker_order_id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_ref TEXT NOT NULL,
    parent_ref TEXT,
    symbol TEXT NOT NULL,
    asset_class TEXT NOT NULL,
    con_id INTEGER,
    side TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    order_type TEXT NOT NULL,
    limit_price TEXT,
    stop_price TEXT,
    tif TEXT NOT NULL,
    reduce_only INTEGER NOT NULL,
    status TEXT NOT NULL,
    filled_quantity INTEGER NOT NULL DEFAULT 0,
    avg_fill_price TEXT,
    reason TEXT NOT NULL DEFAULT '',
    held INTEGER NOT NULL DEFAULT 0,
    placed_at TEXT,
    pending_action TEXT,
    pending_quantity INTEGER,
    pending_limit TEXT,
    pending_stop TEXT,
    pending_at TEXT,
    pending_race TEXT,
    activated INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS sim_orders_ref ON sim_orders(order_ref);
CREATE TABLE IF NOT EXISTS sim_executions (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    exec_id TEXT NOT NULL UNIQUE,
    broker_order_id INTEGER NOT NULL,
    order_ref TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    price TEXT NOT NULL,
    executed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sim_positions (
    symbol TEXT PRIMARY KEY,
    asset_class TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    avg_cost TEXT
);
CREATE TABLE IF NOT EXISTS sim_contract_positions (
    contract_key TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    contract_json TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    avg_cost TEXT
);
CREATE TABLE IF NOT EXISTS sim_contracts (
    contract_key TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    asset_class TEXT NOT NULL,
    month TEXT,
    contract_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sim_quotes (
    symbol TEXT PRIMARY KEY,
    bid TEXT, ask TEXT, last TEXT,
    received_at TEXT NOT NULL,
    quality TEXT NOT NULL,
    updated_at TEXT
);
"""


class SimClock:
    """A settable clock shared by the simulated broker and the coordinator in tests and demos."""

    def __init__(self, start: datetime):
        if start.tzinfo is None:
            raise ValueError("SimClock needs an aware datetime")
        self._now = start

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> datetime:
        self._now += timedelta(seconds=seconds)
        return self._now


class Behaviour(str, Enum):
    FILL = "FILL"  # accept; fill whatever is marketable now (default)
    REST = "REST"  # accept and keep working; no fill until ``fill()``
    PARTIAL = "PARTIAL"  # accept; fill ``quantity`` now, the rest keeps working
    REJECT = "REJECT"  # broker rejects the whole bracket
    LOST_ACK_PLACED = "LOST_ACK_PLACED"  # placed (and filled if marketable), then the ack is lost
    LOST_ACK_NOT_PLACED = "LOST_ACK_NOT_PLACED"  # the request never arrived; the ack is lost
    CRASH_AFTER_PLACE = (
        "CRASH_AFTER_PLACE"  # placed, then the calling process dies (SimulatedCrash)
    )
    DROP_STOP_CHILD = "DROP_STOP_CHILD"  # placed, but the protective stop child goes Inactive
    # The request arrived and the broker rejected (or cancelled) the bracket, then the ack is lost:
    # the order leaves no trace in open orders or executions, only in completed orders.
    LOST_ACK_REJECTED = "LOST_ACK_REJECTED"
    # Placed and resting (no fill), then the ack is lost: visible only through open orders.
    LOST_ACK_PLACED_RESTING = "LOST_ACK_PLACED_RESTING"
    LOST_ACK_CANCELLED = "LOST_ACK_CANCELLED"


@dataclass
class _Script:
    behaviour: Behaviour
    quantity: int | None = None
    reason: str = ""


class SimulatedCrash(BaseException):
    """Raised by CRASH_AFTER_PLACE: models the process dying after the broker accepted the order.
    A BaseException, like KeyboardInterrupt, so ordinary ``except Exception`` handlers do not
    swallow it and the caller's state is left exactly as a real crash would leave it."""


def _d(value: str | None) -> Decimal | None:
    return Decimal(value) if value is not None else None


def _s(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None


class SimBroker:
    def __init__(
        self,
        db_path: str | Path,
        *,
        account: str = "SIM-PAPER-0001",
        clock=None,
        strict: bool = True,
        pending_seconds: float = 0.0,
    ):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.account = account
        self.clock = clock or (lambda: datetime.now(UTC))
        self._submit_script: deque[_Script] = deque()
        self._modify_script: deque[str] = deque()
        self._cancel_script: deque[str] = deque()
        self.strict = strict
        # Strict mode: a cancel or modify takes effect this long after the request, and only at the
        # next broker interaction (0 = at the next interaction).
        self.pending_seconds = pending_seconds
        self.lookup_failures = 0  # next N open_orders()/executions() calls raise BrokerError
        # Test control: the next submit arms ``lookup_failures`` with this count, so the lookups
        # after an order fail while the pre-trade sync still succeeds.
        self.lookup_failures_after_submit = 0
        self.submit_calls = 0
        # IBKR realism, off by default: a freshly placed order is invisible to open_orders() for
        # this long, and executions / completed orders reach back only this far (IBKR: one day).
        self.open_orders_lag_seconds = 0.0
        self.history_seconds: float | None = None
        # Strict mode: a protective child keeps reporting PreSubmitted after its parent fills while
        # it works (IBKR holds and simulates many stops broker-side). Off by default.
        self.children_stay_presubmitted = False
        # Broker-side price conditions on a reference series (REFERENCE_TRIGGER families). A test
        # turns this off to model a broker that cannot place them.
        self.conditions_supported = True
        # Whether quotes carry the series' own last-update time (IBKR lastTimestamp). A test turns
        # this off to model a broker that reports only when it received the quote.
        self.reports_update_time = True
        # Session control (a Trader Workstation restarts daily): while disconnected every Broker
        # call raises BrokerDisconnected; the market and the account keep moving (test controls
        # such as fill, set_quote and expire_day_orders still act). See disconnect / reconnect.
        self._connected = True
        self.reconnect_failures = 0  # the next N reconnect() calls fail (bounded backoff tests)
        self.next_session_account: str | None = None  # the account the next session comes back as
        self.fail_identity_check = False  # the adapter's own identity check fails on reconnect
        self.identity_checks = 0
        with self._tx() as c:
            c.executescript(SCHEMA)
            cols = {r["name"] for r in c.execute("PRAGMA table_info(sim_orders)")}
            for col, kind in (
                ("placed_at", "TEXT"),
                ("pending_action", "TEXT"),
                ("pending_quantity", "INTEGER"),
                ("pending_limit", "TEXT"),
                ("pending_stop", "TEXT"),
                ("pending_at", "TEXT"),
                ("pending_race", "TEXT"),
                ("activated", "INTEGER NOT NULL DEFAULT 0"),
                ("contract_json", "TEXT"),
                # Multi-asset mapping: a broker-side price condition on a reference series.
                ("condition_json", "TEXT"),
                ("condition_released", "INTEGER NOT NULL DEFAULT 0"),
                ("pending_trigger", "TEXT"),
            ):
                if col not in cols:
                    c.execute(f"ALTER TABLE sim_orders ADD COLUMN {col} {kind}")
            qcols = {r["name"] for r in c.execute("PRAGMA table_info(sim_quotes)")}
            if "updated_at" not in qcols:  # the series' own last-update time (multi-asset)
                c.execute("ALTER TABLE sim_quotes ADD COLUMN updated_at TEXT")
            # A store written before positions were keyed by contract: move its rows once.
            for r in c.execute("SELECT * FROM sim_positions").fetchall():
                contract = self.resolve(
                    Instrument(asset_class=AssetClass(r["asset_class"]), symbol=r["symbol"])
                )
                c.execute(
                    "INSERT OR IGNORE INTO sim_contract_positions VALUES (?,?,?,?,?)",
                    (contract.contract_key, r["symbol"], contract.model_dump_json(),
                     r["quantity"], r["avg_cost"]),
                )
            c.execute("DELETE FROM sim_positions")

    # ------------------------------------------------------------------ plumbing

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA busy_timeout=10000")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _now(self) -> datetime:
        return self.clock()

    @staticmethod
    def _contract(row: sqlite3.Row) -> ContractRef:
        if "contract_json" in row.keys() and row["contract_json"]:  # noqa: SIM118 (sqlite3.Row)
            return ContractRef.model_validate_json(row["contract_json"])
        return ContractRef(
            symbol=row["symbol"], asset_class=AssetClass(row["asset_class"]), con_id=row["con_id"]
        )

    @staticmethod
    def _condition(row: sqlite3.Row) -> PriceCondition | None:
        raw = row["condition_json"] if "condition_json" in row.keys() else None  # noqa: SIM118
        return PriceCondition.model_validate_json(raw) if raw else None

    def _quote_for(self, c: sqlite3.Connection, contract: ContractRef):
        """A quote published for this exact contract, else (stocks and ETFs only) by symbol."""
        row = self._quote_row(c, contract.contract_key)
        if row is None and contract.sec_type == "STK":
            row = self._quote_row(c, contract.symbol)
        return row

    @staticmethod
    def _check_rules(item: OrderIntent | None, contract: ContractRef, quantity=None, *prices):
        """The contract's tick and size rules: a non-conforming order is refused, never rounded."""
        qty = item.quantity if item is not None else quantity
        if qty is not None and not contract.on_increment(qty):
            raise BrokerError(
                f"quantity {qty} is not a multiple of the size increment {contract.size_increment}"
            )
        if item is not None:
            prices = (item.limit_price, item.stop_price)
        for price in prices:
            if price is not None and not contract.on_tick(price):
                raise BrokerError(f"price {price} is not on the minimum tick {contract.min_tick}")

    def register_contract(self, contract: ContractRef) -> None:
        """Test control: make a derivative contract resolvable (by symbol and contract month)."""
        with self._tx() as c:
            c.execute(
                "INSERT OR REPLACE INTO sim_contracts VALUES (?,?,?,?,?)",
                (contract.contract_key, contract.symbol, contract.asset_class.value,
                 contract.expiry, contract.model_dump_json()),
            )

    def _order(self, row: sqlite3.Row) -> BrokerOrder:
        return BrokerOrder(
            broker_order_id=str(row["broker_order_id"]),
            order_ref=row["order_ref"],
            parent_ref=row["parent_ref"],
            contract=self._contract(row),
            side=OrderSide(row["side"]),
            quantity=row["quantity"],
            order_type=OrderType(row["order_type"]),
            limit_price=_d(row["limit_price"]),
            stop_price=_d(row["stop_price"]),
            tif=TimeInForce(row["tif"]),
            reduce_only=bool(row["reduce_only"]),
            condition=self._condition(row),
            status=OrderStatus(row["status"]),
            filled_quantity=row["filled_quantity"],
            avg_fill_price=_d(row["avg_fill_price"]),
            reason=row["reason"],
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    # ------------------------------------------------------------------ test control

    def script_next_submit(
        self,
        behaviour: Behaviour | str,
        *,
        quantity: int | None = None,
        reason: str = "SIMULATED_REJECT",
    ) -> None:
        self._submit_script.append(_Script(Behaviour(behaviour), quantity, reason))

    def script_next_modify(self, behaviour: str) -> None:
        """``LOST_ACK_APPLIED`` | ``LOST_ACK_NOT_APPLIED`` | ``REJECT`` | ``FILL_FIRST`` (strict:
        the order fills its remaining quantity before the amendment takes effect)"""
        self._modify_script.append(behaviour)

    def script_next_cancel(self, behaviour: str) -> None:
        """``FILL_FIRST`` (strict: the order fills its remaining quantity before the cancel takes
        effect, so the cancel loses the race) | ``REJECT`` (the cancel request fails)"""
        self._cancel_script.append(behaviour)

    def set_quote(
        self,
        symbol: str,
        bid: str | Decimal | None,
        ask: str | Decimal | None,
        *,
        last: str | Decimal | None = None,
        quality: QuoteQuality = QuoteQuality.LIVE,
        received_at: datetime | None = None,
        contract: ContractRef | None = None,
        updated_at: datetime | None = None,
    ) -> None:
        """Publish a quote and run every working order against it (stops, targets, limits). With
        ``contract`` the quote is for that exact contract; otherwise for the stock ``symbol``.
        ``updated_at`` is the series' own last-update time (default: the receipt time); a frozen
        series is modelled by re-publishing its value with an old ``updated_at``."""
        at = received_at or self._now()
        updated = updated_at or at
        symbol = contract.contract_key if contract is not None else symbol
        with self._tx() as c:
            self._settle(c)
            c.execute(
                """INSERT INTO sim_quotes (symbol,bid,ask,last,received_at,quality,updated_at)
                VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(symbol) DO UPDATE SET bid=excluded.bid, ask=excluded.ask,
                last=excluded.last, received_at=excluded.received_at, quality=excluded.quality,
                updated_at=excluded.updated_at""",
                (
                    symbol,
                    _s(Decimal(bid)) if bid is not None else None,
                    _s(Decimal(ask)) if ask is not None else None,
                    _s(Decimal(last)) if last is not None else None,
                    at.isoformat(),
                    quality.value,
                    updated.isoformat(),
                ),
            )
            self._match(c)

    def fill(self, order_ref: str, quantity: int | None = None, price: str | Decimal | None = None):
        """Fill a working order now (a resting or partially filled order completing later)."""
        with self._tx() as c:
            self._settle(c)
            row = self._working_row(c, order_ref)
            qty = quantity or (row["quantity"] - row["filled_quantity"])
            if price is None:
                q = self._quote_for(c, self._contract(row))
                price = Decimal(q["ask"] if row["side"] == "BUY" else q["bid"])
            self._execute(c, row["broker_order_id"], qty, Decimal(price))
            self._match(c)

    def expire_day_orders(self) -> int:
        """Test control: the session ends and every working DAY order expires (IBKR reports it
        cancelled). Positions are unchanged. Returns the number expired."""
        placeholders = ",".join("?" * len(WORKING_STATUSES))
        with self._tx() as c:
            self._settle(c)
            cur = c.execute(
                f"UPDATE sim_orders SET status='Cancelled', reason='DAY_ORDER_EXPIRED', "
                f"updated_at=? WHERE tif='DAY' AND status IN ({placeholders})",
                [self._now().isoformat(), *[s.value for s in WORKING_STATUSES]],
            )
            return cur.rowcount

    def orders_with_ref(self, order_ref: str) -> list[BrokerOrder]:
        """Every order ever placed under ``order_ref``, any status (test inspection only)."""
        with self._tx() as c:
            rows = c.execute(
                "SELECT * FROM sim_orders WHERE order_ref=? ORDER BY broker_order_id", (order_ref,)
            ).fetchall()
        return [self._order(r) for r in rows]

    def all_orders(self) -> list[BrokerOrder]:
        with self._tx() as c:
            rows = c.execute("SELECT * FROM sim_orders ORDER BY broker_order_id").fetchall()
        return [self._order(r) for r in rows]

    # ------------------------------------------------------------------ session (additive)

    def _check_session(self) -> None:
        if not self._connected:
            raise BrokerDisconnected("simulated: broker session is down")

    def disconnect(self) -> None:
        """Test control: the session drops (as a Trader Workstation restart does). Orders already
        at the broker keep working and can fill, expire or be cancelled meanwhile."""
        self._connected = False

    def connected(self) -> bool:
        return self._connected

    def reconnect(self) -> None:
        """Re-establish the session and run the adapter's own identity checks again. Raises
        BrokerError while ``reconnect_failures`` remain and BrokerIdentityError when the identity
        check fails. ``next_session_account`` models a session that comes back under another
        account (the service must halt on it)."""
        if self.reconnect_failures > 0:
            self.reconnect_failures -= 1
            raise BrokerDisconnected("simulated: reconnect failed")
        self.identity_checks += 1
        if self.fail_identity_check:
            raise BrokerIdentityError("simulated: session identity check failed")
        if self.next_session_account is not None:
            self.account, self.next_session_account = self.next_session_account, None
        self._connected = True

    def session_identity(self) -> dict[str, str]:
        self._check_session()
        return {"account": self.account, "session": "SIMULATED_PAPER"}

    # ------------------------------------------------------------------ Broker protocol

    def account_id(self) -> str:
        self._check_session()
        return self.account

    def resolve(self, instrument: Instrument) -> ContractRef:
        if self is not None:  # tests call SimBroker.resolve(None, ...) for a stock's contract
            self._check_session()
        if instrument.asset_class not in (AssetClass.EQUITY, AssetClass.ETF):
            month = instrument.futures_contract or (
                instrument.expiry.replace("-", "") if instrument.expiry else None
            )
            with self._tx() as c:
                rows = c.execute(
                    "SELECT * FROM sim_contracts WHERE symbol=? AND asset_class=?",
                    (instrument.symbol, instrument.asset_class.value),
                ).fetchall()
            undated = (AssetClass.INDEX, AssetClass.COMMODITY, AssetClass.CFD)
            found = [
                ContractRef.model_validate_json(r["contract_json"])
                for r in rows
                if (month and (r["month"] or "").startswith(month))
                or (instrument.asset_class in undated and not month and not r["month"])
            ]
            if instrument.asset_class == AssetClass.OPTION:
                right = {"CALL": "C", "PUT": "P"}.get(instrument.option_right or "")
                strike = Decimal(instrument.strike) if instrument.strike else None
                found = [f for f in found if f.right == right and f.strike == strike]
            if len(found) != 1:
                raise BrokerError(
                    f"SimBroker: no single registered {instrument.asset_class.value} contract "
                    f"for {instrument.symbol} {month or '(no month)'}"
                )
            return found[0]
        con_id = int(hashlib.sha256(instrument.symbol.encode()).hexdigest()[:8], 16)
        return ContractRef(
            symbol=instrument.symbol, asset_class=instrument.asset_class, con_id=con_id
        )

    def quote(self, contract: ContractRef) -> BrokerQuote:
        self._check_session()
        with self._tx() as c:
            row = self._quote_for(c, contract)
        if row is None:
            return BrokerQuote(
                contract=contract, received_at=self._now(), quality=QuoteQuality.UNAVAILABLE
            )
        return BrokerQuote(
            contract=contract,
            bid=_d(row["bid"]),
            ask=_d(row["ask"]),
            last=_d(row["last"]),
            received_at=datetime.fromisoformat(row["received_at"]),
            quality=QuoteQuality(row["quality"]),
            updated_at=datetime.fromisoformat(row["updated_at"])
            if row["updated_at"] and self.reports_update_time
            else None,
        )

    # ------------------------------------------------------------------ conditional orders (additive)

    def supports_price_conditions(self) -> bool:
        return self.conditions_supported

    def reference_quote(self, contract: ContractRef) -> BrokerQuote:
        """The quote of a reference series (an index, a spot price or another contract), published
        with ``set_quote(..., contract=...)``. Price conditions are evaluated on these."""
        self._check_session()
        return self.quote(contract)

    def modify_condition(self, order_ref: str, *, trigger_price: Decimal) -> BrokerOrder:
        """Amend the trigger level of a working conditional order. Strict: the amendment is pending
        (the order keeps its old trigger) until the next broker interaction after
        ``pending_seconds``."""
        self._check_session()
        behaviour = self._modify_script.popleft() if self._modify_script else "APPLY"
        if behaviour == "LOST_ACK_NOT_APPLIED":
            raise AckLost("simulated: modify lost before reaching the broker")
        with self._tx() as c:
            self._settle(c)
            row = self._working_row(c, order_ref)
            if behaviour == "REJECT":
                raise BrokerError("simulated: modify rejected")
            cond = self._condition(row)
            if cond is None:
                raise BrokerError(f"order {order_ref} carries no price condition")
            if self.strict:
                if row["status"] == OrderStatus.PENDING_CANCEL.value:
                    raise BrokerError("simulated: cannot modify an order that is pending cancel")
                c.execute(
                    "UPDATE sim_orders SET pending_action='MODIFY', pending_trigger=?, "
                    "pending_at=?, pending_race=NULL WHERE broker_order_id=?",
                    (str(trigger_price), self._now().isoformat(), row["broker_order_id"]),
                )
            else:
                new = cond.model_copy(update={"trigger_price": trigger_price})
                c.execute(
                    "UPDATE sim_orders SET condition_json=?, updated_at=? WHERE broker_order_id=?",
                    (new.model_dump_json(), self._now().isoformat(), row["broker_order_id"]),
                )
                self._match(c)
            out = c.execute(
                "SELECT * FROM sim_orders WHERE broker_order_id=?", (row["broker_order_id"],)
            ).fetchone()
        if behaviour == "LOST_ACK_APPLIED":
            raise AckLost("simulated: modify accepted, acknowledgement lost")
        return self._order(out)

    def submit(self, order: OrderIntent, children: Sequence[OrderIntent] = ()) -> list[BrokerOrder]:
        self._check_session()
        self.submit_calls += 1
        if self.lookup_failures_after_submit:
            self.lookup_failures, self.lookup_failures_after_submit = (
                self.lookup_failures_after_submit, 0
            )
        script = self._submit_script.popleft() if self._submit_script else _Script(Behaviour.FILL)
        if any(ch.parent_ref != order.order_ref for ch in children):
            raise BrokerError("every child must name the entry as its parent")
        if order.parent_ref is not None:
            raise BrokerError("an entry cannot itself be a child")
        for item in (order, *children):
            self._check_rules(item, item.contract)
            if item.condition is not None and not self.conditions_supported:
                raise BrokerError("simulated: price conditions are not supported")
        if script.behaviour == Behaviour.LOST_ACK_NOT_PLACED:
            raise AckLost("simulated: request lost before reaching the broker")
        now = self._now().isoformat()
        with self._tx() as c:
            self._settle(c)
            ids = []
            for idx, item in enumerate([order, *children]):
                status = OrderStatus.PRE_SUBMITTED if item.parent_ref else OrderStatus.SUBMITTED
                reason = ""
                if script.behaviour in (Behaviour.REJECT, Behaviour.LOST_ACK_REJECTED):
                    status, reason = OrderStatus.REJECTED, script.reason
                if script.behaviour == Behaviour.LOST_ACK_CANCELLED:
                    status, reason = OrderStatus.CANCELLED, "SIMULATED_BROKER_CANCEL"
                if (
                    script.behaviour == Behaviour.DROP_STOP_CHILD
                    and idx > 0
                    and item.order_type == OrderType.STP
                ):
                    status, reason = OrderStatus.INACTIVE, "SIMULATED_CHILD_INACTIVE"
                held = int(
                    idx == 0
                    and script.behaviour
                    in (Behaviour.REST, Behaviour.PARTIAL, Behaviour.LOST_ACK_PLACED_RESTING)
                )
                cur = c.execute(
                    """INSERT INTO sim_orders (order_ref,parent_ref,symbol,asset_class,con_id,side,
                    quantity,
                    order_type,limit_price,stop_price,tif,reduce_only,status,reason,held,updated_at,
                    placed_at,contract_json,condition_json)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        item.order_ref,
                        item.parent_ref,
                        item.contract.symbol,
                        item.contract.asset_class.value,
                        item.contract.con_id,
                        item.side.value,
                        item.quantity,
                        item.order_type.value,
                        _s(item.limit_price),
                        _s(item.stop_price),
                        item.tif.value,
                        int(item.reduce_only),
                        status.value,
                        reason,
                        held,
                        now,
                        now,
                        item.contract.model_dump_json(),
                        item.condition.model_dump_json() if item.condition else None,
                    ),
                )
                ids.append(cur.lastrowid)
            if script.behaviour == Behaviour.PARTIAL:
                q = self._quote_for(c, order.contract)
                price = Decimal(q["ask"] if order.side == OrderSide.BUY else q["bid"])
                self._execute(c, ids[0], script.quantity or 1, price)
            if script.behaviour not in (
                Behaviour.REJECT,
                Behaviour.LOST_ACK_REJECTED,
                Behaviour.LOST_ACK_CANCELLED,
            ):
                self._match(c)
            rows = [
                c.execute("SELECT * FROM sim_orders WHERE broker_order_id=?", (i,)).fetchone()
                for i in ids
            ]
        if script.behaviour in (
            Behaviour.LOST_ACK_PLACED,
            Behaviour.LOST_ACK_PLACED_RESTING,
            Behaviour.LOST_ACK_REJECTED,
            Behaviour.LOST_ACK_CANCELLED,
        ):
            raise AckLost("simulated: order placed, acknowledgement lost")
        if script.behaviour == Behaviour.CRASH_AFTER_PLACE:
            raise SimulatedCrash("simulated: process died after the broker accepted the order")
        return [self._order(r) for r in rows]

    def modify(
        self,
        order_ref: str,
        *,
        limit_price: Decimal | None = None,
        stop_price: Decimal | None = None,
        quantity: int | None = None,
    ) -> BrokerOrder:
        self._check_session()
        behaviour = self._modify_script.popleft() if self._modify_script else "APPLY"
        if behaviour == "LOST_ACK_NOT_APPLIED":
            raise AckLost("simulated: modify lost before reaching the broker")
        with self._tx() as c:
            self._settle(c)
            row = self._working_row(c, order_ref)
            if behaviour == "REJECT":
                raise BrokerError("simulated: modify rejected")
            if quantity is not None and quantity < row["filled_quantity"]:
                raise BrokerError("cannot size an order below its filled quantity")
            remaining = None if quantity is None else quantity - row["filled_quantity"]
            self._check_rules(
                None, self._contract(row), remaining or None, limit_price, stop_price
            )
            if self.strict:
                if row["status"] == OrderStatus.PENDING_CANCEL.value:
                    raise BrokerError("simulated: cannot modify an order that is pending cancel")
                # The amendment is pending: the order keeps working on its old terms until then.
                c.execute(
                    """UPDATE sim_orders SET pending_action='MODIFY',
                    pending_quantity=COALESCE(?,pending_quantity),
                    pending_limit=COALESCE(?,pending_limit), pending_stop=COALESCE(?,pending_stop),
                    pending_at=?, pending_race=? WHERE broker_order_id=?""",
                    (
                        quantity,
                        _s(limit_price),
                        _s(stop_price),
                        self._now().isoformat(),
                        "FILL_FIRST" if behaviour == "FILL_FIRST" else None,
                        row["broker_order_id"],
                    ),
                )
                out = c.execute(
                    "SELECT * FROM sim_orders WHERE broker_order_id=?", (row["broker_order_id"],)
                ).fetchone()
                if behaviour == "LOST_ACK_APPLIED":
                    raise AckLost("simulated: modify accepted, acknowledgement lost")
                return self._order(out)
            c.execute(
                """UPDATE sim_orders SET limit_price=COALESCE(?,limit_price),
                stop_price=COALESCE(?,stop_price),
                quantity=COALESCE(?,quantity), updated_at=? WHERE broker_order_id=?""",
                (
                    _s(limit_price),
                    _s(stop_price),
                    quantity,
                    self._now().isoformat(),
                    row["broker_order_id"],
                ),
            )
            self._match(c)
            out = c.execute(
                "SELECT * FROM sim_orders WHERE broker_order_id=?", (row["broker_order_id"],)
            ).fetchone()
        if behaviour == "LOST_ACK_APPLIED":
            raise AckLost("simulated: modify applied, acknowledgement lost")
        return self._order(out)

    def cancel(self, order_ref: str) -> BrokerOrder:
        self._check_session()
        behaviour = self._cancel_script.popleft() if self._cancel_script else "APPLY"
        with self._tx() as c:
            self._settle(c)
            row = self._working_row(c, order_ref)
            if behaviour == "REJECT":
                raise BrokerError("simulated: cancel request failed")
            if self.strict:
                if row["status"] != OrderStatus.PENDING_CANCEL.value:
                    c.execute(
                        """UPDATE sim_orders SET status='PendingCancel', pending_action='CANCEL',
                        pending_at=?, pending_race=?, updated_at=? WHERE broker_order_id=?""",
                        (
                            self._now().isoformat(),
                            "FILL_FIRST" if behaviour == "FILL_FIRST" else None,
                            self._now().isoformat(),
                            row["broker_order_id"],
                        ),
                    )
                out = c.execute(
                    "SELECT * FROM sim_orders WHERE broker_order_id=?", (row["broker_order_id"],)
                ).fetchone()
                return self._order(out)
            self._cancel(c, row["broker_order_id"], "CANCELLED_BY_CLIENT")
            # Cancelling an entry cancels children that are still waiting on it.
            c.execute(
                """UPDATE sim_orders SET status='Cancelled', reason='PARENT_CANCELLED', updated_at=?
                WHERE parent_ref=? AND status='PreSubmitted' AND activated=0""",
                (self._now().isoformat(), order_ref),
            )
            out = c.execute(
                "SELECT * FROM sim_orders WHERE broker_order_id=?", (row["broker_order_id"],)
            ).fetchone()
        return self._order(out)

    def open_orders(self) -> list[BrokerOrder]:
        self._check_session()
        self._maybe_fail_lookup()
        placeholders = ",".join("?" * len(WORKING_STATUSES))
        with self._tx() as c:
            self._settle(c)
            rows = c.execute(
                f"SELECT * FROM sim_orders WHERE status IN ({placeholders}) "
                "ORDER BY broker_order_id",
                [s.value for s in WORKING_STATUSES],
            ).fetchall()
        if self.open_orders_lag_seconds:
            cutoff = self._now() - timedelta(seconds=self.open_orders_lag_seconds)
            rows = [r for r in rows if not r["placed_at"]
                    or datetime.fromisoformat(r["placed_at"]) <= cutoff]
        return [self._order(r) for r in rows]

    def _history_cutoff(self, since: datetime | None, *, day_only: bool = False) -> datetime | None:
        floors = []
        if self.history_seconds is not None:
            floors.append(self._now() - timedelta(seconds=self.history_seconds))
        if day_only:
            now = self._now().astimezone(UTC)
            floors.append(now.replace(hour=0, minute=0, second=0, microsecond=0))
        if not floors:
            return since
        floor = max(floors)
        return floor if since is None or since < floor else since

    def executions(self, since: datetime | None = None) -> list[Fill]:
        self._check_session()
        self._maybe_fail_lookup()
        with self._tx() as c:
            self._settle(c)
            rows = c.execute(
                """SELECT e.*, o.asset_class, o.con_id, o.contract_json FROM sim_executions e
                JOIN sim_orders o ON o.broker_order_id = e.broker_order_id ORDER BY e.seq"""
            ).fetchall()
        fills = [
            Fill(
                exec_id=r["exec_id"],
                broker_order_id=str(r["broker_order_id"]),
                order_ref=r["order_ref"],
                contract=self._contract(r),
                side=OrderSide(r["side"]),
                quantity=r["quantity"],
                price=Decimal(r["price"]),
                executed_at=datetime.fromisoformat(r["executed_at"]),
            )
            for r in rows
        ]
        # Strict: like IBKR reqExecutions, only the current day's executions are returned.
        since = self._history_cutoff(since, day_only=self.strict)
        return [f for f in fills if since is None or f.executed_at >= since]

    def completed_orders(self, since: datetime | None = None) -> list[BrokerOrder]:
        self._check_session()
        self._maybe_fail_lookup()
        placeholders = ",".join("?" * len(TERMINAL_STATUSES))
        with self._tx() as c:
            self._settle(c)
            rows = c.execute(
                f"SELECT * FROM sim_orders WHERE status IN ({placeholders}) "
                "ORDER BY broker_order_id",
                [s.value for s in TERMINAL_STATUSES],
            ).fetchall()
        orders = [self._order(r) for r in rows]
        since = self._history_cutoff(since)
        return [o for o in orders if since is None or o.updated_at >= since]

    def positions(self) -> list[BrokerPosition]:
        self._check_session()
        with self._tx() as c:
            self._settle(c)
            rows = c.execute(
                "SELECT * FROM sim_contract_positions WHERE quantity<>0 ORDER BY contract_key"
            ).fetchall()
        out = []
        for r in rows:
            contract = ContractRef.model_validate_json(r["contract_json"])
            out.append(
                BrokerPosition(
                    account=self.account,
                    contract=contract,
                    quantity=r["quantity"],
                    avg_cost=_d(r["avg_cost"]),
                )
            )
        return out

    # ------------------------------------------------------------------ matching engine

    def _maybe_fail_lookup(self) -> None:
        if self.lookup_failures > 0:
            self.lookup_failures -= 1
            raise BrokerError("simulated: broker lookup unavailable")

    @staticmethod
    def _quote_row(c: sqlite3.Connection, symbol: str):
        return c.execute("SELECT * FROM sim_quotes WHERE symbol=?", (symbol,)).fetchone()

    @staticmethod
    def _working_row(c: sqlite3.Connection, order_ref: str) -> sqlite3.Row:
        placeholders = ",".join("?" * len(WORKING_STATUSES))
        row = c.execute(
            f"SELECT * FROM sim_orders WHERE order_ref=? AND status IN ({placeholders}) "
            "ORDER BY broker_order_id DESC LIMIT 1",
            [order_ref, *[s.value for s in WORKING_STATUSES]],
        ).fetchone()
        if row is None:
            raise OrderNotFound(f"no working order with ref {order_ref}")
        return row

    def _cancel(self, c: sqlite3.Connection, broker_order_id: int, reason: str) -> None:
        c.execute(
            "UPDATE sim_orders SET status='Cancelled', reason=?, updated_at=? "
            "WHERE broker_order_id=?",
            (reason, self._now().isoformat(), broker_order_id),
        )

    def _settle(self, c: sqlite3.Connection) -> None:
        """Strict mode: apply every pending cancel or amendment whose time has come. A request
        scripted FILL_FIRST loses the race: the order fills its remaining quantity first."""
        if not self.strict:
            return
        now = self._now()
        rows = c.execute(
            "SELECT * FROM sim_orders WHERE pending_action IS NOT NULL ORDER BY broker_order_id"
        ).fetchall()
        changed = False
        for row in rows:
            if now < datetime.fromisoformat(row["pending_at"]) + timedelta(
                seconds=self.pending_seconds
            ):
                continue
            changed = True
            oid = row["broker_order_id"]
            if row["pending_race"] == "FILL_FIRST" and row["quantity"] > row["filled_quantity"]:
                # As if the market touched the order's own price just before the request landed.
                price = _d(row["limit_price"]) or _d(row["stop_price"])
                if price is None:
                    q = self._quote_for(c, self._contract(row))
                    price = Decimal(q["ask"] if row["side"] == "BUY" else q["bid"])
                self._execute(c, oid, row["quantity"] - row["filled_quantity"], price)
            cur = c.execute("SELECT * FROM sim_orders WHERE broker_order_id=?", (oid,)).fetchone()
            working = cur["status"] in {s.value for s in WORKING_STATUSES}
            if working and row["pending_action"] == "CANCEL":
                self._cancel(c, oid, "CANCELLED_BY_CLIENT")
                c.execute(
                    """UPDATE sim_orders SET status='Cancelled', reason='PARENT_CANCELLED',
                    updated_at=? WHERE parent_ref=? AND status='PreSubmitted' AND activated=0""",
                    (now.isoformat(), row["order_ref"]),
                )
            elif working and row["pending_action"] == "MODIFY":
                qty = row["pending_quantity"]
                if qty is None or qty >= cur["filled_quantity"]:
                    c.execute(
                        """UPDATE sim_orders SET limit_price=COALESCE(?,limit_price),
                        stop_price=COALESCE(?,stop_price), quantity=COALESCE(?,quantity),
                        updated_at=? WHERE broker_order_id=?""",
                        (row["pending_limit"], row["pending_stop"], qty, now.isoformat(), oid),
                    )
                    if qty is not None and qty == cur["filled_quantity"]:
                        c.execute(
                            "UPDATE sim_orders SET status='Filled' WHERE broker_order_id=?",
                            (oid,),
                        )
                    cond = self._condition(cur)
                    if row["pending_trigger"] is not None and cond is not None:
                        new = cond.model_copy(
                            update={"trigger_price": Decimal(row["pending_trigger"])}
                        )
                        c.execute(
                            "UPDATE sim_orders SET condition_json=? WHERE broker_order_id=?",
                            (new.model_dump_json(), oid),
                        )
            c.execute(
                """UPDATE sim_orders SET pending_action=NULL, pending_quantity=NULL,
                pending_limit=NULL, pending_stop=NULL, pending_at=NULL, pending_race=NULL,
                pending_trigger=NULL WHERE broker_order_id=?""",
                (oid,),
            )
        if changed:
            self._match(c)

    def _match(self, c: sqlite3.Connection) -> None:
        """Run working orders against the current quotes until nothing more triggers."""
        for _ in range(100):
            if not self._match_once(c):
                return
        raise BrokerError("simulated matching did not settle")

    def _match_once(self, c: sqlite3.Connection) -> bool:
        rows = c.execute(
            "SELECT * FROM sim_orders WHERE (status IN ('Submitted','PartiallyFilled','PendingCancel') "
            "OR (status='PreSubmitted' AND activated=1)) AND held=0 "
            "ORDER BY broker_order_id"
        ).fetchall()
        for row in rows:
            cond = self._condition(row)
            if cond is not None and not row["condition_released"]:
                # Held at the broker until the reference series' last price (the midpoint when
                # it reports no last) crosses the trigger; then released in its own contract.
                ref = self._quote_row(c, cond.contract.contract_key)
                if ref is None or ref["quality"] == QuoteQuality.UNAVAILABLE.value:
                    continue
                last = _d(ref["last"])
                if last is None and ref["bid"] is not None and ref["ask"] is not None:
                    last = (Decimal(ref["bid"]) + Decimal(ref["ask"])) / 2
                if not cond.met(last):
                    continue
                c.execute(
                    "UPDATE sim_orders SET condition_released=1 WHERE broker_order_id=?",
                    (row["broker_order_id"],),
                )
            q = self._quote_for(c, self._contract(row))
            if q is None or q["quality"] == QuoteQuality.UNAVAILABLE.value or q["bid"] is None:
                continue
            bid, ask = Decimal(q["bid"]), Decimal(q["ask"])
            buy = row["side"] == "BUY"
            kind = row["order_type"]
            price = ask if buy else bid
            if kind == "LMT":
                limit = Decimal(row["limit_price"])
                if not (ask <= limit if buy else bid >= limit):
                    continue
            elif kind == "STP":
                stop = Decimal(row["stop_price"])
                if not (ask >= stop if buy else bid <= stop):
                    continue
            remaining = row["quantity"] - row["filled_quantity"]
            if remaining <= 0:
                continue
            self._execute(c, row["broker_order_id"], remaining, price)
            return True
        return False

    def _execute(
        self, c: sqlite3.Connection, broker_order_id: int, qty: int, price: Decimal
    ) -> None:
        row = c.execute(
            "SELECT * FROM sim_orders WHERE broker_order_id=?", (broker_order_id,)
        ).fetchone()
        buy = row["side"] == "BUY"
        contract = self._contract(row)
        key = contract.contract_key
        pos = c.execute(
            "SELECT * FROM sim_contract_positions WHERE contract_key=?", (key,)
        ).fetchone()
        held = pos["quantity"] if pos else 0
        now = self._now().isoformat()
        if row["reduce_only"] and not self.strict:
            # Reduce-only: never more than the position held in the opposite direction.
            reducible = max(-held, 0) if buy else max(held, 0)
            if reducible <= 0:
                self._cancel(c, broker_order_id, "REDUCE_ONLY_NO_POSITION")
                return
            qty = min(qty, reducible)
        n = c.execute(
            "SELECT COUNT(*) FROM sim_executions WHERE broker_order_id=?", (broker_order_id,)
        ).fetchone()[0]
        c.execute(
            """INSERT INTO sim_executions
            (exec_id,broker_order_id,order_ref,symbol,side,quantity,price,executed_at)
            VALUES (?,?,?,?,?,?,?,?)""",
            (
                f"SIM-{broker_order_id:05d}-{n + 1:02d}",
                broker_order_id,
                row["order_ref"],
                row["symbol"],
                row["side"],
                qty,
                str(price),
                now,
            ),
        )
        filled = row["filled_quantity"] + qty
        prior_avg = _d(row["avg_fill_price"]) or Decimal(0)
        avg = (prior_avg * row["filled_quantity"] + price * qty) / filled
        status = OrderStatus.FILLED if filled >= row["quantity"] else OrderStatus.PARTIALLY_FILLED
        reason = row["reason"]
        if (
            row["reduce_only"]
            and not self.strict
            and status != OrderStatus.FILLED
            and filled < row["quantity"]
        ):
            reducible_after = (max(-held, 0) if buy else max(held, 0)) - qty
            if reducible_after <= 0:
                status, reason = OrderStatus.CANCELLED, "REDUCE_ONLY_CLAMPED"
        c.execute(
            "UPDATE sim_orders SET filled_quantity=?, avg_fill_price=?, status=?, reason=?, "
            "updated_at=? "
            "WHERE broker_order_id=?",
            (
                filled,
                str(avg.quantize(Decimal("0.0001"))),
                status.value,
                reason,
                now,
                broker_order_id,
            ),
        )
        signed = qty if buy else -qty
        new_qty = held + signed
        if pos is None:
            c.execute(
                "INSERT INTO sim_contract_positions VALUES (?,?,?,?,?)",
                (key, row["symbol"], contract.model_dump_json(), new_qty, str(price)),
            )
        else:
            cost = _d(pos["avg_cost"]) or price
            if held == 0 or (held > 0) != (signed > 0):
                new_cost = cost if new_qty != 0 and (new_qty > 0) == (held > 0) else price
            else:
                new_cost = (cost * abs(held) + price * qty) / abs(new_qty)
            c.execute(
                "UPDATE sim_contract_positions SET quantity=?, avg_cost=? WHERE contract_key=?",
                (new_qty, str(new_cost.quantize(Decimal("0.0001"))), key),
            )
        if row["parent_ref"] is None and self.strict:
            # Strict: a parent fill activates its children at the quantity they were submitted with.
            c.execute(
                "UPDATE sim_orders SET status=?, activated=1, updated_at=? "
                "WHERE parent_ref=? AND status='PreSubmitted' AND activated=0",
                (
                    "PreSubmitted" if self.children_stay_presubmitted else "Submitted",
                    now,
                    row["order_ref"],
                ),
            )
        elif self.strict:
            # Strict one-cancels-other: only a complete fill cancels the working siblings.
            if status == OrderStatus.FILLED:
                for sib in c.execute(
                    "SELECT * FROM sim_orders WHERE parent_ref=? AND broker_order_id<>? AND "
                    "(status IN ('Submitted','PartiallyFilled','PendingCancel') "
                    "OR (status='PreSubmitted' AND activated=1))",
                    (row["parent_ref"], broker_order_id),
                ).fetchall():
                    self._cancel(c, sib["broker_order_id"], "OCA_SIBLING_FILLED")
        elif row["parent_ref"] is None:
            # A parent fill activates (or grows) its bracket children to the filled quantity.
            for child in c.execute(
                "SELECT * FROM sim_orders WHERE parent_ref=? AND status IN "
                "('PreSubmitted','Submitted','PartiallyFilled')",
                (row["order_ref"],),
            ).fetchall():
                if child["status"] == "PreSubmitted":
                    c.execute(
                        "UPDATE sim_orders SET status='Submitted', quantity=?, updated_at=? "
                        "WHERE broker_order_id=?",
                        (filled, now, child["broker_order_id"]),
                    )
                else:
                    c.execute(
                        "UPDATE sim_orders SET quantity=quantity+?, updated_at=? "
                        "WHERE broker_order_id=?",
                        (qty, now, child["broker_order_id"]),
                    )
        else:
            # One-cancels-other: a protective fill shrinks or cancels its working siblings.
            for sib in c.execute(
                "SELECT * FROM sim_orders WHERE parent_ref=? AND broker_order_id<>? AND "
                "status IN ('Submitted','PartiallyFilled')",
                (row["parent_ref"], broker_order_id),
            ).fetchall():
                left = sib["quantity"] - qty
                if left <= sib["filled_quantity"]:
                    self._cancel(c, sib["broker_order_id"], "OCA_SIBLING_FILLED")
                else:
                    c.execute(
                        "UPDATE sim_orders SET quantity=?, updated_at=? WHERE broker_order_id=?",
                        (left, now, sib["broker_order_id"]),
                    )
