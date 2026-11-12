"""From a read-only contract-discovery report to an UNAPPROVED mapping proposal (offline).

Input: the JSON catalog ``scripts/ibkr_contract_discovery.py`` writes (schema
``tradingbot.ibkr.contract_catalog/1``) and a proposal policy (which report rows belong to which
family, which vehicle and treatment are proposed, and any owner inputs already decided). Output:

* a verified contract table per family (only what the report states; an absent field is shown
  as absent, a queried product the broker did not return is a NOT_FOUND row with its code);
* a proposed roll calendar per futures vehicle (``roll_date`` = the report's last-trade date minus
  ``business_days_before_last_trade`` business days, Monday to Friday less listed holidays);
* proposed ``[[mapping]]`` entries for ``config/instrument_mapping.toml``, every one
  ``approved = false``. An entry that still needs an owner input or a broker fact carries the keys
  ``owner_input_required`` / ``broker_fact_missing``, which the real loader refuses (unknown key),
  so it cannot be loaded until it is completed;
* a one-page approval sheet per family (Markdown).

No network, no ``ibapi``: the report is plain JSON. Nothing here approves anything. See
docs/MAPPING_PROPOSAL.md.
"""
from __future__ import annotations

import json
import math
import re
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

CATALOG_SCHEMA = "tradingbot.ibkr.contract_catalog/1"
POLICY_SCHEMA = "tradingbot.mapping_proposal_policy/1"
PLACEHOLDER = "OWNER_INPUT_REQUIRED"
BROKER_FACT = "BROKER_FACT_REQUIRED"  # a contract rule the report does not state
FND_FLAG = "first notice date not in report: owner/broker confirmation needed"
TREATMENTS = ("DIRECT", "BASIS_SHIFT", "REFERENCE_TRIGGER")
# Owner inputs the policy may already state; copied verbatim and checked by the real loader.
OWNER_KEYS = {"sizing", "session", "roll", "contract_selection", "basis_shift",
              "reference_trigger", "same_series_reason", "aliases",
              # the owner's whole-unit size increment where the broker allows fractions (shares)
              "size_increment"}
_FAMILY_KEYS = {"family", "treatment", "execution", "reference", "candidates", "level_meaning",
                "illustrative_price", "owner"}
_TOP_KEYS = {"schema", "roll_rule", "family"}
# Contract-details fields shown in the table, in order.
TABLE_FIELDS = ("con_id", "symbol", "local_symbol", "sec_type", "exchange", "primary_exchange",
                "currency", "multiplier", "min_tick", "size_increment", "min_size",
                "trading_class", "contract_month", "last_trade_date",
                "last_trade_date_or_contract_month", "last_trade_time", "real_expiration_date",
                "time_zone_id", "trading_hours", "liquid_hours", "stock_type", "long_name",
                "query_match")
SPEC_FIELDS = ("sec_type", "symbol", "exchange", "currency", "multiplier", "min_tick",
               "size_increment", "trading_class", "time_zone_id", "trading_hours",
               "liquid_hours", "price_magnifier")
# A TOML bare key: the only key form the writer emits, at every nesting level, so no key can
# close a table or open a new [[mapping]] block.
BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")
FAMILY_NAME = re.compile(r"^[A-Z][A-Z0-9]{0,9}(\.OPT)?$")  # as the mapping loader requires
# Keys a proposal never carries; the rendered file is re-parsed and refused if one appears.
NEVER_RENDERED = ("approved_by", "approved_at", "entry_rule", "entry_anchor",
                  "level_contract_rule")


class ProposalInputInvalid(ValueError):
    """The report or the proposal policy is malformed, or tries to approve something."""


class ProposalUnsafe(RuntimeError):
    """The rendered proposal failed its own re-parse check: nothing is written."""


def _check_keys(v: Any, where: str) -> None:
    """Bare keys at every level, and only value types the writer renders and the re-parse
    reproduces exactly: strings, booleans, integers, finite floats, dates and date-times, and
    lists and tables of them."""
    if isinstance(v, dict):
        for k, x in v.items():
            if not isinstance(k, str) or not BARE_KEY.fullmatch(k):
                raise ProposalInputInvalid(f"{where}: {k!r} is not a bare key ([A-Za-z0-9_-]+)")
            _check_keys(x, f"{where}.{k}")
    elif isinstance(v, list):
        for x in v:
            _check_keys(x, where)
    elif isinstance(v, float):
        if not math.isfinite(v):
            raise ProposalInputInvalid(f"{where}: value {v!r} is not a finite number")
    elif not isinstance(v, (str, bool, int, date)):
        raise ProposalInputInvalid(f"{where}: value of type {type(v).__name__} is not supported "
                                   "(strings, booleans, integers, finite numbers, dates, lists "
                                   "and tables only)")


def check_family_name(name: str, where: str = "family") -> str:
    if not isinstance(name, str) or not FAMILY_NAME.fullmatch(name):
        raise ProposalInputInvalid(f"{where}: {name!r} is not a valid family name "
                                   "(A-Z, 0-9, up to 10 characters, optional .OPT)")
    return name


def positive(v: Any) -> Decimal | None:
    """A positive, finite number, or None."""
    if isinstance(v, bool) or absent(v):
        return None
    try:
        d = Decimal(str(v))
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() and d > 0 else None


def absent(v: Any) -> bool:
    return v is None or v == "" or v == [] or v == {}


_LINE_BREAKERS = re.compile(r"[\x00-\x1f\x7f\x85\u2028\u2029]+")


def one_line(v: Any) -> str:
    """Report- or policy-derived text for a comment, header or free text: one line, no control
    characters (each run becomes a single space)."""
    return _LINE_BREAKERS.sub(" ", str(v))


def show(v: Any) -> str:
    return "absent" if absent(v) else one_line(v)


# ============================================================================ inputs


@dataclass
class FamilyPlan:
    family: str
    treatment: str
    execution: str  # report query name
    reference: str | None = None  # report query name; None = the execution series itself
    candidates: list[str] = field(default_factory=list)
    level_meaning: str = ""
    illustrative_price: Decimal | None = None
    owner: dict = field(default_factory=dict)
    source: str = "policy file"


@dataclass
class ProposalPolicy:
    families: list[FamilyPlan]
    roll_days: int | None = None
    holidays: list[date] = field(default_factory=list)


