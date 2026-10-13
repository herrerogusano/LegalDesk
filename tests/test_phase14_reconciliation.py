from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "backend" / "src"))

from legaldesk.documents import InMemoryDocumentMetadataRepository, InMemoryObjectStorage
from legaldesk.domain.models import Document, DocumentStatus
from legaldesk.gateway_interceptor import (
    Boto3DynamoGatewayGrantRepository,
    GatewayAuthorizationGrant,
    HarnessInvocationGrant,
    InMemoryGatewayGrantRepository,
)
from legaldesk.ingestion import IngestionOperationRecord
from legaldesk.reconciliation import (
    GatewayReconciliationCandidate,
    IngestionReconciliationCandidate,
    IngestionReconciliationScope,
    ReconciliationScope,
    ReconciliationService,
)
from legaldesk.state import InMemoryEphemeralStateStore


NOW = 1_000.0


def _document(
    *,
    tenant_id: str = "tenant-a",
    matter_id: str = "matter-a",
    document_id: str = "doc-a",
    status: DocumentStatus = DocumentStatus.PENDING_UPLOAD,
    age_seconds: float = 500.0,
) -> Document:
    return Document(
        document_id=document_id,
        matter_id=matter_id,
        tenant_id=tenant_id,
        name="fictional.txt",
        s3_key=f"quarantine/tenants/{tenant_id}/matters/{matter_id}/documents/{document_id}/original.txt",
        media_type="text/plain",
        jurisdiction="fictional",
        document_date="2099-01-01",
        confidentiality="public-fictional",
        status=status,
        file_size_bytes=10,
        uploaded_at=datetime.fromtimestamp(NOW - age_seconds, tz=timezone.utc),
        quarantine_s3_key=f"quarantine/tenants/{tenant_id}/matters/{matter_id}/documents/{document_id}/original.txt",
    )


class _RecordingMetadata(InMemoryDocumentMetadataRepository):
    def __init__(self) -> None:
        super().__init__()
        self.limits: list[int | None] = []

    def list_for_scope(self, *, tenant_id: str, matter_id: str, limit: int | None = None):
        self.limits.append(limit)
        return super().list_for_scope(tenant_id=tenant_id, matter_id=matter_id, limit=limit)


class _LeakyMetadata(_RecordingMetadata):
    """Fixture that proves reconciliation rechecks repository scope."""

    def list_for_scope(self, *, tenant_id: str, matter_id: str, limit: int | None = None):
        self.limits.append(limit)
        return tuple(self.documents.values())[:limit]


class _FakeIngestionService:
    def __init__(self, store: InMemoryEphemeralStateStore) -> None:
        self.store = store
        self.calls: list[dict[str, str]] = []

    def status(self, **kwargs: str) -> IngestionOperationRecord:
        self.calls.append(kwargs)
        operation = self.store.get_ingestion_operation(kwargs["operation_id"])
        assert operation is not None
        repaired = replace(operation, status="INDEXED", provider_status="COMPLETE", updated_at=NOW)
        self.store.update_ingestion_operation(repaired)
        return repaired


class _FlakyGatewayTable:
    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict[str, object]] = {}
        self.fail_index_calls = 2

    def put_item(self, *, Item, ConditionExpression=None, **_kwargs):
        key = (Item["pk"], Item["sk"])
        if ConditionExpression and key in self.items:
            raise RuntimeError("conditional collision")
        if Item.get("entityType") == "GatewayExpiryIndex" and self.fail_index_calls:
            self.fail_index_calls -= 1
            raise RuntimeError("index unavailable")
        self.items[key] = dict(Item)

    def get_item(self, *, Key, **_kwargs):
        return {"Item": self.items.get((Key["pk"], Key["sk"]))}


