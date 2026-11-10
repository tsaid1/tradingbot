"""Version 2 private account dataflow in a closed invented replay. Invented identities only; no SDK.

Covers the contract's five paired criteria (managed identity, owner-issued binding, private row matching,
complete versus incomplete streams, v1 parity and no private payload), both PR #35 regressions through the
replay, copied and stale handles, mixed versions and single use.
"""
from __future__ import annotations

import copy
import pickle

import pytest

from tradingbot.paper_readiness import private_account as pa
from tradingbot.paper_readiness import scenarios as sc
from tradingbot.paper_readiness.account_position_callbacks import AccountSummaryCallbacks
from tradingbot.paper_readiness.account_ref import MatchStatus
from tradingbot.paper_readiness.diagnostics import ReadinessOutcome
from tradingbot.paper_readiness.observations import ReadinessExpectationV2, ResponseKind
from tradingbot.paper_readiness.preflight import preflight_v2
from tradingbot.paper_readiness.private_account import AccountOwner, Binding, PrivateAccountReplay
from tradingbot.paper_readiness.runner import (
    REQUEST_IDS,
    render_json,
    render_text,
    run_private_account_replay,
)
from tradingbot.paper_readiness.transport import FakeClock, FakeReadOnlyTransport

KEY = b"k" * 32
EXPECTED, FOREIGN = "INVENTEDACCT01", "INVENTEDACCT02"  # the replay's invented identities
PRIVATE_VALUES = (EXPECTED, FOREIGN, "180000.00", "150000.00", "360000.00", "-15.25", "98.5", KEY.decode())
COMPLETE, NOT_READY, REFUSED = (ReadinessOutcome.CONTROL_COMPLETE, ReadinessOutcome.NOT_READY,
                                ReadinessOutcome.REFUSED_BEFORE_DISPATCH)


def run(name: str, key: bytes = KEY):
    return run_private_account_replay(PrivateAccountReplay(name, key))


def codes(report) -> set[str]:
    return {f.code for f in report.findings}


def assert_no_private_payload(*texts: str) -> None:
    for text in texts:
        for value in PRIVATE_VALUES:
            assert value not in text, value


# ----------------------------------------------------------------------------- 1. managed identity

def test_a_matching_singleton_completes_with_only_the_reference():
    report = run("matching_singleton")
    assert report.outcome == COMPLETE and report.records_version == 2
    assert report.account_ref.startswith("acctref-v1:") and len(report.account_ref) == 43
    assert {"ACCOUNT_SNAPSHOT_CONTROL", "POSITIONS_SNAPSHOT_CONTROL", "QUOTE_LIVE_CONTROL"} <= codes(report)


@pytest.mark.parametrize(("name", "code"), [
    ("managed_absent", "ACCOUNT_EVIDENCE_ABSENT"),
    ("managed_foreign", "ACCOUNT_NOT_IN_MANAGED_LIST"),
    ("managed_duplicate", "MANAGED_ACCOUNTS_NOT_SINGULAR"),
])
def test_absent_foreign_or_duplicate_managed_identity_refuses_before_dispatch(name, code):
    report = run(name)
    assert report.outcome == REFUSED and code in codes(report)
    assert report.dispatched == [] and report.account_ref == "UNBOUND"


def test_an_invalid_key_refuses_as_malformed_evidence():
    report = run("matching_singleton", key=b"short")
    assert report.outcome == REFUSED and "ACCOUNT_EVIDENCE_REFUSED" in codes(report)


# ----------------------------------------------------------------------------- 2. owner-issued binding

def owner() -> AccountOwner:
    return AccountOwner(EXPECTED, [EXPECTED], KEY)


def test_only_the_owners_own_binding_is_recognised():
    a = owner()
    assert a.status is MatchStatus.EXACT and a.owns(a.binding)
    forged = object.__new__(Binding)
    object.__setattr__(forged, "_reference", a.binding.reference)
    other = owner()  # same identity and key, different run
    assert not a.owns(forged) and not a.owns(other.binding) and not other.owns(a.binding)
    assert not a.owns(a.binding.reference) and not a.owns(None)
    with pytest.raises(TypeError):
        Binding(object(), a.binding.reference)


@pytest.mark.parametrize("duplicate", [copy.copy, copy.deepcopy, lambda b: pickle.loads(pickle.dumps(b))],
                         ids=["copy", "deepcopy", "pickle"])
def test_a_copied_or_serialized_handle_is_refused(duplicate):
    a = owner()
    try:
        clone = duplicate(a.binding)
    except (TypeError, AttributeError):
        return  # refusing to duplicate is also a refusal
    assert clone is not a.binding and not a.owns(clone)


def test_a_stale_handle_is_refused_after_the_run():
    replay = PrivateAccountReplay("matching_singleton", KEY)
    binding = replay.binding
    assert replay.binding_owned(binding)
    run_private_account_replay(replay)
    assert not replay.binding_owned(binding)
    assert replay.transport.request_account_summary(REQUEST_IDS["request_account_summary"], binding) == []


