"""Evaluation runner: scoring math, matching, failure ranking, golden replays, CLI. Synthetic only."""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from tradingbot.evaluation import (
    PR, FAILURE_SEVERITY, EvaluationReport, GoldenReplay, GoldenStep, LabeledAction, LabeledPost, Predicted,
    evaluate, load_golden, load_labels, load_posts, match_actions, predicted_is_executable, run_golden,
)
from tradingbot.event_models import Timing
from tradingbot.models import Action, PositionSide
from tradingbot.source_events import Provenance, SourcePost
from tradingbot.tkl_interpreter import Classification, MessageClass, PriceRole, Tense, TklInterpreter

REPO = Path(__file__).resolve().parents[1]
FIXTURES = REPO / "fixtures" / "eval_synthetic"
T0 = datetime(2026, 10, 1, 14, 30, tzinfo=timezone.utc)


def post(sid, text, hours=0, **kw):
    t = T0 + timedelta(hours=hours)
    base = {"source_id": sid, "post_id": sid, "author_id": "SYNTH", "verified_author": True, "text": text,
            "text_complete": True, "published_at": t, "received_at": t, "provenance": Provenance.SYNTHETIC}
    base.update(kw)
    return SourcePost(**base)


def label(sid, *actions):
    return LabeledPost(source_id=sid, actions=list(actions))


def act(cls, action=Action.NO_TRADE, **kw):
    return LabeledAction(message_class=cls, action=action, **kw)


def test_pr_math():
    pr = PR(tp=3, fp=1, fn=2)
    assert pr.precision == 0.75 and pr.recall == 0.6 and round(pr.f1, 4) == 0.6667
    assert PR().precision is None and PR().recall is None and PR().f1 is None
    assert PR(fp=1).row()["precision"] == "0.0%" and PR(fp=1).row()["recall"] == "n/a"


def test_label_schema_rejects_extras_and_loads_fixture():
    with pytest.raises(ValidationError):
        LabeledAction(message_class=MessageClass.OPEN, quantity=5)
    labels = load_labels(FIXTURES / "labels.jsonl")
    assert len(labels) == 33 and labels["e06"].actions[1].message_class == MessageClass.MODIFY
    posts = load_posts(FIXTURES / "posts.jsonl")
    assert len(posts) == 33 and posts[0].source_id == "e01" and all(p.provenance == Provenance.SYNTHETIC for p in posts)


def _pred(cls, underlying=Action.NO_TRADE, asset=None, timing=Timing.CURRENT, ambiguities=(), executable=None):
    from tradingbot.event_models import TradeAction
    c = Classification(action_id="a1", message_class=cls, tense=Tense.PRESENT, underlying=underlying, asset=asset, clause="x",
                       ambiguities=list(ambiguities))
    a = TradeAction(action_id="a1", action=underlying, timing=timing, ambiguities=list(ambiguities),
                    explicit_instruction=True, evidence=["x"])
    return Predicted(c, a, predicted_is_executable(a, True) if executable is None else executable)


def test_matching_prefers_asset_and_action_then_class():
    labels = [act(MessageClass.CLOSE, Action.CLOSE, asset="TLT"), act(MessageClass.COMMENTARY)]
    preds = [_pred(MessageClass.COMMENTARY), _pred(MessageClass.CLOSE, Action.CLOSE, asset="TLT")]
    assert sorted(match_actions(labels, preds)) == [(0, 1), (1, 0)]
    assert match_actions(labels, []) == [(0, None), (1, None)]
    assert match_actions([], preds) == [(None, 0), (None, 1)]


def test_executable_candidate_definition():
    from tradingbot.event_models import TradeAction
    a = TradeAction(action_id="a1", action=Action.CLOSE, timing=Timing.CURRENT)
    assert predicted_is_executable(a, True)
    assert not predicted_is_executable(a, False)                                  # ineligible post
    assert not predicted_is_executable(a.model_copy(update={"timing": Timing.HISTORICAL}), True)
    assert not predicted_is_executable(a.model_copy(update={"ambiguities": ["X"]}), True)
    assert not predicted_is_executable(a.model_copy(update={"action": Action.NO_TRADE}), True)


