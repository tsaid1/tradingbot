"""Local paper-environment preflight (SDK-free). Synthetic files and netstat text; no broker port."""
from __future__ import annotations

import getpass
import os
import shutil
import socket
import sys

import pytest

from tradingbot.ibkr.environment import (
    confirm_listener,
    confirm_paper_profile,
    parse_listeners,
)
from tradingbot.ibkr.errors import (
    ListenerNotConfirmed,
    ListenerOwnerUndeterminable,
    PaperModeNotConfirmed,
)

ME = getpass.getuser()
SECRET = "SomeOtherSetting=do-not-echo-this"


def _ini(tmp_path, body: str):
    p = tmp_path / "jts.ini"
    p.write_text(body)
    return str(p)


def test_paper_profile_confirmed(tmp_path):
    path = _ini(tmp_path, f"[Logon]\n{SECRET}\ntradingMode=p\n[IBGateway]\nApiOnly=true\n")
    assert confirm_paper_profile(path, ME) == {"profile": path, "owner": ME, "tradingMode": "p"}


@pytest.mark.parametrize("body,reason", [
    ("[Logon]\ntradingMode=l\n", "NOT_PAPER"),
    ("[Logon]\ntradingMode=\n", "NOT_PAPER"),
    ("[Logon]\ntradingMode=paper\n", "NOT_PAPER"),
    ("[Logon]\ntradingmode=p\n", "KEY_MISSING"),
    ("[Other]\ntradingMode=p\n", "KEY_MISSING"),
    ("tradingMode=p\n", "KEY_MISSING"),  # lands in [Pre], not [Logon]
    ("[Logon]\ntradingMode=p\ntradingMode=l\n", "UNPARSEABLE"),
])
def test_paper_profile_refusals_do_not_echo_file(tmp_path, body, reason):
    with pytest.raises(PaperModeNotConfirmed) as ei:
        confirm_paper_profile(_ini(tmp_path, f"[Pre]\n{SECRET}\n" + body), ME)
    assert ei.value.reason == reason
    assert "do-not-echo" not in str(ei.value) and "SomeOtherSetting" not in str(ei.value)


def test_file_without_section_header_is_unparseable(tmp_path):
    with pytest.raises(PaperModeNotConfirmed) as ei:
        confirm_paper_profile(_ini(tmp_path, f"{SECRET}\n[Logon]\ntradingMode=p\n"), ME)
    assert ei.value.reason == "UNPARSEABLE" and "do-not-echo" not in str(ei.value)


def test_live_mode_is_named(tmp_path):
    with pytest.raises(PaperModeNotConfirmed, match="'l'"):
        confirm_paper_profile(_ini(tmp_path, "[Logon]\ntradingMode=l\n"), ME)


def test_paper_profile_missing_symlink_dir_wrong_owner(tmp_path):
    with pytest.raises(PaperModeNotConfirmed) as ei:
        confirm_paper_profile(str(tmp_path / "nope.ini"), ME)
    assert ei.value.reason == "MISSING"
    real = _ini(tmp_path, "[Logon]\ntradingMode=p\n")
    link = tmp_path / "link.ini"
    link.symlink_to(real)
    with pytest.raises(PaperModeNotConfirmed) as ei:
        confirm_paper_profile(str(link), ME)
    assert ei.value.reason == "NOT_REGULAR"
    with pytest.raises(PaperModeNotConfirmed) as ei:
        confirm_paper_profile(str(tmp_path), ME)
    assert ei.value.reason == "NOT_REGULAR"
    with pytest.raises(PaperModeNotConfirmed) as ei:
        confirm_paper_profile(real, "someoneelse")
    assert ei.value.reason == "WRONG_OWNER"


def test_unreadable_profile(tmp_path):
    path = _ini(tmp_path, "[Logon]\ntradingMode=p\n")
    os.chmod(path, 0)
    try:
        if os.access(path, os.R_OK):
            pytest.skip("running as a user that can read mode-0 files")
        with pytest.raises(PaperModeNotConfirmed) as ei:
            confirm_paper_profile(path, ME)
        assert ei.value.reason == "UNREADABLE"
    finally:
        os.chmod(path, 0o600)


