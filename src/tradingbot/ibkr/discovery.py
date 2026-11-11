"""Read-only contract discovery: the logic behind ``scripts/ibkr_contract_discovery.py``.

SDK-free at import time (the real session is imported lazily, as in ``check.py``). One session runs
a list of queries from a TOML file, sequentially, with a fixed pause between requests, then
disconnects. ``NOT_FOUND``, ``ERROR`` and ``TIMEOUT`` rows are normal results: the batch goes on.
A connection, identity, poison, read-only or cleanup failure stops the batch and makes the catalog
``FAILED`` (the only case with a non-zero exit). The catalog carries the masked account only.
"""
from __future__ import annotations

import json
import time
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from tradingbot.ibkr.check import _err, _session_facts
from tradingbot.ibkr.config import PaperConnectionConfig, load_config
from tradingbot.ibkr.errors import IbkrTimeout, ReadOnlyViolation
from tradingbot.ibkr.models import (
    DISCOVERY_FOUND,
    DISCOVERY_NOT_FOUND,
    ContractQuery,
    to_jsonable,
)

CATALOG_SCHEMA = "tradingbot.ibkr.contract_catalog/1"
QUERIES_SCHEMA = "tradingbot.ibkr.contract_discovery_queries/1"
CATALOG_PREFIX = "contract_catalog"
COMPLETE, FAILED = "COMPLETE", "FAILED"
TIMEOUT, SKIPPED, AMBIGUOUS_UNDERLYING = "TIMEOUT", "SKIPPED", "AMBIGUOUS_UNDERLYING"

_QUERY_KEYS = {"symbol": str, "sec_type": str, "exchange": str, "currency": str,
               "primary_exchange": str, "last_trade_date_or_contract_month": str,
               "multiplier": str, "trading_class": str, "local_symbol": str,
               "strike": (int, float), "right": str, "include_expired": bool}
_CHAIN_KEYS = {"symbol": str, "sec_type": str, "exchange": str, "currency": str,
               "primary_exchange": str, "fut_fop_exchange": str}
_TOP_KEYS = {"schema", "pace_seconds", "limit", "request_timeout", "contract", "option_chain"}


class QueryFileInvalid(ValueError):
    """The query list is malformed. Raised before any config is read or socket opened."""


@dataclass(frozen=True)
class NamedQuery:
    name: str
    query: ContractQuery


@dataclass(frozen=True)
class OptionChainQuery:
    name: str
    underlying: ContractQuery
    fut_fop_exchange: str = ""


@dataclass(frozen=True)
class DiscoveryPlan:
    queries: tuple[NamedQuery, ...]
    option_chains: tuple[OptionChainQuery, ...] = ()
    pace_seconds: float = 1.0
    limit: int = 50
    request_timeout: float = 15.0
    source: str = ""
    names: tuple[str, ...] = ()


def _typed(entry: dict, keys: dict, where: str) -> dict:
    unknown = set(entry) - set(keys) - {"name"}
    if unknown:
        raise QueryFileInvalid(f"{where}: unknown keys {sorted(unknown)}")
    for k, v in entry.items():
        want = str if k == "name" else keys[k]
        if isinstance(v, bool) and want is not bool or not isinstance(v, want):
            raise QueryFileInvalid(f"{where}: {k} has the wrong type")
    if not entry.get("name") or not entry.get("symbol"):
        raise QueryFileInvalid(f"{where}: name and symbol are required")
    return {k: v for k, v in entry.items() if k != "name"}


