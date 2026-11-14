"""The INVENTED scenarios of the adjudication rubric (contract sections 12, 13.6 and 14): posts, the
reviewer's expected answers per form, the expected frozen values, and one contradictory variant
per scenario with the blocking code Save must refuse it with.

Pure data (no I/O). Used by ``tests/test_adjudication_rubric.py``, by the demo bundle
(``scripts/adjudication_demo_bundle.py``) and available to a scorer's self-test. Every word,
price and market is invented; the family names are those of the demo bundle.
"""

from __future__ import annotations

NS, CD, NA = "NOT_STATED", "CANNOT_DETERMINE", "NOT_APPLICABLE"
FAMILIES = ("TLT", "CRUDE", "GOLD", "NATGAS", "SPX", "SILVER")

# Context-only posts (shown as earlier posts; no form of their own).
CONTEXT_POSTS = {
    "s11c": "Closed the crude long at 74.90.",
}

_BASE = {"explicit": "YES", "quoted_series": NS, "traded_instrument": NS, "idea": "NEW_IDEA"}


def act(**kw) -> dict:
    """The answers of a form recording an action (the defaults every scenario shares)."""
    return {**_BASE, **kw}


def R(role, exactness="EXACT", month="", named="") -> dict:
    return {"role": role, "exactness": exactness, "contract_month": month, "month_named": named}


OTHER = {"role": "OTHER_ACTION_OR_MARKET"}
NOT_A_PRICE = {"role": "NOT_A_PRICE"}

CRUDE_LONG_BEFORE = {"position_state": "OPEN", "position_side": "LONG",
                     "position_last_stop": "69.5"}


def form(answers, *, expect=None, variant=None, flags=(), notes="", confidence="HIGH",
         warnings=(), kind="INSTRUCTION", rows=()) -> dict:
    return {"kind": kind, "answers": answers, "rows": [dict(r) for r in rows],
            "flags": list(flags), "notes": notes, "confidence": confidence,
            "expect": dict(expect or {}), "variant": variant, "warnings": sorted(warnings)}


def V(code, **changes) -> dict:
    return {"code": code, "changes": changes}


