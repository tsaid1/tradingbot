"""Write a small adjudication bundle of INVENTED posts, for trying the workspace without real data.

    python scripts/adjudication_demo_bundle.py DIR

Writes, into DIR (created, must be empty, outside this repository):
``bundle/`` (manifest, posts, sequences, items, one invented image) and a
``reviewers.toml`` with three invented logins (``owner@example.invalid`` APPROVER,
``reviewer@example.invalid`` REVIEWER, ``remote@example.invalid`` REVIEWER) and
``remote_full_context_authorized = false``. Every word, price and market below is invented.
Then start the workspace with ``TRADINGBOT_ADJUDICATION_DIR=DIR`` (docs/ADJUDICATION_WORKSPACE.md).
"""
from __future__ import annotations

import hashlib
import json
import re
import struct
import sys
import zlib
from pathlib import Path

from tradingbot.runtime import adjudication as A
from tradingbot.runtime.vocab_review import PrivateDir, PrivatePathError, check_private_path

T0 = "2026-03-02T14:{:02d}:00+00:00"


def _png() -> bytes:
    """A 16x8 invented 'chart': a diagonal line."""
    rows = b"".join(b"\x00" + bytes(255 if x == 2 * y or x == 2 * y + 1 else 0 for x in range(16))
                    for y in range(8))

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(
            ">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 16, 8, 8, 0, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


POSTS = [
    ("p01", 1, "Taking a starter long in TLT here near 88.40. Stop 87.20, first target 91.",
     ["chart-p01.png"]),
    ("p02", 9, "Quiet tape today, the bond crowd seems asleep.", []),
    ("p03", 14, "Adding to the TLT long at 88.10, same stop.", []),
    ("p04", 21, "Raising the TLT stop to 88.00 to protect the position.", []),
    ("p05", 33, "Out of the rest of TLT at 90.60.", []),
    ("p06", 40, "Recap: the TLT idea from this week closed for a gain near 2.2 points.", []),
    ("p07", 44, "Short Zed Metals around 412, stop 420.", []),
    ("p08", 51, "Covered the Zed Metals short at 404.", []),
    ("p09", 55, "Long weekend ahead, back on Tuesday.", []),
]


NA = "NOT_APPLICABLE"


def lv(n, price, role, exactness="EXACT", month=NA) -> dict:
    return {"n": n, "price": price, "role": role, "exactness": exactness,
            "contract_month": month}


def _numbers(text: str) -> list[dict]:
    """A stand-in for the product's number reader: every number of the text, in order."""
    return [{"n": k, "token": t} for k, t in enumerate(re.findall(r"\d+(?:\.\d+)?", text))]


def _ai(**kw) -> dict:
    base = {"post_label": "ACTIONABLE", "action": "NONE", "family": None,
            "instrument_as_read": None, "side": "NONE", "effect": "NONE", "timing": "CURRENT",
            "explicit": True, "levels": [], "entry": {}, "stop": {}, "targets": [],
            "amendment": {},
            "idea_ref": None, "ambiguities": [], "position_context": {"state": "FLAT"}}
    return {**base, **kw}


TLT_MAP = {"reference_series": "TLT", "proposed_execution_instrument": "TLT ETF",
           "basis_treatment": "DIRECT", "approved": True}
OTHER_MAP = {"reference_series": "", "proposed_execution_instrument": "",
             "basis_treatment": "UNSUPPORTED", "approved": False}
HELD = {"SHIPPED": {"status": "SENT", "reasons": []},
        "FIVE_FAMILY": {"status": "SENT", "reasons": []}}


def _items() -> list[dict]:
    open_tlt = {"state": "OPEN", "side": "LONG", "last_stop": 87.2}
    return [
        {"item_id": "i01", "kind": "INSTRUCTION", "sequence_id": "seqA", "set": "DESIGN",
         "post_ref": "p01", "slot": 0, "order_in_sequence": 1, "context_refs": [],
         "ai": _ai(action="OPEN", family="TLT", instrument_as_read="TLT", side="LONG",
                   effect="OPENS", entry={"price": 88.4, "role": "INTENDED_ENTRY",
                                          "exactness": "APPROXIMATE"},
                   stop={"price": 87.2}, targets=[{"price": 91}],
                   levels=[lv(0, 88.4, "INTENDED_ENTRY", "APPROXIMATE"), lv(1, 87.2, "STOP"),
                           lv(2, 91, "TARGET")]),
         "holds": HELD, "mapping": TLT_MAP},
        {"item_id": "i02", "kind": "LABEL_ONLY", "sequence_id": "seqA", "set": "DESIGN",
         "post_ref": "p02", "slot": None, "order_in_sequence": None, "context_refs": ["p01"],
         "ai": _ai(post_label="COMMENTARY", timing=None, explicit=None,
                   position_context=open_tlt), "holds": {}, "mapping": {}},
        {"item_id": "i03", "kind": "INSTRUCTION", "sequence_id": "seqA", "set": "DESIGN",
         "post_ref": "p03", "slot": 0, "order_in_sequence": 2, "context_refs": ["p01", "p02"],
         "ai": _ai(action="ADD", family="TLT", instrument_as_read="TLT", side="LONG",
                   effect="INCREASES", entry={"price": 88.1, "role": "INTENDED_ENTRY",
                                              "exactness": "EXACT"},
                   stop={"price": 87.2}, idea_ref="i01", position_context=open_tlt,
                   levels=[lv(0, 88.1, "INTENDED_ENTRY")]),
         "holds": HELD, "mapping": TLT_MAP},
        {"item_id": "i04", "kind": "INSTRUCTION", "sequence_id": "seqA", "set": "DESIGN",
         "post_ref": "p04", "slot": 0, "order_in_sequence": 3,
         "context_refs": ["p01", "p02", "p03"],
         # The invented interpreter misreads the new stop (87.00) and types the level as a
         # current market observation: each is a correction on its own.
         "ai": _ai(action="MODIFY", family="TLT", instrument_as_read="TLT", side="LONG",
                   effect="AMENDS_PROTECTION", stop={"price": 87.0},
                   amendment={"field": "STOP", "from": "87.2", "to": "87.0"}, idea_ref="i03",
                   position_context=open_tlt, levels=[lv(0, 88, "CURRENT_MARKET")]),
         "holds": HELD, "mapping": TLT_MAP},
        {"item_id": "i05", "kind": "INSTRUCTION", "sequence_id": "seqA", "set": "DESIGN",
         "post_ref": "p05", "slot": 0, "order_in_sequence": 4,
         "context_refs": ["p01", "p03", "p04"],
         "ai": _ai(action="CLOSE", family="TLT", instrument_as_read="TLT", side="LONG",
                   effect="CLOSES", entry={"price": 90.6, "role": "CURRENT_MARKET",
                                           "exactness": "EXACT"}, idea_ref="i04",
                   levels=[lv(0, 90.6, "HISTORICAL_FILL")],
                   position_context={"state": "OPEN", "side": "LONG", "last_stop": 88.0}),
         "holds": HELD, "mapping": TLT_MAP},
        {"item_id": "i06", "kind": "INSTRUCTION", "sequence_id": "seqA", "set": "DESIGN",
         "post_ref": "p06", "slot": 0, "order_in_sequence": 5, "context_refs": ["p05"],
         # A retrospective exit report: action CLOSE with label RETROSPECTIVE.
         "ai": _ai(post_label="RETROSPECTIVE", action="CLOSE", family="TLT",
                   instrument_as_read="TLT", side="LONG", effect="CLOSES",
                   timing="HISTORICAL", idea_ref="seqA", ambiguities=["RETROSPECTIVE_REPORT"]),
         "holds": {"SHIPPED": {"status": "NO_TRADE", "reasons": ["RETROSPECTIVE"]}},
         "mapping": TLT_MAP},
        {"item_id": "i07", "kind": "INSTRUCTION", "sequence_id": "seqB", "set": "CHECK",
         "post_ref": "p07", "slot": 0, "order_in_sequence": 1, "context_refs": [],
         "ai": _ai(action="OPEN", family="OTHER", instrument_as_read="Zed Metals",
                   side="SHORT", effect="OPENS",
                   entry={"price": 412, "role": "CURRENT_MARKET",
                          "exactness": "APPROXIMATE"}, stop={"price": 420},
                   levels=[lv(0, 412, "CURRENT_MARKET", "APPROXIMATE", "NOT_STATED"),
                           lv(1, 420, "STOP", "EXACT", "NOT_STATED"),
                           lv(None, 415, "CHART_LEVEL", "APPROXIMATE", "NOT_STATED")]),
         "holds": {"SHIPPED": {"status": "HOLD", "reasons": ["UNSUPPORTED_INSTRUMENT_MAPPING"]},
                   "FIVE_FAMILY": {"status": "HOLD",
                                   "reasons": ["UNSUPPORTED_INSTRUMENT_MAPPING"]}},
         "mapping": OTHER_MAP},
        {"item_id": "i08", "kind": "INSTRUCTION", "sequence_id": "seqB", "set": "CHECK",
         "post_ref": "p08", "slot": 0, "order_in_sequence": 2, "context_refs": ["p07"],
         "ai": _ai(action="CLOSE", family="OTHER", instrument_as_read="Zed Metals",
                   side="SHORT", effect="CLOSES", entry={"price": 404,
                                                         "role": "CURRENT_MARKET",
                                                         "exactness": "EXACT"},
                   idea_ref="i07", levels=[lv(0, 404, "HISTORICAL_FILL", "EXACT", "NOT_STATED")],
                   position_context={"state": "OPEN", "side": "SHORT", "last_stop": 420}),
         "holds": {"SHIPPED": {"status": "HOLD", "reasons": ["UNSUPPORTED_INSTRUMENT_MAPPING"]}},
         "mapping": OTHER_MAP},
        {"item_id": "i09", "kind": "LABEL_ONLY", "sequence_id": None, "set": "CHECK",
         "post_ref": "p09", "slot": None, "order_in_sequence": None, "context_refs": [],
         "ai": _ai(post_label="COMMENTARY", timing=None, explicit=None), "holds": {},
         "mapping": {}},
    ]


def _jsonl(rows) -> bytes:
    return "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n"
                   for r in rows).encode("utf-8")


