"""Scripted fake TWS for SDK tests. Requires the real ``ibapi``; imported only after importorskip.

``FakeConnection`` replaces ``ibapi.client.Connection`` (same five methods, no socket), so the real
``EClient.connect`` handshake, ``EReader`` thread, ``EClient.run`` loop and ``Decoder`` all run
unchanged. The server decodes the client's protobuf requests and answers with protobuf frames built
from the SDK's own message classes. Test-only synthetic data: no real account IDs.
"""
from __future__ import annotations

import queue
import struct
import threading
from collections.abc import Callable

from ibapi import comm
from ibapi.common import PROTOBUF_MSG_ID
from ibapi.message import IN, OUT
from ibapi.protobuf.AccountSummary_pb2 import AccountSummary
from ibapi.protobuf.AccountSummaryEnd_pb2 import AccountSummaryEnd
from ibapi.protobuf.AccountSummaryRequest_pb2 import AccountSummaryRequest
from ibapi.protobuf.ApiConfig_pb2 import ApiConfig
from ibapi.protobuf.ApiSettingsConfig_pb2 import ApiSettingsConfig
from ibapi.protobuf.CancelAccountSummary_pb2 import CancelAccountSummary
from ibapi.protobuf.CancelContractData_pb2 import CancelContractData
from ibapi.protobuf.CancelMarketData_pb2 import CancelMarketData
from ibapi.protobuf.CancelPositions_pb2 import CancelPositions
from ibapi.protobuf.ConfigRequest_pb2 import ConfigRequest
from ibapi.protobuf.ConfigResponse_pb2 import ConfigResponse
from ibapi.protobuf.Contract_pb2 import Contract as ContractProto
from ibapi.protobuf.ContractData_pb2 import ContractData
from ibapi.protobuf.ContractDataEnd_pb2 import ContractDataEnd
from ibapi.protobuf.ContractDataRequest_pb2 import ContractDataRequest
from ibapi.protobuf.ContractDetails_pb2 import ContractDetails as ContractDetailsProto
from ibapi.protobuf.ErrorMessage_pb2 import ErrorMessage
from ibapi.protobuf.ManagedAccounts_pb2 import ManagedAccounts
from ibapi.protobuf.MarketDataRequest_pb2 import MarketDataRequest
from ibapi.protobuf.MarketDataType_pb2 import MarketDataType
from ibapi.protobuf.MarketDataTypeRequest_pb2 import MarketDataTypeRequest
from ibapi.protobuf.NextValidId_pb2 import NextValidId
from ibapi.protobuf.Position_pb2 import Position
from ibapi.protobuf.PositionEnd_pb2 import PositionEnd
from ibapi.protobuf.PositionsRequest_pb2 import PositionsRequest
from ibapi.protobuf.StartApiRequest_pb2 import StartApiRequest
from ibapi.protobuf.TickPrice_pb2 import TickPrice
from ibapi.protobuf.TickSnapshotEnd_pb2 import TickSnapshotEnd
from ibapi.protobuf.TickString_pb2 import TickString
from ibapi.server_versions import MAX_CLIENT_VER

PAPER = "DU9990001"  # synthetic
OTHER_PAPER = "DU9990002"  # synthetic

_REQUESTS = {
    OUT.START_API: ("START_API", StartApiRequest),
    OUT.REQ_CONTRACT_DATA: ("REQ_CONTRACT_DATA", ContractDataRequest),
    OUT.CANCEL_CONTRACT_DATA: ("CANCEL_CONTRACT_DATA", CancelContractData),
    OUT.REQ_ACCOUNT_SUMMARY: ("REQ_ACCOUNT_SUMMARY", AccountSummaryRequest),
    OUT.CANCEL_ACCOUNT_SUMMARY: ("CANCEL_ACCOUNT_SUMMARY", CancelAccountSummary),
    OUT.REQ_POSITIONS: ("REQ_POSITIONS", PositionsRequest),
    OUT.CANCEL_POSITIONS: ("CANCEL_POSITIONS", CancelPositions),
    OUT.REQ_MKT_DATA: ("REQ_MKT_DATA", MarketDataRequest),
    OUT.CANCEL_MKT_DATA: ("CANCEL_MKT_DATA", CancelMarketData),
    OUT.REQ_MARKET_DATA_TYPE: ("REQ_MARKET_DATA_TYPE", MarketDataTypeRequest),
    OUT.REQ_CONFIG: ("REQ_CONFIG", ConfigRequest),
}


def frame(in_id: int, proto) -> bytes:
    return comm.make_msg_proto(in_id + PROTOBUF_MSG_ID, proto.SerializeToString())


