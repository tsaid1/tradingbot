"""The SDK-free contract-details facade, on invented objects only.

Every case composes the actual facade, the actual ``CallbackRouter`` and the actual contract normalizer.
Each one is compared with the same calls made directly on a second router, and, where meaningful,
with a normalizer fed directly. Covered: a valid object, malformed rows, foreign ids, END handling,
1052 error routing, refused call forms and the facade's surface.
"""
from __future__ import annotations

import ast
import json
import os
import pickle
import subprocess
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest

import tradingbot.ibkr_contract_facade as facade_module
from tradingbot.ibkr_contract_facade import ContractCallbackFacade
from tradingbot.paper_readiness.callback_router import CallbackRouter
from tradingbot.paper_readiness.ibkr_callbacks import CallbackRequest, IbkrCallbackNormalizer
from tradingbot.paper_readiness.observations import ResponseKind
from tradingbot.paper_readiness.transport import FORBIDDEN_NAME_RE, FakeClock

REPO = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 3, 4, 16, 0, tzinfo=UTC)
CRID, PRID = 4101, 4102
CON_ID = 870001
CANARY = "CANARY-FACADE-TEXT"
JSON_CANARY = '{"canary": "CANARY-FACADE-JSON"}'


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
    """An invented client double: connect, stop and disconnect only."""

    def __init__(self) -> None:
        self.connection, self.reader = Connection(), Reader()

    def connect(self):
        return self

    def stop_read(self, slot):
        pass

    def disconnect(self):
        self.connection.connected = False


class Contract:
    def __init__(self, con_id: object = CON_ID, currency: object = "USD") -> None:
        self.conId, self.symbol, self.localSymbol, self.secType = con_id, "FCDETF", "FCDETF", "STK"
        self.currency, self.exchange, self.primaryExchange = currency, "SMART", "INV-LISTING"


class Details:
    def __init__(self, contract: object = None) -> None:
        self.contract = Contract() if contract is None else contract
        self.longName, self.tradingHours = "INVENTED FACADE ETF", "20260304:0400-20260304:2000"
        self.liquidHours, self.timeZoneId, self.minTick = "20260304:0930-20260304:1600", "America/New_York", 0.01


def contract_router() -> CallbackRouter:
    """A router with the contract stream and the single positions window. The ledger establishes local
    quiescence only with at least one lifecycle read, and contract details never take one, so the
    window gives a clean run something to complete."""
    r = CallbackRouter(Client(), _Lock(), FakeClock(NOW))
    r.start()
    r.register_contract(CRID)
    r.register_positions(PRID)
    return r


def paired(script) -> tuple[CallbackRouter, CallbackRouter]:
    """Run one script through the facade and the same script directly on the router's entries."""
    via_facade = contract_router()
    script(ContractCallbackFacade(via_facade))
    direct = contract_router()
    script(_DirectEntries(direct))
    return via_facade, direct


class _DirectEntries:
    """The same call shapes, made directly on the router with the 1052 error selected explicitly."""

    def __init__(self, router: CallbackRouter) -> None:
        self._r = router

    def contractDetails(self, *args):
        self._r.contractDetails(*args)

    def contractDetailsEnd(self, *args):
        self._r.contractDetailsEnd(*args)

    def error(self, *args):
        self._r.error_1052(*args)


def finish(r: CallbackRouter):
    r.positionEnd()  # the positions window ends with no rows; it is not part of the facade
    while r.consume_one():
        pass
    r.shutdown(join_timeout=0.01)
    return r.report()


def assert_same(via_facade: CallbackRouter, direct: CallbackRouter) -> None:
    assert via_facade.contract_envelopes() == direct.contract_envelopes()
    assert via_facade._contract.issues == direct._contract.issues  # failure evidence preserved
    assert finish(via_facade) == finish(direct)


def kinds(router: CallbackRouter) -> list:
    return [e.kind for e in router.contract_envelopes()]


# ----------------------------------------------------------------------------- the one-object unit


def test_control_one_invented_object_then_end_matches_direct_feeding_and_completes_outside_the_ledger():
    def script(target):
        target.contractDetails(CRID, Details())
        target.contractDetailsEnd(CRID)

    via_facade, direct = paired(script)
    bare = IbkrCallbackNormalizer(CRID, CallbackRequest.CONTRACT_DETAILS)
    bare.contractDetails(CRID, Details())
    bare.contractDetailsEnd(CRID)
    assert via_facade.contract_envelopes() == bare.envelopes()
    assert kinds(via_facade) == [ResponseKind.CONTRACT_DETAILS, ResponseKind.CONTRACT_DETAILS_END]
    row = via_facade.contract_envelopes()[0].contract
    assert (row.con_id, row.symbol, row.currency) == (CON_ID, "FCDETF", "USD")
    report = finish(via_facade)
    assert report.completed is True and report.faults == ()
    assert report.contract_details == "OUTSIDE_LIFECYCLE_ACCOUNTING"
    assert {s.slot for s in report.adapter.ledger.slots} == {"POSITIONS"}  # contract details take no slot
    assert finish(direct) == report


