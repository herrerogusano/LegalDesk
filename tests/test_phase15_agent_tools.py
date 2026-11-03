"""Phase 15 T2: new read-only agent tools, interceptor targets and allowlist."""

from __future__ import annotations

import json
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "backend" / "src"))
sys.path.insert(0, str(ROOT / "agent" / "src"))

from legaldesk import agent_contracts as contracts
from legaldesk.authorization import InMemoryAuthorizationStore, build_request_context
from legaldesk.documents import InMemoryDocumentMetadataRepository
from legaldesk.domain.models import (
    Document, DocumentStatus, Matter, MatterStatus, MalwareScanStatus, User,
)
from legaldesk.authorization import _gateway_identity_from_verified_subject
from legaldesk.idp.models import (
    DocumentType, EvidenceAnchor, FieldAcceptance, FieldOrigin, FieldPresence,
    IDPExtractionRun, IDPFieldResult, IDPJobStatus,
)
from legaldesk.idp.persistence import InMemoryIDPRepository
from legaldesk.idp.review import IDPReviewService, InMemoryIDPDecisionRepository
from legaldesk.mcp_server import GET_EXTRACTED_FIELDS, MCPServer

TENANT = "tenant-a"
MATTER = "matter-a"
OTHER_MATTER = "matter-b"
OTHER_TENANT_MATTER = "matter-c"
SUBJECT = "agent-subject"
CORRELATION = "11111111-1111-4111-8111-111111111111"
SHA = "a" * 64


def make_document(document_id: str, *, matter: str = MATTER, tenant: str = TENANT) -> Document:
    return Document(
        document_id=document_id, matter_id=matter, tenant_id=tenant, name=f"{document_id}.pdf",
        s3_key=f"{tenant}/{matter}/{document_id}.pdf", media_type="application/pdf", jurisdiction="synthetic",
        document_date="2024-01-01", confidentiality="internal", status=DocumentStatus.INDEXED,
        file_size_bytes=10, malware_scan_status=MalwareScanStatus.CLEAN,
    )


def make_field(name: str, value: object, *, presence=FieldPresence.PRESENT, acceptance=FieldAcceptance.AUTO_ACCEPTED,
               quote: str = "texto sintético") -> IDPFieldResult:
    evidence = () if value is None else (EvidenceAnchor(page=1, quote=quote, content_sha256=SHA),)
    return IDPFieldResult(
        field=name, value=value, presence=presence, origin=FieldOrigin.LITERAL, acceptance=acceptance,
        evidence=evidence,
    )


def make_run(document_id: str, fields: dict[str, IDPFieldResult], *, matter: str = MATTER, tenant: str = TENANT) -> IDPExtractionRun:
    return IDPExtractionRun(
        run_id=f"run-{document_id}", tenant_id=tenant, matter_id=matter, document_id=document_id,
        document_sha256=SHA, document_type=DocumentType.CONTRACT, schema_version="1.0.0", model_id="synthetic",
        prompt_version="synthetic", status=IDPJobStatus.REVIEW_REQUIRED, fields=fields,
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc), source_key=f"{tenant}/{matter}/{document_id}.pdf",
    )


class GetExtractedFieldsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.auth = InMemoryAuthorizationStore(
            users_by_subject={SUBJECT: User("user-a", SUBJECT, frozenset({TENANT}), frozenset({"attorney"}))},
            matters_by_id={
                MATTER: Matter(MATTER, TENANT, "A", frozenset({"user-a"}), MatterStatus.ACTIVE),
                OTHER_MATTER: Matter(OTHER_MATTER, TENANT, "B", frozenset(), MatterStatus.ACTIVE),
            },
        )
        self.metadata = InMemoryDocumentMetadataRepository()
        self.runs = InMemoryIDPRepository()
        for document_id, fields, matter in (
            ("doc-1", {
                "explicit_expiration_date": make_field("explicit_expiration_date", "2026-03-31"),
                "automatic_renewal": make_field("automatic_renewal", False),
                "amount": make_field("amount", 1200, acceptance=FieldAcceptance.PROVISIONAL),
                "governing_law": make_field("governing_law", None, presence=FieldPresence.ABSENT),
                "jurisdiction": make_field("jurisdiction", "Madrid", acceptance=FieldAcceptance.REJECTED),
            }, MATTER),
            ("doc-2", {"effective_date": make_field("effective_date", "2025-01-01", acceptance=FieldAcceptance.HUMAN_CONFIRMED)}, MATTER),
            ("doc-foreign", {"effective_date": make_field("effective_date", "SECRET-OTHER-MATTER")}, OTHER_MATTER),
        ):
            self.metadata.save(make_document(document_id, matter=matter))
            self.runs.save_run(make_run(document_id, fields, matter=matter))
        self.server = MCPServer(
            self.metadata, idp_repository=self.runs, idp_review_service=IDPReviewService(InMemoryIDPDecisionRepository()),
        )
        self.context = build_request_context(
            _gateway_identity_from_verified_subject(SUBJECT), MATTER, self.auth, correlation_id=CORRELATION,
        )

    def call(self, arguments: dict[str, object], *, context=None):
        response = self.server.handle_jsonrpc(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": GET_EXTRACTED_FIELDS, "arguments": arguments}},
            request_context=context or self.context,
        )
        if "error" in response:
            return response
        return json.loads(response["result"]["content"][0]["text"])

    def fields_of(self, payload: dict[str, object], document_id: str) -> dict[str, dict[str, object]]:
        document = next(item for item in payload["documents"] if item["documentId"] == document_id)
        return {item["name"]: item for item in document["fields"]}

    def test_tool_is_listed_with_its_strict_schema(self) -> None:
        response = self.server.handle_jsonrpc({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
        tool = next(item for item in response["result"]["tools"] if item["name"] == GET_EXTRACTED_FIELDS)
        self.assertEqual(tool["inputSchema"], contracts.GET_EXTRACTED_FIELDS_SCHEMA)

    def test_accepted_are_facts_provisional_pending_absent_and_rejected_empty(self) -> None:
        payload = self.call({"documentIds": ["doc-1", "doc-2"]})
        doc1 = self.fields_of(payload, "doc-1")
        self.assertEqual((doc1["explicit_expiration_date"]["status"], doc1["explicit_expiration_date"]["value"]), ("fact", "2026-03-31"))
        # A boolean False is a real value, not an absent one.
        self.assertEqual((doc1["automatic_renewal"]["status"], doc1["automatic_renewal"]["value"]), ("fact", False))
        self.assertEqual((doc1["amount"]["status"], doc1["amount"]["value"]), ("pending_review", 1200))
        for name in ("governing_law", "jurisdiction"):
            self.assertEqual(
                (doc1[name]["status"], doc1[name]["value"], doc1[name]["evidence"]), ("empty", None, []), name,
            )
        self.assertEqual(self.fields_of(payload, "doc-2")["effective_date"]["status"], "fact")
        # The server output satisfies the contract the validator will parse.
        parsed = contracts.parse_extracted_fields_result(payload)
        self.assertEqual(parsed.documents["doc-1"].fields["amount"].evidence[0].page, 1)

    def test_field_names_restrict_output_and_unknown_requested_fields_come_back_empty(self) -> None:
        payload = self.call({"documentIds": ["doc-1"], "fieldNames": ["amount", "claims"]})
        self.assertEqual(set(self.fields_of(payload, "doc-1")), {"amount", "claims"})
        self.assertEqual(self.fields_of(payload, "doc-1")["claims"]["status"], "empty")

    def test_other_matter_and_missing_documents_are_indistinguishable_and_leak_nothing(self) -> None:
        payload = self.call({"documentIds": ["doc-foreign", "doc-missing", "doc-2"]})
        self.assertEqual([item["documentId"] for item in payload["documents"]], ["doc-2"])
        self.assertEqual(payload["notFound"], ["doc-foreign", "doc-missing"])
        self.assertNotIn("SECRET-OTHER-MATTER", json.dumps(payload))

    def test_scope_comes_only_from_the_server_context(self) -> None:
        other = build_request_context(
            _gateway_identity_from_verified_subject(SUBJECT), MATTER, self.auth, correlation_id=CORRELATION,
        )
        for forbidden in ("matterId", "tenantId", "subject"):
            response = self.call({"documentIds": ["doc-1"], forbidden: OTHER_MATTER}, context=other)
            self.assertEqual(response["error"]["code"], -32602, forbidden)

    def test_batch_limit_and_unknown_field_names_are_rejected(self) -> None:
        for arguments in (
            {"documentIds": [f"doc-{index}" for index in range(21)]},
            {"documentIds": []},
            {"documentIds": ["doc-1"], "fieldNames": ["not_an_idp_field"]},
        ):
            with self.subTest(arguments=arguments):
                self.assertEqual(self.call(arguments)["error"]["code"], -32602)

    def test_not_started_idp_documents_return_no_fields(self) -> None:
        self.metadata.save(make_document("doc-new"))
        payload = self.call({"documentIds": ["doc-new"]})
        self.assertEqual(payload["documents"][0]["fields"], [])


if __name__ == "__main__":
    unittest.main()
