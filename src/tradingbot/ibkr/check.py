"""Read-only paper connectivity check: the logic behind ``scripts/ibkr_paper_check.py``.

SDK-free at import time (the real session is imported lazily), so the step/verdict/report logic is
tested offline with a fake session. Overall ``PASS`` requires every step, including both
disconnects, to pass with no warning (read-only verified by the config read). ``PASS_WITH_WARNINGS``
(exit 0) means every step passed but the quote step delivered only delayed/frozen or partial data.
Anything else is ``FAIL``, including a quote step where nothing arrived on either route.
"""
from __future__ import annotations

import json
import os
import stat
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tradingbot.ibkr.config import PaperConnectionConfig, load_config
from tradingbot.ibkr.models import TLT_QUERY, to_jsonable
from tradingbot.ibkr.redact import redact_accounts

REPORT_SCHEMA = "tradingbot.ibkr.paper_check/1"
PASS, FAIL, SKIPPED = "PASS", "FAIL", "SKIPPED"
PASS_WITH_WARNINGS = "PASS_WITH_WARNINGS"
NO_MARKET_DATA = "no market data delivered"


def _default_session_factory(config: PaperConnectionConfig) -> Any:
    from tradingbot.ibkr.session import IbkrSession  # imports ibapi

    return IbkrSession(config)


def _err(exc: BaseException) -> str:
    text = f"{type(exc).__name__}: {exc}"
    for note in getattr(exc, "__notes__", ()):
        text += f" [{note}]"
    return redact_accounts(text)


class _Steps:
    def __init__(self) -> None:
        self.steps: list[dict] = []

    def run(self, name: str, fn: Callable[[], Any], detail: Callable[[Any], dict] | None = None,
            ) -> tuple[bool, Any]:
        t0 = time.monotonic()
        try:
            value = fn()
        except Exception as exc:  # noqa: BLE001 - every failure becomes a FAIL step
            self.steps.append({"name": name, "status": FAIL, "duration_ms": _ms(t0),
                               "error": _err(exc)})
            return False, exc
        entry = {"name": name, "status": PASS, "duration_ms": _ms(t0)}
        if detail is not None:
            entry["detail"] = to_jsonable(detail(value))
        self.steps.append(entry)
        return True, value

    def fail(self, name: str, error: str) -> None:
        self.steps.append({"name": name, "status": FAIL, "duration_ms": 0, "error": error})

    def skip(self, name: str, why: str) -> None:
        self.steps.append({"name": name, "status": SKIPPED, "duration_ms": 0, "reason": why})


def _ms(t0: float) -> int:
    return int((time.monotonic() - t0) * 1000)


def _session_facts(s: Any) -> dict:
    return {
        "server_version": getattr(s, "server_version", None),
        "tws_connection_time": getattr(s, "connection_time", None),
        "connected_at": getattr(s, "connected_at", None),
        "notices": [{"reqId": r, "code": c, "message": m} for r, c, m in s.notices],
        "connection_events": [{"reqId": r, "code": c, "message": m}
                              for r, c, m in s.connection_events],
        "unrouted_errors": [{"reqId": r, "code": c, "message": m} for r, c, m in s.unrouted_errors],
        "cleanup_failures": list(s.cleanup_failures),
        "internal_errors": list(s.internal_errors),
        "poisoned": bool(s.poisoned),
        "preflight": getattr(s, "preflight_result", None),
        "read_only_status": getattr(s, "read_only_status", None),
        "read_only_settings": getattr(s, "read_only_settings", None),
        "reader_joined": getattr(s, "reader_joined", None),
    }


def run_check(config_path: str | os.PathLike | None = None, *,
              session_factory: Callable[[PaperConnectionConfig], Any] | None = None,
              loader: Callable[[Any], PaperConnectionConfig] = load_config) -> dict:
    """Run every step and return the report dict (never raises for broker/config failures)."""
    factory = session_factory or _default_session_factory
    started = datetime.now(UTC).isoformat(timespec="seconds")
    steps = _Steps()
    report: dict[str, Any] = {"schema": REPORT_SCHEMA, "started_at": started, "read_only": True}
    warnings: list[str] = []
    report["notes"] = []

    ok, cfg = steps.run("load_config", lambda: loader(config_path))
    if not ok:
        return _finish(report, steps, warnings, None)
    report.update(account=cfg.masked_account(), mode=cfg.mode, host=cfg.host, port=cfg.port,
                  client_id=cfg.client_id, tws_os_user=cfg.tws_os_user,
                  read_only_check=cfg.read_only_check)

    ok, session = steps.run("create_session", lambda: factory(cfg))
    if not ok:
        return _finish(report, steps, warnings, cfg)
    report["sdk_version"] = getattr(session, "sdk_version", None)

    connected, _ = steps.run("connect_identity_gate", session.connect)
    report["session"] = _session_facts(session)
    if connected:
        if session.read_only_status != "VERIFIED":  # cannot happen after connect(); fail closed
            steps.fail("read_only_verified", f"read-only status {session.read_only_status}")
        _data_steps(session, steps, warnings, report["notes"])
    else:
        for name in ("resolve_contract", "account_summary", "positions", "quote"):
            steps.skip(name, "identity gate did not pass")

    clean, _ = steps.run("disconnect", session.disconnect)
    report["session"] = _session_facts(session)
    _check_joined(session, connected, steps, "threads_joined")
    if session.cleanup_failures:
        steps.fail("request_cleanup", "; ".join(session.cleanup_failures))

    if connected and clean:
        _reconnect(cfg, factory, steps, report)
    else:
        steps.skip("reconnect_identity_gate", "first connection did not complete cleanly")
        steps.skip("reconnect_disconnect", "first connection did not complete cleanly")
    return _finish(report, steps, warnings, cfg)


