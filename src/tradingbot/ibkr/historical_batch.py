"""Read-only historical-bars batch: the logic behind ``scripts/ibkr_historical_bars.py``.

SDK-free at import time (the real session is imported lazily, as in ``discovery.py``). The request
file and the private output path are validated before any config is read or socket opened. One
session runs the requests one by one (the adapter's pacing applies), then disconnects, and ONE
JSON report is written to the private path (0600, never inside the repository, masked account
only). A broker pacing violation, a local pacing refusal, or a connection, identity, poison,
read-only or cleanup failure stops the batch (remaining rows ``SKIPPED``, report ``FAILED``).
"""
from __future__ import annotations

import json
import os
import stat
import time
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tradingbot.ibkr.check import _err, _session_facts
from tradingbot.ibkr.config import PaperConnectionConfig, load_config
from tradingbot.ibkr.errors import ReadOnlyViolation
from tradingbot.ibkr.historical import (
    COMPLETE,
    DEFAULT_HISTORICAL_TIMEOUT,
    FAILED,
    HIST_MAX_PER_WINDOW,
    MAX_HISTORICAL_TIMEOUT,
    HistoricalBarsRequest,
    HistoricalContract,
    HistoricalRequestInvalid,
    request_to_jsonable,
    result_to_jsonable,
    zone,
)

REPORT_SCHEMA = "tradingbot.ibkr.historical_bars_report/1"
REQUESTS_SCHEMA = "tradingbot.ibkr.historical_bars_requests/1"
SKIPPED = "SKIPPED"

_CONTRACT_KEYS = {"con_id": int, "symbol": str, "sec_type": str, "exchange": str,
                  "currency": str, "primary_exchange": str,
                  "last_trade_date_or_contract_month": str, "multiplier": str,
                  "trading_class": str, "local_symbol": str, "include_expired": bool}
_REQUEST_KEYS = {"name", "contract", "end", "time_zone", "duration", "bar_size", "what_to_show",
                 "regular_hours_only"}
_TOP_KEYS = {"schema", "request_timeout", "request"}


class RequestFileInvalid(ValueError):
    """The request file is malformed. Raised before any config is read or socket opened."""


class OutputPathRefused(ValueError):
    """The report path is unsafe (inside the repository, existing, a symlink, no directory)."""


@dataclass(frozen=True)
class NamedRequest:
    name: str
    request: HistoricalBarsRequest


@dataclass(frozen=True)
class BarsPlan:
    requests: tuple[NamedRequest, ...]
    request_timeout: float = DEFAULT_HISTORICAL_TIMEOUT
    source: str = ""


def _typed(value: Any, want: type, where: str) -> Any:
    if isinstance(value, bool) and want is not bool or not isinstance(value, want):
        raise RequestFileInvalid(f"{where} has the wrong type")
    return value


def _end(entry: dict, where: str) -> datetime:
    end, tz = entry.get("end"), entry.get("time_zone")
    if not isinstance(end, datetime):
        raise RequestFileInvalid(f"{where}.end must be a TOML date-time (with an offset, or a "
                                 "local date-time plus time_zone)")
    if tz is not None:
        _typed(tz, str, f"{where}.time_zone")
        if end.tzinfo is not None:
            raise RequestFileInvalid(f"{where}: give either an offset in end or time_zone")
        return end.replace(tzinfo=zone(tz))
    if end.tzinfo is None:
        raise RequestFileInvalid(f"{where}.end has no offset and no time_zone (naive refused)")
    return end


