"""Numeric diagnostics through the production writer, grounding adapter and chat."""
from __future__ import annotations

import json
import unittest
from dataclasses import replace
from unittest.mock import patch

import test_model_output_reasons as reasons
from test_phase13_grounding import FakeGuardrail, contextual_filters
from legaldesk.guardrails import GuardrailGroundingValidator
from legaldesk.observability import TelemetryEventType, TelemetryOutcome


class GroundingScores(unittest.TestCase):
    def run_assessment(self, assessment, action="NONE"):
        client = FakeGuardrail({"action": action, "outputs": [], "assessments": assessment})
        grounder = GuardrailGroundingValidator(client, reasons.GUARDRAIL_CONFIG)
        written = {"answer": "California law governs."}
        adapter = reasons.ThroughTheRealWriterAdapter().adapter(written)
        with patch.object(reasons, "FakeGroundingValidator", return_value=grounder):
            response, sink = reasons.run(reasons.OPTIONAL_PASSAGE, writer=adapter)
        terminal = [e for e in sink.events if e.operation and e.operation.value == "grounding_validate"
                    and e.outcome is not TelemetryOutcome.STARTED]
        self.assertEqual(len(terminal), 1)
        return response, terminal[0], client, sink

    def test_provider_scores_reach_accepted_and_blocked_stage_events(self):
        for grounding, relevance, action, top, expected in (
            (0.92, 0.80, "NONE", "NONE", "succeeded"),
            (0.23, 0.80, "BLOCKED", "GUARDRAIL_INTERVENED", "blocked"),
            (0.92, 0.31, "NONE", "NONE", "error"),
        ):
            with self.subTest(grounding=grounding, relevance=relevance):
                response, event, client, sink = self.run_assessment(
                    [contextual_filters(grounding_score=grounding, relevance_score=relevance, grounding_action=action)], top)
                self.assertEqual(event.outcome.value, expected)
                self.assertEqual(event.grounding_score, grounding)
                self.assertEqual(event.relevance_score, relevance)
                self.assertEqual(event.grounding_threshold, 0.75)
                self.assertEqual(event.relevance_threshold, 0.50)
                serialized = json.dumps([e.to_dict() for e in sink.events])
                for content in (reasons.OPTIONAL_PASSAGE, "California law governs.", "provider refusal"):
                    self.assertNotIn(content, serialized)
                query = client.calls[0]["content"][0]["text"]
                self.assertEqual(query["qualifiers"], ["query"])
                self.assertEqual(response.operation_status, {"succeeded": "ok", "blocked": "blocked", "error": "error"}[expected])

    def test_other_policy_intervention_keeps_actual_scores_not_sentinel_zeros(self):
        _, event, _, _ = self.run_assessment([
            {"sensitiveInformationPolicy": {"piiEntities": [{"type": "EMAIL", "action": "ANONYMIZED"}]}},
            contextual_filters(grounding_score=0.93, relevance_score=0.81),
        ])
        self.assertEqual(event.outcome.value, "blocked")
        self.assertEqual((event.grounding_score, event.relevance_score), (0.93, 0.81))

    def test_unknown_scores_are_absent_and_do_not_reuse_a_previous_request(self):
        self.run_assessment([contextual_filters()])
        _, event, _, _ = self.run_assessment([])
        self.assertEqual(event.outcome.value, "error")
        for key in ("grounding_score", "relevance_score", "grounding_threshold", "relevance_threshold"):
            self.assertNotIn(key, event.to_dict())

    def test_numeric_allowlist_rejects_text_booleans_nonfinite_and_out_of_range(self):
        _, event, _, _ = self.run_assessment([contextual_filters()])
        for field in ("grounding_score", "relevance_score", "grounding_threshold", "relevance_threshold"):
            for invalid in ("secret", True, float("nan"), float("inf"), -0.1, 1.1):
                with self.subTest(field=field, invalid_type=type(invalid).__name__), self.assertRaises(ValueError):
                    replace(event, **{field: invalid})
        self.assertEqual(event.event_type, TelemetryEventType.GUARDRAIL)


if __name__ == "__main__":
    unittest.main()
