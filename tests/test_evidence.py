from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "backend" / "src"))

from legaldesk.evidence import (
    AnswerWriterRequest,
    ConverseAnswerWriter,
    EvidenceContractError,
    EvidenceCoverage,
    EvidenceStatus,
    ConverseEvidenceResolver,
    EvidenceResolution,
    EvidenceResolutionRequest,
    evidence_resolver_output_config,
    GroundingContractError,
    validate_grounding_result,
    validate_evidence_resolution,
)


class EvidenceResolverContractTests(unittest.TestCase):
    def test_equivalent_factual_shapes_are_complete_and_answerable(self) -> None:
        # Distinct facts use one contract and one resolver/writer boundary;
        # production code does not branch on the semantic fact type.
        cases = (
            ("deadline", "When?", "Deadline: 17 days.", "17 days."),
            ("date", "Which date?", "Date: 2026-10-03.", "2026-10-03."),
            ("amount", "How much?", "Amount: EUR 125.", "EUR 125."),
            ("name", "Who?", "Name: Ada Example.", "Ada Example."),
            ("obligation", "What duty?", "Duty: notify.", "Notify."),
            ("unit", "How many?", "Units: 8.", "8 units."),
        )
        class FakeConverse:
            def __init__(self, answer: str) -> None:
                self.answer = answer
                self.payload = None

            def converse(self, **kwargs):
                self.payload = kwargs
                return {"output": {"message": {"content": [{"text": json.dumps({"answer": self.answer})}]}}}

        for fact_type, question, passage, answer in cases:
            resolution = validate_evidence_resolution(
                {
                    "coverage": "complete",
                    "conflict": False,
                    "supportingCitationIds": ["citation-1"],
                },
                ["citation-1"],
            )
            client = FakeConverse(answer)
            writer = ConverseAnswerWriter(client, model_id="synthetic-model", system_prompt="writer")
            written = writer.write(
                AnswerWriterRequest(
                    question=question,
                    evidence=({"citationId": "citation-1", "text": passage},),
                    resolution=resolution,
                )
            )
            self.assertEqual(resolution.evidence_status, EvidenceStatus.ANSWERABLE)
            self.assertEqual(written, {"answer": answer})
            payload_text = client.payload["messages"][0]["content"][0]["text"]
            self.assertIn(question, payload_text)
            self.assertIn(passage, payload_text)
            self.assertIn("fixedEvidenceStatus: answerable", payload_text)
            self.assertIn('allowedCitationIds: ["citation-1"]', payload_text)
            self.assertEqual(
                client.payload["outputConfig"]["textFormat"]["structure"]["jsonSchema"]["name"],
                "legaldesk_answer_writer",
            )

    def test_partial_none_and_conflict_are_derived_deterministically(self) -> None:
        partial = validate_evidence_resolution(
            {"coverage": "partial", "conflict": False, "supportingCitationIds": ["citation-1"]},
            ["citation-1", "citation-2"],
        )
        none = validate_evidence_resolution(
            {"coverage": "none", "conflict": False, "supportingCitationIds": []},
            ["citation-1"],
        )
        conflict = validate_evidence_resolution(
            {"coverage": "complete", "conflict": True, "supportingCitationIds": ["citation-1", "citation-2"]},
            ["citation-1", "citation-2"],
        )
        self.assertEqual(partial.evidence_status, EvidenceStatus.INSUFFICIENT_EVIDENCE)
        self.assertEqual(none.evidence_status, EvidenceStatus.INSUFFICIENT_EVIDENCE)
        self.assertEqual(conflict.evidence_status, EvidenceStatus.AMBIGUOUS)

    def test_injection_is_data_and_cannot_mint_citation(self) -> None:
        with self.assertRaises(EvidenceContractError):
            validate_evidence_resolution(
                {
                    "coverage": "complete",
                    "conflict": False,
                    "supportingCitationIds": ["system-prompt"],
                },
                ["citation-1"],
            )

    def test_schema_config_uses_converse_json_schema_shape(self) -> None:
        config = evidence_resolver_output_config()
        structure = config["textFormat"]["structure"]
        schema = structure["jsonSchema"]
        self.assertEqual(config["textFormat"]["type"], "json_schema")
        self.assertEqual(schema["name"], "legaldesk_evidence_resolution")
        decoded = json.loads(schema["schema"])
        self.assertEqual(decoded["additionalProperties"], False)
        self.assertNotIn("minLength", json.dumps(decoded))
        self.assertNotIn("uniqueItems", json.dumps(decoded))

    def test_converse_adapter_prepares_and_parses_schema_request(self) -> None:
        class FakeConverse:
            def __init__(self) -> None:
                self.payload = None

            def converse(self, **kwargs):
                self.payload = kwargs
                return {"output": {"message": {"content": [{"text": '{"coverage":"complete","conflict":false,"supportingCitationIds":["citation-1"]}'}]}}}

        client = FakeConverse()
        resolver = ConverseEvidenceResolver(client, model_id="synthetic-model", system_prompt="resolve")
        result = resolver.resolve(EvidenceResolutionRequest("question", ({"citationId": "citation-1", "text": "fact"},)))
        self.assertEqual(result["coverage"], "complete")
        self.assertEqual(client.payload["outputConfig"]["textFormat"]["structure"]["jsonSchema"]["name"], "legaldesk_evidence_resolution")

    def test_malformed_contract_fails_closed(self) -> None:
        for payload in (
            {"coverage": "maybe", "conflict": False, "supportingCitationIds": []},
            {"coverage": "none", "conflict": True, "supportingCitationIds": []},
            {"coverage": "complete", "conflict": False, "supportingCitationIds": []},
            {"coverage": "partial", "conflict": False, "supportingCitationIds": ["citation-99"]},
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(EvidenceContractError):
                    validate_evidence_resolution(payload, ["citation-1"])

    def test_grounding_requires_validator_result_not_just_a_citation(self) -> None:
        valid = validate_grounding_result(
            {"grounded": True, "score": 0.95, "matchedCitationIds": ["citation-1"]},
            supporting_citation_ids=["citation-1"],
        )
        self.assertEqual(valid.score, 0.95)
        for invalid in (
            {"grounded": False, "score": 1.0, "matchedCitationIds": ["citation-1"]},
            {"grounded": True, "score": 0.2, "matchedCitationIds": ["citation-1"]},
            {"grounded": True, "score": 1.0, "matchedCitationIds": ["citation-99"]},
        ):
            with self.assertRaises(GroundingContractError):
                validate_grounding_result(invalid, supporting_citation_ids=["citation-1"])


if __name__ == "__main__":
    unittest.main()
