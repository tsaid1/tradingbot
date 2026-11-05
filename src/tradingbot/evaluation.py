"""Sprint 1.2C: evaluation runner for the TKL interpreter.

Consumes SourcePost items (synthetic fixtures now, the Source Agent's adapter output later), routes
them chronologically through TklInterpreter with its point-in-time context, scores the output
against private labels, runs chronological golden replays through the Sprint 1.1 coordinator, and
ranks parser failures. Mock-only: it adds no validator and no journal, and it never prints post
text; reports carry aggregates, IDs and failure codes only.
"""
from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from tradingbot.event_models import TradeAction, Timing
from tradingbot.event_replay import PaperEventReplay
from tradingbot.models import Action, MockPolicy, PositionSide, Quote, StrictModel
from tradingbot.source_events import SourcePost, coerce_source_posts
from tradingbot.tkl_interpreter import (
    Classification, MessageClass, PriceRole, Tense, TklInterpreter, group_linked_actions,
)

FILL_ACTIONS = frozenset({Action.OPEN, Action.ADD, Action.REDUCE, Action.CLOSE, Action.MODIFY})
INSTRUCTION_CLASSES = frozenset({
    MessageClass.OPEN, MessageClass.ADD, MessageClass.REDUCE, MessageClass.CLOSE, MessageClass.MODIFY,
    MessageClass.EXIT_ANNOUNCEMENT, MessageClass.CONDITIONAL, MessageClass.CANCEL,
})
FOLLOW_UP_ACTIONS = frozenset({Action.ADD, Action.REDUCE, Action.CLOSE, Action.MODIFY})

# Severity: 5 = could place a wrong fictional order; 1 = cosmetic. Rank = severity * frequency.
FAILURE_SEVERITY = {
    "FALSE_EXECUTABLE": 5,
    "WRONG_ACTION": 4, "WRONG_ASSET": 4, "WRONG_SIDE": 4, "LINK_WRONG": 4, "PRICE_ROLE_WRONG": 4,
    "MISSED_INSTRUCTION_PHRASING": 3, "MISSED_INSTRUCTION_SEMANTICS": 3, "TENSE_OR_CLASS_MISMATCH": 3,
    "LINK_MISSING": 2, "PRICE_ROLE_MISSING": 2, "ASSET_UNRESOLVED": 2, "FRACTION_MISMATCH": 2,
    "AMBIGUITY_HOLD": 1, "EXTRA_NON_EXECUTABLE_ACTION": 1, "PRICE_ROLE_EXTRA": 1,
}


# ----------------------------------------------------------------------------- labels

class LabeledAction(StrictModel):
    """One expected clause-level reading of a post. Private when it describes real posts."""
    message_class: MessageClass
    action: Action = Action.NO_TRADE
    tense: Tense = Tense.PRESENT
    asset: str | None = None
    side: PositionSide | None = None
    prices: dict[PriceRole, str] = Field(default_factory=dict)
    close_fraction: str | None = None
    links_to: str | None = None    # source_id of the post that opened the referenced position
    executable: bool = False       # an explicit instruction the follower should act on now


class LabeledPost(StrictModel):
    source_id: str
    revision: int = 1
    actions: list[LabeledAction] = Field(default_factory=list)
    note: str = ""


def load_labels(path: Path) -> dict[str, LabeledPost]:
    labels: dict[str, LabeledPost] = {}
    for line in path.read_text().splitlines():
        if line.strip():
            item = LabeledPost.model_validate_json(line)
            labels[item.source_id] = item
    return labels


def load_posts(path: Path, *, expected_author_id: str | None = None) -> list[SourcePost]:
    """JSONL of SourcePost dicts (schema 1.2) or raw G0 records. Unverified; for synthetic data."""
    items = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    return coerce_source_posts(items, expected_author_id=expected_author_id)


# ----------------------------------------------------------------------------- development export intake

class ExportManifest(BaseModel):
    """Sidecar the Source Agent ships with an export. Unknown keys are ignored so the agent may add
    its own metadata; the keys below are what the consumer checks."""
    model_config = ConfigDict(extra="ignore")
    split: Literal["development", "holdout", "synthetic"]
    post_count: int | None = None
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    labels_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    source: str = ""


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_sha256(path: Path, expected: str | None) -> str:
    actual = sha256_of(path)
    if expected is not None and actual != expected.lower():
        raise ValueError(f"Checksum mismatch for {path.name}: expected {expected[:12]}…, got {actual[:12]}…")
    return actual


