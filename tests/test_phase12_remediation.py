from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from evals.phase12_remediation_runner import (
    DEFAULT_OUTPUT,
    EXPECTED_PROMPT_SHA256,
    EXPECTED_PROMPT_VERSION,
    MAX_REAL_MODEL_INVOCATIONS,
    MODEL_ID,
    REGION,
    assert_output_path,
    build_real_run_plan,
    preflight_real_run,
    run_real_evaluation,
)
from evals.synthetic_debug import SYNTHETIC_CASES, run_synthetic_debug


class Phase12RemediationTests(unittest.TestCase):
    def test_real_plan_is_nine_samples_and_eighteen_invocations(self) -> None:
        plan = build_real_run_plan()
        self.assertEqual(len(plan), MAX_REAL_MODEL_INVOCATIONS)
        self.assertEqual(sum(item["stage"] == "resolver" for item in plan), 9)
        self.assertEqual(sum(item["stage"] == "writer" for item in plan), 9)
        self.assertEqual(len({item["caseId"] for item in plan}), 9)
        self.assertTrue(all(item["maxAttempts"] == 1 and item["retries"] == 0 for item in plan))

    def test_debug_runner_requires_opt_in_and_writes_full_synthetic_report(self) -> None:
        with patch.dict(os.environ, {"EVAL_DEBUG_SYNTHETIC": "true"}):
            with tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / "report.json"
                # Output must remain under evals-local fixtures, so use a
                # temporary in-repo output instead of external paths.
                local_output = Path("evals") / "phase12-debug-test-report.json"
                try:
                    report = run_synthetic_debug(local_output)
                    self.assertEqual(report["awsCalls"], 0)
                    self.assertEqual(report["inferenceCalls"], 0)
                    self.assertEqual(report["totalCases"], 9)
                    encoded = json.dumps(report)
                    self.assertNotIn("cot", encoded.casefold())
                    self.assertNotIn("secret", encoded.casefold())
                    for case in report["cases"]:
                        for key in ("caseId", "syntheticQuestion", "syntheticPassages", "rawResolverOutput", "normalizedResolution", "rawWriterOutput", "finalResult", "citationIds", "groundingResult", "promptVersion", "promptHash", "model", "validationCodes", "errorCodes"):
                            self.assertIn(key, case)
                        if case["category"] == "injection":
                            self.assertEqual(case["normalizedResolution"]["coverage"], "complete")
                            self.assertTrue(case["citationIds"])
                finally:
                    local_output.unlink(missing_ok=True)
        with patch.dict(os.environ, {"EVAL_DEBUG_SYNTHETIC": "false"}):
            with self.assertRaises(RuntimeError):
                run_synthetic_debug()

    def test_real_preflight_is_fixed_and_cost_free(self) -> None:
        preflight = preflight_real_run(DEFAULT_OUTPUT)
        self.assertEqual(preflight["region"], REGION)
        self.assertEqual(preflight["model"], MODEL_ID)
        self.assertEqual(preflight["promptVersion"], EXPECTED_PROMPT_VERSION)
        self.assertEqual(preflight["promptHash"], EXPECTED_PROMPT_SHA256)
        self.assertEqual(preflight["maxModelInvocations"], 18)
        self.assertEqual(preflight["retryCount"], 0)
        self.assertTrue(preflight["structuredOutput"])

    def test_real_execution_requires_execute_and_explicit_preflight(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "--execute"):
            run_real_evaluation()
        with self.assertRaisesRegex(RuntimeError, "preflight"):
            run_real_evaluation(execute=True)

    def test_real_output_path_cannot_escape_results_or_overwrite_history(self) -> None:
        with self.assertRaises(ValueError):
            assert_output_path(Path("outside-real-report.json"))
        with self.assertRaises(ValueError):
            assert_output_path(Path("evals/results/phase12-remediation-synthetic-report.json"))

    def test_real_execution_never_overwrites_its_own_report(self) -> None:
        output = Path("evals/results/phase12-existing-real-test.json")
        output.write_text("existing evidence", encoding="utf-8")
        try:
            with self.assertRaisesRegex(RuntimeError, "never overwrite"):
                run_real_evaluation(
                    client=object(),  # type: ignore[arg-type]
                    output_path=output,
                    execute=True,
                    preflight=True,
                )
            self.assertEqual(output.read_text(encoding="utf-8"), "existing evidence")
        finally:
            output.unlink(missing_ok=True)

    def test_real_provider_failure_is_fail_closed_without_retry(self) -> None:
        class FailingClient:
            calls = 0

            def converse(self, **kwargs: object):
                self.calls += 1
                raise RuntimeError("provider failure that must not be persisted")

        client = FailingClient()
        output = Path("evals/results/phase12-real-failure-test.json")
        try:
            report = run_real_evaluation(client=client, output_path=output, execute=True, preflight=True)
            self.assertEqual(client.calls, 9)
            self.assertEqual(report["inferenceCalls"], 9)
            self.assertEqual(report["retryCount"], 0)
            self.assertEqual(report["acceptedCases"], 0)
            self.assertNotIn("provider failure", json.dumps(report).casefold())
            self.assertTrue(all(not case["writerCalled"] for case in report["cases"]))
        finally:
            output.unlink(missing_ok=True)

    def test_real_happy_path_uses_exactly_eighteen_calls_and_metadata_only_report(self) -> None:
        class FixtureClient:
            def __init__(self) -> None:
                self.calls = 0

            def converse(self, **kwargs: object):
                case = SYNTHETIC_CASES[self.calls // 2]
                stage = "resolver" if self.calls % 2 == 0 else "writer"
                self.calls += 1
                payload = case[stage]
                return {
                    "output": {
                        "message": {
                            "content": [{"text": json.dumps(payload)}],
                        }
                    }
                }

        client = FixtureClient()
        output = Path("evals/results/phase12-real-success-test.json")
        try:
            report = run_real_evaluation(
                client=client,
                output_path=output,
                execute=True,
                preflight=True,
            )
            self.assertEqual(client.calls, 18)
            self.assertEqual(report["inferenceCalls"], 18)
            self.assertEqual(report["acceptedCases"], 9)
            encoded = json.dumps(report)
            for case in SYNTHETIC_CASES:
                self.assertNotIn(str(case["question"]), encoded)
                self.assertNotIn(str(case["passages"][0]["text"]), encoded)
                self.assertNotIn(str(case["writer"]["answer"]), encoded)
        finally:
            output.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
