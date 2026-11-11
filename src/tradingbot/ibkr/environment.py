"""Local paper-environment preflight, run BEFORE any socket is opened (SDK-free, macOS).

1. ``confirm_paper_profile``: the configured TWS profile ``jts.ini`` is a regular file owned by
   ``tws_os_user`` and its ``[Logon]`` section says ``tradingMode=p``. No other line of the file is
   ever echoed.
2. ``confirm_listener``: every TCP socket LISTENing on the configured port belongs to a process owned
   by ``tws_os_user``. Uses ``netstat -anv -p tcp`` (its ``process:pid`` column lists sockets of all
   users to an unprivileged caller on macOS) and ``ps -o user= -p PID``.

Both raise a typed error with a ``reason`` and never pass when something cannot be determined.
Residual risk: these are point-in-time checks; the listener could change between the check and the
connect (the identity gate still applies afterwards).
"""
from __future__ import annotations

import configparser
import os
import pwd
import re
import stat
import subprocess
from collections.abc import Callable, Sequence

from tradingbot.ibkr.errors import (
    ListenerNotConfirmed,
    ListenerOwnerUndeterminable,
    PaperModeNotConfirmed,
)

NETSTAT = "/usr/sbin/netstat"
PS = "/bin/ps"
_MAX_INI_BYTES = 1 << 20
# "...  process-name:PID  00100 00000006 ..." (name may contain spaces; state + options follow)
_PID = re.compile(r":(\d+)\s+[0-9a-fA-F]{5}\s+[0-9a-fA-F]{8}\s")

Runner = Callable[[Sequence[str]], str]


def _run(argv: Sequence[str]) -> str:
    return subprocess.run(list(argv), capture_output=True, text=True, timeout=10,
                          check=True).stdout


def confirm_paper_profile(path: str, os_user: str) -> dict:
    def refuse(reason: str, msg: str) -> PaperModeNotConfirmed:
        return PaperModeNotConfirmed(f"paper mode not confirmed: {msg}", reason=reason)

    try:
        st = os.lstat(path)
    except FileNotFoundError:
        raise refuse("MISSING", f"TWS profile file not found: {path}") from None
    except OSError as exc:
        raise refuse("UNREADABLE", f"cannot stat {path}: {exc.strerror}") from None
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        raise refuse("NOT_REGULAR", f"TWS profile must be a regular file, not a symlink: {path}")
    try:
        owner = pwd.getpwuid(st.st_uid).pw_name
    except KeyError:
        owner = f"uid {st.st_uid}"
    if owner != os_user:
        raise refuse("WRONG_OWNER", f"{path} is owned by {owner}, not tws_os_user {os_user}")
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as fh:
            raw = fh.read(_MAX_INI_BYTES + 1)
    except OSError as exc:
        raise refuse("UNREADABLE", f"cannot read {path}: {exc.strerror}") from None
    if len(raw) > _MAX_INI_BYTES:
        raise refuse("UNREADABLE", f"{path} is implausibly large")
    parser = configparser.ConfigParser(strict=True, interpolation=None)
    parser.optionxform = str  # keys are case-sensitive in jts.ini
    try:
        parser.read_string(raw.decode("utf-8", errors="replace"))
    except configparser.Error as exc:  # message could quote file lines: do not include it
        raise refuse("UNPARSEABLE", f"{path} is not parseable ({type(exc).__name__})") from None
    if not parser.has_section("Logon") or not parser.has_option("Logon", "tradingMode"):
        raise refuse("KEY_MISSING", f"{path} has no [Logon] tradingMode")
    value = parser.get("Logon", "tradingMode").strip()
    if value != "p":
        shown = value if re.fullmatch(r"[a-z]", value) else "<other>"
        raise refuse("NOT_PAPER", f"{path} [Logon] tradingMode is {shown!r}, not 'p'")
    return {"profile": path, "owner": owner, "tradingMode": "p"}


def parse_listeners(netstat_text: str, port: int) -> list[tuple[str, int]]:
    """(local address, pid) of every TCP LISTEN socket on ``port`` in ``netstat -anv`` output."""
    lines = netstat_text.splitlines()
    if not any("process:pid" in line for line in lines[:3]):
        raise ListenerOwnerUndeterminable("netstat output has no process:pid column; cannot attribute "
                                   "the listener")
    found = []
    for line in lines:
        parts = line.split()
        if len(parts) < 6 or not parts[0].startswith("tcp") or parts[5] != "LISTEN":
            continue
        local = parts[3]
        if local.rsplit(".", 1)[-1] != str(port):
            continue
        m = _PID.search(line)
        if m is None:
            raise ListenerOwnerUndeterminable(f"listener {local} has no attributable pid")
        found.append((local, int(m.group(1))))
    return found


def confirm_listener(port: int, os_user: str, *, runner: Runner = _run) -> dict:
    try:
        listeners = parse_listeners(runner([NETSTAT, "-anv", "-p", "tcp"]), port)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ListenerOwnerUndeterminable(f"netstat failed: {type(exc).__name__}") from None
    if not listeners:
        raise ListenerNotConfirmed(f"nothing is listening on port {port}", reason="NO_LISTENER")
    owners: dict[int, str] = {}
    for _local, pid in listeners:
        try:
            user = runner([PS, "-o", "user=", "-p", str(pid)]).strip()
        except (OSError, subprocess.SubprocessError):
            user = ""
        if not user:
            raise ListenerOwnerUndeterminable(f"owner of pid {pid} on port {port} cannot be determined")
        owners[pid] = user
    wrong = {pid: u for pid, u in owners.items() if u != os_user}
    if wrong:
        raise ListenerNotConfirmed(
            f"port {port} listener(s) owned by {sorted(set(wrong.values()))}, not {os_user}",
            reason="WRONG_USER")
    return {"port": port, "listeners": [a for a, _ in listeners], "pids": sorted(owners),
            "user": os_user}


def run_preflight(cfg) -> dict:
    """Profile first, then listener. ``cfg`` is a ``PaperConnectionConfig``."""
    return {"paper_profile": confirm_paper_profile(cfg.tws_profile_ini, cfg.tws_os_user),
            "listener": confirm_listener(cfg.port, cfg.tws_os_user)}
