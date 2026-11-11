"""Thin read-only adapter over the official Interactive Brokers TWS API (``ibapi``).

Only ``tradingbot.ibkr.session`` imports ``ibapi``. Every other module here (config, models, errors,
identity, redact, check) is SDK-free so it can be imported and tested where the SDK is not installed.

This package is READ-ONLY: it never places, modifies or cancels orders. See docs/IBKR_ADAPTER.md.
"""