def test_the_replay_transport_refuses_a_handle_it_did_not_issue():
    replay = PrivateAccountReplay("matching_singleton", KEY)
    foreign = PrivateAccountReplay("matching_singleton", KEY).binding
    assert replay.transport.request_positions(REQUEST_IDS["request_positions"], foreign) == []
    assert replay.handle_refusals == 1


def test_the_binding_is_immutable_and_the_owner_cannot_be_copied():
    a = owner()
    with pytest.raises(AttributeError):
        a.binding._reference = "acctref-v1:" + "0" * 32
    for duplicate in (copy.copy, copy.deepcopy, pickle.dumps):
        with pytest.raises(TypeError):
            duplicate(a)


def test_the_managed_list_is_snapshot_before_retention():
    managed = [EXPECTED]
    a = AccountOwner(EXPECTED, managed, KEY)
    managed.append(FOREIGN)
    assert a.status is MatchStatus.EXACT and a.owns(a.binding)


def test_a_caller_reference_or_unowned_status_is_never_a_binding():
    replay = PrivateAccountReplay("matching_singleton", KEY)
    decision = preflight_v2(replay.expectation(), replay.attest_session(), MatchStatus.EXACT, binding_owned=False)
    assert decision.refused and [f.code for f in decision.refusals] == ["BINDING_REFUSED"]
    caller = preflight_v2(None, replay.attest_session(), MatchStatus.EXACT, binding_owned=True)
    assert [f.code for f in caller.refusals] == ["BINDING_REFUSED"]
    raw = preflight_v2(replay.expectation(), replay.attest_session(), "EXACT", binding_owned=True)
    assert [f.code for f in raw.refusals] == ["ACCOUNT_EVIDENCE_REFUSED"]


# ----------------------------------------------------------------------------- 3. private row matching

def test_matching_rows_project_to_the_reference_and_foreign_rows_keep_their_own():
    replay = PrivateAccountReplay("foreign_summary_rows", KEY)
    rows = [e.account_summary for e in replay.transport.request_account_summary(
        REQUEST_IDS["request_account_summary"], replay.binding) if e.kind == ResponseKind.ACCOUNT_SUMMARY]
    assert len(rows) == 1 and rows[0].account_id.startswith("acctref-v1:")
    assert rows[0].account_id != replay.binding.reference  # never relabeled as the expected account
    report = run("foreign_summary_rows")
    assert report.outcome == NOT_READY and "ACCOUNT_ID_MISMATCH" in codes(report)


@pytest.mark.parametrize(("name", "code"), [
    ("foreign_position_row", "POSITION_ACCOUNT_MISMATCH"),
    ("malformed_row_identity", "ACCOUNT_SUMMARY_END_MISSING"),
    ("duplicate_rows", "ACCOUNT_SUMMARY_END_MISSING"),
])
def test_foreign_malformed_or_duplicate_rows_never_succeed(name, code):
    report = run(name)
    assert report.outcome == NOT_READY and code in codes(report)


def test_the_owner_mode_producer_requires_an_open_owner_with_its_binding():
    with pytest.raises(ValueError):
        AccountSummaryCallbacks(1003, owner=AccountOwner(EXPECTED, [FOREIGN], KEY))
    with pytest.raises(ValueError):
        AccountSummaryCallbacks(1003, owner=object())
    a = owner()
    stream = AccountSummaryCallbacks(1003, owner=a)
    a.close()  # stale mid-stream: rows can no longer be projected
    stream.accountSummary(1003, EXPECTED, "NetLiquidation", "1.00", "USD")
    stream.accountSummaryEnd(1003)
    assert not stream.completed and [i.code.value for i in stream.issues] == ["MALFORMED_CALLBACK"]


# ----------------------------------------------------------------------------- 4. complete versus incomplete

@pytest.mark.parametrize(("name", "code", "absent_control"), [
    ("missing_end", "POSITIONS_END_MISSING", "POSITIONS_SNAPSHOT_CONTROL"),
    ("error_in_stream", "ACCOUNT_SUMMARY_END_MISSING", "ACCOUNT_SNAPSHOT_CONTROL"),
    ("missing_currency", "ACCOUNT_CURRENCY_MISSING", "ACCOUNT_SNAPSHOT_CONTROL"),   # PR #35 regression 1
    ("oversized_quantity", "POSITIONS_END_MISSING", "POSITIONS_SNAPSHOT_CONTROL"),  # PR #35 regression 2
])
def test_missing_end_errors_and_the_pr35_regressions_remain_visible_non_success(name, code, absent_control):
    report = run(name)
    assert report.outcome == NOT_READY and code in codes(report)
    assert absent_control not in codes(report)  # the affected stream never earns its control


class RecordingSocketTransport:
    """A non-FAKE transport that records any call. It must never be reached."""
    transport_kind = "SOCKET"

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __getattr__(self, name: str):
        def record(*_args, **_kwargs):
            self.calls.append(name)
            return []
        return record


