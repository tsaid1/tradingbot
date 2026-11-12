"""Final round of the entry-rule review (PR #66): an anchored level needs UNDERSTOOD neighbours.

Understood neighbour of the level's sentence: the boundary of the post, one of this action's own
instruction sentences, or a sentence stating only the action's own stop and target (the
protective-levels sentence). R1: the sentence AFTER every anchored level must be understood (a
protective-levels sentence counts whatever the key). R2: the sentence BEFORE an elliptical level
must be an own instruction sentence, or, only with ``level_after_own_protective_levels``, a
protective-levels sentence; the generic allow-listed neighbour no longer counts. Invented text, the
whole mock service, the shipped mapping and a copy with the key on.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from execution_harness import SYMBOL, Harness, open_action, source
from runtime_harness import MAPPING, Runtime, post
from test_entry_rule_paper_only import anchored_gate

from tradingbot.execution.store import IntentState

CUT = "# ------------------------------------------------------------------ templates"


def keyed_mapping(folder: Path) -> Path:
    head, tail = MAPPING.read_text().split(CUT, 1)
    assert head.count('environment = "PAPER" }') == 1
    head = head.replace('environment = "PAPER" }',
                        'environment = "PAPER", level_after_own_protective_levels = true }')
    f = folder / "keyed.toml"
    f.write_text(head + CUT + tail)
    return f


def run(text: str, key: bool):
    with tempfile.TemporaryDirectory() as d:
        mapping = keyed_mapping(Path(d)) if key else MAPPING
        (Path(d) / "rt").mkdir()
        rt = Runtime(Path(d) / "rt", [post("1", 0, text)], mapping_file=mapping)
        rt.run(3)
        out = [(a["status"], a["reasons"]) for a in rt.actions() if a["action"] == "OPEN"]
        sent = rt.broker.submit_calls
        rt.service.close()
        return sent, out


NB1 = [f"We are now long $TLT.\n{x}\n88.42 now.\nStop 87.20."
       for x in ("Our position.", "My position.", "Our trade.", "This.")]
NB2 = [f"We are now long $TLT.\n88.42 now.\n{y}\nStop 87.20."
       for y in ("That was our purchase price.", "That is our cost basis.", "Our average.",
                 "It is the stop.")] + ["We are now long $TLT, 88.42 now. That is our average cost."]
KEY_HOLDS = [
    # a protective-levels sentence before, commentary after
    "We are now long $TLT.\nStop 87.20, target 89.50.\n88.42 now.\nThe chart looks constructive.",
]
ALWAYS_HOLDS = [
    # the neighbour before is the action's own clause, the one after is foreign
    "We are now long $TLT.\n88.42 now.\nThe chart looks constructive.\nStop 87.20.",
    "We are now long $TLT, 88.42 now. The chart looks constructive. Stop 87.20.",
]


@pytest.mark.parametrize("key", [False, True])
@pytest.mark.parametrize("text", [*NB1, *NB2, *ALWAYS_HOLDS])
def test_a_level_with_a_neighbour_that_is_not_understood_never_trades(text, key):
    sent, out = run(text, key)
    assert sent == 0 and out and out[0][0] != "EXECUTED", out


@pytest.mark.parametrize("text", KEY_HOLDS)
def test_with_the_key_a_protective_sentence_before_and_commentary_after_holds(text):
    sent, out = run(text, True)
    assert sent == 0 and out[0][0] != "EXECUTED", out


PLAIN = [
    "We are now long $TLT, 88.42 now. Stop 87.20.",  # instruction sentence, own stop after
    "We are now long $TLT, 88.42 now, stop 87.20.",
    "We are now long $TLT.\n88.42 now.\nStop 87.20.",  # elliptical after the instruction
    "We are now long $TLT.\n88.42 now.\nStop 87.20, target 89.50.",
]
KEY_PLAIN = [
    "We are now long $TLT.\nStop 87.20, target 89.50.\n88.42 now.",  # at the end of the post
]
# Not tradable, and not by this rule: a stop written BEFORE the instruction is not attached to it
# by the interpreter (PROTECTIVE_STOP_MISSING), and an elliptical level after an instruction that
# carries its own stop ("We are now long $X, stop 87.20.\n88.42 now.") is held by the unchanged
# level-word deny-list (STOP_OR_TARGET).
STILL_HELD = ["We are now long $TLT, stop 87.20.\n88.42 now.",
              "We are now long $TLT, stop 87.20, 88.42 now."]


@pytest.mark.parametrize("key", [False, True])
@pytest.mark.parametrize("text", STILL_HELD)
def test_an_elliptical_level_after_an_instruction_with_its_stop_stays_held(text, key):
    sent, out = run(text, key)
    assert sent == 0 and "LEVEL_KIND:STOP_OR_TARGET" in out[0][1], out


@pytest.mark.parametrize("key", [False, True])
@pytest.mark.parametrize("text", PLAIN)
def test_the_plain_shapes_still_trade(text, key):
    sent, out = run(text, key)
    assert sent == 1 and out[0][0] == "EXECUTED", out


@pytest.mark.parametrize("text", KEY_PLAIN)
def test_with_the_key_a_level_after_the_own_protective_levels_trades(text):
    sent, out = run(text, True)
    assert sent == 1 and out[0][0] == "EXECUTED", out
    sent, out = run(text, False)
    assert sent == 0, out


# ------------------------------------------------------------------ recovery


def recorded_not_sent(h, gate, act):
    orig = h.coord._submit
    h.coord._submit = lambda *a, **k: None  # the process dies before the submission
    out = h.coord.execute(source("p1", at=h.clock.now()), act, gate, now=h.clock.now())
    h.coord._submit = orig
    h.clock.advance(1)
    return out.order_ref


def test_recovery_uses_the_plans_reference_for_an_anchored_entry_only(tmp_path):
    h = Harness(tmp_path / "a")
    ref = recorded_not_sent(h, anchored_gate(), open_action(entry=None))
    h.broker.set_quote(SYMBOL, "49.98", "50.02", received_at=h.clock.now())
    h.coord.recover()
    assert h.broker.submit_calls == 1, h.coord.store.intent(ref).reasons
    h.coord.close()


def test_recovery_of_any_other_mapped_entry_is_unchanged_from_main(tmp_path):
    """On main a mapped entry's recovery is checked against the intent's own entry reference: here
    the publisher's 50 against a plan in execution prices of 50.02, so the recomputed limit differs
    and it is never resent (RESEND_LIMIT_WOULD_DIFFER)."""
    h = Harness(tmp_path / "b")
    ref = recorded_not_sent(h, anchored_gate(rule="EXPLICIT_ENTRY_ONLY"), open_action(entry="50"))
    h.broker.set_quote(SYMBOL, "49.98", "50.02", received_at=h.clock.now())
    h.coord.recover()
    intent = h.coord.store.intent(ref)
    assert h.broker.submit_calls == 0 and intent.state == IntentState.REFUSED
    assert "RESEND_LIMIT_WOULD_DIFFER" in intent.reasons, intent.reasons
    h.coord.close()
