"""Trading signal adjudication: the bundle, the reviewers file, the annotation log, the comparison
with the interpreter's reading and the frozen ground-truth versions (docs/ADJUDICATION_WORKSPACE.md;
contract: the adjudication workspace specification, version 1).

Pure (no Streamlit). Like ``vocab_recommend`` it must not import the runtime service, mapping,
execution, ``ibkr``, order-capable, storage or validator modules. Every private file is read
through the private-path guards (no link, link count 1, mode 0600) and every refusal is a constant
code, never a path or content.

* ``load_bundle(path, FULL)`` validates the whole bundle; ``load_bundle(path, RESTRICTED)`` never
  opens ``posts.jsonl`` or ``media/`` (it has no code path that does).
* ``append_annotation`` is the app's only write (chained like the recommendations log).
* ``freeze`` writes ``versions/ground_truth_v<N>.jsonl`` and its manifest (owner, local).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tomllib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from tradingbot.runtime.vocab_recommend import (
    RecommendationError,
    Verification,
    _read_private,
    append_chained,
    identity_from_headers,
    verify_log,
)
from tradingbot.runtime.vocab_review import (  # noqa: F401 - PrivatePathError for the app
    _FAMILY_RE,
    PrivateDir,
    PrivatePathError,
    check_private_path,
)

BUNDLE_SCHEMA = "tradingbot.adjudication.bundle/1"
ANNOTATION_SCHEMA = "tradingbot.adjudication.annotation/1"
GROUND_TRUTH_SCHEMA = "tradingbot.adjudication.ground_truth/1"
DIR_ENV, LOCAL_IDENTITY_ENV = "TRADINGBOT_ADJUDICATION_DIR", "TRADINGBOT_REVIEW_LOCAL_IDENTITY"

# Closed value sets (section 3 of the specification; the export must use exactly these).
POST_LABELS = ("ACTIONABLE", "CONDITIONAL", "COMMENTARY", "RETROSPECTIVE", "UNRESOLVED")
ACTIONS = ("OPEN", "ADD", "MODIFY", "REDUCE", "CLOSE", "NONE")
SIDES = ("LONG", "SHORT", "NONE")
EFFECTS = ("OPENS", "INCREASES", "REDUCES", "CLOSES", "AMENDS_PROTECTION", "NONE")
# Price roles (specification 1.2, the owner's taxonomy), for every level and for entry.role.
PRICE_ROLES = ("CURRENT_MARKET", "INTENDED_ENTRY", "HISTORICAL_FILL", "AVERAGE_OR_COST_BASIS",
               "STOP", "TARGET", "CHART_LEVEL", "OTHER_CONTRACT_MONTH", "UNRESOLVED")
TABLE_ROLES = (*PRICE_ROLES, "NOT_A_PRICE")  # the reviewer's price table
ENTRY_ROLES = (*PRICE_ROLES, "NOT_STATED")
EXACTNESS = ("EXACT", "APPROXIMATE", "NOT_APPLICABLE")  # APPROXIMATE: an approximate quotation
CONTRACT_MONTHS = ("FRONT", "NEXT", "NAMED", "NOT_STATED", "NOT_APPLICABLE")
LEVEL_KEYS = ("role", "exactness", "contract_month")
TIMINGS = ("CURRENT", "HISTORICAL", "CONDITIONAL", "COMMENTARY", "UNCERTAIN")
HOLD_STATUSES = ("SENT", "HOLD", "BLOCK", "REFUSED", "HALTED", "UNRESOLVED", "EXCEPTION",
                 "REJECTED", "NO_TRADE", "RECONCILE", "NOT_DECIDED")
# Families: ``manifest.families`` plus these (specification 1.1).
FAMILY_EXTRA = ("OTHER", "UNKNOWN")
SETS, KINDS = ("DESIGN", "CHECK"), ("INSTRUCTION", "LABEL_ONLY")
HOLD_SCENARIOS, POSITION_STATES = ("SHIPPED", "FIVE_FAMILY"), ("OPEN", "FLAT")
# Section 4 and 5.
REVIEWER, APPROVER, FULL, RESTRICTED = "REVIEWER", "APPROVER", "FULL", "RESTRICTED"
INDEPENDENT, AFTER_REVEAL, APPROVAL = "INDEPENDENT", "AFTER_REVEAL", "APPROVAL"
YES_NO_UNSURE, YES_NO = ("YES", "NO", "UNSURE"), ("YES", "NO")
FLAGS = ("MISSING_CONTEXT", "AMBIGUOUS_PRICE_SEMANTICS", "NEEDS_MORE_EVIDENCE", "CHART_NEEDED")
CONFIDENCE = ("HIGH", "MEDIUM", "LOW")
VERDICTS, OUTCOMES = ("CONFIRM", "CORRECT", "UNSURE"), ("ACCEPT", "AMEND", "RETURN")
IDEA_FIXED = ("SAME_AS_PREVIOUS", "NEW_IDEA", "UNKNOWN")  # or "SEQUENCE:<sequence_id>"
MAX_NOTES, MAX_TEXT, MAX_MEDIA_BYTES = 2000, 80, 5 * 1024 * 1024

# The reader's form: every field of the ``ai`` block, flat ("" = not stated), plus the idea
# association and the two mapping questions. Value kinds: a tuple is a closed set.
FIELDS: dict[str, tuple | str] = {
    "post_label": POST_LABELS, "action": ACTIONS, "family": "family",
    "instrument_as_read": "text", "side": SIDES, "effect": EFFECTS, "timing": TIMINGS,
    "explicit": YES_NO, "entry_price": "price", "entry_role": ENTRY_ROLES,
    "entry_exactness": EXACTNESS, "stop_price": "price", "targets": "prices",
    "amendment_field": "code", "amendment_from": "text", "amendment_to": "text",
    "ambiguities": "codes", "position_state": POSITION_STATES, "position_side": SIDES,
    "position_last_stop": "price", "idea": "idea",
    "reference_series_right": YES_NO_UNSURE, "execution_instrument_right": YES_NO_UNSURE,
}
NOT_COMPARED = ("reference_series_right", "execution_instrument_right")
LABEL_ONLY_FIELDS = ("post_label",)
LEVELS = "levels"  # the price table: reading[LEVELS] = [{row, role, exactness, contract_month}]
DEFAULT_SCOPE = 20

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
_CODE = re.compile(r"^[A-Z][A-Z0-9_:.]{0,63}$")
_MEDIA = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,100}\.(png|jpg|jpeg|gif|webp)$")
_PRICE = re.compile(r"^\d{1,9}(\.\d{1,8})?$")
_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_MAGIC = (b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a")
PROXY_MARKERS = ("X-Forwarded-For", "X-Forwarded-Host", "X-Forwarded-Proto", "Forwarded",
                 "Cf-Access-Jwt-Assertion", "Cf-Connecting-Ip")


class AdjudicationError(ValueError):
    """Refused; ``code`` is a constant and at most a field name, never a path or content."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


