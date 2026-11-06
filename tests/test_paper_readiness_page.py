"""OPS-03 regression tests (issue #22): the paper-readiness operator page is OFFLINE, FAKE-only, redacted and
honest about unknowns.

Synthetic inputs only. Covers the headless rendering of ``dashboard/pages/4_Paper_Readiness.py`` through
``streamlit.testing.v1.AppTest`` and compares what it shows with the merged runner's canonical report. The
diagnostics themselves are covered by tests/test_paper_readiness.py and are not mirrored here.
"""

from __future__ import annotations

import ast
import http.client
import re
import socket
import tempfile
import urllib.request
from hashlib import sha256
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

import tradingbot
from tradingbot.paper_readiness import scenarios as sc
from tradingbot.paper_readiness.diagnostics import FindingKind, ReadinessOutcome, Stage
from tradingbot.paper_readiness.observations import (
    AccountSummaryObservation,
    MarketDataType,
    PositionLine,
    ReadinessExpectation,
    ResponseKind,
)
from tradingbot.paper_readiness.runner import render_json

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"
PAGE = REPO / "dashboard" / "pages" / "4_Paper_Readiness.py"
DOCS = REPO / "docs" / "PAPER_READINESS.md"
VIEW_DOCS = REPO / "docs" / "OFFLINE_PAPER_READINESS_VIEW.md"
CONTROL = "tlt_regular_session_control"
STATE_KEY = "paper_readiness_last_run"
RADIO_KEY = "paper_readiness_scenario"
READ_STAGES = (Stage.CONTRACT, Stage.QUOTE, Stage.ACCOUNT, Stage.POSITIONS)
GAP_KINDS = {
    FindingKind.MISSING,
    FindingKind.INCOMPLETE,
    FindingKind.AMBIGUOUS,
    FindingKind.UNENTITLED,
}
# Raw synthetic values from scenarios.py that no page element may ever show (the same list the core test
# applies to the renderers, plus the invented conIds and the position symbol).
RAW_SYNTHETIC = (
    sc.SYNTH_ACCOUNT,
    sc.OTHER_SYNTH_ACCOUNT,
    "1000000",
    "4000000",
    "100.00",
    "100.02",
    "100.01",
    "50.00",
    "SYNTHETIC TLT PLACEHOLDER",
    "SYNTH-LISTING",
    "SYNTH-OTHER-LISTING",
    "ZZQ",
    "900001",
    "900002",
    "900003",
)
INPUT_WIDGETS = (
    "text_input",
    "number_input",
    "text_area",
    "selectbox",
    "multiselect",
    "checkbox",
    "toggle",
    "slider",
    "select_slider",
    "file_uploader",
    "download_button",
    "form",
    "form_submit_button",
    "chat_input",
    "date_input",
    "time_input",
    "datetime_input",
    "color_picker",
    "data_editor",
    "camera_input",
    "audio_input",
    "link_button",
    "page_link",
    "pills",
    "segmented_control",
    "feedback",
    "button_group",
)
# Scenario -> (outcome, headline, reads dispatched, page text that must be present).
REPRESENTATIVE = {
    "environment_unknown_port_7497": (
        "REFUSED_BEFORE_DISPATCH",
        "REFUSED",
        0,
        (
            "no read was dispatched",
            "| CONTRACT | NOT DISPATCHED (unknown) |",
            "`INFO` **SOCKET_PORT_NOT_EVIDENCE**",
            "`REFUSED` **ENVIRONMENT_UNKNOWN**",
            "| SESSION | NOT ASSESSED (refused before dispatch) |",
        ),
    ),
    "session_disconnected": (
        "REFUSED_BEFORE_DISPATCH",
        "REFUSED",
        0,
        (
            "`REFUSED` **SESSION_NOT_CONNECTED**",
            "`REFUSED` **SESSION_CONNECTIVITY_LOST**",
            "Not observed in this scenario (unknown, not failed): CONTRACT, QUOTE, ACCOUNT, POSITIONS.",
        ),
    ),
    "contract_identity_ambiguous": (
        "NOT_READY",
        "INCOMPLETE",
        3,
        (
            "`AMBIGUOUS` **CONTRACT_IDENTITY_AMBIGUOUS**",
            "`INCOMPLETE` **QUOTE_SKIPPED_NO_CONTRACT_IDENTITY**",
            "| QUOTE | NOT DISPATCHED (unknown) |",
            "Not observed in this scenario (unknown, not failed): QUOTE.",
        ),
    ),
    "contract_not_found": (
        "NOT_READY",
        "ERROR",
        3,
        ("`ERROR` **CONTRACT_NOT_FOUND**", "Broker message 200"),
    ),
    "quote_stale": ("NOT_READY", "STALE", 4, ("`STALE` **QUOTE_STALE**",)),
    "quote_delayed": ("NOT_READY", "DELAYED", 4, ("`DELAYED` **QUOTE_DELAYED**",)),
    "entitlement_unavailable": (
        "NOT_READY",
        "UNENTITLED",
        4,
        ("`UNENTITLED` **ENTITLEMENT_UNAVAILABLE**", "`ENTITLEMENT_UNAVAILABLE` (UNENTITLED)"),
    ),
    "account_summary_incomplete": (
        "NOT_READY",
        "INCOMPLETE",
        4,
        (
            "`INCOMPLETE` **ACCOUNT_SUMMARY_END_MISSING**",
            "| ACCOUNT | read dispatched (request 1003) | 1 | NO |",
        ),
    ),
    "outside_regular_session": (
        "NOT_READY",
        "OUT_OF_SCOPE",
        4,
        ("`OUT_OF_SCOPE` **OUTSIDE_REGULAR_SESSION**",),
    ),
    "responses_uncorrelated": (
        "NOT_READY",
        "AMBIGUOUS",
        4,
        ("`AMBIGUOUS` **UNCORRELATED_RESPONSE_IGNORED**",),
    ),
    "responses_duplicated": (
        "CONTROL_COMPLETE",
        "INFO",
        4,
        ("`INFO` **DUPLICATE_RESPONSE_DROPPED**", "never real broker readiness"),
    ),
}


