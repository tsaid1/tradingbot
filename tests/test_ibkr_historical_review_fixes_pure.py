"""Review fixes for historical bars (orchestration/reviews/IBKR_HISTORICAL_BARS_review.md), pure
part: S1 report path by file identity, S2 non-positive prices, N3 end bounds, N4 window, N5 pacer
key and latch, N7 sub-second offsets, N8 setup failure rows, N9 include_expired. SDK-free."""
from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from tradingbot.ibkr import historical as H
from tradingbot.ibkr.historical import (
    HistoricalBarsRequest,
    HistoricalContract,
    HistoricalPacer,
    HistoricalRequestInvalid,
)
from tradingbot.ibkr.historical_batch import (
    OutputPathRefused,
    check_output_path,
    parse_plan,
    run_batch,
)

ROOT = Path(__file__).resolve().parents[1]
END = datetime(2026, 3, 9, 14, 15, tzinfo=UTC)
FUT = HistoricalContract(con_id=100000001, sec_type="FUT", exchange="NYMEX",
                         include_expired=True)


def req(**kw) -> HistoricalBarsRequest:
    opts = {"contract": FUT, "end": END, "duration": "1800 S", "bar_size": "1 min",
            "what_to_show": "TRADES", "regular_hours_only": False}
    opts.update(kw)
    return HistoricalBarsRequest(**opts)


def bar(date, o=1.0, h=2.0, lo=0.5, c=1.5, request=None):
    return H.build_bar(date, o, h, lo, c, volume=Decimal(1), wap=None, bar_count=1,
                       request=request or req(), previous=None)


# --- S1: "inside the repository" by file identity ------------------------------------------------
def _case_insensitive(p: Path) -> bool:
    flipped = Path(str(p).swapcase())
    try:
        return flipped.exists() and os.path.samefile(flipped, p)
    except OSError:
        return False


@pytest.mark.skipif(not _case_insensitive(ROOT), reason="case-sensitive file system")
def test_report_path_with_different_case_inside_the_repository_is_refused():
    for variant in (str(ROOT).upper(), str(ROOT).swapcase(),
                    str(ROOT.parent) + "/" + ROOT.name.upper()):
        with pytest.raises(OutputPathRefused, match="inside the repository"):
            check_output_path(Path(variant) / "x.json", ROOT)
        with pytest.raises(OutputPathRefused, match="inside the repository"):
            check_output_path(Path(variant) / "DOCS" / "x.json", ROOT)


def test_report_path_through_a_symlinked_ancestor_is_refused(tmp_path):
    link = tmp_path / "alias"
    link.symlink_to(ROOT / "docs")
    with pytest.raises(OutputPathRefused, match="inside the repository"):
        check_output_path(link / "x.json", ROOT)


def test_report_path_in_another_checkout_of_the_same_repository_is_refused(tmp_path):
    # A linked worktree's ``.git`` file points at <main>/.git/worktrees/<name>; the main
    # checkout and every other worktree are the same repository.
    main = tmp_path / "main"
    (main / ".git" / "worktrees" / "wt").mkdir(parents=True)
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / ".git").write_text(f"gitdir: {main / '.git' / 'worktrees' / 'wt'}\n")
    (main / ".git" / "worktrees" / "wt" / "gitdir").write_text(str(wt / ".git") + "\n")
    other = tmp_path / "other_wt"
    (other / "sub").mkdir(parents=True)
    (main / ".git" / "worktrees" / "o").mkdir()
    (main / ".git" / "worktrees" / "o" / "gitdir").write_text(str(other / ".git") + "\n")
    for target in (main / "x.json", other / "sub" / "x.json", wt / "x.json"):
        with pytest.raises(OutputPathRefused, match="inside the repository"):
            check_output_path(target, wt)
    assert check_output_path(tmp_path / "x.json", wt) == tmp_path.resolve() / "x.json"


# --- S2: zero, negative and -1 prices fail closed ----------------------------------------------
@pytest.mark.parametrize("prices", [(0, 0, 0, 0), (-1, -1, -1, -1), (1, 2, -1, 1.5),
                                    (1, 2, 0.5, 0), (-37.6, -30.0, -40.0, -37.0)])
def test_non_positive_prices_fail_the_read(prices):
    with pytest.raises(H.BarMalformed, match="positive"):
        bar("1773065640", *prices)


# --- N3: end bounds ------------------------------------------------------------------------------
def test_end_in_the_future_or_before_the_floor_is_refused(monkeypatch):
    now = datetime(2026, 3, 9, 15, 0, tzinfo=UTC)
    monkeypatch.setattr(H, "_now", lambda: now)
    req(end=now + timedelta(seconds=H.HIST_END_FUTURE_TOLERANCE_SECONDS))
    with pytest.raises(HistoricalRequestInvalid, match="future"):
        req(end=now + timedelta(seconds=H.HIST_END_FUTURE_TOLERANCE_SECONDS + 1))
    with pytest.raises(HistoricalRequestInvalid, match="future"):
        req(end=datetime(2099, 1, 1, tzinfo=UTC))
    req(end=H.HIST_END_FLOOR)
    for early in (H.HIST_END_FLOOR - timedelta(seconds=1), datetime(1, 1, 2, tzinfo=UTC)):
        with pytest.raises(HistoricalRequestInvalid, match="before"):
            req(end=early)