class Phase14ReconciliationTests(unittest.TestCase):
    def _service(
        self,
        metadata: InMemoryDocumentMetadataRepository,
        storage: InMemoryObjectStorage,
        store: InMemoryEphemeralStateStore,
        *,
        ingestion_service: object | None = None,
        gateway_repository: InMemoryGatewayGrantRepository | None = None,
    ) -> ReconciliationService:
        return ReconciliationService(
            metadata_repository=metadata,
            object_storage=storage,
            state_store=store,
            ingestion_service=ingestion_service,
            gateway_repository=gateway_repository,
            clock=lambda: NOW,
        )

    def test_pending_upload_cleanup_is_bounded_idempotent_and_scope_checked(self) -> None:
        metadata = _RecordingMetadata()
        storage = InMemoryObjectStorage()
        stale = _document()
        fresh = _document(document_id="doc-fresh", age_seconds=5.0)
        foreign = _document(tenant_id="tenant-b", matter_id="matter-b", document_id="doc-foreign")
        for document in (stale, fresh, foreign):
            metadata.save(document)
            storage.put_object(key=document.quarantine_s3_key, body=b"fictional", media_type=document.media_type, metadata={})
            storage.put_object(key=f"{document.quarantine_s3_key}.metadata.json", body=b"{}", media_type="application/json", metadata={})

        service = self._service(metadata, storage, InMemoryEphemeralStateStore(clock=lambda: NOW))
        report = service.reconcile_pending_uploads(
            scopes=(ReconciliationScope("tenant-a", "matter-a"),),
            stale_after_seconds=60,
            limit_per_scope=2,
        )
        self.assertEqual(report.changed, 1)
        self.assertEqual(metadata.limits, [2])
        self.assertEqual(metadata.get_for_scope(tenant_id="tenant-a", matter_id="matter-a", document_id="doc-a").status, DocumentStatus.FAILED)
        self.assertIsNotNone(metadata.get_for_scope(tenant_id="tenant-b", matter_id="matter-b", document_id="doc-foreign"))
        self.assertNotIn(stale.quarantine_s3_key, storage.objects)
        repeated = service.reconcile_pending_uploads(
            scopes=(ReconciliationScope("tenant-a", "matter-a"),), stale_after_seconds=60, limit_per_scope=2
        )
        self.assertEqual(repeated.changed, 0)

    def test_pending_upload_rejects_repository_scope_leak(self) -> None:
        metadata = _LeakyMetadata()
        metadata.save(_document(tenant_id="tenant-b", matter_id="matter-b", document_id="doc-foreign"))
        storage = InMemoryObjectStorage()
        service = self._service(metadata, storage, InMemoryEphemeralStateStore(clock=lambda: NOW))
        report = service.reconcile_pending_uploads(
            scopes=(ReconciliationScope("tenant-a", "matter-a"),), stale_after_seconds=60, limit_per_scope=1
        )
        self.assertEqual(report.changed, 0)
        self.assertEqual(report.ambiguous, 0)
        self.assertEqual(report.skipped, 1)
        self.assertEqual(metadata.limits, [1])

    def test_ingestion_reconciliation_retries_stale_operation_but_quarantines_ambiguous_start(self) -> None:
        store = InMemoryEphemeralStateStore(clock=lambda: NOW)
        stale = IngestionOperationRecord(
            operation_id="ing-stale", idempotency_key="retry-a", subject="alice", tenant_id="tenant-a",
            matter_id="matter-a", document_ids=("doc-a",), correlation_id="corr-a", ingestion_job_id="job-a",
            status="PENDING", provider_status="IN_PROGRESS", created_at=0.0, updated_at=0.0, expires_at=2_000.0,
        )
        ambiguous = IngestionOperationRecord(
            operation_id="ing-ambiguous", idempotency_key="retry-b", subject="alice", tenant_id="tenant-a",
            matter_id="matter-a", document_ids=("doc-b",), correlation_id="corr-b", ingestion_job_id="",
            status="STARTING", provider_status="STARTING", created_at=0.0, updated_at=0.0, expires_at=2_000.0,
        )
        store.put_ingestion_operation(stale)
        store.put_ingestion_operation(ambiguous)
        ingestion = _FakeIngestionService(store)
        service = self._service(
            InMemoryDocumentMetadataRepository(), InMemoryObjectStorage(), store, ingestion_service=ingestion
        )
        report = service.reconcile_ingestion(
            candidates=(
                IngestionReconciliationCandidate("ing-stale", "alice", "tenant-a", "matter-a"),
                IngestionReconciliationCandidate("ing-ambiguous", "alice", "tenant-a", "matter-a"),
            ),
            stale_after_seconds=60,
        )
        self.assertEqual(report.changed, 1)
        self.assertEqual(report.ambiguous, 1)
        self.assertEqual([call["operation_id"] for call in ingestion.calls], ["ing-stale"])
        self.assertEqual(store.get_ingestion_operation("ing-ambiguous").status, "STARTING")

    def test_ingestion_reconciliation_denies_foreign_scope_before_status_call_and_bounds_batch(self) -> None:
        store = InMemoryEphemeralStateStore(clock=lambda: NOW)
        operation = IngestionOperationRecord(
            operation_id="ing-scope", idempotency_key="retry-a", subject="alice", tenant_id="tenant-a",
            matter_id="matter-a", document_ids=("doc-a",), correlation_id="corr-a", ingestion_job_id="job-a",
            status="PENDING", provider_status="IN_PROGRESS", created_at=0.0, updated_at=0.0, expires_at=2_000.0,
        )
        store.put_ingestion_operation(operation)
        ingestion = _FakeIngestionService(store)
        service = self._service(
            InMemoryDocumentMetadataRepository(), InMemoryObjectStorage(), store, ingestion_service=ingestion
        )
        report = service.reconcile_ingestion(
            candidates=(IngestionReconciliationCandidate("ing-scope", "bob", "tenant-b", "matter-b"),),
            stale_after_seconds=60,
        )
        self.assertEqual(report.skipped, 1)
        self.assertEqual(ingestion.calls, [])
        with self.assertRaises(ValueError):
            service.reconcile_ingestion(
                candidates=tuple(
                    IngestionReconciliationCandidate(f"ing-{index}", "alice", "tenant-a", "matter-a")
                    for index in range(101)
                ),
                stale_after_seconds=60,
            )

    def test_ingestion_scope_reconciliation_uses_explicit_bounded_partition(self) -> None:
        store = InMemoryEphemeralStateStore(clock=lambda: NOW)
        operation = IngestionOperationRecord(
            operation_id="ing-partition", idempotency_key="retry-a", subject="alice", tenant_id="tenant-a",
            matter_id="matter-a", document_ids=("doc-a",), correlation_id="corr-a", ingestion_job_id="job-a",
            status="PENDING", provider_status="IN_PROGRESS", created_at=0.0, updated_at=0.0, expires_at=2_000.0,
        )
        store.put_ingestion_operation(operation)
        ingestion = _FakeIngestionService(store)
        service = self._service(
            InMemoryDocumentMetadataRepository(), InMemoryObjectStorage(), store, ingestion_service=ingestion
        )
        report = service.reconcile_ingestion_scopes(
            scopes=(IngestionReconciliationScope("alice", "tenant-a", "matter-a"),),
            stale_after_seconds=60,
            limit_per_scope=1,
        )
        self.assertEqual(report.changed, 1)
        self.assertEqual([call["operation_id"] for call in ingestion.calls], ["ing-partition"])

    def test_gateway_reconciliation_deletes_only_valid_expired_point_records(self) -> None:
        repository = InMemoryGatewayGrantRepository()
        repository.put(GatewayAuthorizationGrant("grant-old", "alice", "matter-a", "corr-a", "tool-a", 900))
        repository.put(GatewayAuthorizationGrant("grant-new", "alice", "matter-a", "corr-a", "tool-a", 1_100))
        repository.put_invocation(HarnessInvocationGrant("inv-old", "alice", "matter-a", "corr-a", "actor", "session", ("tool-a",), 900))
        repository.grants["grant-new"] = {**repository.grants["grant-new"], "expiresAt": "malformed"}
        service = self._service(
            InMemoryDocumentMetadataRepository(), InMemoryObjectStorage(), InMemoryEphemeralStateStore(clock=lambda: NOW),
            gateway_repository=repository,
        )
        report = service.reconcile_gateway(
            candidates=(
                GatewayReconciliationCandidate("grant-old", "grant", "alice", "matter-a"),
                GatewayReconciliationCandidate("grant-new", "grant", "alice", "matter-a"),
                GatewayReconciliationCandidate("inv-old", "invocation", "alice", "matter-a"),
            )
        )
        self.assertEqual(report.changed, 2)
        self.assertEqual(report.ambiguous, 1)
        self.assertIsNone(repository.get("grant-old"))
        self.assertIsNotNone(repository.get("grant-new"))
        self.assertIsNone(repository.get_invocation("inv-old"))

    def test_gateway_expiry_index_is_bounded_and_revalidated_before_delete(self) -> None:
        repository = InMemoryGatewayGrantRepository()
        repository.put(GatewayAuthorizationGrant("grant-indexed", "alice", "matter-a", "corr-a", "tool-a", 900))
        repository.put_invocation(HarnessInvocationGrant("inv-indexed", "alice", "matter-a", "corr-a", "actor", "session", ("tool-a",), 900))
        service = self._service(
            InMemoryDocumentMetadataRepository(), InMemoryObjectStorage(), InMemoryEphemeralStateStore(clock=lambda: NOW),
            gateway_repository=repository,
        )
        report = service.reconcile_gateway_indexed(now=NOW, limit=2, allowed_matter_ids=("matter-a",))
        self.assertEqual(report.examined, 2)
        self.assertEqual(report.changed, 2)
        self.assertEqual(len(repository.expiry_index), 0)
        self.assertIsNone(repository.get("grant-indexed"))
        self.assertIsNone(repository.get_invocation("inv-indexed"))

    def test_gateway_expiry_index_rejects_foreign_matter_before_point_read(self) -> None:
        repository = InMemoryGatewayGrantRepository()
        repository.put(GatewayAuthorizationGrant("grant-allowed", "alice", "matter-a", "corr-a", "tool-a", 900))
        repository.put(GatewayAuthorizationGrant("grant-foreign", "alice", "matter-b", "corr-b", "tool-a", 900))
        service = self._service(
            InMemoryDocumentMetadataRepository(), InMemoryObjectStorage(), InMemoryEphemeralStateStore(clock=lambda: NOW),
            gateway_repository=repository,
        )
        report = service.reconcile_gateway_indexed(now=NOW, limit=2, allowed_matter_ids=("matter-a",))
        self.assertEqual(report.examined, 2)
        self.assertEqual(report.changed, 1)
        self.assertEqual(report.skipped, 1)
        self.assertTrue(any(item.outcome == "scope_mismatch" for item in report.items))
        self.assertIsNone(repository.get("grant-allowed"))
        self.assertIsNotNone(repository.get("grant-foreign"))

    def test_boto_invocation_retry_repairs_index_after_primary_write(self) -> None:
        table = _FlakyGatewayTable()
        repository = Boto3DynamoGatewayGrantRepository("fictional-table", table=table)
        grant = HarnessInvocationGrant(
            "inv-retry", "alice", "matter-a", "corr-a", "actor", "session", ("tool-a",), 900
        )
        with self.assertRaises(RuntimeError):
            repository.put_invocation(grant)
        self.assertIn((grant.item["pk"], grant.item["sk"]), table.items)
        repository.put_invocation(grant)
        self.assertEqual(sum(item.get("entityType") == "GatewayExpiryIndex" for item in table.items.values()), 1)


