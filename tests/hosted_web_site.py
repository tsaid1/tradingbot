"""Helpers of the hosted web app's tests (``tests/test_hosted_web_*.py``): the site on INVENTED
data, and a browser's view of it.

``Site``: the application on a fresh database holding the demo bundle
(``scripts/adjudication_demo_bundle.py``) with two invented reviewers, an in-memory outbox and a
clock the test moves. The demo's system readings mostly equal what a reviewer answers, so two
forms get system values no reviewer ever types (``SYSTEM_MARKS``): a test can search a raw
response for them.

``Browser``: a Flask test client that behaves like a browser on the site: it keeps the cookie,
sends ``Origin``, reads the form a page shows (as a browser would submit it), changes answers
through the applicability round trip and saves. So the tests exercise the pages, not a
hand-written request.
"""

from __future__ import annotations

import html
import json
import re
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path

import pytest
from hosted_pg import demo_reviewers, dsn, publish_demo, store, write_demo  # noqa: F401
from werkzeug.datastructures import MultiDict

from tradingbot.runtime import adjudication as A
from tradingbot.runtime import adjudication_rubric as R
from tradingbot.runtime import adjudication_scenarios as SC
from tradingbot.runtime.vocab_recommend import encode_line

REPO = Path(__file__).resolve().parents[1]

ORIGIN = "https://reviewer.example.invalid"
SECRET = "an-invented-secret-for-tests-only-0123456789"
FIRST, SECOND = "REMOTE-2", "REMOTE-3"  # the demo file's two remote reviewers, by alias
# System values no reviewer answer, option or post text contains: (item, where, value).
SYSTEM_MARKS = {
    "s14a": {"instrument": "ZQSYS instrument alpha", "reference": "ZQSYS-REF-ALPHA",
             "execution": "ZQSYS-EXEC-ALPHA", "hold": "ZQSYS_HOLD_ALPHA",
             "basis": "ZQSYS_BASIS_ALPHA", "ambiguity": "ZQSYS_AMBIGUITY_ALPHA"},
    "s14b": {"instrument": "ZQSYS instrument beta", "reference": "ZQSYS-REF-BETA",
             "execution": "ZQSYS-EXEC-BETA", "hold": "ZQSYS_HOLD_BETA",
             "basis": "ZQSYS_BASIS_BETA", "ambiguity": "ZQSYS_AMBIGUITY_BETA"},
}
SYSTEM_WORDS = ("ZQSYS", "holds SHIPPED", "system-value", "proposal-value", "not read by")


def mark_system(base: Path) -> None:
    """Give the two forms of the demo's post s14 their unmistakable system values."""
    path = base / "bundle" / "items.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    for row in rows:
        marks = SYSTEM_MARKS.get(row["item_id"])
        if marks:
            row["ai"]["instrument_as_read"] = marks["instrument"]
            row["ai"]["ambiguities"] = [marks["ambiguity"]]
            row["holds"] = {"SHIPPED": {"status": "HOLD", "reasons": [marks["hold"]]}}
            row["mapping"] = {"reference_series": marks["reference"],
                              "proposed_execution_instrument": marks["execution"],
                              "basis_treatment": marks["basis"], "approved": False}
    path.write_text("".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n"
                            for r in rows))
    manifest = base / "bundle" / "manifest.json"
    data = json.loads(manifest.read_text())
    data["bundle_id"] = A.bundle_digest(base / "bundle")
    manifest.write_text(json.dumps(data, indent=1) + "\n")


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 11, 25, 9, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def forward(self, **delta) -> None:
        self.now += timedelta(**delta)


# ------------------------------------------------------------------ a browser's view