def load_export(export: Path, *, manifest: Path | None = None, sha256: str | None = None,
                expected_author_id: str | None = None, allow_holdout: bool = False,
                require_checksum: bool = True) -> tuple[list[SourcePost], dict]:
    """Read a Source Agent export (JSONL of SourcePost dicts or G0 records) with integrity and
    split checks: the file digest must match the manifest and/or the given value, the manifest
    split must not be the holdout (unless explicitly allowed, final evaluation only), and the
    declared post count must match the file."""
    info: dict = {"export": export.name}
    declared: ExportManifest | None = None
    if manifest is not None:
        declared = ExportManifest.model_validate_json(manifest.read_text())
        info.update({"split": declared.split, "manifest": manifest.name, "source": declared.source})
        if declared.split == "holdout" and not allow_holdout:
            raise ValueError("Refusing the holdout split: it is reserved for the final evaluation")
        if sha256 is not None and declared.sha256 is not None and sha256.lower() != declared.sha256:
            raise ValueError("The given checksum disagrees with the manifest")
    expected = sha256.lower() if sha256 else (declared.sha256 if declared else None)
    if expected is None and require_checksum:
        raise ValueError("A checksum is required for a real export (manifest sha256 or --sha256)")
    info["sha256"] = verify_sha256(export, expected)
    lines = [line for line in export.read_text().splitlines() if line.strip()]
    if declared is not None and declared.post_count is not None and declared.post_count != len(lines):
        raise ValueError(f"Manifest declares {declared.post_count} posts, export has {len(lines)}")
    posts = coerce_source_posts([json.loads(line) for line in lines], expected_author_id=expected_author_id)
    info["posts"] = len(posts)
    return posts, info


def coerce_reader_arg(value: str, key: str | None = None):
    """Reader arguments arrive as strings on the command line. They are typed by name and shape so
    the Source Agent's ``historical_inputs(path=..., expected_sha256=..., expected_author_id=...,
    simulated_lag=..., through=...)`` receives what it declares: ``path``/``*_path``/``*_file``
    become Path, ``*lag*`` becomes a timedelta in seconds, ISO-8601 timestamps become aware
    datetimes, integers become int; everything else (digests, IDs) stays a string."""
    if key is not None:
        name = key.lower()
        if name in ("path", "manifest") or name.endswith(("_path", "_file")):
            return Path(value)
        if "lag" in name:
            return timedelta(seconds=float(value))
        if name.endswith(("_id", "sha256", "digest")):
            return value
    if value.isdigit():
        return int(value)
    if "T" in value and value[:4].isdigit():
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            return value
    return value


def parse_reader_args(pairs: Iterable[str]) -> dict:
    out: dict = {}
    for pair in pairs:
        if "=" not in pair:
            raise ValueError(f"Reader argument must be key=value, got {pair!r}")
        key, value = pair.split("=", 1)
        out[key.strip()] = coerce_reader_arg(value.strip(), key.strip())
    return out


def posts_from_adapter(historical_inputs, reader_args: dict, *, expected_author_id: str | None = None) -> list[SourcePost]:
    """Call the Source Agent's adapter with its own reader arguments and normalise the result."""
    return coerce_source_posts(historical_inputs(**reader_args), expected_author_id=expected_author_id)


# ----------------------------------------------------------------------------- scoring primitives

class PR(StrictModel):
    tp: int = 0
    fp: int = 0
    fn: int = 0

    @property
    def precision(self) -> float | None:
        return self.tp / (self.tp + self.fp) if self.tp + self.fp else None

    @property
    def recall(self) -> float | None:
        return self.tp / (self.tp + self.fn) if self.tp + self.fn else None

    @property
    def f1(self) -> float | None:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if p and r else (0.0 if (p is not None and r is not None) else None)

    def row(self) -> dict:
        return {"tp": self.tp, "fp": self.fp, "fn": self.fn, "precision": _pct(self.precision),
                "recall": _pct(self.recall), "f1": _pct(self.f1)}


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def _dec_equal(a: str | None, b: str | None) -> bool:
    if a is None or b is None:
        return a == b
    try:
        return Decimal(a) == Decimal(b)
    except InvalidOperation:
        return a == b