# ----------------------------------------------------------------------------- helpers


def page(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AppTest:
    """A fresh headless page whose cwd, temp directory and forbidden DB path are task-local, so any write
    the page attempted would be visible to `footprint`."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    monkeypatch.setenv("TRADINGBOT_DB", str(tmp_path / "forbidden.sqlite3"))
    return AppTest.from_file(str(PAGE), default_timeout=60)


def footprint(tmp_path: Path) -> set[Path]:
    return {p.relative_to(tmp_path) for p in tmp_path.rglob("*")}


def deny_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every outbound connection primitive raises for the rest of the test (AppTest's own event loop only
    needs a local socketpair, which stays available)."""

    def deny(*args, **kwargs):
        raise AssertionError("the page attempted a network connection")

    for target, name in (
        (socket, "create_connection"),
        (socket, "getaddrinfo"),
        (socket.socket, "connect"),
        (socket.socket, "connect_ex"),
        (urllib.request, "urlopen"),
        (http.client.HTTPConnection, "connect"),
    ):
        monkeypatch.setattr(target, name, deny)


def shown(at: AppTest) -> str:
    """Everything the page displays, from every element type it could use, sidebar and expanders included."""
    parts: list[str] = []
    for kind in (
        "title",
        "header",
        "subheader",
        "error",
        "warning",
        "info",
        "success",
        "markdown",
        "caption",
        "text",
        "code",
        "json",
    ):
        parts += [str(e.value) for e in at.get(kind)]
    parts += [f"{m.label} {m.value}" for m in at.metric]
    parts += (
        [e.label for e in at.expander] + [b.label for b in at.button] + [r.label for r in at.radio]
    )
    parts += [str(option) for r in at.radio for option in r.options]
    parts += [t.value.to_string() for t in at.table] + [d.value.to_string() for d in at.dataframe]
    parts += [str(getattr(e, "value", e)) for e in at.exception]
    return "\n".join(parts)


def without_digests(text: str) -> str:
    """Mask the hex digests the page legitimately shows, so numeric sentinels are tested against prose only."""
    return re.sub(
        r"SYNTH-\.\.\.[0-9a-f]{8}", "<account_ref>", re.sub(r"\b[0-9a-f]{64}\b", "<digest>", text)
    )


def finding_lines(report) -> list[str]:
    return [
        f"`{f.kind.value}` **{f.code}**"
        + (f" (request {f.request_id})" if f.request_id is not None else "")
        + f": {f.detail}"
        for f in report.findings
    ]


def metrics(at: AppTest) -> dict[str, str]:
    return {m.label: str(m.value) for m in at.metric}


def expected_metrics(report) -> dict[str, str]:
    non_info = sum(n for kind, n in report.counts.items() if kind != FindingKind.INFO.value)
    return {
        "Outcome": report.outcome.value,
        "Headline finding kind": report.headline.value,
        "Reads dispatched": f"{len(report.dispatched)} of 4",
        "Non-INFO findings": str(non_info),
    }


def assert_matches_runner(at: AppTest, name: str) -> None:
    """The page shows exactly the runner's canonical report for `name`: outcome, headline, dispatch count,
    every finding line, every observation digest and the byte-identical render_json digest."""
    report = sc.run_scenario(sc.SCENARIOS[name])
    text = shown(at)
    assert not at.exception
    assert metrics(at) == expected_metrics(report)
    for line in finding_lines(report):
        assert line in text, line
    for digest in report.observations:
        assert (
            f"| {digest.stage.value} | {digest.request_id} | {digest.rows} | " in text
            and digest.digest in text
        )
    assert f"sha256:{sha256(render_json(report).encode('utf-8')).hexdigest()}" in text
    assert f"account_ref `{report.account_ref}`" in text and report.notice in text
    assert f"as_of {report.as_of.isoformat()} (**FAKE clock**" in text
    assert "trading_permission = False" in text and "connected capability" in text.lower()
    assert "as scripted" in text and "UNEXPECTED" not in text and "NOT all present" not in text
    assert not at.success  # nothing on this page is ever rendered as a success
    masked = without_digests(text)
    for raw in RAW_SYNTHETIC:
        assert raw not in masked, raw
    assert at.session_state[STATE_KEY]["scenario"] == name


# ----------------------------------------------------------------------------- provenance and guards


def test_import_origin_is_this_checkout():
    assert Path(tradingbot.__file__).resolve().parents[2] == REPO
    assert Path(sc.__file__).resolve() == SRC / "tradingbot" / "paper_readiness" / "scenarios.py"
    assert PAGE.is_file() and DOCS.is_file() and VIEW_DOCS.is_file()


def test_page_source_imports_only_the_report_surface_and_touches_no_network_environment_clock_or_payload():
    source = PAGE.read_text(encoding="utf-8")
    tree = ast.parse(source)
    modules: set[str] = set()
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            modules.add(node.module or "")
            names |= {alias.name for alias in node.names}
    assert modules == {
        "__future__",
        "hashlib",
        "pathlib",
        "streamlit",
        "tradingbot",
        "tradingbot.paper_readiness.diagnostics",
        "tradingbot.paper_readiness.runner",
        "tradingbot.paper_readiness.scenarios",
    }, modules
    assert names == {
        "annotations",
        "sha256",
        "Path",
        "HEADLINE_ORDER",
        "Finding",
        "FindingKind",
        "ReadinessOutcome",
        "ReadinessReport",
        "Stage",
        "render_json",
        "SCENARIOS",
        "run_scenario",
    }, names

    # Every attribute the page calls, on streamlit or anything else: only output elements, the two inputs
    # and report accessors. No input widget, file, environment, clock, connection or payload accessor.
    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    on_st = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "st"
    }
    assert on_st <= {
        "set_page_config",
        "title",
        "error",
        "caption",
        "header",
        "radio",
        "button",
        "subheader",
        "markdown",
        "warning",
        "info",
        "columns",
        "expander",
        "divider",
        "stop",
    }, on_st
    forbidden_calls = set(INPUT_WIDGETS) | {
        "connection",
        "secrets",
        "query_params",
        "write",
        "write_text",
        "write_bytes",
        "open",
        "mkdir",
        "touch",
        "unlink",
        "rename",
        "now",
        "today",
        "utcnow",
        "time",
        "getenv",
        "environ",
        "connect",
        "model_dump",
        "model_dump_json",
        "attest_session",
        "request_quote_snapshot",
        "request_contract_details",
        "request_account_summary",
        "request_positions",
    }
    assert not called & forbidden_calls, called & forbidden_calls
    attributes = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    assert not attributes & {
        "script",
        "expectation",
        "clock_start",
        "environ",
        "argv",
        "secrets",
        "query_params",
    }
    for token in (
        sc.SYNTH_ACCOUNT,
        "SYNTH-PAPER",
        "FakeScript",
        "FakeReadOnlyTransport",
        "FakeClock",
        "Transport",
        "build_transport",
        "run_readiness_check",
        "control_script",
        "EXPECTATION",
        "TLT_TARGET",
        "datetime",
        "getenv",
        "open(",
        "subprocess",
        "socket",
        "urllib",
        "requests",
        "http",
    ):
        assert token not in source, token