def parse_plan(data: dict, source: str = "") -> BarsPlan:
    unknown = set(data) - _TOP_KEYS
    if unknown:
        raise RequestFileInvalid(f"unknown top-level keys {sorted(unknown)}")
    if data.get("schema") != REQUESTS_SCHEMA:
        raise RequestFileInvalid(f"schema must be {REQUESTS_SCHEMA!r}")
    timeout = data.get("request_timeout", DEFAULT_HISTORICAL_TIMEOUT)
    if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
            or not 1 <= timeout <= MAX_HISTORICAL_TIMEOUT):
        raise RequestFileInvalid(f"request_timeout must be a number in 1..{MAX_HISTORICAL_TIMEOUT}")
    entries = data.get("request", [])
    if not isinstance(entries, list) or not entries:
        raise RequestFileInvalid("no [[request]] entries")
    if len(entries) > HIST_MAX_PER_WINDOW:
        raise RequestFileInvalid(f"at most {HIST_MAX_PER_WINDOW} requests per file")
    out, names = [], set()
    for i, entry in enumerate(entries):
        where = f"request[{i}]"
        if not isinstance(entry, dict):
            raise RequestFileInvalid(f"{where} must be a table")
        unknown = set(entry) - _REQUEST_KEYS
        if unknown:
            raise RequestFileInvalid(f"{where}: unknown keys {sorted(unknown)}")
        missing = sorted(_REQUEST_KEYS - {"time_zone"} - set(entry))
        if missing:
            raise RequestFileInvalid(f"{where}: missing keys {missing}")
        name = _typed(entry["name"], str, f"{where}.name")
        if not name or name in names:
            raise RequestFileInvalid(f"{where}: name must be non-empty and unique")
        names.add(name)
        contract = _typed(entry["contract"], dict, f"{where}.contract")
        bad = set(contract) - set(_CONTRACT_KEYS)
        if bad:
            raise RequestFileInvalid(f"{where}.contract: unknown keys {sorted(bad)}")
        for k, v in contract.items():
            _typed(v, _CONTRACT_KEYS[k], f"{where}.contract.{k}")
        try:
            req = HistoricalBarsRequest(
                contract=HistoricalContract(**contract), end=_end(entry, where),
                duration=_typed(entry["duration"], str, f"{where}.duration"),
                bar_size=_typed(entry["bar_size"], str, f"{where}.bar_size"),
                what_to_show=_typed(entry["what_to_show"], str, f"{where}.what_to_show"),
                regular_hours_only=_typed(entry["regular_hours_only"], bool,
                                          f"{where}.regular_hours_only"))
        except HistoricalRequestInvalid as exc:
            raise RequestFileInvalid(f"{where}: {exc}") from exc
        out.append(NamedRequest(name, req))
    return BarsPlan(tuple(out), float(timeout), source)


def load_plan(path: str | Path) -> BarsPlan:
    try:
        data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise RequestFileInvalid(f"cannot read request file {path}: {exc}") from exc
    return parse_plan(data, str(path))


def _identity(path: Path) -> tuple[int, int] | None:
    """(device, inode), None when the path does not exist; other errors propagate (refused)."""
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return None
    return (st.st_dev, st.st_ino)


def _repository_identities(repo: Path) -> set[tuple[int, int]]:
    """File identities of the repository: this checkout, its git directory, the main checkout and
    every linked worktree recorded under ``<git dir>/worktrees/`` (as in runtime/vocab_review)."""
    places = [repo]
    dotgit, common = repo / ".git", None
    if dotgit.is_dir():
        common = dotgit
    elif dotgit.is_file():
        text = dotgit.read_text(encoding="utf-8").strip()
        if text.startswith("gitdir:"):
            gd = Path(text[len("gitdir:"):].strip())
            gd = gd if gd.is_absolute() else repo / gd
            common = gd.parent.parent if gd.parent.name == "worktrees" else gd
    if common is not None:
        places.append(common)
        if common.name == ".git":
            places.append(common.parent)
        worktrees = common / "worktrees"
        if worktrees.is_dir():
            for d in worktrees.iterdir():
                if (d / "gitdir").is_file():
                    places.append(Path((d / "gitdir").read_text(encoding="utf-8").strip()).parent)
    return {i for i in map(_identity, places) if i is not None}


