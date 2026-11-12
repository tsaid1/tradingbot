"""Order submission is DISABLED BY DEFAULT on the merged tree (SDK-free; runs in CI with and
without ``ibapi``). Each test pins one row of the table "What is disabled by default, and what
would have to be true for one order to be sent" in docs/IBKR_PAPER_ORDERS.md. Invented data only;
nothing here opens a socket or reads anything under ``~/.config``."""
from __future__ import annotations

import ast
import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
from runtime_harness import required_toml
from test_ibkr_paper_orders_pure import PAPER, write_cfg

from tradingbot.ibkr.errors import ConfigError as IbkrConfigError
from tradingbot.ibkr_orders import config as order_config
from tradingbot.ibkr_orders.config import (
    OrdersNotEnabled,
    StateDirInvalid,
    UnknownExperiment,
    load_order_config,
)
from tradingbot.ibkr_orders.counter import CounterCorrupt, SubmissionCounter, init_journal

ROOT = Path(__file__).resolve().parents[1]
ORDER_PACKAGE = "tradingbot.ibkr_orders"
# The only files outside the package that may import it (both are run by hand by the owner).
ORDER_SCRIPTS = {"scripts/ibkr_paper_experiment.py", "scripts/ibkr_paper_init_journal.py"}


def _tracked() -> list[Path]:
    try:
        out = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True,
                             check=True).stdout.decode()
        files = [ROOT / p for p in out.split("\0") if p]
    except (OSError, subprocess.CalledProcessError):  # no git: walk the tree
        skip = {".git", ".venv", "worktrees", "node_modules", "__pycache__", ".tools"}
        files = [p for p in ROOT.rglob("*") if p.is_file()
                 and not skip & set(p.relative_to(ROOT).parts)]
    return [p for p in files if p.is_file()]


def _rel(p: Path) -> str:
    return p.relative_to(ROOT).as_posix()


def _imports_order_package(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(a.name == ORDER_PACKAGE or a.name.startswith(ORDER_PACKAGE + ".")
                   for a in node.names):
                return True
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            if node.module == ORDER_PACKAGE or node.module.startswith(ORDER_PACKAGE + "."):
                return True
            if node.module == "tradingbot" and any(a.name == "ibkr_orders" for a in node.names):
                return True
    return False


# --- row 1: nothing outside the package and its two scripts can reach it ------------------------
def test_only_the_order_package_and_its_two_scripts_import_it():
    importers = set()
    for p in _tracked():
        rel = _rel(p)
        if p.suffix != ".py" or rel.startswith("tests/"):
            continue
        if _imports_order_package(ast.parse(p.read_text(), filename=rel)):
            importers.add(rel)
    inside = {r for r in importers if r.startswith("src/tradingbot/ibkr_orders/")}
    assert importers - inside == ORDER_SCRIPTS


def test_no_shipped_non_test_file_names_the_order_package_dynamically():
    """No ``importlib.import_module("tradingbot.ibkr_orders...")``, shell line, launchd template
    or config that names the package or its scripts, outside the package, its two scripts and
    the docs."""
    allowed_prefixes = ("src/tradingbot/ibkr_orders/", "tests/", "docs/")
    hits = []
    for p in _tracked():
        rel = _rel(p)
        if rel in ORDER_SCRIPTS or rel.startswith(allowed_prefixes) or p.suffix == ".md":
            continue  # prose (AGENTS.md, README.md) may name it; it executes nothing
        try:
            text = p.read_text()
        except (UnicodeDecodeError, OSError):
            continue
        if ("ibkr_orders" in text or "ibkr_paper_experiment" in text
                or "ibkr_paper_init_journal" in text or "PaperOrderSession" in text
                or "IbkrPaperBroker" in text):
            hits.append(rel)
    assert hits == []


def test_runtime_execution_core_and_read_only_adapter_do_not_load_the_order_package():
    code = (
        "import sys\n"
        "import tradingbot.runtime.app, tradingbot.runtime.service, tradingbot.runtime.config\n"
        "import tradingbot.execution.coordinator, tradingbot.execution.sim_broker\n"
        "import tradingbot.ibkr.config, tradingbot.ibkr.identity\n"
        "try:\n"
        "    import tradingbot.ibkr.session, tradingbot.ibkr.check\n"
        "except ModuleNotFoundError as exc:\n"
        "    assert 'ibapi' in str(exc), exc\n"
        "bad = sorted(m for m in sys.modules if m.startswith('tradingbot.ibkr_orders'))\n"
        "assert not bad, bad\n")
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    r = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True,
                       check=False)
    assert r.returncode == 0, r.stderr