def test_contract_details_alone_never_establish_local_completion():
    r = CallbackRouter(Client(), _Lock(), FakeClock(NOW))
    r.start()
    r.register_contract(CRID)
    f = ContractCallbackFacade(r)
    f.contractDetails(CRID, Details())
    f.contractDetailsEnd(CRID)
    r.shutdown(join_timeout=0.01)
    report = r.report()
    assert kinds(r) == [ResponseKind.CONTRACT_DETAILS, ResponseKind.CONTRACT_DETAILS_END]
    assert report.faults == () and report.adapter.ledger.slots == ()
    assert report.adapter.ledger.local_quiescence_established is False and report.completed is False


# ----------------------------------------------------------------------------- malformed, foreign and END


@pytest.mark.parametrize("details", [
    Details(Contract(currency=None)), Details(Contract(con_id="not-an-id")), Details(contract=object()), object(),
], ids=["missing-currency", "text-con-id", "contract-without-fields", "details-without-fields"])
def test_a_malformed_object_keeps_the_normalizer_refusal_and_no_end(details):
    def script(target):
        target.contractDetails(CRID, details)
        target.contractDetailsEnd(CRID)

    via_facade, direct = paired(script)
    assert ResponseKind.CONTRACT_DETAILS_END not in kinds(via_facade)
    assert via_facade._contract.issues  # not coerced into a row
    assert_same(via_facade, direct)


@pytest.mark.parametrize(("rid", "router_fault"), [(9999, True), (-5, True), ("4101", True), (4101.0, False)],
                         ids=["foreign", "negative", "text", "float"])
def test_a_foreign_or_mistyped_request_id_is_recorded_not_relabelled(rid, router_fault):
    """A numerically equal float reaches the owner, whose normalizer refuses its type."""
    def script(target):
        target.contractDetails(rid, Details())
        target.contractDetailsEnd(CRID)

    via_facade, direct = paired(script)
    assert ("UNKNOWN_REQUEST_ID" in via_facade.report().faults) is router_fault
    rows = [e for e in via_facade.contract_envelopes() if e.kind == ResponseKind.CONTRACT_DETAILS]
    assert not any(e.request_id == CRID for e in rows)  # never relabelled as the owner's row
    assert rid == 9999 or via_facade._contract.issues  # a mistyped id is refused, not coerced
    assert rid != 9999 or [e.request_id for e in rows] == [9999]  # a foreign id stays a stray
    assert_same(via_facade, direct)


@pytest.mark.parametrize("script_name", ["end-twice", "details-after-end", "end-for-foreign-id"])
def test_end_handling_is_the_normalizers(script_name):
    def script(target):
        target.contractDetails(CRID, Details())
        target.contractDetailsEnd(CRID if script_name != "end-for-foreign-id" else 9998)
        if script_name == "end-twice":
            target.contractDetailsEnd(CRID)
        elif script_name == "details-after-end":
            target.contractDetails(CRID, Details())

    via_facade, direct = paired(script)
    assert_same(via_facade, direct)


# ----------------------------------------------------------------------------- 1052 error routing


def test_an_own_error_reaches_error_1052_once_with_time_before_code_and_no_text():
    def script(target):
        target.contractDetails(CRID, Details())
        target.error(CRID, 1_700_000_000, 200, CANARY, JSON_CANARY)

    via_facade, direct = paired(script)
    errors = [e for e in via_facade.contract_envelopes() if e.kind == ResponseKind.ERROR]
    assert len(errors) == 1 and errors[0].message.code == 200  # the time was not read as the code
    text = repr(via_facade.contract_envelopes()) + repr(via_facade.report()) + repr(via_facade._contract.issues)
    assert "CANARY" not in text
    bare = IbkrCallbackNormalizer(CRID, CallbackRequest.CONTRACT_DETAILS)
    bare.contractDetails(CRID, Details())
    bare.error_1052(CRID, 1_700_000_000, 200, CANARY, JSON_CANARY)
    assert via_facade.contract_envelopes() == bare.envelopes()
    assert_same(via_facade, direct)


@pytest.mark.parametrize("args", [
    (CRID, 0, 200),  # three arguments
    (CRID, "bad-time", 200, CANARY),
    (CRID, 0, True, CANARY),
    (CRID, 0, 200, CANARY, JSON_CANARY, "extra"),
    (),
], ids=["three-arguments", "time-text", "code-bool", "six-arguments", "no-arguments"])
def test_a_malformed_error_shape_keeps_the_routers_1052_judgement(args):
    def script(target):
        target.contractDetails(CRID, Details())
        target.error(*args)
        target.contractDetailsEnd(CRID)

    via_facade, direct = paired(script)
    assert ResponseKind.CONTRACT_DETAILS_END not in kinds(via_facade)
    assert_same(via_facade, direct)