def check_output_path(path: str | os.PathLike, repo_root: str | os.PathLike) -> Path:
    """The private report path: not inside the repository, not existing, not a symlink, in an
    existing directory owned by the caller.

    "Inside the repository" is decided by FILE IDENTITY (device, inode) of every existing
    ancestor against this checkout, the main checkout, every linked worktree and the git
    directory, never only by comparing strings: a case variant on a case-insensitive volume or a
    symlinked ancestor cannot slip past. Any error while checking refuses."""
    try:
        return _check_output_path(path, repo_root)
    except OutputPathRefused:
        raise
    except (OSError, ValueError) as exc:
        raise OutputPathRefused(f"report path could not be checked ({type(exc).__name__}); "
                                "refused") from None


def _check_output_path(path: str | os.PathLike, repo_root: str | os.PathLike) -> Path:
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = Path.cwd() / p
    if p.is_symlink() or p.exists():
        raise OutputPathRefused(f"report path already exists (never overwritten): {p}")
    resolved = p.parent.resolve() / p.name
    repo = Path(repo_root).resolve()
    if resolved == repo or resolved.is_relative_to(repo):
        raise OutputPathRefused("report path is inside the repository; use a private directory")
    ours = _repository_identities(repo)
    for folder in (resolved.parent, *resolved.parent.parents):
        if _identity(folder) in ours:
            raise OutputPathRefused(f"report path is inside the repository ({folder} is the "
                                    "same directory as a checkout or the git directory)")
    try:
        st = os.stat(resolved.parent)
    except OSError as exc:
        raise OutputPathRefused(f"report directory does not exist: {resolved.parent}") from exc
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
        raise OutputPathRefused(f"report directory must be a directory you own: {resolved.parent}")
    return resolved


def write_private_report(report: dict, path: Path) -> Path:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        os.fchmod(fh.fileno(), 0o600)
        json.dump(report, fh, indent=2, default=str)
        fh.write("\n")
    return path


def _default_session_factory(config: PaperConnectionConfig) -> Any:
    from tradingbot.ibkr.session import IbkrSession  # imports ibapi

    return IbkrSession(config)


def run_batch(plan: BarsPlan, config_path: Any = None, *,
              session_factory: Callable[[PaperConnectionConfig], Any] | None = None,
              loader: Callable[[Any], PaperConnectionConfig] = load_config) -> dict:
    """Connect once, run the requests in order, disconnect; return the report dict. Never raises
    for broker or config failures."""
    factory = session_factory or _default_session_factory
    rep: dict[str, Any] = {
        "schema": REPORT_SCHEMA, "started_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "read_only": True, "requests_file": plan.source, "request_timeout": plan.request_timeout,
        "results": [], "failure": None}
    cfg = None
    try:
        cfg = loader(config_path)
        rep.update(account=cfg.masked_account(), host=cfg.host, port=cfg.port,
                   client_id=cfg.client_id, read_only_check=cfg.read_only_check)
        session = factory(cfg)
        rep["sdk_version"] = getattr(session, "sdk_version", None)
    except Exception as exc:  # noqa: BLE001 - config / session creation failure
        rep["failure"] = f"setup: {_err(exc)}"
        return _finish(rep, plan, cfg)
    try:
        session.connect()
    except (Exception, ReadOnlyViolation) as exc:  # noqa: BLE001
        rep["failure"] = f"connect: {_err(exc)}"
        rep["session"] = _session_facts(session)
        return _finish(rep, plan, cfg)
    try:
        _run_requests(session, plan, rep)
    finally:
        try:
            session.disconnect()
        except (Exception, ReadOnlyViolation) as exc:  # noqa: BLE001
            rep["failure"] = rep["failure"] or f"disconnect: {_err(exc)}"
        rep["session"] = _session_facts(session)
        rep["session"]["historical_unrouted_bars"] = getattr(
            session, "historical_unrouted_bars", None)
        rep["session"]["historical_unrouted_ends"] = getattr(
            session, "historical_unrouted_ends", None)
        if session.threads_alive():
            rep["failure"] = rep["failure"] or f"threads still alive: {session.threads_alive()}"
    return _finish(rep, plan, cfg)


