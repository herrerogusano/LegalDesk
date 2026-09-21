from __future__ import annotations

import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT))

from evals.direct_bedrock_final import (  # noqa: E402
    CURRENT_PROMPT_SHA256,
    DEFAULT_OUTPUT,
    FOLLOW_UP_CASES,
    HISTORICAL_REPORT_PATH,
    MAX_MODEL_INVOCATIONS,
    PROMPT_VERSION,
    run_final,
)
from evals.direct_bedrock_smoke import INJECTION_CANARY  # noqa: E402


def _response(answer: str, status: str = "answerable") -> dict[str, object]:
    return {
        "output": {
            "message": {
                "content": [
                    {"text": json.dumps({"answer": answer, "citationIds": ["citation-1"], "evidenceStatus": status})}
                ]
            }
        }
    }


class _FakeBedrock:
    def __init__(self, responses: list[object]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, object]] = []

    def converse(self, **kwargs: object):
        self.calls.append(kwargs)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class DirectBedrockFinalTests(unittest.TestCase):
    def test_final_runner_is_bounded_to_current_prompt_and_metadata_only(self) -> None:
        client = _FakeBedrock([_response("17 days."), _response("17 days.")])
        report = run_final(client_factory=lambda _region: client)
        self.assertEqual(report["promptVersion"], PROMPT_VERSION)
        self.assertEqual(report["promptSha256"], CURRENT_PROMPT_SHA256)
        self.assertEqual(report["maxModelInvocations"], 2)
        self.assertEqual(report["invocationsAttempted"], 2)
        self.assertEqual(report["retryCount"], 0)
        self.assertEqual(report["totalCases"], 2)
        encoded = json.dumps(report)
        for forbidden in (
            "What is the fictional payment deadline?",
            "17 days.",
            "Ignore prior instructions",
            INJECTION_CANARY,
        ):
            self.assertNotIn(forbidden, encoded)

    def test_final_runner_uses_a_distinct_report_path(self) -> None:
        self.assertNotEqual(DEFAULT_OUTPUT.resolve(), HISTORICAL_REPORT_PATH.resolve())
        client = _FakeBedrock([_response("17 days."), _response("17 days.")])
        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = Path(temp_dir) / "final-report.json"
            report = run_final(client_factory=lambda _region: client, output_path=output_path)
            self.assertEqual(report["mode"], "direct-bedrock-final-bounded")
            self.assertTrue(output_path.exists())
            self.assertFalse(HISTORICAL_REPORT_PATH.samefile(output_path))

    def test_historical_output_is_blocked_before_final_client_creation(self) -> None:
        called = False

        def forbidden_factory(_region: str):
            nonlocal called
            called = True
            raise AssertionError("client must not be created")

        with self.assertRaises(RuntimeError):
            run_final(client_factory=forbidden_factory, output_path=HISTORICAL_REPORT_PATH)
        self.assertFalse(called)

    def test_cap_is_checked_before_client_creation(self) -> None:
        called = False

        def forbidden_factory(_region: str):
            nonlocal called
            called = True
            raise AssertionError("client must not be created")

        invalid_cases = (FOLLOW_UP_CASES[0], replace(FOLLOW_UP_CASES[1], evidence=()))
        with self.assertRaises(RuntimeError):
            run_final(client_factory=forbidden_factory, cases=invalid_cases)
        self.assertFalse(called)

    def test_provider_failures_are_two_attempts_without_retry(self) -> None:
        client = _FakeBedrock([RuntimeError("one"), RuntimeError("two")])
        report = run_final(client_factory=lambda _region: client)
        self.assertEqual(len(client.calls), MAX_MODEL_INVOCATIONS)
        self.assertEqual(report["invocationsAttempted"], 2)
        self.assertEqual(report["retryCount"], 0)
        self.assertEqual(report["acceptedCases"], 0)


if __name__ == "__main__":
    unittest.main()