def parse_plan(data: dict, source: str = "") -> DiscoveryPlan:
    unknown = set(data) - _TOP_KEYS
    if unknown:
        raise QueryFileInvalid(f"unknown top-level keys {sorted(unknown)}")
    if data.get("schema") != QUERIES_SCHEMA:
        raise QueryFileInvalid(f"schema must be {QUERIES_SCHEMA!r}")
    pace = data.get("pace_seconds", 1.0)
    limit = data.get("limit", 50)
    timeout = data.get("request_timeout", 15.0)
    if isinstance(pace, bool) or not isinstance(pace, (int, float)) or not 0 <= pace <= 60:
        raise QueryFileInvalid("pace_seconds must be a number in 0..60")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
        raise QueryFileInvalid("limit must be an int in 1..1000")
    if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
            or not 1 <= timeout <= 120):
        raise QueryFileInvalid("request_timeout must be a number in 1..120")
    queries, chains, names = [], [], []
    for i, entry in enumerate(data.get("contract", [])):
        fields = _typed(entry, _QUERY_KEYS, f"contract[{i}]")
        if "strike" in fields:
            fields["strike"] = float(fields["strike"])
        queries.append(NamedQuery(entry["name"], ContractQuery(**fields)))
        names.append(entry["name"])
    for i, entry in enumerate(data.get("option_chain", [])):
        fields = _typed(entry, _CHAIN_KEYS, f"option_chain[{i}]")
        fop = fields.pop("fut_fop_exchange", "")
        chains.append(OptionChainQuery(entry["name"], ContractQuery(**fields), fop))
        names.append(entry["name"])
    if len(set(names)) != len(names):
        raise QueryFileInvalid("query names must be unique")
    if not names:
        raise QueryFileInvalid("no queries")
    return DiscoveryPlan(tuple(queries), tuple(chains), float(pace), limit, float(timeout),
                         source, tuple(names))


def load_plan(path: str | Path) -> DiscoveryPlan:
    try:
        data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise QueryFileInvalid(f"cannot read query file {path}: {exc}") from exc
    return parse_plan(data, str(path))


def _default_session_factory(config: PaperConnectionConfig) -> Any:
    from tradingbot.ibkr.session import IbkrSession  # imports ibapi

    return IbkrSession(config)


def run_discovery(plan: DiscoveryPlan, config_path: Any = None, *,
                  session_factory: Callable[[PaperConnectionConfig], Any] | None = None,
                  loader: Callable[[Any], PaperConnectionConfig] = load_config,
                  sleep: Callable[[float], None] = time.sleep) -> dict:
    """Connect once, run every query in order (``plan.pace_seconds`` between requests),
    disconnect, and return the catalog dict. Never raises for broker or config failures."""
    factory = session_factory or _default_session_factory
    cat: dict[str, Any] = {
        "schema": CATALOG_SCHEMA, "started_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "read_only": True, "queries_file": plan.source, "pace_seconds": plan.pace_seconds,
        "limit": plan.limit, "request_timeout": plan.request_timeout, "results": [],
        "failure": None}
    cfg = None
    try:
        cfg = loader(config_path)
        cat.update(account=cfg.masked_account(), host=cfg.host, port=cfg.port,
                   client_id=cfg.client_id, read_only_check=cfg.read_only_check)
        session = factory(cfg)
        cat["sdk_version"] = getattr(session, "sdk_version", None)
    except Exception as exc:  # noqa: BLE001 - config / session creation failure
        cat["failure"] = f"setup: {_err(exc)}"
        return _finish(cat, plan, cfg)
    try:
        session.connect()
    except (Exception, ReadOnlyViolation) as exc:  # noqa: BLE001
        cat["failure"] = f"connect: {_err(exc)}"
        cat["session"] = _session_facts(session)
        return _finish(cat, plan, cfg)
    try:
        _run_queries(session, plan, cat, sleep)
    finally:
        cat["session"] = _session_facts(session)
        try:
            session.disconnect()
        except (Exception, ReadOnlyViolation) as exc:  # noqa: BLE001
            cat["failure"] = cat["failure"] or f"disconnect: {_err(exc)}"
        cat["session"] = _session_facts(session)
        if session.threads_alive():
            cat["failure"] = cat["failure"] or f"threads still alive: {session.threads_alive()}"
    return _finish(cat, plan, cfg)