def test_false_executable_and_missed_instruction_are_both_reported():
    posts = [post("a", "We are now long $TLT at $88.50. Stop: $86.00. Target: $95.00."),
             post("b", "Pulling the plug on our $TLT long at $90.", hours=1),            # out of lexicon: miss
             post("c", "Closing our $TLT long here at $91.", hours=2)]                   # labeled as commentary: false executable
    labels = {"a": label("a", act(MessageClass.OPEN, Action.OPEN, asset="TLT", side=PositionSide.LONG, executable=True,
                                  prices={PriceRole.ENTRY: "88.50", PriceRole.STOP: "86.00", PriceRole.TARGET: "95.00"})),
              "b": label("b", act(MessageClass.CLOSE, Action.CLOSE, asset="TLT", executable=True, links_to="a")),
              "c": label("c", act(MessageClass.COMMENTARY, asset="TLT"))}
    report = evaluate(posts, labels, dataset="unit")
    assert report.labeled_posts == 3 and report.post_executable["tp"] == 1
    assert [m["source_id"] for m in report.missed_instructions] == ["b"]
    assert report.missed_instructions[0]["failure"] == "MISSED_INSTRUCTION_PHRASING"
    assert [f["source_id"] for f in report.false_executable] == ["c"]
    kinds = {f["kind"]: f for f in report.failures_ranked}
    assert kinds["FALSE_EXECUTABLE"]["severity"] == FAILURE_SEVERITY["FALSE_EXECUTABLE"] == 5
    assert report.failures_ranked[0]["kind"] == "FALSE_EXECUTABLE"                   # ranked by severity x count
    assert report.price_roles["ENTRY"]["correct"] == 1 and report.price_roles["STOP"]["correct"] == 1
    assert any("false executable" in r for r in report.recommendations)
    md = report.to_markdown()
    assert "Pulling the plug" not in md and "$TLT" not in md                           # no text in reports


def test_linkage_scored_only_for_executable_follow_ups():
    posts = [post("a", "We are now long $TLT at $88.50. Stop: $86.00. Target: $95.00."),
             post("b", "Taking profits on half of our $TLT long at $92.00.", hours=1),
             post("c", "Closing the remaining $TLT long here at $94.10.", hours=2),
             post("d", "We closed our $TLT long yesterday at $94.10.", hours=26)]
    labels = {"a": label("a", act(MessageClass.OPEN, Action.OPEN, asset="TLT", side=PositionSide.LONG, executable=True)),
              "b": label("b", act(MessageClass.REDUCE, Action.REDUCE, asset="TLT", close_fraction="0.5", links_to="a", executable=True)),
              "c": label("c", act(MessageClass.CLOSE, Action.CLOSE, asset="TLT", links_to="a", executable=True)),
              "d": label("d", act(MessageClass.RETROSPECTIVE, Action.CLOSE, tense=Tense.PAST, asset="TLT", links_to="a"))}
    report = evaluate(posts, labels, dataset="unit")
    assert report.linkage == {"evaluated": 2, "correct": 2, "missing": 0, "wrong": 0, "accuracy": "100.0%"}
    assert report.class_accuracy == "100.0%" and report.tense_accuracy == "100.0%"
    assert report.failures_ranked == [] and report.recommendations == ["No failures: extend the label set before drawing conclusions."]


def test_ineligible_post_candidates_are_not_false_executables():
    posts = [post("t", "We are now long $TLT at $88.50. Stop: $86.00.", text_complete=False)]
    labels = {"t": label("t", act(MessageClass.OPEN, Action.OPEN, asset="TLT", side=PositionSide.LONG))}
    report = evaluate(posts, labels, dataset="unit")
    assert report.false_executable == [] and report.post_executable["fp"] == 0


def test_golden_replay_detects_mismatches(tmp_path):
    spec = load_golden(FIXTURES / "golden_tlt_lifecycle.json")
    assert run_golden(spec, {}, tmp_path / "ok.sqlite3").passed
    broken = spec.model_copy(update={"steps": [spec.steps[0].model_copy(update={"orders": [{"side": "SELL", "quantity": 4}]})]
                                     + spec.steps[1:]})
    result = run_golden(broken, {}, tmp_path / "bad.sqlite3")
    assert not result.passed and result.mismatches and "g1" in result.mismatches[0]
    by_id = {p.source_id: p for p in spec.posts}
    external = spec.model_copy(update={"posts": []})                                  # resolve posts from the dataset
    assert run_golden(external, by_id, tmp_path / "ext.sqlite3").passed


def test_all_four_synthetic_goldens_pass(tmp_path):
    for path in sorted(FIXTURES.glob("golden_*.json")):
        result = run_golden(load_golden(path), {}, tmp_path / f"{path.stem}.sqlite3")
        assert result.passed, (path.name, result.mismatches)