@pytest.mark.parametrize(("args", "fault"), [
    ((-1, 0, 2104, CANARY), None),
    ((-1, 0, 1100, CANARY), "SESSION_ERROR"),
    ((-1.0, 0, 2104, CANARY), "SESSION_ERROR_MALFORMED"),
    ((9997, 0, 200, CANARY), "UNKNOWN_REQUEST_ID"),
], ids=["session-ok", "session-non-ok", "session-float-id", "foreign-id"])
def test_session_and_foreign_errors_keep_the_routers_rules(args, fault):
    def script(target):
        target.contractDetails(CRID, Details())
        target.error(*args)
        target.contractDetailsEnd(CRID)

    via_facade, direct = paired(script)
    faults = via_facade.report().faults
    assert faults == () if fault is None else fault in faults
    assert_same(via_facade, direct)


# ----------------------------------------------------------------------------- refused call forms


@pytest.mark.parametrize(("call", "name"), [
    (lambda f: f.error(CRID, 0, 200, errorString=CANARY), "error"),
    (lambda f: f.error(reqId=CRID, errorTime=0, errorCode=200, errorString=CANARY), "error"),
    (lambda f: f.contractDetails(CRID, contractDetails=Details()), "contractDetails"),
    (lambda f: f.contractDetails(CRID), "contractDetails"),
    (lambda f: f.contractDetails(CRID, Details(), CANARY), "contractDetails"),
    (lambda f: f.contractDetailsEnd(), "contractDetailsEnd"),
    (lambda f: f.contractDetailsEnd(reqId=CRID), "contractDetailsEnd"),
], ids=["error-kwarg", "error-all-kwargs", "details-kwarg", "details-one-arg", "details-three-args",
        "end-no-arg", "end-kwarg"])
def test_an_unsupported_call_form_is_a_persistent_router_refusal_not_a_raise(call, name):
    r = contract_router()
    f = ContractCallbackFacade(r)
    f.contractDetails(CRID, Details())
    call(f)  # never raises
    f.contractDetailsEnd(CRID)
    report = finish(r)
    assert "UNSUPPORTED_CALLBACK" in report.faults and report.completed is False
    assert ResponseKind.CONTRACT_DETAILS_END not in kinds(r)  # the owner's refusal is kept
    direct = contract_router()
    direct.contractDetails(CRID, Details())
    direct.unsupported_callback(name)
    direct.contractDetailsEnd(CRID)
    assert r.contract_envelopes() == direct.contract_envelopes() and finish(direct) == report
    assert "CANARY" not in repr(report) + repr(r.contract_envelopes()) + repr(r._contract.issues)


def test_a_refused_form_before_registration_still_blocks_completion():
    r = CallbackRouter(Client(), _Lock(), FakeClock(NOW))
    r.start()
    ContractCallbackFacade(r).error(CRID, 0, 200, errorString=CANARY)
    r.register_contract(CRID)
    f = ContractCallbackFacade(r)
    f.contractDetails(CRID, Details())
    f.contractDetailsEnd(CRID)
    report = finish(r)
    assert ResponseKind.CONTRACT_DETAILS_END in kinds(r)  # the later stream itself is clean
    assert "UNSUPPORTED_CALLBACK" in report.faults and report.completed is False


# ----------------------------------------------------------------------------- surface


def test_the_facade_is_three_fixed_methods_over_one_router_and_retains_nothing():
    public = {n for n in dir(ContractCallbackFacade) if not n.startswith("_")}
    assert public == {"contractDetails", "contractDetailsEnd", "error"}
    assert ContractCallbackFacade.__slots__ == ("_router",)
    assert ContractCallbackFacade.__mro__ == (ContractCallbackFacade, object)  # no superclass to reach
    assert "__getattr__" not in vars(ContractCallbackFacade)
    assert "__getattribute__" not in vars(ContractCallbackFacade)
    for name in public | set(facade_module.__all__):
        assert not FORBIDDEN_NAME_RE.search(name), name
    r = contract_router()
    f = ContractCallbackFacade(r)
    f.contractDetails(CRID, Details())
    f.error(CRID, 0, 200, CANARY)
    with pytest.raises(AttributeError):
        f.extra = 1  # type: ignore[attr-defined]
    with pytest.raises(AttributeError):
        f.tickPrice  # noqa: B018 - no other callback surface exists
    assert repr(f) == "ContractCallbackFacade()"
    with pytest.raises(TypeError):
        pickle.dumps(f)


@pytest.mark.parametrize("router", [None, object(), type("Sub", (CallbackRouter,), {})])
def test_only_an_exact_router_is_accepted(router):
    with pytest.raises(TypeError):
        ContractCallbackFacade(router)  # type: ignore[arg-type]


def test_the_module_imports_only_this_project_and_loads_no_network_or_broker_module():
    tree = ast.parse(Path(facade_module.__file__).read_text(encoding="utf-8"))
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            roots.add((node.module or "").split(".")[0])
    assert roots == {"__future__", "tradingbot"}
    probe = (
        "import sys, json\n"
        "import tradingbot.paper_readiness.callback_router\n"
        "before = set(sys.modules)\n"
        "import tradingbot.ibkr_contract_facade\n"
        "print(json.dumps(sorted(set(sys.modules) - before)))\n"
    )
    out = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, check=True,
                         env={**os.environ, "PYTHONPATH": str(REPO / "src")})
    assert json.loads(out.stdout) == ["tradingbot.ibkr_contract_facade"]