# ------------------------------------------------------------------ the bundle


@dataclass
class Bundle:
    root: Path
    access: str
    bundle_id: str
    manifest: dict
    sequences: list[dict]
    items: dict[str, dict]  # item_id -> item, in file order
    families: list[str]
    posts: dict[str, dict] | None = None  # FULL only


def _lines(data: bytes, what: str) -> list[dict]:
    out = []
    for line in data.decode("utf-8").splitlines():
        if line.strip():
            try:
                raw = json.loads(line)
            except ValueError:
                raise AdjudicationError(f"NOT_JSON:{what}") from None
            if not isinstance(raw, dict):
                raise AdjudicationError(f"NOT_AN_OBJECT:{what}")
            out.append(raw)
    return out


def _read(root: Path, name: str) -> bytes:
    data = _read_private(root / name, f"adjudication bundle file {name}")
    if data is None:
        raise AdjudicationError(f"MISSING:{name}")
    return data


def _id(v, what: str, *, optional: bool = False) -> str | None:
    if v is None and optional:
        return None
    if not (isinstance(v, str) and _ID.match(v)):
        raise AdjudicationError(f"BAD_ID:{what}")
    return v


def _closed(v, allowed, what: str):
    if v is None:
        return None
    if v not in allowed:
        raise AdjudicationError(f"UNKNOWN_VALUE:{what}")
    return v


def _price(v, what: str) -> str:
    if v is None or v == "":
        return ""
    if isinstance(v, bool) or not isinstance(v, int | float | str):
        raise AdjudicationError(f"BAD_PRICE:{what}")
    try:
        d = Decimal(str(v))
    except InvalidOperation:
        raise AdjudicationError(f"BAD_PRICE:{what}") from None
    text = format(d.normalize(), "f")
    if not _PRICE.match(text):
        raise AdjudicationError(f"BAD_PRICE:{what}")
    return text


def _short(v, what: str, limit: int = MAX_TEXT) -> str:
    if v is None:
        return ""
    if not isinstance(v, str) or len(v) > limit or _CTRL.search(v):
        raise AdjudicationError(f"BAD_TEXT:{what}")
    return v


def _codes(values, what: str) -> list[str]:
    if values is None:
        return []
    if not isinstance(values, list) or not all(isinstance(c, str) and _CODE.match(c)
                                               for c in values):
        raise AdjudicationError(f"BAD_CODES:{what}")
    return list(values)


def _family(v, families, what: str = "family") -> str | None:
    if v is None:
        return None
    ok = v in FAMILY_EXTRA or v in families
    if not ok:
        raise AdjudicationError(f"UNKNOWN_VALUE:{what}")
    return v


def _ai(raw, families) -> dict:
    """The interpreter's reading, flattened to ``FIELDS`` ("" = not stated); unknown keys
    dropped, every closed value checked."""
    a = raw if isinstance(raw, dict) else {}
    entry, stop = a.get("entry") or {}, a.get("stop") or {}
    amend, pos = a.get("amendment") or {}, a.get("position_context") or {}
    if not all(isinstance(x, dict) for x in (entry, stop, amend, pos)) or not isinstance(
            a.get("targets") or [], list):
        raise AdjudicationError("BAD_SHAPE:ai")
    explicit = a.get("explicit")
    if explicit is not None and not isinstance(explicit, bool):
        raise AdjudicationError("UNKNOWN_VALUE:explicit")
    targets = [_price((t or {}).get("price") if isinstance(t, dict) else None, "targets")
               for t in a.get("targets") or []]
    return {
        "post_label": _closed(a.get("post_label"), POST_LABELS, "post_label") or "",
        "action": _closed(a.get("action"), ACTIONS, "action") or "",
        "family": _family(a.get("family"), families) or "",
        "instrument_as_read": _short(a.get("instrument_as_read"), "instrument_as_read"),
        "side": _closed(a.get("side"), SIDES, "side") or "",
        "effect": _closed(a.get("effect"), EFFECTS, "effect") or "",
        "timing": _closed(a.get("timing"), TIMINGS, "timing") or "",
        "explicit": "" if explicit is None else ("YES" if explicit else "NO"),
        "entry_price": _price(entry.get("price"), "entry.price"),
        "entry_role": _closed(entry.get("role"), ENTRY_ROLES, "entry.role") or "",
        "entry_exactness": _closed(entry.get("exactness"), EXACTNESS, "entry.exactness") or "",
        "stop_price": _price(stop.get("price"), "stop.price"),
        "targets": ", ".join(t for t in targets if t),
        "amendment_field": _closed(amend.get("field"), _Pattern(_CODE), "amendment.field") or "",
        "amendment_from": _short(_str(amend.get("from")), "amendment.from"),
        "amendment_to": _short(_str(amend.get("to")), "amendment.to"),
        "ambiguities": ", ".join(_codes(a.get("ambiguities"), "ambiguities")),
        "position_state": _closed(pos.get("state"), POSITION_STATES, "position_context.state")
        or "",
        "position_side": _closed(pos.get("side"), SIDES, "position_context.side") or "",
        "position_last_stop": _price(pos.get("last_stop"), "position_context.last_stop"),
        "idea_ref": _id(a.get("idea_ref"), "idea_ref", optional=True) or "",
    }