class RoleScore(StrictModel):
    tp: int = 0
    wrong: int = 0
    missing: int = 0
    extra: int = 0

    def row(self) -> dict:
        labeled = self.tp + self.wrong + self.missing
        predicted = self.tp + self.wrong + self.extra
        return {"labeled": labeled, "correct": self.tp, "wrong": self.wrong, "missing": self.missing, "extra": self.extra,
                "precision": _pct(self.tp / predicted if predicted else None),
                "recall": _pct(self.tp / labeled if labeled else None)}


@dataclass
class Failure:
    kind: str
    source_id: str
    where: str          # "label[i]" or "pred[action_id]"
    detail: str = ""    # codes only, never text

    @property
    def severity(self) -> int:
        return FAILURE_SEVERITY.get(self.kind, 1)


@dataclass
class Predicted:
    classification: Classification
    action: TradeAction
    executable: bool


def predicted_is_executable(action: TradeAction, eligible: bool) -> bool:
    """Would this reach the gateway as a fill-capable, unambiguous current candidate?"""
    return eligible and action.timing == Timing.CURRENT and action.action in FILL_ACTIONS and not action.ambiguities


def match_actions(labels: list[LabeledAction], preds: list[Predicted]) -> list[tuple[int | None, int | None]]:
    """Greedy pairing: same (asset, action) first, then same asset and class, then same class."""
    pairs: list[tuple[int | None, int | None]] = []
    free_l, free_p = list(range(len(labels))), list(range(len(preds)))
    for rule in ("asset_action", "asset_class", "class"):
        for li in list(free_l):
            lab = labels[li]
            for pi in list(free_p):
                c = preds[pi].classification
                ok = {
                    "asset_action": lab.asset == c.asset and lab.action == c.underlying and lab.action != Action.NO_TRADE,
                    "asset_class": lab.asset == c.asset and lab.message_class == c.message_class,
                    "class": lab.message_class == c.message_class,
                }[rule]
                if ok:
                    pairs.append((li, pi))
                    free_l.remove(li)
                    free_p.remove(pi)
                    break
    pairs.extend((li, None) for li in free_l)
    pairs.extend((None, pi) for pi in free_p)
    return pairs


# ----------------------------------------------------------------------------- the report

class GoldenStep(StrictModel):
    source_id: str
    post_id: str | None = None  # omit only when requesting the family's original post
    statuses: list[str]
    orders: list[dict] = Field(default_factory=list)     # [{"side": "SELL", "quantity": 3}]
    classes: list[MessageClass] | None = None


class GoldenReplay(StrictModel):
    """A chronological scenario replayed through the real coordinator. `posts` may be inline
    (synthetic) or resolved from the evaluated dataset by source_id (private)."""
    name: str
    policy: dict = Field(default_factory=dict)
    quotes: dict[str, dict[str, str]] = Field(default_factory=dict)   # captured post_id -> symbol -> mid
    lag_seconds: float = 0.0
    posts: list[SourcePost] = Field(default_factory=list)
    steps: list[GoldenStep]


class GoldenResult(StrictModel):
    name: str
    passed: bool
    steps: int
    mismatches: list[str] = Field(default_factory=list)


