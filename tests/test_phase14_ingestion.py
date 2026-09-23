from __future__ import annotations

import sys
import unittest
from io import BytesIO
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "backend" / "src"))

from legaldesk.authorization import AuthorizationDenied
from legaldesk.documents import InMemoryDocumentMetadataRepository
from legaldesk.domain.models import Document, DocumentStatus
from legaldesk.ingestion import AsyncKnowledgeBaseIngestionService, IngestionConflictError
from legaldesk.http_app import ApplicationComposition, LoopbackLegalDeskApp
from legaldesk.state import InMemoryEphemeralStateStore, IngestionOperationRecord


TENANT = "tenant-a"
MATTER = "matter-a"
DOCUMENT_ID = "doc-a"


def _document(document_id: str = DOCUMENT_ID, status: DocumentStatus = DocumentStatus.UPLOADED) -> Document:
    return Document(
        document_id=document_id, matter_id=MATTER, tenant_id=TENANT, name="fictional.txt",
        s3_key=f"tenants/{TENANT}/matters/{MATTER}/documents/{document_id}/original.txt",
        media_type="text/plain", jurisdiction="fictional", document_date="2099-01-01",
        confidentiality="fictional", status=status, file_size_bytes=10,
    )


class _Objects:
    def head_object(self, *, key: str) -> Mapping[str, Any]:
        document_id = key.split("/")[-2]
        if key.endswith(".metadata.json"):
            return {"ContentLength": 100, "ContentType": "application/json", "Metadata": {"document-id": document_id}}
        return {"ContentLength": 10, "ContentType": "text/plain", "Metadata": {"tenant-id": TENANT, "matter-id": MATTER, "document-id": document_id}}


class _Client:
    def __init__(self, statuses: list[str]) -> None:
        self.statuses = list(statuses)
        self.start_calls: list[dict[str, Any]] = []
        self.get_calls: list[dict[str, Any]] = []

    def start_ingestion_job(self, **kwargs: Any) -> Mapping[str, Any]:
        self.start_calls.append(kwargs)
        return {"ingestionJob": {"ingestionJobId": "job-a"}}

    def get_ingestion_job(self, **kwargs: Any) -> Mapping[str, Any]:
        self.get_calls.append(kwargs)
        status = self.statuses.pop(0)
        job: dict[str, Any] = {"status": status}
        if status == "COMPLETE":
            job["statistics"] = {"numberOfDocumentsFailed": 0}
        return {"ingestionJob": job}


class _AmbiguousClient(_Client):
    def start_ingestion_job(self, **kwargs: Any) -> Mapping[str, Any]:
        self.start_calls.append(kwargs)
        raise TimeoutError("provider response was ambiguous")


class _FailingUpdateRepository(InMemoryDocumentMetadataRepository):
    def __init__(self) -> None:
        super().__init__()
        self.fail_updates = True

    def update_status(self, **kwargs: Any):
        if self.fail_updates:
            raise RuntimeError("metadata write ambiguous")
        return super().update_status(**kwargs)


class _FailingTerminalStore(InMemoryEphemeralStateStore):
    def __init__(self) -> None:
        super().__init__(clock=lambda: 100.0)
        self.fail_terminal = True

    def update_ingestion_operation(self, record):
        if self.fail_terminal and record.status == "INDEXED":
            raise RuntimeError("terminal state persistence ambiguous")
        return super().update_ingestion_operation(record)


class _HttpIngestionService:
    def __init__(self, operation: IngestionOperationRecord) -> None:
        self.operation = operation
        self.starts: list[dict[str, Any]] = []
        self.statuses: list[dict[str, Any]] = []

    def start(self, **kwargs: Any) -> IngestionOperationRecord:
        self.starts.append(kwargs)
        return self.operation

    def status(self, **kwargs: Any) -> IngestionOperationRecord:
        self.statuses.append(kwargs)
        return self.operation


