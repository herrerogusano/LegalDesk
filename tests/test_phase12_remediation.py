from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from evals.phase12_remediation_runner import MAX_REAL_MODEL_INVOCATIONS, build_real_run_plan
from evals.synthetic_debug import run_synthetic_debug


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


if __name__ == "__main__":
    unittest.main()