def bundle_digest(files: dict[str, bytes]) -> str:
    """The specification's recipe (1.3), as ``adjudication.bundle_digest`` computes it from the
    files: sha256 over ``<posix path>\\0<sha256 hex>\\n`` per file but the manifest, sorted."""
    lines = "".join(f"{n}\0{hashlib.sha256(d).hexdigest()}\n" for n, d in sorted(files.items()))
    return hashlib.sha256(lines.encode("utf-8")).hexdigest()


REVIEWERS = """remote_full_context_authorized = false
scope_sequences = 20

[[reviewer]]
login = "owner@example.invalid"
name = "Owner Example"
role = "APPROVER"
access = "FULL"

[[reviewer]]
login = "reviewer@example.invalid"
name = "Reviewer Example"
role = "REVIEWER"
access = "FULL"

[[reviewer]]
login = "remote@example.invalid"
name = "Remote Reviewer Example"
role = "REVIEWER"
access = "FULL"
"""


def write_demo(out: str | Path) -> Path:
    out = Path(out).expanduser()
    try:  # before anything is created: never inside this repository
        check_private_path(out / "bundle" / "manifest.json", "demo bundle")
        check_private_path(out / "reviewers.toml", "demo reviewers file")
    except PrivatePathError:
        raise SystemExit("refused: the directory lies inside this program's repository") from None
    if out.exists() and any(out.iterdir()):
        raise SystemExit("refused: the directory is not empty")
    out.mkdir(mode=0o700, parents=True, exist_ok=True)
    posts = [{"post_ref": r, "published_at": T0.format(m), "text": t, "media": media,
              "revision": 0, "reply_to": None, "quotes": None, "numbers": _numbers(t)}
             for r, m, t, media in POSTS]
    sequences = [{"sequence_id": "seqA", "set": "DESIGN",
                  "post_refs": ["p01", "p02", "p03", "p04", "p05", "p06"], "review_order": 2},
                 {"sequence_id": "seqB", "set": "CHECK", "post_refs": ["p07", "p08"], "review_order": 1}]
    files = {"posts.jsonl": _jsonl(posts), "sequences.jsonl": _jsonl(sequences),
             "items.jsonl": _jsonl(_items()), "media/chart-p01.png": _png()}
    manifest = {"schema": A.BUNDLE_SCHEMA, "bundle_id": bundle_digest(files),
                "created_at": "2026-11-14T00:00:00+00:00", "product_commit": "demo",
                "families": ["TLT"],
                "counts": {"sequences": 2, "posts": len(posts), "media_files": 1,
                           "items": {"INSTRUCTION": 7, "LABEL_ONLY": 2}}}
    files["manifest.json"] = json.dumps(manifest, indent=1).encode("utf-8") + b"\n"
    for name, data in files.items():
        with PrivateDir(out / "bundle" / name, "demo bundle file", create=True) as d:
            d.create(d.name, data)
    with PrivateDir(out / "reviewers.toml", "demo reviewers file", create=True) as d:
        d.create(d.name, REVIEWERS.encode("utf-8"))
    return out


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    print(f"invented demo bundle written to {write_demo(sys.argv[1])}")
