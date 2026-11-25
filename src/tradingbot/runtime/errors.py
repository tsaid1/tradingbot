"""Error types shared with modules that must import without the runtime configuration.

``ConfigError`` is defined here so that the adjudication core and the private-path guards can be
imported (the hosted reviewer site does) without loading the configuration models, the risk policy
or anything else of the trading system. ``tradingbot.runtime.config`` re-exports it: one class.
"""
from __future__ import annotations


class ConfigError(ValueError):
    """The configuration is missing, malformed, or asks for something this build refuses."""
