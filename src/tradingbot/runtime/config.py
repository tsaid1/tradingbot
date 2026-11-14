"""Runtime configuration: one TOML file, validated strictly, failing closed.

Two modes are accepted, both with the simulated broker for every order, fill, position and the
account: ``mode = "mock"`` (a static invented market) and ``mode = "shadow"`` (real quotes from the
read-only IBKR adapter, simulated fills; ``[shadow]`` names the private paper config). Every table and key is checked: an
unknown key, a missing required value or a wrong type refuses to start. Every size, cap, collar,
freshness and timing value the service uses, and the expected broker account, must be written out
explicitly (``REQUIRED_KEYS``): nothing that sizes or bounds an order runs on a default (review F4). Relative paths resolve
against the directory of the config file. Credentials are never stored here, only the *paths* of
the private files that hold them.
"""

from __future__ import annotations

import tomllib
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, ValidationError

from tradingbot.models import MockPolicy, StrictModel

DEFAULT_CONFIG_PATH = Path("~/.config/tradingbot/runtime.toml")
ACCEPTED_MODES = ("mock", "shadow")


class ConfigError(ValueError):
    """The configuration is missing, malformed, or asks for something this build refuses."""


class PathsConfig(StrictModel):
    # Holds runtime.sqlite3, execution.sqlite3, replay.sqlite3, sim_broker.sqlite3, heartbeat.json.
    state_dir: Path
    # Emergency halt: no new order or amendment; working entries are cancelled, protection sized.
    halt_file: Path
    # The source-series-to-broker-contract mapping policy (config/instrument_mapping.toml).
    mapping_file: Path


class ReplaySourceConfig(StrictModel):
    kind: Literal["replay"]
    file: Path
    # None: deliver every post with its recorded timestamps (historical replay; decisions are taken
    # at available_at + lag_seconds). A factor k: release posts in compressed real time (k times
    # faster) with timestamps rebased onto the service clock, so they behave like live posts.
    compression: float | None = Field(default=None, gt=0)
    lag_seconds: float = Field(default=5.0, ge=0)
    # Needed only when the file holds G0 archive records rather than SourcePost objects.
    expected_author_id: str | None = None


class XTimelineSourceConfig(StrictModel):
    kind: Literal["x_timeline"]
    # The publisher's numeric X user id. Posts by anyone else are marked unverified (gate: BLOCK).
    author_id: str = Field(pattern=r"^\d{1,20}$")
    # Private files outside the repository and cloud storage (mode 0600, directory 0700).
    token_file: Path
    client_file: Path
    max_results: int = Field(default=100, ge=5, le=100)
    max_pages: int = Field(default=10, ge=1, le=32)


SourceConfig = Annotated[ReplaySourceConfig | XTimelineSourceConfig, Field(discriminator="kind")]


class ServiceConfig(StrictModel):
    cadence_seconds: float = Field(default=5.0, gt=0, le=3600)
    max_backoff_seconds: float = Field(default=300.0, gt=0, le=86400)
    # Consecutive failures of the source or the broker before a notification is sent.
    failure_alert_after: int = Field(default=3, ge=1, le=100)
    # Reconciliation reports an intent of unknown outcome older than this as stuck.
    stuck_after_seconds: int = Field(default=60, gt=0)
    # Alerting only (never sizes or bounds an order), so optional. SOURCE_STALLED is raised when
    # the source cursor has not advanced for this long inside the activity window (unset: off).
    source_stall_after_seconds: float | None = Field(default=None, gt=0)
    # "HH:MM-HH:MM" in UTC, Monday to Friday, when the publisher is expected to post; unset:
    # always expected. Only used with source_stall_after_seconds.
    activity_window_utc: str | None = Field(default=None, pattern=r"^\d{2}:\d{2}-\d{2}:\d{2}$")
    # The watchdog reports the heartbeat stale after this long (unset: max(120, 10 x cadence)).
    heartbeat_stale_after_seconds: float | None = Field(default=None, gt=0)
    # Review N2. True (strict): an identity lockout is cleared only through a full reconnect of
    # the session (the adapter's reconnect, or a session this process opened after the lockout
    # began); an adapter that cannot reconnect stays locked out until the service is restarted.
    # False also accepts an identity read alone, journaled IDENTITY_REREAD_ONLY.
    lockout_clear_requires_full_reconnect: bool = True


class InterpreterConfig(StrictModel):
    # Optional private lexicon extension (tkl_interpreter.LexiconExtension JSON). Never committed.
    lexicon_path: Path | None = None
    # Optional PRIVATE review log for the owner's vocabulary review page (default off): one JSON
    # line per entry-rule hold caused by vocabulary, with the words and sentences involved. Must
    # lie outside the repository and every git work tree; created with mode 0600.
    review_log_path: Path | None = None