class _Pattern:
    """``in`` against a pattern, for ``_closed``."""

    def __init__(self, pattern):
        self.pattern = pattern

    def __contains__(self, v):
        return isinstance(v, str) and bool(self.pattern.match(v))


def _str(v):
    return None if v is None else str(v)


def _holds(raw) -> dict:
    if raw is None:
        return {}
    if not isinstance(raw, dict) or not set(raw) <= set(HOLD_SCENARIOS):
        raise AdjudicationError("UNKNOWN_VALUE:holds")
    out = {}
    for k, v in raw.items():
        if not isinstance(v, dict):
            raise AdjudicationError("BAD_SHAPE:holds")
        out[k] = {"status": _closed(v.get("status"), HOLD_STATUSES, "holds.status") or "",
                  "reasons": _codes(v.get("reasons"), "holds.reasons")}
    return out


def _mapping(raw) -> dict:
    m = raw if isinstance(raw, dict) else {}
    approved = m.get("approved")
    if approved is not None and not isinstance(approved, bool):
        raise AdjudicationError("UNKNOWN_VALUE:mapping.approved")
    return {"reference_series": _short(m.get("reference_series"), "mapping.reference_series"),
            "proposed_execution_instrument": _short(m.get("proposed_execution_instrument"),
                                                    "mapping.proposed_execution_instrument"),
            "basis_treatment": _closed(m.get("basis_treatment"), _Pattern(_CODE),
                                       "mapping.basis_treatment") or "",
            "approved": approved}


def _item(raw: dict, sequences: dict, families) -> dict:
    kind = _closed(raw.get("kind"), KINDS, "kind")
    seq = _id(raw.get("sequence_id"), "sequence_id", optional=True)
    if kind is None or (seq is not None and seq not in sequences):
        raise AdjudicationError("UNKNOWN_VALUE:kind_or_sequence")
    refs = raw.get("context_refs") or []
    if not isinstance(refs, list):
        raise AdjudicationError("BAD_SHAPE:context_refs")

    def num(v, what):
        if v is not None and (isinstance(v, bool) or not isinstance(v, int) or v < 0):
            raise AdjudicationError(f"BAD_NUMBER:{what}")
        return v

    return {"item_id": _id(raw.get("item_id"), "item_id"), "kind": kind, "sequence_id": seq,
            "set": _closed(raw.get("set"), SETS, "set"),
            "post_ref": _id(raw.get("post_ref"), "post_ref"),
            "slot": num(raw.get("slot"), "slot"),
            "order_in_sequence": num(raw.get("order_in_sequence"), "order_in_sequence"),
            "context_refs": [_id(r, "context_refs") for r in refs],
            "ai": _ai(raw.get("ai"), families), "levels": _levels(raw.get("ai")),
            "holds": _holds(raw.get("holds")), "mapping": _mapping(raw.get("mapping"))}


def _levels(ai) -> list[dict]:
    """The interpreter's typed levels: ``{n, price, role, exactness, contract_month}``."""
    raw = (ai or {}).get("levels") if isinstance(ai, dict) else None
    if raw is None:
        return []
    if not isinstance(raw, list) or not all(isinstance(lv, dict) for lv in raw):
        raise AdjudicationError("BAD_SHAPE:levels")
    out = []
    for lv in raw:
        n = lv.get("n")
        if n is not None and (isinstance(n, bool) or not isinstance(n, int) or n < 0):
            raise AdjudicationError("BAD_NUMBER:levels.n")
        out.append({"n": n, "price": _price(lv.get("price"), "levels.price"),
                    "role": _closed(lv.get("role"), PRICE_ROLES, "levels.role") or "",
                    "exactness": _closed(lv.get("exactness"), EXACTNESS, "levels.exactness")
                    or "",
                    "contract_month": _closed(lv.get("contract_month"), CONTRACT_MONTHS,
                                              "levels.contract_month") or ""})
    return out


def _post(raw: dict) -> dict:
    media = raw.get("media") or []
    if not isinstance(media, list) or not all(isinstance(m, str) and _MEDIA.match(m)
                                              for m in media):
        raise AdjudicationError("BAD_MEDIA_NAME")
    text, published = raw.get("text"), raw.get("published_at")
    if not isinstance(text, str) or not isinstance(published, str):
        raise AdjudicationError("BAD_POST")
    try:
        datetime.fromisoformat(published)
    except ValueError:
        raise AdjudicationError("BAD_POST:published_at") from None
    numbers = raw.get("numbers") or []
    if not isinstance(numbers, list) or not all(
            isinstance(x, dict) and x.get("n") == k and isinstance(x.get("token"), str)
            and 0 < len(x["token"]) <= 32 and not _CTRL.search(x["token"])
            for k, x in enumerate(numbers)):
        raise AdjudicationError("BAD_NUMBERS")
    return {"post_ref": _id(raw.get("post_ref"), "post_ref"), "published_at": published,
            "text": text, "media": media, "revision": raw.get("revision"),
            "numbers": [x["token"] for x in numbers],
            "reply_to": _id(raw.get("reply_to"), "reply_to", optional=True),
            "quotes": _id(raw.get("quotes"), "quotes", optional=True)}


