"""Private paper-connection config, loaded fail-closed from an owner-only TOML file (SDK-free).

File format (``~/.config/tradingbot/ibkr/paper.toml``, mode 0600)::

    mode = "paper"
    host = "127.0.0.1"
    port = 7497
    client_id = 17
    account = "DU0000000"
    tws_profile_ini = "/Users/<user>/Jts/jts.ini"   # no default: set per run
    tws_os_user = "<user>"                          # no default: set per run
    read_only_check = "config-read"   # the only legal value

The ``D`` prefix check on ``account`` is defence in depth only. Paper identity is established by
agreement of independent checks: the TWS profile (owned by ``tws_os_user``) says paper mode, the
process listening on ``port`` belongs to ``tws_os_user`` (both before any socket is opened), and the
broker's own ``managedAccounts`` is exactly this one account (the primary control).
"""
from __future__ import annotations

import ipaddress
import os
import re
import stat
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from tradingbot.ibkr.errors import (
    ConfigInvalid,
    ConfigIsSymlink,
    ConfigNotFound,
    ConfigNotRegularFile,
    ConfigPermissionsTooOpen,
    ConfigWrongOwner,
    ModeNotPaper,
    NonLoopbackHost,
    NonPaperAccount,
    PortNotAllowed,
)
from tradingbot.ibkr.redact import mask_account

DEFAULT_CONFIG_PATH = Path("~/.config/tradingbot/ibkr/paper.toml")
PAPER_PORTS = frozenset({7497, 4002})  # paper TWS, paper IB Gateway
READ_ONLY_CHECKS = frozenset({"config-read"})  # fail closed: read-only must be verified
_KEYS = frozenset({"mode", "host", "port", "client_id", "account", "tws_profile_ini", "tws_os_user",
                   "read_only_check"})
_OS_USER = re.compile(r"^[a-z_][a-z0-9_.-]{0,31}$")
_ACCOUNT_FORMAT = re.compile(r"^[A-Z]{1,3}\d{4,12}$")


@dataclass(frozen=True)
class PaperConnectionConfig:
    host: str
    port: int
    client_id: int
    account: str = field(repr=False)
    tws_profile_ini: str
    tws_os_user: str
    read_only_check: str
    mode: str = "paper"

    def masked_account(self) -> str:
        return mask_account(self.account)

    def __repr__(self) -> str:
        return (f"PaperConnectionConfig(host={self.host!r}, port={self.port}, "
                f"client_id={self.client_id}, account={self.masked_account()!r}, mode={self.mode!r}, "
                f"tws_os_user={self.tws_os_user!r}, read_only_check={self.read_only_check!r})")

    __str__ = __repr__


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def validate_config(data: dict) -> PaperConnectionConfig:
    """Validate a parsed mapping. Each refusal has its own exception type."""
    if not isinstance(data, dict):
        raise ConfigInvalid("config must be a TOML table")
    missing = sorted(_KEYS - data.keys())
    unknown = sorted(data.keys() - _KEYS)
    if missing or unknown:
        raise ConfigInvalid(f"config keys: missing {missing}, unknown {unknown}")

    mode = data["mode"]
    if mode != "paper":
        raise ModeNotPaper(f"mode must be the literal 'paper', got {mode!r}")

    host = data["host"]
    if not isinstance(host, str):
        raise ConfigInvalid("host must be a string")
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = False
    if not loopback:
        raise NonLoopbackHost(f"host must be a loopback IP literal (127.0.0.1 or ::1), got {host!r}")

    port = data["port"]
    if not _is_int(port) or not 1 <= port <= 65535:
        raise ConfigInvalid("port must be an integer in 1..65535")
    if port not in PAPER_PORTS:
        raise PortNotAllowed(f"port {port} is not a paper default; allowed: {sorted(PAPER_PORTS)}")

    client_id = data["client_id"]
    if not _is_int(client_id) or not 1 <= client_id <= 2**31 - 1:
        # client id 0 is TWS's special "manual orders" client; refuse it for this adapter.
        raise ConfigInvalid("client_id must be an integer in 1..2147483647")

    account = data["account"]
    if not isinstance(account, str) or not _ACCOUNT_FORMAT.match(account):
        raise ConfigInvalid("account must look like an IBKR account ID (letters then digits)")
    if not account.startswith("D"):
        raise NonPaperAccount(f"account {mask_account(account)} is not a paper-style ID (must start "
                              "with 'D'); live IDs start with U/F/I")
    profile = data["tws_profile_ini"]
    if not isinstance(profile, str) or not os.path.isabs(profile):
        raise ConfigInvalid("tws_profile_ini must be an absolute path to the TWS profile's jts.ini")
    os_user = data["tws_os_user"]
    if not isinstance(os_user, str) or not _OS_USER.match(os_user):
        raise ConfigInvalid("tws_os_user must be a macOS short user name")
    check = data["read_only_check"]
    if check not in READ_ONLY_CHECKS:
        raise ConfigInvalid(f"read_only_check must be one of {sorted(READ_ONLY_CHECKS)}")
    return PaperConnectionConfig(host=host, port=port, client_id=client_id, account=account,
                                 tws_profile_ini=profile, tws_os_user=os_user,
                                 read_only_check=check, mode=mode)


def load_config(path: str | os.PathLike | None = None) -> PaperConnectionConfig:
    """Read and validate the private TOML file. Refuses symlinks, non-regular files, files not owned
    by the current user and any group/other permission bit."""
    return validate_config(read_private_toml(
        path if path is not None else DEFAULT_CONFIG_PATH))


def read_private_toml(path: str | os.PathLike) -> dict:
    """The file-safety checks of ``load_config`` (no symlink, regular file, owned by the current
    user, no group/other bit), then the parsed TOML table. Never echoes file content."""
    p = Path(path).expanduser()
    try:
        st = os.lstat(p)
    except FileNotFoundError:
        raise ConfigNotFound(f"config file not found: {p}") from None
    if stat.S_ISLNK(st.st_mode):
        raise ConfigIsSymlink(f"config file must not be a symlink: {p}")
    if not stat.S_ISREG(st.st_mode):
        raise ConfigNotRegularFile(f"config path is not a regular file: {p}")
    try:
        fd = os.open(p, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:  # raced into a symlink, or unreadable
        raise ConfigNotRegularFile(f"cannot open config file safely: {p}: {exc.strerror}") from None
    with os.fdopen(fd, "rb") as fh:
        st = os.fstat(fh.fileno())  # re-check on the opened descriptor (no TOCTOU)
        if not stat.S_ISREG(st.st_mode):
            raise ConfigNotRegularFile(f"config path is not a regular file: {p}")
        if st.st_uid != os.getuid():
            raise ConfigWrongOwner(f"config file must be owned by the current user: {p}")
        if st.st_mode & 0o077:
            raise ConfigPermissionsTooOpen(
                f"config file must not be group/world accessible (chmod 600): {p} "
                f"has mode {stat.S_IMODE(st.st_mode):o}")
        raw = fh.read()
    try:
        return tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        # Do not echo file content: it contains the account ID.
        raise ConfigInvalid(f"config file is not valid UTF-8 TOML: {type(exc).__name__}") from None