class NotifyConfig(StrictModel):
    log_file: Path | None = None  # default: <state_dir>/notifications.log
    alerts_file: Path | None = None  # default: <state_dir>/alerts.jsonl (the dashboard reads it)
    # Registered channel names (runtime.notify); "macos = true" adds "macos" for compatibility.
    channels: list[str] = Field(default_factory=lambda: ["log", "alerts_file"])
    macos: bool = False
    # An alert still open is notified again this often (one condition never alerts every cycle).
    realert_seconds: float = Field(default=3600.0, ge=60)


class ExecutionSettings(StrictModel):
    """All required (see ``REQUIRED_KEYS``); the ranges are those of ``ExecutionConfig``."""

    # The coordinator refuses to trade against any other broker account.
    expected_account: str = Field(min_length=1)
    max_submit_attempts: int = Field(ge=1, le=5)
    # After an unknown submission outcome, "no trace at the broker" is never concluded sooner.
    settle_seconds: float = Field(ge=0)
    # An exit of unknown outcome is resent only after this many consecutive no-trace lookups
    # spread over at least settle_seconds (an entry or add is never resent).
    exit_no_trace_lookups: int = Field(ge=3, le=20)
    max_intent_age_seconds: int = Field(gt=0)
    max_exit_intent_age_seconds: int = Field(gt=0)
    lookup_horizon_seconds: int = Field(gt=0)
    confirm_attempts: int = Field(ge=1, le=20)
    confirm_wait_seconds: float = Field(ge=0, le=5)


class MockQuote(StrictModel):
    bid: Decimal = Field(gt=0)
    ask: Decimal = Field(gt=0)


class MockConfig(StrictModel):
    account: str = "SIM-PAPER-0001"
    # A static simulated market, republished to the SimBroker every cycle (fresh receipt time).
    quotes: dict[str, MockQuote] = Field(default_factory=dict)


class ShadowConfig(StrictModel):
    """``mode = "shadow"``: real read-only quotes, simulated fills (docs/RUNTIME.md)."""

    # The private IBKR paper connection config, loaded exactly as the paper check loads it
    # (tradingbot.ibkr.config.load_config: 0600 file, owner, no symlink, paper only). Required.
    paper_config: Path
    # The SIMULATED account the shadow broker reports; execution.expected_account must equal it.
    account: str = Field(default="SIM-SHADOW-0001", pattern=r"^SIM-[A-Z0-9-]{1,32}$")


class OperatorConfirmationConfig(StrictModel):
    """Optional operator confirmation of held signals (docs/OPERATOR_CONFIRMATION.md). OFF by
    default; when on, refused unless the broker declares SIM or PAPER (mock and shadow only).
    It never authorizes order submission to a real broker."""

    enabled: bool = False


class RuntimeConfig(StrictModel):
    mode: Literal["mock", "shadow"]
    paths: PathsConfig
    source: SourceConfig
    service: ServiceConfig
    policy: MockPolicy
    interpreter: InterpreterConfig = Field(default_factory=InterpreterConfig)
    notify: NotifyConfig = Field(default_factory=NotifyConfig)
    execution: ExecutionSettings
    mock: MockConfig = Field(default_factory=MockConfig)
    shadow: ShadowConfig | None = None
    operator_confirmation: OperatorConfirmationConfig = Field(
        default_factory=OperatorConfirmationConfig)

    @property
    def simulated_account(self) -> str:
        """The simulated broker's account in either mode."""
        return self.shadow.account if self.shadow is not None else self.mock.account

    @property
    def runtime_db(self) -> Path:
        return self.paths.state_dir / "runtime.sqlite3"

    @property
    def execution_db(self) -> Path:
        return self.paths.state_dir / "execution.sqlite3"

    @property
    def replay_db(self) -> Path:
        return self.paths.state_dir / "replay.sqlite3"

    @property
    def broker_db(self) -> Path:
        return self.paths.state_dir / "sim_broker.sqlite3"

    @property
    def heartbeat_file(self) -> Path:
        return self.paths.state_dir / "heartbeat.json"

    @property
    def notification_log(self) -> Path:
        return self.notify.log_file or self.paths.state_dir / "notifications.log"

    @property
    def alerts_file(self) -> Path:
        return self.notify.alerts_file or self.paths.state_dir / "alerts.jsonl"

    @property
    def operator_db(self) -> Path:
        return self.paths.state_dir / "operator.sqlite3"

    @property
    def heartbeat_stale_after(self) -> float:
        explicit = self.service.heartbeat_stale_after_seconds
        return explicit or max(120.0, 10 * self.service.cadence_seconds)