def load_bundle(path: str | Path, access: str) -> Bundle:
    """The bundle, validated. RESTRICTED reads ``manifest.json``, ``sequences.jsonl`` and
    ``items.jsonl`` only: ``posts.jsonl`` and ``media/`` are never opened."""
    if access not in (FULL, RESTRICTED):
        raise AdjudicationError("UNKNOWN_ACCESS")
    root = check_private_path(Path(path).expanduser() / "manifest.json", "bundle").parent
    try:
        manifest = json.loads(_read(root, "manifest.json"))
    except ValueError:
        raise AdjudicationError("NOT_JSON:manifest") from None
    if not isinstance(manifest, dict) or manifest.get("schema") != BUNDLE_SCHEMA:
        raise AdjudicationError("BUNDLE_SCHEMA")
    bundle_id = manifest.get("bundle_id")
    if not (isinstance(bundle_id, str) and re.fullmatch(r"[0-9a-f]{64}", bundle_id)):
        raise AdjudicationError("BAD_BUNDLE_ID")
    families = manifest.get("families")
    if not (isinstance(families, list) and all(isinstance(f, str) and _FAMILY_RE.match(f)
                                               for f in families)):
        raise AdjudicationError("UNKNOWN_VALUE:families")
    families = [f for f in dict.fromkeys(families) if f not in FAMILY_EXTRA]
    sequences = {}
    for raw in _lines(_read(root, "sequences.jsonl"), "sequences"):
        sid = _id(raw.get("sequence_id"), "sequence_id")
        refs, order = raw.get("post_refs"), raw.get("review_order")
        if sid in sequences or not isinstance(refs, list) or isinstance(order, bool) \
                or not isinstance(order, int):
            raise AdjudicationError("BAD_SEQUENCE")
        sequences[sid] = {"sequence_id": sid, "set": _closed(raw.get("set"), SETS, "set"),
                          "post_refs": [_id(r, "post_refs") for r in refs],
                          "review_order": order}
    if sorted(s["review_order"] for s in sequences.values()) != list(
            range(1, len(sequences) + 1)):
        raise AdjudicationError("BAD_REVIEW_ORDER")
    items: dict[str, dict] = {}
    for raw in _lines(_read(root, "items.jsonl"), "items"):
        item = _item(raw, sequences, families)
        if item["item_id"] in items:
            raise AdjudicationError("DUPLICATE_ITEM")
        items[item["item_id"]] = item
    bundle = Bundle(root, access, bundle_id, manifest,
                    sorted(sequences.values(), key=lambda s: s["review_order"]), items,
                    families + list(FAMILY_EXTRA))
    if access == FULL:
        posts = {}
        for raw in _lines(_read(root, "posts.jsonl"), "posts"):
            p = _post(raw)
            posts[p["post_ref"]] = p
        wanted = {r for s in sequences.values() for r in s["post_refs"]} | {
            r for i in items.values() for r in [i["post_ref"], *i["context_refs"]]} | {
            p[k] for p in posts.values() for k in ("reply_to", "quotes") if p[k]}
        if not wanted <= set(posts):
            raise AdjudicationError("UNKNOWN_POST_REF")
        if any(lv["n"] is not None and lv["n"] >= len(posts[i["post_ref"]]["numbers"])
               for i in items.values() for lv in i["levels"]):
            raise AdjudicationError("LEVEL_NOT_IN_NUMBERS")
        bundle.posts = posts
    return bundle


def read_media(bundle: Bundle, post_ref: str, name: str) -> bytes:
    """One image of a post, by a name the post lists, from ``media/`` only; FULL only."""
    if bundle.access != FULL or bundle.posts is None:
        raise AdjudicationError("RESTRICTED")
    post = bundle.posts.get(post_ref)
    if post is None or name not in post["media"] or not _MEDIA.match(name):
        raise AdjudicationError("MEDIA_NOT_LISTED")
    with PrivateDir(bundle.root / "media" / name, "bundle image") as d:
        fd = d.open(d.name, os.O_RDONLY)
        with os.fdopen(fd, "rb") as f:
            if os.fstat(f.fileno()).st_size > MAX_MEDIA_BYTES:
                raise AdjudicationError("MEDIA_TOO_LARGE")
            data = f.read(MAX_MEDIA_BYTES + 1)
    if len(data) > MAX_MEDIA_BYTES:
        raise AdjudicationError("MEDIA_TOO_LARGE")
    if not (data.startswith(_MAGIC) or (data[:4] == b"RIFF" and data[8:12] == b"WEBP")):
        raise AdjudicationError("MEDIA_NOT_AN_IMAGE")
    return data


def ordered(bundle: Bundle, scope: int | None = None) -> list[tuple[str | None, list[dict]]]:
    """Items by sequence in ``review_order`` (a balanced round-robin: any prefix is a balanced
    sample), each sequence's label-only items with it, then the items of no sequence (the
    sampled label-only posts). Within a sequence: in time order (the post's place in the
    sequence's time-ordered ``post_refs``), then slot, then order. With ``scope``: only the
    first ``scope`` sequences, and the items of no sequence only once the scope covers all."""
    out = []
    shown = bundle.sequences if scope is None else bundle.sequences[:scope]
    for s in shown:  # already in review_order
        refs = s["post_refs"]
        mine = [i for i in bundle.items.values() if i["sequence_id"] == s["sequence_id"]]
        mine.sort(key=lambda i: (refs.index(i["post_ref"]) if i["post_ref"] in refs else 1e9,
                                 i["slot"] or 0, i["order_in_sequence"] or 0))
        out.append((s["sequence_id"], mine))
    rest = [i for i in bundle.items.values() if i["sequence_id"] is None]
    if scope is not None and scope < len(bundle.sequences):
        rest = []
    return out + ([(None, rest)] if rest else [])


