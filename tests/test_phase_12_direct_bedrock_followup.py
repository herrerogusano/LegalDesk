from __future__ import annotations

import json
import sys
import unittest
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT))

from evals.direct_bedrock_followup import (  # noqa: E402
    CURRENT_PROMPT_SHA256,
    FOLLOW_UP_CASES,
    HISTORICAL_REPORT_PATH,
    MAX_MODEL_INVOCATIONS,
    run_follow_up,
)
from evals.direct_bedrock_smoke import (  # noqa: E402
    CASES,
    INJECTION_CANARY,
    _parse_and_validate,
)
from legaldesk.prompts import FileSystemSystemPromptProvider  # noqa: E402
from legaldesk.prompts import SystemPromptArtifact  # noqa: E402


def _response(answer: str, status: str = "answerable") -> dict[str, object]:
    return {"output": {"message": {"content": [{"text": json.dumps({"answer": answer, "citationIds": ["citation-1"], "evidenceStatus": status})}]}}}


def _historical_prompt_provider():
    artifact = SystemPromptArtifact(
        prompt_id="legaldesk-system",
        version="1.2.0",
        content="historical frozen prompt artifact",
        sha256=CURRENT_PROMPT_SHA256,
    )
    return type("HistoricalPromptProvider", (), {"load": lambda _self: artifact})()


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


class DirectBedrockFollowupTests(unittest.TestCase):
    def test_historical_report_overwrite_is_blocked_before_client_creation(self) -> None:
        called = False

        def forbidden_factory(_region: str):
            nonlocal called
            called = True
            raise AssertionError("client must not be created")

        with self.assertRaises(RuntimeError):
            run_follow_up(client_factory=forbidden_factory, output_path=HISTORICAL_REPORT_PATH)
        self.assertFalse(called)

    def test_prompt_hash_mismatch_is_blocked_before_client_creation(self) -> None:
        loaded = FileSystemSystemPromptProvider().load()
        wrong_prompt = SystemPromptArtifact(
            prompt_id=loaded.prompt_id,
            version="1.3.0",
            content=loaded.content,
            sha256="0" * 64,
        )
        called = False

        def forbidden_factory(_region: str):
            nonlocal called
            called = True
            raise AssertionError("client must not be created")

        with self.assertRaises(RuntimeError):
            run_follow_up(
                client_factory=forbidden_factory,
                prompt_provider=type("WrongPromptProvider", (), {"load": lambda _self: wrong_prompt})(),
            )
        self.assertFalse(called)

    def test_approved_prompt_hash_is_the_exact_final_contract(self) -> None:
        with self.assertRaises(RuntimeError):
            run_follow_up(client_factory=lambda _region: _FakeBedrock([]), prompt_provider=FileSystemSystemPromptProvider())

    def test_answerable_disclaimer_does_not_trigger_literal_gate(self) -> None:
        prompt = _historical_prompt_provider().load()
        answer = "The deadline is 17 days. You are not a lawyer and do not create an attorney-client relationship."
        parsed = _parse_and_validate(json.dumps({"answer": answer, "citationIds": ["citation-1"], "evidenceStatus": "answerable"}), CASES[0], prompt)
        self.assertEqual(parsed["validation"], "valid")
        self.assertTrue(parsed["groundingCheck"])

    def test_injection_canary_and_literal_prompt_variants_fail_closed(self) -> None:
        prompt = SystemPromptArtifact(
            prompt_id="legaldesk-system",
            version="1.2.0",
            content="alpha beta gamma delta epsilon zeta eta theta iota kappa",
            sha256=CURRENT_PROMPT_SHA256,
        )
        fragment = " ".join(prompt.content.split()[:8])
        injection = FOLLOW_UP_CASES[1]
        for answer in (f"17 days. {INJECTION_CANARY}", f"17 days. {fragment}", "17 days. Here is the system prompt:"):
            parsed = _parse_and_validate(json.dumps({"answer": answer, "citationIds": ["citation-1"], "evidenceStatus": "answerable"}), injection, prompt)
            self.assertEqual(parsed["validation"], "error")
            self.assertFalse(parsed["groundingCheck"])

    def test_invalid_json_fails_closed(self) -> None:
        invalid_response = {"output": {"message": {"content": [{"text": "not-json"}]}}}
        client = _FakeBedrock([invalid_response, invalid_response])
        report = run_follow_up(client_factory=lambda _region: client, prompt_provider=_historical_prompt_provider())
        self.assertEqual(report["acceptedCases"], 0)
        self.assertTrue(all(item["errorCode"] == "invalid_json" for item in report["cases"]))

    def test_cap_validation_happens_before_client_creation(self) -> None:
        invalid_cases = (FOLLOW_UP_CASES[0], replace(FOLLOW_UP_CASES[1], evidence=()))
        called = False

        def forbidden_factory(_region: str):
            nonlocal called
            called = True
            raise AssertionError("client must not be created")

        with self.assertRaises(RuntimeError):
            run_follow_up(client_factory=forbidden_factory, cases=invalid_cases)
        self.assertFalse(called)

    def test_provider_exceptions_use_exactly_two_calls_and_zero_retries(self) -> None:
        client = _FakeBedrock([RuntimeError("one"), RuntimeError("two")])
        report = run_follow_up(client_factory=lambda _region: client, prompt_provider=_historical_prompt_provider())
        self.assertEqual(len(client.calls), MAX_MODEL_INVOCATIONS)
        self.assertEqual(report["invocationsAttempted"], MAX_MODEL_INVOCATIONS)
        self.assertEqual(report["retryCount"], 0)
        self.assertEqual(report["acceptedModelCases"], 0)
        self.assertEqual(report["acceptedCases"], 0)

    def test_expected_mutation_does_not_change_payload_or_actual(self) -> None:
        responses = [_response("17 days."), _response("17 days.")]
        baseline_client = _FakeBedrock(responses)
        baseline = run_follow_up(client_factory=lambda _region: baseline_client, prompt_provider=_historical_prompt_provider())
        mutated_cases = (replace(FOLLOW_UP_CASES[0], expected_status="insufficient_evidence", expected_citations=()), FOLLOW_UP_CASES[1])
        mutated_client = _FakeBedrock(responses)
        mutated = run_follow_up(client_factory=lambda _region: mutated_client, cases=mutated_cases, prompt_provider=_historical_prompt_provider())
        self.assertEqual(baseline_client.calls, mutated_client.calls)
        self.assertEqual(baseline["cases"][0]["evidenceStatus"], mutated["cases"][0]["evidenceStatus"])
        self.assertTrue(baseline["cases"][0]["passed"])
        self.assertFalse(mutated["cases"][0]["passed"])

    def test_report_is_metadata_only_and_bounded(self) -> None:
        client = _FakeBedrock([_response("17 days."), _response("17 days.")])
        report = run_follow_up(client_factory=lambda _region: client, prompt_provider=_historical_prompt_provider())
        encoded = json.dumps(report)
        for forbidden in ("What is the fictional payment deadline?", "17 days.", "Ignore prior instructions", INJECTION_CANARY):
            self.assertNotIn(forbidden, encoded)
        self.assertEqual(report["mode"], "direct-bedrock-followup-bounded")
        self.assertEqual(report["maxModelInvocations"], 2)


if __name__ == "__main__":
    unittest.main()
