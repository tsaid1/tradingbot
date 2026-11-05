"""Generate the synthetic evaluation fixtures under fixtures/eval_synthetic/.

Every post, price, date and label here is invented. The set deliberately includes phrasings outside
the interpreter's lexicon (labeled executable) so the failure analysis has real misses to rank,
plus distractors with tickers and prices that must not become order candidates.

Run: python scripts/make_synthetic_eval_fixtures.py
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tradingbot.evaluation import GoldenReplay, GoldenStep, LabeledAction, LabeledPost
from tradingbot.models import Action, PositionSide
from tradingbot.source_events import Provenance, SourcePost
from tradingbot.tkl_interpreter import MessageClass, PriceRole, Tense

OUT = Path(__file__).resolve().parents[1] / "fixtures" / "eval_synthetic"
T0 = datetime(2026, 10, 1, 14, 30, tzinfo=timezone.utc)
L, S = PositionSide.LONG, PositionSide.SHORT
E, ST, TG, X, TR, PNL = (PriceRole.ENTRY, PriceRole.STOP, PriceRole.TARGET, PriceRole.EXIT, PriceRole.TRIGGER,
                          PriceRole.PNL_PERCENT)


def post(sid, text, hours, **kw):
    t = T0 + timedelta(hours=hours)
    base = {"source_id": sid, "post_id": sid, "author_id": "SYNTH", "verified_author": True, "text": text,
            "text_complete": True, "is_repost": False, "published_at": t, "received_at": t,
            "provenance": Provenance.SYNTHETIC}
    base.update(kw)
    return SourcePost(**base)


def act(cls, action=Action.NO_TRADE, tense=Tense.PRESENT, asset=None, side=None, prices=None, fraction=None,
        links=None, executable=False):
    return LabeledAction(message_class=cls, action=action, tense=tense, asset=asset, side=side,
                         prices=prices or {}, close_fraction=fraction, links_to=links, executable=executable)


M = MessageClass
CASES = [
    # id, hours, text, labeled actions, extra post kwargs
    ("e01", 0, "We are now long $TLT at $88.50. Stop: $86.00. Target: $95.00.",
     [act(M.OPEN, Action.OPEN, asset="TLT", side=L, prices={E: "88.50", ST: "86.00", TG: "95.00"}, executable=True)], {}),
    ("e02", 1, "We are now short the S&P 500 at 6,850 with a stop at 6,950.",
     [act(M.OPEN, Action.OPEN, asset="SPX", side=S, prices={E: "6850", ST: "6950"}, executable=True)], {}),
    ("e03", 2, "Going long $GLD at $280.00 with a stop at $270.00 and a target of $310.00.",
     [act(M.OPEN, Action.OPEN, asset="GLD", side=L, prices={E: "280.00", ST: "270.00", TG: "310.00"}, executable=True)], {}),
    ("e04", 3, "Buying $SLV here at $31.20, stop $29.50, target $36.",
     [act(M.OPEN, Action.OPEN, asset="SLV", side=L, prices={E: "31.20", ST: "29.50", TG: "36"}, executable=True)], {}),
    # out of lexicon: a past-tense entry announcement
    ("e05", 4, "Initiated a long in $IWM this morning at $220 with a stop at $212.",
     [act(M.OPEN, Action.OPEN, Tense.PAST, asset="IWM", side=L, prices={E: "220", ST: "212"}, executable=True)], {}),
    ("e06", 5, "Adding to our $TLT long at $89.20 and raising our stop to $87.00.",
     [act(M.ADD, Action.ADD, asset="TLT", side=L, prices={E: "89.20"}, links="e01", executable=True),
      act(M.MODIFY, Action.MODIFY, asset="TLT", side=L, prices={ST: "87.00"}, links="e01", executable=True)], {}),
    ("e07", 6, "Stop raised to $90 on our $TLT long.",
     [act(M.MODIFY, Action.MODIFY, asset="TLT", side=L, prices={ST: "90"}, links="e01", executable=True)], {}),
    # out of lexicon: asset between verb and object
    ("e08", 7, "Moving our $GLD stop up to $285.",
     [act(M.MODIFY, Action.MODIFY, asset="GLD", side=L, prices={ST: "285"}, links="e03", executable=True)], {}),
    ("e09", 8, "Taking profits on half of our $TLT long at $92.00.",
     [act(M.REDUCE, Action.REDUCE, asset="TLT", side=L, prices={X: "92.00"}, fraction="0.5", links="e01", executable=True)], {}),
    ("e10", 9, "Selling a third of our $GLD long at $295.",
     [act(M.REDUCE, Action.REDUCE, asset="GLD", side=L, prices={X: "295"}, fraction="0.333333", links="e03", executable=True)], {}),
    ("e30", 9.5, "Taking some profits on our $GLD long here at $299.",
     [act(M.REDUCE, Action.REDUCE, asset="GLD", side=L, prices={X: "299"}, links="e03", executable=True)], {}),
    ("e11", 10, "Closing the remaining $TLT long here at $94.10.",
     [act(M.CLOSE, Action.CLOSE, asset="TLT", side=L, prices={X: "94.10"}, links="e01", executable=True)], {}),
    ("e12", 11, "We have closed our $GLD long at $301 for a +7.5% gain.",
     [act(M.EXIT_ANNOUNCEMENT, Action.CLOSE, Tense.PAST, asset="GLD", side=L, prices={X: "301", PNL: "+7.5"}, links="e03", executable=True)], {}),
    # out of lexicon: terse exit
    ("e13", 12, "Out of $SLV here at $33.80, +8.3%.",
     [act(M.CLOSE, Action.CLOSE, asset="SLV", side=L, prices={X: "33.80", PNL: "+8.3"}, links="e04", executable=True)], {}),
    ("e14", 13, "We are now short $SPY at $685.00 with a stop at $695.00.",
     [act(M.OPEN, Action.OPEN, asset="SPY", side=S, prices={E: "685.00", ST: "695.00"}, executable=True)], {}),
    ("e15", 14, "We closed our $TLT long yesterday at $94.10 for a +6.3% gain.",
     [act(M.RETROSPECTIVE, Action.CLOSE, Tense.PAST, asset="TLT", side=L, prices={X: "94.10", PNL: "+6.3"}, links="e01")], {}),
    ("e16", 15, "We were stopped out of our $SLV long at $29.50.",
     [act(M.RETROSPECTIVE, Action.CLOSE, Tense.PAST, asset="SLV", side=L, prices={X: "29.50"}, links="e04")], {}),
    ("e17", 16, "We took profits on $GLD three times in April for an average gain of +8%.",
     [act(M.RETROSPECTIVE, Action.CLOSE, Tense.PAST, asset="GLD", prices={PNL: "+8"})], {}),
    ("e18", 17, "We are long natural gas from $2.90.",
     [act(M.RETROSPECTIVE, Action.OPEN, Tense.PAST, asset="NATGAS", side=L, prices={E: "2.90"})], {}),
    ("e19", 18, "If natural gas closes below $2.75 we will exit our long.",
     [act(M.CONDITIONAL, Action.CLOSE, Tense.FUTURE, asset="NATGAS", side=L, prices={TR: "2.75"})], {}),
    ("e20", 19, "We will add to our $TLT long on a pullback to $88.",
     [act(M.CONDITIONAL, Action.ADD, Tense.FUTURE, asset="TLT", side=L, prices={TR: "88"})], {}),
    ("e21", 20, "We are no longer looking to exit natural gas on a break below $2.75.",
     [act(M.CANCEL, asset="NATGAS")], {}),
    ("e22", 21, "We remain bullish on $GLD with a 320 target.", [act(M.COMMENTARY, asset="GLD")], {}),
    ("e23", 22, "Treasuries look attractive here; $TLT could rally toward 95.", [act(M.COMMENTARY, asset="TLT")], {}),
    ("e24", 23, "If the S&P 500 breaks 6,000 we could see 5,800.", [act(M.COMMENTARY, asset="SPX")], {}),
    ("e25", 24, "Current positions:\nShort $SPY at $685\nLong natural gas from $2.90", [act(M.POSITION_SUMMARY)], {}),
    ("e26", 25, "Position update:\n1. Short $SPY (entry $685)\nCovering our $SPY short here at $670.",
     [act(M.CLOSE, Action.CLOSE, asset="SPY", side=S, prices={X: "670"}, links="e14", executable=True), act(M.POSITION_SUMMARY)], {}),
    ("e27", 26, "We are now long $QQQ at $500. Stop…",
     [act(M.OPEN, Action.OPEN, asset="QQQ", side=L, prices={E: "500"})], {"text_complete": False}),
    ("e28", 27, "RT @someone: We are now long $QQQ at $500 with a stop at $480.",
     [act(M.OPEN, Action.OPEN, asset="QQQ", side=L, prices={E: "500", ST: "480"})], {"is_repost": True, "text_complete": False}),
    ("e29", 28, "Closing our S&P 500 short at 6,700 and going long at 6,700 with a stop at 6,600.",
     [act(M.CLOSE, Action.CLOSE, asset="SPX", side=S, prices={X: "6700"}, links="e02", executable=True),
      act(M.OPEN, Action.OPEN, asset="SPX", side=L, prices={E: "6700", ST: "6600"}, executable=True)], {}),
    # out of lexicon: idiomatic close (cascades: e05 was missed too)
    ("e31", 29, "Cutting our $IWM long loose at $214.",
     [act(M.CLOSE, Action.CLOSE, asset="IWM", side=L, prices={X: "214"}, links="e05", executable=True)], {}),
    ("e32", 30, "Natural gas has broken below $2.60 and we have exited our long at $2.58.",
     [act(M.EXIT_ANNOUNCEMENT, Action.CLOSE, Tense.PAST, asset="NATGAS", side=L, prices={X: "2.58"}, links="e18", executable=True)], {}),
    ("e33", 31, "Gold just printed $3,000 on the futures, a new high.", [act(M.COMMENTARY, asset="GOLD")], {}),
]


def tlt_golden():
    posts = [post("g1", "We are now long $TLT at $88.50. Stop: $86.00. Target: $95.00.", 0),
             post("g2", "Adding to our $TLT long at $89.20 and raising our stop to $87.00.", 24),
             post("g3", "Taking profits on half of our $TLT long at $92.00.", 120),
             post("g4", "Closing the remaining $TLT long here at $94.10.", 216),
             post("g5", "We have closed our $TLT long at $94.10 for a +6.3% gain.", 217),
             post("g6", "Current positions: none.", 240)]
    return GoldenReplay(
        name="TLT lifecycle (shape of October 2026)", policy={"quantity": 4, "quantity_per_add": 2},
        quotes={"g1": {"TLT": "88.50"}, "g2": {"TLT": "89.20"}, "g3": {"TLT": "92.00"}, "g4": {"TLT": "94.10"},
                "g5": {"TLT": "94.10"}}, posts=posts,
        steps=[GoldenStep(source_id="g1", statuses=["SIMULATED"], orders=[{"side": "BUY", "quantity": 4}]),
               GoldenStep(source_id="g2", statuses=["SIMULATED", "SIMULATED"],
                          orders=[{"side": "BUY", "quantity": 2}, {"side": "NONE", "quantity": 0}]),
               GoldenStep(source_id="g3", statuses=["SIMULATED"], orders=[{"side": "SELL", "quantity": 3}]),
               GoldenStep(source_id="g4", statuses=["SIMULATED"], orders=[{"side": "SELL", "quantity": 3}]),
               GoldenStep(source_id="g5", statuses=["RECONCILE"], classes=[M.RETROSPECTIVE]),
               GoldenStep(source_id="g6", statuses=["NO_TRADE"], classes=[M.POSITION_SUMMARY])])


def spx_golden():
    posts = [post("q1", "We are now short the S&P 500 at 6,850 with a stop at 6,950.", 0),
             post("q2", "Closing our S&P 500 short at 6,700 and going long at 6,700 with a stop at 6,600.", 48),
             post("r1", "We are now short $SPY at $685.00 with a stop at $695.00.", 72),
             post("r2", "Covering our $SPY short at $670.00 and going long $SPY at $670.00 with a stop at $660.00.", 120)]
    return GoldenReplay(
        name="S&P reversal (shape of November 2025)", policy={"quantity": 2},
        quotes={"r1": {"SPY": "685.00"}, "r2": {"SPY": "670.00"}}, posts=posts,
        steps=[GoldenStep(source_id="q1", statuses=["HOLD"], classes=[M.OPEN]),
               GoldenStep(source_id="q2", statuses=["HOLD", "HOLD"], classes=[M.CLOSE, M.OPEN]),
               GoldenStep(source_id="r1", statuses=["SIMULATED"], orders=[{"side": "SELL", "quantity": 2}]),
               GoldenStep(source_id="r2", statuses=["SIMULATED", "SIMULATED"],
                          orders=[{"side": "BUY", "quantity": 2}, {"side": "BUY", "quantity": 2}], classes=[M.CLOSE, M.OPEN])])


def partial_golden():
    posts = [post("s1", "We are now long $GLD at $280.00 with a stop at $270.00 and a target of $310.00.", 0),
             post("s2", "Taking profits on a quarter of our $GLD long at $292.00.", 72),
             post("s3", "Selling half of our remaining $GLD long at $298.00.", 120),
             post("s4", "Taking profits on a third of our $GLD long at $301.00.", 168),
             post("s5", "Closing the rest of our $GLD long at $305.00.", 240),
             post("s6", "We took profits on $GLD three times in April for an average gain of +8%.", 300)]
    return GoldenReplay(
        name="Staged partial exits (shape of April 2025)", policy={"quantity": 8, "max_notional_usd": "5000"},
        quotes={"s1": {"GLD": "280.00"}, "s2": {"GLD": "292.00"}, "s3": {"GLD": "298.00"}, "s4": {"GLD": "301.00"},
                "s5": {"GLD": "305.00"}, "s6": {"GLD": "305.00"}}, posts=posts,
        steps=[GoldenStep(source_id="s1", statuses=["SIMULATED"], orders=[{"side": "BUY", "quantity": 8}]),
               GoldenStep(source_id="s2", statuses=["SIMULATED"], orders=[{"side": "SELL", "quantity": 2}]),
               GoldenStep(source_id="s3", statuses=["SIMULATED"], orders=[{"side": "SELL", "quantity": 3}]),
               GoldenStep(source_id="s4", statuses=["HOLD"]),
               GoldenStep(source_id="s5", statuses=["SIMULATED"], orders=[{"side": "SELL", "quantity": 3}]),
               GoldenStep(source_id="s6", statuses=["RECONCILE"], classes=[M.RETROSPECTIVE])])


def natgas_golden():
    posts = [post("n1", "We are long natural gas from $2.90. If natural gas closes below $2.75 we will exit the position.", 0),
             post("n2", "We are no longer looking to exit natural gas on a break below $2.75. Stop now at $2.60.", 24),
             post("n4", "Exiting our natural gas long now at $2.80.", 48),
             post("n3", "Natural gas has broken below $2.60 and we have exited our long at $2.58.", 72)]
    return GoldenReplay(
        name="Natural-gas conditional exit (shape of October 2026)", posts=posts,
        steps=[GoldenStep(source_id="n1", statuses=["RECONCILE", "HOLD"], classes=[M.RETROSPECTIVE, M.CONDITIONAL]),
               GoldenStep(source_id="n2", statuses=["NO_TRADE", "HOLD"], classes=[M.CANCEL, M.MODIFY]),
               GoldenStep(source_id="n4", statuses=["HOLD"], classes=[M.CLOSE]),
               GoldenStep(source_id="n3", statuses=["RECONCILE"], classes=[M.RETROSPECTIVE])])


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    posts, labels = [], []
    for sid, hours, text, actions, kw in CASES:
        posts.append(post(sid, text, hours, **kw))
        labels.append(LabeledPost(source_id=sid, actions=actions))
    (OUT / "posts.jsonl").write_text("".join(p.model_dump_json() + "\n" for p in posts))
    (OUT / "labels.jsonl").write_text("".join(lbl.model_dump_json() + "\n" for lbl in labels))
    for name, spec in (("golden_tlt_lifecycle.json", tlt_golden()), ("golden_spx_reversal.json", spx_golden()),
                       ("golden_partial_exits.json", partial_golden()), ("golden_natgas_conditional.json", natgas_golden())):
        (OUT / name).write_text(json.dumps(json.loads(spec.model_dump_json()), indent=1) + "\n")
    print(f"wrote {len(posts)} posts, {sum(len(lbl.actions) for lbl in labels)} labeled actions, 4 goldens to {OUT}")


if __name__ == "__main__":
    main()