# --- N4: bars outside the requested window -------------------------------------------------------
def test_intraday_bar_before_the_window_fails_the_read():
    start = int((END - timedelta(seconds=1800)).timestamp())
    assert bar(str(start)).start_utc == END - timedelta(seconds=1800)
    assert bar(str(start - 60)).start_utc  # one bar of tolerance (alignment)
    with pytest.raises(H.BarMalformed, match="before the requested window"):
        bar(str(start - 61))
    with pytest.raises(H.BarMalformed, match="before the requested window"):
        bar(str(int((END - timedelta(hours=10)).timestamp())))


def test_trading_day_durations_allow_weekends_but_not_more():
    r = req(duration="1 D")
    ok = int((END - timedelta(days=1 + H.HIST_CALENDAR_SLACK_DAYS)).timestamp())
    assert bar(str(ok), request=r)
    with pytest.raises(H.BarMalformed, match="before the requested window"):
        bar(str(ok - 1), request=r)
    daily = req(bar_size="1 day", duration="1 W")
    assert bar("20260226", request=daily)  # 7 + 4 days back from 2026-03-09
    with pytest.raises(H.BarMalformed, match="before the requested window"):
        bar("20260225", request=daily)


# --- N5: pacer key is canonical, latch cannot be reset -------------------------------------------
def test_identical_request_key_ignores_how_the_contract_is_written():
    a = req(contract=HistoricalContract(con_id=7, exchange="NYMEX"))
    b = req(contract=HistoricalContract(con_id=7, exchange="nymex", local_symbol="XYZZ6",
                                        symbol="XYZ"))
    assert a.pacing_key() == b.pacing_key()
    f1 = req(contract=HistoricalContract(symbol="xyz", sec_type="FUT", exchange="NYMEX",
                                         currency="usd", last_trade_date_or_contract_month="20261218",
                                         multiplier="1000", trading_class="XYZ"))
    f2 = req(contract=HistoricalContract(symbol="XYZ", sec_type="FUT", exchange="NYMEX",
                                         currency="USD", last_trade_date_or_contract_month="20261218",
                                         multiplier="1000.0", trading_class="XYZ",
                                         local_symbol="XYZZ6"))
    assert f1.pacing_key() == f2.pacing_key()
    assert a.pacing_key() != req(contract=HistoricalContract(con_id=8, exchange="NYMEX")).pacing_key()
    assert a.pacing_key() != req(contract=a.contract, what_to_show="BID").pacing_key()
    clock = [0.0]
    p = HistoricalPacer(clock=lambda: clock[0], sleep=lambda s: None)
    assert p.admit(a.pacing_key()) is None
    clock[0] += 5
    assert p.admit(b.pacing_key()) == H.R_LOCAL_IDENTICAL


def test_latch_cannot_be_reset_through_the_public_surface():
    p = HistoricalPacer(clock=lambda: 0.0, sleep=lambda s: None)
    p.latch("broker pacing violation 162")
    with pytest.raises(AttributeError):
        p.latched = None  # type: ignore[misc]
    p.latch("another")
    assert p.latched == "broker pacing violation 162"
    assert p.admit(("x",)) == H.R_LOCAL_PACING_LATCHED
    assert not [n for n in dir(p) if not n.startswith("_") and "unlatch" in n.lower()]


# --- N7: sub-second UTC offsets ------------------------------------------------------------------
def test_end_with_a_sub_second_offset_is_refused_not_truncated():
    odd = timezone(timedelta(microseconds=500000))
    with pytest.raises(HistoricalRequestInvalid, match="whole second"):
        req(end=datetime(2026, 3, 9, 14, 15, tzinfo=odd))


# --- N8: a setup failure gives one row per request ----------------------------------------------
def test_setup_failure_produces_a_row_with_a_reason_per_request():
    base = {"end": END, "duration": "1800 S", "bar_size": "1 min", "what_to_show": "TRADES",
            "regular_hours_only": False, "contract": {"con_id": 1, "exchange": "NYMEX"}}
    plan = parse_plan({"schema": "tradingbot.ibkr.historical_bars_requests/1",
                       "request": [{"name": "a", **base}, {"name": "b", **base,
                                                            "duration": "3600 S"}]})

    def loader(_p):
        raise OSError("no config")

    rep = run_batch(plan, loader=loader)
    assert [(r["index"], r["status"], r["reason"]) for r in rep["results"]] == [
        (0, "SKIPPED", "NOT_RUN"), (1, "SKIPPED", "NOT_RUN")]
    assert rep["reason_counts"] == {"NOT_RUN": 2} and rep["overall"] == "FAILED"


# --- N9: include_expired needs sec_type FUT ------------------------------------------------------
def test_include_expired_requires_sec_type_fut():
    HistoricalContract(con_id=1, exchange="NYMEX", sec_type="FUT", include_expired=True)
    with pytest.raises(HistoricalRequestInvalid, match="FUT"):
        HistoricalContract(con_id=1, exchange="NYMEX", include_expired=True)


# --- N10: no commit hash in the order-module comment ---------------------------------------------
def test_order_module_comment_cites_no_commit_hash():
    import re

    src = (ROOT / "src/tradingbot/ibkr_orders/session.py").read_text()
    block = src[src.index("ORDER_MODE_READ_MESSAGES = ") - 400:src.index("ORDER_MODE_READ_MESSAGES = ")]
    assert not re.search(r"\b[0-9a-f]{7,40}\b", block)