class EvaluationReport(StrictModel):
    dataset: str
    posts: int
    labeled_posts: int
    labeled_actions: int
    predicted_actions: int
    post_executable: dict
    post_instruction: dict
    action_level: dict
    action_by_type: dict[str, dict]
    class_accuracy: str
    tense_accuracy: str
    confusion: dict[str, int]
    price_roles: dict[str, dict]
    linkage: dict
    false_executable: list[dict]
    missed_instructions: list[dict]
    failures_ranked: list[dict]
    recommendations: list[str]
    golden: list[GoldenResult] = Field(default_factory=list)

    def to_markdown(self) -> str:
        lines = [f"# Interpreter evaluation: {self.dataset}", "",
                 (f"Posts {self.posts} (labeled {self.labeled_posts}), labeled actions {self.labeled_actions}, "
                  f"predicted actions {self.predicted_actions}."), "",
                 "## Post level", "", "| Metric | TP | FP | FN | Precision | Recall | F1 |", "|---|---|---|---|---|---|---|",
                 _md_pr("Executable instruction present", self.post_executable),
                 _md_pr("Any instruction present", self.post_instruction), "",
                 "## Action level", "", "| Scope | TP | FP | FN | Precision | Recall | F1 |", "|---|---|---|---|---|---|---|",
                 _md_pr("All actions", self.action_level)]
        lines += [_md_pr(k, v) for k, v in sorted(self.action_by_type.items())]
        lines += ["", (f"Temporal classification on matched actions: class accuracy {self.class_accuracy}, "
                       f"tense accuracy {self.tense_accuracy}."), "",
                  "| Confusion (label → predicted) | n |", "|---|---|"]
        lines += [f"| {k} | {v} |" for k, v in sorted(self.confusion.items(), key=lambda kv: -kv[1])]
        lines += ["", "## Price roles", "", "| Role | Labeled | Correct | Wrong | Missing | Extra | Precision | Recall |",
                  "|---|---|---|---|---|---|---|---|"]
        lines += [f"| {r} | {v['labeled']} | {v['correct']} | {v['wrong']} | {v['missing']} | {v['extra']} | "
                  f"{v['precision']} | {v['recall']} |" for r, v in sorted(self.price_roles.items())]
        lk = self.linkage
        lines += ["", "## Position linkage", "",
                  (f"Evaluated {lk['evaluated']}, correct {lk['correct']}, missing {lk['missing']}, wrong {lk['wrong']}, "
                   f"accuracy {lk['accuracy']}."), "",
                  f"## False executable candidates: {len(self.false_executable)}", ""]
        lines += [f"- {f['source_id']} {f['action_id']} predicted {f['predicted']} ({f['reason']})" for f in self.false_executable] or ["- none"]
        lines += ["", f"## Missed instructions: {len(self.missed_instructions)}", ""]
        lines += [f"- {m['source_id']} label[{m['label_index']}] {m['label_class']}/{m['label_action']} → {m['failure']}"
                  for m in self.missed_instructions] or ["- none"]
        lines += ["", "## Failures ranked (severity × frequency)", "", "| Rank | Failure | Count | Severity | Score | Examples |",
                  "|---|---|---|---|---|---|"]
        lines += [f"| {i + 1} | {f['kind']} | {f['count']} | {f['severity']} | {f['score']} | {', '.join(f['examples'])} |"
                  for i, f in enumerate(self.failures_ranked)]
        lines += ["", "## Recommendations", ""] + [f"{i + 1}. {r}" for i, r in enumerate(self.recommendations)]
        if self.golden:
            lines += ["", "## Golden replays", "", "| Replay | Steps | Result |", "|---|---|---|"]
            lines += [f"| {g.name} | {g.steps} | {'PASS' if g.passed else 'FAIL: ' + '; '.join(g.mismatches)} |" for g in self.golden]
        return "\n".join(lines) + "\n"


def _md_pr(name: str, row: dict) -> str:
    return f"| {name} | {row['tp']} | {row['fp']} | {row['fn']} | {row['precision']} | {row['recall']} | {row['f1']} |"


# ----------------------------------------------------------------------------- the runner

def evaluate(posts: Iterable[SourcePost], labels: dict[str, LabeledPost], *, dataset: str = "synthetic",
             interpreter: TklInterpreter | None = None) -> EvaluationReport:
    """Chronological pass: interpret each post against the context as it stood, score it, then
    advance the context. Posts without a label are interpreted (they shape the context) but not
    scored."""
    it = interpreter or TklInterpreter()
    ordered = sorted(posts, key=lambda p: (p.available_at, p.revision, p.post_id))
    post_exec, post_instr, action_pr = PR(), PR(), PR()
    by_type: dict[str, PR] = defaultdict(PR)
    class_hits = tense_hits = matched = 0
    confusion: Counter[str] = Counter()
    roles: dict[PriceRole, RoleScore] = defaultdict(RoleScore)
    link = Counter()
    false_exec: list[dict] = []
    missed: list[dict] = []
    failures: list[Failure] = []
    n_pred = n_lab = 0
    labeled_posts = 0

    for post in ordered:
        interpreted = it.interpret(post)
        eligible = it.is_source_eligible(post)
        preds = [Predicted(c, a, predicted_is_executable(a, eligible))
                 for c, a in zip(interpreted.classifications, interpreted.parsed.actions, strict=True)]
        label = labels.get(post.source_id)
        if label is not None and label.revision == post.revision:
            labeled_posts += 1
            n_pred += len(preds)
            n_lab += len(label.actions)
            m, ch, th = _score_post(post, label, preds, eligible, post_exec, post_instr, action_pr, by_type,
                                    confusion, roles, link, false_exec, missed, failures)
            matched, class_hits, tense_hits = matched + m, class_hits + ch, tense_hits + th
        it.observe(post, interpreted)

    ranked = _rank(failures)
    return EvaluationReport(
        dataset=dataset, posts=len(ordered), labeled_posts=labeled_posts, labeled_actions=n_lab,
        predicted_actions=n_pred, post_executable=post_exec.row(), post_instruction=post_instr.row(),
        action_level=action_pr.row(), action_by_type={k: v.row() for k, v in by_type.items()},
        class_accuracy=_pct(class_hits / matched if matched else None),
        tense_accuracy=_pct(tense_hits / matched if matched else None),
        confusion=dict(confusion), price_roles={r.value: s.row() for r, s in roles.items()},
        linkage={"evaluated": link["evaluated"], "correct": link["correct"], "missing": link["missing"],
                 "wrong": link["wrong"], "accuracy": _pct(link["correct"] / link["evaluated"] if link["evaluated"] else None)},
        false_executable=false_exec, missed_instructions=missed, failures_ranked=ranked,
        recommendations=_recommend(ranked, failures),
    )


