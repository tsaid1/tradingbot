"""SDK-free tests of the paper order mode: caps, config gate, pre-trade guard, durable counter, status
mapping. Run with and without the SDK (``TRADINGBOT_BLOCK_IBAPI=1``). Invented data only."""
from __future__ import annotations

import ast
import os
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from tradingbot.execution.broker import OrderStatus
from tradingbot.ibkr import errors as E
from tradingbot.ibkr.config import load_config
from tradingbot.ibkr_orders import caps as C
from tradingbot.ibkr_orders.config import (
    OrdersNotEnabled,
    PaperOrderConfig,
    StateDirInvalid,
    UnknownExperiment,
    load_order_config,
)
from tradingbot.ibkr_orders.counter import (
    CapReached,
    CounterCorrupt,
    CounterLocked,
    CounterMissing,
    JournalExists,
    SubmissionCounter,
    caps_digest,
    init_journal,
)
from tradingbot.ibkr_orders.guard import (
    ALLOW,
    ContractFacts,
    GuardContext,
    ProposedOrder,
    RefusalReason,
    WorkingOrder,
    order_guard,
    parse_hours,
)
from tradingbot.ibkr_orders.mapping import classify_order_error, map_status, merge_status
from tradingbot.ibkr_orders.timefmt import parse_broker_time

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "src" / "tradingbot" / "ibkr_orders"
PAPER = "DU9990001"  # synthetic
CON = 15547841
# 2026-11-11 10:00 US/Eastern (EST, UTC-5)
OPEN_NOW = datetime(2026, 11, 11, 15, 0, tzinfo=UTC)
HOURS = "20261111:0930-20261111:1600;20261112:0930-20261112:1600;20261114:CLOSED"
FACTS = ContractFacts(con_id=CON, symbol="TLT", sec_type="STK", currency="USD",
                      min_tick=Decimal("0.01"), time_zone_id="US/Eastern", liquid_hours=HOURS)


# --- caps live in code ----------------------------------------------------------------------------
def test_paper_test_1_caps_are_the_specified_constants():
    c = C.EXPERIMENTS["paper-test-1"]
    assert c is C.PAPER_TEST_1
    assert (c.symbol, c.sec_type, c.currency) == ("TLT", "STK", "USD")
    assert (c.max_order_quantity, c.max_net_position, c.max_submissions) == (1, 1, 6)
    assert c.max_order_notional == Decimal(150)
    assert c.order_types == {"LMT", "STP"} and c.tifs == {"DAY"}
    assert c.long_only and not c.outside_rth_allowed
    assert list(C.EXPERIMENTS) == ["paper-test-1"]
    with pytest.raises(TypeError):
        C.EXPERIMENTS["other"] = c  # type: ignore[index]


# --- the config gate ------------------------------------------------------------------------------
def write_cfg(tmp_path: Path, **over) -> Path:
    state = tmp_path / "state"
    state.mkdir(mode=0o700, exist_ok=True)
    data = {"mode": "paper", "host": "127.0.0.1", "port": 7497, "client_id": 7,
            "account": PAPER, "tws_profile_ini": "/Users/someone/Jts/jts.ini",
            "tws_os_user": "someone", "read_only_check": "config-read",
            "orders": "paper-test-1", "state_dir": str(state)}
    data.update(over)
    lines = []
    for k, v in data.items():
        if v is None:
            continue
        if isinstance(v, bool):
            lines.append(f"{k} = {str(v).lower()}")
        else:
            lines.append(f'{k} = "{v}"' if isinstance(v, str) else f"{k} = {v}")
    p = tmp_path / "paper_orders.toml"
    p.write_text("\n".join(lines) + "\n")
    os.chmod(p, 0o600)
    return p


def test_valid_order_config_names_a_code_experiment(tmp_path):
    cfg = load_order_config(write_cfg(tmp_path))
    assert isinstance(cfg, PaperOrderConfig) and cfg.issued
    assert cfg.experiment is C.PAPER_TEST_1
    assert PAPER not in repr(cfg)


def test_no_orders_entry_means_no_order_mode(tmp_path):
    with pytest.raises(OrdersNotEnabled):
        load_order_config(write_cfg(tmp_path, orders=None))