def _check_joined(s: Any, connected: bool, steps: _Steps, name: str) -> None:
    if s.threads_alive():
        steps.fail(name, f"threads still alive: {s.threads_alive()}")
    elif connected and not s.reader_joined:
        steps.fail(name, "EReader thread was not observed to exit")


def _data_steps(s: Any, steps: _Steps, warnings: list[str], notes: list[str]) -> None:
    ok, contract = steps.run("resolve_contract", lambda: s.resolve_contract(TLT_QUERY),
                             lambda c: c)

    def check_summary() -> Any:
        summary = s.account_summary()
        if summary.missing_tags:
            raise ValueError(f"account summary missing tags {list(summary.missing_tags)}")
        return summary

    steps.run("account_summary", check_summary, lambda summary: summary)
    steps.run("positions", s.positions, lambda rows: {"count": len(rows), "rows": rows})
    if not ok:
        steps.skip("quote", "contract not resolved")
        return
    ok, report = steps.run("quote", lambda: s.quote(contract), lambda r: {
        "verdict": r.status, "reason": r.reason, "attempts": r.attempts})
    if ok:
        entry = steps.steps[-1]
        entry["status"] = report.status  # PASS | PASS_WITH_WARNINGS | FAIL (with evidence)
        if report.status == FAIL:
            entry["error"] = report.reason
        elif report.status == PASS_WITH_WARNINGS:
            warnings.append(f"TLT quote: {report.reason}")
        if report.status == FAIL or not report.best.has_price:
            warnings.append(f"{NO_MARKET_DATA} for TLT")


def _reconnect(cfg: PaperConnectionConfig, factory: Callable, steps: _Steps, report: dict) -> None:
    ok, s2 = steps.run("reconnect_create_session", lambda: factory(cfg))
    if not ok:
        steps.skip("reconnect_identity_gate", "session not created")
        return
    connected, _ = steps.run("reconnect_identity_gate", s2.connect)
    steps.run("reconnect_disconnect", s2.disconnect)
    _check_joined(s2, connected, steps, "reconnect_threads_joined")
    report["reconnect_session"] = _session_facts(s2)
    if s2.cleanup_failures:
        steps.fail("reconnect_cleanup", "; ".join(s2.cleanup_failures))


def _finish(report: dict, steps: _Steps, warnings: list[str],
            cfg: PaperConnectionConfig | None) -> dict:
    report["steps"] = steps.steps
    report["warnings"] = warnings
    report["finished_at"] = datetime.now(UTC).isoformat(timespec="seconds")
    failed = any(st["status"] == FAIL for st in steps.steps)
    expected = ["load_config", "create_session", "connect_identity_gate", "resolve_contract",
                "account_summary", "positions", "quote", "disconnect",
                "reconnect_identity_gate", "reconnect_disconnect"]
    passed = {st["name"] for st in steps.steps if st["status"] in (PASS, PASS_WITH_WARNINGS)}
    if failed or not all(n in passed for n in expected):
        report["overall"] = FAIL
    else:
        verified = report.get("session", {}).get("read_only_status") == "VERIFIED"
        if not verified or report.get("read_only_check") != "config-read":
            report["overall"] = FAIL
        else:
            report["overall"] = PASS_WITH_WARNINGS if warnings else PASS
    if cfg is not None:  # belt and braces: the full account ID must never be in the report
        text = json.dumps(report, default=str)
        if cfg.account in text:
            report = json.loads(text.replace(cfg.account, cfg.masked_account()))
            report["steps"].append({"name": "redaction_guard", "status": FAIL, "duration_ms": 0,
                                    "error": "full account ID found in report and masked"})
            report["overall"] = FAIL
    return report


def write_report(report: dict, report_dir: str | os.PathLike) -> Path:
    """Write ``ibkr_paper_check_<UTC>.json`` (0600) into an owner-only (0700) directory."""
    d = Path(report_dir).expanduser()
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    st = os.lstat(d)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
        raise PermissionError(f"report dir must be a real directory owned by you: {d}")
    if stat.S_IMODE(st.st_mode) & 0o077:
        os.chmod(d, 0o700)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    for n in range(100):
        path = d / f"ibkr_paper_check_{stamp}{'' if n == 0 else f'_{n}'}.json"
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                         0o600)
        except FileExistsError:
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2, sort_keys=False, default=str)
            fh.write("\n")
        return path
    raise FileExistsError(f"could not create a unique report file in {d}")


def summarize(report: dict) -> str:
    head = (f"IBKR paper check: {report['overall']}  account={report.get('account', '?')}  "
            f"sdk={report.get('sdk_version')}  server_version="
            f"{report.get('session', {}).get('server_version')}  "
            f"read_only_check={report.get('read_only_check', '?')}")
    if any(w.startswith(NO_MARKET_DATA) for w in report.get("warnings", [])):
        head += f"  [{NO_MARKET_DATA}]"
    lines = [head]
    for st in report["steps"]:
        extra = st.get("error") or st.get("reason") or ""
        lines.append(f"  {st['status']:<7} {st['name']:<26} {st['duration_ms']:>6} ms  {extra}")
    for w in report.get("warnings", []):
        lines.append(f"  WARNING {w}")
    for n in report.get("notes", []):
        lines.append(f"  NOTE    {n}")
    return "\n".join(lines)