# Excerpt shaped like `netstat -anv -p tcp` on macOS 26 (process names may contain spaces).
NETSTAT = """Active Internet connections (including servers)
Proto Recv-Q Send-Q  Local Address          Foreign Address        (state)          rxbytes      txbytes  rhiwat  shiwat          process:pid    state  options           gencnt    flags   flags1 usecnt rtncnt fltrs
tcp46      0      0  *.7497                 *.*                    LISTEN                 0            0  131072  131072 JavaApplicationS:1726   00000 00000006 0000000000879358 00000000 00000800      1      0 000000
tcp4       0      0  127.0.0.1.7497         127.0.0.1.50000        ESTABLISHED            0            0  131072  131072 JavaApplicationS:1726   00102 0000000c 0000000000878491 00000080 01000800      2      0 000000
tcp6       0      0  ::1.42051              *.*                    LISTEN                 0            0  131072  131072 OneDrive Sync Se:34617  00100 00000006 0000000000426894 00000001 00000800      1      0 000000
tcp4       0      0  127.0.0.1.17497        *.*                    LISTEN                 0            0  131072  131072        python3.12:4387   00100 00000006 000000000035fd38 00000000 00000800      1      0 000000
"""


def test_parse_listeners():
    assert parse_listeners(NETSTAT, 7497) == [("*.7497", 1726)]
    assert parse_listeners(NETSTAT, 42051) == [("::1.42051", 34617)]
    assert parse_listeners(NETSTAT, 4002) == []


def test_parse_listeners_requires_pid_column():
    no_pid = "\n".join(line[:120] for line in NETSTAT.splitlines())
    with pytest.raises(ListenerOwnerUndeterminable) as ei:
        parse_listeners(no_pid, 7497)
    assert ei.value.reason == "UNDETERMINABLE"


def test_undeterminable_owner_is_its_own_type():
    with pytest.raises(ListenerOwnerUndeterminable):
        confirm_listener(7497, "paperuser", runner=_runner({}))

    def failing(argv):
        raise OSError("no netstat")
    with pytest.raises(ListenerOwnerUndeterminable):
        confirm_listener(7497, "paperuser", runner=failing)


def _runner(users: dict[int, str], netstat: str = NETSTAT):
    def run(argv):
        if argv[0].endswith("netstat"):
            return netstat
        return users.get(int(argv[-1]), "") + "\n"
    return run


def test_confirm_listener_outcomes():
    ok = confirm_listener(7497, "paperuser", runner=_runner({1726: "paperuser"}))
    assert ok["pids"] == [1726] and ok["user"] == "paperuser"
    cases = [(7497, {1726: "liveuser"}, "WRONG_USER"), (4002, {}, "NO_LISTENER"),
             (7497, {}, "UNDETERMINABLE")]
    for port, users, reason in cases:
        with pytest.raises(ListenerNotConfirmed) as ei:
            confirm_listener(port, "paperuser", runner=_runner(users))
        assert ei.value.reason == reason


def test_two_users_on_the_port_is_refused():
    extra = NETSTAT + NETSTAT.splitlines()[2].replace("*.7497 ", "::1.7497").replace(
        ":1726 ", ":9999 ") + "\n"
    with pytest.raises(ListenerNotConfirmed) as ei:
        confirm_listener(7497, "paperuser", runner=_runner({1726: "paperuser", 9999: "x"}, extra))
    assert ei.value.reason == "WRONG_USER"


@pytest.mark.skipif(sys.platform != "darwin" or not shutil.which("netstat"),
                    reason="real netstat mechanism is macOS-only")
def test_real_mechanism_attributes_own_ephemeral_listener():
    # A listener we own on an ephemeral loopback port (never a broker port, nothing connects).
    with socket.socket() as srv:
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        assert port not in {7496, 7497, 4001, 4002}
        got = confirm_listener(port, ME)
        assert got["pids"] == [os.getpid()]
        with pytest.raises(ListenerNotConfirmed):
            confirm_listener(port, "nobody-else")
