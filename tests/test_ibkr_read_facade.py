"""The SDK-free read-callback facade, on invented objects only.

Every case composes the actual facade, the actual ``CallbackRouter`` and the actual normalizers. Each is
compared with the same calls made directly on a second router's entries, and the control also with
normalizers fed directly. Covered: all four read streams, malformed values, foreign ids, END handling,
1052 error routing, refused call forms before and after registration, the explicit managedAccounts
refusal, direct notifications (nested, after closure and from hooks) and the facade's surface.
"""
from __future__ import annotations

import ast
import json
import os
import pickle
import subprocess
import sys
import threading
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

import tradingbot.ibkr_read_facade as facade_module
from tradingbot.ibkr_contract_facade import ContractCallbackFacade
from tradingbot.ibkr_read_facade import ReadCallbackFacade
from tradingbot.paper_readiness.account_position_callbacks import (
    AccountSummaryCallbacks,
    PositionSnapshotCallbacks,
)
from tradingbot.paper_readiness.callback_router import CallbackRouter
from tradingbot.paper_readiness.ibkr_callbacks import CallbackRequest, IbkrCallbackNormalizer
from tradingbot.paper_readiness.observations import ResponseKind
from tradingbot.paper_readiness.transport import FORBIDDEN_NAME_RE, FakeClock

REPO = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 3, 4, 16, 0, tzinfo=UTC)
CRID, QRID, SRID, PRID = 5101, 5102, 5103, 5104
CON_ID = 880001
ACCOUNT = "SYNTH-READFCD0001"
LAST = str(int((NOW - timedelta(seconds=10)).timestamp()))
CANARY = "CANARY-READ-FACADE-TEXT"
ENTRIES = {"contractDetails", "contractDetailsEnd", "error", "marketDataType", "tickPrice", "tickSize",
           "tickString", "tickGeneric", "tickSnapshotEnd", "accountSummary", "accountSummaryEnd",
           "position", "positionEnd", "connectAck", "connectionClosed", "managedAccounts"}


class _Lock:
    def __init__(self) -> None:
        self._lock = threading.Lock()

    def __enter__(self):
        self._lock.acquire()

    def __exit__(self, *exc):
        self._lock.release()


class Connection:
    def __init__(self) -> None:
        self.connected, self.closed = True, True

    def is_connected(self):
        return self.connected

    def socket_closed(self):
        return self.closed


class Reader:
    def __init__(self) -> None:
        self.alive = True

    def join(self, timeout):
        self.alive = False

    def is_alive(self):
        return self.alive

    def partial_bytes(self):
        return 0


class Client:
    """An invented client double. Optional hooks call back into a target from connect or disconnect."""

    def __init__(self, on_connect=None, on_disconnect=None) -> None:
        self.connection, self.reader = Connection(), Reader()
        self.on_connect, self.on_disconnect = on_connect, on_disconnect
        self.target = None

    def connect(self):
        if self.on_connect is not None:
            self.on_connect(self.target)
        return self

    def stop_read(self, slot):
        pass

    def disconnect(self):
        self.connection.connected = False
        if self.on_disconnect is not None:
            self.on_disconnect(self.target)


class Contract:
    def __init__(self, con_id: object = CON_ID) -> None:
        self.conId, self.symbol, self.localSymbol, self.secType = con_id, "RDFETF", "RDFETF", "STK"
        self.currency, self.exchange, self.primaryExchange = "USD", "SMART", "INV-LISTING"


class Details:
    def __init__(self) -> None:
        self.contract = Contract()
        self.longName, self.tradingHours = "INVENTED READ FACADE ETF", "20260304:0400-20260304:2000"
        self.liquidHours, self.timeZoneId, self.minTick = "20260304:0930-20260304:1600", "America/New_York", 0.01


