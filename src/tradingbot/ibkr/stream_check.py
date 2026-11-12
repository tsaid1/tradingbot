"""Read-only held-quote-stream check: the logic behind ``scripts/ibkr_quote_stream_check.py``.

SDK-free at import time (the real session is imported lazily, as in ``check.py``). One session
connects (preflight, identity gate and read-only verification all apply), then for each contract
query in turn: resolve it, open one held stream, poll ``latest()`` for a bounded hold, close it.
It records what only a real TWS can show: the market-data type requested and reported, the tick
classes per side, post-image updates per side, how long after subscribing each side first moved,
the largest per-side age seen while holding, and how often the quote passed the entry freshness
check. The report carries the masked account only.
"""
from __future__ import annotations

import json
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any

from tradingbot.ibkr.check import _err, _session_facts
from tradingbot.ibkr.config import PaperConnectionConfig, load_config
from tradingbot.ibkr.discovery import _QUERY_KEYS, QueryFileInvalid
from tradingbot.ibkr.errors import ReadOnlyViolation
from tradingbot.ibkr.models import (
    MARKET_DATA_TYPE_NAMES,
    ContractQuery,
    entry_quote_check,
    to_jsonable,
)

STREAM_CHECK_SCHEMA = "tradingbot.ibkr.quote_stream_check/1"
STREAM_CHECK_PREFIX = "ibkr_quote_stream_check"
DEFAULT_HOLD_SECONDS, MAX_HOLD_SECONDS = 20.0, 120.0
MAX_CONTRACTS = 10
DEFAULT_MAX_AGE_SECONDS = 3.0  # the approved entry rule's per-side limit
POLL_SECONDS = 0.25
COMPLETE, FAILED = "COMPLETE", "FAILED"
DONE, NOT_RESOLVED, SKIPPED = "DONE", "NOT_RESOLVED", "SKIPPED"


def parse_contract_arg(text: str) -> ContractQuery:
    """``symbol=ES,sec_type=FUT,exchange=CME,last_trade_date_or_contract_month=202612``: the same
    fields (and types) as a ``[[contract]]`` entry of the discovery query file."""
    fields: dict[str, Any] = {}
    for part in text.split(","):
        if not part.strip():
            continue
        key, sep, value = part.partition("=")
        key, value = key.strip(), value.strip()
        if not sep or key not in _QUERY_KEYS:
            raise QueryFileInvalid(f"--contract: unknown or malformed field {part.strip()!r}")
        if key in fields:
            raise QueryFileInvalid(f"--contract: {key} given twice")
        want = _QUERY_KEYS[key]
        if want is bool:
            if value not in ("true", "false"):
                raise QueryFileInvalid(f"--contract: {key} must be true or false")
            fields[key] = value == "true"
        elif key == "strike":
            try:
                fields[key] = float(value)
            except ValueError:
                raise QueryFileInvalid("--contract: strike must be a number") from None
        else:
            fields[key] = value
    if not fields.get("symbol"):
        raise QueryFileInvalid("--contract: symbol is required")
    return ContractQuery(**fields)


def validate_args(queries: Sequence[ContractQuery], hold_seconds: float, market_data_type: int,
                  max_age_seconds: float) -> None:
    """Every argument check, before the private config is read or any socket opens."""
    if not queries or len(queries) > MAX_CONTRACTS:
        raise QueryFileInvalid(f"1..{MAX_CONTRACTS} contracts required")
    if (isinstance(hold_seconds, bool) or not isinstance(hold_seconds, (int, float))
            or not 1 <= hold_seconds <= MAX_HOLD_SECONDS):
        raise QueryFileInvalid(f"--seconds must be in 1..{MAX_HOLD_SECONDS:.0f}")
    if market_data_type not in MARKET_DATA_TYPE_NAMES:
        raise QueryFileInvalid("--market-data-type must be 1, 2, 3 or 4")
    if (isinstance(max_age_seconds, bool) or not isinstance(max_age_seconds, (int, float))
            or not 0 < max_age_seconds <= 60):
        raise QueryFileInvalid("--max-age must be in (0, 60]")