# ------------------------------------------------------------------ the system's reading


def fields_for(kind: str) -> tuple[str, ...]:
    return LABEL_ONLY_FIELDS if kind == "LABEL_ONLY" else tuple(FIELDS)


def system_reading(bundle: Bundle, item: dict) -> dict:
    """The ``ai`` block as a reading (the idea association derived from ``idea_ref``)."""
    ai = dict(item["ai"])
    ref, seq = ai.pop("idea_ref"), item["sequence_id"]
    sequences = {s["sequence_id"] for s in bundle.sequences}
    target = bundle.items[ref]["sequence_id"] if ref in bundle.items else (
        ref if ref in sequences else None)
    if not ref:
        idea = "NEW_IDEA"
    elif target is not None and target == seq:
        idea = "SAME_AS_PREVIOUS"
    elif target is not None:
        idea = f"SEQUENCE:{target}"
    else:
        idea = "UNKNOWN"
    out = {**ai, "idea": idea, "reference_series_right": "", "execution_instrument_right": ""}
    return {f: out[f] for f in fields_for(item["kind"])}


def _norm(f: str, v: str) -> str:
    """"" is "not stated", a first-class answer: equal to "" (and, for the entry role, to
    NOT_STATED)."""
    if f == "entry_role" and v == "NOT_STATED":
        return ""
    if FIELDS[f] == "price" and v:
        try:
            return format(Decimal(v).normalize(), "f")
        except InvalidOperation:
            return v
    if FIELDS[f] in ("prices", "codes"):
        return ",".join(sorted(x.strip().upper() for x in v.split(",") if x.strip()))
    return v.strip().casefold()


def prefill_verdicts(reading: dict, system: dict, kind: str) -> dict[str, str]:
    """Per compared field: CONFIRM when the reviewer's value equals the system's, else CORRECT."""
    return {f: "CONFIRM" if _norm(f, reading.get(f, "")) == _norm(f, system.get(f, ""))
            else "CORRECT" for f in fields_for(kind) if f not in NOT_COMPARED}


def table_rows(bundle: Bundle, item: dict) -> list[tuple[str, str]]:
    """The price table's rows (key, label). FULL: one per number of the post's ``numbers``,
    labelled with its token (source-derived, no role). RESTRICTED (no text): one per typed level
    of the system's reading, labelled with its price. LABEL_ONLY items have no table."""
    if item["kind"] == "LABEL_ONLY":
        return []
    if bundle.posts is not None:
        return [(f"n:{k}", t) for k, t in enumerate(bundle.posts[item["post_ref"]]["numbers"])]
    return [(f"level:{k}", lv["price"] or "-") for k, lv in enumerate(item["levels"])]


def system_rows(bundle: Bundle, item: dict) -> dict[str, dict]:
    """The system's answer per table row; a number the interpreter typed no level for is, in
    its reading, not a price."""
    out = {}
    for key, _label in table_rows(bundle, item):
        kind, k = key.split(":")
        if kind == "level":
            lv = item["levels"][int(k)]
        else:
            lv = next((x for x in item["levels"] if x["n"] == int(k)), None)
        out[key] = ({f: lv[f] for f in LEVEL_KEYS} if lv else
                    {"role": "NOT_A_PRICE", "exactness": "", "contract_month": ""})
    return out


def prefill_row_verdicts(levels: list[dict], system: dict[str, dict]) -> dict[str, str]:
    """Per row: CONFIRM when role, exactness and contract month equal the system's (two
    NOT_A_PRICE rows agree whatever else), else CORRECT; an unanswered row is UNSURE."""
    out = {}
    for row in levels:
        sys_row = system.get(row["row"], {})
        same = (row["role"] == sys_row.get("role") == "NOT_A_PRICE") or all(
            row[f] == sys_row.get(f, "") for f in LEVEL_KEYS)
        out[row["row"]] = "UNSURE" if not row["role"] else "CONFIRM" if same else "CORRECT"
    return out


def _clean_levels(rows, bundle: Bundle, item: dict, partial: bool) -> list[dict]:
    keys = [k for k, _ in table_rows(bundle, item)]
    if not isinstance(rows, list) or not all(isinstance(r, dict) and set(r) <= {
            "row", *LEVEL_KEYS} and r.get("row") in keys for r in rows):
        raise AdjudicationError("BAD_VALUE:levels")
    given = {r["row"]: r for r in rows}
    out = []
    for k in keys:
        if partial and k not in given:
            continue
        r = given.get(k, {})
        row = {"row": k, **{f: str(r.get(f) or "") for f in LEVEL_KEYS}}
        for f, allowed in zip(LEVEL_KEYS, (TABLE_ROLES, EXACTNESS, CONTRACT_MONTHS), strict=True):
            if row[f] and row[f] not in allowed:
                raise AdjudicationError(f"UNKNOWN_VALUE:levels.{f}")
        out.append(row)
    return out


