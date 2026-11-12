"""Shadow mode without the SDK (runs in CI): config refusals, the service's own guard, mock mode
unchanged, and the runtime importing and running with ``ibapi`` blocked. Invented data only;
nothing here opens a socket or reads anything under ``~/.config``."""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from runtime_harness import MAPPING, Runtime, post
from test_ibkr_paper_disabled_by_default import _runtime_raw

from tradingbot.execution.sim_broker import SimBroker
from tradingbot.runtime.config import ConfigError, parse_config
from tradingbot.runtime.service import RuntimeService

ROOT = Path(__file__).resolve().parents[1]
ENTRY = "We are now long $TLT at 88.42. Stop 87.20."  # at the ask: the limit is the stated price (PR #70)


def _paper_file(folder: Path) -> Path:
    d = folder / "private"
    d.mkdir(mode=0o700, exist_ok=True)
    p = d / "paper.toml"
    p.write_text('mode = "paper"\nhost = "127.0.0.1"\nport = 7497\nclient_id = 7\n'
                 'account = "DU9990001"\ntws_profile_ini = "/Users/someone/Jts/jts.ini"\n'
                 'tws_os_user = "someone"\nread_only_check = "config-read"\n')
    os.chmod(p, stat.S_IRUSR | stat.S_IWUSR)
    return p


# ------------------------------------------------------------------ config
@pytest.mark.parametrize("change,match", [
    (lambda r: r.pop("shadow"), "paper_config"),
    (lambda r: r.update(shadow={}), "paper_config"),
    (lambda r: r.update(mock={"quotes": {"TLT": {"bid": "1", "ask": "2"}}}), r"\[mock\]"),
    (lambda r: r.update(mock={}), r"\[mock\]"),
    (lambda r: r["source"].pop("compression"), "historical replay"),
    (lambda r: r["execution"].update(expected_account="SIM-OTHER"), "expected_account"),
    (lambda r: r["shadow"].update(account="DU1234567"), "account"),
])
def test_a_shadow_config_is_refused_unless_complete_and_consistent(tmp_path, change, match):
    raw = _runtime_raw(tmp_path, "shadow")
    change(raw)
    with pytest.raises(ConfigError, match=match):
        parse_config(raw, base=tmp_path)


def test_mock_mode_refuses_a_shadow_table(tmp_path):
    raw = _runtime_raw(tmp_path, "mock")
    raw["shadow"] = {"paper_config": "private/paper.toml"}
    with pytest.raises(ConfigError, match=r"\[shadow\]"):
        parse_config(raw, base=tmp_path)


def test_the_paper_config_path_resolves_against_the_config_file(tmp_path):
    cfg = parse_config(_runtime_raw(tmp_path, "shadow"), base=tmp_path)
    assert cfg.shadow.paper_config == tmp_path / "private" / "paper.toml"


def test_the_shipped_shadow_example_parses_and_its_posts_are_invented():
    from tradingbot.runtime.config import load_config
    path = ROOT / "config" / "runtime.shadow.example.toml"
    cfg = load_config(path)
    assert cfg.mode == "shadow" and cfg.execution.expected_account == cfg.shadow.account
    assert str(cfg.shadow.paper_config).endswith("tradingbot/ibkr/paper.toml")
    assert cfg.source.kind == "replay" and cfg.source.compression
    rows = [json.loads(line) for line in cfg.source.file.read_text().splitlines() if line]
    assert rows and all(r["provenance"] == "SYNTHETIC" for r in rows)


def test_the_service_refuses_shadow_mode_on_any_other_broker(tmp_path):
    rt = Runtime(tmp_path, [], mapping_file=MAPPING)
    rt.service.close()
    shadow = rt.config.model_copy(update={"mode": "shadow"})
    with pytest.raises(ValueError, match="ShadowBroker only"):
        RuntimeService(shadow, source=rt.source, broker=SimBroker(tmp_path / "x.sqlite3"),
                       clock=rt.clock.now, store=rt.store)