@pytest.mark.parametrize("value", ["paper-test-2", "PAPER-TEST-1", "", "true", "1"])
def test_unknown_experiment_refused(tmp_path, value):
    with pytest.raises(UnknownExperiment):
        load_order_config(write_cfg(tmp_path, orders=value))


@pytest.mark.parametrize("value", [True, 1])
def test_non_string_orders_entry_refused(tmp_path, value):
    with pytest.raises(UnknownExperiment):
        load_order_config(write_cfg(tmp_path, orders=value))


@pytest.mark.parametrize("port", [7496, 4001, 4002 + 1])
def test_live_ports_refused_in_order_mode(tmp_path, port):
    with pytest.raises(E.PortNotAllowed):
        load_order_config(write_cfg(tmp_path, port=port))


@pytest.mark.parametrize("account", ["U1234567", "F1234567", "I1234567"])
def test_non_paper_accounts_refused_in_order_mode(tmp_path, account):
    with pytest.raises(E.NonPaperAccount):
        load_order_config(write_cfg(tmp_path, account=account))


def test_non_loopback_host_refused(tmp_path):
    with pytest.raises(E.NonLoopbackHost):
        load_order_config(write_cfg(tmp_path, host="192.168.1.2"))


def test_state_dir_must_exist_and_be_private(tmp_path):
    with pytest.raises(StateDirInvalid):
        load_order_config(write_cfg(tmp_path, state_dir=str(tmp_path / "missing")))
    loose = tmp_path / "loose"
    loose.mkdir()
    os.chmod(loose, 0o777)
    with pytest.raises(StateDirInvalid):
        load_order_config(write_cfg(tmp_path, state_dir=str(loose)))


def test_unknown_extra_key_refused(tmp_path):
    with pytest.raises(E.ConfigInvalid):
        load_order_config(write_cfg(tmp_path, enable_orders="yes"))


def test_order_config_file_safety_is_the_read_only_one(tmp_path):
    p = write_cfg(tmp_path)
    os.chmod(p, 0o644)
    with pytest.raises(E.ConfigPermissionsTooOpen):
        load_order_config(p)
    os.chmod(p, 0o600)
    link = tmp_path / "link.toml"
    link.symlink_to(p)
    with pytest.raises(E.ConfigIsSymlink):
        load_order_config(link)


def test_read_only_loader_refuses_an_order_config(tmp_path):
    with pytest.raises(E.ConfigInvalid):
        load_config(write_cfg(tmp_path))


def _sources() -> list[Path]:
    files = sorted(PKG.glob("*.py"))
    assert len(files) >= 8
    return files


@pytest.mark.parametrize("path", _sources(), ids=lambda p: p.name)
def test_no_environment_override_or_forbidden_call_in_order_package(path):
    tree = ast.parse(path.read_text())
    forbidden = {"environ", "getenv", "putenv", "reqGlobalCancel", "reqGlobalCancelProtoBuf",
                 "exerciseOptions", "exerciseOptionsProtoBuf", "updateConfigProtoBuf",
                 "replaceFA", "replaceFAProtoBuf", "reqAutoOpenOrders", "reqOpenOrders",
                 "placeOrder", "cancelOrder", "UPDATE_CONFIG", "REQ_GLOBAL_CANCEL",
                 "EXERCISE_OPTIONS", "REPLACE_FA"}
    hits = [n.attr if isinstance(n, ast.Attribute) else n.id for n in ast.walk(tree)
            if (isinstance(n, ast.Attribute) and n.attr in forbidden)
            or (isinstance(n, ast.Name) and n.id in forbidden)]
    assert hits == [], f"{path.name}: {hits}"


def test_pure_modules_import_without_the_sdk():
    code = ("import sys, importlib.abc\n"
            "class B(importlib.abc.MetaPathFinder):\n"
            "    def find_spec(self, n, p, t=None):\n"
            "        if n == 'ibapi' or n.startswith('ibapi.'): raise ModuleNotFoundError(n)\n"
            "sys.meta_path.insert(0, B())\n"
            "import tradingbot.ibkr_orders.guard, tradingbot.ibkr_orders.counter\n"
            "import tradingbot.ibkr_orders.config, tradingbot.ibkr_orders.broker\n"
            "import tradingbot.ibkr_orders.mapping, tradingbot.ibkr_orders.caps\n"
            "assert 'ibapi' not in sys.modules\n")
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    subprocess.run([sys.executable, "-c", code], check=True, env=env)