def clean_reading(reading: dict, item: dict, bundle: Bundle, *, partial: bool = False) -> dict:
    """A reader's values checked against the closed sets; "" = not stated (or, in the price
    table, unanswered)."""
    allowed = fields_for(item["kind"])
    table = item["kind"] != "LABEL_ONLY"
    if not isinstance(reading, dict) or not set(reading) <= set(allowed) | (
            {LEVELS} if table else set()):
        raise AdjudicationError("UNKNOWN_FIELD")
    sequences = {s["sequence_id"] for s in bundle.sequences}
    out = {}
    if table and (LEVELS in reading or not partial):
        out[LEVELS] = _clean_levels(reading.get(LEVELS, []), bundle, item, partial)
    for f in (reading if partial else allowed):
        if f == LEVELS:
            continue
        v = reading.get(f, "")
        if not isinstance(v, str) or _CTRL.search(v) or len(v) > MAX_TEXT:
            raise AdjudicationError(f"BAD_VALUE:{f}")
        v, kind = v.strip(), FIELDS[f]
        if v and isinstance(kind, tuple) and v not in kind:
            raise AdjudicationError(f"UNKNOWN_VALUE:{f}")
        if v and kind == "family" and v not in bundle.families:
            raise AdjudicationError("UNKNOWN_VALUE:family")
        if v and kind in ("code", "codes") and not all(
                _CODE.match(c.strip()) for c in v.split(",")):
            raise AdjudicationError(f"BAD_VALUE:{f}")
        if kind == "price":
            v = _price(v, f)
        if kind == "prices":
            v = ", ".join(_price(x.strip(), f) for x in v.split(",") if x.strip())
        if v and kind == "idea" and v not in IDEA_FIXED and not (
                v.startswith("SEQUENCE:") and v.removeprefix("SEQUENCE:") in sequences):
            raise AdjudicationError("UNKNOWN_VALUE:idea")
        out[f] = v
    return out


# ------------------------------------------------------------------ reviewers and sessions


@dataclass(frozen=True)
class Reviewer:
    login: str
    name: str
    role: str
    access: str


@dataclass
class Reviewers:
    entries: dict[str, Reviewer]  # casefolded login -> entry
    remote_full_context_authorized: bool = False
    scope_sequences: int = DEFAULT_SCOPE


def load_reviewers(path: str | Path) -> Reviewers:
    """The owner-edited reviewers file (TOML, mode 0600)::

        remote_full_context_authorized = false
        [[reviewer]]
        login = "ria@example.com"
        name = "Ria"
        role = "REVIEWER"   # or APPROVER
        access = "FULL"     # or RESTRICTED

    plus ``scope_sequences = 20`` (default): only the first that many sequences in review order
    are offered (the sampled label-only posts once it covers all).
    """
    data = _read_private(path, "reviewers file")
    if data is None:
        raise AdjudicationError("NO_REVIEWERS_FILE")
    try:
        raw = tomllib.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise AdjudicationError("REVIEWERS_FILE_UNREADABLE") from None
    switch = raw.get("remote_full_context_authorized", False)
    scope = raw.get("scope_sequences", DEFAULT_SCOPE)
    if not isinstance(switch, bool) or not set(raw) <= {
            "remote_full_context_authorized", "reviewer", "scope_sequences"} or isinstance(
            scope, bool) or not isinstance(scope, int) or scope < 1:
        raise AdjudicationError("REVIEWERS_FILE_UNREADABLE")
    entries = {}
    for r in raw.get("reviewer") or []:
        login, name = r.get("login"), r.get("name", "")
        if not (isinstance(login, str) and login.strip() and isinstance(name, str)) \
                or r.get("role") not in (REVIEWER, APPROVER) \
                or r.get("access") not in (FULL, RESTRICTED) or login.casefold() in entries:
            raise AdjudicationError("REVIEWERS_FILE_ENTRY")
        entries[login.casefold()] = Reviewer(login, name, r["role"], r["access"])
    return Reviewers(entries, switch, scope)


@dataclass(frozen=True)
class Session:
    login: str
    name: str
    role: str
    access: str  # the EFFECTIVE access of this session
    via_proxy: bool
    scope: int | None = None  # sequences offered (reviewers file); None: all (tools, tests)


def resolve_session(headers, env, reviewers: Reviewers, port: int) -> Session:
    """Who this session is and what it may see. Through the proxy (its login header present):
    that login, and RESTRICTED whatever the entry says while ``remote_full_context_authorized``
    is false. Without any proxy header and with a loopback Host: ``TRADINGBOT_REVIEW_LOCAL_IDENTITY``
    (the owner working locally). Not listed, or anything else: refused."""
    ident = identity_from_headers(headers, {k: v for k, v in env.items()
                                            if k.startswith("TRADINGBOT_REVIEWER_")
                                            and not k.endswith("ALLOWED_LOGINS")})
    if ident.refused:
        raise AdjudicationError(ident.refused)
    names = {k.casefold() for k in headers}
    proxied = any(k.startswith("tailscale-") for k in names) or any(
        m.casefold() in names for m in PROXY_MARKERS)
    if ident.login:
        login, via_proxy = ident.login, True
    elif proxied:
        raise AdjudicationError("PROXY_WITHOUT_IDENTITY")
    else:
        host = headers.get("Host", "")
        if host not in (f"127.0.0.1:{port}", f"localhost:{port}"):
            raise AdjudicationError("NOT_LOCAL")
        login, via_proxy = env.get(LOCAL_IDENTITY_ENV, "").strip(), False
    entry = reviewers.entries.get(login.casefold()) if login else None
    if entry is None:
        raise AdjudicationError("NOT_LISTED")
    access = entry.access if (not via_proxy or reviewers.remote_full_context_authorized) \
        else RESTRICTED
    return Session(entry.login, entry.name, entry.role, access, via_proxy,
                   reviewers.scope_sequences)


# ------------------------------------------------------------------ the annotation log


def annotations_path(base: str | Path) -> Path:
    return Path(base).expanduser() / "annotations.jsonl"


