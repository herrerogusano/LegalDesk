from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parents[1] / "backend" / "src"))

from legaldesk.evidence import GroundingContractError, GroundingRequest, validate_grounding_result
from legaldesk.guardrails import (
    GroundingGuardrailBlocked,
    GroundingGuardrailError,
    GuardrailConfig,
    GuardrailGroundingValidator,
)


class FakeGuardrail:
    def __init__(self, response: object = None, error: Exception | None = None) -> None:
        self.response = response
        self.error = error
        self.calls: list[dict[str, object]] = []

    def apply_guardrail(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.response


def request() -> GroundingRequest:
    return GroundingRequest(
        question="Which party must send notice?",
        answer="The supplier must send notice.",
        evidence=(SimpleNamespace(text="The supplier must send written notice."),),
        supporting_citation_ids=("c-1",),
        correlation_id="11111111-1111-4111-8111-111111111111",
    )


def contextual_filters(
    *,
    grounding_score: float = 0.92,
    relevance_score: float = 0.80,
    grounding_action: str = "NONE",
    relevance_action: str = "NONE",
) -> dict[str, object]:
    return {
        "contextualGroundingPolicy": {
            "filters": [
                {
                    "type": "GROUNDING",
                    "score": grounding_score,
                    "threshold": 0.75,
                    "action": grounding_action,
                },
                {
                    "type": "RELEVANCE",
                    "score": relevance_score,
                    "threshold": 0.50,
                    "action": relevance_action,
                },
            ]
        }
    }


class GroundingAdapterTests(unittest.TestCase):
    def test_provider_contextual_assessment_is_accepted(self) -> None:
        client = FakeGuardrail({
            "action": "NONE",
            "outputs": [],
            "assessments": [
                {"invocationMetrics": {"guardrailProcessingLatency": 4}},
                {"appliedGuardrailDetails": {"guardrailId": "g", "guardrailVersion": "1"}},
                {"contentPolicy": {"filters": []}},
                contextual_filters(),
            ],
        })
        validator = GuardrailGroundingValidator(client, GuardrailConfig("g", "1"))
        result = validator.validate(request())
        self.assertTrue(result["grounded"])
        self.assertEqual(result["matchedCitationIds"], ["c-1"])
        self.assertEqual(client.calls[0]["source"], "OUTPUT")

    def test_low_score_is_grounding_failure_not_lexical_acceptance(self) -> None:
        client = FakeGuardrail({
            "action": "NONE",
            "assessments": [contextual_filters(grounding_score=0.50)],
        })
        raw = GuardrailGroundingValidator(client, GuardrailConfig("g", "1")).validate(request())
        with self.assertRaises(GroundingContractError):
            validate_grounding_result(raw, supporting_citation_ids=("c-1",))

    def test_low_relevance_is_also_a_grounding_failure(self) -> None:
        client = FakeGuardrail({
            "action": "NONE",
            "assessments": [contextual_filters(relevance_score=0.40)],
        })
        raw = GuardrailGroundingValidator(client, GuardrailConfig("g", "1")).validate(request())
        self.assertFalse(raw["grounded"])
        with self.assertRaises(GroundingContractError):
            validate_grounding_result(raw, supporting_citation_ids=("c-1",))

    def test_valid_relevance_is_not_compared_to_grounding_threshold(self) -> None:
        client = FakeGuardrail({
            "action": "NONE",
            "assessments": [contextual_filters(relevance_score=0.60)],
        })
        raw = GuardrailGroundingValidator(client, GuardrailConfig("g", "1")).validate(request())
        result = validate_grounding_result(raw, supporting_citation_ids=("c-1",))
        self.assertEqual(result.score, 0.92)

    def test_missing_or_malformed_assessment_fails_closed(self) -> None:
        for response in ({"action": "NONE", "assessments": []}, {"action": "NONE", "assessments": "bad"}):
            with self.subTest(response=response), self.assertRaises(GroundingGuardrailError):
                GuardrailGroundingValidator(FakeGuardrail(response), GuardrailConfig("g", "1")).validate(request())

    def test_unknown_or_actionless_policy_fails_closed(self) -> None:
        responses = (
            {
                "action": "NONE",
                "assessments": [{"futurePolicy": {"action": "NONE"}}],
            },
            {
                "action": "NONE",
                "assessments": [{"contentPolicy": {"filters": [{"type": "VIOLENCE"}]}}],
            },
        )
        for response in responses:
            with self.subTest(response=response), self.assertRaises(GroundingGuardrailError):
                GuardrailGroundingValidator(FakeGuardrail(response), GuardrailConfig("g", "1")).validate(request())

    def test_missing_or_unknown_contextual_filter_fails_closed(self) -> None:
        responses = (
            {"action": "NONE", "assessments": [{"contextualGroundingPolicy": {"filters": [
                {"type": "GROUNDING", "score": 0.90, "threshold": 0.75, "action": "NONE"}
            ]}}]},
            {"action": "NONE", "assessments": [{"contextualGroundingPolicy": {"filters": [
                {"type": "GROUNDING", "score": 0.90, "threshold": 0.75, "action": "NONE"},
                {"type": "UNEXPECTED", "score": 0.90, "threshold": 0.50, "action": "NONE"},
            ]}}]},
        )
        for response in responses:
            with self.subTest(response=response), self.assertRaises(GroundingGuardrailError):
                GuardrailGroundingValidator(FakeGuardrail(response), GuardrailConfig("g", "1")).validate(request())

    def test_none_action_with_returned_text_fails_closed(self) -> None:
        response = {"action": "NONE", "outputs": [{"text": "must not be trusted"}], "assessments": [contextual_filters()]}
        with self.assertRaises(GroundingGuardrailError):
            GuardrailGroundingValidator(FakeGuardrail(response), GuardrailConfig("g", "1")).validate(request())

    def test_top_level_intervention_and_other_policy_cannot_be_ignored(self) -> None:
        responses = (
            {"action": "GUARDRAIL_INTERVENED", "outputs": [{"text": "provider refusal"}], "assessments": [{"contextualGroundingPolicy": {"filters": [
                {"type": "GROUNDING", "score": 0.99, "threshold": 0.75, "action": "NONE"},
                {"type": "RELEVANCE", "score": 0.99, "threshold": 0.50, "action": "NONE"},
            ]}}]},
            {"action": "NONE", "assessments": [
                {"sensitiveInformationPolicy": {"piiEntities": [{"type": "EMAIL", "action": "ANONYMIZED"}]}},
                {"contextualGroundingPolicy": {"filters": [
                    {"type": "GROUNDING", "score": 0.99, "threshold": 0.75, "action": "NONE"},
                    {"type": "RELEVANCE", "score": 0.99, "threshold": 0.50, "action": "NONE"},
                ]}},
            ]},
        )
        for response in responses:
            with self.subTest(response=response), self.assertRaises(GroundingGuardrailBlocked):
                GuardrailGroundingValidator(FakeGuardrail(response), GuardrailConfig("g", "1")).validate(request())

    def test_provider_failure_is_operational_error(self) -> None:
        with self.assertRaises(GroundingGuardrailError):
            GuardrailGroundingValidator(
                FakeGuardrail(error=RuntimeError("provider down")), GuardrailConfig("g", "1")
            ).validate(request())

    def test_provider_limits_are_rejected_before_apply_guardrail(self) -> None:
        oversized = GroundingRequest(
            question="q" * 1_001,
            answer="answer",
            evidence=request().evidence,
            supporting_citation_ids=("c-1",),
        )
        client = FakeGuardrail()
        with self.assertRaises(GroundingGuardrailError):
            GuardrailGroundingValidator(client, GuardrailConfig("g", "1")).validate(oversized)
        self.assertEqual(client.calls, [])


if __name__ == "__main__":
    unittest.main()