def test_page_widgets_are_only_the_scenario_radio_and_the_rerun_button(tmp_path, monkeypatch):
    at = page(tmp_path, monkeypatch).run()
    assert not at.exception
    assert [r.label for r in at.radio] == ["Scenario"]
    assert at.radio[0].value == CONTROL and len(at.radio[0].options) == len(sc.SCENARIOS)
    assert [str(o).split(" · ")[0] for o in at.radio[0].options] == list(sc.SCENARIOS)
    assert all(" · scripted " in str(o) for o in at.radio[0].options)
    assert [b.label for b in at.button] == ["Re-run selected scenario (deterministic)"]
    for widget in INPUT_WIDGETS:
        assert not list(getattr(at, widget, [])), widget
    assert at.session_state[RADIO_KEY] == CONTROL


# ----------------------------------------------------------------------------- positive control


def test_positive_control_renders_the_canonical_redacted_report_without_green_checks(
    tmp_path, monkeypatch
):
    at = page(tmp_path, monkeypatch).run()
    assert_matches_runner(at, CONTROL)
    report = sc.run_scenario(sc.SCENARIOS[CONTROL])
    assert (
        report.outcome == ReadinessOutcome.CONTROL_COMPLETE
    )  # the scenario really is the positive control
    assert at.title[0].value == "Paper Readiness (OFFLINE / FAKE ONLY)"
    banner = at.error[0].value
    assert banner.startswith("OFFLINE / FAKE ONLY") and "Connected capability is UNKNOWN" in banner
    assert "trading_permission = False" in banner and "No broker, network, credential" in banner
    assert (
        sum("OFFLINE / FAKE ONLY" in e.value for e in at.error) == 2
    )  # main banner and sidebar banner
    assert metrics(at) == {
        "Outcome": "CONTROL_COMPLETE",
        "Headline finding kind": "INFO",
        "Reads dispatched": "4 of 4",
        "Non-INFO findings": "0",
    }
    outcome = [w.value for w in at.warning if w.value.startswith("CONTROL_COMPLETE:")]
    assert (
        len(outcome) == 1 and "synthetic observation set was complete and consistent" in outcome[0]
    )
    assert (
        "never real broker readiness, trading authorization or guaranteed protection" in outcome[0]
    )
    assert "Connected capability remains unknown; trading_permission = False" in outcome[0]
    text = shown(at)
    table = next(m.value for m in at.markdown if m.value.startswith("| Stage |"))
    for stage, request_id in (
        ("CONTRACT", 1001),
        ("QUOTE", 1002),
        ("ACCOUNT", 1003),
        ("POSITIONS", 1004),
    ):
        assert re.search(
            rf"\| {stage} \| read dispatched \(request {request_id}\) \| \d+ \| yes \| "
            rf"INFO only \(control, not permission\) \|",
            table,
        ), stage
    assert (
        "| PREFLIGHT | synthetic session attestation | — | — | INFO only (control, not permission) |"
        in table
    )
    assert "| CORRELATION | every dispatched stream | — | — | no finding recorded | — |" in table
    assert "UNKNOWN" not in table and "NOT DISPATCHED" not in table
    for code in (
        "SESSION_ATTESTATION_CONTROL",
        "CONTRACT_IDENTITY_CONTROL",
        "REGULAR_SESSION_CONTROL",
        "QUOTE_LIVE_CONTROL",
        "ACCOUNT_SNAPSHOT_CONTROL",
        "POSITIONS_SNAPSHOT_CONTROL",
    ):
        assert f"`INFO` **{code}**" in text, code
    assert "No evidence gap in the synthetic observation set. That is still a FAKE control" in text
    assert "Still unknown whatever the outcome" in text and "Deterministic replay: run 1 of" in text
    assert "first run (nothing to compare)" in text
    for glyph in ("✅", "✔", "☑", "SATISFIED", "READY FOR", "AUTHORIZED", "APPROVED"):
        assert glyph not in text, glyph
    assert (
        "dispatched: `request_contract_details`, `request_quote_snapshot`, `request_account_summary`, "
        "`request_positions`" in text
    )