def parse_policy(data: dict) -> ProposalPolicy:
    unknown = set(data) - _TOP_KEYS
    if unknown:
        raise ProposalInputInvalid(f"unknown top-level keys {sorted(unknown)}")
    if data.get("schema") != POLICY_SCHEMA:
        raise ProposalInputInvalid(f"schema must be {POLICY_SCHEMA!r}")
    rule = data.get("roll_rule", {})
    if set(rule) - {"business_days_before_last_trade", "holidays"}:
        raise ProposalInputInvalid("roll_rule takes business_days_before_last_trade and holidays")
    days = rule.get("business_days_before_last_trade")
    if days is not None and (isinstance(days, bool) or not isinstance(days, int) or days < 0):
        raise ProposalInputInvalid("business_days_before_last_trade must be an int >= 0")
    holidays = rule.get("holidays", [])
    if not all(isinstance(h, date) for h in holidays):
        raise ProposalInputInvalid("roll_rule.holidays must be TOML dates")
    fams = [parse_family(f, i) for i, f in enumerate(data.get("family", []))]
    return ProposalPolicy(fams, days, list(holidays))


def parse_family(f: dict, i: int | str = 0, source: str = "policy file") -> FamilyPlan:
    where = f"family[{i}]"
    unknown = set(f) - _FAMILY_KEYS
    if unknown:
        raise ProposalInputInvalid(f"{where}: unknown keys {sorted(unknown)} (a proposal never "
                                   "carries an approval)")
    for k in ("family", "treatment", "execution"):
        if not isinstance(f.get(k), str) or not f[k].strip():
            raise ProposalInputInvalid(f"{where}: {k} is required")
    check_family_name(f["family"], where)
    if f["treatment"] not in TREATMENTS:
        raise ProposalInputInvalid(f"{where}: treatment must be one of {TREATMENTS}")
    owner = f.get("owner", {})
    if not isinstance(owner, dict) or set(owner) - OWNER_KEYS:
        raise ProposalInputInvalid(f"{where}: owner takes only {sorted(OWNER_KEYS)}")
    _check_keys(owner, f"{where}.owner")
    inc = owner.get("size_increment")
    if inc is not None and (isinstance(inc, bool) or not isinstance(inc, int) or inc < 1):
        raise ProposalInputInvalid(f"{where}: owner size_increment is the whole-unit increment "
                                   "the owner states (an int >= 1)")
    price = f.get("illustrative_price")
    if price is not None:
        try:
            price = Decimal(str(price))
        except InvalidOperation:
            raise ProposalInputInvalid(f"{where}: illustrative_price is not a number") from None
        if price <= 0:
            raise ProposalInputInvalid(f"{where}: illustrative_price must be positive")
    return FamilyPlan(f["family"], f["treatment"], f["execution"], f.get("reference"),
                      list(f.get("candidates", [])), f.get("level_meaning", ""), price, owner,
                      source)


def parse_family_spec(spec: str) -> FamilyPlan:
    """``FAMILY:TREATMENT:EXECUTION_QUERY[:REFERENCE_QUERY]`` from the command line."""
    parts = spec.split(":")
    if len(parts) not in (3, 4):
        raise ProposalInputInvalid(
            f"--family {spec!r}: expected FAMILY:TREATMENT:EXECUTION_QUERY[:REFERENCE_QUERY]")
    d = {"family": parts[0], "treatment": parts[1], "execution": parts[2]}
    if len(parts) == 4:
        d["reference"] = parts[3]
    return parse_family(d, parts[0], source="command line")


def load_policy(path: str | Path) -> ProposalPolicy:
    try:
        return parse_policy(tomllib.loads(Path(path).read_text(encoding="utf-8")))
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise ProposalInputInvalid(f"cannot read proposal policy {path}: {exc}") from exc


def load_report(path: str | Path) -> dict:
    try:
        cat = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ProposalInputInvalid(f"cannot read discovery report {path}: {exc}") from exc
    if not isinstance(cat, dict) or cat.get("schema") != CATALOG_SCHEMA:
        raise ProposalInputInvalid(f"not a discovery report (schema must be {CATALOG_SCHEMA!r})")
    if not isinstance(cat.get("results"), list):
        raise ProposalInputInvalid("discovery report has no results list")
    names = [r.get("name") for r in cat["results"] if isinstance(r, dict)]
    twice = sorted({n for n in names if names.count(n) > 1}, key=str)
    if twice or len(names) != len(cat["results"]):
        raise ProposalInputInvalid(f"discovery report lists query name(s) {twice} twice "
                                   "(or a result that is not an object): refused, not first-wins")
    return cat


def report_date(cat: dict) -> date | None:
    try:
        return date.fromisoformat(str(cat.get("started_at", ""))[:10])
    except ValueError:
        return None


# ============================================================================ facts


@dataclass
class Vehicle:
    """One report row (one query) as the proposal sees it."""
    query: str
    status: str  # FOUND / NOT_FOUND / ERROR / TIMEOUT / SKIPPED / ... / NOT_IN_REPORT
    kind: str = "contracts"
    contracts: list[dict] = field(default_factory=list)
    params: list[dict] = field(default_factory=list)  # option-chain rows
    error_code: Any = None
    error_message: str | None = None
    truncated: bool = False
    limit: Any = None
    spec: dict = field(default_factory=dict)  # field -> single value stated by every contract
    spec_problems: dict = field(default_factory=dict)  # field -> why there is no single value

    @property
    def found(self) -> bool:
        return self.status == "FOUND" and bool(self.contracts or self.params)

    @property
    def is_future(self) -> bool:
        return self.spec.get("sec_type") == "FUT"


def vehicle(cat: dict, name: str) -> Vehicle:
    row = next((r for r in cat["results"] if r.get("name") == name), None)
    if row is None:
        return Vehicle(name, "NOT_IN_REPORT", error_message="no such query in the report")
    v = Vehicle(name, str(row.get("status")), row.get("kind", "contracts"),
                list(row.get("contracts") or []), list(row.get("params") or []),
                row.get("error_code"), row.get("error_message"), bool(row.get("truncated")),
                row.get("limit"))
    if v.kind == "option_chain":
        und = (row.get("underlying") or {}).get("contracts") or []
        mults = {p.get("multiplier") for p in v.params}
        v.spec = {"sec_type": "OPT", "symbol": und[0].get("symbol") if und else None,
                  "currency": und[0].get("currency") if und else None}
        if len(mults) == 1 and not absent(next(iter(mults))):
            v.spec["multiplier"] = next(iter(mults))
        else:
            v.spec_problems["multiplier"] = (f"{len(mults)} values in the chain" if mults
                                             else "absent")
        v.spec_problems["min_tick"] = "not in an option-chain report"
        v.spec_problems["exchange"] = (f"chain lists {len({p.get('exchange') for p in v.params})}"
                                       " exchanges")
        return v
    for f in SPEC_FIELDS:
        vals = [c.get(f) for c in v.contracts]
        distinct = {json.dumps(x) for x in vals if not absent(x)}
        if v.contracts and len(distinct) == 1 and not any(absent(x) for x in vals):
            v.spec[f] = vals[0]
        elif v.contracts:
            v.spec_problems[f] = ("absent" if not distinct else
                                  f"{len(distinct)} different values across contracts")
    return v