def read_annotations(base: str | Path) -> Verification:
    v = verify_log(annotations_path(base), ANNOTATION_SCHEMA, "annotation log")
    return v if v is not None else Verification(True)


def _of(records, item_id, bundle_id, phase=None, login=None):
    return [r for r in records if r.get("item_id") == item_id and r.get("bundle_id") == bundle_id
            and (phase is None or r.get("phase") == phase)
            and (login is None or (r.get("annotator") or {}).get("login") == login)]


def append_annotation(base: str | Path, bundle: Bundle, session: Session, *, item_id: str,
                      phase: str, blind: bool, reading: dict, field_verdicts: dict | None = None,
                      flags=(), confidence: str = "", notes: str = "",
                      refers_to: int | None = None, outcome: str | None = None,
                      now: datetime | None = None) -> dict:
    """Append ONE annotation record (the app's only write), validated against the bundle, the
    session's role and the records already in the log (under the log's lock)."""
    item = bundle.items.get(item_id)
    if item is None:
        raise AdjudicationError("UNKNOWN_ITEM")
    if session.scope is not None and item_id not in {
            i["item_id"] for _, items in ordered(bundle, session.scope) for i in items}:
        raise AdjudicationError("OUTSIDE_SCOPE")
    if phase not in (INDEPENDENT, AFTER_REVEAL, APPROVAL):
        raise AdjudicationError("UNKNOWN_PHASE")
    if (phase == APPROVAL) != (session.role == APPROVER):
        raise AdjudicationError("ROLE_MAY_NOT_WRITE_PHASE")
    flags = list(flags)
    if not set(flags) <= set(FLAGS) or (confidence and confidence not in CONFIDENCE) or (
            phase != APPROVAL and not confidence):
        raise AdjudicationError("FLAGS_OR_CONFIDENCE")
    if not isinstance(notes, str) or len(notes) > MAX_NOTES or _CTRL.search(notes):
        raise AdjudicationError("NOTES_TOO_LONG_OR_UNPRINTABLE")
    verdicts = dict(field_verdicts or {})
    if phase == APPROVAL:
        if outcome not in OUTCOMES:
            raise AdjudicationError("UNKNOWN_DECISION")
        reading = clean_reading(reading, item, bundle, partial=True)
        if (outcome == "AMEND") != bool(reading) or verdicts:
            raise AdjudicationError("AMEND_NEEDS_AMENDED_FIELDS_ONLY")
        blind = False
    else:
        reading = clean_reading(reading, item, bundle)
        compared = {f for f in fields_for(item["kind"]) if f not in NOT_COMPARED}
        if phase == INDEPENDENT and (not blind or verdicts or refers_to is not None
                                     or outcome):
            raise AdjudicationError("INDEPENDENT_IS_BLIND_WITHOUT_VERDICTS")
        rows = verdicts.get(LEVELS) if LEVELS in reading else None
        flat = {f: v for f, v in verdicts.items() if f != LEVELS}
        if phase == AFTER_REVEAL and (set(flat) != compared or not set(flat.values()) <= set(
                VERDICTS) or outcome or (LEVELS in reading and not (
                    isinstance(rows, dict) and set(rows) == {r["row"] for r in reading[LEVELS]}
                    and set(rows.values()) <= set(VERDICTS)))):
            raise AdjudicationError("VERDICT_FOR_EVERY_COMPARED_FIELD")

    def make(v: Verification) -> dict:
        recs, me = v.records, session.login
        if phase == INDEPENDENT and _of(recs, item_id, bundle.bundle_id, AFTER_REVEAL, me):
            raise AdjudicationError("SYSTEM_READING_ALREADY_SEEN")
        if phase == AFTER_REVEAL:
            mine = {r["seq"] for r in _of(recs, item_id, bundle.bundle_id, INDEPENDENT, me)}
            if (refers_to in mine) != bool(blind) or (not blind and refers_to is not None):
                raise AdjudicationError("AFTER_REVEAL_REFERS_TO_OWN_INDEPENDENT")
        if phase == APPROVAL:
            reviewed = {r["seq"]: r for r in _of(recs, item_id, bundle.bundle_id, AFTER_REVEAL)}
            if refers_to not in reviewed:
                raise AdjudicationError("APPROVAL_REFERS_TO_A_REVIEWED_RECORD")
            if _of([r for r in recs if r.get("phase") != APPROVAL], item_id, bundle.bundle_id,
                   login=me):
                raise AdjudicationError("APPROVER_IS_REVIEWER")
        return {"at": (now or datetime.now(UTC)).astimezone(UTC).isoformat(),
                "bundle_id": bundle.bundle_id, "item_id": item_id,
                "annotator": {"login": me, "name": session.name, "role": session.role,
                              "access": session.access},
                "phase": phase, "blind": bool(blind), "reading": reading,
                "field_verdicts": verdicts, "flags": flags, "confidence": confidence,
                "notes": notes, "refers_to": refers_to, "decision": outcome}

    try:
        return append_chained(annotations_path(base), "annotation log", ANNOTATION_SCHEMA, make)
    except RecommendationError as exc:
        raise AdjudicationError(exc.code) from None


# ------------------------------------------------------------------ ground truth