def text_frame(in_id: int, *fields: str) -> bytes:
    """Legacy text-encoded message (raw 4-byte id, NUL-separated fields), as TWS >= 201 frames it."""
    body = in_id.to_bytes(4, "big") + "".join(f"{f}\0" for f in fields).encode()
    return struct.pack("!I", len(body)) + body


def error_frame(req_id: int, code: int, msg: str) -> bytes:
    return frame(IN.ERR_MSG, ErrorMessage(id=req_id, errorTime=0, errorCode=code, errorMsg=msg))


def tlt_contract_frame(req_id: int, con_id: int = 15547841, primary: str = "NASDAQ") -> bytes:
    c = ContractProto(conId=con_id, symbol="TLT", secType="STK", exchange="SMART",
                      primaryExch=primary, currency="USD", localSymbol="TLT", tradingClass="NMS")
    d = ContractDetailsProto(minTick="0.01", timeZoneId="US/Eastern", longName="ISHARES 20+ YEAR",
                             tradingHours="20261111:0400-20261111:2000",
                             liquidHours="20261111:0930-20261111:1600")
    return frame(IN.CONTRACT_DATA, ContractData(reqId=req_id, contract=c, contractDetails=d))


def delayed_quote_frames(req_id: int) -> list[bytes]:
    return [
        frame(IN.MARKET_DATA_TYPE, MarketDataType(reqId=req_id, marketDataType=3)),
        error_frame(req_id, 10167, "Requested market data is not subscribed. Displaying delayed "
                                   "market data."),
        frame(IN.TICK_PRICE, TickPrice(reqId=req_id, tickType=66, price=88.10, size="300",
                                       attrMask=0)),
        frame(IN.TICK_PRICE, TickPrice(reqId=req_id, tickType=67, price=88.12, size="500",
                                       attrMask=0)),
        frame(IN.TICK_PRICE, TickPrice(reqId=req_id, tickType=68, price=88.11, size="100",
                                       attrMask=0)),
        frame(IN.TICK_STRING, TickString(reqId=req_id, tickType=88, value="1794412800")),
        frame(IN.TICK_SNAPSHOT_END, TickSnapshotEnd(reqId=req_id)),
    ]


class FakeConnection:
    """Same surface as ``ibapi.connection.Connection``; bytes go to/from a ``FakeTws``."""

    def __init__(self, server: FakeTws, host: str, port: int) -> None:
        self.server, self.host, self.port = server, host, port
        self.wrapper = None
        self.lock = threading.Lock()
        self._open = False
        self._inbox: queue.Queue[bytes] = queue.Queue()

    def connect(self) -> None:
        self._open = True
        self.server.connections.append(self)

    def disconnect(self) -> None:
        self._open = False
        self._inbox.put(b"")

    def isConnected(self) -> bool:
        return self._open

    def sendMsg(self, msg: bytes) -> int:
        if not self._open:
            return 0
        self.server.wire.append(bytes(msg))  # every byte string that reached the "socket"
        self.server.receive(self, msg)
        return len(msg)

    def recvMsg(self) -> bytes:
        if not self._open:
            return b""
        try:
            return self._inbox.get(timeout=0.05)
        except queue.Empty:
            return b""

    def push(self, data: bytes) -> None:
        if self._open:
            self._inbox.put(data)


Handler = Callable[["FakeTws", FakeConnection, object], None]


