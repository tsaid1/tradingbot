"""Blind first, byte for byte (round 2, M3: the reviewer's three-database walk,
``HOSTED_REVIEWER_review_probes/test_r01_blind_differential.py`` extended by
``HOSTED_REVIEWER_review_round2_probes/test_q02_blind_differential.py``). Three databases whose
bundles differ ONLY in the system's reading (the demo's own; every field of it scrambled: label,
action, market, instrument, side, effect, timing, entry, stop, targets, amendment, ambiguities,
position, idea, levels, holds and mapping; none at all) must give a reviewer who has not passed
the reveal identical responses: every form page and its headers, filled forms, fragments, hostile
saves, the waiting post, the dashboard, the guide, the fields, every sequence's Start/Continue,
the sidebar, drafts (fragment, X-Draft-Kept, reopened, the no-script round trip) and refused
saves. Masked: the session's tokens, the page keys, the bundle id (the digest of files that
include the system's reading) and the wall-clock time of a save. INVENTED data."""

from __future__ import annotations

import json
import re

import hosted_pg
from hosted_web_site import FIRST, Site, scenario_reading

from tradingbot.runtime import adjudication as A

FRAGMENT = {"X-Fragment": "form"}
MARKS = ("QQSCRAMBLE", "QQFIELD", "QQFROM", "QQTO", "12345.5", "54321.25", "777.75", "999.5")


def _rewrite(base, change) -> None:
    path = base / "bundle" / "items.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    manifest = json.loads((base / "bundle" / "manifest.json").read_text())
    for k, row in enumerate(rows):
        change(k, row, rows, manifest["families"])
    path.write_text("".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n"
                            for r in rows))
    m = base / "bundle" / "manifest.json"
    data = json.loads(m.read_text())
    data["bundle_id"] = A.bundle_digest(base / "bundle")
    m.write_text(json.dumps(data, indent=1) + "\n")


def scramble(base) -> None:
    """Every field of the system's reading of every item changed, its holds and mapping too."""
    def change(k, row, rows, fams):
        fam = row["ai"].get("family")
        row["ai"] = {
            "post_label": "RETROSPECTIVE", "action": "REDUCE",
            "family": fams[(fams.index(fam) + 1) % len(fams)] if fam in fams else fams[0],
            "instrument_as_read": "QQSCRAMBLE instrument", "side": "SHORT", "effect": "REDUCES",
            "timing": "HISTORICAL", "explicit": False,
            "entry": {"price": 12345.5, "role": "HISTORICAL_FILL", "exactness": "EXACT"},
            "stop": {"price": 54321.25}, "targets": [{"price": 777.75}],
            "amendment": {"field": "QQFIELD", "from": "QQFROM", "to": "QQTO"},
            "ambiguities": ["QQSCRAMBLE_AMBIGUITY"],
            "position_context": {"side": "SHORT", "last_stop": 999.5},
            "idea_ref": rows[(k + 7) % len(rows)]["item_id"], "levels": []}
        row["holds"] = {"SHIPPED": {"status": "BLOCK", "reasons": ["QQSCRAMBLE_HOLD"]}}
        row["mapping"] = {"reference_series": "QQSCRAMBLE-REF",
                          "proposed_execution_instrument": "QQSCRAMBLE-EXEC",
                          "basis_treatment": "QQSCRAMBLE_BASIS", "approved": True}
    _rewrite(base, change)


def empty(base) -> None:
    """Nothing of the system: no reading, no holds, no mapping."""
    def change(_k, row, _rows, _fams):
        row["ai"], row["holds"], row["mapping"] = {}, {}, {}
    _rewrite(base, change)