def test_golden_resolves_exact_revision_and_refuses_an_absent_original(tmp_path):
    template = load_golden(FIXTURES / "golden_tlt_lifecycle.json")
    first = template.posts[0]
    edit = first.model_copy(update={"revision": 2, "post_id": "later-version",
                                    "revised_at": first.published_at + timedelta(hours=1)})
    later = GoldenStep(source_id=first.source_id, post_id=edit.post_id, statuses=["HOLD"])
    both = template.model_copy(update={"posts": [], "steps": [template.steps[0], later]})
    versions = {p.post_id: p for p in [edit, first]}
    assert run_golden(both, versions, tmp_path / "both.db").passed
    missing = tmp_path / "missing.db"
    result = run_golden(both, {edit.post_id: edit}, missing)
    assert not result.passed and "requested revision unavailable" in result.mismatches[0]
    assert not missing.exists()
    final_only = both.model_copy(update={"steps": [later]})
    assert run_golden(final_only, {edit.post_id: edit}, tmp_path / "edit.db").passed


def test_synthetic_evaluation_aggregates():
    report = evaluate(load_posts(FIXTURES / "posts.jsonl"), load_labels(FIXTURES / "labels.jsonl"), dataset="synthetic")
    assert report.posts == 33 and report.labeled_posts == 33 and report.labeled_actions == 36
    assert report.false_executable == []                                              # distractors never become candidates
    # After the corrections only the past-tense entry announcement remains, and as a semantic gap, not a phrasing one
    assert [(m["source_id"], m["failure"]) for m in report.missed_instructions] == [("e05", "MISSED_INSTRUCTION_SEMANTICS")]
    assert report.linkage["wrong"] == 0 and report.linkage["missing"] == 0
    assert all(v["wrong"] == 0 for v in report.price_roles.values())
    assert {f["kind"] for f in report.failures_ranked} <= {"MISSED_INSTRUCTION_SEMANTICS", "TENSE_OR_CLASS_MISMATCH"}


def test_cli_synthetic_run_writes_reports_without_text(tmp_path):
    out = tmp_path / "results"
    proc = subprocess.run([sys.executable, str(REPO / "scripts" / "evaluate_corpus.py"), "--synthetic", "--out", str(out)],
                          capture_output=True, text=True, cwd=REPO, check=False)
    assert proc.returncode == 0, proc.stderr[-800:]
    md = (out / "report.md").read_text()
    data = json.loads((out / "report.json").read_text())
    assert "## Golden replays" in md and all(g["passed"] for g in data["golden"]) and len(data["golden"]) == 4
    fixture_text = "".join(json.loads(l)["text"] for l in (FIXTURES / "posts.jsonl").read_text().splitlines())
    for phrase in ("We are now long", "Taking profits", "Covering our", "stopped out"):
        assert phrase in fixture_text and phrase not in md and phrase not in json.dumps(data)


def test_cli_refuses_holdout_paths(tmp_path):
    holdout = tmp_path / "holdout_posts.jsonl"
    holdout.write_text("")
    proc = subprocess.run([sys.executable, str(REPO / "scripts" / "evaluate_corpus.py"), "--posts", str(holdout),
                           "--labels", str(FIXTURES / "labels.jsonl"), "--out", str(tmp_path / "o")],
                          capture_output=True, text=True, cwd=REPO, check=False)
    assert proc.returncode != 0 and "holdout" in (proc.stdout + proc.stderr).lower()


def test_report_markdown_structure():
    report = evaluate(load_posts(FIXTURES / "posts.jsonl"), load_labels(FIXTURES / "labels.jsonl"), dataset="synthetic")
    md = report.to_markdown()
    for heading in ("## Post level", "## Action level", "## Price roles", "## Position linkage",
                    "## False executable candidates", "## Missed instructions", "## Failures ranked", "## Recommendations"):
        assert heading in md
    assert EvaluationReport.model_validate_json(report.model_dump_json()) == report


def test_golden_spec_schema():
    with pytest.raises(ValidationError):
        GoldenReplay(name="x", steps=[GoldenStep(source_id="a", statuses=["SIMULATED"])], live_broker=True)
    spec = GoldenReplay(name="x", steps=[GoldenStep(source_id="a", statuses=["SIMULATED"])])
    assert spec.policy == {} and spec.lag_seconds == 0.0


def test_point_in_time_context_is_shared_across_the_evaluation():
    posts = [post("a", "We are now long $TLT at $88.50. Stop: $86.00. Target: $95.00."),
             post("b", "Closing our $TLT long here at $94.10.", hours=1)]
    labels = {"b": label("b", act(MessageClass.CLOSE, Action.CLOSE, asset="TLT", links_to="a", executable=True))}
    it = TklInterpreter()
    report = evaluate(posts, labels, dataset="unit", interpreter=it)
    assert report.labeled_posts == 1 and report.linkage["correct"] == 1
    assert it.context.ideas["a:a1"].closed_at == T0 + timedelta(hours=1)