def _default_session_factory(config: PaperConnectionConfig) -> Any:
    from tradingbot.ibkr.session import IbkrSession  # imports ibapi

    return IbkrSession(config)


def run_stream_check(queries: Sequence[ContractQuery], config_path: Any = None, *,
                     hold_seconds: float = DEFAULT_HOLD_SECONDS, market_data_type: int = 3,
                     max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS,
                     session_factory: Callable[[PaperConnectionConfig], Any] | None = None,
                     loader: Callable[[Any], PaperConnectionConfig] = load_config,
                     sleep: Callable[[float], None] = time.sleep,
                     clock: Callable[[], float] = time.monotonic) -> dict:
    """Connect once, hold one stream per query in turn, disconnect; return the report dict.
    Never raises for broker or config failures."""
    validate_args(queries, hold_seconds, market_data_type, max_age_seconds)
    factory = session_factory or _default_session_factory
    rep: dict[str, Any] = {
        "schema": STREAM_CHECK_SCHEMA,
        "started_at": datetime.now(UTC).isoformat(timespec="seconds"), "read_only": True,
        "hold_seconds": float(hold_seconds), "requested_market_data_type": market_data_type,
        "max_age_seconds": float(max_age_seconds), "results": [], "failure": None}
    cfg = None
    try:
        cfg = loader(config_path)
        rep.update(account=cfg.masked_account(), host=cfg.host, port=cfg.port,
                   client_id=cfg.client_id, read_only_check=cfg.read_only_check)
        session = factory(cfg)
        rep["sdk_version"] = getattr(session, "sdk_version", None)
    except Exception as exc:  # noqa: BLE001 - config / session creation failure
        rep["failure"] = f"setup: {_err(exc)}"
        return _finish(rep, queries, cfg)
    try:
        session.connect()
    except (Exception, ReadOnlyViolation) as exc:  # noqa: BLE001
        rep["failure"] = f"connect: {_err(exc)}"
        rep["session"] = _session_facts(session)
        return _finish(rep, queries, cfg)
    try:
        if session.read_only_status != "VERIFIED":  # cannot happen after connect(); fail closed
            rep["failure"] = f"read-only status {session.read_only_status}"
        for q in queries:
            if rep["failure"] is not None:
                rep["results"].append({"query": to_jsonable(q), "status": SKIPPED,
                                       "reason": "stopped by an earlier failure"})
                continue
            try:
                rep["results"].append(_hold_one(session, q, hold_seconds, market_data_type,
                                                max_age_seconds, sleep, clock))
            except (Exception, ReadOnlyViolation) as exc:  # noqa: BLE001 - stops the batch
                rep["failure"] = f"{q.symbol}: {_err(exc)}"
                rep["results"].append({"query": to_jsonable(q), "status": SKIPPED,
                                       "reason": rep["failure"]})
    finally:
        try:
            session.disconnect()
        except (Exception, ReadOnlyViolation) as exc:  # noqa: BLE001
            rep["failure"] = rep["failure"] or f"disconnect: {_err(exc)}"
        rep["session"] = _session_facts(session)
        if session.threads_alive():
            rep["failure"] = rep["failure"] or f"threads still alive: {session.threads_alive()}"
    return _finish(rep, queries, cfg)