BUCKET = "bucket-a"


def _pending(document_id: str, *, age_seconds: float = 5_000.0, tenant_id: str = "tenant-a", matter_id: str = "matter-a") -> Document:
    document = _document(
        tenant_id=tenant_id, matter_id=matter_id, document_id=document_id,
        status=DocumentStatus.PENDING_INGESTION, age_seconds=age_seconds,
    )
    return replace(document, s3_key=f"tenants/{tenant_id}/matters/{matter_id}/documents/{document_id}/original.txt")


class _KbClient:
    def __init__(self, states: dict[str, str]) -> None:
        self.states = states
        self.calls: list[list[str]] = []

    def get_knowledge_base_documents(self, **kwargs):
        uris = [item["s3"]["uri"] for item in kwargs["documentIdentifiers"]]
        self.calls.append(uris)
        return {"documentDetails": [
            {"identifier": {"dataSourceType": "S3", "s3": {"uri": uri}}, "status": self.states[uri]}
            for uri in uris if uri in self.states
        ]}


class _PagedMetadata(InMemoryDocumentMetadataRepository):
    def __init__(self) -> None:
        super().__init__()
        self.page_calls: list[tuple[int, str | None]] = []

    def list_for_scope(self, **_kwargs):
        raise AssertionError("pending-document reconciliation must page the matter partition")

    def list_for_scope_page(self, *, tenant_id, matter_id, limit, cursor=None):
        self.page_calls.append((limit, cursor))
        values = sorted(
            (d for (t, m, _), d in self.documents.items() if (t, m) == (tenant_id, matter_id)),
            key=lambda item: item.document_id,
        )
        if cursor is not None:
            values = [item for item in values if item.document_id > cursor]
        page = tuple(values[:limit])
        return page, (page[-1].document_id if page and len(values) > len(page) else None)