# ------------------------------------------------------------------ mock mode unchanged
def test_mock_mode_journal_and_heartbeat_carry_nothing_of_shadow_mode(tmp_path):
    rt = Runtime(tmp_path, [post("1", 0, ENTRY)])
    rt.run(3)
    [a] = [a for a in rt.actions() if a["action"] == "OPEN"]
    assert a["status"] == "EXECUTED"
    hb = rt.heartbeat()
    assert hb["mode"] == "mock" and "shadow" not in hb
    events = rt.store.events()
    assert not [e for e in events if e["event"].startswith("SHADOW_")]
    [start] = [e for e in events if e["event"] == "SERVICE_START"]
    assert set(start["detail"]) == {"pid", "source", "cursor", "restart", "what_if_mapping"}
    assert type(rt.service.broker) is SimBroker
    rt.service.close()


def test_state_directories_are_never_shared_between_modes(tmp_path):
    from tradingbot.runtime.app import SHADOW_MARKER, _check_state_dir_mode
    from tradingbot.runtime.store import RuntimeStore
    rt = Runtime(tmp_path, [])
    rt.run(1)
    rt.service.close()
    store = RuntimeStore(rt.config.runtime_db)
    shadow = rt.config.model_copy(update={"mode": "shadow"})
    with pytest.raises(ConfigError, match="mock mode"):
        _check_state_dir_mode(shadow, store)
    store.put(SHADOW_MARKER, True, rt.clock.now())
    with pytest.raises(ConfigError, match="shadow mode"):
        _check_state_dir_mode(rt.config, store)


# ------------------------------------------------------------------ the SDK blocked (CI)
BLOCK = textwrap.dedent("""
    import importlib.abc, sys
    class Block(importlib.abc.MetaPathFinder):
        def find_spec(self, name, path, target=None):
            if name == "ibapi" or name.startswith("ibapi."):
                raise ModuleNotFoundError("ibapi blocked", name=name)
    sys.meta_path.insert(0, Block())
""")


def _run_blocked(code: str, tmp_path: Path) -> subprocess.CompletedProcess:
    env = {**os.environ, "PYTHONPATH": f"{ROOT / 'src'}{os.pathsep}{ROOT / 'tests'}",
           "HOME": str(tmp_path)}
    return subprocess.run([sys.executable, "-c", BLOCK + textwrap.dedent(code)], env=env,
                          capture_output=True, text=True, check=False, cwd=tmp_path)


def test_with_the_sdk_blocked_the_runtime_imports_and_runs_mock_mode(tmp_path):
    r = _run_blocked(f"""
        import sys
        from pathlib import Path
        from runtime_harness import Runtime, post
        rt = Runtime(Path({str(tmp_path)!r}), [post("1", 0, {ENTRY!r})])
        rt.run(3)
        [a] = [a for a in rt.actions() if a["action"] == "OPEN"]
        assert a["status"] == "EXECUTED", a
        rt.service.close()
        assert not [m for m in sys.modules if m.startswith("ibapi")]
        print("ok")
    """, tmp_path)
    assert r.returncode == 0 and r.stdout.strip() == "ok", r.stderr


def test_with_the_sdk_blocked_shadow_mode_refuses_to_start_cleanly(tmp_path):
    paper = _paper_file(tmp_path)
    r = _run_blocked(f"""
        from tradingbot.execution.shadow_broker import ShadowStartRefused, build_shadow_broker
        try:
            build_shadow_broker(sim_db={str(tmp_path / 's.sqlite3')!r}, account="SIM-SHADOW-0001",
                                paper_config={str(paper)!r})
        except ShadowStartRefused as exc:
            assert "ibapi" in str(exc), exc
            assert "DU9990001" not in str(exc)
            print("refused")
    """, tmp_path)
    assert r.returncode == 0 and r.stdout.strip() == "refused", r.stderr