# --- row 2: the read-only adapter's outgoing allowlist is unchanged -------------------------------
READ_ONLY_OUT = {"START_API", "REQ_CONTRACT_DATA", "CANCEL_CONTRACT_DATA", "REQ_ACCOUNT_SUMMARY",
                 "CANCEL_ACCOUNT_SUMMARY", "REQ_POSITIONS", "CANCEL_POSITIONS", "REQ_MKT_DATA",
                 "CANCEL_MKT_DATA", "REQ_MARKET_DATA_TYPE", "REQ_CONFIG", "REQ_SEC_DEF_OPT_PARAMS"}


def test_read_only_allowlist_is_exactly_the_reviewed_read_set():
    tree = ast.parse((ROOT / "src/tradingbot/ibkr/session.py").read_text())
    names = None
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "ALLOWED_OUT_MESSAGES" for t in node.targets):
            names = {n.attr for n in ast.walk(node.value)
                     if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
                     and n.value.id == "OUT"}
    assert names == READ_ONLY_OUT
    assert not names & {"PLACE_ORDER", "CANCEL_ORDER", "REQ_GLOBAL_CANCEL", "EXERCISE_OPTIONS",
                        "UPDATE_CONFIG", "REPLACE_FA", "REQ_FA", "REQ_IDS"}


def test_read_only_allowlist_by_message_id_with_the_sdk():
    pytest.importorskip("ibapi")
    from ibapi.message import OUT

    from tradingbot.ibkr.session import ALLOWED_CLIENT_METHODS, ALLOWED_OUT_MESSAGES
    assert ALLOWED_OUT_MESSAGES == {getattr(OUT, n) for n in READ_ONLY_OUT}
    assert not {int(m) for m in ALLOWED_OUT_MESSAGES} & {3, 4, 8, 21, 58, 109, 18, 19}
    order_methods = {"placeOrder", "cancelOrder", "placeOrderProtoBuf", "cancelOrderProtoBuf",
                     "reqGlobalCancel", "reqGlobalCancelProtoBuf", "exerciseOptions",
                     "exerciseOptionsProtoBuf", "replaceFA", "replaceFAProtoBuf",
                     "updateConfigProtoBuf", "reqIds", "reqIdsProtoBuf", "reqAllOpenOrders",
                     "reqOpenOrders", "reqAutoOpenOrders"}
    assert not order_methods & ALLOWED_CLIENT_METHODS


# --- row 3: the runtime service runs the simulated broker only --------------------------------------
# Two modes exist, and both simulate every order, fill, position and the account: "mock" (an
# invented market) and "shadow" (the same SimBroker at read-only real quotes). Neither can be
# configured to build an order-capable broker.
SIMULATED_MODES = ("mock", "shadow")
SHADOW_ACCOUNT = "SIM-SHADOW-0001"


def _runtime_raw(tmp_path: Path, mode: str) -> dict:
    text = (f'mode = "{mode}"\n[paths]\nstate_dir = "state"\nhalt_file = "HALT"\n'
            f'mapping_file = "{ROOT / "config" / "instrument_mapping.toml"}"\n'
            '[source]\nkind = "replay"\nfile = "posts.jsonl"\n' + required_toml())
    raw = tomllib.loads(text)
    if mode == "shadow":
        raw["source"]["compression"] = 1.0
        raw["execution"]["expected_account"] = SHADOW_ACCOUNT
        raw["shadow"] = {"paper_config": "private/paper.toml"}
    return raw


@pytest.mark.parametrize("mode", ["paper", "ibkr", "ibkr-paper", "paper-test-1", "live",
                                  "orders", "MOCK", "SHADOW", "shadow-live", "shadow-paper"])
def test_the_runtime_accepts_only_its_simulated_modes(tmp_path, mode):
    from tradingbot.runtime.config import ACCEPTED_MODES, ConfigError, parse_config
    assert ACCEPTED_MODES == SIMULATED_MODES
    for ok in SIMULATED_MODES:
        assert parse_config(_runtime_raw(tmp_path, ok), base=tmp_path).mode == ok
    with pytest.raises(ConfigError):
        parse_config(_runtime_raw(tmp_path, mode), base=tmp_path)
    with pytest.raises(ConfigError):  # nor with the shadow table of a valid shadow config
        parse_config({**_runtime_raw(tmp_path, "shadow"), "mode": mode}, base=tmp_path)