# --- the guard ------------------------------------------------------------------------------------
def order(**over) -> ProposedOrder:
    base = {"order_id": 100, "order_ref": "TB-0001", "account": PAPER, "con_id": CON, "symbol": "TLT",
                "sec_type": "STK", "currency": "USD", "action": "BUY", "quantity": Decimal(1),
                "order_type": "LMT", "limit_price": Decimal("88.12"), "stop_price": None, "tif": "DAY",
                "outside_rth": False}
    base.update(over)
    return ProposedOrder(**base)


def ctx(**over) -> GuardContext:
    base = {"caps": C.PAPER_TEST_1, "submissions_used": 0, "contract": FACTS, "expected_account": PAPER,
                "position": Decimal(0), "working": (), "now": OPEN_NOW}
    base.update(over)
    base.setdefault("implied_position", base["position"])  # the session's own fills agree
    base.setdefault("broker_submissions", 0)
    return GuardContext(**base)


def refused(o, c) -> RefusalReason:
    d = order_guard(o, c)
    assert not d, f"expected a refusal, got {d!r}"
    return d.reason


def test_a_conforming_buy_is_allowed():
    assert order_guard(order(), ctx()) is ALLOW


def test_second_share_refused():
    assert refused(order(), ctx(position=Decimal(1))) == RefusalReason.POSITION_CAP
    working_buy = WorkingOrder(order_id=90, con_id=CON, action="BUY", remaining=Decimal(1))
    assert refused(order(), ctx(working=(working_buy,))) == RefusalReason.POSITION_CAP


def test_two_shares_in_one_order_refused():
    assert refused(order(quantity=Decimal(2)), ctx()) == RefusalReason.ORDER_QUANTITY_CAP


def test_151_usd_notional_refused_150_allowed():
    assert refused(order(limit_price=Decimal("151.00")), ctx()) == RefusalReason.NOTIONAL_CAP
    assert order_guard(order(limit_price=Decimal("150.00")), ctx()) is ALLOW
    stop = order(action="SELL", order_type="STP", limit_price=None,
                 stop_price=Decimal("150.01"))
    assert refused(stop, ctx(position=Decimal(1))) == RefusalReason.NOTIONAL_CAP


def test_seventh_submission_refused():
    assert refused(order(), ctx(submissions_used=6)) == RefusalReason.EXPERIMENT_FINISHED
    assert order_guard(order(), ctx(submissions_used=5)) is ALLOW
    # a bracket needing two placements with one left is refused at its first order
    assert refused(order(), ctx(submissions_used=5, still_to_send=1)) == \
        RefusalReason.SUBMISSION_CAP
    assert refused(order(), ctx(submissions_used=None)) == RefusalReason.SUBMISSION_COUNT_UNKNOWN


def test_market_order_refused():
    assert refused(order(order_type="MKT", limit_price=None), ctx()) == RefusalReason.MARKET_ORDER


@pytest.mark.parametrize("otype", ["STP LMT", "TRAIL", "MOC", "LOC", "REL"])
def test_other_order_types_refused(otype):
    assert refused(order(order_type=otype), ctx()) in (RefusalReason.ORDER_TYPE_NOT_ALLOWED,
                                                       RefusalReason.MARKET_ORDER)


def test_short_sale_refused():
    sell = order(action="SELL", limit_price=Decimal("88.00"))
    assert refused(sell, ctx()) == RefusalReason.SHORT_SALE
    assert refused(sell, ctx(position=Decimal(-1))) == RefusalReason.SHORT_POSITION_PRESENT