def _score_post(post, label, preds, eligible, post_exec, post_instr, action_pr, by_type, confusion, roles, link,
                false_exec, missed, failures) -> tuple[int, int, int]:
    """Score one labeled post; returns (matched pairs, class hits, tense hits)."""
    sid = post.source_id
    matched = class_hits = tense_hits = 0
    lab_exec = any(a.executable for a in label.actions)
    pred_exec = any(p.executable for p in preds)
    _bump(post_exec, lab_exec, pred_exec)
    lab_instr = any(a.message_class in INSTRUCTION_CLASSES for a in label.actions)
    pred_instr = any(p.classification.message_class in INSTRUCTION_CLASSES for p in preds)
    _bump(post_instr, lab_instr, pred_instr)

    for li, pi in match_actions(label.actions, preds):
        lab = label.actions[li] if li is not None else None
        pred = preds[pi] if pi is not None else None
        if lab is not None and pred is not None:
            c, a = pred.classification, pred.action
            matched += 1
            class_hits += lab.message_class == c.message_class
            tense_hits += lab.tense == c.tense
            key = lab.action.value if lab.action != Action.NO_TRADE else lab.message_class.value
            same_action = lab.action == c.underlying and lab.asset == c.asset
            if same_action:
                action_pr.tp += 1
                by_type[key].tp += 1
            else:
                action_pr.fn += 1
                action_pr.fp += 1
                by_type[key].fn += 1
                if lab.asset != c.asset:
                    failures.append(Failure("ASSET_UNRESOLVED" if c.asset is None else "WRONG_ASSET", sid, f"label[{li}]",
                                            f"{lab.asset}->{c.asset}"))
                else:
                    failures.append(Failure("WRONG_ACTION", sid, f"label[{li}]", f"{lab.action.value}->{c.underlying.value}"))
            confusion[f"{lab.message_class.value} → {c.message_class.value}"] += 1
            if lab.message_class != c.message_class or lab.tense != c.tense:
                failures.append(Failure("TENSE_OR_CLASS_MISMATCH", sid, f"label[{li}]",
                                        f"{lab.message_class.value}/{lab.tense.value}->{c.message_class.value}/{c.tense.value}"))
            if lab.side is not None and c.side is not None and lab.side != c.side:
                failures.append(Failure("WRONG_SIDE", sid, f"label[{li}]", f"{lab.side.value}->{c.side.value}"))
            # price roles: scored only when the label carries prices (an empty dict means "not labeled")
            got = {m.role: m.value for m in c.prices}
            if lab.prices:
                for role, value in lab.prices.items():
                    if role not in got:
                        roles[role].missing += 1
                        failures.append(Failure("PRICE_ROLE_MISSING", sid, f"label[{li}]", role.value))
                    elif _dec_equal(got[role], value):
                        roles[role].tp += 1
                    else:
                        roles[role].wrong += 1
                        failures.append(Failure("PRICE_ROLE_WRONG", sid, f"label[{li}]", role.value))
                for role in got:
                    if role not in lab.prices and role != PriceRole.REFERENCE:
                        roles[role].extra += 1
                        failures.append(Failure("PRICE_ROLE_EXTRA", sid, f"pred[{a.action_id}]", role.value))
            if lab.close_fraction is not None and not _dec_equal(lab.close_fraction, c.close_fraction):
                failures.append(Failure("FRACTION_MISMATCH", sid, f"label[{li}]", f"{lab.close_fraction}->{c.close_fraction}"))
            # linkage
            if lab.links_to is not None and lab.action in FOLLOW_UP_ACTIONS and lab.executable:
                # linkage matters where the follower would act; a recap of a closed position has nothing to link
                link["evaluated"] += 1
                ref = (c.idea_reference or "").split(":")[0] or None
                if ref == lab.links_to:
                    link["correct"] += 1
                elif ref is None:
                    link["missing"] += 1
                    failures.append(Failure("LINK_MISSING", sid, f"label[{li}]", ",".join(c.ambiguities)))
                else:
                    link["wrong"] += 1
                    failures.append(Failure("LINK_WRONG", sid, f"label[{li}]", f"{lab.links_to}->{ref}"))
            # executability
            if lab.executable and not pred.executable:
                kind = "AMBIGUITY_HOLD" if a.ambiguities else ("MISSED_INSTRUCTION_SEMANTICS" if same_action else "MISSED_INSTRUCTION_PHRASING")
                if not eligible:
                    kind = "AMBIGUITY_HOLD"   # the post itself is ineligible; nothing could execute
                missed.append({"source_id": sid, "label_index": li, "label_class": lab.message_class.value,
                               "label_action": lab.action.value, "failure": kind})
                failures.append(Failure(kind, sid, f"label[{li}]", ",".join(a.ambiguities) or c.message_class.value))
            elif pred.executable and not lab.executable:
                false_exec.append({"source_id": sid, "action_id": a.action_id, "predicted": c.message_class.value,
                                   "reason": f"label {lab.message_class.value}/{lab.tense.value} not executable"})
                failures.append(Failure("FALSE_EXECUTABLE", sid, f"pred[{a.action_id}]", c.message_class.value))
        elif lab is not None:
            action_pr.fn += 1
            key = lab.action.value if lab.action != Action.NO_TRADE else lab.message_class.value
            by_type[key].fn += 1
            if lab.executable:
                missed.append({"source_id": sid, "label_index": li, "label_class": lab.message_class.value,
                               "label_action": lab.action.value, "failure": "MISSED_INSTRUCTION_PHRASING"})
                failures.append(Failure("MISSED_INSTRUCTION_PHRASING", sid, f"label[{li}]", "no matching prediction"))
            for role in lab.prices:
                roles[role].missing += 1
        elif pred is not None:
            c, a = pred.classification, pred.action
            action_pr.fp += 1
            key = c.underlying.value if c.underlying != Action.NO_TRADE else c.message_class.value
            by_type[key].fp += 1
            if pred.executable:
                false_exec.append({"source_id": sid, "action_id": a.action_id, "predicted": c.message_class.value,
                                   "reason": "no labeled instruction"})
                failures.append(Failure("FALSE_EXECUTABLE", sid, f"pred[{a.action_id}]", c.message_class.value))
            elif c.message_class not in (MessageClass.COMMENTARY, MessageClass.POSITION_SUMMARY):
                failures.append(Failure("EXTRA_NON_EXECUTABLE_ACTION", sid, f"pred[{a.action_id}]", c.message_class.value))
    return matched, class_hits, tense_hits