def last_trade(c: dict) -> tuple[date | None, str]:
    """The last-trade date the report states, and which field it came from."""
    for f in ("last_trade_date", "last_trade_date_or_contract_month"):
        s = str(c.get(f) or "")
        if len(s) >= 8 and s[:8].isdigit():
            try:
                return date(int(s[:4]), int(s[4:6]), int(s[6:8])), f
            except ValueError:
                continue
    return None, ""


def is_business_day(d: date, holidays: list[date]) -> bool:
    return d.weekday() < 5 and d not in holidays


def business_days_before(d: date, n: int, holidays: list[date]) -> date:
    """``n`` business days before ``d``. With ``n = 0`` a last-trade date on a weekend or listed
    holiday moves back to the business day before it."""
    if isinstance(n, bool) or not isinstance(n, int) or n < 0:
        raise ValueError("the number of business days must be an int >= 0")
    out, left = d, n
    if left == 0:
        while not is_business_day(out, holidays):
            out -= timedelta(days=1)
        return out
    while left:
        out -= timedelta(days=1)
        if is_business_day(out, holidays):
            left -= 1
    return out


def business_days_between(a: date, b: date, holidays: list[date]) -> int:
    """Business days d with a < d <= b (negative when b is before a)."""
    if b < a:
        return -business_days_between(b, a, holidays)
    return sum(1 for i in range(1, (b - a).days + 1)
               if is_business_day(a + timedelta(days=i), holidays))


def roll_calendar(v: Vehicle, roll_days: int | None, holidays: list[date],
                  as_of: date | None, min_lead: int | None = None) -> list[dict]:
    rows = []
    for c in sorted(v.contracts, key=lambda c: (str(c.get("contract_month") or ""),
                                                 str(c.get("last_trade_date") or ""))):
        ltd, src = last_trade(c)
        flags = [FND_FLAG]
        month = str(c.get("contract_month") or "")
        if not (len(month) == 6 and month.isdigit()):
            flags.append("contract month not in report: month omitted from the proposal")
            month = ""
        roll, usable = None, True
        if ltd is None:
            flags.append("last-trade date not in report: no roll date proposed")
        elif roll_days is None:
            flags.append("no roll rule parameter set: roll date is an owner input")
        else:
            roll = business_days_before(ltd, roll_days, holidays)
            if roll_days == 0 and roll != ltd:
                flags.append("last-trade date is not a business day: moved back to the one before")
            if as_of is None:
                flags.append("report date unknown: roll date not checked against it")
            elif roll <= as_of:
                usable = False
                flags.append("UNUSABLE: proposed roll date already reached on the report date "
                             "(roll_date <= report date); month left out of the proposal")
            elif min_lead is not None:
                lead = business_days_between(as_of, roll, holidays)
                if lead < min_lead:
                    flags.append(f"inside the minimum lead time: {lead} business days from the "
                                 f"report date to the roll date, fewer than "
                                 f"min_business_days_to_roll {min_lead}")
        rows.append({"con_id": c.get("con_id"), "local_symbol": c.get("local_symbol"),
                     "contract_month": month or None, "last_trade_date": ltd,
                     "last_trade_source": src or None, "roll_date": roll, "usable": usable,
                     "flags": flags})
    return rows


# ============================================================================ the proposal


@dataclass
class FamilyProposal:
    plan: FamilyPlan
    execution: Vehicle
    reference: Vehicle
    candidates: list[Vehicle]
    entry: dict
    calendar: list[dict]
    owner_inputs: list[str]
    broker_missing: list[str]
    unsupported_reason: str | None = None
    warnings: list[str] = field(default_factory=list)
    same_series_diffs: list[dict] = field(default_factory=list)
    holidays_applied: bool = False


def _placeholder_table(keys: list[str], fixed: dict | None = None) -> dict:
    out = {k: PLACEHOLDER for k in keys}
    out.update(fixed or {})
    return out


def _series(v: Vehicle, missing: list[str], where: str, *, execution: bool,
            owner_in: list[str] | None = None, owner_increment: int | None = None) -> dict:
    s = v.spec
    out: dict[str, Any] = {}
    for k in ("sec_type", "symbol", "exchange", "currency"):
        if k in s:
            out[k] = s[k]
        elif not (k == "exchange" and v.kind == "option_chain"):  # an option's is an owner input
            out[k] = BROKER_FACT
            missing.append(f"{where}.{k}: {v.spec_problems.get(k, 'absent')}")
    if s.get("sec_type") == "STK":
        stock_type = {str(c.get("stock_type") or "") for c in v.contracts}
        cls = {"ETF": "ETF", "COMMON": "EQUITY"}.get(next(iter(stock_type)), None) \
            if len(stock_type) == 1 else None
        if cls is None:
            out["asset_class"] = BROKER_FACT
            missing.append(f"{where}.asset_class: report stock_type {sorted(stock_type)}")
        else:
            out["asset_class"] = cls
    if not execution:
        return out
    # A share left without a multiplier keeps the loader's stock default (1 share); every other
    # rule the report does not state, or states as zero, negative or non-numeric, is a missing
    # broker fact.
    stock = s.get("sec_type") == "STK"
    for k in ("multiplier", "min_tick"):
        value = positive(s.get(k))
        if value is not None:
            out[k] = format(value.normalize(), "f")
        elif k in s:
            out[k] = BROKER_FACT
            missing.append(f"{where}.{k}: report states {s[k]!r}: not a positive number")
        elif not (stock and k == "multiplier"):
            out[k] = BROKER_FACT
            missing.append(f"{where}.{k}: {v.spec_problems.get(k, 'absent')}")
    if v.kind == "option_chain":
        return out
    mag = s.get("price_magnifier")
    if mag is None or positive(mag) != 1:
        why = (v.spec_problems.get("price_magnifier", "absent") if mag is None
               else f"report states {mag}")
        missing.append(f"{where}.price_magnifier: {why}: prices may not be in the contract's own "
                       "units, so no dollar values are computed (owner/broker confirmation)")
    inc = positive(s.get("size_increment"))
    if inc is not None and inc >= 1 and inc == inc.to_integral_value():
        out["size_increment"] = int(inc)
    elif stock and inc is not None and inc < 1 and owner_increment is not None:
        out["size_increment"] = owner_increment  # the owner stated whole units only
    elif stock and inc is not None and inc < 1 and owner_in is not None:
        out["size_increment"] = PLACEHOLDER
        owner_in.append(f"{where}.size_increment: the broker allows fractional shares (size "
                        f"increment {_num(inc)}); the loader needs whole units: owner decision")
    else:
        out["size_increment"] = BROKER_FACT
        why = v.spec_problems.get("size_increment") or \
            f"report states {show(s.get('size_increment'))}"
        missing.append(f"{where}.size_increment: {why} (whole units only)")
    if owner_increment is not None and out.get("size_increment") != owner_increment:
        missing.append(f"{where}.size_increment: the owner's whole-unit increment applies only "
                       "where the broker allows fractional shares")
    return out