@pytest.mark.parametrize("base", SIMULATED_MODES)
@pytest.mark.parametrize("extra", [{"broker": "ibkr-paper"}, {"orders": "paper-test-1"},
                                   {"ibkr": {"orders": "paper-test-1"}},
                                   {"ibkr_orders": {"enabled": True}}])
def test_the_runtime_config_has_no_key_that_selects_another_broker(tmp_path, extra, base):
    from tradingbot.runtime.config import ConfigError, parse_config
    with pytest.raises(ConfigError):
        parse_config({**_runtime_raw(tmp_path, base), **extra}, base=tmp_path)


@pytest.mark.parametrize("extra", [{"orders": "paper-test-1"}, {"broker": "ibkr-paper"},
                                   {"session": "PaperOrderSession"}, {"market_data_type": 1},
                                   {"account": "DU1234567"}, {"read_only": False}])
def test_the_shadow_table_has_no_key_that_selects_orders_or_a_real_account(tmp_path, extra):
    from tradingbot.runtime.config import ConfigError, parse_config
    raw = _runtime_raw(tmp_path, "shadow")
    raw["shadow"] = {**raw["shadow"], **extra}
    if "account" in extra:
        raw["execution"]["expected_account"] = extra["account"]
    with pytest.raises(ConfigError):
        parse_config(raw, base=tmp_path)


def test_shadow_mode_reports_a_simulated_account_only(tmp_path):
    from tradingbot.runtime.config import ConfigError, parse_config
    cfg = parse_config(_runtime_raw(tmp_path, "shadow"), base=tmp_path)
    assert cfg.simulated_account == cfg.execution.expected_account == SHADOW_ACCOUNT
    raw = _runtime_raw(tmp_path, "shadow")
    raw["execution"]["expected_account"] = "SIM-PAPER-0001"  # not the shadow broker's account
    with pytest.raises(ConfigError, match="expected_account"):
        parse_config(raw, base=tmp_path)


def _called_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    out = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Call):
            f = n.func
            out.add(f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute)
                    else "")
    return out


def test_the_runtime_builds_the_simulated_broker_only():
    from tradingbot.runtime.app import build_service
    from tradingbot.runtime.config import ConfigError, RuntimeConfig
    for mode in ("paper", "live", "orders", "SHADOW"):
        forced = RuntimeConfig.model_construct(mode=mode)  # past validation, on purpose
        with pytest.raises(ConfigError):
            build_service(forced)
    for p in (ROOT / "src/tradingbot/runtime").rglob("*.py"):
        tree = ast.parse(p.read_text())
        mods = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        mods |= {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
        assert not {m for m in mods if m.startswith(("tradingbot.ibkr", "ibapi"))}, p.name
    # Every broker construction in the wiring: the SimBroker (mock) and the shadow builder,
    # which composes that same SimBroker with a read-only session (checked below). Any other
    # call naming a broker is refused.
    built = {n for n in _called_names(ROOT / "src/tradingbot/runtime/app.py")
             if "broker" in n.lower()}
    assert built == {"SimBroker", "build_shadow_broker"}
    shadow = ROOT / "src/tradingbot/execution/shadow_broker.py"
    assert {n for n in _called_names(shadow) if n.endswith("Broker")} == {"SimBroker",
                                                                        "ShadowBroker"}
    assert {n for n in _called_names(shadow) if n.endswith("Session")} == {"IbkrSession"}


def test_the_shadow_broker_has_no_send_path_of_its_own():
    """Every order method of the shadow broker delegates to the simulator; the module names no
    order call, no order module and no raw transport, and takes only the exact read-only session
    class (a subclass is refused before connect: tests/test_shadow_mode_sdk.py)."""
    from tradingbot.execution.shadow_broker import ShadowBroker
    path = ROOT / "src/tradingbot/execution/shadow_broker.py"
    text = path.read_text()
    for name in ("placeOrder", "cancelOrder", "reqGlobalCancel", "exerciseOptions",
                 "updateConfig", "sendMsg", "PLACE_ORDER", "ibkr_orders", "socket", "_client",
                 "_RAW_CONNECTIONS", "import_module"):
        assert name not in text, name
    assert "type(session) is not IbkrSession" in text
    tree = ast.parse(text)
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "ShadowBroker")
    for fn in (n for n in cls.body if isinstance(n, ast.FunctionDef)):
        if fn.name in ("submit", "modify", "cancel", "modify_condition"):
            calls = {f"{c.func.value.attr}.{c.func.attr}" for c in ast.walk(fn)
                     if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
                     and isinstance(c.func.value, ast.Attribute)}
            assert calls & {"_sim.submit", "_sim.modify", "_sim.cancel", "_sim.modify_condition"}
            assert not {c for c in calls if c.startswith("_session.")}, fn.name
    assert not hasattr(ShadowBroker, "set_quote")  # no invented market either


