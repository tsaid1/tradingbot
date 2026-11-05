"""Synthetic live-probe evidence and authorization failures."""

import json
import sys
import unittest
from copy import deepcopy
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "docs/research/source_corpus"))
from g0_audit import audit, authorization

DENIAL = "https://api.x.com/2/problems/not-authorized-for-resource"


def session():
    target = {"id": "unit-test-author", "protected": True}
    start = "2020-01-01T00:00:00Z"
    records = [
        {"kind": "meta", "target": target},
        {"kind": "baseline", "status": 200, "since_id": "1", "posts": [{"id": "1"}],
         "sent_at": start},
    ]
    for idx, lag in enumerate([10, 12, 20], 2):
        records.append({
            "kind": "call", "interval_s": 15, "status": 200,
            "phase": "p1", "sent_at": f"2020-01-01T00:0{idx}:00Z",
            "received_at": f"2020-01-01T00:0{idx}:{lag:02}Z",
            "posts": [{"id": str(idx), "author_id": target["id"], "lag_s": 99,
                       "text": "sensitive fixture", "created_at": f"2020-01-01T00:0{idx}:00Z"}],
            "bearer_control": {"status": 200, "n_posts": 0, "error_types": [DENIAL]},
        })
    records += [
        {"kind": "reconcile", "status": 200, "pages": 1, "reported": 3, "polled": 3,
         "missed_ids": [], "vanished_ids": []},
        {"kind": "end", "outcome": "done"},
    ]
    return records


class AuditTests(unittest.TestCase):
    def test_verified_run_uses_receipt_and_never_returns_identifying_fields(self):
        result = audit(session(), {"bearer_timeline": {"status": 200, "n_posts": 0,
                                                       "errors": [{"type": DENIAL, "resource_id": "unit-test-author"}]}})
        self.assertEqual(result["freshness_at_15s"], "PASS")
        latency = result["latency_by_effective_interval_s"]["15.0"]["publication_to_receipt_s"]
        self.assertEqual(latency, {"n": 3, "p50": 12, "p95": 20, "p99": 20})
        self.assertEqual(result["measured_window"]["first_sent_at"], "2020-01-01T00:02:00+00:00")
        interval = result["latency_by_effective_interval_s"]["15.0"]
        self.assertEqual(interval["observed_send_gap_s"]["p50"], 60)
        self.assertEqual(result["paired_explicit_denials"], 3)
        self.assertEqual(result["reconciliation"], "PASS")
        self.assertEqual(result["standalone_bearer"], "PASS")
        for private in ("unit-test-author", "sensitive fixture", "created_at", "missed_ids"):
            self.assertNotIn(private, json.dumps(result))

    def test_empty_200_bad_auth_and_server_error_are_not_explicit_denials(self):
        for status in (200, 401, 403, 429, 500):
            with self.subTest(status=status):
                self.assertEqual(authorization({"status": status, "n_posts": 0}), "INCONCLUSIVE")
        self.assertEqual(authorization({"status": 200, "n_posts": 1}), "FAIL")
        self.assertEqual(authorization({"status": 200}), "INCONCLUSIVE")
        self.assertEqual(authorization(None), "UNMEASURED")
        self.assertEqual(authorization({"status": 401, "n_posts": 0, "error_types": [DENIAL]}),
                         "INCONCLUSIVE")

    def test_standalone_denial_must_name_the_protected_target(self):
        control = {"status": 200, "n_posts": 0,
                   "errors": [{"type": DENIAL, "resource_id": "other-user"}]}
        self.assertEqual(audit(session(), {"bearer_timeline": control})["standalone_bearer"],
                         "INCONCLUSIVE")

    def test_invalid_reconcile_and_missing_controls_cannot_pass(self):
        records = session()
        records[-2]["status"] = 503
        records[2].pop("bearer_control")
        result = audit(records, {})
        self.assertEqual(result["paired_bearer"], "INCONCLUSIVE")
        self.assertEqual(result["reconciliation"], "INCONCLUSIVE")
        records[-2]["status"] = 200
        records[-2]["pages"] = 10
        self.assertEqual(audit(records, {})["reconciliation"], "INCONCLUSIVE")
        records[-2]["pages"] = 1
        records[-2]["vanished_ids"] = ["2"]
        self.assertEqual(audit(records, {})["reconciliation"], "FAIL")

    def test_other_cadences_incomplete_runs_and_wrong_authors_cannot_certify(self):
        records = session()
        self.assertEqual(audit(records[:-1], {})["freshness_at_15s"], "INCONCLUSIVE")
        records[2]["interval_s"] = 3
        records[3]["posts"][0]["author_id"] = "some-other-author"
        result = audit(records, {})
        self.assertEqual(result["wrong_author"], 1)
        self.assertEqual(result["freshness_at_15s"], "INCONCLUSIVE")
        self.assertEqual(result["latency_by_effective_interval_s"]["3.0"]
                         ["publication_to_receipt_s"]["n"], 1)

    def test_edits_repeats_preexisting_posts_and_failed_baseline_are_excluded(self):
        records = session()
        records[2]["posts"][0]["edit_of_seen"] = True
        records[3]["posts"][0]["created_at"] = "2019-12-31T00:00:00Z"
        records.insert(5, deepcopy(records[4]))
        result = audit(records, {})
        self.assertEqual(result["duplicates"], 1)
        self.assertEqual(result["excluded_target_samples_or_missing_ids"], 2)
        self.assertEqual(result["freshness_at_15s"], "INCONCLUSIVE")
        records[1]["status"] = 500
        self.assertEqual(audit(records, {})["freshness_at_15s"], "UNMEASURED")

    def test_slow_session_fails_at_strict_thirty_second_boundary(self):
        records = session()
        for rec in records[2:5]:
            rec["received_at"] = rec["received_at"][:17] + "30Z"
        self.assertEqual(audit(records, {})["freshness_at_15s"], "FAIL")


if __name__ == "__main__":
    unittest.main()