def report_warnings(cat: dict, x: Vehicle, ref: Vehicle,
                    span: Callable[[Any], str] | None = None) -> list[str]:
    """The report's warnings. ``span`` formats a name shown in a code span: by default the plain
    Markdown form (the TOML note keeps exactly that); the sheets pass ``_code``."""
    span = span or _md_plain
    out = []
    if cat.get("overall") != "COMPLETE":
        out.append(f"the discovery report is {_md_plain(cat.get('overall'))} "
                   f"({_md_plain(cat.get('failure'))}): "
                   "re-run discovery before relying on it")
    if x.truncated:
        out.append(f"execution row `{span(x.query)}` is TRUNCATED: the month list may be "
                   f"incomplete (the report kept {len(x.contracts)} rows at limit {show(x.limit)})")
    if ref is not x and ref.truncated:
        out.append(f"reference row `{span(ref.query)}` is TRUNCATED: the broker returned "
                   "more rows than "
                   f"the report kept ({len(ref.contracts)} at limit {show(ref.limit)})")
    return out


def same_series_diffs(x: Vehicle, ref: Vehicle, calendar: list[dict],
                      holidays: list[date]) -> list[dict]:
    """Month by month: the execution vehicle's last-trade date against the reference's. The report
    shows listing facts only; it never shows that the two trade at the same price."""
    by_month: dict[str, list[date | None]] = {}
    for c in ref.contracts:
        by_month.setdefault(str(c.get("contract_month") or ""), []).append(last_trade(c)[0])
    out = []
    for row in calendar:
        m = row["contract_month"]
        if m is None:
            continue
        refs = by_month.get(m, [])
        ref_ltd = refs[0] if len(refs) == 1 else None
        xl = row["last_trade_date"]
        n = business_days_between(xl, ref_ltd, holidays) if xl and ref_ltd else None
        if not refs:
            why = "not in the reference row" + (" (row truncated)" if ref.truncated else "")
        elif ref_ltd is None:
            why = "reference last-trade date not in report"
        elif xl is None:
            why = "execution last-trade date not in report"
        elif n == 0:
            why = "same last-trade date as the reference"
        else:
            unit = "business day" if abs(n) == 1 else "business days"
            why = (f"execution last trade {abs(n)} {unit} "
                   f"{'before' if n > 0 else 'after'} the reference's ({ref_ltd})")
        out.append({"month": m, "execution_last_trade": xl, "reference_last_trade": ref_ltd,
                    "business_days": n, "text": why})
        if why != "same last-trade date as the reference":
            row["flags"].append(why)
    return out


