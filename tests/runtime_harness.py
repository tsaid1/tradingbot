"""Shared harness for the runtime-service tests: invented posts, a SimClock, a SimBroker, a temp
state directory and a recording notifier. No network, no real broker, no real post."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from tradingbot.execution.sim_broker import SimBroker, SimClock
from tradingbot.runtime.config import RuntimeConfig, parse_config
from tradingbot.runtime.operator_actions import OperatorQueue
from tradingbot.runtime.service import RuntimeService
from tradingbot.runtime.sources import ReplayFileSource
from tradingbot.runtime.store import RuntimeStore
from tradingbot.source_events import Provenance, SourcePost

REPO = Path(__file__).resolve().parents[1]
MAPPING = REPO / "config" / "instrument_mapping.toml"  # the owner's (shipped) mapping
# The service-mechanics tests run a whole lifecycle at 2 shares; the shipped TLT entry allows 1
# share (owner, 12 Nov 2026), so they use the pre-decision version-1 copy (fixture header).
SERVICE_MAPPING = REPO / "fixtures" / "runtime" / "mock_service_mapping.toml"
DEMO_FIXTURE = REPO / "fixtures" / "runtime" / "mock_service_posts.jsonl"
START = datetime(2026, 6, 1, 14, 0, tzinfo=UTC)
RECORDED = datetime(2026, 3, 2, 15, 0, tzinfo=UTC)  # timestamps inside the fixture files
AUTHOR = "4242"  # invented author id

ENTRY = "We are now long $TLT at 88.40. Stop 87.20."
ADD = "Adding to our $TLT long here at 88.45."
RAISE_STOP = "Raising our $TLT stop to 87.90."
REDUCE_HALF = "Selling half of our $TLT long at 88.40."
CLOSE = "Closing the rest of our $TLT long at 88.40."
INDEX_SHORT = "We are now short the S&P 500 at 6,700. Stop 6,760."
COMMENTARY = "We expect bonds to stay choppy this week."


# Every size, cap, collar, freshness and timing value must be explicit (review F4): these are the
# invented test values, equal to the old defaults.
REQUIRED_SERVICE = {
    "cadence_seconds": 5,
    "max_backoff_seconds": 60,
    "failure_alert_after": 3,
    "stuck_after_seconds": 60,
}
REQUIRED_POLICY = {
    "quantity": 2,
    "quantity_per_add": 2,
    "max_price_deviation_pct": "1.0",
    "max_spread_pct": "1.0",
    "max_notional_usd": "2000",
    "max_signal_age_seconds": 90,
    "max_quote_age_seconds": 30,
    "max_instrument_exposure_usd": "2000",
    "max_aggregate_exposure_usd": "10000",
}
REQUIRED_EXECUTION = {
    "expected_account": "SIM-PAPER-0001",
    "max_submit_attempts": 2,
    "settle_seconds": 10,
    "exit_no_trace_lookups": 3,
    "max_intent_age_seconds": 120,
    "max_exit_intent_age_seconds": 120,
    "lookup_horizon_seconds": 21600,
    "confirm_attempts": 3,
    "confirm_wait_seconds": 0,
}


def required_toml(**tables: dict) -> str:
    """The [service], [policy] and [execution] tables with every required value (overridable)."""
    out = []
    for name, base in (
        ("service", REQUIRED_SERVICE),
        ("policy", REQUIRED_POLICY),
        ("execution", REQUIRED_EXECUTION),
    ):
        values = {**base, **tables.get(name, {})}
        out.append(f"[{name}]")
        out += [f"{k} = {v!r}" if isinstance(v, str) else f"{k} = {v}" for k, v in values.items()]
    return "\n".join(out).replace("'", '"') + "\n"


def post(
    pid: str,
    offset: float,
    text: str,
    *,
    source_id: str | None = None,
    revision: int = 1,
    revised: float | None = None,
    verified: bool = True,
    complete: bool = True,
) -> SourcePost:
    published = RECORDED + timedelta(seconds=offset)
    revised_at = RECORDED + timedelta(seconds=revised) if revised is not None else None
    return SourcePost(
        source_id=source_id or pid,
        post_id=pid,
        revision=revision,
        author_id=AUTHOR,
        verified_author=verified,
        text=text,
        text_complete=complete,
        published_at=published,
        revised_at=revised_at,
        received_at=(revised_at or published) + timedelta(seconds=2),
        provenance=Provenance.SYNTHETIC,
    )


def write_posts(path: Path, posts: list[SourcePost]) -> Path:
    path.write_text("".join(p.model_dump_json() + "\n" for p in posts), encoding="utf-8")
    return path


class RecordingNotifier:
    def __init__(self):
        self.sent: list[tuple[str, str, str]] = []

    def notify(self, kind: str, title: str, message: str) -> None:
        self.sent.append((kind, title, message))

    def kinds(self) -> list[str]:
        return [k for k, _, _ in self.sent]


class Runtime:
    """A runtime service on temp files. ``restart()`` builds fresh objects on the same files, the
    way a new process would (in-memory broker scripts and caches are lost)."""

    def __init__(
        self,
        folder: Path,
        posts: list[SourcePost] | None = None,
        *,
        posts_file: Path | None = None,
        compression: float | None = 1.0,
        mapping_file: Path = SERVICE_MAPPING,
        allow_candidate_lexicon: bool = False,
        **overrides,
    ):
        self.folder = Path(folder)
        self.allow_candidate_lexicon = allow_candidate_lexicon
        self.clock = SimClock(START)
        self.posts_file = posts_file or write_posts(self.folder / "posts.jsonl", posts or [])
        raw = {
            "mode": "mock",
            "paths": {
                "state_dir": str(self.folder / "state"),
                "halt_file": str(self.folder / "HALT"),
                "mapping_file": str(mapping_file),
            },
            "source": {"kind": "replay", "file": str(self.posts_file)},
            "service": dict(REQUIRED_SERVICE),
            "policy": dict(REQUIRED_POLICY),
            "execution": dict(REQUIRED_EXECUTION),
            "mock": {"quotes": {"TLT": {"bid": "88.40", "ask": "88.42"}}},
        }
        if compression is not None:
            raw["source"]["compression"] = compression
        for key, value in overrides.items():
            if isinstance(value, dict) and isinstance(raw.get(key), dict):
                raw[key] = {**raw[key], **value}
            else:
                raw[key] = value
        self.config: RuntimeConfig = parse_config(raw, base=self.folder)
        # The explicit initialisation step a deployment runs once (scripts/tb_operator.py init).
        OperatorQueue.initialise(self.config.operator_db)
        self.notifier = RecordingNotifier()
        self.restart()

    def restart(self, *, source=None) -> RuntimeService:
        if getattr(self, "service", None) is not None:
            self.service.close()  # the old process is gone: the OS would release its store lock
        self.store = RuntimeStore(self.config.runtime_db)
        self.broker = SimBroker(self.config.broker_db, clock=self.clock.now)
        self.source = source or ReplayFileSource(
            self.config.source.file,
            self.store,
            clock=self.clock.now,
            compression=self.config.source.compression,
        )
        self.service = RuntimeService(
            self.config,
            source=self.source,
            broker=self.broker,
            notifier=self.notifier,
            clock=self.clock.now,
            store=self.store,
            allow_candidate_lexicon=self.allow_candidate_lexicon,
        )
        return self.service

    def step(self, seconds: float = 5.0):
        report = self.service.run_once()
        self.clock.advance(seconds)
        return report

    def run(self, cycles: int, seconds: float = 5.0) -> list:
        return [self.step(seconds) for _ in range(cycles)]

    def halt(self) -> None:
        self.config.paths.halt_file.write_text("halt\n")

    def signals(self) -> list[dict]:
        return self.store.signals()

    def actions(self) -> list[dict]:
        return self.store.signal_actions()

    def heartbeat(self) -> dict:
        return json.loads(self.config.heartbeat_file.read_text())

    def refs_placed_more_than_once(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for o in self.broker.all_orders():
            counts[o.order_ref] = counts.get(o.order_ref, 0) + 1
        return {r: n for r, n in counts.items() if n > 1}