class Direct:
    """The same call shapes made directly on the router, with the 1052 error selected explicitly."""

    def __init__(self, router: CallbackRouter) -> None:
        self._r = router

    def contractDetails(self, *args):
        self._r.contractDetails(*args)

    def contractDetailsEnd(self, *args):
        self._r.contractDetailsEnd(*args)

    def error(self, *args):
        self._r.error_1052(*args)

    def marketDataType(self, *args):
        self._r.marketDataType(*args)

    def tickPrice(self, *args):
        self._r.tickPrice(*args)

    def tickSize(self, *args):
        self._r.tickSize(*args)

    def tickString(self, *args):
        self._r.tickString(*args)

    def tickGeneric(self, *args):
        self._r.tickGeneric(*args)

    def tickSnapshotEnd(self, *args):
        self._r.tickSnapshotEnd(*args)

    def accountSummary(self, *args):
        self._r.accountSummary(*args)

    def accountSummaryEnd(self, *args):
        self._r.accountSummaryEnd(*args)

    def position(self, *args):
        self._r.position(*args)

    def positionEnd(self):
        self._r.positionEnd()

    def connectAck(self):
        self._r.connectAck()

    def connectionClosed(self):
        self._r.connectionClosed()

    def managedAccounts(self, *args):
        self._r.unsupported_callback("managedAccounts")


def registered(router: CallbackRouter) -> CallbackRouter:
    router.register_contract(CRID)
    router.register_quote(QRID, CON_ID)
    router.register_account_summary(SRID)
    router.register_positions(PRID)
    return router


def make_router(client: Client | None = None) -> CallbackRouter:
    r = CallbackRouter(client or Client(), _Lock(), FakeClock(NOW))
    r.start()
    return registered(r)


def paired(script, *, setup=make_router) -> tuple[CallbackRouter, CallbackRouter]:
    """Run one script through the facade and the same script directly on a second router's entries."""
    via_facade = setup()
    script(ReadCallbackFacade(via_facade))
    direct = setup()
    script(Direct(direct))
    return via_facade, direct


def contract_feed(target) -> None:
    target.contractDetails(CRID, Details())
    target.contractDetailsEnd(CRID)


def quote_feed(target) -> None:
    target.marketDataType(QRID, 1)
    target.tickPrice(QRID, 1, 40.0, None)
    target.tickSize(QRID, 0, Decimal(200))
    target.tickPrice(QRID, 2, 40.02, None)
    target.tickSize(QRID, 3, Decimal(400))
    target.tickString(QRID, 45, LAST)
    target.tickSnapshotEnd(QRID)


def summary_feed(target) -> None:
    target.accountSummary(SRID, ACCOUNT, "AccountType", "INVENTED", "")
    for tag, value in (("NetLiquidation", "50000.00"), ("TotalCashValue", "40000.00"), ("BuyingPower", "1.00")):
        target.accountSummary(SRID, ACCOUNT, tag, value, "USD")
    target.accountSummaryEnd(SRID)


def positions_feed(target) -> None:
    target.position(ACCOUNT, Contract(840001), Decimal("-3.5"), 12.5)
    target.position(ACCOUNT, Contract(840002), Decimal(7), 3.0)
    target.positionEnd()


def everything(target) -> None:
    contract_feed(target)
    quote_feed(target)
    summary_feed(target)
    positions_feed(target)


def finish(r: CallbackRouter):
    while r.consume_one():
        pass
    r.shutdown(join_timeout=0.01)
    return r.report()


def outputs(r: CallbackRouter) -> tuple:
    """Every stream's envelopes and its normalizer's retained failure evidence."""
    issues = tuple(() if n is None else n.issues for n in (r._contract, r._quote, r._account, r._positions))
    return (r.contract_envelopes(), r.quote_envelopes(), r.account_summary_envelopes(),
            r.position_envelopes(), issues)


def assert_same(via_facade: CallbackRouter, direct: CallbackRouter) -> None:
    assert finish(via_facade) == finish(direct)
    assert outputs(via_facade) == outputs(direct)


def kinds(envelopes) -> list:
    return [e.kind for e in envelopes]


# ----------------------------------------------------------------------------- the four streams


