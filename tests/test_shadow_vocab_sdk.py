"""Shadow mode with the owner-only vocabulary review (PR #68), on the fake TWS; invented text.

* The candidate-lexicon opt-in is refused in shadow mode before any session opens: an experimental
  vocabulary may run only in an offline replay on a throwaway state directory, and a shadow run
  reads a real (read-only) broker session.
* The private review log works as in mock mode, and the leak property holds: with the log on, no
  journal row (the shadow decision-quote and fill rows included), heartbeat (its shadow block
  included), alert, notification, log or state file holds a private word, in any case.
"""
# ruff: noqa: F811 - the pytest fixtures are imported from the shadow and session SDK suites
from __future__ import annotations

import json
import logging

import pytest

pytest.importorskip("ibapi")

from runtime_harness import post
from test_ibkr_session_sdk import tws  # noqa: F401 - fixture
from test_shadow_mode_sdk import (  # noqa: F401 - fixtures
    Shadow,
    assert_only_read_only_messages,
    mono,
    run_until_processed,
    shadow,
)
from test_vocab_review_service import (
    CLEAN,
    OTHER_MARKET,
    PERSON,
    PRIVATE,
    UNKNOWN_WORD,
    files_containing,
    private_dir,
)

from tradingbot.runtime.config import ConfigError


def test_the_candidate_lexicon_opt_in_is_refused_in_shadow_mode(tmp_path, tws, mono):
    with pytest.raises(ConfigError, match="candidate"):
        Shadow(tmp_path, tws, mono, [post("1", 0, CLEAN)], allow_candidate_lexicon=True)
    assert tws.connections == []  # refused before any session opened


def test_the_private_review_log_works_in_shadow_mode_and_nothing_else_holds_the_words(
        shadow, tmp_path, tws, caplog, capsys):
    caplog.set_level(logging.DEBUG)
    log = private_dir(tmp_path) / "review.jsonl"
    # The clean post first (it executes: shadow fill and quote rows exist), then the three holds.
    sh = shadow([post("1", 0, CLEAN), post("2", 30, UNKNOWN_WORD), post("3", 60, PERSON),
                 post("4", 90, OTHER_MARKET)], interpreter={"review_log_path": str(log)})
    moves = iter(range(100000))

    def fresh():
        i = next(moves) % 2
        sh.live(88.40, 88.42 + i / 100)
        sh.live(88.40 - i / 100, 88.42)

    run_until_processed(sh, 4, before=fresh, cycles=200)
    fresh()
    sh.step()
    opens = [a for a in sh.actions() if a["action"] == "OPEN"]
    assert [a["status"] for a in opens] == ["EXECUTED", "HOLD", "HOLD", "HOLD"], [
        (a["status"], a["reasons"], a["mapping_reason"]) for a in opens]
    lines = [json.loads(x) for x in log.read_text().splitlines()]
    assert {t["token"] for x in lines for t in x["tokens"]} == {"zorbly", "Quentin", "Zorbex"}
    assert sh.events("SHADOW_DECISION_QUOTES") and sh.events("SHADOW_SIMULATED_FILL")
    hb = sh.service.store.get("heartbeat")
    assert hb["shadow"]["fills"]
    sh.close()
    skip = {log.resolve(), (tmp_path / "posts.jsonl").resolve()}  # the private log, the input
    assert files_containing(tmp_path, PRIVATE, skip) == []
    out = capsys.readouterr()
    for needle in (n.lower() for n in PRIVATE):
        for blob in (caplog.text, out.out, out.err, json.dumps(sh.notifier.sent),
                     json.dumps(sh.actions(), default=str),
                     json.dumps(sh.service.store.events(), default=str),
                     json.dumps(hb, default=str),
                     json.dumps(sh.service.store.alerts(), default=str)):
            assert needle not in blob.lower(), needle
    assert_only_read_only_messages(tws)
