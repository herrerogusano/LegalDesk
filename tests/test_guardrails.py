from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Mapping

sys.path.insert(0, str(Path(__file__).parents[1] / "backend" / "src"))

from legaldesk.guardrails import (
    GuardrailConfig,
    GuardrailOutcome,
    GuardrailProcessor,
)


class StaticGuardrailClient:
    def __init__(self, response: object) -> None:
        self.response = response

    def apply_guardrail(self, **kwargs: object) -> Mapping[str, object]:
        return self.response  # type: ignore[return-value]


class GuardrailResponseParsingTests(unittest.TestCase):
    def check_input(self, response: object):
        processor = GuardrailProcessor(
            StaticGuardrailClient(response),  # type: ignore[arg-type]
            GuardrailConfig("guardrail-fictional", "1"),
        )
        return processor.check_input(
            "alex@example.invalid", correlation_id="8ec5d1c5-7b58-4bc2-a183-8fd48a3bd279"
        )

    def test_conflicting_none_action_and_blocked_assessment_fails_closed(self) -> None:
        result = self.check_input(
            {
                "action": "NONE",
                "outputs": [{"text": "alex@example.invalid"}],
                "assessments": [
                    {
                        "contentPolicy": {
                            "filters": [{"type": "PROMPT_ATTACK", "action": "BLOCKED"}]
                        }
                    }
                ],
            }
        )
        self.assertFalse(result.can_proceed)
        self.assertEqual(result.outcome, GuardrailOutcome.ERROR)
        self.assertEqual(result.content, ())

    def test_anonymization_without_masked_output_fails_closed(self) -> None:
        result = self.check_input(
            {
                "action": "GUARDRAIL_INTERVENED",
                "outputs": [],
                "assessments": [
                    {
                        "sensitiveInformationPolicy": {
                            "piiEntities": [
                                {"type": "EMAIL", "match": "alex@example.invalid", "action": "ANONYMIZED"}
                            ]
                        }
                    }
                ],
            }
        )
        self.assertFalse(result.can_proceed)
        self.assertEqual(result.outcome, GuardrailOutcome.ERROR)
        self.assertEqual(result.content, ())

    def test_malformed_assessments_and_action_values_fail_closed(self) -> None:
        responses = (
            {
                "action": "NONE",
                "outputs": [],
                "assessments": "not-an-assessment-array",
            },
            {
                "action": "NONE",
                "outputs": [],
                "assessments": [{"contentPolicy": {"filters": [{"action": None}]}}],
            },
        )
        for response in responses:
            with self.subTest(response=response):
                result = self.check_input(response)
                self.assertFalse(result.can_proceed)
                self.assertEqual(result.outcome, GuardrailOutcome.ERROR)
                self.assertEqual(result.content, ())

    def test_real_assessment_details_with_scalar_and_list_metadata_are_accepted(self) -> None:
        result = self.check_input(
            {
                "action": "NONE",
                "outputs": [],
                "assessments": [
                    {
                        "appliedGuardrailDetails": {
                            "guardrailId": "guardrail-fictional",
                            "guardrailVersion": "1",
                            "guardrailOrigin": ["create", "version"],
                        },
                        "contentPolicy": {
                            "filters": [
                                {
                                    "type": "PROMPT_ATTACK",
                                    "confidence": "NONE",
                                    "filterStrength": "HIGH",
                                    "detected": False,
                                    "action": "NONE",
                                }
                            ]
                        },
                        "invocationMetrics": {
                            "guardrailProcessingLatency": 1,
                            "guardrailCoverage": {"textCharacters": {"guarded": 4, "total": 4}},
                        },
                    }
                ],
            }
        )
        self.assertTrue(result.can_proceed)
        self.assertEqual(result.outcome, GuardrailOutcome.ALLOWED)
        self.assertEqual(result.content, ("alex@example.invalid",))

    def test_audit_logs_do_not_include_input_or_assessment_content(self) -> None:
        processor = GuardrailProcessor(
            StaticGuardrailClient(
                {
                    "action": "GUARDRAIL_INTERVENED",
                    "outputs": [{"text": "Blocked."}],
                    "assessments": [
                        {
                            "sensitiveInformationPolicy": {
                                "piiEntities": [
                                    {
                                        "type": "EMAIL",
                                        "match": "alex@example.invalid",
                                        "action": "BLOCKED",
                                    }
                                ]
                            }
                        }
                    ],
                }
            ),  # type: ignore[arg-type]
            GuardrailConfig("guardrail-fictional", "1"),
        )
        with self.assertLogs("legaldesk.guardrails", level="INFO") as captured:
            result = processor.check_input(
                "Please contact alex@example.invalid",
                correlation_id="8ec5d1c5-7b58-4bc2-a183-8fd48a3bd279",
            )
        self.assertFalse(result.can_proceed)
        logged = repr(captured.records[0].__dict__)
        self.assertIn("8ec5d1c5-7b58-4bc2-a183-8fd48a3bd279", logged)
        self.assertIn("blocked", logged)
        self.assertNotIn("alex@example.invalid", logged)
        self.assertNotIn("Please contact", logged)
        self.assertNotIn('"match"', logged)


if __name__ == "__main__":
    unittest.main()