# ----------------------------------------------------------------------------- refusal, unknown, error


@pytest.mark.parametrize("name", list(REPRESENTATIVE))
def test_representative_scenarios_render_the_runner_result_and_its_unknowns_honestly(
    name, tmp_path, monkeypatch
):
    outcome, headline, dispatched, must_show = REPRESENTATIVE[name]
    at = page(tmp_path, monkeypatch).run()
    at.radio[0].set_value(name).run()
    assert_matches_runner(at, name)
    assert metrics(at)["Outcome"] == outcome and metrics(at)["Headline finding kind"] == headline
    assert metrics(at)["Reads dispatched"] == f"{dispatched} of 4"
    text = shown(at)
    for token in must_show:
        assert token in text, token
    report = sc.run_scenario(sc.SCENARIOS[name])
    if report.outcome == ReadinessOutcome.CONTROL_COMPLETE:
        assert any(w.value.startswith("CONTROL_COMPLETE:") for w in at.warning)
    else:
        verdict = [e.value for e in at.error if e.value.startswith(f"{outcome}:")]
        assert len(verdict) == 1 and "trading_permission = False" in verdict[0]
        assert "unknown" in verdict[0].lower()
    if report.outcome == ReadinessOutcome.REFUSED_BEFORE_DISPATCH:
        assert (
            "No read was dispatched, so nothing was observed: every connected capability is unknown."
            in [i.value for i in at.info]
        )
        assert "dispatched: nothing" in text and not report.observations
    gaps = [f for f in report.findings if f.kind in GAP_KINDS]
    gap_warning = [w.value for w in at.warning if w.value.startswith("Findings whose kind records")]
    assert bool(gap_warning) == bool(gaps)
    for f in gaps:
        assert f"`{f.code}` ({f.kind.value})" in gap_warning[0]
    missing_reads = [
        s.value for s in READ_STAGES if s not in {o.stage for o in report.observations}
    ]
    if missing_reads:
        assert (
            f"Not observed in this scenario (unknown, not failed): {', '.join(missing_reads)}."
            in text
        )
    else:
        assert "Not observed in this scenario" not in text