def test_sell_exceeding_position_net_of_working_sells_refused():
    sell = order(order_id=120, action="SELL", limit_price=Decimal("88.00"))
    stop = WorkingOrder(order_id=101, con_id=CON, action="SELL", remaining=Decimal(1),
                        parent_id=100)
    assert refused(sell, ctx(position=Decimal(1), working=(stop,))) == \
        RefusalReason.SELL_EXCEEDS_POSITION
    assert order_guard(sell, ctx(position=Decimal(1))) is ALLOW
    # a pending cancel still counts: only a broker-confirmed terminal order leaves `working`


def test_oca_siblings_count_once_and_not_against_each_other():
    stop = WorkingOrder(order_id=101, con_id=CON, action="SELL", remaining=Decimal(1),
                        parent_id=100)
    target = order(order_id=102, action="SELL", limit_price=Decimal("90.00"), parent_id=100)
    assert order_guard(target, ctx(position=Decimal(1), working=(stop,))) is ALLOW


def test_amending_the_stop_of_a_filled_entry_is_allowed():
    stop = order(order_id=101, action="SELL", order_type="STP", limit_price=None,
                 stop_price=Decimal("87.50"), parent_id=100)
    original = replace(stop, stop_price=Decimal("87.00"))
    working = (WorkingOrder(order_id=101, con_id=CON, action="SELL", remaining=Decimal(1),
                            parent_id=100),)
    assert order_guard(stop, ctx(position=Decimal(1), working=working, original=original)) \
        is ALLOW
    assert refused(replace(stop, action="BUY"), ctx(position=Decimal(1), working=working,
                                                     original=original)) == \
        RefusalReason.MODIFICATION_INVALID


def test_protective_child_must_be_a_sell_no_larger_than_its_buy_parent():
    parent = order()
    child = order(order_id=101, action="SELL", order_type="STP", limit_price=None,
                  stop_price=Decimal("87.00"), parent_id=100)
    assert order_guard(child, ctx(parent=parent)) is ALLOW
    assert refused(replace(child, action="BUY"), ctx(parent=parent)) == \
        RefusalReason.CHILD_INVALID
    assert refused(child, ctx(parent=replace(parent, action="SELL"))) == \
        RefusalReason.CHILD_INVALID


def test_wrong_symbol_contract_id_account_sec_type_currency_refused():
    assert refused(order(symbol="SPY"), ctx()) == RefusalReason.WRONG_SYMBOL
    assert refused(order(con_id=999), ctx()) == RefusalReason.WRONG_CONTRACT_ID
    assert refused(order(), ctx(contract=replace(FACTS, con_id=0))) == \
        RefusalReason.WRONG_CONTRACT_ID
    assert refused(order(account="DU9990002"), ctx()) == RefusalReason.WRONG_ACCOUNT
    assert refused(order(sec_type="OPT"), ctx()) == RefusalReason.WRONG_SEC_TYPE
    assert refused(order(currency="EUR"), ctx()) == RefusalReason.WRONG_CURRENCY
    assert refused(order(order_ref=""), ctx()) == RefusalReason.ORDER_REF_MISSING


def test_tif_and_outside_rth_refused():
    assert refused(order(tif="GTC"), ctx()) == RefusalReason.TIF_NOT_ALLOWED
    assert refused(order(outside_rth=True), ctx()) == RefusalReason.OUTSIDE_RTH_FLAG


@pytest.mark.parametrize("now", [
    datetime(2026, 11, 11, 8, 0, tzinfo=UTC),  # 03:00 ET
    datetime(2026, 11, 11, 14, 29, tzinfo=UTC),  # 09:29 ET
    datetime(2026, 11, 11, 21, 0, tzinfo=UTC),  # 16:00 ET: the close itself is outside
    datetime(2026, 11, 14, 15, 0, tzinfo=UTC),  # a CLOSED day
])
def test_outside_liquid_hours_refused(now):
    assert refused(order(), ctx(now=now)) == RefusalReason.OUTSIDE_LIQUID_HOURS


def test_liquid_hours_and_clock_problems_refused():
    assert refused(order(), ctx(contract=replace(FACTS, liquid_hours="garbage"))) == \
        RefusalReason.LIQUID_HOURS_UNKNOWN
    assert refused(order(), ctx(contract=replace(FACTS, time_zone_id="Mars/Base"))) == \
        RefusalReason.LIQUID_HOURS_UNKNOWN
    assert refused(order(), ctx(now=datetime(2026, 11, 11, 10, 0))) == \
        RefusalReason.CLOCK_NOT_AWARE