class FakeTws:
    def __init__(self) -> None:
        self.server_version = MAX_CLIENT_VER
        self.connection_time = "20261111 15:30:00 Greenwich Mean Time"
        self.handshake = True
        self.refuse = False
        self.managed_accounts: str | list[str] | None = PAPER  # a list sends several callbacks
        self.send_next_valid_id = True
        self.summary_account = PAPER
        self.positions = [(PAPER, "TLT", 15547841, "10")]
        self.fail_send: set[str] = set()  # request names whose send raises OSError
        # ApiSettingsConfig fields returned to reqConfigProtoBuf (None = field absent)
        self.api_settings: dict = {"readOnlyApi": True, "socketPort": 7497,
                                   "allowLocalhostOnly": True}
        self.extra_after_start: list[bytes] = []  # raw frames pushed right after START_API
        self.connections: list[FakeConnection] = []
        self.wire: list[bytes] = []
        self.requests: list[tuple[str, object]] = []
        self._handshaken: set[int] = set()
        self.on: dict[str, Handler] = {
            "START_API": FakeTws.start_api,
            "REQ_CONTRACT_DATA": lambda s, c, r: (c.push(tlt_contract_frame(r.reqId)),
                                                  c.push(frame(IN.CONTRACT_DATA_END,
                                                               ContractDataEnd(reqId=r.reqId)))),
            "REQ_ACCOUNT_SUMMARY": FakeTws.account_summary,
            "REQ_POSITIONS": FakeTws.positions_handler,
            "REQ_MKT_DATA": lambda s, c, r: [c.push(f) for f in delayed_quote_frames(r.reqId)],
            "REQ_CONFIG": FakeTws.config_handler,
        }

    # Connection factory with the signature EClient.connect uses: Connection(host, port)
    def connection_factory(self, host: str, port: int) -> FakeConnection:
        conn = FakeConnection(self, host, port)
        if self.refuse:
            def refused(msg: bytes) -> int:
                raise ConnectionRefusedError(61, "Connection refused")
            conn.sendMsg = refused  # type: ignore[method-assign]
        return conn

    def names(self) -> list[str]:
        return [n for n, _ in self.requests]

    def drop(self) -> None:
        """TWS closes the socket."""
        for c in self.connections:
            c.disconnect()

    def receive(self, conn: FakeConnection, msg: bytes) -> None:
        if id(conn) not in self._handshaken:
            assert msg.startswith(b"API\0")
            self._handshaken.add(id(conn))
            if self.handshake:
                payload = f"{self.server_version}\0{self.connection_time}\0".encode()
                conn.push(struct.pack("!I", len(payload)) + payload)
            return
        size = struct.unpack("!I", msg[:4])[0]
        body = msg[4:4 + size]
        msg_id = int.from_bytes(body[:4], "big")
        assert msg_id > PROTOBUF_MSG_ID, f"unexpected text-encoded request {msg_id}"
        name, cls = _REQUESTS.get(msg_id - PROTOBUF_MSG_ID, (f"OUT_{msg_id - PROTOBUF_MSG_ID}",
                                                            None))
        proto = cls.FromString(body[4:]) if cls is not None else body[4:]
        self.requests.append((name, proto))
        if name in self.fail_send:
            raise OSError("simulated send failure")
        handler = self.on.get(name)
        if handler is not None:
            handler(self, conn, proto)

    @staticmethod
    def start_api(s: FakeTws, conn: FakeConnection, req) -> None:
        if s.send_next_valid_id:
            conn.push(frame(IN.NEXT_VALID_ID, NextValidId(orderId=1)))
        for code, text in ((2104, "Market data farm connection is OK:usfarm"),
                           (2106, "HMDS data farm connection is OK:ushmds"),
                           (2158, "Sec-def data farm connection is OK:secdefnj")):
            conn.push(error_frame(-1, code, text))
        for raw in s.extra_after_start:
            conn.push(raw)
        accounts = s.managed_accounts
        for value in accounts if isinstance(accounts, list) else [accounts]:
            if value is not None:
                conn.push(frame(IN.MANAGED_ACCTS, ManagedAccounts(accountsList=value)))

    @staticmethod
    def account_summary(s: FakeTws, conn: FakeConnection, req) -> None:
        values = {"AccountType": ("INDIVIDUAL", ""), "NetLiquidation": ("1000000.00", "USD"),
                  "TotalCashValue": ("1000000.00", "USD"), "BuyingPower": ("4000000.00", "USD"),
                  "AvailableFunds": ("1000000.00", "USD")}
        for tag in req.tags.split(","):
            value, ccy = values[tag]
            conn.push(frame(IN.ACCOUNT_SUMMARY, AccountSummary(
                reqId=req.reqId, account=s.summary_account, tag=tag, value=value, currency=ccy)))
        conn.push(frame(IN.ACCOUNT_SUMMARY_END, AccountSummaryEnd(reqId=req.reqId)))

    @staticmethod
    def positions_handler(s: FakeTws, conn: FakeConnection, req) -> None:
        for account, symbol, con_id, qty in s.positions:
            c = ContractProto(conId=con_id, symbol=symbol, secType="STK", exchange="NASDAQ",
                              currency="USD")
            conn.push(frame(IN.POSITION_DATA, Position(account=account, contract=c, position=qty,
                                                       avgCost=87.5)))
        conn.push(frame(IN.POSITION_END, PositionEnd()))

    @staticmethod
    def config_handler(s: FakeTws, conn: FakeConnection, req) -> None:
        settings = ApiSettingsConfig(**{k: v for k, v in s.api_settings.items() if v is not None})
        conn.push(frame(IN.CONFIG_RESPONSE, ConfigResponse(
            reqId=req.reqId, api=ApiConfig(settings=settings))))