REQUIRED_TABLES = ("paths", "source", "service", "policy", "execution")
# Keys that must be present even where the model has a default; alternatives are accepted
# spellings (MockPolicy's validation aliases).
REQUIRED_KEYS: dict[str, tuple[tuple[str, ...], ...]] = {
    "service": (
        ("cadence_seconds",),
        ("max_backoff_seconds",),
        ("failure_alert_after",),
        ("stuck_after_seconds",),
    ),
    "policy": (
        ("quantity", "quantity_per_open"),
        ("quantity_per_add",),
        ("max_price_deviation_pct", "max_deviation_pct"),
        ("max_spread_pct",),
        ("max_notional_usd",),
        ("max_signal_age_seconds", "max_source_age_seconds"),
        ("max_quote_age_seconds",),
        ("max_instrument_exposure_usd",),
        ("max_aggregate_exposure_usd",),
    ),
    "execution": (
        ("expected_account",),
        ("max_submit_attempts",),
        ("settle_seconds",),
        ("exit_no_trace_lookups",),
        ("max_intent_age_seconds",),
        ("max_exit_intent_age_seconds",),
        ("lookup_horizon_seconds",),
        ("confirm_attempts",),
        ("confirm_wait_seconds",),
    ),
}


def _missing_keys(raw: dict) -> list[str]:
    missing = []
    for table, keys in REQUIRED_KEYS.items():
        values = raw.get(table)
        values = values if isinstance(values, dict) else {}
        missing += [f"{table}.{names[0]}" for names in keys if not any(n in values for n in names)]
    return missing


def _resolve_paths(raw: dict, base: Path) -> None:
    def fix(table: dict | None, *keys: str) -> None:
        if not isinstance(table, dict):
            return
        for key in keys:
            value = table.get(key)
            if isinstance(value, str):
                p = Path(value).expanduser()
                table[key] = str(p if p.is_absolute() else (base / p))

    fix(raw.get("paths"), "state_dir", "halt_file", "mapping_file")
    fix(raw.get("source"), "file", "token_file", "client_file")
    fix(raw.get("interpreter"), "lexicon_path", "review_log_path")
    fix(raw.get("notify"), "log_file", "alerts_file")
    fix(raw.get("shadow"), "paper_config")


def parse_config(raw: dict, *, base: Path) -> RuntimeConfig:
    if "mode" not in raw:
        raise ConfigError("config has no 'mode'; refusing to start")
    if raw["mode"] not in ACCEPTED_MODES:
        raise ConfigError(
            f"mode {raw['mode']!r} is not accepted by this build; only 'mock' (the simulated "
            "broker) or 'shadow' (the simulated broker at read-only real quotes) may run"
        )
    _check_mode_tables(raw)
    missing = [t for t in REQUIRED_TABLES if t not in raw]
    if missing:
        raise ConfigError(f"config is missing required table(s): {', '.join(missing)}")
    missing = _missing_keys(raw)
    if missing:
        raise ConfigError(
            "config must set every size, cap, collar, freshness and timing value explicitly; "
            f"missing: {', '.join(missing)}"
        )
    raw = {k: (dict(v) if isinstance(v, dict) else v) for k, v in raw.items()}
    _resolve_paths(raw, base)
    try:
        return RuntimeConfig.model_validate(raw)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()
        )
        raise ConfigError(f"invalid runtime config: {problems}") from None


def _check_mode_tables(raw: dict) -> None:
    """Shadow mode has its own table and never an invented market; mock mode has no [shadow]."""
    if raw["mode"] == "mock":
        if "shadow" in raw:
            raise ConfigError("[shadow] is only accepted with mode = \"shadow\"")
        return
    if not isinstance(raw.get("shadow"), dict) or "paper_config" not in raw["shadow"]:
        raise ConfigError("mode = \"shadow\" needs [shadow] paper_config (the private IBKR "
                          "paper connection config); refusing to start")
    if "mock" in raw:
        raise ConfigError("mode = \"shadow\" takes its quotes from the read-only adapter only: "
                          "remove the [mock] table (no invented market, no second account)")
    source = raw.get("source")
    if isinstance(source, dict) and source.get("kind") == "replay" \
            and source.get("compression") is None:
        raise ConfigError("mode = \"shadow\" decides on today's real quotes: a replay source "
                          "needs compression (posts rebased onto the service clock); a historical "
                          "replay would compare old posts with today's market")
    account = raw["shadow"].get("account", ShadowConfig.model_fields["account"].default)
    execution = raw.get("execution")
    if isinstance(execution, dict) and execution.get("expected_account") != account:
        raise ConfigError(f"mode = \"shadow\": execution.expected_account must be the simulated "
                          f"shadow account {account!r}")


def load_config(path: str | Path | None = None) -> RuntimeConfig:
    """Load and validate the runtime config. Raises ConfigError on anything doubtful."""
    p = Path(path or DEFAULT_CONFIG_PATH).expanduser()
    try:
        raw = tomllib.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(f"runtime config not found: {p}") from None
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"runtime config is not valid TOML: {exc}") from None
    return parse_config(raw, base=p.parent.resolve())