@pytest.mark.parametrize("price", ["88.115", "88.1234", "0.005"])
def test_off_tick_price_refused_not_rounded(price):
    assert refused(order(limit_price=Decimal(price)), ctx()) == RefusalReason.OFF_TICK_PRICE


def test_price_shape_refused():
    assert refused(order(limit_price=None), ctx()) == RefusalReason.PRICE_INVALID
    assert refused(order(stop_price=Decimal(1)), ctx()) == RefusalReason.PRICE_INVALID
    assert refused(order(limit_price=Decimal(-1)), ctx()) == RefusalReason.PRICE_INVALID
    assert refused(order(quantity=Decimal("0.5")), ctx()) == RefusalReason.QUANTITY_INVALID
    assert refused(order(), ctx(contract=replace(FACTS, min_tick=None))) == \
        RefusalReason.TICK_UNKNOWN
    assert refused(order(), ctx(position=None)) == RefusalReason.POSITION_UNKNOWN


def test_parse_hours_accepts_both_ibkr_formats():
    new = parse_hours("20261111:0930-20261111:1600;20261112:CLOSED", "US/Eastern")
    old = parse_hours("20261111:0930-1600,1700-1800;20261112:CLOSED", "US/Eastern")
    assert len(new) == 1 and len(old) == 2
    assert new[0][0] == datetime(2026, 11, 11, 14, 30, tzinfo=UTC)
    assert new[0][1] == datetime(2026, 11, 11, 21, 0, tzinfo=UTC)
    assert parse_hours("", "US/Eastern") is None


# --- the durable counter --------------------------------------------------------------------------
MASKED = "DU****001"


def rec(c: SubmissionCounter, oid: int, kind: str = "SINGLE") -> int:
    return c.record(order_id=oid, order_ref=f"paper-test-1:TB-{oid}", kind=kind, action="BUY",
                    quantity=Decimal(1), order_type="LMT", limit_price=Decimal("88.12"),
                    stop_price=None, parent_id=0)


def opened(tmp_path, account=MASKED, caps=C.PAPER_TEST_1) -> SubmissionCounter:
    c = SubmissionCounter(tmp_path, caps, account)
    c.open()
    return c


def test_counter_is_durable_locked_and_capped(tmp_path):
    init_journal(tmp_path, C.PAPER_TEST_1, MASKED)
    c = opened(tmp_path)
    assert c.used == 0
    for i in range(6):
        assert rec(c, 100 + i) == i + 1
    with pytest.raises(CapReached):
        rec(c, 200)
    second = SubmissionCounter(tmp_path, C.PAPER_TEST_1, MASKED)
    with pytest.raises(CounterLocked):
        second.open()
    c.close()
    second.open()  # a restart: the count survives
    assert second.used == 6 and second.max_order_id == 105
    assert [e["action"] for e in second.entries] == ["BUY"] * 6
    with pytest.raises(CapReached):
        rec(second, 201)
    second.close()
    assert oct(os.stat(c.path).st_mode & 0o777) == "0o600"


def test_the_counter_never_creates_the_journal(tmp_path):
    c = SubmissionCounter(tmp_path, C.PAPER_TEST_1, MASKED)
    with pytest.raises(CounterMissing):
        c.open()
    assert not c.path.exists() and c.used is None


def test_init_journal_runs_once_and_records_the_header(tmp_path):
    path = init_journal(tmp_path, C.PAPER_TEST_1, MASKED)
    with pytest.raises(JournalExists):
        init_journal(tmp_path, C.PAPER_TEST_1, MASKED)
    import json
    header = json.loads(path.read_bytes().splitlines()[0])
    assert header["experiment"] == "paper-test-1" and header["account_masked"] == MASKED
    assert header["caps_digest"] == caps_digest(C.PAPER_TEST_1) and header["created_at"]
    assert PAPER not in path.read_text()


