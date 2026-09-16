from __future__ import annotations

import sys
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping

sys.path.insert(0, str(Path(__file__).parents[1] / "backend" / "src"))

from legaldesk.documents import InMemoryDocumentMetadataRepository
from legaldesk.domain.models import Document, DocumentStatus
from legaldesk.ingestion import DocumentScopeRef, run_knowledge_base_sync


TENANT = "tnt_aurora"
MATTER = "mat_sundial"
DOCUMENT_ID = "ecad6ef5-3cdf-40e7-8088-9178adac0037"


def uploaded_document() -> Document:
    return Document(
        document_id=DOCUMENT_ID,
        matter_id=MATTER,
        tenant_id=TENANT,
        name="fictional.txt",
        s3_key=f"tenants/{TENANT}/matters/{MATTER}/documents/{DOCUMENT_ID}/original.txt",
        media_type="text/plain",
        jurisdiction="fictional",
        document_date="2099-01-01",
        confidentiality="fictional-internal",
        status=DocumentStatus.UPLOADED,
        file_size_bytes=100,
    )


def second_uploaded_document() -> Document:
    return Document(
        document_id="acb45e23-05e8-42da-b987-fb9b12345678",
        matter_id="mat_glacier",
        tenant_id="tnt_borealis",
        name="fictional-glacier.txt",
        s3_key="tenants/tnt_borealis/matters/mat_glacier/documents/acb45e23-05e8-42da-b987-fb9b12345678/original.txt",
        media_type="text/plain",
        jurisdiction="fictional",
        document_date="2099-01-01",
        confidentiality="fictional-internal",
        status=DocumentStatus.UPLOADED,
        file_size_bytes=100,
    )


class FakeIngestionClient:
    def __init__(self, status: str, failed_count: int | None = 0, repository=None) -> None:
        self.status = status
        self.failed_count = failed_count
        self.repository = repository
        self.start_calls: list[dict[str, Any]] = []
        self.get_calls: list[dict[str, Any]] = []
        self.status_when_polled: DocumentStatus | None = None

    def start_ingestion_job(self, **kwargs: Any) -> Mapping[str, Any]:
        self.start_calls.append(kwargs)
        if self.repository is not None:
            self.status_when_started = self.repository.get_for_scope(
                tenant_id=TENANT, matter_id=MATTER, document_id=DOCUMENT_ID
            ).status
        return {"ingestionJob": {"ingestionJobId": "job-fictional"}}

    def get_ingestion_job(self, **kwargs: Any) -> Mapping[str, Any]:
        self.get_calls.append(kwargs)
        if self.repository is not None:
            self.status_when_polled = self.repository.get_for_scope(
                tenant_id=TENANT, matter_id=MATTER, document_id=DOCUMENT_ID
            ).status
        job: dict[str, Any] = {"status": self.status}
        if self.failed_count is not None:
            job["statistics"] = {"numberOfDocumentsFailed": self.failed_count}
        return {"ingestionJob": job}


class FakeS3ObjectVerifier:
    def __init__(
        self,
        *,
        missing_key: str | None = None,
        metadata_overrides: Mapping[str, Mapping[str, str]] | None = None,
    ) -> None:
        self.missing_key = missing_key
        self.metadata_overrides = dict(metadata_overrides or {})
        self.headed_keys: list[str] = []

    def head_object(self, *, key: str) -> Mapping[str, Any]:
        self.headed_keys.append(key)
        if key == self.missing_key:
            raise KeyError(key)
        content_type = "application/json" if key.endswith(".metadata.json") else "text/plain"
        parts = key.split("/")
        tenant_id = parts[1]
        matter_id = parts[3]
        document_id = parts[5]
        metadata = (
            {"document-id": document_id}
            if key.endswith(".metadata.json")
            else {
                "tenant-id": tenant_id,
                "matter-id": matter_id,
                "document-id": document_id,
            }
        )
        metadata.update(self.metadata_overrides.get(key, {}))
        return {
            "ContentLength": 100,
            "ContentType": content_type,
            "Metadata": metadata,
        }


class KnowledgeBaseIngestionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = InMemoryDocumentMetadataRepository()
        self.repository.save(uploaded_document())

    def run_sync(
        self,
        client: FakeIngestionClient,
        object_verifier: FakeS3ObjectVerifier | None = None,
    ):
        return run_knowledge_base_sync(
            client=client,
            object_verifier=object_verifier or FakeS3ObjectVerifier(),
            metadata_repository=self.repository,
            knowledge_base_id="kb-fictional",
            data_source_id="ds-fictional",
            document_refs=[DocumentScopeRef(TENANT, MATTER, DOCUMENT_ID)],
            timeout_seconds=30,
            poll_interval_seconds=1,
            sleep_fn=lambda _seconds: None,
        )

    def test_document_is_indexed_only_after_successful_completed_job(self) -> None:
        client = FakeIngestionClient("COMPLETE", failed_count=0, repository=self.repository)
        result = self.run_sync(client)
        stored = self.repository.get_for_scope(
            tenant_id=TENANT, matter_id=MATTER, document_id=DOCUMENT_ID
        )
        self.assertEqual(client.status_when_started, DocumentStatus.UPLOADED)
        self.assertEqual(client.status_when_polled, DocumentStatus.PENDING_INGESTION)
        self.assertEqual(stored.status, DocumentStatus.INDEXED)
        self.assertEqual(result.documents_updated, 1)

    def test_partial_failure_never_marks_selected_documents_indexed(self) -> None:
        result = self.run_sync(FakeIngestionClient("COMPLETE", failed_count=1))
        stored = self.repository.get_for_scope(
            tenant_id=TENANT, matter_id=MATTER, document_id=DOCUMENT_ID
        )
        self.assertEqual(result.status, "COMPLETE")
        self.assertEqual(result.documents_updated, 0)
        self.assertEqual(stored.status, DocumentStatus.PENDING_INGESTION)

    def test_missing_failure_statistics_never_marks_selected_documents_indexed(self) -> None:
        result = self.run_sync(FakeIngestionClient("COMPLETE", failed_count=None))
        stored = self.repository.get_for_scope(
            tenant_id=TENANT, matter_id=MATTER, document_id=DOCUMENT_ID
        )
        self.assertEqual(result.documents_updated, 0)
        self.assertIsNone(result.failed_document_count)
        self.assertEqual(stored.status, DocumentStatus.PENDING_INGESTION)

    def test_failed_job_marks_attempt_failed(self) -> None:
        self.run_sync(FakeIngestionClient("FAILED", failed_count=1))
        stored = self.repository.get_for_scope(
            tenant_id=TENANT, matter_id=MATTER, document_id=DOCUMENT_ID
        )
        self.assertEqual(stored.status, DocumentStatus.FAILED)

    def test_failed_document_can_retry_after_original_and_sidecar_prechecks(self) -> None:
        self.repository.save(replace(uploaded_document(), status=DocumentStatus.FAILED))
        client = FakeIngestionClient("COMPLETE", failed_count=0, repository=self.repository)
        verifier = FakeS3ObjectVerifier()

        result = self.run_sync(client, verifier)

        stored = self.repository.get_for_scope(
            tenant_id=TENANT, matter_id=MATTER, document_id=DOCUMENT_ID
        )
        self.assertEqual(result.status, "COMPLETE")
        self.assertEqual(client.status_when_started, DocumentStatus.FAILED)
        self.assertEqual(client.status_when_polled, DocumentStatus.PENDING_INGESTION)
        self.assertEqual(stored.status, DocumentStatus.INDEXED)
        self.assertEqual(
            verifier.headed_keys,
            [uploaded_document().s3_key, f"{uploaded_document().s3_key}.metadata.json"],
        )

    def test_failed_document_with_mismatched_s3_scope_metadata_cannot_retry(self) -> None:
        document = replace(uploaded_document(), status=DocumentStatus.FAILED)
        self.repository.save(document)
        client = FakeIngestionClient("COMPLETE", failed_count=0)
        verifier = FakeS3ObjectVerifier(
            metadata_overrides={
                document.s3_key: {"matter-id": "mat_other"},
            }
        )

        with self.assertRaises(ValueError):
            self.run_sync(client, verifier)

        stored = self.repository.get_for_scope(
            tenant_id=TENANT, matter_id=MATTER, document_id=DOCUMENT_ID
        )
        self.assertEqual(client.start_calls, [])
        self.assertEqual(stored.status, DocumentStatus.FAILED)

    def test_sidecar_document_id_must_match_selected_document(self) -> None:
        document = uploaded_document()
        verifier = FakeS3ObjectVerifier(
            metadata_overrides={
                f"{document.s3_key}.metadata.json": {"document-id": "doc_other"},
            }
        )
        client = FakeIngestionClient("COMPLETE", failed_count=0)

        with self.assertRaises(ValueError):
            self.run_sync(client, verifier)

        self.assertEqual(client.start_calls, [])
        self.assertEqual(
            self.repository.get_for_scope(
                tenant_id=TENANT, matter_id=MATTER, document_id=DOCUMENT_ID
            ).status,
            DocumentStatus.UPLOADED,
        )

    def test_document_selection_is_scoped_and_explicit(self) -> None:
        client = FakeIngestionClient("COMPLETE", failed_count=0)
        with self.assertRaises(ValueError):
            run_knowledge_base_sync(
                client=client,
                object_verifier=FakeS3ObjectVerifier(),
                metadata_repository=self.repository,
                knowledge_base_id="kb-fictional",
                data_source_id="ds-fictional",
                document_refs=[DocumentScopeRef("tnt_borealis", "mat_glacier", DOCUMENT_ID)],
            )
        with self.assertRaises(ValueError):
            run_knowledge_base_sync(
                client=client,
                object_verifier=FakeS3ObjectVerifier(),
                metadata_repository=self.repository,
                knowledge_base_id="kb-fictional",
                data_source_id="ds-fictional",
                document_refs=[],
            )
        self.assertEqual(client.start_calls, [])

    def test_one_job_can_update_selected_documents_across_matters(self) -> None:
        second = second_uploaded_document()
        self.repository.save(second)
        client = FakeIngestionClient("COMPLETE", failed_count=0)
        result = run_knowledge_base_sync(
            client=client,
            object_verifier=FakeS3ObjectVerifier(),
            metadata_repository=self.repository,
            knowledge_base_id="kb-fictional",
            data_source_id="ds-fictional",
            document_refs=[
                DocumentScopeRef(TENANT, MATTER, DOCUMENT_ID),
                DocumentScopeRef("tnt_borealis", "mat_glacier", second.document_id),
            ],
            sleep_fn=lambda _seconds: None,
        )
        self.assertEqual(len(client.start_calls), 1)
        self.assertEqual(result.documents_updated, 2)
        self.assertEqual(
            self.repository.get_for_scope(
                tenant_id=TENANT, matter_id=MATTER, document_id=DOCUMENT_ID
            ).status,
            DocumentStatus.INDEXED,
        )
        self.assertEqual(
            self.repository.get_for_scope(
                tenant_id="tnt_borealis",
                matter_id="mat_glacier",
                document_id=second.document_id,
            ).status,
            DocumentStatus.INDEXED,
        )

    def test_timed_out_sync_leaves_documents_pending(self) -> None:
        client = FakeIngestionClient("IN_PROGRESS", failed_count=None)
        clock_values = iter((0.0, 0.0, 2.0))
        result = run_knowledge_base_sync(
            client=client,
            object_verifier=FakeS3ObjectVerifier(),
            metadata_repository=self.repository,
            knowledge_base_id="kb-fictional",
            data_source_id="ds-fictional",
            document_refs=[DocumentScopeRef(TENANT, MATTER, DOCUMENT_ID)],
            timeout_seconds=1,
            poll_interval_seconds=1,
            sleep_fn=lambda _seconds: None,
            monotonic_fn=lambda: next(clock_values),
        )
        stored = self.repository.get_for_scope(
            tenant_id=TENANT, matter_id=MATTER, document_id=DOCUMENT_ID
        )
        self.assertEqual(result.status, "TIMED_OUT")
        self.assertEqual(stored.status, DocumentStatus.PENDING_INGESTION)

    def test_missing_source_or_sidecar_prevents_ingestion_start(self) -> None:
        document = uploaded_document()
        client = FakeIngestionClient("COMPLETE", failed_count=0)
        missing_sidecar = FakeS3ObjectVerifier(
            missing_key=f"{document.s3_key}.metadata.json"
        )
        with self.assertRaises(ValueError):
            self.run_sync(client, missing_sidecar)
        stored = self.repository.get_for_scope(
            tenant_id=TENANT, matter_id=MATTER, document_id=DOCUMENT_ID
        )
        self.assertEqual(client.start_calls, [])
        self.assertEqual(stored.status, DocumentStatus.UPLOADED)
        self.assertEqual(
            missing_sidecar.headed_keys,
            [document.s3_key, f"{document.s3_key}.metadata.json"],
        )


if __name__ == "__main__":
    unittest.main()
