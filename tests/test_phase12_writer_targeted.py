from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest.mock import patch

from evals.phase12_writer_targeted import (
    DEFAULT_OUTPUT,
    MAX_MODEL_INVOCATIONS,
    preflight_targeted,
    run_targeted,
)
from evals.synthetic_debug import SYNTHETIC_CASES


class Phase12WriterTargetedTests(unittest.TestCase):
    def test_preflight_is_one_call_metadata_only(self) -> None:
        result = preflight_targeted(DEFAULT_OUTPUT)
        self.assertEqual(result["maxModelInvocations"], 1)
        self.assertEqual(result["retryCount"], 0)
        self.assertEqual(result["targetCaseId"], "synthetic-partial-03")
        self.assertTrue(result["metadataOnly"])

    def test_preflight_rejects_source_report_drift(self) -> None:
        with patch(
            "evals.phase12_writer_targeted.SOURCE_WRITER_REPORT_SHA256",
            "0" * 64,
        ):
            with self.assertRaisesRegex(RuntimeError, "differs"):
                preflight_targeted(DEFAULT_OUTPUT)

    def test_execution_requires_explicit_switches(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "--execute"):
            run_targeted()
        with self.assertRaisesRegex(RuntimeError, "preflight"):
            run_targeted(execute=True)

    def test_happy_path_calls_writer_once_and_persists_no_content(self) -> None:
        target = next(case for case in SYNTHETIC_CASES if case["caseId"] == "synthetic-partial-03")

        class FixtureClient:
            calls = 0

            def converse(self, **kwargs: object):
                self.calls += 1
                return {
                    "output": {
                        "message": {
                            "content": [{"text": json.dumps(target["writer"])}],
                        }
                    }
                }

        client = FixtureClient()
        output = Path("evals/results/phase12-writer-targeted-success-test.json")
        try:
            report = run_targeted(
                client=client,
                output_path=output,
                execute=True,
                preflight=True,
            )
            self.assertEqual(client.calls, MAX_MODEL_INVOCATIONS)
            self.assertEqual(report["acceptedCases"], 1)
            encoded = json.dumps(report)
            self.assertNotIn(str(target["question"]), encoded)
            self.assertNotIn(str(target["passages"][0]["text"]), encoded)
            self.assertNotIn(str(target["writer"]["answer"]), encoded)
        finally:
            output.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