def norm(text: str) -> str:
    text = re.sub(r'name="_key" value="[^"]*"', 'name="_key" value="K"', text)
    text = re.sub(r'name="_csrf" value="[^"]*"', 'name="_csrf" value="C"', text)
    text = re.sub(r"[A-Za-z0-9_-]{16,64}\.[0-9a-f]{32}", "KEY", text)
    text = re.sub(r"\b[0-9a-f]{64}\b", "H", text)
    # The wall-clock time of a save (the records carry the real time, not the test's clock).
    text = re.sub(r"saved \d{4}-\d\d-\d\d at \d\d:\d\d", "saved T", text)
    text = re.sub(r"\(\d{4}-\d\d-\d\d at \d\d:\d\d UTC\)", "(T)", text)
    text = re.sub(r"\d{1,2} [A-Z][a-z]{2} \d{4} at \d\d:\d\d UTC", "D at T UTC", text)
    return re.sub(r"(Submitted|Saved) at \d\d:\d\d", r"\1 at T", text)


def heads(r) -> str:
    return repr(sorted((k, v) for k, v in r.headers.items() if k != "Content-Length"))


def model(site) -> list[tuple[str, str]]:
    """The dashboard's and the sidebar's model and the rendered sidebar, as the server holds
    them for this reviewer now."""
    from flask import render_template
    from reviewer_web import progress as P

    row = site.reviewers[FIRST]
    records = site.admin.records_of(row)
    out = [("dashboard model", json.dumps(P.dashboard(site.bundle, row.session(), records),
                                          sort_keys=True))]
    for item in ("s14a", "s14b", "i07", None):
        nav = P.sidebar(site.bundle, row.session(), records, item)
        with site.app.test_request_context():
            out.append((f"sidebar {item}", render_template("_sidebar.html", nav=nav)))
        out.append((f"sidebar model {item}", json.dumps(nav, sort_keys=True)))
    return out