def test_control_all_four_streams_match_direct_entries_and_directly_fed_normalizers():
    via_facade, direct = paired(everything)
    assert via_facade._outstanding() == direct._outstanding() == 15  # each lifecycle call admitted once
    report = finish(via_facade)
    assert report.completed is True and report.faults == ()
    assert report.contract_details == "OUTSIDE_LIFECYCLE_ACCOUNTING"
    assert {s.slot for s in report.adapter.ledger.slots} == {"QUOTE_SNAPSHOT", "ACCOUNT_SUMMARY", "POSITIONS"}
    assert finish(direct) == report and outputs(via_facade) == outputs(direct)
    contract = IbkrCallbackNormalizer(CRID, CallbackRequest.CONTRACT_DETAILS)
    quote = IbkrCallbackNormalizer(QRID, CallbackRequest.QUOTE_SNAPSHOT, con_id=CON_ID, clock=FakeClock(NOW))
    summary, positions = AccountSummaryCallbacks(SRID), PositionSnapshotCallbacks(PRID)
    contract_feed(contract)
    quote_feed(quote)
    summary_feed(summary)
    positions_feed(positions)
    assert via_facade.contract_envelopes() == contract.envelopes()
    assert via_facade.quote_envelopes() == quote.envelopes()
    assert via_facade.account_summary_envelopes() == summary.envelopes()
    assert via_facade.position_envelopes() == positions.envelopes()
    assert {e.request_id for e in via_facade.position_envelopes()} == {PRID}  # one local snapshot id


@pytest.mark.parametrize("feed", [quote_feed, summary_feed, positions_feed],
                         ids=["quote", "account-summary", "positions"])
def test_each_lifecycle_stream_alone_matches_direct_entries(feed):
    via_facade, direct = paired(feed)
    assert via_facade._outstanding() == direct._outstanding() > 0
    assert_same(via_facade, direct)


# ----------------------------------------------------------------------------- malformed, foreign and END


@pytest.mark.parametrize(("bad", "end_kind", "stream"), [
    (lambda t: t.tickPrice(QRID, 1, "40.0", None), ResponseKind.TICK_SNAPSHOT_END, 1),
    (lambda t: t.tickSize(QRID, 0, 200.5), ResponseKind.TICK_SNAPSHOT_END, 1),
    (lambda t: t.accountSummary(SRID, "CANARYREAL991", "NetLiquidation", "1.00", "USD"),
     ResponseKind.ACCOUNT_SUMMARY_END, 2),
    (lambda t: t.accountSummary(SRID, ACCOUNT, "NetLiquidation", 1, "USD"), ResponseKind.ACCOUNT_SUMMARY_END, 2),
    (lambda t: t.position(ACCOUNT, Contract(840003), 1.5, 1.0), ResponseKind.POSITION_END, 3),
    (lambda t: t.position(ACCOUNT, Contract("not-an-id"), Decimal(1), 1.0), ResponseKind.POSITION_END, 3),
], ids=["price-text", "size-float", "account-not-synthetic", "summary-value-int", "position-float",
        "position-con-id-text"])
def test_a_malformed_value_keeps_the_normalizer_refusal_and_withholds_end(bad, end_kind, stream):
    def script(target):
        bad(target)
        everything(target)

    via_facade, direct = paired(script)
    assert_same(via_facade, direct)  # drains the consumer, so every queued callback is delivered
    assert end_kind not in kinds(outputs(via_facade)[stream])  # not coerced into a clean stream
    assert outputs(via_facade)[4][stream]  # the normalizer's issue is kept
    assert "CANARYREAL991" not in repr(outputs(via_facade))


@pytest.mark.parametrize("stray", [
    lambda t: t.tickPrice(9999, 1, 40.0, None),
    lambda t: t.tickSnapshotEnd(9999),
    lambda t: t.accountSummary(9998, ACCOUNT, "NetLiquidation", "1.00", "USD"),
    lambda t: t.accountSummaryEnd(9998),
], ids=["tick-foreign", "snapshot-end-foreign", "summary-foreign", "summary-end-foreign"])
def test_a_foreign_request_id_stays_a_recorded_stray(stray):
    def script(target):
        everything(target)
        stray(target)

    via_facade, direct = paired(script)
    assert "UNKNOWN_REQUEST_ID" in via_facade.report().faults
    assert_same(via_facade, direct)


