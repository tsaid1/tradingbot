"""A narrow, SDK-free facade for converted contract-details callbacks. NON_AUTHORITATIVE.

``ContractCallbackFacade`` exposes three plain methods named like the converted callbacks a contract
details request produces: ``contractDetails``, ``contractDetailsEnd`` and the explicitly selected SDK
1052-order ``error``. Each call is forwarded once, unchanged, to the same accepted ``CallbackRouter``
entry (``error`` to ``error_1052``, keeping ``errorTime`` before ``errorCode``). The router and its
normalizer do all validation, snapshotting and evidence. Nothing is normalized, retained or decided here.

* **Fixed call forms.** ``contractDetails`` takes exactly two positional arguments and
  ``contractDetailsEnd`` exactly one. ``error`` takes positional arguments only, and the router's 1052
  shape rules judge their count and types. Any keyword argument or other form becomes the router's
  persistent ``unsupported_callback`` refusal, by fixed name, with no request id taken from the
  malformed call. No argument is read, logged or retained by the facade.
* **No version inference.** ``error`` is always the 1052 order. A legacy-order call cannot be detected
  and is not guessed at.
* **Outside the ledger.** Contract details have no lifecycle slot. The facade never touches the
  router's queue, consumer, registration or shutdown.

It imports no SDK, subclasses nothing, opens no socket and starts no thread. It is not a complete
callback interface or a connected adapter, and it establishes no SDK compatibility.
See docs/IBKR_CONTRACT_FACADE.md.
"""
from __future__ import annotations

from tradingbot.paper_readiness.callback_router import CallbackRouter

__all__ = ["ContractCallbackFacade"]


class ContractCallbackFacade:
    """Forwards converted contract-details callbacks to one accepted ``CallbackRouter``."""

    __slots__ = ("_router",)

    def __init__(self, router: CallbackRouter) -> None:
        if type(router) is not CallbackRouter:
            raise TypeError("router must be a CallbackRouter")
        self._router = router

    def contractDetails(self, *args: object, **kwargs: object) -> None:
        """``contractDetails(reqId, contractDetails)``, forwarded unchanged."""
        if kwargs or len(args) != 2:
            self._router.unsupported_callback("contractDetails")
            return
        self._router.contractDetails(args[0], args[1])

    def contractDetailsEnd(self, *args: object, **kwargs: object) -> None:
        """``contractDetailsEnd(reqId)``, forwarded unchanged."""
        if kwargs or len(args) != 1:
            self._router.unsupported_callback("contractDetailsEnd")
            return
        self._router.contractDetailsEnd(args[0])

    def error(self, *args: object, **kwargs: object) -> None:
        """The SDK 1052 order ``reqId, errorTime, errorCode, errorString[, advancedOrderRejectJson]``,
        forwarded unchanged to ``error_1052``. The text arguments are never read."""
        if kwargs:
            self._router.unsupported_callback("error")
            return
        self._router.error_1052(*args)

    def __repr__(self) -> str:
        return "ContractCallbackFacade()"

    def __reduce_ex__(self, protocol: object) -> object:
        raise TypeError("a ContractCallbackFacade holds a live router and cannot be serialized")
