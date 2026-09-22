from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any, Mapping

sys.path.insert(0, str(Path(__file__).parents[1] / "backend" / "src"))

from fixture_loader import load_authorization_store, test_identity
from legaldesk.authorization import AuthorizationDenied, VerifiedIdentity, build_request_context
from legaldesk.retrieval import (
    build_matter_filter,
    search_legal_documents,
)


ALICE = test_identity("idp|alice-fictional")
BOB = test_identity("idp|bob-fictional")


def result(
    tenant_id: str,
    matter_id: str,
    document_id: str,
    text: str,
    *,
    page: int = 2,
    section: str = "Payment terms",
    document_name: str | None = None,
) -> dict[str, Any]:
    metadata = {
        "tenantId": tenant_id,
        "matterId": matter_id,
        "documentId": document_id,
        "mediaType": "application/pdf",
        "jurisdiction": "fictional",
        "confidentiality": "fictional-internal",
        "x-amz-bedrock-kb-document-page-number": page,
        "section": section,
    }
    if document_name is not None:
        metadata["documentName"] = document_name
    return {
        "content": {"type": "TEXT", "text": text},
        "location": {"s3Location": {"uri": f"s3://fictional/{document_id}.pdf"}},
        "metadata": metadata,
        "score": 0.87,
    }


class FakeKnowledgeBaseClient:
    def __init__(self, results: list[Mapping[str, Any]] | None = None) -> None:
        self.results = results or []
        self.calls: list[dict[str, Any]] = []

    def retrieve(self, **kwargs: Any) -> Mapping[str, Any]:
        self.calls.append(kwargs)
        return {"retrievalResults": self.results}


class RetrievalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.auth = load_authorization_store()

    def search(self, identity: VerifiedIdentity, matter_id: str, client: FakeKnowledgeBaseClient):
        return search_legal_documents(
            identity,
            matter_id,
            "What is the payment deadline?",
            authorization_store=self.auth,
            bedrock_client=client,
            knowledge_base_id="kb-fictional",
            correlation_id="8ec5d1c5-7b58-4bc2-a183-8fd48a3bd279",
        )

    def test_filter_is_mandatory_and_uses_server_derived_scope(self) -> None:
        context = build_request_context(ALICE, "mat_sundial", self.auth)
        self.assertEqual(
            build_matter_filter(context),
            {
                "andAll": [
                    {"equals": {"key": "tenantId", "value": "tnt_aurora"}},
                    {"equals": {"key": "matterId", "value": "mat_sundial"}},
                ]
            },
        )
        client = FakeKnowledgeBaseClient()
        self.search(ALICE, "mat_sundial", client)
        config = client.calls[0]["retrievalConfiguration"]["vectorSearchConfiguration"]
        self.assertEqual(config["filter"], build_matter_filter(context))
        self.assertEqual(config["numberOfResults"], 5)

    def test_alice_retrieves_only_sundial_evidence_and_normalizes_citation(self) -> None:
        client = FakeKnowledgeBaseClient(
            [
                result("tnt_aurora", "mat_sundial", "doc-sundial", "Sundial payment is due in 17 days."),
                result("tnt_borealis", "mat_glacier", "doc-glacier", "Glacier payment is due in 43 days."),
            ]
        )
        passages = self.search(ALICE, "mat_sundial", client)
        self.assertEqual(len(passages), 1)
        self.assertEqual(passages[0].text, "Sundial payment is due in 17 days.")
        self.assertEqual(passages[0].citation.document_id, "doc-sundial")
        self.assertIsNone(passages[0].citation.document_name)
        self.assertEqual(passages[0].citation.page_number, 2)
        self.assertEqual(passages[0].citation.section, "Payment terms")
        self.assertEqual(passages[0].citation.source_uri, "s3://fictional/doc-sundial.pdf")
        self.assertEqual(passages[0].citation.source_metadata["matterId"], "mat_sundial")
        self.assertEqual(passages[0].score, 0.87)

    def test_document_name_is_preserved_when_retrieval_provides_it(self) -> None:
        passage = self.search(
            ALICE,
            "mat_sundial",
            FakeKnowledgeBaseClient(
                [
                    result(
                        "tnt_aurora",
                        "mat_sundial",
                        "doc-sundial",
                        "Payment is due in 17 days.",
                        document_name="Sundial agreement.pdf",
                    )
                ]
            ),
        )[0]
        self.assertEqual(passage.citation.document_name, "Sundial agreement.pdf")

    def test_bob_retrieves_only_glacier_evidence(self) -> None:
        client = FakeKnowledgeBaseClient(
            [
                result("tnt_aurora", "mat_sundial", "doc-sundial", "Sundial evidence."),
                result("tnt_borealis", "mat_glacier", "doc-glacier", "Glacier evidence."),
            ]
        )
        passages = self.search(BOB, "mat_glacier", client)
        self.assertEqual([passage.text for passage in passages], ["Glacier evidence."])
        self.assertEqual(passages[0].citation.source_metadata["tenantId"], "tnt_borealis")

    def test_cross_matter_request_is_denied_before_bedrock_call(self) -> None:
        client = FakeKnowledgeBaseClient(
            [result("tnt_borealis", "mat_glacier", "doc-glacier", "Glacier evidence.")]
        )
        with self.assertRaises(AuthorizationDenied):
            self.search(ALICE, "mat_glacier", client)
        self.assertEqual(client.calls, [])

    def test_empty_results_and_results_missing_scope_fail_closed(self) -> None:
        self.assertEqual(self.search(ALICE, "mat_sundial", FakeKnowledgeBaseClient()), ())
        missing_scope = result("tnt_aurora", "mat_sundial", "doc-one", "Text")
        missing_scope["metadata"] = {"documentId": "doc-one"}
        self.assertEqual(
            self.search(ALICE, "mat_sundial", FakeKnowledgeBaseClient([missing_scope])), ()
        )

    def test_malformed_provider_envelope_is_technical_failure(self) -> None:
        class MalformedClient:
            def retrieve(self, **kwargs: Any) -> Mapping[str, Any]:
                return {"retrievalResults": {"not": "a list"}}

        with self.assertRaises(ValueError):
            self.search(ALICE, "mat_sundial", MalformedClient())  # type: ignore[arg-type]

    def test_non_mapping_provider_response_is_technical_failure(self) -> None:
        class MalformedClient:
            def retrieve(self, **kwargs: Any) -> Mapping[str, Any]:
                return []  # type: ignore[return-value]

        with self.assertRaises(ValueError):
            self.search(ALICE, "mat_sundial", MalformedClient())  # type: ignore[arg-type]

    def test_missing_results_is_not_a_valid_empty_response(self) -> None:
        class MalformedClient:
            def retrieve(self, **kwargs: Any) -> Mapping[str, Any]:
                return {}

        with self.assertRaises(ValueError):
            self.search(ALICE, "mat_sundial", MalformedClient())  # type: ignore[arg-type]

    def test_client_cannot_supply_tenant_or_custom_filter(self) -> None:
        client = FakeKnowledgeBaseClient(
            [result("tnt_aurora", "mat_sundial", "doc-sundial", "Authorized evidence.")]
        )
        with self.assertRaises(TypeError):
            search_legal_documents(
                ALICE,
                "mat_sundial",
                "query",
                authorization_store=self.auth,
                bedrock_client=client,
                knowledge_base_id="kb-fictional",
                tenant_id="tnt_borealis",
            )
        with self.assertRaises(TypeError):
            search_legal_documents(
                ALICE,
                "mat_sundial",
                "query",
                authorization_store=self.auth,
                bedrock_client=client,
                knowledge_base_id="kb-fictional",
                retrieval_filter={"equals": {"key": "matterId", "value": "mat_glacier"}},
            )
        self.assertEqual(client.calls, [])

    def test_filter_builder_rejects_untrusted_context_shape(self) -> None:
        with self.assertRaises(AuthorizationDenied):
            build_matter_filter({"tenant_id": "tnt_borealis", "matter_id": "mat_glacier"})  # type: ignore[arg-type]

    def test_invalid_query_is_rejected_after_authorization_before_bedrock_call(self) -> None:
        client = FakeKnowledgeBaseClient()
        with self.assertRaises(ValueError):
            search_legal_documents(
                ALICE,
                "mat_sundial",
                "   ",
                authorization_store=self.auth,
                bedrock_client=client,
                knowledge_base_id="kb-fictional",
            )
        self.assertEqual(client.calls, [])


if __name__ == "__main__":
    unittest.main()
