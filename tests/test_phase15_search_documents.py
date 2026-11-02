"""Phase 15 T2: search_documents Lambda target (grounded pipeline behind a grant)."""

from __future__ import annotations

import copy
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "backend" / "src"))
sys.path.insert(0, str(ROOT / "agent" / "src"))

from fixture_loader import load_authorization_store
from legaldesk import agent_contracts as contracts
from legaldesk import search_documents_lambda as lambda_module
from legaldesk.documents import InMemoryDocumentMetadataRepository
from legaldesk.domain.models import Document, DocumentStatus, MalwareScanStatus
from legaldesk.gateway_interceptor import GatewayAuthorizationGrant, InMemoryGatewayGrantRepository
from legaldesk.guardrails import GuardrailConfig
from legaldesk.prompts import FileSystemSystemPromptProvider
from test_chat import (
    FakeAnswerWriter,
    FakeEvidenceResolver,
    FakeGroundingValidator,
    FakeGuardrailClient,
    FakeKnowledgeBaseClient,
    result,
)

SUBJECT = "idp|alice-fictional"
CORRELATION = "8ec5d1c5-7b58-4bc2-a183-8fd48a3bd279"
GRANT_ID = "9ec5d1c5-7b58-4bc2-a183-8fd48a3bd279"
PASSAGE = "The deadline is 17 days."


class SearchDocumentsLambdaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.grants = InMemoryGatewayGrantRepository()
        self.put_grant()
        self.retrieval = FakeKnowledgeBaseClient([result("tnt_aurora", "mat_sundial", "doc-one", PASSAGE, document_name="A.pdf")])
        self.guardrail = FakeGuardrailClient()
        self.writer = FakeAnswerWriter("El plazo documentado es de 17 días.")
        self.metadata = InMemoryDocumentMetadataRepository()
        self.metadata.save(Document(
            document_id="doc-one", matter_id="mat_sundial", tenant_id="tnt_aurora", name="A.pdf",
            s3_key="tnt_aurora/mat_sundial/doc-one.pdf", media_type="application/pdf", jurisdiction="synthetic",
            document_date="2024-01-01", confidentiality="internal", status=DocumentStatus.INDEXED,
            file_size_bytes=10, malware_scan_status=MalwareScanStatus.CLEAN,
        ))
        self.service = lambda_module.SearchDocumentsService(
            authorization_store=load_authorization_store(),
            retrieval_client=self.retrieval,
            knowledge_base_id="kb-fictional",
            guardrail_client=self.guardrail,
            guardrail_config=GuardrailConfig("guardrail-fictional", "1"),
            evidence_resolver=FakeEvidenceResolver(
                {"coverage": "complete", "conflict": False, "supportingCitationIds": ["citation-1"]}
            ),
            answer_writer=self.writer,
            grounding_validator=FakeGroundingValidator(
                {"grounded": True, "score": 0.95, "matchedCitationIds": ["citation-1"]}
            ),
            prompt_provider=FileSystemSystemPromptProvider(),
            metadata_repository=self.metadata,
            object_storage=None,
        )

    def put_grant(self, *, grant_id=GRANT_ID, subject=SUBJECT, matter="mat_sundial", tool="search_documents",
                  expires_at=None) -> None:
        self.grants.put(GatewayAuthorizationGrant(
            grant_id=grant_id, verified_subject=subject, requested_matter_id=matter, correlation_id=CORRELATION,
            tool_name=tool, expires_at=int(time.time()) + 120 if expires_at is None else expires_at,
        ))

    def call(self, event: dict[str, object]):
        with patch.object(lambda_module, "_service_from_environment", return_value=(self.service, self.grants)):
            return lambda_module.search_documents_lambda_handler(event, None)

    def event(self, **overrides) -> dict[str, object]:
        base = {"question": "¿Cuál es el plazo?", "matterId": "mat_sundial", "_legaldeskGrantId": GRANT_ID}
        base.update(overrides)
        return base

    def test_returns_the_grounded_answer_with_citations_and_passages(self) -> None:
        output = self.call(self.event())
        parsed = contracts.parse_search_documents_result(output)
        self.assertEqual(parsed.answer, "El plazo documentado es de 17 días.")
        self.assertEqual(parsed.evidence_status, "answerable")
        self.assertEqual(
            [(c.citation_id, c.document_id, c.document_name, c.page_number, c.passage) for c in parsed.citations],
            [("citation-1", "doc-one", "A.pdf", 4, PASSAGE)],
        )
        # The pipeline ran as in the direct path: input and grounding guardrails, one retrieval.
        self.assertEqual(len(self.retrieval.calls), 1)
        self.assertGreaterEqual(len(self.guardrail.calls), 1)

    def test_writes_no_memory_state_or_grants(self) -> None:
        def snapshot():
            return copy.deepcopy({name: getattr(self.grants, name) for name in self.grants.__slots__})

        before = snapshot()
        self.call(self.event())
        self.assertEqual(snapshot(), before)
        service_fields = {name for name in lambda_module.SearchDocumentsService.__slots__}
        self.assertFalse({name for name in service_fields if "memory" in name or "state" in name or "conversation" in name})

    def test_scope_is_the_grants_not_the_models(self) -> None:
        self.call(self.event(matterId="mat_glacier"))
        retrieval_filter = str(self.retrieval.calls[0])
        self.assertIn("mat_sundial", retrieval_filter)
        self.assertNotIn("mat_glacier", retrieval_filter)
        self.assertNotIn("tnt_borealis", retrieval_filter)

    def test_passages_of_another_matter_never_reach_the_result(self) -> None:
        self.retrieval.results = [result("tnt_borealis", "mat_glacier", "doc-foreign", "SECRET-OTHER-MATTER")]
        output = self.call(self.event())
        self.assertNotIn("SECRET-OTHER-MATTER", str(output))
        self.assertEqual(output["citations"], [])
        self.assertEqual(output["evidenceStatus"], "insufficient_evidence")

    def test_grant_for_a_matter_the_subject_cannot_access_is_denied_before_any_retrieval(self) -> None:
        self.put_grant(grant_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", matter="mat_glacier")
        output = self.call(self.event(**{"_legaldeskGrantId": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"}))
        self.assertEqual(output, {"error": "access_denied"})
        self.assertEqual(self.retrieval.calls, [])

    def test_missing_forged_expired_and_foreign_tool_grants_are_denied(self) -> None:
        self.put_grant(grant_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb", expires_at=int(time.time()) - 1)
        self.put_grant(grant_id="cccccccc-cccc-4ccc-8ccc-cccccccccccc", tool="get_extracted_fields")
        for grant in ("dddddddd-dddd-4ddd-8ddd-dddddddddddd", "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
                      "cccccccc-cccc-4ccc-8ccc-cccccccccccc"):
            with self.subTest(grant=grant):
                self.assertEqual(self.call(self.event(**{"_legaldeskGrantId": grant})), {"error": "access_denied"})
        self.assertEqual(self.call({"question": "q", "matterId": "mat_sundial"}), {"error": "access_denied"})
        self.assertEqual(self.retrieval.calls, [])

    def test_unexpected_arguments_and_bad_questions_are_rejected(self) -> None:
        for event in (
            self.event(tenantId="tnt_borealis"), self.event(subject="idp|bob-fictional"),
            self.event(question=""), self.event(question="x" * (contracts.MAX_SEARCH_QUESTION_LENGTH + 1)),
            self.event(question=7), self.event(**{"_legaldeskGrantId": "not-a-uuid"}),
            {**self.event(), "authorizedContext": {"matter": "mat_glacier"}},
        ):
            with self.subTest(event=str(event)[:60]):
                self.assertIn(self.call(event), ({"error": "invalid_request"}, {"error": "access_denied"}))
        self.assertEqual(self.retrieval.calls, [])

    def test_input_guardrail_block_is_not_a_valid_tool_result(self) -> None:
        self.guardrail.responses["INPUT"] = {"action": "GUARDRAIL_INTERVENED", "outputs": [{"text": "x"}], "assessments": []}
        output = self.call(self.event())
        self.assertEqual((output["operationStatus"], output["citations"], output["evidenceStatus"]), ("blocked", [], None))
        self.assertEqual(self.retrieval.calls, [])
        with self.assertRaises(contracts.AgentContractError):
            contracts.parse_search_documents_result(output)

    def test_stale_vector_of_a_deleted_document_fails_closed(self) -> None:
        self.metadata.documents.clear()  # vector still in the KB, document record gone
        output = self.call(self.event())
        self.assertEqual((output["operationStatus"], output["citations"]), ("error", []))
        self.assertNotIn(PASSAGE, str(output))

    def test_unconfigured_environment_fails_closed(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            self.assertIsNone(lambda_module._service_from_environment())
            self.assertEqual(lambda_module.search_documents_lambda_handler(self.event(), None), {"error": "service_unavailable"})


if __name__ == "__main__":
    unittest.main()
