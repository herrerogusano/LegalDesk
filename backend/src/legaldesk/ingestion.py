"""Deliberate Knowledge Base sync with conservative document lifecycle updates.

This is an operator workflow, not an HTTP endpoint. Callers select documents
from one stored tenant/matter scope before starting a data-source-wide sync.
Documents become INDEXED only after Bedrock reports COMPLETE with no failed
documents. Partial or timed-out jobs remain PENDING_INGESTION for reconciliation.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from time import monotonic, sleep, time
from typing import Any, Callable, Mapping, Protocol, Sequence
from uuid import uuid4

from .documents import DocumentMetadataRepository
from .domain.models import Document, DocumentStatus, MalwareScanStatus
from .authorization import AuthorizationDenied
from .state import EphemeralStateStore, IngestionOperationRecord, ingestion_document_set_key


class KnowledgeBaseIngestionClient(Protocol):
    def start_ingestion_job(
        self,
        *,
        knowledgeBaseId: str,
        dataSourceId: str,
        clientToken: str,
    ) -> Mapping[str, Any]: ...

    def get_ingestion_job(
        self,
        *,
        knowledgeBaseId: str,
        dataSourceId: str,
        ingestionJobId: str,
    ) -> Mapping[str, Any]: ...


class S3ObjectVerifier(Protocol):
    def head_object(self, *, key: str) -> Mapping[str, Any]: ...


@dataclass(frozen=True, slots=True)
class DocumentScopeRef:
    tenant_id: str
    matter_id: str
    document_id: str


@dataclass(frozen=True, slots=True)
class KnowledgeBaseSyncResult:
    ingestion_job_id: str
    status: str
    documents_updated: int
    failed_document_count: int | None


INGESTION_OPERATION_TTL_SECONDS = 2 * 60 * 60
_TERMINAL_PROVIDER_STATUSES = {"COMPLETE", "FAILED", "STOPPED"}
_PENDING_PROVIDER_STATUSES = {"STARTING", "IN_PROGRESS", "RUNNING", "STOPPING"}


class IngestionConflictError(ValueError):
    """A different active operation already owns this canonical document set."""


def _validate_document_refs(document_refs: Sequence[DocumentScopeRef]) -> None:
    if not document_refs:
        raise ValueError("at least one explicit document reference is required")
    if len(document_refs) > 20:
        raise ValueError("too many document references")
    if any(not ref.tenant_id or not ref.matter_id or not ref.document_id for ref in document_refs):
        raise ValueError("document references must include tenant, matter, and document IDs")
    if len({(ref.tenant_id, ref.matter_id, ref.document_id) for ref in document_refs}) != len(document_refs):
        raise ValueError("document references must be unique")


def validate_ingestion_documents(
    *, object_verifier: S3ObjectVerifier, metadata_repository: DocumentMetadataRepository,
    document_refs: Sequence[DocumentScopeRef],
) -> tuple[Document, ...]:
    """Validate one explicit scope before a KB start request."""

    _validate_document_refs(document_refs)
    documents: list[Document] = []
    for ref in document_refs:
        document = metadata_repository.get_for_scope(
            tenant_id=ref.tenant_id, matter_id=ref.matter_id, document_id=ref.document_id,
        )
        if document is None:
            raise ValueError("a selected document is not present in the requested scope")
        if document.status not in {DocumentStatus.UPLOADED, DocumentStatus.FAILED}:
            raise ValueError("a selected document is not ready for a new ingestion attempt")
        if document.malware_scan_status is not MalwareScanStatus.CLEAN:
            raise ValueError("a selected document has no clean malware scan")
        documents.append(document)

    for document in documents:
        try:
            original = object_verifier.head_object(key=document.s3_key)
            sidecar = object_verifier.head_object(key=f"{document.s3_key}.metadata.json")
        except Exception as exc:
            raise ValueError("a selected source object or sidecar is unavailable") from exc
        original_size = original.get("ContentLength")
        sidecar_size = sidecar.get("ContentLength")
        original_metadata = original.get("Metadata")
        sidecar_metadata = sidecar.get("Metadata")
        expected_original_metadata = {
            "tenant-id": document.tenant_id,
            "matter-id": document.matter_id,
            "document-id": document.document_id,
        }
        if (
            not isinstance(original_size, int) or isinstance(original_size, bool) or original_size <= 0
            or original_size != document.file_size_bytes or original.get("ContentType") != document.media_type
            or not isinstance(original_metadata, Mapping)
            or any(original_metadata.get(name) != value for name, value in expected_original_metadata.items())
            or not isinstance(sidecar_size, int) or isinstance(sidecar_size, bool)
            or sidecar_size <= 0 or sidecar_size > 10 * 1024
            or sidecar.get("ContentType") != "application/json"
            or not isinstance(sidecar_metadata, Mapping)
            or sidecar_metadata.get("document-id") != document.document_id
        ):
            raise ValueError("a selected source object or sidecar is invalid")
    return tuple(documents)


def run_knowledge_base_sync(
    *,
    client: KnowledgeBaseIngestionClient,
    object_verifier: S3ObjectVerifier,
    metadata_repository: DocumentMetadataRepository,
    knowledge_base_id: str,
    data_source_id: str,
    document_refs: Sequence[DocumentScopeRef],
    timeout_seconds: int = 1_800,
    poll_interval_seconds: int = 10,
    sleep_fn: Callable[[float], None] = sleep,
    monotonic_fn: Callable[[], float] = monotonic,
) -> KnowledgeBaseSyncResult:
    """Start one explicit full data-source sync and update selected documents.

    The operator must provide explicit tenant/matter/document references. No
    table scan occurs. The KB sync itself is global to its S3 data source; a
    successful, failure-free job confirms the selected uploaded documents have
    passed the ingestion run.
    """

    if not knowledge_base_id or not data_source_id:
        raise ValueError("knowledge base and data source IDs are required")
    if timeout_seconds <= 0 or poll_interval_seconds <= 0:
        raise ValueError("timeout and poll interval must be positive")
    documents = validate_ingestion_documents(
        object_verifier=object_verifier,
        metadata_repository=metadata_repository,
        document_refs=document_refs,
    )

    started = client.start_ingestion_job(
        knowledgeBaseId=knowledge_base_id,
        dataSourceId=data_source_id,
        clientToken=str(uuid4()),
    )
    job = started.get("ingestionJob")
    job_id = job.get("ingestionJobId") if isinstance(job, Mapping) else None
    if not isinstance(job_id, str) or not job_id:
        raise RuntimeError("Bedrock did not return an ingestion job ID")

    for document in documents:
        metadata_repository.update_status(
            tenant_id=document.tenant_id,
            matter_id=document.matter_id,
            document_id=document.document_id,
            status=DocumentStatus.PENDING_INGESTION,
        )

    deadline = monotonic_fn() + timeout_seconds
    terminal_status: str | None = None
    final_job: Mapping[str, Any] = {}
    while monotonic_fn() < deadline:
        response = client.get_ingestion_job(
            knowledgeBaseId=knowledge_base_id,
            dataSourceId=data_source_id,
            ingestionJobId=job_id,
        )
        current_job = response.get("ingestionJob")
        if not isinstance(current_job, Mapping):
            raise RuntimeError("Bedrock returned an invalid ingestion job response")
        final_job = current_job
        status = current_job.get("status")
        if status in {"COMPLETE", "FAILED", "STOPPED"}:
            terminal_status = str(status)
            break
        sleep_fn(poll_interval_seconds)

    if terminal_status is None:
        return KnowledgeBaseSyncResult(job_id, "TIMED_OUT", 0, None)

    statistics = final_job.get("statistics")
    failed_count = None
    if isinstance(statistics, Mapping):
        raw_failed_count = statistics.get("numberOfDocumentsFailed")
        if isinstance(raw_failed_count, int) and not isinstance(raw_failed_count, bool):
            failed_count = raw_failed_count

    if terminal_status in {"FAILED", "STOPPED"}:
        for document in documents:
            metadata_repository.update_status(
                tenant_id=document.tenant_id,
                matter_id=document.matter_id,
                document_id=document.document_id,
                status=DocumentStatus.FAILED,
            )
        return KnowledgeBaseSyncResult(job_id, terminal_status, len(documents), failed_count)

    if failed_count == 0:
        for document in documents:
            metadata_repository.update_status(
                tenant_id=document.tenant_id,
                matter_id=document.matter_id,
                document_id=document.document_id,
                status=DocumentStatus.INDEXED,
            )
        return KnowledgeBaseSyncResult(job_id, terminal_status, len(documents), failed_count)

    # A completed job with partial failures does not identify which selected
    # documents failed. Leave them pending rather than claim an unproven state.
    return KnowledgeBaseSyncResult(job_id, terminal_status, 0, failed_count)


class AsyncKnowledgeBaseIngestionService:
    """Start and observe one bounded ingestion job without provider polling loops."""

    def __init__(
        self,
        *,
        client: KnowledgeBaseIngestionClient,
        object_verifier: S3ObjectVerifier,
        metadata_repository: DocumentMetadataRepository,
        state_store: EphemeralStateStore,
        knowledge_base_id: str,
        data_source_id: str,
        clock: Callable[[], float] = time,
        operation_ttl_seconds: int = INGESTION_OPERATION_TTL_SECONDS,
    ) -> None:
        if not knowledge_base_id or not data_source_id:
            raise ValueError("knowledge base and data source IDs are required")
        if operation_ttl_seconds <= 0:
            raise ValueError("operation TTL must be positive")
        self.client = client
        self.object_verifier = object_verifier
        self.metadata_repository = metadata_repository
        self.state_store = state_store
        self.knowledge_base_id = knowledge_base_id
        self.data_source_id = data_source_id
        self.clock = clock
        self.operation_ttl_seconds = operation_ttl_seconds

    @staticmethod
    def _operation_id() -> str:
        return f"ing_{uuid4().hex}"

    @staticmethod
    def _job_id(response: Mapping[str, Any]) -> str:
        job = response.get("ingestionJob")
        job_id = job.get("ingestionJobId") if isinstance(job, Mapping) else None
        if not isinstance(job_id, str) or not job_id.strip() or len(job_id) > 256:
            raise RuntimeError("Bedrock did not return an ingestion job ID")
        return job_id

    @staticmethod
    def _failed_count(job: Mapping[str, Any]) -> int | None:
        statistics = job.get("statistics")
        failed_count = statistics.get("numberOfDocumentsFailed") if isinstance(statistics, Mapping) else None
        return failed_count if isinstance(failed_count, int) and not isinstance(failed_count, bool) and failed_count >= 0 else None

    def start(
        self,
        *,
        subject: str,
        tenant_id: str,
        matter_id: str,
        document_ids: tuple[str, ...],
        correlation_id: str,
        idempotency_key: str,
    ) -> IngestionOperationRecord:
        if not all(isinstance(value, str) and value for value in (subject, tenant_id, matter_id, correlation_id, idempotency_key)):
            raise ValueError("ingestion binding is invalid")
        if len(idempotency_key) > 128:
            raise ValueError("idempotency key is too long")
        if not document_ids:
            raise ValueError("at least one document is required")
        document_set_key = ingestion_document_set_key(
            subject=subject, tenant_id=tenant_id, matter_id=matter_id, document_ids=tuple(document_ids),
        )
        existing = self.state_store.get_ingestion_operation_for_idempotency(
            subject=subject, tenant_id=tenant_id, matter_id=matter_id, idempotency_key=idempotency_key,
        )
        if existing is not None:
            if existing.document_set_key != document_set_key:
                raise ValueError("idempotency key is bound to a different document set")
            return existing
        active = self.state_store.get_ingestion_operation_for_document_set(document_set_key)
        if active is not None:
            raise IngestionConflictError("document set already has an active ingestion operation")
        refs = tuple(DocumentScopeRef(tenant_id, matter_id, document_id) for document_id in document_ids)
        documents = validate_ingestion_documents(
            object_verifier=self.object_verifier,
            metadata_repository=self.metadata_repository,
            document_refs=refs,
        )
        now = self.clock()
        operation = IngestionOperationRecord(
            operation_id=self._operation_id(), idempotency_key=idempotency_key,
            subject=subject, tenant_id=tenant_id, matter_id=matter_id,
            document_ids=tuple(document.document_id for document in documents),
            correlation_id=correlation_id, ingestion_job_id="", status="STARTING",
            provider_status="STARTING", created_at=now, updated_at=now,
            expires_at=now + self.operation_ttl_seconds, document_set_key=document_set_key,
        )
        self.state_store.put_ingestion_operation(operation)
        job_id: str | None = None
        try:
            started = self.client.start_ingestion_job(
                knowledgeBaseId=self.knowledge_base_id,
                dataSourceId=self.data_source_id,
                clientToken=operation.operation_id,
            )
            job_id = self._job_id(started)
            operation = replace(
                operation, ingestion_job_id=job_id, status="RECOVERY_PENDING", provider_status="STARTING",
                updated_at=self.clock(),
            )
            # Persist the job binding before touching document metadata. If a
            # later transition fails, status/reconciliation can recover it.
            self.state_store.update_ingestion_operation(operation)
            for document in documents:
                self.metadata_repository.update_status(
                    tenant_id=document.tenant_id, matter_id=document.matter_id,
                    document_id=document.document_id, status=DocumentStatus.PENDING_INGESTION,
                )
            operation = replace(
                operation, status="PENDING", provider_status="STARTING",
                updated_at=self.clock(),
            )
            self.state_store.update_ingestion_operation(operation)
            return operation
        except Exception:
            # Provider start and metadata writes are ambiguous at this point.
            # Keep the active binding and any known job ID so retries cannot
            # create a second provider job.
            try:
                if job_id is None and operation.status != "STARTING":
                    operation = replace(operation, status="STARTING", provider_status="STARTING", updated_at=self.clock())
                self.state_store.update_ingestion_operation(operation)
            except Exception:
                pass
            raise

    def status(
        self, *, operation_id: str, subject: str, tenant_id: str, matter_id: str,
    ) -> IngestionOperationRecord:
        operation = self.state_store.get_ingestion_operation(operation_id)
        if operation is None:
            raise ValueError("ingestion operation is unavailable")
        if (operation.subject, operation.tenant_id, operation.matter_id) != (subject, tenant_id, matter_id):
            raise AuthorizationDenied("ingestion operation scope is invalid")
        if operation.document_set_key != ingestion_document_set_key(
            subject=operation.subject,
            tenant_id=operation.tenant_id,
            matter_id=operation.matter_id,
            document_ids=operation.document_ids,
        ):
            raise ValueError("ingestion operation binding is malformed")
        if operation.status not in {"STARTING", "RECOVERY_PENDING", "PENDING", "INDEXED", "FAILED"}:
            raise ValueError("ingestion operation is malformed")
        if not operation.ingestion_job_id and not (operation.status == "STARTING" and operation.provider_status == "STARTING"):
            raise ValueError("ingestion operation job binding is malformed")
        if operation.status in {"INDEXED", "FAILED"}:
            return operation
        recovering_terminal = operation.status == "RECOVERY_PENDING" and operation.provider_status in _TERMINAL_PROVIDER_STATUSES
        if operation.status in {"STARTING", "RECOVERY_PENDING"} and operation.ingestion_job_id and not recovering_terminal:
            self._normalize_documents(operation)
            operation = replace(operation, status="PENDING", updated_at=self.clock())
            self.state_store.update_ingestion_operation(operation)
        if not operation.ingestion_job_id:
            return operation
        failed_count = operation.failed_document_count
        provider_status = operation.provider_status
        if provider_status not in _TERMINAL_PROVIDER_STATUSES:
            response = self.client.get_ingestion_job(
                knowledgeBaseId=self.knowledge_base_id,
                dataSourceId=self.data_source_id,
                ingestionJobId=operation.ingestion_job_id,
            )
            job = response.get("ingestionJob")
            if not isinstance(job, Mapping):
                raise RuntimeError("Bedrock returned an invalid ingestion job response")
            provider_status = job.get("status")
            if not isinstance(provider_status, str) or provider_status not in _TERMINAL_PROVIDER_STATUSES | _PENDING_PROVIDER_STATUSES:
                raise RuntimeError("Bedrock returned an invalid ingestion job status")
            failed_count = self._failed_count(job)
        if provider_status not in _TERMINAL_PROVIDER_STATUSES:
            updated = replace(operation, provider_status=provider_status, updated_at=self.clock())
            self.state_store.update_ingestion_operation(updated)
            return updated
        if provider_status in {"FAILED", "STOPPED"}:
            target_status, documents_updated = "FAILED", len(operation.document_ids)
        elif provider_status == "COMPLETE" and failed_count == 0:
            target_status, documents_updated = "INDEXED", len(operation.document_ids)
        else:
            # A complete job with partial/unknown failures does not identify
            # which selected documents failed; keep the lifecycle pending.
            updated = replace(operation, provider_status=provider_status, failed_document_count=failed_count, updated_at=self.clock())
            self.state_store.update_ingestion_operation(updated)
            return updated
        recovery = replace(
            operation, status="RECOVERY_PENDING", provider_status=provider_status,
            failed_document_count=failed_count, updated_at=self.clock(),
        )
        self.state_store.update_ingestion_operation(recovery)
        document_status = DocumentStatus.INDEXED if target_status == "INDEXED" else DocumentStatus.FAILED
        self._normalize_documents(recovery, target_status=document_status)
        for document_id in recovery.document_ids:
            self.metadata_repository.update_status(
                tenant_id=recovery.tenant_id, matter_id=recovery.matter_id,
                document_id=document_id, status=document_status,
            )
        updated = replace(
            recovery, status=target_status, provider_status=provider_status,
            documents_updated=documents_updated, failed_document_count=failed_count,
            updated_at=self.clock(),
        )
        self.state_store.update_ingestion_operation(updated)
        return updated

    def _normalize_documents(
        self, operation: IngestionOperationRecord, *, target_status: DocumentStatus | None = None,
    ) -> None:
        """Restore the exact operation scope to pending before terminal work."""

        for document_id in operation.document_ids:
            document = self.metadata_repository.get_for_scope(
                tenant_id=operation.tenant_id,
                matter_id=operation.matter_id,
                document_id=document_id,
            )
            if document is None:
                raise RuntimeError("bound ingestion document is unavailable")
            if target_status is not None and document.status is target_status:
                continue
            if document.status is DocumentStatus.PENDING_INGESTION:
                continue
            self.metadata_repository.update_status(
                tenant_id=operation.tenant_id,
                matter_id=operation.matter_id,
                document_id=document_id,
                status=DocumentStatus.PENDING_INGESTION,
            )