SCENARIOS: list[dict] = [
    {"n": 1, "post": "s01", "text": "Going long crude here around 71.20. Stop 69.50, target 75.",
     "context": [], "forms": [form(
         act(timing="CURRENT", action="OPEN", family="CRUDE", instrument_as_read="crude",
             side="LONG", position_state="FLAT", effect="OPENS", entry_price="71.2",
             entry_role="CURRENT_MARKET", entry_exactness="APPROXIMATE", stop_price="69.5",
             targets="75"),
         rows=[R("CURRENT_MARKET", "APPROXIMATE"), R("STOP"), R("TARGET")],
         expect={"post_label": "ACTIONABLE", "follower_implication": "INSTRUCTION_CANDIDATE",
                 "position_after": {"state": "OPEN", "side": "LONG", "stop": "69.5"}},
         variant=V("ACTION_EFFECT", effect="CLOSES"))]},
    {"n": 2, "post": "s02", "text": "Selling gold short at 2,410 on a limit. Stop 2,440.",
     "context": [], "forms": [form(
         act(timing="CURRENT", action="OPEN", family="GOLD", instrument_as_read="gold",
             side="SHORT", position_state="FLAT", effect="OPENS", entry_price="2410",
             entry_role="INTENDED_ENTRY", entry_exactness="EXACT", stop_price="2440",
             targets=NS),
         rows=[R("INTENDED_ENTRY"), R("STOP")], expect={"targets": NS},
         variant=V("TABLE_STOP", stop_price=NS))]},
    {"n": 3, "post": "s03", "text": "We are buying natural gas now. Stop to follow.",
     "context": [], "forms": [form(
         act(timing="CURRENT", action="OPEN", family="NATGAS", instrument_as_read="natural gas",
             side="LONG", position_state="FLAT", effect="OPENS", entry_price=NS, stop_price=NS,
             targets=NS),
         expect={"entry_role": NS, "entry_exactness": NA, "stop_price": NS, "levels": []},
         variant=V("ENTRY_FIELDS", entry_role="CURRENT_MARKET"))]},
    {"n": 4, "post": "s04",
     "text": "Bought the S&P at 5,012 a few minutes ago. Market now 5,020.",
     "context": [], "ai": {"entry_role": "INTENDED_ENTRY"},  # the invented system's mistake
     "forms": [form(
         act(timing="CURRENT", action="OPEN", family="SPX", instrument_as_read="the S&P",
             side="LONG", position_state="FLAT", effect="OPENS", entry_price="5012",
             entry_role="HISTORICAL_FILL", entry_exactness="EXACT", stop_price=NS,
             targets=NS),
         rows=[R("HISTORICAL_FILL"), R("CURRENT_MARKET")],
         expect={"post_label": "ACTIONABLE", "effect": "OPENS",
                 "follower_implication": "INSTRUCTION_CANDIDATE"},
         variant=V("TIMING_ACTION", timing="COMMENTARY"))]},
    {"n": 5, "post": "s05",
     "text": "Added to our crude long at 70.10. Average is now 70.65. Stop stays 69.50.",
     "context": ["s01"], "forms": [form(
         act(timing="CURRENT", action="ADD", family="CRUDE", instrument_as_read="crude",
             side="LONG", **CRUDE_LONG_BEFORE, effect="INCREASES", entry_price="70.1",
             entry_role="HISTORICAL_FILL", entry_exactness="EXACT", stop_price="69.5",
             targets=NS, idea="SAME_AS_PREVIOUS"),
         rows=[R("HISTORICAL_FILL"), R("AVERAGE_OR_COST_BASIS"), R("STOP")],
         expect={"effect": "INCREASES", "stop_price": "69.5"},
         variant=V("SIDE_POSITION", position_side="SHORT"))]},
    {"n": 6, "post": "s06", "text": "Lowering the stop on our gold short from 2,440 to 2,425.",
     "context": ["s02"], "forms": [form(
         act(timing="CURRENT", action="MODIFY", family="GOLD", instrument_as_read="gold",
             side="SHORT", position_state="OPEN", position_side="SHORT",
             position_last_stop="2440", amendment_field="STOP", amendment_to="2425",
             targets=NS, idea="SAME_AS_PREVIOUS"),
         rows=[R("STOP"), R("STOP")],
         expect={"effect": "AMENDS_PROTECTION", "stop_price": "2425",
                 "amendment_from": "2440"},
         variant=V("AMENDMENT", amendment_to="breakeven"))]},
    {"n": 7, "post": "s07", "text": "Stop on the S&P long moved to breakeven.",
     "context": ["s04"], "forms": [form(
         act(timing="CURRENT", action="MODIFY", family="SPX", instrument_as_read="the S&P",
             side="LONG", position_state="OPEN", position_side="LONG", position_last_stop=NS,
             amendment_field="STOP", amendment_to="5012", targets=NS,
             ambiguities=["LEVEL_BY_REFERENCE"], idea="SAME_AS_PREVIOUS"),
         expect={"stop_price": "5012", "amendment_from": NS},
         # 14.9: no STOP_NOT_IN_TABLE (the level is given by reference, not in the text).
         variant=V("CANNOT_DETERMINE_NEEDS_REASON", amendment_to=CD, ambiguities=[]))]},
    {"n": 7.5, "post": "s07b", "text": "Stop on the S&P long moved to breakeven.",
     "context": [], "forms": [form(
         act(timing="CURRENT", action="MODIFY", family="SPX", instrument_as_read="the S&P",
             side="LONG", position_state="OPEN", position_side="LONG", position_last_stop=NS,
             amendment_field="STOP", amendment_to=CD, targets=NS,
             ambiguities=["LEVEL_BY_REFERENCE"], idea="UNKNOWN"),
         flags=["MISSING_CONTEXT"], confidence="MEDIUM",
         expect={"stop_price": CD, "amendment_to": CD},
         variant=V("REQUIRED_UNANSWERED", amendment_field=""))]},
    {"n": 8, "post": "s08",
     "text": "Took half of the S&P long off at 5,080. Stop on the rest to 5,040.",
     "context": ["s04", "s07"], "forms": [form(
         act(timing="CURRENT", action="REDUCE", family="SPX", instrument_as_read="the S&P",
             side="LONG", position_state="OPEN", position_side="LONG",
             position_last_stop="5012", effect="REDUCES", stop_price="5040", targets=NS,
             idea="SAME_AS_PREVIOUS"),
         rows=[R("HISTORICAL_FILL"), R("STOP")],
         expect={"action": "REDUCE", "stop_price": "5040",
                 "position_after": {"state": "OPEN", "side": "LONG", "stop": "5040"}},
         variant=V("ACTION_EFFECT", effect="OPENS"))]},
    {"n": 9, "post": "s09", "text": "Closing the gold short here, market 2,395.",
     "context": ["s02", "s06"], "forms": [form(
         act(timing="CURRENT", action="CLOSE", family="GOLD", instrument_as_read="gold",
             side="SHORT", position_state="OPEN", position_side="SHORT",
             position_last_stop="2425", effect="CLOSES", idea="SAME_AS_PREVIOUS"),
         rows=[R("CURRENT_MARKET")],
         expect={"entry_price": NA, "stop_price": NA, "side": "SHORT",
                 "position_after": {"state": "FLAT", "side": NA, "stop": NA}},
         variant=V("NOT_APPLICABLE_FIELD", stop_price="2395"))]},
    {"n": 10, "post": "s10", "text": "We were stopped out of natural gas overnight at 2.84.",
     "context": ["s03"], "forms": [form(
         act(timing="HISTORICAL", announcement="FIRST_REPORT", action="CLOSE",
             family="NATGAS", instrument_as_read="natural gas", side="LONG",
             position_state="OPEN", position_side="LONG", position_last_stop=NS,
             effect="CLOSES", idea="SAME_AS_PREVIOUS"),
         # 14.1: the price obtained at a stop-out is HISTORICAL_FILL (it comes before STOP).
         rows=[R("HISTORICAL_FILL")],
         expect={"post_label": "RETROSPECTIVE", "announcement": "FIRST_REPORT",
                 "effect": "CLOSES", "follower_implication": "RECONCILE_EXIT",
                 "position_after": {"state": "FLAT", "side": NA, "stop": NA}},
         # 14.2: a first report changes the position; effect NONE is the form's, for a REPEAT.
         variant=V("ACTION_EFFECT", effect="NONE"))]},
    {"n": 11, "post": "s11",
     "text": "Recap: last week we closed crude at 74.90 for a gain of 5.2%.",
     "context": ["s01", "s11c"], "forms": [form(
         act(timing="HISTORICAL", announcement="REPEAT", action="CLOSE", family="CRUDE",
             instrument_as_read="crude", side="LONG", position_state="FLAT",
             idea="SAME_AS_PREVIOUS"),
         # 14.4: for a REPEAT the position before is the one after the earlier announcement
         # (s11c closed it): FLAT, not the OPEN position of before the close.
         rows=[R("HISTORICAL_FILL"), NOT_A_PRICE],
         expect={"announcement": "REPEAT", "effect": "NONE",
                 "follower_implication": "NO_NEW_INFORMATION",
                 "position_after": {"state": "FLAT", "side": NA, "stop": NA}},
         variant=V("ACTION_EFFECT", effect="CLOSES"))]},
    {"n": 12, "post": "s12",
     "text": "If silver closes above 31.50 we will go long with a stop at 30.20.",
     "context": [], "forms": [form(
         act(timing="CONDITIONAL", action="OPEN", family="SILVER", instrument_as_read="silver",
             side="LONG", position_state="FLAT", effect="OPENS", entry_price=NS,
             stop_price="30.2", targets=NS),
         rows=[R("CHART_LEVEL"), R("STOP")],
         expect={"entry_price": NS, "follower_implication": "WAIT_FOR_CONDITION",
                 "position_after": {"state": "FLAT", "side": NA, "stop": NA}},
         variant=V("TABLE_TARGET", rows={0: R("TARGET")}))]},
    {"n": 13, "post": "s13", "text": "Will buy silver at 30.80 if it pulls back.",
     "context": [], "forms": [form(
         act(timing="CONDITIONAL", action="OPEN", family="SILVER", instrument_as_read="silver",
             side="LONG", position_state="FLAT", effect="OPENS", entry_price="30.8",
             entry_role="INTENDED_ENTRY", entry_exactness="EXACT", stop_price=NS, targets=NS),
         rows=[R("INTENDED_ENTRY")],
         expect={"entry_role": "INTENDED_ENTRY", "follower_implication": "WAIT_FOR_CONDITION"},
         variant=V("TABLE_ROW", rows={0: R("INTENDED_ENTRY", "")}))]},
    {"n": 14, "post": "s14",
     "text": "Closed the crude long at 74.90. We sold gold short at 2,410, stop 2,440.",
     "context": ["s01"], "overall": "ACTIONABLE", "forms": [
         form(act(component="ACTION_RECORDED_HERE", timing="CURRENT", action="CLOSE",
                  family="CRUDE", instrument_as_read="crude", side="LONG",
                  **CRUDE_LONG_BEFORE, effect="CLOSES", idea="SAME_AS_PREVIOUS"),
              rows=[R("HISTORICAL_FILL"), OTHER, OTHER],
              expect={"action": "CLOSE"},
              # 14.4: a close from FLAT is impossible (a STOP row is no longer refused on a
              # CLOSE whose position had a stop: 14.1).
              variant=V("EFFECT_POSITION", position_state="FLAT", position_side="",
                        position_last_stop="")),
         form(act(component="ACTION_RECORDED_HERE", timing="CURRENT", action="OPEN",
                  family="GOLD", instrument_as_read="gold", side="SHORT",
                  position_state="FLAT", effect="OPENS", entry_price="2410",
                  entry_role="HISTORICAL_FILL", entry_exactness="EXACT", stop_price="2440",
                  targets=NS),
              rows=[OTHER, R("HISTORICAL_FILL"), R("STOP")],
              expect={"action": "OPEN", "additional_actions": []},
              variant=V("REQUIRED_UNANSWERED", component=""))]},
    {"n": 15, "post": "s15",
     "text": "Long December natural gas at 3.42 on a limit; January is trading at 3.61.",
     "context": [], "forms": [form(
         act(timing="CURRENT", action="OPEN", family="NATGAS",
             instrument_as_read="December natural gas", quoted_series="FUTURES",
             traded_instrument="FUTURES", side="LONG", position_state="FLAT", effect="OPENS",
             entry_price="3.42", entry_role="INTENDED_ENTRY", entry_exactness="EXACT",
             stop_price=NS, targets=NS),
         rows=[R("INTENDED_ENTRY", "EXACT", "NAMED", "DEC"),
               R("OTHER_CONTRACT_MONTH", "EXACT", "NAMED", "JAN")],
         expect={"quoted_series": "FUTURES"},
         variant=V("TABLE_ROW", rows={0: R("INTENDED_ENTRY", "EXACT", "NAMED", "")}))]},
    {"n": 16, "post": "s16", "text": "Long natural gas futures at 3.42 on a limit.",
     "context": [], "forms": [form(
         act(timing="CURRENT", action="OPEN", family="NATGAS",
             instrument_as_read="natural gas futures", quoted_series="FUTURES",
             traded_instrument="FUTURES", side="LONG", position_state="FLAT", effect="OPENS",
             entry_price="3.42", entry_role="INTENDED_ENTRY", entry_exactness="EXACT",
             stop_price=NS, targets=NS),
         rows=[R("INTENDED_ENTRY", "EXACT", NS)],
         expect={},
         variant=V("TABLE_ROW", rows={0: R("INTENDED_ENTRY", "EXACT", "")}))]},
    {"n": 17, "post": "s17",
     "text": "Bought gold at 2,395 after it held support near 2,380 with RSI at 31. Stop 2,360.",
     "context": [], "forms": [form(
         act(timing="CURRENT", action="OPEN", family="GOLD", instrument_as_read="gold",
             side="LONG", position_state="FLAT", effect="OPENS", entry_price="2395",
             entry_role="HISTORICAL_FILL", entry_exactness="EXACT", stop_price="2360",
             targets=NS),
         rows=[R("HISTORICAL_FILL"), R("CHART_LEVEL", "APPROXIMATE"), NOT_A_PRICE, R("STOP")],
         expect={},
         variant=V("TABLE_ROW", rows={2: R("NOT_A_PRICE", "EXACT")}))]},
    {"n": 18, "post": "s18", "text": "Crude bounced from 70 last week. Our stop goes to 70.",
     "context": ["s01"], "forms": [form(
         act(timing="CURRENT", action="MODIFY", family="CRUDE", instrument_as_read="crude",
             side="LONG", **CRUDE_LONG_BEFORE, amendment_field="STOP", amendment_to="70",
             targets=NS, idea="SAME_AS_PREVIOUS"),
         rows=[R("CHART_LEVEL"), R("STOP")],
         expect={"stop_price": "70", "amendment_from": "69.5"},
         variant=V("SIDE_POSITION", position_side="SHORT"))]},
    {"n": 19, "post": "s19", "text": "Stop goes below yesterday's low.",
     "context": ["s01"], "forms": [form(
         act(timing="CURRENT", action="MODIFY", family="CRUDE", instrument_as_read=NS,
             side="LONG", **CRUDE_LONG_BEFORE, amendment_field="STOP", amendment_to=CD,
             targets=NS, ambiguities=["LEVEL_BY_REFERENCE"], idea="SAME_AS_PREVIOUS"),
         confidence="MEDIUM", expect={"stop_price": CD},
         variant=V("CANNOT_DETERMINE_NEEDS_REASON", ambiguities=[]))]},
    {"n": 20, "post": "s20", "text": "Nothing left on in crude after today's move.",
     "context": ["s01"], "forms": [form(
         act(timing="HISTORICAL", announcement="FIRST_REPORT", action="CLOSE",
             explicit="NO", family="CRUDE", instrument_as_read="crude", side="LONG",
             **CRUDE_LONG_BEFORE, effect="CLOSES", idea="SAME_AS_PREVIOUS"),
         confidence="MEDIUM", expect={"explicit": "NO"},
         variant=V("REQUIRED_UNANSWERED", explicit=""))]},
    {"n": 21, "post": "s21", "text": "Sold half our silver at 32.10.",
     "context": [], "overall": "ACTIONABLE", "forms": [form(
         {"post_label": "ACTIONABLE", "additional_actions": [
             {"timing": "CURRENT", "action": "REDUCE", "family": "SILVER", "side": "LONG"}]},
         kind="LABEL_ONLY", expect={"post_label": "ACTIONABLE"},
         variant=V("ADDITIONAL_ACTION_REQUIRED", additional_actions=[]))]},
    {"n": 22, "post": "s22", "text": "Raising the gold stop to 2,380.",
     "context": ["s17"], "forms": [
         form(act(component="ACTION_RECORDED_HERE", timing="CURRENT", action="MODIFY",
                  family="GOLD", instrument_as_read="gold", side="LONG",
                  position_state="OPEN", position_side="LONG", position_last_stop="2360",
                  amendment_field="STOP", amendment_to="2380", targets=NS,
                  idea="SAME_AS_PREVIOUS"),
              rows=[R("STOP")], expect={"stop_price": "2380"},
              variant=V("AMENDMENT", amendment_to="higher")),
         form({"component": "NO_SEPARATE_ACTION"}, expect={"timing": NA, "action": NA},
              variant=V("NOT_APPLICABLE_FIELD", timing="CURRENT"))]},
    {"n": 23, "post": "s23", "text": "Bought the 5,000/5,100 call spread for 22.",
     "context": [], "forms": [form(
         {"timing": "CURRENT", "action": "OPEN", "explicit": "YES", "family": "SPX",
          "traded_instrument": "OPTIONS", "quoted_series": "OPTION"},
         flags=["FORM_CANNOT_EXPRESS"], notes="an options spread: two strikes and one premium",
         confidence="MEDIUM", expect={"escalated": True, "follower_implication":
                                      "INSTRUCTION_CANDIDATE"},
         variant=V("ESCALATION_NEEDS_NOTES", notes=""))],
     # 14.3: a post with an escalated form has no whole-post label in the frozen version.
     "overall": None},
    {"n": 24, "post": "s24",
     "text": "Last week's crude long was closed at 74.90. Today we are buying it back at the "
             "market.",
     "context": ["s01"], "overall": "ACTIONABLE", "forms": [
         form(act(component="ACTION_RECORDED_HERE", timing="HISTORICAL",
                  announcement="FIRST_REPORT", action="CLOSE", family="CRUDE",
                  instrument_as_read="crude", side="LONG", **CRUDE_LONG_BEFORE,
                  effect="CLOSES", idea="SAME_AS_PREVIOUS"),
              rows=[R("HISTORICAL_FILL")],
              expect={"post_label": "RETROSPECTIVE", "follower_implication": "RECONCILE_EXIT"},
              variant=V("DERIVED_VALUE", post_label="COMMENTARY")),
         form(act(component="ACTION_RECORDED_HERE", timing="CURRENT", action="OPEN",
                  family="CRUDE", instrument_as_read="crude", side="LONG",
                  position_state="FLAT", effect="OPENS", entry_price=NS, stop_price=NS,
                  targets=NS),
              rows=[OTHER],
              expect={"post_label": "ACTIONABLE", "follower_implication":
                      "INSTRUCTION_CANDIDATE"},
              variant=V("ACTION_EFFECT", effect="REDUCES"))]},
    {"n": 25, "post": "s25",
     "text": "Still long gold, stop unchanged at 2,360. Resistance around 2,450.",
     "context": ["s17"], "forms": [form(
         {"timing": "COMMENTARY"},
         expect={"post_label": "COMMENTARY", "action": "NONE", "effect": "NONE",
                 "follower_implication": "NONE", "stop_price": NA, "position_after": None},
         variant=V("TIMING_ACTION", action="OPEN"))]},
    {"n": 26, "post": "s26", "text": "Going long crude at 71.20.",
     "context": [], "ai": {"entry_role": "CURRENT_MARKET"}, "forms": [form(
         act(timing="CURRENT", action="OPEN", family="CRUDE", instrument_as_read="crude",
             side="LONG", position_state="FLAT", effect="OPENS", entry_price="71.2",
             entry_role="UNRESOLVED", entry_exactness="EXACT", stop_price=NS, targets=NS),
         rows=[R("UNRESOLVED")], flags=["AMBIGUOUS_PRICE_SEMANTICS"], confidence="MEDIUM",
         expect={"entry_role": "UNRESOLVED"},
         variant=V("CANNOT_DETERMINE_NEEDS_REASON", flags=[]))]},
    {"n": 27, "post": "s27", "text": "Going long crude at 71.20, the order is working.",
     "context": [], "forms": [form(
         act(timing="CURRENT", action="OPEN", family="CRUDE", instrument_as_read="crude",
             side="LONG", position_state="FLAT", effect="OPENS", entry_price="71.2",
             entry_role="INTENDED_ENTRY", entry_exactness="EXACT", stop_price=NS, targets=NS),
         rows=[R("INTENDED_ENTRY")], expect={"entry_role": "INTENDED_ENTRY"},
         variant=V("REQUIRED_UNANSWERED", entry_exactness=""))]},
    {"n": 28, "post": "s28", "text": "Going long crude at 71.20, filled on the open.",
     "context": [], "forms": [form(
         act(timing="CURRENT", action="OPEN", family="CRUDE", instrument_as_read="crude",
             side="LONG", position_state="FLAT", effect="OPENS", entry_price="71.2",
             entry_role="HISTORICAL_FILL", entry_exactness="EXACT", stop_price=NS, targets=NS),
         rows=[R("HISTORICAL_FILL")], expect={"entry_role": "HISTORICAL_FILL"},
         variant=V("ENTRY_FIELDS", entry_role=NS))]},
    {"n": 29, "post": "s29",
     "text": "Our crude long was stopped at 69.50 overnight. Buying gold now. If silver closes "
             "above 31.50 we will add it. Rates look heavy.",
     "context": ["s01"], "overall": "ACTIONABLE", "forms": [
         form(act(component="ACTION_RECORDED_HERE", timing="HISTORICAL",
                  announcement="FIRST_REPORT", action="CLOSE", family="CRUDE",
                  instrument_as_read="crude", side="LONG", **CRUDE_LONG_BEFORE,
                  effect="CLOSES", idea="SAME_AS_PREVIOUS"),
              # 14.1: "stopped at 69.50" gives the price obtained: HISTORICAL_FILL, although
              # 69.50 was also the stop s01 gave (the fill role comes before STOP).
              rows=[R("HISTORICAL_FILL"), OTHER],
              expect={"timing": "HISTORICAL", "post_label": "RETROSPECTIVE",
                      "announcement": "FIRST_REPORT", "effect": "CLOSES",
                      "follower_implication": "RECONCILE_EXIT"},
              # 14.2: a first report is never effect NONE.
              variant=V("ACTION_EFFECT", effect="NONE")),
         form(act(component="ACTION_RECORDED_HERE", timing="CURRENT", action="OPEN",
                  family="GOLD", instrument_as_read="gold", side="LONG",
                  position_state="FLAT", effect="OPENS", entry_price=NS, stop_price=NS,
                  targets=NS,
                  additional_actions=[{"timing": "CONDITIONAL", "action": "ADD",
                                       "family": "SILVER", "side": "LONG"}]),
              rows=[OTHER, OTHER],
              expect={"timing": "CURRENT", "additional_actions": [
                  {"timing": "CONDITIONAL", "action": "ADD", "family": "SILVER",
                   "side": "LONG"}]},
              variant=V("TIMING_ACTION", additional_actions=[
                  {"timing": "CONDITIONAL", "action": "CANNOT_DETERMINE", "family": "SILVER",
                   "side": "LONG"}]))]},
    {"n": 30, "post": "s30",
     "text": "Closed the crude long at 74.90 and reversed: we are now short crude from 74.85, "
             "stop 76.",
     "context": ["s01"], "overall": "ACTIONABLE", "forms": [
         form(act(component="ACTION_RECORDED_HERE", timing="CURRENT", action="CLOSE",
                  family="CRUDE", instrument_as_read="crude", side="LONG",
                  **CRUDE_LONG_BEFORE, effect="CLOSES", idea="SAME_AS_PREVIOUS"),
              rows=[R("HISTORICAL_FILL"), OTHER, OTHER],
              expect={"position_after": {"state": "FLAT", "side": NA, "stop": NA}},
              variant=V("SIDE_POSITION", position_side="SHORT")),
         form(act(component="ACTION_RECORDED_HERE", timing="CURRENT", action="OPEN",
                  family="CRUDE", instrument_as_read="crude", side="SHORT",
                  position_state="FLAT", effect="OPENS", entry_price="74.85",
                  entry_role="HISTORICAL_FILL", entry_exactness="EXACT", stop_price="76",
                  targets=NS),
              rows=[OTHER, R("HISTORICAL_FILL"), R("STOP")],
              expect={"position_after": {"state": "OPEN", "side": "SHORT", "stop": "76"},
                      "position_compared": False},
              variant=V("NOT_APPLICABLE_FIELD", position_side="SHORT"))]},
]


def item_id(scenario: dict, k: int) -> str:
    """The demo bundle's item id of form ``k`` (0-based) of a scenario."""
    return f"{scenario['post']}{'abcdefgh'[k]}"