def test_the_replay_transport_cannot_be_reassigned():
    replay = PrivateAccountReplay("matching_singleton", KEY)
    with pytest.raises(AttributeError):
        replay.transport = RecordingSocketTransport()
    for name in ("scenario", "clock"):
        with pytest.raises(AttributeError):
            setattr(replay, name, None)


def test_a_replaced_transport_is_refused_before_any_of_its_methods_run():
    """Review P1: even a transport forced past the read-only property is never dispatched."""
    replay = PrivateAccountReplay("matching_singleton", KEY)
    socket_transport = RecordingSocketTransport()
    object.__setattr__(replay, "_transport", socket_transport)
    report = run_private_account_replay(replay)
    assert socket_transport.calls == []
    assert report.outcome == REFUSED and codes(report) == {"REPLAY_TRANSPORT_REFUSED"}
    assert report.dispatched == [] and not replay.binding_owned(replay.binding)  # closed, binding stale


def test_a_transport_bound_to_another_replay_is_refused():
    replay, other = PrivateAccountReplay("matching_singleton", KEY), PrivateAccountReplay("matching_singleton", KEY)
    object.__setattr__(replay, "_transport", other.transport)
    report = run_private_account_replay(replay)
    assert report.outcome == REFUSED and codes(report) == {"REPLAY_TRANSPORT_REFUSED"}


def test_a_replay_runs_once():
    replay = PrivateAccountReplay("matching_singleton", KEY)
    assert run_private_account_replay(replay).outcome == COMPLETE
    again = run_private_account_replay(replay)
    assert again.outcome == REFUSED and codes(again) == {"REPLAY_ALREADY_USED"}


# ----------------------------------------------------------------------------- 5. v1 parity and no payload

def test_v1_scenarios_keep_version_1_reports():
    for scenario in sc.SCENARIOS.values():
        report = sc.run_scenario(scenario)
        assert report.records_version == 1 and report.outcome == scenario.expected_outcome
        assert "acctref" not in render_json(report)


def test_versions_never_mix():
    clock = FakeClock(sc.CLOCK_START)
    v2 = ReadinessExpectationV2(account_ref="acctref-v1:" + "0" * 32, target=sc.TLT_TARGET)
    report = pa_run_v1(v2, clock)
    assert report.outcome == REFUSED and codes(report) == {"RECORDS_VERSION_MISMATCH"}
    assert report.dispatched == []
    v1_session = sc.session()
    decision = preflight_v2(v2, v1_session, MatchStatus.EXACT, binding_owned=True)
    assert [f.code for f in decision.refusals] == ["RECORDS_VERSION_MISMATCH"]
    with pytest.raises(TypeError):
        run_private_account_replay(FakeReadOnlyTransport(sc.control_script(), clock))
    with pytest.raises(ValueError):
        PrivateAccountReplay("not-a-scenario", KEY)


def test_a_v2_expectation_with_a_non_fake_transport_refuses_without_any_call():
    """Review P2: the version check runs before any branch reads a v1-only field."""
    from tradingbot.paper_readiness.runner import run_readiness_check
    clock = FakeClock(sc.CLOCK_START)
    v2 = ReadinessExpectationV2(account_ref="acctref-v1:" + "0" * 32, target=sc.TLT_TARGET)
    socket_transport = RecordingSocketTransport()
    report = run_readiness_check(socket_transport, v2, clock=clock)
    assert report.outcome == REFUSED and codes(report) == {"RECORDS_VERSION_MISMATCH"}
    assert socket_transport.calls == [] and report.dispatched == [] and report.account_ref == "UNBOUND"
    v1 = run_readiness_check(RecordingSocketTransport(), sc.EXPECTATION, clock=clock)
    assert codes(v1) == {"TRANSPORT_NOT_FAKE"}  # a v1 expectation keeps its existing refusal


def pa_run_v1(expectation, clock):
    from tradingbot.paper_readiness.runner import run_readiness_check
    return run_readiness_check(FakeReadOnlyTransport(sc.control_script(), clock), expectation, clock=clock)


@pytest.mark.parametrize("name", sorted(pa.REPLAY_SCENARIOS))
def test_no_raw_identity_key_balance_or_quantity_leaves_any_scenario(name):
    replay = PrivateAccountReplay(name, KEY)
    texts = [repr(replay), repr(replay.binding), repr(replay._owner)]
    report = run_private_account_replay(replay)
    texts += [render_json(report), render_text(report)]
    assert_no_private_payload(*texts)
    assert report.trading_permission is False and report.authority == "NON_AUTHORITATIVE"


def test_errors_carry_no_private_payload():
    messages = []
    for call in (lambda: Binding(object(), EXPECTED), lambda: pickle.dumps(owner()),
                 lambda: PrivateAccountReplay(EXPECTED, KEY), lambda: run_private_account_replay(EXPECTED)):
        with pytest.raises((TypeError, ValueError)) as caught:
            call()
        messages.append(str(caught.value) + repr(caught.value))
    assert_no_private_payload(*messages)