def test_every_bundled_scenario_matches_the_runner_and_shows_no_payload(tmp_path, monkeypatch):
    at = page(tmp_path, monkeypatch).run()
    for name in sc.SCENARIOS:
        at.radio[0].set_value(name).run()
        assert_matches_runner(at, name)
        assert f"**`{name}`** — {sc.SCENARIOS[name].description}" in shown(at)


# ----------------------------------------------------------------------------- determinism and footprint


def test_rerun_is_deterministic_and_the_page_neither_connects_nor_writes(tmp_path, monkeypatch):
    deny_network(monkeypatch)
    at = page(tmp_path, monkeypatch)
    before = footprint(tmp_path)
    at.run()
    assert not at.exception
    first = dict(at.session_state[STATE_KEY])
    control_digest = sha256(
        render_json(sc.run_scenario(sc.SCENARIOS[CONTROL])).encode("utf-8")
    ).hexdigest()
    assert first == {"scenario": CONTROL, "digest": control_digest, "runs": 1}
    for n in (2, 3, 4):
        at.button[0].click().run()
        assert not at.exception
        assert dict(at.session_state[STATE_KEY]) == {
            "scenario": CONTROL,
            "digest": control_digest,
            "runs": n,
        }
        text = shown(at)
        assert (
            f"Deterministic replay: run {n} of `{CONTROL}`" in text
            and "identical to the previous run" in text
        )
        assert "DIFFERENT" not in text and "Scenario re-run." in [c.value for c in at.caption]
    at.radio[0].set_value("quote_stale").run()
    stale_digest = sha256(
        render_json(sc.run_scenario(sc.SCENARIOS["quote_stale"])).encode("utf-8")
    ).hexdigest()
    assert dict(at.session_state[STATE_KEY]) == {
        "scenario": "quote_stale",
        "digest": stale_digest,
        "runs": 1,
    }
    assert stale_digest != control_digest and "first run (nothing to compare)" in shown(at)
    at.button[0].click().run()
    assert (
        at.session_state[STATE_KEY]["runs"] == 2
        and at.session_state[STATE_KEY]["digest"] == stale_digest
    )
    at.radio[0].set_value(CONTROL).run()
    assert (
        at.session_state[STATE_KEY]["digest"] == control_digest
    )  # same script, same report, after switching
    assert footprint(tmp_path) == before  # nothing written anywhere the page could reach
    assert not (tmp_path / "forbidden.sqlite3").exists() and not (tmp_path / "data").exists()
    assert not list((tmp_path / "scratch").iterdir())