class _Form(HTMLParser):
    """The controls of ``<form id="reading-form">`` (or of the first form of the page), as a
    browser holds them."""

    def __init__(self, form_id: str | None):
        super().__init__(convert_charrefs=True)
        self.form_id, self.inside, self.depth = form_id, False, 0
        self.fields: list[list] = []  # [name, value, kind, on] in page order
        self.options: dict[str, list[str]] = {}
        self.action = ""
        self.disabled: set[str] = set()  # ids of disabled buttons
        self._select = self._textarea = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "form" and not self.inside and (self.form_id is None
                                                  or a.get("id") == self.form_id):
            self.inside, self.action = True, a.get("action", "")
            return
        if not self.inside:
            return
        name = a.get("name")
        if tag == "input" and name:
            kind = a.get("type", "text")
            on = "checked" in a if kind in ("radio", "checkbox") else True
            self.fields.append([name, a.get("value", ""), kind, on])
            if kind == "radio":
                self.options.setdefault(name, []).append(a.get("value", ""))
        elif tag == "select" and name:
            self._select = [name, None, "select", True]
            self.options[name] = []
            self.fields.append(self._select)
        elif tag == "option" and self._select is not None:
            self.options[self._select[0]].append(a.get("value", ""))
            if "selected" in a or self._select[1] is None:
                self._select[1] = a.get("value", "") if "selected" in a or self._select[
                    1] is None else self._select[1]
        elif tag == "textarea" and name:
            self._textarea = [name, "", "textarea", True]
            self.fields.append(self._textarea)
        elif tag == "button" and "disabled" in a:
            self.disabled.add(a.get("id", ""))

    def handle_endtag(self, tag):
        if tag == "form":
            self.inside = False
        elif tag == "select":
            self._select = None
        elif tag == "textarea":
            self._textarea = None

    def handle_data(self, data):
        if self._textarea is not None:
            self._textarea[1] += data


class Shown:
    """One response of the site: its status, its text, and the reading form it shows."""

    def __init__(self, response):
        self.response, self.status = response, response.status_code
        self.text = response.get_data(as_text=True)
        parser = _Form("reading-form")
        parser.feed(self.text)
        self._form = parser

    @property
    def has_form(self) -> bool:
        return bool(self._form.action)

    @property
    def names(self) -> set[str]:
        return {f[0] for f in self._form.fields}

    def value(self, name: str):
        """What a control holds: a list for a checkbox group, else a string, None when the page
        has no such control, "" for a radio group with nothing chosen."""
        found = [f for f in self._form.fields if f[0] == name]
        if not found:
            return None
        if found[0][2] == "checkbox":
            return [f[1] for f in found if f[3]]
        if found[0][2] == "radio":
            return next((f[1] for f in found if f[3]), "")
        return found[0][1]

    def data(self) -> list[tuple[str, str]]:
        """What a browser submits for this form."""
        return [(f[0], f[1] or "") for f in self._form.fields if f[3]]

    @property
    def plain(self) -> str:
        """The page's markup with its character references resolved (for texts with quotes)."""
        return html.unescape(self.text)

    def can_save(self) -> bool:
        return self.has_form and "save" not in self._form.disabled

    def visible(self) -> str:
        """The text of the page without its tags (enough for ``in`` checks)."""
        return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", self.text)))


def wanted(reading: dict) -> dict:
    """A reading's answers as the controls that hold them: control name -> value."""
    out: dict = {}
    for f, v in reading.items():
        if f == A.LEVELS:
            for row in v:
                for c in R.ROW_KEYS:
                    if row.get(c):
                        out[f"lv.{row['row']}.{c}"] = row[c]
        elif f == "additional_actions":
            out["aa.n"] = str(len(v))
            for j, x in enumerate(v):
                out.update({f"aa.{j}.{k}": x[k] for k in R.ADDITIONAL_KEYS})
        elif f in R.VALUE_SENTINELS:
            out[f"s.{f}"] = v if v in R.VALUE_SENTINELS[f] else "VALUE"
            if out[f"s.{f}"] == "VALUE":
                out[f] = v
        else:
            out[f] = v
    return out


def scenario_reading(bundle: A.Bundle, item_id: str) -> tuple[dict, dict]:
    """(the answers, the form) of a demo scenario item: a reading Save accepts."""
    for sc in SC.SCENARIOS:
        for k, form in enumerate(sc["forms"]):
            if SC.item_id(sc, k) == item_id:
                reading = dict(form["answers"])
                if form["kind"] == "INSTRUCTION":
                    keys = [key for key, _ in A.table_rows(bundle, bundle.items[item_id])]
                    reading[A.LEVELS] = [{"row": key, **row}
                                         for key, row in zip(keys, form["rows"], strict=False)]
                return reading, form
    from test_adjudication import valid  # the invented readings of the demo's first nine items

    return valid(bundle, item_id), {"confidence": "HIGH", "flags": [], "notes": ""}


