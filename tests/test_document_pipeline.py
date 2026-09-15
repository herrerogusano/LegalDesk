from __future__ import annotations

import sys
import unittest
from pathlib import Path
from uuid import UUID

sys.path.insert(0, str(Path(__file__).parents[1] / "backend" / "src"))

from fixture_loader import load_authorization_store
from legaldesk.authorization import AuthorizationDenied, VerifiedIdentity
from legaldesk.documents import (
    DocumentPipeline,
    Boto3DynamoDocumentMetadataRepository,
    DocumentMetadataError,
    DocumentStorageError,
    DocumentValidationError,
    InMemoryDocumentMetadataRepository,
    InMemoryObjectStorage,
    MAX_DOCUMENT_BYTES,
    UploadRequest,
    build_document_key,
    document_partition_key,
    document_sort_key,
)
from legaldesk.domain.models import DocumentStatus


ALICE = VerifiedIdentity("idp|alice-fictional")
FIXTURE = b"fictional Project Sundial notice; not legal advice."


def request(**overrides: object) -> UploadRequest:
    values: dict[str, object] = {
        "filename": "sundial-notice.txt",
        "media_type": "text/plain",
        "body": FIXTURE,
        "jurisdiction": "fictional-eu",
        "document_date": "2099-01-01",
        "confidentiality": "fictional-internal",
    }
    values.update(overrides)
    return UploadRequest(**values)  # type: ignore[arg-type]


class DocumentPipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.auth = load_authorization_store()
        self.objects = InMemoryObjectStorage()
        self.metadata = InMemoryDocumentMetadataRepository()
        self.pipeline = DocumentPipeline(self.auth, self.objects, self.metadata)

    def test_user_a_uploads_to_matter_a_with_server_scope(self) -> None:
        document = self.pipeline.upload(ALICE, "mat_sundial", request())
        self.assertEqual(document.tenant_id, "tnt_aurora")
        self.assertEqual(document.matter_id, "mat_sundial")
        self.assertEqual(document.status, DocumentStatus.UPLOADED)
        self.assertIn(document.s3_key, self.objects.objects)
        self.assertEqual(self.objects.objects[document.s3_key], FIXTURE)

    def test_user_a_cannot_upload_to_matter_b(self) -> None:
        with self.assertRaises(AuthorizationDenied):
            self.pipeline.upload(ALICE, "mat_glacier", request())
        self.assertEqual(self.objects.objects, {})
        self.assertEqual(self.metadata.documents, {})

    def test_document_id_and_key_are_server_generated(self) -> None:
        document = self.pipeline.upload(
            ALICE,
            "mat_sundial",
            request(filename="evil.pdf", media_type="application/pdf", body=b"%PDF-1.7 fictional"),
        )
        # Filename is metadata only and cannot influence the object key.
        UUID(document.document_id)
        self.assertEqual(
            document.s3_key,
            f"tenants/tnt_aurora/matters/mat_sundial/documents/{document.document_id}/original",
        )
        self.assertNotIn("evil", document.s3_key)

    def test_key_helper_rejects_non_uuid(self) -> None:
        from legaldesk.authorization import RequestContext

        scope = RequestContext("corr", "usr_alice", "tnt_aurora", "mat_sundial", frozenset())
        with self.assertRaises(ValueError):
            build_document_key(scope, "browser-selected-key")

    def test_validation_rejects_type_size_and_path(self) -> None:
        cases = (
            {"media_type": "application/octet-stream"},
            {"body": b"x" * (MAX_DOCUMENT_BYTES + 1)},
            {"filename": "folder/secret.txt"},
            {"filename": "folder\\secret.txt"},
            {"filename": ""},
            {"filename": "notice.pdf"},
            {"filename": "notice.pdf", "media_type": "application/pdf"},
            {"body": bytes([0xFF]), "media_type": "text/plain"},
            {"document_date": "not-a-date"},
            {"confidentiality": "real-client-confidential"},
        )
        for values in cases:
            with self.subTest(values=values):
                with self.assertRaises(DocumentValidationError):
                    self.pipeline.upload(ALICE, "mat_sundial", request(**values))
        self.assertEqual(self.objects.objects, {})

    def test_status_transitions_are_visible(self) -> None:
        document = self.pipeline.upload(ALICE, "mat_sundial", request())
        pending = self.pipeline.mark_status(
            ALICE, "mat_sundial", document.document_id, DocumentStatus.PENDING_INGESTION
        )
        indexed = self.pipeline.mark_status(
            ALICE, "mat_sundial", document.document_id, DocumentStatus.INDEXED
        )
        self.assertEqual(pending.status, DocumentStatus.PENDING_INGESTION)
        self.assertEqual(indexed.status, DocumentStatus.INDEXED)
        with self.assertRaises(DocumentValidationError):
            self.pipeline.mark_status(
                ALICE, "mat_sundial", document.document_id, DocumentStatus.UPLOADED
            )

    def test_listing_is_scoped_to_authorized_matter(self) -> None:
        first = self.pipeline.upload(ALICE, "mat_sundial", request())
        self.assertEqual(self.pipeline.list_documents(ALICE, "mat_sundial"), (first,))
        with self.assertRaises(AuthorizationDenied):
            self.pipeline.list_documents(ALICE, "mat_glacier")

    def test_metadata_has_no_document_body(self) -> None:
        document = self.pipeline.upload(ALICE, "mat_sundial", request())
        stored = self.metadata.documents[("tnt_aurora", "mat_sundial", document.document_id)]
        self.assertFalse(hasattr(stored, "body"))
        self.assertEqual(stored.file_size_bytes, len(FIXTURE))
        self.assertNotIn(FIXTURE.decode(), repr(stored))

    def test_storage_failure_marks_metadata_failed_and_raises(self) -> None:
        objects = InMemoryObjectStorage(fail=True)
        metadata = InMemoryDocumentMetadataRepository()
        pipeline = DocumentPipeline(self.auth, objects, metadata)
        with self.assertRaises(DocumentStorageError):
            pipeline.upload(ALICE, "mat_sundial", request())
        self.assertEqual(len(metadata.documents), 1)
        self.assertEqual(next(iter(metadata.documents.values())).status, DocumentStatus.FAILED)

    def test_metadata_failure_cleans_up_object(self) -> None:
        metadata = InMemoryDocumentMetadataRepository(fail=True)
        pipeline = DocumentPipeline(self.auth, self.objects, metadata)
        with self.assertRaises(DocumentMetadataError):
            pipeline.upload(ALICE, "mat_sundial", request())
        self.assertEqual(self.objects.objects, {})

    def test_cleanup_failure_preserves_public_metadata_error(self) -> None:
        metadata = InMemoryDocumentMetadataRepository(fail=True)
        objects = InMemoryObjectStorage(fail_delete=True)
        pipeline = DocumentPipeline(self.auth, objects, metadata)
        with self.assertRaises(DocumentMetadataError):
            pipeline.upload(ALICE, "mat_sundial", request())
        self.assertEqual(len(objects.objects), 1)

    def test_single_table_key_shape(self) -> None:
        self.assertEqual(document_partition_key("tnt_aurora", "mat_sundial"), "TENANT#tnt_aurora#MATTER#mat_sundial")
        self.assertEqual(document_sort_key("doc-1"), "DOCUMENT#doc-1")

    def test_dynamo_adapter_scopes_documents_filters_prefix_and_paginates(self) -> None:
        first = self.pipeline.upload(ALICE, "mat_sundial", request())
        second = self.pipeline.upload(ALICE, "mat_sundial", request(filename="second.txt"))
        first_item = Boto3DynamoDocumentMetadataRepository._item(first)
        second_item = Boto3DynamoDocumentMetadataRepository._item(second)

        class FakeTable:
            def __init__(self) -> None:
                self.calls: list[dict[str, object]] = []

            def query(self, **kwargs: object) -> dict[str, object]:
                self.calls.append(kwargs)
                if len(self.calls) == 1:
                    return {"Items": [first_item], "LastEvaluatedKey": {"pk": "p", "sk": "s"}}
                return {"Items": [second_item]}

        table = FakeTable()
        repository = Boto3DynamoDocumentMetadataRepository("fictional-table", table=table)
        listed = repository.list_for_scope(tenant_id="tnt_aurora", matter_id="mat_sundial")
        self.assertEqual([item.document_id for item in listed], [first.document_id, second.document_id])
        self.assertEqual(len(table.calls), 2)
        self.assertIn("ExclusiveStartKey", table.calls[1])
        self.assertIn("DOCUMENT#", str(table.calls[0]["KeyConditionExpression"]))
        self.assertNotIn("body", first_item)


if __name__ == "__main__":
    unittest.main()