@pytest.mark.parametrize("which", ["account", "caps"])
def test_a_header_for_another_account_or_caps_refuses(tmp_path, which):
    init_journal(tmp_path, C.PAPER_TEST_1, MASKED)
    with pytest.raises(CounterCorrupt):
        if which == "account":
            opened(tmp_path, account="DU****002")
        else:
            opened(tmp_path, caps=replace(C.PAPER_TEST_1, max_submissions=60))


def _five(tmp_path) -> SubmissionCounter:
    init_journal(tmp_path, C.PAPER_TEST_1, MASKED)
    c = opened(tmp_path)
    for i in range(5):
        rec(c, 100 + i)
    c.close()
    return c


@pytest.mark.parametrize("damage", ["truncate_tail", "drop_middle", "edit_field", "torn",
                                    "swap", "delete_head", "delete_journal", "garbage"])
def test_truncation_and_tampering_are_detected(tmp_path, damage):
    c = _five(tmp_path)
    lines = c.path.read_bytes().splitlines(keepends=True)
    if damage == "truncate_tail":  # the reviewer's probe: 5 -> 1 used to parse
        c.path.write_bytes(b"".join(lines[:2]))
    elif damage == "drop_middle":
        c.path.write_bytes(b"".join(lines[:2] + lines[3:]))
    elif damage == "edit_field":
        c.path.write_bytes(b"".join(lines[:3] + [lines[3].replace(b'"BUY"', b'"SELL"')]
                                    + lines[4:]))
    elif damage == "torn":
        c.path.write_bytes(b"".join(lines)[:-5])
    elif damage == "swap":
        c.path.write_bytes(b"".join([lines[0], lines[2], lines[1], *lines[3:]]))
    elif damage == "delete_head":
        c.head.unlink()
    elif damage == "delete_journal":
        c.path.unlink()
    else:
        c.path.write_bytes(b"garbage\n")
    with pytest.raises(CounterCorrupt):
        opened(tmp_path)


def test_a_crash_between_the_append_and_the_head_anchor_is_tolerated(tmp_path):
    c = _five(tmp_path)
    import json
    lines = c.path.read_bytes().splitlines()
    c.head.write_bytes(json.dumps({"seq": 4, "hash": json.loads(lines[4])["hash"]}).encode())
    assert opened(tmp_path).used == 5


# --- status mapping -------------------------------------------------------------------------------
@pytest.mark.parametrize(("raw", "expected"), [
    ("PendingSubmit", OrderStatus.PENDING_SUBMIT), ("ApiPending", OrderStatus.PENDING_SUBMIT),
    ("PreSubmitted", OrderStatus.PRE_SUBMITTED), ("Submitted", OrderStatus.SUBMITTED),
    ("PendingCancel", OrderStatus.PENDING_CANCEL), ("Filled", OrderStatus.FILLED),
    ("Cancelled", OrderStatus.CANCELLED), ("ApiCancelled", OrderStatus.API_CANCELLED),
    ("Inactive", OrderStatus.INACTIVE), ("Bogus", OrderStatus.UNKNOWN), ("", OrderStatus.UNKNOWN),
])
def test_status_strings_map(raw, expected):
    assert map_status(raw) == expected


def test_partial_fill_and_out_of_order_merge():
    assert map_status("Submitted", Decimal(1), Decimal(1)) == OrderStatus.PARTIALLY_FILLED
    s = merge_status(None, OrderStatus.SUBMITTED, filled=Decimal(0), total=Decimal(1))
    s = merge_status(s, OrderStatus.FILLED, filled=Decimal(1), total=Decimal(1))
    assert merge_status(s, OrderStatus.SUBMITTED, filled=Decimal(1), total=Decimal(1)) == \
        OrderStatus.FILLED
    assert merge_status(s, OrderStatus.UNKNOWN, filled=Decimal(1), total=Decimal(1)) == \
        OrderStatus.FILLED
    assert merge_status(OrderStatus.CANCELLED, OrderStatus.FILLED, filled=Decimal(1),
                        total=Decimal(1)) == OrderStatus.FILLED


