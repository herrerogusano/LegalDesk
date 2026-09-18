from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT))

from evals.real_smoke import CASES, MAX_INVOCATIONS, _classify, main, run_real_smoke  # noqa: E402
from legaldesk_agent import HarnessInvocationError  # noqa: E402


class _FakeInvoker:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def invoke(self, message: str, *, session_id: str):
        self.calls.append(message)
        return type("Result", (), {"text": "Synthetic metadata MCP response for notice.pdf"})()


class Phase12RealSmokeTests(unittest.TestCase):
    def test_runner_has_exact_cap_no_retries_and_metadata_only_report(self) -> None:
        fake = _FakeInvoker()
        report = run_real_smoke("arn:synthetic", invoker_factory=lambda _arn, _region: fake)
        self.assertEqual(len(CASES), MAX_INVOCATIONS)
        self.assertEqual(len(fake.calls), MAX_INVOCATIONS)
        self.assertEqual(report["invocationsAttempted"], MAX_INVOCATIONS)
        self.assertEqual(report["retryCount"], 0)
        self.assertEqual(report["acceptedCases"], 0)
        encoded = json.dumps(report)
        self.assertNotIn("List the fictional documents", encoded)
        self.assertNotIn("Synthetic metadata MCP response", encoded)
        self.assertEqual(set(report), {"runner", "runnerVersion", "mode", "region", "maxInvocations", "invocationsAttempted", "retryCount", "passedCases", "acceptedCases", "acceptancePolicy", "totalCases", "cases"})
        self.assertTrue(all(set(item) <= {"caseId", "category", "expectedOutcome", "actualOutcome", "passed", "accepted", "acceptanceReason", "tool", "citationLike", "latencyMs", "errorCode"} for item in report["cases"]))

    def test_keywords_negations_and_prompt_echo_are_inconclusive(self) -> None:
        adversarial = (
            "I cannot access document metadata.",
            "This request is not denied; here is no data.",
            "No review is needed.",
            "I will not refuse.",
            CASES[0].message,
            "The prompt said 'denied', 'review', and 'MCP'; this is only an echo.",
        )
        for text in adversarial:
            labels = _classify(CASES[0], text)
            self.assertEqual(labels["outcome"], "inconclusive")
            self.assertIsNone(labels["tool"])
            self.assertIsNone(labels["citationLike"])

    def test_harness_error_consumes_one_attempt_per_case_without_retry(self) -> None:
        class FailingInvoker:
            def __init__(self) -> None:
                self.calls = 0

            def invoke(self, _message: str, *, session_id: str):
                self.calls += 1
                raise HarnessInvocationError("synthetic failure")

        failing = FailingInvoker()
        report = run_real_smoke("arn:synthetic", invoker_factory=lambda _arn, _region: failing)
        self.assertEqual(failing.calls, MAX_INVOCATIONS)
        self.assertEqual(report["invocationsAttempted"], MAX_INVOCATIONS)
        self.assertEqual(report["retryCount"], 0)
        self.assertEqual(report["acceptedCases"], 0)
        self.assertTrue(all(item["actualOutcome"] == "invocation_error" for item in report["cases"]))

    def test_cli_exit_uses_accepted_cases_not_lexical_passes(self) -> None:
        report = {
            "mode": "authorized-real-harness-bounded",
            "region": "eu-west-1",
            "maxInvocations": 4,
            "invocationsAttempted": 4,
            "retryCount": 0,
            "passedCases": 2,
            "acceptedCases": 0,
            "totalCases": 4,
        }
        with patch.object(sys, "argv", ["real_smoke", "--harness-arn", "arn:synthetic"]):
            with patch("evals.real_smoke.run_real_smoke", return_value=report):
                self.assertEqual(main(), 1)

    def test_region_is_fixed_to_eu_west_1(self) -> None:
        with self.assertRaises(ValueError):
            run_real_smoke("arn:synthetic", region="us-east-1", invoker_factory=lambda _arn, _region: _FakeInvoker())


if __name__ == "__main__":
    unittest.main()