def _bump(pr: PR, labeled: bool, predicted: bool) -> None:
    if labeled and predicted:
        pr.tp += 1
    elif predicted:
        pr.fp += 1
    elif labeled:
        pr.fn += 1


def _rank(failures: list[Failure]) -> list[dict]:
    groups: dict[str, list[Failure]] = defaultdict(list)
    for f in failures:
        groups[f.kind].append(f)
    rows = [{"kind": k, "count": len(v), "severity": FAILURE_SEVERITY.get(k, 1),
             "score": len(v) * FAILURE_SEVERITY.get(k, 1),
             "examples": sorted({f"{f.source_id}:{f.where}" for f in v})[:6]} for k, v in groups.items()]
    return sorted(rows, key=lambda r: (-r["score"], -r["severity"], r["kind"]))


def _recommend(ranked: list[dict], failures: list[Failure]) -> list[str]:
    out: list[str] = []
    kinds = {r["kind"]: r["count"] for r in ranked}
    if kinds.get("FALSE_EXECUTABLE"):
        out.append(f"{kinds['FALSE_EXECUTABLE']} false executable candidate(s): add the offending phrasing to the "
                   "commentary/recap lexicon or require a stronger verb before any order candidate. Highest priority.")
    if kinds.get("MISSED_INSTRUCTION_PHRASING"):
        out.append(f"{kinds['MISSED_INSTRUCTION_PHRASING']} instruction(s) missed on phrasing: extend the verb lexicon "
                   "with the measured forms (cheap, deterministic) before considering a model-assisted second pass.")
    if kinds.get("MISSED_INSTRUCTION_SEMANTICS"):
        out.append(f"{kinds['MISSED_INSTRUCTION_SEMANTICS']} instruction(s) recognised but not executable: a semantic "
                   "rule is missing (for example past-tense entry announcements); decide the rule, then implement it.")
    if kinds.get("LINK_MISSING") or kinds.get("LINK_WRONG"):
        out.append("Position linkage failures: check whether the opening post was itself missed (cascading) before "
                   "changing the resolver; cascades disappear once the opener is recognised.")
    if kinds.get("PRICE_ROLE_WRONG") or kinds.get("PRICE_ROLE_MISSING"):
        out.append("Price-role failures: review the keyword window rules for the measured phrasings.")
    if kinds.get("TENSE_OR_CLASS_MISMATCH"):
        out.append("Tense/class mismatches: compare the verb forms involved with the present/past lexicon.")
    if kinds.get("AMBIGUITY_HOLD"):
        out.append("Ambiguity holds are fail-closed by design; reduce them only where the label shows the reference "
                   "was in fact unique.")
    if not out:
        out.append("No failures: extend the label set before drawing conclusions.")
    return out


