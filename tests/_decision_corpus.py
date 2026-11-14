"""The invented decision corpus that proves the structured price readings change no execution decision.

Every string literal of the test modules and every post of the invented fixture files is read by the
interpreter; each reading's EXISTING output (the whole InterpretedPost except the additive
``price_readings``) is hashed. The fixture posts are also replayed in time order through the gateway
and the Sprint 1.1 coordinator, and mapped by the shipped instrument mapping, and those decisions are
hashed too. ``test_price_readings_are_additive`` computes both with the price reader and with the
price reader patched to return nothing, on the same code: no frozen golden, so it holds for every
future change. Invented text only (the repository's own tests and fixtures)."""
from __future__ import annotations

import ast
import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
FIXTURE_POSTS = (REPO / "fixtures" / "eval_synthetic" / "posts.jsonl",
                 REPO / "fixtures" / "runtime" / "mock_service_posts.jsonl",
                 REPO / "fixtures" / "runtime" / "shadow_example_posts.jsonl")
_NUM = re.compile(r"(?<![\w.])\$?(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)")


def _sha(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()


def corpus_texts() -> list[str]:
    """Every plain string literal of the test modules that could be a post (invented)."""
    out = set()
    for f in sorted((REPO / "tests").glob("test_*.py")):
        try:
            tree = ast.parse(f.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                s = node.value
                if 8 <= len(s) <= 2000 and re.search(r"[A-Za-z]", s) and " " in s:
                    out.add(s)
    return sorted(out)


def _reading_digest(ip) -> str:
    return _sha(ip.model_dump(mode="json", exclude={"price_readings"}))


def interpreter_digests(texts, readings_seen: list | None = None) -> dict[str, str]:
    from tradingbot.source_events import Provenance, SourcePost
    from tradingbot.tkl_interpreter import TklInterpreter
    t0 = datetime(2026, 1, 5, 15, 0, tzinfo=timezone.utc)
    out = {}
    it = TklInterpreter()
    for i, text in enumerate(texts):
        t = t0 + timedelta(minutes=i)
        p = SourcePost(source_id=f"c{i}", post_id=f"c{i}", author_id="A", verified_author=True, text=text,
                       text_complete=True, is_repost=False, published_at=t, received_at=t,
                       provenance=Provenance.SYNTHETIC)
        ip = it.interpret(p)
        if readings_seen is not None:
            readings_seen.append(len(ip.price_readings))
        out[hashlib.sha256(text.encode()).hexdigest()] = _reading_digest(ip)
    return out


def fixture_posts():
    from tradingbot.source_events import SourcePost
    posts = []
    for f in FIXTURE_POSTS:
        for line in f.read_text(encoding="utf-8").splitlines():
            if line.strip():
                posts.append(SourcePost.model_validate_json(line))
    return posts


def decision_digests(tmp_dir: Path) -> dict[str, str]:
    """Replay (gateway + coordinator) and the shipped mapping over each fixture file, in time order."""
    from tradingbot.event_replay import PaperEventReplay
    from tradingbot.models import MockPolicy, Quote
    from tradingbot.runtime.mapping import MappingPolicy
    from tradingbot.tkl_interpreter import TklInterpreter, group_linked_actions, replay_posts
    from tradingbot.source_events import SourcePost

    def quotes(p, now):  # interpreter-independent: every ticker quoted at the post's first number
        m = _NUM.search(p.text or "")
        if not m:
            return {}
        mid = Decimal(m.group(1).replace(",", ""))
        syms = set(re.findall(r"\$([A-Z]{1,5})\b", p.text)) | {"TLT", "SPY", "QQQ", "GLD", "USO", "UNG"}
        return {s: Quote(symbol=s, bid=mid - Decimal("0.01"), ask=mid + Decimal("0.01"), observed_at=now)
                for s in syms}

    policy = MappingPolicy.from_toml(REPO / "config" / "instrument_mapping.toml")
    out = {}
    for k, f in enumerate(FIXTURE_POSTS):
        posts = [SourcePost.model_validate_json(x) for x in f.read_text(encoding="utf-8").splitlines() if x.strip()]
        coord = PaperEventReplay(tmp_dir / f"paper{k}.sqlite3", MockPolicy())
        it = TklInterpreter()
        steps = replay_posts(posts, coord, it, quotes)
        out[f"replay:{f.name}"] = _sha([[o.model_dump(mode="json") for o in s.result.outcomes] for s in steps])
        it2 = TklInterpreter()
        rows = []
        for p in sorted(posts, key=lambda p: (p.available_at, p.revision, p.post_id)):
            ip = it2.interpret(p)
            mapped, decisions = policy.apply(
                group_linked_actions(ip), clauses={c.action_id: c.clause for c in ip.classifications},
                now=p.available_at, held={}, recorded={}, post_text=p.text,
                levels={c.action_id: [(m.role.value, m.value) for m in c.prices] for c in ip.classifications})
            rows.append([mapped.model_dump(mode="json"), [d.model_dump(mode="json") for d in decisions]])
            it2.observe(p, ip)
        out[f"mapping:{f.name}"] = _sha(rows)
    return out
