"""Order-mode config (SDK-free): the read-only paper config plus a hand-written order entry.

File (default ``~/.config/tradingbot/ibkr/paper_orders.toml``; same file-safety rules as the
read-only config: owner-only, mode 0600, regular file, no symlink)::

    # every key of the read-only paper.toml, unchanged, plus:
    orders = "paper-test-1"                          # names a code-defined experiment (caps.py)
    state_dir = "/Users/<user>/.local/state/tradingbot"  # must already exist; holds the counter

There is no default for ``orders`` or ``state_dir``, no environment-variable override and no flag:
order mode exists only when this file names a known experiment. The read-only ``load_config``
refuses this file (unknown keys), so the read-only diagnostic can never run on an order config.
"""
from __future__ import annotations

import os
import stat
from dataclasses import dataclass, field
from pathlib import Path

from tradingbot.ibkr.config import _KEYS, PaperConnectionConfig, read_private_toml, validate_config
from tradingbot.ibkr.errors import ConfigError, ConfigInvalid
from tradingbot.ibkr_orders.caps import EXPERIMENTS, ExperimentCaps

DEFAULT_ORDER_CONFIG_PATH = Path("~/.config/tradingbot/ibkr/paper_orders.toml")
ORDER_KEYS = frozenset({"orders", "state_dir"})


class OrdersNotEnabled(ConfigError):
    """The config does not contain a valid, hand-written ``orders`` entry."""


class UnknownExperiment(OrdersNotEnabled):
    """``orders`` names no experiment defined in code."""


class StateDirInvalid(ConfigError):
    pass


_ISSUED: list = []  # every config built by validate_order_config (accident guard, not a security
# boundary: in-process code can do anything)


@dataclass(frozen=True)
class PaperOrderConfig:
    connection: PaperConnectionConfig
    experiment: ExperimentCaps
    state_dir: Path = field(repr=False)

    def __repr__(self) -> str:
        return (f"PaperOrderConfig(connection={self.connection!r}, "
                f"experiment={self.experiment.name!r})")

    @property
    def issued(self) -> bool:
        return any(c is self for c in _ISSUED)


def validate_order_config(data: dict) -> PaperOrderConfig:
    if not isinstance(data, dict):
        raise ConfigInvalid("config must be a TOML table")
    if "orders" not in data:
        raise OrdersNotEnabled("no 'orders' entry: order mode is not enabled")
    missing = sorted((_KEYS | ORDER_KEYS) - data.keys())
    unknown = sorted(data.keys() - (_KEYS | ORDER_KEYS))
    if missing or unknown:
        raise ConfigInvalid(f"order config keys: missing {missing}, unknown {unknown}")
    name = data["orders"]
    if not isinstance(name, str) or name not in EXPERIMENTS:
        raise UnknownExperiment(f"'orders' must name an experiment defined in code "
                                f"({sorted(EXPERIMENTS)}); got {name!r}")
    caps = EXPERIMENTS[name]
    connection = validate_config({k: data[k] for k in _KEYS})  # every read-only rule still applies
    raw_dir = data["state_dir"]
    if not isinstance(raw_dir, str) or not os.path.isabs(raw_dir):
        raise StateDirInvalid("state_dir must be an absolute path")
    state_dir = Path(raw_dir)
    try:
        st = os.lstat(state_dir)
    except FileNotFoundError:
        raise StateDirInvalid("state_dir must already exist (create it by hand, mode 0700)") from None
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise StateDirInvalid("state_dir must be a real directory, not a symlink")
    if st.st_uid != os.getuid():
        raise StateDirInvalid("state_dir must be owned by the current user")
    if st.st_mode & 0o022:
        raise StateDirInvalid("state_dir must not be group/world writable")
    cfg = PaperOrderConfig(connection=connection, experiment=caps, state_dir=state_dir)
    _ISSUED.append(cfg)
    return cfg


def load_order_config(path: str | os.PathLike | None = None) -> PaperOrderConfig:
    return validate_order_config(read_private_toml(
        path if path is not None else DEFAULT_ORDER_CONFIG_PATH))