def test_order_error_classes():
    assert classify_order_error(202) == "CANCELLED"
    assert classify_order_error(201) == "REJECTED"
    assert classify_order_error(321) == "REJECTED"
    assert classify_order_error(110) == "REJECTED"
    assert classify_order_error(399) == "NOTICE"
    assert classify_order_error(2109) == "NOTICE"
    assert classify_order_error(10147) == "CANCEL_FAILED"
    assert classify_order_error(512) == "SEND_FAILED"


def test_broker_time_formats():
    assert parse_broker_time("20261111 10:00:01 US/Eastern") == \
        datetime(2026, 11, 11, 15, 0, 1, tzinfo=UTC)
    assert parse_broker_time("20261111-15:00:01") == datetime(2026, 11, 11, 15, 0, 1, tzinfo=UTC)
    assert parse_broker_time("nonsense") is None
    assert parse_broker_time("") is None


# --- review round 2 -----------------------------------------------------------------------------
def test_buy_stop_entry_refused_a_buy_must_be_a_limit():
    """A BUY STP is a market order once triggered: outside "limit entry" and "no market orders"."""
    buy_stop = order(order_type="STP", limit_price=None, stop_price=Decimal("88.50"))
    assert refused(buy_stop, ctx()) == RefusalReason.BUY_NOT_LIMIT
    assert refused(buy_stop, ctx(position=Decimal(0), working=())) == RefusalReason.BUY_NOT_LIMIT


def test_a_stop_is_allowed_only_as_a_protective_or_reducing_sell():
    sell_stop = order(action="SELL", order_type="STP", limit_price=None,
                      stop_price=Decimal("87.00"))
    assert refused(sell_stop, ctx()) == RefusalReason.STOP_NOT_PROTECTIVE  # nothing long
    assert order_guard(sell_stop, ctx(position=Decimal(1))) is ALLOW  # reduces a held long
    child = replace(sell_stop, order_id=101, parent_id=100)
    assert order_guard(child, ctx(parent=order())) is ALLOW  # protects a bracketed long


def test_inactive_is_not_sticky():
    one = Decimal(1)
    assert merge_status(OrderStatus.INACTIVE, OrderStatus.SUBMITTED, filled=Decimal(0),
                        total=one) == OrderStatus.SUBMITTED
    assert merge_status(OrderStatus.INACTIVE, OrderStatus.CANCELLED, filled=Decimal(0),
                        total=one) == OrderStatus.CANCELLED
    assert merge_status(OrderStatus.CANCELLED, OrderStatus.INACTIVE, filled=Decimal(0),
                        total=one) == OrderStatus.CANCELLED


# The reviewer's probes: the broker's position read lags a fill this session already received.
def test_probe_second_sell_refused_when_the_position_read_lags_the_closing_fill():
    """The closing SELL filled (own fills imply 0), the read still says 1, the stop is gone."""
    sell = order(order_id=130, action="SELL", limit_price=Decimal("88.00"))
    c = ctx(position=Decimal(1), implied_position=Decimal(0))
    assert refused(sell, c) == RefusalReason.POSITION_DISAGREEMENT


def test_probe_second_buy_refused_when_the_position_read_lags_the_entry_fill():
    """The entry filled (own fills imply 1), the read still says 0, no buy is working."""
    c = ctx(position=Decimal(0), implied_position=Decimal(1))
    assert refused(order(order_id=130), c) == RefusalReason.POSITION_DISAGREEMENT


def test_mirror_sell_refused_when_own_fills_show_more_than_the_read():
    sell = order(order_id=130, action="SELL", limit_price=Decimal("88.00"))
    assert refused(sell, ctx(position=Decimal(0), implied_position=Decimal(1))) == \
        RefusalReason.POSITION_DISAGREEMENT


def test_mirror_buy_refused_when_the_read_shows_more_than_own_fills():
    assert refused(order(order_id=130), ctx(position=Decimal(1), implied_position=Decimal(0))) \
        == RefusalReason.POSITION_DISAGREEMENT


def test_disagreement_also_stops_children_and_amendments_and_unknown_own_fills_refuse():
    child = order(order_id=101, action="SELL", order_type="STP", limit_price=None,
                  stop_price=Decimal("87.00"), parent_id=100)
    assert refused(child, ctx(parent=order(), implied_position=Decimal(1))) == \
        RefusalReason.POSITION_DISAGREEMENT
    assert refused(order(), ctx(implied_position=None)) == RefusalReason.POSITION_UNKNOWN
    assert order_guard(order(), ctx(position=Decimal(0), implied_position=Decimal(0))) is ALLOW