def test_runtime_and_shadow_modules_load_only_the_read_only_adapter_modules():
    """Transitively: nothing the runtime, the shadow broker, the dashboard's view or the service
    script loads is the order package, and the adapter modules loaded are the read-only ones."""
    code = (
        "import sys\n"
        "import tradingbot.runtime.app, tradingbot.runtime.service, tradingbot.runtime.lifecycle\n"
        "import tradingbot.execution.shadow_broker, tradingbot.execution.shadow_quotes\n"
        "import runpy; runpy.run_path('scripts/run_service.py', run_name='not_main')\n"
        "bad = sorted(m for m in sys.modules if m.startswith('tradingbot.ibkr_orders'))\n"
        "assert not bad, bad\n"
        "ibkr = sorted(m for m in sys.modules if m.startswith('tradingbot.ibkr'))\n"
        "print(','.join(ibkr))\n")
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    r = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True,
                       check=False, cwd=ROOT)
    assert r.returncode == 0, r.stderr
    loaded = set(r.stdout.strip().split(","))
    assert loaded <= {"tradingbot.ibkr", "tradingbot.ibkr.config", "tradingbot.ibkr.errors",
                      "tradingbot.ibkr.models", "tradingbot.ibkr.redact"}, loaded


# --- row 4: an order-capable session needs three things the owner writes by hand ----------------
def test_no_orders_entry_no_state_dir_or_no_journal_means_no_order_capability(tmp_path):
    with pytest.raises(OrdersNotEnabled):  # the read-only keys alone
        load_order_config(write_cfg(tmp_path, orders=None, state_dir=None))
    with pytest.raises(UnknownExperiment):
        load_order_config(write_cfg(tmp_path, orders="paper-test-2"))
    with pytest.raises(StateDirInvalid):
        load_order_config(write_cfg(tmp_path, state_dir=str(tmp_path / "absent")))
    cfg = load_order_config(write_cfg(tmp_path))  # all three written, but no journal yet
    c = SubmissionCounter(cfg.state_dir, cfg.experiment, cfg.connection.masked_account())
    with pytest.raises(CounterCorrupt):  # the session never creates it: count unknown
        c.open()
    assert c.used is None
    assert not list(cfg.state_dir.rglob("*"))  # nothing was created by trying
    init_journal(cfg.state_dir, cfg.experiment, cfg.connection.masked_account())
    c.open()
    try:
        assert c.used == 0
    finally:
        c.close()


def test_the_default_order_config_path_is_private_and_absent_by_default(tmp_path, monkeypatch):
    assert str(order_config.DEFAULT_ORDER_CONFIG_PATH) == \
        "~/.config/tradingbot/ibkr/paper_orders.toml"
    monkeypatch.setenv("HOME", str(tmp_path))  # an empty home: the owner wrote nothing
    with pytest.raises(IbkrConfigError):
        load_order_config()


def test_the_repo_ships_no_order_config_and_no_template_that_works_as_is():
    tomls, found = [], []
    for p in _tracked():
        rel = _rel(p)
        if "paper_orders" in p.name and p.suffix != ".py":  # a config, its template or a copy
            found.append(rel)
        if p.suffix == ".toml" or ".toml" in p.name:
            tomls.append(p)
    assert found == []
    assert tomls  # the scan saw the shipped configs
    for p in tomls:
        try:
            data = tomllib.loads(p.read_text())
        except tomllib.TOMLDecodeError:
            continue
        assert "orders" not in data, _rel(p)
        with pytest.raises((IbkrConfigError, ValueError)):
            order_config._validate_order_config(data)


# --- row 6: the funded account cannot be reached through the config ------------------------------
@pytest.mark.parametrize("account", ["U1234567", "F1234567", "I1234567", "1234567", "du1234567"])
def test_only_a_paper_account_id_can_be_configured(tmp_path, account):
    with pytest.raises(IbkrConfigError):
        load_order_config(write_cfg(tmp_path, account=account))
    assert PAPER.startswith("D")
