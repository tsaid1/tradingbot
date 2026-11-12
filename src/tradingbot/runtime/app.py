"""Wiring: build the source, the simulated broker, the notifier and the service from a config."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from tradingbot.execution.sim_broker import SimBroker
from tradingbot.runtime.config import ConfigError, ReplaySourceConfig, RuntimeConfig
from tradingbot.runtime.notify import ChannelContext, Notifier, build_channels
from tradingbot.runtime.service import RuntimeService
from tradingbot.runtime.sources import (
    FileTokenStore,
    HttpTransport,
    ReplayFileSource,
    SourceAdapter,
    UrllibTransport,
    XTimelineSource,
    load_client_credentials,
)
from tradingbot.runtime.store import RuntimeStore


def with_replay_source(config: RuntimeConfig, file: str | Path) -> RuntimeConfig:
    """``--source replay:<file>``: the same config with a replay source (compression and lag kept
    when the configured source is itself a replay)."""
    current = config.source
    keep = (
        {
            "compression": current.compression,
            "lag_seconds": current.lag_seconds,
            "expected_author_id": current.expected_author_id,
        }
        if isinstance(current, ReplaySourceConfig)
        else {}
    )
    source = ReplaySourceConfig(kind="replay", file=Path(file).expanduser().resolve(), **keep)
    return config.model_copy(update={"source": source})


def build_source(
    config: RuntimeConfig,
    store: RuntimeStore,
    *,
    clock: Callable[[], datetime] | None = None,
    transport: HttpTransport | None = None,
) -> SourceAdapter:
    src = config.source
    if isinstance(src, ReplaySourceConfig):
        if not src.file.exists():
            raise ConfigError(f"replay file not found: {src.file}")
        return ReplayFileSource(
            src.file,
            store,
            clock=clock,
            compression=src.compression,
            expected_author_id=src.expected_author_id,
        )
    return XTimelineSource(
        author_id=src.author_id,
        token_store=FileTokenStore(src.token_file),
        client=load_client_credentials(src.client_file),
        transport=transport or UrllibTransport(),
        store=store,
        clock=clock,
        max_results=src.max_results,
        max_pages=src.max_pages,
    )


def build_notifier(config: RuntimeConfig, clock=None) -> Notifier:
    """The configured channels (``[notify] channels``; ``macos = true`` adds ``macos``)."""
    names = list(config.notify.channels)
    if config.notify.macos and "macos" not in names:
        names.append("macos")
    ctx = ChannelContext(
        log_file=config.notification_log, alerts_file=config.alerts_file, clock=clock
    )
    try:
        return build_channels(names, ctx)
    except ValueError as exc:
        raise ConfigError(str(exc)) from None


def build_service(
    config: RuntimeConfig,
    *,
    clock: Callable[[], datetime] | None = None,
    transport: HttpTransport | None = None,
    notifier: Notifier | None = None,
    allow_candidate_lexicon: bool = False,
) -> RuntimeService:
    if config.mode != "mock":
        raise ConfigError("only mock mode may run")
    config.paths.state_dir.mkdir(parents=True, exist_ok=True)
    if config.interpreter.lexicon_path is not None and not config.interpreter.lexicon_path.exists():
        raise ConfigError("interpreter.lexicon_path is set but the file does not exist")
    store = RuntimeStore(config.runtime_db)
    broker = SimBroker(config.broker_db, account=config.mock.account, clock=clock)
    source = build_source(config, store, clock=clock, transport=transport)
    return RuntimeService(
        config,
        source=source,
        broker=broker,
        notifier=notifier or build_notifier(config, clock),
        clock=clock,
        store=store,
        allow_candidate_lexicon=allow_candidate_lexicon,
    )