def _run_queries(s: Any, plan: DiscoveryPlan, cat: dict, sleep: Callable[[float], None]) -> None:
    if s.read_only_status != "VERIFIED":  # cannot happen after connect(); fail closed
        cat["failure"] = f"read-only status {s.read_only_status}"
        return
    jobs: list[tuple[str, Callable[[], dict]]] = [
        (q.name, lambda q=q: _contract_row(s, q, plan)) for q in plan.queries]
    jobs += [(c.name, lambda c=c: _chain_row(s, c, plan, sleep)) for c in plan.option_chains]
    for i, (name, job) in enumerate(jobs):
        if cat["failure"] is not None:
            cat["results"].append({"name": name, "status": SKIPPED,
                                   "reason": "batch stopped by an earlier failure"})
            continue
        if i:
            sleep(plan.pace_seconds)
        t0 = time.monotonic()
        try:
            row = job()
        except IbkrTimeout as exc:
            row = {"name": name, "status": TIMEOUT, "error_message": _err(exc)}
        except (Exception, ReadOnlyViolation) as exc:  # noqa: BLE001 - stops the batch
            cat["failure"] = f"{name}: {_err(exc)}"
            row = {"name": name, "status": SKIPPED, "reason": cat["failure"]}
        row["duration_ms"] = int((time.monotonic() - t0) * 1000)
        cat["results"].append(row)


def _contract_row(s: Any, nq: NamedQuery, plan: DiscoveryPlan) -> dict:
    r = s.discover_contracts(nq.query, limit=plan.limit, timeout=plan.request_timeout)
    return {"name": nq.name, "kind": "contracts", "query": to_jsonable(nq.query),
            "status": r.status, "count": r.count, "truncated": r.truncated, "limit": r.limit,
            "error_code": r.error_code, "error_message": r.error_message,
            "contracts": to_jsonable(r.contracts)}


def _chain_row(s: Any, oq: OptionChainQuery, plan: DiscoveryPlan,
               sleep: Callable[[float], None]) -> dict:
    row: dict[str, Any] = {"name": oq.name, "kind": "option_chain",
                           "query": to_jsonable(oq.underlying),
                           "fut_fop_exchange": oq.fut_fop_exchange}
    u = s.discover_contracts(oq.underlying, limit=5, timeout=plan.request_timeout)
    row["underlying"] = {"status": u.status, "count": u.count, "error_code": u.error_code,
                         "error_message": u.error_message,
                         "contracts": to_jsonable(u.contracts)}
    if u.status != DISCOVERY_FOUND or u.count != 1:
        row.update(status=u.status if u.status != DISCOVERY_FOUND else AMBIGUOUS_UNDERLYING,
                   count=0, truncated=False, error_code=u.error_code,
                   error_message=u.error_message or f"{u.count} underlying contracts matched")
        return row
    sleep(plan.pace_seconds)
    r = s.option_chain_params(u.contracts[0], fut_fop_exchange=oq.fut_fop_exchange,
                              limit=plan.limit, timeout=plan.request_timeout)
    row.update(status=r.status, count=len(r.params), truncated=r.truncated, limit=r.limit,
               underlying_con_id=r.underlying_con_id, error_code=r.error_code,
               error_message=r.error_message, params=to_jsonable(r.params))
    return row


def _finish(cat: dict, plan: DiscoveryPlan, cfg: PaperConnectionConfig | None) -> dict:
    done = {r["name"] for r in cat["results"] if r["status"] != SKIPPED}
    cleanup = cat.get("session", {}).get("cleanup_failures") or []
    if cleanup and cat["failure"] is None:
        cat["failure"] = "cleanup: " + "; ".join(cleanup)
    verified = cat.get("session", {}).get("read_only_status") == "VERIFIED"
    ok = cat["failure"] is None and verified and done == set(plan.names)
    cat["overall"] = COMPLETE if ok else FAILED
    counts: dict[str, int] = {}
    for r in cat["results"]:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    cat["status_counts"] = counts
    cat["finished_at"] = datetime.now(UTC).isoformat(timespec="seconds")
    if cfg is not None:  # belt and braces: the full account ID must never be in the catalog
        text = json.dumps(cat, default=str)
        if cfg.account in text:
            cat = json.loads(text.replace(cfg.account, cfg.masked_account()))
            cat["failure"] = (cat["failure"] or "") + " [full account ID found and masked]"
            cat["overall"] = FAILED
    return cat