# ----------------------------------------------------------------------------- redaction


def sentinel_scenario() -> sc.Scenario:
    """An invented scenario whose every payload value is a sentinel: a synthetic-marked account the runner
    admits, sentinel prices and sizes, sentinel balances, an undocumented sensitive account tag, a sentinel
    position. The runner redacts all of it; the page must not get hold of any of it by another route."""
    account = "SYNTH-SENTINEL7777"
    contract = sc.contract_row(
        long_name="SENTINEL-LONGNAME-ZQX", local_symbol="SENTINEL-LOCAL", min_tick="0.0777"
    )
    quote = sc.quote_row(bid="777.77", ask="777.79", last="777.78", bid_size=7707, ask_size=7709)
    summary = AccountSummaryObservation(
        account_id=account,
        account_type="SYNTHETIC-SENTINEL-TYPE",
        currency="USD",
        values={
            "NetLiquidation": "8888888.88",
            "TotalCashValue": "8888888.88",
            "BuyingPower": "9999999.99",
            "SYNTH-PRIVATE1234": "not-a-decimal",
        },
    )
    position = PositionLine(
        account_id=account,
        con_id=sc.SYNTH_CON_ID,
        symbol="ZQXSENT",
        sec_type="STK",
        currency="USD",
        position="13",
        avg_cost="55.55",
    )
    script = sc.control_script(managed_accounts=[account]).model_copy(
        update={
            "contract_details": [
                sc.env(
                    "request_contract_details", 1, ResponseKind.CONTRACT_DETAILS, contract=contract
                ),
                sc.env("request_contract_details", 2, ResponseKind.CONTRACT_DETAILS_END),
            ],
            "quote_snapshot": [
                sc.env(
                    "request_quote_snapshot",
                    1,
                    ResponseKind.MARKET_DATA_TYPE,
                    market_data_type=MarketDataType.LIVE,
                ),
                sc.env("request_quote_snapshot", 2, ResponseKind.QUOTE, quote=quote),
                sc.env("request_quote_snapshot", 3, ResponseKind.TICK_SNAPSHOT_END),
            ],
            "account_summary": [
                sc.env(
                    "request_account_summary",
                    1,
                    ResponseKind.ACCOUNT_SUMMARY,
                    account_summary=summary,
                ),
                sc.env("request_account_summary", 2, ResponseKind.ACCOUNT_SUMMARY_END),
            ],
            "positions": [
                sc.env("request_positions", 1, ResponseKind.POSITION, position=position),
                sc.env("request_positions", 2, ResponseKind.POSITION_END),
            ],
        }
    )
    return sc.Scenario(
        name="sentinel_redaction_probe",
        description="Invented sentinel probe: every payload value must stay out of the page.",
        script=script,
        expectation=ReadinessExpectation(account_id=account, target=sc.TLT_TARGET),
        expected_outcome=ReadinessOutcome.NOT_READY,
        expected_codes=["ACCOUNT_TAGS_UNRECOGNIZED_WITHHELD", "ACCOUNT_VALUES_UNPARSEABLE"],
    )


SENTINELS = (
    "SYNTH-SENTINEL7777",
    "SENTINEL7777",
    "SENTINEL-LONGNAME-ZQX",
    "SENTINEL-LOCAL",
    "0.0777",
    "777.7",
    "7707",
    "7709",
    "8888888",
    "9999999",
    "SYNTHETIC-SENTINEL-TYPE",
    "SYNTH-PRIVATE1234",
    "not-a-decimal",
    "ZQXSENT",
    "55.55",
)