class Phase14IngestionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.metadata = InMemoryDocumentMetadataRepository()
        self.metadata.save(_document())
        self.client = _Client(["IN_PROGRESS", "COMPLETE"])
        self.store = InMemoryEphemeralStateStore(clock=lambda: 100.0)
        self.service = AsyncKnowledgeBaseIngestionService(
            client=self.client, object_verifier=_Objects(), metadata_repository=self.metadata,
            state_store=self.store, knowledge_base_id="kb-a", data_source_id="ds-a", clock=lambda: 100.0,
        )

    def test_start_returns_accepted_binding_and_retry_is_idempotent(self) -> None:
        operation = self.service.start(
            subject="alice", tenant_id=TENANT, matter_id=MATTER, document_ids=(DOCUMENT_ID,),
            correlation_id="corr-a", idempotency_key="retry-a",
        )
        self.assertEqual(operation.status, "PENDING")
        self.assertEqual(self.metadata.get_for_scope(tenant_id=TENANT, matter_id=MATTER, document_id=DOCUMENT_ID).status, DocumentStatus.PENDING_INGESTION)
        repeated = self.service.start(
            subject="alice", tenant_id=TENANT, matter_id=MATTER, document_ids=(DOCUMENT_ID,),
            correlation_id="corr-new", idempotency_key="retry-a",
        )
        self.assertEqual(repeated.operation_id, operation.operation_id)
        self.assertEqual(len(self.client.start_calls), 1)

    def test_same_idempotency_key_accepts_document_ids_in_different_order(self) -> None:
        metadata = InMemoryDocumentMetadataRepository()
        metadata.save(_document("doc-a"))
        metadata.save(_document("doc-b"))
        client = _Client([])
        service = AsyncKnowledgeBaseIngestionService(
            client=client, object_verifier=_Objects(), metadata_repository=metadata,
            state_store=InMemoryEphemeralStateStore(clock=lambda: 100.0),
            knowledge_base_id="kb-a", data_source_id="ds-a", clock=lambda: 100.0,
        )
        first = service.start(
            subject="alice", tenant_id=TENANT, matter_id=MATTER, document_ids=("doc-a", "doc-b"),
            correlation_id="corr-a", idempotency_key="retry-a",
        )
        repeated = service.start(
            subject="alice", tenant_id=TENANT, matter_id=MATTER, document_ids=("doc-b", "doc-a"),
            correlation_id="corr-b", idempotency_key="retry-a",
        )
        self.assertEqual(repeated.operation_id, first.operation_id)
        self.assertEqual(len(client.start_calls), 1)

    def test_status_is_pending_then_indexes_and_repeated_terminal_read_is_idempotent(self) -> None:
        operation = self.service.start(
            subject="alice", tenant_id=TENANT, matter_id=MATTER, document_ids=(DOCUMENT_ID,),
            correlation_id="corr-a", idempotency_key="retry-a",
        )
        pending = self.service.status(operation_id=operation.operation_id, subject="alice", tenant_id=TENANT, matter_id=MATTER)
        self.assertEqual(pending.status, "PENDING")
        indexed = self.service.status(operation_id=operation.operation_id, subject="alice", tenant_id=TENANT, matter_id=MATTER)
        self.assertEqual(indexed.status, "INDEXED")
        repeated = self.service.status(operation_id=operation.operation_id, subject="alice", tenant_id=TENANT, matter_id=MATTER)
        self.assertEqual(repeated.status, "INDEXED")
        self.assertEqual(len(self.client.get_calls), 2)
        self.assertEqual(self.metadata.get_for_scope(tenant_id=TENANT, matter_id=MATTER, document_id=DOCUMENT_ID).status, DocumentStatus.INDEXED)

    def test_failed_job_transitions_documents_to_failed(self) -> None:
        self.client.statuses = ["FAILED"]
        operation = self.service.start(
            subject="alice", tenant_id=TENANT, matter_id=MATTER, document_ids=(DOCUMENT_ID,),
            correlation_id="corr-a", idempotency_key="retry-a",
        )
        result = self.service.status(operation_id=operation.operation_id, subject="alice", tenant_id=TENANT, matter_id=MATTER)
        self.assertEqual(result.status, "FAILED")
        self.assertEqual(self.metadata.get_for_scope(tenant_id=TENANT, matter_id=MATTER, document_id=DOCUMENT_ID).status, DocumentStatus.FAILED)

    def test_foreign_scope_denied_before_provider_status_call(self) -> None:
        operation = self.service.start(
            subject="alice", tenant_id=TENANT, matter_id=MATTER, document_ids=(DOCUMENT_ID,),
            correlation_id="corr-a", idempotency_key="retry-a",
        )
        with self.assertRaises(AuthorizationDenied):
            self.service.status(operation_id=operation.operation_id, subject="bob", tenant_id=TENANT, matter_id=MATTER)
        self.assertEqual(self.client.get_calls, [])

    def test_expired_operation_fails_closed(self) -> None:
        operation = self.service.start(
            subject="alice", tenant_id=TENANT, matter_id=MATTER, document_ids=(DOCUMENT_ID,),
            correlation_id="corr-a", idempotency_key="retry-a",
        )
        self.store.ingestion_operations[operation.operation_id] = replace(operation, expires_at=99.0)
        with self.assertRaises(ValueError):
            self.service.status(operation_id=operation.operation_id, subject="alice", tenant_id=TENANT, matter_id=MATTER)
        self.assertEqual(self.client.get_calls, [])

    def test_ambiguous_provider_start_is_retained_and_blocks_new_keys(self) -> None:
        client = _AmbiguousClient([])
        service = AsyncKnowledgeBaseIngestionService(
            client=client, object_verifier=_Objects(), metadata_repository=self.metadata,
            state_store=self.store, knowledge_base_id="kb-a", data_source_id="ds-a", clock=lambda: 100.0,
        )
        with self.assertRaises(TimeoutError):
            service.start(subject="alice", tenant_id=TENANT, matter_id=MATTER, document_ids=(DOCUMENT_ID,), correlation_id="corr-a", idempotency_key="retry-a")
        retained = service.start(subject="alice", tenant_id=TENANT, matter_id=MATTER, document_ids=(DOCUMENT_ID,), correlation_id="corr-b", idempotency_key="retry-a")
        self.assertEqual(retained.status, "STARTING")
        with self.assertRaises(IngestionConflictError):
            service.start(subject="alice", tenant_id=TENANT, matter_id=MATTER, document_ids=(DOCUMENT_ID,), correlation_id="corr-c", idempotency_key="retry-b")
        self.assertEqual(len(client.start_calls), 1)

    def test_post_start_metadata_failure_recovers_exact_documents_before_terminal_transition(self) -> None:
        metadata = _FailingUpdateRepository()
        metadata.save(_document())
        client = _Client(["COMPLETE"])
        service = AsyncKnowledgeBaseIngestionService(
            client=client, object_verifier=_Objects(), metadata_repository=metadata,
            state_store=self.store, knowledge_base_id="kb-a", data_source_id="ds-a", clock=lambda: 100.0,
        )
        with self.assertRaises(RuntimeError):
            service.start(subject="alice", tenant_id=TENANT, matter_id=MATTER, document_ids=(DOCUMENT_ID,), correlation_id="corr-a", idempotency_key="retry-a")
        operation = next(iter(self.store.ingestion_operations.values()))
        self.assertEqual(operation.status, "RECOVERY_PENDING")
        self.assertEqual(operation.ingestion_job_id, "job-a")
        metadata.fail_updates = False
        recovered = service.status(operation_id=operation.operation_id, subject="alice", tenant_id=TENANT, matter_id=MATTER)
        self.assertEqual(recovered.status, "INDEXED")
        self.assertEqual(metadata.get_for_scope(tenant_id=TENANT, matter_id=MATTER, document_id=DOCUMENT_ID).status, DocumentStatus.INDEXED)
        self.assertEqual(len(client.start_calls), 1)
        self.assertEqual(len(client.get_calls), 1)

    def test_terminal_persistence_failure_recovers_already_transitioned_documents(self) -> None:
        store = _FailingTerminalStore()
        client = _Client(["COMPLETE"])
        service = AsyncKnowledgeBaseIngestionService(
            client=client, object_verifier=_Objects(), metadata_repository=self.metadata,
            state_store=store, knowledge_base_id="kb-a", data_source_id="ds-a", clock=lambda: 100.0,
        )
        operation = service.start(
            subject="alice", tenant_id=TENANT, matter_id=MATTER, document_ids=(DOCUMENT_ID,),
            correlation_id="corr-a", idempotency_key="retry-a",
        )
        with self.assertRaises(RuntimeError):
            service.status(operation_id=operation.operation_id, subject="alice", tenant_id=TENANT, matter_id=MATTER)
        store.fail_terminal = False
        recovered = service.status(operation_id=operation.operation_id, subject="alice", tenant_id=TENANT, matter_id=MATTER)
        self.assertEqual(recovered.status, "INDEXED")
        self.assertEqual(len(client.get_calls), 1)

    def test_http_start_and_status_are_separate_routes(self) -> None:
        operation = IngestionOperationRecord(
            operation_id="ing_opaque", idempotency_key="retry-a", subject="alice", tenant_id=TENANT,
            matter_id=MATTER, document_ids=(DOCUMENT_ID,), correlation_id="corr-a", ingestion_job_id="job-a",
            status="PENDING", provider_status="STARTING", created_at=100.0, updated_at=100.0, expires_at=200.0,
        )
        service = _HttpIngestionService(operation)
        composition = ApplicationComposition(
            identity_verifier=object(), token_exchange=None, authorization_store=object(), conversation_store=object(),
            document_pipeline=object(), object_storage=object(), metadata_repository=object(), mcp_server=object(),
            matter_catalog=(MATTER,), ingestion_service=service,
        )
        app = LoopbackLegalDeskApp(composition)
        app._session = lambda _environ: ("selector", type("Session", (), {"csrf_token": "csrf", "identity": type("Identity", (), {"subject": "alice"})()})())
        app._context = lambda _identity, _matter_id: type("Context", (), {"tenant_id": TENANT, "matter_id": MATTER, "correlation_id": "corr-a"})()
        payload = b'{"documentIds":["doc-a"],"idempotencyKey":"retry-a"}'
        environ = {
            "HTTP_HOST": "localhost", "HTTP_X_CSRF_TOKEN": "csrf", "REQUEST_METHOD": "POST",
            "PATH_INFO": f"/api/matters/{MATTER}/ingestions", "QUERY_STRING": "",
            "CONTENT_LENGTH": str(len(payload)), "wsgi.input": BytesIO(payload),
        }
        status, body, _ = app._dispatch(environ)
        self.assertEqual(status.value, 202)
        self.assertEqual(body["operationId"], "ing_opaque")
        self.assertEqual(service.starts[0]["matter_id"], MATTER)
        status, body, _ = app._dispatch({**environ, "REQUEST_METHOD": "GET", "PATH_INFO": f"/api/matters/{MATTER}/ingestions/ing_opaque", "CONTENT_LENGTH": "0", "wsgi.input": BytesIO(b"")})
        self.assertEqual(status.value, 200)
        self.assertEqual(body["operationStatus"], "documents_processing")
        self.assertEqual(service.statuses[0]["operation_id"], "ing_opaque")


if __name__ == "__main__":
    unittest.main()