@pytest.mark.parametrize("after", [
    lambda t: t.tickSnapshotEnd(QRID),
    lambda t: t.tickPrice(QRID, 1, 40.0, None),
    lambda t: t.accountSummaryEnd(SRID),
    lambda t: t.accountSummary(SRID, ACCOUNT, "SettledCash", "1.00", "USD"),
    lambda t: t.positionEnd(),
    lambda t: t.position(ACCOUNT, Contract(840004), Decimal(1), 1.0),
], ids=["snapshot-end-twice", "tick-after-end", "summary-end-twice", "summary-after-end",
        "position-end-twice", "position-after-end"])
def test_post_end_input_follows_the_routers_existing_rules(after):
    def script(target):
        everything(target)
        after(target)

    via_facade, direct = paired(script)
    assert_same(via_facade, direct)


# ----------------------------------------------------------------------------- 1052 error routing


def test_a_quote_error_reaches_error_1052_once_with_time_before_code_and_no_text():
    def script(target):
        target.marketDataType(QRID, 1)
        target.error(QRID, 1_700_000_000, 200, CANARY)
        target.tickSnapshotEnd(QRID)

    via_facade, direct = paired(script)
    assert_same(via_facade, direct)  # drains the consumer, which delivers the queued quote error
    errors = [e for e in via_facade.quote_envelopes() if e.kind == ResponseKind.ERROR]
    assert len(errors) == 1 and errors[0].message.code == 200  # the time was not read as the code
    assert CANARY not in repr(outputs(via_facade)) + repr(via_facade.report())


@pytest.mark.parametrize("args", [
    (SRID, 0, 300, CANARY),
    (-1, 0, 2104, CANARY),
    (-1, 0, 1100, CANARY),
    (-1.0, 0, 2104, CANARY),
    (9997, 0, 200, CANARY),
    (QRID, 0, 200),
], ids=["account-error", "session-ok", "session-non-ok", "session-float-id", "foreign-id", "three-arguments"])
def test_other_errors_keep_the_routers_1052_rules(args):
    def script(target):
        everything(target)
        target.error(*args)

    via_facade, direct = paired(script)
    assert_same(via_facade, direct)


# ----------------------------------------------------------------------------- refused call forms

_SHAPES = [
    ("contractDetails", lambda: (CRID, Details())),
    ("contractDetailsEnd", lambda: (CRID,)),
    ("marketDataType", lambda: (QRID, 1)),
    ("tickPrice", lambda: (QRID, 1, 40.0, None)),
    ("tickSize", lambda: (QRID, 0, Decimal(1))),
    ("tickString", lambda: (QRID, 45, LAST)),
    ("tickGeneric", lambda: (QRID, 49, 0.0)),
    ("tickSnapshotEnd", lambda: (QRID,)),
    ("accountSummary", lambda: (SRID, ACCOUNT, "NetLiquidation", "1.00", "USD")),
    ("accountSummaryEnd", lambda: (SRID,)),
    ("position", lambda: (ACCOUNT, Contract(840001), Decimal(1), 1.0)),
    ("positionEnd", lambda: ()),
    ("connectAck", lambda: ()),
    ("connectionClosed", lambda: ()),
]
_FORMS = [(name, form, shape) for name, shape in _SHAPES for form in ("extra", "missing", "kwarg")
          if not (form == "missing" and not shape())]


def _refused_call(name: str, form: str, shape):
    args = shape()

    def call(facade):
        method = {"contractDetails": facade.contractDetails, "contractDetailsEnd": facade.contractDetailsEnd,
                  "marketDataType": facade.marketDataType, "tickPrice": facade.tickPrice,
                  "tickSize": facade.tickSize, "tickString": facade.tickString,
                  "tickGeneric": facade.tickGeneric, "tickSnapshotEnd": facade.tickSnapshotEnd,
                  "accountSummary": facade.accountSummary, "accountSummaryEnd": facade.accountSummaryEnd,
                  "position": facade.position, "positionEnd": facade.positionEnd,
                  "connectAck": facade.connectAck, "connectionClosed": facade.connectionClosed}[name]
        if form == "extra":
            method(*args, CANARY)
        elif form == "missing":
            method(*args[:-1])
        else:
            method(*args, extra=CANARY)
    return call