def test_invented_sentinel_payloads_never_reach_the_page(tmp_path, monkeypatch):
    probe = sentinel_scenario()
    report = sc.run_scenario(probe)
    assert report.outcome == ReadinessOutcome.NOT_READY and report.headline == FindingKind.MISMATCH
    assert {f.code for f in report.findings} >= set(probe.expected_codes) and len(
        report.dispatched
    ) == 4
    rendered = render_json(report)
    for sentinel in SENTINELS:
        assert sentinel not in rendered, (
            sentinel
        )  # the runner's own redaction, as the core tests assert
    bundled = len(sc.SCENARIOS)
    monkeypatch.setattr(sc, "SCENARIOS", {**sc.SCENARIOS, probe.name: probe})
    at = page(tmp_path, monkeypatch).run()
    assert len(at.radio[0].options) == bundled + 1
    at.radio[0].set_value(probe.name).run()
    assert not at.exception
    text = shown(at)
    masked = without_digests(text)
    for sentinel in SENTINELS:
        assert sentinel not in masked, sentinel
    assert f"account_ref `{report.account_ref}`" in text and report.account_ref.startswith(
        "SYNTH-..."
    )
    assert metrics(at) == expected_metrics(report)
    for line in finding_lines(report):
        assert line in text, line
    assert "`MISMATCH` **ACCOUNT_VALUES_UNPARSEABLE**" in text and "names withheld" in text
    assert "`INFO` **ACCOUNT_TAGS_UNRECOGNIZED_WITHHELD**" in text
    assert probe.description in text and "as scripted" in text
    assert "| POSITIONS | read dispatched (request 1004) | 1 | yes |" in text


def test_runner_failure_halts_safely_without_a_traceback_or_payload(tmp_path, monkeypatch):
    def boom(scenario):
        raise RuntimeError(f"SENTINEL-EXC-PAYLOAD bid=123.45 account={sc.SYNTH_ACCOUNT}")

    monkeypatch.setattr(sc, "run_scenario", boom)
    at = page(tmp_path, monkeypatch).run()
    assert not at.exception
    errors = [e.value for e in at.error]
    assert errors[0].startswith("OFFLINE / FAKE ONLY")
    halted = [
        e for e in errors if e.startswith("Diagnostic halted safely: RuntimeError while replaying")
    ]
    assert len(halted) == 1 and "nothing was connected, written or authorized" in halted[0]
    text = shown(at)
    assert (
        "SENTINEL-EXC-PAYLOAD" not in text and "123.45" not in text and sc.SYNTH_ACCOUNT not in text
    )
    assert not at.metric and "Synthetic outcome" not in [h.value for h in at.subheader]
    assert STATE_KEY not in at.session_state


# ----------------------------------------------------------------------------- static checklist and docs


def test_prerequisite_checklist_is_static_sourced_from_the_merged_docs_and_never_green(
    tmp_path, monkeypatch
):
    at = page(tmp_path, monkeypatch).run()
    lines = [
        line
        for m in at.markdown
        for line in m.value.splitlines()
        if line.startswith("- **NOT RECORDED**")
    ]
    assert len(lines) == 10
    docs = DOCS.read_text(encoding="utf-8")
    gate = docs[docs.index("## Gate checklist") : docs.index("## Relation to the gateway")]
    for line in lines:
        label = re.fullmatch(r"- \*\*NOT RECORDED\*\* — \*\*(.+?)\*\*: .+", line).group(1)
        assert f"**{label}**" in gate, label
    warning = [
        w.value
        for w in at.warning
        if w.value.startswith("None of these ten items is recorded as satisfied")
    ]
    assert len(warning) == 1 and "cannot attest, verify or configure" in warning[0]
    assert "residence country" in warning[0] and "weekly/manual reauthentication" in warning[0]
    unknowns = next(
        m.value for m in at.markdown if m.value.startswith("**Still unknown whatever the outcome")
    )
    assert unknowns.count("\n- ") == 10 and "never evidence of PAPER versus LIVE" in unknowns
    assert not list(at.checkbox) and not list(at.toggle)
    text = shown(at)
    for glyph in ("✅", "✔", "☑", "SATISFIED", "PASSED", "VERIFIED:"):
        assert glyph not in text, glyph
    # The same checklist and unknowns are shown whatever the scenario outcome.
    at.radio[0].set_value("environment_live").run()
    again = shown(at)
    assert all(line in again for line in lines) and unknowns in again


def test_view_docs_name_the_exact_commands_interpretation_and_limits():
    text = VIEW_DOCS.read_text(encoding="utf-8")
    for token in (
        "PYTHONPATH=src streamlit run dashboard/app.py",
        "dashboard/pages/4_Paper_Readiness.py",
        "PYTHONPATH=src python -m pytest tests/test_paper_readiness_page.py",
        "OFFLINE / FAKE ONLY",
        "trading_permission",
        "CONTROL_COMPLETE",
        "REFUSED_BEFORE_DISPATCH",
        "NOT_READY",
        "docs/PAPER_READINESS.md",
        "https://github.com/tsaid1/tradingbot/issues/22",
        "## Limits",
    ):
        assert token in text, token
