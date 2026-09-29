from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest.mock import patch

from evals.phase12_writer_followup import (
    DEFAULT_OUTPUT,
    MAX_WRITER_INVOCATIONS,
    SOURCE_RESOLVER_REPORT,
    assert_output_path,
    preflight_writer_followup,
    run_writer_followup,
)
from evals.synthetic_debug import SYNTHETIC_CASES


class Phase12WriterFollowupTests(unittest.TestCase):
    def test_preflight_reuses_pinned_resolver_evidence_and_caps_calls(self) -> None:
        result = preflight_writer_followup(DEFAULT_OUTPUT)
        self.assertEqual(result["maxModelInvocations"], 9)
        self.assertEqual(result["retryCount"], 0)
        self.assertEqual(result["resolverPromptVersion"], "1.2.0")
        self.assertEqual(result["writerPromptVersion"], "1.4.0")
        self.assertTrue(result["metadataOnly"])

    def test_source_report_and_historical_paths_are_immutable(self) -> None:
        with self.assertRaises(ValueError):
            assert_output_path(SOURCE_RESOLVER_REPORT)
        with patch(
            "evals.phase12_writer_followup.SOURCE_RESOLVER_REPORT_SHA256",
            "0" * 64,
        ):
            with self.assertRaisesRegex(RuntimeError, "hash differs"):
                preflight_writer_followup(DEFAULT_OUTPUT)

    def test_execution_requires_both_explicit_switches(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "--execute"):
            run_writer_followup()
        with self.assertRaisesRegex(RuntimeError, "preflight"):
            run_writer_followup(execute=True)

    def test_happy_path_uses_nine_writer_calls_and_metadata_only_report(self) -> None:
        class FixtureClient:
            def __init__(self) -> None:
                self.calls = 0

            def converse(self, **kwargs: object):
                payload = SYNTHETIC_CASES[self.calls]["writer"]
                self.calls += 1
                return {"output": {"message": {"content": [{"text": json.dumps(payload)}]}}}

        client = FixtureClient()
        output = Path("evals/results/phase12-writer-followup-success-test.json")
        try:
            report = run_writer_followup(
                client=client,
                output_path=output,
                execute=True,
                preflight=True,
            )
            self.assertEqual(client.calls, MAX_WRITER_INVOCATIONS)
            self.assertEqual(report["acceptedCases"], 9)
            encoded = json.dumps(report)
            for case in SYNTHETIC_CASES:
                self.assertNotIn(str(case["question"]), encoded)
                self.assertNotIn(str(case["passages"][0]["text"]), encoded)
                self.assertNotIn(str(case["writer"]["answer"]), encoded)
        finally:
            output.unlink(missing_ok=True)

    def test_provider_failures_are_bounded_and_do_not_persist_exception_text(self) -> None:
        class FailingClient:
            def __init__(self) -> None:
                self.calls = 0

            def converse(self, **kwargs: object):
                self.calls += 1
                raise RuntimeError("secret provider detail")

        client = FailingClient()
        output = Path("evals/results/phase12-writer-followup-failure-test.json")
        try:
            report = run_writer_followup(
                client=client,
                output_path=output,
                execute=True,
                preflight=True,
            )
            self.assertEqual(client.calls, 9)
            self.assertEqual(report["acceptedCases"], 0)
            self.assertNotIn("secret provider detail", json.dumps(report))
        finally:
            output.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
