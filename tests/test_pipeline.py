from datetime import datetime, timedelta, timezone
from decimal import Decimal

from tradingbot.interpreters import DemoInterpreter
from tradingbot.models import (
    Action, AssetClass, Instrument, MockPolicy, PositionSide,
    Quote, SignalCandidate, SourceEvent,
)
from tradingbot.pipeline import SignalPipeline
from tradingbot.storage import Journal
from tradingbot.validator import validate


def quote(symbol="ABC", bid="99.95", ask="100.05", age=0):
    return Quote(
        symbol=symbol, bid=Decimal(bid), ask=Decimal(ask),
        observed_at=datetime.now(timezone.utc) - timedelta(seconds=age),
    )


def source(sid="demo-1", text="BUY ABC @ 100 STOP 95 TARGET 110", age=0):
    return SourceEvent(
        source_id=sid, text=text,
        published_at=datetime.now(timezone.utc) - timedelta(seconds=age),
    )


def pipeline(tmp_path, quantity=1):
    store = Journal(tmp_path / "state.sqlite3")
    return SignalPipeline(store, DemoInterpreter(), MockPolicy(quantity=quantity)), store


def test_explicit_mock_open_is_recorded_atomically(tmp_path):
    p, store = pipeline(tmp_path)
    result = p.process(source(), quote())
    assert result.decision.status == "EXECUTE_MOCK"
    assert result.order.status == "SIMULATED_FILLED"
    assert result.order.idea_id == "demo-1"
    assert store.position("demo-1").quantity == 1
    assert store.recent_events()[0]["status"] == "EXECUTE_MOCK"


def test_duplicate_blocked_before_inference(tmp_path):
    class CountingInterpreter(DemoInterpreter):
        def __init__(self): self.calls = 0
        def parse(self, text, image=None):
            self.calls += 1
            return super().parse(text, image=image)

    p, store = pipeline(tmp_path)
    parser = CountingInterpreter()
    p.interpreter = parser
    assert p.process(source(), quote()).decision.status == "EXECUTE_MOCK"
    duplicate = p.process(source(), quote())
    assert duplicate.decision.status == "DUPLICATE"
    assert parser.calls == 1
    assert store.position("demo-1").quantity == 1


def test_commentary_and_conditional_outlooks_are_not_orders(tmp_path):
    p, store = pipeline(tmp_path)
    texts = [
        "We remain bullish on ABC with a 110 target and 95 stop.",
        "If ABC closes above 100, a rally to 110 could follow.",
        "I bought ABC yesterday, and it rallied."
    ]
    for i, text in enumerate(texts):
        assert p.process(source(str(i), text), quote()).decision.status == "NO_TRADE"
    assert store.positions() == []


def test_stale_message_rejected(tmp_path):
    p, store = pipeline(tmp_path)
    result = p.process(source(age=300), quote())
    assert result.decision.status == "BLOCK"
    assert "STALE_OR_FUTURE_SOURCE" in result.decision.reasons
    assert not store.positions()


def test_stale_quote_rejected(tmp_path):
    p, store = pipeline(tmp_path)
    result = p.process(source(), quote(age=200))
    assert result.decision.status == "BLOCK"
    assert "STALE_OR_FUTURE_QUOTE" in result.decision.reasons
    assert not store.positions()


def test_price_collar_and_spread(tmp_path):
    p, store = pipeline(tmp_path)
    r = p.process(source(), quote(bid="103.9", ask="104.0"))
    assert "PRICE_COLLAR_BREACH" in r.decision.reasons
    assert not store.positions()
    r2 = p.process(source("next"), quote(bid="99", ask="101"))
    assert "SPREAD_TOO_WIDE" in r2.decision.reasons


def test_wrong_symbol_and_wrong_stop(tmp_path):
    p, store = pipeline(tmp_path)
    mismatch = p.process(source(), quote(symbol="XYZ"))
    assert "QUOTE_INSTRUMENT_MISMATCH" in mismatch.decision.reasons
    wrong_stop = p.process(source("another", "BUY ABC @ 100 STOP 105 TARGET 110"), quote())
    assert "STOP_SIDE_CONTRADICTION" in wrong_stop.decision.reasons
    assert not store.positions()


def test_close_only_named_mock_idea(tmp_path):
    p, store = pipeline(tmp_path, quantity=4)
    p.process(source("entry-7"), quote())
    unknown = p.process(source("wrong", "CLOSE ABC IDEA invalid-id"), quote())
    assert unknown.decision.status == "REVIEW"
    assert store.position("entry-7").quantity == 4
    close = p.process(source("exit-8", "CLOSE ABC IDEA entry-7"), quote())
    assert close.decision.status == "EXECUTE_MOCK"
    assert close.order.side == "SELL"
    assert close.order.quantity == 4
    assert store.position("entry-7") is None


def test_partial_reduce_cannot_reverse(tmp_path):
    p, store = pipeline(tmp_path, quantity=10)
    p.process(source("entry-5"), quote())
    reduce = p.process(source("r1", "REDUCE 50% ABC IDEA entry-5"), quote())
    assert reduce.order.quantity == 5
    assert store.position("entry-5").quantity == 5
    again = p.process(source("r2", "REDUCE 50% ABC IDEA entry-5"), quote())
    assert again.decision.status == "REVIEW"
    assert "NON_INTEGER_REDUCTION_IN_MOCK" in again.decision.reasons
    assert store.position("entry-5").quantity == 5


def test_options_and_futures_schema_supported_but_not_executable():
    for cls in (AssetClass.OPTION, AssetClass.FUTURE):
        candidate = SignalCandidate(
            action=Action.OPEN,
            instrument=Instrument(asset_class=cls, symbol="ABC"),
            side=PositionSide.LONG, entry_reference="100", explicit_instruction=True,
            evidence=["Synthetic explicit instruction"],
        )
        decision = validate(source(), candidate, quote(), MockPolicy())
        assert decision.status == "REVIEW"
        assert "MOCK_EXECUTION_EQUITIES_ONLY" in decision.reasons


def test_reservation_after_crash_is_not_blindly_retried(tmp_path):
    p, store = pipeline(tmp_path)
    evt = source()
    assert store.reserve(evt) is True
    assert p.process(evt, quote()).decision.status == "DUPLICATE"
    assert not store.positions()


def test_demo_cannot_read_chart():
    candidate = DemoInterpreter().parse("BUY ABC @ 100 STOP 95 TARGET 110", image=b"not an image")
    assert candidate.action == Action.NO_TRADE
    assert "CHART_NOT_INTERPRETED" in candidate.ambiguities
