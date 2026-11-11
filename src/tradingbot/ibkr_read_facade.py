"""An SDK-free facade for converted read-only callbacks over one accepted router. NON_AUTHORITATIVE.

``ReadCallbackFacade`` exposes fixed plain methods named like the converted callbacks of the existing
read-only streams: contract details, the quote snapshot, the account summary and the positions
snapshot, plus the two direct notifications. Each call is forwarded once, unchanged, to the same-named
entry of one exact ``CallbackRouter``. Contract details and the explicitly selected SDK 1052-order
``error`` go through the accepted ``ContractCallbackFacade``, by composition. The router and its
normalizers do all validation, snapshotting, attribution and evidence. Nothing is normalized,
retained or decided here.

* **Fixed call forms.** Every entry takes exactly its documented number of positional arguments, and
  ``error`` positional arguments only, judged by the router's 1052 rules. A keyword argument or any
  other form becomes the router's persistent ``unsupported_callback`` refusal, by the entry's fixed
  name. No request id or value is taken from a refused call, and nothing raises.
* **One explicit refusal.** ``managedAccounts`` has no accepted sink, so it is always the router's fixed
  refusal and its value is never read. No other callback is covered, intercepted or guessed at: there
  is no ``__getattr__``, reflection, dynamic dispatch or superclass, and protobuf variants are not
  converted callbacks.
* **Ownership stays with the router.** The facade holds only its router and the composed contract
  facade. It never touches the queue, consumer, registration, shutdown or clock.

It imports no SDK, subclasses nothing, opens no socket and starts no thread. It is not a complete
callback interface or a connected adapter, and it establishes no SDK compatibility.
See docs/IBKR_READ_FACADE.md.
"""
from __future__ import annotations

from tradingbot.ibkr_contract_facade import ContractCallbackFacade
from tradingbot.paper_readiness.callback_router import CallbackRouter

__all__ = ["ReadCallbackFacade"]


class ReadCallbackFacade:
    """Forwards converted read-only callbacks to one accepted ``CallbackRouter``."""

    __slots__ = ("_contract", "_router")

    def __init__(self, router: CallbackRouter) -> None:
        if type(router) is not CallbackRouter:
            raise TypeError("router must be a CallbackRouter")
        self._router = router
        self._contract = ContractCallbackFacade(router)  # contract details and the 1052 error

    def _refused(self, name: str, args: tuple, kwargs: dict, arity: int) -> bool:
        """A keyword or another argument count is the router's fixed-name refusal; nothing is read."""
        if kwargs or len(args) != arity:
            self._router.unsupported_callback(name)
            return True
        return False

    # ------------------------------------------------------------------ contract details and errors

    def contractDetails(self, *args: object, **kwargs: object) -> None:
        """``contractDetails(reqId, contractDetails)``, through the accepted contract facade."""
        self._contract.contractDetails(*args, **kwargs)

    def contractDetailsEnd(self, *args: object, **kwargs: object) -> None:
        """``contractDetailsEnd(reqId)``, through the accepted contract facade."""
        self._contract.contractDetailsEnd(*args, **kwargs)

    def error(self, *args: object, **kwargs: object) -> None:
        """The SDK 1052 order ``reqId, errorTime, errorCode, errorString[, advancedOrderRejectJson]``,
        positional only, through the accepted contract facade to ``error_1052``."""
        self._contract.error(*args, **kwargs)

    # ------------------------------------------------------------------ quote snapshot

    def marketDataType(self, *args: object, **kwargs: object) -> None:
        """``marketDataType(reqId, marketDataType)``."""
        if not self._refused("marketDataType", args, kwargs, 2):
            self._router.marketDataType(args[0], args[1])

    def tickPrice(self, *args: object, **kwargs: object) -> None:
        """``tickPrice(reqId, tickType, price, attrib)``. The router drops ``attrib``."""
        if not self._refused("tickPrice", args, kwargs, 4):
            self._router.tickPrice(args[0], args[1], args[2], args[3])

    def tickSize(self, *args: object, **kwargs: object) -> None:
        """``tickSize(reqId, tickType, size)``."""
        if not self._refused("tickSize", args, kwargs, 3):
            self._router.tickSize(args[0], args[1], args[2])

    def tickString(self, *args: object, **kwargs: object) -> None:
        """``tickString(reqId, tickType, value)``."""
        if not self._refused("tickString", args, kwargs, 3):
            self._router.tickString(args[0], args[1], args[2])

    def tickGeneric(self, *args: object, **kwargs: object) -> None:
        """``tickGeneric(reqId, tickType, value)``."""
        if not self._refused("tickGeneric", args, kwargs, 3):
            self._router.tickGeneric(args[0], args[1], args[2])

    def tickSnapshotEnd(self, *args: object, **kwargs: object) -> None:
        """``tickSnapshotEnd(reqId)``."""
        if not self._refused("tickSnapshotEnd", args, kwargs, 1):
            self._router.tickSnapshotEnd(args[0])

    # ------------------------------------------------------------------ account summary

    def accountSummary(self, *args: object, **kwargs: object) -> None:
        """``accountSummary(reqId, account, tag, value, currency)``."""
        if not self._refused("accountSummary", args, kwargs, 5):
            self._router.accountSummary(args[0], args[1], args[2], args[3], args[4])

    def accountSummaryEnd(self, *args: object, **kwargs: object) -> None:
        """``accountSummaryEnd(reqId)``."""
        if not self._refused("accountSummaryEnd", args, kwargs, 1):
            self._router.accountSummaryEnd(args[0])

    # ------------------------------------------------------------------ positions snapshot

    def position(self, *args: object, **kwargs: object) -> None:
        """``position(account, contract, pos, avgCost)``. Positions carry no request id."""
        if not self._refused("position", args, kwargs, 4):
            self._router.position(args[0], args[1], args[2], args[3])

    def positionEnd(self, *args: object, **kwargs: object) -> None:
        """``positionEnd()``."""
        if not self._refused("positionEnd", args, kwargs, 0):
            self._router.positionEnd()

    # ------------------------------------------------------------------ direct notifications

    def connectAck(self, *args: object, **kwargs: object) -> None:
        """``connectAck()``: an adapter observation only, never broker acknowledgement."""
        if not self._refused("connectAck", args, kwargs, 0):
            self._router.connectAck()

    def connectionClosed(self, *args: object, **kwargs: object) -> None:
        """``connectionClosed()``: an adapter observation only, never socket closure."""
        if not self._refused("connectionClosed", args, kwargs, 0):
            self._router.connectionClosed()

    # ------------------------------------------------------------------ explicit refusal

    def managedAccounts(self, *args: object, **kwargs: object) -> None:
        """``managedAccounts(accountsList)`` has no accepted sink: always the router's fixed refusal.
        The account list is never read or retained."""
        self._router.unsupported_callback("managedAccounts")

    def __repr__(self) -> str:
        return "ReadCallbackFacade()"

    def __reduce_ex__(self, protocol: object) -> object:
        raise TypeError("a ReadCallbackFacade holds a live router and cannot be serialized")