# ----------------------------------------------------------------------------- golden replays

def run_golden(spec: GoldenReplay, posts_by_id: dict[str, SourcePost], db_path: Path) -> GoldenResult:
    available = spec.posts or posts_by_id.values()
    originals = {p.source_id: p for p in available if p.revision == 1}
    versions = {p.post_id: p for p in available}
    requested = []
    for step in spec.steps:
        post_id = step.post_id or step.source_id
        post = versions.get(step.post_id) if step.post_id else originals.get(step.source_id)
        if post is None or post.source_id != step.source_id:
            return GoldenResult(name=spec.name, passed=False, steps=len(spec.steps),
                                mismatches=[f"{post_id}: requested revision unavailable"])
        requested.append(post)
    posts = spec.posts or requested
    coordinator = PaperEventReplay(db_path, MockPolicy(**spec.policy))
    it = TklInterpreter()
    mismatches: list[str] = []
    results = {}
    for post in sorted(posts, key=lambda p: (p.available_at, p.revision, p.post_id)):
        interpreted = it.interpret(post)
        now = post.received_at if post.is_live else post.available_at + timedelta(seconds=spec.lag_seconds)
        quote_values = spec.quotes.get(post.post_id, {})
        if post.revision == 1 and post.post_id not in spec.quotes:
            quote_values = spec.quotes.get(post.source_id, {})
        quotes = {sym: Quote(symbol=sym, bid=Decimal(mid) - Decimal("0.05"), ask=Decimal(mid) + Decimal("0.05"), observed_at=now)
                  for sym, mid in quote_values.items()}
        result = coordinator.process(post.to_replay_source(), group_linked_actions(interpreted), quotes, now=now)
        it.observe(post, interpreted)
        results[post.post_id] = (interpreted, result)
    for step, post in zip(spec.steps, requested, strict=True):
        interpreted, result = results[post.post_id]
        statuses = [o.status for o in result.outcomes]
        if statuses != step.statuses:
            mismatches.append(f"{step.source_id}: statuses {statuses} != {step.statuses}")
        orders = [{"side": o.order.side, "quantity": o.order.quantity} for o in result.outcomes if o.order]
        if orders != step.orders:
            mismatches.append(f"{step.source_id}: orders {orders} != {step.orders}")
        if step.classes is not None:
            classes = [c.message_class for c in interpreted.classifications]
            if classes != step.classes:
                mismatches.append(f"{step.source_id}: classes {[c.value for c in classes]} != {[c.value for c in step.classes]}")
    return GoldenResult(name=spec.name, passed=not mismatches, steps=len(spec.steps), mismatches=mismatches)


def load_golden(path: Path) -> GoldenReplay:
    return GoldenReplay.model_validate_json(path.read_text())
