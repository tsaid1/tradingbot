"""Wiring: build the source, the simulated broker, the notifier and the service from a config.

``mode = "mock"``: the ``SimBroker`` on a static invented market. ``mode = "shadow"``: the
``ShadowBroker`` (the same strict ``SimBroker`` for every order, fill, position and the account,
with real quotes from ONE read-only IBKR session). No mode builds an order-capable broker."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from tradingbot.execution.shadow_broker import ShadowStartRefused, build_shadow_broker
from tradingbot.execution.sim_broker import SimBroker
from tradingbot.models import AssetClass, Instrument
from tradingbot.runtime.config import ConfigError, ReplaySourceConfig, RuntimeConfig
from tradingbot.runtime.mapping import FamilyMapping, MappingPolicy
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


def shadow_watch_list(config: RuntimeConfig) -> list[Instrument]:
    """The execution instruments of the approved, executable stock and ETF families: their
    streams are opened at start so a post does not meet a cold stream."""
    policy = MappingPolicy.from_toml(config.paths.mapping_file)
    out = []
    for entry in policy.entries.values():
        x = getattr(entry, "execution", None)
        if isinstance(entry, FamilyMapping) and entry.executable and x is not None \
                and x.sec_type == "STK":
            out.append(Instrument(asset_class=AssetClass(x.asset_class), symbol=x.symbol,
                                  exchange=x.exchange))
    return out


SHADOW_MARKER = "shadow_state_dir"


def _check_state_dir_mode(config: RuntimeConfig, store: RuntimeStore) -> None:
    """A state directory holds one mode's simulated history: shadow positions are keyed by real
    contract ids, mock positions by invented ones. Never mix them."""
    marked = store.get(SHADOW_MARKER) is not None
    used = int(store.get("cycles", 0) or 0) > 0
    if config.mode == "shadow" and used and not marked:
        raise ConfigError("this state_dir was used in mock mode; give shadow mode its own")
    if config.mode == "mock" and marked:
        raise ConfigError("this state_dir was used in shadow mode; give mock mode its own")


def build_service(
    config: RuntimeConfig,
    *,
    clock: Callable[[], datetime] | None = None,
    transport: HttpTransport | None = None,
    notifier: Notifier | None = None,
    session_factory=None,
    allow_candidate_lexicon: bool = False,
) -> RuntimeService:
    """``session_factory`` (shadow mode, tests only): builds the read-only ``IbkrSession`` from the
    paper config; whatever it returns must be exactly an ``IbkrSession`` or the start is refused."""
    if config.mode not in ("mock", "shadow"):
        raise ConfigError("only mock mode or shadow mode may run")
    config.paths.state_dir.mkdir(parents=True, exist_ok=True)
    if config.interpreter.lexicon_path is not None and not config.interpreter.lexicon_path.exists():
        raise ConfigError("interpreter.lexicon_path is set but the file does not exist")
    store = RuntimeStore(config.runtime_db)
    _check_state_dir_mode(config, store)
    if config.mode == "mock":
        broker = SimBroker(config.broker_db, account=config.mock.account, clock=clock)
    else:
        if config.shadow is None:
            raise ConfigError("shadow mode needs its [shadow] table")
        try:
            broker = build_shadow_broker(
                sim_db=config.broker_db, account=config.shadow.account,
                paper_config=config.shadow.paper_config, clock=clock,
                watch=shadow_watch_list(config), session_factory=session_factory)
        except ShadowStartRefused as exc:
            raise ConfigError(f"shadow mode: {exc}") from None
    try:
        source = build_source(config, store, clock=clock, transport=transport)
        service = RuntimeService(
            config,
            source=source,
            broker=broker,
            notifier=notifier or build_notifier(config, clock),
            clock=clock,
            store=store,
            allow_candidate_lexicon=allow_candidate_lexicon,
        )
    except BaseException:
        if config.mode == "shadow":
            broker.close()
        raise
    if config.mode == "shadow":
        store.put(SHADOW_MARKER, True, service.clock())
    return service
