"""scripts/run_service.py and scripts/run_mock_service_demo.py as an operator runs them."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from runtime_harness import DEMO_FIXTURE, MAPPING

REPO = Path(__file__).resolve().parents[1]
ENV = {**os.environ, "PYTHONPATH": str(REPO / "src")}


def config(tmp_path: Path, mode: str = "mock", extra: str = "") -> Path:
    p = tmp_path / "runtime.toml"
    p.write_text(
        f'mode = "{mode}"\n{extra}[paths]\nstate_dir = "state"\nhalt_file = "HALT"\n'
        f'mapping_file = "{MAPPING}"\n[source]\nkind = "replay"\nfile = "{DEMO_FIXTURE}"\n'
        "[service]\n[policy]\nquantity = 2\nquantity_per_add = 2\n"
        '[mock]\nquotes = { TLT = { bid = "88.40", ask = "88.42" } }\n'
    )
    return p


def run(*args: str, timeout: float = 120) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, *args],
        check=False,
        capture_output=True,
        text=True,
        env=ENV,
        cwd=REPO,
        timeout=timeout,
    )


def test_once_runs_one_cycle_and_exits_zero(tmp_path):
    out = run("scripts/run_service.py", "--config", str(config(tmp_path)), "--once")
    assert out.returncode == 0, out.stderr
    report = json.loads(out.stdout)
    # Historical replay without compression: the whole file in one cycle.
    assert report["posts_processed"] == 9 and report["faults"] == []
    assert (tmp_path / "state" / "heartbeat.json").exists()


def test_a_second_once_resumes_from_the_cursor(tmp_path):
    cfg = str(config(tmp_path))
    assert run("scripts/run_service.py", "--config", cfg, "--once").returncode == 0
    again = json.loads(run("scripts/run_service.py", "--config", cfg, "--once").stdout)
    assert again["posts_processed"] == 0


def test_non_mock_mode_refuses_to_start(tmp_path):
    out = run("scripts/run_service.py", "--config", str(config(tmp_path, mode="paper")), "--once")
    assert out.returncode == 2 and "only 'mock'" in out.stderr
    assert not (tmp_path / "state").exists()


def test_unknown_key_refuses_to_start(tmp_path):
    out = run(
        "scripts/run_service.py",
        "--config",
        str(config(tmp_path, extra="surprise = true\n")),
        "--once",
    )
    assert out.returncode == 2


def test_source_override_and_bad_override(tmp_path):
    posts = tmp_path / "one.jsonl"
    posts.write_text(DEMO_FIXTURE.read_text().splitlines()[0] + "\n")
    cfg = str(config(tmp_path))
    out = run("scripts/run_service.py", "--config", cfg, "--once", "--source", f"replay:{posts}")
    assert out.returncode == 0 and json.loads(out.stdout)["posts_processed"] == 1
    bad = run("scripts/run_service.py", "--config", cfg, "--once", "--source", "x:whatever")
    assert bad.returncode == 2


def test_mock_demo_exits_zero_with_every_check_passing(tmp_path):
    out = run("scripts/run_mock_service_demo.py", "--state-dir", str(tmp_path / "demo"))
    assert out.returncode == 0, out.stdout + out.stderr
    assert "[FAIL]" not in out.stdout and out.stdout.count("[PASS]") == 7
    assert "KILLED" in out.stdout and "RESTARTED" in out.stdout
