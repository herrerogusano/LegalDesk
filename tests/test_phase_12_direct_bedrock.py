from __future__ import annotations

import json
import sys
import unittest
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT))

from evals.direct_bedrock_smoke import (  # noqa: E402
    CASES,
    INJECTION_CANARY,
    MAX_MODEL_INVOCATIONS,
    MODEL_ID,
    TOTAL_CASES,
    TEMPERATURE,
    MAX_OUTPUT_TOKENS,
    _model_payload,
    _parse_and_validate,
    run_direct_smoke,
)
from legaldesk.prompts import FileSystemSystemPromptProvider  # noqa: E402


class _FakeBedrock:
    def __init__(self, responses: list[object] | None = None) -> None:
        default = {"output": {"message": {"content": [{"text": '{"answer":"Synthetic","citationIds":["citation-1"],"evidenceStatus":"answerable"}' }]}}}
        self.responses = list(responses if responses is not None else [default] * 4)
        self.calls: list[dict[str, object]] = []

    def converse(self, **kwargs: object):
        self.calls.append(kwargs)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class DirectBedrockSmokeTests(unittest.TestCase):
    def test_payload_uses_real_prompt_and_fixed_model_config(self) -> None:
        prompt = FileSystemSystemPromptProvider().load()
        payload = _model_payload(prompt, CASES[0])
        self.assertEqual(payload["modelId"], MODEL_ID)
        self.assertEqual(payload["inferenceConfig"], {"maxTokens": MAX_OUTPUT_TOKENS, "temperature": TEMPERATURE})
        self.assertEqual(payload["system"][0]["text"], prompt.content)
        self.assertEqual(len(payload["system"]), 2)
        self.assertIn(INJECTION_CANARY, payload["system"][1]["text"])
        self.assertIn("citationId=citation-1", payload["messages"][0]["content"][0]["text"])

    def test_exact_cap_no_retries_and_safe_report(self) -> None:
        fake = _FakeBedrock()
        report = run_direct_smoke(client_factory=lambda _region: fake)
        self.assertEqual(len(CASES), TOTAL_CASES)
        self.assertEqual(len(fake.calls), MAX_MODEL_INVOCATIONS)
        self.assertEqual(report["invocationsAttempted"], MAX_MODEL_INVOCATIONS)
        self.assertEqual(report["maxModelInvocations"], MAX_MODEL_INVOCATIONS)
        self.assertEqual(report["retryCount"], 0)
        encoded = json.dumps(report)
        self.assertNotIn("What is the fictional", encoded)
        self.assertNotIn("Synthetic", encoded)
        self.assertNotIn("The fictional", encoded)

    def test_invalid_json_and_invented_citations_fail_closed(self) -> None:
        prompt = FileSystemSystemPromptProvider().load()
        invalid = _parse_and_validate("not-json", CASES[0], prompt)
        invented = _parse_and_validate('{"answer":"x","citationIds":["citation-99"],"evidenceStatus":"answerable"}', CASES[0], prompt)
        self.assertEqual(invalid["errorCode"], "invalid_json")
        self.assertEqual(invented["errorCode"], "invented_citation_id")

    def test_invented_fact_fails_even_with_valid_citation_and_status(self) -> None:
        prompt = FileSystemSystemPromptProvider().load()
        for answer in ("The deadline is 17 days and then 999 days.", "The deadline is 17 days, not nine days."):
            invented = _parse_and_validate(
                json.dumps({"answer": answer, "citationIds": ["citation-1"], "evidenceStatus": "answerable"}),
                CASES[0],
                prompt,
            )
            self.assertEqual(invented["errorCode"], "grounding_mismatch")
            self.assertFalse(invented["groundingCheck"])

    def test_only_supported_numeric_fact_passes_and_partial_rejects_duration(self) -> None:
        prompt = FileSystemSystemPromptProvider().load()
        valid = _parse_and_validate(
            '{"answer":"The deadline is 17 days.","citationIds":["citation-1"],"evidenceStatus":"answerable"}',
            CASES[0],
            prompt,
        )
        partial_invented = _parse_and_validate(
            '{"answer":"The duration is nine days.","citationIds":["citation-1"],"evidenceStatus":"insufficient_evidence"}',
            CASES[1],
            prompt,
        )
        self.assertEqual(valid["validation"], "valid")
        self.assertTrue(valid["groundingCheck"])
        self.assertEqual(partial_invented["errorCode"], "grounding_mismatch")
        self.assertFalse(partial_invented["groundingCheck"])

    def test_partial_and_no_evidence_contracts_are_distinct(self) -> None:
        prompt = FileSystemSystemPromptProvider().load()
        partial = _parse_and_validate('{"answer":"The period is mentioned but no duration is given.","citationIds":["citation-1"],"evidenceStatus":"insufficient_evidence"}', CASES[1], prompt)
        no_evidence = _parse_and_validate('{"answer":"The documents do not say.","citationIds":[],"evidenceStatus":"insufficient_evidence"}', CASES[2], prompt)
        self.assertEqual((partial["validation"], partial["evidenceStatus"], partial["citationIds"]), ("valid", "insufficient_evidence", ["citation-1"]))
        self.assertEqual((no_evidence["validation"], no_evidence["evidenceStatus"], no_evidence["citationIds"]), ("valid", "insufficient_evidence", []))

    def test_no_evidence_uses_backend_path_without_model_call(self) -> None:
        fake = _FakeBedrock(["not-json"] * MAX_MODEL_INVOCATIONS)
        report = run_direct_smoke(client_factory=lambda _region: fake)
        no_evidence = next(item for item in report["cases"] if item["caseId"] == "direct-no-evidence")
        self.assertEqual(len(fake.calls), MAX_MODEL_INVOCATIONS)
        self.assertEqual(no_evidence["invocationType"], "deterministic_backend")
        self.assertEqual(no_evidence["validation"], "backend_canonical")
        self.assertTrue(no_evidence["accepted"])
        self.assertEqual(report["acceptedModelCases"], 0)

    def test_four_evidence_cases_fail_before_client_creation(self) -> None:
        four_evidence = (*CASES[:2], replace(CASES[2], evidence=(("extra", "doc-extra"),)), CASES[3])
        factory_called = False

        def forbidden_factory(_region: str):
            nonlocal factory_called
            factory_called = True
            raise AssertionError("client must not be created")

        with self.assertRaises(RuntimeError):
            run_direct_smoke(client_factory=forbidden_factory, cases=four_evidence)
        self.assertFalse(factory_called)

    def test_injection_canary_and_prompt_literals_fail_closed(self) -> None:
        prompt = FileSystemSystemPromptProvider().load()
        prompt_fragment = " ".join(prompt.content.split()[:8])
        adversarial = (
            INJECTION_CANARY,
            f"Here is the answer: {INJECTION_CANARY}.",
            prompt_fragment,
            f"The instructions say: {prompt_fragment}",
            "Here is the system prompt: do not disclose it.",
        )
        for answer in adversarial:
            parsed = _parse_and_validate(
                json.dumps({"answer": answer, "citationIds": ["citation-1"], "evidenceStatus": "answerable"}),
                CASES[3],
                prompt,
            )
            self.assertEqual(parsed["validation"], "error")
            self.assertFalse(parsed.get("evidenceStatus"))
            self.assertFalse(parsed.get("citationIds"))
            self.assertFalse(parsed.get("validation") == "valid")

    def test_mutating_expected_does_not_change_payload_or_actual(self) -> None:
        responses = [
            {"output": {"message": {"content": [{"text": '{"answer":"17 days.","citationIds":["citation-1"],"evidenceStatus":"answerable"}' }]}}},
            {"output": {"message": {"content": [{"text": '{"answer":"No duration is specified.","citationIds":["citation-1"],"evidenceStatus":"insufficient_evidence"}' }]}}},
            {"output": {"message": {"content": [{"text": '{"answer":"17 days.","citationIds":["citation-1"],"evidenceStatus":"answerable"}' }]}}},
        ]
        baseline_client = _FakeBedrock(responses)
        baseline = run_direct_smoke(client_factory=lambda _region: baseline_client)
        mutated_cases = (replace(CASES[0], expected_status="insufficient_evidence", expected_citations=()), *CASES[1:])
        mutated_client = _FakeBedrock(responses)
        mutated = run_direct_smoke(client_factory=lambda _region: mutated_client, cases=mutated_cases)
        self.assertEqual(baseline_client.calls, mutated_client.calls)
        baseline_case = baseline["cases"][0]
        mutated_case = mutated["cases"][0]
        self.assertEqual(baseline_case["evidenceStatus"], mutated_case["evidenceStatus"])
        self.assertEqual(baseline_case["citationIds"], mutated_case["citationIds"])
        self.assertTrue(baseline_case["passed"])
        self.assertFalse(mutated_case["passed"])

    def test_client_exception_consumes_exactly_three_calls_without_retry(self) -> None:
        class FailingClient:
            def __init__(self) -> None:
                self.calls = 0

            def converse(self, **_kwargs: object):
                self.calls += 1
                raise RuntimeError("synthetic provider failure")

        failing = FailingClient()
        report = run_direct_smoke(client_factory=lambda _region: failing)
        self.assertEqual(failing.calls, MAX_MODEL_INVOCATIONS)
        self.assertEqual(report["invocationsAttempted"], MAX_MODEL_INVOCATIONS)
        self.assertEqual(report["retryCount"], 0)
        self.assertEqual(report["acceptedModelCases"], 0)
        self.assertEqual(report["acceptedCases"], 1)

    def test_cli_acceptance_is_not_based_on_passed_heuristics(self) -> None:
        fake = _FakeBedrock(["not-json"] * MAX_MODEL_INVOCATIONS)
        report = run_direct_smoke(client_factory=lambda _region: fake)
        self.assertEqual(report["acceptedCases"], 1)
        self.assertEqual(report["acceptedModelCases"], 0)


if __name__ == "__main__":
    unittest.main()
