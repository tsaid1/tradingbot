"""Review fixes for historical bars, SDK part: S2 (a bar without its protobuf message), S3(c)
(no stale pending entry when a cancel raises), N1 / N2 (strict id-20 frame check), N6 (end-marker
strings redacted). Real ibapi 1052.1 over the fake TWS; no socket. Synthetic data only."""
from __future__ import annotations

import pytest

pytest.importorskip("ibapi")

from _ibkr_fake_tws import PAPER, hist_bar, hist_data_frame, hist_end_frame, text_frame
from ibapi import comm
from ibapi.common import PROTOBUF_MSG_ID
from ibapi.contract import Contract
from ibapi.message import IN, OUT
from ibapi.protobuf.HistoricalDataRequest_pb2 import HistoricalDataRequest
from test_ibkr_historical_bars_sdk import E0, answer, req, session, tws  # noqa: F401

from tradingbot.ibkr.errors import ReadOnlyViolation
from tradingbot.ibkr.session import _check_historical_frame


# --- S2: a bar must arrive inside its protobuf message ------------------------------------------
def test_text_framed_bars_with_empty_prices_fail_the_read(tws):  # noqa: F811
    tws.on["REQ_HISTORICAL_DATA"] = answer(
        lambda rid: text_frame(IN.HISTORICAL_DATA, str(rid), "1", str(E0 - 60), "", "", "", "",
                               "10", "1", "1"),
        hist_end_frame)
    with session() as s:
        r = s.historical_bars(req())
    assert (r.status, r.reason, r.bars) == ("FAILED", "MALFORMED", ())
    assert "protobuf" in r.detail


def test_text_framed_bars_with_valid_prices_still_fail_the_read(tws):  # noqa: F811
    tws.on["REQ_HISTORICAL_DATA"] = answer(
        lambda rid: text_frame(IN.HISTORICAL_DATA, str(rid), "1", str(E0 - 60), "1", "2", "0.5",
                               "1.5", "10", "1", "1"),
        hist_end_frame)
    with session() as s:
        r = s.historical_bars(req())
    assert r.reason == "MALFORMED" and r.bars == ()


@pytest.mark.parametrize("prices", [(0.0, 0.0, 0.0, 0.0), (-1.0, -1.0, -1.0, -1.0),
                                    (1.0, 2.0, -1.0, 1.5)])
def test_zero_and_minus_one_prices_fail_the_read_on_the_protobuf_path(tws, prices):  # noqa: F811
    tws.on["REQ_HISTORICAL_DATA"] = answer(
        lambda rid: hist_data_frame(rid, [hist_bar(str(E0 - 60), *prices)]), hist_end_frame)
    with session() as s:
        r = s.historical_bars(req())
    assert r.reason == "MALFORMED" and "positive" in r.detail


# --- S3(c): no stale pending entry when a cancel raises anything --------------------------------
@pytest.mark.parametrize("exc", [ReadOnlyViolation("refused cancel"), KeyboardInterrupt()])
def test_a_cancel_that_raises_leaves_no_pending_entry(tws, exc):  # noqa: F811
    with session() as s:
        def boom(rid):
            raise exc

        s._client.cancelHistoricalData = boom
        with pytest.raises(type(exc)):
            s.historical_bars(req(), timeout=0.3)
        assert s._pending == {}
        del s._client.cancelHistoricalData


# --- N1 / N2: strict id-20 frame check ----------------------------------------------------------
def _good() -> HistoricalDataRequest:
    from ibapi.client_utils import createHistoricalDataRequestProto

    c = Contract()
    c.conId, c.exchange = 100000001, "NYMEX"
    return createHistoricalDataRequestProto(9001, c, "20260309-14:15:00", "1800 S", "1 min",
                                            "TRADES", False, 2, False, [])


def _frame(payload: bytes) -> bytes:
    return comm.make_msg_proto(OUT.REQ_HISTORICAL_DATA + PROTOBUF_MSG_ID, payload)


def test_the_sdk_built_request_passes_the_strict_check():
    _check_historical_frame(_frame(_good().SerializeToString()), 226)


def _variants() -> dict[str, bytes]:
    good = _good().SerializeToString()
    fd1 = _good()
    fd1.formatDate = 1
    no_fd = _good()
    no_fd.ClearField("formatDate")
    keep_false = _good()
    keep_false.keepUpToDate = False  # present, even if false
    partial = HistoricalDataRequest(reqId=9001, contract=_good().contract)
    return {
        "duplicated keepUpToDate ending in 0": good + b"\x48\x01\x48\x00",
        "keepUpToDate with the wrong wire type": good + b"\x4a\x01\x01",
        "keepUpToDate fixed32": good + b"\x4d\x01\x00\x00\x00",
        "unknown field": good + b"\xf8\x01\x01",
        "duplicated reqId": good + b"\x08\x01",
        "empty body": b"",
        "body cut at a field boundary": partial.SerializeToString(),
        "body cut mid-field": good[: len(good) - 3],
        "formatDate=1": fd1.SerializeToString(),
        "formatDate absent": no_fd.SerializeToString(),
        "keepUpToDate present and false": keep_false.SerializeToString(),
        "non-minimal varint": good.replace(b"\x40\x02", b"\x40\x82\x00"),
    }


@pytest.mark.parametrize("name", list(_variants()))
def test_strict_check_refuses_the_reviewers_passing_cases(name):
    payload = _variants()[name]
    assert payload != _good().SerializeToString()
    with pytest.raises(ReadOnlyViolation):
        _check_historical_frame(_frame(payload), 226)


@pytest.mark.parametrize("name", ["duplicated keepUpToDate ending in 0",
                                  "keepUpToDate with the wrong wire type", "empty body",
                                  "body cut at a field boundary", "formatDate=1"])
def test_strict_check_refuses_at_the_connection_and_nothing_reaches_the_wire(tws, name):  # noqa: F811
    with session() as s:
        before = list(tws.wire)
        with pytest.raises(ReadOnlyViolation):
            s._client.conn.sendMsg(_frame(_variants()[name]))
        with pytest.raises(ReadOnlyViolation):
            s._client.sendMsgProtoBuf(OUT.REQ_HISTORICAL_DATA + PROTOBUF_MSG_ID,
                                      _variants()[name])
        assert tws.wire == before


def test_bound_request_with_format_date_1_is_refused(tws):  # noqa: F811
    with session() as s:
        before = list(tws.wire)
        c = Contract()
        c.conId, c.exchange = 100000001, "NYMEX"
        with pytest.raises(ReadOnlyViolation, match="formatDate"):
            s._client.reqHistoricalData(1, c, "20260309-14:15:00", "1800 S", "1 min", "TRADES",
                                        0, 1, False, [])
        assert tws.wire == before


# --- N6: end-marker strings are redacted --------------------------------------------------------
def test_end_marker_strings_are_redacted(tws):  # noqa: F811
    tws.on["REQ_HISTORICAL_DATA"] = answer(
        lambda rid: hist_data_frame(rid, []),
        lambda rid: hist_end_frame(rid, start=f"20260309 14:00:00 {PAPER}", end="U7654321 x"))
    with session() as s:
        r = s.historical_bars(req())
    assert PAPER not in r.end_marker_start and "U7654321" not in r.end_marker_end
    assert "DU…01" in r.end_marker_start