@pytest.mark.parametrize(("name", "form", "shape"), _FORMS,
                         ids=[f"{name}-{form}" for name, form, _ in _FORMS])
def test_a_refused_form_is_only_the_fixed_name_router_refusal(name, form, shape):
    """Compared with a router that receives only unsupported_callback(name): the entry is never also
    called, no request id or value is taken from the call, and nothing raises."""
    via_facade = make_router()
    _refused_call(name, form, shape)(ReadCallbackFacade(via_facade))
    everything(ReadCallbackFacade(via_facade))
    direct = make_router()
    direct.unsupported_callback(name)
    everything(Direct(direct))
    report = finish(via_facade)
    assert "UNSUPPORTED_CALLBACK" in report.faults and report.completed is False
    assert report.adapter.direct_callbacks == ()  # a refused notification is not an observation
    assert finish(direct) == report and outputs(via_facade) == outputs(direct)
    assert CANARY not in repr(report) + repr(outputs(via_facade))


@pytest.mark.parametrize("call", [
    lambda f: f.tickPrice(QRID, 1, 40.0, attrib=None),
    lambda f: f.positionEnd(CANARY),
    lambda f: f.connectAck(CANARY),
    lambda f: f.managedAccounts(CANARY),
    lambda f: f.error(QRID, 0, 200, errorString=CANARY),
], ids=["tick-kwarg", "position-end-extra", "connect-ack-extra", "managed-accounts", "error-kwarg"])
def test_a_refusal_before_any_registration_still_blocks_completion(call):
    r = CallbackRouter(Client(), _Lock(), FakeClock(NOW))
    r.start()
    call(ReadCallbackFacade(r))
    assert r.report().faults == ("UNSUPPORTED_CALLBACK",)
    registered(r)
    everything(ReadCallbackFacade(r))
    report = finish(r)
    assert ResponseKind.TICK_SNAPSHOT_END in kinds(r.quote_envelopes())  # later streams are clean
    assert ResponseKind.POSITION_END in kinds(r.position_envelopes())
    assert report.faults == ("UNSUPPORTED_CALLBACK",) and report.completed is False


class _Exploding:
    """Any attribute access raises, so a read of the value would be visible."""

    def __getattribute__(self, name):
        raise AssertionError("the value was read")


def test_managed_accounts_is_always_the_fixed_refusal_and_its_value_is_never_read():
    def script(target):
        everything(target)
        target.managedAccounts(_Exploding())

    via_facade, direct = paired(script)
    assert "UNSUPPORTED_CALLBACK" in via_facade.report().faults
    assert_same(via_facade, direct)
    r = make_router()
    ReadCallbackFacade(r).managedAccounts(accountsList=CANARY)
    assert r.report().faults == ("UNSUPPORTED_CALLBACK",) and CANARY not in repr(outputs(r))


# ----------------------------------------------------------------------------- direct notifications


def hooked_router(target_kind: str) -> CallbackRouter:
    client = Client(on_connect=lambda t: t.connectAck(), on_disconnect=lambda t: t.connectionClosed())
    r = CallbackRouter(client, _Lock(), FakeClock(NOW))
    client.target = ReadCallbackFacade(r) if target_kind == "facade" else Direct(r)
    r.start()
    return registered(r)


def test_control_connect_and_disconnect_notifications_from_hooks_match_direct_entries():
    via_facade, direct = hooked_router("facade"), hooked_router("direct")
    everything(ReadCallbackFacade(via_facade))
    everything(Direct(direct))
    report = finish(via_facade)
    assert report.adapter.direct_callbacks == ("CONNECT_ACK", "CONNECTION_CLOSED")
    assert report.faults == () and report.completed is True
    assert finish(direct) == report and outputs(via_facade) == outputs(direct)


