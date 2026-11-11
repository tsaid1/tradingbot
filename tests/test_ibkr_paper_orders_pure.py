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
    SubmissionCounter,
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
def test_counter_is_durable_locked_and_capped(tmp_path):
    c = SubmissionCounter(tmp_path, "paper-test-1", 6)
    c.open()
    assert c.used == 0
    for i in range(6):
        assert c.record(order_id=100 + i, order_ref=f"TB-{i}", kind="SINGLE") == i + 1
    with pytest.raises(CapReached):
        c.record(order_id=200, order_ref="TB-x", kind="SINGLE")
    second = SubmissionCounter(tmp_path, "paper-test-1", 6)
    with pytest.raises(CounterLocked):
        second.open()
    c.close()
    second.open()  # a restart: the count survives
    assert second.used == 6 and second.max_order_id == 105
    with pytest.raises(CapReached):
        second.record(order_id=201, order_ref="TB-y", kind="SINGLE")
    second.close()
    assert oct(os.stat(c.path).st_mode & 0o777) == "0o600"


@pytest.mark.parametrize("content", [b'{"n": 1}', b"garbage\n",
                                     b'{"n": 2, "experiment": "paper-test-1", "order_id": 1}\n',
                                     b'{"n": 1, "experiment": "other", "order_id": 1}\n'])
def test_counter_refuses_a_journal_it_cannot_read_exactly(tmp_path, content):
    c = SubmissionCounter(tmp_path, "paper-test-1", 6)
    c.dir.mkdir(mode=0o700)
    c.path.write_bytes(content)
    with pytest.raises(CounterCorrupt):
        c.open()


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