class Browser:
    """A browser on the site: one cookie jar, ``Origin`` on every POST."""

    def __init__(self, site):
        self.site = site
        self.client = site.app.test_client()
        self.shown: Shown | None = None

    # -- plain requests

    def get(self, path: str, **kw):
        return self.client.get(path, base_url=ORIGIN, **kw)

    def post(self, path: str, data=None, *, origin: str | None = ORIGIN, headers=None, **kw):
        headers = dict(headers or {})
        if origin is not None:
            headers.setdefault("Origin", origin)
            headers.setdefault("Sec-Fetch-Site", "same-origin")
        return self.client.post(path, data=MultiDict(data or {}), base_url=ORIGIN,
                                headers=headers, **kw)

    # -- sign-in

    def sign_in(self, alias: str = FIRST):
        """Ask for a code, read it from the outbox, enter it. (The hourly limits are the
        subject of their own test: a test that signs in often starts each time without them.)"""
        login = self.site.reviewers[alias].login_shown
        self.site.sql("DELETE FROM rate_limit")
        assert self.post("/sign-in", {"address": login}).status_code == 200
        code = self.site.code()
        response = self.post("/sign-in/verify", {"address": login, "code": code})
        assert response.status_code == 303, response.status_code
        return self

    def csrf(self) -> str:
        text = self.get("/").get_data(as_text=True)
        return re.search(r'name="_csrf" value="([0-9a-f]{64})"', text).group(1)

    # -- one form

    def open(self, item_id: str) -> Shown:
        self.item_id = item_id
        self.shown = Shown(self.get(f"/form/{item_id}"))
        return self.shown

    def _send(self, action: str, changes: dict, **kw) -> Shown:
        data = [(n, v) for n, v in self.shown.data() if n not in changes]
        for name, value in changes.items():
            data += [(name, x) for x in (value if isinstance(value, list) else [value])]
        self.shown = Shown(self.post(f"/form/{self.item_id}/{action}", data, **kw))
        return self.shown

    def change(self, **changes) -> Shown:
        """Change controls and let the page refresh (the round trip, as without a script)."""
        return self._send("state", {k.replace("__", "."): v for k, v in changes.items()})

    def set(self, changes: dict) -> Shown:
        return self._send("state", changes)

    def fill(self, reading: dict) -> Shown:
        """Answer the form through its controls, one change per round trip, in page order (an
        answer can make the next control appear)."""
        want = wanted(reading)
        for _ in range(200):
            todo = next(((n, v) for n, v in want.items()
                         if n in self.shown.names and self.shown.value(n) != v), None)
            if todo is None:
                return self.shown
            self.set({todo[0]: todo[1]})
            assert self.shown.status == 200, self.shown.status
        raise AssertionError("the form did not settle")

    def save(self, *, confidence: str = "HIGH", flags=(), notes: str = "", ack: bool = True,
             follow: bool = True, extra: dict | None = None) -> Shown:
        """Choose the confidence (and flags, notes), tick the warnings when the page shows
        them, and press Save. Follows the redirect to the saved page."""
        changes = {"confidence": confidence, "flags": list(flags), "notes": notes}
        self.set(changes)
        if ack and "ack" in self.shown.names:
            ack_value = next(f[1] for f in self.shown._form.fields if f[0] == "ack")
            self.set({"ack": ack_value})
        return self.press_save(follow=follow, extra=extra)

    def press_save(self, *, follow: bool = True, extra: dict | None = None) -> Shown:
        shown = self._send("save", extra or {})
        if follow and shown.status == 303:
            self.shown = shown = Shown(self.get(shown.response.headers["Location"]))
        return shown

    def review(self, item_id: str, *, after: bool = True) -> None:
        """The scenario's reading of ``item_id``: independent, and (when the system's reading is
        revealed) after the reveal with the two mapping questions answered YES."""
        reading, form = scenario_reading(self.site.bundle, item_id)
        self.open(item_id)
        self.fill(reading)
        saved = self.save(confidence=form["confidence"], flags=form["flags"],
                          notes=form["notes"])
        assert "Saved." in saved.text, saved.visible()[:400]
        if after and saved.has_form:
            answers = {n: "YES" for n in R.MAPPING_FIELDS if n in saved.names}
            if answers:
                self.set(answers)
            done = self.save(confidence=form["confidence"], flags=form["flags"],
                             notes=form["notes"])
            assert "Saved." in done.text, done.visible()[:600]