def test_notifications_after_closure_are_refused_and_not_recorded():
    via_facade, direct = make_router(), make_router()
    for router, target in ((via_facade, ReadCallbackFacade(via_facade)), (direct, Direct(direct))):
        everything(target)
        finish(router)
        target.connectAck()
        target.connectionClosed()
    report = via_facade.report()
    assert report.faults.count("ARRIVAL_AFTER_CLOSE") == 2 and report.adapter.direct_callbacks == ()
    assert report == direct.report() and outputs(via_facade) == outputs(direct)


class AckingContract(Contract):
    """A contract whose conId getter sends a notification while the router is snapshotting it."""

    def __init__(self, con_id: int, target) -> None:
        super().__init__(con_id)
        self._target = target

    def __getattribute__(self, name):
        if name == "conId":
            object.__getattribute__(self, "_target").connectAck()
        return object.__getattribute__(self, name)


def test_a_notification_nested_in_an_arrival_is_refused_and_delivers_nothing():
    reports = []
    for kind in ("facade", "direct"):
        r = make_router()
        target = ReadCallbackFacade(r) if kind == "facade" else Direct(r)
        target.position(ACCOUNT, AckingContract(840009, target), Decimal(1), 1.0)
        reports.append(r.report())
        assert r.report().faults == ("ARRIVAL_REENTERED",) and r.report().adapter.direct_callbacks == ()
        assert r._outstanding() == 1  # the outer position, admitted once; the nested call delivered nothing
    assert reports[0] == reports[1]


# ----------------------------------------------------------------------------- surface and ownership


def test_the_facade_is_fixed_methods_over_one_router_and_retains_nothing():
    public = {n for n in dir(ReadCallbackFacade) if not n.startswith("_")}
    assert public == ENTRIES
    assert ReadCallbackFacade.__slots__ == ("_contract", "_router")
    assert ReadCallbackFacade.__mro__ == (ReadCallbackFacade, object)  # no superclass to reach
    assert "__getattr__" not in vars(ReadCallbackFacade)
    assert "__getattribute__" not in vars(ReadCallbackFacade)
    for name in public | set(facade_module.__all__):
        assert not FORBIDDEN_NAME_RE.search(name), name
    r = make_router()
    f = ReadCallbackFacade(r)
    assert type(f._contract) is ContractCallbackFacade and f._contract._router is r  # composed, not copied
    with pytest.raises(AttributeError):
        f.extra = 1  # type: ignore[attr-defined]
    for absent in ("nextValidId", "inventedProtoVariant", "consume_one", "shutdown", "register_quote",
                   "unsupported_callback"):
        with pytest.raises(AttributeError):
            getattr(f, absent)  # no other surface, no interception and no router control
    assert repr(f) == "ReadCallbackFacade()"
    with pytest.raises(TypeError):
        pickle.dumps(f)


@pytest.mark.parametrize("router", [None, object(), type("Sub", (CallbackRouter,), {})])
def test_only_an_exact_router_is_accepted(router):
    with pytest.raises(TypeError):
        ReadCallbackFacade(router)  # type: ignore[arg-type]


def test_the_module_has_no_reflection_superclass_logging_or_sdk_import():
    tree = ast.parse(Path(facade_module.__file__).read_text(encoding="utf-8"))
    modules = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert not [node for node in ast.walk(tree) if isinstance(node, ast.Import)]
    assert modules == {"__future__", "tradingbot.ibkr_contract_facade",
                       "tradingbot.paper_readiness.callback_router"}
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    for banned in ("getattr", "setattr", "hasattr", "super", "eval", "exec", "print", "logging", "vars"):
        assert banned not in names, banned
    probe = (
        "import sys, json\n"
        "import tradingbot.paper_readiness.callback_router\n"
        "before = set(sys.modules)\n"
        "import tradingbot.ibkr_read_facade\n"
        "new = sorted(set(sys.modules) - before)\n"
        "sdk = sorted(m for m in sys.modules if m.split('.')[0] in ('ibapi', 'google'))\n"
        "print(json.dumps([new, sdk]))\n"
    )
    out = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, check=True,
                         env={**os.environ, "PYTHONPATH": str(REPO / "src")})
    assert json.loads(out.stdout) == [["tradingbot.ibkr_contract_facade", "tradingbot.ibkr_read_facade"], []]