def build_family(plan: FamilyPlan, cat: dict, policy: ProposalPolicy) -> FamilyProposal:
    x = vehicle(cat, plan.execution)
    ref = vehicle(cat, plan.reference) if plan.reference else x
    names = list(dict.fromkeys([*plan.candidates, plan.execution, *(
        [plan.reference] if plan.reference else [])]))
    cands = [vehicle(cat, n) for n in names]
    as_of = report_date(cat)
    sel = plan.owner.get("contract_selection")
    lead = sel.get("min_business_days_to_roll") if isinstance(sel, dict) else None
    lead = lead if isinstance(lead, int) and not isinstance(lead, bool) else None
    calendar = roll_calendar(x, policy.roll_days, policy.holidays, as_of, lead) \
        if x.is_future else []
    owner_in: list[str] = []
    missing: list[str] = []
    warnings = report_warnings(cat, x, ref)
    warn_note = (" WARNINGS: " + "; ".join(warnings) + ".") if warnings else ""
    shown = report_warnings(cat, x, ref, span=_code)  # the sheets' form of the same warnings
    extra = {"warnings": shown, "holidays_applied": bool(policy.holidays)}

    def unsupported(reason: str) -> FamilyProposal:
        entry = {"family": plan.family, "basis": "UNSUPPORTED", "approved": False,
                 "note": f"PROPOSAL (not approved): {reason}. Unresolved mappings HOLD."
                         + warn_note}
        return FamilyProposal(plan, x, ref, cands, entry, calendar, [], [], reason, **extra)

    for role, v in (("execution", x), ("reference", ref)):
        if not v.found:
            code = "" if v.error_code is None else f" (code {v.error_code})"
            return unsupported(f"{role} query {v.query!r} is {v.status}{code}")
        if v.kind == "contracts" and v.spec.get("sec_type") != "FUT" and len(v.contracts) != 1:
            return unsupported(f"{role} query {v.query!r} returned {len(v.contracts)} contracts, "
                               "not exactly one")
        months = [str(c.get("contract_month")) for c in v.contracts if c.get("contract_month")]
        twice = sorted({m for m in months if months.count(m) > 1})
        if twice:
            return unsupported(f"{role} query {v.query!r} has month(s) {', '.join(twice)} "
                               "listed twice: refused, not first-wins")
    if ref is not x and ref.kind == "option_chain":
        return unsupported("an option chain cannot be a reference series")
    opt = x.kind == "option_chain"
    if opt and plan.treatment != "DIRECT":
        return unsupported("an option is executed only on its own premium (DIRECT)")
    if opt and not plan.family.endswith(".OPT"):
        return unsupported("an option family is named <UNDERLYING>.OPT")

    o = plan.owner
    entry: dict[str, Any] = {"family": plan.family, "basis": plan.treatment, "approved": False}
    entry["note"] = (f"PROPOSAL (not approved) from discovery report {cat.get('started_at')}: "
                     f"execution {x.query!r}, reference {ref.query!r}, treatment from the "
                     f"{plan.source}.")
    reference = _series(ref, missing, "reference", execution=False)
    execution = _series(x, missing, "execution", execution=True, owner_in=owner_in,
                        owner_increment=o.get("size_increment"))
    if opt:
        reference = {"sec_type": "OPT", "symbol": execution["symbol"],
                     "exchange": PLACEHOLDER, "currency": execution["currency"]}
        execution["exchange"] = PLACEHOLDER
        owner_in.append("execution.exchange / reference.exchange: choose one of the chain's "
                        "exchanges")
    same_series_needed = plan.treatment == "DIRECT" and plan.reference not in (
        None, plan.execution) and not opt
    if same_series_needed:
        entry["same_series"] = True
        entry["same_series_reason"] = o.get("same_series_reason", PLACEHOLDER)
        if "same_series_reason" not in o:
            owner_in.append("same_series_reason")
        if reference.get("symbol") != execution.get("symbol"):
            entry["equivalent_roots"] = [reference.get("symbol"), execution.get("symbol")]
    entry["reference"] = reference
    entry["execution"] = execution

    def owner_table(key: str, keys: list[str], fixed: dict | None = None):
        if key in o:
            entry[key] = o[key]
        else:
            entry[key] = _placeholder_table(keys, fixed)
            owner_in.append(key)

    if x.is_future:
        owner_table("contract_selection", ["rule", "min_business_days_to_roll"],
                    {"rule": "EXPLICIT_ONLY"} if same_series_needed else None)
        contracts, past = [], []
        for row in calendar:
            if row["contract_month"] is None:
                continue
            if not row["usable"]:
                past.append(row["contract_month"])
                continue
            rd = row["roll_date"]
            contracts.append({"month": row["contract_month"],
                              "roll_date": rd if rd is not None else PLACEHOLDER})
            if rd is None and "contracts.roll_date" not in owner_in:
                owner_in.append("contracts.roll_date")
        if not contracts:
            missing.append("contracts: no usable listed month (contract month or roll date)")
        if past:
            entry["note"] += (" Months left out, roll date already reached on the report date: "
                              f"{', '.join(past)}.")
        entry["contracts"] = contracts
    legacy = (plan.treatment == "DIRECT" and execution.get("sec_type") == "STK"
              and plan.family == execution.get("symbol") and "sizing" not in o
              and "session" not in o)
    if not legacy:
        owner_table("sizing", ["units_per_signal", "max_units", "max_notional"])
        owner_table("session", ["policy", "timezone", "days", "regular"])
    if x.is_future:
        roll = dict(o.get("roll") or {})
        if "business_days_before" not in roll:
            roll["business_days_before"] = PLACEHOLDER
            owner_in.append("roll.business_days_before")
        roll.setdefault("action", "FLATTEN")
        if policy.holidays and "holidays" not in roll:
            roll["holidays"] = list(policy.holidays)
        entry["roll"] = roll
    if plan.treatment == "BASIS_SHIFT":
        owner_table("basis_shift", ["max_abs_basis", "max_age_seconds", "scale_ratio"])
    if plan.treatment == "REFERENCE_TRIGGER":
        owner_table("reference_trigger", ["reference_hours", "require_backstop"],
                    {"released_order_type": "MKT"})
        rt = o.get("reference_trigger") or {}
        if "basis_shift" in o:
            entry["basis_shift"] = o["basis_shift"]
        elif rt.get("require_backstop") is not False:
            entry["basis_shift"] = _placeholder_table(
                ["max_abs_basis", "max_age_seconds", "scale_ratio"])
            owner_in.append("basis_shift (prices the backstop; drop it with "
                            "require_backstop = false)")
    if "aliases" in o:
        entry["aliases"] = o["aliases"]
    if owner_in:
        entry["owner_input_required"] = list(owner_in)
    if missing:
        entry["broker_fact_missing"] = list(missing)
    diffs = same_series_diffs(x, ref, calendar, policy.holidays) if same_series_needed else []
    entry["note"] += warn_note
    return FamilyProposal(plan, x, ref, cands, entry, calendar, owner_in, missing,
                          same_series_diffs=diffs, **extra)


def build(cat: dict, policy: ProposalPolicy) -> list[FamilyProposal]:
    seen: set[str] = set()
    out = []
    for plan in policy.families:
        if plan.family in seen:
            raise ProposalInputInvalid(f"family {plan.family} is proposed twice")
        seen.add(plan.family)
        out.append(build_family(plan, cat, policy))
    return out


def merge_cli(policy: ProposalPolicy, specs: list[str], roll_days: int | None,
              prices: dict[str, Decimal]) -> ProposalPolicy:
    fams = {f.family: f for f in policy.families}
    for s in specs:
        p = parse_family_spec(s)
        old = fams.get(p.family)
        if old is not None:  # the command line names the vehicle and treatment; keep the rest
            p.candidates, p.level_meaning = old.candidates, old.level_meaning
            p.illustrative_price = old.illustrative_price
            # owner inputs decided for another vehicle or treatment are not carried over
            p.owner = old.owner if (old.treatment, old.execution, old.reference) == (
                p.treatment, p.execution, p.reference) else {}
        fams[p.family] = p
    for fam, price in prices.items():
        if fam not in fams:
            raise ProposalInputInvalid(f"--price {fam}: no such family in the proposal")
        fams[fam].illustrative_price = price
    return ProposalPolicy(list(fams.values()),
                          roll_days if roll_days is not None else policy.roll_days,
                          policy.holidays)


# ============================================================================ rendering


def _tstr(text: str) -> str:
    """A TOML basic string: quote, backslash and every control character (and DEL) escaped."""
    out = []
    for ch in text:
        if ch in '"\\':
            out.append("\\" + ch)
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\u{ord(ch):04X}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def _toml(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, float):
        if not math.isfinite(v):
            raise ProposalInputInvalid(f"value {v!r} is not a finite number: not written")
        return repr(v)
    if isinstance(v, (str, Decimal)):
        return _tstr(str(v))
    if isinstance(v, list):
        return "[" + ", ".join(_toml(x) for x in v) + "]"
    if isinstance(v, dict):
        for k in v:
            if not isinstance(k, str) or not BARE_KEY.fullmatch(k):
                raise ProposalInputInvalid(f"{k!r} is not a bare key: not written")
        return "{ " + ", ".join(f"{k} = {_toml(x)}" for k, x in v.items()) + " }"
    raise TypeError(f"cannot write {type(v).__name__} to TOML")