def _hold_one(s: Any, q: ContractQuery, hold: float, mdt: int, max_age: float,
              sleep: Callable[[float], None], clock: Callable[[], float]) -> dict:
    from tradingbot.ibkr.errors import AmbiguousContract, ContractNotFound

    row: dict[str, Any] = {"query": to_jsonable(q)}
    try:
        contract = s.resolve_contract(q)
    except (ContractNotFound, AmbiguousContract) as exc:
        row.update(status=NOT_RESOLVED, error=_err(exc))
        return row
    row["contract"] = to_jsonable(contract)
    stream = s.open_quote_stream(contract, market_data_type=mdt)
    t0 = clock()
    first: dict[str, float | None] = {"bid": None, "ask": None}
    max_ages: dict[str, float | None] = {"bid": None, "ask": None}
    polls = usable = 0
    try:
        while True:
            snap = stream.latest()
            now = clock()
            polls += 1
            usable += entry_quote_check(snap, now_monotonic=snap.observed_at_monotonic,
                                        max_age_seconds=max_age).usable
            for side in ("bid", "ask"):
                if first[side] is None and getattr(snap, f"{side}_updates") >= 1:
                    first[side] = round(now - t0, 3)
                at = getattr(snap, f"{side}_at_monotonic")
                if at is not None and snap.observed_at_monotonic is not None:
                    age = snap.observed_at_monotonic - at
                    max_ages[side] = round(max(max_ages[side] or 0.0, age), 3)
            if now - t0 >= hold:
                break
            sleep(min(POLL_SECONDS, max(0.0, hold - (now - t0))))
        final_check = entry_quote_check(snap, now_monotonic=snap.observed_at_monotonic,
                                        max_age_seconds=max_age)
    finally:
        final = stream.close()  # CleanupError propagates and stops the batch
    row.update(
        status=DONE, stream_id=stream.stream_id, requested_market_data_type=mdt,
        reported_market_data_type=final.market_data_type_name,
        market_data_types_delivered=list(final.market_data_types_delivered),
        tick_classes={"bid": final.bid_tick_class, "ask": final.ask_tick_class},
        updates={"bid": final.bid_updates, "ask": final.ask_updates},
        age_at_end_seconds={"bid": final_check.bid_age_seconds,
                            "ask": final_check.ask_age_seconds},
        first_update_after_open_seconds=first, max_age_while_held_seconds=max_ages,
        polls=polls, polls_usable_for_entry=usable, quality=final.quality.value,
        quality_reason=final.quality_reason, entry_usable_at_end=final_check.usable,
        entry_reasons_at_end=list(final_check.reasons), notices=list(final.notices),
        flags=list(final.flags), final=to_jsonable(final))
    return row


def _finish(rep: dict, queries: Sequence[ContractQuery], cfg: PaperConnectionConfig | None,
            ) -> dict:
    cleanup = rep.get("session", {}).get("cleanup_failures") or []
    if cleanup and rep["failure"] is None:
        rep["failure"] = "cleanup: " + "; ".join(cleanup)
    verified = rep.get("session", {}).get("read_only_status") == "VERIFIED"
    processed = [r for r in rep["results"] if r["status"] != SKIPPED]
    ok = rep["failure"] is None and verified and len(processed) == len(queries)
    rep["overall"] = COMPLETE if ok else FAILED
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
    sess = rep.get("session", {})
    lines = [(f"IBKR quote stream check: {rep.get('overall')}  account={rep.get('account', '?')}  "
              f"sdk={rep.get('sdk_version')}  server_version={sess.get('server_version')}  "
              f"read_only={sess.get('read_only_status')}  hold={rep.get('hold_seconds')}s  "
              f"max_age={rep.get('max_age_seconds')}s")]
    if rep.get("failure"):
        lines.append(f"  FAILURE {rep['failure']}")
    for r in rep.get("results", []):
        sym = r.get("query", {}).get("symbol", "?")
        if r["status"] != DONE:
            lines.append(f"  {sym:<10} {r['status']}  {r.get('error') or r.get('reason') or ''}")
            continue
        u, a, f, m = (r["updates"], r["age_at_end_seconds"],
                      r["first_update_after_open_seconds"], r["max_age_while_held_seconds"])
        lines.append(
            f"  {sym:<10} {r['quality']:<11} {r['quality_reason']}  requested="
            f"{MARKET_DATA_TYPE_NAMES.get(r['requested_market_data_type'])} reported="
            f"{r['reported_market_data_type']}  ticks bid={r['tick_classes']['bid']} "
            f"ask={r['tick_classes']['ask']}  updates bid={u['bid']} ask={u['ask']}  "
            f"age_end bid={a['bid']} ask={a['ask']}  first_update bid={f['bid']} "
            f"ask={f['ask']}  max_age bid={m['bid']} ask={m['ask']}  entry_ok="
            f"{r['polls_usable_for_entry']}/{r['polls']}  notices={r['notices']}  "
            f"entry_reasons_end={r['entry_reasons_at_end']}")
    return "\n".join(lines)