# ------------------------------------------------------------------ the site


class Site:
    """The application on a fresh database with the invented demo bundle published and its two
    remote reviewers listed. ``browser()`` gives a new browser (a new session)."""

    def __init__(self, tmp_path: Path, admin, dsn: str, *, site_env: str = "test",  # noqa: F811
                 mark: bool = True, edit=None):
        from reviewer_web import app as web
        from reviewer_web import mail
        from reviewer_web.store import Store

        base = write_demo(tmp_path / "demo")
        if mark:
            mark_system(base)
        if edit:
            edit(base)
        self.admin, self.dsn = admin, dsn
        self.bundle = publish_demo(admin, base)
        demo_reviewers(admin, base)
        self.store = Store(dsn)  # the web app's own object: no operator call
        self.outbox, self.clock = mail.Outbox(), Clock()
        self.config = {"SESSION_SECRET": SECRET, "SITE_ENV": site_env, "CLOCK": self.clock,
                       "SIGN_IN_FLOOR_SECONDS": 0}
        self.app = web.create_app(self.store, self.outbox, self.config)

    @property
    def reviewers(self) -> dict:
        return {r.alias: r for r in self.admin.reviewers()}

    def browser(self) -> Browser:
        return Browser(self)

    def code(self) -> str:
        """The code of the newest message in the outbox."""
        return re.search(r"\b(\d{6})\b", self.outbox.sent[-1][2]).group(1)

    def records(self, alias: str, *, markers: bool = True) -> list[dict]:
        rows = self.admin.records_of(self.reviewers[alias])
        return [r for r in rows if markers or r["phase"] != A.SYSTEM_SHOWN]

    def set_reviewer(self, alias: str, **changes) -> None:
        """Change a reviewer's row (role, access, scope_sequences), as the operator tool does."""
        row = self.reviewers[alias]
        kw = {"name": row.name, "role": row.role, "access": row.access,
              "scope_sequences": row.scope_sequences, **changes}
        self.admin.upsert_reviewer(alias, row.login_shown, **kw)

    def sql(self, statement: str, *values) -> list:
        with self.admin.connection() as conn:
            cur = conn.execute(statement, values)
            return cur.fetchall() if cur.description else []

    def return_reading(self, alias: str, item_id: str, note: str = "") -> dict:
        """The approver's RETURN of the reviewer's latest reading after the reveal, stored for
        THAT reviewer as the operator tool stores it (origin RETURN, outside the hosted chain)."""
        from reviewer_web.store import EventLine

        reviewer = self.reviewers[alias]
        after = [r for r in self.records(alias) if r["item_id"] == item_id
                 and r["phase"] == A.AFTER_REVEAL][-1]
        seq = 2_000_001 + len(self.sql("SELECT 1 FROM event WHERE origin = 'RETURN'"))
        record = {"schema": A.ANNOTATION_SCHEMA, "seq": seq, "prev_sha256": "0" * 64,
                  "at": self.clock().isoformat(), "bundle_id": self.bundle.bundle_id,
                  "item_id": item_id,
                  "annotator": {"login": "owner@example.invalid", "name": "Owner Example",
                                "role": A.APPROVER, "access": A.FULL},
                  "phase": A.APPROVAL, "blind": False, "reading": {}, "field_verdicts": {},
                  "flags": [], "confidence": "", "notes": note, "refers_to": after["seq"],
                  "decision": "RETURN", "rubric": A.RUBRIC, "acknowledged_warnings": []}
        self.admin.insert_events([EventLine(reviewer.id, "RETURN", encode_line(record))])
        return record


@pytest.fixture
def site(tmp_path, store, dsn) -> Site:  # noqa: F811
    return Site(tmp_path, store, dsn)