def exit_code(cat: dict) -> int:
    return 0 if cat.get("overall") == COMPLETE else 1


def _is_unexpired(row: dict, today: date) -> bool:
    ltd = str(row.get("last_trade_date_or_contract_month") or "")
    if len(ltd) >= 8 and ltd[:8].isdigit():
        return ltd[:8] >= today.strftime("%Y%m%d")
    if len(ltd) == 6 and ltd.isdigit():
        return ltd >= today.strftime("%Y%m")
    return False


def nearest_unexpired(contracts: list[dict], today: date, n: int = 3) -> list[dict]:
    """The ``n`` nearest futures contracts whose last trade date (or month) is not before
    ``today`` (UTC date; an approximation of the exchange's own date)."""
    futs = [c for c in contracts if c.get("sec_type") == "FUT" and _is_unexpired(c, today)]
    futs.sort(key=lambda c: (str(c.get("last_trade_date_or_contract_month")), c.get("con_id", 0)))
    return futs[:n]


def summarize(cat: dict, today: date | None = None) -> str:
    today = today or datetime.now(UTC).date()
    sess = cat.get("session", {})
    lines = [(f"IBKR contract discovery: {cat.get('overall')}  account={cat.get('account', '?')}  "
             f"sdk={cat.get('sdk_version')}  server_version={sess.get('server_version')}  "
             f"read_only={sess.get('read_only_status')}")]
    if cat.get("failure"):
        lines.append(f"  FAILURE {cat['failure']}")
    lines.append(f"  {'query':<22} {'status':<20} {'n':>4}  detail")
    for r in cat.get("results", []):
        n = r.get("count", "")
        detail = ""
        if r.get("truncated"):
            detail = f"TRUNCATED at {r.get('limit')}; "
        if r.get("kind") == "contracts" and r.get("status") == DISCOVERY_FOUND:
            near = nearest_unexpired(r.get("contracts", []), today)
            if near:
                detail += " | ".join(
                    f"{c.get('last_trade_date_or_contract_month')} conId={c.get('con_id')} "
                    f"x{c.get('multiplier') or '-'} tick={c.get('min_tick')}" for c in near)
            else:
                c = r["contracts"][0]
                detail += (f"conId={c.get('con_id')} {c.get('sec_type')} {c.get('exchange')}/"
                           f"{c.get('primary_exchange') or '-'} tick={c.get('min_tick')} "
                           f"{c.get('long_name', '')}")
        elif r.get("kind") == "option_chain" and r.get("status") == DISCOVERY_FOUND:
            params = r.get("params", [])
            exps = {e for p in params for e in p.get("expirations", [])}
            strikes = {k for p in params for k in p.get("strikes", [])}
            detail += (f"underlying conId={r.get('underlying_con_id')} exchanges={len(params)} "
                       f"expirations={len(exps)} strikes={len(strikes)}")
        elif r.get("status") in (DISCOVERY_NOT_FOUND, "ERROR", TIMEOUT, AMBIGUOUS_UNDERLYING):
            code = r.get("error_code")
            detail += f"{'' if code is None else f'{code} '}{r.get('error_message') or ''}"
        elif r.get("status") == SKIPPED:
            detail += r.get("reason", "")
        lines.append(f"  {r['name']:<22} {r['status']:<20} {n!s:>4}  {detail}".rstrip())
    return "\n".join(lines)