def resolve(records: list[dict], bundle: Bundle) -> list[dict]:
    """Per item: the approver's accepted or amended reading; else the latest AFTER_REVEAL
    reading, PROVISIONAL; with the INDEPENDENT reading it followed (anchoring)."""
    out = []
    for item_id, item in bundle.items.items():
        after = {r["seq"]: r for r in _of(records, item_id, bundle.bundle_id, AFTER_REVEAL)}
        if not after:
            continue
        approvals = _of(records, item_id, bundle.bundle_id, APPROVAL)
        last = approvals[-1] if approvals else None
        if last is not None and _outcome(last) in ("ACCEPT", "AMEND"):
            source, status = after[last["refers_to"]], ("APPROVED" if _outcome(last) ==
                                                          "ACCEPT" else "AMENDED")
            reading = merge_amendment(source["reading"], last["reading"])
        else:
            source = after[max(after)]
            status, reading = "PROVISIONAL", source["reading"]
        independent = next((r for r in records if r["seq"] == source["refers_to"]
                            and r.get("phase") == INDEPENDENT), None)
        out.append({"schema": GROUND_TRUTH_SCHEMA, "bundle_id": bundle.bundle_id,
                    "item_id": item_id, "kind": item["kind"], "set": item["set"],
                    "sequence_id": item["sequence_id"], "status": status, "reading": reading,
                    "field_verdicts": source["field_verdicts"], "blind": source["blind"],
                    "independent_reading": independent["reading"] if independent else None,
                    "reviewer": source["annotator"]["login"],
                    "approver": last["annotator"]["login"] if last else None,
                    "decision": _outcome(last) if last else None,
                    "source_seqs": {"independent": independent["seq"] if independent else None,
                                    "after_reveal": source["seq"],
                                    "approval": last["seq"] if last else None}})
    return out


def merge_amendment(reading: dict, amended: dict) -> dict:
    """The reviewed reading with the approver's amended fields; price-table rows by row."""
    out = {**reading, **{k: v for k, v in amended.items() if k != LEVELS}}
    if LEVELS in amended:
        rows = {r["row"]: r for r in reading.get(LEVELS, [])}
        rows.update({r["row"]: r for r in amended[LEVELS]})
        out[LEVELS] = list(rows.values())
    return out


def _outcome(record: dict) -> str | None:
    """An APPROVAL record's ACCEPT / AMEND / RETURN."""
    return record.get("decision")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def freeze(base: str | Path, now: datetime | None = None) -> tuple[Path, dict]:
    """Write the next ``versions/ground_truth_v<N>.jsonl`` and its manifest (never overwrites)."""
    base = Path(base).expanduser()
    bundle = load_bundle(base / "bundle", RESTRICTED)
    log = read_annotations(base)
    if not log.ok:
        raise AdjudicationError("ANNOTATION_LOG_FAILS_VERIFICATION")
    rows = resolve(log.records, bundle)
    versions = base / "versions"
    with PrivateDir(versions / "manifest", "versions folder", create=True) as d:
        taken = [int(m.group(1)) for n in os.listdir(d.fd)
                 if (m := re.fullmatch(r"ground_truth_v(\d+)\.jsonl", n))]
        n = max(taken, default=0) + 1
        data = "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n"
                       for r in rows).encode("utf-8")
        name = f"ground_truth_v{n}.jsonl"
        manifest = {"schema": GROUND_TRUTH_SCHEMA + ":manifest", "version": n, "file": name,
                    "sha256": _sha(data), "bundle_id": bundle.bundle_id,
                    "created_at": (now or datetime.now(UTC)).astimezone(UTC).isoformat(),
                    "annotation_log": {"count": log.count, "head_sha256": log.head},
                    "items": len(rows), "by_status": {s: sum(r["status"] == s for r in rows)
                                                      for s in ("APPROVED", "AMENDED",
                                                                "PROVISIONAL")}}
        d.create(name, data)
        d.create(f"ground_truth_v{n}.manifest.json",
                 json.dumps(manifest, indent=1, sort_keys=True).encode("utf-8") + b"\n")
    return versions / name, manifest


def verify_versions(base: str | Path) -> list[tuple[str, bool]]:
    """Each frozen version's file against the sha256 its manifest records."""
    versions = Path(base).expanduser() / "versions"
    out = []
    for m in sorted(versions.glob("ground_truth_v*.manifest.json")):
        manifest = json.loads(_read_private(m, "version manifest") or b"{}")
        data = _read_private(versions / str(manifest.get("file")), "frozen version")
        out.append((m.name, data is not None and _sha(data) == manifest.get("sha256")))
    return out


@dataclass
class Progress:
    done: int = 0
    total: int = 0
    sequences_done: int = 0
    sequences: int = 0
    status: dict[str, str] = field(default_factory=dict)  # item_id -> status


def progress(bundle: Bundle, records: list[dict], session: Session) -> Progress:
    """Per item for this session: a reviewer's OPEN / INDEPENDENT_SAVED / DONE; an approver's
    NOT_REVIEWED / AWAITING / RETURNED / DECIDED."""
    in_scope = [i["item_id"] for _, items in ordered(bundle, session.scope) for i in items]
    p = Progress(total=len(in_scope))
    for item_id in in_scope:
        if session.role == REVIEWER:
            phases = {r["phase"] for r in _of(records, item_id, bundle.bundle_id,
                                              login=session.login)}
            s = "DONE" if AFTER_REVEAL in phases else "INDEPENDENT_SAVED" if (
                INDEPENDENT in phases) else "OPEN"
        else:
            appr = _of(records, item_id, bundle.bundle_id, APPROVAL)
            s = ("NOT_REVIEWED" if not _of(records, item_id, bundle.bundle_id, AFTER_REVEAL)
                 else "AWAITING" if not appr else "RETURNED"
                 if _outcome(appr[-1]) == "RETURN" else "DECIDED")
        p.status[item_id] = s
        p.done += s in ("DONE", "DECIDED")
    scoped = [(sid, items) for sid, items in ordered(bundle, session.scope) if sid is not None]
    p.sequences = len(scoped)
    p.sequences_done = sum(all(p.status[i["item_id"]] in ("DONE", "DECIDED") for i in items)
                           for _sid, items in scoped)
    return p
