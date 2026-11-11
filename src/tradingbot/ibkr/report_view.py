"""Load and vet the latest paper-check report for display (SDK-free, no network, read-only).

Used by ``dashboard/pages/7_IBKR_Paper_Check.py``. A report is shown only if it is valid JSON of
schema ``tradingbot.ibkr.paper_check/1`` with the structure the check script writes; anything else
gets one fixed message and nothing from the file. Every string (keys included) passes through
``redact_accounts`` before it is returned, so a full account ID never reaches the page even if a
malformed or hand-edited report contains one.
"""
from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tradingbot.ibkr.check import REPORT_GLOB, REPORT_SCHEMA
from tradingbot.ibkr.redact import redact_accounts

INVALID_REPORT = (f"The most recent report is not a valid {REPORT_SCHEMA} report. Nothing from it "
                  "is shown.")
MAX_REPORT_BYTES = 4 * 1024 * 1024
VERDICTS = frozenset({"PASS", "PASS_WITH_WARNINGS", "FAIL"})
STEP_STATUSES = frozenset({"PASS", "PASS_WITH_WARNINGS", "FAIL", "SKIPPED"})


class InvalidReport(ValueError):
    """The file is not a valid report. Its message is always ``INVALID_REPORT``."""

    def __init__(self) -> None:
        super().__init__(INVALID_REPORT)


@dataclass(frozen=True)
class LoadedReport:
    name: str  # file name only
    report: dict


def redact_deep(obj: Any) -> Any:
    """Apply ``redact_accounts`` to every string, dict keys included."""
    if isinstance(obj, str):
        return redact_accounts(obj)
    if isinstance(obj, dict):
        return {redact_deep(str(k)): redact_deep(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact_deep(v) for v in obj]
    return obj


def latest_report_path(report_dir: str | os.PathLike) -> Path | None:
    """Newest ``ibkr_paper_check_<UTC stamp>[_n].json`` regular file (not a symlink), by name."""
    d = Path(report_dir).expanduser()
    if not d.is_dir():
        return None
    files = [p for p in d.glob(REPORT_GLOB) if p.is_file() and not p.is_symlink()]
    return max(files, key=lambda p: p.name, default=None)


def _reject_constant(_name: str) -> float:
    raise InvalidReport


def _iso(value: Any) -> datetime:
    if not isinstance(value, str):
        raise InvalidReport
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        raise InvalidReport from None
    if dt.tzinfo is None:
        raise InvalidReport
    return dt


def validate_report(data: Any) -> dict:
    """Structural check of what ``tradingbot.ibkr.check.run_check`` writes. Raises InvalidReport."""
    if not isinstance(data, dict) or data.get("schema") != REPORT_SCHEMA:
        raise InvalidReport
    if data.get("overall") not in VERDICTS or data.get("read_only") is not True:
        raise InvalidReport
    _iso(data.get("started_at"))
    _iso(data.get("finished_at"))
    steps = data.get("steps")
    if not isinstance(steps, list) or not steps:
        raise InvalidReport
    for st in steps:
        if not (isinstance(st, dict) and isinstance(st.get("name"), str)
                and st.get("status") in STEP_STATUSES
                and isinstance(st.get("duration_ms"), int)
                and not isinstance(st.get("duration_ms"), bool)):
            raise InvalidReport
        if "detail" in st and not isinstance(st["detail"], (dict, list)):
            raise InvalidReport
    warnings = data.get("warnings")
    if not isinstance(warnings, list) or not all(isinstance(w, str) for w in warnings):
        raise InvalidReport
    for key in ("session", "reconnect_session"):
        if key in data and not isinstance(data[key], dict):
            raise InvalidReport
    return data


def load_report(path: str | os.PathLike) -> LoadedReport:
    """Read, parse, validate, redact. Any problem raises ``InvalidReport`` (fixed message)."""
    p = Path(path)
    try:
        if p.stat().st_size > MAX_REPORT_BYTES:
            raise InvalidReport
        raw = p.read_bytes()
        data = json.loads(raw.decode("utf-8"), parse_constant=_reject_constant)
    except InvalidReport:
        raise
    except (OSError, UnicodeDecodeError, ValueError, RecursionError):
        raise InvalidReport from None
    return LoadedReport(name=redact_accounts(p.name), report=redact_deep(validate_report(data)))


def report_age_seconds(report: dict, now: datetime | None = None) -> float:
    """Seconds since the report finished (``finished_at``)."""
    now = now or datetime.now(UTC)
    return (now - _iso(report["finished_at"])).total_seconds()


def human_age(seconds: float) -> str:
    if not math.isfinite(seconds):
        return "unknown"
    if seconds < 0:
        return f"{-seconds:.0f} s in the future (clock offset?)"
    minutes = seconds / 60
    if minutes < 1:
        return f"{seconds:.0f} s"
    if minutes < 120:
        return f"{minutes:.0f} min"
    hours = minutes / 60
    return f"{hours:.1f} h" if hours < 48 else f"{hours / 24:.1f} days"


def best_attempt(detail: dict) -> dict | None:
    """The attempt the verdict is about: ``best_route`` if the report names it, else the first
    two-sided non-UNAVAILABLE attempt, else the one with the most ticks."""
    attempts = [a for a in detail.get("attempts", []) if isinstance(a, dict)]
    if not attempts:
        return None
    route = detail.get("best_route")
    for a in attempts:
        if route is not None and a.get("route") == route:
            return a
    for a in attempts:
        if a.get("bid") and a.get("ask") and a.get("quality") != "UNAVAILABLE":
            return a
    return max(attempts, key=lambda a: sum((a.get("tick_counts") or {}).values()))