def _run_requests(s: Any, plan: BarsPlan, rep: dict) -> None:
    if s.read_only_status != "VERIFIED":  # cannot happen after connect(); fail closed
        rep["failure"] = f"read-only status {s.read_only_status}"
        return
    for i, nr in enumerate(plan.requests):
        row: dict[str, Any] = {"index": i, "name": nr.name}
        if rep["failure"] is not None:
            row.update(status=SKIPPED, reason=SKIPPED, request=request_to_jsonable(nr.request),
                       detail="batch stopped by an earlier failure")
            rep["results"].append(row)
            continue
        t0 = time.monotonic()
        try:
            result = s.historical_bars(nr.request, timeout=plan.request_timeout)
            row.update(result_to_jsonable(result))
            if result.stops_batch:
                rep["failure"] = f"request {i}: {result.reason}"
        except (Exception, ReadOnlyViolation) as exc:  # noqa: BLE001 - stops the batch
            rep["failure"] = f"request {i}: {_err(exc)}"
            row.update(status=SKIPPED, reason=type(exc).__name__,
                       request=request_to_jsonable(nr.request), detail=rep["failure"])
        row["duration_ms"] = int((time.monotonic() - t0) * 1000)
        rep["results"].append(row)


NOT_RUN = "NOT_RUN"


def _finish(rep: dict, plan: BarsPlan, cfg: PaperConnectionConfig | None) -> dict:
    if not rep["results"]:  # setup or connect failed: one row per request, with a reason code
        rep["results"] = [{"index": i, "name": nr.name, "status": SKIPPED, "reason": NOT_RUN,
                           "request": request_to_jsonable(nr.request),
                           "detail": f"not run: {rep['failure'] or 'no session'}"}
                          for i, nr in enumerate(plan.requests)]
    cleanup = rep.get("session", {}).get("cleanup_failures") or []
    if cleanup and rep["failure"] is None:
        rep["failure"] = "cleanup: " + "; ".join(cleanup)
    verified = rep.get("session", {}).get("read_only_status") == "VERIFIED"
    ran = [r for r in rep["results"] if r.get("status") != SKIPPED]
    ok = rep["failure"] is None and verified and len(ran) == len(plan.requests)
    rep["overall"] = COMPLETE if ok else FAILED
    counts: dict[str, int] = {}
    for r in rep["results"]:
        counts[r["reason"]] = counts.get(r["reason"], 0) + 1
    rep["reason_counts"] = counts
    rep["bars_total"] = sum(r.get("count", 0) or 0 for r in rep["results"])
    rep["finished_at"] = datetime.now(UTC).isoformat(timespec="seconds")
    if cfg is not None:  # belt and braces: the full account ID must never be in the report
        text = json.dumps(rep, default=str)
        if cfg.account in text:
            rep = json.loads(text.replace(cfg.account, cfg.masked_account()))
            rep["failure"] = (rep["failure"] or "") + " [full account ID found and masked]"
            rep["overall"] = FAILED
    return rep


def exit_code(rep: dict) -> int:
    return 0 if rep.get("overall") == COMPLETE else 1


def summarize(rep: dict) -> str:
    """Counts and reason codes only: no names, prices, contract fields or broker text."""
    sess = rep.get("session", {})
    lines = [(f"IBKR historical bars: {rep.get('overall')}  account={rep.get('account', '?')}  "
             f"read_only={sess.get('read_only_status')}  requests={len(rep.get('results', []))}"
             f"  bars={rep.get('bars_total', 0)}")]
    if rep.get("failure"):
        lines.append(f"  FAILURE at {str(rep['failure']).split(':', 1)[0]} (details in the report)")
    for r in rep.get("results", []):
        code = r.get("error_code")
        lines.append(f"  #{r['index']:<3} {r['reason']:<28} bars={r.get('count', 0) or 0:<5}"
                     f"{'' if code is None else f' code={code}'}")
    lines.append("  reasons: " + ", ".join(f"{k}={v}" for k, v in
                                          sorted(rep.get("reason_counts", {}).items())))
    return "\n".join(lines)