def test_guard_refuses_when_the_broker_shows_more_than_the_journal():
    assert refused(order(), ctx(submissions_used=0, broker_submissions=2)) == \
        RefusalReason.SUBMISSION_COUNT_MISMATCH
    assert refused(order(), ctx(broker_submissions=None)) == \
        RefusalReason.SUBMISSION_COUNT_UNKNOWN
    assert order_guard(order(), ctx(submissions_used=3, broker_submissions=2)) is ALLOW


def _load_script(name: str):
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_init_script_is_offline_and_runs_once(tmp_path, monkeypatch, capsys):
    import socket

    def no_network(*a, **k):
        raise AssertionError("the init script must not open a socket")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    script = _load_script("ibkr_paper_init_journal")
    cfg_path = write_cfg(tmp_path)
    assert script.main(["--config", str(cfg_path)]) == 0
    assert PAPER not in capsys.readouterr().out
    assert script.main(["--config", str(cfg_path)]) == 1  # never twice
    c = SubmissionCounter(tmp_path / "state", C.PAPER_TEST_1, "DU…01")
    c.open()
    assert c.used == 0
    c.close()


# --- no public seam can disable or replace a safety check ---------------------------------------
_SEAM_TOKENS = {"preflight", "clock", "now", "today", "guard", "counter", "journal",
                "identity", "gate", "skip", "bypass", "disable", "check", "checks", "verify",
                "hook", "hooks", "stub", "fake", "override", "unsafe", "force", "loader",
                "reader", "validator", "evaluate", "permit", "policy", "transport", "factory",
                "data"}
# The guard's pure inputs: plain values the SESSION builds itself (from its own counter, clock,
# reads and pinned contract) immediately before each placeOrder; they are not session seams.
_PURE_GUARD_INPUTS = {"GuardContext", "ProposedOrder", "WorkingOrder", "ContractFacts",
                      "order_guard", "Refusal", "Allow", "parse_hours"}


def _public_callables():
    import importlib
    import inspect
    import pkgutil

    import tradingbot.ibkr_orders as pkg
    for info in pkgutil.iter_modules(pkg.__path__):
        try:
            mod = importlib.import_module(f"tradingbot.ibkr_orders.{info.name}")
        except ModuleNotFoundError as exc:  # the SDK module, when the SDK is blocked
            if "ibapi" in str(exc):
                continue
            raise
        for name, obj in vars(mod).items():
            if name.startswith("_") or getattr(obj, "__module__", None) != mod.__name__:
                continue
            if name in _PURE_GUARD_INPUTS or not callable(obj):
                continue
            if inspect.isclass(obj) and issubclass(obj, BaseException):
                continue
            yield f"{info.name}.{name}", obj
            if inspect.isclass(obj):
                for m, f in vars(obj).items():
                    if (not m.startswith("_") or m == "__init__") and inspect.isfunction(f):
                        yield f"{info.name}.{name}.{m}", f


def test_no_public_callable_accepts_a_safety_seam():
    import inspect
    found = []
    for where, obj in _public_callables():
        try:
            params = inspect.signature(obj).parameters.values()
        except (TypeError, ValueError):
            continue
        for p in params:
            if p.kind is p.VAR_KEYWORD and (where.endswith("__init__") or "experiment" in where):
                found.append(f"{where}(**{p.name})")  # could forward anything
            tokens = set(p.name.lower().split("_"))
            if tokens & _SEAM_TOKENS:
                found.append(f"{where}({p.name})")
    assert found == []


def test_the_order_session_constructor_takes_only_the_config_and_timeouts():
    pytest.importorskip("ibapi")
    import inspect

    from tradingbot.ibkr_orders.session import PaperOrderSession
    params = inspect.signature(PaperOrderSession.__init__).parameters
    assert list(params)[:2] == ["self", "config"]
    assert all(n.endswith(("_timeout", "_wait", "_seconds")) for n in list(params)[2:]), params