def walk(site) -> list[tuple[str, str]]:
    """Everything a blind reviewer can make the server say, as (label, text)."""
    out = []
    b = site.browser().sign_in(FIRST)
    out.append(("workspace", b.get("/").get_data(as_text=True)))
    out += model(site)
    for item in site.bundle.items:
        r = b.get(f"/form/{item}")
        out.append((f"get {item}", r.get_data(as_text=True)))
        out.append((f"headers {item}", heads(r)))
    # Forms filled completely, never saved (a single-form post would be revealed by a save).
    for item in ("i01", "i05", "i07", "s14a", "s22b", "s29a", "i09"):
        if item not in site.bundle.items:
            continue
        reading, form = scenario_reading(site.bundle, item)
        b.open(item)
        b.fill(reading)
        b.set({"confidence": form["confidence"], "flags": list(form["flags"]),
               "notes": form["notes"]})
        out.append((f"filled {item}", b.shown.text))
        frag = b.post(f"/form/{item}/state", b.shown.data(), headers=FRAGMENT)
        out.append((f"fragment {item} {frag.status_code}", frag.get_data(as_text=True)))
        out.append((f"fragment headers {item}", heads(frag)))
        hostile = [(n, A.AFTER_REVEAL if n == "_phase" else v) for n, v in b.shown.data()]
        hostile += [("reference_series_right", "YES"), ("execution_instrument_right", "NO"),
                    ("v.action", "CORRECT"), ("vt.action", "0" * 8)]
        ans = b.post(f"/form/{item}/save", hostile)
        out.append((f"hostile save {item} {ans.status_code}", ans.get_data(as_text=True)))
        frag = b.post(f"/form/{item}/state", hostile, headers=FRAGMENT)
        out.append((f"hostile fragment {item} {frag.status_code}", frag.get_data(as_text=True)))
        full = b.post(f"/form/{item}/state", hostile)
        out.append((f"hostile state page {item} {full.status_code}", full.get_data(as_text=True)))
    # Form 1 of a two-form post saved: the post waits for form 2.
    for first, second in (("s14a", "s14b"), ("s24a", "s24b")):
        b.review(first, after=False)
        out.append((f"saved {first}", b.shown.text))
        out.append((f"pending {first}", b.open(first).text))
        out.append((f"other {second}", b.open(second).text))
        for phase in (A.AFTER_REVEAL, A.INDEPENDENT):
            frag = b.post(f"/form/{first}/state", [("_csrf", b.csrf()), ("_phase", phase),
                                                   ("_key", "k" * 24)], headers=FRAGMENT)
            out.append((f"pending fragment {first} {phase} {frag.status_code}",
                        frag.get_data(as_text=True)))
        out.append((f"continue after {first}", b.get("/continue").headers.get("Location", "")))
    out += model(site)  # after the reviewer's own saves
    seqs = [sid or "-" for sid, _ in A.ordered(site.bundle, 40)]
    for path in ["/", "/guide", "/fields", "/continue", *[f"/sequence/{s}" for s in seqs],
                 "/sequence/not-a-sequence"]:
        r = b.get(path)
        out.append((f"GET {path} {r.status_code} {r.headers.get('Location')}",
                    r.get_data(as_text=True)))
        out.append((f"H {path}", heads(r)))
    # Drafts: a form filled through the round trip, not saved, then opened again.
    for item in ("i02", "i04", "s22a", "s29b", "i09"):
        if item not in site.bundle.items:
            continue
        reading, _form = scenario_reading(site.bundle, item)
        b.open(item)
        b.fill(reading)
        frag = b.post(f"/form/{item}/state", b.shown.data() + [("ui.later", "field-flags"),
                                                                ("ui.last", "field-notes")],
                      headers=FRAGMENT)
        out.append((f"draft fragment {item} {frag.status_code}", frag.get_data(as_text=True)))
        out.append((f"draft fragment H {item}", heads(frag)))
        again = b.get(f"/form/{item}")
        out.append((f"draft reopened {item}", again.get_data(as_text=True)))
        out.append((f"draft reopened H {item}", heads(again)))
        nojs = b.post(f"/form/{item}/state", b.shown.data())
        out.append((f"draft nojs {item} {nojs.status_code}", nojs.get_data(as_text=True)))
        out.append((f"dashboard with draft {item}", b.get("/").get_data(as_text=True)))
    # Refused saves: an empty form pressed Save (the summary; the draft kept on refusal).
    for item in ("i03", "s24a", "i06"):
        if item not in site.bundle.items:
            continue
        ans = b.post(f"/form/{item}/save", b.open(item).data())
        out.append((f"refused {item} {ans.status_code}", ans.get_data(as_text=True)))
        out.append((f"refused H {item}", heads(ans)))
        out.append((f"refused reopened {item}", b.get(f"/form/{item}").get_data(as_text=True)))
    out.append(("dashboard end", b.get("/").get_data(as_text=True)))
    out += model(site)
    assert all(r["phase"] == A.INDEPENDENT for r in site.records(FIRST))
    return out


def test_three_databases_that_differ_only_in_the_system_s_reading_look_the_same_blind(
        tmp_path):
    from reviewer_web.store import AdminStore

    walks = []
    for name, edit in (("plain", None), ("scrambled", scramble), ("empty", empty)):
        address = hosted_pg.fresh_database()
        admin = AdminStore(address)
        admin.init_schema()
        walks.append(walk(Site(tmp_path / name, admin, address, mark=False, edit=edit)))
    assert len({len(w) for w in walks}) == 1 and len(walks[0]) > 250
    differ = [rows[0][0] for rows in zip(*walks, strict=True)
              if len({r[0] for r in rows}) != 1 or len({norm(r[1]) for r in rows}) != 1]
    assert not differ, differ
    for label, text in walks[1]:
        assert not [m for m in MARKS if m in text], label
    # The market a row names is the one the reviewer read, once they saved a reading of it
    # (their own answer, never the system's): absent before the first save, then shown.
    labels = [label for label, _text in walks[0]]
    first_home = walks[0][labels.index("workspace")][1]
    later_home = walks[0][labels.index("dashboard end")][1]
    assert "in your saved readings: " not in first_home and \
        "in your saved readings: <strong>" in later_home
    assert "In progress" in later_home