def render_toml(props: list[FamilyProposal], cat: dict) -> str:
    lines = [
        "# PROPOSED mapping entries (tradingbot.runtime.mapping_proposal). NOT APPROVED.",
        (f"# Built from discovery report started_at={one_line(cat.get('started_at'))} "
        f"overall={one_line(cat.get('overall'))}; every entry is approved = false."),
        "# Entries carrying owner_input_required / broker_fact_missing do not load: replace each",
        f"# {PLACEHOLDER} with the owner's decision and {BROKER_FACT} with the broker's",
        "# fact (a new discovery query), delete those two keys, review with",
        "# docs/MULTI_ASSET_MAPPING.md, and only then copy an entry into",
        "# config/instrument_mapping.toml. Approval is the owner's alone.",
        "", "version = 2", ""]
    for p in props:
        lines.append("[[mapping]]")
        for k, v in p.entry.items():
            if not BARE_KEY.fullmatch(k):
                raise ProposalInputInvalid(f"{k!r} is not a bare key: not written")
            lines.append(f"{k} = {_toml(v)}")
        lines.append("")
    return "\n".join(lines)


def _plain(v: Any) -> Any:
    """A proposed value as the TOML parser returns it (the writer renders a Decimal as a
    string)."""
    if isinstance(v, dict):
        return {k: _plain(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_plain(x) for x in v]
    if isinstance(v, Decimal):
        return str(v)
    return v


def expected_document(props: list[FamilyProposal]) -> dict:
    return {"version": 2, "mapping": [_plain(p.entry) for p in props]}


def verify_rendered(text: str, props: list[FamilyProposal]) -> list[dict]:
    """Re-parse the exact text to be written. It must EQUAL the proposal structurally (exactly
    the proposed families, in order, each entry key for key and value for value, nothing else
    anywhere), and, as a second, independent check, every entry must be ``approved = false``
    with no approval or owner-rule key. Raises ``ProposalUnsafe``."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ProposalUnsafe(f"rendered proposal does not parse: {exc}") from None
    expected = expected_document(props)
    if data != expected:
        got = [e.get("family") for e in data.get("mapping", [])] if isinstance(
            data.get("mapping"), list) else None
        raise ProposalUnsafe("rendered proposal differs from the proposal (top-level keys "
                             f"{sorted(data)}, families {got})")
    entries = data["mapping"]
    if [e.get("family") for e in entries] != [p.plan.family for p in props]:
        raise ProposalUnsafe("rendered proposal does not hold exactly the proposed families")
    for e in entries:
        if e.get("approved") is not False:
            raise ProposalUnsafe(f"family {e.get('family')}: rendered entry is not "
                                 "approved = false")
        bad = [k for k in NEVER_RENDERED if k in e]
        if bad:
            raise ProposalUnsafe(f"family {e.get('family')}: rendered entry carries {bad}")
    return entries


def _md_plain(v: Any) -> str:
    """Report- or policy-derived text inside Markdown: one line, pipes and backticks escaped, and
    a leading # or > escaped, so it cannot forge a heading, a quote, a code span or a table row.
    No HTML escaping: this form also goes into the TOML notes (warnings)."""
    text = show(v).replace("\\", "\\\\").replace("|", "\\|").replace("`", "'")
    return "\\" + text if text[:1] in ("#", ">") else text


def _html(text: str) -> str:
    """``&`` and ``<`` as entities, so text in a sheet viewed as HTML or Markdown cannot open a
    tag or an entity (a ``>`` alone opens nothing)."""
    return text.replace("&", "&amp;").replace("<", "&lt;")


def _md(v: Any) -> str:
    """Report- or policy-derived text written into a Markdown sheet (``_md_plain``, then
    ``_html``)."""
    return _html(_md_plain(v))


def _code(v: Any) -> str:
    """Report- or policy-derived text inside a code span: written literally (a renderer escapes
    code-span content, and an entity there would show as typed), on one line, with every
    back-tick replaced, so it can neither end its span nor open a new line. No code span is in
    a table row, so a pipe there opens no cell."""
    return show(v).replace("`", "'")


def _html_outside_code(text: str) -> str:
    """``_html`` applied outside code spans only (the text's back-ticks are its own spans)."""
    parts = text.split("`")
    return "`".join(_html(t) if i % 2 == 0 else t for i, t in enumerate(parts))


def render_contract_table(props: list[FamilyProposal], cat: dict) -> str:
    out = [(f"# Verified contract table (report {_md(cat.get('started_at'))}, "
           f"overall {_md(cat.get('overall'))})"), "",
           ("Every value below is stated by the discovery report; `absent` means the report does "
           "not carry it. Nothing is inferred."), ""]
    if cat.get("overall") != "COMPLETE":
        out += [(f"**WARNING: the report is {_md(cat.get('overall'))}** "
                f"({_md(cat.get('failure'))}). Re-run discovery before relying on it."), ""]
    for p in props:
        out += [f"## {p.plan.family}", ""]
        for v in p.candidates:
            role = ("execution" if v.query == p.execution.query else
                    "reference" if v.query == p.reference.query else "candidate")
            out.append(f"### {_md(v.query)} ({role})")
            out.append("")
            if not v.found:
                code = "absent" if v.error_code is None else str(v.error_code)
                label = "NOT_FOUND row" if v.status == "NOT_FOUND" else "unresolved row"
                out += [(f"{label}: status `{_code(v.status)}`, broker code {_md(code)}, message "
                        f"{_md(v.error_message)}."), ""]
                continue
            if v.truncated:
                out += ["**TRUNCATED**: the report kept only part of the broker's rows.", ""]
            if v.kind == "option_chain":
                out += ["| exchange | trading class | multiplier | expirations | strikes |",
                        "|---|---|---|---|---|"]
                for q in v.params:
                    out.append(f"| {_md(q.get('exchange'))} | {_md(q.get('trading_class'))} | "
                               f"{_md(q.get('multiplier'))} | {len(q.get('expirations') or [])} | "
                               f"{len(q.get('strikes') or [])} |")
                out.append("")
                continue
            cols = [f for f in TABLE_FIELDS if f not in ("trading_hours", "liquid_hours")]
            out += ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
            for c in v.contracts:
                out.append("| " + " | ".join(_md(c.get(f)) for f in cols) + " |")
            out.append("")
            for f in ("trading_hours", "liquid_hours"):
                if f in v.spec:
                    out.append(f"* `{f}` (same for every row): {_md(v.spec[f])}")
                else:
                    for c in v.contracts:
                        out.append(f"* `{f}` {_md(c.get('local_symbol'))}: {_md(c.get(f))}")
            out.append("")
    return "\n".join(out)


def render_calendar(props: list[FamilyProposal], policy: ProposalPolicy, cat: dict) -> str:
    rule = ("roll_date = the execution vehicle's last-trade date minus "
            f"{policy.roll_days} business days (Mon-Fri"
            + (f", less {len(policy.holidays)} listed holidays" if policy.holidays else
               "; exchange holidays not applied: no holiday list given") + ")") \
        if policy.roll_days is not None else \
        "no rule parameter set: every roll_date is an owner input"
    out = ["# Proposed roll calendar", "", f"Rule: {rule}. Report date: {_md(report_date(cat))}.",
           ("The report carries neither first-notice dates nor the broker's close-out dates, nor "
           "whether a contract settles physically or in cash: each is flagged for owner/broker "
           "confirmation, never guessed."), ""]
    for p in props:
        if not p.calendar:
            continue
        out += [f"## {p.plan.family}: {_md(p.execution.query)}", ""]
        out += [f"**WARNING:** {_html_outside_code(one_line(w))}." for w in p.warnings]
        if p.warnings:
            out.append("")
        if p.same_series_diffs:
            out += [(f"Same-series proposal: reference `{_code(p.reference.query)}`. Roll dates "
                    "follow the execution vehicle's own last-trade dates; each month's difference "
                    "from the reference is listed in the flags."), ""]
        out += ["| month | local symbol | last trade | from field | proposed roll_date | flags |",
                "|---|---|---|---|---|---|"]
        for r in p.calendar:
            roll = _md(r["roll_date"]) if r["usable"] else f"UNUSABLE ({_md(r['roll_date'])})"
            out.append(f"| {_md(r['contract_month'])} | {_md(r['local_symbol'])} | "
                       f"{_md(r['last_trade_date'])} | {_md(r['last_trade_source'])} | "
                       f"{roll} | {_md('; '.join(r['flags']))} |")
        out.append("")
    return "\n".join(out)


_MEANING = {
    "DIRECT": "the price of {x} itself: entry, stop and target are checked against {x}'s own "
              "quote and must lie on its tick",
    "DIRECT_SAME": "the price of the {r} futures month the instruction names. Executing it in {x} "
                   "rests on an assertion the owner must make (same_series, with its reason): "
                   "that {x} of that month is priced as the same series. The report does not "
                   "show this",
    "BASIS_SHIFT": "a price of {r}; at each decision it is converted into {x} by adding the live "
                   "basis ({x} mid minus {r} mid, both fresh), held if the basis exceeds the cap",
    "REFERENCE_TRIGGER": "a level of {r}; the entry is a limit priced from {x}'s own quote, and "
                         "stop and target are broker-side conditional market exits released when "
                         "{r}'s last price crosses them (plus an optional backstop in {x})",
}


def _num(d: Decimal) -> str:
    return format(d.normalize(), "f")


def render_sheet(p: FamilyProposal, cat: dict) -> str:
    fam, plan, x, r = p.plan.family, p.plan, p.execution, p.reference
    out = [f"# Approval sheet: {fam} (PROPOSAL, NOT APPROVED)", "",
           (f"Discovery report {_md(cat.get('started_at'))} (overall {_md(cat.get('overall'))}). "
           f"Treatment named by the {plan.source}: **{plan.treatment}**. "
           "Every entry this tool writes is `approved = false`."), ""]
    if p.warnings:
        out += ["## Warnings", ""] + [
            f"* **WARNING:** {_html_outside_code(one_line(w))}." for w in p.warnings] + [""]
    if p.unsupported_reason:
        out += [("**Not proposable from this report.** The proposed entry is `UNSUPPORTED` and "
                f"HOLDs: {_md(p.unsupported_reason)}."), "",
                ("Owner/broker: re-run discovery with a query the broker resolves, or choose "
                "another vehicle explicitly. No substitute is chosen by this tool."), ""]
        return "\n".join(out)
    xs, rs = x.spec, r.spec
    xname = f"{_md(xs.get('symbol'))} ({_md(xs.get('sec_type'))}, {_md(xs.get('exchange'))})"
    rname = f"{_md(rs.get('symbol'))} ({_md(rs.get('sec_type'))}, {_md(rs.get('exchange'))})"
    key = "DIRECT_SAME" if p.entry.get("same_series") else plan.treatment
    out += ["## What the publisher's level means", "",
            (_md(plan.level_meaning) + " ") if plan.level_meaning else "",
            "Under this treatment a level is " + _MEANING[key].format(x=xname, r=rname) + ".", ""]
    months = [c["month"] for c in p.entry.get("contracts", [])]
    out += ["## What the bot would trade", "",
            f"{xname}, currency {_md(xs.get('currency'))}"
            + (f", months {_md(', '.join(months))}" if months else "") + ".", ""]
    stock = xs.get("sec_type") == "STK"
    m, t = positive(xs.get("multiplier")), positive(xs.get("min_tick"))
    m_label = _num(m) if m is not None else show(xs.get("multiplier"))
    if m is None and stock and absent(xs.get("multiplier")):
        m = Decimal(1)
        m_label = "1 (the loader's default for shares; the report states none)"
    mag = xs.get("price_magnifier")
    why_not = []
    if m is None:
        why_not.append(f"multiplier {_md(xs.get('multiplier'))} is not a positive number "
                       "stated by the report")
    if t is None:
        why_not.append(f"minimum tick {_md(xs.get('min_tick'))} is not a positive number "
                       "stated by the report")
    if x.kind != "option_chain" and positive(mag) != 1:
        why_not.append(f"price_magnifier {_md(mag)}: prices may not be in the contract's own "
                       "units")
    out += ["## Contract size and risk", ""]
    if why_not:
        out.append("* no dollar values are computed: " + "; ".join(why_not) + ".")
    else:
        unit = "share" if stock else "contract"
        out += [(f"* multiplier {m_label} -> **USD {_num(m)} per 1.00 price point** "
                f"per {unit}"),
                (f"* minimum tick {_num(t)} -> **USD {_num(m * t)} per tick** "
                f"per {unit}")]
        if plan.illustrative_price is not None:
            pr = plan.illustrative_price
            out.append(f"* a 1% move at the owner-supplied price {pr} = 0.01 x {pr} x "
                       f"{_num(m)} = **USD {(pr * m / 100).quantize(Decimal('0.01'))}** "
                       f"per {unit} (illustrative price supplied by the owner, not a market "
                       "price)")
        else:
            out.append(f"* a 1% move at price P = 0.01 x P x {_num(m)} = USD "
                       f"{_num(m / 100)} x P per {unit} (supply P with --price "
                       f"{fam}=P; no market price is assumed)")
    out.append("")
    if p.same_series_diffs:
        xsym, rsym = _md(xs.get("symbol")), _md(rs.get("symbol"))
        out += ["## Same-series claim (an assertion the owner must make)", "",
                (f"`same_series = true` asserts that {xsym} of a month is priced as the same "
                 f"series as {rsym} of that month. The report shows listing facts only (symbols, "
                 "months, last-trade dates, multipliers, ticks); it never shows price equality. "
                 "The owner must make and justify the assertion (`same_series_reason`)."), "",
                (f"Listing differences, month by month; roll dates are proposed from the "
                 f"execution vehicle's ({xsym}) last-trade dates, not the reference's"
                 + ("." if p.holidays_applied else
                    ". Business days are counted Monday to Friday: exchange holidays not "
                    "applied (no holiday list given), so a month spanning a holiday shows one "
                    "day more than the exchange calendar would.")), "",
                f"| month | {xsym} last trade | {rsym} last trade | difference |",
                "|---|---|---|---|"]
        for d in p.same_series_diffs:
            out.append(f"| {_md(d['month'])} | {_md(d['execution_last_trade'])} | "
                       f"{_md(d['reference_last_trade'])} | {_md(d['text'])} |")
        out.append("")
    out += ["## Facts from the broker (this report)", ""]
    for role, v in (("execution", x), ("reference", r)) if r is not x else (("execution", x),):
        all_ids = [str(c.get("con_id")) for c in v.contracts]
        ids = ", ".join(all_ids[:12]) or "absent"
        if len(all_ids) > 12:
            ids = f"first 12 of {len(all_ids)}: {ids}"
        facts = ", ".join(f"{k} {_md(v.spec.get(k))}" for k in (
            "sec_type", "symbol", "exchange", "currency", "multiplier", "min_tick",
            "size_increment", "price_magnifier", "trading_class", "time_zone_id") if k in v.spec)
        out.append(f"* {role} query `{_code(v.query)}`: {facts}; conIds {_md(ids)}")
        for k, why in v.spec_problems.items():
            if k not in ("trading_hours", "liquid_hours"):
                out.append(f"  * `{k}`: {_md(why)}")
        for k in ("trading_hours", "liquid_hours"):
            val = _md(v.spec[k] if k in v.spec else v.spec_problems.get(k, "absent"))
            out.append(f"  * `{k}`: {val}")
    if p.calendar:
        unusable = [r["contract_month"] for r in p.calendar if not r["usable"]]
        out.append(f"* roll calendar: {len(p.calendar)} listed months; see roll_calendar.md. "
                   f"{FND_FLAG}."
                   + ("" if p.holidays_applied else
                      " exchange holidays not applied (no holiday list given).")
                   + (" UNUSABLE (roll date already reached): "
                      f"{_md(', '.join(map(str, unusable)))}."
                      if unusable else ""))
    out.append("* Not in the report: settlement type, first-notice and close-out dates, "
               "margins, trading permissions, market-data entitlements.")
    out.append("")
    out += ["## Decisions the owner must make", ""]
    decisions = [
        f"Treatment: confirm **{plan.treatment}** and that the publisher's levels mean {rname}.",
        "Sizing: `units_per_signal`, `max_units`, `max_notional` (USD)"
        + (" (not set: the version-1 stock policy sizing applies)"
           if "sizing" not in p.entry else ""),
        ("Limits: the gateway policy caps and, if `REFERENCE_AS_COLLAR` is wanted, its anchor "
        "scope, adverse/favourable moves and signal age (default `EXPLICIT_ENTRY_ONLY`)."),
    ]
    if x.is_future:
        decisions += [
            ("Roll margin: `roll.business_days_before`, `contract_selection` rule and "
            "`min_business_days_to_roll`, and each `roll_date` (confirm first notice / close-out "
            "with the broker)."),
        ]
        if plan.treatment == "DIRECT":
            decisions.append("Level contract rule: whether a level without a month means the "
                             "front contract (`FRONT_MONTH`).")
    if p.entry.get("same_series"):
        decisions.append(f"same_series: state why {_md(rs.get('symbol'))} and "
                         f"{_md(xs.get('symbol'))} "
                         "are one price series (`same_series_reason`).")
    if plan.treatment == "BASIS_SHIFT" or "basis_shift" in p.entry:
        decisions.append("Basis: `max_abs_basis`, `max_age_seconds`, `scale_ratio = 1`.")
    if plan.treatment == "REFERENCE_TRIGGER":
        decisions.append("Reference hours (when the reference updates) and whether a backstop "
                         "is required.")
    decisions += [
        "Session: `REGULAR_ONLY` or `EXTENDED`, timezone, days and windows (broker hours above).",
        f"Market data: real-time API data for {xname}"
        + (f" and for {rname}" if r is not x else "") + " (subscription names: not in the report).",
        (f"Trading permission for {_md(xs.get('sec_type'))} on {_md(xs.get('exchange'))}: a FOUND "
        "contract does not prove the account may trade it."),
        "Then, and only then: `approved = true`, `approved_by`, `approved_at`.",
    ]
    out += [f"{i}. {d}" for i, d in enumerate(decisions, 1)]
    out.append("")
    if p.owner_inputs or p.broker_missing:
        out += ["## Still open in the proposed entry (it will not load until filled)", ""]
        out += [f"* owner input: `{_code(k)}`" for k in p.owner_inputs]
        out += [f"* broker fact missing: `{_code(k)}`" for k in p.broker_missing]
        out.append("")
    return "\n".join(out)


def write_outputs(props: list[FamilyProposal], cat: dict, policy: ProposalPolicy,
                  out_dir: str | Path) -> list[Path]:
    """Render everything, re-check the proposal, then write into a new or empty directory.
    Nothing is written when any check fails."""
    d = Path(out_dir)
    if d.exists() and (not d.is_dir() or any(d.iterdir())):
        raise ProposalInputInvalid(f"output directory {d} is not empty: choose a new one")
    for p in props:
        check_family_name(p.plan.family)
    toml_bytes = render_toml(props, cat).encode("utf-8")
    verify_rendered(toml_bytes.decode("utf-8"), props)  # the exact bytes written below
    toml_text = toml_bytes.decode("utf-8")
    files = {"proposed_mapping.toml": toml_text,
             "contract_table.md": render_contract_table(props, cat),
             "roll_calendar.md": render_calendar(props, policy, cat)}
    for p in props:
        files[f"approval_{p.plan.family.replace('.', '_')}.md"] = render_sheet(p, cat)
    d.mkdir(parents=True, exist_ok=True)
    written = []
    for name, text in files.items():
        path = d / name
        path.write_bytes(toml_bytes if name == "proposed_mapping.toml" else text.encode("utf-8"))
        written.append(path)
    return written