class Phase14PendingDocumentReconciliationTests(unittest.TestCase):
    def _build(self, states: dict[str, str], documents: tuple[Document, ...]):
        from legaldesk.ingestion import AsyncKnowledgeBaseIngestionService

        metadata = _PagedMetadata()
        for document in documents:
            metadata.save(document)
        client = _KbClient(states)
        store = InMemoryEphemeralStateStore(clock=lambda: NOW)
        ingestion = AsyncKnowledgeBaseIngestionService(
            client=client, object_verifier=object(), metadata_repository=metadata, state_store=store,
            knowledge_base_id="kb-a", data_source_id="ds-a", clock=lambda: NOW, source_bucket=BUCKET,
        )
        service = ReconciliationService(
            metadata_repository=metadata, object_storage=InMemoryObjectStorage(), state_store=store,
            ingestion_service=ingestion, clock=lambda: NOW,
        )
        return service, metadata, client

    @staticmethod
    def _uri(document: Document) -> str:
        return f"s3://{BUCKET}/{document.s3_key}"

    def _status(self, metadata, document_id: str) -> DocumentStatus:
        return metadata.get_for_scope(tenant_id="tenant-a", matter_id="matter-a", document_id=document_id).status

    def test_stale_pending_document_of_another_subject_is_resolved_without_operation_rows(self) -> None:
        indexed, failed, unknown = _pending("doc-i"), _pending("doc-f"), _pending("doc-u")
        service, metadata, _client = self._build(
            {self._uri(indexed): "INDEXED", self._uri(failed): "FAILED", self._uri(unknown): "INDEXING"},
            (indexed, failed, unknown),
        )
        report = service.reconcile_pending_ingestion_documents(
            scopes=(ReconciliationScope("tenant-a", "matter-a"),), stale_after_seconds=60, limit_per_scope=10,
        )
        self.assertEqual(report.changed, 2)
        self.assertEqual(self._status(metadata, "doc-i"), DocumentStatus.INDEXED)
        self.assertEqual(self._status(metadata, "doc-f"), DocumentStatus.FAILED)
        self.assertEqual(self._status(metadata, "doc-u"), DocumentStatus.PENDING_INGESTION)

    def test_unknown_missing_and_not_found_statuses_stay_pending(self) -> None:
        a, b, c = _pending("doc-a"), _pending("doc-b"), _pending("doc-c")
        service, metadata, _client = self._build(
            {self._uri(a): "NOT_FOUND", self._uri(b): "PENDING"}, (a, b, c),
        )
        report = service.reconcile_pending_ingestion_documents(
            scopes=(ReconciliationScope("tenant-a", "matter-a"),), stale_after_seconds=60, limit_per_scope=10,
        )
        self.assertEqual(report.changed, 0)
        for document_id in ("doc-a", "doc-b", "doc-c"):
            self.assertEqual(self._status(metadata, document_id), DocumentStatus.PENDING_INGESTION)

    def test_fresh_and_non_pending_documents_are_not_resolved(self) -> None:
        fresh = _pending("doc-fresh", age_seconds=10.0)
        done = replace(_pending("doc-done"), status=DocumentStatus.INDEXED)
        service, metadata, client = self._build({self._uri(fresh): "INDEXED"}, (fresh, done))
        report = service.reconcile_pending_ingestion_documents(
            scopes=(ReconciliationScope("tenant-a", "matter-a"),), stale_after_seconds=60, limit_per_scope=10,
        )
        self.assertEqual(report.changed, 0)
        self.assertEqual(client.calls, [])
        self.assertEqual(self._status(metadata, "doc-fresh"), DocumentStatus.PENDING_INGESTION)

    def test_paging_and_candidates_are_bounded_per_scope(self) -> None:
        documents = tuple(_pending(f"doc-{index:03d}") for index in range(12))
        states = {f"s3://{BUCKET}/{d.s3_key}": "INDEXED" for d in documents}
        service, metadata, client = self._build(states, documents)
        report = service.reconcile_pending_ingestion_documents(
            scopes=(ReconciliationScope("tenant-a", "matter-a"),), stale_after_seconds=60, limit_per_scope=5,
        )
        self.assertEqual(report.changed, 5)
        self.assertEqual(sum(len(call) for call in client.calls), 5)
        self.assertTrue(all(len(call) <= 10 for call in client.calls))
        self.assertTrue(all(limit == 5 for limit, _ in metadata.page_calls))
        self.assertLessEqual(len(metadata.page_calls), 3)
        with self.assertRaises(ValueError):
            service.reconcile_pending_ingestion_documents(
                scopes=(ReconciliationScope("tenant-a", "matter-a"),), stale_after_seconds=60, max_pages=4,
            )

    def test_only_configured_scopes_are_queried_and_foreign_rows_are_ignored(self) -> None:
        own, foreign = _pending("doc-own"), _pending("doc-foreign", matter_id="matter-b")
        service, metadata, client = self._build(
            {self._uri(own): "INDEXED", self._uri(foreign): "INDEXED"}, (own, foreign),
        )
        service.reconcile_pending_ingestion_documents(
            scopes=(ReconciliationScope("tenant-a", "matter-a"),), stale_after_seconds=60, limit_per_scope=10,
        )
        self.assertEqual(self._status(metadata, "doc-own"), DocumentStatus.INDEXED)
        self.assertEqual(
            metadata.get_for_scope(tenant_id="tenant-a", matter_id="matter-b", document_id="doc-foreign").status,
            DocumentStatus.PENDING_INGESTION,
        )
        self.assertEqual(client.calls, [[self._uri(own)]])

    def test_provider_error_and_exhausted_budget_fail_closed(self) -> None:
        document = _pending("doc-a")
        service, metadata, client = self._build({}, (document,))

        def boom(**_kwargs):
            raise RuntimeError("provider unavailable")

        client.get_knowledge_base_documents = boom
        report = service.reconcile_pending_ingestion_documents(
            scopes=(ReconciliationScope("tenant-a", "matter-a"),), stale_after_seconds=60, limit_per_scope=10,
        )
        self.assertEqual(report.changed, 0)
        self.assertEqual(self._status(metadata, "doc-a"), DocumentStatus.PENDING_INGESTION)
        calls = []
        client.get_knowledge_base_documents = lambda **kw: calls.append(kw) or {"documentDetails": []}
        service.reconcile_pending_ingestion_documents(
            scopes=(ReconciliationScope("tenant-a", "matter-a"),), stale_after_seconds=60, has_budget=lambda: False,
        )
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
