"""The Execution page shows the service's journal and offers only the emergency halt.

Drives ``dashboard/pages/8_Execution.py`` headlessly through ``streamlit.testing.v1.AppTest`` on a
state directory produced by the runtime service on invented posts.
"""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path

import pytest
from runtime_harness import DEMO_FIXTURE, SERVICE_MAPPING, Runtime, required_toml
from streamlit.testing.v1 import AppTest

REPO = Path(__file__).resolve().parents[1]
PAGE = REPO / "dashboard" / "pages" / "8_Execution.py"


def write_config(rt: Runtime, path: Path) -> Path:
    c = rt.config
    path.write_text(
        f'mode = "mock"\n[paths]\nstate_dir = "{c.paths.state_dir}"\n'
        f'halt_file = "{c.paths.halt_file}"\nmapping_file = "{SERVICE_MAPPING}"\n'
        f'[source]\nkind = "replay"\nfile = "{c.source.file}"\n' + required_toml()
    )
    tomllib.loads(path.read_text())
    return path


@pytest.fixture(scope="module")
def ran(tmp_path_factory):
    folder = tmp_path_factory.mktemp("exec_page")
    rt = Runtime(folder, posts_file=DEMO_FIXTURE)
    rt.run(90)
    return rt, write_config(rt, folder / "runtime.toml")


def page(monkeypatch, config: Path) -> AppTest:
    monkeypatch.setenv("TRADINGBOT_RUNTIME_CONFIG", str(config))
    at = AppTest.from_file(str(PAGE), default_timeout=60)
    at.run()
    assert not at.exception
    return at


def shown(at: AppTest) -> str:
    parts = []
    for kind in (
        "title",
        "subheader",
        "error",
        "warning",
        "info",
        "success",
        "markdown",
        "caption",
    ):
        parts += [str(e.value) for e in at.get(kind)]
    return "\n".join(parts + [f"{m.label} {m.value}" for m in at.metric])


def test_page_shows_the_full_lifecycle_holds_health_and_latency(monkeypatch, ran):
    rt, cfg = ran
    text = shown(page(monkeypatch, cfg))
    for needle in (
        "MOCK MODE",
        "Signal lifecycle",
        "UNSUPPORTED_INSTRUMENT_MAPPING",
        "SOURCE_EDIT_REQUIRES_REVIEW",
        "(duplicate)",
        "SIGNAL_HOLD",
        "Stage latency",
        "end to end",
        "source",
        "broker",
        "cursor 9",
        "Reconciliation clean",
        "No working order at the broker",
        "Flat",
    ):
        assert needle in text, needle
    # every recorded signal appears, in order
    for s in rt.signals():
        assert f"| {s['seq']} | {s['source_id']} r{s['revision']}" in text


def test_page_shows_protection_and_working_orders_mid_lifecycle(monkeypatch, tmp_path):
    rt = Runtime(tmp_path, posts_file=DEMO_FIXTURE)
    rt.run(3)  # only the entry has been processed
    text = shown(page(monkeypatch, write_config(rt, tmp_path / "runtime.toml")))
    assert "CONFIRMED" in text and "| STOP |" in text
    assert "| TLT STK x1 (" in text and "| 1 | 2 | 2 | yes |" in text  # contract and multiplier


def test_halt_engages_in_one_click_and_clears_only_with_the_confirmation_word(monkeypatch, ran):
    rt, cfg = ran
    halt = rt.config.paths.halt_file
    halt.unlink(missing_ok=True)
    at = page(monkeypatch, cfg)
    at.button(key="halt_engage").click().run()
    assert halt.exists() and "HALT ENGAGED" in shown(at)
    at = page(monkeypatch, cfg)
    at.text_input(key="halt_confirm").input("resume please").run()
    at.button(key="halt_clear").click().run()
    assert halt.exists() and "Not cleared" in shown(at)
    at.text_input(key="halt_confirm").input("RESUME").run()
    at.button(key="halt_clear").click().run()
    assert not halt.exists()


def test_missing_config_is_reported_not_raised(monkeypatch, tmp_path):
    at = page(monkeypatch, tmp_path / "absent.toml")
    assert "No usable runtime config" in shown(at)


def test_page_has_no_broker_or_order_path():
    tree = ast.parse(PAGE.read_text())
    imported = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module} | {
        a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names
    }
    assert not any("execution" in m or "sim_broker" in m or "service" in m for m in imported)
    source = PAGE.read_text()
    for forbidden in (".submit(", ".modify(", ".cancel(", "SimBroker", "sqlite3.connect"):
        assert forbidden not in source
