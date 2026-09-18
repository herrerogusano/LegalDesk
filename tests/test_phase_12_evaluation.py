from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT))

from evals.runner import (  # noqa: E402
    EXPECTED_CATEGORIES,
    _groundedness_for_citations,
    _validate_dataset,
    evaluate_dataset,
)


class Phase12EvaluationTests(unittest.TestCase):
    def test_dataset_covers_required_categories_and_minimum_size(self) -> None:
        payload = json.loads((ROOT / "evals" / "phase12_dataset.json").read_text(encoding="utf-8"))
        cases = _validate_dataset(payload)
        self.assertGreaterEqual(len(cases), 20)
        self.assertEqual({case["category"] for case in cases}, EXPECTED_CATEGORIES)
        self.assertEqual(len({case["id"] for case in cases}), len(cases))

    def test_local_runner_passes_all_cases_without_aws_or_inference(self) -> None:
        report = evaluate_dataset()
        self.assertEqual(report["mode"], "local-deterministic-mocks")
        self.assertEqual(report["awsCalls"], 0)
        self.assertEqual(report["inferenceCalls"], 0)
        self.assertEqual(report["totalCases"], 24)
        self.assertEqual(report["passedCases"], report["totalCases"])
        self.assertEqual(report["metrics"]["accessControlDenyRate"], 1.0)
        self.assertEqual(report["metrics"]["citationExactRate"], 1.0)
        self.assertEqual(report["metrics"]["toolChoiceAccuracy"], 1.0)

    def test_report_contains_metadata_only_not_questions_or_evidence_text(self) -> None:
        report = evaluate_dataset()
        encoded = json.dumps(report, ensure_ascii=False)
        self.assertNotIn("What is the fictional payment deadline", encoded)
        self.assertNotIn("Payment is due in 17 fictional days", encoded)
        self.assertNotIn("private identifier placeholder", encoded)
        for result in report["cases"]:
            self.assertEqual(set(result), {"caseId", "category", "expected", "actual", "passed"})

    def test_invalid_dataset_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "invalid.json"
            path.write_text(json.dumps({"cases": []}), encoding="utf-8")
            with self.assertRaises(ValueError):
                evaluate_dataset(path)

    def test_groundedness_proxy_rejects_invented_citation_ids(self) -> None:
        self.assertEqual(_groundedness_for_citations(["citation-1"], ["sundial-payment"]), 1.0)
        self.assertEqual(_groundedness_for_citations(["citation-invented"], ["sundial-payment"]), 0.0)

    def test_mutating_expected_never_changes_actual_and_causes_case_failure(self) -> None:
        source = json.loads((ROOT / "evals" / "phase12_dataset.json").read_text(encoding="utf-8"))
        baseline = evaluate_dataset()
        baseline_actual = {item["caseId"]: item["actual"] for item in baseline["cases"]}
        mutations = {
            "ans-001": {"citationIds": ["citation-invented"]},
            "ans-003": {"tool": "lambda.create_review_task"},
            "amb-001": {"responseClass": "answerable"},
            "cross-001": {"access": "allow"},
            "mal-001": {"refusal": False},
            "adv-001": {"escalation": False},
        }
        for case in source["cases"]:
            if case["id"] in mutations:
                case["expected"].update(mutations[case["id"]])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mutated.json"
            path.write_text(json.dumps(source), encoding="utf-8")
            mutated = evaluate_dataset(path)
        for item in mutated["cases"]:
            if item["caseId"] in mutations:
                actual = dict(item["actual"])
                baseline_actual_without_latency = dict(baseline_actual[item["caseId"]])
                actual.pop("latencyMs", None)
                baseline_actual_without_latency.pop("latencyMs", None)
                self.assertEqual(actual, baseline_actual_without_latency)
                self.assertFalse(item["passed"], item["caseId"])


if __name__ == "__main__":
    unittest.main()
